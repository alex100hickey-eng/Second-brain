"""Weather modules. All share one context per (city, date): the venue's bucket book, the
ensemble bucket probabilities, the day's observations, and the hourly forecast remainder.

  weather_hold (H1)          model probability vs bucket price → maker buy, take-profit or hold
  weather_obs (S1)           observed running max makes buckets impossible → sell them while bid > 0
  weather_lock (S8)          the day's max is locked after the peak → buy the winner at 90-97c
  weather_model_update (S7)  a new model run shifted a bucket's probability and the book has not moved

Venue rules matter: the offshore markets read the HOURLY "Temp" column at the station named in the
description (NYC = KLGA), which reads ~1°F under the daily max; Polymarket US settles on the NWS
CLI daily climate report at KNYC/KMDW/KMIA/KLAX/KSFO. The context carries the right station+rule.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from .. import config, fees
from ..feeds import metar, offshore, weather
from .base import Signal, Strategy, size_for


@dataclass
class WeatherCtx:
    city: str
    date: str
    kind: str
    venue: str
    event: object                     # offshore.WeatherEvent (or a US equivalent with .buckets)
    probs: list                       # aligned with event.buckets, AFTER the observation adjustment
    model_probs: list = field(default_factory=list)   # raw ensemble share, before observations
    obs: list = field(default_factory=list)
    running: float | None = None      # running max (or min) so far today
    n_obs: int = 0
    last_obs_local: str | None = None
    hourly: list = field(default_factory=list)
    remaining_extreme: float | None = None
    local_now: datetime | None = None
    station: str | None = None
    rule: str = "hourly"


def build_ctx(city: str, date: datetime, kind: str = "high", cfg: config.Config | None = None,
              venue: str = "offshore", fetch=None) -> WeatherCtx | None:
    """Assemble the shared context. `fetch` lets tests inject data: dict with event/members/obs/hourly."""
    cfg = cfg or config.load()
    meta = config.city_meta(city)
    if meta is None:
        return None
    tz = meta["tz"]
    fetch = fetch or {}
    event = fetch.get("event")
    if event is None:
        event = offshore.find_weather_event(city, meta["query"], date, kind)
    if event is None or not event.buckets:
        return None
    station = event.station or meta["station"]
    rule = event.rule
    if venue == "us":
        station, rule = meta.get("us_station", station), "cli"
    coords = config.STATIONS.get(station, {"lat": meta["lat"], "lon": meta["lon"]})
    members = fetch.get("members")
    if members is None:
        ens = weather.ensemble_daily(coords["lat"], coords["lon"], tz, days=2, unit=event.unit)
        members = ens.get(event.date, {}).get("max" if kind == "high" else "min", [])
    discount = cfg.hourly_rule_discount_f if (rule == "hourly" and kind == "high") else 0.0
    probs = weather.bucket_probs(members, event.buckets, discount=discount)
    obs = fetch.get("obs")
    if obs is None:
        obs = observations_for_rule(station, rule, event.date, tz, event.unit)
    running, n_obs, last_local = weather.running_extreme(obs, event.date, tz, kind)
    hourly = fetch.get("hourly")
    if hourly is None:
        hourly = weather.hourly_forecast(coords["lat"], coords["lon"], tz, unit=event.unit)
    remaining = weather.hours_remaining_extreme(hourly, event.date, last_local, tz, kind)
    adjusted = adjust_probs_with_obs(probs, event.buckets, running, remaining, kind) if n_obs else list(probs)
    return WeatherCtx(city=city, date=event.date, kind=kind, venue=venue, event=event, probs=adjusted,
                      model_probs=list(probs), obs=obs, running=running, n_obs=n_obs, last_obs_local=last_local,
                      hourly=hourly, remaining_extreme=remaining,
                      local_now=fetch.get("local_now") or datetime.now(ZoneInfo(tz)), station=station, rule=rule)


def observations_for_rule(station: str, rule: str, date: str, tz: str, unit: str = "F") -> list:
    """The observation feed that matches how the market settles.

    'cli' (Polymarket US): the NWS 5-minute ASOS readings — the CLI daily max comes off the same
    continuous sensor, so no reading can exceed it. 'hourly' (offshore): the METAR column only. The
    5-minute feed prints 1-2°F above the hourly METAR (KSFO 2026-09-12: 72 vs a 70-71 settlement),
    which made buckets look dead or locked when they were not."""
    start, end = weather.local_day_bounds(date, tz)
    now = datetime.now(ZoneInfo(tz))
    if rule == "cli" and station.startswith("K"):
        return weather.observations(station, start.isoformat(), min(end, now).isoformat())
    hours = int((now - start).total_seconds() // 3600) + 2
    return metar.observations(station, hours=max(hours, 3), unit=unit)


def adjust_probs_with_obs(probs: list, buckets, running, remaining, kind: str = "high",
                          margin_f: float = 2.0, floor: float = 0.005, tail_factor: float = 0.25) -> list:
    """The ensemble does not know what time it is. Once observations exist for the day:
    - a bucket entirely below the running max (high market) cannot win → 0
    - a bucket entirely above max(running, model's remaining-hours max) + margin → 0
    - the rest keep their model share, renormalised; the bucket holding the running max always
      keeps at least the floor so a locked day still has a winner."""
    if running is None:
        return list(probs)
    out = []
    if kind == "high":
        top = max(running, remaining if remaining is not None else running)
        ceiling = top + margin_f
        for b, p in zip(buckets, probs):
            if b.hi < running or b.lo > ceiling:
                out.append(0.0)                   # already impossible, or beyond any forecast + margin
            elif b.contains(running):
                out.append(max(p, floor))         # the bucket holding today's max always survives
            elif p <= floor:
                out.append(0.0)                   # no model support: a floor is not evidence
            elif b.lo > top:
                out.append(p * tail_factor)       # above both the observed max and the forecast: tail only
            else:
                out.append(p)
    else:  # low: mirror image — the day's minimum can only fall from here
        bottom = min(running, remaining if remaining is not None else running)
        floor_t = bottom - margin_f
        for b, p in zip(buckets, probs):
            if b.lo > running or b.hi < floor_t:
                out.append(0.0)
            elif b.contains(running):
                out.append(max(p, floor))
            elif p <= floor:
                out.append(0.0)
            elif b.hi < bottom:
                out.append(p * tail_factor)
            else:
                out.append(p)
    s = sum(out)
    if s <= 0:
        return [1.0 if b.contains(running) else 0.0 for b in buckets]
    return [p / s for p in out]


def _label(ctx: WeatherCtx, b) -> str:
    return f"{ctx.city} {ctx.date} {ctx.kind} {b.title}"


def _spread_cents(b) -> float | None:
    if b.best_bid is None or b.best_ask is None:
        return None
    return round((b.best_ask - b.best_bid) * 100, 1)


def _in_band(yes_level: float, cfg) -> bool:
    """Model modules only trade buckets the market prices inside hold_price_band (YES terms)."""
    lo, hi = cfg.hold_price_band
    return lo <= yes_level <= hi


def _post_price_buy(b, tick=0.01):
    """Where a maker BUY rests: improve the bid by one tick if that stays under the ask."""
    if b.best_bid is None:
        return None
    p = b.best_bid + tick
    if b.best_ask is not None and p >= b.best_ask:
        p = b.best_bid
    return round(p, 2)


class WeatherHold(Strategy):
    name = "weather_hold"

    def __init__(self, cfg: config.Config):
        self.cfg = cfg

    def scan(self, ctx: WeatherCtx) -> list:
        out = []
        tp = f"tp:{self.cfg.take_profit_cents / 100:.2f}"
        for b, p in zip(ctx.event.buckets, ctx.probs):
            if b.closed or b.best_ask is None:
                continue
            # buckets already killed by observations are weather_obs's job, not a hold
            if ctx.running is not None and ((ctx.kind == "high" and b.hi < ctx.running) or
                                            (ctx.kind == "low" and b.lo > ctx.running)):
                continue
            post = _post_price_buy(b)
            if post is None:
                continue
            edge = (p - post) * 100
            if (edge >= self.cfg.edge_min_cents and post <= 0.93 and _in_band(post, self.cfg)
                    and edge <= self.cfg.hold_edge_max_cents):
                size = size_for(p, post, self.cfg.bankroll_usd, self.cfg.caps)
                if size > 0:
                    out.append(Signal(self.name, ctx.venue, b.yes_token, _label(ctx, b), "BUY_YES", post, size,
                                      round(edge, 1), f"model {p:.0%} vs post {post:.2f} (ask {b.best_ask:.2f})",
                                      exit=tp, horizon_hours=30, spread_cents=_spread_cents(b),
                                      meta={"market_id": b.market_id, "p_model": p, "station": ctx.station}))
            # the NO side: model says this bucket is far less likely than priced
            if (b.best_bid is not None and (b.best_bid - p) * 100 >= self.cfg.edge_min_cents and b.best_bid >= 0.10
                    and _in_band(b.best_bid, self.cfg) and (b.best_bid - p) * 100 <= self.cfg.hold_edge_max_cents):
                no_price = round(1 - b.best_bid + 0.01, 2)  # rest a NO buy one tick inside
                size = size_for(1 - p, no_price, self.cfg.bankroll_usd, self.cfg.caps)
                if size > 0:
                    out.append(Signal(self.name, ctx.venue, b.yes_token, _label(ctx, b), "BUY_NO", no_price, size,
                                      round((b.best_bid - p) * 100, 1), f"model {p:.0%} vs bid {b.best_bid:.2f}",
                                      exit=tp, horizon_hours=30, spread_cents=_spread_cents(b),
                                      meta={"market_id": b.market_id, "p_model": p, "station": ctx.station}))
        return out


class WeatherObs(Strategy):
    """Buckets strictly below today's running max (for a high market) cannot win any more."""
    name = "weather_obs"

    def __init__(self, cfg: config.Config):
        self.cfg = cfg

    def scan(self, ctx: WeatherCtx) -> list:
        out = []
        if ctx.running is None or ctx.n_obs == 0:
            return out
        for b in ctx.event.buckets:
            if b.closed or b.best_bid is None:
                continue
            dead = (b.hi < ctx.running) if ctx.kind == "high" else (b.lo > ctx.running)
            if not dead or b.best_bid < self.cfg.dead_bucket_min_bid:
                continue
            no_price = round(1 - b.best_bid + 0.01, 2)
            size = self.cfg.caps.max_per_market_usd     # dead is dead: the only risk is the feed, so size to the cap
            out.append(Signal(self.name, ctx.venue, b.yes_token, _label(ctx, b), "BUY_NO", no_price, size,
                              round(b.best_bid * 100, 1),
                              f"dead: running {ctx.kind} {ctx.running}° past bucket, bid still {b.best_bid:.2f}",
                              exit="settle", horizon_hours=30, spread_cents=_spread_cents(b),
                              meta={"market_id": b.market_id, "running": ctx.running, "n_obs": ctx.n_obs}))
        return out


def lock_state(ctx: WeatherCtx, margin_f: float = 1.0):
    """Is today's extreme locked? High: the observed peak sits at least `margin_f` above the model's
    remaining-hours max and the last observations are falling. Low: the observed minimum sits at
    least `margin_f` below the remaining-hours min and observations are rising. Returns (locked, winner)."""
    if ctx.running is None or ctx.n_obs < 3 or ctx.remaining_extreme is None:
        return False, None
    temps = [t for _, t in ctx.obs[-3:]]
    if len(temps) < 3:
        return False, None
    if ctx.kind == "high":
        if ctx.remaining_extreme > ctx.running - margin_f:
            return False, None
        if not (temps[-1] <= temps[-2] <= temps[-3] or temps[-1] < ctx.running):
            return False, None
    else:
        if ctx.remaining_extreme < ctx.running + margin_f:
            return False, None
        if not (temps[-1] >= temps[-2] >= temps[-3] or temps[-1] > ctx.running):
            return False, None
    winner = next((b for b in ctx.event.buckets if b.contains(ctx.running)), None)
    if winner is None:
        return False, None
    # a CLI-rule venue can print 1° beyond the hourly running extreme; refuse a lock at the bucket's edge
    if ctx.rule == "cli" and ctx.running == (winner.hi if ctx.kind == "high" else winner.lo):
        return False, None
    return True, winner


class WeatherLock(Strategy):
    name = "weather_lock"

    def __init__(self, cfg: config.Config):
        self.cfg = cfg

    def scan(self, ctx: WeatherCtx) -> list:
        locked, winner = lock_state(ctx)
        if not locked or winner.closed or winner.best_ask is None:
            return []
        post = _post_price_buy(winner)
        if post is None or post > 0.97:
            return []
        edge = (1 - post) * 100 - fees.leg_cost(post, 1, ctx.venue, True) * 100
        if edge < self.cfg.lock_min_edge_cents:
            return []
        size = self.cfg.caps.max_per_market_usd
        return [Signal(self.name, ctx.venue, winner.yes_token, _label(ctx, winner), "BUY_YES", post, size,
                       round(edge, 1), f"locked at {ctx.running}° (model remaining {ctx.remaining_extreme}), post {post:.2f}",
                       exit="settle", horizon_hours=24, spread_cents=_spread_cents(winner),
                       meta={"market_id": winner.market_id, "running": ctx.running, "n_obs": ctx.n_obs})]


class WeatherModelUpdate(Strategy):
    """Compare this run's bucket probabilities to the last stored run; trade the shift the book missed."""
    name = "weather_model_update"

    def __init__(self, cfg: config.Config, ledger):
        self.cfg = cfg
        self.ledger = ledger

    def scan(self, ctx: WeatherCtx) -> list:
        out = []
        raw = ctx.model_probs or ctx.probs      # compare model runs, not observation-driven shifts
        prev = self.ledger.last_model_run(ctx.city, ctx.date, ctx.kind)
        self.ledger.add_model_run(ctx.city, ctx.date, ctx.kind, raw)
        if not prev or len(prev["probs"]) != len(raw):
            return out
        for b, p_now, p_prev in zip(ctx.event.buckets, raw, prev["probs"]):
            if ctx.running is not None and b.hi < ctx.running:
                continue                          # dead buckets belong to weather_obs
            shift = p_now - p_prev
            if abs(shift) < self.cfg.model_update_min_shift or b.closed or b.mid is None:
                continue
            if shift > 0 and b.best_ask is not None:
                post = _post_price_buy(b)
                if post is None:
                    continue
                edge = (p_now - post) * 100
                if edge >= self.cfg.edge_min_cents / 2 and _in_band(post, self.cfg) and edge <= self.cfg.hold_edge_max_cents:
                    size = size_for(p_now, post, self.cfg.bankroll_usd, self.cfg.caps)
                    if size > 0:
                        out.append(Signal(self.name, ctx.venue, b.yes_token, _label(ctx, b), "BUY_YES", post, size,
                                          round(edge, 1), f"model +{shift:.0%} to {p_now:.0%}, post {post:.2f}",
                                          exit=f"tp:{self.cfg.take_profit_cents / 100:.2f}", horizon_hours=12,
                                          spread_cents=_spread_cents(b), meta={"market_id": b.market_id, "shift": shift}))
            elif shift < 0 and b.best_bid is not None and b.best_bid >= 0.10:
                no_price = round(1 - b.best_bid + 0.01, 2)
                edge = (b.best_bid - p_now) * 100
                if edge >= self.cfg.edge_min_cents / 2 and _in_band(b.best_bid, self.cfg) and edge <= self.cfg.hold_edge_max_cents:
                    size = size_for(1 - p_now, no_price, self.cfg.bankroll_usd, self.cfg.caps)
                    if size > 0:
                        out.append(Signal(self.name, ctx.venue, b.yes_token, _label(ctx, b), "BUY_NO", no_price, size,
                                          round(edge, 1), f"model {shift:.0%} to {p_now:.0%}, bid {b.best_bid:.2f}",
                                          exit=f"tp:{self.cfg.take_profit_cents / 100:.2f}", horizon_hours=12,
                                          spread_cents=_spread_cents(b), meta={"market_id": b.market_id, "shift": shift}))
        return out
