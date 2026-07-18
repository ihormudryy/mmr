"""[M1-F3] Task 5 — the approval command saga.

The approval command is the ONE command that dispatches real orders to the
broker. These are the Step-1 contract tests: they drive the whole saga
through the ``TradingCommandCoordinator`` (the sole production mutation
boundary) via fakes for every collaborator that would otherwise talk to IB.
"""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from trader.data.broker_state import BrokerRiskSnapshotError
from trader.data.domain_journal import DomainJournal
from trader.data.duckdb_store import DuckDBConnection
from trader.data.proposal_repository import (
    ProposalDraft,
    ProposalRepository,
    apply_proposal_authority_migration,
)
from trader.data.schema_migrations import SchemaMigrator
from trader.messaging.production_api import (
    ApproveProposalRequest,
    register_command_authority,
)
from trader.messaging.typed_rpc import TypedRpcRegistry
from trader.trading.command_coordinator import (
    ApprovalCommandService,
    BrokerRejectedError,
    CommandAudit,
    CommandLedger,
    CommandRequest,
    SubmittedOrders,
    TradingCommandCoordinator,
    apply_command_ledger_migration,
)
from trader.trading.dispatch_guard import DispatchGuardError, DispatchPermit
from trader.trading.order_correlation import encode_order_ref
from trader.trading.proposal_command_service import ExecutableQuote, ProposalCommandService
from trader.trading.trading_control import (
    TradingControlStore,
    apply_trading_control_migration,
)

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 7, 16, 14, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fakes for every collaborator the saga touches (none reach IB).
# ---------------------------------------------------------------------------

class FakeQuotes:
    """Synthesizes an ``ExecutableQuote`` from an ``age_seconds`` offset."""

    def __init__(self, now):
        self._now = now
        self._quotes: dict[tuple[int, str], ExecutableQuote] = {}
        self.set(265598, ask=210.0)
        self.set(265598, bid=209.5)

    def set(self, conid, *, ask=None, bid=None, feed_type="live",
            age_seconds=0.0, session_state="continuous"):
        ts = self._now() - dt.timedelta(seconds=age_seconds)
        if ask is not None:
            self._quotes[(conid, "ask")] = ExecutableQuote(
                conid=conid, side="ask", price=ask, market_timestamp=ts,
                feed_type=feed_type, session_state=session_state,
            )
        if bid is not None:
            self._quotes[(conid, "bid")] = ExecutableQuote(
                conid=conid, side="bid", price=bid, market_timestamp=ts,
                feed_type=feed_type, session_state=session_state,
            )

    def executable_quote(self, conid, *, side):
        return self._quotes.get((conid, side))


class FakePositions:
    def __init__(self):
        self._held: dict[int, float] = {}
        self.calls = 0

    def set_held(self, conid, quantity):
        self._held[conid] = quantity

    def reducible_quantity(self, account_id, conid):
        self.calls += 1
        return self._held.get(conid, 0.0)


class FakeBroker:
    def __init__(self, positions, account_id, account_mode):
        self.ready = True
        self.positions = positions
        self.account_id = account_id
        self.account_mode = account_mode
        self.calls = 0

    def capture(self, account_id):
        self.calls += 1
        if not self.ready:
            raise BrokerRiskSnapshotError("BROKER_UNAVAILABLE", "not ready")
        return SimpleNamespace(
            account_id=self.account_id,
            account_mode=self.account_mode,
            generation_id=1,
            source_cursor=1,
            open_order_count=0,
            daily_pnl=0.0,
            net_liquidation=100_000.0,
            working_orders=(),
            reducible_quantity=lambda conid: self.positions._held.get(conid, 0.0),
            position_value=lambda conid: abs(self.positions._held.get(conid, 0.0)) * 210.0,
        )


class FakeReconciler:
    def __init__(self):
        self.scheduled: list[str] = []

    def schedule(self, command_id, now):
        self.scheduled.append(command_id)


class FakeRiskGate:
    def __init__(self):
        self.approved = True

    def evaluate(self, signal=None, **_kwargs):
        return SimpleNamespace(approved=self.approved, reason="risk rejected")


class FakeRiskProducer:
    def __init__(self):
        self.decisions: list[tuple[str, dict]] = []

    def publish_decision(self, command_id, payload, correlation_id=None):
        self.decisions.append((command_id, payload))


