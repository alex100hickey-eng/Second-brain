#!/usr/bin/env python3
"""CLI for contact_finder.fill_contacts — the wiring the module never had.

contact_finder.py was written 2026-08-24 to solve "the tracker has no addresses,"
and then sat orphaned: never imported, never registered as a CLARVIS tool, never
covered by a test, and its docstring pointed at the wrong .env. So the module that
existed to unblock Wave 1 could not actually be invoked by anyone. This is the
missing entry point.

Runs on the LOCAL Mac only — contact_finder writes to the iCloud vault, and the
server's copy is a pull-only mirror where a write is silently reverted.

Usage:
    python3 scripts/fill_contacts.py --wave 1                # look up wave 1
    python3 scripts/fill_contacts.py --brands "Diggs,Wild One"
    python3 scripts/fill_contacts.py --wave 1 --limit 3      # respect the 25/mo quota
    python3 scripts/fill_contacts.py --status                # just show wave 1 state
"""
import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "second-brain-chat"))

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(ROOT, ".env"))
except Exception:
    pass

import contact_finder

# The module resolves the tracker from an injected vault path and returns
# "no prospect-tracker.csv on this node" without it — the wiring app.py would
# have done had it ever imported this module.
VAULT = os.environ.get("VAULT_DIR") or os.path.expanduser(
    "~/Library/Mobile Documents/com~apple~CloudDocs/Obsidian/Second brain")


def main():
    contact_finder.init(VAULT, runtime_fn=lambda: "local")
    ap = argparse.ArgumentParser()
    ap.add_argument("--wave", default="")
    ap.add_argument("--brands", default="")
    ap.add_argument("--limit", type=int, default=6)
    ap.add_argument("--status", action="store_true")
    a = ap.parse_args()

    if not contact_finder.api_key():
        print("No HUNTER_API_KEY found in second-brain/.env.\n"
              "Get one free at hunter.io (25 domain searches/month), then:\n"
              '  read -s -p "key: " K && echo "HUNTER_API_KEY=$K" >> ~/second-brain/.env')
        return 1

    if a.status:
        print(contact_finder.wave_text(a.wave or "1"))
        return 0

    brands = [b.strip() for b in a.brands.split(",") if b.strip()] or None
    print(contact_finder.fill_contacts(wave=a.wave, brands=brands, limit=a.limit))
    return 0


if __name__ == "__main__":
    sys.exit(main())
