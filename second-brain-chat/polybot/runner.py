"""The runner: scans modules on a cadence, passes signals through the risk manager, records them
(paper), nudges (signal) or places maker orders (live), settles paper trades, writes the report.

    python3 -m polybot.runner scan            one pass over every enabled module
    python3 -m polybot.runner scan --city nyc --modules weather_lock
    python3 -m polybot.runner settle          fill/close open paper signals from what the market did
    python3 -m polybot.runner report [--days 7]
    python3 -m polybot.runner calibrate [--events 300]
    python3 -m polybot.runner status
    python3 -m polybot.runner promote [--modules weather_obs]   flip gate-passing paper modules to live
    python3 -m polybot.runner loop            run forever on the built-in schedule

Cadence (loop): weather modules at :55 every hour (after the :51 observation) · bucket_sum every
5 min · hold_favorites 09:00 and 21:00 · settle at :20 every hour · report 07:00 · calibrate 03:00.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import json
import traceback
from datetime import datetime
from zoneinfo import ZoneInfo

from . import backtest, calibration, config, notify, pairs
from .execution import Executor
from .feeds import offshore
from .feeds.usvenue import USVenue
from .ledger import Ledger
from .paper import PaperEngine, snapshot_history
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



def _beat(name: str, stale_after_s: int, note: str = "") -> None:
    """Report liveness into the SHARED store so the always-on server can see this Mac loop.

    Silent death is this system's signature failure — polybot slept through a DNS blackout,
    clipbot sat on 187 finished clips for two days, and nothing anywhere noticed. A loop that
    cannot be seen from the server is a loop nobody is watching."""
    try:
        import os, sys
        sys.path.insert(0, os.path.expanduser("~/second-brain/second-brain-chat"))
        import intake, monitor
        from supabase import create_client
        if intake.supabase is None:
            intake.supabase = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
        monitor.supabase = intake.supabase
        monitor.beat(name, stale_after_s, note)
    except Exception:
        pass        # a heartbeat must never break the work it reports on



def _publish(lane: str, facts: dict) -> None:
    """Push this lane's money-relevant numbers into the SHARED store.

    Liveness is not the same as health: polybot can beat happily while every module loses, and
    clipbot beat for two days while 187 finished clips went nowhere. The server cannot read this
    Mac's sqlite, so the scoreboard has to travel."""
    try:
        import os, sys
        sys.path.insert(0, os.path.expanduser("~/second-brain/second-brain-chat"))
        import intake
        from supabase import create_client
        if intake.supabase is None:
            intake.supabase = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
        st = intake._load_state(f"business:{lane}")
        st.update(facts)
        st["key"] = f"business:{lane}"
        st["at"] = __import__("datetime").datetime.now().isoformat()
        intake._save_state(st)
    except Exception:
        pass


