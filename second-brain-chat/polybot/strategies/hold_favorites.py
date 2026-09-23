"""hold_favorites (H2 + S12): the calibration table says contracts in a price band resolve YES more
(or less) often than the price implies.

  favorites   a YES we would POST at 85-95c, resolving within `horizon_days` → BUY_YES when
              realized − post ≥ edge
  deadline NO 'by <date>' style longshots BID at 3-20c → BUY_NO when realized ≪ bid

The band and the calibration lookup read the price the order actually trades at, not the market's
last mark. Reading the mark let a wide book through with a fake edge: mark 0.86, bid 0.70, post 0.71,
looked up in the 85-90c band and "worth" 15-20c — 30 of the first 102 live favourites (2026-09-23
15:56) were posted under 85c that way. Bands, edge, min_n and shrink are unchanged.

Category comes from the event's tags; sports stays off until the Ohio gate is flipped.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from .. import calibration, config
from ..feeds import offshore
from .base import Signal, Strategy, size_for

_DEADLINE_RE = re.compile(r"\b(by|before|until)\b", re.I)
# The daily temperature events belong to weather_lock / weather_obs. One position per market holds in
# paper too, so a favourite taken here is a lock or obs signal refused there — the way weather_hold
# starved weather_lock of 95 signals on 2026-09-13 — and those two are accumulating gate evidence.
WEATHER_MODULE_SLUGS = ("highest-temperature-in-", "lowest-temperature-in-")


def _hours_left(end_iso: str) -> float | None:
    try:
        end = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    return (end - datetime.now(timezone.utc)).total_seconds() / 3600


class HoldFavorites(Strategy):
    name = "hold_favorites"

    def __init__(self, cfg: config.Config, table: dict | None = None):
        self.cfg = cfg
        self._injected = table is not None
        self.table = table if table is not None else calibration.load_table()

    def scan(self, ctx=None, events: list | None = None) -> list:
        out = []
        if not self._injected:
            self.table = calibration.load_table()   # the loop rebuilds it nightly
        if not self.table:
            return out
        if events is None:
            # The whole horizon, not its first page. The calibration never samples the Up-or-Down
            # coin flips, so a favourite there would be priced off a table that knows nothing about
            # them; sports are skipped below anyway, so don't page through them either.
            skip = (offshore.TAG_UP_OR_DOWN,) + (() if self.cfg.caps.sports_enabled else (offshore.TAG_SPORTS,))
            events = offshore.events_ending_within(self.cfg.horizon_days, exclude_tag_ids=skip)
        lo_f, hi_f = self.cfg.favorites_band
        lo_l, hi_l = self.cfg.longshot_band
        for e in events:
            if (e.get("slug") or "").startswith(WEATHER_MODULE_SLUGS):
                continue
            cat = offshore.event_category(e)
            if cat == "sports" and not self.cfg.caps.sports_enabled:
                continue
            for m in e.get("markets", []):
                if m.get("closed"):
                    continue
                try:
                    toks = json.loads(m.get("clobTokenIds") or "[]")
                except (ValueError, TypeError):
                    continue
                if not toks:
                    continue
                bid, ask = offshore._f(m.get("bestBid")), offshore._f(m.get("bestAsk"))
                hours = _hours_left(m.get("endDate") or e.get("endDate") or "")
                if hours is None or hours <= 0 or hours > self.cfg.horizon_days * 24:
                    continue
                label = f"{cat}: {m.get('question') or e.get('title')}"
                spread = None if (bid is None or ask is None) else round((ask - bid) * 100, 1)
                post = round(min(bid + 0.01, ask - 0.01), 2) if bid is not None and ask is not None else None
                if post is not None and lo_f <= post <= hi_f:
                    realized = calibration.lookup(self.table, post, cat)
                    if realized is None:
                        continue
                    edge = (realized - post) * 100
                    if edge >= self.cfg.edge_min_cents / 2:
                        size = size_for(realized, post, self.cfg.bankroll_usd, self.cfg.caps)
                        if size > 0:
                            out.append(Signal(self.name, "offshore", toks[0], label, "BUY_YES", post, size, round(edge, 1),
                                              f"calibration {realized:.0%} vs post {post:.2f} ({hours:.0f}h left)",
                                              exit="settle", horizon_hours=hours, category=cat, spread_cents=spread,
                                              meta={"market_id": str(m.get("id")), "band": "favorite"}))
                elif bid is not None and lo_l <= bid <= hi_l and _DEADLINE_RE.search(m.get("question") or ""):
                    realized = calibration.lookup(self.table, bid, cat)
                    if realized is None:
                        continue
                    no_price = round(1 - bid + 0.01, 2)
                    edge = (bid - realized) * 100
                    if edge >= self.cfg.edge_min_cents / 2:
                        size = size_for(1 - realized, no_price, self.cfg.bankroll_usd, self.cfg.caps)
                        if size > 0:
                            out.append(Signal(self.name, "offshore", toks[0], label, "BUY_NO", no_price, size, round(edge, 1),
                                              f"deadline longshot: calibration {realized:.0%} vs bid {bid:.2f} ({hours:.0f}h left)",
                                              exit="settle", horizon_hours=hours, category=cat, spread_cents=spread,
                                              meta={"market_id": str(m.get("id")), "band": "longshot"}))
        return out
