"""bucket_sum (A1): mutually-exclusive buckets must sum to $1.

This is the only module in the bot whose profit does not require out-forecasting the market. Every
weather module bets that our thermometer reading beats the book's; the paper record says it does
not. This one bets nothing: if six buckets tile every possible temperature, exactly one of them
pays $1 at settlement, so buying one contract of each for less than $1 is the difference, whatever
the weather does.

It had never produced a single signal, for four separate reasons — all fixed here:

1. `neg_risk` was False on every Polymarket US event (a placeholder in the adapter, not a finding),
   and the old gate returned [] on that flag alone. A flag is the wrong thing to trust anyway:
   what makes the arb valid is that the buckets TILE, which we can check ourselves from the parsed
   ranges. `exhaustive()` does, and it works on both venues.
2. It demanded a two-sided quote on every bucket. A buy-all set only needs ASKS. On the US books
   50% of event-minutes have an ask on every bucket but only 7% have both sides everywhere, so the
   bid requirement threw away 7 opportunities in 8.
3. It sized legs in equal DOLLARS (`max_per_market_usd / n`). Equal dollars across legs priced 1c
   and 73c buys 500 contracts of one and 7 of the other — that is not an arb, it is a random
   basket. A set is N contracts of every leg or it is nothing.
4. It sized off the quote with no idea whether the quote could be filled. An ask of 0.01 for 5
   contracts is not 0.01 for 500. Legs now carry `ask_qty` and the set is capped by the thinnest.

The screen is deliberately cheap: `scan` runs off the quotes already in the event (one API call for
the whole event) and only flags a CANDIDATE. Depth costs one book call per bucket, so the runner
spends that only when a candidate appears — which over 8 days of US snapshots was 34 event-minutes
out of 3,353.
"""
from __future__ import annotations

import math

from .. import config, fees
from .base import Signal, Strategy


def exhaustive(buckets) -> bool:
    """Do these buckets tile every integer temperature exactly once?

    That, not a `negRisk` flag, is what makes "they must sum to $1" true. Requires an open-ended
    bucket at each end and no gap or overlap between neighbours. Temperatures settle as whole
    degrees, so contiguous means `next.lo == prev.hi + 1`.
    """
    if len(buckets) < 2:
        return False
    rng = sorted(((b.lo, b.hi) for b in buckets))
    if rng[0][0] != -math.inf or rng[-1][1] != math.inf:
        return False
    for (_, prev_hi), (lo, _) in zip(rng, rng[1:]):
        if prev_hi == math.inf or lo == -math.inf:
            return False          # two open ends on the same side: not a tiling
        if lo != prev_hi + 1:
            return False          # a gap (some temperature pays nobody) or an overlap (pays twice)
    return True


def arb_check(buckets, venue: str, category: str = "weather"):
    """Return (kind, net_cents_per_set, prices) with kind in {'buy_all', 'sell_all', None}.

    A buy-all set needs an ask on every bucket; a sell-all set needs a bid on every bucket. A
    missing quote is never read as zero — that is what turns an empty book into a phantom 100%
    arb (Polymarket US had two cities quoted on neither side on 2026-09-18, and a naive sum called
    them free money). Fees are charged per leg at the taker rate, on the real price of that leg.
    """
    if any(b.closed for b in buckets) or not exhaustive(buckets):
        return None, 0.0, []
    asks = [b.best_ask for b in buckets]
    bids = [b.best_bid for b in buckets]
    net_buy = net_sell = -math.inf
    if all(a is not None for a in asks):
        cost = sum(asks) + sum(fees.leg_cost(a, 1, venue, maker=False, category=category) for a in asks)
        net_buy = (1.0 - cost) * 100
    if all(x is not None for x in bids):
        proceeds = sum(bids) - sum(fees.leg_cost(x, 1, venue, maker=False, category=category) for x in bids)
        net_sell = (proceeds - 1.0) * 100
    if net_buy > 0 and net_buy >= net_sell:
        return "buy_all", round(net_buy, 2), asks
    if net_sell > 0:
        return "sell_all", round(net_sell, 2), bids
    return None, round(max(x for x in (net_buy, net_sell) if x > -math.inf) if
                       max(net_buy, net_sell) > -math.inf else 0.0, 2), []


MIN_TICK = 0.01


def unpriced(buckets) -> list:
    """Buckets the event object gave no ask for. Not the same as "nobody is offering"."""
    return [b for b in buckets if b.best_ask is None]


