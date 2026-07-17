"""[M1-F3] Task 3 — command ledger, audit, and TradingCommandCoordinator."""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from trader.data.domain_journal import DomainJournal
from trader.data.duckdb_store import DuckDBConnection
from trader.data.proposal_repository import ProposalRepository, apply_proposal_authority_migration
from trader.data.schema_migrations import SchemaMigrator
from trader.messaging.production_api import RejectProposalRequest, build_production_registry
from trader.messaging.typed_rpc import HmacServiceAuthenticator
from trader.trading.command_coordinator import (
    ApprovalCommandService,
    BrokerRejectedError,
    CommandAudit,
    CommandLedger,
    CommandRequest,
    CommandValidationError,
    CRITICAL_AFTER_SECONDS,
    IllegalCommandTransition,
    OutcomeReconciler,
    RECONCILE_DELAYS,
    ReconcileResult,
    SubmittedOrders,
    TradingCommandCoordinator,
    apply_command_ledger_migration,
    canonical_request_hash,
)
from trader.data.broker_state import BrokerOrderRow
from trader.data.proposal_repository import ProposalDraft
from trader.strategy.strategy_revisions import StrategyCommandReceipt
from trader.trading.order_correlation import encode_order_ref
from trader.trading.proposal_command_service import (
    ExecutableQuote,
    ProposalCommandService,
    ProposalCreateRequest,
)
from trader.trading.trading_control import TradingControlStore, apply_trading_control_migration

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 7, 16, 14, 0, tzinfo=UTC)


def _request(command_id="cmd-1", action="reject_proposal", body=None, nonce=None, account_id="DU111111"):
    return CommandRequest(
        command_id=command_id, action=action, account_id=account_id,
        target_type="proposal", target_id="7",
        expected_version=None, body=body or {"proposal_id": 7, "reason": "x"},
        source="dashboard", preflight_nonce=nonce)


class FakeNonceGate:
    """Test double for ``PreflightNonceGate``: each nonce is single-use."""

    def __init__(self):
        self._consumed: set[str] = set()
        self._fail_all = False

    def fail_all(self) -> None:
        self._fail_all = True

    def consume_in_tx(self, conn, nonce, request) -> bool:
        if self._fail_all or not nonce or nonce in self._consumed:
            return False
        self._consumed.add(nonce)
        return True


class FakeCommandAudit:
    """Test double for ``CommandAudit``: can be told to fail the next write."""

    def __init__(self):
        self._fail_next = False
        self.records: list[str] = []

    def fail_next(self) -> None:
        self._fail_next = True

    def record_in_tx(self, conn, request, **_kwargs) -> None:
        if self._fail_next:
            self._fail_next = False
            raise RuntimeError("audit sink unavailable")
        self.records.append(request.command_id)


@pytest.fixture
def db(tmp_path):
    return DuckDBConnection.get_instance(str(tmp_path / "journal.duckdb"))


@pytest.fixture
def journal(db):
    migrator = SchemaMigrator(db)
    j = DomainJournal(db)
    j.migrate(migrator)
    apply_command_ledger_migration(migrator)
    return j


@pytest.fixture
def ledger(journal):
    return CommandLedger(journal)


@pytest.fixture
def now():
    return NOW


@pytest.fixture
def coordinator(journal, ledger):
    coord = TradingCommandCoordinator(
        journal=journal, ledger=ledger, audit=FakeCommandAudit(),
        nonces=FakeNonceGate(), now=lambda: NOW,
    )
    return coord


# ---------------------------------------------------------------------------
# Command-ledger ordering
# ---------------------------------------------------------------------------

def test_exact_retry_returns_recorded_state_before_nonce_validation(coordinator):
    coordinator.register_action("noop", lambda cmd: {"ok": True}, requires_preflight=True)
    request = _request(action="noop", nonce="nonce-1")
    first = coordinator.execute(request)
    assert first.state == "RESOLVED"
    # The nonce is consumed. An exact retry must return the recorded outcome
    # WITHOUT re-validating the nonce (spec §9.1).
    coordinator._nonces.fail_all()
    retry = coordinator.execute(request)
    assert retry.state == "RESOLVED" and retry.outcome == first.outcome


def test_same_command_id_with_different_payload_is_a_conflict(coordinator):
    coordinator.register_action("noop", lambda cmd: {"ok": True}, requires_preflight=False)
    coordinator.execute(_request(action="noop", body={"proposal_id": 7, "reason": "x"}))
    conflict = coordinator.execute(_request(action="noop", body={"proposal_id": 8, "reason": "x"}))
    assert conflict.error_code == "COMMAND_CONFLICT" and conflict.retryable is False


def test_ledger_row_exists_before_validation_runs(coordinator, ledger):
    seen = {}

    def handler(cmd):
        seen["row"] = ledger.get(cmd.command_id)
        raise CommandValidationError("RISK_REJECTED", "concentration too high")

    coordinator.register_action("noop", handler, requires_preflight=False)
    receipt = coordinator.execute(_request(action="noop"))
    assert seen["row"].state == "RECEIVED"        # insert precedes validation
    assert receipt.state == "REJECTED" and receipt.error_code == "RISK_REJECTED"


def test_audit_write_failure_fails_closed(coordinator):
    dispatched = []
    coordinator.register_action("noop", lambda cmd: dispatched.append(cmd) or {}, requires_preflight=False)
    coordinator._audit.fail_next()
    receipt = coordinator.execute(_request(action="noop"))
    assert receipt.error_code == "AUDIT_UNAVAILABLE" and receipt.state == "REJECTED"
    assert dispatched == []                        # nothing reached the handler
    assert coordinator._ledger.get("cmd-1") is None  # nothing committed at all


def test_nonce_failure_leaves_no_ledger_row(coordinator, ledger):
    coordinator.register_action("noop", lambda cmd: {"ok": True}, requires_preflight=True)
    receipt = coordinator.execute(_request(action="noop", nonce=None))
    assert receipt.error_code == "PREFLIGHT_REQUIRED" and receipt.state == "REJECTED"
    assert ledger.get("cmd-1") is None


def test_command_transitions_journal_command_updated(coordinator, journal):
    coordinator.register_action("noop", lambda cmd: {"ok": True}, requires_preflight=False)
    coordinator.execute(_request(action="noop"))
    kinds = [e.event_type for e in journal.read_after(0, 100)]
    assert kinds == ["command.updated", "command.updated"]   # RECEIVED, RESOLVED


def test_illegal_transition_raises(ledger, journal, now):
    with pytest.raises(IllegalCommandTransition):
        conn = journal.connect()
        ledger.transition_in_tx(conn, "does-not-exist", "RECEIVED", "RESOLVED")


# ---------------------------------------------------------------------------
# Unexpected (non-CommandValidationError) handler failures must not wedge
# the command at RECEIVED forever.
# ---------------------------------------------------------------------------

