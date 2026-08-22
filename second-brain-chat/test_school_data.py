"""
test_school_data.py — exercises school_data.py: the meets-string parser against
every format actually present in courses.csv, the class-schedule tool against a
fixture vault, and the brief tool's subprocess wrapping of
scripts/school_status.py. No network, no real vault — everything runs against a
temp SCHOOL dir, same harness style as test_training_sync.py.

Run:  python3 test_school_data.py
"""

import csv
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


# ---- study plan -------------------------------------------------------
# Fixed September-2026 fixture (Sep 1 is a Tuesday, Labor Day is Mon Sep 7) so
# every date is deterministic — targets go in through the for_date/day arg, not
# the wall clock. Mirrors each course's real curriculum shape: ECON/ACCT rows
# carry lecture_ref, ACCT adds paren deadline rows, AIQS has no lecture_ref
# anywhere, MATH has only exam rows, CSDS has nothing at all.

print("study plan")

sp = tempfile.mkdtemp(prefix="school-data-plan-")
sp_school = os.path.join(sp, "School")
os.makedirs(sp_school)

with open(os.path.join(sp_school, "courses.csv"), "w", encoding="utf-8") as f:
    f.write(
        "course,code,title,instructor,meets,term,lead_target_days,"
        "prepared_through,syllabus_status,notes\n"
        "ACCT100,ACCT 100,Foundations of Accounting I,Mark Jarvis,"
        "TuTh 10:00-11:15AM Peter B Lewis 201,Fall 2026,5,2026-08-24,imported,\n"
        "AIQS100,AIQS 100,Academic Inquiry Seminar,Dr. N,"
        "MWF 10:25-11:15AM Crawford Hall A13,Fall 2026,3,2026-08-24,imported,\n"
        "CSDS101,CSDS 101,The Digital Revolution,,"
        "MW 12:35-1:50PM Sears 333 + Lab W 6:30-8:30PM Olin 304,Fall 2026,5,"
        "2026-08-24,pending,\n"
        "ECON103,ECON 103,Prin of Macroeconomics,Peter Hammack,"
        "TuTh 11:30AM-12:45PM Peter B Lewis 201,Fall 2026,5,2026-08-24,imported,\n"
        "MATH120,MATH 120,Elem Functions,Adam Krause,"
        "MWF 9:20-10:10AM Olin 305,Fall 2026,7,2026-08-24,imported,\n"
        "PHED171,PHED 171,Varsity Basketball (Men),,TBA,Fall 2026,0,"
        "2026-08-24,n/a,\n"
    )

with open(os.path.join(sp_school, "curriculum.csv"), "w", encoding="utf-8") as f:
    f.write("course,week,date,topic,readings,lecture_ref,deliverable,prepared,notes\n")
    # ECON/ACCT style: meetings have lecture_ref
    f.write("ECON103,1,2026-09-01,Foundations I,T1,class 1,,,\n")
    f.write("ECON103,1,2026-09-03,Foundations II,T2,class 2,,,\n")
    f.write("ECON103,2,2026-09-08,Supply and Demand,T3,class 3,,,\n")
    f.write("ECON103,2,2026-09-10,Elasticity,T4,class 4,,,\n")
    f.write("ECON103,5,2026-10-01,EXAM 1 (in class),all to date,class 9,Exam 1,,\n")
    f.write("ACCT100,1,2026-09-01,Business Organization,Ch.1,class 1,,,\n")
    f.write("ACCT100,1,2026-09-03,Financial Statements,Ch.2,class 2,,,\n")
    f.write("ACCT100,2,2026-09-08,GAAP,Ch.3,class 3,APQ due 10:00 at start of class,,\n")
    f.write("ACCT100,2,2026-09-10,Adjustments,Ch.4,class 4,,,\n")
    f.write("ACCT100,2,2026-08-31,(deadline — no class),,,HW Day 1&2 due 11:59pm,,\n")
    f.write("ACCT100,3,2026-09-07,(Labor Day — no class),,,HW Day 3&4 due 11:59pm,,\n")
    f.write("ACCT100,3,2026-09-15,Exam 1 Review (if time),HW material,class 5,,,\n")
    f.write("ACCT100,3,2026-09-17,EXAM 1 (in class) — financial accounting,"
            "all material,class 6,Exam 1,,\n")
    # AIQS style: lecture_ref empty everywhere; paren rows are not meetings
    f.write("AIQS100,1,2026-09-02,Close reading,TSIS intro,,,,\n")
    f.write("AIQS100,1,2026-09-04,Sonnets 1 18 20,Sonnets,,,,\n")
    f.write("AIQS100,2,2026-09-07,(Labor Day — no class),,,,,\n")
    f.write("AIQS100,2,2026-09-09,Sonnets 42 116 130,Sonnets,,,,\n")
    f.write("AIQS100,2,2026-09-13,(Sunday deadline),,,Paper 1 due 11:59pm,,\n")
    # MATH style: ONLY exam rows — class days come from meets weekday math
    f.write("MATH120,3,2026-09-25,TEST 1 (in class),per weekly Canvas lists,,Test 1,,\n")
    f.write("MATH120,15,2026-12-11,FINAL EXAM (cumulative),all material,final,Final Exam,,\n")

