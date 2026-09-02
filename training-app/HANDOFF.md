# Basketball system — handoff

Written 2026-08-31, updated 2026-09-02 (section 10). Read this whole file before touching anything. It covers the working
agreement, the current state, every decision made in this session, the mistakes I made and got
corrected on, and exactly where the conversation stopped.

---

## 1. How to work with Alex on this — read first

He has 12 years playing experience. You have none. **He supplies the basketball content; you
supply structure, measurement and pushback.** Every time a Claude has invented basketball logic
he has corrected it; every time one built structure around his content it landed.

Hard rules he has stated:

- **Talk before you build.** Agree the shape first. He said this at the start and repeated it.
- **ONE question or ONE section at a time.** His words, 2026-08-31: *"give me one question at a
  time or one section at a time, youre throwing too much at me right now."* Do not end a message
  with four open questions. Ask the single most load-bearing one and wait.
- **Be brief.** He has asked for brevity at least four times. Terse checklists over prose essays.
- **Never delete anything from the library without asking.** He has since authorised specific
  deletions — those are logged below — but the default stands.
- **Never claim automation that does not exist**, and **never describe a deletion or change you
  did not actually apply.** He has been burned by both. Verify against live state before writing
  about it.
- **Name holes rather than hiding them.** He would rather hear "this is weak because X" than get
  agreement.
- **Do not over-systematize.** His stated failure mode for Claude: atomizing one concept per
  block when he wants compound drills that hit four things in one rep, and building taxonomies
  for their own sake.
- **Alex dictates placement.** Times of day and which days things land on are his call. Suggesting
  is fine; writing uninvited placements into the grid is not.

His three-mode model, in his words:

- **Vitamins** — daily floor, fundamentals, unscored, unstressed.
- **Good drills** — the daily push. Skill under constraint: perception, balance, pace. NO make
  standards (a make count on an awkward rep pays you to make it less awkward). Progress by LEVEL.
- **Reps** — grooving game movements. These KEEP make standards.
- **Split Second** — the fourth layer added later: the one place you do not know the answer when
  the rep starts.

---

## 2. Where everything lives

**Training app (the source of truth for the program)**
- PWA at `https://luminous-madeleine-bf89fa.netlify.app`, backed up at `training-app/index.html`
- Syncs to CLARVIS. Read/write the whole snapshot at:
  `https://clarvis.178.156.209.40.sslip.io/training-sync/360452df69bc4532c95986ff/trainingDashboard.json`
- GET returns `{rev, keys:{...}}`; keys are JSON strings. PUT the whole object back with a NEW
  `rev` (any fresh string) or devices will not pull it. Devices poll every ~8s.
- **Always GET immediately before you PUT** — never build a PUT on a stale copy.
- **macOS Python cannot verify the cert.** `urllib` fails with CERTIFICATE_VERIFY_FAILED. Use
  `curl` for the HTTP and Python only for the JSON surgery.
- Storage-key naming: sync keys use `_` where localStorage uses `.`
  (`workoutLibrary_v2` ⇄ `workoutLibrary.v2`).
- Library shape: `{"0".."6": {sel, pages:[{title, body} | {title, type:"table", columns, rows}]}}`
  Categories in order: Lifts, Good Drills, Bag shooting, 50/50, Conditioning, Group workouts,
  Court movement.
- Day cards: `weeklyWorkouts_v1` = `{"0".."6": "• item\n• item"}`, 0 = Sunday.

**Split Second app (the reads trainer)**
- Artifact: https://claude.ai/code/artifact/2d8a27b4-6abb-4205-b212-3c644cfcb441
- **Another session works on this concurrently.** Re-read with the Artifact tool immediately
  before editing or publishing, every time. It has been republished out from under this session
  twice.
- Call audio is pre-baked base64 AAC in `window.CALL_AUDIO`, keyed by lowercase call word.
  iOS `speechSynthesis` ignores Bluetooth routing and plays out the phone speaker, which is why
  clips exist. **Any new call word needs a clip baked** or it silently falls back to the phone:
  `say -v Samantha -o x.aiff "word"` → `afconvert -f m4af -d aac@48000 -c 1 x.aiff x.m4a` →
  base64 → add to CALL_AUDIO.

