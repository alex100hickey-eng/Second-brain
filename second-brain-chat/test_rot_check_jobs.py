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

sys.path.insert(0, os.path.expanduser("~/second-brain/scripts"))
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


def _loaded(monkeypatch, labels):
    monkeypatch.setattr(rc, "_loaded_labels", lambda: set(labels))


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
