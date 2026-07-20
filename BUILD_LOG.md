# BUILD LOG — Obsidian Vault Integration + Security Hardening

Autonomous overnight build. Model: Claude Opus 4.8. All timestamps America/New_York.

---

## Phase 1 — Read & plan — 2026-07-20 (start of session)

**Completed:**
- Read the full project: `second-brain-chat/app.py` (1829 lines), `task_manager.py`,
  the standalone agents, connect scripts, and the vault-sync launchd setup.
- Documented the tool-calling pattern and full integration plan in `RESEARCH_NOTES.md`.
- Captured a byte-for-byte checksum baseline of the read-only Obsidian vault
  (`scratchpad/vault_baseline.sha`) to prove no writes occur.

**Key decisions:**
- There are **two** vaults. The existing `VAULT_PATH` tools point at an agent-*writable*,
  git-synced copy under `com~apple~CloudDocs`. The task's target is the real, **read-only**
  Obsidian vault under `iCloud~md~obsidian/Documents/Second brain`. To keep the read-only
  guarantee airtight, the new search/index tools use a **separate** `OBSIDIAN_VAULT_PATH`
  config and never write. Existing write tools are left untouched.
- The real read-only vault is nearly empty (default `Welcome.md` only), so a project
  `sample_vault/` of realistic notes is created for end-to-end testing; `OBSIDIAN_VAULT_PATH`
  defaults to the real vault and can be pointed at `sample_vault` via env var.

**Findings:** No hardcoded secrets in code (all `os.environ`); git history clean; but the
login gate is disabled and Flask binds `0.0.0.0` with `debug=True` — both fixed in Phase 2.

**Known issues at this point:** none blocking.

---

## Phase 2 — Security hardening — 2026-07-20

**Completed & verified:**
- Created project-root `.env` (mode 600, gitignored) with the 4 API keys (copied from the
  live environment), a generated 24-char `ACCESS_CODE`, and a stable `FLASK_SECRET_KEY`.
  Added `.env.example` and `python-dotenv` to both `requirements.txt`.
- Wired `load_dotenv(<project-root>/.env)` into: `app.py`, `task_manager.py`,
  `money_clips_agent.py`, `morning_brief_agent.py`, `agents/stock_watch_agent.py`,
  `scripts/connect_gmail.py`, `scripts/connect_google_calendar.py`. Each falls back to the
  ambient env if `.env` is missing.
- Network: `app.run` now binds **127.0.0.1** with **debug=False** by default (both
  env-overridable via HOST / FLASK_DEBUG); default port 5001.
- Access gate: rewired login to canonical `ACCESS_CODE` (legacy `JARVIS_PASSWORD` alias
  kept). Unauthed browser → /login; unauthed POST//api/* → 401.
- Wrote `SECURITY_NOTES.md` (findings, fixes, residual items, key-rotation guidance,
  prompt-injection open risk).

**Tests run:**
- With all 4 vars removed from the ambient shell: `app.py` imports cleanly from `.env`;
  Supabase read + Anthropic auth check both succeed.
- All 7 modified scripts byte-compile.
- Git history scan (all 25 commits) for key patterns AND the first-12-chars of each live
  secret → **CLEAN**. `.env` untracked/unstaged.
- Live app: bound to `127.0.0.1:5001` only; the LAN IP (10.0.0.132:5001) is **refused**;
  `/api/history` unauthed → 401, `/` unauthed → 302→/login; correct `ACCESS_CODE` logs in
  and unlocks `/api/history` (returns real Supabase chat history — existing feature intact).

**Decisions/pivots:**
- Found **stale old app processes** still bound to `0.0.0.0:5001` (the pre-hardening
  version) from a prior session — they answered on the LAN. Killed all app instances and
  restarted a single hardened one so the insecure binding is actually gone.
- Left `~/.zshrc` untouched (out of project scope); documented in SECURITY_NOTES that its
  key exports are now redundant and can be removed by Alex if he wants a single source.

**Known issues:** none. Key rotation is optional (nothing found leaked).

---

## Phase 3 — Vault indexer — 2026-07-20

**Completed & tested:**
- New module `second-brain-chat/vault_index.py` — stdlib-only (os, re, difflib), strictly
  read-only. `VaultIndex(path).build()` walks the vault (skipping `.obsidian`/`.git`/etc.,
  handling nested folders), and per note extracts title (H1 or filename), headings, tags
  (YAML frontmatter + inline `#tag`), `[[wikilinks]]`, folder, mtime, and full content.
