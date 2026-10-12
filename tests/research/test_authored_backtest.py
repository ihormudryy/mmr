import datetime as dt
import pickle
from dataclasses import replace

import pandas as pd
import pytest

from tests.authored.fake_sandbox import FakeSandbox, caller_source
from tests.research.evaluation_fixtures import (CONIDS, build_spec_file, write_costs_config, write_trend_bars,
                                               write_universe)
from trader.authored.sandbox_client import SandboxFault, SandboxProtocolError, SandboxUnavailable
from trader.research.authored_backtest import RESEARCH_SESSION_CONID, AuthoredProxyStrategy, AuthoredReplayError
from trader.research.evaluation_jobs import RunEnvironment, WindowJob, run_window_job
from trader.research.strategy_key import authored_strategy_path

UTC = dt.timezone.utc
FEB = (dt.datetime(2024, 2, 1, tzinfo=UTC), dt.datetime(2024, 2, 9, 23, 59, tzinfo=UTC))
PARAMS = {"ENTRY_MINUTE": 600, "EXIT_MINUTE": 660}


def bars(count, start="2024-03-04 14:30"):
    index = pd.date_range(start, periods=count, freq="1min", tz="UTC", name="date")
    closes = [100.0 + i for i in range(count)]
    return pd.DataFrame({"open": closes, "high": closes, "low": closes, "close": closes, "volume": 1000.0},
                        index=index)


@pytest.fixture
def sandbox(monkeypatch):
    fake = FakeSandbox()
    monkeypatch.setattr("trader.research.authored_backtest.connect_sandbox", lambda socket_path: fake)
    return fake


def proxy(sandbox, name, class_name, params=None):
    return AuthoredProxyStrategy(client=sandbox, digest=sandbox.add(caller_source(name)), class_name=class_name,
                                 params=params or {}, bar_size="1 min")


def test_each_decision_saw_only_the_bars_up_to_it(sandbox):
    frame = bars(60)
    strategy = proxy(sandbox, "counts_bars", "CountsBars")
    state = strategy.precompute(frame)
    probabilities = [strategy.on_bar(frame.iloc[:i + 1], state, i).probability for i in range(len(frame))]
    assert probabilities == [min(i + 1, 50) / 10000 for i in range(len(frame))]
    assert [len(s.stamps) for s in sandbox.sessions] == [60]
    assert [s.conids for s in sandbox.sessions] == [(RESEARCH_SESSION_CONID,)]    # one conid per research session
    assert sandbox.open_sessions == []


def test_a_fault_is_raised_at_the_first_bar_and_later_conids_open_nothing(sandbox):
    frame = bars(5)
    strategy = proxy(sandbox, "faults_above_limit", "FaultsAboveLimit", {"LIMIT": 101.5})
    state = strategy.precompute(frame)
    with pytest.raises(SandboxFault):
        strategy.on_bar(frame.iloc[:1], state, 0)
    assert strategy.precompute(frame) == {}
    assert len(sandbox.sessions) == 1 and sandbox.open_sessions == []


def test_an_unreachable_sandbox_is_an_availability_error(sandbox):
    sandbox.unavailable = True
    frame = bars(3)
    strategy = proxy(sandbox, "counts_bars", "CountsBars")
    state = strategy.precompute(frame)
    with pytest.raises(SandboxUnavailable):
        strategy.on_bar(frame.iloc[:1], state, 0)


def test_a_bar_the_decisions_were_not_made_for_is_refused(sandbox):
    strategy = proxy(sandbox, "counts_bars", "CountsBars")
    state = strategy.precompute(bars(3))
    with pytest.raises(AuthoredReplayError):
        strategy.on_bar(bars(3, start="2024-03-05 14:30").iloc[:2], state, 1)


def test_a_caller_error_ends_the_run_with_its_cause_and_opens_nothing_more(sandbox):
    strategy = proxy(sandbox, "counts_bars", "CountsBars", {"LIMIT": "not a number"})
    state = strategy.precompute(bars(3))
    with pytest.raises(AuthoredReplayError) as caught:
        strategy.on_bar(bars(3).iloc[:1], state, 0)
    assert isinstance(caught.value.__cause__, SandboxProtocolError)
    assert strategy.precompute(bars(3)) == {} and sandbox.sessions == []


def test_a_bar_past_the_decided_ones_is_refused(sandbox):
    strategy = proxy(sandbox, "counts_bars", "CountsBars")
    state = strategy.precompute(bars(3))
    with pytest.raises(AuthoredReplayError):
        strategy.on_bar(bars(4), state, 3)

def test_the_proxy_never_runs_live(sandbox):
    with pytest.raises(AuthoredReplayError):
        proxy(sandbox, "counts_bars", "CountsBars").on_prices(bars(3))


@pytest.fixture
def market(tmp_path, tmp_duckdb_path):
    write_universe(tmp_duckdb_path)
    write_trend_bars(tmp_duckdb_path, drift=0.0006)
    costs = write_costs_config(tmp_path / "execution_costs.yaml")
    build_spec_file(tmp_path)
    return RunEnvironment(
        history_db=tmp_duckdb_path, universe_db=tmp_duckdb_path, universe_library="Universes",
        execution_costs_path=str(costs), strategy_file=str(tmp_path / "strategies" / "time_of_day.py"),
        class_name="TimeOfDay", conids=tuple(CONIDS), bar_size="15 mins", order_notional=1900.0,
        account_equity=100_000.0, max_gross_allocation=0.05, regular_session_calendar="XNYS")


def authored(env, sandbox):
    digest = sandbox.add(caller_source("time_of_day_port"))
    return replace(env, strategy_file=authored_strategy_path(digest), class_name="TimeOfDayPort",
                   authored_digest=digest, sandbox_socket="/run/sandbox-research/sandbox.sock")


def job():
    return WindowJob(point_key="p", params=PARAMS, window_kind="fold", window_index=0, start=FEB[0], end=FEB[1],
                     cost_multiplier=1.0)


def fills(outcome):
    return [(t["timestamp"], t["conid"], t["action"], t["quantity"], t["price"]) for t in outcome.trades]


@pytest.mark.timeout(240)
def test_an_authored_window_trades_like_the_catalogue_strategy(market, sandbox):
    catalogue = run_window_job(market, job())
    ported = run_window_job(authored(market, sandbox), job())
    assert fills(ported) == fills(catalogue) and fills(ported)
    assert ported.net_pnl == pytest.approx(catalogue.net_pnl)
    assert len(sandbox.sessions) == len(CONIDS)
    assert sandbox.most_open == 1                    # Review Focus 3: one session at a time fits the pool of 4


def test_an_authored_environment_pickles_for_the_process_pool(market, sandbox):
    env = authored(market, sandbox)
    assert pickle.loads(pickle.dumps(env)) == env
