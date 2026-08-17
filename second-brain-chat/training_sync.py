"""Sync backend + tools for Alex's training app (the Weekly Schedule PWA).

The training app at https://luminous-madeleine-bf89fa.netlify.app keeps Alex's
real schedule — his week blocked out in 30-minute slots — plus workout cards,
daily routines, warmup, and a workout library. Its built-in cross-device sync
just PUTs/GETs one JSON snapshot at <base-url>/trainingDashboard.json (it was
written for Firebase's REST shape). This module makes CLARVIS that base URL, so
the app needs zero changes: Alex pastes the CLARVIS sync URL into the app's
Sync dialog once per device and every edit lands here within ~1 second.

Wire protocol (must stay Firebase-shaped):
    GET  -> the stored snapshot JSON, or the literal `null` if nothing stored
            (Firebase returns null for an empty path; the app then pushes its
            local data up, which is exactly how first-connect migration works)
    PUT  -> body is {"rev": str, "keys": {safeKey: JSON-string-or-null}};
            store it, reply 200 (the app only checks response.ok)
    The app's fetch sends no custom headers but PUT always preflights, so the
    endpoint must answer OPTIONS with CORS headers for the app's origin.

Auth is a capability URL: /training-sync/<token>/trainingDashboard.json where
the token is TRAINING_SYNC_TOKEN or, when unset, an HMAC derived from
ACCESS_CODE — no new secret to provision, and asking CLARVIS in (gated) chat
for the URL is the intended way to retrieve it. Rotating ACCESS_CODE therefore
changes the URL and the app must be re-pointed; the derived token does not
reveal ACCESS_CODE.

Durability: snapshots update ONE Supabase "Agent Outputs" row in place
(agent_name "training_dashboard" — deliberately absent from
retention.RETENTION_DAYS, so never swept), following intake._save_state's
remembered-row-id pattern so the table doesn't grow and polls never re-read
Supabase (in-memory cache serves every GET; Supabase is touched on writes and
once at first read after boot).
"""

import copy
import hmac
import hashlib
import json
import os
import re
import threading
import time
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import training_schedule

AGENT_NAME = "training_dashboard"
MAX_SNAPSHOT_BYTES = 200_000  # same cap as draft_store; real snapshots are ~10-50KB
MAX_ROW_BYTES = 250_000       # snapshot + undo history together (his is ~18KB each)
LOCAL_TZ = ZoneInfo("America/New_York")
# Supabase retries (hydrate + failed persist) are rate-limited: the app GET-polls
# every 8s per open device, and each attempt is blocking network I/O under _lock.
# Without a cooldown, one Supabase outage turns every poll into a hung request.
RETRY_COOLDOWN_S = 60

# The PWA's origin, for CORS. Env-overridable in case the app ever moves.
APP_ORIGIN = os.environ.get(
    "TRAINING_APP_ORIGIN", "https://luminous-madeleine-bf89fa.netlify.app"
)
PUBLIC_BASE_URL = os.environ.get(
    "PUBLIC_BASE_URL", "https://clarvis.178.156.209.40.sslip.io"
)

_supabase = None
_lock = threading.Lock()
# _state["loaded"] flips True after the first Supabase read attempt (even a
# failed one must not be retried per-request — polls arrive every 8s).
_state = {"loaded": False, "row_id": None, "snapshot": None, "saved_at": None,
          "dirty": False,           # in-memory is newer than Supabase (persist failed)
          # Separate cooldowns on purpose: one shared floor would let a hydrate
          # attempt push the persist retry another minute out every time, so an
          # un-saved edit could stay un-saved indefinitely while Supabase is flaky.
          "next_hydrate_at": 0.0,
          "next_persist_at": 0.0,
          "undo": []}              # previous snapshots, oldest first (see UNDO_DEPTH)
_on_update = None  # app-provided callback: bust downstream caches on new data


def init(supabase_client, on_update=None):
    global _supabase, _on_update
    _supabase = supabase_client
    _on_update = on_update


def sync_token() -> str:
    explicit = os.environ.get("TRAINING_SYNC_TOKEN", "").strip()
    if explicit:
        return explicit
    access_code = os.environ.get("ACCESS_CODE") or os.environ.get("JARVIS_PASSWORD") or ""
    if not access_code:
        return "local-dev"  # gate is off in this configuration anyway
    return hmac.new(
        access_code.encode("utf-8"), b"training-sync-v1", hashlib.sha256
    ).hexdigest()[:24]