with open(os.path.join(sp_school, "assignments.csv"), "w", encoding="utf-8") as f:
    f.write("course,title,type,due_date,weight_pct,est_hours,actual_hours,status,"
            "topic,source,submitted_date,grade,notes\n")
    f.write("ECON103,HW 2,homework,2026-09-09,,,,open,,canvas,,,\n")
    f.write("ECON103,Reading 3,reading,2026-09-11,,,,open,,canvas,,,\n")
    f.write("ECON103,Problem Set 4,homework,2026-09-12,,,,open,,canvas,,,\n")
    f.write("ECON103,Old HW,homework,2026-09-08,,,,submitted,,canvas,,,\n")
    f.write("ECON103,Exam 1,exam,2026-10-01T02:30,16.7,,,open,,canvas,,,\n")
    f.write("ECON103,Final Exam,exam,2026-12-14T12:00,33.3,,,open,,canvas,,,\n")
    f.write("ACCT100,Day 3 APQ - GAAP,assignment,2026-09-08T10:00,,,,open,,canvas,,,\n")
    f.write("MATH120,HW week 3,homework,2026-09-12,,,,open,,manual,,,\n")
    f.write("MATH120,Weekly quiz,quiz,2026-09-09,,,,open,,manual,,,\n")
    f.write("AIQS100,Final Paper,exam,2026-09-20,,,,open,,canvas,,,\n")
    f.write("AIQS100,Paper 1,paper,2026-09-13,,,,open,,canvas,,,\n")
    f.write("MATH120,Math Placement Exam,exam,2026-09-10,,,,done,,manual,,,skipped by decision\n")

with open(os.path.join(sp_school, "review-log.csv"), "w", encoding="utf-8") as f:
    f.write("course,topic,last_reviewed,confidence,next_due,times_reviewed,notes\n")

school_data.init(sp)

# Sunday Sep 6: tomorrow is Labor Day — a MATH "class weekday" that is NOT a
# class day. The plan must skip it for next-class AND stay quiet on the quiz.
data = school_data.study_plan_data("2026-09-06")
pc = data["per_course"]

check("ECON lead window catches HW 2",
      any("HW 2" in s for s in pc["ECON103"]["due_soon"]))
check("lead-window boundary day (+5) is included",
      any("Reading 3" in s for s in pc["ECON103"]["due_soon"]))
check("one day past the lead window (+6) is excluded",
      not any("Problem Set 4" in s for s in pc["ECON103"]["due_soon"]))
check("MATH's wider lead (7d) catches what ECON's (5d) would not",
      any("HW week 3" in s for s in pc["MATH120"]["due_soon"]))
check("submitted work never resurfaces as due",
      not any("Old HW" in s for s in pc["ECON103"]["due_soon"] +
              pc["ECON103"]["before_next_class"]))
check("APQ due at class start counts for that class",
      any("Day 3 APQ" in s for s in pc["ACCT100"]["before_next_class"]))
check("AIQS next class = next non-paren row, not the Labor Day row",
      pc["AIQS100"]["next_class"] == "2026-09-09")
check("MATH next class skips the Labor Day Monday",
      pc["MATH120"]["next_class"] == "2026-09-09")
