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
from datetime import date

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

shutil.rmtree(tmp, ignore_errors=True)
print(f"\n{sum(_results)}/{len(_results)} passed")
raise SystemExit(0 if all(_results) else 1)
