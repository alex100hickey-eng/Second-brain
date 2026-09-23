"""leadlag's universe and its price recorder.

`pairs.json` maps each Polymarket US market to the offshore market that asks the same question, and
`PairRecorder` samples both sides on a seconds clock so leadlag has two price paths to compare.

Why this took three pieces rather than one file:

1. `pairs.json` never existed. The builder asked `events.list` for the US catalogue, and that call
   answers SPORTS unless it is given `categories=[...]` (it ignores `tagSlug`, which is what was
   tried) — so every pair it could have found was a sports pair, and leadlag skips sports (Ohio).
   It was also scheduled for 05:00, when this Mac is asleep and DNS is down.
2. Fuzzy title matching is the wrong tool on these venues. Polymarket US copies offshore titles
   almost word for word, so the hard cases are NEAR-identical titles that ask different questions:
   "Texas Senate Election: Tarrant County Winner" vs "...: Denton County Winner" scores 0.9 on a
   character ratio. So two titles match only when their CONTENT words are the same set, after
   dropping filler ("election", "winner", "which", "will") and folding party names and plurals.
3. Nothing ever recorded the prices leadlag compares. Its series came from the snapshot table,
   which only ever holds weather books, and its 120 s window needs samples closer together than
   the 5-minute scan that ran it — a 5-minute cadence could not see a 2-minute move by
   construction. The recorder samples every pair each minute: one batched US events call per 20
   events (the quota the arb sweep also spends) and one CLOB call for every offshore token.
"""
from __future__ import annotations

import json
import os
import re
import time
from collections import deque
from datetime import datetime
from difflib import SequenceMatcher

from . import config
from .feeds.usvenue import _market_closed

PAIRS_PATH = os.path.join(config.ROOT, "pairs.json")

_STOP = {"will", "the", "a", "an", "of", "in", "on", "at", "to", "be", "by", "for", "vs", "and", "or",
         "who", "which", "what", "election", "elections", "winner", "win", "midterm", "midterms",
         "after", "us", "u", "s", "party", "is", "it", "next"}
_MONTHS = {"january", "february", "march", "april", "may", "june", "july", "august", "september",
           "october", "november", "december"}
_SYN = {"democratic": "dem", "democrats": "dem", "democrat": "dem", "dems": "dem",
        "republican": "rep", "republicans": "rep", "reps": "rep", "gop": "rep",
        "decrease": "cut", "decreases": "cut", "increase": "hike", "increases": "hike"}
_YEAR = re.compile(r"^20\d\d$")


def _stem(w: str) -> str:
    if len(w) > 3 and w.isalpha() and w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


def tokens(text: str) -> list:
    """Content words, folded: 'Will the Fed cut rates in September?' -> ['fed', 'cut', 'rate', 'september']."""
    t = (text or "").lower().replace("u.s.", "us").replace("u.s", "us").replace("&", " and ")
    t = re.sub(r"(?<=\d),(?=\d{3})", "", t)            # 85,000 -> 85000
    t = re.sub(r"[^a-z0-9%+.≤≥<>\- ]+", " ", t)
    out = []
    for w in t.split():
        w = w.strip(".-")
        if not w or w in _STOP:
            continue
        w = _SYN.get(w, w)
        out.append(_stem(w))
    return out


def same_question(a: str, b: str) -> bool:
    """True when two titles ask the same thing: equal content-word sets. A year only counts when both
    titles carry one ("2026 Nobel Peace Prize Winner" == "Nobel Peace Prize Winner 2026", but
    "...2026" != "...2027"); months, numbers, places and names always count."""
    ta, tb = set(tokens(a)), set(tokens(b))
    ya, yb = {w for w in ta if _YEAR.match(w)}, {w for w in tb if _YEAR.match(w)}
    if not (ya and yb):
        ta, tb = ta - ya, tb - yb
    return bool(ta) and ta == tb


def normalize(title: str) -> str:
    return " ".join(tokens(title))


def _key(title: str) -> frozenset:
    """Index key: the content words without years, so a lookup finds every title same_question()
    could accept and the year rule is applied after."""
    return frozenset(w for w in tokens(title) if not _YEAR.match(w))


