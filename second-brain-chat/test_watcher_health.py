#!/usr/bin/env python3
"""Tests for scripts/watcher_health.py — the awake-age rule the daily backstop
reads to decide whether the capability watcher is alive.

Run directly:  python3 test_watcher_health.py
No network, no sysctl, no launchctl: assess() is pure, so every branch is driven
directly.

Tests for watcher_health.assess — self-contained, no network, no sysctl.

The whole point of the module is the sleep-subtraction rule, so these drive the
real-world cases that used to produce false alarms (overnight sleep) alongside the
ones that must still fire (awake and silent).
"""

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "scripts"))
import watcher_health as wh

NOW = datetime(2026, 8, 26, 13, 30, tzinfo=timezone.utc)
failures = []


def check(name, got, want):
    if got != want:
        failures.append(f"{name}: got {got!r}, wanted {want!r}")


def verdict(heartbeat=None, last_wake=None, streak=0, loaded=True, now=NOW):
    return wh.assess(now, heartbeat, last_wake, streak, loaded)[0]


def ago(**kw):
    return NOW - timedelta(**kw)


# --- the false alarm this module exists to kill -----------------------------
# Overnight sleep: heartbeat 9h old, but the Mac woke 1 minute ago. The old rule
# ("raw age > 30 min = DEAD") screamed here every single morning.
check("overnight sleep is healthy",
      verdict(heartbeat=ago(hours=9), last_wake=ago(minutes=1)), "HEALTHY")

# The 2026-08-26 incident exactly: heartbeat 74 min old because the Mac slept
# through those 74 minutes and woke moments ago.
check("battery maintenance sleep is healthy",
      verdict(heartbeat=ago(minutes=74), last_wake=ago(seconds=30)), "HEALTHY")
# Mirror image, same numbers, opposite meaning: the wake came FIRST and the Mac
# then sat awake and silent for 74 minutes. Sleep excuses nothing here.
check("awake 74 min with a 74-min-old heartbeat is dead",
      verdict(heartbeat=ago(minutes=74), last_wake=ago(minutes=75)), "DEAD")

# --- staleness that sleep does NOT excuse ----------------------------------
check("awake and silent is dead",
      verdict(heartbeat=ago(minutes=40), last_wake=ago(hours=3)), "DEAD")
check("awake since before heartbeat, long silence is dead",
      verdict(heartbeat=ago(minutes=20), last_wake=ago(days=2)), "DEAD")
check("just under the awake threshold is healthy",
      verdict(heartbeat=ago(minutes=14), last_wake=ago(hours=5)), "HEALTHY")
check("just over the awake threshold is dead",
      verdict(heartbeat=ago(minutes=16), last_wake=ago(hours=5)), "DEAD")

# --- the blind spot's floor -------------------------------------------------
# A wake one minute ago cannot excuse a heartbeat from days back.
check("multi-day staleness beats a fresh wake",
      verdict(heartbeat=ago(days=3), last_wake=ago(minutes=1)), "DEAD")

# --- infrastructure failures outrank everything ----------------------------
check("unloaded launchd job is dead",
      verdict(heartbeat=ago(seconds=30), last_wake=ago(hours=1), loaded=False), "DEAD")
check("missing heartbeat is dead",
      verdict(heartbeat=None, last_wake=ago(hours=1)), "DEAD")

# --- blind (running, but cannot read the queue) ----------------------------
check("failstreak at the alert threshold is blind",
      verdict(heartbeat=ago(seconds=30), last_wake=ago(hours=1),
              streak=wh.FAIL_STREAK_ALERT), "BLIND")
check("transient failstreak is still healthy",
      verdict(heartbeat=ago(seconds=30), last_wake=ago(hours=1), streak=3), "HEALTHY")
# A dead watcher outranks a blind one — it cannot fail reads if it never runs.
check("dead beats blind",
      verdict(heartbeat=ago(hours=2), last_wake=ago(hours=5),
              streak=wh.FAIL_STREAK_ALERT), "DEAD")

# --- no wake data: degrade to raw age, never to silence --------------------
check("no wake data falls back to raw age (fresh)",
      verdict(heartbeat=ago(minutes=2), last_wake=None), "HEALTHY")
check("no wake data falls back to raw age (stale)",
      verdict(heartbeat=ago(minutes=40), last_wake=None), "DEAD")

# --- sleep accounting reported to the reader -------------------------------
_, _, awake_age, slept = wh.assess(NOW, ago(hours=9), ago(minutes=1), 0, True)
check("awake age excludes the sleep", round(awake_age), 60)
check("slept seconds reported", round(slept), 9 * 3600 - 60)

# --- file parsing ----------------------------------------------------------
with tempfile.TemporaryDirectory() as tmp:
    hb = os.path.join(tmp, "hb")
    with open(hb, "w") as fh:
        fh.write("2026-08-26T13:28:20.627779+00:00")
    check("heartbeat parses", wh.read_heartbeat(hb),
          datetime(2026, 8, 26, 13, 28, 20, 627779, tzinfo=timezone.utc))

    naive = os.path.join(tmp, "naive")
    with open(naive, "w") as fh:
        fh.write("2026-08-26T13:28:20")
    check("naive heartbeat assumed UTC", wh.read_heartbeat(naive).tzinfo, timezone.utc)

    check("missing heartbeat reads None", wh.read_heartbeat(os.path.join(tmp, "nope")), None)

    junk = os.path.join(tmp, "junk")
    with open(junk, "w") as fh:
        fh.write("not a timestamp")
    check("unparseable heartbeat reads None", wh.read_heartbeat(junk), None)

    fs = os.path.join(tmp, "fs")
    with open(fs, "w") as fh:
        fh.write("12\n2026-08-26T10:00:00+00:00\n")
    check("failstreak parses", wh.read_failstreak(fs), (12, "2026-08-26T10:00:00+00:00"))
    check("absent failstreak is the healthy case",
          wh.read_failstreak(os.path.join(tmp, "nope")), (0, None))

if failures:
    print(f"FAILED ({len(failures)}):")
    for f in failures:
        print("  " + f)
    sys.exit(1)
print("test_watcher_health: all checks passed")
