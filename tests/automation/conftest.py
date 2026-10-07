from decimal import Decimal

import pytest

from tests.automation.ai_paper_fixtures import (
    ACCOUNT, CONID, NOW, FakeUniverse, Margin, Quotes, SnapshotSequence, make_history, make_journal, secdef,
    snapshot,
)


@pytest.fixture
def parts(tmp_path):
    """The inputs of SP1's ``AiPaperEvidence`` (shared by the evidence and the baseline sizing tests)."""
    from trader.automation.ai_paper_filter import AiEntryFilter
    from trader.trading.trading_filter import TradingFilter
    return dict(
        broker=SnapshotSequence(snapshot()), quotes=Quotes(), margin=Margin(),
        history=make_history(str(tmp_path / "history.duckdb")),
        journal=make_journal(str(tmp_path / "journal.duckdb")),
        account_id=ACCOUNT, account_mode="paper", now=lambda: NOW, max_drift_bps=50.0,
        entry_offset_bps=Decimal("10"),
        entry_filter=AiEntryFilter(universe=FakeUniverse({CONID: secdef()}), load_filter=TradingFilter),
    )
