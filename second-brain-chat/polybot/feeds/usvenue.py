"""Polymarket US adapter over the official `polymarket-us` SDK (pip install polymarket-us).

Even market data is key-gated (verified 2026-09-12: 401 without headers), so `available` is
False until POLYMARKET_KEY_ID / POLYMARKET_SECRET_KEY exist in the environment. Every method
degrades to None/[] when unavailable so the rest of the bot keeps running in paper mode.

Only LIMIT orders are ever sent (maker intent). No market orders exist in this file on purpose.

gateway.polymarket.us sits behind Cloudflare, and CWRU's shared campus IP got rate-limited
(error 1015, "You are being rate limited" / "banned you temporarily") on 2026-09-17. The SDK's
error path (`client.py::_handle_error_response`) puts the raw response body — the whole HTML
page — into the exception message when it isn't JSON, and nothing here caught it: every scan
tick, every settle, and every 5-minute sync kept calling the banned host again, each failure
logging ~250 lines of HTML and doing nothing to let the ban clear. That is why the US-book
cross-check for weather_lock (report's "Polymarket US books only" line) sits stuck on stale
signals — `resolution()` never learns the outcome, it just fails the same way every hour. A
rate limit now trips a cooldown (`available` goes False, same as a missing key) so the loop
backs off instead of hammering a host that just told it to stop.
"""
from __future__ import annotations

import json
import math
import os
import re
import time
from datetime import datetime

from .. import config
from .offshore import Bucket, WeatherEvent, station_from_description

try:  # the SDK is optional until the key exists
    from polymarket_us import PolymarketUS  # type: ignore
except Exception:  # pragma: no cover - import guard
    PolymarketUS = None

# Measured against the live gateway on 2026-09-18, three separate runs: the SIXTH call is refused
# and the first five always succeed — at 0.05s apart, at 0.35s apart, and at a full 1.0s apart
# (5 calls / 5.4s). So this is a QUOTA of five requests per window, not a burst-rate limit, and
# spacing calls out does not buy a sixth. The quota then replenishes within seconds, not the ten
# minutes the old flat backoff assumed — that backoff cost the bot ten minutes of blindness per
# depth read, in the afternoon window where the arbs actually appear.
#
# So: spend from a budget instead of reacting to a ban. The reactive backoff stays as a safety
# net because gateway.polymarket.us sees the whole CWRU campus IP (129.22.1.29) and other people
# on that network spend from the same quota.
SDK_TIMEOUT_S = 10.0               # a 30s call is already a failure for a 2-minute scan
SCREEN_BOOK_TTL_S = 150            # how stale a book may be for SCREENING (never for trading);
                                   # longer than the 2-minute scan so consecutive passes reuse it
CALL_BUDGET = 5                    # requests allowed per window
CALL_WINDOW_S = 12.0               # STARTING window; widened automatically when the venue refuses
CALL_WINDOW_MAX_S = 60.0           # ceiling on the self-tuned window
CLEAN_CALLS_TO_RELAX = 40          # a long clean run earns the window back
# Tokens a SCREENING call will not touch. The depth read is the only call that leads to money and
# it needs six at once, so when it shares a flat budget with routine screening it is the one that
# loses -- and losing it costs the whole episode, not a screen. On 2026-09-18 the best book on
# record (chicago, 21 sets, $3.23) was refused twice with "depth INCOMPLETE, standing down"
# because a leg's book call came back empty, and an empty book call on this venue means the quota
# was gone. Screening now runs on a smaller budget so there is always something left for the call
# that actually trades.
# 1, not 2: the reserve costs screening throughput at CALL_BUDGET=5, and holding back two of
# five tokens made a cold pass miss its 45s budget entirely ("skipped 10 city-day(s)"). One token
# still means a depth read never has to wait a full window to make its first call, which is the
# part that matters -- _space() sleeps rather than failing, so the rest of the read always lands.
DEPTH_RESERVE = 1
# How long a depth read will WAIT OUT a cooldown before giving up on the set. book() fails on
# exactly one path -- the quota -- so "ask again immediately" is useless: the venue is in backoff
# and the retry fails the same way. Serving the cooldown is the only retry that means anything.
# It holds the scan pass, which is why it is capped: 15s to rescue a $3.23 set is worth it, two
# minutes is not, and a sweep runs every 20s anyway so a long cooldown is the next sweep's problem.
DEPTH_RETRY_WAIT_MAX_S = 20.0
RATE_LIMIT_BACKOFF_S = 15          # first offence — short, because recovery is short
RATE_LIMIT_BACKOFF_MAX_S = 120     # ceiling if it keeps happening
# The budget throttles real network calls. Tests inject a fake client and must not sleep for it:
# a suite that takes 25 seconds instead of 1 is a suite that stops being run. Same JARVIS_TEST
# switch app.py and capability_watcher.py use.
TEST_MODE = os.environ.get("JARVIS_TEST", "").strip().lower() in ("1", "true", "yes")
MISSING_EVENT_RETRY_S = 3600  # an event slug that 404s is not asked for again this hour
# A prefetched event is good for the pass that fetched it and no longer. Screening on a quote
# from the PREVIOUS pass is the phantom-edge trap again, so this is deliberately shorter than
# the gap between passes: miss the cache and pay for the call rather than trade on stale paper.
EVENT_PREFETCH_TTL_S = 45


