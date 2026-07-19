"""
Second Brain — Chat Brain
A minimal Jarvis-style chat interface: talk to Claude, it can look things up
and take actions via tools backed by Supabase.

Designed to be adaptable: add new capabilities by adding a new function to
TOOLS + AVAILABLE_TOOLS + handle_tool_call. Nothing else needs to change.

Run locally: python3 app.py  (then open http://localhost:5000)
"""

import os
import re
import json
import time
import hmac
import hashlib
import secrets as pysecrets
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from flask import (
    Flask,
    request,
    jsonify,
    render_template,
    Response,
    stream_with_context,
    session,
    redirect,
)
from anthropic import Anthropic
from supabase import create_client
from composio import Composio
from composio_anthropic import AnthropicProvider

# ---- CONFIG — reads from environment variables ----
CLAUDE_API_KEY = os.environ["CLAUDE_API_KEY"]
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
COMPOSIO_API_KEY = os.environ["COMPOSIO_API_KEY"]
# Composio's user identifier for connected accounts — this app is single-user (Alex only).
COMPOSIO_USER_ID = "alex"
# Path to the Obsidian vault. Defaults to the standard iCloud location on Alex's Mac —
# override with a VAULT_PATH env var if this ever runs somewhere else (e.g. once
# deployed to the server with a git-synced copy of the vault).
VAULT_PATH = os.environ.get(
    "VAULT_PATH",
    os.path.expanduser(
        "~/Library/Mobile Documents/com~apple~CloudDocs/Obsidian/Second brain"
    ),
)
VAULT_FOLDERS = ["Schedule", "Learning", "Money", "School", "Athletics"]
# Where drafted agent scripts get written. Sibling to this file's folder, inside
# the main second-brain project (~/second-brain/agents/).
AGENTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "agents")
# Where drafted *tool* proposals for this app itself get written (self-expansion).
# Never auto-merged into app.py — see create_new_tool.
PROPOSED_TOOLS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "proposed_tools")
# ----------------------------------------------------

# Optional login gate. If JARVIS_PASSWORD is set in the environment, every page
# and endpoint (except /login and static files) requires logging in once per
# browser (31-day session). If it's NOT set, the app is open — same as before
# the gate existed. Never hardcode the password; Alex sets the env var himself.
JARVIS_PASSWORD = os.environ.get("JARVIS_PASSWORD")

app = Flask(__name__)
# Session signing key: derived from the password so sessions survive restarts,
# random (sessions reset each restart) when no password gate is configured.
app.secret_key = (
    hashlib.sha256(f"jarvis-session:{JARVIS_PASSWORD}".encode()).digest()
    if JARVIS_PASSWORD
    else pysecrets.token_bytes(32)
)
app.permanent_session_lifetime = timedelta(days=31)


@app.before_request
def require_login():
    if not JARVIS_PASSWORD:
        return None  # gate disabled — open access
    if request.endpoint in ("login", "static"):
        return None
    if session.get("authed"):
        return None
    if request.method == "POST" or request.path.startswith("/api/"):
        return jsonify({"error": "Not logged in."}), 401
    return redirect("/login")


@app.route("/login", methods=["GET", "POST"])
def login():
    if not JARVIS_PASSWORD:
        return redirect("/")
    error = None
    if request.method == "POST":
        attempt = request.form.get("password", "")
        if hmac.compare_digest(attempt.encode(), JARVIS_PASSWORD.encode()):
            session.permanent = True
            session["authed"] = True
            return redirect("/")
        time.sleep(0.8)  # slow down brute-force attempts
        error = "Wrong password."
    return render_template("login.html", error=error)
claude = Anthropic(api_key=CLAUDE_API_KEY)
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
composio = Composio(provider=AnthropicProvider(), api_key=COMPOSIO_API_KEY)

# Read-only Google Calendar tools only — explicitly whitelisted by slug so write/mutation
# tools in the googlecalendar toolkit (create/update/delete event) can never reach Claude.
# Alex's stated rule: consequential/external actions need a confirmation gate, which
# doesn't exist yet — so nothing that changes his calendar is exposed here.
CALENDAR_TOOL_SLUGS = [
    "GOOGLECALENDAR_EVENTS_LIST",
    "GOOGLECALENDAR_FIND_EVENT",
    "GOOGLECALENDAR_LIST_CALENDARS",
    "GOOGLECALENDAR_GET_CURRENT_DATE_TIME",
]
try:
    CALENDAR_TOOLS = composio.tools.get(user_id=COMPOSIO_USER_ID, tools=CALENDAR_TOOL_SLUGS)
