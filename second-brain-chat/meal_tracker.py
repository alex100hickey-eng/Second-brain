"""Meal swipe tracker — Alex's zero-out-of-pocket food plan at CWRU.

The Universal plan's commons are unlimited but he rates that food last resort;
everything he actually wants to eat sits in five LIMITED pools with daily and
weekly caps. This module is the arithmetic: his dictated daily template, what
he actually swiped, what's left in each pool, and the warning before a cap
bites. He dictates the plan; CLARVIS counts — meals are never auto-placed.

Plan numbers come from CWRU's "Meal Plan Breakdown and Comparison" AY26-27
chart; hours from the official AY26-27 hours PDF (case.edu/dining, dated
08.08.26, fetched 2026-08-30). Hours change semester to semester — when a
swipe is refused at a spot this table says is open, the table is stale, not
the register.

Two deliberate assumptions, surfaced in the UI rather than hidden:
  - Weekly caps are assumed to reset Monday 12:00 AM (CWRU doesn't publish
    the reset day). Daily caps reset at midnight.
  - What one swipe BUYS at each retail spot lives only in the Transact app
    ("retail units themselves determine … what they will offer for it" —
    case.edu/dining). We count swipes, not food.

Durability: one Supabase "Agent Outputs" row updated in place (agent_name
"meal_tracker"), the training_sync remembered-row-id pattern, so the table
never grows and polls never re-read Supabase. Weekly rollup keeps the row
small: log entries older than last week compress into per-week pool totals.
"""

import json
import os
import re
import threading
import time
import uuid
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

AGENT_NAME = "meal_tracker"
LOCAL_TZ = ZoneInfo("America/New_York")
MAX_ROW_BYTES = 200_000
RETRY_COOLDOWN_S = 60
REUSE_DELAY_MIN = 10          # "ALL locations are subject to a 10min reuse delay"
CASECASH_SEMESTER = 150.00    # included with the Universal plan
SEMESTER_END = date(2026, 12, 18)   # fall finals week ends; drives the $/wk guide

# ---- the plan (AY26-27 chart, Universal column) ---------------------------

POOLS = {
    "premium":     {"label": "Commons",     "short": "CMNS", "daily": None, "weekly": None},
    "grab_go":     {"label": "Grab & Go",   "short": "G&G",  "daily": 3,    "weekly": 14},
    "convenience": {"label": "Convenience", "short": "CONV", "daily": 4,    "weekly": 15},
    "portable":    {"label": "Portable",    "short": "PORT", "daily": 2,    "weekly": 7},
    "late_night":  {"label": "Late Night",  "short": "LATE", "daily": 2,    "weekly": 7},
    "scholar":     {"label": "Scholar",     "short": "SCHL", "daily": 2,    "weekly": 2},
}
POOL_ORDER = ["grab_go", "convenience", "portable", "late_night", "scholar", "premium"]


def _spread(spans):
    """{(first_dow, last_dow): (open_min, close_min) | None} -> per-day hours.
    Monday=0. close_min may pass 1440 for spots open past midnight (the Den)."""
    out = {}
    for (a, b), win in spans.items():
        for d in range(a, b + 1):
            out[d] = win
    return out


