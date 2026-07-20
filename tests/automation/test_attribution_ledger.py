"""P3 Task 7 — authoritative attribution ledger.

Contract (plan §Task 7):
* Joins artifact/dataset/signal/intent/context/policy/command/order/fill/
  commission/position plus rejection/breaker/operator actions.
* Out-of-order fills/commissions, corrections, partial fills, duplicate exec
  IDs, multiple exits, MFE/MAE, gross/net P&L, spread/slippage/latency.
* Crash between raw append and derived aggregation still rebuilds correctly.
* Raw evidence is append-only; derived rows rebuild deterministically.
* Promotion queries exclude unresolved trades and report them explicitly.
* Domain events for dashboard observability; dashboard is never authoritative.
* Migrations 32-34: automation_decisions, trade_attribution,
  execution_cost_attribution, operator_action_refs.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest

from trader.data.broker_state import BrokerStateStore
from trader.data.domain_journal import DomainJournal
from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator
from trader.trading.order_correlation import encode_order_ref

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 7, 18, 14, 30, tzinfo=UTC)
ACCOUNT = "DU111111"
CONID = 265598
TRADE_ID = "cmd-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
ORDER_GROUP = f"og-{TRADE_ID}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _db(tmp_path: Path, name: str = "attr.duckdb"):
    db = DuckDBConnection.get_instance(str(tmp_path / name))
    migrator = SchemaMigrator(db)
    journal = DomainJournal(db)
    journal.migrate(migrator)
    return db, migrator, journal


def _ledger(tmp_path: Path, **overrides):
    from trader.automation.attribution import AttributionLedger
    from trader.data.attribution_store import apply_attribution_migrations

    db, migrator, journal = _db(tmp_path)
    apply_attribution_migrations(migrator)
    ledger = AttributionLedger(
        journal=journal,
        db=db,
        account_id=ACCOUNT,
        now=overrides.pop("now", lambda: NOW),
        **overrides,
    )
    return ledger, journal, db, migrator


def _evidence(
    kind: str,
    *,
    trade_id: str = TRADE_ID,
    key: Optional[str] = None,
    ts: Optional[dt.datetime] = None,
    **payload,
):
    from trader.automation.attribution import AttributionEvidenceEvent

    evidence_key = key or f"{kind}:{trade_id}:{payload.get('exec_id') or payload.get('decision_id') or payload.get('ref') or kind}"
    return AttributionEvidenceEvent(
        evidence_key=evidence_key,
        trade_id=trade_id,
        event_kind=kind,
        payload=payload,
        source_timestamp=ts or NOW,
    )


def _full_chain_events(*, with_exit: bool = True, with_commission: bool = True):
    """Minimal join chain covering every required evidence kind."""
    t0 = NOW
    t1 = NOW + dt.timedelta(seconds=1)
    t2 = NOW + dt.timedelta(seconds=2)
    t3 = NOW + dt.timedelta(seconds=3)
    events = [
        _evidence("artifact", artifact_id="artifact-1", dataset_id="dataset-1",
                  key="artifact:artifact-1", ts=t0),
        _evidence("dataset", dataset_id="dataset-1", key="dataset:dataset-1", ts=t0),
        _evidence("signal", signal_id="signal-1", key="signal:signal-1", ts=t0),
        _evidence("intent", intent_id="intent-1", command_id=TRADE_ID,
                  key="intent:intent-1", ts=t0),
        _evidence("context", approval_context_id="ctx-1",
                  expected_entry=160.0, expected_exit=200.0,
                  key="context:ctx-1", ts=t0),
        _evidence("policy", decision_id="pol-1", policy="session_risk",
                  outcome="ALLOW", key="policy:pol-1", ts=t0),
        _evidence("command", command_id=TRADE_ID, order_group_id=ORDER_GROUP,
                  side="BUY", quantity=10.0, key=f"command:{TRADE_ID}", ts=t0),
        _evidence("order", order_entity_id="ord-entry", leg="entry",
                  order_group_id=ORDER_GROUP, status="Filled",
                  key="order:ord-entry:Filled", ts=t1),
        _evidence("fill", exec_id="ex-entry-1", leg="entry", side="BOT",
                  quantity=10.0, price=160.05, order_entity_id="ord-entry",
                  key="fill:ex-entry-1", ts=t1),
    ]
    if with_commission:
        events.append(
            _evidence("commission", exec_id="ex-entry-1", commission=1.25,
                      currency="USD", key="commission:ex-entry-1", ts=t1)
        )
    events.append(
        _evidence("position", conid=CONID, quantity=10.0, average_cost=160.05,
                  key="position:open", ts=t1)
    )
    if with_exit:
        events.extend([
            _evidence("order", order_entity_id="ord-stop", leg="stop",
                      order_group_id=ORDER_GROUP, status="Filled",
                      key="order:ord-stop:Filled", ts=t2),
            _evidence("fill", exec_id="ex-exit-1", leg="stop", side="SLD",
                      quantity=10.0, price=150.0, order_entity_id="ord-stop",
                      key="fill:ex-exit-1", ts=t2),
            _evidence("commission", exec_id="ex-exit-1", commission=1.25,
                      currency="USD", key="commission:ex-exit-1", ts=t2),
            _evidence("position", conid=CONID, quantity=0.0,
                      key="position:flat", ts=t3),
            _evidence("mfe_mae_sample", price=165.0, key="mfe:165", ts=t2),
            _evidence("mfe_mae_sample", price=148.0, key="mae:148", ts=t2),
            _evidence("mfe_mae_sample", price=162.0, key="mfe:162", ts=t2),
        ])
    return events


# ---------------------------------------------------------------------------
# Migrations 32-34
# ---------------------------------------------------------------------------

def test_migrations_32_34_create_append_only_tables(tmp_path):
    from trader.data.attribution_store import (
        ATTRIBUTION_MIGRATION_VERSIONS,
        apply_attribution_migrations,
    )

    db, migrator, _ = _db(tmp_path, "mig.duckdb")
    assert apply_attribution_migrations(migrator) is True
    assert ATTRIBUTION_MIGRATION_VERSIONS == (32, 33, 34)
    assert apply_attribution_migrations(migrator) is False

    for table in (
        "automation_decisions",
        "trade_attribution",
        "execution_cost_attribution",
        "operator_action_refs",
    ):
        cols = {
            row[1]
            for row in db.execute(f"PRAGMA table_info('{table}')", fetch="all")
        }
        assert cols, f"missing table {table}"
        assert "evidence_key" in cols or "trade_id" in cols


# ---------------------------------------------------------------------------
# Join across the full chain
# ---------------------------------------------------------------------------

def test_rebuild_joins_full_evidence_chain(tmp_path):
    ledger, *_ = _ledger(tmp_path)
    for ev in _full_chain_events():
        assert ledger.append(ev) is True

    attr = ledger.rebuild_trade(TRADE_ID)
    assert attr.trade_id == TRADE_ID
    assert attr.resolved is True
    assert attr.artifact_id == "artifact-1"
    assert attr.dataset_id == "dataset-1"
    assert attr.signal_id == "signal-1"
    assert attr.intent_id == "intent-1"
    assert attr.command_id == TRADE_ID
    assert attr.order_group_id == ORDER_GROUP
    assert attr.approval_context_id == "ctx-1"
    assert len(attr.policy_decisions) == 1
    assert attr.policy_decisions[0]["outcome"] == "ALLOW"
    assert len(attr.fills) == 2
    assert attr.gross_pnl is not None
    assert attr.net_pnl is not None
    assert attr.net_pnl < attr.gross_pnl  # commissions deducted
    assert attr.mfe is not None and attr.mae is not None
    assert attr.mfe == Decimal("165.0")  # max price sample vs entry
    assert attr.mae == Decimal("148.0")  # min price sample


def test_joins_rejection_breaker_and_operator_actions(tmp_path):
    ledger, *_ = _ledger(tmp_path)
    for ev in _full_chain_events(with_exit=False):
        ledger.append(ev)

    ledger.append(_evidence(
        "rejection", rejection_id="rej-1", reason="LIQUIDITY",
        key="rejection:rej-1",
    ))
    ledger.append(_evidence(
        "breaker", incident_id="inc-1", reason_code="PROTECTIVE_ORDER_FAILURE",
        key="breaker:inc-1",
    ))
    ledger.append(_evidence(
        "operator", action_id="op-1", action="PAUSE", actor="alice",
        key="operator:op-1",
    ))

    attr = ledger.rebuild_trade(TRADE_ID)
    assert len(attr.rejection_refs) == 1
    assert attr.rejection_refs[0]["reason"] == "LIQUIDITY"
    assert len(attr.breaker_refs) == 1
    assert len(attr.operator_actions) == 1
    assert attr.operator_actions[0]["actor"] == "alice"

    # operator_action_refs table also populated
    rows = ledger.db.execute(
        "SELECT evidence_key FROM operator_action_refs WHERE trade_id = ?",
        [TRADE_ID], fetch="all",
    )
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# Ordering / corrections / duplicates / partials / multi-exit
# ---------------------------------------------------------------------------

def test_out_of_order_commission_before_fill(tmp_path):
    ledger, *_ = _ledger(tmp_path)
    for ev in _full_chain_events(with_exit=False, with_commission=False):
        ledger.append(ev)

    # Commission arrives before its fill is known to rebuild — still appends.
    ledger.append(_evidence(
        "commission", exec_id="ex-late-1", commission=2.0, currency="USD",
        key="commission:ex-late-1",
    ))
    ledger.append(_evidence(
        "fill", exec_id="ex-late-1", leg="entry", side="BOT",
        quantity=5.0, price=161.0, order_entity_id="ord-entry",
        key="fill:ex-late-1", ts=NOW + dt.timedelta(seconds=1),
    ))

    attr = ledger.rebuild_trade(TRADE_ID)
    fill_ids = {f["exec_id"] for f in attr.fills}
    assert "ex-late-1" in fill_ids
    assert attr.total_commission >= Decimal("2.0")


def test_fill_correction_supersedes_prior_price(tmp_path):
    ledger, *_ = _ledger(tmp_path)
    for ev in _full_chain_events(with_exit=False, with_commission=False):
        ledger.append(ev)

    ledger.append(_evidence(
        "fill_correction", exec_id="ex-entry-1", price=160.10, quantity=10.0,
        correction_seq=2, key="fill_correction:ex-entry-1:2",
    ))
    attr = ledger.rebuild_trade(TRADE_ID)
    entry = next(f for f in attr.fills if f["exec_id"] == "ex-entry-1")
    assert Decimal(str(entry["price"])) == Decimal("160.10")


def test_partial_fills_and_multiple_exits(tmp_path):
    ledger, *_ = _ledger(tmp_path)
    t0 = NOW
    t1 = NOW + dt.timedelta(seconds=1)
    t2 = NOW + dt.timedelta(seconds=2)
    base = [
        _evidence("command", command_id=TRADE_ID, order_group_id=ORDER_GROUP,
                  side="BUY", quantity=10.0, key=f"command:{TRADE_ID}", ts=t0),
        _evidence("context", approval_context_id="ctx-1",
                  expected_entry=160.0, expected_exit=200.0, key="context:ctx-1", ts=t0),
        _evidence("fill", exec_id="ex-p1", leg="entry", side="BOT",
                  quantity=6.0, price=160.0, key="fill:ex-p1", ts=t0),
        _evidence("fill", exec_id="ex-p2", leg="entry", side="BOT",
                  quantity=4.0, price=160.2, key="fill:ex-p2", ts=t0),
        _evidence("commission", exec_id="ex-p1", commission=0.6, key="commission:ex-p1", ts=t0),
        _evidence("commission", exec_id="ex-p2", commission=0.4, key="commission:ex-p2", ts=t0),
        # Two exit legs (partial stop + remainder target)
        _evidence("fill", exec_id="ex-stop", leg="stop", side="SLD",
                  quantity=6.0, price=150.0, key="fill:ex-stop", ts=t1),
        _evidence("fill", exec_id="ex-tp", leg="take_profit", side="SLD",
                  quantity=4.0, price=200.0, key="fill:ex-tp", ts=t1),
        _evidence("commission", exec_id="ex-stop", commission=0.6, key="commission:ex-stop", ts=t1),
        _evidence("commission", exec_id="ex-tp", commission=0.4, key="commission:ex-tp", ts=t1),
        _evidence("position", conid=CONID, quantity=0.0, key="position:flat", ts=t2),
    ]
    for ev in base:
        ledger.append(ev)

    attr = ledger.rebuild_trade(TRADE_ID)
    assert attr.resolved is True
    assert len(attr.fills) == 4
    assert len(attr.exit_fills) == 2
    # Gross: (150*6 + 200*4) - (160*6 + 160.2*4) = 1700 - 1600.8 = 99.2
    assert attr.gross_pnl == Decimal("99.2")
    assert attr.net_pnl == Decimal("99.2") - Decimal("2.0")


def test_duplicate_exec_id_not_double_counted(tmp_path):
    ledger, *_ = _ledger(tmp_path)
    for ev in _full_chain_events():
        ledger.append(ev)

    dup = _evidence(
        "fill", exec_id="ex-entry-1", leg="entry", side="BOT",
        quantity=10.0, price=160.05, key="fill:ex-entry-1",
    )
    assert ledger.append(dup) is False  # same evidence_key

    # Different key but same exec_id must still not double-count in rebuild
    assert ledger.append(_evidence(
        "fill", exec_id="ex-entry-1", leg="entry", side="BOT",
        quantity=10.0, price=999.0, key="fill:ex-entry-1:dup-delivery",
        ts=NOW + dt.timedelta(seconds=10),
    )) is True

    attr = ledger.rebuild_trade(TRADE_ID)
    entry_fills = [f for f in attr.fills if f["exec_id"] == "ex-entry-1"]
    assert len(entry_fills) == 1
    assert Decimal(str(entry_fills[0]["price"])) == Decimal("160.05")


def test_spread_slippage_latency_from_context(tmp_path):
    ledger, *_ = _ledger(tmp_path)
    for ev in _full_chain_events():
        ledger.append(ev)

    # Enrich with quote timing evidence
    ledger.append(_evidence(
        "quote_timing",
        signal_ts=NOW.isoformat(),
        submit_ts=(NOW + dt.timedelta(milliseconds=40)).isoformat(),
        fill_ts=(NOW + dt.timedelta(milliseconds=120)).isoformat(),
        quote_bid=159.98, quote_ask=160.02,
        key="quote_timing:1",
    ))
    attr = ledger.rebuild_trade(TRADE_ID)
    assert attr.latency_ms == Decimal("120")
    assert attr.spread_bps is not None and attr.spread_bps > 0
    assert attr.slippage_bps is not None  # entry vs mid


# ---------------------------------------------------------------------------
# Append-only + crash recovery
# ---------------------------------------------------------------------------

def test_raw_evidence_never_overwritten(tmp_path):
    ledger, *_ = _ledger(tmp_path)
    ev = _evidence("fill", exec_id="ex-1", quantity=1.0, price=10.0, side="BOT",
                   key="fill:ex-1")
    assert ledger.append(ev) is True
    assert ledger.append(ev) is False

    raw = ledger.db.execute(
        "SELECT COUNT(*), COUNT(DISTINCT evidence_key) FROM automation_decisions",
        fetch="one",
    )
    assert raw == (1, 1)

    # Attempting to "update" via append with same key is a no-op
    ledger.append(_evidence(
        "fill", exec_id="ex-1", quantity=99.0, price=1.0, side="BOT",
        key="fill:ex-1",
    ))
    row = ledger.db.execute(
        "SELECT payload FROM automation_decisions WHERE evidence_key = ?",
        ["fill:ex-1"], fetch="one",
    )
    import json
    assert json.loads(row[0])["price"] == 10.0


def test_crash_between_append_and_rebuild_recovers(tmp_path):
    """Raw persists; derived missing until rebuild — then deterministic."""
    ledger, journal, db, migrator = _ledger(tmp_path)
    for ev in _full_chain_events():
        ledger.append(ev)

    # Simulate crash: wipe derived tables only
    db.execute("DELETE FROM trade_attribution")
    db.execute("DELETE FROM execution_cost_attribution")

    # Fresh ledger instance (process restart)
    from trader.automation.attribution import AttributionLedger
    recovered = AttributionLedger(
        journal=journal, db=db, account_id=ACCOUNT, now=lambda: NOW,
    )
    a = recovered.rebuild_trade(TRADE_ID)
    b = recovered.rebuild_trade(TRADE_ID)
    assert a == b
    assert a.resolved is True
    assert a.gross_pnl == b.gross_pnl
    assert a.net_pnl == b.net_pnl


def test_rebuild_is_deterministic_across_append_order(tmp_path):
    events = _full_chain_events()
    ledger_a, *_ = _ledger(tmp_path / "a")
    ledger_b, *_ = _ledger(tmp_path / "b")

    for ev in events:
        ledger_a.append(ev)
    for ev in reversed(events):
        ledger_b.append(ev)

    assert ledger_a.rebuild_trade(TRADE_ID) == ledger_b.rebuild_trade(TRADE_ID)


# ---------------------------------------------------------------------------
# Promotion queries
# ---------------------------------------------------------------------------

def test_promotion_query_excludes_unresolved_and_reports_them(tmp_path):
    ledger, *_ = _ledger(tmp_path)

    # Resolved trade
    for ev in _full_chain_events():
        ledger.append(ev)
    ledger.rebuild_trade(TRADE_ID)

    # Unresolved: entry only, no exit / still open
    open_id = "cmd-open-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    for ev in _full_chain_events(with_exit=False):
        # re-key onto open trade
        from trader.automation.attribution import AttributionEvidenceEvent
        payload = dict(ev.payload)
        ledger.append(AttributionEvidenceEvent(
            evidence_key=f"{ev.evidence_key}:open",
            trade_id=open_id,
            event_kind=ev.event_kind,
            payload=payload,
            source_timestamp=ev.source_timestamp,
        ))
    ledger.rebuild_trade(open_id)

    report = ledger.promotion_attribution()
    resolved_ids = {t.trade_id for t in report.resolved}
    unresolved_ids = {t.trade_id for t in report.unresolved}
    assert TRADE_ID in resolved_ids
    assert open_id in unresolved_ids
    assert TRADE_ID not in unresolved_ids
    assert open_id not in resolved_ids
    open_attr = next(t for t in report.unresolved if t.trade_id == open_id)
    assert open_attr.resolved is False
    assert open_attr.unresolved_reasons  # explicit, not assumed zero PnL
    # Must not treat unresolved as zero-cost evidence
    assert open_attr.gross_pnl is None or "open_position" in open_attr.unresolved_reasons


# ---------------------------------------------------------------------------
# Domain events (observability only)
# ---------------------------------------------------------------------------

def test_append_and_rebuild_emit_domain_events(tmp_path):
    ledger, journal, db, _ = _ledger(tmp_path)
    ledger.append(_evidence(
        "command", command_id=TRADE_ID, order_group_id=ORDER_GROUP,
        side="BUY", quantity=10.0, key=f"command:{TRADE_ID}",
    ))
    for ev in _full_chain_events():
        if ev.event_kind == "command":
            continue
        ledger.append(ev)
    ledger.rebuild_trade(TRADE_ID)

    kinds = {
        row[0]
        for row in db.execute(
            "SELECT event_type FROM domain_event_journal "
            "WHERE entity_type IN ('trade_attribution', 'attribution_evidence')",
            fetch="all",
        )
    }
    assert "attribution.evidence_appended" in kinds
    assert "attribution.trade_rebuilt" in kinds


# ---------------------------------------------------------------------------
# broker_ingest wiring
# ---------------------------------------------------------------------------

def test_broker_ingest_appends_fill_and_commission_evidence(tmp_path):
    from trader.automation.attribution import AttributionLedger
    from trader.data.attribution_store import apply_attribution_migrations
    from trader.trading.broker_ingest import (
        BrokerIngest,
        CommissionObservation,
        FillObservation,
    )

    db, migrator, journal = _db(tmp_path, "ingest.duckdb")
    apply_attribution_migrations(migrator)
    store = BrokerStateStore(db)
    store.migrate(migrator)
    ledger = AttributionLedger(
        journal=journal, db=db, account_id=ACCOUNT, now=lambda: NOW,
    )
    ingest = BrokerIngest(
        db=db, journal=journal, store=store,
        account_id=ACCOUNT, account_mode="paper",
        attribution_ledger=ledger,
    )

    fill = FillObservation(
        account_id=ACCOUNT, exec_id="ex-wire-1", perm_id=1,
        client_order_id=0, conid=CONID, side="BOT", quantity=5.0,
        price=160.0, fill_time=NOW, source_timestamp=NOW,
    )
    # Seed trade_id mapping via prior command evidence
    ledger.append(_evidence(
        "command", command_id=TRADE_ID, order_group_id=ORDER_GROUP,
        side="BUY", quantity=5.0, key=f"command:{TRADE_ID}",
    ))
    # Bind fill to trade via order_group on a prior order evidence
    ledger.append(_evidence(
        "order", order_entity_id="ord-x", leg="entry",
        order_group_id=ORDER_GROUP, status="Submitted",
        key="order:ord-x:Submitted",
    ))

    # Direct apply path (no writer thread)
    conn = journal.connect()
    ingest._apply_record(
        conn, fill,
        lambda mutation, write: journal.mutate(conn, mutation, write),
    )
    ingest._apply_record(
        conn,
        CommissionObservation(fill=fill, commission=0.5, currency="USD", realized_pnl=None),
        lambda mutation, write: journal.mutate(conn, mutation, write),
    )

    keys = {
        row[0]
        for row in db.execute(
            "SELECT evidence_key FROM automation_decisions WHERE event_kind IN ('fill', 'commission')",
            fetch="all",
        )
    }
    assert any(k.startswith("fill:") for k in keys)
    assert any(k.startswith("commission:") for k in keys)


def test_broker_ingest_forwards_order_events_to_protective_saga(tmp_path):
    from trader.automation.attribution import AttributionLedger
    from trader.automation.protective_order_saga import BrokerOrderEvent
    from trader.data.attribution_store import apply_attribution_migrations
    from trader.trading.broker_ingest import BrokerIngest
    from trader.trading.order_correlation import OrderObservation

    db, migrator, journal = _db(tmp_path, "saga-wire.duckdb")
    apply_attribution_migrations(migrator)
    store = BrokerStateStore(db)
    store.migrate(migrator)

    seen: list[BrokerOrderEvent] = []

    class FakeSaga:
        def on_broker_event(self, event: BrokerOrderEvent):
            seen.append(event)
            return SimpleNamespace(state="ENTRY_WORKING")

    ledger = AttributionLedger(
        journal=journal, db=db, account_id=ACCOUNT, now=lambda: NOW,
    )
    ingest = BrokerIngest(
        db=db, journal=journal, store=store,
        account_id=ACCOUNT, account_mode="paper",
        attribution_ledger=ledger,
        protective_order_saga=FakeSaga(),
    )

    obs = OrderObservation(
        account_id=ACCOUNT,
        perm_id=42,
        client_order_id=1,
        parent_id=0,
        conid=CONID,
        symbol="AAPL",
        action="BUY",
        order_type="LMT",
        total_quantity=10.0,
        filled_quantity=0.0,
        avg_fill_price=None,
        limit_price=160.0,
        stop_price=None,
        tif="DAY",
        status="Submitted",
        order_ref=encode_order_ref(ORDER_GROUP),
        source_timestamp=NOW,
    )
    conn = journal.connect()
    ingest._apply_record(
        conn, obs,
        lambda mutation, write: journal.mutate(conn, mutation, write),
    )
    assert len(seen) == 1
    assert seen[0].order_group_id == ORDER_GROUP
    assert seen[0].leg == "entry"
    assert seen[0].status == "Submitted"