def arb_possible(buckets, floor: float = MIN_TICK) -> bool:
    """Could a buy-all set POSSIBLY be under $1, if every unquoted leg were as cheap as it can be?

    The event object is what the screen gets for free, and it omits `bestAskQuote` on buckets that
    do have resting offers — nyc's "86 or above" showed ask=None in the event and 0.01 x25,574 in
    the book on 2026-09-18. `arb_check` needs an ask on every leg, so those ticks were never
    evaluated at all: across 9 days it looked at 36 of 1,570 event-minutes that could have held a
    set, i.e. 2%.

    This is the admissible half of the fix — it never rules out a real arb, because no leg can cost
    less than one tick. It is emphatically NOT evidence of an arb: miami the same day had four legs
    quoted at 0.04 total and a favourite with NO ask in the book at any price, which this bound
    would call a 96c opportunity. The caller must price the unquoted legs before believing anything.
    """
    known = [b.best_ask for b in buckets if b.best_ask is not None]
    if not known or any(b.closed for b in buckets) or not exhaustive(buckets):
        return False
    return sum(known) + floor * (len(buckets) - len(known)) < 1.0


def sets_available(buckets, kind: str) -> float | None:
    """How many complete sets the book can actually fill, or None if depth was never looked up.

    One set is one contract of every leg, so the thinnest leg is the whole size. `None` propagates
    rather than defaulting to a number: an unknown depth must block the trade, not permit it.
    """
    qtys = [(b.ask_qty if kind == "buy_all" else b.bid_qty) for b in buckets]
    if any(q is None for q in qtys):
        return None
    return max(0.0, min(qtys))


class BucketSum(Strategy):
    name = "bucket_sum"

    def __init__(self, cfg: config.Config):
        self.cfg = cfg

    def scan(self, ctx) -> list:
        ev = ctx.event
        kind, net, prices = arb_check(ev.buckets, ctx.venue)
        if kind is None or net < self.cfg.bucket_sum_min_net_cents:
            return []
        sets = sets_available(ev.buckets, kind)
        if sets is None:
            # The quotes say there is an arb but nobody has asked the book how deep it is. The
            # runner answers that with one book call per bucket and scans again; until then this
            # is a candidate, not a trade.
            return []
        # Every leg carries the same number of contracts, and the set costs what the set costs.
        # The per-market cap is a cap on ONE leg, so the binding constraint is the dearest leg.
        dearest = max(prices)
        by_cap = self.cfg.caps.max_per_market_usd / dearest if dearest > 0 else sets
        by_total = self.cfg.caps.max_exposure_usd / max(sum(prices), 1e-9)
        contracts = int(min(sets, by_cap, by_total, self.cfg.arb_max_sets))
        if contracts < 1:
            return []
        group = f"{ev.slug}:{kind}:{int(net * 10)}"
        out = []
        for b, px in zip(ev.buckets, prices):
            side = "BUY_YES" if kind == "buy_all" else "BUY_NO"
            price = px if kind == "buy_all" else round(1 - px, 2)
            out.append(Signal(self.name, ctx.venue, b.yes_token, f"{ctx.city} {ctx.date} {ctx.kind} {b.title}", side,
                              price, price * contracts, net,
                              f"{kind}: {contracts} sets net {net:.1f}c each after taker fees "
                              f"({len(ev.buckets)} legs, thinnest {sets:.0f})",
                              # `contracts` is derived from size_usd/price, so the size IS the way to
                              # say "N contracts of this leg". Prices are 2dp and N is an integer, so
                              # price*N is exact at 2dp and the property reads back exactly N.
                              exit="settle", horizon_hours=30, taker=True, arb=True,
                              spread_cents=None if b.best_bid is None or b.best_ask is None
                              else round((b.best_ask - b.best_bid) * 100, 1),
                              meta={"market_id": b.market_id, "group": group, "legs": len(ev.buckets),
                                    "sets": contracts, "net_cents": net}))
        # Belt and braces. `contracts` is derived from size_usd/price, and the arithmetic only
        # round-trips exactly while prices are well behaved — a 4-decimal price rounded to cents
        # silently produced 11 contracts on one leg and 12 on another in test. An unbalanced set
        # is not an arb, it is a naked basket with a story attached, so if any leg disagrees the
        # whole set is dropped rather than half-placed.
        if any(sig.contracts != contracts for sig in out):
            return []
        return out