# Hours in minutes from midnight, None = closed that day, missing dict = hours
# unknown (log freely, no open/closed judgement). Where: N = north res village,
# S = south, Q = quad/academic, HEC = health campus (a shuttle ride away).
SPOTS = {
    # premium — unlimited, the fallback tier
    "leutner":      {"label": "Leutner Commons", "pool": "premium", "where": "N",
                     "hours": _spread({(0, 4): (420, 1230), (5, 6): (510, 1200)})},
    "fribley":      {"label": "Fribley Commons", "pool": "premium", "where": "S",
                     "hours": _spread({(0, 4): (480, 1230), (5, 6): (510, 1200)})},
    "carlton":      {"label": "Carlton Commons (dinner only)", "pool": "premium", "where": "S",
                     "hours": _spread({(0, 3): (1080, 1320), (4, 5): None, (6, 6): (1080, 1320)})},
    # grab & go — 3/day, 14/wk
    "dunkin":       {"label": "Dunkin' @ TVUC", "pool": "grab_go", "where": "Q",
                     "hours": _spread({(0, 4): (420, 1080), (5, 6): (480, 900)})},
    "esi":          {"label": "Elephant Step-Inn", "pool": "grab_go", "where": "S",
                     "hours": _spread({(0, 4): (420, 1200), (5, 6): (480, 1440)})},
    "starbucks_brb": {"label": "BRB Starbucks", "pool": "grab_go", "where": "Q",
                     "hours": _spread({(0, 4): (450, 900), (5, 6): None})},
    "hec_press":    {"label": "HEC Press & Bakery", "pool": "grab_go", "where": "HEC",
                     "hours": _spread({(0, 3): (450, 960), (4, 4): (450, 840), (5, 6): None})},
    "ksl_bagit":    {"label": "KSL Bag-it", "pool": "grab_go", "where": "Q",
                     "hours": _spread({(0, 3): (660, 1260), (4, 4): (660, 960), (5, 5): None, (6, 6): (840, 1140)})},
    "pbl_cafe":     {"label": "PBL Café", "pool": "grab_go", "where": "Q",
                     "hours": _spread({(0, 4): (480, 870), (5, 6): None})},
    "tomlinson_cafe": {"label": "Tomlinson 1st Floor Café (opens 9/14)", "pool": "grab_go", "where": "Q"},
    "cafe_quad":    {"label": "Café on the Quad (closes 9/11)", "pool": "grab_go", "where": "Q",
                     "hours": _spread({(0, 4): (480, 960), (5, 6): None})},
    "tomlinson_gg": {"label": "Tomlinson G&G (closes 9/11)", "pool": "grab_go", "where": "Q",
                     "hours": _spread({(0, 4): (600, 900), (5, 6): None})},
    # convenience — 4/day, 15/wk
    "spartie":      {"label": "Spartie Mart", "pool": "convenience", "where": "N",
                     "hours": _spread({(0, 5): (480, 1200), (6, 6): (600, 1140)})},
    "fujisan":      {"label": "FujiSan (in Spartie Mart)", "pool": "convenience", "where": "N",
                     "hours": _spread({(0, 5): (660, 1140), (6, 6): (660, 1020)})},
    "brb_cafe":     {"label": "BRB Café", "pool": "convenience", "where": "Q",
                     "hours": _spread({(0, 4): (480, 900), (5, 6): None})},
    "hec_cafe":     {"label": "HEC Café", "pool": "convenience", "where": "HEC",
                     "hours": _spread({(0, 3): (450, 990), (4, 4): (450, 840), (5, 6): None})},
    # portable — 2/day, 7/wk
    "subway":       {"label": "Subway (Tomlinson)", "pool": "portable", "where": "Q",
                     "hours": _spread({(0, 3): (600, 1020), (4, 4): (600, 900), (5, 6): None})},
    "choolaah":     {"label": "Choolaah (TVUC)", "pool": "portable", "where": "Q",
                     "hours": _spread({(0, 4): (660, 1260), (5, 6): None})},
    "med23":        {"label": "Med23 (TVUC)", "pool": "portable", "where": "Q",
                     "hours": _spread({(0, 4): (660, 1140), (5, 6): (720, 1020)})},
    "pk":           {"label": "PK @ CWRU (TVUC)", "pool": "portable", "where": "Q",
                     "hours": _spread({(0, 4): (660, 1140), (5, 6): None})},
    "local_taco":   {"label": "Local Taco (Tomlinson)", "pool": "portable", "where": "Q",
                     "hours": _spread({(0, 4): (600, 900), (5, 6): None})},
    "cle_table":    {"label": "CLE Table / Fire Grill", "pool": "portable", "where": "Q",
                     "hours": _spread({(0, 4): (660, 900), (5, 6): None})},
    "near_far":     {"label": "Near and Far / Ramen", "pool": "portable", "where": "Q",
                     "hours": _spread({(0, 4): (660, 900), (5, 6): None})},
    # late night — 2/day, 7/wk
    "den":          {"label": "The Den by Denny's", "pool": "late_night", "where": "N",
                     "hours": _spread({(0, 2): (960, 1440), (3, 6): (960, 1560)})},
    "knight_bytes": {"label": "Knight Bytes (Leutner)", "pool": "late_night", "where": "N",
                     "hours": _spread({(0, 4): (1080, 1440), (5, 6): None})},
    "fribley_ln":   {"label": "Fribley Late Night", "pool": "late_night", "where": "S",
                     "hours": _spread({(0, 4): (1230, 1440), (5, 6): None})},
    "hot_honey":    {"label": "Carlton Hot Honey", "pool": "late_night", "where": "S"},
    # scholar — 2/wk
    "jolly":        {"label": "Jolly Scholar", "pool": "scholar", "where": "Q",
                     "hours": _spread({(0, 2): (660, 1380), (3, 4): (660, 1560), (5, 5): (720, 1560), (6, 6): (720, 1380)})},
    "jollys_pizza": {"label": "Jolly's Pizza", "pool": "scholar", "where": "N"},
    "southside":    {"label": "SouthSide Scholar (Carlton)", "pool": "scholar", "where": "S"},
    "road_scholar": {"label": "Road Scholar (truck)", "pool": "scholar", "where": "?"},
}

