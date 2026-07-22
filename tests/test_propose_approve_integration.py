"""End-to-end integration test for the propose → approve/reject pipeline.

[M1-F3] Task 8: the SDK (`trader.sdk.MMR`) no longer talks to a local
`ProposalStore` for propose/approve/reject/proposals -- it routes through
the command-authority coordinator's typed `create_proposal`/
`approve_proposal`/`reject_proposal`/`get_proposal` methods (Tasks 2-5).
This test drives that REAL production stack in-process (real
`TradingCommandCoordinator`, `ProposalCommandService`, `ApprovalCommandService`,
`ProposalRepository`, `TradingControlStore`, all journal-backed) through an
`_InProcessTypedClient` double that resolves `(role, method)` against the
actual `TypedRpcRegistry` built by `register_command_authority` and invokes
the registered handler directly -- no ZMQ socket, no HMAC signing, but the
SAME request/response models and the SAME coordinator/service code the real
typed transport would dispatch to. Everything that would talk to IB (order
dispatch, quotes, risk gate, broker health, reconciliation) is a fake, same
as `tests/test_approval_command.py`.
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from typing import List
from unittest.mock import MagicMock

import pytest

from trader.common.reactivex import SuccessFailEnum
from trader.data.domain_journal import DomainJournal
from trader.data.duckdb_store import DuckDBConnection
from trader.data.event_store import EventStore, EventType, TradingEvent
from trader.data.proposal_repository import (
    ProposalRepository,
    apply_proposal_authority_migration,
)
from trader.data.schema_migrations import SchemaMigrator
from trader.messaging.production_api import register_command_authority
from trader.messaging.typed_rpc import TypedRpcRegistry, TypedRpcRemoteError, _DispatchProblem
from trader.sdk import MMR
from trader.trading.command_coordinator import (
    ApprovalCommandService,
    BrokerRejectedError,
    CommandAudit,
    CommandLedger,
    SubmittedOrders,
    TradingCommandCoordinator,
    apply_command_ledger_migration,
)
from trader.trading.proposal_command_service import ExecutableQuote, ProposalCommandService
from trader.trading.trading_control import TradingControlStore, apply_trading_control_migration

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 7, 16, 14, 0, tzinfo=UTC)
ACCOUNT_ID = 'DU111111'
ACCOUNT_MODE = 'paper'
AMD_CONID = 1001


# ---------------------------------------------------------------------------
# Fakes for every collaborator the sagas touch (none reach IB) -- mirrors
# tests/test_approval_command.py's fixture builder.
# ---------------------------------------------------------------------------

class _Clock:
    """Mutable `now` callable so a test can advance time past a proposal's
    TTL without a client-side `_utcnow` seam (that seam was deleted along
    with every other direct-ProposalStore code path -- expiry is now
    entirely server-side, evaluated against whatever `now` the SERVICE was
    built with)."""

    def __init__(self, start: dt.datetime):
        self.value = start

    def __call__(self) -> dt.datetime:
        return self.value

    def advance(self, **kwargs) -> None:
        self.value = self.value + dt.timedelta(**kwargs)


class FakeQuotes:
    def __init__(self, now):
        self._now = now
        self._quotes: dict[tuple[int, str], ExecutableQuote] = {}

    def set(self, conid, *, ask=None, bid=None, feed_type='live',
            age_seconds=0.0, session_state='continuous'):
        ts = self._now() - dt.timedelta(seconds=age_seconds)
        if ask is not None:
            self._quotes[(conid, 'ask')] = ExecutableQuote(
                conid=conid, side='ask', price=ask, market_timestamp=ts,
                feed_type=feed_type, session_state=session_state)
        if bid is not None:
            self._quotes[(conid, 'bid')] = ExecutableQuote(
                conid=conid, side='bid', price=bid, market_timestamp=ts,
                feed_type=feed_type, session_state=session_state)

    def executable_quote(self, conid, *, side):
        return self._quotes.get((conid, side))


class FakePositions:
    def __init__(self):
        self._held: dict[int, float] = {}

    def set_held(self, conid, quantity):
        self._held[conid] = quantity

    def reducible_quantity(self, account_id, conid):
        return self._held.get(conid, 0.0)


class FakeUniverse:
    """`ProposalCommandService`'s `UniverseAuthority` -- a LOCAL, exact
    conId->security-definition lookup (never IB discovery), matching how
    `resolve_symbol` behaves everywhere else in this codebase."""

    def __init__(self):
        self._defs: dict[int, SimpleNamespace] = {}

    def add(self, conid, symbol, primary_exchange='NASDAQ', sec_type='STK'):
        self._defs[conid] = SimpleNamespace(
            symbol=symbol, primaryExchange=primary_exchange, secType=sec_type)

    def resolve_conid(self, conid):
        return self._defs.get(conid)


class FakeRiskGate:
    """Serves BOTH `ProposalCommandService.check_instrument` and
    `ApprovalCommandService.evaluate` -- the two services call different
    methods on what's conceptually "the risk gate"."""

    def __init__(self):
        self.approved = True
        self.reason = ''

    def check_instrument(self, **kwargs):
        return SimpleNamespace(approved=self.approved, reason=self.reason)

    def evaluate(self, signal=None, **kwargs):
        return SimpleNamespace(approved=self.approved, reason=self.reason)


