"""Calibration table: for closed offshore markets, how often did a YES priced in band X (measured a
day before resolution) actually resolve YES, by category? This is the reference behind
hold_favorites. It measures the favorite-longshot bias ourselves instead of trusting a blog.

Table shape: {category: {band_key: {"n": int, "yes": int}}}, band_key = "0.85-0.90".
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone

from . import config
from .feeds import offshore

BANDS = [(i / 20, (i + 1) / 20) for i in range(20)]  # 5-cent bands


def band_key(price: float) -> str:
    for lo, hi in BANDS:
        if lo <= price < hi or (hi == 1.0 and price == 1.0):
            return f"{lo:.2f}-{hi:.2f}"
    return "n/a"


def price_before(history, end_ts: float, hours: float = 24.0):
    """Last traded price at least `hours` before resolution time, from a [(t, p)] series."""
    cutoff = end_ts - hours * 3600
    prior = [p for t, p in history if t <= cutoff]
    return prior[-1] if prior else None


def resolution_ts(history, extreme: float = 0.03):
    """When did the market effectively resolve? The first timestamp after which every trade stayed
    within `extreme` of 0 or 1. Gamma's endDate is often a nominal date long after the real
    resolution, which is why the first build sampled prices that were already 0 or 1."""
    if not history:
        return None
    last_p = history[-1][1]
    if not (last_p <= extreme or last_p >= 1 - extreme):
        return None
    hi = last_p >= 1 - extreme
    res = None
    for t, p in reversed(history):
        settled = (p >= 1 - extreme) if hi else (p <= extreme)
        if settled:
            res = t
        else:
            break
    return res


def add_sample(table: dict, category: str, price: float, outcome: int) -> None:
    cell = table.setdefault(category, {}).setdefault(band_key(price), {"n": 0, "yes": 0})
    cell["n"] += 1
    cell["yes"] += int(outcome)


def lookup(table: dict, price: float, category: str, min_n: int = 25, shrink: int = 20):
    """Realized YES frequency for this band, shrunk toward the price with `shrink` pseudo-samples;
    falls back to the all-category cell; None when both are too thin."""
    key = band_key(price)
    cell = table.get(category, {}).get(key) or table.get("all", {}).get(key)
    if not cell or cell["n"] < min_n:
        return None
    return (cell["yes"] + shrink * price) / (cell["n"] + shrink)


def build(max_events: int = 300, sleep_s: float = 0.15, hours_before: float = 24.0, log=print) -> dict:
    """Pull recently closed events, sample each resolved market's price a day before its end."""
    table: dict = {}
    seen = 0
    offset = 0
    while seen < max_events:
        events = offshore.closed_events(limit=100, offset=offset)
        if not events:
            break
        offset += 100
        for e in events:
            cat = offshore.event_category(e)
            for m in e.get("markets", []):
                outcome = offshore._outcome(m)
                toks = json.loads(m.get("clobTokenIds") or "[]")
                end_iso = m.get("endDate") or e.get("endDate")
                if outcome is None or not toks or not end_iso:
                    continue
                try:
                    hist = offshore.prices_history(toks[0], interval="max", fidelity=60)
                except Exception as exc:  # network hiccup: skip the market, keep building
                    log(f"  skip {m.get('id')}: {exc}")
                    continue
                res_ts = resolution_ts(hist)
                if res_ts is None:
                    continue
                p = price_before(hist, res_ts, hours_before)
                if p is None or p <= 0.0 or p >= 1.0:
                    continue
                add_sample(table, cat, p, outcome)
                add_sample(table, "all", p, outcome)
                time.sleep(sleep_s)
            seen += 1
            if seen >= max_events:
                break
        log(f"  calibration: {seen} events, {sum(c['n'] for c in table.get('all', {}).values())} samples")
    table["_built"] = datetime.now(timezone.utc).isoformat()
    return table


def save_table(table: dict, path: str = config.CALIBRATION_PATH) -> None:
    with open(path, "w") as f:
        json.dump(table, f, indent=1)


def load_table(path: str = config.CALIBRATION_PATH) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return json.load(f)


def summary(table: dict, category: str = "all") -> str:
    rows = [f"calibration [{category}] built {table.get('_built', '?')[:16]}"]
    for lo, hi in BANDS:
        cell = table.get(category, {}).get(f"{lo:.2f}-{hi:.2f}")
        if cell and cell["n"]:
            freq = cell["yes"] / cell["n"]
            rows.append(f"  {lo:.2f}-{hi:.2f}  n={cell['n']:<5} realized={freq:.0%}  vs mid {(lo + hi) / 2:.0%}  "
                        f"{'+' if freq > (lo + hi) / 2 else '-'}{abs(freq - (lo + hi) / 2) * 100:.0f}c")
    return "\n".join(rows)
