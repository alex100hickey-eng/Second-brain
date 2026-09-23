# polybot

Alex's multi-strategy Polymarket bot. Design doc: vault `Money/Polymarket Bot — Design v1 (2026-09-12).md`.

## What it is
- Strategy modules, each with a mode `off → paper → signal → live`. The ledger decides who is ready
  (`report` prints the gate for every module: ≥30 signals, ≥50% fills, mark-to-market positive —
  closed net PLUS the open positions marked at the last price — **and ≥`min_us_signals` (10) settled
  signals on the Polymarket US books, in profit**). That last clause is the one that matters: most
  signals are offshore, which is a read-only proxy settling on a different rule, and until 2026-09-18
  a module could pass on offshore evidence alone and go live having never produced a single signal on
  the venue it would be spending real money on. `promote` flips passing modules to live; `auto_promote:
  true` in config.json lets the 07:00 report do it alone. Signals before `gate_since_ts` don't count
  (a rule change resets the evidence).
- One position per market in every mode (paper included). Model modules (`weather_hold`,
  `weather_model_update`) only trade buckets the market prices inside `hold_price_band` (6-94c) and
  skip any "edge" over `hold_edge_max_cents` (30c): the 2026-09-12 paper run lost $412 on 1-5c long
  shots and $107 on 40c+ edges — those are model errors, not market errors.
- One risk manager every order passes through: $20/market, $100 total, halt under $120 bankroll,
  $20 daily loss stop, sports off until flipped (Ohio), maker limit orders only, kill switch =
  `touch polybot/KILL`.
- Two venues. `offshore` (polymarket.com) is read-only: the price reference and the paper proxy.
  `us` (Polymarket US) is the only tradable venue and needs the key.

## Modules
| module | reference the market lags | status today |
|---|---|---|
| weather_lock | day's max locked after the peak | **paper — the candidate.** Only entries ≥ `lock_min_price` |
| weather_obs | NWS hourly observation kills buckets | paper. Skips buckets the book prices over `dead_bucket_max_bid` |
| weather_hold | Open-Meteo ensemble vs bucket price | **off** — backtest −8.9%, paper −$2,456 |
| weather_model_update | new model run vs last run | **off** — 71 closed paper trades, 63% wins, −$291, losing in every edge band |
| bucket_sum | mutually-exclusive buckets ≠ $1 | **paper — the one that doesn't need a forecast.** US only |
| hold_favorites | our own calibration table (run `calibrate`) | paper on offshore books. 0 signals ever until 2026-09-23 (see below) |
| leadlag | offshore price vs US book | paper. `pairs.json` + a 60 s recorder since 2026-09-23 (see below) |
| maker_rewards | incentive-program quoting | idle |

### bucket_sum: the only module that isn't a forecast bet
Every weather module bets our thermometer beats the book's, and the paper record says it doesn't.
This one bets nothing. If six buckets tile every possible temperature, exactly one pays $1 at
settlement, so buying one contract of each for under $1 is the difference — whatever the weather does.

It had **never emitted a single signal**, for four independent reasons, all fixed 2026-09-18:

1. `neg_risk` was `False` on every Polymarket US event (an adapter placeholder, not a finding) and
   the old gate returned on that flag alone. A flag was the wrong thing to trust: what makes the arb
   valid is that the buckets **tile**, so `exhaustive()` now checks the parsed ranges directly —
   open-ended at both ends, `next.lo == prev.hi + 1`, no gap or overlap. Works on both venues.
2. It demanded a **two-sided** quote everywhere. A buy-all set only needs asks: 50% of US
   event-minutes have an ask on every bucket, but only 7% have both sides — that threw away 7 in 8.
3. It sized legs in equal **dollars**. Across legs priced 1c and 73c that buys 500 of one and 7 of
   the other: a random basket, not a set. A set is N contracts of every leg or it is nothing, and
   `scan` drops the whole set if any leg's derived count disagrees.
