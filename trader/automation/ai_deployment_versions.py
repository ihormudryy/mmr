"""Sealed deployment versions and operator withdrawals (SP2c spec 5.2 item 5).

A version binds one DEPLOY judgment to one base deployment for a window of
sessions. The base ``ai_deployments`` row keeps its SP1 meaning; only the
version is authority in SP2c. Both tables are insert-only.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import re
from dataclasses import dataclass, fields
from typing import Any, Callable, Optional

from trader.automation.ai_deployments import DeploymentRefused
from trader.data.schema_migrations import SchemaMigrator
from trader.domain.events import DomainMutation
from trader.research.canonical import canonical_json_bytes

AI_DEPLOYMENT_VERSION_MIGRATION_VERSION = 115
AI_DEPLOYMENT_WITHDRAWAL_MIGRATION_VERSION = 116
INITIAL = "INITIAL"
RENEWAL = "RENEWAL"
WITHDRAWAL_ENTRY_IN_FLIGHT = "WITHDRAWAL_ENTRY_IN_FLIGHT"
_VERSION_DOMAIN = b"mmr.ai-deployment-version.v1\x00"
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_JUDGMENT_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_COLUMNS = "digest, record_json, request_digest, sealed_at, judgment_id, base_digest, prior_version"


def withdrawal_outcome(digest: str, *, newly: bool) -> dict:
    """The receipt outcome of a withdraw_ai_deployment command."""
    return {"version_digest": digest, "withdrawn": True, "already_withdrawn": not newly}


def apply_ai_deployment_version_migrations(migrator: SchemaMigrator) -> None:
    migrator.apply(AI_DEPLOYMENT_VERSION_MIGRATION_VERSION, "sp2c_ai_deployment_versions", (
        """CREATE TABLE IF NOT EXISTS ai_deployment_versions (
            digest VARCHAR PRIMARY KEY, judgment_id VARCHAR NOT NULL UNIQUE, base_digest VARCHAR NOT NULL,
            prior_version VARCHAR UNIQUE, record_json VARCHAR NOT NULL, request_digest VARCHAR NOT NULL,
            principal VARCHAR NOT NULL, command_id VARCHAR NOT NULL, sealed_at TIMESTAMPTZ NOT NULL)""",))
    migrator.apply(AI_DEPLOYMENT_WITHDRAWAL_MIGRATION_VERSION, "sp2c_ai_deployment_withdrawals", (
        """CREATE TABLE IF NOT EXISTS ai_deployment_withdrawals (
            version_digest VARCHAR PRIMARY KEY, reason VARCHAR NOT NULL, principal VARCHAR NOT NULL,
            command_id VARCHAR NOT NULL, withdrawn_at TIMESTAMPTZ NOT NULL)""",))


def _invalid(rule: str) -> DeploymentRefused:
    return DeploymentRefused("DEPLOYMENT_VERSION_INVALID", rule)


@dataclass(frozen=True)
class DeploymentVersion:
    base_digest: str
    judgment_id: str
    kind: str
    prior_version: Optional[str]
    first_session: dt.date
    expiry_session: dt.date
    binding_verified_by_bundle: bool = True

    def __post_init__(self):
        if not isinstance(self.base_digest, str) or not _SHA256.match(self.base_digest):
            raise _invalid("base_digest must be sha256:<64 hex>")
        if not isinstance(self.judgment_id, str) or not _JUDGMENT_ID.match(self.judgment_id):
            raise _invalid("judgment_id must match ^[A-Za-z0-9_.:-]{1,128}$")
        if self.kind not in (INITIAL, RENEWAL):
            raise _invalid("kind must be INITIAL or RENEWAL")
        if (self.kind == INITIAL) != (self.prior_version is None):
            raise _invalid("an INITIAL version has no prior version; a RENEWAL names one")
        if self.prior_version is not None and (not isinstance(self.prior_version, str)
                                               or not _SHA256.match(self.prior_version)):
            raise _invalid("prior_version must be sha256:<64 hex>")
        if type(self.first_session) is not dt.date or type(self.expiry_session) is not dt.date:
            raise _invalid("sessions must be dates, not datetimes")     # datetime is a date subclass
        if self.expiry_session < self.first_session:
            raise _invalid("expiry_session is before first_session")
        if self.binding_verified_by_bundle is not True:
            raise _invalid("a version exists only after the bundle check")

    def to_json(self) -> dict:
        record = {f.name: getattr(self, f.name) for f in fields(self)}
        record["first_session"] = self.first_session.isoformat()
        record["expiry_session"] = self.expiry_session.isoformat()
        return record

    @classmethod
    def from_json(cls, value: Any) -> "DeploymentVersion":
        names = {f.name for f in fields(cls)}
        if not isinstance(value, dict) or set(value) != names:
            raise _invalid(f"a version has exactly the keys {sorted(names)}")
        try:
            sessions = {k: dt.date.fromisoformat(value[k]) for k in ("first_session", "expiry_session")}
        except (TypeError, ValueError):
            raise _invalid("sessions must be ISO dates") from None
        return cls(**{**value, **sessions})


def version_digest(version: DeploymentVersion) -> str:
    return "sha256:" + hashlib.sha256(_VERSION_DOMAIN + canonical_json_bytes(version.to_json())).hexdigest()


@dataclass(frozen=True)
class SealedVersion:
    digest: str
    version: DeploymentVersion
    request_digest: str
    sealed_at: dt.datetime


class AiDeploymentVersionStore:
    def __init__(self, journal: Any, now: Callable[[], dt.datetime]):
        """``journal`` is the trader's DomainJournal: a withdrawal shares its write lock with the saga's
        SUBMITTING row, so the two are ordered (PR #95). Reads use its DuckDB file directly."""
        self._journal = journal
        self._db = journal.db
        self._now = now
        # Built once when the trader starts: a send of an earlier process can never return or be re-sent.
        self._process_started_at = now()

    def seal_in_tx(self, conn, version: DeploymentVersion, *, request_digest: str, principal: str,
                   command_id: str) -> tuple[str, bool]:
        """One version per judgment. The same registration request returns the first version, even when
        its sessions would differ today; another request for a bound judgment is refused."""
        bound = conn.execute("SELECT digest, request_digest FROM ai_deployment_versions WHERE judgment_id = ?",
                             [version.judgment_id]).fetchone()
        if bound is not None:
            if hmac.compare_digest(bound[1], request_digest):
                return bound[0], False
            raise DeploymentRefused("JUDGMENT_ALREADY_BOUND", f"judgment {version.judgment_id} has a version")
        if version.prior_version is not None and conn.execute(
                "SELECT 1 FROM ai_deployment_versions WHERE prior_version = ?", [version.prior_version]).fetchone():
            raise DeploymentRefused("RENEWAL_PRIOR_INVALID", "the prior version was already renewed")
        digest = version_digest(version)
        conn.execute(
            "INSERT INTO ai_deployment_versions (digest, judgment_id, base_digest, prior_version, record_json, "
            "request_digest, principal, command_id, sealed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [digest, version.judgment_id, version.base_digest, version.prior_version,
             canonical_json_bytes(version.to_json()).decode("utf-8"), request_digest, principal, command_id,
             self._now()])
        return digest, True

    def _parse(self, row: tuple) -> SealedVersion:
        digest, record_json, request_digest, sealed_at, *lookup_columns = row
        try:
            version = DeploymentVersion.from_json(json.loads(record_json))
        except (DeploymentRefused, ValueError):
            raise DeploymentRefused("DEPLOYMENT_VERSION_TAMPERED", f"{digest} does not parse") from None
        if not hmac.compare_digest(version_digest(version), digest):
            raise DeploymentRefused("DEPLOYMENT_VERSION_TAMPERED", f"{digest} does not match its record")
        # Lookups and uniqueness use these columns, so they must say what the sealed record says.
        if tuple(lookup_columns) != (version.judgment_id, version.base_digest, version.prior_version):
            raise DeploymentRefused("DEPLOYMENT_VERSION_TAMPERED", f"{digest} columns differ from its record")
        return SealedVersion(digest, version, request_digest, sealed_at)

    def bound_to_judgment(self, judgment_id: str) -> Optional[SealedVersion]:
        row = self._db.execute(f"SELECT {_COLUMNS} FROM ai_deployment_versions WHERE judgment_id = ?",
                               [judgment_id], fetch="one")
        return None if row is None else self._parse(row)

    def bound_in_tx(self, conn, judgment_id: str) -> Optional[SealedVersion]:
        row = conn.execute(f"SELECT {_COLUMNS} FROM ai_deployment_versions WHERE judgment_id = ?",
                           [judgment_id]).fetchone()
        return None if row is None else self._parse(row)

    def sealed_by_command(self, command_id: str) -> Optional[SealedVersion]:
        """The version this command's registration transaction committed, if it committed one."""
        row = self._db.execute(f"SELECT {_COLUMNS} FROM ai_deployment_versions WHERE command_id = ?",
                               [command_id], fetch="one")
        return None if row is None else self._parse(row)

    def version_for_judgment(self, judgment_id: str) -> Optional[str]:
        sealed = self.bound_to_judgment(judgment_id)
        return None if sealed is None else sealed.digest

    def get(self, digest: str) -> DeploymentVersion:
        if not isinstance(digest, str) or not _SHA256.match(digest):
            raise DeploymentRefused("DEPLOYMENT_VERSION_UNKNOWN", "not a deployment version digest")
        row = self._db.execute(f"SELECT {_COLUMNS} FROM ai_deployment_versions WHERE digest = ?", [digest],
                               fetch="one")
        if row is None:
            raise DeploymentRefused("DEPLOYMENT_VERSION_UNKNOWN", "no sealed version has this digest")
        return self._parse(row).version

    def sealed_in_tx(self, conn) -> tuple[SealedVersion, ...]:
        rows = conn.execute(f"SELECT {_COLUMNS} FROM ai_deployment_versions ORDER BY sealed_at, digest").fetchall()
        return tuple(self._parse(row) for row in rows)

    def sealed(self) -> tuple[SealedVersion, ...]:
        return self._db.transaction(self.sealed_in_tx)

    def withdrawn_in_tx(self, conn) -> frozenset[str]:
        return frozenset(row[0] for row in conn.execute(
            "SELECT version_digest FROM ai_deployment_withdrawals").fetchall())

    def withdrawn(self) -> frozenset[str]:
        return self._db.transaction(self.withdrawn_in_tx)

    def is_withdrawn_in_tx(self, conn, digest: str) -> bool:
        return conn.execute("SELECT 1 FROM ai_deployment_withdrawals WHERE version_digest = ?",
                            [digest]).fetchone() is not None

    def committed_withdrawal(self, command_id: str) -> Optional[dict]:
        """The success outcome of the command whose own transaction wrote a withdrawal row, or None.

        The row carries the command id in the same transaction, so it is proof of the commit. A command that
        found the version already withdrawn wrote nothing and gets None."""
        row = self._db.execute("SELECT version_digest FROM ai_deployment_withdrawals WHERE command_id = ?",
                               [command_id], fetch="one")
        return None if row is None else withdrawal_outcome(row[0], newly=True)

    def entries_being_sent_in_tx(self, conn, digest: str) -> tuple[str, ...]:
        """Command ids of entries bound to this version whose broker send has not returned.

        A send is in flight from the saga's SUBMITTING row until ``send_returned_at`` is written, whatever state
        broker events moved the saga to meanwhile (``unreturned_send_sql``); the decision row carries the version
        the entry was admitted under."""
        from trader.automation.protective_order_saga import unreturned_send_sql

        rows = conn.execute(
            "SELECT s.command_id FROM automated_order_sagas s "
            "JOIN ai_paper_decisions d ON d.command_id = s.command_id "
            f"WHERE d.deployment_version = ? AND {unreturned_send_sql('s')} "
            "ORDER BY s.command_id",
            [digest, self._process_started_at]).fetchall()
        return tuple(row[0] for row in rows)

    def withdraw(self, digest: str, *, reason: str, principal: str, command_id: str) -> bool:
        """True when this call withdrew the version, False when it was withdrawn before. A renewed (superseded)
        version is refused with VERSION_SUPERSEDED: it trades no more, and its successor is the one to withdraw.

        A journal mutation: it commits either before an entry's SUBMITTING row (that entry is refused) or
        after it. In the second case the entry is still being sent, so the withdrawal is refused until the
        send returns: a successful receipt is never followed by a new broker plan (PR #95 round 2)."""
        now = self._now()

        def write(conn, append) -> bool:
            if not isinstance(digest, str) or not conn.execute(
                    "SELECT 1 FROM ai_deployment_versions WHERE digest = ?", [digest]).fetchone():
                raise DeploymentRefused("DEPLOYMENT_VERSION_UNKNOWN", "no sealed version has this digest")
            if self.is_withdrawn_in_tx(conn, digest):
                return False
            successor = conn.execute("SELECT digest FROM ai_deployment_versions WHERE prior_version = ?",
                                     [digest]).fetchone()
            if successor is not None:
                raise DeploymentRefused("VERSION_SUPERSEDED",
                                        f"{digest} is superseded by {successor[0]}; withdraw that version")
            in_flight = self.entries_being_sent_in_tx(conn, digest)
            if in_flight:
                raise DeploymentRefused(
                    WITHDRAWAL_ENTRY_IN_FLIGHT,
                    f"an entry of this version is being sent ({', '.join(in_flight)}); "
                    "retry after the entry's send returns")
            mutation = DomainMutation(
                event_type="ai_deployment_version.withdrawn", entity_type="ai_deployment_version",
                entity_id=digest, operation="upsert", account_id=None, source="trader_service",
                source_timestamp=now, correlation_id=command_id,
                payload={"state": "WITHDRAWN", "reason": reason, "principal": principal})

            def insert(conn, _revision: int) -> None:
                conn.execute("INSERT INTO ai_deployment_withdrawals (version_digest, reason, principal, "
                             "command_id, withdrawn_at) VALUES (?, ?, ?, ?, ?)",
                             [digest, reason, principal, command_id, now])
            append(mutation, insert, f"ai-deployment-withdrawal:{digest}")
            return True
        return self._journal.mutate_batch_work(self._journal.connect(), write)
