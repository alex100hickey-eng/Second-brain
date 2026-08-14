"""
august_tracker.py — the August money plan as live, queryable state.

The plan is a document; execution is a state machine. This module turns
`Money/August Execution Tracker.md` in the vault into steps CLARVIS can reason
about, so "what needs Alex right now" is a computed answer rather than something
he has to reconstruct by re-reading a plan.

WHY THE VAULT IS THE SOURCE OF TEXT
The tracker lives in Obsidian because Alex reads and edits it there (phone
included), it git-syncs every 10 minutes, and any Claude Code session can pick it
up without being re-briefed. A database would have been easier to write and
useless to a future session with no app running.

WHY COMPLETION ALSO LIVES IN SUPABASE
Alex talks to CLARVIS from his phone, which reaches the SERVER node — and the
server's vault is a pull-only mirror, so a checkbox ticked there would be wiped by
the next pull. Completion is therefore recorded in Supabase (shared by both nodes)
and UNIONED with the vault checkboxes. That makes two things equivalent:

    ticking the box in Obsidian   ==   telling CLARVIS "I named the service"

`reconcile_vault()` closes the loop from the local node, ticking boxes that
Supabase knows are done so the file Alex reads never goes stale.

BLOCKING IS STRUCTURAL
Each step declares `needs:` — the ids that must complete first. A step whose needs
are unmet is *blocked*, and blocked steps are never nudged about. This is the whole
point: Alex should hear about naming the service, not about the deliverability gate
that is seven days downstream of a mailbox that does not exist yet.
"""

import json
import os
import re
from datetime import date, datetime, timedelta

# Injected by init()
supabase = None
vault_path = None
LOCAL_TZ = None
runtime = "local"

TRACKER_REL = os.path.join("Money", "August Execution Tracker.md")
STATE_AGENT = "agent_state"
STATE_KEY = "august:completed"

# `- [x] **Title** `#id` · owner: alex · due: 2026-08-01 · needs: a, b`
# Fields are matched independently so hand-editing (reordering, extra prose, a
# stray emoji) degrades to "field missing", never to a crash or a silent misparse.
_STEP_RE = re.compile(r"^\s*-\s*\[([ xX])\]\s*(.+)$")
_ID_RE = re.compile(r"`#([a-z0-9][a-z0-9-]*)`")
_TITLE_RE = re.compile(r"\*\*(.+?)\*\*")
_OWNER_RE = re.compile(r"owner:\s*([A-Za-z]+)")
_DUE_RE = re.compile(r"due:\s*(\d{4}-\d{2}-\d{2})")
_NEEDS_RE = re.compile(r"needs:\s*([^·\n]*)")
# `daily: yes` marks a STREAK step — a habit that must happen every day until it's
# ticked, not a one-shot task. One-shot steps nudge twice and go quiet (correct);
# a streak that goes quiet on day 3 just stops happening, which is what killed the
# first warmup run.
_DAILY_RE = re.compile(r"daily:\s*(yes|true)\b", re.I)
# `started: YYYY-MM-DD` on a streak step — the day the run actually began. The warmup
# clock used to key off the MAILBOX being configured, which is not the same thing: the
# mailbox was live 08-01 while the domain had sent on exactly one day, so the clock
# read "running, delay no longer compounding" through eleven silent days. A warmup
# clock must measure sending, and only the streak step knows when sending began.
_STARTED_RE = re.compile(r"started:\s*(\d{4}-\d{2}-\d{2})")
# `needs:` is the last field on the line, so appended status prose lands inside it.
# Everything from the first spaced dash on is commentary, not dependencies.
_NEEDS_PROSE_RE = re.compile(r"\s[—–-]{1,2}\s")
# What a step id can look like (same shape as _ID_RE's capture). Anything else in a
# `needs:` list is prose that survived the cut, not a dependency.
_ID_TOKEN_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def init(supabase_client, vault_dir, local_tz=None, node_runtime="local"):
    global supabase, vault_path, LOCAL_TZ, runtime
    supabase = supabase_client
    vault_path = vault_dir
    LOCAL_TZ = local_tz
    runtime = node_runtime or "local"


def tracker_file() -> str:
    return os.path.join(vault_path or "", TRACKER_REL)


def _today() -> date:
    now = datetime.now(LOCAL_TZ) if LOCAL_TZ else datetime.now()
    return now.date()


