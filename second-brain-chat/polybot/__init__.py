"""polybot — Alex's multi-strategy Polymarket bot.

Design: `Money/Polymarket Bot — Design v1 (2026-09-12).md` in the vault.

Every strategy module has a mode: off → paper → signal → live. The ledger decides
promotion; nothing goes live on opinion. Every "swing" module is defined by the
reference the market lags (a forecast model, a faster venue, the resolution feed),
never by a price pattern alone.

Venues:
  offshore  polymarket.com — READ-ONLY reference + paper proxy. Fully US-blocked for
            trading; the bot can never place an order there.
  us        Polymarket US (QCX) — the only tradable venue. Needs POLYMARKET_KEY_ID and
            POLYMARKET_SECRET_KEY in the environment (even market data is key-gated).
"""

__version__ = "0.1.0"
