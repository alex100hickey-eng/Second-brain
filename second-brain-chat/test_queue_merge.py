"""Two writers on the first-touch queue must not erase each other.

Every writer (splitframe_queue commands, offer_statics swap-first, the daily release) loads the
whole queue, spends seconds on Gmail or the outbox, then saves the whole queue. On 2026-09-25
07:38 the operator's re-address of four brands saved over a Loudcup re-address made moments
before: the queue went back to the old front-desk entry while its Gmail draft kept the new
text and recipient. No network here: the shared state is a dict.
"""
import copy
import importlib.util
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # this checkout, not ~/second-brain


def _load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, "scripts", f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sq = _load("splitframe_queue")
sfd = sq._sfd


def _e(n, **kw):
    e = {"brand": f"B{n}", "to": f"desk{n}@b{n}.com", "draft_id": f"r{n}", "subject": "s", "body": "b"}
    e.update(kw)
    return e


class _State:
    """The intake module's shared state: one dict per key, copied in and out like the real
    Supabase round trip, so a writer never holds a live reference to another writer's list."""
    def __init__(self, queue):
        self.rows = {sq.QUEUE_KEY: {"key": sq.QUEUE_KEY, "queue": copy.deepcopy(queue)}}

    def _load_state(self, key):
        return copy.deepcopy(self.rows.get(key))

    def _save_state(self, st):
        self.rows[st["key"]] = copy.deepcopy(st)

    def queue(self):
        return self.rows[sq.QUEUE_KEY]["queue"]


def test_merge_applies_only_this_writers_changes():
    base = [_e(1), _e(2), _e(3)]
    mine = [_e(1, to="founder1@b1.com"), _e(2), _e(4)]          # changed 1, removed 3, added 4
    latest = [_e(1), _e(2, body="someone else's rewrite"), _e(3), _e(5)]
    out = sfd.merge_queue(base, mine, latest)
    by = {e["draft_id"]: e for e in out}
    assert by["r1"]["to"] == "founder1@b1.com"                  # mine
    assert by["r2"]["body"] == "someone else's rewrite"         # theirs, untouched by me
    assert "r3" not in by                                       # I removed it
    assert "r4" in by and "r5" in by                            # both additions survive
    assert [e["draft_id"] for e in out] == ["r1", "r2", "r5", "r4"]


def test_an_entry_someone_else_removed_is_not_brought_back_by_an_edit():
    out = sfd.merge_queue([_e(1)], [_e(1, body="mine")], [])
    assert out == []


def test_entries_without_a_draft_id_key_on_recipient_brand_and_queued_at():
    a = {"brand": "A", "to": "a@x.com", "queued_at": "t1"}
    b = {"brand": "B", "to": "b@x.com", "queued_at": "t2"}
    out = sfd.merge_queue([a, b], [dict(a, body="new"), b], [a, b, {"brand": "C", "to": "c@x.com"}])
    assert [e.get("body") for e in out] == ["new", None, None]


def test_two_overlapping_revises_both_survive(monkeypatch):
    """The 09-25 race, replayed: A loads, B loads, B saves, A saves."""
    state = _State([_e(1), _e(2)])
    monkeypatch.setattr(sq, "_intake", lambda: state)
    qa, queue_a = sq.load_queue()
    qb, queue_b = sq.load_queue()
    queue_b[1]["to"] = "founder2@b2.com"                        # the operator's re-address
    sq.save_queue(qb, queue_b)
    queue_a[0]["to"] = "founder1@b1.com"                        # my re-address, saved second
    sq.save_queue(qa, queue_a)
    assert [e["to"] for e in state.queue()] == ["founder1@b1.com", "founder2@b2.com"]


def test_a_second_save_from_the_same_load_still_merges(monkeypatch):
    state = _State([_e(1), _e(2)])
    monkeypatch.setattr(sq, "_intake", lambda: state)
    q, queue = sq.load_queue()
    queue[0]["body"] = "first"
    sq.save_queue(q, queue)
    other_q, other = sq.load_queue()
    other[1]["body"] = "other writer"
    sq.save_queue(other_q, other)
    queue[0]["body"] = "second"
    sq.save_queue(q, queue)
    assert [e["body"] for e in state.queue()] == ["second", "other writer"]


def test_a_save_that_cannot_re_read_falls_back_to_the_whole_queue(monkeypatch):
    state = _State([_e(1)])
    monkeypatch.setattr(sq, "_intake", lambda: state)
    q, queue = sq.load_queue()
    queue[0]["body"] = "mine"

    def boom(key):
        raise RuntimeError("supabase down")
    monkeypatch.setattr(state, "_load_state", boom)
    sq.save_queue(q, queue)
    assert state.rows[sq.QUEUE_KEY]["queue"][0]["body"] == "mine"


class _Outbox:
    """An outbox whose add() is where another writer gets in: it edits the queue mid-release,
    as a revise or the operator would while the release is talking to Supabase."""
    def __init__(self, during_add):
        self.during_add = during_add
        self.n = 0

    def open_items(self, limit=60):
        return []

    def add(self, kind, title, **kw):
        self.during_add()
        self.n += 1
        return self.n

    def arm_auto_send(self, item_id, when_iso):
        return None


def test_an_edit_made_during_the_release_survives_it(monkeypatch):
    state = _State([_e(1), _e(2), _e(3)])
    monkeypatch.setattr(sfd, "_shared", state)
    monkeypatch.setattr(sfd, "log", lambda *a, **k: None)
    monkeypatch.setattr(sfd, "tracker_rows", lambda: [])
    monkeypatch.setattr(sfd, "front_desk_hold", lambda entry, by: "")
    monkeypatch.setattr(sfd, "_queued_age_days", lambda entry: 0.0)

    def someone_revises():
        st = state._load_state(sq.QUEUE_KEY)
        st["queue"][2]["body"] = "revised mid-release"
        state._save_state(st)
    out = sfd.release_first_touches(_Outbox(someone_revises), "https://mail", limit=2)
    assert out == ["B1", "B2"]
    queue = state.queue()
    assert [bool(e.get("released")) for e in queue] == [True, True, False]
    assert queue[2]["body"] == "revised mid-release"
