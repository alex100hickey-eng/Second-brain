"""Global hourly station observations via aviationweather.gov METAR (free, no key). The offshore
markets outside the US resolve on exactly these airport readings (Weather Underground shows the
same METARs), and the NWS hourly table for US stations is the METAR feed too.
"""
from __future__ import annotations

import requests

URL = "https://aviationweather.gov/api/data/metar"
TIMEOUT = 25
_session = requests.Session()
_session.headers["User-Agent"] = "polybot/0.1 (research)"


def c_to_f_whole(c: float) -> int:
    import math
    return int(math.floor(c * 9.0 / 5.0 + 32.0 + 0.5))


def observations(icao: str, hours: int = 24, unit: str = "F") -> list:
    """[(iso_utc, whole_degrees)] oldest first. `unit` F or C to match the market's buckets."""
    r = _session.get(URL, params={"ids": icao, "hours": min(max(int(hours), 1), 336), "format": "json"}, timeout=TIMEOUT)
    r.raise_for_status()
    out = []
    for o in r.json() or []:
        t = o.get("temp")
        ts = o.get("reportTime") or o.get("obsTime")
        if t is None or ts is None:
            continue
        try:
            c = float(t)
        except (TypeError, ValueError):
            continue
        if isinstance(ts, (int, float)):
            from datetime import datetime, timezone
            ts = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
        iso = str(ts).replace(" ", "T")
        if not iso.endswith("Z") and "+" not in iso:
            iso += "Z"
        out.append((iso.replace("Z", "+00:00"), c_to_f_whole(c) if unit == "F" else int(round(c))))
    out.sort()
    return out
