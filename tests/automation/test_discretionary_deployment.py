"""SP2 Plan 3 Task 3: the operator's discretionary deployment and its sealed storage."""
from __future__ import annotations

import datetime as dt

import pytest

from tests.automation.test_ai_deployments import GOOD
from trader.automation.ai_deployments import (
    AiDeployment, AiDeploymentStore, DeploymentRefused, apply_ai_deployment_migration,
)
from trader.automation.discretionary_deployment import (
    DEFAULT_SCOPE_RULE, DiscretionaryDeployment, discretionary_digest,
)
from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator

NOW = dt.datetime(2026, 7, 17, 15, 0, tzinfo=dt.timezone.utc)
BODY = {"kind": "discretionary", "style": "intraday_long", "scope_rule": DEFAULT_SCOPE_RULE.to_json(),
        "attestation": {"operator": "owner", "statement": "paper only; rule as sealed",
                        "attested_at": "2026-07-17T10:00:00-04:00"}}


@pytest.fixture
def store(tmp_path):
    db = DuckDBConnection.get_instance(str(tmp_path / "journal.duckdb"))
    apply_ai_deployment_migration(SchemaMigrator(db))
    return AiDeploymentStore(db, now=lambda: NOW)


def test_round_trip_and_stable_digest():
    dep = DiscretionaryDeployment.from_json(BODY)
    reordered = {**BODY, "scope_rule": {**BODY["scope_rule"], "primary_exchanges": ["NYSE", "ARCA", "NASDAQ"]}}
    assert dep.to_json() == DiscretionaryDeployment.from_json(reordered).to_json()
    assert discretionary_digest(dep) == discretionary_digest(DiscretionaryDeployment.from_json(reordered))


@pytest.mark.parametrize("rule", [
    {"primary_exchanges": ["NYSE", "AMEX"]}, {"primary_exchanges": ["PINK"]}, {"primary_exchanges": []},
    {"stock_types": ["COMMON", "WARRANT"]}, {"stock_types": ["ADR"]}, {"stock_types": ["ETF", "ETF"]},
    {"min_price": 4.99}, {"min_price": True}, {"min_median_dollar_volume": 19_999_999.0},
    {"max_order_share_of_dollar_volume": 0.011}, {"max_order_share_of_dollar_volume": 0.0},
    {"min_price": float("nan")}])
def test_the_rule_can_only_narrow(rule):
    with pytest.raises(DeploymentRefused) as exc:
        DiscretionaryDeployment.from_json({**BODY, "scope_rule": {**BODY["scope_rule"], **rule}})
    assert exc.value.code == "DEPLOYMENT_INVALID"


@pytest.mark.parametrize("body", [
    {**BODY, "extra": 1}, {k: v for k, v in BODY.items() if k != "attestation"}, {**BODY, "kind": "strategy"},
    {**BODY, "style": "swing_long"}, {**BODY, "attestation": {**BODY["attestation"], "attested_at": "2026-07-17"}},
    {**BODY, "attestation": {**BODY["attestation"], "statement": ""}}])
def test_shape_is_exact(body):
    with pytest.raises(DeploymentRefused):
        DiscretionaryDeployment.from_json(body)


def test_a_narrower_rule_is_accepted():
    dep = DiscretionaryDeployment.from_json({**BODY, "scope_rule": {**BODY["scope_rule"], "stock_types": ["ETF"],
                                                                   "min_price": 10.0}})
    assert dep.scope_rule.stock_types == ("ETF",) and dep.scope_rule.min_price == 10.0


def test_store_seals_both_kinds_apart(store):
    dep = DiscretionaryDeployment.from_json(BODY)
    digest, created = store.register_discretionary(dep, principal="cli", command_id="aidep-1")
    assert created and store.register_discretionary(dep, principal="cli", command_id="aidep-1") == (digest, False)
    assert store.get_sealed_any(digest) == dep and store.kind_of(digest) == "discretionary"
    assert store.provenance(digest) == "OPERATOR_ATTESTED"
    with pytest.raises(DeploymentRefused) as exc:
        store.get_sealed(digest)
    assert exc.value.code == "DEPLOYMENT_KIND_MISMATCH"
    strategy_digest, _ = store.register(AiDeployment.from_json(GOOD), principal="ai_research", command_id="s-1")
    assert store.kind_of(strategy_digest) == "strategy" and store.get_sealed(strategy_digest).conids


def test_a_tampered_discretionary_row_is_refused(store):
    digest, _ = store.register_discretionary(DiscretionaryDeployment.from_json(BODY), principal="cli",
                                             command_id="aidep-1")
    store._db.execute("UPDATE ai_deployments SET record_json = replace(record_json, '\"owner\"', '\"other\"') "
                      "WHERE digest = ?", [digest])
    with pytest.raises(DeploymentRefused) as exc:
        store.get_sealed_any(digest)
    assert exc.value.code == "DEPLOYMENT_TAMPERED"


def test_research_cannot_send_a_discretionary_body_as_a_strategy():
    with pytest.raises(DeploymentRefused) as exc:
        AiDeployment.from_json(BODY)
    assert exc.value.code == "DEPLOYMENT_INVALID"
