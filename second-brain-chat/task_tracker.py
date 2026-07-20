"""
task_tracker.py — lightweight task bookkeeping for the Second Brain.

This is the FOUNDATION for tracking what Jarvis is working on — a supervised
to-do/idea board, NOT an autonomous executor. Nothing here runs or acts on a
task; it only records structure, status, and history so a future supervised
build can pick work up from an honest ledger.

  * Storage: local SQLite (no Supabase tables, no network).
  * Model:  task = {id, title, description, status, created_at, updated_at, history[]}
            status flows: idea → evaluating → approved → in_progress → done | dropped
            history is an append-only log of status changes and notes.
  * Safe for the app's threads: a single connection guarded by a module lock.

Distinct from task_manager.py (which plans + autonomously executes "managed
tasks" with a guardrail council). This module never executes anything.
"""

import os
import json
import time
import sqlite3
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

_TZ = ZoneInfo("America/New_York")

# Ordered status pipeline. "done" and "dropped" are terminal.
STATUSES = ["idea", "evaluating", "approved", "in_progress", "done", "dropped"]
VALID = set(STATUSES)

DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "task_tracker.db")


def _now_iso() -> str:
    return datetime.now(_TZ).isoformat()


def _clamp(v, lo: int = 0, hi: int = 5) -> int:
    """Urgency/importance live on a 0-5 scale (0 = unset)."""
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return 0


