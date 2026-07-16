"""
Money/Business Agent #1 — "Clips/Curiosity" channel idea generator.

Each run:
1. Picks one of 3 rotating themes
2. Asks Claude to generate a full short-form video concept (topic, hook, script, captions)
3. Writes the result to Supabase for review before you feed it into Viewmax

Run locally first with: python3 money_clips_agent.py
"""

import json
import os
import random
import sys
from datetime import datetime, timezone

from anthropic import Anthropic
from supabase import create_client

# ---- CONFIG — set these as environment variables ----
CLAUDE_API_KEY = os.environ.get("CLAUDE_API_KEY")
SUPABASE_URL = os.environ.get("SUPABASE_URL")       # e.g. https://xxxx.supabase.co
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

for _name, _value in [
    ("CLAUDE_API_KEY", CLAUDE_API_KEY),
    ("SUPABASE_URL", SUPABASE_URL),
    ("SUPABASE_KEY", SUPABASE_KEY),
]:
    if not _value:
        sys.exit(f"Missing required environment variable: {_name}")
# ------------------------------------------------------

THEMES = [
    "oddly satisfying (slime, cutting, mechanisms, patterns)",
    "did-you-know facts (science, history, random trivia)",
    "nature and animal curiosities",
]

AGENT_NAME = "money_clips_agent"


def pick_theme() -> str:
    """Rotate themes based on day of year, so it cycles predictably."""
    day_index = datetime.now(timezone.utc).timetuple().tm_yday
    return THEMES[day_index % len(THEMES)]


def generate_concept(theme: str) -> dict:
    client = Anthropic(api_key=CLAUDE_API_KEY)

    prompt = f"""You're helping generate an original short-form video concept for a YouTube Shorts
channel in the theme: "{theme}".

The content must be 100% original — no copyrighted footage, no reused clips from
existing channels or movies. Assume footage will be created with an AI video tool
(text-to-video) or sourced from properly licensed stock libraries.

Return ONLY valid JSON, no other text, in this exact shape:
{{
  "topic": "short topic title",
  "hook": "the first line/visual that grabs attention in the first 2 seconds",
  "script": "a tight 40-60 second narration script",
  "captions": ["caption idea 1", "caption idea 2", "caption idea 3"]
}}"""

    message = client.messages.create(
        model="claude-sonnet-5",
        max_tokens=1000,
        messages=[{"role": "user", "content": prompt}],
    )

    raw_text = next(block.text for block in message.content if block.type == "text").strip()
    # Strip accidental markdown fences if the model adds them
    raw_text = raw_text.replace("```json", "").replace("```", "").strip()

    return json.loads(raw_text)


def save_to_supabase(theme: str, concept: dict):
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

    output_text = json.dumps(
        {
            "theme": theme,
            "topic": concept["topic"],
            "hook": concept["hook"],
            "script": concept["script"],
            "captions": concept["captions"],
        },
        indent=2,
    )

    result = supabase.table("Agent Outputs").insert(
        {
            "agent_name": AGENT_NAME,
            "output_text": output_text,
        }
    ).execute()

    return result


def main():
    theme = pick_theme()
    print(f"Theme for today: {theme}")

    print("Generating concept with Claude...")
    concept = generate_concept(theme)
    print(json.dumps(concept, indent=2))

    print("Saving to Supabase...")
    save_to_supabase(theme, concept)
    print("Done. Check your Supabase 'Agent Outputs' table for the new row.")


if __name__ == "__main__":
    main()
