"""[M1-F3] Task 2 — trader-owned proposal creation and expiry."""
import datetime as dt
from types import SimpleNamespace

import pytest

from trader.data.domain_journal import DomainJournal
from trader.data.duckdb_store import DuckDBConnection
from trader.data.proposal_repository import (
    ApprovalClaim,
    ProposalRepository,
    apply_proposal_authority_migration,
)
from trader.data.schema_migrations import SchemaMigrator
from trader.trading.proposal_command_service import (
    ExecutableQuote,
    ProposalCommandService,
    ProposalCreateRequest,
    ProposalCreationRefused,
)

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 7, 16, 14, 0, tzinfo=UTC)


class FakeQuotes:
    def __init__(self):
        self.quote = ExecutableQuote(
            conid=265598, side="ask", price=210.0, market_timestamp=NOW,
            feed_type="live", session_state="continuous",
        )

    def executable_quote(self, conid, *, side):
        return self.quote if self.quote and self.quote.conid == conid and self.quote.side == side else None


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
    """Stand-in PositionAuthority. ``held`` is the broker-reported reducible
    (long) quantity for the account/conid; 0 means flat."""

    def __init__(self, held=0.0):
        self.held = held

    def reducible_quantity(self, account_id, conid):
        return self.held


def _bid_quote():
    return ExecutableQuote(
        conid=265598, side="bid", price=209.0, market_timestamp=NOW,
        feed_type="live", session_state="continuous",
    )


@pytest.fixture
def authority(tmp_path):
    db = DuckDBConnection.get_instance(str(tmp_path / "journal.duckdb"))
    migrator = SchemaMigrator(db)
    journal = DomainJournal(db)
    journal.migrate(migrator)
    apply_proposal_authority_migration(migrator)
    repository = ProposalRepository(journal)
    quotes = FakeQuotes()
    risk_gate = FakeRiskGate()
    positions = FakePositions(held=0.0)
    service = ProposalCommandService(
        repository=repository,
        journal=journal,
        risk_gate=risk_gate,
        quotes=quotes,
        universe=FakeUniverse(),
        account_id="DU111111",
        account_mode="paper",
        now=lambda: NOW,
        ttl=dt.timedelta(minutes=5),
        positions=positions,
    )
    return SimpleNamespace(
        db=db, journal=journal, repository=repository, quotes=quotes,
        risk_gate=risk_gate, positions=positions, service=service,
    )


def _request(**overrides):
    values = dict(conid=265598, action="BUY", confidence=0.7, amount=5_000.0)
    values.update(overrides)
    return ProposalCreateRequest(**values)


def test_reserve_id_skips_ids_already_materialized_when_sequence_lags(authority):
    """WAL quarantine can leave rows while DuckDB sequence last_value resets."""
    conn = authority.journal.connect()
    # Occupy id 1 without consuming nextval — the desync shape after a
    # discarded WAL where checkpointed rows outlive sequence state.
    conn.execute(
        """
        INSERT INTO trade_proposals (
            id, symbol, action, quantity, amount, execution, reasoning,
            confidence, thesis, source, metadata, status, created_at,
            updated_at, order_ids, rejection_reason, sec_type, account_id,
            account_mode, conid, reference_price, reference_timestamp,
            reference_quote_side, reference_feed_type, max_price_drift_bps,
            expires_at, live_approval_eligible, revision, order_group_id
        ) VALUES (
            1, 'AAPL', 'BUY', 1.0, NULL, '{}', 'orphan', 0.5, '', 'manual', '{}',
            'REJECTED', ?, ?, '[]', 'seed', 'STK', 'DU111111', 'paper', 265598,
            210.0, ?, 'ask', 'live', 50.0, ?, false, 1, NULL
        )
        """,
        [NOW.replace(tzinfo=None), NOW.replace(tzinfo=None), NOW, NOW],
    )
    conn.execute(
        """
        INSERT INTO domain_event_journal (
            event_id, entity_revision, event_type, entity_type, entity_id,
            operation, account_id, source, source_timestamp, received_timestamp,
            correlation_id, payload
        ) VALUES (
            'proposal:1:1', 1, 'proposal.updated', 'proposal', '1',
            'upsert', 'DU111111', 'trader_service', ?, ?, 'seed', '{}'
        )
        """,
        [NOW, NOW],
    )

    created = authority.service.create_proposal(
        _request(group="tech"), source="dashboard", correlation_id="cmd-after-lag"
    )
    assert created.id == 2
    assert created.status == "PENDING"


def test_create_is_guard_complete_and_journaled(authority):
    record = authority.service.create_proposal(
        _request(group="tech"), source="dashboard", correlation_id="cmd-1"
    )

    assert record.status == "PENDING" and record.revision == 1
    assert record.account_id == "DU111111" and record.account_mode == "paper"
    assert record.conid == 265598
    assert record.reference_price == 210.0
    assert record.reference_quote_side == "ask"
    assert record.reference_feed_type == "live"
    assert record.max_price_drift_bps == 50.0
    assert record.expires_at == NOW + dt.timedelta(minutes=5)
    assert record.live_approval_eligible is True
    events = authority.journal.read_after(0, 10)
    assert [(event.event_type, event.correlation_id) for event in events] == [
        ("proposal.updated", "cmd-1")
    ]
    assert events[0].payload["id"] == record.id


