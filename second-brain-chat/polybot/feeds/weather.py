"""Weather references: Open-Meteo model ensembles (free) and NWS observations / climate reports (free).

Open-Meteo ensemble: ~80 members across ECMWF IFS + GFS, daily max/min per member -> a probability
per temperature bucket. NWS api.weather.gov: hourly station observations (°C, converted to whole °F,
the way both venues read them) and the CLI daily climate report Polymarket US settles on.
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

OM_ENSEMBLE = "https://ensemble-api.open-meteo.com/v1/ensemble"
OM_FORECAST = "https://api.open-meteo.com/v1/forecast"
NWS = "https://api.weather.gov"
UA = {"User-Agent": "polybot/0.1 (alex100hickey@gmail.com)", "Accept": "application/geo+json, application/json"}
TIMEOUT = 25
_session = requests.Session()


def _get(url, params=None, headers=None):
    r = _session.get(url, params=params, headers=headers or {}, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


# ---- model ensemble --------------------------------------------------------------------------
def ensemble_daily(lat: float, lon: float, tz: str, days: int = 2,
                   models: str = "ecmwf_ifs025,gfs025", unit: str = "F") -> dict:
    """{date: {'max': [members...], 'min': [members...]}} in the requested unit."""
    d = _get(OM_ENSEMBLE, {
        "latitude": lat, "longitude": lon, "daily": "temperature_2m_max,temperature_2m_min",
        "models": models, "temperature_unit": "fahrenheit" if unit == "F" else "celsius",
        "timezone": tz, "forecast_days": days,
    })
    daily = d.get("daily", {})
    out = {}
    for i, date in enumerate(daily.get("time", [])):
        out[date] = {
            "max": [daily[k][i] for k in daily if k.startswith("temperature_2m_max") and daily[k][i] is not None],
            "min": [daily[k][i] for k in daily if k.startswith("temperature_2m_min") and daily[k][i] is not None],
        }
    return out


def hourly_forecast(lat: float, lon: float, tz: str, unit: str = "F", days: int = 2):
    d = _get(OM_FORECAST, {
        "latitude": lat, "longitude": lon, "hourly": "temperature_2m",
        "temperature_unit": "fahrenheit" if unit == "F" else "celsius", "timezone": tz, "forecast_days": days,
    })
    h = d.get("hourly", {})
    return list(zip(h.get("time", []), h.get("temperature_2m", [])))


def bucket_probs(members: list, buckets, discount: float = 0.0, floor: float = 0.005) -> list:
    """Share of ensemble members whose whole-degree reading lands in each bucket.

    `discount` shifts every member down (the offshore hourly-max rule reads ~1°F under the true
    daily max). A small floor keeps no bucket at exactly zero, since the model is not the truth.
    """
    if not members:
        return [1.0 / len(buckets)] * len(buckets)
    counts = [0] * len(buckets)
    for v in members:
        t = int(math.floor(v - discount + 0.5))
        for i, b in enumerate(buckets):
            if b.contains(t):
                counts[i] += 1
                break
    n = len(members)
    probs = [max(c / n, floor) for c in counts]
    s = sum(probs)
    return [p / s for p in probs]


# ---- observations ----------------------------------------------------------------------------
def c_to_f_whole(c: float) -> int:
    return int(math.floor(c * 9.0 / 5.0 + 32.0 + 0.5))


def observations(station: str, start_iso: str | None = None, end_iso: str | None = None, limit: int = 500):
    """[(iso_utc, temp_whole_F)] oldest first. NWS returns newest first."""
    params = {"limit": limit}
    if start_iso:
        params["start"] = start_iso
    if end_iso:
        params["end"] = end_iso
    d = _get(f"{NWS}/stations/{station}/observations", params, UA)
    out = []
    for f in d.get("features", []):
        p = f.get("properties", {})
        t = (p.get("temperature") or {}).get("value")
        if t is None:
            continue
        out.append((p.get("timestamp"), c_to_f_whole(float(t))))
    out.sort()
    return out


def latest_observation(station: str):
    d = _get(f"{NWS}/stations/{station}/observations/latest", None, UA)
    p = d.get("properties", {})
    t = (p.get("temperature") or {}).get("value")
    return (p.get("timestamp"), c_to_f_whole(float(t))) if t is not None else (p.get("timestamp"), None)


def local_day_bounds(date_str: str, tz: str):
    z = ZoneInfo(tz)
    start = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=z)
    return start, start + timedelta(days=1)


def running_extreme(obs, date_str: str, tz: str, kind: str = "high"):
    """(extreme_so_far, n_obs, last_local_iso) over observations that fall in the local calendar day."""
    start, end = local_day_bounds(date_str, tz)
    vals, last = [], None
    for iso, temp in obs:
        try:
            t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        except ValueError:
            continue
        if start <= t < end:
            vals.append(temp)
            last = t.astimezone(ZoneInfo(tz)).isoformat()
    if not vals:
        return None, 0, None
    return (max(vals) if kind == "high" else min(vals)), len(vals), last


def hours_remaining_extreme(hourly, date_str: str, after_local_iso: str | None, tz: str, kind: str = "high"):
    """Max (or min) of the model's hourly forecast for the rest of the local day after `after_local_iso`."""
    vals = []
    for iso, temp in hourly:
        if temp is None or not iso.startswith(date_str):
            continue
        if after_local_iso and iso <= after_local_iso[:16]:
            continue
        vals.append(temp)
    if not vals:
        return None
    return max(vals) if kind == "high" else min(vals)


