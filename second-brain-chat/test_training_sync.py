"""
test_training_sync.py — exercises training_sync.py's write tools, with a
particular focus on batch_edit_schedule (bulk schedule population in one
mutation / one undo). No network, no real Supabase — a fake in-memory
Supabase client, same harness style as test_mail_and_escalation.py.

Run:  python3 test_training_sync.py
"""

import sys

import training_schedule
import training_sync

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
            for r in self.rows:
                if all(r.get(k) == v for k, v in self._filters):
                    r.update(self._payload)
            return type("R", (), {"data": []})
        data = [r for r in self.rows if all(r.get(k) == v for k, v in self._filters)]
        data.sort(key=lambda r: r["id"], reverse=True)
        return type("R", (), {"data": data})


class FakeSB:
    def __init__(self):
        self.rows = []

    def table(self, name):
        return FakeQuery(self.rows)


def _reset(seed_grid=None):
    """Fresh fake Supabase + fresh module state, optionally pre-seeded with a
    weeklySchedule.v1 grid ({"slot|col": "text"})."""
    training_sync._state.update({
        "loaded": False, "row_id": None, "snapshot": None, "saved_at": None,
        "dirty": False, "next_hydrate_at": 0.0, "next_persist_at": 0.0, "undo": [],
    })
    sb = FakeSB()
    training_sync.init(sb)
    keys = {}
    if seed_grid is not None:
        training_sync._encode(keys, "weeklySchedule.v1", seed_grid)
    err = training_sync.store_snapshot({"rev": "seed", "keys": keys})
    check("seed snapshot stored cleanly", err == "")
    return sb


# ============================================================
def test_batch_populates_multiple_blocks():
    print("\n=== batch_edit_schedule: bulk-populate an empty week ===")
    _reset(seed_grid={})
    edits = [
        {"day": "monday", "start": "9am", "end": "10am", "text": "ECON 103 Lecture"},
        {"day": "wednesday", "start": "9am", "end": "10am", "text": "ECON 103 Lecture"},
        {"day": "friday", "start": "9am", "end": "10am", "text": "ECON 103 Lecture"},
    ]
    out = training_sync.batch_edit_schedule(edits)
    check("reports all 3 applied", "Applied 3 of 3 edits" in out)
    check("no skipped section", "Skipped" not in out)
    check("undo mentioned", "undo that" in out)
    for day in ("monday", "wednesday", "friday"):
        sched = training_sync.get_training_schedule(day)
        check(f"{day} shows the lecture block", "9:00am" in sched and "ECON 103 Lecture" in sched)
    check("single undo entry for the whole batch", len(training_sync._state["undo"]) == 1)


def test_batch_mixed_valid_and_invalid():
    print("\n=== batch_edit_schedule: some entries fail to resolve ===")
    _reset(seed_grid={})
    edits = [
        {"day": "monday", "start": "2pm", "end": "3pm", "text": "Lift"},
        {"day": "someday", "start": "2pm", "end": "3pm", "text": "bad day"},
        {"day": "tuesday", "start": "9am", "end": "9am", "text": "bad range"},
    ]
    out = training_sync.batch_edit_schedule(edits)
    check("1 of 3 applied", "Applied 1 of 3 edits" in out)
    check("2 skipped", "Skipped 2" in out)
    check("bad day named in the skip list", "someday" in out)
    check("good edit still landed", "Lift" in training_sync.get_training_schedule("monday"))


def test_batch_all_invalid_writes_nothing():
    print("\n=== batch_edit_schedule: every entry unresolvable ===")
    _reset(seed_grid={})
    before = training_sync.get_snapshot()["rev"]
    out = training_sync.batch_edit_schedule([
        {"day": "someday", "start": "2pm", "end": "3pm", "text": "x"},
        {"day": "monday", "start": "9am", "end": "9am", "text": "y"},
    ])
    check("reports nothing could be resolved", "could be resolved" in out)
    check("no mutation happened (rev unchanged)", training_sync.get_snapshot()["rev"] == before)
    check("no undo entry pushed", training_sync._state["undo"] == [])


def test_batch_noop_when_already_matching():
    print("\n=== batch_edit_schedule: edits that change nothing ===")
    _reset(seed_grid={})
    training_sync.batch_edit_schedule([{"day": "monday", "start": "9am", "end": "10am", "text": "Lift"}])
    before = training_sync.get_snapshot()["rev"]
    out = training_sync.batch_edit_schedule([{"day": "monday", "start": "9am", "end": "10am", "text": "Lift"}])
    check("no-op batch reports nothing changed", "already matched" in out)
    check("no-op batch writes nothing (rev unchanged)", training_sync.get_snapshot()["rev"] == before)


