# CLAUDE.md — working rules for this repo

Read this before changing anything. These are the rules that sessions keep
rediscovering the expensive way; `README.md` covers setup, `SECURITY_NOTES.md`
the hardening pass, `NEEDS_ALEX.md` what's blocked on Alex.

## Hard gates — never negotiate these away

- **No email send path, anywhere.** `mail_drafts.py` creates Gmail drafts and
  nothing else; Alex presses Send himself. CLARVIS reads untrusted email bodies,
  so a model holding a send tool is an exfiltration lane. An AST-walking test
  (`test_no_send_capability`) fails the suite if a send slug or `smtplib`
  appears outside a docstring. This stays even if Alex asks casually.
- **Never draft work Alex submits for a grade.** Every one of his Fall 2026
  courses bans AI on submitted work — ECON, MATH, ACCT and AIQS have explicit
  verbatim policies (AIQS bans it even for *ideas*, Grammarly included). Study
  guides, quizzing, planning, and explaining are fine.
- **Consequential actions route to the approval queue.** Money movement, account
  creation, external sends, and deletion go through `jarvis_pending_action` —
  structurally, because managed tasks only act via `handle_tool_call`.
- **Alex dictates his OWN schedule-block placement — but other people's
  commitments get written in as they surface.** Amended 2026-08-24 at his
  direction: "there's too many curveballs to follow a strict 24/7 schedule…
  things that pop up in my texts, like our team meeting at 4 tomorrow and our
  conditioning after, those need to be added in as they come." The line is WHO
  SET THE TIME:
  - **Someone else set it** (team meeting, conditioning, practice, appointment,
    anything with a time he didn't choose) → write it into the grid as it
    surfaces, `this_week_only=True` so a one-off can't calcify into the
    repeating grid, and tell him what landed with a way to undo it.
  - **He sets it** (gym, lifting, study, work, 50/50) → still never auto-placed,
    ever. He decides when those go, working around whatever got added. Suggesting
    in chat is fine; writing it is not.
  - **Classes never change.** They're permanent grid entries — don't rewrite them
    and don't let a pop-up overwrite one.
- **Google Calendar is permanently retired.** The training-app grid is the
  schedule. Don't re-add calendar tooling.

## Node rules

- Two instances share Supabase: the Mac (`local`) and the server (`server`).
- **Only the local node writes the vault.** The server's copy is a pull-only
  mirror, so a write there is silently reverted by the next sync. Gate vault
  writes on the runtime (see `august_tracker.reconcile_vault`,
  `school_data.log_study_review_tool`, `school_grades.log_grade_tool`).
- The two nodes have **different `ACCESS_CODE`s**; anything deriving a token
  from it differs per node.
- Any script born on the Mac and later called server-side must pin
  `America/New_York`. The server runs UTC, so a naive `datetime.now()` is
  already tomorrow every evening after 8 PM ET. This has caused real bugs in
  `school_status.py` and `intake.py`.

## Before you ship

1. `python3 run_tests.py` must be green. Every `second-brain-chat/test_*.py` is
   registered in `suite_modules`; add new suites there or they rot unrun.
2. New tools need a `TOOL_STATUS_LABELS` entry — the suite checks this — and a
   dispatch line in `app.py`.
3. Commit **and push**: Coolify deploys from `main`, so an unpushed commit is a
   feature that exists nowhere. Verify with `/api/version` rather than trusting
   the deploy UI; the queue jams, and the fix is Alex clicking Redeploy or
   `docker restart coolify` over SSH.
4. **Close the intake row when you ship a self-scheduled roadmap item.** CLARVIS
   files its own future work as `intake_event` rows with source `claude_code`.
   Shipping early without closing the row means it nudges Alex to build what
   already exists — which happened on 2026-08-22 with rows 13546/13547. Mark it
   `dismissed` with a `resolution` naming the commit.

## Traps that have bitten more than once

- **Stacked `@app.route` decorators** in the 6k-line `app.py` break silently when
  a function is inserted beneath them (`/school`, `/revenue`, `/schedule` all
  once served the August JSON blob). Pinned by tests now — keep them.
- **Tests must not hardcode dates.** Fixtures dated in the past have twice
  started failing when behavior legitimately changed around them. Use relative
  dates.
- **A hardcoded constant outlives the reality it described.** "4-6 PM window"
  and "Lights out 11:00" both survived a full schedule rebuild and had to be
  derived from the grid instead. When Alex re-dictates his life, grep literals.
- **Don't put a reasoning agent on a polling loop.** A free script watches and
  spawns the agent on a hit (see `scripts/capability_watcher.py`).
- **Don't make a capability depend on the model electing to use it.** Hang it
  off an event that already happens — that's why the person profile and the
  50/50 capture work and the old memory tool didn't.
- Never keep a live `.git` directory inside an iCloud folder.

## Added 2026-09-02 (systems audit follow-through)

- **`JARVIS_TEST=1` is test mode.** `run_tests.py` sets it before importing the
  app; app.py turns the tool-audit mirror, note capture → draft store, incident
  rows, draft rehydration and every background loop into no-ops. Before this the
  "offline" suite wrote ~60 rows per run into production Supabase (audit rows
  tagged as Alex's own use, fixture draft notes, login lockouts) and rehydrated
  130+ fixture notes into `vault_inbox/`. Keep new writers behind `TEST_MODE`.
- **The return channel is the notification shade, not chat.** Alex types into
  CLARVIS about once a day. Any capture that needs him to say something in chat
  will sit empty (scorecard, 50/50, mark_prepared, reviews, grades all did). Put
  capture on a signed `/do` page (`action_links` kinds `scorecard`, `pace`;
  forms post `op=log`) or derive it from data that already flows.
- **Phone decisions reach the vault through `school_state.py`.** The server
  records `school:prepared` / `school:done` state rows; the Mac applies them to
  the CSVs on canvas_sync's 30-minute tick (`school_state.apply_to_vault`).
  Never write the vault from the server.
- **Nightly `canvas-status-sync` scheduled task** (9:35 PM, Claude Code app)
  reads `/api/v1/courses/<id>/students/submissions?student_ids[]=self` through
  the logged-in Browser pane (no token — the CWRU rule stands) and runs
  `scripts/apply_canvas_status.py`, the only thing that flips assignments.csv
  rows to submitted/graded. It needs the pane's SSO session alive.
- **Pace floors.** `school_status.effective_prepared` assumes attendance (a
  lecture that passed is a lecture he sat in → prepared through today) and
  counts submitted readings/APQs/homework. PACE reads "+0d under target" between
  check-ins, never "-6d BEHIND" from decay alone.
- **Ranked day rules (daily_orders.compose).** Overdue > 3 days is backlog: it
  takes no slots and is summarised in one line. Due tomorrow ranks with graded
  work (ACCT APQs lock at class start). Each assignment appears once. Past-due
  open rows are "verify" lines for two days, then silent.
- **Nudge rules.** `due:` and `missed:` on one item are one concern. Canvas
  notification deadlines never go MISSED (unobservable). Mail from Alex's own
  addresses is never extracted. Items assignments.csv owns (course + due date)
  never nudge from intake. Calendar lines matching away/travel/flight/trip
  silence session kickoffs and the 50/50 capture for that date.
- **Local node self-restarts** when `git rev-parse HEAD` moves (launchd
  KeepAlive); set `JARVIS_AUTORESTART=0` to hold it. It also beats `mac-awake`
  so the monitor treats a sleeping Mac as one fact, not four incidents.
- **Budget** now sums both nodes' `usage:<YYYY-MM>:<node>` rollups; the local
  SQLite ledger is only a fallback.
- **Weekend Map is a machine-read file.** `school_data.weekend_plan()` pushes the
  current weekend's "**Clear the board…:**" line, the newest "**Cut …:**" bullets and
  the "**Get-ahead focus:**" bullets to the phone Saturday 9 AM — keep those labels.
- **Exam runways read `curriculum.csv` `readings`** ("… — suggested: #…"): put each
  section's problem list there and the runway names it.
- **`school_data._load` is case-insensitive-safe**: the vault's `Courses/` folder
  shadowed `courses` for weeks (macOS matches case-insensitively). Use isfile.
- **`scripts/rot_check.py`** is the decay detector; the Friday sweep runs it.
