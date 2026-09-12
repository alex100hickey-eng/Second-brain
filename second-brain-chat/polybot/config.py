"""Configuration: caps, module modes, cities/stations. Secrets come from the environment only.

Env vars (never in code or chat):
    POLYMARKET_KEY_ID, POLYMARKET_SECRET_KEY   Polymarket US API key (polymarket.us/developer)
    POLYBOT_CONFIG                              optional path to config.json
    POLYBOT_DB                                  optional path to the sqlite ledger
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.environ.get("POLYBOT_CONFIG", os.path.join(ROOT, "config.json"))
DB_PATH = os.environ.get("POLYBOT_DB", os.path.join(ROOT, "polybot.db"))
KILL_PATH = os.path.join(ROOT, "KILL")          # touch this file = every module stops placing orders
CALIBRATION_PATH = os.path.join(ROOT, "calibration.json")
REPORT_PATH = os.path.join(ROOT, "report-latest.txt")

MODES = ("off", "paper", "signal", "live")

# Module registry. Order = display order in reports.
MODULES = [
    "weather_hold",          # H1  forecast ensemble vs bucket price, hold to settlement (+ take-profit)
    "weather_obs",           # S1  hourly observation kills buckets before the book reprices
    "weather_lock",          # S8  daily max/min is locked after the peak; buy the winner at 90-97c
    "weather_model_update",  # S7  new model run moved the probability, the book hasn't followed
    "bucket_sum",            # A1  mutually-exclusive buckets summing under/over $1
    "hold_favorites",        # H2  85-95c favorites + deadline longshot NO, sized by the calibration table
    "leadlag",               # S2/S3/S4  US book lags the offshore reference (politics; sports if enabled)
    "maker_rewards",         # M1  two-sided quotes inside the incentive spread (needs the US key)
]

# Weather cities on Polymarket US (docs.polymarket.us/faqs/weather-faqs): settlement = NWS Daily
# Climate Report (CLI) at 8 AM ET the next day. The offshore venue resolves on the HOURLY "Temp"
# column of a station named in each market's description (NYC = KLGA), which reads ~1°F under the
# CLI daily max. The station for the offshore rule is parsed from the market description at runtime.
CITIES = {
    "nyc":           {"query": "NYC",           "tz": "America/New_York",    "us_station": "KNYC", "cli_location": "NYC", "lat": 40.779, "lon": -73.969},
    "chicago":       {"query": "Chicago",       "tz": "America/Chicago",     "us_station": "KMDW", "cli_location": "MDW", "lat": 41.786, "lon": -87.752},
    "miami":         {"query": "Miami",         "tz": "America/New_York",    "us_station": "KMIA", "cli_location": "MIA", "lat": 25.795, "lon": -80.290},
    "los-angeles":   {"query": "Los Angeles",   "tz": "America/Los_Angeles", "us_station": "KLAX", "cli_location": "LAX", "lat": 33.938, "lon": -118.389},
    "san-francisco": {"query": "San Francisco", "tz": "America/Los_Angeles", "us_station": "KSFO", "cli_location": "SFO", "lat": 37.619, "lon": -122.375},
}

# Observation stations the offshore descriptions have been seen to reference.
STATIONS = {
    "KLGA": {"lat": 40.777, "lon": -73.872, "tz": "America/New_York"},
    "KNYC": {"lat": 40.779, "lon": -73.969, "tz": "America/New_York"},
    "KJFK": {"lat": 40.639, "lon": -73.762, "tz": "America/New_York"},
    "KMDW": {"lat": 41.786, "lon": -87.752, "tz": "America/Chicago"},
    "KORD": {"lat": 41.978, "lon": -87.906, "tz": "America/Chicago"},
    "KMIA": {"lat": 25.795, "lon": -80.290, "tz": "America/New_York"},
    "KLAX": {"lat": 33.938, "lon": -118.389, "tz": "America/Los_Angeles"},
    "KSFO": {"lat": 37.619, "lon": -122.375, "tz": "America/Los_Angeles"},
}


@dataclass
class Caps:
    max_per_market_usd: float = 20.0
    max_exposure_usd: float = 100.0
    bankroll_floor_usd: float = 120.0
    daily_loss_stop_usd: float = 20.0
    kelly_fraction: float = 0.25
    min_order_usd: float = 5.0
    sports_enabled: bool = False   # Ohio: sports contracts stay off until Alex flips this


@dataclass
class Config:
    caps: Caps = field(default_factory=Caps)
    modes: dict = field(default_factory=lambda: {m: "paper" for m in MODULES})
    cities: list = field(default_factory=lambda: list(CITIES))
    bankroll_usd: float = 200.0            # overwritten by the live balance when the US key exists
    edge_min_cents: float = 6.0            # weather_hold / hold_favorites entry edge
    take_profit_cents: float = 3.0         # resting sell above entry on hold modules
    lock_min_edge_cents: float = 2.0       # weather_lock: 1 - ask must exceed this
    dead_bucket_min_bid: float = 0.03      # weather_obs: only sell dead buckets bid >= this
    model_update_min_shift: float = 0.10   # weather_model_update: probability shift that counts
    bucket_sum_min_net_cents: float = 1.0  # bucket_sum: net after fees must exceed this
    hourly_rule_discount_f: float = 1.0    # offshore hourly-max rule reads this many °F under the daily max
    leadlag_move_cents: float = 3.0
    leadlag_window_s: int = 120
    leadlag_follow_ratio: float = 0.5
    favorites_band: tuple = (0.85, 0.95)
    longshot_band: tuple = (0.03, 0.20)
    horizon_days: int = 7

    def mode(self, module: str) -> str:
        return self.modes.get(module, "off")


def load(path: str = CONFIG_PATH) -> Config:
    cfg = Config()
    if os.path.exists(path):
        with open(path) as f:
            raw = json.load(f)
        caps = raw.pop("caps", {})
        cfg = Config(**{k: v for k, v in raw.items() if k in Config.__dataclass_fields__})
        cfg.caps = Caps(**{k: v for k, v in caps.items() if k in Caps.__dataclass_fields__})
        cfg.favorites_band = tuple(cfg.favorites_band)
        cfg.longshot_band = tuple(cfg.longshot_band)
    for m in MODULES:
        if cfg.modes.get(m) not in MODES:
            cfg.modes[m] = "paper"
    return cfg


def save(cfg: Config, path: str = CONFIG_PATH) -> None:
    with open(path, "w") as f:
        json.dump(asdict(cfg), f, indent=2, default=list)


def us_keys_present() -> bool:
    return bool(os.environ.get("POLYMARKET_KEY_ID") and os.environ.get("POLYMARKET_SECRET_KEY"))


def kill_switch_on() -> bool:
    return os.path.exists(KILL_PATH)
