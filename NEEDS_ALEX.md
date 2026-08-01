# NEEDS_ALEX.md — everything blocked on you

**Updated 2026-07-31 evening. Suite 447/0. Everything is deployed and verified
live on `c6a2829`.** The long-running deploy outage is over — see "Resolved
today" below for what it actually was, because the cause was not what the
earlier version of this file guessed.

Ordered so the first item unblocks the value of everything else.

---

## 0a. 🔴 URGENT 2026-08-01 morning — the Hetzner box's DISK IS FULL again

Found while deploying the overnight work. Evidence: the `54dbf8d` build died at
04:32 UTC with `mkdir: No space left on device` at the nix step (Coolify showed
it "In Progress" for 8.5h — cancelled it, which unwedged the queue), and the
Coolify UI itself now 500s with "Redis … unable to persist to disk". The RUNNING
app is fine (serving `abb5d74`, which includes the jobs.db fix), but **no new
build can land and Coolify's own state can't persist** until disk is freed.
Note: vault pulls and jobs.db writes share that disk — don't sit on this.

Fix (you run it; from your Mac):
```bash
ssh root@178.156.209.40 "df -h / && docker builder prune -af && docker image prune -af && df -h /"
```
(`image prune -af` deletes rollback images — you okayed weighing this before;
`builder prune` alone freed ~9 GB last time and may be enough. While you're
there, the pending kernel reboot from §7 is 15 seconds: `reboot`.)

Then deploy the tip — push anything, or:
```bash
cd ~/second-brain && git commit --allow-empty -m "redeploy" && git push
```
Verify: `curl -s https://clarvis.178.156.209.40.sslip.io/api/version` → `38f27e8`
(or later). Two suite-green commits are waiting: `30f1b91` (the ad pipeline) +
`38f27e8` (review fixes). **Your Mac node already runs them** — restarted on
`38f27e8` at 09:04 ET, so CLARVIS has the full pipeline locally regardless.
Worth asking why disk refilled: 8 builds ran 03:00-04:30 UTC; if this recurs,
a scheduled `docker builder prune` (keep-storage cap) is the durable fix.

---

## 0. NEW 2026-08-01 — August Money Plan: your morning list

The finalized plan (council-amended) is in the vault: **`Money/August Money Plan
(FINAL).md`**. Its "FIRST 72 HOURS" section is your complete ordered checklist.
The two items that touch this repo's infra:

- **Set `FLASK_SECRET_KEY` in Coolify** (app `second-brain-chat`): any long random
  string (e.g. `python3 -c "import secrets;print(secrets.token_hex(32))"`). The code
  already prefers it; today sessions key off the access code. 2 minutes.
- **Pause the `money_clips_agent` Scheduled Task in Coolify** (resource
  `money-clips-agent`): it still burns a daily API call generating YouTube Shorts
  concepts for the superseded strategy. 10 seconds.

Done overnight so you don't have to: jobs.db now survives server redeploys
(parks on the vault volume under `.appstate/`, `JOBS_DB_PATH` overridable), and
the duplicate Mac morning-brief launchd job is retired (plist archived at
`scripts/archive/com.secondbrain.morningbrief.plist.disabled` — the in-app
brief + 08:15 phone nudge remain the single brief path).

ALSO done overnight (`30f1b91`): the full fulfillment machine
(`ad_creative_pipeline.py`, 8 tools, suite 547/0), smoke-tested end-to-end on
THREE real brands — draft spec packs, sample readouts, and outreach drafts are
in the vault under `Money/` awaiting your taste pass. The council's smoke-test
gate is already satisfied; your morning list starts at "name the service."
Accepted residuals (documented, low risk): `client_approved_proof` is an
honor-gated tool arg (anonymized output, your own DB); `_all_rows` caps at 300
rows (fine at August scale); the SSRF guard's DNS-rebinding TOCTOU is inherited
app-wide, not new here.

---

## 1. ✅ RESOLVED — the deploy outage (root cause found)

The server had been stuck on `0efd2a7` (2026-07-25) for six days. The earlier
guess in this file — a dead webhook / bad `pyautogui` requirement — was **wrong**.

**Actual cause:** the Hetzner box is **2 GB RAM with zero swap**. With the app,
Coolify and Docker resident, only ~170 MB was free. Every build froze at nixpacks
step `#8 RUN nix-env -if ... && nix-collect-garbage -d` — the memory peak — and
Coolify reports a starved build as "In Progress" **forever** rather than failing.
One such zombie sat for 10 hours and wedged the whole queue behind it, which is
what made it look like deploys "weren't triggering". The webhook was fine all
along; every push did queue a deployment.

**Fix applied 2026-07-31:** 2 GB swap file added (`/swapfile`, in `/etc/fstab`,
survives reboot) and `docker builder prune -af` run (~9 GB reclaimed). The very
next build shipped in **~5 minutes**, versus 43-48 minutes when starved.

**Diagnosis, if it ever recurs:** a build with no new log output for >10 minutes
at step #8 is starved, not slow. Check `free -m` first. `/api/version` and the
static-file byte-fingerprint are the only trustworthy deploy signals — the
Coolify deployments list has reported both false failures and false successes.