def sync_url() -> str:
    """The base URL Alex pastes into the app's Sync dialog (the app appends
    /trainingDashboard.json itself)."""
    return f"{PUBLIC_BASE_URL}/training-sync/{sync_token()}"


def token_matches(candidate: str) -> bool:
    # Compare BYTES, not str: hmac.compare_digest raises TypeError on non-ASCII
    # str, and the token arrives from a URL path Flask percent-decodes as UTF-8 —
    # so any non-ASCII byte a scanner sends would 500 instead of 404.
    return hmac.compare_digest(
        str(candidate or "").encode("utf-8", "replace"),
        sync_token().encode("utf-8", "replace"),
    )


def _hydrate_locked():
    """Cold-start read of the stored snapshot. Caller must hold _lock.

    Retried (on a cooldown) until it SUCCEEDS rather than once, because a boot
    during a Supabase blip would otherwise leave us permanently empty — and an
    empty server tells the app "nothing stored", at which point whichever device
    polls first pushes its possibly-stale data up as the new truth. Never
    overwrites an in-memory snapshot: once the app has pushed, memory is the
    newer copy and a late hydrate must not roll it back."""
    if _supabase is None or _state["loaded"]:
        return
    now = time.monotonic()
    if now < _state["next_hydrate_at"]:
        return
    _state["next_hydrate_at"] = now + RETRY_COOLDOWN_S
    try:
        rows = (
            _supabase.table("Agent Outputs")
            .select("id,output_text")
            .eq("agent_name", AGENT_NAME)
            .order("id", desc=True)
            .limit(1)
            .execute()
            .data
        )
        if rows:
            stored = json.loads(rows[0]["output_text"])
            _state["row_id"] = rows[0]["id"]
            if _state["snapshot"] is None:
                _state["snapshot"] = stored.get("snapshot")
                _state["saved_at"] = stored.get("saved_at")
                undo = stored.get("undo")
                _state["undo"] = undo if isinstance(undo, list) else []
        _state["loaded"] = True
    except Exception as e:
        print(f"[training_sync] hydrate failed (will retry): {e}")


def get_snapshot():
    """The latest raw snapshot dict, or None if the app has never synced."""
    with _lock:
        _hydrate_locked()
        if _state["dirty"] and time.monotonic() >= _state["next_persist_at"]:
            # A previous persist failed; the app's 8s poll gives us retry ticks,
            # but only one attempt per cooldown — each is blocking network I/O.
            _persist_locked()
        return _state["snapshot"]


def last_synced_at():
    with _lock:
        _hydrate_locked()
        return _state["saved_at"]


def validate_snapshot(payload) -> str:
    """Return an error string, or "" if the payload is a well-formed snapshot."""
    if not isinstance(payload, dict):
        return "body must be a JSON object"
    if not isinstance(payload.get("rev"), str) or not payload["rev"]:
        return "missing rev"
    keys = payload.get("keys")
    if not isinstance(keys, dict):
        return "missing keys object"
    for k, v in keys.items():
        if v is not None and not isinstance(v, str):
            return f"key {k!r} must be a JSON-encoded string or null"
    return ""


def _persist_locked() -> str:
    """Write current in-memory state to Supabase. Caller must hold _lock.
    Returns an error string or ""; failure marks the state dirty for retry."""
    body = json.dumps({"snapshot": _state["snapshot"], "saved_at": _state["saved_at"],
                       "undo": _state["undo"]})
    if len(body) > MAX_ROW_BYTES:
        # Undo history is the disposable part — never let it push the live
        # snapshot past what the row can hold.
        body = json.dumps({"snapshot": _state["snapshot"],
                           "saved_at": _state["saved_at"], "undo": []})
    try:
        if _state["row_id"] is not None:
            _supabase.table("Agent Outputs").update({"output_text": body}).eq(
                "id", _state["row_id"]
            ).execute()
        else:
            res = (
                _supabase.table("Agent Outputs")
                .insert({"agent_name": AGENT_NAME, "output_text": body})
                .execute()
            )
            data = getattr(res, "data", None) or []
            if data and isinstance(data[0], dict) and "id" in data[0]:
                _state["row_id"] = data[0]["id"]
        _state["dirty"] = False
        return ""
    except Exception as e:
        _state["dirty"] = True
        _state["next_persist_at"] = time.monotonic() + RETRY_COOLDOWN_S
        print(f"[training_sync] Supabase persist failed (kept in memory): {e}")
        return str(e)