def hours_remaining_max(hourly, date_str: str, after_local_iso: str | None, tz: str):
    return hours_remaining_extreme(hourly, date_str, after_local_iso, tz, "high")


def historical_members(lat: float, lon: float, tz: str, start_date: str, end_date: str, unit: str = "F") -> dict:
    """What the deterministic models forecast for each day, from Open-Meteo's forecast archive:
    {date: {'max': [per-model], 'min': [per-model]}}. Thinner than the live ensemble (7 models vs ~80
    members) but honest: these are the forecasts that existed at the time."""
    d = _get("https://historical-forecast-api.open-meteo.com/v1/forecast", {
        "latitude": lat, "longitude": lon, "start_date": start_date, "end_date": end_date,
        "daily": "temperature_2m_max,temperature_2m_min",
        "models": "ecmwf_ifs025,gfs_seamless,icon_seamless,gem_seamless,meteofrance_seamless,ukmo_seamless,jma_seamless",
        "temperature_unit": "fahrenheit" if unit == "F" else "celsius", "timezone": tz,
    })
    daily = d.get("daily", {})
    out = {}
    for i, date in enumerate(daily.get("time", [])):
        out[date] = {
            "max": [daily[k][i] for k in daily if k.startswith("temperature_2m_max") and daily[k][i] is not None],
            "min": [daily[k][i] for k in daily if k.startswith("temperature_2m_min") and daily[k][i] is not None],
        }
    return out


def previous_run_hourly(lat: float, lon: float, tz: str, start_date: str, end_date: str, unit: str = "F",
                        model: str = "ecmwf_ifs025", days_back: int = 1):
    """Hourly temperatures as forecast `days_back` days earlier (Open-Meteo previous-runs API):
    [(iso_local_hour, temp)] — the 'remaining hours' view a scan would have had on the day."""
    var = f"temperature_2m_previous_day{days_back}"
    d = _get("https://previous-runs-api.open-meteo.com/v1/forecast", {
        "latitude": lat, "longitude": lon, "start_date": start_date, "end_date": end_date,
        "hourly": var, "models": model, "temperature_unit": "fahrenheit" if unit == "F" else "celsius", "timezone": tz,
    })
    h = d.get("hourly", {})
    return list(zip(h.get("time", []), h.get(var, [])))


# ---- NWS daily climate report (the Polymarket US settlement source) --------------------------
def cli_latest_text(location: str) -> str | None:
    d = _get(f"{NWS}/products/types/CLI/locations/{location}", None, UA)
    items = d.get("@graph", [])
    if not items:
        return None
    prod = _get(items[0]["@id"], None, UA)
    return prod.get("productText")


_CLI_MAX_RE = re.compile(r"MAXIMUM\s+(-?\d+)", re.I)
_CLI_MIN_RE = re.compile(r"MINIMUM\s+(-?\d+)", re.I)


def parse_cli(text: str):
    """Return {'max': int|None, 'min': int|None} from a CLI product's TEMPERATURE section."""
    if not text:
        return {"max": None, "min": None}
    mx = _CLI_MAX_RE.search(text)
    mn = _CLI_MIN_RE.search(text)
    return {"max": int(mx.group(1)) if mx else None, "min": int(mn.group(1)) if mn else None}