class FakeOrders:
    """Test double for ``OrderDispatchPort``: records submissions, can be told
    to raise on the next dispatch (timeout, broker rejection, ...)."""

    def __init__(self):
        self.submissions: list[SubmittedOrders] = []
        self.proposals: list = []              # the ``proposal`` arg each submit received
        self._raise = None
        self._next_id = 1001

    def raise_on_submit(self, exc):
        self._raise = exc

    def submit(self, proposal, order_ref, order_group_id):
        self.proposals.append(proposal)
        if self._raise is not None:
            raise self._raise
        submitted = SubmittedOrders(
            order_group_id=order_group_id, order_ref=order_ref,
            order_ids=[self._next_id],
        )
        self._next_id += 1
        self.submissions.append(submitted)
        return submitted

    def cancel(self, order_entity_id, order_ref):  # pragma: no cover - Task 9
        raise NotImplementedError

    def find_by_order_ref(self, account_id, order_ref):  # pragma: no cover - Task 9
        return []

    def enumeration_complete(self):  # pragma: no cover - Task 9
        return True


class FakeNonceGate:
    def __init__(self):
        self._consumed: set[str] = set()

    def consume_in_tx(self, conn, nonce, request):
        if not nonce or nonce in self._consumed:
            return False
        self._consumed.add(nonce)
        return True


# ---------------------------------------------------------------------------
# Fixture builder
# ---------------------------------------------------------------------------

def _build_approval(tmp_path, *, account_mode, account_id):
    db = DuckDBConnection.get_instance(str(tmp_path / "journal.duckdb"))
    migrator = SchemaMigrator(db)
    journal = DomainJournal(db)
    journal.migrate(migrator)
    apply_proposal_authority_migration(migrator)
    apply_command_ledger_migration(migrator)
    apply_trading_control_migration(migrator)

    repo = ProposalRepository(journal)
    controls = TradingControlStore(journal)
    db.transaction(lambda conn: controls.seed_in_tx(conn, [(account_id, account_mode)], NOW))

    ledger = CommandLedger(journal)
    now = lambda: NOW  # noqa: E731

    quotes = FakeQuotes(now)
    positions = FakePositions()
    broker = FakeBroker(positions, account_id, account_mode)
    reconciler = FakeReconciler()
    orders = FakeOrders()
    risk_gate = FakeRiskGate()
    risk_producer = FakeRiskProducer()

    coordinator = TradingCommandCoordinator(
        journal=journal, ledger=ledger, audit=CommandAudit(journal),
        nonces=FakeNonceGate(), now=now,
    )
    service = ApprovalCommandService(
        journal=journal, ledger=ledger, repo=repo, controls=controls, orders=orders,
        quotes=quotes, risk_gate=risk_gate,
        risk_producer=risk_producer, reconciler=reconciler, broker=broker,
        account_id=account_id, account_mode=account_mode, now=now,
    )
    coordinator.register_action(
        "approve_proposal", service.approve, requires_preflight=True, saga=True,
    )

    def pending(*, conid=265598, action="BUY", quantity=None, reference_price=210.0,
                max_price_drift_bps=50.0, expires_at=None, live_approval_eligible=True):
        pid = repo.reserve_id()
        exp = expires_at if expires_at is not None else NOW + dt.timedelta(minutes=5)
        qty = 10.0 if quantity is None else quantity
        draft = ProposalDraft(
            id=pid, symbol="AAPL", action=action, quantity=qty,
            amount=qty * reference_price, execution={"order_type": "MARKET"},
            reasoning="", confidence=0.7, thesis="", source="dashboard", metadata={},
            sec_type="STK", account_id=account_id, account_mode=account_mode, conid=conid,
            reference_price=reference_price, reference_timestamp=NOW,
            reference_quote_side="ask" if action == "BUY" else "bid",
            reference_feed_type="live", max_price_drift_bps=max_price_drift_bps,
            expires_at=exp, live_approval_eligible=live_approval_eligible, created_at=NOW,
        )
        predicted = ProposalCommandService._record_from_draft(draft, revision=1)
        written: list = []
        journal.mutate(
            journal.connect(),
            repo.mutation_for(predicted, "seed"),
            lambda conn, revision: written.append(repo.insert_pending_in_tx(conn, draft, revision)),
            event_id=f"proposal:{pid}:1",
        )
        return written[0]

    def execute_approve(record, command_id, expected_version=None):
        ev = record.revision if expected_version is None else expected_version
        request = CommandRequest(
            command_id=command_id, action="approve_proposal", account_id=account_id,
            target_type="proposal", target_id=str(record.id), expected_version=ev,
            body={"proposal_id": record.id}, source="dashboard",
            preflight_nonce=f"nonce-{command_id}",
        )
        return coordinator.execute(request)

    return SimpleNamespace(
        db=db, journal=journal, repo=repo, ledger=ledger, controls=controls,
        coordinator=coordinator, service=service, orders=orders, quotes=quotes,
        positions=positions, broker=broker, reconciler=reconciler,
        risk_gate=risk_gate, risk_producer=risk_producer,
        pending=pending, execute_approve=execute_approve, now=now,
    )


