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
            self._conn.commit()

    # ---- internal helpers ----
    def _row_to_dict(self, row) -> dict:
        d = dict(row)
        try:
            d["history"] = json.loads(d.get("history") or "[]")
        except (json.JSONDecodeError, TypeError):
            d["history"] = []
        d["updated_human"] = _humanize(d.get("updated_at", ""))
        d["created_human"] = _humanize(d.get("created_at", ""))
        return d

    def _fetch_raw(self, task_id):
        cur = self._conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
        return cur.fetchone()

    # ---- public API ----
    def create(self, title: str, description: str = "") -> dict:
        title = (title or "").strip()
        if not title:
            return {"error": "A task needs a title."}
        now = _now_iso()
        history = [{"type": "created", "at": now, "note": "task created", "to": "idea"}]
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO tasks (title, description, status, created_at, updated_at, history) "
                "VALUES (?, ?, 'idea', ?, ?, ?)",
                (title, (description or "").strip(), now, now, json.dumps(history)),
            )
            self._conn.commit()
            row = self._fetch_raw(cur.lastrowid)
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
        """Compact rows for the home dashboard's tasks panel. Terminal tasks sink
        below active ones so what's actually in-flight shows first."""
        tasks = self.list(limit=200)
        active = [t for t in tasks if t["status"] not in ("done", "dropped")]
        terminal = [t for t in tasks if t["status"] in ("done", "dropped")]
        ordered = (active + terminal)[:limit]
        return [{
            "id": t["id"], "title": t["title"], "status": t["status"],
            "updated_human": t["updated_human"],
        } for t in ordered]


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


def tool_create_task(title: str, description: str = "") -> str:
    t = get_tracker().create(title, description)
    if t.get("error"):
        return t["error"]
    return (f"Created task #{t['id']}: **{t['title']}** (status: idea)."
            + (f"\n{t['description']}" if t.get("description") else "")
            + "\nUpdate it any time — say something like \"move task "
            f"#{t['id']} to in progress\".")


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
