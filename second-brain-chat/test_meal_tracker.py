"""
test_meal_tracker.py — exercises meal_tracker.py: the plan constants against
the AY26-27 chart, cap/spacing/hours warnings, rollup, and the one-row
Supabase persistence. No network — a fake in-memory Supabase client, same
harness style as test_training_sync.py.

Run:  python3 test_meal_tracker.py
"""

import json
from datetime import datetime, timedelta

import meal_tracker as mt

_results = []


def check(label, cond):
    _results.append(bool(cond))
    print(("  ok " if cond else "  FAIL ") + label)


# ---- fake Supabase ----------------------------------------------------

class FakeQuery:
    def __init__(self, rows):
        self.rows = rows
        self._filters = []
        self._op = None
        self._payload = None

    def insert(self, row):
        self._op, self._payload = "insert", row
        return self

    def update(self, row):
        self._op, self._payload = "update", row
        return self

    def select(self, *a):
        self._op = "select"
        return self

    def eq(self, k, v):
        self._filters.append((k, v))
        return self

    def order(self, *a, **k):
        return self

    def limit(self, n):
        return self

    def execute(self):
        if self._op == "insert":
            rid = len(self.rows) + 1
            rec = {"id": rid, **self._payload}
            self.rows.append(rec)
            return type("R", (), {"data": [rec]})
        if self._op == "update":
            hit = []
            for r in self.rows:
                if all(r.get(k) == v for k, v in self._filters):
                    r.update(self._payload)
                    hit.append(r)
            return type("R", (), {"data": hit})
        hit = [r for r in self.rows
               if all(r.get(k) == v for k, v in self._filters)]
        return type("R", (), {"data": list(reversed(hit))})


class FakeSupabase:
    def __init__(self):
        self.rows = []

    def table(self, name):
        return FakeQuery(self.rows)


def reset(supabase=None):
    mt._supabase = supabase
    mt._state.update({"loaded": False, "row_id": None, "doc": None,
                      "dirty": False, "next_hydrate_at": 0.0,
                      "next_persist_at": 0.0})


NOW = mt._now()
TODAY = NOW.date()
DOW = TODAY.weekday()


def stamp(day_offset=0, hh=12, mm=0):
    d = datetime(TODAY.year, TODAY.month, TODAY.day, hh, mm,
                 tzinfo=mt.LOCAL_TZ) + timedelta(days=day_offset)
    return d.isoformat()


# ---- plan constants vs the AY26-27 chart ------------------------------

print("plan constants")
check("universal pool caps match the chart",
      (mt.POOLS["grab_go"]["daily"], mt.POOLS["grab_go"]["weekly"]) == (3, 14)
      and (mt.POOLS["convenience"]["daily"], mt.POOLS["convenience"]["weekly"]) == (4, 15)
      and (mt.POOLS["portable"]["daily"], mt.POOLS["portable"]["weekly"]) == (2, 7)
      and (mt.POOLS["late_night"]["daily"], mt.POOLS["late_night"]["weekly"]) == (2, 7)
      and (mt.POOLS["scholar"]["weekly"]) == 2
      and mt.POOLS["premium"]["weekly"] is None)
check("limited pools total 45 swipes/week",
      sum(p["weekly"] for p in mt.POOLS.values() if p["weekly"]) == 45)
check("every spot maps to a real pool",
      all(s["pool"] in mt.POOLS for s in mt.SPOTS.values()))

tpl = mt.DEFAULT_TEMPLATE
week_by_pool = {k: 0 for k in mt.POOLS}
for d in range(7):
    for slot in tpl[str(d)]:
        week_by_pool[mt.SPOTS[slot["spot"]]["pool"]] += slot["count"]
check("template: dunkin 2x/day uses G&G at exactly 14/14",
      week_by_pool["grab_go"] == 14)
check("template: spartie 2x/day leaves 1 convenience spare",
      week_by_pool["convenience"] == 14)
check("template: subway weekdays only -> portable 5/7",
      week_by_pool["portable"] == 5)
check("template: den nightly uses late night 7/7",
      week_by_pool["late_night"] == 7)
check("template: scholar untouched (2 free)",
      week_by_pool["scholar"] == 0)
