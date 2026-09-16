"""Polymarket US adapter over the official `polymarket-us` SDK (pip install polymarket-us).

Even market data is key-gated (verified 2026-09-12: 401 without headers), so `available` is
False until POLYMARKET_KEY_ID / POLYMARKET_SECRET_KEY exist in the environment. Every method
degrades to None/[] when unavailable so the rest of the bot keeps running in paper mode.

Only LIMIT orders are ever sent (maker intent). No market orders exist in this file on purpose.
"""
from __future__ import annotations

import json
import math
import os
import re
from datetime import datetime

from .. import config
from .offshore import Bucket, WeatherEvent, station_from_description

try:  # the SDK is optional until the key exists
    from polymarket_us import PolymarketUS  # type: ignore
except Exception:  # pragma: no cover - import guard
    PolymarketUS = None


# ---- response shapes, verified live 2026-09-12 with the real key ---------------------------
# markets.bbo(slug)   -> {"marketData": {"bestBid": {"value": "0.27"}, "bestAsk": {...}, "lastTradePx": {...},
#                         "settlementPx": {...}, "state": "MARKET_STATE_OPEN", "bidDepth": 14, ...}}
# markets.book(slug)  -> {"marketData": {"bids": [{"px": {"value": ".27"}, "qty": "1.0"}], "asks": [...]}}
# account.balances()  -> {"balances": [{"currentBalance": 210.01, "buyingPower": 210.01, "displayedCash": 60.01,
#                         "bonusReservation": 150, ...}]}
# portfolio.positions()-> {"positions": {...}, "availablePositions": [...]}   orders.list -> {"orders": [...]}
# search.query        -> [event, ...]; event {slug: "temp-nychigh-2026-09-13", title, markets: [market, ...]}
# market              -> {slug: "tc-temp-nychigh-2026-09-13-gte78lt79f", title: "78 to 79", outcomePrices:
#                         '["0.35","0.36"]', bestBidQuote: {value}, bestAskQuote: {value}, status:
#                         "MARKET_STATUS_OPEN", description: "... at Central Park (KNYC) ...", feeCoefficient}

_US_RANGE = re.compile(r"(-?\d+(?:\.\d+)?)\s*(?:to|-|–)\s*(-?\d+(?:\.\d+)?)", re.I)
_US_NUM = re.compile(r"-?\d+(?:\.\d+)?")


def parse_us_bucket_title(title: str):
    """'77 or below' -> (-inf, 77); '78 to 79' -> (78, 79); '86 or higher' -> (86, inf). Whole °F."""
    t = (title or "").strip().lower()
    m = _US_RANGE.search(t)
    if m:
        return float(m.group(1)), float(m.group(2))
    n = _US_NUM.search(t)
    if not n:
        raise ValueError(f"unparseable US bucket title: {title!r}")
    a = float(n.group(0))
    if any(w in t for w in ("below", "less", "under")):
        return -math.inf, a
    if any(w in t for w in ("higher", "above", "more", "over")):
        return a, math.inf
    return a, a


def _yes_price(m) -> float | None:
    try:
        return float(json.loads(m.get("outcomePrices") or "[]")[0])
    except (ValueError, IndexError, TypeError):
        return None


def _market_closed(m) -> bool:
    return bool(m.get("closed")) or str(m.get("status", "")).endswith(("RESOLVED", "SETTLED", "CLOSED"))


def bucket_from_us_market(m: dict) -> Bucket:
    lo, hi = parse_us_bucket_title(m.get("title") or m.get("titleShort") or "")
    slug = m.get("slug") or ""
    last = _yes_price(m)
    closed = _market_closed(m)
    outcome = None
    if closed and last is not None:
        outcome = 1 if last >= 0.99 else 0 if last <= 0.01 else None
    return Bucket(title=m.get("title") or m.get("titleShort") or "", lo=lo, hi=hi, unit="F",
                  yes_token=slug, no_token=slug, market_id=slug, condition_id=str(m.get("id") or ""),
                  best_bid=_price(m.get("bestBidQuote")), best_ask=_price(m.get("bestAskQuote")), last=last,
                  closed=closed, outcome=outcome, liquidity=0.0)


def us_event_slug(city_slug: str, date: datetime, kind: str = "high") -> str:
    meta = config.city_meta(city_slug) or {}
    code = (meta.get("cli_location") or "").lower()
    return f"temp-{code}{'high' if kind == 'high' else 'low'}-{date.strftime('%Y-%m-%d')}"


