"""
Video Toolkit — Second Brain (v1)

ffmpeg-backed editing primitives for short-form video, plus a natural-language wrapper
the chat brain uses ("trim this to 30s and caption it with X").

Functions (all operate on files, return the output path):
  probe(path)                          -> {duration, width, height, has_audio, fps}
  trim(inp, start, duration|end)       -> trimmed clip
  concat([clips])                      -> clips joined (auto-normalized to a common size/fps)
  caption(inp, text, position, ...)    -> text burned in (rendered with Pillow + overlaid,
                                          because this ffmpeg build has no drawtext/libass)
  set_audio(inp, audio, mode)          -> replace or mix an audio track
  to_vertical(inp, mode)               -> 9:16 (1080x1920) for Shorts/Reels (crop or pad)
  thumbnail(inp, at)                   -> single-frame JPG/PNG

CLI (for testing each function):
  python3 video_toolkit.py probe inbox/clip.mp4
  python3 video_toolkit.py trim inbox/clip.mp4 --start 2 --duration 5
  python3 video_toolkit.py concat a.mp4 b.mp4 --out joined.mp4
  python3 video_toolkit.py caption inbox/clip.mp4 --text "Hello" --position bottom
  python3 video_toolkit.py vertical inbox/clip.mp4 --mode crop
  python3 video_toolkit.py thumbnail inbox/clip.mp4 --at 1.5
  python3 video_toolkit.py setaudio inbox/clip.mp4 --audio music.m4a --mode replace

AI video GENERATION (text-to-video) is intentionally NOT here — see video_gen_stub.py for
the V2 interface/where a key goes.
"""

import os
import re
import shutil
import subprocess

# brew paths on PATH even under a thin launch environment.
for _p in ("/opt/homebrew/bin", "/usr/local/bin"):
    if _p not in os.environ.get("PATH", "").split(os.pathsep):
        os.environ["PATH"] = os.environ.get("PATH", "") + os.pathsep + _p

_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
INBOX_DIR = os.path.join(_PROJECT_ROOT, "inbox")
OUT_DIR = os.path.join(_PROJECT_ROOT, "media_lib")
os.makedirs(OUT_DIR, exist_ok=True)

# Fonts to try for burned-in captions (macOS system fonts), first that exists wins.
_FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/Library/Fonts/Arial Bold.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/System/Library/Fonts/SFNS.ttf",
]


class ToolkitError(Exception):
    pass


def _bin(name: str) -> str:
    p = shutil.which(name)
    if not p:
        raise ToolkitError(f"`{name}` not found — install ffmpeg (`brew install ffmpeg`).")
    return p


def _run(cmd: list, timeout: int = 600):
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if res.returncode != 0:
        tail = (res.stderr or "").strip().splitlines()[-4:]
        raise ToolkitError("ffmpeg failed: " + " | ".join(tail))
    return res


def resolve_input(name_or_path: str) -> str:
    """Resolve a filename against inbox/, media_lib/, or the project; keep it in-project."""
    raw = (name_or_path or "").strip()
    if not raw:
        raise ToolkitError("No input file given.")
    cands = [raw] if os.path.isabs(raw) else [
        os.path.join(INBOX_DIR, raw), os.path.join(OUT_DIR, raw),
        os.path.join(_PROJECT_ROOT, raw), os.path.join(os.getcwd(), raw),
    ]
    path = next((os.path.realpath(c) for c in cands if os.path.isfile(c)), None)
    if not path:
        raise ToolkitError(f"Couldn't find '{raw}'. Put it in inbox/ and try again.")
    if os.path.commonpath([path, _PROJECT_ROOT]) != _PROJECT_ROOT:
        raise ToolkitError("For safety I only touch files inside the project.")
    return path


