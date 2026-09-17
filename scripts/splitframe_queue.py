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
from datetime import datetime
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

COUNT_RE = re.compile(r"adlib (\d+) active(?: \[read live (\d{4}-\d{2}-\d{2})\])?")
PAGE_ID_RE = re.compile(r"\bid=(\d{6,})")


def today_local() -> str:
    return datetime.now(LOCAL_TZ).date().isoformat()


def _c(value) -> str:
    return (value or "").strip()


# ---------------------------------------------------------------- pure: reading the tracker

# Which inbox an address actually is. Three tiers, and the order is the whole point:
#
#   person      a named human. Best reply rate, always drafted first, only one that gets a name
#               in the greeting.
#   front desk  the company inbox at a small brand — hello@, info@, press@, or the brand's own
#               name. At 5-50 active ads the company is a few people and one of them reads it.
#   ticket desk support@, orders@, wholesale@. Whoever reads it is answering "where is my order",
#               has no say over creative, and can only close the ticket. Never written to.
#
# This used to be one blocklist of role prefixes, which meant any address that was not on the
# list counted as a person: `goodday@`, `justdoughit@`, `oudwarellc@`, `store@` all read as
# humans and would have been greeted by name. So a person now needs positive evidence instead —
# the tracker's own contact_name, a known first name, or a first.last local part. An unusual
# real name gets demoted to front desk, which costs a greeting and some ordering, never a send.

TICKET_LOCALS = {"support", "sup", "help", "care", "service", "customerservice", "custserv",
                 "orders", "order", "returns", "cs", "admin", "billing", "accounts", "shop",
                 "store", "sales", "dealers", "wholesale", "noreply", "no-reply", "donotreply"}

# Deliberately not exhaustive: anything that is not a person and not a ticket desk falls through
# to front desk, because a brand-voice inbox (goodday@, justdoughit@) is a front desk.
FRONT_LOCALS = {"hello", "hi", "hey", "heythere", "info", "contact", "talktous", "team",
                "partnerships", "partners", "press", "media", "marketing", "hola", "inquiries",
                "general", "studio", "office", "mail", "email", "ask", "connect"}

# Enough coverage for founder first names that appear without a contact_name in the tracker.
# A miss demotes to front desk; it never promotes a role inbox to a person.
FIRST_NAMES = {
    "aaron", "abby", "abigail", "adam", "adrian", "alan", "alex", "alexa", "alexis", "ali",
    "alice", "alicia", "allison", "amanda", "amber", "amy", "ana", "andrea", "andrew", "andy",
    "angela", "anna", "anne", "annie", "anthony", "april", "ariel", "ashley", "austin", "ava",
    "becca", "becky", "ben", "benjamin", "beth", "bethany", "bill", "billy", "blake", "bob",
    "bobby", "brad", "bradley", "brandon", "breanna", "brenda", "brendan", "brent", "brett",
    "brian", "briana", "brittany", "brooke", "bruce", "bryan", "caelin", "caitlin", "cameron",
    "camille", "cara", "carl", "carla", "carlos", "carly", "carol", "carolina", "caroline",
    "carrie", "casey", "cassidy", "catherine", "cathy", "chad", "charles", "charlie", "chase",
    "chelsea", "cheryl", "chris", "chrissy", "christian", "christina", "christine", "chloe",
    "cindy", "claire", "clara", "clark", "cody", "colin", "connor", "corey", "courtney", "craig",
    "crystal", "curtis", "dan", "dana", "daniel", "danielle", "danny", "darren", "dave", "david",
    "dawn", "dean", "deb", "debbie", "deborah", "denise", "dennis", "derek", "devin", "diana",
    "diane", "dominic", "don", "donald", "donna", "doug", "douglas", "drew", "dylan", "ed",
    "eddie", "eduardo", "edward", "elaine", "eleanor", "elena", "eli", "elijah", "elise",
    "elizabeth", "ella", "ellen", "ellie", "emily", "emma", "eric", "erica", "erik", "erin",
    "ethan", "eva", "evan", "eve", "faith", "felix", "fiona", "frank", "fred", "gabe", "gabriel",
    "gabriella", "gary", "gavin", "gemma", "gene", "george", "gerald", "gillian", "gina", "grace",
    "gracie", "graham", "grant", "greg", "gregory", "hailey", "haley", "hannah", "harry",
    "hayden", "heather", "heidi", "helen", "henry", "holly", "hope", "hunter", "ian", "isaac",
    "isabel", "isabella", "ivan", "jack", "jackie", "jackson", "jacob", "jacqueline", "jade",
    "jake", "james", "jamie", "jan", "jane", "janet", "jason", "jay", "jayden", "jean", "jeff",
    "jeffrey", "jen", "jenna", "jennifer", "jenny", "jeremy", "jerry", "jess", "jesse", "jessica",
    "jill", "jim", "jimmy", "joan", "joann", "joe", "joel", "john", "johnny", "jon", "jonathan",
    "jordan", "jose", "joseph", "josh", "joshua", "joy", "juan", "judy", "julia", "julian",
    "julianne", "julie", "justin", "kaitlyn", "karen", "kari", "kate", "katelyn", "katherine",
    "kathy", "katie", "katrina", "kayla", "keith", "kelly", "kelsey", "ken", "kenneth", "kevin",
    "kim", "kimberly", "kris", "kristen", "kristin", "kristina", "kyle", "lance", "lara", "larry",
    "laura", "lauren", "laurie", "lee", "leah", "leo", "leslie", "levi", "liam", "lily", "linda",
    "lindsay", "lindsey", "lisa", "liz", "logan", "lori", "louis", "lucas", "lucy", "luke",
    "lydia", "lynn", "madeline", "madison", "maggie", "mandy", "marc", "marcus", "margaret",
    "maria", "mariah", "marie", "mario", "marissa", "mark", "marta", "martha", "martin", "mary",
    "mason", "matt", "matthew", "maureen", "max", "maxx", "maya", "megan", "meghan", "mel",
    "melanie", "melissa", "meredith", "mia", "michael", "michele", "michelle", "mike", "miranda",
    "molly", "monica", "morgan", "nancy", "naomi", "natalie", "natasha", "nate", "nathan", "neil",
    "nicholas", "nick", "nicole", "nina", "noah", "nora", "olivia", "oscar", "owen", "pam",
    "pamela", "pat", "patricia", "patrick", "paul", "paula", "peggy", "peter", "philip", "phil",
    "phoebe", "polina", "rachel", "ralph", "randy", "ray", "rebecca", "regina", "renee", "rich",
    "richard", "rick", "riley", "rob", "robert", "robin", "rochelle", "rodney", "roger", "ron",
    "ronald", "rosa", "rose", "ross", "roy", "russell", "ruth", "ryan", "sabrina", "sally", "sam",
    "samantha", "samuel", "sandra", "sandy", "sara", "sarah", "scott", "sean", "serena", "seth",
    "shane", "shannon", "sharon", "shaun", "shawn", "sheila", "shelby", "shelly", "sierra",
    "simon", "sofia", "sophia", "sophie", "stacey", "stacy", "stephanie", "stephen", "steve",
    "steven", "stormi", "stuart", "sue", "susan", "suzanne", "sydney", "tami", "tammy", "tanya",
    "tara", "taylor", "ted", "teresa", "terry", "tess", "thomas", "tiffany", "tim", "timothy",
    "tina", "toby", "todd", "tom", "tommy", "tony", "tracy", "travis", "trevor", "tricia",
    "tyler", "valerie", "vanessa", "vera", "veronica", "vicki", "victor", "victoria", "vincent",
    "virginia", "wade", "walter", "wayne", "wendy", "wes", "will", "william", "willow", "wyatt",
    "zach", "zachary", "zoe",
}