def test_batch_empty_and_oversized():
    print("\n=== batch_edit_schedule: empty list / too many edits ===")
    _reset(seed_grid={})
    check("empty list rejected", "No edits given" in training_sync.batch_edit_schedule([]))
    before = training_sync.get_snapshot()["rev"]
    too_many = [{"day": "monday", "start": "9am", "end": "10am", "text": "x"}] * (training_sync.MAX_BATCH_EDITS + 1)
    out = training_sync.batch_edit_schedule(too_many)
    check("oversized batch rejected", "split into batches" in out)
    check("oversized batch touched nothing", training_sync.get_snapshot()["rev"] == before)


def test_batch_clear_and_replace_reporting():
    print("\n=== batch_edit_schedule: clearing + overwrite reporting ===")
    _reset(seed_grid={})
    training_sync.batch_edit_schedule([
        {"day": "tuesday", "start": "1pm", "end": "2pm", "text": "Old thing"},
    ])
    out = training_sync.batch_edit_schedule([
        {"day": "tuesday", "start": "1pm", "end": "2pm", "text": "New thing"},  # overwrite
        {"day": "wednesday", "start": "5pm", "end": "6pm", "text": ""},          # clears (nothing there -> no-op)
    ])
    check("overwrite reported as replacing the old text", "replacing Old thing" in out)
    check("new text landed", "New thing" in training_sync.get_training_schedule("tuesday"))


def test_batch_undo_reverts_whole_batch_in_one_step():
    print("\n=== undo_training_edit reverses an entire batch at once ===")
    _reset(seed_grid={})
    training_sync.batch_edit_schedule([{"day": "thursday", "start": "8am", "end": "9am", "text": "Original"}])
    out = training_sync.batch_edit_schedule([
        {"day": "thursday", "start": "8am", "end": "9am", "text": "Changed A"},
        {"day": "friday", "start": "8am", "end": "9am", "text": "Changed B"},
    ])
    check("batch applied", "Applied 2 of 2 edits" in out)
    training_sync.undo_training_edit()
    thu = training_sync.get_training_schedule("thursday")
    fri = training_sync.get_training_schedule("friday")
    check("one undo restores pre-batch Thursday", "Original" in thu and "Changed A" not in thu)
    check("one undo restores pre-batch Friday (nothing booked)", "Nothing blocked" in fri)


def test_batch_before_any_sync():
    print("\n=== batch_edit_schedule before the training app has ever synced ===")
    training_sync._state.update({
        "loaded": False, "row_id": None, "snapshot": None, "saved_at": None,
        "dirty": False, "next_hydrate_at": 0.0, "next_persist_at": 0.0, "undo": [],
    })
    training_sync.init(FakeSB())
    out = training_sync.batch_edit_schedule([{"day": "monday", "start": "9am", "end": "10am", "text": "x"}])
    check("tells him to connect the app first", "hasn't synced" in out)


# ============================================================
# One-week layer (weeklyOnce.v1) + big obligations (bigObligations.v1)
# — app v6's weekly-reset features. All date math uses LOCAL_TZ via the
# grid frame (current_position), never host-local time.

from datetime import datetime, timedelta


def _grid_now():
    now = datetime.now(training_sync.LOCAL_TZ)
    col_now, _ = training_schedule.current_position(now.replace(tzinfo=None))
    return now, col_now, training_schedule.week_start(col_now)


def test_once_layer_parses_and_overrides():
    print("\n=== one-week layer: parse, merge, and override ===")
    now, col_now, ws = _grid_now()
    col = training_schedule.day_index(col_now)
    keys = {}
    training_sync._encode(keys, "weeklySchedule.v1", {f"12|{col}": "Class"})
    training_sync._encode(keys, "weeklyOnce.v1", {
        "weekStart": ws.isoformat(),
        "cells": {f"12|{col}": "Dentist", f"20|{col}": "One-off thing"},
    })
    training_sync._state.update({
        "loaded": False, "row_id": None, "snapshot": None, "saved_at": None,
        "dirty": False, "next_hydrate_at": 0.0, "next_persist_at": 0.0, "undo": [],
    })
    training_sync.init(FakeSB())
    training_sync.store_snapshot({"rev": "seed", "keys": keys})
    p = training_sync.parsed()
    check("once week start parsed", p["once_week_start"] == ws)
    check("once cells parsed", p["once_cells"].get((20, col)) == "One-off thing")
    evs = training_schedule.events_for_date(p, col_now)
    titles = [e["title"] for e in evs]
    check("one-off entry appears", "One-off thing" in titles)
    check("once entry overrides the repeating cell", "Dentist" in titles and "Class" not in titles)
    week = training_sync.get_training_schedule("week")
    check("week view calls out this-week-only entries", "This week only" in week and "Dentist" in week)
    payload = training_sync.week_payload(now)
    day_blocks = [b["label"] for d in payload["days"] if d["index"] == col for b in d["blocks"]]
    check("/schedule payload shows the override too", "Dentist" in day_blocks and "Class" not in day_blocks)

    # A STALE layer (last week's) must be ignored everywhere.
    training_sync._encode(keys, "weeklyOnce.v1", {
        "weekStart": (ws - timedelta(days=7)).isoformat(),
        "cells": {f"12|{col}": "Dentist"},
    })
    training_sync.store_snapshot({"rev": "seed2", "keys": keys})
    p = training_sync.parsed()
    titles = [e["title"] for e in training_schedule.events_for_date(p, col_now)]
    check("stale once layer ignored — repeating grid shows", titles == ["Class"])
    check("stale layer absent from week view", "This week only" not in training_sync.get_training_schedule("week"))