def _is_rate_limited(exc: Exception) -> bool:
    if getattr(exc, "status_code", None) == 429:
        return True
    text = str(exc)[:4000].lower()
    return "rate limit" in text or "banned you temporarily" in text


# ---- response shapes, verified live 2026-09-12 with the real key ---------------------------
# markets.bbo(slug)   -> {"marketData": {"bestBid": {"value": "0.27"}, "bestAsk": {...}, "lastTradePx": {...},
#                         "settlementPx": {...}, "state": "MARKET_STATE_OPEN", "bidDepth": 14, ...}}
# markets.book(slug)  -> {"marketData": {"bids": [{"px": {"value": ".27"}, "qty": "1.0"}], "asks": [...]}}
# account.balances()  -> {"balances": [{"currentBalance": 210.01, "buyingPower": 210.01, "displayedCash": 60.01,
#                         "bonusReservation": 150, ...}]}
# portfolio.positions()-> {"positions": {...}, "availablePositions": [...]}   orders.list -> {"orders": [...]}
# search.query        -> [event, ...]; event {slug: "temp-nychigh-2026-09-13", title, markets: [market, ...]}
# market              -> {slug: "tc-temp-nychigh-2026-09-13-gte78lt79f", title: "78 to 79", outcomePrices:
#                         '["0.35","0.36"]', bestBidQuote: {value}, bestAskQuote: {value}, status:
#                         "MARKET_STATUS_OPEN", description: "... at Central Park (KNYC) ...", feeCoefficient}

_US_RANGE = re.compile(r"(-?\d+(?:\.\d+)?)\s*(?:to|-|–)\s*(-?\d+(?:\.\d+)?)", re.I)
_US_NUM = re.compile(r"-?\d+(?:\.\d+)?")


def parse_us_bucket_title(title: str):
    """'77 or below' -> (-inf, 77); '78 to 79' -> (78, 79); '86 or higher' -> (86, inf). Whole °F."""
    t = (title or "").strip().lower()
    m = _US_RANGE.search(t)
    if m:
        return float(m.group(1)), float(m.group(2))
    n = _US_NUM.search(t)
    if not n:
        raise ValueError(f"unparseable US bucket title: {title!r}")
    a = float(n.group(0))
    if any(w in t for w in ("below", "less", "under")):
        return -math.inf, a
    if any(w in t for w in ("higher", "above", "more", "over")):
        return a, math.inf
    return a, a


def _yes_price(m) -> float | None:
    try:
        return float(json.loads(m.get("outcomePrices") or "[]")[0])
    except (ValueError, IndexError, TypeError):
        return None


def _market_closed(m) -> bool:
    return bool(m.get("closed")) or str(m.get("status", "")).endswith(("RESOLVED", "SETTLED", "CLOSED"))


def _fee_coefficient(m: dict) -> float | None:
    """The market's own taker fee coefficient, or None if it did not say."""
    try:
        v = m.get("feeCoefficient")
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def bucket_from_us_market(m: dict) -> Bucket:
    lo, hi = parse_us_bucket_title(m.get("title") or m.get("titleShort") or "")
    slug = m.get("slug") or ""
    last = _yes_price(m)
    closed = _market_closed(m)
    outcome = None
    if closed and last is not None:
        outcome = 1 if last >= 0.99 else 0 if last <= 0.01 else None
    return Bucket(title=m.get("title") or m.get("titleShort") or "", lo=lo, hi=hi, unit="F",
                  yes_token=slug, no_token=slug, market_id=slug, condition_id=str(m.get("id") or ""),
                  best_bid=_price(m.get("bestBidQuote")), best_ask=_price(m.get("bestAskQuote")), last=last,
                  closed=closed, outcome=outcome, liquidity=0.0,
                  fee_coefficient=_fee_coefficient(m))


def buckets_from_markets(markets: list) -> list:
    """Bucket objects for a NON-weather event (a Fed decision, an election).

    The arb code is written against Bucket because that is what weather produced first, but the
    only fields it needs are the quotes, the depth and `closed`. lo/hi are filled with the leg's
    index purely so the object is well-formed; they are deliberately NOT a tiling, and callers on
    this path must carry `universe`'s settlement proof instead (`assume_exhaustive`).
    """
    out = []
    for i, m in enumerate(markets):
        slug = m.get("slug") or ""
        out.append(Bucket(title=m.get("title") or m.get("titleShort") or slug, lo=float(i), hi=float(i),
                          unit="", yes_token=slug, no_token=slug, market_id=slug,
                          condition_id=str(m.get("id") or ""), best_bid=_price(m.get("bestBidQuote")),
                          best_ask=_price(m.get("bestAskQuote")), last=_yes_price(m),
                          closed=_market_closed(m), outcome=None, liquidity=0.0,
                          fee_coefficient=_fee_coefficient(m)))
    return out


