"""Replay the recorded pair books through leadlag's own rule under reference A and reference C.

    python3 -m polybot.runner leadlag-refs [--days N]      # default: since the gate reset

The decision it serves: at 20 closed leadlag positions (the report counts them), choose the
reference on P&L. A is `leadlag_reference: "mid"`, the live rule; C is `"tight_mid"`, the mid updated
only while the offshore spread is <= `leadlag_ref_max_spread` (design doc, Sep 23 night). Only the
reference differs between the two runs: same thresholds, window, spread check, 3 h dedupe, and the
paper engine's own fill and 6 h `reference` exit. Read-only on the ledger. Report only.

Recorder semantics: rows are change-only plus a heartbeat, and every row of one tick shares a ts, so
each distinct ts is a tick; quotes carry forward between ticks; a gap over `gap_s` is an outage and
the paths restart, as the live recorder's would, since nothing is sampled while blind.
"""
from __future__ import annotations

import collections
import sqlite3
from datetime import datetime

from . import config, paper
from .strategies.leadlag import leadlag_signal, noise_signal

GAP_S = 150.0
DEDUPE_S = 3 * 3600
HORIZON_H = 6.0
DEFINITIONS = {"A": "mid", "C": "tight_mid"}


def _ref(defn, bid, ask, state, max_spread):
    if bid is None or ask is None:
        return None
    if defn == "A":
        return (bid + ask) / 2
    if ask - bid <= max_spread + 1e-9:
        state["last"] = (bid + ask) / 2
    return state.get("last")


def _one_sided(qs, move_cents):
    """Did the mid move because ONE side jumped while the other sat still (a pulled offer or bid)?"""
    (b0, a0), (b1, a1) = qs[0], qs[-1]
    db_, da = abs(b1 - b0) * 100, abs(a1 - a0) * 100
    return (db_ < 1.0 <= da and da >= move_cents) or (da < 1.0 <= db_ and db_ >= move_cents)


