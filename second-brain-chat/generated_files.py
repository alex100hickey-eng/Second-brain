"""
generated_files.py — let CLARVIS remove the files it generated itself, safely.

CLARVIS could write into `synthesized/` (research reports) and `vault_inbox/`
(captured notes staged for filing) but had no way to take anything back out, so
"delete those two reports, I want to start fresh" was a wall. This module is the
narrow, one-file-at-a-time way back out.

SCOPE — this module may touch exactly two directories, both of which contain only
files CLARVIS wrote itself: `synthesized/` and `vault_inbox/`. It cannot reach
Alex's Obsidian vault (still strictly read-only, see vault_index.py), the repo's
source, or anything else on disk. Everything else is out of bounds by
construction, not by convention: the folder is chosen from a two-key dict, the
filename must be a bare basename with an allowed extension, and the resolved
realpath must sit directly inside the chosen folder or the call is refused.

NOTHING IS EVER HARD-DELETED. A removal moves the file into `.generated_trash/`
with a timestamp, the same trash-not-delete guarantee propose_file_cleanup gives
Alex's Downloads. restore_generated_file puts it back. The trash is never emptied
automatically — on the server it is wiped by the next redeploy anyway (it isn't a
volume), and on the Mac these are a few KB of Markdown.

NO BULK ANYTHING. One exact filename per call: no globs, no wildcards, no
prefixes, no "delete everything older than". If CLARVIS wants two files gone it
makes two calls, each naming a file that a listing showed exists. A model acting
on untrusted email/web content should never be one fuzzy match away from
emptying a folder.

Two things outlive the file itself, so removal handles them honestly rather than
claiming a completeness it doesn't have:
  * vault_inbox notes are mirrored into Supabase for redeploy survival
    (draft_store.py). Deleting only the file would let the next boot's rehydrate
    resurrect it. The `on_remove` hook — wired by app.py to draft_store.forget —
    drops that mirror. Symmetrically `on_restore` re-mirrors on a restore.
  * synthesized reports are also logged as an "Agent Outputs" row, and some are
    committed to the repo. Neither is touched here (an audit log shouldn't be
    rewritten by the thing being audited, and rewriting git from a chat tool is
    not a thing this app should do) — instead the return text says so plainly,
    including a specific warning when the file is git-tracked and will therefore
    come back on the next deploy.

Public API:
    list_generated_files(folder="all") -> str
    remove_generated_file(folder, filename) -> str
    restore_generated_file(folder, filename) -> str
"""

import os
import re
import shutil
import subprocess
from datetime import datetime
from zoneinfo import ZoneInfo

_TZ = ZoneInfo("America/New_York")
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The ONLY directories this module may ever touch.
FOLDERS = {
    "synthesized": os.path.join(_PROJECT_ROOT, "synthesized"),
    "vault_inbox": os.path.join(_PROJECT_ROOT, "vault_inbox"),
}

TRASH_DIR = os.path.join(_PROJECT_ROOT, ".generated_trash")

# Generated content is Markdown; .txt is allowed for the odd plain-text drop.
ALLOWED_EXTS = {".md", ".txt"}

# Files that explain the folder itself — never removable.
PROTECTED = {"readme.md"}

# Trash entries are "<stamp>__<original name>"; the separator can't appear in a
# timestamp, so splitting on the first one always recovers the original name.
_STAMP_SEP = "__"

# Optional hooks, set by app.py. Kept as hooks so this module stays stdlib-only
# and Supabase-unaware, the same way note_capture.on_capture does it.
on_remove = None     # fn(folder, filename) -> str   (extra note for the reply)
on_restore = None    # fn(folder, filename, content) -> str


def _folder_error(folder: str) -> str:
    return (f"I can only touch files in {' or '.join(sorted(FOLDERS))} — "
            f"{folder!r} isn't one of them.")


def _resolve(folder: str, filename: str):
    """Validate a (folder, filename) pair. Returns (full_path, None) or (None, error).

    Every rejection here is deliberate: a name that isn't a bare basename, hides a
    glob, points outside the folder, or resolves through a symlink is refused
    rather than sanitized, so nothing surprising can survive a rewrite."""
    if folder not in FOLDERS:
        return None, _folder_error(folder)
    directory = FOLDERS[folder]
    name = (filename or "").strip()
    if not name:
        return None, "Give me the exact filename to remove (list them first if you're unsure)."
    if name != os.path.basename(name) or os.path.isabs(name) or ".." in name:
        return None, f"{name!r} isn't a plain filename — pass just the name, no paths."
    if name.startswith("."):
        return None, f"Refusing to touch the hidden file {name!r}."
    if re.search(r"[*?\[\]]", name):
        return None, ("No wildcards — name one exact file per call. "
                      "Two files to remove means two calls.")
    if os.path.splitext(name)[1].lower() not in ALLOWED_EXTS:
        return None, (f"I only remove {'/'.join(sorted(ALLOWED_EXTS))} files that CLARVIS "
                      f"generated; {name!r} isn't one.")
    if name.lower() in PROTECTED:
        return None, f"{name} explains the folder — that one stays."

    raw = os.path.join(directory, name)
    if os.path.islink(raw):
        return None, f"{name!r} is a symlink; not following it."
    full = os.path.realpath(raw)
    if os.path.dirname(full) != os.path.realpath(directory):
        return None, f"{name!r} doesn't resolve to a file directly inside {folder}/."
    return full, None