class FakeBroker:
    def __init__(self, positions):
        self.ready = True
        self.positions = positions

    def capture(self, account_id):
        if not self.ready:
            raise RuntimeError("broker unavailable")
        return SimpleNamespace(
            account_id=ACCOUNT_ID, account_mode=ACCOUNT_MODE,
            generation_id=1, source_cursor=1, open_order_count=0,
            daily_pnl=0.0, net_liquidation=100_000.0, working_orders=(),
            reducible_quantity=lambda conid: self.positions._held.get(conid, 0.0),
            position_value=lambda conid: abs(self.positions._held.get(conid, 0.0)) * 100.0,
        )


class FakeReconciler:
    def __init__(self):
        self.scheduled: list[str] = []

    def schedule(self, command_id, now):
        self.scheduled.append(command_id)


class FakeRiskProducer:
    def __init__(self):
        self.decisions: list[tuple[str, dict]] = []

    def publish_decision(self, command_id, payload, correlation_id=None):
        self.decisions.append((command_id, payload))


class FakeOrders:
    def __init__(self):
        self.submissions: list[SubmittedOrders] = []
        self._raise = None
        self._next_id = 777

    def raise_on_submit(self, exc):
        self._raise = exc

    def submit(self, proposal, order_ref, order_group_id):
        if self._raise is not None:
            raise self._raise
        submitted = SubmittedOrders(
            order_group_id=order_group_id, order_ref=order_ref, order_ids=[self._next_id])
        self._next_id += 1
        self.submissions.append(submitted)
        return submitted

    def cancel(self, order_entity_id, order_ref):  # pragma: no cover - not exercised here
        raise NotImplementedError

    def find_by_order_ref(self, account_id, order_ref):  # pragma: no cover
        return []

    def enumeration_complete(self):  # pragma: no cover
        return True


class _AlwaysAllowNonceGate:
    """Permissive preflight-nonce gate for this integration test: [M1-F3]
    Task 8's SDK doesn't implement a preflight-nonce ceremony yet (that's
    documented as an [M1-C] concern -- see `register_command_authority`'s
    docstring: "the paper `PreflightNonceGate` accepts the documented
    `paper:<command_id>` self-nonce"), so `approve_proposal`'s body never
    carries one. A real deployment needs a real `PreflightNonceGate`; this
    fake always allows, so the test can focus on the propose/approve/reject
    SDK-adapter behaviour this task actually owns (the saga's own nonce
    ceremony is already exhaustively covered by `test_approval_command.py`)."""

    def consume_in_tx(self, conn, nonce, request):
        return True


