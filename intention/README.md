# Intention

Runs Alex's day from the training app's live schedule. At the right time it says **what** he is
doing, **how**, and **why** (tied to one of four goals: HEALTH, BASKETBALL, CLASSROOM, MONEY).
He taps Done and the next thing appears. The training app stays the editor; Intention reads it
live and never keeps its own copy of the schedule.

- App: https://intention-hickey.netlify.app (PWA, installed to his iPhone home screen)
- Netlify site id `04b531c4-e5a2-4642-b29c-591b5f4abcd4`, team "Training" (his other three
  sites are NOT this one — deploy only with `--site`)
- Code: `~/second-brain/intention/` (this folder). The brief is `BRIEF.md`.

## How it works

```
training app (Netlify PWA) ──sync──▶ CLARVIS /training-sync/<token>/trainingDashboard.json
                                                     │ GET (server-side only, env TRAINING_SYNC_URL)
                                                     ▼
                     lib/day.mjs  ── grid + routines + workout cards + library ──▶ the day's items
                     content/items.json ── how / why / goal / tool / notify defaults (drafts marked)
                     Netlify Blobs ── per-item edits, done taps, journal, subscriptions, settings
                                                     │
                     netlify/functions/tick.mjs (every minute) ── web push (VAPID) ──▶ iPhone
                     netlify/functions/api.mjs ── /api/* for the app and for Claude sessions
```

- **Grid semantics**: 48 slots of 30 min from 3:00 AM, columns Sun=0..Sat=6, key `"slot|day"`.
  A day runs 3 AM → 3 AM. Cell text carries exact times ("MATH 120 · 9:20–10:10") and those win
  over the slot. `weeklyOnce_v1` overrides the repeating grid for one week.
- **Items**: each grid block is an item (kind by title: routine-am / routine-pm / gym-warmup /
  gym-morning / gym-main / fifty / class / meal / study / transition / sleep / gameday). Blocks
  have steps: routine lines (a line ending in `:` groups the lines after it into a checklist),
  the day's workout-card lines for gym blocks (each linked to its library page), GAME DAY steps
  for practice/game. Step planned time = block start + earlier steps' durations.
- **Item ids are stable across days**: `block:<title without times>`, `am:<line>`, `pm:<line>`,
  `drill:<card line>`, `weekly:pressure-test|hpv|bolt|belief-alarm-N`, `cal:<calendar text>`.
  See `norm()`/`cleanTitle()` in `lib/day.mjs`. Renaming a line in the training app changes its
  id, so its edits no longer apply (they stay in the store, harmless).
- **Content precedence**: `content/items.json` kinds → items → Blobs overrides (edits made in the
  app or via the API). `draft: true` = written by Claude, not yet approved by Alex.
- **Depth standard (his, 2026-09-24)**: `how`/`why` are the short push/NOW-card text. The real definition
  is `guide` = `{ title, intro, steps: [{ title, do, intention }], whole, remember, source }`: the exact
  step-by-step, the intention of each step, and how the practice helps him as a whole. A string value
  names a shared entry in `content.guides` (the 6 R's sheet is `guides.sixr`, used by the `sixr` kind and
  by `am:meditate-5-minutes` via `guides.meditate`). The app renders it as a numbered sheet on the item
  screen. Every item should get one; write guides in our own words from his library pages and Nick
  Sweeney's docs (`training-app/nick_sweeney/`, git-excluded), never paste those docs. Set one via the
  API: `PUT /api/item/<id> {"guide": {...}}` or `{"guide": "sixr"}`.
- **Notifications**: the scheduled function runs every minute, builds yesterday/today/tomorrow,
  and pushes anything whose `startAt − lead` fell in the last 3 minutes and was not sent and not
  done. Older than 3 minutes = dropped quietly (nothing is ever "missed"). Steps inside a block:
  the block's push names the first step; a later step pings only once the step before it is Done
  (so a routine he does without the phone does not ping 13 times). Max 3 pushes per minute.
- **6 R's** (his ask 2026-09-23): `sixRItems()` adds `settings.sixr.count` reminders a day (default 4, 0 = off),
  ids `sixr:1..N`, at random 5-minute marks between the end of the morning routine and 30 min before sleep,
  at least 60 min apart, never inside a class or practice/game (10-min margin). Seeded by the date so the
  app and the scheduler agree; new times every day. Kind `sixr`, tool = six tap-through words.
- **Calendar lines** (`bigObligations_v1`, e.g. "Practice 4:00–5:50") have no slot to anchor AM/PM:
  `calendarTimes()` honours an explicit am/pm, else 7–11 = morning and 12, 1–6 = afternoon/evening.
- **Done is the log**: `done/<date>` in Blobs = `{ itemId: epochMs }`. A block with steps counts
  as done when all steps are; marking the block done covers its steps.
- **Write-backs into the training app** (the only ones, ever): BLUE LIST, SCOREBOARD - Confidence
  and Control, 50/50 → Log. Protocol in `netlify/functions/lib/writeback.mjs`: GET fresh → back up
  to Blobs `backups/<rev>` → change only that table's rows → re-GET and abort if the rev moved →
  PUT whole snapshot with a new rev → read back and verify. Never from a cached snapshot.

## API (for a Claude session updating items from chat)

All routes except `/api/health` need `Authorization: Bearer $INTENTION_KEY`. The key is a Netlify
env var (production context, secret — Netlify masks it on read, `env:get` shows only the last 4
chars). A copy lives in the macOS Keychain on Alex's Mac:

```bash
KEY=$(security find-generic-password -s intention-api-key -w)
```

