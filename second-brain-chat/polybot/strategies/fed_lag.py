"""fed_lag (S5): Polymarket US's Fed-decision books lag Kalshi's.

The same meeting, the same five outcomes, the same settlement fact (the FOMC statement), on two
venues. Kalshi's KXFEDDECISION books are the deep ones (~$1M traded per outcome for Oct 2026, 1c
spreads), so when a CPI print or a Fed speaker moves them, the US book is the one that may follow
late. The rule is leadlag's own (`leadlag_signal` / `noise_signal`, the same config thresholds, the
same spread check, the same 6 h `reference` exit), with Kalshi's mid as the reference; only the
reference venue is new. Paper only, and the gate is the same gate.

What to expect: on 2026-09-25 at 00:30 the two venues agreed to within a cent on every outcome of
the October and December meetings, so this module is quiet between data releases by design.

Sampling rides the pair recorder's 40 s clock: one Kalshi call a tick (its own API, not the US
venue's budget) and the US Fed events added to the recorder's batched US read. A market another
module already holds is skipped, so bucket_sum's Fed sets are never crowded (the lesson of
weather_hold starving weather_lock). Overnight the US books are HALTED and read `closed: true`, so
the recorder forgets their paths and nothing compares against them. Only the nearest
MAX_MEETINGS meetings are read: Kalshi lists 11 (to 2028), the US venue 2 (2026-09-25).
"""
from __future__ import annotations

import time

from .. import config
from ..feeds import kalshi
from .base import Signal, Strategy
from .leadlag import leadlag_signal, noise_signal

REF_VENUE = "kalshi"
MAX_MEETINGS = 3
EXIT = "reference"
HORIZON_H = 6


class FedLag(Strategy):
    name = "fed_lag"

    def __init__(self, cfg: config.Config, us_venue, recorder, ledger=None, events_fn=None, clock=time.time):
        """recorder: the runner's PairRecorder (in-memory paths + change-only snapshots)."""
        self.cfg, self.us, self.rec, self.ledger = cfg, us_venue, recorder, ledger
        self.events_fn = events_fn or kalshi.fed_events
        self.clock = clock
        self._pairs, self._pairs_ts = [], 0.0

    @property
    def idle_reason(self):
        return None if self.us.available else self.us.why_unavailable

    def pairs(self) -> list:
        return self._pairs

    def us_events(self) -> list:
        return sorted({p["us_event"] for p in self._pairs})

    def record_ref(self) -> int:
        """One Kalshi read: push every Fed outcome's book into the recorder, refresh the pairs."""
        try:
            events = self.events_fn() or []
        except Exception:                 # a DNS drop costs this tick's reference, no more
            return 0
        ts, n, pairs = self.clock(), 0, []
        for e in sorted(events, key=lambda e: e["date"])[:MAX_MEETINGS]:
            for m in e["markets"]:
                pairs.append({"us_event": kalshi.us_event_slug(e["date"]),
                              "us_slug": kalshi.us_market_slug(e["date"], m["outcome"]),
                              "ref": m["ticker"], "label": f"Fed {e['date']}: {kalshi.FED_OUTCOMES[m['outcome']]}"})
                if m["bid"] is not None or m["ask"] is not None:
                    self.rec._push(REF_VENUE, m["ticker"], m["bid"], m["ask"], ts)
                    n += 1
        if pairs:
            self._pairs, self._pairs_ts = pairs, ts
        return n

    def _held_elsewhere(self) -> set:
        if self.ledger is None:
            return set()
        return {r["market"] for r in self.ledger.open_signals(venue="us") if r["module"] != self.name}

    def scan(self, ctx=None) -> list:
        out = []
        if not self.us.available:
            return out
        held = self._held_elsewhere()
        for p in self._pairs:
            if p["us_slug"] in held:
                continue
            ref = self.rec.get(REF_VENUE, p["ref"])
            tgt = self.rec.get("us", p["us_slug"])
            hit, kind = leadlag_signal(ref, tgt, self.cfg.leadlag_window_s, self.cfg.leadlag_move_cents,
                                       self.cfg.leadlag_follow_ratio), "lag"
            if hit is None:
                hit, kind = noise_signal(ref, tgt, self.cfg.leadlag_window_s, self.cfg.leadlag_move_cents), "noise"
            if hit is None:
                continue
            side, ref_now, tgt_now, gap = hit
            bid, ask = self.rec.quote(p["us_slug"])
            if bid is None or ask is None:
                continue
            spread = round((ask - bid) * 100, 1)
            if abs(gap) <= spread + 1.0:
                continue
            price = round(bid + 0.01, 2) if side == "BUY_YES" else round(1 - ask + 0.01, 2)
            out.append(Signal(self.name, "us", p["us_slug"], p["label"], side, price,
                              self.cfg.caps.max_per_market_usd / 2, abs(gap),
                              f"{kind}: kalshi {ref_now:.2f} vs us {tgt_now:.2f}",
                              exit=EXIT, horizon_hours=HORIZON_H, category="economics", spread_cents=spread,
                              meta={"kind": kind, "ref": ref_now, "tgt": tgt_now, "reference": REF_VENUE,
                                    "ref_ticker": p["ref"], "us_event": p["us_event"]}))
        return out
