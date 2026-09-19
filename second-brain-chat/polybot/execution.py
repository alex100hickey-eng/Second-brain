"""Live order sync for Polymarket US: what happened to the orders we sent?

Runs every loop tick when the US venue is available. For each live signal with a sent order:
  - order no longer resting and a position exists → filled: record it, and for 'tp:' exits rest the
    take-profit sell right away (maker, one tick above entry + tp)
  - order still resting past the signal's horizon → cancel it, signal becomes 'unfilled'
  - kill switch on → cancel everything once
Field names on the SDK's order/position objects are read defensively (`_pick`); the first live day
will confirm them, and the log prints the keys it saw.
"""
from __future__ import annotations

import json
import time

from . import config, notify


def _pick(d: dict, *keys, default=None):
    for k in keys:
        if isinstance(d, dict) and d.get(k) not in (None, ""):
            return d[k]
    return default


def _id(o) -> str:
    return str(_pick(o, "id", "orderId", "order_id", default=""))


# None of these appear in a response that filled. Note "KILL" is NOT among the venue's own words:
# a killed fill-or-kill comes back as CANCELED or EXPIRED.
_DEAD_MARKERS = ("CANCEL", "REJECT", "EXPIR", "KILL", "DONE_FOR_DAY")


def _num(x) -> float:
    if isinstance(x, dict):
        x = x.get("value", x.get("quantity"))
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def fill_result(resp, want: int) -> tuple[int, str]:
    """(contracts actually executed, what the venue called it) from a create-order response.

    This is the check the whole all-or-nothing design rests on, and it was wrong in a way that
    could only ever bite with real money. The SDK's CreateOrderResponse is {id, executions[]} --
    there is NO top-level status or state; the outcome lives in executions[].type
    (EXECUTION_TYPE_FILL / CANCELED / REJECTED / EXPIRED) and executions[].order.state
    (ORDER_STATE_FILLED / CANCELED / ...). The old check read a top-level "status"/"state" that
    is never there, got an empty string, and looked in it for the word "KILL" -- which appears in
    none of the venue's enums anyway. Empty string contains no bad word, so EVERY order read as
    filled, including a killed one. A set would have been marked COMPLETE while holding nothing,
    or holding an unbalanced basket it would then never unwind.

    Ambiguity resolves to NOT filled. Believing a leg filled when it did not leaves an unhedged
    basket nobody unwinds; believing it did not when it did costs the spread and leaves us flat.
    """
    if not isinstance(resp, dict):
        return 0, "no order"
    states, done = [], 0.0
    for ex in (resp.get("executions") or []):
        if not isinstance(ex, dict):
            continue
        t = str(ex.get("type") or "").upper()
        if t:
            states.append(t)
        o = ex.get("order") if isinstance(ex.get("order"), dict) else {}
        st = str(o.get("state") or "").upper()
        if st:
            states.append(st)
        if "FILL" in t and not any(d in t for d in _DEAD_MARKERS):
            done += _num(ex.get("lastShares"))
        done = max(done, _num(o.get("cumQuantity")))
    # Gateways do not always match their own SDK types, so accept a plain statement too.
    top = str(_pick(resp, "state", "status", default="")).upper()
    if top:
        states.append(top)
    done = max(done, _num(resp.get("cumQuantity")))
    if not states and done == 0:
        return 0, "no state reported"
    label = ", ".join(dict.fromkeys(states))
    if done == 0 and any(d in st for st in states for d in _DEAD_MARKERS):
        return 0, label
    # "Filled" with no quantity anywhere: take it at its word. But NOT a partial -- PARTIALLY_FILLED
    # and PARTIAL_FILL both contain "FILL" and neither is a dead marker, so without this they would
    # be read as a complete fill, which is the precise error this whole function exists to prevent.
    if done == 0 and any("FILL" in st and "PARTIAL" not in st
                         and not any(d in st for d in _DEAD_MARKERS) for st in states):
        done = want
    return int(done), label


