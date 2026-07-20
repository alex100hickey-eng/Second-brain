"""
Morning Brief Agent — writes a daily briefing note into the Obsidian vault.

Each run gathers, read-only:
  - Today's Google Calendar events (via Composio)
  - Recent agent outputs and anything pending approval (Supabase)
and writes/overwrites `Schedule/brief-YYYY-MM-DD.md` in the vault. That's its
only side effect — a single markdown note on Alex's own machine.

Run locally: python3 morning_brief_agent.py
Scheduled by: ~/Library/LaunchAgents/com.secondbrain.morningbrief.plist (7:00 AM daily)
"""

import os
import sys
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# Load secrets from the project-root .env (gitignored) before reading os.environ.
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except ImportError:
    pass  # dotenv optional — fall back to the ambient environment

CLAUDE_API_KEY = os.environ.get("CLAUDE_API_KEY")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
COMPOSIO_API_KEY = os.environ.get("COMPOSIO_API_KEY")
for name, val in [("CLAUDE_API_KEY", CLAUDE_API_KEY), ("SUPABASE_URL", SUPABASE_URL),
                  ("SUPABASE_KEY", SUPABASE_KEY), ("COMPOSIO_API_KEY", COMPOSIO_API_KEY)]:
    if not val:
        sys.exit(f"Missing env var: {name}")

from anthropic import Anthropic
from supabase import create_client
from composio import Composio

VAULT_PATH = os.environ.get(
    "VAULT_PATH",
    os.path.expanduser("~/Library/Mobile Documents/com~apple~CloudDocs/Obsidian/Second brain"),
)
LOCAL_TZ = ZoneInfo("America/New_York")
INTERNAL_AGENT_NAMES = {"jarvis_memory", "jarvis_memory_forgotten", "jarvis_pending_action",
                        "jarvis_chat", "jarvis_chat_clear"}


def get_today_events(composio_client):
    now = datetime.now(LOCAL_TZ)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    resp = composio_client.tools.execute(
        slug="GOOGLECALENDAR_EVENTS_LIST",
        arguments={
            "calendarId": "primary",
            "timeMin": start.isoformat(),
            "timeMax": end.isoformat(),
            "singleEvents": True,
            "orderBy": "startTime",
        },
        user_id="alex",
        dangerously_skip_version_check=True,
    )
    events = []
    for ev in (resp.get("data") or {}).get("items") or []:
        s = ev.get("start") or {}
        if "dateTime" in s:
            t = datetime.fromisoformat(s["dateTime"]).astimezone(LOCAL_TZ).strftime("%-I:%M %p")
        else:
            t = "All day"
        events.append(f"{t} — {ev.get('summary', '(untitled)')}")
    return events


def main():
    print("Gathering morning brief data...")
    supabase_client = create_client(SUPABASE_URL, SUPABASE_KEY)
    composio_client = Composio(api_key=COMPOSIO_API_KEY)

    events = get_today_events(composio_client)

    rows = (
        supabase_client.table("Agent Outputs")
        .select("*")
        .order("created_at", desc=True)
        .limit(60)
        .execute()
        .data
        or []
    )
    cutoff = (datetime.now(LOCAL_TZ) - timedelta(hours=24)).isoformat()
    recent_outputs = [
        r for r in rows
        if r["agent_name"] not in INTERNAL_AGENT_NAMES and r["created_at"] >= cutoff
    ]
    pending = []
    for r in rows:
        if r["agent_name"] == "jarvis_pending_action":
            try:
                a = json.loads(r["output_text"])
                if a.get("status") == "pending":
                    pending.append(a.get("display", "(unknown action)"))
            except (json.JSONDecodeError, TypeError):
                pass

    print("Asking Claude to write the brief...")
    claude = Anthropic(api_key=CLAUDE_API_KEY)
    today = datetime.now(LOCAL_TZ).strftime("%A, %B %-d, %Y")
    source_material = (
        f"Date: {today}\n\n"
        f"Calendar events today:\n" + ("\n".join(f"- {e}" for e in events) or "- none") + "\n\n"
        f"Agent outputs from the last 24h:\n"
        + ("\n".join(f"- [{r['agent_name']}] {r['output_text'][:400]}" for r in recent_outputs) or "- none")
        + "\n\n"
        f"Actions awaiting Alex's approval:\n" + ("\n".join(f"- {p}" for p in pending) or "- none")
    )
    msg = claude.messages.create(
        model="claude-sonnet-5",
        max_tokens=1500,
        messages=[{
            "role": "user",
            "content": (
                "Write Alex's morning brief as a markdown note. Sections: a one-line day summary, "
                "'## Today' (his schedule as a clean list), '## Overnight' (what his agents produced, "
                "summarized in plain language — skip raw JSON), and '## Waiting on you' (pending "
                "approvals, or omit the section if none). Keep it tight and scannable — he reads this "
                "over breakfast. No preamble before the first line.\n\n" + source_material
            ),
        }],
    )
    brief = next(b.text for b in msg.content if b.type == "text").strip()

    date_str = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")
    try:
        # Preferred: straight into the vault (iCloud). A launchd-spawned python
        # without Full Disk Access will get PermissionError here — fall back to
        # a local folder so the brief still exists, and say so in the log.
        out_dir = os.path.join(VAULT_PATH, "Schedule")
        os.makedirs(out_dir, exist_ok=True)
        out_path = os.path.join(out_dir, f"brief-{date_str}.md")
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(brief + "\n")
    except (PermissionError, OSError) as e:
        fallback_dir = os.path.expanduser("~/second-brain/briefs")
        os.makedirs(fallback_dir, exist_ok=True)
        out_path = os.path.join(fallback_dir, f"brief-{date_str}.md")
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(brief + "\n")
        print(f"WARNING: couldn't write into the vault ({e}) — "
              "grant Full Disk Access to python3 in System Settings to fix. Wrote fallback instead.")
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