except Exception as e:
    print(f"Warning: couldn't fetch Composio calendar tools at startup: {e}")
    CALENDAR_TOOLS = []

# Rows in "Agent Outputs" with these agent_name values are internal storage for the
# chat brain itself (memories, pending approvals, chat history) — not real agent
# outputs. Keep them out of anything that lists agent activity.
INTERNAL_AGENT_NAMES = {"jarvis_memory", "jarvis_pending_action", "jarvis_chat", "jarvis_chat_clear"}

SYSTEM_PROMPT = """You are Alex's personal assistant — the brain of his "second brain" system.
You're direct, helpful, and a little sharp/witty like a good assistant should be — think
capable and efficient, not overly formal.

You have tools to look up what his agents have found, log quick notes to the database, and
read/write actual notes in his Obsidian vault (folders: Schedule, Learning, Money, School,
Athletics). Use log_note for quick throwaway reminders; use write_vault_note when he wants
something saved as a real note in a specific area of his vault. Use them whenever they'd
help answer the question or complete the request. Keep responses conversational and concise
unless asked for detail.

You can also draft brand-new agent scripts with create_new_agent when Alex asks for a new
agent. This tool only ever writes a Python file to disk for him to review — it never runs,
imports, executes, or deploys the script it creates, and no other tool you have does either.
Always tell him the new agent needs his review before he runs or deploys it himself.

You can also propose brand-new tools/capabilities for YOURSELF with create_new_tool, when Alex
asks you to gain some new ability. This writes a proposal file (schema + function + routing
line) to proposed_tools/ — it does NOT add the tool to your own live toolset, does not edit
app.py, and does not restart or redeploy anything. You cannot self-wire new capabilities into
yourself; a human has to review and merge the proposal. Always tell him that.

You have read-only access to Alex's Google Calendar (GOOGLECALENDAR_* tools) — you can list
and search his events, and check calendars/current time, to answer questions about his
schedule.

You have a persistent memory. Facts you've saved appear below under "Saved memories" — treat
them as things you know about Alex. When he tells you something worth keeping long-term (a
preference, a goal, a recurring commitment, a fact about his life), or asks you to remember
something, save it with the remember tool. Keep each memory to one short, self-contained
sentence. Don't save throwaway context or things already in your memories.

You can propose adding events to Alex's calendar with propose_calendar_event, but you cannot
create them directly. A proposal goes into a pending-approval queue that Alex reviews on his
dashboard — nothing touches his calendar until he clicks Approve there. When you propose an
event, tell him it's waiting for his approval on the dashboard. This approve-first rule
exists because calendar changes are consequential; never imply an event is booked before
he's approved it."""


# ============================================================
# TOOLS — add new capabilities here as the system grows
# ============================================================

