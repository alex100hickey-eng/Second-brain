"""Tests for scripts/dmarc_check.py. No network — summarise() is pure on purpose.

The thresholds are the whole value of the file: it exists to notice the sending domain going bad
BEFORE anyone reads a report by hand, at a moment when the daily cap is ramping to 20/day.
"""
import datetime as dt
import importlib.util
import os

SPEC = importlib.util.spec_from_file_location(
    "dmarc_check", os.path.expanduser("~/second-brain/scripts/dmarc_check.py"))
dc = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(dc)


def _rec(count=1, disposition="none", dkim="pass", spf="pass", ip="1.2.3.4"):
    return {"count": count, "disposition": disposition, "dkim": dkim, "spf": spf, "ip": ip}


def _report(records, org="google.com", day="2026-09-17", policy="none"):
    return {"org": org, "day": day, "policy": policy, "records": records}


def test_clean_sending_raises_nothing():
    s = dc.summarise([_report([_rec(count=20)])], "2026-09-18")
    assert s["total"] == 20 and s["failed"] == 0 and s["alerts"] == []


def test_a_run_of_auth_failures_alerts():
    """Reputation goes silently: no bounce, no reply, nothing — just filtering."""
    s = dc.summarise([_report([_rec(count=10), _rec(count=5, dkim="fail", spf="fail")])],
                     "2026-09-18")
    assert s["failed"] == 5 and s["fail_rate"] == 0.333
    assert any("failed SPF or DKIM" in a for a in s["alerts"])


def test_one_forwarded_message_is_not_an_alert():
    """Forwarding breaks SPF and DKIM by design. Two of 36 real messages looked exactly like
    this and are harmless — alerting on them would train everyone to ignore the alert."""
    s = dc.summarise([_report([_rec(count=34), _rec(count=2, dkim="fail", spf="fail")])],
                     "2026-09-18")
    assert s["failed"] == 2 and s["alerts"] == []
    assert len(s["problems"]) == 1, "still reported as a problem row, just not alerted"


def test_any_quarantine_or_reject_alerts_immediately():
    """Unlike an auth blip, a receiver actually filtering is never noise."""
    s = dc.summarise([_report([_rec(count=50), _rec(count=1, disposition="quarantine")])],
                     "2026-09-18")
    assert any("quarantined or rejected" in a for a in s["alerts"])
    s2 = dc.summarise([_report([_rec(count=50), _rec(count=1, disposition="reject")])],
                      "2026-09-18")
    assert any("quarantined or rejected" in a for a in s2["alerts"])


def test_reports_going_silent_is_itself_the_finding():
    """No report looks identical to a clean report if nobody checks the date. Silence can mean
    the rua address stopped receiving them — losing the only delivery signal there is."""
    s = dc.summarise([_report([_rec(count=5)], day="2026-09-01")], "2026-09-18")
    assert any("No DMARC report" in a for a in s["alerts"])


def test_recent_reports_are_not_stale():
    s = dc.summarise([_report([_rec(count=5)], day="2026-09-17")], "2026-09-18")
    assert not any("No DMARC report" in a for a in s["alerts"])


def test_counts_split_by_day_org_and_disposition():
    s = dc.summarise([
        _report([_rec(count=3)], day="2026-09-16"),
        _report([_rec(count=4)], day="2026-09-17"),
        _report([_rec(count=2)], org="yahoo.com", day="2026-09-17"),
    ], "2026-09-18")
    assert s["by_day"] == {"2026-09-16": 3, "2026-09-17": 6}
    assert s["by_org"] == {"google.com": 7, "yahoo.com": 2}
    assert s["by_disposition"] == {"none": 9}
    assert s["latest_day"] == "2026-09-17"


def test_parse_report_reads_a_real_shaped_document():
    xml = b"""<?xml version="1.0"?><feedback>
      <report_metadata><org_name>google.com</org_name>
        <date_range><begin>1789603200</begin><end>1789689599</end></date_range></report_metadata>
      <policy_published><domain>splitframestudio.com</domain><p>none</p></policy_published>
      <record><row><source_ip>209.85.220.41</source_ip><count>5</count>
        <policy_evaluated><disposition>none</disposition><dkim>pass</dkim><spf>pass</spf>
        </policy_evaluated></row></record></feedback>"""
    r = dc.parse_report(xml)
    assert r["org"] == "google.com" and r["policy"] == "none"
    assert r["records"] == [{"count": 5, "disposition": "none", "dkim": "pass",
                             "spf": "pass", "ip": "209.85.220.41"}]


def test_empty_input_is_safe():
    s = dc.summarise([], "2026-09-18")
    assert s["total"] == 0 and s["alerts"] == [] and s["policy"] == "?"


def test_the_daily_pass_is_wired_into_the_mail_worker():
    """A check nobody runs is the same as no check. It rides the existing 15-minute mail
    worker but guards itself to once a calendar day, because the reports are daily."""
    src = open(os.path.expanduser("~/second-brain/second-brain-chat/app.py")).read()
    assert '_try("dmarc", _dmarc_pass)' in src, "not scheduled"
    body = src[src.index("def _dmarc_pass"):src.index("def _outbox_sent_pass")]
    assert 'st.get("checked") == today' in body, "would re-read the mailbox every 15 minutes"
    assert "proactive.send_nudge" in body, "an alert nobody is told about is not an alert"


def test_the_daily_pass_uses_the_module_alias_for_sys():
    """app.py imports sys as _sys. A plain `sys` in the pass NameErrors, and the mail worker's
    _try() swallows it into 'dmarc: FAILED' forever — the silent-failure shape that has cost
    this project more time than any other bug class."""
    src = open(os.path.expanduser("~/second-brain/second-brain-chat/app.py")).read()
    body = src[src.index("def _dmarc_pass"):src.index("def _outbox_sent_pass")]
    assert "_sys.path.insert" in body
    assert "sys.path.insert" not in body.replace("_sys.path.insert", "")
