"""The pure halves of scripts/creator_weekly_cut.py: target parsing, moment picking, the index, the refusal."""
import datetime as dt, importlib.util, os
import pytest
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("creator_weekly_cut", os.path.join(HERE, "scripts", "creator_weekly_cut.py"))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)


def _c(i, views, dur=30, stream="v1", offset=None, created="2026-09-23T10:00:00Z"):
    return {"id": i, "title": f"t{i}", "views": views, "duration": dur, "stream": stream, "offset": offset, "created": created, "curator": "u"}


def test_target_parsing_defaults_to_twitch_and_rejects_unknown_platforms():
    assert m.parse_target("AnthonyZ") == ("twitch", "anthonyz")
    assert m.parse_target("kick:Konvy") == ("kick", "konvy")
    with pytest.raises(ValueError):
        m.parse_target("youtube:x")


def test_week_folder_is_the_iso_week():
    assert m.week_folder(dt.date(2026, 9, 25)) == "2026-W39"


def test_two_clips_of_the_same_moment_do_not_both_ship():
    """Sequisha's "open the doors" was clipped twice (5k and 2k views) inside the same ten seconds."""
    clips = [_c("a", 5000, offset=1000), _c("b", 2000, offset=1030), _c("c", 900, offset=5000), _c("d", 800, stream="v2", offset=1000)]
    assert [c["id"] for c in m.pick_moments(clips, n=3)] == ["a", "c", "d"]


def test_kick_clips_without_offsets_dedupe_on_clip_time():
    clips = [_c("a", 500, created="2026-09-23T10:00:00Z"), _c("b", 400, created="2026-09-23T10:01:00Z"), _c("c", 300, created="2026-09-23T12:00:00Z")]
    assert [c["id"] for c in m.pick_moments(clips, n=3)] == ["a", "c"]


def test_clips_too_short_for_a_hook_are_skipped_and_the_count_is_honoured():
    clips = [_c("a", 900, dur=5, stream="s1"), _c("b", 800, stream="s2"), _c("c", 700, stream="s3"), _c("d", 600, stream="s4"), _c("e", 500, stream="s5")]
    assert [c["id"] for c in m.pick_moments(clips, n=3)] == ["b", "c", "d"]


def test_index_has_one_line_per_clip_with_the_source_url():
    items = [(_c("Slug1", 1234, dur=30), "x_2026-W39_1_Slug1_1080x1920.mp4")]
    text = m.index_lines("twitch", "x", "2026-W39", items)
    assert "1. `x_2026-W39_1_Slug1_1080x1920.mp4`" in text and "1,234 views" in text
    assert "https://clips.twitch.tv/Slug1" in text
    assert m.clip_url("kick", "konvy", "clip_1") == "https://kick.com/konvy/clips/clip_1"


def test_refuses_the_clipbot_ready_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "fetch_twitch_clips", lambda login, first=20: [])
    assert m.main(["twitch:x", "--out", str(tmp_path / "ClipBot" / "ready"), "--dry-run"]) == 2


def test_no_clips_this_week_is_said_not_invented(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(m, "fetch_twitch_clips", lambda login, first=20: [])
    assert m.main(["twitch:x", "--out", str(tmp_path / "d"), "--dry-run"]) == 1
    assert "do not invent a moment" in capsys.readouterr().out
