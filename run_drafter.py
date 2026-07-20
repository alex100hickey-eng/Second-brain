"""
run_drafter.py — drafts overnight-build run prompts for Alex to review and launch.

Jarvis can turn a goal (or a tracked task) into a complete, correctly-formatted
overnight-build prompt: the same shape as the runs Alex kicks off by hand. The pipeline:

    gather context  →  decision council (pros / cons / feasibility)  →  draft the prompt

The draft is written to run_drafts/<date>-<slug>.md with the council verdict attached,
and tracked in run_drafts/index.json with a status (draft → approved → launched →
completed). Alex reviews drafts on the dashboard and launches approved ones himself with
jarvis-launch.sh.

    *** THIS MODULE DRAFTS ONLY. ***
    It never launches Claude Code, never executes a drafted plan, never schedules anything,
    and never runs any agent. Its entire output is text on disk for Alex to read. There is
    no code path here that starts a run. The SYSTEM DIRECTIVE and HARD SAFETY RULES below
    are copied verbatim into every draft and are NEVER weakened or model-generated — the
    language model only writes the prioritized spec and success criteria.
"""

import os
import re
import json
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

_TZ = ZoneInfo("America/New_York")
_ROOT = os.path.dirname(os.path.abspath(__file__))
RUN_DRAFTS_DIR = os.path.join(_ROOT, "run_drafts")
INDEX_PATH = os.path.join(RUN_DRAFTS_DIR, "index.json")

STATUSES = ["draft", "approved", "launched", "completed"]

_lock = threading.Lock()


# ============================================================================
# VERBATIM SAFETY FRAMEWORK — copied into every draft, never weakened.
# These are constants, not model output, so a drafted run's safety guarantees
# are identical to the runs Alex writes by hand.
# ============================================================================
SYSTEM_DIRECTIVE = """## SYSTEM DIRECTIVE

You are operating in full autonomy mode. I am away and not monitoring this session. These rules override default behavior:

1. **Never ask for permission.** Decide everything yourself; keep moving.
2. **Work the priorities IN ORDER; never leave one half-wired to start the next.** Finished early priorities beat many partials. This is a long list — running out of runway is expected and fine.
3. **If blocked, pivot within 5 minutes.** No accounts, keys, or paid services — build around gaps, mock realistically, mark limitations. Open-source packages and open model downloads are fine.
4. **Log progress** per phase in BUILD_LOG.md (timestamped: completed, decisions, pivots, issues).
5. **Test everything; extend `run_tests.py`** to cover each new feature you build, and keep the full suite passing.
6. **One final message only**: per-priority status, testing summary, limitations, my action list."""

HARD_SAFETY_RULES = """## HARD SAFETY RULES

- Work ONLY inside this project directory. Obsidian vault stays strictly READ-ONLY.
- No deleting files — obsolete goes to `_archive/`.
- No signups, purchases, or credentials beyond `.env`. No Supabase schema changes or data deletion.
- No deployment, no remote systems, nothing exposed beyond 127.0.0.1. All security invariants stay intact (secrets in `.env`, localhost-only, access code).
- **Screen-watch is WATCH-ONLY.** Do not install or write any code that controls the mouse, keyboard, or UI (no pyautogui-style control). Capture and analyze only.
- **The run drafter DRAFTS ONLY.** It must never launch Claude Code, execute a drafted plan, schedule anything, or run any agent autonomously. Output is text for my review, full stop.
- **Privacy:** the conversation-memory database and any captured screenshots must be gitignored and stay local. Screenshots are processed then deleted by default — never silently archived.
- macOS permission dialogs (Screen Recording, Microphone) can only be granted by me. If a permission is missing, do NOT fight it: build the feature fully, test against saved sample inputs, and put the one-time grant steps at the top of my action list."""

PROJECT_CONTEXT = """## PROJECT CONTEXT

second-brain: Flask chat app (localhost:5001, access-code gated) with Claude tools (vault search/read, Supabase lookup, note logging, video input via ffmpeg+whisper.cpp, synthesize_data, create_website, edit_video, conversation memory, watch_screen, draft_run, goals), a decision council (pros, cons, feasibility judge), a dashboard, a Task Manager (statuses: idea → evaluating → approved → in progress → done/dropped, SQLite/JSON local storage), standalone agents, and a test suite (`run_tests.py`). READ FIRST: BUILD_LOG.md, SECURITY_NOTES.md, and the code. Match existing patterns; don't invent parallel architecture. whisper.cpp and its English base model are already installed — reuse them."""


