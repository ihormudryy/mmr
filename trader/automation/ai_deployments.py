"""Sealed ai_paper deployment records (spec 5.4, Plan 3 Task 5, R19).

``ai_research`` registers a deployment; registration validates and seals it in
one insert, so there is never an unsealed row. Every read recomputes the
digest. ``strategy_digest`` is recorded as a claim of ``ai_research``: nothing
in SP1 hashes the strategy file (SP2 follow-up).
"""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
import hmac
import json
import math
import re
from dataclasses import dataclass, fields
from types import MappingProxyType
from typing import Any, Callable, Mapping

from trader.automation.ai_paper_config import SUPPORTED_STYLES
from trader.data.schema_migrations import SchemaMigrator
from trader.objects import BarSize
from trader.research.canonical import canonical_json_bytes

AI_DEPLOYMENT_MIGRATION_VERSION = 55
STRATEGY_DIGEST_PROVENANCE = "CLAIMED_NOT_VERIFIED"
OPERATOR_ATTESTED = "OPERATOR_ATTESTED"
STRATEGY_KIND = "strategy"
DISCRETIONARY_KIND = "discretionary"
VERDICTS = ("DEPLOY", "SHADOW", "REJECT")
MAX_CONIDS = 20
_DIGEST_DOMAIN = b"mmr.ai-deployment.v1\x00"

_PATH = re.compile(r"^strategies/[A-Za-z0-9_]+(/[A-Za-z0-9_]+)*\.py$")
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_CLASS = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_DECIDER = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")


def apply_ai_deployment_migration(migrator: SchemaMigrator) -> bool:
    return migrator.apply(AI_DEPLOYMENT_MIGRATION_VERSION, "sp1_ai_deployments", (
        """CREATE TABLE IF NOT EXISTS ai_deployments (
            digest VARCHAR PRIMARY KEY, record_json VARCHAR NOT NULL, principal VARCHAR NOT NULL,
            command_id VARCHAR NOT NULL, sealed_at TIMESTAMPTZ NOT NULL,
            strategy_digest_provenance VARCHAR NOT NULL, kind VARCHAR NOT NULL)""",
    ))