def store_snapshot(payload) -> str:
    """Persist a validated snapshot. Returns an error string or "".
    In-memory state updates even if Supabase is down — the schedule stays
    usable, durability catches up via the dirty-flag retry in get_snapshot.
    The caller MUST surface a non-empty return to the app: a failed persist that
    reports success would be silently rolled back on the next restart."""
    with _lock:
        _hydrate_locked()
        _state["snapshot"] = payload
        _state["saved_at"] = datetime.now(LOCAL_TZ).isoformat()
        err = _persist_locked()
    if _on_update is not None:
        try:
            _on_update()
        except Exception:
            pass
    return err


def parsed():
    """Parsed view of the latest snapshot, or None if never synced."""
    snap = get_snapshot()
    if snap is None:
        return None
    return training_schedule.parse_snapshot(snap)


def _staleness_note() -> str:
    """A warning line when the app hasn't pushed in a while (sync likely
    disconnected on his devices), else ""."""
    saved = last_synced_at()
    if not saved:
        return ""
    try:
        stamp = datetime.fromisoformat(saved)
        if stamp.tzinfo is None:  # defensive: rows written by hand or old code
            stamp = stamp.replace(tzinfo=LOCAL_TZ)
        age = datetime.now(LOCAL_TZ) - stamp
    except ValueError:
        return ""
    if age > timedelta(hours=48):
        days = age.days
        return (
            f"\n\n(Heads up: the training app last synced {days} day(s) ago — "
            "if Alex has edited his schedule since, his devices may have lost "
            "the sync connection.)"
        )
    return ""


# ---- model-facing tools -------------------------------------------------

_DAY_ALIASES = {d.lower(): i for i, d in enumerate(training_schedule.DAYS)}


def _resolve_day_name(d) -> str:
    return training_schedule.DAYS[training_schedule.day_index(d)]


def _resolve_day(day: str, now=None):
    """'today'/'tomorrow'/weekday-name -> a concrete date (or None for week view)."""
    now = now or datetime.now(LOCAL_TZ)
    d = (day or "").strip().lower()
    if d in ("", "week", "all"):
        return None
    if d == "today":
        return now.date()
    if d == "tomorrow":
        return now.date() + timedelta(days=1)
    if d in _DAY_ALIASES:
        # next occurrence of that weekday, counting today as a match
        target = _DAY_ALIASES[d]
        delta = (target - training_schedule.day_index(now.date())) % 7
        return now.date() + timedelta(days=delta)
    return "unknown"


def get_training_schedule(day: str = "") -> str:
    p = parsed()
    if p is None:
        return (
            "No training-app data yet — Alex hasn't connected the app's sync to "
            "CLARVIS. He can ask me for the sync URL (get_training_sync_url) and "
            "paste it into the app's Sync dialog."
        )
    target = _resolve_day(day)
    if target == "unknown":
        return f"Unknown day {day!r} — use a weekday name, 'today', 'tomorrow', or 'week'."
    if target is None:
        return "Alex's week (from his training app):\n" + training_schedule.schedule_summary(p) + _staleness_note()
    events = training_schedule.events_for_date(p, target)
    label = target.strftime("%A %b %-d")
    if not events:
        return f"Nothing blocked in the schedule for {label}." + _staleness_note()
    lines = [f"Schedule for {label}:"]
    for ev in events:
        lines.append("  " + training_schedule.format_block(ev, target))
    return "\n".join(lines) + _staleness_note()


def get_workout_info(day: str = "") -> str:
    p = parsed()
    if p is None:
        return (
            "No training-app data yet — Alex hasn't connected the app's sync. "
            "Use get_training_sync_url to give him the setup link."
        )
    target = _resolve_day(day or "today")
    if target in (None, "unknown"):
        target = datetime.now(LOCAL_TZ).date()
    label = target.strftime("%A")
    parts = []
    workout = training_schedule.workout_for_date(p, target)
    parts.append(f"Workout card for {label}:\n{workout}" if workout else f"No workout card for {label}.")
    if p.get("warmup"):
        parts.append("Everyday warmup:\n" + p["warmup"])
    routines = p.get("routines") or {}
    if routines.get("morning"):
        parts.append("Morning routine:\n" + routines["morning"])
    if routines.get("night"):
        parts.append("Night routine:\n" + routines["night"])
    return "\n\n".join(parts) + _staleness_note()


