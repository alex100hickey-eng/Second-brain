"""The runner: scans modules on a cadence, passes signals through the risk manager, records them
(paper), nudges (signal) or places maker orders (live), settles paper trades, writes the report.

    python3 -m polybot.runner scan            one pass over every enabled module
    python3 -m polybot.runner scan --city nyc --modules weather_lock
    python3 -m polybot.runner settle          fill/close open paper signals from what the market did
    python3 -m polybot.runner report [--days 7]
    python3 -m polybot.runner calibrate [--events 300]
    python3 -m polybot.runner status
    python3 -m polybot.runner loop            run forever on the built-in schedule

Cadence (loop): weather modules at :55 every hour (after the :51 observation) · bucket_sum every
5 min · hold_favorites 09:00 and 21:00 · settle at :20 every hour · report 07:00 · calibrate 03:00.
"""
from __future__ import annotations

import argparse
import sys
import time
import traceback
from datetime import datetime
from zoneinfo import ZoneInfo

from . import calibration, config
from .feeds.usvenue import USVenue
from .ledger import Ledger
from .paper import PaperEngine
from .risk import RiskManager
from .strategies.bucket_sum import BucketSum
from .strategies.hold_favorites import HoldFavorites
from .strategies.leadlag import LeadLag
from .strategies.maker_rewards import MakerRewards
from .strategies.weather import WeatherHold, WeatherLock, WeatherModelUpdate, WeatherObs, build_ctx

ET = ZoneInfo("America/New_York")
DEDUPE_S = 3 * 3600


class SeriesStore:
    """Recent price samples per (venue, market), fed by the loop's snapshot ticks."""

    def __init__(self, ledger: Ledger, window_s: int = 900):
        self.ledger, self.window_s = ledger, window_s

    def get(self, venue, market):
        rows = self.ledger.snapshots(venue, market, time.time() - self.window_s)
        return [(r["ts"], r["mid"]) for r in rows if r["mid"] is not None]


