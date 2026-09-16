"""Signal + Strategy interface + sizing."""
from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass
class Signal:
    module: str
    venue: str                 # 'offshore' (paper proxy only) | 'us'
    market: str                # offshore: YES token id · us: market slug
    label: str                 # human label, e.g. "NYC 2026-09-12 high 78-79°F"
    side: str                  # 'BUY_YES' | 'BUY_NO'
    price: float               # price we post, in YES terms for BUY_YES, in NO terms for BUY_NO
    size_usd: float
    edge_cents: float
    reason: str
    exit: str = "settle"       # 'settle' | 'tp:0.03' | 'reference' | 'timeout:6h'
    horizon_hours: float = 24.0
    category: str = "weather"
    spread_cents: float | None = None
    taker: bool = False        # True only for arb legs that must cross the spread
    arb: bool = False
    taker_ok: bool = False     # the strategy has a reason to cross that is not an arb (see WeatherLock)
    ts: float | None = None
    meta: dict = field(default_factory=dict)

    @property
    def contracts(self) -> int:
        # floor(size/price) with a float guard: 18 // 0.9 is 19.0 in floating point, not 20
        return int(math.floor(self.size_usd / self.price + 1e-9)) if self.price > 0 else 0

    def dedupe_key(self):
        return (self.module, self.market, self.side)


class Strategy:
    name = "base"
    category = "weather"

    def scan(self, ctx) -> list:
        raise NotImplementedError


def size_for(p_true: float, price: float, bankroll_usd: float, caps, fraction: float | None = None) -> float:
    """Quarter-Kelly for a binary contract bought at `price` with true probability `p_true`,
    capped per market, floored at the min order. Returns dollars (0 if no edge)."""
    if price <= 0 or price >= 1 or p_true <= price:
        return 0.0
    f_star = (p_true - price) / (1.0 - price)
    frac = caps.kelly_fraction if fraction is None else fraction
    usd = min(caps.max_per_market_usd, frac * f_star * bankroll_usd)
    if usd < caps.min_order_usd:
        usd = caps.min_order_usd if f_star * bankroll_usd >= caps.min_order_usd else 0.0
    return math.floor(usd * 100) / 100
