#!/usr/bin/env python3
"""Pre-send lint for anything a client will actually read.

CLARVIS drafts, Alex sends — every time, by hand. That gate is the right design,
but it puts the last check on a human reading a document he has already read
three times, which is exactly when a leftover placeholder gets missed.

Two failure modes are worth a machine's attention:

  1. **A half-filled template.** `Templates/proposal-template.md` carries a
     `> TEMPLATE NOTES (delete before sending)` block. Fill the placeholders,
     forget the block, and the prospect receives a document instructing the
     sender to never describe the work as AI-generated. Nothing but memory
     currently prevents that.
  2. **The plan's hard rule**: the word "AI" appears in no client-facing artifact.
     Worth verifying on the actual outgoing text rather than on intent.

Usage:
    python3 scripts/check_client_doc.py path/to/draft.md [more.md ...]

Exit 0 = safe to send. Exit 1 = something would embarrass you. Exit 2 = bad args.
Advisory by design: it reads files and never edits or sends anything.
"""
import os
import re
import sys

BANNED = [
    (re.compile(r"\bAI\b"), "the letters 'AI'"),
    (re.compile(r"artificial intelligence", re.I), "'artificial intelligence'"),
    (re.compile(r"\bLLM\b", re.I), "'LLM'"),
    (re.compile(r"\b(GPT|Claude|OpenAI|Anthropic|ChatGPT)\b", re.I), "a model/vendor name"),
    (re.compile(r"\bmachine learning\b", re.I), "'machine learning'"),
    (re.compile(r"\bCLARVIS\b", re.I), "the internal system name"),
    # Pipeline tells — 2026-08-03 council taste-pass, all seen in a real pack:
    (re.compile(r"\w\(s\)\b"), "format-string pluralization like 'asset(s)'"),
    (re.compile(r"\bthe gate\b", re.I), "internal pipeline jargon 'the gate'"),
    (re.compile(r"\bon-brief\b", re.I), "'on-brief' (there is no brief)"),
    (re.compile(r"\bclaim guardrails\b", re.I), "internal QA jargon"),
    (re.compile(r"was (not |n't )?provided this period", re.I),
     "fabricated-engagement framing"),
    (re.compile(r"identified in the brief", re.I), "fabricated-engagement framing"),
    (re.compile(r"before (distribution|sending to the client)", re.I),
     "template scaffolding instruction"),
    (re.compile(r"\$X\b"), "an unfilled $X figure"),
]

PLACEHOLDER = re.compile(r"\{\{[^}]+\}\}")
# A header with no content before EOF reads as a truncated export — the exact
# tell that ended the 2026-08-03 taste-pass ("## Full batch" over nothing).
EMPTY_TRAILING_HEADER = re.compile(r"^#{1,6}\s+\S.*\n(?:\s*\n)*\Z", re.M)
INTERNAL_MARKER = re.compile(
    r"(TEMPLATE NOTES|delete before sending|INTERNAL ONLY|DO NOT SEND)", re.I)


def lint(path: str) -> list:
    """Every reason not to send this file, as (line, message)."""
    problems = []
    try:
        text = open(path, encoding="utf-8", errors="replace").read()
    except OSError as e:
        return [(0, f"could not read: {e}")]

    lines = text.splitlines()

    for m in PLACEHOLDER.finditer(text):
        line = text[:m.start()].count("\n") + 1
        problems.append((line, f"unfilled placeholder {m.group(0)}"))

    for i, line in enumerate(lines, 1):
        if INTERNAL_MARKER.search(line):
            problems.append((i, f"internal note still present — {line.strip()[:70]}"))

    for pattern, label in BANNED:
        for m in pattern.finditer(text):
            line = text[:m.start()].count("\n") + 1
            problems.append((line, f"contains {label} — '{m.group(0)}'"))

    m = EMPTY_TRAILING_HEADER.search(text)
    if m:
        line = text[:m.start()].count("\n") + 1
        problems.append((line, "document ends on an empty header — reads as a "
                               f"truncated export ({m.group(0).strip()[:40]})"))

    return sorted(set(problems))


def main() -> int:
    paths = [a for a in sys.argv[1:] if not a.startswith("-")]
    if not paths:
        print(__doc__.strip().split("Usage:")[1].strip(), file=sys.stderr)
        return 2

    total = 0
    for path in paths:
        problems = lint(path)
        name = os.path.basename(path)
        if not problems:
            print(f"✓ {name} — safe to send")
            continue
        total += len(problems)
        print(f"✗ {name} — {len(problems)} issue(s):")
        for line, msg in problems:
            print(f"    L{line}: {msg}")

    if total:
        print(f"\n{total} issue(s). Fix before sending — these are the ones a human "
              f"reader misses on the fourth pass.")
        return 1
    print(f"\nAll {len(paths)} document(s) clean.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
