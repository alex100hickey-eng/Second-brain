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
| bucket_sum | mutually-exclusive buckets ≠ $1 | paper on offshore books |
| hold_favorites | our own calibration table (run `calibrate`) | paper on offshore books |
| leadlag | offshore price vs US book | idle until `pairs.json` |
| maker_rewards | incentive-program quoting | idle |

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
python3 -m polybot.runner promote              # flip every gate-passing paper module to live (writes config.json)
python3 -m polybot.runner calibrate --events 300
python3 -m polybot.runner backtest --days 7    # replay the weather modules on real past days (also Sundays 04:00)
python3 -m polybot.runner pairs                # match US markets to offshore twins for leadlag (needs the key)
python3 -m polybot.runner loop                 # the schedule, forever
```
Tests: `python3 -m pytest test_polybot.py -q` (no network).

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
- Polymarket US fees (2026-07-01): taker 0.06·p·(1−p), maker rebate 0.0125·p·(1−p).
- **Response shapes that have already cost us.** `GET /v1/event/slug/{slug}` answers `{"event": {...}}`
  and `GET /v1/market/slug/{slug}` answers `{"market": {...}}`, while `search.query` answers them bare;
  reading `markets` off the envelope found nothing, so every weather lookup fell through to a second
  search call — 20 calls a scan against a Cloudflare-fronted host that rate-limits the CWRU campus IP
  (error 1015). And the ask side of `markets.book` is **`offers`**, never `asks`: reading `asks` made
  every US book look one-sided, which is the exact shape that turns `(1 - post)` into phantom edge.
- Polymarket US lists a **high** market per city per day and **no low market**. A 404'd event slug is
  remembered for an hour (`MISSING_EVENT_RETRY_S`) instead of being asked for every tick.
- The US venue is scanned **4×/hour** (`minute % 15 == 10`), offshore once at `:55`. US signals are the
  scarce resource — roughly one a day — and they are the only ones that can ever carry real money.