def replay(db_path: str, pairs: list, since_ts: float, until_ts: float | None = None, cfg=None,
           gap_s: float = GAP_S) -> dict:
    cfg = cfg or config.Config()
    W, MOVE, FOLLOW = cfg.leadlag_window_s, cfg.leadlag_move_cents, cfg.leadlag_follow_ratio
    size = cfg.caps.max_per_market_usd / 2
    pairs = {p["us_slug"]: p for p in pairs
             if p.get("category") != "sports" or cfg.caps.sports_enabled}
    off_tokens = {p["offshore_token"] for p in pairs.values()}
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    db.execute("pragma busy_timeout=5000")
    rows = db.execute("SELECT ts, venue, market, bid, ask FROM snapshots WHERE ts >= ? AND ts <= ? ORDER BY ts",
                      (since_ts, until_ts or 9e18)).fetchall()
    db.close()
    rows = [r for r in rows if (r[1] == "us" and r[2] in pairs) or (r[1] == "offshore" and r[2] in off_tokens)]
    ticks = collections.OrderedDict()
    for ts, v, m, b, a in rows:
        ticks.setdefault(round(ts, 3), []).append((v, m, b, a))
    us_hist = collections.defaultdict(list)
    for ts, v, m, b, a in rows:
        if v == "us" and b is not None and a is not None:
            us_hist[m].append((ts, (b + a) / 2))
    out = {"ticks": len(ticks), "from": rows[0][0] if rows else None, "to": rows[-1][0] if rows else None}
    for defn in DEFINITIONS:
        cur, last_tick, sigs, last_sig = {}, None, [], {}
        series = collections.defaultdict(collections.deque)
        quotes = collections.defaultdict(collections.deque)
        cstate = collections.defaultdict(dict)
        for ts, changes in ticks.items():
            if last_tick is not None and ts - last_tick > gap_s:
                series.clear()
                quotes.clear()
            last_tick = ts
            for v, m, b, a in changes:
                cur[(v, m)] = (b, a)
            for slug, p in pairs.items():
                u, o = cur.get(("us", slug)), cur.get(("offshore", p["offshore_token"]))
                if not u or not o or u[0] is None or u[1] is None:
                    continue
                r = _ref(defn, o[0], o[1], cstate[slug], cfg.leadlag_ref_max_spread)
                if r is None:
                    continue
                for key, val, q in ((("ref", slug), r, o), (("us", slug), (u[0] + u[1]) / 2, u)):
                    s = series[key]
                    s.append((ts, val))
                    quotes[key].append((ts, q))
                    while s and s[0][0] < ts - 900:
                        s.popleft()
                        quotes[key].popleft()
                ref, tgt = list(series[("ref", slug)]), list(series[("us", slug)])
                hit, kind = leadlag_signal(ref, tgt, W, MOVE, FOLLOW), "leadlag"
                if hit is None:
                    hit, kind = noise_signal(ref, tgt, W, MOVE), "noise"
                if hit is None:
                    continue
                side, ref_now, tgt_now, gap = hit
                bid, ask = u
                spread = round((ask - bid) * 100, 1)
                if abs(gap) <= spread + 1.0 or ts - last_sig.get((slug, side), -1e18) < DEDUPE_S:
                    continue
                last_sig[(slug, side)] = ts
                price = round(bid + 0.01, 2) if side == "BUY_YES" else round(1 - ask + 0.01, 2)
                moved = ("ref", slug) if kind == "leadlag" else ("us", slug)
                win = [q for t, q in quotes[moved] if t >= ts - W]
                sigs.append({"ts": ts, "slug": slug, "label": p.get("label", slug), "side": side, "price": price,
                             "kind": kind, "gap": gap, "spread": spread,
                             "artifact": len(win) >= 2 and _one_sided(win, MOVE), "category": p.get("category", "other")})
        net_closed = net_open = 0.0
        filled = closed = 0
        for sg in sigs:
            d = {"ts": sg["ts"], "price": sg["price"], "side": sg["side"], "exit_rule": "reference",
                 "horizon_h": HORIZON_H, "contracts": int(size / sg["price"] + 1e-9), "taker": False, "meta": "{}"}
            hist = [(t, px) for t, px in us_hist[sg["slug"]] if t >= sg["ts"] - 60]
            f_ts, f_px = paper.fill_from_history(d, hist)
            sg["filled"] = f_ts is not None
            if f_ts is None:
                continue
            filled += 1
            e_ts, e_px, _ = paper.exit_from_history(d, f_ts, hist)
            if e_ts is not None:
                pnl, _ = paper.pnl_usd(d, f_px, e_px, None, "us", sg["category"])
                closed += 1
                net_closed += pnl
                sg["pnl"] = round(pnl, 2)
            else:
                pnl, _ = paper.pnl_usd(d, f_px, hist[-1][1] if hist else f_px, None, "us", sg["category"])
                net_open += pnl
                sg["mark_pnl"] = round(pnl, 2)
        out[defn] = {"signals": len(sigs), "artifacts": sum(s["artifact"] for s in sigs), "filled": filled,
                     "closed": closed, "net_closed": round(net_closed, 2), "net_open_marked": round(net_open, 2),
                     "list": sigs}
    return out


def render(res: dict) -> str:
    f = lambda t: datetime.fromtimestamp(t).strftime("%m-%d %H:%M") if t else "?"
    lines = [f"leadlag reference replay: {res['ticks']} ticks, {f(res['from'])} to {f(res['to'])}",
             "  ref  signals  artifacts  filled  closed  net closed  open (marked)"]
    for defn, name in DEFINITIONS.items():
        r = res[defn]
        lines.append(f"  {defn} {name:9} {r['signals']:>4} {r['artifacts']:>9} {r['filled']:>7} {r['closed']:>7} "
                     f"{r['net_closed']:>+11.2f} {r['net_open_marked']:>+13.2f}")
    # A few sub-5c fills on a one-print US book (09-24 08:31, a 0.98 bid on a ~45% BoC market: +$274
    # at $10 a position) are worth more than everything else together and hit both references the
    # same way, so the comparison is also shown without each side's two largest wins.
    trimmed = {d: sum(sorted((s["pnl"] for s in res[d]["list"] if "pnl" in s), reverse=True)[2:])
               for d in DEFINITIONS}
    lines.append("  net closed without each side's two largest wins: "
                 + ", ".join(f"{d} {v:+.2f}" for d, v in trimmed.items()))
    a, c = res["A"], res["C"]
    only_a = {(s["slug"], s["side"], round(s["ts"])) for s in a["list"]} - {(s["slug"], s["side"], round(s["ts"])) for s in c["list"]}
    lines.append(f"  A signals C would not have taken: {len(only_a)} "
                 f"({sum(1 for s in a['list'] if (s['slug'], s['side'], round(s['ts'])) in only_a and s['artifact'])} of them one-sided artifacts)")
    lines.append("  flip to C: set \"leadlag_reference\": \"tight_mid\" in config.json (the loop re-reads it within a minute)")
    return "\n".join(lines)