_ALIASES = {
    "dunkin": "dunkin", "dunkin donuts": "dunkin", "dunkins": "dunkin", "dd": "dunkin",
    "spartie": "spartie", "spartie mart": "spartie", "spartiemart": "spartie",
    "the mart": "spartie", "mart": "spartie",
    "subway": "subway",
    "den": "den", "the den": "den", "dennys": "den", "denny's": "den",
    "jolly": "jolly", "jolly scholar": "jolly", "the jolly": "jolly",
    "starbucks": "starbucks_brb", "brb starbucks": "starbucks_brb",
    "elephant step inn": "esi", "step inn": "esi",
    "ksl": "ksl_bagit", "bag it": "ksl_bagit", "bag-it": "ksl_bagit", "cramelot": "ksl_bagit",
    "fuji": "fujisan", "sushi": "fujisan",
    "taco": "local_taco", "ramen": "near_far", "near and far": "near_far",
    "cle": "cle_table", "fire grill": "cle_table",
    "hot honey": "hot_honey", "knight bytes": "knight_bytes",
    "fribley late night": "fribley_ln", "leutner commons": "leutner",
    "fribley commons": "fribley", "carlton commons": "carlton",
    "pizza": "jollys_pizza",
}


def resolve_spot(name: str):
    """A spot key from whatever he typed/tapped, or None."""
    s = re.sub(r"[^a-z0-9 ]", "", str(name or "").strip().lower()).strip()
    if not s:
        return None
    if s in SPOTS:
        return s
    if s in _ALIASES:
        return _ALIASES[s]
    u = s.replace(" ", "_")
    if u in SPOTS:
        return u
    for key, spot in SPOTS.items():
        if s in spot["label"].lower():
            return key
    return None


# Alex's dictated weekly template (2026-08-30): Dunkin twice, Spartie Mart
# twice, Subway once, the Den once — every day. He said "Dunkin 1-2"; it's
# planned at 2 because 2/day is exactly the 14/wk G&G cap, and a skipped
# Dunkin is the one lever that frees a swipe for any other grab-&-go spot.
# Subway is closed weekends (so Sat/Sun carry 2 free portable swipes — Med23
# brunch is the only weekend portable spot, but HE places meals, not us).
DEFAULT_TEMPLATE = {
    str(dow): (
        [{"spot": "dunkin", "count": 2},
         {"spot": "spartie", "count": 2}]
        + ([{"spot": "subway", "count": 1}] if dow < 5 else [])
        + [{"spot": "den", "count": 1}]
    )
    for dow in range(7)
}

DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

# ---- durable state (training_sync's one-row pattern, simplified) ----------

_supabase = None
_lock = threading.Lock()
_state = {"loaded": False, "row_id": None, "doc": None, "dirty": False,
          "next_hydrate_at": 0.0, "next_persist_at": 0.0}


def _empty_doc():
    return {"log": [], "casecash": [], "history": [], "template": None}


def init(supabase_client):
    global _supabase
    _supabase = supabase_client


