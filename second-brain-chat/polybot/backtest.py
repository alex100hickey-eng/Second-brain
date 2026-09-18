"""Backtest the weather modules on real past days, before a single dollar is live.

For each city × day: the offshore event (with its resolved outcome), every bucket's price history
(CLOB, 5-minute points), the station's hourly observations (METAR / NWS) and the forecasts that
existed at the time (Open-Meteo forecast archive + previous-run hourly). The day is replayed hour
by hour at :55 with the SAME strategy code the loop runs; every signal is paper-filled against
the prices that followed and settled on the real outcome.

Also answers the open question "does the offshore hourly rule read ~1°F under the daily max?":
the morning model probability of the eventual winner is scored for several discount values.
"""
from __future__ import annotations

import copy
import json
import math
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import config, paper
from .feeds import metar, offshore, weather
from .strategies.weather import WeatherHold, WeatherLock, WeatherObs, build_ctx

SPREAD = 0.01          # assumed half-spread around the recorded price (live books showed ~2c spreads)
SCAN_HOURS = {"high": range(6, 24), "low": range(2, 24)}
DISCOUNTS = (0.0, 0.5, 1.0, 1.5)


def price_at(history, ts: float):
    p = None
    for t, v in history:
        if t <= ts:
            p = v
        else:
            break
    return p


def _synthetic_event(event, histories: dict, ts: float):
    ev = copy.deepcopy(event)
    for b in ev.buckets:
        p = price_at(histories.get(b.yes_token, []), ts)
        if p is None:
            b.best_bid = b.best_ask = None
            continue
        b.best_bid = round(max(0.01, p - SPREAD), 2)
        b.best_ask = round(min(0.99, p + SPREAD), 2)
        b.last = p
        b.closed = False
    return ev