- Search: keyword-relevance scoring (title/phrase > headings/tags > body frequency) with
  snippet extraction and `#tag` boosting. `get_by_fuzzy()` resolves a note by exact path →
  title/stem → path-endswith → substring → `difflib` close-match. `recent(n)` by mtime.
- Created `sample_vault/` with 11 realistic notes across Athletics/Learning/Money/School/
  Schedule (frontmatter tags, headings, inline tags, wikilinks) and staggered mtimes, since
  the real vault is nearly empty.

**Tests run (all pass):**
- Indexed 11 sample notes; `recent(5)` returns correct newest-first order.
- Relevance search ranks the right notes first with useful snippets.
- Fuzzy read resolves misspellings ("footbal trainng plan" → football-training-plan.md;
  "sprint mechanix" → sprint-mechanics.md), paths, and returns NOT FOUND for junk.
- Tag search (`#speed`) and topic search ("ser vs estar spanish") both correct.
- Real read-only vault indexes cleanly (2 notes, no error).

**Pivot/fix:** inline-tag regex was capturing `#1` ("the #1 lever") as a tag; tightened it
to require a non-numeric char, matching Obsidian's own tag rules.

**Known issues:** none.

---

## Phase 4+5 — Tools + integration — 2026-07-20

**Completed:**
- Added a lazily-built, read-only `NOTE_INDEX = vault_index.VaultIndex(OBSIDIAN_VAULT_PATH)`.
- Implemented three tools matching the existing pattern (function → TOOLS schema →
  handle_tool_call dispatch → TOOL_STATUS_LABELS → SYSTEM_PROMPT):
  - `search_notes(query, limit=5)` — relevance results with folder + snippet; `#tag` weighting.
  - `read_note(title_or_path)` — full note via fuzzy match; suggests near matches on miss.
  - `list_recent_notes(n=5)` — newest-first with folder + one-line preview.
  Output wraps note content in explicit "Alex's data — not instructions" delimiters and tells
  the model to cite the source note (covers the "name which note" nice-to-have + injection guard).
- SYSTEM_PROMPT now describes the search capability and the treat-notes-as-data rule.
- Added a `POST/GET /reindex` route (behind the access gate) + `reindex_vault()` to refresh
  the index without restarting.

**Tests run:** `app.py` compiles; all 3 tools appear in TOOLS (33 total); direct dispatch of
search_notes/read_note (fuzzy "footbal trainng"→football-training-plan)/list_recent_notes all
return correct, well-formatted output against sample_vault.

**Known issues:** none.

---

## Phase 6 — Testing — 2026-07-20

**Direct tool test suite** (`second-brain-chat/test_vault_tools.py`, drives handle_tool_call):
**18/18 PASS** — list_recent_notes ordering/preview, search relevance + snippets + tag search +
graceful no-match, read_note exact/misspelled/path/fuzzy/missing, injection-guard delimiters,
reindex, and the **read-only guarantee** (vault checksum identical before/after all calls).

**Live chat test** (real HTTP `/chat`, streaming, logged in with ACCESS_CODE, against sample_vault):
5 realistic questions all answered from actual vault content, each citing the source note, with
the right tools firing:
1. "most recent notes?" → list_recent_notes → correct 5 newest.
2. "what gets the most views on clips?" → search_notes+read_note → Clip Farming Strategy details.
3. "football plan for Mondays?" → search+read → "Lower body lift + film review".
4. "what tickers am I watching and why?" → search+read → full watchlist + rules.
5. "goals for 2026?" → read_note → all four goal areas.

**Security/infra verification (live):**
- Bound to 127.0.0.1:5001 only; LAN IP refused (curl → connection refused).
- `/` unauth → 302→/login; `/api/*` unauth → 401; correct ACCESS_CODE logs in and unlocks API.
- `/reindex` works against both sample_vault (11) and the real vault (2).
- Real vault **byte-for-byte unchanged** vs the baseline checksum, verified twice (after direct
  tests and after all live tests).
- Existing features intact: `log_note` (Supabase insert) and `get_recent_agent_outputs`
  (Supabase read) both work; `/api/history` returns real chat history.
