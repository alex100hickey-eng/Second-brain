#!/usr/bin/env python3
"""Splitframe daily operator — the follow-up engine that keeps the pipeline moving on its own.

Every first touch Splitframe has ever sent went out on 2026-09-01. Every follow-up since
(FU1 Sep 3, FU2 Sep 8, FU1 Sep 14) was missed, because "write the follow-ups" was a job that
only happened when a person or a chat session remembered. Replies come from follow-ups, so the
business produced $0 from a machine that was otherwise complete.

Every run (daily, launchd com.secondbrain.splitframe):
  - find prospects whose follow-up is due and who haven't replied
  - pull the original email out of the studio Sent folder
  - write touch 2 or 3 in Alex's voice (skill: splitframe-outreach) against what was already said
  - save it as a REPLY DRAFT on the original thread in the studio mailbox
  - nudge Alex's phone with the count and the one action
  - report how many verified addresses are still waiting on a first touch

Drafts only. There is no send path in this module and nothing here may add one — pinned by
test_splitframe_daily.test_no_send_capability. Alex sends.
"""
from __future__ import annotations

import csv
import importlib.util
import json
import os
import re
import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

# On the server the vault is the git-synced copy (VAULT_PATH); on the Mac it is iCloud.
# Drafting only ever READS the tracker — the Mac stays the only writer, which is what keeps
# the two copies from diverging.
# Alex's actual day. Pinned rather than system-local because this also runs inside the
# server container (UTC), where a naive now() rolls the date over at 8pm ET and the
# "5 a day" release cadence would spend two days' worth in one evening.
LOCAL_TZ = ZoneInfo("America/New_York")

VAULT = os.environ.get("VAULT_PATH") or os.path.expanduser(
    "~/Library/Mobile Documents/com~apple~CloudDocs/Obsidian/Second brain")
TRACKER = os.path.join(VAULT, "Money", "prospect-tracker.csv")
STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "splitframe_daily_state.json")   # beside the module, for the same reason as LOG
STATE_KEY = "splitframe:followups"
# Beside this file, NOT under ~. The server container runs the repo somewhere else entirely and
# its HOME is /root, so the expanduser path pointed at /root/second-brain/scripts/ — a directory
# that does not exist there.
LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "splitframe_daily.log")
CHAT = os.path.expanduser("~/second-brain/second-brain-chat")

MODEL = "claude-sonnet-5"
MAX_TOUCHES = 3          # first touch + 2 follow-ups, then the brand is left alone
MIN_BODY_WORDS = 25      # below this the generation failed; it is not a short email
# Follow-up cadence from the first touch (Money/Research — follow-up audit, 2026-09-25): touch 2 on
# day 3, touch 3 on day 10, no touch 4. It was day 7, which left 4 days between two asks and ate
# the daily ceiling that first touches need. splitframe_send and splitframe_queue read these.
FU1_DAYS, FU2_DAYS = 3, 10

# Two touch-2s went to founders as raw JSON (Monday Swimwear 09-20, Goodwipes 09-21: the body
# began `{"body":"Natasha —` and stopped mid-sentence) before parse_body was fixed. Whatever the
# drafter does next, the drafter AND the sender refuse a body that is machine output.
_JSON_KEY = re.compile(r'"body"\s*:')
_FREE_OFFER = re.compile(r"\b(free|yours either way|no charge|on the house)\b|"
                         r"\bi'?ll (build|make|put together|mock up)\b|"
                         r"\bwant me to (build|make|send|put)\b", re.I)


def broken_body(body: str) -> str:
    """Why this body is machine output rather than an email, or ""."""
    b = (body or "").strip()
    if b.startswith("{") or b.startswith("```"):
        return "starts with raw code"
    if _JSON_KEY.search(b):
        return 'contains a "body": key'
    return ""


def followup_problems(body: str, static_sent: bool) -> list:
    """Every reason a written follow-up must not go out (the audit's rules, checked)."""
    problems = []
    broken = broken_body(body)
    if broken:
        problems.append(broken)
    if not static_sent and _FREE_OFFER.search(body or ""):
        problems.append("offers to build something that doesn't exist")
    if "the math" in (body or "").lower():
        problems.append('"the math" re-grades the first email')
    if (body or "").count("—") > 1:
        problems.append("more than one em dash")
    lines = [l.strip() for l in (body or "").strip().splitlines() if l.strip()]
    lines = [l for l in lines if l not in ("Alex Hickey", "Splitframe Studio", "Alex")]
    if not lines or not lines[-1].endswith("?"):
        problems.append("does not end on a question")
    return problems
# Shared inboxes. A first touch about ad creative dies in a support queue, and Hunter will
# happily return one as "deliverable" — talktous@ and support@ both came back in the
# 2026-09-15 pass looking exactly like a real person's address.
SKIP_ADDRESSES = ("support@", "help@", "info@", "hello@", "contact@", "talktous@",
                  "team@", "care@", "service@", "orders@", "admin@", "sales@")

def _s(value) -> str:
    return (value or "").strip()


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
    e = _s(email).lower()
    return e.split("@", 1)[0] if "@" in e else ""


def _letters(value: str) -> str:
    return re.sub(r"[^a-z]", "", (value or "").lower())


def matches_contact(email: str, contact_name: str) -> bool:
    """The local part is the tracker's named contact: becca, maxx.appelman, pveksler, klee, or a
    bare first initial (j@ for Junaid Kalmadi at Farmers Juice, printed on their own site). The
    initial only counts against the row's own named contact, never on its own."""
    parts = [t.lower() for t in re.split(r"[^A-Za-z]+", contact_name or "") if len(t) > 1]
    if not parts:
        return False
    first, last = parts[0], parts[-1]
    local = _letters(_local(email))
    if not local:
        return False
    return local in {first, last, first + last, last + first, first[0] + last, first + last[0],
                     first[0]}


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
    name = _s(row.get("contact_name"))
    for col in cols:                                   # the tracker's own named contact first
        e = _s(row.get(col)).lower()
        if e and name and matches_contact(e, name) and not is_ticket_desk(e):
            return e, "person"
    for col in cols:
        e = _s(row.get(col)).lower()
        if is_person(e):
            return e, "person"
    for col in cols:
        e = _s(row.get(col)).lower()
        if e and "@" in e and is_front_desk(e):
            return e, "shared"
    return "", ""


# ---------------------------------------------------------------------------
# Named person or no send. Adopted 2026-09-23 from the Deliverability Audit (vault), relayed by
# the money session.
#
# 23 of the first 49 first touches went to a front desk (hello@, info@, press@). Two of them,
# Calypsa and Geode, came back as Gorgias support tickets: read by a support agent with no say
# over creative, whose job is to close the ticket. Every signal says the mail is ARRIVING. What
# the evidence points at is who reads it.
#
# So, for the ad-creative lane only: a queued first touch to a front desk is held at release,
# new front-desk drafts are refused, and a front-desk brand waits until a founder's address is on
# its row. The creator lane is exempt, because a streamer's business inbox IS the person.
#
# The cost is real: first-touch volume falls to the rate at which named addresses are found
# (Hunter's monthly quota, plus founders' published addresses). Set False to restore front-desk
# sends; nothing else changes.
NAMED_ONLY = True


