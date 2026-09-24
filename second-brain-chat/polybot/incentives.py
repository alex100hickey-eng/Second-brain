"""Polymarket US liquidity incentives: the program terms, and what a resting quote would earn.

Terms (docs.polymarket.us/incentives/liquidity, read 2026-09-24):
  - Every second a random snapshot of the book is scored. Each side is scored independently and
    normalised to 1.0 per snapshot; a trader earns their share of the side's qualified score.
  - Score = discount_factor ** (ticks from the best price) x size. A tick is the market's minimum
    increment (0.1c books count 0.1c ticks).
  - Target size is an AGGREGATE threshold, not a per-trader cap: walking out from the best price,
    orders count until the running total reaches target size; orders beyond that depth score zero,
    and a side that never reaches it scores nothing that second (the pool for it is forfeited).
  - Max spread (optional; no non-sports program had one on 2026-09-24): both sides must sit within it
    of the midpoint, else the second is forfeited.
  - Pools are per market per period (`daily_event` / `daily` = one day). Paid within 5+2 business days.
  - Per-market parameters: GET https://api.polymarket.us/v1/incentives (authenticated).
We assume a side carries half the period's pool (each side is normalised to 1.0 per snapshot).
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

# Categories the incentives API answers for non-sports markets (2026-09-24). "SPR" is sports: off
# under the Ohio rule unless the switch is flipped.
NON_SPORT_CATEGORIES = ("POL", "CUL", "TECH", "FIN", "CRY", "MAC", "GEO", "SCI")
SPORTS_CATEGORY = "SPR"


def _ts(iso):
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def active_period(program: dict, now: float | None = None) -> dict | None:
    """The liquidity period paying right now, or None."""
    now = datetime.now(timezone.utc).timestamp() if now is None else now
    for t in program.get("timePeriods") or []:
        if t.get("programType") != "liquidityProgram" or t.get("status") != "active":
            continue
        start, end = _ts(t.get("start")), _ts(t.get("end"))
        if start is not None and start > now:
            continue
        if end is not None and end <= now:
            continue
        if not t.get("rewardPool"):
            continue
        return t
    return None


def fetch_programs(us, categories=NON_SPORT_CATEGORIES, max_pages: int = 20) -> list:
    """Every open-instrument liquidity program in the given categories (~12 calls for non-sports)."""
    out = []
    for cat in categories:
        token = None
        for _ in range(max_pages):
            q = {"page_size": 100, "statuses": ["active"], "program_type": "liquidityProgram",
                 "category": cat, "instrument_states": ["INSTRUMENT_STATE_OPEN"]}
            if token:
                q["page_token"] = token
            r = us._guarded(lambda q=q: us._client.get("/v1/incentives", query=q, authenticated=True),
                            default=None) if us is not None and us.available else None
            if not r:
                break
            out.extend(r.get("programs") or [])
            token = r.get("nextPageToken")
            if not token or not r.get("programs"):
                break
    return out


def tick_of(levels_a, levels_b, default: float = 0.01) -> float:
    """The market's increment, inferred from the book: any price off the whole cent means 0.1c."""
    for px, _ in list(levels_a or []) + list(levels_b or []):
        if abs(round(px * 100) - px * 100) > 1e-6:
            return 0.001
    return default


def side_score(levels, target: float, discount: float, tick: float, ours=None, bid_side: bool = True):
    """(total qualified score, our score, qualified?) for one side of the book.

    `levels` is best-first [(px, qty)] WITHOUT our order; `ours` is (px, qty) or None. Our order is
    inserted at its price (joining a level puts it with that level)."""
    lv = [(float(px), float(q), False) for px, q in (levels or []) if q and q > 0]
    if ours and ours[1] > 0:
        lv.append((float(ours[0]), float(ours[1]), True))
    if not lv:
        return 0.0, 0.0, False
    lv.sort(key=lambda x: -x[0] if bid_side else x[0])
    best = lv[0][0]
    cum = total = mine = 0.0
    reached = target <= 0
    for i, (px, q, is_ours) in enumerate(lv):
        # A whole level scores once the walk reaches it: "if Target Size is 20,000 and there are
        # 25,000 contracts resting at the best price, orders at the second-best price receive zero".
        if reached and i and px != lv[i - 1][0]:
            break
        w = discount ** int(round(abs(px - best) / tick))
        total += w * q
        if is_ours:
            mine += w * q
        cum += q
        if target > 0 and cum >= target:
            reached = True
    if not reached:
        return 0.0, 0.0, False
    return total, mine, True


def quote_rate(book: dict, period: dict, our_bid=None, our_ask=None) -> dict:
    """What a quote earns per day on this book: {'bid': share, 'ask': share, 'usd_per_day': x}.

    our_bid / our_ask are (px, qty). A side whose aggregate size never reaches target size earns 0."""
    bids, asks = book.get("bids") or [], book.get("asks") or []
    tick = tick_of(bids, asks)
    df = float(period.get("discountFactor") or 1.0)
    target = float(period.get("targetSize") or 0)
    pool = float(period.get("rewardPool") or 0)
    out = {"bid": 0.0, "ask": 0.0, "bid_ok": False, "ask_ok": False, "tick": tick}
    for side, lv, ours, is_bid in (("bid", bids, our_bid, True), ("ask", asks, our_ask, False)):
        total, mine, ok = side_score(lv, target, df, tick, ours, bid_side=is_bid)
        out[f"{side}_ok"] = ok
        out[side] = (mine / total) if ok and total > 0 else 0.0
    max_spread = period.get("maxSpread")
    if max_spread and bids and asks:
        mid = (bids[0][0] + asks[0][0]) / 2
        for side, ours in (("bid", our_bid), ("ask", our_ask)):
            if not ours or abs(ours[0] - mid) > float(max_spread) + 1e-9:
                out["bid"] = out["ask"] = 0.0         # the whole second is forfeited
    out["usd_per_day"] = (out["bid"] + out["ask"]) * pool / 2.0
    return out


def collateral(px: float, qty: float, side: str) -> float:
    """Dollars a resting order ties up: a bid pays px, an ask (short YES) posts 1 - px."""
    return qty * (px if side == "bid" else 1.0 - px)


def size_for(px: float, side_usd: float, side: str) -> float:
    per = px if side == "bid" else 1.0 - px
    return float(math.floor(side_usd / per)) if per > 0 else 0.0