def _hydrate_locked():
    if _supabase is None or _state["loaded"]:
        if _state["doc"] is None and _supabase is None:
            _state["doc"] = _empty_doc()
            _state["loaded"] = True
        return
    now = time.monotonic()
    if now < _state["next_hydrate_at"]:
        if _state["doc"] is None:
            _state["doc"] = _empty_doc()
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
            if _state["doc"] is None and isinstance(stored, dict):
                doc = _empty_doc()
                doc.update({k: stored[k] for k in doc if k in stored})
                _state["doc"] = doc
        if _state["doc"] is None:
            _state["doc"] = _empty_doc()
        _state["loaded"] = True
    except Exception as e:
        print(f"[meal_tracker] hydrate failed (will retry): {e}")
        if _state["doc"] is None:
            _state["doc"] = _empty_doc()


def _persist_locked():
    if _supabase is None:
        return
    body = json.dumps(_state["doc"])
    if len(body) > MAX_ROW_BYTES:
        _rollup_locked(keep_weeks=1)
        body = json.dumps(_state["doc"])
    try:
        if _state["row_id"] is not None:
            _supabase.table("Agent Outputs").update({"output_text": body}).eq(
                "id", _state["row_id"]).execute()
        else:
            res = (_supabase.table("Agent Outputs")
                   .insert({"agent_name": AGENT_NAME, "output_text": body}).execute())
            data = getattr(res, "data", None) or []
            if data and isinstance(data[0], dict) and "id" in data[0]:
                _state["row_id"] = data[0]["id"]
        _state["dirty"] = False
    except Exception as e:
        _state["dirty"] = True
        _state["next_persist_at"] = time.monotonic() + RETRY_COOLDOWN_S
        print(f"[meal_tracker] persist failed (kept in memory): {e}")


def _doc():
    """Current doc; caller must hold _lock."""
    _hydrate_locked()
    if _state["dirty"] and time.monotonic() >= _state["next_persist_at"]:
        _persist_locked()
    return _state["doc"]


# ---- time helpers ---------------------------------------------------------

def _now():
    return datetime.now(LOCAL_TZ)


def _entry_dt(entry):
    try:
        dt = datetime.fromisoformat(entry["ts"])
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=LOCAL_TZ)
        return dt.astimezone(LOCAL_TZ)
    except (KeyError, ValueError, TypeError):
        return None


def week_start(d: date) -> date:
    """Monday of d's week — the ASSUMED weekly-cap reset (CWRU doesn't
    publish the reset day; if a register ever disagrees, this is why)."""
    return d - timedelta(days=d.weekday())


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


def _clock_str(minutes: int) -> str:
    m = int(minutes) % 1440
    h, mm = divmod(m, 60)
    ap = "a" if h < 12 else "p"
    h12 = h % 12 or 12
    return f"{h12}:{mm:02d}{ap}"


def hours_window(spot_key: str, dow: int):
    """(open_min, close_min) for a spot on a weekday, None if closed,
    'unknown' if we have no hours for it."""
    hours = SPOTS[spot_key].get("hours")
    if hours is None:
        return "unknown"
    return hours.get(dow)


def window_label(spot_key: str, dow: int) -> str:
    win = hours_window(spot_key, dow)
    if win == "unknown":
        return "hours unknown"
    if win is None:
        return "closed today"
    return f"{_clock_str(win[0])}–{_clock_str(win[1])}"


# ---- aggregation ----------------------------------------------------------

def _template(doc):
    tpl = doc.get("template")
    return tpl if isinstance(tpl, dict) and tpl else DEFAULT_TEMPLATE


def _pool_of(entry):
    spot = SPOTS.get(entry.get("spot"))
    return spot["pool"] if spot else None


def _counts(entries, start: date, end: date):
    """(per_pool, per_spot) swipe counts for start <= local-date < end."""
    pools = {k: 0 for k in POOLS}
    spots = {}
    for e in entries:
        dt = _entry_dt(e)
        if dt is None or not (start <= dt.date() < end):
            continue
        pool = _pool_of(e)
        if pool:
            pools[pool] += 1
            spots[e["spot"]] = spots.get(e["spot"], 0) + 1
    return pools, spots


