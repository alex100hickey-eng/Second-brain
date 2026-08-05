#!/usr/bin/env python3
"""Turn pasted syllabus / class-page text into proposed tracker rows.

There is no live connector to a school portal, so the reliable path is paste-in:
copy the schedule off the class page, drop it in School/Intake/paste-here.md,
run this. It finds dated lines, guesses which are graded work and which are just
topics, and writes a proposal file with ready-to-paste CSV rows.

It deliberately does NOT write assignments.csv or curriculum.csv. Date parsing
off free-form syllabus text is guesswork, and a silently wrong due date is worse
than no due date. You read the proposal, fix what's wrong, paste it in.

Usage:
    python3 scripts/syllabus_intake.py --course BIO201
    python3 scripts/syllabus_intake.py --course BIO201 --file some/other.txt --year 2026
"""
import argparse
import os
import re
import sys
from datetime import datetime

VAULT = os.path.expanduser(
    "~/Library/Mobile Documents/com~apple~CloudDocs/Obsidian/Second brain")
SCHOOL = os.environ.get("SCHOOL_DIR") or os.path.join(VAULT, "School")

MONTHS = ("jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec")
DATE_PATTERNS = [
    re.compile(rf"\b({MONTHS})[a-z]*\.?\s+(\d{{1,2}})(?:,?\s*(\d{{4}}))?\b", re.I),
    re.compile(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b"),
    re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b"),
]
MONTH_NUM = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}

# How a line announces graded work. ORDER MATTERS — most specific first.
# "Final paper" is a paper, not an exam, so paper/lab/project are tested before
# the generic exam words; "lab report" is a lab, so lab precedes paper.
WORK = [
    (r"\bquiz\b", "quiz"),
    (r"\b(problem set|pset|homework|hw\b)\b", "problem_set"),
    (r"\blab\b", "lab"),
    (r"\b(paper|essay|report|write-?up)\b", "paper"),
    (r"\b(project|presentation|demo)\b", "project"),
    (r"\b(midterm|mid-term|final exam|exam|test)\b", "exam"),
    (r"\bassignment\b", "problem_set"),
    (r"\b(reading|read|chapter|ch\.)\b", "reading"),
]
# noise to strip off the front of a title/topic once the date is parsed
LEAD_NOISE = re.compile(
    rf"^(week\s*\d+\s*)?[\s\-–—:|]*((?:{MONTHS})[a-z]*\.?\s+\d{{1,2}}(?:,?\s*\d{{4}})?"
    rf"|\d{{1,2}}/\d{{1,2}}(?:/\d{{2,4}})?|\d{{4}}-\d{{2}}-\d{{2}})?[\s\-–—:|]*",
    re.I)
WEEK_NO = re.compile(r"\bweek\s*(\d+)\b", re.I)
DUE_HINT = re.compile(r"\b(due|submit|deadline|hand in|turn in|by \d)\b", re.I)
WEIGHT = re.compile(r"(\d{1,3})\s*%")


def parse_date(line, default_year):
    for i, pat in enumerate(DATE_PATTERNS):
        m = pat.search(line)
        if not m:
            continue
        try:
            if i == 0:
                mon = MONTH_NUM[m.group(1)[:3].lower()]
                day, yr = int(m.group(2)), int(m.group(3) or default_year)
            elif i == 1:
                mon, day = int(m.group(1)), int(m.group(2))
                yr = int(m.group(3) or default_year)
                if yr < 100:
                    yr += 2000
            else:
                yr, mon, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
            return datetime(yr, mon, day).date(), bool(
                m.group(3) if i < 2 else True)
        except (ValueError, KeyError):
            continue
    return None, False


