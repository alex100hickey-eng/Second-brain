"""Draft Alex's answer to a founder who replied, so his part is one Send.

    python3 scripts/splitframe_queue.py reply --from <their address>   [--slots "..."] [--dry-run]
    python3 scripts/splitframe_queue.py reply --thread <gmail thread id> [--slots "..."] [--dry-run]

A reply is the whole point of the lane, and the step after it was manual: read the thread, open
the pre-call brief, work out two times he's free, write it in his voice. This does all of that
and leaves a Gmail draft on the thread with an outbox row, so Alex sees it on his phone with a
Send button. It never sends and never arms an auto-send: a human answered, so a human sends.

The reply follows Money/Splitframe — Reply Playbook (2026-09-25): one of its types, its price
story word for word ($650 first drop, then $950/month), no links, no third number. Call times
appear only when the founder asked for a call; they come from his own schedule (the training
app grid CLARVIS syncs), the first free half hour on the next weekdays, evenings first, at least
18 h out. `--slots` overrides them ("Tue 9/29 at 7:30 PM ET; Wed 9/30 at 8 PM ET").
"""
from __future__ import annotations

import glob
import importlib.util
import json
import os
import re
import sys
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CHAT = os.path.join(ROOT, "second-brain-chat")
LOCAL_TZ = ZoneInfo("America/New_York")

OWN_DOMAINS = {"splitframestudio.com"}
CALL_MINUTES = 30
MIN_LEAD_HOURS = 18
BUFFER_MINUTES = 15
# Evenings first: he is in class or at practice most afternoons, and a founder can usually take
# a call after work. Afternoon half hours are the fallback when an evening is booked.
PREFERRED_STARTS = ([time(19, 0), time(19, 30), time(20, 0), time(18, 30), time(18, 0),
                     time(17, 30), time(20, 30)]
                    + [time(h, m) for h in range(12, 17) for m in (0, 30)])
REPLY_MIN_WORDS, REPLY_MAX_WORDS = 15, 140


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_sfd = _load("splitframe_daily")


def _c(v) -> str:
    return (v or "").strip() if isinstance(v, str) else ""


def sender_address(msg: dict) -> str:
    s = str(msg.get("sender") or msg.get("from") or "")
    return s.split("<")[-1].rstrip(">").strip().lower()


def is_ours(msg: dict) -> bool:
    return sender_address(msg).split("@")[-1] in OWN_DOMAINS


def their_latest(msgs: list) -> dict:
    """The newest message in the thread that did not come from the studio mailbox."""
    theirs = [m for m in msgs if sender_address(m) and not is_ours(m)]
    return max(theirs, key=lambda m: str(m.get("messageTimestamp") or ""), default={})


def our_latest(msgs: list) -> dict:
    ours = [m for m in msgs if is_ours(m)]
    return max(ours, key=lambda m: str(m.get("messageTimestamp") or ""), default={})


def body_text(msg: dict) -> str:
    """The message without quoted history (the same rule reply_watch uses)."""
    raw = msg.get("messageText")
    if not raw:
        p = msg.get("preview")
        raw = p.get("body") if isinstance(p, dict) else p
    lines = []
    for line in str(raw or "").splitlines():
        s = line.lstrip()
        if s.startswith(">"):
            continue
        if s.lower().startswith("on ") and s.rstrip().endswith("wrote:"):
            break
        lines.append(line)
    return "\n".join(lines).strip()


def find_precall(clients_dir: str, brand: str) -> tuple:
    """(path, text) of the newest pre-call brief whose title names this brand, or ("", "")."""
    want = _c(brand).lower()
    best = ("", "")
    for path in sorted(glob.glob(os.path.join(clients_dir, "*", "precall-*.md"))):
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
        except OSError:
            continue
        m = re.search(r"Pre-Call Brief:\s*(.+)", text)
        title = m.group(1).strip().lower() if m else ""
        if want and (title == want or title.startswith(want) or want.startswith(title) and title):
            best = (path, text)                        # sorted: the last match is the newest
    return best


# His grid plans every half hour, so "free" means a block that already holds work he could move:
# study, work, cleanup, review. A sales call IS work. Class, gym, meals, the 50/50 drill, the
# night routine and sleep stay blocked. (Found 2026-09-25: counting every block as busy left
# only Friday evenings a week apart.)
FLEXIBLE = re.compile(r"\b(study|work|clean ?up|review|free|open)\b", re.I)
FIXED = re.compile(r"\b(class|gym|practice|game|lift|dinner|lunch|breakfast|sleep|routine|50/50|"
                   r"[A-Z]{3,5} ?\d{3})\b")


def is_flexible(block: dict) -> bool:
    title = _c(block.get("title"))
    return bool(FLEXIBLE.search(title)) and not FIXED.search(title.split("·")[0])


