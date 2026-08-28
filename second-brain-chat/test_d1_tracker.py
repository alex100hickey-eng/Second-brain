"""
test_d1_tracker.py — exercises the D1 transfer tracker's derivation and scoring.

No network: every test drives the pure functions off synthetic ESPN-shaped
payloads, and the storage tests point DB_PATH at a temp file. That matters more
here than in most suites — the whole model rests on two normalisations that are
easy to break and invisible when they break:

  * `_present_at_arrival`, which decides who counts as a blocker, and which is
    only correct once class year is read *relative to the roster's own season*;
  * `_completed_season`, which stops a preseason roster from asking for a
    season with no games in it and rendering every guard as a zero.

Both are pinned below with the real-world cases that motivated them.

Run:  python3 test_d1_tracker.py
"""

import os
import sys
import tempfile
from datetime import datetime

import d1_tracker as d1

_results = []
_tmpfiles = []


def check(label, cond):
    _results.append(bool(cond))
    print(("  ok " if cond else "  FAIL ") + label)


def athlete(name, pos="G", years=2, height=74, jersey="1", aid="1"):
    return {
        "id": aid, "fullName": name, "jersey": jersey, "height": height,
        "displayHeight": "6' 2\"", "displayWeight": "180 lbs",
        "position": {"abbreviation": pos},
        "experience": {"years": years,
                       "displayValue": {1: "Freshman", 2: "Sophomore",
                                        3: "Junior", 4: "Senior"}[years],
                       "abbreviation": {1: "FR", 2: "SO", 3: "JR",
                                        4: "SR"}[years]},
    }


def stats(minutes=600, gp=30, ast=100, tov=50, pts=400, mpg=None, **kw):
    d = {
        "minutes": minutes, "gamesPlayed": gp, "gamesStarted": gp,
        "assists": ast, "turnovers": tov, "points": pts,
        "avgMinutes": mpg if mpg is not None else (minutes / gp if gp else 0),
        "avgPoints": pts / gp if gp else 0,
        "avgAssists": ast / gp if gp else 0,
        "avgTurnovers": tov / gp if gp else 0,
        "fieldGoalPct": 44.0, "freeThrowPct": 78.0,
        "threePointFieldGoalsMade": 40, "threePointFieldGoalsAttempted": 100,
        "PER": 15.0,
    }
    d.update(kw)
    return d


# ---------------------------------------------------------------- eligibility

def test_present_at_arrival():
    """The blocker/departure split, normalised against the roster's season."""
    print("\n[present_at_arrival]")
    # Roster is the upcoming season (2026-27); Alex arrives 2027-28, so anyone
    # with at least one season left after 2026-27 is still there.
    check("FR on a 2026-27 roster is still there in 2027-28",
          d1._present_at_arrival(1, 2027) is True)
    check("JR on a 2026-27 roster is still there in 2027-28",
          d1._present_at_arrival(3, 2027) is True)
    check("SR on a 2026-27 roster is gone by 2027-28",
          d1._present_at_arrival(4, 2027) is False)

    # Same player class, roster a year stale — the answer must shift by one.
    # This is the Le Moyne / Cornell case: their feed still served 2025-26 in
    # August 2026 while Syracuse served 2026-27.
    check("SO on a 2025-26 roster is still there in 2027-28",
          d1._present_at_arrival(2, 2026) is True)
    check("JR on a 2025-26 roster is GONE by 2027-28 (stale-roster shift)",
          d1._present_at_arrival(3, 2026) is False)
    check("unknown class yields None, not a guess",
          d1._present_at_arrival(None, 2027) is None)
    check("unknown roster season yields None",
          d1._present_at_arrival(2, None) is None)


def test_roster_currency():
    print("\n[roster currency]")
    aug = datetime(2026, 8, 28, tzinfo=d1.ET)
    jan = datetime(2027, 1, 15, tzinfo=d1.ET)
    check("upcoming season in Aug 2026 is 2027", d1.upcoming_season(aug) == 2027)
    check("upcoming season in Jan 2027 is still 2027",
          d1.upcoming_season(jan) == 2027)
    check("a 2026-27 roster is current in Aug 2026",
          d1.roster_is_current(2027, aug) is True)
    check("a 2025-26 roster is stale in Aug 2026",
          d1.roster_is_current(2026, aug) is False)
    check("a missing roster season counts as stale",
          d1.roster_is_current(None, aug) is False)


def test_completed_season():
    """Preseason must not ask for a season that hasn't been played."""
    print("\n[completed season]")
    # Syracuse served roster season 2027 on 2026-08-28. Asking 2027 for
    # statistics returns empty lines for every player, which would render as a
    # roster of guards who all average zero.
    got = d1._completed_season(2027)
    check("a preseason roster falls back to a played season", got <= 2027)
    check("a played roster season is used as-is", d1._completed_season(2026) == 2026)
    check("no roster season still returns a usable year",
          isinstance(d1._completed_season(None), int))


# ------------------------------------------------------------- classification

