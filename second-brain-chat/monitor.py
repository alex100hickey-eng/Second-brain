"""
MONITORING AGENT — one place that watches the whole build: health + cost.

  2a. HEALTH — watches worker-thread liveness (via threading.enumerate(), so no
      changes were needed to how workers start), reads health.py's static system
      check, and reads system_events — a shared report_event() log that other
      components call from their except blocks (a few lines each, not a rewrite:
      see task_manager._managed_worker and app.py's _task_worker). Produces a
      plain-English incident report, delivered to chat/dashboard/morning brief.
      A conservative FIXER (v1) may auto-act on a strict, user-editable allowlist
      (monitor_config.json); anything else proposes via the EXISTING approval
      queue (jarvis_pending_action) instead of inventing a new one.
  2b. COST — extends observability.py's cost tracking with a monthly rollup and
      three budget tiers (budget_config.json — PLACEHOLDER numbers, clearly
      marked for Alex to set for real). warn notifies; throttle pauses
      non-essential automated agents via is_agent_allowed(); shutdown stops all
      automated agents until Alex re-enables (the chat brain, being 'essential',
      keeps working but warns on every message). Every tier transition is logged
      with the numbers that triggered it.

Supabase row types (same "Agent Outputs" piggyback convention as the rest of the
codebase):
  system_event        one row per reported problem/notice. level: info|warning|
                       error|critical. Written by report_event() from anywhere.
  jarvis_budget_state  the current budget tier + the numbers behind it, so a
                       transition is only logged/notified once, not every cycle.
"""

import json
import os
import subprocess
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

_TZ = ZoneInfo("America/New_York")
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

MONITOR_CONFIG_PATH = os.path.join(HERE, "monitor_config.json")
BUDGET_CONFIG_PATH = os.path.join(HERE, "budget_config.json")

# ---- shared context, injected by app.py via init() (same pattern as the other subsystems) ----
supabase = None
claude = None
post_to_chat = None  # app.save_chat_message — same sink task_manager/expansion_pipeline use
health_module = None  # app's `health` import (health.run_health_check / health_text)

_WORKER_RESTARTERS = {}   # thread-name -> zero-arg callable that (re)starts it
_LAST_TIER = {"tier": None}          # in-process cache; source of truth is Supabase
_LAST_EVENT_ID_SEEN = {"id": 0}      # dedupe: don't re-notify on the same event every cycle


def init(supabase_client, claude_client, post_to_chat_fn, health_mod):
    global supabase, claude, post_to_chat, health_module
    supabase = supabase_client
    claude = claude_client
    post_to_chat = post_to_chat_fn
    health_module = health_mod


def register_worker(name: str, restart_fn) -> None:
    """Let app.py tell the monitor how to restart a named daemon thread, e.g.
    monitor.register_worker('jarvis-managed-worker', lambda: task_manager.start_managed_worker(post_to_chat=save_chat_message))
    A worker not registered here is still liveness-checked, just not auto-restartable
    (the fixer will propose a manual restart instead)."""
    _WORKER_RESTARTERS[name] = restart_fn


def _now_iso() -> str:
    return datetime.now(_TZ).isoformat()


# ============================================================
# CONFIG — both files are user-editable; ship sensible, clearly-marked defaults
# if missing so the monitor still works out of the box.
# ============================================================

_DEFAULT_MONITOR_CONFIG = {
    "_comment": "Edit this to change what the fixer may do automatically. Anything not "
               "listed here always proposes a fix and waits for your dashboard approval.",
    "fixer_allowlist": ["restart_crashed_worker", "clear_temp_dir", "retry_transient_api"],
    "scan_interval_seconds": 300,
    "incident_window_hours": 6,
}

_DEFAULT_BUDGET_CONFIG = {
    "_comment": "PLACEHOLDER NUMBERS — set monthly_budget_usd to your real monthly cap.",
    "monthly_budget_usd": 20.0,
    "tiers": {"warn": 0.50, "throttle": 0.80, "shutdown": 1.00},
    "essential_features": ["chat"],
}