# --- completion state (Supabase half) ---------------------------------------

def _load_completed() -> set:
    """Step ids marked done through CLARVIS. Fail-soft: an unreachable Supabase
    must degrade to "trust the vault checkboxes", never to an exception that takes
    the awareness pass down with it."""
    if supabase is None:
        return set()
    try:
        rows = (supabase.table("Agent Outputs").select("*")
                .eq("agent_name", STATE_AGENT).order("id", desc=True)
                .limit(50).execute().data or [])
        for row in rows:
            try:
                data = json.loads(row["output_text"])
            except (json.JSONDecodeError, TypeError):
                continue
            if data.get("key") == STATE_KEY:
                return set(data.get("done", []))
    except Exception:
        pass
    return set()


def _save_completed(done: set) -> bool:
    if supabase is None:
        return False
    try:
        payload = {"agent_name": STATE_AGENT,
                   "output_text": json.dumps({
                       "key": STATE_KEY, "done": sorted(done),
                       "updated_at": datetime.now().astimezone().isoformat()})}
        supabase.table("Agent Outputs").insert(payload).execute()
        return True
    except Exception:
        return False


# --- parsing -----------------------------------------------------------------

def _parse_line(raw: str) -> dict | None:
    m = _STEP_RE.match(raw)
    if not m:
        return None
    checked, rest = m.group(1).lower() == "x", m.group(2)
    id_m = _ID_RE.search(rest)
    if not id_m:
        return None                      # not a tracked step, just a bullet
    title_m = _TITLE_RE.search(rest)
    due_m = _DUE_RE.search(rest)
    owner_m = _OWNER_RE.search(rest)
    needs_raw = _NEEDS_RE.search(rest)
    needs = []
    if needs_raw:
        # Cut appended status prose first ("needs: mailbox-dns — ⚠️ day 1 was 08-03…").
        # Without this every word of that note became a phantom dependency, which
        # marked the step blocked, and blocked steps are deliberately never nudged —
        # so writing a status note under a step silently switched off its reminders.
        value = _NEEDS_PROSE_RE.split(needs_raw.group(1).strip(), maxsplit=1)[0]
        for token in re.split(r"[,\s]+", value):
            token = token.strip().strip("`#").rstrip(".,;:")
            # "—" is the written form of "nothing"; keep it out of the graph.
            if token and token not in ("—", "-", "none") and _ID_TOKEN_RE.match(token):
                needs.append(token)
    return {
        "id": id_m.group(1),
        "title": (title_m.group(1) if title_m else rest).strip(),
        "owner": (owner_m.group(1) if owner_m else "alex").lower(),
        "due": due_m.group(1) if due_m else None,
        "needs": needs,
        "daily": bool(_DAILY_RE.search(rest)),
        "started": (m2.group(1) if (m2 := _STARTED_RE.search(rest)) else None),
        "checked": checked,
    }


def load_steps() -> list:
    """Every step, with `done` unioned across the vault checkbox and Supabase, and
    `blocked` computed from unmet needs."""
    path = tracker_file()
    try:
        text = open(path, encoding="utf-8").read()
    except OSError:
        return []

    steps, section = [], ""
    for raw in text.splitlines():
        if raw.startswith("###"):
            section = raw.lstrip("#").strip()
            continue
        step = _parse_line(raw)
        if step:
            step["section"] = section
            steps.append(step)

    completed = _load_completed()
    by_id = {}
    for s in steps:
        s["done"] = s["checked"] or s["id"] in completed
        by_id[s["id"]] = s

    for s in steps:
        # A `needs:` naming a step that isn't in the document is a typo or a scrap of
        # prose, not a real dependency. Enforcing it would block the step forever, and
        # a silently-blocked step is the exact failure this tracker exists to prevent.
        # So unknown ids are recorded and surfaced, never used to block.
        s["unknown_needs"] = [n for n in s["needs"] if n not in by_id]
        unmet = [n for n in s["needs"] if n in by_id and not by_id[n]["done"]]
        s["blocked_by"] = unmet
        s["blocked"] = bool(unmet)
    return steps


# --- queries -----------------------------------------------------------------

def _is_overdue(step, today) -> bool:
    return bool(step["due"]) and step["due"] < today.isoformat()


