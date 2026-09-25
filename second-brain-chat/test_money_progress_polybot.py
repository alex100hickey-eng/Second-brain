"""The money scorecard's polybot lane reads the loop wherever it runs (polybot/SERVER_MOVE.md)."""
import importlib.util
import os
import time

HERE = os.path.dirname(os.path.abspath(__file__))
# this checkout's copy, not ~/second-brain's: a worktree must test its own code
_spec = importlib.util.spec_from_file_location("money_progress", os.path.join(HERE, "..", "scripts", "money_progress.py"))
mp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mp)

REPORT = """polybot report — last 1d — 2026-09-25 07:00
  maker_rewards incentive accrual (ESTIMATE, paper): $13.10 in 1d, $13.10 since the gate reset, 6 market(s) quoted
  gate bucket_sum             hold — 19/30 signals
       eta 19/30 decisions at 2.9/day (7d), US 19/10 at 2.9/day -> ~4 day(s) to the counts
  gate leadlag                PASS — all checks
  compounding: off — caps are the fixed dollars in config.json"""


def _state(node="server", beat_age_s=60, report_age_s=300, now=1_800_000_000.0):
    from datetime import datetime, timezone
    beat = datetime.fromtimestamp(now - beat_age_s, tz=timezone.utc).isoformat()
    return lambda keys: {"business:polybot": {"key": "business:polybot", "node": node, "report": REPORT,
                                               "report_at": now - report_age_s},
                         "heartbeat:polybot": {"key": "heartbeat:polybot", "beat_at": beat}}


def test_server_rows_give_gates_etas_accrual_and_alive():
    now = 1_800_000_000.0
    pb = mp.polybot_remote(now=now, state_fn=_state(now=now))
    assert pb["source"] == "server" and pb["alive"] and pb["log_age_min"] == 1
    assert pb["gates"]["bucket_sum"] == "19/30 signals" and pb["passing"] == ["leadlag"]
    assert pb["etas"]["bucket_sum"].endswith("~4 day(s) to the counts")
    assert pb["accrual"].startswith("maker_rewards incentive accrual") and pb["error"] == ""


def test_a_stale_server_heartbeat_is_not_alive_and_an_old_report_says_so():
    now = 1_800_000_000.0
    pb = mp.polybot_remote(now=now, state_fn=_state(beat_age_s=20 * 60, report_age_s=2 * 3600, now=now))
    assert not pb["alive"] and pb["log_age_min"] == 20
    assert pb["error"] == "server report 120 min old" and pb["gates"]      # still shows what it last knew


def test_rows_from_the_mac_loop_or_no_rows_are_not_the_server():
    assert mp.polybot_remote(state_fn=_state(node="mac:alexs-mbp")) is None
    assert mp.polybot_remote(state_fn=lambda keys: {}) is None             # store unreachable / never published


def test_a_fresh_mac_log_never_asks_the_store(tmp_path, monkeypatch):
    log = tmp_path / "loop.log"
    log.write_text("09-25 07:00:31 polybot report\n")
    monkeypatch.setattr(mp, "POLYBOT_LOG", str(log))
    monkeypatch.setattr(mp, "ROOT", str(tmp_path))          # the local report subprocess finds nothing: fine
    asked = []
    pb = mp.polybot_metrics(remote_fn=lambda: asked.append(1) or {"source": "server"})
    assert asked == [] and pb["source"] == "mac" and pb["alive"]


def test_a_quiet_mac_log_switches_to_the_server_and_falls_back_without_it(tmp_path, monkeypatch):
    log = tmp_path / "loop.log"
    log.write_text("old\n")
    old = time.time() - 3600
    os.utime(log, (old, old))
    monkeypatch.setattr(mp, "POLYBOT_LOG", str(log))
    monkeypatch.setattr(mp, "ROOT", str(tmp_path))
    served = {"alive": True, "gates": {"bucket_sum": "19/30 signals"}, "passing": [], "etas": {}, "accrual": "",
              "error": "", "source": "server"}
    assert mp.polybot_metrics(remote_fn=lambda: served) is served
    pb = mp.polybot_metrics(remote_fn=lambda: None)                    # still on the Mac, just dead
    assert pb["source"] == "mac" and not pb["alive"] and pb["log_age_min"] == 60
    pb = mp.polybot_metrics(remote_fn=lambda: 1 / 0)                   # the store blew up: Mac path
    assert pb["source"] == "mac"


def test_the_report_parser_is_the_one_both_paths_use():
    out = dict(gates={}, passing=[], etas={}, accrual="")
    mp._parse_report(REPORT, out)
    assert set(out["gates"]) == {"bucket_sum", "leadlag"} and "leadlag" not in out["etas"]
