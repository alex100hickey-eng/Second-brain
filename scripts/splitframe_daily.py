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
import json
import os
import sys
from datetime import date, datetime

# On the server the vault is the git-synced copy (VAULT_PATH); on the Mac it is iCloud.
# Drafting only ever READS the tracker — the Mac stays the only writer, which is what keeps
# the two copies from diverging.
VAULT = os.environ.get("VAULT_PATH") or os.path.expanduser(
    "~/Library/Mobile Documents/com~apple~CloudDocs/Obsidian/Second brain")
TRACKER = os.path.join(VAULT, "Money", "prospect-tracker.csv")
STATE = os.path.expanduser("~/second-brain/scripts/splitframe_daily_state.json")
STATE_KEY = "splitframe:followups"
LOG = os.path.expanduser("~/second-brain/scripts/splitframe_daily.log")
CHAT = os.path.expanduser("~/second-brain/second-brain-chat")

MODEL = "claude-sonnet-5"
MAX_TOUCHES = 3          # first touch + 2 follow-ups, then the brand is left alone
SKIP_ADDRESSES = ("support@", "help@", "info@", "hello@", "contact@")

VOICE = """You are drafting a follow-up email as Alex Hickey, 19, who runs Splitframe Studio,
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

What "something new" is allowed to be, then:
- reasoning he did not spell out the first time, built from what the first email already stated
- the arithmetic on numbers ALREADY in the first email, shown plainly
- a sharper version of the same observation
- a concrete, honest offer (he will build one concept free if they want it — offered, not done)

Follow-up rules:
- Three touches total, then stop. EVERY follow-up must add something NEW, drawn only from the
  list above. Never "just bumping this up".
- The last touch gives an explicit easy out ("if this isn't a priority that's a fair no"),
  because a clean no is worth more than silence.
- 60-110 words. Shorter than a first touch.
- It is a reply inside the original thread, so do not reintroduce himself.
- End with something answerable, not a CTA wearing a question mark.

Return STRICT JSON: {"body": "..."} and nothing else. No subject — it is a threaded reply."""


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
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M')} {msg}"
    print(line)
    with open(LOG, "a") as f:
        f.write(line + "\n")


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
    with open(STATE, "w") as f:
        json.dump({k: v for k, v in st.items() if k != "_row_id"}, f, indent=1)


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
        key = (r.get("email") or "").strip().lower()
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


def write_followup(client, brand: str, contact: str, touch: int, original: str, days_since: int) -> str:
    last = touch == MAX_TOUCHES
    ask = (f"Brand: {brand}\nContact first name: {contact or '(unknown — do not guess a name)'}\n"
           f"This is touch {touch} of {MAX_TOUCHES}. {days_since} days have passed since the first email.\n"
           f"{'This is the LAST touch: give the explicit easy out.' if last else ''}\n"
           f"{'That gap is long enough to acknowledge plainly in a few words, without apologising twice.' if days_since > 7 else ''}\n\n"
           f"The email he already sent (do not repeat its points, build on them):\n---\n{original}\n---")
    msg = client.messages.create(model=MODEL, max_tokens=700, system=VOICE,
                                 messages=[{"role": "user", "content": ask}])
    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()
    return parse_body(text)


def parse_body(text: str) -> str:
    """The model is asked for {"body": ...} and mostly complies. A strict json.loads on the raw
    reply turns any preamble into a crash, which in an unattended job means no follow-up and no
    reason why — so pull the JSON object out, and fall back to the prose itself."""
    if text.startswith("```"):
        text = text.split("```")[1].removeprefix("json").strip()
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            body = json.loads(text[start:end + 1]).get("body")
            if body and body.strip():
                return body.strip()
        except (ValueError, AttributeError):
            pass
    return text.strip()


def nudge(title: str, body: str) -> None:
    sys.path.insert(0, CHAT)
    try:
        import proactive  # type: ignore
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


def waiting_for_first_touch(rows) -> list:
    """Verified real-person addresses that have never been emailed. Support desks don't count —
    a first touch about ad creative dies in a support queue."""
    out = []
    for r in rows:
        email = (r.get("email") or "").strip().lower()
        if not email or (r.get("sent_date") or "").strip():
            continue
        if email.startswith(SKIP_ADDRESSES):
            continue
        out.append(r)
    return out


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
    for row, touch in due:
        brand = (row.get("brand") or "").strip()
        address = (row.get("email") or "").strip()
        if address.lower() in waiting:
            log(f"{brand}: touch {touch} skipped — an unsent draft for {address} is already waiting")
            drafted.setdefault(address.lower(), []).append(touch)
            continue
        original = original_email(c, entity, address)
        if not original.get("thread_id"):
            log(f"{brand}: no sent message found for {address} — skipped (nothing to reply to)")
            continue
        sent_on = _d(row.get("sent_date", "")) or today
        try:
            body = write_followup(client, brand, (row.get("contact_name") or "").split(" ")[0],
                                  touch, original.get("body", ""), (today - sent_on).days)
        except Exception as exc:
            log(f"{brand}: draft generation failed — {str(exc)[:160]}")
            continue
        if len(body.split()) < 25:
            log(f"{brand}: touch {touch} REJECTED — body came back empty or too short to send")
            rejected.append(f"{brand} (touch {touch}): empty draft")
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
        made.append(f"{brand} (touch {touch})")
        log(f"{brand}: touch {touch} drafted on thread {original['thread_id']}")

    waiting = waiting_for_first_touch(rows)
    st["last_run"] = datetime.now().isoformat()
    st["waiting_first_touch"] = len(waiting)
    save_state(st)

    if rejected:
        nudge("Splitframe: a follow-up was withheld",
              "The generated copy claimed research Alex hasn't done, so it was not staged: "
              + "; ".join(rejected) + ". It needs a real look at the account first.")
    if made:
        nudge(f"{len(made)} follow-up{'s' if len(made) > 1 else ''} ready to send",
              ", ".join(made) + ". They're reply drafts on the original threads in "
              "splitframestudio Gmail — read, hit send. Nothing goes out until you do.")
    log(f"{len(made)} follow-up draft(s) made · {len(due)} due · "
        f"{len(waiting)} verified addresses still waiting on a first touch")
    return 0


if __name__ == "__main__":
    sys.exit(main())
