"""
Tests for the Money Pipeline (Scouts → Council → Planner).

Run directly:  python3 test_money_pipeline.py
No network, no real Supabase, no real Claude — the same in-memory fake Supabase
and monkeypatched scouts/council approach used for the expansion pipeline.

Covers:
  1. idea DEDUPLICATION — by URL and by name (capability ideas have no URL),
     both vs the table and within one run
  2. council RUBRIC output format (required keys + decision in {pursue,reject,defer},
     unparseable rubric fail-safes to defer)
  3. calibration BACKSTOP — every downgrade rule (low plausibility, low autonomy,
     high risk, no margin, unquantified economics), clean pursue preserved,
     reject never upgraded, end-to-end override recorded
  4. planner GATE — refuses any idea not council-rated 'pursue'; a pursue idea
     gets its plan persisted and lands 'planned'
  5. query distillation fallback (no claude → keywords, never crashes)
  6. scout FAIL-SOFT — one scout raising never kills the run
  7. hard exclusions are baked into the scout + council prompts
"""

import json
import sys

import money_pipeline as mp


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
                   "created_at": f"2026-07-21T00:00:{rid:02d}-04:00"}
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
    mp.init(claude_client=None, supabase_client=FakeSB(), tool_dispatcher=None,
            council_call_fn=council_fn or (lambda s, u: ""), log_council_fn=lambda *a, **k: None,
            tools_list=tools or [], excluded_tools=set(),
            feasibility_fn=feasibility_fn or (lambda *a, **k: "feasible enough"))


def _patch_scouts(**by_name):
    """Point the scout registry at stubs; everything unnamed returns []."""
    for name in mp._SCOUTS:
        mp._SCOUTS[name] = by_name.get(name, lambda brief, cap: [])


# ============================================================
def test_dedup():
    print("\n=== 1. idea dedup (url + name) ===")
    _reset()
    # ideas already in the table: one with a URL, one name-only (capability style)
    mp._insert_idea({"name": "known site", "url": "https://example.com/known", "status": "found"})
    mp._insert_idea({"name": "Niche Newsletter", "url": "", "status": "found"})

    _patch_scouts(
        github=lambda brief, cap: [
            {"name": "known site", "url": "https://example.com/known/", "method": "x"},   # trailing slash → same
            {"name": "fresh idea", "url": "https://example.com/fresh", "method": "y"},
        ],
        capability=lambda brief, cap: [
            {"name": "niche  NEWSLETTER", "url": "", "method": "z"},   # name-normalized dup
            {"name": "fresh idea 2", "url": "https://example.com/fresh", "method": "y2"},  # url dup within run
            {"name": "report service", "url": "", "method": "w"},
        ],
    )
    out = mp.run_money_scouts(focus_brief="test", sources="all", cap=10)

    ideas = [json.loads(r["output_text"]) for r in mp.supabase.store["_all"]
             if r["agent_name"] == "money_idea"]
    urls = [i.get("url") for i in ideas]
    names = [i.get("name") for i in ideas]
    check("known URL not resubmitted (dedupe vs table)",
          urls.count("https://example.com/known/") == 0)
    check("name-only dup not resubmitted (case/space-insensitive)",
          "niche  NEWSLETTER" not in names)
    check("in-run URL duplicate collapsed",
          urls.count("https://example.com/fresh") == 1)
    check("genuinely new ideas inserted", "report service" in names and "fresh idea" in names)
    check("summary reports the new count", "queued 2 new" in out)


