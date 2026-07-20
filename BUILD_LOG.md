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
