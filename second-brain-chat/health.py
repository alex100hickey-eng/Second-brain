"""
health.py — one-glance system health for the Second Brain.

Checks the things that quietly break: databases you can't read, a stale semantic index,
missing binaries (whisper/ffmpeg), no disk headroom for backups, and how long since the
test suite last passed. Surfaces as a chat command ("system health" / "health check") and
a dashboard indicator. Read-only — it inspects, never fixes.

Each check returns {name, ok, status, detail}. `ok` is True/False/None (None = warning /
unknown). The overall status is the worst of the parts.
"""

import os
import shutil
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

_TZ = ZoneInfo("America/New_York")
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)

# Where run_tests.py records its last green run (written on a 0-failure run).
LAST_TEST_PASS_FILE = os.path.join(_ROOT, ".last_test_pass")
BACKUP_DIR = os.path.expanduser("~/second-brain-backups")

_DBS = {
    "conversation memory": os.path.join(_HERE, "conversation_memory.db"),
    "task tracker": os.path.join(_HERE, "task_tracker.db"),
    "semantic index": os.path.join(_HERE, "semantic_index.db"),
    "observability": os.path.join(_HERE, "observability.db"),
}


def _now():
    return datetime.now(_TZ)


def _check_db(name, path) -> dict:
    if not os.path.exists(path):
        # A DB that hasn't been created yet is fine (lazy) — report as a warning, not a failure.
        return {"name": f"DB: {name}", "ok": None, "status": "not created yet",
                "detail": "will be created on first use"}
    try:
        conn = sqlite3.connect(path)
        conn.execute("SELECT 1").fetchone()
        conn.close()
        size = os.path.getsize(path)
        return {"name": f"DB: {name}", "ok": True, "status": "readable",
                "detail": f"{size/1024:.0f} KB"}
    except Exception as e:
        return {"name": f"DB: {name}", "ok": False, "status": "UNREADABLE", "detail": str(e)}


def _check_binary(name, hint="") -> dict:
    path = shutil.which(name)
    if path:
        return {"name": f"binary: {name}", "ok": True, "status": "present", "detail": path}
    return {"name": f"binary: {name}", "ok": False, "status": "MISSING", "detail": hint}


def _check_index() -> dict:
    try:
        import semantic_index
        stats = semantic_index.get_index().stats()
        if stats["total"] == 0:
            return {"name": "semantic index", "ok": None, "status": "empty",
                    "detail": "run /reindex-all (or ask Jarvis to search) to build it"}
        mode = "semantic" if stats["semantic"] else "keyword-only (model unavailable)"
        return {"name": "semantic index", "ok": True, "status": mode,
                "detail": f"{stats['total']} docs, {stats['vectors']} vectors"}
    except Exception as e:
        return {"name": "semantic index", "ok": False, "status": "error", "detail": str(e)}


def _check_disk_for_backups() -> dict:
    target = BACKUP_DIR if os.path.isdir(BACKUP_DIR) else os.path.expanduser("~")
    try:
        usage = shutil.disk_usage(target)
        free_gb = usage.free / (1024**3)
        ok = free_gb >= 2.0
        warn = free_gb < 5.0
        status = "ok" if not warn else ("LOW" if ok else "CRITICALLY LOW")
        return {"name": "disk (backups)", "ok": True if not warn else (None if ok else False),
                "status": status, "detail": f"{free_gb:.1f} GB free at {target}"}
    except Exception as e:
        return {"name": "disk (backups)", "ok": None, "status": "unknown", "detail": str(e)}


def _check_backups_present() -> dict:
    if not os.path.isdir(BACKUP_DIR):
        return {"name": "backups", "ok": None, "status": "none yet",
                "detail": f"no {BACKUP_DIR} — run a backup to create one"}
    zips = [f for f in os.listdir(BACKUP_DIR) if f.endswith(".zip")]
    if not zips:
        return {"name": "backups", "ok": None, "status": "none yet", "detail": "folder exists but empty"}
    newest = max(zips, key=lambda f: os.path.getmtime(os.path.join(BACKUP_DIR, f)))
    age_days = (_now().timestamp() - os.path.getmtime(os.path.join(BACKUP_DIR, newest))) / 86400
    ok = age_days <= 14
    return {"name": "backups", "ok": True if ok else None, "status": f"{len(zips)} on disk",
            "detail": f"newest {age_days:.1f} days old ({newest})"}


def _check_last_test_pass() -> dict:
    if not os.path.exists(LAST_TEST_PASS_FILE):
        return {"name": "test suite", "ok": None, "status": "no record",
                "detail": "run_tests.py hasn't recorded a green run yet"}
    try:
        with open(LAST_TEST_PASS_FILE, encoding="utf-8") as f:
            content = f.read().strip()
        # First line is an ISO timestamp; rest is optional detail.
        iso = content.splitlines()[0]
        dt = datetime.fromisoformat(iso)
        age_days = (_now() - dt).total_seconds() / 86400
        ok = age_days <= 7
        return {"name": "test suite", "ok": True if ok else None,
                "status": f"last passed {age_days:.1f} days ago",
                "detail": dt.strftime("%Y-%m-%d %H:%M") + (f"  ({content.splitlines()[1]})" if len(content.splitlines()) > 1 else "")}
    except Exception as e:
        return {"name": "test suite", "ok": None, "status": "unreadable record", "detail": str(e)}


def run_health_check() -> dict:
    checks = []
    checks.append({"name": "app", "ok": True, "status": "running", "detail": "you're talking to it"})
    for name, path in _DBS.items():
        checks.append(_check_db(name, path))
    checks.append(_check_index())
    checks.append(_check_binary("whisper-cli", "brew install whisper-cpp"))
    checks.append(_check_binary("ffmpeg", "brew install ffmpeg"))
    checks.append(_check_disk_for_backups())
    checks.append(_check_backups_present())
    checks.append(_check_last_test_pass())

    has_fail = any(c["ok"] is False for c in checks)
    has_warn = any(c["ok"] is None for c in checks)
    overall = "critical" if has_fail else ("degraded" if has_warn else "healthy")
    return {"overall": overall, "checks": checks, "generated_at": _now().strftime("%-I:%M:%S %p")}


def health_text() -> str:
    """Chat-facing health rundown."""
    result = run_health_check()
    icon = {"healthy": "🟢", "degraded": "🟡", "critical": "🔴"}[result["overall"]]
    lines = [f"{icon} System health: **{result['overall'].upper()}**", ""]
    for c in result["checks"]:
        mark = {True: "✓", False: "✗", None: "•"}[c["ok"]]
        detail = f" — {c['detail']}" if c.get("detail") else ""
        lines.append(f"  {mark} {c['name']}: {c['status']}{detail}")
    return "\n".join(lines)


def record_test_pass(detail: str = "") -> None:
    """Called by run_tests.py after a fully green run."""
    try:
        with open(LAST_TEST_PASS_FILE, "w", encoding="utf-8") as f:
            f.write(_now().isoformat() + ("\n" + detail if detail else ""))
    except Exception as e:
        print(f"health: couldn't record test pass: {e}")
