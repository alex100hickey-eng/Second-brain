"""
test_school_state.py — the phone→vault loop (school_state), the nightly Canvas
status apply script, the outbox self-close sweep, and grades read from
assignments.csv. Fake Supabase, temp vault, no network.

Run:  python3 test_school_state.py
"""

import csv
import importlib.util
import json
import os
import shutil
import sys
import tempfile
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import intake            # noqa: E402
import outbox            # noqa: E402
import school_data       # noqa: E402
import school_grades     # noqa: E402
import school_state      # noqa: E402

_results = []


def check(label, cond):
    _results.append(bool(cond))
    print(("  ok   " if cond else "  FAIL ") + label)


# ---- minimal fake Supabase (enough for intake._load_state/_save_state + outbox)

class _Q:
    def __init__(self, sb):
        self.sb, self.f, self.like, self.lim, self.op, self.payload = sb, [], [], None, None, None
        self.desc = True

    def select(self, *a):
        self.op = "select"; return self

    def insert(self, row):
        self.op, self.payload = "insert", row; return self

    def update(self, row):
        self.op, self.payload = "update", row; return self

    def eq(self, k, v):
        self.f.append((k, v)); return self

    def ilike(self, k, pat):
        self.like.append((k, pat.strip("%"))); return self

    def order(self, k, desc=False):
        self.desc = desc; return self

    def limit(self, n):
        self.lim = n; return self

    def execute(self):
        rows = self.sb.rows
        if self.op == "insert":
            self.sb.seq += 1
            row = {"id": self.sb.seq, **self.payload}
            rows.append(row)
            return type("R", (), {"data": [row]})()
        if self.op == "update":
            out = []
            for r in rows:
                if all(r.get(k) == v for k, v in self.f):
                    r.update(self.payload); out.append(r)
            return type("R", (), {"data": out})()
        sel = [r for r in rows if all(r.get(k) == v for k, v in self.f)
               and all(s in str(r.get(k, "")) for k, s in self.like)]
        sel.sort(key=lambda r: r["id"], reverse=self.desc)
        if self.lim:
            sel = sel[:self.lim]
        return type("R", (), {"data": [dict(r) for r in sel]})()


class FakeSB:
    def __init__(self):
        self.rows, self.seq = [], 0

    def table(self, name):
        return _Q(self)


# ---- fixture vault -----------------------------------------------------------

tmp = tempfile.mkdtemp(prefix="school-state-test-")
school = os.path.join(tmp, "School")
os.makedirs(school)
with open(os.path.join(school, "courses.csv"), "w", encoding="utf-8") as f:
    f.write("course,code,title,instructor,meets,term,lead_target_days,prepared_through,syllabus_status,notes\n"
            "ACCT100,ACCT 100,Foundations of Accounting I,,TuTh 10:00-11:15AM PBL 201,Fall 2026,10,2026-09-01,imported,\n"
            "ECON103,ECON 103,Prin of Macroeconomics,,TuTh 11:30AM-12:45PM PBL 201,Fall 2026,10,2026-09-01,imported,\n")
with open(os.path.join(school, "grading.csv"), "w", encoding="utf-8") as f:
    f.write("course,component,weight_pct,count,drops,notes\n"
            "ACCT100,Exams 1-4,64,4,0,\nACCT100,Homework,16,27,3,\n"
            "ACCT100,Adaptive Practice Questions (APQs),8,22,4,\n"
            "ECON103,Course Engagement — Homework Scores (HW 1-5 + Canvas quizzes),11.1,8,0,\n")
