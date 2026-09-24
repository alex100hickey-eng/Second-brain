"""The ledger: every signal, paper trade, order, snapshot and daily line, in sqlite.

This is the thing that promotes a module from paper → signal → live. Nothing else does.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from datetime import datetime, timezone

from . import config
from .fees import US_TAKER_THETA

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    module TEXT NOT NULL,
    venue TEXT NOT NULL,
    market TEXT NOT NULL,
    label TEXT,
    side TEXT NOT NULL,
    price REAL NOT NULL,
    size_usd REAL NOT NULL,
    contracts INTEGER NOT NULL,
    edge_cents REAL,
    reason TEXT,
    exit_rule TEXT,
    horizon_h REAL,
    mode TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    spread_cents REAL,
    category TEXT,
    meta TEXT
);
CREATE TABLE IF NOT EXISTS paper_trades (
    signal_id INTEGER PRIMARY KEY,
    filled_ts REAL,
    fill_price REAL,
    exit_ts REAL,
    exit_price REAL,
    exit_kind TEXT,
    outcome INTEGER,
    pnl_usd REAL,
    fees_usd REAL,
    mark_price REAL,
    status TEXT NOT NULL DEFAULT 'pending',
    note TEXT
);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id INTEGER,
    ts REAL NOT NULL,
    venue TEXT NOT NULL,
    market TEXT NOT NULL,
    venue_order_id TEXT,
    side TEXT,
    price REAL,
    contracts INTEGER,
    status TEXT,
    raw TEXT
);
CREATE TABLE IF NOT EXISTS snapshots (
    ts REAL NOT NULL,
    venue TEXT NOT NULL,
    market TEXT NOT NULL,
    bid REAL, ask REAL, mid REAL, last REAL,
    -- Contracts resting at the best price. NULL means the book was never asked (the normal case:
    -- quotes are free with the event, depth costs a call per bucket). Only the arb path fills
    -- these in, and they are the record that answers the one question about bucket_sum that
    -- history cannot: was the arb ever big enough to be worth taking?
    bid_qty REAL, ask_qty REAL,
    -- The WHOLE ladder as JSON [[px, qty], ...], best price first, for arb candidates only.
    -- Top-of-book alone cannot answer the question that decides whether this strategy is worth
    -- real money: the miami set on 2026-09-19 showed 13c of edge and ONE contract at the best
    -- ask, so the episode was worth 13 cents -- unless level two was also under $1, which
    -- bid_qty/ask_qty simply do not say. Sizing walks these levels; now the record keeps them.
    bid_ladder TEXT, ask_ladder TEXT
);
CREATE INDEX IF NOT EXISTS snapshots_idx ON snapshots (venue, market, ts);
CREATE TABLE IF NOT EXISTS model_runs (
    ts REAL NOT NULL,
    city TEXT NOT NULL,
    date TEXT NOT NULL,
    kind TEXT NOT NULL,
    probs TEXT NOT NULL
);
-- maker_rewards: the paper book's resting quotes (one row per market+side) and what they earned.
CREATE TABLE IF NOT EXISTS maker_quotes (
    market TEXT NOT NULL,
    side TEXT NOT NULL,             -- 'bid' | 'ask'
    px REAL,
    qty REAL,
    last_ts REAL,                   -- when this quote was last looked at (accrual runs from here)
    last_trade REAL,                -- the book's last print when we looked (a new print = a trade)
    filled_ts REAL,
    PRIMARY KEY (market, side)
);
CREATE TABLE IF NOT EXISTS maker_accrual (
    ts REAL NOT NULL,
    market TEXT NOT NULL,
    side TEXT NOT NULL,
    qty REAL,
    share REAL,                     -- our share of the side's qualified score on the book we read
    reward_usd REAL,                -- share x pool/2 x interval / day  (an ESTIMATE until live)
    qualified INTEGER,              -- did the side reach target size at all
    pool REAL,
    program TEXT
);
CREATE INDEX IF NOT EXISTS maker_accrual_idx ON maker_accrual (ts);
CREATE TABLE IF NOT EXISTS daily (
    date TEXT PRIMARY KEY,
    realized_usd REAL DEFAULT 0,
    unrealized_usd REAL DEFAULT 0,
    notes TEXT
);
"""


