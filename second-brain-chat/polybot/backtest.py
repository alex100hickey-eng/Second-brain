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
                                                             since_ts=start.timestamp() - 3600 * 12,
                                                             until_ts=end.timestamp() + 3600 * 12)
        except Exception as exc:
            log(f"    history {b.title}: {exc}")
        time.sleep(0.05)
    hours_back = int((datetime.now(ZoneInfo(tz)) - start).total_seconds() // 3600) + 2
    try:
        # The feed has to follow the settlement rule, as it does live (`observations_for_rule`).
        # Every offshore market settles on the hourly METAR column, US stations included, and the
        # NWS 5-minute feed reads 1-2°F above it — the exact error that sent the first paper day's
        # "dead" and "locked" buckets the wrong way. It also only reaches back about a week, so a
        # K-station day older than that replayed with no observations at all.
        if event.rule == "cli" and station.startswith("K"):
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


# ---- replaying what the live model actually said --------------------------------------------
# The harness above re-creates the morning forecast from Open-Meteo's archive. weather_model_update
# cannot be replayed that way at all — it trades the CHANGE between two live runs, and the archive
# holds one forecast per day — and weather_hold's live evidence came from the live ensemble, not the
# archive. The loop stored every run it made in `model_runs` (09-12 .. 09-18, until model_update
# went off). This replays both modules over those runs, with the current filters.
US_BUCKETS = 6        # a US weather event has six buckets, an offshore one eleven: the stored run's
                      # length says which venue's scan wrote it (the table has no venue column)


class _PrevRuns:
    """The two ledger calls WeatherModelUpdate makes, served from the stored runs of ONE venue.

    Live, both venues wrote runs for the same city-day into one table and `last_model_run` handed
    back whichever came last, so a US run was compared against an offshore one about half the
    time and the length check silently dropped the comparison. The replay keeps them apart."""

    def __init__(self):
        self.prev = None

    def last_model_run(self, city, date, kind):
        return self.prev

    def add_model_run(self, *a, **k):
        pass


def _stored_runs(db_path: str) -> dict:
    import sqlite3
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    groups: dict = {}
    for ts, city, date, kind, probs in conn.execute(
            "SELECT ts, city, date, kind, probs FROM model_runs ORDER BY ts"):
        p = json.loads(probs)
        venue = "us" if len(p) == US_BUCKETS else "offshore"
        groups.setdefault((venue, city, date, kind), []).append((ts, p))
    conn.close()
    return groups


def _us_book_at(snaps: dict, token: str, ts: float, max_age_s: float = 1800.0):
    rows = snaps.get(token) or []
    best = None
    for t, bid, ask in rows:
        if t > ts:
            break
        best = (t, bid, ask)
    if best is None or ts - best[0] > max_age_s:
        return None, None
    return best[1], best[2]


def _run_modules(ctx, runs_prev, cfg, modules, taken):
    from .strategies.weather import WeatherHold, WeatherModelUpdate
    out = []
    for name in modules:
        if name == "weather_hold":
            mod = WeatherHold(cfg)
        else:
            stub = _PrevRuns()
            stub.prev = runs_prev
            mod = WeatherModelUpdate(cfg, stub)
        for sig in mod.scan(ctx):
            if (name, sig.market) in taken:       # one position per market, as the risk manager does
                continue
            taken.add((name, sig.market))
            out.append((name, sig))
    return out


def replay_stored_runs(db_path: str | None = None, cfg: config.Config | None = None, log=print,
                       modules=("weather_hold", "weather_model_update"), load_day=None, us_venue=None) -> dict:
    """Replay the stored live model runs through the CURRENT strategy code and filters.

    offshore runs: book = the CLOB price history ± SPREAD (as the harness does), observations and the
    remaining-hours forecast from `fetch_day` (or `load_day`). US runs: book = the real bid/ask the
    loop snapshotted (only exists from 2026-09-15 19:45), outcome from the venue. Returns the same
    summary shape as `run`, split by venue."""
    from .strategies.weather import WeatherCtx, adjust_probs_with_obs
    cfg = cfg or config.load()
    db_path = db_path or config.DB_PATH
    load_day = load_day or (lambda city, date, kind: fetch_day(city, date, kind, log=lambda *a: None))
    groups = _stored_runs(db_path)
    signals = {"offshore": [], "us": []}
    days_done = {"offshore": 0, "us": 0}

    us_events = {}
    us_snaps: dict = {}
    us_keys = [k for k in groups if k[0] == "us"]
    if us_keys and us_venue is not None and us_venue.available:
        from .feeds.usvenue import us_event_slug, weather_event_from_us
        slugs = {us_event_slug(c, datetime.strptime(d, "%Y-%m-%d"), k): (c, d, k) for _, c, d, k in us_keys}
        for slug, e in (us_venue.events_by_slug(list(slugs)) or {}).items():
            c, d, k = slugs[slug]
            ev = weather_event_from_us(e, c, k)
            if ev is not None:
                us_events[(c, d, k)] = ev
        import sqlite3
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        for ev in us_events.values():
            for b in ev.buckets:
                us_snaps[b.yes_token] = conn.execute(
                    "SELECT ts, bid, ask FROM snapshots WHERE venue='us' AND market=? ORDER BY ts",
                    (b.yes_token,)).fetchall()
        conn.close()

    for (venue, city, date, kind), runs in sorted(groups.items(), key=lambda kv: kv[0][2]):
        meta = config.city_meta(city)
        if not meta:
            continue
        tz = ZoneInfo(meta["tz"])
        try:
            day = load_day(city, datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=tz), kind)
        except Exception as exc:
            log(f"  {venue} {city} {date} {kind}: fetch error {exc}")
            continue
        if day is None:
            continue
        if venue == "offshore":
            event = day["event"]
        else:
            event = us_events.get((city, date, kind))
            if event is None or not any(b.outcome is not None for b in event.buckets):
                continue
        if len(event.buckets) != len(runs[0][1]):
            continue                                  # the event changed shape since the run was stored
        obs_all = day["obs"]
        if venue == "us" and meta.get("us_station"):
            # The US market settles at its own station (Central Park, not LaGuardia). The 5-minute
            # NWS feed it really settles on is gone after a week, so read that station's METAR —
            # the right sensor, if a degree or so under the CLI maximum.
            try:
                start, _ = weather.local_day_bounds(date, meta["tz"])
                hours = int((datetime.now(tz) - start).total_seconds() // 3600) + 2
                obs_all = metar.observations(meta["us_station"], hours=hours, unit="F") or obs_all
            except Exception:
                pass
        taken, prev, day_sigs = set(), None, []
        for ts, probs in runs:
            if venue == "offshore":
                ev = _synthetic_event(event, day["histories"], ts)
            else:
                ev = copy.deepcopy(event)
                for b in ev.buckets:
                    b.best_bid, b.best_ask = _us_book_at(us_snaps, b.yes_token, ts)
                    b.closed = False
                if all(b.best_bid is None and b.best_ask is None for b in ev.buckets):
                    prev = {"probs": probs}
                    continue                          # no book on record at that moment
            obs_until = [(iso, t) for iso, t in obs_all
                         if datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() <= ts]
            running, n_obs, last_local = weather.running_extreme(obs_until, date, meta["tz"], kind)
            remaining = weather.hours_remaining_extreme(day["hourly"], date, last_local, meta["tz"], kind)
            adjusted = adjust_probs_with_obs(probs, ev.buckets, running, remaining, kind) if n_obs else list(probs)
            ctx = WeatherCtx(city=city, date=date, kind=kind, venue=venue, event=ev, probs=adjusted,
                             model_probs=list(probs), obs=obs_until, running=running, n_obs=n_obs,
                             last_obs_local=last_local, hourly=day["hourly"], remaining_extreme=remaining,
                             local_now=datetime.fromtimestamp(ts, tz), station=day["station"], rule=day["rule"])
            for name, sig in _run_modules(ctx, prev, cfg, modules, taken):
                bucket = next(b for b in event.buckets if b.yes_token == sig.market)
                if venue == "offshore":
                    hist = [(t, p) for t, p in day["histories"].get(sig.market, []) if t >= ts]
                else:
                    hist = [(t, (b_ + a_) / 2) for t, b_, a_ in us_snaps.get(sig.market, [])
                            if t >= ts and b_ is not None and a_ is not None]
                d = {"ts": ts, "price": sig.price, "side": sig.side, "exit_rule": sig.exit,
                     "horizon_h": sig.horizon_hours, "contracts": sig.contracts, "taker": False}
                fill_ts, fill_px = paper.fill_from_history(d, hist)
                rec = {"module": name, "venue": venue, "city": city, "date": date, "kind": kind,
                       "bucket": bucket.title, "side": sig.side, "price": sig.price, "size_usd": sig.size_usd,
                       "edge_cents": sig.edge_cents, "filled": fill_ts is not None, "outcome": bucket.outcome,
                       "pnl": 0.0, "exit": None}
                if fill_ts is not None:
                    exit_ts, exit_px, ekind = paper.exit_from_history(d, fill_ts, hist)
                    pnl, _ = paper.pnl_usd(d, fill_px, exit_px, bucket.outcome, venue, "weather")
                    rec.update(pnl=round(pnl, 2), exit=ekind or "settle")
                day_sigs.append(rec)
            prev = {"probs": probs}
        days_done[venue] += 1
        signals[venue].extend(day_sigs)
    out = {}
    for venue in ("offshore", "us"):
        out[venue] = summarize(signals[venue], {}, days_done[venue])
        out[venue]["runs"] = sum(len(r) for k, r in groups.items() if k[0] == venue)
    out["signals"] = signals
    return out