def get_workout_library(category: str = "", page: str = "") -> str:
    p = parsed()
    if p is None:
        return "No training-app data yet — Alex hasn't connected the app's sync."
    lib = p.get("library") or {}
    if not lib:
        return "The workout library is empty."
    if not category:
        lines = ["Workout library categories:"]
        for cat, pages in lib.items():
            lines.append(f"  {cat}: " + ", ".join(pg["title"] for pg in pages))
        return "\n".join(lines)
    match = next((c for c in lib if c.lower() == category.strip().lower()), None)
    if match is None:
        return f"No category {category!r}. Categories: " + ", ".join(lib)
    pages = lib[match]
    if page:
        pg = next((x for x in pages if x["title"].lower() == page.strip().lower()), None)
        if pg is None:
            return f"No page {page!r} in {match}. Pages: " + ", ".join(x["title"] for x in pages)
        return f"{match} — {pg['title']}:\n{pg['body']}"
    out = [f"{match} pages:"]
    for pg in pages:
        out.append(f"--- {pg['title']} ---\n{pg['body']}")
    return "\n\n".join(out)


# ---- writing back into the app ---------------------------------------------
#
# Writes are real edits to Alex's own schedule, so three rules hold them down:
#   1. Read-modify-write happens INSIDE _lock, so a device push landing mid-edit
#      can't interleave and produce a half-applied grid.
#   2. Every write stashes the previous snapshot, so "undo that" always works.
#   3. A new rev is minted on every write — that is the signal the app polls for,
#      and without it devices would never pull the change.
# The one race we cannot close from here: if Alex is typing in the app at the
# same moment, its debounced push carries a FULL pre-edit snapshot and will
# overwrite what we just wrote. It's a ~1s window, and the tools say so.

UNDO_DEPTH = 3


def _new_rev() -> str:
    return uuid.uuid4().hex[:16]


def _decode(keys: dict, storage_key: str):
    raw = keys.get(training_schedule.safe_key(storage_key))
    if raw is None:
        return {}
    try:
        val = json.loads(raw)
        return val if isinstance(val, dict) else {}
    except (ValueError, TypeError):
        return {}


def _encode(keys: dict, storage_key: str, value) -> None:
    keys[training_schedule.safe_key(storage_key)] = json.dumps(value)


def parse_clock(text: str):
    """'5', '5pm', '5:30 PM', '17:00' -> minutes from midnight, or None."""
    s = str(text or "").strip().lower().replace(".", "")
    m = re.match(r"^(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$", s)
    if not m:
        return None
    h, mins, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if mins > 59:
        return None
    if ap:
        if h < 1 or h > 12:
            return None
        h = (h % 12) + (12 if ap == "pm" else 0)
    elif h > 23:
        return None
    return h * 60 + mins


def _resolve_span(target, start: str, end: str):
    """(column index, start slot, exclusive end slot) for a day + clock range.

    The subtle part is which COLUMN a time belongs to. Columns run 3AM-3AM, so
    "Monday 2am" is the 2 AM that happens on Monday — which the app stores in
    SUNDAY's column, in its after-midnight tail. Taking the day at face value
    would silently write it to Tuesday morning instead.
    Returns (None, error) on bad input."""
    s_min, e_min = parse_clock(start), parse_clock(end)
    if s_min is None:
        return None, f"Couldn't read the start time {start!r} — try '5:30am' or '17:00'."
    if e_min is None:
        return None, f"Couldn't read the end time {end!r} — try '6pm' or '18:00'."
    if s_min == e_min:
        return None, f"Start and end are both {start} — give the range an end time."
    origin = training_schedule.DAY_START_MIN
    day = target - timedelta(days=1) if s_min < origin else target
    s_slot = (s_min - origin) % (24 * 60) // training_schedule.SLOT_MIN
    e_slot = (e_min - origin) % (24 * 60) // training_schedule.SLOT_MIN
    if e_slot <= s_slot:
        e_slot += training_schedule.N_SLOTS  # runs into the column's post-midnight tail
    if e_slot > training_schedule.N_SLOTS:
        return None, ("That range crosses 3:00am, where the app starts a new day "
                      "column, so it can't be one block — split it in two "
                      "(…until 3:00am, then 3:00am onward).")
    return (training_schedule.day_index(day), int(s_slot), int(e_slot)), ""


