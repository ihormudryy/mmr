"""P5 Task 2 — allocation ladder enforcement at command time."""
from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from trader.automation.artifact_verifier import VerifiedArtifact
from trader.automation.intent_ids import derive_command_id, derive_intent_id
from trader.automation.models import (
    EntryPolicy,
    ExecutionIntent,
    StopPolicy,
    TargetPolicy,
    TimeExitPolicy,
)
from trader.data.allocation_authority_store import AllocationAuthorityStore, apply_allocation_authority_migrations
from trader.data.broker_state import BrokerOrderRow, BrokerPositionRow, BrokerRiskSnapshot
from trader.data.domain_journal import DomainJournal
from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator
from trader.promotion.allocation_attestation import STAGE_SCALE_1, sign_allocation_payload
from trader.promotion.allocation_policy import (
    AllocationPolicy,
    compute_gross_notional,
    compute_working_entry_notional,
    resolve_effective_gross_ceiling,
)
from trader.research.signing import AttestationSigner
from cryptography.hazmat.primitives.asymmetric import ed25519

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 7, 20, 15, 0, tzinfo=UTC)
ACCOUNT = "DU111111"
CONID = 265598
ARTIFACT = "artifact-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


def _intent(**overrides) -> ExecutionIntent:
    entry = EntryPolicy(order_type="LIMIT", limit_offset_bps=Decimal("5"), tif="DAY")
    stop = StopPolicy(stop_price=Decimal("99"), order_type="STP")
    target = TargetPolicy(target_price=Decimal("110"), order_type="LMT")
    time_exit = TimeExitPolicy(max_hold_bars=10, close_by=NOW + dt.timedelta(hours=2))
    fields = dict(
        artifact_id=ARTIFACT,
        session_id="session-1",
        bar_id="bar-1",
        signal_id="signal-1",
        account_mode="paper",
        conid=CONID,
        side="BUY",
        requested_quantity=Decimal("100"),
        risk_fraction=Decimal("0.001"),
        entry_policy=entry,
        stop_policy=stop,
        target_policy=target,
        time_exit_policy=time_exit,
        artifact_digest="digest-artifact-1",
        eligibility_attestation_digest="digest-attest-1",
        signal_timestamp=NOW,
        completed_bar_timestamp=NOW - dt.timedelta(minutes=1),
    )
    fields.update(overrides)
    from dataclasses import asdict

    dict_fields = {k: (asdict(v) if hasattr(v, "__dataclass_fields__") else v) for k, v in fields.items()}
    fields["intent_id"] = derive_intent_id(dict_fields)
    fields["command_id"] = derive_command_id(fields["intent_id"])
    return ExecutionIntent(**fields)


def _artifact(**overrides) -> VerifiedArtifact:
    base = dict(
        artifact_id=ARTIFACT,
        manifest_digest="sha256:manifest",
        dataset_manifest_digest="sha256:dataset",
        parameters={},
        allowlist=(str(CONID),),
        max_gross_allocation=0.15,
        expires_at=NOW + dt.timedelta(days=30),
        public_key_id="key-1",
        verification_reason_codes=("OK",),
        allowlist_digest="allow-1",
        ruleset_digest="rules-1",
    )
    base.update(overrides)
    return VerifiedArtifact(**base)


def _position(conid: int, qty: float, market_value: float) -> BrokerPositionRow:
    return BrokerPositionRow(
        account_id=ACCOUNT,
        conid=conid,
        symbol=str(conid),
        sec_type="STK",
        exchange="SMART",
        currency="USD",
        quantity=qty,
        average_cost=100.0,
        market_price=100.0,
        market_value=market_value,
        unrealized_pnl=0.0,
        realized_pnl=0.0,
        daily_pnl=0.0,
        deleted=False,
        revision=1,
        source_timestamp=NOW,
    )


