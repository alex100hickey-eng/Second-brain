"""
icloud_intake.py — READ-ONLY iCloud Mail intake via IMAP.

Composio has no iCloud connector, so this talks to Apple's IMAP server
directly (imap.mail.me.com:993) using Python's stdlib imaplib — no new
dependency. Feeds into the same intake.record_raw() pipeline as Gmail and
iMessage: noise-filtered, deduped, and it's the ONLY write this module ever
does — it never sends, replies to, deletes, or modifies anything on the
iCloud side, matching every other connector in this file.

Setup (Alex does this once):
  1. https://appleid.apple.com -> Sign-In and Security -> App-Specific
     Passwords -> generate one named "CLARVIS". This is NOT your Apple ID
     password — it's a revocable, mail-only credential.
  2. Add to .env:
       ICLOUD_EMAIL=you@icloud.com
       ICLOUD_APP_PASSWORD=xxxx-xxxx-xxxx-xxxx
  3. Restart the app.
"""

import email
import imaplib
import os
from datetime import datetime, timedelta, timezone
from email.header import decode_header

IMAP_HOST = "imap.mail.me.com"
IMAP_PORT = 993

_record_raw = None   # injected by init() — intake.record_raw


def init(record_raw_fn):
    global _record_raw
    _record_raw = record_raw_fn


def _configured() -> bool:
    return bool(os.environ.get("ICLOUD_EMAIL") and os.environ.get("ICLOUD_APP_PASSWORD"))


def _decode(s) -> str:
    if not s:
        return ""
    parts = decode_header(s)
    out = []
    for text, enc in parts:
        if isinstance(text, bytes):
            out.append(text.decode(enc or "utf-8", errors="replace"))
        else:
            out.append(text)
    return "".join(out)


def scan_icloud(days: int = 2, cap: int = 15) -> str:
    """Poll recent iCloud inbox mail (read-only IMAP SELECT + FETCH, never
    marks read, never moves/deletes) and ingest via the shared intake
    pipeline. Mirrors scan_gmail's contract and return shape."""
    if not _configured():
        return ("iCloud isn't connected yet — generate an app-specific password at "
                "appleid.apple.com and set ICLOUD_EMAIL / ICLOUD_APP_PASSWORD in .env.")
    try:
        conn = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
        conn.login(os.environ["ICLOUD_EMAIL"], os.environ["ICLOUD_APP_PASSWORD"])
        # read-only: no message flags are ever changed from this connection
        conn.select("INBOX", readonly=True)
        since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%d-%b-%Y")
        status, data = conn.search(None, f'(SINCE "{since}")')
        if status != "OK":
            conn.logout()
            return f"iCloud scan failed: search returned {status}"
        ids = data[0].split()[-cap:]  # most recent `cap`

        new, noise = 0, 0
        for msg_id in ids:
            # BODY.PEEK[] rather than RFC822: verified live 2026-07-26 that iCloud
            # answers RFC822 with an empty "44144 ()" and no body at all, so every
            # message was silently skipped. PEEK is also the right call regardless —
            # it's the variant defined not to set the \Seen flag, so this stays
            # genuinely read-only even if the mailbox is ever opened writable.
            raw = _fetch_raw(conn, msg_id)
            if not raw:
                continue
            m = _parse_message(raw)
            ref = m["ref"] or f"icloud-{msg_id.decode()}"
            res = _record_raw("icloud", ref, m["sender"], m["ts"],
                              f"Subject: {m['subject']}\n{m['body'][:2000]}")
            if res.get("recorded"):
                new += 1
            elif res.get("reason") == "noise":
                noise += 1
        conn.logout()
        return f"iCloud scan: {new} new intake event(s), {noise} filtered as noise."
    except imaplib.IMAP4.error as e:
        return f"iCloud login/scan failed (check the app-specific password): {e}"
    except Exception as e:
        return f"iCloud scan failed: {str(e)[:200]}"


