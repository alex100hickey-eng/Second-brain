# Habit Streak Tracker

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

### Priority 1: Habit data model & isolated persistence layer
- New module `habit_tracker.py`, structurally mirroring `task_tracker.py`'s CRUD/persistence pattern but with its **own storage namespace** (its own file or a clearly separate top-level key/section, never commingled with `task_tracker.py`'s schema) — a bug here must not be able to corrupt or block loading of existing task/dashboard data.
- Data model: `Habit{id, name, created_date, active}` and per-day marks `HabitLog{habit_id, date (ISO, no time component), done: bool}`.
- CRUD operations: create habit, list/archive/delete habit, mark a specific date done/undone, edit a past date's mark (backfill).
- Explicitly define and document semantics before writing streak logic:
  - Date is always caller-supplied ISO date, never derived from server wall-clock at call time — no timezone ambiguity, no "marked done late at night flips the day" bugs.
  - No entry for a day = "not done," distinct from "no habit data at all" (a brand-new habit with zero entries is not the same as a habit with a 0-day current streak).
- Tests: CRUD round-trip (create/mark/reload), persistence survives process restart, and an explicit isolation test proving

## SUCCESS CRITERIA

- Every priority above is complete and works end-to-end (verified, not just written).
- `run_tests.py` is extended to cover each new feature and the full suite passes.
- All security invariants intact: secrets in `.env`, bound to 127.0.0.1 only, access-code gate enforced, Obsidian vault never written, no mouse/keyboard control code, drafter/screen-watch limits held.
- BUILD_LOG.md updated per phase (completed, decisions, pivots, issues).
- Final message: per-priority status, testing summary, limitations, and my action list.

---
The only message I want from you is the final deliverable. Go.


<!-- ============ COUNCIL REVIEW (not part of the launch prompt) ============ -->
## Decision Council Verdict

## Council deliberation: Add a simple habit-streak tracker to the dashboard — let Alex define habits, mark them done per day, and see current/longest streak per habit, persisted alongside the existing dashboard data.

### Advocate — the case for
- **Leverages proven infrastructure**: The build log shows a task-tracker with "full CRUD + history + persistence" already exists and works — this is the same pattern (define item, mark done, persist, display) applied to a new data type, so most of the hard engineering (persistence layer, dashboard integration) is already solved rather than invented from scratch.

- **Low marginal complexity, high behavioral payoff**: Habit streaks are one of the best-evidenced simple mechanisms for sustaining behavior change — the visible "don't break the chain" signal is what tasks alone don't provide (tasks are one-off; habits are recurring identity-level patterns).

- **Fills a real gap**: The dashboard already has task tracking and a decision council, but nothing captures recurring daily behaviors (exercise, reading, meditation, etc.). Right now that data either lives nowhere or in Alex's head — losing it means losing the only objective record of consistency over time.

- **Compounding visibility**: Current/longest streak numbers turn invisible daily effort into a visible asset. Without this, Alex has no way to notice patterns like "I always break around day 10" or celebrate genuine progress — the data to self-correct simply doesn't exist.

- **Consistent with existing design philosophy**: The vault integration project already established patterns for clean data delimiting and persistence alongside existing dashboard data — adding habits alongside tasks keeps a single coherent source of truth rather than fragmenting Alex's tracking across tools.

- **Cheap to build, cheap to regret not building**: Given the CRUD+persistence groundwork already exists from the task tracker, this is a small, bounded addition. Deferring it costs real momentum-building time now, for a feature that only gets more valuable the earlier streaks start accumulating.

### Critic — the case against
- **Likely duplicate of existing functionality** — `task_tracker.py` already does "full CRUD + history + persistence"; a habit tracker is functionally a task tracker with date-based recurrence and streak math layered on top. Building a parallel system risks two overlapping data stores and two UIs for what's conceptually the same "did I do X" tracking.

- **Streak logic is deceptively fiddly** — timezone/day-boundary handling, "what counts as missing a day," retroactive edits breaking streak counts, and longest-streak recomputation on backfill are classic sources of off-by-one bugs. This is more edge-case-heavy than it looks from the one-line spec.

- **Security hardening was just the focus of the prior build** — the vault integration work went through explicit phases to sandbox/delimit untrusted data and lock down what gets persisted. Bolting a new user-writable, persisted data structure onto the dashboard reopens questions (input validation, injection into whatever renders the dashboard, persistence-layer trust boundaries) that were presumably just closed off. Needs to go through the same rigor, not skip it because it's "simple."

