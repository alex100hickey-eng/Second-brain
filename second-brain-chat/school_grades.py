"""
school_grades.py — what grade is Alex actually running, and what does he need?

Goal C is "high grades for the least time and stress", and the lever that serves
it is not more studying: it is knowing which work MOVES the grade and which work
is rounding error. A 22-item APQ component worth 8% with the 4 lowest dropped
means one APQ is 0.44% of the semester — worth 20 minutes, never worth an hour
stolen from a 15%-weighted test. Nothing in the system could say that before.

Three files, three jobs:
  * School/grading.csv  — the RUBRIC (course, component, weight_pct, count,
    drops). Written once from the syllabi, Alex-reviewable, rarely changes.
  * School/grades.csv   — the LEDGER (one row per returned score). Appended by
    log_grade from ordinary conversation ("got 9/10 on the GDP quiz").
  * this module         — the MATH. Current standing, what's still live, what
    he needs on the rest, and which upcoming items are safe to satisfice.

Design stances carried over from school_data.py, deliberately:
  * Writes happen ONLY on the local (Mac) node — the server's vault is a
    pull-only mirror, so a write there is silently reverted by the next sync.
  * Atomic tmp+os.replace: a crash never truncates a ledger.
  * Never invent a weight. A course with no grading.csv rows reports that it
    has no rubric, rather than guessing a plausible split — a wrong weight
    produces confidently wrong advice about what to skip, which is worse than
    no projection at all.
  * Projections are stated as ranges/needs, never as a promise. The engine says
    "you need 88% on what's left for an A-", not "you will get an A-".
"""

import csv
import os
from datetime import datetime

import school_data

GRADING = "grading.csv"
GRADES = "grades.csv"

GRADING_COLUMNS = ["course", "component", "weight_pct", "count", "drops", "notes"]
GRADES_COLUMNS = ["course", "component", "item", "score", "out_of", "date", "notes"]

# CWRU letter thresholds. A- is the interesting one: it is the difference
# between "coasting is fine" and "the next exam matters".
LETTERS = [("A", 93.0), ("A-", 90.0), ("B+", 87.0), ("B", 83.0), ("B-", 80.0),
           ("C+", 77.0), ("C", 73.0)]


def _path(name):
    return os.path.join(school_data._SCHOOL_DIR or "", name)


def _num(v, default=None):
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return default


def rubric(course: str = "") -> dict:
    """{COURSE: [component dicts]} from grading.csv."""
    want = (course or "").strip().lower()
    out = {}
    for r in school_data._load(GRADING):
        code = (r.get("course") or "").strip()
        if not code or (want and want not in code.lower()):
            continue
        w = _num(r.get("weight_pct"))
        if w is None:
            continue
        out.setdefault(code, []).append({
            "component": (r.get("component") or "").strip(),
            "weight_pct": w,
            "count": int(_num(r.get("count"), 0) or 0),
            "drops": int(_num(r.get("drops"), 0) or 0),
            "notes": (r.get("notes") or "").strip(),
        })
    return out


def scores(course: str = "") -> list:
    """Logged scores from grades.csv, newest last."""
    want = (course or "").strip().lower()
    out = []
    for r in school_data._load(GRADES):
        code = (r.get("course") or "").strip()
        if not code or (want and want not in code.lower()):
            continue
        got, total = _num(r.get("score")), _num(r.get("out_of"))
        if got is None or not total:
            continue
        out.append({"course": code, "component": (r.get("component") or "").strip(),
                    "item": (r.get("item") or "").strip(), "score": got,
                    "out_of": total, "pct": 100.0 * got / total,
                    "date": (r.get("date") or "").strip()})
    return out


def _stem(word: str) -> str:
    """Crude singular form. Alex says 'exam 1' and the rubric says 'Exams 1-4';
    a matcher that can't cross that gap silently files real grades under a
    component that counts toward nothing."""
    w = word.strip().lower().strip("().,:")
    if len(w) > 3 and w.endswith("es") and not w.endswith("ses"):
        return w[:-2]
    if len(w) > 3 and w.endswith("s"):
        return w[:-1]
    return w


def _tokens(text: str) -> set:
    raw = (text or "").lower().replace("-", " ").replace("/", " ").split()
    return {_stem(t) for t in raw if t and not t.isdigit()}


