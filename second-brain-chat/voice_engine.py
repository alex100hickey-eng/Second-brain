"""
voice_engine.py — cloud voice (ElevenLabs) with local fallbacks.

STT: ElevenLabs Scribe (`scribe_v1`) when ELEVENLABS_API_KEY is set — accurate,
fast, and crucially works ON THE SERVER, so push-to-talk functions from Alex's
phone anywhere. Falls back to local whisper.cpp on the Mac (video_processor)
when no key is set; the caller handles that routing.

TTS: ElevenLabs text-to-speech (`eleven_flash_v2_5` — the low-latency model) so
Jarvis has an actual voice. The UI falls back to browser speechSynthesis / the
Mac's `say` when no key is set.

Privacy note (SECURITY_NOTES §10): with a key set, push-to-talk audio and spoken
reply text are sent to ElevenLabs' API over HTTPS. No audio is stored locally;
nothing is sent anywhere without Alex pressing the mic / enabling Voice.

Stdlib-only (urllib + a minimal multipart encoder) — no new dependencies.
"""

import json
import os
import ssl
import urllib.error
import urllib.request
import uuid

# Framework Python on macOS ships without system CA certs — use certifi's bundle
# (already present as an httpx/supabase dependency) so HTTPS verification works.
try:
    import certifi
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:  # pragma: no cover — certifi is a transitive dep everywhere we run
    _SSL_CTX = ssl.create_default_context()

API_BASE = "https://api.elevenlabs.io/v1"
# Default voice: "George" — a deep, calm British voice. Override with
# ELEVENLABS_VOICE_ID (find ids at elevenlabs.io/app/voice-library).
DEFAULT_VOICE_ID = "JBFqnCBsd6RMkjVDRZzb"
STT_MODEL = "scribe_v1"
TTS_MODEL = "eleven_flash_v2_5"   # lowest latency; fine quality for replies
TTS_MAX_CHARS = 1500              # cap cost/runtime on very long replies


def _api_key() -> str:
    return (os.environ.get("ELEVENLABS_API_KEY") or "").strip()


def available() -> bool:
    return bool(_api_key())


def _multipart(fields: dict, file_field: str, filename: str, file_bytes: bytes,
               file_mime: str):
    """Minimal multipart/form-data encoder (stdlib only)."""
    boundary = f"----jarvis{uuid.uuid4().hex}"
    lines = []
    for k, v in fields.items():
        lines += [f"--{boundary}", f'Content-Disposition: form-data; name="{k}"', "", str(v)]
    lines += [f"--{boundary}",
              f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"',
              f"Content-Type: {file_mime}", ""]
    head = ("\r\n".join(lines) + "\r\n").encode()
    tail = f"\r\n--{boundary}--\r\n".encode()
    return head + file_bytes + tail, f"multipart/form-data; boundary={boundary}"


def transcribe(audio_bytes: bytes, filename: str = "clip.webm",
               mime: str = "audio/webm") -> dict:
    """ElevenLabs Scribe STT. Returns {"text": ...} or {"error": ...}. Never raises."""
    if not available():
        return {"error": "ELEVENLABS_API_KEY not set"}
    try:
        body, ctype = _multipart({"model_id": STT_MODEL}, "file",
                                 filename, audio_bytes, mime)
        req = urllib.request.Request(
            f"{API_BASE}/speech-to-text", data=body, method="POST",
            headers={"xi-api-key": _api_key(), "Content-Type": ctype})
        with urllib.request.urlopen(req, timeout=60, context=_SSL_CTX) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        return {"text": (data.get("text") or "").strip()}
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:300]
        return {"error": f"ElevenLabs STT HTTP {e.code}: {detail}"}
    except Exception as e:
        return {"error": f"ElevenLabs STT failed: {e}"}


def speak(text: str) -> dict:
    """ElevenLabs TTS. Returns {"audio": <mp3 bytes>} or {"error": ...}. Never raises."""
    if not available():
        return {"error": "ELEVENLABS_API_KEY not set"}
    text = (text or "").strip()[:TTS_MAX_CHARS]
    if not text:
        return {"error": "no text"}
    voice = (os.environ.get("ELEVENLABS_VOICE_ID") or DEFAULT_VOICE_ID).strip()
    try:
        payload = json.dumps({"text": text, "model_id": TTS_MODEL}).encode()
        req = urllib.request.Request(
            f"{API_BASE}/text-to-speech/{voice}?output_format=mp3_44100_128",
            data=payload, method="POST",
            headers={"xi-api-key": _api_key(), "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60, context=_SSL_CTX) as resp:
            return {"audio": resp.read()}
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:300]
        return {"error": f"ElevenLabs TTS HTTP {e.code}: {detail}"}
    except Exception as e:
        return {"error": f"ElevenLabs TTS failed: {e}"}