**Week grid artifact**
- https://claude.ai/code/artifact/2eec7814-f53b-4e1c-890f-bd11a697b84a — regenerated from live
  card data, not hand-typed. Regenerate it whenever the week changes.

**Repo**
- `~/second-brain`, private GitHub `alex100hickey-eng/Second-brain`. Coolify auto-deploys `main`.
- Read `CLAUDE.md` before changing code. Tests: `python3 run_tests.py` from the repo root
  (not from `second-brain-chat/`). Must be green before shipping.
- **Other sessions commit to this repo concurrently.** Stage your own files explicitly; never
  `git add -A`. There are currently uncommitted changes from another session in the working tree.

---

## 3. What was shipped this session

**CLARVIS library write-back — commit `7ad257f`, deployed and verified live.**
Two tools in `second-brain-chat/training_sync.py`:
- `edit_library_page(category, page, old_text, new_text)` — exact-match replace in a page body.
  The match must be verbatim and occur exactly once, so a loose match cannot clobber prose.
- `append_library_log_row(category, page, values)` — appends a row to a table page, filling the
  first blank row the way the app does.
Both go through the existing lock / undo / new-rev machinery. Also added a guard in `_mutate` so
no server-side write can grow the snapshot past `MAX_SNAPSHOT_BYTES` (200KB) — past that, the
`/training-sync` route would reject the app's own pushes and sync would break entirely.
Page create / rename / delete deliberately NOT included — those stay Alex's by hand.
Tests: 95/95 in `test_training_sync.py`, 1042/1042 full suite.

**Split Second — three shipped changes:**
1. The seven dictated sections from `split-second-defense.json` baked into `BUILTIN_DRILLS`
   (not `ss.custom`, because his phone's localStorage cannot be written remotely and baked-in
   survives a wipe; the cost is they are not editable in place).
2. **GO CUE mode** (`mode:'gocue'`, drill *Flip and Go*) and **GO CUE + STOPWATCH**
   (`mode:'stopwatch'`, drill *Chase*), both built to his written spec.
3. **Scoring.** When a scored set ends the app asks "how many did you make?" as a one-tap grid.
   Stored as a percentage in `ss.makes::<key>` because he sets reps freely and 18/25 ≠ 18/30.
   Shows best + last three on the drill screen. Beat your best twice running and it says to move
   the call later — it never changes his settings for him.
   **Scoring is offence-only** (`scoresMakes()` excludes `side === 'defense'`), because his
   Defense page says nothing there is scored on outcome.
4. **Passing section** — three calls, Left / Middle / Right, wall targets. Baked a `middle` clip.

---

## 4. Library changes — 58 pages down to 30

**Deleted with his explicit authorisation** (27 in one pass, then more):
Bag Shooting (superseded) · 4 "(original)" bag archives · Reps Off Ball · REPS TRANSITION ·
Reps Passing · 3 "(original)" Good archives · Good Off Ball · Good Ball Screen · Good Defense ·
Defense - Navigate Close Out Contain (original) · SPLIT SECOND - Modes to build (both modes are
now built) · SPLIT SECOND - The decision layer (stale) · REPS - How to run them · OPEN QUESTIONS ·
all 9 LOG tables · Bag off Screens · Bag Off Dribble and Bag once by (folded, see below) ·
**THE CLOSER**.

Content preserved rather than lost when pages went:
- OPEN QUESTIONS' three live drill questions were moved onto TO BUILD first.
- The never-take-the-same-rep-twice rule was written inline into the surviving bag pages before
  REPS - How to run them was deleted.
- Bag Off Dribble + Bag once by were **folded into one page, DRIBBLE BAG**, all 21 drills carried
  over verbatim at their existing standards, split into Half A (off the dribble, midrange, 9
  drills / 17 sides) and Half B (once by, paint, 12 drills / 24 sides). Whole, it is 41 sides,
  roughly 150 attempts.

**The 50/50 Log table was deleted and then restored at his request** — deleting it broke
`log_5050`, `fifty_fifty_trend` and `logged_5050_on` in CLARVIS. That last one matters: it always
returned False, so the daily prompt asking for his numbers would never go quiet.

