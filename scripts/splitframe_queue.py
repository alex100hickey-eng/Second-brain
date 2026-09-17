#!/usr/bin/env python3
"""Splitframe first-touch queue — the one guarded way the nightly money shift feeds the funnel.

The server releases up to five queued first touches a day (scripts/splitframe_daily.py,
release_first_touches) and arms each one to send itself three hours later unless Alex taps
"Not doing it". Until now the queue was fed by hand inside chat sessions, which is why the
funnel ran dry every time nobody opened one. The scheduled Claude Code task `money-shift`
runs this every evening instead.

  status [--json]   pending / released-today counts, days of runway, who can be drafted next
                    (qualified, real person, never emailed, not yet queued), what to qualify
                    next, and what the next Hunter window should look up
  add               validate a drafted first touch, create the studio Gmail draft WITHOUT filing
                    an outbox row (the server's cadence, not this script, decides when it goes),
                    append it to the queue, stamp the live ad count into the tracker
  creator           the same, for the creator-retainer lane: the address must be in the creator
                    prospect list, not marked UNVERIFIED there, and the offer must be approved
  note              stamp a live Ad Library read into the tracker: 0 active -> hold,
                    5-50 active on a `candidate` row -> qualified
  source            add a brand the tracker never had, from a live Ad Library read: the
                    keyword search is the only supply left now that the 97 rows are mined out

Mac only: it writes the iCloud tracker and the Mac is the only vault writer.
Drafts only. There is no send path here and none may be added — pinned by
test_splitframe_queue.test_no_send_capability.
"""
from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import os
import re
import shutil
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHAT = os.path.join(ROOT, "second-brain-chat")
sys.path.insert(0, CHAT)
try:                                   # launchd and scheduled tasks hand us a bare env
    from dotenv import load_dotenv     # type: ignore
    load_dotenv(os.path.join(ROOT, ".env"))
except Exception:
    pass

LOCAL_TZ = ZoneInfo("America/New_York")
# The Mac reads and WRITES the iCloud vault; the server (money_operator) only READS its git-synced
# copy at VAULT_PATH to size the funnel. Same rule as splitframe_daily.py.
VAULT = os.environ.get("VAULT_PATH") or os.path.expanduser(
    "~/Library/Mobile Documents/com~apple~CloudDocs/Obsidian/Second brain")
TRACKER = os.path.join(VAULT, "Money", "prospect-tracker.csv")
DRAFT_DOC_DIR = os.path.join(VAULT, "Money", "Clients")
CREATOR_PROSPECTS = os.path.join(VAULT, "Money", "Creator Lane — Prospects.md")
CREATOR_OFFER = os.path.join(VAULT, "Money", "Creator Lane — Offer (approved).md")
QUEUE_KEY = "splitframe:firsttouch_queue"
PER_DAY = 5
RUNWAY_TARGET = 2 * PER_DAY            # two release days in stock at every evening shift
MIN_WORDS, MAX_WORDS = 80, 180         # the skill targets 110-150; past 170 is padding
IN_BAND = (5, 50)                      # the sweet spot: enough ads to have a problem, no in-house team
TOO_BIG = 100                          # 100+ active ads means an in-house team — out of band
QUALIFY_BATCH = 8

# Reuse the daily job's guards instead of copying them: a copied guard is one that silently
# drifts, and this one decides whether a founder gets an email with an invented claim in it.
_SPEC = importlib.util.spec_from_file_location(
    "splitframe_daily", os.path.join(ROOT, "scripts", "splitframe_daily.py"))
_sfd = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_sfd)
fabrication_risk = _sfd.fabrication_risk
SKIP_ADDRESSES = _sfd.SKIP_ADDRESSES
_released_date = _sfd._released_date
# Who an address belongs to lives in the daily job, because the daily job needs it too: it picks
# follow-up targets, and a second copy here would be the copy that drifts.
is_person = _sfd.is_person
is_front_desk = _sfd.is_front_desk
is_ticket_desk = _sfd.is_ticket_desk
matches_contact = _sfd.matches_contact
target_address = _sfd.target_address
FRONT_LOCALS, TICKET_LOCALS, FIRST_NAMES = _sfd.FRONT_LOCALS, _sfd.TICKET_LOCALS, _sfd.FIRST_NAMES

COUNT_RE = re.compile(r"adlib (\d+) active(?: \[read live (\d{4}-\d{2}-\d{2})\])?")
PAGE_ID_RE = re.compile(r"\bid=(\d{6,})")


def today_local() -> str:
    return datetime.now(LOCAL_TZ).date().isoformat()


def _c(value) -> str:
    return (value or "").strip()


# ---------------------------------------------------------------- pure: reading the tracker

def known_count(row: dict):
    """(active ad count, date it was read live or None) from the notes column. The newest
    stamp is always written first, so the first match is the freshest."""
    m = COUNT_RE.search(row.get("notes") or "")
    if not m:
        return None, None
    return int(m.group(1)), m.group(2)


