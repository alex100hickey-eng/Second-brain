# Pomodoro Focus Timer + Session Logging

## SYSTEM DIRECTIVE

You are operating in full autonomy mode. I am away and not monitoring this session. These rules override default behavior:

1. **Never ask for permission.** Decide everything yourself; keep moving.
2. **Work the priorities IN ORDER; never leave one half-wired to start the next.** Finished early priorities beat many partials. This is a long list — running out of runway is expected and fine.
3. **If blocked, pivot within 5 minutes.** No accounts, keys, or paid services — build around gaps, mock realistically, mark limitations. Open-source packages and open model downloads are fine.
4. **Log progress** per phase in BUILD_LOG.md (timestamped: completed, decisions, pivots, issues).
5. **Test everything; extend `run_tests.py`** to cover each new feature you build, and keep the full suite passing.
6. **One final message only**: per-priority status, testing summary, limitations, my action list.

## HARD SAFETY RULES

- Work ONLY inside this project directory. Obsidian vault stays strictly READ-ONLY.
- No deleting files — obsolete goes to `_archive/`.
- No signups, purchases, or credentials beyond `.env`. No Supabase schema changes or data deletion.
- No deployment, no remote systems, nothing exposed beyond 127.0.0.1. All security invariants stay intact (secrets in `.env`, localhost-only, access code).
- **Screen-watch is WATCH-ONLY.** Do not install or write any code that controls the mouse, keyboard, or UI (no pyautogui-style control). Capture and analyze only.
- **The run drafter DRAFTS ONLY.** It must never launch Claude Code, execute a drafted plan, schedule anything, or run any agent autonomously. Output is text for my review, full stop.
- **Privacy:** the conversation-memory database and any captured screenshots must be gitignored and stay local. Screenshots are processed then deleted by default — never silently archived.
- macOS permission dialogs (Screen Recording, Microphone) can only be granted by me. If a permission is missing, do NOT fight it: build the feature fully, test against saved sample inputs, and put the one-time grant steps at the top of my action list.

## PROJECT CONTEXT

second-brain: Flask chat app (localhost:5001, access-code gated) with Claude tools (vault search/read, Supabase lookup, note logging, video input via ffmpeg+whisper.cpp, synthesize_data, create_website, edit_video, conversation memory, watch_screen, draft_run, goals), a decision council (pros, cons, feasibility judge), a dashboard, a Task Manager (statuses: idea → evaluating → approved → in progress → done/dropped, SQLite/JSON local storage), standalone agents, and a test suite (`run_tests.py`). READ FIRST: BUILD_LOG.md, SECURITY_NOTES.md, and the code. Match existing patterns; don't invent parallel architecture. whisper.cpp and its English base model are already installed — reuse them.

## PRIORITIES — COMPLETE IN THIS ORDER

