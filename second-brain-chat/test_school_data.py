"""
test_school_data.py — exercises school_data.py: the meets-string parser against
every format actually present in courses.csv, the class-schedule tool against a
fixture vault, and the brief tool's subprocess wrapping of
scripts/school_status.py. No network, no real vault — everything runs against a
temp SCHOOL dir, same harness style as test_training_sync.py.

Run:  python3 test_school_data.py
"""

import os
import shutil
import subprocess
import sys
import tempfile
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import school_data

_results = []


def check(label, cond):
    _results.append(bool(cond))
    print(("  ok " if cond else "  FAIL ") + label)


# ---- parse_meets: every real courses.csv format -----------------------

print("parse_meets")

lines = school_data.parse_meets("TuTh 10:00-11:15AM Peter B Lewis 201")
check("TuTh both-AM", lines == ["Tue/Thu 10:00 AM–11:15 AM — Peter B Lewis 201"])

lines = school_data.parse_meets("MWF 9:20-10:10AM Olin 305")
check("MWF single-letter days", lines == ["Mon/Wed/Fri 9:20 AM–10:10 AM — Olin 305"])

lines = school_data.parse_meets("TuTh 11:30AM-12:45PM Peter B Lewis 201")
check("explicit AM-PM crossing noon",
      lines == ["Tue/Thu 11:30 AM–12:45 PM — Peter B Lewis 201"])

lines = school_data.parse_meets("MW 12:35-1:50PM Sears 333 + Lab W 6:30-8:30PM Olin 304")
check("two components", len(lines) == 2)
check("start inherits end's PM", lines[0] == "Mon/Wed 12:35 PM–1:50 PM — Sears 333")
check("labeled lab component", lines[1] == "(Lab) Wed 6:30 PM–8:30 PM — Olin 304")

check("TBA", school_data.parse_meets("TBA") == ["TBA — not yet scheduled"])
check("empty", school_data.parse_meets("") == ["TBA — not yet scheduled"])

lines = school_data.parse_meets("whenever the coach says")
check("unparseable falls back to raw", lines == ["whenever the coach says"])


# ---- fixture vault ----------------------------------------------------

tmp = tempfile.mkdtemp(prefix="school-data-test-")
school = os.path.join(tmp, "School")
os.makedirs(school)

with open(os.path.join(school, "courses.csv"), "w", encoding="utf-8") as f:
    f.write(
        "course,code,title,instructor,meets,term,lead_target_days,"
        "prepared_through,syllabus_status,notes\n"
        "ECON103,ECON 103,Prin of Macroeconomics,Peter Hammack,"
        "TuTh 11:30AM-12:45PM Peter B Lewis 201,Fall 2026,5,2026-08-24,"
        "imported,3 units\n"
        "MATH120,MATH 120,Elem Functions,,MWF 9:20-10:10AM Olin 305,"
        "Fall 2026,7,2026-08-24,pending,sequential\n"
    )

# curriculum: one topic tomorrow (in window), one far future (out of window)
tomorrow = (date.today() + timedelta(days=1)).isoformat()
far = (date.today() + timedelta(days=60)).isoformat()
with open(os.path.join(school, "curriculum.csv"), "w", encoding="utf-8") as f:
    f.write(
        "course,week,date,topic,readings,lecture_ref,deliverable,prepared,notes\n"
        f"ECON103,1,{tomorrow},Intro — What is Economics,T1-2,class 1,,,\n"
        f"ECON103,9,{far},Aggregate Demand,T13,class 17,,,\n"
    )

with open(os.path.join(school, "assignments.csv"), "w", encoding="utf-8") as f:
    f.write(
        "course,title,type,due_date,weight_pct,est_hours,actual_hours,status,"
        "topic,source,submitted_date,grade,notes\n"
        f"ECON103,Syllabus Quiz,quiz,{tomorrow},,,,open,,manual,,,\n"
    )

with open(os.path.join(school, "review-log.csv"), "w", encoding="utf-8") as f:
    f.write("course,topic,last_reviewed,confidence,next_due,times_reviewed,notes\n")

school_data.init(tmp)


# ---- get_class_schedule ----------------------------------------------

print("get_class_schedule")

out = school_data.get_class_schedule_tool()
check("both courses listed", "ECON103" in out and "MATH120" in out)
check("meets parsed", "Tue/Thu 11:30 AM–12:45 PM" in out)
check("instructor shown", "Peter Hammack" in out)
check("near-term curriculum shown", "What is Economics" in out)
check("far-future curriculum hidden", "Aggregate Demand" not in out)
check("points at batch_edit_schedule", "batch_edit_schedule" in out)

out = school_data.get_class_schedule_tool("math120")
check("course filter case-insensitive", "MATH120" in out and "ECON103" not in out)

out = school_data.get_class_schedule_tool("BIO999")
check("unknown course names the known ones", "No course matching" in out and "ECON103" in out)


# ---- get_school_brief -------------------------------------------------

print("get_school_brief")

out = school_data.get_school_brief_tool(days=14)
check("brief runs", "SCHOOL BRIEF" in out)
check("fixture deadline visible", "Syllabus Quiz" in out)
check("no ANSI colors when piped", "\033[" not in out)

out = school_data.get_school_brief_tool(days=14, course="ECON103")
check("course-filtered brief runs", "SCHOOL BRIEF" in out)

# An unknown course must NOT reach school_status.py: it filters every CSV before
# its empty-state check, so it would answer "the skeleton is built but empty" —
# which reads as "your whole school system is unpopulated".
out = school_data.get_school_brief_tool(days=14, course="BIO999")
check("unknown course is caught before shelling out", "No course matching" in out)
check("...and does not claim the system is empty", "skeleton" not in out)

