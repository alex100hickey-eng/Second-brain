"""scripts/creator_sample_publish.py: slug, page, transcode ladder, and the git calls with the push stubbed."""
import importlib.util, os, subprocess
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("creator_sample_publish", os.path.join(HERE, "scripts", "creator_sample_publish.py"))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)


def test_slug_is_login_plus_eight_hex_and_stable():
    s = m.slug_for("AnthonyZ")
    assert s == "anthonyz-5b6a70c5" and s == m.slug_for("anthonyz")


def test_page_is_noindex_has_video_download_and_no_ai():
    p = m.page_html("Henya", 'the "okay, anyway" bit', 3560, "Tuesday")
    assert 'name="robots" content="noindex,nofollow,noarchive"' in p
    assert "<video controls playsinline" in p and 'href="clip.mp4" download' in p
    assert "From your Tuesday stream" in p and "3,560 views" in p and "&quot;okay, anyway&quot;" in p
    assert "Yours to post, no strings." in p and " AI" not in p and "<nav" not in p


def test_transcode_steps_crf_up_until_under_the_cap(tmp_path):
    dst = str(tmp_path / "clip.mp4"); sizes = iter([9_000_000, 7_000_000, 5_000_000])
    calls = []
    def run(cmd, check):
        calls.append(cmd); open(dst, "wb").write(b"x" * next(sizes))
    assert m.transcode("src.mp4", dst, run=run) == 5_000_000
    assert [c[c.index("-crf") + 1] for c in calls] == ["28", "30", "32"]
    assert "scale=720:1280" in calls[0] and "+faststart" in calls[0]


def test_publish_writes_page_commits_and_pushes_only_when_asked(tmp_path):
    site = tmp_path / "site"; (site / ".git").mkdir(parents=True)
    calls = []
    def run(cmd, check=True, capture_output=False, text=False):
        calls.append(cmd)
        if cmd[0].endswith("ffmpeg"):
            open(cmd[-1], "wb").write(b"v" * 100)
        return subprocess.CompletedProcess(cmd, 0, "", "")
    url = m.publish("s.mp4", "Kaise", title="honeymoon", site=str(site), push=False, run=run)
    assert url == "https://splitframestudio.com/samples/kaise-" + m.slug_for("kaise")[-8:] + "/"
    folder = site / "samples" / m.slug_for("kaise")
    assert (folder / "index.html").exists() and (folder / "clip.mp4").read_bytes() == b"v" * 100
    gits = [c for c in calls if c[0] == "git"]
    assert [c[3] for c in gits] == ["add", "commit"]
    m.publish("s.mp4", "Kaise", site=str(site), push=True, run=run)
    assert [c[3] for c in calls if c[0] == "git"][-1] == "push"
    assert calls[-1][3:] == ["push", "origin", "main"]


def test_refuses_the_clipbot_ready_folder(tmp_path):
    import pytest
    with pytest.raises(SystemExit):
        m.publish(str(tmp_path / "ClipBot" / "ready" / "x.mp4"), "x", site=str(tmp_path), push=False)


def test_refuses_when_site_repo_is_missing(tmp_path):
    import pytest
    with pytest.raises(SystemExit, match="site repo not found"):
        m.publish("s.mp4", "x", site=str(tmp_path), push=False)


def test_no_push_flag_prints_the_not_live_warning(tmp_path, capsys, monkeypatch):
    site = tmp_path / "site"; (site / ".git").mkdir(parents=True)
    def run(cmd, check=True, capture_output=False, text=False):
        if cmd[0].endswith("ffmpeg"): open(cmd[-1], "wb").write(b"v")
        return subprocess.CompletedProcess(cmd, 0, "", "")
    monkeypatch.setattr(m.subprocess, "run", run)
    assert m.main(["s.mp4", "Camy", "--site", str(site), "--no-push"]) == 0
    assert "NOT pushed" in capsys.readouterr().out
