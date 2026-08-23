"""
Tests for contact resolution (contacts.py) and date-anchored notification
wording (proactive._when_words).

Run:  python3 test_contacts_and_dates.py
No network. Contacts tests use a synthetic AddressBook sqlite file, so they
pass on a machine with no Contacts data at all.

Both behaviors here come from real notifications Alex received:
  * "DUE in 1h (10:00am) — Lifting session tomorrow at 10am" for a lift that
    had happened the previous day (relative date resolved against the SCAN
    date instead of the message's send date).
  * "⏰ Today in 23h (8:00am)" — a title claiming today about tomorrow.
  * "From +16307306461" instead of "From Logan Case".
"""

import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import contacts
import proactive

PASS, FAIL = "PASS    ", "**FAIL**"
_results = []
TZ = ZoneInfo("America/New_York")


def check(label, cond):
    _results.append(bool(cond))
    print(f"{PASS if cond else FAIL} {label}")


def _fake_addressbook(rows, emails=()):
    """Build a minimal AddressBook-shaped sqlite file and point contacts at it."""
    d = tempfile.mkdtemp()
    path = os.path.join(d, "AddressBook-v22.abcddb")
    con = sqlite3.connect(path)
    con.execute("create table ZABCDRECORD (Z_PK integer primary key, "
                "ZFIRSTNAME text, ZLASTNAME text, ZORGANIZATION text)")
    con.execute("create table ZABCDPHONENUMBER (Z_PK integer primary key, "
                "ZOWNER integer, ZFULLNUMBER text)")
    con.execute("create table ZABCDEMAILADDRESS (Z_PK integer primary key, "
                "ZOWNER integer, ZADDRESS text)")
    for i, (num, first, last, org) in enumerate(rows, 1):
        con.execute("insert into ZABCDRECORD values (?,?,?,?)", (i, first, last, org))
        con.execute("insert into ZABCDPHONENUMBER values (?,?,?)", (i, i, num))
    for j, (addr, owner) in enumerate(emails, len(rows) + 1):
        con.execute("insert into ZABCDEMAILADDRESS values (?,?,?)", (j, owner, addr))
    con.commit()
    con.close()
    contacts._CACHE.update({"map": None, "loaded_at": 0.0, "db": path})
    return path


# ============================================================
def test_number_normalization():
    print("\n=== 1. number formats all resolve to the same person ===")
    _fake_addressbook([("(845) 216-4428", "Jeannine", "Hickey", "")])
    for fmt in ("+18452164428", "845-216-4428", "(845) 216-4428", "8452164428",
                "1 845 216 4428"):
        check(f"{fmt} resolves", contacts.name_for(fmt) == "Jeannine Hickey")
    check("an unknown number resolves to nothing",
          contacts.name_for("+19995550000") == "")
    check("garbage input is safe", contacts.name_for("") == ""
          and contacts.name_for(None) == "")


def test_duplicate_preference():
    print("\n=== 2. duplicates prefer what Alex actually calls them ===")
    _fake_addressbook([
        ("+18452164428", "Jeannine", "Hickey", ""),
        ("+18452164428", "Mom", "", ""),
        ("+18452164428", "A Mom", "", ""),      # sort-hack duplicate
    ])
    got = contacts.name_for("+18452164428")
    check("the nickname wins over the full legal name", got == "Mom")
    check("a leading-initial sort hack loses", got != "A Mom")


def test_label_and_describe():
    print("\n=== 3. label_for degrades to the raw handle, never to an error ===")
    _fake_addressbook([("+13049067033", "Coach", "Staley", "Case Western")])
    check("known number becomes a name",
          contacts.label_for("+13049067033") == "Coach Staley")
    check("unknown number passes through UNCHANGED (never blank)",
          contacts.label_for("+15550001111") == "+15550001111")
    check("describe names the person", "Coach Staley" in contacts.describe("+13049067033"))
    check("describe is honest about a miss",
          "isn't in your contacts" in contacts.describe("+15550001111"))

    # An organization-only card still gives something better than digits.
    _fake_addressbook([("+18005550000", "", "", "Quest Diagnostics")])
    check("org-only contact still resolves",
          contacts.name_for("+18005550000") == "Quest Diagnostics")


def test_no_addressbook():
    print("\n=== 4. no address book (the server) degrades quietly ===")
    contacts._CACHE.update({"map": None, "loaded_at": 0.0,
                            "db": "/nonexistent/AddressBook-v22.abcddb"})
    check("available() is False, not an exception", contacts.available() is False)
    check("label_for still returns the handle",
          contacts.label_for("+13049067033") == "+13049067033")
    check("describe explains rather than failing",
          "Mac node" in contacts.describe("+13049067033"))


def test_relative_shift_limits():
    print("\n=== 6. each relative word gets its own defensible shift ===")
    import intake
    # "tonight" from a Saturday-8pm message means SATURDAY. Resolving it to
    # Sunday is the live bug that buzzed Alex about the previous night's event;
    # a single shared threshold of +1 let it through because +1 is correct for
    # "tomorrow".
    check("tonight may not move off the send day",
          intake._max_date_shift("practice tonight at 9") == 0)
    check("today may not move either",
          intake._max_date_shift("finish it today") == 0)
    check("tomorrow is allowed exactly one day",
          intake._max_date_shift("lifting tomorrow at 10") == 1)
    check("this weekend gets a week of slack",
          intake._max_date_shift("free this weekend?") == 7)
    check("an absolute date has no relative word to bound",
          intake._max_date_shift("exam on Sep 11") is None)
    check("the widest word wins when several appear",
          intake._max_date_shift("tonight or maybe this weekend") == 7)


def test_when_words_day_accuracy():
    print("\n=== 5. notification wording names the right DAY ===")
    real_now = proactive._now
    now = datetime(2026, 8, 23, 9, 0, tzinfo=TZ)
    proactive._now = lambda: now
    proactive.LOCAL_TZ = TZ
    try:
        def words(dt):
            return proactive._when_words((dt - now).total_seconds() / 3600, dt.isoformat())

        soon = words(now + timedelta(hours=2))
        check("same-day says today", "today" in soon.lower())
        # The live bug: 23h away was titled "Today in 23h".
        tmw = words(now + timedelta(hours=23))
        check("23h away says TOMORROW, not today",
              "TOMORROW" in tmw and "today" not in tmw.lower())
        check("a 2-day-out item names its weekday",
              "Aug 25" in words(now + timedelta(days=2)))
        check("under an hour is 'within the hour'",
              "within the hour" in words(now + timedelta(minutes=30)))
        past = words(now - timedelta(days=1))
        check("something already gone never reads as NOW",
              "already passed" in past and "NOW" not in past)
        check("due right now still reads NOW",
              "NOW" in words(now - timedelta(minutes=5)))
    finally:
        proactive._now = real_now


# ============================================================
if __name__ == "__main__":
    test_number_normalization()
    test_duplicate_preference()
    test_label_and_describe()
    test_no_addressbook()
    test_relative_shift_limits()
    test_when_words_day_accuracy()
    total, passed = len(_results), sum(_results)
    print("\n" + "=" * 48)
    print(f"{passed}/{total} checks passed")
    print("=" * 48)
    sys.exit(0 if passed == total else 1)