check("template respects every DAILY cap",
      all(sum(slot["count"] for slot in tpl[str(d)]
              if mt.SPOTS[slot["spot"]]["pool"] == pool) <= cap["daily"]
          for d in range(7) for pool, cap in mt.POOLS.items()
          if cap["daily"] is not None))

# ---- hours ------------------------------------------------------------

print("hours")
check("subway closed weekends", mt.hours_window("subway", 5) is None
      and mt.hours_window("subway", 6) is None)
check("subway friday closes 3p", mt.hours_window("subway", 4) == (600, 900))
check("dunkin weekday 7a-6p", mt.hours_window("dunkin", 0) == (420, 1080))
check("dunkin open weekends 8a-3p", mt.hours_window("dunkin", 6) == (480, 900))
check("den runs past midnight thu-sun", mt.hours_window("den", 4)[1] == 1560)
check("carlton closed fri/sat", mt.hours_window("carlton", 4) is None)
check("unknown-hours spot says so", mt.hours_window("hot_honey", 0) == "unknown"
      and mt.window_label("hot_honey", 0) == "hours unknown")
check("window label renders clocks", mt.window_label("dunkin", 0) == "7:00a–6:00p")

# ---- aliases ----------------------------------------------------------

print("aliases")
check("spartie mart resolves", mt.resolve_spot("Spartie Mart") == "spartie")
check("the den resolves", mt.resolve_spot("the den") == "den")
check("dunkin' resolves", mt.resolve_spot("Dunkin'") == "dunkin")
check("label substring resolves", mt.resolve_spot("jolly scholar") == "jolly")
check("nonsense returns None", mt.resolve_spot("chipotle") is None)

# ---- logging + counters ----------------------------------------------

print("logging")
reset(FakeSupabase())
out = mt.log_swipe("dunkin", when="8:00am")
check("first dunkin logs against G&G", "Grab & Go" in out and "1/3 today" in out)
out = mt.log_swipe("dunkin", when="11:00am")
check("second dunkin: 2/3 today, 12 left on the week",
      "2/3 today" in out and "12 left" in out)
d = mt.deck_data()
row = {r["key"]: r for r in d["pools"]}
check("deck counters agree", row["grab_go"]["used_today"] == 2
      and row["grab_go"]["left_week"] == 12)
check("today's plan marks dunkin done 2/2",
      next(r for r in d["today_plan"] if r["spot"] == "dunkin")["done"] == 2)

out = mt.log_swipe("leutner", when="6:00pm")
check("commons logs as fallback, not a cap",
      "fallback #1" in out and mt.deck_data()["commons_week"] == 1)

out = mt.log_swipe("spartie", when="1:00pm", note="yogurt + bars")
check("fridge note kept", any(e["note"] == "yogurt + bars"
                              for e in mt.deck_data()["log_tail"]))
check("fridge run counted", mt.deck_data()["fridge_runs"] == 1)

# spacing: two swipes 5 minutes apart
out = mt.log_swipe("spartie", when="1:05pm")
check("same-spot <10min warns refusal", "may be refused" in out)
out = mt.log_swipe("subway", when="1:09pm")
check("cross-spot <10min gets the soft heads-up", "reuse rule may apply" in out)

# undo
before = len(mt.deck_data()["log_tail"])
out = mt.undo_last()
check("undo removes the newest entry", "Removed the last swipe" in out
      and len(mt.deck_data()["log_tail"]) == before - 1)

# hours notes (subway is closed sat/sun; on weekdays 3am is outside hours)
if DOW >= 5:
    out = mt.log_swipe("subway", when="1:00pm")
    check("closed-day swipe flags stale hours", "CLOSED today" in out)
    mt.undo_last()
else:
    out = mt.log_swipe("subway", when="3:00am")
    check("outside-hours swipe noted, still logged", "listed hours" in out)
    mt.undo_last()

# daily cap: third+fourth dunkin today
mt.log_swipe("dunkin", when="2:00pm")
out = mt.log_swipe("dunkin", when="2:30pm")
check("over daily cap raises the OVER warning", "OVER a cap" in out)
mt.undo_last()
mt.undo_last()

# ---- weekly cap + scholar ---------------------------------------------

