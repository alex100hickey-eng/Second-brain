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
5. **Postmaster Tools** — postmaster.google.com → splitframestudio.com → confirm
   data is populating. 2 min. If it's still empty by 08-17, something's wrong with
   the domain setup and you want to know before the 08-21 gate, not after.
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

1. **Start Command — the server still handles ONE request at a time.** A streaming
   chat reply blocks the dashboard and any second message. Two-field drill:
   ```
   gunicorn --chdir second-brain-chat app:app --bind 0.0.0.0:5000 --timeout 120 --worker-class gthread --threads 8
   ```
2. **`WEATHER_LATLON`** — built and dormant since 08-11. Set it to your town, e.g.
   `WEATHER_LATLON=41.39,-73.45`, and CLARVIS's ambient block gains weather.
   Coordinates only ever go to the keyless open-meteo.com API. (Also worth adding to
   `~/.zshrc` so the Mac node has it.)
3. **`TAVILY_API_KEY`** — confirmed still unset on the Mac node's startup check
   today, so web search is running on keyless DuckDuckGo, the weakest fallback.
   Free tier is 1,000 searches/month at tavily.com. Instantly sharper research.
4. **`VAULT_GIT_TOKEN` rotation** — still open from the 08-01 handoff. Revoke at
   github.com/settings/tokens → new token, repo scope, `Second-brain` only → paste
   into the env here → Redeploy.

### In the Supabase dashboard (~5 min)

Org `jbyfwshwyrzcuwmgalbm` (alex2hoop@icloud.com).

5. **Plan status: resolved per you, 2026-08-14** ("my money issues are resolved").
   Verified the same day: the DB answers HTTP 200, no 402. One residual worth $25/mo:
   if what you did was upgrade to Pro, the 08-08 egress fixes (~53 GB/mo → ~3 GB/mo)
   mean you can likely **downgrade back to Free next cycle** — check the usage graph
   at https://supabase.com/dashboard/org/jbyfwshwyrzcuwmgalbm/billing in early
   September and keep the $25 if it held.
6. **Rotate to the `service_role` key** and enable RLS — kills the recurring
   "security vulnerabilities" emails. Project Settings → API → copy `service_role` →
   replace `SUPABASE_KEY` in Coolify env, `~/.zshrc` and `~/second-brain/.env` →
   verify the app still answers → SQL editor:
   `alter table public."Agent Outputs" enable row level security;`
   (No policies needed — service_role bypasses RLS, and the app needs zero code
   changes.) Audited 08-08: the anon key is server-side only, so this is hygiene,
   not an emergency.

### On the Hetzner box

7. ~~**Kernel reboot**~~ ✅ DONE 2026-08-14 — you said "you handle it," so it was
   rebooted at 12:48 ET and verified back in **36 seconds**: all containers healthy,
   the reboot-required flag cleared, app serving the latest commit. First reboot
   since the box was set up (13.5 days uptime).
8. ~~**Docker image cleanup**~~ ✅ NOTHING TO DO — verified after the reboot: the
   hourly keep-3 guard is doing its job on its own (its 16:17 run pruned 82% → 65%).
   Rollback targets intact. This item is closed, not pending.

### Elsewhere, small

9. **Porkbun 2FA** — porkbun.com/account#accountSecuritySettings. That account
   controls the domain your whole pipeline sends from.
10. **UptimeRobot** — if the Hetzner box dies, nothing tells you. Free plan pinging
    `https://clarvis.178.156.209.40.sslip.io/health` is a 3-minute signup, zero code.
11. **Look at the phone HUD.** The instrument bands shipped and are verified in the
    served assets, but nobody has looked at them on a real phone yet.
12. **Optional: `claude setup-token`** → put `CLAUDE_CODE_OAUTH_TOKEN` in
    `~/second-brain/.env`. The capability watcher now *detects* auth failure loudly
    (fixed `aaaaf73`, after it logged five days of dead builds as clean rc=0
    finishes), but a long-lived token stops the lapse happening at all.

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