def _match_component(name: str, comps: list):
    """Best rubric component for a free-text label ('quiz' -> 'Quizzes',
    'Exam 2' -> 'Exams 1-4'). Returns None when nothing plausibly matches —
    guessing wrong is worse than telling Alex the label didn't land."""
    n = (name or "").strip().lower()
    if not n:
        return None
    for c in comps:                                   # exact
        if c["component"].strip().lower() == n:
            return c
    for c in comps:                                   # containment either way
        cl = c["component"].strip().lower()
        if n in cl or cl.startswith(n):
            return c
    # Stemmed token overlap, so singular/plural and item numbers don't matter.
    want = _tokens(n)
    if not want:
        return None
    best, best_hits = None, 0
    for c in comps:
        hits = len(want & _tokens(c["component"]))
        if hits > best_hits:
            best, best_hits = c, hits
    return best


def item_weight(course: str, component: str) -> float:
    """What ONE item in a component is worth, as a percent of the final grade.

    This is the satisficing number. Drops make it smaller than the naive
    weight/count: an 8% component of 22 items with 4 dropped spreads over 18
    counted items, so each is 0.44% — and the four you bomb cost nothing."""
    comps = rubric(course).get(_code(course) or course.upper(), [])
    c = _match_component(component, comps)
    if not c:
        return 0.0
    effective = max(1, c["count"] - c["drops"]) if c["count"] else 1
    return c["weight_pct"] / effective


def _code(course: str):
    code, err = school_data._canonical_course(course)
    return None if err else code


def project(course: str) -> dict:
    """Current standing and what the remaining work has to average.

    Only components with at least one logged score count toward "earned so far"
    — a component nothing has been returned in yet is live weight, not a zero.
    Treating unreturned work as zeros is the classic way these engines panic
    students in week 3."""
    code = _code(course)
    if not code:
        return {"error": school_data._canonical_course(course)[1]}
    comps = rubric(code).get(code, [])
    if not comps:
        return {"error": f"No rubric rows for {code} in School/{GRADING} — "
                         "its weights were never imported, so I won't guess them."}
    logged = [s for s in scores(code)]
    by_comp = {}
    for s in logged:
        c = _match_component(s["component"], comps)
        key = c["component"] if c else s["component"]
        by_comp.setdefault(key, []).append(s)

    graded_weight = 0.0     # weight whose outcome is already decided
    earned_points = 0.0     # points banked out of the total 100
    lines = []
    for c in comps:
        got = by_comp.get(c["component"]) or []
        if not got:
            lines.append({"component": c["component"], "weight_pct": c["weight_pct"],
                          "status": "nothing returned yet", "pct": None,
                          "n": 0, "of": c["count"]})
            continue
        pcts = sorted(s["pct"] for s in got)
        # Drops only bite once more items exist than will be counted.
        keep = pcts
        if c["drops"] and c["count"] and len(pcts) > (c["count"] - c["drops"]):
            keep = pcts[c["drops"]:]
        avg = sum(keep) / len(keep)
        # Only the fraction of the component actually returned is decided.
        share = (len(got) / c["count"]) if c["count"] else 1.0
        share = min(1.0, share)
        graded_weight += c["weight_pct"] * share
        earned_points += c["weight_pct"] * share * (avg / 100.0)
        lines.append({"component": c["component"], "weight_pct": c["weight_pct"],
                      "status": "in progress" if share < 1 else "complete",
                      "pct": round(avg, 1), "n": len(got), "of": c["count"]})

    remaining = max(0.0, 100.0 - graded_weight)
    current = (100.0 * earned_points / graded_weight) if graded_weight else None
    needs = []
    for letter, cut in LETTERS:
        if remaining <= 0:
            break
        need = (cut - earned_points) / remaining * 100.0
        if need <= 100.5:                 # anything above 100 is unreachable
            needs.append({"letter": letter, "cut": cut, "need_pct": round(need, 1),
                          "locked": need <= 0})
    return {"course": code, "components": lines,
            "graded_weight": round(graded_weight, 1),
            "remaining_weight": round(remaining, 1),
            "earned_points": round(earned_points, 2),
            "current_pct": round(current, 1) if current is not None else None,
            "needs": needs, "n_scores": len(logged)}


# ---------------------------------------------------------------------------
# tools
# ---------------------------------------------------------------------------

