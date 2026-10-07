"""Strict wire models of ``discover_ai_candidates`` and ``get_ai_entry_quote`` (SP2 Plan 3; Plans 5 and 6 use them).

Every model is ``extra="forbid", strict=True``: a bool is never an int and no unknown key passes.
"""
from __future__ import annotations

from typing import Annotated, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

Origin = Literal["gainer", "loser", "most_active", "watchlist"]
Resolution = Literal["RESOLVED", "NOT_FOUND", "AMBIGUOUS", "SYMBOL_FORM_UNSUPPORTED",
                     "RESOLUTION_BUDGET", "RESOLUTION_FAILED"]
ScopePart = Literal["exchange", "instrument_type", "price", "dollar_volume", "liquidity",
                    "trading_filter", "evidence_stale"]

_STRICT = ConfigDict(extra="forbid", strict=True)
WatchlistSymbol = Annotated[str, Field(pattern=r"^[A-Z]{1,5}$")]


class DiscoverAiCandidatesRequest(BaseModel):
    model_config = _STRICT

    deployment_digest: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
    movers_top: Annotated[int, Field(ge=1, le=50)]          # gainers and losers each
    most_actives_top: Annotated[int, Field(ge=1, le=100)]
    watchlist: Annotated[list[WatchlistSymbol], Field(max_length=25)]
    news_per_symbol: Annotated[int, Field(ge=0, le=10)]
    news_symbols_max: Annotated[int, Field(ge=0, le=30)]


class SourceCoverage(BaseModel):
    model_config = _STRICT

    requested: int
    returned: int
    failed: bool
    error_code: Optional[str]
    as_of: Optional[str]


class NewsCoverage(BaseModel):
    model_config = _STRICT

    requested_symbols: int
    returned_symbols: int
    failed_symbols: list[str]


class ResolutionCoverage(BaseModel):
    model_config = _STRICT

    requested: int
    resolved: int
    unresolved: int
    failed: int


class DiscoveryCoverage(BaseModel):
    model_config = _STRICT

    movers: SourceCoverage
    most_actives: SourceCoverage
    watchlist: SourceCoverage
    news: NewsCoverage
    resolution: ResolutionCoverage
    complete: bool


class ScopePrecheck(BaseModel):
    model_config = _STRICT

    status: Literal["PASS", "FAIL", "NOT_CHECKED"]
    part: Optional[ScopePart]
    reason: str
    median_dollar_volume_20d: Optional[float]


class DiscoveryNewsItem(BaseModel):
    model_config = _STRICT

    id: str
    published: str
    title: str
    summary: str
    url: str
    source: str


class DiscoveryCandidate(BaseModel):
    model_config = _STRICT

    symbol: str
    origins: list[Origin]
    conid: Optional[int]
    resolution: Resolution
    primary_exchange: Optional[str]
    stock_type: Optional[str]
    price: Optional[float]
    change_pct: Optional[float]
    volume: Optional[float]
    source_timestamp: Optional[str]
    delayed: bool
    scope_precheck: ScopePrecheck
    news_status: Literal["OK", "FAILED", "SKIPPED"]
    news: list[DiscoveryNewsItem]


class DiscoverAiCandidatesResponse(BaseModel):
    model_config = _STRICT

    read_at: str
    source: Literal["alpaca"]
    delayed: bool
    delay_minutes: int
    deployment_digest: str
    coverage: DiscoveryCoverage
    candidates: list[DiscoveryCandidate]
