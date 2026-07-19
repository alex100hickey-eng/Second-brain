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
import threading
import subprocess
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

# Read-only Gmail tools only — same whitelist-by-slug pattern as the calendar.
# Nothing that sends, drafts, deletes, labels, forwards, or touches settings is
# reachable; reading mail is read-only, so it runs without the approval gate.
GMAIL_TOOL_SLUGS = [
    "GMAIL_FETCH_EMAILS",
    "GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID",
    "GMAIL_FETCH_MESSAGE_BY_THREAD_ID",
    "GMAIL_LIST_THREADS",
    "GMAIL_LIST_LABELS",
    "GMAIL_GET_PROFILE",
]
try:
    GMAIL_TOOLS = composio.tools.get(user_id=COMPOSIO_USER_ID, tools=GMAIL_TOOL_SLUGS)
except Exception as e:
    print(f"Warning: couldn't fetch Composio gmail tools at startup: {e}")
    GMAIL_TOOLS = []

# Rows in "Agent Outputs" with these agent_name values are internal storage for the
# chat brain itself (memories, pending approvals, chat history) — not real agent
# outputs. Keep them out of anything that lists agent activity.
INTERNAL_AGENT_NAMES = {
    "jarvis_memory",
    "jarvis_memory_forgotten",  # soft-deleted memories — kept, never shown
    "jarvis_pending_action",
    "jarvis_chat",
    "jarvis_chat_clear",
    "jarvis_task",  # delegated background tasks (see delegate_task)
    "jarvis_managed_task",  # Task Manager subsystem runs (see task_manager.py)
    "jarvis_taskman_step",  # per-step audit trail for managed tasks
    "jarvis_taskman_kill",  # kill-switch rows for managed tasks
    "jarvis_file_undo",  # reversible-file-op undo trail for managed tasks
}

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
asks you to gain some new ability. This writes a proposal file to proposed_tools/ — it does
NOT make the tool live. The full self-expansion pipeline is: create_new_tool drafts it →
adopt_tool queues it for Alex's dashboard approval → his approval pushes it to a review
branch on GitHub → a human merges it → it loads as a live extension on the next restart.
Every new capability passes through Alex's hands twice before you can use it. Explain this
honestly whenever it comes up; never imply a drafted tool is usable.

You have read-only access to Alex's Google Calendar (GOOGLECALENDAR_* tools) — you can list
and search his events, and check calendars/current time, to answer questions about his
schedule.

You have read-only access to Alex's Gmail (GMAIL_* tools) — you can search his email
(GMAIL_FETCH_EMAILS supports Gmail query syntax like from:, subject:, newer_than:2d),
read specific messages and threads, and list labels. You cannot send, draft, delete, or
modify anything in his mailbox — reading only. Use it to answer questions about his email,
find things he's looking for, or summarize what needs attention.

You have a persistent memory. Facts you've saved appear below under "Saved memories" — treat
them as things you know about Alex. When he tells you something worth keeping long-term (a
preference, a goal, a recurring commitment, a fact about his life), or asks you to remember
something, save it with the remember tool. Keep each memory to one short, self-contained
sentence. Don't save throwaway context or things already in your memories.

When Alex wants a decision analyzed or asks whether something is worth doing, you can run it
through his decision council with the deliberate tool — an Advocate argues for it, a Critic
independently argues against it, and a Judge rules. Present the full deliberation to him.

You can propose adding events to Alex's calendar with propose_calendar_event, but you cannot
create them directly. A proposal goes into a pending-approval queue that Alex reviews on his
dashboard — nothing touches his calendar until he clicks Approve there. When you propose an
event, tell him it's waiting for his approval on the dashboard. This approve-first rule
exists because calendar changes are consequential; never imply an event is booked before
he's approved it.

You can help clean Alex's Downloads folder (only when running on his Mac): scan_downloads
lists junk candidates (read-only), and propose_file_cleanup queues chosen files for the same
dashboard approval. On approval files move to the macOS Trash — you never permanently delete
anything, and nothing moves without his explicit Approve click.

You can work on things in the background with delegate_task: hand off a multi-step job
(research, digging through notes/agents/calendar, drafting a summary) and it runs on its own
while the conversation continues — the result lands in this chat thread and on the dashboard
when it finishes. Use it when Alex says "get back to me", "work on this in the background",
or the job would take a while. Background runs follow the exact same rules you do — anything
consequential still goes through the approval queue. Use check_delegated_tasks to see how
past tasks went. Don't delegate trivial one-tool lookups — just do those directly.

