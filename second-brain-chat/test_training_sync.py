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
if __name__ == "__main__":
    test_batch_populates_multiple_blocks()
    test_batch_mixed_valid_and_invalid()
    test_batch_all_invalid_writes_nothing()
    test_batch_noop_when_already_matching()
    test_batch_empty_and_oversized()
    test_batch_clear_and_replace_reporting()
    test_batch_undo_reverts_whole_batch_in_one_step()
    test_batch_before_any_sync()
    total, passed = len(_results), sum(_results)
    print("\n" + "=" * 48)
    print(f"{passed}/{total} checks passed")
    print("=" * 48)
    sys.exit(0 if passed == total else 1)
