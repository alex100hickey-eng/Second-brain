"""Polymarket US adapter over the official `polymarket-us` SDK (pip install polymarket-us).

Even market data is key-gated (verified 2026-09-12: 401 without headers), so `available` is
False until POLYMARKET_KEY_ID / POLYMARKET_SECRET_KEY exist in the environment. Every method
degrades to None/[] when unavailable so the rest of the bot keeps running in paper mode.

Only LIMIT orders are ever sent (maker intent). No market orders exist in this file on purpose.
"""
from __future__ import annotations

import os

try:  # the SDK is optional until the key exists
    from polymarket_us import PolymarketUS  # type: ignore
except Exception:  # pragma: no cover - import guard
    PolymarketUS = None


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
        d = self._client.markets.bbo(slug) or {}
        return _price(d.get("bid")), _price(d.get("ask"))

    def book(self, slug: str):
        return self._client.markets.book(slug) if self.available else None

    # ---- account -------------------------------------------------------------------------
    def balance_usd(self) -> float | None:
        if not self.available:
            return None
        b = self._client.account.balances() or {}
        for k in ("available", "cash", "balance", "total"):
            if k in b:
                return _price(b[k])
        return None

    def positions(self):
        return self._client.portfolio.positions() if self.available else []

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
