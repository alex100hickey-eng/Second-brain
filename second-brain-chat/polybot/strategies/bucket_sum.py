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


def arb_check(buckets, venue: str, category: str = "weather", assume_exhaustive: bool = False):
    """Return (kind, net_cents_per_set, prices) with kind in {'buy_all', 'sell_all', None}.

    A buy-all set needs an ask on every bucket; a sell-all set needs a bid on every bucket. A
    missing quote is never read as zero — that is what turns an empty book into a phantom 100%
    arb (Polymarket US had two cities quoted on neither side on 2026-09-18, and a naive sum called
    them free money). Fees are charged per leg at the taker rate, on the real price of that leg.
    """
    # Weather buckets PROVE exhaustiveness by tiling a number line. Everything else on the venue
    # (a Fed decision, an election) has labelled outcomes with no number line to tile, so the proof
    # comes from `universe`: a settled instance of the same series with exactly one winner. That is
    # the only thing `assume_exhaustive` may ever mean — never a guess, never a price heuristic.
    if any(b.closed for b in buckets) or not (assume_exhaustive or exhaustive(buckets)):
        return None, 0.0, []
    asks = [b.best_ask for b in buckets]
    bids = [b.best_bid for b in buckets]
    net_buy = net_sell = -math.inf
    if all(a is not None for a in asks):
        cost = sum(asks) + sum(fees.leg_cost(a, 1, venue, maker=False, category=category,
                                            theta=b.fee_coefficient) for a, b in zip(asks, buckets))
        net_buy = (1.0 - cost) * 100
    if all(x is not None for x in bids):
        proceeds = sum(bids) - sum(fees.leg_cost(x, 1, venue, maker=False, category=category,
                                                 theta=b.fee_coefficient) for x, b in zip(bids, buckets))
        net_sell = (proceeds - 1.0) * 100
    # Pick by RETURN ON CAPITAL, not cents per set. The two directions are not comparable per set:
    # a buy-all set ties up sum(asks) ~= $0.88 to make 12c (13.6%), while a sell-all set on the same
    # six legs ties up N - sum(bids) ~= $4.92 to make 8c (1.6%) — because buying NO on every leg
    # means paying (1 - bid) six times over. With a fixed exposure cap the cheaper set is worth ~8x
    # the dollars, so "bigger net per set" would systematically choose the worse trade.
    roc_buy = (net_buy / 100.0) / max(sum(a for a in asks if a is not None), 1e-9) if net_buy > 0 else -1
    sell_cost = len(buckets) - sum(x for x in bids if x is not None)
    roc_sell = (net_sell / 100.0) / max(sell_cost, 1e-9) if net_sell > 0 else -1
    if net_buy > 0 and roc_buy >= roc_sell:
        return "buy_all", round(net_buy, 2), asks
    if net_sell > 0:
        return "sell_all", round(net_sell, 2), bids
    return None, round(max(x for x in (net_buy, net_sell) if x > -math.inf) if
                       max(net_buy, net_sell) > -math.inf else 0.0, 2), []


MIN_TICK = 0.01


def unpriced(buckets) -> list:
    """Buckets the event object left half-quoted. Not the same as "nobody is trading them".

    Both sides matter. A buy-all set needs an ask on every leg; a SELL-all set (buy NO on every
    leg) needs a bid on every leg, and pays when the bids sum to over $1. The sell side is the one
    that tight books produce — usfed-fomc's four quoted legs bid 1.03 on 2026-09-18 while its asks
    summed to 1.08 — so screening only for missing asks would have looked straight past it.
    """
    return [b for b in buckets if b.best_ask is None or b.best_bid is None]