with open(os.path.join(school, "assignments.csv"), "w", encoding="utf-8") as f:
    f.write("course,title,type,due_date,weight_pct,est_hours,actual_hours,status,topic,source,submitted_date,grade,notes\n"
            "ACCT100,Day 4 APQ - Financial Statement Analysis,assignment,2026-09-03T10:00,,,,open,,canvas:event-assignment-11,,,\n"
            "ACCT100,HW - Day 2 - Elements of Financial Statements,homework,2026-08-31,,,,open,,canvas:event-assignment-12,,,\n"
            "ECON103,QUIZ: GDP,quiz,2026-09-15,,,,open,,canvas:event-assignment-13,,,\n"
            "ACCT100,Day 1 Reading,reading,2026-08-25T10:00,,,,open,,canvas:event-assignment-14,,,\n"
            "MATH120,Math Placement Exam,exam,2026-08-21,,,,done,,manual,,,\n")
with open(os.path.join(school, "curriculum.csv"), "w", encoding="utf-8") as f:
    f.write("course,week,date,topic,readings,lecture_ref,deliverable,prepared,notes\n")
with open(os.path.join(school, "grades.csv"), "w", encoding="utf-8") as f:
    f.write("course,component,item,score,out_of,date,notes\n")
with open(os.path.join(school, "review-log.csv"), "w", encoding="utf-8") as f:
    f.write("course,topic,last_reviewed,confidence,next_due,times_reviewed,notes\n")
school_data.init(tmp)


def _rows(name):
    with open(os.path.join(school, name), newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


# ---- 1. apply_canvas_status: the nightly flip ----------------------------------

print("apply_canvas_status")
spec = importlib.util.spec_from_file_location(
    "apply_canvas_status",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "scripts", "apply_canvas_status.py"))
acs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(acs)
acs.CSV_PATH = os.path.join(school, "assignments.csv")
sub_file = os.path.join(tmp, "ACCT100.json")
with open(sub_file, "w", encoding="utf-8") as f:
    f.write('while(1);' + json.dumps([
        {"assignment_id": 11, "workflow_state": "graded", "score": 9,
         "submitted_at": "2026-09-02T14:00:00Z", "assignment": {"points_possible": 10}},
        {"assignment_id": 12, "workflow_state": "submitted", "submitted_at": "2026-09-01T23:30:00Z"},
        {"assignment_id": 13, "workflow_state": "unsubmitted", "missing": True},
        {"assignment_id": 14, "workflow_state": "unsubmitted", "excused": True},
        {"assignment_id": 99, "workflow_state": "graded", "score": 5},
    ]))
dry = acs.apply([sub_file], dry_run=True)
check("dry run flips nothing on disk",
      dry["dry_run"] and all(r["status"] == "open" for r in _rows("assignments.csv")[:4]))
res = acs.apply([sub_file])
rows = {r["source"]: r for r in _rows("assignments.csv")}
check("graded submission → graded with score/points", rows["canvas:event-assignment-11"]["status"] == "graded"
      and rows["canvas:event-assignment-11"]["grade"] == "9/10")
check("submitted_at lands as a LOCAL date", rows["canvas:event-assignment-11"]["submitted_date"] == "2026-09-02")
check("submitted (ungraded) → submitted", rows["canvas:event-assignment-12"]["status"] == "submitted")
check("unsubmitted/missing stays open", rows["canvas:event-assignment-13"]["status"] == "open")
check("excused → done", rows["canvas:event-assignment-14"]["status"] == "done")
check("unknown assignment ids are ignored", res["matched_rows"] == 4 and len(res["changed"]) == 3)
check("a backup was written before the first change",
      any(n.startswith("assignments.csv.bak-pre-canvas-status-") for n in os.listdir(school)))
again = acs.apply([sub_file])
check("second run is idempotent", again["changed"] == [])

# ---- 2. school_grades reads the grade column the sync just wrote --------------

print("school_grades from assignments.csv")
sc = school_grades.scores("ACCT100")
apq = [s for s in sc if s.get("source") == "assignments.csv"]
check("the graded APQ counts as a score", len(apq) == 1 and apq[0]["pct"] == 90.0)
check("…filed under the APQ rubric component", "APQ" in apq[0]["component"])
proj = school_grades.project("ACCT100")
check("projection sees one score", proj.get("n_scores") == 1 and proj.get("current_pct") == 90.0)
check("a submitted-but-ungraded row adds no score", not any("HW - Day 2" in s["item"] for s in sc))