def _order(
    *,
    conid=CONID,
    action="BUY",
    total=50.0,
    filled=0.0,
    limit=100.0,
    entity_id="ord-1",
) -> BrokerOrderRow:
    return BrokerOrderRow(
        order_entity_id=entity_id,
        account_id=ACCOUNT,
        conid=conid,
        symbol=str(conid),
        order_group_id="og-1",
        leg="ENTRY",
        is_external=False,
        action=action,
        order_type="LMT",
        total_quantity=total,
        filled_quantity=filled,
        avg_fill_price=None,
        limit_price=limit,
        stop_price=None,
        tif="DAY",
        status="Submitted",
        deleted=False,
        revision=1,
        source_timestamp=NOW,
    )


def _broker(
    *,
    net_liquidation=1_000_000.0,
    positions=(),
    working_orders=(),
    account_mode="paper",
    generation_id=1,
    source_cursor=1,
) -> BrokerRiskSnapshot:
    return BrokerRiskSnapshot(
        generation_id=generation_id,
        source_cursor=source_cursor,
        promoted_at=NOW,
        account_id=ACCOUNT,
        account_mode=account_mode,
        net_liquidation=net_liquidation,
        daily_pnl=0.0,
        positions=positions,
        working_orders=working_orders,
    )


def _authority_record(tmp_path: Path, *, max_gross=0.09, stage=STAGE_SCALE_1, account_mode="live"):
    db = DuckDBConnection.get_instance(str(tmp_path / "auth.duckdb"))
    migrator = SchemaMigrator(db)
    journal = DomainJournal(db)
    journal.migrate(migrator)
    apply_allocation_authority_migrations(migrator)
    store = AllocationAuthorityStore(journal=journal, db=db, now=lambda: NOW)
    signer = AttestationSigner(ed25519.Ed25519PrivateKey.generate())
    from trader.promotion.allocation_attestation import (
        AllocationAttestationVerifier,
        ExpectedAllocationBindings,
        build_allocation_payload,
    )

    unsigned = build_allocation_payload(
        strategy_id="orb",
        account_id=ACCOUNT,
        account_mode=account_mode,
        stage=stage,
        artifact_digest=ARTIFACT,
        allowlist_digest="allow-1",
        ruleset_digest="rules-1",
        max_gross_allocation=max_gross,
        evidence_digest="evidence-1",
        issued_at=NOW,
        expires_at=NOW + dt.timedelta(days=30),
        operator="operator:alice",
        reason="scale",
        public_key_id=signer.public_key_id,
    )
    attestation = sign_allocation_payload(signer, unsigned)
    verifier = AllocationAttestationVerifier({signer.public_key_id: signer.public_key})
    verified = verifier.verify(
        attestation,
        expected=ExpectedAllocationBindings(
            account_id=ACCOUNT,
            account_mode=account_mode,
            artifact_digest=ARTIFACT,
            allowlist_digest="allow-1",
            ruleset_digest="rules-1",
            strategy_id="orb",
        ),
        now=NOW,
    )
    digest = store.record_issued(attestation, verified, operator="op", reason="test")
    store.record_activated(digest, command_id="cmd-1")
    return store.active_for(ACCOUNT, ARTIFACT, now=NOW)


def test_resolve_ceiling_most_restrictive_wins():
    effective, candidates, digest, reasons = resolve_effective_gross_ceiling(
        artifact_max_gross=0.15,
        authority=None,
        account_mode="paper",
        account_id=ACCOUNT,
        artifact_digest=ARTIFACT,
        now=NOW,
    )
    assert effective == 0.15
    assert not reasons
    names = {c.name for c in candidates}
    assert "artifact" in names and "trader_steady_cap" in names


def test_live_requires_signed_authority():
    _, _, _, reasons = resolve_effective_gross_ceiling(
        artifact_max_gross=0.15,
        authority=None,
        account_mode="live",
        account_id=ACCOUNT,
        artifact_digest=ARTIFACT,
        now=NOW,
    )
    assert "ALLOCATION_AUTHORITY_ABSENT" in reasons


def test_rejects_gross_with_pending_working_orders():
    positions = (_position(CONID, 400.0, 40_000.0),)
    working = (_order(total=200.0, limit=100.0),)  # +$20k pending
    broker = _broker(positions=positions, working_orders=working)
    policy = AllocationPolicy(now=lambda: NOW)
    decision = policy.evaluate(
        _intent(requested_quantity=Decimal("100")),
        broker,
        None,
        artifact=_artifact(max_gross_allocation=0.06),
        entry_price=100.0,
    )
    assert decision.approved is False
    assert "GROSS_EXPOSURE" in decision.reason_codes


