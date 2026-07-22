"""
proactive.py — the proactive engine: CLARVIS comes to Alex, not the other way around.

A recurring AWARENESS PASS (server-side, always-on) reviews what the system knows —
tasks, intake events with due dates, today's calendar — and decides whether anything
warrants reaching out to Alex's phone. Delivery is ntfy.sh (Alex's chosen channel):
one HTTPS POST to a private, long-random topic; the ntfy app on his phone shows the
notification and tapping it deep-links back into CLARVIS.

A NAGGING ASSISTANT GETS DELETED. The respect rules are first-class:
  * quiet hours (default 22:00–08:00 local) — nothing sends, ever;
  * max nudges/day (default 8) — hard cap;
  * every nudge has a KEY — the same concern never nudges twice in its window;
  * without NTFY_TOPIC configured, nothing can send at all (nudges just log).
Config lives in a Supabase row (key "notify:config") so Alex's settings apply on
every device and survive restarts; `set_notification_rules` edits it from chat.

What triggers a nudge (deterministic rules, model only WRITES the message):
  * an intake item or task due within DUE_SOON_HOURS that is still open;
  * new intake events waiting for triage (batched — one nudge, not N);
  * morning brief window: a one-shot "here's your day" summary;
  * evening window: a one-shot review prompt when things are still open.

Sending is fail-soft and auditable: every attempt (sent/skipped/why) is recorded as
a "jarvis_nudge" row. No message content goes anywhere except to ntfy (Alex's own
channel); nudge text is short and contains only what Alex needs to act.
"""

import json
import os
import ssl
import threading
import time
import urllib.request
from datetime import datetime, timedelta

try:
    import certifi
    _SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CONTEXT = None   # falls back to the system trust store

# Injected by init()
claude = None
supabase = None
dispatch_tool = None
task_tracker = None
intake_mod = None
report_event = None
LOCAL_TZ = None

NUDGE_AGENT = "jarvis_nudge"
CONFIG_KEY = "notify:config"
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh")
DEEP_LINK = os.environ.get("JARVIS_PUBLIC_URL",
                           "https://clarvis.178.156.209.40.sslip.io") + "/dashboard"
PASS_INTERVAL = 15 * 60
DUE_SOON_HOURS = 24

DEFAULT_CONFIG = {
    "enabled": True,
    "quiet_start": "22:00",   # local time, inclusive
    "quiet_end": "08:00",     # local time, exclusive
    "max_per_day": 8,
    "morning_brief": "08:15",  # "" disables
    "evening_review": "20:30",  # "" disables
}

_worker_started = False


def init(claude_client, supabase_client, tool_dispatcher, tracker, intake_module,
         report_event_fn=None, local_tz=None):
    global claude, supabase, dispatch_tool, task_tracker, intake_mod, report_event, LOCAL_TZ
    claude = claude_client
    supabase = supabase_client
    dispatch_tool = tool_dispatcher
    task_tracker = tracker
    intake_mod = intake_module
    report_event = report_event_fn
    LOCAL_TZ = local_tz


def _now():
    return datetime.now(LOCAL_TZ) if LOCAL_TZ else datetime.now()


# ============================================================
# Config + nudge ledger (Supabase-backed, cross-device)
# ============================================================

def get_config() -> dict:
    state = intake_mod._load_state(CONFIG_KEY)
    cfg = dict(DEFAULT_CONFIG)
    cfg.update({k: v for k, v in state.items()
                if k in DEFAULT_CONFIG})
    return cfg


def set_config(**changes) -> dict:
    state = intake_mod._load_state(CONFIG_KEY)
    for k, v in changes.items():
        if k in DEFAULT_CONFIG and v is not None:
            state[k] = v
    intake_mod._save_state(state)
    return get_config()


def _nudge_rows(limit: int = 80) -> list:
    rows = (supabase.table("Agent Outputs").select("*")
            .eq("agent_name", NUDGE_AGENT).order("id", desc=True)
            .limit(limit).execute().data or [])
    out = []
    for r in rows:
        try:
            out.append(json.loads(r["output_text"]))
        except (json.JSONDecodeError, TypeError):
            continue
    return out


def _log_nudge(record: dict) -> None:
    record["at"] = _now().isoformat()
    supabase.table("Agent Outputs").insert(
        {"agent_name": NUDGE_AGENT, "output_text": json.dumps(record)}).execute()


def _sent_today() -> int:
    today = _now().strftime("%Y-%m-%d")
    return sum(1 for n in _nudge_rows()
               if n.get("status") == "sent" and (n.get("at") or "").startswith(today))


def _already_nudged(key: str, within_hours: int = 20) -> bool:
    cutoff = (_now() - timedelta(hours=within_hours)).isoformat()
    return any(n.get("key") == key and n.get("status") == "sent"
               and (n.get("at") or "") >= cutoff for n in _nudge_rows())


def _in_quiet_hours(cfg: dict) -> bool:
    now = _now().strftime("%H:%M")
    start, end = cfg["quiet_start"], cfg["quiet_end"]
    if start <= end:                      # e.g. 01:00–06:00
        return start <= now < end
    return now >= start or now < end      # e.g. 22:00–08:00 (wraps midnight)


