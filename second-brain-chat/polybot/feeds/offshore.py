"""polymarket.com read-only feed (gamma + CLOB public endpoints). Works from a US IP for reading.

Used as (a) the price REFERENCE that the US venue lags and (b) the paper-trading proxy for the
weather modules until the US key exists. The bot can never trade here (US-blocked).
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

import requests

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
TIMEOUT = 20
_session = requests.Session()
_session.headers["User-Agent"] = "polybot/0.1 (read-only research)"

MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august",
          "september", "october", "november", "december"]


def _get(url, params=None):
    r = _session.get(url, params=params, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


# ---- buckets ---------------------------------------------------------------------------------
@dataclass
class Bucket:
    title: str
    lo: float
    hi: float
    unit: str
    yes_token: str
    no_token: str
    market_id: str
    condition_id: str
    best_bid: float | None
    best_ask: float | None
    last: float | None
    closed: bool
    outcome: int | None          # 1 / 0 once resolved, else None
    liquidity: float = 0.0
    # Contracts resting at best_bid / best_ask. None means "not looked up" — NOT zero. An arb sized
    # off a quote it cannot actually fill is the same phantom-edge mistake as reading a missing ask
    # as free money, so bucket_sum refuses to size a set until these are filled in from the book.
    bid_qty: float | None = None
    ask_qty: float | None = None
    # Full price ladders, best first. Top-of-book alone badly understates how big a set can be:
    # chicago's "71 or below" bid 3 contracts at 0.16 and 100 at 0.15 on 2026-09-19, so one cent
    # of price turned a 3-set arb into a 100-set one.
    bid_levels: list | None = None
    ask_levels: list | None = None
    # The venue's own taker fee coefficient for THIS market (`feeCoefficient`). None means "ask
    # the fee table", which is only right when the venue did not say -- fees are ~30% of an arb's
    # gross edge, so this is not a detail that can be carried by a constant read out of a doc.
    fee_coefficient: float | None = None

    def contains(self, temp: float) -> bool:
        return self.lo <= temp <= self.hi

    @property
    def mid(self):
        if self.best_bid is not None and self.best_ask is not None:
            return (self.best_bid + self.best_ask) / 2
        return self.last


@dataclass
class WeatherEvent:
    slug: str
    city: str
    date: str                    # YYYY-MM-DD local
    station: str | None          # e.g. KLGA — parsed from the description
    rule: str                    # 'hourly' (offshore Temp column) or 'cli' (NWS daily climate report)
    unit: str
    buckets: list = field(default_factory=list)
    neg_risk: bool = True
    end_iso: str = ""
    kind: str = "high"           # high | low


_BUCKET_RE = re.compile(r"(-?\d+(?:\.\d+)?)\s*(?:-\s*(-?\d+(?:\.\d+)?))?\s*°?\s*([CF])?", re.I)


def parse_bucket_title(title: str):
    """'69°F or below' -> (-inf, 69, 'F'); '70-71°F' -> (70, 71, 'F'); '27°C' -> (27, 27, 'C');
    '88°F or higher' -> (88, inf, 'F')."""
    t = title.strip()
    m = _BUCKET_RE.search(t)
    if not m:
        raise ValueError(f"unparseable bucket title: {title!r}")
    a = float(m.group(1))
    b = float(m.group(2)) if m.group(2) else None
    unit = (m.group(3) or "F").upper()
    low = t.lower()
    if "below" in low or "less" in low or "under" in low:
        return -math.inf, a, unit
    if "higher" in low or "above" in low or "more" in low or "over" in low:
        return a, math.inf, unit
    return (a, b if b is not None else a, unit)


def station_from_description(desc: str) -> str | None:
    """NWS 'timeseries?site=klga' → KLGA; Weather Underground 'history/daily/gb/london/EGLL' → EGLL;
    a bare '(EGLL)' or 'station EGLL' → EGLL."""
    d = desc or ""
    for pat in (r"site=([A-Za-z][A-Za-z0-9]{3})\b", r"history/daily/[a-z]{2}/[^/\s]+/([A-Z]{4})\b",
                r"\bstation\s+\(?([A-Z]{4})\)?", r"\(([A-Z]{4})\)"):
        m = re.search(pat, d)
        if m:
            return m.group(1).upper()
    return None


def _outcome(m) -> int | None:
    if not m.get("closed"):
        return None
    try:
        prices = json.loads(m.get("outcomePrices") or "[]")
        y = float(prices[0])
        if y >= 0.99:
            return 1
        if y <= 0.01:
            return 0
    except (ValueError, IndexError, TypeError):
        pass
    return None


def _bucket_from_market(m) -> Bucket:
    lo, hi, unit = parse_bucket_title(m.get("groupItemTitle") or m.get("question") or "")
    toks = json.loads(m.get("clobTokenIds") or "[]")
    return Bucket(
        title=m.get("groupItemTitle") or m.get("question") or "",
        lo=lo, hi=hi, unit=unit,
        yes_token=toks[0] if toks else "", no_token=toks[1] if len(toks) > 1 else "",
        market_id=str(m.get("id")), condition_id=m.get("conditionId") or "",
        best_bid=_f(m.get("bestBid")), best_ask=_f(m.get("bestAsk")), last=_f(m.get("lastTradePrice")),
        closed=bool(m.get("closed")), outcome=_outcome(m), liquidity=_f(m.get("liquidityNum")) or 0.0,
    )


def _f(x):
    try:
        return None if x is None else float(x)
    except (TypeError, ValueError):
        return None


def weather_event_from_gamma(e: dict, city: str, kind: str = "high") -> WeatherEvent:
    markets = sorted((m for m in e.get("markets", [])), key=lambda m: parse_bucket_title(
        m.get("groupItemTitle") or m.get("question") or "")[0])
    buckets = [_bucket_from_market(m) for m in markets]
    desc = e.get("description") or (markets[0].get("description") if markets else "") or ""
    station = station_from_description(desc)
    date = _date_from_slug(e.get("slug", "")) or (e.get("endDate") or "")[:10]
    return WeatherEvent(slug=e.get("slug", ""), city=city, date=date, station=station,
                        rule="hourly" if "hourly" in desc.lower() or "timeseries" in desc.lower() else "cli",
                        unit=buckets[0].unit if buckets else "F", buckets=buckets,
                        neg_risk=bool(e.get("negRisk")), end_iso=e.get("endDate") or "", kind=kind)


def _date_from_slug(slug: str) -> str | None:
    m = re.search(r"on-([a-z]+)-(\d{1,2})-(\d{4})$", slug)
    if not m or m.group(1) not in MONTHS:
        return None
    return f"{m.group(3)}-{MONTHS.index(m.group(1)) + 1:02d}-{int(m.group(2)):02d}"


def slug_for(city_slug: str, date: datetime, kind: str = "high") -> str:
    word = "highest" if kind == "high" else "lowest"
    return f"{word}-temperature-in-{city_slug}-on-{MONTHS[date.month - 1]}-{date.day}-{date.year}"


def event_by_slug(slug: str) -> dict | None:
    data = _get(f"{GAMMA}/events", {"slug": slug})
    return data[0] if data else None


def find_weather_event(city_slug: str, query_name: str, date: datetime, kind: str = "high") -> WeatherEvent | None:
    """Try the canonical slug first, then a public search for the same day."""
    e = event_by_slug(slug_for(city_slug, date, kind))
    if e is None:
        word = "highest" if kind == "high" else "lowest"
        res = _get(f"{GAMMA}/public-search", {"q": f"{word} temperature in {query_name}", "limit_per_type": 12})
        want = f"on-{MONTHS[date.month - 1]}-{date.day}-{date.year}"
        for cand in res.get("events", []):
            if cand.get("slug", "").endswith(want) and word in cand.get("slug", ""):
                e = event_by_slug(cand["slug"]) or cand
                break
    return weather_event_from_gamma(e, city_slug, kind) if e else None


# ---- books / history ------------------------------------------------------------------------
def book(token: str) -> dict:
    d = _get(f"{CLOB}/book", {"token_id": token})
    bids = sorted(((float(x["price"]), float(x["size"])) for x in d.get("bids", [])), reverse=True)
    asks = sorted(((float(x["price"]), float(x["size"])) for x in d.get("asks", [])))
    return {"bids": bids, "asks": asks, "tick": float(d.get("tick_size") or 0.01),
            "min_size": float(d.get("min_order_size") or 5), "last": _f(d.get("last_trade_price")),
            "neg_risk": bool(d.get("neg_risk"))}


def best_bid_ask(token: str):
    b = book(token)
    return (b["bids"][0][0] if b["bids"] else None, b["asks"][0][0] if b["asks"] else None, b)


def prices_history(token: str, interval: str = "1d", fidelity: int = 5, since_ts: float | None = None,
                   until_ts: float | None = None):
    """`until_ts` bounds the window. The CLOB 400s when startTs..endTs is too wide for the fidelity,
    and an open-ended window is "since then until NOW" — so a backtest of a day three weeks ago
    asked for three weeks of 5-minute points and got nothing back (2026-09-18: $0.00 on 987 of 987
    buckets). A day only needs its own day."""
    params = {"market": token, "fidelity": fidelity}
    if since_ts:
        params["startTs"] = int(since_ts)
        now = datetime.now(timezone.utc).timestamp()
        params["endTs"] = int(min(until_ts, now) if until_ts else now)
    else:
        params["interval"] = interval
    d = _get(f"{CLOB}/prices-history", params)
    return [(float(p["t"]), float(p["p"])) for p in d.get("history", [])]


def market_by_id(market_id: str) -> dict | None:
    try:
        return _get(f"{GAMMA}/markets/{market_id}")
    except requests.HTTPError:
        return None


def market_resolution(market_id: str) -> int | None:
    m = market_by_id(market_id)
    return _outcome(m) if m else None


def batch_prices(tokens) -> dict:
    """{token: (bid, ask)} for many tokens in one POST per 200. The leadlag recorder samples every
    paired market each minute, and one book call per token would be ~100 calls a minute for a
    number the CLOB will hand over in a single request."""
    out = {}
    tokens = [t for t in dict.fromkeys(tokens) if t]
    for i in range(0, len(tokens), 200):
        chunk = tokens[i:i + 200]
        r = _session.post(f"{CLOB}/prices", json=[{"token_id": t, "side": s} for t in chunk for s in ("BUY", "SELL")],
                          timeout=TIMEOUT)
        r.raise_for_status()
        for tok, sides in (r.json() or {}).items():
            px = [p for p in (_f((sides or {}).get("BUY")), _f((sides or {}).get("SELL"))) if p is not None]
            # BUY is the best bid and SELL the best ask (checked live 2026-09-23: 0.65 / 0.66 on a
            # 0.655 midpoint). Order them rather than trust the labels: a swapped pair would make
            # every mid right and every spread negative, which is the kind of error nobody sees.
            out[tok] = (min(px), max(px)) if len(px) == 2 else (px[0], px[0]) if px else (None, None)
    return out


# ---- generic events (hold_favorites, leadlag pairs, calibration) ----------------------------
# Gamma tag ids (checked via /tags/{id} 2026-09-23). "Up or Down" is the 5- and 15-minute crypto
# coin-flip series: ~1,600 of every 2,100 events closing in a 12-hour window, never priced anywhere
# near a favourite a day out, and it buries everything else in any listing ordered by end date.
TAG_UP_OR_DOWN = 102127
TAG_SPORTS = 1


# Gamma refuses any offset past ~2,000 (HTTP 422, measured 2026-09-23), so a listing that only pages
# by offset stops dead there. Past it, move the date window forward and start the offsets again.
MAX_GAMMA_OFFSET = 2000


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _paged_events(params: dict, max_events: int, date_key: str = "end_date_min") -> list:
    """Every event a gamma listing answers, ordered by endDate ascending, deduped by id.

    Pages by offset within one `date_key` floor and, when the offset cap or a full page of equal
    end dates stops that, restarts from the last end date seen. Stops at `max_events`."""
    seen: dict = {}
    base = dict(params, limit=100, order="endDate", ascending="true")
    floor = base.get(date_key)
    while len(seen) < max_events:
        progressed = False
        offset = 0
        last_end = None
        while offset <= MAX_GAMMA_OFFSET and len(seen) < max_events:
            q = dict(base, offset=offset)
            if floor:
                q[date_key] = floor
            try:
                page = _get(f"{GAMMA}/events", q)
            except requests.HTTPError:
                break                          # the offset cap: move the floor instead
            for e in page:
                if e.get("id") not in seen:
                    seen[e.get("id")] = e
                    progressed = True
                last_end = e.get("endDate") or last_end
            if len(page) < 100:
                return list(seen.values())
            offset += 100
        if not progressed or not last_end or last_end == floor:
            break                              # more than ~2,000 events share one end date
        floor = last_end
    return list(seen.values())


def events_ending_within(days: int, limit: int = 200, closed: bool = False, max_events: int = 20000,
                         exclude_tag_ids=()):
    """Every open event ending in the next `days` days.

    This used to be ONE page of the soonest-ending events, and gamma caps a page at 100: on
    2026-09-23 that page was the next 40 minutes — 98 crypto "Up or Down" coin flips and two KHL
    games — out of 3,677 events ending inside a single day. hold_favorites looked at 147 markets,
    two of them in its 85-95c band, and never fired once. `limit` is kept for callers that really
    do want only the first page."""
    now = datetime.now(timezone.utc).timestamp()
    params = {"closed": "true" if closed else "false", "end_date_min": _iso(now),
              "end_date_max": _iso(now + days * 86400)}
    if exclude_tag_ids:
        params["exclude_tag_id"] = list(exclude_tag_ids)
    if not closed:
        params["active"] = "true"
    if limit and limit <= 100:
        return _get(f"{GAMMA}/events", dict(params, limit=limit, order="endDate", ascending="true"))
    return _paged_events(params, max_events)


def active_events_by_tag(tag: str, max_events: int = 6000, exclude_tag_ids=(TAG_UP_OR_DOWN,)) -> list:
    """Open events carrying a gamma tag (`politics`, `midterms`, `fed` ...), past the offset cap."""
    params = {"active": "true", "closed": "false", "tag_slug": tag}
    if exclude_tag_ids:
        params["exclude_tag_id"] = list(exclude_tag_ids)
    return _paged_events(params, max_events)


def closed_events(limit: int = 100, offset: int = 0, end_date_max: str | None = None):
    params = {"closed": "true", "limit": limit, "offset": offset, "order": "endDate", "ascending": "false"}
    if end_date_max:
        params["end_date_max"] = end_date_max
    return _get(f"{GAMMA}/events", params)


def event_category(e: dict) -> str:
    slugs = {(t.get("slug") or "").lower() for t in (e.get("tags") or [])}
    for cat in ("sports", "weather", "crypto", "politics", "geopolitics", "economics", "finance",
                "culture", "tech", "mentions"):
        if cat in slugs or any(s.startswith(cat) for s in slugs):
            return cat
    title = (e.get("title") or "").lower()
    if any(w in title for w in ("vs.", " vs ", "win on", "spread:", "o/u", "moneyline")):
        return "sports"
    return "other"
