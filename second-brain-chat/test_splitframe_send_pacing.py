"""Tests for how scripts/splitframe_send.py paces sends and survives a hang. No network.

Three failures from 2026-09-22/23 are pinned here:
  * a run hung in an SSL read for eight hours, and launchd started no new run behind it, so
    nothing sent all morning (watchdog);
  * drafts held over from the day before went at 00:56 in a burst and used the whole day's cap
    before that day's follow-ups existed (quiet hours, one email per run, room held back for
    follow-ups);
  * a follow-up left the building but its log line didn't, because the log was written after
    the bookkeeping that hung (record first).

Every outbound path main() has is faked: outbox, Composio, Supabase, nudge, the tracker stamp,
the heartbeat and the watchdog itself. A test that calls a real main() and misses one of those
is how run_tests once spawned real money workers.
"""
import ast
import importlib.util
import os
import plistlib
import socket
import sys
import types
from datetime import date, datetime, timedelta

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SENDER = os.path.join(ROOT, "scripts", "splitframe_send.py")
spec = importlib.util.spec_from_file_location("splitframe_send_pacing_under_test", SENDER)
sfs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sfs)

TZ = sfs.SEND_TZ
MORNING = datetime(2026, 9, 23, 10, 0, tzinfo=TZ)
NIGHT = datetime(2026, 9, 23, 1, 0, tzinfo=TZ)


def _draft(i, to, follow_up=True, due="2026-09-23T09:00:00", **kw):
    subject = "Re: your ad account" if follow_up else "your ad account"
    it = {"id": i, "kind": "email_draft", "status": "open", "title": f"Send the reply to {to}",
          "ref": f"gmail:studio:r{i}", "detail": f"Subject: {subject}\n\nbody",
          "auto_send_at": due}
    it.update(kw)
    return it


class FakeOutbox:
    DONE = "done"

    def __init__(self, items, close_raises=False):
        self.items = {it["id"]: dict(it) for it in items}
        self.closed, self.snoozed, self.armed = [], [], []
        self.close_raises = close_raises

    def init(self, _sb):
        pass

    def _open(self):
        return [it for it in sorted(self.items.values(), key=lambda x: -x["id"])
                if it.get("status") == "open"]

    def awaiting_send(self):
        return [it for it in self._open() if it.get("send_approved") and not it.get("sent_at")]

    def due_to_auto_send(self, now_iso):
        return [it for it in self._open()
                if not it.get("sent_at") and not it.get("send_approved")
                and it.get("auto_send_at") and it["auto_send_at"] <= now_iso]

    def open_items(self, limit=60, include_snoozed=True):
        return self._open()

    def _write(self, item_id, changes):
        self.items[item_id].update(changes)

    def close(self, item_id, status, note=""):
        if self.close_raises:
            raise RuntimeError("supabase went away")
        self.items[item_id]["status"] = status
        self.closed.append(item_id)

    def snooze(self, item_id, hours=3):
        self.snoozed.append(item_id)

    def arm_auto_send(self, item_id, when_iso):
        self.items[item_id]["auto_send_at"] = when_iso
        self.armed.append(item_id)


