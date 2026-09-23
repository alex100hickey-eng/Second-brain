"""ffmpeg transforms: voice hook + on-screen text hook + per-platform trim/zoom.

Output is always 1080x1920 H.264/AAC with faststart. The voice hook is mixed OVER the clip's own
audio (ducked to 20% while the hook plays), so the clip's first seconds are Alex's voice, then the
source. The text hook is a PNG card rendered with Pillow and composited with `overlay` — this
Homebrew ffmpeg has no `drawtext`. `build_cmd` is pure (testable); `make_variant` runs it.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import textwrap

from . import config

FFMPEG = os.environ.get("FFMPEG", "ffmpeg")
FFPROBE = os.environ.get("FFPROBE", "ffprobe")


def have_ffmpeg() -> bool:
    try:
        subprocess.run([FFMPEG, "-version"], capture_output=True, check=True)
        return True
    except Exception:
        return False


def probe_duration(path: str) -> float:
    out = subprocess.run([FFPROBE, "-v", "error", "-show_entries", "format=duration", "-of", "json", path],
                         capture_output=True, text=True, check=True).stdout
    return float(json.loads(out)["format"]["duration"])


def wrap_text(text: str, width: int = 18, max_lines: int = 3) -> str:
    text = " ".join((text or "").split())
    lines = textwrap.wrap(text, width=width) or [""]
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1][: max(0, width - 1)] + "…"
    return "\n".join(lines)


_EMOJI = re.compile("[\U0001F000-\U0001FFFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u200d]+")


def plain_text(text: str) -> str:
    """The on-screen card font has no emoji: drop them there, keep them in the caption."""
    return re.sub(r"\s{2,}", " ", _EMOJI.sub("", text or "")).strip()


FIT_STEPS = ((62, 18), (54, 21), (48, 24), (42, 27))   # (font px, chars per line): shrink before truncating


def fit_text(text: str, max_lines: int = 3) -> tuple:
    """(font size, wrapped text). A brief's 60-character line shrinks to fit instead of ending in '…'."""
    clean = " ".join((text or "").split())
    for size, width in FIT_STEPS:
        lines = textwrap.wrap(clean, width=width) or [""]
        if len(lines) <= max_lines:
            return size, "\n".join(lines)
    size, width = FIT_STEPS[-1]
    return size, wrap_text(clean, width=width, max_lines=max_lines + 1)


def render_text_png(text: str, path: str, max_width: int = 1000, font_path: str = config.FONT, size: int = 0) -> tuple:
    """White bold text with a black stroke on a rounded translucent box. Returns (w, h)."""
    from PIL import Image, ImageDraw, ImageFont  # optional dependency, imported lazily

    if size:
        wrapped = wrap_text(text)
    else:
        size, wrapped = fit_text(text)
    try:
        font = ImageFont.truetype(font_path, size)
    except OSError:
        font = ImageFont.load_default(size=size)
    lines = wrapped.split("\n")
    pad, gap, stroke = 28, 10, 4
    meas = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    boxes = [meas.textbbox((0, 0), ln, font=font, stroke_width=stroke) for ln in lines]
    widths = [b[2] - b[0] for b in boxes]
    line_h = max(b[3] - b[1] for b in boxes) + gap
    w = int(min(max_width, max(widths) + 2 * pad))
    h = int(line_h * len(lines) + 2 * pad)
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, w - 1, h - 1), radius=24, fill=(0, 0, 0, 115))
    y = pad
    for ln, lw, b in zip(lines, widths, boxes):
        d.text(((w - lw) / 2 - b[0], y - b[1]), ln, font=font, fill="white", stroke_width=stroke, stroke_fill="black")
        y += line_h
    img.save(path)
    return w, h


