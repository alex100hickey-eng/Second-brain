"""
school_data.py — CLARVIS's read-only window into the School system.

Why this exists: CLARVIS kept telling Alex "I have no connector to your
class-times portal — paste me your schedule" while every fact it needed was
already sitting in the git-synced vault (School/*.csv). courses.csv carries the
full Fall 2026 course list WITH meeting times (filled 2026-08-16/18),
curriculum.csv the week-by-week syllabus timeline (ECON103 imported from the
official syllabus 2026-08-18, others land as professors publish), and
assignments.csv every Canvas deadline (auto-synced every 30 minutes by
canvas_sync.py). The limit was wiring, not data — these two tools close it.

Read-only on purpose: school truth is written by canvas_sync, syllabus imports,
and Alex; CLARVIS reads it here and puts blocks on the week with the EXISTING
training-schedule tools (batch_edit_schedule, after one yes). No new write path.

Vault path: init(vault_path) from app.py — VAULT_PATH resolves to the iCloud
vault on the Mac node and /data/vault on the server, so the same code reads
fresh CSVs on both nodes (worst-case staleness ≈ one 10-min sync cycle each way).

get_school_brief shells out to scripts/school_status.py (repo root) rather than
reimplementing it: that script is the single source of truth for pace/review
math, it already honors a SCHOOL_DIR env override, and its output is already
non-TTY-safe (colors off when piped).
"""

import csv
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# Alex's day, not the container's. This module runs on the Mac (ET) AND in the
# server container (UTC) — a naive date.today() there flips to tomorrow at 8pm ET,
# which silently drops tonight's prep topic out of the "up next" window. Same
# constant and same reason as app.py / training_sync.py.
LOCAL_TZ = ZoneInfo("America/New_York")

_SCHOOL_DIR = None  # set by init()

# scripts/school_status.py lives at the second-brain REPO ROOT, one level above
# this module's directory — true both locally and in the deployed container
# (Coolify base directory is the repo root).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_STATUS_SCRIPT = os.path.join(_REPO_ROOT, "scripts", "school_status.py")


def init(vault_path):
    global _SCHOOL_DIR
    _SCHOOL_DIR = os.path.join(vault_path, "School")


# ---------------------------------------------------------------------------
# courses.csv "meets" parser
# ---------------------------------------------------------------------------
# Formats actually present (this module wrote none of them — fail soft and show
# the raw string whenever a component doesn't parse):
#   "TuTh 10:00-11:15AM Peter B Lewis 201"
#   "MWF 9:20-10:10AM Olin 305"
#   "MW 12:35-1:50PM Sears 333 + Lab W 6:30-8:30PM Olin 304"
#   "TuTh 11:30AM-12:45PM Peter B Lewis 201"
#   "TBA"

_DAY_NAMES = {"Su": "Sun", "M": "Mon", "Tu": "Tue", "W": "Wed",
              "Th": "Thu", "F": "Fri", "Sa": "Sat"}

_COMPONENT_RE = re.compile(
    r"^(?:(?P<label>Lab|Lecture|Seminar|Recitation|Studio)\s+)?"
    r"(?P<days>(?:Su|Tu|Th|Sa|M|W|F)+)\s+"
    r"(?P<start>\d{1,2}(?::\d{2})?(?:AM|PM)?)-(?P<end>\d{1,2}(?::\d{2})?(?:AM|PM)?)"
    r"\s*(?P<loc>.*)$",
    re.IGNORECASE,
)


def _split_days(days: str):
    """'MWF' -> ['Mon','Wed','Fri']; two-letter tokens (Tu/Th/Su/Sa) first."""
    out, i = [], 0
    while i < len(days):
        two = days[i:i + 2]
        if two in _DAY_NAMES:
            out.append(_DAY_NAMES[two])
            i += 2
        elif days[i] in _DAY_NAMES:
            out.append(_DAY_NAMES[days[i]])
            i += 1
        else:
            return None  # unknown token — caller falls back to the raw string
    return out


def _norm_time(t: str, meridiem: str) -> str:
    """'12:35'+'PM' -> '12:35 PM'; '10'+'AM' -> '10:00 AM'."""
    t = t.strip().upper()
    m = re.match(r"^(\d{1,2})(?::(\d{2}))?(AM|PM)?$", t)
    if not m:
        return t
    hh, mm, ap = m.group(1), m.group(2) or "00", m.group(3) or meridiem
    return f"{hh}:{mm} {ap}"


