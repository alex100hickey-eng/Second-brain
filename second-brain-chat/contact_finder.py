"""
contact_finder.py — the field the outreach pipeline never had: who to send it to.

Found 2026-08-24, mid-Wave-1: the pipeline sources prospects, qualifies them,
reads their ad accounts, and writes finished first-touch drafts — and then stops
one field short of sendable. `prospect-tracker.csv` had 97 rows and **no email
column at all**. Every draft was a letter with no address on the envelope, which
is why Wave 1 sat written-but-unsent.

THE RULE THIS MODULE EXISTS TO ENFORCE: never guess an address.

That is not fastidiousness, it's the whole asset. splitframestudio.com took ten
days of warmup to reach a 10/10 mail-tester score, and hard bounces are one of
the strongest negative signals a young sending domain can emit. Guessing
`firstname@brand.com` across a three-email first wave risks trading the entire
warmup for three sends. So:

  * addresses come from a lookup provider (Hunter), never from a pattern;
  * every address is VERIFIED before it is written as sendable;
  * `risky` / catch-all results are stored but NOT marked sendable — they are
    Alex's judgment call, made deliberately, not a default the machine took;
  * generic inboxes (info@, help@, support@) are recorded and ranked last: a
    first-touch about ad creative dies in a support queue, and a burnt prospect
    can't be re-approached.

QUOTA: the free tier is 25 domain searches a month, and a wave is 3 brands — the
budget is fine as long as nothing re-searches a domain it already knows. Rows
that already carry an email are skipped, and domains that came back empty are
remembered in the CSV too (`email_status=none-found`) so a fruitless search is
paid for exactly once.

Vault writes are LOCAL-NODE-ONLY, like august_tracker.reconcile_vault and
ad_creative_pipeline.reconcile_sends — the server's vault is a pull-only mirror,
so a write there is silently reverted by the next sync.
"""

import csv
import json
import os
import ssl
import urllib.parse
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

try:
    import certifi
    _SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CONTEXT = None

_TZ = ZoneInfo("America/New_York")
API_ROOT = "https://api.hunter.io/v2"

# Columns this module owns. Appended to the tracker if absent, never reordered.
COLUMNS = ["email", "email_status", "contact_name", "contact_title", "email_checked"]

# Verification verdicts, in descending order of "safe to send to".
SENDABLE = "deliverable"
RISKY = "risky"            # catch-all or low confidence — stored, never auto-sendable
UNDELIVERABLE = "undeliverable"
NONE_FOUND = "none-found"

# Titles that mean "this person can say yes to ad creative". Ranked: a founder at
# a 9-ad brand reads their own mail; at a 39-ad brand the growth lead is the buyer.
DECISION_TITLES = (
    "founder", "co-founder", "cofounder", "ceo", "owner",
    "cmo", "chief marketing", "vp marketing", "head of marketing",
    "marketing director", "director of marketing", "brand director",
    "growth", "performance marketing", "head of ecommerce", "ecommerce director",
    "creative director", "head of brand", "marketing manager",
)

# Mailboxes nobody makes buying decisions in.
GENERIC_LOCALPARTS = ("info", "help", "support", "hello", "contact", "sales",
                      "orders", "press", "team", "admin", "care", "service")

vault_path = None
_runtime_fn = None


def init(vault_path_value: str = "", runtime_fn=None):
    global vault_path, _runtime_fn
    vault_path = vault_path_value
    _runtime_fn = runtime_fn


def _runtime() -> str:
    try:
        return _runtime_fn() if _runtime_fn else "local"
    except Exception:
        return "unknown"


def _today() -> str:
    return datetime.now(_TZ).date().isoformat()


def api_key() -> str:
    return (os.environ.get("HUNTER_API_KEY") or "").strip()


def _tracker_path():
    if not vault_path:
        return None
    p = os.path.join(vault_path, "Money", "prospect-tracker.csv")
    return p if os.path.exists(p) else None


# ============================================================
# HTTP (one seam, so tests never touch the network or the quota)
# ============================================================

def _http_get(url: str) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20, context=_SSL_CONTEXT) as r:
        return json.loads(r.read().decode())


_fetch = _http_get   # test seam


def _call(path: str, **params) -> dict:
    """One API call. Returns {} on any failure — a lookup that fails must leave the
    row untouched rather than write a wrong address into an outreach pipeline."""
    params["api_key"] = api_key()
    url = f"{API_ROOT}/{path}?" + urllib.parse.urlencode(params)
    try:
        return _fetch(url) or {}
    except Exception as e:
        print(f"contact_finder: {path} failed ({str(e)[:120]})")
        return {}