@pytest.fixture
def run(tmp_path, monkeypatch):
    """main() with every outbound path faked. Returns (outbox, sent draft ids, log text)."""
    calls = {"stamped": [], "nudged": [], "armed": []}

    def go(items, now=MORNING, allowed=None, fail_drafts=(), cap=10, sent_today=0,
           close_raises=False, followups_sent_today=0, ceiling=20, share=False, unreadable=False):
        box = FakeOutbox(items, close_raises=close_raises)
        sent = []

        class Tools:
            def execute(self, slug, user_id=None, dangerously_skip_version_check=None,
                        arguments=None):
                if slug == "GMAIL_GET_DRAFT":        # an interrupted send's draft is still there
                    return {"successful": True, "data": {"message": {"labelIds": ["DRAFT"]}}}
                assert slug == sfs.SEND_SLUG
                if arguments["draft_id"] in fail_drafts:
                    return {"successful": False, "error": "draft not found"}
                sent.append(arguments["draft_id"])
                return {"successful": True}

        composio = types.ModuleType("composio")
        composio.Composio = lambda api_key=None: types.SimpleNamespace(tools=Tools())
        supabase = types.ModuleType("supabase")
        supabase.create_client = lambda *a, **k: object()
        monkeypatch.setitem(sys.modules, "outbox", box)
        monkeypatch.setitem(sys.modules, "composio", composio)
        monkeypatch.setitem(sys.modules, "supabase", supabase)
        for k in ("SUPABASE_URL", "SUPABASE_KEY", "COMPOSIO_API_KEY", "STUDIO_GMAIL_ENTITY"):
            monkeypatch.setenv(k, "test")
        log = tmp_path / "send.log"
        today = date.today().isoformat()
        # first touches as unmarked lines (the pre-09-23 format), follow-ups marked
        log.write_text("".join(f"{today} 08:0{i} item {i}: SENT to old{i}@x.com (draft d)\n"
                               for i in range(sent_today))
                       + "".join(f"{today} 09:0{i} item 5{i}: SENT to fu{i}@x.com (draft d) — "
                                 f"automatically [follow-up]\n" for i in range(followups_sent_today)))
        monkeypatch.setattr(sfs, "LOG", str(log))
        monkeypatch.setattr(sfs, "PAUSE_FILE", str(tmp_path / "no-pause"))
        monkeypatch.setattr(sfs, "_now", lambda: now)
        monkeypatch.setattr(sfs, "daily_cap", lambda: cap)
        monkeypatch.setattr(sfs, "total_ceiling", lambda: ceiling)
        monkeypatch.setattr(sfs, "followups_share_cap", lambda: share)
        monkeypatch.setattr(sfs, "approved_recipients", (lambda: None) if unreadable else lambda: set(
            allowed if allowed is not None
            else [it["title"].split()[-1].lower() for it in items]))
        monkeypatch.setattr(sfs, "stamp_tracker", lambda who: calls["stamped"].append(who))
        monkeypatch.setattr(sfs, "nudge", lambda t, b: calls["nudged"].append(t))
        monkeypatch.setattr(sfs, "_beat", lambda note="": None)
        monkeypatch.setattr(sfs, "arm_watchdog", lambda s=None: calls["armed"].append(True))
        assert sfs.main() == 0
        return box, sent, log.read_text()

    go.calls = calls
    return go


# ---- quiet hours and one email per run ----

def test_quiet_hours_run_from_ten_at_night_to_eight_in_the_morning():
    at = lambda h, m=0: datetime(2026, 9, 23, h, m, tzinfo=TZ)
    assert sfs.in_quiet_hours(at(22)) and sfs.in_quiet_hours(at(1)) and sfs.in_quiet_hours(at(7, 59))
    assert not sfs.in_quiet_hours(at(8)) and not sfs.in_quiet_hours(at(21, 59))


def test_one_email_per_run_and_the_follow_up_goes_first(run):
    items = [_draft(9, "cold@a.com", follow_up=False), _draft(8, "fu1@b.com"),
             _draft(7, "fu2@c.com")]
    box, sent, log = run(items)
    assert sent == ["r7"], "a follow-up before the cold email, the oldest one first, and only one email this run"
    assert log.count(": SENT to ") == 1
    box2, sent2, _ = run([it for it in items if it["id"] != 8])
    assert sent2 == ["r7"]


def test_nothing_auto_sends_at_night_but_a_send_alex_tapped_still_goes(run):
    yesterday = "2026-09-22T12:00:00"                   # long past its hold: really due
    items = [_draft(8, "fu@b.com", due=yesterday),
             _draft(6, "tapped@d.com", send_approved="2026-09-23T00:50")]
    _box, sent, _log = run(items, now=NIGHT)
    assert sent == ["r6"]
    _box, sent, _log = run([_draft(8, "fu@b.com", due=yesterday)], now=NIGHT)
    assert sent == []
    _box, sent, _log = run([_draft(8, "fu@b.com", due=yesterday)], now=MORNING)
    assert sent == ["r8"], "and the same draft goes once sending hours open"