If both are lost, rotate: `npx netlify-cli@latest env:set INTENTION_KEY <new> --context production --secret`,
redeploy, `security add-generic-password -a alexhickey24 -s intention-api-key -w <new> -U`, then
re-pair his phone (Settings → Forget key, then a new code from `POST /api/pair`).

To pair a new phone: `curl -sS -X POST -H "Authorization: Bearer $KEY" https://intention-hickey.netlify.app/api/pair`
returns a 6-character single-use code valid 24 h; he types it on the app's first screen.

| Route | Does |
|---|---|
| `GET /api/day?date=YYYY-MM-DD` | the built day (items, steps, done map, rev). Default = current grid day |
| `POST /api/done {date,id,done}` | mark/unmark done |
| `GET /api/overrides` | every per-item edit |
| `PUT /api/item/<id> {how,why,goal,tool,notify,lead,duration,title}` | edit an item (any subset; `null` clears a field) |
| `DELETE /api/item/<id>` | remove the edits → back to `content/items.json` |
| `GET/PUT/PATCH /api/settings` | weekly placements: `{pressureTest:{day,time}, hpv:[{day,time}×3], alarms:[{day:'*',time}×3], bolt:{day,time}}` — day `0..6` or `*` |
| `GET /api/preview-due` | every ping the scheduler would send for yesterday/today/tomorrow |
| `POST /api/push-test {inMinutes}` | test push (0 = now); opens the current item on tap |
| `POST /api/tick` / `GET /api/tick-log` | run the scheduler now / last 60 tick reports |
| `GET /api/subscriptions` | subscribed devices |
| `GET/POST /api/journal` | his journal text (private) |
| `POST /api/writeback {table:'blue'|'scoreboard'|'fifty', row:[…]}` | one of the three write-backs; `match:{col,value}` replaces a row |

Example — change an item's why and turn its ping off:

```bash
KEY=$(security find-generic-password -s intention-api-key -w)
curl -sS -X PUT -H "Authorization: Bearer $KEY" -H 'content-type: application/json' \
  https://intention-hickey.netlify.app/api/item/am%3Acars \
  -d '{"why":"Range before load.","notify":false}'
```

Ids with `:` must be URL-encoded (`%3A`). Get the ids from `GET /api/day`.

To change a **default** for everyone-day (not a one-off edit), edit `content/items.json` and
redeploy. To add how/why for a new routine line or drill, add its id there.

## Deploy

```bash
cd ~/second-brain/intention && npm test && \
npx netlify-cli@latest deploy --prod --dir public --functions netlify/functions \
  --site 04b531c4-e5a2-4642-b29c-591b5f4abcd4 --message "what changed"
```

Bump the `?v=N` on `app.js`/`style.css` in `index.html` and `VERSION` in `sw.js` + `app.js` when
the client changes, or phones keep the old shell. Env vars: `TRAINING_SYNC_URL`, `INTENTION_KEY`,
`VAPID_PRIVATE_KEY` (secrets, production context), `VAPID_PUBLIC_KEY`, `VAPID_SUBJECT`.
Never commit them; `.netlify/` and `.env*` are gitignored.

## Debugging notifications

0. `GET /api/diag` — the phone reports every step of Enable notifications (boot, enable-tap, permission,
   sw-ready, subscribed, saved-on-server, test-tap, *-fail). Ignore rows whose `ua` is Macintosh (the
   browser pane). First real failure 2026-09-23: permission granted but nothing saved — the boot
   `autoSubscribe` self-heal fixed it on the next open.
1. `GET /api/subscriptions` — is his phone subscribed? (empty = re-do Settings → Enable)
2. `GET /api/preview-due` — is the item scheduled when he expects?
3. `GET /api/tick-log` — did the minute tick run, what did it send, any push errors
   (410/404 = dead subscription, removed automatically).
4. `POST /api/push-test {"inMinutes":0}` — an immediate push; if this lands, the pipe works.
5. iOS: web push only works for the app **installed to the Home Screen** (Share → Add to Home
   Screen) and opened from that icon, iOS 16.4+. Permission is per-install: reinstalling the icon
   means re-enabling. If a few pushes show nothing, iOS revokes the subscription — the service
   worker always shows a notification for that reason.
5b. **Tap → right item** has three layers because iOS usually kills the suspended app and cold-boots
   it on tap: (1) `sw.js notificationclick` calls `clients.openWindow(url)` promptly (never await a
   cache write first — iOS 18.7 then launched the app at start_url with no hash); (2) the target is
   parked in CacheStorage `intention-nav` `/__pending` and read by `consumePending()` at boot and on
   every return to the foreground; (3) the server keeps `last-push` (set by the tick and by
   push-test) and `maybeRouteToLastPush()` opens it if the app launches within 10 minutes. The SW
   logs to `/__swlog`; the page uploads it as `sw:*` rows in `GET /api/diag`.
6. Scheduled functions run only on the published deploy, UTC cron; everything here uses
   America/New_York wall-clock so DST does not drift.

## Local

```bash
npm test                                             # engine tests (fixture = a live snapshot)
node scripts/show-day.mjs 2026-09-24 --snapshot test/fixtures/snapshot.json --drafts
TRAINING_SYNC_URL=... node scripts/show-day.mjs      # against the live snapshot
```

## Rules baked in (from the brief)

- Times and placement live in the training app. Intention never moves anything on the grid.
- Nothing is ever labeled missed. Skipped items drop quietly.
- No mood scores, no streaks. The Done tap is the log.
- Journal text is private (Blobs only). The BLUE LIST row is the one thing that leaves.
- Don't invent basketball logic: how/why for training comes from his library pages; anything
  drafted is marked `draft` until he approves it.