# ============================================================
# Ranking
# ============================================================

def _is_generic(email: str) -> bool:
    return (email or "").split("@", 1)[0].lower() in GENERIC_LOCALPARTS


def _title_rank(position: str) -> int:
    """Lower is better. Unknown titles sort after known decision-makers but ahead
    of generic inboxes — an unnamed personal address still reaches a human."""
    pos = (position or "").lower()
    for i, t in enumerate(DECISION_TITLES):
        if t in pos:
            return i
    return len(DECISION_TITLES)


def rank_candidates(emails: list) -> list:
    """Hunter's domain-search payload -> best-first candidate list."""
    out = []
    for e in emails or []:
        if not isinstance(e, dict):
            continue
        addr = (e.get("value") or "").strip().lower()
        if "@" not in addr:
            continue
        name = " ".join(x for x in ((e.get("first_name") or ""),
                                    (e.get("last_name") or "")) if x).strip()
        out.append({
            "email": addr,
            "name": name,
            "title": (e.get("position") or "").strip(),
            "generic": _is_generic(addr) or (e.get("type") == "generic"),
            "confidence": int(e.get("confidence") or 0),
        })
    out.sort(key=lambda c: (c["generic"],                 # people before inboxes
                            _title_rank(c["title"]),      # buyers before everyone
                            -c["confidence"]))
    return out


# ============================================================
# Lookup
# ============================================================

def verify(email: str) -> tuple:
    """(status, score). Anything we can't positively confirm is RISKY, never
    SENDABLE — the failure mode we're insuring against is a bounce."""
    data = (_call("email-verifier", email=email) or {}).get("data") or {}
    result = (data.get("result") or "").lower()
    score = int(data.get("score") or 0)
    if result == "deliverable":
        return SENDABLE, score
    if result == "undeliverable":
        return UNDELIVERABLE, score
    return RISKY, score


def find_for_domain(domain: str) -> dict:
    """Best verified contact for one domain.

    Returns {email, name, title, status, score} — status NONE_FOUND when the
    domain yields nothing, so the caller can record the miss and never pay for
    that search again."""
    miss = {"email": "", "name": "", "title": "", "status": NONE_FOUND, "score": 0}
    if not domain:
        return miss
    data = (_call("domain-search", domain=domain, limit=10) or {}).get("data") or {}
    candidates = rank_candidates(data.get("emails") or [])
    if not candidates:
        return miss
    first_risky = None
    for c in candidates:
        status, score = verify(c["email"])
        if status == UNDELIVERABLE:
            continue                      # never store an address we know bounces
        record = {"email": c["email"], "name": c["name"], "title": c["title"],
                  "status": status, "score": score}
        if status == SENDABLE:
            return record
        first_risky = first_risky or record
    # Nothing confirmed. Hand back the best unconfirmed one, clearly labelled, so
    # Alex can decide — but it is not sendable until he says it is.
    return first_risky or miss


# ============================================================
# The tracker
# ============================================================

def _read_tracker():
    path = _tracker_path()
    if not path:
        return None, [], []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return path, list(reader.fieldnames or []), list(reader)


def _write_tracker(path: str, cols: list, rows: list) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({c: (r.get(c) or "") for c in cols})
    os.replace(tmp, path)     # atomic — a crash never truncates the tracker


def _targets(rows: list, wave: str = "", brands=None) -> list:
    wanted = {b.strip().lower() for b in (brands or []) if b.strip()}
    out = []
    for r in rows:
        brand = (r.get("brand") or "").strip()
        if wanted:
            if brand.lower() not in wanted:
                continue
        elif wave and (r.get("wave") or "").strip() != str(wave):
            continue
        out.append(r)
    return out


