#!/usr/bin/env python3
"""Division I transfer tracker — guard depth at Alex's 13 target programs.

Alex plays a PG/SG hybrid and is looking to transfer up to D1 *after the 2026-27
season*, which means he'd arrive for **2027-28**. Everything here is scored
against that arrival date, not against today's roster. A senior who is lighting
it up right now is not a blocker — he's gone before Alex gets there. A quiet
freshman is a blocker for three years. Ranking by "who's good now" would point
him at exactly the wrong schools, so the whole model keys off
`present_at_arrival`.

Data comes from ESPN's public JSON endpoints:
  - roster:  site.api.espn.com/.../teams/{id}/roster
  - stats:   sports.core.api.espn.com/.../seasons/{yr}/types/2/athletes/{id}/statistics

Two things about that data are load-bearing and easy to get wrong:

1. **ESPN's position field is only Guard/Forward/Center.** There is no PG/SG
   split anywhere in the feed. So the PG-vs-SG read here is *derived* from
   production (assists per 40, assist/turnover, usage, height) and labelled as a
   lean, never asserted as fact. `role_basis` records which it was so the UI can
   say "derived" rather than implying ESPN told us.

2. **Roster season varies by program.** Syracuse flips to next season's roster in
   August; Le Moyne was still serving 2025-26 on the same day. Class year is
   therefore meaningless until it's normalised against the season the roster
   actually describes — see `_present_at_arrival`. Every school carries its
   `roster_season` through to the UI so a stale one is visible, not silent.

Season-year convention is ESPN's: season `2026` == the 2025-26 season.

Fail-soft everywhere. One school 500ing must degrade to "that school is stale",
never take down the page Alex checks from his phone.
"""

from __future__ import annotations

import json
import os
import sqlite3
import ssl
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# The server runs UTC and this module is called from both nodes; a naive
# datetime.now() is already tomorrow every evening after 8 PM ET. See CLAUDE.md.
ET = ZoneInfo("America/New_York")

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "d1_tracker.db")

# The season Alex would arrive for: he transfers after 2026-27, so 2027-28,
# which is ESPN season year 2028.
ARRIVAL_SEASON = 2028

# ESPN's edge exact-matches the User-Agent on these public endpoints: `curl/8.4.0`
# returns 200, while a descriptive string ("CLARVIS-d1-tracker/1.0") and a
# browser string both 403. Nothing here is authenticated or paywalled — it's the
# same JSON the public team pages render from — so this is the UA that works
# rather than a disguise. POLITE_DELAY keeps a full refresh to ~2 req/sec.
USER_AGENT = "curl/8.4.0"
FETCH_TIMEOUT = 20
POLITE_DELAY = 0.4          # seconds between requests — ~90 calls a refresh

# Refresh cadence. In-season the tracker is worth pulling several times a day
# (box scores land nightly); in the offseason a daily pull is plenty and the
# roster barely moves.
TTL_IN_SEASON_S = 6 * 3600
TTL_OFFSEASON_S = 24 * 3600


# ============================================================
# The target list
# ============================================================
# `staff` is the actual actionable field — for a transfer you email the
# assistant/recruiting coordinator, not the head coach, and ESPN's feed only
# carries head coaches. These are the official athletics staff directories.
SCHOOLS = [
    {"key": "bu",     "espn": "104",  "name": "Boston University",  "short": "BU",
     "staff": "https://goterriers.com/sports/mens-basketball/coaches"},
    {"key": "bc",     "espn": "103",  "name": "Boston College",     "short": "BC",
     "staff": "https://bceagles.com/sports/mens-basketball/coaches"},
    {"key": "harvard", "espn": "108", "name": "Harvard",            "short": "HARV",
     "staff": "https://gocrimson.com/sports/mens-basketball/coaches"},
    {"key": "holycross", "espn": "107", "name": "Holy Cross",       "short": "HC",
     "staff": "https://goholycross.com/sports/mens-basketball/coaches"},
    {"key": "northeastern", "espn": "111", "name": "Northeastern",  "short": "NU",
     "staff": "https://nuhuskies.com/sports/mens-basketball/coaches"},
    {"key": "umass",  "espn": "113",  "name": "UMass",              "short": "UMASS",
     "staff": "https://umassathletics.com/sports/mens-basketball/coaches"},
    {"key": "yale",   "espn": "43",   "name": "Yale",               "short": "YALE",
     "staff": "https://yalebulldogs.com/sports/mens-basketball/coaches"},
    {"key": "fairfield", "espn": "2217", "name": "Fairfield",       "short": "FAIR",
     "staff": "https://fairfieldstags.com/sports/mens-basketball/coaches"},
    {"key": "sacredheart", "espn": "2529", "name": "Sacred Heart",  "short": "SHU",
     "staff": "https://sacredheartpioneers.com/sports/mens-basketball/coaches"},
    {"key": "lemoyne", "espn": "2330", "name": "Le Moyne",          "short": "LEM",
     "staff": "https://lemoynedolphins.com/sports/mens-basketball/coaches"},
    {"key": "cornell", "espn": "172", "name": "Cornell",            "short": "COR",
     "staff": "https://cornellbigred.com/sports/mens-basketball/coaches"},
    {"key": "colgate", "espn": "2142", "name": "Colgate",           "short": "COLG",
     "staff": "https://gocolgateraiders.com/sports/mens-basketball/coaches"},
    {"key": "syracuse", "espn": "183", "name": "Syracuse",          "short": "SYR",
     "staff": "https://cuse.com/sports/mens-basketball/coaches"},
]