def _humanize(iso: str) -> str:
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return ""
    delta = datetime.now(_TZ) - dt
    secs = delta.total_seconds()
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{int(secs // 60)}m ago"
    if secs < 86400:
        return f"{int(secs // 3600)}h ago"
    if secs < 7 * 86400:
        return f"{int(secs // 86400)}d ago"
    return dt.strftime("%b %-d")


class TaskTracker:
    def __init__(self, db_path: str = DEFAULT_DB):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self):
        with self._lock:
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    description TEXT DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'idea',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    history TEXT NOT NULL DEFAULT '[]'
                )"""
            )
            # --- Round-4 migration: urgency/importance on tasks + goal_id link ---
            cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(tasks)")}
            if "urgency" not in cols:
                self._conn.execute("ALTER TABLE tasks ADD COLUMN urgency INTEGER DEFAULT 0")
            if "importance" not in cols:
                self._conn.execute("ALTER TABLE tasks ADD COLUMN importance INTEGER DEFAULT 0")
            if "goal_id" not in cols:
                self._conn.execute("ALTER TABLE tasks ADD COLUMN goal_id INTEGER")
            # --- Goals ---
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS goals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    description TEXT DEFAULT '',
                    target_date TEXT DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    history TEXT NOT NULL DEFAULT '[]'
                )"""
            )
            self._conn.commit()

    # ---- internal helpers ----
    def _row_to_dict(self, row) -> dict:
        d = dict(row)
        try:
            d["history"] = json.loads(d.get("history") or "[]")
        except (json.JSONDecodeError, TypeError):
            d["history"] = []
        d["urgency"] = d.get("urgency") or 0
        d["importance"] = d.get("importance") or 0
        # Priority score: importance weighted a touch above urgency; drives default ordering.
        d["priority_score"] = d["importance"] * 2 + d["urgency"]
        d["updated_human"] = _humanize(d.get("updated_at", ""))
        d["created_human"] = _humanize(d.get("created_at", ""))
        return d

    def _fetch_raw(self, task_id):
        cur = self._conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
        return cur.fetchone()

    # ---- public API ----
    def create(self, title: str, description: str = "", urgency: int = 0,
               importance: int = 0) -> dict:
        title = (title or "").strip()
        if not title:
            return {"error": "A task needs a title."}
        urgency = _clamp(urgency)
        importance = _clamp(importance)
        now = _now_iso()
        history = [{"type": "created", "at": now, "note": "task created", "to": "idea"}]
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO tasks (title, description, status, created_at, updated_at, history, "
                "urgency, importance) VALUES (?, ?, 'idea', ?, ?, ?, ?, ?)",
                (title, (description or "").strip(), now, now, json.dumps(history),
                 urgency, importance),
            )
            self._conn.commit()
            row = self._fetch_raw(cur.lastrowid)
        return self._row_to_dict(row)

    def set_priority(self, task_id, urgency: int = None, importance: int = None) -> dict | None:
        with self._lock:
            row = self._fetch_raw(task_id)
            if not row:
                return None
            u = _clamp(urgency) if urgency is not None else row["urgency"]
            i = _clamp(importance) if importance is not None else row["importance"]
            now = _now_iso()
            d = self._row_to_dict(row)
            d["history"].append({"type": "priority", "urgency": u, "importance": i, "at": now})
            self._conn.execute(
                "UPDATE tasks SET urgency = ?, importance = ?, updated_at = ?, history = ? WHERE id = ?",
                (u, i, now, json.dumps(d["history"]), task_id),
            )
            self._conn.commit()
            row = self._fetch_raw(task_id)
        return self._row_to_dict(row)

    def get(self, task_id) -> dict | None:
        with self._lock:
            row = self._fetch_raw(task_id)
        return self._row_to_dict(row) if row else None

    def list(self, status: str = None, limit: int = 100) -> list:
        with self._lock:
            if status:
                cur = self._conn.execute(
                    "SELECT * FROM tasks WHERE status = ? ORDER BY updated_at DESC LIMIT ?",
                    (status, limit),
                )
            else:
                cur = self._conn.execute(
                    "SELECT * FROM tasks ORDER BY updated_at DESC LIMIT ?", (limit,)
                )
            rows = cur.fetchall()
        return [self._row_to_dict(r) for r in rows]

    def update_status(self, task_id, new_status: str, note: str = "") -> dict | None:
        new_status = (new_status or "").strip().lower().replace(" ", "_")
        if new_status not in VALID:
            return {"error": f"Unknown status '{new_status}'. Valid: {', '.join(STATUSES)}."}
        with self._lock:
            row = self._fetch_raw(task_id)
            if not row:
                return None
            d = self._row_to_dict(row)
            old = d["status"]
            now = _now_iso()
            d["history"].append({
                "type": "status", "from": old, "to": new_status,
                "note": (note or "").strip(), "at": now,
            })
            self._conn.execute(
                "UPDATE tasks SET status = ?, updated_at = ?, history = ? WHERE id = ?",
                (new_status, now, json.dumps(d["history"]), task_id),
            )
            self._conn.commit()
            row = self._fetch_raw(task_id)
        return self._row_to_dict(row)

    def add_note(self, task_id, note: str) -> dict | None:
        note = (note or "").strip()
        if not note:
            return {"error": "Empty note."}
        with self._lock:
            row = self._fetch_raw(task_id)
            if not row:
                return None
            d = self._row_to_dict(row)
            now = _now_iso()
            d["history"].append({"type": "note", "note": note, "at": now})
            self._conn.execute(
                "UPDATE tasks SET updated_at = ?, history = ? WHERE id = ?",
                (now, json.dumps(d["history"]), task_id),
            )
            self._conn.commit()
            row = self._fetch_raw(task_id)
        return self._row_to_dict(row)

    def recent_for_dashboard(self, limit: int = 8) -> list:
        """Compact rows for the home dashboard's tasks panel. Active tasks first,
        ordered by priority (importance+urgency) then recency; terminal tasks sink."""
        tasks = self.list(limit=200)
        active = [t for t in tasks if t["status"] not in ("done", "dropped")]
        terminal = [t for t in tasks if t["status"] in ("done", "dropped")]
        active.sort(key=lambda t: (t["priority_score"], t.get("updated_at", "")), reverse=True)
        ordered = (active + terminal)[:limit]
        return [{
            "id": t["id"], "title": t["title"], "status": t["status"],
            "updated_human": t["updated_human"],
            "urgency": t["urgency"], "importance": t["importance"],
            "priority_score": t["priority_score"],
        } for t in ordered]

    def top_by_priority(self, limit: int = 5) -> list:
        """Highest-priority ACTIVE tasks (for the briefing). Excludes terminal states."""
        active = [t for t in self.list(limit=200) if t["status"] not in ("done", "dropped")]
        active.sort(key=lambda t: (t["priority_score"], t.get("updated_at", "")), reverse=True)
        return active[:limit]

    # ================= GOALS =================
    def create_goal(self, title: str, description: str = "", target_date: str = "") -> dict:
        title = (title or "").strip()
        if not title:
            return {"error": "A goal needs a title."}
        now = _now_iso()
        history = [{"type": "created", "at": now, "note": "goal created"}]
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO goals (title, description, target_date, status, created_at, updated_at, history) "
                "VALUES (?, ?, ?, 'active', ?, ?, ?)",
                (title, (description or "").strip(), (target_date or "").strip(), now, now, json.dumps(history)),
            )
            self._conn.commit()
            gid = cur.lastrowid
        return self.get_goal(gid)

    def get_goal(self, goal_id) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM goals WHERE id = ?", (goal_id,)).fetchone()
            if not row:
                return None
            linked = self._conn.execute(
                "SELECT id, title, status FROM tasks WHERE goal_id = ? ORDER BY id", (goal_id,)
            ).fetchall()
        g = dict(row)
        try:
            g["history"] = json.loads(g.get("history") or "[]")
        except (json.JSONDecodeError, TypeError):
            g["history"] = []
        tasks = [dict(t) for t in linked]
        total = len(tasks)
        done = sum(1 for t in tasks if t["status"] == "done")
        g["total_tasks"] = total
        g["done_tasks"] = done
        g["progress_pct"] = int(round(100 * done / total)) if total else 0
        g["linked_tasks"] = tasks
        g["updated_human"] = _humanize(g.get("updated_at", ""))
        return g

    def list_goals(self, status: str = None, limit: int = 100) -> list:
        with self._lock:
            if status:
                rows = self._conn.execute(
                    "SELECT id FROM goals WHERE status = ? ORDER BY updated_at DESC LIMIT ?",
                    (status, limit)).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT id FROM goals ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
        return [self.get_goal(r["id"]) for r in rows]

    def update_goal(self, goal_id, status: str = None, note: str = "",
                    title: str = None, target_date: str = None) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM goals WHERE id = ?", (goal_id,)).fetchone()
            if not row:
                return None
            g = dict(row)
            try:
                hist = json.loads(g.get("history") or "[]")
            except (json.JSONDecodeError, TypeError):
                hist = []
            now = _now_iso()
            new_status = g["status"]
            if status:
                new_status = status.strip().lower()
                if new_status not in ("active", "achieved", "dropped"):
                    return {"error": "Goal status must be active, achieved, or dropped."}
                hist.append({"type": "status", "to": new_status, "at": now})
            new_title = title.strip() if title else g["title"]
            new_target = target_date.strip() if target_date is not None else g["target_date"]
            if note:
                hist.append({"type": "note", "note": note.strip(), "at": now})
            self._conn.execute(
                "UPDATE goals SET status = ?, title = ?, target_date = ?, updated_at = ?, history = ? WHERE id = ?",
                (new_status, new_title, new_target, now, json.dumps(hist), goal_id),
            )
            self._conn.commit()
        return self.get_goal(goal_id)

    def link_task_to_goal(self, task_id, goal_id) -> dict | None:
        with self._lock:
            t = self._fetch_raw(task_id)
            g = self._conn.execute("SELECT id FROM goals WHERE id = ?", (goal_id,)).fetchone()
            if not t or not g:
                return None
            self._conn.execute("UPDATE tasks SET goal_id = ? WHERE id = ?", (goal_id, task_id))
            self._conn.commit()
        return self.get_goal(goal_id)

    def goals_for_dashboard(self, limit: int = 6) -> list:
        goals = self.list_goals(limit=limit * 2)
        active = [g for g in goals if g["status"] == "active"]
        other = [g for g in goals if g["status"] != "active"]
        ordered = (active + other)[:limit]
        return [{
            "id": g["id"], "title": g["title"], "status": g["status"],
            "target_date": g.get("target_date", ""),
            "progress_pct": g["progress_pct"], "done_tasks": g["done_tasks"],
            "total_tasks": g["total_tasks"], "when": g["updated_human"],
        } for g in ordered]