4. It sized off quotes with **no idea whether they could be filled**. Legs carry `ask_qty`/`bid_qty`
   now, `None` means "never looked up" and blocks the trade rather than defaulting to a size.

**Cheap screen, expensive confirm.** Quotes come free with the event (one call); depth costs a call
per bucket. The runner spends that only when the quotes already show a set under $1 — 34
event-minutes out of 3,353 over 8 days of US books, so ~1% of scans pay for it. Tomorrow's event is
scanned alongside today's for the arb path only: an arb doesn't care when the market settles, and
tomorrow's thinner book is where a set under $1 is *more* likely.

**The venue's rate limit is a quota, not a burst.** Measured three ways on 2026-09-18 (calls 0.05s,
0.35s and 1.0s apart): the first five always succeed and the sixth is refused, so spacing buys
nothing and a six-bucket depth read cannot be done in one window. It replenishes in seconds, not
the ten minutes the old flat backoff assumed — that backoff cost a full depth read *and* blinded
the next scan, which is why every arb candidate on 2026-09-18 stood down. `USVenue` now spends from
a `CALL_BUDGET`/`CALL_WINDOW_S` token bucket (5 per 12s), so a depth read takes ~13s and completes.
The reactive backoff survives as a safety net because the gateway sees the whole CWRU campus IP and
other people spend from the same quota.

**What the books actually offered** (8 days, `python3 -m polybot.runner arbs --days 8`): ~3 candidate
event-minutes a day, most worth 1–5c per set, with occasional 10–48c. **Whether any of it is
fillable is still unknown** — the old snapshots stored no sizes. Every candidate now records depth,
which is the evidence that decides whether this is a business.

Two rails, because an arb that half-fills is worse than no arb:
- **Group execution exists now** (`Executor.place_arb_set`): every leg goes out `FILL_OR_KILL`, so a
  leg either fills whole at our price or does not exist — no partial fills, no resting remainder
  that fills later at a price that is no longer part of any arb. If any leg is killed, the ones
  that filled are sold straight back at the bid `IMMEDIATE_OR_CANCEL`: the spread on those legs is
  a known bounded loss, an unhedged basket is not. `Runner.handle_arb_set` accepts or refuses the
  set whole, so a cheap leg tripping a cap can't leave the dear legs filled.
- `arb_live_ok` is still **off**. The unwind path above has never run against the real venue, and
  the SELL_* intents are unverified. Flip it only after a live set has been watched.
- The gate counts **sets, not legs** (`decision_count`, via `meta.group`). Six rows from one episode
  are one piece of evidence; counting rows would let real money out after five observed sets.

### Why lock and obs have a price floor/ceiling
Both modules trade on a fact that has **already happened** — the day's peak is in, or a bucket can no
longer win. When our feed says that and a liquid book disagrees, the book has been right essentially
every time. Paper 2026-09-12..18:

| | with the guard | without it |
|---|---|---|
| `weather_lock` entries | ≥0.80: **21/22 wins, +$16.78** | <0.50: 0/8, −$160 · 0.50–0.80: 2/4, −$8 |
| `weather_obs` vs the bid | ≤0.95: the rest of the book | >0.95: **0/6, −$70** |

The shape behind it: every winner is capped (a 0.90 favorite pays 10c) and every loser costs the whole
stake, so `weather_lock` needs an **87% win rate just to break even**. One bad entry erases twenty good
ones, which is why these guards are price filters and not size tweaks.

