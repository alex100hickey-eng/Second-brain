"""
test_daily_orders.py — exercises daily_orders.py's composer, follow-up union
(the elif-undercount fix), scorecard/streak math and fail-soft behavior. No
network, no real Supabase — a fake in-memory client, same harness style as
test_training_sync.py.

Run:  python3 test_daily_orders.py
"""

import csv
import os
import shutil
import sys
import tempfile
from datetime import date, datetime, time, timedelta, timezone

import ad_creative_pipeline
import august_tracker
import daily_orders
import intake
import school_data
import training_schedule
import training_sync

LOCAL_TZ = daily_orders.LOCAL_TZ

_results = []


def check(label, cond):
    _results.append(bool(cond))
    print(("  ok " if cond else "  FAIL ") + label)


# ---- fake Supabase (with ilike, which intake._load_state depends on) --------

class FakeQuery:
    def __init__(self, rows):
        self.rows = rows
        self._filters = []
        self._ilike = []
        self._limit = None
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

    def ilike(self, k, pattern):
        self._ilike.append((k, pattern.strip("%")))
        return self

    def order(self, *a, **k):
        return self

    def limit(self, n):
        self._limit = n
        return self

    def execute(self):
        if self._op == "insert":
            rec = {"id": len(self.rows) + 1, **self._payload}
            self.rows.append(rec)
            return type("R", (), {"data": [rec]})
        if self._op == "update":
            for r in self.rows:
                if all(r.get(k) == v for k, v in self._filters):
                    r.update(self._payload)
            return type("R", (), {"data": []})
        data = [r for r in self.rows
                if all(r.get(k) == v for k, v in self._filters)
                and all(sub in str(r.get(k, "")) for k, sub in self._ilike)]
        data.sort(key=lambda r: r["id"], reverse=True)
        if self._limit:
            data = data[:self._limit]
        return type("R", (), {"data": data})


class FakeSB:
    def __init__(self):
        self.rows = []

    def table(self, name):
        return FakeQuery(self.rows)


class DownSB:
    """Supabase mid-outage: every access raises."""

    def table(self, name):
        raise ConnectionError("supabase down")


# ---- fixtures ---------------------------------------------------------------

TODAY = datetime.now(LOCAL_TZ).date()
_tmpdirs = []


def _reset(sb=None):
    """Fresh fake Supabase + fresh vault dir + every sibling re-inited."""
    sb = sb or FakeSB()
    vault = tempfile.mkdtemp(prefix="orders-test-")
    _tmpdirs.append(vault)
    intake.supabase = sb
    daily_orders.init(sb)
    ad_creative_pipeline.vault_path = vault
    august_tracker.init(sb, vault, LOCAL_TZ)
    training_sync._state.update({
        "loaded": False, "row_id": None, "snapshot": None, "saved_at": None,
        "dirty": False, "next_hydrate_at": 0.0, "next_persist_at": 0.0, "undo": [],
    })
    training_sync.init(FakeSB())
    if hasattr(school_data, "study_plan_data"):
        del school_data.study_plan_data
    return sb, vault


