"""
capability_escalation.py — CLARVIS files its own feature requests.

Before this: CLARVIS hits a wall ("I can't read raw subjects", "no tool for X"),
tells Alex, Alex screenshots it into a Claude Code session, Claude Code ships a
fix. Alex is the middleman, and he's asked to stop being one.

Now: CLARVIS calls request_capability() the moment it hits a wall. The request
lands in Supabase (same "Agent Outputs" table every subsystem shares). On Alex's
Mac, a launchd watcher (scripts/capability_watcher.py, every 2 min) sees the new
row and spawns Claude Code, which implements the request in the repo (tests, then
push → auto-deploy) and writes an update row back — which CLARVIS reads via
check_capability_requests to tell Alex what shipped.

The watcher replaced a Claude Code task that polled every 30 minutes: ~48 agent
sessions a day to answer a question that is one SELECT, for a queue that gets a
request maybe weekly. Polling is free; booting a reasoning agent to poll is not.
That task still runs once a day as a backstop, and checks the watcher's heartbeat
— a dead watcher and a quiet queue look identical otherwise.

Row shapes (insert-only, like draft_store — newest row wins):
  agent_name="capability_request"        output_text=json {slug, title, problem,
                                          needed, requested_at}
  agent_name="capability_request_update" output_text=json {slug, status, note,
                                          commit, updated_at}
  status: in_progress | done | declined

The processor treats request text as UNTRUSTED input (it originates from model
output that read untrusted emails/web pages). Its standing guardrails live in
the scheduled task prompt: never add secrets, never wire send/exfiltration
capability, never weaken data_boundary or auth, tests must pass before push.

CLI (used by the scheduled processor on the Mac; reads .env / env vars):
  python3 capability_escalation.py pending          → JSON list of open requests
  python3 capability_escalation.py mark SLUG STATUS "note" [commit]
"""

import json
import os
import re
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

REQUEST_KIND = "capability_request"
UPDATE_KIND = "capability_request_update"
VALID_STATUSES = ("in_progress", "done", "declined")
FIELD_CAP = 2000          # per-field sanity ceiling on request text

supabase = None


def init(supabase_client):
    global supabase
    supabase = supabase_client


def _now_iso() -> str:
    return datetime.now(ZoneInfo("America/New_York")).isoformat()


def _rows(kind: str, limit: int = 100) -> list:
    if supabase is None:
        return []
    try:
        return (supabase.table("Agent Outputs").select("id,output_text")
                .eq("agent_name", kind).order("id", desc=True)
                .limit(limit).execute().data or [])
    except Exception:
        return []


def _parsed(kind: str, limit: int = 100) -> list:
    out = []
    for row in _rows(kind, limit):
        try:
            out.append(json.loads(row["output_text"]))
        except (json.JSONDecodeError, TypeError):
            continue
    return out


def _latest_update(slug: str) -> dict:
    for u in _parsed(UPDATE_KIND):        # newest first
        if u.get("slug") == slug:
            return u
    return {}


