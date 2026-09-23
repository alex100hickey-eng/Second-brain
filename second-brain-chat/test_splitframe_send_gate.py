"""Tests for the recipient whitelist in scripts/splitframe_send.py.

This gate is the last thing between a written draft and a stranger's inbox, and it fails CLOSED —
which is exactly why a hole in it is invisible. On 2026-09-19 it refused all ten of that day's
emails, including the first two creator-lane offers, and closed their outbox rows so they could
not retry. Nothing errored; a refusal reads like the safety feature working.

The cause: the gate read only the tracker's `email` column while the drafter had, two days
earlier, started drafting to FRONT DESK addresses in `email_generic`, and to a creator list that
lives in a vault doc and never touches the tracker at all.
"""
from __future__ import annotations

import importlib.util
import os

import pytest

# The checkout these tests live in, not ~/second-brain: a worktree must test its own copy.
SENDER = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "scripts", "splitframe_send.py")
spec = importlib.util.spec_from_file_location("splitframe_send", SENDER)
sfs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sfs)


HEADER = "brand,email,email_generic,status\n"


@pytest.fixture
def tracker(tmp_path, monkeypatch):
    def write(rows: str, creators: str = ""):
        t = tmp_path / "prospect-tracker.csv"
        t.write_text(HEADER + rows, encoding="utf-8")
        monkeypatch.setattr(sfs, "TRACKER", str(t))
        c = tmp_path / "creators.md"
        c.write_text(creators, encoding="utf-8")
        monkeypatch.setattr(sfs, "CREATOR_LIST", str(c))
        return sfs.approved_recipients()
    return write


def test_a_named_person_address_is_allowed(tracker):
    assert "amy@tower28beauty.com" in tracker("Tower 28,amy@tower28beauty.com,,qualified\n")


def test_a_front_desk_address_is_allowed(tracker):
    """The whole reason draftable went 0 -> 41 on 2026-09-17. If the gate cannot see these, every
    one of those 41 is written, released, approved and then thrown away."""
    got = tracker("Antler Farms,,info@antlerfarms.com,qualified\n")
    assert "info@antlerfarms.com" in got


def test_both_columns_are_allowed_on_one_row(tracker):
    got = tracker("Brand,founder@brand.com,hello@brand.com,qualified\n")
    assert {"founder@brand.com", "hello@brand.com"} <= got


def test_creator_lane_addresses_are_allowed(tracker):
    """Creator prospects are curated in a vault doc and never enter the tracker. Guzu and
    MISTERARTHER were both refused on 2026-09-19 for exactly this."""
    got = tracker("", creators="### Guzu\n- **Email:** `guzubusiness@hotmail.com` VERIFIED\n")
    assert "guzubusiness@hotmail.com" in got


def test_addresses_are_matched_case_insensitively(tracker):
    got = tracker("Brand,Founder@Brand.COM,,qualified\n")
    assert "founder@brand.com" in got


def test_blank_cells_do_not_become_an_empty_allowed_address(tracker):
    """An empty string in the set would let a draft with no parseable recipient through."""
    assert "" not in tracker("Brand,,,qualified\n")


def test_a_missing_tracker_denies_everything_rather_than_allowing_it(tmp_path, monkeypatch):
    """Fail closed: an unreadable list must never read as 'no restrictions'."""
    monkeypatch.setattr(sfs, "TRACKER", str(tmp_path / "gone.csv"))
    monkeypatch.setattr(sfs, "CREATOR_LIST", str(tmp_path / "gone.md"))
    assert sfs.approved_recipients() == set()


def test_an_address_on_no_list_is_still_refused(tracker):
    """The gate's whole purpose: a replayed or forged approval can only reach a chosen prospect."""
    got = tracker("Brand,founder@brand.com,,qualified\n")
    assert "stranger@example.com" not in got


# ---- the two numbers that had to agree ----