# date.weekday() numbering, so weekday math needs no calendar module.
_WD_NUM = {"Mon": 0, "Tue": 1, "Wed": 2, "Thu": 3, "Fri": 4, "Sat": 5, "Sun": 6}


def meets_components(meets: str):
    """Structured view of a courses.csv meets string.

    Returns None for TBA/empty (nothing schedulable), else one dict per
    ' + '-joined component: {raw, label, days, weekdays, start, end, loc}.
    An unparseable component keeps its raw string with every parsed field None
    — same raw-beats-wrong stance parse_meets has always taken. A start time
    with no AM/PM inherits the end's ('12:35-1:50PM' means both PM in every
    real row — registrar convention).
    """
    meets = (meets or "").strip()
    if not meets or meets.upper() == "TBA":
        return None
    comps = []
    for raw in meets.split(" + "):
        raw = raw.strip()
        m = _COMPONENT_RE.match(raw)
        days = _split_days(m.group("days")) if m else None
        if not m or not days:
            comps.append({"raw": raw, "label": None, "days": None,
                          "weekdays": None, "start": None, "end": None,
                          "loc": None})
            continue
        end_ap = re.search(r"(AM|PM)", m.group("end"), re.IGNORECASE)
        meridiem = end_ap.group(1).upper() if end_ap else ""
        comps.append({
            "raw": raw,
            "label": m.group("label").title() if m.group("label") else None,
            "days": days,
            "weekdays": [_WD_NUM[d] for d in days],
            "start": _norm_time(m.group("start"), meridiem),
            "end": _norm_time(m.group("end"), meridiem),
            "loc": m.group("loc").strip() or None,
        })
    return comps


def parse_meets(meets: str):
    """One human line per meeting component; raw string when unparseable."""
    comps = meets_components(meets)
    if comps is None:
        return ["TBA — not yet scheduled"]
    lines = []
    for c in comps:
        if c["days"] is None:
            lines.append(c["raw"])  # unparseable — raw beats wrong
            continue
        label = f"({c['label']}) " if c["label"] else ""
        lines.append(f"{label}{'/'.join(c['days'])} {c['start']}–{c['end']}"
                     + (f" — {c['loc']}" if c["loc"] else ""))
    return lines


# ---------------------------------------------------------------------------
# CSV access (same reading convention as scripts/school_status.py)
# ---------------------------------------------------------------------------

def _load(name):
    path = os.path.join(_SCHOOL_DIR or "", name)
    if not _SCHOOL_DIR or not os.path.exists(path):
        return []
    try:
        with open(path, newline="", encoding="utf-8") as f:
            # A row longer than the header puts its overflow in a LIST under the
            # None restkey; a row shorter fills missing fields with None. Only
            # look at real string values, so one malformed line can't take the
            # whole file down and get misreported as "not synced yet".
            return [r for r in csv.DictReader(f)
                    if any(v.strip() for v in r.values() if isinstance(v, str))]
    except Exception:
        return []


def _parse_date(s):
    """Same formats scripts/school_status.py accepts, so the two tools never
    disagree about a row a human hand-typed as '9/2/2026'."""
    s = (s or "").strip()
    if not s:
        return None
    if "T" in s:
        s = s.split("T", 1)[0]
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%b %d, %Y", "%B %d, %Y",
                "%b %d %Y", "%m-%d-%Y", "%d %b %Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None


def _filter_courses(courses, want: str):
    return [c for c in courses
            if want in (c.get("course") or "").lower()
            or want in (c.get("code") or "").lower()]


def _unknown_course_msg(course: str) -> str:
    known = ", ".join((c.get("course") or "?").strip() for c in _load("courses.csv"))
    return f"No course matching '{course}'. Known: {known}"


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

