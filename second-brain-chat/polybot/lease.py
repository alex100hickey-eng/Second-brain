"""One polybot loop at a time, across machines.

The loop lives on the Mac today and moves to the server when Alex says so. Two loops on one account
would double every paper signal and, once live, every order. The runbook's first line of defence is
ORDER (stop the Mac loop, then start the server's); this is the second: a lease in the shared state
store (`polybot:lease` via intake, the same Supabase rows the heartbeats use). A loop refuses to start
while another node's lease is fresh, and renews its own every minute while it runs.

Fail-open on a store error, on purpose: the Mac loop has run for weeks without this, and a Supabase
hiccup must not stop it. The server supervisor refuses to start on its own if the store is
unreachable (see polybot_supervisor.py), so the new node is the one that waits.
"""
from __future__ import annotations

import os
import socket
import time

LEASE_KEY = "polybot:lease"
LEASE_TTL_S = 10 * 60          # a holder silent this long has stopped
RENEW_EVERY_S = 60


def node_name() -> str:
    return os.environ.get("POLYBOT_NODE") or f"mac:{socket.gethostname()}"


def _store():
    import sys
    from . import config
    app_dir = os.path.dirname(config.ROOT)
    if app_dir not in sys.path:
        sys.path.insert(0, app_dir)
    import intake
    if intake.supabase is None:
        from supabase import create_client
        intake.supabase = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
    return intake


def holder(store=None) -> dict:
    """The current lease row ({} if none), or raises if the store is unreachable."""
    st = (store or _store())._load_state(LEASE_KEY)
    return {k: st.get(k) for k in ("node", "pid", "renewed_at")} if st.get("node") else {}


def conflict(me: str, row: dict, now: float | None = None) -> str | None:
    """Why `me` may not run, or None."""
    now = time.time() if now is None else now
    if not row or row.get("node") == me:
        return None
    age = now - float(row.get("renewed_at") or 0)
    if age < LEASE_TTL_S:
        return f"{row['node']} holds the polybot lease (renewed {age:.0f}s ago)"
    return None


def renew(me: str, store=None, now: float | None = None) -> None:
    s = store or _store()
    st = s._load_state(LEASE_KEY)
    st.update(key=LEASE_KEY, node=me, pid=os.getpid(), renewed_at=time.time() if now is None else now)
    s._save_state(st)


def release(me: str, store=None) -> None:
    s = store or _store()
    st = s._load_state(LEASE_KEY)
    if st.get("node") == me:
        st.update(key=LEASE_KEY, node=None, renewed_at=0)
        s._save_state(st)


def main(argv=None) -> int:
    """`python3 -m polybot.lease show | release [--node NAME]` — the server move's lease steps."""
    import argparse
    ap = argparse.ArgumentParser(prog="polybot.lease")
    ap.add_argument("cmd", choices=["show", "release"])
    ap.add_argument("--node", default=None, help="release: the node whose lease to drop (default: this one)")
    a = ap.parse_args(argv)
    if a.cmd == "show":
        row = holder()
        print(f"lease: {row.get('node')} (renewed {time.time() - float(row.get('renewed_at') or 0):.0f}s ago)"
              if row else "lease: free")
        return 0
    me = a.node or node_name()
    release(me)
    print(f"lease released for {me} (if it held it)")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