**The Closer was killed outright**, 2026-08-31. His words: *"delete the closer. thats some
bullshit not gonna lie. get it out of the system completely."* Page deleted, removed from all
seven cards, TO BUILD item 7 marked KILLED. Do not resurrect it.

---

## 5. Pages written this session

**READS - The Daily Block** (Bag tab) — every action, every read, every day. He sets reps per
section, 20–30 is the working range, more on some sections than others. Score = makes out of the
reps he set; a rep counts only if he did what the call said AND finished it. Ladder is the call
landing later, not the shot getting harder: L1 few calls / call in the countdown → L2 full call
set → L3 call lands late → L4 window tightened → L5 chaining, **marked NOT BUILT** (needs app
work; he parked it: *"we need to get there software wise which im not worried about at the
moment"*).

**ON THE MOVE - The Shooting Block** (Bag tab) — replaced Bag Shooting, which ran on gates
("2/3 to move on") that leave no number behind. His dictated content: eight movements in two
fixed halves — **A (off a screen):** fade, curl, zip up (off an elevator or a pin), deep slides;
**B (on your own):** drift, lift, transition, back pedal. **Six shots off every one:** catch and
shoot, no dip, jab-step back-shoot, shot fake-sidestep-shoot, shot fake-pull up, jab-shoot.
Two attempts each, 48 shots a session, halves alternate. Each half carries its own bar and its
own setting. Bar moves up on two clearances in a row or clearing by three (two, not one, because
at 48 shots a single make is 2%). When the bar gets high, one setting moves and the bar resets.

**DRIBBLE BAG** — the fold described above.

**RECORDING - Does It Sell** (Court movement) — four days a week (Sun/Mon/Fri/Sat), 10–15 min at
the END of a workout, warm. His reasoning: film the move when the handle and body feel right, so
you are watching the version you actually own. Judged not on makes but on **whether a defender
would bite** — verdicts are sells / doesn't sell / can't tell. The one design call I made:
**the camera stands where the defender stands**, in front at his height, not side-on like Film
Protocol, because deception cannot be judged from an angle nobody is being lied to from. Three or
four moves a session, three reps each, ~a dozen clips. Keep one best-rep clip per move as the
reference. The moves that don't sell are next session's work.

**Passing** (Good Drills) — rewritten. Was four court targets; now **three wall targets, left /
middle / right**, small, taped. 10–15 min, four days (Thu/Fri/Sat/Sun). The call names the
target, the round names the pass. His entries, passes, L1–L9 ladder and standard all kept as he
wrote them.

---

## 6. The week as it stands

Morning is settled and unchanged apart from the Closer being removed:
Vitamins → Movement A/B/C → Good Handles ×2 → **Good Finishing Sun/Wed/Fri, Good Shooting the
other four**.

| Day | Afternoon |
|---|---|
| Sun | Passing · Dribble Bag B · Reads · Defense · *jump A* · Recording · 50/50 |
| Mon | On The Move A · Reads · *heavy lower* · Recording · 50/50 |
| Tue | Dribble Bag A · Reads · Defense · *upper* · 50/50 |
| Wed | On The Move B · Reads · *jump B* · 50/50 |
| Thu | Passing · Dribble Bag B · Reads · Defense · *heavy upper* · 50/50 |
| Fri | Passing · On The Move A · Reads · *lower lengthened* · Recording · 50/50 |
| Sat | Passing · Dribble Bag A · Reads · Defense · *mobility* · Recording · 50/50 |

Frequencies he set himself: **Defense Sun/Tue/Thu/Sat** ("to start"), **Passing Thu/Fri/Sat/Sun**
(four, down from five), **Recording Sun/Mon/Fri/Sat** (four, up from the two he first said).
Reads and 50/50 daily. Dribble Bag 4, On The Move 3 — that split was my proposal and he has not
objected, but it is mine, not his.

---

## 7. Corrections he made — do not repeat these

1. **Burnsey, Lowman, Reverse through, McGarrity, Misdirection are WARMUP DRILLS, not game
   moves.** I read them as moves and built four of Split Second's seven Handling calls out of
   them. That section was broken and has since been removed from the app entirely by the other
   session. They legitimately live on the Good pages as *handling entries* — how you arrive at a
   drill.
2. **Transition and off-ball are group work, not solo.** Their solo pages were deleted. But the
   off-ball *movements into a shot* (drift, lift, fade, curl, zip) survive as shooting entries on
   ON THE MOVE — the movement is a solo skill even though the decision is not.
3. **Box-outs and rebounding** — he gets better at those by lifting and applying at practice.
   Do not build a solo box-out drill.
4. **Catch-and-finish off a cut should be a GOOD drill, not a reps drill** — his reasoning: it is
   about moving fast and not knowing what is coming, so mix in the ball arriving off a high lob
   or a low pass. Parked for later, his words: "we can get to that when it comes."
5. **Beating a man off the dribble already has reads in the hedge section** — he told me not to
   worry about that gap.
6. **He can see the ball-screen coverage** — the defense calls it out loud. Knowing it in advance
   is realistic, not a design flaw. The real read is what defenders do inside the coverage.
7. **No contact or physicality in solo work.** A wall does not push back; training contact on one
   teaches you to lean.

Also worth knowing: **all nine LOG tables were empty, zero rows, while the Closer's inline line
had `8/30 3:44` written on the page.** He logs inline, where the work is; he does not navigate to
a table. Every measurement built since puts the number on the page or in the app.

---

## 8. Open items

- **His move list** — the moves he would actually use to beat somebody off the dribble. Blocks the
  Recording rotation and any rebuild of Split Second's Handling section. Asked twice, not yet
  given.
- **Two bars and eight distances** on ON THE MOVE. He runs each half twice, then sets the bar from
  his own numbers. Deliberately blank — do not guess them.
- **Compete + Fatigue** and **Film + IQ** — both written, neither placed in a day.
- **Conditioning** — nothing in the system trains it. He parked it: "well get to conditioning
  after we handle all the other stuff." Later he said the holes I named (conditioning, fuelling,
  pre-competition routine, in-season plan, chaining) are "all handled right now."
