# TOOL_AUDIT.md — tool usage, measured 2026-07-30

**Registered tools: 93. Ever invoked on the Mac: 12.** (Fable 5's review estimated ~70/16
from an older snapshot; the registry has grown since.)

## ⚠️ Read this first — what this data can and can't say

This audit reads the **Mac's** `observability.db` only. The server keeps its own audit
ledger **inside the container, which is wiped on every redeploy** — and the server is
where phone chats land. So a tool unused here may still earn its keep from your phone:
`check_intake`, `morning_briefing`, `run_managed_task` and friends were all exercised in
live server sessions per the build logs. **Do not prune from this list alone.**

Two fixes would make the next audit trustworthy end-to-end (both need your OK):
1. **Mirror tool-audit rows to Supabase** (like everything else) so both nodes' usage is
   visible from anywhere. ~100–300 small rows/day; pairs well with the retention sweep
   Fable suggested for the Agent Outputs table.
2. Until then, the server's ledger could be snapshotted over SSH before each deploy — but
   that's manual, which is the disease this week's work has been curing.

## What the Mac's numbers say

| calls | last used | tool | note |
|---|---|---|---|
| 164 | 07-30 | read_note | vault reading is the workhorse |
| 106 | 07-30 | search_notes | |
| 47 | 07-30 | list_recent_notes | |
| 22 | 07-28 | run_money_scouts | mostly self-exercising (see below) |
| 18 | 07-30 | run_scout | incl. today's debugging |
| 7 | 07-28 | develop_money_idea | |
| 4 | 07-26 | GMAIL_FETCH_EMAILS | |
| 4 | 07-30 | apply_finding | today's end-to-end test |
| 2–1 | 07-20 | activity_log, cost_report, system_health, search_everything | one-offs |

Fable's observation holds: after the vault tools, the top "usage" is **pipelines
reviewing their own output** (`expansion_review` 50×, `money_review` 21× in the audit
ledger). The daily-assistant loop (intake, tasks, briefings) barely registers *on this
node* — but that loop lives on the server, which is exactly the blind spot above.

## The 81 never-used-locally, grouped honestly

- **Probably server-used (don't touch without server data):** intake suite
  (check/accept/dismiss/capture/scan_*), morning_briefing, notifications suite,
  managed-task suite, task/goal suite, memory suite (remember/search/forget/distill).
- **New or gated this week (unused is expected):** screen_control_* (blocked on
  Accessibility), review_findings/adopt_tool (pipeline just fixed), log_friction.
- **Plausibly dead weight — my prune candidates, pending your call and server data:**
  - `get_word_count` (the demo extension tool; its review branch is still unmerged)
  - `edit_video`, `analyze_video`, `create_website`, `synthesize_data`, `draft_run`,
    `list_drafted_runs` (the pre-pivot "many features" era)
  - `propose_file_cleanup`, `scan_downloads`, `watch_screen` (superseded by managed
    tasks' file lane and screen control)
  - Redundant Composio granulars: `GMAIL_LIST_THREADS`, `GMAIL_GET_PROFILE`,
    `GOOGLECALENDAR_LIST_CALENDARS`, etc. — the intake layer wraps these better.
- **Keep regardless of usage (safety/ops):** check_budget, check_system_health,
  undo_file_operations, stop_managed_task, run_backup.

## Why this matters even before pruning

All 93 schemas ship in **every** chat request. Prompt caching (the 07-23 latency pass)
absorbs most of the token cost, but a 93-way choice still taxes the model's tool
selection on every turn. Fewer, sharper tools is a quality lever, not just a cost one.

## Recommended sequence

1. You OK the Supabase audit mirror → next session builds it (small).
2. Let it collect ~a week of real two-node data.
3. Re-run this audit; prune with evidence. The registry extraction out of app.py
   (5,500+ lines) rides along with that prune, per Fable.