def test_classify_guard():
    print("\n[guard classification]")
    lead = d1.classify_guard(stats(minutes=900, gp=30, ast=180, tov=90), 73)
    check("high assist rate reads as a lead guard", lead[0] == "Lead guard (PG)")
    check("lead-guard label shows the numbers it was read from",
          "ast/40" in lead[1] and lead[2] == "full")

    off = d1.classify_guard(stats(minutes=900, gp=30, ast=30, tov=40), 78)
    check("low assist rate reads as an off guard", off[0] == "Off guard (SG)")

    combo = d1.classify_guard(stats(minutes=900, gp=30, ast=90, tov=60), 75)
    check("middling assist rate reads as a combo guard",
          combo[0] == "Combo guard")

    # The Ametri Moss case (Le Moyne 2025-26): 2 games, 55 minutes, but he
    # started both at 27.5 mpg. A minutes-only gate buried him under the same
    # "unproven" label as a 3-mpg walk-on.
    moss = d1.classify_guard(
        stats(minutes=55, gp=2, ast=9, tov=2, pts=15, mpg=27.5), 74)
    check("a starter with a short season is still classified",
          moss[0] == "Lead guard (PG)")
    check("...and the thin sample is disclosed, not hidden",
          moss[2] == "small" and "gp only" in moss[1])

    bench = d1.classify_guard(stats(minutes=9, gp=3, ast=0, tov=1, mpg=3.0), 74)
    check("a genuine deep-bench line is named as such",
          bench[0] == "Guard (deep bench)" and bench[2] == "none")

    none = d1.classify_guard(stats(minutes=0, gp=0, ast=0, tov=0), 74)
    check("no games logged is distinct from no minutes",
          none[0] == "Guard (no data)")

    zero_tov = d1.classify_guard(
        stats(minutes=600, gp=25, ast=100, tov=0), 73)
    check("zero turnovers doesn't divide by zero", zero_tov[0].startswith("Lead"))


def test_build_player():
    print("\n[player row]")
    p = d1.build_player(athlete("Test Guard", years=4),
                        stats(minutes=900, gp=30, ast=150, tov=75, pts=500), 2027)
    for f in ("ppg", "apg", "topg", "fg_pct", "tp_pct", "ft_pct", "spg", "bpg",
              "per", "ast_to", "ast40", "mpg", "gp", "gs", "plus_minus"):
        check(f"row carries {f}", f in p)
    check("senior on a current roster is marked departing",
          p["present_at_arrival"] is False)
    check("3P% is derived when ESPN omits the pct field", p["tp_pct"] == 40.0)
    check("A:TO computed", p["ast_to"] == 2.0)

    # A stat feed that returns nothing must not raise or fabricate.
    empty = d1.build_player(athlete("No Stats", years=1), {}, 2027)
    check("a player with no stat line still builds", empty["name"] == "No Stats")
    check("...with has_stats False", empty["has_stats"] is False)


# -------------------------------------------------------------------- scoring

def test_score_school():
    print("\n[scoring]")
    def g(name, mpg, ppg, apg, present, role="Combo guard"):
        return {"name": name, "mpg": mpg, "ppg": ppg, "apg": apg,
                "present_at_arrival": present, "role": role}

    wide_open = [g("SeniorStar", 32, 18, 5, False, "Lead guard (PG)"),
                 g("SeniorTwo", 28, 14, 3, False),
                 g("BenchFrosh", 4, 1, 0, True)]
    locked = [g("StarPG", 33, 17, 6, True, "Lead guard (PG)"),
              g("StarSG", 30, 15, 2, True),
              g("ThirdGuard", 22, 9, 3, True),
              g("FourthGuard", 16, 6, 2, True)]

    s_open = d1.score_school(wide_open)
    s_lock = d1.score_school(locked)
    check("an emptying room scores lower than a locked one",
          s_open["score"] < s_lock["score"])
    check("departing guards are counted as vacated, not as blockers",
          s_open["n_returning"] == 1 and s_open["n_departing"] == 2)
    check("vacated share reflects minutes leaving",
          s_open["vacated_share"] > 90)
    check("proven returners counted at the 15-mpg line",
          s_lock["n_proven_returning"] == 4)
    check("lead guards tracked separately in and out",
          s_open["n_lead_departing"] == 1 and s_lock["n_lead_returning"] == 1)
    check("score stays inside 0-100",
          0 <= s_open["score"] <= 100 and 0 <= s_lock["score"] <= 100)
    check("an empty guard list doesn't raise",
          d1.score_school([])["score"] == 0.0)

    # A star returner must outweigh several replaceable bodies, or the ranking
    # would tell Alex a room with an all-conference PG is the easy one.
    star = [g("Star", 34, 20, 6, True, "Lead guard (PG)")]
    crowd = [g(f"Sub{i}", 12, 3, 1, True) for i in range(5)]
    check("one entrenched star outscores five low-minute bodies",
          d1.score_school(star)["score"] > d1.score_school(crowd)["score"])


