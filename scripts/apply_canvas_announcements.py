#!/usr/bin/env python3
"""
apply_canvas_announcements.py — Canvas announcements into CLARVIS's intake.

Announcements are where the un-deadlined obligations hide ("bring your index
card Tuesday", "finish the last three handout problems") — the ICS feed never
sees them, and the weekly sweep only reads them on Fridays. The nightly
canvas-status-sync task navigates the logged-in Browser pane to

    /api/v1/announcements?context_codes[]=course_<id>&...&start_date=<2 days ago>

saves the JSON under .canvas_status/announcements.json, and runs this script,
which hands each NEW announcement to intake.record_raw — the same extractor and
dedupe every email goes through — so anything actionable nudges like mail does.

    python3 scripts/apply_canvas_announcements.py .canvas_status/announcements.json [--dry-run]

Reads only; the one write is an intake_event row per actionable announcement.
Seen ids are remembered in .canvas_status/announcements_seen.json as well as in
intake's own seen-cache, so a re-run never re-extracts.
"""

import html
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHAT = os.path.join(ROOT, "second-brain-chat")
SEEN_PATH = os.path.join(ROOT, ".canvas_status", "announcements_seen.json")
COURSES = {"53812": "ACCT100", "53744": "ECON103", "55030": "CSDS101",
           "54100": "AIQS100", "54158": "MATH120"}
SOURCE = "canvas"


def strip_html(text: str) -> str:
    text = re.sub(r"<\s*br\s*/?>|</p>|</div>|</li>", "\n", text or "", flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n", text)).strip()


def load(path: str) -> list:
    text = open(path, encoding="utf-8", errors="replace").read()
    text = re.sub(r"^\s*while\s*\(\s*1\s*\)\s*;", "", text).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"(\[\s*\{.*\}\s*\])", text, re.S)
        data = json.loads(m.group(1)) if m else []
    return [a for a in data if isinstance(a, dict) and a.get("id")]


def _seen() -> set:
    try:
        return set(json.load(open(SEEN_PATH)))
    except Exception:
        return set()


def _remember(ids: set) -> None:
    os.makedirs(os.path.dirname(SEEN_PATH), exist_ok=True)
    json.dump(sorted(ids), open(SEEN_PATH, "w"))


def apply(files: list, record=None, dry_run: bool = False) -> dict:
    """record(source, ref, sender, ts, text) -> intake.record_raw's result.
    Injected so the test can run this without a model or a database."""
    seen = _seen()
    new, skipped, recorded = [], 0, []
    for f in files:
        for a in load(f):
            aid = str(a["id"])
            if aid in seen:
                skipped += 1
                continue
            course = COURSES.get(str(a.get("context_code") or "").replace("course_", ""),
                                 str(a.get("context_code") or "canvas"))
            title = strip_html(a.get("title") or "")
            body = strip_html(a.get("message") or "")
            url = a.get("html_url") or ""
            new.append((aid, course, title, body, a.get("posted_at") or "", url))
    if dry_run:
        return {"new": len(new), "skipped": skipped, "recorded": [], "dry_run": True,
                "titles": [f"{c}: {t}" for _, c, t, *_ in new]}
    if new and record is None:
        record = _real_recorder()
    done = set(seen)
    for aid, course, title, body, ts, url in new:
        text = f"Subject: {title}\n{body}"[:4000]
        sender = f"{course} announcement" + (f" ({url})" if url else "")
        try:
            res = record(SOURCE, f"ann-{aid}", sender, ts, text)
        except Exception as e:
            res = {"recorded": False, "reason": f"error: {str(e)[:120]}"}
        recorded.append((course, title, res.get("recorded"), res.get("reason")))
        done.add(aid)
    _remember(done)
    return {"new": len(new), "skipped": skipped, "recorded": recorded, "dry_run": False}


def _real_recorder():
    """intake.record_raw with the real clients — only reached from the Mac."""
    sys.path.insert(0, CHAT)
    envp = os.path.join(ROOT, ".env")
    if os.path.exists(envp):
        for line in open(envp):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    from anthropic import Anthropic
    from supabase import create_client
    import intake
    import task_tracker
    intake.init(claude_client=Anthropic(api_key=os.environ["CLAUDE_API_KEY"]),
                supabase_client=create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"]),
                tool_dispatcher=lambda slug, args: "{}",
                tracker=task_tracker.get_tracker())
    return intake.record_raw


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        sys.exit(__doc__)
    res = apply(args, dry_run="--dry-run" in sys.argv)
    print(json.dumps({k: v for k, v in res.items() if k not in ("recorded", "titles")}))
    for line in res.get("titles") or []:
        print("   would record:", line)
    for course, title, ok, why in res.get("recorded") or []:
        print(f"   {course}: {title[:60]} -> {'recorded' if ok else why}")