def test_once_layer_failsoft_on_junk():
    print("\n=== one-week layer: junk data parses fail-soft ===")
    keys = {}
    keys[training_schedule.safe_key("weeklyOnce.v1")] = "not json {"
    keys[training_schedule.safe_key("bigObligations.v1")] = '{"wrong": "shape"}'
    p = training_schedule.parse_snapshot({"rev": "x", "keys": keys})
    check("junk once layer -> empty", p["once_week_start"] is None and p["once_cells"] == {})
    check("junk obligations -> empty list", p["obligations"] == [])
    p2 = training_schedule.parse_snapshot({"rev": "x", "keys": {}})
    check("missing keys -> defaults", p2["once_cells"] == {} and p2["obligations"] == [])


def test_edit_schedule_this_week_only():
    print("\n=== edit_schedule this_week_only ===")
    now, col_now, ws = _grid_now()
    col = training_schedule.day_index(col_now)
    _reset(seed_grid={f"12|{col}": "Class"})
    expected_ok = ws <= now.date() < ws + timedelta(days=7)
    out = training_sync.edit_schedule("today", "9am", "10am", "Advisor meeting",
                                      this_week_only=True)
    if expected_ok:
        check("one-off write says this week only", "this week only" in out)
        snap = training_sync.get_snapshot()
        once = training_sync._decode(snap["keys"], "weeklyOnce.v1")
        check("weekStart pinned to the grid's current week", once.get("weekStart") == ws.isoformat())
        check("cells landed in the once layer", "Advisor meeting" in (once.get("cells") or {}).values())
        grid = training_sync._decode(snap["keys"], "weeklySchedule.v1")
        check("repeating grid untouched", list(grid.values()) == ["Class"])
        p = training_sync.parsed()
        titles = [e["title"] for e in training_schedule.events_for_date(p, now.date())]
        check("one-off shows for today", "Advisor meeting" in titles)
        # clearing an empty once span explains the show-through rule
        out2 = training_sync.edit_schedule("today", "1pm", "2pm", "", this_week_only=True)
        check("clearing empty once span explains show-through", "shows through" in out2)
    else:
        # pre-3AM Sunday edge: calendar-today belongs to the NEXT grid week
        check("pre-rollover edge rejected with guidance", "outside the current week" in out)

    if training_schedule.day_index(col_now) != 0 and col_now == now.date():
        # the weekday furthest out resolves to next week -> must be rejected
        name6 = training_schedule.DAYS[(training_schedule.day_index(now.date()) + 6) % 7]
        out3 = training_sync.edit_schedule(name6, "9am", "10am", "X", this_week_only=True)
        check("future-week one-off rejected toward big stuff",
              "outside the current week" in out3 and "set_big_obligation" in out3)
    else:
        print("  (skipped future-week rejection — grid week just started)")


