"""The tradable universe: which Polymarket US events are one-winner sets worth arbing.

bucket_sum only works on a group of markets where EXACTLY ONE pays $1. On weather that is
provable — the buckets tile every integer degree — and `strategies.bucket_sum.exhaustive` proves
it. But weather is ten markets a day inside a six-hour window, which is why the bot gets ~2
signals a day. The same arb applies to every other multi-outcome event on the venue, and there
are far more of those: one pass of 15 searches on 2026-09-18 found 37 open non-sports events with
2+ markets (Fed decisions, central banks, elections, Oscars, Nobel, crypto ranges).

The danger in generalising is precise: if the outcome set is NOT exhaustive, buying every leg for
under $1 is not an arb, it is a bet that pays nothing when the real world picks "none of these".
The venue is full of ladders that look like sets and are not — "CPI YoY above 2.0 / above 2.5 /
above 3.0" are all simultaneously true or false, and their prices sum to 7.12, not 1.00.

So membership is tiered, and only the proven tier may ever trade:

  TIER_PROVEN    the markets tile a number line (weather), or a SETTLED instance of the same
                 series was observed to have exactly one winner. Tradable.
  TIER_WATCH     the outcome prices sum to ~1.00, which is the market itself saying "these are a
                 partition and nothing else can happen". Strong evidence, not proof. Recorded and
                 measured, never traded, and promoted to PROVEN the first time an instance of its
                 series settles with exactly one winner.
  TIER_REJECT    anything else, most importantly the ladders.

`mid_sum` is the discriminator, and it is a real test rather than a vibe: if a meaningful "none of
the above" outcome existed, the legs could not sum to 1.
"""
from __future__ import annotations

import json
import re
import time

TIER_PROVEN, TIER_WATCH, TIER_REJECT = "proven", "watch", "reject"

# How far the outcome prices may stray from $1 and still look like a partition. Wide enough for
# a thin book's marks, tight enough to exclude the ladders (which sum to 4-8).
PARTITION_BAND = (0.94, 1.06)
MIN_LEGS = 2
MAX_LEGS = 40          # 31 Nobel legs cost ~6c of taker fees per set; past that the fees eat any arb

_SERIES_DATE = re.compile(r"-(\d{4}-\d{2}-\d{2})$")


def series_key(slug: str) -> str:
    """'usfed-fomc-2026-10-28' -> 'usfed-fomc'. What repeats is what can be verified once and
    trusted next time; a slug with no date is its own series."""
    return _SERIES_DATE.sub("", slug or "")


def outcome_price(market: dict) -> float | None:
    try:
        return float(json.loads(market.get("outcomePrices") or "[]")[0])
    except (ValueError, IndexError, TypeError):
        return None


def price_sum(markets: list) -> float | None:
    """Sum of the venue's own YES marks. None if any leg has no mark at all."""
    prices = [outcome_price(m) for m in markets]
    if not prices or any(p is None for p in prices):
        return None
    return round(sum(prices), 4)


def classify(event: dict, proven_series: set | None = None) -> tuple[str, str]:
    """(tier, why) for one event dict from search/events."""
    markets = event.get("markets") or []
    n = len(markets)
    if n < MIN_LEGS:
        return TIER_REJECT, f"{n} market(s)"
    if n > MAX_LEGS:
        return TIER_REJECT, f"{n} legs — taker fees alone exceed any plausible edge"
    if (event.get("category") or "").lower() == "sports":
        return TIER_REJECT, "sports (Ohio)"
    if series_key(event.get("slug") or "") in (proven_series or set()):
        return TIER_PROVEN, "a settled instance of this series had exactly one winner"
    s = price_sum(markets)
    if s is None:
        return TIER_REJECT, "a leg has no mark"
    if PARTITION_BAND[0] <= s <= PARTITION_BAND[1]:
        return TIER_WATCH, f"outcome prices sum to {s:.2f} — a partition, but unproven"
    return TIER_REJECT, f"outcome prices sum to {s:.2f} — not a partition (ladder?)"


def settled(event: dict) -> bool:
    """Has this event actually RESOLVED — as opposed to merely being priced like a foregone
    conclusion?

    This distinction is the whole safety of the registry. Prices alone cannot tell them apart: an
    uncontested 2026-11-03 race sits at 0.99/0.01 six weeks before anyone votes, which reads
    exactly like a settled market. Proving a series off that would mark it tradable on evidence
    that does not exist yet — and `usltgov-tx` and `usltgov-vt` did precisely that on the first
    run. Settlement is a fact the venue reports, so ask it rather than inferring it.
    """
    if not event.get("closed"):
        return False
    markets = event.get("markets") or []
    if not markets:
        return False
    return all(m.get("closed") or str(m.get("status", "")).upper().endswith(("RESOLVED", "SETTLED"))
               for m in markets)


def one_winner(markets: list) -> bool | None:
    """Did exactly one leg pay $1? Only meaningful once `settled(event)` is True — on its own this
    is a statement about PRICES, not outcomes."""
    prices = [outcome_price(m) for m in markets]
    if not prices or any(p is None for p in prices):
        return None
    if not all(p >= 0.99 or p <= 0.01 for p in prices):
        return None                       # still trading
    return sum(1 for p in prices if p >= 0.99) == 1