def is_named_address(row, address: str) -> bool:
    """A person's own inbox: the row's named contact, a known first name, or first.last."""
    a = _s(address).lower()
    if not a or "@" not in a or is_ticket_desk(a):
        return False
    name = _s((row or {}).get("contact_name"))
    return bool(name and matches_contact(a, name)) or is_person(a)


def front_desk_hold(entry: dict, rows_by_address: dict) -> str:
    """Why this queued first touch must wait under NAMED_ONLY, or "" to let it go."""
    if not NAMED_ONLY or _s(entry.get("lane")) == "creator":
        return ""
    to = _s(entry.get("to")).lower()
    row = rows_by_address.get(to)
    if is_named_address(row, to):
        return ""
    brand = _s(entry.get("brand")) or to
    better, tier = target_address(row) if row else ("", "")
    if tier == "person" and better != to:
        return f"{brand} (a named address, {better}, is on the row now: re-draft to it)"
    return f"{brand} ({to})"


def rows_by_address(rows) -> dict:
    out = {}
    for r in rows or []:
        for col in ("email", "email_generic"):
            a = _s(r.get(col)).lower()
            if a:
                out[a] = r
    return out


AD_VOICE = """You are drafting a follow-up email as Alex Hickey, 19, who runs Splitframe Studio,
a one-person ad-creative service for DTC brands ($650 flat drop, $950/mo retainer).

How Alex writes:
- Short sentences and fragments. He stops and starts a new sentence instead of joining clauses.
- Concrete nouns, almost no adjectives. "actual trash cans", not "essential accessories".
- Dry, understated humour. Self-deprecating honesty: "took way longer than it should have".
- "actually" is his intensifier. Trailing practical clause with "so".
- Ends flat. No sign-off flourish, no "looking forward to hearing from you".
- Sentence case for strangers.

Tells that destroy the email (the whole pitch rests on it not reading as generated):
- More than one em dash. Appositive asides. Throat-clearing openers ("The thing that stood out:").
- Consultant vocabulary ("high-intent", "visceral"). More than one hedge. Tricolons.
- Sentences of similar length in a row. Compliment sandwiches.

HARD CONSTRAINT — what you actually know:
You have NOT looked at their ad account since the first email. You have no new data about them.
So you may NOT:
- claim to have "pulled the account again", "checked", or "looked since"
- state the current state of their ads (what is live, how many versions, what came down)
- invent numbers, metrics, headlines, warranty lengths, percentages or names
- claim work has been produced ("I actually built it", "made a couple already") unless the
  brief below explicitly says the asset exists
The whole pitch rests on Alex only ever saying things he actually did. One invented detail a
founder can check is worse than no follow-up at all.

The follow-up templates (Money/Research — follow-up audit, 2026-09-25). Fill them, don't
improvise around them. The first email already told them what's wrong with their ads; saying it
again, or as a percentage ("did the math"), is a grade, not something new.

Touch 2, no ad attached:
  {First name},
  Different idea from my first email.
  {One line, quoted exactly, that the first email already quoted from their site, reviews or ads,
  and one plain test built on it: that line as a static against what they run now.}
  {One question about their customer, not their numbers, not their ads.}
  Alex Hickey
  Splitframe Studio

Touch 3, no ad attached (the last one):
  {First name},
  Last one from me. {The test from the first two emails, in one short sentence} is still the
  first thing I'd test.
  If that isn't a priority right now, that's a fair no. If it's timing, tell me a month and I'll
  check back then.
  Is it a no, or a not now?
  Alex Hickey
  Splitframe Studio

Touch 3 after the ad was already delivered (the brief below says so):
  {First name},
  Last one from me. The {product} ad is yours to run whether we ever talk or not.
  If ads aren't a priority right now, that's a fair no. If it's timing, tell me a month and I'll
  check back then.
  Is it a no, or a not now?
  Alex Hickey
  Splitframe Studio

Rules:
- NEVER offer to build, make or send anything, and never say "free" or "yours either way",
  unless the brief below says the ad already exists and was delivered. An offer of work that
  doesn't exist is the one promise this lane kept breaking.
- No price. No "the math". No guilt ("so I stop following up"). No "one more thing".
- The line before the sign-off is a question they can answer about their own customers.
- 35-80 words. It is a reply inside the original thread, so do not reintroduce himself.
- If the contact's first name is unknown, open with no name at all.

Return STRICT JSON: {"body": "..."} and nothing else. No subject — it is a threaded reply."""


# The creator lane sells a completely different thing to completely different people: a $400/mo
# clip retainer to Twitch streamers, not ad creative to DTC founders. Running those follow-ups
# through AD_VOICE would quote the wrong offer and reason about an "ad account" the streamer does
# not have — an email that reads as a mail-merge accident to a live, qualified prospect. The first
# touches went out 2026-09-19 with follow-ups dated 09-22 and 09-26, so this was three days from
# sending itself.
CREATOR_VOICE = AD_VOICE.replace(
    "who runs Splitframe Studio,\na one-person ad-creative service for DTC brands "
    "($650 flat drop, $950/mo retainer).",
    "who cuts streamers' VODs into short-form clips for\nTikTok and YouTube Shorts. "
    "The offer is $400/mo for 3 clips a week, posted to their channels.",
).replace(
    """HARD CONSTRAINT — what you actually know:
You have NOT looked at their ad account since the first email. You have no new data about them.
So you may NOT:
- claim to have "pulled the account again", "checked", or "looked since"
- state the current state of their ads (what is live, how many versions, what came down)""",
    """HARD CONSTRAINT — what you actually know:
You have NOT watched their streams or VODs since the first email. You have no new data about them.
So you may NOT:
- claim to have "watched since", "caught the stream", "checked", or "looked since"
- state the current state of their channel (viewer counts, recent streams, what they played)""",
).replace(
    "- claim work has been produced (\"I actually built it\", \"made a couple already\") unless the\n"
    "  brief below explicitly says the asset exists",
    "- claim clips have been cut (\"I actually made one\", \"cut a couple already\") unless the\n"
    "  brief below explicitly says the clip exists",
)

# Category -> the voice that lane sells in. `category` is the tracker's own column.
VOICES = {"creator": CREATOR_VOICE}


def voice_for(row: dict) -> str:
    """The system prompt for this prospect's lane. Unknown categories get the ad-creative voice,
    which is what the overwhelming majority of the tracker is."""
    return VOICES.get((row or {}).get("category", "").strip().lower(), AD_VOICE)


# Phrases that assert Alex did work he did not do. The prompt forbids them; this is the check that
# runs anyway, because a prompt is a request and a founder who spots one invented detail is gone.
FABRICATION_TELLS = (
    "pulled the account", "pulled your account", "checked again", "looked again",
    "re-checked", "rechecked", "went back through", "since i wrote", "i actually built",
    "i built the", "i made the", "i put together", "already built", "made a couple",
    "ran the numbers on your", "i ran it against",
)


