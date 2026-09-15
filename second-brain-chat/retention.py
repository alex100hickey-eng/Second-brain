"""
retention.py — bounded growth for the shared "Agent Outputs" table.

The single-table junk drawer is fine at one-user scale (Fable 5's review said as
much), but only if it stays bounded: high-churn operational rows — nudge logs,
screen-relay traffic, task steps, the new tool-audit mirror — accumulate forever
otherwise, and every reader that does `.order(id, desc).limit(n)` slowly loses
reach into the tags it actually needs.

Design rules, in order of importance:
1. WHITELIST ONLY. A tag is swept only if it appears in RETENTION_DAYS with an
   explicit TTL. Anything not listed — memories, chat history, expansion
   findings, drafts, intake, pending actions — is untouchable by construction,
   not by remembering to exclude it.
2. Conservative TTLs. These rows are diagnostics; nothing here is the source of
   truth for any feature. When in doubt, keep it longer.
3. Never raise. A retention failure must not take down the worker that hosts it.
"""

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

_TZ = ZoneInfo("America/New_York")

# tag -> days to keep. THE whitelist: absence means "never swept".
RETENTION_DAYS = {
    "jarvis_nudge": 30,          # send/skip log; status_text shows only the last few
    "screen_command": 7,         # Mac<->server screen-control relay traffic
    "screen_result": 7,
    "jarvis_taskman_step": 30,   # managed-task step audit; task rows themselves are kept
    "jarvis_taskman_kill": 14,   # kill-switch flags — meaningless once acted on
    # 14, not 90. At ~205 rows/day this tag was 9,455 of the table's 12,220 rows —
    # 77% of everything — and the 90-day TTL meant not one row had EVER been swept
    # since the first on 2026-07-31. Nothing reads a tool audit older than a couple
    # of weeks; the monthly cost rollup lives in intake_state, not here.
    "jarvis_tool_audit": 14,
    "system_event": 60,          # monitor incident log; scans read the last 6h
    # Added 2026-09-15: test fixtures were landing in the live table (132 of them)
    # and nothing swept them, so they grew forever. Fixtures are exhaust by
    # definition — unlike expansion_finding, jarvis_chat and the rest of the
    # protected set in run_tests.py, which are CONTENT and must stay unbounded no
    # matter how untriaged they look.
    "jarvis_draft_note_test": 7,
}

BATCH = 200                      # delete in slices; never one giant call

supabase = None


def init(supabase_client):
    global supabase
    supabase = supabase_client


def sweep(dry_run: bool = False) -> dict:
    """One retention pass. Returns {tag: rows_deleted} (or rows_that_would_be,
    when dry_run). Safe to call repeatedly; never raises."""
    out = {}
    if supabase is None:
        return out
    for tag, days in RETENTION_DAYS.items():
        cutoff = (datetime.now(_TZ) - timedelta(days=days)).isoformat()
        deleted = 0
        try:
            while True:
                rows = (supabase.table("Agent Outputs").select("id")
                        .eq("agent_name", tag).lt("created_at", cutoff)
                        .order("id", desc=False).limit(BATCH).execute().data or [])
                if not rows:
                    break
                if dry_run:
                    deleted += len(rows)
                    if len(rows) < BATCH:
                        break
                    # dry-run can't page past rows it didn't delete; estimate stops here
                    deleted = f"{deleted}+"
                    break
                ids = [r["id"] for r in rows]
                supabase.table("Agent Outputs").delete().in_("id", ids).execute()
                deleted += len(ids)
        except Exception as e:
            out[tag] = f"error: {str(e)[:120]}"
            continue
        if deleted:
            out[tag] = deleted
    return out


def sweep_summary(result: dict) -> str:
    if not result:
        return "Retention sweep: nothing to remove."
    parts = [f"{tag}: {n}" for tag, n in sorted(result.items(), key=lambda kv: str(kv[1]), reverse=True)]
    return "Retention sweep removed old rows — " + ", ".join(parts)