# days=0 means "today only" — `or 14` would silently widen it to two weeks.
out = school_data.handle_tool_call("get_school_brief", {"days": 0})
check("days=0 stays 0", "NEXT 0 DAYS" in out)
out = school_data.handle_tool_call("get_school_brief", {})
check("days omitted defaults to 14", "NEXT 14 DAYS" in out)


# ---- malformed CSV rows ----------------------------------------------

print("malformed rows")

mal = tempfile.mkdtemp(prefix="school-data-malformed-")
mal_school = os.path.join(mal, "School")
os.makedirs(mal_school)
with open(os.path.join(mal_school, "courses.csv"), "w", encoding="utf-8") as f:
    f.write("course,code,title,instructor,meets,term,lead_target_days,"
            "prepared_through,syllabus_status,notes\n")
    f.write("ECON103,ECON 103,Prin of Macroeconomics,,"
            "TuTh 11:30AM-12:45PM PBL 201,Fall 2026,5,2026-08-24,imported,\n")
    f.write(",,,,,,,,,,,,\n")          # overflow row: more commas than header
    f.write("MATH120\n")                # short row: trailing fields are None
with open(os.path.join(mal_school, "curriculum.csv"), "w", encoding="utf-8") as f:
    f.write("course,week,date,topic,readings,lecture_ref,deliverable,prepared,notes\n")
    slash = (date.today() + timedelta(days=2)).strftime("%m/%d/%Y")
    f.write(f"ECON103,1,{slash},Hand-typed slash date,,,,,\n")
school_data.init(mal)

out = school_data.get_class_schedule_tool()
check("overflow row doesn't nuke the whole file", "ECON103" in out)
check("short row renders no literal None", "None" not in out)
check("slash date parses like school_status does", "Hand-typed slash date" in out)

shutil.rmtree(mal, ignore_errors=True)
school_data.init(tmp)


# ---- timezone: the server container runs UTC -------------------------
# Between 8pm ET and midnight, a naive today() is already tomorrow. Both tools
# must still answer with Alex's day, or tonight's prep topic silently vanishes
# and work due today gets reported as overdue.

print("timezone")

check("module pins America/New_York", str(school_data.LOCAL_TZ) == "America/New_York")

et_today = datetime.now(ZoneInfo("America/New_York")).date()

tz = tempfile.mkdtemp(prefix="school-data-tz-")
tz_school = os.path.join(tz, "School")
os.makedirs(tz_school)
with open(os.path.join(tz_school, "courses.csv"), "w", encoding="utf-8") as f:
    f.write("course,code,title,instructor,meets,term,lead_target_days,"
            "prepared_through,syllabus_status,notes\n")
    f.write("ECON103,ECON 103,Prin of Macroeconomics,,"
            "TuTh 11:30AM-12:45PM PBL 201,Fall 2026,5,"
            f"{et_today.isoformat()},imported,\n")
with open(os.path.join(tz_school, "curriculum.csv"), "w", encoding="utf-8") as f:
    f.write("course,week,date,topic,readings,lecture_ref,deliverable,prepared,notes\n")
    f.write(f"ECON103,1,{et_today.isoformat()},Tonight's prep target,,,,,\n")
with open(os.path.join(tz_school, "assignments.csv"), "w", encoding="utf-8") as f:
    f.write("course,title,type,due_date,weight_pct,est_hours,actual_hours,status,"
            "topic,source,submitted_date,grade,notes\n")
    f.write(f"ECON103,Due Tonight,homework,{et_today.isoformat()}T23:30,,,,open,,manual,,,\n")
with open(os.path.join(tz_school, "review-log.csv"), "w", encoding="utf-8") as f:
    f.write("course,topic,last_reviewed,confidence,next_due,times_reviewed,notes\n")

# The real bug window (UTC vs ET) is only 8pm-midnight ET, so testing against
# TZ=UTC would pass vacuously for 20 hours a day. Instead pick whichever extreme
# zone's calendar day currently differs from Alex's — one of these two always
# does — which reproduces the same "container's day != Alex's day" condition at
# any hour. Under it, a naive today() is provably wrong.
skewed = next(z for z in ("Pacific/Kiritimati", "Pacific/Midway")
              if datetime.now(ZoneInfo(z)).date() != et_today)
check("a skewed container day is actually being simulated",
      datetime.now(ZoneInfo(skewed)).date() != et_today)

skew_env = dict(os.environ, TZ=skewed, SCHOOL_DIR=tz_school)
probe = (
    "import school_data, sys;"
    f"school_data.init({tz!r});"
    "out = school_data.get_class_schedule_tool();"
    "brief = school_data.get_school_brief_tool(days=0);"
    "sys.stdout.write(out + '\\n=====\\n' + brief)"
)
p = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True,
                   env=skew_env, cwd=os.path.dirname(os.path.abspath(__file__)))
sched_out, _, brief_out = p.stdout.partition("\n=====\n")

check("on a skewed-clock node the class tool keeps today's topic",
      "Tonight's prep target" in sched_out)
check("on a skewed-clock node the brief still dates itself to Alex's day",
      et_today.strftime("%B %d, %Y") in brief_out)
check("on a skewed-clock node today's work is not called overdue",
      "OVERDUE" not in brief_out)

shutil.rmtree(tz, ignore_errors=True)
school_data.init(tmp)


# ---- missing data fail-soft ------------------------------------------

print("fail-soft")

school_data.init(os.path.join(tmp, "nonexistent"))
out = school_data.get_class_schedule_tool()
check("missing vault explains itself", "empty or unreadable" in out)

school_data.init(tmp)  # restore for cleanliness

shutil.rmtree(tmp, ignore_errors=True)

print(f"\n{sum(_results)}/{len(_results)} passed")
raise SystemExit(0 if all(_results) else 1)
