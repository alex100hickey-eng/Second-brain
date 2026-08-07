# NEEDS_ALEX.md — everything blocked on you

## 2026-08-07 — ambient awareness + protocols shipped; calendar OAuth is DEAD

CLARVIS now has a JARVIS-style ambient layer: every turn it already knows the
time, today's calendar, your top tasks, what's waiting on you, and what's running
in the background — plus **protocols** (standing orders: "when I say game day, do
X then Y" → saved to vault `Protocols/`, run by name). Two things on you:

1. **Your Google Calendar connection is gone.** Composio returns "No connected
   account found for entity 'alex' / toolkit googlecalendar" — every calendar
   feature (Today widget, briefing, the new ambient block, event proposals) is
   flying blind until you re-auth. Fix: run
   `python3 scripts/connect_google_calendar.py` from `~/second-brain` and finish
   the Google OAuth screen it opens. ~2 minutes.
2. **Push when ready** — today's work (person profile + situational awareness +
   protocols + interaction doctrine) is committed on main but NOT pushed, so the
   live server doesn't have it yet: `cd ~/second-brain && git push` (auto-deploys).


## 2026-08-02 morning — full mail read + draft replies + self-service escalation shipped

CLARVIS can now: **read every email** (raw, all three accounts — `list_emails`/
`read_email`), **write replies as real Gmail drafts** you send yourself
(`create_email_draft` — sending is deliberately impossible, enforced by a test),
and **file its own feature requests** (`request_capability`) into a queue that a
scheduled Claude Code task on your Mac processes every 30 min — you're out of the
middleman job. Commit `fdb492c`. Three small things on you:

1. **Click "Run now" once** on the `clarvis-capability-processor` task (Claude
   app → Scheduled section in the sidebar) — the first run asks for tool
   permissions; approving them once means future runs never stall on prompts.
   A real request is already queued (the misleading "0 new, 0 filtered" scan
   summary you hit this morning), so that first run will also ship a fix.
2. **The processor only runs while the Claude desktop app is open** on your Mac.
   No action needed, just know that's the heartbeat.
3. **Delete the test draft** "CLARVIS draft test — safe to delete" sitting in
   your personal Gmail Drafts (proof the draft chain works).

---

**Updated 2026-07-31 evening. Suite 447/0. Everything is deployed and verified
live on `c6a2829`.** The long-running deploy outage is over — see "Resolved
today" below for what it actually was, because the cause was not what the
earlier version of this file guessed.

Ordered so the first item unblocks the value of everything else.

---

## 0a. ✅ RESOLVED 2026-08-01 evening — disk freed, deploy landed, recurrence fixed

