"""Between the gates: the scorecard's milestone list and weekend-aware targets (2026-09-25)."""
import importlib.util
import os
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("money_progress", os.path.join(HERE, "..", "scripts", "money_progress.py"))
mp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mp)


def test_weekend_gives_the_day_to_creators():
    sat, mon = mp.day_targets(date(2026, 9, 26)), mp.day_targets(date(2026, 9, 28))
    assert sat["sf_first_touches"] == 0 and sat["creator_sends"] == 10
    assert mon["sf_first_touches"] == 10 and mon["creator_sends"] == 5
    assert sat["clip_posts_per_account"] == mon["clip_posts_per_account"]


def _seed(tmp_path, monkeypatch, playbook=True):
    money = tmp_path / "Money"; site = tmp_path / "site"; deliv = tmp_path / "deliveries"; root = tmp_path / "root"
    (money / "Clients" / "spec-ads" / "first-touch-qa-2026-09-28").mkdir(parents=True)
    (site / "samples" / "a-1").mkdir(parents=True); (site / "samples" / "b-2").mkdir()
    (deliv / "someone").mkdir(parents=True); (root / "scripts").mkdir(parents=True)
    if playbook:
        (money / "Splitframe — Reply Playbook (2026-09-25).md").write_text("# playbook\nhttps://buy.stripe.com/test_abc")
    (money / "Clients" / "spec-ads" / "first-touch-qa-2026-09-28" / "INDEX.md").write_text(
        "| brand | file | verdict |\n|---|---|---|\n" + "".join(f"| b{i} | `b{i}.png` | approve |\n" for i in range(5)))
    (site / "index.html").write_text("<p>Your first drop is 15 ads for $650, delivered within 72 hours.</p>")
    (money / "Clients" / "sample-links.json").write_text("{}")
    (money / "Creator Lane — Long-form Prospects (2026-09-25).csv").write_text("name\n")
    (root / "scripts" / "splitframe_queue.py").write_text('sub.add_parser("reply", help="x")')
    (root / "second-brain-chat" / "polybot").mkdir(parents=True)
    (root / "second-brain-chat" / "polybot" / "server_move.py").write_text("# move")
    monkeypatch.setattr(mp, "MONEY", str(money)); monkeypatch.setattr(mp, "SITE_DIR", str(site))
    monkeypatch.setattr(mp, "DELIVERIES", str(deliv)); monkeypatch.setattr(mp, "ROOT", str(root))
    monkeypatch.setattr(mp, "whop_linked_accounts", lambda: 1)


def test_milestones_are_read_from_disk(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    ms = mp.milestones([{"close_variant": "arm-A"}, {"close_variant": ""}])
    assert len(ms) == 12 and all(m["done"] for m in ms)
    by = {m["name"]: m for m in ms}
    assert by["sample pages live on splitframestudio.com/samples"]["evidence"] == "2 pages"
    assert by["Monday statics approved in the ad layouts"]["evidence"] == "5 approved rows"


def test_a_missing_fact_reads_as_not_done(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch, playbook=False)
    ms = mp.milestones([])
    by = {m["name"]: m for m in ms}
    assert by["reply playbook written"]["done"] is False
    assert by["first-touch A/B live (arm-A vs arm-B rows)"]["done"] is False
    assert sum(1 for m in ms if not m["done"]) == 3