def _load_json_config(path: str, default: dict) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
        merged = dict(default)
        merged.update(cfg)
        return merged
    except (FileNotFoundError, json.JSONDecodeError):
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(default, f, indent=2)
        except OSError:
            pass
        return dict(default)


def monitor_config() -> dict:
    return _load_json_config(MONITOR_CONFIG_PATH, _DEFAULT_MONITOR_CONFIG)


def budget_config() -> dict:
    return _load_json_config(BUDGET_CONFIG_PATH, _DEFAULT_BUDGET_CONFIG)


# ============================================================
# SYSTEM EVENTS — the shared log other components report into.
# ============================================================

def report_event(component: str, level: str, message: str, detail: str = "") -> None:
    """Shared incident/notice log. Fail-soft: a logging hiccup must never break the
    thing being reported on. Call this from an except block — a couple of lines,
    not a rewrite (see task_manager._managed_worker / app.py's _task_worker)."""
    if level not in ("info", "warning", "error", "critical"):
        level = "info"
    try:
        supabase.table("Agent Outputs").insert(
            {"agent_name": "system_event", "output_text": json.dumps({
                "component": component, "level": level,
                "message": str(message)[:500], "detail": str(detail)[:800],
                "ts": _now_iso(),
            })}
        ).execute()
    except Exception:
        pass


def get_recent_events(limit: int = 40, min_level: str = "info",
                      max_age_hours: float = None) -> list:
    """[{"id", **event}], newest first. `max_age_hours` drops events older than the
    window so a long-quiet system stops surfacing stale incidents (an event with an
    unparseable timestamp is kept — never silently hide a report)."""
    order = {"info": 0, "warning": 1, "error": 2, "critical": 3}
    floor = order.get(min_level, 0)
    cutoff = None
    if max_age_hours is not None:
        cutoff = datetime.now(ZoneInfo("America/New_York")) - timedelta(hours=max_age_hours)
    rows = (
        supabase.table("Agent Outputs").select("*")
        .eq("agent_name", "system_event").order("id", desc=True)
        .limit(limit * 3).execute().data or []
    )
    out = []
    for row in rows:
        try:
            e = json.loads(row["output_text"])
        except (json.JSONDecodeError, TypeError):
            continue
        if order.get(e.get("level"), 0) >= floor:
            if cutoff is not None:
                try:
                    if datetime.fromisoformat(e.get("ts", "")) < cutoff:
                        continue
                except (ValueError, TypeError):
                    pass  # unparseable ts — keep the event
            out.append({"id": row["id"], **e})
        if len(out) >= limit:
            break
    return out


# ============================================================
# 2a. HEALTH — worker liveness + static health check + recent events
# ============================================================

def _worker_liveness() -> dict:
    alive_names = {t.name for t in threading.enumerate()}
    return {name: (name in alive_names) for name in _WORKER_RESTARTERS} or \
           {name: (name in alive_names) for name in
            ("jarvis-managed-worker", "jarvis-task-worker", "jarvis-monitor")}


def run_health_scan() -> dict:
    """Combine static health (DBs/binaries/index/disk — health.py), worker liveness,
    and recent error/critical events into one picture."""
    static = {}
    if health_module is not None:
        try:
            static = health_module.run_health_check()
        except Exception as e:
            static = {"overall": "critical", "checks": [], "error": str(e)[:200]}

    workers = _worker_liveness()
    dead = [n for n, ok in workers.items() if not ok]

    recent_bad = get_recent_events(
        limit=20, min_level="error",
        max_age_hours=monitor_config().get("incident_window_hours", 6))

    incidents = []
    for name in dead:
        incidents.append({"type": "worker_down", "component": name,
                          "message": f"Worker thread '{name}' is not running."})
    for e in recent_bad:
        incidents.append({"type": "reported_event", "component": e.get("component"),
                          "message": e.get("message"), "level": e.get("level"),
                          "event_id": e.get("id")})

    overall = static.get("overall", "healthy")
    if dead or any(i.get("level") == "critical" for i in incidents):
        overall = "critical"
    elif incidents and overall == "healthy":
        overall = "degraded"

    return {"overall": overall, "static": static, "workers": workers, "incidents": incidents,
           "scanned_at": _now_iso()}


