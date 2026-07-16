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
    )
    return SimpleNamespace(
        db=db, journal=journal, repository=repository, quotes=quotes,
        risk_gate=risk_gate, service=service,
    )


def _request(**overrides):
    values = dict(conid=265598, action="BUY", confidence=0.7, amount=5_000.0)
    values.update(overrides)
    return ProposalCreateRequest(**values)


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