# ---- module-level singleton (shared by the chat tools + dashboard) ----------
_TRACKER = None
_TRACKER_LOCK = threading.Lock()


def get_tracker(db_path: str = DEFAULT_DB) -> TaskTracker:
    global _TRACKER
    with _TRACKER_LOCK:
        if _TRACKER is None:
            _TRACKER = TaskTracker(db_path)
    return _TRACKER


# ============================================================
# CHAT-TOOL WRAPPERS — return friendly strings for the chat brain.
# ============================================================
def _fmt_task_line(t: dict) -> str:
    return f"#{t['id']} [{t['status']}] {t['title']}"


def tool_create_task(title: str, description: str = "", urgency: int = 0,
                     importance: int = 0) -> str:
    t = get_tracker().create(title, description, urgency=urgency, importance=importance)
    if t.get("error"):
        return t["error"]
    pri = ""
    if t.get("urgency") or t.get("importance"):
        pri = f" · urgency {t['urgency']}/5, importance {t['importance']}/5"
    return (f"Created task #{t['id']}: **{t['title']}** (status: idea{pri})."
            + (f"\n{t['description']}" if t.get("description") else "")
            + "\nUpdate it any time — say something like \"move task "
            f"#{t['id']} to in progress\".")


def tool_set_task_priority(task_id: int, urgency: int = None, importance: int = None) -> str:
    res = get_tracker().set_priority(task_id, urgency=urgency, importance=importance)
    if res is None:
        return f"No task #{task_id} found."
    return (f"Task #{res['id']} priority set — urgency {res['urgency']}/5, "
            f"importance {res['importance']}/5.")