def fabrication_risk(body: str) -> list:
    """Phrases in a generated follow-up that claim fresh research or finished work."""
    low = " ".join(body.lower().split())
    return [t for t in FABRICATION_TELLS if t in low]


def log(msg: str) -> None:
    """Write a line, and NEVER be the reason the caller dies.

    This was unguarded, and on the server every call raised FileNotFoundError because LOG
    pointed into a directory that only exists on the Mac. Any log line reached on the server
    took the whole follow-up-and-release run down with it, hourly, reported only as a generic
    "follow-up drafting failed" warning nobody was reading.

    It only became total when a cap line was added to release_first_touches — before that the
    log calls were all inside branches, so the run survived exactly on the days nothing
    interesting happened and died on the days a follow-up was due. That is almost certainly why
    FU1 on Sep 3, FU2 on Sep 8 and FU1 on Sep 14 were all "missed".

    The stdout line is what actually matters (the server captures it); the file is a
    convenience. So print first, then try the file, and swallow anything it throws.
    """
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M')} {msg}"
    print(line, flush=True)
    try:
        with open(LOG, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


_shared = None          # intake module, once a Supabase client is wired into it


def load_state() -> dict:
    """Shared state, not a local file: the server drafts at 07:30 whether or not the Mac is
    awake, and the Mac can still run this by hand. Two nodes with two state files would send
    the same prospect the same follow-up twice."""
    if _shared:
        try:
            st = _shared._load_state(STATE_KEY)
            if st:
                st.setdefault("drafted", {})
                return st
        except Exception as exc:
            log(f"shared state unavailable ({str(exc)[:80]}) — falling back to the local file")
    try:
        with open(STATE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"drafted": {}}


def save_state(st: dict) -> None:
    if _shared:
        try:
            st["key"] = STATE_KEY
            _shared._save_state(st)
            return
        except Exception as exc:
            log(f"shared state write failed ({str(exc)[:80]}) — writing the local file")
    # The local file is the FALLBACK, so it must not crash harder than the thing it is catching.
    # Unguarded, it had the same defect LOG did: on the server this path does not exist, so a
    # Supabase hiccup would turn into a FileNotFoundError that killed the release outright.
    # Losing the state file only risks re-drafting a follow-up, and `already_waiting` catches
    # that downstream; losing the run means nothing goes out at all.
    try:
        with open(STATE, "w") as f:
            json.dump({k: v for k, v in st.items() if k != "_row_id"}, f, indent=1)
    except OSError as exc:
        log(f"local state write failed too ({str(exc)[:80]}) — continuing; a follow-up may "
            "be re-drafted, which already_waiting will defer")


def tracker_rows() -> list:
    with open(TRACKER, newline="") as f:
        return list(csv.DictReader(f))


def _d(value: str):
    try:
        return date.fromisoformat((value or "").strip())
    except ValueError:
        return None


def already_waiting(outbox_mod) -> set:
    """Recipients that already have an open outbox row. The state dict says what THIS node
    drafted; the outbox says what is actually sitting in front of Alex. When the two disagree
    the outbox is right — a crash between drafting and saving state once put a second identical
    follow-up in front of the same prospect."""
    out = set()
    try:
        for it in outbox_mod.open_items():
            if it.get("kind") != "email_draft":
                continue
            title = it.get("title") or ""
            if "@" in title:
                out.add(title.split()[-1].strip().lower())
    except Exception:
        pass
    return out


def due_followups(rows, today: date, drafted: dict) -> list:
    """(row, touch) for every prospect owed a follow-up now. touch 2 = FU1, touch 3 = FU2.

    A prospect who replied is done — the reply watcher stamps `replied`, and chasing someone who
    already answered is the one mistake that costs the relationship rather than just the email."""
    out = []
    for r in rows:
        if not (r.get("sent_date") or "").strip():
            continue
        if (r.get("replied") or "").strip() or (r.get("outcome") or "").strip():
            continue
        # NOT r["email"]: a front-desk brand carries its address in email_generic and leaves
        # `email` empty, so keying on that column skipped every one of them — a first touch went
        # out and no follow-up ever could. Silent, and most of the funnel is those brands now.
        key, _tier = target_address(r)
        if not key:
            continue
        done = drafted.get(key, [])
        for touch, column in ((2, "followup1_date"), (3, "followup2_date")):
            if touch in done or touch > MAX_TOUCHES:
                continue
            when = _d(r.get(column, ""))
            if when and when <= today:
                out.append((r, touch))
                break          # one touch per prospect per run, in order
    return out


def original_email(c, entity: str, address: str) -> dict:
    """The first touch as it was actually sent: thread id, subject and body."""
    res = c.tools.execute("GMAIL_FETCH_EMAILS", user_id=entity, dangerously_skip_version_check=True,
                          arguments={"query": f"in:sent to:{address}", "max_results": 5})
    msgs = (res.get("data") or {}).get("messages") or []
    if not msgs:
        return {}
    m = sorted(msgs, key=lambda x: str(x.get("messageTimestamp") or ""))[0]
    body = m.get("messageText") or ""
    if not body:
        preview = m.get("preview")
        body = (preview or {}).get("body", "") if isinstance(preview, dict) else str(preview or "")
    return {"thread_id": m.get("threadId") or "", "subject": str(m.get("subject") or ""),
            "body": str(body)[:2500], "sent": str(m.get("messageTimestamp") or "")[:10]}


def write_followup(client, brand: str, contact: str, touch: int, original: str, days_since: int,
                   voice: str = None, extra: str = "") -> str:
    last = touch == MAX_TOUCHES
    ask = (f"Brand: {brand}\nContact first name: {contact or '(unknown — do not guess a name)'}\n"
           f"This is touch {touch} of {MAX_TOUCHES}. {days_since} days have passed since the first email.\n"
           f"{'This is the LAST touch: give the explicit easy out.' if last else ''}\n"
           f"{'That gap is long enough to acknowledge plainly in a few words, without apologising twice.' if days_since > 7 else ''}\n\n"
           f"The email he already sent (do not repeat its points, build on them):\n---\n{original}\n---"
           + (f"\n\n{extra}" if extra else ""))
    # The call comes back empty often enough to matter — one run in a handful returns no text
    # block at all (verified 2026-09-18 reproducing Gunner Kennels touch 3). Unretried, that is
    # a prospect's LAST touch dropped in silence: the caller's word-count check rejects it, the
    # brand is marked drafted, and nobody ever writes to them again. Retry before giving up.
    body = ""
    for _ in range(3):
        msg = client.messages.create(model=MODEL, max_tokens=1400, system=voice or AD_VOICE,
                                     messages=[{"role": "user", "content": ask}])
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()
        body = parse_body(text)
        if len(body.split()) >= MIN_BODY_WORDS:
            return body
    return body


def parse_body(text: str) -> str:
    """The model is asked for {"body": ...} and mostly complies. A strict json.loads on the raw
    reply turns any preamble into a crash, which in an unattended job means no follow-up and no
    reason why — so pull the JSON object out, and fall back to the prose itself.

    The fallback is deliberately NOT applied to a reply that was trying to be JSON and got cut off.
    A generation truncated at max_tokens has no closing brace, so the old fallback returned the raw
    text — meaning the email body literally began `{"body": "` and then stopped mid-sentence. It
    cleared every downstream guard (45 words, well over MIN_BODY_WORDS, no fabrication tells) and
    would have been sent to a live prospect. Reproduced on Guzu touch 2, 2026-09-19.

    Returning "" instead puts it back through write_followup's retry loop, and if every attempt
    fails the caller rejects it loudly rather than sending braces to a founder."""
    if text.startswith("```"):
        text = text.split("```")[1].removeprefix("json").strip()
    text = text.strip()
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            body = json.loads(text[start:end + 1]).get("body")
            if body and body.strip():
                return body.strip()
        except (ValueError, AttributeError):
            pass
    if text.startswith("{"):
        # It committed to JSON and the JSON is unusable — truncated, or malformed past repair.
        # Prose that merely mentions a brace elsewhere is unaffected.
        return ""
    return text


def nudge(title: str, body: str) -> None:
    sys.path.insert(0, CHAT)
    try:
        import proactive  # type: ignore
        # proactive.send_nudge only needs the intake module (for the config and the sent-ledger)
        # and a timezone — not the Claude client or the tool dispatcher that full init() wants.
        # On the server app.py does the full init; on the Mac nothing ever did, so every nudge
        # from this job died with "'NoneType' object has no attribute '_load_state'" and fell
        # through to raw ntfy. That still delivers, but it loses deduplication and re-nudging,
        # so the one notification that lets Alex veto a send became best-effort. Wire the two
        # globals it actually uses, and only when they are unset, so a real init always wins.
        if getattr(proactive, "intake_mod", None) is None and _shared is not None:
            proactive.intake_mod = _shared
        if getattr(proactive, "LOCAL_TZ", None) is None:
            proactive.LOCAL_TZ = LOCAL_TZ
        reason = proactive.send_nudge("splitframe-daily", title, body, priority="high",
                                      tags="envelope", force=True)
        if not reason:
            return
        log(f"send_nudge refused: {reason}")
    except Exception as exc:
        log(f"send_nudge unavailable: {exc}")
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        log("no NTFY_TOPIC and send_nudge unavailable — nudge dropped")
        return
    # curl, not urllib: this Python has no local issuer certs, so urlopen dies with
    # CERTIFICATE_VERIFY_FAILED and the nudge is lost exactly when it matters.
    import subprocess
    url = f"{os.environ.get('NTFY_SERVER', 'https://ntfy.sh')}/{topic}"
    r = subprocess.run(["curl", "-fsS", "--max-time", "10", "-H", f"Title: {title[:120]}",
                        "-H", "Priority: high", "-H", "Tags: envelope", "-d", body, url],
                       capture_output=True, text=True)
    if r.returncode != 0:
        log(f"ntfy failed rc={r.returncode}: {r.stderr.strip()[:160]}")


QUEUE_KEY = "splitframe:firsttouch_queue"
HOLD_HOURS = 3             # Alex asked for auto-send (2026-09-15). This is the window in which he
                           # can still kill one: he does nothing and it goes, which is the point,
                           # but nothing leaves the building the instant a model wrote it.
PER_DAY = 5                # the floor, and where a cold or a troubled domain sends

# The cadence is not a constant any more, because leaving it at 5 was quietly the binding
# constraint on the whole business. 5/day is 25 a week; a first client needs on the order of
# 150-250 first touches at a 1-3% positive-reply rate, so 5/day put the first realistic shot
# past the Oct 15 kill date with no time left to actually close anyone.
#
# It stayed at 5 because raising it was a judgement nobody was scheduled to make. So it is a
# function of delivery evidence instead of a number someone has to remember to change:
#
#   under 20 clean sends   5/day   a cold or unproven domain
#   20-59                 10/day   the August plan's stated ceiling
#   60-119                15/day
#   120+                  20/day
#
# and ANY sign of bounce trouble drops it straight back to the floor. 2026-09-18, Alex, asked
# whether to go past the plan's 10: "Raise whatever needs to be raised to get me money." The
# risk was named to him first — this is his domain's reputation, and it is the asset that makes
# any of this reach an inbox. What makes 20 defensible rather than reckless: the domain is seven
# weeks old with clean auth (mail-tester 10/10), 31 sends have produced zero bounces and zero
# DSNs, and a single warmed mailbox on Workspace sustains this comfortably (the provider limit is
# 2,000/day — never the binding number). The bounce gate below is what keeps it honest, and it
# needs no one to remember it.
RAMP = ((120, 20), (60, 15), (20, 10), (0, 5))
BOUNCE_WINDOW_DAYS = 14

# Follow-ups on their own budget — decision A, 2026-09-23, relayed by the money session and
# logged in the Shift Log with this reversal. One shared cap meant ten first touches a day owed
# twenty follow-ups, and follow-ups alone filled the cap for days while first touches got no
# slot (22 due on 09-23 against a cap of 10). Now the bounce-gated cap above counts FIRST
# TOUCHES only; follow-ups go on the sender's one-per-run pacing; and TOTAL_DAILY_CEILING is a
# hard bound on everything the mailbox sends in a day, which is what the domain experiences.
# Flip FOLLOWUPS_SHARE_CAP to True to restore the pre-09-23 rule in the release AND the sender.
FOLLOWUPS_SHARE_CAP = False
# Raised 20 -> 25 on 2026-09-24 (tab 1's decision under Alex's standing order, logged in the Shift
# Log). The record then: 1 bounce in 53 sends, a 54-day-old Workspace domain, and 14-17 follow-ups
# a day to addresses already written to, which at 20 left room for about 3 first touches. 25
# keeps ~8 named first touches a day flowing; 30 waits for two weeks of clean bounce data.
# Revert: set TOTAL_DAILY_CEILING back to 20.
TOTAL_DAILY_CEILING = 25
# The guard on the raise: any bounce in the last BOUNCE_CEILING_HOURS holds the day at
# CEILING_AFTER_BOUNCE. So one bounce drops it to 20 at once and it only comes back after 48 hours
# with none, and an unreadable bounce record also reads as 20, never as the raised number.
CEILING_AFTER_BOUNCE = 20
BOUNCE_CEILING_HOURS = 48
BOUNCE_HOLD_RATE, BOUNCE_HOLD_MIN = 0.08, 2   # same threshold the bounce nudge fires on
BOUNCE_KEY = "splitframe:bounces"


def _event_time(e: dict):
    raw = _s((e or {}).get("at"))
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=LOCAL_TZ)


