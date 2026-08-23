"""
canvas_sync.py — Canvas deadlines into the school system, via the personal ICS feed.

Why ICS and not the Canvas REST API: CWRU's Canvas admins have disabled student
self-service access tokens (verified 2026-08-16 — the settings page says so outright).
The personal calendar feed is the sanctioned alternative: a signed, revocable URL that
carries every assignment/event due date across all of Alex's courses, readable with
zero stored credentials. It delivers most of what the API connector would have:
deadlines appear here the moment a professor publishes them.

Config: CANVAS_ICS_URL in the environment (lives in ~/second-brain/.env — treat it as
a secret; anyone holding the URL can read the whole academic calendar). Unset => this
module is inert, like every optional source.

What sync does: merges feed events into School/assignments.csv keyed by the feed's
UID (source column = "canvas:<uid>").
  - New events are APPENDED with status "open".
  - An existing canvas row whose due date moved in the feed is UPDATED (professors
    reschedule things; the feed is authoritative for its own rows).
  - Rows a human created (source != canvas:*) are NEVER touched, and canvas rows that
    vanish from the feed are kept (feeds only show a rolling window; disappearance is
    not cancellation).
Auto-merge rather than the syllabus-intake proposal flow on purpose: proposals exist
because free-text syllabus dates are guesswork. ICS dates are exact machine data from
the source of truth, so making Alex confirm each one would be pure friction.

The school brief (scripts/school_status.py) reads assignments.csv, so synced deadlines
flow into DUE-soon warnings and nudges with no further wiring.
"""

import csv
import os
import re
import ssl
import urllib.request
from datetime import datetime, timezone

try:
    import certifi
    _SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CONTEXT = None

DEFAULT_VAULT = os.path.expanduser(
    "~/Library/Mobile Documents/com~apple~CloudDocs/Obsidian/Second brain")
FETCH_TIMEOUT = 20

# "✅ Syllabus Quiz  [ECON 103]" -> title + course code. The suffix is Canvas's own
# convention for feed items, so a missing bracket means a personal/manual event.
_COURSE_RE = re.compile(r"^(?P<title>.*?)\s*\[(?P<course>[A-Z]{2,5}\s?\d{2,4}[A-Z]?)\]\s*$")

_TYPE_HINTS = [  # first match wins, purely cosmetic for the csv's `type` column
    (re.compile(r"\bquiz\b", re.I), "quiz"),
    (re.compile(r"\bexam|midterm|final\b", re.I), "exam"),
    (re.compile(r"\breading\b", re.I), "reading"),
    (re.compile(r"\b(hw|homework|problem set|pset)\b", re.I), "homework"),
    (re.compile(r"\bproject\b", re.I), "project"),
    (re.compile(r"\bpaper|essay\b", re.I), "paper"),
]


def _fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "clarvis-second-brain"})
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT, context=_SSL_CONTEXT) as r:
        return r.read().decode("utf-8", errors="replace")


def _unfold(text: str) -> list:
    """RFC 5545 line unfolding: a line starting with space/tab continues the previous."""
    out = []
    for raw in text.splitlines():
        if raw[:1] in (" ", "\t") and out:
            out[-1] += raw[1:]
        else:
            out.append(raw)
    return out


def _to_local(dtstart: str, tz=None) -> tuple:
    """('2026-08-26T23:30', False) for timed values, ('2026-08-27', True) for all-day.

    Canvas emits UTC ('...T033000Z'); the csv and the brief live in Alex's local time,
    so an 03:30Z deadline must land as 11:30 PM the previous day, not as a phantom
    early-morning one."""
    if re.fullmatch(r"\d{8}", dtstart):
        return f"{dtstart[:4]}-{dtstart[4:6]}-{dtstart[6:]}", True
    m = re.fullmatch(r"(\d{8})T(\d{6})(Z?)", dtstart)
    if not m:
        return "", False
    dt = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
    if m.group(3) == "Z":
        dt = dt.replace(tzinfo=timezone.utc).astimezone(tz)  # tz=None -> system local
    return dt.strftime("%Y-%m-%dT%H:%M"), False


def parse_events(ics_text: str, tz=None) -> list:
    """[{uid, title, course, due, all_day, url}] — one per VEVENT with a parseable date."""
    events, cur = [], None
    for line in _unfold(ics_text):
        if line == "BEGIN:VEVENT":
            cur = {}
        elif line == "END:VEVENT" and cur is not None:
            due, all_day = _to_local(cur.get("dtstart", ""), tz=tz)
            summary = cur.get("summary", "").strip()
            if due and summary:
                m = _COURSE_RE.match(summary)
                events.append({
                    "uid": cur.get("uid", ""),
                    "title": (m.group("title") if m else summary).strip(),
                    "course": (m.group("course").replace(" ", "") if m else ""),
                    "due": due,
                    "all_day": all_day,
                    "url": cur.get("url", ""),
                })
            cur = None
        elif cur is not None and ":" in line:
            name, value = line.split(":", 1)
            name = name.split(";")[0].upper()  # DTSTART;VALUE=DATE -> DTSTART
            if name in ("UID", "SUMMARY", "DTSTART", "URL"):
                # ICS escapes commas/semicolons in text values
                cur[name.lower()] = value.replace("\\,", ",").replace("\\;", ";")
    return events


def _guess_type(title: str) -> str:
    for rx, kind in _TYPE_HINTS:
        if rx.search(title):
            return kind
    return "assignment"