- Chat history cleared at the end so Alex wakes to a clean slate.

**Decisions:** running instance defaults to the **real** vault (honest production default), which
is nearly empty today — the rich demo above used `sample_vault` via the env override.

**Known issues:** none functional. Minor model-behavior quirk observed (occasional over-cautious
"let me re-verify" phrasing) — cosmetic, not a bug.

---

## Phase 7 — Polish + docs — 2026-07-20

- Created project **`README.md`** (quick start, `.env` config table, full vault-search section,
  re-index instructions, the two-vaults note, sample-vault demo, and security summary).
- This BUILD_LOG finalized. RESEARCH_NOTES.md and SECURITY_NOTES.md complete.

### SUCCESS CRITERIA — final status
- [x] Flask app starts with no errors on localhost:5001 (127.0.0.1, debug off).
- [x] "most recent notes?" returns real vault notes.
- [x] Topic question returns an answer built from actual vault content (5/5 live).
- [x] Read-by-name works with imperfect spelling (fuzzy match verified).
- [x] Vault byte-for-byte unchanged (baseline vs final checksum identical).
- [x] Existing agent-output lookup + Supabase note logging still work.
- [x] No secrets in code or git history; all in gitignored `.env`.
- [x] App localhost-only + rejects requests without the access code.
- [x] SECURITY_NOTES.md, BUILD_LOG.md, RESEARCH_NOTES.md complete.

**Build complete.** The hardened app is running on http://127.0.0.1:5001.

---

# OVERNIGHT BUILD — ROUND 2 — 2026-07-20

New scope: (1) video input for the chat brain, (2) data synthesizer agent, (3) website
creator agent, (4) video maker/editor toolkit. All built inside the project; vault stays
read-only; app stays on 127.0.0.1 behind the access code; no schema/secrets changes.

## Setup — toolchain
- Installed via brew: **ffmpeg 8.1.2** (+ ffprobe) and **whisper-cpp** (whisper-cli).
- Downloaded **ggml-base.en** Whisper model (141 MB) to `models/ggml-base.en.bin`;
  verified against the bundled jfk.wav — transcription accurate.
- pip: **ddgs** (keyless DuckDuckGo search), **beautifulsoup4**, **lxml**.
- Decision: use **whisper.cpp**, NOT openai-whisper/faster-whisper. Python here is 3.14 and
  torch has no stable 3.14 wheels — whisper.cpp is a native binary with no Python deps, so
  it's robust and fast. No cloud, no API key for transcription.
- New dirs: `inbox/`, `synthesized/`, `sites/`, `media_lib/`, `video_work/`, `models/`, `_archive/`.

## Phase 1 — Video input for the chat brain — DONE ✅
- New module `second-brain-chat/video_processor.py`:
  - `probe_video` (ffprobe: duration, audio presence, validity),
  - `sample_frames` (ffmpeg scene-change detection merged with evenly-spaced sampling so a
    static clip still yields frames; downscaled to 768px; capped at 8, max 16),
  - `transcribe_audio` (ffmpeg → 16 kHz mono wav → whisper-cli; caps transcription at 15 min),
  - `analyze_video` (assembles frames-as-images + transcript + instruction and calls Claude
    vision with the existing Anthropic pattern; returns text for the chat tool loop).
  - Path containment: only reads files inside the project (inbox/); rejects traversal.
- Wired into `app.py`: new **`analyze_video`** tool (schema + handle_tool_call routing +
  status label + system-prompt paragraph), plus **`/api/upload_video`** endpoint
  (500 MB cap, extension whitelist, secure_filename, no-clobber).
- Chat UI (`index.html`): 📎 attach button + hidden file input + attachment chip; uploads to
  inbox/ then augments the sent message so Claude calls analyze_video on it.
- **Tested end-to-end:** generated a 12 s test clip (red→green→blue scenes + macOS `say`
  narration). Via HTTP through the live app (login → upload → chat): tool fired, reply
  correctly gave color order AND quoted the narrated purpose. Edge cases verified:
  no-audio clip (visual-only note), unsupported .txt (clean error), missing file (clean error).
- Known limits: frames are samples not every moment; very long videos capped at 15 min of
  audio transcription (noted in output). base.en model is English-tuned.