check("no quiz pointer when tomorrow is a holiday",
      pc["MATH120"]["quiz_pointer"] is None)
check("quiz pointer only ever exists for MATH",
      all(pc[c]["quiz_pointer"] is None for c in pc if c != "MATH120"))

# Tuesday Sep 8: tomorrow (Wed) is a real MATH class — quiz covers the PRIOR
# class (Fri Sep 4; Labor Day Monday was no class), pointing at the Canvas
# problem list and never at an invented topic name.
data2 = school_data.study_plan_data("2026-09-08")
ptr = data2["per_course"]["MATH120"]["quiz_pointer"]
check("quiz pointer fires the night before a class day", ptr is not None)
check("pointer names the prior CLASS day (Fri Sep 4, not Labor Day Mon)",
      ptr is not None and "Sep 4" in ptr)
check("pointer points at the Canvas problem list, no invented topics",
      ptr is not None and "Canvas problem list" in ptr)

# Friday Sep 4: tomorrow is Saturday — no class, no pointer.
data3 = school_data.study_plan_data("2026-09-04")
check("no quiz pointer when tomorrow isn't a class day",
      data3["per_course"]["MATH120"]["quiz_pointer"] is None)

# Exams from Sep 6: ACCT Exam 1 (curriculum-only), MATH Test 1, ECON Exam 1
# (in both files — deduped to one). AIQS "Final Paper" (canvas_sync mistype),
# the ACCT review day, the quiz row, and the >30d finals must all stay out.
exams = data["exams"]
check("curriculum-only ACCT exam makes the list",
      any(e["course"] == "ACCT100" and e["date"] == "2026-09-17" for e in exams))
check("MATH test row makes the list",
      any(e["course"] == "MATH120" and e["date"] == "2026-09-25" for e in exams))
check("ECON exam in both files appears exactly once",
      sum(1 for e in exams if e["course"] == "ECON103") == 1)
check("AIQS 'Final Paper' mistyped as exam is excluded",
      not any(e["course"] == "AIQS100" for e in exams))
check("'Exam 1 Review' day is prep, not an exam",
      not any("Review" in e["name"] for e in exams))
check("a done exam row (taken or skipped by decision) keeps no runway alive",
      not any("Placement" in e["name"] for e in exams))
check("exams >30 days out stay off the list",
      not any(e["date"] in ("2026-12-11", "2026-12-14") for e in exams))
check("exam days_out counted from the plan date",
      next(e["days_out"] for e in exams if e["date"] == "2026-09-17") == 11)

# CSDS101 has zero rows anywhere: unknown load, never "nothing due".
check("CSDS101 lands in unknown", data["unknown"] == ["CSDS101"])
check("CSDS101 renders as unknown load",
      any("CSDS101: syllabus still unpublished — unknown load" in l
          for l in data["lines"]))
check("CSDS101 never claims nothing due", "CSDS101 —" not in "\n".join(data["lines"]))
check("lead_target_days=0 course is skipped entirely",
      "PHED171" not in pc and "PHED171" not in data["unknown"])

# day argument plumbing
et_now = datetime.now(ZoneInfo("America/New_York")).date()
out = school_data.study_plan("tomorrow")
check("day='tomorrow' plans tomorrow",
      (et_now + timedelta(days=1)).isoformat() in out)
out = school_data.study_plan("2026-09-06")
check("ISO day arg plans that date", "2026-09-06" in out and "STUDY PLAN" in out)
out = school_data.study_plan("someday")
check("unreadable day explains itself", "Couldn't read day" in out)

# tool plumbing
check("get_study_plan registered",
      "get_study_plan" in school_data.TOOL_NAMES
      and "get_study_plan" in school_data.TOOL_STATUS_LABELS
      and any(t["name"] == "get_study_plan" for t in school_data.TOOL_SCHEMAS))
out = school_data.handle_tool_call("get_study_plan", {"day": "2026-09-06"})
check("handle_tool_call routes get_study_plan",
      "STUDY PLAN" in out and "2026-09-06" in out)

# structured meets parser feeds the weekday math without changing parse_meets
comps = school_data.meets_components("MWF 9:20-10:10AM Olin 305")
check("structured parser exposes weekday numbers",
      comps is not None and comps[0]["weekdays"] == [0, 2, 4])
