#!/usr/bin/env python3
"""Alex's own season stat line, pulled from Case Western's athletics site.

The D1 tracker can rank him against every target program's returning guards, but
only once it knows his numbers. Typing them in by hand is the kind of chore that
gets done twice and then never again, so this reads them off the source that
updates itself: CWRU's published cumulative stats page.

He's a freshman in 2026-27, so **there is nothing to find until the season
starts in November** — and possibly not then, if he doesn't crack the box score
early. Every path here is built to return "not found yet" rather than to fail,
and `sync_me` never writes a partial or empty line over a good one.

Source: athletics.case.edu (Sidearm Sports). No JSON endpoint exists — the
`?view=json` and `/api/v2/` routes both serve HTML — so this parses the stats
table. Two things make that less fragile than it sounds:

  * Columns are resolved **by header name**, not by position. The same page
    serves an overall table and a conference table whose column counts differ
    (the conference one carries an extra AST/G), so a hardcoded index would read
    turnovers out of the steals column on one of them.
  * The header is two rows of grouped cells with colspans ("Rebounds" spanning
    OFF/DEF/TOT/AVG), so the groups are expanded and joined before matching.

Percentages arrive as `.411` and are returned on the 0-100 scale the tracker
uses everywhere else.
"""

from __future__ import annotations

import html
import json
import os
import re
import ssl
import urllib.error
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

BASE = "https://athletics.case.edu"
STATS_URL = BASE + "/sports/mens-basketball/stats/{season}"

# Sidearm 403s an unrecognised agent the same way ESPN does.
USER_AGENT = "curl/8.4.0"
FETCH_TIMEOUT = 25

# Who to look for. Matched loosely because the site prints "Hickey, Alex" while
# everything else in this repo says "Alex Hickey", and a middle initial or a
# jersey number can ride along in the same cell.
PLAYER_LAST = "hickey"
PLAYER_FIRST = "alex"


def _ssl_context() -> ssl.SSLContext:
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


_SSL_CTX = _ssl_context()


def current_season(now: datetime | None = None) -> str:
    """CWRU's season label, e.g. "2026-27".

    A basketball season spans the new year, so anything from May onward belongs
    to the season starting that calendar year."""
    n = now or datetime.now(ET)
    start = n.year if n.month >= 5 else n.year - 1
    return f"{start}-{str(start + 1)[2:]}"


def _get(url: str) -> str | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT,
                                    context=_SSL_CTX) as r:
            return r.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
            OSError) as e:
        print(f"[cwru_stats] fetch failed {url}: {e}")
        return None


def _text(cell_html: str) -> str:
    return " ".join(html.unescape(re.sub("<[^>]+>", " ", cell_html)).split())


def _cells(row_html: str) -> list[tuple[str, int]]:
    """(text, colspan) for each cell in a row."""
    out = []
    for m in re.finditer(r"<t[dh]([^>]*)>(.*?)</t[dh]>", row_html, re.S):
        span = re.search(r'colspan\s*=\s*["\']?(\d+)', m.group(1))
        out.append((_text(m.group(2)), int(span.group(1)) if span else 1))
    return out


def _columns(rows: list[str]) -> list[str]:
    """Flat column names from the two header rows.

    The rows do NOT line up one-to-one, which is the trap. The group row has 16
    cells whose colspans sum to 27; the sub row has only the 17 cells belonging
    to the groups that span more than one column. So a group with colspan n
    consumes the next n names from the sub row, and an ungrouped header
    (#, Player, GP, PF, AST, TO…) stands alone and consumes none.

    Names are qualified by their group — "rebounds tot" vs "minutes tot" —
    because TOT and AVG each appear three times and would otherwise collide."""
    if not rows:
        return []
    top = _cells(rows[0])
    sub = [t for t, _ in _cells(rows[1])] if len(rows) > 1 else []
    cols: list[str] = []
    i = 0
    for name, span in top:
        if span <= 1:
            cols.append(name.lower())
            continue
        for _ in range(span):
            leaf = sub[i] if i < len(sub) else ""
            i += 1
            cols.append(f"{name} {leaf}".strip().lower())
    return cols


def parse_stats_page(page: str) -> list[dict]:
    """Every player row from the overall (not conference) stats table."""
    best: list[dict] = []
    for table in re.findall(r"<table[^>]*>.*?</table>", page, re.S):
        if "/roster/" not in table:
            continue
        rows = re.findall(r"<tr[^>]*>(.*?)</tr>", table, re.S)
        if len(rows) < 3:
            continue
        # Header rows are the ones made of <th>; data rows follow.
        head = [r for r in rows[:2]]
        cols = _columns(head)
        if not cols:
            continue
        players = []
        for r in rows[2:]:
            cells = [t for t, _ in _cells(r)]
            if len(cells) < len(cols) - 2:
                continue
            rec = {cols[i]: cells[i] for i in range(min(len(cols), len(cells)))}
            name = clean_name(rec.get("player", ""))
            # The table ends with aggregate rows ("Total", "Opponent", and a
            # "Team TM Team" row for team rebounds) that parse like players.
            low = name.lower()
            if not name or low.startswith("team") or "total" in low \
                    or "opponent" in low:
                continue
            rec["player"] = name
            players.append(rec)
        # The overall table lists more players than the conference one; when
        # both parse, keep the fuller.
        if len(players) > len(best):
            best = players
    return best