def log_grade_tool(course: str, component: str, score, out_of,
                   item: str = "", when: str = "", notes: str = "") -> str:
    if school_data._RUNTIME != "local":
        return ("Grade logging lives on the Mac node — this node's vault is a "
                "pull mirror, so a write here would be reverted by the next "
                "sync. Nothing was logged.")
    code, err = school_data._canonical_course(course)
    if err:
        return err
    got, total = _num(score), _num(out_of)
    if got is None or total is None or total <= 0:
        return f"Need a score and what it was out of, got {score!r}/{out_of!r}."
    if got < 0 or got > total * 1.5:      # allow a little extra credit, not a typo
        return f"{got}/{total} doesn't look right — re-say it and I'll log it."
    if not school_data._SCHOOL_DIR or not os.path.isdir(school_data._SCHOOL_DIR):
        return (f"School folder not found at {school_data._SCHOOL_DIR or '(uninit)'} "
                "— nothing was logged.")
    d = school_data._parse_date(when) if (when or "").strip() else \
        datetime.now(school_data.LOCAL_TZ).date()
    if d is None:
        return f"Couldn't read '{when}' as a date — use YYYY-MM-DD, or omit for today."

    comps = rubric(code).get(code, [])
    matched = _match_component(component, comps)
    comp_name = matched["component"] if matched else (component or "").strip()

    rows = school_data._load(GRADES)
    rows.append({"course": code, "component": comp_name,
                 "item": (item or "").strip(), "score": f"{got:g}",
                 "out_of": f"{total:g}", "date": d.isoformat(),
                 "notes": (notes or "").strip()})
    path, tmp = _path(GRADES), _path(GRADES) + ".tmp"
    try:
        with open(tmp, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=GRADES_COLUMNS, extrasaction="ignore")
            w.writeheader()
            for r in rows:
                w.writerow({c: (r.get(c) or "") for c in GRADES_COLUMNS})
        os.replace(tmp, path)
    except OSError as e:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return f"couldn't write {GRADES}: {e} — nothing was logged."

    pct = 100.0 * got / total
    msg = [f"Logged: {code} {comp_name}{f' — {item}' if item else ''} "
           f"{got:g}/{total:g} ({pct:.0f}%)."]
    if not matched and comps:
        msg.append(f"Heads up: '{component}' doesn't match a rubric component "
                   f"({', '.join(c['component'] for c in comps[:4])}) — it's "
                   "recorded, but it won't count toward the projection until the "
                   "name lines up.")
    p = project(code)
    if not p.get("error") and p.get("current_pct") is not None:
        msg.append(f"{code} now sits at {p['current_pct']:.0f}% on "
                   f"{p['graded_weight']:.0f}% of the grade.")
    return " ".join(msg)


def _fmt_projection(p: dict) -> str:
    if p.get("error"):
        return p["error"]
    out = [f"{p['course']} — {p['graded_weight']:.0f}% of the grade decided, "
           f"{p['remaining_weight']:.0f}% still live"]
    if p["current_pct"] is not None:
        out[0] += f"; running {p['current_pct']:.0f}% on what's returned."
    else:
        out[0] += "; nothing returned yet."
    for c in p["components"]:
        if c["pct"] is None:
            out.append(f"  {c['component']} ({c['weight_pct']:g}%) — nothing back yet")
        else:
            of = f"/{c['of']}" if c["of"] else ""
            out.append(f"  {c['component']} ({c['weight_pct']:g}%) — "
                       f"{c['pct']:.0f}% over {c['n']}{of}")
    live = [n for n in p["needs"] if not n["locked"]]
    locked = [n for n in p["needs"] if n["locked"]]
    if locked:
        out.append(f"  {locked[0]['letter']} is already locked in by the math.")
    for n in live[:3]:
        out.append(f"  {n['letter']} ({n['cut']:g}%) needs {n['need_pct']:.0f}% "
                   f"average across the remaining {p['remaining_weight']:.0f}%.")
    if not p["needs"]:
        out.append("  Everything is graded — no runway left to move it.")
    return "\n".join(out)


def grade_projection_tool(course: str = "") -> str:
    codes = [_code(course)] if (course or "").strip() else sorted(rubric().keys())
    codes = [c for c in codes if c]
    if not codes:
        return (f"No rubric rows in School/{GRADING} yet — import the grading "
                "weights first and I'll project from real numbers.")
    blocks = [_fmt_projection(project(c)) for c in codes]
    head = (f"GRADE PROJECTION — {datetime.now(school_data.LOCAL_TZ).date().isoformat()}"
            "\n(a need above ~95% means the letter is realistically gone; "
            "below 0 means it's locked)")
    return head + "\n\n" + "\n\n".join(blocks)


