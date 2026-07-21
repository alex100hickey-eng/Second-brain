#!/usr/bin/env python3
"""Insert a monitor `system_event` row from OUTSIDE the running app.

Background jobs (e.g. the vault-sync launchd task) run in their own process and
can't call `monitor.report_event()` directly, so their failures used to be silent
— you only found out by reading a raw log. This tiny CLI writes the exact same
`system_event` row shape the monitor uses, so a background outage surfaces in
CLARVIS's incident log and the dashboard "Budget & Incidents" panel.

    python3 report_event.py <component> <level> <message> [detail]

level: info | warning | error | critical (anything else → info).
Env REPORT_EVENT_DRYRUN=1 prints the row instead of inserting it (for tests).
Fail-soft by design: a logging hiccup must never break the caller, so this always
exits 0 and never raises.
"""
import os
import sys
import json
from datetime import datetime
from zoneinfo import ZoneInfo


def main() -> int:
    if len(sys.argv) < 4:
        print("usage: report_event.py <component> <level> <message> [detail]")
        return 0
    component, level, message = sys.argv[1], sys.argv[2], sys.argv[3]
    detail = sys.argv[4] if len(sys.argv) > 4 else ""
    if level not in ("info", "warning", "error", "critical"):
        level = "info"

    row = {
        "agent_name": "system_event",
        "output_text": json.dumps({
            "component": component, "level": level,
            "message": str(message)[:500], "detail": str(detail)[:800],
            "ts": datetime.now(ZoneInfo("America/New_York")).isoformat(),
        }),
    }

    if os.environ.get("REPORT_EVENT_DRYRUN") == "1":
        print("[dry-run] would insert:", row["output_text"])
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
        sb.table("Agent Outputs").insert(row).execute()
    except Exception as e:
        # fail-soft: never propagate a reporting failure to the job being reported on
        print(f"[report_event] could not record event: {e}")
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