SCHOOL_BY_KEY = {s["key"]: s for s in SCHOOLS}

# Level of competition, held separately from the weakness score on purpose.
# A wide-open guard room at Syracuse is not the same opportunity as a wide-open
# one at Le Moyne, and folding that into one number would hide the trade-off
# Alex actually has to make. Shown as its own column instead.
TIER = {
    "high-major": 1,    # ACC
    "mid-major": 2,     # CAA / MAC / A-10-ish
    "low-major": 3,     # Ivy / Patriot / MAAC / NEC
}
CONF_TIER = {
    "ACC": "high-major", "Atlantic Coast Conference": "high-major",
    "CAA": "mid-major", "Coastal Athletic Association": "mid-major",
    "MAC": "mid-major", "Mid-American Conference": "mid-major",
    "A10": "mid-major", "Atlantic 10 Conference": "mid-major",
}

# ESPN calls the MAAC "Metro", which reads as a different league entirely on a
# page Alex is scanning quickly.
CONF_ALIAS = {"Metro": "MAAC"}


# ============================================================
# HTTP
# ============================================================
def _ssl_context() -> ssl.SSLContext:
    """Verified TLS, using certifi's bundle when it's importable.

    python.org builds on macOS ship without a system trust store wired up, so a
    plain default context raises CERTIFICATE_VERIFY_FAILED here while curl is
    fine. Falls back to the default context (still verifying) rather than ever
    disabling verification."""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


_SSL_CTX = _ssl_context()


def _get(url: str, tries: int = 3, quiet_404: bool = False) -> dict | None:
    """GET JSON, fail-soft. Returns None rather than raising.

    ESPN rate-limits and occasionally 502s under load; a single school's blip
    should cost that school's freshness and nothing else.

    404 is never retried — it's an answer, not a blip. On the statistics
    endpoint it's also the *normal* response for a true freshman with no college
    season behind him, which is why callers can silence it: left noisy, a routine
    refresh printed 20+ "fetch failed" lines and buried the failures that matter.
    """
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT,
                                        context=_SSL_CTX) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                if not quiet_404:
                    print(f"[d1_tracker] not found: {url}")
                return None
            if attempt == tries - 1:
                print(f"[d1_tracker] fetch failed {url}: {e}")
                return None
            time.sleep(1.5 * (attempt + 1))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError,
                OSError) as e:
            if attempt == tries - 1:
                print(f"[d1_tracker] fetch failed {url}: {e}")
                return None
            time.sleep(1.5 * (attempt + 1))
    return None


# ============================================================
# Fetch
# ============================================================
def fetch_roster(espn_id: str) -> tuple[int | None, str, list[dict]]:
    """Return (season_year, season_label, raw athletes) for a team."""
    d = _get("https://site.api.espn.com/apis/site/v2/sports/basketball/"
             f"mens-college-basketball/teams/{espn_id}/roster")
    if not d:
        return None, "", []
    season = d.get("season") or {}
    return (season.get("year"), season.get("displayName", ""),
            d.get("athletes") or [])


def fetch_conference(espn_id: str, season: int) -> str:
    """Conference name, via the team's group id.

    Worth a live lookup rather than a hardcoded map: UMass moved to the MAC in
    2025 and Sacred Heart to the MAAC in 2024. A stale constant here would
    silently mis-tier two of the thirteen."""
    t = _get("https://site.api.espn.com/apis/site/v2/sports/basketball/"
             f"mens-college-basketball/teams/{espn_id}")
    gid = (((t or {}).get("team") or {}).get("groups") or {}).get("id")
    if not gid:
        return ""
    g = _get("https://sports.core.api.espn.com/v2/sports/basketball/leagues/"
             f"mens-college-basketball/seasons/{season}/types/2/groups/{gid}")
    if not g:
        return ""
    return g.get("shortName") or g.get("name") or ""