def _rollup_locked(keep_weeks: int = 2):
    """Compress log entries older than keep_weeks into per-week history
    totals so the Supabase row stays small forever. Caller holds _lock."""
    doc = _state["doc"]
    cutoff = week_start(_now().date()) - timedelta(weeks=keep_weeks - 1)
    keep, old = [], {}
    for e in doc.get("log", []):
        dt = _entry_dt(e)
        if dt is None:
            continue
        if dt.date() >= cutoff:
            keep.append(e)
        else:
            old.setdefault(week_start(dt.date()).isoformat(), []).append(e)
    if not old:
        return
    hist = {h["week_of"]: h for h in doc.get("history", []) if isinstance(h, dict)}
    for wk, entries in old.items():
        pools, spots = _counts(entries, date.min, date.max)
        h = hist.setdefault(wk, {"week_of": wk, "pools": {}, "spots": {}})
        for p, n in pools.items():
            if n:
                h["pools"][p] = h["pools"].get(p, 0) + n
        for s, n in spots.items():
            h["spots"][s] = h["spots"].get(s, 0) + n
    doc["history"] = sorted(hist.values(), key=lambda h: h["week_of"])
    doc["log"] = keep


# ---- the deck payload -----------------------------------------------------

def _plan_counts(tpl, dow: int):
    """{spot: planned_count} for one weekday of the template."""
    out = {}
    for slot in tpl.get(str(dow), []):
        try:
            out[slot["spot"]] = out.get(slot["spot"], 0) + int(slot.get("count", 1))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _pool_plan(tpl, dow: int):
    out = {k: 0 for k in POOLS}
    for spot, n in _plan_counts(tpl, dow).items():
        info = SPOTS.get(spot)
        if info:
            out[info["pool"]] += n
    return out


def deck_data(now=None):
    now = now or _now()
    today = now.date()
    dow = today.weekday()
    ws = week_start(today)
    with _lock:
        doc = _doc()
        entries = sorted(doc.get("log", []), key=lambda e: e.get("ts") or "")
        casecash = list(doc.get("casecash", []))
        tpl = _template(doc)

    week_pools, week_spots = _counts(entries, ws, ws + timedelta(days=7))
    day_pools, day_spots = _counts(entries, today, today + timedelta(days=1))

    # projected week-end usage if he runs the template for the rest of the week
    projected = dict(week_pools)
    for d in range(dow, 7):
        plan = _pool_plan(tpl, d)
        if d == dow:
            done = day_pools
            for p in POOLS:
                projected[p] += max(0, plan[p] - done.get(p, 0))
        else:
            for p in POOLS:
                projected[p] += plan[p]

    pools_out, warnings = [], []
    for key in POOL_ORDER:
        p = POOLS[key]
        used_w, used_d = week_pools[key], day_pools[key]
        row = {"key": key, "label": p["label"], "short": p["short"],
               "daily": p["daily"], "weekly": p["weekly"],
               "used_today": used_d, "used_week": used_w}
        if p["weekly"] is not None:
            row["left_week"] = max(0, p["weekly"] - used_w)
            row["left_today"] = max(0, min(p["daily"] - used_d, row["left_week"]))
            row["projected"] = projected[key]
            if used_w > p["weekly"] or used_d > p["daily"]:
                warnings.append(f"{p['label']} is OVER a cap ({used_d}/day, {used_w}/wk logged) — "
                                "the register may have refused one of these.")
            elif row["left_week"] == 0 and any(
                    SPOTS[s]["pool"] == key and n > day_spots.get(s, 0)
                    for s, n in _plan_counts(tpl, dow).items()):
                warnings.append(f"{p['label']} is spent for the week — today's planned "
                                f"{p['label']} swipe(s) would be refused.")
            elif projected[key] > p["weekly"]:
                warnings.append(f"On pace to exceed {p['label']}: template + extras project "
                                f"{projected[key]}/{p['weekly']} this week.")
        pools_out.append(row)

    # today's plan, with live done-counts and open windows
    plan_today = []
    for slot in tpl.get(str(dow), []):
        spot = slot.get("spot")
        info = SPOTS.get(spot)
        if not info:
            continue
        count = int(slot.get("count", 1))
        win = hours_window(spot, dow)
        nowm = now.hour * 60 + now.minute
        open_now = (win not in (None, "unknown")
                    and (win[0] <= nowm < win[1]
                         or (win[1] > 1440 and nowm < win[1] - 1440)))
        plan_today.append({
            "spot": spot, "label": info["label"], "pool": info["pool"],
            "pool_short": POOLS[info["pool"]]["short"],
            "count": count, "done": day_spots.get(spot, 0),
            "window": window_label(spot, dow), "open_now": bool(open_now),
        })

    week_out = []
    for d in range(7):
        day = ws + timedelta(days=d)
        planned = sum(_plan_counts(tpl, d).values())
        _, spots_d = _counts(entries, day, day + timedelta(days=1))
        week_out.append({"short": DAYS[d][:3].upper(), "name": DAYS[d],
                         "is_today": d == dow, "is_past": day < today,
                         "planned": planned, "logged": sum(spots_d.values())})

    # slack: swipes the template leaves unused this week
    slack = []
    tpl_week = {k: 0 for k in POOLS}
    for d in range(7):
        for p, n in _pool_plan(tpl, d).items():
            tpl_week[p] += n
    for key in POOL_ORDER:
        cap = POOLS[key]["weekly"]
        if cap is not None and tpl_week[key] < cap:
            slack.append(f"{cap - tpl_week[key]} {POOLS[key]['label']}")

    spent = sum(float(c.get("amount", 0) or 0) for c in casecash)
    weeks_left = max(1, round((SEMESTER_END - today).days / 7))
    fridge = sum(n for s, n in week_spots.items()
                 if s in ("spartie", "fujisan"))

    tail = []
    for e in list(reversed(entries))[:12]:
        dt = _entry_dt(e)
        info = SPOTS.get(e.get("spot"), {})
        tail.append({"id": e.get("id"), "label": info.get("label", e.get("spot")),
                     "pool_short": POOLS.get(info.get("pool"), {}).get("short", "?"),
                     "when": dt.strftime("%a %-I:%M%p").lower() if dt else "?",
                     "note": e.get("note") or ""})

    return {
        "generated_at": now.strftime("%-I:%M %p"),
        "today": {"name": DAYS[dow], "date": today.strftime("%b %-d"), "dow": dow},
        "week_of": ws.strftime("%b %-d"),
        "pools": pools_out,
        "today_plan": plan_today,
        "week": week_out,
        "warnings": warnings,
        "slack": slack,
        "commons_week": week_pools["premium"],
        "fridge_runs": fridge,
        "casecash": {"start": CASECASH_SEMESTER, "spent": round(spent, 2),
                     "left": round(CASECASH_SEMESTER - spent, 2),
                     "weeks_left": weeks_left,
                     "per_week": round(max(0.0, CASECASH_SEMESTER - spent) / weeks_left, 2)},
        "log_tail": tail,
        "spots": [{"key": k, "label": s["label"], "pool": s["pool"],
                   "pool_short": POOLS[s["pool"]]["short"],
                   "window": window_label(k, dow)}
                  for k, s in SPOTS.items()],
        "assumptions": [
            "Weekly caps assumed to reset Monday 12:00a — CWRU doesn't publish the reset day.",
            "Hours from CWRU's AY26-27 PDF (Aug 2026); registers win when they disagree.",
            "What a swipe buys at each retail spot is set in Transact — we count swipes, not food.",
        ],
    }


