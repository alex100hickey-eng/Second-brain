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

from . import config


def _pick(d: dict, *keys, default=None):
    for k in keys:
        if isinstance(d, dict) and d.get(k) not in (None, ""):
            return d[k]
    return default


def _id(o) -> str:
    return str(_pick(o, "id", "orderId", "order_id", default=""))


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
        # Thinnest leg first. The legs go out one at a time -- the venue has no atomic multi-leg
        # order -- so whichever leg is going to be killed decides how much unwinding we pay for.
        # Killed on the first leg costs nothing; killed on the fifth means selling four legs back
        # at the bid. The leg most likely to be killed is the one whose ladder barely covers the
        # order, and that is usually the one the whole set was sized on: chicago on 2026-09-18 had
        # five legs holding thousands of contracts and a binding leg holding exactly 21.
        legs = sorted(legs, key=lambda sl: (sl[1].meta or {}).get("depth", float("inf")))
        filled = []
        for sid, sig in legs:
            try:
                order = self.us.place_limit(sig.market, sig.side, sig.price, sig.contracts, tif="fok")
                out["placed"] += 1
            except Exception as exc:
                self.ledger.add_order(sid, "us", sig.market, sig.side, sig.price, sig.contracts, f"error: {exc}")
                self.log(f"  arb leg {sig.label}: ORDER ERROR {exc}")
                order = None
            status = str(_pick(order or {}, "status", "state", default="")).upper()
            got = bool(order) and "KILL" not in status and "CANCEL" not in status and "REJECT" not in status
            self.ledger.add_order(sid, "us", sig.market, sig.side, sig.price, sig.contracts,
                                  "filled" if got else "killed", venue_order_id=_id(order or {}), raw=order)
            if got:
                filled.append((sid, sig))
                out["filled"] += 1
            else:
                self.ledger.set_signal_status(sid, "unfilled")
                self.log(f"  arb leg {sig.label}: killed (status {status or 'no order'})")
        if out["filled"] == len(legs):
            for sid, sig in legs:
                self.ledger.upsert_paper(sid, filled_ts=time.time(), fill_price=sig.price, status="filled",
                                         note="live arb leg")
            out["ok"] = True
            self.log(f"  arb set COMPLETE: {len(legs)} legs")
            return out
        # Partial. Unwind what filled, at whatever the bid is now.
        self.log(f"  arb set INCOMPLETE ({out['filled']}/{len(legs)} legs) — unwinding")
        for sid, sig in filled:
            exit_side = "SELL_YES" if sig.side == "BUY_YES" else "SELL_NO"
            bid, ask = self.us.bbo(sig.market)
            px = bid if sig.side == "BUY_YES" else (None if ask is None else round(1 - ask, 2))
            if px is None:
                self.log(f"  arb unwind {sig.label}: NO BID — leg left open, needs a human")
                continue
            try:
                o = self.us.place_limit(sig.market, exit_side, px, sig.contracts, tif="ioc")
                self.ledger.add_order(sid, "us", sig.market, exit_side, px, sig.contracts, "unwind", raw=o)
                out["unwound"] += 1
            except Exception as exc:
                self.log(f"  arb unwind {sig.label} FAILED: {exc} — leg left open, needs a human")
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
