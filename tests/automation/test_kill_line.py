"""SP1 Plan 4 Task 2: pure kill-line evaluation."""
from __future__ import annotations

import math

import pytest

from tests.automation.experiment_fixtures import armed_record
from trader.automation.ai_paper_config import AiPaperConfig, AiPaperConfigError, load_ai_paper_config
from trader.automation.kill_line import KillLine, KillLineInputError, effective_kill_line, evaluate_kill_line

START = KillLine(20.0, "start")
PEAK = KillLine(20.0, "peak")


@pytest.mark.parametrize("nlv,hit", [(80_000.0, True), (80_000.01, False), (79_000.0, True), (120_000.0, False)])
def test_start_basis_hits_at_the_line(nlv, hit):
    assert evaluate_kill_line(START, anchor=100_000.0, peak=130_000.0, net_liquidation=nlv).hit is hit


def test_peak_basis_measures_from_the_peak():
    ev = evaluate_kill_line(PEAK, anchor=100_000.0, peak=125_000.0, net_liquidation=100_000.0)
    assert (ev.hit, ev.reference, ev.drawdown_pct) == (True, 125_000.0, pytest.approx(20.0))


def test_peak_never_below_anchor():                       # a peak column can lag one tick
    ev = evaluate_kill_line(PEAK, anchor=100_000.0, peak=90_000.0, net_liquidation=95_000.0)
    assert ev.reference == 100_000.0


def test_a_gain_is_a_negative_drawdown():
    ev = evaluate_kill_line(START, anchor=100_000.0, peak=100_000.0, net_liquidation=110_000.0)
    assert ev.hit is False and ev.drawdown_pct == pytest.approx(-10.0)


@pytest.mark.parametrize("field,value", [("net_liquidation", math.nan), ("net_liquidation", 0.0),
    ("net_liquidation", -5.0), ("net_liquidation", True), ("net_liquidation", None), ("anchor", math.inf),
    ("peak", "1e5")])
def test_bad_inputs_raise_not_kill(field, value):
    kwargs = dict(anchor=100_000.0, peak=100_000.0, net_liquidation=90_000.0) | {field: value}
    with pytest.raises(KillLineInputError):
        evaluate_kill_line(START, **kwargs)


@pytest.mark.parametrize("frozen,config,expected", [
    (None, None, None), (20.0, None, 20.0), (None, 15.0, 15.0), (20.0, 15.0, 15.0), (15.0, 20.0, 15.0)])
def test_tighter_config_wins_looser_is_ignored(frozen, config, expected):        # Review Focus 4, K9
    line = effective_kill_line(armed_record(kill_drawdown_pct=frozen, kill_basis="start"),
                               AiPaperConfig(experiment_kill_drawdown_pct=config, experiment_kill_basis="peak"))
    assert (line.pct if line else None) == expected and (line is None or line.basis == "start")


def test_kill_pct_true_fails_config_load():               # pins Plan 3's parser: no bool-as-number
    with pytest.raises(AiPaperConfigError):
        load_ai_paper_config({"enabled": True, "experiment_kill_drawdown_pct": True}, trading_mode="paper")