- **Movement - Drive Position** still says to log the letter in LOG - Movement + Defense, a table
  that was deleted. Needs fixing.
- **Deep slides off an off-ball screen** is still undefined; running as a long lateral slide into
  the catch until he says otherwise.
- Chaining reads (L5) needs app work.

---

## 9. Where the conversation actually stopped — the mental side

This is live and unfinished. He raised it himself, unprompted, right at the end:

> *"theres a big part of my game that ive been sleeping on. and it costed me yesterday. the mental
> side. not just what read to make or basketball IQ. the mental health piece. confidence,
> happiness, and enjoying the game. I need to work on things like meditation, breathwork, etc. so
> that my nervous system doesnt get fried when i go to play."*

What happened, in his words: first practice with a new team. A coach he rates poorly, a big
roster, and nothing he has done there yet to prove himself. He was nervous going in, **let his
mistakes compound**, played **hesitant — "playing to not make a mistake rather than to score"**,
felt like he was having a **panic attack**, and his **body felt weak**. He has had issues before
but never that bad. The arc was **hot first, then cold as it got worse**.

I told him: a recurring thing that got that bad is worth taking to someone, CWRU counselling is
free, and sports psych is standard equipment at the level he is chasing. Say it once, plainly,
and do not turn it into the whole conversation or treat him as fragile. He is a competitor asking
for a tool.

The loop I named for him, which he did not push back on: nerves put the body in a threat state
(the weakness); with no specific thing he was trying to *do*, "don't mess up" filled the empty
slot; avoidance goals make you slow because every option gets risk-checked; slow produces the
mistakes; the mistakes confirm the threat. The antidote is not more confidence — confidence is
downstream of evidence and he has none yet at this school. It is having something specific he is
hunting, and **he already wrote that page**: Practice Intentions, one thing, chosen before he
walks in. None of those intentions require him to feel good first.

**The shape I proposed and he has NOT yet answered:**

- **A daily practice**, so the system is trained before he needs it. He already has 5 min
  meditation and prayer in the morning and slow breathing at night, but neither is a deliberate
  practice — "slow breathe" is a note, not a protocol.
