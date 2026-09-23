"""check_jobs must actually catch a dead job.

Written because the failure it exists for produced no evidence at all: on 2026-09-19 the reply
watcher's plist had been renamed `.plist.disabled`, launchctl had no row for it, and the last line
in its log was the same "no prospect replies" a healthy run writes. Two days, 46 cold emails out,
nothing watching for answers.

A liveness check that has only ever printed ✓ is untested, so these drive it through both failure
shapes: the label missing from launchctl, and the label present but the log gone quiet.
"""
import os
import sys
import time
from datetime import datetime

import pytest

# This checkout's scripts/, not ~/second-brain: a worktree must test its own copy.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import rot_check as rc  # noqa: E402


@pytest.fixture(autouse=True)
def clean_report():
    rc.WARN.clear()
    rc.OK.clear()
    yield
    rc.WARN.clear()
    rc.OK.clear()


@pytest.fixture
def fake_job(tmp_path, monkeypatch):
    """One job named 'com.secondbrain.faux', logging to a file we control the mtime of."""
    logs = tmp_path / "scripts"
    logs.mkdir()
    monkeypatch.setattr(rc, "ROOT", str(tmp_path))
    monkeypatch.setattr(rc, "MONEY_JOBS", {"com.secondbrain.faux": ("faux.log", 2)})
    return logs / "faux.log"


def _loaded(monkeypatch, labels, live=None, age_s=None):
    monkeypatch.setattr(rc, "_loaded_labels", lambda: set(labels))
    monkeypatch.setattr(rc, "_live_pids", lambda: dict(live or {}))
    monkeypatch.setattr(rc, "_process_age_s", lambda pid: age_s)


def test_a_disabled_job_is_reported(fake_job, monkeypatch):
    # The exact 2026-09-19 shape: plist renamed .disabled, so launchctl has no row.
    fake_job.write_text("no prospect replies\n")          # a log that looks perfectly healthy
    _loaded(monkeypatch, {"com.secondbrain.somethingelse"})
    rc.check_jobs()
    assert any("NOT LOADED" in w for w in rc.WARN), rc.WARN


def test_a_healthy_log_does_not_rescue_a_missing_job(fake_job, monkeypatch):
    # The trap: the log is fresh AND reassuring, because the last run before it died was fine.
    fake_job.write_text("no prospect replies\n")
    os.utime(fake_job, None)
    _loaded(monkeypatch, set())
    rc.check_jobs()
    assert not rc.OK
    assert rc.WARN


def test_a_loaded_but_silent_job_is_reported(fake_job, monkeypatch):
    # Loaded, but erroring on every run, or running stale code that never writes.
    fake_job.write_text("stale\n")
    old = time.time() - 9 * 3600
    os.utime(fake_job, (old, old))
    _loaded(monkeypatch, {"com.secondbrain.faux"})
    rc.check_jobs()
    assert any("silent for" in w for w in rc.WARN), rc.WARN


def test_a_loaded_and_fresh_job_is_clean(fake_job, monkeypatch):
    fake_job.write_text("fine\n")
    os.utime(fake_job, None)
    _loaded(monkeypatch, {"com.secondbrain.faux"})
    rc.check_jobs()
    assert not rc.WARN, rc.WARN
    assert any("alive" in o for o in rc.OK)


def test_a_job_that_has_never_run_is_reported(fake_job, monkeypatch):
    _loaded(monkeypatch, {"com.secondbrain.faux"})   # log file never created
    rc.check_jobs()
    assert any("missing" in w for w in rc.WARN), rc.WARN


def test_launchctl_returning_nothing_is_itself_a_warning(fake_job, monkeypatch):
    # Fail loud. An empty label set must never read as "every job is fine".
    _loaded(monkeypatch, set())
    monkeypatch.setattr(rc, "MONEY_JOBS", {})
    rc.check_jobs()
    assert any("cannot tell" in w for w in rc.WARN), rc.WARN


def test_the_real_money_jobs_are_all_watched():
    # The reply watcher is the one that was dark; it must be in the list, not just fixed once.
    assert "com.secondbrain.replywatch" in rc.MONEY_JOBS
    assert "com.secondbrain.splitframesend" in rc.MONEY_JOBS


# ---- a job whose process never ends (2026-09-23) ----

def test_a_run_alive_longer_than_its_budget_is_reported_even_with_a_fresh_log(fake_job, monkeypatch):
    """The exact 2026-09-23 shape: sender loaded, log last written 01:56 (inside the 24 h silence
    budget, and the line reads like a healthy send), and the 01:56 process still alive at 10:10.
    Eight hours of nothing sent, and both older checks said fine."""
    fake_job.write_text("item 19799: SENT to guzubusiness@hotmail.com\n")
    os.utime(fake_job, None)
    monkeypatch.setattr(rc, "MONEY_JOBS", {"com.secondbrain.faux": ("faux.log", 24, 15)})
    _loaded(monkeypatch, {"com.secondbrain.faux"}, live={"com.secondbrain.faux": 4719}, age_s=8 * 3600 + 590)
    rc.check_jobs()
    assert any("HUNG" in w and "4719" in w for w in rc.WARN), rc.WARN
    assert not rc.OK


def test_a_run_inside_its_budget_is_not_a_hang(fake_job, monkeypatch):
    fake_job.write_text("fine\n")
    os.utime(fake_job, None)
    _loaded(monkeypatch, {"com.secondbrain.faux"}, live={"com.secondbrain.faux": 77}, age_s=40)
    rc.check_jobs()
    assert not rc.WARN, rc.WARN


def test_a_job_with_no_live_process_skips_the_age_check(fake_job, monkeypatch):
    fake_job.write_text("fine\n")
    os.utime(fake_job, None)
    _loaded(monkeypatch, {"com.secondbrain.faux"}, live={}, age_s=None)
    rc.check_jobs()
    assert not rc.WARN


def test_a_two_tuple_job_spec_still_works_with_the_default_budget(fake_job, monkeypatch):
    _loaded(monkeypatch, {"com.secondbrain.faux"}, live={"com.secondbrain.faux": 5},
            age_s=rc.DEFAULT_MAX_RUN_MIN * 60 + 1)
    fake_job.write_text("x\n")
    rc.check_jobs()
    assert any("HUNG" in w for w in rc.WARN)


def test_ps_elapsed_time_is_parsed_in_every_shape():
    assert rc.parse_etime("08:09:50") == 8 * 3600 + 9 * 60 + 50
    assert rc.parse_etime("05:30") == 330
    assert rc.parse_etime("1-02:03:04") == 86400 + 2 * 3600 + 3 * 60 + 4
    assert rc.parse_etime("   00:07\n") == 7
    assert rc.parse_etime("") is None
    assert rc.parse_etime("garbage") is None


def test_the_sender_and_watcher_budgets_are_minutes_not_a_day():
    """The sender finishes in seconds; a budget that would have let the 8 h hang pass is no budget."""
    assert rc.MONEY_JOBS["com.secondbrain.splitframesend"][2] <= 15
    assert rc.MONEY_JOBS["com.secondbrain.replywatch"][2] <= 15