def test_unexpected_handler_exception_reraises_and_wedges_to_outcome_unknown(coordinator, ledger):
    def handler(cmd):
        raise RuntimeError("boom: handler's own mutate() blew up")

    coordinator.register_action("noop", handler, requires_preflight=False)
    request = _request(action="noop")

    # (a) execute() re-raises the original exception -- the surface fails
    # loud, it is not swallowed into some receipt.
    with pytest.raises(RuntimeError, match="boom"):
        coordinator.execute(request)

    # (b) the ledger row is left in OUTCOME_UNKNOWN with error_code
    # INTERNAL_ERROR, not stuck at RECEIVED.
    row = ledger.get("cmd-1")
    assert row is not None
    assert row.state == "OUTCOME_UNKNOWN"
    assert row.error_code == "INTERNAL_ERROR"

    # (c) a subsequent replay of the SAME command_id (exact-retry lookup by
    # command_id + matching request_hash) returns the ledger's actual
    # OUTCOME_UNKNOWN state -- NOT a re-run of the handler and NOT a
    # falsely "still pending" RECEIVED.
    replay = coordinator.execute(request)
    assert replay.state == "OUTCOME_UNKNOWN"
    assert replay.error_code == "INTERNAL_ERROR"


def test_unexpected_handler_exception_does_not_affect_the_validation_error_path(coordinator):
    """The existing CommandValidationError -> REJECTED path must be
    unchanged by the new broad `except Exception` clause."""
    def handler(cmd):
        raise CommandValidationError("RISK_REJECTED", "concentration too high")

    coordinator.register_action("noop", handler, requires_preflight=False)
    receipt = coordinator.execute(_request(action="noop"))
    assert receipt.state == "REJECTED"
    assert receipt.error_code == "RISK_REJECTED"


def test_outcome_unknown_from_unexpected_error_survives_purge_expired(ledger, now):
    """Mirrors test_retention_purges_terminal_but_never_unknown: an
    OUTCOME_UNKNOWN row produced by the wedged-RECEIVED fix must never be
    purged, no matter how old it is -- it stays visible until a human/
    automated reconciler actually resolves it (see purge_expired's own
    docstring)."""
    ledger.insert_for_test(
        "wedged-old", state="OUTCOME_UNKNOWN", updated_at=now - dt.timedelta(days=400),
    )
    assert ledger.purge_expired(now) == 0
    assert ledger.get("wedged-old") is not None


def test_colon_bearing_command_id_is_rejected():
    with pytest.raises(ValueError, match="colon|:"):
        _request(command_id="bad:id")


def test_canonical_request_hash_ignores_source_and_nonce():
    a = _request(action="noop", nonce="n1")
    b = CommandRequest(
        command_id=a.command_id, action=a.action, account_id=a.account_id,
        target_type=a.target_type, target_id=a.target_id,
        expected_version=a.expected_version, body=a.body,
        source="cli", preflight_nonce="n2",
    )
    assert canonical_request_hash(a) == canonical_request_hash(b)


# ---------------------------------------------------------------------------
# Retention: purge terminal, never OUTCOME_UNKNOWN
# ---------------------------------------------------------------------------

def test_retention_purges_terminal_but_never_unknown(ledger, now):
    ledger.insert_for_test("old-resolved", state="RESOLVED", updated_at=now - dt.timedelta(days=31))
    ledger.insert_for_test("old-unknown", state="OUTCOME_UNKNOWN", updated_at=now - dt.timedelta(days=31))
    assert ledger.purge_expired(now) == 1
    assert ledger.get("old-resolved") is None
    assert ledger.get("old-unknown") is not None


def test_retention_keeps_recent_terminal_rows(ledger, now):
    ledger.insert_for_test("recent-resolved", state="RESOLVED", updated_at=now - dt.timedelta(days=1))
    assert ledger.purge_expired(now) == 0
    assert ledger.get("recent-resolved") is not None


# ---------------------------------------------------------------------------
# Production registry: no risk-limit admin / bypass on the command socket
# ---------------------------------------------------------------------------

class _FakeIB:
    def accountValues(self):
        return []

    def managedAccounts(self):
        return ["DU111111"]


class _FakeClient:
    def __init__(self):
        self.ib = _FakeIB()


class _FakeTrader:
    def __init__(self):
        self.ib_account = "DU111111"
        self.client = _FakeClient()

    def status(self) -> dict:
        return {"ib_connected": True, "ib_upstream_connected": True}


@pytest.fixture
def authority(tmp_path):
    db_ = DuckDBConnection.get_instance(str(tmp_path / "journal2.duckdb"))
    migrator = SchemaMigrator(db_)
    journal_ = DomainJournal(db_)
    journal_.migrate(migrator)
    apply_proposal_authority_migration(migrator)
    apply_command_ledger_migration(migrator)
    repository = ProposalRepository(journal_)
    ledger_ = CommandLedger(journal_)

    quotes = SimpleNamespace(executable_quote=lambda conid, side: ExecutableQuote(
        conid=265598, side="ask", price=210.0, market_timestamp=NOW,
        feed_type="live", session_state="continuous",
    ) if conid == 265598 else None)
    universe = SimpleNamespace(resolve_conid=lambda conid: SimpleNamespace(
        conId=conid, symbol="AAPL", primaryExchange="NASDAQ", secType="STK",
        exchange="SMART", currency="USD",
    ) if conid == 265598 else None)
    risk_gate = SimpleNamespace(check_instrument=lambda **_kw: SimpleNamespace(approved=True, reason=""))

    proposal_service = ProposalCommandService(
        repository=repository, journal=journal_, risk_gate=risk_gate, quotes=quotes,
        universe=universe, account_id="DU111111", account_mode="paper", now=lambda: NOW,
    )
    coordinator = TradingCommandCoordinator(
        journal=journal_, ledger=ledger_, audit=FakeCommandAudit(), nonces=FakeNonceGate(),
        now=lambda: NOW,
    )
    return SimpleNamespace(
        journal=journal_, repository=repository, ledger=ledger_,
        proposal_service=proposal_service, coordinator=coordinator,
    )


@pytest.fixture
def production_registry(authority):
    authenticator = HmacServiceAuthenticator(b"k" * 32, now=lambda: 1_700_000_000.0)
    return build_production_registry(
        _FakeTrader(), authenticator,
        command_coordinator=authority.coordinator,
        proposal_service=authority.proposal_service,
        proposal_repository=authority.repository,
    )


def test_no_risk_limit_admin_or_bypass_on_the_command_socket(production_registry):
    for method in ("set_risk_limits", "place_order_simple", "place_expressive_order"):
        assert not production_registry.contains("command", method)
    with pytest.raises(ValidationError):
        RejectProposalRequest(command_id="c", proposal_id=7, reason="x", skip_risk_gate=True)


def test_command_authority_methods_are_registered(production_registry):
    assert production_registry.contains("command", "create_proposal")
    assert production_registry.contains("command", "reject_proposal")
    assert production_registry.contains("query", "get_command")
    assert production_registry.contains("query", "get_proposal")
    assert production_registry.contains("query", "list_proposals")


def test_create_and_reject_proposal_over_the_registry(production_registry):
    create_reg = production_registry.resolve("command", "create_proposal")
    from trader.messaging.production_api import CreateProposalRequest
    parsed = CreateProposalRequest(command_id="cmd-create-1", conid=265598, action="BUY", quantity=10)
    receipt = create_reg.handler(parsed)
    assert receipt["state"] == "RESOLVED"
    proposal_id = receipt["outcome"]["id"]

    reject_reg = production_registry.resolve("command", "reject_proposal")
    from trader.messaging.production_api import RejectProposalRequest as RPR
    parsed_reject = RPR(command_id="cmd-reject-1", proposal_id=proposal_id, reason="changed thesis")
    reject_receipt = reject_reg.handler(parsed_reject)
    assert reject_receipt["state"] == "RESOLVED"
    assert reject_receipt["outcome"]["status"] == "REJECTED"

    get_proposal_reg = production_registry.resolve("query", "get_proposal")
    from trader.messaging.production_api import GetProposalRequest
    fetched = get_proposal_reg.handler(GetProposalRequest(proposal_id=proposal_id))
    assert fetched["status"] == "REJECTED"

    get_command_reg = production_registry.resolve("query", "get_command")
    from trader.messaging.production_api import GetCommandRequest
    command_state = get_command_reg.handler(GetCommandRequest(command_id="cmd-create-1"))
    assert command_state["state"] == "RESOLVED"


