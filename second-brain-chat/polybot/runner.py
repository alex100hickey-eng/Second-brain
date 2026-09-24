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
import faulthandler
import traceback
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import backtest, calibration, config, notify, pairs
from .execution import Executor
from .feeds import offshore
from .feeds.usvenue import USVenue, buckets_from_markets
from .ledger import Ledger
from .paper import PaperEngine, snapshot_history
from .risk import RiskManager
from . import universe


class _Ev:
    """The two attributes the arb code reads off a weather event."""

    def __init__(self, slug, buckets):
        self.slug, self.buckets = slug, buckets


class _UniverseCtx:
    """A weather ctx has a model and observations; a Fed decision has neither. The arb only ever
    reads event/venue/city/date/kind, plus the settlement proof that stands in for tiling and the
    horizon that turns cents-per-set into cents per dollar-day."""

    proven_exhaustive = True

    def __init__(self, event, venue, city, date, kind, settles_in_days=None):
        self.event, self.venue, self.city, self.date, self.kind = event, venue, city, date, kind
        self.settles_in_days = settles_in_days


def _scan_date(city: str, date, day_offset: int):
    """The calendar day a (city, day_offset) pass asks for — the CITY's own day, not the Mac's.

    One place on purpose: the prefetch has to build exactly the slugs `_scan_one` will go on to
    ask for, and near midnight New York and Los Angeles are on different dates. A mismatch here
    would not error, it would just turn every prefetch into a miss and quietly undo the batching.
    """
    now_local = datetime.now(ZoneInfo(config.city_meta(city)["tz"]))
    return (date or now_local) + timedelta(days=day_offset)


def _days_until(iso: str | None) -> float | None:
    """Days from now to an ISO timestamp, floored at half a day. None when unparseable — and the
    arb then falls back to the flat cents threshold rather than inventing a horizon."""
    if not iso:
        return None
    try:
        txt = str(iso).replace("Z", "+00:00")
        when = datetime.fromisoformat(txt)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return max((when - datetime.now(timezone.utc)).total_seconds() / 86400.0, 0.5)
    except (ValueError, TypeError):
        return None
from .strategies.bucket_sum import (consume_levels, BucketSum, arb_check, arb_possible, explain_no_set, unpriced,
                                    worth_confirming)
from .strategies.hold_favorites import HoldFavorites
from .strategies.leadlag import LeadLag
from .strategies import maker_rewards as maker_mod
from .strategies.maker_rewards import MakerRewards
from .strategies.weather import WeatherHold, WeatherLock, WeatherModelUpdate, WeatherObs, build_ctx

ET = ZoneInfo("America/New_York")
DEDUPE_S = 3 * 3600
# The Python watchdog thread fired 5.5 minutes late on its first real stall (630s against a 300s
# limit) because the main thread was stuck in a C call holding the GIL — the same reason httpx's
# own 10s timeout never fired. faulthandler's timer runs in a C thread that does not need the
# GIL, so it fires on time whatever Python is doing, and it prints the stack of every thread on
# the way out: the first hard evidence of WHERE these hangs actually are.
# 240s soft / 300s hard, not tighter. The longest LEGITIMATE gap between heartbeats is one
# city-day: six price_legs calls against a window the venue may have widened to 60s is ~120-150s
# cold. A false restart costs a cold cache and a skipped pass, so the limit sits above that with
# room, and the win comes from stamping progress DURING a pass rather than from cutting it fine.
WATCHDOG_HARD_S = 300.0


def _arm_hard_watchdog(seconds: float) -> None:
    """(Re)start the GIL-proof timer. Called on every loop iteration, so it only expires when the
    loop genuinely stops coming back."""
    try:
        faulthandler.cancel_dump_traceback_later()
        faulthandler.dump_traceback_later(max(seconds, 60.0), exit=True)
    except Exception:
        pass        # a watchdog that breaks the loop is worse than no watchdog


# Hours the arb sweep owns (see _arb_interval_s). Long jobs that hold the loop stay out of them.
ARB_HOURS = range(9, 17)
QUIET_REPEAT_S = 600.0
JOB_RETRY_S = 3600.0
UNIVERSE_SLOTS = ((6, 30), (18, 30))
JOBS_PATH = os.path.join(config.ROOT, "jobs-state.json")
# Where the leadlag universe comes from. US: every non-sports category events.list honours.
# Offshore: the gamma tags those questions live under (checked 2026-09-23).
# "technology" is its own category (6 events on 2026-09-23); asking for "tech" returns nothing.
PAIR_US_CATEGORIES = ("politics", "macro", "culture", "finance", "climate", "crypto", "geopolitics", "science",
                      "technology")
PAIR_OFFSHORE_TAGS = ("politics", "elections", "midterms", "us-politics", "trump", "economy", "fed", "fed-rates",
                      "economic-policy", "inflation", "geopolitics", "world", "pop-culture", "awards", "oscars",
                      "finance", "tech", "science", "climate", "crypto-prices", "bitcoin")


def _last_slot(now, hours, weekday=None):
    """The latest datetime at one of `hours` (ET) that is not after `now`. An hour is an int (on the
    hour) or (hour, minute); `weekday` (0 = Monday) makes the slot weekly."""
    best = None
    for back in range(8 if weekday is not None else 2):
        day = (now - timedelta(days=back)).date()
        if weekday is not None and day.weekday() != weekday:
            continue
        for h in hours:
            hh, mm = h if isinstance(h, tuple) else (h, 0)
            t = datetime(day.year, day.month, day.day, hh, mm, tzinfo=ET)
            if t <= now and (best is None or t > best):
                best = t
    return best


def _load_jobs(path: str | None = None) -> dict:
    try:
        with open(path or JOBS_PATH) as f:
            return {k: float(v) for k, v in json.load(f).items()}
    except (OSError, ValueError, AttributeError):
        return {}


