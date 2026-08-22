"""
Tests for the grade engine (school_grades.py).

Run directly:  python3 test_school_grades.py
No network, no Supabase — a temp vault with a hand-written rubric.

Covers:
  1. rubric loading + component matching from free-text labels
  2. item_weight — the satisficing number, and that drops shrink it
  3. project() — partial components are LIVE weight, never implicit zeros
  4. needs-to-hit-a-letter math, incl. locked and unreachable letters
  5. log_grade — writes, validates, refuses off-node, matches components
  6. drop handling once more items exist than will be counted
"""

import csv
import os
import shutil
import sys
import tempfile

import school_data
import school_grades

PASS, FAIL = "PASS    ", "**FAIL**"
_results = []
_tmpdirs = []


def check(label, cond):
    _results.append(bool(cond))
    print(f"{PASS if cond else FAIL} {label}")


RUBRIC = [
    # course, component, weight, count, drops
    ("TEST101", "Exams", 60, 4, 0),
    ("TEST101", "Homework", 20, 20, 5),
    ("TEST101", "Participation", 20, 0, 0),   # count 0 = not item-based
]


def _vault(scores=()):
    tmp = tempfile.mkdtemp()
    _tmpdirs.append(tmp)
    school = os.path.join(tmp, "School")
    os.makedirs(school)
    with open(os.path.join(school, "grading.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["course", "component", "weight_pct", "count", "drops", "notes"])
        for row in RUBRIC:
            w.writerow(list(row) + [""])
    with open(os.path.join(school, "courses.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["course", "code", "title", "instructor", "meets", "term",
                    "lead_target_days", "prepared_through", "syllabus_status", "notes"])
        w.writerow(["TEST101", "TEST 101", "Testing", "Prof", "MWF 9:00-9:50AM Room 1",
                    "Fall 2026", "5", "2026-09-01", "imported", ""])
    with open(os.path.join(school, "grades.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["course", "component", "item", "score", "out_of", "date", "notes"])
        for s in scores:
            w.writerow(s)
    school_data.init(tmp, "local")
    return tmp


# ============================================================
def test_rubric_and_matching():
    print("\n=== 1. rubric + free-text component matching ===")
    _vault()
    r = school_grades.rubric("TEST101")
    check("rubric loads every component", len(r.get("TEST101") or []) == 3)
    comps = r["TEST101"]
    check("exact name matches",
          school_grades._match_component("Exams", comps)["component"] == "Exams")
    check("case/partial label matches ('homework')",
          school_grades._match_component("homework", comps)["component"] == "Homework")
    check("an item label resolves to its component ('Exam 2' -> Exams)",
          school_grades._match_component("Exam 2", comps)["component"] == "Exams")
    check("nonsense matches nothing rather than guessing wrong",
          school_grades._match_component("", comps) is None)


def test_item_weight():
    print("\n=== 2. item_weight — the satisficing number ===")
    _vault()
    check("an exam is weight/count with no drops",
          abs(school_grades.item_weight("TEST101", "Exams") - 15.0) < 0.01)
    # 20% over 20 items with 5 dropped => 15 counted => 1.333% each
    w = school_grades.item_weight("TEST101", "Homework")
    check("drops SHRINK the counted pool, raising per-item weight",
          abs(w - (20 / 15)) < 0.01)
    check("a component with no item count doesn't divide by zero",
          school_grades.item_weight("TEST101", "Participation") == 20.0)
    check("an unknown component is 0, not a crash",
          school_grades.item_weight("TEST101", "Nonexistent") == 0.0)
    out = school_grades.worth_it_tool("TEST101", "Homework")
    check("worth_it names the free drops", "lowest 5" in out)
    check("worth_it calls a big component worth protecting",
          "real chunk" in school_grades.worth_it_tool("TEST101", "Exams"))


def test_partial_components_are_live_weight():
    print("\n=== 3. unreturned work is LIVE weight, never an implicit zero ===")
    _vault([("TEST101", "Exams", "Exam 1", "80", "100", "2026-09-10", "")])
    p = school_grades.project("TEST101")
    check("only the returned share of a component counts as decided",
          abs(p["graded_weight"] - 15.0) < 0.01)
    check("current standing reflects only what's back", p["current_pct"] == 80.0)
    check("the rest stays live", abs(p["remaining_weight"] - 85.0) < 0.01)
    # The failure this pins: treating the other 3 exams as zeros would put him
    # at 20% overall and trigger a panic that isn't real.
    check("a student one exam in is not reported as failing",
          p["current_pct"] > 50)


def test_needs_math():
    print("\n=== 4. what the remaining work has to average ===")
    _vault([("TEST101", "Exams", "Exam 1", "100", "100", "2026-09-10", "")])
    p = school_grades.project("TEST101")
    needs = {n["letter"]: n["need_pct"] for n in p["needs"]}
    # 15 banked points of 15 graded; 85 live. For A (93): (93-15)/85 = 91.8%
    check("A need is computed off banked points and live weight",
          abs(needs.get("A", 0) - 91.8) < 0.2)
    check("a lower letter needs less", needs["A-"] < needs["A"])

    # Bomb everything returnable: high letters become unreachable and drop off.
    _vault([("TEST101", "Exams", "Exam 1", "0", "100", "2026-09-10", ""),
            ("TEST101", "Exams", "Exam 2", "0", "100", "2026-10-10", ""),
            ("TEST101", "Exams", "Exam 3", "0", "100", "2026-11-10", "")])
    p2 = school_grades.project("TEST101")
    letters = {n["letter"] for n in p2["needs"]}
    check("mathematically unreachable letters are not offered", "A" not in letters)

    # Bank enough that a letter is locked regardless of the rest.
    _vault([("TEST101", "Exams", "Exam 1", "100", "100", "2026-09-10", ""),
            ("TEST101", "Exams", "Exam 2", "100", "100", "2026-10-10", ""),
            ("TEST101", "Exams", "Exam 3", "100", "100", "2026-11-10", ""),
            ("TEST101", "Exams", "Exam 4", "100", "100", "2026-12-10", ""),
            ("TEST101", "Participation", "part", "100", "100", "2026-12-10", "")])
    p3 = school_grades.project("TEST101")
    locked = [n for n in p3["needs"] if n["locked"]]
    check("a letter already banked is reported as locked", bool(locked))


def test_drops_applied():
    print("\n=== 5. drops only bite once the pool exceeds what's counted ===")
    # 20 HW items, 5 dropped => 15 counted. Log 16 scores: fifteen 100s and one 0.
    rows = [("TEST101", "Homework", f"HW{i}", "100", "100", "2026-09-10", "")
            for i in range(15)]
    rows.append(("TEST101", "Homework", "HW-bomb", "0", "100", "2026-09-11", ""))
    _vault(rows)
    p = school_grades.project("TEST101")
    hw = next(c for c in p["components"] if c["component"] == "Homework")
    check("the single bombed score is dropped, not averaged in", hw["pct"] == 100.0)

    # With only 3 scores logged, nothing is dropped yet (3 <= 15 counted).
    _vault([("TEST101", "Homework", "HW1", "100", "100", "2026-09-01", ""),
            ("TEST101", "Homework", "HW2", "100", "100", "2026-09-02", ""),
            ("TEST101", "Homework", "HW3", "0", "100", "2026-09-03", "")])
    p2 = school_grades.project("TEST101")
    hw2 = next(c for c in p2["components"] if c["component"] == "Homework")
    check("early in the term a bad score still counts (drops aren't spent yet)",
          abs(hw2["pct"] - 66.7) < 0.5)


def test_log_grade():
    print("\n=== 6. log_grade writes, validates, and respects the node gate ===")
    _vault()
    out = school_grades.log_grade_tool("TEST101", "Exams", 88, 100, item="Exam 1")
    check("a valid score is logged", out.startswith("Logged:"))
    check("the ledger row round-trips", len(school_grades.scores("TEST101")) == 1)
    check("the projection updates immediately",
          school_grades.project("TEST101")["current_pct"] == 88.0)

    check("a nonsense score is refused",
          "doesn't look right" in school_grades.log_grade_tool("TEST101", "Exams", 900, 100))
    check("a missing denominator is refused",
          "out of" in school_grades.log_grade_tool("TEST101", "Exams", 9, 0))
    check("an unknown course is refused",
          "No course matching" in school_grades.log_grade_tool("NOPE", "Exams", 9, 10))
    off = school_grades.log_grade_tool("TEST101", "Exams", 9, 10)
    school_data._RUNTIME = "server"
    try:
        off = school_grades.log_grade_tool("TEST101", "Exams", 9, 10)
        check("the server node refuses to write the vault", "Mac node" in off)
    finally:
        school_data._RUNTIME = "local"

    # An unmatched component still records, but says it won't count yet —
    # silently dropping a real grade would be worse than a loud mismatch.
    msg = school_grades.log_grade_tool("TEST101", "Mystery", 5, 10)
    check("an unmatched component logs but warns", "doesn't match a rubric" in msg)


def test_no_rubric_refuses_to_guess():
    print("\n=== 7. no rubric => say so, never invent a split ===")
    tmp = tempfile.mkdtemp()
    _tmpdirs.append(tmp)
    os.makedirs(os.path.join(tmp, "School"))
    with open(os.path.join(tmp, "School", "courses.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["course", "code", "title", "instructor", "meets", "term",
                    "lead_target_days", "prepared_through", "syllabus_status", "notes"])
        w.writerow(["BARE101", "BARE 101", "No rubric", "P", "TBA", "Fall 2026",
                    "3", "", "pending", ""])
    school_data.init(tmp, "local")
    p = school_grades.project("BARE101")
    check("a course with no rubric reports that, with no numbers",
          "never imported" in (p.get("error") or ""))
    check("the tool surface says it too, rather than projecting nothing",
          "grading.csv" in school_grades.grade_projection_tool("BARE101"))


# ============================================================
if __name__ == "__main__":
    try:
        test_rubric_and_matching()
        test_item_weight()
        test_partial_components_are_live_weight()
        test_needs_math()
        test_drops_applied()
        test_log_grade()
        test_no_rubric_refuses_to_guess()
    finally:
        for d in _tmpdirs:
            shutil.rmtree(d, ignore_errors=True)
    total, passed = len(_results), sum(_results)
    print("\n" + "=" * 48)
    print(f"{passed}/{total} checks passed")
    print("=" * 48)
    sys.exit(0 if passed == total else 1)