# ================= GOAL TOOLS =================
def _progress_bar(pct: int) -> str:
    filled = pct // 10
    return "▰" * filled + "▱" * (10 - filled)


def tool_create_goal(title: str, description: str = "", target_date: str = "") -> str:
    g = get_tracker().create_goal(title, description, target_date)
    if g.get("error"):
        return g["error"]
    td = f" (target {g['target_date']})" if g.get("target_date") else ""
    return (f"Created goal #{g['id']}: **{g['title']}**{td}. Link tasks to it "
            f"(\"link task #N to goal #{g['id']}\") and its progress will track them.")


def tool_update_goal(goal_id: int, status: str = None, note: str = "") -> str:
    res = get_tracker().update_goal(goal_id, status=status, note=note)
    if res is None:
        return f"No goal #{goal_id} found."
    if isinstance(res, dict) and res.get("error"):
        return res["error"]
    bits = []
    if status:
        bits.append(f"status → {res['status']}")
    if note:
        bits.append("note added")
    change = (" (" + ", ".join(bits) + ")") if bits else ""
    return f"Updated goal #{res['id']} **{res['title']}**{change} — {res['progress_pct']}% done."


def tool_link_task_to_goal(task_id: int, goal_id: int) -> str:
    res = get_tracker().link_task_to_goal(task_id, goal_id)
    if res is None:
        return f"Couldn't link — check that both task #{task_id} and goal #{goal_id} exist."
    return (f"Linked task #{task_id} to goal **{res['title']}** (#{res['id']}). "
            f"Goal is now {res['progress_pct']}% ({res['done_tasks']}/{res['total_tasks']} tasks).")


def tool_list_goals(status: str = None) -> str:
    status_n = (status or "").strip().lower() or None
    goals = get_tracker().list_goals(status=status_n)
    if not goals:
        return "No goals yet." if not status_n else f"No goals with status '{status_n}'."
    lines = ["Goals:" if not status_n else f"Goals ({status_n}):"]
    for g in goals:
        td = f" · target {g['target_date']}" if g.get("target_date") else ""
        lines.append(f"\n**#{g['id']} {g['title']}** [{g['status']}]{td}")
        lines.append(f"{_progress_bar(g['progress_pct'])} {g['progress_pct']}% "
                     f"({g['done_tasks']}/{g['total_tasks']} tasks)")
        if g.get("description"):
            lines.append(g["description"])
    return "\n".join(lines)


def tool_update_task_status(task_id: int, status: str, note: str = "") -> str:
    res = get_tracker().update_status(task_id, status, note)
    if res is None:
        return f"No task #{task_id} found. Use \"list my tasks\" to see the ids."
    if res.get("error"):
        return res["error"]
    return f"Task #{res['id']} → **{res['status'].replace('_', ' ')}**." + (f" ({note})" if note else "")


def tool_list_tasks(status: str = None) -> str:
    status_n = (status or "").strip().lower().replace(" ", "_") or None
    if status_n and status_n not in VALID:
        return f"'{status}' isn't a status. Valid: {', '.join(STATUSES)}."
    tasks = get_tracker().list(status=status_n)
    if not tasks:
        return ("No tasks yet." if not status_n
                else f"No tasks in '{status_n}'.")
    header = f"Tasks ({status_n})" if status_n else "Tasks"
    lines = [header + ":"]
    for t in tasks:
        line = _fmt_task_line(t)
        if t.get("updated_human"):
            line += f"  · updated {t['updated_human']}"
        lines.append(line)
    return "\n".join(lines)


def tool_show_task_history(task_id: int) -> str:
    t = get_tracker().get(task_id)
    if not t:
        return f"No task #{task_id} found."
    lines = [f"**#{t['id']} {t['title']}** — status: {t['status']}"]
    if t.get("description"):
        lines.append(t["description"])
    lines.append("\nHistory:")
    for h in t["history"]:
        stamp = _humanize(h.get("at", ""))
        if h.get("type") == "status":
            frm = h.get("from")
            entry = f"  • {frm} → {h.get('to')}" if frm else f"  • set to {h.get('to')}"
        elif h.get("type") == "created":
            entry = "  • created"
        else:
            entry = f"  • note: {h.get('note', '')}"
        if h.get("note") and h.get("type") == "status":
            entry += f" — {h['note']}"
        lines.append(f"{entry}  ({stamp})")
    return "\n".join(lines)


def tool_add_task_note(task_id: int, note: str) -> str:
    res = get_tracker().add_note(task_id, note)
    if res is None:
        return f"No task #{task_id} found."
    if res.get("error"):
        return res["error"]
    return f"Noted on task #{task_id}."