def _save_jobs(jobs: dict, path: str | None = None) -> None:
    try:
        tmp = (path or JOBS_PATH) + ".tmp"
        with open(tmp, "w") as f:
            json.dump(jobs, f)
        os.replace(tmp, path or JOBS_PATH)
    except OSError:
        pass          # losing the record costs one repeated job, never a missed trade


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
        self.us.on_backoff = lambda msg: self.log(f"  {msg}")
        self.risk = RiskManager(self.cfg, self.ledger)
        self.paper = PaperEngine(self.ledger, history_fn=self._paper_history, resolution_fn=self._paper_resolution)
        self.executor = Executor(self.ledger, self.us, self.cfg, self.log)
        self.uni = universe.Universe(self.ledger.conn)
        self.arb = BucketSum(self.cfg)      # the universe path runs the arb outside scan_weather
        self.pair_rec = pairs.PairRecorder(self.ledger, self.us, log=self.log)
        self._pairs_cache = (None, [])
        self._quiet: dict = {}              # log-line dedupe, see _quiet_log
        self._jobs = _load_jobs()
        self._attempts: dict = {}
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
            "leadlag": LeadLag(self.cfg, self.us, self.pair_rec, quote_fn=self.pair_rec.quote,
                               pairs_fn=self.leadlag_pairs),
            "maker_rewards": MakerRewards(self.cfg, self.us, self.ledger),
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

    def refresh_universe(self) -> str:
        """Re-discover the multi-outcome catalogue, re-tier it, and promote any series a settled
        instance has now proved. ~20 API calls, so this runs twice a day, not every tick."""
        if not self.us.available:
            return f"universe: venue unavailable ({self.us.why_unavailable})"
        found = self.us.discover(universe.DISCOVERY_QUERIES)
        tiers = {}
        for slug, e in found.items():
            tier, _ = self.uni.record(e)
            tiers[tier] = tiers.get(tier, 0) + 1
            # A closed event with exactly one winner proves its whole series for good.
            if self.uni.prove(e):
                self.log(f"  universe: series {universe.series_key(slug)} PROVED by {slug}")
        proved = self.prove_pending()
        return (f"universe: {len(found)} multi-outcome events — "
                + ", ".join(f"{n} {t}" for t, n in sorted(tiers.items()))
                + (f"; proved {proved} series" if proved else ""))

    def scan_universe(self) -> int:
        """Run the arb over every PROVEN multi-outcome event, not just the ten weather markets.

        This is the whole point of the universe. Weather is five cities, one market each, inside a
        six-hour window — about two signals a day, which is why the bot has so few shots. The same
        arb applies to every one-winner event on the venue, and the registry is what makes that
        safe: only series a SETTLED instance has proved may trade here.
        """
        if not self.us.available:
            return 0
        n = 0
        for row in self.uni.tradable():
            slug = row["slug"]
            e = self.us.event(slug)
            markets = (e or {}).get("markets") or []
            if not markets:
                continue
            buckets = buckets_from_markets(markets)
            if any(b.closed for b in buckets):
                continue
            if unpriced(buckets) and arb_possible(buckets, assume_exhaustive=True):
                got = self.us.price_legs(buckets)
                if got:
                    self.log(f"  arb screen us {slug}: priced {got} unquoted leg(s)")
            kind, net, _ = arb_check(buckets, "us", assume_exhaustive=True)
            for b in buckets:
                self.ledger.add_snapshot("us", b.yes_token, b.best_bid, b.best_ask, b.last)
            days = _days_until((e or {}).get("endDate"))
            if kind is None or not worth_confirming(buckets, net, self.cfg, days):
                continue
            # Everything below mirrors the weather path deliberately. These are the same bugs
            # already fixed there, and a second copy of the arb that quietly lacks the fixes is
            # worse than no second copy: it looks supported and is not.
            side = "BUY_YES" if kind == "buy_all" else "BUY_NO"
            if any(self.ledger.recent_signal_exists("bucket_sum", b.yes_token, side,
                                                    self.cfg.arb_dedupe_s) for b in buckets):
                self._quiet_log((slug, "taken"), [f"  arb candidate us {slug} {kind} {net:.1f}c/set — "
                                                  f"already taken this episode, not re-confirming"])
                continue
            got = self.us.fill_depth_buckets(buckets)
            confirm = [f"  arb candidate us {slug} {kind} {net:.1f}c/set ({len(buckets)} legs) — "
                       f"depth {'read' if got else 'INCOMPLETE, standing down'}"]
            if not got:
                self._quiet_log((slug, "confirm"), confirm)
                continue          # the return was being ignored; unknown depth is not zero depth
            held_max = 0.0
            for b in buckets:     # a position we already hold has eaten that liquidity
                held = self.ledger.held_contracts("us", b.yes_token, "bucket_sum")
                held_max = max(held_max, held)
                if held <= 0:
                    continue
                if kind == "buy_all":
                    b.ask_levels = consume_levels(b.ask_levels, held)
                    b.ask_qty = sum(q for px, q in b.ask_levels
                                    if b.ask_levels and px == b.ask_levels[0][0]) or 0.0
                else:
                    b.bid_levels = consume_levels(b.bid_levels, held)
                    b.bid_qty = sum(q for px, q in b.bid_levels
                                    if b.bid_levels and px == b.bid_levels[0][0]) or 0.0
            for b in buckets:
                self.ledger.add_snapshot("us", b.yes_token, b.best_bid, b.best_ask, b.last,
                                         bid_qty=b.bid_qty, ask_qty=b.ask_qty,
                                         bid_levels=getattr(b, "bid_levels", None),
                                         ask_levels=getattr(b, "ask_levels", None))
            ctx = _UniverseCtx(event=_Ev(slug, buckets), venue="us", city=row["series"],
                               date=slug[-10:], kind=row["category"] or "event",
                               settles_in_days=days)
            sigs = list(self.arb.scan(ctx))
            if sigs:
                self._quiet.pop((slug, "confirm"), None)
                for line in confirm:
                    self.log(line)
                n += self.handle_arb_set(sigs)
            else:
                confirm.append("    no set: " + explain_no_set(buckets, kind, self.cfg, days, held_max))
                self._quiet_log((slug, "confirm"), confirm)
        return n

    def prove_by_date_sweep(self, days_back: int = 200, series=None, max_series: int = 6) -> int:
        """Prove a recurring series by finding ANY past instance of it that settled.

        A series otherwise sits unprovable until its next instance resolves — banxico meets eight
        times a year, an election is once. But `events.list(slug=[...])` takes twenty slugs per
        call and returns closed events with their markets, so asking "did <series>-<date> exist?"
        for every date in the last N days costs ~10 calls and needs no knowledge of the schedule.
        This is how usfed-fomc became tradable today instead of on 2026-10-28.
        """
        from datetime import date, timedelta
        # Each series costs ~10 calls (200 dates, 20 to a request), so cap the batch: the venue's
        # quota is five requests per window and this IP is shared with the whole campus.
        targets = series if series is not None else self.uni.dated_unproven_series()[:max_series]
        if not targets:
            return 0
        today = date.today()
        proved = 0
        for ser in targets:
            slugs = [f"{ser}-{(today - timedelta(days=d)).isoformat()}" for d in range(1, days_back + 1)]
            found = self.us.events_by_slug(slugs)
            for slug, e in found.items():
                if self.uni.prove(e):
                    self.log(f"  universe: series {ser} PROVED by {slug} "
                             f"({len(e.get('markets') or [])} legs, exactly one winner)")
                    proved += 1
                    break            # one settled instance is enough for the whole series
        return proved

    def prove_pending(self, extra_slugs=()) -> int:
        """Re-ask the venue about unproven WATCH events and promote any series that has settled.

        This is what makes the universe compound: a series sits in WATCH until one of its
        instances closes with exactly one winner, and then every future instance is tradable
        without asking again. `events.list(slug=[...])` returns closed events with their markets,
        twenty to a call, so checking the whole watchlist costs one or two requests.
        """
        slugs = list(self.uni.unproven_slugs()) + list(extra_slugs)
        if not slugs:
            return 0
        proved = 0
        for slug, e in self.us.events_by_slug(slugs).items():
            if self.uni.prove(e):
                self.log(f"  universe: series {universe.series_key(slug)} PROVED by {slug} "
                         f"({len(e.get('markets') or [])} legs, exactly one winner)")
                proved += 1
        return proved

    def handle_arb_set(self, sigs) -> int:
        """An arb set is one decision, so it is accepted or refused whole.

        Checking legs one at a time is how you end up holding four of six: the dear legs pass, a
        cheap one trips a cap, and what is left on the book is a bet nobody sized. Every leg must
        clear risk before ANY of them is recorded, and in live mode the whole set goes to the
        executor, which fills it all or unwinds what filled.
        """
        if not sigs:
            return 0
        mode = self.cfg.mode(sigs[0].module)
        if mode == "off":
            return 0
        if mode == "live" and any(s.venue != "us" for s in sigs):
            mode = "paper"
        # An arb is not a view, so the 3-hour window that stops a directional module re-entering
        # the same bucket is the wrong rule here. A set is self-liquidating -- it pays $1 whatever
        # happens -- so taking the same arb twice is two independent profitable trades, not a
        # doubled opinion, and what should limit it is the exposure and per-market caps that
        # already do. The 3-hour window cost real repeats: on 2026-09-19 miami offered sets at
        # 11:47, 11:48, 11:51, 12:10 and 12:13; the first was taken and every later one was
        # refused, including 12:10 at 10.7c/set, which was worth MORE than the one taken.
        #
        # A short window is still wanted. At a 20s sweep the same episode is seen several times
        # over, and in paper that would book the same set repeatedly and overstate the strategy.
        # Three minutes is long enough to cover one episode and short enough to let the next one
        # through.
        window = self.cfg.arb_dedupe_s if any(s.arb for s in sigs) else DEDUPE_S
        if any(self.ledger.recent_signal_exists(s.module, s.market, s.side, window) for s in sigs):
            return 0
        for s in sigs:
            ok, why = self.risk.allow(s, mode=mode)
            if not ok:
                self.log(f"    refused arb set {sigs[0].label}: leg {s.label}: {why}")
                return 0
        # The per-leg risk check cannot see its siblings. Every leg is measured against the SAME
        # exposure baseline, because none of them is recorded until they all pass — so six legs of
        # $20 each sail through individually and then put $120 on a book that only had $80 of room.
        # Proved against live settings: existing exposure $100, a $120 set, all six legs "ok",
        # resulting exposure $220 against a $180 cap. A set is one decision and has to be measured
        # as one.
        cost = sum(s.size_usd for s in sigs)
        if mode == "live":
            total = self.ledger.exposure_usd("us", None, live_only=True)
            if total + cost > self.cfg.caps.max_exposure_usd + 1e-9:
                self.log(f"    refused arb set {sigs[0].label}: set ${cost:.2f} + open ${total:.2f} "
                         f"over exposure cap ${self.cfg.caps.max_exposure_usd:.0f}")
                return 0
            # Only the exposure cap needs the set-level view. The bankroll floor compares account
            # VALUE to the floor, which is identical for every leg, so risk.allow already has it —
            # inventing a stricter "keep the floor uncommitted" rule here would quietly change a
            # limit Alex set.
        legs = [(self.ledger.add_signal(s, mode), s) for s in sigs]
        self.log(f"    {mode.upper():<6} arb set {sigs[0].module} {len(sigs)} legs, {sigs[0].contracts} sets, "
                 f"${cost:.2f} in for $1.00/set out — {sigs[0].reason}")
        profit = (sigs[0].edge_cents / 100.0) * sigs[0].contracts
        if mode == "signal":
            notify.nudge(f"polybot arb: {sigs[0].label}",
                         f"{len(sigs)} legs, {sigs[0].contracts} sets, ${cost:.2f} for "
                         f"{sigs[0].edge_cents:.1f}c/set. Must be taken together.",
                         key="polybot-arb", log=self.log)
        elif mode == "paper" and profit >= self.cfg.arb_notify_usd:
            # Almost every set is worth pennies, and those can stay in the log. A big one is a
            # different event: it is the case the whole strategy exists for, it lasts about a
            # minute, and while arb_live_ok is off it passes by with nobody told. Say so, so the
            # decision to go live is made against a real opportunity rather than a backtest.
            #
            # And say whether it was even takeable. "You missed $6.84" means something different
            # depending on whether the money was there: on 2026-09-19 the account held $272.50 of
            # value and $0.06 of spendable cash, all of it in positions Alex had opened himself.
            # An alert that leaves that out invites him to flip the switch into an order that
            # cannot fill.
            why = f"Paper only: arb_live_ok is off."
            try:
                cash = self.us.balance_usd()
            except Exception:
                cash = None
            if cash is not None and cash + 1e-9 < cost:
                why += (f" Live could NOT have taken it either: buying power ${cash:.2f} "
                        f"against a ${cost:.2f} set.")
            notify.nudge(f"polybot arb (paper): ${profit:.2f} on the table",
                         f"{sigs[0].label} — {len(sigs)} legs, {sigs[0].contracts} sets, "
                         f"${cost:.2f} in at {sigs[0].edge_cents:.1f}c/set. {why}",
                         key="polybot-arb-big", log=self.log)
        if mode == "live":
            self.executor.place_arb_set(legs)
        return len(legs)

    # ---- scans -----------------------------------------------------------------------------
    def scan_weather(self, cities=None, modules=None, date: datetime | None = None, kinds=None,
                     venue: str = "offshore", day_offsets=None) -> int:
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
        # An arb does not care which day the market settles: if the buckets tile, exactly one pays
        # $1 whenever it resolves. Polymarket US lists tomorrow's event beside today's, and
        # tomorrow's book is the thinner, worse-quoted one — which is where a set under $1 is MORE
        # likely, not less. Only the arb path takes this: weather_lock and weather_obs trade on
        # observations of a day in progress and have nothing to say about tomorrow.
        offsets = day_offsets if day_offsets is not None else ((0, 1) if (light and venue == "us") else (0,))
        # A scan pass has a wall clock, not just a call budget. Arb episodes last a median of 4 minutes, up to 15,
        # so five cities scanned slowly is worth less than three scanned now — and on 2026-09-19 a
        # pass stalled after its first city and held the loop for fifteen minutes, which no amount
        # of cadence tuning upstream can fix. Abandon the tail and let the next tick start clean.
        deadline = time.time() + self.cfg.arb_pass_budget_s if venue == "us" else None
        # Ask for every event this pass needs in ONE request before touching the first city. Ten
        # separate lookups was more than the venue's whole per-window quota, so the pass spent its
        # budget on housekeeping and finished a minute late — see prefetch_weather_events.
        if venue == "us":
            try:
                self.us.prefetch_weather_events(
                    (city, _scan_date(city, date, off), kind)
                    for city in cities for kind in kinds for off in offsets)
            except Exception as exc:
                self.log(f"  us prefetch failed ({exc}) — falling back to per-event lookups")
        skipped = 0
        jobs = [(city, kind, off) for city in cities for kind in kinds for off in offsets]
        # When the deadline bites it always bit the same way: the loop ran cities in a fixed order,
        # so the tail of the list was the part that got dropped, every single pass. With ten
        # city-days and a 45s budget that meant san-francisco and ALL of tomorrow were
        # systematically never screened while the log honestly reported "skipped 4" each time.
        # Rotate the starting point so the cost of running out of time is shared out instead of
        # falling on the same books forever.
        if deadline and len(jobs) > 1:
            self._scan_rot = (getattr(self, "_scan_rot", 0) + 1) % len(jobs)
            jobs = jobs[self._scan_rot:] + jobs[:self._scan_rot]
        for city, kind, day_offset in jobs:
            if deadline and time.time() > deadline:
                skipped += 1
                continue
            # Progress, not just arrival. A pass can legitimately take a minute, and without a
            # stamp here the watchdog cannot tell a slow pass from a hung socket — which is why
            # its limit had to be set generously enough to sit through most of a real stall.
            self._heartbeat = time.time()
            try:
                n += self._scan_one(city, kind, day_offset, wanted, light, venue, date)
            except Exception as exc:
                # One book's bad luck is not the other nine books' problem. _scan_one guards its
                # context build, but the arb screen after it — price_legs, fill_depth — was
                # unprotected, so a single timed-out request took the WHOLE sweep down:
                #   17:52:22  arb sweep error: Request timed out.  (httpx, inside scan_weather)
                # Ten books went unscreened because one of them was slow. Log it, move on, and
                # let the next book have its turn.
                self.log(f"  {venue} {city} {kind} +{day_offset}d: scan failed ({exc}) — "
                         f"skipping this book, continuing the sweep")
        if skipped:
            self.log(f"  us scan over its {self.cfg.arb_pass_budget_s:.0f}s budget — skipped "
                     f"{skipped} city-day(s); next tick starts fresh")
        return n

    def _scan_one(self, city, kind, day_offset, wanted, light, venue, date) -> int:
        """One (city, kind, day): build the context, snapshot the book, run the wanted modules."""
        n = 0
        try:
            scan_date = _scan_date(city, date, day_offset)
            fetch = {"members": [], "obs": [], "hourly": []} if light else {}
            if venue == "us":
                event = self.us.find_weather_event(city, scan_date, kind)
                if event is None:
                    if kind == "high" and day_offset == 0:
                        self.log(f"  us {city} {kind}: no market today")
                    return 0
                fetch["event"] = event
            ctx = build_ctx(city, scan_date, kind, self.cfg, venue=venue, fetch=fetch or None)
        except Exception as exc:
            self.log(f"  {venue} {city} {kind}: context error: {exc}")
            return 0
        if ctx is None:
            if kind == "high" and day_offset == 0:
                self.log(f"  {venue} {city} {kind}: no market today")
            return 0
        if not light:
            probs = " ".join(f"{b.title.split('°')[0]}={p:.0%}" for b, p in zip(ctx.event.buckets, ctx.probs) if p >= 0.03)
            self.log(f"  {venue} {city} {ctx.date} {kind} [{ctx.station}/{ctx.rule}] running={ctx.running} n_obs={ctx.n_obs} "
                     f"remaining={ctx.remaining_extreme} | model {probs}")
        # US weather settles on the NWS daily climate report at 8 AM ET the morning AFTER the
        # market's date, so a set bought today is capital locked for roughly a day — which is what
        # makes it worth many times a central-bank set paying more cents six weeks out.
        if venue == "us":
            ctx.settles_in_days = _days_until(f"{ctx.date}T12:00:00+00:00") or 1.0
            ctx.settles_in_days = max(ctx.settles_in_days, 0.5) + 0.5
        for b in ctx.event.buckets:
            self.ledger.add_snapshot(venue, b.yes_token, b.best_bid, b.best_ask, b.last)
        confirm = confirm_key = confirm_arb = None     # an arb confirm waiting to be told, see below
        # Cheap screen, expensive confirm. The quotes come free with the event (one call); depth
        # costs a book call per bucket. bucket_sum needs depth to size a set at all, so look it up
        # only when the quotes say a set might be there — 34 event-minutes out of 3,353 over 8 days
        # of US books, i.e. ~1% of the scans pay for it.
        if venue == "us" and "bucket_sum" in wanted:
            # The event object omits `bestAskQuote` on buckets that DO have resting offers, and
            # arb_check needs an ask on every leg — so across 9 days the screen evaluated 36 of
            # 1,570 event-minutes that could have held a set (2%). Price the missing legs from the
            # book first: usually one call, because the usual case is exactly one unquoted leg.
            # This is not optimism — miami on 2026-09-18 had four legs quoted at 0.04 total and a
            # favourite with no ask at ANY price, which the bound alone would call a 96c arb.
            if unpriced(ctx.event.buckets) and arb_possible(ctx.event.buckets):
                got = self.us.price_legs(ctx.event.buckets)
                if got:
                    self.log(f"  arb screen {venue} {city} {ctx.date} {kind}: priced {got} unquoted "
                             f"leg(s) from the book")
            arb_kind, net, _ = arb_check(ctx.event.buckets, venue)
            taken = False
            if arb_kind is not None:
                # Cheap filter before the expensive call, again. handle_arb_set re-arms a set
                # after arb_dedupe_s, but it does that AFTER the depth read — so an episode that
                # persists costs six calls a sweep to confirm something already decided. Miami on
                # 2026-09-19 produced candidates at 13:57:40, 13:58:14, 13:58:47 and 13:59:20;
                # the first was taken and the rest each paid six calls to be refused. That quota
                # is shared with every other book, and a depth read losing it is exactly what
                # stood down the best set on record.
                side = "BUY_YES" if arb_kind == "buy_all" else "BUY_NO"
                taken = any(self.ledger.recent_signal_exists("bucket_sum", b.yes_token, side,
                                                             self.cfg.arb_dedupe_s)
                            for b in ctx.event.buckets)
            if taken:
                # Skip the confirm, not the whole scan: `wanted` can carry the weather modules too
                # (scan_weather with modules=None), and they have their own work to do here.
                self._quiet_log((ctx.event.slug, "taken"),
                                [f"  arb candidate {venue} {city} {ctx.date} {kind} {arb_kind} "
                                 f"{net:.1f}c/set — already taken this episode, not re-confirming"])
            elif arb_kind is not None and worth_confirming(ctx.event.buckets, net, self.cfg,
                                                           getattr(ctx, "settles_in_days", None)):
                got = self.us.fill_depth(ctx.event)
                held_max = 0.0
                if got:
                    # A position we already hold has consumed that liquidity. Paper orders do not,
                    # so without this the same mispricing is bought over and over against the same
                    # contracts -- miami was booked twice five minutes apart, 62 sets each, while
                    # the binding leg showed the SAME 79 contracts both times. Sizing, not pricing:
                    # best_bid/best_ask stay the real market so the logs keep telling the truth.
                    for b in ctx.event.buckets:
                        held = self.ledger.held_contracts(venue, b.yes_token, "bucket_sum")
                        held_max = max(held_max, held)
                        if held <= 0:
                            continue
                        if arb_kind == "buy_all":
                            b.ask_levels = consume_levels(b.ask_levels, held)
                            b.ask_qty = sum(q for px, q in b.ask_levels
                                            if b.ask_levels and px == b.ask_levels[0][0]) or 0.0
                        else:
                            b.bid_levels = consume_levels(b.bid_levels, held)
                            b.bid_qty = sum(q for px, q in b.bid_levels
                                            if b.bid_levels and px == b.bid_levels[0][0]) or 0.0
                confirm = [f"  arb candidate {venue} {city} {ctx.date} {kind} {arb_kind} {net:.1f}c/set — "
                           f"depth {'read' if got else 'INCOMPLETE, standing down'}"]
                confirm_key = (ctx.event.slug, "confirm")
                confirm_arb = (arb_kind, getattr(ctx, "settles_in_days", None), held_max)
                if got:
                    # Say what the BOOK said, not just what the quotes promised. Six of the eight
                    # candidates that got this far produced no set and gave no reason, which is
                    # this bot's oldest failure mode wearing a new hat — the answer turned out to
                    # be legs with no offers at any price, but that took an evening of forensics
                    # on the snapshot table to establish. One line makes it readable live.
                    post_kind, post_net, _ = arb_check(ctx.event.buckets, venue)
                    depths = [b.ask_qty if arb_kind == "buy_all" else b.bid_qty
                              for b in ctx.event.buckets]
                    known = [d for d in depths if d is not None]
                    confirm.append(f"    book says {post_kind or 'NO ARB'} "
                                   f"{post_net:.1f}c/set (screen said {net:.1f}c), "
                                   f"thinnest leg {min(known) if known else '?'} contracts"
                                   + (" — a leg has no offers at any price"
                                      if known and min(known) == 0 else ""))
                else:
                    self._quiet_log(confirm_key, confirm)
                    confirm = None
                # Record the book WITH sizes. Whether these arbs are big enough to be worth taking
                # is the one question the old snapshots cannot answer, so every candidate leaves
                # evidence behind whether or not it trades.
                for b in ctx.event.buckets:
                    self.ledger.add_snapshot(venue, b.yes_token, b.best_bid, b.best_ask, b.last,
                                             bid_qty=b.bid_qty, ask_qty=b.ask_qty,
                                             bid_levels=getattr(b, "bid_levels", None),
                                             ask_levels=getattr(b, "ask_levels", None))
        for name in wanted:
            try:
                sigs = list(self.weather_modules[name].scan(ctx))
                if sigs and all(s.arb for s in sigs):
                    if confirm is not None:          # a new set is news: always tell it
                        self._quiet.pop(confirm_key, None)
                        for line in confirm:
                            self.log(line)
                        confirm = None
                    n += self.handle_arb_set(sigs)      # all legs or none
                    continue
                for sig in sigs:
                    if self.handle(sig) in ("paper", "signal", "live"):
                        n += 1
            except Exception as exc:
                self.log(f"  {name} error: {exc}\n{traceback.format_exc(limit=2)}")
        if not light and ctx.model_probs and self.cfg.mode("weather_model_update") == "off":
            # Keep the evidence while the module is off. It writes its own runs when it is on; off, the
            # loop wrote none after 2026-09-18, so the one question left about it — does it work on
            # the US venue? — could never be replayed. ~30 rows an hour.
            self.ledger.add_model_run(ctx.city, ctx.date, ctx.kind, list(ctx.model_probs), venue=venue)
        if confirm is not None:
            # The book was read and nothing was booked. Say why — once, not every 20 s sweep.
            kind_, days_, held_ = confirm_arb
            confirm.append("    no set: " + explain_no_set(ctx.event.buckets, kind_, self.cfg, days_, held_))
            self._quiet_log(confirm_key, confirm)
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
        if self.us.available and self._us_settle_due():
            self._last_us_settle = time.time()
            us_counts = self.paper.settle_open("us", self.log)
            for k, v in us_counts.items():
                counts[k] = counts.get(k, 0) + v
        self.log(f"settle: {counts}")
        return counts

    def _us_settle_due(self, now=None) -> bool:
        """Is this an hour where asking the US venue for settlements is worth the quota?

        Every open US position costs a resolution() call, so an hourly settle is a burst of ~25
        requests into a budget of five per window. It tripped the limiter at 14:20:53 on
        2026-09-19 and blinded the arb sweep for fifteen seconds, then widened the window on top
        of that — for answers that could not exist: US weather settles on the NWS climate report
        at 8 AM ET the morning AFTER the market's date, so every mid-day call asks a question
        whose answer is certainly "not yet".

        So stay out of the hours the arb sweep owns. 08:20 still runs, right after settlement,
        and so does every hour through the evening and overnight — sixteen chances a day at
        something that happens once. Nothing settles later than it would have; the calls that
        stop happening are only the ones that were always going to say no.
        """
        hour = (now or datetime.now(ET)).hour
        if not (9 <= hour <= 16):
            return True
        # ...unless we have simply missed it. The jobs are keyed to an exact minute, so a stall or
        # a watchdog restart across :20 loses that tick entirely — the 17:20 settle vanished today
        # inside the stall the watchdog killed at 17:22. Harmless most hours; not harmless at
        # 08:20, because the next allowed hour after a missed 08:20 is 17:20, and that is nine
        # hours of not knowing what the overnight sets paid. Three hours is the most this may
        # quietly drift.
        last = getattr(self, "_last_us_settle", 0.0)
        return (time.time() - last) >= 3 * 3600

    def backtest(self, days: int = 7, cities=None, kinds=("high",)) -> str:
        out = f"{config.ROOT}/backtest-latest.json"
        summary = backtest.run(days, cities, kinds, self.cfg, self.log, out_path=out)
        text = backtest.format_summary(summary)
        self.log(text)
        return text

    def build_pairs(self) -> str:
        """Match Polymarket US markets to the offshore markets asking the same question. Writes
        pairs.json, leadlag's universe. ~20 US calls (the catalogue by category, 100 to a call)
        and ~60 gamma calls, so it runs once a day outside the arb window, not every tick."""
        if not self.us.available:
            return f"pairs: idle — {self.us.why_unavailable}"
        cats = PAIR_US_CATEGORIES + (("sports",) if self.cfg.caps.sports_enabled else ())
        us_events = list(self.us.events_by_category(cats).values())
        if not us_events:
            return "pairs: the US catalogue came back empty — keeping the pairs we had"
        tags = PAIR_OFFSHORE_TAGS + (("sports",) if self.cfg.caps.sports_enabled else ())
        off = {}
        for tag in tags:
            try:
                for e in offshore.active_events_by_tag(tag):
                    off[e.get("id")] = e
            except Exception as exc:
                self.log(f"  pairs: offshore tag {tag} failed ({exc})")
        found, counts = pairs.match_events(us_events, list(off.values()))
        if not found:
            return (f"pairs: 0 matched from {len(us_events)} US × {len(off)} offshore events — "
                    "keeping the pairs we had")
        path = pairs.save_pairs(found)
        self._pairs_cache = (None, [])
        return (f"pairs: {len(found)} market pairs over {counts['events_matched']} of {len(us_events)} US events "
                f"({counts['quoted_events']} with a two-sided US quote; {counts['gap_rejected']} refused on a "
                f"price gap over {pairs.MAX_PRICE_GAP:.0%}) × {len(off)} offshore events → {path}")

    def leadlag_pairs(self) -> list:
        """pairs.json, re-read only when the file changes, without the categories leadlag may not
        trade (sports, until the Ohio switch is flipped)."""
        try:
            mtime = os.path.getmtime(pairs.PAIRS_PATH)
        except OSError:
            return []
        if self._pairs_cache[0] != mtime:
            self._pairs_cache = (mtime, pairs.load_pairs())
        rows = self._pairs_cache[1]
        if self.cfg.caps.sports_enabled:
            return rows
        return [p for p in rows if p.get("category") != "sports"]

    def record_pairs(self) -> int:
        """Sample both sides of every recorded pair, then run leadlag on the fresh paths."""
        rows = self.leadlag_pairs()
        if not rows or not self.us.available:
            return 0
        got = self.pair_rec.record(rows)
        n = self.scan_other(modules=["leadlag"])
        if time.time() - getattr(self, "_pairs_logged", 0.0) >= 3600:
            self._pairs_logged = time.time()
            self.log(f"  pairs: recording {got['events']} US events every {pairs.RECORD_INTERVAL_S:.0f}s — "
                     f"{got['us']} US and {got['offshore']} offshore quotes this tick")
        return n

    def record_maker(self) -> int:
        """One maker_rewards tick: read the quoted and scouted books, book fills as paper signals,
        accrue the incentive share, re-quote. ~8 book calls every 5 minutes."""
        strat = self.other_modules["maker_rewards"]
        got = strat.tick(log=self.log)
        n = 0
        for sig in got["signals"]:
            if self.handle(sig) in ("paper", "signal", "live"):
                n += 1
        if got["fills"] or time.time() - getattr(self, "_maker_logged", 0.0) >= 3600:
            self._maker_logged = time.time()
            self.log(f"  maker_rewards: {got['quoted']} quoted, {got['scouted']} scouted, {got['fills']} fill(s), "
                     f"est ${got['reward_usd']:.2f} this tick · ${self.ledger.maker_rewards_usd(time.time() - 86400):.2f} "
                     f"in 24 h (estimate; the real number comes from /v1/incentives/earnings once live)")
        return n

    def _quiet_log(self, key, lines, every_s: float = QUIET_REPEAT_S) -> bool:
        """Log `lines` unless this key logged exactly the same lines within `every_s`.

        The arb sweep runs every 20 s and re-reads a persisting candidate each time, so one book
        on 2026-09-23 wrote the same two lines every ~33 s for six minutes (09:41-09:44 miami,
        "book says sell_all 2.8c/set ... thinnest leg 42"). The first telling says everything; a
        repeat says only that nothing changed, and it now says so once, with a count, when the
        story does change."""
        now = time.time()
        text = "\n".join(lines)
        st = self._quiet.get(key)
        if st and st["text"] == text and now - st["ts"] < every_s:
            st["n"] += 1
            st["last"] = now
            return False
        if st and st["n"]:
            self.log(f"    (the previous {key[0] if isinstance(key, tuple) else key} lines repeated {st['n']}x "
                     f"more, until {datetime.fromtimestamp(st['last'], ET).strftime('%H:%M:%S')})")
        self._quiet[key] = {"text": text, "ts": now, "n": 0, "last": now}
        for line in lines:
            self.log(line)
        return True

    def _due(self, name: str, now, hours, quiet_hours=(), weekday=None) -> bool:
        """Has the most recent scheduled slot for `name` passed without a run?

        Every daily job used to fire on one exact minute, and this loop is on a laptop: asleep at
        03:00 and 05:00 (calibration last built 2026-09-12; pairs.json never built at all), and a
        20 s tick behind a 75 s arb pass can step over any given minute. A slot that was missed now
        runs at the next chance instead — outside `quiet_hours`, which keeps the long jobs out of
        the arb window. Runs are remembered on disk so a watchdog restart does not repeat them."""
        if now.hour in quiet_hours:
            return False
        # A job that failed stays due; without this it would be retried every minute, and the pairs
        # build alone is ~20 calls on the US quota the arb sweep needs.
        if time.time() - self._attempts.get(name, 0.0) < JOB_RETRY_S:
            return False
        return self._jobs.get(name, 0.0) < _last_slot(now, hours, weekday).timestamp()

    def universe_catchup_active(self) -> bool:
        """The universe jobs catch up only when the switch was thrown AT a gate reset.

        Re-discovering the catalogue and proving new series changes what bucket_sum trades, and
        bucket_sum is 15/30 decisions into its evidence. So the switch is not a bool: it is the
        gate_since_ts it was enabled with, and it is live only while that is still THE gate reset.
        Turning it on means setting both fields to the same new timestamp — one edit, one reset."""
        armed = float(getattr(self.cfg, "universe_catchup_gate_ts", 0.0) or 0.0)
        return armed > 0 and abs(armed - float(self.cfg.gate_since_ts or 0.0)) < 1e-6

    def _attempt(self, name: str) -> None:
        self._attempts[name] = time.time()

    def _ran(self, name: str) -> None:
        self._jobs[name] = time.time()
        _save_jobs(self._jobs)

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
                 f"{'built ' + calibration.load_table().get('_built', '')[:10] if calibration.load_table() else 'not built'}"
                 f"{' (' + str(calibration.load_table().get('_n')) + ' samples)' if calibration.load_table().get('_n') else ''}",
                 f"  leadlag pairs: {len(self.leadlag_pairs())}"
                 + (f" (built {pairs.pairs_age_s() / 3600:.0f}h ago)" if pairs.pairs_age_s() is not None else " — pairs.json missing")]
        return "\n".join(lines)

    # ---- the loop --------------------------------------------------------------------------
    def _arb_interval_s(self, now) -> float:
        """Seconds between US arb sweeps at this hour.

        The "13:00-14:00 peak" this used to chase was an artefact of when the bot happened to
        scan. Normalised by observed event-minutes, the rate of a positive net after fees is flat
        across the liquid day -- 2.7% over 12:00-15:00 against 2.8% over 09:00-13:00, on 1,450
        observations. There is no peak hour to concentrate on, so concentrating on one only buys
        dense coverage of four hours and thin coverage of the other four.

        What the same data does say clearly is where NOT to look: 18:00-23:00 is 0 opportunities
        in 281 observed event-minutes. So sweep the whole liquid day at the fast rate and let the
        evening go.

        20s is not the venue's limit, it is the loop's: a batched pass costs ~3.4 calls, so three
        sweeps a minute spend ~10 against a budget near 25, and the loop sleeps 20s anyway.
        """
        if 9 <= now.hour <= 16:
            return 20.0
        if now.hour == 17 or now.hour <= 8:
            return 120.0
        return 600.0

    @contextmanager
    def _long_job(self, name: str, grace_s: float = 1800.0):
        """Hold the watchdog off a job that legitimately takes minutes.

        The backtest, the nightly calibration rebuild, the pairs build and the universe sweeps all
        run for far longer than a scan pass. Without this the watchdog would read them as a stall
        and kill the process in the middle — turning a safety net into a way of never finishing
        the weekly backtest.
        """
        self._heartbeat = time.time() + grace_s
        _arm_hard_watchdog(grace_s + WATCHDOG_HARD_S)
        try:
            yield
        finally:
            self._heartbeat = time.time()
            _arm_hard_watchdog(WATCHDOG_HARD_S)

    def _start_watchdog(self, limit_s: float = 240.0) -> None:
        """Kill the process if the loop stops making progress, so launchd can restart it.

        A hung socket blinded this bot for THIRTY-FOUR MINUTES on 2026-09-19, in the middle of
        the liquid window, and said nothing at all:

            15:34:31  arb screen us nyc ...          <- last line
            16:08:25  us scan over its 75s budget    <- 34 minutes later

        Nothing upstream could catch it. `arb_pass_budget_s` is only tested BETWEEN city-days, so
        a call that never returns is never measured; the SDK's own 10s timeout did not fire; and
        `_beat` is called from this same thread, so a blocked loop stops reporting its own
        liveness and the server would not call it stale for three hours.

        So the check has to live somewhere the stall cannot reach. A daemon thread watches a
        timestamp the loop updates every iteration — the loop sleeps 20s and a pass is capped at
        75s, so five minutes of no progress is unambiguous — and exits hard. launchd's KeepAlive
        brings it straight back. Losing a cold cache costs one pass; losing half an hour of the
        best trading window costs the day.
        """
        import threading

        _arm_hard_watchdog(WATCHDOG_HARD_S)

        def watch():
            while True:
                time.sleep(30)
                age = time.time() - self._heartbeat
                if age > limit_s:
                    self.log(f"WATCHDOG: no loop progress for {age:.0f}s — exiting for a restart")
                    try:
                        sys.stdout.flush()
                    except Exception:
                        pass
                    os._exit(1)

        threading.Thread(target=watch, daemon=True, name="polybot-watchdog").start()

    def loop(self):
        self.log("polybot loop started (Ctrl+C to stop)")
        done = set()
        next_arb = 0.0        # sweep immediately on start, then on its own seconds clock
        next_pairs = 0.0
        next_maker = 0.0
        self._heartbeat = time.time()
        # Start the catch-up clock at boot, not at zero: a restart should not fire a 25-call
        # settle burst into the same cold cache that is already re-pricing every leg.
        self._last_us_settle = time.time()
        self._start_watchdog()
        while True:
            self._heartbeat = time.time()
            _arm_hard_watchdog(WATCHDOG_HARD_S)
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
                    # Arbs are brief: 12 of the 16 buy-side episodes on record were seen in a
                    # single observed minute, and the observation cadence WAS five minutes — so a
                    # five-minute scan samples a fraction of them. They also cluster hard: every
                    # fully-quoted sub-$1 book so far landed between 11:00 and 17:00 ET, peaking at
                    # 13:00-14:00. So scan every two minutes across that window and every five
                    # outside it. A pass is ~12 calls against a budget of 25 a minute, which leaves
                    # room for the six-call depth read a candidate triggers.
                    # Cadence follows where the arbs are. Every fully-quoted sub-$1 book on record
                    # landed between 11:00 and 17:00 ET, peaking at 13:00-14:00, and episodes last
                    # about a minute — so the peak is scanned every minute, the shoulders every
                    # two, and the rest of the day every five. Measured cost: ~7.4 calls a pass
                    # (5 events + ~2.4 leg-pricings) against a budget near 17 a minute, so even
                    # the minute cadence runs at well under half, leaving room for the six-call
                    # depth read a candidate triggers.
                    # The arb sweep itself is no longer minute-gated — see _arb_interval_s and
                    # the sweep below. Only the central-bank universe stays on a slow cadence:
                    # those settle weeks out and the dollar-day filter refuses them anyway, so
                    # they are worth a look for the record, not worth a place in the hot path.
                    if now.minute % 10 == 0 and self.us.available and self.cfg.mode("bucket_sum") != "off":
                        with self._long_job("scan_universe", grace_s=300):
                            self.scan_universe()
                    if now.minute % 5 == 0:
                        # leadlag and maker_rewards run on their own clocks below.
                        if self.us.available:
                            with self._long_job("sync", grace_s=300):
                                self.executor.sync()
                    if now.minute == 20:
                        with self._long_job("settle", grace_s=600):
                            self.settle()
                    if now.weekday() == 6 and now.hour == 4 and now.minute == 0:
                        with self._long_job("backtest"):
                            self.backtest(7)
                    if (self.us.available and self.cfg.mode("leadlag") != "off"
                            and self._due("build_pairs", now, (5,), quiet_hours=ARB_HOURS)):
                        self._attempt("build_pairs")
                        with self._long_job("build_pairs"):
                            self.log(self.build_pairs())
                        self._ran("build_pairs")
                    if self.cfg.mode("hold_favorites") != "off" and self._due("hold_favorites", now, (9, 21)):
                        self._attempt("hold_favorites")
                        with self._long_job("hold_favorites", grace_s=600):
                            n = self.scan_other(modules=["hold_favorites"])
                        self._ran("hold_favorites")
                        self.log(f"  hold_favorites: {n} signal(s)")
                    if now.hour == 7 and now.minute == 0:
                        self.log(self.report(1))
                        promoted = self.promote() if self.cfg.auto_promote else []
                        line = self.ledger.summary(1)
                        if not promoted:
                            ready = [m for m in config.MODULES if self.cfg.mode(m) == "paper" and self.ledger.promotion_check(m)[0]]
                            if ready:
                                line += f" — say the word to go live: {', '.join(ready)}"
                        notify.nudge("polybot daily", line, key="polybot-daily", log=self.log)
                    # Re-discover the catalogue twice a day (~20 search calls) and promote any
                    # series a settled instance has now proved. The registry compounds: every
                    # proof is permanent and every future instance of that series is tradable.
                    #
                    # Both of these change which markets bucket_sum may trade, so their catch-up is
                    # behind `universe_catchup_active()`: off, they keep their exact minutes (which
                    # the laptop misses — the refresh last ran 2026-09-19 18:32); on, a missed slot
                    # runs at the next tick outside the arb window. See config.universe_catchup_gate_ts.
                    catchup = self.universe_catchup_active()
                    if (self._due("refresh_universe", now, UNIVERSE_SLOTS, quiet_hours=ARB_HOURS) if catchup
                            else now.hour in (6, 18) and now.minute == 30):
                        self._attempt("refresh_universe")
                        with self._long_job("refresh_universe"):
                            self.log(self.refresh_universe())
                        self._ran("refresh_universe")
                    # Weekly: try to prove recurring series from their own past instances instead
                    # of waiting for the next one to settle. banxico meets every six weeks;
                    # usfed-fomc eight times a year. Both became tradable this way on 2026-09-18.
                    if (self._due("date_sweep", now, ((5, 30),), quiet_hours=ARB_HOURS, weekday=6) if catchup
                            else now.weekday() == 6 and now.hour == 5 and now.minute == 30):
                        self._attempt("date_sweep")
                        with self._long_job("date_sweep"):
                            n = self.prove_by_date_sweep()
                        self._ran("date_sweep")
                        self.log(f"universe: date sweep proved {n} series")
                    if self._due("calibration", now, (3,), quiet_hours=ARB_HOURS):
                        self._attempt("calibration")
                        with self._long_job("calibration"):
                            calibration.save_table(calibration.build(log=self.log))
                        self._ran("calibration")
                    if now.hour == 3 and now.minute == 0:
                        self.log(f"pruned {self.ledger.prune_snapshots(self.cfg.snapshot_keep_days)} snapshots older than {self.cfg.snapshot_keep_days}d")
                except Exception as exc:
                    self.log(f"loop error: {exc}\n{traceback.format_exc(limit=3)}")
                if len(done) > 5000:
                    done = set(sorted(done)[-100:])
            # ---- the arb sweep runs on seconds, not minutes -----------------------------------
            # An episode lasts about a minute, so a once-a-minute sweep samples each one roughly
            # once and misses anything shorter outright. It was minute-gated because a pass cost
            # ~7.4 calls against a shared quota; batching the event lookups cut that to ~3.4, and
            # at three sweeps a minute that is ~10 calls against a budget near 25 — so the limit
            # is now the loop's own granularity, not the venue's. If the campus IP does start
            # refusing, the token bucket widens its own window and these sweeps simply space
            # themselves out: the cadence degrades instead of the bot going blind.
            if (self.us.available and time.time() >= next_arb
                    and self.cfg.mode("bucket_sum") != "off"):
                next_arb = time.time() + self._arb_interval_s(now)
                try:
                    # TODAY only. Screening tomorrow as well was justified by a guess — "a thinner,
                    # worse-quoted book is where a set under $1 is more likely" — and the guess was
                    # wrong. Measured over every snapshot on record:
                    #
                    #     today      2920 complete-book minutes,  58 with a positive net  (2.0%)
                    #     tomorrow   1196 complete-book minutes,   0                      (0.0%)
                    #
                    # Nought for 1,196. Mispricings come from active trading, not from the absence
                    # of it: a thin book just sits at 1.05-1.10 and never crosses. Screening it
                    # doubled the work per sweep, caused the over-budget skips, and never once paid.
                    self.scan_weather(modules=["bucket_sum"], venue="us", day_offsets=(0,))
                except Exception as exc:
                    self.log(f"  arb sweep error: {exc}\n{traceback.format_exc(limit=2)}")
            if (self.us.available and self.cfg.mode("leadlag") != "off"
                    and time.time() >= next_pairs):
                next_pairs = time.time() + pairs.RECORD_INTERVAL_S
                try:
                    self.record_pairs()
                except Exception as exc:
                    self.log(f"  pairs record error: {exc}\n{traceback.format_exc(limit=2)}")
            if (self.us.available and self.cfg.mode("maker_rewards") != "off"
                    and time.time() >= next_maker):
                next_maker = time.time() + maker_mod.INTERVAL_S
                try:
                    self.record_maker()
                except Exception as exc:
                    self.log(f"  maker_rewards error: {exc}\n{traceback.format_exc(limit=2)}")
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
    ap.add_argument("cmd", choices=["scan", "settle", "report", "calibrate", "status", "loop", "backtest",
                                   "pairs", "promote", "arbs", "universe", "leadlag"])
    ap.add_argument("--city", action="append")
    ap.add_argument("--modules", nargs="*")
    ap.add_argument("--venue", default="offshore", choices=["offshore", "us"], help="scan: which books to read")
    ap.add_argument("--days", type=int, default=1)
    ap.add_argument("--events", type=int, default=3000)
    ap.add_argument("--minutes", type=float, default=10, help="leadlag: how long to record")
    ap.add_argument("--kinds", nargs="*", default=["high"])
    a = ap.parse_args(argv)
    r = Runner(log=_stamped_log if a.cmd == "loop" else print)
    if a.cmd == "universe":
        print(r.refresh_universe())
        print(r.uni.report())
        return 0
    if a.cmd == "arbs":
        print(r.ledger.arb_report(a.days if a.days > 1 else 7))
        return 0
    if a.cmd == "backtest":
        print(r.backtest(a.days if a.days > 1 else 7, a.city, tuple(a.kinds)))
    elif a.cmd == "pairs":
        print(r.build_pairs())
    elif a.cmd == "leadlag":
        # Record and scan on the loop's cadence for a while: proof the pairs produce paper signals
        # without waiting for the loop, and without touching anything else it schedules.
        end = time.time() + a.minutes * 60
        total = 0
        while time.time() < end:
            t0 = time.time()
            try:
                total += r.record_pairs()
            except Exception as exc:
                print(f"  pairs record error: {exc}")
            time.sleep(max(0.0, pairs.RECORD_INTERVAL_S - (time.time() - t0)))
        print(f"leadlag: {total} signal(s) in {a.minutes:.0f} min")
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