For bigger autonomous goals there's the Task Manager (run_managed_task): a Prompter extracts
the goal and candidate guardrails, the Guardrail Council (same Advocate/Critic/Judge pattern
as deliberate) rules on each guardrail, and a worker then pursues the goal step by step within
those guardrails, logging every action. Choose it over delegate_task when a goal needs
guardrails, many steps, or an audit trail (e.g. anything involving money, lots of files, or
open-ended work). Hard limits always hold regardless of council verdicts: spending money,
creating accounts, sending anything externally, and file deletion pause for Alex's dashboard
approval. check_managed_tasks shows status; stop_managed_task is the kill switch. Tasks can
target runtime 'local' (his Mac — required for anything touching his files) or 'server'."""


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
        "name": "scan_downloads",
        "description": (
            "Look through Alex's Downloads folder (read-only) and list cleanup candidates — old and/or "
            "large files. Use when he asks what's cluttering his Downloads or wants to clean up files. "
            "Only works on his Mac (the local instance); the server has no access to his files. This "
            "tool only LOOKS — nothing is moved or deleted."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "days_old": {
                    "type": "integer",
                    "description": "Only list files older than this many days. Default 30.",
                },
                "min_size_mb": {
                    "type": "number",
                    "description": "Only list files at least this big in MB. Default 0 (any size).",
                },
            },
        },
    },
    {
        "name": "propose_file_cleanup",
        "description": (
            "Queue a file-cleanup for Alex's approval: the named files from his Downloads folder would "
            "be moved to the macOS Trash (never permanently deleted) — but ONLY after he clicks Approve "
            "on the dashboard. Use after scan_downloads, with the specific files he agrees are junk. "
            "Always tell him it's waiting for his approval."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "filenames": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Filenames (relative to Downloads) to include, e.g. ['old-installer.dmg'].",
                }
            },
            "required": ["filenames"],
        },
    },
    {
        "name": "adopt_tool",
        "description": (
            "Start the adoption process for a tool proposal you previously drafted with create_new_tool. "
            "Queues an approval action; when Alex approves it on the dashboard, the proposal is committed "
            "to a git branch (jarvis/tool-<name>) and pushed to GitHub for review. It does NOT merge to "
            "main, does NOT deploy, and the tool does NOT become live — a human must review and merge the "
            "branch, then the extension loads on the next restart. Only works on Alex's Mac."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Name of an existing proposal in proposed_tools/, e.g. 'get_word_count'.",
                }
            },
            "required": ["name"],
        },
    },
    {
        "name": "forget_memory",
        "description": (
            "Remove a fact from your saved memories, when Alex asks you to forget something or a memory "
            "is wrong/outdated. Give a distinctive fragment of the memory's text; it only forgets when "
            "exactly one memory matches (otherwise it lists the matches so you can be more specific). "
            "Forgotten memories are archived, not destroyed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "matching_text": {
                    "type": "string",
                    "description": "A distinctive fragment of the memory to forget, e.g. 'football practice'.",
                }
            },
            "required": ["matching_text"],
        },
    },
    {
        "name": "deliberate",
        "description": (
            "Run an idea or decision through Alex's three-agent decision council: an Advocate builds "
            "the strongest case FOR, a Critic independently builds the strongest case AGAINST (neither "
            "sees the other's arguments), and a Judge weighs both and delivers a verdict with reasoning. "
            "Use when Alex asks whether something is worth doing, wants a decision analyzed, or says to "
            "'run it through the council'. Purely analytical — produces a recommendation, takes no action."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "idea": {
                    "type": "string",
                    "description": "The idea or decision to deliberate, stated plainly, e.g. 'buying a $900 camera for the YouTube channel'.",
                },
                "context": {
                    "type": "string",
                    "description": "Optional relevant context Alex gave (budget, goals, constraints).",
                },
            },
            "required": ["idea"],
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
        "name": "delegate_task",
        "description": (
            "Hand off a multi-step job to run in the background while the conversation continues. "
            "The task runs on its own with the same tools and the same approval rules (consequential "
            "actions still queue for Alex's approval), and its result appears in the chat thread and "
            "on the dashboard when done. Use for research, multi-source digests, or anything Alex says "
            "to 'work on and get back to me'. Not for trivial single lookups — do those directly."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "description": {
                    "type": "string",
                    "description": "A complete, self-contained description of the job — what to do, what a good result looks like, any constraints Alex gave. The background run sees ONLY this text, not the conversation.",
                }
            },
            "required": ["description"],
        },
    },
    {
        "name": "check_delegated_tasks",
        "description": "See recent background tasks and their status (queued / running / done / failed) and results. Use when Alex asks how a delegated task is going or what it found.",
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "How many recent tasks to show. Default 5.",
                }
            },
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
] + CALENDAR_TOOLS + GMAIL_TOOLS

# ---- EXTENSIONS: tools Jarvis drafted that Alex adopted (see adopt_tool). ----
# Each extensions/*.py defines TOOL_SCHEMA plus a function named after it.
# Files only land here through the propose → approve → branch → merge pipeline,
# so by the time they load they've been human-reviewed. A broken extension is
# skipped with a warning rather than taking the app down.
EXTENSIONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "extensions")
EXTENSION_FUNCS = {}
if os.path.isdir(EXTENSIONS_DIR):
    import importlib.util

    for _fn in sorted(os.listdir(EXTENSIONS_DIR)):
        if not _fn.endswith(".py"):
            continue
        try:
            _spec = importlib.util.spec_from_file_location(f"ext_{_fn[:-3]}", os.path.join(EXTENSIONS_DIR, _fn))
            _mod = importlib.util.module_from_spec(_spec)
            # Shared context the proposal format is told it can assume exists.
            _mod.__dict__.update(
                {"claude": claude, "supabase": supabase, "VAULT_PATH": VAULT_PATH, "os": os, "json": json}
            )
            _spec.loader.exec_module(_mod)
            _schema = _mod.TOOL_SCHEMA
            EXTENSION_FUNCS[_schema["name"]] = getattr(_mod, _schema["name"])
            TOOLS.append(_schema)
            print(f"Loaded extension tool: {_schema['name']}")
        except Exception as e:
            print(f"Warning: failed to load extension {_fn}: {e}")


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


def forget_memory(matching_text: str) -> str:
    result = (
        supabase.table("Agent Outputs")
        .select("id, output_text")
        .eq("agent_name", "jarvis_memory")
        .ilike("output_text", f"%{matching_text}%")
        .execute()
    )
    matches = result.data or []
    if not matches:
        return f"No saved memory matches '{matching_text}'."
    if len(matches) > 1:
        listing = "\n".join(f"- {m['output_text']}" for m in matches)
        return f"{len(matches)} memories match — be more specific:\n{listing}"
    # Soft-delete: retag the row so it disappears from memory loading but is recoverable.
    supabase.table("Agent Outputs").update({"agent_name": "jarvis_memory_forgotten"}).eq(
        "id", matches[0]["id"]
    ).execute()
    return f"Forgotten: {matches[0]['output_text']}"


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


DOWNLOADS_DIR = os.path.expanduser("~/Downloads")


def scan_downloads(days_old: int = 30, min_size_mb: float = 0) -> str:
    if not os.path.isdir(DOWNLOADS_DIR):
        return "No Downloads folder here — file scanning only works when I'm running on Alex's Mac."
    now = time.time()
    candidates = []
    for name in os.listdir(DOWNLOADS_DIR):
        if name.startswith("."):
            continue
        full = os.path.join(DOWNLOADS_DIR, name)
        if not os.path.isfile(full):
            continue
        st = os.stat(full)
        age_days = (now - st.st_mtime) / 86400
        size_mb = st.st_size / 1_000_000
        if age_days >= days_old and size_mb >= min_size_mb:
            candidates.append((size_mb, age_days, name))
    if not candidates:
        return f"Nothing in Downloads is both older than {days_old} days and at least {min_size_mb} MB."
    candidates.sort(reverse=True)
    lines = [f"- {name} ({size:.1f} MB, {int(age)} days old)" for size, age, name in candidates[:40]]
    total = sum(c[0] for c in candidates)
    return (
        f"{len(candidates)} cleanup candidates in Downloads ({total:.0f} MB total):\n"
        + "\n".join(lines)
        + "\n\nNothing has been touched — use propose_file_cleanup to queue any of these for approval."
    )


def propose_file_cleanup(filenames: list) -> str:
    if not os.path.isdir(DOWNLOADS_DIR):
        return "No Downloads folder here — file cleanup only works when I'm running on Alex's Mac."
    valid, missing = [], []
    for name in filenames:
        # Confine strictly to files directly inside ~/Downloads — no traversal.
        full = os.path.realpath(os.path.join(DOWNLOADS_DIR, os.path.basename(name)))
        if os.path.dirname(full) == os.path.realpath(DOWNLOADS_DIR) and os.path.isfile(full):
            valid.append(os.path.basename(name))
        else:
            missing.append(name)
    if not valid:
        return f"None of those files exist in Downloads: {', '.join(filenames)}"

    total_mb = sum(os.path.getsize(os.path.join(DOWNLOADS_DIR, n)) for n in valid) / 1_000_000
    action = {
        "action": "clean_files",
        "files": valid,
        "display": f"[Mac only] Move {len(valid)} file(s) to Trash from Downloads ({total_mb:.0f} MB): "
                   + ", ".join(valid[:5]) + ("…" if len(valid) > 5 else ""),
        "status": "pending",
    }
    supabase.table("Agent Outputs").insert(
        {"agent_name": "jarvis_pending_action", "output_text": json.dumps(action)}
    ).execute()
    note = f" (couldn't find: {', '.join(missing)})" if missing else ""
    return (
        f"Queued for approval: moving {len(valid)} file(s) to Trash{note}. "
        "Nothing moves until Alex approves it on the dashboard. Files go to the Trash, never permanent deletion."
    )


def _execute_clean_files(action: dict) -> dict:
    moved, failed = [], []
    for name in action.get("files", []):
        full = os.path.realpath(os.path.join(DOWNLOADS_DIR, os.path.basename(name)))
        if os.path.dirname(full) != os.path.realpath(DOWNLOADS_DIR) or not os.path.isfile(full):
            failed.append(name)
            continue
        # Finder's delete = real Trash with Put Back support.
        script = f'tell application "Finder" to delete POSIX file "{full}"'
        result = subprocess.run(["osascript", "-e", script], capture_output=True, timeout=30)
        (moved if result.returncode == 0 else failed).append(name)
    if moved and not failed:
        return {"ok": True, "detail": f"Moved to Trash: {', '.join(moved)}"}
    if moved:
        return {"ok": True, "detail": f"Moved: {', '.join(moved)}; failed: {', '.join(failed)}"}
    return {"ok": False, "error": f"Couldn't move any files (this only works on Alex's Mac): {', '.join(failed)}"}


REPO_ROOT = os.path.realpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))


def adopt_tool(name: str) -> str:
    if not re.fullmatch(r"[a-z][a-z0-9_]*", name or ""):
        return f"Invalid tool name '{name}'."
    src = os.path.join(PROPOSED_TOOLS_DIR, f"{name}.py")
    if not os.path.isfile(src):
        return f"No proposal found at proposed_tools/{name}.py — draft it with create_new_tool first."
    if not os.path.isdir(os.path.join(REPO_ROOT, ".git")):
        return "No git repo here — tool adoption only works when I'm running on Alex's Mac."

    action = {
        "action": "adopt_tool",
        "name": name,
        "display": f"[Mac only] Adopt tool '{name}': commit proposal to branch jarvis/tool-{name} on GitHub for review (no merge, no deploy)",
        "status": "pending",
    }
    supabase.table("Agent Outputs").insert(
        {"agent_name": "jarvis_pending_action", "output_text": json.dumps(action)}
    ).execute()
    return (
        f"Queued for approval: adopting '{name}'. If Alex approves, the proposal goes to a review branch "
        "on GitHub — it still won't be live until a human merges it and the app restarts."
    )


def _execute_adopt_tool(action: dict) -> dict:
    import tempfile, shutil

    name = action["name"]
    src = os.path.join(PROPOSED_TOOLS_DIR, f"{name}.py")
    if not os.path.isfile(src):
        return {"ok": False, "error": f"Proposal proposed_tools/{name}.py no longer exists."}
    if not os.path.isdir(os.path.join(REPO_ROOT, ".git")):
        return {"ok": False, "error": "No git repo (this only works on Alex's Mac)."}

    branch = f"jarvis/tool-{name}"
    wt = tempfile.mkdtemp(prefix="jarvis-adopt-")
    try:
        def git(*args, cwd=REPO_ROOT):
            r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=60)
            if r.returncode != 0:
                raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()[:300]}")
            return r.stdout

        # Work in an isolated worktree so Alex's checkout is never disturbed.
        git("worktree", "add", "-B", branch, wt, "main")
        dest_dir = os.path.join(wt, "second-brain-chat", "extensions")
        os.makedirs(dest_dir, exist_ok=True)
        shutil.copyfile(src, os.path.join(dest_dir, f"{name}.py"))
        git("add", "-A", cwd=wt)
        git("commit", "-m", f"Jarvis proposes extension tool: {name}\n\nDrafted via create_new_tool, adopted via approval queue.", cwd=wt)
        git("push", "-u", "origin", branch, cwd=wt)
        return {"ok": True, "detail": f"Pushed branch {branch}"}
    except (RuntimeError, subprocess.TimeoutExpired) as e:
        return {"ok": False, "error": str(e)[:400]}
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", wt], cwd=REPO_ROOT,
                       capture_output=True, timeout=30)
        shutil.rmtree(wt, ignore_errors=True)


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


def get_decided_actions(limit: int = 8) -> list:
    """Recently approved/denied/failed actions, newest first — the gate's paper trail."""
    result = (
        supabase.table("Agent Outputs")
        .select("*")
        .eq("agent_name", "jarvis_pending_action")
        .order("id", desc=True)
        .limit(40)
        .execute()
    )
    decided = []
    for row in result.data or []:
        try:
            action = json.loads(row["output_text"])
        except (json.JSONDecodeError, TypeError):
            continue
        if action.get("status") in ("approved", "denied", "failed"):
            decided.append(
                {
                    "display": action.get("display", "(unknown action)"),
                    "status": action["status"],
                    "created_at": row.get("created_at"),
                }
            )
        if len(decided) >= limit:
            break
    return decided


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

    # Approved — execute by action type. This is the ONLY place queued actions run.
    if action.get("action") == "adopt_tool":
        result = _execute_adopt_tool(action)
        action["status"] = "approved" if result["ok"] else "failed"
        if not result["ok"]:
            action["error"] = result["error"]
        supabase.table("Agent Outputs").update({"output_text": json.dumps(action)}).eq("id", row_id).execute()
        return {"ok": result["ok"], "status": action["status"], **({} if result["ok"] else {"error": result["error"]})}

    if action.get("action") in ("promote_tool", "shell_command"):
        # Nothing executes HERE: the paused Task Manager run is polling this
        # row and performs the hot-load / shell run itself, on whichever
        # machine (Mac or server) the task actually lives on.
        action["status"] = "approved"
        supabase.table("Agent Outputs").update({"output_text": json.dumps(action)}).eq("id", row_id).execute()
        return {"ok": True, "status": "approved"}

    if action.get("action") == "clean_files":
        result = _execute_clean_files(action)
        if not result["ok"]:
            action["status"] = "failed"
            action["error"] = result["error"][:500]
            supabase.table("Agent Outputs").update({"output_text": json.dumps(action)}).eq("id", row_id).execute()
            return {"ok": False, "error": result["error"]}
        action["status"] = "approved"
        supabase.table("Agent Outputs").update({"output_text": json.dumps(action)}).eq("id", row_id).execute()
        return {"ok": True, "status": "approved"}

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