def test_set_big_obligation_lifecycle():
    print("\n=== set_big_obligation: add / replace / remove ===")
    now, _col, _ws = _grid_now()
    _reset(seed_grid={})
    iso = (now.date() + timedelta(days=10)).isoformat()
    out = training_sync.set_big_obligation(iso, "Physics exam\nTeam dinner")
    check("add reports added", "Added big stuff" in out)
    week = training_sync.get_training_schedule("week")
    check("week view lists it", "Big stuff coming up" in week and "Physics exam / Team dinner" in week)
    payload = training_sync.week_payload(now)
    check("/schedule payload carries obligations", any(o["date"] == iso for o in payload["obligations"]))
    out = training_sync.set_big_obligation(iso, "Physics exam moved to 9am")
    check("same date replaces", "Replaced" in out)
    out = training_sync.set_big_obligation(iso, "")
    check("empty text removes (and echoes old)", "Removed" in out and "Physics exam moved" in out)
    out = training_sync.set_big_obligation(iso, "")
    check("removing nothing explains", "nothing to remove" in out)
    out = training_sync.set_big_obligation("2020-01-01", "too late")
    check("past dates rejected", "already passed" in out)
    out = training_sync.set_big_obligation("not-a-date", "x")
    check("junk date rejected", "Couldn't read" in out)
    out = training_sync.set_big_obligation("tomorrow", "Early flight")
    check("day words resolve", "Added big stuff" in out)
    tomorrow_view = training_sync.get_training_schedule("tomorrow")
    check("day view shows big stuff for that date", "Big stuff on" in tomorrow_view and "Early flight" in tomorrow_view)


# ============================================================
def _reset_with_library():
    """Fresh state seeded with a small workoutLibrary.v2: one text page with a
    level ladder (Good Drills) and one LOG table (Court movement)."""
    training_sync._state.update({
        "loaded": False, "row_id": None, "snapshot": None, "saved_at": None,
        "dirty": False, "next_hydrate_at": 0.0, "next_persist_at": 0.0, "undo": [],
    })
    sb = FakeSB()
    training_sync.init(sb)
    keys = {}
    lib = {
        "1": {"sel": 0, "pages": [
            {"title": "Good Handling",
             "body": "CRAWL SPIN FINISH\nNOW: L2\n\nWALL TAPS\nNOW: L2\n"},
        ]},
        "6": {"sel": 0, "pages": [
            {"title": "LOG - Movement + Defense", "type": "table",
             "columns": ["Date", "Drill", "Level", "Notes"],
             "rows": [["", "", "", ""], ["", "", "", ""]]},
        ]},
    }
    training_sync._encode(keys, "workoutLibrary.v2", lib)
    err = training_sync.store_snapshot({"rev": "seed", "keys": keys})
    check("library seed stored cleanly", err == "")
    return sb


def _library_page(cat_idx, title):
    snap = training_sync.get_snapshot()
    lib = training_sync._decode(snap["keys"], "workoutLibrary.v2")
    return next(p for p in lib[cat_idx]["pages"] if p["title"] == title)


def test_edit_library_page():
    print("\n=== edit_library_page: unique-match body edits ===")
    _reset_with_library()
    out = training_sync.edit_library_page(
        "Good Drills", "Good Handling", "CRAWL SPIN FINISH\nNOW: L2", "CRAWL SPIN FINISH\nNOW: L3")
    check("edit reports itself", "Edited 'Good Handling'" in out)
    check("devices/undo boilerplate present", "undo that" in out)
    body = _library_page("1", "Good Handling")["body"]
    check("level moved on the page", "CRAWL SPIN FINISH\nNOW: L3" in body)
    check("other ladder untouched", "WALL TAPS\nNOW: L2" in body)
    rev = training_sync.get_snapshot()["rev"]
    check("new rev minted", rev != "seed")

    out = training_sync.edit_library_page("Good Drills", "Good Handling", "NOW: L2", "NOW: L4")
    check("ambiguous before the edit? no — now unique after L3 move", "Edited" in out)
    _reset_with_library()
    out = training_sync.edit_library_page("Good Drills", "Good Handling", "NOW: L2", "NOW: L3")
    check("ambiguous match refused with count", "appears 2 times" in out)
    check("nothing written on refusal", training_sync.get_snapshot()["rev"] == "seed")
    out = training_sync.edit_library_page("Good Drills", "Good Handling", "NOT ON PAGE", "x")
    check("missing text refused, points at verbatim read", "isn't on 'Good Handling'" in out)
    out = training_sync.edit_library_page("Good Drills", "Nope Page", "a", "b")
    check("unknown page lists real pages", "No page 'Nope Page'" in out and "Good Handling" in out)
    out = training_sync.edit_library_page("Wrong Cat", "Good Handling", "a", "b")
    check("unknown category lists categories", "No library category" in out and "Good Drills" in out)
    out = training_sync.edit_library_page("Court movement", "LOG - Movement + Defense", "a", "b")
    check("table page redirected to row tool", "append_library_log_row" in out)
    out = training_sync.edit_library_page("Good Drills", "Good Handling", "", "x")
    check("empty old_text refused", "old_text is empty" in out)
    out = training_sync.edit_library_page("Good Drills", "Good Handling", "same", "same")
    check("no-op refused", "identical" in out)