def fetch_player_stats(athlete_id: str, season: int) -> dict:
    """Flatten one player's season stat line into a plain dict.

    ESPN nests stats under splits.categories[].stats[]; names collide across
    categories (`avgPoints` is offensive, `avgSteals` defensive) but not in ways
    that matter, so a flat merge is safe and much easier to read downstream."""
    d = _get("https://sports.core.api.espn.com/v2/sports/basketball/leagues/"
             f"mens-college-basketball/seasons/{season}/types/2/athletes/"
             f"{athlete_id}/statistics", quiet_404=True)
    if not d:
        return {}
    out: dict = {}
    for cat in ((d.get("splits") or {}).get("categories") or []):
        for s in cat.get("stats") or []:
            name, val = s.get("name"), s.get("value")
            if name is None:
                continue
            out[name] = val if val is not None else s.get("displayValue")
    return out


# ============================================================
# Derivation
# ============================================================
def _present_at_arrival(experience_years: int | None,
                        roster_season: int | None) -> bool | None:
    """Will this player still be on the roster when Alex arrives (2027-28)?

    Normalised against the season the roster actually describes, because that
    season is not the same across programs on any given day (see module
    docstring). `experience.years` is 1=FR … 4=SR, so a player has
    `4 - years` seasons left *after* the roster's own season.

    Approximate by construction: redshirts, medical years and COVID-era
    eligibility all extend a career past what the class label implies. Treated
    as a lean, and the UI says so. Returns None when class is unknown.
    """
    if not experience_years or not roster_season:
        return None
    seasons_left_after = 4 - int(experience_years)
    needed = ARRIVAL_SEASON - int(roster_season)
    return seasons_left_after >= needed


def _height_inches(a: dict) -> int | None:
    h = a.get("height")
    try:
        return int(h) if h else None
    except (TypeError, ValueError):
        return None


def classify_guard(stats: dict, height_in: int | None) -> tuple[str, str, str]:
    """Derive a PG / combo / SG lean. Returns (label, basis, sample).

    ESPN has no PG/SG split, so this reads it off how the player actually
    played. Assists per 40 is the spine of it — it survives a bench role, where
    raw APG doesn't, and Alex needs to know whether a 12-minute freshman is
    being *developed as* a lead guard.

    Sample quality is tracked separately from the label, because "not many
    minutes" covers two players who mean opposite things to Alex:

      - a 3-mpg senior who is genuinely not in the rotation, and
      - a starter who played 27 mpg across two games before an injury ended his
        season (Le Moyne's Ametri Moss, 2025-26).

    Collapsing both into "unproven" would hide a rotation guard behind the same
    label as a walk-on. So the rates get read whenever the role was real
    (>=12 mpg over >=2 games) and the thin sample is disclosed in `basis`
    instead of thrown away.
    """
    mins = _f(stats.get("minutes"))
    mpg = _f(stats.get("avgMinutes"))
    gp = _f(stats.get("gamesPlayed"))
    ast = _f(stats.get("assists"))
    tov = _f(stats.get("turnovers") or stats.get("totalTurnovers"))

    if not gp:
        return "Guard (no data)", "no games logged", "none"

    if mins >= 150:
        sample = "full"
    elif mpg >= 12 and gp >= 2:
        sample = "small"
    else:
        sample = "none"

    if sample == "none":
        label = "Guard (deep bench)" if mpg < 8 else "Guard (unproven)"
        return label, f"{gp:.0f} gp, {mpg:.1f} mpg", "none"

    ast40 = ast / mins * 40 if mins else 0.0
    # No turnovers at all is a real (if lucky) line on a short sample; call it
    # 3.0 rather than dividing by zero or discarding the player.
    ato = (ast / tov) if tov else (3.0 if ast else 0.0)

    # Thresholds calibrated on the returned lines across these 13 rosters: true
    # lead guards land 5+ ast/40, pure off-guards under 3.
    if ast40 >= 5.0 and ato >= 1.4:
        role = "Lead guard (PG)"
    elif ast40 >= 3.2:
        role = "Combo guard"
    else:
        role = "Off guard (SG)"

    basis = f"{ast40:.1f} ast/40, {ato:.1f} A:TO"
    if sample == "small":
        basis += f" — {gp:.0f} gp only"
    return role, basis, sample


