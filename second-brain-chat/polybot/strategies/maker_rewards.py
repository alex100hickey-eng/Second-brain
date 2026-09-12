"""maker_rewards (M1): two-sided quotes inside the Polymarket US liquidity-incentive spread.

Needs the US key (the incentive program, its per-market spread/size targets and the book are all
key-gated). Until then this module reports idle. Quote logic (documented for the build-out):
  fair = model mid (weather) or offshore mid (politics); post bid = fair - half_spread, ask = fair +
  half_spread with half_spread inside the program's max spread; size = per-market cap / 2 each side;
  re-quote every 30 s; pull quotes when inventory > cap or the reference moves > 2c in 10 s.
"""
from __future__ import annotations

from .. import config
from .base import Strategy


class MakerRewards(Strategy):
    name = "maker_rewards"

    def __init__(self, cfg: config.Config, us_venue):
        self.cfg = cfg
        self.us = us_venue
        self.idle_reason = None if us_venue.available else us_venue.why_unavailable

    def scan(self, ctx=None) -> list:
        return []  # quoting is stateful; it lives in the runner's live loop once the key exists
