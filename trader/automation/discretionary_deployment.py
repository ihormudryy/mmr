"""The operator's discretionary deployment (SP2 spec 6.6): a sealed scope rule plus an attestation.

The rule may only narrow the spec default (Plan 3 ruling 2). Sealing and reads live in
``AiDeploymentStore``; this module owns the record, its checks and its digest.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import math
import re
from dataclasses import dataclass
from typing import Any, Callable

from trader.automation.ai_deployments import DISCRETIONARY_KIND, DeploymentRefused
from trader.automation.ai_paper_config import SUPPORTED_STYLES
from trader.research.canonical import canonical_json_bytes

ALLOWED_EXCHANGES = frozenset({"NYSE", "NASDAQ", "ARCA"})
ALLOWED_STOCK_TYPES = frozenset({"COMMON", "ETF"})
PRICE_FLOOR = 5.0
DOLLAR_VOLUME_FLOOR = 20_000_000.0
ORDER_SHARE_CEILING = 0.01
VOLUME_SESSIONS = 20
_DIGEST_DOMAIN = b"mmr.ai-discretionary-deployment.v1\x00"
_OPERATOR = re.compile(r"^[A-Za-z0-9_.@-]{1,64}$")
_KEYS = frozenset({"kind", "style", "scope_rule", "attestation"})
_ATTESTATION_KEYS = frozenset({"operator", "statement", "attested_at"})


def _invalid(name: str, rule: str) -> DeploymentRefused:
    return DeploymentRefused("DEPLOYMENT_INVALID", f"{name} {rule}")


def _exact_keys(name: str, value: Any, keys: frozenset) -> dict:
    if not isinstance(value, dict) or set(value) != keys:
        raise _invalid(name, f"must have exactly the keys {sorted(keys)}")
    return value


def _subset(name: str, value: Any, allowed: frozenset) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not value or not all(isinstance(v, str) for v in value):
        raise _invalid(name, "must be a non-empty list of strings")
    if len(set(value)) != len(value) or not set(value) <= allowed:
        raise _invalid(name, f"must name distinct values from {sorted(allowed)}")
    return tuple(sorted(value))


def _bounded(name: str, value: Any, *, low: float, high: float = math.inf, open_low: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise _invalid(name, "must be a finite number")
    if (value <= low if open_low else value < low) or value > high:
        raise _invalid(name, f"must be in {'(' if open_low else '['}{low}, {high}]")
    return float(value)


@dataclass(frozen=True)
class DiscretionaryScopeRule:
    primary_exchanges: tuple[str, ...]
    stock_types: tuple[str, ...]
    min_price: float
    min_median_dollar_volume: float
    max_order_share_of_dollar_volume: float

    def __post_init__(self):
        checks: dict[str, Callable[[str, Any], Any]] = {
            "primary_exchanges": lambda n, v: _subset(n, v, ALLOWED_EXCHANGES),
            "stock_types": lambda n, v: _subset(n, v, ALLOWED_STOCK_TYPES),
            "min_price": lambda n, v: _bounded(n, v, low=PRICE_FLOOR),
            "min_median_dollar_volume": lambda n, v: _bounded(n, v, low=DOLLAR_VOLUME_FLOOR),
            "max_order_share_of_dollar_volume": lambda n, v: _bounded(n, v, low=0.0, high=ORDER_SHARE_CEILING,
                                                                      open_low=True),
        }
        for name, check in checks.items():
            object.__setattr__(self, name, check(name, getattr(self, name)))

    @classmethod
    def from_json(cls, value: Any) -> "DiscretionaryScopeRule":
        return cls(**_exact_keys("scope_rule", value, frozenset(cls.__dataclass_fields__)))

    def to_json(self) -> dict:
        return {"primary_exchanges": list(self.primary_exchanges), "stock_types": list(self.stock_types),
                "min_price": self.min_price, "min_median_dollar_volume": self.min_median_dollar_volume,
                "max_order_share_of_dollar_volume": self.max_order_share_of_dollar_volume}


DEFAULT_SCOPE_RULE = DiscretionaryScopeRule(("ARCA", "NASDAQ", "NYSE"), ("COMMON", "ETF"), PRICE_FLOOR,
                                            DOLLAR_VOLUME_FLOOR, ORDER_SHARE_CEILING)


@dataclass(frozen=True)
class OperatorAttestation:
    operator: str
    statement: str
    attested_at: str

    def __post_init__(self):
        if not isinstance(self.operator, str) or not _OPERATOR.match(self.operator):
            raise _invalid("attestation.operator", f"must match {_OPERATOR.pattern}")
        if (not isinstance(self.statement, str) or not 1 <= len(self.statement) <= 500
                or not self.statement.isprintable()):
            raise _invalid("attestation.statement", "must be 1-500 printable characters")
        if not _is_aware_iso(self.attested_at):
            raise _invalid("attestation.attested_at", "must be ISO-8601 with a UTC offset")

    def to_json(self) -> dict:
        return {"operator": self.operator, "statement": self.statement, "attested_at": self.attested_at}


def _is_aware_iso(value: Any) -> bool:
    try:
        return dt.datetime.fromisoformat(value).utcoffset() is not None
    except (TypeError, ValueError):
        return False


@dataclass(frozen=True)
class DiscretionaryDeployment:
    style: str
    scope_rule: DiscretionaryScopeRule
    attestation: OperatorAttestation
    kind: str = DISCRETIONARY_KIND

    def __post_init__(self):
        if self.kind != DISCRETIONARY_KIND:
            raise _invalid("kind", f"must be {DISCRETIONARY_KIND!r}")
        if self.style not in SUPPORTED_STYLES:
            raise _invalid("style", f"must be one of {sorted(SUPPORTED_STYLES)}")
        if not isinstance(self.scope_rule, DiscretionaryScopeRule) or not isinstance(self.attestation,
                                                                                     OperatorAttestation):
            raise _invalid("deployment", "needs a scope rule and an attestation")

    @classmethod
    def from_json(cls, value: Any) -> "DiscretionaryDeployment":
        body = _exact_keys("deployment", value, _KEYS)
        attestation = _exact_keys("attestation", body["attestation"], _ATTESTATION_KEYS)
        return cls(style=body["style"], scope_rule=DiscretionaryScopeRule.from_json(body["scope_rule"]),
                   attestation=OperatorAttestation(**attestation), kind=body["kind"])

    def to_json(self) -> dict:
        return {"kind": self.kind, "style": self.style, "scope_rule": self.scope_rule.to_json(),
                "attestation": self.attestation.to_json()}


def discretionary_digest(deployment: DiscretionaryDeployment) -> str:
    return "sha256:" + hashlib.sha256(_DIGEST_DOMAIN + canonical_json_bytes(deployment.to_json())).hexdigest()