def get_class_schedule_tool(course: str = "") -> str:
    courses = _load("courses.csv")
    if not courses:
        return ("School/courses.csv is empty or unreadable — the vault copy may "
                "not have synced yet (syncs every 10 min).")
    want = (course or "").strip().lower()
    if want:
        courses = _filter_courses(courses, want)
        if not courses:
            return _unknown_course_msg(course)

    curriculum = _load("curriculum.csv")
    today = datetime.now(LOCAL_TZ).date()
    horizon = today + timedelta(days=14)

    out = []
    for c in courses:
        code = (c.get("course") or "?").strip()
        title = (c.get("title") or "").strip()
        out.append(f"{code} — {title}" if title else code)
        instr = (c.get("instructor") or "").strip()
        if instr:
            out.append(f"  Instructor: {instr}")
        for line in parse_meets(c.get("meets", "")):
            out.append(f"  Meets: {line}")
        term = (c.get("term") or "").strip()
        notes = (c.get("notes") or "").strip()
        if term:
            out.append(f"  Term: {term}")
        if notes:
            out.append(f"  Notes: {notes}")
        coming = sorted(
            (r for r in curriculum
             if (r.get("course") or "").lower() == code.lower()
             and (d := _parse_date(r.get("date"))) and today <= d <= horizon),
            key=lambda r: r.get("date", ""))
        for r in coming[:4]:
            out.append(f"  Up next: {(r.get('date') or '').strip()}  "
                       f"{(r.get('topic') or '').strip()}")
        out.append("")

    out.append("Source: School/courses.csv + curriculum.csv in the vault "
               "(synced from the Mac every 10 min). To put these on his week "
               "grid, map them to blocks yourself and use batch_edit_schedule "
               "after showing him the full list once.")
    return "\n".join(out)


def get_school_brief_tool(days: int = 14, course: str = "") -> str:
    if not os.path.exists(_STATUS_SCRIPT):
        return f"school_status.py not found at {_STATUS_SCRIPT}"
    cmd = [sys.executable, _STATUS_SCRIPT, "--days", str(int(days))]
    course = (course or "").strip()
    if course:
        # school_status filters every CSV before its empty-state check, so an
        # unknown code makes it print "the skeleton is built but empty" — which
        # reads as "school system unpopulated" rather than "no such course".
        if not _filter_courses(_load("courses.csv"), course.lower()):
            return _unknown_course_msg(course)
        cmd += ["--course", course]
    env = dict(os.environ)
    if _SCHOOL_DIR:
        env["SCHOOL_DIR"] = _SCHOOL_DIR
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=20, env=env)
    except subprocess.TimeoutExpired:
        return "school brief timed out after 20s"
    except Exception as e:
        return f"school brief failed to run: {e}"
    if p.returncode != 0:
        return f"school brief exited {p.returncode}: {(p.stderr or '')[:400]}"
    return p.stdout.strip() or "school brief produced no output"


# ---------------------------------------------------------------------------
# Study plan — "what should I actually work on tonight, per course"
# ---------------------------------------------------------------------------
# The brief (school_status.py) answers "what's due"; this answers "what to
# study for WHICH class next", honoring each course's lead_target_days and the
# very different shapes the four syllabi took in curriculum.csv:
#   ECON/ACCT — class meetings carry lecture_ref ("class 7"); ACCT also has
#               deadline-only rows with parenthesized topics ("(deadline — no
#               class)") that are NOT meetings.
#   AIQS100   — lecture_ref empty everywhere; meetings = rows whose topic does
#               not start with "(" (paren rows are deadlines/holidays).
#   MATH120   — only exam rows exist, so class days come from meets weekday
#               math minus the campus-holiday set derived from other courses'
#               "(... no class)" rows (Labor Day, fall break, Thanksgiving —
#               NOT ACCT's routine "(deadline — no class)" Mondays).

# Assignment statuses that mean "already handled" — everything else is open.
_DONE_STATUSES = {"submitted", "graded", "done", "complete", "completed"}

# assignments.csv types that are real exams. type=quiz stays out (MATH has a
# quiz EVERY class — flooding the exam list would bury the four real tests).
_EXAM_TYPES = {"exam", "test", "midterm", "final"}

_EXAM_WORD_RE = re.compile(r"\b(exam|test|final)\b", re.IGNORECASE)
# Campus holidays only — matched against paren-topic rows so ACCT's weekly
# "(deadline — no class)" rows can't erase real MATH Mondays.
_HOLIDAY_RE = re.compile(r"labor day|fall break|thanksgiving", re.IGNORECASE)

_UNKNOWN_LOAD_LINE = "{code}: syllabus still unpublished — unknown load"


def _lead_days(course_row) -> int:
    try:
        return int((course_row.get("lead_target_days") or "").strip())
    except (ValueError, AttributeError):
        return 5  # sane default when the column is missing/garbled


def _fmt_day(d) -> str:
    """'Tue Sep 8' — no %-d (not portable), no invented precision."""
    return f"{d.strftime('%a %b')} {d.day}"


def _course_curriculum(curriculum, code):
    code = code.lower()
    out = []
    for r in curriculum:
        if (r.get("course") or "").strip().lower() != code:
            continue
        d = _parse_date(r.get("date"))
        if d:
            out.append((d, r))
    return sorted(out, key=lambda t: t[0])