def actionable(owner: str = "alex") -> list:
    """Steps this owner could actually start right now: not done, not blocked.

    Sorted by due date so the answer to "what should I do" is the first row rather
    than a list to triage."""
    steps = [s for s in load_steps()
             if not s["done"] and not s["blocked"] and s["owner"] == owner]
    return sorted(steps, key=lambda s: (s["due"] or "9999-12-31", s["id"]))


def blocked(owner: str = "alex") -> list:
    return [s for s in load_steps()
            if not s["done"] and s["blocked"] and s["owner"] == owner]


def status() -> dict:
    """One shot of everything: counts, what's next, what's overdue, the clock."""
    steps = load_steps()
    today = _today()
    done = [s for s in steps if s["done"]]
    open_steps = [s for s in steps if not s["done"]]
    ready = [s for s in open_steps if not s["blocked"]]
    return {
        "total": len(steps),
        "done": len(done),
        "open": len(open_steps),
        "ready": ready,
        "blocked": [s for s in open_steps if s["blocked"]],
        "overdue": sorted([s for s in ready if _is_overdue(s, today)],
                          key=lambda s: s["due"]),
        "due_today": [s for s in ready if s["due"] == today.isoformat()],
        "next_for_alex": (actionable("alex") or [None])[0],
        "next_for_clarvis": (actionable("clarvis") or [None])[0],
        "clock": warmup_clock(),
    }


def warmup_clock() -> dict:
    """The one genuinely time-boxed thing in the plan.

    Sends cannot start until 7 days of mailbox warmup have elapsed, so until the
    mailbox exists the Aug 31 deadline is eroding by a day per day. Making that
    visible is most of the value — it's the difference between "I'll get to it" and
    "this costs me a selling day"."""
    steps = {s["id"]: s for s in load_steps()}
    mailbox = steps.get("mailbox-dns")
    warmup = steps.get("warmup-daily") or {}
    deadline = date(2026, 8, 31)
    today = _today()
    # The run is anchored to the day SENDING began, declared on the streak step. A live
    # mailbox is a prerequisite for warmup, never evidence of it — conflating the two is
    # how this reported "delay is no longer compounding" through eleven days of silence.
    started_str = warmup.get("started")
    started = None
    if started_str:
        try:
            started = date.fromisoformat(started_str)
        except ValueError:
            started = None
    if not mailbox or not mailbox.get("done") or (started is None
                                                  and not warmup.get("done")):
        earliest_send = today + timedelta(days=7)
        return {
            "warmup_started": False,
            "earliest_send": earliest_send.isoformat(),
            "selling_days_if_started_today": max(0, (deadline - earliest_send).days),
            "note": ("Warmup sending has not started. Every day it slips costs one "
                     "selling day before Aug 31."),
        }
    started = started or _completed_on("mailbox-dns") or today
    send_date = started + timedelta(days=7)
    return {
        "warmup_started": True,
        "started": started.isoformat(),
        "earliest_send": send_date.isoformat(),
        "days_remaining": max(0, (send_date - today).days),
        "selling_days": max(0, (deadline - max(send_date, today)).days),
        "note": "Warmup clock is running; delay is no longer compounding.",
    }


def _completed_on(step_id: str) -> date | None:
    """When a step was marked done through CLARVIS, if we recorded it."""
    if supabase is None:
        return None
    try:
        rows = (supabase.table("Agent Outputs").select("*")
                .eq("agent_name", STATE_AGENT).order("id", desc=True)
                .limit(50).execute().data or [])
        for row in rows:
            try:
                data = json.loads(row["output_text"])
            except (json.JSONDecodeError, TypeError):
                continue
            if data.get("key") == STATE_KEY and step_id in data.get("done", []):
                stamp = data.get("updated_at", "")
                return datetime.fromisoformat(stamp).date() if stamp else None
    except Exception:
        pass
    return None


# --- mutation ----------------------------------------------------------------

def mark_done(step_id: str) -> dict:
    steps = {s["id"]: s for s in load_steps()}
    if step_id not in steps:
        return {"ok": False, "error": f"No step '{step_id}'. Known: "
                                      f"{', '.join(sorted(steps)) or '(tracker unreadable)'}"}
    if steps[step_id]["done"]:
        return {"ok": True, "already": True, "step": steps[step_id]["title"]}

    done = _load_completed()
    done.add(step_id)
    saved = _save_completed(done)
    mirrored = reconcile_vault()

    after = {s["id"]: s for s in load_steps()}
    unblocked = [s["title"] for s in after.values()
                 if not s["done"] and not s["blocked"] and step_id in s["needs"]]
    return {"ok": True, "step": steps[step_id]["title"], "saved": saved,
            "vault_updated": mirrored, "unblocked": unblocked}