class _InProcessTypedClient:
    """Drives the REAL production registry in-process (no ZMQ/HMAC): resolves
    `(role, method)`, validates the body against the registered
    `request_model`, invokes the handler, and returns the raw result.
    `response_model` is honoured the same way `TypedRpcClient.call` treats
    it (`dict` -> raw dict; otherwise construct `response_model(**result)`)
    -- since the handlers here return plain dicts shaped exactly like
    `CommandReceipt`'s fields (`_receipt_to_dict`), reconstructing via
    keyword arguments works without needing a `.model_validate` classmethod
    (the documented `CommandReceipt`/`model_validate` wire gap is a property
    of the REAL `TypedRpcClient`, not of this in-process test double)."""

    def __init__(self, registry: TypedRpcRegistry, role: str):
        self._registry = registry
        self._role = role

    def call(self, method, body, response_model=None, timeout=None):
        registration = self._registry.resolve(self._role, method)
        if registration is None:
            raise TypedRpcRemoteError(
                'METHOD_NOT_ALLOWED', f'{method!r} not registered on {self._role!r}')
        request_model = registration.request_model
        parsed = body if request_model is dict else request_model.model_validate(body)
        try:
            result = registration.handler(parsed)
        except _DispatchProblem as exc:
            raise TypedRpcRemoteError(exc.code, str(exc)) from exc
        if response_model is dict or response_model is None:
            return result
        if isinstance(result, response_model):
            return result
        return response_model(**result)


# ---------------------------------------------------------------------------
# Stack + MMR builders
# ---------------------------------------------------------------------------

def _build_stack(tmp_path):
    db = DuckDBConnection.get_instance(str(tmp_path / 'journal.duckdb'))
    migrator = SchemaMigrator(db)
    journal = DomainJournal(db)
    journal.migrate(migrator)
    apply_proposal_authority_migration(migrator)
    apply_command_ledger_migration(migrator)
    apply_trading_control_migration(migrator)

    repo = ProposalRepository(journal)
    controls = TradingControlStore(journal)
    db.transaction(lambda conn: controls.seed_in_tx(conn, [(ACCOUNT_ID, ACCOUNT_MODE)], NOW))

    ledger = CommandLedger(journal)
    clock = _Clock(NOW)

    quotes = FakeQuotes(clock)
    quotes.set(AMD_CONID, ask=100.0, bid=99.5)
    universe = FakeUniverse()
    universe.add(AMD_CONID, 'AMD')
    positions = FakePositions()
    risk_gate = FakeRiskGate()
    broker = FakeBroker(positions)
    reconciler = FakeReconciler()
    orders = FakeOrders()
    risk_producer = FakeRiskProducer()

    coordinator = TradingCommandCoordinator(
        journal=journal, ledger=ledger, audit=CommandAudit(journal),
        nonces=_AlwaysAllowNonceGate(), now=clock,
    )
    proposal_service = ProposalCommandService(
        repository=repo, journal=journal, risk_gate=risk_gate, quotes=quotes,
        universe=universe, account_id=ACCOUNT_ID, account_mode=ACCOUNT_MODE, now=clock,
        controls=controls, positions=positions,
    )
    approval_service = ApprovalCommandService(
        journal=journal, ledger=ledger, repo=repo, controls=controls, orders=orders,
        quotes=quotes, risk_gate=risk_gate,
        risk_producer=risk_producer, reconciler=reconciler, broker=broker,
        account_id=ACCOUNT_ID, account_mode=ACCOUNT_MODE, now=clock,
    )

    registry = TypedRpcRegistry()
    register_command_authority(
        registry, coordinator, proposal_service, repo,
        account_id=ACCOUNT_ID, account_mode="paper", controls=controls,
        resume_ready=lambda: True, reconciliation_complete=lambda command_id: True,
        approval_service=approval_service,
    )

    return SimpleNamespace(
        db=db, journal=journal, repo=repo, ledger=ledger, controls=controls,
        coordinator=coordinator, registry=registry, orders=orders, quotes=quotes,
        universe=universe, positions=positions, risk_gate=risk_gate, broker=broker,
        reconciler=reconciler, risk_producer=risk_producer, clock=clock,
    )


class FakeSecurityDefinition:
    def __init__(self, symbol='AMD', conId=AMD_CONID, secType='STK',
                 exchange='SMART', primaryExchange='NASDAQ', currency='USD'):
        self.symbol = symbol
        self.conId = conId
        self.secType = secType
        self.exchange = exchange
        self.primaryExchange = primaryExchange
        self.currency = currency


