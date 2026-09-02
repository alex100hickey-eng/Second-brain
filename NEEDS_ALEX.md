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

## 🔧 2026-09-02 (Wed) — audit follow-through shipped; 3 things are yours

Everything below is live after the next deploy (`/api/version` shows the commit).
Basketball items were parked on your call.

1. **Log in to Canvas once in the Mac's Browser pane** (canvas.case.edu). Two
   scheduled tasks now read Canvas through that logged-in pane, no token: a nightly
   **canvas-status-sync** (9:35 PM) that flips assignments.csv rows to submitted/graded
   so finished work stops showing as open, and the Friday content sweep. The pane's
   SSO session expired today, so neither can run until you log in. You will get one
   phone alert if it is still logged out tonight.
2. **Evening review: on or off?** It has been OFF since Aug 25 (`evening_review: ""`).
   The nightly scorecard is now a 4-tap page the evening nudge carries, so turning it
   back on costs one notification a night. Say "evening review at 9:00 pm" (or any
   time) and it's on; say nothing and it stays off.
3. **Excel Prep 1 is due Fri 9/4 11:59 PM and you leave ~1:30 PM.** Thursday night
   is the slot. Drop/Add (WileyPlus decision) also closes Friday.

**What I wrote into your data (all reversible):**
- Training-app calendar: Fri 9/4 "AWAY from ~1:30 PM (Avelo ~4:30 PM)", Sat/Sun
  "AWAY — Boston", Mon 9/7 "Flying back — JetBlue 7:15 AM". Any calendar line
  containing away/travel/flight/trip now silences the gym/study kickoffs and the 50/50
  ping for that day. Backup: `.canvas_status/bigObligations_backup_2026-09-02.json`.
  To undo: clear those four days in the app's Calendar.
- Weekend Map W2 re-cut as an away weekend (clear Friday before the train; Labor Day
  afternoon is the work window).
- **Canvas moved the whole CSDS project spine a week EARLIER** (ICS sync 09:42):
  Proposal Fri Oct 23 · Update Fri Nov 6 · Final Report + Reflection **Sun Nov 29**
  (Thanksgiving Sunday) · Presentation **Mon Nov 30 12:30 PM**. assignments.csv,
  curriculum.csv, courses.csv and the Weekend Map (W8-W16) now agree. From now on a
  moved deadline pushes a phone alert.
- The 3 AIQS paper rows and 2 CSDS report/presentation rows were typed "exam"; now
  paper/project (they were earning exam-runway orders while MATH Test 1 got none).
- 132 test-fixture "draft notes" retagged out of the way and their files removed from
  vault_inbox (the suite used to write into production; it no longer can).

**What changed in how CLARVIS talks to you:**
- The ranked day no longer fills with stale overdue items: anything more than 3 days
  overdue becomes ONE "Backlog: N items" line; tomorrow's dues rank with graded work;
  each assignment appears once; a just-passed open row is a "verify" line for 2 days.
