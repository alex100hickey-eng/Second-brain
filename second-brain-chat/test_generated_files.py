"""
Tests for generated_files.py — removing/restoring the files CLARVIS generated.

Run directly:  python3 test_generated_files.py
No network, no Supabase, no real synthesized//vault_inbox: the module's folder
map is repointed at a temp dir, so nothing in the repo is touched. Same harness
style as test_mail_and_escalation.py.

Covers:
  1. list_generated_files — both folders, single folder, bad folder, README hidden
  2. remove_generated_file — the happy path is a MOVE to trash, never a delete
  3. The containment guarantees: traversal, absolute paths, wildcards, hidden
     files, symlinks, protected README, wrong extension, unknown folder — every
     one refused, and the file it aimed at still on disk
  4. restore_generated_file — round-trip, won't clobber, unknown name
  5. The on_remove/on_restore hooks fire for vault_inbox (draft_store mirror)
  6. A missing file reports what IS there instead of failing silently
  7. app.py wires all three tools into TOOLS + handle_tool_call routing
"""

import ast
import os
import shutil
import subprocess
import sys
import tempfile

import generated_files as gf

_passed = 0
_failed = 0


def check(name, cond, detail=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  PASS  {name}")
    else:
        _failed += 1
        print(f"  FAIL  {name}  {detail}")


def setup_sandbox(root):
    """Repoint the module at a temp project root and lay down fixtures."""
    synth = os.path.join(root, "synthesized")
    inbox = os.path.join(root, "vault_inbox")
    os.makedirs(synth)
    os.makedirs(inbox)
    gf.FOLDERS["synthesized"] = synth
    gf.FOLDERS["vault_inbox"] = inbox
    gf.TRASH_DIR = os.path.join(root, ".generated_trash")
    gf._PROJECT_ROOT = root

    with open(os.path.join(synth, "20260801-seed-oils.md"), "w") as f:
        f.write("# Seed oils, sweep 1\n\nBody text.\n")
    with open(os.path.join(synth, "20260801-seed-oils-1.md"), "w") as f:
        f.write("# Seed oils, sweep 2\n\nBody text.\n")
    with open(os.path.join(inbox, "README.md"), "w") as f:
        f.write("# vault_inbox\n")
    with open(os.path.join(inbox, "2026-08-01-creatine.md"), "w") as f:
        f.write("---\ntitle: Creatine\n---\n")
    # A secret living OUTSIDE both folders — nothing may reach it.
    with open(os.path.join(root, "secret.md"), "w") as f:
        f.write("do not touch\n")
    return synth, inbox


def _make_git_repo(root):
    """Commit the fixture synthesized/ into a throwaway local repo. Returns False
    if git isn't usable here, so the suite skips rather than fails."""
    env = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_SYSTEM="/dev/null")
    cmds = [
        ["git", "init", "-q"],
        ["git", "add", "synthesized/20260801-seed-oils-1.md"],
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "fixture"],
    ]
    for cmd in cmds:
        try:
            if subprocess.run(cmd, cwd=root, capture_output=True, env=env, timeout=30).returncode:
                return False
        except Exception:
            return False
    return True


