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

spec = importlib.util.spec_from_file_location(
    "splitframe_send", os.path.expanduser("~/second-brain/scripts/splitframe_send.py"))
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
    src = open(os.path.expanduser("~/second-brain/scripts/splitframe_send.py")).read()
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
