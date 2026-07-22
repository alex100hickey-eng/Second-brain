"""
imessage_intake.py — read-only iMessage reader for the intake stream (Mac home node).

Reads ~/Library/Messages/chat.db (needs Full Disk Access — already granted) and
feeds NEW messages into intake.record_raw, where the noise filter + obligation
extraction run. This is a HOME-NODE source: it only runs on the Mac
(JARVIS_RUNTIME=local); the server never sees the raw database, only the
normalized intake events in Supabase.

HARD SAFETY PROPERTIES:
  * STRICTLY READ-ONLY: the database is opened with SQLite URI mode=ro. There is
    no code path that writes to chat.db or sends a message — CLARVIS has no
    message-sending capability anywhere, by design (run_tests.py enforces
    no-control-code project-wide; external sends are structurally impossible).
  * The cursor (last seen ROWID) lives in OUR local file, not in chat.db.
  * Message text is untrusted data — intake wraps it in the injection boundary.

Apple stores message text in `text` OR (increasingly, on newer macOS) in
`attributedBody`, a typedstream blob. _decode_attributed() implements the
well-known minimal parse (find the NSString payload, read its length-prefixed
UTF-8 bytes) with paranoid fallbacks — an undecodable blob yields "" and the
message is simply skipped, never crashed on.
"""

import json
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone

import intake

CHAT_DB = os.path.expanduser("~/Library/Messages/chat.db")
CURSOR_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "imessage_cursor.json")   # gitignored
POLL_SECONDS = 180
BATCH_CAP = 25          # max messages ingested per poll (extraction costs tokens)
SKIP_OLDER_DAYS = 3     # first run: don't churn through years of history

_worker_started = False
report_event = None     # optionally injected by app.py (monitor hookup)
is_agent_allowed = None


def available() -> bool:
    """True when the Messages DB exists and is readable (i.e. we're on the Mac
    home node with Full Disk Access). On the server this is simply False."""
    try:
        con = _connect()
        con.execute("SELECT 1 FROM message LIMIT 1")
        con.close()
        return True
    except Exception:
        return False


def _connect():
    return sqlite3.connect(f"file:{CHAT_DB}?mode=ro", uri=True, timeout=5)


def _load_cursor() -> int:
    try:
        with open(CURSOR_FILE) as f:
            return int(json.load(f).get("last_rowid", 0))
    except Exception:
        return 0


def _save_cursor(rowid: int) -> None:
    try:
        with open(CURSOR_FILE, "w") as f:
            json.dump({"last_rowid": rowid, "updated_at":
                       datetime.now(timezone.utc).isoformat()}, f)
    except Exception:
        pass


def _decode_attributed(blob) -> str:
    """Best-effort text from an attributedBody typedstream. '' on any doubt."""
    if not blob:
        return ""
    try:
        data = bytes(blob)
        idx = data.find(b"NSString")
        if idx == -1:
            return ""
        # Payload starts shortly after the class name; scan for the '+' marker
        # that precedes the length-prefixed UTF-8 string in typedstream encoding.
        seg = data[idx:idx + 12]
        plus = data.find(b"+", idx)
        if plus == -1 or plus - idx > 24:
            return ""
        i = plus + 1
        length = data[i]
        i += 1
        if length == 0x81:          # two-byte little-endian length follows
            length = int.from_bytes(data[i:i + 2], "little")
            i += 2
        elif length == 0x82:        # four-byte length (very long messages)
            length = int.from_bytes(data[i:i + 4], "little")
            i += 4
        text = data[i:i + length].decode("utf-8", errors="ignore")
        # Sanity: refuse garbage (control chars, implausible length)
        if not text or len(text) > 20000:
            return ""
        return re.sub(r"[\x00-\x08\x0b-\x1f\x7f￼]", "", text).strip()
    except Exception:
        return ""


APPLE_EPOCH = 978307200  # 2001-01-01 in unix seconds; message.date is ns since then


