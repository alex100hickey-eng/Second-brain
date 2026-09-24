"""The pure halves of scripts/creator_sample_cut.py: SRT parsing, wrapping, the filter graph, the ready-folder refusal."""
import importlib.util, os
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("creator_sample_cut", os.path.join(HERE, "scripts", "creator_sample_cut.py"))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)

SRT = "1\n00:00:00,000 --> 00:00:02,520\n ♪ It uses head ♪\n\n2\n00:00:02,520 --> 00:00:05,060\n hello there\n\n3\n00:00:05,060 --> 00:00:06,380\n ♪ ♪\n"

def test_srt_parses_and_drops_music_only_cues():
    cues = m.parse_srt(SRT)
    assert cues == [(0.0, 2.52, "It uses head"), (2.52, 5.06, "hello there")]

def test_wrap_breaks_on_width_and_caps_lines():
    meas = lambda s: len(s) * 10
    assert m.wrap("one two three four five", meas, 95) == ["one two", "three", "four five"]
    assert len(m.wrap("a " * 40, meas, 50)) == 3

def test_filter_graph_has_one_overlay_per_cue_and_ends_in_vout():
    cues = [(0.0, 2.0, "a"), (2.0, 4.0, "b")]
    fc = m.build_filter(cues, [112, 112], 0.0, None)
    assert fc.count("overlay=") == 2 and fc.endswith("[vout]")
    assert "crop=ih*9/16:ih" in fc and "scale=1080:1920" in fc
    assert "between(t,0.00,2.00)" in fc

def test_a_start_offset_shifts_the_cue_times():
    fc = m.build_filter([(10.0, 12.0, "a")], [112], 10.0, 15.0)
    assert "between(t,0.00,2.00)" in fc

def test_refuses_to_write_into_the_clipbot_ready_folder(tmp_path):
    out = str(tmp_path / "ClipBot" / "ready" / "x.mp4")
    assert m.main(["file.mp4", out]) == 2