# ---- 3. school_state: phone decisions → state rows → vault ----------------------

print("school_state")
sb = FakeSB()
intake.supabase = sb
cur = school_state.record_prepared({"ACCT100": "2026-09-08", "econ103": "2026-09-04"})
check("record_prepared stores per course, upper-cased", cur.get("ACCT100") == "2026-09-08" and cur.get("ECON103") == "2026-09-04")
cur = school_state.record_prepared({"ACCT100": "2026-09-05"})
check("an earlier date never moves prepared_through backwards", cur.get("ACCT100") == "2026-09-08")
school_state.record_done("canvas:event-assignment-13", "submitted", "2026-09-03")
msg = school_state.apply_to_vault(tmp)
courses = {r["course"]: r for r in _rows("courses.csv")}
check("prepared_through applied to courses.csv", courses["ACCT100"]["prepared_through"] == "2026-09-08"
      and courses["ECON103"]["prepared_through"] == "2026-09-04")
rows = {r["source"]: r for r in _rows("assignments.csv")}
check("done flag closes the assignment row with its date",
      rows["canvas:event-assignment-13"]["status"] == "submitted"
      and rows["canvas:event-assignment-13"]["submitted_date"] == "2026-09-03")
check("apply reports what it did", msg.startswith("applied ") and "ACCT100" in msg)
check("second apply is a no-op", school_state.apply_to_vault(tmp) == "nothing to apply")
courses = {r["course"]: r for r in _rows("courses.csv")}
check("a hand-edited later date in the vault is left alone",
      courses["ACCT100"]["prepared_through"] == "2026-09-08")

# ---- 4. outbox.sweep_sent: drafts that left Drafts close themselves ------------

print("outbox.sweep_sent")
sb2 = FakeSB()
outbox.init(sb2)
a = outbox.add("email_draft", "Reply to advisor", ref="gmail:school:draft-A", account="school")
b = outbox.add("email_draft", "Reply to coach", ref="gmail:school:draft-B", account="school")
c = outbox.add("email_draft", "Wave 1 email", ref="gmail:studio:draft-C", account="studio")
d = outbox.add("file", "Sign the roommate agreement", ref="", account="")
calls = []


def ids(account):
    calls.append(account)
    return {"school": {"draft-B"}, "studio": None}[account]


closed = outbox.sweep_sent(ids)
open_ids = {it["id"] for it in outbox.open_items()}
check("a draft gone from Drafts closes its item", a in closed and a not in open_ids)
check("a draft still in Drafts stays open", b not in closed and b in open_ids)
check("an unknown listing (None) closes nothing", c not in closed and c in open_ids)
check("non-email items are untouched", d in open_ids)
check("one listing call per account", sorted(calls) == ["school", "studio"])
check("a throwing lister closes nothing", outbox.sweep_sent(lambda acc: 1 / 0) == [])

# ---- 5. school_data helpers the phone surfaces depend on ---------------------

print("school_data: loader, session targets, classes line, runway, weekend plan")
school_data.init(tmp)
os.makedirs(os.path.join(school, "Courses"), exist_ok=True)   # the real vault has this folder
check("_load accepts the bare name too, even with a Courses/ folder shadowing it",
      school_data._load("courses") and school_data._load("courses") == school_data._load("courses.csv"))
_today = date.today()
_tue = _today - timedelta(days=(_today.weekday() - 1) % 7)          # a Tuesday: ACCT + ECON meet
from datetime import datetime as _dt, timedelta as _td
tg = school_data.session_targets(block_start=_dt(_tue.year, _tue.month, _tue.day, 13, 0), for_date=_tue)
check("a 1 PM Tuesday block is told to review the classes that already met",
      any(t.startswith("review ACCT100") for t in tg) and any(t.startswith("review ECON103") for t in tg))