class Runner:
    def __init__(self, cfg: config.Config | None = None, ledger: Ledger | None = None, log=print):
        self.cfg = cfg or config.load()
        self._cfg_mtime = os.path.getmtime(config.CONFIG_PATH) if os.path.exists(config.CONFIG_PATH) else 0.0
        self.ledger = ledger or Ledger()
        self.log = log
        self.ledger.gate_since_ts = self.cfg.gate_since_ts
        self.ledger.min_us_signals = self.cfg.min_us_signals
        self.us = USVenue()
        self.risk = RiskManager(self.cfg, self.ledger)
        self.paper = PaperEngine(self.ledger, history_fn=self._paper_history, resolution_fn=self._paper_resolution)
        self.executor = Executor(self.ledger, self.us, self.cfg, self.log)
        if self.us.available:
            # Account VALUE, not buying power: money already in positions is still the bankroll.
            # Reading buying power halted the bot at "bankroll under floor" the moment anything
            # was deployed, and it can never compound if deployed money stops counting.
            bal = self.us.account_value_usd()
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

    # ---- paper price paths per venue ------------------------------------------------------
    def _paper_history(self, sig):
        if sig["venue"] == "us":
            return snapshot_history(self.ledger, "us", sig["market"], sig["ts"] - 60)
        return offshore.prices_history(sig["market"], since_ts=sig["ts"] - 60, fidelity=1)

    def _paper_resolution(self, sig):
        if sig["venue"] == "us":
            return self.us.resolution(sig["market"])
        return offshore.market_resolution(json.loads(sig["meta"] or "{}").get("market_id", ""))

    # ---- one signal through the gate ------------------------------------------------------
    def handle(self, sig) -> str:
        mode = self.cfg.mode(sig.module)
        if mode == "off":
            return "off"
        if mode == "live" and sig.venue != "us":
            mode = "paper"                # offshore is a paper proxy: a live module keeps measuring there
        if self.ledger.recent_signal_exists(sig.module, sig.market, sig.side, DEDUPE_S):
            return "dup"
        ok, why = self.risk.allow(sig, mode=mode)
        if not ok:
            self.log(f"    refused {sig.module} {sig.label}: {why}")
            return "refused"
        sid = self.ledger.add_signal(sig, mode)
        line = (f"    {mode.upper():<6} #{sid} {sig.module} {sig.side} {sig.label} @ {sig.price:.2f} "
                f"${sig.size_usd:.0f} edge {sig.edge_cents:.1f}c — {sig.reason}")
        self.log(line)
        if mode == "signal":
            notify.nudge(f"polybot: {sig.side.replace('_', ' ')} {sig.label}",
                         f"{sig.module}: post {sig.price:.2f} for ${sig.size_usd:.0f} ({sig.contracts} contracts), "
                         f"edge {sig.edge_cents:.1f}c. {sig.reason}. Exit: {sig.exit}.",
                         key=f"polybot-signal-{sig.module}", log=self.log)
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
    def scan_weather(self, cities=None, modules=None, date: datetime | None = None, kinds=None,
                     venue: str = "offshore") -> int:
        """venue='offshore': the paper proxy over every city. venue='us': the five Polymarket US cities on
        the venue's own books — the only signals that can ever be sent live."""
        n = 0
        if venue == "us":
            if not self.us.available:
                return 0
            cities = cities or [c for c in self.cfg.cities if (config.city_meta(c) or {}).get("cli_location")]
        else:
            cities = cities or (config.all_city_slugs() if self.cfg.all_cities else self.cfg.cities)
        kinds = kinds or tuple(self.cfg.kinds)
        wanted = [m for m in self.weather_modules if (modules is None or m in modules) and self.cfg.mode(m) != "off"]
        if not wanted:
            return 0
        # bucket_sum only needs the books: skip the model/observation calls on the 5-minute path
        light = all(m == "bucket_sum" for m in wanted)
        for city in cities:
            for kind in kinds:
                try:
                    now_local = datetime.now(ZoneInfo(config.city_meta(city)["tz"]))
                    fetch = {"members": [], "obs": [], "hourly": []} if light else {}
                    if venue == "us":
                        event = self.us.find_weather_event(city, date or now_local, kind)
                        if event is None:
                            if kind == "high":
                                self.log(f"  us {city} {kind}: no market today")
                            continue
                        fetch["event"] = event
                    ctx = build_ctx(city, date or now_local, kind, self.cfg, venue=venue, fetch=fetch or None)
                except Exception as exc:
                    self.log(f"  {venue} {city} {kind}: context error: {exc}")
                    continue
                if ctx is None:
                    if kind == "high":
                        self.log(f"  {venue} {city} {kind}: no market today")
                    continue
                if not light:
                    probs = " ".join(f"{b.title.split('°')[0]}={p:.0%}" for b, p in zip(ctx.event.buckets, ctx.probs) if p >= 0.03)
                    self.log(f"  {venue} {city} {ctx.date} {kind} [{ctx.station}/{ctx.rule}] running={ctx.running} n_obs={ctx.n_obs} "
                             f"remaining={ctx.remaining_extreme} | model {probs}")
                for b in ctx.event.buckets:
                    self.ledger.add_snapshot(venue, b.yes_token, b.best_bid, b.best_ask, b.last)
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
        if self.us.available:
            us_counts = self.paper.settle_open("us", self.log)
            for k, v in us_counts.items():
                counts[k] = counts.get(k, 0) + v
        self.log(f"settle: {counts}")
        return counts

    def backtest(self, days: int = 7, cities=None, kinds=("high",)) -> str:
        out = f"{config.ROOT}/backtest-latest.json"
        summary = backtest.run(days, cities, kinds, self.cfg, self.log, out_path=out)
        text = backtest.format_summary(summary)
        self.log(text)
        return text

    def build_pairs(self) -> str:
        """Match Polymarket US markets to offshore twins (needs the key). Writes pairs.json."""
        if not self.us.available:
            return f"pairs: idle — {self.us.why_unavailable}"
        us_markets = []
        for e in self.us.events(limit=200, active=True):
            for m in e.get("markets", []) or []:
                us_markets.append({"slug": m.get("slug") or e.get("slug"), "title": m.get("title") or m.get("question") or e.get("title", ""),
                                   "end": m.get("endDate") or e.get("endDate"), "category": (e.get("category") or "").lower() or None})
        off = []
        for e in offshore.events_ending_within(14, limit=200):
            cat = offshore.event_category(e)
            for m in e.get("markets", []):
                toks = m.get("clobTokenIds")
                try:
                    tok = __import__("json").loads(toks or "[]")[0]
                except (ValueError, IndexError):
                    continue
                off.append({"token": tok, "title": m.get("question") or e.get("title", ""), "end": m.get("endDate") or e.get("endDate"), "category": cat})
        found = pairs.match_pairs(us_markets, off)
        path = pairs.save_pairs(found)
        return f"pairs: {len(found)} matched from {len(us_markets)} US × {len(off)} offshore markets → {path}"

    def reload_config_if_changed(self) -> bool:
        """Pick up an edited config.json without a restart. The loop used to read config once at
        startup, so flipping a module by hand did nothing until a relaunch — and worse, the next
        `promote` wrote the stale in-memory copy back over the edit."""
        try:
            mtime = os.path.getmtime(config.CONFIG_PATH)
        except OSError:
            return False
        if mtime == self._cfg_mtime:
            return False
        self._cfg_mtime = mtime
        before = dict(self.cfg.modes)
        self.cfg = config.load()
        self.ledger.gate_since_ts = self.cfg.gate_since_ts
        self.ledger.min_us_signals = self.cfg.min_us_signals
        changed = {m: (before.get(m), v) for m, v in self.cfg.modes.items() if before.get(m) != v}
        if changed:
            self.log("  config reloaded: " + ", ".join(f"{m} {a}->{b}" for m, (a, b) in changed.items()))
        return True

    def report(self, days: int = 1) -> str:
        text = self.ledger.report(days)
        with open(config.REPORT_PATH, "w") as f:
            f.write(text + "\n")
        return text

    def promote(self, modules=None) -> list:
        """Flip every paper module whose gate passes to live (or only the named ones), write config.json,
        and say so. Nothing else ever changes a mode. Returns the modules flipped."""
        flipped = []
        for m in (modules or config.MODULES):
            if self.cfg.mode(m) != "paper":
                if modules:
                    self.log(f"  promote {m}: mode is {self.cfg.mode(m)}, not paper")
                continue
            ok, why = self.ledger.promotion_check(m)
            if ok:
                self.cfg.modes[m] = "live"
                flipped.append(m)
                self.log(f"  promote {m}: paper -> LIVE ({why})")
            elif modules:
                self.log(f"  promote {m}: refused — {why}")
        if flipped:
            config.save(self.cfg)
            notify.nudge("polybot: LIVE", f"{', '.join(flipped)} passed the gate and now place real orders "
                         f"(caps ${self.cfg.caps.max_per_market_usd:.0f}/market, ${self.cfg.caps.max_exposure_usd:.0f} total, "
                         f"${self.cfg.caps.daily_loss_stop_usd:.0f} daily stop).", key="polybot-promote", log=self.log)
        return flipped

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
                    self.reload_config_if_changed()
                    # weather_lock first: it is the only module the 208-city-day backtest paid
                    # (+29.8% ROI vs weather_hold -14.5%), and in paper the per-market cap let
                    # whoever scanned first take the bucket — weather_hold refused 95 lock signals
                    # that way, starving the one strategy worth promoting.
                    if now.minute == 55:
                        self.scan_weather(modules=["weather_lock", "weather_model_update", "weather_hold", "weather_obs"])
                    # The US venue is scanned four times an hour, not once. It is the only venue that
                    # can ever hold real money and it carries ~1 signal a day — the scarcest resource
                    # in this bot — while offshore is 30 cities of proxy. A US pass used to cost ~20
                    # calls (5 cities x high+low x a slug lookup that always missed plus a search
                    # fallback); unwrapping the event envelope and remembering 404s for an hour cut
                    # that to ~5, which is what buys the extra passes without walking back into the
                    # Cloudflare rate limit that banned this IP on 2026-09-17.
                    if now.minute % 15 == 10 and self.us.available:
                        self.scan_weather(modules=["weather_lock", "weather_model_update", "weather_hold", "weather_obs"], venue="us")
                    if now.minute % 5 == 0:
                        self.scan_weather(modules=["bucket_sum"])
                        if self.us.available:
                            self.scan_weather(modules=["bucket_sum"], venue="us")
                        self.scan_other(modules=["leadlag", "maker_rewards"])
                        if self.us.available:
                            self.executor.sync()
                    if now.minute == 20:
                        self.settle()
                    if now.weekday() == 6 and now.hour == 4 and now.minute == 0:
                        self.backtest(7)
                    if now.hour == 5 and now.minute == 0 and self.us.available:
                        self.log(self.build_pairs())
                    if now.hour in (9, 21) and now.minute == 0:
                        self.scan_other(modules=["hold_favorites"])
                    if now.hour == 7 and now.minute == 0:
                        self.log(self.report(1))
                        promoted = self.promote() if self.cfg.auto_promote else []
                        line = self.ledger.summary(1)
                        if not promoted:
                            ready = [m for m in config.MODULES if self.cfg.mode(m) == "paper" and self.ledger.promotion_check(m)[0]]
                            if ready:
                                line += f" — say the word to go live: {', '.join(ready)}"
                        notify.nudge("polybot daily", line, key="polybot-daily", log=self.log)
                    if now.hour == 3 and now.minute == 0:
                        calibration.save_table(calibration.build(log=self.log))
                        self.log(f"pruned {self.ledger.prune_snapshots(self.cfg.snapshot_keep_days)} snapshots older than {self.cfg.snapshot_keep_days}d")
                except Exception as exc:
                    self.log(f"loop error: {exc}\n{traceback.format_exc(limit=3)}")
                if len(done) > 5000:
                    done = set(sorted(done)[-100:])
            _beat("polybot", 3 * 3600, f"{self.cfg.mode('weather_lock')} lock")
            try:
                ready = [m for m in config.MODULES
                         if self.cfg.mode(m) == "paper" and self.ledger.promotion_check(m)[0]]
                _publish("polybot", {
                    "modes": {m: self.cfg.mode(m) for m in config.MODULES
                              if self.cfg.mode(m) != "off"},
                    "signals_24h": self.ledger.signal_count_since(time.time() - 86400)
                    if hasattr(self.ledger, "signal_count_since") else None,
                    "ready_to_promote": ready,
                    "live_orders": self.ledger.live_order_count()
                    if hasattr(self.ledger, "live_order_count") else None,
                    "bankroll_usd": self.cfg.bankroll_usd})
            except Exception:
                pass
            time.sleep(20)


