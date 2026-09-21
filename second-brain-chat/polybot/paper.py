"""Paper engine: turns recorded signals into fills, exits and P&L using what the market did next.

Fill rule (conservative): a resting BUY at price P fills only if the market later TRADED at or
below P (someone hit our bid); a resting BUY_NO (i.e. a YES sell at 1-P) fills only if YES later
traded at or above 1-P. Exit rules: 'settle' waits for resolution; 'tp:X' sells X above entry as
a maker (fills when YES later trades at or above entry+X); 'reference' / 'timeout' close at the
last price after the horizon. Fees use the venue's real schedule (offshore makers pay nothing;
US makers earn the rebate). Open positions are marked at the last price as unrealized.
"""
from __future__ import annotations

import json
import time

from . import fees
from .feeds import offshore


def _yes_entry(sig) -> float:
    """Our order expressed as a YES price level: BUY_YES rests at P; BUY_NO at P is a YES sell at 1-P."""
    return sig["price"] if sig["side"] == "BUY_YES" else round(1 - sig["price"], 2)


def is_taker(sig) -> bool:
    try:
        return bool(json.loads(sig.get("meta") or "{}").get("taker", False))
    except (TypeError, ValueError):
        return False


def fill_from_history(sig, history):
    """Return (fill_ts, fill_price_yes) or (None, None).

    A RESTING order fills only if the market later trades through our level — someone has to come
    and hit it. A TAKER order does not wait for anybody: crossing the spread is the fill, at the
    price we crossed at, at the moment we sent it. Modelling a taker as a resting order made the
    cheap legs of an arb set depend on a 1c bucket printing a trade, so a set could "fill" four
    legs of six in the paper record — which is not an arb, and would have quietly poisoned the
    only evidence bucket_sum is being judged on. (weather_lock's taker entries filled anyway, by
    accident: the mid always sits below the ask we bought at.)
    """
    if is_taker(sig):
        return sig["ts"], _yes_entry(sig)
    lvl = _yes_entry(sig)
    for t, p in history:
        if t < sig["ts"]:
            continue
        if sig["side"] == "BUY_YES" and p <= lvl:
            return t, lvl
        if sig["side"] == "BUY_NO" and p >= lvl:
            return t, lvl
    return None, None


def exit_from_history(sig, fill_ts, history):
    """Return (exit_ts, exit_price_yes, kind) or (None, None, None) for a still-open position."""
    rule = sig["exit_rule"] or "settle"
    lvl = _yes_entry(sig)
    if rule.startswith("tp:"):
        tp = float(rule.split(":", 1)[1])
        target = lvl + tp if sig["side"] == "BUY_YES" else lvl - tp
        for t, p in history:
            if t <= fill_ts:
                continue
            if sig["side"] == "BUY_YES" and p >= target:
                return t, round(target, 2), "tp"
            if sig["side"] == "BUY_NO" and p <= target:
                return t, round(target, 2), "tp"
        return None, None, None
    if rule.startswith("timeout:") or rule == "reference":
        hours = float(rule.split(":", 1)[1].rstrip("h")) if ":" in rule else sig["horizon_h"] or 6
        deadline = fill_ts + hours * 3600
        later = [(t, p) for t, p in history if t >= deadline]
        if later:
            return later[0][0], later[0][1], "timeout"
        return None, None, None
    return None, None, None  # 'settle': resolution decides


def pnl_usd(sig, fill_price_yes, exit_price_yes, outcome, venue: str, category: str):
    """Dollars including fees, for `contracts` contracts. exit_price_yes None → use the outcome."""
    n = sig["contracts"]
    entry_leg = fill_price_yes if sig["side"] == "BUY_YES" else 1 - fill_price_yes
    if exit_price_yes is None:
        payoff = (1.0 if outcome == 1 else 0.0) if sig["side"] == "BUY_YES" else (1.0 if outcome == 0 else 0.0)
        exit_leg_price, exit_maker = None, True
    else:
        payoff = exit_price_yes if sig["side"] == "BUY_YES" else 1 - exit_price_yes
        exit_leg_price, exit_maker = payoff, True
    gross = (payoff - entry_leg) * n
    taker = sig.get("taker")
    if taker is None:
        try:
            taker = json.loads(sig.get("meta") or "{}").get("taker", False)
        except (TypeError, ValueError):
            taker = False
    fee = fees.leg_cost(entry_leg, n, venue, maker=not taker, category=category)
    if exit_leg_price is not None:
        fee += fees.leg_cost(exit_leg_price, n, venue, maker=exit_maker, category=category)
    return round(gross - fee, 4), round(fee, 4)


def _is_arb(sig) -> bool:
    """Is this signal one leg of an all-or-nothing set?"""
    if sig.get("arb"):
        return True
    try:
        return bool(json.loads(sig.get("meta") or "{}").get("arb"))
    except (TypeError, ValueError):
        return False


