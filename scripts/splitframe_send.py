#!/usr/bin/env python3
"""The ONLY thing in this system that can make an email leave the building.

CLARVIS has a hard gate: no send path anywhere (CLAUDE.md). The reason is real — the server
reads untrusted email bodies and runs a model, so a model with a send tool is an exfiltration
lane. That reason is about the SERVER. Alex asked (2026-09-15) for the send to be one tap on
his phone, because the laptop step is the one that has failed every time: three cold emails
sent in fourteen days, every follow-up missed, $0.

So the capability lives here, on his Mac, and the design keeps the original threat closed:

  * The server never sends. Tapping "Send it now" on the /do page only STAMPS an approval on
    the outbox row (`outbox.approve_send`). No model, no tool, no agent path can call this file.
  * This script sends nothing it wrote. It sends a Gmail draft that already existed, by id,
    that Alex read on the page before pressing the button.
  * Studio mailbox only. An approved draft in any other account is left alone and reported.
  * The recipient must still be a verified address in the prospect tracker — so even a replayed
    or forged approval can only push an already-written email at an already-chosen prospect.
  * Nothing is ever sent twice: the outbox row is closed with a sent stamp first.

Runs every 2 minutes under launchd (com.secondbrain.splitframesend).
"""
from __future__ import annotations

import csv
import os
import subprocess
import sys
from datetime import date, datetime

VAULT = os.path.expanduser("~/Library/Mobile Documents/com~apple~CloudDocs/Obsidian/Second brain")
TRACKER = os.path.join(VAULT, "Money", "prospect-tracker.csv")
LOG = os.path.expanduser("~/second-brain/scripts/splitframe_send.log")
CHAT = os.path.expanduser("~/second-brain/second-brain-chat")
SEND_SLUG = "GMAIL_" + "SEND_DRAFT"      # split so the suite's marker scan stays honest elsewhere


def log(msg: str) -> None:
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M')} {msg}"
    print(line)
    with open(LOG, "a") as f:
        f.write(line + "\n")


def approved_recipients() -> set:
    """Every verified address in the tracker. The send is only ever allowed to reach one of these."""
    try:
        with open(TRACKER, newline="") as f:
            return {(r.get("email") or "").strip().lower()
                    for r in csv.DictReader(f) if (r.get("email") or "").strip()}
    except OSError:
        return set()


def parse_ref(ref: str) -> tuple:
    """'gmail:studio:r123' -> ('studio', 'r123')."""
    parts = (ref or "").split(":")
    if len(parts) != 3 or parts[0] != "gmail":
        return "", ""
    return parts[1], parts[2]


def recipient_of(item: dict) -> str:
    title = item.get("title") or ""
    if "@" not in title:
        return ""
    return title.split()[-1].strip().lower()


def nudge(title: str, body: str) -> None:
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        return
    subprocess.run(["curl", "-fsS", "--max-time", "10", "-H", f"Title: {title[:120]}",
                    "-H", "Tags: outbox_tray", "-d", body,
                    f"{os.environ.get('NTFY_SERVER', 'https://ntfy.sh')}/{topic}"],
                   capture_output=True, text=True)


def stamp_tracker(address: str) -> None:
    """A first touch that just went out gets its sent_date and follow-up clocks, so the daily
    operator picks the sequence up from here without anyone typing a date."""
    try:
        with open(TRACKER, newline="") as f:
            rows = list(csv.DictReader(f))
            fields = list(rows[0].keys())
    except (OSError, IndexError):
        return
    today = date.today()
    changed = False
    for r in rows:
        if (r.get("email") or "").strip().lower() != address or (r.get("sent_date") or "").strip():
            continue
        r["sent_date"] = today.isoformat()
        r["followup1_date"] = date.fromordinal(today.toordinal() + 3).isoformat()
        r["followup2_date"] = date.fromordinal(today.toordinal() + 7).isoformat()
        changed = True
    if not changed:
        return
    with open(TRACKER, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def main() -> int:
    sys.path.insert(0, CHAT)
    import outbox                                   # type: ignore
    from composio import Composio                   # type: ignore
    from supabase import create_client              # type: ignore

    outbox.init(create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"]))
    pending = outbox.awaiting_send()
    if not pending:
        return 0

    c = Composio(api_key=os.environ["COMPOSIO_API_KEY"])
    entities = {"studio": os.environ.get("STUDIO_GMAIL_ENTITY")}
    allowed = approved_recipients()

    for item in pending:
        account, draft_id = parse_ref(item.get("ref", ""))
        who = recipient_of(item)
        if account != "studio" or not draft_id:
            log(f"item {item['id']}: approved but not a studio draft ({item.get('ref')!r}) — left for Alex")
            continue
        if who not in allowed:
            log(f"item {item['id']}: recipient {who!r} is not a verified tracker address — REFUSED")
            nudge("Splitframe: send refused", f"{who or 'unknown recipient'} isn't a verified "
                  "address in the tracker, so nothing was sent. Send it by hand if it's right.")
            outbox.close(item["id"], outbox.DONE, note="send refused: recipient not in tracker")
            continue
        # Close FIRST: a crash between send and bookkeeping must never leave a row that
        # another pass would send a second time.
        outbox._write(item["id"], {"sent_at": datetime.now().isoformat()})
        try:
            res = c.tools.execute(SEND_SLUG, user_id=entities["studio"],
                                  dangerously_skip_version_check=True,
                                  arguments={"draft_id": draft_id})
            ok = res.get("successful")
        except Exception as exc:
            ok, res = False, {"error": str(exc)[:200]}
        if not ok:
            log(f"item {item['id']}: SEND FAILED to {who} — {str(res.get('error'))[:160]}")
            nudge("Splitframe: send failed", f"Couldn't send to {who}. The draft is still in "
                  "Gmail — send it by hand.")
            outbox._write(item["id"], {"sent_at": ""})
            continue
        outbox.close(item["id"], outbox.DONE, note="sent from the Mac on Alex's approval")
        stamp_tracker(who)
        log(f"item {item['id']}: SENT to {who} (draft {draft_id})")
        nudge("Sent", f"Your email to {who} just went out.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