def test_a_first_touch_leaves_the_slots_the_waiting_follow_ups_need(run):
    """Two follow-ups are drafted but still inside their 3-hour hold. With 2 slots left under
    the day's ceiling, a cold email that's due now must not take one of them."""
    later = "2026-09-23T13:00:00"
    items = [_draft(9, "cold@a.com", follow_up=False),
             _draft(5, "held1@x.com", due=later), _draft(4, "held2@y.com", due=later)]
    _box, sent, _log = run(items, cap=10, sent_today=5, followups_sent_today=13)   # 18 of 20
    assert sent == []
    _box, sent, _log = run(items, cap=10, sent_today=5, followups_sent_today=12)   # 17 of 20
    assert sent == ["r9"], "with a third slot free, the first touch may have it"


# ---- decision A (2026-09-23): the cap counts first touches, follow-ups have their own budget ----

def test_follow_ups_still_go_after_the_first_touch_cap_is_spent(run):
    items = [_draft(9, "cold@a.com", follow_up=False), _draft(8, "fu@b.com")]
    _box, sent, log = run(items, cap=10, sent_today=10)
    assert sent == ["r8"]
    _box, sent, log = run([_draft(9, "cold@a.com", follow_up=False)], cap=10, sent_today=10)
    assert sent == [] and "first-touch cap reached" in log


def test_the_ceiling_stops_everything(run):
    items = [_draft(9, "cold@a.com", follow_up=False), _draft(8, "fu@b.com")]
    _box, sent, log = run(items, cap=10, sent_today=8, followups_sent_today=12)
    assert sent == [] and "daily ceiling reached (20/20" in log


def test_the_sent_line_carries_the_kind_and_old_lines_read_as_first_touches(run, tmp_path):
    _box, sent, log = run([_draft(8, "fu@b.com"), _draft(7, "cold@c.com", follow_up=False)],
                          sent_today=2)
    assert log.rstrip().endswith("[follow-up]")
    counts = sfs.sent_counts_today()
    assert counts == {"first": 2, "follow": 1, "total": 3}


def test_the_switch_restores_the_shared_cap_in_the_sender(run):
    _box, sent, log = run([_draft(8, "fu@b.com")], cap=10, sent_today=10, share=True)
    assert sent == [] and "ceiling reached" in log


def test_send_budget_arithmetic():
    c = {"first": 3, "follow": 9, "total": 12}
    assert sfs.send_budget(c, 10, 20, share=False) == (7, 8)
    assert sfs.send_budget(c, 10, 20, share=True) == (0, 0)
    assert sfs.send_budget({"first": 10, "follow": 0, "total": 10}, 10, 20, False) == (0, 10)
    assert sfs.send_budget({"first": 0, "follow": 20, "total": 20}, 10, 20, False) == (0, 0)


def test_pick_keeps_first_touches_inside_their_own_cap():
    due = [_draft(9, "cold@a.com", follow_up=False), _draft(8, "fu@b.com"),
           _draft(7, "cold2@c.com", follow_up=False)]
    # same due time, so the older first touch (7) comes before the newer one (9)
    assert [i["id"] for i in sfs.pick_auto_sends(due, 1, 5, 0, limit=5)] == [8, 7]
    assert [i["id"] for i in sfs.pick_auto_sends(due, 0, 5, 0, limit=5)] == [8]
    assert [i["id"] for i in sfs.pick_auto_sends(due, 2, 5, 3, limit=5)] == [8, 7]   # 5-1 > 3, 5-2 <= 3


def test_the_reserve_only_counts_follow_ups_that_are_written_and_unsent():
    open_items = [_draft(1, "a@x.com"), _draft(2, "b@x.com", sent_at="2026-09-23T09:00"),
                  _draft(3, "c@x.com", follow_up=False), _draft(4, "d@x.com"),
                  {"id": 5, "kind": "task", "detail": "Subject: Re: x"}]
    assert sfs.followups_waiting(open_items, exclude_ids={4}) == 1