def _f(v) -> float:
    """Coerce ESPN's mixed str/num stat values to float, 0.0 on junk."""
    try:
        if v is None:
            return 0.0
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def build_player(a: dict, stats: dict, roster_season: int | None) -> dict:
    """One guard, flattened to what the tracker and the UI both need."""
    exp = (a.get("experience") or {}).get("years")
    height_in = _height_inches(a)
    role, basis, sample = classify_guard(stats, height_in)
    mins = _f(stats.get("minutes"))
    gp = _f(stats.get("gamesPlayed"))
    ast = _f(stats.get("assists"))
    tov = _f(stats.get("turnovers") or stats.get("totalTurnovers"))

    return {
        "id": a.get("id"),
        "name": a.get("fullName") or a.get("displayName") or "",
        "jersey": a.get("jersey") or "",
        "height": a.get("displayHeight") or "",
        "height_in": height_in,
        "weight": a.get("displayWeight") or "",
        "class": (a.get("experience") or {}).get("displayValue") or "",
        "class_abbr": (a.get("experience") or {}).get("abbreviation") or "",
        "exp_years": exp,
        "present_at_arrival": _present_at_arrival(exp, roster_season),
        "role": role,
        "role_basis": basis,
        "sample": sample,
        "injured": bool(a.get("injuries")),
        # counting + rate stats — everything Alex asked to track
        "gp": int(gp), "gs": int(_f(stats.get("gamesStarted"))),
        "mpg": round(_f(stats.get("avgMinutes")), 1),
        "minutes": int(mins),
        "ppg": round(_f(stats.get("avgPoints")), 1),
        "apg": round(_f(stats.get("avgAssists")), 1),
        "rpg": round(_f(stats.get("avgRebounds")), 1),
        "spg": round(_f(stats.get("avgSteals")), 1),
        "bpg": round(_f(stats.get("avgBlocks")), 1),
        "topg": round(_f(stats.get("avgTurnovers")), 1),
        "fg_pct": round(_f(stats.get("fieldGoalPct")), 1),
        "fg": stats.get("fieldGoals") or "",
        "tp_made": int(_f(stats.get("threePointFieldGoalsMade"))),
        "tp_att": int(_f(stats.get("threePointFieldGoalsAttempted"))),
        "tp_pct": round(_tp_pct(stats), 1),
        "ft_pct": round(_f(stats.get("freeThrowPct")), 1),
        "per": round(_f(stats.get("PER")), 1),
        "ast_to": round((ast / tov) if tov else 0.0, 2),
        "ast40": round((ast / mins * 40) if mins else 0.0, 1),
        "plus_minus": int(_f(stats.get("plusMinus"))),
        "has_stats": bool(mins),
    }


def _tp_pct(stats: dict) -> float:
    """ESPN omits threePointFieldGoalPct on some lines; derive it."""
    direct = stats.get("threePointFieldGoalPct")
    if direct not in (None, ""):
        return _f(direct)
    made = _f(stats.get("threePointFieldGoalsMade"))
    att = _f(stats.get("threePointFieldGoalsAttempted"))
    return (made / att * 100) if att else 0.0


# ============================================================
# Scoring
# ============================================================
def score_school(guards: list[dict]) -> dict:
    """Score how locked-down a guard room is for 2027-28. 0 = wide open.

    Only guards who'll still be there when Alex arrives can block him, so
    departing seniors are excluded from the blocking math entirely — they show
    up as *vacated* minutes, which is the opposite signal.

    Three components, deliberately kept legible rather than tuned into a black
    box Alex can't argue with:

      returning_quality (0-55) — the entrenched guys. Driven by minutes and
        production of returners, with the top returner weighted hardest: one
        all-conference lead guard blocks more than three replaceable bench
        guards do.
      crowding (0-25) — how many bodies are already in the room.
      vacancy (-30-0) — a credit for minutes walking out the door.

    Clamped to 0-100.
    """
    returning = [g for g in guards if g.get("present_at_arrival") is not False]
    departing = [g for g in guards if g.get("present_at_arrival") is False]

    # --- returning quality -------------------------------------------------
    # Per-guard "entrenchment": minutes played is the honest proxy for whether
    # a coach trusts him, scaled by production.
    def entrench(g: dict) -> float:
        mpg = g.get("mpg") or 0
        prod = (g.get("ppg") or 0) + 1.6 * (g.get("apg") or 0)
        return min(1.0, mpg / 30.0) * min(1.0, prod / 18.0)

    scores = sorted((entrench(g) for g in returning), reverse=True)
    quality = 0.0
    if scores:
        # top returner counts double; the next two at full weight; rest decay
        weights = [2.0, 1.0, 1.0] + [0.5] * max(0, len(scores) - 3)
        quality = sum(s * w for s, w in zip(scores, weights))
    returning_quality = min(55.0, quality * 16.0)

    # --- crowding ----------------------------------------------------------
    # A room of 8 returning guards is hard to crack even if none is a star.
    crowding = min(25.0, max(0, len(returning) - 2) * 4.5)

    # --- vacancy credit ----------------------------------------------------
    vacated_mpg = sum(g.get("mpg") or 0 for g in departing)
    total_mpg = sum(g.get("mpg") or 0 for g in guards) or 1
    vacated_share = vacated_mpg / total_mpg
    vacancy = -min(30.0, vacated_share * 45.0)

    score = max(0.0, min(100.0, returning_quality + crowding + vacancy))

    proven_returning = [g for g in returning if (g.get("mpg") or 0) >= 15]
    return {
        "score": round(score, 1),
        "returning_quality": round(returning_quality, 1),
        "crowding": round(crowding, 1),
        "vacancy": round(vacancy, 1),
        "n_guards": len(guards),
        "n_returning": len(returning),
        "n_departing": len(departing),
        "n_proven_returning": len(proven_returning),
        "vacated_mpg": round(vacated_mpg, 1),
        "vacated_share": round(vacated_share * 100, 1),
        "n_lead_returning": len([g for g in returning
                                 if g["role"].startswith("Lead")]),
        "n_lead_departing": len([g for g in departing
                                 if g["role"].startswith("Lead")]),
    }