def weather_event_from_us(e: dict, city_slug: str, kind: str = "high") -> WeatherEvent | None:
    markets = [m for m in (e.get("markets") or []) if m.get("slug")]
    if not markets:
        return None
    buckets = sorted((bucket_from_us_market(m) for m in markets), key=lambda b: b.lo)
    desc = " ".join(m.get("description") or "" for m in markets[:1])
    station = station_from_description(desc) or (config.city_meta(city_slug) or {}).get("us_station")
    slug = e.get("slug") or ""
    m_date = re.search(r"(\d{4}-\d{2}-\d{2})$", slug)
    date = m_date.group(1) if m_date else (e.get("endDate") or "")[:10]
    return WeatherEvent(slug=slug, city=city_slug, date=date, station=station, rule="cli", unit="F",
                        buckets=buckets, neg_risk=False, end_iso=e.get("endDate") or "", kind=kind)


def _unwrap(d):
    return (d or {}).get("marketData") or d or {}


def _is_not_found(exc: Exception) -> bool:
    return "NotFound" in type(exc).__name__ or "not found" in str(exc).lower()


class USVenue:
    name = "us"

    def __init__(self):
        self.key_id = os.environ.get("POLYMARKET_KEY_ID")
        self.secret = os.environ.get("POLYMARKET_SECRET_KEY")
        self.sdk_installed = PolymarketUS is not None
        self.available = bool(self.sdk_installed and self.key_id and self.secret)
        self._client = PolymarketUS(key_id=self.key_id, secret_key=self.secret) if self.available else None

    @property
    def why_unavailable(self) -> str:
        if not self.sdk_installed:
            return "polymarket-us SDK not installed (pip install polymarket-us)"
        if not (self.key_id and self.secret):
            return "POLYMARKET_KEY_ID / POLYMARKET_SECRET_KEY not set"
        return ""

    # ---- market data ---------------------------------------------------------------------
    def search(self, query: str):
        if not self.available:
            return None
        return self._client.search.query({"query": query})

    def events(self, **params):
        if not self.available:
            return []
        return self._client.events.list(params or {"limit": 50, "active": True}).get("events", [])

    def market(self, slug: str):
        return self._client.markets.retrieve_by_slug(slug) if self.available else None

    def bbo(self, slug: str):
        """Return (bid, ask) as floats, or (None, None)."""
        if not self.available:
            return None, None
        d = _unwrap(self._client.markets.bbo(slug))
        return _price(d.get("bestBid", d.get("bid"))), _price(d.get("bestAsk", d.get("ask")))

    def bbo_full(self, slug: str) -> dict:
        """The whole marketData block: bestBid/bestAsk/lastTradePx/settlementPx/state/depths."""
        return _unwrap(self._client.markets.bbo(slug)) if self.available else {}

    def book(self, slug: str):
        """{'bids': [(px, qty)...] high→low, 'asks': [(px, qty)...] low→high, 'last': float|None}."""
        if not self.available:
            return None
        d = _unwrap(self._client.markets.book(slug))
        bids = sorted(((_price(x.get("px")), float(x.get("qty") or 0)) for x in d.get("bids", []) if _price(x.get("px")) is not None), reverse=True)
        asks = sorted(((_price(x.get("px")), float(x.get("qty") or 0)) for x in d.get("asks", []) if _price(x.get("px")) is not None))
        return {"bids": bids, "asks": asks, "last": _price(d.get("lastTradePx")), "tick": 0.01}

    # ---- weather events (the five US cities) ----------------------------------------------
    def find_weather_event(self, city_slug: str, date: datetime, kind: str = "high") -> WeatherEvent | None:
        """The US venue's own temperature event for a city/day, or None. Slug first, then search."""
        if not self.available:
            return None
        slug = us_event_slug(city_slug, date, kind)
        e = None
        try:
            e = self._client.events.retrieve_by_slug(slug)
        except Exception as exc:
            if not _is_not_found(exc):
                raise
        if not e or not e.get("markets"):
            word = "Highest" if kind == "high" else "Lowest"
            query = (config.city_meta(city_slug) or {}).get("query", city_slug)
            res = self._client.search.query({"query": f"{word} temperature in {query}"}) or []
            items = res if isinstance(res, list) else (res.get("events") or res.get("results") or [])
            e = next((x for x in items if isinstance(x, dict) and x.get("slug") == slug), None)
        return weather_event_from_us(e, city_slug, kind) if e else None

    def resolution(self, slug: str) -> int | None:
        """1/0 once the market settled, else None. Settlement is a 404 until it exists."""
        if not self.available:
            return None
        try:
            s = self._client.markets.settlement(slug) or {}
        except Exception as exc:
            if _is_not_found(exc):
                return None
            raise
        s = _unwrap(s)
        px = _price(s.get("settlementPx") or s.get("settlementPrice") or s.get("price") or s.get("px"))
        if px is None:
            for k in ("result", "outcome", "winningOutcome", "winner"):
                v = str(s.get(k, "")).lower()
                if v in ("yes", "long", "1"):
                    return 1
                if v in ("no", "short", "0"):
                    return 0
            return None
        return 1 if px >= 0.99 else 0 if px <= 0.01 else None

    # ---- account -------------------------------------------------------------------------
    def balance_usd(self) -> float | None:
        """Buying power (cash + any promo credit the venue lets you trade with)."""
        if not self.available:
            return None
        b = self._client.account.balances() or {}
        rows = b.get("balances") if isinstance(b, dict) else b
        row = (rows or [{}])[0] if isinstance(rows, list) else (rows or {})
        for k in ("buyingPower", "currentBalance", "available", "cash", "balance", "total"):
            if row.get(k) is not None:
                return _price(row[k])
        return None

    def account_value_usd(self) -> float | None:
        """Cash buying power PLUS what is already sitting in open positions.

        `balance_usd` returns buying power, which is what you can deploy right now — and on
        2026-09-16 that read $0.41 while the account actually held ~$187 in two open positions.
        Sizing and the bankroll floor must use account VALUE, or the bot halts the moment money is
        working ("bankroll under floor") and never compounds. Alex's rule: you start at $200, and
        what you make becomes what you trade with."""
        if not self.available:
            return None
        cash = self.balance_usd() or 0.0
        held = 0.0
        for p in (self.positions() or []):
            cost = p.get("cost") if isinstance(p, dict) else None
            val = (cost or {}).get("value") if isinstance(cost, dict) else None
            if val is not None:
                try:
                    held += float(val)
                except (TypeError, ValueError):
                    pass
        return round(cash + held, 2)


    def balance_detail(self) -> dict:
        if not self.available:
            return {}
        b = self._client.account.balances() or {}
        rows = b.get("balances") if isinstance(b, dict) else b
        row = (rows or [{}])[0] if isinstance(rows, list) else (rows or {})
        return {k: row.get(k) for k in ("buyingPower", "currentBalance", "displayedCash", "bonusReservation",
                                        "openOrders", "availableToWithdraw") if k in row}

    def positions(self):
        if not self.available:
            return []
        d = self._client.portfolio.positions() or {}
        if isinstance(d, list):
            return d
        pos = d.get("positions")
        if isinstance(pos, dict):
            pos = list(pos.values())
        return list(pos or []) or list(d.get("availablePositions") or [])

    # ---- orders (limit only) -------------------------------------------------------------
    INTENTS = {"BUY_YES": "ORDER_INTENT_BUY_LONG", "BUY_NO": "ORDER_INTENT_BUY_SHORT",
               "SELL_YES": "ORDER_INTENT_SELL_LONG", "SELL_NO": "ORDER_INTENT_SELL_SHORT"}

    def place_limit(self, slug: str, side: str, price: float, contracts: int):
        """side: BUY_YES (open long) · BUY_NO (open short) · SELL_YES / SELL_NO (close). Limit + GTC only.
        The SELL_* intent names follow the SDK's BUY_LONG/BUY_SHORT pattern and are unverified until
        the first live take-profit; the executor logs the venue's reply either way."""
        if not self.available:
            raise RuntimeError(self.why_unavailable)
        intent = self.INTENTS[side]
        return self._client.orders.create({
            "marketSlug": slug,
            "intent": intent,
            "type": "ORDER_TYPE_LIMIT",
            "price": {"value": f"{price:.2f}", "currency": "USD"},
            "quantity": int(contracts),
            "tif": "TIME_IN_FORCE_GOOD_TILL_CANCEL",
        })

    def cancel(self, order_id: str, slug: str):
        return self._client.orders.cancel(order_id, {"marketSlug": slug}) if self.available else None

    def cancel_all(self):
        return self._client.orders.cancel_all() if self.available else None

    def open_orders(self):
        if not self.available:
            return []
        d = self._orders_list()
        if isinstance(d, list):
            return d
        return list((d or {}).get("orders") or [])

    def _orders_list(self):
        return self._client.orders.list() if self.available else []

    def close(self):
        if self._client:
            self._client.close()


def _price(x):
    if x is None:
        return None
    if isinstance(x, dict):
        x = x.get("value", x.get("price"))
    try:
        return float(x)
    except (TypeError, ValueError):
        return None