**Also learned:** Coolify's **Redeploy** button rebuilds the previously pinned
commit, NOT the branch tip. To deploy new code, push (the webhook builds HEAD).

---

## 2. ✅ DONE (verified 2026-07-31) — macOS permissions granted

Alex granted both. Verified live from a shell:
`AXIsProcessTrusted()` → **True**, `CGPreflightScreenCaptureAccess()` → **True**.
(If the app is ever launched by a *different* parent app than the one granted,
re-check with the same two calls.)

---

## 3. ✅ DONE — voice conversation mode

Alex tested it 2026-07-31: "the mic works great now." No threshold tuning was
needed; `SILENCE_MS` / `MIN_SPEECH_MS` in the VAD block of `templates/index.html`
stay as shipped.

---

## 4. ✅ DONE — `screen_agent.py` wired in (Alex approved 2026-07-31)

Registered inside the Mac-only branch of `app.py` (after `import screen_control`),
dispatched in `handle_tool_call`, status label "Driving your screen…". On the
server the tool refuses with a message rather than relaying — the see->act loop
must run where the mouse is. The pinning test in `run_tests.py` was flipped from
"must NOT be wired" to guarding the wiring, with both halves negative-tested.

⚠️ **It only loads when the Mac-side app is running.** ✅ Started 2026-08-01 and
verified: `screen_control` + `screen_agent` import cleanly, `RUNTIME=local`, so the
screen tools register. The Mac node serves on **http://127.0.0.1:5001** (not 5000).

To start it yourself after a reboot:

```bash
cd ~/second-brain/second-brain-chat && python3 app.py
```

There is no LaunchAgent for the main app — it's deliberate and manual. (The
`screenrelay`, `morningbrief`, and `vaultsync` agents ARE installed and load on their
own.) A healthy boot prints `Startup self-check: DEGRADED` — that is expected and only
means the optional Tavily/Serper/Brave search keys aren't set, so search falls back to
keyless DuckDuckGo. Every REQUIRED check passes.

---

## 5. ✅ DONE — the quick wins

- **`GITHUB_TOKEN`** — added to `~/second-brain/.env` and verified live (5000/hr
  core, 30/min search, up from 10/min unauthenticated). Also added to Coolify.
- **SSH key** — `~/.ssh/id_ed25519` generated and installed on the box with
  `ssh-copy-id`; `ssh root@178.156.209.40` is now passwordless.
- **Stale expansion findings** — the review queue is fully drained: 9 rejected,
  5 deferred, 0 pending. The job-scraper items scored 2/5 usefulness and were
  rejected by the council.

---

## 6. ⏳ Waiting on time, not you

- **Tool prune** — 93 registered, 12 used on the Mac. The cross-node audit mirror shipped
  07-30; give it ~a week of real two-node data, then we prune with evidence. See `TOOL_AUDIT.md`.
- **`app.py` is ~5,600 lines** — real structural debt, not urgent. Best done *with* the prune.
- ~~**Dependency pinning**~~ ✅ DONE 2026-07-31 — 15 of 16 pinned in
  `second-brain-chat/requirements.txt`. `gunicorn` stays unpinned on purpose: it's
  server-only, so there's no locally-verified version to pin it to.

---

## What I did NOT need you for

15 commits, 435 passing checks, and one real security hole (SSRF) found and closed —
see `VIBE_CODE_AUDIT.md`, `TOOL_AUDIT.md`, and today's git log.

---

## 7. 🟡 Genuinely still open (small)

- **Look at the phone HUD.** The instrument bands shipped and are verified in the
  served assets, but nobody has looked at them on a real phone yet.
- ~~**Heartbeat triage**~~ ✅ DONE 2026-08-01. All four heartbeats fresh (retention,
  proactive, mail-intake, expansion-scout). No `error` or `critical` event since
  2026-07-24 — those were transient Supabase timeouts and Cloudflare 525s that stopped
  on their own. The only recent `warning`s are `login lockout tripped` from
  `ip=127.0.0.1`, which is the local test suite tripping its own gate, not an intrusion.
  Nothing to forward.
- **Kernel reboot** — the box prints `*** System restart required ***`. Safe to do
  now that builds aren't fragile.
- **~9 GB of unused Docker images** could be reclaimed (`docker image prune -af`),
  but that deletes rollback targets, so it's a deliberate choice, not routine.
- **Dependency pinning is done** (15 of 16 exact; `gunicorn` left unpinned because
  it isn't installed on the Mac, so no locally-verified version exists). Server
  confirmed **Python 3.12** from the build log.
- ~~**`under_review` orphan bug**~~ ✅ FIXED and LIVE 2026-08-01 (`5f47e26`). Findings
  now stamp `review_started_at`; anything stranded in `under_review` past 10 minutes is
  reclaimed by the next pass, and any exception in the council resets the finding to
  `found` and surfaces the error instead of swallowing it. Regression tests negative-test
  both halves (a stale one IS picked up; a fresh in-flight one is NOT). Verified against
  the live findings table.