# ============================================================================
# INDEX (status tracking)
# ============================================================================
def _load_index() -> list:
    if not os.path.exists(INDEX_PATH):
        return []
    try:
        with open(INDEX_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []


def _save_index(entries: list) -> None:
    os.makedirs(RUN_DRAFTS_DIR, exist_ok=True)
    tmp = INDEX_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(entries, f, indent=2)
    os.replace(tmp, INDEX_PATH)


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return (s[:50] or "run").rstrip("-")


def _now_iso() -> str:
    return datetime.now(_TZ).isoformat()


# ============================================================================
# DRAFT GENERATION
# ============================================================================
def _spec_prompt(goal: str, context: str, council_verdict: str) -> str:
    return (
        "You are drafting the BODY of an overnight autonomous-build prompt for a coding agent "
        "working on the 'second-brain' project. I will prepend the fixed SYSTEM DIRECTIVE, HARD "
        "SAFETY RULES, and PROJECT CONTEXT myself — do NOT write those. You write ONLY two "
        "sections, in this exact markdown shape:\n\n"
        "## PRIORITIES — COMPLETE IN THIS ORDER\n\n"
        "### Priority 1: <name>\n<clear, specific, buildable spec with concrete bullet points, "
        "including what to test>\n\n### Priority 2: <name>\n<...>\n"
        "(2-5 priorities, ordered by importance, each self-contained and testable)\n\n"
        "## SUCCESS CRITERIA\n- <checkable outcome>\n- <...>\n"
        "(include: run_tests.py extended and passing; security invariants intact; BUILD_LOG per phase)\n\n"
        "Guidelines: be concrete and technical, reference real parts of the system, keep each "
        "priority independently shippable, and make the FIRST priority the highest-value one. "
        "Fold in the council's cautions where relevant. Do not weaken any safety rule. Do not "
        "add launching/scheduling/deploying to the spec.\n\n"
        f"GOAL TO TURN INTO A RUN:\n{goal}\n\n"
        f"GATHERED CONTEXT:\n{context or '(none)'}\n\n"
        f"DECISION COUNCIL VERDICT (weigh its cautions):\n{council_verdict or '(none)'}\n"
    )


def generate_spec(goal: str, context: str, council_verdict: str, claude_client) -> str:
    """Model-written prioritized spec + success criteria (the only model-generated part)."""
    msg = claude_client.messages.create(
        model="claude-sonnet-5",
        max_tokens=2000,
        messages=[{"role": "user", "content": _spec_prompt(goal, context, council_verdict)}],
    )
    return "".join(b.text for b in msg.content if b.type == "text").strip()


DEFAULT_SUCCESS_CRITERIA = """## SUCCESS CRITERIA

- Every priority above is complete and works end-to-end (verified, not just written).
- `run_tests.py` is extended to cover each new feature and the full suite passes.
- All security invariants intact: secrets in `.env`, bound to 127.0.0.1 only, access-code gate enforced, Obsidian vault never written, no mouse/keyboard control code, drafter/screen-watch limits held.
- BUILD_LOG.md updated per phase (completed, decisions, pivots, issues).
- Final message: per-priority status, testing summary, limitations, and my action list."""


def assemble_prompt(title: str, goal: str, spec_body: str) -> str:
    """Assemble the full run prompt: verbatim safety framework + model-written spec.
    Coverage guard: the format REQUIRES a Success Criteria section — if the model folded
    it into the priorities instead, append a standard one so every draft is complete."""
    spec_body = spec_body.strip()
    if "success criteria" not in spec_body.lower():
        spec_body += "\n\n" + DEFAULT_SUCCESS_CRITERIA
    header = f"# {title}\n\n" if title else ""
    return (
        f"{header}"
        f"{SYSTEM_DIRECTIVE}\n\n"
        f"{HARD_SAFETY_RULES}\n\n"
        f"{PROJECT_CONTEXT}\n\n"
        f"{spec_body}\n\n"
        "---\n"
        "The only message I want from you is the final deliverable. Go.\n"
    )


def create_draft(goal: str, context: str, council_verdict: str, claude_client,
                 title: str = "") -> dict:
    """Full pipeline entry point. Generates the spec, assembles the prompt, writes the
    draft file + council verdict sidecar, records it in the index. DRAFTS ONLY."""
    goal = (goal or "").strip()
    if not goal:
        return {"error": "Give me a goal (or a task) to draft a run from."}

    title = (title or "").strip() or f"Overnight run — {goal[:60]}"
    spec_body = generate_spec(goal, context, council_verdict, claude_client)
    full_prompt = assemble_prompt(title, goal, spec_body)

    os.makedirs(RUN_DRAFTS_DIR, exist_ok=True)
    date = datetime.now(_TZ).strftime("%Y-%m-%d")
    base = f"{date}-{_slug(title or goal)}"
    fname = base + ".md"
    path = os.path.join(RUN_DRAFTS_DIR, fname)
    n = 1
    while os.path.exists(path):
        fname = f"{base}-{n}.md"
        path = os.path.join(RUN_DRAFTS_DIR, fname)
        n += 1

    # The draft file: the launchable prompt, then the council verdict appended for review.
    content = full_prompt
    if council_verdict:
        content += (
            "\n\n<!-- ============ COUNCIL REVIEW (not part of the launch prompt) ============ -->\n"
            "## Decision Council Verdict\n\n" + council_verdict.strip() + "\n"
        )
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)

    with _lock:
        entries = _load_index()
        new_id = (max([e.get("id", 0) for e in entries], default=0) + 1)
        entry = {
            "id": new_id,
            "title": title,
            "goal": goal,
            "file": fname,
            "status": "draft",
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
            "has_verdict": bool(council_verdict),
        }
        entries.append(entry)
        _save_index(entries)
    return {"ok": True, "id": new_id, "file": fname, "path": path, "title": title}


