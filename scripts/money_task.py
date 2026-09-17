#!/usr/bin/env python3
"""money_task.py — the Mac-side CLI for the money operator's queue.

The worker (a headless `claude -p` spawned by the capability watcher) reports through this:

    python3 scripts/money_task.py done    --slug sf_topup-20260916-2010 --note "3 queued" --facts '{"drafts_queued": 3}'
    python3 scripts/money_task.py blocked --slug ... --note "Vyro login wall" --facts '{"vyro_logged_in": false, "asks": ["Log in to Vyro once ..."]}'
    python3 scripts/money_task.py failed  --slug ... --note "what was tried"

Humans use the same entry point:

    python3 scripts/money_task.py status        # what the server operator is doing
    python3 scripts/money_task.py pending       # tasks waiting for hands
    python3 scripts/money_task.py tick --dry-run  # what the ladder would file right now

It loads ~/second-brain/.env itself (launchd hands children a bare environment).
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHAT = os.path.join(ROOT, "second-brain-chat")
sys.path.insert(0, CHAT)
try:
    from dotenv import load_dotenv        # type: ignore
    load_dotenv(os.path.join(ROOT, ".env"))
except Exception:
    pass


def main() -> int:
    from supabase import create_client    # type: ignore
    import intake                         # type: ignore
    import money_operator as mo           # type: ignore
    url, key = os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_KEY")
    if not (url and key):
        print("SUPABASE_URL / SUPABASE_KEY missing from ~/second-brain/.env")
        return 2
    sb = create_client(url, key)
    intake.supabase = sb
    mo.init(sb, intake)
    return mo.main(sys.argv[1:])


if __name__ == "__main__":
    sys.exit(main())