def _now() -> float:
    return time.time()


class Ledger:
    def __init__(self, path: str = config.DB_PATH):
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        # The depth columns arrived after the table did; CREATE TABLE IF NOT EXISTS will not add
        # them to the 130 MB database already on disk.
        have = {r[1] for r in self.conn.execute("PRAGMA table_info(snapshots)")}
        for col in ("bid_qty", "ask_qty"):
            if col not in have:
                self.conn.execute(f"ALTER TABLE snapshots ADD COLUMN {col} REAL")
        for col in ("bid_ladder", "ask_ladder"):
            if col not in have:
                self.conn.execute(f"ALTER TABLE snapshots ADD COLUMN {col} TEXT")
        # Both venues write model runs for the same city-day, so a run has to say whose it is (below).
        if "venue" not in {r[1] for r in self.conn.execute("PRAGMA table_info(model_runs)")}:
            self.conn.execute("ALTER TABLE model_runs ADD COLUMN venue TEXT")
        self.conn.commit()
        self.gate_since_ts = 0.0     # set by the runner from config: evidence before a rule change doesn't count
        self.min_us_signals = 10     # set by the runner from config: the gate's US-evidence floor

    # ---- signals -------------------------------------------------------------------------
    def add_signal(self, sig, mode: str) -> int:
        cur = self.conn.execute(
            """INSERT INTO signals (ts, module, venue, market, label, side, price, size_usd, contracts,
               edge_cents, reason, exit_rule, horizon_h, mode, spread_cents, category, meta)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (sig.ts or _now(), sig.module, sig.venue, sig.market, sig.label, sig.side, sig.price,
             sig.size_usd, sig.contracts, sig.edge_cents, sig.reason, sig.exit, sig.horizon_hours,
             mode, sig.spread_cents, sig.category,
             json.dumps({**(sig.meta or {}), "taker": bool(sig.taker), "arb": bool(sig.arb)})),
        )
        self.conn.commit()
        return cur.lastrowid

    def recent_signal_exists(self, module: str, market: str, side: str, within_s: float) -> bool:
        """Have we already decided this market+side recently? Voided rows do not count.

        A void means "this decision was wrong, forget it" — `decision_count` and `module_stats`
        already exclude them, and this is the third place that must. Otherwise striking a bad
        signal from the record silently suppresses the real opportunity behind it: voiding a
        double-counted miami set on 2026-09-19 immediately blocked a genuine 13.1c/set candidate
        three minutes later, for no reason a reader of the log could see.
        """
        row = self.conn.execute(
            "SELECT 1 FROM signals WHERE module=? AND market=? AND side=? AND ts>=? "
            "AND status!='void' LIMIT 1",
            (module, market, side, _now() - within_s),
        ).fetchone()
        return row is not None

    def open_signals(self, module: str | None = None, venue: str | None = None):
        q = "SELECT * FROM signals WHERE status='open'"
        args = []
        if module:
            q += " AND module=?"
            args.append(module)
        if venue:
            q += " AND venue=?"
            args.append(venue)
        return [dict(r) for r in self.conn.execute(q + " ORDER BY ts", args)]

    # ---- maker_rewards ----------------------------------------------------------------------
    def maker_quotes(self, market: str | None = None) -> list:
        q, args = "SELECT * FROM maker_quotes", []
        if market:
            q, args = q + " WHERE market=?", [market]
        return [dict(r) for r in self.conn.execute(q + " ORDER BY market, side", args)]

    def maker_set(self, market, side, px, qty, ts, last_trade, filled_ts=None) -> None:
        self.conn.execute(
            "INSERT INTO maker_quotes (market, side, px, qty, last_ts, last_trade, filled_ts) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(market, side) DO UPDATE SET px=excluded.px, qty=excluded.qty, last_ts=excluded.last_ts, "
            "last_trade=excluded.last_trade, filled_ts=excluded.filled_ts",
            (market, side, px, qty, ts, last_trade, filled_ts))
        self.conn.commit()

    def maker_start(self, market, ts) -> None:
        for side in ("bid", "ask"):
            self.maker_set(market, side, None, 0.0, ts, None)

    def maker_drop(self, market) -> None:
        self.conn.execute("DELETE FROM maker_quotes WHERE market=?", (market,))
        self.conn.commit()

    def maker_accrue(self, ts, market, side, qty, share, reward_usd, qualified, pool, program) -> None:
        self.conn.execute(
            "INSERT INTO maker_accrual (ts, market, side, qty, share, reward_usd, qualified, pool, program) "
            "VALUES (?,?,?,?,?,?,?,?,?)", (ts, market, side, qty, share, reward_usd, int(bool(qualified)), pool, program))
        self.conn.commit()

    def maker_position_open(self, market, side) -> bool:
        for r in self.open_signals(module="maker_rewards", venue="us"):
            if r["market"] == market and (json.loads(r["meta"] or "{}").get("maker_side") == side):
                return True
        return False

    def maker_positions_open(self, market) -> bool:
        return any(r["market"] == market for r in self.open_signals(module="maker_rewards", venue="us"))

    def maker_rewards_usd(self, since_ts: float) -> float:
        """Estimated incentive rewards accrued by the paper book since `since_ts`."""
        r = self.conn.execute("SELECT COALESCE(SUM(reward_usd), 0) FROM maker_accrual WHERE ts>=?", (since_ts,)).fetchone()
        return float(r[0] or 0.0)

    def extra_mtm(self, module: str, days: int = 30) -> float:
        """Income a module earns that is not a position: maker_rewards' incentive accrual. Zero for
        every other module, so no other gate reads differently."""
        if module != "maker_rewards":
            return 0.0
        return self.maker_rewards_usd(max(_now() - days * 86400, float(self.gate_since_ts or 0.0)))

    def set_signal_status(self, signal_id: int, status: str) -> None:
        self.conn.execute("UPDATE signals SET status=? WHERE id=?", (status, signal_id))
        self.conn.commit()

    # ---- paper trades --------------------------------------------------------------------
    def upsert_paper(self, signal_id: int, **fields) -> None:
        cols = ", ".join(fields)
        placeholders = ", ".join("?" for _ in fields)
        updates = ", ".join(f"{k}=excluded.{k}" for k in fields)
        self.conn.execute(
            f"INSERT INTO paper_trades (signal_id, {cols}) VALUES (?, {placeholders}) "
            f"ON CONFLICT(signal_id) DO UPDATE SET {updates}",
            (signal_id, *fields.values()),
        )
        self.conn.commit()

    def paper_row(self, signal_id: int):
        r = self.conn.execute("SELECT * FROM paper_trades WHERE signal_id=?", (signal_id,)).fetchone()
        return dict(r) if r else None

    # ---- orders / snapshots / model runs -------------------------------------------------
    def add_order(self, signal_id, venue, market, side, price, contracts, status, venue_order_id=None, raw=None) -> int:
        cur = self.conn.execute(
            "INSERT INTO orders (signal_id, ts, venue, market, venue_order_id, side, price, contracts, status, raw)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (signal_id, _now(), venue, market, venue_order_id, side, price, contracts, status,
             json.dumps(raw) if raw is not None else None),
        )
        self.conn.commit()
        return cur.lastrowid

    def last_order(self, signal_id: int):
        r = self.conn.execute("SELECT * FROM orders WHERE signal_id=? ORDER BY id DESC LIMIT 1", (signal_id,)).fetchone()
        return dict(r) if r else None

    def set_order_status(self, order_id: int, status: str) -> None:
        self.conn.execute("UPDATE orders SET status=? WHERE id=?", (status, order_id))
        self.conn.commit()

    def add_snapshot(self, venue, market, bid, ask, last=None, ts=None, bid_qty=None, ask_qty=None,
                     bid_levels=None, ask_levels=None) -> None:
        mid = (bid + ask) / 2 if (bid is not None and ask is not None) else None
        # Ladders are kept only for the arb candidates that paid for a depth read, so this stays a
        # couple of hundred bytes on ~1% of rows rather than a second copy of the whole database.
        dump = lambda lv: json.dumps([[px, qty] for px, qty in lv]) if lv else None
        self.conn.execute(
            "INSERT INTO snapshots (ts, venue, market, bid, ask, mid, last, bid_qty, ask_qty, "
            "bid_ladder, ask_ladder) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (ts or _now(), venue, market, bid, ask, mid, last, bid_qty, ask_qty,
             dump(bid_levels), dump(ask_levels)))
        self.conn.commit()

    def snapshots(self, venue, market, since_ts):
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM snapshots WHERE venue=? AND market=? AND ts>=? ORDER BY ts", (venue, market, since_ts))]

    def add_model_run(self, city, date, kind, probs: list, ts=None, venue: str | None = None) -> None:
        self.conn.execute("INSERT INTO model_runs (ts, city, date, kind, probs, venue) VALUES (?,?,?,?,?,?)",
                          (ts or _now(), city, date, kind, json.dumps(probs), venue))
        self.conn.commit()

    def last_model_run(self, city, date, kind, venue: str | None = None):
        """The previous run for this city-day — of the SAME venue when one is named.

        Without the venue, the US scan and the offshore scan of one city-day alternated in this
        table, so `last_model_run` handed each the other's run: an 11-bucket offshore run against a
        6-bucket US event. The length check dropped those silently, and only 29 of 260 US runs
        (2026-09-12..18) were ever compared against a US run — weather_model_update was never
        really tested on the venue that matters."""
        if venue is None:
            r = self.conn.execute(
                "SELECT * FROM model_runs WHERE city=? AND date=? AND kind=? ORDER BY ts DESC LIMIT 1",
                (city, date, kind)).fetchone()
        else:
            r = self.conn.execute(
                "SELECT * FROM model_runs WHERE city=? AND date=? AND kind=? AND venue=? ORDER BY ts DESC LIMIT 1",
                (city, date, kind, venue)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["probs"] = json.loads(d["probs"])
        return d

    # ---- exposure / pnl ------------------------------------------------------------------
    def exposure_usd(self, venue: str, market: str | None = None, live_only: bool = True) -> float:
        q = "SELECT COALESCE(SUM(size_usd),0) AS s FROM signals WHERE status='open' AND venue=?"
        args = [venue]
        if live_only:
            q += " AND mode='live'"
        if market:
            q += " AND market=?"
            args.append(market)
        return float(self.conn.execute(q, args).fetchone()["s"])

    def held_contracts(self, venue: str, market: str, module: str | None = None) -> float:
        """Contracts of this leg sitting in OPEN positions.

        Paper orders do not consume the book, so without this a persistent mispricing is bought
        over and over against the same contracts. Miami on 2026-09-19 was booked twice five
        minutes apart, 62 sets each, while the binding leg's ladder showed the SAME 79 contracts
        both times -- live, the first order would have taken 62 of them. That inflates paper P&L,
        and paper P&L is the evidence the live decision rests on.
        """
        q = ("SELECT COALESCE(SUM(contracts),0) AS n FROM signals "
             "WHERE status='open' AND venue=? AND market=?")
        args = [venue, market]
        if module:
            q += " AND module=?"
            args.append(module)
        return float(self.conn.execute(q, args).fetchone()["n"])

    def realized_today_usd(self, mode: str = "live") -> float:
        start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        r = self.conn.execute(
            """SELECT COALESCE(SUM(p.pnl_usd),0) AS s FROM paper_trades p JOIN signals s ON s.id=p.signal_id
               WHERE p.status='closed' AND p.exit_ts>=? AND s.mode=?""", (start, mode)).fetchone()
        return float(r["s"])

    def prune_snapshots(self, keep_days: int = 7) -> int:
        cur = self.conn.execute("DELETE FROM snapshots WHERE ts < ?", (_now() - keep_days * 86400,))
        self.conn.commit()
        return cur.rowcount

    # ---- reporting -----------------------------------------------------------------------
    def module_stats(self, days: int = 30, venue: str | None = None):
        """Per module+mode: counts, closed net, and the mark-to-market of what is still open. Closed net
        alone flatters a take-profit module — winners close in hours, losers sit open until settlement."""
        since = max(_now() - days * 86400, float(self.gate_since_ts or 0.0))
        q = """SELECT s.module, s.mode, COUNT(*) AS n,
                      SUM(CASE WHEN p.status='closed' THEN 1 ELSE 0 END) AS closed,
                      SUM(CASE WHEN p.filled_ts IS NOT NULL THEN 1 ELSE 0 END) AS filled,
                      SUM(CASE WHEN p.status='filled' THEN 1 ELSE 0 END) AS open_filled,
                      COALESCE(SUM(CASE WHEN p.status='closed' THEN p.pnl_usd END),0) AS pnl,
                      COALESCE(SUM(CASE WHEN p.status='filled' THEN p.pnl_usd END),0) AS unreal,
                      COALESCE(SUM(CASE WHEN p.status='closed' THEN p.fees_usd END),0) AS fees,
                      AVG(s.edge_cents) AS edge, AVG(s.spread_cents) AS spread
               FROM signals s LEFT JOIN paper_trades p ON p.signal_id=s.id
               WHERE s.ts>=? AND s.status!='void'"""
        args = [since]
        if venue:
            q += " AND s.venue=?"
            args.append(venue)
        rows = [dict(r) for r in self.conn.execute(q + " GROUP BY s.module, s.mode ORDER BY s.module", args)]
        for r in rows:
            r["mtm"] = (r["pnl"] or 0.0) + (r["unreal"] or 0.0)
        return rows

    def decision_count(self, module: str, days: int = 30, venue: str | None = None) -> int:
        """Independent decisions, not rows. An arb set writes one signal per leg, so a six-bucket
        bucket_sum episode looks like six pieces of evidence when it is one — and thirty rows would
        let real money out after five observed sets. Signals carrying a `meta.group` are counted by
        distinct group; everything else is one decision per row."""
        since = max(_now() - days * 86400, float(self.gate_since_ts or 0.0))
        # A voided signal is one the bot has since decided it would NOT take — a rule changed
        # under it. Counting it as evidence releases real money on the strength of trades the
        # current rules refuse, which is the opposite of what the gate is for.
        q = ("SELECT COUNT(DISTINCT COALESCE(json_extract(meta,'$.group'), 'row:' || id)) AS n "
             "FROM signals WHERE module=? AND ts>=? AND status!='void'")
        args = [module, since]
        if venue:
            q += " AND venue=?"
            args.append(venue)
        return int(self.conn.execute(q, args).fetchone()["n"] or 0)

    def promotion_check(self, module: str, days: int = 30, min_signals: int = 30, min_fill_rate: float = 0.5,
                        min_us_signals: int | None = None):
        """The gate: enough signals, enough fills, positive mark-to-market (not just closed net), and a
        real track record on the Polymarket US books — the only books real money ever touches.

        `min_us_signals` is the part that is easy to leave out and expensive to get wrong. Most signals
        are offshore, which is a read-only proxy on a DIFFERENT settlement rule (hourly METAR at KLGA vs
        the daily CLI report at KNYC). Until 2026-09-18 the US record was only consulted when it existed,
        so a module could reach 30 offshore signals, pass, and be flipped live having never once produced
        a signal on the venue it would be spending Alex's money on — weather_model_update was 17/30 with
        exactly zero US signals when this was found. Offshore evidence is a hint; US evidence is the case."""
        if min_us_signals is None:
            min_us_signals = self.min_us_signals
        stats = [s for s in self.module_stats(days) if s["module"] == module]
        if not stats:
            return False, "no signals"
        n = self.decision_count(module, days)
        rows = sum(s["n"] for s in stats)
        closed = sum(s["closed"] or 0 for s in stats)
        filled = sum(s["filled"] or 0 for s in stats)
        mtm = sum(s["mtm"] for s in stats) + self.extra_mtm(module, days)
        if n < min_signals:
            return False, f"{n}/{min_signals} signals"
        if closed == 0:
            return False, "nothing closed yet"
        if filled / max(rows, 1) < min_fill_rate:      # fills are per leg, so measure them per leg
            return False, f"fill rate {filled / rows:.0%} < {min_fill_rate:.0%}"
        if mtm <= 0:
            return False, f"mark-to-market {mtm:+.2f} not positive"
        us = [s for s in self.module_stats(days, venue="us") if s["module"] == module]
        us_n = self.decision_count(module, days, venue="us")
        us_closed = sum(s["closed"] or 0 for s in us)
        us_mtm = sum(s["mtm"] for s in us) + self.extra_mtm(module, days)   # maker rewards are US-only
        if us_n < min_us_signals:
            return False, f"{us_n}/{min_us_signals} US signals (offshore evidence does not count for live)"
        if us_closed == 0:
            return False, f"US paper: {us_n} signals, none settled yet"
        if us_mtm < 0:
            return False, f"US paper mark-to-market {us_mtm:+.2f} negative over {us_n} signals"
        ok, why = self.tail_check(module, days)
        if not ok:
            return False, why
        return True, (f"{n} signals, {closed} closed, mtm {mtm:+.2f}, fills {filled / rows:.0%}, "
                      f"US {us_n} signals {us_mtm:+.2f}")

    def tail_check(self, module: str, days: int = 30):
        """(ok, why) — is this record an edge, or one lucky trade and one unlucky one?

        The rest of the gate counts signals, fills and mark-to-market, and none of that can tell a
        small repeatable edge from a coin flip. weather_obs on 2026-09-19 read +$8.43 over 28
        closed trades and was the module CLOSEST to promotion — while its best trade was +$80.10
        and its worst was -$20.00 on a $20 position. One trade was the whole record and one loss
        undid it, which is what "pennies in front of a steamroller" looks like in a ledger.

        Measured per DECISION, not per row: an arb's legs look dreadful on their own (five lose
        their premium so that one can win) while the set they belong to cannot lose at all. Legs
        carrying a `meta.group` are therefore summed into their set first.
        """
        rows = list(self.conn.execute(
            """SELECT s.meta, s.id, p.pnl_usd FROM signals s JOIN paper_trades p ON p.signal_id=s.id
               WHERE s.module=? AND p.status='closed' AND s.status!='void' AND s.ts>=?""",
            (module, max(_now() - days * 86400, float(self.gate_since_ts or 0.0)))))
        if not rows:
            return True, "no closed trades to judge"
        by_decision = {}
        for meta, sid, pnl in rows:
            try:
                key = (json.loads(meta or "{}").get("group")) or f"sig:{sid}"
            except (TypeError, ValueError):
                key = f"sig:{sid}"
            by_decision[key] = by_decision.get(key, 0.0) + (pnl or 0.0)
        vals = list(by_decision.values())
        net = sum(vals)
        if net <= 0:
            return False, f"net {net:+.2f} over {len(vals)} decisions"
        worst, best = min(vals), max(vals)
        if abs(worst) >= net:
            return False, (f"one loss ({worst:+.2f}) is bigger than the whole record ({net:+.2f}) "
                           f"over {len(vals)} decisions — variance, not edge")
        if best >= net:
            return False, (f"one trade ({best:+.2f}) IS the whole record ({net:+.2f}) "
                           f"over {len(vals)} decisions — not yet repeatable")
        return True, f"{len(vals)} decisions, net {net:+.2f}, worst {worst:+.2f}, best {best:+.2f}"

    def arb_report(self, days: int = 7) -> str:
        """Every moment the US books offered a complete bucket set worth taking, and how deep it was.

        bucket_sum is the only module whose profit does not depend on out-forecasting anyone, so
        the question that decides whether it is a business is not "does the arb appear" (it does)
        but "is it ever big enough to be worth taking".

        Three things this used to get wrong, all of which flattered it:
          - it accepted ANY two legs as a set, so a minute where only 2 of 6 legs happened to be
            snapshotted read as a 90c edge. A set is the whole book or it is nothing.
          - it reported GROSS, so rows like "gross=1.0c/set" were listed as candidates when six
            legs of taker fee is ~3c and they are losses.
          - it only ever looked at the buy side, while sell-side sets are the commoner shape.
        """
        since = _now() - days * 86400
        rows = [dict(r) for r in self.conn.execute(
            "SELECT ts, market, bid, ask, bid_qty, ask_qty, bid_ladder, ask_ladder FROM snapshots "
            "WHERE venue='us' AND ts>=? ORDER BY ts", (since,))]
        events, width = {}, {}
        for r in rows:
            m = re.match(r"^(tc-temp-[a-z]+(?:high|low)-\d{4}-\d{2}-\d{2})-", r["market"] or "")
            if m:
                events.setdefault((m.group(1), int(r["ts"] // 60)), {})[r["market"]] = r
        for (slug, _), legs in events.items():          # how many legs this event actually has
            width[slug] = max(width.get(slug, 0), len(legs))

        def fee(px):
            return US_TAKER_THETA * px * (1.0 - px)

        lines = [f"arb candidates — last {days}d (US books, net of taker fees)"]
        found = taken = 0
        for (slug, minute), legs in sorted(events.items(), key=lambda kv: kv[0][1]):
            if len(legs) < width.get(slug, 0) or len(legs) < 3:
                continue                                 # a partial book is not a set
            asks = [l["ask"] for l in legs.values()]
            bids = [l["bid"] for l in legs.values()]
            best = None
            if all(a is not None for a in asks):
                net = (1.0 - sum(asks) - sum(fee(a) for a in asks)) * 100
                best = ("buy_all", net, [l["ask_qty"] for l in legs.values()])
            if all(b is not None for b in bids):
                net = (sum(bids) - 1.0 - sum(fee(b) for b in bids)) * 100
                if best is None or net > best[1]:
                    best = ("sell_all", net, [l["bid_qty"] for l in legs.values()])
            if best is None or best[1] <= 0:
                continue
            kind, net, qtys = best
            found += 1
            if any(q is None for q in qtys):
                depth, worth = "?", ""
            else:
                n = min(qtys)
                depth = f"{n:.0f} sets"
                worth = f"  ${net / 100 * n:6.2f}"
                taken += 1
            when = datetime.fromtimestamp(minute * 60).strftime("%m-%d %H:%M")
            lines.append(f"  {when}  {slug:<32} {kind:<8} net={net:5.1f}c/set  "
                         f"fillable={depth}{worth}")
        if not found:
            lines.append("  none")
        else:
            lines.append(f"  {found} event-minutes with a positive net; {taken} had depth recorded. "
                         "Depth is only written when the arb path runs, so the rest read '?' — "
                         "and depth, not price, is what decides whether any of this is money.")
        return "\n".join(lines)

    def report(self, days: int = 1) -> str:
        lines = [f"polybot report — last {days}d — {datetime.now().strftime('%Y-%m-%d %H:%M')}"]
        gate_since = float(self.gate_since_ts or 0.0)
        if gate_since > _now() - days * 86400:
            # A rule change resets gate_since_ts so old evidence stops counting — correct, but a
            # module reading "no signals" right after a reset looks identical to one that stalled
            # for the whole window. This is the difference between the two (found 2026-09-18: the
            # gate had reset 4h earlier and every "no signals" line below was read as a dead module).
            age_h = (_now() - gate_since) / 3600
            lines.append(f"  gate evidence reset {datetime.fromtimestamp(gate_since).strftime('%Y-%m-%d %H:%M')} "
                         f"({age_h:.1f}h ago) — counts and gates below only reflect signals since then")
        stats = self.module_stats(days)
        if not stats:
            lines.append("  no signals")
        for s in stats:
            lines.append(
                f"  {s['module']:<22} {s['mode']:<6} signals={s['n']:<3} filled={s['filled'] or 0:<3} "
                f"closed={s['closed'] or 0:<3} net=${s['pnl']:+.2f} open={s['open_filled'] or 0:<3} "
                f"unreal=${s['unreal']:+.2f} mtm=${s['mtm']:+.2f} fees=${s['fees']:+.2f} "
                f"edge={s['edge'] or 0:.1f}c spread={s['spread'] or 0:.1f}c")
        us = self.module_stats(days, venue="us")
        if us:
            lines.append("  Polymarket US books only (what live would have done):")
            for s in us:
                lines.append(f"    {s['module']:<22} {s['mode']:<6} signals={s['n']:<3} filled={s['filled'] or 0:<3} "
                             f"closed={s['closed'] or 0:<3} mtm=${s['mtm']:+.2f}")
        mk = self.maker_rewards_usd(max(_now() - days * 86400, float(self.gate_since_ts or 0.0)))
        mk_all = self.maker_rewards_usd(float(self.gate_since_ts or 0.0))
        if mk_all:
            quoted = self.conn.execute("SELECT COUNT(DISTINCT market) FROM maker_quotes").fetchone()[0]
            lines.append(f"  maker_rewards incentive accrual (ESTIMATE, paper): ${mk:.2f} in {days}d, "
                         f"${mk_all:.2f} since the gate reset, {quoted} market(s) quoted")
        for m in config.MODULES:
            ok, why = self.promotion_check(m)
            lines.append(f"  gate {m:<22} {'PASS' if ok else 'hold'} — {why}")
        if any(s["module"] == "weather_lock" for s in stats):
            # Answers "why does live show so few weather_lock signals against the backtest ROI" from
            # snapshots the loop already wrote — no live API calls, so it belongs in the fast daily
            # report, not only in the slow `backtest` command that has to re-fetch every book.
            from . import backtest
            liq = backtest.liquidity_reality(db_path=self.path)
            if liq:
                lines.append(f"  weather_lock offshore book reality ({liq['days']}d, ask "
                             f"{liq['band'][0]:.2f}-{liq['band'][1]:.2f}, n={liq['n']}): median spread "
                             f"{liq['median_spread_cents']:.0f}c · {liq['tradable_share']:.0%} clear the "
                             f"{liq['max_spread_cents']:.0f}c filter — this bounds live signal count "
                             "regardless of backtest ROI")
        return "\n".join(lines)

    def summary(self, days: int = 1) -> str:
        """One line for a phone nudge."""
        stats = self.module_stats(days)
        n = sum(s["n"] for s in stats)
        paper = sum(s["mtm"] for s in stats if s["mode"] != "live")
        live = [s for s in stats if s["mode"] == "live"]
        parts = [f"{days}d: {n} signals, paper mtm {paper:+.2f}"]
        if live:
            parts.append(f"LIVE net {sum(s['pnl'] for s in live):+.2f} ({sum(s['open_filled'] or 0 for s in live)} open)")
        gates = [(m, self.promotion_check(m)) for m in config.MODULES]
        passing = [m for m, (ok, _) in gates if ok]
        parts.append(f"gate PASS: {', '.join(passing) if passing else 'none yet'}")
        return " · ".join(parts)
