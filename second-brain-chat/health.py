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


# ============================================================
# STARTUP SELF-CHECK — run once at app boot. Verifies every dependency the system
# needs BEFORE a request hits a missing one mid-conversation. Reuses the checks above
# (DBs / index / binaries / disk) and adds env-var, Supabase-reachability, and embedding-
# model checks. A missing REQUIRED dependency is a loud critical; a missing OPTIONAL one
# degrades gracefully with a visible notice. The structured report is cached so the
# dashboard/health panel can show it.
# ============================================================

# Vars the app genuinely cannot run without (chat brain + datastore).
REQUIRED_ENV = ["CLAUDE_API_KEY", "SUPABASE_URL", "SUPABASE_KEY"]
# Vars that gate specific features or hardening — missing = degraded, not dead.
OPTIONAL_ENV = {
    "COMPOSIO_API_KEY": "Google Calendar + Gmail tools disabled",
    "ACCESS_CODE": "chat gate OPEN — anyone reaching the port can use the brain (set one!)",
    "FLASK_SECRET_KEY": "sessions won't survive a restart (a random key is used)",
    "GITHUB_TOKEN": "expansion scout uses unauthenticated GitHub (lower rate limit)",
    "TAVILY_API_KEY": "web search falls back to keyless DuckDuckGo",
    "SERPER_API_KEY": "web search falls back to keyless DuckDuckGo",
    "BRAVE_API_KEY": "web search falls back to keyless DuckDuckGo",
}

_LAST_STARTUP_REPORT = None


def _check_env() -> list:
    checks = []
    for var in REQUIRED_ENV:
        present = bool(os.environ.get(var))
        checks.append({"name": f"env: {var}", "ok": present,
                       "status": "set" if present else "MISSING (required)",
                       "detail": "" if present else "the app cannot function without this"})
    for var, consequence in OPTIONAL_ENV.items():
        present = bool(os.environ.get(var))
        # A missing ACCESS_CODE is a security concern — surface it as a warning, others as info.
        ok = True if present else None
        checks.append({"name": f"env: {var}", "ok": ok,
                       "status": "set" if present else "not set",
                       "detail": "" if present else consequence})
    return checks


def _check_supabase(supabase_client) -> dict:
    if supabase_client is None:
        return {"name": "Supabase reachability", "ok": None, "status": "not checked",
                "detail": "no client passed to the startup check"}
    try:
        supabase_client.table("Agent Outputs").select("id").limit(1).execute()
        return {"name": "Supabase reachability", "ok": True, "status": "reachable",
                "detail": "query round-tripped"}
    except Exception as e:
        return {"name": "Supabase reachability", "ok": False, "status": "UNREACHABLE",
                "detail": str(e)[:200]}


def _check_embedding_model() -> dict:
    try:
        import embeddings
        if embeddings.available():
            return {"name": "embedding model", "ok": True, "status": "loaded",
                    "detail": f"model2vec {embeddings.MODEL_ID}"}
        return {"name": "embedding model", "ok": None, "status": "unavailable",
                "detail": "semantic search falls back to keyword ranking (still works)"}
    except Exception as e:
        return {"name": "embedding model", "ok": None, "status": "unavailable",
                "detail": str(e)[:200]}


def run_startup_check(supabase_client=None) -> dict:
    """Full boot-time dependency check. Returns a structured report and caches it.
    overall: healthy | degraded | critical. missing_required lists dead-stop problems."""
    global _LAST_STARTUP_REPORT
    checks = []
    checks += _check_env()
    for name, path in _DBS.items():
        checks.append(_check_db(name, path))
    checks.append(_check_index())
    checks.append(_check_embedding_model())
    checks.append(_check_binary("whisper-cli", "brew install whisper-cpp"))
    checks.append(_check_binary("ffmpeg", "brew install ffmpeg"))
    checks.append(_check_disk_for_backups())
    checks.append(_check_supabase(supabase_client))

    missing_required = [c["name"] for c in checks if c["ok"] is False]
    notices = [f"{c['name']}: {c['detail']}" for c in checks
               if c["ok"] is None and c.get("detail")]
    has_fail = bool(missing_required)
    has_warn = any(c["ok"] is None for c in checks)
    overall = "critical" if has_fail else ("degraded" if has_warn else "healthy")
    report = {"overall": overall, "checks": checks, "missing_required": missing_required,
              "notices": notices, "generated_at": _now().isoformat()}
    _LAST_STARTUP_REPORT = report
    return report


def get_last_startup_report() -> dict:
    return _LAST_STARTUP_REPORT or {}


def startup_report_text(report: dict = None) -> str:
    """Readable boot summary for the log / dashboard."""
    report = report or _LAST_STARTUP_REPORT or run_startup_check()
    icon = {"healthy": "🟢", "degraded": "🟡", "critical": "🔴"}[report["overall"]]
    lines = [f"{icon} Startup self-check: {report['overall'].upper()}"]
    if report["missing_required"]:
        lines.append("  ✗ MISSING REQUIRED: " + ", ".join(report["missing_required"])
                     + "  — expect failures until fixed.")
    for c in report["checks"]:
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
