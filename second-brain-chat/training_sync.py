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
from datetime import date, datetime, timedelta
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


# Scriptable widget sources. %s slots: feed URL / schedule page URL (schedule
# widget), talk URL (talk widget). Plain %-substitution, not f-strings — these
# are JavaScript, and escaping every brace would make them unreadable.
_SCHEDULE_WIDGET_JS = """\
// CLARVIS — Schedule widget (add as a LARGE Scriptable widget)
const FEED = "%s";
const OPEN = "%s";
const CYAN = new Color("#4fd4e8"), ACCENT = new Color("#9bf2fa");
const DIM = new Color("#96e1f5", 0.72), WHITE = new Color("#e8fdff");
let d = null;
try { d = await new Request(FEED).loadJSON(); } catch (e) {}
const w = new ListWidget();
w.backgroundColor = new Color("#010a20");
w.url = OPEN;
w.setPadding(16, 16, 14, 16);
const head = w.addText("CLARVIS · " + (d && d.day ? d.day.toUpperCase() : "SCHEDULE"));
head.font = Font.boldSystemFont(11); head.textColor = CYAN;
w.addSpacer(10);
if (!d) {
  const t = w.addText("Couldn't reach CLARVIS"); t.font = Font.systemFont(14); t.textColor = DIM;
} else if (!d.connected) {
  const t = w.addText("No schedule synced yet"); t.font = Font.systemFont(14); t.textColor = DIM;
} else {
  const capNow = w.addText("RIGHT NOW"); capNow.font = Font.mediumSystemFont(9); capNow.textColor = DIM;
  const nowT = w.addText(d.now ? d.now.label : "Nothing blocked");
  nowT.font = Font.boldSystemFont(20); nowT.textColor = d.now ? WHITE : CYAN; nowT.lineLimit = 2;
  if (d.now) { const u = w.addText("until " + d.now.until); u.font = Font.systemFont(11); u.textColor = DIM; }
  w.addSpacer(10);
  const capNext = w.addText("NEXT"); capNext.font = Font.mediumSystemFont(9); capNext.textColor = DIM;
  if (d.next) {
    const n = w.addText(d.next.label); n.font = Font.boldSystemFont(15); n.textColor = ACCENT; n.lineLimit = 1;
    const nt = w.addText(d.next.start + (d.next.day ? " " + d.next.day : ""));
    nt.font = Font.systemFont(11); nt.textColor = DIM;
  } else {
    const n = w.addText("Clear"); n.font = Font.systemFont(14); n.textColor = CYAN;
  }
  for (const item of (d.later || [])) {
    w.addSpacer(4);
    const row = w.addText(item.start + "  " + item.label + (item.day ? " (" + item.day + ")" : ""));
    row.font = Font.systemFont(11); row.textColor = CYAN; row.lineLimit = 1;
  }
  w.addSpacer();
  const foot = w.addText("updated " + d.updated);
  foot.font = Font.systemFont(8); foot.textColor = DIM;
}
w.refreshAfterDate = new Date(Date.now() + 5 * 60 * 1000);
Script.setWidget(w);
Script.complete();
"""

_TALK_WIDGET_JS = """\
// CLARVIS — Talk button (add as a MEDIUM or LARGE Scriptable widget)
const TALK = "%s";
const w = new ListWidget();
const g = new LinearGradient();
g.colors = [new Color("#123a6e"), new Color("#010a20")];
g.locations = [0, 1];
w.backgroundGradient = g;
w.url = TALK;
const stack = w.addStack();
stack.layoutVertically();
stack.centerAlignContent();
w.addSpacer();
const mic = w.addText("🎙");
mic.font = Font.systemFont(44);
mic.centerAlignText();
w.addSpacer(8);
const label = w.addText("TALK TO CLARVIS");
label.font = Font.boldSystemFont(16);
label.textColor = new Color("#9bf2fa");
label.centerAlignText();
w.addSpacer();
Script.setWidget(w);
Script.complete();
"""