def ceiling_for(events: list, now: datetime) -> tuple:
    """(today's total ceiling, why) from the bounce events. Pure."""
    cutoff = now - timedelta(hours=BOUNCE_CEILING_HOURS)
    recent = [e for e in (events or []) if (_event_time(e) or cutoff) > cutoff]
    if recent:
        return CEILING_AFTER_BOUNCE, (f"{len(recent)} bounce(s) in the last {BOUNCE_CEILING_HOURS} h: "
                                      f"held at {CEILING_AFTER_BOUNCE}")
    return TOTAL_DAILY_CEILING, f"no bounce in {BOUNCE_CEILING_HOURS} h"


def _bounce_events() -> list:
    """The bounce record. The server wires _shared in main(); the Mac sender loads this module on
    its own and never did, so connect here the same way when it is missing."""
    shared = _shared
    if shared is None:
        sys.path.insert(0, CHAT)
        import intake                                   # type: ignore
        if intake.supabase is None:
            from supabase import create_client          # type: ignore
            intake.supabase = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
        shared = intake
    return (shared._load_state(BOUNCE_KEY) or {}).get("events") or []


def effective_ceiling(now: datetime = None) -> tuple:
    """(ceiling, why) for today, bounce-guarded. Unreadable reads as CEILING_AFTER_BOUNCE."""
    try:
        events = _bounce_events()
    except Exception as e:                            # noqa: BLE001
        return CEILING_AFTER_BOUNCE, (f"bounce record unreadable ({type(e).__name__}): "
                                      f"held at {CEILING_AFTER_BOUNCE}")
    return ceiling_for(events, now or datetime.now(LOCAL_TZ))


