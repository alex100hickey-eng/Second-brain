"""bucket_sum (A1): mutually-exclusive buckets must sum to $1.

If every bucket's best ASK sums to under $1 minus fees, buying one of each locks a $1 payout.
If every bucket's best BID sums to over $1 plus fees, selling one of each (buying NO on each)
locks the difference. These are the only signals in the bot allowed to TAKE liquidity.
"""
from __future__ import annotations

from .. import config, fees
from .base import Signal, Strategy


def arb_check(buckets, venue: str, category: str = "weather"):
    """Return (kind, net_cents_per_set, prices) with kind in {'buy_all', 'sell_all', None}.
    Every bucket needs a two-sided quote; a missing side means no arb can be locked."""
    asks = [b.best_ask for b in buckets]
    bids = [b.best_bid for b in buckets]
    if any(b.closed for b in buckets) or any(a is None for a in asks) or any(x is None for x in bids):
        return None, 0.0, []
    ask_sum = sum(asks)
    buy_fees = sum(fees.leg_cost(a, 1, venue, maker=False, category=category) for a in asks)
    net_buy = (1.0 - ask_sum - buy_fees) * 100
    bid_sum = sum(bids)
    sell_fees = sum(fees.leg_cost(x, 1, venue, maker=False, category=category) for x in bids)
    net_sell = (bid_sum - 1.0 - sell_fees) * 100
    if net_buy > 0 and net_buy >= net_sell:
        return "buy_all", round(net_buy, 2), asks
    if net_sell > 0:
        return "sell_all", round(net_sell, 2), bids
    return None, round(max(net_buy, net_sell), 2), []


class BucketSum(Strategy):
    name = "bucket_sum"

    def __init__(self, cfg: config.Config):
        self.cfg = cfg

    def scan(self, ctx) -> list:
        ev = ctx.event
        if not getattr(ev, "neg_risk", True):
            return []
        kind, net, prices = arb_check(ev.buckets, ctx.venue)
        if kind is None or net < self.cfg.bucket_sum_min_net_cents:
            return []
        n = len(ev.buckets)
        per_leg = max(self.cfg.caps.min_order_usd, self.cfg.caps.max_per_market_usd / n)
        group = f"{ev.slug}:{kind}:{int(net * 10)}"
        out = []
        for b, px in zip(ev.buckets, prices):
            side = "BUY_YES" if kind == "buy_all" else "BUY_NO"
            price = px if kind == "buy_all" else round(1 - px, 2)
            out.append(Signal(self.name, ctx.venue, b.yes_token, f"{ctx.city} {ctx.date} {ctx.kind} {b.title}", side,
                              price, per_leg, net, f"{kind}: set nets {net:.1f}c after taker fees",
                              exit="settle", horizon_hours=30, taker=True, arb=True,
                              spread_cents=None if b.best_bid is None else round((b.best_ask - b.best_bid) * 100, 1),
                              meta={"market_id": b.market_id, "group": group, "legs": n}))
        return out