def page_id(row: dict) -> str:
    """The Ad Library page id recorded in notes ("page=Obvi id=2431731276838642"). The
    keyword-search adlib_url is junk for several brands; the page id view is the one to read."""
    m = PAGE_ID_RE.search(row.get("notes") or "")
    return m.group(1) if m else ""


def band(count) -> str:
    if count is None:
        return "unknown"
    if count == 0:
        return "zero"
    if IN_BAND[0] <= count <= IN_BAND[1]:
        return "in"
    if count > TOO_BIG:
        return "big"
    return "out"


def pending_entries(queue: list) -> list:
    return [e for e in queue if _released_date(e) is None]


def released_on(queue: list, day) -> list:
    return [e for e in queue if _released_date(e) == day]


def next_targets(rows: list, queue: list) -> list:
    """Who a first touch can be written for: qualified, real person, never emailed, no reply
    or outcome, not already in the queue (released or not — a released entry is in the
    outbox or already sent). In-band brands first, then ones never read live, then the
    51-100 borderline. 0-ad brands are holds and 100+ are skipped outright."""
    queued = {_c(e.get("to")).lower() for e in queue}
    out = []
    for r in rows:
        email, tier = target_address(r)
        if _c(r.get("status")) != "qualified" or not email:
            continue
        if _c(r.get("sent_date")) or _c(r.get("replied")) or _c(r.get("outcome")):
            continue
        if email in queued:
            continue
        n, when = known_count(r)
        b = band(n)
        if b in ("zero", "big"):
            continue
        out.append({"brand": _c(r.get("brand")), "to": email, "tier": tier,
                    "contact": _c(r.get("contact_name")), "page_id": page_id(r),
                    "adlib_url": _c(r.get("adlib_url")), "known_count": n,
                    "count_read_live": when, "band": b})
    # A named founder outranks a front desk at a better-fitting brand: who reads it moves the
    # reply rate more than five ads either way does.
    tier_order = {"person": 0, "shared": 1}
    order = {"in": 0, "unknown": 1, "out": 2}
    out.sort(key=lambda t: (tier_order[t["tier"]], order[t["band"]], t["brand"].lower()))
    return out


def candidates_to_qualify(rows: list, today: str, limit: int = QUALIFY_BATCH) -> list:
    """`candidate` rows worth an Ad Library read: they have a page to read and no live count
    from the last 30 days. Reading them is what turns the next Hunter window into sends."""
    out = []
    for r in rows:
        if _c(r.get("status")) != "candidate":
            continue
        pid = page_id(r)
        if not pid and not _c(r.get("adlib_url")):
            continue
        n, when = known_count(r)
        if when and (datetime.fromisoformat(today) - datetime.fromisoformat(when)).days < 30:
            continue
        out.append({"brand": _c(r.get("brand")), "page_id": pid,
                    "adlib_url": _c(r.get("adlib_url")), "known_count": n})
    return out[:limit]


def hunter_targets(rows: list) -> list:
    """Qualified, in-band, and no real person's address yet — the next Hunter searches."""
    out = []
    for r in rows:
        if _c(r.get("status")) != "qualified" or is_person(r.get("email")):
            continue
        n, _ = known_count(r)
        if band(n) in ("zero", "big"):
            continue
        out.append({"brand": _c(r.get("brand")), "domain": _c(r.get("domain")),
                    "known_count": n, "has_generic": bool(_c(r.get("email")))})
    return out


# ---------------------------------------------------------------- pure: guarding a draft

# ---------------------------------------------------------------- the close, as an experiment

# Eighteen sent, zero replies. That is not evidence the email is bad — at a normal cold-email
# reply rate of 1-5%, eighteen sends expects well under one reply, so 0 is the likeliest single
# outcome even for a campaign that works. It is evidence of nothing at all, which is the problem:
# the funnel is about to send forty more and would learn just as little from those.
#
# So the one thing every email has in common gets split. The body, the voice and the observation
# are untouched — they are the part there is no reason to doubt. Only the last line changes:
#
#   question  what has always been sent. Names the service, then asks something real about their
#             business. Low pressure, and the reply it invites is about them, not about hiring.
#   offer     names the service, then offers to make the specific test the email just described,
#             free. Removes the decision — the founder is agreeing to see work, not to buy it.
#
# Assignment alternates on what is already in the queue rather than at random, because at this
# sample size a coin flip can hand one arm twelve of twenty and the result reads as a difference.
CLOSE_VARIANTS = ("question", "offer")