TOOLS = [
    {
        "name": "get_recent_agent_outputs",
        "description": "Look up recent outputs from Alex's specialist agents (e.g. the money/clips idea generator). Use this when he asks what his agents have found, generated, or been up to.",
        "input_schema": {
            "type": "object",
            "properties": {
                "agent_name": {
                    "type": "string",
                    "description": "Filter to a specific agent's name, e.g. 'money_clips_agent'. Omit to get outputs from all agents.",
                },
                "limit": {
                    "type": "integer",
                    "description": "How many recent rows to return. Default 5.",
                },
            },
        },
    },
    {
        "name": "log_note",
        "description": "Save a note, task, or reminder Alex wants tracked. Use this when he asks you to remember, note down, or track something.",
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "The note or task content to save.",
                }
            },
            "required": ["text"],
        },
    },
    {
        "name": "list_vault_notes",
        "description": "List the Obsidian notes in Alex's vault, optionally within one folder (Schedule, Learning, Money, School, Athletics). Use this to see what notes exist before reading one, or to answer 'what's in my X notes'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "folder": {
                    "type": "string",
                    "description": "One of: Schedule, Learning, Money, School, Athletics. Omit to list all notes in the vault.",
                }
            },
        },
    },
    {
        "name": "read_vault_note",
        "description": "Read the full content of a specific note in Alex's Obsidian vault. Use list_vault_notes first if you don't know the exact path.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path relative to the vault root, e.g. 'Money/idea.md'.",
                }
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_vault_note",
        "description": "Create a new note or append to an existing one in Alex's Obsidian vault. Use this when he asks you to save, write, or add something to his notes (as opposed to log_note, which saves to the database instead).",
        "input_schema": {
            "type": "object",
            "properties": {
                "folder": {
                    "type": "string",
                    "description": "One of: Schedule, Learning, Money, School, Athletics.",
                },
                "filename": {
                    "type": "string",
                    "description": "Filename for the note, e.g. 'video-ideas.md'. Adds .md automatically if missing.",
                },
                "content": {
                    "type": "string",
                    "description": "The text to write into the note.",
                },
                "append": {
                    "type": "boolean",
                    "description": "If true, adds to the end of an existing note instead of overwriting it. Default false.",
                },
            },
            "required": ["folder", "filename", "content"],
        },
    },
    {
        "name": "remember",
        "description": "Save a fact about Alex to your persistent long-term memory. It will be available to you in every future conversation. Use when he tells you something worth keeping (preferences, goals, recurring commitments, life facts) or asks you to remember something. One short sentence per memory.",
        "input_schema": {
            "type": "object",
            "properties": {
                "fact": {
                    "type": "string",
                    "description": "The fact to remember, as one short self-contained sentence, e.g. 'Alex's football practice is on Tuesday and Thursday evenings.'",
                }
            },
            "required": ["fact"],
        },
    },
    {
        "name": "propose_calendar_event",
        "description": (
            "Propose a new Google Calendar event for Alex. This does NOT create the event — it queues "
            "a pending action that Alex must approve on his dashboard before anything is added to his "
            "calendar. Use when he asks to schedule, book, or add something to his calendar. Always tell "
            "him afterwards that it's awaiting his approval on the dashboard."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Event title, e.g. 'Film study session'.",
                },
                "start_datetime": {
                    "type": "string",
                    "description": "Start time in ISO 8601 local time, e.g. '2026-07-20T15:00:00'. Interpreted in Alex's timezone (America/New_York).",
                },
                "end_datetime": {
                    "type": "string",
                    "description": "End time in ISO 8601 local time, e.g. '2026-07-20T16:00:00'.",
                },
                "description": {
                    "type": "string",
                    "description": "Optional event description/notes.",
                },
            },
            "required": ["title", "start_datetime", "end_datetime"],
        },
    },
    {
        "name": "create_new_tool",
        "description": (
            "Draft a proposal for a brand-new tool/capability for THIS chat brain itself (Jarvis) — "
            "e.g. a new way to look something up or take a small reversible action. Saves a proposal "
            "file to proposed_tools/ containing the tool's schema, its Python function, and the routing "
            "line, for Alex to review and manually merge into app.py. This ONLY writes the proposal file — "
            "it never edits app.py, never adds itself to this app's live TOOLS list, and never restarts "
            "or redeploys anything. Use this when Alex asks Jarvis to gain a new capability for itself, "
            "as opposed to create_new_agent which is for standalone background agents."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Short snake_case name for the tool, e.g. 'get_weather'. Used as the filename (proposed_tools/<name>.py) and the tool's name.",
                },
                "purpose": {
                    "type": "string",
                    "description": "Plain-language description of what the tool should do and what inputs it needs, e.g. 'looks up the weather for a city Alex names'.",
                },
            },
            "required": ["name", "purpose"],
        },
    },
    {
        "name": "create_new_agent",
        "description": (
            "Draft a brand-new standalone agent script (following the money_clips_agent.py pattern: "
            "reads secrets from env vars, calls Claude to do its work, logs its output to the Supabase "
            "'Agent Outputs' table) and save it to the agents/ folder for review. Use this when Alex asks "
            "to create, build, or set up a new agent. This ONLY writes the file — it never runs, imports, "
            "or deploys the script. Always tell him it needs review before use."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Short snake_case name for the agent, e.g. 'stock_watch_agent'. Used as the filename (agents/<name>.py) and as its agent_name when logging to Supabase.",
                },
                "purpose": {
                    "type": "string",
                    "description": "Plain-language description of what the agent should do each run, e.g. 'checks stock news for tickers Alex follows and summarizes anything notable'.",
                },
            },
            "required": ["name", "purpose"],
        },
    },
] + CALENDAR_TOOLS


def get_recent_agent_outputs(agent_name: str = None, limit: int = 5) -> str:
    # Over-fetch so internal rows (memories, pending approvals) can be filtered
    # out while still returning up to `limit` real outputs.
    query = (
        supabase.table("Agent Outputs")
        .select("*")
        .order("created_at", desc=True)
        .limit(limit + 20)
    )
    if agent_name:
        query = query.eq("agent_name", agent_name)
    result = query.execute()
    rows = [r for r in (result.data or []) if r["agent_name"] not in INTERNAL_AGENT_NAMES]
    if not rows:
        return "No agent outputs found."
    return json.dumps(rows[:limit], indent=2, default=str)


