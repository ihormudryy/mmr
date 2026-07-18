"""Real preflight nonce gate (design C5 / sequence step 5).

The coordinator's PreflightNonceGate port was a Protocol with only test fakes.
This pins the production gate: a single-use, TTL-bounded nonce bound to the
command's identity (command_id + account + the canonical request hash), consumed
ATOMICALLY inside the command's own transaction so it can be spent exactly once
and can't be replayed for a mutated request.
"""
from __future__ import annotations

import datetime as dt

import pytest

from trader.data.domain_journal import DomainJournal
from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator
from trader.trading.command_coordinator import CommandRequest, canonical_request_hash
from trader.trading.preflight_nonce import (
    PreflightNonceGate,
    apply_preflight_nonce_migration,
)

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 7, 18, 14, 30, tzinfo=UTC)


def _request(*, command_id="cmd-1", action="approve_proposal", account_id="DU123",
             target_id="42", body=None):
    return CommandRequest(
        command_id=command_id, action=action, account_id=account_id,
        target_type="proposal", target_id=target_id, expected_version=None,
        body=(body if body is not None else {"amount": 5000}),
        source="dashboard", preflight_nonce=None)


@pytest.fixture
def clock():
    return {"now": NOW}


@pytest.fixture
def gate(tmp_path, clock):
    db = DuckDBConnection.get_instance(str(tmp_path / "journal.duckdb"))
    migrator = SchemaMigrator(db)
    journal = DomainJournal(db)
    journal.migrate(migrator)
    apply_preflight_nonce_migration(migrator)
    g = PreflightNonceGate(journal, ttl_seconds=120.0, now=lambda: clock["now"])
    return g, journal


def _issue(gate, request, **over):
    kw = dict(
        command_id=request.command_id, account_id=request.account_id,
        account_mode="paper", session_fingerprint="sess-abc",
        request_hash=canonical_request_hash(request))
    kw.update(over)
    return gate.issue(**kw)


def _consume(gate, journal, nonce, request):
    return journal.db.transaction(lambda conn: gate.consume_in_tx(conn, nonce, request))


def test_issue_then_consume_succeeds_exactly_once(gate):
    g, journal = gate
    req = _request()
    nonce = _issue(g, req)
    assert _consume(g, journal, nonce, req) is True
    # single use -- a replay of the SAME nonce fails
    assert _consume(g, journal, nonce, req) is False


def test_unknown_or_missing_nonce_is_rejected(gate):
    g, journal = gate
    req = _request()
    assert _consume(g, journal, "no-such-nonce", req) is False
    assert _consume(g, journal, None, req) is False
    assert _consume(g, journal, "", req) is False


def test_expired_nonce_is_rejected(gate, clock):
    g, journal = gate
    req = _request()
    nonce = _issue(g, req)
    clock["now"] = NOW + dt.timedelta(seconds=121)  # past the 120s TTL
    assert _consume(g, journal, nonce, req) is False


def test_mutated_request_body_is_rejected(gate):
    g, journal = gate
    issued_for = _request(body={"amount": 5000})
    nonce = _issue(g, issued_for)
    # Same command_id + target, but a materially different body -> different
    # canonical hash -> the nonce must NOT authorize it.
    tampered = _request(body={"amount": 5_000_000})
    assert _consume(g, journal, nonce, tampered) is False
    # ... and the original request still works.
    assert _consume(g, journal, nonce, issued_for) is True


def test_wrong_command_id_is_rejected(gate):
    g, journal = gate
    nonce = _issue(g, _request(command_id="cmd-1"))
    assert _consume(g, journal, nonce, _request(command_id="cmd-2")) is False


def test_wrong_account_is_rejected(gate):
    g, journal = gate
    nonce = _issue(g, _request(account_id="DU123"))
    assert _consume(g, journal, nonce, _request(account_id="DUother")) is False


def test_issue_records_binding_fields(gate):
    g, journal = gate
    req = _request()
    nonce = _issue(g, req, account_mode="live", session_fingerprint="sess-xyz")
    row = journal.connect().execute(
        "SELECT account_mode, session_fingerprint, request_hash, consumed "
        "FROM preflight_nonces WHERE nonce = ?", [nonce]).fetchone()
    assert row[0] == "live" and row[1] == "sess-xyz"
    assert row[2] == canonical_request_hash(req) and row[3] is False
