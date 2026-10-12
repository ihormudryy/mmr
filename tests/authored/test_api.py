"""SP3 Plan 1 Task 1: the authored-strategy API (spec 7.1)."""
from __future__ import annotations

import math

import pandas as pd
import pytest

from trader.authored.api import AuthoredStrategy, Decision


def test_decision_holds_action_and_probability():
    d = Decision(action="BUY", probability=0.6)
    assert (d.action, d.probability) == ("BUY", 0.6)


def test_decision_is_frozen():
    d = Decision(action="SELL", probability=0.1)
    with pytest.raises(Exception):
        d.action = "BUY"  # type: ignore[misc]


@pytest.mark.parametrize("bad", [-0.01, 1.01, math.nan, math.inf])
def test_decision_rejects_a_probability_outside_the_unit_interval(bad):
    with pytest.raises(ValueError):
        Decision(action="BUY", probability=bad)


def test_decision_rejects_an_unknown_action():
    with pytest.raises(ValueError):
        Decision(action="HOLD", probability=0.5)  # type: ignore[arg-type]


def test_default_lookback_is_200():
    class S(AuthoredStrategy):
        def on_bar(self, history, params):
            return None

    assert S.LOOKBACK_BARS == 200


@pytest.mark.parametrize("bad", [0, -1, 2001, 1.5, True])
def test_lookback_bars_out_of_range_is_refused(bad):
    with pytest.raises(ValueError):
        class S(AuthoredStrategy):
            LOOKBACK_BARS = bad

            def on_bar(self, history, params):
                return None


def test_subclass_runs_on_bar():
    class S(AuthoredStrategy):
        LOOKBACK_BARS = 3

        def on_bar(self, history, params):
            return Decision(action="BUY", probability=0.9) if len(history) >= 2 else None

    frame = pd.DataFrame(
        {"open": [1.0, 2.0], "high": [1.0, 2.0], "low": [1.0, 2.0],
         "close": [1.0, 2.0], "volume": [1.0, 2.0]},
        index=pd.DatetimeIndex(["2024-01-02T14:30:00Z", "2024-01-02T14:31:00Z"], name="date"),
    )
    assert S().on_bar(frame, {}).action == "BUY"