## Run
```bash
cd ~/second-brain/second-brain-chat
python3 -m polybot.runner status
python3 -m polybot.runner scan                 # one pass over all 30 offshore cities, high + low markets
python3 -m polybot.runner settle               # fill/close paper signals from what the market did next
python3 -m polybot.runner report --days 7
python3 -m polybot.runner arbs --days 7        # every moment the US books offered a set under $1, and how deep
python3 -m polybot.runner promote              # flip every gate-passing paper module to live (writes config.json)
python3 -m polybot.runner calibrate            # ADD newly closed markets to the sample cache (incremental)
python3 -m polybot.runner backtest --days 7    # replay the weather modules on real past days (also Sundays 04:00)
python3 -m polybot.runner pairs                # match US markets to offshore twins for leadlag (needs the key, ~2 min)
python3 -m polybot.runner leadlag --minutes 20 # record the pairs + run leadlag on the loop's cadence, standalone
python3 -m polybot.runner loop                 # the schedule, forever
```
Tests: `python3 -m pytest test_polybot.py -q` (no network).

## leadlag: pairs and the recorder (2026-09-23)
Zero signals in its life, for three separate reasons — all three had to go:
- **No universe.** `pairs.json` was never written: the builder asked `events.list` for the US
  catalogue and that call answers *sports* unless given `categories=[...]` (it ignores `tagSlug`),
  and leadlag skips sports (Ohio). It was also pinned to 05:00, when this Mac is asleep.
  `USVenue.events_by_category` now lists the whole non-sports catalogue in ~20 calls (1,290 events
  on 2026-09-23, 1,175 of them politics) and gamma's `tag_slug` gives the offshore side.
- **Fuzzy matching is the wrong tool here.** Polymarket US copies offshore titles almost word for
  word, so the danger is a near-identical title asking a different question (Tarrant vs Denton
  County, October vs December). `pairs.same_question` demands equal content-word sets (fillers
  dropped, parties and plurals folded, a year only when both sides carry one), then the outcome
  labels must match the same way, and a pair whose two prices sit more than 25c apart is refused
  as a mismatch (on 2026-09-23 those were all US books quoting BOTH candidates of a race at 0.975).
  First build: **5,747 market pairs over 1,086 of 1,290 US events**, 623 with a two-sided US quote.
- **Nothing recorded the prices.** Its series came from the snapshot table (weather books only) and
  it ran every 5 minutes against a 120 s window, which cannot see a 2-minute move by construction.
  `pairs.PairRecorder` samples the first 40 US events' worth of pairs every 60 s (2 batched US calls
  + 1 CLOB call), keeps every sample in memory for the move test, and writes only CHANGES to the
  snapshot table (paper needs them to fill and exit). leadlag runs right after each sample.

## hold_favorites: why it never fired (2026-09-23)
- `calibration.lookup` needs n ≥ 25 in a band. The 2026-09-12 table held 146 samples in total —
  10 in 0.85-0.90, 15 in 0.90-0.95 — so every favourite looked up None. The build walked 300 events
  newest-first, which meant a couple of hours of 5-minute crypto coin flips, and the 03:00 rebuild
  never ran again (Mac asleep). Now: an incremental sample cache (`calibration-samples.json`), the
  Up-or-Down and sports tags excluded server-side, 6,606 samples on the first build (55 and 89 in
  the favourite bands). The lookup also falls back to `all` when a category cell is THIN, as its
  docstring always said (it only did when the cell was missing). min_n and shrink are unchanged.
