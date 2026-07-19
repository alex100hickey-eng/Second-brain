# Second Brain Project — Handoff / Status Document

Last updated: 2026-07-19 (late night), by Claude Code. Two sessions ago: deployed `money_clips_agent`, vault persistence, read-only Calendar via Composio. This session: self-expansion (`create_new_tool`), dashboard with widgets, streaming chat, persistent memory, approval layer with first gated write (calendar event creation), server-side chat history, dormant password gate, PWA/mobile support, and fixed the vault-sync launchd job (two stacked causes: Full Disk Access regression + `.git` inside iCloud causing "Resource deadlock avoided" on commit — git dir now lives at `~/.second-brain-vault.git` via `--separate-git-dir`, vault has a `.git` pointer file).

## Current Feature Set (chat brain, deployed & verified)

- **Streaming chat** — `/chat` streams NDJSON (text deltas + tool-status events); UI types replies live and shows "Checking your calendar…"-style status lines. Gunicorn start command includes `--timeout 120` for this.
- **Persistent memory** — `remember` tool; memories are `jarvis_memory` rows in Supabase `Agent Outputs`, injected into the system prompt every request. Shared by local + deployed instances.
- **Server-side chat history** — conversation stored as `jarvis_chat` rows; all devices see the same thread (`/api/history`, clear via marker rows, `_normalize_for_api` guards alternating roles). No more localStorage.
- **Dashboard** (`/dashboard`, `/api/dashboard`) — widgets: Today (Google Calendar, 5-min cache), Awaiting Your Approval (Approve/Deny buttons → `/api/approve`), Awaiting Your Review (drafted agents/ + proposed_tools/ files), Recent Agent Outputs, Vault Notes.
- **Approval layer** — `propose_calendar_event` queues a `jarvis_pending_action` row; ONLY an explicit dashboard Approve executes (GOOGLECALENDAR_CREATE_EVENT, `create_meeting_room: False`). Deny + already-decided guards tested. This is the reusable pattern for all future consequential actions.
- **Self-expansion** — `create_new_agent` (drafts to `agents/`) and `create_new_tool` (drafts schema+function+routing to `proposed_tools/`); both draft-only, never wired/run automatically; drafts surface on the dashboard for review.
- **Password gate (DORMANT)** — full login flow exists (`/login`, 31-day session, brute-force delay) but only enforces when a `JARVIS_PASSWORD` env var is set. **Not yet set — the app is still open. First thing next session: Alex adds JARVIS_PASSWORD in Coolify env vars (Runtime only) + redeploy.** Note session cookies are signed with a key derived from the password.
- **PWA** — manifest + icons in `static/`; add-to-home-screen works on iOS/Android, standalone dark UI.
- Internal Supabase row types (filtered out of all agent-output views): `jarvis_memory`, `jarvis_pending_action`, `jarvis_chat`, `jarvis_chat_clear` — all piggyback on the `Agent Outputs` table so no schema changes were needed.

**Coolify deploy gotcha (new):** the deployment queue can silently jam — a triggered deploy sits "Queued" forever and pushes don't auto-deploy. Fix: Deployment page → **Force Start**. The Actions dropdown is flaky under automation; reliable sequence is real mouse click on Actions → screenshot to confirm menu is open → click the item.

---

## Vision

Alex is building a personal AI "second brain," modeled on Jarvis from Iron Man: a chat interface that knows his stuff, can act on his behalf, and eventually manages its own agents across his digital life.

The end goal: a chat interface plus an adaptable home-screen/dashboard he can add components to over time, where the AI can do almost anything he could do on his own computer — including creating and registering its own new agents on request, and eventually gaining access to new sites/services as needed.

The intended autonomy model:
- **Read-only and easily-reversible actions** can run autonomously, no confirmation needed.
- **Consequential or irreversible actions** (spending money, signing up for new accounts, sending things externally) require a confirmation gate.
- Safeguards are being built incrementally as the system grows, not solved all upfront. As of this writing, **no confirmation-gate layer exists yet** — this is why every new capability added so far has been deliberately kept read-only or draft-only (see Guardrails section below).

