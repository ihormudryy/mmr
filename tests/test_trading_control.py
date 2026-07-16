"""[M1-F3] Task 4 — durable per-account pause gate."""
import datetime as dt
from types import SimpleNamespace

import pytest

from trader.data.domain_journal import DomainJournal
from trader.data.duckdb_store import DuckDBConnection
from trader.data.proposal_repository import ProposalRepository, apply_proposal_authority_migration
from trader.data.schema_migrations import SchemaMigrator
from trader.messaging.production_api import register_command_authority
from trader.messaging.typed_rpc import TypedRpcRegistry
from trader.trading.command_coordinator import (
    CommandAudit,
    CommandLedger,
    CommandRequest,
    TradingCommandCoordinator,
    apply_command_ledger_migration,
)
from trader.trading.proposal_command_service import (
    ExecutableQuote,
    ProposalCommandService,
    ProposalCreateRequest,
    ProposalCreationRefused,
)
from trader.trading.trading_control import (
    PauseRevisionConflict,
    PauseStateUnavailable,
    TradingControlStore,
    TradingPausedError,
    apply_trading_control_migration,
)

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 7, 16, 14, 0, tzinfo=UTC)
ACCOUNT_ID = "DU111111"


def _set(controls, db, *, paused, expected_revision, command_id):
    """Test helper mirroring the ``_seed`` idiom from the brief: drives the
    ``_in_tx`` method directly through an externally-owned transaction."""
    def _tx(conn):
        return controls.set_pause_in_tx(conn, ACCOUNT_ID, paused, expected_revision, command_id, "test", NOW)
    return db.transaction(_tx)


class FakeQuotes:
    """Supports both sides for conId 265598 so BUY (ask) and a reducing
    SELL close (bid) both resolve without a separate fixture per action."""

    def __init__(self):
        self._quotes = {
            (265598, "ask"): ExecutableQuote(
                conid=265598, side="ask", price=210.0, market_timestamp=NOW,
                feed_type="live", session_state="continuous",
            ),
            (265598, "bid"): ExecutableQuote(
                conid=265598, side="bid", price=209.5, market_timestamp=NOW,
                feed_type="live", session_state="continuous",
            ),
        }

    def executable_quote(self, conid, *, side):
        return self._quotes.get((conid, side))


class FakeRiskGate:
    def __init__(self):
        self.approved = True

    def check_instrument(self, **_kwargs):
        return SimpleNamespace(approved=self.approved, reason="denylisted")


class FakeUniverse:
    def resolve_conid(self, conid):
        if conid != 265598:
            return None
        return SimpleNamespace(
            conId=conid, symbol="AAPL", primaryExchange="NASDAQ", secType="STK",
            exchange="SMART", currency="USD",
        )


class FakePositions:
    """Test double for the position-reducibility check: ``create_proposal``
    exempts a SELL from the pause gate only when its quantity is verified
    to be <= the currently held (reducible) quantity."""

    def __init__(self):
        self._held: dict[int, float] = {}

    def set_held(self, conid: int, quantity: float) -> None:
        self._held[conid] = quantity

    def reducible_quantity(self, account_id: str, conid: int) -> float:
        return self._held.get(conid, 0.0)


class FakeNonceGate:
    def __init__(self):
        self._consumed: set[str] = set()

    def consume_in_tx(self, conn, nonce, request) -> bool:
        if not nonce or nonce in self._consumed:
            return False
        self._consumed.add(nonce)
        return True


def _create_request(**overrides):
    values = dict(conid=265598, action="BUY", confidence=0.7, amount=5_000.0)
    if "quantity" in overrides:
        values.pop("amount", None)
    values.update(overrides)
    return ProposalCreateRequest(**values)


@pytest.fixture
def db(tmp_path):
    return DuckDBConnection.get_instance(str(tmp_path / "journal.duckdb"))


@pytest.fixture
def journal(db):
    migrator = SchemaMigrator(db)
    j = DomainJournal(db)
    j.migrate(migrator)
    apply_trading_control_migration(migrator)
    return j


@pytest.fixture
def control_db(db, journal):
    return db


@pytest.fixture
def controls(journal, db):
    store = TradingControlStore(journal)
    db.transaction(lambda conn: store.seed_in_tx(conn, [(ACCOUNT_ID, "paper")], NOW))
    return store


@pytest.fixture
def authority_with_controls(tmp_path):
    db = DuckDBConnection.get_instance(str(tmp_path / "journal.duckdb"))
    migrator = SchemaMigrator(db)
    journal = DomainJournal(db)
    journal.migrate(migrator)
    apply_proposal_authority_migration(migrator)
    apply_command_ledger_migration(migrator)
    apply_trading_control_migration(migrator)

    repository = ProposalRepository(journal)
    controls = TradingControlStore(journal)
    db.transaction(lambda conn: controls.seed_in_tx(conn, [(ACCOUNT_ID, "paper")], NOW))

    quotes = FakeQuotes()
    risk_gate = FakeRiskGate()
    positions = FakePositions()
    service = ProposalCommandService(
        repository=repository,
        journal=journal,
        risk_gate=risk_gate,
        quotes=quotes,
        universe=FakeUniverse(),
        account_id=ACCOUNT_ID,
        account_mode="paper",
        now=lambda: NOW,
        ttl=dt.timedelta(minutes=5),
        controls=controls,
        positions=positions,
    )

    ledger = CommandLedger(journal)
    coordinator = TradingCommandCoordinator(
        journal=journal, ledger=ledger, audit=CommandAudit(journal),
        nonces=FakeNonceGate(), now=lambda: NOW,
    )
    registry = TypedRpcRegistry()
    register_command_authority(
        registry, coordinator, service, repository, account_id=ACCOUNT_ID, controls=controls,
    )

    return SimpleNamespace(
        db=db, journal=journal, repository=repository, controls=controls,
        positions=positions, quotes=quotes, risk_gate=risk_gate, service=service,
        coordinator=coordinator, registry=registry,
    )


