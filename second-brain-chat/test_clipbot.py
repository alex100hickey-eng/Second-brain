"""clipbot tests — no network. ffmpeg smoke test runs only if ffmpeg is installed."""
import json
import os
import subprocess
import tempfile
import time

import pytest

from clipbot import config, hooks, posting, transform
from clipbot.ledger import Ledger
from clipbot.opus_api import OpusClient, estimate_credits, normalize_clips, project_id_from
from clipbot.runner import Runner, can_spend


@pytest.fixture(autouse=True)
def _no_live_kill_switch(monkeypatch):
    """The real KILL file pauses the launchd loop; tests must not see it."""
    monkeypatch.setattr(config, "kill_switch_on", lambda: False)


def _ledger():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return Ledger(path)


class FakeSession:
    """Records requests; answers with canned JSON."""

    def __init__(self, answers):
        self.answers, self.calls, self.headers = answers, [], {}

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        path = url.replace("https://api.opus.pro/api", "")
        class R:
            status_code = 200
            content = b"{}"
            def raise_for_status(self): pass
            def json(self_inner): return self.answers.get((method, path), {})
        return R()


def test_credits_and_ids():
    assert estimate_credits(4.5) == 10 and estimate_credits(62.9) == 62
    assert project_id_from({"data": {"projectId": "p1"}}) == "p1"
    assert project_id_from({"id": 7}) == "7"
    assert project_id_from({"nope": 1}) is None


def test_normalize_clips_handles_shapes():
    payload = {"data": {"list": [
        {"clipId": "c1", "title": "Big moment", "score": "87", "duration": 28.4, "previewUrl": "p", "uriForExport": "hd", "hashtags": ["fun"]},
        {"contentId": "proj.c2", "name": "Other", "viralityScore": 55},
        "junk",
    ]}}
    clips = normalize_clips(payload, "proj")
    assert [c["clip_id"] for c in clips] == ["c1", "c2"]
    assert clips[0]["score"] == 87.0 and clips[0]["hd_url"] == "hd" and clips[0]["duration_s"] == 28.4
    assert clips[1]["title"] == "Other" and clips[1]["score"] == 55.0
    assert normalize_clips([], "proj") == []


def test_client_request_shapes():
    fs = FakeSession({("POST", "/clip-projects"): {"projectId": "P9"}, ("GET", "/api-usage"): {"uncapped": False, "monthly": {"remaining": 500}}})
    c = OpusClient(api_key="k", session=fs)
    resp = c.create_project("https://drive/x", title="Ep 1", prompt="funny bits", durations=[[15, 35]], aspect="portrait")
    assert project_id_from(resp) == "P9"
    body = fs.calls[0][2]["json"]
    assert body["curationPref"] == {"model": "ClipAnything", "clipDurations": [[15, 35]], "customPrompt": "funny bits"}
    assert body["renderPref"] == {"layoutAspectRatio": "portrait"} and body["uploadedVideoAttr"] == {"title": "Ep 1"}
    c.schedule("P9", "c1", "acc", "2026-09-13T23:00:00Z", "T", "D")
    sched = fs.calls[-1][2]["json"]
    assert sched["publishAt"] == "2026-09-13T23:00:00Z" and sched["postDetail"]["custom"]["privacy"] == "public"
    assert c.usage()["monthly"]["remaining"] == 500
    assert not OpusClient(api_key=None).available


def test_hd_urls_via_collection():
    fs = FakeSession({("POST", "/collections"): {"data": {"collectionId": "COL1"}},
                      ("POST", "/collections/COL1/export"): {"data": {"contentList": [
                          {"contentId": "P9.c1", "uriForExport": "https://hd/c1.mp4"},
                          {"contentId": "P9.c2", "uriForExport": ""}]}}})
    c = OpusClient(api_key="k", session=fs)
    assert c.hd_urls_via_collection("P9", ["c1", "c2"]) == {"c1": "https://hd/c1.mp4"}
    adds = [call for call in fs.calls if call[1].endswith("/collection-contents")]
    assert [call[2]["json"]["contentId"] for call in adds] == ["P9.c1", "P9.c2"]


