"""The ai_paper World with a discretionary deployment and a real scope service (SP2 Plan 3 Tasks 7-8).

Only IB contract details and the 20-session volume are fakes; the quotes are the World's own, so the
scope check, ``AiPaperEvidence`` and the dispatch guard read the same quote.
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Optional

from tests.automation.ai_paper_world import World
from trader.automation.ai_paper_filter import MtimeCachedFilterLoader
from trader.automation.discretionary_deployment import DEFAULT_SCOPE_RULE, DiscretionaryDeployment
from trader.automation.discretionary_scope import (
    DiscretionaryScopeService, ScopeCheckStore, apply_scope_check_migration, trading_filter_refusal,
)
from trader.automation.production_evidence import TwentySessionVolume, latest_closed_sessions
from trader.automation.scope_evidence import ContractEvidence, ScopeEvidenceUnavailable
from trader.data.schema_migrations import SchemaMigrator

DEFAULT_VOLUME = 1_000_000.0           # shares per session at a close of 100.0: a $100M median


class FakeContracts:
    """IB contract details by conid; ``set`` changes the fields, ``fail_with`` makes every read fail."""

    def __init__(self, clock):
        self.clock = clock
        self.fields: dict[int, dict] = {}
        self.failure: Optional[str] = None
        self.calls = 0

    def set(self, conid: int, **fields) -> None:
        self.fields.setdefault(conid, {}).update(fields)

    def fail_with(self, reason: str) -> None:
        self.failure = reason

    def by_conid(self, conid: int) -> ContractEvidence:
        self.calls += 1
        if self.failure is not None:
            raise ScopeEvidenceUnavailable(self.failure)
        base = ContractEvidence(conid=conid, symbol="AAPL", sec_type="STK", currency="USD",
                                primary_exchange="NASDAQ", stock_type="COMMON", fetched_at=self.clock())
        return replace(base, **self.fields.get(conid, {}))


class FakeVolumes:
    """Twenty current sessions at a close of 100.0."""

    def __init__(self, clock):
        self.clock = clock
        self.volume = DEFAULT_VOLUME
        self.failure: Optional[str] = None
        self.calls = 0

    def set(self, *, volume: float) -> None:
        self.volume = volume

    def fail_with(self, reason: str) -> None:
        self.failure = reason

    def twenty_sessions(self, conid: int, symbol: str) -> TwentySessionVolume:
        self.calls += 1
        if self.failure is not None:
            raise ScopeEvidenceUnavailable(self.failure)
        return TwentySessionVolume(conid=conid, sessions=latest_closed_sessions(self.clock()),
                                   closes=(100.0,) * 20, volumes=(self.volume,) * 20, source="local_daily_bars")

    def cached(self, conid: int) -> Optional[TwentySessionVolume]:
        return None


def discretionary_body(rule: Optional[dict] = None) -> dict:
    return {"kind": "discretionary", "style": "intraday_long",
            "scope_rule": {**DEFAULT_SCOPE_RULE.to_json(), **(rule or {})},
            "attestation": {"operator": "owner", "statement": "paper only; rule as sealed",
                            "attested_at": "2026-07-17T10:00:00-04:00"}}


def discretionary_world(tmp_path: Path, *, rule: Optional[dict] = None, **world_kwargs) -> World:
    world = World(tmp_path, **world_kwargs)
    apply_scope_check_migration(SchemaMigrator(world.db))
    world.ddigest, _ = world.deployments.register_discretionary(
        DiscretionaryDeployment.from_json(discretionary_body(rule)), principal="cli", command_id="aidep-world")
    world.contracts = FakeContracts(world.clock)
    world.volumes = FakeVolumes(world.clock)
    world.scope_filter = trading_filter_refusal(MtimeCachedFilterLoader(str(world.filter_file.path)))
    world.scope = DiscretionaryScopeService(
        contracts=world.contracts, volumes=world.volumes, quotes=world.quotes,
        accepted_feeds=world.accepted_feeds, filter_refusal=world.scope_filter,
        checks=ScopeCheckStore(world.db, now=world.clock), now=world.clock)
    world.service.attach_scope(world.scope)
    return world