Alex delegated terminal execution ("I can't run anything, I'm remote control — if
you want to run anything in terminal do it"), so this was carried out rather than
handed over. What happened, in order:

- **Disk was 100% full, 271 MB free.** `docker builder prune -af` freed 9.8 GB
  (100% → 80%). Rollback images deliberately untouched at that stage.
- **The push still didn't deploy.** Root cause was NOT the disk: deployment #251
  had been stuck `in_progress` since 13:29 with four orphaned `coolify-helper`
  containers, so every later deploy queued behind a corpse. Cleared the zombies,
  retired the stale queue rows.
- **It still didn't deploy** — the queue *worker* was dead too: when the disk
  filled, Redis lost the job payloads, leaving DB rows with no job behind them.
  Restarting `coolify-redis` + `coolify` (control plane only; the app container
  served throughout) fixed it. A fresh push then built normally.
- **Live on `d161281`**, verified by `/api/version`, which now carries the `disk`
  block on both nodes. `FLASK_SECRET_KEY` is active as of that restart.
- **The real recurrence cause, found and fixed:** every deploy leaves a ~1.94 GB
  image and nothing removed the old one — **25 had piled up in 26 hours (~20 GB)**.
  The guard now keeps the **newest 3 generations per app** (Alex's call, chosen
  over keep-5 / leave-alone / `prune -af`). It never deletes an image backing a
  running container. First run removed 19 images, skipped 0: **89% → 48%,
  4.1 GB → 19 GB free**, with `d161281` + two rollback targets preserved.
- **Installed** at `/usr/local/bin/server-disk-guard.sh`, hourly root cron at :17,
  logging to `/var/log/disk-guard.log`.

Residual worth knowing: the box is 38 GB total and each build needs ~2 GB. Keep-3
holds steady state near 48%, so there's real headroom now. `scripts/check_disk.py`
warns long before it matters.

<details>
<summary>Original entry (kept for the record)</summary>

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

**Nothing has been pushed.** The tip commits are sitting local on purpose: a push
auto-triggers a build, that build dies on the full disk, and last time a dead
build wedged the queue for 8.5 hours. Free the disk first, then push once.

### The durable fix is now built and waiting for you (2026-08-01 midday)

The recurrence risk in this section is closed in code — it just needs installing,
because it runs on the box and remote shell is your call, not Claude's.

1. **The box now announces its own disk before it fills.** `/api/version` carries a
   `disk` block (percent used, GB free), so the endpoint you already curl to verify
   a deploy also answers "is it about to wedge?". Both nodes degrade gracefully:
   the field is simply absent on older code.
2. **`scripts/check_disk.py`** polls both nodes and escalates by band — quiet under
   75%, prints at 75%, and files a `system_event` (so it lands in the incident log
   and the HUD) at 85% / 92%. Run it hourly or by hand: `python3 scripts/check_disk.py`.
3. **`scripts/server-disk-guard.sh`** is the hourly root cron for the box. It prunes
   the *build cache under a 10 GB keep-storage cap* — not `-af`, so ordinary rebuilds
   stay warm — and only under real pressure (>70%). It deliberately **never** runs
   `docker image prune -af`; deleting rollback targets stays your deliberate call,
   and the test suite pins that. Install (two lines, both yours to run):
   ```bash
   scp ~/second-brain/scripts/server-disk-guard.sh root@178.156.209.40:/usr/local/bin/
   ssh root@178.156.209.40 'chmod +x /usr/local/bin/server-disk-guard.sh && \
     (crontab -l 2>/dev/null | grep -v server-disk-guard; \
      echo "17 * * * * /usr/local/bin/server-disk-guard.sh >> /var/log/disk-guard.log 2>&1") | crontab -'
   ```
   Do this *after* the manual prune above — the guard prevents the next fill, it
   won't dig you out of this one.

</details>

---

## 0b. ✅ 2026-08-01 LATE NIGHT — the service is NAMED and the mail infra is DONE

**Splitframe Studio · splitframestudio.com · alexhickey@splitframestudio.com**

Alex picked the name from CLARVIS's shortlist and bought the domain ($11.08/yr,
Porkbun, WHOIS privacy on) and the Workspace seat (Business Starter). Everything
else was carried out and dig-verified the same night: MX → Google (sole route),
single SPF, **DKIM 2048 authenticating**, DMARC `p=none` with reports to the real
mailbox, Porkbun's parking MX/SPF defaults deleted, Postmaster Tools verified,
and a **mail-tester baseline of 10/10** ("Perfect, you can send") from a real
Gmail send. Tracker: **4/17 done** (`name-service`, `buy-domain`, `mailbox-dns`,
`dns-strings`), ticked in the vault.

**Update 2026-08-03 14:30 — site is LIVE.** ~~not yet public~~ splitframestudio.com
serves HTTP 200 over valid TLS (cert issued 2026-08-03, expires 11-01), A records
on GitHub Pages, `www` CNAME resolving. Re-verified by dig + curl, not by UI claim.
Mail DNS re-checked the same way and still clean: sole Google MX, single SPF, DKIM
published, DMARC `p=none`, no parking leftovers.

**Alex's remaining clicks, in order (each is minutes, not hours):**

1. ~~**Read the portfolio copy / say "ship it"**~~ ✅ DONE — deployed and verified
   live 2026-08-03.
2. ⚠️ **Warmup day 1 — NOT STARTED. This is the critical path.** The 3 drafts are
   still sitting unsent in the splitframestudio Gmail Drafts folder. Verified
   2026-08-03: a search of alex100hickey@gmail.com for anything from
   splitframestudio.com over the last 5 days returns **zero** messages (control
   query on the same mailbox returned 22 threads, so the search path works — the
   mail genuinely has not gone out). **The 7-day clock has not started, so it is
   not day 1 of warmup yet — it is day 0.** Every downstream date slides with the
   day you actually send: earliest Wave 1 send is first-send + 7 days, behind the
   mail-tester re-check gate. Send from **splitframestudio Gmail → Drafts**, 3
   spaced across the day, then reply to each from your gmail. Plan: vault
   `Money/Warmup Plan.md`.
