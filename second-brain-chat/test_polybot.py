"""polybot unit tests — no network. Run: python3 -m pytest test_polybot.py -q"""
import json
import math
import os
import tempfile
import time
from datetime import datetime
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
def test_us_fee_schedule_matches_docs():
    assert fees.us_taker_fee(0.5, 100) == pytest.approx(1.50)
    assert fees.us_maker_rebate(0.5, 100) == pytest.approx(0.3125)
    assert fees.leg_cost(0.5, 100, "us", maker=True) == pytest.approx(-0.3125)
    assert fees.leg_cost(0.5, 100, "offshore", maker=True) == 0.0
    assert fees.leg_cost(0.5, 100, "offshore", maker=False, category="weather") == pytest.approx(1.25)
    # a taken round trip at 50c on US costs 3c per contract; two maker legs earn 0.6c
    assert fees.swing_breakeven_cents(0.5, "us", False, False, spread_cents=0) == pytest.approx(3.0)
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


def test_weather_lock_requires_peak_passed_and_falling_obs():
    cfg = _cfg()
    obs = [("2026-09-12T15:51:00+00:00", 76), ("2026-09-12T18:51:00+00:00", 79), ("2026-09-12T19:51:00+00:00", 78), ("2026-09-12T20:51:00+00:00", 77)]
    hourly = [("2026-09-12T17:00", 74.0), ("2026-09-12T18:00", 73.0), ("2026-09-12T20:00", 70.0)]
    ctx = _ctx(obs=obs, hourly=hourly)
    locked, winner = lock_state(ctx)
    assert locked and winner.title == "78-79°F"
    sigs = WeatherLock(cfg).scan(ctx)
    assert len(sigs) == 1 and sigs[0].side == "BUY_YES" and sigs[0].price == 0.68 and sigs[0].size_usd == 20.0
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
    for b in ev.buckets:
        b.best_bid = b.best_bid or 0.001
    asks_sum = sum(b.best_ask for b in ev.buckets)
    assert asks_sum > 1.0
    for b in ev.buckets:                                               # make the asks sum to 0.90
        b.best_ask = round(b.best_ask * 0.90 / asks_sum, 4)
    kind, net, prices = arb_check(ev.buckets, "us")
    assert kind == "buy_all" and 5.0 < net < 10.0 and len(prices) == len(ev.buckets)   # 10c gross minus ~3.3c taker fees
    sigs = BucketSum(_cfg()).scan(_ctx(event=ev))
    assert len(sigs) == len(ev.buckets) and all(s.taker and s.arb for s in sigs)


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
    ok, why = led.promotion_check("weather_obs")
    assert ok and "mtm +15.00" in why                               # closed +20, open marked -5
    # the same closed net with the open positions deep underwater is a hold
    for sid in ids[20:]:
        led.upsert_paper(sid, pnl_usd=-3.0)
    ok, why = led.promotion_check("weather_obs")
    assert not ok and why.startswith("mark-to-market -10.00")
    for sid in ids[20:]:
        led.upsert_paper(sid, pnl_usd=0.5)
    assert led.promotion_check("weather_obs")[0]
    # a losing US-venue paper record blocks promotion even when the offshore proxy passes
    sid = led.add_signal(Signal("weather_obs", "us", "slug", "x", "BUY_NO", 0.9, 10, 10, "r", ts=t0), "paper")
    led.upsert_paper(sid, filled_ts=t0, fill_price=0.1, status="filled", pnl_usd=-2.0)
    ok, why = led.promotion_check("weather_obs")
    assert not ok and why.startswith("US paper mark-to-market -2.00")
    led.upsert_paper(sid, pnl_usd=0.4)
    ok, why = led.promotion_check("weather_obs")
    assert ok and "US 1 signals +0.40" in why
    assert "Polymarket US books only" in led.report(1)
    assert "gate PASS: weather_obs" in led.summary(1)
    # evidence before a rule change stops counting
    led.gate_since_ts = time.time() + 1
    assert led.promotion_check("weather_obs") == (False, "no signals")
    led.gate_since_ts = 0.0
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
