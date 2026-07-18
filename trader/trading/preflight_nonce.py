"""Production preflight nonce gate.

Design: docs/superpowers/specs/2026-07-18-command-plane-activation-design.md
(constraint C5 / sequence step 5).

The coordinator's ``PreflightNonceGate`` port was a ``Protocol`` with only test
fakes -- "issuing nonces is [M1-C]'s job". This is that job: a single-use,
TTL-bounded nonce bound to a command's identity, consumed ATOMICALLY inside the
command's own claiming transaction so it can be spent exactly once and can never
authorize a materially different (mutated) request.
"""
from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, Callable, Optional

from trader.data.domain_journal import DomainJournal
from trader.data.schema_migrations import SchemaMigrator
from trader.trading.command_coordinator import CommandRequest, canonical_request_hash

# [M1-F3] owns journal-DB migration versions 20-29 (proposal authority = 20,
# command ledger = 21, trading control = 22); the [M1-C] preflight nonce store
# takes the next free version, 23.
PREFLIGHT_NONCE_MIGRATION_VERSION = 23
PREFLIGHT_NONCE_MIGRATION_NAME = "m1c_preflight_nonces"

_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS preflight_nonces (
        nonce VARCHAR PRIMARY KEY,
        command_id VARCHAR NOT NULL,
        account_id VARCHAR,
        account_mode VARCHAR NOT NULL,
        session_fingerprint VARCHAR NOT NULL,
        request_hash VARCHAR NOT NULL,
        issued_at TIMESTAMPTZ NOT NULL,
        expires_at TIMESTAMPTZ NOT NULL,
        consumed BOOLEAN NOT NULL
    )
    """,
)


def apply_preflight_nonce_migration(migrator: SchemaMigrator) -> None:
    """Create ``preflight_nonces`` in the journal DB (idempotent)."""
    migrator.apply(
        version=PREFLIGHT_NONCE_MIGRATION_VERSION,
        name=PREFLIGHT_NONCE_MIGRATION_NAME,
        statements=list(_STATEMENTS),
    )


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _as_utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc)


class PreflightNonceGate:
    """Single-use, TTL-bounded preflight nonces bound to a command's identity.

    ``issue`` binds the nonce to ``command_id`` + account + account mode +
    session fingerprint + the canonical request hash (design C5) and returns it.
    ``consume_in_tx`` re-verifies everything the ``CommandRequest`` carries
    (command_id, account, the canonical hash of action/target/expected-version/
    body, and the issuing ``session_fingerprint``), plus not-expired and
    not-already-consumed, then spends the nonce -- ALL on the caller's ``conn``,
    so the spend commits together with the command claim: a nonce can never be
    double-spent, replayed, made to authorize a mutated request, or replayed
    from a different session. ``account_mode`` is recorded for audit but not
    re-compared at consume: it is subsumed by the account_id binding (paper vs
    live accounts have distinct ids) and is fixed per single-mode trader.
    """

    def __init__(self, journal: DomainJournal, *,
                 ttl_seconds: float = 120.0,
                 now: Callable[[], dt.datetime] = _utcnow):
        self._journal = journal
        self._ttl = ttl_seconds
        self._now = now

    def issue(self, *, command_id: str, account_id: Optional[str],
              account_mode: str, session_fingerprint: str,
              request_hash: str) -> str:
        nonce = uuid.uuid4().hex
        issued = self._now()
        expires = issued + dt.timedelta(seconds=self._ttl)
        self._journal.db.transaction(lambda conn: conn.execute(
            "INSERT INTO preflight_nonces (nonce, command_id, account_id, "
            "account_mode, session_fingerprint, request_hash, issued_at, "
            "expires_at, consumed) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [nonce, command_id, account_id, account_mode, session_fingerprint,
             request_hash, issued, expires, False],
        ))
        return nonce

    def consume_in_tx(self, conn: Any, nonce: Optional[str],
                      request: CommandRequest) -> bool:
        if not nonce:
            return False
        row = conn.execute(
            "SELECT command_id, account_id, request_hash, session_fingerprint, "
            "expires_at, consumed FROM preflight_nonces WHERE nonce = ?",
            [nonce]).fetchone()
        if row is None:
            return False
        command_id, account_id, request_hash, session_fingerprint, expires_at, consumed = row
        if consumed:
            return False
        if self._now() > _as_utc(expires_at):
            return False
        # Bindings a mismatch on means this nonce was issued for a different /
        # mutated command, or from a different session, and must not authorize
        # this request.
        if command_id != request.command_id:
            return False
        if account_id != request.account_id:
            return False
        if request_hash != canonical_request_hash(request):
            return False
        # Session binding: the command must be submitted under the SAME session
        # the nonce was issued to (fail closed if the request carries no
        # fingerprint). NOTE: account_mode is recorded at issue for audit but is
        # deliberately NOT re-compared here -- it is subsumed by the account_id
        # binding above (a paper vs a live account has a distinct account_id) and
        # is fixed for a single-mode trader process, so a separate comparison
        # could never fail.
        if session_fingerprint != (request.session_fingerprint or ""):
            return False
        conn.execute(
            "UPDATE preflight_nonces SET consumed = TRUE WHERE nonce = ?", [nonce])
        return True