class _StubRPCClient:
    """Minimal legacy RPC client kept for non-resolve RPCs. Symbol resolution
    now goes through typed ``discover_instrument`` (see
    ``_ResolveAwareTypedClient``); this stub's ``secdefs`` map feeds that."""

    def __init__(self, secdefs: dict):
        self.is_setup = True
        self.secdefs = secdefs
        self.calls: List[dict] = []

    def rpc(self, return_type=None):
        outer = self

        class _Chain:
            def __init__(self, names=()):
                self._names = names

            def __getattr__(self, name):
                return _Chain(self._names + (name,))

            def __call__(self, *args, **kwargs):
                method = self._names[-1] if self._names else ''
                outer.calls.append({'method': method, 'args': args, 'kwargs': kwargs})
                return None

        return _Chain()


class _ResolveAwareTypedClient:
    """Wraps the in-process typed client to serve ``discover_instrument`` /
    ``resolve_instrument`` from the same local secdef map the old legacy
    ``resolve_symbol`` stub used. Command-authority methods still hit the
    real registry."""

    def __init__(self, inner: _InProcessTypedClient, secdefs: dict):
        self._inner = inner
        self._by_symbol = secdefs
        self._by_conid = {
            int(d.conId): d for defs in secdefs.values() for d in defs
        }

    @staticmethod
    def _wire(d) -> dict:
        return {
            'instrument_id': int(d.conId),
            'symbol': str(d.symbol),
            'exchange': str(getattr(d, 'exchange', '') or ''),
            'primary_exchange': str(getattr(d, 'primaryExchange', '') or ''),
            'currency': str(getattr(d, 'currency', '') or ''),
            'security_type': str(getattr(d, 'secType', 'STK') or 'STK'),
            'time_zone_id': '',
        }

    def call(self, method, body, response_model=None, timeout=None):
        if method == 'discover_instrument':
            defs = self._by_symbol.get(body.get('symbol'), [])
            return {'instruments': [self._wire(d) for d in defs]}
        if method == 'resolve_instrument':
            d = self._by_conid.get(int(body['instrument_id']))
            return {'instruments': [self._wire(d)] if d is not None else []}
        return self._inner.call(method, body, response_model=response_model, timeout=timeout)


def _make_mmr(stack, rpc_client) -> MMR:
    """Build a just-enough MMR wired to the in-process typed registry above
    (command authority + symbol discovery)."""
    mmr = MMR.__new__(MMR)
    mmr._client = rpc_client
    mmr._data_client = None
    mmr._massive_rest_client = None
    mmr._rpc_address = 'tcp://127.0.0.1'
    mmr._rpc_port = 42001
    mmr._timeout = 5
    mmr._subscriptions = []
    mmr._position_map = {}
    mmr._contract_map = {}

    container = MagicMock()
    container.config_file = '/tmp/test_trader.yaml'
    container.config.return_value = {'duckdb_path': ''}
    mmr._container = container

    mmr._typed_command_client = _InProcessTypedClient(stack.registry, 'command')
    query = _InProcessTypedClient(stack.registry, 'query')
    mmr._typed_query_client = _ResolveAwareTypedClient(query, rpc_client.secdefs)
    return mmr


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def stack(tmp_path):
    return _build_stack(tmp_path)


@pytest.fixture
def rpc():
    return _StubRPCClient({'AMD': [FakeSecurityDefinition(symbol='AMD', conId=AMD_CONID)]})


@pytest.fixture
def mmr(stack, rpc):
    return _make_mmr(stack, rpc)