def _stamped_log(*parts):
    """The loop's log had no timestamps, which on 2026-09-14 made a 20-minute DNS blackout
    indistinguishable from a bug, and again on 2026-09-18 made it impossible to tell a fresh
    sqlite error from one written on day one. launchd appends this file forever, so every line
    carries the wall clock."""
    print(datetime.now(ET).strftime("%m-%d %H:%M:%S"), *parts, flush=True)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="polybot")
    ap.add_argument("cmd", choices=["scan", "settle", "report", "calibrate", "status", "loop", "backtest", "pairs", "promote"])
    ap.add_argument("--city", action="append")
    ap.add_argument("--modules", nargs="*")
    ap.add_argument("--venue", default="offshore", choices=["offshore", "us"], help="scan: which books to read")
    ap.add_argument("--days", type=int, default=1)
    ap.add_argument("--events", type=int, default=300)
    ap.add_argument("--kinds", nargs="*", default=["high"])
    a = ap.parse_args(argv)
    r = Runner(log=_stamped_log if a.cmd == "loop" else print)
    if a.cmd == "backtest":
        print(r.backtest(a.days if a.days > 1 else 7, a.city, tuple(a.kinds)))
    elif a.cmd == "pairs":
        print(r.build_pairs())
    elif a.cmd == "scan":
        n = r.scan(a.city, a.modules) if a.venue == "offshore" else r.scan_weather(a.city, a.modules, venue="us")
        print(f"{n} signal(s) recorded")
    elif a.cmd == "settle":
        r.settle()
    elif a.cmd == "report":
        print(r.report(a.days))
    elif a.cmd == "promote":
        flipped = r.promote(a.modules)
        print(f"promoted: {', '.join(flipped) if flipped else 'nothing (see the gate lines in `report`)'}")
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
