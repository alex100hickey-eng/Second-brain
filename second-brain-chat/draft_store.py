"""
draft_store.py — keep CLARVIS's self-authored drafts alive across redeploys.

The problem: everything Jarvis writes for itself lands on the container's local
filesystem — `agents/` (drafted agent scripts), `proposed_tools/` (tool proposals
from create_new_tool), `vault_inbox/` (captured notes staged for filing). None of
those paths are a volume, so every Coolify deploy silently deletes them. The
self-expansion pipeline's whole point is that Jarvis drafts something and a human
approves it LATER — and "later" is exactly the window a redeploy lands in.

Rather than add a Docker volume (which needs Coolify UI access this side can't
reach), drafts mirror into Supabase — the same "Agent Outputs" table every other
subsystem already piggybacks on. The filesystem stays the source of truth for
reads, so nothing downstream changes: adopt_tool still reads a real file, the
review UI still lists a real directory, git still commits a real path. Supabase
is purely the backup that survives the container.

Lifecycle:
  save(kind, name, code)     — mirror a draft as it is written
  rehydrate(kind, directory) — called at boot: restore anything missing on disk
  forget(kind, name)         — drop the mirror once a draft is adopted or discarded

Rehydrate NEVER overwrites a file that already exists on disk. A file present
locally is newer-or-equal by definition (it either survived, or a human just put
it there), and clobbering it with an older mirrored copy would be the one way
this module could destroy work instead of saving it.
"""

import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo

# One row tag per draft kind. Both are registered in app.INTERNAL_AGENT_NAMES so
# they stay out of every agent-output-facing view.
KIND_AGENT = "jarvis_draft_agent"
KIND_TOOL = "jarvis_draft_tool"
KIND_NOTE = "jarvis_draft_note"

VALID_KINDS = (KIND_AGENT, KIND_TOOL, KIND_NOTE)

# A drafted tool/agent is a small Python file. Anything wildly bigger than that is
# not a draft and has no business in a text column.
MAX_DRAFT_BYTES = 200_000

supabase = None


def init(supabase_client):
    global supabase
    supabase = supabase_client


def _now_iso() -> str:
    return datetime.now(ZoneInfo("America/New_York")).isoformat()


def _rows(kind: str, limit: int = 200) -> list:
    """Newest-first rows for a kind. One row per save; a later save of the same
    name supersedes an earlier one, so callers must take the FIRST match."""
    if supabase is None:
        return []
    try:
        return (supabase.table("Agent Outputs").select("id,output_text")
                .eq("agent_name", kind).order("id", desc=True)
                .limit(limit).execute().data or [])
    except Exception:
        return []


def save(kind: str, name: str, code: str) -> bool:
    """Mirror one draft. Returns True if it was stored. Never raises: a failed
    backup must not break the draft that was just written to disk."""
    if supabase is None or kind not in VALID_KINDS or not name:
        return False
    if code is None or len(code.encode("utf-8", "ignore")) > MAX_DRAFT_BYTES:
        return False
    try:
        supabase.table("Agent Outputs").insert({
            "agent_name": kind,
            "output_text": json.dumps({"name": name, "code": code, "saved_at": _now_iso()}),
        }).execute()
    except Exception:
        return False
    # Supersede older copies of the same draft. Reads already take the newest row,
    # so stale ones were only ever dead weight — but re-saving the same name (every
    # boot, every re-capture) grew the table without bound and pushed real drafts
    # past the read limit. Pruning AFTER the insert means a failure here costs
    # tidiness, never the backup itself.
    try:
        _prune_older(kind, name)
    except Exception:
        pass
    return True


def _prune_older(kind: str, name: str) -> None:
    """Delete every row for `name` except the newest."""
    matching = []
    for row in _rows(kind, limit=500):
        try:
            if json.loads(row["output_text"]).get("name") == name:
                matching.append(row["id"])
        except (json.JSONDecodeError, TypeError):
            continue
    for row_id in sorted(matching, reverse=True)[1:]:      # keep the highest id
        try:
            supabase.table("Agent Outputs").delete().eq("id", row_id).execute()
        except Exception:
            continue


def forget(kind: str, name: str) -> int:
    """Drop every mirrored copy of one draft — call when it's adopted or discarded,
    so a rehydrate can't resurrect something a human deliberately got rid of.
    Returns how many rows were removed."""
    if supabase is None or kind not in VALID_KINDS or not name:
        return 0
    removed = 0
    for row in _rows(kind, limit=500):
        try:
            if json.loads(row["output_text"]).get("name") != name:
                continue
            supabase.table("Agent Outputs").delete().eq("id", row["id"]).execute()
            removed += 1
        except Exception:
            continue
    return removed


def _mirrored_names(kind: str) -> set:
    names = set()
    for row in _rows(kind, limit=500):
        try:
            n = json.loads(row["output_text"]).get("name")
        except (json.JSONDecodeError, TypeError):
            continue
        if n:
            names.add(n)
    return names


def backfill(kind: str, directory: str, suffix: str = ".py") -> list:
    """Mirror drafts that already exist on disk but were never backed up — the ones
    written before this module existed. Without it, the drafts most at risk (the old
    ones nobody has adopted yet) would be the only ones left unprotected. Returns
    the names newly mirrored."""
    if supabase is None or kind not in VALID_KINDS or not os.path.isdir(directory):
        return []
    have = _mirrored_names(kind)
    added = []
    for fn in sorted(os.listdir(directory)):
        if not fn.endswith(suffix) or fn.startswith((".", "_", "README")):
            continue
        name = fn[: -len(suffix)]
        if name in have:
            continue
        try:
            with open(os.path.join(directory, fn), encoding="utf-8") as f:
                code = f.read()
        except OSError:
            continue
        if save(kind, name, code):
            added.append(name)
    return added


def rehydrate(kind: str, directory: str, suffix: str = ".py") -> list:
    """Restore mirrored drafts that are missing from `directory`. Returns the names
    restored. Existing files are left strictly alone — see the module docstring."""
    if supabase is None or kind not in VALID_KINDS:
        return []
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError:
        return []

    restored, seen = [], set()
    for row in _rows(kind):
        try:
            d = json.loads(row["output_text"])
        except (json.JSONDecodeError, TypeError):
            continue
        name, code = d.get("name"), d.get("code")
        if not name or code is None or name in seen:
            continue
        seen.add(name)                      # newest row wins; skip older copies
        # Defend the directory boundary: a name is a bare filename, never a path.
        if os.path.basename(name) != name or name.startswith("."):
            continue
        dest = os.path.join(directory, f"{name}{suffix}")
        if os.path.exists(dest):
            continue                        # on disk already = newer or equal
        try:
            with open(dest, "w", encoding="utf-8") as f:
                f.write(code)
            restored.append(name)
        except OSError:
            continue
    return restored
