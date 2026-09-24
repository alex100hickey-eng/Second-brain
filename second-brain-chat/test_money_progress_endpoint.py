"""The money-progress feed: right token serves the vault's progress.json read-only,
wrong token is a bare 404, missing file is a 503 the page can show."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
os.environ.setdefault("JARVIS_TEST", "1")


def _app(tmp_path, monkeypatch):
    monkeypatch.setenv("ACCESS_CODE", "test-code")
    monkeypatch.setenv("VAULT_PATH", str(tmp_path))
    import importlib
    import app as app_module
    importlib.reload(app_module)
    return app_module


def test_right_token_serves_file(tmp_path, monkeypatch):
    m = _app(tmp_path, monkeypatch)
    (tmp_path / "Money").mkdir()
    (tmp_path / "Money" / "progress.json").write_text(json.dumps({"date": "2026-09-24", "streak": 2}))
    c = m.app.test_client()
    r = c.get(f"/money-progress/{m.money_progress_token()}/progress.json")
    assert r.status_code == 200
    assert r.get_json()["streak"] == 2
    assert r.headers["Access-Control-Allow-Origin"] == "*"


def test_wrong_token_is_a_bare_404(tmp_path, monkeypatch):
    m = _app(tmp_path, monkeypatch)
    r = m.app.test_client().get("/money-progress/nope/progress.json")
    assert r.status_code == 404
    assert "Access-Control-Allow-Origin" not in r.headers


def test_missing_file_is_503(tmp_path, monkeypatch):
    m = _app(tmp_path, monkeypatch)
    r = m.app.test_client().get(f"/money-progress/{m.money_progress_token()}/progress.json")
    assert r.status_code == 503


def test_options_preflight_needs_no_token(tmp_path, monkeypatch):
    m = _app(tmp_path, monkeypatch)
    r = m.app.test_client().options("/money-progress/anything/progress.json")
    assert r.status_code == 204