def read_school(guards: list[dict], sc: dict, roster_current: bool = True) -> str:
    """One plain sentence naming the actual opening, or the actual wall.

    The number ranks; this is what makes it actionable. Deliberately concrete
    about *which* guys and *what* minutes, because "score 34" tells Alex
    nothing he can put in an email to a coach.
    """
    if not guards:
        return "No guard data yet — roster not posted."
    if not roster_current:
        return ("Last season's roster — incoming class not listed yet, so this "
                "room will fill. " + _read_body(guards, sc))
    return _read_body(guards, sc)


def _read_body(guards: list[dict], sc: dict) -> str:
    if sc["n_proven_returning"] == 0 and sc["n_departing"]:
        return (f"Wide open — all {sc['n_departing']} rotation guard(s) gone, "
                f"nobody returning with 15+ mpg.")
    if sc["n_lead_returning"] == 0 and sc["n_lead_departing"] > 0:
        return ("Lead-guard hole — the primary ball-handler graduates and "
                "nobody returning profiles as a PG.")
    if sc["vacated_share"] >= 45:
        return (f"{sc['vacated_share']:.0f}% of guard minutes walk out the door; "
                f"{sc['n_proven_returning']} proven returner(s) left.")
    if sc["n_proven_returning"] >= 3:
        return (f"Crowded — {sc['n_proven_returning']} guards returning with "
                f"15+ mpg. Hard room to crack.")
    top = max(guards, key=lambda g: (g.get("present_at_arrival") is not False,
                                     g.get("mpg") or 0), default=None)
    if top and top.get("present_at_arrival") is not False and (top.get("mpg") or 0) >= 25:
        return (f"{top['name']} ({top['class_abbr']}, {top['mpg']} mpg, "
                f"{top['ppg']} ppg) returns and holds the spot.")
    return (f"{sc['n_returning']} guard(s) returning, "
            f"{sc['n_proven_returning']} of them proven.")