class Executor:
    def __init__(self, ledger, us, cfg: config.Config, log=print):
        self.ledger, self.us, self.cfg, self.log = ledger, us, cfg, log
        self._killed = False

    # ---- arb sets: all legs or none ------------------------------------------------------
    def place_arb_set(self, legs) -> dict:
        """Place every leg of an arb, or leave the account flat.

        `legs` is [(signal_id, sig), ...] for one `meta.group`. The set is only worth anything
        whole: six legs bought for 92c pay $1, but four of the six bought for 80c pay $1 only if
        the temperature lands in one of the four, which is a bet, not an arb — and a bet nobody
        sized or measured. So:

          1. every leg goes out FILL_OR_KILL, so a leg either fills completely at our price or
             does not exist. No partial fills, no resting remainder that fills later at a price
             that is no longer part of any arb.
          2. if any leg is killed, the legs that DID fill are sold straight back at the bid
             (immediate-or-cancel). That costs the spread on those legs — a known, small, bounded
             loss — instead of leaving an unhedged basket on the book.

        Returns {'placed': n, 'filled': n, 'unwound': n, 'ok': bool}.
        """
        out = {"placed": 0, "filled": 0, "unwound": 0, "ok": False}
        if not self.us.available:
            self.log("  arb set: venue unavailable, nothing sent")
            return out
        # Can we actually pay for this? The bankroll the caps are measured against is account
        # VALUE — cash plus whatever is sitting in open positions — because a bot that halts the
        # moment its money is working never compounds. That is right for the floor and wrong here:
        # value you cannot spend does not fill an order.
        #
        # On 2026-09-19 the account read $272.50 of value and $0.06 of buying power, the rest
        # being two positions Alex had opened himself. Every leg of a set would have been refused
        # for insufficient funds, one at a time, and the first ones to fill would have had to be
        # unwound. Ask once, before committing to anything.
        need = sum(getattr(sig, "size_usd", None) or (sig.price * sig.contracts) for _, sig in legs)
        try:
            cash = self.us.balance_usd()
        except Exception as exc:
            self.log(f"  arb set: could not read buying power ({exc}) — proceeding on the caps")
            cash = None
        if cash is not None and cash + 1e-9 < need:
            self.log(f"  arb set: buying power ${cash:.2f} < set cost ${need:.2f} — nothing sent "
                     f"(account value is not spendable cash)")
            out["reason"] = "insufficient buying power"
            return out
        # Thinnest leg first. The legs go out one at a time -- the venue has no atomic multi-leg
        # order -- so whichever leg is going to be killed decides how much unwinding we pay for.
        # Killed on the first leg costs nothing; killed on the fifth means selling four legs back
        # at the bid. The leg most likely to be killed is the one whose ladder barely covers the
        # order, and that is usually the one the whole set was sized on: chicago on 2026-09-18 had
        # five legs holding thousands of contracts and a binding leg holding exactly 21.
        legs = sorted(legs, key=lambda sl: (sl[1].meta or {}).get("depth", float("inf")))
        filled = []
        for idx, (sid, sig) in enumerate(legs):
            try:
                order = self.us.place_limit(sig.market, sig.side, sig.price, sig.contracts, tif="fok")
                out["placed"] += 1
            except Exception as exc:
                self.ledger.add_order(sid, "us", sig.market, sig.side, sig.price, sig.contracts, f"error: {exc}")
                self.log(f"  arb leg {sig.label}: ORDER ERROR {exc}")
                order = None
            qty, status = fill_result(order, sig.contracts)
            got = qty >= sig.contracts
            self.ledger.add_order(sid, "us", sig.market, sig.side, sig.price, sig.contracts,
                                  "filled" if got else ("partial" if qty else "killed"),
                                  venue_order_id=_id(order or {}), raw=order)
            if got:
                filled.append((sid, sig, sig.contracts))
                out["filled"] += 1
            else:
                self.log(f"  arb leg {sig.label}: {'PARTIAL ' + str(qty) if qty else 'killed'} "
                         f"of {sig.contracts} ({status})")
                if qty:
                    # A fill-or-kill should never part-fill. If the venue does it anyway we own
                    # those contracts, so they have to be unwound with the rest rather than
                    # forgotten because the leg "failed" — and the ledger must not call a leg we
                    # are actually holding "unfilled", or exposure and held_contracts both lie.
                    filled.append((sid, sig, qty))
                else:
                    self.ledger.set_signal_status(sid, "unfilled")
                # Stop the moment a leg fails. The set cannot complete without it, so every
                # further order buys a leg we are about to sell straight back at the spread —
                # which is also what made "thinnest leg first" worth doing: a kill on the leg
                # most likely to fail should cost NOTHING, and it only does if we stop here.
                # Without this the ordering bought five legs and unwound all five.
                for rest_sid, rest_sig in legs[idx + 1:]:
                    self.ledger.set_signal_status(rest_sid, "unfilled")
                    self.log(f"  arb leg {rest_sig.label}: not sent — set already failed")
                break
        if out["filled"] == len(legs):
            for sid, sig in legs:
                self.ledger.upsert_paper(sid, filled_ts=time.time(), fill_price=sig.price, status="filled",
                                         note="live arb leg")
            out["ok"] = True
            self.log(f"  arb set COMPLETE: {len(legs)} legs")
            return out
        # Partial. Unwind what filled, at whatever the bid is now.
        #
        # Everything here is about ending FLAT and knowing whether we did. An unwind that is only
        # *sent* proves nothing: an immediate-or-cancel order is killed exactly like the
        # fill-or-kill that started this, so "unwound" must mean the contracts actually left, not
        # that a request was made. And a leg we could not sell is real money sitting in an
        # unhedged position — the one outcome this whole design exists to prevent — so it cannot
        # be a log line nobody reads.
        self.log(f"  arb set INCOMPLETE ({out['filled']}/{len(legs)} legs) — unwinding")
        stranded = []
        for sid, sig, qty in filled:
            exit_side = "SELL_YES" if sig.side == "BUY_YES" else "SELL_NO"
            bid, ask = self.us.bbo(sig.market)
            px = bid if sig.side == "BUY_YES" else (None if ask is None else round(1 - ask, 2))
            if px is None:
                stranded.append((sid, sig, qty, "no bid to sell into"))
                continue
            try:
                # `qty`, not sig.contracts: on a part-filled leg we own only what filled, and
                # selling the full order size would turn an unwind into a naked short.
                o = self.us.place_limit(sig.market, exit_side, px, qty, tif="ioc")
            except Exception as exc:
                self.ledger.add_order(sid, "us", sig.market, exit_side, px, qty, f"unwind error: {exc}")
                stranded.append((sid, sig, qty, str(exc)[:80]))
                continue
            sold, why = fill_result(o, qty)
            self.ledger.add_order(sid, "us", sig.market, exit_side, px, qty,
                                  "unwind" if sold >= qty else ("unwind partial" if sold else "unwind killed"),
                                  venue_order_id=_id(o or {}), raw=o)
            if sold >= qty:
                self.ledger.set_signal_status(sid, "closed")      # flat: stop counting exposure
                out["unwound"] += 1
            else:
                stranded.append((sid, sig, qty - sold, f"unwind {why}"))

        if stranded:
            # Leave these OPEN on purpose. They are positions we hold, and the ledger has to keep
            # saying so or exposure, held_contracts and every later sizing decision are all wrong.
            held = sum(s.price * q for _, s, q, _ in stranded)
            lines = "; ".join(f"{s.label} {q}@{s.price}" for _, s, q, _ in stranded)
            self.log(f"  arb set STRANDED {len(stranded)} leg(s), ~${held:.2f} unhedged: {lines}")
            for _, _, _, why in stranded:
                self.log(f"    reason: {why}")
            out["stranded"] = len(stranded)
            notify.nudge(
                f"polybot: {len(stranded)} arb leg(s) STRANDED, ~${held:.2f} unhedged",
                f"A set failed and these could not be sold back: {lines}. "
                f"This is an unhedged position, not an arb — it needs a human.",
                key="polybot-arb-stranded", log=self.log)
        return out

    def sync(self) -> dict:
        counts = {"filled": 0, "cancelled": 0, "tp_placed": 0, "open": 0}
        if not self.us.available:
            return counts
        if config.kill_switch_on():
            if not self._killed:
                self.us.cancel_all()
                self._killed = True
                self.log("kill switch: cancel_all sent")
            return counts
        self._killed = False
        try:
            resting = {_id(o): o for o in (self.us.open_orders() or [])}
            positions = self.us.positions() or []
        except Exception as exc:
            self.log(f"  sync: venue read failed: {exc}")
            return counts
        pos_by_market = {}
        for p in positions:
            slug = _pick(p, "marketSlug", "market_slug", "slug", "market", default="")
            if slug:
                pos_by_market[slug] = p
        now = time.time()
        for sig in self.ledger.open_signals(venue="us"):
            if sig["mode"] != "live":
                continue
            order = self.ledger.last_order(sig["id"])
            if not order or order["status"] not in ("sent",):
                counts["open"] += 1
                continue
            oid = order["venue_order_id"] or ""
            if oid in resting:
                if now > sig["ts"] + (sig["horizon_h"] or 24) * 3600:
                    try:
                        self.us.cancel(oid, sig["market"])
                        self.ledger.set_order_status(order["id"], "cancelled")
                        self.ledger.set_signal_status(sig["id"], "unfilled")
                        counts["cancelled"] += 1
                    except Exception as exc:
                        self.log(f"  cancel {oid}: {exc}")
                else:
                    counts["open"] += 1
                continue
            # not resting any more: filled (a position exists) or gone
            pos = pos_by_market.get(sig["market"])
            if pos is None:
                self.ledger.set_order_status(order["id"], "gone")
                self.ledger.set_signal_status(sig["id"], "unfilled")
                counts["cancelled"] += 1
                continue
            self.ledger.set_order_status(order["id"], "filled")
            self.ledger.upsert_paper(sig["id"], filled_ts=now, fill_price=sig["price"], status="filled",
                                     note=f"live fill; position keys {sorted(pos.keys())[:8]}")
            counts["filled"] += 1
            rule = sig["exit_rule"] or "settle"
            if rule.startswith("tp:"):
                tp = float(rule.split(":", 1)[1])
                exit_side = "SELL_YES" if sig["side"] == "BUY_YES" else "SELL_NO"
                exit_price = round(sig["price"] + tp, 2)
                try:
                    o = self.us.place_limit(sig["market"], exit_side, exit_price, sig["contracts"])
                    self.ledger.add_order(sig["id"], "us", sig["market"], exit_side, exit_price, sig["contracts"],
                                          "sent-tp", venue_order_id=_id(o), raw=o)
                    counts["tp_placed"] += 1
                except Exception as exc:
                    self.log(f"  take-profit for signal {sig['id']} failed: {exc}")
        return counts
