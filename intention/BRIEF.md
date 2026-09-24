You're building INTENTION, a new app for me (Alex Hickey). The frame is agreed; your job is to build it, deploy it and get it working on my iPhone. What matters most: it has to FUNCTION. Read this whole brief first. If your memory index lists intention_app, training_app_schedule_source or basketball_development_system, read those files too.

WHAT IT IS
Intention runs my day. At the right time it tells me what I'm doing now, how to do it, and why I'm doing it. Every "why" ties to one of my four goals: GENERAL HEALTH, BASKETBALL EXCELLENCE, CLASSROOM EXCELLENCE, MONEY MAKING. I tap Done and the next thing appears. Home screen = a big NOW card, NEXT under it, and today's timeline. My training app stays the place where my schedule and content get written and edited; Intention reads it live and never keeps its own copy.

MY GOALS (for drafting "why" lines)
- Health: sleep, recovery, energy, faith.
- Basketball: D1 by 2027-28, so this season is my audition. My CONFIDENCE AND CONTROL system exists to close my pressure gap (7-8/10 confidence in normal games, 4-5 under pressure).
- Classroom: near-perfect grades at CWRU.
- Money: cash in the bank from my businesses.

THE DATA IT READS: my training app
- The training app is a PWA at https://luminous-madeleine-bf89fa.netlify.app (Netlify site id d36bb648-d6a1-4cd9-b216-c1600bcd9822; repo copy ~/second-brain/training-app/index.html). Do not modify or redeploy it.
- Its data syncs to https://clarvis.178.156.209.40.sslip.io/training-sync/360452df69bc4532c95986ff/trainingDashboard.json. That URL works like a password: keep it server-side in an env var, never in client code or the repo.
- GET returns {rev, keys}. Keys use _ where the app uses dots:
  - weeklySchedule_v1: my week in 30-min slots. 48 slots starting 3:00 AM (a day runs 3 AM to 3 AM), columns Sun=0..Sat=6, cell key "slot|day".
  - weeklyOnce_v1: one-week overlay {weekStart, cells} that overrides the repeating grid for that week only.
  - bigObligations_v1: dated calendar items [{date, text}].
  - dailyRoutines_v1: {"morning": lines, "night": lines} - my routine steps.
  - weeklyWorkouts_v1: each day's workout card. warmupRoutine_v1: my everyday warmup.
  - workoutLibrary_v2: {"0".."7": {sel, pages:[{title, body} | {title, type:"table", columns, rows}]}}. Tabs: Lifts, Good Drills, Bag shooting, 50/50, Conditioning, Group workouts, Court movement, IQ. The IQ tab holds my CONFIDENCE AND CONTROL system (pages DAILY BASE, PRESSURE, HIGH PRESSURE VISUALIZATION, GAME DAY; tables BLUE LIST and SCOREBOARD - Confidence and Control). Most how/why for my routines and training is already written in those pages.
- Timezone America/New_York. Use wall-clock time so DST days don't drift.
- Write back into this snapshot ONLY for three table pages: BLUE LIST, SCOREBOARD - Confidence and Control, and 50/50 -> Log. Protocol: GET fresh -> back it up -> change only that table's rows -> PUT the whole snapshot {rev: new unique rev, keys: all keys} -> read back and verify. Never PUT from a stale copy.
- The training app polls every 8 s and pushes its full snapshot of its own keys, so any extra key you add to that node gets wiped. Intention's own state lives in its own store.

