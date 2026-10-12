"""The only repo module an authored strategy may import (spec 7.1).

An authored strategy sees one symbol's recent bars and returns a long-only
decision. It gets no storage, runtime, logger, clock, randomness or conid
lookup. Jev and the risk gate size the trade, as for catalogue signals.
"""
from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Literal, Mapping

import pandas as pd

MIN_LOOKBACK_BARS = 1
MAX_LOOKBACK_BARS = 2000
ACTIONS = ("BUY", "SELL")


@dataclass(frozen=True)
class Decision:
    action: Literal["BUY", "SELL"]
    probability: float

    def __post_init__(self) -> None:
        if self.action not in ACTIONS:
            raise ValueError(f"action must be one of {ACTIONS}, got {self.action!r}")
        probability = self.probability
        if not isinstance(probability, (int, float)) or isinstance(probability, bool):
            raise ValueError(f"probability must be a real number, got {probability!r}")
        if not math.isfinite(probability) or not 0.0 <= probability <= 1.0:
            raise ValueError(f"probability must be within 0..1, got {probability!r}")


class AuthoredStrategy(ABC):
    LOOKBACK_BARS: int = 200

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        lookback = cls.LOOKBACK_BARS
        if isinstance(lookback, bool) or not isinstance(lookback, int):
            raise ValueError(f"LOOKBACK_BARS must be an int, got {lookback!r}")
        if not MIN_LOOKBACK_BARS <= lookback <= MAX_LOOKBACK_BARS:
            raise ValueError(
                f"LOOKBACK_BARS must be within {MIN_LOOKBACK_BARS}..{MAX_LOOKBACK_BARS}, "
                f"got {lookback}")

    @abstractmethod
    def on_bar(self, history: pd.DataFrame, params: Mapping[str, float]) -> Decision | None:
        """Decide on the bar at ``history.index[-1]``.

        ``history`` holds the last ``LOOKBACK_BARS`` completed bars (columns
        open, high, low, close, volume; DatetimeIndex). ``params`` carries the
        tunables' values. Return a BUY or SELL ``Decision`` or ``None``.
        """
        raise NotImplementedError
