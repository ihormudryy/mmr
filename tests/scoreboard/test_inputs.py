import datetime as dt

import pytest

from tests.scoreboard.common import EXP_ID, NOW
from trader.scoreboard.inputs import record_ai_cost, record_simulated_row
from trader.scoreboard.store import ScoreboardConflict


def _cost(store, call_id="c1", cost_usd=None):
    record_ai_cost(store, call_id=call_id, provider="anthropic", model="m", input_tokens=10, output_tokens=5,
                   cost_usd=cost_usd, called_at=NOW, served_kind="decision", served_id="d1", experiment_id=EXP_ID)


def test_ai_cost_row_is_sealed_and_unknown_cost_stays_null(store):
    _cost(store)
    assert store.fetch("ai_costs", {})[0]["cost_usd"] is None and store.verify_seals() == []


def test_duplicate_call_id_is_a_conflict(store):
    _cost(store, cost_usd=0.1)
    with pytest.raises(ScoreboardConflict):
        _cost(store, cost_usd=0.2)


def test_simulated_row_is_always_labelled_simulated(store):
    record_simulated_row(store, book_id="b1", experiment_id=EXP_ID, session_date=dt.date(2026, 10, 6),
                         baseline="follow_the_signal", pnl_usd=None, trades=3)
    row = store.fetch("simulated_books", {})[0]
    assert row["label"] == "simulated" and row["pnl_usd"] is None and store.verify_seals() == []