# ============================================================
# Delivery — ntfy.sh (fail-soft; disabled without a topic)
# ============================================================

def _header_safe(value: str) -> str:
    """HTTP headers must be Latin-1-transportable; ntfy titles/tags carry emoji.
    Encoding UTF-8 bytes as Latin-1 text is a lossless, reversible round-trip (both
    are 1-byte-per-codepoint over 0-255) — urllib re-encodes to the original UTF-8
    bytes on the wire, and ntfy decodes the header as UTF-8 on its end."""
    return value.encode("utf-8").decode("latin-1")


def _post_ntfy(topic: str, title: str, body: str, priority: str, tags: str) -> None:
    req = urllib.request.Request(
        f"{NTFY_SERVER}/{topic}", data=body.encode(),
        headers={"Title": _header_safe(title), "Priority": priority,
                 "Tags": _header_safe(tags), "Click": DEEP_LINK})
    urllib.request.urlopen(req, timeout=10, context=_SSL_CONTEXT).read()


_sender = _post_ntfy   # test seam


def send_nudge(key: str, title: str, body: str, priority: str = "default",
               tags: str = "brain", force: bool = False) -> str:
    """The ONLY door to Alex's phone. Applies every respect rule, then sends."""
    cfg = get_config()
    topic = os.environ.get("NTFY_TOPIC", "")
    reason = None
    if not cfg.get("enabled"):
        reason = "notifications disabled"
    elif not topic:
        reason = "no NTFY_TOPIC configured"
    elif not force and _in_quiet_hours(cfg):
        reason = "quiet hours"
    elif not force and _sent_today() >= int(cfg.get("max_per_day", 8)):
        reason = "daily cap reached"
    elif _already_nudged(key):
        reason = "already nudged about this"
    if reason:
        _log_nudge({"key": key, "title": title, "status": "skipped", "why": reason})
        return f"Nudge skipped ({reason})."
    try:
        _sender(topic, title, body, priority, tags)
        _log_nudge({"key": key, "title": title, "body": body, "status": "sent"})
        return f"Nudge sent: {title}"
    except Exception as e:
        _log_nudge({"key": key, "title": title, "status": "failed", "why": str(e)})
        if report_event:
            try:
                report_event("proactive", "warning", "nudge delivery failed", str(e))
            except Exception:
                pass
        return f"Nudge delivery failed: {e}"


# ============================================================
# The awareness pass
# ============================================================

def _gather() -> dict:
    """Everything the decision rules look at. Fail-soft per source."""
    now = _now()
    picture = {"now": now, "due_soon": [], "new_intake": 0, "open_tasks": []}
    try:
        for r in intake_mod.list_intake("new", limit=40):
            ev = r["event"]
            picture["new_intake"] += 1
            for item in ev.get("items", []):
                due = item.get("due")
                if not due:
                    continue
                try:
                    dt = datetime.fromisoformat(due)
                    if dt.tzinfo is None and LOCAL_TZ:
                        dt = dt.replace(tzinfo=LOCAL_TZ)
                except ValueError:
                    continue
                hours = (dt - now).total_seconds() / 3600
                if -2 <= hours <= DUE_SOON_HOURS:
                    picture["due_soon"].append(
                        {"what": item["text"], "due": due, "hours": round(hours, 1),
                         "ref": f"intake:{r['id']}"})
    except Exception:
        pass
    try:
        for t in task_tracker.top_by_priority(limit=10):
            if t.get("status") in ("idea", "approved", "in_progress"):
                picture["open_tasks"].append(t.get("title", ""))
                # accepted-intake tasks carry "(due YYYY-MM-DD...)" in the title
                title = t.get("title", "")
                if "(due " in title:
                    due = title.split("(due ", 1)[1].rstrip(")").strip()
                    try:
                        dt = datetime.fromisoformat(due)
                        if dt.tzinfo is None and LOCAL_TZ:
                            dt = dt.replace(tzinfo=LOCAL_TZ)
                        hours = (dt - now).total_seconds() / 3600
                        if -2 <= hours <= DUE_SOON_HOURS:
                            picture["due_soon"].append(
                                {"what": title, "due": due, "hours": round(hours, 1),
                                 "ref": f"task:{t.get('id')}"})
                    except ValueError:
                        pass
    except Exception:
        pass
    return picture


