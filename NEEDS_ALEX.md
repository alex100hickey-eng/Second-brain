# NEEDS_ALEX.md — everything blocked on you

**Rewritten 2026-08-14 from verified state, not from the previous version of this
file.** Everything below was checked today against the live systems: the server and
Mac nodes' `/api/version`, a real Supabase query, the actual contents of the
splitframestudio mailbox, and the suite (829/0). Items the old file listed as open
that turned out to be *already done* have been deleted rather than reworded — a
blocked-on-you list that's mostly noise is a list you stop reading.

History for resolved items lives in git and `BUILD_LOG.md`. This file is only what
is still yours to do.

---

## 🔴 THE ONE THAT MATTERS — warmup restarts today

Everything on the money side is behind this single gate, and it has now slipped
eleven days.

**Verified this morning, by searching your gmail rather than trusting the tracker:**
the studio domain has sent mail on **exactly one day ever — Mon 08-03** (three
threads, one reply). Nothing 08-04 → 08-13. The replies on 08-10 and 08-11 went
*from* your gmail *to* the studio: good for the mailbox, but they build no sending
reputation, which is the entire point of warmup. **Today is day 1 of 7. Again.**

Nothing is damaged — DKIM authenticates, SPF is single and clean, DMARC is
published, last mail-tester was 10/10. A quiet domain is not a burned domain. You
lost time, not the asset.

- **Do today (~10 min, from your phone):** open
  [[warmup-drafts-2026-08-14]] in the vault, send the 3 day-1 emails from
  `alexhickey@splitframestudio.com`, spaced through the day.
- **The floor is two emails a day.** Not four. A streak dies at zero, never at two.
- **Day 7 is Thu 08-20 → mail-tester gate Fri 08-21 → Wave 1 opens the same day**,
  leaving 10 selling days before Aug 31.

### Why it broke — it was a bug, not your memory

Worth 30 seconds because it means the fix isn't "try harder."

A status note written under the warmup step on 08-06 was parsed as that step's
**dependency list**. `needs:` is the last field on the line, so its regex ran to
end-of-line and swallowed all 40 words of the note. None name a real step → the
step was permanently blocked → and blocked steps are *deliberately* never nudged,
because nagging about work you can't start is how a proactive assistant gets muted.

**So documenting a step's status silently switched off its reminders.** It hit
`warmup-daily`, `outside-read` and `sales-rehearsal` — three of the most important
things on the board — on the same day, and the symptom was silence, which is
exactly what you'd expect if there were simply nothing to do.

Fixed today (`86fa58f`), two independent ways, plus: warmup now nudges **daily**
instead of twice-then-silence, and the warmup clock now measures *sending* rather
than the mailbox existing — it had been reporting "warmup running, delay is no
longer compounding" straight through those eleven silent days. Suite 815 → 829.

---

## 🟠 Money — the rest, in the order I'd do them

1. **Send the outside-reader email.** Draft has been sitting in your personal gmail
   Drafts since 08-06 ("quick favor — 5 min read"); PDF at
   `Desktop/splitframe-sample-pack.pdf`. Pick one person whose taste you trust,
   attach, send. This is the cheapest real signal you can get before pitching
   anyone, and it costs one email.
2. ~~**The 14-brand call**~~ ✅ DECIDED 2026-08-14 — you delegated it ("up to you"),
   so the under-150 rule was applied. See the table below for the record.
3. **Stripe** — dashboard.stripe.com/register, sole prop, personal checking, ACH on,
   tax auto-transfer 25–30%. 15 minutes. Not urgent until someone says yes, but it's
   the only thing between a "yes" and money landing. Do it while warmup runs.
4. **The call card's open question** — vault [[call-card]]: keep or strike the
   teardown refund guarantee. Decide it now, not on a live call.
5. ~~**Postmaster Tools**~~ ✅ CHECKED 2026-08-14 — domain **Verified** since Aug 1.
   The charts say "No data to display" and always will: Postmaster only renders above
   a few hundred messages/day to Gmail, and your whole plan tops out at 5/day. An
   empty chart here is **not** a deliverability signal — don't read it as one.
   mail-tester on 08-21 is the real gate, and it works on a single email.