@pytest.fixture
def approval(tmp_path):
    return _build_approval(tmp_path, account_mode="paper", account_id="DU111111")


@pytest.fixture
def approval_live(tmp_path):
    return _build_approval(tmp_path, account_mode="live", account_id="U1234567")


# ---------------------------------------------------------------------------
# Step-1 contract tests (verbatim from the brief).
# ---------------------------------------------------------------------------

def test_happy_path_claims_dispatches_and_binds_order_ref(approval):
    record = approval.pending(conid=265598, action="BUY", reference_price=210.0)
    receipt = approval.execute_approve(record, command_id="cmd-1")
    assert receipt.state == "SUBMITTED"
    submitted = approval.orders.submissions[0]
    assert submitted.order_group_id == "og-cmd-1"
    assert submitted.order_ref == encode_order_ref("og-cmd-1")   # [M1-F2] helper: "mmr:og-cmd-1"
    stored = approval.repo.get(record.id)
    assert stored.status == "EXECUTED"                    # storage keeps legacy name ([S0] display maps it)
    assert stored.order_group_id == "og-cmd-1"
    assert stored.revision == record.revision + 2         # claim + submit-link
    # [Finding 2] the write-once risk decision is recorded, exactly once, with
    # the INCREASING-approve payload, BEFORE the claim tx.
    assert approval.risk_producer.decisions == [
        ("cmd-1", {
            "decision": "approve", "direction": "INCREASING",
            "proposal_id": record.id, "generation_id": 1,
            "source_cursor": 1, "quote_timestamp": NOW.isoformat(),
        })
    ]
    assert approval.broker.calls == 1
    assert approval.positions.calls == 0  # approval uses the fenced snapshot, not live state


def test_expired_row_flips_to_expired_inside_the_claiming_transaction(approval):
    record = approval.pending(conid=265598, action="BUY",
                              expires_at=approval.now() - dt.timedelta(seconds=1))
    receipt = approval.execute_approve(record, command_id="cmd-1")
    assert receipt.state == "REJECTED" and receipt.error_code == "PROPOSAL_EXPIRED"
    assert approval.repo.get(record.id).status == "EXPIRED"
    assert approval.orders.submissions == []


def test_revision_mismatch_rejects_without_dispatch(approval):
    record = approval.pending(conid=265598, action="BUY")
    receipt = approval.execute_approve(record, command_id="cmd-1",
                                       expected_version=record.revision + 5)
    assert receipt.error_code == "REVISION_MISMATCH"
    assert approval.orders.submissions == []


def test_drift_beyond_recorded_guard_rejects(approval):
    record = approval.pending(conid=265598, action="BUY",
                              reference_price=210.0, max_price_drift_bps=50.0)
    approval.quotes.set(265598, ask=212.0)                # ~95 bps drift
    receipt = approval.execute_approve(record, command_id="cmd-1")
    assert receipt.error_code == "PRICE_DRIFT_EXCEEDED"
    # [Finding 2] the risk decision is recorded EVEN on the reject path — the
    # publish_decision call precedes the reject branch — exactly once.
    assert approval.risk_producer.decisions == [
        ("cmd-1", {"decision": "reject", "code": "PRICE_DRIFT_EXCEEDED", "proposal_id": record.id})
    ]


def test_live_mode_requires_fresh_live_executable_side_quote(approval_live):
    record = approval_live.pending(conid=265598, action="BUY")
    approval_live.quotes.set(265598, ask=210.0, feed_type="delayed")
    assert approval_live.execute_approve(record, "c1").error_code == "FEED_NOT_LIVE"
    approval_live.quotes.set(265598, ask=210.0, feed_type="live",
                             age_seconds=6.0)
    assert approval_live.execute_approve(record, "c2").error_code == "QUOTE_STALE"
    approval_live.quotes.set(265598, ask=210.0, feed_type="live",
                             session_state="closed")
    assert approval_live.execute_approve(record, "c3").error_code == "SESSION_INCOMPATIBLE"