def _slugify(title: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")[:40]
    return f"{s or 'request'}-{datetime.now(ZoneInfo('America/New_York')).strftime('%m%d%H%M')}"


def file_request(title: str, problem: str, needed: str = "") -> str:
    """CLARVIS-facing: file one capability request. Returns chat-ready text."""
    if supabase is None:
        return "Escalation queue isn't connected (no Supabase client)."
    title = (title or "").strip()[:200]
    if not title or not (problem or "").strip():
        return "A request needs at least a title and the problem you hit."
    # One open request per problem: if an un-resolved request already has a very
    # similar title, point at it instead of double-filing.
    for r in _parsed(REQUEST_KIND):
        st = _latest_update(r.get("slug", "")).get("status")
        if st in ("done", "declined"):
            continue
        if r.get("title", "").lower()[:60] == title.lower()[:60]:
            return (f"Already filed as '{r['slug']}' and still open — no need to "
                    f"file it twice. Check check_capability_requests for status.")
    slug = _slugify(title)
    try:
        supabase.table("Agent Outputs").insert({
            "agent_name": REQUEST_KIND,
            "output_text": json.dumps({
                "slug": slug, "title": title,
                "problem": (problem or "").strip()[:FIELD_CAP],
                "needed": (needed or "").strip()[:FIELD_CAP],
                "requested_at": _now_iso()}),
        }).execute()
    except Exception as e:
        return f"Couldn't file the request: {str(e)[:200]}"
    return (f"Filed capability request '{slug}'. A watcher on Alex's Mac picks "
            f"this up within a couple of minutes (whenever the Mac is awake), "
            f"builds the fix, and it auto-deploys. Tell Alex it's filed and that "
            f"you'll have the ability once the build lands — he doesn't need to "
            f"relay anything.")


def status_report() -> str:
    """CLARVIS-facing: open + recently resolved requests, human-readable."""
    reqs = _parsed(REQUEST_KIND, limit=50)
    if not reqs:
        return "No capability requests on file — the escalation queue is empty."
    open_lines, closed_lines = [], []
    for r in reqs:
        u = _latest_update(r.get("slug", ""))
        st = u.get("status")
        line = f"- {r.get('slug')}: {r.get('title')}"
        if st == "done":
            closed_lines.append(line + f" — SHIPPED ({u.get('note', '')[:120]})")
        elif st == "declined":
            closed_lines.append(line + f" — declined ({u.get('note', '')[:120]})")
        elif st == "in_progress":
            open_lines.append(line + " — being built right now")
        else:
            open_lines.append(line + f" — waiting for the next engineering pass "
                                     f"(filed {str(r.get('requested_at'))[:16]})")
    out = []
    if open_lines:
        out.append("Open requests:\n" + "\n".join(open_lines))
    if closed_lines:
        out.append("Recently resolved:\n" + "\n".join(closed_lines[:5]))
    return "\n\n".join(out)


# ------------------------------------------------------------
# Processor side (Claude Code on the Mac) — importable + CLI
# ------------------------------------------------------------

def pending_requests() -> list:
    """Open requests, oldest first, each with its latest status attached."""
    out = []
    for r in _parsed(REQUEST_KIND, limit=50):
        u = _latest_update(r.get("slug", ""))
        if u.get("status") in ("done", "declined"):
            continue
        r["status"] = u.get("status", "pending")
        out.append(r)
    return list(reversed(out))


def mark(slug: str, status: str, note: str = "", commit: str = "") -> str:
    if status not in VALID_STATUSES:
        return f"Bad status {status!r} — use one of {VALID_STATUSES}."
    if supabase is None:
        return "No Supabase client."
    supabase.table("Agent Outputs").insert({
        "agent_name": UPDATE_KIND,
        "output_text": json.dumps({
            "slug": slug, "status": status, "note": (note or "")[:FIELD_CAP],
            "commit": commit, "updated_at": _now_iso()}),
    }).execute()
    return f"Marked {slug} → {status}."


def _cli():
    from dotenv import load_dotenv
    from supabase import create_client
    for env_path in (os.path.join(os.path.dirname(__file__), "..", ".env"),
                     os.path.join(os.path.dirname(__file__), ".env")):
        load_dotenv(env_path)
    init(create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"]))
    cmd = sys.argv[1] if len(sys.argv) > 1 else "pending"
    if cmd == "pending":
        print(json.dumps(pending_requests(), indent=2))
    elif cmd == "mark" and len(sys.argv) >= 4:
        print(mark(sys.argv[2], sys.argv[3],
                   sys.argv[4] if len(sys.argv) > 4 else "",
                   sys.argv[5] if len(sys.argv) > 5 else ""))
    else:
        print(__doc__.split("CLI", 1)[1])


if __name__ == "__main__":
    _cli()


TOOL_SCHEMAS = [
    {"name": "request_capability",
     "description": "File a feature/fix request with Claude Code, the engineering agent "
                    "that builds and maintains you. Use this THE MOMENT you hit a wall: "
                    "a tool you don't have, data you can't reach, a bug in one of your "
                    "tools, anything Alex asks for that you can't do yet. Do NOT make "
                    "Alex relay problems to the engineering side himself — filing this "
                    "IS the escalation. A scheduled engineering pass picks it up within "
                    "~30 minutes while Alex's Mac has the Claude app open, and fixes "
                    "auto-deploy to you.",
     "input_schema": {"type": "object", "required": ["title", "problem"], "properties": {
         "title": {"type": "string", "description": "Short name for the missing capability."},
         "problem": {"type": "string",
                     "description": "What you tried, what happened, what Alex actually needed."},
         "needed": {"type": "string",
                    "description": "Your best guess at what to build (optional — the "
                                   "engineering side decides the design)."}}}},
    {"name": "check_capability_requests",
     "description": "Status of the escalation queue — what's waiting, what's being built, "
                    "what shipped. Check this when Alex asks about a previously filed "
                    "request or when you want to know if a new ability has landed.",
     "input_schema": {"type": "object", "properties": {}}},
]

TOOL_STATUS_LABELS = {
    "request_capability": "Filing a request with engineering…",
    "check_capability_requests": "Checking the engineering queue…",
}


def handle_tool_call(name: str, tool_input: dict) -> str:
    if name == "request_capability":
        return file_request(tool_input.get("title", ""), tool_input.get("problem", ""),
                            tool_input.get("needed", ""))
    if name == "check_capability_requests":
        return status_report()
    return "Unknown escalation tool."