def next_close_variant(queue: list) -> str:
    """Whichever arm has fewer emails IN THE EXPERIMENT behind it.

    Only entries carrying an explicit variant count. The eighteen sent before the split were all
    the question close, and counting them would have the arms balance against history instead of
    against each other: the offer arm would take every email until it caught up to eighteen.
    That is not a slower experiment, it is a different and worse one — the offer arm would get
    this week's list (front desks, sourced today) while the question arm's record is last week's
    (named founders), and the close would be confounded with who was written to. The arms have to
    run side by side over the same list to be comparable at all.

    close_report still credits those eighteen to question; that is the honest account of what was
    sent. It is only the assignment that ignores them.
    """
    counts = {v: sum(1 for e in queue if _c(e.get("close_variant")) == v) for v in CLOSE_VARIANTS}
    return min(CLOSE_VARIANTS, key=lambda v: (counts[v], CLOSE_VARIANTS.index(v)))


def close_report(rows: list, queue: list) -> dict:
    """Sent and replied per arm. Reads the tracker for outcomes and the queue for assignment, so
    it stays right even for rows queued before the column existed (those are 'question')."""
    by_addr = {}
    for e in queue:
        addr = _c(e.get("to")).lower()
        if addr:
            by_addr[addr] = _c(e.get("close_variant")) or "question"
    out = {v: {"sent": 0, "replied": 0} for v in CLOSE_VARIANTS}
    for r in rows:
        if not _c(r.get("sent_date")):
            continue
        addr = _c(r.get("email")).lower() or _c(r.get("email_generic")).lower()
        arm = _c(r.get("close_variant")) or by_addr.get(addr) or "question"
        if arm not in out:
            continue
        out[arm]["sent"] += 1
        if _c(r.get("replied")):
            out[arm]["replied"] += 1
    return out


def guard_body(body: str) -> list:
    """Every reason a first-touch body must not go out. The whole pitch rests on the email
    reading as a person who actually opened the account, and on every claim being true."""
    problems = []
    words = len((body or "").split())
    if words < MIN_WORDS:
        problems.append(f"too short ({words} words; a first touch is 110-150)")
    if words > MAX_WORDS:
        problems.append(f"too long ({words} words; past 170 is padding)")
    risky = fabrication_risk(body or "")
    if risky:
        problems.append("claims work not done: " + ", ".join(risky))
    dashes = (body or "").count("—")
    if dashes > 1:
        problems.append(f"{dashes} em dashes (one at most; it is the loudest AI tell)")
    if re.search(r"\bA\.?I\.?\b", body or ""):
        problems.append('the word "AI" appears (it appears in no client-facing artifact)')
    if re.search(r"looking forward to hearing", (body or "").lower()):
        problems.append("sign-off flourish (he ends flat)")
    return problems


IMAGE_URL_RE = re.compile(r"https?://[^\s\"'<>]+\.(?:jpg|jpeg|png|webp)(?:\?[^\s\"'<>]*)?", re.I)


def offer_image_problems(url: str, domain: str) -> list:
    """Why a promised spec ad could not be built from this photo.

    The offer close makes a claim the fabrication guard cannot see, because it is about the
    FUTURE: "I'll build you X." On 2026-09-17 the first batch promised Antler Farms a static
    built from imagery of their free-grazing herds. They publish product bottles and a logo.
    That ad could only have been made by inventing a farm.

    So the offer arm has to name the photo it would use, on the brand's own domain, and it gets
    stored with the queue entry — which also means that when someone says yes, the image is
    already chosen instead of being hunted for under a same-day promise.
    """
    url = _c(url)
    if not url:
        return ["--offer-image is required with --close offer: name the photo on their own site "
                "you would actually build the ad from, or the promise is one nobody can keep"]
    if not IMAGE_URL_RE.fullmatch(url):
        return [f"--offer-image {url[:60]!r} is not an image URL (jpg/png/webp)"]
    host = re.sub(r"^https?://", "", url).split("/")[0].lower().removeprefix("www.")
    base = _c(domain).lower().removeprefix("www.")
    if not base:
        return []                                  # no domain on the row to check against
    root = ".".join(base.split(".")[-2:])
    if root not in host:
        return [f"--offer-image is on {host}, not {base} — a spec ad uses the brand's OWN photo, "
                f"never one found elsewhere"]
    return []


