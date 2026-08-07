"""
weather.py — one compact weather line for the ambient RIGHT NOW block.

Alex trains outdoors; "track at 6 PM, 94°F and a thunderstorm risk" is exactly the kind
of detail a JARVIS-grade assistant should just know. Open-Meteo is keyless and free, so
this costs nothing and needs no signup — only coordinates.

Config: WEATHER_LATLON env var, e.g. "42.36,-71.06". Unset => weather is simply absent
from the ambient block (fail-soft, like every other situational source). Coordinates are
city-level context, not tracking — Alex sets them once, by hand.

Cached 15 minutes: weather doesn't move faster than that, and the ambient block rebuilds
every ~90s, so without a cache we'd hammer a free API for identical answers.
"""

import json
import os
import ssl
import threading
import time
import urllib.request

try:
    import certifi
    _SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CONTEXT = None  # system trust store

API = "https://api.open-meteo.com/v1/forecast"
TTL_SECONDS = 15 * 60

_lock = threading.Lock()
_cache = {"at": 0.0, "line": None}

# Open-Meteo WMO weather codes -> plain words (compressed to what matters for planning).
_CODES = [
    ((0,), "clear"),
    ((1, 2), "mostly clear"),
    ((3,), "overcast"),
    ((45, 48), "foggy"),
    ((51, 53, 55, 56, 57), "drizzle"),
    ((61, 63, 65, 66, 67, 80, 81, 82), "rain"),
    ((71, 73, 75, 77, 85, 86), "snow"),
    ((95, 96, 99), "thunderstorms"),
]


def _describe(code) -> str:
    for codes, words in _CODES:
        if code in codes:
            return words
    return ""


def _latlon() -> tuple | None:
    raw = os.environ.get("WEATHER_LATLON", "").strip()
    if not raw:
        return None
    try:
        lat, lon = (float(p) for p in raw.split(","))
        return lat, lon
    except (ValueError, TypeError):
        print(f"weather: WEATHER_LATLON isn't 'lat,lon' ({raw!r}); weather disabled")
        return None


def _fetch(lat: float, lon: float) -> dict:
    """Raw API call — separated as a test seam."""
    url = (f"{API}?latitude={lat}&longitude={lon}"
           "&current=temperature_2m,apparent_temperature,weather_code,wind_speed_10m"
           "&daily=temperature_2m_max,temperature_2m_min,precipitation_probability_max"
           "&temperature_unit=fahrenheit&wind_speed_unit=mph&timezone=auto&forecast_days=1")
    req = urllib.request.Request(url, headers={"User-Agent": "clarvis-second-brain"})
    with urllib.request.urlopen(req, timeout=6, context=_SSL_CONTEXT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def format_line(data: dict) -> str:
    """API payload -> one planning-grade line. Returns '' if the payload is unusable."""
    try:
        cur = data["current"]
        daily = data.get("daily", {})
        parts = [f"{round(cur['temperature_2m'])}°F"]
        words = _describe(cur.get("weather_code"))
        if words:
            parts.append(words)
        feels = cur.get("apparent_temperature")
        if feels is not None and abs(feels - cur["temperature_2m"]) >= 6:
            parts.append(f"feels like {round(feels)}°F")
        wind = cur.get("wind_speed_10m")
        if wind is not None and wind >= 15:
            parts.append(f"windy ({round(wind)} mph)")
        line = ", ".join(parts)
        hi = (daily.get("temperature_2m_max") or [None])[0]
        lo = (daily.get("temperature_2m_min") or [None])[0]
        if hi is not None and lo is not None:
            line += f" — high {round(hi)}°F / low {round(lo)}°F"
        rain = (daily.get("precipitation_probability_max") or [None])[0]
        if rain is not None and rain >= 30:
            line += f", {round(rain)}% chance of precipitation today"
        return line
    except (KeyError, TypeError, IndexError):
        return ""


def current_line() -> str:
    """The cached weather line, or '' when unconfigured/unavailable. Never raises."""
    spot = _latlon()
    if spot is None:
        return ""
    with _lock:
        fresh = _cache["line"] is not None and (time.time() - _cache["at"]) < TTL_SECONDS
        if fresh:
            return _cache["line"]
    try:
        line = format_line(_fetch(*spot))
    except Exception as e:  # network/API trouble: serve stale if we have it, else nothing
        print(f"weather: fetch failed ({e})")
        with _lock:
            return _cache["line"] or ""
    with _lock:
        _cache["at"] = time.time()
        _cache["line"] = line
    return line


def invalidate() -> None:
    with _lock:
        _cache["at"] = 0.0
        _cache["line"] = None
