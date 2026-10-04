"""Unit tests for web/trader_link.py — the shared typed-RPC transport.

Covers the plumbing the facades used to each re-implement: endpoint parsing,
lazy build, the reconnect-on-transport-failure + close, the one transport-error
contract (timeout vs unavailable + cause), and that a healthy-socket
application error (TypedRpcRemoteError) propagates WITHOUT a reconnect.
"""
from __future__ import annotations

import threading

import pytest

from trader.messaging.typed_rpc import TypedRpcRemoteError
from web.trader_link import TraderLink, TraderLinkError, parse_endpoint


class FakeClient:
    def __init__(self):
        self.calls = []
        self.raise_exc: BaseException | None = None
        self.closed = False

    def call(self, method, body, response_model, timeout=None):
        self.calls.append((method, body, timeout))
        if self.raise_exc is not None:
            raise self.raise_exc
        return {"ok": True, "method": method}

    def close(self):
        self.closed = True


def _link_over(factory):
    return TraderLink(client_factory=factory)


# ── parse_endpoint ────────────────────────────────────────────────────────────

def test_parse_endpoint_splits_host_and_port():
    assert parse_endpoint("tcp://trader:42102") == ("tcp://trader", 42102)


@pytest.mark.parametrize("bad", ["trader:42102", "http://trader:42102", "tcp://trader"])
def test_parse_endpoint_rejects_non_tcp_or_portless(bad):
    with pytest.raises(ValueError):
        parse_endpoint(bad)


# ── lazy build + happy path ──────────────────────────────────────────────────

def test_builds_client_lazily_on_first_call_then_reuses():
    made = []

    def factory():
        c = FakeClient()
        made.append(c)
        return c

    link = _link_over(factory)
    assert made == []                      # nothing built until first call
    link.call("get_status", {})
    link.call("get_status", {"x": 1})
    assert len(made) == 1                  # same client reused
    assert made[0].calls[0][0] == "get_status"


def test_timeout_passed_through_only_when_set():
    c = FakeClient()
    link = _link_over(lambda: c)
    link.call("m", {})                     # no timeout
    link.call("m", {}, timeout=45.0)       # explicit timeout
    assert c.calls[0][2] is None
    assert c.calls[1][2] == 45.0


# ── the one transport error + reconnect ──────────────────────────────────────

def test_timeout_raises_traderlink_error_and_discards_client():
    first = FakeClient()
    first.raise_exc = TimeoutError("no reply")
    link = _link_over(lambda: first)
    with pytest.raises(TraderLinkError) as exc:
        link.call("approve", {})
    assert exc.value.kind == "timeout"
    assert isinstance(exc.value.cause, TimeoutError)
    assert first.closed                    # stale client discarded


def test_connection_error_is_unavailable_and_rebuilds_next_call():
    made: list[FakeClient] = []

    def factory():
        c = FakeClient()
        if not made:                       # only the first-built client fails
            c.raise_exc = ConnectionError("socket down")
        made.append(c)
        return c

    link = _link_over(factory)
    with pytest.raises(TraderLinkError) as exc:
        link.call("cancel", {})
    assert exc.value.kind == "unavailable"
    assert made[0].closed
    # next call builds a fresh client and succeeds
    result = link.call("cancel", {})
    assert result["ok"] is True
    assert len(made) == 2


def test_remote_error_propagates_without_reconnect():
    c = FakeClient()
    c.raise_exc = TypedRpcRemoteError(code="VERSION_CONFLICT", message="stale")
    link = _link_over(lambda: c)
    with pytest.raises(TypedRpcRemoteError):
        link.call("approve", {})
    # healthy socket: NOT discarded, so the same client is reused next time
    assert c.closed is False
    c.raise_exc = None
    link.call("approve", {})
    assert len(c.calls) == 2


# ── construction contract ─────────────────────────────────────────────────────

def test_requires_factory_or_role_endpoint():
    with pytest.raises(ValueError):
        TraderLink()                       # neither factory nor (role, endpoint)


def test_has_its_own_serialization_lock():
    link = _link_over(lambda: FakeClient())
    assert isinstance(link._lock, type(threading.Lock()))