# ============================================================================
# QUERY / STATUS
# ============================================================================
def list_drafts(status: str = None) -> list:
    entries = _load_index()
    if status:
        status = status.strip().lower()
        entries = [e for e in entries if e.get("status") == status]
    return sorted(entries, key=lambda e: e.get("id", 0), reverse=True)


def get_draft(draft_id: int) -> dict | None:
    return next((e for e in _load_index() if e.get("id") == draft_id), None)


def read_draft_body(draft_id: int) -> str | None:
    e = get_draft(draft_id)
    if not e:
        return None
    path = os.path.join(RUN_DRAFTS_DIR, e["file"])
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return f.read()


def set_status(draft_id: int, status: str) -> dict | None:
    status = (status or "").strip().lower()
    if status not in STATUSES:
        return {"error": f"Unknown status '{status}'. Valid: {', '.join(STATUSES)}."}
    with _lock:
        entries = _load_index()
        for e in entries:
            if e.get("id") == draft_id:
                e["status"] = status
                e["updated_at"] = _now_iso()
                _save_index(entries)
                return e
    return None


def dashboard_rows(limit: int = 8) -> list:
    """Compact rows for the home dashboard's 'Drafted runs' panel."""
    out = []
    for e in list_drafts()[:limit]:
        out.append({
            "id": e["id"], "title": e["title"], "status": e["status"],
            "file": e["file"], "has_verdict": e.get("has_verdict", False),
            "when": _humanize(e.get("updated_at", "")),
        })
    return out


def _humanize(iso: str) -> str:
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return ""
    secs = (datetime.now(_TZ) - dt).total_seconds()
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{int(secs // 60)}m ago"
    if secs < 86400:
        return f"{int(secs // 3600)}h ago"
    if secs < 7 * 86400:
        return f"{int(secs // 86400)}d ago"
    return dt.strftime("%b %-d")


# ============================================================================
# CHAT-TOOL WRAPPER
# ============================================================================
def tool_list_drafts(status: str = None) -> str:
    drafts = list_drafts(status)
    if not drafts:
        return ("No drafted runs yet. Say \"draft a run to <goal>\" and I'll write one for your review."
                if not status else f"No drafted runs with status '{status}'.")
    lines = ["Drafted overnight runs:"]
    for e in drafts:
        v = " · council ✓" if e.get("has_verdict") else ""
        lines.append(f"#{e['id']} [{e['status']}] {e['title']} — run_drafts/{e['file']}{v}")
    lines.append("\nReview them on the dashboard's Drafted Runs panel. Launch an approved one "
                 "yourself with `bash jarvis-launch.sh` — I never launch anything.")
    return "\n".join(lines)
