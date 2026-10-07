"""P3 Task 3 — automated intent enters the existing command coordinator only.

Boundary contract (plan §Task 3):
* ``execute_automated_intent`` is a coordinator saga action, not a second order path.
* Only the ``strategy`` principal may call it; dashboard/browser/CLI are refused.
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

from trader.messaging.principals import TRADER_ACL
from trader.messaging.typed_rpc import RpcCaller
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
SOURCE_DIGEST = "src-attested"
ARMED_ARTIFACT_ID = "artifact-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


# ---------------------------------------------------------------------------
# Intent / request helpers
# ---------------------------------------------------------------------------

def _intent_fields(**overrides):
    entry = EntryPolicy(order_type="LIMIT", limit_offset_bps=Decimal("5"), tif="DAY")
    stop = StopPolicy(stop_price=Decimal("150"), order_type="STP")
    target = TargetPolicy(target_price=Decimal("200"), order_type="LMT")
    time_exit = TimeExitPolicy(max_hold_bars=10, close_by=NOW + dt.timedelta(hours=2))
    fields = dict(
        artifact_id=ARMED_ARTIFACT_ID,
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
        "strategy_source_digest": SOURCE_DIGEST,
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
        self.allowlist = ("265598",)

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
            allowlist=self.allowlist,
            max_gross_allocation=0.06,
            expires_at=now + dt.timedelta(days=30),
            public_key_id="ed25519-test",
            verification_reason_codes=("RULES_PASS",),
            attested_strategy=SimpleNamespace(source_digest=SOURCE_DIGEST),
        )


class FakeSchedule:
    def __init__(self):
        self.calls = 0

    def schedule(self, command_id):
        self.calls += 1


class FakeProtectiveSaga:
    """Stands in for ProtectiveOrderSaga: sends through the fake dispatch like the real one."""

    def __init__(self, dispatch):
        self._dispatch = dispatch

    def start(self, *, intent, **_evidence):
        order_group_id = f"og-{intent.command_id}"
        try:
            submitted = self._dispatch.submit(
                intent=intent, order_group_id=order_group_id,
                order_ref=encode_order_ref(order_group_id),
            )
        except Exception:
            return SimpleNamespace(state="OUTCOME_UNKNOWN", error_code="DISPATCH_AMBIGUOUS",
                                   submitted_order_ids=[])
        return SimpleNamespace(state="SUBMITTING", error_code=None,
                               submitted_order_ids=list(submitted.order_ids))


def _build_stack(tmp_path: Path, *, dispatch=None, verifier=None, now=None, with_saga=True, liquidation=None,
                 broker=None, protective_saga=None, approval_factory=None):
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
        expected_artifact_id=ARMED_ARTIFACT_ID,
        schedule_reconcile=schedule.schedule,
        liquidation=liquidation,
        broker=broker,
        protective_saga=protective_saga or (FakeProtectiveSaga(dispatch) if with_saga else None),
        approval_factory=approval_factory or ((lambda **_kwargs: object()) if with_saga else None),
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
        source="strategy", principal="strategy",
    )
    receipt = stack.coordinator.execute(request)
    assert receipt.state in ("SUBMITTED", "RESOLVED")
    assert receipt.error_code is None
    assert len(stack.dispatch.calls) == 1


@pytest.mark.parametrize("principal", ["dashboard", "cli", "ai_supervisor", "trader", None])
def test_non_strategy_principal_is_rejected(tmp_path, principal):
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
        source="strategy",  # a label alone grants nothing
        principal=principal,
    )
    receipt = stack.coordinator.execute(request)
    assert receipt.state == "REJECTED"
    assert receipt.error_code == "PRINCIPAL_FORBIDDEN"
    assert stack.dispatch.calls == []


# ---------------------------------------------------------------------------
# Claim / audit / correlation before dispatch
# ---------------------------------------------------------------------------

def test_without_a_saga_the_command_is_refused_and_nothing_is_sent(tmp_path):
    stack = _build_stack(tmp_path, with_saga=False)
    intent = make_intent()
    (tmp_path / "bundles").mkdir()
    (tmp_path / "bundles" / ARTIFACT_DIGEST.replace(":", "_")).mkdir()
    request = CommandRequest(
        command_id=intent.command_id, action="execute_automated_intent",
        account_id=ACCOUNT, target_type="intent", target_id=intent.intent_id,
        expected_version=None, body=intent_to_request_body(intent),
        source="strategy_service", principal="strategy",
    )

    receipt = stack.coordinator.execute(request)

    assert (receipt.state, receipt.error_code) == ("REJECTED", "SAGA_REQUIRED")
    assert stack.ledger.get(intent.command_id).state == "REJECTED"
    assert stack.dispatch.calls == []
    assert stack.schedule.calls == 0


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
        source="strategy", principal="strategy",
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
        source="strategy", principal="strategy",
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
        source="strategy", principal="strategy",
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
        source="strategy", principal="strategy",
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
        source="strategy", principal="strategy",
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
    registry = TypedRpcRegistry(acl=TRADER_ACL)
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
    assert registration.allowed_principals == frozenset({"strategy"})
    receipt = registration.handler(parsed, RpcCaller("strategy", None))
    assert receipt["state"] in ("SUBMITTED", "RESOLVED")
    assert stack.dispatch.calls[0]["intent_id"] == intent.intent_id


@pytest.mark.parametrize("stage", ["approval", "session_state", "allocation"])
def test_evidence_read_failure_is_rejected_without_reconciliation(tmp_path, stage):
    from trader.trading.approval_context import ApprovalContextError

    stack = _build_stack(tmp_path)
    intent = make_intent()

    def missing_quote(**kwargs):
        raise ApprovalContextError("NO_QUOTE", "no executable market evidence")

    def must_not_start(**kwargs):
        pytest.fail("saga started despite failed evidence capture")

    stack.service._approval_factory = lambda **kwargs: object()
    stack.service._session_state_factory = lambda **kwargs: object()
    stack.service._allocation_factory = lambda **kwargs: object()
    setattr(stack.service, f"_{stage}_factory", missing_quote)
    stack.service._protective_saga = SimpleNamespace(start=must_not_start)
    request = CommandRequest(
        command_id=intent.command_id, action="execute_automated_intent",
        account_id=ACCOUNT, target_type="intent", target_id=intent.intent_id,
        expected_version=None, body=intent_to_request_body(intent),
        source="strategy", principal="strategy",
    )

    receipt = stack.coordinator.execute(request)

    assert receipt.state == "REJECTED"
    assert receipt.error_code == "NO_QUOTE"
    assert stack.ledger.get(intent.command_id).state == "REJECTED"
    assert stack.schedule.calls == 0
    assert stack.dispatch.calls == []
    assert stack.coordinator.execute(request).state == "REJECTED"


def test_session_evidence_uses_the_same_approval_snapshot(tmp_path):
    stack = _build_stack(tmp_path)
    intent = make_intent()
    approval = object()
    session = object()
    seen = []

    def session_factory(*, intent, command, approval):
        seen.append(approval)
        return session

    def start(**kwargs):
        assert kwargs["approval"] is approval
        assert kwargs["session_state"] is session
        return SimpleNamespace(state="SUBMITTING", submitted_order_ids=[501])

    stack.service._approval_factory = lambda **kwargs: approval
    stack.service._session_state_factory = session_factory
    stack.service._protective_saga = SimpleNamespace(start=start)
    request = CommandRequest(
        command_id=intent.command_id, action="execute_automated_intent",
        account_id=ACCOUNT, target_type="intent", target_id=intent.intent_id,
        expected_version=None, body=intent_to_request_body(intent),
        source="strategy", principal="strategy",
    )

    receipt = stack.coordinator.execute(request)

    assert receipt.state == "SUBMITTED"
    assert seen == [approval]


def test_configured_bundle_path_is_not_derived_from_wire_manifest_digest(tmp_path):
    stack = _build_stack(tmp_path)
    intent = make_intent()
    configured = tmp_path / "artifacts" / "0123456789abcdef"
    stack.service._configured_bundle_path = configured
    stack.service._expected_artifact_id = intent.artifact_id
    request = CommandRequest(
        command_id=intent.command_id, action="execute_automated_intent",
        account_id=ACCOUNT, target_type="intent", target_id=intent.intent_id,
        expected_version=None,
        body=intent_to_request_body(intent, bundle_digest="sha256:manifest-ok"),
        source="strategy", principal="strategy",
    )

    receipt = stack.coordinator.execute(request)

    assert receipt.state == "SUBMITTED"
    assert stack.verifier.calls[0]["bundle_path"] == str(configured)


@pytest.mark.parametrize(
    ("configured_id", "bundle_digest", "code"),
    [
        ("artifact-other", "sha256:manifest-ok", "ARTIFACT_NOT_ARMED"),
        (None, "sha256:different-manifest", "BUNDLE_DIGEST_MISMATCH"),
    ],
)
def test_configured_artifact_binding_cannot_be_changed_by_intent(
    tmp_path, configured_id, bundle_digest, code,
):
    stack = _build_stack(tmp_path)
    intent = make_intent()
    stack.service._configured_bundle_path = tmp_path / "configured-artifact"
    stack.service._expected_artifact_id = configured_id or intent.artifact_id
    request = CommandRequest(
        command_id=intent.command_id, action="execute_automated_intent",
        account_id=ACCOUNT, target_type="intent", target_id=intent.intent_id,
        expected_version=None,
        body=intent_to_request_body(intent, bundle_digest=bundle_digest),
        source="strategy", principal="strategy",
    )

    receipt = stack.coordinator.execute(request)

    assert receipt.state == "REJECTED"
    assert receipt.error_code == code
    assert stack.dispatch.calls == []


def test_production_bundle_provenance_is_checked_after_signature_verification(tmp_path):
    from trader.automation.paper_materials import PaperMaterialsError

    stack = _build_stack(tmp_path)
    intent = make_intent()
    checked = []

    def reject_fixture(path):
        assert stack.verifier.calls  # never trust unsigned provenance
        checked.append(path)
        raise PaperMaterialsError("offline fixture is not qualified research")

    stack.service._bundle_evidence_validator = reject_fixture
    request = CommandRequest(
        command_id=intent.command_id, action="execute_automated_intent",
        account_id=ACCOUNT, target_type="intent", target_id=intent.intent_id,
        expected_version=None, body=intent_to_request_body(intent),
        source="strategy", principal="strategy",
    )
    receipt = stack.coordinator.execute(request)

    assert receipt.state == "REJECTED"
    assert receipt.error_code == "ARTIFACT_UNVERIFIED"
    assert len(checked) == 1
    assert stack.dispatch.calls == []


# ---------------------------------------------------------------------------
# Strategy source digest
# ---------------------------------------------------------------------------

class AttestingVerifier(FakeArtifactVerifier):
    def verify(self, bundle_path, expected_mode, expected_artifact_id, now, *, revoked_digests=()):
        artifact = super().verify(bundle_path, expected_mode, expected_artifact_id, now)
        artifact.attested_strategy = SimpleNamespace(source_digest='src-attested')
        return artifact


class UnattestedVerifier(FakeArtifactVerifier):
    """A verifier whose artifact carries no attested strategy at all."""

    def __init__(self, *, keep_attribute_as_none: bool):
        super().__init__()
        self._keep_attribute_as_none = keep_attribute_as_none

    def verify(self, bundle_path, expected_mode, expected_artifact_id, now, *, revoked_digests=()):
        artifact = super().verify(bundle_path, expected_mode, expected_artifact_id, now)
        if self._keep_attribute_as_none:
            artifact.attested_strategy = None
        else:
            del artifact.attested_strategy
        return artifact


def _execute_with_body(stack, tmp_path, body):
    (tmp_path / 'bundles').mkdir()
    (tmp_path / 'bundles' / ARTIFACT_DIGEST.replace(':', '_')).mkdir()
    intent = make_intent()
    return stack.coordinator.execute(CommandRequest(
        command_id=intent.command_id, action='execute_automated_intent', account_id=ACCOUNT,
        target_type='intent', target_id=intent.intent_id, expected_version=None,
        body=body, source='strategy', principal='strategy'))


@pytest.mark.parametrize('sent, expected_error', [
    ('src-attested', None),
    ('src-other', 'STRATEGY_SOURCE_MISMATCH'),
    (None, 'STRATEGY_SOURCE_MISMATCH'),
])
def test_intent_source_digest_must_match_the_attested_file(tmp_path, sent, expected_error):
    stack = _build_stack(tmp_path, verifier=AttestingVerifier())
    intent = make_intent()
    (tmp_path / 'bundles').mkdir()
    (tmp_path / 'bundles' / ARTIFACT_DIGEST.replace(':', '_')).mkdir()
    body = intent_to_request_body(intent)
    body['strategy_source_digest'] = sent
    receipt = stack.coordinator.execute(CommandRequest(
        command_id=intent.command_id, action='execute_automated_intent', account_id=ACCOUNT,
        target_type='intent', target_id=intent.intent_id, expected_version=None,
        body=body, source='strategy', principal='strategy'))
    assert receipt.error_code == expected_error


@pytest.mark.parametrize('keep_attribute_as_none', [True, False])
def test_bundle_without_an_attested_strategy_rejects_the_intent(tmp_path, keep_attribute_as_none):
    stack = _build_stack(
        tmp_path, verifier=UnattestedVerifier(keep_attribute_as_none=keep_attribute_as_none))
    body = intent_to_request_body(make_intent())
    assert body['strategy_source_digest'] == SOURCE_DIGEST

    receipt = _execute_with_body(stack, tmp_path, body)

    assert receipt.state == 'REJECTED'
    assert receipt.error_code == 'STRATEGY_SOURCE_MISMATCH'
    assert 'attests no strategy' in receipt.outcome['detail']
    assert stack.dispatch.calls == []


def test_wire_model_carries_the_strategy_source_digest():
    wire = intent_to_wire(make_intent())
    wire['strategy_source_digest'] = 'src-1'
    parsed = ExecuteAutomatedIntentRequest(**wire)
    assert parsed.model_dump(mode='json')['strategy_source_digest'] == 'src-1'


def test_an_intent_naming_an_artifact_that_is_not_armed_is_rejected(tmp_path):
    stack = _build_stack(tmp_path)
    other = make_intent(artifact_id="artifact-" + "b" * 32)
    (tmp_path / "bundles" / ARTIFACT_DIGEST.replace(":", "_")).mkdir(parents=True)

    receipt = stack.coordinator.execute(CommandRequest(
        command_id=other.command_id, action="execute_automated_intent", account_id=ACCOUNT,
        target_type="intent", target_id=other.intent_id, expected_version=None,
        body=intent_to_request_body(other), source="strategy", principal="strategy"))

    assert (receipt.state, receipt.error_code) == ("REJECTED", "ARTIFACT_NOT_ARMED")
    assert stack.verifier.calls == []
    assert stack.dispatch.calls == []


def test_the_service_refuses_to_start_without_an_armed_artifact(tmp_path):
    from trader.automation.automated_intent_command import AutomatedIntentCommandService

    with pytest.raises(ValueError, match="expected_artifact_id"):
        AutomatedIntentCommandService(
            ledger=None, audit=None, journal=None, controls=None,
            dispatch=FakeIntentDispatch(), artifact_verifier=FakeArtifactVerifier(),
            account_id=ACCOUNT, account_mode="paper", now=lambda: NOW,
            bundle_root=tmp_path / "bundles", expected_artifact_id="")


# ---------------------------------------------------------------------------
# SP1 plan 1 Task 12: SELL intents become a proven-reduction close
# ---------------------------------------------------------------------------

class _FakeCloseLiquidation:
    def __init__(self, *, raise_exc=None, root=None):
        self.starts = []
        self.raise_exc = raise_exc
        self.root = root

    def start(self, account_id, cause_command_id, deadline, **kwargs):
        if self.raise_exc is not None:
            raise self.raise_exc
        self.starts.append((account_id, cause_command_id, deadline, kwargs))
        return SimpleNamespace(cause_command_id=self.root or cause_command_id, state="VERIFYING",
                               generation_id=1, detail="close submitted")


class _FakeBrokerSnapshot:
    def __init__(self, held):
        self.held = held

    def capture(self, account_id):
        return SimpleNamespace(account_id=ACCOUNT, generation_id=1,
                               reducible_quantity=lambda conid: self.held if conid == 265598 else 0.0)


def _execute_sell(stack, tmp_path, requested):
    (tmp_path / "bundles" / ARTIFACT_DIGEST.replace(":", "_")).mkdir(parents=True, exist_ok=True)
    intent = make_intent(side="SELL", requested_quantity=None if requested is None else Decimal(str(requested)))
    return stack.coordinator.execute(CommandRequest(
        command_id=intent.command_id, action="execute_automated_intent", account_id=ACCOUNT,
        target_type="intent", target_id=intent.intent_id, expected_version=None,
        body=intent_to_request_body(intent), source="strategy_service", principal="strategy",
    )), intent


def test_sell_intent_becomes_a_scoped_close_and_never_a_bracket(tmp_path):
    liquidation = _FakeCloseLiquidation()
    stack = _build_stack(tmp_path, liquidation=liquidation, broker=_FakeBrokerSnapshot(10.0))
    receipt, intent = _execute_sell(stack, tmp_path, None)
    assert (receipt.state, receipt.error_code) == ("OUTCOME_UNKNOWN", "CLOSE_PENDING")
    assert receipt.outcome["close_root_id"] == intent.command_id
    _account, root, _deadline, kwargs = liquidation.starts[0]
    assert (root, kwargs) == (intent.command_id, {"scope": "conid", "conid": 265598, "quantity": None})
    assert stack.dispatch.calls == []
    assert stack.schedule.calls == 1          # R17: reconciled from its exact root


@pytest.mark.parametrize("requested,expected", [(4, 4.0), (10, None)])
def test_sell_intent_quantity_is_a_partial_or_a_full_close(tmp_path, requested, expected):
    liquidation = _FakeCloseLiquidation()
    stack = _build_stack(tmp_path, liquidation=liquidation, broker=_FakeBrokerSnapshot(10.0))
    _execute_sell(stack, tmp_path, requested)
    assert liquidation.starts[0][3]["quantity"] == expected


@pytest.mark.parametrize("held,requested", [(0.0, None), (10.0, 11), (-5.0, None)])
def test_sell_that_is_not_a_reduction_is_refused(tmp_path, held, requested):
    liquidation = _FakeCloseLiquidation()
    stack = _build_stack(tmp_path, liquidation=liquidation, broker=_FakeBrokerSnapshot(held))
    receipt, _intent = _execute_sell(stack, tmp_path, requested)
    assert (receipt.state, receipt.error_code) == ("REJECTED", "NOT_A_REDUCTION")
    assert liquidation.starts == [] and stack.dispatch.calls == []


@pytest.mark.parametrize("bad", [1.5, True, "1", 0])
def test_sell_intent_with_an_inexact_conid_is_invalid_before_any_broker_read(tmp_path, bad):
    """#21 round 5: the body's conid is never coerced. The intent ids are derived for conid 1,
    so ``int(bad)`` would have passed every id check and closed conid 1."""
    liquidation = _FakeCloseLiquidation()
    captures = []
    broker = SimpleNamespace(capture=lambda account_id: captures.append(account_id) or _FakeBrokerSnapshot(
        10.0).capture(account_id))
    stack = _build_stack(tmp_path, liquidation=liquidation, broker=broker)
    (tmp_path / "bundles" / ARTIFACT_DIGEST.replace(":", "_")).mkdir(parents=True, exist_ok=True)
    intent = make_intent(side="SELL", conid=1)
    body = dict(intent_to_request_body(intent), conid=bad)
    receipt = stack.coordinator.execute(CommandRequest(
        command_id=intent.command_id, action="execute_automated_intent", account_id=ACCOUNT,
        target_type="intent", target_id=intent.intent_id, expected_version=None,
        body=body, source="strategy_service", principal="strategy"))
    assert (receipt.state, receipt.error_code) == ("REJECTED", "INTENT_INVALID")
    assert (captures, liquidation.starts, stack.dispatch.calls) == ([], [], [])


def _recording_broker(captures, held=10.0):
    return SimpleNamespace(capture=lambda account_id: captures.append(account_id) or SimpleNamespace(
        account_id=ACCOUNT, generation_id=1, reducible_quantity=lambda conid: held))


def test_sell_intent_for_a_conid_outside_the_artifact_allowlist_is_refused_before_any_broker_read(tmp_path):
    """#29/#22 round 6: the armed artifact allowlists only 999999; a SELL of the held 265598 must not
    start a close. Refused before any snapshot, claim or order."""
    liquidation, captures = _FakeCloseLiquidation(), []
    verifier = FakeArtifactVerifier()
    verifier.allowlist = ("999999",)
    stack = _build_stack(tmp_path, liquidation=liquidation, broker=_recording_broker(captures), verifier=verifier)
    receipt, intent = _execute_sell(stack, tmp_path, None)
    assert (receipt.state, receipt.error_code) == ("REJECTED", "CONID_NOT_PERMITTED")
    assert (captures, liquidation.starts, stack.dispatch.calls) == ([], [], [])
    assert stack.ledger.get(intent.command_id).state == "REJECTED"


@pytest.mark.parametrize("bad", [True, "1", 1.0, 0, -1])
def test_typed_intent_request_refuses_an_inexact_conid_before_the_handler(tmp_path, bad):
    """#21 round 6: the typed RPC model is the first parser. It must not turn true, "1" or 1.0 into
    conid 1 (the intent ids are derived for conid 1, and the artifact allowlists 1 here)."""
    from trader.messaging.production_api import _execute_automated_intent_rpc_handler
    from trader.messaging.typed_rpc import _coerce_request_body

    liquidation, captures = _FakeCloseLiquidation(), []
    verifier = FakeArtifactVerifier()
    verifier.allowlist = ("1", "265598")
    stack = _build_stack(tmp_path, liquidation=liquidation, broker=_recording_broker(captures), verifier=verifier)
    (tmp_path / "bundles" / ARTIFACT_DIGEST.replace(":", "_")).mkdir(parents=True, exist_ok=True)
    intent = make_intent(side="SELL", conid=1, requested_quantity=None)
    handler = _execute_automated_intent_rpc_handler(stack.coordinator, ACCOUNT)
    with pytest.raises(ValidationError):
        handler(_coerce_request_body(dict(intent_to_wire(intent), conid=bad), ExecuteAutomatedIntentRequest))
    assert (captures, liquidation.starts) == ([], [])


def test_typed_intent_request_accepts_an_exact_conid(tmp_path):
    liquidation, captures = _FakeCloseLiquidation(), []
    from trader.messaging.production_api import _execute_automated_intent_rpc_handler
    from trader.messaging.typed_rpc import _coerce_request_body

    stack = _build_stack(tmp_path, liquidation=liquidation, broker=_recording_broker(captures))
    (tmp_path / "bundles" / ARTIFACT_DIGEST.replace(":", "_")).mkdir(parents=True, exist_ok=True)
    intent = make_intent(side="SELL", requested_quantity=None)
    handler = _execute_automated_intent_rpc_handler(stack.coordinator, ACCOUNT)
    receipt = handler(_coerce_request_body(intent_to_wire(intent), ExecuteAutomatedIntentRequest),
                      RpcCaller("strategy", None))
    assert receipt["error_code"] == "CLOSE_PENDING" and liquidation.starts[0][3]["conid"] == 265598


def test_sell_intent_refused_while_another_close_owns_the_conid(tmp_path):
    from trader.trading.exit_owner import ExitInProgress
    stack = _build_stack(tmp_path, liquidation=_FakeCloseLiquidation(raise_exc=ExitInProgress("other-root")),
                         broker=_FakeBrokerSnapshot(10.0))
    receipt, _intent = _execute_sell(stack, tmp_path, 3)
    assert (receipt.state, receipt.error_code) == ("REJECTED", "EXIT_IN_PROGRESS")


def test_sell_intent_refused_by_the_partial_quantity_rule(tmp_path):
    from trader.trading.liquidation_service import LiquidationRefused
    stack = _build_stack(tmp_path, liquidation=_FakeCloseLiquidation(
        raise_exc=LiquidationRefused("PARTIAL_QUANTITY_INVALID")), broker=_FakeBrokerSnapshot(10.0))
    receipt, _intent = _execute_sell(stack, tmp_path, 0.4)
    assert (receipt.state, receipt.error_code) == ("REJECTED", "PARTIAL_QUANTITY_INVALID")


def test_joined_sell_records_the_root_it_must_follow(tmp_path):
    stack = _build_stack(tmp_path, liquidation=_FakeCloseLiquidation(root="time-exit-1"),
                         broker=_FakeBrokerSnapshot(10.0))
    receipt, _intent = _execute_sell(stack, tmp_path, None)
    assert receipt.outcome["close_root_id"] == "time-exit-1"


def test_buy_intent_path_is_unchanged_with_close_configured(tmp_path):
    liquidation = _FakeCloseLiquidation()
    stack = _build_stack(tmp_path, liquidation=liquidation, broker=_FakeBrokerSnapshot(0.0))
    (tmp_path / "bundles" / ARTIFACT_DIGEST.replace(":", "_")).mkdir(parents=True)
    intent = make_intent()
    receipt = stack.coordinator.execute(CommandRequest(
        command_id=intent.command_id, action="execute_automated_intent", account_id=ACCOUNT,
        target_type="intent", target_id=intent.intent_id, expected_version=None,
        body=intent_to_request_body(intent), source="strategy_service", principal="strategy",
    ))
    assert receipt.state in ("SUBMITTED", "RESOLVED")
    assert len(stack.dispatch.calls) == 1 and liquidation.starts == []


def _execute_buy(stack, tmp_path):
    (tmp_path / "bundles" / ARTIFACT_DIGEST.replace(":", "_")).mkdir(parents=True, exist_ok=True)
    intent = make_intent()
    return stack.coordinator.execute(CommandRequest(
        command_id=intent.command_id, action="execute_automated_intent", account_id=ACCOUNT,
        target_type="intent", target_id=intent.intent_id, expected_version=None,
        body=intent_to_request_body(intent), source="strategy_service", principal="strategy",
    ))


def test_sell_closes_while_new_exposure_is_paused_and_a_buy_is_refused(tmp_path):
    """R32 / D11: the pause stops new exposure, never an exit."""
    liquidation = _FakeCloseLiquidation()
    stack = _build_stack(tmp_path, liquidation=liquidation, broker=_FakeBrokerSnapshot(10.0))
    stack.controls.set(ACCOUNT, True, None, "pause-1", "test", NOW)
    receipt, _intent = _execute_sell(stack, tmp_path, None)
    assert (receipt.state, receipt.error_code) == ("OUTCOME_UNKNOWN", "CLOSE_PENDING")
    assert len(liquidation.starts) == 1
    buy = _execute_buy(stack, tmp_path)
    assert (buy.state, buy.error_code) == ("REJECTED", "TRADING_PAUSED")


def test_sell_without_the_close_path_is_refused_never_bracketed(tmp_path):
    """R32 / #31: an unwired close path fails loudly; the bracket path would add a reverse stop."""
    stack = _build_stack(tmp_path)
    receipt, _intent = _execute_sell(stack, tmp_path, None)
    assert (receipt.state, receipt.error_code) == ("REJECTED", "CLOSE_PATH_UNAVAILABLE")
    assert stack.dispatch.calls == []