def _parse_message(raw: bytes) -> dict:
    """One fetched RFC822 blob → {ref, sender, to, subject, ts, body}. Shared by
    the intake scanner above and mail_reader's raw-read tools."""
    msg = email.message_from_bytes(raw)
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and not part.get("Content-Disposition"):
                try:
                    body = part.get_payload(decode=True).decode(
                        part.get_content_charset() or "utf-8", errors="replace")
                except Exception:
                    body = ""
                break
    else:
        try:
            body = msg.get_payload(decode=True).decode(
                msg.get_content_charset() or "utf-8", errors="replace")
        except Exception:
            body = ""
    return {"ref": msg.get("Message-ID") or "", "sender": _decode(msg.get("From", "?")),
            "to": _decode(msg.get("To", "")), "subject": _decode(msg.get("Subject", "(no subject)")),
            "ts": msg.get("Date", ""), "body": body}


def _fetch_raw(conn, msg_id) -> bytes:
    """BODY.PEEK[] fetch with the tuple-vs-bare-bytes response handling from
    scan_icloud (see comment there); returns b"" when the shape is off."""
    status, msg_data = conn.fetch(msg_id, "(BODY.PEEK[])")
    if status != "OK" or not msg_data:
        return b""
    return next((p[1] for p in msg_data
                 if isinstance(p, tuple) and len(p) >= 2
                 and isinstance(p[1], (bytes, bytearray))), b"")


def _open_inbox():
    conn = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    conn.login(os.environ["ICLOUD_EMAIL"], os.environ["ICLOUD_APP_PASSWORD"])
    conn.select("INBOX", readonly=True)
    return conn


def list_messages(days: int = 7, cap: int = 10) -> list:
    """Recent inbox messages, newest last: [{ref, sender, subject, ts, seq}].
    Read-only; raises on connection/auth problems (caller reports)."""
    if not _configured():
        raise RuntimeError("iCloud isn't connected (ICLOUD_EMAIL / ICLOUD_APP_PASSWORD unset).")
    conn = _open_inbox()
    try:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%d-%b-%Y")
        status, data = conn.search(None, f'(SINCE "{since}")')
        if status != "OK":
            raise RuntimeError(f"IMAP search returned {status}")
        out = []
        for msg_id in data[0].split()[-cap:]:
            raw = _fetch_raw(conn, msg_id)
            if not raw:
                continue
            m = _parse_message(raw)
            m["seq"] = msg_id.decode()
            m.pop("body", None)   # listing stays light — read_message gets the body
            out.append(m)
        return out
    finally:
        conn.logout()


def read_message(ref: str) -> dict:
    """One full message by Message-ID header (or bare IMAP sequence number as a
    fallback). Returns the _parse_message dict; raises when not found."""
    if not _configured():
        raise RuntimeError("iCloud isn't connected (ICLOUD_EMAIL / ICLOUD_APP_PASSWORD unset).")
    conn = _open_inbox()
    try:
        msg_id = None
        if ref.strip().isdigit():
            msg_id = ref.strip().encode()
        else:
            status, data = conn.search(None, "HEADER", "Message-ID", ref.strip())
            if status == "OK" and data and data[0].split():
                msg_id = data[0].split()[-1]
        if not msg_id:
            raise RuntimeError(f"No iCloud message found for ref {ref!r}.")
        raw = _fetch_raw(conn, msg_id)
        if not raw:
            raise RuntimeError(f"iCloud returned no body for ref {ref!r}.")
        return _parse_message(raw)
    finally:
        conn.logout()


TOOL_SCHEMAS = [
    {"name": "scan_icloud_intake",
     "description": "Check Alex's iCloud inbox for anything actionable (read-only IMAP), same as scan_email_intake but for iCloud Mail.",
     "input_schema": {"type": "object", "properties": {
         "days": {"type": "integer", "description": "How many days back to look (default 2)."}
     }}},
]

TOOL_STATUS_LABELS = {
    "scan_icloud_intake": "Checking your iCloud mail…",
}


def handle_tool_call(name: str, tool_input: dict) -> str:
    if name == "scan_icloud_intake":
        return scan_icloud(days=tool_input.get("days", 2))
    return "Unknown iCloud tool."