def test_append_library_log_row():
    print("\n=== append_library_log_row: table rows ===")
    _reset_with_library()
    out = training_sync.append_library_log_row(
        "Court movement", "LOG - Movement + Defense", ["8/30", "Flip and Go", "L2"])
    check("append reports row + headers", "Logged into" in out and "Date, Drill, Level" in out)
    page = _library_page("6", "LOG - Movement + Defense")
    check("first blank row filled, padded to columns",
          page["rows"][0] == ["8/30", "Flip and Go", "L2", ""])
    check("second blank row still blank", page["rows"][1] == ["", "", "", ""])
    training_sync.append_library_log_row(
        "Court movement", "LOG - Movement + Defense", ["8/31", "Chase", "L1", "slow"])
    training_sync.append_library_log_row(
        "Court movement", "LOG - Movement + Defense", ["9/1", "Closeout", "L3"])
    page = _library_page("6", "LOG - Movement + Defense")
    check("blanks exhausted then appends", len(page["rows"]) == 3
          and page["rows"][2][0] == "9/1")
    out = training_sync.append_library_log_row(
        "Court movement", "LOG - Movement + Defense", ["a", "b", "c", "d", "e"])
    check("too many cells refused with column names", "5 values for 4 columns" in out)
    out = training_sync.append_library_log_row("Good Drills", "Good Handling", ["x"])
    check("text page redirected to edit tool", "edit_library_page" in out)
    out = training_sync.append_library_log_row("Court movement", "LOG - Movement + Defense", [])
    check("empty values refused", "values is empty" in out)


def test_library_undo_and_size_cap():
    print("\n=== library writes: undo + snapshot size cap ===")
    _reset_with_library()
    training_sync.edit_library_page("Good Drills", "Good Handling",
                                    "CRAWL SPIN FINISH\nNOW: L2", "CRAWL SPIN FINISH\nNOW: L3")
    out = training_sync.undo_training_edit()
    check("undo names the library edit", "library edit" in out)
    body = _library_page("1", "Good Handling")["body"]
    check("undo restored the old level", "CRAWL SPIN FINISH\nNOW: L2" in body)

    real_cap = training_sync.MAX_SNAPSHOT_BYTES
    training_sync.MAX_SNAPSHOT_BYTES = 2_000
    try:
        out = training_sync.edit_library_page(
            "Good Drills", "Good Handling", "WALL TAPS", "W" * 5_000)
        check("over-cap write refused, names the limit", "sync limit" in out)
        body = _library_page("1", "Good Handling")["body"]
        check("page untouched after refusal", "WALL TAPS" in body and "W" * 100 not in body)
    finally:
        training_sync.MAX_SNAPSHOT_BYTES = real_cap


def test_library_tools_wired():
    print("\n=== library tools: schemas + dispatch wiring ===")
    check("both tools in TOOL_NAMES",
          "edit_library_page" in training_sync.TOOL_NAMES
          and "append_library_log_row" in training_sync.TOOL_NAMES)
    check("both tools have status labels",
          "edit_library_page" in training_sync.TOOL_STATUS_LABELS
          and "append_library_log_row" in training_sync.TOOL_STATUS_LABELS)
    _reset_with_library()
    out = training_sync.handle_tool_call(
        "edit_library_page",
        {"category": "Good Drills", "page": "Good Handling",
         "old_text": "WALL TAPS", "new_text": "WALL TAPS (both hands)"})
    check("dispatch reaches edit_library_page", "Edited 'Good Handling'" in out)
    out = training_sync.handle_tool_call(
        "append_library_log_row",
        {"category": "Court movement", "page": "LOG - Movement + Defense",
         "values": ["8/30", "Flip and Go", "L2"]})
    check("dispatch reaches append_library_log_row", "Logged into" in out)


# ============================================================
if __name__ == "__main__":
    test_batch_populates_multiple_blocks()
    test_batch_mixed_valid_and_invalid()
    test_batch_all_invalid_writes_nothing()
    test_batch_noop_when_already_matching()
    test_batch_empty_and_oversized()
    test_batch_clear_and_replace_reporting()
    test_batch_undo_reverts_whole_batch_in_one_step()
    test_batch_before_any_sync()
    test_once_layer_parses_and_overrides()
    test_once_layer_failsoft_on_junk()
    test_edit_schedule_this_week_only()
    test_set_big_obligation_lifecycle()
    test_edit_library_page()
    test_append_library_log_row()
    test_library_undo_and_size_cap()
    test_library_tools_wired()
    total, passed = len(_results), sum(_results)
    print("\n" + "=" * 48)
    print(f"{passed}/{total} checks passed")
    print("=" * 48)
    sys.exit(0 if passed == total else 1)