- **A competition protocol in three parts:**
  - **Before** — extended exhale to bring the hot down. Target is not calm; calm is flat and flat
    gets you beat. Activated without threat. Then the intention, so the slot is full.
  - **Between possessions** — a short physical reset, 2–3 seconds, done with the body so it is not
    a thought he has to have. This is the one that stops the compounding.
  - **When it has gone cold** — the opposite of all of the above. No slow breathing; that sinks
    him further. Sharp breath, hard movement, eyes outward, and one effort-only goal that cannot
    fail (sprint the floor, talk, box out). Effort still works when skill is gone and it rebuilds
    evidence.

**The exact next thing to do:** he was asked "does that shape look right before I write it?" and
asked for this handoff instead. Pick that question back up. Do not write the pages until he says
yes to the shape.

Two things to be careful of when you do build it: do not ladder confidence or enjoyment — they are
not L1–L9 material, and turning them into a scored system is exactly the over-systematizing he
warned about. And the cold-state tools are the opposite of the hot-state tools; getting them
backwards makes it worse.

---

## 10. 2026-09-02 session — dead references fixed, mental page written

Alex opened with "look at HANDOFF.md so we can continue optimizing my basketball system", asked
whether the whole system had actually been read (it had not — only the outline), then said
**"do whatever you think."** Everything below was done on that authority. He has NOT read the
mental page yet.

**Live library write, rev `be94b1b67314cec0`** (direct GET → JSON surgery → PUT, 35 exact-match
edits each verified to occur exactly once, read back and verified). Backup of the pre-write
snapshot (rev `6905308527dd4391`) is in that session's scratchpad as `live_before.json`; the
edit script is `apply_edits.py` beside it. 30 → 31 pages, 118KB of the 200KB cap.

**What the 35 edits did.** The 8/31 deletion of the 9 LOG tables left 14 pages telling him to log
in tables that no longer existed, and — worse — the Mon A/B, Sat A/B/C, Movement A/B/C and
Film + IQ A–D rotations all *read their letter* from those tables, so nothing said which letter
today was. Every reference now points at an inline line on its own page, overwritten in place the
way the `___ lb` blanks are (his logging truth: he writes where the work is, never in a table):
- `LAST RUN: ___ (A or B) date ___` on Mon Heavy Lower (+ `NEXT DELOAD: week of ___`), Sat Mobility,
  Movement - Drive Position, Film + IQ.
- `LAST SUNDAY ___ | top height ___ ft | attempts ___ | bound ___ in | box ___ in | sprints ___` on
  Sun jumps; `LAST WEDNESDAY …` on Wed jumps.
- `TEAM TEST: date ___ squat ___ bench ___ clean ___` on Rules - Missed Days + Testing.
- Good Finishing / Shooting / Handling / How-they-work / Defense: "put a row in LOG - Good Drills"
  → "overwrite the NOW line next to that drill".
- Movement monthly benchmarks got `___ s (last month ___ s)` blanks. Film Protocol got `LAST FILMED`.
- Practice Intentions got `TODAY: ___ AFTER: ___ /10` + `LAST PRACTICE`. Partner Menu, Compete +
  Fatigue, PRACTICE - TRANSITION AND READS got `LAST RUN` / `PICKED` lines. Film + IQ got
  `LAST COUNT: turnovers ___ passed-up ___ beaten ___`.
- TO BUILD refreshed: item 10 (box-outs do NOT come back — his 8/31 correction), 12 (deletions
  done), 17 (HANDOFF.md uncommitted), new items 20–23.
Nothing deleted. No card or grid change. Routine lines untouched.

