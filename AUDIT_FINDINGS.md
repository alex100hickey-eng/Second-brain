# AUDIT_FINDINGS.md — Intent vs. Reality

**Audit date:** 2026-07-21 (00:30–01:15 ET). **Spec:** `AUDIT_INTENT.md` (88 items). **Method:** every claim exercised against the running system — full test suites, live HTTP against the app on 127.0.0.1:5001, direct function calls for guards/sandbox, git/launchd/log forensics. Read-and-report only; nothing was fixed.

## Executive summary

The promised system is overwhelmingly real: of 88 intent items, all but a dozen check out exactly as documented, and **every security invariant I tested held under attack** (path guards, sandbox, gate, vault read-only, gitignore, no-control-code). The worst finding is that the live app's two background workers — the execution layer of the Task Manager, Alex's stated top priority — are intermittently failing their polling cycles every few minutes with socket errors on the very instance the handoff calls "running clean," and that same Task Manager has zero reproducible test coverage in the repo. I'd fix the worker thread-safety issue first, then check the Task Manager's (re-verified tonight, still passing) safety guarantees into `run_tests.py` so they stop depending on one-off session scripts.

---

## Findings

### #1 — Live background workers intermittently failing
- **Intended:** "Local app restarted and running clean … zero import/wiring errors" (handoff-21 §4); delegated tasks and managed tasks are claimed by daemon workers polling every 8 s (intent #34/#35).
- **Actual:** The monitor (working as designed) shows **overall: degraded, 14 incidents** in the ~50 minutes since the 23:38 restart. Both `jarvis-task-worker` and `jarvis-managed-worker` log `worker cycle failed — [Errno 35] Resource temporarily unavailable` (plus one h2 state-machine error, `Invalid input StreamInputs.SEND_HEADERS in state 5`) at 00:18, 00:23, 00:25, 00:28, 00:29… Roughly ~2% of cycles fail; the workers are fail-soft and keep running, so tasks still get claimed — but the errors are continuous and the h2 error signature points at the **shared Supabase HTTP client being used concurrently from both worker threads + request handlers**, which corrupts the connection.
- **How it's wrong:** "Running clean" is only true at import time; at runtime the instance is in a permanent low-grade error loop the docs don't mention.
- **Severity:** DEGRADED
- **Suggested fix:** Give each worker thread its own Supabase client (or wrap shared-client calls in a lock). ~An hour, including watching the incident log go quiet.

### #2 — Task Manager has no reproducible tests
- **Intended:** "Verified secure via direct tests: path guards reject /etc, ~/.ssh, ~/Library, the repo, traversal; move/undo round-trips; sandbox blocks network + out-of-scratch writes at two layers; kill switch + guardrail fail-closed confirmed" (handoff-21 §2.7). README: run_tests.py is "the single regression suite — run it after any change."
- **Actual:** `run_tests.py` has 21 suites; **none touches `task_manager.py`**. No test file for it exists anywhere in the repo — the "direct tests" from the 2026-07-19 sessions were never checked in. I re-ran the equivalents tonight and they **pass**: `_safe_path` blocked all 8 attack paths (`/etc/hosts`, `~/.ssh/id_rsa`, `~/Library`, the repo, two traversals, `~/.zshrc`, `/tmp`) and allowed `~/Downloads`/`~/Desktop`; the sandbox ran a benign tool (exit 0, correct output) and blocked a `~/.zshrc` read, a network fetch, and an out-of-scratch write to `~/Desktop`.
- **How it's wrong:** The most dangerous subsystem's safety properties are enforced in code but protected by no regression test — a future refactor could silently weaken them.
- **Severity:** DEGRADED
- **Suggested fix:** Port tonight's checks into a `suite_taskman` in run_tests.py (path guards, sandbox three-way block, undo round-trip, guardrail fail-closed with a stubbed council). Its own short run.

### #3 — Cinematic homepage truncation bug: still open, and missing from the master handoff
- **Intended:** handoff-21 presents itself as "the single canonical, comprehensive record" including open items (§5); the website agent's cinematic mode is described as robust (effects.css isolation, `_balance_braces`, `_fix_images`, `_ensure_script`).
- **Actual:** The 2026-07-20 evening session found cinematic homepages can **truncate at max_tokens=4096** (demo `index.html` was cut mid-tag; no `</body>`/`</html>`). The `_ensure_script` fix was committed (`20c1a2b`) — but page builds still use `max_tokens=4096` (`website_creator_agent.py:629`) with **no completion guard** (nothing detects a missing `</html>` and repairs/regenerates). The bug is absent from handoff-21 §5's open-items list.
- **How it's wrong:** A known, reproducible defect survived into the "comprehensive" handoff undocumented, and the code path is unchanged.
- **Severity:** DEGRADED
- **Suggested fix:** Raise max_tokens for cinematic payoff pages (e.g. 8000) and add a missing-`</html>` detect-and-repair pass mirroring `_balance_braces`; add the bug to the handoff. ~An hour.

### #4 — Vault-sync: intermittent iCloud failure mode confirmed (open item 4, now characterized)
- **Intended:** vault_sync launchd job auto-commits the writable vault every 10 min; handoff-21 §5.4 flags it "was failing silently (exit 128) — re-verify it's actually syncing."
- **Actual:** Re-verified. It **is currently syncing**: launchctl last exit 0, vault repo clean at `c624c1a`, last push 2026-07-20 11:02. But the log shows the documented failure mode is real and recurring: four consecutive `fatal: error reading …/Second brain/.git` runs between Jul 19 12:35 and Jul 20 11:02 — the morning brief written at 07:00 didn't reach GitHub until 11:02, a **~4-hour silent sync outage** caused by iCloud evicting the `.git` directory. It self-recovered. The script exits silently on no-changes, so healthy-idle and broken look identical in the log.
- **How it's wrong:** Works today, but the failure recurs and is invisible while happening.
- **Severity:** DEGRADED
- **Suggested fix:** Move the vault's git dir out of iCloud (`git init --separate-git-dir ~/.vault-git` pattern) so iCloud can't evict it, and/or `report_event()` on git failure so the monitor surfaces it. ~An hour.

### #5 — `.env.example` incomplete; `GITHUB_TOKEN` documented nowhere
- **Intended:** README: "See `.env.example` for every variable," and README's own config table lists `OBSIDIAN_VAULT_PATH`, `HOST`, `FLASK_DEBUG`.
- **Actual:** `.env.example` has only 7 variables (ACCESS_CODE, CLAUDE_API_KEY, COMPOSIO_API_KEY, FLASK_SECRET_KEY, PORT, SUPABASE_KEY, SUPABASE_URL). Missing: `OBSIDIAN_VAULT_PATH`, `HOST`, `FLASK_DEBUG` (all in README's table), `VAULT_PATH`, the optional search keys (`TAVILY_API_KEY`/`SERPER_API_KEY`/`BRAVE_API_KEY` — README says "drop in .env"), `EMBED_MODEL_ID`, `MEMORY_SESSION_GAP`, `JARVIS_RUNTIME` — and **`GITHUB_TOKEN`**, which `expansion_pipeline.py` reads and which appears in no doc at all.
- **How it's wrong:** The "every variable" claim is false; one variable the newest subsystem depends on is entirely off the paper trail.
- **Severity:** WRONG
- **Suggested fix:** Add the missing lines (commented, with one-line explanations) to `.env.example` and a GITHUB_TOKEN mention to the README/handoff. Minutes.

### #6 — Six newest tools skip the "sacred" modular pattern
- **Intended:** "One `TOOLS` schema entry + one same-named function + one `handle_tool_call` routing line + **a status label** + **a system-prompt mention**. Every capability follows this." (handoff-21 §2.1)
- **Actual:** `run_scout`, `review_findings`, `apply_finding`, `check_expansion_findings`, `check_system_health`, `check_budget` all have schemas, functions, and dispatch — but **none has a `TOOL_STATUS_LABELS` entry** (the UI falls back to a generic "Working on it…") and **none is named in `SYSTEM_PROMPT`** (a general expansion paragraph exists; the monitor tools aren't described at all). Tools still work — the model sees the schema descriptions.
- **How it's wrong:** The pattern the docs call sacred was followed 3/5 for the six newest tools.
- **Severity:** COSMETIC
- **Suggested fix:** Six label entries + two or three SYSTEM_PROMPT sentences. Minutes.

### #7 — Whisper test failure: wrong explanation in the handoff, weak guard in the test
- **Intended:** handoff-21 §4: "170/171 offline (the 1 failure = whisper model file absent in some checkouts, environmental)."
- **Actual:** The model file **is present** (`models/ggml-base.en.bin`, 147 MB) and whisper works — it transcribed a real speech clip verbatim during this audit. The real cause: the test generates its sample with `say -o`, and under a sandboxed shell `say` silently emits a ~4 KB header-only file; the test's guard (`size > 1000`) passes it, whisper correctly transcribes silence as `''`, and the check fails. In a normal terminal it passes (the 171/171 `.last_test_pass` from Jul 20 17:56 is genuine).
- **How it's wrong:** The documented root cause is factually wrong (the conclusion "environmental" happens to be right), and the test can't distinguish empty audio from real audio.
- **Severity:** COSMETIC
- **Suggested fix:** Probe the generated sample's duration (>0.5 s) and skip with a clear message when `say` produces silence; correct the handoff sentence. Minutes.

### #8 — Master handoff figures drifted: tool count and app size
- **Intended:** "46 chat tools live today"; "app.py (~3,700 lines)" (handoff-21 §2).
- **Actual:** `app.TOOLS` contains **66 schemas** — 56 native tools + 10 Composio (4 Calendar + 6 Gmail) — with no duplicates and full dispatch coverage. app.py is 4,086 lines.
- **How it's wrong:** Undercount by 10+ (the count appears to predate the Task-Manager/expansion/monitor tool additions).
- **Severity:** COSMETIC
- **Suggested fix:** Correct the two figures in handoff-21. Minutes.

### #9 — BUILD_LOG.md is truncated and missing whole subsystems
- **Intended:** "BUILD_LOG.md has the fullest phase-by-phase engineering record" (handoff-21 header).
- **Actual:** The file **ends mid-list** at line 751 — the Round-5 "final verification" section has one bullet and stops. And it contains no record at all of the Task Manager, the HUD rebuild, the expansion pipeline, or the monitoring agent (those exist only in handoffs).
- **Severity:** COSMETIC
- **Suggested fix:** Append the missing Round-5 closing bullets and a short pointer ("Task Manager/HUD/expansion/monitor: see handoffs"). Minutes.

### #10 — Phantom `jarvis_tasktracker` row type
- **Intended:** `INTERNAL_AGENT_NAMES` entries correspond to real internal row types (handoff-21 §3 lists 13 row types; this isn't one of them).
- **Actual:** `app.py:261` filters `"jarvis_tasktracker"` with a comment "lightweight task-tracker mirror rows (see task_tracker.py)" — but **no code anywhere writes such rows**; task_tracker.py has zero Supabase references (as the docs correctly say: local SQLite only).
- **Severity:** COSMETIC (harmless defensive filter; misleading comment)
- **Suggested fix:** Delete the entry or fix the comment. Minutes.

### #11 — Cost tracking over-estimates during Sonnet intro pricing
- **Intended:** pricing.json rates "seeded from Anthropic's published pricing… please verify" (SECURITY_NOTES §8).
- **Actual:** Verified against current published pricing: all three models and both cache multipliers are **correct** ($3/$15 Sonnet 5, $5/$25 Opus 4.8, $1/$5 Haiku 4.5; cache read 0.1×, 5-min write 1.25×). But intro pricing of $2/$10 is in effect through **2026-08-31**, so the tracker currently over-reports Sonnet spend by ~33%. The file itself discloses this in a note — deliberate conservatism, now confirmed.
- **Severity:** COSMETIC
- **Suggested fix:** None required; optionally set $2/$10 until 2026-09-01 for exact figures. Minutes.

### #12 — `sites/inkling-1`: duplicate build outside the idempotency window
- **Intended:** Idempotency guard: "one request produces exactly one build" via module lock + 5-min TTL cache (BUILD_LOG R3 P1); double-build bug "unproven-fixed" (handoff-20).
- **Actual:** `sites/inkling` (Jul 20 11:41) and `sites/inkling-1` (Jul 20 17:53) — same brief built twice, six hours apart. The guard behaved **as designed** (its TTL is only 5 minutes; the second build likely came from Round 5's `--live` test run), but the artifact shows the protection is narrow and the duplicate sits undocumented on disk.
- **Severity:** COSMETIC
- **Suggested fix:** Nothing, or extend the guard to check `sites/` for an existing slug and ask before rebuilding. Minutes to decide; delete `inkling-1` if unwanted (Alex's call — audit deleted nothing).

---

## Documented-open items (§5 of the handoff) — all confirmed accurately open

1. **Budget cap** — still the $20 placeholder; monitor live and reporting ($0.81 spent this month, 4% of cap, tier "ok"). ✔ open as documented.
2. **Server not redeployed** — not directly verifiable tonight (audit rules: no external services). Local `main` (`bbc4aae`) **is** pushed to origin — confirmed by fetching the identical handoff from GitHub. Coolify state unverified.
3. **Task Manager on server** — still unverified anywhere; and see finding #1 for its local runtime health.
4. **Vault-sync** — re-verified; see finding #4.
5. **Branding** — confirmed: index.html, login.html, home.html, and the PWA manifest ("Second Brain"/"Jarvis") all still Jarvis-branded; only the HUD carries CLARVIS. ✔ open as documented.
6. **`jarvis/tool-get_word_count`** — branch exists locally and on origin, `proposed_tools/get_word_count.py` present, still awaiting merge/discard. ✔
7-10. HTTPS, server-side draft persistence, expansion install→live-tool adapter step, and the lower-priority list — all accurately still open; `~/.jarvis_expansion/` doesn't exist yet, consistent with zero findings ever applied (`expansion` dashboard bucket empty).

---

## Test results (run tonight)

| Suite | Result | Notes |
|---|---|---|
| `run_tests.py` (offline, 21 suites) | **170 passed / 1 failed** | Failure = voice `say`-sample check; see finding #7 — harness artifact, whisper itself verified working on real audio |
| `test_expansion_monitor.py` | **53/53** | Scout dedup, rubric, calibration backstop, applicator approval-gate, budget tiers, fixer allowlist, event log |
| `test_vault_tools.py` | **18/18** | Incl. byte-for-byte read-only vault guarantee |
| Direct: `_safe_path` attack battery | **8/8 blocked, 2/2 allowed** | This audit |
| Direct: sandbox lane (`sandbox_test_tool`) | **benign ✓ / secret-read ✗ / network ✗ / escape-write ✗** | This audit — all blocks held |

## Matches — items that fully check out (the good news)

Verified exactly as documented (intent item numbers in parentheses):

- **Gate & network:** unauth `/` → 302 /login; unauth API → 401; wrong code delayed 0.88 s; correct code → 31-day session; all pages 200 authed; LAN connection refused; single process bound to 127.0.0.1:5001 only; debug off (#3, #4, #59, #60, #74).
- **Routes & data:** `/dashboard`, `/hud`, `/memory` all 200; `/api/home` returns all 16 promised data buckets (tasks, council, activity, vault notes, reports, sites, goals, drafts, memory, captured, cost, health, monitor, expansion…); `/reindex` returns note count (2, real vault); `/reindex-all` gated; `/api/weekly-review?fast=1` 200; `/preview/<slug>/` 200 with traversal → 404; drafts raw view + status flow (#5, #11, #13, #14, #33).
- **Tooling integrity:** 66 tool schemas, zero duplicates, every native tool dispatched, Composio tools routed via their own branch; `list_drafted_runs`, `undo_file_operations`, all task/goal/memory/council/media tools present (#7, #8 modulo finding #8's count).
- **Security invariants:** no secrets in any .py or in git history (suite); `.env` mode 600, untracked; full gitignore coverage (all 4 DBs, screenshots/, model weights, vault_inbox notes, media dirs), no inline-comment regressions; no mouse/keyboard control code (suite-enforced); run_drafter has no subprocess; jarvis-launch.sh never invokes claude; screenshots deleted after processing (suite); Obsidian vault byte-for-byte unchanged (suite, twice tonight) (#58, #61–65).
- **Task Manager guards (re-verified live):** home-dir-only path guards with dotdir/Library/repo/traversal rejection; sandbox denies secrets, network, and out-of-scratch writes while running benign tools correctly; `BACKGROUND_EXCLUDED_TOOLS` structurally bars autonomous runs from task-spawning and all expansion tools; budget gate (`is_agent_allowed`) wired into `run_scout` (#37–41, #46 exclusion claim).
- **Connectors:** Calendar = exactly 4 read-only slugs; Gmail = exactly 6 read-only slugs; **no send/draft/delete tool exists anywhere**; `get_today_events` 5-min cache; propose/approve flow routes through `jarvis_pending_action` + `/api/approve` (#42–44, #66).
- **Memory & search:** conversation memory, FTS + semantic recall, `/memory` CRUD (suite); embeddings model loads and embeds; semantic index incremental logic (suite); `remember`/`forget` row types filtered from outputs, incl. the just-fixed leak entries (#18–21, #67).
- **Monitoring:** health checks all green (DBs readable, index fresh, binaries present, disk OK, backup age, real `.last_test_pass`); monitor incident log capturing real events (see #1 — the system caught its own bug); budget tiers computing correctly from real spend; fixer allowlist matches docs exactly (#47–50).
- **Media pipeline:** whisper transcribes real audio verbatim; ffmpeg/ffprobe/whisper-cli/model all present; upload endpoint has the 500 MB cap, extension whitelist, secure_filename (#25–26, #76).
- **Ops:** morning brief ran on schedule Jul 20 07:00 and wrote the vault note; backups exist with retention config; shortcuts.json complete; pricing verified (#32, #52–53, #69).
- **UI wiring:** HUD uses `ev.label` and `clarvis_revenue`; overlays + reduced-motion present; index.html handles 401-bounce and `?q=` prefill; PWA assets present (#9, #12).
- **Repo state:** clean tree, `main`=`bbc4aae` pushed, rollback tag `pre-expansion-subsystems` exists, review branch present (#73).

## Undocumented reality — exists in the codebase, appears in no doc

- **`GITHUB_TOKEN`** env var read by the expansion scout (also finding #5).
- **`jarvis_tasktracker`** phantom filter entry (finding #10).
- **`sites/inkling-1`** duplicate build (finding #12), plus `_archive/` contents (partially documented) and leftover test media in `inbox/`/`media_lib/` from Round-2 verification (harmless, gitignored).
- **A once-a-minute polling loop is hitting `/api/dashboard` with two 401s then a 200** — almost certainly stale + live HUD browser tabs left open; ambient noise, not a code defect, but it's been writing 3 log lines/minute since restart.
- **`scripts/app_overnight.log`, `site_build.log`, `morning_brief.log`** — operational logs no doc mentions (contents look healthy).
- The live app's stdout goes to a prior session's scratchpad file (`app_restart.log` in a temp dir) — it will vanish on temp cleanup; harmless but worth knowing when hunting logs.

---

*App state at audit end: running, PID 78382, bound 127.0.0.1:5001, access code enforced, all endpoints healthy. Nothing was modified, deleted, deployed, or pushed; the only files created are AUDIT_INTENT.md and AUDIT_FINDINGS.md.*
