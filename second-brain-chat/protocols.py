"""
protocols.py — standing orders: named routines Alex defines once and invokes by name.

WHY THIS EXISTS
---------------
The Iron Man films' most load-bearing assistant feature isn't wit — it's protocols.
"House Party Protocol" is a complex, pre-agreed sequence compressed into two words said
at the right moment. CLARVIS had shortcuts.json (one keyword -> one canned prompt), but
Alex couldn't create a NEW multi-step routine from inside a conversation; that required
editing a JSON file on disk, which is exactly the kind of friction that means it never
happens.

A protocol here is a named, ordered checklist of plain-English steps, defined in chat
("when I say 'game day': check my calendar for the game, clear my afternoon tasks,
draft a focus note") and stored as markdown in the vault where Alex can read and edit
it like any other note.

EXECUTION MODEL — deliberately the simple, honest one
-----------------------------------------------------
Running a protocol does NOT spin up an engine that executes tools directly. run_text()
returns the steps as framed instructions, and the model carries them out in the same
turn using its normal tools. Two properties fall out structurally:

  1. Every existing gate still applies. A protocol step that says "email the coach"
     still ends at a draft (there is no send capability to reach); a step that wants a
     calendar event still lands in the approval queue. Protocols add convenience, never
     authority.
  2. Steps are Alex-authored instructions (from chat or hand-edits in his own vault),
     so they carry exactly the trust of Alex typing them — no more, and no data-boundary
     wrapping needed. What they can DO is bounded by the tool layer, same as live chat.

Nothing is deleted here either: retiring a protocol moves it to Protocols/Archive/.
"""

import os
import re
import shutil
from datetime import datetime, timezone

PROTOCOLS_FOLDER = "Protocols"
ARCHIVE_FOLDER = os.path.join(PROTOCOLS_FOLDER, "Archive")
MAX_STEPS = 20
MAX_STEP_CHARS = 500

_STEP_RE = re.compile(r"^\s*(?:\d+[.)]|[-*•])\s+(?P<text>.+?)\s*$")


def _today() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return s[:60] or "unnamed"


def _dir(vault_path: str) -> str:
    return os.path.join(vault_path, PROTOCOLS_FOLDER)


def _path(vault_path: str, name: str) -> str:
    return os.path.join(_dir(vault_path), f"{_slug(name)}.md")


def _fm_value(body: str, key: str) -> str:
    m = re.search(rf"^{key}:\s*(.+)$", body, re.M)
    return m.group(1).strip().strip('"') if m else ""


# ----------------------------------------------------------------------- write
def save(vault_path: str, name: str, steps: list, title: str = "",
         overwrite: bool = False) -> str:
    """Create (or with overwrite=True, replace) a protocol. Returns a friendly message."""
    slug = _slug(name)
    clean_steps = []
    for s in steps or []:
        s = re.sub(r"\s+", " ", str(s)).strip()
        if s:
            clean_steps.append(s[:MAX_STEP_CHARS])
    if not clean_steps:
        return "A protocol needs at least one step."
    if len(clean_steps) > MAX_STEPS:
        return (f"That's {len(clean_steps)} steps — protocols cap at {MAX_STEPS} so they "
                "stay runnable in one pass. Split it into two protocols.")

    path = _path(vault_path, name)
    exists = os.path.exists(path)
    if exists and not overwrite:
        return (f"A protocol named '{slug}' already exists. Say the word and I'll "
                "overwrite it (or pick a different name).")

    title = (title or name).strip()
    created = _today()
    if exists:  # preserve the original creation date through an overwrite
        try:
            with open(path, "r", encoding="utf-8") as f:
                created = _fm_value(f.read(), "created") or created
        except OSError:
            pass

    os.makedirs(_dir(vault_path), exist_ok=True)
    lines = [
        "---",
        "type: protocol",
        f"name: {slug}",
        f"title: {title}",
        f"created: {created}",
        f"updated: {_today()}",
        "---",
        "",
        f"# {title}",
        "",
        "> A standing order — CLARVIS runs these steps when Alex invokes it by name.",
        "> Edit freely; this file is the single source of truth for the routine.",
        "",
    ]
    lines += [f"{i}. {s}" for i, s in enumerate(clean_steps, 1)]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    verb = "Updated" if exists else "Saved"
    return (f"{verb} protocol '{slug}' ({len(clean_steps)} step"
            f"{'s' if len(clean_steps) != 1 else ''}). Alex can run it any time by name.")


def archive(vault_path: str, name: str) -> str:
    """Retire a protocol: move its file to Protocols/Archive/ (kept, not deleted)."""
    slug = _slug(name)
    path = _path(vault_path, name)
    if not os.path.exists(path):
        return f"No protocol named '{slug}' exists."
    dest_dir = os.path.join(vault_path, ARCHIVE_FOLDER)
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, f"{slug}-{_today()}.md")
    shutil.move(path, dest)
    return f"Archived protocol '{slug}' (kept under {ARCHIVE_FOLDER}/)."


# ------------------------------------------------------------------------ read
def load(vault_path: str, name: str) -> dict | None:
    """{name, title, steps, created, updated} or None. Steps parse from numbered OR
    bulleted lines, so hand-edits in Obsidian keep working."""
    path = _path(vault_path, name)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            body = f.read()
    except OSError:
        return None
    steps = [m.group("text") for m in
             (_STEP_RE.match(ln) for ln in body.splitlines()) if m]
    # Quote-style lines are the description blurb, not steps.
    steps = [s for s in steps if s]
    return {
        "name": _fm_value(body, "name") or _slug(name),
        "title": _fm_value(body, "title") or name,
        "created": _fm_value(body, "created"),
        "updated": _fm_value(body, "updated"),
        "steps": steps,
    }


def list_all(vault_path: str) -> list:
    """All active protocols, alphabetical: [{name, title, steps_count, updated}]."""
    d = _dir(vault_path)
    if not os.path.isdir(d):
        return []
    out = []
    for fn in sorted(os.listdir(d)):
        if not fn.endswith(".md"):
            continue
        p = load(vault_path, fn[:-3])
        if p and p["steps"]:
            out.append({"name": p["name"], "title": p["title"],
                        "steps_count": len(p["steps"]), "updated": p["updated"]})
    return out


# ------------------------------------------------------------------- execution
def run_text(protocol: dict) -> str:
    """The framed instruction block the model executes in-turn. See EXECUTION MODEL
    in the module docstring for why this is text, not an engine."""
    header = (
        f"STANDING ORDER — '{protocol['title']}' (protocol '{protocol['name']}', "
        f"defined {protocol.get('created') or 'previously'} by Alex).\n"
        "Execute these steps NOW, in order, using your normal tools. Every usual gate "
        "still applies: approvals still queue, mail still stops at drafts, nothing "
        "irreversible happens without Alex. If a step can't be done, say so and move to "
        "the next. When finished, give Alex a one-line-per-step report of what happened."
    )
    steps = "\n".join(f"{i}. {s}" for i, s in enumerate(protocol["steps"], 1))
    return f"{header}\n\n{steps}"