def daily_cap(sent_total: int, bounces_recent: int, sent_recent: int) -> int:
    """How many first touches may go out today, from the delivery record.

    Kept pure so the decision is testable without a mailbox: the caller supplies the counts.
    """
    rate = (bounces_recent / sent_recent) if sent_recent else 0.0
    if bounces_recent >= BOUNCE_HOLD_MIN and rate >= BOUNCE_HOLD_RATE:
        # Not a pause — a pause needs someone to un-pause it. Back to the floor, which keeps
        # the business running while the addresses are looked at.
        return PER_DAY
    for threshold, cap in RAMP:
        if sent_total >= threshold:
            return cap
    return PER_DAY


def _sent_counts(since: str = "") -> tuple:
    """(total sends, sends on/after `since`) straight from the tracker.

    Deliberately reads the CSV here rather than calling ad_creative_pipeline.sent_since:
    that helper needs the pipeline to have been init'd with a vault path, and when it has
    not been it returns 0 — which this function would read as "no sends yet, stay at the
    floor". Silent, and wrong in the direction that looks safe. This module already knows
    where the tracker is.
    """
    total = recent = 0
    try:
        with open(TRACKER, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                sent = _s(row.get("sent_date"))
                if not sent:
                    continue
                total += 1
                if since and sent >= since:
                    recent += 1
    except (OSError, csv.Error):
        return 0, 0
    return total, recent


def current_cap() -> tuple:
    """(cap, why) from live state. Falls back to the floor if anything is unreadable —
    an unreadable bounce record must never read as "no bounces, send more"."""
    try:
        cutoff = datetime.now(LOCAL_TZ) - timedelta(days=BOUNCE_WINDOW_DAYS)
        events = []
        if _shared:
            st = _shared._load_state(BOUNCE_KEY) or {}
            events = [e for e in (st.get("events") or [])
                      if _s(e.get("at")) >= cutoff.isoformat()]
        sent_total, sent_recent = _sent_counts(cutoff.date().isoformat())
    except Exception as e:                            # noqa: BLE001
        return PER_DAY, f"floor: could not read delivery state ({type(e).__name__})"
    if not sent_total:
        return PER_DAY, "floor: no send history readable"
    cap = daily_cap(sent_total, len(events), sent_recent)
    if len(events) >= BOUNCE_HOLD_MIN and cap == PER_DAY:
        return cap, f"floor: {len(events)} bounce(s) in {sent_recent} recent sends"
    return cap, (f"{sent_total} sends all time, {len(events)} bounce(s) "
                 f"in the last {BOUNCE_WINDOW_DAYS}d")


# A queued draft states facts about a brand's LIVE ad account — "about 21 active ads", "five of
# them are the same post". Those facts rot. Ironcroft went from 8 active ads to 0 in the two days
# between being read and being drafted, and the whole pitch rests on Alex only ever saying things
# a founder can check and find true. The queue holds two release days of stock, so a draft should
# never be more than a few days old when it goes; if it is, the release stalled and the right
# answer is to re-read the account, not to send an aged claim.
STALE_DRAFT_DAYS = 3


def _queued_age_days(entry, now=None) -> float:
    """How long this draft has been sitting. Unparseable or missing reads as 0 — an unknown age
    must not silently block a send; only a KNOWN old one does."""
    # A revise re-reads the facts (fresh clip, fresh ad-library read), which is what the stale
    # rule guards against, so the clock restarts at revised_at when there is one.
    raw = (entry.get("revised_at") or entry.get("queued_at") or "").strip()
    if not raw:
        return 0.0
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=LOCAL_TZ)
    return ((now or datetime.now(LOCAL_TZ)) - dt).total_seconds() / 86400.0


def open_follow_ups(outbox_mod) -> int:
    """Follow-up drafts sitting in the outbox: written, not yet sent. The same marker the sender
    uses (splitframe_send.is_follow_up): the drafter always threads a follow-up with "Re:".
    Studio mailbox only; a reply CLARVIS drafted for another inbox is not this lane's budget."""
    n = 0
    try:
        for it in outbox_mod.open_items():
            if it.get("kind") != "email_draft" or it.get("sent_at"):
                continue
            if (it.get("account") or "studio") != "studio":
                continue
            head = (it.get("detail") or "").split("\n", 1)[0].strip().lower()
            if head.startswith("subject: re:"):
                n += 1
    except Exception:                                  # noqa: BLE001
        pass
    return n


def followups_due_today(rows=None, today=None, drafted=None, outbox_mod=None) -> int:
    """How much of today's send budget the follow-up sequence has already claimed: the touches
    still to be drafted (due, and not in `drafted`) plus the follow-up drafts already waiting in
    the outbox.

    Until 2026-09-25 this counted every prospect whose follow-up DATE had passed, including the
    ones whose follow-ups had long since gone out. By 09-24 that read 52 against a ceiling of
    20-25, so the release worked out room for 0 first touches every morning: nothing was
    released after 09-21, fresh named founders and creator drafts included, while the real
    number due was 14. `drafted` is what due_followups already uses to skip done touches.

    A brand that replied or was closed out is not chased. Reads the tracker directly and fails
    to 0 — an unreadable tracker must not silently stop first touches, which is the failure the
    logger bug already taught once. A follow-up already SENT today isn't counted: the release
    runs at 07:50, before the 08:00 send window, and the sender enforces the ceiling itself.
    """
    today = today or datetime.now(LOCAL_TZ).date()
    if rows is None:
        try:
            with open(TRACKER, newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
        except (OSError, csv.Error):
            return 0
    n = len(due_followups(rows, today, drafted or {}))
    if outbox_mod is not None:
        n += open_follow_ups(outbox_mod)
    return n


def _released_date(entry):
    """The NY-local calendar day this entry actually went out, or None if it never did.

    Only an ISO timestamp counts as released. Anything else — including the old terminal
    "skipped: already waiting" marker written by earlier versions — reads as "still
    pending", so a queue entry that was retired by mistake comes back on the next run.
    """
    raw = (entry.get("released") or "").strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=LOCAL_TZ)
    return dt.astimezone(LOCAL_TZ).date()


