"""Parse Alex's training-app sync snapshots into schedule events and workout data.

The training app (the "Weekly Schedule" PWA at luminous-madeleine-bf89fa.netlify.app)
mirrors its localStorage to a sync backend as one JSON snapshot:

    {"rev": "<opaque>", "keys": {"<safeKey>": "<JSON-encoded string or null>", ...}}

where safeKey is the localStorage key with the characters . $ # [ ] / replaced by "_"
(Firebase field-name rules; our endpoint keeps the same wire format so the app needs
no changes). The keys that matter:

    weeklySchedule.v1   {"<slot>|<day>": "cell text"}  slot 0-47, day 0=Sun..6=Sat
    weeklyWorkouts.v1   {"0".."6": "workout card text"} Sun..Sat
    dailyRoutines.v1    {"morning": str, "night": str}
    warmupRoutine.v1    plain string
    workoutLibrary.v2   {"<catIdx>": {"sel": int, "pages": [{title, body} | table]}}

Grid semantics (must match the app's rendering exactly): slot r covers
3:00 AM + r*30min for 30 minutes, so a day column spans 3:00 AM of its day to
3:00 AM of the NEXT day — slots 42-47 are the after-midnight tail. Events are
therefore computed as absolute datetimes anchored at the column's date + 3:00,
which naturally rolls past midnight, and "events for date D" unions the
overlapping blocks from column D-1 and column D.

Pure assembly module: no I/O, no globals mutated. The app owns storage and
fetching; everything here takes parsed snapshot data and a datetime.
"""

import json
from datetime import datetime, date, time, timedelta

DAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
LIBRARY_CATS = ["Lifts", "Good Drills", "Bag shooting", "50/50", "Conditioning",
                "Group workouts", "Court movement"]
N_SLOTS = 48
SLOT_MIN = 30
DAY_START_MIN = 3 * 60  # each column starts at 3:00 AM

_SAFE = {".": "_", "$": "_", "#": "_", "[": "_", "]": "_", "/": "_"}


def _safe_key(k: str) -> str:
    return "".join(_SAFE.get(ch, ch) for ch in k)


def _get_key(snapshot: dict, storage_key: str):
    """Pull one localStorage value out of a snapshot and JSON-decode it."""
    keys = snapshot.get("keys") if isinstance(snapshot, dict) else None
    if not isinstance(keys, dict):
        return None
    raw = keys.get(_safe_key(storage_key))
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


def parse_snapshot(snapshot: dict) -> dict:
    """Normalize a raw sync snapshot. Every section is fail-soft: bad or missing
    keys become empty structures, never exceptions."""
    grid = {}
    sched = _get_key(snapshot, "weeklySchedule.v1")
    if isinstance(sched, dict):
        for key, text in sched.items():
            try:
                r_s, c_s = key.split("|", 1)
                r, c = int(r_s), int(c_s)
            except (ValueError, AttributeError):
                continue
            if 0 <= r < N_SLOTS and 0 <= c < 7 and isinstance(text, str) and text.strip():
                grid[(r, c)] = text.strip()

    workouts = {}
    w = _get_key(snapshot, "weeklyWorkouts.v1")
    if isinstance(w, dict):
        for k, v in w.items():
            try:
                i = int(k)
            except (ValueError, TypeError):
                continue
            if 0 <= i < 7 and isinstance(v, str) and v.strip():
                workouts[i] = v.strip()

    routines = {}
    r = _get_key(snapshot, "dailyRoutines.v1")
    if isinstance(r, dict):
        for k in ("morning", "night"):
            if isinstance(r.get(k), str) and r[k].strip():
                routines[k] = r[k].strip()

    warmup = _get_key(snapshot, "warmupRoutine.v1")
    if not isinstance(warmup, str):
        warmup = ""

    library = {}
    lib = _get_key(snapshot, "workoutLibrary.v2")
    if isinstance(lib, dict):
        for k, cat in lib.items():
            try:
                i = int(k)
            except (ValueError, TypeError):
                continue
            if not (0 <= i < len(LIBRARY_CATS)) or not isinstance(cat, dict):
                continue
            pages = []
            for p in cat.get("pages") or []:
                if not isinstance(p, dict):
                    continue
                title = p.get("title") or "Untitled"
                if p.get("type") == "table":
                    rows = ["\t".join(str(c) for c in row)
                            for row in (p.get("rows") or []) if isinstance(row, list)]
                    header = "\t".join(str(c) for c in (p.get("columns") or []))
                    body = header + "\n" + "\n".join(rows) if header else "\n".join(rows)
                else:
                    body = p.get("body") or ""
                pages.append({"title": str(title), "body": str(body)})
            if pages:
                library[LIBRARY_CATS[i]] = pages

    return {
        "rev": (snapshot or {}).get("rev") if isinstance(snapshot, dict) else None,
        "grid": grid,
        "workouts": workouts,
        "routines": routines,
        "warmup": warmup.strip(),
        "library": library,
    }


