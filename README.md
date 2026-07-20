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

## Media & research capabilities (Round 2)

These require two system tools from Homebrew — `brew install ffmpeg whisper-cpp` — plus the
Whisper model at `models/ggml-base.en.bin` (download command is in `requirements.txt`). Python
deps (`ddgs`, `beautifulsoup4`, `lxml`, `Pillow`) come from `pip install -r requirements.txt`.

### 🎬 Video input to the chat (`analyze_video`)
Upload a video in the chat (📎 button) or drop one in `inbox/`, then ask about it. The pipeline
samples representative frames (ffmpeg scene detection + even sampling), transcribes the audio
locally with Whisper (no cloud), and sends frames + transcript + your instruction to Claude.
Handles no-audio clips, long videos (caps transcription at 15 min), and unsupported formats.
Module: `second-brain-chat/video_processor.py`. Upload endpoint: `POST /api/upload_video`.

### 🔎 Data synthesizer (`synthesize_data` / `data_synthesizer_agent.py`)
Give it a topic to research online (keyless DuckDuckGo) or paste raw material to organize; it
produces one structured markdown report (summary, sections, cited sources) saved to
`synthesized/` and logged to Supabase. Drop a `TAVILY_API_KEY`/`SERPER_API_KEY`/`BRAVE_API_KEY`
in `.env` to upgrade search with zero code changes.
```bash
python3 data_synthesizer_agent.py "topic to research"
python3 data_synthesizer_agent.py "title" --text "raw notes to organize..."
```

### 🌐 Website creator (`create_website` / `website_creator_agent.py`)
Give it a brief; it plans a site, designs a coherent visual system, writes each page with real
copy, self-reviews, and saves a complete static site to `sites/<name>/` with a one-command
preview and README. Nothing is deployed.
```bash
python3 website_creator_agent.py --brief "A landing site for..."
bash sites/<name>/serve.sh          # preview on http://localhost:8080
```

### ✂️ Video toolkit (`edit_video` / `video_toolkit.py`)
ffmpeg-backed editing from chat or CLI: trim, caption (burned-in), concat, add/replace audio,
9:16 vertical for Shorts, thumbnail. Output goes to `media_lib/`. AI video *generation* is a
documented V2 stub (`video_gen_stub.py`), not yet implemented.
```bash
python3 video_toolkit.py caption inbox/clip.mp4 --text "Hello" --position bottom
python3 video_toolkit.py vertical inbox/clip.mp4 --mode crop
```

## Decisions, dashboard & tasks (Round 3)

