"""
outbox.py — the ledger of work CLARVIS finished that still needs Alex's hand.

There is a hard gate in this system: no send path, anywhere (see CLAUDE.md).
`create_email_draft` writes a real Gmail draft and stops; Alex presses Send.
That gate is correct and stays. But it left a hole nobody was watching: once the
draft existed, NOTHING in the system knew it was still sitting there. CLARVIS
would spend a model call writing a reply to a coach, announce "draft saved", and
then never mention it again — so the reply that mattered went out three days late,
or never. The gate needs a partner: if the machine can't finish the job, it has to
keep asking for the hand that can.

An outbox item is that ask, made durable:
  * it names ONE physical action ("send the 2 drafts in the studio Gmail"),
  * it carries the LINK that starts that action and the STEPS to finish it,
  * it stays open until Alex says it's done (or CLARVIS observes it is),
  * and while it's open it is a first-class nudge source — proactive.py raises it
    like a deadline, because a prepared-but-unsent thing IS a deadline.

Storage is the usual "Agent Outputs" piggyback (`jarvis_outbox`), updated in place
like `jarvis_pending_action`. Kept deliberately small: one row per waiting thing,
status open|done|dropped, plus a snooze stamp so "not now" is a real answer that
doesn't cost the item its life.
"""

import json
from datetime import datetime, timedelta

AGENT = "jarvis_outbox"
OPEN, DONE, DROPPED = "open", "done", "dropped"

# How long an item waits before it is allowed to nudge at all. A draft written
# 40 seconds ago does not need a notification — Alex is very likely still in the
# conversation that produced it, and buzzing him about his own last sentence is
# how a channel gets muted. Per-kind, because urgency differs.
FIRST_NUDGE_AFTER_MIN = {
    "email_draft": 45,
    "default": 30,
}

supabase = None
LOCAL_TZ = None


def init(supabase_client, local_tz=None):
    global supabase, LOCAL_TZ
    supabase = supabase_client
    LOCAL_TZ = local_tz


def _now():
    return datetime.now(LOCAL_TZ) if LOCAL_TZ else datetime.now()


def _parse(ts: str):
    try:
        dt = datetime.fromisoformat(str(ts))
        if dt.tzinfo is None and LOCAL_TZ:
            dt = dt.replace(tzinfo=LOCAL_TZ)
        return dt
    except (TypeError, ValueError):
        return None


def add(kind: str, title: str, *, detail: str = "", link: str = "",
        steps=None, account: str = "", ref: str = "") -> int | None:
    """File one waiting thing. Returns the row id, or None if the store is down —
    fail-soft on purpose: an outbox write must never break the action that
    produced the draft. Duplicate `ref`s collapse onto the existing open item so a
    retried tool call doesn't nudge twice about one email."""
    if not supabase or not (title or "").strip():
        return None
    try:
        if ref:
            for it in open_items(limit=60):
                if it.get("ref") == ref:
                    return it["id"]
        payload = {"kind": kind, "title": title.strip()[:200],
                   "detail": (detail or "").strip()[:2000],
                   "link": (link or "").strip()[:500],
                   "steps": [str(s)[:200] for s in (steps or [])][:6],
                   "account": account, "ref": ref,
                   "status": OPEN, "created": _now().isoformat(),
                   "snooze_until": ""}
        row = (supabase.table("Agent Outputs")
               .insert({"agent_name": AGENT,
                        "output_text": json.dumps(payload)}).execute())
        return (row.data or [{}])[0].get("id")
    except Exception as e:
        print(f"outbox: add failed ({e})")
        return None


def _rows(limit: int = 60) -> list:
    if not supabase:
        return []      # uninitialised (tests, import-time callers) is not an error
    try:
        res = (supabase.table("Agent Outputs").select("*").eq("agent_name", AGENT)
               .order("id", desc=True).limit(limit).execute())
    except Exception as e:
        print(f"outbox: read failed ({e})")
        return []
    out = []
    for r in res.data or []:
        try:
            item = json.loads(r["output_text"])
        except (json.JSONDecodeError, TypeError, KeyError):
            continue
        item["id"] = r["id"]
        out.append(item)
    return out