# ============================================================
# CHAT HISTORY — stored server-side (Supabase) so every device sees
# the same conversation. "Clearing" inserts a marker rather than
# deleting anything; history loads only messages after the last marker.
# ============================================================

def save_chat_message(role: str, content: str) -> None:
    supabase.table("Agent Outputs").insert(
        {"agent_name": "jarvis_chat", "output_text": json.dumps({"role": role, "content": content})}
    ).execute()


def _last_clear_id() -> int:
    result = (
        supabase.table("Agent Outputs")
        .select("id")
        .eq("agent_name", "jarvis_chat_clear")
        .order("id", desc=True)
        .limit(1)
        .execute()
    )
    return result.data[0]["id"] if result.data else 0


def load_chat_history(limit: int = 40) -> list:
    """Messages since the last clear marker, oldest first."""
    result = (
        supabase.table("Agent Outputs")
        .select("*")
        .eq("agent_name", "jarvis_chat")
        .gt("id", _last_clear_id())
        .order("id", desc=True)
        .limit(limit)
        .execute()
    )
    messages = []
    for row in reversed(result.data or []):
        try:
            msg = json.loads(row["output_text"])
        except (json.JSONDecodeError, TypeError):
            continue
        if msg.get("role") in ("user", "assistant") and isinstance(msg.get("content"), str):
            messages.append({"role": msg["role"], "content": msg["content"]})
    return messages


def remember(fact: str) -> str:
    supabase.table("Agent Outputs").insert(
        {"agent_name": "jarvis_memory", "output_text": fact}
    ).execute()
    return f"Remembered: {fact}"


def load_memories() -> list:
    """All saved memories, oldest first, for injection into the system prompt."""
    result = (
        supabase.table("Agent Outputs")
        .select("output_text")
        .eq("agent_name", "jarvis_memory")
        .order("created_at", desc=False)
        .execute()
    )
    return [row["output_text"] for row in (result.data or [])]


def build_system_prompt() -> str:
    memories = load_memories()
    if not memories:
        return SYSTEM_PROMPT + "\n\nSaved memories: none yet."
    lines = "\n".join(f"- {m}" for m in memories)
    return SYSTEM_PROMPT + f"\n\nSaved memories:\n{lines}"


# ============================================================
# APPROVAL LAYER — consequential actions get queued here instead of
# executing. Alex approves or denies them on the dashboard; only an
# explicit approval (POST /api/approve) ever executes anything.
# ============================================================

def propose_calendar_event(title: str, start_datetime: str, end_datetime: str, description: str = "") -> str:
    try:
        start = datetime.fromisoformat(start_datetime)
        end = datetime.fromisoformat(end_datetime)
    except ValueError:
        return "Invalid start or end time — use ISO 8601 format like 2026-07-20T15:00:00."
    if end <= start:
        return "End time must be after start time — proposal not queued."

    action = {
        "action": "create_calendar_event",
        "tool_slug": "GOOGLECALENDAR_CREATE_EVENT",
        "arguments": {
            "calendar_id": "primary",
            "summary": title,
            "start_datetime": start_datetime,
            "end_datetime": end_datetime,
            "timezone": "America/New_York",
            "description": description,
            "create_meeting_room": False,
        },
        "display": f"{title} — {start.strftime('%a %b %-d, %-I:%M %p')} to {end.strftime('%-I:%M %p')}",
        "status": "pending",
    }
    supabase.table("Agent Outputs").insert(
        {"agent_name": "jarvis_pending_action", "output_text": json.dumps(action)}
    ).execute()
    return (
        f"Queued for approval: '{title}' ({start_datetime} to {end_datetime}). "
        "NOT on the calendar yet — Alex must approve it on the dashboard first."
    )


def get_pending_actions() -> list:
    result = (
        supabase.table("Agent Outputs")
        .select("*")
        .eq("agent_name", "jarvis_pending_action")
        .order("created_at", desc=True)
        .limit(30)
        .execute()
    )
    pending = []
    for row in result.data or []:
        try:
            action = json.loads(row["output_text"])
        except (json.JSONDecodeError, TypeError):
            continue
        if action.get("status") == "pending":
            pending.append({"id": row["id"], "display": action.get("display", "(unknown action)"), "action": action.get("action")})
    return pending


