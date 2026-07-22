# POLISH_PROMPT.md — the weekly polish ritual (reusable overnight prompt)

Paste the block below into a fresh Claude Code session (or `jarvis-launch.sh` draft)
whenever CLARVIS deserves a polish night — weekly is the rhythm. It turns FRICTION.md
+ real usage data into fixes. This is the "always improving, in every manner" engine.

---

You are polishing CLARVIS (~/second-brain), Alex's personal AI assistant. This is a
POLISH run, not a feature run — no new capabilities. Read the latest handoff doc first.

1. Read FRICTION.md — every unchecked item is a real complaint from Alex, in his words.
2. Read the real-usage evidence: the tool audit log (`observability.db` via
   `activity_log`/`cost_report`), `scripts/app.log`, and the monitor's incident log
   (`system_event` rows). Find what's actually slow, actually erroring, actually unused.
3. Rank: friction items first (Alex said them), then evidence-backed issues. Pick the
   top 3–5 you can fix WELL tonight; ignore the rest.
4. Fix them one at a time: smallest correct change, test after each, commit each with
   a clear message. UI fixes must match the existing aesthetic (HUD stays sci-fi; home
   stays clean). Never weaken a safety gate, never add a write-path without the
   approval queue, secrets stay in env.
5. Check off fixed items in FRICTION.md (`- [x] … (fixed YYYY-MM-DD, <commit>)`).
6. End: full `run_tests.py` green + standalone suites green, BUILD_LOG.md entry,
   updated handoff if state changed, everything committed. Push only if the run's
   instructions say to. Leave a short report: what was fixed, what was deliberately
   skipped, what you'd fix next week.

Hard rules: vault content untouchable; iMessage DB read-only; no sends of any kind;
deletions only for items Alex explicitly named; SECURITY_NOTES.md is the safety
authority. If a fix can't be finished cleanly, revert it and note it — a polish run
never leaves things half-wired.