- One item = one concern (due + missed together, max 2 touches). Canvas emails never
  say "Missed" (CLARVIS can't see you submit). Your own sent mail is never extracted.
  Undated asks expire after 14 days untriaged.
- PACE reads honestly: attendance is assumed, submitted work counts, MATH's tests show
  in EXAM READINESS, DO NEXT names MATH first. Sundays 7 PM you get a one-page
  "where did you get to" with one tap per course.
- The money-step "Done" button on notification pages now actually banks the step.
- The Mac node restarts itself when the code moves; a sleeping Mac no longer raises
  "subsystem down" alarms; the $50 budget now sees both nodes.

## 💰 2026-08-30 (Sun) — SEND IS UNBLOCKED. 3 decisions, then Monday is 10 minutes.

**Why the send slipped twice: there are no recipient email addresses.**
*(Correction: this was NOT a new discovery — `contact_finder.py` records finding the
exact same gap on 2026-08-24. The proper fix was written then and has never run,
because it needs a free `HUNTER_API_KEY` that was never obtained. The gap stayed
open for six days behind a five-minute signup.)*

**THE ONE ACTION THAT ACTUALLY CLOSES THIS: get a Hunter key.** hunter.io free tier,
25 domain searches/month, a wave is 3. Key goes in `second-brain-chat/.env` as
`HUNTER_API_KEY`, restart, then say "fill contacts for wave 1" — you get named,
verified contacts instead of help desks. 5 minutes.

Interim fallback added today:
`email` + `email_source` columns added, and all three Wave 1 brands populated from
their live sites (Diggs `help@diggs.pet` · Fable Pets `help@fablepets.com` ·
Gunner `info@gunner.com`, each verified on the site 2026-08-30).

**Ready to go, nothing left to write:**
- `Money/SEND SHEET — Mon Sep 1, 11:15.md` — addresses + all three emails, paste-ready
- `Money/Clients/followups-wave1-2026-08-30.md` — FU1 (Wed Sep 3) and FU2 (Mon Sep 8)
  for all three brands, written. The sequence is what converts; it didn't exist before.

**THREE DECISIONS — 15 minutes, and they must exist before a founder can reply:**
1. **Refund guarantee — keep or strike?** (call-card flags it: decide before, never
   live). Recommendation: **KEEP for the first three calls.** Costs nothing until a
   teardown actually lands flat, it's the sharpest answer to "you're 19," strike later
   with zero downside.
2. **Your two numbers.** Ranges are $500-750 drop / $750-1,250 retainer.
   Recommendation: **$650 drop, $950/mo.** Write them down; never invent on a call.
3. **What "spec pack" means when they say yes.** All three emails promise a free spec
   pack. `splitframe-sample-pack.pdf` is real and good — but it's built for *Portland
   Pet Food Company*, so it's a sample, not theirs. Recommendation: **send the Portland
   pack same hour as proof, offer theirs within 48h** (ad_creative_pipeline.py builds it).

**This week, not Monday: finish Stripe.** Still "Continue setting up your Stripe
account" since 8/24. Doesn't block the send; completely blocks the close — the call
card says invoice within 2 hours of hanging up, and right now you cannot invoice anyone.

**Known gap, not urgent:** 46 of the 49 qualified brands still have no email address.
Waves 2-4 need the same lookup before they're sendable. Say the word and it gets done
in a batch.

## 🏀 2026-08-30 — FLAG DEC 4-6 TO JARVIS NOW (basketball away game Dec 5)

Alex reported an **away game Sat Dec 5**, which makes **Fri Dec 4 a travel day**. That
collides with the single heaviest academic day of the semester, and one item on it is
close to unrecoverable:

**Fri Dec 4** — *last day of instruction, hard University cutoff, no extension possible:*
- **ACCT: MP Presentation + Team Evaluations** ← the real problem. In-person GROUP
  presentation. ACCT group projects are **12% with ZERO drops**, and the syllabus allows
  no makeup without a **University-sanctioned absence**. Varsity travel normally qualifies,
  but courses.csv already carries the standing instruction: *"flag basketball travel early."*
  Teammates are also depending on the date — this is not a solo reschedule.
- ACCT HW Day 27 (Make or Buy & Discontinued Ops) + HW Day 28 (Sales Mix)
- ECON HW 5: The Basic Tools of Finance
- AIQS last class (workshop reflections + course evals — "bring a device")

**Sun Dec 6** — if the trip returns late, this stack lands on a travel-tired Sunday:
- **AIQS Writing Folder** (replaces the final — single PDF) + Experience Portfolio
- **CSDS Final Project Report** + Project Reflection
- ACCT 10-K / MP Discussion + Team Member Evaluation

**Action (do it in the first half of September, not November):** email Jarvis with the
basketball travel dates and ask how he wants the MP Presentation handled — present early,
present remotely, or have the group cover it. Asking in September reads as responsible;
asking in December reads as a scramble. Same email can flag any other travel dates once
the season schedule is published.

**Still unknown:** the full game schedule isn't in the vault. Get it from the team and
we can diff the whole season against every deadline in one pass.


## 💰 2026-08-29 (Sat) — Wave 1 rebooted for Monday. Three tasks, ~30 min total.

Week one of classes ate the money lane whole — zero sends since the warmup email
last Sunday, verified against the tracker CSV and the studio mailbox. Nothing is
damaged (the 8/21 mail-tester 10/10 stands; a quiet domain is not a burned one),
but the $1k-by-Aug-31 target is officially out of reach — September is now the
selling month, and the Oct 15 kill-criteria checkpoint is the real horizon.

The Monday-morning friction has been removed for you: all three Wave 1 brands'
public Ad Library pages were re-pulled TODAY and the drafts regenerated against
that evidence — no "check their ads first" homework left. Yours:

1. **Today + Sunday: 2 warmup sends each day** from the studio mailbox
   (~5 min/day, phone). Drafts ready: `Money/warmup-drafts-2026-08-29.md`.
   Reply from the gmail side each time.
2. **Sunday evening: skim the 3 drafts, put them in your voice** (~10 min):
   `Money/Clients/outreach-drafts-wave1-2026-08-29.md` — Diggs, Fable Pets,
   Gunner Kennels. Every ad claim in them was verified live this morning.
3. **Monday 11:15-11:30: send all three** from the studio mailbox, then tell
   CLARVIS "sent wave 1" — the tracker logs itself.

**One decision to make before a founder can call back** (do it whenever this
weekend, 2 min): the call card's open question — keep or strike the teardown
refund guarantee ("if the teardown tells you nothing new, kill it and I'll refund
it"). Recommendation: **keep it for the first three calls.** It's the sharpest
answer you have to "you're 19," it costs nothing until a teardown actually lands
flat, and you can strike it later with zero downside. Say the word and the call
card gets updated.

After Monday the rhythm is **Mon/Wed/Fri, 11:15, 3-5 sends** — at that pace all
22 wave-1 brands are contacted by mid-September with follow-ups on schedule.
Fresh drafts for each next batch get regenerated the evening before (ask CLARVIS
or Claude Code to "prep the next 3" — it's one tool call now).

## 🟢 2026-08-27 — get-ahead system is live; 3 things are yours

Canvas got swept today (CSDS101 published — full 16-week schedule imported; AIQS
paper prompts archived; MATH problem lists through Test 2 captured). Lead targets
raised to your "1-2 weeks ahead" standard; the weekend sprint is in
`School/Get-Ahead Playbook.md` (vault). Yours:

1. **Verify ACCT Day-1 HW went in** — due Thu 8/27 10:00 AM: name tag + physical
   photo info card handed in, AND the Google Student Info Form
   (https://forms.gle/VuyvjAy8KDEjahsJ7). Zero-late-work course; if anything is
   missing, email Jarvis today.
2. **Pick your 10-K group + company in Canvas NOW — it's first come, first
   serve.** ACCT's own project brief says "the sooner you do this, the better
   your selection choices"; anyone unselected by Fri Sep 11 gets auto-assigned
   a company AND a group. Canvas → ACCT 100 → Groups, add your name under a
   company. Max 4 per group. This is a free advantage that expires.
   (Files are all downloaded — that item is closed.)

4. ~~Click "Run now" on `friday-canvas-sweep`~~ — **DONE**: the sweep ran on
   schedule 2026-08-28 3:10 PM, Canvas SSO held, no prompts. It runs weekly now.
3. **Pick your AIQS presentation slot BEFORE Fri Sep 4 class** (sign-up opens
   that day; slots #1-12 run Sep 16→Nov 16). Avoid Oct 5-9 and Nov 16-23 —
   those are exam-collision weeks.

## 🟠 2026-08-28 — Friday Canvas sweep: 3 things are yours

Sweep ran clean (15 files imported; nothing silently revised in MATH/CSDS). This
weekend's list is cut into `School/Weekend Map — Fall 2026.md` under **Cut
2026-08-28**. What needs *you*:

1. **Three ACCT items are showing 1 day OVERDUE in the brief** — Day 1 Homework
   (Introduction), Day 2 APQ, and Day 2 Reading (Ch.1 LO 1-2), all due Thu 8/27.
   These are almost certainly done and just not marked in the vault (nothing marks
   them but you). Open Canvas → ACCT 100 → Grades, confirm all three submitted,
   and say so — I'll flip them to done. **If any actually didn't go in, email
   Jarvis tonight**: this is the zero-late-work course.
2. **Two ACCT tasks from Jarvis's Friday announcement that live nowhere else**
   (no Canvas due date, so the deadline sync can't see them):
   **finish the last three in-class problems on the Day 2 handout** — he goes over
   them Tuesday and they are Exam-1 material — and **bring your index card to
   Tuesday's class**. The handout is now in the vault at
   `Courses/ACCT100/Handouts/Day 2 Handout — Financial Information.pdf`.
3. **Start Excel Prep 1 this weekend** (due Fri Sep 4). His words: it takes a
   while, start early. Needs desktop Excel 365 installed — UTech can do it if
   yours isn't working yet.

Still open from last week, both time-sensitive and both unchanged:
**pick your 10-K group + company in Canvas** (first come, first serve — auto-assigned
if you're unselected by Sep 11) and **pick your AIQS presentation slot before Fri
Sep 4 class** (sign-up happens in the room).

## ✅ DONE 2026-08-22 — studio mailbox connected

`alex-studio` is live: CLARVIS reads alexhickey@splitframestudio.com directly
and drafts replies INTO it, so a prospect reply no longer means retyping in the
right account. Verified by reading the box back; `STUDIO_GMAIL_ENTITY` is set in
`.env` and Coolify, server restarted on it. Still draft-only — you send.

## 🔵 MONDAY 8/24 — three things converge on one day

First day of classes, Wave 1 opens, and the Quest appointment. In order:

1. **Wave 1 (3 emails) — send at 11:15 AM, not 8 AM.** The old "8-10 AM"
   plan predates your schedule: 8:00-8:30 is gym drills, 8:30-9:00 is the walk
   back and getting ready, and MATH runs 9:20-10:10. You have no free minute in
   that window. Best slot is **11:15-11:30**, the top of your lunch/class-review
   block — still prime inbox time. The 10:10-10:25 gap between MATH and AIQS
   works too (phone is fine for two plain-text emails).
   **The drafts are already written** — Diggs, Fable Pets, Gunner Kennels, fresh
   against live sites, lint clean, in
   `Money/Clients/outreach-drafts-wave1-2026-08-23.md`. Review, put them in your
   own voice, send from the studio mailbox by hand, then tell CLARVIS you sent —
   the tracker CSV updates itself now.
   **One real caveat:** each draft makes claims about the brand's *currently
   running* ads, but the stored Ad Library notes are undated and possibly weeks
   old. Two minutes in the Ad Library per brand before sending. Being wrong
   about a founder's own ads is the one mistake cold outreach doesn't survive.
2. **9:20 AM · MATH 120, Olin 305** — first class. Quizzes start immediately
   (prior class's material, no make-ups), so the 11:15 review block is where
   Monday's quiz points get banked.
3. **2:40 PM · Quest blood test** — bring the paper script. It overlaps your
   2:30–5 gym block; move the session, don't skip the test (it gates Healthy
   Roster and therefore participation).

Your wake-time brief (6:30 AM Mon) now leads with these automatically.

## ✅ 2026-08-21 — THE GATE IS PASSED: mail-tester 10/10, Wave 1 is cleared

Run Friday 8/21 ~5:45 PM with a real pitch-shaped email (subject "quick idea for
your next ad test"): **10/10.** SpamAssassin clean, SPF/DKIM/DMARC all green, no
blocklists. The domain is proven. Per the plan Wave 1 (3 emails) is now cleared
to send — recommendation on record: Monday 8–10 AM so founders see it at the top
of the inbox, not under a weekend pile. Wave-1 drafts get generated fresh against
live ads right before send.

Same day: Hetzner overdue invoice ($13.73, warning level 2, lockout threatened
8/22) found and paid, and a card was saved for auto-collection — that failure
mode is permanently closed.

## 🔴 (RESOLVED — history) warmup restarts today

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

1. ~~**Send the outside-reader email.**~~ ❌ STRUCK 2026-08-20 — superseded by your
   own later decision to keep the personal circle out of the money process entirely
   (warmup runs self-contained). The 08-06 draft ("quick favor — 5 min read") should
   be **deleted**, not sent — it joins the stale-drafts bin in item 7.
2. ~~**The 14-brand call**~~ ✅ DECIDED 2026-08-14 — you delegated it ("up to you"),
   so the under-150 rule was applied. See the table below for the record.
3. ~~**Stripe — activate payments.**~~ ✅ DONE — verified 2026-08-24 12:05 in the
   live dashboard, not from this file. Account status shows **"No active tasks to
   complete"**; Capabilities lists **Payments** and **Payouts** as Active (only
   Cartes Bancaires is paused, which is a French card network and irrelevant);
   the publishable key is `pk_live_`; and a default USD payout bank is attached
   (Fidelity Investments via UMB, ••••9416). Business address is filled in.
   **Nothing here is blocked on you.** The 08-20 entry that said "Sandbox/test
   mode, ~15 min, all of it yours" was stale and sent Alex to a finished task on
   08-24 — re-verify against the dashboard before ever re-opening this item.

4. **The call card's open question** — vault [[call-card]]: keep or strike the
   teardown refund guarantee. Decide it now, not on a live call.
5. ~~**Postmaster Tools**~~ ✅ CHECKED 2026-08-14 — domain **Verified** since Aug 1.
   The charts say "No data to display" and always will: Postmaster only renders above
   a few hundred messages/day to Gmail, and your whole plan tops out at 5/day. An
   empty chart here is **not** a deliverability signal — don't read it as one.
   mail-tester on 08-21 is the real gate, and it works on a single email.
6. ~~**Confirm the move-in date** and make the Week 3/4 swap call.~~ ✅ MOOT
   2026-08-20 — you moved in Aug 16 (Sherman 222); verified from your own texts
   during the schedule rebuild. The swap-call question died with the move.
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

## 🎒 2026-08-21 — school week-1 buy/do list (from the freshly imported syllabi)

All four available syllabi are now imported and verified (CSDS 101 still
unpublished — it gets swept when the prof opens it). These are the items only
you can do, ordered by deadline pressure:

1. **Order the AIQS course pack at FedEx in Thwing — Saturday.** Printing takes
   days and physical copies are REQUIRED in class; first reading due Wed 8/26.
2. **MATH textbook before Monday** (Zill & Dewar, Essentials of Precalculus 6e,
   ISBN 9781284056327) — daily quizzes start immediately, no make-ups.
3. **AIQS books:** They Say I Say 6e (978-1324070030), Arden Romeo & Juliet
   (978-1903436912), Arden Antony & Cleopatra (978-1904271017).
4. **Email Dr. N (jtn33@case.edu) THIS WEEK** re: basketball absences — >4 hurt,
   >9 = automatic F, no excused/unexcused distinction. Same for known travel to
   Prof. Jarvis (ACCT, no makeups) and Dr. Krause (MATH, notify BEFORE exams).
5. **WileyPlus decision by 9/4** (Drop/Add end): ACCT digital textbook
   auto-charges ~$120 to your student account; opting out costs more. Create the
   WileyPlus login with your case email.
6. **Install full desktop Excel** from the university software center (not the
   web version) — 3 ACCT homeworks + both projects need it; Prep 1 lands 9/4.
7. **Print ACCT Class 1+2 lecture packets** from Canvas; calculator; PollEverywhere
   on your phone (ECON attendance from day 1).
8. **Sickle cell**: Quest is booked **Mon 8/24, 2:40 PM** — bring the paper
   script; it overlaps your 2:30–5 gym block, so that session shifts. The Emily
   Randall email was SENT 8/22 (not a draft any more); a pediatrician
   newborn-screening record can still substitute if it lands first.
   Teamworks has TWO forms waiting (NCAA statement + Business Office).

Every exam and hard deadline from all four syllabi is already in your training
app's Calendar (32 dates) — including the **Dec 8 double final** (ACCT 8-11 AM,
MATH 3:30-6:30 PM).

## 🟡 CLARVIS / infra — what's genuinely still open

Grouped by where you'd be when you do them, so each group is one sitting.

### 🆕 2026-08-17 — put CLARVIS on your home screen (~5 min, phone only)

Two widgets are ready: a schedule widget (what you're doing now + what's next,
straight from your training grid) and a TALK button that opens the mic.

1. Install the free **Scriptable** app from the App Store.
2. In the CLARVIS **web chat**, ask: **"give me my widget setup"** — it replies
   with two scripts and the exact steps. Paste each into Scriptable, add a
   LARGE widget (schedule) and a MEDIUM one (talk) to your home screen, point
   each at its script. The optional "Hey Siri, talk to CLARVIS" shortcut is in
   the same reply.

Same node rule as the sync URL: ask in the web chat, not the Mac.

### 🆕 2026-08-16 — connect your training app to CLARVIS (~1 min per device)

CLARVIS is now the sync backend for your training app (the Weekly Schedule PWA)
— your real 30-minute schedule replaces the dead Google Calendar everywhere
(RIGHT NOW block, Today panel, schedule questions, workouts). One step, on each
device that uses the app:

1. Ask CLARVIS in chat: **"give me my training sync URL"** and copy it.
2. Open the training app → tap **Sync** → paste the URL into the Database URL
   field → **Connect**. That device's data pushes up immediately; every edit
   after that syncs within ~1 second.

**Ask in the web chat, not the Mac node.** Verified live: the two nodes have
different access codes, and without a pinned token each derives its own — so a
URL built on the Mac carries a token the server rejects. CLARVIS now warns you
when that's the case, but the web chat is the one that always answers correctly.

Worth doing while you're in Coolify anyway: set **`TRAINING_SYNC_TOKEN`** to any
long random string (and the same value in `~/.zshrc` on the Mac). That pins one
URL for good — both nodes agree, and rotating the access code stops moving it.

If you had a Firebase database connected in the app, the CLARVIS URL simply
replaces it: your data pushes up on first connect, and the Firebase copy can be
deleted afterwards.

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

## ✅ 2026-08-14 LATE NIGHT — full functionality audit, everything fixed same night

A 6-agent audit swept server runtime, Mac node, the 838-test suite, sync+money
plumbing, and mail/queue watchers, then a critic pass hunted for what the sweep
missed. Every defect found was fixed and verified before 1 AM:

- **Reminder-feed ordering bug** (found because the suite ran at 23:17): date-only
  dues sorted *before* same-day timed dues, backwards from the system's own
  end-of-day semantics. Fixed + pinned by a deterministic test (`e1f83f2`).
- **Task-manager web lane** was still scraping DuckDuckGo HTML; now rides the shared
  Tavily-first stack with the scrape as fallback (`8a7fe33`).
- **Self-check scored three interchangeable search keys individually**, so a healthy
  node read DEGRADED forever. Now one group check — the Mac node's first-ever
  🟢 HEALTHY startup (`e9f2337`).
- **Screen relay had been silently dead since RLS went live** — a Jul 29 process
  still holding the revoked anon key, polling empty results every 1.5s. Restarted
  onto the new key (Alex's kickstart).
- **Legacy Supabase JWT keys disabled** (Alex's click) and *proven* dead: old key now
  gets 401 "Legacy API keys are disabled." Every pre-rotation credential in the
  system is now revoked.
- **Mac node moved under launchd** (`com.secondbrain.chatapp`, KeepAlive) — survives
  reboot/crash and re-reads `.env` on every restart, closing both the
  "7-commits-stale for a week" and the "stale key in a long-lived process" modes.
- Both nodes verified on the same final commit; UptimeRobot green throughout.

## ☀️ Tomorrow morning (Aug 15) — the short list

1. **Stripe** (top slot, ~15 min, due before 08-20 — Wave 1 opens 08-21 and a reply
   that converts with no way to pay burns the lead).
2. **Confirm the warmup nudge actually hit your phone** (~10:30). Today was streak
   day 1; the daily `august:streak:warmup-daily` push has never fired before. If no
   push arrived, tell Claude Code — that's a bug to chase, not your memory failing.
3. **Warmup day 2**: two more sends from the studio account, spaced.
4. **Outside-reader email + bin the two stale drafts** (one gmail visit).
5. **Glance at the phone HUD** (10 seconds, still unverified on real glass).
6. Mac disk is at 91% — ~8.9 GB of it is `~/Library/Caches`. Worth a cleanup pass
   soon; deletions are yours, not CLARVIS's.

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