def test_poll_keeps_going_when_score_floor_drops_everything(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", str(tmp_path / "home"))
    monkeypatch.setattr(config, "READY_DIR", str(tmp_path / "ready"))
    monkeypatch.setattr(config, "HOOKS_DIR", str(tmp_path / "hooks"))
    monkeypatch.setattr(config, "INBOX_DIR", str(tmp_path / "inbox"))
    led = _ledger()

    class Client:
        available = True
        def clips(self, pid): return [{"clip_id": "c1", "title": "low", "score": 10.0, "duration_s": 20, "hd_url": "h", "preview_url": "", "hashtags": [], "transcript": "", "raw_keys": ["a"], "project_id": pid}]
        def usage(self): return {"uncapped": True}

    cfg = config.Config(min_score=50)
    r = Runner(cfg, led, Client(), log=lambda *_: None)
    cid = led.add_campaign("X")
    sid = led.add_source(cid, "u", "t", 10, 10)
    led.update_source(sid, status="submitted", opus_project_id="P1")
    assert r.poll_submitted() == 1
    assert led.sources("clipped")[0]["id"] == sid and led.clips() == []


def test_urls_file_hook_script_next_slot(tmp_path):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from clipbot.runner import parse_urls_file, write_hook_script
    p = tmp_path / "urls.txt"
    p.write_text("https://drive.google.com/x 62\nnot a url 5\nhttps://youtu.be/y notanumber\nhttps://youtu.be/z 30.5\n")
    assert parse_urls_file(str(p)) == [("https://drive.google.com/x", 62.0), ("https://youtu.be/z", 30.5)]
    hooks_dir = tmp_path / "hooks"
    path = write_hook_script(str(hooks_dir))
    assert path and os.path.exists(path) and "wait for this part" in open(path).read()
    assert write_hook_script(str(hooks_dir)) is None                # already written
    (hooks_dir / "line.m4a").write_bytes(b"0")
    os.remove(path)
    assert write_hook_script(str(hooks_dir)) is None                # audio exists → no script
    z = ZoneInfo("America/New_York")
    assert posting.next_slot("tiktok", datetime(2026, 9, 12, 10, 0, tzinfo=z)).hour == 19
    assert posting.next_slot("tiktok", datetime(2026, 9, 12, 20, 0, tzinfo=z)).day == 13


def test_prune_removes_only_finished_old_clips(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", str(tmp_path / "home"))
    monkeypatch.setattr(config, "READY_DIR", str(tmp_path / "ready"))
    monkeypatch.setattr(config, "HOOKS_DIR", str(tmp_path / "hooks"))
    monkeypatch.setattr(config, "INBOX_DIR", str(tmp_path / "inbox"))
    led = _ledger()
    r = Runner(config.Config(), led, OpusClient(api_key=None), log=lambda *_: None)
    cid = led.add_campaign("X")
    sid = led.add_source(cid, "u", "t", 10, 10)
    old = led.add_clip(sid, {"clip_id": "c1", "title": "old"})
    hd = tmp_path / "old.mp4"; hd.write_bytes(b"0")
    var = tmp_path / "old_tiktok.mp4"; var.write_bytes(b"0")
    led.update_clip(old, local_path=str(hd), status="transformed")
    vid = led.add_variant(old, "tiktok", str(var))
    led.conn.execute("UPDATE clips SET created=? WHERE id=?", (1.0, old)); led.conn.commit()
    assert r.prune(14) == 0 and hd.exists()                           # variant not posted yet
    led.mark_posted(vid, "https://x")
    assert r.prune(14) == 2 and not hd.exists() and not var.exists()


def test_can_spend_governor():
    led, cfg = _ledger(), config.Config()
    assert can_spend(led, cfg, 60, None) == (True, "ok")
    led.record_credits(280, "earlier")
    assert can_spend(led, cfg, 60, None)[1].startswith("weekly budget")
    assert can_spend(led, cfg, 10, {"uncapped": False, "monthly": {"remaining": 5}})[1].startswith("OpusClip monthly cap")
    assert can_spend(led, cfg, 10, {"uncapped": True}) == (True, "ok")


def test_wrap_text_and_cmd():
    assert transform.wrap_text("this is the part nobody talks about at all", 18) == "this is the part\nnobody talks about\nat all"
    assert transform.wrap_text("a " * 60, 18).count("\n") == 2 and transform.wrap_text("a " * 60, 18).endswith("…")
    cmd = transform.build_cmd("in.mp4", "out.mp4", "card.png", "middle", 0.3, 0.2, 1.03, 30.0, "hook.m4a", 2.5, 2.8)
    joined = " ".join(cmd)
    assert cmd[cmd.index("-ss") + 1] == "0.30" and cmd[cmd.index("-t") + 1] == "29.50"
    assert "-i hook.m4a -i card.png" in joined and "[1:a]aformat" in joined and "[v0][2:v]overlay=(W-w)/2:(H-h)/2" in joined
    assert "amix=inputs=2" in joined and "volume=0.2" in joined and "scale=trunc(iw*1.03/2)*2" in joined
    plain = " ".join(transform.build_cmd("in.mp4", "out.mp4", "card.png", "top", 0, 0, 1.0, 30.0))
    assert "anull" in plain and "amix" not in plain and "[v0][1:v]overlay=(W-w)/2:H*0.12" in plain
    bare = " ".join(transform.build_cmd("in.mp4", "out.mp4", None, "top", 0, 0, 1.0, None))
    assert "overlay" not in bare and " -t " not in bare


def test_caption_and_staging(tmp_path):
    camp = {"name": "Vyro MrBeast", "marketplace": "vyro", "rate_per_1k": 3.0, "cap_per_clip": 0, "hashtags": "#vyro beast"}
    clip = {"title": "He actually did it", "hashtags": ["#beast", "insane", "x", "y", "z", "w"], "score": 80, "duration_s": 28}
    title, body = posting.build_caption("tiktok", clip, camp, "wait for this part")
    assert title == "He actually did it"
    assert body.startswith("wait for this part\n\n#vyro #beast #insane #x #y #z") and "#w" not in body
    text = posting.caption_file_text("tiktok", title, body, camp, clip, 12)
    assert "POST WINDOW (ET): 7:00–9:30 PM" in text and "--variant 12" in text
    src = tmp_path / "v.mp4"
    src.write_bytes(b"00")
    dest = posting.stage_folder(str(src), "tiktok", title, text, 12, ready_dir=str(tmp_path / "ready"))
    assert dest.endswith("0012_He-actually-did-it.mp4") and os.path.exists(dest.replace(".mp4", ".txt"))


def test_hooks_pick_least_used(tmp_path):
    (tmp_path / "wait_for_it.m4a").write_bytes(b"0")
    (tmp_path / "nobody_saw_this.m4a").write_bytes(b"0")
    (tmp_path / "manifest.json").write_text('{"wait_for_it.m4a": "Wait for it."}')
    lib = hooks.library(str(tmp_path))
    assert {h["text"] for h in lib} == {"Wait for it.", "nobody saw this"}
    assert hooks.pick(lib, {"wait_for_it.m4a": 3})["name"] == "nobody_saw_this.m4a"
    assert hooks.pick(lib, {}, exclude={"nobody_saw_this.m4a"})["name"] == "wait_for_it.m4a"
    assert hooks.pick([], {}) is None and hooks.library(str(tmp_path / "missing")) == []


def test_ledger_flow_and_stats():
    led = _ledger()
    cid = led.add_campaign("Vyro MrBeast", "vyro", 3.0, 0, "#vyro", "funny", "tiktok,shorts")
    assert led.campaign("vyro mrbeast")["id"] == cid and led.campaign(cid)["rate_per_1k"] == 3.0
    sid = led.add_source(cid, "/x/ep1.mp4", "ep1", 60, 60)
    led.update_source(sid, status="submitted", opus_project_id="P1")
    assert led.sources("submitted")[0]["opus_project_id"] == "P1"
    clip_id = led.add_clip(sid, {"clip_id": "c1", "title": "T", "score": 70, "duration_s": 30})
    assert led.add_clip(sid, {"clip_id": "c1"}) == clip_id            # idempotent
    vid = led.add_variant(clip_id, "tiktok", "/v/1.mp4", "wait.m4a", "T")
    led.update_variant(vid, staged_path="/r/1.mp4", status="staged")
    led.mark_posted(vid, "https://tiktok/1")
    led.update_post(vid, views=5000, qualified_views=2000, usd_approved=6.0)
    s = led.stats()
    assert s["posts"] == 1 and s["views"] == 5000 and s["usd_expected"] == 6.0 and s["usd_approved"] == 6.0
    led.record_credits(60, "p1")
    assert led.credits_this_week() == 60
    led.bump_hook("wait.m4a"); led.bump_hook("wait.m4a")
    assert led.hook_uses() == {"wait.m4a": 2}
    led.set_kv("last_nudged_variant", 4)
    assert led.get_kv("last_nudged_variant") == 4 and "posts 1" in led.report()


def test_runner_queues_without_key(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", str(tmp_path / "home"))
    monkeypatch.setattr(config, "READY_DIR", str(tmp_path / "ready"))
    monkeypatch.setattr(config, "HOOKS_DIR", str(tmp_path / "hooks"))
    monkeypatch.setattr(config, "INBOX_DIR", str(tmp_path / "inbox"))
    led = _ledger()
    r = Runner(config.Config(), led, OpusClient(api_key=None), log=lambda *_: None)
    cid = r.add_campaign("Whop Podcast", "whop", 1.75, 100, "#pod")
    sid = r.ingest(cid, url="https://youtu.be/x", minutes=45)
    src = led.sources("queued")[0]
    assert src["id"] == sid and src["credits_est"] == 45 and "OPUSCLIP_API_KEY" in src["error"]
    assert r.ingest(cid, url="https://youtu.be/x", minutes=45) is None      # dedupe
    assert r.ingest("nope", url="https://youtu.be/y", minutes=1) is None
    assert r.process()["submitted"] == 0 and "MISSING" in r.status()


@pytest.mark.skipif(not transform.have_ffmpeg(), reason="ffmpeg not installed")
def test_ffmpeg_smoke(tmp_path):
    src = str(tmp_path / "src.mp4")
    hook = str(tmp_path / "hook.m4a")
    subprocess.run([transform.FFMPEG, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=720x1280:rate=30",
                    "-f", "lavfi", "-i", "sine=frequency=440", "-t", "6", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", src], check=True)
    subprocess.run([transform.FFMPEG, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=880", "-t", "1.5",
                    "-c:a", "aac", hook], check=True)
    dst = str(tmp_path / "out" / "v.mp4")
    transform.make_variant(src, dst, "this is the part nobody talks about", config.VARIANTS["shorts"], hook, 1.5)
    assert os.path.exists(dst)
    assert abs(transform.probe_duration(dst) - (6 - 0.3 - 0.2)) < 0.4
    probe = subprocess.run([transform.FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                            "-of", "csv=p=0", dst], capture_output=True, text=True, check=True).stdout.strip()
    assert probe == "1080,1920"


def test_rules_caption_and_defaults():
    camp = {"name": "FX Adults S2", "marketplace": "vyro", "rate_per_1k": 2.0, "cap_per_clip": 1000,
            "hashtags": "#adults #fxpartner",
            "rules": '{"extra_tags": false, "caption": "Watch Adults season 2 on FXX | Hulu", "tag": "@adultsfx",'
                     ' "voice": false, "min_seconds": 30}'}
    clip = {"title": "The group chat leak", "hashtags": ["#funny", "#lol"], "score": 80, "duration_s": 40}
    title, body = posting.build_caption("tiktok", clip, camp, "nobody prepares you for this")
    assert title == "The group chat leak"
    assert body == "nobody prepares you for this\n\nWatch Adults season 2 on FXX | Hulu\n\n@adultsfx #adults #fxpartner"
    rules = config.campaign_rules(camp)
    assert rules["voice"] is False and rules["text_hook"] is True and rules["min_seconds"] == 30
    assert config.campaign_rules({"rules": "not json"})["extra_tags"] is True and config.campaign_rules(None)["tag"] == ""
    led = _ledger()
    cid = led.add_campaign("X", rules={"direct": True, "bogus": 1})
    r = led.rules(led.campaign(cid))
    assert r["direct"] is True and "bogus" not in r
    # a DB from before per-campaign rules gets the column on open
    import sqlite3
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE campaigns (id INTEGER PRIMARY KEY, name TEXT UNIQUE, created REAL)")
    c.commit()
    c.close()
    assert "rules" in {row[1] for row in Ledger(path).conn.execute("PRAGMA table_info(campaigns)")}


def test_rules_drive_durations_and_direct_ingest(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", str(tmp_path / "home"))
    monkeypatch.setattr(config, "READY_DIR", str(tmp_path / "ready"))
    monkeypatch.setattr(config, "HOOKS_DIR", str(tmp_path / "hooks"))
    monkeypatch.setattr(config, "INBOX_DIR", str(tmp_path / "inbox"))
    led = _ledger()

    class FakeClient:
        available = True

        def __init__(self):
            self.created = []

        def usage(self):
            return None

        def upload_local(self, path, log):
            return "upl-1"

        def create_project(self, video_url, **kw):
            self.created.append((video_url, kw))
            return {"id": "P1"}

    fc = FakeClient()
    r = Runner(config.Config(), led, fc, log=lambda *_: None)
    cid = r.add_campaign("FX Adults S2", "vyro", 2.0, 1000, "#adults", "chaotic friend-group moments", "tiktok",
                         rules={"min_seconds": 30, "max_seconds": 120, "voice": False, "brand_template_id": "tpl-x"})
    r.ingest(cid, url="https://example.com/ep.mp4", minutes=12)
    url, kw = fc.created[0]
    assert url == "https://example.com/ep.mp4" and kw["durations"] == [[30.0, 120.0]] and kw["brand_template_id"] == "tpl-x"
    # direct ingest of a pre-cut clip: no OpusClip call, 0 credits, clip lands as downloaded
    src = tmp_path / "E6 1 Titled.mp4"
    src.write_bytes(b"00")
    monkeypatch.setattr(transform, "probe_duration", lambda p: 84.0)
    sid = r.ingest(cid, path=str(src), direct=True)
    assert len(fc.created) == 1 and led.sources("clipped")[0]["id"] == sid and led.credits_this_week() == 12
    clip = led.clips("downloaded")[0]
    assert clip["duration_s"] == 84.0 and clip["title"] == "E6 1 Titled"
    # transform honours the window: a 12 s clip is skipped; the 84 s one is made with no voice hook, text card kept
    short = led.add_clip(sid, {"clip_id": "s", "title": "short", "duration_s": 12})
    led.update_clip(short, local_path=str(src), status="downloaded", duration_s=12)
    made = []
    monkeypatch.setattr(transform, "make_variant",
                        lambda s, d, text, recipe, ha=None, hl=0.0, ts=2.8, log=None: made.append((text, ha)) or d)
    r.transform_downloaded()
    assert led.clip(short)["status"] == "skipped" and led.clip(clip["id"])["status"] == "transformed"
    assert made == [("E6 1 Titled", None)]
    # inbox folder for a direct campaign routes through the same path
    r2 = Runner(config.Config(), led, fc, log=lambda *_: None)
    did = r2.add_campaign("The Shards E6-7", "vyro", 2.0, 1000, "#TheShards", "", "tiktok", rules={"direct": True})
    d = tmp_path / "inbox" / "The Shards E6-7"
    d.mkdir(parents=True)
    f = d / "E7 2 Titled.mp4"
    f.write_bytes(b"00")
    os.utime(f, (time.time() - 600, time.time() - 600))
    assert r2.ingest_inbox() == 1 and len(fc.created) == 1
    assert led.campaign(did)["id"] == led.sources("clipped")[-1]["campaign_id"]


def test_ingest_claims_source_before_upload(tmp_path, monkeypatch):
    """The loop's queue sweep must not resubmit a source whose upload is still running (that cost 10 credits once)."""
    monkeypatch.setattr(config, "HOME", str(tmp_path / "home"))
    led = _ledger()
    seen = {}

    class SlowClient:
        available = True

        def usage(self):
            return None

        def upload_local(self, path, log):
            seen["status_during_upload"] = led.sources("submitting")[0]["status"]
            seen["queued_during_upload"] = led.sources("queued")
            return "upl"

        def create_project(self, video_url, **kw):
            return {"id": "P9"}

    r = Runner(config.Config(), led, SlowClient(), log=lambda *_: None)
    cid = r.add_campaign("C")
    f = tmp_path / "ep.mp4"
    f.write_bytes(b"00")
    monkeypatch.setattr(transform, "probe_duration", lambda p: 600.0)
    sid = r.ingest(cid, path=str(f))
    assert seen == {"status_during_upload": "submitting", "queued_during_upload": []}
    assert led.sources("submitted")[0]["id"] == sid and r.submit_queued() == 0


def test_hook_lines_rotate_and_card_text_has_no_emoji(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", str(tmp_path / "home"))
    monkeypatch.setattr(config, "HOOKS_DIR", str(tmp_path / "hooks"))
    assert transform.plain_text("Every friend group has one 💀  ok ✅") == "Every friend group has one ok"
    led = _ledger()
    r = Runner(config.Config(), led, OpusClient(api_key=None), log=lambda *_: None)
    cid = r.add_campaign("A", "vyro", 2.0, 0, "#a", "", "tiktok", rules={"voice": False, "hook_lines": ["one 😭", "two"]})
    src = tmp_path / "s.mp4"
    src.write_bytes(b"00")
    monkeypatch.setattr(transform, "probe_duration", lambda p: 40.0)
    sid = r.ingest(cid, path=str(src), direct=True)
    c1 = led.clips("downloaded")[0]["id"]
    c2 = led.add_clip(sid, {"clip_id": "x", "title": "Clickbait!!", "duration_s": 40})
    led.update_clip(c2, local_path=str(src), status="downloaded")
    cards = []
    monkeypatch.setattr(transform, "make_variant", lambda s, d, text, *a, **k: cards.append(text) or d)
    r.transform_downloaded()
    assert cards == ["two", "one"] and {led.clip(c1)["title"], led.clip(c2)["title"]} == {"two", "one 😭"}
    assert [v["text_hook"] for v in led.variants("made")] == ["two", "one 😭"]


def test_post_order_ranks_deadline_then_score(tmp_path):
    led = _ledger()
    a = led.add_campaign("Adults", rules={"ends": "2026-09-24", "per_day": 2})
    s = led.add_campaign("Shards", rules={"ends": "2026-09-14"})
    sa = led.add_source(a, "/a.mp4", "a", 10, 10)
    ss = led.add_source(s, "/s.mp4", "s", 2, 0)
    ids = []
    for src, score, name in ((sa, 60, "a-low"), (sa, 90, "a-high"), (sa, 70, "a-mid"), (ss, 0, "shard")):
        cid = led.add_clip(src, {"clip_id": name, "title": name, "score": score, "duration_s": 40})
        vid = led.add_variant(cid, "tiktok", f"/v/{name}.mp4", "", name)
        led.update_variant(vid, staged_path=f"/r/{name}.mp4", status="staged")
        ids.append(vid)
    led.mark_posted(ids[0], "https://t/1")                       # posted ones drop out
    rows = posting.post_order(led, today="2026-09-12")
    assert [r["file"] for r in rows] == ["shard.mp4", "a-high.mp4", "a-mid.mp4"]
    assert [r["day"] for r in rows] == [0, 0, 0] and rows[0]["days_left"] == 2
    text = posting.format_post_order(rows)
    assert "## Shards — TODAY · campaign ends in 2d" in text and "v2" in text and "a-low" not in text
    path = posting.write_post_order(led, ready_dir=str(tmp_path))
    assert path.endswith("POST ORDER.txt") and os.path.exists(path)


def test_fit_text_shrinks_before_truncating():
    assert transform.fit_text("wait for it") == (62, "wait for it")
    size, wrapped = transform.fit_text("Just a group of adults who have absolutely no idea what they’re doing")
    assert size < 62 and "…" not in wrapped and wrapped.count("\n") <= 2
    size, wrapped = transform.fit_text("word " * 40)
    assert size == 42 and wrapped.endswith("…") and wrapped.count("\n") == 3


def test_refresh_urlless_repolls_then_gives_up():
    led = _ledger()
    cid = led.add_campaign("C")
    sid = led.add_source(cid, "/x.mp4", "x", 10, 10)
    led.update_source(sid, status="clipped", opus_project_id="P1")
    a = led.add_clip(sid, {"clip_id": "a", "title": "a"})       # no urls yet
    b = led.add_clip(sid, {"clip_id": "b", "title": "b"})

    class Client:
        available = True
        calls = 0

        def clips(self, pid):
            Client.calls += 1
            return [{"clip_id": "a", "hd_url": "https://hd/a.mp4", "preview_url": "", "duration_s": 41, "score": 77}]

        def hd_urls_via_collection(self, pid, ids, name=""):
            assert name.startswith("clipbot P1 retry ")
            return {}

    r = Runner(config.Config(), led, Client(), log=lambda *_: None)
    assert r.refresh_urlless(max_tries=2) == 1
    assert led.clip(a)["hd_url"] == "https://hd/a.mp4" and led.clip(a)["duration_s"] == 41 and led.clip(b)["status"] == "new"
    r.refresh_urlless(max_tries=2)
    assert led.clip(b)["status"] == "new"                       # try 2 still allowed
    r.refresh_urlless(max_tries=2)
    assert led.clip(b)["status"] == "skipped" and Client.calls == 3
    assert r.refresh_urlless(max_tries=2) == 0                  # nothing pending: no call


def test_post_order_interleaves_caption_lines():
    led = _ledger()
    a = led.add_campaign("A", rules={"ends": "2026-09-24"})
    sa = led.add_source(a, "/a.mp4", "a", 10, 10)
    for i, (score, line) in enumerate([(99, "same"), (99, "same"), (99, "same"), (90, "other"), (80, "third")]):
        cid = led.add_clip(sa, {"clip_id": f"c{i}", "title": line, "score": score, "duration_s": 30})
        vid = led.add_variant(cid, "tiktok", f"/v/{i}.mp4", "", line)
        led.update_variant(vid, staged_path=f"/r/{i}.mp4", status="staged")
    lines = [r["line"] for r in posting.post_order(led, today="2026-09-12")]
    assert lines == ["same", "other", "same", "third", "same"]


def test_stalled_backlog_nudges_with_the_blockers(tmp_path, monkeypatch):
    """A staged backlog nobody is posting must break the silence. Sep 12-14 2026: 187 clips sat ready,
    nothing went out, and ready_nudge returned False every day because no NEW variant had been staged."""
    monkeypatch.setattr(config, "HOME", str(tmp_path / "home"))
    monkeypatch.setattr(config, "READY_DIR", str(tmp_path / "ready"))
    monkeypatch.setattr(config, "HOOKS_DIR", str(tmp_path / "hooks"))
    monkeypatch.setattr(config, "INBOX_DIR", str(tmp_path / "inbox"))
    sent = []
    monkeypatch.setattr("clipbot.notify.nudge", lambda title, body, **kw: sent.append((title, body)) or True)
    led = _ledger()
    r = Runner(config.Config(), led, OpusClient(api_key=None), log=lambda *_: None)
    cid = r.add_campaign("Battlbox", "vyro", 1.5, 1000, "#battlbox")
    sid = led.add_source(cid, "file:///a.mp4", "a", 10.0, 10)
    clip_id = led.add_clip(sid, {"clip_id": "c1", "title": "t", "score": 90, "start": 0, "end": 30})
    vid = led.add_variant(clip_id, "tiktok", str(tmp_path / "a.mp4"))
    led.update_variant(vid, status="staged", staged_path=str(tmp_path / "a.mp4"))

    # Fresh backlog, nothing ever posted -> the normal "clips ready" nudge.
    assert r.ready_nudge() is True and "ready to post" in sent[0][0]
    sent.clear()

    # Same backlog a day later, still nothing posted: silence before, a blocker-carrying nudge now.
    led.set_kv("blockers", json.dumps(["Vyro: check approvals"]))
    assert r.ready_nudge() is True
    assert sent[0][0] == "clipbot is stalled"
    assert "1 clips staged" in sent[0][1] and "Vyro: check approvals" in sent[0][1]

    # Once per day, not once per loop tick.
    sent.clear()
    assert r.ready_nudge() is False and sent == []

    # And a backlog that IS moving stays quiet.
    led.set_kv("last_stalled_nudge_day", "")
    led.mark_posted(vid, url="https://tiktok.com/x", posted_at=time.time())
    led.update_variant(vid, status="staged")
    assert r.ready_nudge() is False


def test_campaigns_that_forbid_transformation_are_flagged_as_account_risk(tmp_path, monkeypatch):
    """The Shards was the account's FIRST campaign — 6 pre-cut clips, no voice, no text card,
    posted Sep 12. Reach went to zero and never came back, and YouTube deleted the lot. A brief
    that bans every form of transformation costs the account, not just the payout."""
    monkeypatch.setattr(config, "HOME", str(tmp_path / "home"))
    monkeypatch.setattr(config, "READY_DIR", str(tmp_path / "ready"))
    monkeypatch.setattr(config, "HOOKS_DIR", str(tmp_path / "hooks"))
    monkeypatch.setattr(config, "INBOX_DIR", str(tmp_path / "inbox"))
    led = _ledger()
    r = Runner(config.Config(), led, OpusClient(api_key=None), log=lambda *_: None)

    shards = r.add_campaign("Shards", "vyro", 2.0, 1000, "#x")
    led.update_campaign(shards, rules=json.dumps(
        {"voice": False, "text_hook": False, "direct": True})) if hasattr(led, "update_campaign") else None
    row = dict(led.campaign(shards))
    row["rules"] = json.dumps({"voice": False, "text_hook": False, "direct": True})
    assert "unoriginal" in r.transformation_risk(row)

    battlbox = dict(led.campaign(r.add_campaign("Battlbox", "vyro", 1.5, 1000, "#y")))
    battlbox["rules"] = json.dumps({"extra_tags": False, "min_seconds": 20})
    assert r.transformation_risk(battlbox) == ""

    # one restriction alone is survivable; it takes two to make a repost
    one = dict(battlbox); one["rules"] = json.dumps({"voice": False})
    assert r.transformation_risk(one) == ""


def test_posting_policy_earns_volume_with_account_age(tmp_path, monkeypatch):
    """13 clips in ~5 hours on a one-day-old account is what took @wildest_moments to zero reach
    and never gave it back. Volume is earned by age, and rhythm beats bursts."""
    import time
    monkeypatch.setattr(config, "HOME", str(tmp_path / "home"))
    monkeypatch.setattr(config, "READY_DIR", str(tmp_path / "ready"))
    monkeypatch.setattr(config, "HOOKS_DIR", str(tmp_path / "hooks"))
    monkeypatch.setattr(config, "INBOX_DIR", str(tmp_path / "inbox"))
    r = Runner(config.Config(), _ledger(), OpusClient(api_key=None), log=lambda *_: None)

    assert r.posting_policy(0.5, 0)[0] == 0, "day zero must post nothing"
    assert r.posting_policy(3, 0)[0] == 1 and r.posting_policy(3, 1)[0] == 0
    assert r.posting_policy(10, 1)[0] == 1 and r.posting_policy(10, 2)[0] == 0
    assert r.posting_policy(30, 0)[0] >= 2, "a matured account may run normal volume"

    # the burst guard: a post 20 minutes ago blocks the next one regardless of the daily cap
    allowed, why = r.posting_policy(30, 0, last_post_ts=time.time() - 1200)
    assert allowed == 0 and "too recent" in why
    assert r.posting_policy(30, 0, last_post_ts=time.time() - 4 * 3600)[0] >= 2