def plan_add(rows: list, queue: list, to: str, subject: str, body: str,
             ad_count, brand: str = "", close: str = "", offer_image: str = ""):
    """(tracker row, problems). An empty problems list is the only permission to queue."""
    to = _c(to).lower()
    row = next((r for r in rows
                if to in {_c(r.get("email")).lower(), _c(r.get("email_generic")).lower()} and to),
               None)
    if row is None:
        return None, [f"{to or '(empty)'} is not in the tracker — the sender only ever sends "
                      "to a tracker-verified address, so this could never go out"]
    problems = []
    if not (is_person(to) or is_front_desk(to)):
        problems.append("ticket queue — a first touch about ad creative dies in a support inbox")
    if _c(row.get("status")) != "qualified":
        problems.append(f"row status is {row.get('status')!r}, not qualified")
    if _c(row.get("sent_date")):
        problems.append(f"already emailed on {row['sent_date']}")
    if _c(row.get("replied")) or _c(row.get("outcome")):
        problems.append("this brand already replied or has an outcome — leave them alone")
    if brand and _c(brand).lower() != _c(row.get("brand")).lower():
        problems.append(f"--brand {brand!r} does not match the tracker row ({row.get('brand')!r})")
    if any(_c(e.get("to")).lower() == to for e in queue):
        problems.append("already in the first-touch queue")
    if not _c(subject):
        problems.append("no subject")
    if ad_count is None:
        problems.append("--ad-count is required: the live count read tonight is the proof "
                        "the account was actually opened before writing")
    elif ad_count == 0:
        problems.append("0 active ads — a brand running no ads is not buying creative "
                        "(stamp it with `note` instead)")
    elif ad_count > TOO_BIG:
        problems.append(f"{ad_count} active ads means an in-house team — out of band, "
                        "don't burn the funnel on it")
    if close == "offer":
        problems += offer_image_problems(offer_image, _c(row.get("domain")))
    problems += guard_body(body)
    return row, problems


def stamp_count(row: dict, count: int, today: str):
    """The tracker changes a live read implies. Returns (notes, status, changes)."""
    notes = _c(row.get("notes"))
    stamp = f"adlib {count} active [read live {today}]"
    changes = []
    if not notes.startswith(stamp):
        notes = f"{stamp} · {notes}" if notes else stamp
        changes.append(f"noted {count} active")
    status = _c(row.get("status"))
    if count == 0 and status != "hold" and not _c(row.get("sent_date")):
        status = "hold"
        changes.append("status -> hold (no ads running)")
    elif IN_BAND[0] <= count <= IN_BAND[1] and status == "candidate":
        status = "qualified"
        changes.append("status -> qualified (in the 5-50 band)")
    return notes, status, changes


PAGE_URL = ("https://www.facebook.com/ads/library/?active_status=active&ad_type=all"
            "&country=US&view_all_page_id={pid}")
KEYWORD_URL = ("https://www.facebook.com/ads/library/?active_status=active&ad_type=all"
               "&country=US&q={q}&search_type=keyword_unordered")


def clean_domain(domain: str) -> str:
    d = _c(domain).lower()
    d = re.sub(r"^https?://", "", d).split("/")[0]
    return d[4:] if d.startswith("www.") else d


def status_for_count(count: int) -> str:
    """Where a brand lands the first time anyone reads its Ad Library page. Mirrors `band`,
    in the tracker's own status vocabulary."""
    if count == 0:
        return "hold"
    if count < IN_BAND[0]:
        return "too_small"
    if count > TOO_BIG:
        return "too_big"
    return "qualified"


def plan_source(rows: list, brand: str, domain: str, count: int, today: str,
                pid: str = "", category: str = "", evidence: str = ""):
    """A brand nobody had, read live tonight, as a tracker row. Returns (row, problems).

    The 97-row tracker was mined out on 2026-09-16: every undrafted prospect left was a
    96-150 ad in-house-team brand and every `candidate` row sampled returned zero active ads.
    The funnel needs new names, and the Ad Library keyword search is the one source where
    every advertiser it returns is currently spending — which is the qualification test
    itself. This is the guarded way those names land in the tracker: a live count, a page id,
    and the same status rules a `note` would apply, so nothing enters already qualified on a
    number nobody read."""
    problems = []
    brand = _c(brand)
    domain = clean_domain(domain)
    if not brand:
        problems.append("no brand")
    if not domain:
        problems.append("no domain (the Hunter window looks a person up by domain)")
    if count is None or count < 0:
        problems.append("no live ad count — source a brand by reading its page, never from a list")
    for r in rows:
        if _c(r.get("brand")).lower() == brand.lower():
            problems.append(f"{brand} is already in the tracker (status {_c(r.get('status')) or '?'})")
            break
        if domain and clean_domain(r.get("domain")) == domain:
            problems.append(f"{domain} is already in the tracker as {_c(r.get('brand'))}")
            break
    if problems:
        return None, problems
    notes = f"adlib {count} active [read live {today}] · sourced {today} ad library"
    if pid:
        notes += f" · page={brand} id={pid}"
    if evidence:
        notes += f" · {evidence}"
    return {"brand": brand, "domain": domain, "category": _c(category),
            "adlib_url": PAGE_URL.format(pid=pid) if pid else KEYWORD_URL.format(q=brand.replace(" ", "%20")),
            "meta_page_guess": brand, "email": "", "status": status_for_count(count),
            "notes": notes}, []


# ---------------------------------------------------------------- IO: tracker, queue, Gmail

def tracker_rows():
    with open(TRACKER, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return list(reader), reader.fieldnames


def write_tracker(rows: list, fieldnames: list, tag: str) -> str:
    """Backup once per tag per day, then an atomic replace — a crash never truncates it.
    Default csv line endings (CRLF) match the file as it stands."""
    bak = f"{TRACKER}.bak-{tag}-{today_local()}"
    if not os.path.exists(bak):
        shutil.copy2(TRACKER, bak)
    tmp = TRACKER + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, TRACKER)
    return bak


