"""leadlag (S2 / S3 / S4): the US book lags the offshore reference.

  S2 lead-lag  reference moved ≥ move_cents in the window, target followed < follow_ratio of it
               → trade the target toward the reference, exit when it catches up.
  S3 noise     target moved ≥ move_cents, reference did not → fade the target back to the reference.
  S4 in-play   the same rule on live sports pairs (sports gate applies; Ohio).

The pure functions work on two price series so they can be tested and back-run on recordings.
The Strategy needs the US venue (the key), `pairs.json` (built by `pairs.match_events`) and a series
store that is sampled faster than the 120 s window — `pairs.PairRecorder`, which the runner feeds
every minute. Without the key it reports idle and returns nothing.
"""
from __future__ import annotations

from .. import config, pairs as pairs_mod
from .base import Signal, Strategy

PAIRS_PATH = pairs_mod.PAIRS_PATH


def _move(series, window_s: float):
    """(delta, first, last) of a [(ts, price)] series over the trailing window."""
    if len(series) < 2:
        return 0.0, None, None
    t_end = series[-1][0]
    start = next((p for t, p in series if t >= t_end - window_s), series[0][1])
    return series[-1][1] - start, start, series[-1][1]


def leadlag_signal(ref, tgt, window_s: float, move_cents: float, follow_ratio: float):
    """Return ('BUY_YES'|'BUY_NO', ref_now, tgt_now, gap_cents) or None."""
    d_ref, _, ref_now = _move(ref, window_s)
    d_tgt, _, tgt_now = _move(tgt, window_s)
    if ref_now is None or tgt_now is None:
        return None
    if abs(d_ref) * 100 < move_cents:
        return None
    if abs(d_tgt) >= follow_ratio * abs(d_ref) and (d_tgt * d_ref) > 0:
        return None  # already followed
    gap = (ref_now - tgt_now) * 100
    if abs(gap) < move_cents / 2:
        return None
    return ("BUY_YES" if gap > 0 else "BUY_NO", ref_now, tgt_now, round(gap, 1))


def noise_signal(ref, tgt, window_s: float, move_cents: float, quiet_ratio: float = 0.34):
    """Target moved, reference did not: fade the target back. Same return shape as leadlag_signal."""
    d_ref, _, ref_now = _move(ref, window_s)
    d_tgt, _, tgt_now = _move(tgt, window_s)
    if ref_now is None or tgt_now is None:
        return None
    if abs(d_tgt) * 100 < move_cents or abs(d_ref) > quiet_ratio * abs(d_tgt):
        return None
    gap = (ref_now - tgt_now) * 100
    if abs(gap) < move_cents / 2:
        return None
    return ("BUY_YES" if gap > 0 else "BUY_NO", ref_now, tgt_now, round(gap, 1))


def load_pairs(path: str | None = None) -> list:
    """[{'us_slug':..., 'offshore_token':..., 'category': 'politics'|'sports'|..., 'label':...}]"""
    return pairs_mod.load_pairs(path)


class LeadLag(Strategy):
    name = "leadlag"

    def __init__(self, cfg: config.Config, us_venue, series_store, quote_fn=None, pairs_fn=None):
        """series_store: object with .get(venue, market) -> [(ts, price)] (recent samples).
        quote_fn(us_slug) -> (bid, ask): a fresh quote already in hand; falls back to a book call."""
        self.cfg = cfg
        self.us = us_venue
        self.store = series_store
        self.quote_fn = quote_fn
        self.pairs_fn = pairs_fn or load_pairs

    @property
    def idle_reason(self):
        # Asked at scan time, not frozen at startup: a rate-limit backoff in the first second of a
        # restart used to leave leadlag reporting "idle" until the next restart.
        return None if self.us.available else self.us.why_unavailable

    def scan(self, ctx=None) -> list:
        out = []
        if not self.us.available:
            return out
        for pair in self.pairs_fn():
            cat = pair.get("category", "other")
            if cat == "sports" and not self.cfg.caps.sports_enabled:
                continue
            ref = self.store.get("offshore", pair["offshore_token"])
            tgt = self.store.get("us", pair["us_slug"])
            hit = leadlag_signal(ref, tgt, self.cfg.leadlag_window_s, self.cfg.leadlag_move_cents,
                                 self.cfg.leadlag_follow_ratio)
            kind = "leadlag"
            if hit is None:
                hit = noise_signal(ref, tgt, self.cfg.leadlag_window_s, self.cfg.leadlag_move_cents)
                kind = "noise"
            if hit is None:
                continue
            side, ref_now, tgt_now, gap = hit
            bid, ask = self.quote_fn(pair["us_slug"]) if self.quote_fn else (None, None)
            if bid is None or ask is None:
                bid, ask = self.us.bbo(pair["us_slug"])
            if bid is None or ask is None:
                continue
            spread = round((ask - bid) * 100, 1)
            if abs(gap) <= spread + 1.0:
                continue  # the gap is inside the spread: nothing to collect as a maker
            price = round(bid + 0.01, 2) if side == "BUY_YES" else round(1 - ask + 0.01, 2)
            out.append(Signal(self.name, "us", pair["us_slug"], pair.get("label", pair["us_slug"]), side, price,
                              self.cfg.caps.max_per_market_usd / 2, abs(gap), f"{kind}: ref {ref_now:.2f} vs us {tgt_now:.2f}",
                              exit="reference", horizon_hours=6, category=cat, spread_cents=spread,
                              meta={"kind": kind, "ref": ref_now, "tgt": tgt_now}))
        return out
