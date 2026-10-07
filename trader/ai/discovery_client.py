"""The ai side of trader-owned discovery (SP2 spec 6.5, 9, 10; Plan 6 Ruling 7).

Only RESOLVED candidates whose scope precheck PASSed reach a model; everything else is counted. Coverage is
recorded as it really was: the client may lower the trader's `complete` flag, never raise it.
"""
from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from pydantic import ValidationError

from trader.ai.config import DiscoverySettings
from trader.ai.ids import canonical_json
from trader.ai.tools import ToolUnavailable
from trader.automation.ai_discovery_wire import DiscoverAiCandidatesResponse, DiscoveryCandidate

logger = logging.getLogger(__name__)
PARTIAL_RESOLUTIONS = frozenset({"RESOLUTION_BUDGET", "RESOLUTION_FAILED"})


@dataclass(frozen=True)
class NewsLine:
    published: str
    title: str
    summary: str
    source: str


@dataclass(frozen=True)
class EligibleCandidate:
    ref: str
    symbol: str
    conid: int
    origins: tuple[str, ...]
    price: Optional[float]
    change_pct: Optional[float]
    volume: Optional[float]
    median_dollar_volume: Optional[float]
    source_timestamp: Optional[str]
    news: tuple[NewsLine, ...]

    @classmethod
    def from_wire(cls, ref: str, candidate: DiscoveryCandidate) -> "EligibleCandidate":
        """The URL is not kept: it is never shown to a model."""
        news = tuple(NewsLine(n.published, n.title, n.summary, n.source) for n in candidate.news)
        return cls(ref, candidate.symbol, candidate.conid, tuple(candidate.origins), candidate.price,
                   candidate.change_pct, candidate.volume, candidate.scope_precheck.median_dollar_volume_20d,
                   candidate.source_timestamp, news)


@dataclass(frozen=True)
class DiscoveryRead:
    cycle_id: str
    ok: bool
    error_code: Optional[str]
    complete: bool
    read_at: Optional[str]
    coverage: Optional[Mapping[str, Any]]
    eligible: tuple[EligibleCandidate, ...]
    dropped: Mapping[str, int]
    seen: int


def request_body(settings: DiscoverySettings, deployment_digest: str) -> dict:
    return {"deployment_digest": deployment_digest, "movers_top": settings.movers_top,
            "most_actives_top": settings.most_actives_top, "watchlist": list(settings.watchlist),
            "news_per_symbol": settings.news_per_symbol, "news_symbols_max": settings.news_symbols_max}


def really_complete(response: DiscoverAiCandidatesResponse) -> bool:
    """Ruling 7: the trader's flag AND no visible failure. The client can only lower it, never raise it."""
    coverage = response.coverage
    failed = (coverage.movers.failed or coverage.most_actives.failed or coverage.watchlist.failed
              or bool(coverage.news.failed_symbols) or coverage.resolution.failed > 0
              or any(c.resolution in PARTIAL_RESOLUTIONS for c in response.candidates))
    return coverage.complete and not failed


def drop_reason(candidate: DiscoveryCandidate) -> Optional[str]:
    if candidate.conid is None or candidate.resolution != "RESOLVED":
        return "CONID_MISSING"
    if candidate.scope_precheck.status == "FAIL":
        return f"SCOPE_{candidate.scope_precheck.part}"
    if candidate.scope_precheck.status != "PASS":
        return "SCOPE_NOT_CHECKED"
    return None


class DiscoveryClient:
    def __init__(self, *, store: Any, settings: DiscoverySettings, deployment_digest: str, clock: Any):
        self._store, self._settings, self._digest, self._clock = store, settings, deployment_digest, clock

    async def read(self, tools: Any, cycle_id: str) -> DiscoveryRead:
        try:
            reply = await tools.read("discovery", request_body(self._settings, self._digest))
            response = DiscoverAiCandidatesResponse.model_validate_json(canonical_json(reply))
        except ToolUnavailable as exc:
            return await self._failed(cycle_id, exc.code)
        except (ValidationError, TypeError, ValueError):
            return await self._failed(cycle_id, "DISCOVERY_REPLY_INVALID")
        if response.deployment_digest != self._digest:
            return await self._failed(cycle_id, "DISCOVERY_DEPLOYMENT_MISMATCH")
        dropped: Counter[str] = Counter()
        eligible: list[EligibleCandidate] = []
        for candidate in response.candidates:
            reason = drop_reason(candidate)
            if reason is None and len(eligible) >= self._settings.max_candidates_to_model:
                reason = "OVER_LIMIT"
            if reason is not None:
                dropped[reason] += 1
                continue
            eligible.append(EligibleCandidate.from_wire(f"C{len(eligible) + 1}", candidate))
        read = DiscoveryRead(cycle_id=cycle_id, ok=True, error_code=None, complete=really_complete(response),
                             read_at=response.read_at, coverage=response.coverage.model_dump(mode="json"),
                             eligible=tuple(eligible), dropped=dict(dropped), seen=len(response.candidates))
        await self._record(read)
        return read

    async def _failed(self, cycle_id: str, code: str) -> DiscoveryRead:
        logger.error("discovery read for %s failed: %s (no candidates this cycle)", cycle_id, code)
        read = DiscoveryRead(cycle_id, False, code, False, None, None, (), {}, 0)
        await self._record(read)
        return read

    async def _record(self, read: DiscoveryRead) -> None:
        now = self._clock.now()
        row = [read.cycle_id, "OK" if read.ok else "FAILED", read.error_code, read.read_at, read.complete,
               None if read.coverage is None else canonical_json(read.coverage), read.seen, len(read.eligible),
               canonical_json(dict(read.dropped)), now]
        await self._store.atransaction(lambda conn: conn.execute(
            "INSERT INTO ai_discovery_reads (cycle_id, status, error_code, read_at, complete, coverage_json, seen, "
            "eligible, dropped_json, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (cycle_id) DO NOTHING", row))