class DeploymentRefused(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def _invalid(name: str, rule: str) -> DeploymentRefused:
    return DeploymentRefused("DEPLOYMENT_INVALID", f"{name} {rule}")


def _matching(pattern: re.Pattern) -> Callable[[str, Any], Any]:
    def check(name: str, value: Any) -> Any:
        if not isinstance(value, str) or not pattern.match(value):
            raise _invalid(name, f"must match {pattern.pattern}")
        return value
    return check


def _one_of(allowed) -> Callable[[str, Any], Any]:
    def check(name: str, value: Any) -> Any:
        if not isinstance(value, str) or value not in allowed:
            raise _invalid(name, f"must be one of {sorted(allowed)}")
        return value
    return check


def _evidence_ref(name: str, value: Any) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 256 or not value.isprintable():
        raise _invalid(name, "must be 1-256 printable characters")
    return value


def _positive_number(name: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise _invalid(name, "must be a finite number > 0")
    return float(value)


def _conids(name: str, value: Any) -> tuple[int, ...]:
    if not isinstance(value, (list, tuple)) or not 1 <= len(value) <= MAX_CONIDS:
        raise _invalid(name, f"must be a list of 1-{MAX_CONIDS} conids")
    if any(type(conid) is not int or conid <= 0 for conid in value):
        raise _invalid(name, "must hold positive JSON integers only")
    if len(set(value)) != len(value):
        raise _invalid(name, "must not repeat a conid")
    return tuple(sorted(value))


def _is_scalar(value: Any) -> bool:
    if isinstance(value, float):
        return math.isfinite(value)
    return value is None or isinstance(value, (str, bool, int))


def _params(name: str, value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _invalid(name, "must be a mapping")
    for key, item in value.items():
        if not isinstance(key, str):
            raise _invalid(name, "keys must be strings")
        items = item if isinstance(item, (list, tuple)) else (item,)
        if not all(_is_scalar(element) for element in items):
            raise _invalid(name, f"{key} must be a finite scalar or a flat list of them")
    return MappingProxyType(copy.deepcopy(dict(value)))


_VALIDATORS: dict[str, Callable[[str, Any], Any]] = {
    "strategy_path": _matching(_PATH),
    "strategy_digest": _matching(_SHA256),
    "class_name": _matching(_CLASS),
    "params": _params,
    "conids": _conids,
    "bar_size": _one_of(frozenset(BarSize.bar_sizes())),
    "style": _one_of(SUPPORTED_STYLES),
    "decider": _matching(_DECIDER),
    "decider_verdict": _one_of(frozenset(VERDICTS)),
    "evidence_ref": _evidence_ref,
    "evidence_order_notional": _positive_number,
}


@dataclass(frozen=True)
class AiDeployment:
    strategy_path: str
    strategy_digest: str
    class_name: str
    params: Mapping[str, Any]
    conids: tuple[int, ...]
    bar_size: str
    style: str
    decider: str
    decider_verdict: str
    evidence_ref: str
    evidence_order_notional: float

    def __post_init__(self):
        # In-process callers bypass the wire model, so every field is checked here.
        for name, validate in _VALIDATORS.items():
            object.__setattr__(self, name, validate(name, getattr(self, name)))

    @classmethod
    def from_json(cls, value: object) -> "AiDeployment":
        if not isinstance(value, dict) or set(value) != set(_VALIDATORS):
            raise DeploymentRefused("DEPLOYMENT_INVALID", f"deployment must have exactly the keys {sorted(_VALIDATORS)}")
        return cls(**value)

    def to_json(self) -> dict:
        record = {field.name: getattr(self, field.name) for field in fields(self)}
        record["params"] = copy.deepcopy(dict(self.params))
        record["conids"] = list(self.conids)
        return record


def deployment_digest(deployment: AiDeployment) -> str:
    return "sha256:" + hashlib.sha256(_DIGEST_DOMAIN + canonical_json_bytes(deployment.to_json())).hexdigest()


class AiDeploymentStore:
    """Insert-only store of both kinds (SP2 Plan 3 ruling 1); there is no update, delete or unseal."""

    def __init__(self, db: Any, now: Callable[[], dt.datetime]):
        self._db = db
        self._now = now

    def register(self, deployment: AiDeployment, *, principal: str, command_id: str) -> tuple[str, bool]:
        if not isinstance(deployment, AiDeployment):
            raise DeploymentRefused("DEPLOYMENT_INVALID", "deployment must be AiDeployment")
        return self._seal(deployment_digest(deployment), deployment.to_json(), STRATEGY_KIND,
                          STRATEGY_DIGEST_PROVENANCE, principal, command_id)

    def register_discretionary(self, deployment: Any, *, principal: str, command_id: str) -> tuple[str, bool]:
        from trader.automation.discretionary_deployment import DiscretionaryDeployment, discretionary_digest
        if not isinstance(deployment, DiscretionaryDeployment):
            raise DeploymentRefused("DEPLOYMENT_INVALID", "deployment must be DiscretionaryDeployment")
        return self._seal(discretionary_digest(deployment), deployment.to_json(), DISCRETIONARY_KIND,
                          OPERATOR_ATTESTED, principal, command_id)

    def _seal(self, digest: str, record: dict, kind: str, provenance: str, principal: str,
              command_id: str) -> tuple[str, bool]:
        record_json = canonical_json_bytes(record).decode("utf-8")
        now = self._now()

        def write(conn) -> bool:
            if conn.execute("SELECT 1 FROM ai_deployments WHERE digest = ?", [digest]).fetchone():
                return False
            conn.execute("INSERT INTO ai_deployments (digest, record_json, principal, command_id, sealed_at, "
                         "strategy_digest_provenance, kind) VALUES (?, ?, ?, ?, ?, ?, ?)",
                         [digest, record_json, principal, command_id, now, provenance, kind])
            return True
        return digest, self._db.transaction(write)

    def get_sealed_any(self, digest: str) -> Any:
        """A strategy ``AiDeployment`` or a ``DiscretionaryDeployment``; the digest is re-checked on every read."""
        from trader.automation.discretionary_deployment import DiscretionaryDeployment, discretionary_digest
        kind, raw = self._row(digest, "kind, record_json")
        parsers = {STRATEGY_KIND: (AiDeployment.from_json, deployment_digest),
                   DISCRETIONARY_KIND: (DiscretionaryDeployment.from_json, discretionary_digest)}
        if kind not in parsers:
            raise DeploymentRefused("DEPLOYMENT_TAMPERED", f"stored kind {kind!r} is unknown")
        parse, digest_of = parsers[kind]
        try:
            deployment = parse(json.loads(raw))
        except Exception:
            raise DeploymentRefused("DEPLOYMENT_TAMPERED", "stored record does not parse") from None
        if not hmac.compare_digest(digest_of(deployment), digest):
            raise DeploymentRefused("DEPLOYMENT_TAMPERED", "stored record does not match its digest")
        return deployment

    def get_sealed(self, digest: str) -> AiDeployment:
        """Strategy deployments only: no SP1 caller can mistake a discretionary one for a strategy."""
        deployment = self.get_sealed_any(digest)
        if not isinstance(deployment, AiDeployment):
            raise DeploymentRefused("DEPLOYMENT_KIND_MISMATCH", "this digest names a discretionary deployment")
        return deployment

    def kind_of(self, digest: str) -> str:
        return self._row(digest, "kind")[0]

    def provenance(self, digest: str) -> str:
        return self._row(digest, "strategy_digest_provenance")[0]

    def _row(self, digest: str, column: str) -> tuple:
        if not isinstance(digest, str) or not _SHA256.match(digest):
            raise DeploymentRefused("DEPLOYMENT_NOT_SEALED", "not a deployment digest")
        row = self._db.execute(f"SELECT {column} FROM ai_deployments WHERE digest = ?", [digest], fetch="one")
        if row is None:
            raise DeploymentRefused("DEPLOYMENT_NOT_SEALED", "no sealed deployment has this digest")
        return row