def us_event_slug(city_slug: str, date: datetime, kind: str = "high") -> str:
    meta = config.city_meta(city_slug) or {}
    code = (meta.get("cli_location") or "").lower()
    return f"temp-{code}{'high' if kind == 'high' else 'low'}-{date.strftime('%Y-%m-%d')}"


def weather_event_from_us(e: dict, city_slug: str, kind: str = "high") -> WeatherEvent | None:
    markets = [m for m in (e.get("markets") or []) if m.get("slug")]
    if not markets:
        return None
    buckets = sorted((bucket_from_us_market(m) for m in markets), key=lambda b: b.lo)
    desc = " ".join(m.get("description") or "" for m in markets[:1])
    station = station_from_description(desc) or (config.city_meta(city_slug) or {}).get("us_station")
    slug = e.get("slug") or ""
    m_date = re.search(r"(\d{4}-\d{2}-\d{2})$", slug)
    date = m_date.group(1) if m_date else (e.get("endDate") or "")[:10]
    return WeatherEvent(slug=slug, city=city_slug, date=date, station=station, rule="cli", unit="F",
                        buckets=buckets, neg_risk=False, end_iso=e.get("endDate") or "", kind=kind)


def _unwrap(d):
    return (d or {}).get("marketData") or d or {}


def _unwrap_event(d):
    """`GET /v1/event/slug/{slug}` answers `{"event": {...}}` (SDK `GetEventResponse`), while
    `search.query` answers the event objects bare. Reading `markets` off the envelope found
    nothing, so every weather lookup fell through to the search fallback — two calls per city
    per scan against a Cloudflare-fronted host that rate-limits this campus IP (error 1015,
    2026-09-17). Unwrap, and the slug path works on one call again."""
    if isinstance(d, dict) and "event" in d and isinstance(d["event"], dict):
        return d["event"]
    return d


def _is_not_found(exc: Exception) -> bool:
    return "NotFound" in type(exc).__name__ or "not found" in str(exc).lower()