def test_read_school():
    print("\n[plain-language read]")
    sc = {"n_proven_returning": 0, "n_departing": 3, "n_returning": 2,
          "n_lead_returning": 0, "n_lead_departing": 1, "vacated_share": 88.0}
    guards = [{"name": "X", "mpg": 5, "ppg": 2, "class_abbr": "FR",
               "present_at_arrival": True, "role": "Combo guard"}]
    txt = d1.read_school(guards, sc, roster_current=True)
    check("a wide-open room is described as wide open", "Wide open" in txt)
    stale_txt = d1.read_school(guards, sc, roster_current=False)
    check("a stale roster leads with that caveat",
          stale_txt.startswith("Last season's roster"))
    check("no guards yields an honest empty read",
          "roster not posted" in d1.read_school([], sc, True))


# -------------------------------------------------------------------- storage

def test_storage_and_deck():
    print("\n[storage + read model]")
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    _tmpfiles.append(path)
    orig = d1.DB_PATH
    d1.DB_PATH = path
    try:
        d1.init_db()
        payload = {
            "key": "bu", "name": "Boston University", "short": "BU",
            "staff": "https://example.invalid", "guards": [],
            "roster_season": 2027, "roster_current": True,
            "score": {"score": 20.0, "n_returning": 3, "n_departing": 2,
                      "vacated_share": 40.0},
        }
        d1._save("bu", payload, ok=True)
        got = d1.load_snapshot("bu")
        check("a snapshot round-trips", got and got["short"] == "BU")
        check("fetched_at is stamped", bool(got.get("fetched_at")))
        check("a fresh snapshot is not stale", d1.is_stale("bu") is False)
        check("an unfetched school is stale", d1.is_stale("yale") is True)
        check("history row written", len(d1.score_trend("bu")) == 1)

        # Re-saving the same day must update, not accumulate — otherwise a
        # 6-hourly in-season refresh writes four rows a day per school.
        d1._save("bu", payload, ok=True)
        check("same-day history stays one row", len(d1.score_trend("bu")) == 1)

        deck = d1.deck_data()
        check("deck lists every target school",
              len(deck["schools"]) == len(d1.SCHOOLS))
        check("scored schools are ranked", deck["n_ranked"] == 1)
        check("unfetched schools are marked pending, not dropped",
              deck["n_pending"] == len(d1.SCHOOLS) - 1)
        check("arrival season is exposed", deck["arrival_season"] == "2027-28")

        hud = d1.hud_summary()
        check("hud summary names the most open room",
              hud["n"] == 1 and hud["top"][0]["short"] == "BU")

        txt = d1.summarize_for_chat()
        check("chat summary renders", "BU" in txt)
        check("chat summary names an unknown school rather than guessing",
              "No target school matching" in d1.summarize_for_chat("Duke"))
        check("chat summary finds a school by short code",
              "Boston University" in d1.summarize_for_chat("BU"))

        # A failed fetch must not be served as if it were real.
        d1._save("yale", {"key": "yale", "short": "YALE", "guards": [],
                          "score": None}, ok=False)
        check("a failed snapshot stays stale so it gets retried",
              d1.is_stale("yale") is True)
    finally:
        d1.DB_PATH = orig


def test_target_list():
    print("\n[target list]")
    check("all 13 target schools are configured", len(d1.SCHOOLS) == 13)
    keys = [s["key"] for s in d1.SCHOOLS]
    check("keys are unique", len(set(keys)) == len(keys))
    check("every school has an ESPN id and a staff directory",
          all(s.get("espn") and s.get("staff") for s in d1.SCHOOLS))
    # SHU is Sacred Heart, not Seton Hall — ESPN's own abbreviation for Sacred
    # Heart is literally "SHU", and it fits the Fairfield/Yale/Le Moyne cluster.
    shu = d1.SCHOOL_BY_KEY.get("sacredheart")
    check("SHU resolves to Sacred Heart (espn 2529)",
          shu and shu["espn"] == "2529")
    check("MAAC alias applied (ESPN calls it 'Metro')",
          d1.CONF_ALIAS.get("Metro") == "MAAC")


def test_failsoft():
    print("\n[fail-soft]")
    check("a 404 returns None rather than raising",
          d1._get("https://sports.core.api.espn.com/v2/sports/basketball/"
                  "leagues/mens-college-basketball/seasons/2026/types/2/"
                  "athletes/000000000/statistics", tries=1,
                  quiet_404=True) is None)
    check("_f coerces junk to 0.0", d1._f("nope") == 0.0 and d1._f(None) == 0.0)
    check("_f passes numbers through", d1._f("12.5") == 12.5)


if __name__ == "__main__":
    print("=" * 48)
    print("d1_tracker tests")
    print("=" * 48)
    try:
        test_present_at_arrival()
        test_roster_currency()
        test_completed_season()
        test_classify_guard()
        test_build_player()
        test_score_school()
        test_read_school()
        test_storage_and_deck()
        test_target_list()
        test_failsoft()
    finally:
        for p in _tmpfiles:
            try:
                os.unlink(p)
            except OSError:
                pass
    total, passed = len(_results), sum(_results)
    print("\n" + "=" * 48)
    print(f"{passed}/{total} checks passed")
    print("=" * 48)
    sys.exit(0 if passed == total else 1)