def get_widget_setup() -> str:
    is_server = (os.environ.get("JARVIS_RUNTIME") or "local").strip().lower() == "server"
    warn = ""
    if not is_server and not os.environ.get("WIDGET_FEED_TOKEN", "").strip():
        warn = ("\n\n⚠️ I'm answering from the Mac node with no WIDGET_FEED_TOKEN "
                "pinned, so this feed URL's token comes from THIS machine's access "
                "code and the server may reject it. Ask again in the web chat for "
                "the authoritative version.")
    feed = f"{PUBLIC_BASE_URL}/api/widget/{widget_token()}"
    schedule_js = _SCHEDULE_WIDGET_JS % (feed, f"{PUBLIC_BASE_URL}/schedule")
    talk_js = _TALK_WIDGET_JS % (f"{PUBLIC_BASE_URL}/chat-classic?talk=1",)
    return (
        "Home-screen widgets, one-time setup (~5 min):\n\n"
        "1. Install the free **Scriptable** app from the App Store.\n"
        "2. In Scriptable, tap + and paste SCRIPT 1 below; name it `CLARVIS Schedule`.\n"
        "3. Tap + again and paste SCRIPT 2; name it `CLARVIS Talk`.\n"
        "4. Long-press the home screen → + → search Scriptable → add a LARGE "
        "widget → long-press it → Edit Widget → Script: CLARVIS Schedule.\n"
        "5. Repeat with a MEDIUM widget → Script: CLARVIS Talk. Set 'When "
        "Interacting' to 'Open URL' if shown.\n"
        "6. Optional 'Hey Siri': in the Shortcuts app, create a shortcut named "
        f"'Talk to CLARVIS' with one action — Open URL → {PUBLIC_BASE_URL}/chat-classic?talk=1 "
        "— then saying 'Hey Siri, talk to CLARVIS' opens the mic.\n\n"
        "Notes: iOS refreshes widgets on its own clock (typically every 5–15 "
        "min), so RIGHT NOW can lag a few minutes at block boundaries. Tapping "
        "the schedule widget opens /schedule; tapping the talk widget opens the "
        "chat listening. Safari must be logged in to CLARVIS once for the talk "
        "button to land in chat directly."
        f"{warn}\n\n"
        "----- SCRIPT 1: CLARVIS Schedule -----\n"
        f"{schedule_js}\n"
        "----- SCRIPT 2: CLARVIS Talk -----\n"
        f"{talk_js}"
    )


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
        out = "Alex's week (from his training app):\n" + training_schedule.schedule_summary(p)
        today = datetime.now(LOCAL_TZ).date()
        ws = training_schedule.week_start(today)
        cells = p.get("once_cells") or {}
        if cells and p.get("once_week_start") == ws:
            out += "\n\nThis week only (clears when the week rolls over):"
            for i in range(7):
                d = ws + timedelta(days=i)
                for ev in training_schedule.column_events(cells, d):
                    out += "\n  %s  %s–%s  %s" % (
                        d.strftime("%a %b %-d"),
                        training_schedule.clock(ev["start"]),
                        training_schedule.clock(ev["end"]), ev["title"])
        obls = training_schedule.upcoming_obligations(p, today)
        if obls:
            out += "\n\nBig stuff coming up:"
            for o in obls[:12]:
                out += "\n  %s — %s" % (o["date"].strftime("%a %b %-d"),
                                        o["text"].replace("\n", " / "))
            if len(obls) > 12:
                out += f"\n  (+{len(obls) - 12} more further out)"
        return out + _staleness_note()
    events = training_schedule.events_for_date(p, target)
    label = target.strftime("%A %b %-d")
    obls = training_schedule.obligations_for_date(p, target)
    if not events and not obls:
        return f"Nothing blocked in the schedule for {label}." + _staleness_note()
    lines = []
    if events:
        lines.append(f"Schedule for {label}:")
        for ev in events:
            lines.append("  " + training_schedule.format_block(ev, target))
    else:
        lines.append(f"Nothing blocked in the schedule for {label}.")
    if obls:
        lines.append(f"Big stuff on {label}:")
        for t in obls:
            lines.append("  " + t.replace("\n", " / "))
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
        if len(json.dumps(new_snap)) > MAX_SNAPSHOT_BYTES:
            # The /training-sync route rejects app pushes over this cap, so a
            # server-side write that grows past it would leave devices unable to
            # push their own edits back — sync would break, not just this write.
            return (f"That change would push the app's data over its "
                    f"{MAX_SNAPSHOT_BYTES // 1000}KB sync limit, and the app "
                    "could no longer push edits back. Nothing was written — "
                    "trim what's being added.")
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


def _apply_span_to(store: dict, col: int, s_slot: int, e_slot: int, label: str):
    """Write (or clear) one resolved span into a grid-shaped dict in place.
    Returns (replaced_texts, changed)."""
    replaced, changed = [], False
    for slot in range(s_slot, e_slot):
        k = f"{slot}|{col}"
        old = store.get(k)
        old_txt = old.strip() if isinstance(old, str) else ""
        if old_txt and old_txt != label:
            replaced.append(old_txt)
        if label:
            if old_txt != label:
                changed = True
            store[k] = label
        else:
            if k in store:
                changed = True
            store.pop(k, None)
    return replaced, changed