## Phase 2 — Data synthesizer agent — DONE ✅
- New agent `data_synthesizer_agent.py` (project root, follows money_clips_agent pattern):
  - Modes: **web research** (keyless `ddgs` DuckDuckGo → fetch pages → bs4 text extraction)
    and **organize raw material** you paste; `auto` picks based on whether material is given.
  - Output: one structured markdown report (executive summary, thematic `##` sections
    synthesized across sources, inline `[n]` citations, `## Sources` list with URLs). A
    machine-appended Sources block guarantees traceability even if the model omits it.
  - Saved to `synthesized/<date>-<slug>.md` (no-clobber) AND logged to Supabase "Agent Outputs".
  - `search_web()` is provider-pluggable: if TAVILY/SERPER/BRAVE_API_KEY is set it uses that
    (branches stubbed with exact request shapes), else keyless DDG. Never scrapes Google.
  - CLI: `python3 data_synthesizer_agent.py "topic"` / `--text ...` / `--stdin` / `--web`.
- Wired into `app.py` as the **`synthesize_data`** tool (schema + routing + status label +
  system prompt); reuses the app's Claude + Supabase clients.
- **Tested end-to-end:** (1) web research on "creatine monohydrate benefits and dosing" — 5 live
  sources fetched, cited, saved (8.5 KB), Supabase row confirmed. (2) Through the live chat:
  "cold water immersion for recovery" — tool fired, report saved, chat gave highlights + file
  path. (3) Raw-material mode on standup notes — organized cleanly, 0 web sources.
- Known limits: keyless DDG is rate-limited / lower recall than a paid API (drop-in upgrade
  path documented); page extraction is best-effort (paywalls/JS/PDF may yield little).

## Phase 4 — Video maker/editor toolkit — DONE ✅
- New `video_toolkit.py` (project root) — ffmpeg-backed primitives, each returns an output path
  in `media_lib/`: `probe`, `trim`, `concat` (auto-normalizes mixed sizes/fps + handles clips
  with/without audio via silent-track padding), `caption`, `set_audio` (replace or mix),
  `to_vertical` (9:16 1080x1920, crop or pad), `thumbnail`. Full CLI with subcommands.
- **Captions:** this brew ffmpeg has NO drawtext/subtitles/libass filter (verified). Pivot:
  render caption text to a transparent PNG with **Pillow** (word-wrapped, bold, stroked, sized to
  video width) and `overlay` it — full styling control, robust. Installed Pillow.
- NL wrapper: `run_operation(operation, **params)` dispatches one edit and returns a friendly
  string with the output filename so the chat can chain steps.
- AI generation stub: `video_gen_stub.py` — clearly-marked NotImplementedError stubs documenting
  V2 (provider options Runway/Luma/Veo/Pika/Kling/Stability + which env key, submit→poll→fetch
  interface, the dashboard-approval spend gate, and how it slots into money_clips_agent → toolkit
  to assemble Shorts). No network calls.
- Wired into `app.py` as the **`edit_video`** tool (operation enum + params) + system prompt.
- **Tested:** generated test clips; ran all 7 ops via CLI and verified outputs with ffprobe —
  trim=4.0s, vertical=1080x1920, concat=15s (normalized from mixed 640x480+480x480), caption text
  pixels confirmed present, audio replace=6s (‑shortest), audio add=full length mix, thumbnail JPG.
  Through the live chat: "trim to first 5s then caption 'Color test clip'" → chained two edit_video
  calls → final file 5.0s with caption verified present.
- Known limits: captions use macOS system fonts (Arial/Helvetica fallback); no libass styling;
  AI generation is V2 (stubbed).

## Phase 3 — Website creator agent — DONE ✅
- New `website_creator_agent.py` (project root). Staged single-agent pipeline:
  1. **Plan+design** — forced structured-JSON (Anthropic tool-forcing, so no fragile text/quote
     parsing) → site name/slug, 3-5 pages, and a real design system (characterful Google Font
     pairing, considered palette, radius/shadow, vibe keywords).
  2. **Stylesheet** — one hand-tuned styles.css (CSS variables, fluid clamp type, flex/grid,
     components: nav+mobile hamburger, buttons, hero, cards, sections, footer, forms).
  3. **Pages** — one call per page, real on-brand copy (no lorem ipsum), reusing the shared classes.
  4. **Self-review polish** — an APPENDED override layer (never a destructive rewrite).
  5. **Coverage guard** — any class a page uses but the sheet never defined gets auto-filled.
  Writes sites/<slug>/ with pages, styles.css, main.js (mobile nav), `serve.sh` (one-command
  preview on **:8080**, deliberately not 5001), and a per-site README. Logs a summary to Supabase.
