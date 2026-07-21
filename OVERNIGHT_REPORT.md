# OVERNIGHT REPORT — 2026-07-21 (audit fixes + reliability + smarter brain)

## 1. Verdict

All three priorities landed. **Priority 1 (every audit finding) is done** — 11 of 12 findings
FIXED with test proof, and finding #4's risky half (moving the vault's `.git` out of iCloud) is
written up as a ready-to-run, backup-first plan for you rather than attempted blind on a live vault.
**Priority 2 (reliability & speed) is done and verified end-to-end against the running app** —
startup self-check, streaming with a clean mid-response fallback, and a persistent background job
queue. **Priority 3 (smarter brain) is mostly done** — memory distillation and retrieval tuning are
complete with tests; cross-feature awareness landed as one concrete task↔council link with the
broader silo-linking documented as a follow-up.

**The one thing to look at first:** the worker thread-safety fix (finding #1). The pre-fix instance
was logging ~30 worker socket errors an hour; the fixed instance logged **zero over a 31-minute
observation window**. It's verified, but it's the most important change, so glance at the worker
health section below and, if you want extra assurance, watch the dashboard "Budget & Incidents"
panel stay quiet through tomorrow.

Full test suite went from **170 passed / 1 failed → 243 passed / 0 failed**. 17 commits, all local
(nothing pushed, per the run's rules). The app is running healthy on `127.0.0.1:5001` (PID 83625)
with all changes loaded and workers quiet.

---

## 2. Priority 1 scorecard — all 12 findings

| # | Finding | Status | Proof |
|---|---------|--------|-------|
| 1 | Worker thread-safety (DEGRADED) | **FIXED** | Per-thread Supabase clients (`_ThreadLocalSupabase`). 0 worker incidents in 31 min vs 30 pre-fix. Proxy logic unit-tested. |
| 2 | Task Manager has no tests (DEGRADED) | **FIXED** | New `suite_taskman` (21 checks): `_safe_path` 8-block/2-allow, sandbox three-way block, move/undo round-trip, guardrail fail-closed. Offline, no residue. |
| 3 | Cinematic homepage truncation (DEGRADED) | **FIXED** | max_tokens 4096→8000 for cinematic pages + regenerate-on-truncation + `_ensure_complete_html` guard. 7 tests. |
| 4 | Vault-sync iCloud eviction (DEGRADED) | **FIXED (part 1) / NEEDS-ALEX (part 2)** | Part 1 done: `vault_sync.sh` timestamps IDLE/SYNCED/ERROR + reports failures to the monitor via `report_event.py` (verified end-to-end). Part 2 (`--separate-git-dir` migration) = ready-to-run plan below — deferred because it needs Obsidian closed + a real test commit on the live vault. |
| 5 | `.env.example` incomplete (WRONG) | **FIXED** | Every env var added (commented, verified against `os.environ` usages); GITHUB_TOKEN documented in README + handoff. |
| 6 | Six tools missing labels/prompt (COSMETIC) | **FIXED** | Labels + SYSTEM_PROMPT paragraphs added for all six; regression guard in `suite_observability`. |
| 7 | Whisper test guard (COSMETIC) | **FIXED** | `suite_voice` probes audio duration (ffprobe) and SKIPs on silent `say` output; handoff root cause corrected. |
| 8 | Handoff figures drifted (COSMETIC) | **FIXED** | "66 tools", "~4,100 lines" corrected in handoff (now 69 tools after tonight's additions). |
| 9 | BUILD_LOG truncated (COSMETIC) | **FIXED** | Round-5 closing bullets completed + subsystem pointer note added. |
| 10 | Phantom `jarvis_tasktracker` (COSMETIC) | **FIXED** | Comment corrected (defensive filter, no writer today); filter kept for forward-safety. |
| 11 | Intro pricing over-reports (COSMETIC) | **FIXED** | Explicit `_intro_pricing_note` in pricing.json (~33% over-report until 2026-09-01); conservative rates kept. |
| 12 | `inkling-1` duplicate (COSMETIC) | **FIXED** | Not deleted (your call — see removals below). Idempotency guard extended: `create_website` asks before rebuilding an existing site (`on_existing='ask'` → confirmation; `force` to proceed). 4 tests. |

Also addressed the audit's "undocumented reality": app stdout redirected to durable `scripts/app.log`
(was a temp-dir file that vanishes); operational logs + GITHUB_TOKEN documented in the handoff; the
stale-HUD-tab 401 polling noted (below, no code change).

---

## 3. Worker health — before vs after (finding #1)

- **Before (PID 78382, pre-fix):** monitor logged **30 worker incidents (15 per worker)** between
  00:00 and 00:58 — `[Errno 35] Resource temporarily unavailable` and h2 `SEND_HEADERS in state 5`,
  ~1 every 2 minutes, matching the audit's "14 in 50 min".
- **Root cause:** one shared `supabase` httpx/HTTP-2 client used concurrently by the Flask request
  handler, both background workers, and the monitor scan. HTTP/2 multiplexing isn't thread-safe.
- **Fix:** `_ThreadLocalSupabase` proxy — each thread lazily gets its own client. No call site changed.
- **After (PID 82083, fixed):** **0 error/critical worker incidents over 31 minutes** of observation
  while I did the rest of the work. The final instance (83625) is likewise quiet.
- The only `system_event` rows written post-fix were an info-level self-test (id 359, from verifying
  finding #4's monitor wiring) and two WARNING rows (ids 360/361) from the streaming **test** before
  I made it side-effect-free — not real streaming failures.

---

## 4. Priorities 2 & 3 — what was built + how to try each in 30 seconds

### P2.1 Startup self-check
Verifies every dependency at boot (env required/optional, DBs, index, embedding model, binaries,
disk, Supabase reachability). **Try it:** `tail -n 25 scripts/app.log` after a restart, or open the
dashboard — `/api/home` now has a `startup` bucket. A missing required dep prints loudly and is
reported to the monitor.

### P2.2 Streaming with clean fallback
Chat already streamed word-by-word; now a mid-response streaming failure retries once non-streaming
so the reply is never lost (UI self-corrects via `replace`/`final` events). **Try it:** just chat —
replies stream. (Verified end-to-end: a `/chat` turn returned text deltas then a `final` event.)

### P2.3 Background job queue
Long ops (website builds, data synthesis) run on a persistent, restart-surviving SQLite queue.
**Try it in chat:** *"Build me a site for a coffee cart, in the background."* → you get "Started
background job #N"; the result posts back into the conversation when done, and the dashboard `jobs`
bucket shows queued/running/done/failed. (Verified: enqueued a real synthesis job → the running
worker ran it to done in ~15s and announced it in chat.)

### P3.1 Memory distillation
Compresses old conversations into durable structured facts (preferences/decisions/topics/goals/open
threads) with provenance; originals kept; recall prefers distilled facts; never fabricates. **Try it
in chat:** *"Distill my old conversations"* → runs `distill_memory`. (Demo from tests: a grounded
fact is stored with its source session id; a fabricated "scuba diving in Fiji" fact with no basis in
the transcript is dropped by the traceability guard.)

### P3.2 Cross-feature awareness (partial)
Council verdicts and the task they evaluated now reference each other by id. **Try it in chat:**
*"Evaluate task #N"* → the council runs, the council row records `ref="task:N"`, and the task's
history shows a structured `council verdict: … [task:N]` entry (visible via "show task #N history").

### P3.3 Retrieval tuning
`search_everything` now dedupes near-identical hits, applies recency weighting, and re-ranks so the
best match across all sources surfaces first. **Try it in chat:** *"Search everything for <topic>"* —
results are de-duplicated and freshness-aware. (Demo from tests: a known-answer query surfaces the
right note first; between two equally-relevant hits the more recent wins; a strong match still beats
a weak-but-recent one.)

---

## 5. Test totals — baseline vs final

| Suite | Baseline (start of night) | Final |
|-------|---------------------------|-------|
| `run_tests.py` (offline) | 170 passed / **1 failed** (whisper `say`-sample) | **243 passed / 0 failed** |
| `test_expansion_monitor.py` | 53 / 53 | 53 / 53 |
| `test_vault_tools.py` | 18 / 18 | 18 / 18 |

New suites/checks added tonight: `suite_taskman` (21), website truncation+rebuild (11),
observability hygiene + startup (8), voice guard (fail→skip), `suite_streaming` (5),
`suite_jobs` (10), `suite_retrieval` (7), `suite_distillation` (10), task↔council (2).
`.last_test_pass` records the green 243/0 run.

---

## 6. Git log — tonight's commits (all local, nothing pushed)

```
8036997 P3.2 (partial): cross-link tasks and council verdicts by id
0108989 P3.1: memory distillation — compress old chats into durable facts
4879d4e P3.3: retrieval tuning — dedupe + recency weighting + re-rank
845f78d Log Priority 2 end-to-end verification against the running app
06f3e64 P2.3: persistent background job queue
a6ec83a P2.2: streaming fallback — never lose a reply on mid-stream failure
b0071f3 P2.1: startup self-check integrated with the health system
40046fb Docs: update handoff to reflect tonight's audit-fix run + operational logs
f48a92b Findings #11/#12: pricing note + ask-before-rebuild site guard
1129b28 Findings #8/#9/#10: fix doc/code drift
71739d0 Finding #7: skip whisper test on silent say output; fix handoff root cause
0ade2a2 Finding #6: add status labels + prompt mentions for 6 tools
47893db Finding #5: complete .env.example + document GITHUB_TOKEN
2f02689 Finding #4 (part 1): surface vault-sync failures to the monitor
fe8b666 Finding #3: fix cinematic homepage truncation + completion guard
c7b8e88 Finding #2: suite_taskman — regression tests for Task Manager safety
9a6b36c Finding #1: per-thread Supabase clients to fix worker thread-safety
```

---

## 7. For the next run / your attention

> **UPDATE 2026-07-21 morning: part 2 is already done — no migration needed.** The vault's `.git`
> is a 52-byte pointer file to `~/.second-brain-vault.git` (outside iCloud) and has been since
> 2026-07-19 00:04. The recurring `fatal: error reading .git` failures were iCloud evicting the tiny
> *pointer file* itself, which self-heals when iCloud re-materializes it; part 1's monitor reporting
> now surfaces those windows when they happen. The script below would be a no-op — skip it.

**~~Ready-to-run: move the vault's `.git` out of iCloud (finding #4, part 2).~~** Do this with Obsidian
CLOSED and iCloud idle:
```bash
VAULT="$HOME/Library/Mobile Documents/com~apple~CloudDocs/Obsidian/Second brain"
cp -R "$VAULT/.git" "$HOME/vault-git-backup-$(date +%Y%m%d)"        # 1. back up first
git -C "$VAULT" init --separate-git-dir="$HOME/.vault-git"           # 2. move .git out of iCloud
git -C "$VAULT" status && git -C "$VAULT" remote -v                  # 3. verify
git -C "$VAULT" add -A && git -C "$VAULT" commit -m "migrate git dir out of iCloud" && git -C "$VAULT" push origin main
launchctl kickstart -k gui/$(id -u)/com.secondbrain.vaultsync        # 4. confirm the job still syncs
# rollback if anything looks wrong: remove the pointer .git, mv the backup back to "$VAULT/.git"
```

**Removal candidates (I don't delete files — your call):**
- `sites/inkling-1/` — the duplicate build the audit flagged (identical brief, built 6h after `sites/inkling/`). Keep or remove.
- `synthesized/20260721-overnight-test-note.md` — artifact from tonight's end-to-end job-queue test. Safe to delete.
- Two chat-history messages from tonight's live tests ("streaming works" + the job-queue announcement) sit in your conversation history — harmless.

**No code change, just FYI (from the audit):** a browser tab left open on the HUD polls
`/api/dashboard` once a minute and logs two 401s + a 200 each time — ambient noise from a stale +
live tab, not a defect. Close the stale tab to quiet it.

**Follow-ups / not done:**
- P3.2 is partial: the task↔council link is in; broader silo-linking (notes/reports ↔ the
  conversation that spawned them, weekly review reading from those links) is designed but deferred
  to avoid a rushed multi-store change late in the run.
- `distill_memory` is manual + schedulable but I did not create a launchd job for it (no new
  autonomous jobs per the run's rules). If you want it nightly, add a launchd plist mirroring
  `com.secondbrain.morningbrief.plist` that runs a one-liner calling the tool/distiller.
- Still open from before tonight (unchanged): set the real budget cap; deploy to the server;
  CLARVIS branding pass; review branch `jarvis/tool-get_word_count`; HTTPS. See handoff §5.

*App state at report time: running, PID 83625, `127.0.0.1:5001`, access code enforced, workers
quiet, startup self-check green on required deps. Nothing pushed to any remote; no server/Coolify
changes; the Obsidian vault's content was not touched.*