def sync(vault_dir: str = DEFAULT_VAULT, ics_text: str = None, tz=None) -> dict:
    """Merge the feed into School/assignments.csv. Returns {added, updated, unchanged,
    skipped_no_course, error}. Never raises; never touches human-created rows."""
    url = os.environ.get("CANVAS_ICS_URL", "").strip()
    if ics_text is None:
        if not url:
            return {"error": "CANVAS_ICS_URL not set", "added": 0, "updated": 0,
                    "unchanged": 0, "skipped_no_course": 0}
        try:
            ics_text = _fetch(url)
        except Exception as e:
            return {"error": f"feed fetch failed: {e}", "added": 0, "updated": 0,
                    "unchanged": 0, "skipped_no_course": 0}

    path = os.path.join(vault_dir, "School", "assignments.csv")
    try:
        with open(path, newline="") as f:
            reader = csv.DictReader(f)
            fields = reader.fieldnames
            rows = list(reader)
    except FileNotFoundError:
        return {"error": f"missing {path}", "added": 0, "updated": 0,
                "unchanged": 0, "skipped_no_course": 0}

    by_uid = {r["source"][len("canvas:"):]: r for r in rows
              if (r.get("source") or "").startswith("canvas:")}
    added = updated = unchanged = skipped = 0
    # Which courses gained rows, so a course going live is visible instead of
    # arriving as an anonymous bump in the "added" count.
    per_course, had_rows = {}, {(r.get("course") or "").strip()
                                for r in rows if (r.get("course") or "").strip()}
    for ev in parse_events(ics_text, tz=tz):
        if not ev["course"]:
            skipped += 1          # personal calendar event, not coursework
            continue
        old = by_uid.get(ev["uid"])
        if old is None:
            rows.append({**{k: "" for k in fields},
                         "course": ev["course"], "title": ev["title"],
                         "type": _guess_type(ev["title"]), "due_date": ev["due"],
                         "status": "open", "source": f"canvas:{ev['uid']}"})
            added += 1
            per_course[ev["course"]] = per_course.get(ev["course"], 0) + 1
        elif old.get("due_date") != ev["due"]:
            old["due_date"] = ev["due"]   # professor moved it; feed wins for its rows
            updated += 1
        else:
            unchanged += 1

    if added or updated:
        with open(path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(rows)
    # A course appearing in the feed for the FIRST time means the professor just
    # published it. CSDS101 — the heaviest course, 4 units + lab — was still
    # unpublished, and without this its debut would land as a silent +6 in a log
    # file nothing reads.
    newly_live = sorted(c for c in per_course if c not in had_rows)
    return {"added": added, "updated": updated, "unchanged": unchanged,
            "skipped_no_course": skipped, "error": "",
            "per_course": per_course, "newly_live": newly_live}


def _notify_newly_live(courses: list) -> str:
    """One ntfy push when a course publishes. Deliberately dependency-free (the
    same stance as the rest of this script, which launchd runs standalone): a
    direct POST, no imports from the Flask app."""
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic or not courses:
        return "no topic" if courses else ""
    names = ", ".join(courses)
    body = (f"{names} just went live on Canvas — deadlines are syncing now, but "
            "there's no curriculum imported yet, so lead-window planning is "
            "partial. Drop the syllabus in School/Intake and ask CLARVIS to run "
            "the import.")
    req = urllib.request.Request(
        f"{os.environ.get('NTFY_SERVER', 'https://ntfy.sh')}/{topic}",
        data=body.encode("utf-8"),
        headers={"Title": f"New course live: {names}", "Tags": "books",
                 "Priority": "default"})
    try:
        urllib.request.urlopen(req, timeout=10)
        return f"notified: {names}"
    except Exception as e:            # a failed push must never fail the sync
        return f"notify failed: {e}"


if __name__ == "__main__":
    import json
    import sys
    if "--sync" in sys.argv:
        result = sync()
        if result.get("newly_live"):
            result["notify"] = _notify_newly_live(result["newly_live"])
        # Heartbeat: a sync that finds nothing looks identical to a sync that
        # never ran, and canvas_sync had no beat, no event, and a log nothing
        # reads — its last recorded line was a fetch timeout nobody saw. Both a
        # local file (cheap, works offline) and a monitor heartbeat (so
        # check_heartbeats raises an incident if the job stops firing).
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        try:
            with open(os.path.join(root, ".canvas_sync_heartbeat"), "w") as f:
                f.write(datetime.now().isoformat())
        except OSError:
            pass
        import subprocess
        if result.get("error"):
            # Deliberately NO heartbeat on a failed run: beating here would make
            # a permanently broken sync look perfectly healthy, which is the
            # exact failure this is meant to catch. Raise an incident instead,
            # and let the heartbeat go stale.
            try:
                subprocess.run(
                    [sys.executable, os.path.join(root, "scripts", "report_event.py"),
                     "canvas-sync", "warning", "canvas sync failed",
                     str(result["error"])[:300]],
                    timeout=30, capture_output=True)
            except Exception:
                pass
        else:
            try:
                subprocess.run(
                    [sys.executable, os.path.join(root, "scripts", "beat.py"),
                     "canvas-sync", "86400",
                     f"+{result.get('added', 0)} ~{result.get('updated', 0)}"],
                    timeout=30, capture_output=True)
            except Exception:
                pass      # a heartbeat must never fail the sync it reports on
        print(f"[{datetime.now().strftime('%Y-%m-%dT%H:%M:%S')}]",
              json.dumps(result))
    else:
        for ev in parse_events(_fetch(os.environ["CANVAS_ICS_URL"])):
            print(ev["due"], f"[{ev['course'] or 'personal'}]", ev["title"])
