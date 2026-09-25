"""Replay every leadlag signal since the gate reset under both paper fill models. Report only.

    python3 -m polybot.runner leadlag-fills

"mid" is the live model (`fill_from_history`: the snapshot mid reached our level). "book_tick" is
the stricter one behind `leadlag_fill_model` (`fill_from_book`: the book offered at our level for
a full recorder tick). Same signals, same 6 h `reference` exit on the mid, same fees; only the fill
decision differs, and both only count a fill inside the order's life (signal + horizon). The fill
rate is the gate's: filled over every signal, pending ones included. Read-only on the ledger.
"""
from __future__ import annotations

import time
from datetime import datetime

from . import paper

MODELS = ("mid", "book_tick")


def _sig(row) -> dict:
    return {k: row[k] for k in ("id", "ts", "module", "venue", "market", "label", "side", "price", "contracts",
                                "exit_rule", "horizon_h", "category", "meta")}


def replay(ledger, since_ts: float, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    rows = ledger.conn.execute(
        "SELECT * FROM signals WHERE module='leadlag' AND venue='us' AND status!='void' AND ts>=? ORDER BY ts",
        (since_ts,)).fetchall()
    out = {m: {"signals": 0, "filled": 0, "closed": 0, "unfilled": 0, "net_closed": 0.0, "open_marked": 0.0}
           for m in MODELS}
    per = []
    for r in rows:
        sig = _sig(r)
        hist = paper.snapshot_history(ledger, "us", sig["market"], sig["ts"] - 60)
        quotes = [(q["ts"], q["bid"], q["ask"]) for q in ledger.snapshots("us", sig["market"], sig["ts"] - 3600)]
        horizon_end = sig["ts"] + (sig["horizon_h"] or 6) * 3600
        got = {}
        for m in MODELS:
            # Both models get the order's real life: a crossing after the horizon met a cancelled
            # order. (The live engine only marks "unfilled" when a settle runs after the horizon
            # and finds nothing, so a replay run days later must cut the history itself.)
            f_ts, f_px = (paper.fill_from_history(sig, [(t, p) for t, p in hist if t <= horizon_end]) if m == "mid"
                          else paper.fill_from_book(sig, quotes, until=horizon_end))
            o = out[m]
            o["signals"] += 1
            if f_ts is None:
                if now > horizon_end:
                    o["unfilled"] += 1
                got[m] = None
                continue
            o["filled"] += 1
            e_ts, e_px, _ = paper.exit_from_history(sig, f_ts, hist)
            if e_ts is not None:
                pnl, _ = paper.pnl_usd(sig, f_px, e_px, None, "us", sig["category"] or "other")
                o["closed"] += 1
                o["net_closed"] += pnl
            else:
                pnl, _ = paper.pnl_usd(sig, f_px, hist[-1][1] if hist else f_px, None, "us", sig["category"] or "other")
                o["open_marked"] += pnl
            got[m] = (round(f_ts - sig["ts"]), round(pnl, 2), e_ts is not None)
        per.append({"id": sig["id"], "ts": sig["ts"], "label": sig["label"], "side": sig["side"],
                    "price": sig["price"], **got})
    for o in out.values():
        o["fill_rate"] = o["filled"] / o["signals"] if o["signals"] else 0.0
        o["net_closed"], o["open_marked"] = round(o["net_closed"], 2), round(o["open_marked"], 2)
    out["list"] = per
    return out


def render(res: dict) -> str:
    lines = ["leadlag fill models, every US signal since the gate reset (the gate needs fills >= 50%):",
             "  model      signals  filled  fill rate  closed  net closed  open (marked)"]
    for m in MODELS:
        r = res[m]
        lines.append(f"  {m:10} {r['signals']:>7} {r['filled']:>7} {r['fill_rate']:>9.0%} {r['closed']:>7} "
                     f"{r['net_closed']:>+11.2f} {r['open_marked']:>+14.2f}")
    diff = [p for p in res["list"] if (p["mid"] is None) != (p["book_tick"] is None)]
    lines.append(f"  signals the models disagree on: {len(diff)}")
    for p in diff[:12]:
        when = datetime.fromtimestamp(p["ts"]).strftime("%m-%d %H:%M")
        lines.append(f"    #{p['id']} {when} {p['side']} @{p['price']:.2f} — mid: "
                     f"{'filled +' + str(p['mid'][0]) + 's, ' + format(p['mid'][1], '+.2f') if p['mid'] else 'unfilled'}; "
                     f"book_tick: {'filled' if p['book_tick'] else 'unfilled'} — {(p['label'] or '')[:50]}")
    lines.append("  to switch (Alex's call, it changes the gate's evidence): \"leadlag_fill_model\": \"book_tick\" in config.json")
    return "\n".join(lines)
