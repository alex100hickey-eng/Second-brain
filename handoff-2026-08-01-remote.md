# Handoff — 2026-08-01 morning → remote-control session

**Paste or read this at the start of a fresh session (phone-driven remote control
of Claude Code on Alex's Mac).** It is self-contained: current state, what's
running, the one urgent unblock, and where everything lives. Written ~09:30 ET
by the overnight session that built the August fulfillment machine.

> Verify before acting — status docs drift. `/api/version` (server and local)
> and `git log` are the truth sources, never this file or the Coolify UI.

---

## 1. State snapshot (verified ~09:25 ET)

| Thing | State |
|---|---|
| Repo `~/second-brain` | `main`, clean, local HEAD includes `30f1b91` (ad pipeline) + `38f27e8` (review fixes) + `b35cf82` (docs), all pushed except this handoff commit |
| Test suite | **553 passed / 0 failed** (`python3 run_tests.py` from repo root) |
| Mac node | **RUNNING** `38f27e8` — PID may drift; check `curl -s http://127.0.0.1:5001/api/version`. Started via nohup, log at `/tmp/clarvis-mac-node.log`. Has ALL new pipeline tools. |
| Server (Hetzner/Coolify) | App **healthy** on `abb5d74` at `https://clarvis.178.156.209.40.sslip.io` — but **builds are blocked: the box's disk is FULL** (see §3). |
| Vault (Obsidian, iCloud + git-synced) | Synced clean; all plan/business files landed. launchd `vaultsync` pushes every 10 min. |
| launchd agents | `com.secondbrain.vaultsync` and `com.secondbrain.screenrelay` loaded. `morningbrief` deliberately retired overnight (in-app brief + 08:15 nudge replace it). |

**Alex's morning updates (already reflected in NEEDS_ALEX.md):**
- `FLASK_SECRET_KEY` is set in Coolify — takes effect on the next restart/redeploy.
- The `money_clips_agent` scheduled task **does not exist** (verified two ways; last ran 07-28). Do not hunt for it.

## 2. Keep-running list (things a session must not kill)

- **Mac node** (`python3 app.py` in `~/second-brain/second-brain-chat`, port 5001).
  If it dies: `cd ~/second-brain/second-brain-chat && nohup python3 app.py > /tmp/clarvis-mac-node.log 2>&1 &`
  A healthy boot prints `Startup self-check: DEGRADED` — that is NORMAL (optional
  search keys absent; every REQUIRED check passes).
- The two launchd agents above (they manage themselves).
- The server app container (don't Stop/Restart it in Coolify — a restart is fine
  once disk is fixed, and will pick up FLASK_SECRET_KEY).

## 3. 🔴 THE one urgent unblock: server disk is full

Full detail + exact commands in **`NEEDS_ALEX.md` §0a**. Short version: the
`54dbf8d` build died on `No space left on device`; Coolify showed it "In
Progress" for 8.5h (cancelled — queue unwedged); Coolify's own Redis now refuses
writes. Nothing builds until disk is freed. Fix is **Alex running** (Claude must
not run remote shell unless Alex names the exact command AND host in his own message):

```bash
ssh root@178.156.209.40 "df -h / && docker builder prune -af && docker image prune -af && df -h /"
```

Then push anything (this handoff commit is sitting unpushed for exactly this) and
verify `curl -s https://clarvis.178.156.209.40.sslip.io/api/version` → `38f27e8`+.
If a build wedges "In Progress" with no log output ≥10 min: open the deployment
page and click Cancel (a JS `.click()` on the Cancel button works when the pane
click doesn't). Coolify's list lies in both directions — only `/api/version` counts.

## 4. The business context (why last night happened)

The operative plan is **`Money/August Money Plan (FINAL).md`** in the vault
("Second brain"/Money) — council-amended (Supabase council row #4763, on the HUD).
Goals fixed by Alex: $1,000/mo automated by Aug 31; $7-10k/mo at 6-10 hrs/wk on
the honest clock. Lane: ad-creative service for small/mid DTC brands sold as a
"creative testing engine" — the word "AI" appears in NO client-facing artifact.

Built and live on the Mac node (`ad_creative_pipeline.py`, 8 chat tools):
`ingest_brand → generate_angles → produce_variants → qa_check → package_delivery`,
plus `build_client_report`, `draft_outreach`, `check_ad_pipeline`. Smoke-tested
end-to-end on three real brands (Portland Pet Food Company, Golde, Fishwife) —
their draft spec packs + sample readouts are in vault `Money/Clients/<slug>/`,
outreach drafts in `Money/outreach-drafts-2026-08-01.md`.

Ready ammunition in the vault: `Money/prospect-tracker.csv` (97 candidates,
pre-built Ad Library lookup URLs), `Money/target-list-notes.md` (Alex's 3-hour
qualification instructions), `Money/Templates/` (agreement, invoice, 6 outreach
emails, proposal with retainer price pre-written, pre-call brief + call script,
sample readout, client intake). Portfolio site scaffold: repo `portfolio-site/`
(blocked on Alex naming the service).

**Alex's ordered checklist is the plan's "FIRST 72 HOURS" section** — starts with
naming the service (gates domain → mailbox → 7-day warmup clock → Wave 1).

## 5. Hard rules for any session (violating these is worse than doing nothing)

- **No autonomous outbound, ever.** CLARVIS/Claude drafts; Alex reviews and
  sends every email by hand. There is deliberately no send path in the pipeline.
- **Remote shell on the Hetzner box** only when Alex names the exact command AND
  host in his own message. SSH key auth works — the gate is policy, not access.
- **Secrets** live in env vars (`~/second-brain/.env`) — never in chat, never
  typed into Coolify by Claude.
- **Every fix gets a regression test** in `run_tests.py`; verify what ships
  (live probes), not what compiles.
- Human gates on money/accounts/external sends/deletion are structural — never
  route around them. Coolify config edits are Alex's.

## 6. Quick verification cheat-sheet

```bash
# Local node alive + on what code
curl -s http://127.0.0.1:5001/api/version
# Server alive + on what code (only trustworthy deploy signal)
curl -s https://clarvis.178.156.209.40.sslip.io/api/version
# Suite
cd ~/second-brain && python3 run_tests.py            # full (~4 min)
cd ~/second-brain && python3 run_tests.py --only=adpipeline
# Pipeline status via the app itself (or just ask CLARVIS "check the ad pipeline")
# Vault sync health
cd ~/Library/Mobile\ Documents/com~apple~CloudDocs/Obsidian/Second\ brain && git log --oneline -2
```

## 7. Open threads, smallest first

- Push this handoff commit once disk is fixed (it's the "push anything").
- Server restart after deploy also activates the new `FLASK_SECRET_KEY`.
- Recurring-risk note: if disk refills, a scheduled `docker builder prune`
  with a keep-storage cap is the durable fix (design note in NEEDS_ALEX §0a).
- Accepted residuals from the adversarial review (documented in NEEDS_ALEX §0):
  honor-gated `client_approved_proof` arg; `_all_rows` 300-row cap; inherited
  SSRF DNS-rebinding TOCTOU. None block August.
- September-filed: job-board vertical research (#3243), Apify re-scoring — both
  feeder tasks failed pre-07-31-fix; re-queue on the fixed worker if wanted.