def test_a_failing_draft_does_not_take_the_runs_only_slot(run):
    """The per-run limit counts sends that WENT. Otherwise one broken draft would be retried
    first every ten minutes and nothing behind it would ever go."""
    # the broken draft is the older row, so it is first in line
    items = [_draft(8, "fine@c.com"), _draft(7, "broken@b.com")]
    _box, sent, log = run(items, fail_drafts={"r7"})
    assert sent == ["r8"]
    assert "SEND FAILED to broken@b.com" in log


# ---- the record before the bookkeeping ----

def test_the_send_is_logged_even_when_the_bookkeeping_after_it_fails(run):
    box, sent, log = run([_draft(8, "fu@b.com")], close_raises=True)
    assert sent == ["r8"]
    assert "item 8: SENT to fu@b.com" in log
    assert "sent, but close failed" in log
    assert run.calls["stamped"] == ["fu@b.com"], "a failed close must not skip the tracker stamp"
    assert log.index("SENT to fu@b.com") < log.index("close failed")


def test_a_refused_auto_send_is_held_rather_than_retried_every_run(run):
    """A snooze alone doesn't hold an auto-send, because due_to_auto_send reads auto_send_at.
    Without moving the send, every run refused it again and nudged again."""
    box, sent, _log = run([_draft(8, "stranger@z.com")], allowed=set())
    assert sent == [] and box.snoozed == [8] and box.armed == [8]
    assert box.items[8]["auto_send_at"] > "2026-09-23T13", "the send moved past the hold"


# ---- the watchdog ----

def test_the_watchdog_budget_fits_inside_the_launchd_interval():
    with open(os.path.join(ROOT, "scripts", "com.secondbrain.splitframesend.plist"), "rb") as f:
        interval = plistlib.load(f)["StartInterval"]
    assert 0 < sfs.RUN_BUDGET_SECONDS < interval
    assert socket.getdefaulttimeout() == 60


def test_main_arms_the_watchdog_before_it_touches_the_network(run):
    run([])
    assert run.calls["armed"] == [True]
    tree = ast.parse(open(SENDER, encoding="utf-8").read())
    main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
    first_call = next(n for n in ast.walk(main) if isinstance(n, ast.Call))
    assert getattr(first_call.func, "id", "") == "arm_watchdog"


@pytest.mark.parametrize("phase,expect", [("sending", "mid-send"), ("close", "WAS sent")])
def test_the_watchdog_says_which_email_it_cut_off(tmp_path, monkeypatch, phase, expect):
    log = tmp_path / "send.log"
    monkeypatch.setattr(sfs, "LOG", str(log))
    monkeypatch.setattr(sfs, "_in_flight", (5, "a@b.com", phase))

    def fake_exit(code):
        raise SystemExit(code)
    monkeypatch.setattr(sfs.os, "_exit", fake_exit)
    with pytest.raises(SystemExit):
        sfs._watchdog(None, None)
    text = log.read_text()
    assert "ABORTED" in text and "item 5 to a@b.com" in text and expect in text


def test_the_logger_cannot_kill_the_run(monkeypatch):
    monkeypatch.setattr(sfs, "LOG", "/nonexistent-dir-for-tests/splitframe_send.log")
    sfs.log("this must not raise")


def test_an_unreadable_tracker_stands_the_run_down_without_holding_anything(run):
    """2026-09-24 10:57 and 11:07: an evicted tracker made every recipient read as unapproved, so
    thirteen written follow-ups were held, pushed 6 hours back, and Alex got a nudge for each.
    Unreadable is "don't know", not "no": send nothing, hold nothing, nudge nobody."""
    items = [_draft(8, "fu@b.com"), _draft(7, "fu2@c.com")]
    box, sent, log = run(items, unreadable=True)
    assert sent == [] and box.snoozed == [] and box.armed == []
    assert run.calls["nudged"] == []
    assert "nothing sent and nothing held" in log

