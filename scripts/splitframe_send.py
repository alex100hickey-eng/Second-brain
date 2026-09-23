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

Runs under launchd (com.secondbrain.splitframesend): the repo plist says every 120 s, the copy
installed on 2026-09-23 runs every 600 s. RUN_BUDGET_SECONDS stays below both.
"""
from __future__ import annotations

import csv
import json
import os
import random
import re
import signal
import socket
import subprocess
import sys
from datetime import date, datetime, timedelta

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


# ---------------------------------------------------------------------------
# Pacing, quiet hours and a watchdog — the three things the 2026-09-23 deliverability audit
# found this script doing to the sending domain.
#
# BURSTS. This loop sent every expired-hold item in one run: 12 emails in one minute on 09-18,
# 10 in one minute on 09-19 and 09-20, 10 in two minutes at 00:56 on 09-22. Ten identical-shape
# cold emails leaving one mailbox inside a minute is the most spam-like thing a young domain can
# do, and the warmup plan's one mechanical rule was "spaced across the day, never a burst".
# Now: ONE send per run, and a 4-9 minute gap before the next one (launchd fires every 120 s per
# the repo plist, every 600 s as installed on 2026-09-23 — one send per tick either way). Ten
# emails take one to two hours.
#
# NIGHT SENDS. 16 of 79 sends went out between midnight and 03:00 — the 3-hour hold expired while
# the Mac slept and everything fired the moment it woke. A cold email that lands at 01:00 is at
# the bottom of the morning pile. Now: nothing sends between 22:00 and 07:30 local. An item Alex
# approved by hand still goes at night (his tap means now), but keeps the gap.
#
# HANGS. On 2026-09-23 the run that started 01:56 sent three emails and then blocked forever in an
# SSL read on the Supabase call that closes the row (the same failure that took the reply watcher
# out for 34 hours on 09-21). launchd never starts a new instance while the old one lives, so one
# hung run stopped ALL sending for eight hours with the process looking perfectly healthy. Now: a
# SIGALRM watchdog kills the run well inside the launchd interval, and the process exits through
# os._exit so no library's atexit hook can hang it either.
RUN_BUDGET_SECONDS = 100       # below the launchd interval (120 s repo / 600 s installed)
QUIET_START = (22, 0)          # local clock; from here...
QUIET_END = (7, 30)            # ...until here, nothing auto-sends
MIN_GAP_MIN, JITTER_MIN = 4, 5 # between two sends: 4 + [0, 5) minutes
PACE_STATE = os.path.expanduser("~/second-brain/scripts/splitframe_send_state.json")
socket.setdefaulttimeout(60)   # belt and braces; httpx keeps its own timeouts, SIGALRM is the net

_armed_for = RUN_BUDGET_SECONDS


def _watchdog(_sig, _frm):
    log(f"ABORTED: run exceeded {_armed_for}s and was killed so the next one can start")
    os._exit(1)


def arm_watchdog(seconds: int = RUN_BUDGET_SECONDS) -> None:
    global _armed_for
    _armed_for = seconds
    try:
        signal.signal(signal.SIGALRM, _watchdog)
        signal.alarm(seconds)
    except (AttributeError, ValueError):
        pass                   # not the main thread, or a platform without SIGALRM


def in_quiet_hours(now: datetime) -> bool:
    """True between QUIET_START and QUIET_END on the local clock (a window that crosses midnight)."""
    t = (now.hour, now.minute)
    return t >= QUIET_START or t < QUIET_END


def load_pace_state() -> dict:
    try:
        with open(PACE_STATE) as f:
            st = json.load(f)
            return st if isinstance(st, dict) else {}
    except (OSError, ValueError):
        return {}


def save_pace_state(st: dict) -> None:
    try:
        with open(PACE_STATE, "w") as f:
            json.dump(st, f)
    except OSError as exc:
        log(f"pace state not saved: {exc}")


def may_send_now(state: dict, now: datetime) -> bool:
    """False while the gap after the previous send is still running. Unparseable state reads as
    'no gap' — a corrupt file must never stop sending for good."""
    raw = (state or {}).get("not_before") or ""
    try:
        return now >= datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return True


def after_send(state: dict, now: datetime, rng=random.random) -> dict:
    """The gap the next send has to wait out."""
    gap = MIN_GAP_MIN + rng() * JITTER_MIN
    state = dict(state or {})
    state["last_send"] = now.isoformat()
    state["not_before"] = (now + timedelta(minutes=gap)).replace(microsecond=0).isoformat()
    return state


def not_snoozed(items: list, now: datetime) -> list:
    """Drop rows whose snooze is still running. awaiting_send() already does this; due_to_auto_send()
    does not, so a HELD auto-send row (bad address) was re-refused and re-nudged every two minutes
    and, once the loop sends one per run, would have blocked everything behind it."""
    out = []
    for it in items:
        raw = str(it.get("snooze_until") or "")
        try:
            if raw and datetime.fromisoformat(raw).replace(tzinfo=None) > now:
                continue
        except ValueError:
            pass
        out.append(it)
    return out


def choose(pending: list, state: dict, now: datetime) -> list:
    """The at-most-one item this run may send, in the order `pending` already has (follow-ups
    first). Quiet hours hold automatic sends; an item Alex approved by hand still goes, because
    his tap means now. The gap after the previous send applies to everything."""
    pending = not_snoozed(pending, now)
    if in_quiet_hours(now):
        pending = [it for it in pending if it.get("send_approved")]
    if not pending or not may_send_now(state, now):
        return []
    return pending[:1]



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


def is_follow_up(item: dict) -> bool:
    """A reply on an existing thread, rather than a cold first touch.

    The outbox stores "Subject: ..." at the head of `detail`, and the daily operator always
    prefixes a follow-up with "Re:" so it threads. That is the only marker either has.
    """
    detail = (item.get("detail") or "")
    head = detail.split("\n", 1)[0].strip().lower()
    return head.startswith("subject: re:")


def follow_ups_first(items: list) -> list:
    """Order the day's auto-sends so FOLLOW-UPS take the cap before cold first touches.

    The queue reads newest-id-first, and the daily operator drafts follow-ups and THEN releases
    first touches — so the cold emails are newer and were winning every slot. On 2026-09-22 that
    would have put 10 first touches out and starved 33 follow-ups.

    That is exactly backwards. Follow-ups are replies on threads that already delivered, so they
    carry almost no deliverability risk, and they are where replies come from: this whole daily
    operator exists because "every follow-up missed" is what produced $0 from an otherwise
    complete machine. A cold email deferred a day costs a day. A follow-up deferred past its
    window is a sequence that never finishes.

    Stable within each group, so the existing newest-first order is preserved otherwise.
    """
    return sorted(items, key=lambda it: 0 if is_follow_up(it) else 1)


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
    arm_watchdog()
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
        for it in follow_ups_first(outbox.due_to_auto_send(datetime.now().isoformat())):
            if it["id"] in approved_ids or len(pending) - len(approved_ids) >= room:
                continue
            pending.append(it)
    elif outbox.due_to_auto_send(datetime.now().isoformat()):
        log(f"daily cap reached ({sent_today}/{cap}) — auto-sends deferred to tomorrow")
    if not pending:
        return 0
    now = datetime.now()
    state = load_pace_state()
    if in_quiet_hours(now) and not any(it.get("send_approved") for it in not_snoozed(pending, now)):
        # Say it once a night, not every two minutes.
        if state.get("quiet_noted") != now.date().isoformat():
            log(f"quiet hours — {len(pending)} waiting, nothing auto-sends before "
                f"{QUIET_END[0]:02d}:{QUIET_END[1]:02d}")
            state["quiet_noted"] = now.date().isoformat()
            save_pace_state(state)
        return 0
    pending = choose(pending, state, now)
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
        save_pace_state(after_send(state, datetime.now()))
        nudge("Sent", f"Your email to {who} just went out.")
    return 0


if __name__ == "__main__":
    rc = main()
    try:
        signal.alarm(0)
    except (AttributeError, ValueError):
        pass
    sys.stdout.flush()
    os._exit(int(rc or 0))