2b. **Read the 40 drafts — Waves 1, 2 and 3, all ready.** Vault
   `Money/outreach-drafts-wave1-2026-08-03.md` (22),
   `-wave2-` (14), `-wave3-` (4). Generated 2026-08-03 against each brand's live
   Ad Library creative; **40/40 succeeded, 0 failures**. All lint clean
   (`check_client_doc.py` re-run independently, exit 0: no "AI", no vendor names,
   no placeholders, no internal markers). All 40 verified inside your 5-100
   active-ad band, no overlap between waves, and **every qualified brand in the
   tracker is now assigned to a wave**. Nothing sends itself — these wait on the
   warmup gate, which is item 2.
2c. **14 brands sit in your 101-199 "flag for me" band** — decisions only you can
   make; scraping does not resolve them. This is now the single biggest pool of
   held-up prospects, bigger than any wave after wave 1:

   | brand | active ads | domain |
   |---|---|---|
   | Guava Family | 190 | guavafamily.com |
   | Divi | 190 | diviofficial.com |
   | Native Pet | 160 | nativepet.com |
   | Nani Swimwear | 160 | naniswimwear.com |
   | UrbanStems | 150 | urbanstems.com |
   | ROAD iD | 150 | roadid.com |
   | SheFit | 130 | shefit.com |
   | Canvas Beauty | 130 | canvasbeautybrand.com |
   | Needed | 120 | thisisneeded.com |
   | Momentous | 120 | livemomentous.com |
   | Fishwife | 120 | eatfishwife.com |
   | Apothékary | 120 | apothekary.co |
   | The Outset | 110 | theoutset.com |
   | Pet Honesty | 110 | pethonesty.com |

   Say "in" or "out" per brand (or one blanket call — e.g. "anything under 150 is
   in") and CLARVIS moves them into a wave and drafts them.
2d. **3 of the 7 `identity_mismatch` rows look like matcher false positives**, not
   real mismatches — worth ~30 seconds each:
   - **Apothékary** — page stored as literal escape `Apothékary`, so the
     string compare failed. Almost certainly the right page. 120 active → if you
     confirm, it belongs in the 101-199 flag list above, not in limbo.
   - **Bask and Lather** — page `Bask & Lather Co` (`&` vs `and`). 570 active →
     if confirmed, it is `too_big`, not a candidate.
   - **Big Barker** — page `Barker Dog Beds`, 24 active. If that is their trading
     page, it qualifies cleanly inside 5-100.

   The other four (OffLimits → "Kapil tony works", Doe Lashes → "Jolynn Brant",
   Divi → no page, Create Wellness → "Trycreate") are either genuine wrong-page
   hits or have no data. Not reclassified automatically — who receives outreach
   is your call, not a script's.
2e. **Root-caused why qualification was stalling — two parser bugs, both now
   understood, and 9 brands recovered from them (no action needed from you).**
   The 58-brand retry recovered only 3, so instead of running it again the
   failures were diagnosed directly:

   - **`page_not_found` (was 17)** — nothing to do with handles. Facebook serves
     logged-out headless Chrome a *bare shell* for `facebook.com/<handle>`: 333KB,
     title just "Facebook", no page id anywhere. The lookup could never work.
     **Fix:** resolve page ids through the Ad Library's own keyword search, which
     does render fully. That produced 8 brand-new page ids.
   - **`no_data` (was 19)** — the page loaded fine every time. Two compounding
     bugs: the count regex ran against **raw HTML** while the number and the word
     "results" sit in different DOM nodes (so it could never match), and the Ad
     Library's **"No ads match your search criteria"** empty state was read as
     *unknown* rather than as **zero**. Separately, several of those page ids were
     simply **wrong** — Fishwife was pointed at a page called "The Fish Wife",
     Canvas Beauty and Crown Affair at other pages entirely.

   **Recovered:** 4 new qualified (Obvi 24, Oudware 15, Halfdays 55, Emi Jay 84 —
   now wave 3), 3 added to your flag list above (Fishwife, Canvas Beauty,
   UrbanStems), and 2 correctly disqualified as `too_big` (Crown Affair 320,
   Mugsy 300) that had been invisible.

   **Caveat worth knowing:** name-matching against Ad Library search produces
   false positives — it offered *Franne Golde* for Golde, *Recess Therapy* for
   Recess, *Crane & Canopy* for Canopy, and *Humane Society of Huron Valley* for
   Huron. Only exact and corporate-suffix matches (`EmiJay Inc.`, `Canvas Beauty
   Brands`) were accepted; the rest were rejected rather than guessed at, which is
   why 46 brands remain unresolved rather than being force-matched.
2f. **Two brands need a 10-second identity call from you** — the search found a
   plausible page but the name isn't an exact match, and guessing wrong means
   pitching the wrong company:
   - **GOODLES** → page "Goodles: Noodles, Gooder." — almost certainly them, but
     it's a tagline, not a name.
   - **Recess** → page "Recess Therapy" — probably **not** them (Recess Therapy is
     the street-interview series; Recess is the sparkling drink). Left unmatched.
3. **Stripe** — dashboard.stripe.com/register (sole prop, personal checking OK,
   ACH on, tax auto-transfer). CLARVIS cannot create financial accounts.
4. **Taste-pass spec pack #1** — files sent to you in chat; also vault
   `Money/Clients/portland-pet-food-company/`.
5. **Rotate `VAULT_GIT_TOKEN`** (still open from the 08-01 handoff): revoke at
   github.com/settings/tokens → new token (repo scope, `Second-brain` only) →
   paste into Coolify app env
   (`http://178.156.209.40:8000/project/xn159afo226l4480ogtcrznz/environment/p78muchurjjfu962yg4iredu/application/h72tei3gy97z4wlqyqpvuylg`)
   → Redeploy.
6. **Porkbun 2FA** — porkbun.com/account#accountSecuritySettings. The account
   now controls the domain your whole pipeline sends from.
7. Optional: add `alex@` alias in Google Admin → Users (nicer sending address);
   CLARVIS will repoint DMARC reports back to it afterwards.

---

## 0. NEW 2026-08-01 — August Money Plan: your morning list

The finalized plan (council-amended) is in the vault: **`Money/August Money Plan
(FINAL).md`**. Its "FIRST 72 HOURS" section is your complete ordered checklist.
The two items that touch this repo's infra:

- ~~**Set `FLASK_SECRET_KEY` in Coolify**~~ ✅ DONE 2026-08-01 — Alex generated and saved
  it on the app resource. Takes effect on the next restart/redeploy.
- ~~**Pause the `money_clips_agent` Scheduled Task**~~ ✅ NOTHING TO DO — **the task does
  not exist.** Verified 2026-08-01 two ways: the app's Scheduled Tasks tab in Coolify
  lists exactly one entry, `sync-vault` (`*/10 * * * *`, last run success), and the
  agent's own Supabase rows stop dead after **2026-07-28T13:00:13** — it had run daily
  at 13:00 UTC from 07-19 to 07-28, then never again (nothing on 07-29, 07-30, 07-31,
  08-01). Nothing in the repo schedules it either: no cron, no launchd, no loop in
  `app.py`. So it is **not** burning a daily API call, and hasn't been for days. Don't
  go hunting for this task — it isn't there.

Done overnight so you don't have to: jobs.db now survives server redeploys
(parks on the vault volume under `.appstate/`, `JOBS_DB_PATH` overridable), and
the duplicate Mac morning-brief launchd job is retired (plist archived at
`scripts/archive/com.secondbrain.morningbrief.plist.disabled` — the in-app
brief + 08:15 phone nudge remain the single brief path).

**Naming the service is still item #1, and it is now a one-command decision.**
The portfolio site no longer needs an editing pass — `portfolio-site/render.py`
applies the name everywhere at once:

```bash
python3 portfolio-site/render.py --name "Northrun" --email hello@northrun.com
# → portfolio-site/dist/ ; preview: python3 -m http.server -d portfolio-site/dist 8080
```

It refuses to emit a page that breaks a plan rule: any surviving `{{placeholder}}`,
or the word "AI" anywhere (word-boundary, so "email" and "available" are fine).
**One change you should know about, because it's a judgment call I made for you:**
the three spec-pack tiles under the "work" heading now render as *category* labels
("pet food brand", "wellness brand", "food & beverage brand") rather than
Portland Pet Food / Golde / Fishwife. Those packs were built unsolicited from
public ads and none of those brands has been contacted — naming them under a
heading called "work" on a public page implies a client relationship that does not
exist. If you want the real names there, it takes an explicit
`--brands ... --brands-approved`, and it should follow an actual conversation
with them, not precede one.

**Related, and worth 10 seconds before your first send:** `scripts/check_client_doc.py`
lints anything a client will read. `Templates/proposal-template.md` carries a
"delete before sending" notes block whose own text says *never describe the work as
AI-generated* — fill the placeholders, forget the block, and the prospect reads that
sentence. Memory was the only thing preventing it.

```bash
python3 scripts/check_client_doc.py path/to/your-filled-proposal.md
```

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