def resolve_pending_action(row_id: int, decision: str) -> dict:
    """Approve or deny one pending action. Approval is the ONLY path that executes."""
    result = supabase.table("Agent Outputs").select("*").eq("id", row_id).execute()
    if not result.data:
        return {"ok": False, "error": "No such action."}
    row = result.data[0]
    if row["agent_name"] != "jarvis_pending_action":
        return {"ok": False, "error": "Not an approvable action."}
    action = json.loads(row["output_text"])
    if action.get("status") != "pending":
        return {"ok": False, "error": f"Already {action.get('status')}."}

    if decision == "deny":
        action["status"] = "denied"
        supabase.table("Agent Outputs").update({"output_text": json.dumps(action)}).eq("id", row_id).execute()
        return {"ok": True, "status": "denied"}

    resp = composio.tools.execute(
        slug=action["tool_slug"],
        arguments=action["arguments"],
        user_id=COMPOSIO_USER_ID,
        dangerously_skip_version_check=True,
    )
    if resp.get("successful") is False:
        action["status"] = "failed"
        action["error"] = str(resp.get("error"))[:500]
        supabase.table("Agent Outputs").update({"output_text": json.dumps(action)}).eq("id", row_id).execute()
        return {"ok": False, "error": f"Execution failed: {action['error']}"}

    action["status"] = "approved"
    supabase.table("Agent Outputs").update({"output_text": json.dumps(action)}).eq("id", row_id).execute()
    return {"ok": True, "status": "approved"}


def log_note(text: str) -> str:
    supabase.table("Agent Outputs").insert(
        {"agent_name": "chat_brain_note", "output_text": text}
    ).execute()
    return f"Saved note: {text}"


def list_vault_notes(folder: str = None) -> str:
    search_root = os.path.join(VAULT_PATH, folder) if folder else VAULT_PATH
    if not os.path.isdir(search_root):
        return f"Folder not found: {folder}. Valid folders: {', '.join(VAULT_FOLDERS)}"

    notes = []
    for root, _, files in os.walk(search_root):
        for f in files:
            if f.endswith(".md"):
                full = os.path.join(root, f)
                rel = os.path.relpath(full, VAULT_PATH)
                notes.append(rel)

    if not notes:
        return "No notes found there."
    return "\n".join(sorted(notes))


def read_vault_note(path: str) -> str:
    full = os.path.join(VAULT_PATH, path)
    if not os.path.isfile(full):
        return f"Note not found: {path}"
    with open(full, "r", encoding="utf-8") as f:
        return f.read()


def write_vault_note(folder: str, filename: str, content: str, append: bool = False) -> str:
    if folder not in VAULT_FOLDERS:
        return f"Invalid folder '{folder}'. Valid folders: {', '.join(VAULT_FOLDERS)}"
    if not filename.endswith(".md"):
        filename += ".md"

    folder_path = os.path.join(VAULT_PATH, folder)
    os.makedirs(folder_path, exist_ok=True)
    full = os.path.join(folder_path, filename)

    mode = "a" if append and os.path.isfile(full) else "w"
    with open(full, mode, encoding="utf-8") as f:
        if mode == "a":
            f.write("\n\n" + content)
        else:
            f.write(content)

    action = "Appended to" if mode == "a" else "Created"
    return f"{action} note: {folder}/{filename}"


AGENT_SCRIPT_PROMPT = """Write a complete, standalone Python script for a new autonomous agent named
"{name}". Its purpose: {purpose}

Follow this exact structure and set of conventions (this mirrors an existing working agent,
money_clips_agent.py, in the same project):

1. A module docstring explaining what the agent does and how to run it locally
   (`python3 {name}.py`).
2. Read secrets from environment variables only — CLAUDE_API_KEY, SUPABASE_URL, SUPABASE_KEY —
   using os.environ.get(...), and sys.exit() with a clear message if any are missing. Never
   hardcode secrets or placeholder keys.
3. Use `from anthropic import Anthropic` and `from supabase import create_client`.
4. Do the agent's actual work (call Claude as needed for any generation/analysis the purpose
   requires).
5. Save its result as JSON to the Supabase table "Agent Outputs" (note the space and capital
   letters — must match exactly), with columns agent_name (= "{name}") and output_text (the
   JSON-encoded result as a string).
6. A main() function that prints progress as it runs, and an `if __name__ == "__main__":` guard.
7. Model to use for any Claude calls: claude-sonnet-5.

Return ONLY the raw Python source code for the file — no markdown fences, no commentary before
or after."""