def _intake():
    import intake                                  # type: ignore
    if intake.supabase is None:
        from supabase import create_client         # type: ignore
        intake.supabase = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
    return intake


BOUNCE_KEY = "splitframe:bounces"


def recent_bounces(days: int = 14) -> list:
    """Delivery failures in the last `days`, newest first. The server records these (a bounce is
    invisible to reply detection — it comes from mailer-daemon at one of our own domains), and
    this is where the decision they inform gets made: whether the daily cap can go up, and
    whether the newest addresses in the tracker are any good."""
    cutoff = (datetime.now(LOCAL_TZ) - timedelta(days=days)).isoformat()
    try:
        events = (_intake()._load_state(BOUNCE_KEY) or {}).get("events") or []
    except Exception:                                        # noqa: BLE001
        return []
    return sorted([e for e in events if _c(e.get("at")) >= cutoff],
                  key=lambda e: _c(e.get("at")), reverse=True)


def load_queue():
    q = _intake()._load_state(QUEUE_KEY) or {}
    return q, list(q.get("queue") or [])


def save_queue(q: dict, queue: list) -> None:
    q["key"] = QUEUE_KEY
    q["queue"] = queue
    _intake()._save_state(q)


def create_studio_draft(to: str, subject: str, body: str):
    """A Gmail draft in the studio mailbox with NO outbox row. The outbox is what puts an email
    in front of Alex, and the server's release (5 a day, 3 h hold) is the only thing allowed
    to do that for a first touch — filing it here would land it on his phone tonight AND make
    release_first_touches defer it forever as "a draft to this address is already open".
    Returns (draft_id or None, the module's own message)."""
    from composio import Composio                   # type: ignore
    import mail_drafts                              # type: ignore
    mail_drafts._file_in_outbox = lambda *a, **k: None
    mail_drafts.init(Composio(api_key=os.environ["COMPOSIO_API_KEY"]),
                     os.environ.get("PERSONAL_GMAIL_ENTITY", "alex"),
                     os.environ.get("SCHOOL_GMAIL_ENTITY", "alex-school"),
                     os.environ.get("STUDIO_GMAIL_ENTITY", ""))
    result = mail_drafts.create_email_draft("studio", to, subject, body)
    m = re.search(r"draft id ([^)\s]+)\)", result or "")
    return (m.group(1) if m else None), result


def record_draft_doc(brand: str, to: str, subject: str, body: str, ad_count: int,
                     evidence: str, today: str) -> str:
    """The same evidence-plus-email record the wave docs kept, so Alex (or a later session)
    can see exactly what was read and what was claimed."""
    os.makedirs(DRAFT_DOC_DIR, exist_ok=True)
    path = os.path.join(DRAFT_DOC_DIR, f"outreach-drafts-shift-{today}.md")
    new = not os.path.exists(path)
    with open(path, "a", encoding="utf-8") as f:
        if new:
            f.write(f"# Shift drafts — {today}\n\nWritten by the nightly money shift from a "
                    "live Ad Library read the same evening. Queued for the server's 5-a-day "
                    "release; each sends itself 3 h after release unless Alex taps Not doing it.\n")
        f.write(f"\n## {brand} — {to}\n**Live read {today}:** {ad_count} active ads. "
                f"{_c(evidence)}\n\n**Subject:** {subject}\n\n{body.strip()}\n")
    return path


# ---------------------------------------------------------------- the creator lane

# Approving the offer does not make the lane earn anything: the only way an email reaches anyone
# is the queue below, and `add` is shaped entirely around a DTC brand (a tracker row, a live ad
# count). A streamer has neither. So the creator lane gets its own door into the SAME queue —
# same 5-a-day release, same 3 h veto, same body guards — with its own proof of work.
#
# Its list is a markdown file rather than a CSV because that is what the operator writes, and a
# brittle parser over it would be a worse guard than a plain containment check: the address has
# to appear in the file, and the file is the only place an address can come from.

# Whole words, not substrings: "UNVERIFIED" contains "VERIFIED", and a marker that matched on
# substrings would read every warning as a clearance.
UNVERIFIED_RE = re.compile(r"\bUNVERIFIED\b")
VERIFIED_RE = re.compile(r"\bVERIFIED\b")