def _holiday_dates(curriculum):
    """Dates of '(Labor Day — no class)'-style rows, any course."""
    out = set()
    for r in curriculum:
        topic = (r.get("topic") or "").strip()
        if topic.startswith("(") and _HOLIDAY_RE.search(topic):
            d = _parse_date(r.get("date"))
            if d:
                out.add(d)
    return out


def _row_is_exam(r) -> bool:
    return bool(_EXAM_WORD_RE.search((r.get("deliverable") or "") + " "
                                     + (r.get("topic") or "")))


def _class_day_map(courses, curriculum):
    """{code: sorted class-meeting dates} for every study course (lead > 0)."""
    dated = {}          # code -> [(date, row)] with parseable dates
    weekday_courses = []  # courses with no meeting rows at all (MATH-shaped)
    days = {}
    for c in courses:
        code = (c.get("course") or "").strip()
        rows = _course_curriculum(curriculum, code)
        dated[code] = rows
        ref_rows = [d for d, r in rows
                    if (r.get("lecture_ref") or "").strip().lower().startswith("class")]
        if ref_rows:
            # ECON/ACCT style — any lecture_ref means a real meeting.
            days[code] = sorted(d for d, r in rows
                                if (r.get("lecture_ref") or "").strip())
            continue
        topical = [d for d, r in rows
                   if not (r.get("topic") or "").strip().startswith("(")
                   and not _row_is_exam(r)]
        if topical:
            # AIQS style — meetings are every non-paren-topic row.
            days[code] = sorted(d for d, r in rows
                                if not (r.get("topic") or "").strip().startswith("("))
        elif rows or (c.get("meets") or "").strip():
            weekday_courses.append(c)

    # Term bounds for weekday math: the span of everyone else's real (non-exam)
    # meetings, so generated MATH days stop at the last week of classes instead
    # of marching into finals week.
    bound_days = [d for code, rows in dated.items() if days.get(code)
                  for d, r in rows
                  if (d in days[code]) and not _row_is_exam(r)]
    holidays = _holiday_dates(curriculum)
    for c in weekday_courses:
        code = (c.get("course") or "").strip()
        comps = meets_components(c.get("meets", "")) or []
        weekdays = {wd for comp in comps for wd in (comp["weekdays"] or [])}
        if not weekdays or not bound_days:
            days[code] = sorted(d for d, r in dated.get(code, []))
            continue
        start, end = min(bound_days), max(bound_days)
        days[code] = [start + timedelta(days=i)
                      for i in range((end - start).days + 1)
                      if (start + timedelta(days=i)).weekday() in weekdays
                      and (start + timedelta(days=i)) not in holidays]
    return days


def _open_assignments(assignments, code):
    code = code.lower()
    out = []
    for r in assignments:
        if (r.get("course") or "").strip().lower() != code:
            continue
        if (r.get("status") or "").strip().lower() in _DONE_STATUSES:
            continue
        d = _parse_date(r.get("due_date"))
        if d:
            out.append((d, r))
    return sorted(out, key=lambda t: t[0])


def _assignment_line(d, r) -> str:
    title = (r.get("title") or "?").strip()
    typ = (r.get("type") or "").strip()
    return f"{title}{f' ({typ})' if typ else ''} — due {_fmt_day(d)}"


def _quiz_pointer(for_date, class_days, exam_days):
    """MATH's daily quiz always covers the PRIOR class's material and the
    weekly Canvas problem list is the only source — there is no per-class
    topic schedule, so this never names a topic. Fires only when tomorrow is
    an actual (non-test) class day: that's the night the quiz prep matters."""
    nxt = for_date + timedelta(days=1)
    if nxt not in class_days or nxt in exam_days:
        return None
    prev = [d for d in class_days if d <= for_date]
    if not prev:
        return None
    return (f"MATH quiz next class covers {_fmt_day(prev[-1])}'s material — "
            "this week's Canvas problem list")


