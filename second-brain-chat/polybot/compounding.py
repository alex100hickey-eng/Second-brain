"""Caps that grow with the bankroll, but only from realized live profit. OFF by default.

Every dollar cap is expressed as a fraction of a BASIS, and the fractions are the configured dollars
over `compounding_base_usd` ($200), so the day the switch is thrown every cap still reads the dollars
config.json holds. The basis is not the account value: that includes the promo credit ($174.48 on
2026-09-24) and any deposit, and the roadmap's rule is "never funded from outside money". It is:

    basis = base + realized live profit banked at scale-up checkpoints + any loss since the last one

  - LOSSES count at once: a drawdown shrinks every cap the same day.
  - PROFIT counts only at a checkpoint, at most every `scale_step_days` (14), and only when the live
    record since the last checkpoint is "consistent live profit" (defaults, all in config):
        >= 14 live days · net > 0 after fees · max drawdown < 10% of the basis · fills >= 50%.
  - Nothing is ever raised from paper results.
State (the banked basis and the last checkpoint) lives in compounding-state.json.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from . import config

STATE_PATH = os.path.join(config.ROOT, "compounding-state.json")
CAPS_FIELDS = ("max_per_market_usd", "max_exposure_usd", "daily_loss_stop_usd", "bankroll_floor_usd")
CFG_FIELDS = ("arb_max_set_cost_usd", "arb_max_risk_usd")
ET = ZoneInfo("America/New_York")


def dollars(cfg) -> dict:
    """The configured dollar caps (captured once per loaded config, before any scaling)."""
    if not hasattr(cfg, "_compounding_dollars"):
        d = {f: float(getattr(cfg.caps, f)) for f in CAPS_FIELDS}
        d.update({f: float(getattr(cfg, f)) for f in CFG_FIELDS})
        cfg._compounding_dollars = d
    return cfg._compounding_dollars


def fractions(cfg) -> dict:
    base = float(cfg.compounding_base_usd)
    return {k: v / base for k, v in dollars(cfg).items()}


def live_record(ledger, since_ts: float) -> dict:
    """What live trading did since `since_ts`: days, net after fees, max drawdown, fill rate."""
    rows = ledger.conn.execute(
        """SELECT s.ts, p.status, p.filled_ts, p.exit_ts, p.pnl_usd FROM signals s
           LEFT JOIN paper_trades p ON p.signal_id=s.id
           WHERE s.mode='live' AND s.status!='void' AND s.ts>=? ORDER BY s.ts""", (since_ts,)).fetchall()
    days = {datetime.fromtimestamp(r[0], ET).date() for r in rows}
    filled = sum(1 for r in rows if r[2] is not None)
    closed = sorted((r[3] or r[0], r[4] or 0.0) for r in rows if r[1] == "closed")
    net = peak = dd = run = 0.0
    for _, pnl in closed:
        run += pnl
        peak = max(peak, run)
        dd = max(dd, peak - run)
    net = run
    return {"live_days": len(days), "signals": len(rows), "fill_rate": filled / len(rows) if rows else 0.0,
            "net": round(net, 2), "max_drawdown": round(dd, 2)}


def consistent(record: dict, basis: float, cfg) -> tuple[bool, str]:
    """(ok, why) — is this live record consistent profit, by the numbers in config?"""
    if record["live_days"] < cfg.scale_min_live_days:
        return False, f"{record['live_days']}/{cfg.scale_min_live_days} live days"
    if record["net"] <= 0:
        return False, f"net {record['net']:+.2f} after fees"
    if record["max_drawdown"] >= cfg.scale_max_drawdown_frac * basis:
        return False, (f"max drawdown ${record['max_drawdown']:.2f} >= "
                       f"{cfg.scale_max_drawdown_frac:.0%} of ${basis:.2f}")
    if record["fill_rate"] < cfg.scale_min_fill_rate:
        return False, f"fills {record['fill_rate']:.0%} < {cfg.scale_min_fill_rate:.0%}"
    return True, (f"{record['live_days']} live days, net {record['net']:+.2f}, max drawdown "
                  f"${record['max_drawdown']:.2f}, fills {record['fill_rate']:.0%}")


def load_state(path: str = STATE_PATH) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(state: dict, path: str = STATE_PATH) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, path)


def apply(cfg, ledger, now: float | None = None, path: str = STATE_PATH, persist: bool = True) -> dict:
    """Set every cap from basis x fraction when compounding is on; do nothing when it is off."""
    now = time.time() if now is None else now
    if not getattr(cfg, "compounding", False):
        return {"on": False}
    state = load_state(path)
    base = float(cfg.compounding_base_usd)
    banked = float(state.get("banked_usd", base))
    since = float(state.get("since_ts", now))
    if "since_ts" not in state:
        state = {"banked_usd": banked, "since_ts": since, "steps": []}
    rec = live_record(ledger, since)
    stepped = None
    if now - since >= cfg.scale_step_days * 86400:
        ok, why = consistent(rec, banked, cfg)
        if ok:
            stepped = {"ts": now, "from": banked, "to": round(banked + rec["net"], 2), "why": why}
            banked = round(banked + rec["net"], 2)
            state["steps"] = (state.get("steps") or []) + [stepped]
        state.update(banked_usd=banked, since_ts=now)       # a checkpoint either way: the next window starts
        since, rec = now, live_record(ledger, now)
    basis = banked + min(0.0, rec["net"])                   # a loss counts today, a gain waits
    for k, frac in fractions(cfg).items():
        v = round(frac * basis, 2)
        if k in CAPS_FIELDS:
            setattr(cfg.caps, k, v)
        else:
            setattr(cfg, k, v)
    if persist:
        save_state(state, path)
    return {"on": True, "basis": round(basis, 2), "banked": banked, "since_ts": since, "record": rec,
            "stepped": stepped, "next_check_days": max(0.0, (since + cfg.scale_step_days * 86400 - now) / 86400)}


def describe(info: dict, cfg) -> str:
    if not info.get("on"):
        return "compounding: off — caps are the fixed dollars in config.json"
    rec = info["record"]
    return (f"compounding: ON — basis ${info['basis']:.2f} (banked ${info['banked']:.2f}); caps "
            f"${cfg.caps.max_per_market_usd:.2f}/market, ${cfg.caps.max_exposure_usd:.2f} total, "
            f"${cfg.caps.daily_loss_stop_usd:.2f} daily stop; since the last checkpoint {rec['live_days']} live "
            f"days, net {rec['net']:+.2f}, max drawdown ${rec['max_drawdown']:.2f}; next check in "
            f"{info['next_check_days']:.0f}d" + (f" — STEPPED to ${info['stepped']['to']:.2f}" if info.get("stepped") else ""))
