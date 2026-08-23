"""
contacts.py — turn a phone number into a person's name.

A notification that says "+13049067033 asked you to…" makes Alex do the lookup
himself, which is the work the assistant was supposed to remove. It also makes
the nudge easy to ignore, because an unrecognized number reads like spam. The
same message headed "Coach Staley" is instantly triageable.

Read-only, and LOCAL NODE ONLY: the macOS Contacts store lives on Alex's Mac
(`~/Library/Application Support/AddressBook/Sources/*/AddressBook-v22.abcddb`),
so the server can never resolve anything. That's why resolution happens at
INGEST time on the Mac and the resolved name is persisted onto the intake event
— by the time the server nudges, the name is already in the row. Anything
resolved later just degrades to the raw number, never to an error.

The DB is opened read-only via a file: URI, same stance as imessage_intake:
this module must be incapable of modifying his address book.
"""

import glob
import os
import re
import sqlite3
import threading
import time

_ADDRESSBOOK_GLOB = os.path.expanduser(
    "~/Library/Application Support/AddressBook/Sources/*/AddressBook-v22.abcddb")

_CACHE = {"map": None, "loaded_at": 0.0, "db": None}
_CACHE_TTL = 900          # contacts change rarely; a 15-min cache is plenty
_lock = threading.Lock()


def _digits(value: str) -> str:
    """Last 10 digits — the comparable core of a US number.

    Contacts stores '(845) 216-4428', iMessage hands us '+18452164428'. Both
    reduce to 8452164428, which is what makes them matchable."""
    d = re.sub(r"\D", "", str(value or ""))
    return d[-10:] if len(d) >= 10 else d


def _better_name(a: str, b: str) -> str:
    """Pick the more useful of two names for the SAME number.

    Duplicates are normal (an iCloud card plus a hand-added one). Prefer what
    Alex actually calls the person: 'Mom' beats 'Jeannine Hickey' in a nudge.
    Shorter usually means the nickname — but skip the sort-hack forms like
    'A Mom' that start with a stray single letter."""
    def penalty(n):
        toks = n.split()
        lead_initial = 2 if toks and len(toks[0]) == 1 else 0
        return (lead_initial, len(n))
    return a if penalty(a) <= penalty(b) else b


def _db_path():
    hits = sorted(glob.glob(_ADDRESSBOOK_GLOB))
    for p in hits:
        try:
            uri = f"file:{p}?mode=ro"
            con = sqlite3.connect(uri, uri=True, timeout=5)
            n = con.execute("select count(*) from ZABCDPHONENUMBER").fetchone()[0]
            con.close()
            if n:
                return p      # the top-level file is usually an empty shell
        except sqlite3.Error:
            continue
    return None


def _load(force: bool = False) -> dict:
    """{last10digits: name} plus {lowercased email: name}. {} when unavailable."""
    with _lock:
        fresh = (_CACHE["map"] is not None
                 and time.time() - _CACHE["loaded_at"] < _CACHE_TTL)
        if fresh and not force:
            return _CACHE["map"]
        path = _CACHE["db"] or _db_path()
        out = {}
        if path:
            _CACHE["db"] = path
            try:
                con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
                rows = con.execute("""
                    select p.ZFULLNUMBER,
                           trim(coalesce(r.ZFIRSTNAME,'') || ' ' || coalesce(r.ZLASTNAME,'')),
                           coalesce(r.ZORGANIZATION,'')
                    from ZABCDPHONENUMBER p
                    join ZABCDRECORD r on p.ZOWNER = r.Z_PK
                """).fetchall()
                for number, name, org in rows:
                    name = (name or "").strip() or (org or "").strip()
                    key = _digits(number)
                    if not name or len(key) < 7:
                        continue
                    out[key] = _better_name(out[key], name) if key in out else name
                try:
                    for addr, name, org in con.execute("""
                        select e.ZADDRESS,
                               trim(coalesce(r.ZFIRSTNAME,'') || ' ' || coalesce(r.ZLASTNAME,'')),
                               coalesce(r.ZORGANIZATION,'')
                        from ZABCDEMAILADDRESS e
                        join ZABCDRECORD r on e.ZOWNER = r.Z_PK
                    """).fetchall():
                        name = (name or "").strip() or (org or "").strip()
                        a = (addr or "").strip().lower()
                        if name and a:
                            out[a] = _better_name(out[a], name) if a in out else name
                except sqlite3.Error:
                    pass          # email table absent on some macOS versions
                con.close()
            except sqlite3.Error:
                out = {}
        _CACHE["map"] = out
        _CACHE["loaded_at"] = time.time()
        return out


def available() -> bool:
    return bool(_load())


def name_for(handle: str) -> str:
    """The contact name for a phone/email, or "" — never raises, never guesses."""
    h = str(handle or "").strip()
    if not h:
        return ""
    m = _load()
    if not m:
        return ""
    if "@" in h:
        return m.get(h.lower(), "")
    key = _digits(h)
    return m.get(key, "") if len(key) >= 7 else ""


def label_for(handle: str) -> str:
    """A display label: the name when known, else the handle unchanged.

    Used at ingest so the stored sender is already human-readable and the
    server — which has no address book — inherits the name for free."""
    return name_for(handle) or str(handle or "").strip()


def describe(handle: str) -> str:
    """Chat-tool answer for 'who is this number'."""
    h = str(handle or "").strip()
    if not h:
        return "Give me a number or email to look up."
    if not available():
        return ("No contacts database reachable — this only works on the Mac node, "
                "and it needs Contacts access for the Python binary.")
    n = name_for(h)
    return f"{h} is {n}." if n else f"{h} isn't in your contacts."


TOOL_SCHEMAS = [
    {"name": "who_is",
     "description": ("Look up who a phone number or email address belongs to in Alex's "
                     "macOS Contacts. Use when a notification, message, or intake item "
                     "shows a bare number and naming the person would help. Mac node only."),
     "input_schema": {"type": "object", "required": ["handle"], "properties": {
         "handle": {"type": "string", "description": "Phone number or email address."}}}},
]

TOOL_STATUS_LABELS = {"who_is": "Checking your contacts…"}
TOOL_NAMES = {t["name"] for t in TOOL_SCHEMAS}


def handle_tool_call(tool_name: str, tool_input: dict) -> str:
    if tool_name == "who_is":
        return describe(tool_input.get("handle", ""))
    return f"unknown tool {tool_name}"