class USVenue:
    name = "us"

    def __init__(self):
        self.key_id = os.environ.get("POLYMARKET_KEY_ID")
        self.secret = os.environ.get("POLYMARKET_SECRET_KEY")
        self.sdk_installed = PolymarketUS is not None
        # Never live in the test suite. The keys are in the environment whenever the loop's own
        # .env is sourced, so constructing a Runner in a test was making REAL calls to the
        # gateway (Runner.__init__ reads the account balance) — flaky, slow, and spending the
        # same shared campus quota the running bot needs. A test that wants a venue says so by
        # assigning `available = True` and injecting a fake client, which is what they all do.
        self._base_available = bool(self.sdk_installed and self.key_id and self.secret
                                    and not TEST_MODE)
        # The SDK defaults to a 30s timeout. For a scanner that wants to sweep five markets every
        # two minutes a 30s call has already failed — on 2026-09-19 a pass stalled between nyc and
        # los-angeles for fifteen minutes with no error and no rate limit, just slow sockets on
        # campus wifi. Fail fast and let the next tick try again.
        self._client = (PolymarketUS(key_id=self.key_id, secret_key=self.secret, timeout=SDK_TIMEOUT_S)
                        if self._base_available else None)
        self._backoff_until = 0.0
        self._backoff_reason = ""
        self._backoff_s = RATE_LIMIT_BACKOFF_S
        self._calls: list[float] = []      # timestamps of recent requests, the token bucket
        self._window_s = CALL_WINDOW_S     # self-tuning: the campus IP's spare quota varies
        self._clean = 0
        self.on_backoff = None             # set by the runner so a blind venue is never silent
        self._missing: dict[str, float] = {}   # event slug -> retry-after ts
        self._book_cache: dict[str, tuple] = {}   # market slug -> (ts, book), for the SCREEN only
        self._prefetch: dict[str, tuple] = {}     # event slug -> (ts, event), one batched pass

    @property
    def available(self) -> bool:
        return self._base_available and time.time() >= self._backoff_until

    @available.setter
    def available(self, value: bool) -> None:
        self._base_available = bool(value)

    @property
    def why_unavailable(self) -> str:
        if not self.sdk_installed:
            return "polymarket-us SDK not installed (pip install polymarket-us)"
        if not (self.key_id and self.secret):
            return "POLYMARKET_KEY_ID / POLYMARKET_SECRET_KEY not set"
        if time.time() < self._backoff_until:
            mins = int((self._backoff_until - time.time()) / 60) + 1
            return f"backing off {mins}m ({self._backoff_reason})"
        return ""

    def _enter_backoff(self, exc: Exception) -> None:
        self._backoff_until = time.time() + self._backoff_s
        self._backoff_reason = f"{type(exc).__name__}: {str(exc).splitlines()[0][:120]} ({self._backoff_s:.0f}s)"
        # The budget was tuned on a probe that had the campus IP to itself. It does not, so let the
        # window find its own level rather than pretending 5-per-12s is a law.
        self._window_s = min(self._window_s * 1.5, CALL_WINDOW_MAX_S)
        self._clean = 0
        # A venue that has gone blind used to do so in total silence: scan_weather returns 0
        # without logging, so the 2-minute arb scan simply did not happen and nothing said why.
        # That is this system's signature failure and it cost 12-to-16-minute holes in the window
        # on 2026-09-19.
        if self.on_backoff:
            try:
                self.on_backoff(f"us venue blind for {self._backoff_s:.0f}s "
                                f"(budget now {CALL_BUDGET}/{self._window_s:.0f}s) — {self._backoff_reason}")
            except Exception:
                pass
        self._backoff_s = min(self._backoff_s * 2, RATE_LIMIT_BACKOFF_MAX_S)

    def _space(self, priority: bool = False) -> None:
        """Spend one request from the budget, waiting for the window to roll if it is empty.

        A six-bucket depth read is six requests, so it costs one window plus a moment — about
        fifteen seconds. The arb episodes in the snapshot record lasted one to four minutes, so
        that is affordable; being banned for ten minutes was not.

        `priority` is for the depth read, the only call that can lead to a trade. It may spend the
        whole budget; screening may not, so a candidate never arrives to find the quota already
        spent on looking at books that had nothing in them.
        """
        if TEST_MODE:
            return
        budget = CALL_BUDGET if priority else max(1, CALL_BUDGET - DEPTH_RESERVE)
        now = time.time()
        self._calls = [t for t in self._calls if now - t < self._window_s]
        if len(self._calls) >= budget:
            wait = self._window_s - (now - self._calls[0]) + 0.05
            if wait > 0:
                time.sleep(wait)
            now = time.time()
            self._calls = [t for t in self._calls if now - t < self._window_s]
        self._calls.append(now)

    def _guarded(self, fn, default=None, priority: bool = False):
        """Run an SDK call; on a rate limit, back off instead of calling a banned host again next
        tick, and return `default` instead of the raw (often HTML) error body every caller would
        otherwise have to log in full."""
        self._space(priority)
        try:
            out = fn()
        except Exception as exc:
            if _is_rate_limited(exc):
                self._enter_backoff(exc)
                return default
            raise
        self._backoff_s = RATE_LIMIT_BACKOFF_S   # a clean call resets the escalation
        self._clean += 1
        if self._clean >= CLEAN_CALLS_TO_RELAX and self._window_s > CALL_WINDOW_S:
            self._window_s = max(self._window_s / 1.5, CALL_WINDOW_S)
            self._clean = 0
        return out

    # ---- market data ---------------------------------------------------------------------
    def search(self, query: str):
        if not self.available:
            return None
        return self._guarded(lambda: self._client.search.query({"query": query}))

    def events(self, **params):
        if not self.available:
            return []
        return self._guarded(
            lambda: self._client.events.list(params or {"limit": 50, "active": True}).get("events", []),
            default=[])

    def market(self, slug: str):
        if not self.available:
            return None
        return self._guarded(lambda: self._client.markets.retrieve_by_slug(slug))

    def bbo(self, slug: str):
        """Return (bid, ask) as floats, or (None, None)."""
        if not self.available:
            return None, None
        d = self._guarded(lambda: _unwrap(self._client.markets.bbo(slug)), default={})
        return _price(d.get("bestBid", d.get("bid"))), _price(d.get("bestAsk", d.get("ask")))

    def bbo_full(self, slug: str) -> dict:
        """The whole marketData block: bestBid/bestAsk/lastTradePx/settlementPx/state/depths."""
        if not self.available:
            return {}
        return self._guarded(lambda: _unwrap(self._client.markets.bbo(slug)), default={})

    def book(self, slug: str, max_age_s: float = 0.0, priority: bool = False):
        """{'bids': [(px, qty)...] high→low, 'asks': [(px, qty)...] low→high, 'last': float|None}.

        `max_age_s` serves the SCREEN, never a trade. Re-pricing the same 1c tail leg every two
        minutes is most of what the screen spends — one tick on 2026-09-18 burned 19 calls doing it
        across five cities — and at five requests per twelve seconds that housekeeping can put a
        real candidate's depth read behind it in the queue. The decision to trade always reads
        fresh (`fill_depth_buckets` leaves this at 0), so a stale screen can only cost a second
        look, never a bad fill."""
        if not self.available:
            return None
        if max_age_s > 0:
            hit = self._book_cache.get(slug)
            if hit and time.time() - hit[0] <= max_age_s:
                return hit[1]
        d = self._guarded(lambda: _unwrap(self._client.markets.book(slug)), priority=priority)
        if d is None:
            return None
        # The venue calls the ask side "offers" (SDK `MarketBook`: bids / offers). Reading "asks"
        # returned [] on every US market ever sampled, so this book looked one-sided — which is
        # exactly the shape that makes `(1 - post)` read as phantom edge. Accept both names.
        levels = lambda key, alt: [x for x in (d.get(key) or d.get(alt) or [])]
        bids = sorted(((_price(x.get("px")), float(x.get("qty") or 0)) for x in levels("bids", "bid")
                       if _price(x.get("px")) is not None), reverse=True)
        asks = sorted(((_price(x.get("px")), float(x.get("qty") or 0)) for x in levels("offers", "asks")
                       if _price(x.get("px")) is not None))
        last = _price(d.get("lastTradePx"))
        if last is None:  # moved under stats.lastPriceSample.longPx
            sample = ((d.get("stats") or {}).get("lastPriceSample") or {})
            last = _price(sample.get("longPx") or sample.get("px"))
        out = {"bids": bids, "asks": asks, "last": last, "tick": 0.01}
        self._book_cache[slug] = (time.time(), out)
        return out

    # ---- weather events (the five US cities) ----------------------------------------------
    def prefetch_weather_events(self, triples) -> int:
        """Fetch a whole pass's weather events in ONE call. `triples` is (city_slug, date, kind).

        `events.list({"slug": [...]})` returns the same event objects `retrieve_by_slug` does —
        checked field-for-field against the live gateway on 2026-09-19: identical key sets, the
        `bestBidQuote`/`bestAskQuote` blocks present, same count of unquoted legs. So a pass over
        five cities and two days, which asked for up to TEN separate events, can ask once.

        That was the real cadence limit. At five requests per window a ten-event pass needed two
        full windows before it priced a single leg, which is why a scan configured for every
        minute actually ran every two — and arb episodes last about a minute. It also makes
        TOMORROW's event free to watch on every pass instead of once an hour, and tomorrow's book
        is the thinner, worse-quoted one, i.e. where a set under $1 is MORE likely.

        Returns how many events were cached. Any failure leaves the cache empty and every caller
        falls through to the per-event path, so this can make a pass cheaper but never wronger.
        """
        self._prefetch.clear()
        if not self.available:
            return 0
        want = []
        for city_slug, date, kind in triples:
            slug = us_event_slug(city_slug, date, kind)
            retry_at = self._missing.get(slug)
            if retry_at and time.time() < retry_at:
                continue          # known 404 this hour — a batch slot is cheap, not free
            if slug not in want:
                want.append(slug)
        if not want:
            return 0
        now = time.time()
        for slug, e in self.events_by_slug(want).items():
            if e.get("markets"):
                self._prefetch[slug] = (now, e)
        return len(self._prefetch)

    def find_weather_event(self, city_slug: str, date: datetime, kind: str = "high") -> WeatherEvent | None:
        """The US venue's own temperature event for a city/day, or None. Slug first, then search."""
        if not self.available:
            return None
        slug = us_event_slug(city_slug, date, kind)
        retry_at = self._missing.get(slug)
        if retry_at and time.time() < retry_at:
            return None
        hit = self._prefetch.get(slug)
        if hit and time.time() - hit[0] <= EVENT_PREFETCH_TTL_S:
            return weather_event_from_us(hit[1], city_slug, kind)
        e = None
        try:
            self._space()
            e = _unwrap_event(self._client.events.retrieve_by_slug(slug))
        except Exception as exc:
            if _is_rate_limited(exc):
                self._enter_backoff(exc)
                return None
            if not _is_not_found(exc):
                raise
        if not e or not e.get("markets"):
            word = "Highest" if kind == "high" else "Lowest"
            query = (config.city_meta(city_slug) or {}).get("query", city_slug)
            try:
                self._space()
                res = self._client.search.query({"query": f"{word} temperature in {query}"}) or []
            except Exception as exc:
                if _is_rate_limited(exc):
                    self._enter_backoff(exc)
                    return None
                raise
            items = res if isinstance(res, list) else (res.get("events") or res.get("results") or [])
            e = next((x for x in items if isinstance(x, dict) and x.get("slug") == slug), None)
        if not e:
            # Polymarket US lists a HIGH market per city per day and no LOW market at all, so the
            # `low` half of every scan was two wasted calls per city per hour against the host that
            # rate-limits us. Remember the miss for an hour instead of asking again every tick; an
            # hour is short enough to pick up a market the venue posts later in the day.
            self._missing[slug] = time.time() + MISSING_EVENT_RETRY_S
            return None
        self._missing.pop(slug, None)
        return weather_event_from_us(e, city_slug, kind)

    def event(self, slug: str):
        """One event by slug, envelope unwrapped, closed events included."""
        if not self.available:
            return None
        try:
            self._space()
            return _unwrap_event(self._client.events.retrieve_by_slug(slug))
        except Exception as exc:
            if _is_rate_limited(exc):
                self._enter_backoff(exc)
                return None
            if _is_not_found(exc):
                return None
            raise

    def _read_depth(self, b) -> bool:
        """Ladders and top-of-book sizes for ONE leg. False if the book could not be read."""
        book = self.book(b.yes_token, priority=True)
        if not book:
            b.bid_qty = b.ask_qty = None
            return False
        bids, asks = book.get("bids") or [], book.get("asks") or []
        b.bid_levels, b.ask_levels = bids, asks
        b.bid_qty = sum(q for px, q in bids if px == bids[0][0]) if bids else 0.0
        b.ask_qty = sum(q for px, q in asks if px == asks[0][0]) if asks else 0.0
        # Once the book has answered, the book is the truth. Leaving the event object's stale
        # quote in place when the ladder comes back EMPTY is the phantom-edge trap wearing a
        # new hat: miahigh's "92 or above" showed ask=0.04 from the event and no offers at all
        # in the book on 2026-09-19, and arb_check duly reported a 4.4c buy-all on a leg that
        # could not be bought at any price.
        b.best_bid = bids[0][0] if bids else None
        b.best_ask = asks[0][0] if asks else None
        return True

    def fill_depth_buckets(self, buckets, retries: int = 1) -> bool:
        """`fill_depth` for a bare list of legs (the universe path has no WeatherEvent).

        One unreadable leg fails the whole set, and that is correct -- sizing off a partly-unknown
        book is how you buy five legs and discover the sixth was never fillable. But it makes a
        single dropped HTTP call as expensive as a missing market, and on 2026-09-18 that is
        exactly what happened:

            13:55:58  arb candidate us chicago buy_all 9.4c/set — depth INCOMPLETE, standing down

        That book was the best one on record. Five legs had thousands of contracts behind them and
        the binding leg had 21, at an ask_sum of 0.82: 21 sets at 17.5c, about $3.68 -- 72% of all
        the profit this strategy has ever found, in one minute. It was refused because the sixth
        leg's book call came back empty once.

        book() fails on exactly one path -- the quota. So "ask again straight away" would be
        useless, because the venue is in backoff and the second call fails identically. What
        rescues the set is serving the cooldown and then asking: 15s of a held scan pass against
        a $3.23 set, capped so a long ban is left to the next sweep instead of stalling this one.

        The all-or-nothing rule is unchanged. A leg that still will not answer stands the whole
        set down, because a set sized off a partly-unknown book is how you buy five legs and find
        the sixth was never fillable. What changes is how hard we try before giving up.
        """
        missed = [b for b in buckets if not self._read_depth(b)]
        for _ in range(max(0, retries)):
            if not missed:
                break
            wait = self._backoff_until - time.time()
            if wait > 0:
                if wait > DEPTH_RETRY_WAIT_MAX_S:
                    break                 # too long to hold the pass; the next sweep can have it
                if not TEST_MODE:
                    time.sleep(wait + 0.05)
                self._backoff_until = 0.0            # the cooldown has been served
            missed = [b for b in missed if not self._read_depth(b)]
        return not missed

    def fill_depth(self, event) -> bool:
        """Put real top-of-book sizes on an event's buckets. One call per bucket, so the caller
        only spends it when the quotes already say an arb might be there (over 8 days of US
        snapshots that was 34 event-minutes out of 3,353). Returns False if any leg could not be
        read — a set sized off a partly-unknown book is the thing we are trying not to do."""
        if not self.available:
            return False
        return self.fill_depth_buckets(event.buckets)

    def discover(self, queries) -> dict:
        """{slug: event} for every open non-sports event with 2+ markets the queries can reach.

        `events.list` answers only sports and ignores tagSlug, so search is the only door to the
        rest of the catalogue. One call per query.
        """
        found = {}
        for q in queries:
            res = self.search(q)
            if not res:
                continue
            items = res if isinstance(res, list) else (res.get("events") or res.get("results") or [])
            for e in items:
                if not isinstance(e, dict) or not e.get("slug"):
                    continue
                if len(e.get("markets") or []) < 2:
                    continue
                found[e["slug"]] = e
        return found

    def events_by_slug(self, slugs, batch: int = 20) -> dict:
        """{slug: event} for specific slugs, CLOSED ones included, many per call.

        `events.list` ignores seriesSlug and tagSlug (it answers sports whatever you ask), but it
        honours `slug=[...]` and returns closed events with their full markets. That is what lets
        the universe prove its own series: ask once for twenty slugs and see which have settled.
        """
        out = {}
        slugs = list(slugs)
        for i in range(0, len(slugs), batch):
            chunk = slugs[i:i + batch]
            r = self._guarded(lambda c=chunk: self._client.events.list({"slug": c, "limit": len(c)}), default=None)
            if not r:
                continue
            for e in (r.get("events", []) if isinstance(r, dict) else r):
                if isinstance(e, dict) and e.get("slug"):
                    out[e["slug"]] = e
        return out

    def price_legs(self, buckets, limit: int = 6) -> int:
        """Fill in best_bid/best_ask for buckets the event object left unquoted, from the book.

        One call per bucket, and the usual case is a single unquoted leg (1,329 of 1,570 candidate
        event-minutes over 9 days), so this is cheap where a full depth read is not. `limit` caps
        the damage on the rare event that is missing several. Returns how many were priced.
        """
        # All or nothing. arb_check needs EVERY leg quoted on the side it trades, so pricing four
        # legs of a six-leg book answers nothing and spends four calls doing it — which was 42% of
        # all screens (1,049 of 2,488 event-minutes), almost all of them books where every leg has
        # an ask and none has a bid, i.e. exactly the sell-side shape that is 5x the more common
        # arb. Spend six or spend none.
        need = [b for b in buckets if b.best_ask is None or b.best_bid is None]
        if len(need) > limit:
            return 0
        done = 0
        for b in need:
            book = self.book(b.yes_token, max_age_s=SCREEN_BOOK_TTL_S)
            if book is None:
                continue                      # rate limited: leave it unquoted, it stays a no-go
            bids, asks = book.get("bids") or [], book.get("asks") or []
            if asks:
                b.best_ask = asks[0][0]
                b.ask_qty = sum(q for px, q in asks if px == asks[0][0])
            else:
                b.ask_qty = 0.0               # genuinely nobody offering — the leg is unbuyable
            if bids:
                b.best_bid = bids[0][0]
                b.bid_qty = sum(q for px, q in bids if px == bids[0][0])
            done += 1
        return done

    def resolution(self, slug: str) -> int | None:
        """1/0 once the market settled, else None. Settlement is a 404 until it exists.

        A priority call. Settling is what turns a paper record into evidence, and a settle that
        runs out of budget half way leaves an arb set part-closed — which does not merely delay
        the answer, it reports a profitable set as a loss until the rest catches up.
        """
        if not self.available:
            return None
        try:
            self._space(priority=True)
            s = self._client.markets.settlement(slug) or {}
        except Exception as exc:
            if _is_rate_limited(exc):
                self._enter_backoff(exc)
                return None
            if _is_not_found(exc):
                return None
            raise
        s = _unwrap(s)
        # The live endpoint answers {"slug": ..., "settlement": 0|1} — a bare number under
        # `settlement`, not the `settlementPrice` Amount the SDK's type hints promise. Reading only
        # the hinted names meant resolution() returned None for markets that had plainly RESOLVED,
        # so no US paper trade ever closed: they sat marked-to-market forever, the gate's "US paper
        # has closed trades and is in profit" clause could never be satisfied, and NO module could
        # ever be promoted. This was the hard blocker on going live at all, and it read as a rate
        # limit for days. Note `settlement` is legitimately 0, so it cannot be chained with `or`.
        for key in ("settlement", "settlementPx", "settlementPrice", "price", "px"):
            if s.get(key) is not None:
                px = _price(s[key])
                if px is not None:
                    return 1 if px >= 0.99 else 0 if px <= 0.01 else None
        for k in ("result", "outcome", "winningOutcome", "winner"):
            v = str(s.get(k, "")).lower()
            if v in ("yes", "long", "1"):
                return 1
            if v in ("no", "short", "0"):
                return 0
        return None

    # ---- account -------------------------------------------------------------------------
    def balance_usd(self) -> float | None:
        """Buying power (cash + any promo credit the venue lets you trade with)."""
        if not self.available:
            return None
        b = self._guarded(lambda: self._client.account.balances(), default={}) or {}
        rows = b.get("balances") if isinstance(b, dict) else b
        row = (rows or [{}])[0] if isinstance(rows, list) else (rows or {})
        for k in ("buyingPower", "currentBalance", "available", "cash", "balance", "total"):
            if row.get(k) is not None:
                return _price(row[k])
        return None

    def account_value_usd(self) -> float | None:
        """Cash buying power PLUS what is already sitting in open positions.

        `balance_usd` returns buying power, which is what you can deploy right now — and on
        2026-09-16 that read $0.41 while the account actually held ~$187 in two open positions.
        Sizing and the bankroll floor must use account VALUE, or the bot halts the moment money is
        working ("bankroll under floor") and never compounds. Alex's rule: you start at $200, and
        what you make becomes what you trade with."""
        if not self.available:
            return None
        cash = self.balance_usd() or 0.0
        held = 0.0
        for p in (self.positions() or []):
            cost = p.get("cost") if isinstance(p, dict) else None
            val = (cost or {}).get("value") if isinstance(cost, dict) else None
            if val is not None:
                try:
                    held += float(val)
                except (TypeError, ValueError):
                    pass
        return round(cash + held, 2)


    def balance_detail(self) -> dict:
        if not self.available:
            return {}
        b = self._guarded(lambda: self._client.account.balances(), default={}) or {}
        rows = b.get("balances") if isinstance(b, dict) else b
        row = (rows or [{}])[0] if isinstance(rows, list) else (rows or {})
        return {k: row.get(k) for k in ("buyingPower", "currentBalance", "displayedCash", "bonusReservation",
                                        "openOrders", "availableToWithdraw") if k in row}

    def positions(self):
        if not self.available:
            return []
        d = self._guarded(lambda: self._client.portfolio.positions(), default={}) or {}
        if isinstance(d, list):
            return d
        pos = d.get("positions")
        if isinstance(pos, dict):
            pos = list(pos.values())
        return list(pos or []) or list(d.get("availablePositions") or [])

    # ---- orders (limit only) -------------------------------------------------------------
    INTENTS = {"BUY_YES": "ORDER_INTENT_BUY_LONG", "BUY_NO": "ORDER_INTENT_BUY_SHORT",
               "SELL_YES": "ORDER_INTENT_SELL_LONG", "SELL_NO": "ORDER_INTENT_SELL_SHORT"}

    TIF = {"gtc": "TIME_IN_FORCE_GOOD_TILL_CANCEL",
           "ioc": "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL",
           "fok": "TIME_IN_FORCE_FILL_OR_KILL"}

    def place_limit(self, slug: str, side: str, price: float, contracts: int, tif: str = "gtc"):
        """side: BUY_YES (open long) · BUY_NO (open short) · SELL_YES / SELL_NO (close). Limit only.
        The SELL_* intent names follow the SDK's BUY_LONG/BUY_SHORT pattern and are unverified until
        the first live take-profit; the executor logs the venue's reply either way.

        `tif='fok'` is what an arb leg wants: fill the whole quantity at my price or do not exist.
        A partly-filled leg is the worst outcome for a set — you pay for an unbalanced basket AND
        still have a resting order that may fill later at a price that is no longer part of any arb.
        """
        if not self.available:
            raise RuntimeError(self.why_unavailable)
        # Deliberately NOT spaced through the token bucket: an arb set is six orders that have to
        # land together, and a leg waiting a full window is a leg that fills into a book which has
        # moved. That looked dangerous — the depth read just spent the budget, so six unthrottled
        # requests on top should have been refused around the sixth.
        #
        # It is not, because orders do not use the rate-limited host. The SDK routes authenticated
        # calls to api.polymarket.us and market data to gateway.polymarket.us, and only the
        # gateway is the Cloudflare front that throttles this campus IP. Measured 2026-09-19:
        # eight back-to-back authenticated calls with no spacing, zero refusals, while the gateway
        # still refuses the sixth. The order burst and the screening budget are separate.
        body = {
            "marketSlug": slug,
            "intent": self.INTENTS[side],
            "type": "ORDER_TYPE_LIMIT",
            "price": {"value": f"{price:.2f}", "currency": "USD"},
            "quantity": int(contracts),
            "tif": self.TIF[tif],
        }
        if tif in ("fok", "ioc"):
            # An immediate order's whole point is that its outcome is known at once, and
            # `fill_result` reads that outcome out of the response's `executions`. Without this
            # the reply can come back before the venue has decided, which reads as "nothing
            # filled" — and the caller would then believe it holds none of a set it actually
            # bought. A resting GTC order is deliberately left alone: it has no immediate
            # outcome to wait for. (SDK CreateOrderParams field; unverified live until the first
            # real arb, which is why the executor logs every raw reply.)
            body["synchronousExecution"] = True
        return self._client.orders.create(body)

    def cancel(self, order_id: str, slug: str):
        return self._client.orders.cancel(order_id, {"marketSlug": slug}) if self.available else None

    def cancel_all(self):
        return self._client.orders.cancel_all() if self.available else None

    def open_orders(self):
        if not self.available:
            return []
        d = self._orders_list()
        if isinstance(d, list):
            return d
        return list((d or {}).get("orders") or [])

    def _orders_list(self):
        if not self.available:
            return []
        return self._guarded(lambda: self._client.orders.list(), default=[])

    def close(self):
        if self._client:
            self._client.close()


def _price(x):
    if x is None:
        return None
    if isinstance(x, dict):
        x = x.get("value", x.get("price"))
    try:
        return float(x)
    except (TypeError, ValueError):
        return None
