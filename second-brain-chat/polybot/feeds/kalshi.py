"""Kalshi's public market data (no key): the Fed-decision books fed_lag uses as its reference.

Why Kalshi and not CME FedWatch: the FedWatch probabilities come from CME, whose site answered
2026-09-25 with a 403 naming its Data Terms of Use, which prohibit scripted access; the paid
FedWatch API is the only permitted route. Kalshi publishes a documented, free market-data API, and
its KXFEDDECISION books are the deepest prediction-market Fed books (Oct 2026: ~$1M traded per
outcome, 1c spreads). One call returns every open meeting with its five outcomes.
"""
from __future__ import annotations

from . import offshore

BASE = "https://api.elections.kalshi.com/trade-api/v2"
FED_SERIES = "KXFEDDECISION"
# Kalshi outcome suffix -> Polymarket US market-slug suffix (rdc-usfed-fomc-<date>-<suffix>)
FED_OUTCOMES = {"C26": "cut50", "C25": "cut25", "H0": "nochng", "H25": "hike25", "H26": "hike50"}


def _px(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def fed_events(get=None) -> list:
    """[{'date': 'YYYY-MM-DD', 'event': ticker, 'markets': [{'ticker', 'outcome', 'bid', 'ask'}]}]
    for every open Fed meeting. A side with no resting order reads None, never 0."""
    get = get or offshore._get
    d = get(f"{BASE}/events", {"series_ticker": FED_SERIES, "status": "open", "limit": 50,
                                "with_nested_markets": "true"}) or {}
    out = []
    for e in d.get("events") or []:
        date = (e.get("strike_date") or "")[:10]
        if not date:
            continue
        mkts = []
        for m in e.get("markets") or []:
            outcome = (m.get("ticker") or "").rsplit("-", 1)[-1]
            if outcome not in FED_OUTCOMES or m.get("status") not in (None, "active", "open"):
                continue
            bid, ask = _px(m.get("yes_bid_dollars")), _px(m.get("yes_ask_dollars"))
            mkts.append({"ticker": m["ticker"], "outcome": outcome,
                         "bid": bid if bid else None, "ask": ask if ask and ask < 1.0 else None})
        out.append({"date": date, "event": e.get("event_ticker"), "markets": mkts})
    return out


def us_event_slug(date: str) -> str:
    return f"usfed-fomc-{date}"


def us_market_slug(date: str, outcome: str) -> str:
    return f"rdc-usfed-fomc-{date}-{FED_OUTCOMES[outcome]}"
