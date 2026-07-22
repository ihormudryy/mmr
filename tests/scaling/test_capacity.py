"""P5 Task 5 — capacity and execution-quality monitoring."""
from __future__ import annotations

from decimal import Decimal

import pytest

from trader.automation.attribution import (
    CapacityAttributionWindow,
    PromotionAttributionReport,
    TradeAttribution,
    export_capacity_window,
)
from trader.promotion.capacity import (
    BLOCK_ADV_PARTICIPATION,
    BLOCK_HIGH_SLIPPAGE,
    BLOCK_LOW_FILL_PROBABILITY,
    BLOCK_MISSING_DEPTH,
    BLOCK_SPARSE_EVIDENCE,
    CapacityMonitor,
)

INSTRUMENT = "265598"
ADV = 1_000_000.0


def _record(
    idx: int,
    *,
    requested: float = 100.0,
    filled: float = 100.0,
    slippage_bps: float = 3.0,
    spread_bps: float = 5.0,
    depth_available: bool = True,
    instrument_id: str = INSTRUMENT,
    adv: float | None = None,
) -> dict:
    return {
        "trade_id": f"t-{idx}",
        "instrument_id": instrument_id,
        "requested_quantity": requested,
        "filled_quantity": filled,
        "slippage_bps": slippage_bps,
        "spread_bps": spread_bps,
        "depth_available": depth_available,
        "adv": adv,
    }


def _window(*records, sparse: bool = False) -> CapacityAttributionWindow:
    return CapacityAttributionWindow(records=tuple(records), sparse=sparse)


def _good_records(n: int = 6):
    return [_record(i) for i in range(n)]


def test_capacity_passes_with_sufficient_clean_evidence():
    decision = CapacityMonitor().evaluate(
        _window(*_good_records()),
        {INSTRUMENT: 1000.0},
        adv_by_instrument={INSTRUMENT: ADV},
    )
    assert decision.passed is True
    assert decision.metrics["fill_probability"] == pytest.approx(1.0)
    assert decision.metrics["partial_fill_rate"] == pytest.approx(0.0)
    assert decision.metrics["slippage_bps"] == pytest.approx(3.0)
    assert decision.metrics["spread_bps"] == pytest.approx(5.0)


def test_capacity_rejects_sparse_evidence():
    decision = CapacityMonitor().evaluate(
        _window(*_good_records(3), sparse=True),
        {INSTRUMENT: 100.0},
        adv_by_instrument={INSTRUMENT: ADV},
    )
    assert decision.passed is False
    assert BLOCK_SPARSE_EVIDENCE in decision.blockers


def test_capacity_fails_closed_on_missing_depth():
    records = [_record(i, depth_available=False) for i in range(6)]
    decision = CapacityMonitor().evaluate(
        _window(*records),
        {INSTRUMENT: 100.0},
        adv_by_instrument={INSTRUMENT: ADV},
    )
    assert decision.passed is False
    assert BLOCK_MISSING_DEPTH in decision.blockers


def test_capacity_rejects_adv_participation_breach():
    # 3000 / 1_000_000 = 0.30% > 0.25%
    decision = CapacityMonitor().evaluate(
        _window(*_good_records()),
        {INSTRUMENT: 3000.0},
        adv_by_instrument={INSTRUMENT: ADV},
    )
    assert decision.passed is False
    assert BLOCK_ADV_PARTICIPATION in decision.blockers
    assert decision.projected_adv_participation == pytest.approx(0.003)


def test_capacity_rejects_low_fill_probability():
    records = [_record(i, filled=50.0) for i in range(6)]
    decision = CapacityMonitor().evaluate(
        _window(*records),
        {INSTRUMENT: 100.0},
        adv_by_instrument={INSTRUMENT: ADV},
    )
    assert decision.passed is False
    assert BLOCK_LOW_FILL_PROBABILITY in decision.blockers


def test_capacity_rejects_high_slippage():
    records = [_record(i, slippage_bps=25.0) for i in range(6)]
    decision = CapacityMonitor().evaluate(
        _window(*records),
        {INSTRUMENT: 100.0},
        adv_by_instrument={INSTRUMENT: ADV},
    )
    assert decision.passed is False
    assert BLOCK_HIGH_SLIPPAGE in decision.blockers


def test_export_capacity_window_from_promotion_report():
    attr = TradeAttribution(
        trade_id="trade-1",
        resolved=True,
        fills=({"exec_id": "1", "leg": "entry", "side": "BUY", "quantity": "100", "price": "150"},),
        exit_fills=({"exec_id": "2", "leg": "exit", "side": "SELL", "quantity": "100", "price": "155"},),
        spread_bps=Decimal("4.5"),
        slippage_bps=Decimal("2.0"),
        gross_pnl=Decimal("500"),
        net_pnl=Decimal("495"),
    )
    report = PromotionAttributionReport(resolved=(attr,), unresolved=())
    window = export_capacity_window(
        report,
        instrument_by_trade={"trade-1": INSTRUMENT},
        adv_by_instrument={INSTRUMENT: ADV},
    )
    assert len(window.records) == 1
    assert window.records[0].instrument_id == INSTRUMENT
    assert window.records[0].filled_quantity == pytest.approx(100.0)
    assert window.records[0].spread_bps == pytest.approx(4.5)
    assert window.sparse is True  # below default min_records=5
