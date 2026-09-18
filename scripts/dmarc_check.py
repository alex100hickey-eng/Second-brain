#!/usr/bin/env python3
"""Read the DMARC aggregate reports nobody was reading, and say whether sending is healthy.

Why this exists (2026-09-18): 23 cold emails had gone out and produced zero replies, and there
was no way to tell "the copy is not landing" from "the mail is not arriving". Bounces were being
watched, but a bounce is only the loudest failure — a message can be accepted and still be
filtered, and a domain can start failing authentication without a single bounce.

Meanwhile a DMARC aggregate report from Google had been arriving in the studio inbox every day
since August, containing exactly the missing data: how many messages each receiver saw, whether
SPF and DKIM passed and aligned, and what the receiver did with them. Twelve reports had arrived.
None had ever been opened.

What the first read found: 40 messages, every one disposition=none with DKIM and SPF passing —
so the sending setup is genuinely clean and the silence is not an authentication problem. Two
records (1 message each) failed both SPF and DKIM from a Google IP, which is what forwarding
looks like; harmless under p=none, and exactly what would start being dropped if the policy is
ever tightened to quarantine.

WHAT THIS CANNOT TELL YOU, said plainly because it is the tempting misread: DMARC reports say
whether mail AUTHENTICATED and what POLICY was applied. They do not say whether it reached the
inbox or the spam folder. "disposition: none" means DMARC did not cause filtering; it does not
mean the message was read. Inbox placement at this volume is not observable — Postmaster Tools
needs hundreds of messages a day — so the honest answer stays "auth is clean, keep sending".

It matters more now than last week: the daily cap ramps to 20/day, and a domain that starts
failing at that volume burns weeks of warmup before anyone notices by hand.

    python3 scripts/dmarc_check.py                 # last 30 days
    python3 scripts/dmarc_check.py --days 7 --json
"""
from __future__ import annotations

import argparse
import base64
import collections
import datetime as dt
import email
import gzip
import io
import json
import os
import sys
import xml.etree.ElementTree as ET
import zipfile

# A run of authentication failures is the domain's reputation going, and it is silent. These are
# the thresholds worth waking someone for; below them it is forwarding noise.
FAIL_ALERT_RATE, FAIL_ALERT_MIN = 0.15, 3
# Google reports daily. Silence for this long means the reports stopped, the mailbox changed, or
# the DMARC record lost its rua — all of which look identical to "everything is fine" otherwise.
STALE_DAYS = 4


def _reports_from_raw(raw_b64: str) -> list:
    """Every DMARC XML document attached to one raw RFC822 message."""
    out = []
    msg = email.message_from_bytes(
        base64.urlsafe_b64decode(raw_b64 + "=" * (-len(raw_b64) % 4)))
    for part in msg.walk():
        fn = part.get_filename()
        if not fn:
            continue
        data = part.get_payload(decode=True) or b""
        try:
            if fn.endswith(".gz"):
                xml = gzip.decompress(data)
            elif fn.endswith(".zip"):
                z = zipfile.ZipFile(io.BytesIO(data))
                xml = z.read(z.namelist()[0])
            else:
                xml = data
            out.append(xml)
        except Exception:                                  # noqa: BLE001
            continue
    return out


def parse_report(xml: bytes) -> dict:
    """One report → {org, day, records:[{count, disposition, dkim, spf, ip}]}."""
    root = ET.fromstring(xml)
    begin = int(root.findtext("report_metadata/date_range/begin", "0") or 0)
    day = dt.datetime.fromtimestamp(begin, dt.timezone.utc).strftime("%Y-%m-%d")
    records = []
    for rec in root.findall("record"):
        records.append({
            "count": int(rec.findtext("row/count", "0") or 0),
            "disposition": rec.findtext("row/policy_evaluated/disposition", "?"),
            "dkim": rec.findtext("row/policy_evaluated/dkim", "?"),
            "spf": rec.findtext("row/policy_evaluated/spf", "?"),
            "ip": rec.findtext("row/source_ip", "?"),
        })
    return {"org": root.findtext("report_metadata/org_name", "?"),
            "day": day,
            "policy": root.findtext("policy_published/p", "?"),
            "records": records}