def reconcile_vault() -> bool:
    """Tick vault checkboxes for anything Supabase knows is done.

    Only from the local node: the server's vault is a pull-only mirror, so writing
    there would be silently reverted on the next sync and could conflict the pull.
    Returns True only if the file actually changed."""
    if runtime != "local":
        return False
    completed = _load_completed()
    if not completed:
        return False
    path = tracker_file()
    try:
        text = open(path, encoding="utf-8").read()
    except OSError:
        return False

    out, changed = [], False
    for raw in text.splitlines():
        step = _parse_line(raw)
        if step and not step["checked"] and step["id"] in completed:
            raw = re.sub(r"^(\s*-\s*)\[ \]", r"\1[x]", raw, count=1)
            changed = True
        out.append(raw)
    if not changed:
        return False
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(out) + ("\n" if text.endswith("\n") else ""))
        return True
    except OSError:
        return False


# --- rendering ---------------------------------------------------------------

def summary_text(limit: int = 6) -> str:
    """Human-readable status, used by the chat tool and the morning brief."""
    st = status()
    if not st["total"]:
        return ("The August tracker isn't readable — expected it at "
                f"`{TRACKER_REL}` in the vault.")

    lines = [f"**August plan — {st['done']}/{st['total']} done.**"]
    clock = st["clock"]
    if not clock["warmup_started"]:
        lines.append(f"⏳ Warmup hasn't started. Earliest send {clock['earliest_send']} "
                     f"→ {clock['selling_days_if_started_today']} selling days before Aug 31. "
                     f"Each day of delay costs one.")
    else:
        lines.append(f"✅ Warmup running — sends open {clock['earliest_send']} "
                     f"({clock['days_remaining']}d), {clock['selling_days']} selling days.")

    if st["overdue"]:
        lines.append("\n**Overdue:**")
        lines += [f"- {s['title']} (due {s['due']})" for s in st["overdue"][:limit]]

    nxt = st["next_for_alex"]
    if nxt:
        lines.append(f"\n**Your next step:** {nxt['title']}"
                     + (f" — due {nxt['due']}" if nxt["due"] else ""))

    ready_alex = [s for s in st["ready"] if s["owner"] == "alex"]
    if len(ready_alex) > 1:
        lines.append("\n**Also unblocked for you:**")
        lines += [f"- {s['title']}" for s in ready_alex[1:limit]]

    ready_cl = [s for s in st["ready"] if s["owner"] == "clarvis"]
    if ready_cl:
        lines.append("\n**Waiting on me:** " + ", ".join(s["title"] for s in ready_cl[:limit]))

    if st["blocked"]:
        lines.append(f"\n{len(st['blocked'])} step(s) still blocked upstream.")
    return "\n".join(lines)


# --- nudge sourcing (consumed by proactive.py) -------------------------------