def creator_entry(text: str, email: str) -> dict:
    """What the prospect list says about one address: {found, unverified, name}.

    `unverified` is the whole point. The list marks an address UNVERIFIED when it came from a
    search-result summary instead of a page that was actually read, and an address like that is
    a guess — it bounces, or worse, reaches a stranger under Alex's name.
    """
    want = _c(email).lower()
    out = {"found": False, "unverified": False, "name": ""}
    if not want:
        return out
    name, lines = "", (text or "").splitlines()
    for i, line in enumerate(lines):
        if line.startswith("### "):
            name = line[4:].split("—")[0].strip()
        if want in line.lower():
            out["found"] = True
            out["name"] = name
            own = line.upper()
            if VERIFIED_RE.search(own):
                # The address's own line states it outright. This wins over anything nearby,
                # including a note explaining what the address USED to be marked as — which is
                # otherwise indistinguishable from a live warning.
                out["unverified"] = False
            elif UNVERIFIED_RE.search(own):
                out["unverified"] = True
            else:
                out["unverified"] = bool(
                    UNVERIFIED_RE.search(" ".join(lines[i + 1:i + 3]).upper()))
            return out
    return out


EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def creator_state(list_text: str, queue: list) -> dict:
    """{available, queued_pending} — how many creators can still be written to, and how many are
    already waiting in the queue. The operator uses this to hold a slice of the queue open: with
    41 brands draftable, a lane that always yields to Splitframe is a lane that never sends."""
    queued = {_c(e.get("to")).lower() for e in queue}
    pending = sum(1 for e in queue if e.get("lane") == "creator" and not _released_date(e))
    seen, available = set(), 0
    for raw in EMAIL_RE.findall(list_text or ""):
        addr = raw.lower().rstrip(".,;:)`")
        if addr in seen:
            continue
        seen.add(addr)
        entry = creator_entry(list_text, addr)
        if entry["found"] and not entry["unverified"] and not is_ticket_desk(addr) \
                and addr not in queued:
            available += 1
    return {"available": available, "queued_pending": pending}


def plan_creator(list_text: str, queue: list, to: str, subject: str, body: str,
                 evidence: str, offer_approved: bool) -> tuple:
    """(entry, problems). An empty problems list is the only permission to queue."""
    to = _c(to).lower()
    problems = []
    if not offer_approved:
        problems.append("the creator offer is not approved — no email on this lane goes out "
                        "until \"Creator Lane — Offer (approved).md\" exists")
    entry = creator_entry(list_text, to)
    if not entry["found"]:
        problems.append(f"{to or '(empty)'} is not in the creator prospect list — the list is the "
                        "only place an address on this lane may come from")
    if entry["unverified"]:
        problems.append("the list marks this address UNVERIFIED (it came from a search summary, "
                        "not a page that was read) — confirm it off their own page first")
    if is_ticket_desk(to):
        problems.append("ticket queue — this never reaches the creator")
    if any(_c(e.get("to")).lower() == to for e in queue):
        problems.append("already in the queue")
    if not _c(subject):
        problems.append("no subject")
    if len(_c(evidence)) < 25:
        problems.append("--evidence must say which stream and which moment was actually watched: "
                        "the pitch is their own footage back at them, and it is the one claim "
                        "that cannot be bluffed")
    problems += guard_body(body)
    return entry, problems


def record_creator_doc(name: str, to: str, subject: str, body: str,
                       evidence: str, today: str) -> str:
    os.makedirs(DRAFT_DOC_DIR, exist_ok=True)
    path = os.path.join(DRAFT_DOC_DIR, f"creator-drafts-{today}.md")
    new = not os.path.exists(path)
    with open(path, "a", encoding="utf-8") as f:
        if new:
            f.write(f"# Creator lane drafts — {today}\n\nWritten from footage watched the same "
                    "day. Queued for the same 5-a-day release as Splitframe; each sends itself "
                    "3 h after release unless Alex taps Not doing it.\n")
        f.write(f"\n## {name or to} — {to}\n**Watched {today}:** {_c(evidence)}\n\n"
                f"**Subject:** {subject}\n\n{body.strip()}\n")
    return path


def cmd_creator(args) -> int:
    list_text = ""
    if os.path.exists(CREATOR_PROSPECTS):
        with open(CREATOR_PROSPECTS, encoding="utf-8") as f:
            list_text = f.read()
    q, queue = load_queue()
    with open(args.body_file, encoding="utf-8") as f:
        body = f.read().strip()
    entry, problems = plan_creator(list_text, queue, args.to, args.subject, body,
                                   args.evidence, os.path.exists(CREATOR_OFFER))
    if problems:
        print("NOT queued:")
        for p in problems:
            print(f"  - {p}")
        return 1
    name = entry["name"] or _c(args.creator)
    if args.dry_run:
        print(f"OK (dry run): {name} <{args.to}> passes every guard; not drafted.")
        return 0
    draft_id, msg = create_studio_draft(args.to, args.subject, body)
    if not draft_id:
        print(f"NOT queued: no draft id came back from Gmail — {msg[:200]}")
        return 1
    today = today_local()
    queue.append({"brand": name, "to": _c(args.to).lower(), "subject": _c(args.subject),
                  "body": body, "draft_id": draft_id, "lane": "creator",
                  "evidence": _c(args.evidence), "queued_at": datetime.now(LOCAL_TZ).isoformat(),
                  "queued_by": "money-shift"})
    save_queue(q, queue)
    doc = record_creator_doc(name, args.to, args.subject, body, args.evidence, today)
    print(f"QUEUED (creator): {name} <{args.to}> draft {draft_id}; record "
          f"{os.path.basename(doc)}. It goes on the next 07:30 release (5/day) with the 3 h hold.")
    return 0


