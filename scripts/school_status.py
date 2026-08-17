#!/usr/bin/env python3
"""The daily school brief: what's late, what's coming, whether you're actually
ahead of each class, and what to study next.

The whole point of this system is staying *ahead* of the curriculum, so pace is
computed, not guessed. Each course declares a `lead_target_days` (how far ahead
of the lecture you intend to be) and a `prepared_through` date (how far you have
actually prepared). Lead = prepared_through - today. If lead < target, the course
is flagged, with the specific topics you'd need to cover to close the gap.

Usage:
    python3 scripts/school_status.py            # full brief
    python3 scripts/school_status.py --days 14  # widen the deadline window
    python3 scripts/school_status.py --course BIO201

Read-only. It never edits a CSV — it tells you what to do, you decide.
"""
import argparse
import csv
import os
import sys
from datetime import datetime, timedelta

VAULT = os.path.expanduser(
    "~/Library/Mobile Documents/com~apple~CloudDocs/Obsidian/Second brain")
SCHOOL = os.environ.get("SCHOOL_DIR") or os.path.join(VAULT, "School")

# confidence (1 = blank stare, 5 = could teach it) -> days until it needs review
SPACING = {1: 1, 2: 3, 3: 7, 4: 16, 5: 35}
DONE = {"submitted", "graded", "done", "complete", "completed"}
EXAM_TYPES = {"exam", "quiz", "test", "midterm", "final"}
# Successive relearning (Rawson & Dunlosky): ~3 spaced recall sessions per topic
# before an assessment; more than that shows sharply diminishing returns.
RELEARN_TARGET = 3
# Cepeda et al. 2008: the optimal first review gap is ~10-20% of the time until
# the test. Used to anchor review scheduling to the exam it actually serves.
EXAM_GAP_FRAC = 0.15

C = {"red": "\033[31m", "yellow": "\033[33m", "green": "\033[32m",
     "bold": "\033[1m", "dim": "\033[2m", "off": "\033[0m"}


def color(s, c):
    return f"{C[c]}{s}{C['off']}" if sys.stdout.isatty() else s


def load(name):
    path = os.path.join(SCHOOL, name)
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return [r for r in csv.DictReader(f) if any((v or "").strip() for v in r.values())]


def date(s):
    """Parse the date formats a human actually types — plus the timed ISO form
    canvas_sync writes ('2026-08-27T23:30'), which counts as its calendar day."""
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