def edit_schedule(day: str, start: str, end: str, text: str = "",
                  this_week_only: bool = False) -> str:
    """Set (or clear, with empty text) a time range on one day of the grid.
    this_week_only writes the one-week layer instead: it shows on top of the
    repeating grid and clears when the week rolls over (Sunday 3 AM)."""
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

    if this_week_only:
        now = datetime.now(LOCAL_TZ)
        col_now, _slot = training_schedule.current_position(now.replace(tzinfo=None))
        ws = training_schedule.week_start(col_now)
        # The date the resolved COLUMN represents (target-1 for before-3AM
        # ranges) must sit inside the current week, or the entry would land on
        # a day that already passed / be wiped before its week arrives.
        col_date = target if training_schedule.day_index(target) == col else target - timedelta(days=1)
        if not (ws <= col_date < ws + timedelta(days=7)):
            week_end = (ws + timedelta(days=6)).strftime("%a %b %-d")
            return (f"{_resolve_day_name(target)} {target.strftime('%b %-d')} is outside "
                    f"the current week (through {week_end}) — one-week entries only "
                    "cover this week. Use set_big_obligation for it, or make it a "
                    "repeating block.")
        span_txt += " (this week only)"

        def apply_once(keys):
            oc = _decode(keys, "weeklyOnce.v1")
            cells = oc.get("cells") if isinstance(oc.get("cells"), dict) else {}
            if oc.get("weekStart") != ws.isoformat():
                cells = {}  # stale layer from a previous week — roll it over here
            replaced, changed = _apply_span_to(cells, col, s_slot, e_slot, label)
            _encode(keys, "weeklyOnce.v1", {"weekStart": ws.isoformat(), "cells": cells})
            if not changed:
                return "", (f"{span_txt} already reads \"{label}\" — nothing to change."
                            if label else
                            f"No one-week entry at {span_txt} — no change made. (Clearing "
                            "here only removes one-week entries; the repeating grid shows "
                            "through. To blank a repeating block for just this week, set a "
                            "this-week text like \"CANCELED\" instead.)")
            if not label:
                return f"Cleared {span_txt} (was {', '.join(dict.fromkeys(replaced))}).", ""
            over = f" — covering {', '.join(dict.fromkeys(replaced))} for this week" if replaced else ""
            return f"Set {span_txt} to \"{label}\"{over}. It clears when the week rolls over.", ""

        return _mutate(apply_once, f"edit {span_txt}")

    def apply(keys):
        grid = _decode(keys, "weeklySchedule.v1")
        replaced, changed = _apply_span_to(grid, col, s_slot, e_slot, label)
        _encode(keys, "weeklySchedule.v1", grid)
        if not changed:
            return "", (f"{span_txt} already reads \"{label}\" — nothing to change."
                        if label else f"Nothing was booked {span_txt} — no change made.")
        if not label:
            return f"Cleared {span_txt} (was {', '.join(dict.fromkeys(replaced))}).", ""
        over = f" — replacing {', '.join(dict.fromkeys(replaced))}" if replaced else ""
        return f"Set {span_txt} to \"{label}\"{over}.", ""

    return _mutate(apply, f"edit {span_txt}")


MAX_BATCH_EDITS = 60  # a full week at 30-min granularity is nowhere near this


def batch_edit_schedule(edits: list) -> str:
    """Apply many edit_schedule-shaped edits as ONE mutation / ONE undo entry —
    for bulk-populating a week from a pasted syllabus rather than one
    confirmation per block. Best-effort: edits that fail to resolve (bad day
    or time) are reported and skipped; the rest still apply. Nothing writes
    at all if every edit fails to resolve, or if none of them change anything."""
    if not edits:
        return "No edits given."
    if len(edits) > MAX_BATCH_EDITS:
        return f"That's {len(edits)} edits at once — split into batches of {MAX_BATCH_EDITS} or fewer."

    resolved, errors = [], []
    for i, e in enumerate(edits):
        if not isinstance(e, dict):
            errors.append(f"#{i + 1}: not a valid edit.")
            continue
        day, start, end = e.get("day", ""), e.get("start", ""), e.get("end", "")
        target = _resolve_day(day)
        if target in (None, "unknown"):
            errors.append(f"#{i + 1} ({day!r}): couldn't resolve the day.")
            continue
        span, err = _resolve_span(target, start, end)
        if err:
            errors.append(f"#{i + 1} {_resolve_day_name(target)}: {err}")
            continue
        col, s_slot, e_slot = span
        span_txt = f"{_resolve_day_name(target)} {start}–{end}"
        resolved.append((span_txt, col, s_slot, e_slot, str(e.get("text", "")).strip()))

    if not resolved:
        return "None of those edits could be resolved:\n" + "\n".join(errors)

    def apply(keys):
        grid = _decode(keys, "weeklySchedule.v1")
        applied_lines = []
        for span_txt, col, s_slot, e_slot, label in resolved:
            replaced, changed = _apply_span_to(grid, col, s_slot, e_slot, label)
            if changed:
                verb = f'set to "{label}"' if label else "cleared"
                over = f" (replacing {', '.join(dict.fromkeys(replaced))})" if replaced else ""
                applied_lines.append(f"{span_txt}: {verb}{over}")
        if not applied_lines:
            return "", "None of those edits changed anything — every block already matched."
        _encode(keys, "weeklySchedule.v1", grid)
        lines = [f"Applied {len(applied_lines)} of {len(edits)} edits:"]
        lines += ["  " + l for l in applied_lines]
        if errors:
            lines.append(f"Skipped {len(errors)}:")
            lines += ["  " + e for e in errors]
        return "\n".join(lines), ""

    return _mutate(apply, f"batch edit ({len(resolved)} blocks)")


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