def _out_path(base_hint: str, suffix: str, ext: str = ".mp4") -> str:
    stem = re.sub(r"[^a-zA-Z0-9._-]+", "_", os.path.splitext(os.path.basename(base_hint))[0])[:40]
    path = os.path.join(OUT_DIR, f"{stem}_{suffix}{ext}")
    n = 1
    root, e = os.path.splitext(path)
    while os.path.exists(path):
        path = f"{root}_{n}{e}"
        n += 1
    return path


# ============================================================
# PROBE
# ============================================================
def probe(path: str) -> dict:
    path = resolve_input(path) if not os.path.isabs(path) else path
    import json
    res = subprocess.run(
        [_bin("ffprobe"), "-v", "error", "-print_format", "json",
         "-show_format", "-show_streams", path],
        capture_output=True, text=True, timeout=60,
    )
    if res.returncode != 0:
        raise ToolkitError("Couldn't read that file as video.")
    data = json.loads(res.stdout)
    v = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), {})
    has_audio = any(s.get("codec_type") == "audio" for s in data.get("streams", []))
    fps = 30.0
    if v.get("r_frame_rate") and "/" in v["r_frame_rate"]:
        a, b = v["r_frame_rate"].split("/")
        fps = float(a) / float(b) if float(b) else 30.0
    try:
        duration = float(data.get("format", {}).get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    return {"duration": duration, "width": int(v.get("width") or 0),
            "height": int(v.get("height") or 0), "has_audio": has_audio, "fps": round(fps, 2)}


# ============================================================
# TRIM
# ============================================================
def trim(inp: str, start: float = 0.0, duration: float = None, end: float = None,
         out: str = None) -> str:
    src = resolve_input(inp)
    out = out or _out_path(src, "trim")
    cmd = [_bin("ffmpeg"), "-y", "-ss", str(max(0.0, float(start))), "-i", src]
    if duration is not None:
        cmd += ["-t", str(float(duration))]
    elif end is not None:
        cmd += ["-to", str(max(0.0, float(end) - float(start)))]
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-c:a", "aac", "-movflags", "+faststart", out]
    _run(cmd)
    return out


# ============================================================
# CONCAT (normalize then join — robust across mixed inputs)
# ============================================================
def concat(inputs: list, out: str = None, target_w: int = None, target_h: int = None,
           fps: int = 30) -> str:
    if not inputs or len(inputs) < 2:
        raise ToolkitError("Need at least two clips to concatenate.")
    srcs = [resolve_input(i) for i in inputs]
    # target size = first clip's size unless overridden
    info0 = probe(srcs[0])
    tw = target_w or info0["width"] or 1280
    th = target_h or info0["height"] or 720
    # even dimensions required by libx264
    tw -= tw % 2
    th -= th % 2
    out = out or _out_path(srcs[0], "concat")

    cmd = [_bin("ffmpeg"), "-y"]
    for s in srcs:
        cmd += ["-i", s]
    parts, labels = [], ""
    for idx in range(len(srcs)):
        # scale to fit, pad to exact size, normalize SAR/fps; ensure an audio stream exists.
        parts.append(
            f"[{idx}:v]scale={tw}:{th}:force_original_aspect_ratio=decrease,"
            f"pad={tw}:{th}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps},format=yuv420p[v{idx}];"
            f"[{idx}:a]aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo[a{idx}]"
        )
        labels += f"[v{idx}][a{idx}]"
    filtergraph = ";".join(parts) + f";{labels}concat=n={len(srcs)}:v=1:a=1[v][a]"
    # If a source lacks audio, synthesize silence so the concat's audio pads line up.
    cmd_pre = [_bin("ffmpeg"), "-y"]
    inputs_have_audio = all(probe(s)["has_audio"] for s in srcs)
    if not inputs_have_audio:
        # add a silent audio source per input via anullsrc mapped in the graph instead
        # (simplest robust path: give every input a silent track by pre-adding anullsrc)
        return _concat_with_silence(srcs, tw, th, fps, out)

    cmd += ["-filter_complex", filtergraph, "-map", "[v]", "-map", "[a]",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-c:a", "aac",
            "-movflags", "+faststart", out]
    _run(cmd)
    return out


def _concat_with_silence(srcs, tw, th, fps, out):
    """Concat where some inputs have no audio: give each input a silent stereo track."""
    cmd = [_bin("ffmpeg"), "-y"]
    for s in srcs:
        cmd += ["-i", s]
    n = len(srcs)
    # one shared anullsrc input we can reuse per clip via atrim isn't reliable; instead
    # generate silence per clip with anullsrc as extra lavfi inputs.
    for _ in range(n):
        cmd += ["-f", "lavfi", "-t", "3600", "-i", "anullsrc=r=44100:cl=stereo"]
    parts, labels = [], ""
    for idx in range(n):
        info = probe(srcs[idx])
        has_a = info["has_audio"]
        adur = info["duration"] or 1.0
        parts.append(
            f"[{idx}:v]scale={tw}:{th}:force_original_aspect_ratio=decrease,"
            f"pad={tw}:{th}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps},format=yuv420p[v{idx}]"
        )
        if has_a:
            parts.append(f"[{idx}:a]aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo[a{idx}]")
        else:
            sil_idx = n + idx
            parts.append(f"[{sil_idx}:a]atrim=0:{adur:.3f},aresample=44100,"
                         f"aformat=sample_fmts=fltp:channel_layouts=stereo[a{idx}]")
        labels += f"[v{idx}][a{idx}]"
    filtergraph = ";".join(parts) + f";{labels}concat=n={n}:v=1:a=1[v][a]"
    cmd += ["-filter_complex", filtergraph, "-map", "[v]", "-map", "[a]",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-c:a", "aac",
            "-shortest", "-movflags", "+faststart", out]
    _run(cmd)
    return out


# ============================================================
# CAPTION (Pillow-rendered PNG overlay — no drawtext needed)
# ============================================================
def _font_path() -> str:
    return next((f for f in _FONT_CANDIDATES if os.path.isfile(f)), None)


def _render_caption_png(text: str, video_w: int, png_path: str,
                        font_frac: float = 0.075, pad_frac: float = 0.04):
    from PIL import Image, ImageDraw, ImageFont
    font_size = max(18, int(video_w * font_frac))
    fp = _font_path()
    try:
        font = ImageFont.truetype(fp, font_size) if fp else ImageFont.load_default()
    except Exception:
        font = ImageFont.load_default()

    pad = int(video_w * pad_frac)
    max_text_w = video_w - 2 * pad
    # word-wrap to fit width
    tmp = Image.new("RGBA", (10, 10))
    draw = ImageDraw.Draw(tmp)

    def text_w(s):
        return draw.textbbox((0, 0), s, font=font)[2]

    words, lines, cur = text.split(), [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if text_w(trial) <= max_text_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)

    line_h = draw.textbbox((0, 0), "Ag", font=font)[3] + int(font_size * 0.25)
    img_h = line_h * len(lines) + 2 * pad
    img = Image.new("RGBA", (video_w, img_h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    stroke = max(2, font_size // 12)
    y = pad
    for ln in lines:
        w = text_w(ln)
        x = (video_w - w) // 2
        d.text((x, y), ln, font=font, fill=(255, 255, 255, 255),
               stroke_width=stroke, stroke_fill=(0, 0, 0, 230))
        y += line_h
    img.save(png_path)
    return img_h


def caption(inp: str, text: str, position: str = "bottom", out: str = None,
            start: float = None, end: float = None) -> str:
    src = resolve_input(inp)
    info = probe(src)
    vw, vh = info["width"] or 1280, info["height"] or 720
    out = out or _out_path(src, "caption")

    png = _out_path(src, "cap", ".png")
    cap_h = _render_caption_png(text, vw, png)

    margin = int(vh * 0.06)
    if position == "top":
        y = margin
    elif position in ("center", "middle"):
        y = f"(H-{cap_h})/2"
    else:  # bottom
        y = vh - cap_h - margin
    overlay = f"[0:v][1:v]overlay=0:{y}"
    if start is not None or end is not None:
        s = start if start is not None else 0
        e = end if end is not None else info["duration"]
        overlay += f":enable='between(t,{s},{e})'"
    overlay += "[v]"

    cmd = [_bin("ffmpeg"), "-y", "-i", src, "-i", png,
           "-filter_complex", overlay, "-map", "[v]"]
    if info["has_audio"]:
        cmd += ["-map", "0:a", "-c:a", "copy"]
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-movflags", "+faststart", out]
    _run(cmd)
    try:
        os.remove(png)
    except OSError:
        pass
    return out


# ============================================================
# AUDIO (replace / mix)
# ============================================================
def set_audio(inp: str, audio: str, mode: str = "replace", out: str = None) -> str:
    src = resolve_input(inp)
    aud = resolve_input(audio)
    out = out or _out_path(src, "audio")
    if mode == "add" and probe(src)["has_audio"]:
        # mix new audio with existing (duck nothing fancy — equal mix), cut to video length
        cmd = [_bin("ffmpeg"), "-y", "-i", src, "-i", aud,
               "-filter_complex", "[0:a][1:a]amix=inputs=2:duration=first:dropout_transition=2[a]",
               "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac",
               "-movflags", "+faststart", out]
    else:  # replace
        cmd = [_bin("ffmpeg"), "-y", "-i", src, "-i", aud,
               "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac",
               "-shortest", "-movflags", "+faststart", out]
    _run(cmd)
    return out


# ============================================================
# 9:16 VERTICAL (Shorts / Reels)
# ============================================================
def to_vertical(inp: str, mode: str = "crop", out: str = None,
                width: int = 1080, height: int = 1920) -> str:
    src = resolve_input(inp)
    out = out or _out_path(src, "9x16")
    if mode == "pad":
        vf = (f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
              f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1")
    else:  # crop — fill the frame, center-crop the overflow
        vf = (f"scale={width}:{height}:force_original_aspect_ratio=increase,"
              f"crop={width}:{height},setsar=1")
    cmd = [_bin("ffmpeg"), "-y", "-i", src, "-vf", vf,
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "20"]
    if probe(src)["has_audio"]:
        cmd += ["-c:a", "aac"]
    cmd += ["-movflags", "+faststart", out]
    _run(cmd)
    return out


# ============================================================
# THUMBNAIL
# ============================================================
def thumbnail(inp: str, at: float = None, out: str = None) -> str:
    src = resolve_input(inp)
    info = probe(src)
    if at is None:
        at = (info["duration"] or 2) / 3.0  # a third of the way in — usually representative
    out = out or _out_path(src, "thumb", ".jpg")
    cmd = [_bin("ffmpeg"), "-y", "-ss", str(at), "-i", src, "-frames:v", "1",
           "-q:v", "2", out]
    _run(cmd)
    return out


# ============================================================
# NATURAL-LANGUAGE WRAPPER (used by the chat brain's edit_video tool)
# ============================================================
OPERATIONS = ("trim", "caption", "concat", "set_audio", "vertical", "thumbnail", "probe")


def run_operation(operation: str, **params) -> str:
    """Dispatch a single editing operation and return a human-readable result string
    (including the output filename so the chat can chain or report it). Output lands in
    media_lib/. Raises ToolkitError with a clean message on bad input."""
    op = (operation or "").strip().lower()
    if op not in OPERATIONS:
        raise ToolkitError(f"Unknown operation '{operation}'. Options: {', '.join(OPERATIONS)}.")

    if op == "probe":
        info = probe(params["filename"])
        return (f"{params['filename']}: {info['duration']:.1f}s, {info['width']}x{info['height']}, "
                f"{info['fps']}fps, audio={'yes' if info['has_audio'] else 'no'}.")

    if op == "trim":
        out = trim(params["filename"], start=params.get("start", 0),
                   duration=params.get("duration"), end=params.get("end"))
    elif op == "caption":
        if not params.get("text"):
            raise ToolkitError("caption needs `text`.")
        out = caption(params["filename"], params["text"],
                      position=params.get("position", "bottom"),
                      start=params.get("start"), end=params.get("end"))
    elif op == "concat":
        clips = params.get("filenames") or params.get("clips")
        if not clips or len(clips) < 2:
            raise ToolkitError("concat needs `filenames`: a list of at least two clips.")
        out = concat(clips)
    elif op == "set_audio":
        if not params.get("audio"):
            raise ToolkitError("set_audio needs `audio` (the audio filename).")
        out = set_audio(params["filename"], params["audio"], mode=params.get("mode", "replace"))
    elif op == "vertical":
        out = to_vertical(params["filename"], mode=params.get("mode", "crop"))
    elif op == "thumbnail":
        out = thumbnail(params["filename"], at=params.get("at"))

    rel = os.path.relpath(out, _PROJECT_ROOT)
    info = ""
    if out.endswith((".mp4", ".mov", ".webm", ".mkv")):
        try:
            p = probe(out)
            info = f" ({p['duration']:.1f}s, {p['width']}x{p['height']})"
        except Exception:
            pass
    return (f"Done: {op}. Saved to {rel}{info}. "
            f"The output filename is '{os.path.basename(out)}' (in media_lib/) — "
            f"use it as the input if you need to apply another edit.")


# ============================================================
# CLI
# ============================================================
def _cli():
    import argparse
    import json
    p = argparse.ArgumentParser(description="ffmpeg-backed video toolkit.")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("probe"); sp.add_argument("input")
    sp = sub.add_parser("trim"); sp.add_argument("input")
    sp.add_argument("--start", type=float, default=0); sp.add_argument("--duration", type=float)
    sp.add_argument("--end", type=float); sp.add_argument("--out")
    sp = sub.add_parser("concat"); sp.add_argument("inputs", nargs="+"); sp.add_argument("--out")
    sp = sub.add_parser("caption"); sp.add_argument("input"); sp.add_argument("--text", required=True)
    sp.add_argument("--position", default="bottom"); sp.add_argument("--start", type=float)
    sp.add_argument("--end", type=float); sp.add_argument("--out")
    sp = sub.add_parser("setaudio"); sp.add_argument("input"); sp.add_argument("--audio", required=True)
    sp.add_argument("--mode", default="replace", choices=["replace", "add"]); sp.add_argument("--out")
    sp = sub.add_parser("vertical"); sp.add_argument("input")
    sp.add_argument("--mode", default="crop", choices=["crop", "pad"]); sp.add_argument("--out")
    sp = sub.add_parser("thumbnail"); sp.add_argument("input"); sp.add_argument("--at", type=float)
    sp.add_argument("--out")

    a = p.parse_args()
    if a.cmd == "probe":
        print(json.dumps(probe(a.input), indent=2))
    elif a.cmd == "trim":
        print(trim(a.input, start=a.start, duration=a.duration, end=a.end, out=a.out))
    elif a.cmd == "concat":
        print(concat(a.inputs, out=a.out))
    elif a.cmd == "caption":
        print(caption(a.input, a.text, position=a.position, start=a.start, end=a.end, out=a.out))
    elif a.cmd == "setaudio":
        print(set_audio(a.input, a.audio, mode=a.mode, out=a.out))
    elif a.cmd == "vertical":
        print(to_vertical(a.input, mode=a.mode, out=a.out))
    elif a.cmd == "thumbnail":
        print(thumbnail(a.input, at=a.at, out=a.out))


if __name__ == "__main__":
    _cli()