def create_new_agent(name: str, purpose: str) -> str:
    if not re.fullmatch(r"[a-z][a-z0-9_]*", name or ""):
        return (
            f"Invalid agent name '{name}'. Use lowercase letters, numbers, and underscores only, "
            "starting with a letter (e.g. 'stock_watch_agent')."
        )

    os.makedirs(AGENTS_DIR, exist_ok=True)
    dest = os.path.join(AGENTS_DIR, f"{name}.py")
    if os.path.exists(dest):
        return f"An agent script already exists at agents/{name}.py — pick a different name or ask Alex whether to overwrite it."

    message = claude.messages.create(
        model="claude-sonnet-5",
        max_tokens=4096,
        messages=[
            {
                "role": "user",
                "content": AGENT_SCRIPT_PROMPT.format(name=name, purpose=purpose),
            }
        ],
    )
    if message.stop_reason == "max_tokens":
        return (
            f"Draft for '{name}' got cut off before finishing (hit the token limit) — "
            "no file was written. Try again, maybe with a narrower purpose description."
        )

    script = next(block.text for block in message.content if block.type == "text").strip()
    script = script.replace("```python", "").replace("```", "").strip()

    with open(dest, "w", encoding="utf-8") as f:
        f.write(script + "\n")

    return (
        f"Drafted agents/{name}.py — NOT run or deployed. Review the script before executing it "
        "(it will need CLAUDE_API_KEY, SUPABASE_URL, SUPABASE_KEY in the environment to run)."
    )


TOOL_PROPOSAL_PROMPT = """Draft a proposal for a new tool named "{name}" to add to a Flask app called
Jarvis (a personal AI assistant for a guy named Alex). Its purpose: {purpose}

The app's existing tools follow this exact shape — a dict in a TOOLS list (Anthropic tool-use schema:
name, description, input_schema), a matching plain Python function of the same name, and one routing
line in a handle_tool_call(tool_name, tool_input) function that calls it. Available already in scope
if the tool needs them: `claude` (an Anthropic client), `supabase` (a Supabase client), `VAULT_PATH`
(the Obsidian vault root). Don't redeclare these — assume they exist.

Output ONLY the following three things, in this exact order, with no markdown fences and no extra
commentary:

1. A Python dict literal named TOOL_SCHEMA, exactly matching the Anthropic tools schema shape (name,
   description, input_schema), for this one tool.
2. The complete Python function implementing it (a plain, correct, defensively-written function —
   validate any risky inputs, no destructive or external-write side effects unless the purpose
   explicitly calls for something reversible like a database insert).
3. A single-line Python comment showing the exact `if` block to add inside handle_tool_call to route
   to this function, in this form:
   # if tool_name == "{name}":
   #     return {name}(...)"""


def create_new_tool(name: str, purpose: str) -> str:
    if not re.fullmatch(r"[a-z][a-z0-9_]*", name or ""):
        return (
            f"Invalid tool name '{name}'. Use lowercase letters, numbers, and underscores only, "
            "starting with a letter (e.g. 'get_weather')."
        )

    os.makedirs(PROPOSED_TOOLS_DIR, exist_ok=True)
    dest = os.path.join(PROPOSED_TOOLS_DIR, f"{name}.py")
    if os.path.exists(dest):
        return f"A tool proposal already exists at proposed_tools/{name}.py — pick a different name or ask Alex whether to overwrite it."

    message = claude.messages.create(
        model="claude-sonnet-5",
        max_tokens=4096,
        messages=[
            {
                "role": "user",
                "content": TOOL_PROPOSAL_PROMPT.format(name=name, purpose=purpose),
            }
        ],
    )
    if message.stop_reason == "max_tokens":
        return (
            f"Proposal for '{name}' got cut off before finishing (hit the token limit) — "
            "no file was written. Try again, maybe with a narrower purpose description."
        )

    draft = next(block.text for block in message.content if block.type == "text").strip()
    draft = draft.replace("```python", "").replace("```", "").strip()

    header = (
        f'"""\n'
        f"PROPOSED TOOL: {name}\n"
        f"Drafted by Jarvis on request — purpose: {purpose}\n\n"
        f"This is a PROPOSAL ONLY. Nothing here is wired into app.py or the live TOOLS list.\n"
        f"To adopt it, a human (or a future Claude Code session) must manually:\n"
        f"  1. Copy TOOL_SCHEMA below into the TOOLS list in app.py\n"
        f"  2. Copy the {name}() function below into app.py\n"
        f"  3. Add the routing line shown at the bottom into handle_tool_call\n"
        f'"""\n\n'
    )

    with open(dest, "w", encoding="utf-8") as f:
        f.write(header + draft + "\n")

    return (
        f"Drafted proposed_tools/{name}.py — a proposal only. It is NOT part of my live tools yet and "
        "won't be until you (or a Claude Code session) review it and merge the three pieces into app.py "
        "yourself."
    )


