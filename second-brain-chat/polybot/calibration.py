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
    falls back to the all-category cell; None when both are too thin.

    The fallback has to trigger on a THIN category cell, not only a missing one. It used to take
    `category_cell or all_cell`, so a politics 0.85-0.90 cell holding 2 samples shadowed an `all`
    cell that might hold 60, and the lookup returned None for exactly the markets the module exists
    to trade. min_n and shrink are unchanged; this only makes the fallback do what it says."""
    key = band_key(price)
    for cell in (table.get(category, {}).get(key), table.get("all", {}).get(key)):
        if cell and cell["n"] >= min_n:
            return (cell["yes"] + shrink * price) / (cell["n"] + shrink)
    return None


# Every sample ever taken, keyed by market id, so a nightly rebuild ADDS to the table instead of
# re-deriving a few hundred samples from scratch. The 2026-09-12 build walked 300 events newest-
# first, which that night meant a couple of hours of crypto coin flips: 146 samples in total, 10
# in 0.85-0.90 and 15 in 0.90-0.95 — under lookup's min_n of 25 in BOTH favourite bands, so
# hold_favorites could never produce a signal, and the 03:00 rebuild never ran again because the
# Mac is asleep at 03:00.
SAMPLES_PATH = os.path.join(config.ROOT, "calibration-samples.json")
# Not sampled: the "Up or Down" 5/15-minute series (~75% of everything that closes, never a
# favourite a day out) and sports (never traded under the Ohio rule). Both would only have spent
# history calls; neither ever lands in a band hold_favorites trades.
EXCLUDE_TAG_IDS = (offshore.TAG_UP_OR_DOWN, offshore.TAG_SPORTS)


def load_samples(path: str = SAMPLES_PATH) -> dict:
    if not os.path.exists(path):
        return {"markets": {}, "newest_end": None, "oldest_end": None}
    with open(path) as f:
        d = json.load(f)
    d.setdefault("markets", {})
    return d


def save_samples(samples: dict, path: str = SAMPLES_PATH) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(samples, f)
    os.replace(tmp, path)


def sample_market(m: dict, hours_before: float = 24.0, history_fn=None):
    """(price a day before it effectively resolved, outcome) for one closed market, or None."""
    outcome = offshore._outcome(m)
    try:
        toks = json.loads(m.get("clobTokenIds") or "[]")
    except (TypeError, ValueError):
        toks = []
    if outcome is None or not toks:
        return None
    hist = (history_fn or (lambda t: offshore.prices_history(t, interval="max", fidelity=60)))(toks[0])
    res_ts = resolution_ts(hist)
    if res_ts is None:
        return None
    p = price_before(hist, res_ts, hours_before)
    if p is None or p <= 0.0 or p >= 1.0:
        return None
    return p, outcome


def table_from_samples(samples: dict) -> dict:
    table: dict = {}
    for row in samples.get("markets", {}).values():
        if not row:
            continue
        cat, p, outcome = row
        add_sample(table, cat, p, outcome)
        add_sample(table, "all", p, outcome)
    table["_built"] = datetime.now(timezone.utc).isoformat()
    table["_n"] = sum(c["n"] for c in table.get("all", {}).values())
    return table


def _closed_pages(end_max_iso: str | None, end_min_iso: str | None, max_events: int):
    """Closed events newest-first inside [end_min, end_max], past gamma's offset cap."""
    seen = 0
    cursor = end_max_iso
    while seen < max_events:
        moved = False
        last = None
        for offset in range(0, offshore.MAX_GAMMA_OFFSET + 1, 100):
            params = {"closed": "true", "limit": 100, "offset": offset, "order": "endDate",
                      "ascending": "false", "exclude_tag_id": list(EXCLUDE_TAG_IDS)}
            if cursor:
                params["end_date_max"] = cursor
            if end_min_iso:
                params["end_date_min"] = end_min_iso
            try:
                page = offshore._get(f"{offshore.GAMMA}/events", params)
            except Exception:
                break
            if not page:
                return
            yield page
            seen += len(page)
            moved = True
            last = page[-1].get("endDate") or last
            if len(page) < 100 or seen >= max_events:
                return
        if not moved or not last or last == cursor:
            return
        cursor = last


def build(max_events: int = 3000, sleep_s: float = 0.0, hours_before: float = 24.0, log=print,
          max_new: int = 2000, max_age_days: int = 180, samples_path: str = SAMPLES_PATH,
          workers: int = 4, history_fn=None, pages_fn=None) -> dict:
    """Add up to `max_new` newly closed (or not yet sampled) markets to the sample cache and return
    the table over EVERYTHING sampled so far.

    Walks two stretches: whatever closed since the newest event already sampled, then further back
    from the oldest one, until `max_age_days`. A night's run therefore costs history calls only for
    markets it has never seen — the gamma paging over cached ones is cheap."""
    from concurrent.futures import ThreadPoolExecutor

    samples = load_samples(samples_path)
    known = samples["markets"]
    now = datetime.now(timezone.utc)
    floor = datetime.fromtimestamp(now.timestamp() - max_age_days * 86400, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    stretches = [(now_iso, samples.get("newest_end"))]
    if samples.get("oldest_end"):
        stretches.append((samples["oldest_end"], floor))
    pages = pages_fn or _closed_pages
    todo = []
    newest, oldest = samples.get("newest_end"), samples.get("oldest_end")
    for end_max, end_min in stretches:
        for page in pages(end_max, end_min or floor, max_events):
            for e in page:
                end = e.get("endDate")
                if end and end <= now_iso:
                    newest = max(newest or end, end)
                    oldest = min(oldest or end, end)
                cat = offshore.event_category(e)
                for m in e.get("markets", []):
                    mid = str(m.get("id"))
                    if mid in known or not m.get("closed"):
                        continue
                    todo.append((mid, cat, m))
            if len(todo) >= max_new:
                break
        if len(todo) >= max_new:
            break
    # No trimming to max_new: `oldest` has already moved past every event on the pages walked, so a
    # market cut here would sit behind the cursor and never be sampled. One page over is the price.

    def one(item):
        mid, cat, m = item
        try:
            got = sample_market(m, hours_before, history_fn)
        except Exception as exc:          # a network hiccup must not poison the cache with a None
            return mid, cat, exc
        if sleep_s:
            time.sleep(sleep_s)
        return mid, cat, got

    added = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for i, (mid, cat, got) in enumerate(pool.map(one, todo), 1):
            if isinstance(got, Exception):
                continue                  # try again next build
            known[mid] = [cat, got[0], got[1]] if got else None
            added += bool(got)
            if i % 500 == 0:
                log(f"  calibration: {i}/{len(todo)} markets checked, {added} new samples")
                save_samples(samples, samples_path)
    samples["newest_end"], samples["oldest_end"] = newest, oldest
    save_samples(samples, samples_path)
    table = table_from_samples(samples)
    log(f"  calibration: +{added} samples from {len(todo)} new markets → {table['_n']} total")
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