def arb_possible(buckets, floor: float = MIN_TICK, assume_exhaustive: bool = False) -> bool:
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
    if any(b.closed for b in buckets) or not (assume_exhaustive or exhaustive(buckets)):
        return False
    known_asks = [b.best_ask for b in buckets if b.best_ask is not None]
    if known_asks:
        # BUY side: an unquoted leg can be no cheaper than one tick.
        if sum(known_asks) + floor * (len(buckets) - len(known_asks)) < 1.0:
            return True
    # SELL side: an unquoted leg's bid can be no HIGHER than its own ask (and never above 0.99),
    # which is a tight bound rather than a hopeful one.
    best_case = 0.0
    for b in buckets:
        if b.best_bid is not None:
            best_case += b.best_bid
        else:
            best_case += min(b.best_ask if b.best_ask is not None else 0.99, 0.99)
    return best_case > 1.0


def sets_available(buckets, kind: str) -> float | None:
    """How many complete sets the book can actually fill, or None if depth was never looked up.

    One set is one contract of every leg, so the thinnest leg is the whole size. `None` propagates
    rather than defaulting to a number: an unknown depth must block the trade, not permit it.
    """
    qtys = [(b.ask_qty if kind == "buy_all" else b.bid_qty) for b in buckets]
    if any(q is None for q in qtys):
        return None
    return max(0.0, min(qtys))


def unwind_cost_cents(buckets, kind: str) -> float:
    """What it costs, in cents per set, to undo a set that only half-filled.

    Per leg: what we paid for it, minus what closing it would return. Buying YES at the ask and
    selling back at the bid loses the spread — but a leg with NO bid returns NOTHING, so it loses
    the whole stake. That is the worst case, and the old code SKIPPED the entire unwind test
    whenever any leg was one-sided (`len(spreads) == len(buckets)`), which let exactly those sets
    through unchecked and made a survey of buy-side arbs come back empty because it had quietly
    thrown out every book with an unquoted tail leg.
    """
    total = 0.0
    for b in buckets:
        if kind == "buy_all":
            paid = b.best_ask
            back = b.best_bid or 0.0          # no bid: nobody will take it off us at any price
        else:
            paid = None if b.best_bid is None else 1.0 - b.best_bid
            back = 0.0 if b.best_ask is None else 1.0 - b.best_ask
        if paid is None:
            return math.inf                   # cannot even price the leg: treat as untradable
        total += max(paid - back, 0.0)
    return total * 100


def depth_at(levels, limit_px: float | None, kind: str) -> float:
    """Contracts available at `limit_px` or better on this leg's ladder.

    This is the leg's margin for error when the set goes out: an order for 21 contracts against a
    ladder holding 22 is far likelier to be killed than the same order against a ladder holding
    20,000, and execution uses that to decide what to risk first.
    """
    if not levels or limit_px is None:
        return 0.0
    if kind == "buy_all":
        return sum(q for px, q in levels if px <= limit_px + 1e-9)
    return sum(q for px, q in levels if px >= limit_px - 1e-9)


def limit_for(levels, n: int) -> float | None:
    """The worst price we must accept to get `n` contracts from a price ladder, or None if the
    ladder is too thin.

    This is the price a FILL_OR_KILL order is placed at, so it is what the arb must be costed on:
    the fill may be better, but only the limit is guaranteed. Walking the ladder is where the size
    is — chicago's "71 or below" bid 3 contracts at 0.16 and 100 at 0.15, so one cent of price
    turned a 3-set arb into a 100-set one, and 3 sets x 6c is $0.18 against 100 x 5c = $5.00.
    """
    if not levels:
        return None
    taken = 0.0
    for px, qty in levels:
        taken += qty
        if taken >= n:
            return px
    return None