def build_cmd(src: str, dst: str, text_png: str | None = None, text_pos: str = "top", trim_start: float = 0.0,
              trim_end: float = 0.0, zoom: float = 1.0, src_duration: float | None = None,
              hook_audio: str | None = None, hook_len: float = 0.0, text_seconds: float = 2.8,
              badge_png: str | None = None, logo_png: str | None = None) -> list:
    out_dur = max(3.0, src_duration - trim_start - trim_end) if src_duration else None
    cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{trim_start:.2f}", "-i", src]
    idx = 1
    hook_idx = png_idx = None
    if hook_audio and hook_len > 0:
        cmd += ["-i", hook_audio]
        hook_idx, idx = idx, idx + 1
    if text_png:
        cmd += ["-i", text_png]
        png_idx, idx = idx, idx + 1
    badge_idx = logo_idx = None
    if badge_png:
        cmd += ["-i", badge_png]
        badge_idx, idx = idx, idx + 1
    if logo_png:
        cmd += ["-i", logo_png]
        logo_idx, idx = idx, idx + 1
    vf = ["scale=1080:1920:force_original_aspect_ratio=increase", "crop=1080:1920"]
    if zoom and zoom != 1.0:
        vf += [f"scale=trunc(iw*{zoom}/2)*2:trunc(ih*{zoom}/2)*2", "crop=1080:1920"]
    chain = f"[0:v]{','.join(vf)}[v0]"
    stage = "v0"
    if png_idx is not None:
        y = "H*0.12" if text_pos == "top" else "(H-h)/2"
        chain += (f";[{stage}][{png_idx}:v]overlay=(W-w)/2:{y}:"
                  f"enable='between(t,0,{text_seconds:.2f})'[v1]")
        stage = "v1"
    if badge_idx is not None:
        # Low and always on. It has to survive the whole clip because the brief asks for the text
        # to BE on screen, not to have flashed past in the first two seconds.
        chain += f";[{stage}][{badge_idx}:v]overlay=(W-w)/2:H*0.86[v2]"
        stage = "v2"
    if logo_idx is not None:
        # A brand watermark a brief REQUIRES (Crazy Taxi: "Add the official logo as a watermark").
        # Top-right and always on, clear of the centred hook card and of the caption safe area
        # TikTok puts over the bottom-right of the frame.
        chain += f";[{stage}][{logo_idx}:v]overlay=W-w-40:60[v3]"
        stage = "v3"
    chain += f";[{stage}]format=yuv420p[v]"
    if hook_idx is not None:
        chain += (f";[0:a]volume=enable='between(t,0,{hook_len:.2f})':volume=0.2[a0];"
                  f"[{hook_idx}:a]aformat=sample_rates=48000:channel_layouts=stereo,atrim=0:{hook_len:.2f},apad=pad_dur=0.1[a1];"
                  f"[a0][a1]amix=inputs=2:duration=first:normalize=0[a]")
    else:
        chain += ";[0:a]anull[a]"
    cmd += ["-filter_complex", chain, "-map", "[v]", "-map", "[a]"]
    if out_dur:
        cmd += ["-t", f"{out_dur:.2f}"]
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-r", "30",
            "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-movflags", "+faststart", dst]
    return cmd


def make_variant(src: str, dst: str, text_hook: str, recipe: dict, hook_audio: str | None = None,
                 hook_len: float = 0.0, text_seconds: float = 2.8, log=None) -> str:
    # Validate the brief's compliance requirements BEFORE touching any media. A missing logo is a
    # configuration error, and finding it out after probing and half-rendering just buries it under
    # an ffmpeg failure. Same reasoning as required_text: the brief makes the clip rejectable
    # without it, so this is a refusal, not something to shrug off and render anyway.
    logo = (recipe.get("required_logo") or "").strip()
    if logo and not os.path.exists(logo):
        raise RuntimeError(f"required logo watermark is missing: {logo}")
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    dur = probe_duration(src)
    png = badge = None
    required = (recipe.get("required_text") or "").strip()
    if required:
        fd, badge = tempfile.mkstemp(suffix=".png", prefix="badge_")
        os.close(fd)
        try:
            render_text_png(required, badge, max_width=760, size=44)
        except Exception as exc:
            # A brief that REQUIRES this text makes the clip rejectable without it, so this is a
            # failure, not a cosmetic miss. Refuse rather than ship an unpayable clip.
            os.remove(badge)
            raise RuntimeError(f"required on-screen text could not be rendered: {exc}") from exc
    if text_hook:
        fd, png = tempfile.mkstemp(suffix=".png", prefix="hook_")
        os.close(fd)
        try:
            render_text_png(text_hook, png)
        except Exception as exc:  # Pillow missing or font trouble: ship without the card, say so
            if log:
                log(f"  text hook skipped: {exc}")
            os.remove(png)
            png = None
    try:
        cmd = build_cmd(src, dst, png, recipe.get("text_pos", "top"), recipe.get("trim_start", 0.0),
                        recipe.get("trim_end", 0.0), recipe.get("zoom", 1.0), dur, hook_audio, hook_len,
                        text_seconds, badge, logo or None)
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"ffmpeg failed: {exc.stderr[-800:]}") from exc
    finally:
        for tmp in (png, badge):
            if tmp:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
    return dst