DOTTED_NAME = re.compile(r"^[a-z]{2,}[._-][a-z]{2,}$")


def _local(email: str) -> str:
    e = _c(email).lower()
    return e.split("@", 1)[0] if "@" in e else ""


def _letters(value: str) -> str:
    return re.sub(r"[^a-z]", "", (value or "").lower())


def matches_contact(email: str, contact_name: str) -> bool:
    """The local part is the tracker's named contact: becca, maxx.appelman, pveksler, klee."""
    parts = [t.lower() for t in re.split(r"[^A-Za-z]+", contact_name or "") if len(t) > 1]
    if not parts:
        return False
    first, last = parts[0], parts[-1]
    local = _letters(_local(email))
    if not local:
        return False
    return local in {first, last, first + last, last + first, first[0] + last, first + last[0]}


def is_ticket_desk(email: str) -> bool:
    """An inbox whose job is orders. A creative pitch there is deleted by someone who could not
    have acted on it anyway, and costs a spam complaint on a domain that took weeks to warm."""
    return _local(email) in TICKET_LOCALS


def is_person(email: str) -> bool:
    """A real person's address, by positive evidence — a known first name or a first.last local
    part. Without a name there is nobody to greet, and a greeting invented for a shared inbox is
    the tell that the email was machine-written."""
    local = _local(email)
    if not local or local in TICKET_LOCALS or local in FRONT_LOCALS:
        return False
    return _letters(local) in FIRST_NAMES or bool(DOTTED_NAME.match(local))


def is_front_desk(email: str) -> bool:
    """A shared inbox a pitch can survive in: the company's own front door. Anything that is not
    a ticket desk qualifies, including brand-voice inboxes like goodday@ or justdoughit@ —
    those are the front door with a costume on."""
    return bool(_local(email)) and not is_ticket_desk(email)


def target_address(row: dict) -> tuple:
    """(address, tier) for a tracker row — "person", "shared", or ("", "") for nobody to write to.

    Both address columns are read because the tracker grew a column: `email` held whatever was
    found first and `email_generic` was added later for site-scraped addresses. A row can carry
    either, and a person always wins over a front desk on the same row.
    """
    cols = ("email", "email_generic")
    name = _c(row.get("contact_name"))
    for col in cols:                                   # the tracker's own named contact first
        e = _c(row.get(col)).lower()
        if e and name and matches_contact(e, name) and not is_ticket_desk(e):
            return e, "person"
    for col in cols:
        e = _c(row.get(col)).lower()
        if is_person(e):
            return e, "person"
    for col in cols:
        e = _c(row.get(col)).lower()
        if e and "@" in e and is_front_desk(e):
            return e, "shared"
    return "", ""


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


def plan_add(rows: list, queue: list, to: str, subject: str, body: str,
             ad_count, brand: str = ""):
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
    row, problems = plan_add(rows, queue, args.to, args.subject, body, args.ad_count, args.brand)
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
    queue.append({"brand": brand, "to": _c(args.to).lower(), "subject": _c(args.subject),
                  "body": body, "draft_id": draft_id, "ad_count": args.ad_count,
                  "evidence": _c(args.evidence), "queued_at": datetime.now(LOCAL_TZ).isoformat(),
                  "queued_by": "money-shift"})
    save_queue(q, queue)
    notes, status, changes = stamp_count(row, args.ad_count, today)
    row["notes"], row["status"] = notes, status
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
    n = sub.add_parser("note", help="stamp a live Ad Library count into the tracker")
    n.add_argument("--brand", required=True)
    n.add_argument("--ad-count", type=int, required=True)
    n.set_defaults(fn=cmd_note)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