# ============================================================
# Storage
# ============================================================
def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def init_db() -> None:
    with _conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS school_snapshot (
            key TEXT PRIMARY KEY,
            payload TEXT NOT NULL,
            fetched_at TEXT NOT NULL,
            ok INTEGER NOT NULL DEFAULT 1
        )""")
        # History lets the tab show movement — "Fairfield's room opened up since
        # November" is the signal Alex actually acts on, and it only exists if
        # every refresh leaves a row behind.
        c.execute("""CREATE TABLE IF NOT EXISTS score_history (
            key TEXT NOT NULL,
            scored_at TEXT NOT NULL,
            score REAL NOT NULL,
            n_returning INTEGER,
            PRIMARY KEY (key, scored_at)
        )""")


def _save(key: str, payload: dict, ok: bool = True) -> None:
    # init here rather than only in refresh_all: refresh_school is called
    # directly by the chat tool and by tests, and a missing table there would
    # surface as "roster unavailable" for every school.
    init_db()
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as c:
        c.execute("INSERT INTO school_snapshot (key, payload, fetched_at, ok) "
                  "VALUES (?,?,?,?) ON CONFLICT(key) DO UPDATE SET "
                  "payload=excluded.payload, fetched_at=excluded.fetched_at, "
                  "ok=excluded.ok",
                  (key, json.dumps(payload), now, 1 if ok else 0))
        if ok and payload.get("score"):
            # one row per day per school — enough to draw a trend, not enough to
            # bloat the db on a 6-hourly refresh
            day = datetime.now(ET).date().isoformat()
            c.execute("INSERT OR REPLACE INTO score_history "
                      "(key, scored_at, score, n_returning) VALUES (?,?,?,?)",
                      (key, day, payload["score"]["score"],
                       payload["score"]["n_returning"]))


def load_snapshot(key: str) -> dict | None:
    init_db()
    with _conn() as c:
        r = c.execute("SELECT payload, fetched_at, ok FROM school_snapshot "
                      "WHERE key=?", (key,)).fetchone()
    if not r:
        return None
    try:
        p = json.loads(r["payload"])
    except json.JSONDecodeError:
        return None
    p["fetched_at"] = r["fetched_at"]
    p["ok"] = bool(r["ok"])
    return p


def score_trend(key: str, limit: int = 30) -> list[dict]:
    init_db()
    with _conn() as c:
        rows = c.execute("SELECT scored_at, score FROM score_history WHERE key=? "
                         "ORDER BY scored_at DESC LIMIT ?", (key, limit)).fetchall()
    return [{"date": r["scored_at"], "score": r["score"]} for r in reversed(rows)]


# ============================================================
# Refresh
# ============================================================
def in_season(now: datetime | None = None) -> bool:
    """Nov 1 - Apr 10. Drives refresh cadence only."""
    n = now or datetime.now(ET)
    return n.month >= 11 or n.month <= 3 or (n.month == 4 and n.day <= 10)


def upcoming_season(now: datetime | None = None) -> int:
    """ESPN season year of the next/current season to be played."""
    n = now or datetime.now(ET)
    return n.year + 1 if n.month >= 5 else n.year


def roster_is_current(roster_season: int | None, now: datetime | None = None) -> bool:
    """Has this program posted the upcoming season's roster yet?

    This is the single biggest confounder in the ranking and it has to be
    visible. Programs update at wildly different times — on 2026-08-28 Syracuse,
    BC, Yale, UMass, Fairfield and Holy Cross had posted 2026-27 while Cornell,
    Northeastern, Colgate, BU, Le Moyne, Sacred Heart and Harvard were still
    serving 2025-26.

    A stale roster has **no incoming freshmen or transfers on it**, so that
    program's guard room looks emptier than it will actually be, which pushes it
    up the "weakest" ranking for a reason that has nothing to do with
    opportunity. On the day this was built, all three of the top-ranked weakest
    rooms were stale-roster schools — so the bias was material, not theoretical.

    Self-heals: by November every program has posted. Until then the UI flags
    these rather than pretending the comparison is clean.
    """
    if not roster_season:
        return False
    return int(roster_season) >= upcoming_season(now)


def ttl_seconds() -> int:
    return TTL_IN_SEASON_S if in_season() else TTL_OFFSEASON_S


def is_stale(key: str) -> bool:
    snap = load_snapshot(key)
    if not snap or not snap.get("ok"):
        return True
    try:
        fetched = datetime.fromisoformat(snap["fetched_at"])
    except (ValueError, KeyError):
        return True
    if fetched.tzinfo is None:
        fetched = fetched.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - fetched) > timedelta(seconds=ttl_seconds())


def refresh_school(school: dict, stats_season: int | None = None) -> dict:
    """Pull one school. Never raises — returns a payload with ok=False instead."""
    key, espn = school["key"], school["espn"]
    season, season_label, athletes = fetch_roster(espn)
    if not athletes:
        payload = {"key": key, "name": school["name"], "short": school["short"],
                   "staff": school["staff"], "guards": [], "score": None,
                   "roster_season": season, "roster_season_label": season_label,
                   "error": "roster unavailable"}
        _save(key, payload, ok=False)
        return payload

    # Stats always come from the most recently *completed* season. During
    # preseason the roster is next year's but no games have been played, so
    # asking for that season's stats returns nothing for everyone.
    stat_season = stats_season or _completed_season(season)

    guards = []
    for a in athletes:
        pos = (a.get("position") or {}).get("abbreviation") or ""
        if pos != "G":
            continue
        time.sleep(POLITE_DELAY)
        stats = fetch_player_stats(a.get("id"), stat_season)
        guards.append(build_player(a, stats, season))

    # deepest rotation first — that's the order he reads a depth chart in
    guards.sort(key=lambda g: (-(g.get("mpg") or 0), g.get("name") or ""))
    sc = score_school(guards)
    conf = fetch_conference(espn, _completed_season(season))
    conf = CONF_ALIAS.get(conf, conf)
    tier = CONF_TIER.get(conf, "low-major")
    current = roster_is_current(season)

    payload = {
        "key": key, "name": school["name"], "short": school["short"],
        "staff": school["staff"],
        "espn_roster": ("https://www.espn.com/mens-college-basketball/team/roster/"
                        f"_/id/{espn}"),
        "conference": conf, "tier": tier, "tier_rank": TIER.get(tier, 3),
        "roster_season": season, "roster_season_label": season_label,
        "roster_current": current,
        "confidence": "high" if current else "low",
        "stats_season": stat_season,
        "stats_season_label": f"{stat_season - 1}-{str(stat_season)[2:]}",
        "guards": guards, "score": sc,
        "read": read_school(guards, sc, current),
        "error": None,
    }
    _save(key, payload, ok=True)
    return payload


def _completed_season(roster_season: int | None) -> int:
    """Most recent season with real box scores in it.

    ESPN flips `season` to the upcoming year during preseason. Asking that year
    for statistics returns empty lines for the entire roster, which would render
    as "every guard averages 0" — indistinguishable from a team of walk-ons.
    """
    now = datetime.now(ET)
    # Before November, the completed season is the one ending this calendar year.
    natural = now.year if now.month < 11 else now.year + 1
    if not roster_season:
        return natural
    return min(int(roster_season), natural)


def refresh_all(only_stale: bool = True, keys: list[str] | None = None) -> dict:
    """Refresh every school. Returns a small run summary for the heartbeat."""
    init_db()
    targets = [SCHOOL_BY_KEY[k] for k in keys if k in SCHOOL_BY_KEY] if keys else SCHOOLS
    done, skipped, failed = [], [], []
    for s in targets:
        if only_stale and not is_stale(s["key"]):
            skipped.append(s["key"])
            continue
        try:
            p = refresh_school(s)
            (done if not p.get("error") else failed).append(s["key"])
        except Exception as e:                       # never let one school abort the run
            print(f"[d1_tracker] {s['key']} failed: {e}")
            failed.append(s["key"])
    return {"refreshed": done, "skipped": skipped, "failed": failed,
            "at": datetime.now(ET).isoformat()}


# ============================================================
# Read model — what the tab and the chat tool both consume
# ============================================================
def deck_data() -> dict:
    """Everything the D1 tab renders, ranked weakest guard room first.

    Reads cache only. A cold cache returns an honest empty state rather than
    blocking the page on ~90 HTTP calls."""
    init_db()
    schools = []
    for s in SCHOOLS:
        snap = load_snapshot(s["key"])
        if not snap:
            schools.append({"key": s["key"], "name": s["name"],
                            "short": s["short"], "staff": s["staff"],
                            "guards": [], "score": None, "pending": True,
                            "read": "Not fetched yet."})
            continue
        snap["pending"] = False
        snap["trend"] = score_trend(s["key"], 20)
        schools.append(snap)

    # Score first, then vacated share as the tie-break: the 0-100 clamp means
    # two genuinely wide-open rooms can both land on 0.0, and without a second
    # key their order would flip between refreshes for no reason Alex could see.
    ranked = sorted(
        [s for s in schools if s.get("score")],
        key=lambda s: (s["score"]["score"], -s["score"]["vacated_share"],
                       s["short"]))
    for i, s in enumerate(ranked, 1):
        s["rank"] = i
    unranked = [s for s in schools if not s.get("score")]

    fetched = [s.get("fetched_at") for s in schools if s.get("fetched_at")]
    stale_rosters = [s["short"] for s in ranked if not s.get("roster_current")]
    return {
        "schools": ranked + unranked,
        "n_ranked": len(ranked),
        "n_pending": len(unranked),
        "arrival_season": f"{ARRIVAL_SEASON - 1}-{str(ARRIVAL_SEASON)[2:]}",
        "in_season": in_season(),
        "refresh_hours": ttl_seconds() // 3600,
        "last_fetch": max(fetched) if fetched else None,
        "generated_at": datetime.now(ET).isoformat(),
        # The comparability caveat, computed rather than written into the UI as
        # prose, so it disappears on its own once every program has posted.
        "stale_rosters": stale_rosters,
        "n_stale": len(stale_rosters),
        "caveat": (
            f"{len(stale_rosters)} of {len(ranked)} programs still show last "
            "season's roster, so their incoming freshmen and transfers aren't "
            "counted yet — they rank as more open than they'll turn out to be. "
            "Resolves once rosters post (usually by November)."
        ) if stale_rosters else "",
    }


def hud_summary() -> dict:
    """Three lines for the deck tile. Cache-only and deliberately cheap.

    The HUD feed is shared by every page, so this reads the snapshot rows and
    does no scoring, no history and no network — same reasoning as the August
    tab keeping its own endpoint."""
    init_db()
    with _conn() as c:
        rows = c.execute("SELECT key, payload FROM school_snapshot "
                         "WHERE ok=1").fetchall()
    scored = []
    for r in rows:
        try:
            p = json.loads(r["payload"])
        except json.JSONDecodeError:
            continue
        if p.get("score"):
            scored.append((p["score"]["score"], p["short"], p["score"]))
    scored.sort()
    return {
        "n": len(scored),
        "top": [{"short": s, "score": round(v, 1),
                 "returning": sc["n_returning"], "out": sc["n_departing"]}
                for v, s, sc in scored[:3]],
        "arrival": f"{ARRIVAL_SEASON - 1}-{str(ARRIVAL_SEASON)[2:]}",
    }


def summarize_for_chat(key: str | None = None) -> str:
    """Text form, for the chat tool. Same numbers the tab shows."""
    d = deck_data()
    if key:
        k = key.strip().lower()
        match = next((s for s in d["schools"]
                      if s["key"] == k or s["short"].lower() == k
                      or k in s["name"].lower()), None)
        if not match:
            return f"No target school matching '{key}'. Tracked: " + \
                   ", ".join(s["short"] for s in d["schools"])
        return _school_text(match, d)

    lines = [f"D1 guard-room tracker — ranked weakest to strongest for "
             f"{d['arrival_season']} (Alex's arrival season).", ""]
    for s in d["schools"]:
        if not s.get("score"):
            lines.append(f"  --  {s.get('short', '?'):<6} "
                         f"{s.get('read') or 'pending'}")
            continue
        sc = s["score"]
        flag = "" if s.get("roster_current") else "  [stale roster]"
        # .get throughout: a snapshot written by an older build of this module
        # can be missing newer keys, and the chat tool degrading to a blank
        # cell beats it raising and taking the whole answer with it.
        lines.append(
            f"  {s.get('rank', '?'):>2}. {s.get('short', '?'):<6} "
            f"score {sc.get('score', 0):>5.1f}  "
            f"{sc.get('n_returning', 0)}ret/{sc.get('n_departing', 0)}out  "
            f"({s.get('conference', '?')}, {s.get('tier', '?')}){flag}  "
            f"{s.get('read', '')}")
    if d["n_pending"]:
        lines.append(f"\n{d['n_pending']} school(s) not fetched yet.")
    if d.get("caveat"):
        lines.append(f"\nCaveat: {d['caveat']}")
    lines.append(f"\nLast fetch: {d.get('last_fetch') or 'never'}. "
                 f"Refreshes every {d['refresh_hours']}h.")
    return "\n".join(lines)


def _school_text(s: dict, d: dict) -> str:
    sc = s.get("score") or {}
    out = [f"{s['name']} ({s.get('conference','?')}, {s.get('tier','?')}) — "
           f"rank {s.get('rank','?')} of {d['n_ranked']} weakest guard room",
           f"Read: {s.get('read','')}",
           f"Roster shown: {s.get('roster_season_label','?')} · "
           f"stats from {s.get('stats_season_label','?')}",
           f"Score {sc.get('score','?')} "
           f"(returning quality {sc.get('returning_quality','?')}, "
           f"crowding {sc.get('crowding','?')}, "
           f"vacancy credit {sc.get('vacancy','?')})",
           f"Staff directory: {s.get('staff','')}", "",
           "Guards:"]
    for g in s.get("guards", []):
        tag = ("RETURNS" if g.get("present_at_arrival") else
               "GONE" if g.get("present_at_arrival") is False else "?")
        out.append(
            f"  {g.get('name', '?')} ({g.get('class_abbr', '?')}, "
            f"{g.get('height', '?')}) [{tag}] {g.get('role', '?')} — "
            f"{g.get('mpg', 0)} mpg, {g.get('ppg', 0)} ppg, "
            f"{g.get('apg', 0)} apg, {g.get('topg', 0)} to, "
            f"{g.get('fg_pct', 0)}% fg, {g.get('tp_pct', 0)}% 3p, "
            f"{g.get('ft_pct', 0)}% ft, {g.get('spg', 0)} stl, "
            f"A:TO {g.get('ast_to', 0)}, PER {g.get('per', 0)}")
    return "\n".join(out)


if __name__ == "__main__":
    import sys
    if "--refresh" in sys.argv:
        force = "--force" in sys.argv
        print(json.dumps(refresh_all(only_stale=not force), indent=2))
    else:
        print(summarize_for_chat())