# ============================================================
def test_rubric_format():
    print("\n=== 2. council rubric format ===")

    good_rubric = {
        "plausibility": 4, "autonomy": 4, "setup_effort": "small",
        "est_monthly_profit_usd": 300, "est_monthly_cost_usd": 40, "risk": 2,
        "verdict": "Proven model, hands-off, healthy margin.", "decision": "pursue",
    }

    def council_good(system, user):
        # rubric call is the one whose system prompt is the rubric system
        if "MONEY-REVIEW" in system:
            return "```json\n" + json.dumps(good_rubric) + "\n```"
        return "- a bullet"   # advocate / critic

    _reset(council_good)
    rid = mp._insert_idea({"name": "cand", "url": "https://example.com/a", "status": "found"})
    res = mp.money_review_one(rid, mp._find_row(rid))

    required = {"plausibility", "autonomy", "setup_effort", "est_monthly_profit_usd",
                "est_monthly_cost_usd", "risk", "verdict", "decision"}
    check("rubric has all required keys", required.issubset(res["rubric"].keys()))
    check("decision ∈ {pursue,reject,defer}", res["decision"] in ("pursue", "reject", "defer"))
    check("pursue → idea status 'pursue'", mp._find_row(rid)["status"] == "pursue")
    check("rubric persisted onto idea", mp._find_row(rid)["council"]["plausibility"] == 4)

    # unparseable rubric must FAIL-SAFE to 'defer' (never silently pursue/drop)
    def council_junk(system, user):
        return "the council could not produce JSON, sorry"
    _reset(council_junk)
    rid2 = mp._insert_idea({"name": "c2", "url": "https://example.com/b", "status": "found"})
    res2 = mp.money_review_one(rid2, mp._find_row(rid2))
    check("unparseable rubric → deferred (fail-safe)", res2["decision"] == "defer"
          and mp._find_row(rid2)["status"] == "deferred")


# ============================================================
def test_calibration_backstop():
    print("\n=== 3. calibration backstop ===")
    base = {"plausibility": 4, "autonomy": 4, "est_monthly_profit_usd": 300,
            "est_monthly_cost_usd": 40, "risk": 2, "decision": "pursue"}

    d, why = mp._calibrate_decision(dict(base, plausibility=2))
    check("low plausibility pursue → defer", d == "defer" and "plausibility" in (why or ""))

    d, why = mp._calibrate_decision(dict(base, autonomy=1))
    check("low autonomy pursue → defer", d == "defer" and "autonomy" in (why or ""))

    d, why = mp._calibrate_decision(dict(base, risk=5))
    check("high risk pursue → defer", d == "defer" and "risk" in (why or ""))

    d, why = mp._calibrate_decision(dict(base, est_monthly_profit_usd=30, est_monthly_cost_usd=50))
    check("cost ≥ profit pursue → defer (no margin)", d == "defer" and "margin" in (why or ""))

    d, why = mp._calibrate_decision(dict(base, est_monthly_profit_usd="unknown"))
    check("unquantified economics pursue → defer", d == "defer" and "unquantified" in (why or ""))

    check("clean high-value pursue stays pursue", mp._calibrate_decision(base) == ("pursue", None))
    check("reject is preserved (never upgraded)",
          mp._calibrate_decision({"decision": "reject"})[0] == "reject")
    check("unrecognized decision → defer", mp._calibrate_decision({"decision": "yolo"})[0] == "defer")

    # end-to-end: model returns pursue+risk5, idea must land 'deferred' with the override recorded
    def council_riskpursue(system, user):
        if "MONEY-REVIEW" in system:
            return json.dumps(dict(base, risk=5, verdict="looks great"))
        return "- bullet"
    _reset(council_riskpursue)
    rid = mp._insert_idea({"name": "risky", "url": "https://example.com/z", "status": "found"})
    res = mp.money_review_one(rid, mp._find_row(rid))
    check("e2e: risky 'pursue' calibrated to deferred",
          res["decision"] == "defer" and mp._find_row(rid)["status"] == "deferred"
          and "calibration_override" in res["rubric"])


