# Second Brain / Jarvis — Handoff Document

**Last updated:** 2026-07-19, by Claude Code, after an overnight autonomous session (bypass mode, Alex asleep). This document is meant to be pasted into or attached in a **new** Claude Code chat so a fresh session can pick up execution with full context. Read it, then verify anything load-bearing against the actual repo/Coolify/Supabase state before acting — status docs like this one drift.

---

## The Vision

Alex is building a personal AI "second brain," modeled on Jarvis from Iron Man: a chat interface that knows his stuff, can act on his behalf, and — this is the distinguishing goal, not a nice-to-have — **can extend itself and eventually manage its own agents** across his digital life.

**What "done" looks like eventually:** a chat interface plus an adaptable home-screen dashboard Alex can keep adding components to, where the AI can do almost anything Alex could do on his own computer — including drafting and registering brand-new capabilities for itself on request, and gaining access to new sites/services as needed. Not a fixed feature set; a system that grows.

**The autonomy model — this is the single most important design constraint on everything built so far:**
- **Read-only and easily-reversible actions run autonomously**, no confirmation needed (checking calendar, reading notes, drafting a file nobody runs yet).
- **Consequential or irreversible actions require a confirmation gate**: spending money, signing up for new accounts, sending things externally, editing/deleting real files, actually adopting a new capability. Alex is aware of the risk of an AI acting on its own in costly ways and wants this solved incrementally as the system grows, not all upfront.
- Concretely, this means: nothing Jarvis "writes" for itself (a new agent, a new tool, a code change) ever runs, deploys, or wires itself in automatically. Every new capability passes through a human at least once, sometimes twice (approve, then merge).

**Priorities, per Alex directly:** he cares much less about the money/content-agent side than about building out core Jarvis capability — the self-expanding assistant itself, plus the dashboard, are the parts he wants to see get better. Specialist life-agents (Schedule, School, Athletics) and additional money agents are explicitly deprioritized for now.

**Longer-term angles mentioned (lower priority):** a YouTube content-agent business line targeting roughly $3,000/month (the reason `money_clips_agent` exists at all).

---

## Stack / Infrastructure