def incident_report_text(scan: dict = None) -> str:
    """Plain-English incident report for chat/dashboard/morning brief."""
    scan = scan or run_health_scan()
    if not scan["incidents"] and scan["overall"] == "healthy":
        return "System health: 🟢 all clear — workers running, no recent errors."
    dot = {"healthy": "🟢", "degraded": "🟡", "critical": "🔴"}.get(scan["overall"], "⚪")
    lines = [f"System health: {dot} {scan['overall'].upper()}", ""]
    for inc in scan["incidents"][:10]:
        where = inc.get("component") or "?"
        lines.append(f"- [{inc.get('type', 'issue')}] {where}: {inc.get('message', '')}")
    if len(scan["incidents"]) > 10:
        lines.append(f"...and {len(scan['incidents']) - 10} more.")
    return "\n".join(lines)


def check_system_health() -> str:
    """Chat tool: on-demand incident report."""
    return incident_report_text()


# ============================================================
# FIXER v1 — a strict, config-driven allowlist. Auto-act only on what's listed;
# everything else proposes via the EXISTING approval queue and waits.
# ============================================================

def _queue_fix_approval(problem_type: str, component: str, message: str, proposed_action: str) -> None:
    """Reuse the existing jarvis_pending_action queue rather than inventing a new
    approval path — the dashboard already renders and resolves these."""
    try:
        supabase.table("Agent Outputs").insert({
            "agent_name": "jarvis_pending_action",
            "output_text": json.dumps({
                "action": "monitor_fix",   # inert type: approving it just marks status;
                                          # there is nothing further to execute automatically —
                                          # Alex applies the fix himself once he approves it.
                "display": (f"[Monitor] {component}: {message}\nProposed fix: {proposed_action} "
                           f"(not on the auto-fix allowlist — needs your OK)."),
                "status": "pending",
            })
        }).execute()
    except Exception:
        pass


def _fix_restart_crashed_worker(component: str, **_) -> str:
    restart = _WORKER_RESTARTERS.get(component)
    if not restart:
        return f"no restart function registered for '{component}' — can't auto-restart"
    try:
        restart()
        return f"restarted '{component}'"
    except Exception as e:
        return f"restart of '{component}' failed: {e}"


def _fix_clear_temp_dir(component: str, **_) -> str:
    """Only ever touches KNOWN, hardcoded-safe scratch dirs — the allowlist config
    controls whether this ACTION CLASS runs automatically, but the function itself
    still restricts WHERE it can act (defense in depth, same pattern as
    task_manager._safe_path)."""
    safe_dirs = [
        os.path.join(os.path.expanduser("~"), ".jarvis_sandbox"),
        os.path.join(os.path.expanduser("~"), ".jarvis_expansion"),
    ]
    cleared = []
    for d in safe_dirs:
        if not os.path.isdir(d):
            continue
        try:
            free_gb = _disk_free_gb(d)
            if free_gb is not None and free_gb > 2.0:
                continue  # plenty of room — nothing to clear
            for name in os.listdir(d):
                p = os.path.join(d, name)
                if os.path.isdir(p) and name.startswith("task_"):
                    import shutil
                    shutil.rmtree(p, ignore_errors=True)
                    cleared.append(p)
        except Exception:
            continue
    return f"cleared {len(cleared)} stale scratch dir(s)" if cleared else "nothing needed clearing"


def _disk_free_gb(path: str):
    try:
        st = os.statvfs(path)
        return (st.f_bavail * st.f_frsize) / (1024 ** 3)
    except Exception:
        return None