def test_partial_fill_working_order_uses_remaining_quantity():
    working = (_order(total=100.0, filled=60.0, limit=100.0),)
    broker = _broker(working_orders=working)
    pending, _ = compute_working_entry_notional(broker)
    assert pending == pytest.approx(4_000.0)


def test_overlapping_symbol_adds_incremental_exposure():
    positions = (_position(CONID, 300.0, 30_000.0),)
    broker = _broker(positions=positions)
    policy = AllocationPolicy(now=lambda: NOW)
    decision = policy.evaluate(
        _intent(requested_quantity=Decimal("400")),  # +$40k → 7% gross
        broker,
        None,
        artifact=_artifact(max_gross_allocation=0.06),
        entry_price=100.0,
    )
    assert decision.approved is False
    assert "GROSS_EXPOSURE" in decision.reason_codes


def test_signed_authority_allows_higher_than_six_percent(tmp_path):
    authority = _authority_record(tmp_path, max_gross=0.09)
    broker = _broker(account_mode="live")
    policy = AllocationPolicy(now=lambda: NOW)
    decision = policy.evaluate(
        _intent(requested_quantity=Decimal("800")),  # 8% gross
        broker,
        authority,
        artifact=_artifact(max_gross_allocation=0.15),
        entry_price=100.0,
    )
    assert decision.approved is True
    assert decision.effective_gross_ceiling == pytest.approx(0.09)


def test_stale_broker_generation_rejected_on_dispatch():
    approved = _broker(generation_id=5, source_cursor=10)
    current = _broker(generation_id=4, source_cursor=10, account_mode="live")
    policy = AllocationPolicy(now=lambda: NOW)
    decision = policy.revalidate_dispatch(
        broker=current,
        approved_broker=approved,
        conid=CONID,
        side="BUY",
        quantity=100.0,
        entry_price=100.0,
        authority=None,
        artifact_max_gross=0.15,
        artifact_digest=ARTIFACT,
        authority_digest=None,
        effective_gross_ceiling=0.09,
    )
    assert decision.approved is False
    assert "BROKER_GENERATION_STALE" in decision.reason_codes


@given(
    hi=st.floats(min_value=0.04, max_value=0.15, allow_nan=False, allow_infinity=False),
    shrink=st.floats(min_value=0.2, max_value=0.95, allow_nan=False, allow_infinity=False),
)
@settings(max_examples=25, deadline=None)
def test_lower_signed_ceiling_cannot_approve_after_reject(hi, shrink):
    assume(shrink < 1.0)
    lo = hi * shrink
    policy = AllocationPolicy(now=lambda: NOW)
    intent = _intent(requested_quantity=Decimal("700"))
    broker = _broker()
    artifact = _artifact(max_gross_allocation=0.15)
    hi_decision = policy.evaluate(intent, broker, None, artifact=artifact, entry_price=100.0)
    from trader.automation.session_risk import _SyntheticAllocationAuthority

    lo_auth = _SyntheticAllocationAuthority(
        account_id=ACCOUNT,
        account_mode="paper",
        artifact_digest=ARTIFACT,
        max_gross_allocation=lo,
    )
    lo_decision = policy.evaluate(intent, broker, lo_auth, artifact=artifact, entry_price=100.0)
    if not hi_decision.approved:
        assert not lo_decision.approved


def test_limit_candidates_recorded_in_decision(tmp_path):
    authority = _authority_record(tmp_path, max_gross=0.09)
    policy = AllocationPolicy(now=lambda: NOW)
    decision = policy.evaluate(
        _intent(requested_quantity=Decimal("100")),
        _broker(account_mode="live"),
        authority,
        artifact=_artifact(max_gross_allocation=0.15),
        entry_price=100.0,
    )
    assert decision.authority_digest is not None
    assert len(decision.limit_candidates) >= 3
    assert decision.broker_generation_id == 1
