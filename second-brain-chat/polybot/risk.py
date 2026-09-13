"""The shared risk manager. Every order, in every mode, passes through `allow()`.

Caps are enforced here in code, not in judgment:
  per-market cap · total exposure cap · bankroll floor · daily loss stop · sports gate (Ohio) ·
  kill switch file · maker-only (taker signals are refused in live mode unless the module says arb).
"""
from __future__ import annotations

from . import config


class RiskManager:
    def __init__(self, cfg: config.Config, ledger):
        self.cfg = cfg
        self.ledger = ledger

    def allow(self, sig, bankroll_usd: float | None = None, mode: str | None = None):
        """Return (ok, reason). Applies to paper too, so paper stats reflect what live would do.
        `mode` is the effective mode for this signal (a live module's offshore signals are paper)."""
        caps = self.cfg.caps
        bankroll = self.cfg.bankroll_usd if bankroll_usd is None else bankroll_usd
        if config.kill_switch_on():
            return False, "kill switch on"
        if sig.size_usd <= 0 or sig.contracts <= 0:
            return False, "zero size"
        if sig.size_usd < caps.min_order_usd:
            return False, f"below min order ${caps.min_order_usd:.0f}"
        if sig.size_usd > caps.max_per_market_usd + 1e-9:
            return False, f"over per-market cap ${caps.max_per_market_usd:.0f}"
        if (sig.category or "") == "sports" and not caps.sports_enabled:
            return False, "sports disabled (Ohio)"
        if not (0.01 <= sig.price <= 0.99):
            return False, "price outside 1-99c"
        if sig.taker and not sig.arb:
            return False, "taker order outside an arb"
        mode = mode or self.cfg.mode(sig.module)
        live_like = mode == "live"
        # One position per market, in every mode: the 2026-09-12 paper run re-entered the same bucket
        # every 3 hours (133 extra entries), so one wrong call cost $40-60 instead of $20.
        existing = self.ledger.exposure_usd(sig.venue, sig.market, live_only=live_like)
        if existing + sig.size_usd > caps.max_per_market_usd + 1e-9:
            return False, f"market exposure ${existing:.0f}+${sig.size_usd:.0f} > cap (already in this market)"
        # Portfolio-level caps. In paper mode they are recorded, not enforced: the ledger must see every
        # signal a module produces to measure it, and the note tells us what live sizing would have cut.
        portfolio_refusal = None
        total = self.ledger.exposure_usd(sig.venue, None, live_only=live_like)
        if total + sig.size_usd > caps.max_exposure_usd + 1e-9:
            portfolio_refusal = f"total exposure ${total:.0f}+${sig.size_usd:.0f} > cap ${caps.max_exposure_usd:.0f}"
        if portfolio_refusal is None and bankroll < caps.bankroll_floor_usd:
            portfolio_refusal = f"bankroll ${bankroll:.0f} under floor ${caps.bankroll_floor_usd:.0f}"
        if portfolio_refusal is None and live_like and self.ledger.realized_today_usd("live") <= -caps.daily_loss_stop_usd:
            portfolio_refusal = "daily loss stop hit"
        if portfolio_refusal:
            if mode == "paper":
                sig.meta["live_would_refuse"] = portfolio_refusal
                return True, f"paper (live would refuse: {portfolio_refusal})"
            return False, portfolio_refusal
        return True, "ok"
