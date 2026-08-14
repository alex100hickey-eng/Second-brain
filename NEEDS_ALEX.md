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
2. **The 14-brand call** — see the table below. One blanket answer covers it.
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

### The 14 brands in the 101–199 band

These are held up on a judgment call, not on scraping. All 14 are real brands with
real ad spend; they sit above the 5–100 active-ad band you set.

| brand | active ads | category | domain |
|---|---|---|---|
| Guava Family | 190 | Baby gear | guavafamily.com |
| Divi | 190 | Scalp & hair care | diviofficial.com |
| Native Pet | 160 | Pet supplements | nativepet.com |
| Nani Swimwear | 160 | Swimwear | naniswimwear.com |
| UrbanStems | 150 | Flowers & gifting | urbanstems.com |
| ROAD iD | 150 | Safety wearables | roadid.com |
| SheFit | 130 | Activewear | shefit.com |
| Canvas Beauty | 130 | Haircare | canvasbeautybrand.com |
| Needed | 120 | Prenatal supplements | thisisneeded.com |
| Momentous | 120 | Performance supplements | livemomentous.com |
| Fishwife | 120 | Tinned seafood | eatfishwife.com |
| Apothékary | 120 | Herbal wellness | apothekary.co |
| The Outset | 110 | Skincare | theoutset.com |
| Pet Honesty | 110 | Dog supplements | pethonesty.com |

**My recommendation: "anything under 150 is in" — that's 8 brands**, and it draws
the line where the pitch actually changes. Under ~150 you're usually still reaching
a founder or a two-person marketing team who reads their own email. At 150–190
you're emailing someone who already has an agency, and the reply rate reflects it.

One of the 8 is a freebie: **Fishwife already has a finished, taste-passed spec pack
built** — it was one of your three sample drops. Pitching them costs no new work.

Worth knowing before you answer: you have 40 qualified prospects and roughly 30–50
sends of capacity before Aug 31, so these 14 are Wave 4 at the earliest. Saying "all
out" costs you nothing this month.

### Five identity calls (~10 seconds each)

- **Apothékary** — stored as the literal escape `Apoth&eacute;kary`, so the string
  compare failed. Almost certainly the right page. Confirm → it joins the table above.
- **Bask and Lather** — page is `Bask & Lather Co` (`&` vs "and"), 570 active.
  Confirm → it's `too_big`, not a candidate.
- **Big Barker** — page is `Barker Dog Beds`, 24 active. If that's their trading
  page it qualifies cleanly inside 5–100.
- **GOODLES** → page "Goodles: Noodles, Gooder." — almost certainly them, but it's a
  tagline rather than a name, so it wasn't auto-accepted.
- **Recess** → page "Recess Therapy" — probably **not** them (Recess Therapy is the
  street-interview series; Recess is the sparkling drink). Left unmatched on purpose.

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

5. **Tell me what you decided on the plan.** The Aug 9 restriction deadline passed
   and **the database is answering normally today** — I ran a live query against
   `Agent Outputs` and got HTTP 200, not a 402. So either you upgraded, or the
   restriction never landed. Worth confirming which, because the 08-08 egress fixes
   took the run-rate from ~53 GB/month to ~3 GB — comfortably inside the free tier —
   so if you *did* upgrade to Pro you can likely downgrade next cycle and keep the $25.
6. **Rotate to the `service_role` key** and enable RLS — kills the recurring
   "security vulnerabilities" emails. Project Settings → API → copy `service_role` →
   replace `SUPABASE_KEY` in Coolify env, `~/.zshrc` and `~/second-brain/.env` →
   verify the app still answers → SQL editor:
   `alter table public."Agent Outputs" enable row level security;`
   (No policies needed — service_role bypasses RLS, and the app needs zero code
   changes.) Audited 08-08: the anon key is server-side only, so this is hygiene,
   not an emergency.

### On the Hetzner box (needs your say-so — remote shell is your call, not mine)

7. **Kernel reboot** — the box has been printing `*** System restart required ***`
   for two weeks. Safe now that builds aren't fragile. 15 seconds: `reboot`.
8. **~9 GB of unused Docker images** could be reclaimed, but `docker image prune -af`
   deletes rollback targets, so it stays a deliberate choice. Disk is at **66%** with
   12.7 GB free today, and the hourly keep-3 guard is holding steady — so this is
   genuinely optional right now, not pending.

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