def handle_tool_call(tool_name: str, tool_input: dict) -> str:
    if tool_name == "get_recent_agent_outputs":
        return get_recent_agent_outputs(
            agent_name=tool_input.get("agent_name"),
            limit=tool_input.get("limit", 5),
        )
    if tool_name == "log_note":
        return log_note(tool_input["text"])
    if tool_name == "remember":
        return remember(tool_input["fact"])
    if tool_name == "propose_calendar_event":
        return propose_calendar_event(
            title=tool_input["title"],
            start_datetime=tool_input["start_datetime"],
            end_datetime=tool_input["end_datetime"],
            description=tool_input.get("description", ""),
        )
    if tool_name == "list_vault_notes":
        return list_vault_notes(folder=tool_input.get("folder"))
    if tool_name == "read_vault_note":
        return read_vault_note(tool_input["path"])
    if tool_name == "write_vault_note":
        return write_vault_note(
            folder=tool_input["folder"],
            filename=tool_input["filename"],
            content=tool_input["content"],
            append=tool_input.get("append", False),
        )
    if tool_name == "create_new_agent":
        return create_new_agent(
            name=tool_input["name"],
            purpose=tool_input["purpose"],
        )
    if tool_name == "create_new_tool":
        return create_new_tool(
            name=tool_input["name"],
            purpose=tool_input["purpose"],
        )
    if tool_name in CALENDAR_TOOL_SLUGS:
        result = composio.tools.execute(
            slug=tool_name,
            arguments=tool_input,
            user_id=COMPOSIO_USER_ID,
            # Always use the latest tool version — matches what Composio's own
            # agentic execution path does internally for provider tool calls.
            dangerously_skip_version_check=True,
        )
        return json.dumps(result, default=str)
    return f"Unknown tool: {tool_name}"


# ============================================================
# CHAT LOOP
# ============================================================

# Human-friendly status lines shown live in the UI while each tool runs.
TOOL_STATUS_LABELS = {
    "get_recent_agent_outputs": "Checking what your agents have found…",
    "log_note": "Saving that note…",
    "remember": "Committing that to memory…",
    "propose_calendar_event": "Queuing that for your approval…",
    "list_vault_notes": "Looking through your vault…",
    "read_vault_note": "Reading your notes…",
    "write_vault_note": "Writing to your vault…",
    "create_new_agent": "Drafting a new agent…",
    "create_new_tool": "Drafting a new tool proposal…",
    "GOOGLECALENDAR_EVENTS_LIST": "Checking your calendar…",
    "GOOGLECALENDAR_FIND_EVENT": "Searching your calendar…",
    "GOOGLECALENDAR_LIST_CALENDARS": "Checking your calendars…",
    "GOOGLECALENDAR_GET_CURRENT_DATE_TIME": "Checking the time…",
}


def stream_chat(messages: list):
    """Runs the Claude tool-use loop, yielding events as they happen:
    {"type": "text", "delta": ...}    — a chunk of the reply as it's written
    {"type": "status", "label": ...}  — what tool is being used right now
    """
    system_prompt = build_system_prompt()  # fresh memories every request
    while True:
        with claude.messages.stream(
            model="claude-sonnet-5",
            max_tokens=1024,
            system=system_prompt,
            tools=TOOLS,
            messages=messages,
        ) as stream:
            for text in stream.text_stream:
                yield {"type": "text", "delta": text}
            response = stream.get_final_message()

        if response.stop_reason != "tool_use":
            return

        # Model wants to use one or more tools
        messages.append({"role": "assistant", "content": response.content})

        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                yield {
                    "type": "status",
                    "label": TOOL_STATUS_LABELS.get(block.name, "Working on it…"),
                }
                result = handle_tool_call(block.name, block.input)
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    }
                )

        messages.append({"role": "user", "content": tool_results})


# ============================================================
# TODAY'S CALENDAR — cached read-only fetch for the dashboard.
# Cached for 5 minutes so the dashboard's 30-second refresh loop
# doesn't hammer the Google Calendar API.
# ============================================================

LOCAL_TZ = ZoneInfo("America/New_York")
_calendar_cache = {"events": None, "fetched_at": 0.0}


