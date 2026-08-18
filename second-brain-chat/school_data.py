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


def parse_meets(meets: str):
    """One human line per meeting component; raw string when unparseable.

    A start time with no AM/PM inherits the end's ('12:35-1:50PM' means both PM
    in every real row — registrar convention).
    """
    meets = (meets or "").strip()
    if not meets or meets.upper() == "TBA":
        return ["TBA — not yet scheduled"]
    lines = []
    for comp in meets.split(" + "):
        m = _COMPONENT_RE.match(comp.strip())
        days = _split_days(m.group("days")) if m else None
        if not m or not days:
            lines.append(comp.strip())  # unparseable — raw beats wrong
            continue
        end_ap = re.search(r"(AM|PM)", m.group("end"), re.IGNORECASE)
        meridiem = end_ap.group(1).upper() if end_ap else ""
        start = _norm_time(m.group("start"), meridiem)
        end = _norm_time(m.group("end"), meridiem)
        label = f"({m.group('label').title()}) " if m.group("label") else ""
        loc = m.group("loc").strip()
        lines.append(f"{label}{'/'.join(days)} {start}–{end}"
                     + (f" — {loc}" if loc else ""))
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
]

TOOL_NAMES = {t["name"] for t in TOOL_SCHEMAS}

TOOL_STATUS_LABELS = {
    "get_class_schedule": "Checking your class schedule…",
    "get_school_brief": "Pulling up your school brief…",
}


def handle_tool_call(tool_name: str, tool_input: dict) -> str:
    if tool_name == "get_class_schedule":
        return get_class_schedule_tool(tool_input.get("course", ""))
    if tool_name == "get_school_brief":
        days = tool_input.get("days")
        # `or 14` would swallow 0, which legitimately means "today only".
        return get_school_brief_tool(
            14 if days is None else days, tool_input.get("course", ""))
    return f"unknown tool {tool_name}"