def _fix_retry_transient_api(component: str, **_) -> str:
    """v1 is honest about its limits: we don't retain the original failed call's
    context to replay it. This confirms the API is reachable again and logs that —
    a real replay would need the caller to retry itself, which task_manager/
    expansion_pipeline's own tool loops already do on the next round."""
    if claude is None:
        return "no claude client available to probe"
    try:
        claude.messages.create(model="claude-sonnet-5", max_tokens=8,
                               messages=[{"role": "user", "content": "ping"}], timeout=15)
        return "API connectivity confirmed recovered"
    except Exception as e:
        return f"API still unreachable: {e}"


_FIXERS = {
    "restart_crashed_worker": _fix_restart_crashed_worker,
    "clear_temp_dir": _fix_clear_temp_dir,
    "retry_transient_api": _fix_retry_transient_api,
}


def attempt_fix(problem_type: str, component: str, message: str) -> str:
    """The fixer's single entry point. Allowlisted -> act + log what happened.
    Anything else (including an unrecognized problem_type) -> propose and wait."""
    allowlist = set(monitor_config().get("fixer_allowlist", []))
    if problem_type in allowlist and problem_type in _FIXERS:
        outcome = _FIXERS[problem_type](component=component, message=message)
        report_event("fixer", "info", f"auto-fix '{problem_type}' on {component}: {outcome}")
        return f"auto-fixed: {outcome}"
    proposed = {
        "restart_crashed_worker": f"restart the '{component}' worker thread",
        "clear_temp_dir": "clear stale scratch directories",
        "retry_transient_api": "confirm API connectivity",
    }.get(problem_type, "manual investigation")
    _queue_fix_approval(problem_type, component, message, proposed)
    report_event("fixer", "warning", f"proposed fix for {component} (not auto-applied): {proposed}")
    return f"proposed (needs approval): {proposed}"


# ============================================================
# 2b. COST — monthly rollup + three-tier budget, extends observability.py
# ============================================================

def _observability():
    import observability
    return observability.get_observability()


def spend_vs_budget() -> dict:
    cfg = budget_config()
    budget = float(cfg.get("monthly_budget_usd", 0)) or 0.0001  # avoid /0 on a misconfigured 0
    monthly = _observability().monthly_summary()
    spend = monthly.get("cost", 0.0)
    pct = spend / budget

    tiers = cfg.get("tiers", {})
    tier = "ok"
    if pct >= float(tiers.get("shutdown", 1.0)):
        tier = "shutdown"
    elif pct >= float(tiers.get("throttle", 0.8)):
        tier = "throttle"
    elif pct >= float(tiers.get("warn", 0.5)):
        tier = "warn"

    return {"spend": spend, "budget": budget, "pct": round(pct, 4), "tier": tier,
           "by_feature": monthly.get("by_feature", []), "since": monthly.get("since")}


def _load_last_tier() -> str:
    rows = (
        supabase.table("Agent Outputs").select("*")
        .eq("agent_name", "jarvis_budget_state").order("id", desc=True)
        .limit(1).execute().data or []
    )
    if not rows:
        return "ok"
    try:
        return json.loads(rows[0]["output_text"]).get("tier", "ok")
    except (json.JSONDecodeError, TypeError):
        return "ok"


def _save_tier_state(state: dict) -> None:
    try:
        supabase.table("Agent Outputs").insert(
            {"agent_name": "jarvis_budget_state", "output_text": json.dumps(state)}
        ).execute()
    except Exception:
        pass


