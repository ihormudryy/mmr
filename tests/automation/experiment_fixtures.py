"""Shared builders for the Plan 4 experiment tests."""
from __future__ import annotations

import datetime as dt

from trader.automation.experiments import ExperimentRecord, experiment_id_for

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 7, 17, 15, 0, tzinfo=UTC)
ACCOUNT = "DU1"


def armed_record(account: str = ACCOUNT, **changes) -> ExperimentRecord:
    command_id = changes.pop("start_command_id", "start-1")
    fields = dict(
        experiment_id=experiment_id_for(account, command_id), account_id=account, started_at=NOW,
        start_net_liquidation=100_000.0, base_currency="USD", start_usd_per_base=1.0, state="ARMED",
        killed_at=None, start_command_id=command_id, start_generation_id=7, start_fx_source="base_is_usd",
        config_digest="d" * 64, styles=("intraday_long",), kill_drawdown_pct=20.0, kill_basis="start",
        revision=1, peak_net_liquidation=100_000.0, kill_anchor_net_liquidation=100_000.0)
    fields.update(changes)
    return ExperimentRecord(**fields)
