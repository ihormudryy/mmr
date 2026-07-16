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
    CommandLedger,
    CommandRequest,
    CommandValidationError,
    IllegalCommandTransition,
    TradingCommandCoordinator,
    apply_command_ledger_migration,
    canonical_request_hash,
)
from trader.trading.proposal_command_service import ExecutableQuote, ProposalCommandService

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