def _fetch_new(cursor_rowid: int, cap: int = BATCH_CAP) -> list:
    """New messages after cursor: [{rowid, guid, ts, sender, chat, text, from_me}]."""
    con = _connect()
    con.row_factory = sqlite3.Row
    min_ts_ns = (time.time() - SKIP_OLDER_DAYS * 86400 - APPLE_EPOCH) * 1e9
    rows = con.execute(
        """
        SELECT m.ROWID AS rowid, m.guid, m.text, m.attributedBody, m.date,
               m.is_from_me, h.id AS handle,
               c.display_name AS chat_name, c.chat_identifier
        FROM message m
        LEFT JOIN handle h ON h.ROWID = m.handle_id
        LEFT JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
        LEFT JOIN chat c ON c.ROWID = cmj.chat_id
        WHERE m.ROWID > ? AND m.date > ?
          AND m.associated_message_type = 0      -- skip tapbacks/reactions
        ORDER BY m.ROWID ASC LIMIT ?
        """,
        (cursor_rowid, min_ts_ns, cap),
    ).fetchall()
    out = []
    for r in rows:
        text = (r["text"] or "").strip() or _decode_attributed(r["attributedBody"])
        ts = datetime.fromtimestamp(r["date"] / 1e9 + APPLE_EPOCH,
                                    tz=timezone.utc).isoformat()
        sender = "Alex (me)" if r["is_from_me"] else (r["handle"] or "unknown")
        chat = r["chat_name"] or r["chat_identifier"] or ""
        out.append({"rowid": r["rowid"], "guid": r["guid"], "ts": ts,
                    "sender": sender, "chat": chat, "text": text,
                    "from_me": bool(r["is_from_me"])})
    con.close()
    return out


def scan_once(cap: int = BATCH_CAP) -> str:
    """One poll cycle: read new messages, push through intake. Returns a summary."""
    if not available():
        return "iMessage database not readable here (home-node only)."
    cursor = _load_cursor()
    if cursor == 0:
        # First run: start at the tip minus the recent window, not year one.
        con = _connect()
        max_rowid = con.execute("SELECT COALESCE(MAX(ROWID), 0) FROM message").fetchone()[0]
        con.close()
        recent = _fetch_new(0, cap=10_000_000)   # bounded by SKIP_OLDER_DAYS anyway
        msgs = recent[-cap:]
        new_cursor = max_rowid
    else:
        msgs = _fetch_new(cursor, cap)
        new_cursor = msgs[-1]["rowid"] if msgs else cursor
    ingested, noise, empty = 0, 0, 0
    for m in msgs:
        if not m["text"]:
            empty += 1
            continue
        label = m["sender"] + (f" in '{m['chat']}'" if m["chat"] and "chat" not in
                               str(m["chat"]).lower() else "")
        res = intake.record_raw("imessage", m["guid"], label, m["ts"], m["text"])
        if res.get("recorded"):
            ingested += 1
        elif res.get("reason") == "noise":
            noise += 1
    _save_cursor(new_cursor)
    return (f"iMessage scan: {len(msgs)} new message(s) → {ingested} intake event(s), "
            f"{noise} filtered as noise, {empty} undecodable/empty.")


def _watch_loop():
    while True:
        try:
            if is_agent_allowed is None or is_agent_allowed("imessage_intake"):
                scan_once()
        except Exception as e:
            try:
                if report_event:
                    report_event("imessage-intake", "error", "scan cycle failed", str(e))
            except Exception:
                pass
        time.sleep(POLL_SECONDS)


def start_watcher(report_event_fn=None, is_agent_allowed_fn=None) -> bool:
    """Start the home-node poller (idempotent). No-op when chat.db is unreadable."""
    global _worker_started, report_event, is_agent_allowed
    if _worker_started or not available():
        return False
    report_event = report_event_fn
    is_agent_allowed = is_agent_allowed_fn
    t = threading.Thread(target=_watch_loop, daemon=True, name="jarvis-imessage-intake")
    t.start()
    _worker_started = True
    return True


TOOL_SCHEMAS = [
    {
        "name": "scan_messages_intake",
        "description": "Scan new iMessages (Mac home node, strictly read-only) into the "
                       "intake stream right now — extraction + noise filter included. "
                       "Runs automatically every few minutes; use this to force a pass.",
        "input_schema": {"type": "object", "properties": {
            "cap": {"type": "integer", "description": "Max messages this pass (default 25)."}}},
    },
]

TOOL_STATUS_LABELS = {
    "scan_messages_intake": "Reading your new texts (read-only)…",
}