# ---------------------------------------------------------------------------
# A colon-bearing command_id must be rejected as a clean client-input
# validation error, not surface as an opaque INTERNAL_ERROR deep inside the
# handler (CommandRequest.__post_init__ is defense-in-depth, not the only
# check).
# ---------------------------------------------------------------------------

def test_create_proposal_request_rejects_colon_bearing_command_id():
    from trader.messaging.production_api import CreateProposalRequest
    with pytest.raises(ValidationError, match="colon|:"):
        CreateProposalRequest(command_id="bad:id", conid=265598, action="BUY", quantity=10)


def test_reject_proposal_request_rejects_colon_bearing_command_id():
    with pytest.raises(ValidationError, match="colon|:"):
        RejectProposalRequest(command_id="bad:id", proposal_id=7, reason="x")


def test_create_proposal_colon_command_id_fails_at_coercion_not_the_handler(production_registry):
    """Mirrors test_create_and_reject_proposal_over_the_registry's wiring,
    but drives the request through the SAME request-coercion helper
    (`_coerce_request_body`) that `TypedRpcServer._handle_request` calls
    before ever invoking the registered handler. A malformed wire
    `command_id` must fail HERE, as a `pydantic.ValidationError` (which the
    server maps to `RpcProblem(code="VALIDATION_ERROR", ...)`) -- not deep
    inside the handler via `CommandRequest.__post_init__`'s bare
    `ValueError`, which the server's broad, unrelated-exception catch-all
    would otherwise scrub to an opaque `INTERNAL_ERROR` ("internal error")."""
    from trader.messaging.typed_rpc import _coerce_request_body

    create_reg = production_registry.resolve("command", "create_proposal")
    with pytest.raises(ValidationError):
        _coerce_request_body(
            {"command_id": "bad:id", "conid": 265598, "action": "BUY", "quantity": 10},
            create_reg.request_model,
        )


def test_reject_proposal_colon_command_id_fails_at_coercion_not_the_handler(production_registry):
    from trader.messaging.typed_rpc import _coerce_request_body

    reject_reg = production_registry.resolve("command", "reject_proposal")
    with pytest.raises(ValidationError):
        _coerce_request_body(
            {"command_id": "bad:id", "proposal_id": 7, "reason": "x"},
            reject_reg.request_model,
        )


# ---------------------------------------------------------------------------
# Concurrent duplicate-command_id claim race: the handler must run AT MOST
# ONCE per command_id. The lock-free claim_or_replay read can go stale
# between the lookup and mutate()'s locked idempotency check; the loser must
# detect it (skipped write_materialized / EventIdentityConflict) and return
# the winner's recorded receipt WITHOUT dispatching the handler again.
# ---------------------------------------------------------------------------

from trader.trading.command_coordinator import LedgerClaim  # noqa: E402