def test_the_sender_cap_follows_the_release_cadence():
    """The release cadence is bounce-aware and was raised to 10/day; this script's cap stayed
    hardcoded at 5. Ten drafts released each morning, five quietly held for a tomorrow that never
    came — and "daily cap reached" reads like correct behaviour in the log.

    Contract: the cap is read from the release cadence and can exceed the floor, never sit below it.
    """
    cap = sfs.daily_cap()
    assert isinstance(cap, int) and cap >= sfs.DAILY_CAP


def test_main_uses_the_live_cap_and_not_the_hardcoded_floor():
    """Pinned at the source level: the bug was main() restating the number instead of asking."""
    src = open(SENDER).read()
    body = src[src.index("def main("):]
    assert "daily_cap()" in body
    assert "DAILY_CAP - sent_today" not in body


def test_the_cap_fails_to_the_floor_not_to_unlimited(monkeypatch):
    """If the cadence source cannot be read, the safe answer is the conservative number."""
    monkeypatch.setattr(sfs, "DAILY_CAP", 5)
    import builtins
    real = builtins.__import__
    def boom(name, *a, **k):
        if name == "importlib.util":
            raise RuntimeError("no")
        return real(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", boom)
    assert sfs.daily_cap() == 5


def test_stamping_matches_a_front_desk_address(tmp_path, monkeypatch):
    """No sent_date means no +3d and no +7d, so the brand gets one email and is then invisible
    to the follow-up drafter. Replies come from follow-ups."""
    t = tmp_path / "t.csv"
    t.write_text("brand,email,email_generic,sent_date,followup1_date,followup2_date\n"
                 "Antler Farms,,info@antlerfarms.com,,,\n", encoding="utf-8")
    monkeypatch.setattr(sfs, "TRACKER", str(t))
    sfs.stamp_tracker("info@antlerfarms.com")
    row = t.read_text().splitlines()[1]
    assert row.count(",") == 5 and "2026" in row or row.split(",")[3]


def test_stamping_still_matches_a_named_person_address(tmp_path, monkeypatch):
    t = tmp_path / "t.csv"
    t.write_text("brand,email,email_generic,sent_date,followup1_date,followup2_date\n"
                 "Tower 28,amy@tower28beauty.com,,,,\n", encoding="utf-8")
    monkeypatch.setattr(sfs, "TRACKER", str(t))
    sfs.stamp_tracker("amy@tower28beauty.com")
    assert t.read_text().splitlines()[1].split(",")[3]


def test_stamping_never_overwrites_an_existing_send_date(tmp_path, monkeypatch):
    """Re-stamping would restart the follow-up clock and re-send a sequence already run."""
    t = tmp_path / "t.csv"
    t.write_text("brand,email,email_generic,sent_date,followup1_date,followup2_date\n"
                 "Brand,,hello@brand.com,2026-09-01,2026-09-04,2026-09-08\n", encoding="utf-8")
    monkeypatch.setattr(sfs, "TRACKER", str(t))
    sfs.stamp_tracker("hello@brand.com")
    assert "2026-09-01" in t.read_text()


# ---------------------------------------------------------------------------
# What a refusal DOES. The gate failing closed is correct; destroying the email
# when it fails is not. It used to close the outbox row DONE, so a written,
# released, approved first touch was thrown away and the row then read as if it
# had been sent. The whole 2026-09-19 batch hit this, and survived only because
# the whitelist was corrected within two minutes.
# ---------------------------------------------------------------------------

def test_a_refused_send_is_held_not_destroyed():
    """The gate exists because the whitelist might be wrong — and a wrong whitelist is exactly
    the case where the email is fine and the LIST needs fixing. Closing the row throws away the
    one thing that cannot be recreated cheaply."""
    import inspect
    src = inspect.getsource(sfs.main) if hasattr(sfs, "main") else open(
        os.path.expanduser("~/second-brain/scripts/splitframe_send.py")).read()
    block = src[src.index("if who not in allowed:"):]
    block = block[:block.index("continue")]
    assert "outbox.snooze" in block, "a refused row must be held for retry"
    assert "outbox.close" not in block, "a refused row must never be closed as done"
    assert "outbox.DONE" not in block


def test_the_hold_is_long_enough_not_to_nag_and_short_enough_to_recover():
    """Two minutes would nag; a week would silently park real emails past their moment."""
    assert 1 <= sfs.REFUSED_HOLD_HOURS <= 24


def test_a_snoozed_row_stays_open_and_comes_back():
    """Snooze is only the right primitive if the row survives it — open_items must skip it
    while snoozed and return it afterwards."""
    import importlib.util as ilu
    from datetime import datetime, timedelta
    ospec = ilu.spec_from_file_location(
        "outbox", os.path.expanduser("~/second-brain/second-brain-chat/outbox.py"))
    ob = ilu.module_from_spec(ospec)
    ospec.loader.exec_module(ob)
    future = (datetime.now(ob._now().tzinfo) + timedelta(hours=6)).isoformat()
    past = (datetime.now(ob._now().tzinfo) - timedelta(hours=1)).isoformat()
    rows = [{"id": 1, "status": ob.OPEN, "snooze_until": future},
            {"id": 2, "status": ob.OPEN, "snooze_until": past},
            {"id": 3, "status": ob.DONE, "snooze_until": ""}]
    for r in rows:
        r.update({"kind": "email_draft", "send_approved": "2026-09-19T12:00:00", "sent_at": ""})
    ob._rows = lambda limit=30: rows
    ids = [it["id"] for it in ob.awaiting_send()]
    assert 1 not in ids, "still snoozed — the sender runs every 2 min and must respect the hold"
    assert 2 in ids, "snooze expired, should come back"
    assert 3 not in ids, "closed rows never come back"


# ---- who wins the daily cap ----

def test_follow_ups_take_the_cap_before_cold_first_touches():
    """The queue reads newest-id-first and the daily operator drafts follow-ups BEFORE releasing
    first touches, so the cold emails were newer and won every slot. On 2026-09-22 that would have
    sent 10 first touches and starved 33 follow-ups — the emails that actually produce replies."""
    items = [{"id": 9, "detail": "Subject: Fifty three ads, one offer\n\nb"},
             {"id": 8, "detail": "Subject: Re: Your crate ads skip new rescues\n\nb"},
             {"id": 7, "detail": "Subject: Forty eight ads, all from June 12\n\nb"},
             {"id": 6, "detail": "Subject: RE: your ad library has a gap\n\nb"}]
    assert [i["id"] for i in sfs.follow_ups_first(items)] == [8, 6, 9, 7]


def test_ordering_is_stable_inside_each_group():
    """Newest-first is preserved within follow-ups and within first touches; only the two groups
    move relative to each other."""
    items = [{"id": 5, "detail": "Subject: Re: a\n\nb"},
             {"id": 4, "detail": "Subject: Re: b\n\nb"},
             {"id": 3, "detail": "Subject: cold\n\nb"}]
    assert [i["id"] for i in sfs.follow_ups_first(items)] == [5, 4, 3]


def test_a_reply_is_recognised_whatever_the_case():
    assert sfs.is_follow_up({"detail": "Subject: Re: thing\n\nb"})
    assert sfs.is_follow_up({"detail": "Subject: RE: thing\n\nb"})
    assert not sfs.is_follow_up({"detail": "Subject: Rethinking your ads\n\nb"})


def test_a_draft_with_no_detail_is_treated_as_a_first_touch():
    """Unknown must not jump the queue ahead of a real follow-up."""
    assert not sfs.is_follow_up({})
    assert not sfs.is_follow_up({"detail": ""})


# ---- pacing, quiet hours, watchdog (deliverability audit, 2026-09-23) ----

from datetime import datetime  # noqa: E402


def _t(h, m=0, day=23):
    return datetime(2026, 9, day, h, m)


def test_watchdog_budget_fits_inside_the_launchd_interval():
    """launchd fires every 120 s and never starts a second instance while one lives. On 2026-09-23
    one run blocked in an SSL read for eight hours and ALL sending stopped, with the process looking
    healthy. The budget has to expire before the next tick."""
    assert 0 < sfs.RUN_BUDGET_SECONDS < 120


def test_main_arms_the_watchdog_before_doing_any_work():
    src = open(SENDER).read()
    body = src[src.index("def main("):]
    first = [l.strip() for l in body.splitlines()[1:] if l.strip() and not l.strip().startswith(("#", '"'))][0]
    assert first == "arm_watchdog()", first


def test_the_process_exits_through_os_exit_so_no_atexit_hook_can_hang_it():
    src = open(SENDER).read()
    tail = src[src.index('if __name__ == "__main__":'):]
    assert "os._exit(" in tail and "sys.exit(main())" not in tail


def test_quiet_hours_cover_the_night_and_nothing_else():
    assert not sfs.in_quiet_hours(_t(21, 59))
    assert sfs.in_quiet_hours(_t(22, 0))
    assert sfs.in_quiet_hours(_t(0, 56))        # the 09-22 batch went out here
    assert sfs.in_quiet_hours(_t(7, 29))
    assert not sfs.in_quiet_hours(_t(7, 30))
    assert not sfs.in_quiet_hours(_t(12, 36))


def test_only_one_item_is_chosen_per_run():
    items = [{"id": 3, "detail": "Subject: Re: a\n\nb"}, {"id": 2, "detail": "Subject: cold\n\nb"},
             {"id": 1, "detail": "Subject: cold2\n\nb"}]
    assert [i["id"] for i in sfs.choose(items, {}, _t(10, 30))] == [3]


def test_the_gap_after_a_send_is_four_to_nine_minutes():
    st = sfs.after_send({}, _t(10, 0), rng=lambda: 0.0)
    assert st["not_before"] == _t(10, 4).isoformat()
    st = sfs.after_send({}, _t(10, 0), rng=lambda: 0.999)
    nb = datetime.fromisoformat(st["not_before"])
    assert _t(10, 8) <= nb < _t(10, 9)


def test_nothing_sends_while_the_gap_is_running():
    st = sfs.after_send({}, _t(10, 0), rng=lambda: 0.0)          # not before 10:04
    items = [{"id": 1, "detail": "Subject: cold\n\nb"}]
    assert sfs.choose(items, st, _t(10, 3)) == []
    assert [i["id"] for i in sfs.choose(items, st, _t(10, 4))] == [1]


def test_a_corrupt_pace_state_never_stops_sending_for_good():
    items = [{"id": 1}]
    assert sfs.may_send_now({"not_before": "garbage"}, _t(10)) is True
    assert sfs.choose(items, {"not_before": None}, _t(10)) == items


def test_quiet_hours_hold_automatic_sends_but_not_an_approved_one():
    auto = {"id": 1, "detail": "Subject: cold\n\nb"}
    tapped = {"id": 2, "detail": "Subject: cold\n\nb", "send_approved": "2026-09-22T23:10:00"}
    assert sfs.choose([auto], {}, _t(23, 15)) == []
    assert [i["id"] for i in sfs.choose([auto, tapped], {}, _t(23, 15))] == [2]
    assert [i["id"] for i in sfs.choose([auto, tapped], {}, _t(9, 15))] == [1]


def test_a_snoozed_row_is_skipped_rather_than_blocking_the_queue():
    """A HELD auto-send row (bad address) used to be re-refused and re-nudged every two minutes;
    with one send per run it would also have blocked every row behind it."""
    held = {"id": 1, "snooze_until": _t(16, 0).isoformat()}
    ok = {"id": 2}
    assert [i["id"] for i in sfs.choose([held, ok], {}, _t(10, 0))] == [2]
    assert [i["id"] for i in sfs.choose([held, ok], {}, _t(16, 1))] == [1]
    assert sfs.not_snoozed([{"id": 3, "snooze_until": "not a date"}], _t(10)) == [{"id": 3, "snooze_until": "not a date"}]


def test_main_chooses_one_and_records_the_gap():
    """Pinned at the source: main() must go through choose() and after_send(), or the loop is
    back to firing every expired hold in one minute."""
    src = open(SENDER).read()
    body = src[src.index("def main("):]
    assert "choose(pending, state, now)" in body
    assert "after_send(state" in body
    assert body.index("choose(pending") < body.index("for item in pending")