### Priority 1: Session data model & logging endpoint (storage foundation)
Before touching UI, resolve the council's flagged weak link: whether the existing storage layer (whatever the vault indexer / chat history uses — check for flat files, JSON, or SQLite in the current data directory) is generic enough to extend, or whether it's single-purpose. Do this investigation first and record the finding in BUILD_LOG.
- Inspect existing persistence code (chat history, vault index cache, any `.env`/config-adjacent data files) to identify the established storage convention (file location, format, naming, write permissions).
- Add a `focus_sessions` store using that same convention (e.g., same JSON-lines/SQLite file directory, same access patterns) — do not invent a parallel storage format or a new ad-hoc file type if an existing generic pattern exists. If the layer truly is rigid/single-purpose, document why and choose the closest-fitting extension, not a bolted-on side file with no relation to the rest.
- Schema: `id`, `started_at` (ISO-8601, UTC stored, converted to Alex's local timezone for display), `duration_seconds` (int, computed from wall-clock `end - start`, never from tick-counting), `type` (`focus`/`break`), `label` (optional, freeform string, max length enforced, HTML-escaped/sanitized on write and on render — no injection via task-tag strings).
- Backend endpoint(s) (Flask route(s), same auth-gated pattern as the rest of the dashboard — must sit behind the existing `ACCESS_CODE` login gate, no new unauthenticated routes) to: create/complete a session record, list sessions (with optional date-range filter), compute aggregates.
- Explicitly define and document: "today" and "this week" boundaries (local timezone, week starts Monday) — write this down in BUILD_LOG so it isn't ambiguous later.
- Only log a session when it reaches natural completion (full focus interval elapsed) or explicit user "reset"/save action — no partial/incomplete sessions silently written on tab close; define and document this behavior explicitly.
- Test: unit tests for schema validation, label sanitization (attempt script/HTML injection in label, confirm it's neutralized), day/week boundary calculation across a few fixed fake "now" timestamps, and a live end-to-end test that POSTs a fake completed session and confirms it round-trips through the storage layer correctly.

### Priority 2: Timer UI — start/pause/reset, wall-clock accurate
Build the client-side timer as its own testable unit, decoupled from the dashboard chrome so Priority 3 can integrate it visually without touching timer logic.
- Implement timer using stored `startTimestamp` (`Date.now()`) plus periodic diffing against wall clock for display — explicitly not naive `setInterval` tick-counting — so backgrounded tabs or laptop sleep don't desync the displayed time from actual elapsed time.
- Controls: Start, Pause (freezes elapsed time, resumable), Reset (clears current interval without logging a session). Support both focus and break intervals in sequence (auto-transition focus→break, with a visible/audible or at least visual state change, not requiring an external notification system).
- Configurable durations (default 25 min focus / 5 min break) via a simple settings control on the same page — persisted alongside the session store or in a small config record, not hardcoded.
- Persist in-progress timer state (start timestamp, mode, remaining config) to `localStorage` (or session-equivalent) so a page reload mid-session doesn't lose progress — reconstruct correctly from wall-clock diff on reload.
- On focus-interval completion, call the Priority 1 logging endpoint with the actual elapsed wall-clock duration (not the configured target

---
The only message I want from you is the final deliverable. Go.


<!-- ============ COUNCIL REVIEW (not part of the launch prompt) ============ -->
## Decision Council Verdict

## Council deliberation: Add a Pomodoro focus timer to Alex's Jarvis dashboard: a UI with start/pause/reset controls for standard focus/break intervals (default 25 min focus / 5 min break, configurable), and each completed focus session gets logged (timestamp, duration, optionally a label/task tag) so it's persisted and viewable later (e.g. history list or simple stats like sessions today/this week). Should integrate cleanly with the existing dashboard's look and data storage patterns rather than being a bolted-on separate page.

### Advocate — the case for
- **Fills a real gap already implicit in the project's direction**: Alex is building Jarvis into a personal ops hub (vault search, notes, security-hardened access) — a focus timer with persisted session history is the natural next data source for "what did I actually do today," complementing the vault rather than duplicating it.
- **Low integration risk, high reuse**: the build log shows an established pattern for adding a feature cleanly (dedicated phase, sample data, live testing, docs) and an existing data storage/access-gate architecture to slot into — this isn't a new subsystem, it's a new table/route inside a system that already handles auth and persistence.
- **Immediate, tangible payoff with no dependencies**: unlike vault search (needs an indexed corpus) or chat tools (needs the LLM plumbing), a Pomodoro timer is self-contained — it can be built, tested, and used the same day, giving Alex a quick concrete win to point to.
- **Turns unstructured effort into visible, motivating data**: "sessions today/this week" stats give Alex passive accountability every time he opens the dashboard, and the optional task label turns raw timer usage into a lightweight time-tracking log he can later cross-reference against vault notes (e.g., "How much focused time went into School this week?").
- **Cheap to build, cheap to keep**: start/pause/reset plus a log table is a small, well-understood feature surface (no external APIs, no new secrets, no security-hardening burden like Phases 1-2 required) — it's unlikely to introduce the kind of risk this project just spent two phases cleaning up.
- **What's lost by skipping it**: Alex keeps using some separate timer app (or none), so focus sessions stay invisible to the one dashboard meant to reflect his real life — a missed chance to make Jarvis the single place he looks to answer "am I actually focusing, and on what."
- **Compounding value over time**: the logged history is cumulative — a week from now it's a few data points, a quarter from now it's a genuine trend line (streaks, best focus days, drop-off patterns) that's only available if logging starts now rather than being retrofitted later.

### Critic — the case against
- **Scope/timing mismatch**: the log shows Alex just finished a security-hardening + vault-indexer push; bolting a whole new stateful feature (timer + persistence + stats UI) on immediately risks scope creep before the last thing is even used/validated. "Should it exist now" is a fair question before "should it integrate cleanly."

- **New persistent data store = new attack surface + maintenance burden**: you just spent a phase locking down `.env`, access gates, and binding. Every new table/log file (session history) is another thing that needs the same rigor (write permissions, input validation on labels/tags, no injection via task-tag strings) — easy to bolt on carelessly right after finishing "security hardening" and undo the discipline.

- **Redundant with existing tools**: Alex almost certainly has a phone, OS-level, or browser Pomodoro app already (free, tested, has notifications/sound/lock-screen support). A dashboard-embedded timer can't background-alert as reliably as a native app/phone — you may be rebuilding a worse version of something solved.

- **Timer correctness is deceptively hard**: browser tab timers drift/pause when tab is backgrounded or laptop sleeps; naive `setInterval` countdowns will desync from wall-clock time. If "duration logged" is wrong because the tab was inactive, the stats feature becomes actively misleading rather than just missing.

- **Ambiguous spec invites silent scope growth**: "optionally a label/task tag," "stats like sessions today/this week" — each of these is a small design decision (freeform text vs. fixed task list? week starting Sunday/Monday? timezone handling for "today"?) that can balloon a "simple timer" into a mini project-tracking feature nobody asked to fully spec.

- **No stated success criterion**: unlike the vault work (tested via 5 real chat queries with citations), there's no test plan here — how do you know the feature is "done" beyond "buttons work"? Without that, it's easy to ship a timer that looks fine in a screenshot but never gets used because it doesn't fit Alex's actual workflow (e.g., he starts sessions from his phone, not the dashboard).

- **Alternative worth considering**: a much cheaper version — just a `log_focus_session` tool/endpoint Alex (or Jarvis via chat) can call, with history viewable in existing chat/search, no new timer UI at all. That reuses the vault-indexer pattern already built and validated, instead of adding a second, disconnected UI subsystem (start/pause/reset state machine) to maintain.

### Feasibility Judge — can it actually work?
**Plausibility: 9/10 (likely)** — This is a small, well-understood feature (client-side timer + simple logging) built on top of infrastructure Alex has already proven he can build (auth, Flask routes, data persistence), so it's squarely in "achievable, not just hard" territory.

**Technical feasibility** — Fully mature, boring technology: JS `setInterval`/`Date.now()` for the timer, a small backend route + existing storage (whatever the vault indexer/chat history already uses — likely flat files or SQLite) for session logs, and basic aggregation (count/sum by day) for stats. Nothing here requires new libraries, APIs, or research-grade work.

**Resource realism** — This is a few-hours-to-one-weekend project for someone who already has a working Flask app, login gate, and a dashboard shell: timer UI/state machine (~1-2 hrs), logging endpoint + schema (~1 hr), history/stats view (~1-2 hrs), styling to match existing dashboard (~1-2 hrs, the most variable part). Well within solo-generalist-with-limited-time budget.

**Causal chain** — (1) timer logic correctly tracks focus/break intervals → (2) completed sessions get POSTed/saved with correct timestamp/duration/label → (3) persisted data survives reloads and matches existing storage schema/conventions → (4) history/stats view correctly reads and aggregates that data → (5) UI is styled to blend with the rest of the dashboard. Weakest link: **step 3, fitting cleanly into "existing dashboard's data storage patterns"** — if that pattern isn't already generic/flexible, shoehorning a new data type (timestamp+duration+tag) in without duplicating or hacking the schema is the part most likely to take longer than expected or produce a slightly bolted-on result despite the stated goal.

**Most likely failure mode** — Not failure to build it, but scope/consistency drift: the timer works, but "clean integration" slips — e.g., browser-tab throttling causes timer drift when backgrounded, a session is lost if the tab closes mid-focus-block (no crash recovery), or the UI ends up visually close-but-not-quite matching the rest of the dashboard, making it feel like an add-on rather than a native feature

### Judge's ruling
**Ruling: WORTH IT IF** — the timer is built on wall-clock timestamps (start time + `Date.now()` diffing, not naive `setInterval` tick-counting) so backgrounded/sleeping tabs don't silently corrupt logged durations, and the session log reuses the existing storage pattern/schema (flat file or SQLite, whatever the vault indexer already uses) rather than inventing a parallel data format.

**Reasoning:** Feasibility is genuinely strong (9/10, boring mature tech, few-hours-to-weekend scope, no new dependencies or secrets) — this is the strongest signal here and rules out "great idea, can't be pulled off" concerns. The critic's redundancy argument ("just use a phone app") is the weakest point since a dashboard-native timer with logged history serves a different purpose (structured data feeding into Jarvis) than a standalone timer app ever would. But two critic points land hard and align directly with the feasibility judge's own identified weak links: timer drift/background-tab correctness (explicitly flagged as the "most likely failure mode") and ambiguous spec inviting scope creep (label format, week boundaries, timezone) — both are cheap to fix upfront by constraining the spec now rather than discovering the mess mid-build. The advocate's "low integration risk" claim is only true if the storage-pattern question (step 3 in the causal chain) is resolved cleanly, which isn't guaranteed. With those two constraints nailed down before starting, this is a low-risk, self-contained, compounding-value feature well worth building.

**What would change my mind:** If the existing dashboard's storage layer turns out to be rigid/single-purpose (not easily extended to a new log type) rather than generic — that would validate the critic's "bolted-on" risk and push this toward NOT WORTH IT as scoped, favoring the cheaper `log_focus_session`-tool alternative instead.