check("structured parser: TBA is None", school_data.meets_components("TBA") is None)
comps = school_data.meets_components("MW 12:35-1:50PM Sears 333 + Lab W 6:30-8:30PM Olin 304")
check("structured parser keeps the Lab component",
      comps is not None and len(comps) == 2 and comps[1]["label"] == "Lab")

# TZ discipline: on a skewed-clock node the plan still dates to Alex's day.
plan_probe = (
    "import school_data, sys;"
    f"school_data.init({sp!r});"
    "sys.stdout.write(school_data.study_plan_data()['date'])"
)
p = subprocess.run([sys.executable, "-c", plan_probe], capture_output=True,
                   text=True, env=dict(os.environ, TZ=skewed),
                   cwd=os.path.dirname(os.path.abspath(__file__)))
check("on a skewed-clock node the plan is dated to Alex's day",
      p.stdout.strip() == datetime.now(ZoneInfo("America/New_York")).date().isoformat())

shutil.rmtree(sp, ignore_errors=True)
school_data.init(tmp)


# ---- study review logger ---------------------------------------------
# The write half of the review loop, against the same tmp fixture: ECON103 has
# an open quiz due TOMORROW (so exam anchoring is live), review-log.csv starts
# header-only. school_status.py stays the read-side source of truth — the last
# check below proves it can read every row this logger writes.

print("study review logger")

rl_path = os.path.join(school, "review-log.csv")
_PINNED_HEADER = "course,topic,last_reviewed,confidence,next_due,times_reviewed,notes"


