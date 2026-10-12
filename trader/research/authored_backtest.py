"""Backtests of an authored strategy (SP3 spec 7.6): its code runs only in the research sandbox.

The backtester walks all conids together, bar by bar. Holding one session per conid through that walk would need
up to 20 sessions at once; the research pool has 4. So ``precompute`` streams each conid's bars through one
session, one bar at a time in time order, and keeps only the decisions; ``on_bar(prices, state, i)`` returns the
decision for bar i. When the sandbox decided bar i it had been sent bars 0..i only, so no decision can look ahead
(Plan 4 ruling 1). Fills at the next open, costs, live rules and P&L stay the backtester's.
"""
from __future__ import annotations

import logging
from typing import Any, Mapping, Optional

import pandas as pd

from trader.authored.sandbox_client import SandboxClient, SandboxFault, SandboxUnavailable
from trader.authored.streaming import BarStream, decision_signal
from trader.objects import BarSize
from trader.simulation.backtester import Backtester, BacktestResult
from trader.trading.strategy import Signal, Strategy, StrategyContext, StrategyState


class AuthoredReplayError(RuntimeError):
    """The proxy holds no decision for a bar the backtester asked about: a bug or a failure outside the
    sandbox, never a strategy fault."""


class SandboxOpenFault(SandboxFault):
    """A fault answered to OPEN: the stored file did not load. No session reported its build, so the evaluator
    checks the build again before it blames the file (ruling F7b)."""

    def __reduce__(self):
        return _sandbox_open_fault, (self.detail, self.code)


def _sandbox_open_fault(detail: str, code: str) -> SandboxOpenFault:
    return SandboxOpenFault(detail, code=code)


# The backtester's precompute does not name its conid. A research session holds exactly one conid and the
# strategy never sees it, so one fixed label is enough; each conid still gets its own session (ruling 1, D1).
RESEARCH_SESSION_CONID = 1


def connect_sandbox(socket_path: str) -> SandboxClient:
    """The research socket's client. Tests replace this function with an in-process fake."""
    return SandboxClient(socket_path)


class AuthoredProxyStrategy(Strategy):
    def __init__(self, *, client: Any, digest: str, class_name: str, params: Mapping[str, float], bar_size: str,
                 expected_build: Optional[str] = None):
        super().__init__()
        self._client, self._digest, self._class_name = client, digest, class_name
        self._params, self._bar_size = dict(params), bar_size
        self._expected_build = expected_build
        self._failure: Optional[Exception] = None

    def precompute(self, prices: pd.DataFrame) -> dict:
        """One session for this conid's bars. An error is kept and raised by ``on_bar``, because the backtester
        logs and drops what ``precompute`` raises. After the first error no later conid opens a session."""
        if self._failure is not None:
            return {}
        stream = None
        try:
            session = self._open()
            stream = BarStream(session, RESEARCH_SESSION_CONID)
            self._require_expected_build(session)
            return {"stamps": prices.index, "decisions": stream.send_frame(prices)}
        except (SandboxFault, SandboxUnavailable) as exc:
            self._failure = exc
            return {}
        except Exception as exc:
            self._failure = AuthoredReplayError(f"streaming bars to the sandbox failed outside it: {exc}")
            self._failure.__cause__ = exc
            return {}
        finally:
            if stream is not None:
                stream.close()

    def _open(self) -> Any:
        try:
            return self._client.open_session(self._digest, self._class_name, self._params, self._bar_size,
                                             [RESEARCH_SESSION_CONID])
        except SandboxFault as fault:
            raise SandboxOpenFault(fault.detail, code=fault.code) from fault

    def _require_expected_build(self, session: Any) -> None:
        """The trader proves the build of research's STARTED report; a sandbox restarted onto another build
        would run an unreported one (pre-flight F7). Not a fault of the strategy."""
        if self._expected_build is not None and session.image_digest != self._expected_build:
            raise SandboxUnavailable(f"the session opened on build {session.image_digest}, "
                                     f"STARTED reported {self._expected_build}")

    def on_bar(self, prices: pd.DataFrame, state: dict, index: int) -> Optional[Signal]:
        if not state:
            raise self._failure or AuthoredReplayError(
                f"no sandbox decisions for this conid at bar {index}: precompute failed outside the sandbox")
        if not 0 <= index < len(state["stamps"]):
            raise AuthoredReplayError(f"bar {index} is outside the {len(state['stamps'])} bars that were decided")
        if prices.index[-1] != state["stamps"][index]:
            raise AuthoredReplayError(
                f"bar {index} is {prices.index[-1]}, its decision was made for {state['stamps'][index]}")
        return decision_signal(state["decisions"][index], self.name)

    def on_prices(self, prices: pd.DataFrame) -> Optional[Signal]:
        raise AuthoredReplayError("the research proxy runs inside the backtester only")


# ``env`` is a RunEnvironment; it is not imported here because evaluation_jobs imports this module.
def run_authored_backtest(backtester: Backtester, env: Any, params: Mapping[str, float],
                          universe_accessor: Any) -> BacktestResult:
    proxy = AuthoredProxyStrategy(client=connect_sandbox(env.sandbox_socket), digest=env.authored_digest,
                                  class_name=env.class_name, params=params, bar_size=env.bar_size,
                                  expected_build=env.sandbox_image_digest)
    proxy.install(StrategyContext(
        name=env.class_name, bar_size=BarSize.parse_str(env.bar_size), conids=list(env.conids), universe=None,
        historical_days_prior=0, paper_only=False, storage=backtester.storage, universe_accessor=universe_accessor,
        logger=logging, module=env.strategy_file, class_name=env.class_name))
    proxy.state = StrategyState.RUNNING
    return backtester.run(proxy, list(env.conids))
