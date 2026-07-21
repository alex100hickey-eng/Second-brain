"""
observability.py — watch the watcher.

The Second Brain now reads Alex's screen, the web, his vault, and remembers every
conversation. This module makes what it *does* auditable and its costs visible:

  1. TOOL AUDIT LOG — every tool call is recorded locally (timestamp, tool, the
     triggering context — user message / agent / drafter, an input summary, and
     success/failure). Queryable on the dashboard and via chat ("what did you do today?").
  2. COST TRACKING — every Claude API call's token usage is recorded and priced from a
     configurable table (../pricing.json, clearly marked for Alex to verify). Rolled up
     today / this week / by feature.
  3. Both live in a local, gitignored SQLite DB (observability.db). Nothing leaves the box.

Attribution uses two independent thread-local dimensions, set by the app:
  * TRIGGER — who kicked off this turn: 'user' | 'agent' | 'drafter' | 'managed' | 'system'
  * FEATURE — what the current work is: a tool name, 'chat', 'council', 'summary', etc.

Everything here is best-effort and fail-soft: an observability hiccup must never break
the thing being observed.
"""

import os
import re
import json
import time
import sqlite3
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

_TZ = ZoneInfo("America/New_York")
DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "observability.db")
PRICING_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "pricing.json")


def _now():
    return datetime.now(_TZ)


def _now_iso():
    return _now().isoformat()


# ---- thread-local attribution context ---------------------------------------
_ctx = threading.local()


def _get(name, default):
    return getattr(_ctx, name, default)


def set_trigger(name: str):
    _ctx.trigger = name


def current_trigger() -> str:
    return _get("trigger", "user")


def current_feature() -> str:
    stack = _get("feature_stack", None)
    return stack[-1] if stack else "chat"


class feature:
    """Context manager: `with observability.feature('create_website'):` attributes any
    API calls / tool work inside to that feature. Nestable."""
    def __init__(self, name):
        self.name = name

    def __enter__(self):
        stack = _get("feature_stack", None)
        if stack is None:
            stack = []
            _ctx.feature_stack = stack
        stack.append(self.name)
        return self

    def __exit__(self, *a):
        stack = _get("feature_stack", None)
        if stack:
            stack.pop()
        return False


# ---- pricing ----------------------------------------------------------------
_PRICING_CACHE = {"data": None, "mtime": 0}


def _load_pricing() -> dict:
    try:
        mtime = os.path.getmtime(PRICING_PATH)
        if _PRICING_CACHE["data"] is None or mtime != _PRICING_CACHE["mtime"]:
            with open(PRICING_PATH, encoding="utf-8") as f:
                _PRICING_CACHE["data"] = json.load(f)
            _PRICING_CACHE["mtime"] = mtime
    except Exception:
        # Sensible fallback so cost tracking still works if the file is missing.
        _PRICING_CACHE["data"] = {
            "models": {"claude-sonnet-5": {"input": 3.0, "output": 15.0}},
            "cache_multipliers": {"cache_read": 0.1, "cache_write": 1.25},
            "default_model_if_unknown": "claude-sonnet-5",
        }
    return _PRICING_CACHE["data"]


def estimate_cost(model: str, input_tokens: int, output_tokens: int,
                  cache_read: int = 0, cache_write: int = 0) -> float:
    p = _load_pricing()
    models = p.get("models", {})
    entry = models.get(model) or models.get(p.get("default_model_if_unknown", ""), {})
    in_rate = float(entry.get("input", 0.0))
    out_rate = float(entry.get("output", 0.0))
    mult = p.get("cache_multipliers", {})
    cost = (input_tokens * in_rate + output_tokens * out_rate) / 1_000_000.0
    cost += (cache_read * in_rate * float(mult.get("cache_read", 0.1))) / 1_000_000.0
    cost += (cache_write * in_rate * float(mult.get("cache_write", 1.25))) / 1_000_000.0
    return round(cost, 6)