tg_early = school_data.session_targets(block_start=_dt(_tue.year, _tue.month, _tue.day, 8, 0), for_date=_tue)
check("…but not classes that haven't happened yet", not any(t.startswith("review ") for t in tg_early))
cl = school_data.classes_line(_tue)
check("classes line names rooms in start order", cl.startswith("ACCT 10:00 PBL 201") and "ECON 11:30 PBL 201" in cl)
check("classes line is empty on a day with no classes", school_data.classes_line(_tue + _td(days=4)) == "")
# exam runway: 5 rows since term start, exam in 4 days, one day away
mon = _today - _td(days=_today.weekday())
with open(os.path.join(school, "curriculum.csv"), "w", encoding="utf-8") as f:
    f.write("course,week,date,topic,readings,lecture_ref,deliverable,prepared,notes\n")
    for i in range(5):
        f.write(f"MATH120,1,{(_today - _td(days=10 - 2 * i)).isoformat()},§1.{i + 1} (title TBC from textbook),"
                f"\"Zill §1.{i + 1} — suggested: #{i + 1},{i + 3}\",,,,\n")
    f.write(f"MATH120,2,{(_today - _td(days=1)).isoformat()},(deadline — no class),,,,,\n")
    f.write(f"MATH120,2,{(_today + _td(days=4)).isoformat()},TEST 1 (in class),,,Test 1,,\n")
with open(os.path.join(school, "courses.csv"), "a", encoding="utf-8") as f:
    f.write(f"MATH120,MATH 120,Elem Functions,,MWF 9:20-10:10AM Sears 439,Fall 2026,14,{_today.isoformat()},imported,\n")
cur = school_data._load("curriculum.csv")
rw = school_data.exam_runway("MATH120", cur, _today + _td(days=4), _today, away={_today + _td(days=1)})
check("runway spreads five syllabus rows over the three usable days",
      rw and rw["left"] == 3 and sum(len(x) for x in [rw["today"]]) >= 1 and rw["next"])
check("runway labels carry the section's own suggested list", any("suggested" not in x and "#1,3" in x for x in rw["today"]))
check("paren rows and the exam itself are never runway items",
      not any("deadline" in x or "TEST" in x for x in rw["today"]))
check("no runway once the exam has passed", school_data.exam_runway("MATH120", cur, _today - _td(days=1), _today) is None)
plan = school_data.study_plan_data(_today)
ex = [e for e in plan["exams"] if e["course"] == "MATH120"]
check("study plan attaches the runway to the exam entry", ex and ex[0].get("runway") and "today" in ex[0]["runway"])
check("…and prints it", any("runway" in ln for ln in plan["lines"]))
# weekend plan parser
fri = _today - _td(days=(_today.weekday() - 4) % 7)
with open(os.path.join(school, "Weekend Map — Fall 2026.md"), "w", encoding="utf-8") as f:
    f.write("# Weekend Map — Fall 2026\n\n## W1 · Fri Jan 2 – Sun Jan 4 — old\n**Clear the board (by Sun):** nothing\n\n"
            f"## W9 · Fri {fri.strftime('%b')} {fri.day} – Sun {(fri + _td(days=2)).strftime('%b')} {(fri + _td(days=2)).day} — the one\n"
            "**Clear the board (by Sun):** ACCT HW Day 4 (Mon) · CSDS Lab 1 (Tue)\n"
            "**Front-load (runway ≤14d):** MATH Test 1 Fri\n"
            "**Get-ahead focus:**\n- MATH120: finish the Ch 1 list\n- ACCT100: Day 5 reading\n- ECON103: skim Ch 4\n"
            "**Watch:**\n- Labor Day\n"
            "**Cut 2026-01-01:**\n- old cut line\n"
            "**Cut 2026-01-08:**\n- newest cut line one\n- newest cut line two\n\n## W10 · Fri Dec 4 – Sun Dec 6 — later\n")
wp = school_data.weekend_plan(fri + _td(days=1))
check("weekend plan finds this weekend's section by its Friday date",
      wp and wp[0].startswith("Clear: ACCT HW Day 4"))
