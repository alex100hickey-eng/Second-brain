"""
vault_index.py — a small, dependency-free indexer + search over an Obsidian vault.

Used by the chat brain's read-only note tools (search_notes / read_note /
list_recent_notes in app.py). It scans a vault directory for Markdown files and
extracts, per note: title, headings, tags (#inline and YAML frontmatter), wiki
links, folder, modified time, and full content. Search is simple keyword-relevance
scoring with snippet extraction — no external services, embeddings, or API keys.

STRICTLY READ-ONLY: nothing in this module ever writes to, moves, or deletes any
file in the vault. It only calls os.walk / open(..., "r").

Standalone use / re-index test:
    python3 vault_index.py "/path/to/vault"
"""

import os
import re
import difflib
from datetime import datetime, timezone

# Folders we never descend into (Obsidian config, VCS, macOS cruft).
SKIP_DIRS = {".obsidian", ".git", ".trash", "__pycache__", ".DS_Store"}

# Very common English words that shouldn't drive relevance on their own.
_STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "of", "to", "in", "on", "for", "with",
    "is", "are", "was", "were", "be", "as", "at", "by", "it", "this", "that",
    "what", "do", "does", "my", "me", "i", "about", "say", "says", "said", "note",
    "notes", "from", "have", "has", "how", "can", "you", "your",
}