def test_after_a_session_loss_breach_a_sell_closes_and_a_buy_is_refused(tmp_path):
    """#31: session_risk (inside the saga) refuses a BUY after a daily-loss breach; a SELL never reaches it."""
    saga_calls = []

    class _BreachedSaga:
        def start(self, *, intent, **_kwargs):
            saga_calls.append(intent.side)
            return SimpleNamespace(state="CLOSED", error_code="DAILY_LOSS", submitted_order_ids=())

    liquidation = _FakeCloseLiquidation()
    stack = _build_stack(tmp_path, liquidation=liquidation, broker=_FakeBrokerSnapshot(10.0),
                         protective_saga=_BreachedSaga(), approval_factory=lambda **_k: SimpleNamespace())
    buy = _execute_buy(stack, tmp_path)
    assert (buy.state, buy.error_code) == ("REJECTED", "DAILY_LOSS")
    receipt, _intent = _execute_sell(stack, tmp_path, None)
    assert (receipt.state, receipt.error_code) == ("OUTCOME_UNKNOWN", "CLOSE_PENDING")
    assert saga_calls == ["BUY"] and len(liquidation.starts) == 1


def test_sell_intent_closes_even_without_a_saga(tmp_path):
    """The saga guards new exposure only. A SELL exit does not need it; a BUY does
    (test_without_a_saga_the_command_is_refused_and_nothing_is_sent)."""
    liquidation = _FakeCloseLiquidation()
    stack = _build_stack(tmp_path, liquidation=liquidation, broker=_FakeBrokerSnapshot(10.0), with_saga=False)
    receipt, intent = _execute_sell(stack, tmp_path, None)
    assert (receipt.state, receipt.error_code) == ("OUTCOME_UNKNOWN", "CLOSE_PENDING")
    assert liquidation.starts[0][1] == intent.command_id
    assert stack.dispatch.calls == []
