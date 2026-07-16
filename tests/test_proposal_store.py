import datetime as dt
import pytest
from trader.data.proposal_store import ProposalStore
from trader.trading.proposal import ExecutionSpec, TradeProposal


def _make_proposal(
    symbol='AMD',
    action='BUY',
    quantity=100.0,
    execution=None,
    reasoning='Test reasoning',
    source='manual',
):
    return TradeProposal(
        symbol=symbol,
        action=action,
        quantity=quantity,
        execution=execution or ExecutionSpec(),
        reasoning=reasoning,
        source=source,
    )


class TestProposalStore:
    def test_table_creation(self, proposal_store):
        assert proposal_store is not None

    def test_add_returns_id(self, proposal_store):
        pid = proposal_store.add(_make_proposal())
        assert pid == 1

    def test_add_auto_increments(self, proposal_store):
        id1 = proposal_store.add(_make_proposal())
        id2 = proposal_store.add(_make_proposal(symbol='AAPL'))
        id3 = proposal_store.add(_make_proposal(symbol='MSFT'))
        assert id1 == 1
        assert id2 == 2
        assert id3 == 3

    def test_get_retrieves_all_fields(self, proposal_store):
        spec = ExecutionSpec(
            order_type='LIMIT',
            limit_price=165.0,
            exit_type='BRACKET',
            take_profit_price=180.0,
            stop_loss_price=150.0,
            tif='GTC',
            outside_rth=True,
        )
        proposal = TradeProposal(
            symbol='AMD',
            action='BUY',
            quantity=100.0,
            amount=16500.0,
            execution=spec,
            reasoning='Breakout play',
            confidence=0.85,
            thesis='AMD momentum',
            source='llm',
            metadata={'key': 'value'},
            sec_type='STK',
        )
        pid = proposal_store.add(proposal)
        result = proposal_store.get(pid)

        assert result is not None
        assert result.id == pid
        assert result.symbol == 'AMD'
        assert result.action == 'BUY'
        assert result.quantity == 100.0
        assert result.amount == 16500.0
        assert result.execution.order_type == 'LIMIT'
        assert result.execution.limit_price == 165.0
        assert result.execution.exit_type == 'BRACKET'
        assert result.execution.take_profit_price == 180.0
        assert result.execution.stop_loss_price == 150.0
        assert result.execution.tif == 'GTC'
        assert result.execution.outside_rth is True
        assert result.reasoning == 'Breakout play'
        assert result.confidence == 0.85
        assert result.thesis == 'AMD momentum'
        assert result.source == 'llm'
        assert result.metadata == {'key': 'value'}
        assert result.status == 'PENDING'
        assert result.sec_type == 'STK'
        assert result.created_at is not None
        assert result.updated_at is not None

    def test_get_nonexistent_returns_none(self, proposal_store):
        assert proposal_store.get(999) is None

    def test_query_all(self, proposal_store):
        proposal_store.add(_make_proposal(symbol='AMD'))
        proposal_store.add(_make_proposal(symbol='AAPL'))
        proposal_store.add(_make_proposal(symbol='MSFT'))
        results = proposal_store.query()
        assert len(results) == 3

    def test_query_by_status(self, proposal_store):
        pid1 = proposal_store.add(_make_proposal(symbol='AMD'))
        pid2 = proposal_store.add(_make_proposal(symbol='AAPL'))
        proposal_store.update_status(pid1, 'APPROVED')

        pending = proposal_store.query(status='PENDING')
        assert len(pending) == 1
        assert pending[0].symbol == 'AAPL'

        approved = proposal_store.query(status='APPROVED')
        assert len(approved) == 1
        assert approved[0].symbol == 'AMD'

    def test_query_limit(self, proposal_store):
        for i in range(10):
            proposal_store.add(_make_proposal(symbol=f'SYM{i}'))
        results = proposal_store.query(limit=3)
        assert len(results) == 3

    def test_query_ordering_desc(self, proposal_store):
        pid1 = proposal_store.add(_make_proposal(symbol='FIRST'))
        pid2 = proposal_store.add(_make_proposal(symbol='SECOND'))
        results = proposal_store.query()
        # Most recent first
        assert results[0].symbol == 'SECOND'
        assert results[1].symbol == 'FIRST'

    def test_update_status(self, proposal_store):
        pid = proposal_store.add(_make_proposal())
        proposal_store.update_status(pid, 'APPROVED')
        p = proposal_store.get(pid)
        assert p.status == 'APPROVED'

    def test_update_status_with_order_ids(self, proposal_store):
        pid = proposal_store.add(_make_proposal())
        # State machine requires PENDING → APPROVED → EXECUTED
        proposal_store.update_status(pid, 'APPROVED')
        proposal_store.update_status(pid, 'EXECUTED', order_ids=[101, 102, 103])
        p = proposal_store.get(pid)
        assert p.status == 'EXECUTED'
        assert p.order_ids == [101, 102, 103]

    def test_invalid_transition_raises(self, proposal_store):
        """Terminal states cannot transition further."""
        from trader.data.proposal_store import InvalidProposalTransition

        pid = proposal_store.add(_make_proposal())
        proposal_store.update_status(pid, 'REJECTED', rejection_reason='no')
        # REJECTED is terminal
        with pytest.raises(InvalidProposalTransition):
            proposal_store.update_status(pid, 'APPROVED')
        with pytest.raises(InvalidProposalTransition):
            proposal_store.update_status(pid, 'EXECUTED')

    def test_cannot_double_approve(self, proposal_store):
        """Re-approving an APPROVED proposal must RAISE, not silently pass.

        A silent same-status no-op let a second concurrent approver believe it
        won the transition and place a duplicate live order. Same-status
        transitions are now rejected.
        """
        from trader.data.proposal_store import InvalidProposalTransition

        pid = proposal_store.add(_make_proposal())
        proposal_store.update_status(pid, 'APPROVED')
        # Re-approving the same proposal is refused (defeats double-execution).
        with pytest.raises(InvalidProposalTransition):
            proposal_store.update_status(pid, 'APPROVED')
        proposal_store.update_status(pid, 'EXECUTED', order_ids=[1])
        # EXECUTED is terminal — cannot re-approve
        with pytest.raises(InvalidProposalTransition):
            proposal_store.update_status(pid, 'APPROVED')

    def test_try_transition_is_atomic_cas(self, proposal_store):
        """Exactly one of two concurrent PENDING->APPROVED claims wins."""
        pid = proposal_store.add(_make_proposal())
        assert proposal_store.try_transition(pid, 'PENDING', 'APPROVED') is True
        # Second claimant loses — row is no longer PENDING.
        assert proposal_store.try_transition(pid, 'PENDING', 'APPROVED') is False
        # Legal onward CAS works; terminal is immutable.
        assert proposal_store.try_transition(pid, 'APPROVED', 'EXECUTED', order_ids=[1]) is True
        assert proposal_store.try_transition(pid, 'APPROVED', 'FAILED') is False

    def test_unknown_status_raises(self, proposal_store):
        from trader.data.proposal_store import InvalidProposalTransition

        pid = proposal_store.add(_make_proposal())
        with pytest.raises(InvalidProposalTransition):
            proposal_store.update_status(pid, 'BOGUS')

    def test_update_status_missing_proposal_raises(self, proposal_store):
        from trader.data.proposal_store import InvalidProposalTransition

        with pytest.raises(InvalidProposalTransition):
            proposal_store.update_status(999, 'APPROVED')

    def test_update_status_with_rejection_reason(self, proposal_store):
        pid = proposal_store.add(_make_proposal())
        proposal_store.update_status(pid, 'REJECTED', rejection_reason='Changed thesis')
        p = proposal_store.get(pid)
        assert p.status == 'REJECTED'
        assert p.rejection_reason == 'Changed thesis'

    def test_status_transitions(self, proposal_store):
        pid = proposal_store.add(_make_proposal())
        assert proposal_store.get(pid).status == 'PENDING'

        proposal_store.update_status(pid, 'APPROVED')
        assert proposal_store.get(pid).status == 'APPROVED'

        proposal_store.update_status(pid, 'EXECUTED', order_ids=[42])
        p = proposal_store.get(pid)
        assert p.status == 'EXECUTED'
        assert p.order_ids == [42]

    def test_delete(self, proposal_store):
        pid = proposal_store.add(_make_proposal())
        assert proposal_store.get(pid) is not None
        result = proposal_store.delete(pid)
        assert result is True
        assert proposal_store.get(pid) is None

    def test_delete_nonexistent(self, proposal_store):
        result = proposal_store.delete(999)
        assert result is True  # no row to delete, but query returns 0 count

    def test_json_round_trip_execution(self, proposal_store):
        """Verify ExecutionSpec survives JSON serialization in DuckDB."""
        spec = ExecutionSpec(
            order_type='LIMIT',
            limit_price=250.0,
            exit_type='TRAILING_STOP',
            trailing_stop_percent=2.5,
            tif='GTC',
            outside_rth=True,
        )
        pid = proposal_store.add(TradeProposal(
            symbol='NVDA',
            action='BUY',
            quantity=50,
            execution=spec,
        ))
        result = proposal_store.get(pid)
        assert result.execution.order_type == 'LIMIT'
        assert result.execution.limit_price == 250.0
        assert result.execution.exit_type == 'TRAILING_STOP'
        assert result.execution.trailing_stop_percent == 2.5
        assert result.execution.tif == 'GTC'
        assert result.execution.outside_rth is True

    def test_json_round_trip_metadata(self, proposal_store):
        """Verify metadata dict survives JSON serialization in DuckDB."""
        meta = {
            'scanner_preset': 'momentum',
            'indicators': {'rsi': 72.5, 'macd_signal': True},
            'news': 'Earnings beat',
        }
        pid = proposal_store.add(TradeProposal(
            symbol='AAPL',
            action='BUY',
            quantity=10,
            metadata=meta,
        ))
        result = proposal_store.get(pid)
        assert result.metadata == meta
        assert result.metadata['indicators']['rsi'] == 72.5

    def test_update_metadata_merges(self, proposal_store):
        """update_metadata merges new keys into existing metadata."""
        pid = proposal_store.add(TradeProposal(
            symbol='AMD',
            action='BUY',
            quantity=100,
            metadata={'original_key': 'original_value'},
        ))
        leverage_info = {
            'current_leverage': 1.12,
            'estimated_leverage': 1.45,
            'net_liquidation': 987654.0,
            'buying_power': 523450.0,
            'uses_margin': True,
        }
        proposal_store.update_metadata(pid, {'leverage_estimate': leverage_info})
        result = proposal_store.get(pid)
        assert result.metadata['original_key'] == 'original_value'
        assert result.metadata['leverage_estimate']['current_leverage'] == 1.12
        assert result.metadata['leverage_estimate']['uses_margin'] is True

    def test_update_metadata_nonexistent(self, proposal_store):
        """update_metadata on non-existent proposal does nothing."""
        proposal_store.update_metadata(999, {'key': 'value'})  # should not raise

    def test_updated_at_changes_on_status_update(self, proposal_store):
        pid = proposal_store.add(_make_proposal())
        p1 = proposal_store.get(pid)

        import time
        time.sleep(0.01)

        proposal_store.update_status(pid, 'APPROVED')
        p2 = proposal_store.get(pid)
        assert p2.updated_at >= p1.updated_at

    def test_group_round_trip(self, proposal_store):
        """Group field survives storage via _group metadata key."""
        proposal = TradeProposal(
            symbol='BHP',
            action='BUY',
            quantity=100,
            group='mining',
            exchange='ASX',
            currency='AUD',
        )
        pid = proposal_store.add(proposal)
        result = proposal_store.get(pid)
        assert result.group == 'mining'
        assert result.exchange == 'ASX'
        assert result.currency == 'AUD'
        # _group should not leak into user-visible metadata
        assert '_group' not in result.metadata

    def test_group_empty_not_stored(self, proposal_store):
        """Empty group string is not stored in metadata."""
        pid = proposal_store.add(_make_proposal())
        result = proposal_store.get(pid)
        assert result.group == ''
        assert '_group' not in result.metadata

    def test_claim_for_approval_expires_elapsed_row(self, proposal_store):
        from trader.data.proposal_store import ApprovalClaimResult

        pid = proposal_store.add(_make_proposal(source="strategy:orb"))
        proposal_store.update_metadata(pid, {"expires_at": "2026-07-15T10:00:00Z"})
        result = proposal_store.claim_for_approval(
            pid, dt.datetime(2026, 7, 15, 10, 0, 1, tzinfo=dt.timezone.utc)
        )
        assert result is ApprovalClaimResult.EXPIRED
        assert proposal_store.get(pid).status == "EXPIRED"

    def test_claim_for_approval_allows_missing_legacy_expiry(self, proposal_store):
        from trader.data.proposal_store import ApprovalClaimResult

        pid = proposal_store.add(_make_proposal(source="manual"))
        result = proposal_store.claim_for_approval(
            pid, dt.datetime(2026, 7, 15, 10, 0, tzinfo=dt.timezone.utc)
        )
        assert result is ApprovalClaimResult.CLAIMED
        assert proposal_store.get(pid).status == "APPROVED"

    def test_claim_for_approval_rejects_naive_expiry(self, proposal_store):
        from trader.data.proposal_store import ApprovalClaimResult

        pid = proposal_store.add(_make_proposal(source="strategy:orb"))
        proposal_store.update_metadata(pid, {"expires_at": "2026-07-15T10:30:00"})
        result = proposal_store.claim_for_approval(
            pid, dt.datetime(2026, 7, 15, 10, 0, tzinfo=dt.timezone.utc)
        )
        assert result is ApprovalClaimResult.EXPIRED

    def test_claim_for_approval_not_pending_returns_not_pending(self, proposal_store):
        from trader.data.proposal_store import ApprovalClaimResult

        pid = proposal_store.add(_make_proposal(source="strategy:orb"))
        # Already claimed by someone else (PENDING -> APPROVED).
        assert proposal_store.try_transition(pid, "PENDING", "APPROVED") is True
        result = proposal_store.claim_for_approval(
            pid, dt.datetime(2026, 7, 15, 10, 0, tzinfo=dt.timezone.utc)
        )
        assert result is ApprovalClaimResult.NOT_PENDING
        # Status is left untouched — the second claimant does not re-mark it.
        assert proposal_store.get(pid).status == "APPROVED"

    def test_claim_for_approval_nonexistent_returns_not_found(self, proposal_store):
        from trader.data.proposal_store import ApprovalClaimResult

        result = proposal_store.claim_for_approval(
            999, dt.datetime(2026, 7, 15, 10, 0, tzinfo=dt.timezone.utc)
        )
        assert result is ApprovalClaimResult.NOT_FOUND

    def test_expire_stale_pending_expires_elapsed_aware(self, proposal_store):
        pid = proposal_store.add(_make_proposal(source="strategy:orb"))
        proposal_store.update_metadata(pid, {"expires_at": "2026-07-15T10:00:00Z"})
        expired = proposal_store.expire_stale_pending(
            dt.datetime(2026, 7, 15, 10, 0, 1, tzinfo=dt.timezone.utc)
        )
        assert expired == [pid]
        assert proposal_store.get(pid).status == "EXPIRED"

    def test_expire_stale_pending_skips_missing_legacy_expiry(self, proposal_store):
        pid = proposal_store.add(_make_proposal(source="manual"))
        expired = proposal_store.expire_stale_pending(
            dt.datetime(2026, 7, 15, 10, 0, tzinfo=dt.timezone.utc)
        )
        assert pid not in expired
        assert proposal_store.get(pid).status == "PENDING"

    def test_expire_stale_pending_expires_naive_and_garbage(self, proposal_store):
        naive = proposal_store.add(_make_proposal(symbol="AMD", source="strategy:orb"))
        proposal_store.update_metadata(naive, {"expires_at": "2026-07-15T10:30:00"})
        garbage = proposal_store.add(_make_proposal(symbol="AAPL", source="llm"))
        proposal_store.update_metadata(garbage, {"expires_at": "not-a-timestamp"})

        expired = proposal_store.expire_stale_pending(
            dt.datetime(2026, 7, 15, 10, 0, tzinfo=dt.timezone.utc)
        )
        assert set(expired) == {naive, garbage}
        assert proposal_store.get(naive).status == "EXPIRED"
        assert proposal_store.get(garbage).status == "EXPIRED"

    def test_expire_stale_pending_no_limit_no_source_filter(self, proposal_store):
        now = dt.datetime(2026, 7, 15, 10, 0, tzinfo=dt.timezone.utc)

        # Several stale PENDING rows with mixed sources (elapsed / naive / garbage).
        stale = []
        s1 = proposal_store.add(_make_proposal(symbol="S1", source="strategy:orb"))
        proposal_store.update_metadata(s1, {"expires_at": "2026-07-15T09:00:00Z"})
        stale.append(s1)
        s2 = proposal_store.add(_make_proposal(symbol="S2", source="llm"))
        proposal_store.update_metadata(s2, {"expires_at": "2026-07-15T09:30:00"})  # naive
        stale.append(s2)
        s3 = proposal_store.add(_make_proposal(symbol="S3", source="manual"))
        proposal_store.update_metadata(s3, {"expires_at": "garbage"})  # invalid
        stale.append(s3)

        # One fresh-future row and one missing-expiry legacy row — must be untouched.
        fresh = proposal_store.add(_make_proposal(symbol="FRESH", source="strategy:orb"))
        proposal_store.update_metadata(fresh, {"expires_at": "2026-07-15T11:00:00Z"})
        missing = proposal_store.add(_make_proposal(symbol="MISSING", source="manual"))

        expired = proposal_store.expire_stale_pending(now)

        assert set(expired) == set(stale)
        for pid in stale:
            assert proposal_store.get(pid).status == "EXPIRED"
        assert proposal_store.get(fresh).status == "PENDING"
        assert proposal_store.get(missing).status == "PENDING"
