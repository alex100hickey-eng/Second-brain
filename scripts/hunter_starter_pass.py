#!/usr/bin/env python3
"""The one command for when Alex buys Hunter Starter: verify every waiting founder address,
stalest ad accounts first, and put the deliverable ones on the tracker.

    scripts/hunter_starter_pass.sh --dry-run      # the order and the count, no credit spent
    scripts/hunter_starter_pass.sh                # spend it

Waiting = a Named Contacts row still `candidate` that Hunter hasn't already called risky. Stale
first because a founder whose ads are months old has the clearest reason to want new ones (the
discovery rows carry "stale a/b oldest Nd" from their live Ad Library read).

It never touches the reserve: it refuses with HUNTER_USE_RESERVE set, refuses below
--min-balance (Starter not active yet), and spends at most the balance minus HUNTER_RESERVE.
contact_finder refuses at the reserve on its own too; this just doesn't ask.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "second-brain-chat"))

_spec = importlib.util.spec_from_file_location("splitframe_queue", os.path.join(HERE, "splitframe_queue.py"))
sq = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sq)

STARTER_MIN_BALANCE = 100
_STALE = re.compile(r"stale (\d+)/(\d+)(?: oldest (\d+)d)?")


def staleness(row: dict) -> tuple:
    """Sort key, smallest first: most-stale share, then oldest ad, then no data at all."""
    m = _STALE.search(row.get("email_evidence") or "")
    if not m:
        return (1, 0.0, 0)
    share = int(m.group(1)) / max(1, int(m.group(2)))
    return (0, -share, -int(m.group(3) or 0))


def waiting(rows: list) -> list:
    out = [r for r in rows
           if (r.get("email_status") or "").strip().lower() == "candidate"
           and (r.get("named_email") or "").strip()
           and ": risky" not in (r.get("email_evidence") or "")]
    return sorted(out, key=staleness)


def guard(left, env: dict, min_balance: int) -> str:
    """Why the pass must not run, or ""."""
    if env.get("HUNTER_USE_RESERVE") == "1":
        return "HUNTER_USE_RESERVE is set: this pass never spends the reserve"
    if left is None:
        return "Hunter's balance can't be read"
    if left < min_balance:
        return f"{left} verifications left: Starter isn't active yet (needs {min_balance}+)"
    return ""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="spend at most this many (0 = the whole budget)")
    ap.add_argument("--min-balance", type=int, default=STARTER_MIN_BALANCE)
    args = ap.parse_args(argv)
    import contact_finder as cf                                    # type: ignore
    left = cf.verifications_left()
    rows = sq.read_named()
    queue = waiting(rows)
    budget = max(0, (left or 0) - cf.HUNTER_RESERVE)
    if args.limit:
        budget = min(budget, args.limit)
    print(f"{len(queue)} addresses waiting; balance {left}; this pass may spend {budget} "
          f"(keeps the last {cf.HUNTER_RESERVE}).")
    for r in queue[:budget or len(queue)]:
        m = _STALE.search(r.get("email_evidence") or "")
        print(f"  {r.get('brand', '')[:28]:28} {r['named_email']:38} {m.group(0) if m else 'stale ?'}")
    stop = guard(left, os.environ, args.min_balance)
    if args.dry_run:
        print("dry run: nothing verified." + (f" A real run would stop: {stop}" if stop else ""))
        return 0
    if stop:
        print(f"NOT run: {stop}")
        return 1

    def _verify(email):
        status, score = cf.verify(email)
        if status == cf.NOT_CHECKED:
            return "error", 0
        return {cf.SENDABLE: "deliverable", cf.UNDELIVERABLE: "undeliverable"}.get(status, "risky"), score

    changed = sq.verify_candidates(queue, _verify, limit=budget)
    sq.write_named(rows)
    good = [e for e, s in changed if s == "verified"]
    print(f"verified {len(good)}, rejected {sum(1 for _e, s in changed if s == 'rejected')}.")
    res = subprocess.run([sys.executable, os.path.join(HERE, "splitframe_queue.py"), "named", "--write"],
                         capture_output=True, text=True)
    print((res.stdout or res.stderr).strip().splitlines()[-1:] or [""])
    return 0


if __name__ == "__main__":
    sys.exit(main())
