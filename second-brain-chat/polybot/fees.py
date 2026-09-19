"""Fee math for both venues. All functions return DOLLARS for `contracts` contracts.

Polymarket US (schedule effective 2026-07-01, docs.polymarket.us/fees):
    fee = theta * contracts * p * (1 - p)
    taker theta = 0.06   (max $1.50 per 100 contracts at 50c)
    maker theta = -0.0125 (a REBATE, max $0.31 per 100 at 50c)

polymarket.com (2026-07-10, help.polymarket.com trading-fees): makers never pay; takers
pay rate * p * (1-p) per share with the rate by category.
"""

# The live gateway reports its own `feeCoefficient` on every market, and on 2026-09-19 all 30
# US weather markets said 0.0695 -- not the 0.06 the July fee schedule documented. A stale
# constant here is not a rounding error for an arb: it decides whether a set is taken at all.
# Scoring a set at 1.0c net with theta=0.06 means 0.38c in reality, against a 1.0c floor, so the
# threshold was clearing trades that did not actually clear it. Prefer the per-market number the
# venue hands us (`theta=`); this constant is only the fallback when a caller has none.
US_TAKER_THETA = 0.0695
US_MAKER_THETA = -0.0125

OFFSHORE_TAKER_RATE = {
    "crypto": 0.07,
    "sports": 0.05,
    "finance": 0.04,
    "politics": 0.04,
    "mentions": 0.04,
    "tech": 0.04,
    "economics": 0.05,
    "culture": 0.05,
    "weather": 0.05,
    "geopolitics": 0.0,
    "other": 0.05,
}


def _pq(price: float) -> float:
    price = min(max(float(price), 0.0), 1.0)
    return price * (1.0 - price)


def us_taker_fee(price: float, contracts: float = 1, theta: float | None = None) -> float:
    """Dollars paid when an order TAKES liquidity on Polymarket US.

    `theta` is the market's own `feeCoefficient` when the caller has it; underestimating a cost
    that decides whether to trade is the expensive direction, so the venue's number wins."""
    return (US_TAKER_THETA if theta is None else float(theta)) * contracts * _pq(price)


def us_maker_rebate(price: float, contracts: float = 1) -> float:
    """Dollars RECEIVED when a resting order is filled on Polymarket US (positive number)."""
    return -US_MAKER_THETA * contracts * _pq(price)


def offshore_taker_fee(price: float, contracts: float = 1, category: str = "other") -> float:
    rate = OFFSHORE_TAKER_RATE.get(category, OFFSHORE_TAKER_RATE["other"])
    return rate * contracts * _pq(price)


def leg_cost(price: float, contracts: float, venue: str, maker: bool, category: str = "other",
             theta: float | None = None) -> float:
    """Net dollars this leg costs in fees. Negative means a rebate.

    `theta` overrides the US taker coefficient with the market's own `feeCoefficient`."""
    if venue == "us":
        return -us_maker_rebate(price, contracts) if maker else us_taker_fee(price, contracts, theta)
    if venue == "offshore":
        return 0.0 if maker else offshore_taker_fee(price, contracts, category)
    raise ValueError(f"unknown venue {venue}")


def round_trip_cost(price_in: float, price_out: float, contracts: float, venue: str,
                    maker_in: bool = True, maker_out: bool = True, category: str = "other") -> float:
    """Fees for entering at price_in and exiting at price_out (dollars, negative = net rebate)."""
    return (leg_cost(price_in, contracts, venue, maker_in, category)
            + leg_cost(price_out, contracts, venue, maker_out, category))


def swing_breakeven_cents(price: float, venue: str, maker_in: bool, maker_out: bool,
                          spread_cents: float, category: str = "other") -> float:
    """How many cents a swing must move to clear fees + the spread you cross.

    Taking both legs at 50c on Polymarket US costs 3c per contract; resting both legs
    earns 0.6c. The spread is paid once per taken leg.
    """
    fees = round_trip_cost(price, price, 1, venue, maker_in, maker_out, category) * 100.0
    crossed = spread_cents * ((0 if maker_in else 1) + (0 if maker_out else 1))
    return fees + crossed