class TestConcurrentClaimRace:
    def test_stale_new_claim_with_identical_mutation_never_redispatches(self, coordinator, ledger):
        # Winner completes normally first.
        calls: list[str] = []
        coordinator.register_action(
            "count_calls", lambda cmd: calls.append(cmd.command_id) or {"ok": True},
            requires_preflight=False)
        request = _request(command_id="race-1", action="count_calls")
        first = coordinator.execute(request)
        assert first.state == "RESOLVED" and calls == ["race-1"]

        # Simulate the loser's exact interleaving: its lock-free claim read
        # happened BEFORE the winner committed (returned "new"), but by the
        # time its mutate() runs the winner's RECEIVED event exists. The
        # frozen fixture clock makes the loser's mutation byte-identical, so
        # mutate() silently replays and skips _write_received.
        real = ledger.claim_or_replay_in_tx
        lied = {"done": False}

        def stale_read(conn, req):
            if not lied["done"] and req.command_id == "race-1":
                lied["done"] = True
                return LedgerClaim("new", None)
            return real(conn, req)

        coordinator._ledger = SimpleNamespace(
            claim_or_replay_in_tx=stale_read,
            insert_received_in_tx=ledger.insert_received_in_tx,
            transition_in_tx=ledger.transition_in_tx,
            get=ledger.get,
        )
        second = coordinator.execute(request)
        assert calls == ["race-1"], "loser must NOT re-dispatch the handler"
        assert second.state == "RESOLVED"  # the winner's recorded receipt

    def test_stale_new_claim_with_differing_timestamp_never_redispatches(self, journal, ledger):
        # Same race, but the loser runs on a coordinator with a DIFFERENT
        # frozen clock, so its RECEIVED mutation has a different
        # source_timestamp -> mutate() raises EventIdentityConflict instead
        # of silently replaying. The loser must map that to the winner's
        # recorded receipt, not AUDIT_UNAVAILABLE, and never dispatch.
        calls: list[str] = []
        winner = TradingCommandCoordinator(
            journal=journal, ledger=ledger, audit=FakeCommandAudit(),
            nonces=FakeNonceGate(), now=lambda: NOW)
        winner.register_action(
            "count_calls", lambda cmd: calls.append(cmd.command_id) or {"ok": True},
            requires_preflight=False)
        request = _request(command_id="race-2", action="count_calls")
        assert winner.execute(request).state == "RESOLVED" and calls == ["race-2"]

        later = NOW + dt.timedelta(seconds=1)
        loser = TradingCommandCoordinator(
            journal=journal, ledger=ledger, audit=FakeCommandAudit(),
            nonces=FakeNonceGate(), now=lambda: later)
        loser.register_action(
            "count_calls", lambda cmd: calls.append(cmd.command_id) or {"ok": True},
            requires_preflight=False)
        real = ledger.claim_or_replay_in_tx
        lied = {"done": False}

        def stale_read(conn, req):
            if not lied["done"] and req.command_id == "race-2":
                lied["done"] = True
                return LedgerClaim("new", None)
            return real(conn, req)

        loser._ledger = SimpleNamespace(
            claim_or_replay_in_tx=stale_read,
            insert_received_in_tx=ledger.insert_received_in_tx,
            transition_in_tx=ledger.transition_in_tx,
            get=ledger.get,
        )
        receipt = loser.execute(request)
        assert calls == ["race-2"], "loser must NOT re-dispatch the handler"
        assert receipt.state == "RESOLVED"
        assert receipt.error_code != "AUDIT_UNAVAILABLE"

    def test_two_threads_same_command_id_dispatch_exactly_once(self, coordinator, ledger, journal):
        import threading

        # Force the true interleaving with real threads: both threads
        # complete the lock-free claim read (both see "new") before either
        # enters mutate(). The barrier gates only the first two claim calls;
        # the loser's post-race re-claim passes straight through.
        barrier = threading.Barrier(2)
        gate_lock = threading.Lock()
        gated_calls = {"n": 0}
        real = ledger.claim_or_replay_in_tx

        def gated_read(conn, req):
            claim = real(conn, req)
            with gate_lock:
                gated_calls["n"] += 1
                should_gate = gated_calls["n"] <= 2
            if should_gate:
                barrier.wait(timeout=5)
            return claim

        coordinator._ledger = SimpleNamespace(
            claim_or_replay_in_tx=gated_read,
            insert_received_in_tx=ledger.insert_received_in_tx,
            transition_in_tx=ledger.transition_in_tx,
            get=ledger.get,
        )
        calls: list[str] = []
        calls_lock = threading.Lock()

        def handler(cmd):
            with calls_lock:
                calls.append(cmd.command_id)
            return {"ok": True}

        coordinator.register_action("count_calls", handler, requires_preflight=False)
        request = _request(command_id="race-3", action="count_calls")
        results: list = [None, None]

        def run(i):
            try:
                results[i] = coordinator.execute(request)
            except Exception as exc:  # surface, don't swallow
                results[i] = exc

        threads = [threading.Thread(target=run, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        assert all(not t.is_alive() for t in threads)
        assert not any(isinstance(r, Exception) for r in results), results
        assert calls == ["race-3"], f"handler must run exactly once, ran {len(calls)}x"
        # Both callers hold a coherent receipt for the same command; the
        # loser may have observed the winner mid-flight (RECEIVED) or
        # finished (RESOLVED) -- never a second dispatch.
        assert {r.command_id for r in results} == {"race-3"}
        assert all(r.state in ("RECEIVED", "RESOLVED") for r in results)
        row = ledger.get("race-3")
        assert row is not None and row.state == "RESOLVED"


# ---------------------------------------------------------------------------
# [M1-F3] Task 9 -- OUTCOME_UNKNOWN reconciliation schedule + resolution.
#
# The reconciler is driven directly (reconcile_once / run_due / rescan) against
# a REAL journal/repo/ledger with FAKE broker/strategy/alert ports -- the same
# fake-port shape the approval/cancel sagas use.
# ---------------------------------------------------------------------------


class FakeReconcilerOrders:
    """Test double for ``OrderDispatchPort`` on the reconciliation read path.

    Records nothing on submit/cancel (the reconciler must NEVER dispatch);
    only the read seams (``find_by_order_ref`` / ``enumeration_complete``)
    return meaningful data.
    """

    def __init__(self):
        self._by_ref: dict[str, list] = {}
        self.enumeration_ok = False
        self.submissions: list = []
        self.find_calls: list[str] = []

    def add_broker_order(self, *, order_ref, status, order_ids):
        self._by_ref.setdefault(order_ref, []).append(
            SimpleNamespace(order_ref=order_ref, status=status, order_ids=list(order_ids))
        )

    def find_by_order_ref(self, account_id, order_ref):
        self.find_calls.append(order_ref)
        return list(self._by_ref.get(order_ref, []))

    def enumeration_complete(self):
        return self.enumeration_ok

    def submit(self, *a, **k):  # pragma: no cover - reconciler never dispatches
        raise AssertionError("reconciler must never re-submit an order")

    def cancel(self, *a, **k):  # pragma: no cover - reconciler never dispatches
        raise AssertionError("reconciler must never re-cancel an order")


class FakeStrategyPort:
    """Test double for ``StrategyControlPort``: read-only receipt lookup; a
    ``forward`` call would be a bug (the reconciler only reconciles, never
    re-forwards a mutation)."""

    def __init__(self):
        self.receipts: dict[str, StrategyCommandReceipt] = {}
        self.forward_calls: list = []

    def forward(self, request):  # pragma: no cover - reconciler never forwards
        self.forward_calls.append(request)
        raise AssertionError("reconciler must never re-forward a strategy mutation")

    def get_receipt(self, command_id):
        return self.receipts.get(command_id)


class FakeAlerts:
    def __init__(self):
        self.raised: list[str] = []

    def raise_alert(self, command_id, detail):
        self.raised.append(command_id)


class FakeOrdersView:
    """Test double for ``OrderStateView`` -- the conn-free ``get_order`` seam
    onto [M1-F2]'s materialized broker-order store the cancel reconciliation
    reads for the TARGET order's authoritative status (MEDIUM-2)."""

    def __init__(self):
        self._orders: dict[str, BrokerOrderRow] = {}

    def add(self, order: BrokerOrderRow) -> None:
        self._orders[order.order_entity_id] = order

    def get_order(self, order_entity_id: str):
        return self._orders.get(order_entity_id)


def _broker_order(order_entity_id: str, *, status: str, deleted: bool = False) -> BrokerOrderRow:
    return BrokerOrderRow(
        order_entity_id=order_entity_id, account_id="DU111111", conid=265598, symbol="AAPL",
        order_group_id=None, leg="entry", is_external=False, action="SELL", order_type="MKT",
        total_quantity=10.0, filled_quantity=0.0, avg_fill_price=None, limit_price=None,
        stop_price=None, tif="DAY", status=status, deleted=deleted, revision=1,
        source_timestamp=NOW,
    )


def _recon_proposal_service(recon):
    """A REAL ``ProposalCommandService`` bound to the reconciliation fixture's
    journal/repo (no pause gate) -- used to drive genuine create/reject
    mutations that then wedge (HIGH-1)."""
    quotes = SimpleNamespace(executable_quote=lambda conid, side: ExecutableQuote(
        conid=conid, side=side, price=210.0 if side == "ask" else 209.5,
        market_timestamp=NOW, feed_type="live", session_state="continuous"))
    universe = SimpleNamespace(resolve_conid=lambda conid: SimpleNamespace(
        conId=conid, symbol="AAPL", primaryExchange="NASDAQ", secType="STK",
    ) if conid == 265598 else None)
    risk_gate = SimpleNamespace(
        check_instrument=lambda **_kw: SimpleNamespace(approved=True, reason=""),
        evaluate=lambda **_kw: SimpleNamespace(approved=True, reason=""))
    return ProposalCommandService(
        repository=recon.repo, journal=recon.journal, risk_gate=risk_gate, quotes=quotes,
        universe=universe, account_id="DU111111", account_mode="paper", now=lambda: NOW,
        controls=None, positions=SimpleNamespace(reducible_quantity=lambda a, c: 0.0),
    )


def _recon_coordinator(recon):
    return TradingCommandCoordinator(
        journal=recon.journal, ledger=recon.ledger, audit=CommandAudit(recon.journal),
        nonces=FakeNonceGate(), now=lambda: NOW,
    )


def _recon_view_reconciler(recon, view):
    return OutcomeReconciler(
        journal=recon.journal, ledger=recon.ledger, orders=recon.orders,
        strategy=recon.strategy, alerts=recon.alerts, repo=recon.repo,
        orders_view=view, now=recon.now,
    )


@pytest.fixture
def recon(tmp_path):
    db_ = DuckDBConnection.get_instance(str(tmp_path / "recon.duckdb"))
    migrator = SchemaMigrator(db_)
    journal_ = DomainJournal(db_)
    journal_.migrate(migrator)
    apply_proposal_authority_migration(migrator)
    apply_command_ledger_migration(migrator)
    repo = ProposalRepository(journal_)
    ledger_ = CommandLedger(journal_)
    orders = FakeReconcilerOrders()
    strategy = FakeStrategyPort()
    alerts = FakeAlerts()
    now_fn = lambda: NOW  # noqa: E731
    reconciler = OutcomeReconciler(
        journal=journal_, ledger=ledger_, orders=orders, strategy=strategy,
        alerts=alerts, repo=repo, now=now_fn,
    )

    def _seed_approved(pid, order_group_id):
        draft = ProposalDraft(
            id=pid, symbol="AAPL", action="BUY", quantity=10.0, amount=2100.0,
            execution={"order_type": "MARKET"}, reasoning="", confidence=0.7, thesis="",
            source="dashboard", metadata={}, sec_type="STK", account_id="DU111111",
            account_mode="paper", conid=265598, reference_price=210.0, reference_timestamp=NOW,
            reference_quote_side="ask", reference_feed_type="live", max_price_drift_bps=50.0,
            expires_at=NOW + dt.timedelta(minutes=5), live_approval_eligible=True, created_at=NOW,
        )
        predicted = ProposalCommandService._record_from_draft(draft, revision=1)

        def _write(conn, revision):
            repo.insert_pending_in_tx(conn, draft, revision)
            conn.execute(
                "UPDATE trade_proposals SET status='APPROVED', order_group_id=? WHERE id=?",
                [order_group_id, pid],
            )

        journal_.mutate(
            journal_.connect(), repo.mutation_for(predicted, "seed"), _write,
            event_id=f"proposal:{pid}:1",
        )

    def mark_unknown(command_id, *, target_type, target_id=None, order_group_id=None,
                     proposal_id=None, action=None):
        if proposal_id is not None:
            _seed_approved(proposal_id, order_group_id or f"og-{command_id}")
        if target_type == "proposal":
            resolved_target = target_id if target_id is not None else str(proposal_id)
            outcome = None
            default_action = "approve_proposal"
        elif target_type == "strategy":
            resolved_target = target_id or ""
            outcome = None
            default_action = "enable_strategy"
        else:  # "order" (cancel) -- carries no proposal in production; the plan's
                # order test wires one explicitly via the ledger outcome.
            resolved_target = target_id or order_group_id or ""
            outcome = {"proposal_id": proposal_id} if proposal_id is not None else None
            default_action = "cancel_order"
        ledger_.insert_for_test(
            command_id, state="OUTCOME_UNKNOWN", updated_at=NOW, account_id="DU111111",
            action=action or default_action, target_type=target_type,
            target_id=resolved_target, outcome=outcome,
        )
        reconciler.schedule(command_id, NOW)

    return SimpleNamespace(
        db=db_, journal=journal_, repo=repo, ledger=ledger_, orders=orders,
        strategy=strategy, alerts=alerts, reconciler=reconciler,
        now=lambda: NOW, cursor=0, mark_unknown=mark_unknown, seed_approved=_seed_approved,
    )


def test_schedule_is_immediate_then_5s_then_30s_for_15_minutes():
    assert RECONCILE_DELAYS[0] == 0.0
    assert RECONCILE_DELAYS[1:13] == (5.0,) * 12        # every 5 s for the first minute
    assert RECONCILE_DELAYS[13:] == (30.0,) * 28        # every 30 s for the next 14 minutes
    assert sum(RECONCILE_DELAYS) == 900.0               # critical alert boundary
    assert CRITICAL_AFTER_SECONDS == 900.0


def test_proposal_approve_resolves_by_encoded_order_ref(recon):
    # RJ2: the REAL approve saga stamps target_type="proposal"; an ambiguous
    # approve must resolve via the og-{command_id} order lookup.
    recon.mark_unknown("cmd-1", target_type="proposal", order_group_id="og-cmd-1", proposal_id=7)
    recon.orders.add_broker_order(
        order_ref=encode_order_ref("og-cmd-1"), status="Submitted", order_ids=[17])
    result = recon.reconciler.reconcile_once("cmd-1", recon.now())
    assert result.resolved is True
    row = recon.ledger.get("cmd-1")
    assert row.state == "RESOLVED" and row.outcome["order_ids"] == [17]
    assert recon.repo.get(7).status == "EXECUTED"       # submission evidence recorded
    kinds = [e.event_type for e in recon.journal.read_after(recon.cursor, 100)]
    assert "command.updated" in kinds and "proposal.updated" in kinds


# ---------------------------------------------------------------------------
# MEDIUM-2: a ``cancel_order`` creates NO og-* group, so the og lookup is
# always empty and must NEVER be used to resolve a cancel. Resolution reads
# the TARGET order's authoritative status via the injected ``OrderStateView``;
# with no view wired (dormant) or a still-active order it stays unknown.
# ---------------------------------------------------------------------------

def test_cancel_wedge_without_order_view_stays_unknown(recon):
    # RED before: the old target_type-only branch ran a cancel through the
    # og-{command_id} order path and rubber-stamped RESOLVED on a complete
    # enumeration. A cancel dispatches no og group, so that is fail-UNSAFE.
    recon.mark_unknown("cmd-1", target_type="order", target_id="ord-1")
    recon.orders.enumeration_ok = True                  # must NOT rubber-stamp a cancel
    result = recon.reconciler.reconcile_once("cmd-1", recon.now())
    assert result.resolved is False
    assert recon.ledger.get("cmd-1").state == "OUTCOME_UNKNOWN"
    assert encode_order_ref("og-cmd-1") not in recon.orders.find_calls  # order path untouched


def test_cancel_wedge_resolves_only_when_target_order_is_terminal(recon):
    view = FakeOrdersView()
    reconciler = _recon_view_reconciler(recon, view)
    recon.mark_unknown("cmd-1", target_type="order", target_id="ord-1")
    # Target order still Submitted -> the cancel didn't take -> stay unknown.
    view.add(_broker_order("ord-1", status="Submitted"))
    assert reconciler.reconcile_once("cmd-1", recon.now()).resolved is False
    assert recon.ledger.get("cmd-1").state == "OUTCOME_UNKNOWN"
    # Target order Cancelled -> RESOLVED, recording the authoritative status.
    view.add(_broker_order("ord-1", status="Cancelled"))
    result = reconciler.reconcile_once("cmd-1", recon.now())
    assert result.resolved is True
    row = recon.ledger.get("cmd-1")
    assert row.state == "RESOLVED"
    assert row.outcome["authoritative_status"] == "Cancelled"
    assert recon.orders.submissions == []               # never re-dispatched


def test_cancel_wedge_with_missing_target_order_stays_unknown(recon):
    view = FakeOrdersView()                              # order not present in the view
    reconciler = _recon_view_reconciler(recon, view)
    recon.mark_unknown("cmd-1", target_type="order", target_id="ord-gone")
    assert reconciler.reconcile_once("cmd-1", recon.now()).resolved is False
    assert recon.ledger.get("cmd-1").state == "OUTCOME_UNKNOWN"


def test_definitive_absence_requires_a_complete_enumeration(recon):
    recon.mark_unknown("cmd-1", target_type="proposal", order_group_id="og-cmd-1", proposal_id=7)
    recon.orders.enumeration_ok = False                 # no fenced generation yet
    assert recon.reconciler.reconcile_once("cmd-1", recon.now()).resolved is False
    assert recon.ledger.get("cmd-1").state == "OUTCOME_UNKNOWN"
    recon.orders.enumeration_ok = True                  # fenced view proves absence
    result = recon.reconciler.reconcile_once("cmd-1", recon.now())
    assert result.resolved is True
    assert recon.ledger.get("cmd-1").outcome == {"submitted": False}
    assert recon.repo.get(7).status == "FAILED"         # proven never-submitted: clean failure
    assert recon.orders.submissions == []               # never blindly resubmitted


def test_strategy_receipts_reconcile_by_root_command_id(recon):
    recon.mark_unknown("cmd-9", target_type="strategy", target_id="smi_crossover")
    recon.strategy.receipts["cmd-9"] = StrategyCommandReceipt(
        "cmd-9", "smi_crossover", "enable_strategy", "COMMITTED",
        control_revision=5, state_revision=12, error=None)
    assert recon.reconciler.reconcile_once("cmd-9", recon.now()).resolved is True
    assert recon.ledger.get("cmd-9").outcome["control_revision"] == 5
    # A missing receipt stays unknown and the mutation is never repeated.
    recon.mark_unknown("cmd-10", target_type="strategy", target_id="smi_crossover")
    assert recon.reconciler.reconcile_once("cmd-10", recon.now()).resolved is False
    assert recon.strategy.forward_calls == []


def test_unresolved_after_15_minutes_is_critical_never_failed(recon):
    recon.mark_unknown("cmd-1", target_type="proposal", order_group_id="og-cmd-1", proposal_id=7)
    late = recon.now() + dt.timedelta(seconds=901)
    results = recon.reconciler.run_due(late)
    assert results[0].critical is True
    assert recon.alerts.raised == ["cmd-1"]
    assert recon.ledger.get("cmd-1").state == "OUTCOME_UNKNOWN"   # never timeout-to-failure


def test_run_due_only_fires_attempts_that_are_actually_due(recon):
    recon.mark_unknown("cmd-1", target_type="proposal", order_group_id="og-cmd-1", proposal_id=7)
    # First attempt fires immediately (delay 0); it stays unknown (no order,
    # incomplete enumeration) and is rescheduled 5 s out.
    first = recon.reconciler.run_due(recon.now())
    assert [r.command_id for r in first] == ["cmd-1"]
    assert first[0].resolved is False
    # 1 s later nothing is due yet.
    assert recon.reconciler.run_due(recon.now() + dt.timedelta(seconds=1)) == []
    # By +5 s the second attempt is due again.
    again = recon.reconciler.run_due(recon.now() + dt.timedelta(seconds=5))
    assert [r.command_id for r in again] == ["cmd-1"]


def test_resolved_command_is_dropped_from_the_schedule(recon):
    recon.mark_unknown("cmd-1", target_type="proposal", order_group_id="og-cmd-1", proposal_id=7)
    recon.orders.add_broker_order(
        order_ref=encode_order_ref("og-cmd-1"), status="Submitted", order_ids=[21])
    assert recon.reconciler.reconcile_once("cmd-1", recon.now()).resolved is True
    # Once resolved, run_due no longer revisits it.
    assert recon.reconciler.run_due(recon.now() + dt.timedelta(seconds=901)) == []
    assert recon.alerts.raised == []


def test_startup_rescan_requeues_inflight_commands(recon):
    recon.mark_unknown("cmd-1", target_type="proposal", order_group_id="og-cmd-1", proposal_id=7)
    recon.ledger.insert_for_test("cmd-2", state="SUBMITTING",
                                 updated_at=recon.now() - dt.timedelta(minutes=2))
    requeued = recon.reconciler.rescan_on_startup()
    assert set(requeued) == {"cmd-1", "cmd-2"}          # crash between claim and ack is covered


def test_rescan_ignores_terminal_rows(recon):
    recon.ledger.insert_for_test("done", state="RESOLVED", updated_at=recon.now())
    recon.ledger.insert_for_test("rej", state="REJECTED", updated_at=recon.now())
    recon.mark_unknown("cmd-1", target_type="proposal", order_group_id="og-cmd-1", proposal_id=7)
    assert set(recon.reconciler.rescan_on_startup()) == {"cmd-1"}


# ---------------------------------------------------------------------------
# HIGH-1: approve/create/reject all stamp target_type="proposal" but resolve by
# entirely different authority. Only ``approve_proposal`` may use the
# og-{command_id} order lookup. A wedged create/reject must resolve by the
# ACTUAL proposal-mutation outcome -- never via the order path (which falsely
# marks a create never-submitted and can mark a concurrently-APPROVED proposal
# FAILED).
# ---------------------------------------------------------------------------

def test_create_wedge_resolves_by_proposal_creation_not_order_path(recon):
    svc = _recon_proposal_service(recon)
    coord = _recon_coordinator(recon)

    def create_then_wedge(cmd):
        # REAL create: commits the proposal + journals proposal.updated
        # correlated to the command, THEN blows up (ambiguous-but-committed).
        svc.create_proposal(
            ProposalCreateRequest(conid=cmd.body["conid"], action=cmd.body["action"],
                                  quantity=cmd.body.get("quantity")),
            source=cmd.source, correlation_id=cmd.command_id)
        raise RuntimeError("boom after create committed")

    coord.register_action("create_proposal", create_then_wedge, requires_preflight=False)
    create_cmd = CommandRequest(
        command_id="cc-1", action="create_proposal", account_id="DU111111",
        target_type="proposal", target_id="", expected_version=None,
        body={"conid": 265598, "action": "BUY", "quantity": 10}, source="dashboard")
    with pytest.raises(RuntimeError):
        coord.execute(create_cmd)
    assert recon.ledger.get("cc-1").state == "OUTCOME_UNKNOWN"

    recon.orders.enumeration_ok = True                  # a complete enum must NOT falsely resolve
    result = recon.reconciler.reconcile_once("cc-1", recon.now())
    assert result.resolved is True
    row = recon.ledger.get("cc-1")
    assert row.outcome.get("created") is True
    assert row.outcome != {"submitted": False}          # NOT the never-submitted order path
    assert recon.orders.find_calls == []                # order path never consulted for a create


def test_create_wedge_without_committed_proposal_stays_unknown(recon):
    coord = _recon_coordinator(recon)

    def wedge_before_create(cmd):
        raise RuntimeError("boom before any proposal write")

    coord.register_action("create_proposal", wedge_before_create, requires_preflight=False)
    create_cmd = CommandRequest(
        command_id="cc-2", action="create_proposal", account_id="DU111111",
        target_type="proposal", target_id="", expected_version=None,
        body={"conid": 265598, "action": "BUY", "quantity": 10}, source="dashboard")
    with pytest.raises(RuntimeError):
        coord.execute(create_cmd)

    recon.orders.enumeration_ok = True
    result = recon.reconciler.reconcile_once("cc-2", recon.now())
    assert result.resolved is False                     # not positively confirmed -> stay unknown
    assert recon.ledger.get("cc-2").state == "OUTCOME_UNKNOWN"
    assert recon.orders.find_calls == []


def test_reject_wedge_resolves_when_proposal_is_rejected(recon):
    svc = _recon_proposal_service(recon)
    coord = _recon_coordinator(recon)
    created = svc.create_proposal(
        ProposalCreateRequest(conid=265598, action="BUY", quantity=10),
        source="dashboard", correlation_id="seed-create")
    pid = created.id

    def reject_then_wedge(cmd):
        svc.reject_proposal(int(cmd.body["proposal_id"]), cmd.body["reason"], cmd.command_id)
        raise RuntimeError("boom after reject committed")

    coord.register_action("reject_proposal", reject_then_wedge, requires_preflight=False)
    reject_cmd = CommandRequest(
        command_id="rj-2", action="reject_proposal", account_id="DU111111",
        target_type="proposal", target_id=str(pid), expected_version=None,
        body={"proposal_id": pid, "reason": "changed thesis"}, source="dashboard")
    with pytest.raises(RuntimeError):
        coord.execute(reject_cmd)
    assert recon.ledger.get("rj-2").state == "OUTCOME_UNKNOWN"
    assert recon.repo.get(pid).status == "REJECTED"

    recon.orders.enumeration_ok = True
    result = recon.reconciler.reconcile_once("rj-2", recon.now())
    assert result.resolved is True
    assert recon.ledger.get("rj-2").outcome["status"] == "REJECTED"
    assert recon.orders.find_calls == []                # order path never consulted for a reject


def test_reject_wedge_never_marks_a_separately_approved_proposal_failed(recon):
    # Proposal 7 is APPROVED (a live order path owns it). A reject command that
    # wedged to OUTCOME_UNKNOWN must NEVER be run through the order path (which
    # would mark the APPROVED proposal FAILED via _resolve_never_submitted).
    recon.mark_unknown("rej-1", target_type="proposal", target_id="7",
                       proposal_id=7, action="reject_proposal")
    recon.orders.enumeration_ok = True
    result = recon.reconciler.reconcile_once("rej-1", recon.now())
    assert result.resolved is False                     # proposal is APPROVED, not REJECTED
    assert recon.ledger.get("rej-1").state == "OUTCOME_UNKNOWN"
    assert recon.repo.get(7).status == "APPROVED"       # NEVER blind-marked FAILED


# ---------------------------------------------------------------------------
# MEDIUM-3: set_trading_pause and the cancel_orders root must reach a defined
# resolution rather than falling through to an eternal critical alert.
# ---------------------------------------------------------------------------

def _seed_controls(recon):
    apply_trading_control_migration(SchemaMigrator(recon.db))
    controls = TradingControlStore(recon.journal)
    recon.db.transaction(lambda conn: controls.seed_in_tx(conn, [("DU111111", "paper")], NOW))
    return controls


def test_pause_wedge_resolves_when_control_row_reflects_the_command(recon):
    controls = _seed_controls(recon)
    controls.set("DU111111", True, None, "pz-1", "risk event", NOW)  # committed under pz-1
    recon.ledger.insert_for_test(
        "pz-1", state="OUTCOME_UNKNOWN", updated_at=NOW, account_id="DU111111",
        action="set_trading_pause", target_type="trading_control", target_id="DU111111")
    result = recon.reconciler.reconcile_once("pz-1", recon.now())
    assert result.resolved is True
    row = recon.ledger.get("pz-1")
    assert row.state == "RESOLVED"
    assert row.outcome["new_exposure_paused"] is True


def test_pause_wedge_stays_unknown_when_control_row_is_from_another_command(recon):
    controls = _seed_controls(recon)
    controls.set("DU111111", True, None, "other-cmd", "x", NOW)      # a DIFFERENT command set it
    recon.ledger.insert_for_test(
        "pz-2", state="OUTCOME_UNKNOWN", updated_at=NOW, account_id="DU111111",
        action="set_trading_pause", target_type="trading_control", target_id="DU111111")
    result = recon.reconciler.reconcile_once("pz-2", recon.now())
    assert result.resolved is False                     # not confirmed committed by THIS command
    assert recon.ledger.get("pz-2").state == "OUTCOME_UNKNOWN"


def test_cancel_orders_root_wedge_reaches_a_defined_terminal(recon):
    # The root fan-out dispatches nothing itself; children are independently
    # reconciled. A wedged root must resolve, not alert forever.
    recon.ledger.insert_for_test(
        "co-1", state="OUTCOME_UNKNOWN", updated_at=NOW, account_id="DU111111",
        action="cancel_orders", target_type="order_group", target_id="")
    recon.reconciler.schedule("co-1", NOW)
    result = recon.reconciler.reconcile_once("co-1", recon.now())
    assert result.resolved is True
    assert recon.ledger.get("co-1").state == "RESOLVED"
    # And it never escalates to a perpetual critical alert.
    assert recon.reconciler.run_due(recon.now() + dt.timedelta(seconds=901)) == []
    assert recon.alerts.raised == []


# ---------------------------------------------------------------------------
# MEDIUM-4: a hard crash between the approve saga's VALIDATED commit and the
# claim tx orphans a VALIDATED row that reconcilable() never requeues -- it
# would block the proposal forever via unresolved_for_target/COMMAND_IN_FLIGHT.
# A VALIDATED-but-unclaimed command dispatched no order, so startup recovery can
# safely terminalize it (fail-safe) and unblock the proposal.
# ---------------------------------------------------------------------------

def test_rescan_terminalizes_orphaned_validated_rows(recon):
    recon.ledger.insert_for_test(
        "val-1", state="VALIDATED", updated_at=recon.now() - dt.timedelta(minutes=2),
        account_id="DU111111", action="approve_proposal", target_type="proposal", target_id="7")
    assert recon.ledger.unresolved_for_target("proposal", "7")      # blocks the proposal
    recon.reconciler.rescan_on_startup()
    row = recon.ledger.get("val-1")
    assert row.state == "REJECTED"                                  # terminalized
    assert row.error_code == "CRASH_ORPHANED"
    assert recon.ledger.unresolved_for_target("proposal", "7") == []  # proposal unblocked


def test_rescan_leaves_received_rows_untouched(recon):
    # RECEIVED (pre-VALIDATED) for a non-saga action may carry a committed side
    # effect (a created proposal / committed pause), so it is NOT blindly
    # terminalized -- only the provably pre-dispatch VALIDATED state is.
    recon.ledger.insert_for_test(
        "rcv-1", state="RECEIVED", updated_at=recon.now(), account_id="DU111111",
        action="set_trading_pause", target_type="trading_control", target_id="DU111111")
    recon.reconciler.rescan_on_startup()
    assert recon.ledger.get("rcv-1").state == "RECEIVED"


# ---------------------------------------------------------------------------
# RJ2 (addendum #2): the coordinator schedules reconciliation for EVERY
# OUTCOME_UNKNOWN wedge -- including the generic saga-exception fallback path
# that previously scheduled nothing and relied on a restart to be revisited.
# The hook is optional/default-None so existing coordinator tests are
# unaffected (proven by the whole existing suite above, which builds the
# coordinator without a reconciler).
# ---------------------------------------------------------------------------


class _RecordingReconciler:
    def __init__(self):
        self.scheduled: list[str] = []

    def schedule(self, command_id, now):
        self.scheduled.append(command_id)


def test_coordinator_schedules_reconcile_when_a_non_saga_handler_wedges(journal, ledger):
    hook = _RecordingReconciler()
    coord = TradingCommandCoordinator(
        journal=journal, ledger=ledger, audit=FakeCommandAudit(),
        nonces=FakeNonceGate(), now=lambda: NOW, reconciler=hook,
    )

    def handler(cmd):
        raise RuntimeError("boom: handler's own mutate() blew up")

    coord.register_action("noop", handler, requires_preflight=False)
    with pytest.raises(RuntimeError, match="boom"):
        coord.execute(_request(action="noop"))
    assert ledger.get("cmd-1").state == "OUTCOME_UNKNOWN"
    assert hook.scheduled == ["cmd-1"]                  # the wedge was scheduled, not left orphaned


def test_coordinator_fallback_schedules_reconcile_for_a_saga_wedge(journal, ledger):
    hook = _RecordingReconciler()
    coord = TradingCommandCoordinator(
        journal=journal, ledger=ledger, audit=FakeCommandAudit(),
        nonces=FakeNonceGate(), now=lambda: NOW, reconciler=hook,
    )

    def saga_handler(cmd):
        # A saga that advances past RECEIVED then blows up before reaching its
        # own DISPATCH_AMBIGUOUS branch -- the generic fallback must schedule.
        coord._transition(cmd, "RECEIVED", "VALIDATED")
        raise RuntimeError("saga blew up pre-dispatch")

    coord.register_action("saga_noop", saga_handler, requires_preflight=False, saga=True)
    with pytest.raises(RuntimeError, match="saga blew up"):
        coord.execute(_request(action="saga_noop"))
    assert ledger.get("cmd-1").state == "OUTCOME_UNKNOWN"
    assert hook.scheduled == ["cmd-1"]


def test_coordinator_without_reconciler_still_wedges_cleanly(journal, ledger):
    # Default None hook: no scheduling, no crash -- byte-identical to the
    # pre-Task-9 behaviour exercised by the rest of this suite.
    coord = TradingCommandCoordinator(
        journal=journal, ledger=ledger, audit=FakeCommandAudit(),
        nonces=FakeNonceGate(), now=lambda: NOW,
    )
    coord.register_action(
        "noop", lambda cmd: (_ for _ in ()).throw(RuntimeError("boom")), requires_preflight=False)
    with pytest.raises(RuntimeError):
        coord.execute(_request(action="noop"))
    assert ledger.get("cmd-1").state == "OUTCOME_UNKNOWN"


# ---------------------------------------------------------------------------
# An OUTCOME_UNKNOWN command blocks any OTHER command for the same target
# (the COMMAND_IN_FLIGHT guard, §9.5), driven through the REAL coordinator +
# approval saga.
# ---------------------------------------------------------------------------


class _GateOrders:
    def __init__(self):
        self._raise = None
        self._next = 5001
        self.submissions: list = []

    def raise_on_submit(self, exc):
        self._raise = exc

    def submit(self, proposal, order_ref, order_group_id):
        if self._raise is not None:
            raise self._raise
        s = SubmittedOrders(order_group_id=order_group_id, order_ref=order_ref,
                            order_ids=[self._next])
        self._next += 1
        self.submissions.append(s)
        return s

    def cancel(self, *a, **k):  # pragma: no cover
        raise NotImplementedError

    def find_by_order_ref(self, account_id, order_ref):
        return []

    def enumeration_complete(self):
        return True


@pytest.fixture
def gate(tmp_path):
    db_ = DuckDBConnection.get_instance(str(tmp_path / "gate.duckdb"))
    migrator = SchemaMigrator(db_)
    journal_ = DomainJournal(db_)
    journal_.migrate(migrator)
    apply_proposal_authority_migration(migrator)
    apply_command_ledger_migration(migrator)
    apply_trading_control_migration(migrator)
    repo = ProposalRepository(journal_)
    ledger_ = CommandLedger(journal_)
    controls = TradingControlStore(journal_)
    db_.transaction(lambda conn: controls.seed_in_tx(conn, [("DU111111", "paper")], NOW))
    now_fn = lambda: NOW  # noqa: E731

    orders = _GateOrders()
    quotes = SimpleNamespace(executable_quote=lambda conid, side: ExecutableQuote(
        conid=conid, side=side, price=210.0 if side == "ask" else 209.5,
        market_timestamp=NOW, feed_type="live", session_state="continuous"))
    positions = SimpleNamespace(reducible_quantity=lambda account_id, conid: 0.0)
    broker = SimpleNamespace(is_ready=lambda: True)
    risk_gate = SimpleNamespace(evaluate=lambda **_kw: SimpleNamespace(approved=True, reason=""))
    risk_producer = SimpleNamespace(publish_decision=lambda *a, **k: None)
    fake_reconciler = _RecordingReconciler()

    coordinator = TradingCommandCoordinator(
        journal=journal_, ledger=ledger_, audit=CommandAudit(journal_),
        nonces=FakeNonceGate(), now=now_fn, reconciler=fake_reconciler,
    )
    approval = ApprovalCommandService(
        journal=journal_, ledger=ledger_, repo=repo, controls=controls, orders=orders,
        positions=positions, quotes=quotes, risk_gate=risk_gate, risk_producer=risk_producer,
        reconciler=fake_reconciler, broker=broker, account_id="DU111111",
        account_mode="paper", now=now_fn,
    )
    coordinator.register_action(
        "approve_proposal", approval.approve, requires_preflight=True, saga=True)

    def _reject_action(cmd):
        pid = int(cmd.body["proposal_id"])
        inflight = [
            r for r in ledger_.unresolved_for_target("proposal", str(pid))
            if r.command_id != cmd.command_id
        ]
        if inflight:
            raise CommandValidationError(
                "COMMAND_IN_FLIGHT", "another command is in flight for this proposal")
        return {"rejected": True}

    coordinator.register_action("reject_proposal", _reject_action, requires_preflight=False)

    def pending(*, conid=265598, action="BUY"):
        pid = repo.reserve_id()
        draft = ProposalDraft(
            id=pid, symbol="AAPL", action=action, quantity=10.0, amount=2100.0,
            execution={"order_type": "MARKET"}, reasoning="", confidence=0.7, thesis="",
            source="dashboard", metadata={}, sec_type="STK", account_id="DU111111",
            account_mode="paper", conid=conid, reference_price=210.0, reference_timestamp=NOW,
            reference_quote_side="ask" if action == "BUY" else "bid",
            reference_feed_type="live", max_price_drift_bps=50.0,
            expires_at=NOW + dt.timedelta(minutes=5), live_approval_eligible=True, created_at=NOW,
        )
        predicted = ProposalCommandService._record_from_draft(draft, revision=1)
        written: list = []
        journal_.mutate(
            journal_.connect(), repo.mutation_for(predicted, "seed"),
            lambda conn, revision: written.append(repo.insert_pending_in_tx(conn, draft, revision)),
            event_id=f"proposal:{pid}:1",
        )
        return written[0]

    def execute_approve(record, command_id):
        request = CommandRequest(
            command_id=command_id, action="approve_proposal", account_id="DU111111",
            target_type="proposal", target_id=str(record.id), expected_version=record.revision,
            body={"proposal_id": record.id}, source="dashboard",
            preflight_nonce=f"nonce-{command_id}")
        return coordinator.execute(request)

    def execute_reject(record, command_id):
        request = CommandRequest(
            command_id=command_id, action="reject_proposal", account_id="DU111111",
            target_type="proposal", target_id=str(record.id), expected_version=None,
            body={"proposal_id": record.id, "reason": "x"}, source="dashboard")
        return coordinator.execute(request)

    return SimpleNamespace(
        journal=journal_, repo=repo, ledger=ledger_, controls=controls,
        coordinator=coordinator, approval=approval, orders=orders,
        pending=pending, execute_approve=execute_approve, execute_reject=execute_reject,
    )


def test_unknown_command_blocks_other_commands_for_the_same_proposal(gate):
    record = gate.pending(conid=265598, action="BUY")
    gate.orders.raise_on_submit(TimeoutError("ack lost"))
    unknown = gate.execute_approve(record, command_id="cmd-1")
    assert unknown.state == "OUTCOME_UNKNOWN"
    blocked = gate.execute_reject(record, command_id="cmd-2")
    assert blocked.error_code == "COMMAND_IN_FLIGHT"              # §9.5