def _collect_exams(courses, curriculum, assignments, for_date):
    """(course, date)-deduped union of assignment exams and curriculum exam
    rows, 0-30 days out. Canvas (assignments.csv) wins the dedupe — its dates
    correct the syllabus ('EXAM 2 window' vs the real Nov 19)."""
    codes = {(c.get("course") or "").strip() for c in courses}
    seen, exams = set(), []

    for r in assignments:
        code = (r.get("course") or "").strip()
        typ = (r.get("type") or "").strip().lower()
        title = (r.get("title") or "").strip()
        if code not in codes or typ not in _EXAM_TYPES:
            continue
        # a closed exam row (taken — or deliberately skipped, like the math
        # placement) must not keep a runway alive
        if (r.get("status") or "").strip().lower() in _DONE_STATUSES:
            continue
        # canvas_sync mistyped AIQS papers as exams — a Final Paper is not a
        # final (AIQS has no exams at all; the Writing Folder replaces one).
        if code == "AIQS100" and "paper" in title.lower():
            continue
        d = _parse_date(r.get("due_date"))
        if d and (code, d) not in seen:
            seen.add((code, d))
            exams.append({"course": code, "name": title, "date": d.isoformat(),
                          "days_out": (d - for_date).days})

    for r in curriculum:
        code = (r.get("course") or "").strip()
        if code not in codes or code == "AIQS100" or not _row_is_exam(r):
            # AIQS100 skipped on purpose: no exams exist there, so every regex
            # hit ("final paper", "final projects") would be a false positive.
            continue
        text = ((r.get("deliverable") or "") + " " + (r.get("topic") or ""))
        if re.search(r"\breview\b", text, re.IGNORECASE):
            continue  # "Exam 1 Review (if time)" is prep, not an exam
        d = _parse_date(r.get("date"))
        if not d or (code, d) in seen:
            continue
        seen.add((code, d))
        name = (r.get("deliverable") or "").strip() or (r.get("topic") or "").strip()
        exams.append({"course": code, "name": name, "date": d.isoformat(),
                      "days_out": (d - for_date).days})

    return sorted((e for e in exams if 0 <= e["days_out"] <= 30),
                  key=lambda e: e["date"])


def study_plan_data(for_date=None) -> dict:
    """Everything a study-plan renderer (or daily_orders) needs, as data."""
    if for_date is None:
        for_date = datetime.now(LOCAL_TZ).date()
    elif isinstance(for_date, str):
        for_date = _parse_date(for_date) or datetime.now(LOCAL_TZ).date()

    empty = {"date": for_date.isoformat(), "lines": [], "per_course": {},
             "exams": [], "unknown": []}
    all_courses = _load("courses.csv")
    if not all_courses:
        empty["lines"] = ["School/courses.csv is empty or unreadable — the "
                          "vault copy may not have synced yet (syncs every 10 min)."]
        return empty

    # lead_target_days == 0 means "no independent prep" (AIQS850F, PHED171).
    courses = [c for c in all_courses if _lead_days(c) > 0]
    curriculum = _load("curriculum.csv")
    assignments = _load("assignments.csv")
    class_days = _class_day_map(courses, curriculum)

    per_course, unknown = {}, []
    for c in sorted(courses, key=lambda c: (c.get("course") or "")):
        code = (c.get("course") or "").strip()
        cur_rows = _course_curriculum(curriculum, code)
        open_rows = _open_assignments(assignments, code)
        if not cur_rows and not open_rows:
            # Nothing published anywhere (CSDS101) — "unknown load" is the
            # honest answer; "nothing due" would be a lie.
            unknown.append(code)
            continue

        cdays = class_days.get(code) or []
        next_class = next((d for d in cdays if d > for_date), None)
        lead = _lead_days(c)
        due_soon = [_assignment_line(d, r) for d, r in open_rows
                    if for_date <= d <= for_date + timedelta(days=lead)]
        # ACCT APQs/readings due at class start (10:00) T-split to the class
        # date itself, so "on or before" naturally counts them for that class.
        before_next = [_assignment_line(d, r) for d, r in open_rows
                       if next_class and for_date <= d <= next_class]
        pointer = None
        if code == "MATH120":
            exam_days = {d for d, r in cur_rows if _row_is_exam(r)}
            pointer = _quiz_pointer(for_date, cdays, exam_days)
        per_course[code] = {
            "due_soon": due_soon,
            "before_next_class": before_next,
            "next_class": next_class.isoformat() if next_class else None,
            "quiz_pointer": pointer,
        }

    exams = _collect_exams(courses, curriculum, assignments, for_date)

    lines = [f"STUDY PLAN — {_fmt_day(for_date)} ({for_date.isoformat()})"]
    for code, pc in per_course.items():
        nc = pc["next_class"]
        lines.append(f"{code} — next class {_fmt_day(_parse_date(nc))}" if nc
                     else f"{code} — no upcoming class on file")
        shown = set()
        for s in pc["due_soon"][:6]:
            shown.add(s)
            lines.append(f"  due soon: {s}")
        for s in pc["before_next_class"][:6]:
            if s not in shown:
                lines.append(f"  before next class: {s}")
        if pc["quiz_pointer"]:
            lines.append(f"  {pc['quiz_pointer']}")
        if not pc["due_soon"] and not pc["before_next_class"] and not pc["quiz_pointer"]:
            lines.append("  nothing in the lead window")
    for code in unknown:
        lines.append(_UNKNOWN_LOAD_LINE.format(code=code))
    if exams:
        lines.append("EXAMS WITHIN 30 DAYS:")
        for e in exams:
            lines.append(f"  {e['course']} {e['name']} — "
                         f"{_fmt_day(_parse_date(e['date']))} ({e['days_out']}d out)")

    return {"date": for_date.isoformat(), "lines": lines,
            "per_course": per_course, "exams": exams, "unknown": unknown}


