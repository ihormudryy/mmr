"""Live paper-stack coverage for strategy controls and the params editor."""
from __future__ import annotations

from uuid import uuid4

import pytest


def _strategy_rows(dashboard_client) -> list[dict]:
    response = dashboard_client.get("/api/snapshot")
    assert response.status_code == 200, (
        f"strategy snapshot failed: {response.status_code} {response.text}"
    )
    strategies = response.json().get("strategies")
    assert isinstance(strategies, list), "snapshot strategies must be a list"
    if not strategies:
        pytest.skip("no deployed strategies")
    return strategies


def _strategy_name(row: dict) -> str:
    name = row.get("strategy_name") or row.get("name")
    assert isinstance(name, str) and name, f"strategy row has no name: {row}"
    return name


def _control_revision(row: dict) -> int:
    revision = row.get("control_revision")
    assert isinstance(revision, int) and revision >= 0, (
        f"strategy row has no control revision: {row}"
    )
    return revision


def _command_headers(dashboard_client, paper_stack) -> dict[str, str]:
    return {
        **dashboard_client.csrf_headers(),
        "Origin": paper_stack.dashboard_url,
    }


def _row_for_strategy(dashboard_client, strategy_name: str) -> dict:
    return next(
        (
            row
            for row in _strategy_rows(dashboard_client)
            if _strategy_name(row) == strategy_name
        ),
        {},
    )


def _assert_runtime_row_fields(row: dict) -> None:
    assert row.get("class_name"), f"strategy class is absent: {row}"
    assert row.get("bar_size"), f"strategy bar size is absent: {row}"
    assert row.get("conids") or row.get("universe"), (
        f"strategy has neither conids nor universe: {row}"
    )
    assert row.get("strategy_state") or row.get("state"), (
        f"strategy state is absent: {row}"
    )


def test_strategy_enable_disable_and_runtime_panel(
    paper_stack, dashboard_client, typed_rpc, require_capability,
):
    """Disable then restore a deployed strategy through the command API."""
    require_capability("strategy_control")
    assert typed_rpc is paper_stack.typed

    login = dashboard_client.login()
    assert login.status_code == 200, f"login failed: {login.status_code} {login.text}"
    strategy_name = _strategy_name(_strategy_rows(dashboard_client)[0])
    _assert_runtime_row_fields(_row_for_strategy(dashboard_client, strategy_name))

    disabled = False
    try:
        disable = dashboard_client.post(
            f"/api/commands/strategies/{strategy_name}/disable",
            headers=_command_headers(dashboard_client, paper_stack),
            json={
                "command_id": str(uuid4()),
                "expected_version": _control_revision(
                    _row_for_strategy(dashboard_client, strategy_name)
                ),
            },
        )
        assert disable.status_code == 202, (
            f"disable strategy failed: {disable.status_code} {disable.text}"
        )
        disabled = True

        enable = dashboard_client.post(
            f"/api/commands/strategies/{strategy_name}/enable",
            headers=_command_headers(dashboard_client, paper_stack),
            json={
                "command_id": str(uuid4()),
                "expected_version": _control_revision(
                    _row_for_strategy(dashboard_client, strategy_name)
                ),
            },
        )
        assert enable.status_code == 202, (
            f"enable strategy failed: {enable.status_code} {enable.text}"
        )
        disabled = False

        row = _row_for_strategy(dashboard_client, strategy_name)
        assert row, f"strategy {strategy_name!r} disappeared from snapshot"
        _assert_runtime_row_fields(row)
    finally:
        if disabled:
            row = _row_for_strategy(dashboard_client, strategy_name)
            if row:
                restore = dashboard_client.post(
                    f"/api/commands/strategies/{strategy_name}/enable",
                    headers=_command_headers(dashboard_client, paper_stack),
                    json={
                        "command_id": str(uuid4()),
                        "expected_version": _control_revision(row),
                    },
                )
                assert restore.status_code == 202, (
                    "failed to restore strategy enable state: "
                    f"{restore.status_code} {restore.text}"
                )


def test_strategy_params_round_trip(
    paper_stack, dashboard_client, typed_rpc, require_capability,
):
    """Save current deployed params and read the same values back."""
    require_capability("strategy_control")
    assert typed_rpc is paper_stack.typed

    login = dashboard_client.login()
    assert login.status_code == 200, f"login failed: {login.status_code} {login.text}"
    strategy_name = _strategy_name(_strategy_rows(dashboard_client)[0])

    params_response = dashboard_client.get(f"/api/strategies/{strategy_name}/params")
    assert params_response.status_code == 200, (
        f"strategy params failed: {params_response.status_code} {params_response.text}"
    )
    params_editor = params_response.json()
    assert params_editor.get("strategy_name") == strategy_name
    assert params_editor.get("class_name")
    assert isinstance(params_editor.get("params"), dict)
    assert isinstance(params_editor.get("tunables"), dict)
    params = params_editor["params"]
    if not params:
        pytest.skip(f"strategy {strategy_name!r} has no configured params to round-trip")

    update = dashboard_client.post(
        f"/api/commands/strategies/{strategy_name}/params",
        headers=_command_headers(dashboard_client, paper_stack),
        json={
            "command_id": str(uuid4()),
            "expected_version": _control_revision(
                _row_for_strategy(dashboard_client, strategy_name)
            ),
            "params": params,
        },
    )
    assert update.status_code == 202, (
        f"update strategy params failed: {update.status_code} {update.text}"
    )

    after = dashboard_client.get(f"/api/strategies/{strategy_name}/params")
    assert after.status_code == 200, (
        f"strategy params after update failed: {after.status_code} {after.text}"
    )
    assert after.json().get("params") == params
