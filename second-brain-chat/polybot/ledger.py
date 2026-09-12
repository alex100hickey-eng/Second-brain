"""The ledger: every signal, paper trade, order, snapshot and daily line, in sqlite.

This is the thing that promotes a module from paper → signal → live. Nothing else does.
"""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone

from . import config

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
    bid REAL, ask REAL, mid REAL, last REAL
);
CREATE INDEX IF NOT EXISTS snapshots_idx ON snapshots (venue, market, ts);
CREATE TABLE IF NOT EXISTS model_runs (
    ts REAL NOT NULL,
    city TEXT NOT NULL,
    date TEXT NOT NULL,
    kind TEXT NOT NULL,
    probs TEXT NOT NULL
);
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
        row = self.conn.execute(
            "SELECT 1 FROM signals WHERE module=? AND market=? AND side=? AND ts>=? LIMIT 1",
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

    def add_snapshot(self, venue, market, bid, ask, last=None, ts=None) -> None:
        mid = (bid + ask) / 2 if (bid is not None and ask is not None) else None
        self.conn.execute("INSERT INTO snapshots (ts, venue, market, bid, ask, mid, last) VALUES (?,?,?,?,?,?,?)",
                          (ts or _now(), venue, market, bid, ask, mid, last))
        self.conn.commit()

    def snapshots(self, venue, market, since_ts):
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM snapshots WHERE venue=? AND market=? AND ts>=? ORDER BY ts", (venue, market, since_ts))]

    def add_model_run(self, city, date, kind, probs: list, ts=None) -> None:
        self.conn.execute("INSERT INTO model_runs (ts, city, date, kind, probs) VALUES (?,?,?,?,?)",
                          (ts or _now(), city, date, kind, json.dumps(probs)))
        self.conn.commit()

    def last_model_run(self, city, date, kind):
        r = self.conn.execute(
            "SELECT * FROM model_runs WHERE city=? AND date=? AND kind=? ORDER BY ts DESC LIMIT 1",
            (city, date, kind)).fetchone()
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

    def realized_today_usd(self, mode: str = "live") -> float:
        start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        r = self.conn.execute(
            """SELECT COALESCE(SUM(p.pnl_usd),0) AS s FROM paper_trades p JOIN signals s ON s.id=p.signal_id
               WHERE p.status='closed' AND p.exit_ts>=? AND s.mode=?""", (start, mode)).fetchone()
        return float(r["s"])

    # ---- reporting -----------------------------------------------------------------------
    def module_stats(self, days: int = 30):
        since = _now() - days * 86400
        rows = self.conn.execute(
            """SELECT s.module, s.mode, COUNT(*) AS n,
                      SUM(CASE WHEN p.status='closed' THEN 1 ELSE 0 END) AS closed,
                      SUM(CASE WHEN p.filled_ts IS NOT NULL THEN 1 ELSE 0 END) AS filled,
                      COALESCE(SUM(CASE WHEN p.status='closed' THEN p.pnl_usd END),0) AS pnl,
                      COALESCE(SUM(CASE WHEN p.status='closed' THEN p.fees_usd END),0) AS fees,
                      AVG(s.edge_cents) AS edge, AVG(s.spread_cents) AS spread
               FROM signals s LEFT JOIN paper_trades p ON p.signal_id=s.id
               WHERE s.ts>=? GROUP BY s.module, s.mode ORDER BY s.module""", (since,)).fetchall()
        return [dict(r) for r in rows]

    def promotion_check(self, module: str, days: int = 30, min_signals: int = 30, min_fill_rate: float = 0.5):
        """The gate from the design doc. Returns (ok, reason)."""
        stats = [s for s in self.module_stats(days) if s["module"] == module]
        if not stats:
            return False, "no signals"
        n = sum(s["n"] for s in stats)
        closed = sum(s["closed"] or 0 for s in stats)
        filled = sum(s["filled"] or 0 for s in stats)
        pnl = sum(s["pnl"] or 0 for s in stats)
        if n < min_signals:
            return False, f"{n}/{min_signals} signals"
        if closed == 0:
            return False, "nothing closed yet"
        if filled / max(n, 1) < min_fill_rate:
            return False, f"fill rate {filled / n:.0%} < {min_fill_rate:.0%}"
        if pnl <= 0:
            return False, f"net {pnl:+.2f} not positive"
        return True, f"{n} signals, {closed} closed, net {pnl:+.2f}, fills {filled / n:.0%}"

    def report(self, days: int = 1) -> str:
        lines = [f"polybot report — last {days}d — {datetime.now().strftime('%Y-%m-%d %H:%M')}"]
        stats = self.module_stats(days)
        if not stats:
            lines.append("  no signals")
        for s in stats:
            lines.append(
                f"  {s['module']:<22} {s['mode']:<6} signals={s['n']:<3} filled={s['filled'] or 0:<3} "
                f"closed={s['closed'] or 0:<3} net=${s['pnl']:+.2f} fees=${s['fees']:+.2f} "
                f"edge={s['edge'] or 0:.1f}c spread={s['spread'] or 0:.1f}c")
        for m in config.MODULES:
            ok, why = self.promotion_check(m)
            lines.append(f"  gate {m:<22} {'PASS' if ok else 'hold'} — {why}")
        return "\n".join(lines)