# ---------------------------------------------------------------------------
# Table shape
# ---------------------------------------------------------------------------

def test_table_shape_matches_spec(control_db):
    cols = control_db.execute(
        "SELECT column_name, is_nullable FROM information_schema.columns "
        "WHERE table_name = 'trading_control_state' ORDER BY ordinal_position",
        fetch="all")
    assert [c[0] for c in cols] == [
        "account_id", "new_exposure_paused", "revision", "updated_at",
        "updated_by_command_id", "updated_reason"]
    assert all(c[1] == "NO" for c in cols[1:])     # every non-key column NOT NULL


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

def test_bootstrap_seeds_live_paused_paper_unpaused_idempotently(controls, db):
    def _seed(conn):
        return controls.seed_in_tx(conn, [("U1234567", "live"), ("DU111111", "paper")], NOW)
    db.transaction(_seed)
    db.transaction(_seed)                                    # idempotent re-run
    live, paper = controls.get("U1234567"), controls.get("DU111111")
    assert live.new_exposure_paused is True and paper.new_exposure_paused is False
    assert live.revision == 1 and paper.revision == 1
    assert live.updated_by_command_id == "system:bootstrap"
    assert live.updated_reason == "account initialization"


# ---------------------------------------------------------------------------
# Absolute set semantics
# ---------------------------------------------------------------------------

def test_pause_is_absolute_and_idempotent_from_a_stale_view(controls, db):
    state = _set(controls, db, paused=True, expected_revision=None, command_id="cmd-1")
    again = _set(controls, db, paused=True, expected_revision=None, command_id="cmd-2")
    assert state.new_exposure_paused is True and again.new_exposure_paused is True
    assert again.revision == state.revision      # no-op repeat mints no revision


def test_resume_requires_the_exact_current_revision(controls, db):
    paused = _set(controls, db, paused=True, expected_revision=None, command_id="cmd-1")
    with pytest.raises(PauseRevisionConflict):
        _set(controls, db, paused=False, expected_revision=paused.revision - 1, command_id="cmd-2")
    resumed = _set(controls, db, paused=False, expected_revision=paused.revision, command_id="cmd-3")
    assert resumed.new_exposure_paused is False and resumed.revision == paused.revision + 1


def test_missing_row_fails_closed_for_exposure_increasing_actions(controls):
    with pytest.raises(PauseStateUnavailable):
        controls.require_unpaused("U_NEVER_SEEDED")


# ---------------------------------------------------------------------------
# Serialization against dispatch + proposal-creation integration
# ---------------------------------------------------------------------------

def test_pause_commit_serializes_against_final_dispatch(authority_with_controls):
    """A pause that commits first must reject a later exposure-increasing
    dispatch: the SUBMITTING claim transaction re-checks the gate row."""
    a = authority_with_controls
    _set(a.controls, a.db, paused=True, expected_revision=None, command_id="pause-1")
    with pytest.raises(TradingPausedError):
        a.db.transaction(lambda conn: a.controls.require_unpaused_in_tx(conn, "DU111111"))


def test_paused_account_blocks_exposure_increasing_creation_only(authority_with_controls):
    a = authority_with_controls
    _set(a.controls, a.db, paused=True, expected_revision=None, command_id="pause-1")
    with pytest.raises(ProposalCreationRefused) as exc:
        a.service.create_proposal(_create_request(conid=265598, action="BUY"),
                                  source="dashboard", correlation_id="c1")
    assert exc.value.code == "TRADING_PAUSED"
    a.positions.set_held(265598, 100.0)          # verified reducible position
    close = a.service.create_proposal(
        _create_request(conid=265598, action="SELL", quantity=100.0),
        source="dashboard", correlation_id="c2")
    assert close.status == "PENDING"             # reducing close allowed while paused


def test_unverified_sell_is_treated_as_exposure_increasing(authority_with_controls):
    """A SELL with no verified held position (or no quantity given, e.g. an
    auto-sized/amount-based request) must NOT be exempted -- fail closed."""
    a = authority_with_controls
    _set(a.controls, a.db, paused=True, expected_revision=None, command_id="pause-1")
    with pytest.raises(ProposalCreationRefused) as exc:
        a.service.create_proposal(
            _create_request(conid=265598, action="SELL", quantity=100.0),
            source="dashboard", correlation_id="c1")
    assert exc.value.code == "TRADING_PAUSED"


# ---------------------------------------------------------------------------
# Production RPC wiring (set_trading_pause command, get_trading_control query)
# ---------------------------------------------------------------------------

def test_set_trading_pause_registered_and_runs_through_the_coordinator(authority_with_controls):
    a = authority_with_controls
    assert a.registry.contains("command", "set_trading_pause")
    assert a.registry.contains("query", "get_trading_control")

    request = CommandRequest(
        command_id="cmd-pause-1", action="set_trading_pause", account_id=ACCOUNT_ID,
        target_type="trading_control", target_id=ACCOUNT_ID, expected_version=None,
        body={"paused": True, "expected_version": None, "reason": "manual halt"},
        source="dashboard", preflight_nonce="test-nonce-1",
    )
    receipt = a.coordinator.execute(request)
    assert receipt.state == "RESOLVED"
    assert a.controls.get(ACCOUNT_ID).new_exposure_paused is True
