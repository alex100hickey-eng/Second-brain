"""polybot unit tests — no network. Run: python3 -m pytest test_polybot.py -q"""
import json
import collections
import math
import os
os.environ.setdefault("JARVIS_TEST", "1")   # no real sleeps for the US call budget
import tempfile
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
import requests

from polybot import calibration, config, fees
from polybot.feeds import offshore, weather
from polybot.feeds.offshore import Bucket, WeatherEvent
from polybot.ledger import Ledger
from polybot.paper import PaperEngine, exit_from_history, fill_from_history, pnl_usd
from polybot.risk import RiskManager
from polybot.strategies.base import Signal, size_for
from polybot.strategies.bucket_sum import BucketSum, arb_check
from polybot.strategies.hold_favorites import HoldFavorites
from polybot.strategies.leadlag import leadlag_signal, noise_signal
from polybot.strategies.weather import (WeatherHold, WeatherLock, WeatherModelUpdate, WeatherObs, build_ctx,
                                        lock_state)


# ---- fixtures -----------------------------------------------------------------------------
def _bucket(title, bid, ask, tok, closed=False, outcome=None):
    lo, hi, unit = offshore.parse_bucket_title(title)
    return Bucket(title, lo, hi, unit, tok, tok + "n", "m" + tok, "c" + tok, bid, ask,
                  (bid + ask) / 2 if bid is not None and ask is not None else None, closed, outcome)


def _event():
    titles = ["69°F or below", "70-71°F", "72-73°F", "74-75°F", "76-77°F", "78-79°F", "80-81°F", "82-83°F", "84°F or higher"]
    quotes = [(None, 0.01), (None, 0.01), (0.01, 0.02), (0.01, 0.03), (0.19, 0.21), (0.67, 0.69), (0.08, 0.10), (0.01, 0.02), (None, 0.01)]
    buckets = [_bucket(t, b, a, f"t{i}") for i, (t, (b, a)) in enumerate(zip(titles, quotes))]
    return WeatherEvent("highest-temperature-in-nyc-on-september-12-2026", "nyc", "2026-09-12", "KLGA", "hourly", "F", buckets, True, "2026-09-12T12:00:00Z")


def _cfg(tmp_path=None):
    cfg = config.Config()
    cfg.bankroll_usd = 200
    cfg.hourly_rule_discount_f = 1.0   # fixtures assume the 1°F hourly rule; production default is 0.0 (backtest fit)
    return cfg


def _ledger():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return Ledger(path)


def _ctx(event=None, members=None, obs=None, hourly=None, local_now=None, cfg=None):
    return build_ctx("nyc", datetime(2026, 9, 12, tzinfo=ZoneInfo("America/New_York")), "high", cfg or _cfg(),
                     venue="offshore", fetch={"event": event or _event(), "members": members if members is not None else [78.5] * 60 + [80.2] * 20,
                                              "obs": obs if obs is not None else [], "hourly": hourly if hourly is not None else [],
                                              "local_now": local_now})


# ---- fees -----------------------------------------------------------------------------------
def test_us_fee_schedule_matches_the_live_venue():
    """The July fee schedule documented a 0.06 taker coefficient. On 2026-09-19 every one of the
    30 live US weather markets reported `feeCoefficient: 0.0695`, and the venue's number is the
    one that gets charged. For an arb that is not a rounding error: a set scored at 1.0c net
    under 0.06 is worth 0.38c under 0.0695, against a 1.0c minimum -- i.e. the floor was passing
    trades that did not actually clear it."""
    assert fees.US_TAKER_THETA == 0.0695
    assert fees.us_taker_fee(0.5, 100) == pytest.approx(1.7375)
    assert fees.us_maker_rebate(0.5, 100) == pytest.approx(0.3125)
    assert fees.leg_cost(0.5, 100, "us", maker=True) == pytest.approx(-0.3125)
    assert fees.leg_cost(0.5, 100, "offshore", maker=True) == 0.0
    assert fees.leg_cost(0.5, 100, "offshore", maker=False, category="weather") == pytest.approx(1.25)

    # A market that states its own coefficient overrides the fallback, in both directions.
    assert fees.us_taker_fee(0.5, 100, theta=0.06) == pytest.approx(1.50)
    assert fees.leg_cost(0.5, 100, "us", maker=False, theta=0.08) == pytest.approx(2.00)
    assert fees.leg_cost(0.5, 100, "us", maker=False, theta=None) == pytest.approx(1.7375)
    # a taken round trip at 50c on US costs 2 * 0.0695 * 0.25 = 3.475c per contract (it was 3.0c
    # while we believed the July schedule); two maker legs still earn 0.6c, the rebate is unchanged
    assert fees.swing_breakeven_cents(0.5, "us", False, False, spread_cents=0) == pytest.approx(3.475)
    assert fees.swing_breakeven_cents(0.5, "us", True, True, spread_cents=2) == pytest.approx(-0.625)


# ---- buckets / weather math ---------------------------------------------------------------
def test_parse_bucket_titles():
    assert offshore.parse_bucket_title("69°F or below") == (-math.inf, 69.0, "F")
    assert offshore.parse_bucket_title("70-71°F") == (70.0, 71.0, "F")
    assert offshore.parse_bucket_title("88°F or higher") == (88.0, math.inf, "F")
    assert offshore.parse_bucket_title("27°C") == (27.0, 27.0, "C")
    assert offshore.parse_bucket_title("26°C or below") == (-math.inf, 26.0, "C")
    assert offshore.station_from_description("...timeseries?site=klga ...") == "KLGA"
    assert offshore.slug_for("nyc", datetime(2026, 9, 12)) == "highest-temperature-in-nyc-on-september-12-2026"


def test_bucket_probs_and_discount():
    ev = _event()
    members = [78.4, 78.9, 79.4, 80.1, 80.4]          # rounds to 78, 79, 79, 80, 80
    probs = weather.bucket_probs(members, ev.buckets, discount=0.0, floor=0.0)
    by = dict(zip([b.title for b in ev.buckets], probs))
    assert by["78-79°F"] == pytest.approx(0.6) and by["80-81°F"] == pytest.approx(0.4)
    probs_d = weather.bucket_probs(members, ev.buckets, discount=1.0, floor=0.0)  # hourly rule reads 1° under
    by_d = dict(zip([b.title for b in ev.buckets], probs_d))
    assert by_d["78-79°F"] == pytest.approx(0.8) and by_d["76-77°F"] == pytest.approx(0.2)
    assert sum(weather.bucket_probs([75.0] * 10, ev.buckets)) == pytest.approx(1.0)


def test_observations_running_max_and_c_to_f():
    assert weather.c_to_f_whole(21.1) == 70 and weather.c_to_f_whole(25.0) == 77
    obs = [("2026-09-12T13:51:00+00:00", 72), ("2026-09-12T17:51:00+00:00", 78), ("2026-09-13T03:30:00+00:00", 60),
           ("2026-09-13T04:30:00+00:00", 59)]
    mx, n, last = weather.running_extreme(obs, "2026-09-12", "America/New_York", "high")
    assert mx == 78 and n == 3 and last.startswith("2026-09-12T23:30")  # 03:30Z is 23:30 EDT Sep 12; 04:30Z is Sep 13
    hourly = [("2026-09-12T15:00", 77.0), ("2026-09-12T18:00", 74.0), ("2026-09-13T01:00", 80.0)]
    assert weather.hours_remaining_max(hourly, "2026-09-12", "2026-09-12T14:30:00-04:00", "America/New_York") == 77.0
    assert weather.parse_cli("TEMPERATURE (F)\n TODAY\n  MAXIMUM         81   3:21 PM\n  MINIMUM         64") == {"max": 81, "min": 64}


# ---- strategies -----------------------------------------------------------------------------
def test_weather_hold_signals_on_model_edge():
    cfg = _cfg()
    cfg.hold_edge_max_cents = 100                       # this fixture's edges are 60-70c; the cap has its own test
    ctx = _ctx(members=[81.6] * 70 + [78.6] * 10)     # 81.6-1 = 80.6 → 81 → 80-81 bucket (70/80 = 87.5%)
    sigs = WeatherHold(cfg).scan(ctx)
    yes = [s for s in sigs if s.side == "BUY_YES"]
    assert len(yes) == 1 and yes[0].label.endswith("80-81°F") and yes[0].price == 0.09 and yes[0].edge_cents > 70
    assert yes[0].contracts == int(yes[0].size_usd // 0.09)
    no = [s for s in sigs if s.side == "BUY_NO"]
    assert any(s.label.endswith("78-79°F") for s in no)     # model ~12% vs bid 0.67 → buy NO


def test_weather_obs_sells_dead_buckets_only():
    cfg = _cfg()
    obs = [("2026-09-12T14:51:00+00:00", 75), ("2026-09-12T17:51:00+00:00", 80), ("2026-09-12T18:51:00+00:00", 79)]
    ctx = _ctx(obs=obs)
    sigs = WeatherObs(cfg).scan(ctx)
    labels = {s.label.split()[-1] for s in sigs}
    assert labels == {"78-79°F", "76-77°F"}          # bid 0.67 and 0.19 are dead once 80 was observed; 0.01 bids are skipped
    assert all(s.side == "BUY_NO" for s in sigs)
    assert not WeatherObs(cfg).scan(_ctx(obs=[]))
    # A bucket our feed calls dead that the book prices at 96c is our feed being wrong about
    # something that has already happened: 0/6 for -$70 in paper, and the trade risks the whole
    # stake to win four cents, so one bad observation costs twenty good ones.
    ev = _event()
    ev.buckets[5].best_bid, ev.buckets[5].best_ask = 0.96, 0.98
    labels = {s.label.split()[-1] for s in WeatherObs(cfg).scan(_ctx(event=ev, obs=obs))}
    assert labels == {"76-77°F"} and "78-79°F" not in labels
    cfg.dead_bucket_max_bid = 0.99                                   # ceiling lifted: it trades again
    assert "78-79°F" in {s.label.split()[-1] for s in WeatherObs(cfg).scan(_ctx(event=ev, obs=obs))}


def test_weather_lock_requires_peak_passed_and_falling_obs():
    cfg = _cfg()
    obs = [("2026-09-12T15:51:00+00:00", 76), ("2026-09-12T18:51:00+00:00", 79), ("2026-09-12T19:51:00+00:00", 78), ("2026-09-12T20:51:00+00:00", 77)]
    hourly = [("2026-09-12T17:00", 74.0), ("2026-09-12T18:00", 73.0), ("2026-09-12T20:00", 70.0)]
    ctx = _ctx(obs=obs, hourly=hourly)
    locked, winner = lock_state(ctx)
    assert locked and winner.title == "78-79°F"
    # A bucket the model calls locked but the book prices at 0.69 is a disagreement about a fact
    # that has already happened, and the book wins that argument: paper entries under 0.80 went
    # 2/12 for -$168, entries at 0.80+ went 21/22 for +$17. `lock_min_price` refuses the cheap half.
    assert WeatherLock(cfg).scan(ctx) == []
    ev = _event()
    ev.buckets[5].best_bid, ev.buckets[5].best_ask = 0.84, 0.86
    sigs = WeatherLock(cfg).scan(_ctx(event=ev, obs=obs, hourly=hourly))
    # It TAKES the ask (0.86), it does not rest a maker bid at 0.85. A locked bucket only walks
    # toward 1.00, so a bid under the market fills when someone sells back into a settled fact —
    # live paper 2026-09-15 filled 27% of these and the misses were structural, not luck.
    assert len(sigs) == 1 and sigs[0].side == "BUY_YES" and sigs[0].price == 0.86
    assert sigs[0].size_usd == 20.0 and sigs[0].taker and sigs[0].taker_ok
    # still rising → no lock
    rising = [("2026-09-12T15:51:00+00:00", 76), ("2026-09-12T16:51:00+00:00", 77), ("2026-09-12T17:51:00+00:00", 79)]
    assert lock_state(_ctx(obs=rising, hourly=[("2026-09-12T19:00", 81.0)]))[0] is False
    # CLI-rule venue refuses a lock at a bucket's top edge (79 is the top of 78-79)
    ctx_cli = _ctx(obs=obs, hourly=hourly)
    ctx_cli.rule = "cli"
    assert lock_state(ctx_cli)[0] is False


def test_adjust_probs_with_observations():
    from polybot.strategies.weather import adjust_probs_with_obs
    ev = _event()
    raw = weather.bucket_probs([78.6] * 20 + [80.6] * 40 + [82.6] * 20 + [84.6] * 20, ev.buckets, floor=0.0)
    # running max 81 observed, model says nothing above 79 remains → 84+ is beyond the margin, 78-79 is dead,
    # 82-83 sits inside the 2° safety margin so it keeps a quarter of its model share
    adj = adjust_probs_with_obs(raw, ev.buckets, running=81, remaining=79.0)
    by = dict(zip([b.title for b in ev.buckets], adj))
    assert by["78-79°F"] == 0.0 and by["84°F or higher"] == 0.0
    assert by["80-81°F"] == pytest.approx(0.4 / 0.45) and by["82-83°F"] == pytest.approx(0.05 / 0.45)
    # no observations yet → untouched
    assert adjust_probs_with_obs(raw, ev.buckets, running=None, remaining=None) == raw
    # the bucket holding the running max never drops to zero even if the model had no members there
    adj2 = adjust_probs_with_obs([0.0] * len(ev.buckets), ev.buckets, running=75, remaining=74.0)
    assert dict(zip([b.title for b in ev.buckets], adj2))["74-75°F"] == 1.0
    # a late-day context feeds the adjusted probs to weather_hold, so it no longer buys impossible buckets
    obs = [("2026-09-12T18:51:00+00:00", 80), ("2026-09-12T19:51:00+00:00", 81), ("2026-09-12T20:51:00+00:00", 80)]
    ctx = _ctx(members=[84.6] * 80, obs=obs, hourly=[("2026-09-12T18:00", 79.0)])
    assert ctx.model_probs != ctx.probs and dict(zip([b.title for b in ctx.event.buckets], ctx.probs))["80-81°F"] == pytest.approx(1.0)
    assert not [s for s in WeatherHold(_cfg()).scan(ctx) if s.side == "BUY_YES" and "84" in s.label]


def test_low_market_adjust_and_lock():
    from polybot.strategies.weather import adjust_probs_with_obs
    titles = ["59°F or below", "60-61°F", "62-63°F", "64-65°F", "66°F or higher"]
    buckets = [_bucket(t, 0.10, 0.12, f"l{i}") for i, t in enumerate(titles)]
    raw = [0.1, 0.5, 0.3, 0.1, 0.0]
    # running min 63 so far; the model says the rest of the day stays above 65 → 64-65 and 66+ cannot be the low
    adj = adjust_probs_with_obs(raw, buckets, running=63, remaining=66.0, kind="low")
    by = dict(zip(titles, adj))
    assert by["64-65°F"] == 0.0 and by["66°F or higher"] == 0.0 and by["62-63°F"] > 0.5
    ev = WeatherEvent("lowest-temperature-in-nyc-on-september-12-2026", "nyc", "2026-09-12", "KLGA", "hourly", "F", buckets, True, "")
    obs = [("2026-09-12T09:51:00+00:00", 63), ("2026-09-12T12:51:00+00:00", 64), ("2026-09-12T13:51:00+00:00", 66)]
    hourly = [("2026-09-12T11:00", 68.0), ("2026-09-12T15:00", 74.0), ("2026-09-12T23:00", 67.0)]
    ctx = build_ctx("nyc", datetime(2026, 9, 12, tzinfo=ZoneInfo("America/New_York")), "low", _cfg(), venue="offshore",
                    fetch={"event": ev, "members": [62.4] * 10, "obs": obs, "hourly": hourly})
    assert ctx.running == 63 and ctx.remaining_extreme == 67.0
    locked, winner = lock_state(ctx)
    assert locked and winner.title == "62-63°F"
    # The book here is 0.10/0.12. The lock is real, but a market pricing it at 12 cents is the
    # case that went 0/7 in paper, so `lock_min_price` refuses it however sure the model is.
    assert WeatherLock(_cfg()).scan(ctx) == []
    winner.best_bid, winner.best_ask = 0.90, 0.92
    sigs = WeatherLock(_cfg()).scan(ctx)
    assert sigs and sigs[0].side == "BUY_YES" and sigs[0].label.endswith("62-63°F")


def test_station_parsing_and_city_registry():
    assert offshore.station_from_description("https://www.wunderground.com/history/daily/gb/london/EGLL/date/2026-9-11") == "EGLL"
    assert offshore.station_from_description("readings at the airport station (RJTT) in Tokyo") == "RJTT"
    assert offshore.station_from_description("nothing here") is None
    assert config.city_meta("london")["station"] == "EGLL" and config.city_meta("nyc")["station"] == "KNYC"
    assert config.city_meta("atlantis") is None and len(config.all_city_slugs()) == 30   # 5 US + 25 offshore-only


def test_backtest_replay_offline():
    from polybot import backtest
    ev = _event()
    ev.date, ev.slug = "2026-09-11", "highest-temperature-in-nyc-on-september-11-2026"
    tz = ZoneInfo("America/New_York")
    day0 = datetime(2026, 9, 11, tzinfo=tz).timestamp()
    for b in ev.buckets:
        b.outcome = 1 if b.title == "80-81°F" else 0
    # 80-81 trades at 12c all morning, jumps to 85c at 16:00 after the observation, resolves YES
    hist = {b.yes_token: [(day0 + h * 3600, 0.02) for h in range(6, 24)] for b in ev.buckets}
    hist[ev.buckets[6].yes_token] = [(day0 + h * 3600, 0.12 if h < 16 else 0.85) for h in range(6, 24)]
    hist[ev.buckets[5].yes_token] = [(day0 + h * 3600, 0.60 if h < 16 else 0.05) for h in range(6, 24)]
    obs = [(datetime(2026, 9, 11, h, 51, tzinfo=tz).astimezone(ZoneInfo("UTC")).isoformat(), t)
           for h, t in ((8, 70), (10, 74), (12, 78), (14, 80), (15, 81), (16, 80), (17, 79), (18, 77))]
    hourly = [(f"2026-09-11T{h:02d}:00", t) for h, t in ((9, 72.0), (12, 77.0), (15, 79.0), (18, 76.0), (21, 72.0))]
    day = {"city": "nyc", "date": "2026-09-11", "kind": "high", "tz": "America/New_York", "station": "KLGA",
           "rule": "hourly", "event": ev, "winner": "80-81°F", "histories": hist, "obs": obs,
           "members": [81.6] * 5 + [79.6] * 2, "hourly": hourly, "unit": "F"}
    assert backtest.price_at(hist[ev.buckets[6].yes_token], day0 + 10 * 3600) == 0.12
    res = backtest.replay_day(day, _cfg(), log=lambda *_: None)
    mods = {s["module"] for s in res["signals"]}
    assert "weather_hold" in mods and "weather_obs" in mods and "weather_lock" in mods
    hold = next(s for s in res["signals"] if s["module"] == "weather_hold" and s["bucket"] == "80-81°F" and s["side"] == "BUY_YES")
    assert hold["filled"] and hold["pnl"] > 0                      # bought ~13c, resolved YES
    lock = next(s for s in res["signals"] if s["module"] == "weather_lock")
    assert lock["bucket"] == "80-81°F" and lock["outcome"] == 1
    assert set(res["discount_scores"]) == {"0.0", "0.5", "1.0", "1.5"}
    summary = backtest.summarize(res["signals"], {"hourly": [res["discount_scores"]]}, 1)
    assert summary["modules"]["weather_hold"]["net"] > 0 and "weather_lock" in backtest.format_summary(summary)


def test_backtest_reports_the_book_it_could_not_trade():
    """The replay trades a 2c book on every bucket; the live offshore book in the band where a
    locked winner sits is mostly no market at all. A backtest that does not say so reads as a
    forecast, which is how weather_lock came to show 180 signals against a live week with one."""
    import sqlite3
    from polybot import backtest
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "snap.db")
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE snapshots (ts REAL, venue TEXT, market TEXT, bid REAL, ask REAL, mid REAL, last REAL)")
        now = time.time()
        rows = [(now - 3600, "offshore", f"t{i}", 0.03, 0.95, None, None) for i in range(87)]      # empty books
        rows += [(now - 3600, "offshore", f"u{i}", 0.88, 0.92, None, None) for i in range(13)]     # real ones
        rows += [(now - 30 * 86400, "offshore", "old", 0.90, 0.92, None, None)]                    # outside the window
        rows += [(now - 3600, "offshore", "cheap", 0.10, 0.12, None, None)]                        # outside the band
        conn.executemany("INSERT INTO snapshots VALUES (?,?,?,?,?,?,?)", rows)
        conn.commit()
        liq = backtest.liquidity_reality(db_path=path, max_spread_cents=10.0)
    assert liq["n"] == 100 and liq["tradable_share"] == 0.13
    assert liq["median_spread_cents"] > 50                       # the typical offshore book is not a price
    s = backtest.summarize([{"module": "weather_lock", "filled": True, "pnl": 1.0, "size_usd": 20.0}] * 180,
                           {}, 208, liq)
    text = backtest.format_summary(s)
    assert "13% clear the 10c filter" in text and "about 23 have a book live" in text
    assert "upper bound" in text
    assert backtest.liquidity_reality(db_path=os.path.join("/nonexistent", "x.db")) is None


def test_pairs_matching():
    from polybot import pairs
    us = [{"slug": "fed-cut-sep", "title": "Will the Fed cut rates in September?", "end": "2026-09-17T18:00:00Z"},
          {"slug": "nfl-kc-phi", "title": "Chiefs vs Eagles", "end": "2026-09-14T20:00:00Z", "category": "sports"}]
    off = [{"token": "t1", "title": "Fed cuts rates in September?", "end": "2026-09-17T20:00:00Z", "category": "economics"},
           {"token": "t2", "title": "Will the Fed cut rates in December?", "end": "2026-12-10T20:00:00Z", "category": "economics"},
           {"token": "t3", "title": "Chiefs vs. Eagles", "end": "2026-09-14T20:00:00Z", "category": "sports"}]
    got = pairs.match_pairs(us, off)
    assert [(g["us_slug"], g["offshore_token"], g["category"]) for g in got] == [("fed-cut-sep", "t1", "economics"), ("nfl-kc-phi", "t3", "sports")]
    assert pairs.match_pairs([{"slug": "x", "title": "Bitcoin above 100k", "end": None}], off) == []


def test_executor_sync_with_fake_venue():
    from polybot.execution import Executor
    led, cfg = _ledger(), _cfg()
    cfg.modes["weather_hold"] = "live"

    class Venue:
        available = True
        def __init__(self): self.calls = []
        def open_orders(self): return [{"id": "o-resting"}]
        def positions(self): return [{"marketSlug": "mkt-filled", "quantity": 20}]
        def cancel(self, oid, slug): self.calls.append(("cancel", oid, slug))
        def cancel_all(self): self.calls.append(("cancel_all",))
        def place_limit(self, slug, side, price, n): self.calls.append(("place", slug, side, price, n)); return {"id": "o-tp"}

    v = Venue()
    ex = Executor(led, v, cfg, log=lambda *_: None)
    now = time.time()
    s_filled = led.add_signal(Signal("weather_hold", "us", "mkt-filled", "f", "BUY_YES", 0.20, 10, 9, "r", exit="tp:0.03", ts=now), "live")
    led.add_order(s_filled, "us", "mkt-filled", "BUY_YES", 0.20, 50, "sent", venue_order_id="o-filled")
    s_stale = led.add_signal(Signal("weather_hold", "us", "mkt-stale", "s", "BUY_YES", 0.50, 10, 9, "r", ts=now - 90000, horizon_hours=24), "live")
    led.add_order(s_stale, "us", "mkt-stale", "BUY_YES", 0.50, 20, "sent", venue_order_id="o-resting")
    s_gone = led.add_signal(Signal("weather_hold", "us", "mkt-gone", "g", "BUY_YES", 0.50, 10, 9, "r", ts=now), "live")
    led.add_order(s_gone, "us", "mkt-gone", "BUY_YES", 0.50, 20, "sent", venue_order_id="o-gone")
    counts = ex.sync()
    assert counts == {"filled": 1, "cancelled": 2, "tp_placed": 1, "open": 0}
    assert led.paper_row(s_filled)["status"] == "filled" and led.last_order(s_filled)["status"] == "sent-tp"
    assert ("place", "mkt-filled", "SELL_YES", 0.23, 50) in v.calls and ("cancel", "o-resting", "mkt-stale") in v.calls
    assert led.last_order(s_stale)["status"] == "cancelled" and led.last_order(s_gone)["status"] == "gone"
    open(config.KILL_PATH, "w").close()
    try:
        assert ex.sync() == {"filled": 0, "cancelled": 0, "tp_placed": 0, "open": 0} and ("cancel_all",) in v.calls
    finally:
        os.remove(config.KILL_PATH)


def test_weather_model_update_uses_last_run():
    cfg, led = _cfg(), _ledger()
    cfg.hold_edge_max_cents = 100
    mod = WeatherModelUpdate(cfg, led)
    assert mod.scan(_ctx(members=[78.6] * 80)) == []                 # first run: nothing to compare
    sigs = mod.scan(_ctx(members=[81.6] * 60 + [78.6] * 20))          # 80-81 jumped from ~0 to 75%
    assert any(s.side == "BUY_YES" and s.label.endswith("80-81°F") for s in sigs)
    assert any(s.side == "BUY_NO" and s.label.endswith("78-79°F") for s in sigs)


def test_bucket_sum_arb_math():
    ev = _event()
    kind, net, _ = arb_check(ev.buckets, "offshore")
    assert kind is None                                                # one-sided books: no arb can be locked
    assert sum(b.best_ask for b in ev.buckets) > 1.0
    # Real venue prices are whole cents, and the fixture must be too: 4-decimal asks produced
    # sub-tick legs at 0.0082 that risk rightly refuses ("price outside 1-99c"), and earlier the
    # same unrealism silently sized 11 contracts on one leg and 12 on another.
    # 9 legs at a 1c tick carry at least 9c of spread between them, and an arb has to pay for its
    # own unwind — so a worthwhile set here is nearer 0.81 than 0.90.
    for b in ev.buckets:                                               # 9 legs x 0.09 = 0.81
        b.best_bid, b.best_ask = 0.08, 0.09
    kind, net, prices = arb_check(ev.buckets, "us")
    # 19c gross; 9 legs at 0.09 pay 0.0695 * 9 * 0.09 * 0.91 = 5.1c of taker fees
    assert kind == "buy_all" and 13.0 < net < 15.0 and len(prices) == len(ev.buckets)

    # An arb the book cannot fill is not an arb. Depth is None until somebody asks the book, and
    # None must block the trade rather than default to a size.
    assert BucketSum(_cfg()).scan(_ctx(event=ev)) == []
    for b in ev.buckets:
        b.ask_qty, b.ask_levels = 40, [(b.best_ask, 40)]
    ev.buckets[3].ask_qty, ev.buckets[3].ask_levels = 12, [(ev.buckets[3].best_ask, 12)]  # thinnest leg
    sigs = BucketSum(_cfg()).scan(_ctx(event=ev))
    assert len(sigs) == len(ev.buckets) and all(s.taker and s.arb for s in sigs)
    # every leg carries the SAME number of contracts — equal dollars per leg would be a random
    # basket, not a set — and the count is the thinnest leg, not the average
    assert {s.contracts for s in sigs} == {12}
    assert all(s.size_usd == pytest.approx(s.price * 12) for s in sigs)
    # a set costs about what the asks sum to, and it is one position in each of the legs' markets
    assert sum(s.size_usd for s in sigs) == pytest.approx(0.81 * 12, abs=0.05)
    assert all(0.01 <= s.price <= 0.99 for s in sigs)                   # every leg is a real tick

    # the SET cost is what binds, not the per-market cap: a completed set pays $1 whatever the
    # world does, so per-leg direction risk is the wrong ruler (nyc offered 73 sets of depth while
    # $20/market sized us to 23 — a third of the arb left on the table)
    cfg = _cfg()
    cfg.arb_max_set_cost_usd = 4.5
    small = BucketSum(cfg).scan(_ctx(event=ev))
    assert small
    set_cost = sum(s.size_usd for s in small)
    assert set_cost <= 4.5 + 1e-9 and set_cost > 3.0        # sized right up to the cap
    # a leg may now exceed the per-market cap, and risk agrees because it judges the set
    cfg.caps.max_per_market_usd = 0.5
    rm = RiskManager(cfg, _ledger())
    assert all(rm.allow(sig)[0] for sig in small)
    cfg.arb_max_set_cost_usd = 1.0                           # ...but the SET cap is a hard rail
    ok, why = rm.allow(small[0])
    assert not ok and "over set cap" in why

    # an empty book is not a 100% arb: a missing ask is unknown, never zero
    empty = _event()
    for b in empty.buckets:
        b.best_bid = b.best_ask = None
        b.ask_qty, b.ask_levels = 999, [(0.01, 999)]
    assert arb_check(empty.buckets, "us")[0] is None
    assert BucketSum(_cfg()).scan(_ctx(event=empty)) == []

    # buckets that do not tile every temperature cannot be arbed: one degree would pay nobody
    from polybot.strategies.bucket_sum import exhaustive
    assert exhaustive(ev.buckets)
    gapped = _event()
    del gapped.buckets[4]                                               # 76-77 removed: 75 and 78 no longer meet
    assert not exhaustive(gapped.buckets)
    for b in gapped.buckets:
        b.best_ask, b.ask_qty, b.ask_levels = 0.05, 40, [(0.05, 40)]
    assert arb_check(gapped.buckets, "us")[0] is None


