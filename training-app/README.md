# Training app (Weekly Schedule PWA)

Alex's training app — the weekly 30-minute-block schedule grid plus workout
cards, daily routines, everyday warmup, and workout library. **The live copy is
the Netlify deploy at https://luminous-madeleine-bf89fa.netlify.app/** — this
folder is the source of truth in git (recovered 2026-08-16 from the live site;
edited here since). It was built in an earlier session and previously existed
nowhere in git.

The COURT/SKILL program was rebuilt with Alex 2026-08-28..30. **That rebuild lives only in
the synced snapshot, not in this file** — the pages are his own content, so they were never
`DEFAULTS`. `index.html` seeds a fresh install; the live library is the source of truth and is
read/written at `/training-sync/<token>/trainingDashboard.json`.

Read `TO BUILD` (Court movement tab) first — it is the authoritative record of what is agreed
and not yet built, and it is maintained in the app rather than here.

Shape of the current day: Vitamins, Movement, two Good Handles drills, Good Shooting or Good
Finishing, Closer, Passing (5x/wk), a bag package, Defense, the lift, 50/50. Mornings alternate
Finishing Sun/Wed/Fri and Shooting the other four.

Three modes, and they are Alex's model not mine:
- **Vitamins** — daily floor, unscored.
- **Good drills** — skill under constraint (perception, balance, pace). SKILLS ONLY: handling,
  shooting, finishing. No make standards, ever — a make count on a deliberately awkward rep pays
  him to make it less awkward. Progress by per-drill level ladders (`NOW: L___`).
- **Reps** (Bag pages) — grooving game movements, make standards kept, run varied over two passes.

Rules that came from him and should not be re-litigated: drills are compound, never one block per
concept; off-ball and transition are not solo gym work; defense is stress plus reads, not
vocabulary; **no contact or physicality in any solo page** (parked on `GROUP WORKOUT IDEAS`).

Files added here alongside the app:
- `split-second-defense.json` — seven ready-to-load sections for his Split Second reads app, in
  its exact custom-drill shape.
- `split-second-modes-spec.txt` — spec for two engine modes that do not exist yet (a tone-based
  GO CUE at a varying delay, and the same plus a stopwatch he taps to stop).
- `PENDING_SCORING_SPEC.json` — a full scoring-layer design, 56 page edits, deliberately UNAPPLIED.
  It assumes CLARVIS can write library pages. **It cannot** — it can read the library and write the
  schedule grid and day cards only. That missing write-back is the fork the whole scoring layer
  waits on.

App version 8 (2026-08-27) rebuilt the lift program:

- The week was re-cut around Alex's own stated framework — court work is the
  sport-specific stimulus, so lifting exists only for max force and injury
  resilience; dunking and sprinting replace plyometrics. Sun/Wed are the two
  jump days (Sun one-foot/lateral, Wed two-foot/vertical), Mon is heavy lower,
  Thu is heavy upper, Tue is the upper system day (light — practice is that
  afternoon from Sep 1), Fri is lower lengthened, Sat is mobility.
- `WDEFAULTS` (day cards) and the seven `cat:0` "Lifts" pages in `DEFAULTS`
  (ids `lifts-v8-*`) were replaced wholesale. Every externally loaded lift now
  carries a `___ lb` slot and every page ends in a PROGRESSION block — the old
  pages recorded no loads at all, which was the program's biggest gap.
- Migration is guarded by `programRebuild.v8` in localStorage and is
  deliberately conservative: a day card is replaced only when it still matches
  the old seed string exactly, and an old lift page is removed only when its
  title AND body length both match the original. Anything hand-edited survives.
  Verified against a copy of the live 2026-08-27 snapshot: 5 old lift pages
  removed, 7 new seeded, and the schedule grid, 50/50 log, bag pages, Good
  Drills, warmup and calendar all untouched.

App version 6 (2026-08-19) added the weekly reset:

- Column headers carry the current week's dates (computed client-side, Sunday
  start, 3 AM day boundary) and highlight today.
- Two grid layers: the repeating week (`weeklySchedule.v1`, unchanged shape)
  plus a one-week overlay (`weeklyOnce.v1` = `{weekStart, cells}`) that renders
  amber, covers the repeating cell it sits on, and is wiped client-side when
  the week rolls over. The "Edits:" header toggle picks which layer edits land
  in; cells already in the once layer always edit the once layer.
- "Calendar" (`bigObligations.v1` = `[{date, text}]`, v7 renamed from "Big
  Stuff Coming Up"): every day of the next 3 weeks renders as its own box (a
  rolling window computed from today — empty days are dashed placeholder
  slots, only days with text are stored), then an "after that" zone showing
  only dated big-big stuff plus "+ Add a date". Duplicate dates merge on
  render. Past dates purge on load.
- Server counterpart: `second-brain-chat/training_schedule.py` parses both new
  keys (once layer merges into events via `grid_for_week`; obligations feed
  today's context and the schedule tools), and `training_sync.py` gained
  `edit_schedule(this_week_only=…)` + `set_big_obligation`.

Single self-contained HTML file, no build step. All data lives in
localStorage; the built-in Sync feature mirrors everything to any URL speaking
Firebase's REST shape (`PUT`/`GET <base>/trainingDashboard.json`) — which is
now the CLARVIS server's `/training-sync/<token>` endpoint (see
`second-brain-chat/training_sync.py`). Ask CLARVIS in chat for the sync URL
(`get_training_sync_url`).

If you edit this file, redeploy to Netlify AND keep this copy in sync.