# How far back an open-item read may go. 1000 is PostgREST's default max rows per request, so a
# bigger number would be silently truncated by the server anyway.
SCAN_CEILING = 1000


def open_items(limit: int = 60, include_snoozed: bool = True) -> list:
    """Everything still waiting on Alex, newest first: up to `limit` OPEN items.

    `limit` counts open items, not rows read. It used to go straight to _rows(), which reads the
    newest `limit` rows of ANY status and filters afterwards, so an open item became invisible
    once `limit` newer rows existed. On 2026-09-24 Moon Juice's follow-up (static attached, due
    09:17) sat behind 34 newer rows: the sender never saw it, and Alex tapping Send wouldn't have
    helped, because awaiting_send read the same 30-row window. So the read now widens until it
    has `limit` open items or has read every row, and it says so if it ever hits the ceiling."""
    now = _now()
    scan = max(limit, 60)
    while True:
        rows = _rows(scan)
        out = []
        for it in rows:
            if it.get("status") != OPEN:
                continue
            if not include_snoozed:
                until = _parse(it.get("snooze_until") or "")
                if until and until > now:
                    continue
            out.append(it)
        if len(out) >= limit or len(rows) < scan:
            return out[:limit]
        if scan >= SCAN_CEILING:
            print(f"outbox: read the newest {scan} rows and found {len(out)} open item(s); "
                  "anything older is not visible. Close or archive old rows.")
            return out[:limit]
        scan = min(scan * 4, SCAN_CEILING)


def nudgeable(limit: int = 60) -> list:
    """Open items that have aged past their quiet period and aren't snoozed —
    the set proactive.py is allowed to raise."""
    now = _now()
    out = []
    for it in open_items(limit, include_snoozed=False):
        created = _parse(it.get("created") or "")
        wait = FIRST_NUDGE_AFTER_MIN.get(it.get("kind"),
                                         FIRST_NUDGE_AFTER_MIN["default"])
        if created and (now - created) < timedelta(minutes=wait):
            continue
        out.append(it)
    return out


def get(item_id: int) -> dict | None:
    """One item by id, however old. Scanning the newest 120 rows made an older item read as
    gone on its /do page."""
    if not supabase:
        return None
    try:
        res = (supabase.table("Agent Outputs").select("*").eq("id", item_id).execute())
    except Exception as e:
        print(f"outbox: read failed ({e})")
        return None
    for r in res.data or []:
        if r.get("agent_name") != AGENT:
            continue
        try:
            item = json.loads(r["output_text"])
        except (json.JSONDecodeError, TypeError, KeyError):
            return None
        item["id"] = r["id"]
        return item
    return None


def _write(item_id: int, changes: dict) -> dict | None:
    try:
        res = (supabase.table("Agent Outputs").select("*")
               .eq("id", item_id).execute())
        if not res.data or res.data[0].get("agent_name") != AGENT:
            return None
        item = json.loads(res.data[0]["output_text"])
        item.update(changes)
        (supabase.table("Agent Outputs")
         .update({"output_text": json.dumps(item)}).eq("id", item_id).execute())
        item["id"] = item_id
        return item
    except Exception as e:
        print(f"outbox: write failed ({e})")
        return None


def close(item_id: int, status: str = DONE, note: str = "") -> dict | None:
    if status not in (DONE, DROPPED):
        return None
    return _write(item_id, {"status": status, "note": note[:200],
                            "closed": _now().isoformat()})