def set_big_obligation(when: str, text: str = "") -> str:
    """Set, replace, or (with empty text) remove the Big Stuff entry for one
    date in the training app. text is the FULL list for that day (1-3 lines)."""
    w = (when or "").strip()
    target = None
    try:
        target = date.fromisoformat(w)
    except ValueError:
        r = _resolve_day(w)
        if isinstance(r, date):
            target = r
    if target is None:
        return (f"Couldn't read {when!r} as a date — use YYYY-MM-DD, a weekday "
                "name, 'today', or 'tomorrow'.")
    if target < datetime.now(LOCAL_TZ).date():
        return (f"{target.strftime('%a %b %-d')} already passed — the app drops "
                "past dates on its own, so there's nothing to put there.")
    label = text.strip()
    day_txt = target.strftime("%a %b %-d")

    def apply(keys):
        raw = keys.get(training_schedule.safe_key("bigObligations.v1"))
        try:
            arr = json.loads(raw) if raw else []
        except (ValueError, TypeError):
            arr = []
        if not isinstance(arr, list):
            arr = []
        iso = target.isoformat()
        existing = [o for o in arr if isinstance(o, dict) and o.get("date") == iso]
        rest = [o for o in arr if not (isinstance(o, dict) and o.get("date") == iso)]
        if not label and not existing:
            return "", f"No big stuff listed for {day_txt} — nothing to remove."
        if label:
            rest.append({"date": iso, "text": label})
            rest.sort(key=lambda o: str(o.get("date") or "9999")
                      if isinstance(o, dict) else "9999")
        keys[training_schedule.safe_key("bigObligations.v1")] = json.dumps(rest)
        if not label:
            old = " / ".join(str(o.get("text") or "").replace("\n", " / ") for o in existing)
            return f"Removed the big stuff for {day_txt} (was: {old}).", ""
        if existing:
            return f"Replaced the big stuff for {day_txt} with: {label}", ""
        return f"Added big stuff for {day_txt}: {label}", ""

    return _mutate(apply, f"big stuff {day_txt}")


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
        for ev in training_schedule.column_events(training_schedule.grid_for_week(p, d), d):
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
        "obligations": [
            {"date": o["date"].isoformat(),
             "label": o["date"].strftime("%a %b %-d"),
             "text": o["text"]}
            for o in training_schedule.upcoming_obligations(p, today)
        ],
        "app_url": APP_ORIGIN,
    }


def widget_token() -> str:
    """Read-only token for the phone-widget feed. Separate from sync_token on
    purpose: the sync token can WRITE the whole dataset, and it shouldn't have
    to live inside a Scriptable script just so a widget can read now/next."""
    explicit = os.environ.get("WIDGET_FEED_TOKEN", "").strip()
    if explicit:
        return explicit
    access_code = os.environ.get("ACCESS_CODE") or os.environ.get("JARVIS_PASSWORD") or ""
    if not access_code:
        return "local-dev"
    return hmac.new(
        access_code.encode("utf-8"), b"widget-feed-v1", hashlib.sha256
    ).hexdigest()[:24]


def widget_token_matches(candidate: str) -> bool:
    return hmac.compare_digest(
        str(candidate or "").encode("utf-8", "replace"),
        widget_token().encode("utf-8", "replace"),
    )


# Grid blocks that structure the day but never need anticipating. Since the
# v7 full-week rebuild the grid carries sleep/routines/meals around the clock,
# and without this filter the widget's NEXT/LATER slots were all "Sleep" —
# accurate, useless. "now" stays unfiltered: knowing you're inside the night
# routine is real information; being warned that sleep is coming is not.
_STRUCTURAL_BLOCKS = {"sleep", "morning routine", "night routine",
                      "lunch", "dinner"}


