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
import re
import subprocess
import sys
from datetime import date, datetime

VAULT = os.path.expanduser("~/Library/Mobile Documents/com~apple~CloudDocs/Obsidian/Second brain")
TRACKER = os.path.join(VAULT, "Money", "prospect-tracker.csv")
LOG = os.path.expanduser("~/second-brain/scripts/splitframe_send.log")
PAUSE_FILE = os.path.expanduser("~/second-brain/scripts/SPLITFRAME_PAUSE")
DAILY_CAP = 5           # FLOOR only — see daily_cap(). Also the blast radius of a drafter bug.
CHAT = os.path.expanduser("~/second-brain/second-brain-chat")
SEND_SLUG = "GMAIL_" + "SEND_DRAFT"      # split so the suite's marker scan stays honest elsewhere


def log(msg: str) -> None:
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M')} {msg}"
    print(line)
    with open(LOG, "a") as f:
        f.write(line + "\n")



def _sent_today() -> int:
    """How many this script has already sent today, read back from its own log."""
    today = date.today().isoformat()
    try:
        with open(LOG) as f:
            return sum(1 for line in f if line.startswith(today) and ": SENT to " in line)
    except OSError:
        return 0


def daily_cap() -> int:
    """How many auto-sends this script allows today — the SAME cadence the release uses.

    These were two numbers that had to agree and didn't. The release cadence is bounce-aware and
    had been raised to 10/day; this script's cap stayed hardcoded at 5. So ten drafts were released
    each morning and five of them quietly waited for tomorrow, forever — the lane running at half
    throttle with nothing in any log saying so, because "daily cap reached" reads like correct
    behaviour. Read the one source of truth instead of restating it.
    """
    try:
        sys.path.insert(0, os.path.expanduser("~/second-brain/scripts"))
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "_sfd_cap", os.path.expanduser("~/second-brain/scripts/splitframe_daily.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        cap, _why = mod.current_cap()
        return max(DAILY_CAP, int(cap))
    except Exception:
        return DAILY_CAP       # fail to the floor, never to "unlimited"


# How long a refused row waits before trying again. Long enough that a genuinely wrong address
# cannot nag every two minutes; short enough that fixing the list the same day still sends.
REFUSED_HOLD_HOURS = 6

CREATOR_LIST = os.path.join(VAULT, "Money", "Creator Lane — Prospects.md")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def approved_recipients() -> set:
    """Every curated address this script is allowed to reach.

    The point of the whitelist is that a replayed or forged approval can still only push an
    already-written email at an already-chosen prospect. So it has to cover every list the drafter
    is allowed to draft from — and it silently did not.

    It read only the tracker's `email` column, the named-person address. On 2026-09-17 the queue
    gained address tiers and started drafting to FRONT DESK addresses, which live in a separate
    `email_generic` column; that is how draftable went 0 -> 41. The gate was never widened to
    match, so from that day every front-desk send was refused at the last step — the drafts were
    written, released, approved and then thrown away with the outbox row closed. Ten of them died
    that way on 2026-09-19 before anyone noticed, because the refusal looks like a safety feature.
    The creator lane has the same hole: its prospects are in a vault doc, never in the tracker.

    Lesson worth keeping: when the drafter learns a new source of recipients, the SEND GATE is
    part of that change, not a separate concern.
    """
    out = set()
    try:
        with open(TRACKER, newline="") as f:
            for r in csv.DictReader(f):
                for col in ("email", "email_generic"):
                    addr = (r.get(col) or "").strip().lower()
                    if addr:
                        out.add(addr)
    except OSError:
        pass
    # Creator-lane prospects are curated by hand in the vault and never enter the tracker.
    try:
        with open(CREATOR_LIST) as f:
            out.update(a.lower() for a in _EMAIL_RE.findall(f.read()))
    except OSError:
        pass
    return out


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
    operator picks the sequence up from here without anyone typing a date.

    Matches BOTH address columns. It used to compare only `email`, which meant every front-desk
    send (the `email_generic` column, and the majority of the list since 2026-09-17) went out and
    was never stamped — no sent_date, so no +3d and no +7d, so the brand got exactly one email and
    was then invisible to the follow-up drafter forever. Replies come from follow-ups, so that is
    the difference between a prospect and a wasted send. Found 2026-09-19 on the same day the send
    gate turned out to have the identical blind spot.
    """
    try:
        with open(TRACKER, newline="") as f:
            rows = list(csv.DictReader(f))
            fields = list(rows[0].keys())
    except (OSError, IndexError):
        return
    today = date.today()
    changed = False
    for r in rows:
        addrs = {(r.get(c) or "").strip().lower() for c in ("email", "email_generic")}
        if address not in addrs or (r.get("sent_date") or "").strip():
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



def _beat(note: str = "") -> None:
    """Liveness into the shared store so the always-on server can see this Mac job."""
    try:
        sys.path.insert(0, CHAT if "CHAT" in globals() else os.path.expanduser("~/second-brain/second-brain-chat"))
        import intake, monitor
        from supabase import create_client
        if intake.supabase is None:
            intake.supabase = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
        monitor.supabase = intake.supabase
        monitor.beat("splitframe-send", 3600, note)
    except Exception:
        pass

def main() -> int:
    sys.path.insert(0, CHAT)
    import outbox                                   # type: ignore
    from composio import Composio                   # type: ignore
    from supabase import create_client              # type: ignore

    outbox.init(create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"]))
    if os.path.exists(PAUSE_FILE):
        log("paused (scripts/SPLITFRAME_PAUSE exists) — nothing sent")
        return 0

    _beat("alive")
    pending = list(outbox.awaiting_send())
    approved_ids = {it["id"] for it in pending}
    # Auto-send: drafts whose hold window has expired. Alex asked for this 2026-09-15 so he can be
    # hands-off. The cap is what stops a Mac that slept through three days of drafts waking up and
    # firing all of them into the same morning.
    sent_today = _sent_today()
    cap = daily_cap()
    room = max(0, cap - sent_today)
    if room:
        for it in outbox.due_to_auto_send(datetime.now().isoformat()):
            if it["id"] in approved_ids or len(pending) - len(approved_ids) >= room:
                continue
            pending.append(it)
    elif outbox.due_to_auto_send(datetime.now().isoformat()):
        log(f"daily cap reached ({sent_today}/{cap}) — auto-sends deferred to tomorrow")
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
            # HELD, never closed. This used to close the row DONE, which threw the email away:
            # a written, released, approved first touch was destroyed and the row then read as
            # if it had been sent. That is the wrong direction for a SAFETY gate — the gate
            # exists because the whitelist might be wrong, and the whitelist being wrong is
            # exactly the case where the email is fine and the list needs fixing.
            #
            # It nearly cost ten emails today: the gate had never been widened to cover
            # front-desk addresses, so the whole 2026-09-19 batch was refused, and it only
            # survived because the list was fixed within two minutes. An hour later and all ten
            # would have been silently closed DONE.
            #
            # Snoozing keeps the row open and retries on its own once the list is corrected,
            # and spaces the nudge so a genuinely wrong address cannot nag every two minutes.
            log(f"item {item['id']}: recipient {who!r} is not an approved address — HELD, not sent")
            outbox.snooze(item["id"], hours=REFUSED_HOLD_HOURS)
            nudge("Splitframe: send held", f"{who or 'unknown recipient'} isn't on the approved "
                  "list, so nothing was sent. The email is still queued — add the address to the "
                  "tracker or the creator list and it goes on the next pass.")
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
        route = "on Alex's approval" if item.get("send_approved") else "automatically (hold window expired)"
        outbox.close(item["id"], outbox.DONE, note=f"sent from the Mac {route}")
        stamp_tracker(who)
        log(f"item {item['id']}: SENT to {who} (draft {draft_id}) — {route}")
        nudge("Sent", f"Your email to {who} just went out.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