- **"Simple" features have a habit of scope-creeping** — once streaks exist, the natural asks are reminders, streak-freeze/grace days, historical charts, habit categories, export. If none of that is wanted, fine, but worth stating explicitly now so it doesn't silently grow.

- **Unclear this solves a real bottleneck** — nothing in the build log suggests habit tracking was a stated need; it reads like an adjacent nice-to-have riding on the dashboard's momentum. Opportunity cost: time spent here is time not spent hardening/extending the task tracker, video pipeline, or website/data-synth agents that were the actual multi-phase focus.

- **Alternative with less new-surface-area**: extend `task_tracker.py` with a "recurring" flag and derive streaks from existing completion history, rather than standing up a second persisted entity. Reuses tested CRUD/persistence code instead of duplicating it.

- **Persistence coupling risk** — "persisted alongside the existing dashboard data" is vague; if it means bolting onto the same file/schema as other dashboard state without a migration/versioning plan, a bug in the new feature can corrupt or break loading of the unrelated data it's stored next to.

- **No stated success criteria** — prior phases all end in explicit DONE/verified criteria; this idea has none (how many habits, what happens on missed days, is a 0-day streak shown differently from "no data yet"). Worth defining before writing code.

### Feasibility Judge — can it actually work?
**Plausibility: 9/10 (likely)** — This is a small, well-understood CRUD+date-math feature that closely mirrors the existing task_tracker.py, so it's almost entirely a matter of following an established pattern rather than solving anything new.

**Technical feasibility** — Fully solved territory: storing a list of habits, per-day boolean marks, and computing current/longest streak is basic date arithmetic and persistence, no exotic dependencies or unproven tech involved. The only "hard" parts are edge cases (timezones, day boundaries, backfilling missed days), which are well-known problems with known solutions, not research problems.

**Resource realism** — Given task_tracker.py already exists as a template for CRUD + persistence within this dashboard, this should take Alex a few hours to a day, well within a solo student's after-class time budget and skill level. No new tools, libraries, or infra are needed beyond what's already running.

**Causal chain** — (1) define habit data model → (2) UI/API to mark done per day → (3) streak calculation logic → (4) persistence layer stores it alongside existing dashboard data without corrupting other data → (5) dashboard renders it correctly. The weakest link is (3)/(4) combined: streak math around timezones/missed-day edge cases, and making sure the new data writes don't collide with or destabilize the existing persistence file/schema used by task_tracker and other dashboard modules.

**Most likely failure mode** — Streak counts silently go wrong in an edge case (e.g., marking a habit done late at night flips it to the wrong day, or a timezone mismatch causes an off-by-one that breaks "current streak" without Alex noticing until he's lost trust in the number).

**What would raise the rating** — Already near ceiling; it would tick up to a clean 10 if the persistence schema is explicitly namespaced/isolated from other dashboard data (so a bug can't corrupt unrelated data) and if a couple of unit tests cover the day-boundary/timezone edge cases before calling it done.

### Judge's ruling
**WORTH IT IF** — implemented as a namespaced/isolated data store (not commingled with task_tracker's schema, to avoid corruption risk) with explicit success criteria defined upfront (missed-day behavior, backfill/edit rules, 0-day vs no-data display) and basic unit tests covering day-boundary/timezone edge cases before it's called done.

The Feasibility Judge and Advocate agree this is low-risk, well-understood engineering that closely mirrors the proven task_tracker pattern — that's the strongest point in favor, and it neutralizes the Critic's "reopens security questions" concern somewhat since it's simple persisted data, not untrusted external input like the vault content was. The Critic's strongest hits are the persistence-coupling risk ("alongside existing dashboard data" is genuinely vague and could corrupt unrelated state) and the missing success criteria, both of which are cheap to fix now and expensive to fix after a corrupted schema or a silently-wrong streak erodes trust. The Critic's "just extend task_tracker with a recurring flag" alternative is worth considering but isn't a blocker — it's a design choice, not a feasibility gate. The scope-creep and "not a stated bottleneck" objections are real but minor; they argue for keeping v1 narrow, not for skipping it. Net: high plausibility, small effort, clear gap filled — but only if the two concrete safeguards (isolation + edge-case tests) are actually done, not skipped because "it's simple."

What would change my mind: if the existing dashboard persistence layer turns out to be a single monolithic file/schema that can't be cleanly namespaced without nontrivial refactoring, this tips toward NOT WORTH IT (yet) until that refactor is scoped separately.