def test_live_ineligible_row_cannot_be_approved_live(approval_live):
    record = approval_live.pending(conid=265598, action="BUY", live_approval_eligible=False)
    assert approval_live.execute_approve(record, "c1").error_code == "LIVE_INELIGIBLE"


def test_position_reducing_exit_is_exempt_but_quantity_capped(approval):
    approval.positions.set_held(265598, 100.0)
    approval.quotes.set(265598, bid=205.0, feed_type="delayed", age_seconds=600.0)
    close = approval.pending(conid=265598, action="SELL", quantity=100.0)
    assert approval.execute_approve(close, "c1").state == "SUBMITTED"   # stale feed tolerated
    oversized = approval.pending(conid=265598, action="SELL", quantity=150.0)
    receipt = approval.execute_approve(oversized, "c2")
    assert receipt.error_code == "REDUCIBLE_QUANTITY_EXCEEDED"


def test_broker_unhealthy_rejects_retryably(approval):
    approval.broker.ready = False
    record = approval.pending(conid=265598, action="BUY")
    receipt = approval.execute_approve(record, "c1")
    assert receipt.error_code == "BROKER_UNAVAILABLE" and receipt.retryable is True


def test_dispatch_timeout_becomes_outcome_unknown_never_failed(approval):
    record = approval.pending(conid=265598, action="BUY")
    approval.orders.raise_on_submit(TimeoutError("ib ack timeout"))
    receipt = approval.execute_approve(record, "cmd-1")
    assert receipt.state == "OUTCOME_UNKNOWN"
    assert approval.repo.get(record.id).status == "APPROVED"    # ambiguous: not FAILED
    assert approval.reconciler.scheduled == ["cmd-1"]
    # [Finding 3] the durable proposal->group link the Task-9 reconciler needs
    # is written in the claim tx BEFORE dispatch, so it survives the ambiguity.
    stored = approval.repo.get(record.id)
    assert stored.order_group_id == "og-cmd-1"
    assert receipt.error_code == "DISPATCH_AMBIGUOUS"
    assert receipt.retryable is False                           # NEVER auto-retry ambiguous real money
    assert approval.ledger.get("cmd-1").state == "OUTCOME_UNKNOWN"


def test_pre_dispatch_revalidation_failure_never_reaches_order_dispatch(approval):
    class Guard:
        def __init__(self):
            self.calls = 0

        def revalidate(self, approved, request, now):
            self.calls += 1
            if self.calls == 2:
                raise DispatchGuardError(
                    "BROKER_STATE_CHANGED", "position changed before dispatch",
                    retryable=True,
                )
            return DispatchPermit(1, 1, NOW, NOW)

    guard = Guard()
    approval.service._dispatch_guard = guard
    record = approval.pending(conid=265598, action="BUY")

    receipt = approval.execute_approve(record, command_id="cmd-guard")

    assert guard.calls == 2
    assert receipt.state == "REJECTED"
    assert receipt.error_code == "BROKER_STATE_CHANGED"
    assert approval.orders.submissions == []
    assert approval.repo.get(record.id).status == "FAILED"
    assert approval.risk_producer.decisions[-1] == (
        "cmd-guard/dispatch",
        {"decision": "reject", "code": "BROKER_STATE_CHANGED", "proposal_id": record.id},
    )


def test_dispatch_approval_records_refreshed_fence_and_market_evidence(approval):
    class Guard:
        def revalidate(self, approved, request, now):
            return DispatchPermit(3, 27, NOW, NOW)

    approval.service._dispatch_guard = Guard()
    record = approval.pending(conid=265598, action="BUY")

    receipt = approval.execute_approve(record, command_id="cmd-permit")

    assert receipt.state == "SUBMITTED"
    assert approval.risk_producer.decisions[-1] == (
        "cmd-permit/dispatch",
        {
            "decision": "approve", "proposal_id": record.id,
            "generation_id": 3, "source_cursor": 27,
            "quote_timestamp": NOW.isoformat(),
            "what_if_timestamp": NOW.isoformat(),
            "warnings": [],
        },
    )


# ---------------------------------------------------------------------------
# [Finding 1] broker-rejection leg: the only proposal-FAILED path.
# ---------------------------------------------------------------------------