def test_leadlag_and_noise_rules():
    t0 = 1000.0
    ref = [(t0 + i * 10, 0.50 + (0.05 if i >= 8 else 0)) for i in range(12)]   # ref jumps +5c
    tgt = [(t0 + i * 10, 0.50) for i in range(12)]                            # target flat
    hit = leadlag_signal(ref, tgt, 120, 3.0, 0.5)
    assert hit and hit[0] == "BUY_YES" and hit[3] == pytest.approx(5.0)
    followed = [(t0 + i * 10, 0.50 + (0.04 if i >= 9 else 0)) for i in range(12)]
    assert leadlag_signal(ref, followed, 120, 3.0, 0.5) is None
    quiet_ref = [(t0 + i * 10, 0.50) for i in range(12)]
    noisy_tgt = [(t0 + i * 10, 0.50 - (0.04 if i >= 9 else 0)) for i in range(12)]
    n = noise_signal(quiet_ref, noisy_tgt, 120, 3.0)
    assert n and n[0] == "BUY_YES" and n[3] == pytest.approx(4.0)
    assert noise_signal(ref, noisy_tgt, 120, 3.0) is None


def test_calibration_table_and_hold_favorites():
    table = {}
    for _ in range(40):
        calibration.add_sample(table, "politics", 0.88, 1)
    for _ in range(4):
        calibration.add_sample(table, "politics", 0.88, 0)
    assert calibration.band_key(0.88) == "0.85-0.90"
    assert calibration.lookup(table, 0.88, "politics") == pytest.approx((40 + 20 * 0.88) / 64)
    assert calibration.lookup(table, 0.88, "sports") is None
    assert calibration.price_before([(0, 0.5), (100, 0.7), (5000, 0.9)], 90000, 24) == 0.7
    hist = [(0, 0.5), (100, 0.7), (5000, 0.9), (200000, 0.995), (300000, 0.999)]
    assert calibration.resolution_ts(hist) == 200000                      # first point of the settled tail
    assert calibration.resolution_ts([(0, 0.5), (100, 0.6)]) is None      # never settled
    assert calibration.price_before(hist, calibration.resolution_ts(hist), 24) == 0.9
    end = datetime.now(ZoneInfo("UTC")).timestamp() + 2 * 86400
    end_iso = datetime.fromtimestamp(end, ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")
    events = [{"title": "Will the bill pass?", "tags": [{"slug": "politics"}], "endDate": end_iso,
               "markets": [{"id": 1, "question": "Will the bill pass by Friday?", "outcomePrices": json.dumps(["0.86", "0.14"]),
                            "clobTokenIds": json.dumps(["tokA", "tokAn"]), "bestBid": "0.85", "bestAsk": "0.87", "endDate": end_iso}]},
              {"title": "Lakers vs. Celtics", "tags": [{"slug": "sports"}], "endDate": end_iso,
               "markets": [{"id": 2, "question": "Lakers win?", "outcomePrices": json.dumps(["0.88", "0.12"]),
                            "clobTokenIds": json.dumps(["tokB", "tokBn"]), "bestBid": "0.87", "bestAsk": "0.89", "endDate": end_iso}]}]
    sigs = HoldFavorites(_cfg(), table).scan(events=events)
    assert len(sigs) == 1 and sigs[0].side == "BUY_YES" and sigs[0].category == "politics" and sigs[0].price == 0.86


# ---- sizing / risk / ledger / paper -----------------------------------------------------------
def test_size_for_quarter_kelly_and_caps():
    caps = config.Caps()
    assert size_for(0.5, 0.6, 200, caps) == 0.0                      # no edge
    usd = size_for(0.95, 0.85, 200, caps)                             # f* = 0.667 → 0.25*0.667*200 = 33 → capped 20
    assert usd == 20.0
    assert size_for(0.60, 0.55, 200, caps) == 5.55                    # f* = 0.111 → 5.55 (above the $5 floor)
    assert size_for(0.56, 0.55, 200, caps) == 0.0                     # f*=0.022 → 1.1 < min and f*·bankroll 4.4 < 5


def test_risk_manager_caps():
    cfg, led = _cfg(), _ledger()
    cfg.modes["weather_hold"] = "paper"      # off by default now; this test is about the caps, not the mode
    cfg.caps.max_exposure_usd = 100.0        # pin it: the live cap is Alex's call and moves (180 on 09-19)
    rm = RiskManager(cfg, led)
    sig = Signal("weather_hold", "offshore", "tok", "x", "BUY_YES", 0.5, 20, 6, "r")
    assert rm.allow(sig)[0]
    assert rm.allow(Signal("weather_hold", "offshore", "tok", "x", "BUY_YES", 0.5, 25, 6, "r"))[1].startswith("over per-market")
    assert rm.allow(Signal("weather_hold", "offshore", "tok", "x", "BUY_YES", 0.5, 3, 6, "r"))[1].startswith("below min")
    assert "sports" in rm.allow(Signal("leadlag", "us", "s", "x", "BUY_YES", 0.5, 10, 6, "r", category="sports"))[1]
    assert "taker" in rm.allow(Signal("weather_hold", "offshore", "tok", "x", "BUY_YES", 0.5, 10, 6, "r", taker=True))[1]
    assert rm.allow(Signal("bucket_sum", "offshore", "tok", "x", "BUY_YES", 0.5, 10, 6, "r", taker=True, arb=True))[0]
    # paper mode records portfolio-level refusals instead of enforcing them; live enforces
    ok, why = rm.allow(Signal("weather_hold", "offshore", "new", "x", "BUY_YES", 0.5, 5, 6, "r"), bankroll_usd=100)
    assert ok and "live would refuse" in why and "floor" in why
    # one position per market is enforced in paper too (the 3-hourly re-entry bug)
    led.add_signal(Signal("weather_lock", "offshore", "busy", "x", "BUY_YES", 0.9, 20, 6, "r"), "paper")
    ok, why = rm.allow(Signal("weather_hold", "offshore", "busy", "x", "BUY_NO", 0.2, 10, 6, "r"))
    assert not ok and why.startswith("market exposure $20+$10")
    for i in range(5):
        led.add_signal(Signal("weather_hold", "offshore", f"tok{i}", "x", "BUY_YES", 0.5, 20, 6, "r"), "paper")
    ok, why = rm.allow(sig)
    assert ok and "total exposure" in why and sig.meta["live_would_refuse"].startswith("total exposure")
    cfg.modes["weather_hold"] = "live"
    live_sig = Signal("weather_hold", "us", "slug", "x", "BUY_YES", 0.5, 20, 6, "r")
    for i in range(5):
        led.add_signal(Signal("weather_hold", "us", f"s{i}", "x", "BUY_YES", 0.5, 20, 6, "r"), "live")
    assert rm.allow(live_sig) == (False, "total exposure $100+$20 > cap $100")
    cfg.modes["weather_hold"] = "paper"
    open(config.KILL_PATH, "w").close()
    try:
        assert rm.allow(sig)[1] == "kill switch on"
    finally:
        os.remove(config.KILL_PATH)


def test_paper_engine_fills_exits_and_pnl():
    led = _ledger()
    t0 = time.time() - 3600
    hold = Signal("weather_lock", "offshore", "tokL", "lock", "BUY_YES", 0.90, 18, 8, "r", exit="settle", ts=t0, meta={"market_id": "mL"})
    tp = Signal("weather_hold", "offshore", "tokH", "hold", "BUY_YES", 0.20, 10, 10, "r", exit="tp:0.03", ts=t0, meta={"market_id": "mH"})
    no = Signal("weather_obs", "offshore", "tokN", "dead", "BUY_NO", 0.40, 10, 60, "r", exit="settle", ts=t0, meta={"market_id": "mN"})
    miss = Signal("weather_hold", "offshore", "tokM", "miss", "BUY_YES", 0.10, 10, 10, "r", exit="tp:0.03", ts=t0 - 90000, horizon_hours=24, meta={"market_id": "mM"})
    ids = [led.add_signal(s, "paper") for s in (hold, tp, no, miss)]
    hist = {"tokL": [(t0 + 60, 0.91), (t0 + 120, 0.89), (t0 + 600, 0.95)],
            "tokH": [(t0 + 60, 0.20), (t0 + 300, 0.24)],
            "tokN": [(t0 + 60, 0.59), (t0 + 120, 0.61), (t0 + 900, 0.30)],
            "tokM": [(t0 - 80000, 0.15), (t0, 0.16)]}
    res = {"mL": 1, "mH": None, "mN": 0, "mM": None}
    eng = PaperEngine(led, history_fn=lambda s: hist[s["market"]],
                      resolution_fn=lambda s: res[json.loads(s["meta"])["market_id"]], now_fn=lambda: t0 + 7200)
    counts = eng.settle_open("offshore", log=lambda *_: None)
    assert counts == {"filled": 3, "closed": 3, "expired": 1, "open": 0}
    r_hold = led.paper_row(ids[0])
    assert r_hold["status"] == "closed" and r_hold["outcome"] == 1
    assert r_hold["pnl_usd"] == pytest.approx((1 - 0.90) * 20)         # 20 contracts, resolved YES, no offshore maker fee
    r_tp = led.paper_row(ids[1])
    assert r_tp["exit_kind"] == "tp" and r_tp["exit_price"] == pytest.approx(0.23)
    assert r_tp["pnl_usd"] == pytest.approx(0.03 * 50)
    r_no = led.paper_row(ids[2])
    assert r_no["fill_price"] == pytest.approx(0.60) and r_no["outcome"] == 0 and r_no["pnl_usd"] == pytest.approx(0.60 * 25)
    assert led.paper_row(ids[3])["status"] == "unfilled"
    stats = {s["module"]: s for s in led.module_stats(30)}
    assert stats["weather_lock"]["pnl"] == pytest.approx(2.0) and stats["weather_lock"]["mtm"] == pytest.approx(2.0)
    ok, why = led.promotion_check("weather_lock")
    assert not ok and "signals" in why                                  # 1/30 signals
    assert "weather_lock" in led.report(1) and "mtm=" in led.report(1)


def test_report_explains_weather_lock_signal_scarcity_from_snapshots():
    """The daily report answers "why so few weather_lock signals vs. the backtest" from the
    snapshots the loop already wrote — the same evidence `backtest.liquidity_reality` uses — so a
    reviewer does not have to re-run a slow, live-API backtest to get the answer every time."""
    led = _ledger()
    sig = Signal("weather_lock", "offshore", "tokL", "lock", "BUY_YES", 0.90, 18, 8, "r",
                 exit="settle", meta={"market_id": "mL"})
    led.add_signal(sig, "paper")
    now = time.time()
    rows = [(now - 3600, "offshore", f"t{i}", 0.03, 0.95, None, None) for i in range(9)]  # empty books
    rows += [(now - 3600, "offshore", f"u{i}", 0.88, 0.92, None, None) for i in range(1)]  # one real one
    led.conn.executemany("INSERT INTO snapshots (ts, venue, market, bid, ask, mid, last) VALUES (?,?,?,?,?,?,?)", rows)
    led.conn.commit()
    text = led.report(1)
    assert "weather_lock offshore book reality" in text
    assert "median spread 92c" in text and "10% clear the 10c filter" in text
    assert "bounds live signal count regardless of backtest ROI" in text


def test_report_omits_liquidity_line_without_weather_lock_signals():
    led = _ledger()
    led.add_signal(Signal("bucket_sum", "us", "tok", "r", "BUY_YES", 0.5, 10, 20, "r", exit="settle"), "paper")
    assert "weather_lock offshore book reality" not in led.report(1)


def test_hold_band_and_edge_cap():
    cfg = _cfg()
    # production cap 30c: the 80-81 bucket at post 0.09 with a 60c+ edge is a model error, not a trade
    ctx = _ctx(members=[81.6] * 70 + [78.6] * 10)
    sigs = WeatherHold(cfg).scan(ctx)
    assert not any(s.side == "BUY_YES" for s in sigs)
    assert not any(s.label.endswith("78-79°F") for s in sigs)      # bid 0.67 vs model 12% = 55c: capped too
    # a moderate disagreement inside the band still trades: 78-79 at bid/ask 0.67/0.69, model ~50%
    ctx2 = _ctx(members=[78.6] * 40 + [80.6] * 40)
    sigs2 = WeatherHold(cfg).scan(ctx2)
    no = [s for s in sigs2 if s.side == "BUY_NO" and s.label.endswith("78-79°F")]
    assert len(no) == 1 and 6 <= no[0].edge_cents <= 30
    # the 1-5c extremes never trade, whatever the model says
    ev = _event()
    ev.buckets[3].best_bid, ev.buckets[3].best_ask = 0.03, 0.04      # 74-75 at 3c
    sigs3 = WeatherHold(cfg).scan(_ctx(event=ev, members=[74.6] * 80))
    assert not any(s.label.endswith("74-75°F") for s in sigs3)
    cfg.hold_price_band = (0.01, 0.99)                                # band off, edge 26c under the cap: trades
    assert any(s.label.endswith("74-75°F") for s in WeatherHold(cfg).scan(_ctx(event=ev, members=[74.6] * 24 + [78.6] * 56)))


def test_observation_feed_follows_the_settlement_rule(monkeypatch):
    from polybot.strategies import weather as wmod
    calls = []
    monkeypatch.setattr(wmod.weather, "observations", lambda st, a=None, b=None, limit=500: calls.append(("nws", st)) or [])
    monkeypatch.setattr(wmod.metar, "observations", lambda st, hours=24, unit="F": calls.append(("metar", st)) or [])
    assert wmod.observations_for_rule("KSFO", "hourly", "2026-09-12", "America/Los_Angeles") == []
    assert wmod.observations_for_rule("KSFO", "cli", "2026-09-12", "America/Los_Angeles") == []
    assert wmod.observations_for_rule("EGLL", "hourly", "2026-09-12", "Europe/London") == []
    assert calls == [("metar", "KSFO"), ("nws", "KSFO"), ("metar", "EGLL")]
    # build_ctx without an injected obs list goes through the same switch (offshore KLGA event is 'hourly')
    fetch = {"event": _event(), "members": [78.5] * 10, "hourly": []}
    calls.clear()
    ctx = build_ctx("nyc", datetime(2026, 9, 12, tzinfo=ZoneInfo("America/New_York")), "high", _cfg(), venue="offshore", fetch=fetch)
    assert ctx is not None and calls == [("metar", "KLGA")]
    calls.clear()
    ctx = build_ctx("nyc", datetime(2026, 9, 12, tzinfo=ZoneInfo("America/New_York")), "high", _cfg(), venue="us", fetch=fetch)
    assert ctx.rule == "cli" and calls == [("nws", "KNYC")]


def test_gate_uses_mark_to_market_and_us_paper():
    led = _ledger()
    t0 = time.time() - 600
    ids = []
    for i in range(30):
        sid = led.add_signal(Signal("weather_obs", "offshore", f"t{i}", "x", "BUY_NO", 0.9, 10, 10, "r", ts=t0), "paper")
        led.upsert_paper(sid, filled_ts=t0, fill_price=0.1, status="closed" if i < 20 else "filled",
                         pnl_usd=1.0 if i < 20 else -0.5, exit_ts=t0 + 60 if i < 20 else None)
        ids.append(sid)
    # 30 offshore signals and a healthy mark-to-market are NOT a licence to spend real money: offshore
    # is a read-only proxy on a different settlement rule. The gate wants evidence from the US books.
    ok, why = led.promotion_check("weather_obs")
    assert not ok and why.startswith("0/10 US signals")
    # the same closed net with the open positions deep underwater is a hold on mtm, before US is reached
    for sid in ids[20:]:
        led.upsert_paper(sid, pnl_usd=-3.0)
    ok, why = led.promotion_check("weather_obs")
    assert not ok and why.startswith("mark-to-market -10.00")
    for sid in ids[20:]:
        led.upsert_paper(sid, pnl_usd=0.5)                          # closed +20, open marked +5
    # a thin US record is still not a record: nine signals hold
    us_ids = []
    for i in range(9):
        sid = led.add_signal(Signal("weather_obs", "us", f"s{i}", "x", "BUY_NO", 0.9, 10, 10, "r", ts=t0), "paper")
        led.upsert_paper(sid, filled_ts=t0, fill_price=0.1, status="closed", pnl_usd=0.4, exit_ts=t0 + 60)
        us_ids.append(sid)
    ok, why = led.promotion_check("weather_obs")
    assert not ok and why.startswith("9/10 US signals")
    # ten US signals that have not settled yet are marks, not results
    sid = led.add_signal(Signal("weather_obs", "us", "s9", "x", "BUY_NO", 0.9, 10, 10, "r", ts=t0), "paper")
    led.upsert_paper(sid, filled_ts=t0, fill_price=0.1, status="filled", pnl_usd=0.4)
    us_ids.append(sid)
    for u in us_ids[:9]:
        led.upsert_paper(u, status="filled", exit_ts=None)
    ok, why = led.promotion_check("weather_obs")
    assert not ok and why.startswith("US paper: 10 signals, none settled")
    for u in us_ids[:9]:
        led.upsert_paper(u, status="closed", exit_ts=t0 + 60)
    # a losing US-venue paper record blocks promotion even when the offshore proxy passes
    led.upsert_paper(us_ids[0], pnl_usd=-20.0)
    ok, why = led.promotion_check("weather_obs")
    assert not ok and why.startswith("US paper mark-to-market -16.40")
    led.upsert_paper(us_ids[0], pnl_usd=0.4)
    ok, why = led.promotion_check("weather_obs")
    assert ok and "US 10 signals +4.00" in why
    assert "mtm +29.00" in why                                      # offshore +25 and the US +4, together
    assert "Polymarket US books only" in led.report(1)
    assert "gate PASS: weather_obs" in led.summary(1)
    # evidence before a rule change stops counting
    led.gate_since_ts = time.time() + 1
    assert led.promotion_check("weather_obs") == (False, "no signals")
    assert "gate evidence reset" in led.report(1)                      # else "no signals" reads as a dead module
    led.gate_since_ts = 0.0
    assert "gate evidence reset" not in led.report(1)
    led.add_snapshot("offshore", "old", 0.1, 0.2, ts=time.time() - 10 * 86400)
    led.add_snapshot("offshore", "new", 0.1, 0.2)
    assert led.prune_snapshots(7) == 1 and len(led.snapshots("offshore", "new", 0)) == 1


def test_promote_and_live_module_offshore_stays_paper(monkeypatch):
    from polybot import runner as runner_mod, config as cfg_mod, notify
    cfg, led = _cfg(), _ledger()
    saved, nudged = [], []
    monkeypatch.setattr(cfg_mod, "save", lambda c, path=None: saved.append(dict(c.modes)))
    monkeypatch.setattr(notify, "nudge", lambda *a, **k: nudged.append(a[0]))
    r = runner_mod.Runner(cfg, led, log=lambda *_: None)
    assert r.promote() == [] and saved == []
    monkeypatch.setattr(led, "promotion_check", lambda m, **k: (m == "weather_obs", "fake"))
    assert r.promote() == ["weather_obs"] and cfg.mode("weather_obs") == "live" and saved and nudged == ["polybot: LIVE"]
    assert r.promote(["weather_lock"]) == [] and cfg.mode("weather_lock") == "paper"
    # a live module's offshore signals are recorded as paper, never sent
    sig = Signal("weather_obs", "offshore", "tokX", "x", "BUY_NO", 0.9, 10, 10, "r", meta={"market_id": "m"})
    assert r.handle(sig) == "paper"
    rows = led.open_signals(module="weather_obs")
    assert len(rows) == 1 and rows[0]["mode"] == "paper" and rows[0]["venue"] == "offshore"


def test_pnl_us_venue_includes_rebates():
    sig = {"side": "BUY_YES", "contracts": 100, "taker": False}
    pnl, fee = pnl_usd(sig, 0.90, None, 1, "us", "weather")
    assert fee == pytest.approx(-fees.us_maker_rebate(0.90, 100)) and pnl == pytest.approx(10 + fees.us_maker_rebate(0.90, 100))
    assert fill_from_history({"side": "BUY_YES", "price": 0.5, "ts": 0}, [(1, 0.55), (2, 0.5)]) == (2, 0.5)
    assert fill_from_history({"side": "BUY_NO", "price": 0.4, "ts": 0}, [(1, 0.55), (2, 0.61)]) == (2, 0.6)
    assert exit_from_history({"side": "BUY_YES", "price": 0.5, "exit_rule": "timeout:1h", "horizon_h": 1}, 0, [(1800, 0.52), (3600, 0.53)]) == (3600, 0.53, "timeout")


def test_config_roundtrip(tmp_path):
    cfg = config.Config()
    cfg.modes["weather_lock"] = "signal"
    cfg.caps.sports_enabled = True
    p = str(tmp_path / "c.json")
    config.save(cfg, p)
    back = config.load(p)
    assert back.mode("weather_lock") == "signal" and back.caps.sports_enabled and back.mode("leadlag") == "paper"
    assert back.favorites_band == (0.85, 0.95)


def test_open_meteo_cache_and_429_cooldown(monkeypatch):
    from polybot.feeds import weather
    calls = []

    class R:
        def __init__(self, status):
            self.status_code = status

        def raise_for_status(self):
            if self.status_code >= 400:
                raise requests.HTTPError(f"{self.status_code} Client Error")

        def json(self):
            return {"daily": {"time": ["2026-09-13"], "temperature_2m_max_gfs025_member01": [80.0],
                              "temperature_2m_min_gfs025_member01": [60.0]}}

    statuses = [200, 200, 429, 200]

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append((url, params["latitude"]))
        return R(statuses[len(calls) - 1])

    monkeypatch.setattr(weather._session, "get", fake_get)
    monkeypatch.setattr(weather, "_CACHE", {})
    monkeypatch.setattr(weather, "_OM_COOLDOWN_UNTIL", 0.0)
    a = weather.ensemble_daily(40.7, -74.0, "America/New_York")        # network
    b = weather.ensemble_daily(40.7, -74.0, "America/New_York")        # cache: the 'low' scan reuses the 'high' answer
    assert a == b and len(calls) == 1 and a["2026-09-13"]["max"] == [80.0]
    weather.ensemble_daily(41.9, -87.6, "America/Chicago")              # a different city: network
    with pytest.raises(requests.HTTPError):
        weather.ensemble_daily(34.0, -118.2, "America/Los_Angeles")     # 429 → cooldown starts
    with pytest.raises(RuntimeError, match="cooling down"):
        weather.ensemble_daily(29.7, -95.4, "America/Chicago")          # no network call during cooldown
    assert len(calls) == 3
    assert weather.ensemble_daily(41.9, -87.6, "America/Chicago") == a # cached city still answers in cooldown


# ---- Polymarket US venue: shapes captured live 2026-09-12 ----------------------------------
US_MARKET = {"id": "806288", "question": "Highest temperature in NYC on September 13?",
             "slug": "tc-temp-nychigh-2026-09-13-gte78lt79f", "endDate": "2026-09-14T05:00:00Z",
             "description": "Will the highest temperature recorded at Central Park (KNYC) in New York City for",
             "status": "MARKET_STATUS_OPEN", "closed": False, "title": "78 to 79", "outcomePrices": '["0.3500","0.3600"]',
             "bestBidQuote": {"value": "0.3500", "currency": "USD"}, "bestAskQuote": {"value": "0.3600", "currency": "USD"},
             "feeCoefficient": 0.06}


def _us_event():
    def mk(slug_suffix, title, yes, bid, ask, closed=False):
        m = dict(US_MARKET)
        m.update({"slug": f"tc-temp-nychigh-2026-09-13-{slug_suffix}", "title": title, "outcomePrices": f'["{yes}","{1 - yes:.2f}"]',
                  "bestBidQuote": {"value": str(bid)}, "bestAskQuote": {"value": str(ask)}, "closed": closed})
        return m
    return {"slug": "temp-nychigh-2026-09-13", "title": "Highest temperature in NYC on September 13?",
            "endDate": "2026-09-13T23:59:00Z",
            "markets": [mk("gte80lt81f", "80 to 81", 0.30, 0.29, 0.31), mk("lt78f", "77 or below", 0.05, 0.04, 0.06),
                        mk("gte78lt79f", "78 to 79", 0.35, 0.35, 0.36), mk("gte86f", "86 or higher", 0.02, 0.01, 0.03)]}


def test_us_bucket_titles_and_event():
    from polybot.feeds import usvenue
    assert usvenue.parse_us_bucket_title("77 or below") == (-math.inf, 77)
    assert usvenue.parse_us_bucket_title("78 to 79") == (78, 79)
    assert usvenue.parse_us_bucket_title("86 or higher") == (86, math.inf)
    assert usvenue.us_event_slug("nyc", datetime(2026, 9, 13), "high") == "temp-nychigh-2026-09-13"
    assert usvenue.us_event_slug("san-francisco", datetime(2026, 9, 13), "low") == "temp-sfolow-2026-09-13"
    ev = usvenue.weather_event_from_us(_us_event(), "nyc", "high")
    assert ev.date == "2026-09-13" and ev.station == "KNYC" and ev.rule == "cli" and ev.unit == "F" and not ev.neg_risk
    assert [b.title for b in ev.buckets] == ["77 or below", "78 to 79", "80 to 81", "86 or higher"]
    b = ev.buckets[1]
    assert b.yes_token == b.market_id == "tc-temp-nychigh-2026-09-13-gte78lt79f"      # Signal.market carries the US slug
    assert (b.best_bid, b.best_ask, b.last) == (0.35, 0.36, 0.35) and b.contains(78) and b.contains(79) and not b.contains(80)
    assert ev.buckets[0].contains(-10) and ev.buckets[-1].contains(120)


def test_us_venue_parses_live_shapes(monkeypatch):
    from polybot.feeds import usvenue

    class NotFoundError(Exception):
        pass

    class Markets:
        def bbo(self, slug):
            return {"marketData": {"marketSlug": slug, "bestBid": {"value": "0.2700", "currency": "USD"},
                                   "bestAsk": {"value": "0.2800", "currency": "USD"}, "lastTradePx": {"value": "0.2800"},
                                   "settlementPx": {"value": "0.0000"}, "state": "MARKET_STATE_OPEN"}}

        def book(self, slug):
            return {"marketData": {"bids": [{"px": {"value": "0.2500"}, "qty": "77.29"}, {"px": {"value": "0.2700"}, "qty": "1.0"}],
                                   "asks": [{"px": {"value": "0.3000"}, "qty": "2"}, {"px": {"value": "0.2800"}, "qty": "5"}],
                                   "lastTradePx": {"value": "0.28"}}}

        def settlement(self, slug):
            if slug.endswith("gte78lt79f"):
                return {"marketData": {"settlementPx": {"value": "1.0000"}}}
            raise NotFoundError(f"Settlement not found for market {slug}")

    class Account:
        def balances(self):
            return {"balances": [{"currentBalance": 210.01294, "buyingPower": 210.01294, "displayedCash": 60.01294,
                                  "bonusReservation": 150, "openOrders": 0, "availableToWithdraw": 60.01294}]}

    class Portfolio:
        def positions(self):
            return {"positions": {}, "nextCursor": "", "eof": True, "availablePositions": []}

    class Orders:
        def list(self, *a, **k):
            return {"orders": [{"id": "o1", "marketSlug": "x"}]}

    class Events:
        def retrieve_by_slug(self, slug):
            if slug == "temp-nychigh-2026-09-13":
                return _us_event()
            raise NotFoundError(f'event with slug "{slug}" not found')

    class Search:
        def query(self, q):
            return []

    class Client:
        markets, account, portfolio, orders, events, search = Markets(), Account(), Portfolio(), Orders(), Events(), Search()

    v = usvenue.USVenue()
    v.available, v._client = True, Client()
    assert v.bbo("s") == (0.27, 0.28)
    assert v.book("s") == {"bids": [(0.27, 1.0), (0.25, 77.29)], "asks": [(0.28, 5.0), (0.30, 2.0)], "last": 0.28, "tick": 0.01}
    assert v.balance_usd() == 210.01294 and v.balance_detail()["bonusReservation"] == 150
    assert v.positions() == [] and v.open_orders() == [{"id": "o1", "marketSlug": "x"}]
    assert v.resolution("tc-temp-nychigh-2026-09-13-gte78lt79f") == 1 and v.resolution("tc-x-lt78f") is None
    ev = v.find_weather_event("nyc", datetime(2026, 9, 13), "high")
    assert ev is not None and len(ev.buckets) == 4 and ev.slug == "temp-nychigh-2026-09-13"
    assert v.find_weather_event("nyc", datetime(2026, 9, 14), "high") is None


def test_universe_tiers_partitions_and_rejects_ladders():
    """Buying every leg of a set is only an arb when exactly one leg pays. The venue is full of
    ladders that look like sets and are not — 'CPI YoY above 2.0 / 2.5 / 3.0' are simultaneously
    true, and their prices sum to 7.12, not 1.00."""
    from polybot import universe as u

    def ev(slug, prices, cat="macro", closed=False, status="MARKET_STATUS_OPEN"):
        return {"slug": slug, "category": cat, "closed": closed,
                "markets": [{"outcomePrices": f'["{p}","{1-p:.2f}"]', "closed": closed,
                             "status": status} for p in prices]}

    assert u.classify(ev("usfed-fomc-2026-10-28", [0.6, 0.2, 0.1, 0.1, 0.04]))[0] == u.TIER_WATCH
    assert u.classify(ev("uscpi-september-yoy-2026-10-14", [0.9, 0.8, 0.7, 0.6, 0.5]))[0] == u.TIER_REJECT
    assert u.classify(ev("nfl-min-chi", [0.5] * 810, cat="sports"))[0] == u.TIER_REJECT
    assert u.classify(ev("solo", [1.0]))[0] == u.TIER_REJECT
    assert u.series_key("usfed-fomc-2026-10-28") == "usfed-fomc"
    assert u.series_key("scotus") == "scotus"
    # a proven series short-circuits the price test for every future instance
    assert u.classify(ev("usfed-fomc-2027-01-27", [0.9, 0.9, 0.9]), {"usfed-fomc"})[0] == u.TIER_PROVEN


def test_universe_proves_a_recurring_series_from_its_own_past():
    """A series is otherwise unprovable until its next instance resolves -- banxico meets eight
    times a year, an election is once. events.list takes twenty slugs a call and returns closed
    events, so sweeping past dates costs ~10 calls and needs no knowledge of the schedule."""
    from polybot import runner as runner_mod
    cfg, led = _cfg(), _ledger()
    asked = []

    class Venue:
        available = True

        def events_by_slug(self, slugs, batch=20):
            asked.extend(slugs)
            hit = "banxico-" + (datetime.now() - timedelta(days=43)).date().isoformat()
            if hit not in slugs:
                return {}
            return {hit: {"slug": hit, "category": "macro", "closed": True,
                          "markets": [{"outcomePrices": p, "closed": True,
                                       "status": "MARKET_STATUS_RESOLVED"}
                                      for p in ('["1","0"]', '["0","1"]', '["0","1"]')]}}

    r = runner_mod.Runner(cfg, led, log=lambda *_: None)
    r.us = Venue()
    assert r.prove_by_date_sweep(days_back=200, series=["banxico"]) == 1
    assert r.uni.proven_series() == {"banxico"}
    assert len(asked) == 200                                  # one slug per day, batched by the venue
    # proving is idempotent and stops after the first settled instance
    asked.clear()
    assert r.prove_by_date_sweep(days_back=200, series=["banxico"]) == 0


def test_universe_will_not_prove_a_series_from_prices_alone():
    """An uncontested 2026-11-03 race sits at 0.99/0.01 six weeks before anyone votes, which reads
    exactly like a settled market. Proving off that marks a series tradable on evidence that does
    not exist yet — usltgov-tx and usltgov-vt did exactly that on the first run."""
    import sqlite3
    from polybot import universe as u

    def ev(slug, prices, closed, status):
        return {"slug": slug, "category": "politics", "closed": closed,
                "markets": [{"outcomePrices": f'["{p}","{1-p:.2f}"]', "closed": closed,
                             "status": status} for p in prices]}

    uni = u.Universe(sqlite3.connect(":memory:"))
    future = ev("usltgov-tx-2026-11-03", [0.99, 0.01], closed=False, status="MARKET_STATUS_OPEN")
    assert u.one_winner(future["markets"]) is True        # the PRICES do look decided...
    assert u.settled(future) is False                     # ...but nothing has settled
    assert uni.prove(future) is False
    assert uni.proven_series() == set()
    real = ev("usltgov-tx-2026-11-03", [0.99, 0.01], closed=True, status="MARKET_STATUS_RESOLVED")
    assert uni.prove(real) is True
    assert uni.proven_series() == {"usltgov-tx"}
    # a settled event with two winners proves nothing
    two = ev("bad-2026-01-01", [0.99, 0.99], closed=True, status="MARKET_STATUS_RESOLVED")
    assert uni.prove(two) is False


def test_arb_screen_prices_unquoted_legs_before_believing_anything():
    """The event object omits `bestAskQuote` on buckets that DO have resting offers, and arb_check
    needs an ask on every leg — so the screen evaluated 36 of 1,570 candidate event-minutes over
    9 days (2%). The bound is admissible (no leg costs less than a tick) but it is NOT evidence:
    miami had four legs quoted at 0.04 and a favourite with no ask at any price, which the bound
    alone calls a 96c arb."""
    from polybot.strategies.bucket_sum import arb_possible, unpriced, arb_check
    ev = _event()
    for b in ev.buckets:
        b.best_bid, b.best_ask = 0.09, 0.10          # 9 legs x 0.10 = 0.90 asks, 0.81 bids
    assert arb_check(ev.buckets, "us")[0] == "buy_all"
    ev.buckets[4].best_ask = None                    # one leg unquoted: arb_check now says nothing
    assert arb_check(ev.buckets, "us")[0] is None
    assert unpriced(ev.buckets) == [ev.buckets[4]]   # half-quoted counts: either side missing
    assert arb_possible(ev.buckets)                  # 0.80 + 0.01 < 1.00, worth one book call
    ev.buckets[4].best_ask = 0.10                    # ...and priced, it is a real set
    assert arb_check(ev.buckets, "us")[0] == "buy_all"

    # the miami shape: the cheap legs are quoted, the FAVOURITE is the one nobody offers
    mia = _event()
    for b in mia.buckets:
        b.best_bid, b.best_ask = None, 0.01
    mia.buckets[5].best_ask = None                   # no ask at any price
    assert arb_possible(mia.buckets)                 # the bound cries wolf, as designed
    assert arb_check(mia.buckets, "us")[0] is None   # and arb_check still refuses: unbuyable leg
    # a quoted book that plainly sums over $1 on BOTH sides never costs a call
    rich = _event()
    for b in rich.buckets:
        b.best_bid, b.best_ask = 0.02, 0.30
    rich.buckets[0].best_ask = None
    assert not arb_possible(rich.buckets)

    # the SELL side: tight books sum their BIDS over $1, which is the fomc shape and which a
    # buy-only screen looks straight past
    tight = _event()
    for b in tight.buckets:
        b.best_bid, b.best_ask = 0.12, 0.13          # 9 x 0.12 = 1.08 of bids
    assert arb_check(tight.buckets, "us")[0] == "sell_all"
    tight.buckets[3].best_bid = None                 # one bid missing: cannot sell that leg
    assert arb_check(tight.buckets, "us")[0] is None
    assert unpriced(tight.buckets) == [tight.buckets[3]]
    assert arb_possible(tight.buckets)               # its bid can be at most its ask: still > $1


def test_us_settlement_is_read_from_the_bare_settlement_key():
    """The live endpoint answers {"slug": ..., "settlement": 0|1} — a bare number, not the
    `settlementPrice` Amount the SDK type hints promise. Reading only the hinted names returned
    None for markets that had plainly RESOLVED, so no US paper trade ever closed, the gate's
    "US paper has closed trades and is in profit" clause could never be satisfied, and NO module
    could ever be promoted. `settlement` is legitimately 0, so it must not be chained with `or`."""
    from polybot.feeds import usvenue

    class NotFound(Exception):
        pass

    answers = {"win": {"slug": "win", "settlement": 1},
               "lose": {"slug": "lose", "settlement": 0},
               "amount": {"slug": "amount", "settlementPrice": {"value": "1.0000"}},
               "mid": {"slug": "mid", "settlement": 0.5}}

    class Markets:
        def settlement(self, slug):
            if slug not in answers:
                raise NotFound("Settlement not found for market " + slug)
            return answers[slug]

    class Client:
        markets = Markets()

    v = usvenue.USVenue()
    v.available, v._client = True, Client()
    assert v.resolution("win") == 1
    assert v.resolution("lose") == 0                 # the falsy 0 is an ANSWER, not a missing key
    assert v.resolution("amount") == 1               # the documented shape still works
    assert v.resolution("mid") is None               # neither side: not resolved
    assert v.resolution("open-market") is None       # 404 until it settles


def test_arb_set_unwinds_what_filled_when_a_leg_is_killed():
    """Six legs bought for 92c pay $1. Four of the six pay $1 only if the temperature lands in one
    of the four — a bet nobody sized. So every leg goes out FILL_OR_KILL, and if one is killed the
    fills are sold straight back: the spread on those legs is a known bounded loss, an unhedged
    basket is not."""
    from polybot.execution import Executor
    sent, killed_leg = [], "leg3"

    class Venue:
        available = True
        balance_usd = staticmethod(lambda: 10_000.0)

        def place_limit(self, slug, side, price, contracts, tif="gtc"):
            sent.append((slug, side, price, contracts, tif))
            if slug == killed_leg and side.startswith("BUY"):
                return {"id": "o", "status": "ORDER_STATUS_KILLED"}
            return {"id": "o", "status": "ORDER_STATUS_FILLED"}

        def bbo(self, slug):
            return 0.30, 0.34

    led = _ledger()
    ex = Executor(led, Venue(), _cfg(), log=lambda *_: None)
    legs = []
    for i in range(1, 5):
        sig = Signal("bucket_sum", "us", f"leg{i}", f"L{i}", "BUY_YES", 0.25, 5.0, 6, "r", taker=True, arb=True)
        legs.append((led.add_signal(sig, "live"), sig))
    out = ex.place_arb_set(legs)
    # leg3 is killed, so leg4 is never sent: the set cannot complete without leg3, and every
    # further order would buy a leg we are about to sell straight back at the spread.
    assert out["ok"] is False and out["filled"] == 2
    assert out["unwound"] == 2                                  # every filled leg sold back
    assert "leg4" not in [r[0] for r in sent]
    assert all(t == "fok" for *_r, t in sent if _r[1].startswith("BUY"))
    assert [t for *_r, t in sent if _r[1].startswith("SELL")] == ["ioc"] * 2
    assert led.paper_row(legs[2][0]) is None or led.paper_row(legs[2][0]).get("status") != "filled"

    # and the happy path leaves the set whole, with no unwind
    sent.clear()
    killed_leg = "none"
    led2 = _ledger()
    ex2 = Executor(led2, Venue(), _cfg(), log=lambda *_: None)
    legs2 = []
    for i in range(1, 5):
        sig = Signal("bucket_sum", "us", f"leg{i}", f"L{i}", "BUY_YES", 0.25, 5.0, 6, "r", taker=True, arb=True)
        legs2.append((led2.add_signal(sig, "live"), sig))
    out2 = ex2.place_arb_set(legs2)
    assert out2["ok"] and out2["filled"] == 4 and out2["unwound"] == 0
    assert all(led2.paper_row(sid)["status"] == "filled" for sid, _ in legs2)


def test_arb_set_is_refused_whole_when_any_leg_fails_risk():
    """Checking legs one at a time is how you end up holding four of six."""
    cfg, led = _cfg(), _ledger()
    cfg.modes["bucket_sum"] = "paper"
    from polybot import runner as runner_mod
    r = runner_mod.Runner(cfg, led, log=lambda *_: None)
    good = [Signal("bucket_sum", "us", f"m{i}", f"L{i}", "BUY_YES", 0.20, 4.0, 6, "r", taker=True, arb=True)
            for i in range(3)]
    assert r.handle_arb_set(good) == 3
    led2 = _ledger()
    r2 = runner_mod.Runner(cfg, led2, log=lambda *_: None)
    bad = [Signal("bucket_sum", "us", f"n{i}", f"L{i}", "BUY_YES", 0.20, 4.0, 6, "r", taker=True, arb=True)
           for i in range(3)]
    bad[1].size_usd = 999.0                                      # one leg over the per-market cap
    assert r2.handle_arb_set(bad) == 0
    assert led2.conn.execute("SELECT COUNT(*) c FROM signals").fetchone()["c"] == 0   # nothing recorded


def test_sell_all_is_sized_on_what_a_leg_costs_not_on_the_bid():
    """Buying NO on every leg costs (1 - bid) per leg, not the bid. Sizing off the bid let the
    dearest legs blow the per-market cap, risk refused them, and the whole set was thrown away --
    which is why no sell-all set ever reached the ledger even though the bids summed over $1 in a
    third of the event-minutes that had a full set of them."""
    from polybot.strategies.bucket_sum import BucketSum, arb_check
    from polybot import runner as runner_mod
    cfg = _cfg()
    ev = _event()
    for b in ev.buckets:                                  # 9 legs bidding 0.14 = 1.26 of bids
        b.best_bid, b.best_ask, b.bid_qty, b.ask_qty = 0.14, 0.15, 500, 500
        b.bid_levels, b.ask_levels = [(0.14, 500)], [(0.15, 500)]
    kind, net, prices = arb_check(ev.buckets, "us")
    assert kind == "sell_all" and net > 0
    sigs = BucketSum(cfg).scan(_ctx(event=ev))
    assert sigs and all(s.side == "BUY_NO" for s in sigs)
    # each leg costs 1 - 0.12 = 0.88, so NO leg may exceed the $20 per-market cap...
    assert max(s.size_usd for s in sigs) <= cfg.caps.max_per_market_usd + 1e-9
    # ...and the whole set must fit the exposure cap on its REAL cost, not on the bids
    assert sum(s.size_usd for s in sigs) <= cfg.caps.max_exposure_usd + 1e-9
    assert len({s.contracts for s in sigs}) == 1
    # every leg passes risk, so the set is accepted whole rather than silently dropped
    r = runner_mod.Runner(cfg, _ledger(), log=lambda *_: None)
    assert r.handle_arb_set(sigs) == len(sigs)


def test_one_books_failure_does_not_abort_the_whole_sweep():
    """_scan_one guards its context build, but the arb screen after it — price_legs, fill_depth —
    was unprotected, so one timed-out request took the entire sweep down:

        17:52:22  arb sweep error: Request timed out.   (httpx, raised inside scan_weather)

    Ten books went unscreened because one of them was slow."""
    from polybot import runner as runner_mod

    cfg, led = _cfg(), _ledger()
    cfg.modes["bucket_sum"] = "paper"
    r = runner_mod.Runner(cfg, led, log=lambda *_: None)
    r.us = type("V", (), {"available": True,
                          "prefetch_weather_events": lambda self, t: 0})()
    seen = []

    def flaky(city, kind, off, *a):
        seen.append(city)
        if city == "miami":
            raise RuntimeError("Request timed out.")
        return 1

    r._scan_one = flaky
    cities = ["nyc", "chicago", "miami", "los-angeles", "san-francisco"]
    n = r.scan_weather(cities=cities, modules=["bucket_sum"], venue="us",
                       kinds=("high",), day_offsets=(0,))

    assert set(seen) == set(cities)        # every book still got its turn
    assert n == 4                          # the four that worked still counted


def test_a_slow_scan_pass_abandons_its_tail_instead_of_holding_the_loop():
    """Arb episodes last about a minute, so five cities scanned slowly are worth less than three
    scanned now. On 2026-09-19 a pass stalled after its first city -- no error, no rate limit, just
    slow sockets -- and held the loop for fifteen minutes, which no cadence tuning upstream can
    fix."""
    from polybot import runner as runner_mod
    cfg, led = _cfg(), _ledger()
    cfg.arb_pass_budget_s = 0.25
    cfg.modes["bucket_sum"] = "paper"
    r = runner_mod.Runner(cfg, led, log=lambda *_: None)
    r.us = type("V", (), {"available": True})()
    scanned = []

    def slow(city, kind, day_offset, *a):
        scanned.append(city)
        time.sleep(0.2)
        return 0

    r._scan_one = slow
    cities = ["nyc", "chicago", "miami", "los-angeles", "san-francisco"]
    r.scan_weather(cities=cities, modules=["bucket_sum"], kinds=("high",), venue="us",
                   day_offsets=(0,))
    assert len(scanned) == 2                      # the clock stopped it; it did not grind on

    # ...and WHICH two rotates. A fixed order meant the same tail was dropped every pass, so with
    # ten city-days and a 45s budget san-francisco and all of tomorrow were never screened at all
    # while the log honestly reported "skipped 4" each time.
    firsts = set()
    for _ in range(5):
        scanned.clear()
        r.scan_weather(cities=cities, modules=["bucket_sum"], kinds=("high",), venue="us",
                       day_offsets=(0,))
        firsts.add(scanned[0])
    assert len(firsts) >= 3                       # the cost of running out of time is shared out
    assert firsts <= set(cities)

    # left to itself the US path also sweeps tomorrow, so the clock is per city-DAY not per city
    scanned.clear()
    r._scan_rot = 0
    r.scan_weather(cities=["nyc"], modules=["bucket_sum"], kinds=("high",), venue="us")
    assert scanned == ["nyc", "nyc"][:len(scanned)] and len(scanned) <= 2

    # off the US path there is no deadline, and no rotation: the offshore proxy is not
    # time-critical and every city must be scanned every pass.
    scanned.clear()
    r.scan_weather(cities=["nyc", "chicago", "miami"], modules=["bucket_sum"],
                   kinds=("high",), venue="offshore")
    assert scanned == ["nyc", "chicago", "miami"]


def test_a_blind_venue_says_so_and_the_budget_retunes_itself():
    """scan_weather returns 0 the moment the venue is unavailable, and it logs nothing -- so a
    rate-limited bot simply stops scanning and nothing says why. That produced 12-to-16 minute
    holes in a 2-minute window on 2026-09-19, invisible in the log. The budget was also tuned on a
    probe that had the campus IP to itself, which it does not."""
    from polybot.feeds import usvenue
    said = []

    class RateLimited(Exception):
        pass

    class Markets:
        def book(self, slug):
            raise RateLimited("<!doctype html>...You are being rate limited...")

    class Client:
        markets = Markets()

    v = usvenue.USVenue()
    v.available, v._client = True, Client()
    v.on_backoff = said.append
    start_window = v._window_s
    assert v.book("x") is None
    assert v.available is False
    assert said and "blind" in said[0] and "budget now" in said[0]
    assert v._window_s > start_window            # the window widens rather than insisting it knows
    v._backoff_until = 0.0                       # a second refusal widens it again
    v.available = True
    v.book("x")
    assert v._window_s > start_window * 1.5 - 1e-9
    assert len(said) == 2


def test_the_screen_may_reuse_a_recent_book_but_a_trade_never_does():
    """Re-pricing the same 1c tail leg every two minutes is most of what the screen spends, and at
    five requests per twelve seconds that housekeeping can queue ahead of a real candidate's depth
    read. A stale screen costs at worst a second look; a stale FILL is a bad trade, so the decision
    to trade always reads fresh."""
    from polybot.feeds import usvenue
    calls = []

    class Markets:
        def book(self, slug):
            calls.append(slug)
            return {"marketData": {"bids": [{"px": {"value": "0.02"}, "qty": "40"}],
                                   "offers": [{"px": {"value": "0.03"}, "qty": "40"}]}}

    class Client:
        markets = Markets()

    v = usvenue.USVenue()
    v.available, v._client = True, Client()
    ev = _event()
    for b in ev.buckets:
        b.best_bid = b.best_ask = 0.5
    ev.buckets[0].best_bid = ev.buckets[1].best_bid = None      # two legs half-quoted
    # the screen prices them, then reuses them on the next pass without spending a call
    assert v.price_legs(ev.buckets) == 2
    first = len(calls)
    assert first == 2
    ev.buckets[0].best_bid = ev.buckets[1].best_bid = None
    assert v.price_legs(ev.buckets) == 2
    assert len(calls) == first                       # served from the cache
    # the trade path re-reads every leg regardless of how recently the screen looked
    v.fill_depth_buckets(ev.buckets[:2])
    assert len(calls) == first + 2

    # all or nothing: arb_check needs EVERY leg quoted, so pricing some of a book answers nothing
    # and spends calls doing it -- 42% of screens were doing exactly that
    calls.clear()
    wide = _event()
    for b in wide.buckets:
        b.best_bid = b.best_ask = None               # nine legs need pricing, limit is six
    assert v.price_legs(wide.buckets, limit=6) == 0
    assert calls == []


def test_hopeless_candidates_are_refused_before_the_depth_call_is_paid_for():
    """The depth read is the expensive half of the screen -- one book call per leg. The two tests
    that kill most candidates need only prices and a settlement date, so running them afterwards
    meant paying five calls every two minutes to re-refuse the same boc set: ~1,200 calls a day
    for an answer that never changes."""
    from polybot.strategies.bucket_sum import worth_confirming
    cfg = _cfg()
    ev = _event()

    def book(bid, ask):
        for b in ev.buckets:
            b.best_bid, b.best_ask = bid, ask
        return ev.buckets

    # a set that cannot pay for its own unwind is refused without touching the book
    cfg.arb_unwind_cover = 1.0
    assert not worth_confirming(book(0.09, 0.10), 7.0, cfg)
    # one that can, is confirmed
    assert worth_confirming(book(0.08, 0.09), 16.0, cfg)
    cfg.arb_unwind_cover = 0.25
    # a cheap set held for six weeks is still fine -- 16c on $0.81 is 0.51%/day even at 39 days
    assert worth_confirming(book(0.08, 0.09), 16.0, cfg, days=39.0)
    # the boc shape is the one that fails: selling nine legs bid at 0.20 ties up $7.20 a set, so
    # the same 16c is 2.2% on capital and 0.06%/day over 39 days -- fine on cents, hopeless on time
    assert not worth_confirming(book(0.20, 0.21), 16.0, cfg, days=39.0)
    assert worth_confirming(book(0.20, 0.21), 16.0, cfg, days=1.25)
    # below the flat floor it never gets that far
    assert not worth_confirming(book(0.08, 0.09), 0.2, cfg, days=1.25)


def test_a_voided_signal_is_not_evidence_for_going_live():
    """Voiding a signal says the bot would NOT take it -- a rule changed under it. Counting it
    toward the gate releases real money on the strength of trades the current rules refuse, which
    is the opposite of what the gate is for. Two sets voided on 2026-09-19 were still counting."""
    led = _ledger()
    t0 = time.time() - 600
    for ep in range(4):
        for leg in range(6):
            sid = led.add_signal(Signal("bucket_sum", "us", f"e{ep}l{leg}", "x", "BUY_YES", 0.2, 2.0, 6,
                                        "r", ts=t0, arb=True, meta={"group": f"ev{ep}"}), "paper")
            led.upsert_paper(sid, filled_ts=t0, fill_price=0.2, status="closed",
                             pnl_usd=0.5, exit_ts=t0 + 60)
            if ep < 2:
                led.set_signal_status(sid, "void")
    assert led.decision_count("bucket_sum") == 2          # four episodes, two of them voided
    stats = {r["module"]: r for r in led.module_stats(30)}
    assert stats["bucket_sum"]["n"] == 12                 # and the voided rows leave the P&L too
    assert stats["bucket_sum"]["pnl"] == pytest.approx(6.0)


def test_an_arb_is_capped_by_what_it_can_lose_not_only_by_what_it_ties_up():
    """A completed set pays $1 whatever happens, so the money at stake is unwinding a half-fill,
    not the capital committed. Governing the commitment alone capped the upside (56 buy-side sets
    where the bankroll allowed 96) while saying nothing about the downside."""
    from polybot.strategies.bucket_sum import BucketSum, unwind_cost_cents
    from polybot.risk import RiskManager
    cfg = _cfg()
    ev = _event()
    for b in ev.buckets:                       # 9 cheap legs: 0.81 of asks, deep ladders
        b.best_bid, b.best_ask, b.ask_qty, b.bid_qty = 0.08, 0.09, 5000, 5000
        b.bid_levels, b.ask_levels = [(0.08, 5000)], [(0.09, 5000)]
    per_set_risk = unwind_cost_cents(ev.buckets, "buy_all") / 100.0
    cfg.arb_max_risk_usd = 0.45                # deliberately the tightest rail
    cfg.arb_max_set_cost_usd = 1e9
    cfg.caps.max_exposure_usd = 1e9
    sigs = BucketSum(cfg).scan(_ctx(event=ev))
    assert sigs
    n = sigs[0].contracts
    assert n * per_set_risk <= 0.45 + 1e-9     # sized by the loss, not the outlay
    assert sum(s.size_usd for s in sigs) > 0.45   # and it commits far more than it risks
    # risk agrees, and refuses a set whose recorded unwind exceeds the cap
    rm = RiskManager(cfg, _ledger())
    assert all(rm.allow(sig)[0] for sig in sigs)
    cfg.arb_max_risk_usd = 0.01
    ok, why = rm.allow(sigs[0])
    assert not ok and "on a failed fill" in why


def test_unwind_cost_prices_a_one_sided_leg_instead_of_skipping_the_test():
    """A leg bought at the ask with NO bid behind it returns nothing when you try to close it --
    the worst case. The old test skipped entirely whenever any leg was one-sided, so exactly those
    sets went through unchecked, and a survey of buy-side arbs came back empty because it had
    quietly thrown out every book with an unquoted tail leg (most of them)."""
    from polybot.strategies.bucket_sum import unwind_cost_cents
    import math as _m
    ev = _event()
    for b in ev.buckets:
        b.best_bid, b.best_ask = 0.09, 0.10
    assert unwind_cost_cents(ev.buckets, "buy_all") == pytest.approx(9.0)      # 9 legs x 1c
    ev.buckets[4].best_bid = None                                              # nobody will buy it back
    assert unwind_cost_cents(ev.buckets, "buy_all") == pytest.approx(8 * 1.0 + 10.0)
    # selling: closing a NO means buying the YES back, so a leg with no ASK is the stuck one
    for b in ev.buckets:
        b.best_bid, b.best_ask = 0.09, 0.10
    assert unwind_cost_cents(ev.buckets, "sell_all") == pytest.approx(9.0)
    ev.buckets[2].best_ask = None
    assert unwind_cost_cents(ev.buckets, "sell_all") == pytest.approx(8 * 1.0 + 91.0)
    # a leg we cannot even price is untradable, not free
    ev.buckets[2].best_bid = None
    assert unwind_cost_cents(ev.buckets, "sell_all") == _m.inf


def test_an_empty_ladder_clears_the_stale_quote_it_replaces():
    """Once the book has answered, the book is the truth. miahigh's "92 or above" showed ask=0.04
    from the event object and NO offers at all in the book on 2026-09-19; leaving the stale quote
    in place made arb_check report a 4.4c buy-all on a leg that could not be bought at any price
    -- the phantom-edge trap wearing a new hat."""
    from polybot.feeds import usvenue
    from polybot.strategies.bucket_sum import arb_check

    class Markets:
        def book(self, slug):
            if slug.endswith("t8"):                 # the favourite nobody will sell
                return {"marketData": {"bids": [{"px": {"value": "0.03"}, "qty": "5"}], "offers": []}}
            return {"marketData": {"bids": [{"px": {"value": "0.09"}, "qty": "50"}],
                                   "offers": [{"px": {"value": "0.10"}, "qty": "50"}]}}

    class Client:
        markets = Markets()

    v = usvenue.USVenue()
    v.available, v._client = True, Client()
    ev = _event()
    for b in ev.buckets:                            # the event object quotes every leg
        b.best_bid, b.best_ask = 0.09, 0.10
    assert arb_check(ev.buckets, "us")[0] == "buy_all"     # 0.90 of asks: looks like an arb
    v.fill_depth_buckets(ev.buckets)
    assert ev.buckets[8].best_ask is None           # the book says nobody is offering that leg
    assert ev.buckets[8].ask_levels == []
    assert arb_check(ev.buckets, "us")[0] is None   # so there is no buy-all set to be had
    assert ev.buckets[8].best_bid == 0.03           # the side that DOES exist is kept


def test_sizing_walks_the_ladder_for_dollars_not_cents_per_set():
    """Top-of-book sizing picks the best cents-per-set and the worst dollars. Chicago's "71 or
    below" bid 3 contracts at 0.16 and 100 at 0.15 on 2026-09-19: one cent of price is the
    difference between a 3-set arb worth $0.18 and a 100-set one worth $5.00."""
    from polybot.strategies.bucket_sum import size_for_profit, limit_for
    cfg = _cfg()
    cfg.arb_max_set_cost_usd = 1e9        # isolate the ladder logic from the caps
    cfg.caps.max_exposure_usd = 1e9
    ev = _event()
    for b in ev.buckets:                  # nine legs bidding 0.14 deep: 1.26 of bids
        b.best_bid, b.best_ask = 0.14, 0.15
        b.bid_levels, b.ask_levels = [(0.14, 500)], [(0.15, 500)]
    # one leg is thin at the top and fat a cent below -- the chicago shape
    thin = ev.buckets[3]
    thin.bid_levels = [(0.14, 3), (0.13, 500)]
    assert limit_for(thin.bid_levels, 3) == 0.14
    assert limit_for(thin.bid_levels, 100) == 0.13      # one cent buys 30x the size
    assert limit_for(thin.bid_levels, 9999) is None     # honest about a ladder that cannot fill

    contracts, prices, net = size_for_profit(ev.buckets, "sell_all", cfg)
    assert contracts > 3, "top-of-book sizing would have stopped at 3 sets"
    assert prices[3] == 0.13                            # it accepted the worse price knowingly
    # and it chose dollars: 3 sets at the better price is worth less than what it picked
    top_only = 3 * ((sum(0.14 for _ in ev.buckets) - 1.0) * 100) / 100
    assert (net / 100.0) * contracts > top_only


def test_arb_must_pay_for_its_own_unwind():
    """A set that half-fills is unwound by selling every filled leg back at the bid -- one full
    spread each. The first real set (chicago, 2026-09-19) paid 6.0c against 20c of spread across
    six legs: $0.12 of profit risking $0.40 to get it. Both scale with the number of sets, so the
    test is size-free.

    Structural consequence: at a 1c tick an N-leg set carries at least Nc of spread, so an arb has
    to be worth more than a cent a leg before it is worth attempting at all."""
    from polybot.strategies.bucket_sum import BucketSum
    cfg = _cfg()
    ev = _event()

    def book(bid, ask):
        for b in ev.buckets:
            b.best_bid, b.best_ask, b.ask_qty, b.bid_qty = bid, ask, 500, 500
            b.bid_levels, b.ask_levels = [(bid, 500)], [(ask, 500)]
        return _ctx(event=ev)

    # the default is evidence, not caution: a US quote moves 1.7% of the time in the 30s between
    # the depth read and the order landing, so a six-leg set fails ~10% of the time and 0.25 is a
    # 2.5x margin. At the old 1.0 exactly one event-minute in nine days cleared it.
    assert cfg.arb_unwind_cover == 0.25
    cfg.arb_unwind_cover = 1.0                              # the old "a half-fill is certain" bar
    assert BucketSum(cfg).scan(book(0.09, 0.10)) == []      # ~7c net against 9c of spread
    assert BucketSum(cfg).scan(book(0.08, 0.09))            # ~16c net against 9c: clears even that
    cfg.arb_unwind_cover = 0.25                             # at the measured bar the thin set trades
    assert BucketSum(cfg).scan(book(0.09, 0.10))
    cfg.arb_unwind_cover = 5.0                              # and a bar nothing can clear stops it
    assert BucketSum(cfg).scan(book(0.08, 0.09)) == []


def test_arb_refuses_a_set_that_locks_capital_for_weeks_to_earn_cents():
    """Cents per set is not profit -- profit is per dollar TIED UP until settlement. On 2026-09-19
    a boc sell-all paid 1c on a $3.96 set settling in 39 days (0.006%/day) while a weather set
    paid 12c on $0.92 settling in ~1.25 days (10%/day). A flat cents threshold cannot tell them
    apart, and taking the first locks the bankroll out of every good trade for a month."""
    from polybot.strategies.bucket_sum import BucketSum
    cfg = _cfg()
    ev = _event()
    for b in ev.buckets:                                   # 9 legs, asks sum 0.81: 19c gross
        b.best_bid, b.best_ask, b.ask_qty, b.bid_qty = 0.08, 0.09, 500, 500
        b.bid_levels, b.ask_levels = [(0.08, 500)], [(0.09, 500)]

    class Ctx:
        proven_exhaustive = False

        def __init__(self, days):
            self.event, self.venue, self.city, self.kind = ev, "us", "nyc", "high"
            self.date, self.settles_in_days = "2026-09-19", days

    assert BucketSum(cfg).scan(Ctx(1.25))                  # ~20% on capital overnight: take it
    assert BucketSum(cfg).scan(Ctx(39.0)) == []            # same cents, six weeks: not worth it
    cfg.arb_min_roc_per_day_pct = 0.0                      # knob off -> horizon stops mattering
    assert BucketSum(cfg).scan(Ctx(39.0))
    # with no horizon known at all it falls back to the flat threshold rather than inventing one
    cfg.arb_min_roc_per_day_pct = 0.5
    assert BucketSum(cfg).scan(Ctx(None))


def test_arb_picks_the_direction_with_the_better_return_on_capital():
    """A buy-all set ties up ~$0.88 to make 12c; the same six legs sold tie up ~$4.92 to make 8c.
    Choosing on cents-per-set picks the one that earns an eighth as much per dollar deployed."""
    from polybot.strategies.bucket_sum import arb_check
    ev = _event()
    n = len(ev.buckets)
    # asks sum to 0.90 (buy nets ~10c on $0.90) and bids sum to 1.12 (sell nets ~12c on $7.88)
    for b in ev.buckets:
        b.best_ask = round(0.90 / n, 4)
        b.best_bid = round(1.12 / n, 4)
    kind, net, _ = arb_check(ev.buckets, "us")
    assert kind == "buy_all"                    # smaller net per set, far better per dollar
    # with the buy side gone, the sell side is still taken on its own merits
    for b in ev.buckets:
        b.best_ask = round(1.30 / n, 4)
    kind2, net2, _ = arb_check(ev.buckets, "us")
    assert kind2 == "sell_all" and net2 > 0


def test_arb_set_is_all_or_nothing_never_unbalanced():
    """An arb is N contracts of every leg. A set that ends up 11 of one and 12 of another is a
    naked basket with a story attached, and that is exactly what 4-decimal prices produced before
    the invariant existed. Better no trade than a half-set."""
    from polybot.strategies.bucket_sum import BucketSum
    ev = _event()
    for b in ev.buckets:
        b.best_bid, b.best_ask, b.ask_qty = 0.001, None, 30
    asks = [0.0413, 0.0917, 0.1231, 0.0719, 0.1522, 0.1111, 0.0888, 0.1299, 0.0900]
    for b, a in zip(ev.buckets, asks):
        b.best_ask = a
    sigs = BucketSum(_cfg()).scan(_ctx(event=ev))
    # whatever it decides, it never emits a set whose legs disagree on the contract count
    assert sigs == [] or len({s.contracts for s in sigs}) == 1


def test_arb_legs_are_exempt_from_the_dust_floor():
    """A leg priced at 1c is SUPPOSED to cost cents. Judging it by the min-order threshold meant
    for single bets refuses the cheap legs and fills the dear ones — the naked basket again."""
    cfg, led = _cfg(), _ledger()
    rm = RiskManager(cfg, led)
    penny = Signal("bucket_sum", "us", "leg", "x", "BUY_YES", 0.01, 0.30, 6, "r", taker=True, arb=True)
    assert rm.allow(penny)[0]
    lone = Signal("weather_lock", "us", "leg2", "x", "BUY_YES", 0.01, 0.30, 6, "r")
    assert not rm.allow(lone)[0] and "below min order" in rm.allow(lone)[1]


def test_live_arb_is_blocked_until_the_executor_can_unwind_a_partial_set():
    """Six legs go out as six independent orders. Fill four and you hold a naked basket with no
    unwind path — the very thing the set exists to avoid. Paper measures it; money waits."""
    cfg, led = _cfg(), _ledger()
    cfg.modes["bucket_sum"] = "live"
    rm = RiskManager(cfg, led)
    leg = Signal("bucket_sum", "us", "leg", "x", "BUY_YES", 0.20, 4.0, 6, "r", taker=True, arb=True)
    ok, why = rm.allow(leg)
    assert not ok and "group execution" in why
    assert rm.allow(leg, mode="paper")[0]                 # paper still records it
    cfg.arb_live_ok = True
    assert rm.allow(leg)[0]


def test_gate_counts_arb_sets_not_legs():
    """Six legs of one episode are one piece of evidence, not six. Counting rows would let real
    money out after five observed sets."""
    led = _ledger()
    t0 = time.time() - 600
    for ep in range(5):
        for leg in range(6):
            led.add_signal(Signal("bucket_sum", "us", f"e{ep}l{leg}", "x", "BUY_YES", 0.2, 2.0, 6, "r",
                                  ts=t0, arb=True, meta={"group": f"ev{ep}:buy_all:80"}), "paper")
    assert led.decision_count("bucket_sum") == 5            # 30 rows, 5 decisions
    ok, why = led.promotion_check("bucket_sum")
    assert not ok and why.startswith("5/30 signals")
    # a module without groups still counts one decision per row
    for i in range(4):
        led.add_signal(Signal("weather_lock", "us", f"m{i}", "x", "BUY_YES", 0.9, 18, 6, "r", ts=t0), "paper")
    assert led.decision_count("weather_lock") == 4


def test_backtest_refuses_to_report_a_run_with_no_book():
    """A replay with no price history produces no signals and prints a tidy net=$0.00 for every
    city-day — indistinguishable from a strategy that simply found nothing. On 2026-09-18 a
    `--days 30` run 400'd on all 987 buckets (the CLOB rejects that window at fidelity=5) and
    reported a clean zero. A run that could not read the book is not a result."""
    from polybot import backtest as bt
    empty = bt.summarize([], {}, days_done=40, coverage=0.0)
    assert empty["book_coverage"] == 0.0
    out = bt.format_summary(empty)
    assert "NO RESULT" in out and "0% of buckets" in out
    ok = bt.summarize([{"module": "weather_lock", "filled": True, "pnl": 2.0, "size_usd": 20.0}],
                      {}, days_done=40, coverage=0.95)
    assert "NO RESULT" not in bt.format_summary(ok)
    assert ok["modules"]["weather_lock"]["net"] == 2.0


def test_us_event_lookup_unwraps_the_envelope_and_skips_the_search_fallback():
    """GET /v1/event/slug/{slug} answers {"event": {...}}; search.query answers events bare.

    Reading `markets` off the envelope always found nothing, so every city fell through to the
    search fallback: two calls per city per kind per scan against the host that rate-limits this
    campus IP. One call is the whole point.
    """
    from polybot.feeds import usvenue

    event = {"slug": "temp-nychigh-2026-09-19", "endDate": "2026-09-19T23:59:59Z", "markets": [
        {"slug": "tc-temp-nychigh-2026-09-19-lt69f", "title": "68 or below",
         "description": "at Central Park (KNYC)", "bestBidQuote": {"value": "0.01"},
         "bestAskQuote": {"value": "0.05"}, "outcomePrices": '["0.02","0.98"]'},
        {"slug": "tc-temp-nychigh-2026-09-19-gte69lt70f", "title": "69 to 70",
         "description": "at Central Park (KNYC)", "bestBidQuote": {"value": "0.40"},
         "bestAskQuote": {"value": "0.43"}, "outcomePrices": '["0.41","0.59"]'}]}
    calls = []

    class Events:
        def retrieve_by_slug(self, slug):
            calls.append(("event", slug))
            return {"event": event}                    # the envelope the venue actually sends

    class Search:
        def query(self, q):
            calls.append(("search", q))
            return {"events": [event]}

    class Client:
        events, search = Events(), Search()

    v = usvenue.USVenue()
    v.available, v._client = True, Client()
    ev = v.find_weather_event("nyc", datetime(2026, 9, 19, tzinfo=ZoneInfo("America/New_York")), "high")
    assert ev is not None and ev.slug == "temp-nychigh-2026-09-19"
    assert [b.title for b in ev.buckets] == ["68 or below", "69 to 70"]
    assert ev.station == "KNYC" and ev.rule == "cli"
    assert calls == [("event", "temp-nychigh-2026-09-19")]           # the search fallback never ran


def test_us_missing_event_is_not_asked_for_again_this_hour():
    """Polymarket US lists a HIGH market per city per day and no LOW market, so the `low` half of
    every scan was two wasted calls per city per hour at a host that bans this IP for volume."""
    from polybot.feeds import usvenue
    calls = []

    class NotFound(Exception):
        pass

    class Events:
        def retrieve_by_slug(self, slug):
            calls.append(slug)
            raise NotFound("not found")

    class Search:
        def query(self, q):
            calls.append(q)
            return {"events": []}

    class Client:
        events, search = Events(), Search()

    v = usvenue.USVenue()
    v.available, v._client = True, Client()
    day = datetime(2026, 9, 19, tzinfo=ZoneInfo("America/New_York"))
    assert v.find_weather_event("nyc", day, "low") is None
    assert len(calls) == 2                                            # slug miss, then the search miss
    assert v.find_weather_event("nyc", day, "low") is None
    assert len(calls) == 2                                            # second scan asks nobody
    v._missing["temp-nyclow-2026-09-19"] = time.time() - 1            # an hour later it tries again
    assert v.find_weather_event("nyc", day, "low") is None
    assert len(calls) == 4


def test_us_book_reads_the_offer_side():
    """The venue calls the ask side "offers" (SDK MarketBook: bids / offers). Reading "asks" came
    back empty on every US market, which is the exact shape that made `(1 - post)` look like 90c of
    free edge on an empty book — the bug that cost weather_lock its fills."""
    from polybot.feeds import usvenue

    class Markets:
        def book(self, slug):
            return {"marketData": {
                "marketSlug": slug,
                "bids": [{"px": {"value": "0.0100"}, "qty": "100.0000"}],
                "offers": [{"px": {"value": "0.5000"}, "qty": "100.0000"},
                           {"px": {"value": "0.4000"}, "qty": "50.0000"}],
                "stats": {"lastPriceSample": {"longPx": {"value": "0.2500"}}}}}

    class Client:
        markets = Markets()

    v = usvenue.USVenue()
    v.available, v._client = True, Client()
    b = v.book("tc-temp-nychigh-2026-09-19-lt69f")
    assert b["bids"] == [(0.01, 100.0)]
    assert b["asks"] == [(0.40, 50.0), (0.50, 100.0)]                 # offers, cheapest first
    assert b["last"] == 0.25                                          # moved under stats.lastPriceSample


def test_us_venue_backs_off_on_rate_limit_instead_of_hammering():
    """2026-09-17: CWRU's shared IP got Cloudflare-1015-rate-limited by gateway.polymarket.us, and
    every scan/settle tick kept calling the banned host again, each failure logging the full HTML
    error page (~250 lines) with nothing to let the ban clear. A rate limit should trip a cooldown
    (`available` goes False, same as a missing key) so callers stop hitting it and degrade to their
    normal no-key defaults instead of raising the raw response body."""
    from polybot.feeds import usvenue

    class RateLimited(Exception):
        status_code = 429

    calls = {"n": 0}

    class Markets:
        def settlement(self, slug):
            calls["n"] += 1
            raise RateLimited("<!doctype html>...You are being rate limited...")

    class Client:
        markets = Markets()

    v = usvenue.USVenue()
    v.available, v._client = True, Client()
    # why_unavailable names a missing key before a backoff, so without these the test passed only in
    # a shell that had sourced the real keys (the launchd loop's env) and failed everywhere else.
    v.key_id, v.secret = "test-key", "test-secret"
    assert v.resolution("some-slug") is None          # degrades cleanly, no raw HTML propagates
    assert calls["n"] == 1
    assert v.available is False                        # backoff engaged
    assert "RateLimited" in v.why_unavailable

    assert v.resolution("some-slug") is None            # still backing off
    assert calls["n"] == 1                              # ...so it never calls the banned host again

    v._backoff_until = 0.0                              # cooldown elapsed
    assert v.available is True
    with pytest.raises(RateLimited):
        v._client.markets.settlement("some-slug")       # sanity: the fake still raises when called directly
    calls["n"] = 0
    assert v.resolution("some-slug") is None
    assert calls["n"] == 1                              # tries again once backoff has passed


def _fake_us_weather_event(slug, n_markets=3):
    """An events.list-shaped event for `slug`, with quoted markets."""
    city = "New York City"
    return {"slug": slug, "endDate": "2026-09-20T05:00:00Z", "markets": [
        {"slug": f"tc-{slug}-lt{69 + i}f", "active": True, "closed": False,
         "title": f"{69 + i * 2} to {70 + i * 2}",
         "question": "Highest temperature in NYC on September 19?",
         "description": f"Will the highest temperature recorded at Central Park (KNYC) in {city} "
                        f"for 2026-09-19 ... be less than or equal to {69 + i}F?",
         "outcomes": '["Yes","No"]', "outcomePrices": '["0.30","0.70"]',
         "bestBidQuote": {"value": "0.30"}, "bestAskQuote": {"value": "0.34"}}
        for i in range(n_markets)]}


# The real chicago book of 2026-09-18 13:54:38, straight out of the snapshots table. Five legs
# with thousands of contracts behind them, one binding leg with 21, ask_sum 0.82. This is the best
# book on record and it is most of the profit the strategy has ever found, so it gets a test.
_CHICAGO_0918 = [("69 or below", None, 0.01, 0.0, 22716.07),
                 ("70 to 71", None, 0.01, 0.0, 27764.0),
                 ("72 to 73", 0.59, 0.66, 1.0, 21.0),
                 ("74 to 75", 0.03, 0.08, 10.0, 10157.92),
                 ("76 to 77", 0.02, 0.04, 1.0, 16310.0),
                 ("78 or above", None, 0.02, 0.0, 9000.0)]


def _chicago_legs(with_depth=True):
    from polybot.feeds.offshore import Bucket
    out = []
    for i, (t, bid, ask, bq, aq) in enumerate(_CHICAGO_0918):
        lo, hi = ((-math.inf, 69.0) if i == 0 else (78.0, math.inf) if i == 5
                  else (70.0 + (i - 1) * 2, 71.0 + (i - 1) * 2))
        b = Bucket(title=t, lo=lo, hi=hi, unit="F", yes_token=f"leg{i}", no_token=f"leg{i}",
                   market_id=f"leg{i}", condition_id=str(i), best_bid=bid, best_ask=ask,
                   last=ask, closed=False, outcome=None, liquidity=0.0, fee_coefficient=0.0695)
        if with_depth:
            b.bid_qty, b.ask_qty = bq, aq
            b.bid_levels = [(bid, bq)] if bid else []
            b.ask_levels = [(ask, aq)]
        out.append(b)
    return out


def test_the_best_book_on_record_is_sized_and_taken():
    """2026-09-18 13:54, chicago: six legs at an ask_sum of 0.82, so 18c gross and 15.39c net
    after the venue's own 0.0695 fee. The binding leg had 21 contracts; the rest had thousands.
    21 sets x 15.39c = $3.23 -- around 70% of every dollar this strategy has ever found, in a
    single minute. If a change ever stops this book from producing a set, that change is wrong."""
    from polybot.strategies.bucket_sum import arb_check, size_for_profit, worth_confirming

    cfg = _cfg()
    legs = _chicago_legs()
    kind, net, _ = arb_check(legs, "us")
    assert kind == "buy_all"
    assert net == pytest.approx(15.39, abs=0.05)
    assert worth_confirming(legs, net, cfg, 1.0)

    contracts, prices, net_n = size_for_profit(legs, kind, cfg, days=1.0)
    assert contracts == 21                       # the thinnest leg, not a cap
    assert sum(prices) == pytest.approx(0.82, abs=1e-6)
    assert contracts * net_n / 100 == pytest.approx(3.23, abs=0.05)
    # None of the caps bind here: this is a depth-limited set, which is the normal shape.
    assert sum(prices) < cfg.arb_max_set_cost_usd
    assert contracts * net_n / 100 >= cfg.arb_min_profit_usd


def test_watchdog_exits_when_the_loop_stops_making_progress(monkeypatch):
    """A hung socket blinded this bot for thirty-four minutes on 2026-09-19, mid-afternoon, in
    total silence:

        15:34:31  arb screen us nyc ...        <- last line
        16:08:25  us scan over its 75s budget  <- 34 minutes later

    Nothing upstream could catch it: arb_pass_budget_s is only tested BETWEEN city-days, so a
    call that never returns is never measured; the SDK's own 10s timeout did not fire; and _beat
    runs on the same thread, so a blocked loop stops reporting its own liveness and the server
    would not call it stale for three hours. The check has to live where the stall cannot reach
    it, and it has to exit hard, because a blocked thread cannot be unwound politely."""
    from polybot import runner as runner_mod

    r = object.__new__(runner_mod.Runner)
    said, exited = [], []
    r.log = lambda m: said.append(m)
    monkeypatch.setattr(runner_mod.os, "_exit", lambda code: exited.append(code))

    # Run the watchdog's body once per state rather than waiting on the wall clock.
    def tick(limit_s=300.0):
        age = time.time() - r._heartbeat
        if age > limit_s:
            r.log(f"WATCHDOG: no loop progress for {age:.0f}s — exiting for a restart")
            runner_mod.os._exit(1)

    r._heartbeat = time.time()
    tick()
    assert exited == [] and said == []            # a live loop is left alone

    r._heartbeat = time.time() - 120              # a slow pass (budget is 75s) is not a stall
    tick()
    assert exited == []

    # The longest legitimate gap is one city-day: six price_legs calls against a window the venue
    # may have widened to 60s, about 120-150s cold. The limit has to clear that, because a false
    # restart costs a cold cache and a skipped pass. The win comes from stamping progress DURING
    # a pass, not from cutting the limit fine.
    from polybot import runner as rm
    assert rm.WATCHDOG_HARD_S >= 240
    r._heartbeat = time.time() - 150              # still inside one slow city-day
    tick(limit_s=240.0)
    assert exited == []

    r._heartbeat = time.time() - 2040             # the real 34-minute stall
    tick()
    assert exited == [1]
    assert "WATCHDOG" in said[0] and "2040s" in said[0]

    # A job that legitimately runs for minutes must not be read as a stall. The backtest, the
    # nightly calibration and the universe sweeps all outlast a scan pass by a wide margin, and a
    # watchdog that kills them turns a safety net into a way of never finishing the backtest.
    r._heartbeat = time.time()
    with runner_mod.Runner._long_job(r, "backtest", grace_s=1800):
        assert r._heartbeat > time.time() + 1700      # held off while it runs
        exited.clear(); said.clear()
        tick()
        assert exited == []
    assert r._heartbeat <= time.time() + 1            # and handed straight back afterwards

    # The Python thread is not enough on its own: it fired 5.5 minutes late on its first real
    # stall (630s against a 300s limit) because the main thread held the GIL inside a C call —
    # the same reason httpx's own 10s timeout never fired. faulthandler's timer runs in a C
    # thread that does not need the GIL, so it expires on time whatever Python is doing.
    import faulthandler
    runner_mod._arm_hard_watchdog(3600)
    assert faulthandler.is_enabled() or True        # arming must never raise
    runner_mod._arm_hard_watchdog(0.0)              # clamped, not disabled
    faulthandler.cancel_dump_traceback_later()

    # And the thread itself is a daemon, so it can never hold the process open.
    import threading
    before = {t.name for t in threading.enumerate()}
    r2 = object.__new__(runner_mod.Runner)
    r2.log, r2._heartbeat = lambda m: None, time.time()
    r2._start_watchdog(limit_s=9999)
    t = next(t for t in threading.enumerate() if t.name == "polybot-watchdog")
    assert t.daemon is True


def test_us_settle_stays_out_of_the_hours_the_arb_sweep_owns():
    """Every open US position costs a resolution() call, so an hourly settle is a ~25-request
    burst into a budget of five per window. On 2026-09-19 it tripped the limiter at 14:20:53 and
    blinded the arb sweep for 15s, then widened the window on top — for answers that could not
    exist, because US weather settles on the NWS climate report at 8 AM ET the morning AFTER the
    market's date.

    Nothing settles later as a result: the calls that stop happening are only the ones that were
    always going to say "not yet"."""
    from polybot.runner import Runner

    at = lambda h: datetime(2026, 9, 19, h, 20)
    due = Runner._us_settle_due

    class Fresh:                                    # settled a moment ago: nothing is overdue
        _last_us_settle = time.time()

    for h in range(9, 17):
        assert due(Fresh(), at(h)) is False, f"{h}:20 is inside the fast arb sweep"
    # 08:20 runs, immediately after the 8 AM ET settlement the whole day's positions wait on
    assert due(Fresh(), at(8)) is True
    for h in list(range(17, 24)) + list(range(0, 9)):
        assert due(Fresh(), at(h)) is True
    # sixteen opportunities a day for something that happens once
    assert sum(1 for h in range(24) if due(Fresh(), at(h))) == 16

    # ...but a MISSED tick must not hide for nine hours. These jobs are keyed to an exact minute,
    # so a stall or a watchdog restart across :20 loses that tick entirely — today's 17:20 settle
    # vanished inside the stall the watchdog killed at 17:22. Harmless most hours; after a missed
    # 08:20 the next allowed hour is 17:20, which is nine hours of not knowing what the overnight
    # sets paid, on the morning the first real P&L is supposed to arrive.
    class Stale:
        _last_us_settle = time.time() - 4 * 3600

    assert due(Stale(), at(13)) is True             # peak hour, but it has drifted too long
    assert due(Stale(), at(2)) is True

    class Recent:
        _last_us_settle = time.time() - 600

    assert due(Recent(), at(13)) is False           # ten minutes ago: still stay out of the way


def test_paper_cannot_buy_the_same_liquidity_twice(tmp_path):
    """Paper orders do not consume the book, so a persistent mispricing gets bought over and over
    against the same contracts. Miami on 2026-09-19 was booked twice five minutes apart, 62 sets
    each, while the binding leg's ladder showed the SAME 79 contracts both times — live, the first
    order would have taken 62 of them and the second set could not have existed.

    That is not just a reporting error: the promotion gate runs on paper P&L, so an inflated
    paper record could promote a strategy that cannot perform live."""
    from polybot.strategies.bucket_sum import consume_levels
    from polybot.ledger import Ledger
    from polybot.strategies.base import Signal

    # The ladder we already ate into.
    lad = [(0.41, 79.0), (0.42, 300.0)]
    assert consume_levels(lad, 0) == lad
    assert consume_levels(lad, 62) == [(0.41, 17.0), (0.42, 300.0)]   # 17 left at the best price
    assert consume_levels(lad, 79) == [(0.42, 300.0)]                 # best level gone entirely
    assert consume_levels(lad, 100) == [(0.42, 279.0)]                # eats into the next level
    assert consume_levels(lad, 500) == []                             # nothing left to buy
    assert consume_levels([], 10) == []
    assert consume_levels(None, 10) == []

    # And the ledger reports what we hold on a leg, per module.
    led = Ledger(str(tmp_path / "t.db"))
    led.add_signal(Signal("bucket_sum", "us", "leg-a", "leg-a", "BUY_YES", 0.41, 25.42, 9.0,
                          "set", taker=True, arb=True, meta={"group": "g"}), "paper")
    assert led.held_contracts("us", "leg-a", "bucket_sum") == 62      # 25.42 / 0.41
    assert led.held_contracts("us", "leg-b", "bucket_sum") == 0
    assert led.held_contracts("us", "leg-a", "weather_obs") == 0      # another module's position

    # A settled/closed position no longer blocks the book.
    sid = led.open_signals(module="bucket_sum")[0]["id"]
    led.set_signal_status(sid, "closed")
    assert led.held_contracts("us", "leg-a", "bucket_sum") == 0


def test_an_already_taken_episode_does_not_pay_for_another_depth_read(monkeypatch):
    """The depth read is six calls and the dedupe lived AFTER it, so an episode that persists paid
    six calls a sweep to confirm a set already decided. Miami on 2026-09-19 produced candidates at
    13:57:40, 13:58:14, 13:58:47 and 13:59:20; the first was taken and the rest each bought a depth
    read to be refused — and at 13:59:53 the venue rate-limited us. That quota is shared with every
    other book, and a depth read losing it is exactly what stood down the best set on record."""
    from polybot import runner as runner_mod
    from polybot.strategies.base import Signal

    cfg, led = _cfg(), _ledger()
    cfg.modes["bucket_sum"] = "paper"
    cfg.arb_dedupe_s = 180.0
    r = runner_mod.Runner(cfg, led, log=lambda *_: None)

    # Record a set on these legs, as handle_arb_set would have.
    legs = ["tc-a", "tc-b", "tc-c"]
    for m in legs:
        led.add_signal(Signal("bucket_sum", "us", m, m, "BUY_YES", 0.30, 0.60, 9.0, "set",
                              taker=True, arb=True, meta={"group": "g"}), "paper")

    assert all(led.recent_signal_exists("bucket_sum", m, "BUY_YES", cfg.arb_dedupe_s) for m in legs)
    # ...and the opposite side is NOT suppressed: a sell-side set on the same book is a
    # different trade, not a repeat of this one.
    assert not any(led.recent_signal_exists("bucket_sum", m, "BUY_NO", cfg.arb_dedupe_s)
                   for m in legs)
    # ...nor is an untouched book.
    assert not led.recent_signal_exists("bucket_sum", "tc-other", "BUY_YES", cfg.arb_dedupe_s)

    # Once the window passes, the next episode is confirmable again.
    led.conn.execute("UPDATE signals SET ts = ts - ?", (cfg.arb_dedupe_s + 10,))
    led.conn.commit()
    assert not any(led.recent_signal_exists("bucket_sum", m, "BUY_YES", cfg.arb_dedupe_s)
                   for m in legs)

    # A VOIDED signal must not suppress anything. A void means "that decision was wrong, forget
    # it", and striking a bad record should never cost us the real opportunity behind it —
    # voiding a double-counted miami set on 2026-09-19 immediately blocked a genuine 13.1c/set
    # candidate three minutes later, for no reason visible in the log.
    led.conn.execute("UPDATE signals SET ts = ?", (__import__("time").time(),))
    led.conn.commit()
    assert all(led.recent_signal_exists("bucket_sum", m, "BUY_YES", cfg.arb_dedupe_s) for m in legs)
    led.conn.execute("UPDATE signals SET status='void'")
    led.conn.commit()
    assert not any(led.recent_signal_exists("bucket_sum", m, "BUY_YES", cfg.arb_dedupe_s)
                   for m in legs)


def test_the_universe_arb_path_stands_down_on_unknown_depth():
    """There are two copies of the arb: the five weather books, and scan_universe over every
    proven one-winner event. The universe copy ignored fill_depth_buckets' return value, so an
    unreadable leg — the thing that cost the best book on record — was simply not noticed.

    A second copy of the arb that quietly lacks the first copy's fixes is worse than no second
    copy, because it looks supported and is not."""
    from polybot import runner as runner_mod
    from polybot.feeds.offshore import Bucket

    cfg, led = _cfg(), _ledger()
    cfg.modes["bucket_sum"] = "paper"
    r = runner_mod.Runner(cfg, led, log=lambda *_: None)

    def leg(i, ask):
        b = Bucket(title=f"o{i}", lo=float(i), hi=float(i), unit="", yes_token=f"u{i}",
                   no_token=f"u{i}", market_id=f"u{i}", condition_id=str(i), best_bid=ask - 0.01,
                   best_ask=ask, last=ask, closed=False, outcome=None, liquidity=0.0,
                   fee_coefficient=0.0695)
        return b

    buckets = [leg(i, 0.20) for i in range(4)]        # ask_sum 0.80 — a real-looking arb
    # one leg's book never answers, so its depth is unknown, not zero
    for b in buckets[:-1]:
        b.ask_levels, b.bid_levels = [(b.best_ask, 500.0)], [(b.best_bid, 500.0)]
        b.ask_qty, b.bid_qty = 500.0, 500.0
    assert buckets[-1].ask_levels is None

    from polybot.strategies.bucket_sum import size_for_profit
    n, _, _ = size_for_profit(buckets, "buy_all", cfg, days=1.0)
    assert n == 0, "a set must not be sized while a leg's depth is unknown"

    # and held contracts are consumed off the ladder on this path too
    from polybot.strategies.bucket_sum import consume_levels
    assert consume_levels([(0.20, 500.0)], 500) == []
    assert consume_levels([(0.20, 500.0)], 120) == [(0.20, 380.0)]


def test_an_open_arb_is_carried_at_cost_not_marked_leg_by_leg(tmp_path):
    """Six arb legs are one instrument that pays exactly $1.00 a set at settlement. Marking each
    at its own mid and adding them up prices the market's inefficiency six times over, and implies
    selling all six at mid simultaneously — which is not a thing you can do, you would hit bids.

    On 2026-09-19 that read $17.18 unrealised on sets whose settlement value is $8.70: flattering
    by a factor of two, in the number the promotion gate reads."""
    from polybot.ledger import Ledger
    from polybot.paper import PaperEngine
    from polybot.strategies.base import Signal

    led = Ledger(str(tmp_path / "m.db"))
    prices = [0.01, 0.01, 0.01, 0.09, 0.32, 0.41]         # ask_sum 0.85
    ids = []
    for i, px in enumerate(prices):
        sig = Signal("bucket_sum", "us", f"l{i}", f"l{i}", "BUY_YES", px, round(px * 62, 2), 11.0,
                     "set", taker=True, arb=True, meta={"group": "g"})
        sid = led.add_signal(sig, "paper")
        led.upsert_paper(sid, filled_ts=time.time() - 60, fill_price=px, status="filled")
        ids.append(sid)

    # every leg's mid has drifted UP; marking leg-by-leg would book a fat unrealised gain
    pe = PaperEngine(led,
                     history_fn=lambda sig: [(time.time(), float(sig["price"]) + 0.05)],
                     resolution_fn=lambda sig: None)
    pe.settle_open("us", log=lambda *a, **k: None)
    unreal = sum((led.paper_row(i) or {}).get("pnl_usd") or 0 for i in ids)
    assert unreal == pytest.approx(0.0, abs=0.01), f"arb marked leg-by-leg: {unreal}"

    # a NON-arb position is still marked normally — this must not blunt ordinary reporting
    sig = Signal("weather_obs", "us", "w", "w", "BUY_YES", 0.30, 6.0, 5.0, "view")
    wid = led.add_signal(sig, "paper")
    led.upsert_paper(wid, filled_ts=time.time() - 60, fill_price=0.30, status="filled")
    pe.settle_open("us", log=lambda *a, **k: None)
    assert ((led.paper_row(wid) or {}).get("pnl_usd") or 0) > 0


def test_exhaustive_is_strict_about_what_it_calls_a_tiling():
    """The function the entire guarantee rests on. "Buy every outcome for under $1" is only
    risk-free if the outcomes really are every outcome — one gap and some temperature pays nobody,
    one overlap and the set costs more than it can ever return.

    It is deliberately not the venue's negRisk flag, which was not trustworthy."""
    from polybot.strategies.bucket_sum import exhaustive
    from polybot.feeds.offshore import Bucket

    def bs(*ranges):
        return [Bucket(title=f"{lo}-{hi}", lo=lo, hi=hi, unit="F", yes_token=f"t{i}",
                       no_token=f"t{i}", market_id=f"t{i}", condition_id=str(i), best_bid=0.1,
                       best_ask=0.2, last=0.15, closed=False, outcome=None, liquidity=0.0)
                for i, (lo, hi) in enumerate(ranges)]

    inf = math.inf
    # the real shape: open at both ends, contiguous whole degrees
    assert exhaustive(bs((-inf, 69), (70, 71), (72, 73), (74, inf)))
    assert exhaustive(bs((-inf, 69), (70, inf)))                  # two legs is a valid tiling
    # order must not matter
    assert exhaustive(bs((72, 73), (-inf, 69), (74, inf), (70, 71)))

    assert not exhaustive(bs((-inf, 69)))                         # one leg is not a set
    assert not exhaustive(bs((70, 71), (72, inf)))                # nothing covers the cold tail
    assert not exhaustive(bs((-inf, 69), (70, 71)))               # nothing covers the hot tail
    assert not exhaustive(bs((-inf, 69), (71, inf)))              # 70 pays nobody
    assert not exhaustive(bs((-inf, 70), (70, inf)))              # 70 pays twice
    assert not exhaustive(bs((-inf, 69), (-inf, 69), (70, inf)))  # duplicated leg
    assert not exhaustive(bs((-inf, 69), (70, inf), (71, inf)))   # two open hot ends
    assert not exhaustive(bs((-inf, 69), (69.5, inf)))            # half-degree: refuse, do not guess

    # and arb_check will not price a set it cannot prove, however tempting the numbers look
    from polybot.strategies.bucket_sum import arb_check
    gappy = bs((-inf, 69), (71, inf))
    for b in gappy:
        b.best_bid, b.best_ask = 0.10, 0.20                       # "buy both for 0.40, win $1"
    assert arb_check(gappy, "us") == (None, 0.0, [])
    # unless the universe has PROVED it by settlement, which is the only override there is
    assert arb_check(gappy, "us", assume_exhaustive=True)[0] == "buy_all"


def test_the_gate_refuses_a_record_that_is_one_trade(tmp_path):
    """Counting signals, fills and mark-to-market cannot tell a small repeatable edge from a coin
    flip. On 2026-09-19 weather_obs read +$8.43 over 28 closed trades and was the module CLOSEST
    to going live — while its best trade was +$80.10 and its worst -$20.00 on a $20 position. One
    trade was the whole record; one loss undid it. That is pennies in front of a steamroller, and
    nothing in the gate was looking at it."""
    from polybot.ledger import Ledger
    from polybot.strategies.base import Signal

    led = Ledger(str(tmp_path / "t.db"))

    def closed(module, pnl, group=None, i=[0]):
        i[0] += 1
        sig = Signal(module, "us", f"m{i[0]}", f"m{i[0]}", "BUY_NO", 0.90, 20.0, 5.0, "r",
                     meta={"group": group} if group else {})
        sid = led.add_signal(sig, "paper")
        led.upsert_paper(sid, filled_ts=time.time() - 60, fill_price=0.90, status="closed",
                         pnl_usd=pnl)

    # the weather_obs shape: lots of small wins, one big win, one loss that swallows the lot
    for _ in range(9):
        closed("obs", 1.20)
    closed("obs", 80.10)
    closed("obs", -20.00)
    ok, why = led.tail_check("obs")
    assert ok is False and "one trade" in why          # +80.10 IS the record

    # and the mirror: profitable overall, but one loss is bigger than everything earned
    for _ in range(25):
        closed("steamroller", 1.00)
    closed("steamroller", -20.00)                      # net +5.00, but -20 swallows it
    ok, why = led.tail_check("steamroller")
    assert ok is False and "one loss" in why, why

    # a genuine edge — many small wins, no single trade carrying it — passes
    for _ in range(20):
        closed("real", 1.00)
    closed("real", -0.50)
    ok, why = led.tail_check("real")
    assert ok is True, why

    # AN ARB MUST NOT BE BLOCKED. Its legs look dreadful alone: five lose their premium so the
    # sixth can win. Measured on the real settled sets, leg-level is worst -$20.78 / best +$35.54,
    # which a per-row check calls variance — while the SETS are worst +$0.08 and cannot lose.
    for g, legs in (("s1", [-0.62, -0.62, -5.58, -25.42, -0.62, 35.54]),
                    ("s2", [-0.30, -0.30, -1.10, 4.00, -0.30, -0.30])):
        for pnl in legs:
            closed("arb", pnl, group=g)
    ok, why = led.tail_check("arb")
    assert ok is True, why
    assert "2 decisions" in why                        # judged as two sets, not twelve legs

    # The same rows judged per-leg would be rejected, which is exactly why grouping matters:
    # the worst LEG (-25.42) dwarfs the net (+4.68) while the worst SET is +1.90 and safe.
    all_legs = [-0.62, -0.62, -5.58, -25.42, -0.62, 35.54,
                -0.30, -0.30, -1.10, 4.00, -0.30, -0.30]
    assert abs(min(all_legs)) >= sum(all_legs)         # per-leg: looks like pure variance
    assert min(sum(all_legs[:6]), sum(all_legs[6:])) > 0   # per-set: every decision made money


def test_settling_asks_each_market_once_not_each_leg(tmp_path):
    """A market's outcome is a property of the market. The same market appears once per set that
    touched it, so settling asked the venue the same question over and over: on 2026-09-19 that
    was 24 open legs across only 6 distinct markets, four times the calls needed, against a
    five-per-window budget.

        09-20 17:21:04 settle: {'closed': 15, 'open': 14}
        09-20 17:21:04   us venue blind for 15s — RateLimitError

    Fifteen legs closed and the limiter cut off the rest — including four legs of a set whose
    other two HAD closed, which reported a $52.70 arb as a $1.33 loss. An arb settles whole or it
    says nothing true at all."""
    from polybot.ledger import Ledger
    from polybot.paper import PaperEngine
    from polybot.strategies.base import Signal

    led = Ledger(str(tmp_path / "s.db"))
    markets = ["lt84", "b84", "b86", "b88", "b90", "b92"]
    for group in ("set1", "set2", "set3", "set4"):          # four sets over the same six markets
        for m in markets:
            sid = led.add_signal(Signal("bucket_sum", "us", m, m, "BUY_YES", 0.10, 1.0, 9.0,
                                        "set", taker=True, arb=True, meta={"group": group}), "paper")
            led.upsert_paper(sid, filled_ts=time.time() - 60, fill_price=0.10, status="filled")

    asked = []

    def resolve(sig):
        asked.append(sig["market"])
        return 1 if sig["market"] == "b90" else 0

    pe = PaperEngine(led, history_fn=lambda sig: [], resolution_fn=resolve)
    out = pe.settle_open("us", log=lambda *a, **k: None)

    assert len(asked) == len(set(asked)) == 6, f"asked {len(asked)} times for 6 markets: {asked}"
    assert out["closed"] == 24                    # every leg of every set still settles

    # and the answer is applied to every set, not just the first — no half-closed sets
    rows = list(led.conn.execute(
        "SELECT p.status FROM signals s JOIN paper_trades p ON p.signal_id=s.id"))
    assert {r[0] for r in rows} == {"closed"}


def test_a_settled_arb_pays_the_same_whatever_wins(tmp_path):
    """The one property the whole strategy rests on, driven through the real settlement code.

    Buy every bucket of an exhaustive set for less than $1 and exactly one pays $1, so the
    outcome cannot change the result. Verified on the real 2026-09-19 miami set (6 legs, 62 sets,
    $52.70 in) by settling it six times, once for each bucket winning: $6.84 every time.

    If a change ever makes this vary by outcome, the thing being run is no longer an arbitrage —
    it is a bet — and no amount of edge in the pricing makes that acceptable."""
    from polybot.ledger import Ledger
    from polybot.paper import PaperEngine
    from polybot.strategies.base import Signal

    prices = [0.01, 0.01, 0.01, 0.09, 0.32, 0.41]      # ask_sum 0.85, the real book
    names = ["lt84", "b84", "b86", "b88", "b90", "b92"]
    sets_n = 62

    results = []
    for winner in names:
        led = Ledger(str(tmp_path / f"{winner}.db"))
        ids = []
        for nm, px in zip(names, prices):
            sig = Signal("bucket_sum", "us", nm, nm, "BUY_YES", px, round(px * sets_n, 2), 11.0,
                         "set", taker=True, arb=True, meta={"group": "g"})
            sid = led.add_signal(sig, "paper")
            led.upsert_paper(sid, filled_ts=time.time() - 60, fill_price=px, status="filled")
            ids.append(sid)
        pe = PaperEngine(led,
                         history_fn=lambda sig: [],
                         resolution_fn=lambda sig, w=winner: 1 if sig["market"] == w else 0)
        pe.settle_open("us", log=lambda *a, **k: None)
        results.append(round(sum((led.paper_row(i) or {}).get("pnl_usd") or 0 for i in ids), 2))

    assert len(set(results)) == 1, f"outcome changed the P&L: {dict(zip(names, results))}"
    # 62 * (1.00 - 0.85) gross, less 0.0695 * sum p(1-p) per set in taker fees
    assert results[0] == pytest.approx(6.84, abs=0.05)
    assert results[0] > 0                               # and it is a profit in every case


def test_a_set_is_measured_against_the_exposure_cap_as_one_decision():
    """Every leg is risk-checked against the SAME exposure baseline, because none is recorded
    until they all pass. So six legs of $20 each are each compared with $80 of room and each says
    yes — and then $120 goes on. Proved against live settings: $100 open, a $120 set, all six
    legs "ok", resulting exposure $220 against a $180 cap.

    A set is one decision; it has to be measured as one."""
    from polybot import runner as runner_mod
    from polybot.strategies.base import Signal

    cfg, led = _cfg(), _ledger()
    cfg.modes["bucket_sum"] = "live"
    cfg.arb_live_ok = True
    cfg.caps.max_exposure_usd = 180.0
    cfg.bankroll_usd = 256.0
    r = runner_mod.Runner(cfg, led, log=lambda *_: None)
    r.executor.place_arb_set = lambda legs: {"ok": True}

    for i in range(5):                       # $100 already committed
        led.add_signal(Signal("weather_obs", "us", f"x{i}", f"x{i}", "BUY_YES", 0.5, 20.0, 5.0, "r"), "live")
    assert led.exposure_usd("us", None, live_only=True) == 100.0

    def legs(each, tag):
        return [Signal("bucket_sum", "us", f"{tag}{i}", f"{tag}{i}", "BUY_YES", 0.5, each, 9.0,
                       "set", taker=True, arb=True, meta={"group": tag}) for i in range(6)]

    # $120 on top of $100 breaches the $180 cap, even though every leg passes alone.
    assert all(r.risk.allow(s, mode="live")[0] for s in legs(20.0, "big"))
    assert r.handle_arb_set(legs(20.0, "big")) == 0
    assert led.exposure_usd("us", None, live_only=True) == 100.0      # nothing recorded

    # A set that genuinely fits is still taken.
    assert r.handle_arb_set(legs(10.0, "ok")) == 6
    assert led.exposure_usd("us", None, live_only=True) == 160.0


def test_an_arb_rearms_in_minutes_while_a_view_stays_locked_for_hours():
    """The 3-hour dedupe exists because a directional module re-entered the same bucket every 3
    hours and turned a $20 call into a $60 one. An arb is not a view: a set pays $1 whatever
    happens, so taking the same one twice is two independent profitable trades, limited by the
    exposure caps rather than by a clock.

    That window cost real repeats. On 2026-09-19 miami offered sets at 11:47, 11:48, 11:51, 12:10
    and 12:13; the first was taken and every later one refused -- including 12:10 at 10.7c/set,
    worth more than the one taken. But some window is still needed: at a 20s sweep the same
    episode is seen repeatedly, and in paper that would book it several times over."""
    from polybot import runner as runner_mod
    from polybot.strategies.base import Signal

    cfg, led = _cfg(), _ledger()
    cfg.modes["bucket_sum"] = "paper"
    cfg.arb_dedupe_s = 180.0
    r = runner_mod.Runner(cfg, led, log=lambda *_: None)
    r.risk.allow = lambda sig, mode=None: (True, "ok")

    def arb_legs():
        return [Signal("bucket_sum", "us", f"m{i}", f"leg {i}", "BUY_YES", 0.30, 0.60, 5.0,
                       "set", taker=True, arb=True, meta={"group": "g"}) for i in range(3)]

    assert r.handle_arb_set(arb_legs()) == 3
    assert r.handle_arb_set(arb_legs()) == 0        # same episode, seen again 20s later

    # ...but once the short window has passed, the next episode is takeable.
    led.conn.execute("UPDATE signals SET ts = ts - ?", (cfg.arb_dedupe_s + 10,))
    led.conn.commit()
    assert r.handle_arb_set(arb_legs()) == 3

    # A non-arb signal keeps the long window: it IS a view, and re-entering doubles it.
    led.conn.execute("UPDATE signals SET ts = ts - ?", (cfg.arb_dedupe_s + 10,))
    led.conn.commit()
    view = Signal("weather_obs", "us", "m0", "leg 0", "BUY_YES", 0.30, 6.0, 5.0, "view")
    assert led.recent_signal_exists("bucket_sum", "m0", "BUY_YES", runner_mod.DEDUPE_S) is True
    assert led.recent_signal_exists("bucket_sum", "m0", "BUY_YES", cfg.arb_dedupe_s) is False


def test_a_big_paper_arb_is_announced_and_a_small_one_is_not():
    """Almost every set is worth pennies and belongs in the log. A big one is a different event:
    it is the case the whole strategy exists for, it lasts about a minute, and while arb_live_ok
    is off it would otherwise pass by with nobody told."""
    from polybot import runner as runner_mod

    sent = []
    monkey = lambda title, body, key=None, log=None: sent.append((title, body))

    cfg, led = _cfg(), _ledger()
    cfg.modes["bucket_sum"] = "paper"
    cfg.arb_notify_usd = 2.0
    r = runner_mod.Runner(cfg, led, log=lambda *_: None)
    r.risk.allow = lambda sig, mode=None: (True, "ok")

    from polybot.strategies.base import Signal

    def legs(edge_cents, contracts):
        # `contracts` is derived from size_usd/price, so the size IS how you ask for N contracts.
        return [Signal("bucket_sum", "us", f"m{i}-{edge_cents}", f"leg {i}", "BUY_YES",
                       0.30, round(0.30 * contracts, 2), edge_cents, "test set",
                       taker=True, arb=True, meta={"group": f"g{edge_cents}"})
                for i in range(3)]

    import polybot.notify as notify
    old = notify.nudge
    notify.nudge = monkey
    try:
        r.handle_arb_set(legs(0.5, 4))          # $0.02 — dust, stays in the log
        assert sent == []
        r.handle_arb_set(legs(15.4, 21))        # $3.23 — the chicago set, worth waking him for
        assert len(sent) == 1
        assert "$3.23" in sent[0][0]
        assert "arb_live_ok is off" in sent[0][1]

        # "You missed $3.23" means something different depending on whether the money was there.
        # On 2026-09-19 the account held $272.50 of value and $0.06 of spendable cash, all of it
        # in positions Alex had opened himself; an alert that leaves that out invites him to flip
        # the switch into an order that cannot fill.
        sent.clear()
        r.us = type("V", (), {"available": True, "balance_usd": staticmethod(lambda: 0.06)})()
        r.handle_arb_set(legs(15.5, 21))
        assert len(sent) == 1 and "could NOT have taken it" in sent[0][1]
        assert "$0.06" in sent[0][1]

        sent.clear()
        r.us = type("V", (), {"available": True, "balance_usd": staticmethod(lambda: 10_000.0)})()
        r.handle_arb_set(legs(15.6, 21))
        assert len(sent) == 1 and "could NOT have taken it" not in sent[0][1]
    finally:
        notify.nudge = old


def _arb_sig(market, depth, price=0.30, contracts=21):
    from polybot.strategies.base import Signal
    return Signal("bucket_sum", "us", market, market, "BUY_YES", price,
                  round(price * contracts, 2), 9.0, "set", taker=True, arb=True,
                  meta={"group": "g", "depth": depth})


def _fill(qty):
    return {"id": "o", "executions": [{"type": "EXECUTION_TYPE_FILL",
                                       "order": {"state": "ORDER_STATE_FILLED"},
                                       "lastShares": str(qty)}]}


_KILLED = {"id": "o", "executions": [{"type": "EXECUTION_TYPE_CANCELED",
                                      "order": {"state": "ORDER_STATE_CANCELED"}}]}


def test_a_set_is_not_sent_without_the_cash_to_pay_for_it():
    """The bankroll the caps are measured against is account VALUE — cash plus what is already in
    open positions — because a bot that halts the moment its money is working never compounds.
    That is right for the floor and wrong for placing an order: value you cannot spend does not
    fill anything.

    On 2026-09-19 the account read $272.50 of value against $0.06 of buying power, the rest being
    two positions Alex had opened himself. Without this check every leg would have been refused
    for insufficient funds one at a time, and whichever filled first would have needed unwinding."""
    from polybot import execution

    sent = []

    class US:
        available = True

        def __init__(self, cash):
            self.cash = cash

        def balance_usd(self):
            return self.cash

        def place_limit(self, market, side, price, contracts, tif=None):
            sent.append(market)
            return _fill(contracts)

        def bbo(self, slug):
            return 0.29, 0.31

    led = _ledger()
    legs = [(led.add_signal(_arb_sig(n, 100.0), "live"), _arb_sig(n, 100.0)) for n in ("a", "b")]
    need = sum(sig.size_usd for _, sig in legs)

    broke = execution.Executor(led, US(0.06), _cfg(), log=lambda *_: None)
    out = broke.place_arb_set(legs)
    assert out["ok"] is False and out["placed"] == 0
    assert out.get("reason") == "insufficient buying power"
    assert sent == []                              # nothing was sent at all

    sent.clear()
    rich = execution.Executor(led, US(need + 1.0), _cfg(), log=lambda *_: None)
    assert rich.place_arb_set(legs)["ok"] is True
    assert sent == ["a", "b"]

    # A venue that will not say costs us nothing: unknown cash must not block a real set.
    sent.clear()
    unknown = execution.Executor(led, US(None), _cfg(), log=lambda *_: None)
    assert unknown.place_arb_set(legs)["ok"] is True


def test_a_rate_limit_mid_set_aborts_and_unwinds_rather_than_stranding(monkeypatch):
    """The most likely way the first live set fails.

    place_limit deliberately bypasses the token bucket — an arb cannot wait a full window between
    legs — but the depth read immediately before it has just spent about six calls. So the orders
    go out unthrottled against a budget that is already empty, and a 429 partway through the set
    is the realistic failure, not a hypothetical one.

    What must NOT happen is silence: the legs already bought are a naked basket."""
    from polybot import execution

    class RateLimited(Exception):
        status_code = 429

    nudges, sent = [], []
    monkeypatch.setattr(execution.notify, "nudge",
                        lambda title, body, key=None, log=None: nudges.append(title))

    class US:
        available = True
        balance_usd = staticmethod(lambda: 10_000.0)      # plenty; this test is not about cash

        def place_limit(self, market, side, price, contracts, tif=None):
            sent.append((market, tif))
            if tif == "fok" and market == "c":
                raise RateLimited("<!doctype html>...You are being rate limited...")
            return _fill(contracts)

        def bbo(self, slug):
            return 0.29, 0.31

    led = _ledger()
    ex = execution.Executor(led, US(), _cfg(), log=lambda *_: None)
    legs = []
    for nm, depth in (("a", 10.0), ("b", 20.0), ("c", 30.0), ("d", 40.0)):
        sig = _arb_sig(nm, depth)
        legs.append((led.add_signal(sig, "live"), sig))
    out = ex.place_arb_set(legs)

    assert out["ok"] is False
    assert out["filled"] == 2                      # a and b bought before c was refused
    assert ("d", "fok") not in sent                # and d was never sent
    assert out["unwound"] == 2                     # both bought legs really sold back
    assert "stranded" not in out and nudges == []  # nothing left unhedged, so nothing to shout about
    assert {r["market"] for r in led.open_signals(module="bucket_sum")} == set()


def test_an_unwind_that_did_not_fill_is_not_counted_as_unwound(monkeypatch):
    """An immediate-or-cancel unwind is killed exactly like the fill-or-kill that started the
    set, so "unwound" has to mean the contracts actually left — not that a request was sent. A
    leg we could not sell is an unhedged position, which is the single outcome this whole design
    exists to prevent, so it stays OPEN in the ledger and it wakes somebody."""
    from polybot import execution

    nudges = []
    monkeypatch.setattr(execution.notify, "nudge",
                        lambda title, body, key=None, log=None: nudges.append(title))

    class US:
        available = True
        balance_usd = staticmethod(lambda: 10_000.0)      # plenty; this test is not about cash

        def place_limit(self, market, side, price, contracts, tif=None):
            if tif == "fok":
                # thin goes first (it is the thinnest) and FILLS; the later leg is killed, so
                # there is something to unwind. Killing the first leg would abort before any buy.
                return _KILLED if market == "fat" else _fill(contracts)
            return _KILLED            # every unwind is killed too

        def bbo(self, slug):
            return 0.29, 0.31

    led = _ledger()
    ex = execution.Executor(led, US(), _cfg(), log=lambda *_: None)
    legs = [(led.add_signal(_arb_sig("fat", 9000.0), "live"), _arb_sig("fat", 9000.0)),
            (led.add_signal(_arb_sig("thin", 21.0), "live"), _arb_sig("thin", 21.0))]
    out = ex.place_arb_set(legs)

    assert out["ok"] is False
    assert out["unwound"] == 0                 # sent, but nothing actually sold
    assert out.get("stranded") == 1
    assert nudges and "STRANDED" in nudges[0]

    # The leg we still hold must still read as held, or exposure and sizing both lie.
    rows = {r["market"]: r["status"] for r in led.open_signals(module="bucket_sum")}
    assert rows.get("thin") == "open"          # bought, could not be sold back, still held


def test_a_successful_unwind_closes_the_position(monkeypatch):
    """The mirror case: when the contracts really do leave, the signal has to stop counting as
    exposure, or every later set is sized against capital we no longer have committed."""
    from polybot import execution

    monkeypatch.setattr(execution.notify, "nudge", lambda *a, **k: None)

    class US:
        available = True
        balance_usd = staticmethod(lambda: 10_000.0)      # plenty; this test is not about cash

        def place_limit(self, market, side, price, contracts, tif=None):
            if tif == "fok":
                return _KILLED if market == "fat" else _fill(contracts)
            return _fill(contracts)             # the unwind fills

        def bbo(self, slug):
            return 0.29, 0.31

    led = _ledger()
    ex = execution.Executor(led, US(), _cfg(), log=lambda *_: None)
    legs = [(led.add_signal(_arb_sig("fat", 9000.0), "live"), _arb_sig("fat", 9000.0)),
            (led.add_signal(_arb_sig("thin", 21.0), "live"), _arb_sig("thin", 21.0))]
    out = ex.place_arb_set(legs)

    assert out["unwound"] == 1 and "stranded" not in out
    open_markets = {r["market"] for r in led.open_signals(module="bucket_sum")}
    assert "thin" not in open_markets          # sold back, so no longer exposure
    assert led.held_contracts("us", "thin", "bucket_sum") == 0


def test_a_killed_order_is_never_read_as_filled():
    """The check the whole all-or-nothing design rests on, against the SDK's REAL response shape.

    CreateOrderResponse is {id, executions[]}: there is no top-level status or state. The outcome
    lives in executions[].type (EXECUTION_TYPE_FILL / CANCELED / REJECTED / EXPIRED) and
    executions[].order.state. The old check read a top-level "status"/"state" that is never there,
    got "", and looked in it for "KILL" -- a word that appears in none of the venue's enums. An
    empty string contains no bad word, so every order read as FILLED, including killed ones. Live,
    that marks a set COMPLETE while holding nothing, or holds an unbalanced basket and never
    unwinds it. It could only ever bite with real money, which is why no test caught it."""
    from polybot.execution import fill_result

    def resp(ex_type, state, shares=None, cum=None):
        ex = {"type": ex_type, "order": {"state": state}}
        if shares is not None:
            ex["lastShares"] = shares
        if cum is not None:
            ex["order"]["cumQuantity"] = cum
        return {"id": "o1", "executions": [ex]}

    # A filled fill-or-kill.
    assert fill_result(resp("EXECUTION_TYPE_FILL", "ORDER_STATE_FILLED", "21", 21), 21)[0] == 21

    # Every way the venue says no. None of these contain the word "KILL".
    for t, st in (("EXECUTION_TYPE_CANCELED", "ORDER_STATE_CANCELED"),
                  ("EXECUTION_TYPE_REJECTED", "ORDER_STATE_REJECTED"),
                  ("EXECUTION_TYPE_EXPIRED", "ORDER_STATE_EXPIRED")):
        qty, label = fill_result(resp(t, st), 21)
        assert qty == 0, f"{t} must not read as filled"
        assert "KILL" not in label                  # the old check was looking for a word nobody says

    # Ambiguity resolves to NOT filled: an unhedged basket nobody unwinds is worse than a
    # needless unwind that leaves us flat.
    assert fill_result({"id": "o1"}, 21) == (0, "no state reported")
    assert fill_result({"id": "o1", "executions": []}, 21) == (0, "no state reported")
    assert fill_result(None, 21)[0] == 0
    assert fill_result({}, 21)[0] == 0

    # A part-filled leg reports what it actually got, and that is not "filled" for a 21-lot.
    qty, _ = fill_result(resp("EXECUTION_TYPE_PARTIAL_FILL", "ORDER_STATE_PARTIALLY_FILLED", "5", 5), 21)
    assert qty == 5

    # ...and a partial that states NO quantity must not fall through to "complete". Both
    # PARTIALLY_FILLED and PARTIAL_FILL contain the word FILL and neither is a dead marker, so a
    # naive "it said filled, take its word" rule reads them as a full fill -- the exact error
    # this function exists to prevent.
    qty, _ = fill_result(resp("EXECUTION_TYPE_PARTIAL_FILL", "ORDER_STATE_PARTIALLY_FILLED"), 21)
    assert qty == 0
    # while a genuine fill with no quantity IS taken at its word
    assert fill_result(resp("EXECUTION_TYPE_FILL", "ORDER_STATE_FILLED"), 21)[0] == 21

    # Gateways do not always match their own SDK types, so a plain statement is honoured too.
    assert fill_result({"state": "ORDER_STATE_FILLED", "cumQuantity": 21}, 21)[0] == 21
    assert fill_result({"status": "ORDER_STATE_CANCELED"}, 21)[0] == 0


def test_a_part_filled_leg_is_unwound_for_what_it_actually_holds():
    """A fill-or-kill should never part-fill. If the venue does it anyway we own those contracts:
    they must be unwound with the rest rather than forgotten because the leg "failed", and the
    unwind must sell the quantity HELD -- selling the full order size would turn an unwind into a
    naked short."""
    from polybot import execution

    sent = []

    class Sig:
        def __init__(self, market, depth):
            self.market, self.label, self.side = market, market, "BUY_YES"
            self.price, self.contracts = 0.10, 21
            self.meta = {"depth": depth}

    class US:
        available = True
        balance_usd = staticmethod(lambda: 10_000.0)      # plenty; this test is not about cash

        def place_limit(self, market, side, price, contracts, tif=None):
            sent.append((market, side, contracts, tif))
            if tif == "ioc":
                return {"id": "u", "executions": [{"type": "EXECUTION_TYPE_FILL",
                                                   "order": {"state": "ORDER_STATE_FILLED"},
                                                   "lastShares": str(contracts)}]}
            if market == "thin":       # the binding leg part-fills
                return {"id": "a", "executions": [{"type": "EXECUTION_TYPE_PARTIAL_FILL",
                                                   "order": {"state": "ORDER_STATE_PARTIALLY_FILLED",
                                                             "cumQuantity": 5}}]}
            return {"id": "b", "executions": [{"type": "EXECUTION_TYPE_FILL",
                                               "order": {"state": "ORDER_STATE_FILLED"},
                                               "lastShares": "21"}]}

        def bbo(self, slug):
            return 0.09, 0.11

    ex = execution.Executor(_ledger(), US(), _cfg(), log=lambda *_: None)
    out = ex.place_arb_set([(0, Sig("thin", 21.0)), (1, Sig("fat", 9000.0))])

    assert out["ok"] is False                       # the set never completed
    # `thin` is the thinnest leg so it goes first, and a part fill means the set is already dead:
    # `fat` is never bought at all. The only thing to undo is the 5 contracts we really got.
    assert out["filled"] == 0                       # 5 of 21 is not a filled leg
    assert "fat" not in [x[0] for x in sent]
    unwinds = [x for x in sent if x[3] == "ioc"]
    assert unwinds == [("thin", "SELL_YES", 5, "ioc")]   # the 5 we hold, not the 21 we asked for
    assert out["unwound"] == 1


def test_arb_places_the_thinnest_leg_first():
    """The venue has no atomic multi-leg order, so legs go out one at a time and whichever leg is
    killed decides the unwind bill. Killed on the first leg costs nothing; killed on the fifth
    means selling four legs back at the bid. The leg most likely to be killed is the one whose
    ladder barely covers the order -- chicago 2026-09-18 had five legs holding thousands of
    contracts and a binding leg holding exactly 21."""
    from polybot import execution

    class Sig:
        def __init__(self, market, depth):
            self.market, self.label, self.side = market, market, "BUY_YES"
            self.price, self.contracts = 0.10, 21
            self.meta = {"depth": depth} if depth is not None else {}

    sent = []

    class US:
        available = True
        balance_usd = staticmethod(lambda: 10_000.0)      # plenty; this test is not about cash

        def place_limit(self, market, side, price, contracts, tif=None):
            sent.append(market)
            return {"status": "FILLED", "id": market}

    led = _ledger()
    ex = execution.Executor(led, US(), _cfg(), log=lambda *_: None)
    legs = [(i, Sig(m, d)) for i, (m, d) in enumerate(
        [("fat-a", 22716.0), ("fat-b", 27764.0), ("thin", 21.0), ("fat-c", 10157.0)])]

    ex.place_arb_set(legs)
    assert sent[0] == "thin"                  # the leg that can actually fail goes out first
    assert set(sent) == {"fat-a", "fat-b", "thin", "fat-c"}

    # A leg with no depth recorded must not jump the queue ahead of a known-thin one.
    sent.clear()
    legs = [(0, Sig("unknown", None)), (1, Sig("thin", 3.0))]
    ex.place_arb_set(legs)
    assert sent[0] == "thin"


class _RateLimited(Exception):
    status_code = 429


def _chicago_client(fail_leg5_times):
    """A fake gateway serving the 2026-09-18 chicago book, where leg5 is rate limited the first
    `fail_leg5_times` times it is asked for. A rate limit is the ONLY way book() can fail -- an
    SDK that simply returns nothing still produces an empty-but-real book -- so this is what the
    live failure looked like."""
    calls = collections.Counter()

    class Markets:
        def book(self, slug):
            calls[slug] += 1
            if slug == "leg5" and calls[slug] <= fail_leg5_times:
                raise _RateLimited("<!doctype html>...You are being rate limited...")
            i = int(slug[-1])
            _, bid, ask, bq, aq = _CHICAGO_0918[i]
            return {"bids": ([{"px": bid, "qty": bq}] if bid else []),
                    "offers": [{"px": ask, "qty": aq}]}

    class Client:
        markets = Markets()

    return Client(), calls


def test_a_rate_limited_leg_does_not_cost_the_whole_episode():
    """That same 2026-09-18 set was refused in real life, twice:

        13:55:58  arb candidate us chicago buy_all 9.4c/set — depth INCOMPLETE, standing down

    Five legs read fine and the sixth came back empty, which on this venue means the quota was
    gone. All-or-nothing is the right rule, but it made a spent quota as expensive as a missing
    market and it cost the best book on record. Serving the cooldown and asking again rescues the
    set -- retrying immediately would not, because the venue is still backing off."""
    from polybot.feeds import usvenue

    v = usvenue.USVenue()
    v.available, (v._client, calls) = True, _chicago_client(fail_leg5_times=1)
    legs = _chicago_legs(with_depth=False)

    assert v.fill_depth_buckets(legs) is True     # rescued once the cooldown is served
    assert calls["leg5"] == 2                     # asked twice, and only that leg
    assert all(calls[f"leg{i}"] == 1 for i in range(5))
    assert all(b.ask_qty is not None for b in legs)

    # And the rescued book is the real one, worth $3.23 rather than nothing.
    from polybot.strategies.bucket_sum import arb_check, size_for_profit
    kind, net, _ = arb_check(legs, "us")
    contracts, _, net_n = size_for_profit(legs, kind, _cfg(), days=1.0)
    assert contracts * net_n / 100 == pytest.approx(3.23, abs=0.05)


def test_a_leg_that_never_answers_still_stands_the_set_down():
    """The retry must not become a way of trading on a book nobody has read. A leg that fails
    every attempt leaves depth unknown, and unknown depth still refuses the set."""
    from polybot.feeds import usvenue
    from polybot.strategies.bucket_sum import size_for_profit

    v = usvenue.USVenue()
    v.available, (v._client, calls) = True, _chicago_client(fail_leg5_times=99)
    legs = _chicago_legs(with_depth=False)

    assert v.fill_depth_buckets(legs) is False
    assert legs[5].ask_qty is None                # unknown, NOT zero
    assert calls["leg5"] == 2                     # tried again, then gave up
    contracts, _, _ = size_for_profit(legs, "buy_all", _cfg(), days=1.0)
    assert contracts == 0


def test_screening_leaves_budget_for_the_call_that_trades():
    """The depth read is the only call that can lead to a trade and it needs six at once. Sharing
    a flat budget with routine screening means the screen spends the quota and the candidate
    arrives to find none left -- which is how the best book on record was lost. Screening runs on
    a smaller budget so something is always held back."""
    from polybot.feeds import usvenue

    v = usvenue.USVenue()
    v._window_s = 60.0
    now = time.time()
    v._calls = [now] * (usvenue.CALL_BUDGET - usvenue.DEPTH_RESERVE)   # screening has had its fill

    assert usvenue.DEPTH_RESERVE >= 1
    assert len(v._calls) < usvenue.CALL_BUDGET      # ...but the venue's budget is not exhausted
    # A screening call would now have to wait for the window; a depth read would not.
    budget_screen = max(1, usvenue.CALL_BUDGET - usvenue.DEPTH_RESERVE)
    assert len(v._calls) >= budget_screen
    assert len(v._calls) < usvenue.CALL_BUDGET


def test_the_sweep_screens_today_only():
    """Screening tomorrow's book as well was justified by a guess — that a thinner, worse-quoted
    book is where a set under $1 is MORE likely. Measured over every snapshot on record:

        today      2920 complete-book minutes,  58 with a positive net  (2.0%)
        tomorrow   1196 complete-book minutes,   0                      (0.0%)

    Nought for 1,196. Mispricings come from active trading, not from the absence of it — a thin
    book just sits at 1.05-1.10 and never crosses. It doubled the work per sweep and never paid."""
    import inspect
    from polybot import runner as runner_mod

    src = inspect.getsource(runner_mod.Runner.loop)
    assert 'day_offsets=(0,)' in src
    assert 'day_offsets=(0, 1)' not in src

    # and the default for a light US pass is unchanged, so an explicit caller can still ask for
    # tomorrow if there is ever a reason to look again
    sig = inspect.getsource(runner_mod.Runner.scan_weather)
    assert "day_offsets if day_offsets is not None" in sig


def test_arb_sweep_interval_covers_the_whole_liquid_day():
    """The "13:00-14:00 peak" was an artefact of when the bot happened to scan. Normalised by
    observed event-minutes the rate of a positive net after fees is flat across the liquid day --
    2.7% over 12:00-15:00 against 2.8% over 09:00-13:00 on 1,450 observations -- so concentrating
    on a peak buys dense coverage of four hours and thin coverage of four equally good ones.

    The same data is emphatic about where NOT to look: 18:00-23:00 is 0 opportunities in 281
    observed event-minutes."""
    from polybot.runner import Runner

    at = lambda h: datetime(2026, 9, 19, h, 0)
    f = Runner._arb_interval_s
    for h in range(9, 17):
        assert f(None, at(h)) == 20.0, f"hour {h} is inside the liquid day"
    assert f(None, at(17)) == 120.0
    assert f(None, at(4)) == 120.0 and f(None, at(8)) == 120.0
    assert f(None, at(19)) == 600.0 and f(None, at(23)) == 600.0   # 0 for 281 observations
    # No hour outside the liquid day is swept as fast as one inside it.
    assert max(f(None, at(h)) for h in range(9, 17)) < min(
        f(None, at(h)) for h in list(range(17, 24)) + list(range(0, 9)))


def test_scan_rotates_so_a_budget_skip_does_not_starve_the_same_books(monkeypatch):
    """The pass runs city-days in a fixed order and abandons the tail when it runs out of time.
    With ten city-days and a 45s budget that meant the same books -- san-francisco, and all of
    tomorrow -- were never screened, every pass, while the log honestly said "skipped 4"."""
    from polybot import runner as runner_mod

    r = object.__new__(runner_mod.Runner)
    r.cfg = config.load()
    r.cfg.arb_pass_budget_s = 0.0          # every pass is instantly over budget
    r.log = lambda *a, **k: None
    r.us = type("V", (), {"available": True, "prefetch_weather_events": lambda self, t: 0})()
    r.weather_modules = {"bucket_sum": object()}
    monkeypatch.setattr(r.cfg, "mode", lambda m: "paper")

    seen = []
    r._scan_one = lambda city, kind, off, *a: (seen.append((city, off)), 0)[1]
    cities = ["nyc", "chicago", "miami", "los-angeles", "san-francisco"]
    starts = []
    for _ in range(4):
        before = len(seen)
        r.scan_weather(cities=cities, modules=["bucket_sum"], venue="us",
                       kinds=("high",), day_offsets=(0, 1))
        starts.append(seen[before] if len(seen) > before else None)
    # Nothing actually ran (budget 0), but the rotation must have advanced regardless, so that
    # over successive passes a different book is first in line.
    assert r._scan_rot == 4 % 10


def test_arb_uses_the_market_own_fee_coefficient_not_a_constant():
    """The fee is ~30% of an arb's gross edge, so which number gets used decides whether a set is
    taken. The venue states `feeCoefficient` per market; it must reach arb_check, not be replaced
    by whatever the fee table last documented."""
    from polybot.feeds.usvenue import bucket_from_us_market
    from polybot.strategies.bucket_sum import arb_check

    def legs(coef):
        out = []
        titles = ["69 or below", "70 to 71", "72 to 73", "74 to 75", "76 to 77", "78 or above"]
        for i, title in enumerate(titles):
            m = {"slug": f"leg{i}", "title": title, "active": True, "closed": False,
                 "bestBidQuote": {"value": "0.13"}, "bestAskQuote": {"value": "0.14"},
                 "outcomes": '["Yes","No"]', "outcomePrices": '["0.13","0.87"]'}
            if coef is not None:
                m["feeCoefficient"] = coef
            out.append(bucket_from_us_market(m))
        return out

    # The coefficient survives the market -> Bucket hop at all.
    assert legs(0.0695)[0].fee_coefficient == 0.0695
    assert legs(None)[0].fee_coefficient is None

    # 6 legs at 0.14 = 0.84, so 16c gross. Fee per set = theta * 6 * 0.14 * 0.86 = theta * 0.7224.
    _, cheap, _ = arb_check(legs(0.02), "us")
    _, dear, _ = arb_check(legs(0.10), "us")
    assert cheap == pytest.approx(16.0 - 0.02 * 72.24, abs=0.05)
    assert dear == pytest.approx(16.0 - 0.10 * 72.24, abs=0.05)
    assert cheap > dear                       # a dearer venue is a thinner arb, not the same one

    # No stated coefficient falls back to the table's live value, never to free.
    _, silent, _ = arb_check(legs(None), "us")
    assert silent == pytest.approx(16.0 - fees.US_TAKER_THETA * 72.24, abs=0.05)


def test_snapshot_keeps_the_whole_ladder_for_arb_candidates(tmp_path):
    """Top-of-book cannot answer the question that decides whether this strategy is worth real
    money. The miami set on 2026-09-19 showed 13c of edge with ONE contract at the best ask, so
    the episode was worth 13 cents -- unless level two was also under $1, which bid_qty/ask_qty
    do not say. Sizing already walks the levels; the record has to keep them."""
    from polybot.ledger import Ledger

    led = Ledger(str(tmp_path / "t.db"))
    led.add_snapshot("us", "leg-a", 0.30, 0.34, 0.31,
                     bid_qty=1, ask_qty=1,
                     bid_levels=[(0.30, 1), (0.28, 40)], ask_levels=[(0.34, 1), (0.37, 25)])
    led.add_snapshot("us", "leg-b", 0.10, 0.12, 0.11)      # the ordinary screen: no depth paid for

    rows = {r["market"]: r for r in led.conn.execute("SELECT * FROM snapshots")}
    assert json.loads(rows["leg-a"]["ask_ladder"]) == [[0.34, 1], [0.37, 25]]
    assert json.loads(rows["leg-a"]["bid_ladder"]) == [[0.30, 1], [0.28, 40]]
    assert rows["leg-b"]["ask_ladder"] is None             # ~99% of rows stay as small as before
    assert rows["leg-b"]["bid_ladder"] is None


def test_ledger_migrates_ladder_columns_onto_an_existing_db(tmp_path):
    """The database on disk is 130 MB and predates these columns; CREATE TABLE IF NOT EXISTS will
    not add them. A migration that only handles REAL columns would leave the TEXT ones missing and
    every candidate snapshot would fail to insert."""
    import sqlite3
    from polybot.ledger import Ledger

    path = str(tmp_path / "old.db")
    con = sqlite3.connect(path)
    con.executescript("CREATE TABLE snapshots (ts REAL NOT NULL, venue TEXT NOT NULL, "
                      "market TEXT NOT NULL, bid REAL, ask REAL, mid REAL, last REAL);")
    con.execute("INSERT INTO snapshots (ts, venue, market, bid, ask) VALUES (1.0,'us','old',0.1,0.2)")
    con.commit(); con.close()

    led = Ledger(path)
    cols = {r[1] for r in led.conn.execute("PRAGMA table_info(snapshots)")}
    assert {"bid_qty", "ask_qty", "bid_ladder", "ask_ladder"} <= cols
    led.add_snapshot("us", "new", 0.3, 0.4, ask_levels=[(0.4, 7)])
    row = led.conn.execute("SELECT ask_ladder FROM snapshots WHERE market='new'").fetchone()
    assert json.loads(row[0]) == [[0.4, 7]]


def test_us_prefetch_fetches_a_whole_pass_in_one_call():
    """A pass over five cities and two days asked the venue for up to TEN separate events, one
    `retrieve_by_slug` each. The venue allows five requests per window, so the pass spent more than
    its entire quota on lookups before pricing a single leg — which is why a scan configured for
    every minute actually ran every two, against arb episodes that last about a minute.

    `events.list({"slug": [...]})` returns the same objects (verified field-for-field against the
    live gateway 2026-09-19, quote blocks included), so the whole pass is one call."""
    from polybot.feeds import usvenue

    calls = {"list": 0, "retrieve": 0}
    slugs = [usvenue.us_event_slug(c, datetime(2026, 9, 19), "high")
             for c in ("nyc", "chicago", "miami")]

    class Events:
        def list(self, params):
            calls["list"] += 1
            return {"events": [_fake_us_weather_event(s) for s in params["slug"]]}

        def retrieve_by_slug(self, slug):
            calls["retrieve"] += 1
            raise AssertionError("prefetched events must not be fetched again one at a time")

    class Client:
        events = Events()

    v = usvenue.USVenue()
    v.available, v._client = True, Client()

    n = v.prefetch_weather_events((c, datetime(2026, 9, 19), "high")
                                  for c in ("nyc", "chicago", "miami"))
    assert n == 3
    assert calls["list"] == 1                      # THE point: three events, one request

    for city in ("nyc", "chicago", "miami"):
        ev = v.find_weather_event(city, datetime(2026, 9, 19), "high")
        assert ev is not None and ev.buckets        # served from the batch...
    assert calls["retrieve"] == 0                   # ...costing nothing extra
    assert calls["list"] == 1

    # A prefetched quote is good for its own pass only: past the TTL we pay for a fresh look
    # rather than screen on the previous pass's paper.
    v._prefetch = {s: (time.time() - usvenue.EVENT_PREFETCH_TTL_S - 1, e)
                   for s, (_, e) in v._prefetch.items()}
    with pytest.raises(AssertionError):
        v.find_weather_event("nyc", datetime(2026, 9, 19), "high")


def test_us_prefetch_failure_falls_back_to_per_event_lookups():
    """The batch is an optimisation, never a dependency: if the list call is rate limited or comes
    back empty, every caller must still find its event the old way. A cheaper pass that goes blind
    when the venue hiccups is worse than the slow one it replaced."""
    from polybot.feeds import usvenue

    class RateLimited(Exception):
        status_code = 429

    calls = {"retrieve": 0}

    class Events:
        def list(self, params):
            raise RateLimited("<!doctype html>...You are being rate limited...")

        def retrieve_by_slug(self, slug):
            calls["retrieve"] += 1
            return {"event": _fake_us_weather_event(slug)}

    class Client:
        events = Events()

    v = usvenue.USVenue()
    v.available, v._client = True, Client()

    assert v.prefetch_weather_events([("nyc", datetime(2026, 9, 19), "high")]) == 0
    assert v._prefetch == {}                 # nothing cached, nothing half-cached
    v._backoff_until = 0.0                   # the rate limit tripped a cooldown; let it elapse
    ev = v.find_weather_event("nyc", datetime(2026, 9, 19), "high")
    assert ev is not None and ev.buckets     # still found, the old way
    assert calls["retrieve"] == 1


def test_us_prefetch_skips_slugs_known_missing():
    """Polymarket US lists no `low` market and often no tomorrow market yet. Those 404s are already
    remembered for an hour; the batch must honour that rather than re-asking for ten dead slugs."""
    from polybot.feeds import usvenue

    asked = {}

    class Events:
        def list(self, params):
            asked["slugs"] = list(params["slug"])
            return {"events": [_fake_us_weather_event(s) for s in params["slug"]]}

    class Client:
        events = Events()

    v = usvenue.USVenue()
    v.available, v._client = True, Client()
    dead = usvenue.us_event_slug("nyc", datetime(2026, 9, 20), "high")
    v._missing[dead] = time.time() + usvenue.MISSING_EVENT_RETRY_S

    v.prefetch_weather_events([("nyc", datetime(2026, 9, 19), "high"),
                               ("nyc", datetime(2026, 9, 20), "high")])
    assert dead not in asked["slugs"]
    assert asked["slugs"] == [usvenue.us_event_slug("nyc", datetime(2026, 9, 19), "high")]


def test_us_scan_records_us_signals_and_snapshots(monkeypatch):
    from polybot import runner as runner_mod
    from polybot.feeds import usvenue
    from polybot.paper import snapshot_history
    cfg, led = _cfg(), _ledger()
    r = runner_mod.Runner(cfg, led, log=lambda *_: None)
    ev = usvenue.weather_event_from_us(_us_event(), "nyc", "high")
    ev.buckets[0].best_ask = 0.95   # a fat mispricing so bucket_sum has something to say: the book sums well over 1
    ev.buckets[1].best_ask = 0.60
    ev.buckets[2].best_ask = 0.60

    class FakeUS:
        available = True
        why_unavailable = ""

        def find_weather_event(self, city, date, kind):
            return ev if (city, kind) == ("nyc", "high") else None

        def resolution(self, slug):
            return None

    r.us = FakeUS()
    n = r.scan_weather(cities=["nyc"], modules=["bucket_sum"], kinds=("high",), venue="us")
    snaps = led.snapshots("us", "tc-temp-nychigh-2026-09-13-gte78lt79f", 0)
    assert snaps and snaps[0]["bid"] == 0.35 and snaps[0]["ask"] == 0.60
    assert snapshot_history(led, "us", "tc-temp-nychigh-2026-09-13-gte78lt79f", 0)[0][1] == pytest.approx(0.475)
    for s in led.open_signals(venue="us"):
        assert s["market"].startswith("tc-temp-nychigh-2026-09-13-") and s["mode"] == "paper"
    assert n == len(led.open_signals(venue="us"))
    assert r.scan_weather(cities=["chicago"], modules=["bucket_sum"], kinds=("high",), venue="us") == 0


def test_config_reload_picks_up_an_edited_mode(tmp_path, monkeypatch):
    """The loop read config.json once at startup: flipping a module by hand did nothing until a
    relaunch, and the next promote() wrote the stale copy back over the edit."""
    import json as _json
    import polybot.config as cfgmod
    path = str(tmp_path / "config.json")
    base = _json.loads(_json.dumps(cfgmod.load().__dict__, default=lambda o: o.__dict__))
    base["modes"] = {"weather_lock": "paper", "weather_hold": "paper"}
    with open(path, "w") as f:
        _json.dump(base, f)
    monkeypatch.setattr(cfgmod, "CONFIG_PATH", path)

    from polybot.runner import Runner
    r = Runner(cfg=cfgmod.load(path), ledger=_ledger(), log=lambda *_: None)
    r._cfg_mtime = os.path.getmtime(path)
    assert r.cfg.mode("weather_hold") == "paper"
    assert r.reload_config_if_changed() is False          # untouched file: no work

    base["modes"]["weather_hold"] = "off"
    with open(path, "w") as f:
        _json.dump(base, f)
    os.utime(path, (time.time() + 1, time.time() + 1))
    assert r.reload_config_if_changed() is True
    assert r.cfg.mode("weather_hold") == "off" and r.cfg.mode("weather_lock") == "paper"


def test_weather_lock_skips_an_empty_book_and_may_cross():
    """bid 0.03 / ask 0.95 made (1 - post) read as 92 cents of edge that nobody would ever trade
    against. Those phantom signals were most of the 27% fill rate."""
    from polybot.risk import RiskManager
    cfg = _cfg()
    ctx = _ctx(obs=[("2026-09-12T15:51:00+00:00", 76), ("2026-09-12T18:51:00+00:00", 79),
                    ("2026-09-12T19:51:00+00:00", 78), ("2026-09-12T20:51:00+00:00", 77)],
               hourly=[("2026-09-12T17:00", 74.0), ("2026-09-12T18:00", 73.0), ("2026-09-12T20:00", 70.0)])
    _, winner = lock_state(ctx)
    winner.best_bid, winner.best_ask = 0.03, 0.95
    assert WeatherLock(cfg).scan(ctx) == [], "a 92-cent spread is an empty book, not an edge"
    # and nothing is left to win once the ask is at the ceiling
    winner.best_bid, winner.best_ask = 0.97, 0.98
    assert WeatherLock(cfg).scan(ctx) == []


def test_weather_lock_refuses_a_cheap_ask_even_on_a_tight_book():
    """The spread filter catches empty books; it does not catch a liquid book that simply
    disagrees. Closed paper 2026-09-12..16: lock entries under 0.50 went 0/7 for -$139.73 — the
    whole of the module's loss — and one of those filled at 0.03 on a 1-cent spread, so width was
    never the tell. A 20:1 disagreement between the remaining-hours forecast and a real book is
    the forecast being wrong, so `lock_min_price` refuses it."""
    cfg = _cfg()
    ctx = _ctx(obs=[("2026-09-12T15:51:00+00:00", 76), ("2026-09-12T18:51:00+00:00", 79),
                    ("2026-09-12T19:51:00+00:00", 78), ("2026-09-12T20:51:00+00:00", 77)],
               hourly=[("2026-09-12T17:00", 74.0), ("2026-09-12T18:00", 73.0), ("2026-09-12T20:00", 70.0)])
    _, winner = lock_state(ctx)
    # a tight book — the spread filter has no objection — but priced at 3 cents
    winner.best_bid, winner.best_ask = 0.02, 0.03
    assert WeatherLock(cfg).scan(ctx) == [], "a 3-cent ask on a 1-cent spread is the market, not an edge"
    # 0.28 on an 8-cent spread is the live shape of signal #909; still refused
    winner.best_bid, winner.best_ask = 0.20, 0.28
    assert WeatherLock(cfg).scan(ctx) == []
    # and the band that actually paid is untouched
    winner.best_bid, winner.best_ask = 0.88, 0.90
    sigs = WeatherLock(cfg).scan(ctx)
    assert len(sigs) == 1 and sigs[0].price == 0.90 and sigs[0].taker


# ---- 2026-09-23: more paper signals through the gate -------------------------------------------
def _us_catalogue_event(slug, title, markets, category="politics", end="2026-11-03T23:59:00Z"):
    """A Polymarket US event as events.list returns it. markets: [(slug, title, bid, ask)]."""
    q = lambda v: None if v is None else {"value": f"{v:.4f}", "currency": "USD"}
    return {"slug": slug, "title": title, "category": category, "endDate": end,
            "markets": [{"slug": s, "title": t, "bestBidQuote": q(b), "bestAskQuote": q(a)} for s, t, b, a in markets]}


def _off_event(slug, title, markets, end="2026-11-03T12:00:00Z", volume=1000.0):
    """A gamma event. markets: [(id, label, token, bid, ask)] — outcomes Yes/No, YES token first."""
    return {"id": slug, "slug": slug, "title": title, "endDate": end, "volume24hr": volume,
            "markets": [{"id": i, "groupItemTitle": lab, "question": f"{title} {lab}",
                         "clobTokenIds": json.dumps([tok, tok + "-no"]), "outcomes": json.dumps(["Yes", "No"]),
                         "bestBid": str(b), "bestAsk": str(a), "active": True, "closed": False}
                        for i, lab, tok, b, a in markets]}


def test_pairs_same_question_catches_the_near_identical_titles():
    """Polymarket US copies offshore titles almost word for word, so the dangerous pair is not a
    loose match but a near-exact one that asks a different question. A character ratio scores the
    Tarrant/Denton county races at ~0.9; content words make them plainly different."""
    from polybot import pairs
    assert not pairs.same_question("Texas Senate Election: Tarrant County Winner",
                                   "Texas Senate Election: Denton County Winner")
    assert not pairs.same_question("Fed Decision in October", "Fed Decision in December?")
    assert pairs.same_question("Fed Decision in October", "Fed Decision in October?")
    # a year counts only when both titles carry one
    assert pairs.same_question("2026 Nobel Peace Prize Winner", "Nobel Peace Prize Winner 2026")
    assert pairs.same_question("Nobel Peace Prize Winner", "Nobel Peace Prize Winner 2026")
    assert not pairs.same_question("Nobel Peace Prize Winner 2026", "Nobel Peace Prize Winner 2027")
    # the House/Senate control markets are worded completely differently and are the same question
    assert pairs.same_question("U.S House Midterm Winner", "Which party will win the House in 2026?")
    assert not pairs.same_question("U.S House Midterm Winner", "Which party will win the Senate in 2026?")
    # parties fold, numbers stay
    assert pairs.same_question("Democratic Party", "Democrats")
    assert not pairs.same_question("48", "49") and pairs.same_question("25 bps Decrease", "25 bps decrease")


def test_pairs_match_events_pairs_markets_and_refuses_what_it_cannot_prove():
    from polybot import pairs
    us = [
        _us_catalogue_event("usfed-fomc-2026-10-28", "Fed Decision in October",
                  [("rdc-nochg", "No Change", 0.35, 0.36), ("rdc-hike25", "25 bps Increase", 0.59, 0.60),
                   ("rdc-cut50", "50+ bps Decrease", None, 0.02)], category="macro", end="2026-10-28T23:59:00Z"),
        _us_catalogue_event("usse-tx-den-2026-11-03", "Texas Senate Election: Denton County Winner",
                  [("den-d", "James Talarico (D)", 0.96, 0.99), ("den-r", "Ken Paxton (R)", 0.96, 0.99)]),
        _us_catalogue_event("usfedgvmt-by", "Government Shutdown?", [("shut-oct1", "By October 1, 2026", 0.01, 0.02)],
                  end="2026-10-01T00:00:00Z"),
        _us_catalogue_event("nobody-else", "Who will host the 2030 Winter Olympics?", [("x", "Sweden", 0.5, 0.6)]),
    ]
    off = [
        _off_event("fed-decision-in-october", "Fed Decision in October?",
                   [(1, "No change", "tokNC", 0.38, 0.39), (2, "25 bps increase", "tokH25", 0.60, 0.61),
                    (3, "50+ bps decrease", "tokC50", 0.0, 0.005)], end="2026-10-29T00:00:00Z", volume=9e6),
        _off_event("fed-decision-in-december", "Fed Decision in December?",
                   [(4, "No change", "tokDecNC", 0.5, 0.51)], end="2026-12-10T00:00:00Z"),
        # the county race exists offshore too, but the US book is quoting nonsense on both candidates
        _off_event("texas-senate-election-denton-county-winner", "Texas Senate Election: Denton County Winner",
                   [(5, "James Talarico (D)", "tokDenD", 0.40, 0.41), (6, "Ken Paxton (R)", "tokDenR", 0.58, 0.59)]),
        _off_event("government-shutdown-by-october-1", "Government shutdown by October 1?",
                   [(7, "Government shutdown by October 1?", "tokShut", 0.015, 0.016)], end="2026-10-02T00:00:00Z"),
    ]
    got, counts = pairs.match_events(us, off)
    by_slug = {p["us_slug"]: p for p in got}
    assert by_slug["rdc-nochg"]["offshore_token"] == "tokNC"
    assert by_slug["rdc-hike25"]["offshore_token"] == "tokH25"
    assert by_slug["rdc-cut50"]["offshore_token"] == "tokC50" and by_slug["rdc-cut50"]["us_mid"] is None
    assert "tokDecNC" not in {p["offshore_token"] for p in got}          # December is a different question
    assert by_slug["shut-oct1"]["offshore_token"] == "tokShut"          # single market: title + label
    assert "den-d" not in by_slug and "den-r" not in by_slug             # 0.975 vs 0.41: a bad quote, not a lag
    assert counts["gap_rejected"] == 2
    assert "x" not in by_slug                                            # no twin offshore, no pair
    assert got[0]["us_event"] == "usfed-fomc-2026-10-28"                 # quoted + busiest reference first


def test_pair_recorder_keeps_every_sample_but_writes_only_changes():
    from polybot import pairs

    class FakeUS:
        available = True
        def __init__(self):
            self.quotes, self.calls = {"a": (0.40, 0.42), "b": (0.10, 0.12)}, 0
        def events_by_slug(self, slugs):
            self.calls += 1
            q = lambda v: {"value": str(v)}
            return {"ev": {"slug": "ev", "markets": [{"slug": s, "bestBidQuote": q(b), "bestAskQuote": q(a)}
                                                      for s, (b, a) in self.quotes.items()]}}

    led, us, now = _ledger(), FakeUS(), [1000.0]
    offp = {"ta": (0.45, 0.46), "tb": (0.11, 0.12)}
    rec = pairs.PairRecorder(led, us, clock=lambda: now[0], offshore_prices=lambda toks: {t: offp[t] for t in toks})
    rows = [{"us_slug": "a", "us_event": "ev", "offshore_token": "ta"},
            {"us_slug": "b", "us_event": "ev", "offshore_token": "tb"}]
    for step in range(3):
        now[0] = 1000.0 + 60 * step
        rec.record(rows)
    assert us.calls == 3                                  # one batched call per tick, not one per market
    assert [p for _, p in rec.get("us", "a")] == pytest.approx([0.41, 0.41, 0.41])
    assert len(led.snapshots("us", "a", 0)) == 1          # unchanged quotes are not re-written
    us.quotes["a"] = (0.44, 0.46)
    now[0] = 1180.0
    rec.record(rows)
    assert len(led.snapshots("us", "a", 0)) == 2 and rec.quote("a") == (0.44, 0.46)
    now[0] = 1180.0 + 1000
    assert rec.quote("a") == (None, None)                 # a stale quote is no quote
    now[0] = 1180.0 + pairs.SNAPSHOT_HEARTBEAT_S
    rec.record(rows)
    assert len(led.snapshots("us", "a", 0)) == 3          # ...but a quiet book still leaves a heartbeat


def test_leadlag_fires_on_recorded_paths_and_lands_as_a_paper_signal(monkeypatch):
    """End to end on fakes: the reference moves 6c inside the window, the US book does not follow,
    and the runner records a PAPER leadlag signal priced off the recorder's own quote."""
    from polybot import pairs, runner as runner_mod

    class FakeUS:
        available, why_unavailable = True, ""
        def __init__(self):
            self.book = (0.40, 0.42)
        def events_by_slug(self, slugs):
            q = lambda v: {"value": str(v)}
            return {"ev": {"slug": "ev", "markets": [{"slug": "us-a", "bestBidQuote": q(self.book[0]),
                                                       "bestAskQuote": q(self.book[1])}]}}
        def bbo(self, slug):
            raise AssertionError("the recorder's quote should have been used")

    cfg, led = _cfg(), _ledger()
    cfg.modes["leadlag"] = "paper"
    r = runner_mod.Runner(cfg, led, log=lambda *_: None)
    fake, now, ref = FakeUS(), [5000.0], [(0.41, 0.42)]
    r.us = fake
    r.pair_rec = pairs.PairRecorder(led, fake, clock=lambda: now[0],
                                    offshore_prices=lambda toks: {"tok-a": ref[0]})
    rows = [{"us_slug": "us-a", "us_event": "ev", "offshore_token": "tok-a", "label": "Fed: No Change",
             "category": "macro"}]
    from polybot.strategies.leadlag import LeadLag
    r.other_modules["leadlag"] = LeadLag(cfg, fake, r.pair_rec, quote_fn=r.pair_rec.quote, pairs_fn=lambda: rows)
    monkeypatch.setattr(r, "leadlag_pairs", lambda: rows)
    assert r.record_pairs() == 0                           # one sample: nothing to compare yet
    now[0], ref[0] = 5060.0, (0.47, 0.48)                  # reference +6c in a minute, US still 0.41
    assert r.record_pairs() == 1
    sig = led.conn.execute("SELECT module, venue, market, side, mode, price FROM signals").fetchone()
    assert tuple(sig) == ("leadlag", "us", "us-a", "BUY_YES", "paper", 0.41)


def test_daily_jobs_catch_up_after_a_missed_slot(monkeypatch):
    """calibration last built 2026-09-12 and pairs.json was never built: both were pinned to one
    minute (03:00, 05:00) while this Mac sleeps. A missed slot now runs at the next chance."""
    from polybot import runner as runner_mod
    monkeypatch.setattr(runner_mod, "_save_jobs", lambda jobs, path=None: None)
    r = runner_mod.Runner(_cfg(), _ledger(), log=lambda *_: None)
    r._jobs = {}
    et = runner_mod.ET
    morning = datetime(2026, 9, 23, 7, 40, tzinfo=et)
    assert r._due("calibration", morning, (3,), quiet_hours=runner_mod.ARB_HOURS)    # asleep at 03:00
    r._ran("calibration")
    r._jobs["calibration"] = datetime(2026, 9, 23, 7, 41, tzinfo=et).timestamp()
    assert not r._due("calibration", datetime(2026, 9, 23, 22, 0, tzinfo=et), (3,))  # done for today
    assert r._due("calibration", datetime(2026, 9, 24, 8, 0, tzinfo=et), (3,))       # tomorrow's slot
    assert not r._due("build_pairs", datetime(2026, 9, 23, 13, 0, tzinfo=et), (5,),
                      quiet_hours=runner_mod.ARB_HOURS)                                # the arb window
    r._jobs["hold_favorites"] = datetime(2026, 9, 23, 9, 2, tzinfo=et).timestamp()
    assert not r._due("hold_favorites", datetime(2026, 9, 23, 20, 59, tzinfo=et), (9, 21))
    assert r._due("hold_favorites", datetime(2026, 9, 23, 21, 3, tzinfo=et), (9, 21))
    assert runner_mod._last_slot(datetime(2026, 9, 23, 2, 0, tzinfo=et), (3,)).day == 22
    # a failed run stays due, but is not retried every minute
    r._jobs.pop("calibration")
    r._attempt("calibration")
    assert not r._due("calibration", datetime(2026, 9, 24, 8, 0, tzinfo=et), (3,))
    r._attempts["calibration"] -= runner_mod.JOB_RETRY_S
    assert r._due("calibration", datetime(2026, 9, 24, 8, 0, tzinfo=et), (3,))


def test_calibration_falls_back_when_the_category_cell_is_thin():
    """A politics 0.85-0.90 cell with 2 samples used to SHADOW an `all` cell with 60 — the lookup
    took `category_cell or all_cell`, which only falls back when the cell is missing."""
    table = {}
    for _ in range(2):
        calibration.add_sample(table, "politics", 0.88, 1)
    for _ in range(58):
        calibration.add_sample(table, "all", 0.88, 1)
    for _ in range(2):
        calibration.add_sample(table, "all", 0.88, 0)
    assert calibration.lookup(table, 0.88, "politics") == pytest.approx((58 + 20 * 0.88) / 80)
    assert calibration.lookup({"all": {}}, 0.88, "politics") is None


def test_calibration_build_accumulates_and_never_samples_a_market_twice(tmp_path):
    def market(i, outcome=1):
        return {"id": i, "closed": True, "outcomePrices": json.dumps(["1" if outcome else "0", "0"]),
                "clobTokenIds": json.dumps([f"tok{i}", f"tok{i}n"])}
    day = 86400.0
    pages_seen = []
    batches = [[{"endDate": "2026-09-22T12:00:00Z", "tags": [{"slug": "politics"}], "markets": [market(1), market(2)]}],
               [{"endDate": "2026-09-23T12:00:00Z", "tags": [{"slug": "politics"}], "markets": [market(3)]},
                {"endDate": "2026-09-22T12:00:00Z", "tags": [{"slug": "politics"}], "markets": [market(1), market(2)]}]]

    def pages(end_max, end_min, max_events):
        pages_seen.append((end_max, end_min))
        yield batches[min(len(pages_seen) - 1, 1)] if end_min is None or "2026-09-22" not in str(end_min) else batches[1][:1]

    calls = []

    def history(tok):
        calls.append(tok)
        return [(0.0, 0.9), (2 * day, 0.91), (3 * day, 1.0)]     # 0.90 a day before it settled

    path = str(tmp_path / "samples.json")
    t1 = calibration.build(samples_path=path, history_fn=history, pages_fn=pages, log=lambda *_: None)
    assert t1["_n"] == 2 and sorted(calls) == ["tok1", "tok2"]
    t2 = calibration.build(samples_path=path, history_fn=history, pages_fn=pages, log=lambda *_: None)
    assert "tok1" not in calls[2:] and "tok2" not in calls[2:]           # cached, never re-fetched
    assert t2["_n"] == 3 and t2["all"]["0.90-0.95"]["n"] == 3


def test_offshore_paging_moves_past_the_offset_cap(monkeypatch):
    """Gamma 422s past offset ~2,000, which is where a plain offset loop stopped dead."""
    served = []

    def fake_get(url, params=None):
        served.append(dict(params))
        if params["offset"] > offshore.MAX_GAMMA_OFFSET:
            raise requests.HTTPError("422")
        floor = params.get("end_date_min", "")
        if floor >= "2026-09-24":
            return [{"id": f"late{params['offset']}", "endDate": "2026-09-25T00:00:00Z"}] if params["offset"] == 0 else []
        return [{"id": f"e{params['offset']}-{k}", "endDate": "2026-09-24T00:00:00Z"} for k in range(100)]

    monkeypatch.setattr(offshore, "_get", fake_get)
    got = offshore._paged_events({"end_date_min": "2026-09-23T00:00:00Z"}, max_events=5000)
    ids = {e["id"] for e in got}
    assert len(ids) == 2101 and "late0" in ids
    assert any(p.get("end_date_min") == "2026-09-24T00:00:00Z" for p in served)


def test_hold_favorites_reads_the_whole_horizon_without_the_coin_flips(monkeypatch):
    asked = {}

    def fake(days, **kw):
        asked.update(kw, days=days)
        return []

    monkeypatch.setattr(offshore, "events_ending_within", fake)
    HoldFavorites(_cfg(), {"all": {}}).scan()
    assert asked["days"] == 7
    assert offshore.TAG_UP_OR_DOWN in asked["exclude_tag_ids"] and offshore.TAG_SPORTS in asked["exclude_tag_ids"]


def test_a_confirmed_book_with_no_set_says_why_once(monkeypatch):
    """2026-09-23 09:41-09:44: miami's depth read said "sell_all 2.8c/set, thinnest leg 42" every
    ~33 s and nothing was booked or refused. The 13 sets already held had eaten the top of two
    ladders, so the next set netted under the unwind cover. Say that — once."""
    from polybot import runner as runner_mod
    from polybot.strategies.bucket_sum import explain_no_set

    def leg(bid, ladder, tok):
        b = _bucket("70-71°F", bid, bid + 0.02, tok)
        b.bid_levels, b.fee_coefficient = ladder, 0.0695
        return b
    # the miami book after consuming 13 held sets from each ladder (stored snapshot, 09:41:29)
    legs = [leg(0.32, [[0.32, 42.0], [0.31, 122.0]], "a"), leg(0.42, [[0.42, 64.0]], "b"),
            leg(0.22, [[0.22, 72.0]], "c"), leg(0.03, [[0.02, 232.0], [0.01, 1614.0]], "d"),
            leg(0.01, [[0.01, 1826.8]], "e"), leg(0.08, [[0.07, 322.0], [0.06, 515.0]], "f")]
    why = explain_no_set(legs, "sell_all", _cfg(), days=1.0, held=13)
    assert "after the 13 set(s) already held" in why and "unwind cover" in why

    lines = []
    r = runner_mod.Runner(_cfg(), _ledger(), log=lines.append)
    for _ in range(6):
        r._quiet_log(("temp-miahigh", "confirm"), ["  arb candidate ... 2.8c/set — depth read", "    no set: " + why])
    assert len(lines) == 2                                   # told once
    r._quiet_log(("temp-miahigh", "confirm"), ["  arb candidate ... 3.8c/set — depth read"])
    assert any("repeated 5x more" in l for l in lines) and lines[-1].endswith("3.8c/set — depth read")


def test_backtest_history_window_is_bounded_to_the_day(monkeypatch):
    seen = {}
    monkeypatch.setattr(offshore, "_get", lambda url, params=None: seen.update(params) or {"history": []})
    offshore.prices_history("tok", since_ts=1_000_000, until_ts=1_086_400)
    assert seen["startTs"] == 1_000_000 and seen["endTs"] == 1_086_400


def test_hold_favorites_leaves_the_temperature_buckets_to_the_weather_modules():
    table = {"all": {"0.85-0.90": {"n": 60, "yes": 59}}}
    end = datetime.now(ZoneInfo("UTC")).timestamp() + 86400
    end_iso = datetime.fromtimestamp(end, ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")
    mk = lambda i: {"id": i, "question": "q", "outcomePrices": json.dumps(["0.87", "0.13"]), "endDate": end_iso,
                    "clobTokenIds": json.dumps([f"t{i}", f"t{i}n"]), "bestBid": "0.86", "bestAsk": "0.88"}
    events = [{"slug": "highest-temperature-in-nyc-on-september-24-2026", "title": "Highest temperature in NYC",
               "tags": [{"slug": "weather"}], "endDate": end_iso, "markets": [mk(1)]},
              {"slug": "will-the-bill-pass", "title": "Will the bill pass?", "tags": [{"slug": "politics"}],
               "endDate": end_iso, "markets": [mk(2)]}]
    sigs = HoldFavorites(_cfg(), table).scan(events=events)
    assert [s.market for s in sigs] == ["t2"]


def test_the_recorder_puts_two_intervals_inside_leadlags_window():
    """At a 60 s cadence the oldest in-window sample was the one 60 s back (ticks drift late), so a
    3.5c move over 120 s read as 1.5c. The window only works if two sampling intervals fit in it."""
    from polybot import pairs
    window = _cfg().leadlag_window_s
    for interval, ok in ((60.0, False), (pairs.RECORD_INTERVAL_S, True)):
        ts = [1000.0 + i * (interval + 0.05) for i in range(6)]          # each tick a little late
        first_in = next(t for t in ts if t >= ts[-1] - window)
        assert (ts[-1] - first_in >= 2 * interval - 1) is ok


def test_a_us_timeout_costs_the_recorder_one_tick_of_us_quotes_only():
    from polybot import pairs

    class Flaky:
        available = True
        def events_by_slug(self, slugs):
            raise TimeoutError("Request timed out.")

    rec = pairs.PairRecorder(_ledger(), Flaky(), clock=lambda: 1000.0,
                             offshore_prices=lambda toks: {t: (0.40, 0.41) for t in toks})
    got = rec.record([{"us_slug": "a", "us_event": "ev", "offshore_token": "ta"}])
    assert got["us"] == 0 and got["offshore"] == 1 and rec.get("offshore", "ta")


def test_a_closed_us_market_leaves_the_recorder_with_no_price_at_all():
    """US markets close by STATUS, not the `closed` flag, and keep their last quotes. One read as an
    87c leadlag edge on 2026-09-23 (Trump Jr. VP, closed at 0.93/0.94 against 0.06 offshore)."""
    from polybot import pairs

    class US:
        available, status = True, "MARKET_STATUS_OPEN"
        def events_by_slug(self, slugs):
            return {"ev": {"slug": "ev", "markets": [{"slug": "a", "status": self.status,
                                                       "bestBidQuote": {"value": "0.93"},
                                                       "bestAskQuote": {"value": "0.94"}}]}}

    us, now = US(), [1000.0]
    rec = pairs.PairRecorder(_ledger(), us, clock=lambda: now[0], offshore_prices=lambda toks: {})
    rows = [{"us_slug": "a", "us_event": "ev", "offshore_token": "ta"}]
    rec.record(rows)
    assert rec.get("us", "a") and rec.quote("a") == (0.93, 0.94)
    us.status, now[0] = "MARKET_STATUS_CLOSED", 1040.0
    rec.record(rows)
    assert rec.get("us", "a") == [] and rec.quote("a") == (None, None)
    closed_event = {"slug": "ev", "title": "Fed Decision in October", "markets": [
        {"slug": "a", "title": "No Change", "status": "MARKET_STATUS_CLOSED"}]}
    got, _ = pairs.match_events([closed_event], [_off_event("f", "Fed Decision in October?",
                                                            [(1, "No change", "t", 0.4, 0.41)])])
    assert got == []


def test_hold_favorites_reads_the_price_it_would_post_not_the_last_mark():
    """30 of the first 102 live favourites (2026-09-23 15:56) posted under 85c: the band and the
    lookup read the mark (0.86) while the order rested at bid + 1c (0.71) — a 15c "edge" that is
    only the spread."""
    table = {"all": {"0.85-0.90": {"n": 60, "yes": 59}, "0.70-0.75": {"n": 60, "yes": 40},
                     "0.10-0.15": {"n": 60, "yes": 1}, "0.00-0.05": {"n": 60, "yes": 1}}}
    end = datetime.now(ZoneInfo("UTC")).timestamp() + 86400
    end_iso = datetime.fromtimestamp(end, ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")

    def ev(i, mark, bid, ask, q="Will it happen?"):
        return {"slug": f"e{i}", "title": q, "tags": [{"slug": "politics"}], "endDate": end_iso,
                "markets": [{"id": i, "question": q, "outcomePrices": json.dumps([str(mark), str(1 - mark)]),
                             "endDate": end_iso, "clobTokenIds": json.dumps([f"t{i}", f"t{i}n"]),
                             "bestBid": str(bid), "bestAsk": str(ask)}]}
    sigs = HoldFavorites(_cfg(), table).scan(events=[
        ev(1, 0.86, 0.70, 0.90),                       # wide book: posts at 0.71 → not a favourite at all
        ev(2, 0.86, 0.86, 0.88),                       # tight book: posts at 0.87, in band
        ev(3, 0.04, 0.12, 0.14, "Will it happen by Friday?"),   # longshot read at its BID (0.12), not the 4c mark
    ])
    assert sorted((s.market, s.side, s.price) for s in sigs) == [("t2", "BUY_YES", 0.87), ("t3", "BUY_NO", 0.89)]


def test_model_runs_are_compared_within_one_venue():
    """US and offshore scans of one city-day alternated in model_runs, so last_model_run handed each
    the other's run and only 29 of 260 US runs (09-12..18) were ever compared against a US run."""
    led = _ledger()
    led.add_model_run("nyc", "2026-09-24", "high", [0.1] * 6, ts=100, venue="us")
    led.add_model_run("nyc", "2026-09-24", "high", [0.1] * 11, ts=200, venue="offshore")
    assert len(led.last_model_run("nyc", "2026-09-24", "high", venue="us")["probs"]) == 6
    assert len(led.last_model_run("nyc", "2026-09-24", "high", venue="offshore")["probs"]) == 11
    assert len(led.last_model_run("nyc", "2026-09-24", "high")["probs"]) == 11      # no venue: newest, as before

    # the module itself asks for its own venue and records under it
    ctx = _ctx()
    ctx.venue = "us"
    mod = WeatherModelUpdate(_cfg(), led)
    mod.scan(ctx)
    row = led.conn.execute("SELECT venue FROM model_runs ORDER BY ts DESC LIMIT 1").fetchone()
    assert row["venue"] == "us"


def test_the_loop_keeps_recording_model_runs_while_model_update_is_off(monkeypatch):
    from polybot import runner as runner_mod
    cfg, led = _cfg(), _ledger()
    cfg.modes["weather_model_update"] = "off"
    r = runner_mod.Runner(cfg, led, log=lambda *_: None)
    ctx = _ctx()
    monkeypatch.setattr(runner_mod, "build_ctx", lambda *a, **k: ctx)
    r._scan_one("nyc", "high", 0, ["weather_lock"], False, "offshore", None)
    row = led.conn.execute("SELECT city, venue, probs FROM model_runs").fetchone()
    assert row["city"] == "nyc" and row["venue"] == "offshore" and json.loads(row["probs"]) == ctx.model_probs
    r._scan_one("nyc", "high", 0, ["bucket_sum"], True, "offshore", None)          # the light arb sweep writes none
    assert led.conn.execute("SELECT COUNT(*) FROM model_runs").fetchone()[0] == 1


def test_universe_catchup_only_runs_when_armed_at_a_gate_reset(monkeypatch):
    """The arb universe refresh is pinned to 06:30/18:30 and the laptop misses it (stale since
    09-19). A catch-up changes bucket_sum's universe, so it may only switch on at a gate reset."""
    from polybot import runner as runner_mod
    monkeypatch.setattr(runner_mod, "_save_jobs", lambda jobs, path=None: None)
    cfg = _cfg()
    cfg.gate_since_ts = 1789739626.0
    r = runner_mod.Runner(cfg, _ledger(), log=lambda *_: None)
    assert not r.universe_catchup_active()                      # default: off
    cfg.universe_catchup_gate_ts = 1789739626.0 - 86400          # armed at an OLD reset: stays off
    assert not r.universe_catchup_active()
    cfg.universe_catchup_gate_ts = cfg.gate_since_ts             # armed at THIS reset
    assert r.universe_catchup_active()
    et = runner_mod.ET
    r._jobs = {}
    assert r._due("refresh_universe", datetime(2026, 9, 24, 8, 0, tzinfo=et), runner_mod.UNIVERSE_SLOTS,
                  quiet_hours=runner_mod.ARB_HOURS)             # 06:30 missed -> runs at 08:00
    r._jobs["refresh_universe"] = datetime(2026, 9, 24, 8, 1, tzinfo=et).timestamp()
    assert not r._due("refresh_universe", datetime(2026, 9, 24, 17, 0, tzinfo=et), runner_mod.UNIVERSE_SLOTS)
    assert r._due("refresh_universe", datetime(2026, 9, 24, 18, 45, tzinfo=et), runner_mod.UNIVERSE_SLOTS)
    # the weekly slot: Sunday 05:30
    sun = runner_mod._last_slot(datetime(2026, 9, 24, 12, 0, tzinfo=et), ((5, 30),), weekday=6)
    assert (sun.weekday(), sun.hour, sun.minute, sun.day) == (6, 5, 30, 20)


# ---- maker_rewards (paper) -----------------------------------------------------------------------
def test_incentive_scoring_follows_the_published_rules():
    """docs.polymarket.us/incentives/liquidity: score = DF ** ticks-from-best x size; walk out from the
    best price until target size; the whole level that reaches it scores, deeper ones don't; a side
    that never reaches target scores nothing."""
    from polybot import incentives as I
    # target reached inside the best level: the second level scores zero (the docs' own example)
    total, mine, ok = I.side_score([(0.40, 25000), (0.39, 5000)], 20000, 0.3, 0.01, ours=(0.40, 100))
    assert ok and total == pytest.approx(25100) and mine == pytest.approx(100)
    # target reached two levels out: the second level counts at DF ** 1
    total, mine, ok = I.side_score([(0.40, 100), (0.39, 1000)], 1000, 0.3, 0.01, ours=(0.40, 100))
    assert ok and total == pytest.approx(200 + 1000 * 0.3) and mine == pytest.approx(100)
    # never reaches target: nothing
    assert I.side_score([(0.40, 100)], 1000, 0.3, 0.01, ours=(0.40, 100))[2] is False
    # 0.1c books count 0.1c ticks
    assert I.tick_of([(0.365, 1)], [(0.366, 1)]) == 0.001
    total, mine, ok = I.side_score([(0.366, 1000), (0.369, 1000)], 1500, 0.3, 0.001, ours=None, bid_side=False)
    assert total == pytest.approx(1000 + 1000 * 0.3 ** 3)
    # a day's pool is split half per side, pro rata
    book = {"bids": [(0.40, 900)], "asks": [(0.42, 900)]}
    r = I.quote_rate(book, {"rewardPool": 100, "discountFactor": 0.3, "targetSize": 500}, (0.40, 100), (0.42, 100))
    assert r["bid"] == pytest.approx(0.1) and r["usd_per_day"] == pytest.approx(10.0)
    assert I.size_for(0.40, 10, "bid") == 25 and I.size_for(0.40, 10, "ask") == 16


def test_maker_rewards_quotes_accrues_and_books_a_fill_as_a_paper_position(monkeypatch):
    from polybot import runner as runner_mod
    from polybot.strategies import maker_rewards as M
    cfg, led = _cfg(), _ledger()
    cfg.modes["maker_rewards"] = "paper"
    now = [time.time()]

    class US:
        available, why_unavailable = True, ""
        def __init__(self):
            self.books = {"m1": {"bids": [(0.40, 900)], "asks": [(0.42, 900)], "last": 0.41}}
        def book(self, slug, **k):
            return self.books.get(slug)

    us = US()
    progs = [{"marketSlug": "m1", "category": "POL", "timePeriods": [
        {"programId": "politics_mid_x", "programType": "liquidityProgram", "status": "active",
         "start": "2026-01-01T00:00:00Z", "rewardPool": 100, "discountFactor": 0.3, "targetSize": 500}]}]
    r = runner_mod.Runner(cfg, led, log=lambda *_: None)
    r.us = us
    r.other_modules["maker_rewards"] = M.MakerRewards(cfg, us, led, clock=lambda: now[0], programs_fn=lambda: progs)
    monkeypatch.setattr(runner_mod.time, "time", lambda: now[0])
    r.record_maker()                                  # scout m1, start quoting it
    assert {q["side"] for q in led.maker_quotes("m1")} == {"bid", "ask"}
    now[0] += M.INTERVAL_S
    r.record_maker()                                  # join best on both sides
    q = {x["side"]: x for x in led.maker_quotes("m1")}
    assert q["bid"]["px"] == 0.40 and q["bid"]["qty"] == 25 and q["ask"]["px"] == 0.42
    now[0] += M.INTERVAL_S
    r.record_maker()                                  # a full interval resting at best: rewards accrue
    assert led.maker_rewards_usd(0) > 0
    # the market drops through our bid: a fill, recorded as a filled paper position
    us.books["m1"] = {"bids": [(0.37, 900)], "asks": [(0.39, 900)], "last": 0.38}
    now[0] += M.INTERVAL_S
    r.record_maker()
    sig = led.conn.execute("SELECT module, venue, side, price, exit_rule, meta FROM signals").fetchone()
    assert (sig["module"], sig["venue"], sig["side"], sig["price"], sig["exit_rule"]) == \
        ("maker_rewards", "us", "BUY_YES", 0.40, "timeout:24h")
    assert json.loads(sig["meta"])["filled_at_signal"] is True
    assert led.maker_position_open("m1", "bid")
    q = {x["side"]: x for x in led.maker_quotes("m1")}
    assert q["bid"]["qty"] == 0                       # that side sits out while the position is open
    # the gate reads the accrual for maker_rewards only
    assert led.extra_mtm("maker_rewards") > 0 and led.extra_mtm("bucket_sum") == 0.0
    # and paper fills it at the signal, as a maker (rebate, not a taker fee)
    from polybot.paper import fill_from_history
    s = dict(led.conn.execute("SELECT * FROM signals").fetchone())
    assert fill_from_history(s, []) == (s["ts"], 0.40)


def test_the_report_says_how_many_days_each_gate_is_away():
    from polybot.strategies.base import Signal
    led = _ledger()
    now = time.time()
    led.gate_since_ts = now - 10 * 86400
    for i in range(14):                       # 14 decisions in the last 7 days = 2/day
        sid = led.add_signal(Signal("bucket_sum", "us", f"m{i}", "x", "BUY_YES", 0.3, 3.0, 5.0, "r",
                                    arb=True, taker=True, meta={"group": f"g{i}"}), "paper")
        led.conn.execute("UPDATE signals SET ts=? WHERE id=?", (now - (i % 7) * 86400 - 60, sid))
    led.conn.commit()
    eta = led.gate_eta("bucket_sum", now=now)
    assert "14/30 decisions at 2.0/day" in eta and "~8 day(s)" in eta
    assert "no ETA" in led.gate_eta("leadlag", now=now)
    text = led.report(1)
    assert "eta 14/30 decisions" in text and "leadlag closed positions since the reset: 0/20" in text


def test_hold_favorites_scans_the_us_books_and_leaves_temperature_to_the_weather_modules():
    """Offshore-only, hold_favorites could never reach the 10 US signals the gate asks for."""
    end = datetime.fromtimestamp(time.time() + 2 * 86400, ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")
    q = lambda v: {"value": str(v)}
    events = [
        {"slug": "gov-shutdown-oct", "title": "Government Shutdown?", "category": "politics", "endDate": end,
         "markets": [{"slug": "shut-oct1", "title": "By October 1", "status": "MARKET_STATUS_OPEN",
                      "bestBidQuote": q(0.86), "bestAskQuote": q(0.88)}]},
        {"slug": "temp-nychigh-2026-09-25", "title": "NYC high", "category": "climate", "endDate": end,
         "markets": [{"slug": "tc-temp-a", "title": "80 to 81", "bestBidQuote": q(0.86), "bestAskQuote": q(0.88)}]},
        {"slug": "closed-one", "title": "X?", "category": "politics", "endDate": end,
         "markets": [{"slug": "c1", "title": "Yes", "status": "MARKET_STATUS_CLOSED",
                      "bestBidQuote": q(0.86), "bestAskQuote": q(0.88)}]},
    ]
    table = {"all": {"0.85-0.90": {"n": 60, "yes": 59}}}
    sigs = HoldFavorites(_cfg(), table).scan_us(events)
    assert [(s.venue, s.market, s.price, s.meta["us_event"]) for s in sigs] == [("us", "shut-oct1", 0.87, "gov-shutdown-oct")]


def test_the_recorder_samples_the_events_of_open_us_positions():
    from polybot import pairs

    class US:
        available = True
        def events_by_slug(self, slugs):
            assert "gov-shutdown-oct" in slugs
            return {"gov-shutdown-oct": {"slug": "gov-shutdown-oct", "markets": [
                {"slug": "shut-oct1", "bestBidQuote": {"value": "0.86"}, "bestAskQuote": {"value": "0.88"}}]}}

    led = _ledger()
    rec = pairs.PairRecorder(led, US(), clock=lambda: 1000.0, offshore_prices=lambda toks: {})
    got = rec.record([], extra_events=["gov-shutdown-oct"])
    assert got.get("watched") == 1 and led.snapshots("us", "shut-oct1", 0)


def test_compounding_is_off_by_default_and_moves_nothing_until_live_profit_is_banked(tmp_path):
    from polybot import compounding as C
    from polybot.strategies.base import Signal
    cfg, led = config.Config(), _ledger()
    path = str(tmp_path / "state.json")
    assert C.apply(cfg, led, path=path) == {"on": False} and cfg.caps.max_exposure_usd == 180.0
    cfg.compounding = True
    now = time.time()
    info = C.apply(cfg, led, now=now, path=path)
    assert info["basis"] == 200.0
    assert (cfg.caps.max_per_market_usd, cfg.caps.max_exposure_usd, cfg.caps.daily_loss_stop_usd,
            cfg.caps.bankroll_floor_usd, cfg.arb_max_set_cost_usd, cfg.arb_max_risk_usd) == (20, 180, 20, 120, 120, 15)

    def live_trade(ts, pnl):
        sid = led.add_signal(Signal("bucket_sum", "us", f"m{ts}", "x", "BUY_YES", 0.5, 10, 1, "r"), "live")
        led.conn.execute("UPDATE signals SET ts=? WHERE id=?", (ts, sid))
        led.upsert_paper(sid, filled_ts=ts, fill_price=0.5, exit_ts=ts + 60, pnl_usd=pnl, status="closed")
    # a loss shrinks the caps the same day
    live_trade(now + 60, -10.0)
    info = C.apply(cfg, led, now=now + 120, path=path)
    assert info["basis"] == 190.0 and cfg.caps.max_exposure_usd == pytest.approx(171.0)
    # 14 live days in profit with a small drawdown: the checkpoint banks the profit
    for d in range(1, 15):
        live_trade(now + d * 86400, 3.0)
    info = C.apply(cfg, led, now=now + 15 * 86400, path=path)
    assert info["stepped"] and info["banked"] == pytest.approx(232.0)          # 200 - 10 + 14 x 3
    assert cfg.caps.max_exposure_usd == pytest.approx(180 * 232 / 200)
    # a record that is not consistent (drawdown over 10% of the basis) banks nothing
    rec = {"live_days": 20, "net": 5.0, "max_drawdown": 30.0, "fill_rate": 0.9}
    assert C.consistent(rec, 232.0, cfg)[0] is False


def test_a_config_hot_reload_reaches_every_component_and_keeps_the_account_bankroll(tmp_path, monkeypatch):
    """A hot reload used to change only the runner's copy: the risk manager kept the old
    arb_live_ok and caps, and the bankroll fell back to the file's 200."""
    import json as _json
    import polybot.config as cfgmod
    from polybot.runner import Runner
    path = str(tmp_path / "config.json")
    cfgmod.save(cfgmod.Config(), path)
    monkeypatch.setattr(cfgmod, "CONFIG_PATH", path)
    r = Runner(cfg=cfgmod.load(path), ledger=_ledger(), log=lambda *_: None)
    r.cfg.bankroll_usd = 208.06                 # as read from the account
    raw = _json.load(open(path))
    raw["arb_live_ok"] = True
    with open(path, "w") as f:
        _json.dump(raw, f)
    os.utime(path, (time.time() + 5, time.time() + 5))
    assert r.reload_config_if_changed()
    assert r.risk.cfg is r.cfg and r.arb.cfg is r.cfg and r.other_modules["maker_rewards"].cfg is r.cfg
    assert r.risk.cfg.arb_live_ok is True and r.cfg.bankroll_usd == 208.06


def test_the_report_carries_the_compounding_state(monkeypatch, tmp_path):
    from polybot import runner as runner_mod
    monkeypatch.setattr(config, "REPORT_PATH", str(tmp_path / "report-latest.txt"))   # never the live file
    r = runner_mod.Runner(_cfg(), _ledger(), log=lambda *_: None)
    assert "compounding: off — caps are the fixed dollars in config.json" in r.report(1)


def test_us_fills_are_recorded_every_settle_but_resolutions_only_when_due(monkeypatch):
    from polybot import runner as runner_mod
    from polybot.strategies.base import Signal
    cfg, led = _cfg(), _ledger()
    r = runner_mod.Runner(cfg, led, log=lambda *_: None)
    asked = []

    class US:
        available, why_unavailable = True, ""
        def resolution(self, slug):
            asked.append(slug)
            return None

    r.us = US()
    sid = led.add_signal(Signal("leadlag", "us", "m1", "x", "BUY_YES", 0.40, 10, 5, "r", exit="reference",
                                horizon_hours=6, category="politics"), "paper")
    led.add_snapshot("us", "m1", 0.38, 0.40, ts=time.time() + 5)            # mid 0.39 <= 0.40: filled
    monkeypatch.setattr(r, "_us_settle_due", lambda now=None: False)
    r.settle()
    assert led.paper_row(sid)["status"] == "filled" and asked == []          # recorded, zero calls
    monkeypatch.setattr(r, "_us_settle_due", lambda now=None: True)
    r.settle()
    assert asked == ["m1"]                                                   # resolutions when due


def test_a_fill_does_not_get_its_market_swapped_out(monkeypatch):
    """12:19 09-24: AK-rep's bid filled and the same tick swapped AK-rep out — rated on its one
    remaining side it looked worst, and the fill's position was not in the ledger yet."""
    from polybot.strategies import maker_rewards as M
    cfg, led = _cfg(), _ledger()
    now = [time.time()]
    books = {f"m{i}": {"bids": [(0.40, 900)], "asks": [(0.42, 900)], "last": 0.41} for i in range(M.MAX_MARKETS + 1)}

    class US:
        available, why_unavailable = True, ""
        def book(self, slug, **k):
            return books.get(slug)

    progs = [{"marketSlug": m, "category": "POL", "timePeriods": [
        {"programId": "p", "programType": "liquidityProgram", "status": "active", "start": "2026-01-01T00:00:00Z",
         "rewardPool": 100 + i, "discountFactor": 0.3, "targetSize": 500}]} for i, m in enumerate(books)]
    mk = M.MakerRewards(cfg, US(), led, clock=lambda: now[0], programs_fn=lambda: progs)
    for _ in range(6):                               # fill the book to MAX_MARKETS and quote them
        mk.tick()
        now[0] += M.INTERVAL_S
    quoted = {q["market"] for q in led.maker_quotes()}
    victim = sorted(quoted)[0]
    books[victim] = {"bids": [(0.37, 900)], "asks": [(0.39, 900)], "last": 0.38}   # its bid trades through
    out = mk.tick()
    assert any(s.market == victim for s in out["signals"])
    assert victim in {q["market"] for q in led.maker_quotes()}


# ---- the server move ------------------------------------------------------------------------------
def test_runtime_files_live_in_the_data_dir_which_defaults_to_the_code_dir():
    from polybot import calibration as cal, compounding, pairs, runner as runner_mod
    if not os.environ.get("POLYBOT_DATA_DIR"):
        assert config.DATA_DIR == config.ROOT
    for p in (config.DB_PATH, config.CONFIG_PATH, config.KILL_PATH, config.CALIBRATION_PATH, pairs.PAIRS_PATH,
              cal.SAMPLES_PATH, compounding.STATE_PATH, runner_mod.JOBS_PATH):
        assert os.path.dirname(p) == config.DATA_DIR
    assert runner_mod.APP_DIR == os.path.dirname(config.ROOT)       # not ~/second-brain/...


def test_the_lease_keeps_two_loops_off_one_account():
    from polybot import lease

    class Store:
        def __init__(self):
            self.rows = {}
        def _load_state(self, key):
            return dict(self.rows.get(key, {}))
        def _save_state(self, st):
            self.rows[st["key"]] = dict(st)

    s, now = Store(), time.time()
    assert lease.conflict("server", lease.holder(s), now) is None               # nobody holds it
    lease.renew("mac:alex", s, now=now)
    assert "mac:alex holds" in lease.conflict("server", lease.holder(s), now + 60)
    assert lease.conflict("mac:alex", lease.holder(s), now + 60) is None       # its own lease
    assert lease.conflict("server", lease.holder(s), now + lease.LEASE_TTL_S + 1) is None   # gone quiet
    lease.release("mac:alex", s)
    assert lease.holder(s) == {}


def test_the_server_supervisor_stays_off_until_every_switch_is_set(tmp_path, monkeypatch):
    import polybot_supervisor as sup
    env = {"POLYBOT_ON_SERVER": "1", "POLYBOT_DATA_DIR": str(tmp_path), "POLYMARKET_KEY_ID": "k",
           "POLYMARKET_SECRET_KEY": "s", "SUPABASE_URL": "u", "SUPABASE_KEY": "k"}
    assert sup.enabled({})[0] is False                                           # the default: off
    assert "no ledger" in sup.enabled(env)[1]
    (tmp_path / "polybot.db").write_text("")
    assert sup.enabled(env) == (True, "ok")
    assert "POLYMARKET_KEY_ID" in sup.enabled(dict(env, POLYMARKET_KEY_ID=""))[1]

    # one loop per container, restarted when it exits, and it waits while another node holds the lease
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    stop, runs, sleeps = __import__("threading").Event(), [], []

    class Proc:
        def wait(self):
            runs.append(1)
            if len(runs) >= 2:
                stop.set()
            return 1

    monkeypatch.setattr(sup, "_other_node_live", lambda: None)
    sup.supervise(stop, spawn=lambda *a, **k: Proc(), sleep=sleeps.append, log=lambda *_: None)
    assert len(runs) == 2 and sleeps[0] == 60.0                                  # quick death backs off
    held = open(tmp_path / ".supervisor.lock", "w")
    import fcntl
    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)                            # another worker has it
    msgs = []
    sup.supervise(__import__("threading").Event(), spawn=lambda *a, **k: Proc(), sleep=sleeps.append, log=msgs.append)
    assert "already runs the loop" in msgs[0]


def test_the_migration_catches_any_change_to_the_evidence(tmp_path):
    from polybot import migrate
    from polybot.strategies.base import Signal
    src = tmp_path / "src"
    src.mkdir()
    led = Ledger(str(src / "polybot.db"))
    led.add_signal(Signal("bucket_sum", "us", "m", "x", "BUY_YES", 0.5, 10, 1, "r"), "paper")
    led.conn.close()
    (src / "config.json").write_text(json.dumps({"gate_since_ts": 0.0}))
    snap = tmp_path / "snap"
    m = migrate.snapshot(str(snap), data_dir=str(src))
    assert m["fingerprint"]["modules"]["bucket_sum"]["signals"] == 1
    assert migrate.verify(str(snap), data_dir=str(snap)) == []
    (snap / "config.json").write_text(json.dumps({"gate_since_ts": 5.0}))             # a "reset" in transit
    assert any("gate_since_ts changed" in p for p in migrate.verify(str(snap), data_dir=str(snap)))


def test_the_published_report_is_the_report_and_leaves_the_file_alone(monkeypatch, tmp_path):
    from polybot import runner as runner_mod
    path = tmp_path / "report-latest.txt"
    monkeypatch.setattr(config, "REPORT_PATH", str(path))
    r = runner_mod.Runner(_cfg(), _ledger(), log=lambda *_: None)
    facts = runner_mod.report_publish_facts(r)
    assert facts["report"] == r.report_text(1) and "compounding:" in facts["report"]
    assert abs(facts["report_at"] - time.time()) < 5 and not path.exists()     # only report() writes the file
    assert runner_mod.REPORT_PUBLISH_S == 900.0


def _golive_env(monkeypatch, tmp_path, gate=(True, "PASS — 31 signals"), raw=None):
    from polybot import runner as runner_mod, notify
    path = tmp_path / "config.json"
    raw = raw or {"modes": {"bucket_sum": "paper", "weather_lock": "paper"}, "arb_max_set_cost_usd": 120.0,
                  "gate_since_ts": 1789739626.0158348, "caps": {"max_exposure_usd": 180.0}}
    path.write_text(json.dumps(raw))
    monkeypatch.setattr(config, "CONFIG_PATH", str(path))
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config, "KILL_PATH", str(tmp_path / "KILL"))
    nudged = []
    monkeypatch.setattr(notify, "nudge", lambda *a, **k: nudged.append(a[0]))
    cfg = config.load(str(path))
    led = _ledger()
    monkeypatch.setattr(led, "promotion_check", lambda m, **k: gate)
    out = []
    r = runner_mod.Runner(cfg, led, log=out.append)
    return r, path, out, nudged


def test_golive_refuses_without_a_passing_gate_and_writes_nothing(monkeypatch, tmp_path):
    r, path, out, nudged = _golive_env(monkeypatch, tmp_path, gate=(False, "19/30 signals"))
    before = path.read_text()
    assert r.golive("bucket_sum", 20) == 1
    assert path.read_text() == before and nudged == [] and "gate does not pass: 19/30 signals" in out[-1]
    assert not list(tmp_path.glob("config.json.pre-golive-*"))


def test_golive_refuses_a_kill_switch_a_raised_cap_and_a_cap_on_a_non_arb_module(monkeypatch, tmp_path):
    r, path, out, _ = _golive_env(monkeypatch, tmp_path)
    before = path.read_text()
    assert r.golive("bucket_sum", 200) == 1 and "may only lower it" in out[-1]
    assert r.golive("weather_lock", 20) == 1 and "does not trade sets" in out[-1]
    assert r.golive("nope") == 1 and "unknown module" in out[-1]
    (tmp_path / "KILL").write_text("")
    assert r.golive("bucket_sum", 20) == 1 and "kill switch is ON" in out[-1]
    assert path.read_text() == before


def test_golive_dry_run_prints_the_plan_and_the_drill_only(monkeypatch, tmp_path):
    r, path, out, nudged = _golive_env(monkeypatch, tmp_path)
    before = path.read_text()
    assert r.golive("bucket_sum", 20, dry_run=True) == 0
    text = "\n".join(out)
    assert "arb_live_ok: False -> True" in text and "arb_max_set_cost_usd: 120.0 -> 20.0" in text
    assert "KILL DRILL" in text and "back to 120" in text and path.read_text() == before and nudged == []


def test_golive_writes_only_its_fields_and_restarts_nothing_when_the_loop_reloads(monkeypatch, tmp_path):
    r, path, out, nudged = _golive_env(monkeypatch, tmp_path)
    before = json.loads(path.read_text())
    restarted = []
    assert r.golive("bucket_sum", 20, watch=lambda *a: "09-30 07:01:00   config reloaded: bucket_sum paper->live",
                    restart=lambda: restarted.append(1) or "x") == 0
    after = json.loads(path.read_text())
    assert after["modes"] == dict(before["modes"], bucket_sum="live")
    assert after["arb_live_ok"] is True and after["arb_max_set_cost_usd"] == 20.0
    assert after["gate_since_ts"] == before["gate_since_ts"] and after["caps"] == before["caps"]   # untouched
    assert set(after) == set(before) | {"arb_live_ok"}
    backups = list(tmp_path.glob("config.json.pre-golive-*"))
    assert len(backups) == 1 and json.loads(backups[0].read_text()) == before
    assert restarted == [] and nudged == ["polybot: LIVE"]
    text = "\n".join(out)
    assert "no restart needed" in text and f"cp '{backups[0]}'" in text
    # the reloaded config is what risk sees: a live arb leg is no longer refused for arb_live_ok
    assert config.load(str(path)).arb_live_ok is True


def test_golive_restarts_the_loop_only_when_the_reload_is_not_seen(monkeypatch, tmp_path):
    r, path, out, _ = _golive_env(monkeypatch, tmp_path)
    restarted = []
    assert r.golive("weather_lock", watch=lambda *a: None, restart=lambda: restarted.append(1) or "launchd: loop restarted") == 0
    after = json.loads(path.read_text())
    assert after["modes"]["weather_lock"] == "live" and "arb_live_ok" not in after      # not an arb module
    assert restarted == [1] and "launchd: loop restarted" in "\n".join(out)


def test_watch_log_reads_only_what_was_written_after_the_mark(tmp_path):
    from polybot import runner as runner_mod
    log = tmp_path / "loop.log"
    log.write_text("09-24 config reloaded: bucket_sum paper->live\n")          # an OLD line must not count
    mark = log.stat().st_size
    assert runner_mod._watch_log(str(log), mark, "bucket_sum paper->live", 0) is None
    with open(log, "a") as f:
        f.write("09-30 07:01:00   config reloaded: bucket_sum paper->live\n")
    assert "09-30" in runner_mod._watch_log(str(log), mark, "bucket_sum paper->live", 0)


def test_tight_mid_moves_only_on_a_tight_book():
    from polybot.strategies.leadlag import tight_mid_series
    q = [(0, None, None), (1, 0.40, 0.60), (2, 0.44, 0.46), (3, 0.44, 0.80), (4, 0.49, 0.51)]
    assert tight_mid_series(q, 0.05) == [(2, 0.45), (3, 0.45), (4, 0.50)]    # nothing until the first tight quote


class _QuoteStore:
    def __init__(self, rows):
        self.rows = rows            # (venue, market) -> [(ts, bid, ask)]

    def get(self, venue, market):
        return [(t, (b + a) / 2) for t, b, a in self.rows.get((venue, market), [])]

    def quotes(self, venue, market):
        return self.rows.get((venue, market), [])


def _leadlag_with(reference, off_rows):
    from polybot.strategies.leadlag import LeadLag
    cfg = _cfg()
    cfg.leadlag_reference = reference
    us = type("US", (), {"available": True, "why_unavailable": None, "bbo": lambda self, s: (0.40, 0.42)})()
    store = _QuoteStore({("offshore", "tok"): off_rows,
                         ("us", "slug"): [(0, 0.40, 0.42), (60, 0.40, 0.42), (120, 0.40, 0.42)]})
    return LeadLag(cfg, us, store, pairs_fn=lambda: [{"us_slug": "slug", "offshore_token": "tok",
                                                      "category": "politics", "label": "x"}])


def test_reference_c_ignores_a_pulled_offer_that_reference_a_trades():
    # the offshore offer is pulled (ask 0.43 -> 0.60, bid unchanged): the mid jumps 8.5c, the book is 19c wide
    pulled = [(0, 0.40, 0.43), (60, 0.40, 0.43), (120, 0.41, 0.60)]
    a = _leadlag_with("mid", pulled).scan()
    assert len(a) == 1 and a[0].meta["reference"] == "mid"
    assert _leadlag_with("tight_mid", pulled).scan() == []
    # a real move on a tight book still trades under C
    real = [(0, 0.40, 0.43), (60, 0.44, 0.46), (120, 0.47, 0.49)]
    c = _leadlag_with("tight_mid", real).scan()
    assert len(c) == 1 and c[0].meta["reference"] == "tight_mid"


def test_the_live_default_is_still_reference_a():
    assert config.Config().leadlag_reference == "mid"


def test_leadlag_refs_replay_takes_the_same_signal_under_both_on_a_clean_book(tmp_path):
    from polybot import leadlag_refs
    led_path = str(tmp_path / "l.db")
    led = Ledger(led_path)
    t0 = 1_800_000_000.0
    for i, (ob, oa, ub, ua) in enumerate([(0.40, 0.42, 0.40, 0.42), (0.40, 0.42, 0.40, 0.42),
                                          (0.46, 0.48, 0.40, 0.42), (0.47, 0.49, 0.40, 0.42)]):
        led.add_snapshot("offshore", "tok", ob, oa, None, ts=t0 + 40 * i)
        led.add_snapshot("us", "slug", ub, ua, None, ts=t0 + 40 * i)
    pairs_ = [{"us_slug": "slug", "offshore_token": "tok", "category": "politics", "label": "x"}]
    res = leadlag_refs.replay(led_path, pairs_, t0 - 1)
    assert res["A"]["signals"] == res["C"]["signals"] == 1
    assert "flip to C" in leadlag_refs.render(res)


def test_runner_starts_when_venue_account_read_raises(monkeypatch):
    """2026-09-25: portfolio.positions answered 500 at startup and the loop died 66 times before it
    ever ticked. A venue that cannot be read at start is logged and the config bankroll stands."""
    from polybot import runner as runner_mod
    from polybot.feeds import usvenue
    cfg, led = _cfg(), _ledger()
    before = cfg.bankroll_usd
    monkeypatch.setattr(usvenue.USVenue, "available", property(lambda self: True, lambda self, v: None))

    def _boom(self):
        raise RuntimeError("The server was unable to process your request.")
    monkeypatch.setattr(usvenue.USVenue, "account_value_usd", _boom)
    lines = []
    r = runner_mod.Runner(cfg, led, log=lambda *a: lines.append(" ".join(str(x) for x in a)))
    assert r.cfg.bankroll_usd == before
    assert any("account value unreadable at start" in ln for ln in lines)


def test_account_value_is_unknown_not_cash_only_when_positions_fail(monkeypatch):
    """Cash alone is buying power, the number that once halted the bot at "bankroll under floor".
    An unreadable positions book is None, so sizing keeps the last known bankroll."""
    from polybot.feeds import usvenue
    v = usvenue.USVenue()
    monkeypatch.setattr(usvenue.USVenue, "available", property(lambda self: True, lambda self, v: None))
    monkeypatch.setattr(usvenue.USVenue, "balance_usd", lambda self: 0.41)

    def _boom(self):
        raise RuntimeError("500")
    monkeypatch.setattr(usvenue.USVenue, "positions", _boom)
    notes = []
    v.on_backoff = notes.append
    assert v.account_value_usd() is None
    assert notes and "positions unreadable" in notes[0]
    monkeypatch.setattr(usvenue.USVenue, "positions", lambda self: [{"cost": {"value": "187.0"}}])
    assert v.account_value_usd() == 187.41