def size_for_profit(buckets, kind: str, cfg, days=None):
    """Choose the number of sets that maximises TOTAL profit, not cents per set.

    Every extra set costs price: the ladder gets worse as you take more of it. Sizing to
    top-of-book (the old `sets_available`) picks the best cents-per-set and the worst dollars,
    because the binding leg's top level is often a handful of contracts sitting above a hundred.
    Returns (contracts, limit_prices, net_cents_per_set) or (0, [], 0.0).
    """
    ladders = [(b.bid_levels if kind == "sell_all" else b.ask_levels) for b in buckets]
    if any(l is None for l in ladders):
        return 0, [], 0.0
    deepest = int(min((sum(q for _, q in l) for l in ladders), default=0))
    best = (0, [], 0.0, 0.0)          # contracts, prices, net_cents, total_profit
    for n in _candidate_sizes(deepest):
        prices = [limit_for(l, n) for l in ladders]
        if any(px is None for px in prices):
            continue
        if kind == "buy_all":
            cost = sum(prices) + sum(fees.leg_cost(px, 1, "us", maker=False, theta=b.fee_coefficient)
                                     for px, b in zip(prices, buckets))
            net = (1.0 - cost) * 100
            set_cost = sum(prices)
        else:
            proceeds = sum(prices) - sum(fees.leg_cost(px, 1, "us", maker=False, theta=b.fee_coefficient)
                                         for px, b in zip(prices, buckets))
            net = (proceeds - 1.0) * 100
            set_cost = len(buckets) - sum(prices)
        if net <= 0 or set_cost <= 0:
            continue
        if net < cfg.arb_unwind_cover * unwind_cost_cents(buckets, kind):
            continue
        if days is not None and (net / set_cost) / max(float(days), 0.5) < cfg.arb_min_roc_per_day_pct:
            continue
        # Two different questions: how much capital this ties up, and how much it can lose.
        unwind = unwind_cost_cents(buckets, kind) / 100.0
        by_risk = cfg.arb_max_risk_usd / unwind if unwind > 0 else cfg.arb_max_sets
        capped = int(min(n, cfg.arb_max_set_cost_usd / set_cost,
                         cfg.caps.max_exposure_usd / set_cost, by_risk, cfg.arb_max_sets))
        if capped < 1:
            continue
        total = (net / 100.0) * capped
        if total > best[3]:
            best = (capped, prices, round(net, 2), total)
    return best[0], best[1], best[2]


def _candidate_sizes(deepest: int):
    """Sizes worth evaluating. The ladder only changes price at level boundaries, so a geometric
    sweep finds the optimum without walking every integer up to several hundred."""
    if deepest < 1:
        return []
    out, n = [], 1
    while n <= deepest:
        out.append(n)
        n = max(n + 1, int(n * 1.3))
    out.append(deepest)
    return sorted(set(out))


def worth_confirming(buckets, net: float, cfg, days=None) -> bool:
    """Is this candidate worth spending a book call per leg to confirm?

    The depth read is the expensive half of the screen, and the two tests that kill most
    candidates — unwind cover and return per dollar-day — need only prices and a settlement date.
    Running them after the depth read meant paying five calls every two minutes to re-refuse the
    same structurally hopeless boc set: ~1,200 calls a day for an answer that never changes.
    Same rules as `BucketSum.scan`, which keeps them as the hard rail.
    """
    if net < cfg.bucket_sum_min_net_cents:
        return False
    kind = "buy_all" if _set_cost(buckets, net) and all(b.best_ask is not None for b in buckets) \
        and sum(b.best_ask for b in buckets) < 1.0 else "sell_all"
    if net < cfg.arb_unwind_cover * unwind_cost_cents(buckets, kind):
        return False
    if days is not None:
        set_cost = _set_cost(buckets, net)
        if set_cost and (net / set_cost) / max(float(days), 0.5) < cfg.arb_min_roc_per_day_pct:
            return False
    return True


def _set_cost(buckets, net: float) -> float:
    """Cost of one set in whichever direction the net came from — asks if buying, (1-bid) if
    selling. Only used for the cheap screen; `scan` recomputes it from the chosen direction."""
    asks = [b.best_ask for b in buckets]
    bids = [b.best_bid for b in buckets]
    if all(a is not None for a in asks) and sum(asks) < 1.0:
        return sum(asks)
    if all(x is not None for x in bids):
        return len(buckets) - sum(bids)
    return 0.0