def test_broker_rejection_marks_proposal_failed(approval):
    record = approval.pending(conid=265598, action="BUY")
    approval.orders.raise_on_submit(BrokerRejectedError("Order rejected by IB"))
    receipt = approval.execute_approve(record, command_id="cmd-1")

    # A definite negative outcome: proposal FAILED, command REJECTED, no retry.
    assert receipt.state == "REJECTED"
    assert receipt.error_code == "BROKER_REJECTED"
    assert receipt.retryable is False

    stored = approval.repo.get(record.id)
    assert stored.status == "FAILED"
    assert stored.rejection_reason == "Order rejected by IB"    # non-None reason recorded
    assert stored.revision == record.revision + 2               # claim + fail-mark

    # A DEFINITE negative outcome must never be scheduled for reconciliation.
    assert approval.reconciler.scheduled == []


# ---------------------------------------------------------------------------
# [Finding 4] one saga-level test per error-code row.
# ---------------------------------------------------------------------------

def test_risk_rejected_stays_pending_and_never_dispatches(approval):
    approval.risk_gate.approved = False
    record = approval.pending(conid=265598, action="BUY")
    receipt = approval.execute_approve(record, command_id="cmd-1")
    assert receipt.error_code == "RISK_REJECTED"
    assert approval.repo.get(record.id).status == "PENDING"     # not FAILED / not EXECUTED
    assert approval.orders.submissions == []


def _foreign_pending(approval, *, foreign_account_id, conid=265598, action="BUY",
                     quantity=10.0, reference_price=210.0):
    """Insert a PENDING proposal owned by a DIFFERENT account than the service.

    Mirrors the fixture's ``pending()`` closure (which hardcodes the service's
    own ``account_id``) but with a caller-supplied foreign ``account_id`` so the
    WRONG_ACCOUNT guard can be exercised.
    """
    repo = approval.repo
    journal = approval.journal
    pid = repo.reserve_id()
    draft = ProposalDraft(
        id=pid, symbol="AAPL", action=action, quantity=quantity,
        amount=quantity * reference_price, execution={"order_type": "MARKET"},
        reasoning="", confidence=0.7, thesis="", source="dashboard", metadata={},
        sec_type="STK", account_id=foreign_account_id, account_mode="paper", conid=conid,
        reference_price=reference_price, reference_timestamp=NOW,
        reference_quote_side="ask" if action == "BUY" else "bid",
        reference_feed_type="live", max_price_drift_bps=50.0,
        expires_at=NOW + dt.timedelta(minutes=5), live_approval_eligible=True, created_at=NOW,
    )
    predicted = ProposalCommandService._record_from_draft(draft, revision=1)
    written: list = []
    journal.mutate(
        journal.connect(),
        repo.mutation_for(predicted, "seed"),
        lambda conn, revision: written.append(repo.insert_pending_in_tx(conn, draft, revision)),
        event_id=f"proposal:{pid}:1",
    )
    return written[0]


def test_foreign_account_proposal_is_rejected_wrong_account(approval):
    # Service account is DU111111; this proposal belongs to a different account.
    record = _foreign_pending(approval, foreign_account_id="DFOREIGN9")
    receipt = approval.execute_approve(record, command_id="cmd-1")
    assert receipt.error_code == "WRONG_ACCOUNT"
    assert approval.orders.submissions == []


def test_command_in_flight_when_a_prior_command_wedged_non_terminal(approval):
    record = approval.pending(conid=265598, action="BUY")

    # Wedge the FIRST command pre-claim: make publish_decision raise once so the
    # coordinator's broad-except leaves it at OUTCOME_UNKNOWN (non-terminal) with
    # the proposal still PENDING (never claimed).
    raised: list = []

    def flaky(command_id, payload, correlation_id=None):
        if not raised:
            raised.append(True)
            raise RuntimeError("audit sink down")
        approval.risk_producer.decisions.append((command_id, payload))

    approval.risk_producer.publish_decision = flaky
    with pytest.raises(RuntimeError):
        approval.execute_approve(record, command_id="cmd-1")
    assert approval.ledger.get("cmd-1").state == "OUTCOME_UNKNOWN"
    assert approval.repo.get(record.id).status == "PENDING"

    # A NEW command for the SAME proposal sees the in-flight (unresolved) one.
    receipt = approval.execute_approve(record, command_id="cmd-2")
    assert receipt.error_code == "COMMAND_IN_FLIGHT"
    assert receipt.retryable is True
    assert approval.orders.submissions == []