def sweep_sent(draft_ids_fn) -> list:
    """Close open email items whose Gmail draft has left the Drafts folder.

    The no-send gate means CLARVIS never learns an email went out unless Alex
    says so — and he doesn't say so (the advisor reply was nudged twice after
    it had gone). The mailbox itself is readable: if the draft an item filed
    (ref "gmail:<account>:<draft id>") is no longer in Drafts, it was sent or
    discarded, and either way it stopped waiting on his hand.
    `draft_ids_fn(account)` returns a set of ids, or None for "don't know" —
    and None never closes anything. Returns the item ids closed."""
    by_account = {}
    for it in open_items(limit=60, include_snoozed=True):
        ref = str(it.get("ref") or "")
        parts = ref.split(":", 2)
        if len(parts) != 3 or parts[0] != "gmail" or not parts[2]:
            continue
        by_account.setdefault(parts[1], []).append((it, parts[2]))
    closed = []
    for account, items in by_account.items():
        try:
            present = draft_ids_fn(account)
        except Exception:
            present = None
        if not isinstance(present, (set, frozenset)):
            continue
        for it, draft_id in items:
            if draft_id in present:
                continue
            if close(it["id"], note="draft left Gmail Drafts (sent or discarded) — "
                                    "closed automatically") is not None:
                closed.append(it["id"])
    return closed


def approve_send(item_id: int, source: str = "notification") -> dict | None:
    """Stamp Alex's approval to send an email_draft item. This records a decision; it does not
    send. A separate process on his Mac (scripts/splitframe_send.py) is the only thing that can,
    which is what keeps the send capability off the internet-facing node."""
    item = get(item_id)
    if not item or item.get("kind") != "email_draft":
        return None
    if item.get("status") != OPEN or item.get("send_approved"):
        return item
    return _write(item_id, {"send_approved": _now().isoformat(),
                            "send_approved_by": source})


def arm_auto_send(item_id: int, when_iso: str) -> dict | None:
    """Schedule an email_draft to go on its own at `when_iso` unless Alex stops it first.

    The hold window is the whole safety model for auto-send: he does nothing and it goes, which
    is what he asked for, but there is a real window in which "Not doing it" still kills it."""
    item = get(item_id)
    if not item or item.get("kind") != "email_draft":
        return None
    return _write(item_id, {"auto_send_at": when_iso})


def due_to_auto_send(now_iso: str, limit: int = 500) -> list:
    """Open email drafts whose hold window has expired and that nobody approved or killed."""
    out = []
    for it in open_items(limit=limit):
        if it.get("kind") != "email_draft" or it.get("sent_at") or it.get("send_approved"):
            continue
        when = it.get("auto_send_at")
        if when and when <= now_iso:
            out.append(it)
    return out


def awaiting_send(limit: int = 500) -> list:
    """Open email_draft items Alex has approved and nobody has sent yet.

    Snoozed rows are excluded. open_items() includes them by default, which is right for "what
    is still on his plate" but wrong here: the sender runs every two minutes, so an item held
    back for any reason would be retried — and re-nudged — every two minutes. The send gate
    holds a refused recipient by snoozing it, and that hold only means anything if the next
    pass actually respects it.
    """
    return [it for it in open_items(limit=limit, include_snoozed=False)
            if it.get("kind") == "email_draft" and it.get("send_approved")
            and not it.get("sent_at")]


def snooze(item_id: int, hours: float = 3) -> dict | None:
    """"Not now" without losing the item. Snoozing is the honest answer most of
    the time, and an assistant that only offers done/never trains him to lie."""
    until = _now() + timedelta(hours=float(hours))
    return _write(item_id, {"snooze_until": until.isoformat()})


def summary_line(it: dict) -> str:
    """One line for a brief or a nudge body."""
    bits = [it.get("title", "(untitled)")]
    created = _parse(it.get("created") or "")
    if created:
        age_h = (_now() - created).total_seconds() / 3600
        if age_h >= 24:
            bits.append(f"waiting {int(age_h // 24)}d")
        elif age_h >= 1:
            bits.append(f"waiting {int(age_h)}h")
    return " — ".join(bits)


# ============================================================
# Tools — so CLARVIS can put something on Alex's hand deliberately
# ============================================================

