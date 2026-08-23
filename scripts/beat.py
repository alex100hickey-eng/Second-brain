#!/usr/bin/env python3
"""Record a monitor heartbeat from OUTSIDE the running app.

`monitor.beat()` needs the app's injected Supabase client, so launchd jobs that
run in their own process (canvas sync, vault sync) had no way to say "I ran."
That's the silent-failure shape that matters most: a sync which stops firing
looks exactly like a sync with nothing to do. `monitor.check_heartbeats()`
already reads every `heartbeat:*` row with no registration step, so a job only
has to write one for CLARVIS to start watching it.

    python3 beat.py <name> <stale_after_seconds> [note]

Writes/updates the same `intake_state` row shape `monitor.beat()` produces:
    {"key": "heartbeat:<name>", "stale_after_s": int, "note": str, "beat_at": iso}

Fail-soft by design — a heartbeat write must never break the job reporting it,
so this always exits 0.
"""
import json
import os
import sys
from datetime import datetime, timezone

STATE_AGENT = "intake_state"        # must match intake.STATE_AGENT


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: beat.py <name> <stale_after_seconds> [note]")
        return 0
    name = sys.argv[1]
    try:
        stale_after_s = int(sys.argv[2])
    except ValueError:
        print("stale_after_seconds must be an integer")
        return 0
    note = sys.argv[3] if len(sys.argv) > 3 else ""
    key = f"heartbeat:{name}"
    state = {"key": key, "stale_after_s": stale_after_s, "note": note[:120],
             "beat_at": datetime.now(timezone.utc).isoformat()}

    if os.environ.get("BEAT_DRYRUN") == "1":
        print("[dry-run] would write:", json.dumps(state))
        return 0

    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
    except Exception:
        pass
    try:
        from supabase import create_client
        sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
        # Update in place when the row exists, so heartbeats don't accumulate a
        # row per run — the same read-modify-write intake._save_state does.
        existing = (sb.table("Agent Outputs").select("id")
                    .eq("agent_name", STATE_AGENT)
                    .ilike("output_text", f'%"key": "{key}"%')
                    .order("id", desc=True).limit(1).execute().data or [])
        payload = {"agent_name": STATE_AGENT, "output_text": json.dumps(state)}
        if existing:
            sb.table("Agent Outputs").update(
                {"output_text": payload["output_text"]}).eq("id", existing[0]["id"]).execute()
        else:
            sb.table("Agent Outputs").insert(payload).execute()
    except Exception as e:
        print(f"[beat] could not record heartbeat: {e}")
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
