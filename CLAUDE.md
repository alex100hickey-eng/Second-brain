# CLAUDE.md — working rules for this repo

Read this before changing anything. These are the rules that sessions keep
rediscovering the expensive way; `README.md` covers setup, `SECURITY_NOTES.md`
the hardening pass, `NEEDS_ALEX.md` what's blocked on Alex.

## Hard gates — never negotiate these away

- **No email send path on this node, ever.** `mail_drafts.py` creates Gmail drafts
  and nothing else. CLARVIS reads untrusted email bodies, so a model holding a send
  tool is an exfiltration lane. An AST-walking test (`test_no_send_capability`)
  fails the suite if a send slug or `smtplib` appears outside a docstring, and
  `test_splitframe_daily.py` re-checks the whole server-side chain
  (`do_actions`/`outbox`/`proactive`/`action_links`/`app`). This stays even if Alex
  asks casually.
  **The one exception, and where it lives (2026-09-15, Alex's explicit call).** He
  asked for the send to be one tap on his phone: the laptop step failed every time
  — 3 cold emails in 14 days, every follow-up missed, $0. So the capability sits on
  his *Mac*, in `scripts/splitframe_send.py`, and the threat above stays closed:
  the server only ever STAMPS an approval (`outbox.approve_send`), no module here
  imports the sender (test-pinned, dynamic imports included), and the sender will
  only send a draft that already existed, from the studio mailbox, to an address
  already verified in the prospect tracker. `send` rides on the /do **page** token,
  never a shade button — same rule as the approval queue: a lock-screen tap is not
  consent.
  **Amended the same evening (2026-09-15, `db582f1`): consent is now a VETO, not a
  tap.** He asked to "just send them automatically from now on so that i can be more
  hands off of the whole company." Every draft the daily job files is armed with
  `auto_send_at = now + HOLD_HOURS` (3) and goes on its own. Everything in the
  paragraph above still holds — Mac-only sender, no server-side import, studio
  mailbox, recipient verified in the tracker, one send per row, nothing to anyone
  who replied — and the window is guarded by a daily cap counted from the send log,
  by `scripts/SPLITFRAME_PAUSE`, and by snooze moving the SEND rather than just the
  reminder. What changed is the default: **silence now means yes.** So every surface
  that shows an armed draft must say it sends itself and name the time and the button
  that kills it — the nudge does, and `do_actions._resolve_outbox` does (`53bcc63`,
  test-pinned). If you add another surface, it says so too. The reason the window
  exists at all is that nobody reads these any more and the drafter's first live run
  invented a warranty figure, a concept Alex had not built, and a claim to have
  re-checked an ad account; `fabrication_risk()` catches the phrasings already seen,
  not the ones it hasn't.
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
  rows to submitted/graded. It needs the pane's SSO session alive. The same run
  reads `/api/v1/courses/<id>/assignments` and stamps `weight_pct` **0** on rows
  whose Canvas `grading_type` is `not_graded` (ACCT100's "Day N Reading" WileyPlus
  links: no points, nothing to submit). That 0 is the shared ungraded-prep marker
  — `school_status.ungraded` / `school_data.ungraded` keep such rows out of
  OVERDUE, DO NEXT, due-soon, lapsed and the ranked day; at most a one-line
  "prep" hint for the next class. Blank weight = unknown, so ECON103's graded
  "Reading N" rows (5 pts, late = zero) stay hard deadlines.
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