# ============================================================
def test_planner_gate():
    print("\n=== 4. planner gate ===")
    _reset()
    # planner refuses anything not council-rated 'pursue'
    for st in ("found", "under_review", "deferred", "rejected", "planned"):
        rid = mp._insert_idea({"name": f"idea-{st}", "url": "", "status": st})
        msg = mp.develop_money_idea(rid)
        check(f"planner refuses status '{st}'", "not 'pursue'" in msg)
    check("planner handles a missing id", "No money idea" in mp.develop_money_idea(99999))

    # a pursue idea gets a plan drafted, persisted, and lands 'planned'
    orig_call = mp._call
    mp._call = lambda s, u, max_tokens=2000: "## Setup (Alex, one time)\n- create the account\n## Build (Jarvis)\n1. build it"
    try:
        rid = mp._insert_idea({"name": "winner", "url": "", "status": "pursue",
                               "council": {"decision": "pursue"}})
        msg = mp.develop_money_idea(rid)
        row = mp._find_row(rid)
        check("pursue idea gets a plan drafted", "Launch plan drafted" in msg)
        check("plan persisted onto the row", "## Setup" in (row.get("plan") or ""))
        check("idea lands status 'planned'", row["status"] == "planned")
        check("output states nothing executed", "nothing executed" in msg)
    finally:
        mp._call = orig_call


# ============================================================
def test_query_distillation():
    print("\n=== 5. query distillation fallback ===")
    _reset()
    # no claude wired (claude=None) → distill falls back to keyword extraction, never crashes
    qs = mp._distill_queries("ways that jarvis could make money with newsletters and automation")
    check("distill returns at least one query", isinstance(qs, list) and len(qs) >= 1)
    check("fallback strips filler words", all("ways" not in q and "could" not in q for q in qs))
    check("fallback keeps signal words",
          any("newsletters" in q or "automation" in q or "jarvis" in q for q in qs))


# ============================================================
def test_scout_fail_soft():
    print("\n=== 6. scout fail-soft ===")
    _reset()

    def boom(brief, cap):
        raise RuntimeError("scout exploded")

    _patch_scouts(
        reddit=boom,
        capability=lambda brief, cap: [{"name": "survivor", "url": "", "method": "m"}],
    )
    out = mp.run_money_scouts(focus_brief="test", sources="all", cap=5)
    names = [json.loads(r["output_text"]).get("name") for r in mp.supabase.store["_all"]
             if r["agent_name"] == "money_idea"]
    check("a raising scout doesn't kill the run", "survivor" in names)
    check("run still reports its queue", "queued 1 new" in out)

    # sources filtering: an unknown source name is ignored, not crashed on
    _reset()
    _patch_scouts(capability=lambda brief, cap: [{"name": "only-cap", "url": "", "method": "m"}])
    mp.run_money_scouts(focus_brief="t", sources="capability,nonsense", cap=5)
    names = [json.loads(r["output_text"]).get("name") for r in mp.supabase.store["_all"]
             if r["agent_name"] == "money_idea"]
    check("sources filter runs only the named scouts", names == ["only-cap"])


# ============================================================
def test_hard_exclusions_in_prompts():
    print("\n=== 7. hard exclusions baked into prompts ===")
    for label, text in (("scout structuring prompt", mp._STRUCTURE_SYSTEM),
                        ("council rubric prompt", mp._RUBRIC_SYSTEM)):
        check(f"{label} carries the exclusion list", mp.HARD_EXCLUSIONS in text)
    for term in ("crypto", "gambling", "MLM", "terms of service"):
        check(f"exclusions mention {term}", term in mp.HARD_EXCLUSIONS)
    check("untrusted banner in rubric prompt", "UNTRUSTED" in mp._RUBRIC_SYSTEM)
    check("untrusted banner in structuring prompt", "UNTRUSTED" in mp._STRUCTURE_SYSTEM)


if __name__ == "__main__":
    test_dedup()
    test_rubric_format()
    test_calibration_backstop()
    test_planner_gate()
    test_query_distillation()
    test_scout_fail_soft()
    test_hard_exclusions_in_prompts()
    total, passed = len(_results), sum(_results)
    print(f"\n{'='*48}\n{passed}/{total} checks passed")
    sys.exit(0 if passed == total else 1)