def classify(line):
    low = line.lower()
    for pat, kind in WORK:
        if re.search(pat, low):
            return kind
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--course", required=True, help="course code, e.g. BIO201")
    ap.add_argument("--file", default=os.path.join(SCHOOL, "Intake", "paste-here.md"))
    ap.add_argument("--year", type=int, default=datetime.now().year,
                    help="year to assume when the syllabus omits it")
    args = ap.parse_args()

    if not os.path.exists(args.file):
        print(f"no input at {args.file}\n"
              f"paste the class schedule into School/Intake/paste-here.md first")
        return 1

    text = open(args.file, encoding="utf-8", errors="replace").read()
    # drop HTML comment blocks whole — the instructions in paste-here.md contain
    # example dates, and a multi-line comment is not caught by a per-line check
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.S)
    lines = [l.strip() for l in text.splitlines() if l.strip()]

    work, topics, undated, guessed_year = [], [], [], 0
    for line in lines:
        if len(line) < 4 or line.startswith(("#", ">", "<!--")):
            continue
        d, had_year = parse_date(line, args.year)
        kind = classify(line)
        wk = WEEK_NO.search(line)
        clean = re.sub(r"\s{2,}", " ", line).strip(" -•\t|")
        # drop the leading "Week 3  Sep 14 —" scaffolding; the date is its own column
        label = LEAD_NOISE.sub("", clean, count=1).strip(" -•\t|:") or clean
        if not d:
            # graded work with no date still matters — you cannot plan around it
            if kind and kind != "reading":
                undated.append((kind, label))
            continue
        if not had_year:
            guessed_year += 1
        wt = WEIGHT.search(line)
        looks_graded = kind and kind != "reading" and (
            DUE_HINT.search(line) or kind in ("exam", "quiz"))
        if looks_graded:
            work.append({"date": d, "type": kind, "title": label[:90],
                         "weight": wt.group(1) if wt else ""})
        else:
            topics.append({"date": d, "topic": label[:90],
                           "week": wk.group(1) if wk else ""})

    work.sort(key=lambda r: r["date"])
    topics.sort(key=lambda r: r["date"])

    stamp = datetime.now().strftime("%Y-%m-%d")
    out = os.path.join(SCHOOL, "Intake", f"proposal-{args.course}-{stamp}.md")
    with open(out, "w", encoding="utf-8") as f:
        f.write(f"# Syllabus intake proposal — {args.course} ({stamp})\n\n"
                f"Parsed `{os.path.basename(args.file)}`: "
                f"**{len(work)} graded items**, **{len(topics)} schedule rows**.\n\n"
                "Nothing was written to the trackers. Check the dates against the "
                "syllabus — especially anything flagged below — then paste the rows "
                "into `assignments.csv` and `curriculum.csv`.\n\n")
        if guessed_year:
            f.write(f"> ⚠ {guessed_year} row(s) had no year in the text; "
                    f"assumed **{args.year}**. Verify anything near a term boundary.\n\n")
        if undated:
            f.write("## Graded work with no date found — add dates by hand\n\n")
            for kind, line in undated:
                f.write(f"- ({kind}) {line}\n")
            f.write("\n")

        f.write("## Rows for `assignments.csv`\n\n```csv\n")
        f.write("course,title,type,due_date,weight_pct,est_hours,actual_hours,status,topic,source,submitted_date,grade,notes\n")
        for r in work:
            title = r["title"].replace(",", ";")
            f.write(f"{args.course},{title},{r['type']},{r['date']},"
                    f"{r['weight']},,,not_started,,syllabus,,,\n")
        f.write("```\n\n## Rows for `curriculum.csv`\n\n```csv\n")
        f.write("course,week,date,topic,readings,lecture_ref,deliverable,prepared,notes\n")
        for i, r in enumerate(topics, 1):
            topic = r["topic"].replace(",", ";")
            f.write(f"{args.course},{r.get('week') or i},{r['date']},{topic},,,,,\n")
        f.write("```\n")

    print(f"{len(work)} graded items, {len(topics)} schedule rows, "
          f"{len(undated)} undated -> {out}")
    if guessed_year:
        print(f"  ⚠ assumed year {args.year} on {guessed_year} row(s) — verify")
    print("review it, then paste the rows into the trackers")
    return 0


if __name__ == "__main__":
    sys.exit(main())