def fill_contacts(wave: str = "", brands=None, limit: int = 6) -> str:
    """Look up and verify contacts for a wave (or named brands) and write them in.

    Skips any row that already has an email OR was already searched and came back
    empty — the free tier is 25 searches a month and a re-search buys nothing."""
    if _runtime() != "local":
        return "skipped: the tracker lives in the vault, and only the local node writes it."
    if not api_key():
        return ("No HUNTER_API_KEY set, so I can't look anyone up. Sign up for the "
                "free tier at hunter.io (25 searches/mo — a wave is 3), put the key "
                "in second-brain-chat/.env as HUNTER_API_KEY, and restart. Don't "
                "paste the key into chat.")
    path, cols, rows = _read_tracker()
    if not path:
        return "skipped: no prospect-tracker.csv on this node."
    for c in COLUMNS:
        if c not in cols:
            cols.append(c)

    done, skipped, found = [], 0, 0
    for row in _targets(rows, wave, brands):
        if (row.get("email") or "").strip() or (row.get("email_status") or "").strip():
            skipped += 1
            continue
        if len(done) >= max(1, int(limit)):
            break
        brand = (row.get("brand") or "").strip()
        hit = find_for_domain((row.get("domain") or "").strip())
        row["email"] = hit["email"]
        row["email_status"] = hit["status"]
        row["contact_name"] = hit["name"]
        row["contact_title"] = hit["title"]
        row["email_checked"] = _today()
        if hit["status"] == SENDABLE:
            found += 1
        done.append(f"{brand}: " + (f"{hit['email']} ({hit['status']})"
                                    if hit["email"] else "nothing found"))
    if not done:
        return (f"Nothing to look up — {skipped} row(s) already checked."
                if skipped else "No matching rows in the tracker.")
    _write_tracker(path, cols, rows)
    lines = [f"Checked {len(done)}, {found} verified sendable:"] + [f"- {d}" for d in done]
    if skipped:
        lines.append(f"({skipped} already had a result, not re-searched.)")
    return "\n".join(lines)


def wave_status(wave: str = "1") -> dict:
    """{sendable, risky, missing, rows} — the gate that should have existed."""
    _, _, rows = _read_tracker()
    out = {"sendable": [], "risky": [], "missing": []}
    for r in _targets(rows, wave=wave):
        brand = (r.get("brand") or "").strip()
        status = (r.get("email_status") or "").strip()
        entry = {"brand": brand, "email": (r.get("email") or "").strip(),
                 "who": " · ".join(x for x in ((r.get("contact_name") or "").strip(),
                                               (r.get("contact_title") or "").strip()) if x)}
        if status == SENDABLE and entry["email"]:
            out["sendable"].append(entry)
        elif status == RISKY and entry["email"]:
            out["risky"].append(entry)
        else:
            out["missing"].append(entry)
    return out


def wave_text(wave: str = "1") -> str:
    st = wave_status(wave)
    lines = [f"Wave {wave} send-readiness:"]
    for e in st["sendable"]:
        lines.append(f"✅ {e['brand']} — {e['email']}" + (f" ({e['who']})" if e["who"] else ""))
    for e in st["risky"]:
        lines.append(f"⚠️  {e['brand']} — {e['email']} unconfirmed (catch-all or "
                     f"low confidence). Your call; a bounce costs the domain.")
    for e in st["missing"]:
        lines.append(f"❌ {e['brand']} — no address yet.")
    if not st["sendable"] and not st["risky"]:
        lines.append("Nothing is sendable yet — run find_prospect_contacts first.")
    return "\n".join(lines)


TOOL_SCHEMAS = [
    {"name": "find_prospect_contacts",
     "description": "Look up and VERIFY the buyer's email address for prospects in "
                    "the outreach tracker, and write it in. Use before any wave "
                    "goes out. Never guesses an address — an unverified guess risks "
                    "a hard bounce, which is what would undo the domain warmup.",
     "input_schema": {"type": "object", "properties": {
         "wave": {"type": "string", "description": "Wave number, e.g. '1'."},
         "brands": {"type": "array", "items": {"type": "string"},
                    "description": "Specific brand names instead of a whole wave."},
         "limit": {"type": "integer",
                   "description": "Max lookups this run (default 6; free tier is "
                                  "25 searches/month)."}}}},
    {"name": "check_wave_ready",
     "description": "Whether a wave can actually be sent: which prospects have a "
                    "verified address, which are unconfirmed, which have none.",
     "input_schema": {"type": "object", "properties": {
         "wave": {"type": "string"}}}},
]

TOOL_STATUS_LABELS = {
    "find_prospect_contacts": "Finding and verifying who to email…",
    "check_wave_ready": "Checking the wave is actually sendable…",
}


def handle_tool_call(name: str, tool_input: dict) -> str:
    if name == "find_prospect_contacts":
        return fill_contacts(wave=str(tool_input.get("wave") or ""),
                             brands=tool_input.get("brands"),
                             limit=tool_input.get("limit") or 6)
    if name == "check_wave_ready":
        return wave_text(str(tool_input.get("wave") or "1"))
    return f"Unknown contact_finder tool: {name}"
