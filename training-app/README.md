# Training app (Weekly Schedule PWA)

Alex's training app — the weekly 30-minute-block schedule grid plus workout
cards, daily routines, everyday warmup, and workout library. **The live copy is
the Netlify deploy at https://luminous-madeleine-bf89fa.netlify.app/** — this
folder is the source of truth in git (recovered 2026-08-16 from the live site;
edited here since). It was built in an earlier session and previously existed
nowhere in git.

App version 6 (2026-08-19) added the weekly reset:

- Column headers carry the current week's dates (computed client-side, Sunday
  start, 3 AM day boundary) and highlight today.
- Two grid layers: the repeating week (`weeklySchedule.v1`, unchanged shape)
  plus a one-week overlay (`weeklyOnce.v1` = `{weekStart, cells}`) that renders
  amber, covers the repeating cell it sits on, and is wiped client-side when
  the week rolls over. The "Edits:" header toggle picks which layer edits land
  in; cells already in the once layer always edit the once layer.
- "Calendar" (`bigObligations.v1` = `[{date, text}]`, v7 renamed from "Big
  Stuff Coming Up"): every day of the next 3 weeks renders as its own box (a
  rolling window computed from today — empty days are dashed placeholder
  slots, only days with text are stored), then an "after that" zone showing
  only dated big-big stuff plus "+ Add a date". Duplicate dates merge on
  render. Past dates purge on load.
- Server counterpart: `second-brain-chat/training_schedule.py` parses both new
  keys (once layer merges into events via `grid_for_week`; obligations feed
  today's context and the schedule tools), and `training_sync.py` gained
  `edit_schedule(this_week_only=…)` + `set_big_obligation`.

Single self-contained HTML file, no build step. All data lives in
localStorage; the built-in Sync feature mirrors everything to any URL speaking
Firebase's REST shape (`PUT`/`GET <base>/trainingDashboard.json`) — which is
now the CLARVIS server's `/training-sync/<token>` endpoint (see
`second-brain-chat/training_sync.py`). Ask CLARVIS in chat for the sync URL
(`get_training_sync_url`).

If you edit this file, redeploy to Netlify AND keep this copy in sync.