# When the server's daily run (follow-up drafting, then the first-touch release) may start. The
# Mac attaches approved statics to queued first touches at 07:30 (static_first_backstop.sh), and
# a first touch the release has already put in the outbox can't be swapped any more. The loop
# used to wake hourly and run at its first tick after 07:00, which drifted with every deploy: on
# 2026-09-25 it ran at 07:18, so an approved static would have missed its email. Nothing sends
# before 08:00 and every release carries a 3 h hold, so starting at 07:40 costs nothing.
DAILY_RUN_AFTER = (7, 40)


def daily_run_due(now, last_run: str) -> bool:
    """True when the server loop should run today's splitframe job: not yet run today, and past
    DAILY_RUN_AFTER local time."""
    return last_run != now.strftime("%Y-%m-%d") and (now.hour, now.minute) >= DAILY_RUN_AFTER


def _entry_key(e: dict) -> tuple:
    """What identifies a queue entry across writers: its Gmail draft id, which `revise` keeps
    (it edits the draft in place, re-addresses included). Without a draft id: recipient, brand
    and when it was queued."""
    d = _s(e.get("draft_id"))
    if d:
        return ("draft", d)
    return ("entry", _s(e.get("to")).lower(), _s(e.get("brand")), _s(e.get("queued_at")))


def merge_queue(base: list, mine: list, latest: list) -> list:
    """Apply THIS writer's changes (base -> mine) onto the queue as it is now (latest).

    Every writer loads the whole queue, spends seconds on Gmail or the outbox, then saves the
    whole queue back. Two writers overlapping meant the later save erased the earlier one's
    work. 2026-09-25 07:38: the operator re-addressed four brands while a founder re-address of
    Loudcup was saving, and the Loudcup edit vanished from the queue while its Gmail draft kept
    the new text and the new recipient. Now only what this writer changed, added or removed is
    applied; everyone else's edits since the load survive. The same entry changed by both: the
    later save wins that entry, which is the old behaviour narrowed to one entry.
    """
    def sig(e):
        return json.dumps(e, sort_keys=True, default=str)
    base_sig = {_entry_key(e): sig(e) for e in base}
    out = list(latest)
    index = {_entry_key(e): i for i, e in enumerate(out)}
    kept = set()
    for e in mine:
        k = _entry_key(e)
        kept.add(k)
        swapped = ("draft", _s(e.get("replaced_draft")))
        if k not in base_sig and swapped in base_sig:
            # swap-first gives an entry a new Gmail draft (the one with the static attached) and
            # keeps the old id in replaced_draft. That is an edit of the old entry, not a removal
            # plus an addition: keyed naively it moved to the END of the FIFO queue and could miss
            # the day it was attached for.
            kept.add(swapped)
            if swapped in index:
                out[index[swapped]] = e
                index[k] = index.pop(swapped)
            continue
        if k not in base_sig:                          # added by this writer
            if k not in index:
                index[k] = len(out)
                out.append(e)
            continue
        if sig(e) != base_sig[k] and k in index:        # changed by this writer
            out[index[k]] = e
    removed = set(base_sig) - kept                     # removed by this writer
    if removed:
        out = [e for e in out if _entry_key(e) not in removed]
    return out


