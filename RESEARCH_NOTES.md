# RESEARCH NOTES — Obsidian Vault Integration + Security Hardening

_Overnight autonomous build. Author: Claude (Opus 4.8). Started 2026-07-20._

## 1. Project overview

`second-brain-chat` is a Flask chat app (the "chat brain" of a larger Jarvis-style
system) that talks to the Claude API with tool access. It runs locally on port 5001
(port 5000 is taken by macOS AirPlay). State lives in Supabase (table `Agent Outputs`).

Key files:
- `second-brain-chat/app.py` (1829 lines) — the Flask app + Claude tool-use loop.
- `second-brain-chat/task_manager.py` — a separate module of background/managed-task tools, imported by app.py.
- `money_clips_agent.py`, `morning_brief_agent.py`, `agents/stock_watch_agent.py` — standalone cron/launchd agents.
- `scripts/connect_gmail.py`, `scripts/connect_google_calendar.py` — one-time Composio OAuth setup.
- `scripts/vault_sync.sh` + launchd plist — auto-commits a *separate* git-synced vault.

## 2. The tool-calling pattern (must match this exactly)

Adding a capability touches exactly these places (documented in app.py's own header):

1. **`TOOLS` list** (app.py ~line 241) — a list of Anthropic tool schemas, each
   `{"name", "description", "input_schema": {"type":"object","properties":{…},"required":[…]}}`.
   The list literal ends with `] + CALENDAR_TOOLS + GMAIL_TOOLS` (line 559). Composio
   calendar/gmail tool schemas are concatenated; extension tools `.append()` later;
   `task_manager.TOOL_SCHEMAS` is `.extend()`ed at line 1735.
2. **A Python function** implementing the tool (returns a `str` — the tool result).
3. **`handle_tool_call(tool_name, tool_input)`** (line 1449) — a chain of
   `if tool_name == "...": return fn(...)` dispatches. Unknown → `f"Unknown tool: {tool_name}"`.
4. **`TOOL_STATUS_LABELS`** dict (line 1541) — a human-friendly "…doing X" string shown live in the UI.
5. Optionally, a mention in **`SYSTEM_PROMPT`** (line 162) so the model knows the capability exists.

The chat loop `stream_chat()` (line 1575) streams from `claude.messages.stream(model="claude-sonnet-5", tools=TOOLS, …)`,
and on `stop_reason == "tool_use"` runs each tool via `handle_tool_call`, appends `tool_result`
blocks, and loops. Tool functions must return strings.

## 3. Existing vault tools (important nuance — TWO vaults)

app.py already has `list_vault_notes`, `read_vault_note`, `write_vault_note`, driven by:
```
VAULT_PATH = os.environ.get("VAULT_PATH",
    "~/Library/Mobile Documents/com~apple~CloudDocs/Obsidian/Second brain")
VAULT_FOLDERS = ["Schedule", "Learning", "Money", "School", "Athletics"]
```
This `VAULT_PATH` points at a **git-synced, agent-WRITABLE** copy of the vault under
`com~apple~CloudDocs` (2 notes, written by morning_brief_agent etc., pushed to
`second-brain-vault` on GitHub). `write_vault_note` writes here.

**The vault this task targets is different:** the real Obsidian app vault at
`~/Library/Mobile Documents/iCloud~md~obsidian/Documents/Second brain`, which the
directive says is **strictly read-only**. It currently contains only the default
Obsidian `Welcome.md` + an empty `Untitled.md` (folders Athletics/Learning/Money/
Schedule/School exist but are empty).

### Decision
To honor the read-only guarantee AND avoid disturbing existing write tools, the new
search/index tools get their **own separate, read-only config**:
```
OBSIDIAN_VAULT_PATH = os.environ.get("OBSIDIAN_VAULT_PATH", <the iCloud~md~obsidian path>)
```
The three new tools (`search_notes`, `read_note`, `list_recent_notes`) **only read**.
The existing `VAULT_PATH` write tools are left untouched.

Because the real read-only vault is nearly empty, a `sample_vault/` (~10 realistic
notes) is created inside the project for end-to-end testing. `OBSIDIAN_VAULT_PATH`
defaults to the real vault; tests point it at `sample_vault` via the env var. This is
called out prominently in the final report so Alex can switch it to see a rich demo.

## 4. Security findings (pre-fix)

- **No hardcoded secrets in any project code file** — app.py, task_manager.py, and all
  agents already read `CLAUDE_API_KEY / SUPABASE_URL / SUPABASE_KEY / COMPOSIO_API_KEY`
  via `os.environ`. The secrets currently live in `~/.zshrc` (the launchd plists
  `source ~/.zshrc`). `~/.zshrc` is outside the project and not committed.
- **Git history is clean** — `git log --all -p` grep for key patterns found only a link
  to `supabase.com/dashboard`, no actual credentials. 25 commits.
- **`.gitignore`** already lists `.env`, `__pycache__/`, `*.pyc`, `scripts/*.log`. Good.
- **Login gate currently DISABLED** — the app's `require_login` only activates if
  `JARVIS_PASSWORD` is set; it is not set, so the app is fully open. **Gap.**
- **Network binding is `0.0.0.0` with `debug=True`** (`app.run(host="0.0.0.0", …, debug=True)`,
  line ~1828) — reachable from the LAN and running the Werkzeug debugger. **Gap.**

### Security plan (Phase 2)
1. Create project `.env` (gitignored) with the 4 secrets (copied from the current
   environment), a generated `ACCESS_CODE`, and a generated `FLASK_SECRET_KEY`. Add `.env.example`.
2. Add `python-dotenv` + `load_dotenv()` to app.py, task_manager.py, all 3 agents, and
   the 2 connect scripts, so the project is self-contained (no longer depends on `~/.zshrc`).
3. Bind Flask to `127.0.0.1`, `debug=False` by default (both env-overridable), default port 5001.
4. Wire an `ACCESS_CODE` gate: canonical env `ACCESS_CODE` (fall back to legacy
   `JARVIS_PASSWORD`). Requests without a valid session are rejected. Generate a random default.
5. Write `SECURITY_NOTES.md` — findings, fixes, residual risk (incl. prompt-injection
   from vault notes), prioritized action list (rotate keys is precautionary only —
   history is clean, so not mandatory).

## 5. Indexer design (Phase 3)

New module `second-brain-chat/vault_index.py`:
- `walk` the vault, skip `.obsidian/` and non-`.md` files, handle nested folders.
- Per note extract: `path` (rel), `folder`, `title` (H1 or filename), `headings`,
  `tags` (`#tag` + YAML frontmatter `tags:`), `links` (`[[wikilinks]]`), full `content`, `mtime`.
- Keep an in-memory list; `build()`/`reindex()` rebuilds it. Cheap for a personal vault.
- Relevance search: keyword scoring — title match > heading/tag match > body frequency,
  with a matching snippet around the best hit. No external services / API keys.
- `read_note` fuzzy match: exact path → exact title → case-insensitive → `difflib` close match.

## 6. Success criteria checklist (tracked to completion in BUILD_LOG.md)

- [ ] App starts clean on 127.0.0.1:5001, debug off.
- [ ] "recent notes" / topic questions / read-by-name (fuzzy) all return real vault content.
- [ ] Vault byte-for-byte unchanged (baseline sha captured in scratchpad `vault_baseline.sha`).
- [ ] Existing agent-output + Supabase note logging still work.
- [ ] No secrets in code or git history; all in gitignored `.env`.
- [ ] App localhost-only + rejects requests without the access code.
- [ ] SECURITY_NOTES.md, BUILD_LOG.md, RESEARCH_NOTES.md complete.