class Runner:
    def __init__(self, cfg: config.Config | None = None, ledger: Ledger | None = None, log=print):
        self.cfg = cfg or config.load()
        self.ledger = ledger or Ledger()
        self.log = log
        self.us = USVenue()
        self.risk = RiskManager(self.cfg, self.ledger)
        self.paper = PaperEngine(self.ledger)
        if self.us.available:
            bal = self.us.balance_usd()
            if bal is not None:
                self.cfg.bankroll_usd = bal
        self.weather_modules = {
            "weather_hold": WeatherHold(self.cfg),
            "weather_obs": WeatherObs(self.cfg),
            "weather_lock": WeatherLock(self.cfg),
            "weather_model_update": WeatherModelUpdate(self.cfg, self.ledger),
            "bucket_sum": BucketSum(self.cfg),
        }
        self.other_modules = {
            "hold_favorites": HoldFavorites(self.cfg),
            "leadlag": LeadLag(self.cfg, self.us, SeriesStore(self.ledger)),
            "maker_rewards": MakerRewards(self.cfg, self.us),
        }

    # ---- one signal through the gate ------------------------------------------------------
    def handle(self, sig) -> str:
        mode = self.cfg.mode(sig.module)
        if mode == "off":
            return "off"
        if self.ledger.recent_signal_exists(sig.module, sig.market, sig.side, DEDUPE_S):
            return "dup"
        ok, why = self.risk.allow(sig)
        if not ok:
            self.log(f"    refused {sig.module} {sig.label}: {why}")
            return "refused"
        if mode == "live" and sig.venue != "us":
            return "paper-venue"          # offshore is a paper proxy; nothing is ever sent there
        sid = self.ledger.add_signal(sig, mode)
        line = (f"    {mode.upper():<6} #{sid} {sig.module} {sig.side} {sig.label} @ {sig.price:.2f} "
                f"${sig.size_usd:.0f} edge {sig.edge_cents:.1f}c — {sig.reason}")
        self.log(line)
        if mode == "live":
            try:
                order = self.us.place_limit(sig.market, sig.side, sig.price, sig.contracts)
                self.ledger.add_order(sid, "us", sig.market, sig.side, sig.price, sig.contracts, "sent",
                                      venue_order_id=str((order or {}).get("id")), raw=order)
            except Exception as exc:
                self.ledger.add_order(sid, "us", sig.market, sig.side, sig.price, sig.contracts, f"error: {exc}")
                self.ledger.set_signal_status(sid, "error")
                self.log(f"    ORDER ERROR: {exc}")
                return "error"
        return mode

    # ---- scans -----------------------------------------------------------------------------
    def scan_weather(self, cities=None, modules=None, date: datetime | None = None, kinds=("high",)) -> int:
        n = 0
        cities = cities or self.cfg.cities
        wanted = [m for m in self.weather_modules if (modules is None or m in modules) and self.cfg.mode(m) != "off"]
        if not wanted:
            return 0
        # bucket_sum only needs the books: skip the model/observation calls on the 5-minute path
        light = all(m == "bucket_sum" for m in wanted)
        for city in cities:
            for kind in kinds:
                try:
                    now_local = datetime.now(ZoneInfo(config.CITIES[city]["tz"]))
                    ctx = build_ctx(city, date or now_local, kind, self.cfg, venue="offshore",
                                    fetch={"members": [], "obs": [], "hourly": []} if light else None)
                except Exception as exc:
                    self.log(f"  {city} {kind}: context error: {exc}")
                    continue
                if ctx is None:
                    self.log(f"  {city} {kind}: no market today")
                    continue
                if not light:
                    probs = " ".join(f"{b.title.split('°')[0]}={p:.0%}" for b, p in zip(ctx.event.buckets, ctx.probs) if p >= 0.03)
                    self.log(f"  {city} {ctx.date} {kind} [{ctx.station}/{ctx.rule}] running={ctx.running} n_obs={ctx.n_obs} "
                             f"remaining={ctx.remaining_extreme} | model {probs}")
                for b in ctx.event.buckets:
                    self.ledger.add_snapshot("offshore", b.yes_token, b.best_bid, b.best_ask, b.last)
                for name in wanted:
                    try:
                        for sig in self.weather_modules[name].scan(ctx):
                            if self.handle(sig) in ("paper", "signal", "live"):
                                n += 1
                    except Exception as exc:
                        self.log(f"  {name} error: {exc}\n{traceback.format_exc(limit=2)}")
        return n

    def scan_other(self, modules=None) -> int:
        n = 0
        for name, strat in self.other_modules.items():
            if (modules is not None and name not in modules) or self.cfg.mode(name) == "off":
                continue
            idle = getattr(strat, "idle_reason", None)
            if idle:
                self.log(f"  {name}: idle — {idle}")
                continue
            try:
                for sig in strat.scan(None):
                    if self.handle(sig) in ("paper", "signal", "live"):
                        n += 1
            except Exception as exc:
                self.log(f"  {name} error: {exc}\n{traceback.format_exc(limit=2)}")
        return n

    def scan(self, cities=None, modules=None) -> int:
        self.log(f"scan {datetime.now(ET).strftime('%Y-%m-%d %H:%M ET')} — bankroll ${self.cfg.bankroll_usd:.0f} — "
                 f"us venue: {'ready' if self.us.available else self.us.why_unavailable}")
        return self.scan_weather(cities, modules) + self.scan_other(modules)

    def settle(self) -> dict:
        counts = self.paper.settle_open("offshore", self.log)
        self.log(f"settle: {counts}")
        return counts

    def report(self, days: int = 1) -> str:
        text = self.ledger.report(days)
        with open(config.REPORT_PATH, "w") as f:
            f.write(text + "\n")
        return text

    def status(self) -> str:
        lines = [f"polybot {config.__name__.split('.')[0]} — modes: " + ", ".join(f"{m}={self.cfg.mode(m)}" for m in config.MODULES),
                 f"  us venue: {'ready' if self.us.available else self.us.why_unavailable}",
                 f"  kill switch: {'ON' if config.kill_switch_on() else 'off'} ({config.KILL_PATH})",
                 f"  bankroll ${self.cfg.bankroll_usd:.0f} · caps ${self.cfg.caps.max_per_market_usd:.0f}/market "
                 f"${self.cfg.caps.max_exposure_usd:.0f} total · floor ${self.cfg.caps.bankroll_floor_usd:.0f} · "
                 f"sports {'ON' if self.cfg.caps.sports_enabled else 'off (Ohio)'}",
                 f"  open signals: {len(self.ledger.open_signals())} · calibration: "
                 f"{'built ' + calibration.load_table().get('_built', '')[:10] if calibration.load_table() else 'not built'}"]
        return "\n".join(lines)

    # ---- the loop --------------------------------------------------------------------------
    def loop(self):
        self.log("polybot loop started (Ctrl+C to stop)")
        done = set()
        while True:
            now = datetime.now(ET)
            key = now.strftime("%Y-%m-%d %H:%M")
            if key not in done:
                done.add(key)
                try:
                    if now.minute == 55:
                        self.scan_weather(modules=["weather_hold", "weather_obs", "weather_lock", "weather_model_update"])
                    if now.minute % 5 == 0:
                        self.scan_weather(modules=["bucket_sum"])
                        self.scan_other(modules=["leadlag", "maker_rewards"])
                    if now.minute == 20:
                        self.settle()
                    if now.hour in (9, 21) and now.minute == 0:
                        self.scan_other(modules=["hold_favorites"])
                    if now.hour == 7 and now.minute == 0:
                        self.log(self.report(1))
                    if now.hour == 3 and now.minute == 0:
                        calibration.save_table(calibration.build(log=self.log))
                except Exception as exc:
                    self.log(f"loop error: {exc}\n{traceback.format_exc(limit=3)}")
                if len(done) > 5000:
                    done = set(sorted(done)[-100:])
            time.sleep(20)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="polybot")
    ap.add_argument("cmd", choices=["scan", "settle", "report", "calibrate", "status", "loop"])
    ap.add_argument("--city", action="append")
    ap.add_argument("--modules", nargs="*")
    ap.add_argument("--days", type=int, default=1)
    ap.add_argument("--events", type=int, default=300)
    a = ap.parse_args(argv)
    r = Runner()
    if a.cmd == "scan":
        n = r.scan(a.city, a.modules)
        print(f"{n} signal(s) recorded")
    elif a.cmd == "settle":
        r.settle()
    elif a.cmd == "report":
        print(r.report(a.days))
    elif a.cmd == "calibrate":
        table = calibration.build(max_events=a.events)
        calibration.save_table(table)
        print(calibration.summary(table))
    elif a.cmd == "status":
        print(r.status())
    elif a.cmd == "loop":
        r.loop()


if __name__ == "__main__":
    sys.exit(main())