def _mutate(apply_fn, label: str) -> str:
    """Run apply_fn(keys) against a copy of the snapshot and store the result.
    apply_fn returns (summary, error); a non-empty error aborts with nothing written."""
    with _lock:
        _hydrate_locked()
        current = _state["snapshot"]
        if current is None:
            return ("The training app hasn't synced into CLARVIS yet, so there's "
                    "nothing to edit. Use get_training_sync_url to connect it first.")
        new_snap = copy.deepcopy(current)
        summary, err = apply_fn(new_snap.setdefault("keys", {}))
        if err:
            return err
        new_snap["rev"] = _new_rev()
        _state["undo"].append({"snapshot": current, "label": label})
        del _state["undo"][:-UNDO_DEPTH]
        _state["snapshot"] = new_snap
        _state["saved_at"] = datetime.now(LOCAL_TZ).isoformat()
        perr = _persist_locked()
    if _on_update is not None:
        try:
            _on_update()
        except Exception:
            pass
    tail = ("\n\nNote: saved here and pushed to his devices, but the durable copy "
            "failed to write (" + perr[:120] + ") — it will retry.") if perr else ""
    return (summary + "\n\nHis devices pick this up within about 8 seconds. Say "
            "'undo that' to reverse it." + tail)


def edit_schedule(day: str, start: str, end: str, text: str = "") -> str:
    """Set (or clear, with empty text) a time range on one day of the grid."""
    target = _resolve_day(day)
    if target in (None, "unknown"):
        return (f"Which day? {day!r} isn't one I can resolve — use a weekday name, "
                "'today' or 'tomorrow'.")
    span, err = _resolve_span(target, start, end)
    if err:
        return err
    col, s_slot, e_slot = span
    label = text.strip()
    # The label names the day Alex said; col may be the previous column when the
    # range starts before 3 AM (see _resolve_span).
    span_txt = f"{_resolve_day_name(target)} {start}–{end}"

    def apply(keys):
        grid = _decode(keys, "weeklySchedule.v1")
        replaced, changed = [], False
        for slot in range(s_slot, e_slot):
            k = f"{slot}|{col}"
            old = grid.get(k)
            old_txt = old.strip() if isinstance(old, str) else ""
            if old_txt and old_txt != label:
                replaced.append(old_txt)
            if label:
                if old_txt != label:
                    changed = True
                grid[k] = label
            else:
                if k in grid:
                    changed = True
                grid.pop(k, None)
        _encode(keys, "weeklySchedule.v1", grid)
        if not changed:
            return "", (f"{span_txt} already reads \"{label}\" — nothing to change."
                        if label else f"Nothing was booked {span_txt} — no change made.")
        if not label:
            return f"Cleared {span_txt} (was {', '.join(dict.fromkeys(replaced))}).", ""
        over = f" — replacing {', '.join(dict.fromkeys(replaced))}" if replaced else ""
        return f"Set {span_txt} to \"{label}\"{over}.", ""

    return _mutate(apply, f"edit {span_txt}")


def set_workout_card(day: str, text: str) -> str:
    """Replace a day's workout card (the list under the grid)."""
    target = _resolve_day(day)
    if target in (None, "unknown"):
        return f"Which day? {day!r} isn't one I can resolve."
    col = training_schedule.day_index(target)

    def apply(keys):
        cards = _decode(keys, "weeklyWorkouts.v1")
        body = text.strip()
        if body:
            cards[str(col)] = body
        else:
            cards.pop(str(col), None)
        _encode(keys, "weeklyWorkouts.v1", cards)
        name = training_schedule.DAYS[col]
        return (f"{name}'s workout card is now:\n{body}" if body
                else f"Cleared {name}'s workout card."), ""

    return _mutate(apply, f"workout card {training_schedule.DAYS[col]}")