def snapshot_history(ledger, venue: str, market: str, since_ts: float) -> list:
    """[(ts, yes price)] from the runner's book snapshots: the paper price path for venues with no
    public price history (Polymarket US). Mid when both sides exist, else last."""
    out = []
    for r in ledger.snapshots(venue, market, since_ts):
        p = r.get("mid") if r.get("mid") is not None else r.get("last")
        if p is not None:
            out.append((float(r["ts"]), float(p)))
    return out


class PaperEngine:
    def __init__(self, ledger, history_fn=None, resolution_fn=None, now_fn=None):
        self.ledger = ledger
        self.history = history_fn or (lambda sig: offshore.prices_history(sig["market"], since_ts=sig["ts"] - 60, fidelity=1))
        self.resolve = resolution_fn or (lambda sig: offshore.market_resolution(json.loads(sig["meta"] or "{}").get("market_id", "")))
        self.now = now_fn or time.time

    def settle_open(self, venue: str = "offshore", log=print) -> dict:
        counts = {"filled": 0, "closed": 0, "expired": 0, "open": 0}
        # One resolution per MARKET, not per signal. A market's outcome is a property of the
        # market, and the same market appears once per set that touched it — on 2026-09-19 that
        # was 24 open legs across only 6 distinct miami/nyc markets, so settling asked the venue
        # the same six questions four times over. Against a five-per-window budget that is a
        # guaranteed rate limit, and it is exactly what happened:
        #
        #   09-20 17:21:04 settle: {'closed': 15, 'open': 14}
        #   09-20 17:21:04   us venue blind for 15s — RateLimitError
        #
        # Fifteen legs closed, then the limiter cut it off and left 14 — including four legs of a
        # set whose other two HAD closed, which reports a $52.70 arb as a $1.33 loss. An arb set
        # settles whole or it says nothing true at all.
        resolved = {}

        def resolve_once(sig):
            key = (sig["venue"], sig["market"])
            if key not in resolved:
                resolved[key] = self.resolve(sig)
            return resolved[key]

        for sig in self.ledger.open_signals(venue=venue):
            row = self.ledger.paper_row(sig["id"]) or {}
            try:
                hist = self.history(sig)
            except Exception as exc:
                log(f"  history error signal {sig['id']}: {exc}")
                continue
            category = sig.get("category") or "other"
            fill_ts, fill_px = row.get("filled_ts"), row.get("fill_price")
            if fill_ts is None:
                fill_ts, fill_px = fill_from_history(sig, hist)
                if fill_ts is None:
                    # unfilled past the horizon: the order would have been cancelled
                    if self.now() > sig["ts"] + (sig["horizon_h"] or 24) * 3600:
                        self.ledger.upsert_paper(sig["id"], status="unfilled", note="no fill within horizon")
                        self.ledger.set_signal_status(sig["id"], "unfilled")
                        counts["expired"] += 1
                    else:
                        counts["open"] += 1
                    continue
                self.ledger.upsert_paper(sig["id"], filled_ts=fill_ts, fill_price=fill_px, status="filled")
                counts["filled"] += 1
            exit_ts, exit_px, kind = exit_from_history(sig, fill_ts, hist)
            outcome = None
            if exit_ts is None:
                try:
                    outcome = resolve_once(sig)
                except Exception as exc:
                    log(f"  resolution error signal {sig['id']}: {exc}")
                if outcome is None:
                    mark = hist[-1][1] if hist else fill_px
                    if _is_arb(sig):
                        # An arb leg is not an independent position and must not be marked like
                        # one. The six legs are one instrument that pays exactly $1.00 a set at
                        # settlement; marking each at its own mid and adding them up prices the
                        # market's inefficiency SIX TIMES, and implies selling all six legs at mid
                        # simultaneously, which is not a thing you can do — you would hit bids.
                        #
                        # On 2026-09-19 that read $17.18 unrealised on sets whose settlement value
                        # is $8.70. Flattering by a factor of two, in the number the promotion
                        # gate reads. An arb's profit is locked at entry and realised at
                        # settlement, so carry it at cost until then and let the close book it.
                        # Carried flat, not at cost-minus-a-phantom-exit-fee: there is no exit.
                        # The set is held to settlement, where pnl_usd charges the entry fee once
                        # and books the $1.00 payout. Marking anything here would either invent a
                        # round trip that never happens or double-count the fee.
                        mark = fill_px
                        self.ledger.upsert_paper(sig["id"], mark_price=mark, pnl_usd=0.0,
                                                 status="filled")
                        counts["open"] += 1
                        continue
                    unreal, _ = pnl_usd(sig, fill_px, mark, None, venue, category)
                    self.ledger.upsert_paper(sig["id"], mark_price=mark, pnl_usd=unreal, status="filled")
                    counts["open"] += 1
                    continue
                exit_ts, kind = self.now(), "settle"
            pnl, fee = pnl_usd(sig, fill_px, exit_px, outcome, venue, category)
            self.ledger.upsert_paper(sig["id"], exit_ts=exit_ts, exit_price=exit_px, exit_kind=kind,
                                     outcome=outcome, pnl_usd=pnl, fees_usd=fee, status="closed")
            self.ledger.set_signal_status(sig["id"], "closed")
            counts["closed"] += 1
        return counts
