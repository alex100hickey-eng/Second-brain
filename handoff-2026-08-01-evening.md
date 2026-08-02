# Handoff — 2026-08-01 evening

**Read this first in a fresh session.** Self-contained: verified state, what
changed today, what's blocked on Alex, and the rules that are not negotiable.
Written ~19:45 ET.

> Verify before acting — status docs drift. `/api/version` on both nodes and
> `git log` are the truth sources, never this file and never the Coolify UI.

---

## 1. State snapshot (verified 19:45 ET, not copied forward)

| Thing | State |
|---|---|
| Repo `~/second-brain` | `main`, clean, **everything pushed**. HEAD `5ab9c4f`. |
| Test suite | **672 passed / 0 failed** (`python3 run_tests.py`, ~5 min) |
| Mac node | **RUNNING** `5ab9c4f`, disk 90% (21.8 GB free). 17/17 required self-checks pass. |
| Server (Hetzner/Coolify) | **HEALTHY** on `5ab9c4f`, disk **36%** (23.7 GB free) |
| Deploy pipeline | **WORKING** — push → live in ~90 seconds, verified three times today |
| Vault | Synced (`289ad71`), launchd pushes every 10 min |
| launchd agents | `com.secondbrain.vaultsync`, `com.secondbrain.screenrelay` loaded |

The long-running deploy outage is **over**. Both nodes run the same commit.

## 2. Keep-running list (do not kill)

- **Mac node** — `python3 app.py` in `~/second-brain/second-brain-chat`, port 5001.
  If it dies: `cd ~/second-brain/second-brain-chat && nohup python3 app.py > /tmp/clarvis-mac-node.log 2>&1 &`
  A healthy boot prints `Startup self-check: DEGRADED` — **that is NORMAL** (three
  optional search keys absent; all 17 required checks pass).
  Note: `pkill -f "python3 app.py"` does **not** match it — the process runs as
  framework `Python app.py`. Use `lsof -nP -iTCP:5001 -sTCP:LISTEN -t`.
  Also: **Flask caches templates**, so any `templates/*.html` edit needs a restart.
- The two launchd agents (self-managing).
- The server app container. The **`coolify` control-plane** container can be
  restarted safely — the app serves throughout.

## 3. What changed today (all shipped, tested, live)

**Infrastructure — the disk problem is solved at the root.**
The box had filled twice. Three stacked failures, found in this order:
1. Disk 100% full (271 MB free) → `docker builder prune -af` freed 9.8 GB.
2. Deploy #251 stuck `in_progress` since 13:29 with four orphaned `coolify-helper`
   containers — everything queued behind a corpse. Cleared them.
3. The queue *worker* was dead: when the disk filled, Redis lost the job payloads,
   leaving DB rows with no job behind them. Restarting `coolify-redis` + `coolify`
   fixed it.

Root cause: **every deploy leaves a ~1.94 GB image and nothing removed the old one
— 25 had piled up in 26 hours.** Alex chose keep-3 retention. `server-disk-guard.sh`
is installed on the box (hourly root cron at :17, logs to `/var/log/disk-guard.log`);
first run removed 19 images, 89% → 48%. It never touches an image backing a running
container and never runs `image prune -af`.

- `/api/version` now carries a `disk` block on both nodes.
- `scripts/check_disk.py` polls both, escalating by **per-node** bands (server
  75/85/92, Mac 88/94/97 — the Mac normally runs full; identical bands would fire
  CRITICAL every run and train Alex to ignore it).

**The August plan now runs through CLARVIS.**
- `Money/August Execution Tracker.md` in the vault is the **source of truth**:
  17 steps with owners, due dates and `needs:` dependencies. Any Claude Code
  session can pick it up from there without being re-briefed.
- Tools `check_august_plan` and `complete_august_step`. Ticking a checkbox in
  Obsidian and telling CLARVIS are **equivalent** — completion is unioned across
  the vault and Supabase, because Alex's phone reaches the *server*, whose vault is
  a pull-only mirror where a ticked box would be wiped by the next pull.
  `reconcile_vault()` writes back from the local node only.
- The 15-min awareness pass nudges his phone (ntfy configured). **Blocked steps are
  never nudged** — that's load-bearing, not a nicety.
- Morning brief leads with it.

**Its own HUD tab.** Tile on the deck → `/august`, nine panels (clock, next move,
needs you, waiting on CLARVIS, blocked, gates, fulfillment, prospects, guardrails),
own feed `/api/august`, 60s poll. Verified in a real browser at 390×844 and
1512×950 — which caught panels clipping real data (hiding 5 of 11 blocked steps),
a collision with shared chrome, and a 4px tile clip. Long lists now cap with "+N more".

**Client-facing safety.** `portfolio-site/render.py` turns the naming decision into
one command and refuses to emit a page with a leftover placeholder or the word "AI".
`scripts/check_client_doc.py` lints anything a client reads — the proposal template
carries a "delete before sending" block whose own text says never to call the work
AI-generated.