_WORD_RE = re.compile(r"[a-z0-9]+")
_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.*)$", re.MULTILINE)
# Obsidian tags must contain at least one non-numeric character, so "#1" / "#2026"
# (e.g. "the #1 lever") are NOT tags — require a letter/underscore somewhere.
_TAG_RE = re.compile(r"(?:^|\s)#([A-Za-z0-9_/-]*[A-Za-z_][A-Za-z0-9_/-]*)")
_WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:[#|][^\]]*)?\]\]")


def _tokenize(text: str) -> list:
    return _WORD_RE.findall(text.lower())


def _parse_frontmatter_tags(body: str) -> list:
    """Pull tags out of a leading YAML frontmatter block, if present.
    Supports both `tags: [a, b]` and a block list of `  - a` lines."""
    if not body.startswith("---"):
        return []
    end = body.find("\n---", 3)
    if end == -1:
        return []
    fm = body[3:end]
    tags = []
    lines = fm.splitlines()
    for i, line in enumerate(lines):
        m = re.match(r"\s*tags\s*:\s*(.*)$", line, re.IGNORECASE)
        if not m:
            continue
        inline = m.group(1).strip()
        if inline and inline not in ("[]", "~"):
            # inline list form: tags: [a, b]  OR  tags: a, b  OR  tags: a
            cleaned = inline.strip("[]")
            tags += [t.strip().strip("'\"#") for t in cleaned.split(",") if t.strip()]
        else:
            # block list form: subsequent `  - tag` lines
            for follow in lines[i + 1:]:
                bm = re.match(r"\s*-\s*(.+?)\s*$", follow)
                if bm:
                    tags.append(bm.group(1).strip().strip("'\"#"))
                elif follow.strip() and not follow.startswith(" "):
                    break
        break
    return [t for t in tags if t]


def _strip_frontmatter(body: str) -> str:
    if body.startswith("---"):
        end = body.find("\n---", 3)
        if end != -1:
            nl = body.find("\n", end + 1)
            return body[nl + 1:] if nl != -1 else ""
    return body


def parse_note(full_path: str, vault_path: str) -> dict:
    """Read and parse a single note into a structured record. Read-only."""
    with open(full_path, "r", encoding="utf-8", errors="replace") as f:
        content = f.read()

    rel_path = os.path.relpath(full_path, vault_path)
    folder = os.path.dirname(rel_path)
    filename = os.path.basename(rel_path)
    stem = filename[:-3] if filename.lower().endswith(".md") else filename

    headings = [h[1].strip() for h in _HEADING_RE.findall(content)]
    # Title: first H1 if present, else the filename stem.
    h1 = next((h[1].strip() for h in _HEADING_RE.findall(content) if len(h[0]) == 1), None)
    title = h1 or stem

    body_no_fm = _strip_frontmatter(content)
    inline_tags = _TAG_RE.findall(body_no_fm)
    fm_tags = _parse_frontmatter_tags(content)
    # De-dup, preserve order, keep original case for display but compare lower.
    seen, tags = set(), []
    for t in fm_tags + inline_tags:
        if t.lower() not in seen:
            seen.add(t.lower())
            tags.append(t)

    links = [l.strip() for l in _WIKILINK_RE.findall(content)]

    try:
        mtime = os.path.getmtime(full_path)
    except OSError:
        mtime = 0.0

    return {
        "path": rel_path,
        "folder": folder or "(root)",
        "filename": filename,
        "stem": stem,
        "title": title,
        "headings": headings,
        "tags": tags,
        "links": links,
        "content": content,
        "body": body_no_fm,
        "mtime": mtime,
        "size": len(content),
    }


class VaultIndex:
    """In-memory index of a vault's Markdown notes. Cheap to rebuild for a personal vault."""

    def __init__(self, vault_path: str):
        self.vault_path = vault_path
        self.notes = []          # list of note dicts
        self.built_at = None     # datetime of last successful build
        self.error = None        # str if the vault path is unusable

    # ---- indexing ------------------------------------------------------

    def build(self) -> "VaultIndex":
        """(Re)scan the vault from scratch. Returns self. Never writes anything."""
        self.notes = []
        self.error = None
        if not os.path.isdir(self.vault_path):
            self.error = f"Vault path not found or not a directory: {self.vault_path}"
            self.built_at = datetime.now(timezone.utc)
            return self

        for root, dirs, files in os.walk(self.vault_path):
            # Prune skip-dirs in place so os.walk doesn't descend into them.
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for fn in files:
                if not fn.lower().endswith(".md"):
                    continue
                full = os.path.join(root, fn)
                try:
                    self.notes.append(parse_note(full, self.vault_path))
                except Exception as e:  # a single bad file never breaks the index
                    self.notes.append({
                        "path": os.path.relpath(full, self.vault_path),
                        "folder": os.path.dirname(os.path.relpath(full, self.vault_path)) or "(root)",
                        "filename": fn, "stem": fn[:-3], "title": fn[:-3],
                        "headings": [], "tags": [], "links": [], "content": "",
                        "body": "", "mtime": 0.0, "size": 0, "_parse_error": str(e),
                    })
        self.built_at = datetime.now(timezone.utc)
        return self

    def ensure_built(self) -> "VaultIndex":
        if self.built_at is None:
            self.build()
        return self

    @property
    def count(self) -> int:
        return len(self.notes)

    # ---- search --------------------------------------------------------

    def _score(self, note: dict, terms: list, phrase: str) -> tuple:
        """Return (score, best_snippet). Higher score = more relevant."""
        title_l = note["title"].lower()
        stem_l = note["stem"].lower()
        path_l = note["path"].lower()
        headings_l = " \n ".join(note["headings"]).lower()
        tags_l = " ".join(note["tags"]).lower()
        body_l = note["body"].lower()

        score = 0.0
        # Whole-phrase hits are strong signals.
        if phrase and phrase in title_l:
            score += 40
        if phrase and phrase in body_l:
            score += 8

        for term in terms:
            if term in title_l or term in stem_l:
                score += 12
            if term in path_l:
                score += 3
            if term in headings_l:
                score += 6
            if term in tags_l:
                score += 8
            # body frequency, with diminishing returns
            freq = body_l.count(term)
            if freq:
                score += min(freq, 5) * 1.5

        snippet = self._snippet(note["body"], terms, phrase)
        # Small recency nudge so ties favor fresher notes (kept tiny).
        return score, snippet

    @staticmethod
    def _snippet(body: str, terms: list, phrase: str, width: int = 160) -> str:
        if not body.strip():
            return ""
        low = body.lower()
        pos = low.find(phrase) if phrase else -1
        if pos == -1:
            for t in terms:
                pos = low.find(t)
                if pos != -1:
                    break
        if pos == -1:
            pos = 0
        start = max(0, pos - width // 3)
        end = min(len(body), pos + width)
        snippet = body[start:end].replace("\n", " ").strip()
        snippet = re.sub(r"\s+", " ", snippet)
        if start > 0:
            snippet = "…" + snippet
        if end < len(body):
            snippet = snippet + "…"
        return snippet

    def search(self, query: str, limit: int = 5) -> list:
        """Return up to `limit` note dicts, each with added `score` and `snippet`,
        ordered most-relevant first."""
        self.ensure_built()
        phrase = query.strip().lower()
        terms = [t for t in _tokenize(query) if t not in _STOPWORDS] or _tokenize(query)
        # allow explicit #tag queries
        tag_query = [q.lstrip("#").lower() for q in query.split() if q.startswith("#")]

        scored = []
        for note in self.notes:
            score, snippet = self._score(note, terms, phrase)
            if tag_query and any(tq in (t.lower() for t in note["tags"]) for tq in tag_query):
                score += 25
            if score > 0:
                scored.append((score, note["mtime"], note, snippet))

        scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
        out = []
        for score, _mtime, note, snippet in scored[:limit]:
            r = dict(note)
            r["score"] = round(score, 1)
            r["snippet"] = snippet
            out.append(r)
        return out

    # ---- direct lookup -------------------------------------------------

    def get_by_fuzzy(self, title_or_path: str):
        """Find a single note by (in priority order) exact rel-path, exact/loose
        title or filename, case-insensitive contains, then difflib close-match.
        Returns a note dict or None."""
        self.ensure_built()
        q = title_or_path.strip()
        ql = q.lower()
        ql_stem = ql[:-3] if ql.endswith(".md") else ql

        # 1. exact relative path (with or without .md)
        for n in self.notes:
            if n["path"].lower() == ql or n["path"].lower() == ql + ".md":
                return n
        # 2. exact title or filename stem
        for n in self.notes:
            if n["title"].lower() == ql_stem or n["stem"].lower() == ql_stem:
                return n
        # 3. path endswith (e.g. "idea.md" or "Money/idea")
        for n in self.notes:
            if n["path"].lower().endswith(ql) or n["path"].lower().endswith(ql + ".md"):
                return n
        # 4. case-insensitive substring on title/stem/path
        subs = [n for n in self.notes
                if ql_stem in n["title"].lower() or ql_stem in n["stem"].lower()
                or ql_stem in n["path"].lower()]
        if len(subs) == 1:
            return subs[0]
        if subs:
            # prefer the closest title match among substring hits
            subs.sort(key=lambda n: difflib.SequenceMatcher(None, ql_stem, n["title"].lower()).ratio(),
                      reverse=True)
            return subs[0]
        # 5. fuzzy close-match on titles / stems
        candidates = {}
        for n in self.notes:
            candidates[n["title"]] = n
            candidates[n["stem"]] = n
        close = difflib.get_close_matches(q, list(candidates.keys()), n=1, cutoff=0.6)
        if close:
            return candidates[close[0]]
        return None

    def recent(self, n: int = 5) -> list:
        """Return the n most-recently-modified notes (dicts), newest first."""
        self.ensure_built()
        return sorted(self.notes, key=lambda x: x["mtime"], reverse=True)[:max(1, n)]


# ---- helpers for turning results into tool-friendly text -------------------

def one_line_preview(note: dict, width: int = 100) -> str:
    """First non-empty, non-heading, non-frontmatter line of a note."""
    for line in note["body"].splitlines():
        s = line.strip()
        if s and not s.startswith("#") and not s.startswith("---"):
            s = re.sub(r"\s+", " ", s)
            return (s[:width] + "…") if len(s) > width else s
    # fall back to title if body is empty
    return note["title"]


def humanize_mtime(mtime: float) -> str:
    if not mtime:
        return "unknown date"
    return datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")


if __name__ == "__main__":
    import sys
    vp = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("OBSIDIAN_VAULT_PATH", "")
    idx = VaultIndex(vp).build()
    print(f"Vault: {vp}")
    if idx.error:
        print("ERROR:", idx.error)
        sys.exit(1)
    print(f"Indexed {idx.count} notes at {idx.built_at.isoformat()}")
    print("\nMost recent:")
    for n in idx.recent(5):
        print(f"  - {n['path']}  [{humanize_mtime(n['mtime'])}]  — {one_line_preview(n)}")
    q = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else "test"
    print(f"\nSearch '{q}':")
    for r in idx.search(q, limit=5):
        print(f"  ({r['score']}) {r['path']} — {r['snippet'][:100]}")