def test_source_clock_skew_rejects_future_dated_live_quote(approval_live):
    record = approval_live.pending(conid=265598, action="BUY")
    # Future timestamp beyond MAX_SOURCE_CLOCK_SKEW_SECONDS (30s): age = -31.
    approval_live.quotes.set(265598, ask=210.0, feed_type="live", age_seconds=-31)
    receipt = approval_live.execute_approve(record, command_id="cmd-1")
    assert receipt.error_code == "SOURCE_CLOCK_SKEW"
    assert approval_live.orders.submissions == []


def test_executable_quote_missing_rejects_retryably(approval):
    # A conid FakeQuotes never `.set(...)`: no ask quote exists for it.
    record = approval.pending(conid=424242, action="BUY")
    receipt = approval.execute_approve(record, command_id="cmd-1")
    assert receipt.error_code == "EXECUTABLE_QUOTE_MISSING"
    assert receipt.retryable is True
    assert approval.orders.submissions == []


def test_trading_paused_blocks_increasing_but_exempts_reducing(approval):
    # Pause the service's own account (paper seeds unpaused at revision 1).
    approval.controls.set("DU111111", True, None, "pause-1", "manual halt", NOW)

    # INCREASING BUY is blocked by the in-tx pause re-check; proposal untouched.
    buy = approval.pending(conid=265598, action="BUY")
    receipt = approval.execute_approve(buy, command_id="cmd-buy")
    assert receipt.error_code == "TRADING_PAUSED"
    assert receipt.retryable is True
    assert approval.repo.get(buy.id).status == "PENDING"        # not claimed
    assert approval.orders.submissions == []

    # REDUCING SELL of a verified held quantity is pause-EXEMPT and dispatches.
    approval.positions.set_held(265598, 100.0)
    sell = approval.pending(conid=265598, action="SELL", quantity=100.0)
    reducing = approval.execute_approve(sell, command_id="cmd-sell")
    assert reducing.state == "SUBMITTED"
    assert approval.orders.submissions[-1].order_group_id == "og-cmd-sell"


# ---------------------------------------------------------------------------
# [Finding 5] the production approve_proposal RPC surface.
# ---------------------------------------------------------------------------

def _minimal_proposal_service(approval):
    """A real ``ProposalCommandService`` for the ``register_command_authority``
    signature. Its create/reject actions are never driven in this test — only
    the ``approve_proposal`` saga is — so the unused collaborators are simple
    fakes; the object itself is real."""
    return ProposalCommandService(
        repository=approval.repo, journal=approval.journal, risk_gate=approval.risk_gate,
        quotes=approval.quotes, universe=SimpleNamespace(resolve_conid=lambda conid: None),
        account_id="DU111111", account_mode="paper", now=approval.now,
        controls=approval.controls, positions=approval.positions,
    )


def test_approve_proposal_rpc_surface_registers_and_drives_a_real_approve(approval):
    registry = TypedRpcRegistry()
    register_command_authority(
        registry, approval.coordinator, _minimal_proposal_service(approval), approval.repo,
        account_id="DU111111", controls=approval.controls, approval_service=approval.service,
    )

    # (register branch) approve_proposal is wired on the command role.
    assert registry.contains("command", "approve_proposal")

    # Drive ONE approve through the REAL typed handler with the REAL request.
    record = approval.pending(conid=265598, action="BUY")
    registration = registry.resolve("command", "approve_proposal")
    parsed = ApproveProposalRequest(
        command_id="cmd-rpc", proposal_id=record.id, expected_version=record.revision,
        preflight_nonce="nonce-cmd-rpc",
    )
    result = registration.handler(parsed)
    assert result["state"] == "SUBMITTED"
    assert result["command_id"] == "cmd-rpc"
    assert approval.repo.get(record.id).status == "EXECUTED"


def test_approve_proposal_request_model_rejects_extra_field():
    # extra='forbid'
    with pytest.raises(ValidationError):
        ApproveProposalRequest(
            command_id="cmd-1", proposal_id=1, expected_version=1, bogus="x",
        )


def test_approve_proposal_request_model_rejects_colon_command_id():
    # command_id colon reservation (encode_order_ref mmr: prefix).
    with pytest.raises(ValidationError):
        ApproveProposalRequest(command_id="bad:id", proposal_id=1, expected_version=1)


