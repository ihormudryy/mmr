"""Soft-load attested strategies when paper automation is disabled.

When ``automation.enabled`` is false, strategies that declare
``artifact_bundle_path`` must still load (so they appear in Strategies /
Scaling for Activate). When automation is enabled, verification stays
fail-closed.
"""
from __future__ import annotations

import logging
import os
from unittest.mock import MagicMock

import pytest

from trader.automation.artifact_verifier import ArtifactVerifierError
from trader.data.data_access import TickStorage
from trader.data.universe import UniverseAccessor


def _make_runtime(tmp_path, tmp_duckdb_path, *, automation_enabled: bool):
    from trader.strategy.strategy_runtime import StrategyRuntime

    rt = object.__new__(StrategyRuntime)
    rt.strategy_implementations = []
    rt.strategies = {}
    rt.streams = {}
    rt.strategies_directory = str(tmp_path / "strategies")
    rt.strategy_config_file = str(tmp_path / "strategy_runtime.yaml")
    rt.duckdb_path = tmp_duckdb_path
    rt.universe_library = "Universes"
    rt.storage = TickStorage(duckdb_path=tmp_duckdb_path)
    rt.universe_accessor = UniverseAccessor.__new__(UniverseAccessor)
    rt.universe_accessor.duckdb_path = tmp_duckdb_path
    rt.universe_accessor.universe_library = "Universes"
    rt.paper_trading = True
    rt.automation_enabled = automation_enabled
    rt.automation_live_enabled = False
    rt.automation_expected_artifact_id = ""
    rt.automation_public_key_ring_path = ""
    rt._artifact_verifier = None
    os.makedirs(rt.strategies_directory, exist_ok=True)
    return rt


def _write_strategy(directory: str, name: str = "vwap_reclaim_cat") -> str:
    path = os.path.join(directory, f"{name}.py")
    with open(path, "w") as f:
        f.write(
            "from trader.trading.strategy import Strategy\n\n"
            "class VwapReclaimCat(Strategy):\n"
            "    def on_prices(self, prices):\n"
            "        return None\n"
        )
    return path


def test_soft_load_when_automation_disabled(tmp_path, tmp_duckdb_path, caplog):
    rt = _make_runtime(tmp_path, tmp_duckdb_path, automation_enabled=False)
    path = _write_strategy(rt.strategies_directory)
    rt._verify_artifact_at_load = MagicMock(
        side_effect=AssertionError("verify must not run when automation is off")
    )

    with caplog.at_level(logging.WARNING):
        rt.load_strategy(
            name="vwap_reclaim_cat",
            bar_size_str="1 min",
            conids=[265598],
            universe=None,
            historical_days_prior=5,
            module=path,
            class_name="VwapReclaimCat",
            description="attested candidate",
            params={"artifact_bundle_path": "~/artifacts/deadbeef"},
        )

    assert len(rt.strategy_implementations) == 1
    assert rt.strategy_implementations[0].name == "vwap_reclaim_cat"
    rt._verify_artifact_at_load.assert_not_called()
    assert any(
        "loading without attestation" in r.message for r in caplog.records
    )


def test_refuse_when_automation_enabled_and_verify_fails(tmp_path, tmp_duckdb_path):
    rt = _make_runtime(tmp_path, tmp_duckdb_path, automation_enabled=True)
    path = _write_strategy(rt.strategies_directory)
    rt._verify_artifact_at_load = MagicMock(
        side_effect=ArtifactVerifierError(
            "strategy 'vwap_reclaim_cat' specifies artifact_bundle_path but "
            "automation is not enabled or public_key_ring_path is not configured"
        )
    )

    rt.load_strategy(
        name="vwap_reclaim_cat",
        bar_size_str="1 min",
        conids=[265598],
        universe=None,
        historical_days_prior=5,
        module=path,
        class_name="VwapReclaimCat",
        description="attested candidate",
        params={"artifact_bundle_path": "~/artifacts/deadbeef"},
    )

    assert rt.strategy_implementations == []
    rt._verify_artifact_at_load.assert_called_once()


def test_verify_runs_when_automation_enabled(tmp_path, tmp_duckdb_path):
    rt = _make_runtime(tmp_path, tmp_duckdb_path, automation_enabled=True)
    path = _write_strategy(rt.strategies_directory)
    rt._verify_artifact_at_load = MagicMock()

    rt.load_strategy(
        name="vwap_reclaim_cat",
        bar_size_str="1 min",
        conids=[265598],
        universe=None,
        historical_days_prior=5,
        module=path,
        class_name="VwapReclaimCat",
        description="attested candidate",
        params={"artifact_bundle_path": "~/artifacts/deadbeef"},
    )

    assert len(rt.strategy_implementations) == 1
    rt._verify_artifact_at_load.assert_called_once_with(
        "vwap_reclaim_cat", "~/artifacts/deadbeef"
    )
