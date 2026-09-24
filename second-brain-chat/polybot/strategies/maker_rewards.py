"""maker_rewards (M1): rest two-sided quotes on Polymarket US liquidity-incentive markets, in paper.

The income is the incentive pool, not the spread: every second the exchange scores resting orders
by discount ** (ticks from best) x size and splits each market's daily pool pro rata (see
`polybot.incentives`). What it costs is the fills — a resting quote is picked off exactly when the
market moves through it — so paper has to measure both, and the gate reads both.

The paper loop, every `INTERVAL_S` (runner.record_maker):
  1. Read the book of each quoted market (<= MAX_MARKETS) plus a few scouted candidates (2 a tick in
     the arb window, 6 outside it), walking the programs by pool. The richest pools are not where a
     $20 quote earns most: on 2026-09-24 the $1,125/day Senate books paid ~$0.78/day for $20 because
     44,552 contracts already sat at the best bid, while a $450 House book paid ~$47 with 190 ahead.
  2. For each quote, decide whether the book traded THROUGH it since the last look (the only fill
     paper can claim: a best ask at or under our bid, or our level gone with a print at or under it).
     A fill becomes a paper signal (filled at the signal, 24 h exit), so its adverse selection lands in
     the same ledger the gate reads.
  3. Accrue the reward for the interval at the share the official formula gives our order on the
     book just read (a side short of target size earns nothing), into `maker_accrual`.
  4. Re-join the best price on both sides with SIDE_USD of collateral per side; a side that filled
     sits out until that position has exited.
  5. Swap a quoted market for a scouted one that pays at least SWAP_MARGIN more per dollar.
Quotes join the best price, never improve it: improving earns more score but also volunteers for
every fill, and the first version should err toward the side that under-claims.
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from .. import config, incentives
from .base import Signal, Strategy

INTERVAL_S = 300.0
MAX_MARKETS = 6
SCOUT_PER_TICK = 2       # inside the arb window (09:00-16:59 ET), where the call budget is the arb's
SCOUT_PER_TICK_QUIET = 6 # outside it: ~1,000 programs get a look about once a day
SIDE_USD = 10.0          # per side; two sides = the $20 per-market cap
SWAP_MARGIN = 1.3
PROGRAMS_TTL_S = 6 * 3600
EXIT = "timeout:24h"


class MakerRewards(Strategy):
    name = "maker_rewards"

    def __init__(self, cfg: config.Config, us_venue, ledger=None, clock=time.time, programs_fn=None):
        self.cfg, self.us, self.ledger, self.clock = cfg, us_venue, ledger, clock
        self.programs_fn = programs_fn or (lambda: incentives.fetch_programs(
            self.us, incentives.NON_SPORT_CATEGORIES + ((incentives.SPORTS_CATEGORY,)
                                                        if self.cfg.caps.sports_enabled else ())))
        self._programs, self._programs_ts = {}, 0.0
        self._scout_i = 0
        self._scouted: dict = {}           # market -> (usd_per_day_per_usd, ts)

    @property
    def idle_reason(self):
        if self.ledger is None:
            return "no ledger"
        return None if self.us.available else self.us.why_unavailable

    def scan(self, ctx=None) -> list:
        return []            # stateful; runs from runner.record_maker, not the signal scan

    # ---- programs --------------------------------------------------------------------------
    def programs(self) -> dict:
        now = self.clock()
        if not self._programs or now - self._programs_ts > PROGRAMS_TTL_S:
            rows = self.programs_fn() or []
            progs = {}
            for p in rows:
                per = incentives.active_period(p, now)
                if per and p.get("marketSlug"):
                    progs[p["marketSlug"]] = {"period": per, "category": (p.get("category") or "").lower()}
            if progs:
                self._programs, self._programs_ts = progs, now
        return self._programs

    def ranked(self) -> list:
        return sorted(self.programs(), key=lambda s: -float(self._programs[s]["period"].get("rewardPool") or 0))

    # ---- one tick --------------------------------------------------------------------------
    def tick(self, log=lambda *a: None) -> dict:
        """Returns counts; the signals it produced are in out['signals'] for the runner to record."""
        now = self.clock()
        progs = self.programs()
        out = {"quoted": 0, "scouted": 0, "fills": 0, "reward_usd": 0.0, "signals": []}
        if not progs:
            return out
        quotes = self.ledger.maker_quotes()
        quoted = sorted({q["market"] for q in quotes})
        for m in quoted:
            if m not in progs:                               # program over: stop quoting it
                self.ledger.maker_drop(m)
        quoted = [m for m in quoted if m in progs]
        # scouts: the next candidates by pool that we are not already quoting
        ranked = [m for m in self.ranked() if m not in quoted]
        scouts = []
        busy = 9 <= datetime.fromtimestamp(now, ZoneInfo("America/New_York")).hour <= 16
        for _ in range(min(SCOUT_PER_TICK if busy else SCOUT_PER_TICK_QUIET, len(ranked))):
            scouts.append(ranked[self._scout_i % len(ranked)])
            self._scout_i += 1
        for m in quoted + scouts:
            book = self.us.book(m)
            if not book or not book.get("bids") or not book.get("asks"):
                continue
            self.ledger.add_snapshot("us", m, book["bids"][0][0], book["asks"][0][0], book.get("last"), ts=now)
            per = progs[m]["period"]
            if m in quoted:
                out["quoted"] += 1
                got = self._service(m, per, book, now, progs[m]["category"], log)
                out["fills"] += len(got["signals"])
                out["reward_usd"] += got["reward_usd"]
                out["signals"].extend(got["signals"])
            else:
                out["scouted"] += 1
                bb, ba = book["bids"][0][0], book["asks"][0][0]
                r = incentives.quote_rate(book, per, (bb, incentives.size_for(bb, SIDE_USD, "bid")),
                                          (ba, incentives.size_for(ba, SIDE_USD, "ask")))
                self._scouted[m] = (r["usd_per_day"] / (2 * SIDE_USD), now)
        self._rotate(quoted, progs, now, log)
        return out

    def _service(self, m, per, book, now, category, log) -> dict:
        bb, ba = book["bids"][0][0], book["asks"][0][0]
        last = book.get("last")
        res = {"signals": [], "reward_usd": 0.0}
        qs = {q["side"]: q for q in self.ledger.maker_quotes(m)}
        # 1. fills since the last look
        for side, q in qs.items():
            if q["qty"] <= 0:
                continue
            px = q["px"]
            printed = last is not None and q["last_trade"] is not None and abs(last - q["last_trade"]) > 1e-9
            if side == "bid":
                hit = ba <= px + 1e-9 or (bb < px - 1e-9 and printed and last <= px + 1e-9)
            else:
                hit = bb >= px - 1e-9 or (ba > px + 1e-9 and printed and last >= px - 1e-9)
            if hit:
                res["signals"].append(self._fill_signal(m, side, px, q["qty"], category, per))
                self.ledger.maker_set(m, side, px, 0.0, now, last, filled_ts=now)
                qs[side] = dict(q, qty=0.0, filled_ts=now)
        # 2. accrue for the interval at the share our resting orders have on this book
        our_bid = (qs["bid"]["px"], qs["bid"]["qty"]) if "bid" in qs and qs["bid"]["qty"] > 0 else None
        our_ask = (qs["ask"]["px"], qs["ask"]["qty"]) if "ask" in qs and qs["ask"]["qty"] > 0 else None
        r = incentives.quote_rate(book, per, our_bid, our_ask)
        pool = float(per.get("rewardPool") or 0)
        for side, ours in (("bid", our_bid), ("ask", our_ask)):
            if not ours:
                continue
            last_ts = qs[side]["last_ts"] or now
            dt = min(max(0.0, now - last_ts), 2 * INTERVAL_S)       # a blind stretch earns nothing
            usd = r[side] * pool / 2.0 * dt / 86400.0
            self.ledger.maker_accrue(now, m, side, ours[1], r[side], usd, r[f"{side}_ok"], pool,
                                     per.get("programId"))
            res["reward_usd"] += usd
        # 3. re-join the best price; a side that filled waits until that position has exited
        for side, px in (("bid", bb), ("ask", ba)):
            q = qs.get(side)
            # filled this tick (its signal is recorded after the tick returns) or still holding it
            if q and q.get("filled_ts") and (q["filled_ts"] >= now or self.ledger.maker_position_open(m, side)):
                self.ledger.maker_set(m, side, px, 0.0, now, last, filled_ts=q["filled_ts"])
                continue
            self.ledger.maker_set(m, side, px, incentives.size_for(px, SIDE_USD, side), now, last)
        self._scouted[m] = ((r["usd_per_day"]) / (2 * SIDE_USD), now)
        return res

    def _fill_signal(self, m, side, px, qty, category, per) -> Signal:
        """A filled quote is a position: bid filled = long YES at px; ask filled = long NO at 1 - px."""
        if side == "bid":
            sig_side, price = "BUY_YES", round(px, 4)
        else:
            sig_side, price = "BUY_NO", round(1 - px, 4)
        return Signal(self.name, "us", m, f"maker {side} fill: {m}", sig_side, price, round(price * qty, 2), 0.0,
                      f"resting {side} at {px:.3f} traded through (reward program {per.get('programId')})",
                      exit=EXIT, horizon_hours=24, category=category or "other",
                      meta={"filled_at_signal": True, "maker_side": side, "program": per.get("programId")})

    def _rotate(self, quoted, progs, now, log):
        rate = lambda m: self._scouted.get(m, (0.0, 0))[0]
        pool = sorted((m for m in self._scouted if m not in quoted and m in progs), key=rate, reverse=True)
        quoted = list(quoted)
        for cand in pool:
            if rate(cand) <= 0:
                break
            if len(quoted) < MAX_MARKETS:
                quoted.append(cand)
                self.ledger.maker_start(cand, now)
                log(f"  maker_rewards: quoting {cand} (est ${rate(cand) * 2 * SIDE_USD:.2f}/day on ${2 * SIDE_USD:.0f})")
                continue
            worst = min(quoted, key=rate)
            if rate(cand) > SWAP_MARGIN * rate(worst) and not self.ledger.maker_positions_open(worst):
                self.ledger.maker_drop(worst)
                quoted.remove(worst)
                quoted.append(cand)
                self.ledger.maker_start(cand, now)
                log(f"  maker_rewards: {cand} replaces {worst} "
                    f"(est ${rate(cand) * 2 * SIDE_USD:.2f} vs ${rate(worst) * 2 * SIDE_USD:.2f}/day)")
