"""
school_state.py — the phone's school decisions, carried to the vault.

Two one-tap pages (do_actions: the Sunday pace check-in, and — via the nightly
Canvas status sync — "done" flags) run on the SERVER, because that is the node
that sends nudges. The vault is written only by the Mac. So a decision is first
recorded here as a key-addressed state row (same store as heartbeats and the
nudge ledger), and the Mac applies it to the CSVs on its next 30-minute
canvas_sync tick. Both halves are idempotent: applying twice changes nothing.

State rows (agent intake_state, one updated-in-place row per key):
  school:prepared  {"key": ..., "courses": {"MATH120": "2026-09-09", ...},
                    "updated_at": iso}
  school:done      {"key": ..., "items": {"canvas:event-assignment-753485":
                    {"status": "submitted", "date": "2026-09-02"}, ...}}

prepared_through only ever moves FORWARD from here (Alex can still hand-edit the
column backwards in the vault). A done flag only ever CLOSES a row.
"""

import csv
import os
from datetime import datetime, timezone

PREPARED_KEY = "school:prepared"
DONE_KEY = "school:done"
DEFAULT_VAULT = os.path.expanduser(
    "~/Library/Mobile Documents/com~apple~CloudDocs/Obsidian/Second brain")
_DONE = {"submitted", "graded", "done", "complete", "completed"}


def _intake():
    """intake with a live Supabase client. Inside the app it is already wired;
    from the standalone canvas_sync tick we build a client from the env."""
    import intake
    if getattr(intake, "supabase", None) is None:
        url, key = os.environ.get("SUPABASE_URL", ""), os.environ.get("SUPABASE_KEY", "")
        if not url or not key:
            raise RuntimeError("no Supabase client and no SUPABASE_URL/KEY in the environment")
        from supabase import create_client
        intake.supabase = create_client(url, key)
    return intake


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --- record (server or Mac) -----------------------------------------------------

def record_prepared(courses: dict) -> dict:
    """courses = {code: 'YYYY-MM-DD'}. Later dates win; earlier ones are kept."""
    it = _intake()
    st = it._load_state(PREPARED_KEY)
    cur = st.get("courses") if isinstance(st.get("courses"), dict) else {}
    for code, d in (courses or {}).items():
        code = str(code).strip().upper()
        d = str(d).strip()[:10]
        if len(d) != 10:
            continue
        if d >= (cur.get(code) or ""):
            cur[code] = d
    st["courses"] = cur
    st["updated_at"] = _now_iso()
    it._save_state(st)
    return cur


def record_done(source: str, status: str = "submitted", date: str = "") -> dict:
    it = _intake()
    st = it._load_state(DONE_KEY)
    items = st.get("items") if isinstance(st.get("items"), dict) else {}
    items[str(source)] = {"status": status if status in _DONE else "submitted",
                          "date": (date or _now_iso())[:10]}
    st["items"] = items
    st["updated_at"] = _now_iso()
    it._save_state(st)
    return items


# --- apply (Mac only) -------------------------------------------------------------

def _read_csv(path):
    with open(path, newline="", encoding="utf-8") as f:
        r = csv.DictReader(f)
        return (r.fieldnames or []), list(r)


def _write_csv(path, cols, rows):
    tmp = path + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({c: (r.get(c) or "") for c in cols})
    os.replace(tmp, path)


def apply_to_vault(vault_dir: str = None) -> str:
    """Bring courses.csv / assignments.csv up to what the phone decided. Returns
    a one-line summary. Only meaningful on the node that owns the vault."""
    vault_dir = vault_dir or os.environ.get("OBSIDIAN_VAULT_PATH") or DEFAULT_VAULT
    school = os.path.join(vault_dir, "School")
    it = _intake()
    changed = []

    prepared = (it._load_state(PREPARED_KEY).get("courses") or {})
    if isinstance(prepared, dict) and prepared:
        path = os.path.join(school, "courses.csv")
        try:
            cols, rows = _read_csv(path)
        except OSError:
            cols, rows = [], []
        if "prepared_through" in cols:
            dirty = False
            for r in rows:
                code = (r.get("course") or "").strip().upper()
                want = prepared.get(code)
                if want and want > (r.get("prepared_through") or "").strip():
                    r["prepared_through"] = want
                    dirty = True
                    changed.append(f"{code} prepared→{want}")
            if dirty:
                _write_csv(path, cols, rows)

    done = (it._load_state(DONE_KEY).get("items") or {})
    if isinstance(done, dict) and done:
        path = os.path.join(school, "assignments.csv")
        try:
            cols, rows = _read_csv(path)
        except OSError:
            cols, rows = [], []
        if "status" in cols:
            dirty = False
            for r in rows:
                flag = done.get((r.get("source") or "").strip())
                if not flag or (r.get("status") or "").strip().lower() in _DONE:
                    continue
                r["status"] = flag.get("status") or "submitted"
                if "submitted_date" in cols and not (r.get("submitted_date") or "").strip():
                    r["submitted_date"] = flag.get("date") or ""
                dirty = True
                changed.append(f"{(r.get('course') or '').strip()}:{(r.get('title') or '')[:30]}→{r['status']}")
            if dirty:
                _write_csv(path, cols, rows)
    return ("applied " + "; ".join(changed)) if changed else "nothing to apply"
