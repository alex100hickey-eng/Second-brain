# Second Brain / CLARVIS — Handoff Document

**Last updated:** 2026-07-19 (late-night session — third session this day, after the daytime and evening sessions referenced throughout), by Claude. This document is meant to be pasted into or attached in a **new** Claude Code chat so a fresh session can pick up execution with full context. Read it, then verify anything load-bearing against the actual repo/Coolify/Supabase state before acting — status docs like this one drift.

**NAMING: the system is now called "CLARVIS"** (Alex's chosen name, Iron Man J.A.R.V.I.S. homage). So far only the dashboard carries the new branding — the chat page, login page, PWA manifest, and this doc's older sections still say "Jarvis". A full branding pass is an open offer Alex hasn't taken up yet.

---

## The Vision

Alex is building a personal AI "second brain," modeled on Jarvis from Iron Man: a chat interface that knows his stuff, can act on his behalf, and — this is the distinguishing goal, not a nice-to-have — **can extend itself and eventually manage its own agents** across his digital life.

**What "done" looks like eventually:** a chat interface plus an adaptable home-screen dashboard Alex can keep adding components to, where the AI can do almost anything Alex could do on his own computer — including drafting and registering brand-new capabilities for itself on request, and gaining access to new sites/services as needed. Not a fixed feature set; a system that grows.

**The autonomy model — this is the single most important design constraint on everything built so far:**
- **Read-only and easily-reversible actions run autonomously**, no confirmation needed (checking calendar, reading notes, drafting a file nobody runs yet).
- **Consequential or irreversible actions require a confirmation gate**: spending money, signing up for new accounts, sending things externally, editing/deleting real files, actually adopting a new capability. Alex is aware of the risk of an AI acting on its own in costly ways and wants this solved incrementally as the system grows, not all upfront.
- Concretely, this means: nothing Jarvis "writes" for itself (a new agent, a new tool, a code change) ever runs, deploys, or wires itself in automatically. Every new capability passes through a human at least once, sometimes twice (approve, then merge).

**2026-07-19 evening escalation (important, see Task Manager below):** Alex pushed hard for "unlimited access" for a new autonomous **Task Manager** subsystem — a Prompter + Guardrail Council + worker that can execute open-ended multi-step goals without stopping to ask permission at every turn. Claude declined true zero-gates and negotiated a compromise Alex explicitly accepted: council verdicts default to *no restriction* unless the Critic convinces the Judge otherwise, but a fixed set of **hard gates** (money, new accounts, external sends, file deletion) always pause for one dashboard tap regardless of what the council decides. This is now layered on top of, not a replacement for, the approval-gate model described above.

**Priorities, per Alex directly:** as of 2026-07-19 evening, **the Task Manager is his main priority**, above the rest of the roadmap below. He cares much less about the money/content-agent side than about building out core Jarvis capability.

**Longer-term angles mentioned (lower priority):** a YouTube content-agent business line targeting roughly $3,000/month (the reason `money_clips_agent` exists at all).

---

## 2026-07-19 Late-Night Session — CLARVIS rename, HUD dashboard, Task Manager fixes

Everything in this section was done in the third session of 2026-07-19 (roughly 7pm–midnight ET), all committed and pushed. Three commits: `364b1c8`, `6d6b496`, `736fb77`.

### Dashboard → CLARVIS "command deck" (commits `364b1c8` then `736fb77`)
`templates/dashboard.html` was rebuilt twice, ending as a near-1:1 replica of a sci-fi HUD reference image Alex supplied (he pushed through ~6 design iterations demanding maximal fidelity — when touching the dashboard, match that aesthetic, don't "clean it up"):
- **Desktop (>1100px) is a free-form scatter scene, not panels:** a central animated reactor (shattered segment rings, outline trapezoid flaps, comet arc, white-hot center) with the glowing **CLARVIS** wordmark; live instruments scattered around it — triple donuts top-left (Approvals / Review / Memories), gauges right (Managed Ops / Revenue vs $3k goal / Background), a Today event stack, Vault + Transmissions readouts — all wired to the core with elbow leader lines; ~77 pure-decor animated pieces (spinning rings, pies, reticles, hexagons, ECG monitor, trapezoid assembly, PCB traces, geodesic sphere, batteries, progress bars, perspective grid floor, concentric floor arcs, starfield).
- **Every instrument taps open a detail overlay** (Esc / tap-outside closes) containing the old full panel — Approve/Deny buttons work inside the overlay. Zero data or function was removed; it moved behind taps.
- **Chat dock at the bottom** of the dashboard POSTs to `/chat` and streams the NDJSON reply (status events use `ev.label`, NOT `ev.message` — rendering `.message` produced "undefined" bubbles, already fixed).
- **Quick links** are a glyph-only "orbit" row (Chat, Claude, GitHub, Supabase, Coolify, Composio, Gmail, Calendar, YT Studio).
- **Revenue instrument is manual for now** — value lives in localStorage key `clarvis_revenue` (Update button in its overlay); wiring a real revenue feed is future work.
- **Mobile (≤1100px) falls back to the older stacked-panel layout** (still CLARVIS-branded). `prefers-reduced-motion` freezes all animation.
- Palette went bluer: `--accent: #45d6ff`, new `--accent2: #3d7dff`, bg `#081226`. Template-only changes; zero backend edits.
- Deploy status: `364b1c8` confirmed live by Alex; **final `736fb77` pushed ~midnight but NOT yet confirmed rendering live** — verify with a hard-refresh (Cmd+Shift+R, old page caches) and remember the deploy-queue gotcha below.

### Task Manager fixes (commit `6d6b496`) — all verified locally, deployed with the push
Alex hit two real failures: chat froze 15+ min on "Convening the council", and tasks #84/#92 finished "done" with "(task produced no text output)". Root causes and fixes, all in `task_manager.py`:
1. **Council was glacial, not hung:** `run_managed_task` made up to 19 *sequential* Claude calls (1 plan + 3 per guardrail × up to 6) inside the chat request, no timeouts. Now: Advocate+Critic run concurrently per guardrail and all guardrail councils run concurrently (`ThreadPoolExecutor`); measured end-to-end ask→queued: **26 seconds** (was 15+ min).
2. **Silent truncation ate results:** worker loop treated any non-`tool_use` stop as "finished", but `max_tokens=2000` meant long final reports / big tool calls got cut mid-generation → empty "done". Now `max_tokens=8000`, and a `max_tokens` stop with no tool calls appends a "continue and finish" nudge instead of ending (still bounded by MAX_ROUNDS). #84/#92's research was lost to this.
3. **Timeouts:** council calls `timeout=120.0`, worker calls `timeout=300.0` — a stuck API call can no longer freeze chat indefinitely.
4. **Duplicate guard:** identical `original_request` already queued/running/waiting_approval → returns a pointer to the existing task instead of re-queueing (the #84/#92 double-run was caused by a garbled saved chat reply making the model re-queue).
- Verified by live task #133 (Downloads file count): council seconds, 2 steps, real final report.
- **Still not verified: any managed task running on the *server* instance** — everything above was exercised on the local runtime only.

### Claude Code (the dev tooling, NOT CLARVIS) got design skills
Tasks #84/#92's goal ("make Claude Code better at graphic design") was completed manually: official Anthropic skills **frontend-design** and **canvas-design** installed into `~/.claude/skills/` (sparse-cloned from `anthropics/skills`). They auto-load in new Claude Code sessions and shaped the final dashboard. Note the distinction Alex asked about: these improve Claude Code only — CLARVIS itself has no skill system. Giving CLARVIS its own design capability (distilled design guidance in its prompt + a `create_design` tool rendering SVG→PNG/PDF) was discussed and is an open offer.

### Session gotchas worth keeping
- A broken/partial assistant reply saved into chat history can make the model **re-queue the same managed-task goal** on the next message. The dedupe guard now catches the worst of it.
- The local dev server (`python3 app.py`, port 5001, debug=True) auto-reloads on edits to watched .py files — this both applies fixes and kills in-flight requests/workers. Templates hot-reload without restart.
- Coolify still auto-deploys on push (with the known queue-jam caveat). Browser-pane clicks on the live Coolify UI remain unreliable — verify deploys via container exec, not the UI.

---

## Stack / Infrastructure

- **Hetzner** — CPX11 server ($24/mo, 2 vCPU, 2GB RAM, 40GB SSD), Ashburn, Virginia. IP: `178.156.209.40`. Alex has root SSH access (`ssh root@178.156.209.40`) and used it directly this session — Claude Code does **not** have a standing way to run remote shell commands on this box; anything needed there requires walking Alex through it or getting his explicit per-command authorization.
- **Coolify** v4.1.2 — dashboard at `http://178.156.209.40:8000` (login-gated; Claude Code does not have credentials and must never be given them — Alex logs in himself). GitHub App connected as "second-brain1".
  - Project UUID: `xn159afo226l4480ogtcrznz`, environment UUID: `p78muchurjjfu962yg4iredu` (production).
  - App **`second-brain-chat`** (the actual Jarvis) — UUID `h72tei3gy97z4wlqyqpvuylg`. Live at `http://h72tei3gy97z4wlqyqpvuylg.178.156.209.40.sslip.io`. **Confirmed running commit `053104b` as of 2026-07-19 evening** (verified by shelling into the running container and checking `/app/task_manager.py` + `/app/app.py` timestamps and `JARVIS_RUNTIME=server` env var directly — see Deploy Queue Gotcha below for why the Coolify UI itself was not trustworthy for this check).
  - App **`money-clips-agent`** — UUID `dfjbnh7wz3cvxk29vf3b39vg`. A background content-idea generator, not the chat brain.
- **GitHub** — private repo `Second-brain` under account `alex100hickey-eng`, remote `https://github.com/alex100hickey-eng/Second-brain.git`. Local working copy at `~/second-brain` on Alex's Mac.
  - Second private repo `second-brain-vault` (same account) — a git mirror of the Obsidian vault, used for syncing vault content to the server (see Vault Persistence below).
  - A review branch, **`jarvis/tool-get_word_count`**, still sits on the main repo awaiting Alex's review — the first real artifact of the self-expansion pipeline. Still needs a merge-or-discard decision (unchanged since daytime session).
- **Supabase** — single table `Agent Outputs` (note the space and capital letters — easy to typo) does double duty as the entire app's datastore. Columns: `agent_name` (text), `output_text` (text), `created_at` (timestamptz). RLS disabled. Every internal subsystem piggybacks on this same table with a distinct `agent_name` tag — see "Internal row types" below, now expanded with Task Manager row types.
- **Claude API** — `CLAUDE_API_KEY` env var, set in `~/.zshrc` locally and in Coolify env vars for the deployed app.
- **Composio** (`app.composio.dev`) — used for real-world connectors (currently just Google Calendar, read-only). `COMPOSIO_API_KEY` env var, needs to be a full-access key.
- **Obsidian vault** — named "Second brain" (lowercase b), lives on iCloud Drive at `/Users/alexhickey24/Library/Mobile Documents/com~apple~CloudDocs/Obsidian/Second brain`.
- **Claude Code** — installed on Alex's Mac, Node.js + Composio + GitHub linked.
- **Claude in Chrome / in-app Browser pane** — as of this session, Claude drove Coolify via the sandboxed in-app Browser pane (separate cookie jar from Alex's real Chrome). Alex was logged in there already from a prior session. **Reliability gotcha found this session:** the Coolify "Redeploy" split-button's dropdown (`With rolling update if possible` / `Restart without rebuilding` / `Stop`) did not respond reliably to synthetic clicks even when targeting the correct `ref` from `read_page` — clicks landed with no visible effect and no console errors. Root cause not fully diagnosed (possibly an Alpine.js `x-on:click` trust-event issue). **Do not assume the Redeploy dropdown will work via automation — verify by checking actual deployment state (container exec, not just the UI) rather than trusting a click "succeeded."**

**Secrets policy, non-negotiable:** always env vars, never hardcoded, never pasted into chat, never entered into any web form by Claude on Alex's behalf. When a Coolify field needs a real secret, Claude sets up a placeholder (`REPLACE_ME`) and Alex pastes the real value in himself.

**Remote shell policy (reinforced this session):** Claude Code's permission system requires the user to explicitly name the exact command *and* target host in their own message before Claude may execute a remote shell command (e.g. `docker restart coolify` on `178.156.209.40`) — a bare "yes" or "confirm" to a prior offer is not sufficient. When this gate is hit, the fallback is to walk Alex through running the command himself via his own SSH session and share output back.

---

## What's Built — Jarvis's Current Capabilities

The chat brain lives at `~/second-brain/second-brain-chat/app.py` (+ `templates/`, + `task_manager.py`), a Flask app with a dark HUD-style UI, calling Claude with a tool-use loop. Run locally with `python3 app.py` (port 5001). Deployed via Coolify with `gunicorn app:app --bind 0.0.0.0:5000 --timeout 120` (never change to `python3 app.py` — exposes Werkzeug's debugger).

**The modular tool pattern this app follows for everything:** one entry in the `TOOLS` list (Anthropic tool schema), one plain Python function of the same name, one routing line in `handle_tool_call`. Preserve this shape.

### Core interface
- **Streaming chat**, **server-side chat history**, **Dashboard** (`/dashboard`), **Markdown rendering**, **PWA support** — all as before, unchanged this session.
- **Login gate — NOW ACTIVE.** `JARVIS_PASSWORD` is set in Coolify and confirmed working: an unauthenticated hit to the live URL now redirects straight to `/login` (verified this session by navigating to the dashboard URL and observing the password prompt). This was the #1 outstanding item from the daytime handoff and is now resolved.

### Voice, Background tasks, Memory, Real-world connectors (Calendar read-only, Gmail read-only), the approval layer, Self-expansion, Decision council, File cleaning, Background agents, Vault persistence
All unchanged since the 2026-07-19 daytime session — see git history / prior handoff content for full detail on each if needed. Nothing in this session touched these subsystems.

### Task Manager (NEW this session — Alex's current top priority)
Built in `~/second-brain/second-brain-chat/task_manager.py` (~450+ lines), wired into `app.py` and a new "Managed Tasks" dashboard widget in `templates/dashboard.html`. This is a **separate, more autonomous execution layer** from `delegate_task` — it can pursue an open-ended multi-step goal across many tool calls without a human in the loop for every step, subject to the guardrail/hard-gate system below.

**Flow:** `run_managed_task(goal)` (Prompter) extracts the goal + candidate guardrails → `guardrail_council()` (three independent Claude calls: Advocate / Critic / Judge, structured JSON verdicts) rules on each candidate guardrail, defaulting to *no restriction* unless the Critic convinces the Judge → task queued as a `jarvis_managed_task` Supabase row → a daemon worker (same compare-and-swap claim pattern as `jarvis_task`) runs the tool-use loop, checking every action against the applied guardrails (fail-closed on unparseable verdicts) and a kill switch (`jarvis_taskman_kill` rows, checked every loop iteration). Every step is audited as a `jarvis_taskman_step` row. Tasks carry a `runtime: local|server|any` field; each running instance only claims tasks matching its own runtime, determined by the `JARVIS_RUNTIME` env var (`server` in Coolify, defaults to `local` on Alex's Mac if `~/Downloads` exists).

**Hard gates (non-negotiable, structurally enforced, independent of council verdicts):** spending money, creating accounts, sending things externally, deleting files — these **always** pause for a dashboard approval tap, because managed tasks can only act via the same `handle_tool_call` used everywhere else in the app, and the consequential tools it routes to already require `jarvis_pending_action` approval. The council cannot vote this away.

**Four capability lanes given to managed tasks, each with its own autonomy/gate profile:**
1. **Reversible file operations** (autonomous, no gate) — `fs_list`, `fs_move`, `fs_copy`, `fs_make_folder`. Home-directory-only via hard code-level path guards (no dotdirs, no `~/Library`, no `second-brain` repo, no path traversal). Every operation is logged to a `jarvis_file_undo` trail; `undo_file_operations(task_id)` rolls back moves/copies/folder creations, also exposed as a chat tool for Alex to call directly.
2. **Web access** (autonomous, read-only) — `web_fetch`, `web_search`. Output is explicitly labeled `[UNTRUSTED]` in the string returned to the model, since this is the one place a managed task ingests arbitrary internet content.
3. **Self-expansion via sandbox** (test autonomously, promotion gated) — `sandbox_test_tool(name, code, test_input)` runs Jarvis-written Python instantly under macOS `sandbox-exec`: no network, writes confined to `~/.jarvis_sandbox/task_<id>`, secrets stripped from the environment, sensitive paths denied. `promote_tool(name)` is the gate: it pauses the task (status `waiting_approval`) for **one** dashboard tap; on approval, the tool is hot-loaded with live context for that task only (not persisted globally — a fresh task has to promote it again, or a human has to wire it in permanently the normal way via `create_new_tool`/`adopt_tool`).
4. **Arbitrary shell commands** (gated, one tap each) — `run_shell_command(cmd)` queues to the same `jarvis_pending_action` approval queue as everything else; the task blocks until Alex approves or denies from the dashboard, then executes (or skips) and resumes.

**Verification done this session (all local, on Alex's Mac):**
- Direct test script exercised: path guards reject `/etc/hosts`, `~/.ssh/id_rsa`, `~/Library/...`, the repo itself, and traversal attempts; file-ops-then-undo round trip restores exact prior state; a no-overwrite guard on `fs_move`/`fs_copy` works; `web_fetch`/`web_search` return labeled untrusted content; `sandbox_test_tool` successfully runs a benign tool and successfully **blocks** a tool that tries to read `~/.zshrc` (sandbox profile confirmed effective); the shell-approval wait genuinely blocks (~12s in the test) until a simulated dashboard tap flips the row to `approved`, then executes and resumes.
- Live end-to-end managed task (task #34): a real "inventory my Downloads folder" goal — council applied 4 guardrails, task ran, result and audit trail all showed up correctly in Supabase and the dashboard widget.
- Live end-to-end task exercising the file-ops lane (task #46): organized a Desktop test folder via 16 autonomous `fs_*` calls, no gate hit (as expected — reversible file ops are lane 1, ungated).

**Known quirk:** automated browser form-submission into the chat UI (typing a goal and clicking send) flaked after Flask hot-restarts during development — a Livewire/JS state issue, not a backend bug. Workaround used: POST directly to `/chat` instead of driving the browser form. If this resurfaces, don't assume the backend is broken — check whether a raw POST works first.

**Status: code complete, verified locally, deployed to the server and confirmed present (`/app/task_manager.py` exists in the running container, `JARVIS_RUNTIME=server` is set) — but end-to-end behavior has NOT yet been verified running *on the server* itself, only locally.** See Immediate Next Steps.

### Internal Supabase row types
All piggyback on the one `Agent Outputs` table, filtered out of every agent-output-facing view (the `INTERNAL_AGENT_NAMES` set in `app.py`): `jarvis_memory`, `jarvis_memory_forgotten`, `jarvis_pending_action`, `jarvis_chat`, `jarvis_chat_clear`, and now **`jarvis_managed_task`, `jarvis_taskman_step`, `jarvis_taskman_kill`**.

---

## Known Gotchas Worth Remembering

- **Coolify deploy queue can silently jam, and a container restart can leave a "ghost" deployment.** This session: a deployment (`053104b`, containing the Task Manager) got stuck "Queued" behind an earlier zombie deployment with zero logs. Fix applied: Alex ran `docker restart coolify` directly via SSH on the Hetzner box (root terminal, not the Coolify web terminal — that one only gives you a shell *inside the app container*, not the host; use **Server → Terminal** in the Coolify UI, or real SSH, to reach the host itself). This unblocked the queue, but **the restart also force-killed the in-flight build, so the deployment record itself still shows "Failed" in the Coolify UI, with its "Ended" timestamp bizarrely ticking forward to "now" on every page load** (looks like a display bug for records with a null `finished_at`, not an actual stuck retry loop). **Despite the "Failed" label in the UI, the actual container swap had already completed successfully** — confirmed by shelling into the running container (`docker exec $(docker ps --filter name=<app-uuid> -q) sh -c 'ls /app && echo $JARVIS_RUNTIME'`) and finding the new code and env var already in place. **Lesson: after any deploy drama, verify by execing into the actual running container, not by trusting the Coolify deployments list — it can be stale or cosmetically wrong even when the real state is fine.**
- **The Coolify "Redeploy" split-button dropdown was unreliable via browser automation this session** — see the Stack/Infrastructure note above. If Redeploy needs to be triggered again, try it once, then immediately verify actual state via container exec rather than trusting the click or the deployments list.
- **Remote shell commands need explicit per-command, per-host user authorization** — see Secrets/Remote shell policy above. Don't expect a prior "yes" to cover a retry of the same command; the permission system wants the command and host named freshly.
- **Coolify browser automation in general:** screenshots can render at a different scale than click coordinates depending on viewport width — the narrow default viewport (~720px) truncates the right side of Coolify's header (hiding Redeploy/Restart/Stop entirely); widen the viewport (e.g. 1400px) before trying to find action buttons in the app header.
- **Coolify's "New Environment Variable" modal** is finicky with automated clicks; the "Developer view" toggle (plain multi-line `KEY=value` textarea) is far more reliable for bulk env var edits.
- **Composio SDK:** install `composio` (current), never `composio-core` (abandoned). `composio.tools.execute()` needs `dangerously_skip_version_check=True`. `ConnectedAccounts.initiate()` is deprecated — use `composio.connected_accounts.link(...)`.
- **Nixpacks + Coolify Scheduled Tasks:** the venv at `/opt/venv` isn't on PATH for `docker exec`-based Scheduled Tasks — always call `/opt/venv/bin/python3` explicitly.
- **Status docs (including this one) drift.** A past version of this doc once claimed a phase was "code complete" when the code didn't exist at all. Verify against live repo/Coolify/Supabase state before acting on anything written here, especially after time has passed.

---

## Immediate Next Steps, in priority order

1. **Confirm the final dashboard (`736fb77`) is rendering on the live site.** Hard-refresh `/dashboard` (Cmd+Shift+R — the old page caches). If it hasn't flipped, suspect the Coolify deploy queue jam (see Gotchas) and verify via container exec, not the Coolify UI.
2. **Verify the Task Manager end-to-end on the *deployed* server**, not just locally — now including the `6d6b496` fixes (fast council, timeouts, no-truncation, dedupe). Log into the live site, give CLARVIS a multi-step goal from the live chat, and confirm: council runs in seconds, a `jarvis_managed_task` row appears, the dashboard instruments show it, steps execute, and — critically — a hard-gated action (or the shell/promote lanes) produces a dashboard approval prompt on the *server* instance the same way it did locally. Carried over two sessions now; still not done.
3. **CLARVIS branding pass** — chat page, login page, PWA manifest (`static/manifest.json`, apple-mobile-web-app-title), page titles still say "Jarvis"/"Second Brain".
4. **Review branch `jarvis/tool-get_word_count`** on GitHub — merge it or discard it. Untouched since the daytime session.
5. **HTTPS** — still not attempted. Unlocks mic on the live site; prerequisite for push notifications.
6. **Server-side persistence for `agents/` and `proposed_tools/` drafts** — still ephemeral on the deployed container. Same fix pattern as the vault (persistent volume). The Task Manager's sandbox lane writing to `~/.jarvis_sandbox/task_<id>` is macOS/local-only in practice, so it mainly matters if the server ever needs its own sandboxing story.
7. **Real revenue feed for the dashboard's Revenue instrument** (currently manual localStorage). Likely source: YouTube/AdSense once the content-agent line exists; even a Supabase row CLARVIS updates would beat manual entry.
8. **CLARVIS-native design capability** (distilled design guidance in its system prompt + a `create_design` tool rendering to PNG/PDF in the vault) — discussed with Alex, fits the modular tool pattern, not started.
9. **Calendar edit/delete via the approval queue**, then more approval-gated writes generally (send email, etc. — Gmail is read-only by design).
10. Decide what to do with `agents/stock_watch_agent.py` (review, discard, or approve to run).
11. Specialist agents (Schedule/School/Athletics) and additional money agents — still explicitly low priority per Alex.

---

## Working Style / Principles to Keep

- **Modular tool design** — one `TOOLS` entry + one function + one `handle_tool_call` line per capability. Don't refactor this shape away.
- **One capability at a time, local-first then deploy.** Test locally before deploying; don't stack untested changes.
- **Read-only/reversible now, write/consequential only behind the approval gate — including for the new Task Manager's hard gates, which are non-negotiable regardless of what the guardrail council decides.** Never add a write-capable tool without routing it through the existing pending-action pattern, or without explicitly confirming scope with Alex first.
- **Secrets always via env vars** — never hardcoded, never pasted into chat, never entered into a form by Claude on Alex's behalf.
- **Remote shell commands on the Hetzner box require Alex naming the exact command and host himself** — don't try to work around this with rephrased confirmations; walk him through running it if needed.
- **Narrate what's being done and why, step by step.** Alex wants to understand the system as it's built, not receive a black box.
- **Dashboard aesthetic is sacred:** Alex iterated ~6 rounds to get the sci-fi HUD look matching his reference image. Match it when touching the dashboard; never "simplify" it back toward clean cards. The `frontend-design` skill in `~/.claude/skills` helps.
- **Verify claims against actual files/git/service state before acting on them — for Coolify specifically, that means execing into the real running container, not trusting the deployments list or a UI click's apparent success.**
- **When official docs and an installed package disagree, read the package source** — it's ground truth.