def free_slots(events_for, now: datetime, n: int = 2) -> list:
    """The first free call-length window on each of the next weekdays, evenings first.

    `events_for(date)` returns that day's schedule blocks ({"start", "end"} naive local
    datetimes, as training_schedule.events_for_date does). A window must clear every block by
    BUFFER_MINUTES and start at least MIN_LEAD_HOURS from `now`."""
    local_now = now.astimezone(LOCAL_TZ).replace(tzinfo=None) if now.tzinfo else now
    earliest = local_now + timedelta(hours=MIN_LEAD_HOURS)
    out = []
    for i in range(1, 15):
        d = local_now.date() + timedelta(days=i)
        if d.weekday() >= 5:                            # founders take calls on weekdays
            continue
        blocks = [b for b in (events_for(d) or []) if not is_flexible(b)]
        for t in PREFERRED_STARTS:
            start = datetime.combine(d, t)
            end = start + timedelta(minutes=CALL_MINUTES)
            if start < earliest:
                continue
            pad = timedelta(minutes=BUFFER_MINUTES)
            if any(b["start"] < end + pad and b["end"] > start - pad for b in blocks):
                continue
            out.append(start)
            break
        if len(out) >= n:
            break
    return out


def fmt_slot(dt: datetime) -> str:
    hour = dt.strftime("%I").lstrip("0")
    minute = "" if dt.minute == 0 else f":{dt.minute:02d}"
    return f"{dt.strftime('%A')} {dt.month}/{dt.day} at {hour}{minute} {dt.strftime('%p')} ET"


PLAYBOOK = "Money/Splitframe — Reply Playbook (2026-09-25).md"

REPLY_VOICE = """You are answering, as Alex Hickey (19, runs Splitframe Studio alone), a founder who
replied to his cold email. The rulebook is the Splitframe reply playbook; follow it exactly.

How Alex writes: short sentences and fragments, concrete nouns, dry and understated, no
consultant vocabulary, at most one em dash, sentence case, ends flat, signs "Alex". Never
"looking forward to hearing from you". The word "AI" never appears; asked how the ads get made:
"Judge them in the account."

The price story, word for word, the only numbers that may appear:
- First drop, $650 flat: 15 ads (12 statics, 3 short video cuts from footage they already have)
  built from a teardown of their live ads, plus a one-page test plan. Within 72 hours of
  kickoff. No contract.
- Then, only if it earns it, $950 a month: 20 new ads, five a week, plus a monthly readout.
  Cancel any month.
- Kickoff is the payment link paid and five brief answers back. One round of changes on any
  ad. They keep everything.

Pick the ONE type their message is, and write that reply (same thread, answer what they asked,
no re-pitch of the first email):
- interested ("these are good", "sure, send it"): thanks for writing back; if the brief below
  says the ad was NOT attached in the first email, say it's attached now; then the first drop in
  two sentences from the price story; end "Want me to start with the {product}?"
- pricing ("send more info", "what does it cost"): "Sure, short version so you don't have to
  open anything." then the first drop, then the monthly line, then "If they don't, you keep
  everything."; end "Should I start with the {product}?"
- not_now ("maybe later", "not right now"): "Totally fair." then ask what month to check back,
  and promise one email then with a new ad, not before. No pitch.
- agency ("we have an agency"): he isn't after the media buying or the brand work; more ads on
  angles theirs don't run; offer two more ads for the {product} tomorrow to hand to the agency.
- source ("how did you get my email"): the true source from the brief (their own site, or
  Hunter, an email lookup tool), that he found them through their ads in the Meta Ad Library,
  and "If you'd rather not hear from me, say so and you won't." Never guess the source.
- hostile or unsubscribe: exactly "Understood, you're off my list. Sorry for the noise."
- call (they asked for a call): offer the two times given below in words, word for word, or
  "send me a time that works". Only this type offers times.
- other: answer what they said in two or three sentences from the brief; if you can't, say he
  will find out.

Never: a discount or any third number, a calendar link or any link, a PDF or deck, a promise of
work not in the price story, invented results or clients.

Return JSON only: {"kind": "<one type above>", "body": "..."}"""


def row_facts(row: dict) -> str:
    """What the playbook's replies depend on, from the tracker row: was the ad already attached
    (arm A, or a static-first send) and where the address came from (the source question)."""
    row = row or {}
    arm = _c(row.get("close_variant"))
    attached = "yes" if arm == "arm-A" else ("no, attach it now" if arm == "arm-B" else "unknown")
    status = _c(row.get("email_status")).lower()
    source = ("printed on their own site" if status == "published" else
              "Hunter, an email lookup tool" if status in ("verified", "deliverable") else "unknown: do not guess")
    return f"Ad attached in the first email: {attached}\nWhere their address came from: {source}\n"


