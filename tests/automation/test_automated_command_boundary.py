"""P3 Task 3 — automated intent enters the existing command coordinator only.

Boundary contract (plan §Task 3):
* ``execute_automated_intent`` is a coordinator saga action, not a second order path.
* Only the ``strategy_service`` principal may call it; dashboard/browser/CLI are refused.
* The typed request carries the full intent + artifact bundle digest; account_id is
  server-derived (never on the wire).
* Duplicate delivery (in-flight / after reject / after submit / after OUTCOME_UNKNOWN /
  after resolve) never re-dispatches.
* Audit records artifact/session/signal/intent/attestation digests before any dispatch.
* Order-group correlation is colon-free and round-trips through ``encode_order_ref``.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from trader.automation.intent_ids import derive_command_id, derive_intent_id
from trader.automation.models import (
    EntryPolicy,
    ExecutionIntent,
    StopPolicy,
    TargetPolicy,
    TimeExitPolicy,
)
from trader.data.domain_journal import DomainJournal
from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator
from trader.messaging.production_api import (
    ExecuteAutomatedIntentRequest,
    register_command_authority,
)
from trader.messaging.typed_rpc import TypedRpcRegistry
from trader.trading.command_coordinator import (
    CommandAudit,
    CommandLedger,
    CommandRequest,
    TradingCommandCoordinator,
    apply_command_ledger_migration,
)
from trader.trading.order_correlation import encode_order_ref
from trader.trading.trading_control import (
    TradingControlStore,
    apply_trading_control_migration,
)

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 7, 18, 14, 30, tzinfo=UTC)
ACCOUNT = "DU111111"
ARTIFACT_DIGEST = "sha256:artifact-bundle-deadbeef"


# ---------------------------------------------------------------------------
# Intent / request helpers
# ---------------------------------------------------------------------------

def _intent_fields(**overrides):
    entry = EntryPolicy(order_type="LIMIT", limit_offset_bps=Decimal("5"), tif="DAY")
    stop = StopPolicy(stop_price=Decimal("150"), order_type="STP")
    target = TargetPolicy(target_price=Decimal("200"), order_type="LMT")
    time_exit = TimeExitPolicy(max_hold_bars=10, close_by=NOW + dt.timedelta(hours=2))
    fields = dict(
        artifact_id="artifact-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        session_id="session-1",
        bar_id="bar-1",
        signal_id="signal-1",
        account_mode="paper",
        conid=265598,
        side="BUY",
        requested_quantity=Decimal("10"),
        risk_fraction=Decimal("0.02"),
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
    dict_fields = {
        k: (asdict(v) if hasattr(v, "__dataclass_fields__") else v)
        for k, v in fields.items()
    }
    intent_id = derive_intent_id(dict_fields)
    command_id = derive_command_id(intent_id)
    fields["intent_id"] = intent_id
    fields["command_id"] = command_id
    return fields


def make_intent(**overrides) -> ExecutionIntent:
    return ExecutionIntent(**_intent_fields(**overrides))


def intent_to_request_body(intent: ExecutionIntent, *, bundle_digest: str = ARTIFACT_DIGEST) -> dict:
    """JSON-safe body for CommandRequest (ledger hash + audit persistence)."""
    return intent_to_wire(intent, bundle_digest=bundle_digest)


def intent_to_wire(intent: ExecutionIntent, *, bundle_digest: str = ARTIFACT_DIGEST) -> dict:
    """JSON-shaped payload for ExecuteAutomatedIntentRequest."""
    def _dec(v):
        return str(v) if isinstance(v, Decimal) else v

    def _ts(v):
        return v.isoformat().replace("+00:00", "Z") if isinstance(v, dt.datetime) else v

    return {
        "command_id": intent.command_id,
        "artifact_id": intent.artifact_id,
        "session_id": intent.session_id,
        "bar_id": intent.bar_id,
        "signal_id": intent.signal_id,
        "intent_id": intent.intent_id,
        "account_mode": intent.account_mode,
        "conid": intent.conid,
        "side": intent.side,
        "requested_quantity": _dec(intent.requested_quantity),
        "risk_fraction": _dec(intent.risk_fraction),
        "entry_policy": {
            "order_type": intent.entry_policy.order_type,
            "limit_offset_bps": _dec(intent.entry_policy.limit_offset_bps),
            "tif": intent.entry_policy.tif,
        },
        "stop_policy": {
            "stop_price": _dec(intent.stop_policy.stop_price),
            "order_type": intent.stop_policy.order_type,
        },
        "target_policy": (
            None if intent.target_policy is None else {
                "target_price": _dec(intent.target_policy.target_price),
                "order_type": intent.target_policy.order_type,
            }
        ),
        "time_exit_policy": {
            "max_hold_bars": intent.time_exit_policy.max_hold_bars,
            "close_by": _ts(intent.time_exit_policy.close_by),
        },
        "artifact_digest": intent.artifact_digest,
        "eligibility_attestation_digest": intent.eligibility_attestation_digest,
        "signal_timestamp": _ts(intent.signal_timestamp),
        "completed_bar_timestamp": _ts(intent.completed_bar_timestamp),
        "artifact_bundle_digest": bundle_digest,
    }


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeIntentDispatch:
    """Records automated-intent dispatches; never talks to IB."""

    def __init__(self):
        self.calls: list[dict] = []
        self._raise = None
        self._next_id = 9001

    def raise_on_submit(self, exc):
        self._raise = exc

    def submit(self, *, intent, order_group_id, order_ref):
        if self._raise is not None:
            raise self._raise
        self.calls.append({
            "intent_id": intent.intent_id,
            "command_id": intent.command_id,
            "order_group_id": order_group_id,
            "order_ref": order_ref,
        })
        order_id = self._next_id
        self._next_id += 1
        return SimpleNamespace(order_group_id=order_group_id, order_ref=order_ref,
                               order_ids=[order_id])


class FakeArtifactVerifier:
    def __init__(self):
        self.calls: list[dict] = []
        self._error = None

    def fail_with(self, exc):
        self._error = exc

    def verify(self, bundle_path, expected_mode, expected_artifact_id, now, *,
               revoked_digests=()):
        self.calls.append({
            "bundle_path": str(bundle_path),
            "expected_mode": expected_mode,
            "expected_artifact_id": expected_artifact_id,
            "now": now,
        })
        if self._error is not None:
            raise self._error
        return SimpleNamespace(
            artifact_id=expected_artifact_id,
            manifest_digest="manifest-ok",
            dataset_manifest_digest="dataset-ok",
            parameters={},
            allowlist=("265598",),
            max_gross_allocation=0.06,
            expires_at=now + dt.timedelta(days=30),
            public_key_id="ed25519-test",
            verification_reason_codes=("RULES_PASS",),
        )


class FakeSchedule:
    def __init__(self):
        self.calls = 0

    def schedule(self, command_id):
        self.calls += 1


def _build_stack(tmp_path: Path, *, dispatch=None, verifier=None, now=None):
    from trader.automation.automated_intent_command import AutomatedIntentCommandService

    db = DuckDBConnection.get_instance(str(tmp_path / "automation.duckdb"))
    migrator = SchemaMigrator(db)
    journal = DomainJournal(db)
    journal.migrate(migrator)
    apply_command_ledger_migration(migrator)
    apply_trading_control_migration(migrator)

    clock = now or (lambda: NOW)
    controls = TradingControlStore(journal)
    db.transaction(lambda conn: controls.seed_in_tx(conn, [(ACCOUNT, "paper")], NOW))
    ledger = CommandLedger(journal)
    audit = CommandAudit(journal)
    dispatch = dispatch or FakeIntentDispatch()
    verifier = verifier or FakeArtifactVerifier()
    schedule = FakeSchedule()
    service = AutomatedIntentCommandService(
        ledger=ledger,
        audit=audit,
        journal=journal,
        controls=controls,
        dispatch=dispatch,
        artifact_verifier=verifier,
        account_id=ACCOUNT,
        account_mode="paper",
        now=clock,
        bundle_root=tmp_path / "bundles",
        schedule_reconcile=schedule.schedule,
    )

    class _NonceGate:
        def consume_in_tx(self, *args, **kwargs):
            return True

    coordinator = TradingCommandCoordinator(
        ledger=ledger,
        audit=audit,
        journal=journal,
        nonces=_NonceGate(),
        now=clock,
        reconciler=SimpleNamespace(schedule=lambda command_id, now=None: schedule.schedule(command_id)),
    )
    coordinator.register_action(
        "execute_automated_intent", service.execute,
        requires_preflight=False, saga=True,
    )
    return SimpleNamespace(
        conn=db, journal=journal, ledger=ledger, audit=audit,
        service=service, coordinator=coordinator, dispatch=dispatch,
        verifier=verifier, controls=controls, schedule=schedule,
    )


# ---------------------------------------------------------------------------
# Wire model
# ---------------------------------------------------------------------------

def test_wire_model_forbids_account_id_and_extra_fields():
    intent = make_intent()
    payload = intent_to_wire(intent)
    payload["account_id"] = "HACKED"
    with pytest.raises(ValidationError):
        ExecuteAutomatedIntentRequest.model_validate(payload)


def test_wire_model_rejects_colon_in_command_id():
    intent = make_intent()
    payload = intent_to_wire(intent)
    payload["command_id"] = "auto:forged"
    with pytest.raises(ValidationError, match=":"):
        ExecuteAutomatedIntentRequest.model_validate(payload)


def test_wire_model_round_trips_intent_fields():
    intent = make_intent()
    parsed = ExecuteAutomatedIntentRequest.model_validate(intent_to_wire(intent))
    assert parsed.command_id == intent.command_id
    assert parsed.intent_id == intent.intent_id
    assert parsed.conid == intent.conid
    assert parsed.artifact_bundle_digest == ARTIFACT_DIGEST
    assert not hasattr(parsed, "account_id") or "account_id" not in parsed.model_fields


# ---------------------------------------------------------------------------
# Principal allowlisting
# ---------------------------------------------------------------------------

def test_strategy_service_principal_is_accepted(tmp_path):
    stack = _build_stack(tmp_path)
    intent = make_intent()
    (tmp_path / "bundles").mkdir()
    (tmp_path / "bundles" / ARTIFACT_DIGEST.replace(":", "_")).mkdir()

    request = CommandRequest(
        command_id=intent.command_id,
        action="execute_automated_intent",
        account_id=ACCOUNT,
        target_type="intent",
        target_id=intent.intent_id,
        expected_version=None,
        body=intent_to_request_body(intent),
        source="strategy_service",
    )
    receipt = stack.coordinator.execute(request)
    assert receipt.state in ("SUBMITTED", "RESOLVED")
    assert receipt.error_code is None
    assert len(stack.dispatch.calls) == 1


@pytest.mark.parametrize("source", ["dashboard", "browser", "cli", "mmr_cli", "unknown"])
def test_non_strategy_principal_is_rejected(tmp_path, source):
    stack = _build_stack(tmp_path)
    intent = make_intent()
    request = CommandRequest(
        command_id=intent.command_id,
        action="execute_automated_intent",
        account_id=ACCOUNT,
        target_type="intent",
        target_id=intent.intent_id,
        expected_version=None,
        body=intent_to_request_body(intent),
        source=source,
    )
    receipt = stack.coordinator.execute(request)
    assert receipt.state == "REJECTED"
    assert receipt.error_code == "PRINCIPAL_FORBIDDEN"
    assert stack.dispatch.calls == []


# ---------------------------------------------------------------------------
# Claim / audit / correlation before dispatch
# ---------------------------------------------------------------------------

def test_command_is_claimed_and_audited_before_dispatch(tmp_path):
    stack = _build_stack(tmp_path)
    intent = make_intent()
    (tmp_path / "bundles").mkdir()
    (tmp_path / "bundles" / ARTIFACT_DIGEST.replace(":", "_")).mkdir()

    request = CommandRequest(
        command_id=intent.command_id,
        action="execute_automated_intent",
        account_id=ACCOUNT,
        target_type="intent",
        target_id=intent.intent_id,
        expected_version=None,
        body=intent_to_request_body(intent),
        source="strategy_service",
    )
    receipt = stack.coordinator.execute(request)
    row = stack.ledger.get(intent.command_id)
    assert row is not None
    assert row.state == receipt.state

    # Audit (written at RECEIVED, before dispatch) must carry authority digests.
    audit_row = stack.conn.execute(
        "SELECT redacted_inputs FROM command_audit WHERE command_id = ?",
        [intent.command_id],
        fetch="one",
    )
    assert audit_row is not None
    redacted = audit_row[0]
    for token in (
        intent.artifact_id, intent.session_id, intent.signal_id, intent.intent_id,
        intent.eligibility_attestation_digest, ARTIFACT_DIGEST,
    ):
        assert token in str(redacted)

    assert stack.dispatch.calls[0]["order_group_id"] == f"og-{intent.command_id}"
    assert ":" not in stack.dispatch.calls[0]["order_group_id"]
    assert stack.dispatch.calls[0]["order_ref"] == encode_order_ref(
        stack.dispatch.calls[0]["order_group_id"])


# ---------------------------------------------------------------------------
# Idempotency / conflict
# ---------------------------------------------------------------------------

def test_exact_replay_returns_recorded_receipt_without_redispatch(tmp_path):
    stack = _build_stack(tmp_path)
    intent = make_intent()
    (tmp_path / "bundles").mkdir()
    (tmp_path / "bundles" / ARTIFACT_DIGEST.replace(":", "_")).mkdir()
    request = CommandRequest(
        command_id=intent.command_id,
        action="execute_automated_intent",
        account_id=ACCOUNT,
        target_type="intent",
        target_id=intent.intent_id,
        expected_version=None,
        body=intent_to_request_body(intent),
        source="strategy_service",
    )
    first = stack.coordinator.execute(request)
    second = stack.coordinator.execute(request)
    assert second.state == first.state
    assert second.command_id == first.command_id
    assert len(stack.dispatch.calls) == 1


def test_changed_payload_conflicts(tmp_path):
    stack = _build_stack(tmp_path)
    intent = make_intent()
    (tmp_path / "bundles").mkdir()
    (tmp_path / "bundles" / ARTIFACT_DIGEST.replace(":", "_")).mkdir()
    request = CommandRequest(
        command_id=intent.command_id,
        action="execute_automated_intent",
        account_id=ACCOUNT,
        target_type="intent",
        target_id=intent.intent_id,
        expected_version=None,
        body=intent_to_request_body(intent),
        source="strategy_service",
    )
    stack.coordinator.execute(request)

    conflicted = CommandRequest(
        command_id=intent.command_id,
        action="execute_automated_intent",
        account_id=ACCOUNT,
        target_type="intent",
        target_id=intent.intent_id,
        expected_version=None,
        body=intent_to_request_body(intent, bundle_digest="sha256:other"),
        source="strategy_service",
    )
    receipt = stack.coordinator.execute(conflicted)
    assert receipt.state == "REJECTED"
    assert receipt.error_code == "COMMAND_CONFLICT"
    assert len(stack.dispatch.calls) == 1


@pytest.mark.parametrize("phase", ["reject", "submit", "outcome_unknown"])
def test_duplicate_delivery_never_dispatches_twice(tmp_path, phase):
    dispatch = FakeIntentDispatch()
    if phase == "outcome_unknown":
        dispatch.raise_on_submit(TimeoutError("ib ack lost"))
    stack = _build_stack(tmp_path, dispatch=dispatch)
    intent = make_intent()
    (tmp_path / "bundles").mkdir()
    (tmp_path / "bundles" / ARTIFACT_DIGEST.replace(":", "_")).mkdir()

    if phase == "reject":
        # Force principal rejection on the first call by using a forbidden source,
        # then replay with the same command_id/hash — still no dispatch.
        request = CommandRequest(
            command_id=intent.command_id,
            action="execute_automated_intent",
            account_id=ACCOUNT,
            target_type="intent",
            target_id=intent.intent_id,
            expected_version=None,
            body=intent_to_request_body(intent),
            source="dashboard",
        )
        first = stack.coordinator.execute(request)
        assert first.state == "REJECTED"
        second = stack.coordinator.execute(request)
        assert second.state == "REJECTED"
        assert dispatch.calls == []
        return

    request = CommandRequest(
        command_id=intent.command_id,
        action="execute_automated_intent",
        account_id=ACCOUNT,
        target_type="intent",
        target_id=intent.intent_id,
        expected_version=None,
        body=intent_to_request_body(intent),
        source="strategy_service",
    )
    first_receipt = stack.coordinator.execute(request)
    if phase == "outcome_unknown":
        assert first_receipt.state == "OUTCOME_UNKNOWN"
        assert first_receipt.retryable is False
        assert len(dispatch.calls) == 0
    else:
        assert first_receipt.state == "SUBMITTED"
        assert len(dispatch.calls) == 1

    replay = stack.coordinator.execute(request)
    # Exact replay must not add another dispatch call.
    assert len(dispatch.calls) == (0 if phase == "outcome_unknown" else 1)
    if phase == "submit":
        assert replay.state == "SUBMITTED"
    if phase == "outcome_unknown":
        assert replay.state == "OUTCOME_UNKNOWN"
        assert replay.retryable is False


# ---------------------------------------------------------------------------
# RPC registration surface
# ---------------------------------------------------------------------------

def test_rpc_registers_execute_automated_intent_for_strategy_principal(tmp_path):
    stack = _build_stack(tmp_path)
    (tmp_path / "bundles").mkdir()
    (tmp_path / "bundles" / ARTIFACT_DIGEST.replace(":", "_")).mkdir()
    registry = TypedRpcRegistry()
    register_command_authority(
        registry,
        stack.coordinator,
        proposal_service=SimpleNamespace(),  # unused when only automation is wired
        repository=SimpleNamespace(),
        account_id=ACCOUNT,
        account_mode="paper",
        controls=stack.controls,
        automated_intent_service=stack.service,
    )
    registration = registry.resolve("command", "execute_automated_intent")
    assert registration is not None

    intent = make_intent()
    parsed = ExecuteAutomatedIntentRequest.model_validate(intent_to_wire(intent))
    receipt = registration.handler(parsed)
    assert receipt["state"] in ("SUBMITTED", "RESOLVED")
    assert stack.dispatch.calls[0]["intent_id"] == intent.intent_id
