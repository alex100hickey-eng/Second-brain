"""Are Polymarket US twins priced differently from the same offshore question? Report only.

    python3 -m polybot.runner twins

The 2026-09-25 measurement, as a command, so the re-measure after hold_favorites' US signals settle
(9/27-9/30) is one line. Method: every recorded pair since the gate reset; quotes carried forward
per venue; a sample only when BOTH books are at most `max_spread` wide and both quotes are under
`max_skew_s` old; one sample per pair-hour (the last); per pair, the median of US mid minus
offshore mid in each band of the offshore price; per band, the median of those pair medians and
how many pairs sit above or below (half a cent either way). On 2026-09-25 (215 pairs): US
favourites (85-95c) -1.27c, US longshots (<10c) +0.95c, the middle flat.

The second half is what the gate will care about: hold_favorites' US signals that have SETTLED,
their entry against the offshore twin at the moment of entry, and what they paid.
"""
from __future__ import annotations

import collections
import json
import statistics
from datetime import datetime

BANDS = ((0.0, 0.10, "<10c"), (0.10, 0.50, "10-50c"), (0.50, 0.85, "50-85c"), (0.85, 0.95, "85-95c"),
         (0.95, 1.01, ">=95c"))
SETTLED_TARGET = 10          # the gate's min_us_signals: re-measure once this many have settled


def _band(p: float) -> str:
    return next(name for lo, hi, name in BANDS if lo <= p < hi)


def divergence(ledger, pairs: list, since_ts: float, max_spread: float = 0.03, max_skew_s: float = 1800.0) -> dict:
    by_us = {p["us_slug"]: p for p in pairs if p.get("category") != "sports"}
    by_off = {p["offshore_token"]: p["us_slug"] for p in by_us.values()}
    if not by_us:
        return {"pairs": 0, "bands": {}}
    q = ("SELECT ts, venue, market, bid, ask FROM snapshots WHERE ts >= ? AND ((venue='us' AND market IN (%s)) "
         "OR (venue='offshore' AND market IN (%s))) ORDER BY ts") % (",".join("?" * len(by_us)), ",".join("?" * len(by_off)))
    cur, samples = {}, collections.defaultdict(dict)
    for ts, venue, market, bid, ask in ledger.conn.execute(q, [since_ts, *by_us, *by_off]):
        slug = market if venue == "us" else by_off[market]
        cur[(venue, slug)] = (bid, ask, ts)
        u, o = cur.get(("us", slug)), cur.get(("offshore", slug))
        if not u or not o or None in (u[0], u[1], o[0], o[1]) or abs(u[2] - o[2]) > max_skew_s:
            continue
        if u[1] - u[0] > max_spread + 1e-9 or o[1] - o[0] > max_spread + 1e-9:
            continue
        samples[slug][int(ts // 3600)] = ((u[0] + u[1]) / 2, (o[0] + o[1]) / 2)
    per_band = collections.defaultdict(list)
    for slug, hours in samples.items():
        diffs = collections.defaultdict(list)
        for um, om in hours.values():
            diffs[_band(om)].append(um - om)
        for band, d in diffs.items():
            per_band[band].append(statistics.median(d))
    bands = {}
    for _, _, name in BANDS:
        d = per_band.get(name)
        if d:
            bands[name] = {"pairs": len(d), "median_cents": round(statistics.median(d) * 100, 2),
                           "us_higher": sum(x > 0.005 for x in d), "us_lower": sum(x < -0.005 for x in d)}
    return {"pairs": len(samples), "bands": bands}


def settled_us(ledger, pairs: list, since_ts: float) -> dict:
    """hold_favorites' settled US signals: entry vs the offshore twin at entry, and the result."""
    twin = {p["us_slug"]: p["offshore_token"] for p in pairs}
    rows = ledger.conn.execute(
        """SELECT s.id, s.ts, s.market, s.side, s.price, p.status, p.pnl_usd FROM signals s
           LEFT JOIN paper_trades p ON p.signal_id=s.id
           WHERE s.module='hold_favorites' AND s.venue='us' AND s.status!='void' AND s.ts>=? ORDER BY s.ts""",
        (since_ts,)).fetchall()
    out = {"signals": len(rows), "settled": 0, "won": 0, "net": 0.0, "gaps_cents": [], "list": []}
    for sid, ts, market, side, price, status, pnl in rows:
        if status != "closed":
            continue
        out["settled"] += 1
        out["won"] += int((pnl or 0) > 0)
        out["net"] += pnl or 0.0
        gap = None
        tok = twin.get(market)
        if tok:
            r = ledger.conn.execute("SELECT bid, ask FROM snapshots WHERE venue='offshore' AND market=? AND ts<=? "
                                    "ORDER BY ts DESC LIMIT 1", (tok, ts)).fetchone()
            if r and r[0] is not None and r[1] is not None:
                yes_entry = price if side == "BUY_YES" else 1 - price
                gap = round((yes_entry - (r[0] + r[1]) / 2) * 100, 1)
                out["gaps_cents"].append(gap)
        out["list"].append({"id": sid, "ts": ts, "market": market, "side": side, "price": price, "pnl": pnl,
                            "twin_gap_cents": gap})
    out["net"] = round(out["net"], 2)
    return out


def render(div: dict, st: dict) -> str:
    lines = [f"US vs offshore twins: {div['pairs']} pairs with tight books on both venues since the gate reset",
             "  band      pairs  US - offshore (median)  US higher / lower"]
    for _, _, name in BANDS:
        b = div["bands"].get(name)
        if b:
            lines.append(f"  {name:8} {b['pairs']:>6}  {b['median_cents']:>+10.2f}c            {b['us_higher']:>3} / {b['us_lower']}")
    lines.append(f"hold_favorites US: {st['settled']}/{SETTLED_TARGET} settled of {st['signals']} signals"
                 + (f" — won {st['won']}, net ${st['net']:+.2f}" if st["settled"] else ""))
    if st["gaps_cents"]:
        lines.append(f"  entry vs the offshore twin at entry (YES terms): median {statistics.median(st['gaps_cents']):+.1f}c "
                     f"over {len(st['gaps_cents'])} with a twin (negative = the US entry was cheaper)")
    if st["settled"] < SETTLED_TARGET:
        lines.append(f"  re-measure when {SETTLED_TARGET} have settled (next due: see `runner report`)")
    return "\n".join(lines)