def run_awareness_pass(force: bool = False) -> str:
    """One decision cycle. Deterministic triggers; the model only phrases text."""
    cfg = get_config()
    picture = _gather()
    now = picture["now"]
    actions = []

    # 1. Deadlines approaching (one nudge per item, keyed by ref+day)
    for d in picture["due_soon"]:
        key = f"due:{d['ref']}:{now.strftime('%Y-%m-%d')}"
        when = ("NOW" if d["hours"] <= 0 else
                f"in {int(d['hours'])}h" if d["hours"] >= 1 else "within the hour")
        actions.append(send_nudge(
            key, f"⏰ {when}: {d['what'][:70]}",
            f"{d['what']}\nDue {d['due']}. Open CLARVIS to see details.",
            priority="high" if d["hours"] <= 3 else "default",
            tags="alarm_clock", force=force))

    # 2. Intake pile-up (one batched nudge per day-half)
    if picture["new_intake"] >= 3:
        key = f"intake:{now.strftime('%Y-%m-%d-%p')}"
        actions.append(send_nudge(
            key, f"📥 {picture['new_intake']} things waiting for triage",
            "New texts/emails/events with extracted obligations are waiting. "
            "One tap each to accept into tasks or dismiss.",
            tags="inbox_tray", force=force))

    # 3. Morning brief / evening review windows (one-shot per day)
    for label, cfg_key, emoji in (("morning brief", "morning_brief", "🌅"),
                                  ("evening review", "evening_review", "🌆")):
        target = cfg.get(cfg_key) or ""
        if not target:
            continue
        try:
            t_h, t_m = map(int, target.split(":"))
        except ValueError:
            continue
        window_start = now.replace(hour=t_h, minute=t_m, second=0, microsecond=0)
        if not (window_start <= now < window_start + timedelta(minutes=PASS_INTERVAL // 60 + 20)):
            continue
        key = f"{cfg_key}:{now.strftime('%Y-%m-%d')}"
        n_open = len(picture["open_tasks"])
        if cfg_key == "morning_brief":
            body = (f"{n_open} open task(s), {picture['new_intake']} intake to triage, "
                    f"{len(picture['due_soon'])} deadline(s) in the next day. "
                    "Tap for the full picture.")
        else:
            if n_open == 0 and picture["new_intake"] == 0:
                continue   # quiet evening — don't nudge about nothing
            body = (f"Still open: {n_open} task(s), {picture['new_intake']} untriaged. "
                    "Two minutes now saves tomorrow morning.")
        actions.append(send_nudge(key, f"{emoji} {label.title()}", body,
                                  tags="sunrise" if "morning" in cfg_key else "city_sunset",
                                  force=force))

    sent = sum(1 for a in actions if a.startswith("Nudge sent"))
    return (f"Awareness pass: {len(picture['due_soon'])} due-soon, "
            f"{picture['new_intake']} untriaged → {sent} sent, "
            f"{len(actions) - sent} suppressed by respect rules.")


# ============================================================
# Worker + tools
# ============================================================

def _loop():
    while True:
        try:
            run_awareness_pass()
        except Exception as e:
            try:
                if report_event:
                    report_event("proactive", "error", "awareness pass failed", str(e))
            except Exception:
                pass
        time.sleep(PASS_INTERVAL)


def start_worker() -> bool:
    global _worker_started
    if _worker_started:
        return False
    t = threading.Thread(target=_loop, daemon=True, name="jarvis-proactive")
    t.start()
    _worker_started = True
    return True


def status_text() -> str:
    cfg = get_config()
    recent = [n for n in _nudge_rows(20)]
    lines = [f"Notifications {'ON' if cfg['enabled'] else 'OFF'} — "
             f"quiet {cfg['quiet_start']}–{cfg['quiet_end']}, max {cfg['max_per_day']}/day, "
             f"brief {cfg['morning_brief'] or 'off'}, review {cfg['evening_review'] or 'off'}, "
             f"channel {'configured' if os.environ.get('NTFY_TOPIC') else 'NOT CONFIGURED'}."]
    for n in recent[:8]:
        lines.append(f"  [{(n.get('at') or '')[:16]}] {n.get('status')}: "
                     f"{n.get('title')}{' — ' + n.get('why', '') if n.get('why') else ''}")
    return "\n".join(lines)


TOOL_SCHEMAS = [
    {
        "name": "check_notifications",
        "description": "Show the proactive-nudge settings (quiet hours, daily cap, brief/"
                       "review times, channel state) and the recent nudge log.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "set_notification_rules",
        "description": "Update Alex's nudge respect rules: quiet hours, max per day, "
                       "morning brief / evening review times ('' disables), or enabled on/off.",
        "input_schema": {"type": "object", "properties": {
            "enabled": {"type": "boolean"},
            "quiet_start": {"type": "string", "description": "HH:MM local"},
            "quiet_end": {"type": "string", "description": "HH:MM local"},
            "max_per_day": {"type": "integer"},
            "morning_brief": {"type": "string", "description": "HH:MM or '' to disable"},
            "evening_review": {"type": "string", "description": "HH:MM or '' to disable"}}},
    },
    {
        "name": "run_awareness_now",
        "description": "Run the proactive awareness pass immediately (deadlines, intake "
                       "pile-up, brief windows) and report what it sent or suppressed.",
        "input_schema": {"type": "object", "properties": {
            "force": {"type": "boolean",
                      "description": "Bypass quiet-hours/caps for a live test."}}},
    },
    {
        "name": "test_nudge",
        "description": "Send one test notification to Alex's phone right now to prove the "
                       "channel works.",
        "input_schema": {"type": "object", "properties": {}},
    },
]

TOOL_STATUS_LABELS = {
    "check_notifications": "Checking your nudge settings…",
    "set_notification_rules": "Updating your notification rules…",
    "run_awareness_now": "Scanning for anything worth telling you…",
    "test_nudge": "Pinging your phone…",
}