# ---- store ------------------------------------------------------------------
class Observability:
    def __init__(self, db_path: str = DEFAULT_DB):
        self.db_path = db_path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self):
        with self._lock:
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS tool_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    tool TEXT NOT NULL,
                    trigger TEXT DEFAULT 'user',
                    input_summary TEXT DEFAULT '',
                    success INTEGER DEFAULT 1,
                    detail TEXT DEFAULT '',
                    ms INTEGER DEFAULT 0
                )"""
            )
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS api_usage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    feature TEXT DEFAULT 'chat',
                    trigger TEXT DEFAULT 'user',
                    model TEXT DEFAULT '',
                    input_tokens INTEGER DEFAULT 0,
                    output_tokens INTEGER DEFAULT 0,
                    cache_read INTEGER DEFAULT 0,
                    cache_write INTEGER DEFAULT 0,
                    cost REAL DEFAULT 0
                )"""
            )
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_ts ON tool_audit(ts)")
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_ts ON api_usage(ts)")
            self._conn.commit()

    # ---- writes ----
    def log_tool(self, tool: str, trigger: str, input_summary: str, success: bool,
                 detail: str = "", ms: int = 0):
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT INTO tool_audit (ts, tool, trigger, input_summary, success, detail, ms) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (_now_iso(), tool, trigger, input_summary[:400], 1 if success else 0, detail[:400], int(ms)),
                )
                self._conn.commit()
        except Exception as e:
            print(f"observability: log_tool failed: {e}")

    def log_usage(self, feature: str, trigger: str, model: str, input_tokens: int,
                  output_tokens: int, cache_read: int = 0, cache_write: int = 0):
        try:
            cost = estimate_cost(model, input_tokens, output_tokens, cache_read, cache_write)
            with self._lock:
                self._conn.execute(
                    "INSERT INTO api_usage (ts, feature, trigger, model, input_tokens, output_tokens, cache_read, cache_write, cost) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (_now_iso(), feature, trigger, model, int(input_tokens), int(output_tokens),
                     int(cache_read), int(cache_write), cost),
                )
                self._conn.commit()
            return cost
        except Exception as e:
            print(f"observability: log_usage failed: {e}")
            return 0.0

    # ---- reads: audit ----
    def recent_tools(self, limit: int = 25) -> list:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM tool_audit ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def tools_since(self, since_iso: str) -> list:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM tool_audit WHERE ts >= ? ORDER BY id DESC", (since_iso,)
            ).fetchall()
        return [dict(r) for r in rows]

    def _day_start_iso(self, days_ago: int = 0) -> str:
        d = (_now() - timedelta(days=days_ago)).replace(hour=0, minute=0, second=0, microsecond=0)
        return d.isoformat()

    def tool_activity_summary(self, period: str = "today") -> dict:
        since = self._day_start_iso(0) if period == "today" else self._day_start_iso(7)
        rows = self.tools_since(since)
        by_tool, fails = {}, 0
        for r in rows:
            by_tool[r["tool"]] = by_tool.get(r["tool"], 0) + 1
            if not r["success"]:
                fails += 1
        return {"period": period, "total": len(rows), "failures": fails,
                "by_tool": dict(sorted(by_tool.items(), key=lambda kv: kv[1], reverse=True))}

    # ---- reads: cost ----
    def cost_summary(self) -> dict:
        today_since = self._day_start_iso(0)
        week_since = self._day_start_iso(7)

        def agg(where_since):
            with self._lock:
                row = self._conn.execute(
                    "SELECT COUNT(*) n, COALESCE(SUM(cost),0) c, COALESCE(SUM(input_tokens),0) it, "
                    "COALESCE(SUM(output_tokens),0) ot FROM api_usage WHERE ts >= ?", (where_since,)
                ).fetchone()
            return {"requests": row["n"], "cost": round(row["c"], 4),
                    "input_tokens": row["it"], "output_tokens": row["ot"]}

        with self._lock:
            by_feat_rows = self._conn.execute(
                "SELECT feature, COUNT(*) n, COALESCE(SUM(cost),0) c FROM api_usage "
                "WHERE ts >= ? GROUP BY feature ORDER BY c DESC", (week_since,)
            ).fetchall()
        by_feature = [{"feature": r["feature"], "requests": r["n"], "cost": round(r["c"], 4)}
                      for r in by_feat_rows]
        return {"today": agg(today_since), "week": agg(week_since), "by_feature": by_feature,
                "pricing_note": "Estimated from ../pricing.json — verify those rates."}

    # ---- reads: monthly rollup (added for the budget/tier monitor) ----
    def _month_start_iso(self) -> str:
        n = _now()
        return n.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()

    def monthly_summary(self) -> dict:
        """This calendar month's spend, total + by feature/agent. Used by the budget
        tier engine — never call this from a hot path, it's a small aggregate query."""
        since = self._month_start_iso()
        with self._lock:
            total = self._conn.execute(
                "SELECT COUNT(*) n, COALESCE(SUM(cost),0) c FROM api_usage WHERE ts >= ?",
                (since,)
            ).fetchone()
            by_feat_rows = self._conn.execute(
                "SELECT feature, COUNT(*) n, COALESCE(SUM(cost),0) c FROM api_usage "
                "WHERE ts >= ? GROUP BY feature ORDER BY c DESC", (since,)
            ).fetchall()
        return {
            "since": since,
            "requests": total["n"],
            "cost": round(total["c"], 4),
            "by_feature": [{"feature": r["feature"], "requests": r["n"], "cost": round(r["c"], 4)}
                           for r in by_feat_rows],
        }


