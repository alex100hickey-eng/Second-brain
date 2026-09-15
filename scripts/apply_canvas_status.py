#!/usr/bin/env python3
"""
apply_canvas_status.py — flip assignments.csv rows to submitted/graded from Canvas,
and stamp the rows Canvas does not grade so nothing treats them as deadlines.

Nothing marked a CSV row done on its own: the ICS feed carries deadlines but never
submission state, and CWRU forbids API tokens, so every "submitted" flag so far was a
human edit. This closes the loop without a token: the nightly canvas-status-sync
scheduled task navigates the logged-in Browser pane to each course's

    /api/v1/courses/<id>/students/submissions?student_ids[]=self&per_page=100

saves the JSON it sees under .canvas_status/<CODE>.json, and runs this script. Each
submission object carries assignment_id, workflow_state (unsubmitted | submitted |
graded | pending_review), submitted_at, score, late, missing, excused. The CSV keys
its Canvas rows by the ICS UID, "canvas:event-assignment-<assignment_id>".

The same task also saves each course's assignment list,

    /api/v1/courses/<id>/assignments?per_page=100   ->  .canvas_status/<CODE>.assignments.json

(a bare list, or {"items": [...]} — the projection the task writes: id, name,
grading_type, points_possible, submission_types). Any mix of both file kinds may be
passed; a submission that embeds its assignment (include[]=assignment) counts too.

Grading facts drive ONE more column. ACCT100's "Day N Reading: Ch. X LO Y" rows are
grading_type "not_graded" in Canvas (verified 2026-09-02/03): WileyPlus section
links, no points, nothing to submit — the APQ is the only graded reading check. The
ICS feed cannot see that, so the brief listed "Day 3 Reading" as OVERDUE and the
ranked day nagged it. Such a row gets weight_pct "0" (when the cell is blank). That
0 is the shared "ungraded prep" marker: school_status.py and school_data.py keep
these rows out of OVERDUE / DO NEXT / due-soon / lapsed / ranked orders and show at
most a one-line prep hint for the next class. ECON103's "Reading N" rows are graded
(5 pts, late = zero) and are never touched. A human-typed weight is never
overwritten; a blank weight means unknown, not 0.

    python3 scripts/apply_canvas_status.py .canvas_status/*.json [--dry-run]

Reversible: a .bak-pre-canvas-status-<date> copy is written before the first change.
Only ever CLOSES rows (open -> submitted/graded) and fills blank cells; never
reopens, never touches human-created rows, never edits dates or titles.
"""

import csv
import json
import os
import re
import shutil
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

LOCAL_TZ = ZoneInfo("America/New_York")
VAULT = os.environ.get("OBSIDIAN_VAULT_PATH") or os.path.expanduser(
    "~/Library/Mobile Documents/com~apple~CloudDocs/Obsidian/Second brain")
CSV_PATH = os.path.join(VAULT, "School", "assignments.csv")
DONE = {"submitted", "graded", "done", "complete", "completed"}
CLOSED_STATES = {"submitted", "graded", "pending_review"}
NOT_GRADED = "not_graded"
# The weight_pct value that marks "Canvas does not grade this". Read by
# school_status.ungraded / school_data.ungraded — change all three together.
UNGRADED_WEIGHT = "0"


def _load_objects(path: str) -> list:
    """Canvas prefixes JSON with `while(1);` when it is rendered in a tab; strip it.
    Accepts a bare list or a dict wrapping one ({"submissions"|"assignments"|
    "items"|"data": [...]})."""
    text = open(path, encoding="utf-8", errors="replace").read()
    text = re.sub(r"^\s*while\s*\(\s*1\s*\)\s*;", "", text).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # get_page_text sometimes wraps the body in an HTML pre element; find the array
        m = re.search(r"(\[\s*\{.*\}\s*\])", text, re.S)
        data = json.loads(m.group(1)) if m else []
    if isinstance(data, dict):
        data = (data.get("submissions") or data.get("assignments")
                or data.get("items") or data.get("data") or [])
    return [o for o in data if isinstance(o, dict)]


def load_submissions(path: str) -> list:
    return [s for s in _load_objects(path) if s.get("assignment_id")]


