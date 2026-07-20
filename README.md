# Second Brain

A personal, Jarvis-style AI system. The centerpiece is **`second-brain-chat`**, a local
Flask app that talks to the Claude API with tool access, backed by Supabase. Around it sit
standalone specialist agents (morning brief, money/clips ideas, stock watch).

## Quick start

```bash
# 1. Install deps (framework Python 3.14 is what the launchd agents use)
pip install -r requirements.txt
pip install -r second-brain-chat/requirements.txt

# 2. Configure secrets — copy the example and fill in real values
cp .env.example .env        # then edit .env

# 3. Run the chat app (localhost only, port 5001)
cd second-brain-chat && python3 app.py
# open http://127.0.0.1:5001  and enter your ACCESS_CODE
```

All configuration and secrets live in the gitignored **`.env`** at the project root
(loaded automatically via `python-dotenv`). See `.env.example` for every variable.

## Configuration (.env)

| Variable | What it does |
|----------|--------------|
| `CLAUDE_API_KEY`, `SUPABASE_URL`, `SUPABASE_KEY`, `COMPOSIO_API_KEY` | API credentials |
| `ACCESS_CODE` | Passphrase the chat UI requires (once per browser). Blank = open access. |
| `FLASK_SECRET_KEY` | Session signing key (keep stable so logins survive restarts) |
| `OBSIDIAN_VAULT_PATH` | The **read-only** Obsidian vault the note-search tools index |
| `PORT` (5001), `HOST` (127.0.0.1), `FLASK_DEBUG` (0) | Runtime — safe defaults |

## Obsidian vault search (note tools)

The chat brain can answer questions grounded in your Obsidian notes. It indexes the vault at
`OBSIDIAN_VAULT_PATH` (**strictly read-only** — these tools never write to your vault) and
exposes three tools to Claude:

- **`search_notes(query, limit)`** — keyword-relevance search across all notes (nested folders
  included), returning the best matches with a snippet and the note's folder. Prefix a word with
  `#` to weight it as a tag (e.g. `#money`).
- **`read_note(title_or_path)`** — full content of one note, resolved by fuzzy title/path match
  (tolerates misspellings, a missing folder, or a missing `.md`).
- **`list_recent_notes(n)`** — the most recently modified notes, each with a one-line preview.

Ask things like *"what do my notes say about clip farming?"*, *"read my football training plan"*,
or *"what are my most recent notes?"* The assistant names the note each answer came from.

**Re-indexing:** the index builds on first use and can be refreshed without restarting via
`GET/POST http://127.0.0.1:5001/reindex` (behind the access gate). It returns the note count.

The indexer is a standalone, dependency-free module (`second-brain-chat/vault_index.py`) and can
be run directly:

```bash
python3 second-brain-chat/vault_index.py "/path/to/vault" "search terms"
```

**Note on two vaults:** the older `list_vault_notes`/`read_vault_note`/`write_vault_note` tools
operate on a separate, agent-*writable* git-synced vault (`VAULT_PATH`). The new search tools
target your real, read-only Obsidian app vault (`OBSIDIAN_VAULT_PATH`). They are intentionally
distinct so nothing can write to the read-only vault.

### Trying it with sample notes

The repo ships a `sample_vault/` of ~11 realistic notes for demos/tests. To point the chat brain
at it instead of your real vault, set in `.env`:

```
OBSIDIAN_VAULT_PATH=/Users/alexhickey24/second-brain/sample_vault
```

Run the tool test suite against it:

```bash
cd second-brain-chat
OBSIDIAN_VAULT_PATH=../sample_vault python3 test_vault_tools.py
```

## Adding a new tool/capability

The app is built to extend by touching one place per concern (see the header comment in
`app.py`): add a schema to `TOOLS`, a function, a branch in `handle_tool_call`, a label in
`TOOL_STATUS_LABELS`, and (optionally) a line in `SYSTEM_PROMPT`.

## Security

- The app binds to **127.0.0.1** with **debug off** by default — it is not reachable from the LAN.
- A **passphrase gate** (`ACCESS_CODE`) protects every page and endpoint.
- Secrets live only in the gitignored `.env`; no credentials are committed.
- Note content the assistant reads is treated as **data, not instructions** (prompt-injection
  guard). See **`SECURITY_NOTES.md`** for the full findings, fixes, and open risks.