def _is_structural(title: str) -> bool:
    return (title or "").strip().lower() in _STRUCTURAL_BLOCKS


def widget_payload(now: datetime) -> dict:
    """What the home-screen widget shows: the block he's in, what's next, and a
    couple after that. Clock strings are formatted HERE so the Scriptable side
    stays a dumb renderer — one place owns the grid's time semantics."""
    p = parsed()
    updated = now.strftime("%-I:%M %p")
    if p is None:
        return {"connected": False, "updated": updated}
    naive = now.replace(tzinfo=None)
    events_today = training_schedule.events_for_date(p, now.date())
    current = next((e for e in events_today if e["start"] <= naive < e["end"]), None)
    upcoming = [e for e in events_today
                if e["start"] > naive and not _is_structural(e["title"])]
    # If today is running dry, keep the widget useful into the evening by
    # showing tomorrow's opening blocks (labeled, so 5:30am isn't read as PM).
    tom = now.date() + timedelta(days=1)
    tomorrow = [e for e in training_schedule.events_for_date(p, tom)
                if e["start"].date() == tom and not _is_structural(e["title"])]
    queue = ([{"e": e, "day": ""} for e in upcoming]
             + [{"e": e, "day": "tomorrow"} for e in tomorrow])

    def enc(item):
        e, day = item["e"], item["day"]
        return {"label": e["title"],
                "start": training_schedule.clock(e["start"]),
                "end": training_schedule.clock(e["end"]),
                "day": day}

    workout = training_schedule.workout_for_date(p, now.date())
    return {
        "connected": True,
        "updated": updated,
        "day": now.strftime("%A"),
        "now": ({"label": current["title"],
                 "until": training_schedule.clock(current["end"])} if current else None),
        "next": enc(queue[0]) if queue else None,
        "later": [enc(i) for i in queue[1:4]],
        "workout_line": (workout.splitlines()[-1].lstrip("• ").strip()
                         if workout else ""),
    }



def _5050_table(keys):
    """(library_dict, page) for the 50/50 Log table, or (lib, None). Category 3
    is "50/50" in the app's fixed CATS list; the page is the seeded type=table
    "Log". Matched by shape (a table whose first column is Date) so a renamed
    title survives."""
    lib = _decode(keys, "workoutLibrary.v2")
    cat = lib.get("3") or {}
    for page in cat.get("pages") or []:
        if (isinstance(page, dict) and page.get("type") == "table"
                and (page.get("columns") or [""])[0].strip().lower() == "date"):
            return lib, page
    return lib, None


def log_5050(threes, free_throws, when: str = "") -> str:
    """Log a 50/50 session (threes made /50, free throws made /50) into the
    app's Log table. Fills the first empty row (that's where he'd type) so the
    entry lands at the top of the table, not under 78 blank rows."""
    try:
        t, f = int(threes), int(free_throws)
    except (TypeError, ValueError):
        return "Numbers didn't parse — give threes and free throws as counts out of 50."
    if not (0 <= t <= 50 and 0 <= f <= 50):
        return f"Out of range: {t}/50 threes, {f}/50 free throws — the drill is out of 50."
    if when.strip():
        d = None
        for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d"):
            try:
                d = datetime.strptime(when.strip(), fmt)
                break
            except ValueError:
                continue
        if d is None:
            return f"Couldn't read the date {when!r} — use M/D or YYYY-MM-DD, or omit for today."
        if fmt == "%m/%d":
            d = d.replace(year=datetime.now(LOCAL_TZ).year)
    else:
        d = datetime.now(LOCAL_TZ)
    stamp = f"{d.month}/{d.day}"

    def apply(keys):
        lib, page = _5050_table(keys)
        if page is None:
            return "", ("No 50/50 Log table found in the workout library — has the "
                        "app's library been cleared?")
        rows = page.get("rows") or []
        entry = [stamp, str(t), str(f)]
        for row in rows:
            if not any((c or "").strip() for c in row):
                row[:] = entry
                break
        else:
            rows.append(entry)
            page["rows"] = rows
        _encode(keys, "workoutLibrary.v2", lib)
        return f"Logged 50/50 for {stamp}: {t}/50 threes, {f}/50 free throws.", ""

    out = _mutate(apply, f"50/50 log {stamp}")
    if out.startswith("Logged"):
        out += "\n" + fifty_fifty_trend(limit=5)
    return out


