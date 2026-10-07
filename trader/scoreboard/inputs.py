"""Sealed inputs that SP2 fills: AI call costs and simulated books (spec 5.2, ruling 16).

SP1 creates the tables and shows them. Neither function is exposed over RPC.
"""
from __future__ import annotations

import datetime as dt
from typing import Optional

from trader.scoreboard.store import ScoreboardStore


def record_ai_cost(store: ScoreboardStore, *, call_id: str, provider: str, model: str,
                   input_tokens: Optional[int], output_tokens: Optional[int], cost_usd: Optional[float],
                   called_at: dt.datetime, served_kind: str, served_id: str,
                   experiment_id: Optional[str] = None) -> None:
    """One model call; ``cost_usd=None`` is an unknown cost, never 0."""
    store.insert_sealed("ai_costs", {
        "call_id": call_id, "experiment_id": experiment_id, "provider": provider, "model": model,
        "input_tokens": input_tokens, "output_tokens": output_tokens, "cost_usd": cost_usd,
        "called_at": called_at, "served_kind": served_kind, "served_id": served_id})


def record_simulated_row(store: ScoreboardStore, *, book_id: str, experiment_id: str, session_date: dt.date,
                         baseline: str, pnl_usd: Optional[float], trades: Optional[int]) -> None:
    """Every simulated row is labelled ``simulated``; the caller cannot choose another label."""
    store.insert_sealed("simulated_books", {
        "book_id": book_id, "experiment_id": experiment_id, "session_date": session_date, "baseline": baseline,
        "label": "simulated", "pnl_usd": pnl_usd, "trades": trades, "created_at": store.now()})