def check_budget_tier() -> dict:
    """Compute the current tier; on a CHANGE from last time, log + notify with the
    numbers that triggered it. Safe to call every scan cycle — only transitions
    make noise."""
    state = spend_vs_budget()
    last = _LAST_TIER["tier"] if _LAST_TIER["tier"] is not None else _load_last_tier()
    if state["tier"] != last:
        state_row = {**state, "previous_tier": last, "ts": _now_iso()}
        _save_tier_state(state_row)
        _LAST_TIER["tier"] = state["tier"]
        msg = (f"**Budget tier changed: {last} → {state['tier']}** "
              f"(${state['spend']:.2f} / ${state['budget']:.2f}, {state['pct']*100:.0f}%)")
        report_event("budget", "warning" if state["tier"] in ("warn", "throttle") else
                    ("critical" if state["tier"] == "shutdown" else "info"), msg)
        if post_to_chat:
            try:
                post_to_chat("assistant", msg + _tier_explanation(state["tier"]))
            except Exception:
                pass
    else:
        _LAST_TIER["tier"] = state["tier"]
    return state


def _tier_explanation(tier: str) -> str:
    return {
        "warn": "\n\nJust a heads up — nothing is paused yet.",
        "throttle": "\n\nNon-essential automated agents (scouts, content agents) are now paused. "
                   "The chat brain keeps working normally.",
        "shutdown": "\n\nAll automated agents are stopped until you re-enable them. "
                   "The chat brain still works but will remind you of this every message.",
        "ok": "",
    }.get(tier, "")


def is_agent_allowed(feature: str) -> bool:
    """The gate scheduled/automated agents should check before running a cycle of
    work. 'ok'/'warn' → everyone runs. 'throttle'/'shutdown' → only essential
    features (the chat brain) run."""
    cfg = budget_config()
    essential = set(cfg.get("essential_features", ["chat"]))
    tier = _LAST_TIER["tier"] or _load_last_tier()
    if tier in ("ok", "warn"):
        return True
    return feature in essential


def budget_status_text() -> str:
    s = spend_vs_budget()
    return (f"Spend this month: ${s['spend']:.2f} / ${s['budget']:.2f} budget "
           f"({s['pct']*100:.0f}%) — tier: **{s['tier']}**")


# ============================================================
# MONITOR LOOP — periodic scan (daemon thread), same pattern as the other workers
# ============================================================

def _monitor_loop() -> None:
    while True:
        interval = monitor_config().get("scan_interval_seconds", 300)
        try:
            scan = run_health_scan()
            for inc in scan["incidents"]:
                # Only act on NEW events, so we don't re-fix/re-notify every cycle.
                eid = inc.get("event_id")
                if eid is not None and eid <= _LAST_EVENT_ID_SEEN["id"]:
                    continue
                if inc["type"] == "worker_down":
                    attempt_fix("restart_crashed_worker", inc["component"], inc["message"])
                if eid is not None:
                    _LAST_EVENT_ID_SEEN["id"] = max(_LAST_EVENT_ID_SEEN["id"], eid)
            check_budget_tier()
        except Exception as e:
            report_event("monitor", "error", f"scan cycle failed: {e}")
        time.sleep(max(30, int(interval)))


def start_monitor(post_to_chat_fn=None) -> None:
    global post_to_chat
    if post_to_chat_fn is not None:
        post_to_chat = post_to_chat_fn
    t = threading.Thread(target=_monitor_loop, daemon=True, name="jarvis-monitor")
    t.start()


# ============================================================
# DASHBOARD + chat tools
# ============================================================

def get_monitor_dashboard_data() -> dict:
    scan = run_health_scan()
    budget = spend_vs_budget()
    events = get_recent_events(limit=10, min_level="warning")
    return {
        "overall": scan["overall"],
        "worker_status": scan["workers"],
        "incident_count": len(scan["incidents"]),
        "budget": budget,
        "recent_events": events,
    }


TOOL_SCHEMAS = [
    {"name": "check_system_health",
     "description": "Plain-English incident report: worker status, recent errors, overall health.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "check_budget",
     "description": "Current month's API spend vs. budget, and which tier (ok/warn/throttle/shutdown) that puts you in.",
     "input_schema": {"type": "object", "properties": {}}},
]