# ---- a send interrupted mid-flight is settled by the next run ----

class _Gmail:
    def __init__(self, draft_there=True, in_sent=False, boom=False):
        self.draft_there, self.in_sent, self.boom = draft_there, in_sent, boom
        self.tools = self

    def execute(self, slug, user_id=None, dangerously_skip_version_check=None, arguments=None):
        if self.boom:
            raise ConnectionError("Connection error.")
        if slug == "GMAIL_GET_DRAFT":
            if self.draft_there:
                return {"successful": True, "data": {"message": {"labelIds": ["DRAFT"]}}}
            return {"successful": False, "error": "Requested entity was not found."}
        if slug == "GMAIL_FETCH_EMAILS":
            return {"successful": True, "data": {"messages": [{"id": "m1"}] if self.in_sent else []}}
        raise AssertionError(slug)


def _interrupted(minutes_ago, i=8):
    now = datetime(2026, 9, 24, 18, 40)
    return now, _draft(i, "hello@finalbosssour.com",
                       sent_at=(now - timedelta(minutes=minutes_ago)).isoformat())


def test_only_a_stale_sent_at_on_an_open_row_counts_as_interrupted():
    now, old = _interrupted(76)
    _n, fresh = _interrupted(3, i=9)
    rows = [old, fresh, _draft(10, "x@y.com")]
    assert [r["id"] for r in sfs.interrupted_rows(rows, now)] == [8]


def test_a_draft_still_in_drafts_goes_back_in_line(tmp_path, monkeypatch):
    monkeypatch.setattr(sfs, "LOG", str(tmp_path / "send.log"))
    now, row = _interrupted(76)
    box = FakeOutbox([row])
    assert sfs.recover_interrupted(box, _Gmail(draft_there=True), "studio", now) == [(8, "requeued")]
    assert box.items[8]["sent_at"] == "" and box.items[8]["status"] == "open"
    assert "back in line" in (tmp_path / "send.log").read_text()


def test_a_draft_that_left_and_is_in_sent_is_closed_and_logged_once(tmp_path, monkeypatch):
    log = tmp_path / "send.log"
    monkeypatch.setattr(sfs, "LOG", str(log))
    now, row = _interrupted(76)
    box = FakeOutbox([row])
    assert sfs.recover_interrupted(box, _Gmail(draft_there=False, in_sent=True), "studio", now) == [(8, "closed")]
    assert box.items[8]["status"] == "done" and log.read_text().count("item 8: SENT to") == 1
    # a row whose SENT line was already written (killed during its bookkeeping) is not logged twice
    now, row = _interrupted(76)
    box = FakeOutbox([row])
    sfs.recover_interrupted(box, _Gmail(draft_there=False, in_sent=True), "studio", now)
    assert log.read_text().count("item 8: SENT to") == 1


def test_unclear_or_erroring_checks_leave_the_row_alone(tmp_path, monkeypatch):
    monkeypatch.setattr(sfs, "LOG", str(tmp_path / "send.log"))
    for gmail, what in ((_Gmail(draft_there=False, in_sent=False), "unclear"), (_Gmail(boom=True), "error")):
        now, row = _interrupted(76)
        box = FakeOutbox([row])
        assert sfs.recover_interrupted(box, gmail, "studio", now) == [(8, what)]
        assert box.items[8]["sent_at"] and box.items[8]["status"] == "open", "never a guess that could double-send"


def test_the_next_run_requeues_and_sends_the_interrupted_email(run):
    """2026-09-24: Final Boss's follow-up sat marked sent for 67 minutes with its draft unsent."""
    stale = (MORNING - timedelta(minutes=76)).replace(tzinfo=None).isoformat()
    items = [_draft(8, "hello@finalbosssour.com", sent_at=stale)]
    box, sent, log = run(items)
    assert sent == ["r8"] and "back in line" in log


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q", "-p", "no:cacheprovider"]))