def release_first_touches(outbox_mod, drafts_url: str, limit: int = None) -> list:
    """Move up to `limit` already-written first-touch drafts into the outbox, which is what puts
    them in front of Alex. The drafts are written in a batch (they need a live Ad Library read,
    which stays manual); releasing them on a daily cadence is what stops that batch landing as
    one unreadable pile of twelve notifications.

    `limit` defaults to `current_cap()` — the cadence earns its way up from the delivery
    record instead of sitting at the starting number forever.

    `limit` is per CALENDAR DAY, not per call. It used to cap only the current invocation, so a
    retry, a launchd overlap or one manual run released another five on top of the five already
    sitting in Alex's outbox — which is exactly the pile the cadence exists to prevent.
    """
    if not _shared:
        return []
    if limit is None:
        cap, why = current_cap()
        # The cap is TOTAL sends a day — that is what the sending domain experiences, and the
        # sender enforces it across first touches and follow-ups alike. The release used to ask
        # for the whole cap as if it were its own, so on a heavy follow-up day it queued emails
        # the sender could not send: on 2026-09-20 five follow-ups and five first touches filled
        # the cap by 11:05 and ten more sat deferred all evening.
        #
        # That is not just delay. The backlog grows every day the two compete, and a first touch
        # that waits long enough is HELD as stale (STALE_DRAFT_DAYS) because its ad-library
        # claims have expired — so the work is thrown away rather than sent late.
        #
        # Follow-ups win the tie on purpose. They go to someone who has already been written to,
        # they are where replies actually come from, and their clock is fixed by a date already
        # promised to a prospect. A first touch can wait a day; a follow-up cannot be moved
        # without lying about the sequence.
        try:
            drafted = load_state().get("drafted") or {}
        except Exception:                              # noqa: BLE001
            drafted = {}
        due = followups_due_today(drafted=drafted, outbox_mod=outbox_mod)
        if FOLLOWUPS_SHARE_CAP:
            limit = max(0, cap - due)
            log(f"daily cap {cap}/day ({why}); {due} follow-up(s) due today, "
                f"so up to {limit} first touch(es)")
            if limit == 0 and due:
                log("no first touches today — follow-ups alone fill the cap. If that repeats, "
                    "the ceiling is what limits new prospects, not the drafting.")
        else:
            # First touches get their own cap; what the ceiling leaves after today's follow-ups
            # still bounds them, or the release would queue drafts the sender cannot send today
            # and they would go stale in the outbox.
            ceiling, ceiling_why = effective_ceiling()
            limit = max(0, min(cap, ceiling - due))
            log(f"first-touch cap {cap}/day ({why}); {due} follow-up(s) due today on their own "
                f"budget under a ceiling of {ceiling} ({ceiling_why}), so up to {limit} first touch(es)")
            if limit == 0 and due:
                log("no first touches today — follow-ups alone reach the daily ceiling. If that "
                    "repeats, the ceiling is what limits new prospects, not the drafting.")
    q = _shared._load_state(QUEUE_KEY)
    queue = q.get("queue") or []
    base = json.loads(json.dumps(queue, default=str))   # what this run loaded, for merge_queue
    today = datetime.now(LOCAL_TZ).date()
    spent = sum(1 for d in queue if _released_date(d) == today)
    room = limit - spent
    if room <= 0:
        return []
    pending = [d for d in queue if _released_date(d) is None]
    if not pending:
        return []
    waiting = already_waiting(outbox_mod)
    try:
        by_address = rows_by_address(tracker_rows())
    except (OSError, csv.Error):
        by_address = {}            # no row to consult: is_person(to) alone decides the tier
    released, deferred, malformed, stale, desk, held = [], [], [], [], [], []
    for entry in pending:
        if len(released) >= room:
            break
        hold_until = _s(entry.get("hold_until"))
        if hold_until and today.isoformat() < hold_until:
            # A date set with `splitframe_queue.py revise --hold-until`: the queue is FIFO, and a
            # draft kept for a planned day (the 09-28 A/B) must not go on an earlier one.
            held.append(f"{entry.get('brand') or entry.get('to')} (until {hold_until})")
            continue
        to = (entry.get("to") or "").strip()
        draft_id = (entry.get("draft_id") or "").strip()
        if not to or not draft_id:
            # scripts/splitframe_send.py refuses a ref with no draft id, so releasing this
            # would put "ready to send" in front of Alex for an email the one-tap path
            # cannot send. outbox.add also collapses duplicate refs, so every malformed
            # entry would land on the SAME "gmail:studio:" row and all of them would be
            # marked released off that one id. Hold them and say so.
            malformed.append(entry.get("brand") or to or "(no recipient)")
            continue
        age = _queued_age_days(entry)
        if age > STALE_DRAFT_DAYS:
            # Held, not dropped: the email is fine, its FACTS are what expired. Sending "about
            # 21 active ads" to a brand that now runs none is the one mistake this pitch cannot
            # survive, and it is checkable in ten seconds by the person receiving it.
            stale.append(f"{entry.get('brand') or to} ({age:.0f}d)")
            continue
        hold = front_desk_hold(entry, by_address)
        if hold:
            desk.append(hold)
            continue
        if to.lower() in waiting:
            # Deferred, NOT retired. This used to write a terminal "skipped" marker, so any
            # unrelated open draft to this address killed the queued first touch for good —
            # a written cold email that silently never went out and never reported it.
            deferred.append(entry.get("brand") or to)
            continue
        rid = outbox_mod.add(
            "email_draft", f"Send the reply to {to}",
            detail=f"Subject: {entry.get('subject','')}\n\n{entry.get('body','')}",
            link=drafts_url,
            # The /do page recomputes these from the item's armed state (do_actions
            # ._resolve_outbox); these are the fallback for any other renderer, so they must
            # not tell him to press a button the email no longer waits for.
            steps=[f"This sends itself about {HOLD_HOURS} hours from now. Nothing to do.",
                   "Read it — this is exactly what goes out.",
                   "Wrong? Tap Not doing it to kill it, or Snooze to push the send back."],
            account="studio", ref=f"gmail:studio:{draft_id}")
        if rid:
            entry["released"] = datetime.now(LOCAL_TZ).isoformat()
            outbox_mod.arm_auto_send(
                rid, (datetime.now(LOCAL_TZ) + timedelta(hours=HOLD_HOURS)).isoformat())
            released.append(f"{entry.get('brand', to)}")
    if deferred:
        log("first touch held (a draft to the same address is already open, will retry): "
            + ", ".join(deferred))
    if malformed:
        log("first touch NOT released — queue entry has no draft_id/recipient, it cannot be "
            "sent by the one-tap path: " + ", ".join(malformed))
    if desk:
        log("first touch HELD — named person or no send (NAMED_ONLY): a front desk waits until "
            "a founder's address is on its row: " + ", ".join(desk))
    if held:
        log("first touch HELD — kept for a later day on purpose (--hold-until): " + ", ".join(held))
    if stale:
        log(f"first touch HELD — drafted more than {STALE_DRAFT_DAYS} days ago and its ad-library "
            "claims may no longer be true; re-read the account and re-draft: " + ", ".join(stale))
    q["key"] = QUEUE_KEY
    try:
        latest = (_shared._load_state(QUEUE_KEY) or {}).get("queue") or []
        q["queue"] = merge_queue(base, queue, latest)
    except Exception:                                  # noqa: BLE001
        q["queue"] = queue                             # can't re-read: the old whole-queue write
    _shared._save_state(q)
    return released


def waiting_for_first_touch(rows) -> list:
    """Addresses that have never been emailed and can be. Ticket queues don't count — a first
    touch about ad creative dies in a support queue — but a front desk does."""
    out = []
    for r in rows:
        email, _tier = target_address(r)
        if not email or (r.get("sent_date") or "").strip():
            continue
        out.append(r)
    return out


def funnel_headline(rows, drafted) -> str:
    """The one-line summary from scripts/funnel_report.py, for this run's output.

    This runs on the server, which has no send log, so a follow-up counts as done here once
    it's drafted. The full report file is written on the Mac only: two machines writing the
    same vault path is a git conflict in vault sync. This never raises. A report isn't worth
    losing a day's follow-ups over, and an unguarded logger already taught that once.
    """
    try:
        spec = importlib.util.spec_from_file_location(
            "funnel_report", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                          "funnel_report.py"))
        fr = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fr)
        return fr.summary_line(fr.build(rows, datetime.now(LOCAL_TZ).date(), drafted=drafted))
    except Exception as exc:                          # noqa: BLE001
        return f"funnel: unavailable ({type(exc).__name__}: {str(exc)[:80]})"