def test_approve_proposal_request_model_requires_expected_version():
    with pytest.raises(ValidationError):
        ApproveProposalRequest(command_id="cmd-1", proposal_id=1)


# ---------------------------------------------------------------------------
# Re-verified correctness fixes on the dispatch saga (fix2 set).
# ---------------------------------------------------------------------------

def test_finish_tx_failure_after_dispatch_degrades_to_ambiguous(approval):
    # Fix 1 (FINISH-TX-AFTER-DISPATCH): the bracket is LIVE at the broker
    # (submit returned) but the post-dispatch finish tx fails. The known
    # order_ids must be preserved and the reconciler scheduled -- NOT lost to a
    # generic INTERNAL_ERROR with an empty schedule.
    record = approval.pending(conid=265598, action="BUY")
    # mark_order_submitted_in_tx returns None -> the saga's
    # _ConcurrentProposalChange inside the finish tx.
    approval.repo.mark_order_submitted_in_tx = lambda *a, **k: None

    receipt = approval.execute_approve(record, command_id="cmd-1")

    assert receipt.state == "OUTCOME_UNKNOWN"
    assert receipt.error_code == "DISPATCH_AMBIGUOUS"
    assert receipt.retryable is False                       # never auto-retry ambiguous real money
    assert receipt.outcome is not None
    assert receipt.outcome["order_ids"] == approval.orders.submissions[0].order_ids
    # the live order was dispatched -> the Task-9 reconciler must resolve it
    assert approval.reconciler.scheduled == ["cmd-1"]
    # the claim committed, so the proposal stays APPROVED (NOT FAILED)
    assert approval.repo.get(record.id).status == "APPROVED"
    assert approval.ledger.get("cmd-1").state == "OUTCOME_UNKNOWN"


def test_outcome_unknown_replay_is_not_retryable(approval):
    # Fix 2 (RJ3): OUTCOME_UNKNOWN is not in _TERMINAL_STATES, so a get_command
    # / same-command replay must NOT report retryable=True -- it would
    # contradict the saga's explicit retryable=False on the ambiguity.
    record = approval.pending(conid=265598, action="BUY")
    approval.orders.raise_on_submit(TimeoutError("ib ack timeout"))
    first = approval.execute_approve(record, command_id="cmd-1")
    assert first.state == "OUTCOME_UNKNOWN"
    assert first.retryable is False

    fetched = approval.coordinator.get_command("cmd-1")
    assert fetched.state == "OUTCOME_UNKNOWN"
    assert fetched.retryable is False

    # idempotent same-command_id replay must match the original (not retryable)
    replay = approval.execute_approve(record, command_id="cmd-1")
    assert replay.state == "OUTCOME_UNKNOWN"
    assert replay.retryable is False


def test_dispatch_uses_the_claimed_record_not_a_reread(approval):
    # Fix 3 (SA5): dispatch must pass the in-hand claimed_record, not a
    # redundant re-read inside the ambiguity window.
    record = approval.pending(conid=265598, action="BUY")

    calls = {"n": 0}
    real_get = approval.repo.get

    def counting_get(pid):
        calls["n"] += 1
        return real_get(pid)

    approval.repo.get = counting_get
    receipt = approval.execute_approve(record, command_id="cmd-1")
    reads_during_saga = calls["n"]          # capture BEFORE assertion-time reads
    approval.repo.get = real_get            # restore so assertions don't inflate

    assert receipt.state == "SUBMITTED"
    # the dispatched proposal is the claimed_record carrying the order_group_id
    dispatched = approval.orders.proposals[0]
    assert dispatched.order_group_id == "og-cmd-1"
    # and no redundant post-claim re-read happened inside the ambiguity window
    assert reads_during_saga == 1


def test_validation_order_matches_spec(approval):
    # Fix 4 (SA6): design-spec 9.2 pins "exists -> PENDING -> expected_version"
    # ahead of account/mode. A PENDING foreign-account proposal approved with a
    # WRONG expected_version must report REVISION_MISMATCH (version before
    # account), not WRONG_ACCOUNT.
    record = _foreign_pending(approval, foreign_account_id="DFOREIGN9")
    receipt = approval.execute_approve(record, command_id="cmd-1",
                                       expected_version=record.revision + 5)
    assert receipt.error_code == "REVISION_MISMATCH"
    assert approval.orders.submissions == []
