"""
test_web_read.py — covers web_read.py, the shared "read any URL as text" path.

Everything here is OFFLINE: httpx and subprocess are stubbed, so the suite can
run on a plane and a network blip can never turn into a red build. The things
worth pinning are the ones that bit us:

  * the SSRF guard, which is the only thing between an untrusted page and
    169.254.169.254 (task_manager has its own copy; both must hold)
  * "thin page → retry through Jina", the whole reason this module exists
  * yt-dlp's --no-simulate, which cost an hour on 2026-09-14: with --print alone
    yt-dlp silently writes no caption file and every video reads "no captions"
  * the UNTRUSTED banner, which must survive on every success path

Run:  python3 test_web_read.py
"""

import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import web_read  # noqa: E402

_passed = 0
_failed = 0


def check(label, ok, detail=""):
    global _passed, _failed
    if ok:
        _passed += 1
        print(f"  ok   {label}")
    else:
        _failed += 1
        print(f"  FAIL {label}" + (f"\n       {detail}" if detail else ""))


class FakeResponse:
    def __init__(self, text="", status_code=200, headers=None):
        self.text = text
        self.status_code = status_code
        self.headers = headers or {}


def main():
    print("SSRF guard:")
    for url, why in [
        ("http://169.254.169.254/latest/meta-data/", "cloud metadata"),
        ("http://127.0.0.1:5001/api/version", "loopback"),
        ("http://localhost/admin", "localhost by name"),
        ("file:///etc/passwd", "non-http scheme"),
        ("ftp://example.com/x", "non-http scheme"),
        ("https://", "no host"),
    ]:
        check(f"refuses {why}: {url}", bool(web_read.ssrf_check(url)))
    check("allows a public host", web_read.ssrf_check("https://example.com") == "")

    print("\nYouTube detection:")
    for u in ["https://youtube.com/watch?v=x", "https://www.youtube.com/watch?v=x",
              "https://youtu.be/x", "https://m.youtube.com/watch?v=x"]:
        check(f"detects {u}", web_read.is_youtube(u))
    check("does not treat a lookalike host as YouTube",
          not web_read.is_youtube("https://notyoutube.com/watch?v=x"))
    check("does not treat a path mention as YouTube",
          not web_read.is_youtube("https://example.com/youtube.com/watch"))

    print("\npage reads (httpx stubbed):")
    saved_get = web_read.httpx.get
    try:
        fat = "<html><body>" + ("real article text. " * 200) + "</body></html>"
        web_read.httpx.get = lambda url, **kw: FakeResponse(fat)
        out = web_read.read_url("https://example.com/article")
        check("a page with real text is returned directly", "real article text" in out)
        check("direct read carries the UNTRUSTED banner", "UNTRUSTED WEB CONTENT" in out)
        check("direct read does NOT go through Jina", "Jina" not in out)

        # Thin page → Jina retry. Jina is reached through the same httpx.get, so
        # dispatch on the URL to simulate each side.
        thin = "<html><body><div id='root'></div></body></html>"
        def thin_then_jina(url, **kw):
            if url.startswith("https://r.jina.ai/"):
                return FakeResponse("Title: Rendered\n\n" + ("rendered body text. " * 100))
            return FakeResponse(thin)
        web_read.httpx.get = thin_then_jina
        out = web_read.read_url("https://example.com/spa")
        check("a thin page is retried through Jina Reader", "via Jina Reader" in out)
        check("the Jina result is what comes back", "rendered body text" in out)
        check("Jina read carries the UNTRUSTED banner", "UNTRUSTED WEB CONTENT" in out)

        # Jina down as well → we still return what little we have, and say so.
        def thin_then_dead(url, **kw):
            if url.startswith("https://r.jina.ai/"):
                raise RuntimeError("jina unreachable")
            return FakeResponse(thin)
        web_read.httpx.get = thin_then_dead
        out = web_read.read_url("https://example.com/spa")
        check("Jina failure degrades to a named note, not an exception",
              "rendered in-browser" in out and "UNTRUSTED" in out, out[:160])

        # A redirect to a private address must be refused mid-chain.
        def redirect_to_metadata(url, **kw):
            if "start" in url:
                return FakeResponse("", 302, {"location": "http://169.254.169.254/"})
            return FakeResponse("should never be reached")
        web_read.httpx.get = redirect_to_metadata
        out = web_read.read_url("https://example.com/start")
        check("a redirect into a private address is refused",
              "refused" in out and "169.254.169.254" in out, out[:160])

        web_read.httpx.get = lambda url, **kw: (_ for _ in ()).throw(RuntimeError("dns dead"))
        out = web_read.read_url("https://example.com/down")
        check("a total fetch failure reports the failure", "Fetch failed" in out, out[:120])
    finally:
        web_read.httpx.get = saved_get

    print("\nmax_chars:")
    try:
        # Filler is 'z' and the URL has no 'z' — 'example' contains an x, which
        # made the first version of this assertion fail by exactly one character.
        web_read.httpx.get = lambda url, **kw: FakeResponse("<p>" + ("z" * 5000) + "</p>")
        out = web_read.read_url("https://example.com/long", max_chars=100)
        check("max_chars caps the body", out.count("z") == 100, f"{out.count('z')} z's")
    finally:
        web_read.httpx.get = saved_get

    print("\nyt-dlp invocation:")
    src = open(os.path.join(HERE, "web_read.py"), encoding="utf-8").read()
    check("--no-simulate is passed (without it yt-dlp writes no caption file)",
          '"--no-simulate"' in src)
    check("--skip-download is passed (we never pull the video)", '"--skip-download"' in src)

    saved_which, saved_run = web_read.shutil.which, web_read.subprocess.run
    try:
        web_read.shutil.which = lambda name: None
        out = web_read.youtube_transcript("https://youtu.be/x")
        check("a node without yt-dlp says so plainly", "not installed on this node" in out,
              out[:120])

        web_read.shutil.which = lambda name: "/usr/local/bin/yt-dlp"
        captured = {}

        def fake_run(cmd, **kw):
            captured["cmd"] = cmd
            # yt-dlp writes the caption file next to the -o target.
            outdir = os.path.dirname(cmd[cmd.index("-o") + 1])
            with open(os.path.join(outdir, "cap.en.vtt"), "w", encoding="utf-8") as fh:
                fh.write("WEBVTT\nKind: captions\nLanguage: en\n\n"
                         "1\n00:00:01.000 --> 00:00:03.000\nhello there\n\n"
                         "2\n00:00:03.000 --> 00:00:05.000\nhello there\n\n"
                         "3\n00:00:05.000 --> 00:00:07.000\nsecond line<c> here</c>\n")
            return types.SimpleNamespace(stdout="A Title\nAn Uploader\n10:00", stderr="")

        web_read.subprocess.run = fake_run
        out = web_read.youtube_transcript("https://youtu.be/x")
        check("transcript includes the title line", "A Title" in out)
        check("transcript includes spoken text", "hello there" in out)
        check("repeated caption cues are de-duplicated", out.count("hello there") == 1,
              f"count={out.count('hello there')}")
        check("inline caption tags are stripped", "<c>" not in out)
        check("transcript carries the UNTRUSTED banner", "UNTRUSTED WEB CONTENT" in out)
        check("timestamps are not returned as text", "-->" not in out)

        def no_captions(cmd, **kw):
            return types.SimpleNamespace(stdout="A Title", stderr="no subtitles found")
        web_read.subprocess.run = no_captions
        out = web_read.youtube_transcript("https://youtu.be/x")
        check("a video with no captions says so", "No captions available" in out, out[:120])
    finally:
        web_read.shutil.which, web_read.subprocess.run = saved_which, saved_run

    print("\nurl normalisation:")
    check("empty url refused", "refused" in web_read.read_url(""))
    check("bare host gets https://", web_read.ssrf_check("https://example.com") == "")

    print(f"\n==== {_passed} passed, {_failed} failed ====")
    sys.exit(1 if _failed else 0)


if __name__ == "__main__":
    main()
