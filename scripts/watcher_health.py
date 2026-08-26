#!/usr/bin/env python3
"""
watcher_health.py — one-command verdict on whether the capability watcher is alive.

WHY THIS EXISTS
    The daily backstop used to judge the watcher by raw heartbeat age: "older than
    30 minutes = DEAD". On a laptop that rule is wrong most mornings. launchd
    StartInterval jobs do not fire while the Mac sleeps, and macOS coalesces the
    whole backlog into a single run on wake rather than replaying every missed
    2-minute tick. So an overnight sleep leaves a heartbeat hours old with nothing
    whatsoever wrong — and on 2026-08-26 that produced exactly that false alarm: a
    74-minute "outage" that was a battery Maintenance Sleep.

    A false alarm every morning is worse than no alarm, because it trains the
    reader to skip the one signal that separates a dead watcher from a quiet queue.

THE FIX — measure AWAKE age, not wall-clock age
    The watcher rewrites the heartbeat on EVERY run, before it does anything else
    (capability_watcher.beat() is the first line of main()), including runs where
    the queue read fails. So a heartbeat that has not moved while the Mac was awake
    is a real defect; a heartbeat that has not moved while the Mac was asleep is
    just physics.

        awake_age = now - max(heartbeat, last_wake)

    kern.waketime is the last wake from sleep, and it is free to read (~5ms) —
    unlike `pmset -g log`, which takes minutes on a loaded machine and is far too
    slow to sit in a health check.

KNOWN BLIND SPOT
    max(heartbeat, last_wake) measures only the MOST RECENT awake window. If the
    Mac woke, sat awake 20 minutes with a dead watcher, slept, and woke again one
    minute ago, awake_age reads 1 minute and this reports healthy. That is a
    deliberate trade: the watcher fires on wake anyway, so a genuinely dead one
    stays dead and the next backstop run catches it. MAX_RAW_AGE_S is the floor
    under that blind spot — nothing legitimately goes a day and a half without one
    awake window long enough to fire.

Usage:  python3 scripts/watcher_health.py [--quiet]
Exit:   0 = healthy, 1 = needs attention (DEAD / BLIND)
"""

import os
import re
import subprocess
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Read the SAME files the watcher writes — a second set of path constants here
# would silently drift the day one of them moves.
from capability_watcher import (  # noqa: E402
    FAILSTREAK,
    FAIL_STREAK_ALERT,
    HEARTBEAT,
)

LABEL = "com.secondbrain.capabilitywatcher"
POLL_INTERVAL_S = 120           # must match StartInterval in the plist
STALE_AWAKE_S = 15 * 60         # ~7 missed polls while awake — past any noise
MAX_RAW_AGE_S = 36 * 3600       # floor under the multi-sleep-cycle blind spot


def _fmt_age(seconds: float) -> str:
    seconds = int(max(seconds, 0))
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 90 * 60:
        return f"{seconds // 60}m"
    hours, mins = divmod(seconds // 60, 60)
    return f"{hours}h{mins:02d}m"


def read_heartbeat(path: str = HEARTBEAT):
    try:
        with open(path) as fh:
            raw = fh.read().strip()
    except OSError:
        return None
    try:
        stamp = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def read_failstreak(path: str = FAILSTREAK):
    """(count, since) — absence is the healthy case, so (0, None) is normal."""
    try:
        with open(path) as fh:
            lines = fh.read().splitlines()
    except OSError:
        return 0, None
    try:
        count = int((lines[0] if lines else "0").strip() or 0)
    except ValueError:
        return 0, None
    return count, (lines[1].strip() if len(lines) > 1 else None)


def read_last_wake():
    """kern.waketime = last wake from sleep, or boot time on a Mac that never
    slept. Costs ~5ms, which is why this exists instead of parsing `pmset -g log`."""
    try:
        out = subprocess.run(
            ["sysctl", "-n", "kern.waketime"],
            capture_output=True, text=True, timeout=10,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"sec\s*=\s*(\d+)", out)
    if not m:
        return None
    return datetime.fromtimestamp(int(m.group(1)), tz=timezone.utc)


def launchd_loaded(label: str = LABEL) -> bool:
    try:
        return subprocess.run(
            ["launchctl", "list", label],
            capture_output=True, text=True, timeout=10,
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def assess(now, heartbeat, last_wake, streak, loaded):
    """Pure verdict logic — no I/O, so the tests can drive every branch.

    Returns (verdict, headline, awake_age_s or None, slept_s or None)."""
    if not loaded:
        return "DEAD", f"launchd job {LABEL} is NOT loaded — the watcher cannot run", None, None
    if heartbeat is None:
        return "DEAD", "no readable heartbeat file — the watcher has never run, or cannot write", None, None

    raw_age = (now - heartbeat).total_seconds()

    # Sleep only excuses staleness that happened DURING the sleep. Time awake
    # since the later of (heartbeat, last wake) is time the watcher owed a poll.
    if last_wake is None:
        awake_age, slept = raw_age, None      # no wake data: fall back to raw age
    else:
        awake_age = (now - max(heartbeat, last_wake)).total_seconds()
        slept = max(raw_age - awake_age, 0)

    if raw_age > MAX_RAW_AGE_S:
        return ("DEAD",
                f"heartbeat is {_fmt_age(raw_age)} old — past {_fmt_age(MAX_RAW_AGE_S)} "
                f"no amount of sleep explains it", awake_age, slept)
    if awake_age > STALE_AWAKE_S:
        return ("DEAD",
                f"{_fmt_age(awake_age)} AWAKE with no heartbeat "
                f"(~{int(awake_age // POLL_INTERVAL_S)} missed polls)", awake_age, slept)
    if streak >= FAIL_STREAK_ALERT:
        return ("BLIND",
                f"{streak} consecutive failed queue reads (~{streak * 2} min blind) — "
                f"the watcher is RUNNING but cannot see the queue", awake_age, slept)
    return "HEALTHY", f"last poll {_fmt_age(awake_age)} of awake-time ago", awake_age, slept


def main() -> int:
    quiet = "--quiet" in sys.argv
    now = datetime.now(timezone.utc)
    heartbeat = read_heartbeat()
    last_wake = read_last_wake()
    streak, since = read_failstreak()
    loaded = launchd_loaded()

    verdict, headline, awake_age, slept = assess(now, heartbeat, last_wake, streak, loaded)

    print(f"VERDICT: {verdict} — {headline}")
    if not quiet:
        if heartbeat:
            raw_age = (now - heartbeat).total_seconds()
            print(f"  heartbeat   {heartbeat.isoformat(timespec='seconds')}  "
                  f"(wall-clock age {_fmt_age(raw_age)})")
        else:
            print("  heartbeat   MISSING")
        if last_wake:
            print(f"  last wake   {last_wake.isoformat(timespec='seconds')}")
        if awake_age is not None:
            note = f"  ({_fmt_age(slept)} of the gap was sleep)" if slept else ""
            print(f"  awake age   {_fmt_age(awake_age)}{note}   <- the number that matters")
        print(f"  failstreak  {streak or 'none'}" + (f" since {since}" if since else ""))
        print(f"  launchd     {'loaded' if loaded else 'NOT LOADED'}")

    return 0 if verdict == "HEALTHY" else 1


if __name__ == "__main__":
    sys.exit(main())
