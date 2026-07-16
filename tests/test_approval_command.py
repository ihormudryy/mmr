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

from trader.data.domain_journal import DomainJournal
from trader.data.duckdb_store import DuckDBConnection
from trader.data.proposal_repository import (
    ProposalDraft,
    ProposalRepository,
    apply_proposal_authority_migration,
)
from trader.data.schema_migrations import SchemaMigrator
from trader.trading.command_coordinator import (
    ApprovalCommandService,
    CommandAudit,
    CommandLedger,
    CommandRequest,
    SubmittedOrders,
    TradingCommandCoordinator,
    apply_command_ledger_migration,
)
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

    def set_held(self, conid, quantity):
        self._held[conid] = quantity

    def reducible_quantity(self, account_id, conid):
        return self._held.get(conid, 0.0)


class FakeBroker:
    def __init__(self):
        self.ready = True

    def is_ready(self):
        return self.ready


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
        self._raise = None
        self._next_id = 1001

    def raise_on_submit(self, exc):
        self._raise = exc

    def submit(self, proposal, order_ref, order_group_id):
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
    broker = FakeBroker()
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
        positions=positions, quotes=quotes, risk_gate=risk_gate,
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
