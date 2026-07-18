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

from flask import Flask, request, jsonify, render_template
from anthropic import Anthropic
from supabase import create_client

# ---- CONFIG — reads from environment variables ----
CLAUDE_API_KEY = os.environ["CLAUDE_API_KEY"]
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
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
# ----------------------------------------------------

app = Flask(__name__)
claude = Anthropic(api_key=CLAUDE_API_KEY)
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

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
Always tell him the new agent needs his review before he runs or deploys it himself."""


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
]


def get_recent_agent_outputs(agent_name: str = None, limit: int = 5) -> str:
    query = supabase.table("Agent Outputs").select("*").order("created_at", desc=True).limit(limit)
    if agent_name:
        query = query.eq("agent_name", agent_name)
    result = query.execute()
    if not result.data:
        return "No agent outputs found."
    return json.dumps(result.data, indent=2, default=str)


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


def handle_tool_call(tool_name: str, tool_input: dict) -> str:
    if tool_name == "get_recent_agent_outputs":
        return get_recent_agent_outputs(
            agent_name=tool_input.get("agent_name"),
            limit=tool_input.get("limit", 5),
        )
    if tool_name == "log_note":
        return log_note(tool_input["text"])
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
    return f"Unknown tool: {tool_name}"


# ============================================================
# CHAT LOOP
# ============================================================

def run_chat(messages: list) -> str:
    """Runs the Claude tool-use loop until it produces a final text answer."""
    while True:
        response = claude.messages.create(
            model="claude-sonnet-5",
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages,
        )

        if response.stop_reason != "tool_use":
            # Final answer — extract the text block
            text_blocks = [b.text for b in response.content if b.type == "text"]
            return "\n".join(text_blocks)

        # Model wants to use one or more tools
        messages.append({"role": "assistant", "content": response.content})

        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
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
# ROUTES
# ============================================================

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/chat", methods=["POST"])
def chat():
    data = request.get_json()
    user_message = data.get("message", "")
    history = data.get("history", [])  # list of {role, content} from the client

    messages = history + [{"role": "user", "content": user_message}]
    reply = run_chat(messages)

    return jsonify({"reply": reply})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