def test_missing_quote_refuses_without_row_or_event(authority):
    authority.quotes.quote = None

    with pytest.raises(ProposalCreationRefused, match="QUOTE_UNAVAILABLE"):
        authority.service.create_proposal(_request(), source="dashboard", correlation_id="cmd-2")

    assert authority.repository.list(status="PENDING", limit=10) == []
    assert authority.journal.read_after(0, 10) == []


def test_trading_filter_rejection_refuses_creation(authority):
    authority.risk_gate.approved = False

    with pytest.raises(ProposalCreationRefused, match="TRADING_FILTER_REJECTED"):
        authority.service.create_proposal(_request(), source="dashboard", correlation_id="cmd-3")


def test_strategy_duplicate_pending_is_refused(authority):
    authority.service.create_proposal(_request(), source="strategy:orb", correlation_id="c1")

    with pytest.raises(ProposalCreationRefused, match="DUPLICATE_PENDING"):
        authority.service.create_proposal(_request(), source="strategy:orb", correlation_id="c2")


def test_strategy_sell_while_flat_is_refused(authority):
    """Long-only bridge semantics: a strategy SELL with no held long is
    'ignored when flat' — refused before it can become a short proposal."""
    authority.positions.held = 0.0

    with pytest.raises(ProposalCreationRefused, match="NO_LONG_TO_CLOSE"):
        authority.service.create_proposal(
            _request(action="SELL", amount=5_000.0),
            source="strategy:orb", correlation_id="sell-flat",
        )

    assert authority.repository.list(status="PENDING", limit=10) == []
    assert authority.journal.read_after(0, 10) == []


def test_strategy_sell_with_held_long_proceeds(authority):
    """A strategy SELL that actually reduces a held long is a legitimate
    close and must be proposed."""
    authority.positions.held = 100.0
    authority.quotes.quote = _bid_quote()

    record = authority.service.create_proposal(
        _request(action="SELL", amount=5_000.0),
        source="strategy:orb", correlation_id="sell-held",
    )

    assert record.status == "PENDING" and record.action == "SELL"
    assert record.reference_quote_side == "bid"


def test_manual_sell_while_flat_is_allowed(authority):
    """The long-only 'ignore when flat' rule is a strategy-bridge semantic;
    a human/LLM SELL (source != strategy:) may intentionally open a short."""
    authority.positions.held = 0.0
    authority.quotes.quote = _bid_quote()

    record = authority.service.create_proposal(
        _request(action="SELL", amount=5_000.0),
        source="dashboard", correlation_id="sell-manual",
    )

    assert record.status == "PENDING" and record.action == "SELL"


def test_reject_is_idempotent_and_journals_once(authority):
    record = authority.service.create_proposal(_request(), source="dashboard", correlation_id="c1")

    first = authority.service.reject_proposal(record.id, "changed thesis", "c2")
    again = authority.service.reject_proposal(record.id, "changed thesis", "c3")

    assert first.status == "REJECTED" and again.status == "REJECTED"
    assert first.revision == again.revision == 2
    assert [event.payload["status"] for event in authority.journal.read_after(0, 10)] == [
        "PENDING", "REJECTED"
    ]


def test_expiry_sweep_updates_every_stale_proposal_and_journals(authority):
    record = authority.service.create_proposal(_request(), source="strategy:orb", correlation_id="c1")

    expired = authority.service.expire_stale(record.expires_at + dt.timedelta(seconds=1))

    assert expired == [record.id]
    stored = authority.repository.get(record.id)
    assert stored.status == "EXPIRED" and stored.revision == 2
    assert [event.payload["status"] for event in authority.journal.read_after(0, 10)] == [
        "PENDING", "EXPIRED"
    ]


def test_auto_sized_proposal_uses_the_injected_sizer(authority):
    authority.service._sizer = SimpleNamespace(
        compute=lambda **_kwargs: SimpleNamespace(amount_usd=2_100.0, quantity=10)
    )

    record = authority.service.create_proposal(
        _request(amount=None), source="dashboard", correlation_id="cmd-auto"
    )

    assert record.quantity == 10
    assert record.amount == 2_100.0


def test_atomic_claim_rechecks_expiry_and_journals_the_expired_transition(authority):
    record = authority.service.create_proposal(_request(), source="dashboard", correlation_id="c1")
    now = record.expires_at + dt.timedelta(seconds=1)
    predicted = record.__class__(
        **{**record.__dict__, "status": "EXPIRED", "updated_at": now, "revision": 2}
    )
    outcome = []

    def write_materialized(conn, revision):
        assert revision == 2
        outcome.append(authority.repository.claim_for_approval_in_tx(
            conn, record.id, record.revision, record.account_id, now
        ))

    authority.journal.mutate(
        authority.journal.connect(),
        authority.repository.mutation_for(predicted, correlation_id="c2"),
        write_materialized,
        event_id=f"proposal:{record.id}:2",
    )

    assert outcome[0].result == ApprovalClaim.EXPIRED
    assert outcome[0].record.status == "EXPIRED"
