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
        if sig.size_usd < caps.min_order_usd and not sig.arb:
            # An arb leg is not a standalone trade — it is one sixth of a set that either happens
            # whole or not at all, and a leg priced at 1c is SUPPOSED to cost cents. Judging it by
            # the dust threshold meant for single bets would refuse the cheap legs and leave the
            # expensive ones filled: the exact naked-basket outcome the set exists to avoid.
            # (The venue may enforce its own minimum; unverified until the first live arb.)
            return False, f"below min order ${caps.min_order_usd:.0f}"
        if sig.arb:
            # An arb leg is judged by what the SET costs, not by the per-market cap meant for
            # single directional bets — the set is the thing that either completes and pays $1 or
            # has to be unwound. The strategy sizes to this, and this is the hard rail under it.
            set_cost = float((sig.meta or {}).get("set_cost_usd") or 0.0) * sig.contracts
            if set_cost > self.cfg.arb_max_set_cost_usd + 1e-9:
                return False, (f"arb set ${set_cost:.2f} over set cap "
                               f"${self.cfg.arb_max_set_cost_usd:.0f}")
            # And the cap that matches the actual failure: a completed set pays $1 regardless, so
            # what is at stake is unwinding a half-fill, not the capital it ties up.
            at_risk = float((sig.meta or {}).get("unwind_usd") or 0.0)
            if at_risk > self.cfg.arb_max_risk_usd + 1e-9:
                return False, (f"arb risks ${at_risk:.2f} on a failed fill, over "
                               f"${self.cfg.arb_max_risk_usd:.0f}")
        elif sig.size_usd > caps.max_per_market_usd + 1e-9:
            return False, f"over per-market cap ${caps.max_per_market_usd:.0f}"
        if (sig.category or "") == "sports" and not caps.sports_enabled:
            return False, "sports disabled (Ohio)"
        if not (0.01 <= sig.price <= 0.99):
            return False, "price outside 1-99c"
        if sig.taker and not (sig.arb or sig.taker_ok):
            return False, "taker order outside an arb"
        mode = mode or self.cfg.mode(sig.module)
        live_like = mode == "live"
        if live_like and sig.arb and not self.cfg.arb_live_ok:
            # An arb is all-or-nothing by definition, and `execution.py` places each leg as an
            # independent order with no notion of the group. Fill four of six and you are not
            # arbed — you are holding a naked basket that the set was built to avoid, with no
            # unwind path. Paper can measure this safely; real money cannot until the executor
            # can complete or unwind a partial set. Flip `arb_live_ok` when that exists.
            return False, "arb legs need group execution before live (arb_live_ok is off)"
        # One position per market, in every mode: the 2026-09-12 paper run re-entered the same bucket
        # every 3 hours (133 extra entries), so one wrong call cost $40-60 instead of $20. An arb
        # leg is measured against the set cap for the same reason it is sized against it — the leg
        # is not a position anyone took a view on, it is a sixth of something that pays $1.
        per_market_cap = self.cfg.arb_max_set_cost_usd if sig.arb else caps.max_per_market_usd
        existing = self.ledger.exposure_usd(sig.venue, sig.market, live_only=live_like)
        if existing + sig.size_usd > per_market_cap + 1e-9:
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