def logged_5050_on(d=None) -> bool:
    """Has a 50/50 row been stamped for date d (default today)?

    The ask-for-numbers prompts read this so they go quiet the moment Alex
    logs — a prompt that keeps asking after you answered is how a nudge
    channel gets muted."""
    d = d or datetime.now(LOCAL_TZ)
    stamp = f"{d.month}/{d.day}"
    snap = get_snapshot()
    if snap is None:
        return False
    _, page = _5050_table(snap.get("keys") or {})
    if page is None:
        return False
    return any((r or [""])[0].strip() == stamp for r in page.get("rows") or [])


def fifty_fifty_trend(limit: int = 10) -> str:
    """Recent 50/50 entries plus bests — the measurable behind 'am I improving'."""
    snap = get_snapshot()
    if snap is None:
        return "No training-app data synced yet."
    _, page = _5050_table(snap.get("keys") or {})
    if page is None:
        return "No 50/50 Log table found in the workout library."
    filled = [r for r in page.get("rows") or []
              if any((c or "").strip() for c in r)]
    if not filled:
        return ("No 50/50 sessions logged yet. Say 'log 50/50: <threes> and "
                "<free throws>' after a session to start the record.")

    def num(v):
        try:
            return int(str(v).strip().split("/")[0])
        except (TypeError, ValueError):
            return None

    lines = [f"50/50 log — last {min(limit, len(filled))} of {len(filled)} sessions:"]
    for r in filled[-limit:]:
        lines.append(f"  {(r[0] or '?').strip():>6}  threes {(r[1] or '?').strip():>2}/50   FTs {(r[2] or '?').strip():>2}/50")
    threes = [n for r in filled if (n := num(r[1] if len(r) > 1 else None)) is not None]
    fts = [n for r in filled if (n := num(r[2] if len(r) > 2 else None)) is not None]
    if threes and fts:
        lines.append(f"  best: {max(threes)}/50 threes · {max(fts)}/50 FTs"
                     f"   avg last 5: {sum(threes[-5:]) / len(threes[-5:]):.0f} / {sum(fts[-5:]) / len(fts[-5:]):.0f}")
    return "\n".join(lines)


# ---- library page write-back ------------------------------------------------
#
# The scoring layer (miss lines, moves on the NOW: L__ level ladders, appended
# log rows) needs CLARVIS to edit library PAGES, not just the grid and day
# cards. Two deliberately narrow tools rather than a page CRUD surface: an
# exact-substring body edit that must match uniquely (so a loose old_text can't
# silently clobber prose), and a table-row append. No page create, rename, or
# delete — the library is Alex's content and those stay his by-hand decisions.

def _find_library_page(keys, category: str, page: str):
    """(lib, page_dict, err) — exact case-insensitive category + title match
    against the RAW workoutLibrary.v2 dict (numeric category keys)."""
    lib = _decode(keys, "workoutLibrary.v2")
    cats = training_schedule.LIBRARY_CATS
    want_cat = (category or "").strip().lower()
    idx = next((i for i, c in enumerate(cats) if c.lower() == want_cat), None)
    if idx is None:
        return lib, None, f"No library category {category!r}. Categories: " + ", ".join(cats)
    pages = (lib.get(str(idx)) or {}).get("pages") or []
    want = (page or "").strip().lower()
    pg = next((p for p in pages if isinstance(p, dict)
               and str(p.get("title", "")).strip().lower() == want), None)
    if pg is None:
        titles = ", ".join(str(p.get("title", "?")) for p in pages if isinstance(p, dict))
        return lib, None, f"No page {page!r} in {cats[idx]}. Pages: {titles or '(none)'}"
    return lib, pg, ""


def _clip(s: str, n: int = 90) -> str:
    s = str(s)
    return s if len(s) <= n else s[: n - 1] + "…"


def edit_library_page(category: str, page: str, old_text: str, new_text: str) -> str:
    """Replace one exact, unique occurrence of old_text in a library page body."""
    old, new = str(old_text or ""), str(new_text or "")
    if not old.strip():
        return "old_text is empty — give the exact text to replace."
    if old == new:
        return "old_text and new_text are identical — nothing would change."

    def apply(keys):
        lib, pg, err = _find_library_page(keys, category, page)
        if err:
            return "", err
        title = str(pg.get("title", page))
        if pg.get("type") == "table":
            return "", (f"{title!r} is a table page — use append_library_log_row "
                        "for tables. This tool edits text pages.")
        body = pg.get("body") or ""
        n = body.count(old)
        if n == 0:
            return "", (f"That exact text isn't on {title!r}. Read the page with "
                        "get_workout_library and copy it verbatim — whitespace "
                        "and line breaks count.")
        if n > 1:
            return "", (f"That text appears {n} times on {title!r} — include more "
                        "surrounding text so the match is unique.")
        pg["body"] = body.replace(old, new, 1)
        _encode(keys, "workoutLibrary.v2", lib)
        verb = "Removed from" if not new.strip() else "Edited"
        return f"{verb} {title!r}: {_clip(old)!r} → {_clip(new)!r}", ""

    return _mutate(apply, f"library edit: {page}")