def fetch_day(city: str, date: datetime, kind: str, log=print) -> dict | None:
    meta = config.city_meta(city)
    if meta is None:
        return None
    event = offshore.find_weather_event(city, meta["query"], date, kind)
    if event is None or not event.buckets or not any(b.outcome is not None for b in event.buckets):
        return None
    winner = next((b for b in event.buckets if b.outcome == 1), None)
    if winner is None:
        return None
    tz = meta["tz"]
    station = event.station or meta["station"]
    coords = config.STATIONS.get(station, {"lat": meta["lat"], "lon": meta["lon"]})
    start, end = weather.local_day_bounds(event.date, tz)
    histories = {}
    for b in event.buckets:
        try:
            histories[b.yes_token] = offshore.prices_history(b.yes_token, fidelity=5,
                                                             since_ts=start.timestamp() - 3600 * 12)
        except Exception as exc:
            log(f"    history {b.title}: {exc}")
        time.sleep(0.05)
    hours_back = int((datetime.now(ZoneInfo(tz)) - start).total_seconds() // 3600) + 2
    try:
        if station.startswith("K"):
            obs = weather.observations(station, start.isoformat(), end.isoformat())
        else:
            obs = metar.observations(station, hours=hours_back, unit=event.unit)
    except Exception as exc:
        log(f"    obs {station}: {exc}")
        obs = []
    try:
        arch = weather.historical_members(coords["lat"], coords["lon"], tz, event.date, event.date, unit=event.unit)
        members = arch.get(event.date, {}).get("max" if kind == "high" else "min", [])
    except Exception as exc:
        log(f"    archive: {exc}")
        members = []
    try:
        hourly = weather.previous_run_hourly(coords["lat"], coords["lon"], tz, event.date, event.date, unit=event.unit)
    except Exception as exc:
        log(f"    previous-run: {exc}")
        hourly = []
    return {"city": city, "date": event.date, "kind": kind, "tz": tz, "station": station, "rule": event.rule,
            "event": event, "winner": winner.title, "histories": histories, "obs": obs, "members": members,
            "hourly": hourly, "unit": event.unit,
            "coverage": len([h for h in histories.values() if h]) / max(len(event.buckets), 1)}


def replay_day(day: dict, cfg: config.Config, log=print) -> dict:
    """Hour-by-hour replay. Returns {'signals': [...], 'discount_scores': {...}}."""
    tz = ZoneInfo(day["tz"])
    event, kind = day["event"], day["kind"]
    modules = {"weather_hold": WeatherHold(cfg), "weather_obs": WeatherObs(cfg), "weather_lock": WeatherLock(cfg)}
    seen, signals = set(), []
    day_start = datetime.strptime(day["date"], "%Y-%m-%d").replace(tzinfo=tz)
    for hour in SCAN_HOURS[kind]:
        now_local = day_start + timedelta(hours=hour, minutes=55)
        ts = now_local.timestamp()
        obs_until = [(iso, t) for iso, t in day["obs"] if datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() <= ts]
        ev = _synthetic_event(event, day["histories"], ts)
        ctx = build_ctx(day["city"], now_local, kind, cfg, venue="offshore",
                        fetch={"event": ev, "members": day["members"], "obs": obs_until, "hourly": day["hourly"],
                               "local_now": now_local})
        if ctx is None:
            continue
        for name, mod in modules.items():
            for sig in mod.scan(ctx):
                key = (name, sig.market, sig.side)
                if key in seen:
                    continue
                seen.add(key)
                bucket = next(b for b in event.buckets if b.yes_token == sig.market)
                d = {"ts": ts, "price": sig.price, "side": sig.side, "exit_rule": sig.exit, "horizon_h": sig.horizon_hours,
                     "contracts": sig.contracts, "taker": False}
                hist = [(t, p) for t, p in day["histories"].get(sig.market, []) if t >= ts]
                fill_ts, fill_px = paper.fill_from_history(d, hist)
                rec = {"module": name, "city": day["city"], "date": day["date"], "kind": kind, "hour": hour,
                       "bucket": bucket.title, "side": sig.side, "price": sig.price, "size_usd": sig.size_usd,
                       "edge_cents": sig.edge_cents, "filled": fill_ts is not None, "outcome": bucket.outcome,
                       "pnl": 0.0, "exit": None}
                if fill_ts is not None:
                    exit_ts, exit_px, ekind = paper.exit_from_history(d, fill_ts, hist)
                    pnl, fee = paper.pnl_usd(d, fill_px, exit_px, bucket.outcome, "offshore", "weather")
                    rec.update(pnl=round(pnl, 2), exit=ekind or "settle")
                signals.append(rec)
    # discount scoring: morning scan (first hour), probability the model gave the eventual winner
    scores = {}
    winner = next(b for b in event.buckets if b.title == day["winner"])
    for disc in DISCOUNTS:
        probs = weather.bucket_probs(day["members"], event.buckets, discount=disc if kind == "high" else 0.0)
        p_win = probs[event.buckets.index(winner)]
        scores[str(disc)] = round(math.log(max(p_win, 1e-4)), 3)
    return {"signals": signals, "discount_scores": scores, "rule": day["rule"]}


def run(days: int = 7, cities: list | None = None, kinds=("high",), cfg: config.Config | None = None,
        log=print, out_path: str | None = None) -> dict:
    cfg = cfg or config.load()
    cities = cities or config.all_city_slugs()
    all_signals, disc = [], {}
    days_done = 0
    coverage = []
    for city in cities:
        meta = config.city_meta(city)
        if not meta:
            continue
        tz = ZoneInfo(meta["tz"])
        for back in range(1, days + 1):
            date = datetime.now(tz) - timedelta(days=back)
            for kind in kinds:
                try:
                    day = fetch_day(city, date, kind, log)
                except Exception as exc:
                    log(f"  {city} {date.date()} {kind}: fetch error {exc}")
                    continue
                if day is None:
                    continue
                res = replay_day(day, cfg, log)
                days_done += 1
                coverage.append(day["coverage"])
                all_signals.extend(res["signals"])
                disc.setdefault(res["rule"], []).append(res["discount_scores"])
                net = sum(s["pnl"] for s in res["signals"])
                log(f"  {city} {day['date']} {kind} [{day['station']}/{day['rule']}] winner {day['winner']} "
                    f"obs={len(day['obs'])} members={len(day['members'])} signals={len(res['signals'])} net=${net:+.2f}")
    summary = summarize(all_signals, disc, days_done,
                        liquidity_reality(max_spread_cents=cfg.lock_max_spread_cents),
                        coverage=sum(coverage) / len(coverage) if coverage else 0.0)
    if out_path:
        with open(out_path, "w") as f:
            json.dump({"summary": summary, "signals": all_signals, "built": datetime.now().isoformat()}, f, indent=1)
    return summary


TAKER_BAND = (0.80, 0.97)     # where a locked winner actually trades


def liquidity_reality(db_path: str | None = None, max_spread_cents: float = 10.0,
                      band: tuple = TAKER_BAND, days: int = 7, venue: str = "offshore") -> dict | None:
    """How much of this replay could have happened against the real book.

    `_synthetic_event` hangs a bid and an ask `SPREAD` either side of every recorded price, so the
    replay trades a 2c book on every bucket of every day. The live offshore books in the band where
    a locked winner sits are mostly 0.03 bid against 0.95 ask — not a price — and `WeatherLock`
    refuses them on `lock_max_spread_cents`. That is the whole distance between a backtest that
    finds 180 lock signals and a live week that finds one, and it is a property of the venue rather
    than a bug in either. Measured from the snapshots the loop already writes, so the number ages
    with the books instead of living in a comment.

    Returns None when there are no snapshots to measure (a fresh checkout, or an offline test)."""
    import sqlite3
    from . import config as _config
    path = db_path or _config.DB_PATH
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        rows = conn.execute(
            "SELECT bid, ask FROM snapshots WHERE venue=? AND bid IS NOT NULL AND ask IS NOT NULL "
            "AND ask BETWEEN ? AND ? AND ts > ?",
            (venue, band[0], band[1], time.time() - days * 86400)).fetchall()
        conn.close()
    except Exception:
        return None
    spreads = sorted((a - b) * 100 for b, a in rows)
    if not spreads:
        return None
    tradable = [s for s in spreads if s <= max_spread_cents]
    return {"venue": venue, "days": days, "band": list(band), "n": len(spreads),
            "median_spread_cents": round(spreads[len(spreads) // 2], 1),
            "max_spread_cents": max_spread_cents,
            "tradable_share": round(len(tradable) / len(spreads), 3)}


def summarize(signals: list, disc: dict, days_done: int, liquidity: dict | None = None,
              coverage: float = 1.0) -> dict:
    by_mod = {}
    for s in signals:
        m = by_mod.setdefault(s["module"], {"signals": 0, "filled": 0, "wins": 0, "losses": 0, "net": 0.0, "staked": 0.0})
        m["signals"] += 1
        if s["filled"]:
            m["filled"] += 1
            m["net"] += s["pnl"]
            m["staked"] += s["size_usd"]
            m["wins" if s["pnl"] > 0 else "losses"] += 1
    for m in by_mod.values():
        m["net"] = round(m["net"], 2)
        m["roi_pct"] = round(100 * m["net"] / m["staked"], 1) if m["staked"] else 0.0
    disc_avg = {}
    for rule, rows in disc.items():
        disc_avg[rule] = {k: round(sum(r[k] for r in rows) / len(rows), 3) for k in rows[0]} if rows else {}
    out = {"days": days_done, "modules": by_mod, "discount_loglik_by_rule": disc_avg,
           "book_coverage": round(coverage, 3)}
    if liquidity:
        out["liquidity"] = liquidity
    return out


MIN_BOOK_COVERAGE = 0.5   # below this the replay had no prices to trade and every number is a zero


def format_summary(s: dict) -> str:
    lines = [f"backtest — {s['days']} city-days"]
    cov = s.get("book_coverage", 1.0)
    if cov < MIN_BOOK_COVERAGE:
        # A replay with no price history produces no signals and reports a tidy net=$0.00 per day,
        # which reads exactly like "the strategy did nothing wrong". It is not a result, it is a
        # failed download: `prices-history` 400s when the window is too wide for fidelity=5, so a
        # `--days 30` run silently answered $0.00 on 987 of 987 buckets on 2026-09-18.
        lines.append(f"  *** NO RESULT: price history came back for {cov:.0%} of buckets (need "
                     f"{MIN_BOOK_COVERAGE:.0%}). Nothing below is a measurement — shorten --days "
                     f"and re-run. ***")
    for name, m in s["modules"].items():
        lines.append(f"  {name:<14} signals={m['signals']:<4} filled={m['filled']:<4} W/L={m['wins']}/{m['losses']} "
                     f"net=${m['net']:+.2f} on ${m['staked']:.0f} staked (ROI {m['roi_pct']:+.1f}%)")
    liq = s.get("liquidity")
    if liq:
        lock = s["modules"].get("weather_lock", {})
        live_n = round(lock.get("signals", 0) * liq["tradable_share"])
        lines.append(f"  {liq['venue']} book reality ({liq['days']}d, ask {liq['band'][0]:.2f}-{liq['band'][1]:.2f}, "
                     f"n={liq['n']}): median spread {liq['median_spread_cents']:.0f}c · "
                     f"{liq['tradable_share']:.0%} clear the {liq['max_spread_cents']:.0f}c filter")
        lines.append(f"  → of {lock.get('signals', 0)} replayed lock signals, about {live_n} have a book live. "
                     "The ROI above is an upper bound, not a forecast.")
    for rule, sc in s["discount_loglik_by_rule"].items():
        best = max(sc, key=sc.get) if sc else "?"
        lines.append(f"  discount fit [{rule} rule]: " + "  ".join(f"{k}°F→{v}" for k, v in sc.items()) + f"  → best {best}°F")
    return "\n".join(lines)