def build_ask(brand: str, contact: str, theirs: str, ours: str, brief: str, slots: list,
              facts: str = "") -> str:
    return (f"Brand: {brand}\nTheir first name: {contact or '(unknown: no name in the greeting)'}\n{facts}"
            f"The two call times, ONLY if they asked for a call (use them word for word): "
            f"{'; '.join(slots) if slots else '(none)'}\n\n"
            f"What they wrote (the latest message):\n---\n{theirs[:2500]}\n---\n\n"
            f"Alex's last email in the thread:\n---\n{ours[:2000]}\n---\n\n"
            f"The pre-call brief (internal, never quote it as a document):\n---\n{brief[:4000]}\n---")


def parse_reply(text: str) -> tuple:
    """(kind, body) from the model's JSON, ("", "") when it isn't usable."""
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.split("```")[1].removeprefix("json").strip()
    start, end = t.find("{"), t.rfind("}")
    if start == -1 or end <= start:
        return "", ""
    try:
        obj = json.loads(t[start:end + 1])
    except ValueError:
        return "", ""
    return _c(obj.get("kind")).lower(), _c(obj.get("body"))


def check_reply(kind: str, body: str, slots: list) -> list:
    problems = []
    words = len(body.split())
    if kind != "hostile" and words < REPLY_MIN_WORDS:
        problems.append(f"too short ({words} words)")
    if words > REPLY_MAX_WORDS:
        problems.append(f"too long ({words} words)")
    risky = _sfd.fabrication_risk(body)
    if risky:
        problems.append("claims work not done: " + ", ".join(risky))
    if body.count("—") > 1:
        problems.append(f"{body.count('—')} em dashes")
    if re.search(r"\bA\.?I\.?\b", body):
        problems.append('the word "AI" appears')
    if "looking forward" in body.lower():
        problems.append("sign-off flourish")
    if re.search(r"https?://|www\.|calendly", body, re.I):
        problems.append("a link (the playbook: no links, no calendar in a reply)")
    prices = set(re.findall(r"\$\s?([\d,]+)", body))
    if prices - {"650", "950"}:
        problems.append("a price outside the price story: $" + ", $".join(sorted(prices - {"650", "950"})))
    if kind == "call":
        missing = [x for x in slots if x not in body]
        if slots and missing:
            problems.append("the call times are not in it word for word: " + "; ".join(missing))
    elif any(x in body for x in slots):
        problems.append("offers call times they didn't ask for")
    if kind == "hostile" and body.strip() != "Understood, you're off my list. Sorry for the noise.":
        problems.append("a hostile reply gets exactly the one-line removal")
    return problems


def write_reply(client, ask: str, slots: list, tries: int = 3) -> tuple:
    """(kind, body, problems) from the best of up to `tries` generations."""
    last = ("", "", ["no usable draft came back"])
    for _ in range(tries):
        msg = client.messages.create(model=_sfd.MODEL, max_tokens=1200, system=REPLY_VOICE,
                                     messages=[{"role": "user", "content": ask}])
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        kind, body = parse_reply(text)
        if not body:
            continue
        problems = check_reply(kind, body, slots)
        last = (kind, body, problems)
        if not problems:
            break
    return last


def schedule_slots(now: datetime, n: int = 2) -> list:
    """Two free call times from his synced training-app schedule, or [] if it can't be read."""
    try:
        sys.path.insert(0, CHAT)
        import training_schedule                           # type: ignore
        import training_sync                               # type: ignore
        from supabase import create_client                 # type: ignore
        training_sync.init(create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"]))
        parsed = training_sync.parsed()
        if not parsed:
            return []
        return [fmt_slot(s) for s in free_slots(
            lambda d: training_schedule.events_for_date(parsed, d), now, n)]
    except Exception:                                       # noqa: BLE001
        return []


def record(clients_dir: str, precall_path: str, brand: str, to: str, kind: str, body: str,
           slots: list, result: str) -> str:
    # The playbook's place for them: Money/Clients/splitframe-replies-<date>.md
    os.makedirs(clients_dir, exist_ok=True)
    path = os.path.join(clients_dir, f"splitframe-replies-{datetime.now(LOCAL_TZ).strftime('%Y-%m-%d')}.md")
    new = not os.path.exists(path)
    with open(path, "a", encoding="utf-8") as f:
        if new:
            f.write("# Splitframe replies\n\nDrafted by `splitframe_queue.py reply` from the reply "
                    "playbook. Alex sends; nothing here goes out by itself.\n")
        f.write(f"\n## {datetime.now(LOCAL_TZ).strftime('%H:%M')} — {brand} <{to}> ({kind})\n"
                f"Brief: {precall_path or 'none'}\n"
                f"Times offered: {'; '.join(slots) or 'none'}\n\n{body}\n\n_{result[:200]}_\n")
    return path