def _date(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def match_pairs(us_markets: list, offshore_markets: list, threshold: float = 0.72, max_days_apart: float = 2.0) -> list:
    """Flat lists, one question per entry. us: [{slug, title, end, category?}] · offshore:
    [{token, title, end, category}] → [{us_slug, offshore_token, label, category, score}].

    Same-question first; the character ratio only breaks a tie between equal content sets (and
    must still clear `threshold`). Dates within `max_days_apart` when both sides have one."""
    out = []
    for u in us_markets:
        du = _date(u.get("end"))
        best, best_score = None, 0.0
        for m in offshore_markets:
            dm = _date(m.get("end"))
            if du and dm and abs((du - dm).total_seconds()) > max_days_apart * 86400:
                continue
            if not same_question(u.get("title", ""), m.get("title", "")):
                continue
            score = SequenceMatcher(None, normalize(u.get("title", "")), normalize(m.get("title", ""))).ratio()
            if score > best_score:
                best, best_score = m, score
        if best and best_score >= threshold:
            out.append({"us_slug": u["slug"], "offshore_token": best["token"], "label": u.get("title", u["slug"]),
                        "category": u.get("category") or best.get("category", "other"), "score": round(best_score, 3)})
    return out


# ---- event-level matching (the real builder) ------------------------------------------------
def _us_quote(m: dict):
    def px(q):
        try:
            return float((q or {}).get("value"))
        except (TypeError, ValueError):
            return None
    return px(m.get("bestBidQuote")), px(m.get("bestAskQuote"))


def _mid(bid, ask):
    if bid is not None and ask is not None:
        return (bid + ask) / 2
    return None


def _off_market_ok(m: dict):
    """(yes_token, label, price) for a live offshore market whose first outcome is YES, else None."""
    if m.get("closed") or m.get("active") is False or m.get("acceptingOrders") is False:
        return None
    try:
        toks = json.loads(m.get("clobTokenIds") or "[]")
        outs = json.loads(m.get("outcomes") or "[]")
    except (TypeError, ValueError):
        return None
    if not toks or (outs and str(outs[0]).lower() != "yes"):
        return None
    bid, ask = _f(m.get("bestBid")), _f(m.get("bestAsk"))
    price = _mid(bid, ask)
    if price is None:
        try:
            price = float(json.loads(m.get("outcomePrices") or "[]")[0])
        except (TypeError, ValueError, IndexError):
            price = None
    return toks[0], (m.get("groupItemTitle") or m.get("question") or ""), price


def _f(x):
    try:
        return None if x is None else float(x)
    except (TypeError, ValueError):
        return None


# Recording order. Politics first (with macro: a Fed decision is the same kind of news-driven book),
# then the rest. Inside a rank the busiest offshore reference goes first — a reference that never
# moves can never lead anything.
CATEGORY_RANK = {"politics": 0, "macro": 0, "geopolitics": 1, "culture": 2, "finance": 3, "crypto": 4}
# Two venues pricing the same YES more than this far apart is a mismatched pair (a NO token, a
# different threshold, a different race), not a lag worth trading: a real lag closes within minutes.
MAX_PRICE_GAP = 0.25
TIGHT_SPREAD = 0.04          # a US book this tight can pass leadlag's "gap outside the spread" test


def match_events(us_events: list, off_events: list, max_gap: float = MAX_PRICE_GAP) -> tuple[list, dict]:
    """Pair US markets with offshore markets asking the same question.

    An event pair needs same_question() on the titles; within it, a market pair needs the same on
    the outcome labels ("James Talarico (D)", "No Change", "48"). A US event with a single market
    may match an offshore single-market question on title + label ("Government Shutdown?" / "By
    October 1, 2026" == "Government shutdown by October 1?"). Returns (pairs, counts)."""
    idx: dict = {}
    single_idx: dict = {}
    for e in off_events:
        idx.setdefault(_key(e.get("title", "")), []).append(e)
        live = [m for m in e.get("markets") or [] if _off_market_ok(m)]
        if len(live) == 1:
            for text in {e.get("title", ""), _off_market_ok(live[0])[1]}:
                single_idx.setdefault(_key(text), []).append((e, live[0], text))
    counts = {"us_events": len(us_events), "events_matched": 0, "pairs": 0, "gap_rejected": 0}
    out = []
    for ue in us_events:
        u_markets = [m for m in ue.get("markets") or [] if m.get("slug") and not _market_closed(m)]
        if not u_markets:
            continue
        cands = [e for e in idx.get(_key(ue.get("title", "")), [])
                 if same_question(ue.get("title", ""), e.get("title", ""))]
        du = _date(ue.get("endDate"))
        # Several offshore events can ask the same thing (a relisted market): take the nearest end date.
        cands.sort(key=lambda e: abs((_date(e.get("endDate")) - du).total_seconds())
                   if du and _date(e.get("endDate")) else 9e12)
        found = []
        if cands:
            oe = cands[0]
            offs = [(m, _off_market_ok(m)) for m in oe.get("markets") or []]
            offs = [(m, ok) for m, ok in offs if ok]
            for um in u_markets:
                label = um.get("title") or um.get("titleShort") or ""
                hits = [(m, ok) for m, ok in offs if same_question(label, ok[1])]
                if len(u_markets) == 1 and len(offs) == 1 and not hits:
                    hits = offs                   # one question on each side, and the events match
                if len(hits) == 1:
                    found.append((ue, um, oe, hits[0][0], hits[0][1]))
        elif len(u_markets) == 1:
            um = u_markets[0]
            q = f"{ue.get('title', '')} {um.get('title') or ''}"
            hits = {str(m.get("id")): (e, m) for e, m, text in single_idx.get(_key(q), []) if same_question(q, text)}
            if len(hits) == 1:
                e, m = next(iter(hits.values()))
                found.append((ue, um, e, m, _off_market_ok(m)))
        if found:
            counts["events_matched"] += 1
        for ue_, um, oe, om, (tok, olabel, oprice) in found:
            bid, ask = _us_quote(um)
            umid = _mid(bid, ask)
            if umid is not None and oprice is not None and abs(umid - oprice) > max_gap:
                counts["gap_rejected"] += 1
                continue
            out.append({"us_slug": um["slug"], "us_event": ue_.get("slug"), "offshore_token": tok,
                        "offshore_market_id": str(om.get("id") or ""), "offshore_event": oe.get("slug"),
                        "label": f"{ue_.get('title', '')}: {um.get('title') or ''}".strip(": "),
                        "offshore_label": olabel,
                        "category": (ue_.get("category") or "other").lower(),
                        "volume24h": _f(oe.get("volume24hr")) or 0.0,
                        "us_mid": umid, "offshore_mid": oprice,
                        "us_spread": None if umid is None else round(ask - bid, 4)})
    counts["pairs"] = len(out)
    # An event whose US book showed no two-sided quote at build time has nothing to record: the
    # recorder's mid would be empty every tick. Those go last, whatever their category. Within a
    # category, events with more TIGHT US books go first: leadlag refuses any gap inside the US
    # spread, so a 38c-wide book (Brazil's first round, 2026-09-23) can be recorded forever and never
    # produce a signal, while it takes one of the recorder's 40 slots.
    quoted = {p["us_event"] for p in out if p["us_mid"] is not None}
    tight: dict = {}
    for p in out:
        if p["us_spread"] is not None and p["us_spread"] <= TIGHT_SPREAD:
            tight[p["us_event"]] = tight.get(p["us_event"], 0) + 1
    out.sort(key=lambda p: (p["us_event"] not in quoted, CATEGORY_RANK.get(p["category"], 9),
                            -tight.get(p["us_event"], 0), -p["volume24h"], p["us_event"] or "", p["us_slug"]))
    counts["quoted_events"] = len(quoted)
    return out, counts


def save_pairs(pairs: list, path: str | None = None) -> str:
    path = path or PAIRS_PATH
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(pairs, f, indent=1)
    os.replace(tmp, path)
    return path


def load_pairs(path: str | None = None) -> list:
    path = path or PAIRS_PATH
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return json.load(f)


def pairs_age_s(path: str | None = None) -> float | None:
    path = path or PAIRS_PATH
    return time.time() - os.path.getmtime(path) if os.path.exists(path) else None


# ---- the recorder ---------------------------------------------------------------------------
# leadlag measures a move from the OLDEST sample inside its 120 s window. At a 60 s cadence the
# ticks drift a fraction of a second late, so the sample 120 s back always fell just outside and the
# rule was really measuring 60 s moves (2026-09-23 10:57: governorships ref +3.5c over 120 s, +1.5c
# over 60 s — no signal). 40 s puts two samples back inside every window.
RECORD_INTERVAL_S = 40.0
RECORD_MAX_EVENTS = 40       # 2 batched US calls a sample (~3 a minute), on a quota the arb sweep shares
SERIES_WINDOW_S = 900
# An unchanged quote is re-written this often anyway. Paper closes a leadlag position at the first
# price on record after its 6 h horizon; with changes only, a quiet book's "first price after" could
# be a day later and not the price at the deadline at all.
SNAPSHOT_HEARTBEAT_S = 1800


class PairRecorder:
    """In-memory price paths for every pair, plus change-only snapshots in the ledger.

    Every sample stays in memory, because leadlag's move test needs a sample at both ends of its
    window; only CHANGES (plus a half-hourly heartbeat) go to the snapshot table, because that is
    all paper needs to fill and exit a US position and the table is already the bulk of a 64 MB
    database."""

    def __init__(self, ledger, us_venue, window_s: float = SERIES_WINDOW_S, max_events: int = RECORD_MAX_EVENTS,
                 clock=time.time, offshore_prices=None, log=None):
        self.ledger, self.us = ledger, us_venue
        self.window_s, self.max_events = window_s, max_events
        self.clock = clock
        self.log = log or (lambda *a: None)
        if offshore_prices is None:
            from .feeds import offshore
            offshore_prices = offshore.batch_prices
        self.offshore_prices = offshore_prices
        self.series: dict = {}
        self.quotes: dict = {}
        self._written: dict = {}

    def _push(self, venue, market, bid, ask, ts):
        mid = _mid(bid, ask)
        if mid is None:
            return
        s = self.series.setdefault((venue, market), deque())
        s.append((ts, mid))
        while s and s[0][0] < ts - self.window_s:
            s.popleft()
        last = self._written.get((venue, market))
        if last is None or last[:2] != (bid, ask) or ts - last[2] >= SNAPSHOT_HEARTBEAT_S:
            self._written[(venue, market)] = (bid, ask, ts)
            if self.ledger is not None:
                self.ledger.add_snapshot(venue, market, bid, ask, ts=ts)

    def get(self, venue, market) -> list:
        """SeriesStore interface: [(ts, mid)] over the window, oldest first."""
        return list(self.series.get((venue, market), ()))

    def quote(self, us_slug: str, max_age_s: float = 2 * RECORD_INTERVAL_S):
        """The recorder's latest US (bid, ask) — saves leadlag a book call per candidate."""
        hit = self.quotes.get(us_slug)
        if not hit or self.clock() - hit[0] > max_age_s:
            return None, None
        return hit[1], hit[2]

    def record(self, pairs: list) -> dict:
        """Sample both sides of the first `max_events` US events' worth of pairs."""
        ts = self.clock()
        events, chosen = [], []
        for p in pairs:
            ev = p.get("us_event")
            if not ev:
                continue
            if ev not in events:
                if len(events) >= self.max_events:
                    continue
                events.append(ev)
            chosen.append(p)
        got = {"events": len(events), "us": 0, "offshore": 0}
        if not chosen:
            return got
        us_quotes = {}
        if self.us is not None and self.us.available:
            try:
                got_events = self.us.events_by_slug(events) or {}
            except Exception as exc:            # a campus-wifi timeout costs this tick's US half, no more
                self.log(f"  pairs: US quotes failed ({type(exc).__name__})")
                got_events = {}
            for slug, e in got_events.items():
                for m in e.get("markets") or []:
                    if not m.get("slug"):
                        continue
                    if _market_closed(m):
                        # A US market that stops trading keeps publishing its last quotes. On
                        # 2026-09-23 "Trump Jr. for 2028 GOP VP" closed mid-day still showing 0.93/0.94,
                        # and leadlag read that against the 0.06 offshore price as an 87c edge. A
                        # closed book has no price: forget its path so nothing compares against it.
                        self.series.pop(("us", m["slug"]), None)
                        self.quotes.pop(m["slug"], None)
                        continue
                    us_quotes[m["slug"]] = _us_quote(m)
        try:
            off = self.offshore_prices([p["offshore_token"] for p in chosen]) or {}
        except Exception as exc:
            self.log(f"  pairs: offshore prices failed ({exc})")
            off = {}
        for p in chosen:
            bid, ask = us_quotes.get(p["us_slug"], (None, None))
            if bid is not None or ask is not None:
                self.quotes[p["us_slug"]] = (ts, bid, ask)
                self._push("us", p["us_slug"], bid, ask, ts)
                got["us"] += 1
            obid, oask = off.get(p["offshore_token"], (None, None))
            if obid is not None:
                self._push("offshore", p["offshore_token"], obid, oask, ts)
                got["offshore"] += 1
        return got
