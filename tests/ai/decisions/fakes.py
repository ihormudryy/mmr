"""Doubles for the decision engine tests: scripted trader reads and a scripted OpenRouter provider."""
from __future__ import annotations

import datetime as dt
from typing import Any, Callable, Union

import httpx

from tests.ai.fakes import FakeProvider
from trader.ai.rpc_clients import RpcNotSent

AAPL, MSFT = 265598, 272093
NOW = dt.datetime(2026, 7, 17, 15, 0, tzinfo=dt.timezone.utc)          # 11:00 New York
STRATEGY_DIGEST, DISCRETIONARY_DIGEST = "sha256:" + "a" * 64, "sha256:" + "d" * 64
LIMITS = {"max_positions": 3, "position_fraction": 0.05, "gross_fraction": 0.06, "trade_risk_fraction": 0.002,
          "daily_loss_fraction": 0.005, "drawdown_fraction": 0.03, "max_pending_entry_orders": 3}


def entry_quote_reply(conid=AAPL, bid=229.9, ask=230.0, at=NOW, feed="live", session_state="continuous",
                      accepted=("live",)):
    """Plan 3's get_ai_entry_quote reply: the trader's quote and the trader's accepted feeds."""
    return {"conid": conid, "read_at": NOW.isoformat(), "account_mode": "paper", "accepted_feeds": list(accepted),
            "quote": {"bid": bid, "ask": ask, "bid_size": 300.0, "ask_size": 300.0,
                      "market_timestamp": None if at is None else at.isoformat(), "feed": feed,
                      "session_state": session_state}}


def deployment_reply(body: dict) -> dict:
    if body["digest"] == STRATEGY_DIGEST:
        return {"digest": body["digest"], "error_code": None, "kind": "strategy",
                "deployment": {"decider_verdict": "DEPLOY", "conids": [AAPL, MSFT],
                               "evidence_order_notional": 25_000.0}}
    return {"digest": body["digest"], "error_code": None, "kind": "discretionary",
            "deployment": {"kind": "discretionary", "scope_rule": {"max_order_share_of_dollar_volume": 0.01}}}


class FakeReads:
    """ReadOnlySupervisor double. Replies per method: a dict, a callable(body) or an exception to raise."""

    def __init__(self, **overrides: Any):
        self.replies: dict[str, Any] = {
            "get_ai_entry_quote": lambda body: entry_quote_reply(conid=body["conid"]),
            "get_ai_risk_policy": {"latest_published_revision": 1, "effective": LIMITS, "latest_published": LIMITS},
            "get_ai_deployment": deployment_reply,
            "get_account_values": {"NetLiquidation": {"value": "100000.0", "currency": "USD"}},
            "get_positions": {"positions": []},
            "get_experiment_trips": {"experiment_id": "exp-" + "b" * 20, "trips": []},
            "get_broker_order_evidence": {"generation_id": 7, "promoted": True, "orders": []},
        }
        self.replies.update(overrides)
        self.calls: list[tuple[str, dict]] = []

    async def call(self, method, body):
        self.calls.append((method, dict(body)))
        reply = self.replies[method]
        if isinstance(reply, Exception):
            raise reply
        return reply(body) if callable(reply) else reply


Reply = Union[str, int, Callable[[httpx.Request], Union[str, int]]]


class ScriptedProvider(FakeProvider):
    """A fake OpenRouter behind the real adapter. Replies are queued per marker found in the request body:
    a str is the model's text, an int an HTTP status, a callable runs during the call and returns either."""

    def __init__(self, model: str):
        super().__init__(model=model)
        self.queues: dict[str, list[Reply]] = {}
        self.respond = self._answer

    def script(self, marker: str, *replies: Reply) -> None:
        self.queues.setdefault(marker, []).extend(replies)

    def _answer(self, request: httpx.Request) -> httpx.Response:
        text = request.content.decode()
        marker = next((m for m in self.queues if m in text and self.queues[m]), None)
        if marker is None:
            raise AssertionError(f"unexpected call to {self.model}")
        reply = self.queues[marker].pop(0)
        if callable(reply):
            reply = reply(request)
        if isinstance(reply, int):
            return httpx.Response(reply, json={"error": {"message": "scripted"}})
        return httpx.Response(200, json={
            "id": f"gen-{len(self.requests)}", "model": self.model,
            "choices": [{"message": {"content": reply}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 900, "completion_tokens": 60}})


def trader_down(code="TRADER_UNREACHABLE"):
    return RpcNotSent(code)
