"""
weather.py — one compact weather line for the ambient RIGHT NOW block.

Alex trains outdoors; "track at 6 PM, 94°F and a thunderstorm risk" is exactly the kind
of detail a JARVIS-grade assistant should just know. Open-Meteo is keyless and free, so
this costs nothing and needs no signup — only coordinates.

Config: WEATHER_LATLON env var. Two accepted forms:

    "42.36,-71.06"                                  one place, full detail
    "Ridgefield:41.28,-73.50; BC:42.34,-71.17"      several, labeled and compact

Unset => weather is simply absent from the ambient block (fail-soft, like every other
situational source). Coordinates are city-level context, not tracking — Alex sets them
once, by hand.

Several places matter because Alex's life spans them: home plus the campuses he cares
about. Each extra place costs a line in a context block that gets rebuilt every turn, so
multi-place output is deliberately terser than single-place output — enough to answer
"do I need a jacket there", not a forecast.

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


def _spots() -> list:
    """[(label, lat, lon)] parsed from WEATHER_LATLON; [] when unset or unusable.

    A malformed entry is dropped with a message rather than taking the whole config
    down — losing one campus's weather beats losing all of it over a stray character."""
    raw = os.environ.get("WEATHER_LATLON", "").strip()
    if not raw:
        return []
    out = []
    for chunk in raw.split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        label, sep, coords = chunk.rpartition(":")
        label = label.strip() if sep else ""
        try:
            lat, lon = (float(p) for p in coords.split(","))
        except (ValueError, TypeError):
            print(f"weather: skipping {chunk!r} — not 'lat,lon' or 'Label:lat,lon'")
            continue
        out.append((label, lat, lon))
    if not out:
        print(f"weather: WEATHER_LATLON unusable ({raw!r}); weather disabled")
    return out


def _fetch(lat: float, lon: float) -> dict:
    """Raw API call — separated as a test seam."""
    url = (f"{API}?latitude={lat}&longitude={lon}"
           "&current=temperature_2m,apparent_temperature,weather_code,wind_speed_10m"
           "&daily=temperature_2m_max,temperature_2m_min,precipitation_probability_max"
           "&temperature_unit=fahrenheit&wind_speed_unit=mph&timezone=auto&forecast_days=1")
    req = urllib.request.Request(url, headers={"User-Agent": "clarvis-second-brain"})
    with urllib.request.urlopen(req, timeout=6, context=_SSL_CONTEXT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def format_line(data: dict, compact: bool = False) -> str:
    """API payload -> one planning-grade line. Returns '' if the payload is unusable.

    `compact` trims to temperature, conditions and a real precipitation risk — used when
    several places are shown at once and the full high/low/feels-like treatment would
    crowd out everything else in the ambient block."""
    try:
        cur = data["current"]
        daily = data.get("daily", {})
        parts = [f"{round(cur['temperature_2m'])}°F"]
        words = _describe(cur.get("weather_code"))
        if words:
            parts.append(words)
        if compact:
            line = ", ".join(parts)
            rain = (daily.get("precipitation_probability_max") or [None])[0]
            if rain is not None and rain >= 50:
                line += f", {round(rain)}% precip"
            return line
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
    """The cached weather line(s), or '' when unconfigured/unavailable. Never raises.

    With several places configured, returns one labeled line each. One place failing
    doesn't suppress the others — a dead API for Cleveland shouldn't hide Boston."""
    spots = _spots()
    if not spots:
        return ""
    with _lock:
        fresh = _cache["line"] is not None and (time.time() - _cache["at"]) < TTL_SECONDS
        if fresh:
            return _cache["line"]
    compact = len(spots) > 1
    lines = []
    for label, lat, lon in spots:
        try:
            line = format_line(_fetch(lat, lon), compact=compact)
        except Exception as e:
            print(f"weather: fetch failed for {label or 'location'} ({e})")
            continue
        if line:
            lines.append(f"{label}: {line}" if label else line)
    if not lines:  # everything failed: serve stale if we have it, else nothing
        with _lock:
            return _cache["line"] or ""
    out = "\n".join(lines)
    with _lock:
        _cache["at"] = time.time()
        _cache["line"] = out
    return out


def invalidate() -> None:
    with _lock:
        _cache["at"] = 0.0
        _cache["line"] = None
