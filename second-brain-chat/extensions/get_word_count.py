"""
EXTENSION TOOL: get_word_count
Counts words (and optionally characters) in text provided in the chat.

Adopted 2026-07-21 from the first self-expansion proposal (proposed_tools/get_word_count.py).
Loaded automatically by app.py's extensions loader: it reads TOOL_SCHEMA, registers the
same-named function, and str()-wraps the return. Runs with the shared {claude, supabase,
VAULT_PATH, os, json} namespace the loader injects (this tool needs none of them).
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
    result = {"word_count": len(words)}

    if include_characters:
        result["character_count_with_spaces"] = len(text)
        result["character_count_without_spaces"] = len(
            text.replace(" ", "").replace("\t", "").replace("\n", "")
        )

    return result