check("the newest Cut block wins and precedes the get-ahead bullets",
      "newest cut line one" in wp[1] and not any("old cut" in x for x in wp) and any("MATH120: finish" in x for x in wp))
check("a weekend with no section returns nothing", school_data.weekend_plan(fri + _td(days=21)) == [])

# ---- 6. derived streak evidence + grade weight on orders ----------------------

print("daily_orders: derived pillars, item weight")
import daily_orders
intake.supabase = FakeSB()
daily_orders.init(intake.supabase)
with open(os.path.join(school, "assignments.csv"), "w", encoding="utf-8") as f:
    f.write("course,title,type,due_date,weight_pct,est_hours,actual_hours,status,topic,source,submitted_date,grade,notes\n"
            f"ACCT100,Day 4 APQ - Financial Statement Analysis,assignment,{(_today - _td(days=1)).isoformat()}T10:00,,,,graded,,canvas:1,,9/10,\n"
            f"ACCT100,HW - Day 3,homework,{(_today - _td(days=2)).isoformat()},,,,open,,canvas:2,,,\n"
            f"ACCT100,Day 9 Reading,reading,{(_today + _td(days=3)).isoformat()}T10:00,,,,open,,canvas:3,,,\n")
d1 = daily_orders._derived_pillars(_today - _td(days=1))
d2 = daily_orders._derived_pillars(_today - _td(days=2))
d3 = daily_orders._derived_pillars(_today - _td(days=5))
check("a day whose dues are all closed counts as a school day", d1.get("school") is True)
check("a passed day with an open due counts against school", d2.get("school") is False)
check("a day with no evidence stays absent", "school" not in d3)
check("an APQ order carries its grade weight",
      daily_orders._item_weight("ACCT100", "Day 4 APQ - Financial Statement Analysis (assignment)") > 0.3)
check("a reading with no rubric component carries none", daily_orders._item_weight("ACCT100", "Day 9 Reading (reading)") == 0.0)

# ---- 7. announcements → intake (injected recorder) ----------------------------

print("apply_canvas_announcements")
spec2 = importlib.util.spec_from_file_location(
    "apply_canvas_announcements",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "scripts", "apply_canvas_announcements.py"))
aca = importlib.util.module_from_spec(spec2)
spec2.loader.exec_module(aca)
aca.SEEN_PATH = os.path.join(tmp, "seen.json")
ann_file = os.path.join(tmp, "announcements.json")
with open(ann_file, "w", encoding="utf-8") as f:
    f.write('while(1);' + json.dumps([
        {"id": 501, "context_code": "course_53812", "title": "Bring your index card Tuesday",
         "message": "<p>Finish the last <b>three</b> problems on the Day 2 handout.<br>See you Tuesday.</p>",
         "posted_at": "2026-09-02T17:00:00Z", "html_url": "https://canvas.case.edu/x"},
        {"id": 502, "context_code": "course_54158", "title": "Room change", "message": "<p>Sears 439</p>",
         "posted_at": "2026-09-02T17:05:00Z"},
    ]))
calls = []


def rec(source, ref, sender, ts, text):
    calls.append((source, ref, sender, text))
    return {"recorded": True}


res = aca.apply([ann_file], record=rec)
check("each new announcement is handed to the intake extractor once", res["new"] == 2 and len(calls) == 2)
check("HTML is stripped and the course is named", calls[0][2].startswith("ACCT100 announcement")
      and "three problems" in calls[0][3] and "<" not in calls[0][3])
check("source/ref shape lets intake dedupe", calls[0][0] == "canvas" and calls[0][1] == "ann-501")
res2 = aca.apply([ann_file], record=rec)
check("a second run skips everything already seen", res2["new"] == 0 and res2["skipped"] == 2 and len(calls) == 2)

shutil.rmtree(tmp, ignore_errors=True)
print(f"\n{sum(_results)}/{len(_results)} passed")
raise SystemExit(0 if all(_results) else 1)