Longer-term angles mentioned: a YouTube content-agent business line targeting roughly $3,000/month, plus personal-life specialist agents for Schedule, School, and Athletics. Alex has said he currently cares less about the money-agent side and more about building out the core Jarvis capabilities (the chat brain itself).

Roughly in priority order per Alex's stated plan, the big pieces are: (1) money agent(s) deployed and running, (2) vault/agent persistence on the server, (3) more specialist agents, (4) a dashboard/visual layer, (5) real-world action connectors (calendar, email, etc. via Composio), (6) an approval/confirmation layer for consequential actions. Items 1, 2, and the calendar half of 5 are now done — see below.

---

## Stack / Infrastructure

- **Hetzner** — CPX11 server ($24/mo, 2 vCPU, 2GB RAM, 40GB SSD), Ashburn, Virginia. IP: `178.156.209.40`.
- **Coolify** v4.1.2 — dashboard at `http://178.156.209.40:8000` (login-gated; Claude does not have credentials and should never be given them — Alex logs in himself; the browser session sometimes stays authenticated across a Claude Code session once he's logged in once). GitHub App connected as "second-brain1".
  - Project UUID: `xn159afo226l4480ogtcrznz`, environment UUID: `p78muchurjjfu962yg4iredu` (production).
  - **`second-brain-chat`** (chat brain) app UUID: `h72tei3gy97z4wlqyqpvuylg`. Live at `http://h72tei3gy97z4wlqyqpvuylg.178.156.209.40.sslip.io`.
  - **`money-clips-agent`** app UUID: `dfjbnh7wz3cvxk29vf3b39vg`.
- **GitHub** — private repo `Second-brain` under account `alex100hickey-eng`, remote `https://github.com/alex100hickey-eng/Second-brain.git`. Local working copy at `~/second-brain`.
  - New this session: private repo `second-brain-vault` (same account) — a git mirror of the Obsidian vault. See Phase 4 below.
- **Supabase** — table `Agent Outputs` (note the space and capital letters — easy to typo). Columns: `agent_name` (text), `output_text` (text), `created_at` (timestamptz). RLS disabled.
- **Claude API** — `CLAUDE_API_KEY` env var set in `~/.zshrc`.
- **Composio** — Alex has an account at `app.composio.dev`. `COMPOSIO_API_KEY` env var set in `~/.zshrc` (a full-access classic-style key; the first key he generated was scoped read-only on `auth_configs` and had to be replaced — see Phase 5 gotchas). Also connected as an MCP server directly to Claude Code itself (separate from the app-embedded integration — see Phase 5 for the distinction).
- **Claude Code** — installed on Alex's Mac, Node.js + Composio + GitHub linked.
- **Claude in Chrome** — installed, available on Alex's paid plan.
- **Obsidian vault** — named "Second brain" (lowercase b), lives on iCloud Drive. Full path:
  `/Users/alexhickey24/Library/Mobile Documents/com~apple~CloudDocs/Obsidian/Second brain`
  **Actual current contents (verified, not assumed):** only a `Money/` folder exists, containing `assistant-note.md`. There is no `.obsidian` config folder, meaning the vault may never have actually been opened in the real Obsidian app — it exists on disk but might be effectively just a folder structure the chat brain writes into. The `Schedule`, `Learning`, `School`, `Athletics` folders referenced in `VAULT_FOLDERS` in `app.py` **do not exist yet** — they get created on demand the first time something is written into them.
  Despite the sparse vault contents, real Google Calendar data confirms Alex does have a detailed daily routine (training/school/practice blocks) — that data lives in Google Calendar, not in vault notes.

Secrets are always env vars — never hardcoded, never pasted into chat, never entered into any tool or form by Claude on Alex's behalf. When a key needs to go into Coolify, Alex pastes it in himself; Claude sets up placeholder fields (`REPLACE_ME`) and navigates the browser there for him.

---

## What's Built So Far

### Phase 1 — `money_clips_agent` (✅ complete, deployed, verified working end-to-end)

`money_clips_agent.py` (repo root) — rotates between 3 content themes by day-of-year (oddly satisfying / did-you-know facts / nature & animal curiosities). Each run: picks a theme, asks Claude for a short-form video concept (topic, hook, 40–60 sec script, 3 caption ideas), saves the result as JSON to the Supabase `Agent Outputs` table for review before feeding into a video tool. Model: `claude-sonnet-5`.

**Deployed to Coolify** as its own resource, `money-clips-agent`:
- Build Pack: Nixpacks, Base Directory `/` (repo root)
- Start Command: `sleep infinity` — this is **not** a web server, so the container just idles; the actual work happens via a Scheduled Task
- Health check: disabled (nothing to check — no HTTP server)
- Scheduled Task `run-money-clips-agent`, cron `0 13 * * *` (daily, 13:00 UTC / 9am ET), command: **`/opt/venv/bin/python3 money_clips_agent.py`**
- Env vars: `CLAUDE_API_KEY`, `SUPABASE_URL`, `SUPABASE_KEY`

**Gotcha worth remembering:** Nixpacks builds a Python venv at `/opt/venv` and installs `requirements.txt` into it, but does **not** set a container-wide `ENV PATH` pointing at it — that activation only happens inside the shell wrapper around the app's own Start Command. Coolify Scheduled Tasks run via `docker exec`, a separate process that does **not** inherit that activation. A bare `python3` in a Scheduled Task command will hit the system Python (no packages installed) and fail with `ModuleNotFoundError`. Fix: always call `/opt/venv/bin/python3` explicitly in Scheduled Task commands for Nixpacks Python apps.

Verified: manually triggered the scheduled task, it ran in ~11s, and a real generated concept ("Your Body Is Glowing Right Now") was confirmed saved in Supabase `Agent Outputs`.

### Phase 2 — Chat brain app (✅ complete, deployed live, actively extended this session)

`second-brain-chat/app.py` + `templates/index.html` — a Flask web app with a dark HUD-style chat UI. Calls the Claude API with a tool-use loop, model `claude-sonnet-5`. This is "Jarvis" itself — the persistent interface, as opposed to one-shot background agents like `money_clips_agent`.

- **Locally:** runs on `localhost:5001` (port 5000 taken by macOS AirPlay). Run with `python3 app.py`.
- **Deployed:** `http://h72tei3gy97z4wlqyqpvuylg.178.156.209.40.sslip.io`. Coolify config:
  - Build Pack: Nixpacks, Base Directory: `/second-brain-chat`
  - Start Command: `gunicorn app:app --bind 0.0.0.0:5000` (hardcoded port — Coolify's Start Command field blocks bare `$`, so `$PORT` doesn't work there; port must just match "Ports Exposes")
  - Ports Exposes: `5000`
  - Persistent volume `vault-data` mounted at `/data/vault` (added this session — see Phase 4)
  - Env vars (Runtime access unless noted): `CLAUDE_API_KEY`, `SUPABASE_URL`, `SUPABASE_KEY`, `VAULT_PATH=/data/vault`, `VAULT_GIT_TOKEN` (Runtime only, not Buildtime), `COMPOSIO_API_KEY` (Runtime only, not Buildtime)

**Security note (unchanged, still important):** `app.py`'s local dev entrypoint runs `app.run(..., debug=True)`. Flask's `debug=True` exposes the unauthenticated Werkzeug debugger — if that path ever ran on a public server, a triggered error would let anyone execute arbitrary Python on the box. The Coolify deployment uses `gunicorn` specifically to avoid ever hitting that `if __name__ == "__main__":` block. **Do not change the Coolify Start Command to `python3 app.py`.**

The modular tool pattern in `app.py` (add one entry to `TOOLS`, one function, one line in `handle_tool_call`) is deliberately preserved — see Working Style below.

### Phase 3 — Self-extending agent tool (✅ complete, tested locally; one draft awaiting review)

`create_new_agent(name, purpose)` tool in `app.py`. Drafts a complete Python script following the `money_clips_agent.py` pattern and saves it to `agents/<name>.py`. **Hardcoded safety boundary:** only ever writes the file to disk — never runs, imports, or deploys it. The system prompt and the tool's own return message both always tell Alex it needs review.

`agents/stock_watch_agent.py` — a test draft (checks stock news for a hardcoded ticker watchlist using Claude's own knowledge, no live market-data API). Compiles cleanly, 139 lines. **Still untracked in git, still not reviewed or approved by Alex.** One caveat surfaced during review: since it has no real market-data feed, its "news" is really just Claude's training-knowledge recall — treat it as a rough digest, not a live data source, if it's ever approved and run.

**Known gap, not yet fixed:** drafted agents written via the *deployed* chat brain land in that container's ephemeral filesystem and vanish on redeploy — same root cause the vault had (see Phase 4) and same fix pattern would apply (a persistent volume), just not yet done for `agents/`.

### Phase 4 — Obsidian vault persistence (✅ complete, full round trip verified)

This was the big fix this session — previously the deployed chat brain had no access to the real vault at all.

**Three vault tools already existed in `app.py`** (`list_vault_notes`, `read_vault_note`, `write_vault_note`) and worked locally, but had nothing to read on the server. The fix was a full sync pipeline, not a code change:

1. **New private GitHub repo `second-brain-vault`** (`alex100hickey-eng/second-brain-vault`) — a git mirror of the vault contents.
2. **The vault folder itself is now a git repo** (`git init` was run directly inside `.../Obsidian/Second brain/`), with a `.gitignore` for `.DS_Store` and Obsidian workspace/cache files, pushed to `second-brain-vault`.
3. **`~/second-brain/scripts/vault_sync.sh`** — a small script that `git add -A && git commit && git push`es any vault changes. Runs via a **launchd** job, `~/Library/LaunchAgents/com.secondbrain.vaultsync.plist`, every 10 minutes (`StartInterval: 600`).
4. **Coolify side:** a persistent Docker volume `vault-data` mounted at `/data/vault` on the `second-brain-chat` resource, plus a Scheduled Task `sync-vault` (every 10 min) that clones-or-pulls `second-brain-vault` into that volume using a `VAULT_GIT_TOKEN` credential embedded in the clone URL.
5. **`VAULT_PATH` env var** on the deployed chat brain points at `/data/vault` — so the existing vault tools now transparently work against the real (synced) content with zero code changes.

**Verified end-to-end:** wrote a test line into the real vault on the Mac, confirmed the launchd job picked it up and pushed it, confirmed Coolify's scheduled pull grabbed it, and confirmed the deployed chat brain's `/chat` endpoint reported the updated content back correctly.

**Gotchas worth remembering:**
- **macOS Full Disk Access:** the launchd job initially failed with `fatal: Unable to read current working directory: Operation not permitted` when touching the iCloud Drive path — even though the exact same script ran fine when invoked from an interactive terminal. This is macOS TCC sandboxing: a `launchd`-spawned process doesn't inherit Terminal's Full Disk Access grant. Granting FDA to `/bin/bash` did **not** fix it — the actual syscall was coming from `git`, not `bash`. Fix: System Settings → Privacy & Security → Full Disk Access → add `/usr/bin/git` specifically (not bash, not Terminal).
- **`VAULT_GIT_TOKEN` needs a classic GitHub PAT, not fine-grained.** A fine-grained token with `Contents: Read-only` on just that repo authenticated fine but returned `403: Write access to repository not granted` on clone — a known-confusing GitHub error message for fine-grained tokens with insufficient scope (it says "write" even for read operations). Switched to a classic PAT with the `repo` scope and it worked immediately.
- **Secret in Docker build args:** the first pass accidentally left `VAULT_GIT_TOKEN` (and initially `COMPOSIO_API_KEY`) checked "Available at Buildtime" in Coolify, which Docker flagged with a `SecretsUsedInArgOrEnv` warning (risk of the secret being baked into an image layer). Neither needs buildtime access — both are only used by scheduled tasks / the running app. Fixed by unchecking Buildtime, leaving Runtime only, and redeploying.

### Phase 5 — Read-only Google Calendar access via Composio (✅ complete, verified on deployed instance)

The first "real-world action" capability — previously the chat brain could only touch the vault and Supabase.

**Scope, deliberately limited per Alex's own stated autonomy model:** read-only only (list/search events, list calendars, get current time). No `create_event`/`update_event`/`delete_event` tools were added, and none are reachable even if Claude wanted to call them — this was enforced by explicitly whitelisting exactly 4 tool slugs (`GOOGLECALENDAR_EVENTS_LIST`, `GOOGLECALENDAR_FIND_EVENT`, `GOOGLECALENDAR_LIST_CALENDARS`, `GOOGLECALENDAR_GET_CURRENT_DATE_TIME`) when fetching schemas from Composio, rather than pulling in the whole `googlecalendar` toolkit. This matches the stated rule that consequential/external actions need a confirmation gate that doesn't exist yet.

**Implementation in `app.py`:**
- `composio = Composio(provider=AnthropicProvider(), api_key=COMPOSIO_API_KEY)`
- `CALENDAR_TOOLS = composio.tools.get(user_id="alex", tools=CALENDAR_TOOL_SLUGS)` fetched once at import time, merged into `TOOLS`
- In `handle_tool_call`: any tool name in `CALENDAR_TOOL_SLUGS` gets routed to `composio.tools.execute(slug=..., arguments=..., user_id="alex", dangerously_skip_version_check=True)`
- `requirements.txt` pins `composio==0.18.0` and `composio-anthropic==0.18.0` exactly (see gotcha below)

**One-time OAuth setup:** `~/second-brain/scripts/connect_google_calendar.py` — creates a Composio-managed auth config for the `googlecalendar` toolkit (no Google Cloud project needed; Composio provides shared OAuth credentials for common toolkits) and generates a connection link. Alex opened the link and authorized his Google account once; the connection is now `ACTIVE` and tied to `user_id="alex"` under Alex's Composio project — it persists regardless of which app or session calls it, as long as `COMPOSIO_API_KEY` matches the same project.

**Gotchas worth remembering:**
- **Package name confusion:** `pip install composio-core` pulls in a legacy/abandoned package line (last version `0.3.11`, homepage `SamparkAI/composio_sdk`) that is incompatible with the current `composio-anthropic` package. The real, current core package is just `composio` (currently `0.18.0`, homepage `ComposioHQ/composio`). Don't install `composio-core` — install `composio` and pin the version to match `composio-anthropic`.
- **`ConnectedAccounts.initiate()` is deprecated and was fully retired 2026-07-03** for Composio-managed OAuth configs. Docs found via web search still describe it; the actual current SDK method is `composio.connected_accounts.link(user_id, auth_config_id)`, which returns a `ConnectionRequest` with `.redirect_url` and a `.wait_for_connection(timeout=...)` method. When web-search results and the installed package disagree, trust the installed package's source (`pip download --no-deps` + read the `.py` files directly) — it's ground truth.
- **`composio.tools.execute()` requires `dangerously_skip_version_check=True`** when called manually outside of the higher-level `provider.handle_tool_calls()` path, or it raises `ToolVersionRequiredError`. This flag is exactly what Composio's own internal agentic-execution wrapper sets by default, so it's not actually risky for this use case (always uses the latest tool version, no manual pinning needed).
- **Auth config permission scoping:** the first Composio API key Alex generated was scoped read-only on `auth_configs`, which let it *list* configs but not *create* one (403 `APIKey_InsufficientPermissions`). Needed a key with full/write access.

Verified: asked the deployed chat brain "What do I have on my calendar today?" and got back a real, detailed, correctly-dated list of the day's actual Google Calendar events.

---

## Coolify Browser-Automation Notes (for whoever drives the dashboard next)

Screenshots returned by the browser tool render at roughly 2x the CSS logical pixel scale used for click/type coordinates. Visually estimating a click position from a screenshot image is unreliable and was the source of most of the friction this session (repeated failed clicks on the "New Environment Variable" dialog). The reliable pattern:
1. Use `javascript_tool` (read-only) to call `getBoundingClientRect()` on the target element and get real CSS-pixel coordinates.
2. Click at that CSS-pixel coordinate with the `computer` tool.
3. Verify the click landed (e.g. check `document.activeElement`) before typing.

Also: Coolify's "New Environment Variable" modal is finicky with rapid automated clicks — when in doubt, the **"Developer view"** toggle (a plain multi-line `KEY=value` textarea) is a far more reliable way to bulk-inspect or edit env vars than the per-field modal. Caution: it renders **all existing secret values in plaintext**, and editing it by moving the cursor with `Cmd+End` doesn't reliably jump to the end in this environment — always re-measure cursor/selection position before typing into it, or a paste can land mid-line and corrupt an adjacent variable's value. If that happens and nothing has been saved yet, a page reload discards the unsaved draft safely.

---

## What Needs to Be Accomplished (Roadmap)

1. **Activate the password gate** — Alex sets `JARVIS_PASSWORD` in Coolify env vars (Runtime only), redeploy, log in from his devices. The app is on the open internet until this happens.
2. **HTTPS** — deliberately not attempted overnight (Let's Encrypt on sslip.io domains is rate-limit-prone and a failed cert could break the site unattended). Consider a real domain at the same time.
3. **Gmail (read-only) via Composio** — needs Alex present for the OAuth click. Then morning-brief type features.
4. **More approval-gated writes** — send email / edit events through the existing approval queue pattern.
5. **More specialist agents** (Schedule, School, Athletics, Money) — Alex has deprioritized these vs. core Jarvis capability.
6. **Server-side persistence for `agents/`+`proposed_tools/` drafts on the deployed instance** — same volume-mount pattern as the vault; currently drafts made via the live chat vanish on redeploy (local drafts are fine).
7. `agents/stock_watch_agent.py` still needs Alex's review/approval before it's committed or ever run.
8. ~~Dashboard~~ / ~~approval layer~~ / ~~vault persistence~~ / ~~money_clips deploy~~ — ✅ all done.

---

## Working Style / Principles to Keep

- **Modular tool design:** adding a new capability to the chat brain should mean adding one function to `TOOLS` + the function itself + one line in `handle_tool_call` — nothing else in `app.py` should need to change. The Composio calendar integration followed this same shape (one block of setup + one routing branch), even though the underlying mechanism (dynamically-fetched external tool schemas) is different from the hand-written local tools.
- **One capability at a time, local-first then deploy.** Don't jump ahead to deploying multiple untested things at once.
- **Read-only/reversible now, write/consequential only behind a confirmation gate later.** This is why calendar access is list/search-only, and why `create_new_agent` only ever drafts, never executes. Don't add write-capable tools (send email, create calendar event, spend money, sign up for accounts) without first building the approval layer, or without explicitly confirming scope with Alex first.
- **Secrets always via env vars** — never hardcoded, never pasted into chat, never entered into any web form by the assistant on Alex's behalf. Alex looks up values himself (`echo $VAR_NAME`) and pastes them in himself, whether that's into a shell config file or a Coolify field Claude has navigated the browser to.
- **Narrate what's being done and why, step by step** — Alex wants to understand the system as it's built, not have it appear as a black box.
- **Verify claims against actual files/git/service state before acting on them.** This handoff doc has drifted from reality before (an earlier version claimed a phase was "code complete" when it didn't exist at all; the vault folder list in this doc is now based on an actual `ls`, not assumption). Treat any status claim — including everything in this document — as something to double-check against the live repo/Coolify/Supabase/Composio state before relying on it, especially after time has passed.
- **When official docs and the installed package disagree, read the package source.** This mattered concretely this session with Composio's SDK — a deprecated method was still documented online.
