#!/usr/bin/env python3
"""
apply_canvas_status.py — flip assignments.csv rows to submitted/graded from Canvas.

Nothing marked a CSV row done on its own: the ICS feed carries deadlines but never
submission state, and CWRU forbids API tokens, so every "submitted" flag so far was a
human edit. This closes the loop without a token: the nightly canvas-status-sync
scheduled task navigates the logged-in Browser pane to each course's

    /api/v1/courses/<id>/students/submissions?student_ids[]=self&per_page=100

saves the JSON it sees under .canvas_status/<CODE>.json, and runs this script. Each
submission object carries assignment_id, workflow_state (unsubmitted | submitted |
graded | pending_review), submitted_at, score, late, missing, excused. The CSV keys
its Canvas rows by the ICS UID, "canvas:event-assignment-<assignment_id>".

    python3 scripts/apply_canvas_status.py .canvas_status/*.json [--dry-run]

Reversible: a .bak-pre-canvas-status-<date> copy is written before the first change.
Only ever CLOSES rows (open -> submitted/graded); never reopens, never touches
human-created rows, never edits dates or titles.
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


def load_submissions(path: str) -> list:
    """Canvas prefixes JSON with `while(1);` when it is rendered in a tab; strip it.
    Accepts a bare list or {"submissions": [...]}."""
    text = open(path, encoding="utf-8", errors="replace").read()
    text = re.sub(r"^\s*while\s*\(\s*1\s*\)\s*;", "", text).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # get_page_text sometimes wraps the body in an HTML pre element; find the array
        m = re.search(r"(\[\s*\{.*\}\s*\])", text, re.S)
        data = json.loads(m.group(1)) if m else []
    if isinstance(data, dict):
        data = data.get("submissions") or data.get("data") or []
    return [s for s in data if isinstance(s, dict) and s.get("assignment_id")]


def local_day(iso: str) -> str:
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.astimezone(LOCAL_TZ).strftime("%Y-%m-%d")
    except ValueError:
        return iso[:10]


def apply(sub_files: list, dry_run: bool = False) -> dict:
    subs = {}
    for f in sub_files:
        for s in load_submissions(f):
            subs[str(s["assignment_id"])] = s
    with open(CSV_PATH, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        cols, rows = reader.fieldnames or [], list(reader)
    changes, seen = [], 0
    for r in rows:
        src = (r.get("source") or "").strip()
        m = re.match(r"canvas:event-assignment-(\d+)$", src)
        if not m:
            continue
        s = subs.get(m.group(1))
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
            pts = s.get("points_possible") or (s.get("assignment") or {}).get("points_possible")
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
    return {"submissions": len(subs), "matched_rows": seen, "changed": changes,
            "dry_run": dry_run}


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
