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
    m = re.search(r"site=([kK][a-zA-Z0-9]{3})", desc or "")
    return m.group(1).upper() if m else None


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


def prices_history(token: str, interval: str = "1d", fidelity: int = 5, since_ts: float | None = None):
    params = {"market": token, "fidelity": fidelity}
    if since_ts:
        params["startTs"] = int(since_ts)
        params["endTs"] = int(datetime.now(timezone.utc).timestamp())
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


# ---- generic events (hold_favorites, leadlag pairs, calibration) ----------------------------
def events_ending_within(days: int, limit: int = 200, closed: bool = False):
    end_max = datetime.now(timezone.utc).timestamp() + days * 86400
    params = {"closed": "true" if closed else "false", "limit": limit, "order": "endDate", "ascending": "true",
              "end_date_min": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
              "end_date_max": datetime.fromtimestamp(end_max, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    if not closed:
        params["active"] = "true"
    return _get(f"{GAMMA}/events", params)


def closed_events(limit: int = 100, offset: int = 0):
    return _get(f"{GAMMA}/events", {"closed": "true", "limit": limit, "offset": offset,
                                     "order": "endDate", "ascending": "false"})


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