# ---- singleton --------------------------------------------------------------
_OBS = None
_OBS_LOCK = threading.Lock()


def get_observability(db_path: str = DEFAULT_DB) -> Observability:
    global _OBS
    with _OBS_LOCK:
        if _OBS is None:
            _OBS = Observability(db_path)
    return _OBS


# ---- input summarizer for the audit log -------------------------------------
def summarize_input(tool_input: dict) -> str:
    """A short, safe one-line summary of a tool's input for the audit log."""
    if not isinstance(tool_input, dict):
        return str(tool_input)[:120]
    parts = []
    for k, v in tool_input.items():
        s = re.sub(r"\s+", " ", str(v)).strip()
        if len(s) > 60:
            s = s[:60] + "…"
        parts.append(f"{k}={s}")
    return ", ".join(parts)[:300]


# ---- Claude client wrapper for automatic cost tracking ----------------------
def wrap_client(real_client):
    """Wrap an Anthropic client so EVERY .messages.create / .messages.stream call records
    token usage against the current feature/trigger. Downstream code (agents that receive
    the client) is tracked automatically. Fail-soft: recording never breaks a call."""
    return _ClientProxy(real_client)


class _ClientProxy:
    def __init__(self, real):
        self._real = real
        self.messages = _MessagesProxy(real.messages)

    def __getattr__(self, name):
        return getattr(self._real, name)


class _MessagesProxy:
    def __init__(self, real):
        self._real = real

    def create(self, **kw):
        resp = self._real.create(**kw)
        _record_usage(kw.get("model", ""), getattr(resp, "usage", None))
        return resp

    def stream(self, **kw):
        return _StreamProxy(self._real.stream(**kw), kw.get("model", ""))

    def __getattr__(self, name):
        return getattr(self._real, name)


class _StreamProxy:
    """Proxies the streaming context manager so get_final_message() records usage."""
    def __init__(self, real_cm, model):
        self._real_cm = real_cm
        self._model = model
        self._stream = None

    def __enter__(self):
        self._stream = self._real_cm.__enter__()
        return self

    def __exit__(self, *a):
        return self._real_cm.__exit__(*a)

    @property
    def text_stream(self):
        return self._stream.text_stream

    def get_final_message(self):
        msg = self._stream.get_final_message()
        _record_usage(self._model, getattr(msg, "usage", None))
        return msg

    def __getattr__(self, name):
        return getattr(self._stream, name)


def _record_usage(model, usage):
    if usage is None:
        return
    try:
        it = int(getattr(usage, "input_tokens", 0) or 0)
        ot = int(getattr(usage, "output_tokens", 0) or 0)
        cr = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
        cw = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
        get_observability().log_usage(current_feature(), current_trigger(), model, it, ot, cr, cw)
    except Exception as e:
        print(f"observability: usage record failed: {e}")


# ---- chat-facing summaries --------------------------------------------------
def tool_activity_text(period: str = "today") -> str:
    """Answer 'what did you do today?' from the audit log."""
    obs = get_observability()
    summ = obs.tool_activity_summary(period)
    since = obs._day_start_iso(0 if period == "today" else 7)
    rows = obs.tools_since(since)
    label = "today" if period == "today" else "this week"
    if not rows:
        return f"I haven't run any tools {label} (nothing in the audit log)."
    lines = [f"What I did {label}: {summ['total']} tool call(s)"
             + (f", {summ['failures']} failed" if summ["failures"] else "") + "."]
    counts = summ["by_tool"]
    lines.append("By tool: " + ", ".join(f"{t}×{n}" for t, n in list(counts.items())[:12]) + ".")
    lines.append("\nMost recent:")
    for r in rows[:10]:
        when = _humanize(r["ts"])
        ok = "✓" if r["success"] else "✗"
        who = r["trigger"]
        summ_in = f" — {r['input_summary']}" if r["input_summary"] else ""
        lines.append(f"  {ok} [{when}] {r['tool']} (via {who}){summ_in}")
    return "\n".join(lines)


def _humanize(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso)
    except (ValueError, TypeError):
        return ""
    secs = (_now() - dt).total_seconds()
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{int(secs//60)}m ago"
    if secs < 86400:
        return f"{int(secs//3600)}h ago"
    return dt.strftime("%b %-d %-I:%M %p")
