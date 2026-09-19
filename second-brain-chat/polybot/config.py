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

# Every city the offshore venue runs daily temperature markets for (Sep 2026 search). Only the five
# above exist on Polymarket US; the rest are paper-proxy signal volume and backtest data.
# slug: (tz, lat, lon, ICAO used when the market description names no station)
OFFSHORE_CITIES = {
    "ankara":       ("Europe/Istanbul",     40.128,  32.995, "LTAC"),
    "atlanta":      ("America/New_York",    33.640, -84.427, "KATL"),
    "beijing":      ("Asia/Shanghai",       40.080, 116.585, "ZBAA"),
    "busan":        ("Asia/Seoul",          35.180, 128.938, "RKPK"),
    "chengdu":      ("Asia/Shanghai",       30.579, 103.947, "ZUUU"),
    "chongqing":    ("Asia/Shanghai",       29.719, 106.642, "ZUCK"),
    "denver":       ("America/Denver",      39.856, -104.673, "KDEN"),
    "helsinki":     ("Europe/Helsinki",     60.317,  24.963, "EFHK"),
    "hong-kong":    ("Asia/Hong_Kong",      22.309, 113.915, "VHHH"),
    "kuala-lumpur": ("Asia/Kuala_Lumpur",    2.745, 101.710, "WMKK"),
    "london":       ("Europe/London",       51.470,  -0.461, "EGLL"),
    "madrid":       ("Europe/Madrid",       40.472,  -3.561, "LEMD"),
    "mexico-city":  ("America/Mexico_City", 19.436, -99.072, "MMMX"),
    "munich":       ("Europe/Berlin",       48.354,  11.786, "EDDM"),
    "paris":        ("Europe/Paris",        49.010,   2.548, "LFPG"),
    "seoul":        ("Asia/Seoul",          37.469, 126.451, "RKSI"),
    "shanghai":     ("Asia/Shanghai",       31.143, 121.805, "ZSPD"),
    "shenzhen":     ("Asia/Shanghai",       22.639, 113.811, "ZGSZ"),
    "singapore":    ("Asia/Singapore",       1.364, 103.991, "WSSS"),
    "taipei":       ("Asia/Taipei",         25.080, 121.232, "RCTP"),
    "tel-aviv":     ("Asia/Jerusalem",      32.009,  34.886, "LLBG"),
    "tokyo":        ("Asia/Tokyo",          35.553, 139.781, "RJTT"),
    "toronto":      ("America/Toronto",     43.677, -79.631, "CYYZ"),
    "wellington":   ("Pacific/Auckland",   -41.327, 174.805, "NZWN"),
    "wuhan":        ("Asia/Shanghai",       30.784, 114.208, "ZHHH"),
}


def city_meta(slug: str) -> dict | None:
    """Unified city record: {query, tz, lat, lon, station, us_station?, cli_location?}."""
    if slug in CITIES:
        m = dict(CITIES[slug])
        m["station"] = m["us_station"]
        return m
    if slug in OFFSHORE_CITIES:
        tz, lat, lon, icao = OFFSHORE_CITIES[slug]
        return {"query": slug.replace("-", " ").title(), "tz": tz, "lat": lat, "lon": lon, "station": icao}
    return None