def main():
    root = tempfile.mkdtemp(prefix="genfiles-test-")
    try:
        synth, inbox = setup_sandbox(root)

        # --- listing --------------------------------------------------------
        print("\nlist_generated_files:")
        out = gf.list_generated_files()
        check("lists both folders", "synthesized/" in out and "vault_inbox/" in out, out)
        check("shows exact filenames", "20260801-seed-oils.md" in out, out)
        check("pulls the H1 as a title", "Seed oils, sweep 1" in out, out)
        check("hides README", "README.md" not in out, out)
        check("single folder only", "vault_inbox/" not in gf.list_generated_files("synthesized"))
        check("unknown folder refused", "isn't one of them" in gf.list_generated_files("/etc"))

        # --- containment ----------------------------------------------------
        print("\ncontainment (every one must be refused):")
        os.symlink(os.path.join(root, "secret.md"), os.path.join(synth, "link.md"))
        for label, folder, name in [
            ("parent traversal", "synthesized", "../secret.md"),
            ("deep traversal", "synthesized", "../../etc/passwd"),
            ("absolute path", "synthesized", "/etc/passwd"),
            ("subdir path", "synthesized", "sub/thing.md"),
            ("wildcard", "synthesized", "*.md"),
            ("bracket glob", "synthesized", "2026080[12]-seed-oils.md"),
            ("hidden file", "synthesized", ".env"),
            ("symlink out", "synthesized", "link.md"),
            ("protected README", "vault_inbox", "README.md"),
            ("wrong extension", "synthesized", "app.py"),
            ("unknown folder", "second-brain-chat", "app.py"),
            ("empty name", "synthesized", ""),
        ]:
            res = gf.remove_generated_file(folder, name)
            ok = not res.startswith("Removed")
            check(f"refuses {label}", ok, res[:100])
        check("secret.md untouched", os.path.isfile(os.path.join(root, "secret.md")))
        check("symlink still there (not followed)", os.path.islink(os.path.join(synth, "link.md")))
        os.remove(os.path.join(synth, "link.md"))

        # --- missing file ---------------------------------------------------
        print("\nmissing file:")
        res = gf.remove_generated_file("synthesized", "nope.md")
        check("says it's missing", "no 'nope.md'" in res.replace('"', "'"), res[:120])
        check("shows what IS there", "20260801-seed-oils.md" in res, res[:200])

        # --- remove = move to trash ----------------------------------------
        print("\nremove_generated_file:")
        target = os.path.join(synth, "20260801-seed-oils.md")
        res = gf.remove_generated_file("synthesized", "20260801-seed-oils.md")
        check("reports removal", res.startswith("Removed"), res[:120])
        check("gone from synthesized/", not os.path.exists(target))
        trashed = gf._trashed("synthesized")
        check("exactly one item in trash", len(trashed) == 1, str(trashed))
        check("trash keeps the original name", trashed[0][1] == "20260801-seed-oils.md", str(trashed))
        trash_path = os.path.join(gf.TRASH_DIR, "synthesized", trashed[0][0])
        check("content survived the move (never deleted)",
              open(trash_path).read().startswith("# Seed oils, sweep 1"))
        check("says it's restorable", "restore" in res.lower(), res)
        check("mentions the Agent Outputs log", "Agent Outputs" in res, res)
        check("listing now shows it as restorable",
              "recently removed" in gf.list_generated_files("synthesized"))
        check("second report still there", os.path.isfile(os.path.join(synth, "20260801-seed-oils-1.md")))

        # --- restore --------------------------------------------------------
        print("\nrestore_generated_file:")
        res = gf.restore_generated_file("synthesized", "20260801-seed-oils.md")
        check("reports restore", res.startswith("Restored"), res[:120])
        check("back on disk", os.path.isfile(target))
        check("content intact", open(target).read().startswith("# Seed oils, sweep 1"))
        check("trash emptied of it", not gf._trashed("synthesized"))
        check("unknown name handled",
              "Nothing named" in gf.restore_generated_file("synthesized", "ghost.md"))

        # won't clobber an existing file
        gf.remove_generated_file("synthesized", "20260801-seed-oils.md")
        with open(target, "w") as f:
            f.write("# A newer report with the same name\n")
        res = gf.restore_generated_file("synthesized", "20260801-seed-oils.md")
        check("won't overwrite an existing file", "already exists" in res, res[:120])
        check("newer file untouched", "newer report" in open(target).read())

        # --- hooks (draft_store mirror) -------------------------------------
        print("\non_remove / on_restore hooks:")
        calls = []
        gf.on_remove = lambda folder, filename: (calls.append(("remove", folder, filename))
                                                 or "Its backup copy is gone too.")
        gf.on_restore = lambda folder, filename, content: (calls.append(("restore", folder, filename, content))
                                                           or "")
        res = gf.remove_generated_file("vault_inbox", "2026-08-01-creatine.md")
        check("on_remove fired with folder+filename",
              calls[0] == ("remove", "vault_inbox", "2026-08-01-creatine.md"), str(calls))
        check("hook note reaches the reply", "backup copy is gone" in res, res)
        gf.restore_generated_file("vault_inbox", "2026-08-01-creatine.md")
        check("on_restore fired with the file's content",
              calls[1][0] == "restore" and "title: Creatine" in calls[1][3], str(calls[1])[:120])

        # a hook that explodes must not lose the removal
        gf.on_remove = lambda folder, filename: 1 / 0
        res = gf.remove_generated_file("vault_inbox", "2026-08-01-creatine.md")
        check("broken hook doesn't break removal", res.startswith("Removed"), res[:120])
        check("file really moved despite the hook",
              not os.path.exists(os.path.join(inbox, "2026-08-01-creatine.md")))
        gf.on_remove = gf.on_restore = None

        # --- git-tracked warning --------------------------------------------
        # A committed report comes back on the next deploy, so removal has to say
        # so rather than imply the file is gone. Needs a real (tiny, local) repo.
        print("\ngit-tracked report warning:")
        if _make_git_repo(root):
            res = gf.remove_generated_file("synthesized", "20260801-seed-oils-1.md")
            check("warns that a committed report returns on redeploy",
                  "redeploy" in res and "git" in res, res[:200])
            gf.restore_generated_file("synthesized", "20260801-seed-oils-1.md")
            with open(os.path.join(synth, "untracked.md"), "w") as f:
                f.write("# Not committed\n")
            res = gf.remove_generated_file("synthesized", "untracked.md")
            check("no false warning for an untracked report", "redeploy" not in res, res[:200])
        else:
            print("  SKIP  no usable git binary")

        # --- app.py wiring --------------------------------------------------
        print("\napp.py wiring:")
        src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.py")).read()
        tree = ast.parse(src)
        check("imports generated_files",
              any(isinstance(n, ast.Import) and any(a.name == "generated_files" for a in n.names)
                  for n in ast.walk(tree)))
        for tool in ("list_generated_files", "remove_generated_file", "restore_generated_file"):
            check(f"{tool} routed in handle_tool_call", f'"{tool}"' in src, "")
        check("schemas + labels registered",
              "TOOLS.extend(generated_files.TOOL_SCHEMAS)" in src
              and "TOOL_STATUS_LABELS.update(generated_files.TOOL_STATUS_LABELS)" in src)
        check("mirror hooks wired", "generated_files.on_remove" in src
              and "generated_files.on_restore" in src)
        check("every schema has a status label",
              all(s["name"] in gf.TOOL_STATUS_LABELS for s in gf.TOOL_SCHEMAS))
        check("handler covers every schema",
              all(gf.handle_tool_call(s["name"], {}) != "Unknown generated_files tool."
                  for s in gf.TOOL_SCHEMAS))

        print(f"\n==== {_passed} passed, {_failed} failed ====")
        sys.exit(1 if _failed else 0)
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
