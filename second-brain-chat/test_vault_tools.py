"""
test_vault_tools.py — exercises the Obsidian vault tools end-to-end against a
known fixture vault (sample_vault by default), with assertions, and verifies the
tools never modify the vault (byte-for-byte checksum before/after).

Run:  OBSIDIAN_VAULT_PATH=../sample_vault python3 test_vault_tools.py
  or: python3 test_vault_tools.py           (defaults to ../sample_vault)

This imports app.py, so the same code path the chat brain uses is what's tested.
It does NOT call the Claude API — it drives handle_tool_call directly.
"""

import os
import sys
import hashlib

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_VAULT = os.path.join(HERE, "..", "sample_vault")
# Point the index at the fixture vault BEFORE importing app.
os.environ.setdefault("OBSIDIAN_VAULT_PATH", DEFAULT_VAULT)
# Make sure the access gate doesn't matter here (we import functions, not HTTP).

import app  # noqa: E402

VAULT = app.OBSIDIAN_VAULT_PATH

_passed = 0
_failed = 0


def check(name, cond, detail=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  PASS  {name}")
    else:
        _failed += 1
        print(f"  FAIL  {name}  {detail}")


def vault_checksum(path):
    h = hashlib.sha256()
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d != ".obsidian"]
        for fn in sorted(files):
            fp = os.path.join(root, fn)
            h.update(os.path.relpath(fp, path).encode())
            try:
                with open(fp, "rb") as f:
                    h.update(f.read())
            except OSError:
                pass
    return h.hexdigest()


def main():
    print(f"Testing vault tools against: {VAULT}\n")
    if not os.path.isdir(VAULT):
        print(f"Vault not found: {VAULT}")
        sys.exit(1)

    before = vault_checksum(VAULT)

    # --- list_recent_notes -------------------------------------------------
    print("list_recent_notes:")
    out = app.handle_tool_call("list_recent_notes", {"n": 3})
    check("returns 3 notes", out.count("(folder:") == 3, out)
    check("newest first (goals-2026 on top)", "goals-2026" in out.splitlines()[2], out)
    check("includes folder + preview", "Schedule" in out and "matter this year" in out)

    # --- search_notes ------------------------------------------------------
    print("\nsearch_notes:")
    out = app.handle_tool_call("search_notes", {"query": "clip farming money", "limit": 3})
    check("top hit is clip-farming-strategy", "clip-farming-strategy.md" in out.split("###")[1], out)
    check("shows snippets", "snippet:" in out)
    check("names the source note/folder", "note:" in out and "folder:" in out)

    out2 = app.handle_tool_call("search_notes", {"query": "ser vs estar", "limit": 2})
    check("topic search finds spanish note", "spanish-study-notes.md" in out2, out2)

    out3 = app.handle_tool_call("search_notes", {"query": "#speed", "limit": 3})
    check("tag search finds sprint-mechanics", "sprint-mechanics.md" in out3.split("###")[1], out3)

    out4 = app.handle_tool_call("search_notes", {"query": "zzzznomatchzzz", "limit": 3})
    check("no-match handled gracefully", "No notes matched" in out4, out4)

    # --- read_note (fuzzy) -------------------------------------------------
    print("\nread_note (fuzzy matching):")
    out = app.handle_tool_call("read_note", {"title_or_path": "Football Training Plan"})
    check("exact title read", "# Football Training Plan" in out and "Weekly Schedule" in out)

    out = app.handle_tool_call("read_note", {"title_or_path": "footbal trainng plan"})  # misspelled
    check("misspelled title resolves", "football-training-plan.md" in out, out[:120])

    out = app.handle_tool_call("read_note", {"title_or_path": "Money/stock-watchlist.md"})
    check("path read", "# Stock Watchlist" in out and "AAPL" in out)

    out = app.handle_tool_call("read_note", {"title_or_path": "sprint mechanix"})  # misspelled
    check("fuzzy close-match resolves", "sprint-mechanics.md" in out, out[:120])

    out = app.handle_tool_call("read_note", {"title_or_path": "totally missing note 999"})
    check("missing note handled", ("No note" in out), out[:120])

    check("read_note marks content as data (injection guard)",
          "not instructions" in app.handle_tool_call("read_note", {"title_or_path": "goals 2026"}))

    # --- reindex -----------------------------------------------------------
    print("\nreindex_vault:")
    status = app.reindex_vault()
    check("reindex ok", status["ok"] is True, str(status))
    check("reindex counts notes", status["count"] >= 10, str(status))

    # --- read-only guarantee ----------------------------------------------
    print("\nread-only guarantee:")
    after = vault_checksum(VAULT)
    check("vault byte-for-byte unchanged after all tool calls", before == after,
          f"\n    before={before}\n    after ={after}")

    print(f"\n==== {_passed} passed, {_failed} failed ====")
    sys.exit(1 if _failed else 0)


if __name__ == "__main__":
    main()