- **Hetzner** — CPX11 server ($24/mo, 2 vCPU, 2GB RAM, 40GB SSD), Ashburn, Virginia. IP: `178.156.209.40`.
- **Coolify** v4.1.2 — dashboard at `http://178.156.209.40:8000` (login-gated; Claude Code does not have credentials and must never be given them — Alex logs in himself; the browser session sometimes stays authenticated across sessions once he's logged in once). GitHub App connected as "second-brain1".
  - Project UUID: `xn159afo226l4480ogtcrznz`, environment UUID: `p78muchurjjfu962yg4iredu` (production).
  - App **`second-brain-chat`** (the actual Jarvis) — UUID `h72tei3gy97z4wlqyqpvuylg`. Live at `http://h72tei3gy97z4wlqyqpvuylg.178.156.209.40.sslip.io`. **No HTTPS yet, no password enforced yet — see Immediate Next Steps.**
  - App **`money-clips-agent`** — UUID `dfjbnh7wz3cvxk29vf3b39vg`. A background content-idea generator, not the chat brain.
- **GitHub** — private repo `Second-brain` under account `alex100hickey-eng`, remote `https://github.com/alex100hickey-eng/Second-brain.git`. Local working copy at `~/second-brain` on Alex's Mac.
  - Second private repo `second-brain-vault` (same account) — a git mirror of the Obsidian vault, used for syncing vault content to the server (see Vault Persistence below).
  - A review branch, **`jarvis/tool-get_word_count`**, currently sits on the main repo awaiting Alex's review — the first real artifact of the self-expansion pipeline (see below). Needs a merge-or-discard decision.
- **Supabase** — single table `Agent Outputs` (note the space and capital letters — easy to typo) does double duty as the entire app's datastore. Columns: `agent_name` (text), `output_text` (text), `created_at` (timestamptz). RLS disabled. Every internal subsystem (memory, chat history, pending approvals) piggybacks on this same table with a distinct `agent_name` tag rather than needing schema changes — see "Internal row types" below.
- **Claude API** — `CLAUDE_API_KEY` env var, set in `~/.zshrc` locally and in Coolify env vars for the deployed app.
- **Composio** (`app.composio.dev`) — used for real-world connectors (currently just Google Calendar, read-only). `COMPOSIO_API_KEY` env var, needs to be a full-access key (a read-only-on-auth_configs key will authenticate but can't create configs). Also connected as an MCP server directly to Claude Code (separate concern from the app's own Composio usage).
- **Obsidian vault** — named "Second brain" (lowercase b), lives on iCloud Drive at `/Users/alexhickey24/Library/Mobile Documents/com~apple~CloudDocs/Obsidian/Second brain`. Sparse contents (mostly just a `Money/` folder and daily briefs) — the vault may never have been opened in the real Obsidian app, it might just be a folder structure. Alex's actual daily routine lives in Google Calendar, not vault notes.
- **Claude Code** — installed on Alex's Mac, Node.js + Composio + GitHub linked.
- **Claude in Chrome** — installed, on Alex's paid plan; used for driving the Coolify dashboard.

**Secrets policy, non-negotiable:** always env vars, never hardcoded, never pasted into chat, never entered into any web form by Claude on Alex's behalf. When a Coolify field needs a real secret, Claude sets up a placeholder (`REPLACE_ME`) and Alex pastes the real value in himself.

---

## What's Built — Jarvis's Current Capabilities

The chat brain lives at `~/second-brain/second-brain-chat/app.py` (+ `templates/`), a Flask app with a dark HUD-style UI, calling Claude with a tool-use loop. Run locally with `python3 app.py` (port 5001 — 5000 is taken by macOS AirPlay). Deployed via Coolify with `gunicorn app:app --bind 0.0.0.0:5000 --timeout 120` (the long timeout is for streaming; **never** change this Start Command to `python3 app.py` — the local dev entrypoint has `debug=True`, which exposes Werkzeug's unauthenticated debugger, an RCE risk if it ever ran on the public server).

**The modular tool pattern this app follows for everything:** one entry in the `TOOLS` list (Anthropic tool schema), one plain Python function of the same name, one routing line in `handle_tool_call`. Nothing else needs to change when adding a capability. Preserve this shape.

### Core interface
- **Streaming chat** (`/chat`) — replies stream in live (NDJSON: text deltas + tool-status events like "Checking your calendar…"), instead of one blocking response.
- **Server-side chat history** — conversation stored in Supabase (`jarvis_chat` rows), so every device sees the same thread. `/api/history`, cleared via a marker row rather than deletion.
- **Dashboard** (`/dashboard`, backed by `/api/dashboard`) — a second page, same visual language, showing real data: Today's calendar, pending approvals, decision history, drafted-agents/tools awaiting review, recent agent outputs, vault notes, and Jarvis's saved memories.
- **Markdown rendering** in assistant replies (safe: HTML-escaped first, then formatted).
- **PWA support** — manifest + generated icons, add-to-home-screen works on iOS/Android.
- **Login gate — built but DORMANT.** Full flow exists (`/login`, 31-day session, brute-force delay), but only activates once a `JARVIS_PASSWORD` env var is set in Coolify. **It is not set yet — the live site is currently open to anyone with the URL.** This is the single most important thing for Alex to do next.

### Memory
- `remember(fact)` — saves a fact to Supabase (`jarvis_memory` rows); every memory is injected into the system prompt on every request, so it's available in brand-new conversations and shared between local and deployed instances.
- `forget_memory(matching_text)` — soft-deletes (retags to `jarvis_memory_forgotten`, never destroyed) when exactly one memory matches; otherwise lists matches so Alex/Jarvis can be more specific.

### Real-world connectors
- **Google Calendar, read-only** — list/search events, list calendars, get current time. Deliberately whitelisted to exactly those Composio tool slugs; no create/update/delete slugs are reachable at all, by design (matches the autonomy model — write access needs the approval gate, see below).
- Vault tools (`list_vault_notes`, `read_vault_note`, `write_vault_note`) — read/write actual Obsidian notes.

### The approval layer (the mechanism, not just one feature)
A generic pattern: a tool call queues a `jarvis_pending_action` row in Supabase with status `pending`; the dashboard shows it under "Awaiting Your Approval" with Approve/Deny buttons; **only** a POST to `/api/approve` executes anything, and it is the *only* code path that does. This is reused by:
- `propose_calendar_event` → on approval, creates the event via Composio (`create_meeting_room: False`).
- `propose_file_cleanup` (see File cleaning below) → on approval, moves files to Trash.
- `adopt_tool` (see Self-expansion below) → on approval, pushes a branch to GitHub.

Decided actions (approved/denied/failed) show up in a "Decision History" log on the dashboard — the gate's paper trail.

### Self-expansion (the flagship capability)
Two things Jarvis can draft for itself, both gated by a two-step human review:
- `create_new_agent(name, purpose)` — drafts a full standalone background-agent script (like `money_clips_agent.py`) to `agents/<name>.py`. Never runs it.
- `create_new_tool(name, purpose)` — drafts a proposal (schema + function + routing line) for a new **tool for Jarvis itself** to `proposed_tools/<name>.py`. Never wires it in.
- `adopt_tool(name)` — the next step for a tool proposal: queues it for dashboard approval; **on Approve**, it's committed (via an isolated git worktree, so Alex's checkout is never touched) to a branch `jarvis/tool-<name>` and pushed to GitHub — **not** to `main`, **not** deployed. A human must review and merge the branch.
- **Extension loader** in `app.py` — once merged into `main` and the app restarts, any `second-brain-chat/extensions/*.py` file (containing a `TOOL_SCHEMA` dict + matching function) loads automatically as a live tool, with `claude`, `supabase`, `VAULT_PATH` injected as shared context. A broken extension logs a warning and is skipped rather than crashing the app.

So the full loop is: **draft → adopt (approval #1) → branch on GitHub → human merge (approval #2) → restart → live.** Two human gates. Verified end-to-end overnight (drafted `get_word_count`, adopted it, branch confirmed on GitHub, simulated the merge locally to prove the loader works, then removed the simulation — the real merge decision is still Alex's). **`jarvis/tool-get_word_count` is the actual artifact sitting on GitHub right now, needing Alex's review.**

### Decision council
`deliberate(idea, context)` — three independent Claude calls: an **Advocate** builds the strongest honest case *for* an idea, a **Critic** independently builds the strongest case *against* it (neither sees the other's argument), and a **Judge** weighs both and rules WORTH IT / NOT WORTH IT / WORTH IT IF, with reasoning. Pure analysis, takes no action. Verified the Judge incorporates Jarvis's saved memories about Alex when relevant.

### File cleaning (Mac-only, Trash-only, approval-gated)
- `scan_downloads(days_old, min_size_mb)` — read-only scan of `~/Downloads` listing cleanup candidates. Only works when Jarvis is running locally on Alex's Mac (the deployed server has no access to his files).
- `propose_file_cleanup(filenames)` — queues chosen files for dashboard approval. **On Approve, files move to the macOS Trash via Finder/AppleScript (Put Back works) — never permanent deletion.** Paths are confined strictly to `~/Downloads` with no traversal.

### Background agents (separate from the chat brain)
- **`money_clips_agent.py`** (repo root) — rotates through 3 content themes daily, asks Claude for a short-form video concept, saves it to Supabase. Deployed on its own Coolify resource with `sleep infinity` as the start command (not a web server) plus a Scheduled Task running it daily at 9am ET. **Gotcha:** Nixpacks' venv at `/opt/venv` isn't on PATH for Scheduled Tasks (which run via `docker exec`, bypassing the shell wrapper that activates it) — always call `/opt/venv/bin/python3` explicitly.
- **`morning_brief_agent.py`** (repo root) — runs locally via launchd daily at 7:00 AM (`com.secondbrain.morningbrief`), gathers today's calendar + last-24h agent outputs + pending approvals, asks Claude to write it up, saves as `Schedule/brief-<date>.md` in the vault. Falls back to `~/second-brain/briefs/` with a warning if it can't write into the iCloud vault path (Full Disk Access issue — see Gotchas).
- **`agents/stock_watch_agent.py`** — a draft created early on to test `create_new_agent`. Still untracked, still needs Alex's review before it's ever run or committed. Its "market data" is really just Claude's training-knowledge recall, not a live feed — flag that if it's ever approved.

### Vault persistence (server can see the real vault)
Because the deployed chat brain runs in a container with no access to Alex's Mac, a sync pipeline bridges the gap:
1. The vault folder is its own git repo, pushed to the private `second-brain-vault` GitHub repo.
2. `~/second-brain/scripts/vault_sync.sh`, run by launchd (`com.secondbrain.vaultsync`) every 10 minutes, commits and pushes any vault changes.
3. On the server: a persistent Docker volume `vault-data` mounted at `/data/vault`, plus a Coolify Scheduled Task that pulls `second-brain-vault` into it every 10 minutes.
4. `VAULT_PATH=/data/vault` env var on the deployed app — the vault tools then transparently work against real, synced content.

**This broke and was fixed overnight, twice-stacked root cause:** (1) macOS Full Disk Access for `/usr/bin/git` had regressed — a launchd-spawned process doesn't inherit Terminal's FDA grant, and the checkbox needed re-enabling in System Settings; (2) even with FDA restored, commits failed with `fatal: could not open '.git/COMMIT_EDITMSG': Resource deadlock avoided` — the `.git` directory living *inside* an iCloud-synced folder hit iCloud's file locking. Fixed by moving git's internal directory out of iCloud entirely: `git init --separate-git-dir=$HOME/.second-brain-vault.git` run inside the vault, leaving only a one-line `.git` pointer file in the synced folder. **Lesson: never keep a live `.git` directory inside an iCloud Drive folder.** Verified working via two automatic `launchctl kickstart` runs that committed and pushed on their own.

### Internal Supabase row types
All piggyback on the one `Agent Outputs` table, filtered out of every agent-output-facing view (the `INTERNAL_AGENT_NAMES` set in `app.py`): `jarvis_memory`, `jarvis_memory_forgotten`, `jarvis_pending_action`, `jarvis_chat`, `jarvis_chat_clear`.

---

## Known Gotchas Worth Remembering

- **Coolify deploy queue can silently jam.** A webhook-triggered deploy sometimes sits "Queued" forever and never auto-starts; pushes alone don't guarantee a deploy happens. A stuck deploy can also block later ones in the queue (a 3+ hour zombie `money-clips-agent` build once blocked the chat-brain deploy entirely — cancel it from its own Deployment page). **Reliable trigger when this happens:** on the app's Deployments page, JS-click the hidden `wire:click="deploy"` div (visible text "Redeploy" inside the Actions dropdown) rather than fighting the dropdown UI, then click "Force Start" on the resulting deployment page. The Actions dropdown itself is flaky under browser automation — sometimes a real mouse click works, sometimes only `element.focus()` + spacebar, sometimes neither; verify with a screenshot before trusting a click landed.
- **Coolify browser automation in general:** screenshots render at ~2x the CSS pixel scale used for click coordinates — use `getBoundingClientRect()` via JS to get real click targets rather than eyeballing a screenshot.
- **Coolify's "New Environment Variable" modal** is finicky with automated clicks; the "Developer view" toggle (plain multi-line `KEY=value` textarea) is far more reliable for bulk env var edits — but it renders all existing secrets in plaintext, so treat it carefully, and re-measure cursor position before typing (a misplaced paste can corrupt an adjacent variable).
- **Composio SDK:** install `composio` (current, `ComposioHQ/composio`), never `composio-core` (abandoned legacy package, incompatible). `composio.tools.execute()` needs `dangerously_skip_version_check=True` outside the higher-level provider path. `ConnectedAccounts.initiate()` is deprecated — use `composio.connected_accounts.link(user_id, auth_config_id)`. When Composio's docs and the installed package disagree, trust the installed package's source.
- **Nixpacks + Coolify Scheduled Tasks:** the venv at `/opt/venv` isn't on PATH for `docker exec`-based Scheduled Tasks — always call `/opt/venv/bin/python3` explicitly.
- **A past version of this very handoff doc was wrong** — it once claimed a phase was "code complete" when the code didn't exist at all. Treat status claims (including in this document) as something to verify against live repo/Coolify/Supabase state, not as ground truth, especially after time has passed.

---

## Immediate Next Steps, in priority order

1. **Set `JARVIS_PASSWORD`** in Coolify env vars (Runtime only) for `second-brain-chat`, then redeploy. The login gate is fully built and waiting — until this env var exists, the live site is open to anyone with the URL.
2. **Review branch `jarvis/tool-get_word_count`** on GitHub — merge it (completing the first real self-expansion cycle) or discard it.
3. **HTTPS** — deliberately not attempted autonomously (Let's Encrypt on sslip.io domains is rate-limit-prone; a failed cert mid-deploy could break the live site unattended). Worth pairing with acquiring a real domain.
4. **Gmail (read-only) via Composio** — needs Alex present for the OAuth consent click, same pattern as Calendar.
5. **Server-side persistence for `agents/` and `proposed_tools/` drafts** — currently, anything drafted via the *live* (deployed) chat brain lands in that container's ephemeral filesystem and vanishes on redeploy. Local drafts are fine. Same fix pattern as the vault (a persistent volume) hasn't been applied here yet.
6. **Calendar edit/delete via the approval queue** — extend the same propose→approve pattern already built for event creation to event editing/deletion.
7. **More approval-gated writes generally** (send email, etc.) once Gmail exists.
8. Decide what to do with `agents/stock_watch_agent.py` (review, discard, or approve to run).
9. Specialist agents (Schedule/School/Athletics) and additional money agents — explicitly low priority per Alex.

---

## Working Style / Principles to Keep

- **Modular tool design** — one `TOOLS` entry + one function + one `handle_tool_call` line per capability. Don't refactor this shape away.
- **One capability at a time, local-first then deploy.** Test locally before deploying; don't stack untested changes.
- **Read-only/reversible now, write/consequential only behind the approval gate.** Never add a write-capable tool without routing it through the existing pending-action pattern, or without explicitly confirming scope with Alex first.
- **Secrets always via env vars** — never hardcoded, never pasted into chat, never entered into a form by Claude on Alex's behalf.
- **Narrate what's being done and why, step by step.** Alex wants to understand the system as it's built, not receive a black box.
- **Verify claims against actual files/git/service state before acting on them** — this doc, and the system generally, can drift from reality.
- **When official docs and an installed package disagree, read the package source** — it's ground truth.