def undo_training_edit() -> str:
    """Roll back the most recent change CLARVIS made."""
    with _lock:
        _hydrate_locked()
        if not _state["undo"]:
            return "Nothing to undo — I haven't changed his schedule this session."
        entry = _state["undo"].pop()
        restored = copy.deepcopy(entry["snapshot"])
        restored["rev"] = _new_rev()  # devices only pull on a rev they haven't seen
        _state["snapshot"] = restored
        _state["saved_at"] = datetime.now(LOCAL_TZ).isoformat()
        _persist_locked()
        label = entry.get("label") or "the last change"
    if _on_update is not None:
        try:
            _on_update()
        except Exception:
            pass
    return f"Reverted {label}. His devices will show the old version within ~8 seconds."


def week_payload(now: datetime) -> dict:
    """The /schedule page's feed: the week as columns of slot-positioned blocks.

    Slot indices (0-47, 30 min each from 3 AM) go to the client rather than clock
    strings, so the page lays the grid out by row span instead of re-deriving the
    same arithmetic in JS and drifting from it."""
    p = parsed()
    if p is None:
        return {"connected": False, "now": now.strftime("%-I:%M %p")}

    col_date, slot_now = training_schedule.current_position(now)
    today = now.date()
    # A weekly template, not a rolling 7 days: Monday's column is Monday's
    # blocks whatever today is, which is how the app itself reads.
    week_start = today - timedelta(days=training_schedule.day_index(today))
    days = []
    for i in range(7):
        d = week_start + timedelta(days=i)
        anchor = datetime(d.year, d.month, d.day) + timedelta(
            minutes=training_schedule.DAY_START_MIN)
        blocks = []
        for ev in training_schedule.column_events(p.get("grid") or {}, d):
            per = training_schedule.SLOT_MIN
            blocks.append({
                "label": ev["title"],
                "slot_start": int((ev["start"] - anchor).total_seconds() // 60 // per),
                "slot_end": int((ev["end"] - anchor).total_seconds() // 60 // per),
                "start": training_schedule.clock(ev["start"]),
                "end": training_schedule.clock(ev["end"]),
            })
        days.append({
            "index": i,
            "name": training_schedule.DAYS[i],
            "short": training_schedule.DAYS[i][:3].upper(),
            "is_today": d == today,
            "is_current_column": d == col_date,
            "blocks": blocks,
            "workout": training_schedule.workout_for_date(p, d),
        })
    return {
        "connected": True,
        "saved_at": last_synced_at(),
        "now": now.strftime("%-I:%M %p"),
        "now_slot": round(slot_now, 3),
        "today_name": training_schedule.DAYS[training_schedule.day_index(today)],
        "days": days,
        "warmup": p.get("warmup") or "",
        "routines": p.get("routines") or {},
        "library": p.get("library") or {},
        "app_url": APP_ORIGIN,
    }


def get_training_sync_url() -> str:
    out = (
        "Training-app sync URL (paste into the app's Sync dialog on each device):\n"
        f"{sync_url()}\n\n"
        "Steps: open the training app → tap Sync → paste that URL into the "
        "Database URL field → Connect. The device's data pushes up on first "
        "connect, and every later edit syncs automatically. (If the CLARVIS "
        "access code is ever rotated, this URL changes — just ask me for it "
        "again and re-paste it.)"
    )
    is_server = (os.environ.get("JARVIS_RUNTIME") or "local").strip().lower() == "server"
    if not is_server and not os.environ.get("TRAINING_SYNC_TOKEN", "").strip():
        # The URL always points at the public server, but with no pinned token
        # each node derives its own from its own ACCESS_CODE — so a URL built
        # here on the Mac carries a token the server would reject. Say so rather
        # than handing over a link that 404s.
        out += (
            "\n\nHeads up: I'm answering from the Mac node, and no "
            "TRAINING_SYNC_TOKEN is pinned, so this URL's token was derived "
            "from THIS machine's access code — the server may reject it. Ask "
            "me again in the web chat (the server) for the authoritative link, "
            "or set the same TRAINING_SYNC_TOKEN on both to pin one URL."
        )
    return out


TOOL_SCHEMAS = [
    {
        "name": "get_training_schedule",
        "description": (
            "Alex's real schedule, from his training app (his week blocked out in "
            "30-minute slots, edited on his phone). Use for any question about his "
            "schedule beyond today — today's blocks are already in the RIGHT NOW "
            "context. day: a weekday name, 'today', 'tomorrow', or 'week' (default) "
            "for the whole week."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "day": {
                    "type": "string",
                    "description": "Weekday name, 'today', 'tomorrow', or 'week'.",
                }
            },
            "required": [],
        },
    },
    {
        "name": "get_workout_info",
        "description": (
            "Alex's workout card for a day plus his everyday warmup and "
            "morning/night routines, from his training app. day defaults to today."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "day": {
                    "type": "string",
                    "description": "Weekday name, 'today', or 'tomorrow'.",
                }
            },
            "required": [],
        },
    },
    {
        "name": "get_workout_library",
        "description": (
            "Alex's workout library from his training app (Lifts, Good Drills, Bag "
            "shooting, 50/50, Conditioning, Group workouts, Court movement). No "
            "args lists categories; category lists/returns its pages; add page for "
            "one specific page."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "category": {"type": "string", "description": "Library category name."},
                "page": {"type": "string", "description": "Exact page title within the category."},
            },
            "required": [],
        },
    },
    {
        "name": "edit_schedule",
        "description": (
            "Change a time range on Alex's training-app schedule — the real grid he "
            "sees on his phone. Use for 'move my lift to 5', 'block 7-9 for film', "
            "'clear Thursday afternoon'. Leave text empty to CLEAR the range. Times "
            "are wall-clock ('5:30am', '6pm', '17:00'); end is exclusive. Confirm "
            "with him first when the day or time is ambiguous, and tell him what it "
            "replaced. He can say 'undo that'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "day": {"type": "string", "description": "Weekday name, 'today', or 'tomorrow'."},
                "start": {"type": "string", "description": "Start time, e.g. '3pm' or '15:00'."},
                "end": {"type": "string", "description": "End time (exclusive), e.g. '4:30pm'."},
                "text": {"type": "string", "description": "What goes in the block. Empty clears it."},
            },
            "required": ["day", "start", "end"],
        },
    },
    {
        "name": "set_workout_card",
        "description": (
            "Replace the workout card for one day in Alex's training app (the bullet "
            "list under the grid — e.g. '• Warmup\\n• 50/50\\n• Lower heavy'). Pass "
            "the full new card; empty text clears it. Not for schedule blocks — use "
            "edit_schedule for those."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "day": {"type": "string", "description": "Weekday name, 'today', or 'tomorrow'."},
                "text": {"type": "string", "description": "The full card text, newline-separated."},
            },
            "required": ["day", "text"],
        },
    },
    {
        "name": "undo_training_edit",
        "description": (
            "Reverse the last change made to Alex's training app (schedule block or "
            "workout card). Use when he says 'undo that' or 'put it back'."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_training_sync_url",
        "description": (
            "The URL Alex pastes into his training app's Sync dialog to connect it "
            "to CLARVIS. Use when he asks how to connect/reconnect the training "
            "app, or when training tools report no data has ever synced."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
]

TOOL_STATUS_LABELS = {
    "get_training_schedule": "Checking your schedule…",
    "get_workout_info": "Pulling up your workout…",
    "get_workout_library": "Opening the workout library…",
    "get_training_sync_url": "Getting your sync link…",
    "edit_schedule": "Updating your schedule…",
    "set_workout_card": "Rewriting that workout card…",
    "undo_training_edit": "Putting that back…",
}

TOOL_NAMES = tuple(t["name"] for t in TOOL_SCHEMAS)


def handle_tool_call(tool_name: str, tool_input: dict) -> str:
    if tool_name == "get_training_schedule":
        return get_training_schedule(tool_input.get("day", ""))
    if tool_name == "get_workout_info":
        return get_workout_info(tool_input.get("day", ""))
    if tool_name == "get_workout_library":
        return get_workout_library(
            tool_input.get("category", ""), tool_input.get("page", "")
        )
    if tool_name == "get_training_sync_url":
        return get_training_sync_url()
    if tool_name == "edit_schedule":
        return edit_schedule(tool_input["day"], tool_input["start"],
                             tool_input["end"], tool_input.get("text", ""))
    if tool_name == "set_workout_card":
        return set_workout_card(tool_input["day"], tool_input.get("text", ""))
    if tool_name == "undo_training_edit":
        return undo_training_edit()
    return f"Unknown tool: {tool_name}"