# ---------------------------------------------------------------- commands

def status_report(rows: list, queue: list, today: str) -> dict:
    pending = pending_entries(queue)
    day = datetime.fromisoformat(today).date()
    return {
        "today": today,
        "pending": [{"brand": e.get("brand"), "to": e.get("to"), "subject": e.get("subject")}
                    for e in pending],
        "pending_count": len(pending),
        "released_today": len(released_on(queue, day)),
        "runway_days": round(len(pending) / PER_DAY, 1),
        "need_drafts": max(0, RUNWAY_TARGET - len(pending)),
        "next_targets": next_targets(rows, queue),
        "candidates_to_qualify": candidates_to_qualify(rows, today),
        "hunter_targets": hunter_targets(rows),
        "next_close": next_close_variant(queue),
        "close_report": close_report(rows, queue),
        "bounces": recent_bounces(),
    }


def _print_status(rep: dict) -> None:
    print(f"First-touch queue, {rep['today']}: {rep['pending_count']} pending "
          f"({rep['runway_days']} days of runway at {PER_DAY}/day), "
          f"{rep['released_today']} released today. Need {rep['need_drafts']} more draft(s) "
          f"to hold {RUNWAY_TARGET} in stock.")
    for e in rep["pending"]:
        print(f"  queued: {e['brand']} <{e['to']}> — {e['subject']}")
    people = sum(1 for t in rep["next_targets"] if t["tier"] == "person")
    shared = len(rep["next_targets"]) - people
    print(f"\nCan be drafted next ({len(rep['next_targets'])}: "
          f"{people} named, {shared} front desk):")
    for t in rep["next_targets"]:
        known = ("never read live" if t["known_count"] is None else
                 f"{t['known_count']} active" + (f" (live {t['count_read_live']})"
                                                 if t["count_read_live"] else " (old note)"))
        who = t["contact"] or ("front desk" if t["tier"] == "shared" else "(no name)")
        print(f"  [{t['band']:>7}] {t['brand']} <{t['to']}> {who} "
              f"— {known} — page id {t['page_id'] or '?'}")
    cr = rep["close_report"]
    print(f"\nClose experiment — write the next one with the {rep['next_close'].upper()} close.")
    for arm in CLOSE_VARIANTS:
        print(f"  {arm:9} {cr[arm]['sent']:>3} sent, {cr[arm]['replied']} replied")

    b = rep["bounces"]
    if b:
        print(f"\nBounced in the last 14 days ({len(b)}) — the sending domain is the asset here:")
        for e in b[:6]:
            print(f"  {e.get('brand')} <{e.get('address')}> {_c(e.get('at'))[:10]}")

    print(f"\nCandidates worth a live read ({len(rep['candidates_to_qualify'])}):")
    for c in rep["candidates_to_qualify"]:
        print(f"  {c['brand']} — page id {c['page_id'] or '?'}")
    print(f"\nNext Hunter window ({len(rep['hunter_targets'])} in-band brands with no person):")
    for h in rep["hunter_targets"]:
        print(f"  {h['brand']} ({h['domain']}) — {h['known_count']} active")


def cmd_status(args) -> int:
    rows, _ = tracker_rows()
    _, queue = load_queue()
    rep = status_report(rows, queue, today_local())
    if args.json:
        print(json.dumps(rep, indent=1))
    else:
        _print_status(rep)
    return 0


def cmd_add(args) -> int:
    rows, fields = tracker_rows()
    q, queue = load_queue()
    with open(args.body_file, encoding="utf-8") as f:
        body = f.read().strip()
    variant_arg = _c(args.close)
    row, problems = plan_add(rows, queue, args.to, args.subject, body, args.ad_count, args.brand,
                             variant_arg, _c(args.offer_image))
    if problems:
        print("NOT queued:")
        for p in problems:
            print(f"  - {p}")
        return 1
    brand = _c(row.get("brand"))
    today = today_local()
    if args.dry_run:
        print(f"OK (dry run): {brand} <{args.to}> passes every guard; not drafted.")
        return 0
    draft_id, msg = create_studio_draft(args.to, args.subject, body)
    if not draft_id:
        # release_first_touches refuses an entry without a draft id, and the sender refuses
        # the ref — so a queue entry here would be a written email that can never go out.
        print(f"NOT queued: no draft id came back from Gmail — {msg[:200]}")
        return 1
    variant = variant_arg or next_close_variant(queue)
    queue.append({"brand": brand, "to": _c(args.to).lower(), "subject": _c(args.subject),
                  "body": body, "draft_id": draft_id, "ad_count": args.ad_count,
                  "close_variant": variant, "offer_image": _c(args.offer_image),
                  "evidence": _c(args.evidence), "queued_at": datetime.now(LOCAL_TZ).isoformat(),
                  "queued_by": "money-shift"})
    save_queue(q, queue)
    notes, status, changes = stamp_count(row, args.ad_count, today)
    row["notes"], row["status"] = notes, status
    row["close_variant"] = variant
    if "close_variant" not in fields:
        fields = fields + ["close_variant"]
    write_tracker(rows, fields, "shift")
    doc = record_draft_doc(brand, args.to, args.subject, body, args.ad_count, args.evidence, today)
    print(f"QUEUED: {brand} <{args.to}> draft {draft_id}; tracker {', '.join(changes) or 'unchanged'}; "
          f"record {os.path.basename(doc)}. It goes on the next 07:30 release (5/day) with the 3 h hold.")
    return 0