## 4. 🔴 What needs Alex — in order

**The only thing on the critical path is naming the service.** It gates the domain →
mailbox → the 7-day warmup clock → every send. As of today that's 30 days to Aug 31
and 23 selling days *if the mailbox goes live immediately*; each day of delay costs
one. Nothing else competes with this.

Then: buy the .com; Google Workspace mailbox + SPF/DKIM/DMARC; taste-pass a sample
drop (validates whether the offer is even good — independent of the name, worth
doing today); Stripe + ACH + tax rule; qualify the 97 prospects; daily warmup emails.

**Live status is in the tracker, not in this file.** Run:
`cd ~/second-brain/second-brain-chat && python3 -c "import august_tracker as at, os; at.init(None, os.path.expanduser('~/Library/Mobile Documents/com~apple~CloudDocs/Obsidian/Second brain'), None, 'local'); print(at.summary_text())"`

**Security: rotate `VAULT_GIT_TOKEN`.** It was printed into a chat transcript today
(I dumped container env unfiltered instead of grepping). Revoke and reissue on
GitHub, then update it in Coolify. Not known to be abused; rotate anyway.

**Also open:** Mac disk at 90% (Chrome cache 7.1 GB needs Chrome quit to reclaim;
`~/Movies` is 15 GB). Kernel reboot pending on the box. Phone HUD instrument bands
still unseen on a real phone.

## 5. Hard rules — violating these is worse than doing nothing

- **No autonomous outbound, ever.** CLARVIS drafts; Alex reviews and sends every
  email by hand. There is deliberately no send path in the code.
- **The word "AI" appears in no client-facing artifact.** Lint with
  `scripts/check_client_doc.py` before anything goes out.
- **No unapproved brand shown as work.** The three spec packs were built
  unsolicited from public ads; those brands have never been contacted. The
  portfolio renders category labels by default and requires `--brands-approved`
  to name anyone.
- **Remote shell on the Hetzner box** is gated: Alex names the command and host.
  *Today he explicitly delegated it* ("I can't run anything, I'm remote control —
  if you want to run anything in terminal do it") because he was driving from his
  phone. **Treat that as spent, not standing** — re-confirm before SSHing again.
- **Secrets** live in env vars / `.env`, never in chat. Grep for the specific
  variable; never dump `printenv` wholesale (that's how the token leaked).
- **Every fix gets a regression test** in `run_tests.py`. Verify what ships with
  live probes, not what compiles.
- **All UI work follows `HUD_STYLE.md` exactly** (see `second-brain-chat/CLAUDE.md`).
  Verify UI in a real browser — three real bugs today were invisible in the code.
- Human gates on money, accounts, external sends and deletion are structural.

## 6. Verification cheat-sheet

```bash
curl -s http://127.0.0.1:5001/api/version                       # Mac: commit + disk
curl -s https://clarvis.178.156.209.40.sslip.io/api/version     # server (only real deploy signal)
cd ~/second-brain && python3 run_tests.py                       # full suite (~5 min)
python3 run_tests.py --only=august,version,portfolio            # today's areas
python3 scripts/check_disk.py                                   # both nodes' disk
cd ~/Library/Mobile\ Documents/com~apple~CloudDocs/Obsidian/Second\ brain && git log --oneline -2
```

Deploy: push to `main` → live in ~90s. If `/api/version` doesn't move in ~5 min,
check `application_deployment_queues` in `coolify-db` for a row stuck `in_progress`
(see §3 — that exact wedge happened today and the fix is documented).

## 7. Where things live

| What | Where |
|---|---|
| **Execution state (start here)** | vault `Money/August Execution Tracker.md` |
| Strategy | vault `Money/August Money Plan (FINAL).md` |
| Blocked-on-Alex infra | `NEEDS_ALEX.md` (§0a is now RESOLVED with the full story) |
| Prospects (97) | vault `Money/prospect-tracker.csv` |
| Templates, sample drops | vault `Money/Templates/`, `Money/Clients/<brand>/` |
| Fulfillment pipeline | `second-brain-chat/ad_creative_pipeline.py` |
| Plan state machine | `second-brain-chat/august_tracker.py` |
| The HUD tab | `templates/subpage.html` (`august:` key), `/api/august` in `app.py` |
| Disk tooling | `scripts/check_disk.py`, `scripts/server-disk-guard.sh` |

## 8. Honest read on where this stands

The machine is built, tested, deployed, monitored, and now has its own instrument
panel. **Zero prospects have been contacted and no money has moved.** The risk has
moved from "can it be built" to "will anyone pay", and only the second one matters
now. More tooling is motion, not progress.

If a session finds itself building infrastructure, stop and check the tracker: the
top row has said *name the service* all day, and everything else is downstream.
