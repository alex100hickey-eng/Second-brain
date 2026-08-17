"""
proactive.py — the proactive engine: CLARVIS comes to Alex, not the other way around.

A recurring AWARENESS PASS (server-side, always-on) reviews what the system knows —
tasks, intake events with due dates — and decides whether anything
warrants reaching out to Alex's phone. Delivery is ntfy.sh (Alex's chosen channel):
one HTTPS POST to a private, long-random topic; the ntfy app on his phone shows the
notification and tapping it deep-links back into CLARVIS.

A NAGGING ASSISTANT GETS DELETED. The respect rules are first-class:
  * quiet hours (default 22:00–08:00 local) — nothing sends, ever;
  * max nudges/day (default 8) — hard cap;
  * every nudge has a KEY; keys collapse to a CONCERN (date suffixes stripped) and
    a concern nudges at most `max_per_concern` times (default 2) per
    `concern_window_days` — Alex's rule: once or twice per thing, then silence;
  * a concern can't re-nudge within `renudge_after_hours` of its last send
    (recurring windows like the morning brief are exempt from the lifetime cap
    but still spaced by this);
  * without NTFY_TOPIC configured, nothing can send at all (nudges just log).

All of this is decided from ONE compact state row ("notify:sent"), not by
scanning the ledger — the old ledger-window approach broke silently once skip
rows outnumbered sent rows (seen 2026-08-05: the same 6 nudges sent 3× in a day
past an 8/day cap, because every rule read the last 80 rows of a ledger getting
hundreds of skip rows a day). Skips are no longer logged at all; the ledger
records only sent/failed, so it stays a meaningful audit trail.
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
# Timed reminders ("remind me AT 4pm") enter the nudge window this many hours before the
# moment — with the 15-min pass cadence the nudge lands ~30-45 min ahead, once, at high
# priority. Date-only deadlines ("essay due Friday") keep the roomy DUE_SOON_HOURS flow
# (heads-up a day out + escalation close to due). Window must stay > pass interval or a
# reminder can fall between passes and never fire.
TIMED_REMIND_HOURS = 0.75

DEFAULT_CONFIG = {
    "enabled": True,
    "quiet_start": "22:00",   # local time, inclusive
    "quiet_end": "08:00",     # local time, exclusive
    "max_per_day": 8,
    "morning_brief": "08:15",  # "" disables
    "evening_review": "20:30",  # "" disables
    "max_per_concern": 2,       # lifetime sends per concern within the window
    "renudge_after_hours": 20,  # min gap between sends of the same concern
    "concern_window_days": 7,   # after this quiet, a concern may earn 2 more
}
SENT_KEY = "notify:sent"       # the one state row every respect rule reads

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


import re as _re

_DATE_SUFFIX = _re.compile(r":\d{4}-\d{2}-\d{2}(-[AP]M)?$")


def _base_key(key: str) -> str:
    """Collapse a nudge key to its CONCERN: strip trailing date/half-day suffixes
    so legacy callers that bake the date into the key (august:overdue:x:2026-08-05)
    still count as ONE thing across days."""
    prev = None
    while prev != key:
        prev, key = key, _DATE_SUFFIX.sub("", key)
    return key


def _sent_state() -> dict:
    """The one decision record: per-concern counts + today's send counter.
    {"concerns": {base: {"n": int, "last": iso}}, "day": "YYYY-MM-DD", "day_sent": int}"""
    st = intake_mod._load_state(SENT_KEY)
    st.setdefault("concerns", {})
    today = _now().strftime("%Y-%m-%d")
    if st.get("day") != today:            # midnight rollover resets the daily cap
        st["day"], st["day_sent"] = today, 0
    return st


def _save_sent_state(st: dict) -> None:
    cutoff = (_now() - timedelta(days=30)).isoformat()
    st["concerns"] = {k: v for k, v in st["concerns"].items()
                      if (v.get("last") or "") >= cutoff}
    intake_mod._save_state(st)


def _sent_today() -> int:
    return int(_sent_state().get("day_sent", 0))


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
               tags: str = "brain", force: bool = False, recurring: bool = False,
               renudge_hours: float = None) -> str:
    """The ONLY door to Alex's phone. Applies every respect rule, then sends.

    `recurring=True` marks a daily window (morning brief) — exempt from the
    per-concern lifetime cap but still spaced by renudge_after_hours.
    `renudge_hours` overrides the spacing for this call (e.g. a deadline nudge
    that's allowed one escalation a few hours before it's due)."""
    cfg = get_config()
    topic = os.environ.get("NTFY_TOPIC", "")
    st = _sent_state()
    base = _base_key(key)
    concern = st["concerns"].get(base) or {}
    now = _now()
    gap_h = float(renudge_hours if renudge_hours is not None
                  else cfg.get("renudge_after_hours", 20))

    reason = None
    if not cfg.get("enabled"):
        reason = "notifications disabled"
    elif not topic:
        reason = "no NTFY_TOPIC configured"
    elif not force and _in_quiet_hours(cfg):
        reason = "quiet hours"
    elif not force and st.get("day_sent", 0) >= int(cfg.get("max_per_day", 8)):
        reason = "daily cap reached"
    elif concern and not force:
        try:
            last = datetime.fromisoformat(concern.get("last", ""))
        except ValueError:
            last = None
        window = timedelta(days=float(cfg.get("concern_window_days", 7)))
        if last and (now - last) >= window and not recurring:
            concern = {}          # quiet a full window → the concern earns a fresh 2
            st["concerns"].pop(base, None)
        elif last and (now - last) < timedelta(hours=gap_h):
            reason = "already nudged about this recently"
        elif (not recurring
              and concern.get("n", 0) >= int(cfg.get("max_per_concern", 2))):
            reason = (f"already nudged {concern.get('n')}x about this — "
                      f"muted for {cfg.get('concern_window_days', 7)} days")
    if reason:
        # Deliberately NOT logged as a row: skip-row flooding is what broke the
        # ledger-based rules. The returned string is the caller's audit.
        return f"Nudge skipped ({reason})."
    try:
        _sender(topic, title, body, priority, tags)
        st["concerns"][base] = {"n": int(concern.get("n", 0)) + 1,
                                "last": now.isoformat()}
        st["day_sent"] = int(st.get("day_sent", 0)) + 1
        _save_sent_state(st)
        _log_nudge({"key": key, "concern": base, "title": title, "body": body,
                    "status": "sent"})
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

    # Real due timestamps on tasks — the reminders feature ("remind me to call coach at
    # 4pm" -> create_task with due). Timed dues get the tight TIMED_REMIND_HOURS window
    # (one sharp nudge shortly before the moment); date-only dues get the full day-ahead
    # flow. Refs dedupe against the legacy title-string path above so a task can never
    # nudge twice through two doors.
    try:
        from task_tracker import due_moment, is_timed
        seen_refs = {d["ref"] for d in picture["due_soon"]}
        for t in task_tracker.open_with_due():
            ref = f"task:{t.get('id')}"
            if ref in seen_refs:
                continue
            dt = due_moment(t.get("due", ""), tz=LOCAL_TZ)
            if dt is None:
                continue
            hours = (dt - now).total_seconds() / 3600
            window = TIMED_REMIND_HOURS if is_timed(t["due"]) else DUE_SOON_HOURS
            if -2 <= hours <= window:
                picture["due_soon"].append(
                    {"what": t.get("title", ""), "due": t["due"],
                     "hours": round(hours, 1), "ref": ref})
    except Exception:
        pass
    return picture


def _august_actions() -> list:
    """Nudges sourced from the August plan tracker.

    The tracker decides WHAT is worth raising (only steps that are actionable now —
    never ones blocked upstream, which would be noise Alex can't act on); this layer
    still applies quiet hours, the daily cap and per-key dedup like any other nudge.
    Fail-soft: an unreadable tracker must not take down the awareness pass."""
    out = []
    try:
        import august_tracker
        for n in august_tracker.nudges_due():
            out.append(send_nudge(n["key"], n["title"], n["body"],
                                  priority=n.get("priority", "default"),
                                  tags="calendar",
                                  # Streaks clear the twice-then-silence cap.
                                  recurring=n.get("recurring", False)))
    except Exception as e:
        if report_event:
            try:
                report_event("proactive", "warning",
                             "august tracker nudges unavailable", str(e)[:300])
            except Exception:
                pass
    return out


def _due_context(ref: str) -> str:
    """Provenance lines for a due-soon nudge — who asked, and the original text.
    A nudge that names its source gets acted on; 'open CLARVIS for details' gets
    ignored. Fail-soft: no context beats no nudge."""
    try:
        kind, _, rid = ref.partition(":")
        if kind == "intake":
            ev = intake_mod._event_row(int(rid))
            if ev:
                src = ev.get("source", "?")
                sender = ev.get("sender") or "?"
                prev = (ev.get("preview") or "").strip().replace("\n", " ")[:160]
                return f"From {sender} (via {src}): “{prev}”"
        elif kind == "task":
            t = task_tracker.get(int(rid))
            if t:
                desc = (t.get("description") or "").strip().replace("\n", " ")[:160]
                return f"Task #{rid}. {desc}" if desc else f"Task #{rid}."
    except Exception:
        pass
    return ""


def _when_words(hours: float, due: str) -> str:
    try:
        dt = datetime.fromisoformat(due)
        clock = dt.strftime("%-I:%M%p").lower()
    except ValueError:
        clock = due
    if hours <= 0:
        return f"NOW ({clock})"
    if hours < 1:
        return f"within the hour ({clock})"
    if hours < 24:
        return f"in {int(hours)}h ({clock})"
    return f"{clock}"


def run_awareness_pass(force: bool = False) -> str:
    """One decision cycle. Deterministic triggers, deterministic text — every
    nudge must name the thing, the source, and the next physical action."""
    cfg = get_config()
    picture = _gather()
    now = picture["now"]
    actions = []
    actions.extend(_august_actions())

    # 1. Deadlines approaching. Concern = the item itself (no date in the key):
    #    lifetime max 2 = one heads-up ~a day out + one escalation close to due,
    #    enabled by the 6h renudge override. Never a third.
    for d in picture["due_soon"]:
        close = d["hours"] <= 3
        title = (f"🔴 DUE {_when_words(d['hours'], d['due'])} — {d['what'][:55]}"
                 if close else
                 f"⏰ Today {_when_words(d['hours'], d['due'])} — {d['what'][:55]}")
        ctx = _due_context(d["ref"])
        body = "\n".join(x for x in (
            d["what"],
            f"Due: {_when_words(d['hours'], d['due'])}",
            ctx,
            "Do it (or drop it), then mark it done in CLARVIS so this stays quiet.",
        ) if x)
        actions.append(send_nudge(
            f"due:{d['ref']}", title, body,
            priority="high" if close else "default",
            tags="alarm_clock", force=force, renudge_hours=6))

    # 2. Intake pile-up. Only when the pile GREW meaningfully since the last
    #    nudge about it — a static pile he chose not to triage is not news.
    n_intake = picture["new_intake"]
    st = _sent_state()
    last_n = int(st.get("intake_last_n", 0))
    if n_intake >= 3 and (n_intake >= last_n + 5 or last_n == 0):
        tops = []
        try:
            for r in intake_mod.list_intake("new", limit=3):
                ev = r["event"]
                first = (ev.get("items") or [{}])[0].get("text") or ev.get("preview", "")
                tops.append(f"• {ev.get('sender', '?')}: {str(first)[:70]}")
        except Exception:
            pass
        body = "\n".join(
            [f"{n_intake} extracted obligations waiting. Newest:"] + tops +
            ["Triage takes one tap each: accept → task, or dismiss."])
        r = send_nudge("intake-pileup",
                       f"📥 {n_intake} waiting — triage is the bottleneck",
                       body, tags="inbox_tray", force=force, recurring=True)
        if r.startswith("Nudge sent"):
            st = _sent_state()
            st["intake_last_n"] = n_intake
            _save_sent_state(st)
        actions.append(r)

    # 3. Morning brief / evening review windows (recurring; skipped entirely on
    #    an empty day — "0 open, 0 due" is not a notification, it's noise).
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
        n_open = len(picture["open_tasks"])
        if n_open == 0 and n_intake == 0 and not picture["due_soon"]:
            continue   # nothing actionable — the respectful brief is no brief
        head = []
        for d in picture["due_soon"][:3]:
            head.append(f"• {_when_words(d['hours'], d['due'])}: {d['what'][:60]}")
        for t in picture["open_tasks"][:max(0, 3 - len(head))]:
            head.append(f"• open: {t[:60]}")
        if cfg_key == "morning_brief":
            title = (f"{emoji} Today: {len(picture['due_soon'])} deadline(s), "
                     f"{n_open} open")
            tail = ([f"…plus {n_intake} intake to triage."] if n_intake else [])
        else:
            title = f"{emoji} Still open tonight: {n_open + n_intake} thing(s)"
            tail = ["Two minutes now saves tomorrow morning."]
        actions.append(send_nudge(
            f"{cfg_key}:{now.strftime('%Y-%m-%d')}", title,
            "\n".join(head + tail),
            tags="sunrise" if "morning" in cfg_key else "city_sunset",
            force=force, recurring=True))

    sent = sum(1 for a in actions if a.startswith("Nudge sent"))
    # Heartbeat: a pass that decides to send NOTHING is still a completed pass —
    # nudge rows can't tell "quiet by choice" from "worker dead", this can.
    try:
        import monitor
        monitor.beat("proactive", stale_after_s=8 * PASS_INTERVAL,   # 2h on a 15-min cadence
                     note=f"{sent} sent")
    except Exception:
        pass
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
    st = _sent_state()
    lines = [f"Notifications {'ON' if cfg['enabled'] else 'OFF'} — "
             f"quiet {cfg['quiet_start']}–{cfg['quiet_end']}, max {cfg['max_per_day']}/day, "
             f"{cfg['max_per_concern']}x per concern per {cfg['concern_window_days']}d, "
             f"brief {cfg['morning_brief'] or 'off'}, review {cfg['evening_review'] or 'off'}, "
             f"channel {'configured' if os.environ.get('NTFY_TOPIC') else 'NOT CONFIGURED'}. "
             f"Sent today: {st.get('day_sent', 0)}; tracked concerns: {len(st.get('concerns', {}))}."]
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
            "evening_review": {"type": "string", "description": "HH:MM or '' to disable"},
            "max_per_concern": {"type": "integer",
                                "description": "Lifetime nudges per concern in the window (default 2)"},
            "renudge_after_hours": {"type": "integer",
                                    "description": "Min hours between nudges of the same concern"},
            "concern_window_days": {"type": "integer",
                                    "description": "Quiet days after which a concern may re-earn nudges"}}},
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