def nudges_due() -> list:
    """Deterministic nudge candidates: [{key, title, body, priority}].

    Only ACTIONABLE, ALEX-OWNED steps ever appear here. Blocked steps are silent by
    design — a nudge about something he cannot start is noise, and noise is how a
    proactive assistant gets muted. proactive.py still applies quiet hours, the
    daily cap and per-key dedup on top of this."""
    st = status()
    today = _today().isoformat()
    out = []

    # A streak step is a habit, not a task: it needs a nudge every day until it's
    # ticked. It gets its own branch (its due date is the END of the run, so the
    # overdue branch would stay silent through every day that actually matters) and
    # is marked recurring, which exempts it from the twice-then-silence cap while
    # proactive.py's 20-hour spacing still holds it to one a day.
    streaks = {s["id"] for s in st["ready"]
               if s.get("daily") and s["owner"] == "alex"}
    for s in st["ready"]:
        if s["id"] not in streaks:
            continue
        out.append({
            "key": f"august:streak:{s['id']}",
            "title": f"📨 Today's {s['title'][:45]}",
            "body": "\n".join(x for x in (
                f"Why it matters: {_why(s)}",
                _do(s),
                "Miss a day and the run restarts. Tick it in the vault when done.",
            ) if x),
            "priority": "high",
            "recurring": True,
        })

    # Keys carry NO date on purpose: one step = one concern, and proactive.py's
    # per-concern cap (2 per window) means a step nudges at most twice, then goes
    # silent instead of nagging daily. due-today and overdue share the step's key
    # so the pair together can't exceed the cap either.
    for s in st["overdue"]:
        if s["owner"] != "alex" or s["id"] in streaks:
            continue
        days_late = 0
        try:
            days_late = (_today() - date.fromisoformat(s["due"])).days
        except (TypeError, ValueError):
            pass
        late = f"{days_late}d LATE" if days_late > 0 else "LATE"
        body = "\n".join(x for x in (
            f"Was due {s['due']}.",
            f"Why it matters: {_why(s)}",
            _do(s),
            "Done already? Tick it in the vault tracker and this goes quiet.",
        ) if x)
        out.append({
            "key": f"august:step:{s['id']}",
            "title": f"🔴 {late} — {s['title'][:55]}",
            "body": body,
            "priority": "high",
        })

    for s in st["due_today"]:
        if (s["owner"] != "alex" or s["id"] in streaks
                or any(o["id"] == s["id"] for o in st["overdue"])):
            continue
        body = "\n".join(x for x in (
            f"Why it matters: {_why(s)}",
            _do(s),
        ) if x)
        out.append({
            "key": f"august:step:{s['id']}",
            "title": f"📅 Due TODAY — {s['title'][:55]}",
            "body": body,
            "priority": "default",
        })

    # The compounding cost of not starting warmup is the single most important
    # thing to keep in front of him. Dateless key: the concern cap lets it fire
    # twice per window, not daily forever.
    clock = st["clock"]
    if not clock["warmup_started"] and st["done"] < st["total"]:
        out.append({
            "key": "august:clock",
            "title": f"⏳ {clock['selling_days_if_started_today']} selling days left "
                     "— warmup unstarted",
            "body": (f"Start today → first sends {clock['earliest_send']}. "
                     f"Each day of delay costs one selling day.\n"
                     "Do: splitframestudio Gmail → Drafts → send 3 warmup emails, "
                     "spaced; reply to each from your gmail. ~5 min."),
            "priority": "default",
        })
    return out


def _why(step) -> str:
    """One line on why this step matters — a nudge that only names a task gets
    dismissed; one that names the consequence gets done."""
    reasons = {
        "name-service": "Blocks the domain, the mailbox and the whole warmup clock.",
        "buy-domain": "The mailbox can't exist without it.",
        "mailbox-dns": "Starts the 7-day warmup. Until it's live, Aug 31 erodes daily.",
        "taste-pass-1": "Everything rests on the first drop being good. Worth knowing now.",
        "qualify-list": "Wave 1 sends only go to qualified rows.",
        "stripe": "Needed before anyone can pay you.",
        "warmup-daily": "Skipping days restarts the clock and risks the domain.",
        "deliverability-gate": "Hard go/no-go before the first real send.",
    }
    return reasons.get(step["id"], "On the critical path for Aug 31.")


def _do(step) -> str:
    """The exact next physical action, when it's known. A nudge that says WHAT to
    click gets done in the gap between phone-unlock and doom-scroll; one that says
    'handle Stripe' gets postponed."""
    actions = {
        "stripe": "Do: dashboard.stripe.com/register — sole prop, personal checking, "
                  "ACH on, tax auto-transfer 25-30%. ~15 min.",
        "warmup-daily": "Do: open splitframestudio Gmail → Drafts → send 3, spaced "
                        "through the day; reply to each from your gmail. ~5 min.",
        "postmaster-check": "Do: postmaster.google.com → splitframestudio.com → check "
                            "data is populating. ~2 min.",
        "deliverability-gate": "Do: mail-tester.com → send from splitframestudio Gmail "
                               "→ needs 10/10 before Wave 1. ~5 min.",
        "taste-pass-1": "Do: read the pack in vault Money/Clients/, mark up anything "
                        "off-voice, say 'approved' or list changes. ~20 min.",
    }
    return actions.get(step["id"], "")


# --- prospect list -----------------------------------------------------------

