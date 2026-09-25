"""clipbot ledger (sqlite): campaigns → sources → clips → variants → posts, plus credits and hooks."""
from __future__ import annotations

import json
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS campaigns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    marketplace TEXT,                -- vyro | whop | kick | own | splitframe
    rate_per_1k REAL DEFAULT 0,
    cap_per_clip REAL DEFAULT 0,
    hashtags TEXT DEFAULT '',        -- required tags, space separated
    prompt TEXT DEFAULT '',          -- ClipAnything prompt aimed at the brief
    platforms TEXT DEFAULT '',       -- csv; empty = config default
    notes TEXT DEFAULT '',
    rules TEXT DEFAULT '{}',         -- json: per-campaign brief rules (see config.DEFAULT_RULES)
    status TEXT DEFAULT 'active',
    created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id INTEGER NOT NULL,
    locator TEXT NOT NULL,           -- URL, local path, or opus uploadId
    title TEXT DEFAULT '',
    minutes REAL DEFAULT 0,
    credits_est INTEGER DEFAULT 0,
    opus_project_id TEXT,
    status TEXT DEFAULT 'queued',    -- queued | submitted | clipped | failed
    error TEXT DEFAULT '',
    created REAL NOT NULL,
    updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS clips (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL,
    opus_clip_id TEXT,
    title TEXT DEFAULT '',
    transcript TEXT DEFAULT '',
    score REAL DEFAULT 0,
    duration_s REAL DEFAULT 0,
    preview_url TEXT DEFAULT '',
    hd_url TEXT DEFAULT '',
    local_path TEXT DEFAULT '',
    status TEXT DEFAULT 'new',       -- new | downloaded | transformed | skipped | failed
    created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS variants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    clip_id INTEGER NOT NULL,
    platform TEXT NOT NULL,
    path TEXT DEFAULT '',
    hook_file TEXT DEFAULT '',
    text_hook TEXT DEFAULT '',
    staged_path TEXT DEFAULT '',
    status TEXT DEFAULT 'made',      -- made | staged | scheduled | posted | skipped
    schedule_id TEXT DEFAULT '',
    created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    variant_id INTEGER UNIQUE NOT NULL,
    platform TEXT NOT NULL,
    url TEXT DEFAULT '',
    posted_at REAL,
    views INTEGER DEFAULT 0,
    qualified_views INTEGER DEFAULT 0,
    usd_approved REAL DEFAULT 0,
    usd_settled REAL DEFAULT 0,
    updated REAL
);
CREATE TABLE IF NOT EXISTS credits (
    ts REAL NOT NULL,
    amount INTEGER NOT NULL,
    note TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS hook_uses (
    hook_file TEXT PRIMARY KEY,
    uses INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS accounts (
    handle TEXT PRIMARY KEY,         -- '@name' as it appears in the post URL
    platform TEXT NOT NULL,
    created_at REAL NOT NULL,        -- when the platform account was made: posting_policy ages from this
    registered_at REAL NOT NULL,
    campaigns TEXT DEFAULT '',       -- comma-separated campaign ids: the one niche this account posts
    connector_id TEXT DEFAULT '',    -- Higgsfield TikTok connector
    status TEXT DEFAULT 'active',    -- active | retired (retired = never post from it)
    notes TEXT DEFAULT '',
    linked TEXT DEFAULT ''           -- boards the account is verified on ("whop"); a post from an unlinked account can't be submitted
);
"""

# A post only earns once it is submitted to the board, and only past the board's per-post floor.
# Vyro pays nothing under 5,000 views a post; Whop's minimum payout is one clip's rate (~1,000 views).
PAYOUT_FLOOR_VIEWS = {"vyro": 5000, "whop": 1000}


def _now() -> float:
    return time.time()


def handle_from_url(url: str) -> str:
    """'@name' from a TikTok post URL (tiktok.com/@name/video/<id>); '' for anything else."""
    m = re.search(r"tiktok\.com/(@[A-Za-z0-9_.]+)/", url or "")
    return m.group(1) if m else ""


class Ledger:
    def __init__(self, path: str = config.DB_PATH):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(campaigns)")}
        if "rules" not in cols:                       # DBs created before per-campaign rules existed
            self.conn.execute("ALTER TABLE campaigns ADD COLUMN rules TEXT DEFAULT '{}'")
            self.conn.commit()
        pcols = {r[1] for r in self.conn.execute("PRAGMA table_info(posts)")}
        if "account" not in pcols:                    # DBs from before a second account existed
            self.conn.execute("ALTER TABLE posts ADD COLUMN account TEXT DEFAULT ''")
            for r in self.conn.execute("SELECT variant_id, url FROM posts").fetchall():
                h = handle_from_url(r[1])
                if h:
                    self.conn.execute("UPDATE posts SET account=? WHERE variant_id=?", (h, r[0]))
            self.conn.commit()
        if "submitted_at" not in pcols:
            self.conn.execute("ALTER TABLE posts ADD COLUMN submitted_at REAL")
            self.conn.commit()
        if "views_at_submit" not in pcols:            # boards pay on views after submission, not before
            self.conn.execute("ALTER TABLE posts ADD COLUMN views_at_submit INTEGER DEFAULT 0")
            self.conn.commit()
        acols = {r[1] for r in self.conn.execute("PRAGMA table_info(accounts)")}
        if "linked" not in acols:                     # boards this account is verified on, e.g. "whop"
            self.conn.execute("ALTER TABLE accounts ADD COLUMN linked TEXT DEFAULT ''")
            self.conn.commit()

    def _rows(self, q, args=()):
        return [dict(r) for r in self.conn.execute(q, args)]

    def _one(self, q, args=()):
        r = self.conn.execute(q, args).fetchone()
        return dict(r) if r else None

    # ---- campaigns -----------------------------------------------------------------------
    def add_campaign(self, name, marketplace="", rate_per_1k=0.0, cap_per_clip=0.0, hashtags="",
                     prompt="", platforms="", notes="", rules=None) -> int:
        cur = self.conn.execute(
            "INSERT INTO campaigns (name, marketplace, rate_per_1k, cap_per_clip, hashtags, prompt, platforms, notes,"
            " rules, created) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (name, marketplace, rate_per_1k, cap_per_clip, hashtags, prompt, platforms, notes,
             json.dumps(rules or {}), _now()))
        self.conn.commit()
        return cur.lastrowid

    @staticmethod
    def rules(campaign) -> dict:
        """Brief rules for a campaign row (dict or None), defaults filled in."""
        return config.campaign_rules(campaign)

    def campaign(self, ref) -> dict | None:
        if isinstance(ref, int) or str(ref).isdigit():
            return self._one("SELECT * FROM campaigns WHERE id=?", (int(ref),))
        return self._one("SELECT * FROM campaigns WHERE lower(name)=lower(?)", (str(ref),))

    def campaigns(self, status="active"):
        return self._rows("SELECT * FROM campaigns WHERE status=? ORDER BY id", (status,))

    # ---- sources -------------------------------------------------------------------------
    def add_source(self, campaign_id, locator, title="", minutes=0.0, credits_est=0) -> int:
        cur = self.conn.execute(
            "INSERT INTO sources (campaign_id, locator, title, minutes, credits_est, created, updated) VALUES (?,?,?,?,?,?,?)",
            (campaign_id, locator, title, minutes, credits_est, _now(), _now()))
        self.conn.commit()
        return cur.lastrowid

    def source_by_locator(self, locator):
        return self._one("SELECT * FROM sources WHERE locator=?", (locator,))

    def sources(self, status=None):
        if status:
            return self._rows("SELECT * FROM sources WHERE status=? ORDER BY id", (status,))
        return self._rows("SELECT * FROM sources ORDER BY id")

    def update_source(self, source_id, **fields):
        fields["updated"] = _now()
        sets = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE sources SET {sets} WHERE id=?", (*fields.values(), source_id))
        self.conn.commit()

    # ---- clips / variants / posts --------------------------------------------------------
    def add_clip(self, source_id, c: dict) -> int:
        existing = self._one("SELECT id FROM clips WHERE source_id=? AND opus_clip_id=?", (source_id, c.get("clip_id")))
        if existing:
            return existing["id"]
        cur = self.conn.execute(
            "INSERT INTO clips (source_id, opus_clip_id, title, transcript, score, duration_s, preview_url, hd_url, created)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (source_id, c.get("clip_id"), c.get("title", ""), c.get("transcript", ""), c.get("score", 0),
             c.get("duration_s", 0), c.get("preview_url", ""), c.get("hd_url", ""), _now()))
        self.conn.commit()
        return cur.lastrowid

    def clips(self, status=None, source_id=None):
        q, args = "SELECT * FROM clips WHERE 1=1", []
        if status:
            q += " AND status=?"
            args.append(status)
        if source_id:
            q += " AND source_id=?"
            args.append(source_id)
        return self._rows(q + " ORDER BY score DESC, id", args)

    def clip(self, clip_id):
        return self._one("SELECT * FROM clips WHERE id=?", (clip_id,))

    def update_clip(self, clip_id, **fields):
        sets = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE clips SET {sets} WHERE id=?", (*fields.values(), clip_id))
        self.conn.commit()

    def add_variant(self, clip_id, platform, path, hook_file="", text_hook="") -> int:
        cur = self.conn.execute(
            "INSERT INTO variants (clip_id, platform, path, hook_file, text_hook, created) VALUES (?,?,?,?,?,?)",
            (clip_id, platform, path, hook_file, text_hook, _now()))
        self.conn.commit()
        return cur.lastrowid

    def variants(self, status=None, clip_id=None):
        q, args = "SELECT * FROM variants WHERE 1=1", []
        if status:
            q += " AND status=?"
            args.append(status)
        if clip_id:
            q += " AND clip_id=?"
            args.append(clip_id)
        return self._rows(q + " ORDER BY id", args)

    def variant(self, variant_id):
        return self._one("SELECT * FROM variants WHERE id=?", (variant_id,))

    def update_variant(self, variant_id, **fields):
        sets = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE variants SET {sets} WHERE id=?", (*fields.values(), variant_id))
        self.conn.commit()

    def mark_posted(self, variant_id, url="", posted_at=None, account="") -> None:
        v = self.variant(variant_id)
        if not v:
            raise ValueError(f"no variant {variant_id}")
        account = account or handle_from_url(url)
        self.conn.execute(
            "INSERT INTO posts (variant_id, platform, url, posted_at, updated, account) VALUES (?,?,?,?,?,?)"
            " ON CONFLICT(variant_id) DO UPDATE SET url=excluded.url, posted_at=excluded.posted_at,"
            " updated=excluded.updated, account=excluded.account",
            (variant_id, v["platform"], url, posted_at or _now(), _now(), account))
        self.conn.execute("UPDATE variants SET status='posted' WHERE id=?", (variant_id,))
        self.conn.commit()

    def update_post(self, variant_id, **fields):
        fields["updated"] = _now()
        sets = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE posts SET {sets} WHERE variant_id=?", (*fields.values(), variant_id))
        self.conn.commit()

    def posts(self):
        return self._rows("""SELECT p.*, v.clip_id, c.title, s.campaign_id, k.name AS campaign, k.rate_per_1k, k.marketplace
                             FROM posts p JOIN variants v ON v.id=p.variant_id JOIN clips c ON c.id=v.clip_id
                             JOIN sources s ON s.id=c.source_id JOIN campaigns k ON k.id=s.campaign_id ORDER BY p.posted_at""")

    def mark_submitted(self, variant_id, ts=None, views_at_submit=None) -> None:
        """Record the submission and the view count at that moment: what a board pays on is the
        views AFTER it has the URL, so the report needs the baseline to say what is actually earning."""
        if views_at_submit is None:
            p = self._one("SELECT views FROM posts WHERE variant_id=?", (variant_id,))
            views_at_submit = int((p or {}).get("views") or 0)
        self.update_post(variant_id, submitted_at=ts or _now(), views_at_submit=views_at_submit)

    # ---- accounts ------------------------------------------------------------------------
    def add_account(self, handle, platform="tiktok", created_at=None, campaigns=(), connector_id="",
                    notes="") -> None:
        handle = "@" + handle.lstrip("@")
        self.conn.execute(
            "INSERT INTO accounts (handle, platform, created_at, registered_at, campaigns, connector_id, notes)"
            " VALUES (?,?,?,?,?,?,?) ON CONFLICT(handle) DO UPDATE SET platform=excluded.platform,"
            " created_at=excluded.created_at, campaigns=excluded.campaigns,"
            " connector_id=excluded.connector_id, notes=excluded.notes",
            (handle, platform, created_at or _now(), _now(), ",".join(str(c) for c in campaigns), connector_id, notes))
        self.conn.commit()

    def account(self, handle):
        return self._one("SELECT * FROM accounts WHERE handle=?", ("@" + handle.lstrip("@"),))

    def accounts(self, status="active"):
        return self._rows("SELECT * FROM accounts WHERE status=? ORDER BY registered_at", (status,))

    def link_account(self, handle, board) -> None:
        a = self.account(handle)
        if not a:
            raise ValueError(f"no account {handle}")
        boards = sorted({b for b in (a.get("linked") or "").split(",") if b} | {board})
        self.conn.execute("UPDATE accounts SET linked=? WHERE handle=?", (",".join(boards), a["handle"]))
        self.conn.commit()

    def set_account_status(self, handle, status) -> None:
        self.conn.execute("UPDATE accounts SET status=? WHERE handle=?", (status, "@" + handle.lstrip("@")))
        self.conn.commit()

    def account_posts(self, handle):
        return [p for p in self.posts() if p.get("account") == "@" + handle.lstrip("@")]

    # ---- credits / hooks / kv ------------------------------------------------------------
    def record_credits(self, amount: int, note: str = "") -> None:
        self.conn.execute("INSERT INTO credits (ts, amount, note) VALUES (?,?,?)", (_now(), amount, note))
        self.conn.commit()

    def credits_since(self, ts: float) -> int:
        r = self.conn.execute("SELECT COALESCE(SUM(amount),0) AS s FROM credits WHERE ts>=?", (ts,)).fetchone()
        return int(r["s"])

    def credits_this_week(self) -> int:
        now = datetime.now(timezone.utc)
        monday = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        return self.credits_since(monday.timestamp())

    def hook_uses(self) -> dict:
        return {r["hook_file"]: r["uses"] for r in self._rows("SELECT * FROM hook_uses")}

    def bump_hook(self, hook_file: str) -> None:
        self.conn.execute("INSERT INTO hook_uses (hook_file, uses) VALUES (?,1) ON CONFLICT(hook_file) DO UPDATE SET uses=uses+1",
                          (hook_file,))
        self.conn.commit()

    def get_kv(self, k, default=None):
        r = self._one("SELECT v FROM kv WHERE k=?", (k,))
        return json.loads(r["v"]) if r else default

    def set_kv(self, k, v) -> None:
        self.conn.execute("INSERT INTO kv (k, v) VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, json.dumps(v)))
        self.conn.commit()

    # ---- reporting -----------------------------------------------------------------------
    def stats(self) -> dict:
        n = lambda q, a=(): int(self.conn.execute(q, a).fetchone()[0])  # noqa: E731
        posts = self.posts()
        views = sum(p["views"] or 0 for p in posts)
        qviews = sum(p["qualified_views"] or 0 for p in posts)
        expected = sum((p["qualified_views"] or p["views"] or 0) / 1000.0 * (p["rate_per_1k"] or 0) for p in posts)
        return {
            "campaigns": n("SELECT COUNT(*) FROM campaigns WHERE status='active'"),
            "sources": {s: n("SELECT COUNT(*) FROM sources WHERE status=?", (s,)) for s in ("queued", "submitting", "submitted", "clipped", "failed")},
            "clips": {s: n("SELECT COUNT(*) FROM clips WHERE status=?", (s,)) for s in ("new", "downloaded", "transformed", "skipped", "failed")},
            "variants": {s: n("SELECT COUNT(*) FROM variants WHERE status=?", (s,)) for s in ("made", "staged", "scheduled", "posted", "skipped")},
            "posts": len(posts), "views": views, "qualified_views": qviews,
            "usd_expected": round(expected, 2),
            "usd_approved": round(sum(p["usd_approved"] or 0 for p in posts), 2),
            "usd_settled": round(sum(p["usd_settled"] or 0 for p in posts), 2),
            "credits_week": self.credits_this_week(),
            "credits_total": self.credits_since(0),
        }

    def payout_estimate(self) -> dict:
        """What the posted clips are actually worth, not views x rate. A post counts only if it was
        submitted to its board and is past that board's per-post floor. `if_all_paid` is the naive
        views x rate number, kept beside it so the gap between the two is visible."""
        paid, naive, counted = 0.0, 0.0, 0
        for p in self.posts():
            v = p["qualified_views"] or p["views"] or 0
            rate = p["rate_per_1k"] or 0
            naive += v / 1000.0 * rate
            floor = PAYOUT_FLOOR_VIEWS.get((p.get("marketplace") or "").lower(), 0)
            if p.get("submitted_at") and v >= floor:
                paid += v / 1000.0 * rate
                counted += 1
        return {"usd_estimated": round(paid, 2), "usd_if_all_paid": round(naive, 2), "posts_paying": counted}

    def payout_by_campaign(self) -> list:
        """Per live campaign: what is submitted, the views it gained since submission, the estimate at the
        campaign's rate (posts past the board's per-post floor only), and what can't be submitted yet
        because its account isn't linked on the board. The views-to-dollars line Alex reads."""
        linked = {a["handle"]: (a.get("linked") or "").split(",")
                  for a in self._rows("SELECT handle, linked FROM accounts")}
        out = {}
        for p in self.posts():
            board = (p.get("marketplace") or "").lower()
            c = out.setdefault(p["campaign"], {"campaign": p["campaign"], "board": board,
                                               "rate": p["rate_per_1k"] or 0, "submitted": 0,
                                               "views_since_submit": 0, "usd_est": 0.0, "unsubmitted": 0,
                                               "blocked_unlinked": 0, "blocked_views": 0})
            views = int(p["views"] or 0)
            if p.get("submitted_at"):
                since = max(0, views - int(p.get("views_at_submit") or 0))
                c["submitted"] += 1
                c["views_since_submit"] += since
                if since >= PAYOUT_FLOOR_VIEWS.get(board, 0):
                    c["usd_est"] += since / 1000.0 * c["rate"]
            else:
                c["unsubmitted"] += 1
                if board not in linked.get(p.get("account") or "", []):
                    c["blocked_unlinked"] += 1
                    c["blocked_views"] += views
        for c in out.values():
            c["usd_est"] = round(c["usd_est"], 2)
        return sorted(out.values(), key=lambda c: (-c["usd_est"], -c["views_since_submit"], c["campaign"]))

    def payout_lines(self, campaigns=None) -> list:
        rows = [c for c in self.payout_by_campaign() if not campaigns or c["campaign"] in campaigns]
        return [f"  {c['campaign']}: {c['submitted']} submitted · {c['views_since_submit']:,} views since submission "
                f"· est ${c['usd_est']:.2f} at ${c['rate']:.2f}/1k"
                + (f" · {c['blocked_unlinked']} post(s) / {c['blocked_views']:,} views blocked: account not linked on "
                   f"{c['board']}" if c["blocked_unlinked"] else "")
                for c in rows]

    def report(self) -> str:
        s = self.stats()
        lines = [f"clipbot — {datetime.now().strftime('%Y-%m-%d %H:%M')}",
                 f"  campaigns {s['campaigns']} · sources {s['sources']} · clips {s['clips']}",
                 f"  variants {s['variants']}",
                 f"  posts {s['posts']} · views {s['views']:,} (qualified {s['qualified_views']:,}) · "
                 f"expected ${s['usd_expected']:.2f} · approved ${s['usd_approved']:.2f} · settled ${s['usd_settled']:.2f}",
                 f"  credits this week {s['credits_week']} · total {s['credits_total']}"]
        live = {c["name"] for c in self.campaigns()}
        pay = self.payout_lines(live)
        if pay:
            lines += ["  payout (live campaigns):"] + pay
        return "\n".join(lines)