def _title_of(path: str, fallback: str) -> str:
    """First Markdown H1, or the frontmatter title, else the filename."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            head = f.read(4000)
    except OSError:
        return fallback
    m = re.search(r"^#\s+(.+)$", head, re.MULTILINE) or re.search(r"^title:\s*(.+)$", head, re.MULTILINE)
    return m.group(1).strip() if m else fallback


def _listing(folder: str) -> list:
    """(filename, title, size_kb, mtime) for one folder, newest first."""
    directory = FOLDERS[folder]
    if not os.path.isdir(directory):
        return []
    out = []
    for fn in os.listdir(directory):
        if fn.startswith(".") or fn.lower() in PROTECTED:
            continue
        if os.path.splitext(fn)[1].lower() not in ALLOWED_EXTS:
            continue
        fp = os.path.join(directory, fn)
        if not os.path.isfile(fp) or os.path.islink(fp):
            continue
        try:
            st = os.stat(fp)
        except OSError:
            continue
        out.append((fn, _title_of(fp, fn), st.st_size / 1024, st.st_mtime))
    out.sort(key=lambda r: r[3], reverse=True)
    return out


def _trashed(folder: str) -> list:
    """(trash_filename, original_filename, mtime) for one folder's trash, newest first."""
    directory = os.path.join(TRASH_DIR, folder)
    if not os.path.isdir(directory):
        return []
    out = []
    for fn in os.listdir(directory):
        if _STAMP_SEP not in fn:
            continue
        fp = os.path.join(directory, fn)
        if not os.path.isfile(fp):
            continue
        try:
            mtime = os.path.getmtime(fp)
        except OSError:
            continue
        out.append((fn, fn.split(_STAMP_SEP, 1)[1], mtime))
    out.sort(key=lambda r: r[2], reverse=True)
    return out


def _is_git_tracked(path: str) -> bool:
    """True if `path` is committed to the repo — meaning removing the file here is
    undone by the next deploy. Best-effort: any git trouble answers False, since a
    false warning is worse than none."""
    try:
        # `path` is already a realpath; the root must be too, or a symlinked prefix
        # (/var -> /private/var on macOS) turns the relative path into an escape.
        root = os.path.realpath(_PROJECT_ROOT)
        rel = os.path.relpath(path, root)
        r = subprocess.run(
            ["git", "ls-files", "--error-unmatch", "--", rel],
            cwd=root, capture_output=True, timeout=10)
        return r.returncode == 0
    except Exception:
        return False


def _when(mtime: float) -> str:
    return datetime.fromtimestamp(mtime, _TZ).strftime("%b %-d, %-I:%M %p")


def list_generated_files(folder: str = "all") -> str:
    """What's currently in synthesized/ and vault_inbox/, plus what's restorable."""
    if folder in ("", "all", None):
        folders = sorted(FOLDERS)
    elif folder in FOLDERS:
        folders = [folder]
    else:
        return _folder_error(folder)

    lines = []
    for f in folders:
        rows = _listing(f)
        lines.append(f"{f}/ — {len(rows)} file(s)" + (":" if rows else " (empty)"))
        for fn, title, kb, mtime in rows[:40]:
            lines.append(f"  - {fn}  ({title}, {kb:.0f} KB, {_when(mtime)})")
        if len(rows) > 40:
            lines.append(f"  …and {len(rows) - 40} more")
        trash = _trashed(f)
        if trash:
            names = ", ".join(orig for _t, orig, _m in trash[:5])
            lines.append(f"  recently removed (restorable): {names}"
                         + (f" …and {len(trash) - 5} more" if len(trash) > 5 else ""))
    return "\n".join(lines)