def prospect_summary() -> dict:
    """Counts from the 97-row prospect CSV. Read-only, and fail-soft to zeros so a
    missing tracker degrades one HUD panel instead of the whole page."""
    path = os.path.join(vault_path or "", "Money", "prospect-tracker.csv")
    out = {"total": 0, "by_status": {}, "qualified": 0, "sent": 0, "replied": 0}
    try:
        import csv
        with open(path, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                out["total"] += 1
                st = (row.get("status") or "candidate").strip() or "candidate"
                out["by_status"][st] = out["by_status"].get(st, 0) + 1
                if st == "qualified":
                    out["qualified"] += 1
                if (row.get("sent_date") or "").strip():
                    out["sent"] += 1
                if (row.get("replied") or "").strip():
                    out["replied"] += 1
    except (OSError, ImportError, ValueError):
        pass
    return out


# --- HUD feed ----------------------------------------------------------------

# Short display names for the tracker's "### Gate X — ..." headings. The HUD has
# ~30 characters of panel width; the full heading is written for a human reading
# the document, not for a 355px column.
def _gate_label(section: str) -> str:
    s = (section or "Other").split("—")[0].strip()
    return s or "Other"


def deck_data() -> dict:
    """Everything the August HUD tab renders, in one shot.

    Shaped for display rather than for correctness-of-record: pre-sorted,
    pre-truncated, already grouped by gate. The HUD should render a payload, not
    compute a plan."""
    steps = load_steps()
    st = status()
    today = _today()
    deadline = date(2026, 8, 31)

    def brief(s):
        return {"id": s["id"], "title": s["title"], "due": s["due"],
                "owner": s["owner"], "why": _why(s),
                "overdue": _is_overdue(s, today),
                "blocked_by": s["blocked_by"]}

    gates, order = {}, []
    for s in steps:
        g = _gate_label(s.get("section"))
        if g not in gates:
            gates[g] = {"name": g, "done": 0, "total": 0}
            order.append(g)
        gates[g]["total"] += 1
        if s["done"]:
            gates[g]["done"] += 1

    ready_alex = [s for s in st["ready"] if s["owner"] == "alex"]
    return {
        "clock": st["clock"],
        "days_to_deadline": max(0, (deadline - today).days),
        "counts": {"done": st["done"], "total": st["total"],
                   "ready": len(st["ready"]), "blocked": len(st["blocked"])},
        "next": brief(st["next_for_alex"]) if st["next_for_alex"] else None,
        "needs_you": [brief(s) for s in ready_alex],
        "clarvis_queue": [brief(s) for s in st["ready"] if s["owner"] == "clarvis"],
        "blocked": [brief(s) for s in st["blocked"]],
        "gates": [gates[g] for g in order],
        "prospects": prospect_summary(),
    }


# --- tool surface ------------------------------------------------------------

TOOL_SCHEMAS = [
    {
        "name": "check_august_plan",
        "description": (
            "Live status of the August money plan: what Alex should do next, what is "
            "overdue, what is blocked and where the 7-day mailbox warmup clock stands. "
            "Reads the tracker in the vault. Use whenever Alex asks what needs him, "
            "what's next, where things stand, or how the money plan is going."),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "complete_august_step",
        "description": (
            "Mark a step of the August plan done, by its id (e.g. 'name-service', "
            "'mailbox-dns'). Use when Alex says he has finished something. Reports "
            "what this unblocks. Call check_august_plan first if unsure of the id."),
        "input_schema": {
            "type": "object",
            "properties": {"step_id": {"type": "string",
                                       "description": "Step id, e.g. 'name-service'."}},
            "required": ["step_id"],
        },
    },
]

TOOL_STATUS_LABELS = {
    "check_august_plan": "Checking the August plan…",
    "complete_august_step": "Updating the August plan…",
}


def check_august_plan() -> str:
    return summary_text()


def complete_august_step(step_id: str) -> str:
    res = mark_done((step_id or "").strip().strip("#`"))
    if not res.get("ok"):
        return res.get("error", "Couldn't update that step.")
    if res.get("already"):
        return f"'{res['step']}' was already done."
    msg = f"✅ Marked done: {res['step']}."
    if res.get("unblocked"):
        msg += " That unblocks: " + ", ".join(res["unblocked"]) + "."
    if not res.get("saved"):
        msg += " (Warning: couldn't persist to Supabase — may not survive a restart.)"
    return msg + "\n\n" + summary_text()
