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
            status, msg_data = conn.fetch(msg_id, "(BODY.PEEK[])")
            if status != "OK" or not msg_data:
                continue
            # IMAP FETCH responses aren't uniformly shaped: the message body
            # arrives as a (header, body) TUPLE, but the server also returns bare
            # bytes items (flags, closing parens) in the same list. Indexing [0][1]
            # blindly grabs a single int out of one of those bare items instead of
            # the message — pick out the first real tuple payload instead.
            raw = next((p[1] for p in msg_data
                        if isinstance(p, tuple) and len(p) >= 2
                        and isinstance(p[1], (bytes, bytearray))), None)
            if raw is None:
                continue
            msg = email.message_from_bytes(raw)
            ref = msg.get("Message-ID") or f"icloud-{msg_id.decode()}"
            sender = _decode(msg.get("From", "?"))
            subject = _decode(msg.get("Subject", "(no subject)"))
            ts = msg.get("Date", "")
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
            res = _record_raw("icloud", ref, sender, ts, f"Subject: {subject}\n{body[:2000]}")
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


TOOL_SCHEMAS = [
    {"name": "scan_icloud_intake",
     "description": "Check Alex's iCloud inbox for anything actionable (read-only IMAP), same as scan_email_intake but for iCloud Mail.",
     "input_schema": {"type": "object", "properties": {
         "days": {"type": "integer", "description": "How many days back to look (default 2)."}
     }}},
]


def handle_tool_call(name: str, tool_input: dict) -> str:
    if name == "scan_icloud_intake":
        return scan_icloud(days=tool_input.get("days", 2))
    return "Unknown iCloud tool."
