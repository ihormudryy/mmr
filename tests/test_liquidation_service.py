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


class _LedgerRow:
    def __init__(self, state): self.state = state


class _Ledger:
    def __init__(self): self.row = _LedgerRow("OUTCOME_UNKNOWN"); self.transitions = []
    def get(self, command_id): return self.row
    def transition_in_tx(self, _conn, command_id, before, after, **kwargs):
        self.transitions.append((command_id, before, after, kwargs)); self.row.state = after


class _Journal:
    def connect(self): return object()
    def mutate(self, conn, mutation, write, event_id): write(conn, 1)


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


def test_broker_flat_proof_resolves_pending_command_root():
    broker, dispatch, breaker = _Broker([_snapshot(1, [_position()]), _snapshot(2, [])]), _Dispatch(), _Breaker()
    ledger = _Ledger()
    service = LiquidationService(broker, dispatch, breaker=breaker, now=lambda: NOW,
                                 journal=_Journal(), ledger=ledger)
    service.start(ACCOUNT, "root-1", NOW + dt.timedelta(minutes=1))
    assert service.rescan().state == "FLAT"
    assert ledger.transitions[0][:3] == ("root-1", "OUTCOME_UNKNOWN", "RESOLVED")


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


class _FirstCaptureBlocks(_Broker):
    """The first capture waits for ``release``; later captures return at once."""
    def __init__(self, snapshots):
        super().__init__(snapshots)
        self.first_entered = __import__("threading").Event()
        self.release = __import__("threading").Event()

    def capture(self, account_id):
        if self.calls == 0:
            self.calls += 1
            self.first_entered.set()
            assert self.release.wait(5.0)
            return self.snapshots[0]
        return super().capture(account_id)


def _threaded_service(**kwargs):
    broker = _FirstCaptureBlocks([_snapshot(1, [_position()])])
    dispatch = _Dispatch()
    service = LiquidationService(broker, dispatch, breaker=_Breaker(), now=lambda: NOW, **kwargs)
    return service, broker, dispatch


def test_start_and_rescan_serialize_across_threads():
    import threading

    service, broker, dispatch = _threaded_service()
    results = {}
    a = threading.Thread(target=lambda: results.setdefault(
        "a", service.start(ACCOUNT, "root-1", NOW + dt.timedelta(minutes=1))))
    a.start()
    assert broker.first_entered.wait(2.0)
    b = threading.Thread(target=lambda: results.setdefault("b", service.rescan()))
    b.start()
    b.join(0.2)  # without serialization B reduces now, while A is still capturing
    broker.release.set()
    a.join(2.0)
    b.join(2.0)

    reduces = [call for call in dispatch.calls if call[0] == "reduce"]
    assert len(reduces) == 1, dispatch.calls
    assert results["a"].state == "VERIFYING"
    assert results["b"].state == "VERIFYING"
    assert results["b"].generation_id == 1


def test_busy_service_raises_liquidation_busy_instead_of_blocking():
    import threading
    import time
    from trader.trading.liquidation_service import LiquidationBusy

    service, broker, dispatch = _threaded_service(lock_timeout_seconds=0.1)
    a = threading.Thread(target=lambda: service.start(ACCOUNT, "root-1", NOW + dt.timedelta(minutes=1)))
    a.start()
    assert broker.first_entered.wait(2.0)
    started = time.monotonic()
    try:
        with pytest.raises(LiquidationBusy):
            service.rescan()
        with pytest.raises(LiquidationBusy):
            service.start(ACCOUNT, "root-2", NOW + dt.timedelta(minutes=1))
        assert time.monotonic() - started < 1.0
        assert issubclass(LiquidationBusy, RuntimeError)
    finally:
        broker.release.set()
        a.join(2.0)
    assert [call[0] for call in dispatch.calls] == ["reduce"]


def _hold_lock(service):
    """Hold the service lock from another thread until ``release`` is set."""
    import threading
    held, release = threading.Event(), threading.Event()

    def hold():
        with service._lock:
            held.set()
            release.wait(5.0)

    holder = threading.Thread(target=hold)
    holder.start()
    assert held.wait(2.0)
    return holder, release