# `events.list` returns only sports and ignores tagSlug, so the non-sports catalogue is reachable
# only through search. These queries are the net; each costs one API call, and the whole sweep is
# ~20 calls, which is why it runs twice a day rather than every tick.
DISCOVERY_QUERIES = [
    # Recurring macro is the richest seam: every one of these that a settled instance proves
    # becomes a permanently tradable 5-to-9-leg market with a tight book, and central banks meet
    # on a schedule forever. usfed-fomc and banxico both came from this list.
    "Fed decision", "interest rate decision", "central bank", "ECB", "Bank of England",
    "Bank of Japan", "Banxico", "rate cut", "rate hike",
    "CPI", "inflation", "PCE", "unemployment", "jobs report", "payrolls", "jobless claims",
    "GDP", "retail sales", "recession", "government shutdown", "debt ceiling",
    "election winner", "Senate", "Governor", "Mayor", "Supreme Court",
    "Bitcoin price", "Ethereum price",
    "Oscar winner", "Nobel", "Grammy", "Person of the Year",
]


SCHEMA = """
CREATE TABLE IF NOT EXISTS universe (
    slug TEXT PRIMARY KEY,
    series TEXT NOT NULL,
    category TEXT,
    title TEXT,
    n_markets INTEGER,
    price_sum REAL,
    tier TEXT NOT NULL,
    why TEXT,
    first_seen REAL,
    last_seen REAL
);
CREATE TABLE IF NOT EXISTS series_proof (
    series TEXT PRIMARY KEY,
    proved_by TEXT,        -- the settled event slug that showed exactly one winner
    proved_ts REAL
);
"""


class Universe:
    """The registry, in the same sqlite file as everything else."""

    def __init__(self, conn):
        self.conn = conn
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def proven_series(self) -> set:
        return {r[0] for r in self.conn.execute("SELECT series FROM series_proof")}

    def record(self, event: dict) -> tuple[str, str]:
        tier, why = classify(event, self.proven_series())
        slug = event.get("slug") or ""
        now = time.time()
        self.conn.execute(
            """INSERT INTO universe (slug, series, category, title, n_markets, price_sum, tier, why,
                                     first_seen, last_seen) VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(slug) DO UPDATE SET n_markets=excluded.n_markets, price_sum=excluded.price_sum,
                   tier=excluded.tier, why=excluded.why, last_seen=excluded.last_seen""",
            (slug, series_key(slug), event.get("category"), event.get("title"),
             len(event.get("markets") or []), price_sum(event.get("markets") or []), tier, why, now, now))
        self.conn.commit()
        return tier, why

    def prove(self, event: dict) -> bool:
        """A SETTLED event with exactly one winner promotes its whole series to PROVEN."""
        slug = event.get("slug") or ""
        markets = event.get("markets") or []
        if not settled(event) or one_winner(markets) is not True:
            return False
        series = series_key(slug)
        if series in self.proven_series():
            return False          # already known; `INSERT OR IGNORE` would report a success it did not have
        self.conn.execute("INSERT OR IGNORE INTO series_proof (series, proved_by, proved_ts) VALUES (?,?,?)",
                          (series, slug, time.time()))
        self.conn.execute("UPDATE universe SET tier=?, why=? WHERE series=? AND tier=?",
                          (TIER_PROVEN, f"series proved by {slug}", series, TIER_WATCH))
        self.conn.commit()
        return True

    def unproven_slugs(self) -> list:
        """WATCH events whose series has no proof yet — the ones worth re-checking for settlement."""
        proven = self.proven_series()
        return [r[0] for r in self.conn.execute(
            "SELECT slug, series FROM universe WHERE tier=? ORDER BY last_seen", (TIER_WATCH,))
            if r[1] not in proven]

    def dated_unproven_series(self) -> list:
        """Recurring series (their slug carries a date) with no proof yet — the ones a sweep of
        past dates can settle today instead of in weeks."""
        proven = self.proven_series()
        out = []
        for series, slug in self.conn.execute(
                "SELECT series, slug FROM universe WHERE tier=? GROUP BY series", (TIER_WATCH,)):
            if series not in proven and _SERIES_DATE.search(slug or ""):
                out.append(series)
        return out

    def tradable(self) -> list:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM universe WHERE tier=? ORDER BY n_markets", (TIER_PROVEN,))]

    def watching(self) -> list:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM universe WHERE tier=? ORDER BY n_markets", (TIER_WATCH,))]

    def report(self) -> str:
        lines = ["universe — Polymarket US multi-outcome events"]
        for tier in (TIER_PROVEN, TIER_WATCH, TIER_REJECT):
            rows = [dict(r) for r in self.conn.execute(
                "SELECT * FROM universe WHERE tier=? ORDER BY n_markets DESC", (tier,))]
            lines.append(f"  {tier.upper()}: {len(rows)}")
            for r in rows[:12 if tier != TIER_REJECT else 4]:
                lines.append(f"    {r['slug'][:38]:<40}{str(r['category'])[:8]:<9}"
                             f"{r['n_markets']:>3} legs  sum={r['price_sum'] or 0:.2f}  {r['why'][:46]}")
            if len(rows) > (12 if tier != TIER_REJECT else 4):
                lines.append(f"    ... and {len(rows) - (12 if tier != TIER_REJECT else 4)} more")
        proofs = list(self.conn.execute("SELECT series, proved_by FROM series_proof"))
        lines.append(f"  proven series: {len(proofs)}" + (
            " — " + ", ".join(f"{s} (by {b})" for s, b in proofs[:6]) if proofs else " (none yet)"))
        return "\n".join(lines)