def load_assignments(path: str) -> dict:
    """{assignment_id: assignment object} — from an assignments file, or from the
    `assignment` each submission embeds when fetched with include[]=assignment.
    Announcement/other objects (an id but no grading_type) are ignored."""
    out = {}
    for o in _load_objects(path):
        if o.get("assignment_id"):
            a = o.get("assignment")
            if isinstance(a, dict) and "grading_type" in a:
                out[str(o["assignment_id"])] = a
        elif o.get("id") is not None and "grading_type" in o:
            out[str(o["id"])] = o
    return out


def is_not_graded(a) -> bool:
    """Canvas's own word for it: grading_type "not_graded" (submission_types is
    then ["not_graded"] too). points_possible 0 is NOT the same thing — the
    Respondus check and the 10-K milestone are 0-point rows that are required."""
    if not isinstance(a, dict):
        return False
    if (a.get("grading_type") or "").strip().lower() == NOT_GRADED:
        return True
    st = a.get("submission_types")
    return isinstance(st, list) and [str(x).lower() for x in st] == [NOT_GRADED]


def local_day(iso: str) -> str:
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.astimezone(LOCAL_TZ).strftime("%Y-%m-%d")
    except ValueError:
        return iso[:10]


def apply(sub_files: list, dry_run: bool = False) -> dict:
    subs, asg = {}, {}
    for f in sub_files:
        for s in load_submissions(f):
            subs[str(s["assignment_id"])] = s
        asg.update(load_assignments(f))
    with open(CSV_PATH, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        cols, rows = reader.fieldnames or [], list(reader)
    changes, seen, stamped = [], 0, 0
    for r in rows:
        src = (r.get("source") or "").strip()
        m = re.match(r"canvas:event-assignment-(\d+)$", src)
        if not m:
            continue
        aid = m.group(1)
        # Grading fact first, independent of submission state: a not-graded row
        # stays "open" forever in Canvas terms, and that is fine once it is
        # marked prep. Blank cells only — a typed weight is Alex's.
        a = asg.get(aid)
        if (a is not None and "weight_pct" in cols and is_not_graded(a)
                and not (r.get("weight_pct") or "").strip()):
            r["weight_pct"] = UNGRADED_WEIGHT
            stamped += 1
            changes.append(f"{r.get('course')}: {(r.get('title') or '')[:48]} "
                           f"-> weight_pct {UNGRADED_WEIGHT} (Canvas: not graded)")
        s = subs.get(aid)
        if s is None:
            continue
        seen += 1
        if (r.get("status") or "").strip().lower() in DONE:
            continue
        state = (s.get("workflow_state") or "").strip().lower()
        scored = s.get("score") is not None
        if s.get("excused"):
            new_status = "done"
        elif state == "graded" or scored:
            new_status = "graded"
        elif state in CLOSED_STATES:
            new_status = "submitted"
        else:
            continue
        r["status"] = new_status
        if "submitted_date" in cols and not (r.get("submitted_date") or "").strip():
            r["submitted_date"] = local_day(s.get("submitted_at") or s.get("graded_at") or "")
        if "grade" in cols and scored and not (r.get("grade") or "").strip():
            pts = (s.get("points_possible")
                   or (s.get("assignment") or {}).get("points_possible")
                   or (a or {}).get("points_possible"))
            r["grade"] = f"{s['score']:g}/{pts:g}" if pts else f"{s['score']:g}"
        changes.append(f"{r.get('course')}: {r.get('title')[:48]} -> {new_status}")
    if changes and not dry_run:
        stamp = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")
        bak = f"{CSV_PATH}.bak-pre-canvas-status-{stamp}"
        if not os.path.exists(bak):
            shutil.copy2(CSV_PATH, bak)
        tmp = CSV_PATH + ".tmp"
        with open(tmp, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            for r in rows:
                w.writerow({c: (r.get(c) or "") for c in cols})
        os.replace(tmp, CSV_PATH)
    return {"submissions": len(subs), "assignments": len(asg), "matched_rows": seen,
            "stamped_ungraded": stamped, "changed": changes, "dry_run": dry_run}


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        sys.exit(__doc__)
    res = apply(args, dry_run="--dry-run" in sys.argv)
    print(json.dumps({k: v for k, v in res.items() if k != "changed"}))
    for c in res["changed"]:
        print("  ", c)
    if not res["changed"]:
        print("   nothing to flip")