def summarise(reports: list, today: str = "") -> dict:
    """The verdict, from parsed reports. Pure, so the thresholds are testable without a mailbox."""
    by_disp, by_org, by_day = collections.Counter(), collections.Counter(), collections.Counter()
    total = failed = 0
    problems = []
    for r in reports:
        for rec in r["records"]:
            n = rec["count"]
            total += n
            by_disp[rec["disposition"]] += n
            by_org[r["org"]] += n
            by_day[r["day"]] += n
            bad_auth = rec["dkim"] != "pass" or rec["spf"] != "pass"
            if bad_auth:
                failed += n
            if bad_auth or rec["disposition"] != "none":
                problems.append({**rec, "org": r["org"], "day": r["day"]})
    rate = (failed / total) if total else 0.0
    alerts = []
    if failed >= FAIL_ALERT_MIN and rate >= FAIL_ALERT_RATE:
        alerts.append(f"{failed} of {total} reported messages failed SPF or DKIM ({rate:.0%}). "
                      "That is the sending domain's reputation, not one lost email.")
    quarantined = by_disp.get("quarantine", 0) + by_disp.get("reject", 0)
    if quarantined:
        alerts.append(f"{quarantined} message(s) were quarantined or rejected by a receiver. "
                      "Stop sending and look before the domain is burned.")
    latest = max(by_day) if by_day else ""
    if today and latest:
        gap = (dt.date.fromisoformat(today) - dt.date.fromisoformat(latest)).days
        if gap >= STALE_DAYS:
            alerts.append(f"No DMARC report covering the last {gap} days. Either nothing is "
                          "being sent, or the rua address stopped receiving them — both worth "
                          "checking, because this is the only delivery signal there is.")
    return {"total": total, "failed": failed, "fail_rate": round(rate, 3),
            "by_disposition": dict(by_disp), "by_org": dict(by_org),
            "by_day": dict(sorted(by_day.items())), "problems": problems,
            "alerts": alerts, "latest_day": latest,
            "policy": reports[-1]["policy"] if reports else "?"}


def fetch_reports(days: int = 30) -> list:
    """Pull and decode every DMARC report in the studio mailbox. Needs the studio connector."""
    sys.path.insert(0, os.path.expanduser("~/second-brain/second-brain-chat"))
    from dotenv import load_dotenv                          # type: ignore
    load_dotenv(os.path.expanduser("~/second-brain/.env"))
    from composio import Composio                           # type: ignore
    c = Composio(api_key=os.environ["COMPOSIO_API_KEY"])
    ent = os.environ.get("STUDIO_GMAIL_ENTITY")
    if not ent:
        raise RuntimeError("STUDIO_GMAIL_ENTITY is not set — the reports live in the studio mailbox")
    listing = c.tools.execute(
        "GMAIL_FETCH_EMAILS", user_id=ent, dangerously_skip_version_check=True,
        arguments={"query": f"subject:(Report domain) newer_than:{days}d", "max_results": 50})
    msgs = (listing.get("data", listing) or {}).get("messages") or []
    out = []
    for m in msgs:
        mid = m.get("messageId") or m.get("id")
        if not mid:
            continue
        try:
            got = c.tools.execute(
                "GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID", user_id=ent,
                dangerously_skip_version_check=True,
                arguments={"message_id": mid, "format": "raw"})
            raw = (got.get("data", got) or {}).get("raw")
            if not raw:
                continue
            for xml in _reports_from_raw(raw):
                out.append(parse_report(xml))
        except Exception as e:                              # noqa: BLE001
            print(f"  skipped {mid}: {str(e)[:100]}", file=sys.stderr)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    reports = fetch_reports(a.days)
    if not reports:
        print("No DMARC reports found. That is itself a finding — they should arrive daily.")
        return 1
    s = summarise(reports, dt.date.today().isoformat())
    if a.json:
        print(json.dumps(s, indent=1))
        return 0
    print(f"DMARC — splitframestudio.com, last {a.days} days ({len(reports)} report(s), "
          f"policy p={s['policy']})")
    print(f"  {s['total']} messages seen by receivers; {s['failed']} failed auth "
          f"({s['fail_rate']:.0%})")
    print(f"  disposition: {s['by_disposition']}")
    print(f"  reporting receivers: {s['by_org']}")
    print("\n  per day:")
    for day, n in s["by_day"].items():
        print(f"    {day}  {n}")
    if s["problems"]:
        print(f"\n  records with an auth or policy problem ({len(s['problems'])}):")
        for p in s["problems"][:10]:
            print(f"    {p['day']} {p['ip']} x{p['count']} — disposition {p['disposition']}, "
                  f"dkim {p['dkim']}, spf {p['spf']}")
    for al in s["alerts"]:
        print(f"\n  ** {al}")
    if not s["alerts"]:
        print("\n  Healthy: authenticating and accepted. Note this says nothing about INBOX vs "
              "spam placement — DMARC cannot see that, and at this volume nothing can.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
