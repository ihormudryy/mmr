import datetime as dt

import pytest

from trader.data.broker_state import BrokerOrderRow, BrokerPositionRow, BrokerRiskSnapshot
from trader.trading.liquidation_service import LiquidationService


UTC = dt.timezone.utc
NOW = dt.datetime(2026, 7, 18, 14, 30, tzinfo=UTC)
ACCOUNT = "DU123"


def _position(quantity=10.0):
    return BrokerPositionRow(
        account_id=ACCOUNT, conid=1, symbol="AAPL", sec_type="STK", exchange="SMART",
        currency="USD", quantity=quantity, average_cost=None, market_price=None,
        market_value=None, unrealized_pnl=None, realized_pnl=None, daily_pnl=None,
        deleted=False, revision=1, source_timestamp=NOW,
    )


def _order(entity="external-1", group=None):
    return BrokerOrderRow(
        order_entity_id=entity, account_id=ACCOUNT, conid=1, symbol="AAPL",
        order_group_id=group, leg=None, is_external=True, action="BUY", order_type="LMT",
        total_quantity=10, filled_quantity=0, avg_fill_price=None, limit_price=100,
        stop_price=None, tif="DAY", status="Submitted", deleted=False, revision=1,
        source_timestamp=NOW,
    )


def _snapshot(generation, positions=(), working=()):
    return BrokerRiskSnapshot(
        generation_id=generation, source_cursor=generation, promoted_at=NOW,
        account_id=ACCOUNT, account_mode="paper", net_liquidation=100_000,
        daily_pnl=0, positions=tuple(positions), working_orders=tuple(working),
    )


class _Broker:
    def __init__(self, snapshots):
        self.snapshots = list(snapshots)
        self.calls = 0

    def capture(self, account_id):
        self.calls += 1
        value = self.snapshots.pop(0) if len(self.snapshots) > 1 else self.snapshots[0]
        if isinstance(value, Exception):
            raise value
        return value


class _Dispatch:
    def __init__(self):
        self.calls = []

    def cancel(self, order, command_id):
        self.calls.append(("cancel", order.order_entity_id, command_id))

    def reduce(self, position, side, quantity, command_id):
        self.calls.append(("reduce", position.conid, side, quantity, command_id))


class _Breaker:
    def __init__(self): self.calls = []
    def trip_liquidation(self, cause, detail): self.calls.append((cause, detail))


def _service(snapshots, *, now=NOW):
    dispatch, breaker = _Dispatch(), _Breaker()
    return LiquidationService(_Broker(snapshots), dispatch, breaker=breaker, now=lambda: now), dispatch, breaker


def test_cancels_external_orders_before_submitting_any_reduction():
    service, dispatch, _ = _service([
        _snapshot(1, [_position()], [_order()]),
        _snapshot(2, [_position()]),
    ])
    receipt = service.start(ACCOUNT, "root-1", NOW + dt.timedelta(minutes=1))
    assert receipt.state == "VERIFYING"
    receipt = service.rescan()
    assert receipt.state == "VERIFYING"
    assert [call[0] for call in dispatch.calls] == ["cancel", "reduce"]
    assert dispatch.calls[1][2:4] == ("SELL", 10.0)


def test_submitted_reduction_is_not_treated_as_flat_without_new_broker_snapshot():
    service, dispatch, breaker = _service([_snapshot(1, [_position()])])
    receipt = service.start(ACCOUNT, "root-1", NOW + dt.timedelta(minutes=1))
    assert receipt.state == "VERIFYING"
    assert len(dispatch.calls) == 1
    assert breaker.calls


def test_flat_requires_newer_generation_after_actions():
    service, _dispatch, _ = _service([
        _snapshot(1, [_position()]), _snapshot(2, []),
    ])
    assert service.start(ACCOUNT, "root-1", NOW + dt.timedelta(minutes=1)).state == "VERIFYING"
    assert service.rescan().state == "FLAT"


def test_disconnect_is_outcome_unknown_and_keeps_breaker_tripped():
    service, dispatch, breaker = _service([RuntimeError("IB down")])
    receipt = service.start(ACCOUNT, "root-1", NOW + dt.timedelta(minutes=1))
    assert receipt.state == "OUTCOME_UNKNOWN"
    assert not dispatch.calls
    assert breaker.calls


def test_timeout_never_claims_flat_or_submits_after_deadline():
    service, dispatch, breaker = _service([_snapshot(1, [_position()])], now=NOW)
    receipt = service.start(ACCOUNT, "root-1", NOW)
    assert receipt.state == "FAILED_SAFE"
    assert not dispatch.calls
    assert breaker.calls


def test_repeated_root_is_idempotent_while_waiting_for_broker_resolution():
    service, dispatch, _ = _service([_snapshot(1, [_position()])])
    service.start(ACCOUNT, "root-1", NOW + dt.timedelta(minutes=1))
    service.start(ACCOUNT, "root-1", NOW + dt.timedelta(minutes=1))
    assert [call[0] for call in dispatch.calls] == ["reduce"]


@pytest.mark.parametrize(("quantity", "side"), [(10.0, "SELL"), (-7.0, "BUY")])
def test_reduction_never_flips_position(quantity, side):
    service, dispatch, _ = _service([_snapshot(1, [_position(quantity)])])
    service.start(ACCOUNT, "root-1", NOW + dt.timedelta(minutes=1))
    _kind, _conid, actual_side, actual_quantity, _child = dispatch.calls[0]
    assert actual_side == side
    assert actual_quantity == abs(quantity)
