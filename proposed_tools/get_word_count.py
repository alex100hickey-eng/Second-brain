"""
PROPOSED TOOL: get_word_count
Drafted by Jarvis on request — purpose: Counts the number of words (and optionally characters) in a piece of text Alex provides directly in the chat.

This is a PROPOSAL ONLY. Nothing here is wired into app.py or the live TOOLS list.
To adopt it, a human (or a future Claude Code session) must manually:
  1. Copy TOOL_SCHEMA below into the TOOLS list in app.py
  2. Copy the get_word_count() function below into app.py
  3. Add the routing line shown at the bottom into handle_tool_call
"""

TOOL_SCHEMA = {
    "name": "get_word_count",
    "description": "Counts the number of words in a piece of text Alex provides directly in the chat. Optionally also returns the character count (with and without spaces). Use this when Alex asks how long a piece of text is, or wants word/character statistics for something he's typed or pasted.",
    "input_schema": {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "The text to analyze. Provide the full text directly."
            },
            "include_characters": {
                "type": "boolean",
                "description": "If true, also include character counts (with and without spaces) in the result. Defaults to false.",
                "default": False
            }
        },
        "required": ["text"]
    }
}


def get_word_count(text, include_characters=False):
    if text is None or not isinstance(text, str):
        return {"error": "text must be a non-empty string"}

    if not text.strip():
        return {
            "word_count": 0,
            "message": "The provided text is empty or contains only whitespace."
        }

    words = text.split()
    result = {
        "word_count": len(words)
    }

    if include_characters:
        result["character_count_with_spaces"] = len(text)
        result["character_count_without_spaces"] = len(text.replace(" ", "").replace("\t", "").replace("\n", ""))

    return result


# if tool_name == "get_word_count":
#     return get_word_count(**tool_input)