def all_city_slugs() -> list:
    return list(CITIES) + [c for c in OFFSHORE_CITIES if c not in CITIES]


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
    # A fresh checkout starts where the evidence left it, not at a hopeful "paper" for everything.
    # weather_hold: 1,744 backtested signals at -8.9%, -$2,456 in paper. weather_model_update:
    # 71 closed paper trades, 63% wins, -$291, negative in every edge band (2026-09-18).
    modes: dict = field(default_factory=lambda: {
        m: ("off" if m in ("weather_hold", "weather_model_update") else "paper") for m in MODULES})
    cities: list = field(default_factory=lambda: list(CITIES))
    # Offshore is a read-only proxy on a different settlement rule, and under the current rules it
    # is structurally incapable of producing a signal: its median spread inside weather_lock's
    # 0.80-0.97 band is 89c against a 10c filter. Measured 2026-09-18 — in the 24h after the rules
    # change it wrote 48,389 snapshots over 1,298 markets and generated ZERO signals, which was 93%
    # of a 137 MB database and ~1,440 API calls a day for nothing. Five cities keeps the proxy
    # alive for comparison at a sixth of the cost; flip back to True if offshore ever matters again.
    all_cities: bool = False
    kinds: list = field(default_factory=lambda: ["high", "low"])
    bankroll_usd: float = 200.0            # overwritten by the live balance when the US key exists
    edge_min_cents: float = 6.0            # weather_hold / hold_favorites entry edge
    take_profit_cents: float = 3.0         # resting sell above entry on hold modules
    lock_min_edge_cents: float = 2.0       # weather_lock: 1 - ask must exceed this
    lock_max_spread_cents: float = 10.0    # weather_lock: wider is an empty book, not a price
    lock_take_min_edge_cents: float = 4.0  # weather_lock crosses the spread; make it worth the fee
    lock_min_price: float = 0.80           # weather_lock: below this the market is calling the lock wrong, and it has been right every time (paper 2026-09-12..18: <50c 0/8 -$160, 50-80c 2/4 -$8, 80c+ 21/22 +$17)
    dead_bucket_max_bid: float = 0.95      # weather_obs: a bucket the market prices 95c+ is not dead, our feed is (paper 2026-09-12..18: 0/6, -$70)
    dead_bucket_min_bid: float = 0.03      # weather_obs: only sell dead buckets bid >= this
    model_update_min_shift: float = 0.10   # weather_model_update: probability shift that counts
    bucket_sum_min_net_cents: float = 1.0  # bucket_sum: net per set after fees must exceed this
    arb_max_sets: int = 200                # hard ceiling on one arb set, whatever the book offers
    # An arb's risk is NOT per-leg direction risk -- once the set is complete it pays $1 whatever
    # happens, so the per-market cap meant for single bets is the wrong ruler and a costly one:
    # nyc offered 73 sets of depth while $20/market sized us to 23, i.e. a third of the arb. What
    # an arb can actually lose is the cost of a set that half-fills, so the cap belongs on the SET.
    arb_max_set_cost_usd: float = 50.0
    # Cents per set is not profit — it is profit per dollar TIED UP UNTIL SETTLEMENT, and the
    # venue's events settle anywhere from tomorrow to next March. Measured 2026-09-19: a boc
    # sell-all paid 1c on a $3.96 set that settles in 39 days (0.006%/day) while a weather set
    # paid 12c on $0.92 settling in ~1.25 days (10%/day) — a 1,600x difference that a flat
    # cents-per-set threshold cannot see. Without this the bot locks the bankroll in the worst
    # trade available and misses every good one for a month.
    arb_min_roc_per_day_pct: float = 0.5
    # An arb that does not complete has to be unwound, and unwinding means selling every filled
    # leg back at the bid — one full spread per leg. The first real set (chicago 2026-09-19)
    # offered 6.0c a set against 20c of spread across its six legs: $0.12 of profit risking $0.40
    # to get it. Both scale with the number of sets, so the test is size-free: the arb must pay
    # for its own unwind. Lower this only when live fills prove reliable.
    # How much of a failed set's unwind the arb must pre-pay. 1.0 assumed a half-fill was
    # CERTAIN, which was a guess and an expensive one: across nine days of US books exactly ONE
    # event-minute cleared it, so the filter was really an off switch. Measured 2026-09-19 instead:
    # a US quote changes 1.7% of the time within 30s (0.4% for the cheap legs we buy), and 30s is
    # the whole window between the depth read and the order landing. That is ~10% for a six-leg
    # set before counting that some moves are favourable. 0.25 is a 2.5x margin on the measured
    # rate, stays clearly +EV (0.25*spread earned against ~0.10*spread expected cost), and passes
    # 48 of those nine days' event-minutes instead of one.
    arb_unwind_cover: float = 0.25
    arb_min_profit_usd: float = 0.10       # below this a set is not worth the calls or the risk
    arb_pass_budget_s: float = 45.0        # a US scan pass abandons its tail rather than hold the loop
    arb_live_ok: bool = False              # arb legs stay paper until the executor can unwind a partial set
    hourly_rule_discount_f: float = 0.0    # offshore hourly-max rule discount; backtest fit 2026-09-12 (208 city-days) says 0.0, not the 1.0 assumed
    leadlag_move_cents: float = 3.0
    leadlag_window_s: int = 120
    leadlag_follow_ratio: float = 0.5
    favorites_band: tuple = (0.85, 0.95)
    longshot_band: tuple = (0.03, 0.20)
    horizon_days: int = 7
    hold_price_band: tuple = (0.06, 0.94)  # weather_hold / model_update: the YES level the market must sit in; 1-5c and
                                           # 95-99c carry information the model can't have (2026-09-12 paper: -$412 there)
    hold_edge_max_cents: float = 30.0      # a bigger model-vs-market gap is a model or venue-rule error, not an edge
    gate_since_ts: float = 0.0             # signals before this epoch don't count toward the report or the gate
    min_us_signals: int = 10               # a module cannot go live on offshore evidence alone (see the gate)
    auto_promote: bool = False             # True: the 07:00 report flips PASS modules paper -> live by itself
    snapshot_keep_days: int = 7            # book snapshots older than this are pruned at 03:00

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
        cfg.hold_price_band = tuple(cfg.hold_price_band)
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