def study_plan(day: str = "") -> str:
    """Human text; day in ''|'today'|'tomorrow'|ISO (or anything _parse_date
    takes — same formats as every other date in this module)."""
    want = (day or "").strip().lower()
    today = datetime.now(LOCAL_TZ).date()
    if want in ("", "today"):
        target = today
    elif want == "tomorrow":
        target = today + timedelta(days=1)
    else:
        target = _parse_date(day)
        if not target:
            return (f"Couldn't read day '{day}' — use 'today', 'tomorrow', "
                    "or a date like 2026-09-08.")
    return "\n".join(study_plan_data(target)["lines"])


# ---------------------------------------------------------------------------
# Tool plumbing (same shape as training_sync)
# ---------------------------------------------------------------------------

TOOL_SCHEMAS = [
    {
        "name": "get_class_schedule",
        "description": (
            "Alex's real class schedule for the term, straight from "
            "School/courses.csv in his vault: every course's meeting days, "
            "times, rooms, and instructor, plus what's coming up next in each "
            "course from the imported syllabus timeline (curriculum.csv). Use "
            "this for 'when is class', 'what's next in X', and for populating "
            "his week grid with class blocks — never ask him to paste his "
            "schedule. PHED171 (varsity basketball) shows TBA until the team "
            "schedule lands."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "course": {
                    "type": "string",
                    "description": "Optional course code filter, e.g. ECON103 or MATH120. Empty = all courses.",
                },
            },
        },
    },
    {
        "name": "get_school_brief",
        "description": (
            "The school due-date and pace brief (same engine as "
            "scripts/school_status.py): overdue work, everything due in the "
            "window, pace per course versus its lead target with the specific "
            "topics that would close a gap, spaced-review queue, and exam "
            "readiness. Deadlines come from School/assignments.csv, which "
            "syncs from his Canvas calendar feed every 30 minutes — this is "
            "the source of truth for tests and due dates, not email scraps."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": "Look-ahead window in days (default 14).",
                },
                "course": {
                    "type": "string",
                    "description": "Optional course code filter, e.g. ECON103.",
                },
            },
        },
    },
    {
        "name": "get_study_plan",
        "description": (
            "Per-course study plan for a given day: what's due inside each "
            "course's lead window (lead_target_days from courses.csv), what "
            "must be done before the next class meeting, when that next class "
            "is, MATH120's daily-quiz pointer (quiz every class on the PRIOR "
            "class's material — weekly Canvas problem list, no topic names "
            "exist), and every exam within 30 days. Use for 'what should I "
            "study tonight' — NOT for drafting the work itself: all four "
            "courses ban AI on submitted work, so this plans time only."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "day": {
                    "type": "string",
                    "description": "Optional: 'today' (default), 'tomorrow', or an ISO date like 2026-09-08.",
                },
            },
        },
    },
]

TOOL_NAMES = {t["name"] for t in TOOL_SCHEMAS}

TOOL_STATUS_LABELS = {
    "get_class_schedule": "Checking your class schedule…",
    "get_school_brief": "Pulling up your school brief…",
    "get_study_plan": "Building your study plan…",
}


def handle_tool_call(tool_name: str, tool_input: dict) -> str:
    if tool_name == "get_class_schedule":
        return get_class_schedule_tool(tool_input.get("course", ""))
    if tool_name == "get_school_brief":
        days = tool_input.get("days")
        # `or 14` would swallow 0, which legitimately means "today only".
        return get_school_brief_tool(
            14 if days is None else days, tool_input.get("course", ""))
    if tool_name == "get_study_plan":
        return study_plan(tool_input.get("day", ""))
    return f"unknown tool {tool_name}"