# ---- mutations ------------------------------------------------------------

def _spacing_warning(entries, new_dt, new_spot):
    """The 10-minute reuse rule, checked against the most recent swipe."""
    last, last_dt = None, None
    for e in entries:
        dt = _entry_dt(e)
        if dt is not None and (last_dt is None or dt > last_dt) and dt <= new_dt:
            last, last_dt = e, dt
    if last is None:
        return ""
    gap = (new_dt - last_dt).total_seconds() / 60
    if gap >= REUSE_DELAY_MIN:
        return ""
    same = last.get("spot") == new_spot
    where = SPOTS.get(last["spot"], {}).get("label", "the last spot")
    if same:
        return (f"Only {gap:.0f} min since your last swipe at {where} — the register "
                f"enforces a {REUSE_DELAY_MIN}-minute reuse delay, this one may be refused.")
    return (f"Heads-up: {gap:.0f} min since your last swipe ({where}) — the "
            f"{REUSE_DELAY_MIN}-minute reuse rule may apply across orders.")


def log_swipe(spot_name: str, note: str = "", when: str = "") -> str:
    key = resolve_spot(spot_name)
    if key is None:
        return (f"I don't know the spot {str(spot_name)!r}. Places on the plan: "
                + ", ".join(sorted(s["label"] for s in SPOTS.values())))
    now = _now()
    if when.strip():
        mins = parse_clock(when)
        if mins is None:
            return f"Couldn't read the time {when!r} — try '12:30pm' or '18:00'."
        now = now.replace(hour=mins // 60, minute=mins % 60, second=0, microsecond=0)

    info = SPOTS[key]
    pool = POOLS[info["pool"]]
    entry = {"id": uuid.uuid4().hex[:8], "ts": now.isoformat(),
             "spot": key, "note": str(note or "").strip()}
    with _lock:
        doc = _doc()
        entries = sorted(doc.get("log", []), key=lambda e: e.get("ts") or "")
        notes = []
        s_warn = _spacing_warning(entries, now, key)
        if s_warn:
            notes.append(s_warn)
        win = hours_window(key, now.weekday())
        nowm = now.hour * 60 + now.minute
        if win is None:
            notes.append(f"{info['label']} shows CLOSED today in my hours table — "
                         "if this swipe worked, my hours are stale.")
        elif win != "unknown" and not (win[0] <= nowm < win[1]
                                       or (win[1] > 1440 and nowm < win[1] - 1440)):
            notes.append(f"Outside {info['label']}'s listed hours "
                         f"({window_label(key, now.weekday())}) — logged anyway.")
        doc["log"].append(entry)
        _rollup_locked()
        _persist_locked()
        perr = _state["dirty"]

    d = deck_data()
    row = next(r for r in d["pools"] if r["key"] == info["pool"])
    if pool["weekly"] is None:
        counts = f"commons fallback #{d['commons_week']} this week"
    else:
        counts = (f"{row['used_today']}/{pool['daily']} today, "
                  f"{row['used_week']}/{pool['weekly']} this week "
                  f"({row['left_week']} left)")
    out = f"Logged {info['label']} — {pool['label']}: {counts}."
    for n in notes:
        out += "\n" + n
    for w in d["warnings"]:
        if w not in notes:
            out += "\n" + w
    if perr:
        out += "\nNote: saved in memory but the durable write failed — it will retry."
    return out


def undo_last() -> str:
    with _lock:
        doc = _doc()
        if not doc.get("log"):
            return "Nothing to undo — no swipes logged."
        entries = sorted(doc["log"], key=lambda e: e.get("ts") or "")
        gone = entries[-1]
        doc["log"] = [e for e in doc["log"] if e is not gone]
        _persist_locked()
    info = SPOTS.get(gone.get("spot"), {})
    dt = _entry_dt(gone)
    return (f"Removed the last swipe — {info.get('label', gone.get('spot'))}, "
            f"{dt.strftime('%a %-I:%M%p').lower() if dt else '?'}.")


def log_casecash(amount, note: str = "") -> str:
    try:
        amt = round(float(amount), 2)
    except (TypeError, ValueError):
        return "Couldn't read that amount — give dollars like 4.50."
    if not (0 < amt <= CASECASH_SEMESTER):
        return f"That amount looks off ({amt}) — CaseCash spends are between $0 and ${CASECASH_SEMESTER:.0f}."
    with _lock:
        doc = _doc()
        doc["casecash"].append({"id": uuid.uuid4().hex[:8], "ts": _now().isoformat(),
                                "amount": amt, "note": str(note or "").strip()})
        _persist_locked()
    cc = deck_data()["casecash"]
    return (f"Logged ${amt:.2f} CaseCash. ${cc['left']:.2f} left of ${cc['start']:.0f} "
            f"— guide is ${cc['per_week']:.2f}/wk for the {cc['weeks_left']} weeks left.")


# ---- summaries ------------------------------------------------------------

def status_text() -> str:
    d = deck_data()
    lines = [f"Meal swipes — {d['today']['name']} {d['today']['date']} "
             f"(week of {d['week_of']}, caps assumed to reset Monday):"]
    for r in d["today_plan"]:
        state = f"{r['done']}/{r['count']}"
        mark = "done" if r["done"] >= r["count"] else (
            "open now" if r["open_now"] else r["window"])
        lines.append(f"  {r['label']}: {state} · {mark}")
    lines.append("Pools this week:")
    for r in d["pools"]:
        if r["weekly"] is None:
            lines.append(f"  Commons: unlimited · {d['commons_week']} fallback(s) this week")
        else:
            lines.append(f"  {r['label']}: {r['used_week']}/{r['weekly']} used "
                         f"({r['left_week']} left; today {r['used_today']}/{r['daily']})")
    if d["slack"]:
        lines.append("Unplanned swipes the week leaves free: " + ", ".join(d["slack"]) + ".")
    cc = d["casecash"]
    lines.append(f"CaseCash: ${cc['left']:.2f} left (guide ${cc['per_week']:.2f}/wk).")
    lines.append(f"Fridge-stock runs (Spartie/FujiSan) this week: {d['fridge_runs']}.")
    for w in d["warnings"]:
        lines.append("⚠ " + w)
    return "\n".join(lines)


def hud_summary():
    d = deck_data()
    return {
        "today_done": sum(min(r["done"], r["count"]) for r in d["today_plan"]),
        "today_planned": sum(r["count"] for r in d["today_plan"]),
        "pools": [{"short": r["short"], "left": r.get("left_week"),
                   "weekly": r["weekly"]}
                  for r in d["pools"] if r["weekly"] is not None],
        "commons_week": d["commons_week"],
        "casecash_left": d["casecash"]["left"],
    }


# ---- API wrappers (the /meals page) ---------------------------------------

def api_log(spot: str, note: str = ""):
    msg = log_swipe(spot, note=note)
    return {"ok": not msg.startswith("I don't know"), "message": msg, "deck": deck_data()}


def api_undo():
    return {"ok": True, "message": undo_last(), "deck": deck_data()}


def api_casecash(amount, note: str = ""):
    msg = log_casecash(amount, note)
    return {"ok": msg.startswith("Logged"), "message": msg, "deck": deck_data()}


# ---- chat tools -----------------------------------------------------------

TOOL_SCHEMAS = [
    {
        "name": "log_meal_swipe",
        "description": (
            "Log a dining swipe the moment Alex says he ate/swiped somewhere "
            "('just hit dunkin', 'grabbed subway', 'spartie run'). Counts it "
            "against the right pool (Grab & Go, Convenience, Portable, Late "
            "Night, Scholar, or unlimited Commons) and reports what's left "
            "today and this week, plus cap/spacing warnings."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "spot": {"type": "string", "description": "Where he swiped, e.g. 'dunkin', 'spartie mart', 'subway', 'the den', 'leutner'."},
                "note": {"type": "string", "description": "Optional note, e.g. what the fridge run stocked."},
                "when": {"type": "string", "description": "Optional time like '12:30pm' if he's back-logging; omit for now."},
            },
            "required": ["spot"],
        },
    },
    {
        "name": "get_meal_status",
        "description": (
            "Alex's meal-swipe state: today's dictated meal plan with done-"
            "counts, all six pool counters (used/left today and this week), "
            "CaseCash remaining, fridge-run count, and any cap warnings. Use "
            "for 'how many swipes do I have left', 'what's my food plan "
            "today', 'can I still hit the den'."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "undo_meal_swipe",
        "description": "Remove the most recently logged meal swipe ('undo that', 'I didn't actually go').",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "log_casecash_spend",
        "description": (
            "Log CaseCash spending (the $150 prepaid balance on the Universal "
            "plan) when Alex says he spent CaseCash — tracks remaining balance "
            "against a per-week guide for the rest of the semester."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "amount": {"type": "number", "description": "Dollars spent, e.g. 4.50."},
                "note": {"type": "string", "description": "Optional: what it bought / where."},
            },
            "required": ["amount"],
        },
    },
]

TOOL_STATUS_LABELS = {
    "log_meal_swipe": "Logging that swipe…",
    "get_meal_status": "Checking your swipe counters…",
    "undo_meal_swipe": "Taking that swipe back off…",
    "log_casecash_spend": "Logging that CaseCash spend…",
}

TOOL_NAMES = tuple(t["name"] for t in TOOL_SCHEMAS)


def handle_tool_call(tool_name: str, tool_input: dict) -> str:
    if tool_name == "log_meal_swipe":
        return log_swipe(tool_input.get("spot", ""),
                         note=tool_input.get("note", ""),
                         when=tool_input.get("when", ""))
    if tool_name == "get_meal_status":
        return status_text()
    if tool_name == "undo_meal_swipe":
        return undo_last()
    if tool_name == "log_casecash_spend":
        return log_casecash(tool_input.get("amount"), tool_input.get("note", ""))
    return f"Unknown tool: {tool_name}"