- Wired into `app.py` as the **`create_website`** tool (+ system prompt).
- **Two bugs found and fixed during testing:**
  1. The self-review originally REWROTE styles.css and dropped .hero/.btn/.card definitions →
     pages rendered unstyled. Fixed: made self-review additive (append-only polish layer) + added
     the coverage guard that fills any missing class rules. Verified: 0 used-but-undefined classes.
  2. The self-review call returned EMPTY — `claude-sonnet-5` spent the whole 2500-token budget on
     automatic thinking (2128 thinking tokens) before emitting CSS, hitting max_tokens. Fixed by
     raising that call's budget to 6000 so thinking + the CSS both fit. Also hardened fence-stripping.
- **Tested end-to-end:** (1) "Tidewater Rowing Club" (4 pages) direct — coherent nautical design
  (Fraunces/Public Sans, teal/sand/copper), all pages serve 200, real copy, all classes styled.
  (2) Through the live chat: "Ember & Oak" wood-fired pizza (3 pages) — built, self-review polish
  layer present, all classes styled, serves 200, logged to Supabase. Archived the first (pre-fix)
  broken build to _archive/.
- Known limits: Google Fonts load from the network in-browser (rest is self-contained); ~2-3 min
  per site (several sequential model passes); no image assets generated (design is type/color/layout).

## Final verification — 2026-07-20
- **All 4 priorities working end-to-end through the live chat** (login → tool → result), verified
  over HTTP against the running app on 127.0.0.1:5001.