def worth_it_tool(course: str, component: str) -> str:
    """How much one item actually moves the grade — the satisficing answer."""
    code = _code(course)
    if not code:
        return school_data._canonical_course(course)[1]
    comps = rubric(code).get(code, [])
    if not comps:
        return f"No rubric for {code} — can't weigh it."
    c = _match_component(component, comps)
    if not c:
        return (f"'{component}' doesn't match a {code} component. Known: "
                + ", ".join(x["component"] for x in comps))
    w = item_weight(code, c["component"])
    effective = max(1, c["count"] - c["drops"]) if c["count"] else 1
    out = [f"{code} — one {c['component']} item is worth about "
           f"{w:.2f}% of your final grade "
           f"({c['weight_pct']:g}% spread over {effective} counted item"
           f"{'s' if effective != 1 else ''})."]
    if c["drops"]:
        out.append(f"The lowest {c['drops']} get dropped — so the first "
                   f"{c['drops']} you skip or bomb cost you nothing at all.")
    else:
        out.append("No drops in this component — every one counts.")
    if w >= 5:
        out.append("That's a real chunk. Protect the time for it.")
    elif w < 1:
        out.append("That's rounding error. Do it fast and spend the hour on "
                   "something weighted.")
    if c["notes"]:
        out.append(c["notes"])
    return " ".join(out)


TOOL_SCHEMAS = [
    {"name": "log_grade",
     "description": ("Record a returned score in School/grades.csv — the ledger the "
                     "grade projection reads. Call it whenever Alex mentions getting "
                     "a grade back ('got a 9/10 on the GDP quiz', '88 on exam 1'), "
                     "including in the nightly check-in. Infer the course and the "
                     "rubric component from context. Mac node only."),
     "input_schema": {"type": "object",
                      "required": ["course", "component", "score", "out_of"],
                      "properties": {
         "course": {"type": "string", "description": "Course code, e.g. ACCT100."},
         "component": {"type": "string",
                       "description": "Rubric component it belongs to (Exams, Homework, "
                                      "Quizzes, APQs…) — match grading.csv wording."},
         "score": {"type": "number"},
         "out_of": {"type": "number"},
         "item": {"type": "string", "description": "Which one, e.g. 'Exam 1', 'HW 3'."},
         "when": {"type": "string", "description": "YYYY-MM-DD; omit for today."},
         "notes": {"type": "string"}}}},
    {"name": "grade_projection",
     "description": ("Where each course actually stands and what the remaining work "
                     "has to average to land an A / A- / B+. Use when Alex asks about "
                     "grades, 'am I okay in X', what he needs on a final, or how much "
                     "an upcoming exam matters. Omit course for all courses."),
     "input_schema": {"type": "object", "properties": {
         "course": {"type": "string"}}}},
    {"name": "grade_worth_it",
     "description": ("What ONE item of a component is worth as a percent of the final "
                     "grade, including whether drops already cover it. Use when Alex "
                     "is deciding whether something is worth his time tonight — the "
                     "least-time-for-high-grades lever."),
     "input_schema": {"type": "object", "required": ["course", "component"],
                      "properties": {
         "course": {"type": "string"},
         "component": {"type": "string"}}}},
]

TOOL_STATUS_LABELS = {
    "log_grade": "Logging that grade…",
    "grade_projection": "Running your grade projection…",
    "grade_worth_it": "Weighing what that's worth…",
}


TOOL_NAMES = {t["name"] for t in TOOL_SCHEMAS}


def handle_tool_call(tool_name: str, tool_input: dict) -> str:
    if tool_name == "log_grade":
        return log_grade_tool(
            tool_input.get("course", ""), tool_input.get("component", ""),
            tool_input.get("score"), tool_input.get("out_of"),
            tool_input.get("item", ""), tool_input.get("when", ""),
            tool_input.get("notes", ""))
    if tool_name == "grade_projection":
        return grade_projection_tool(tool_input.get("course", ""))
    if tool_name == "grade_worth_it":
        return worth_it_tool(tool_input.get("course", ""),
                             tool_input.get("component", ""))
    return f"unknown tool {tool_name}"
