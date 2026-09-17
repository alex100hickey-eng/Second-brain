"""The disk check's slope awareness.

Bands answer "how full is it" and cannot answer "how fast is it filling". check_disk.py's own
opening line is that the slope was visible for hours before both outages and nothing was watching
it. On 2026-09-17 the server fell from 17.4 GB free to 6.4 GB in about two hours — thirteen
deploys in one working session — and every reading was inside NOTICE, which is silent by design.
"""
import importlib.util
import os

PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "scripts", "check_disk.py")
_spec = importlib.util.spec_from_file_location("check_disk", PATH)
cd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cd)

HOUR = 3600.0
NOW = 1_800_000_000.0


def test_the_real_slope_would_have_been_caught():
    """17.4 GB -> 6.4 GB over two hours: 5.5 GB/h, so 6.4 GB left is about 70 minutes."""
    eta = cd.hours_to_full({"at": NOW - 2 * HOUR, "free_gb": 17.4}, 6.4, NOW)
    assert eta is not None and 1.0 < eta < 1.4
    assert eta <= cd.HOURS_TO_FULL_WARNING


def test_a_healthy_box_reports_no_eta():
    # Flat: no fall at all.
    assert cd.hours_to_full({"at": NOW - HOUR, "free_gb": 20.0}, 20.0, NOW) is None
    # Rising, because something was pruned.
    assert cd.hours_to_full({"at": NOW - HOUR, "free_gb": 6.0}, 18.0, NOW) is None
    # A drift too small to be anything but noise.
    assert cd.hours_to_full({"at": NOW - HOUR, "free_gb": 20.2}, 20.0, NOW) is None


def test_a_slow_fall_is_not_an_alarm():
    """1 GB/h with 30 GB left is nine days of warning, not an incident."""
    eta = cd.hours_to_full({"at": NOW - HOUR, "free_gb": 31.0}, 30.0, NOW)
    assert eta is not None and eta > cd.HOURS_TO_FULL_WARNING


def test_a_stale_or_impossible_reading_is_ignored():
    """An old reading describes a different day. Guessing from it is worse than the band alone."""
    assert cd.hours_to_full({"at": NOW - 48 * HOUR, "free_gb": 30.0}, 6.0, NOW) is None
    assert cd.hours_to_full({"at": NOW + HOUR, "free_gb": 30.0}, 6.0, NOW) is None
    assert cd.hours_to_full({}, 6.0, NOW) is None
    assert cd.hours_to_full(None, 6.0, NOW) is None


def test_a_corrupt_trend_file_never_raises():
    """This runs on a schedule to prevent an outage; it may not become one."""
    for bad in ({"at": "nonsense", "free_gb": 10}, {"free_gb": 10}, {"at": NOW}, {"at": None}):
        assert cd.hours_to_full(bad, 6.0, NOW) is None


def test_bands_still_work_on_their_own():
    assert cd.band(95, (75, 85, 92)) == "critical"
    assert cd.band(86, (75, 85, 92)) == "warning"
    assert cd.band(81, (75, 85, 92)) == "notice"
    assert cd.band(40, (75, 85, 92)) == "ok"