WHAT EVERY ITEM HAS
An item = anything in my day: a grid block (class, gym, dinner, study) or a step inside one (a routine line, a drill).
- when: time from my grid, or order inside its block (planned time = block start + earlier steps' durations)
- what: the title
- how: short instructions
- why: the intention, 1-2 lines, tied to ONE goal
- goal tag: HEALTH / BASKETBALL / CLASSROOM / MONEY
- tool: none, journal prompts, timer, breath pacer, counter, stopwatch, guided visualization, checklist
- notify: on/off plus minutes of lead time
Source the how/why from the library pages for training and mental items (e.g. "Journal the day out" -> DAILY BASE section JOURNAL THE DAY OUT; "Meditate 5 minutes" -> MEDITATE). Where nothing is written, draft a one-line how/why, mark it DRAFT, and show me ALL drafts in one message to approve or edit. Don't invent basketball logic.

ADJUSTABLE - this matters
- Every item stays in the day view even with notifications off. Example: I know when my classes are, so classes default to notify OFF but still show in my day.
- Defaults by kind (routine steps and gym ON, classes and meals OFF). I can override any single item.
- I can edit any item's how, why, goal, notify and lead time in the app, any time, and it saves.
- Times and the schedule stay in the training app. Intention never places or moves anything on my schedule; I dictate times.
- Weekly items I place once in Intention's settings: pressure test day, 3 visualization mornings, 3 belief-alarm times. Unplaced = not shown.
- A simple authenticated edit API plus a README, so a future Claude session can update items for me when I ask in chat.

ON TIME - the core requirement
- Notifications land within about a minute of each item's time. Tapping one opens that exact item.
- Skip the notification if I already marked the item Done.
- Nothing is ever labeled "missed". Skipped items drop quietly.

TOOLS INSIDE ITEMS (v1)
- Morning journal: TODAY I HUNT, BEING SEEN, I AM + my ethos.
- Night journal: RED, BLUE (3 wins), NEXT. The best win becomes a BLUE LIST row.
- Meditation timer showing the 6 R's.
- Breath pacer for 4-7-8 and the DOWN breath (two inhales through the nose, one long exhale). Vortex runs in my Coherence app, so that step says to open it, shows its job, and shows the safety line: holds lying down or sitting on the floor/bed, ended at the first urge, never standing or driving.
- High Pressure Visualization: its 5 steps with the day's scene, 10-min timer.
- Weekly pressure test: rounds A and B with make/miss taps; the gap is computed and written to the SCOREBOARD.
- BOLT stopwatch -> SCOREBOARD. 50/50 tap counters (5 racks of threes, 5 sets of free throws) -> 50/50 Log.
- Gym blocks: the day's workout card and its library pages as a drill checklist, with a stakes set at the end.
- Blocks named practice or game use the GAME DAY page steps.
- Belief alarms: a notification carrying the question.

RULES
- Private: the API needs a secret key kept in env vars and on my phone, never in the repo. Journal text is private.
- The Done tap IS the log. No mood scores, no streak guilt.
- Phone-first: big tap targets; text boxes must work with iPhone dictation.
- Don't touch my other Netlify sites. Netlify CLI is logged in on this Mac; deploy with --site to your own new site only.
- Code in ~/second-brain/intention/ (this brief lives there). Stage only your own files. Ask me before any git push: pushing that repo's main redeploys my CLARVIS server.
- Talk to me briefly, one question at a time, and narrate as you go. I do the phone steps (Add to Home Screen, allow notifications) - tell me exactly what to tap.

SUGGESTED BUILD (change it if something doesn't work)
- A PWA (manifest + service worker) installed to my iPhone home screen; iOS 16.4+ supports web push for installed web apps.
- Netlify Functions for the API, Netlify Blobs for Intention's state, and a Scheduled Function every minute that sends due pushes with web-push (VAPID keys in env vars). Check the plan's limits first.
- If iOS web push won't work reliably, fall back to ntfy scheduled messages (already on my phone; CLARVIS uses it). CLARVIS's own nudge rules (quiet hours, 12 a day) must not govern Intention.
- No Xcode on this Mac, so no native app for now.

ORDER - verify each milestone on my phone before the next
1. Walking skeleton: deploy, install on my phone, and prove one scheduled notification lands on time and opens the right item.
2. My real day: tomorrow from my grid and routines as items with goal tags; show it to me; per-item notify with defaults.
3. Item editing and weekly placement.
4. The tools and the three write-backs.
5. Gym blocks, GAME DAY and the confidence system.
6. README + a memory note on how to update items, deploy, and debug notifications.

Start by reading the live snapshot and showing me tomorrow the way Intention would run it. Then build milestone 1.