# ============================================================
# DECISION COUNCIL — three independent Claude calls: Advocate (pro),
# Critic (con, blind to the Advocate), and a Judge who weighs both.
# Analysis only; never takes an action.
# ============================================================

def _council_call(system: str, user: str) -> str:
    msg = claude.messages.create(
        model="claude-sonnet-5",
        max_tokens=1200,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return next((b.text for b in msg.content if b.type == "text"), "").strip()


def deliberate(idea: str, context: str = "") -> str:
    subject = f"Idea: {idea}" + (f"\nContext: {context}" if context else "")

    pro = _council_call(
        "You are the Advocate on a personal decision council. Build the strongest honest case FOR "
        "the idea — concrete benefits, upside scenarios, what's lost by not doing it. Be persuasive "
        "but truthful; don't invent facts. 4-8 tight bullet points.",
        subject,
    )
    con = _council_call(
        "You are the Critic on a personal decision council. Build the strongest honest case AGAINST "
        "the idea — costs, risks, failure modes, better alternatives, hidden downsides. Be sharp but "
        "truthful; don't invent facts. 4-8 tight bullet points.",
        subject,
    )
    verdict = _council_call(
        "You are the Judge on a personal decision council. You receive an idea plus an Advocate's case "
        "for it and a Critic's case against it, prepared independently. Weigh both fairly, note which "
        "arguments are strongest and which are weak, and rule: WORTH IT, NOT WORTH IT, or WORTH IT IF "
        "(with the condition). Give your ruling first, then 3-6 sentences of reasoning, then one line "
        "on what evidence would change your mind.",
        f"{subject}\n\n--- ADVOCATE'S CASE ---\n{pro}\n\n--- CRITIC'S CASE ---\n{con}",
    )

    return (
        f"## Council deliberation: {idea}\n\n"
        f"### Advocate — the case for\n{pro}\n\n"
        f"### Critic — the case against\n{con}\n\n"
        f"### Judge's ruling\n{verdict}"
    )


# ============================================================
# BACKGROUND TASKS — delegate_task queues a job as a jarvis_task row;
# a single daemon worker thread claims queued tasks (atomically, via a
# compare-and-swap on the row's JSON) and runs each through the same
# tool-use loop the chat uses. Same tools, same rules: consequential
# actions still land in the approval queue. Results are written back to
# the row, surfaced on the dashboard, and dropped into the chat thread.
# ============================================================

# Tools a background run may NOT use: no recursive delegation (a task that
# delegates tasks could multiply forever), and no reason to self-inspect.
BACKGROUND_EXCLUDED_TOOLS = {
    "delegate_task",
    "check_delegated_tasks",
    # A background task spawning managed tasks (or vice versa) could multiply
    # runaway work — autonomous runs never get the task-spawning tools.
    "run_managed_task",
    "check_managed_tasks",
    "stop_managed_task",
}

BACKGROUND_TASK_PROMPT_SUFFIX = """

--- BACKGROUND MODE ---
You are running as a delegated background task, not in a live conversation. Alex is not
watching and cannot answer questions — work with what the task description gives you.
Do the job fully, then write your final answer as a clear, self-contained report (it will
be posted into the chat thread for Alex to read later). All the usual rules apply:
consequential actions still go through the approval queue, and you must say so in your
report if you queued any."""


def delegate_task(description: str) -> str:
    if not description or not description.strip():
        return "Task description is empty — nothing queued."
    task = {
        "description": description.strip(),
        "status": "queued",
        "result": None,
        "queued_at": datetime.now(ZoneInfo("America/New_York")).isoformat(),
    }
    supabase.table("Agent Outputs").insert(
        {"agent_name": "jarvis_task", "output_text": json.dumps(task)}
    ).execute()
    return (
        "Background task queued — it's running on its own now. The result will appear in "
        "this chat and on the dashboard when it finishes."
    )


def check_delegated_tasks(limit: int = 5) -> str:
    result = (
        supabase.table("Agent Outputs")
        .select("*")
        .eq("agent_name", "jarvis_task")
        .order("id", desc=True)
        .limit(limit)
        .execute()
    )
    rows = result.data or []
    if not rows:
        return "No background tasks have been delegated yet."
    lines = []
    for row in rows:
        try:
            task = json.loads(row["output_text"])
        except (json.JSONDecodeError, TypeError):
            continue
        line = f"[{task.get('status', '?')}] {task.get('description', '')[:120]}"
        if task.get("status") == "done" and task.get("result"):
            line += f"\n  Result: {task['result'][:500]}"
        if task.get("status") == "failed" and task.get("error"):
            line += f"\n  Error: {task['error'][:200]}"
        lines.append(line)
    return "\n\n".join(lines) if lines else "No readable background tasks found."


def get_background_tasks(limit: int = 6) -> list:
    """Recent background tasks for the dashboard, newest first."""
    result = (
        supabase.table("Agent Outputs")
        .select("*")
        .eq("agent_name", "jarvis_task")
        .order("id", desc=True)
        .limit(limit)
        .execute()
    )
    tasks = []
    for row in result.data or []:
        try:
            task = json.loads(row["output_text"])
        except (json.JSONDecodeError, TypeError):
            continue
        tasks.append(
            {
                "description": task.get("description", "")[:160],
                "status": task.get("status", "?"),
                "result": (task.get("result") or "")[:400],
                "created_at": row.get("created_at"),
            }
        )
    return tasks


def _run_background_task(row_id: int, task: dict) -> None:
    """Run one claimed task through a non-streaming Claude tool-use loop."""
    tools = [t for t in TOOLS if t.get("name") not in BACKGROUND_EXCLUDED_TOOLS]
    messages = [{"role": "user", "content": f"Background task:\n{task['description']}"}]
    system_prompt = build_system_prompt() + BACKGROUND_TASK_PROMPT_SUFFIX

    final_text = ""
    try:
        for _ in range(20):  # hard cap on tool rounds so a task can't loop forever
            response = claude.messages.create(
                model="claude-sonnet-5",
                max_tokens=2000,
                system=system_prompt,
                tools=tools,
                messages=messages,
            )
            final_text = "".join(b.text for b in response.content if b.type == "text").strip()
            if response.stop_reason != "tool_use":
                break
            messages.append({"role": "assistant", "content": response.content})
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    result = handle_tool_call(block.name, block.input)
                    tool_results.append(
                        {"type": "tool_result", "tool_use_id": block.id, "content": result}
                    )
            messages.append({"role": "user", "content": tool_results})
        else:
            raise RuntimeError("Task hit the 20-round tool limit without finishing.")

        task["status"] = "done"
        task["result"] = final_text or "(task produced no text output)"
    except Exception as e:
        task["status"] = "failed"
        task["error"] = str(e)[:500]

    task["finished_at"] = datetime.now(ZoneInfo("America/New_York")).isoformat()
    supabase.table("Agent Outputs").update({"output_text": json.dumps(task)}).eq(
        "id", row_id
    ).execute()

    # Surface the outcome in the chat thread so Alex sees it next time he looks.
    if task["status"] == "done":
        chat_note = f"**Background task finished** — {task['description'][:120]}\n\n{task['result']}"
    else:
        chat_note = (
            f"**Background task failed** — {task['description'][:120]}\n\n"
            f"Error: {task['error']}"
        )
    try:
        save_chat_message("assistant", chat_note)
    except Exception as e:
        print(f"Warning: couldn't post task result to chat: {e}")


def _claim_task(row_id: int, original_text: str, task: dict) -> bool:
    """Atomically flip a queued task to running. The .eq on the exact original
    JSON makes this a compare-and-swap: if another worker (or another process)
    claimed it first, the row text changed and this update matches nothing."""
    task["status"] = "running"
    task["started_at"] = datetime.now(ZoneInfo("America/New_York")).isoformat()
    result = (
        supabase.table("Agent Outputs")
        .update({"output_text": json.dumps(task)})
        .eq("id", row_id)
        .eq("agent_name", "jarvis_task")
        .eq("output_text", original_text)
        .execute()
    )
    return bool(result.data)


def _task_worker() -> None:
    while True:
        try:
            rows = (
                supabase.table("Agent Outputs")
                .select("*")
                .eq("agent_name", "jarvis_task")
                .order("id", desc=False)
                .limit(20)
                .execute()
                .data
                or []
            )
            for row in rows:
                try:
                    task = json.loads(row["output_text"])
                except (json.JSONDecodeError, TypeError):
                    continue
                if task.get("status") != "queued":
                    continue
                if _claim_task(row["id"], row["output_text"], task):
                    print(f"Background task {row['id']} started: {task['description'][:80]}")
                    _run_background_task(row["id"], task)
                    print(f"Background task {row['id']} finished: {task['status']}")
        except Exception as e:
            print(f"Warning: task worker cycle failed: {e}")
        time.sleep(8)


def start_task_worker() -> None:
    t = threading.Thread(target=_task_worker, daemon=True, name="jarvis-task-worker")
    t.start()


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
    if tool_name == "forget_memory":
        return forget_memory(tool_input["matching_text"])
    if tool_name == "scan_downloads":
        return scan_downloads(
            days_old=tool_input.get("days_old", 30),
            min_size_mb=tool_input.get("min_size_mb", 0),
        )
    if tool_name == "propose_file_cleanup":
        return propose_file_cleanup(tool_input["filenames"])
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
    if tool_name == "deliberate":
        return deliberate(
            idea=tool_input["idea"],
            context=tool_input.get("context", ""),
        )
    if tool_name == "adopt_tool":
        return adopt_tool(tool_input["name"])
    if tool_name == "delegate_task":
        return delegate_task(tool_input["description"])
    if tool_name == "check_delegated_tasks":
        return check_delegated_tasks(limit=tool_input.get("limit", 5))
    if tool_name == "run_managed_task":
        return task_manager.run_managed_task(
            request=tool_input["request"],
            runtime=tool_input.get("runtime"),
        )
    if tool_name == "check_managed_tasks":
        return task_manager.check_managed_tasks(limit=tool_input.get("limit", 5))
    if tool_name == "stop_managed_task":
        return task_manager.stop_managed_task(task_id=tool_input.get("task_id"))
    if tool_name == "undo_file_operations":
        return task_manager.undo_file_operations(task_row_id=tool_input["task_row_id"])
    if tool_name in EXTENSION_FUNCS:
        try:
            return str(EXTENSION_FUNCS[tool_name](**tool_input))
        except Exception as e:
            return f"Extension tool '{tool_name}' failed: {e}"
    if tool_name in CALENDAR_TOOL_SLUGS or tool_name in GMAIL_TOOL_SLUGS:
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
    "forget_memory": "Forgetting that…",
    "propose_calendar_event": "Queuing that for your approval…",
    "deliberate": "Convening the council…",
    "scan_downloads": "Scanning your Downloads…",
    "propose_file_cleanup": "Queuing cleanup for your approval…",
    "list_vault_notes": "Looking through your vault…",
    "read_vault_note": "Reading your notes…",
    "write_vault_note": "Writing to your vault…",
    "create_new_agent": "Drafting a new agent…",
    "create_new_tool": "Drafting a new tool proposal…",
    "adopt_tool": "Queuing tool adoption for your approval…",
    "delegate_task": "Handing that off to run in the background…",
    "check_delegated_tasks": "Checking on background tasks…",
    "run_managed_task": "Convening the council and planning the task…",
    "check_managed_tasks": "Checking on managed tasks…",
    "stop_managed_task": "Hitting the kill switch…",
    "undo_file_operations": "Rolling those file changes back…",
    "GOOGLECALENDAR_EVENTS_LIST": "Checking your calendar…",
    "GOOGLECALENDAR_FIND_EVENT": "Searching your calendar…",
    "GOOGLECALENDAR_LIST_CALENDARS": "Checking your calendars…",
    "GOOGLECALENDAR_GET_CURRENT_DATE_TIME": "Checking the time…",
    "GMAIL_FETCH_EMAILS": "Searching your email…",
    "GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID": "Reading that email…",
    "GMAIL_FETCH_MESSAGE_BY_THREAD_ID": "Reading that thread…",
    "GMAIL_LIST_THREADS": "Looking through your inbox…",
    "GMAIL_LIST_LABELS": "Checking your mail labels…",
    "GMAIL_GET_PROFILE": "Checking your mail account…",
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
        "decided_actions": get_decided_actions(),
        "memories": load_memories(),
        "background_tasks": get_background_tasks(),
        "managed_tasks": task_manager.get_managed_tasks(),
    }


# Start the background-task worker. Under gunicorn (one worker process) this is
# one thread; under the local dev server the reloader may import the module twice,
# but the compare-and-swap claim in _claim_task makes duplicate workers harmless.
start_task_worker()

# Task Manager subsystem (Prompter + Guardrail Council + managed worker) —
# lives in task_manager.py, shares this app's client objects and tool loop.
# Managed runs get the same tool exclusions as background runs (no spawning
# more autonomous work from inside autonomous work).
import task_manager  # noqa: E402 — needs the objects above to exist first

task_manager.init(
    claude_client=claude,
    supabase_client=supabase,
    tool_dispatcher=handle_tool_call,
    system_prompt_builder=build_system_prompt,
    tools_list=TOOLS,
    excluded_tools=BACKGROUND_EXCLUDED_TOOLS,
)
TOOLS.extend(task_manager.TOOL_SCHEMAS)
task_manager.start_managed_worker(post_to_chat=save_chat_message)


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