def day_index(d: date) -> int:
    """Python weekday (Mon=0) -> app column index (Sun=0)."""
    return (d.weekday() + 1) % 7


def column_events(grid: dict, col_date: date) -> list:
    """Merge one column's contiguous identical cells into absolute-datetime events."""
    col = day_index(col_date)
    slots = sorted((r, txt) for (r, c), txt in grid.items() if c == col)
    events = []
    anchor = datetime.combine(col_date, time(0, 0)) + timedelta(minutes=DAY_START_MIN)
    run_start = None
    prev_r = None
    prev_txt = None
    for r, txt in slots + [(None, None)]:  # sentinel flushes the final run
        contiguous = prev_r is not None and r == prev_r + 1 and txt == prev_txt
        if not contiguous:
            if prev_r is not None:
                events.append({
                    "title": prev_txt,
                    "start": anchor + timedelta(minutes=run_start * SLOT_MIN),
                    "end": anchor + timedelta(minutes=(prev_r + 1) * SLOT_MIN),
                })
            run_start = r
        prev_r, prev_txt = r, txt
    return events


def events_for_date(parsed: dict, d: date) -> list:
    """All schedule blocks overlapping calendar date d, sorted by start time.
    Checks column d-1 too, since columns run 3AM-3AM and can spill past midnight."""
    grid = parsed.get("grid") or {}
    day_lo = datetime.combine(d, time(0, 0))
    day_hi = day_lo + timedelta(days=1)
    out = []
    for col_date in (d - timedelta(days=1), d):
        for ev in column_events(grid, col_date):
            if ev["start"] < day_hi and ev["end"] > day_lo:
                out.append(ev)
    out.sort(key=lambda e: e["start"])
    return out


def clock(dt: datetime) -> str:
    return dt.strftime("%-I:%M%p").lower()


def current_position(now: datetime):
    """(column date, fractional slot) for `now` in the grid's own frame.

    Before 3 AM you are still inside the PREVIOUS column — that's the frame the
    app draws in, so a 1 AM "you are here" marker belongs at the bottom of
    yesterday's column, not the top of today's."""
    anchor = datetime.combine(now.date(), time(0, 0)) + timedelta(minutes=DAY_START_MIN)
    if now.replace(tzinfo=None) < anchor:
        anchor -= timedelta(days=1)
    slot = (now.replace(tzinfo=None) - anchor).total_seconds() / 60.0 / SLOT_MIN
    return anchor.date(), slot


def format_block(ev: dict, ref: date) -> str:
    """One block as a line, qualified when it doesn't sit inside ref's own day.

    Columns run 3AM-3AM, so a night block legitimately belongs to two calendar
    dates: viewed on Monday, both Sunday-night's 11pm-3am and Monday-night's
    11pm-3am overlap the day and would otherwise render as the same line twice.
    The qualifier says which night is which."""
    start, end = ev["start"], ev["end"]
    line = "%s–%s  %s" % (clock(start), clock(end), ev["title"])
    if start.date() < ref:
        return line + " (from %s night)" % start.strftime("%a")
    if end.date() > ref:
        return line + " (into %s)" % end.strftime("%a")
    return line


def workout_for_date(parsed: dict, d: date) -> str:
    return (parsed.get("workouts") or {}).get(day_index(d), "")


def schedule_summary(parsed: dict) -> str:
    """Compact whole-week text view (for the model's get_training_schedule tool)."""
    lines = []
    grid = parsed.get("grid") or {}
    for c, name in enumerate(DAYS):
        # borrow column_events via a synthetic date with the right column index:
        # any date works as an anchor because we only print clock times here.
        anchor = date(2000, 1, 2 + c)  # 2000-01-02 was a Sunday
        evs = column_events(grid, anchor)
        if not evs:
            continue
        lines.append(name + ":")
        for ev in evs:
            lines.append("  %s–%s  %s" % (clock(ev["start"]), clock(ev["end"]), ev["title"]))
    return "\n".join(lines) if lines else "(schedule grid is empty)"
