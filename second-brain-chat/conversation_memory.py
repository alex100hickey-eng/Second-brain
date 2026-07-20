"""
conversation_memory.py — long-term conversation memory for the Second Brain chat brain.

The chat's *live* working window still comes from Supabase (load_chat_history, capped
and cleared with the Clear button). THIS module is the durable, searchable long-term
memory: every message is mirrored into a local SQLite database, grouped into sessions
by inactivity, summarized when a session closes, and made searchable so Jarvis can
"remember" what you discussed days or weeks ago — either because you ask, or
automatically when a new message is relevant to a past one.

Design goals:
  * Local + private. Storage is a gitignored SQLite file; nothing leaves the machine.
  * Standalone + testable. No app import; a summarizer callable is injected (falls back
    to a deterministic heuristic summary when no model client is wired, so offline tests
    and a cold start still work).
  * Cheap on the hot path. Logging a message is a couple of inserts. Summarization of a
    closed session runs on a background daemon thread so the chat never blocks on it.
  * Search that degrades gracefully. Uses SQLite FTS5 when the build supports it, else a
    LIKE-based fallback — same public API either way.

Nothing here executes anything or acts on your behalf; it only records and retrieves.
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

DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "conversation_memory.db")

# A new session starts when this much wall-clock passes with no messages. 45 min is
# long enough that a quick "back in a sec" stays one conversation, short enough that a
# next-morning chat is a fresh session (so "yesterday's conversation" means something).
SESSION_GAP_SECONDS = int(os.environ.get("MEMORY_SESSION_GAP", 45 * 60))

# Common words we don't want to drive relevance matching.
_STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "if", "then", "is", "are", "was", "were",
    "be", "been", "being", "to", "of", "in", "on", "for", "with", "at", "by", "from",
    "up", "about", "into", "over", "after", "i", "you", "he", "she", "it", "we",
    "they", "me", "my", "your", "his", "her", "our", "their", "this", "that", "these",
    "those", "what", "which", "who", "whom", "how", "when", "where", "why", "do",
    "does", "did", "can", "could", "will", "would", "should", "so", "just", "get",
    "got", "have", "has", "had", "not", "no", "yes", "ok", "okay", "please", "thanks",
    "hey", "hi", "let", "lets", "want", "need", "know", "think", "tell", "say", "said",
}


def _now() -> datetime:
    return datetime.now(_TZ)


def _now_iso() -> str:
    return _now().isoformat()


def _parse(iso: str):
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso)
    except ValueError:
        return None


def _humanize(iso: str) -> str:
    dt = _parse(iso)
    if not dt:
        return ""
    delta = _now() - dt
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


def _tokens(text: str) -> list:
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    return [w for w in words if len(w) > 2 and w not in _STOPWORDS]


class ConversationMemory:
    def __init__(self, db_path: str = DEFAULT_DB, summarizer=None):
        self.db_path = db_path
        self._lock = threading.RLock()
        self._summarizer = summarizer  # callable(list_of_msg_dicts) -> (title, summary)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._fts = False
        self._init_schema()

    # ------------------------------------------------------------------ schema
    def _init_schema(self):
        with self._lock:
            c = self._conn
            c.execute(
                """CREATE TABLE IF NOT EXISTS sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at TEXT NOT NULL,
                    ended_at TEXT NOT NULL,
                    title TEXT DEFAULT '',
                    summary TEXT DEFAULT '',
                    message_count INTEGER NOT NULL DEFAULT 0,
                    closed INTEGER NOT NULL DEFAULT 0
                )"""
            )
            c.execute(
                """CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    ts TEXT NOT NULL
                )"""
            )
            c.execute("CREATE INDEX IF NOT EXISTS idx_msg_session ON messages(session_id)")
            # Try to stand up an FTS5 mirror for fast search; fall back to LIKE if the
            # SQLite build has no FTS5.
            try:
                c.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts "
                    "USING fts5(content, session_id UNINDEXED, content='messages', content_rowid='id')"
                )
                self._fts = True
            except sqlite3.OperationalError:
                self._fts = False
            c.commit()

    # ------------------------------------------------------------- internals
    def _open_session_row(self):
        cur = self._conn.execute(
            "SELECT * FROM sessions WHERE closed = 0 ORDER BY id DESC LIMIT 1"
        )
        return cur.fetchone()

    def _new_session(self, now_iso: str) -> int:
        cur = self._conn.execute(
            "INSERT INTO sessions (started_at, ended_at, message_count, closed) "
            "VALUES (?, ?, 0, 0)",
            (now_iso, now_iso),
        )
        return cur.lastrowid

    def _current_session_id(self, now: datetime) -> int:
        """Return the id of the session this moment belongs to, closing a stale open
        session (and queuing it for summarization) when the gap is too large."""
        row = self._open_session_row()
        if row is None:
            return self._new_session(now.isoformat())
        last = _parse(row["ended_at"]) or now
        if (now - last).total_seconds() > SESSION_GAP_SECONDS:
            # Close the stale session, summarize it in the background, open a new one.
            self._conn.execute(
                "UPDATE sessions SET closed = 1 WHERE id = ?", (row["id"],)
            )
            self._conn.commit()
            _queue_summary(self, row["id"])
            return self._new_session(now.isoformat())
        return row["id"]

    # ------------------------------------------------------------------ write
    def log(self, role: str, content: str) -> int:
        """Record one chat message. Returns its message id (0 if skipped)."""
        role = (role or "").strip()
        content = (content or "").strip()
        if role not in ("user", "assistant") or not content:
            return 0
        now = _now()
        with self._lock:
            sid = self._current_session_id(now)
            cur = self._conn.execute(
                "INSERT INTO messages (session_id, role, content, ts) VALUES (?, ?, ?, ?)",
                (sid, role, content, now.isoformat()),
            )
            mid = cur.lastrowid
            if self._fts:
                self._conn.execute(
                    "INSERT INTO messages_fts (rowid, content, session_id) VALUES (?, ?, ?)",
                    (mid, content, sid),
                )
            self._conn.execute(
                "UPDATE sessions SET ended_at = ?, message_count = message_count + 1 WHERE id = ?",
                (now.isoformat(), sid),
            )
            self._conn.commit()
        return mid

    # --------------------------------------------------------------- summary
    def close_open_sessions(self, older_than_seconds: int = SESSION_GAP_SECONDS) -> int:
        """Close (and summarize) any open session idle longer than the gap. Called at
        startup so a session left open by a crash still gets summarized. Returns count."""
        closed = 0
        now = _now()
        with self._lock:
            rows = self._conn.execute("SELECT * FROM sessions WHERE closed = 0").fetchall()
            for r in rows:
                last = _parse(r["ended_at"]) or now
                if (now - last).total_seconds() > older_than_seconds and r["message_count"] > 0:
                    self._conn.execute("UPDATE sessions SET closed = 1 WHERE id = ?", (r["id"],))
                    closed += 1
            self._conn.commit()
            ids = [r["id"] for r in rows
                   if (now - (_parse(r["ended_at"]) or now)).total_seconds() > older_than_seconds
                   and r["message_count"] > 0]
        for sid in ids:
            _queue_summary(self, sid)
        return closed

    def summarize_session(self, session_id: int, force: bool = False) -> dict | None:
        """Generate (or regenerate with force) a title + summary for a session."""
        with self._lock:
            srow = self._conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
            if not srow:
                return None
            if srow["summary"] and not force:
                return dict(srow)
            msgs = self._conn.execute(
                "SELECT role, content, ts FROM messages WHERE session_id = ? ORDER BY id", (session_id,)
            ).fetchall()
        msg_dicts = [{"role": m["role"], "content": m["content"], "ts": m["ts"]} for m in msgs]
        if not msg_dicts:
            return dict(srow)

        title, summary = self._make_summary(msg_dicts)
        with self._lock:
            self._conn.execute(
                "UPDATE sessions SET title = ?, summary = ? WHERE id = ?",
                (title, summary, session_id),
            )
            self._conn.commit()
            srow = self._conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        return dict(srow)

    def _make_summary(self, msgs: list) -> tuple:
        """Return (title, summary). Uses the injected model summarizer if present, else
        a deterministic heuristic so the system is never dependent on the network."""
        if self._summarizer is not None:
            try:
                title, summary = self._summarizer(msgs)
                title = (title or "").strip()[:120]
                summary = (summary or "").strip()
                if summary:
                    return title or self._heuristic_title(msgs), summary
            except Exception as e:  # never let a summarizer failure break memory
                print(f"conversation_memory: summarizer failed, using heuristic ({e})")
        return self._heuristic_summary(msgs)

    def _heuristic_title(self, msgs: list) -> str:
        first_user = next((m["content"] for m in msgs if m["role"] == "user"), "")
        first_user = re.sub(r"\s+", " ", first_user).strip()
        return (first_user[:70] + "…") if len(first_user) > 70 else (first_user or "Conversation")

    def _heuristic_summary(self, msgs: list) -> tuple:
        title = self._heuristic_title(msgs)
        user_msgs = [m["content"] for m in msgs if m["role"] == "user"]
        # Top keywords across the user's side of the conversation.
        freq = {}
        for content in user_msgs:
            for tok in _tokens(content):
                freq[tok] = freq.get(tok, 0) + 1
        top = sorted(freq, key=lambda k: freq[k], reverse=True)[:8]
        parts = [f"Conversation with {len(msgs)} messages."]
        if user_msgs:
            preview = re.sub(r"\s+", " ", user_msgs[0]).strip()
            parts.append(f"Opened with: \"{preview[:160]}\".")
        if top:
            parts.append("Topics: " + ", ".join(top) + ".")
        return title, " ".join(parts)

    # ---------------------------------------------------------------- search
    def search(self, query: str, limit: int = 6) -> list:
        """Return matching sessions with the best-matching snippet from each.
        Result: [{session_id, title, summary, when, ended_at, message_count, snippet, role}]."""
        toks = _tokens(query)
        if not toks:
            return []
        hits = {}  # session_id -> {"snippet", "role", "score", "mid"}
        with self._lock:
            rows = []
            if self._fts:
                fts_q = " OR ".join(toks)
                try:
                    rows = self._conn.execute(
                        "SELECT m.id, m.session_id, m.role, m.content "
                        "FROM messages_fts f JOIN messages m ON m.id = f.rowid "
                        "WHERE messages_fts MATCH ? ORDER BY bm25(messages_fts) LIMIT 200",
                        (fts_q,),
                    ).fetchall()
                except sqlite3.OperationalError:
                    rows = []
            if not rows:
                like = "%" + "%".join(toks[:1]) + "%"
                rows = self._conn.execute(
                    "SELECT id, session_id, role, content FROM messages "
                    "WHERE lower(content) LIKE ? ORDER BY id DESC LIMIT 200",
                    (like,),
                ).fetchall()

            for r in rows:
                content_l = r["content"].lower()
                score = sum(content_l.count(t) for t in toks)
                if score <= 0:
                    continue
                sid = r["session_id"]
                if sid not in hits or score > hits[sid]["score"]:
                    hits[sid] = {
                        "score": score, "role": r["role"],
                        "snippet": self._snippet(r["content"], toks), "mid": r["id"],
                    }
            if not hits:
                return []
            session_rows = {
                s["id"]: s for s in self._conn.execute(
                    "SELECT * FROM sessions WHERE id IN (%s)" %
                    ",".join("?" * len(hits)), tuple(hits.keys())
                ).fetchall()
            }
        out = []
        for sid, h in sorted(hits.items(), key=lambda kv: kv[1]["score"], reverse=True)[:limit]:
            s = session_rows.get(sid)
            if not s:
                continue
            out.append({
                "session_id": sid,
                "title": s["title"] or self._untitled(sid),
                "summary": s["summary"],
                "ended_at": s["ended_at"],
                "when": _humanize(s["ended_at"]),
                "message_count": s["message_count"],
                "snippet": h["snippet"],
                "role": h["role"],
                "score": h["score"],
            })
        return out

    def _snippet(self, content: str, toks: list, width: int = 160) -> str:
        content_flat = re.sub(r"\s+", " ", content).strip()
        low = content_flat.lower()
        pos = -1
        for t in toks:
            p = low.find(t)
            if p != -1 and (pos == -1 or p < pos):
                pos = p
        if pos == -1:
            return content_flat[:width] + ("…" if len(content_flat) > width else "")
        start = max(0, pos - width // 3)
        end = min(len(content_flat), start + width)
        snip = content_flat[start:end]
        if start > 0:
            snip = "…" + snip
        if end < len(content_flat):
            snip = snip + "…"
        return snip

    def _untitled(self, sid: int) -> str:
        return f"Session #{sid}"

    # ------------------------------------------------------ automatic recall
    def relevant_context(self, query: str, limit: int = 3, exclude_session_id: int = None) -> str:
        """A compact block of the most relevant PAST conversation for a new message —
        injected into the system prompt so Jarvis just 'remembers'. Empty string when
        nothing relevant, so the prompt isn't padded with noise."""
        results = [r for r in self.search(query, limit=limit + 2)
                   if r["session_id"] != exclude_session_id and r["score"] >= 2]
        if not results:
            return ""
        lines = []
        for r in results[:limit]:
            when = r["when"] or "earlier"
            gist = r["summary"] or r["snippet"]
            gist = re.sub(r"\s+", " ", gist).strip()
            if len(gist) > 220:
                gist = gist[:220] + "…"
            lines.append(f"- ({when}) {r['title']}: {gist}")
        return "\n".join(lines)

    def last_closed_summary(self, within_days: int = 3) -> dict | None:
        """The most recent closed, summarized session (for a morning briefing's
        'yesterday's conversation'). None if nothing recent."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM sessions WHERE closed = 1 AND summary != '' "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
        if not row:
            return None
        ended = _parse(row["ended_at"])
        if ended and (_now() - ended) > timedelta(days=within_days):
            return None
        return dict(row)

    # -------------------------------------------------------- browse / manage
    def list_sessions(self, limit: int = 50) -> list:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM sessions WHERE message_count > 0 ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [{
            "session_id": r["id"],
            "title": r["title"] or self._untitled(r["id"]),
            "summary": r["summary"],
            "message_count": r["message_count"],
            "started_at": r["started_at"],
            "ended_at": r["ended_at"],
            "when": _humanize(r["ended_at"]),
            "closed": bool(r["closed"]),
        } for r in rows]

    def get_session(self, session_id: int) -> dict | None:
        with self._lock:
            s = self._conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
            if not s:
                return None
            msgs = self._conn.execute(
                "SELECT role, content, ts FROM messages WHERE session_id = ? ORDER BY id", (session_id,)
            ).fetchall()
        return {
            "session_id": s["id"],
            "title": s["title"] or self._untitled(s["id"]),
            "summary": s["summary"],
            "message_count": s["message_count"],
            "started_at": s["started_at"],
            "ended_at": s["ended_at"],
            "when": _humanize(s["ended_at"]),
            "closed": bool(s["closed"]),
            "messages": [{"role": m["role"], "content": m["content"],
                          "ts": m["ts"], "when": _humanize(m["ts"])} for m in msgs],
        }

    def delete_session(self, session_id: int) -> bool:
        """Permanently delete a conversation from memory."""
        with self._lock:
            s = self._conn.execute("SELECT id FROM sessions WHERE id = ?", (session_id,)).fetchone()
            if not s:
                return False
            if self._fts:
                try:
                    self._conn.execute("DELETE FROM messages_fts WHERE session_id = ?", (session_id,))
                except sqlite3.OperationalError:
                    pass
            self._conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            self._conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            self._conn.commit()
        return True

    def stats(self) -> dict:
        with self._lock:
            s = self._conn.execute(
                "SELECT COUNT(*) n, COALESCE(SUM(message_count),0) m FROM sessions WHERE message_count > 0"
            ).fetchone()
        return {"sessions": s["n"], "messages": s["m"], "fts": self._fts}


# ================================================================ singleton ==
_MEM = None
_MEM_LOCK = threading.Lock()


def get_memory(db_path: str = DEFAULT_DB, summarizer=None) -> ConversationMemory:
    global _MEM
    with _MEM_LOCK:
        if _MEM is None:
            _MEM = ConversationMemory(db_path, summarizer=summarizer)
        elif summarizer is not None and _MEM._summarizer is None:
            _MEM._summarizer = summarizer
    return _MEM


# ---- background summarization (never block the chat request) ----------------
def _queue_summary(mem: "ConversationMemory", session_id: int) -> None:
    def _run():
        try:
            mem.summarize_session(session_id)
        except Exception as e:
            print(f"conversation_memory: background summary failed for #{session_id}: {e}")
    threading.Thread(target=_run, daemon=True).start()


# ================================================================ chat tools ==
def tool_search_memory(query: str, limit: int = 5) -> str:
    """Search past conversations. Friendly string for the chat brain."""
    query = (query or "").strip()
    if not query:
        return "Tell me what to search your conversation history for."
    results = get_memory().search(query, limit=limit)
    if not results:
        return f"I don't find anything in our past conversations about \"{query}\"."
    lines = [f"From our past conversations about \"{query}\":"]
    for r in results:
        head = r["title"] or f"Session #{r['session_id']}"
        lines.append(f"\n**{head}** ({r['when']}, {r['message_count']} messages)")
        if r["summary"]:
            lines.append(r["summary"])
        lines.append(f"…{r['snippet']}…")
    return "\n".join(lines)


def recall_for_prompt(user_message: str, exclude_session_id: int = None) -> str:
    """Compact recall block for automatic system-prompt injection (or '')."""
    try:
        return get_memory().relevant_context(user_message, limit=3,
                                              exclude_session_id=exclude_session_id)
    except Exception as e:
        print(f"conversation_memory: recall failed ({e})")
        return ""
