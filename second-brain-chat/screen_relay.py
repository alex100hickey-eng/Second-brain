#!/usr/bin/env python3
"""
screen_relay.py — run this ON THE MAC so the server-side CLARVIS can drive the
screen. See screen_bridge.py for the design and the safety properties.

It polls Supabase for screen commands, executes them through screen_control.py
(Escape kill-switch, 5-min session expiry, credential refusal all still apply),
and writes results back. It never opens a port — the Mac only ever dials out.

Run manually:      python3 screen_relay.py
Run on login:      see scripts/com.secondbrain.screenrelay.plist

Stop it any time with Ctrl-C, or just quit it — with the relay down, any command
the server sends simply expires unexecuted.
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(_HERE), ".env"))

import anthropic
from supabase import create_client

import screen_bridge


def main():
    url, key = os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_KEY")
    if not (url and key):
        print("screen_relay: SUPABASE_URL / SUPABASE_KEY missing from .env — can't start.")
        return 1
    sb = create_client(url, key)

    api_key = os.environ.get("CLAUDE_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
    claude = anthropic.Anthropic(api_key=api_key) if api_key else None
    if claude is None:
        print("screen_relay: no CLAUDE_API_KEY — screenshots will not be describable.")

    # Fail loudly here rather than at the first click: without Accessibility
    # permission the Escape kill-switch can't catch keys, and screen_control
    # will (correctly) refuse to arm.
    try:
        import Quartz
        if not Quartz.CGPreflightListenEventAccess():
            print("screen_relay: WARNING — macOS Accessibility/Input Monitoring is NOT "
                  "granted. The Escape kill-switch can't catch keys, so screen control "
                  "will refuse to arm. Grant it in System Settings > Privacy & Security.")
    except Exception:
        pass

    print("screen_relay: starting. Ctrl-C to stop.")
    try:
        screen_bridge.run_relay(sb, claude)
    except KeyboardInterrupt:
        print("\nscreen_relay: stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
