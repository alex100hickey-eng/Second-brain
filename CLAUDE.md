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
- **Alex dictates schedule-block placement.** He says when he trains, studies, or
  works; code transcribes. Suggesting in chat is fine; writing an uninvited block
  into the training grid is not.
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