def _num(v, pct: bool = False) -> float | None:
    """CWRU prints percentages as `.411` and blanks as `-` or empty."""
    if v is None:
        return None
    s = str(v).strip()
    if not s or s in ("-", "--", "/"):
        return None
    try:
        f = float(s)
    except ValueError:
        return None
    return round(f * 100, 1) if pct else f


def clean_name(raw: str) -> str:
    """"Edwards, Ethan 31 Edwards, Ethan" -> "Edwards, Ethan".

    Sidearm packs a desktop and a mobile rendering of the name into one cell,
    separated by the jersey number, so the raw text carries the name twice."""
    return re.split(r"\s+\d", (raw or "").strip(), maxsplit=1)[0].strip()


def _pick(rec: dict, *needles) -> str | None:
    """First column whose name contains all the given fragments."""
    for k, v in rec.items():
        if all(n in k for n in needles):
            return v
    return None


def to_line(rec: dict) -> dict:
    """Map one CWRU row onto the schema the D1 tracker compares against.

    Per-game rates are read from the site's own AVG columns where they exist and
    derived from totals otherwise, because a mid-season page sometimes carries
    totals before the averages are populated."""
    gp = _num(_pick(rec, "gp")) or 0

    def per_game(total_key_parts, avg_val=None):
        if avg_val is not None:
            n = _num(avg_val)
            if n is not None:
                return n
        t = _num(_pick(rec, *total_key_parts))
        return round(t / gp, 1) if (t is not None and gp) else None

    # Exact keys, not _pick: the conference table carries an "ast/g" column too,
    # and a substring match on "ast" would grab whichever came first.
    ast = _num(rec.get("ast"))
    tov = _num(rec.get("to"))

    line = {
        "gp": int(gp) if gp else 0,
        "gs": int(_num(_pick(rec, "gs")) or 0),
        "mpg": per_game(("minutes", "tot"), _pick(rec, "minutes", "avg")),
        "ppg": per_game(("scoring", "pts"), _pick(rec, "scoring", "avg")),
        "rpg": per_game(("rebounds", "tot"), _pick(rec, "rebounds", "avg")),
        "apg": round(ast / gp, 1) if (ast is not None and gp) else None,
        "topg": round(tov / gp, 1) if (tov is not None and gp) else None,
        "spg": round(_num(rec.get("stl")) / gp, 1)
               if (_num(rec.get("stl")) is not None and gp) else None,
        "fg_pct": _num(_pick(rec, "fg%"), pct=True),
        "ft_pct": _num(_pick(rec, "ft%"), pct=True),
    }
    # Attempts travel with the percentage so the tracker can apply its own
    # "did he actually shoot any" gate. A player who never took a three shows
    # .000 on the page, and shipping that as 0.0% would read as a cold shooter
    # rather than as no attempts.
    tpa = _num(_pick(rec, "3pta"))
    if tpa:
        line["tp_pct"] = _num(_pick(rec, "3pt%"), pct=True)
        line["tp_att"] = int(tpa)
    if ast is not None and tov:
        line["ast_to"] = round(ast / tov, 2)
    return {k: v for k, v in line.items() if v is not None}


def find_me(players: list[dict]) -> dict | None:
    for rec in players:
        n = (rec.get("player") or "").lower()
        if PLAYER_LAST in n and PLAYER_FIRST in n:
            return rec
    return None


def fetch_my_line(season: str | None = None) -> dict:
    """His line for the season, or a dict saying why there isn't one.

    Never raises and never invents numbers — 'not on the stats page yet' is the
    expected answer for most of the autumn, not an error."""
    season = season or current_season()
    url = STATS_URL.format(season=season)
    page = _get(url)
    if not page:
        return {"found": False, "reason": "stats page unavailable",
                "season": season, "url": url}
    players = parse_stats_page(page)
    if not players:
        return {"found": False, "reason": "no stats posted yet for this season",
                "season": season, "url": url, "n_players": 0}
    rec = find_me(players)
    if not rec:
        return {"found": False,
                "reason": "not on the stats page yet",
                "season": season, "url": url, "n_players": len(players),
                "roster_names": [p.get("player", "") for p in players][:30]}
    line = to_line(rec)
    if not line.get("gp"):
        return {"found": False, "reason": "listed but no games played yet",
                "season": season, "url": url}
    return {"found": True, "season": season, "url": url,
            "name": rec.get("player", ""), "line": line}


def sync_me(season: str | None = None, write: bool = True) -> dict:
    """Pull his line and store it for the tracker's comparison.

    Writes only a line with games behind it. A half-populated or empty line
    silently overwriting a good one would produce confident, wrong rankings —
    worse than showing nothing."""
    import d1_tracker

    res = fetch_my_line(season)
    if not res.get("found"):
        return res
    if write:
        d1_tracker.save_me(
            note=f"CWRU {res['season']}, {res['line'].get('gp')} games "
                 f"(auto-synced from athletics.case.edu)",
            **res["line"])
    return res


if __name__ == "__main__":
    import sys
    season = next((a for a in sys.argv[1:] if re.match(r"^\d{4}-\d{2}$", a)), None)
    out = sync_me(season, write="--dry-run" not in sys.argv)
    print(json.dumps(out, indent=2)[:2000])