**NERVOUS SYSTEM - Hot and Cold** — new page in Group workouts, right after Practice Intentions.
The shape from §9, written out: THE LOOP · THE TWO BREATHS (DOWN = physiological sigh, double
nasal inhale + long mouth exhale; UP = sharp nasal inhales + movement) · DAILY (his existing
"Meditate 5 minutes" = 5 min of DOWN breaths; "Slow Breathe" = 4 in / 6 out nasal; **and the
reset rehearsed in the gym after every 50/50 miss and every wrong Reads call — the structural
addition, zero extra time**) · BEFORE (3 DOWN breaths, say the ONE intention out loud, first
possession is an effort job; reappraise nerves as readiness, never "calm down") · BETWEEN (3-second
physical reset: cue → one DOWN breath → eyes to the next job; same after good plays) · GONE COLD
(the inverse: UP breaths, eyes OUT, voice, one effort-only goal for three possessions; open shots
still get shot) · WHAT THIS IS NOT (no ladder, no mood score; one yes/no line) · WHEN IT IS BIGGER
THAN A PAGE (counselling free, sports psych standard — said once, plainly).
Guardrails honoured: no confidence/enjoyment ladder, hot and cold tools kept opposite, nothing
placed in the grid. **It is his to edit or kill.** If he wants the daily piece wired in, the two
routine lines are the hook — do not rewrite his routine without asking.

**Found this session, not fixed (his call):**
- **Practice Intentions says pick ONE; PRACTICE - TRANSITION AND READS says pick TWO.** Contradiction
  in his own system. That page also still names "Reps Transition", deleted 8/31.
- ~~The week grid has no practice block~~ — WRONG, corrected the same evening: practices are dated
  entries in the app Calendar (Sep 8, 10, 15, 22, 29, Oct 6, 13) inside the Tue/Thu 3–7 gym block.
  And Alex's standing rule (2026-09-02): **do not schedule specific things in his calendar — it
  changes too often; his texts and emails are the source of truth about his life.**
- **Zero numbers recorded anywhere as of 9/2**: 65 load blanks empty, no `NOW: L` filled, 50/50 log
  0 rows. The program is 2–6 days old, so early rather than damning — but every progression rule
  runs on those numbers. Worth a look in two weeks.
- Conditioning still untrained (parked by him). Compete + Fatigue and Film + IQ still unplaced.

**Also that evening (Alex: "do whatever you think", then "optimize whatever you can"):**
- TO BUILD items 5 + 23 refreshed (rev `c4ca20b93e804e25`).
- One CLARVIS reminder filed as an `intake_event` row (Supabase id 17013, type `event` so it can never
  read as "missed"): heads-up before the Sep 8 practice to read the NERVOUS SYSTEM page. Text is hedged
  ("calendar says… it moves, trust the team chat").
- `daily_orders._card_headline` fixed: it derives the bullets shared by every card (Vitamins / Movement /
  Good Handles / Reads / 50-50) instead of a hardcoded list, so the day's ball order names the real work
  again. Test added (`test_headline_derives_shared_bullets`).
- `training-app/README.md` refreshed; `PENDING_SCORING_SPEC.json` stamped `_STATUS: SUPERSEDED` (its
  targets were deleted 8/31 — never apply it).

**Late evening — the team assigned a lift program (found by reading his school mail on his
"you can find most stuff about me in my texts and emails").** Coach Rocco Mitolo (S&C), email
"New Lifts" 2026-09-02 3:20 PM, unread by Alex at the time: **3 lifts/week Sun–Sat, never two in
a day, never more than 2 days in a row**, first-years on his first-year sheet (Google Sheet, login
only — could not read it), numbers into his own sheet + a Google Form the same day, 1 day this
week, Rocco in the weight room Mon/Wed 8:30–~10 (not Labor Day), **retest the week of 10/15**.
His strength-test numbers (tested Mon 8/31: **squat 335 / bench 285 / clean 205**, his own text that night) are now written on the Rules page's TEAM TEST line (rev after `c4ca20b93e804e25`). This collides with the
app's lift week (Mon heavy lower / Tue upper system / Thu heavy upper / Fri lower lengthened +
Sun/Wed jumps). Written to NEEDS_ALEX as item 1. **Nothing on the lift pages was changed.**

**Where it stopped / the ONE question to ask next:** which three days are the team's lifts, and
what happens to his own four loaded days (replace, merge, or stack). That outranks the NERVOUS
SYSTEM read-through and the reset-cue question — the team program starts full-bore next week.
Also filed for him: intake 17013 (hedged pre-practice reminder), 17015 (three unsent professor
drafts), outbox 16792 closed (advisor reply was sent 9/1).