def _write_tracker_csv(vault, rows):
    os.makedirs(os.path.join(vault, "Money"), exist_ok=True)
    cols = ["brand", "status", "sent_date", "followup1_date",
            "followup2_date", "replied", "outcome"]
    with open(os.path.join(vault, "Money", "prospect-tracker.csv"),
              "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in cols})


def _fake_plan(**overrides):
    """Install a study_plan_data double shaped exactly like the contract."""
    base = {"date": TODAY.isoformat(), "lines": [], "per_course": {},
            "exams": [], "unknown": []}
    base.update(overrides)
    school_data.study_plan_data = lambda for_date=None: base


def _seed_training(grid=None, workouts=None, obligations=None):
    keys = {}
    training_sync._encode(keys, "weeklySchedule.v1", grid or {})
    if workouts:
        training_sync._encode(keys, "weeklyWorkouts.v1", workouts)
    if obligations:
        training_sync._encode(keys, "bigObligations.v1", obligations)
    training_sync.store_snapshot({"rev": "seed", "keys": keys})


# ============================================================
def test_ranking_deadlines_first():
    print("\n=== compose: rank honors deadline > exam runway > money > training > prep ===")
    _, vault = _reset()
    _fake_plan(
        per_course={"ECON103": {
            "due_soon": [{"title": "Problem Set 2", "due": TODAY.isoformat(), "points": 20}],
            "before_next_class": [{"title": "Read chapter 4"}],
            "next_class": (TODAY + timedelta(days=2)).isoformat(),
            "quiz_pointer": None,
        }},
        exams=[{"course": "ECON103", "name": "Midterm 1",
                "date": (TODAY + timedelta(days=7)).isoformat(), "days_out": 7}],
    )
    _write_tracker_csv(vault, [
        {"brand": "Acme", "status": "sent",
         "sent_date": (TODAY - timedelta(days=4)).isoformat()},
    ])
    _seed_training(workouts={str(training_schedule.day_index(TODAY)): "- Ball handling\n- Shooting 200 makes\n- Lift B"})
    res = daily_orders.compose()
    ranks = {}
    for o in res["orders"]:
        if "Problem Set 2" in o["title"]:
            ranks["deadline"] = o["rank"]
        if "runway" in o["title"]:
            ranks["exam"] = o["rank"]
        if "follow-up" in o["title"]:
            ranks["money"] = o["rank"]
        if "4-6 PM window" in o["title"]:
            ranks["training"] = o["rank"]
        if "Read chapter 4" in o["title"]:
            ranks["prep"] = o["rank"]
    check("all five tiers composed", len(ranks) == 5)
    check("deadline before exam runway", ranks.get("deadline", 99) < ranks.get("exam", 0))
    check("exam runway before money follow-up", ranks.get("exam", 99) < ranks.get("money", 0))
    check("money before training", ranks.get("money", 99) < ranks.get("training", 0))
    check("training before prep", ranks.get("training", 99) < ranks.get("prep", 0))
    deadline = next(o for o in res["orders"] if "Problem Set 2" in o["title"])
    check("deadline why names the points at stake", "20 pts" in deadline["why"])
    check("orders capped at 8", len(res["orders"]) <= 8)
    check("ranks are 1..n sequential",
          [o["rank"] for o in res["orders"]] == list(range(1, len(res["orders"]) + 1)))


def test_elif_undercount_fix():
    print("\n=== follow-ups: 9-day-old send with neither follow-up → BOTH stages due ===")
    _, vault = _reset()
    _write_tracker_csv(vault, [
        {"brand": "Niner", "status": "sent",
         "sent_date": (TODAY - timedelta(days=9)).isoformat()},
    ])
    pipeline_view = ad_creative_pipeline.due_followups()
    check("pipeline's elif undercounts (only +3d)",
          [d["which"] for d in pipeline_view if d["brand"] == "Niner"] == ["+3d"])
    fu = daily_orders._followups_due(TODAY)
    stages = sorted(d["which"] for d in fu["due"] if d["brand"] == "Niner")
    check("our union emits BOTH +3d and +7d", stages == ["+3d", "+7d"])
    res = daily_orders.compose()
    titles = [o["title"] for o in res["orders"]]
    check("+7d escalation surfaces as an order",
          any("+7d follow-up — Niner" in t for t in titles))
    check("no unlogged flag when the CSV knows the send", fu["unlogged"] == [])


def test_sends_logged_but_csv_empty():
    print("\n=== sends state without CSV sent_date → 'log your sends' order ===")
    _, vault = _reset()
    _write_tracker_csv(vault, [{"brand": "GhostBrand", "status": "qualified"}])
    out = daily_orders.log_outreach_send("GhostBrand",
                                         (TODAY - timedelta(days=4)).isoformat())
    check("send logged with both clocks named", "+3d" in out and "+7d" in out)
    fu = daily_orders._followups_due(TODAY)
    check("brand flagged unlogged", fu["unlogged"] == ["GhostBrand"])
    check("state-only send still starts the +3d clock",
          any(d["brand"] == "GhostBrand" and d["which"] == "+3d" for d in fu["due"]))
    titles = [o["title"] for o in daily_orders.compose()["orders"]]
    check("compose carries the log-your-sends order",
          any("Log your sends" in t for t in titles))


def test_scorecard_roundtrip_and_streaks():
    print("\n=== scorecard: round-trip, ✓/✗ rendering, streak math with a break ===")
    _reset()
    # Seed three prior days through the real persistence path, then log today.
    days = {}
    days[(TODAY - timedelta(days=3)).isoformat()] = {
        "note": "old", "pillars": {"school": True, "ball": True, "money": False, "sleep": True}}
    days[(TODAY - timedelta(days=2)).isoformat()] = {
        "note": "", "pillars": {"school": True, "ball": True, "money": False, "sleep": True}}
    days[(TODAY - timedelta(days=1)).isoformat()] = {
        "note": "", "pillars": {"school": True, "ball": False, "money": False, "sleep": True}}
    intake._save_state({"key": daily_orders.SCORECARD_KEY, "days": days})
    out = daily_orders.log_scorecard(
        "solid day", {"school": True, "ball": True, "money": False, "sleep": True})
    check("log confirms today's date", TODAY.isoformat() in out)
    streaks = daily_orders.compose()["streaks"]
    check("unbroken pillar counts all 4 days", streaks["school"] == 4)
    check("yesterday's ✗ breaks the ball streak at 1", streaks["ball"] == 1)
    check("never-true pillar streak is 0", streaks["money"] == 0)
    text = daily_orders.scorecard_text(7)
    check("scorecard renders ✓ and ✗ with the note",
          "✓" in text and "✗" in text and "solid day" in text)
    # A gap day (nothing logged) also breaks a streak.
    gap = {
        (TODAY - timedelta(days=3)).isoformat(): {"note": "", "pillars": {"sleep": True}},
        TODAY.isoformat(): {"note": "", "pillars": {"sleep": True}},
    }
    check("a missing day breaks the streak", daily_orders._streaks(gap)["sleep"] == 1)


def test_failsoft_school_absent_and_supabase_down():
    print("\n=== fail-soft: no study_plan_data yet / Supabase down ===")
    _reset()  # _reset deletes any study_plan_data double
    res = daily_orders.compose()
    check("compose survives a school_data without study_plan_data",
          isinstance(res, dict) and "orders" in res)
    check("no school orders when the plan is absent",
          not any(o["pillar"] == "school" for o in res["orders"]))
    _reset(sb=DownSB())
    res = daily_orders.compose()
    check("compose survives a dead Supabase", isinstance(res, dict))
    check("streaks degrade to zeros", set(res["streaks"].values()) == {0})
    out = daily_orders.log_scorecard("day", {"school": True})
    check("log_scorecard warns instead of raising", "couldn't persist" in out)
    check("scorecard_text degrades to the empty message",
          "No scorecard entries" in daily_orders.scorecard_text())


def test_local_tz_discipline():
    print("\n=== LOCAL_TZ: every 'today' is Alex's day, not the container's ===")
    check("module pins America/New_York", str(daily_orders.LOCAL_TZ.key) == "America/New_York")
    _reset()
    res = daily_orders.compose()
    check("default now composes for the ET date",
          res["date"] == datetime.now(LOCAL_TZ).date().isoformat())
    # 2 AM UTC is still the previous evening in ET — the date must not flip.
    utc_now = datetime(2026, 8, 22, 2, 0, tzinfo=timezone.utc)
    check("aware UTC input converts to the ET date",
          daily_orders.compose(utc_now)["date"] == "2026-08-21")


def test_training_orders_and_sleep_guard():
    print("\n=== training: card, obligations routing, 50/50 log, sleep guard ===")
    _reset()
    tomorrow = TODAY + timedelta(days=1)
    _seed_training(
        grid={f"12|{training_schedule.day_index(tomorrow)}": "ECON 103 Lecture"},
        workouts={str(training_schedule.day_index(TODAY)): "- Ball handling\n- Shooting"},
        obligations=[{"date": TODAY.isoformat(), "text": "Team practice 6-8pm"},
                     {"date": TODAY.isoformat(), "text": "Call with advisor office"}],
    )
    res = daily_orders.compose()
    by_title = {o["title"]: o for o in res["orders"]}
    card = next((o for o in res["orders"] if "4-6 PM window" in o["title"]), None)
    check("workout card becomes the 4-6 PM order with first 2 bullets",
          card and "Ball handling" in card["title"] and "Shooting" in card["title"])
    practice = by_title.get("Team practice 6-8pm")
    check("practice obligation routed to ball", practice and practice["pillar"] == "ball")
    life = by_title.get("Call with advisor office")
    check("non-ball obligation routed to life", life and life["pillar"] == "life")
    check("50/50 log appended on a training day",
          any("50/50" in o["title"] for o in res["orders"]))
    guard = next((o for o in res["orders"] if "Lights out" in o["title"]), None)
    check("9:00am first block triggers the sleep guard with its time",
          guard is not None and "9:00am" in guard["title"])


def test_brief_and_evening_lines():
    print("\n=== brief_lines / evening_lines shapes ===")
    _, vault = _reset()
    long_title = "An extremely long assignment title that goes on and on " * 3
    _fake_plan(per_course={"MATH121": {
        "due_soon": [{"title": long_title, "due": TODAY.isoformat()},
                     {"title": "Short one", "due": TODAY.isoformat()}],
        "before_next_class": [], "next_class": None, "quiz_pointer": None,
    }})
    lines = daily_orders.brief_lines(limit=4)
    check("brief respects the limit", 0 < len(lines) <= 4)
    check("brief lines are 'N) title — why'",
          all(lines[i].startswith(f"{i + 1}) ") and " — " in lines[i]
              for i in range(len(lines))))
    check("brief lines truncate at 90 chars", all(len(l) <= 90 for l in lines))
    ev = daily_orders.evening_lines()
    check("evening opens with the scorecard ask",
          ev[0].startswith("Scorecard time:") and "school/ball/money/sleep" in ev[0])
    check("evening previews at most 2 tomorrow orders",
          len(ev) <= 3 and all(l.startswith("Tomorrow: ") for l in ev[1:]))


def test_tool_surface():
    print("\n=== tool surface: names, dispatch, labels ===")
    _reset()
    check("all four tools registered",
          daily_orders.TOOL_NAMES == ("get_daily_orders", "log_scorecard",
                                      "get_scorecard", "log_outreach_send"))
    check("every tool has a status label",
          set(daily_orders.TOOL_STATUS_LABELS) == set(daily_orders.TOOL_NAMES))
    out = daily_orders.handle_tool_call("get_daily_orders", {"day": "today"})
    check("get_daily_orders dispatches", "Daily orders" in out)
    out = daily_orders.handle_tool_call("get_daily_orders", {"day": "not-a-day"})
    check("junk day rejected with guidance", "Couldn't read" in out)
    out = daily_orders.handle_tool_call("log_scorecard",
                                        {"note": "ok day", "pillars": {"ball": True}})
    check("log_scorecard dispatches", "Scorecard logged" in out)
    check("get_scorecard dispatches",
          "ok day" in daily_orders.handle_tool_call("get_scorecard", {}))
    check("unknown tool named back",
          "Unknown tool" in daily_orders.handle_tool_call("nope", {}))



def test_string_study_plan_items():
    """school_data's real study plan emits STRINGS ("<title> (<type>) — due Thu
    Aug 27"), not dicts — the shape the live smoke test caught ranking wrong:
    a string item due TODAY was landing in the prep tier instead of deadline."""
    print("\n=== string study-plan items: due parsing, tiering, title strip ===")
    from datetime import date as _date
    anchor_day = _date(2026, 8, 22)
    check("string due tail parses to a real date",
          daily_orders._item_due("Day 1 HW (homework) — due Thu Aug 27", anchor_day)
          == _date(2026, 8, 27))
    check("no anchor -> no date (never guess a year)",
          daily_orders._item_due("x — due Thu Aug 27") is None)
    check("New Year wrap goes forward",
          daily_orders._item_due("final — due Fri Jan 5", _date(2026, 12, 20))
          == _date(2027, 1, 5))
    check("hyphen variant parses",
          daily_orders._item_due("thing - due Mon Sep 1", anchor_day) == _date(2026, 9, 1))
    check("plain title stays undated",
          daily_orders._item_due("just a title (reading)", anchor_day) is None)

    fake_plan = {"date": "2026-08-22", "lines": [], "unknown": [], "exams": [],
                 "per_course": {"ACCT100": {
                     "due_soon": ["Day 1 HW (homework) — due Sat Aug 22",
                                  "Day 2 APQ (assignment) — due Thu Aug 27"],
                     "before_next_class": [], "next_class": "2026-08-25",
                     "quiz_pointer": None}}}
    import school_data
    orig = getattr(school_data, "study_plan_data", None)
    school_data.study_plan_data = lambda for_date=None: fake_plan
    try:
        pairs = daily_orders._school_orders(anchor_day)
    finally:
        if orig is not None:
            school_data.study_plan_data = orig
    tiers = {o["title"]: t for t, o in pairs}
    today_titles = [t for t in tiers if "Day 1 HW" in t]
    later_titles = [t for t in tiers if "Day 2 APQ" in t]
    check("string item due TODAY lands in the deadline tier",
          bool(today_titles) and tiers[today_titles[0]] == daily_orders._T_DEADLINE)
    check("string item due later lands in the prep tier",
          bool(later_titles) and tiers[later_titles[0]] == daily_orders._T_PREP)
    check("parsed titles drop the due tail (no dup with the why)",
          bool(today_titles) and "due Sat" not in today_titles[0])


# ============================================================
if __name__ == "__main__":
    try:
        test_ranking_deadlines_first()
        test_elif_undercount_fix()
        test_sends_logged_but_csv_empty()
        test_scorecard_roundtrip_and_streaks()
        test_failsoft_school_absent_and_supabase_down()
        test_local_tz_discipline()
        test_training_orders_and_sleep_guard()
        test_brief_and_evening_lines()
        test_tool_surface()
        test_string_study_plan_items()
    finally:
        for d in _tmpdirs:
            shutil.rmtree(d, ignore_errors=True)
    total, passed = len(_results), sum(_results)
    print("\n" + "=" * 48)
    print(f"{passed}/{total} checks passed")
    print("=" * 48)
    sys.exit(0 if passed == total else 1)