def remove_generated_file(folder: str, filename: str) -> str:
    """Move ONE generated file to the project trash. Never a hard delete."""
    full, err = _resolve(folder, filename)
    if err:
        return err
    name = os.path.basename(full)
    if not os.path.isfile(full):
        rows = _listing(folder)
        have = "\n".join(f"  - {fn}" for fn, _t, _k, _m in rows[:20]) or "  (folder is empty)"
        return f"There's no {name!r} in {folder}/. What's actually there:\n{have}"

    tracked = _is_git_tracked(full)
    dest_dir = os.path.join(TRASH_DIR, folder)
    try:
        os.makedirs(dest_dir, exist_ok=True)
        stamp = datetime.now(_TZ).strftime("%Y%m%d-%H%M%S")
        dest = os.path.join(dest_dir, f"{stamp}{_STAMP_SEP}{name}")
        n = 1
        while os.path.exists(dest):
            dest = os.path.join(dest_dir, f"{stamp}-{n}{_STAMP_SEP}{name}")
            n += 1
        shutil.move(full, dest)
    except OSError as e:
        return f"Couldn't remove {name}: {e}"

    parts = [f"Removed `{folder}/{name}` — moved to the project trash, not deleted. "
             f"Say the word and I'll restore it."]
    if on_remove:
        try:
            extra = on_remove(folder, name)
            if extra:
                parts.append(extra)
        except Exception as e:
            parts.append(f"(couldn't clear its backup copy: {str(e)[:120]})")
    if folder == "synthesized":
        parts.append("Its original Agent Outputs log entry stays — that's the audit trail, "
                     "not the report.")
        if tracked:
            parts.append("Heads up: this report is committed to the repo, so a redeploy will "
                         "bring the file back. Tell Alex it needs removing from git to be gone "
                         "for good.")
    return " ".join(parts)


def restore_generated_file(folder: str, filename: str) -> str:
    """Put the most recently trashed copy of `filename` back."""
    if folder not in FOLDERS:
        return _folder_error(folder)
    _probe, err = _resolve(folder, filename)
    if err:
        return err
    name = os.path.basename((filename or "").strip())

    match = next((t for t, orig, _m in _trashed(folder) if orig == name), None)
    if not match:
        trash = _trashed(folder)
        have = ", ".join(orig for _t, orig, _m in trash[:10]) or "(nothing)"
        return f"Nothing named {name!r} in {folder}/'s trash. Restorable: {have}"

    dest = os.path.join(FOLDERS[folder], name)
    if os.path.exists(dest):
        return f"{folder}/{name} already exists — not overwriting it."
    try:
        os.makedirs(FOLDERS[folder], exist_ok=True)
        shutil.move(os.path.join(TRASH_DIR, folder, match), dest)
    except OSError as e:
        return f"Couldn't restore {name}: {e}"

    parts = [f"Restored `{folder}/{name}`."]
    if on_restore:
        try:
            with open(dest, encoding="utf-8", errors="replace") as f:
                content = f.read()
            extra = on_restore(folder, name, content)
            if extra:
                parts.append(extra)
        except Exception as e:
            parts.append(f"(couldn't re-create its backup copy: {str(e)[:120]})")
    return " ".join(parts)


TOOL_SCHEMAS = [
    {"name": "list_generated_files",
     "description": "List the files CLARVIS generated: research reports in synthesized/ and "
                    "captured notes staged in vault_inbox/, with their exact filenames. Use "
                    "this before remove_generated_file so you name a file that really exists. "
                    "Also shows what was recently removed and can still be restored.",
     "input_schema": {"type": "object", "properties": {
         "folder": {"type": "string", "enum": ["all", "synthesized", "vault_inbox"],
                    "description": "Which folder to list (default all)."}}}},
    {"name": "remove_generated_file",
     "description": "Remove ONE file that CLARVIS generated — a report in synthesized/ or a "
                    "staged note in vault_inbox/ — when Alex asks to delete/clear/start fresh. "
                    "The file moves to a project trash folder and can be restored; nothing is "
                    "permanently deleted. Requires the exact filename (get it from "
                    "list_generated_files); no wildcards, one file per call. It cannot touch "
                    "Alex's Obsidian vault or anything outside those two folders.",
     "input_schema": {"type": "object", "required": ["folder", "filename"], "properties": {
         "folder": {"type": "string", "enum": ["synthesized", "vault_inbox"]},
         "filename": {"type": "string",
                      "description": "Exact filename inside that folder, e.g. "
                                     "20260801-seed-oils.md — no path, no wildcards."}}}},
    {"name": "restore_generated_file",
     "description": "Undo a remove_generated_file — puts the most recently removed copy of that "
                    "filename back into synthesized/ or vault_inbox/.",
     "input_schema": {"type": "object", "required": ["folder", "filename"], "properties": {
         "folder": {"type": "string", "enum": ["synthesized", "vault_inbox"]},
         "filename": {"type": "string", "description": "The original filename that was removed."}}}},
]

TOOL_STATUS_LABELS = {
    "list_generated_files": "Looking at what I've generated…",
    "remove_generated_file": "Moving that file to the trash…",
    "restore_generated_file": "Restoring that file…",
}


def handle_tool_call(name: str, tool_input: dict) -> str:
    if name == "list_generated_files":
        return list_generated_files(tool_input.get("folder", "all"))
    if name == "remove_generated_file":
        return remove_generated_file(tool_input.get("folder", ""), tool_input.get("filename", ""))
    if name == "restore_generated_file":
        return restore_generated_file(tool_input.get("folder", ""), tool_input.get("filename", ""))
    return "Unknown generated_files tool."