### 🧭 Decision council + feasibility judge (`deliberate` / `assess_feasibility`)
Run an idea through the council: an **Advocate** argues for it, a **Critic** argues against, a
**Feasibility Judge** assesses whether it can *actually* work as intended (a plausibility rating
N/10, the causal chain's weakest link, and the most likely failure mode), and a **Judge** weighs
all three. Ask *"run this through the council"* for the full deliberation, or *"is this feasible: …"*
for just the calibrated feasibility read. Analytical only — takes no action. Both are logged and
surface on the dashboard's Council panel.

### 🏠 Home dashboard (`/dashboard`)
A clean, mobile-friendly command deck: Tasks, Council Decisions, Recent Agent Activity, Recent
Vault Notes, Synthesized Reports, and Built Sites (each links to a live in-app preview at
`/preview/<slug>/`). Quick actions deep-link into the chat. Auto-refreshes every 30s. The original
sci-fi HUD is preserved at **`/hud`**.

### ✅ Task tracker (`create_task` / `update_task_status` / `list_tasks` / `show_task_history` / `evaluate_task`)
A supervised idea/to-do board stored in local SQLite (`second-brain-chat/task_tracker.py`). Tasks
flow idea → evaluating → approved → in_progress → done/dropped, with a per-task history log.
`evaluate_task` sends a task to the council and attaches the verdict. **This is bookkeeping only —
nothing here executes a task** (distinct from the autonomous `task_manager.py`).

## Memory, screen, drafts, goals & voice (Round 4)

### 🧠 Conversation memory (`search_memory` + automatic recall + `/memory`)
Every chat is stored in a local, gitignored SQLite DB (`second-brain-chat/conversation_memory.py`),
grouped into sessions by inactivity and summarized (by Claude, with a heuristic fallback) when a
session closes. Jarvis recalls relevant past context **automatically** — the most relevant snippets
from earlier conversations are injected into each turn — and you can search explicitly with
`search_memory` ("what did we discuss about X?"). Browse, search, re-summarize, and **permanently
delete** conversations on the **Memory page** (`/memory`). Private + local; never committed.

### 👀 Screen-watch (`watch_screen`) — WATCH-ONLY
Ask "what's on my screen?", "what's this error?", or "summarize this article" and Jarvis captures
your screen (macOS `screencapture`, main or all displays) and answers with Claude vision. The
screenshot is **deleted right after** unless you say to keep it (saved to gitignored `screenshots/`).
It is strictly watch-only — **no mouse/keyboard/UI control code exists anywhere**. Needs macOS
Screen Recording permission; if it's missing you get grant instructions, not a wrong answer.

### 📝 Run drafter (`draft_run` / `run_drafter.py` / `jarvis-launch.sh`) — DRAFTS ONLY
Jarvis turns a goal (or a tracked task) into a complete overnight-build prompt: it gathers context,
runs the idea through the council, and writes a ready-to-launch prompt to `run_drafts/` — with the
**hard safety rules copied verbatim (never weakened)** and the council verdict attached. Review
drafts on the dashboard's **Drafted Runs** panel (view full, approve, mark launched/completed).
It **never launches anything** — you launch an approved draft yourself with `bash jarvis-launch.sh`,
which lists approved drafts, prints the exact command, and copies the draft path (it never invokes
`claude`).

### 🎯 Goals + task priority (`create_goal` / `link_task_to_goal` / `list_goals`, urgency/importance)
Track bigger goals (title, target date, status) whose progress derives from their linked tasks
(shown with progress bars on the dashboard). Tasks now carry **urgency** and **importance** (0-5);
the default task ordering and the briefing use them.

### 🎙️ Voice v1 (mic → local Whisper; spoken replies)
The mic records audio and transcribes it **locally with whisper.cpp** (`/api/transcribe`), dropping
the text into the chat box for you to send. Spoken replies (off by default, "Voice" toggle) use the
browser's system voices; `/api/speak` also exposes macOS `say`. Not always-listening.

### ☀️ Morning briefing (`morning_briefing`, say "brief me")
A short, prioritized rundown: urgent/important tasks, goal progress, drafts awaiting approval,
recent agent/council activity, recent notes, and a recap of your last conversation.

### 💾 Backups + shortcuts (`run_backup` / `scripts/backup.sh` / `shortcuts.json`)
`scripts/backup.sh` snapshots the project (including the conversation + task/goal DBs, excluding
model weights and generated media) plus a read-only Obsidian vault copy to `~/second-brain-backups/`,
keeping the newest 7. Run it by hand or say "back up my system" (`run_backup`) — it does **not**
schedule itself. `shortcuts.json` maps short commands (brief, goals, tasks, screen, backup, …) to
longer prompts, expanded in chat; edit it freely.

## Testing (`run_tests.py`)

`run_tests.py` at the project root is the single regression suite — **run it after any change.**

```bash
python3 run_tests.py                 # offline: fast, free, no new network calls (the regression bar)
python3 run_tests.py --live          # ALSO run real Claude API / web tests
python3 run_tests.py --only vault,gate,tasks   # run named suites only
```

It covers vault tools (+ the read-only guarantee), the access gate, the video toolkit and pipeline,
the data synthesizer, the website idempotency guard, the feasibility judge, the task tracker,
conversation memory (sessions/search/recall/delete), goals + task priority, screen-watch (blank
detection + vision + no control code), the run drafter (verbatim safety + status flow), voice (local
whisper transcription), the briefing + shortcuts, the backup script, and the security invariants
(no live secret in any `.py`, localhost-only default, `.env`/memory-DB/screenshots gitignored, and
**no mouse/keyboard control code anywhere**).
Offline mode uses realistic fakes for anything that would hit Claude or the web, so it's
deterministic and costs nothing; `--live` exercises the real model/network paths. It points
`OBSIDIAN_VAULT_PATH` at `sample_vault` first, so it never touches your real vault.

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