- The universe was one gamma page: the 100 soonest-ending events, i.e. the next 40 minutes of crypto
  coin flips. It now pages the whole 7-day horizon (past gamma's 2,000-offset cap), minus coin flips,
  sports and the daily temperature events (those belong to weather_lock / weather_obs).
- It can still never pass the gate: every signal is offshore, and the gate needs 10 US signals.

## Daily jobs catch up
Calibration (03:00), the pairs build (05:00) and hold_favorites (09:00, 21:00) used to fire on one
exact minute — asleep, or stepped over by a 75 s arb pass. `Runner._due` runs a missed slot at the
next chance (the long ones outside 09:00-16:59, the arb window) and remembers runs in
`jobs-state.json` so a watchdog restart does not repeat them. The 03:00 snapshot prune is unchanged.

## The backtester
`backtest.py` replays each city-day hour by hour with the same strategy code: real bucket price
history (CLOB), the station's real hourly observations (METAR / NWS), and the forecasts that existed
that morning (Open-Meteo forecast archive + previous-run hourly). Signals are paper-filled on the
prices that followed and settled on the real outcome. It also scores the morning model probability
of the eventual winner for several hourly-rule discounts, which is how `hourly_rule_discount_f` gets
tuned from data. Output: `backtest-latest.json` + a summary in the log.

## Live-day tools (activate with the key)
- `execution.py` — order sync every 5 min: fills → take-profit sells, stale orders → cancel, kill file → cancel_all.
- `pairs.py` — title/date matcher that writes `pairs.json`, the leadlag universe.
- `notify.py` — a module in `signal` mode nudges the trade to Alex's phone instead of placing it.

## Going live (later, in order)
1. `pip install polymarket-us`; put `POLYMARKET_KEY_ID` and `POLYMARKET_SECRET_KEY` in the server env.
2. `status` shows `us venue: ready`; bankroll is read from the account.
3. Flip one module to `signal` in `polybot/config.json`; nudges only.
4. `promote` (or `auto_promote: true`) flips a module to `live` once the gate passes. Orders are limit/GTC;
   `cancel_all` on the kill switch. A live module's offshore signals keep being recorded as paper.

## Venue rules that matter
- Offshore weather markets resolve on the HOURLY "Temp" column of the station in the description
  (NYC = LaGuardia KLGA). Polymarket US settles on the NWS daily climate report (CLI) at Central Park
  KNYC (and KMDW/KMIA/KLAX/KSFO) at 8 AM ET the next day. The context carries the right station and
  rule per venue; `hourly_rule_discount_f` accounts for the hourly column reading under the daily max.
- The observation feed follows the rule too: `cli` reads the NWS 5-minute ASOS feed (the CLI max comes
  off the same sensor), `hourly` reads the METAR column via aviationweather.gov. The 5-minute feed prints
  1-2°F above the hourly METAR (KSFO 2026-09-12: 72 vs a 70-71 settlement), which is why the first paper
  day's offshore "dead" and "locked" buckets went the wrong way.
- Polymarket US fees: the July schedule documented taker 0.06·p·(1−p), but every live market
  reports its own **`feeCoefficient: 0.0695`** (checked across all 30 US weather markets,
  2026-09-19). The venue's number is what gets charged, and fees are ~30% of an arb's gross
  edge, so `fees.py` prefers the per-market coefficient and only falls back to the constant.
  Maker rebate 0.0125·p·(1−p) (unchanged).
- **Response shapes that have already cost us.** `GET /v1/event/slug/{slug}` answers `{"event": {...}}`
  and `GET /v1/market/slug/{slug}` answers `{"market": {...}}`, while `search.query` answers them bare;
  reading `markets` off the envelope found nothing, so every weather lookup fell through to a second
  search call — 20 calls a scan against a Cloudflare-fronted host that rate-limits the CWRU campus IP
  (error 1015). And the ask side of `markets.book` is **`offers`**, never `asks`: reading `asks` made
  every US book look one-sided, which is the exact shape that turns `(1 - post)` into phantom edge.
- Polymarket US lists a **high** market per city per day and **no low market**. A 404'd event slug is
  remembered for an hour (`MISSING_EVENT_RETRY_S`) instead of being asked for every tick.
- The *weather modules* scan US 4×/hour (`minute % 15 == 10`) and offshore once at `:55`. The **arb
  sweep is separate and runs on seconds**, not minutes (`_arb_interval_s`): 20s through 09:00–16:59,
  120s at 17:00 and overnight, 600s in the evening — 18:00–23:00 produced 0 opportunities in 281
  observed event-minutes. It screens all ten books (5 cities × today+tomorrow) off ONE batched
  `events.list` call. Measured 2026-09-19: median 21s per book, ~975 book-screens/hour, against ~150
  before. There is no intra-day "peak": normalised by observation the rate of a positive net is flat
  (2.7% over 12:00–15:00 vs 2.8% over 09:00–13:00, 1,450 event-minutes).

## How long an episode actually lasts (corrected 2026-09-19)

Everything in this repo used to say "about a minute", and that was measurement bias, not a fact.
You cannot observe a four-minute episode as four minutes when you look every five:

    09-16..09-18, sampling every ~5 min : 11 episodes, median 1 min, max 2,  0/11 >= 4 min
    09-19,        sampling every ~21 s  :  7 episodes, median 4 min, max 15, 4/7  >= 4 min

So an episode runs a median of FOUR minutes and can run fifteen. The 20s sweep is therefore
comfortable rather than marginal — it samples a typical episode a dozen times. Do not use the old
"one minute" figure to justify anything; it was an artefact of how rarely we looked.

An episode also OPENS marginal and deepens. Miami on 2026-09-19 went 0.85c at 13:53, 2.83c at
13:56, 11.03c at 13:57, peaking at 15.14c at 13:58:30 before easing back. Taking the first
qualifying price is not taking the best one — but the peak is not knowable in advance, and tuning
a threshold against seven episodes is overfitting, not edge. Measured: raising the minimum net to
10c gains $0.12 on the day, and raising it to 12c catches nothing at all.

## bucket_sum (the arb) — state at 2026-09-19, and what is still unknown

The only module whose profit does not require out-forecasting anyone. Buy every leg of an
exhaustive set for under $1, or sell every leg for over $1; exactly one leg pays $1.

**Depth, not price, decides whether this is a business.** Prices show a positive net after fees on
a few percent of observed event-minutes, and almost all of those are worth pennies because the
binding leg holds one or two contracts. The cheap legs are not the problem — measured live, a 1c
leg offered 505,171 contracts and a 3c leg 508 — so the leg that binds is always the *expensive*
one, and that is the number we still do not have enough of.

What is known:
- Best book on record: chicago 2026-09-18 13:54, six legs at ask_sum 0.82, binding leg 21
  contracts. 21 sets × 15.39c ≈ **$3.23**. It was refused live ("depth INCOMPLETE") because one
  leg's book call lost the quota race; that is what `DEPTH_RESERVE` and the cooldown retry fix.
- Bigger books exist and were never depth-read: mdwhigh 2026-09-17 sat at ask_sum **0.52** three
  times between 14:20 and 14:56 (45.9c/set net), flickering on and off within minutes. Today's
  code confirms and sizes it; at 20 contracts that is $9.18, and `arb_max_risk_usd` binds at 88
  sets (~$40).
- Every candidate now stores its **full ladder**, so the next fat book answers the depth question
  from evidence rather than modelling. Do not quote a $/week figure until one is measured.

Next levers, in order of expected value:
1. **Direction-aware screening.** `price_legs` fires whenever a leg is missing *either* side, so we
   routinely buy books for legs whose ask we already have just to learn a bid we only need for the
   unwind estimate. Pricing only the side the plausible direction actually needs would cut most
   screening calls — which matters because those calls compete with the depth read for one shared
   campus-IP quota, and losing that race is what cost the best book on record.
2. **Maker legs.** Taker is 0.0695·p(1−p) and the maker rebate is −0.0125: on the miami book that
   swing is ~4.7c/set against a 13c gross edge. It needs resting orders and a partial-fill policy,
   so it is a different strategy, not a tweak.
3. **Live.** `arb_live_ok` is Alex's switch. The executor is ready (group placement, thinnest leg
   first, execution-based fill detection, unwind of what actually filled) but **no order has ever
   been sent**, so request/response shapes are checked against the SDK's types and nothing else.
   The first live set should be small and watched.
