"""vault_sync.sh with iCloud-evicted files that will not come back.

On 2026-09-24 seven vault files stayed dataless (they read empty; git: "short read while
indexing") and the script skipped the WHOLE run every 10 minutes from 12:19 on. Nothing
written after that reached git, and the send gate, reply watch and the 07:30 static-first
backstop, which fall back to the vault's git copy, read one twelve hours old.

A real dataless file can't be made in a test, so the script's own test hooks stand in:
VAULT_SYNC_FAKE_DATALESS lists the "evicted" files, and chmod 000 makes them unreadable the
way an evicted one is. Any read of them by git fails the run.

chmod 000 does not reproduce one part: the short read git's index refresh hits on a tracked
evicted file even when it is excluded from `git add`. That is why the script also marks those
files assume-unchanged for the run. Checked on the real vault 2026-09-25 with its seven stuck
files: `git status` gave 3 short-read errors without the mark and 0 with it.
"""
import os
import stat
import subprocess

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(os.path.dirname(HERE), "scripts", "vault_sync.sh")

GIT_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
}


def git(cwd, *args):
    env = {**os.environ, **GIT_ENV}
    return subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True, check=True).stdout


@pytest.fixture
def vault(tmp_path):
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(remote))
    v = tmp_path / "vault"
    git(tmp_path, "clone", "-q", str(remote), str(v))
    git(v, "checkout", "-q", "-b", "main")
    (v / "a.md").write_text("one\n")
    (v / "evicted.md").write_text("safe in the cloud\n")
    git(v, "add", "-A")
    git(v, "commit", "-q", "-m", "seed")
    git(v, "push", "-q", "-u", "origin", "main")
    locked = []
    yield v, remote, locked
    for p in locked:                                   # let tmp cleanup delete them
        os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)


def evict(locked, path):
    os.chmod(path, 0)
    locked.append(path)


def run_sync(v, dataless=""):
    env = {**os.environ, **GIT_ENV,
           "VAULT_SYNC_PATH": str(v), "VAULT_SYNC_PY": "/usr/bin/true",
           "VAULT_SYNC_BRCTL": "/usr/bin/true", "VAULT_SYNC_WAIT_TRIES": "1",
           "VAULT_SYNC_FAKE_DATALESS": dataless}
    return subprocess.run(["/bin/bash", SCRIPT], env=env, capture_output=True, text=True, timeout=60)


def remote_file(remote, name):
    return subprocess.run(["git", "--git-dir", str(remote), "show", f"main:{name}"],
                          capture_output=True, text=True).stdout


def test_evicted_tracked_file_is_left_out_and_the_rest_syncs(vault):
    v, remote, locked = vault
    (v / "a.md").write_text("two\n")
    evict(locked, v / "evicted.md")
    r = run_sync(v, "./evicted.md")
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    assert "PARTIAL" in out and "SYNCED" in out, out
    assert remote_file(remote, "a.md") == "two\n"
    assert remote_file(remote, "evicted.md") == "safe in the cloud\n"


def test_the_assume_unchanged_mark_is_cleared_after_the_run(vault):
    v, remote, locked = vault
    (v / "a.md").write_text("two\n")
    evict(locked, v / "evicted.md")
    assert run_sync(v, "./evicted.md").returncode == 0
    # lower-case "h" = assume-unchanged. Left set, a later real edit would never sync.
    assert git(v, "ls-files", "-v", "evicted.md").startswith("H ")
    os.chmod(v / "evicted.md", stat.S_IRUSR | stat.S_IWUSR)
    (v / "evicted.md").write_text("edited after it came back\n")
    r = run_sync(v)
    assert r.returncode == 0, r.stdout + r.stderr
    assert remote_file(remote, "evicted.md") == "edited after it came back\n"


def test_evicted_untracked_file_is_not_added(vault):
    v, remote, locked = vault
    (v / "a.md").write_text("three\n")
    (v / "new.md").write_text("not yet anywhere\n")
    evict(locked, v / "new.md")
    r = run_sync(v, "./new.md")
    assert r.returncode == 0, r.stdout + r.stderr
    assert remote_file(remote, "a.md") == "three\n"
    assert "new.md" not in git(v, "ls-files")


def test_an_evicted_git_pointer_still_stops_the_run(vault):
    v, remote, locked = vault
    (v / "a.md").write_text("four\n")
    r = run_sync(v, "./.git")
    assert r.returncode == 1
    assert ".git pointer" in r.stdout
    assert remote_file(remote, "a.md") == "one\n"


def test_nothing_evicted_syncs_as_before(vault):
    v, remote, _ = vault
    (v / "a.md").write_text("five\n")
    r = run_sync(v)
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    assert "SYNCED" in out and "PARTIAL" not in out
    assert remote_file(remote, "a.md") == "five\n"