6. **Confirm the move-in date** and make the Week 3/4 swap call.
7. **Two stale drafts to bin** (deletions are never automated, so they're yours):
   "Re: this week" — written 08-06 for a thread you already replied to on 08-11,
   so sending it now would be a duplicate. And "CLARVIS draft test — safe to
   delete" from 08-02.

### The 14-brand call — decided 2026-08-14 (you delegated it)

**Rule applied: under 150 active ads is in.** That's where the pitch changes —
under ~150 you're usually still reaching a founder or a two-person team who reads
their own email; at 150–190 an agency likely already holds the account.

**IN → qualified, wave 4 (8):** The Outset (110), Pet Honesty (110), Needed (120),
Momentous (120), Fishwife (120), Apothékary (120), SheFit (130), Canvas Beauty (130).
Fishwife is a freebie — its spec pack is already built and taste-passed.

**OUT (6):** Guava Family (190), Divi (190), Native Pet (160), Nani Swimwear (160),
UrbanStems (150), ROAD iD (150). They stay in the tracker marked out, revisit only
if capacity opens.

**Identity calls, all closed:** Apothékary confirmed (HTML-escape artifact) → in.
Bask and Lather confirmed (`&` vs "and") → `too_big` at 570. Big Barker confirmed
("Barker Dog Beds" is their trading page; they sell dog beds) → qualified at 26
active, wave 4. GOODLES accepted ("Noodles, Gooder." is their own tagline) but its
ad count still needs a recount before banding. Recess **rejected** — "Recess
Therapy" is the street-interview series, not the drink; pitching the wrong company
is worse than skipping one.

Tracker now stands at **49 qualified** (was 40). Wave-4 drafts get generated fresh
against live ads when wave 3 finishes — drafts written now would be stale by send
day. Overrule any of this by telling CLARVIS; the pre-decision snapshot is
`prospect-tracker.csv.snapshot-pre-wave4-2026-08-14`.

---

## 🟡 CLARVIS / infra — what's genuinely still open

Grouped by where you'd be when you do them, so each group is one sitting.

### In the Coolify dashboard (one visit, ~5 min)

App resource →
`http://178.156.209.40:8000/project/xn159afo226l4480ogtcrznz/environment/p78muchurjjfu962yg4iredu/application/h72tei3gy97z4wlqyqpvuylg`

1. ~~**Start Command — one request at a time**~~ ✅ DONE 2026-08-14. Now runs
   `--worker-class gthread --threads 8`. Verified at the container, not the UI:
   5 concurrent requests finished in **0.26s wall clock** instead of serializing.
2. ~~**`WEATHER_LATLON`**~~ ✅ DONE 2026-08-14 — set to Ridgefield + CWRU + BC. The
   code only accepted one coordinate pair, so it was extended to labeled multi-place
   (`16dae29`); several places render compact, one place is unchanged.
3. ~~**`TAVILY_API_KEY`**~~ ✅ DONE 2026-08-14 — free Researcher tier (1,000/mo, no
   card). Web search is off keyless DuckDuckGo. Still worth adding to `~/.zshrc` so
   the **Mac node** gets it too; only the server has it right now.
4. ~~**`VAULT_GIT_TOKEN` rotation**~~ ✅ DONE 2026-08-14, and the old note had the wrong
   repo — the vault lives in `second-brain-vault`, not `Second-brain`. Replaced with a
   **fine-grained** token: one repo, Contents read/write only, expires Nov 12. The old
   one had full `repo` scope across *every* repository you own. Two dead classic tokens
   (`Clarvis`, `Second brain push`) were revoked; `CLARVIS!` was kept and verified by
   scope to be `GITHUB_TOKEN`, not the vault token. Rotation was done in the order that
   matters — new token proven with a real `git pull` **before** revoking the old — because
   the old token was baked into the server clone's remote URL, and revoking first would
   have broken vault sync silently every 10 minutes.

### In the Supabase dashboard (~5 min)

Org `jbyfwshwyrzcuwmgalbm` (alex2hoop@icloud.com).

5. ~~**Plan status**~~ ✅ SETTLED 2026-08-14 — **you are on the Free Plan and should
   stay there. Do not pay $25/mo.** You never upgraded; the cycle reset 08-13 with a
   $0.00 invoice. Current cycle (14 Aug – 14 Sep) reads **egress 0.085 GB of 5 GB —
   2%**, against **12.87 GB** last cycle. The 08-08 polling fix landed better than
   predicted: the run-rate is ~2.5 GB/month, half the free allowance. The orange
   "grace period" banner is a stale leftover from the previous cycle, not a live
   warning. Nothing to buy here.
6. ~~**Rotate the key + enable RLS**~~ ✅ DONE 2026-08-14. Supabase has moved to new
   key formats, so this used the modern **`sb_secret_`** key rather than the legacy
   `service_role` JWT (verified safe first: nothing in the codebase parses the key,
   it goes straight to `create_client`). Replaced in all three places — Coolify,
   `~/second-brain/.env`, `~/.zshrc` — and RLS enabled on `Agent Outputs`.
   **Proven both directions:** the app reads and writes normally with the secret key,
   and the old anon key now returns HTTP 200 with **zero rows**. That's RLS working —
   the leaked-key scenario is now worthless to an attacker.
   `~/.zshrc` had been holding the old anon key, which would have failed *silently*
   (empty results, no error) for anything sourcing your shell profile instead of
   `.env`. Fixed at the same time.

### On the Hetzner box

7. ~~**Kernel reboot**~~ ✅ DONE 2026-08-14 — you said "you handle it," so it was
   rebooted at 12:48 ET and verified back in **36 seconds**: all containers healthy,
   the reboot-required flag cleared, app serving the latest commit. First reboot
   since the box was set up (13.5 days uptime).
8. ~~**Docker image cleanup**~~ ✅ NOTHING TO DO — verified after the reboot: the
   hourly keep-3 guard is doing its job on its own (its 16:17 run pruned 82% → 65%).
   Rollback targets intact. This item is closed, not pending.

### Elsewhere, small

9. ~~**Porkbun 2FA**~~ ✅ DONE 2026-08-14 — app-based TOTP enabled and verified
   server-side (state survived a full reload). Code lives in iCloud Passwords on the
   phone, so it's backed up. Recovery details were checked (masked): name correct,
   backup email = the verified gmail, phone = a real 845 number. Debugging note for the
   future: the 2FA switch does nothing in an automated browser because it opens a
   native `confirm()` dialog that gets auto-cancelled — the warning text was
   acknowledged explicitly before proceeding.
10. ~~**UptimeRobot**~~ ✅ DONE 2026-08-14 — free account on the personal gmail,
    monitoring `/api/version` every 5 min (NOT `/health`, which 302s to login and
    would have false-alarmed). Test notification fired and both DOWN/UP emails
    verified arriving at alex100hickey@gmail.com within seconds. If the box dies,
    Alex knows inside ~6 minutes.
11. **Look at the phone HUD.** The instrument bands shipped and are verified in the
    served assets, but nobody has looked at them on a real phone yet.
12. **Optional: `claude setup-token`** → put `CLAUDE_CODE_OAUTH_TOKEN` in
    `~/second-brain/.env`. The capability watcher now *detects* auth failure loudly
    (fixed `aaaaf73`, after it logged five days of dead builds as clean rc=0
    finishes), but a long-lived token stops the lapse happening at all.

---

## ✅ 2026-08-14 — the second app nobody knew was there (biggest infra win of the day)

`money-clips-agent` was not a stray scheduled task. It was **an entire second Coolify
application**, deployed from this same repo on branch `main`, and it explains the
recurring disk emergencies far better than anything in the old notes did.

- Its container ran **`sleep infinity`, 24/7**, doing nothing — it existed only so a
  daily cron had somewhere to `exec`.
- Because it tracked the same repo, it **rebuilt a full 1.94 GB image on every push to
  main**. Four of them were sitting there tagged with today's commits alone. That is
  why every push showed "2 deployments" and why disk climbed ~4 GB per push instead of
  ~2 GB. The 08-01 and 08-08 disk-full incidents, the 20 GB in 26 hours, the keep-3
  guard — half of all of it was this.
- The daily Sonnet call was the least of it, and its output was never consumed:
  the docstring says the concepts are "for review before you feed it into Viewmax,"
  and that feeding never once happened.

**Why the 2026-08-01 note said it didn't exist:** that check opened the *second-brain*
app's Scheduled Tasks tab, correctly saw only `sync-vault`, and concluded the task was
gone everywhere. It was never on that resource. Nobody thought to look at a second
application. It kept running for another two weeks.

**Done:** script archived to `scripts/archive/money_clips_agent.py` (it had existed
only on the server, never in git), app deleted, its 4 orphaned images removed *by
repository name* so the main app's rollback targets were untouched — deliberately
declining Coolify's "Run Docker Cleanup" checkbox, which would have taken them.
**Disk 71% → 60%, free 11 GB → 15 GB, and every future push now costs half as much
disk and RAM.** Generated concepts remain in Supabase.

---

## ✅ 2026-08-14 — CLARVIS's server-side notes were being silently thrown away

Found while rotating the vault token. **Vault sync only ever ran `git pull` on the
server.** But CLARVIS also *writes* there — managed research tasks save notes into the
server's `/data/vault` — and those files had nowhere to go. They simply accumulated,
invisible to Obsidian, to GitHub, and to you.

**Four files were stranded, 42 KB of finished work:**

| file | size | what it is |
|---|---|---|
| `Money/apify-tender-feed-validation.md` | 20 KB | council-reviewed validation of a procurement/tender feed idea |
| `Money/apify-target-research.md` | 13 KB | ranked shortlist of Apify Store scraper opportunities |
| `Money/clip-for-pay-vetted-candidates.md` | 8.6 KB | vetted clip-for-pay programs, council-reviewed |
| `Learning/seed-oils-collection.md` | 0.3 KB | research collection stub |

All four are now **rescued into your vault and pushed** — they'll appear in Obsidian on
the next iCloud sync. Nobody knows how long they sat there, or whether earlier notes
were lost the same way before anyone looked.

**Fixed so it can't recur:** the server is now a full sync peer, not a read-only
consumer — `scripts/server_vault_sync.sh` pulls, then commits and pushes whatever
CLARVIS wrote, and `scripts/vault_sync.sh` on the Mac now pulls before committing so
the two can't clobber each other. Proven end-to-end by writing a file on the server and
watching Coolify's own scheduled task push it to GitHub.

**One more trap closed:** the sync command used to live only in Coolify's task field,
where `$(git status --porcelain)` was expanded by the *host* shell before the container
ever saw it — so the commit branch never ran while Coolify cheerfully reported "Success"
in 0 seconds. The identical command worked first try by hand. It now lives in a script
in this repo, which is also the third time today that server-only code turned out to be
invisible code.

---

## 🆕 Found today, not previously on any list

- **Your Mac's disk is at 90%** — 21.8 GB free of 228 GB. Not urgent, but the
  server's disk problems in this project have all started looking exactly like this.
  Worth a look before it's a Sunday-night emergency.
- **The Mac node had been running 7 commits behind since 08-08** — it was still on
  `2e55fd0` while the server ran `aaaaf73`, so locally CLARVIS was missing ambient
  awareness, protocols, reminders, weather and both escalation fixes. Restarted onto
  current code today and verified; no action needed from you. Worth knowing the
  failure mode exists, since the Mac node is started by hand and nothing watches it.

---

## ⏳ Waiting on time, not you

- **Tool prune** — 93 registered, 12 used on the Mac. Cross-node audit mirror shipped
  07-30; it wants ~a week of two-node data, then prune with evidence. See `TOOL_AUDIT.md`.
- **`app.py` is ~5,600 lines** — real structural debt, not urgent. Best done *with*
  the prune.
- **Google Calendar stays disconnected on purpose** (your call, 08-11 — the calendar's
  contents are junk, so connecting it would feed CLARVIS bad data). Everything degrades
  gracefully without it. Not a bug, don't let anything flag it as one.