def _propose_amd(mmr, quantity=10) -> int:
    result = mmr.propose(symbol='AMD', action='BUY', quantity=quantity, reasoning='integration test')
    assert result.is_success(), f'propose() failed: {result.error}'
    return result.obj['proposal_id']


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestProposeApproveFlow:
    def test_happy_path_execute(self, mmr, stack):
        """Propose → Approve → EXECUTED with recorded order IDs, driven
        entirely through the SDK's typed adapters against the real
        coordinator/services."""
        pid = _propose_amd(mmr)

        result = mmr.approve(pid)

        assert result.success_fail == SuccessFailEnum.SUCCESS
        assert result.obj == stack.orders.submissions[0].order_ids
        stored = stack.repo.get(pid)
        assert stored.status == 'EXECUTED'
        assert stored.order_ids == stack.orders.submissions[0].order_ids
        assert len(stack.orders.submissions) == 1

    def test_double_approve_rejected_by_state_machine(self, mmr, stack):
        """A proposal that's already executed can't be approved again."""
        pid = _propose_amd(mmr)

        r1 = mmr.approve(pid)
        assert r1.success_fail == SuccessFailEnum.SUCCESS

        r2 = mmr.approve(pid)
        assert r2.success_fail == SuccessFailEnum.FAIL
        assert len(stack.orders.submissions) == 1   # no second dispatch

    def test_reject_transitions_to_rejected(self, mmr, stack):
        pid = _propose_amd(mmr)

        ok = mmr.reject(pid, reason='changed thesis')

        assert ok is True
        stored = stack.repo.get(pid)
        assert stored.status == 'REJECTED'
        assert stored.rejection_reason == 'changed thesis'

    def test_rejected_cannot_be_approved(self, mmr, stack):
        """REJECTED is a terminal state — approve() must fail."""
        pid = _propose_amd(mmr)
        assert mmr.reject(pid, reason='nope') is True

        result = mmr.approve(pid)

        assert result.success_fail == SuccessFailEnum.FAIL
        assert stack.orders.submissions == []

    def test_broker_rejection_marks_proposal_failed(self, mmr, stack):
        """If the broker rejects the order, the proposal transitions to
        FAILED, not stuck at APPROVED."""
        stack.orders.raise_on_submit(BrokerRejectedError('Order rejected by IB'))
        pid = _propose_amd(mmr)

        result = mmr.approve(pid)

        assert result.success_fail == SuccessFailEnum.FAIL
        stored = stack.repo.get(pid)
        assert stored.status == 'FAILED', (
            f'expected FAILED, got {stored.status} — the approve flow should '
            'surface broker failures through the state machine'
        )

    def test_dispatch_timeout_is_outcome_unknown_not_a_silent_failure(self, mmr, stack):
        """A dispatch timeout is genuinely ambiguous — approve() must
        surface it loudly (never silently as a clean failure) and never
        transition the proposal to a terminal state."""
        stack.orders.raise_on_submit(TimeoutError('ib ack timeout'))
        pid = _propose_amd(mmr)

        result = mmr.approve(pid)

        assert not result.is_success()
        assert 'outcome unknown' in result.error.lower()
        assert 'do not re-approve' in result.error.lower()
        stored = stack.repo.get(pid)
        assert stored.status == 'APPROVED'   # ambiguous, not FAILED

    def test_nonexistent_proposal_rejected(self, mmr):
        result = mmr.approve(99999)

        assert result.success_fail == SuccessFailEnum.FAIL
        assert 'not_found' in (result.error or '').lower()


class TestApprovalExpiryTruth:
    """Approval must route through the guarded saga's expiry-aware claim:
    an expired proposal is transitioned PENDING -> EXPIRED server-side and
    never reaches order dispatch, instead of being silently approved and
    executed against a stale signal."""

    def test_expired_proposal_never_calls_order_rpc(self, mmr, stack):
        pid = _propose_amd(mmr)
        stack.clock.advance(minutes=10)   # past ProposalCommandService's 5-minute default TTL

        result = mmr.approve(pid)

        assert not result.is_success()
        assert stack.repo.get(pid).status == 'EXPIRED'
        assert stack.orders.submissions == []


class TestEventStoreAuditTrail:
    """The atomic event_store writes should record SIGNAL → etc. entries and
    durably survive a read from a fresh connection. Independent of the
    propose/approve pipeline above (a different DuckDB file entirely)."""

    def test_append_and_query_back_after_reconnect(self, tmp_duckdb_path):
        store = EventStore(tmp_duckdb_path)
        store.append(TradingEvent(
            event_type=EventType.SIGNAL,
            timestamp=dt.datetime.now(),
            strategy_name='integration',
            conid=1, symbol='AMD', action='BUY',
            signal_probability=0.8, signal_risk=0.1,
        ))

        # Simulate a fresh process: clear singleton, re-open, read.
        DuckDBConnection._instances.pop(tmp_duckdb_path, None)
        store2 = EventStore(tmp_duckdb_path)
        events = store2.query_all()
        assert len(events) == 1
        assert events[0].symbol == 'AMD'
        assert events[0].event_type == EventType.SIGNAL