class BucketSum(Strategy):
    name = "bucket_sum"

    def __init__(self, cfg: config.Config):
        self.cfg = cfg

    def scan(self, ctx) -> list:
        ev = ctx.event
        proven = bool(getattr(ctx, "proven_exhaustive", False))
        kind, net_top, _ = arb_check(ev.buckets, ctx.venue, assume_exhaustive=proven)
        if kind is None or net_top < self.cfg.bucket_sum_min_net_cents:
            return []
        days = getattr(ctx, "settles_in_days", None)
        # Size on the whole ladder, not the top of it. `size_for_profit` applies the unwind cover,
        # the dollar-day floor and the caps at every candidate size, and returns the one that makes
        # the most DOLLARS — which is rarely the one that makes the most cents per set.
        contracts, prices, net = size_for_profit(ev.buckets, kind, self.cfg, days)
        if contracts < 1 or not prices:
            # Either nobody has read the book yet (the runner answers that with a depth call and
            # scans again) or no size clears the filters.
            return []
        leg_costs = [px if kind == "buy_all" else round(1 - px, 2) for px in prices]
        set_cost = sum(leg_costs)
        roc_pct = net / set_cost if set_cost > 0 else 0.0
        if (net / 100.0) * contracts < self.cfg.arb_min_profit_usd:
            return []            # real, but not worth the calls or the operational risk
        group = f"{ev.slug}:{kind}:{int(net * 10)}"
        out = []
        for b, px in zip(ev.buckets, prices):
            side = "BUY_YES" if kind == "buy_all" else "BUY_NO"
            price = px if kind == "buy_all" else round(1 - px, 2)
            out.append(Signal(self.name, ctx.venue, b.yes_token, f"{ctx.city} {ctx.date} {ctx.kind} {b.title}", side,
                              price, price * contracts, net,
                              f"{kind}: {contracts} sets net {net:.1f}c each after taker fees "
                              f"({len(ev.buckets)} legs, ${set_cost * contracts:.2f} in, "
                              f"{roc_pct:.1f}% on capital"
                              + (f" over {days:.1f}d = {roc_pct / max(float(days), 0.5):.2f}%/day"
                                 if days is not None else "")
                              + f", ${(net / 100.0) * contracts:.2f} profit)",
                              # `contracts` is derived from size_usd/price, so the size IS the way to
                              # say "N contracts of this leg". Prices are 2dp and N is an integer, so
                              # price*N is exact at 2dp and the property reads back exactly N.
                              exit="settle", horizon_hours=30, taker=True, arb=True,
                              spread_cents=None if b.best_bid is None or b.best_ask is None
                              else round((b.best_ask - b.best_bid) * 100, 1),
                              meta={"market_id": b.market_id, "group": group, "legs": len(ev.buckets),
                                    "sets": contracts, "net_cents": net,
                                    "set_cost_usd": round(set_cost, 4),
                                    "roc_pct": round(roc_pct, 2),
                                    "settles_in_days": days,
                                    "unwind_usd": round(unwind_cost_cents(ev.buckets, kind)
                                                        / 100.0 * contracts, 2),
                                    "depth": round(depth_at(
                                        b.ask_levels if kind == "buy_all" else b.bid_levels,
                                        px, kind), 2)}))
        # Belt and braces. `contracts` is derived from size_usd/price, and the arithmetic only
        # round-trips exactly while prices are well behaved — a 4-decimal price rounded to cents
        # silently produced 11 contracts on one leg and 12 on another in test. An unbalanced set
        # is not an arb, it is a naked basket with a story attached, so if any leg disagrees the
        # whole set is dropped rather than half-placed.
        if any(sig.contracts != contracts for sig in out):
            return []
        return out