def load_offer_statics():
    """(module, approved, state) from scripts/offer_statics.py, or (None, {}, {}).

    The offer arm promised a static "yours either way". Once Alex approves one in the QA folder's
    INDEX.md, that brand's next follow-up is the written "here it is" email with the PNG attached,
    instead of a generated one. Never raises: a missing QA folder or an unreadable state just
    means every follow-up keeps its normal wording."""
    try:
        spec = importlib.util.spec_from_file_location(
            "offer_statics", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                          "offer_statics.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        approved, skipped = mod.approved_statics()
        for why in skipped:
            log(f"static not used: {why}")
        state = (_shared._load_state(mod.STATE_KEY) if _shared else {}) or {}
        return mod, approved, state
    except Exception as exc:                          # noqa: BLE001
        log(f"offer statics unavailable ({type(exc).__name__}: {str(exc)[:80]}); follow-ups "
            "keep their normal wording")
        return None, {}, {}


def arm_new_followup(outbox_mod, address: str, extra: dict = None) -> bool:
    """Arm the outbox row create_email_draft (or _file_in_outbox) just filed for `address`."""
    row = next((o for o in outbox_mod.open_items()
                if (o.get("title") or "").strip().lower().endswith(address.lower())
                and not o.get("auto_send_at")), None)
    if not row:
        return False
    if extra:
        outbox_mod._write(row["id"], extra)
    outbox_mod.arm_auto_send(
        row["id"], (datetime.now(LOCAL_TZ) + timedelta(hours=HOLD_HOURS)).isoformat())
    return True


def main() -> int:
    sys.path.insert(0, CHAT)
    import anthropic                                    # type: ignore
    from composio import Composio                       # type: ignore
    import mail_drafts                                  # type: ignore
    import outbox                                       # type: ignore
    from supabase import create_client                  # type: ignore

    c = Composio(api_key=os.environ["COMPOSIO_API_KEY"])
    entity = os.environ.get("STUDIO_GMAIL_ENTITY")
    mail_drafts.init(c, os.environ.get("PERSONAL_GMAIL_ENTITY", "alex"),
                     os.environ.get("SCHOOL_GMAIL_ENTITY", "alex-school"), entity)
    # Without this the outbox filing inside create_email_draft fails soft and the whole
    # one-tap chain never starts: no outbox row means no nudge, no /do page, no Send button.
    # The draft would sit in Gmail exactly as invisibly as it did before any of this existed.
    sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
    outbox.init(sb)
    global _shared
    import intake                                     # type: ignore
    intake.supabase = sb
    _shared = intake
    # the repo standardised on CLAUDE_API_KEY; accept the vendor name too so a future .env
    # rename does not silently stop the follow-ups the way the last gap did
    key = os.environ.get("CLAUDE_API_KEY") or os.environ["ANTHROPIC_API_KEY"]
    client = anthropic.Anthropic(api_key=key)

    rows = tracker_rows()
    st = load_state()
    drafted = st.setdefault("drafted", {})
    today = date.today()
    due = due_followups(rows, today, drafted)
    waiting = already_waiting(outbox)

    made, rejected = [], []
    statics_mod, approved_statics, statics_state = load_offer_statics()
    delivered_statics = statics_state.get("delivered") or {}
    for row, touch in due:
        brand = (row.get("brand") or "").strip()
        address, _tier = target_address(row)
        if address.lower() in waiting:
            log(f"{brand}: touch {touch} skipped — an unsent draft for {address} is already waiting")
            drafted.setdefault(address.lower(), []).append(touch)
            continue
        original = original_email(c, entity, address)
        if not original.get("thread_id"):
            log(f"{brand}: no sent message found for {address} — skipped (nothing to reply to)")
            continue
        sent_on = _d(row.get("sent_date", "")) or today
        plan = (statics_mod.plan_for(address, delivered_statics, approved_statics)
                if statics_mod else "")
        if plan == "attach":
            info = approved_statics[address.lower()]
            subject = original["subject"]
            subject = subject if subject.lower().startswith("re:") else f"Re: {subject}"
            draft_id, problems = statics_mod.create_attach_draft(
                c, entity, address, subject, info["body"], original["thread_id"], info["png"])
            if problems:
                log(f"{brand}: touch {touch} static NOT attached ({'; '.join(problems)}); "
                    "writing the normal follow-up instead")
            else:
                mail_drafts._file_in_outbox("studio", address, subject, info["body"], draft_id)
                drafted.setdefault(address.lower(), []).append(touch)
                save_state(st)
                delivered_statics[address.lower()] = {
                    "brand": brand, "file": info["file"], "touch": touch, "draft": draft_id,
                    "via": "drafter", "at": datetime.now(LOCAL_TZ).isoformat()}
                try:
                    _shared._save_state({**statics_state, "key": statics_mod.STATE_KEY,
                                         "delivered": delivered_statics})
                except Exception as exc:                  # noqa: BLE001
                    log(f"statics state not saved ({str(exc)[:80]}); the next touch may re-offer it")
                if not arm_new_followup(outbox, address, {"static_attached": info["file"]}):
                    log(f"{brand}: static drafted but no outbox row to arm — it will wait for a tap")
                made.append(f"{brand} (touch {touch}, static attached)")
                log(f"{brand}: touch {touch} drafted WITH the static ({info['file']}) on thread "
                    f"{original['thread_id']}")
                continue
        try:
            body = write_followup(client, brand, (row.get("contact_name") or "").split(" ")[0],
                                  touch, original.get("body", ""), (today - sent_on).days,
                                  voice_for(row),
                                  extra=statics_mod.DELIVERED_NOTE if plan == "delivered" else "")
        except Exception as exc:
            log(f"{brand}: draft generation failed — {str(exc)[:160]}")
            continue
        if len(body.split()) < MIN_BODY_WORDS:
            log(f"{brand}: touch {touch} REJECTED — body came back empty or too short to send")
            rejected.append(f"{brand} (touch {touch}): empty draft")
            continue
        shape = followup_problems(body, static_sent=(plan == "delivered"))
        if shape:
            log(f"{brand}: touch {touch} REJECTED — {'; '.join(shape)}")
            rejected.append(f"{brand} (touch {touch}): {'; '.join(shape)}")
            continue
        risky = fabrication_risk(body)
        if risky:
            log(f"{brand}: touch {touch} REJECTED — claims work not done: {', '.join(risky)}")
            rejected.append(f"{brand} (touch {touch}): {', '.join(risky)}")
            continue
        subject = original["subject"]
        result = mail_drafts.create_email_draft("studio", address,
                                                subject if subject.lower().startswith("re:") else f"Re: {subject}",
                                                body, thread_id=original["thread_id"])
        if "Draft saved" not in result:
            log(f"{brand}: draft NOT saved — {result[:160]}")
            continue
        drafted.setdefault(address.lower(), []).append(touch)
        save_state(st)
        # create_email_draft filed the outbox row; arm it so it sends itself after the hold.
        row = next((o for o in outbox.open_items()
                    if (o.get("title") or "").strip().lower().endswith(address.lower())
                    and not o.get("auto_send_at")), None)
        if row:
            outbox.arm_auto_send(
                row["id"], (datetime.now(LOCAL_TZ) + timedelta(hours=HOLD_HOURS)).isoformat())
        else:
            log(f"{brand}: drafted but no outbox row to arm — it will wait for a tap")
        made.append(f"{brand} (touch {touch})")
        log(f"{brand}: touch {touch} drafted on thread {original['thread_id']}")

    import mail_drafts as _md                            # type: ignore
    fresh = release_first_touches(outbox, _md.drafts_url("studio"))
    if fresh:
        log(f"released {len(fresh)} first touch(es): {', '.join(fresh)}")

    waiting = waiting_for_first_touch(rows)
    st["last_run"] = datetime.now(LOCAL_TZ).isoformat()
    st["waiting_first_touch"] = len(waiting)
    # The funnel headline goes into this run's output and into shared state, where the rest of
    # the system can read it without opening the vault.
    st["funnel"] = funnel_headline(rows, drafted)
    log(st["funnel"])
    save_state(st)

    if rejected:
        nudge("Splitframe: a follow-up was withheld",
              "The generated copy claimed research Alex hasn't done, so it was not staged: "
              + "; ".join(rejected) + ". It needs a real look at the account first.")
    going = made + fresh
    if going:
        when = (datetime.now(LOCAL_TZ) + timedelta(hours=HOLD_HOURS)).strftime("%-I:%M %p")
        nudge(f"{len(going)} email{'s' if len(going) > 1 else ''} going out at {when}",
              ", ".join(going) + f". They send themselves at {when} — open this and tap "
              "'Not doing it' on any you want stopped. Nothing needed if they're fine.")


if __name__ == "__main__":
    sys.exit(main())