def get_today_events() -> list:
    if _calendar_cache["events"] is not None and time.time() - _calendar_cache["fetched_at"] < 300:
        return _calendar_cache["events"]

    now = datetime.now(LOCAL_TZ)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)

    try:
        resp = composio.tools.execute(
            slug="GOOGLECALENDAR_EVENTS_LIST",
            arguments={
                "calendarId": "primary",
                "timeMin": day_start.isoformat(),
                "timeMax": day_end.isoformat(),
                "singleEvents": True,
                "orderBy": "startTime",
            },
            user_id=COMPOSIO_USER_ID,
            dangerously_skip_version_check=True,
        )
        items = (resp.get("data") or {}).get("items") or []
        events = []
        for ev in items:
            start = ev.get("start") or {}
            end = ev.get("end") or {}
            events.append(
                {
                    "title": ev.get("summary", "(untitled)"),
                    "start": start.get("dateTime") or start.get("date"),
                    "end": end.get("dateTime") or end.get("date"),
                    "all_day": "date" in start and "dateTime" not in start,
                }
            )
        _calendar_cache["events"] = events
        _calendar_cache["fetched_at"] = time.time()
        return events
    except Exception as e:
        print(f"Warning: couldn't fetch today's calendar events: {e}")
        return _calendar_cache["events"] or []


# ============================================================
# DASHBOARD DATA — read-only summary of system state.
# Doubles as the review surface for self-expansion drafts (create_new_agent /
# create_new_tool both land here as "pending" until Alex reviews them).
# ============================================================

def _list_py_files(dir_path: str) -> list:
    if not os.path.isdir(dir_path):
        return []
    return sorted(f for f in os.listdir(dir_path) if f.endswith(".py"))


def get_dashboard_data() -> dict:
    outputs = (
        supabase.table("Agent Outputs")
        .select("*")
        .order("created_at", desc=True)
        .limit(28)
        .execute()
        .data
        or []
    )
    outputs = [r for r in outputs if r["agent_name"] not in INTERNAL_AGENT_NAMES][:8]

    all_notes = []
    if os.path.isdir(VAULT_PATH):
        for root, _, files in os.walk(VAULT_PATH):
            for f in files:
                if f.endswith(".md"):
                    all_notes.append(os.path.relpath(os.path.join(root, f), VAULT_PATH))
    all_notes.sort()

    return {
        "recent_outputs": outputs,
        "vault_notes": all_notes,
        "pending_agents": _list_py_files(AGENTS_DIR),
        "pending_tools": _list_py_files(PROPOSED_TOOLS_DIR),
        "today_events": get_today_events(),
        "pending_actions": get_pending_actions(),
        "memories": load_memories(),
    }


# ============================================================
# ROUTES
# ============================================================

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/dashboard")
def dashboard():
    return render_template("dashboard.html")


@app.route("/api/dashboard")
def api_dashboard():
    return jsonify(get_dashboard_data())


@app.route("/api/approve", methods=["POST"])
def api_approve():
    data = request.get_json()
    row_id = data.get("id")
    decision = data.get("decision")
    if decision not in ("approve", "deny") or not isinstance(row_id, int):
        return jsonify({"ok": False, "error": "Bad request."}), 400
    result = resolve_pending_action(row_id, decision)
    return jsonify(result), (200 if result.get("ok") else 422)


def _normalize_for_api(messages: list) -> list:
    """Claude requires strictly alternating roles starting with 'user'.
    Merge consecutive same-role messages and drop a leading assistant turn
    (can happen if a past request died between saving the two sides)."""
    cleaned = []
    for msg in messages:
        if cleaned and cleaned[-1]["role"] == msg["role"]:
            cleaned[-1] = {"role": msg["role"], "content": cleaned[-1]["content"] + "\n" + msg["content"]}
        else:
            cleaned.append(dict(msg))
    while cleaned and cleaned[0]["role"] != "user":
        cleaned.pop(0)
    return cleaned


@app.route("/chat", methods=["POST"])
def chat():
    data = request.get_json()
    user_message = data.get("message", "")

    history = load_chat_history()
    messages = _normalize_for_api(history + [{"role": "user", "content": user_message}])
    save_chat_message("user", user_message)

    def generate():
        reply_parts = []
        try:
            for event in stream_chat(messages):
                if event.get("type") == "text":
                    reply_parts.append(event["delta"])
                yield json.dumps(event) + "\n"
        except Exception as e:
            yield json.dumps({"type": "error", "message": str(e)}) + "\n"
        finally:
            if reply_parts:
                save_chat_message("assistant", "".join(reply_parts))

    return Response(
        stream_with_context(generate()),
        mimetype="application/x-ndjson",
        # Tell proxies (Coolify's traefik included) not to buffer the stream.
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/api/history")
def api_history():
    return jsonify({"messages": load_chat_history(limit=80)})


@app.route("/api/history/clear", methods=["POST"])
def api_history_clear():
    supabase.table("Agent Outputs").insert(
        {"agent_name": "jarvis_chat_clear", "output_text": "cleared"}
    ).execute()
    return jsonify({"ok": True})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
