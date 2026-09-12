# polybot

Alex's multi-strategy Polymarket bot. Design doc: vault `Money/Polymarket Bot — Design v1 (2026-09-12).md`.

## What it is
- Strategy modules, each with a mode `off → paper → signal → live`. The ledger promotes them
  (`report` prints the gate for every module: ≥30 signals, net positive after fees, ≥50% fills).
- One risk manager every order passes through: $20/market, $100 total, halt under $120 bankroll,
  $20 daily loss stop, sports off until flipped (Ohio), maker limit orders only, kill switch =
  `touch polybot/KILL`.
- Two venues. `offshore` (polymarket.com) is read-only: the price reference and the paper proxy.
  `us` (Polymarket US) is the only tradable venue and needs the key.

## Modules
| module | reference the market lags | status today |
|---|---|---|
| weather_hold | Open-Meteo ensemble vs bucket price | paper on offshore books |
| weather_obs | NWS hourly observation kills buckets | paper on offshore books |
| weather_lock | day's max locked after the peak | paper on offshore books |
| weather_model_update | new model run vs last run | paper on offshore books |
| bucket_sum | mutually-exclusive buckets ≠ $1 | paper on offshore books |
| hold_favorites | our own calibration table (run `calibrate`) | paper on offshore books |
| leadlag | offshore price vs US book | idle until key + `pairs.json` |
| maker_rewards | incentive-program quoting | idle until key |

## Run
```bash
cd ~/second-brain/second-brain-chat
python3 -m polybot.runner status
python3 -m polybot.runner scan                 # one pass, records paper signals
python3 -m polybot.runner settle               # fill/close paper signals from what the market did next
python3 -m polybot.runner report --days 7
python3 -m polybot.runner calibrate --events 300
python3 -m polybot.runner loop                 # the schedule, forever
```
Tests: `python3 -m pytest test_polybot.py -q` (no network).

## Going live (later, in order)
1. `pip install polymarket-us`; put `POLYMARKET_KEY_ID` and `POLYMARKET_SECRET_KEY` in the server env.
2. `status` shows `us venue: ready`; bankroll is read from the account.
3. Flip one module to `signal` in `polybot/config.json`; nudges only.
4. Flip to `live` only after the gate passes twice. Orders are limit/GTC; `cancel_all` on the kill switch.

## Venue rules that matter
- Offshore weather markets resolve on the HOURLY "Temp" column of the station in the description
  (NYC = LaGuardia KLGA). Polymarket US settles on the NWS daily climate report (CLI) at Central Park
  KNYC (and KMDW/KMIA/KLAX/KSFO) at 8 AM ET the next day. The context carries the right station and
  rule per venue; `hourly_rule_discount_f` (1°F) accounts for the hourly column reading under the daily max.
- Polymarket US fees (2026-07-01): taker 0.06·p·(1−p), maker rebate 0.0125·p·(1−p).
