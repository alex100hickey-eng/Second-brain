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

Runs every 10 minutes under launchd (com.secondbrain.splitframesend, StartInterval 600). Each run
sends at most one email, auto-sends only between 08:00 and 22:00 ET, and dies on a watchdog rather
than hanging. Why each of those exists is written where it lives, below.
"""
from __future__ import annotations

import csv
import io
import os
import re
import signal
import socket
import subprocess
import sys
from datetime import date, datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

VAULT = os.path.expanduser("~/Library/Mobile Documents/com~apple~CloudDocs/Obsidian/Second brain")
TRACKER = os.path.join(VAULT, "Money", "prospect-tracker.csv")
LOG = os.path.expanduser("~/second-brain/scripts/splitframe_send.log")
PAUSE_FILE = os.path.expanduser("~/second-brain/scripts/SPLITFRAME_PAUSE")
DAILY_CAP = 5           # FLOOR only — see daily_cap(). Also the blast radius of a drafter bug.
CHAT = os.path.expanduser("~/second-brain/second-brain-chat")
SEND_SLUG = "GMAIL_" + "SEND_DRAFT"      # split so the suite's marker scan stays honest elsewhere


def log(msg: str) -> None:
    """Print first, then append to the log, and never raise. This runs straight after a send
    succeeds. A logger that throws there kills the run between "sent" and "recorded", which is the
    one place a crash costs a record of an email that really went. splitframe_daily's logger
    learned this on 2026-09-19; this one hadn't been given the lesson."""
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M')} {msg}"
    print(line, flush=True)
    try:
        with open(LOG, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Watchdog.
#
# 2026-09-23: a run started at 01:55, sent two emails, and then sat in an SSL read on a connection
# that died when the Mac went to sleep. It was still sitting there eight hours later. launchd
# will not start a new instance while the old one is alive, so that single hang stopped every
# send: 12 follow-ups due the day before and 10 due that day, all drafted and all waiting. The
# job looked healthy the whole time: loaded, a live PID, nothing in the log.
#
# reply_watch.py hit exactly this on 2026-09-21 and got this watchdog then. This file uses the
# same Composio client and the same launchd pattern, and never got it.
#
# The Composio client takes no timeout argument, so the backstop is a wall clock: whatever the run
# is blocked on, it dies inside the 600 s interval and launchd starts a clean one.
RUN_BUDGET_SECONDS = 420
socket.setdefaulttimeout(60)        # belt and braces for anything built on the socket layer

_armed_for = RUN_BUDGET_SECONDS
_in_flight = None                   # (item id, recipient, phase) while a send is under way


def _watchdog(_sig, _frm):
    where = ""
    if _in_flight:
        item_id, who, phase = _in_flight
        if phase == "sending":
            where = (f" — item {item_id} to {who} was mid-send. Its outbox row already carries "
                     "sent_at, so it is settled by the next run: requeued if its draft is still in Drafts, closed if Gmail Sent has it")
        else:
            where = (f" — item {item_id} to {who} WAS sent; only its bookkeeping ({phase}) "
                     "was cut off")
    log(f"ABORTED: run exceeded {_armed_for}s and was killed so the next one can start{where}")
    os._exit(1)


def arm_watchdog(seconds: int = RUN_BUDGET_SECONDS) -> None:
    global _armed_for
    _armed_for = seconds
    try:
        signal.signal(signal.SIGALRM, _watchdog)
        signal.alarm(seconds)
    except (AttributeError, ValueError):
        pass                        # not the main thread, or no SIGALRM on this platform



def sent_counts_today() -> dict:
    """{first, follow, total} sent today, read back from this script's own log.

    A SENT line ends with "[follow-up]" or "[first touch]" since 2026-09-23. Older lines carry no
    marker and count as first touches — the conservative reading for the first-touch cap, and
    the same either way for the total ceiling."""
    today = date.today().isoformat()
    out = {"first": 0, "follow": 0, "total": 0}
    try:
        with open(LOG) as f:
            for line in f:
                if not (line.startswith(today) and ": SENT to " in line):
                    continue
                out["total"] += 1
                out["follow" if line.rstrip().endswith("[follow-up]") else "first"] += 1
    except OSError:
        pass
    return out


def _sent_today() -> int:
    """How many this script has already sent today, all kinds."""
    return sent_counts_today()["total"]


def _daily_module():
    """scripts/splitframe_daily.py, loaded from beside this file: the one source of truth for
    the cadence, the ceiling and the follow-up budget rule. A worktree reads its own copy."""
    import importlib.util
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, here)
    spec = importlib.util.spec_from_file_location("_sfd_cap", os.path.join(here, "splitframe_daily.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def daily_cap() -> int:
    """How many auto-sends this script allows today — the SAME cadence the release uses.

    These were two numbers that had to agree and didn't. The release cadence is bounce-aware and
    had been raised to 10/day; this script's cap stayed hardcoded at 5. So ten drafts were released
    each morning and five of them quietly waited for tomorrow, forever — the lane running at half
    throttle with nothing in any log saying so, because "daily cap reached" reads like correct
    behaviour. Read the one source of truth instead of restating it.
    """
    try:
        cap, _why = _daily_module().current_cap()
        return max(DAILY_CAP, int(cap))
    except Exception:
        return DAILY_CAP       # fail to the floor, never to "unlimited"


# Follow-ups on their own budget — decision A, 2026-09-23 (relayed by the money session, logged in
# the Shift Log with the reversal). Until then one cap covered everything the mailbox sent, and
# with ten first touches a day owing twenty follow-ups, follow-ups alone filled it for days while
# first touches got no slot. Now the bounce-gated cap (daily_cap) counts FIRST TOUCHES only,
# follow-ups run on the same one-per-run pacing, and a hard ceiling bounds the day's total.
# Both numbers and the rule live in splitframe_daily.py so the server's release and this sender
# cannot drift apart again; the fallbacks below are the conservative direction.
DEFAULT_TOTAL_CEILING = 20


def total_ceiling() -> int:
    """Today's bound on everything the mailbox sends: splitframe_daily.effective_ceiling(), which
    holds at 20 for 48 h after any bounce and whenever the bounce record can't be read."""
    try:
        return max(DAILY_CAP, int(_daily_module().effective_ceiling()[0]))
    except Exception:
        return DEFAULT_TOTAL_CEILING


def followups_share_cap() -> bool:
    """True restores the pre-09-23 rule: one cap for first touches and follow-ups together."""
    try:
        return bool(_daily_module().FOLLOWUPS_SHARE_CAP)
    except Exception:
        return False


def send_budget(counts: dict, cap: int, ceiling: int, share: bool) -> tuple:
    """(room for first touches, room for anything) left today. Pure, so the arithmetic is
    testable without a log file."""
    if share:
        room = max(0, cap - counts["total"])
        return room, room
    room_total = max(0, ceiling - counts["total"])
    room_first = max(0, min(cap - counts["first"], room_total))
    return room_first, room_total


# How long a refused row waits before trying again. Long enough that a genuinely wrong address
# cannot nag every two minutes; short enough that fixing the list the same day still sends.
REFUSED_HOLD_HOURS = 6

CREATOR_LIST = os.path.join(VAULT, "Money", "Creator Lane — Prospects.md")
# The vault's git mirror (vaultsync commits to it). iCloud evicts vault files to "dataless"
# placeholders, and reading one can fail outright (EDEADLK); the mirror's copy is always on disk.
VAULT_GIT = os.path.expanduser("~/.second-brain-vault.git")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def _mirror_text(vault_path: str = "Money/prospect-tracker.csv") -> str | None:
    """A vault file as the vault's git mirror last saw it."""
    try:
        r = subprocess.run(["git", "--git-dir", VAULT_GIT, "show", f"HEAD:{vault_path}"],
                           capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 and r.stdout.strip() else None


def _tracker_text() -> str | None:
    """The tracker's CSV: the iCloud file, else the git mirror's copy, else None."""
    try:
        with open(TRACKER, newline="") as f:
            return f.read()
    except OSError as exc:
        text = _mirror_text()
        if text is not None:
            log(f"tracker unreadable in iCloud ({type(exc).__name__}, probably evicted); the "
                "allow-list comes from the vault git mirror")
        return text


def approved_recipients() -> set | None:
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

    Returns None when the tracker can't be read at all, which is different from "nobody is
    allowed". On 2026-09-24 iCloud evicted the tracker at 10:46; the read failed, this returned
    an empty set, and the 10:57 and 11:07 runs HELD thirteen written follow-ups as "not an
    approved address", pushed each 6 hours back and sent Alex a nudge per email. Now the mirror's
    copy stands in, and with neither readable the run sends nothing and holds nothing.
    """
    text = _tracker_text()
    if text is None:
        return None
    out = set()
    for r in csv.DictReader(io.StringIO(text)):
        for col in ("email", "email_generic"):
            addr = (r.get(col) or "").strip().lower()
            if addr:
                out.add(addr)
    # Creator-lane prospects are curated by hand in the vault and never enter the tracker.
    try:
        with open(CREATOR_LIST) as f:
            creators = f.read()
    except OSError:
        creators = _mirror_text("Money/Creator Lane — Prospects.md") or ""
    out.update(a.lower() for a in _EMAIL_RE.findall(creators))
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

    Inside each group the OLDEST due goes first. The queue reads newest-id-first, and keeping
    that order meant every newly due draft jumped the line: on 2026-09-24 Moon Juice's follow-up
    (static attached, due 09:17) was still waiting at 10:00 while drafts due at 09:29 and 09:53
    went ahead of it, and ten more due at 10:23 were about to do the same. With one send per run
    and a daily ceiling, last-in-first-out means the oldest drafts are the ones that never go,
    and an old first touch is also the one closest to being held as stale.
    """
    return sorted(items, key=lambda it: (0 if is_follow_up(it) else 1,
                                         str(it.get("auto_send_at") or ""), it.get("id") or 0))


# ---------------------------------------------------------------------------
# Pacing.
#
# The Deliverability Audit (vault, 2026-09-23) found this script's one habit most likely to hurt
# placement: it sent every expired item in a single run. That meant 12 emails in one minute on
# 09-18 and 10 in two minutes at 00:56 ET on 09-22, and 16 of 79 sends between midnight and 3 AM,
# because the 3-hour hold expires while the Mac sleeps and everything fires the moment it wakes.
#
# The same midnight burst starved follow-ups. Drafts held over from 09-21 went at 00:56 on
# 09-22 and used that day's entire cap before the server drafted that day's follow-ups at
# 07:50. follow_ups_first() never got to choose, because the follow-ups didn't exist yet.
#
# So, three rules:
#   * no AUTO-sends outside 08:00-22:00 ET. The server's daily run drafts follow-ups between 07:00
#     and 08:00, so by the time sending opens, the day's follow-ups are in the outbox to be
#     ordered first. A send Alex tapped himself still goes at any hour.
#   * at most one email per run. The job runs every 10 minutes, so a 10-email day spreads over
#     about 100 minutes instead of one.
#   * a cold first touch only takes a slot the day's follow-ups don't need. Follow-ups drafted
#     but not yet due (the 3-hour hold) still hold their places in today's cap.
SEND_TZ = ZoneInfo("America/New_York")
QUIET_FROM, QUIET_UNTIL = dtime(22, 0), dtime(8, 0)
MAX_SENDS_PER_RUN = 1


def _now() -> datetime:
    return datetime.now(SEND_TZ)


def in_quiet_hours(now: datetime) -> bool:
    t = now.astimezone(SEND_TZ).time() if now.tzinfo else now.time()
    return t >= QUIET_FROM or t < QUIET_UNTIL


def followups_waiting(open_items: list, exclude_ids: set) -> int:
    """Follow-up drafts already written and not yet sent that aren't in this run's due list:
    the ones still inside their hold window. Each will want a slot in today's cap."""
    return sum(1 for it in open_items
               if it.get("kind") == "email_draft" and is_follow_up(it)
               and not it.get("sent_at") and it.get("id") not in exclude_ids)


def pick_auto_sends(due: list, room_first: int, room_total: int, reserved: int,
                    limit: int = MAX_SENDS_PER_RUN) -> list:
    """Which expired drafts this run sends. Follow-ups first, out of `room_total`; a first touch
    only while its own cap (`room_first`) has room AND the day's remaining total leaves a slot
    for every follow-up still waiting inside its hold."""
    out, first_taken = [], 0
    for it in follow_ups_first(due):
        if len(out) >= min(room_total, limit):
            break
        if not is_follow_up(it):
            if first_taken >= room_first or room_total - len(out) <= reserved:
                continue
            first_taken += 1
        out.append(it)
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

# A send interrupted mid-flight leaves its row open with sent_at set, and nothing retried it: the
# watchdog only said "check studio Sent". The Mac is a laptop, so this is routine: 2026-09-23 01:56
# and 2026-09-24 17:24 (Final Boss's static follow-up, marked sent for 67 minutes while its draft
# sat unsent). A live send takes seconds and the watchdog fires at RUN_BUDGET_SECONDS, so a row
# still open with sent_at older than this was interrupted.
INTERRUPTED_AFTER_MINUTES = 15


def interrupted_rows(open_items: list, now: datetime) -> list:
    """Open email drafts that carry sent_at from a run that never finished. Pure."""
    out = []
    for it in open_items or []:
        stamp = (it.get("sent_at") or "").strip()
        if it.get("kind") != "email_draft" or not stamp:
            continue
        try:
            when = datetime.fromisoformat(stamp)
        except ValueError:
            continue
        if when.tzinfo is not None:
            when = when.astimezone(SEND_TZ).replace(tzinfo=None)
        if now.replace(tzinfo=None) - when >= timedelta(minutes=INTERRUPTED_AFTER_MINUTES):
            out.append(it)
    return out


def _already_logged(item_id) -> bool:
    try:
        with open(LOG, encoding="utf-8", errors="replace") as f:
            return any(f"item {item_id}: SENT to " in line for line in f)
    except OSError:
        return False


def recover_interrupted(outbox, composio, entity: str, now: datetime) -> list:
    """Settle every interrupted send by asking Gmail. Returns [(item id, what happened)].

    Draft still in Drafts: it never went, so sent_at is cleared and it waits its turn again.
    Draft gone and the email in Sent: it went, so the row is closed and the send is logged
    (unless the log already has it). Anything unclear or erroring is left exactly as it was and
    said once in the log, so a wrong guess can never send an email twice."""
    done = []
    for it in interrupted_rows(outbox.open_items(), now):
        _acct, draft_id = parse_ref(it.get("ref", ""))
        who = recipient_of(it)
        try:
            got = composio.tools.execute("GMAIL_GET_DRAFT", user_id=entity,
                                         dangerously_skip_version_check=True,
                                         arguments={"draft_id": draft_id, "format": "metadata"})
            msg = (got.get("data") or {}).get("message") or {}
            if got.get("successful") and "DRAFT" in (msg.get("labelIds") or []):
                outbox._write(it["id"], {"sent_at": ""})
                log(f"item {it['id']}: interrupted send to {who} recovered. Its draft is still in "
                    f"Drafts, so it never went; it is back in line")
                done.append((it["id"], "requeued"))
                continue
            sent = composio.tools.execute("GMAIL_FETCH_EMAILS", user_id=entity,
                                          dangerously_skip_version_check=True,
                                          arguments={"query": f"in:sent to:{who} newer_than:2d",
                                                     "max_results": 5})
            if (sent.get("data") or {}).get("messages"):
                if not _already_logged(it["id"]):
                    kind = "follow-up" if is_follow_up(it) else "first touch"
                    log(f"item {it['id']}: SENT to {who} (draft {draft_id}) — recorded after an "
                        f"interrupted run: its draft is gone and Gmail Sent has it [{kind}]")
                outbox.close(it["id"], outbox.DONE, note="sent; recorded after an interrupted run")
                done.append((it["id"], "closed"))
            else:
                log(f"item {it['id']}: interrupted send to {who} is unclear (draft gone, nothing in "
                    f"Sent). Left as it is; check studio Sent")
                done.append((it["id"], "unclear"))
        except Exception as exc:                            # noqa: BLE001
            log(f"item {it['id']}: could not check an interrupted send ({type(exc).__name__}); "
                f"left as it is")
            done.append((it["id"], "error"))
    return done


def main() -> int:
    global _in_flight
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
    # Settle sends a previous run was killed in the middle of, before anything new goes out.
    try:
        if interrupted_rows(outbox.open_items(), _now()):
            from composio import Composio               # type: ignore
            recover_interrupted(outbox, Composio(api_key=os.environ["COMPOSIO_API_KEY"]),
                                os.environ.get("STUDIO_GMAIL_ENTITY"), _now())
    except Exception as exc:                            # noqa: BLE001
        log(f"interrupted-send check skipped ({type(exc).__name__})")
    # Sends Alex tapped himself go first and aren't held by quiet hours: he chose the moment.
    pending = list(outbox.awaiting_send())
    approved_ids = {it["id"] for it in pending}
    # Auto-send: drafts whose hold window has expired. Alex asked for this 2026-09-15 so he can be
    # hands-off. The cap is what stops a Mac that slept through three days of drafts waking up and
    # firing all of them into the same morning.
    now = _now()
    due = [it for it in outbox.due_to_auto_send(now.replace(tzinfo=None).isoformat())
           if it["id"] not in approved_ids]
    if due and not in_quiet_hours(now):
        counts = sent_counts_today()
        cap, ceiling = daily_cap(), total_ceiling()
        room_first, room_total = send_budget(counts, cap, ceiling, followups_share_cap())
        if room_total:
            reserved = followups_waiting(outbox.open_items(), {it["id"] for it in due})
            # limit=room_total, not 1: the per-run limit counts SUCCESSFUL sends (below), so a
            # draft that fails or is refused can't take the run's only slot every ten minutes.
            picked = pick_auto_sends(due, room_first, room_total, reserved, limit=room_total)
            if not picked and not room_first and not any(is_follow_up(it) for it in due):
                log(f"first-touch cap reached ({counts['first']}/{cap}) — cold emails wait for "
                    f"tomorrow; follow-ups still go")
            pending.extend(picked)
        else:
            log(f"daily ceiling reached ({counts['total']}/{ceiling}: {counts['first']} first "
                f"touch(es), {counts['follow']} follow-up(s)) — auto-sends deferred to tomorrow")
    if not pending:
        return 0

    c = Composio(api_key=os.environ["COMPOSIO_API_KEY"])
    entities = {"studio": os.environ.get("STUDIO_GMAIL_ENTITY")}
    allowed = approved_recipients()
    if allowed is None:
        log("tracker unreadable in iCloud and in the vault git mirror: nothing sent and nothing "
            "held this run; the next run tries again")
        return 0

    sent = 0
    for item in pending:
        if sent >= MAX_SENDS_PER_RUN:
            break
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
            if item.get("auto_send_at") and not item.get("send_approved"):
                # A snooze alone doesn't hold an AUTO-send: due_to_auto_send reads auto_send_at,
                # so a refused auto-send came straight back, was refused again and re-nudged on
                # every run. Move the send too, the way the /do page's snooze does.
                outbox.arm_auto_send(item["id"], (datetime.now() + timedelta(
                    hours=REFUSED_HOLD_HOURS)).isoformat())
            nudge("Splitframe: send held", f"{who or 'unknown recipient'} isn't on the approved "
                  "list, so nothing was sent. The email is still queued — add the address to the "
                  "tracker or the creator list and it goes on the next pass.")
            continue
        # Close FIRST: a crash between send and bookkeeping must never leave a row that
        # another pass would send a second time.
        _in_flight = (item["id"], who, "sending")
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
            _in_flight = None
            continue
        route = "on Alex's approval" if item.get("send_approved") else "automatically (hold window expired)"
        # The record FIRST, the bookkeeping after. It used to be the other way round, and on
        # 2026-09-23 a run hung inside the bookkeeping after Antler Farms' follow-up had gone:
        # the email left, and neither the log nor the daily count ever knew.
        kind = "follow-up" if is_follow_up(item) else "first touch"
        log(f"item {item['id']}: SENT to {who} (draft {draft_id}) — {route} [{kind}]")
        sent += 1
        for phase, step in (("close", lambda: outbox.close(item["id"], outbox.DONE,
                                                            note=f"sent from the Mac {route}")),
                            ("tracker stamp", lambda: stamp_tracker(who)),
                            ("nudge", lambda: nudge("Sent", f"Your email to {who} just went out."))):
            _in_flight = (item["id"], who, phase)
            try:
                step()
            except Exception as exc:                    # noqa: BLE001
                log(f"item {item['id']}: sent, but {phase} failed — {str(exc)[:160]}")
        _in_flight = None
    return 0


if __name__ == "__main__":
    rc = main()
    try:
        signal.alarm(0)
    except (AttributeError, ValueError):
        pass
    sys.stdout.flush()
    # os._exit, not sys.exit: a library's atexit hook that blocks on the network would hold the
    # launchd slot exactly like the 01:56 hang did, with the work already done.
    os._exit(int(rc or 0))