def test_busy_start_still_registers_root_for_a_later_rescan():
    from trader.trading.liquidation_service import LiquidationBusy

    saved = []
    store = type("_Store", (), {"load_unresolved": lambda self: [],
                                "save": lambda self, receipt, now: saved.append(receipt)})()
    service = LiquidationService(_Broker([_snapshot(1, [_position()])]), _Dispatch(), breaker=_Breaker(),
                                 now=lambda: NOW, store=store, lock_timeout_seconds=0.05)
    dispatch = service._dispatch
    holder, release = _hold_lock(service)
    try:
        with pytest.raises(LiquidationBusy):
            service.start(ACCOUNT, "saga-root", NOW + dt.timedelta(minutes=1))
        assert dispatch.calls == []
    finally:
        release.set()
        holder.join(2.0)

    assert [r.state for r in saved] == ["REQUESTED"]
    receipt = service.rescan()
    assert receipt.cause_command_id == "saga-root"
    assert receipt.state == "VERIFYING"
    assert [call[0] for call in dispatch.calls] == ["reduce"]


def test_root_bound_to_one_account_rejects_another_account():
    service, _dispatch, _ = _service([_snapshot(1, [_position()])])
    service.start(ACCOUNT, "root-1", NOW + dt.timedelta(minutes=1))
    with pytest.raises(ValueError):
        service.start("DU999", "root-1", NOW + dt.timedelta(minutes=1))


def test_busy_flatten_records_pending_root_and_surfaces_busy():
    from types import SimpleNamespace
    from trader.trading.liquidation_service import LiquidationBusy

    ledger = _Ledger()
    ledger.row = _LedgerRow("RECEIVED")
    service = LiquidationService(_Broker([_snapshot(1, [_position()]), _snapshot(2, [])]), _Dispatch(),
                                 breaker=_Breaker(), now=lambda: NOW, journal=_Journal(), ledger=ledger,
                                 lock_timeout_seconds=0.05)
    holder, release = _hold_lock(service)
    try:
        with pytest.raises(LiquidationBusy):
            service.liquidate(SimpleNamespace(account_id=ACCOUNT, command_id="flatten-1"))
    finally:
        release.set()
        holder.join(2.0)

    assert ledger.transitions[0][:3] == ("flatten-1", "RECEIVED", "OUTCOME_UNKNOWN")
    assert ledger.transitions[0][3]["outcome"]["liquidation_state"] == "REQUESTED"
    assert service.rescan().state == "VERIFYING"
    assert service.rescan().state == "FLAT"
    assert ledger.transitions[-1][:3] == ("flatten-1", "OUTCOME_UNKNOWN", "RESOLVED")


def _store_with(*receipts):
    return type("_Store", (), {"load_unresolved": lambda self: list(receipts),
                               "save": lambda self, receipt, now: None})()


def test_rescan_skips_failed_safe_root_and_advances_a_busy_registered_root():
    from trader.trading.liquidation_service import LiquidationBusy, LiquidationReceipt

    old = LiquidationReceipt(ACCOUNT, "old-root", "FAILED_SAFE", NOW)
    service = LiquidationService(_Broker([_snapshot(1, [_position()])]), _Dispatch(), breaker=_Breaker(),
                                 now=lambda: NOW, store=_store_with(old), lock_timeout_seconds=0.05)
    dispatch = service._dispatch
    holder, release = _hold_lock(service)
    try:
        with pytest.raises(LiquidationBusy):
            service.start(ACCOUNT, "saga-root", NOW + dt.timedelta(minutes=1))
    finally:
        release.set()
        holder.join(2.0)

    receipt = service.rescan()
    assert receipt.cause_command_id == "saga-root"
    assert receipt.state == "VERIFYING"
    assert [call[0] for call in dispatch.calls] == ["reduce"]


def test_rescan_returns_none_when_only_failed_safe_roots_remain():
    from trader.trading.liquidation_service import LiquidationReceipt

    old = LiquidationReceipt(ACCOUNT, "old-root", "FAILED_SAFE", NOW)
    broker = _Broker([_snapshot(1, [_position()])])
    service = LiquidationService(broker, _Dispatch(), now=lambda: NOW, store=_store_with(old))
    assert service.rescan() is None
    assert broker.calls == 0