def flag_for_alex(title: str, steps=None, link: str = "", detail: str = "",
                  kind: str = "action") -> str:
    """Queue a thing only Alex can finish, WITH the steps and the link.

    This is the deliberate half of the outbox. The email path files itself, but
    plenty of what blocks him doesn't come from a tool call — a form to submit, a
    call to make, a number to confirm. Anything filed here gets nudged like a
    deadline and gets its own one-tap page, so "you need to do this" arrives with
    the instructions attached instead of as a sentence he has to reconstruct."""
    item_id = add(kind, title, detail=detail, link=link, steps=steps)
    if not item_id:
        return "Couldn't file that — the outbox store didn't answer."
    return (f"Filed as outbox #{item_id}. Alex gets a notification with the link "
            f"and steps, and it stays open until he says it's done.")


def waiting_text() -> str:
    """What's currently blocked on Alex — for chat, and for the ambient context."""
    items = open_items()
    if not items:
        return "Nothing is waiting on you."
    lines = [f"{len(items)} thing(s) waiting on you:"]
    for it in items:
        lines.append(f"- #{it['id']} {summary_line(it)}"
                     + (f"  ({it['link']})" if it.get("link") else ""))
    return "\n".join(lines)


def clear_item(item_id: int, dropped: bool = False) -> str:
    res = close(int(item_id), DROPPED if dropped else DONE, note="closed from chat")
    if not res:
        return f"No open outbox item #{item_id}."
    return f"Outbox #{item_id} → {res['status']}."


TOOL_SCHEMAS = [
    {"name": "flag_for_alex",
     "description": "Put something on Alex's plate that only he can finish (submit "
                    "a form, make a call, send/confirm something), WITH the exact "
                    "steps and the link that starts it. He gets a phone "
                    "notification carrying those steps and a one-tap done button, "
                    "and it keeps surfacing until he closes it. Use this instead of "
                    "telling him about it once in chat — chat scrolls away.",
     "input_schema": {"type": "object", "required": ["title"], "properties": {
         "title": {"type": "string",
                   "description": "The physical action, imperative: 'Send the reply "
                                  "to Coach Staley', 'Submit the ACCT quiz'."},
         "steps": {"type": "array", "items": {"type": "string"},
                   "description": "2-4 concrete steps, first one being the tap that "
                                  "starts it. No vague 'handle it'."},
         "link": {"type": "string",
                  "description": "URL that starts step 1 (the form, the mailbox, the "
                                 "portal). Omit if there genuinely isn't one."},
         "detail": {"type": "string",
                    "description": "Context he'll need on the page — the draft text, "
                                   "the numbers, who asked."}}}},
    {"name": "list_waiting_on_alex",
     "description": "What is currently blocked on Alex — unsent drafts and anything "
                    "else flagged for his hand.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "clear_waiting_item",
     "description": "Close an outbox item when Alex says he did it (or won't).",
     "input_schema": {"type": "object", "required": ["item_id"], "properties": {
         "item_id": {"type": "integer"},
         "dropped": {"type": "boolean",
                     "description": "True if he's not doing it, rather than done."}}}},
]

TOOL_STATUS_LABELS = {
    "flag_for_alex": "Putting that on your plate…",
    "list_waiting_on_alex": "Checking what's waiting on you…",
    "clear_waiting_item": "Closing that out…",
}


def handle_tool_call(name: str, tool_input: dict) -> str:
    if name == "flag_for_alex":
        return flag_for_alex(tool_input.get("title", ""),
                             steps=tool_input.get("steps"),
                             link=tool_input.get("link", ""),
                             detail=tool_input.get("detail", ""))
    if name == "list_waiting_on_alex":
        return waiting_text()
    if name == "clear_waiting_item":
        return clear_item(tool_input.get("item_id", 0),
                          bool(tool_input.get("dropped")))
    return f"Unknown outbox tool: {name}"
