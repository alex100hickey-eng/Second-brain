#!/usr/bin/env python3
"""Refresh the D1 transfer tracker's cache from outside the app.

The tab also refreshes itself lazily when someone opens it, but that isn't
enough on its own: the point of this tracker is that it's *current when Alex
goes looking*, particularly in-season when a guard's role can change over a
weekend. A page that only updates when viewed shows him stale numbers exactly
once — the first time he checks after something moved.

Cadence is set by the launchd agent (hourly), but the module's own TTL decides
whether a given school actually gets refetched: 6h in-season, 24h in the
offseason. So this runs cheaply most of the time and only does real work when
something is due.

Writes a monitor heartbeat via scripts/beat.py so a refresh that silently stops
firing is visible — the failure mode that matters most, since a frozen tracker
looks exactly like a tracker with no news.

    python3 d1_refresh.py [--force]
"""

import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CHAT = os.path.join(ROOT, "second-brain-chat")
sys.path.insert(0, CHAT)

# Stale after ~3 missed hourly runs; the tracker is worth watching but a single
# blip isn't worth waking anyone over.
HEARTBEAT_STALE_S = 3 * 3600


def beat(note: str) -> None:
    """Report 'I ran' to the monitor. Never fails the refresh."""
    try:
        subprocess.run([sys.executable, os.path.join(HERE, "beat.py"),
                        "d1_refresh", str(HEARTBEAT_STALE_S), note],
                       timeout=30, capture_output=True)
    except Exception as e:
        print(f"[d1_refresh] heartbeat failed (non-fatal): {e}")


def main() -> int:
    import d1_tracker

    force = "--force" in sys.argv
    try:
        result = d1_tracker.refresh_all(only_stale=not force)
    except Exception as e:
        print(f"[d1_refresh] refresh failed: {e}")
        beat(f"failed: {str(e)[:80]}")
        return 1

    n_ok, n_fail = len(result["refreshed"]), len(result["failed"])
    print(json.dumps(result, indent=2))

    # A run where every school failed is an outage, not a quiet night — say so
    # in the heartbeat note rather than reporting a clean run.
    if n_fail and not n_ok:
        beat(f"all {n_fail} schools failed")
        return 1
    beat(f"{n_ok} refreshed, {len(result['skipped'])} fresh, {n_fail} failed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