def section(title):
    print(f"\n{color(title, 'bold')}")
    print(color("─" * len(title), "dim"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7,
                    help="deadline lookahead window (default 7)")
    ap.add_argument("--course", help="limit to one course code")
    args = ap.parse_args()

    today = datetime.now().date()
    horizon = today + timedelta(days=args.days)

    courses = load("courses.csv")
    curriculum = load("curriculum.csv")
    assignments = load("assignments.csv")
    reviews = load("review-log.csv")

    if args.course:
        k = args.course.lower()
        keep = lambda r: k in (r.get("course", "") or "").lower()   # noqa: E731
        courses = [r for r in courses if keep(r)]
        curriculum = [r for r in curriculum if keep(r)]
        assignments = [r for r in assignments if keep(r)]
        reviews = [r for r in reviews if keep(r)]

    print(color(f"\nSCHOOL BRIEF — {today:%A, %B %d, %Y}", "bold"))

    if not courses and not assignments and not curriculum:
        print("\nNothing loaded yet — the skeleton is built but empty.")
        print("Start here:")
        print("  1. Add a row per class to     School/courses.csv")
        print("  2. Drop syllabus text into    School/Intake/paste-here.md")
        print("  3. Run                        python3 scripts/syllabus_intake.py")
        print("\nSee School/README.md for the full loop.")
        return 0

    # ---------- what is actually late ----------
    open_work = [a for a in assignments
                 if (a.get("status", "") or "").strip().lower() not in DONE]
    overdue = sorted(
        [(date(a["due_date"]), a) for a in open_work
         if date(a.get("due_date")) and date(a["due_date"]) < today],
        key=lambda t: t[0])
    if overdue:
        section(f"OVERDUE ({len(overdue)})")
        for d, a in overdue:
            late = (today - d).days
            print(f"  {color('!', 'red')} {a['course']:<10} {a['title'][:46]:<46} "
                  f"{color(f'{late}d late', 'red')}  ({d:%b %d})")

    # ---------- what is coming ----------
    soon = sorted(
        [(date(a["due_date"]), a) for a in open_work
         if date(a.get("due_date")) and today <= date(a["due_date"]) <= horizon],
        key=lambda t: t[0])
    section(f"DUE IN THE NEXT {args.days} DAYS ({len(soon)})")
    if not soon:
        print("  nothing scheduled")
    for d, a in soon:
        days = (d - today).days
        when = "TODAY" if days == 0 else ("tomorrow" if days == 1 else f"{days}d")
        hrs = (a.get("est_hours") or "").strip()
        wt = (a.get("weight_pct") or "").strip()
        tail = "  ".join(x for x in [f"~{hrs}h" if hrs else "",
                                     f"{wt}% of grade" if wt else ""] if x)
        print(f"  {color(when, 'yellow' if days <= 2 else 'green'):<18} "
              f"{a['course']:<10} {a['title'][:44]:<44} {color(tail, 'dim')}")

    # Planning-fallacy correction: people reliably underestimate task time.
    # Until there's personal data, buffer estimates 1.5x; once actual_hours
    # accumulate, use the measured ratio instead.
    ratios = []
    for a in assignments:
        try:
            est = float((a.get("est_hours") or "").strip())
            act = float((a.get("actual_hours") or "").strip())
            if est > 0 and act > 0:
                ratios.append(act / est)
        except ValueError:
            pass
    factor = (sum(ratios) / len(ratios)) if len(ratios) >= 3 else 1.5
    est_load = 0.0
    for _, a in soon:
        try:
            est_load += float((a.get("est_hours") or "").strip())
        except ValueError:
            pass
    if est_load:
        basis = (f"your measured ratio over {len(ratios)} tasks"
                 if len(ratios) >= 3 else "default 1.5x buffer")
        print(f"\n  {color(f'workload: ~{est_load:.0f}h estimated — plan for '
                           f'~{est_load * factor:.0f}h ({basis})', 'dim')}")

    # ---------- the part that matters: are you ahead? ----------
    section("PACE — are you ahead of each class?")
    if not courses:
        print("  no courses.csv rows yet")
    # lead_target_days = 0 means "not a prep-driven course" — the varsity-athletics
    # PHED row and the AIQS administrative shell both live in courses.csv because
    # they're real enrollments, but reporting a reading-lead figure for basketball
    # is noise that makes the real six harder to scan.
    for c in [c for c in courses
              if int((c.get("lead_target_days") or "0").strip() or 0) > 0]:
        code = c.get("course") or c.get("code") or "?"
        target = int((c.get("lead_target_days") or "0").strip() or 0)
        prepared = date(c.get("prepared_through"))
        if not prepared:
            print(f"  {code:<10} {color('prepared_through not set', 'dim')} "
                  f"(target: {target}d ahead)")
            continue
        lead = (prepared - today).days
        if lead >= target:
            state = color(f"{lead:+d}d — on plan", "green")
        elif lead >= 0:
            state = color(f"{lead:+d}d — ahead, but under your {target}d target", "yellow")
        else:
            state = color(f"{lead:+d}d — BEHIND the lecture", "red")
        print(f"  {code:<10} {state}")

        # the concrete gap: topics between prepared_through and the target line
        gap_end = today + timedelta(days=target)
        gap = [r for r in curriculum
               if (r.get("course") or "").lower() == (code or "").lower()
               and date(r.get("date"))
               and prepared < date(r["date"]) <= gap_end
               and (r.get("prepared", "") or "").strip().lower() not in ("y", "yes", "1", "true")]
        for r in gap[:4]:
            print(f"       {color('→', 'dim')} {date(r['date']):%b %d}  {r.get('topic','')[:52]}")
        if len(gap) > 4:
            print(f"       {color(f'… +{len(gap)-4} more topics', 'dim')}")

    # next upcoming exam per course — reviews get anchored to it
    next_exam = {}
    for a in assignments:
        if (a.get("type", "") or "").strip().lower() not in EXAM_TYPES:
            continue
        if (a.get("status", "") or "").strip().lower() in DONE:
            continue
        d = date(a.get("due_date"))
        if d and d >= today:
            key = (a.get("course") or "").lower()
            if key not in next_exam or d < next_exam[key][0]:
                next_exam[key] = (d, a)

    # ---------- spaced retrieval, anchored to the exam it serves ----------
    due_reviews = []
    for r in reviews:
        nd = date(r.get("next_due"))
        if not nd:
            last = date(r.get("last_reviewed"))
            try:
                conf = int((r.get("confidence") or "3").strip())
            except ValueError:
                conf = 3
            nd = last + timedelta(days=SPACING.get(conf, 7)) if last else None
        # An interval that lands after the exam it serves is useless — pull it in.
        # With an exam ahead, the gap should be a fraction of time-to-test
        # (Cepeda), never "after the test".
        exam = next_exam.get((r.get("course") or "").lower())
        if nd and exam:
            exam_date = exam[0]
            if nd >= exam_date:
                frac_gap = max(1, int((exam_date - today).days * EXAM_GAP_FRAC))
                nd = min(exam_date - timedelta(days=1),
                         today + timedelta(days=frac_gap))
        if nd and nd <= today:
            due_reviews.append((nd, r))
    due_reviews.sort(key=lambda t: t[0])
    section(f"REVIEW DUE ({len(due_reviews)})")
    if not due_reviews:
        print("  nothing due — log topics in review-log.csv as you learn them")
    for nd, r in due_reviews[:10]:
        conf = (r.get("confidence") or "?").strip()
        od = (today - nd).days
        print(f"  {r.get('course',''):<10} {r.get('topic','')[:44]:<44} "
              f"{color(f'confidence {conf}/5', 'dim')}"
              f"{color(f'  {od}d overdue', 'yellow') if od > 0 else ''}")

    # ---------- exam readiness: 3 spaced recalls per topic, done before the day ----------
    horizon_exams = sorted(
        [(d, a) for d, a in next_exam.values() if (d - today).days <= 21],
        key=lambda t: t[0])
    if horizon_exams:
        section("EXAM READINESS (target: 3 spaced recalls per topic)")
        for d, a in horizon_exams:
            days_out = (d - today).days
            code = a.get("course", "?")
            when = "TODAY" if days_out == 0 else f"in {days_out}d"
            print(f"  {color(code, 'bold')} — {a.get('title','')[:40]}  "
                  f"{color(when, 'red' if days_out <= 3 else 'yellow')}")
            # every topic this course has logged, vs the relearning target
            topics = [r for r in reviews
                      if (r.get("course") or "").lower() == code.lower()]
            if not topics:
                print(f"       {color('no topics logged in review-log.csv — '
                                      'readiness unknown', 'red')}")
                continue
            behind_t, ok_t = [], 0
            for r in topics:
                try:
                    n = int((r.get("times_reviewed") or "0").strip() or 0)
                except ValueError:
                    n = 0
                if n >= RELEARN_TARGET:
                    ok_t += 1
                else:
                    behind_t.append((n, r.get("topic", "")))
            print(f"       {ok_t}/{len(topics)} topics at target")
            behind_t.sort(key=lambda t: t[0])
            for n, topic in behind_t[:5]:
                print(f"       {color('→', 'dim')} {topic[:48]:<48} "
                      f"{color(f'{n}/{RELEARN_TARGET} recalls', 'yellow')}")
            if len(behind_t) > 5:
                print(f"       {color(f'… +{len(behind_t)-5} more below target', 'dim')}")
            # is there still room to space the remaining recalls?
            need = max((RELEARN_TARGET - n) for n, _ in behind_t) if behind_t else 0
            if need and days_out < need * 2:
                print(f"       {color(f'⚠ {need} recalls left but only {days_out}d — '
                                      f'start today, space what you can', 'red')}")

    # ---------- one honest next action ----------
    section("DO NEXT")
    if overdue:
        d, a = overdue[0]
        print(f"  1. Clear the oldest overdue: {a['course']} — {a['title']}")
    elif soon:
        d, a = soon[0]
        hrs = (a.get("est_hours") or "?").strip()
        print(f"  1. {a['course']} — {a['title']} (due {d:%b %d}, ~{hrs}h)")
    else:
        print("  1. Nothing due — use the clear runway to extend your lead")
    behind = [c for c in courses
              if date(c.get("prepared_through"))
              and (date(c["prepared_through"]) - today).days
              < int((c.get("lead_target_days") or "0").strip() or 0)]
    if behind:
        print(f"  2. Close the pace gap in: "
              f"{', '.join(c.get('course','?') for c in behind[:3])}")
    if due_reviews:
        print(f"  3. Retrieval practice: {due_reviews[0][1].get('topic','')}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
