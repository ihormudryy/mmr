"""Tests only: a strategy service node that follows a served trader's active AI deployments."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pandas as pd

from tests.test_strategy_artifact_soft_load import _make_runtime
from trader.data.duckdb_store import DuckDBConnection
from trader.data.strategy_signal_record import StrategySignalRecord
from trader.messaging.ai_deployment_wire import GetActiveAiDeploymentsResponse
from trader.strategy.ai_deployment_source import AiDeploymentSource


class StrategyNode:
    """The real runtime and second source, wired to a served trader over signed RPC."""

    def __init__(self, served: Any, *, strategies_dir: Path):
        duckdb_path = served.trader.duckdb_path
        self.runtime = _make_runtime(strategies_dir.parent, duckdb_path, automation_enabled=False)
        self.runtime.strategies_directory = str(strategies_dir)
        self.runtime._load_enabled = lambda name: None
        self.runtime._last_dispatched_bar = {}
        self.runtime.signal_record = StrategySignalRecord(DuckDBConnection.get_instance(duckdb_path))
        self.runtime.event_store = SimpleNamespace(append=lambda event: None)
        self.runtime.zmq_messagebus_client = SimpleNamespace(write=lambda *args: None)
        self.runtime._load_ai_history = lambda instance: None     # no IB in a node test
        strategy_query = served.client("strategy", "query")
        self._source = AiDeploymentSource(
            runtime=self.runtime, paper=True,
            read_active=lambda: strategy_query.call(
                "get_active_ai_deployments", {}, GetActiveAiDeploymentsResponse).deployments)

    def reconcile(self) -> None:
        self._source.reconcile()

    def instances(self) -> dict[str, str]:
        """Loaded AI instances: version digest to instance name."""
        return {s.ai_deployment_version: s.name for s in self.runtime.ai_instances().values()}

    def feed_bar(self, conid: int, frame: pd.DataFrame) -> None:
        for instance in self.runtime.ai_instances().values():
            if conid not in instance.conids:
                continue
            signal = instance.on_prices(frame)
            if signal is not None:
                self.runtime._dispatch_once(instance, signal, conid, frame)