def append_library_log_row(category: str, page: str, values: list) -> str:
    """Append one row to a type=table library page (fills the first blank
    pre-seeded row, like the app itself — else appends at the end)."""
    vals = [str(v) for v in (values or [])]
    if not any(v.strip() for v in vals):
        return "values is empty — give the row's cells in column order."

    def apply(keys):
        lib, pg, err = _find_library_page(keys, category, page)
        if err:
            return "", err
        title = str(pg.get("title", page))
        if pg.get("type") != "table":
            return "", (f"{title!r} is a text page, not a table — use "
                        "edit_library_page for text pages.")
        cols = [str(c) for c in (pg.get("columns") or [])]
        if cols and len(vals) > len(cols):
            return "", (f"{len(vals)} values for {len(cols)} columns "
                        f"({', '.join(cols)}) — give at most one value per "
                        "column, in order.")
        entry = vals + [""] * (len(cols) - len(vals)) if cols else vals
        rows = pg.get("rows") or []
        for row in rows:
            if isinstance(row, list) and not any((c or "").strip() for c in row):
                row[:] = entry
                break
        else:
            rows.append(entry)
            pg["rows"] = rows
        _encode(keys, "workoutLibrary.v2", lib)
        head = f" ({', '.join(cols)})" if cols else ""
        return f"Logged into {title!r}{head}: " + " | ".join(entry), ""

    return _mutate(apply, f"log row: {page}")


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
            "are wall-clock ('5:30am', '6pm', '17:00'); end is exclusive. Set "
            "this_week_only=true for one-off things in the CURRENT week (an "
            "appointment, a canceled class) — those show in amber on top of the "
            "repeating grid and clear when the week rolls over; default false "
            "changes the repeating week. Confirm with him first when the day, time, "
            "or one-off-vs-repeating is ambiguous, and tell him what it replaced. "
            "He can say 'undo that'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "day": {"type": "string", "description": "Weekday name, 'today', or 'tomorrow'."},
                "start": {"type": "string", "description": "Start time, e.g. '3pm' or '15:00'."},
                "end": {"type": "string", "description": "End time (exclusive), e.g. '4:30pm'."},
                "text": {"type": "string", "description": "What goes in the block. Empty clears it."},
                "this_week_only": {"type": "boolean", "description": "True = one-off entry for the current week only (clears at week rollover). False/omitted = repeating grid."},
            },
            "required": ["day", "start", "end"],
        },
    },
    {
        "name": "set_big_obligation",
        "description": (
            "Set, replace, or remove one date's entry in the training app's "
            "'Calendar' section — dated one-off obligations (exams, games, trips, "
            "deadlines, anything out of the ordinary), NOT routine classes/"
            "workouts. The app shows every day of the next 3 weeks as its own "
            "box, then only dated big stuff beyond that; this tool writes any "
            "date either way. text is the FULL list for that day (1-3 lines, "
            "newline-separated); it replaces whatever that date had, and empty "
            "text removes the date. Past dates drop off the app automatically."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "when": {"type": "string", "description": "Date as YYYY-MM-DD (preferred), or a weekday name / 'today' / 'tomorrow'."},
                "text": {"type": "string", "description": "The 1-3 big things that day, newline-separated. Empty removes the date."},
            },
            "required": ["when"],
        },
    },
    {
        "name": "batch_edit_schedule",
        "description": (
            "Apply MANY schedule edits at once — e.g. populating a whole week of "
            "class times parsed from a pasted syllabus — as a single change with a "
            "single undo, instead of one edit_schedule call per block. Same rules as "
            "edit_schedule (weekday/'today'/'tomorrow', wall-clock times, exclusive "
            "end, empty text clears). Bad entries (unresolvable day/time) are skipped "
            "and reported, not fatal to the rest. ALWAYS show him the full list of "
            "blocks you're about to set and get a yes before calling this — bulk "
            "writes are the easiest way to make a mess of his week."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "edits": {
                    "type": "array",
                    "description": "Up to 60 edits.",
                    "items": {
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
            },
            "required": ["edits"],
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
        "name": "log_5050",
        "description": (
            "Log Alex's 50/50 shooting session (threes made out of 50 and free "
            "throws made out of 50) into his training app's Log table — use the "
            "moment he reports numbers ('50/50 was 38 and 44'). Defaults to "
            "today; returns the recent trend so he sees the trajectory."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "threes": {"type": "integer", "description": "Threes made, out of 50."},
                "free_throws": {"type": "integer", "description": "Free throws made, out of 50."},
                "when": {"type": "string", "description": "Session date M/D or YYYY-MM-DD; omit for today."},
            },
            "required": ["threes", "free_throws"],
        },
    },
    {
        "name": "get_5050_trend",
        "description": (
            "Alex's recent 50/50 shooting numbers and bests from the training "
            "app's log — the measurable behind 'is my shooting improving'. Use "
            "for any question about his shooting trend or when reviewing the week."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "edit_library_page",
        "description": (
            "Change text on ONE page of Alex's workout library — a level move "
            "('NOW: L2' → 'NOW: L3'), a number or line he asked to correct, "
            "marking a TO BUILD item done. old_text must match the page body "
            "EXACTLY (whitespace included) and exactly once; read the page with "
            "get_workout_library first and copy verbatim. The library is HIS "
            "content: only make edits he asked for or already agreed to, and "
            "never remove content without his say-so. He can say 'undo that'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "category": {"type": "string", "description": "Library category, e.g. 'Good Drills', 'Court movement'."},
                "page": {"type": "string", "description": "Exact page title within the category."},
                "old_text": {"type": "string", "description": "Exact existing text to replace (must occur exactly once)."},
                "new_text": {"type": "string", "description": "Replacement text. Empty deletes the matched text — only when he asked."},
            },
            "required": ["category", "page", "old_text", "new_text"],
        },
    },
    {
        "name": "append_library_log_row",
        "description": (
            "Add one row to a LOG table page in Alex's workout library (LOG - "
            "Good Drills, LOG - Passing, LOG - Movement + Defense, the lift "
            "logs…) the moment he reports a result. values go in the table's "
            "column order; missing trailing cells stay blank. For 50/50 numbers "
            "keep using log_5050."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "category": {"type": "string", "description": "Library category the table lives in."},
                "page": {"type": "string", "description": "Exact table-page title, e.g. 'LOG - Good Drills'."},
                "values": {"type": "array", "items": {"type": "string"},
                           "description": "Cell values in column order."},
            },
            "required": ["category", "page", "values"],
        },
    },
    {
        "name": "undo_training_edit",
        "description": (
            "Reverse the last change made to Alex's training app (schedule block, "
            "workout card, or library page). Use when he says 'undo that' or 'put "
            "it back'."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_widget_setup",
        "description": (
            "Setup instructions + ready-to-paste Scriptable scripts for Alex's "
            "iPhone home-screen widgets: a schedule widget (what's happening now / "
            "next, from his training app) and a talk-to-CLARVIS button. Use when he "
            "asks to set up, fix, or re-install his phone widgets."
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
    "get_widget_setup": "Building your widget setup…",
    "edit_schedule": "Updating your schedule…",
    "batch_edit_schedule": "Filling in your schedule…",
    "set_big_obligation": "Noting that big day…",
    "set_workout_card": "Rewriting that workout card…",
    "log_5050": "Logging your shooting numbers…",
    "get_5050_trend": "Pulling your shooting trend…",
    "edit_library_page": "Editing that library page…",
    "append_library_log_row": "Adding the log row…",
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
    if tool_name == "get_widget_setup":
        return get_widget_setup()
    if tool_name == "edit_schedule":
        return edit_schedule(tool_input["day"], tool_input["start"],
                             tool_input["end"], tool_input.get("text", ""),
                             bool(tool_input.get("this_week_only")))
    if tool_name == "batch_edit_schedule":
        return batch_edit_schedule(tool_input.get("edits") or [])
    if tool_name == "set_big_obligation":
        return set_big_obligation(tool_input["when"], tool_input.get("text", ""))
    if tool_name == "set_workout_card":
        return set_workout_card(tool_input["day"], tool_input.get("text", ""))
    if tool_name == "log_5050":
        return log_5050(tool_input.get("threes"), tool_input.get("free_throws"),
                        tool_input.get("when", ""))
    if tool_name == "get_5050_trend":
        return fifty_fifty_trend()
    if tool_name == "edit_library_page":
        return edit_library_page(tool_input.get("category", ""), tool_input.get("page", ""),
                                 tool_input.get("old_text", ""), tool_input.get("new_text", ""))
    if tool_name == "append_library_log_row":
        return append_library_log_row(tool_input.get("category", ""),
                                      tool_input.get("page", ""),
                                      tool_input.get("values") or [])
    if tool_name == "undo_training_edit":
        return undo_training_edit()
    return f"Unknown tool: {tool_name}"