def _ledger():
    with open(rl_path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


# append path
out = school_data.log_study_review_tool(
    "econ103", "Supply and demand", 4, "missed elasticity Q6")
check("append reports course, spacing, and recall count",
      "ECON103" in out and "+16d" in out and "1/3" in out)
check("append warns about the exam it now anchors to", "Syllabus Quiz" in out)
rows = _ledger()
check("one row appended with times_reviewed=1",
      len(rows) == 1 and rows[0]["times_reviewed"] == "1")
with open(rl_path, encoding="utf-8") as f:
    check("header stays the pinned seven columns",
          f.readline().strip() == _PINNED_HEADER)
check("course canonicalized to the courses.csv spelling",
      rows[0]["course"] == "ECON103")
check("next_due = today + SPACING[confidence] (4 -> +16d)",
      rows[0]["last_reviewed"] == et_today.isoformat()
      and rows[0]["next_due"] == (et_today + timedelta(days=16)).isoformat())

# update path — same course+topic matches case-insensitively
out = school_data.log_study_review_tool("ECON103", "supply AND demand", 2)
rows = _ledger()
check("re-log updates the row instead of appending",
      len(rows) == 1 and rows[0]["times_reviewed"] == "2")
check("re-log recomputes next_due from the NEW confidence (2 -> +3d)",
      rows[0]["confidence"] == "2"
      and rows[0]["next_due"] == (et_today + timedelta(days=3)).isoformat())
check("re-log without notes keeps the old note",
      rows[0]["notes"] == "missed elasticity Q6")

# a different topic appends; confidence garbage/overflow degrades sanely
school_data.log_study_review_tool("MATH120", "Function transformations", 5)
school_data.log_study_review_tool("ECON103", "Garbled confidence", "not-a-number")
school_data.log_study_review_tool("ECON103", "Clamp check", 9)
rows = {r["topic"]: r for r in _ledger()}
check("different topics append their own rows", len(rows) == 4)
check("unreadable confidence defaults to 3 (+7d)",
      rows["Garbled confidence"]["confidence"] == "3"
      and rows["Garbled confidence"]["next_due"]
      == (et_today + timedelta(days=7)).isoformat())
check("out-of-range confidence clamps to 5 (+35d)",
      rows["Clamp check"]["confidence"] == "5"
      and rows["Clamp check"]["next_due"]
      == (et_today + timedelta(days=35)).isoformat())

# bad inputs never touch the ledger
before = open(rl_path, encoding="utf-8").read()
out = school_data.log_study_review_tool("BIO999", "Cells", 3)
check("unknown course guarded with the known list",
      "No course matching" in out and "ECON103" in out)
out = school_data.log_study_review_tool("ECON103", "   ", 3)
check("blank topic guarded", "topic is required" in out)
check("guarded calls leave the ledger untouched",
      open(rl_path, encoding="utf-8").read() == before)

# off-node guard — the server vault is a pull mirror (august_tracker stance)
school_data.init(tmp, node_runtime="server")
out = school_data.log_study_review_tool("ECON103", "Server-side attempt", 3)
check("off-node write refused, pointing at the Mac node",
      "Mac node" in out and "Nothing was logged" in out)
check("off-node call leaves the ledger untouched",
      open(rl_path, encoding="utf-8").read() == before)
out = school_data.get_review_status_tool()
check("review status still READS off-node", "REVIEW STATUS" in out)
school_data.init(tmp)  # back to local

# get_review_status: due filter + exam pull-forward + readiness
# ECON's quiz is tomorrow, so every ECON next_due (+3/+7/+35) lands past it and
# must pull in to today (Cepeda gap); MATH has no exam, so its +35 stays out.
with open(rl_path, "a", newline="", encoding="utf-8") as f:
    f.write(f"MATH120,Old blank next due,{(et_today - timedelta(days=10)).isoformat()},3,,1,\n")
out = school_data.get_review_status_tool()
due_part = out.split("EXAM READINESS")[0]
check("exam-anchored rows pull forward into today's queue",
      "Supply and demand" in due_part and "Clamp check" in due_part)
check("far-future review with no exam stays out of the due list",
      "Function transformations" not in due_part)
check("blank next_due computes from last_reviewed + spacing (10d ago, conf 3 -> 3d overdue)",
      "Old blank next due" in due_part and "3d overdue" in due_part)
check("readiness section fires for the quiz tomorrow",
      "EXAM READINESS" in out and "Syllabus Quiz" in out and "in 1d" in out)
check("readiness counts topics against the 3-recall target",
      "0/3 topics at target" in out and "(2/3 recalls)" in out
      and "(1/3 recalls)" in out)

out = school_data.get_review_status_tool("math120")
check("course filter narrows queue and readiness to that course",
      "Old blank next due" in out and "Supply and demand" not in out
      and "Syllabus Quiz" not in out)
out = school_data.get_review_status_tool("BIO999")
check("review status guards unknown courses too", "No course matching" in out)

# tool plumbing
check("both review tools registered",
      {"log_study_review", "get_review_status"} <= school_data.TOOL_NAMES
      and "log_study_review" in school_data.TOOL_STATUS_LABELS
      and "get_review_status" in school_data.TOOL_STATUS_LABELS)
out = school_data.handle_tool_call(
    "log_study_review", {"course": "ECON103", "topic": "Routed via tool call"})
check("handle_tool_call routes log_study_review (confidence defaults to 3)",
      "Routed via tool call" in out and "+7d" in out)
out = school_data.handle_tool_call("get_review_status", {})
check("handle_tool_call routes get_review_status", "REVIEW STATUS" in out)

# the read-side source of truth accepts everything the logger wrote
out = school_data.get_school_brief_tool(days=14)
check("school_status.py reads the ledger this module wrote",
      "SCHOOL BRIEF" in out and "Supply and demand" in out)

# a deleted ledger is recreated with the pinned header
os.remove(rl_path)
school_data.log_study_review_tool("ECON103", "Fresh ledger", 3)
with open(rl_path, encoding="utf-8") as f:
    check("missing ledger recreated with the pinned header",
          f.readline().strip() == _PINNED_HEADER
          and "Fresh ledger" in f.read())


# ---- missing data fail-soft ------------------------------------------

print("fail-soft")

school_data.init(os.path.join(tmp, "nonexistent"))
out = school_data.get_class_schedule_tool()
check("missing vault explains itself", "empty or unreadable" in out)
out = school_data.log_study_review_tool("ECON103", "Anything", 3)
check("logger refuses to invent a School dir in a missing vault",
      "School folder not found" in out)

school_data.init(tmp)  # restore for cleanliness

shutil.rmtree(tmp, ignore_errors=True)

print(f"\n{sum(_results)}/{len(_results)} passed")
raise SystemExit(0 if all(_results) else 1)