- **Regression — prior functionality intact:**
  - Vault tools test suite: **18 passed, 0 failed** (incl. read-only byte-for-byte guarantee).
  - Supabase agent-output lookup via chat: works (listed the synthesizer's reports).
  - Access gate: `/api/*` unauth → 401, `/` → 302→login, wrong code rejected.
  - Localhost-only: LAN IP (10.0.0.132:5001) refused connection; bound to 127.0.0.1.
- App log clean (no tracebacks/errors across all testing).
- New deps added to requirements.txt (pip: ddgs, beautifulsoup4, lxml, Pillow; system/brew:
  ffmpeg, whisper-cpp + ggml-base.en model). `.gitignore` updated to keep the 141 MB model,
  temp dirs, and test media out of any commit. README updated with a Round-2 capabilities section.
- Housekeeping: test chat history cleared (clean slate); archived one duplicate site build and
  the pre-fix broken site to `_archive/`. Nothing committed to git.
- **Known minor issue:** the chat model occasionally invokes create_website twice for one request
  (two valid sites built). Benign but wastes a build; noted for a future idempotency guard.

**Round 2 build complete.** App running on http://127.0.0.1:5001 with all new tools live.

---

# OVERNIGHT BUILD — ROUND 3 — 2026-07-20 (late morning session)

Scope: (1) fixes & polish + a single regression suite, (2) a Feasibility Judge for the
decision council, (3) a clean readable/mobile home dashboard, (4) a task-tracker
scaffold (bookkeeping only, never autonomous), (5) a full-system smoothing pass.
All inside the project; vault stays read-only; app stays on 127.0.0.1 behind the access
code; no Supabase schema changes; nothing executes tasks autonomously.

## Priority 1 — Fixes, polish & the regression suite — DONE ✅
- **create_website idempotency guard** (`website_creator_agent.py`): a module lock
  serializes builds and a 5-min TTL cache keyed by the normalized brief makes one request
  produce exactly one build. The duplicate second tool-call the model sometimes emits now
  returns the first build's result instantly with a "reused that build" note — no second
  site dir. Empty briefs are rejected cleanly. Verified by the suite (build-count == 1 on a
  duplicate call; a different brief still builds).
- **Error handling / feedback sweep:** create_website failures now say nothing-was-saved +
  what to do; the chat UI (`index.html`) now handles a non-OK HTTP response (401 → bounce to
  /login with a message; other codes → restore the message to the box to retry) instead of
  silently reading an error body as a stream. Existing per-tool try/except friendly messages
  (edit_video, analyze_video, synthesize) confirmed. Loading feedback already present via the
  live tool-status pulse; left intact.
- **`run_tests.py`** (project root, ONE command = the regression bar): offline by default
  (fast, free, deterministic — fakes/stubs stand in for anything that would hit Claude or the
  web), `--live` adds the real model/network paths, `--only a,b` runs named suites. Covers:
  vault tools (+read-only byte-for-byte guarantee), the access gate (Flask test client:
  unauth redirect/401, wrong vs right code), video toolkit (trim/vertical/thumbnail/caption/
  concat via ffmpeg), the video pipeline's local stages (probe/frame-sample/transcribe),
  the data synthesizer (offline organize mode via a fake client + graceful zero-source web),
  the website idempotency guard, the feasibility judge (shape offline, 3-idea differentiation
  live), the task tracker (full CRUD + history + persistence), and the security invariants
  (no live secret value in any .py, 127.0.0.1 default + debug off, .env gitignored/untracked).
  **Result: 49 passed, 0 failed offline.** README documents how to run it.
- **Fix the suite revealed:** video-toolkit fixtures must live inside the project (the toolkit
  refuses out-of-project paths) — moved test clips under media_lib/ with cleanup. Also made
  the suite flush before its hard exit (background daemon threads).

## Priority 2 — Feasibility Judge (council's third member) — DONE ✅
- Added `feasibility_judge()` + `assess_feasibility()` in `app.py`, following the exact
  `_council_call` pattern. It answers a different question than the Advocate/Critic: CAN this
  actually work, and how likely to work as intended? Fixed headings: Plausibility (N/10 +
  unlikely/possible/likely), Technical feasibility, Resource realism (framed for a solo
  college student), Causal chain + weakest link, Most likely failure mode, What would raise
  the rating. Prompt explicitly separates "impossible" from "hard" and licenses a plain
  "this won't work."
- Wired into the council: `deliberate()` now runs Advocate + Critic + **Feasibility Judge**,
  and the final Judge sees the feasibility read too. Output shows all four sections.
- Also standalone: new `assess_feasibility` chat tool ("is this idea feasible: …") returns just
  the calibrated read. Both are logged to Supabase (agent_name "council") for the dashboard.
- **Tested (live), 3 ideas of different plausibility:** budgeting spreadsheet → **9/10 likely**;
  YouTube to 10k in a year → **4/10 unlikely-but-possible** (named the weak links); FTL radio
  in a dorm → **0/10** (correctly grounded in physics/relativity, not "just hard"). Ratings and
  reasoning meaningfully differ. Through live chat, "is this feasible: juggle in a week" → 8/10.

## Priority 3 — Home dashboard (/dashboard) — DONE ✅
- New `templates/home.html`: a clean, fast, **mobile-friendly** command deck matching the chat
  app's aesthetic (same dark-navy palette, cyan accent, mono headers). Responsive auto-fill grid,
  30-second auto-refresh + a manual Refresh button, and a graceful empty-state (with a helpful
  hint) for every panel — designed for the sparse reality, not fake fullness.
- Panels: **Tasks**, **Council Decisions** (pros/cons/feasibility summaries), **Recent Agent
  Activity**, **Recent Vault Notes** (from the read-only Obsidian index), **Synthesized Reports**,
  **Built Sites** (each links to a live preview). **Quick actions:** Open Chat, Run Synthesizer,
  Build Website, New Task — the three build actions deep-link to the chat with a pre-filled (not
  auto-sent) prompt via `?q=`, which `index.html` now reads.
- New backend: `/api/home` (`get_home_data()` — every panel independently fail-safe), plus
  `get_home_agent_outputs / get_recent_council / get_recent_vault_notes / get_recent_reports /
  get_recent_sites`. New `/preview/<slug>/<page>` route serves a built site read-only, behind the
  gate, path-contained to sites/<slug>/ (traversal → 404, verified).
- **Preserved the old sci-fi HUD** at **/hud** (nothing deleted); `/dashboard` now serves the
  new readable home base; `/` stays the chat.
- **Tested (live over HTTP, logged in):** `/dashboard` 200, `/api/home` returns real data
  (agent outputs, 3 reports, 3 sites) with empty council/tasks that then populate as used;
  preview index+styles 200, traversal 404; the old `/api/dashboard` HUD still 200.

## Priority 4 — Task Tracker scaffold (structure only) — DONE ✅
- New `second-brain-chat/task_tracker.py` — **local SQLite** (no Supabase tables, no network),
  distinct from the autonomous `task_manager.py`. Model: task = {id, title, description, status,
  created_at, updated_at, history[]}; pipeline idea → evaluating → approved → in_progress →
  done/dropped; append-only history of status changes + notes. Thread-safe (one guarded
  connection). **Nothing here executes a task** — pure bookkeeping.
- Chat tools wired into `app.py`: `create_task`, `update_task_status`, `list_tasks`,
  `show_task_history`, and `evaluate_task` (optional wiring: sends a task to the council, sets it
  to 'evaluating', and attaches the verdict + feasibility rating to the task's history). Plus the
  Tasks panel on the dashboard. System prompt describes it and stresses it's bookkeeping only.
- DB gitignored.
- **Tested end-to-end through chat:** "create a task…" → #1 (idea); "move task 1 to in progress"
  → in_progress; both reflected on `/api/home` immediately. CRUD/history/persistence covered by
  the suite (8/8).

## Priority 5 — Full-system smoothing pass — DONE ✅
Used the system as a user (chat + dashboard) and fixed the rough edges found:
- **Duplicate agent-activity rows** (old pre-idempotency double builds showed twice) — the
  home dashboard now collapses repeat agent+result rows so activity reads clean.
- **Inconsistent timestamps** — the dashboard mixed raw ISO strings (agent/council rows) with
  relative times (file panels). Added `_humanize_iso` so every panel reads "27m ago" / "1h ago".
- **Chat error on a non-OK HTTP response** — `index.html` previously tried to stream-parse an
  error body; now it handles it: 401 → bounce to /login with a message, other codes → restore
  the message to the box to retry.
- **`/api/dashboard` (HUD) 500 on a transient upstream read error** — a startup Composio/Supabase
  read race surfaced as an HTML 500. Both `/api/dashboard` and `/api/home` now catch it and return
  clean JSON (503) so the front-end just retries on its refresh loop. (Endpoints verified reliably
  200 across repeated requests afterward.)
- **Website-build failure messaging** — now states nothing was saved + what to do next.
- **Feasibility offline test side-effect** — the offline suite was writing "council" rows to
  production Supabase via the logger; stubbed it so `run_tests.py` (offline) is side-effect-free.

## Round 3 — final verification — 2026-07-20
- **`run_tests.py`: 49 passed / 0 failed offline; 56 / 0 with `--live`** (real web synthesis, a
  real small website build, real video-vision analysis, and the 3-idea feasibility differentiation
  9 > 4 > 1 all pass).
- **Feasibility judge live-differentiates** (solo-budget spreadsheet 9/10 · YouTube-to-10k 4/10 ·
  FTL radio 0-1/10, grounded in physics not "hard"); wired into the council (Advocate/Critic/
  **Feasibility**/Judge) and standalone. Verified end-to-end through chat, including `evaluate_task`
  attaching the verdict to a tracked task.
- **Dashboard**: `/dashboard` (new home base) + `/hud` (preserved HUD) + `/preview/<slug>/`
  (site previews, traversal-blocked). `/api/home` returns real data with graceful empty states;
  auto-refresh; mobile grid. No Composio dependency (faster + can't 500 on calendar).
- **Task tracker**: SQLite CRUD + history + council evaluation, all through chat and on the
  dashboard. Nothing autonomous.
- **Security invariants intact**: bound to 127.0.0.1 only (LAN 192.168.7.27 refused); gate
  enforced (/ →302, /api/* →401); no live secret in any `.py`; `.env` gitignored/untracked;
  the read-only Obsidian vault is never written (suite's byte-for-byte guarantee passes).
- **Regression — prior functionality intact**: vault tools, synthesizer, website agent, video
  toolkit + pipeline all pass; existing tools untouched.
- **Housekeeping**: test chat history cleared; junk test task removed (kept one illustrative
  evaluated task); no stray test media; task DB gitignored; README updated (Round-3 section +
  a Testing section); nothing committed to git.

**Round 3 build complete.** App running on http://127.0.0.1:5001 — new home dashboard at
/dashboard, the council now has its Feasibility Judge, and the task tracker is live (bookkeeping only).