print("weekly caps")
reset(FakeSupabase())
mt.log_swipe("jolly", when="12:00pm")
out = mt.log_swipe("jolly", when="12:20pm")
check("scholar hits 0 left at 2 swipes", "0 left" in out)
d = mt.deck_data()
check("scholar projected stays at cap", {r["key"]: r for r in d["pools"]}["scholar"]["projected"] == 2)

# ---- casecash ---------------------------------------------------------

print("casecash")
reset(FakeSupabase())
out = mt.log_casecash(4.50, "smoothie")
check("casecash logs and reports remaining", "$145.50 left" in out)
check("bad amount rejected", "Couldn't read" in mt.log_casecash("lots"))
check("absurd amount rejected", "looks off" in mt.log_casecash(900))
cc = mt.deck_data()["casecash"]
check("per-week guide is remaining/weeks", abs(cc["per_week"] - cc["left"] / cc["weeks_left"]) < 0.02)

# ---- rollup -----------------------------------------------------------

print("rollup")
reset(FakeSupabase())
with mt._lock:
    doc = mt._doc()
    doc["log"] = [
        {"id": "old1", "ts": stamp(day_offset=-21, hh=9), "spot": "dunkin", "note": ""},
        {"id": "old2", "ts": stamp(day_offset=-21, hh=13), "spot": "spartie", "note": ""},
        {"id": "new1", "ts": stamp(day_offset=0, hh=9), "spot": "dunkin", "note": ""},
    ]
    mt._rollup_locked()
    kept = [e["id"] for e in doc["log"]]
    hist = doc["history"]
check("old entries leave the log", kept == ["new1"])
check("history keeps the pool totals",
      len(hist) == 1 and hist[0]["pools"] == {"grab_go": 1, "convenience": 1}
      and hist[0]["spots"] == {"dunkin": 1, "spartie": 1})

# ---- persistence ------------------------------------------------------

print("persistence")
fake = FakeSupabase()
reset(fake)
mt.log_swipe("dunkin", when="8:00am")
mt.log_swipe("den", when="9:00pm")
check("one row, updated in place", len(fake.rows) == 1
      and fake.rows[0]["agent_name"] == "meal_tracker")
stored = json.loads(fake.rows[0]["output_text"])
check("stored doc carries both entries", len(stored["log"]) == 2)

reset(fake)  # cold boot against the same fake — hydrate path
d = mt.deck_data()
check("hydrate restores counters after reboot",
      {r["key"]: r for r in d["pools"]}["late_night"]["used_week"] >= 1)
mt.log_swipe("spartie", when="1:00pm")
check("post-hydrate writes reuse the same row", len(fake.rows) == 1)

check("no supabase client still works in memory",
      (reset(None) or "Logged" in mt.log_swipe("dunkin", when="8:00am")))

# ---- tools + api ------------------------------------------------------

print("tools")
reset(FakeSupabase())
check("tool names registered", mt.TOOL_NAMES ==
      ("log_meal_swipe", "get_meal_status", "undo_meal_swipe", "log_casecash_spend"))
check("every tool has a status label",
      all(n in mt.TOOL_STATUS_LABELS for n in mt.TOOL_NAMES))
out = mt.handle_tool_call("log_meal_swipe", {"spot": "the den", "when": "9:00pm"})
check("tool call logs the den", "Late Night" in out)
out = mt.handle_tool_call("get_meal_status", {})
check("status lists pools and casecash", "Pools this week" in out and "CaseCash" in out)
check("status shows the dictated plan rows", "Dunkin" in out and "Spartie" in out)
api = mt.api_log("subway")
check("api returns deck for atomic re-render", api["ok"] and "pools" in api["deck"])
api = mt.api_log("chipotle")
check("api flags unknown spots", not api["ok"])
hud = mt.hud_summary()
check("hud summary is compact", set(hud) ==
      {"today_done", "today_planned", "pools", "commons_week", "casecash_left"}
      and len(hud["pools"]) == 5)

# ---- verdict ----------------------------------------------------------

passed, total = sum(_results), len(_results)
print(f"\n{passed}/{total} checks passed")
raise SystemExit(0 if passed == total else 1)
