"""
job_queue.py — a lightweight, persistent background job queue for long-running work.

Long operations (website builds, video processing, data synthesis) shouldn't block the chat
turn. This extends the existing daemon-worker pattern (delegate_task / the Task Manager) with a
local, SQLite-backed queue so chat can return "started, job #N" instantly and the work runs on a
background thread.

Design mirrors the rest of the system:
  * Local SQLite (`jobs.db`, gitignored) — state PERSISTS across an app restart. Any job left
    'running' when the app died is requeued on the next boot (requeue_interrupted).
  * Thread-local SQLite connections — safe to touch from the worker thread and request handlers
    at once (same lesson as the Supabase thread-safety fix).
  * A handler registry injected by app.py (job type -> callable), so this module stays generic.
  * The worker respects the budget gate and reports failures to the monitor (both injected).

Job lifecycle: queued -> running -> done | failed. Each row carries timestamps, a human label,
the result (or error), and a 'surfaced' flag so completions can be announced exactly once.
"""

import os
import json
import time
import sqlite3
import threading
import traceback
from datetime import datetime
from zoneinfo import ZoneInfo

_TZ = ZoneInfo("America/New_York")
_HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(_HERE, "jobs.db")


def _now() -> str:
    return datetime.now(_TZ).isoformat()


class JobQueue:
    def __init__(self, db_path: str = DEFAULT_DB):
        self.db_path = db_path
        self._local = threading.local()
        self._write_lock = threading.Lock()  # serialize writes; SQLite is single-writer
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.db_path, check_same_thread=False, timeout=30)
            c.row_factory = sqlite3.Row
            self._local.conn = c
        return c

    def _init_db(self):
        with self._write_lock:
            c = self._conn()
            c.execute("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    type TEXT NOT NULL,
                    params TEXT NOT NULL,
                    label TEXT DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'queued',
                    trigger TEXT DEFAULT 'user',
                    result TEXT,
                    error TEXT,
                    surfaced INTEGER DEFAULT 0,
                    created_at TEXT,
                    started_at TEXT,
                    finished_at TEXT
                )
            """)
            c.commit()

    def enqueue(self, job_type: str, params: dict, label: str = "", trigger: str = "user") -> int:
        with self._write_lock:
            c = self._conn()
            cur = c.execute(
                "INSERT INTO jobs (type, params, label, status, trigger, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (job_type, json.dumps(params or {}), label or job_type, "queued", trigger, _now()),
            )
            c.commit()
            return cur.lastrowid

    def claim_next(self) -> dict:
        """Atomically claim the oldest queued job (compare-and-swap on status)."""
        with self._write_lock:
            c = self._conn()
            row = c.execute(
                "SELECT id FROM jobs WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
            if not row:
                return None
            upd = c.execute(
                "UPDATE jobs SET status='running', started_at=? WHERE id=? AND status='queued'",
                (_now(), row["id"]))
            c.commit()
            if upd.rowcount == 0:
                return None
            return dict(c.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone())

    def _set(self, job_id: int, **fields):
        with self._write_lock:
            c = self._conn()
            cols = ", ".join(f"{k}=?" for k in fields)
            c.execute(f"UPDATE jobs SET {cols} WHERE id=?", (*fields.values(), job_id))
            c.commit()

    def mark_done(self, job_id: int, result: str):
        self._set(job_id, status="done", result=str(result)[:8000], finished_at=_now())

    def mark_failed(self, job_id: int, error: str):
        self._set(job_id, status="failed", error=str(error)[:2000], finished_at=_now())

    def requeue(self, job_id: int):
        self._set(job_id, status="queued", started_at=None)

    def requeue_interrupted(self) -> int:
        """On boot, any job still 'running' was interrupted by the restart — requeue it."""
        with self._write_lock:
            c = self._conn()
            n = c.execute(
                "UPDATE jobs SET status='queued', started_at=NULL WHERE status='running'").rowcount
            c.commit()
            return n

    def get(self, job_id: int) -> dict:
        row = self._conn().execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return dict(row) if row else None

    def list_jobs(self, limit: int = 20, status: str = None) -> list:
        if status:
            rows = self._conn().execute(
                "SELECT * FROM jobs WHERE status=? ORDER BY id DESC LIMIT ?",
                (status, limit)).fetchall()
        else:
            rows = self._conn().execute(
                "SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def counts(self) -> dict:
        rows = self._conn().execute(
            "SELECT status, COUNT(*) n FROM jobs GROUP BY status").fetchall()
        return {r["status"]: r["n"] for r in rows}

    def take_unsurfaced_finished(self) -> list:
        """Return done/failed jobs not yet announced, marking them surfaced (exactly once)."""
        with self._write_lock:
            c = self._conn()
            rows = [dict(r) for r in c.execute(
                "SELECT * FROM jobs WHERE status IN ('done','failed') AND surfaced=0 "
                "ORDER BY id").fetchall()]
            for r in rows:
                c.execute("UPDATE jobs SET surfaced=1 WHERE id=?", (r["id"],))
            c.commit()
            return rows


def start_job_worker(queue: JobQueue, handlers: dict, is_allowed=None,
                     on_finish=None, report_event=None, poll_seconds: int = 5) -> None:
    """Daemon worker: claim queued jobs and run them through the handler registry.
    - is_allowed(agent_name) -> bool : the budget gate; if it says no, the job is requeued and
      the worker backs off (non-essential background work pauses under budget throttle).
    - on_finish(job_dict) : called after a job reaches done/failed (e.g. announce it in chat).
    - report_event(component, level, message, detail) : monitor incident log.
    """
    def loop():
        while True:
            try:
                job = queue.claim_next()
                if job:
                    jtype = job["type"]
                    if is_allowed is not None and not is_allowed("job_worker"):
                        queue.requeue(job["id"])  # budget throttle — try again later
                        time.sleep(30)
                        continue
                    handler = handlers.get(jtype)
                    if handler is None:
                        queue.mark_failed(job["id"], f"no handler registered for job type '{jtype}'")
                    else:
                        try:
                            params = json.loads(job["params"] or "{}")
                            result = handler(params)
                            queue.mark_done(job["id"], result)
                        except Exception as e:
                            queue.mark_failed(job["id"],
                                              f"{e}\n{traceback.format_exc()[:1200]}")
                            if report_event:
                                try:
                                    report_event("jarvis-job-worker", "error",
                                                 f"job #{job['id']} ({jtype}) failed", str(e))
                                except Exception:
                                    pass
                    if on_finish:
                        try:
                            on_finish(queue.get(job["id"]))
                        except Exception:
                            pass
                    continue  # immediately look for the next job
            except Exception as e:
                if report_event:
                    try:
                        report_event("jarvis-job-worker", "error", "job worker cycle failed", str(e))
                    except Exception:
                        pass
            time.sleep(poll_seconds)

    t = threading.Thread(target=loop, daemon=True, name="jarvis-job-worker")
    t.start()
