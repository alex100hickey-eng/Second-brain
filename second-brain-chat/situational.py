"""
situational.py — the RIGHT NOW block: CLARVIS's ambient awareness of Alex's moment.

WHY THIS EXISTS
---------------
The movie-assistant behavior Alex is chasing (JARVIS's "Sir, Ms. Potts is on the line",
E.V. continuously watching Peter's vitals) is never the result of the assistant *fetching*
context — it's the result of the context already being there when it speaks. CLARVIS had
all the sources (schedule, tasks, jobs, approvals, intake) but only as tools the model had
to elect to call, which means most turns were answered blind to the day they landed in.

Same architectural insight as person_profile.py, applied to NOW instead of ALWAYS:
don't make the model choose to look — put a compact, fresh snapshot of the moment into
every request. "Should I hit the gym tonight?" then gets answered by something that knows
it's Wednesday, that practice ran this morning, that a task is due tomorrow, and that two
approvals are sitting on the dashboard — without a single tool call.

DESIGN
------
- This module is PURE assembly + caching: formatting helpers and a TTL cache, no imports
  from the app, no network. app.py owns the actual sources (it knows their shapes) and
  passes a builder; everything here is testable offline with fixed clocks.
- Every source is fail-soft AT THE CALLER: a dead schedule source or unreachable Supabase
  costs that one section, never the chat request.
- The digest is budget-capped. Ambient context earns its place by being small; the moment
  it crowds the window it becomes noise. Sections are ordered by how likely they are to
  change what CLARVIS says next.
- The TTL cache (default 90s) means the per-turn cost of ambient awareness is ~zero: at
  most one rebuild every 90 seconds no matter how fast the conversation moves.
"""

import threading
import time
from datetime import datetime

TTL_SECONDS = 90
MAX_CHARS = 1600

_lock = threading.Lock()
_cache = {"at": 0.0, "text": None}


# ---------------------------------------------------------------- formatting
def _fmt_clock(dt: datetime) -> str:
    return dt.strftime("%-I:%M %p").lstrip("0")


def format_event_line(ev: dict, now: datetime) -> str:
    """One calendar event -> one line, annotated relative to now.

    Events are dicts shaped by app.get_today_events(): {title, start, end, all_day}.
    The annotation is the point: "(NOW)" and "(next up)" are what let the model speak
    like it's watching the day rather than reading a printout.
    """
    title = ev.get("title") or "(untitled)"
    if ev.get("all_day"):
        return f"{title} — all day"
    try:
        start = datetime.fromisoformat(ev["start"])
        end = datetime.fromisoformat(ev["end"]) if ev.get("end") else None
        if start.tzinfo is None:  # naive timestamps: compare in local wall-clock
            now = now.replace(tzinfo=None)
    except (ValueError, TypeError, KeyError):
        return title
    if end and end <= now:
        return f"{title} — {_fmt_clock(start)} (already happened)"
    if start <= now and (end is None or end > now):
        until = f" until {_fmt_clock(end)}" if end else ""
        return f"{title} — happening NOW{until}"
    return f"{title} — at {_fmt_clock(start)}"


def calendar_lines(events: list, now: datetime, max_lines: int = 8) -> list:
    """Today's events as annotated lines, earliest first, next-upcoming flagged.

    COMPACTED, because the schedule sits first and assemble() drops whole
    trailing sections when the budget runs out: a fully-dictated day is ~15
    blocks (wake, breakfast, two gym sessions, five classes, meals, 50/50,
    study, routine, sleep), which alone can crowd out the approval queue and
    the due-and-overdue list. What CLARVIS needs in every turn is what is
    happening NOW and what is next — the morning's finished blocks collapse to
    a count, and a long tail truncates with an explicit remainder so nothing
    reads as "that's the whole day"."""
    lines, done = [], 0
    upcoming_flagged = False
    for ev in events or []:
        line = format_event_line(ev, now)
        if "(already happened)" in line:
            done += 1
            continue
        if not upcoming_flagged and ("— at " in line):
            line += " (next up)"
            upcoming_flagged = True
        lines.append(line)
    remaining = 0
    if len(lines) > max_lines:
        remaining = len(lines) - max_lines
        lines = lines[:max_lines]
    if done:
        lines.insert(0, f"({done} earlier block{'s' if done != 1 else ''} already done)")
    if remaining:
        lines.append(f"…+{remaining} more later today (ask for the full grid)")
    return lines


def assemble(sections: list, now: datetime, max_chars: int = MAX_CHARS) -> str:
    """[(header, [line, ...]), ...] -> the digest text. Empty sections vanish.

    The time header is unconditional: even with nothing else to say, CLARVIS should
    always know what day and time it is — the movies' assistants never ask.
    """
    parts = [f"It is {now.strftime('%A, %B %-d, %Y — %-I:%M %p')}."]
    used = len(parts[0])
    for header, lines in sections:
        if not lines:
            continue
        block_lines = [header] + [f"- {ln}" for ln in lines]
        block = "\n".join(block_lines)
        if used + len(block) > max_chars:
            # Budget: drop whole trailing sections rather than truncating mid-line —
            # a half-sentence about an approval is worse than silence about it.
            break
        parts.append(block)
        used += len(block)
    return "\n\n".join(parts)


# ------------------------------------------------------------------- caching
def get_cached(builder, ttl: int = TTL_SECONDS, force: bool = False) -> str:
    """The digest, rebuilt at most once per TTL. `builder()` -> str.

    Compute happens OUTSIDE the lock: a slow source (Supabase hiccup) must never make
    concurrent chat turns queue behind it. The race this allows — two threads both
    rebuilding on the same expiry tick — costs one redundant build, which is cheaper
    than serializing every turn.
    """
    with _lock:
        fresh = _cache["text"] is not None and (time.time() - _cache["at"]) < ttl
        if fresh and not force:
            return _cache["text"]
    try:
        text = builder()
    except Exception as e:  # fail-soft: ambient context is never worth a failed reply
        print(f"situational: builder failed, serving stale/empty ({e})")
        with _lock:
            return _cache["text"] or ""
    with _lock:
        _cache["at"] = time.time()
        _cache["text"] = text
    return text


def invalidate() -> None:
    """Drop the cache (tests, or after something that obviously changes the situation)."""
    with _lock:
        _cache["at"] = 0.0
        _cache["text"] = None
