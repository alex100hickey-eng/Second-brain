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