def cmd_note(args) -> int:
    rows, fields = tracker_rows()
    row = next((r for r in rows if _c(r.get("brand")).lower() == _c(args.brand).lower()), None)
    if row is None:
        print(f"no tracker row for {args.brand!r}")
        return 1
    notes, status, changes = stamp_count(row, args.ad_count, today_local())
    if not changes:
        print(f"{row['brand']}: already stamped today, nothing to change")
        return 0
    row["notes"], row["status"] = notes, status
    bak = write_tracker(rows, fields, "shift")
    print(f"{row['brand']}: {', '.join(changes)} (backup {os.path.basename(bak)})")
    return 0


def cmd_source(args) -> int:
    rows, fields = tracker_rows()
    row, problems = plan_source(rows, args.brand, args.domain, args.ad_count, today_local(),
                                pid=_c(args.page_id), category=args.category, evidence=args.evidence)
    if problems:
        print("NOT sourced:")
        for p in problems:
            print(f"  - {p}")
        return 1
    if args.dry_run:
        print(f"OK (dry run): {row['brand']} would enter as {row['status']} ({args.ad_count} active).")
        return 0
    rows.append({f: row.get(f, "") for f in fields})
    bak = write_tracker(rows, fields, "source")
    print(f"SOURCED: {row['brand']} ({row['domain']}) — {args.ad_count} active, status {row['status']} "
          f"(backup {os.path.basename(bak)})")
    if row["status"] == "qualified":
        print("  next: it needs a person. It shows up in the Hunter window on the next status.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("status", help="queue depth, runway, who to draft next")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_status)
    a = sub.add_parser("add", help="validate + draft + queue one first touch")
    a.add_argument("--to", required=True, help="recipient, must be a tracker-verified person")
    a.add_argument("--subject", required=True)
    a.add_argument("--body-file", required=True, help="plain-text body, 110-150 words")
    a.add_argument("--ad-count", type=int, required=True, help="active ads counted live tonight")
    a.add_argument("--brand", default="", help="sanity check against the tracker row")
    a.add_argument("--evidence", default="", help="one line: what the ads actually showed")
    a.add_argument("--close", default="", choices=("", "question", "offer"),
                   help="which close this email used; default alternates to balance the arms")
    a.add_argument("--offer-image", default="",
                   help="with --close offer: the photo ON THEIR OWN SITE the promised ad would be "
                        "built from. Required, because the promise is unkeepable without one")
    a.add_argument("--dry-run", action="store_true", help="run every guard, draft nothing")
    a.set_defaults(fn=cmd_add)
    so = sub.add_parser("source", help="add a brand nobody had, from a live Ad Library read")
    so.add_argument("--brand", required=True)
    so.add_argument("--domain", required=True)
    so.add_argument("--ad-count", type=int, required=True, help="active ads counted live tonight")
    so.add_argument("--page-id", default="", help="Ad Library page id, if the page view was the read")
    so.add_argument("--category", default="")
    so.add_argument("--evidence", default="", help="one line: what the ads actually showed")
    so.add_argument("--dry-run", action="store_true")
    so.set_defaults(fn=cmd_source)
    c = sub.add_parser("creator", help="validate + draft + queue one creator-lane first touch")
    c.add_argument("--to", required=True, help="must appear in the creator prospect list")
    c.add_argument("--creator", default="", help="name, if the list heading does not give one")
    c.add_argument("--subject", required=True)
    c.add_argument("--body-file", required=True, help="plain-text body, 110-150 words")
    c.add_argument("--evidence", required=True,
                   help="which stream and which moment was watched, with the date")
    c.add_argument("--dry-run", action="store_true", help="run every guard, draft nothing")
    c.set_defaults(fn=cmd_creator)
    n = sub.add_parser("note", help="stamp a live Ad Library count into the tracker")
    n.add_argument("--brand", required=True)
    n.add_argument("--ad-count", type=int, required=True)
    n.set_defaults(fn=cmd_note)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
