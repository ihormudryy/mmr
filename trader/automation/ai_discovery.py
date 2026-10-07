"""``discover_ai_candidates``: the trader-owned Alpaca discovery read (SP2 spec 6.5, Plan 3 rulings 11-13).

Alpaca movers, most-actives and per-symbol news, named explicitly (no registry
default, no fallback, never the IB scanner), each symbol resolved to exactly one
IB conid, and a scope precheck on delayed data. A failed source is reported with
its real coverage and the read is then not complete (spec section 9). The trader
re-checks everything at admission.
"""
from __future__ import annotations

import datetime as dt
import logging
import math
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional

from trader.automation.ai_deployments import DeploymentRefused
from trader.automation.ai_discovery_wire import (
    DiscoverAiCandidatesRequest, DiscoverAiCandidatesResponse, DiscoveryCandidate, DiscoveryCoverage,
    NewsCoverage, ResolutionCoverage, SourceCoverage,
)
from trader.automation.calendar_policy import ET
from trader.automation.discretionary_deployment import DiscretionaryDeployment
from trader.automation.discretionary_scope import effective_dollar_volume_floor, static_scope_refusal
from trader.automation.scope_evidence import ScopeEvidenceUnavailable, SymbolResolution
from trader.data_providers.alpaca.news import news_items
from trader.data_providers.capabilities import Capability
from trader.data_providers.errors import ProviderError, ProviderNotConfigured

logger = logging.getLogger(__name__)

DELAY_MINUTES = 15
MAX_IB_LOOKUPS = 40
LOOKUP_DEADLINE_SECONDS = 30.0
MAX_VOLUME_FETCHES = 20
_SYMBOL = re.compile(r"^[A-Z]{1,5}$")
_UNCACHED = frozenset({"RESOLUTION_BUDGET", "RESOLUTION_FAILED"})


class DiscoveryRefused(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class _MalformedPayload(ProviderError):
    """An Alpaca reply that is not the documented shape: reported as a failed source."""


class SymbolResolver:
    """Ruling 12: exact symbols only, cached per ET session date, bounded per read."""

    def __init__(self, *, contracts: Any, now: Callable[[], dt.datetime], max_lookups: int = MAX_IB_LOOKUPS,
                 deadline_seconds: float = LOOKUP_DEADLINE_SECONDS, clock: Callable[[], float] = time.monotonic):
        self._contracts = contracts
        self._now = now
        self._max_lookups = max_lookups
        self._deadline_seconds = deadline_seconds
        self._clock = clock
        self._cache: dict[str, SymbolResolution] = {}
        self._cache_date: Optional[dt.date] = None
        self._lock = threading.Lock()

    def resolve_all(self, symbols: Iterable[str]) -> dict[str, SymbolResolution]:
        self._start_session(self._now().astimezone(ET).date())
        started, lookups = self._clock(), 0
        resolved: dict[str, SymbolResolution] = {}
        for symbol in symbols:
            found = self._cached(symbol)
            if found is None and not _SYMBOL.fullmatch(symbol):
                found = SymbolResolution(symbol, "SYMBOL_FORM_UNSUPPORTED", None, None,
                                         "only ^[A-Z]{1,5}$ is looked up")
            if found is None and (lookups >= self._max_lookups
                                  or self._clock() - started >= self._deadline_seconds):
                found = SymbolResolution(symbol, "RESOLUTION_BUDGET", None, None,
                                         "lookup budget for this read used up")
            if found is None:
                lookups += 1
                found = self._look_up(symbol)
            if found.status not in _UNCACHED:
                with self._lock:
                    self._cache[symbol] = found
            resolved[symbol] = found
        return resolved

    def _start_session(self, session_date: dt.date) -> None:
        with self._lock:
            if self._cache_date != session_date:
                self._cache.clear()
                self._cache_date = session_date

    def _cached(self, symbol: str) -> Optional[SymbolResolution]:
        with self._lock:
            return self._cache.get(symbol)

    def _look_up(self, symbol: str) -> SymbolResolution:
        try:
            return self._contracts.by_symbol(symbol)
        except ScopeEvidenceUnavailable as ex:
            return SymbolResolution(symbol, "RESOLUTION_FAILED", None, None, ex.reason)


@dataclass
class _Row:
    symbol: str
    origins: list[str] = field(default_factory=list)
    price: Optional[float] = None
    change_pct: Optional[float] = None
    volume: Optional[float] = None
    as_of: Optional[str] = None


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _entries(payload: Any, key: str, numbers: tuple[str, ...]) -> list[dict]:
    """Every row of ``payload[key]``, or ``_MalformedPayload``: a missing list or a bad row is never dropped
    silently, because that would shrink the candidate universe while reporting the source as complete."""
    if not isinstance(payload, dict):
        raise _MalformedPayload("the reply is not an object")
    entries = payload.get(key)
    if not isinstance(entries, list):
        raise _MalformedPayload(f"{key} is missing or not a list")
    for entry in entries:
        if (not isinstance(entry, dict) or not isinstance(entry.get("symbol"), str) or not entry["symbol"].strip()
                or not all(_is_number(entry.get(name)) for name in numbers)):
            raise _MalformedPayload(f"a {key} row has no symbol or a field of {list(numbers)} is not a number")
    return entries


def _articles(payload: Any) -> list[dict]:
    if not isinstance(payload, dict) or not isinstance(payload.get("news"), list):
        raise _MalformedPayload("news is missing or not a list")
    if not all(_usable_article(article) for article in payload["news"]):
        raise _MalformedPayload("a news article has no id, headline, source or time with an offset")
    return payload["news"]


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _usable_article(article: Any) -> bool:
    """Alpaca always sends id, headline, source and created_at; url and summary may be empty or absent."""
    if not isinstance(article, dict):
        return False
    article_id = article.get("id")
    has_id = _text(article_id) or (type(article_id) is int and article_id > 0)
    optional_text = all(article.get(name) is None or isinstance(article.get(name), str) for name in ("url", "summary"))
    return (has_id and _text(article.get("headline")) and _text(article.get("source"))
            and _aware_time(article.get("created_at")) and optional_text)


def _aware_time(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).utcoffset() is not None
    except ValueError:
        return False


def _as_of(payload: dict) -> Optional[str]:
    value = payload.get("last_updated")
    return value if isinstance(value, str) else None


class AiDiscoveryReader:
    def __init__(self, *, providers: Callable[[Capability], Any], resolver: SymbolResolver, volumes: Any,
                 deployments: Any, filter_refusal: Callable[[Any, float], Optional[str]],
                 now: Callable[[], dt.datetime], volume_budget: int = MAX_VOLUME_FETCHES):
        self._providers = providers
        self._resolver = resolver
        self._volumes = volumes
        self._deployments = deployments
        self._filter_refusal = filter_refusal
        self._now = now
        self._volume_budget = volume_budget

    def read(self, request: DiscoverAiCandidatesRequest) -> DiscoverAiCandidatesResponse:
        rule = self._rule(request.deployment_digest)
        movers_provider = self._provider(Capability.MOVERS)
        rows: dict[str, _Row] = {}
        movers = self._source("movers", 2 * request.movers_top,
                              lambda: movers_provider.screener(request.movers_top),
                              lambda payload: self._add_movers(rows, payload))
        actives = self._source("most_actives", request.most_actives_top,
                               lambda: movers_provider.most_actives(request.most_actives_top),
                               lambda payload: self._add_actives(rows, payload))
        for symbol in request.watchlist:
            self._row(rows, symbol, "watchlist")
        watchlist = SourceCoverage(requested=len(request.watchlist), returned=len(request.watchlist),
                                   failed=False, error_code=None, as_of=None)
        resolutions = self._resolver.resolve_all(list(rows))
        budget = [self._volume_budget]
        candidates = [self._candidate(row, resolutions[row.symbol], rule, budget) for row in rows.values()]
        news = self._news(candidates, request)
        resolution = _resolution_coverage(resolutions.values())
        budget_hit = any(r.status == "RESOLUTION_BUDGET" for r in resolutions.values())
        volume_unchecked = any(c["scope_precheck"]["status"] == "NOT_CHECKED"
                               and c["scope_precheck"]["part"] == "dollar_volume" for c in candidates)
        complete = not (movers.failed or actives.failed or news.failed_symbols or resolution.failed or budget_hit
                        or volume_unchecked)
        return DiscoverAiCandidatesResponse(
            read_at=self._now().isoformat(), source="alpaca", delayed=True, delay_minutes=DELAY_MINUTES,
            deployment_digest=request.deployment_digest,
            coverage=DiscoveryCoverage(movers=movers, most_actives=actives, watchlist=watchlist, news=news,
                                       resolution=resolution, complete=complete),
            candidates=[DiscoveryCandidate(**c) for c in candidates])

    # -- sources -----------------------------------------------------------------

    def _rule(self, digest: str) -> Any:
        try:
            deployment = self._deployments.get_sealed_any(digest)
        except DeploymentRefused as ex:
            raise DiscoveryRefused(ex.code, ex.message) from None
        if not isinstance(deployment, DiscretionaryDeployment):
            raise DiscoveryRefused("DEPLOYMENT_KIND_MISMATCH", "discovery needs a discretionary deployment")
        return deployment.scope_rule

    def _provider(self, capability: Capability) -> Any:
        try:
            return self._providers(capability)
        except ProviderNotConfigured:
            raise DiscoveryRefused("DISCOVERY_SOURCE_UNAVAILABLE",
                                   "Alpaca keys are not configured in the trader") from None

    @staticmethod
    def _source(name: str, requested: int, fetch: Callable[[], Any],
                add: Callable[[Any], int]) -> SourceCoverage:
        try:
            payload = fetch()
            returned = add(payload)
        except ProviderError as ex:
            # Spec 9: a failed source is recorded with real coverage, never presented as complete.
            logger.error("discover_ai_candidates: %s failed: %s", name, type(ex).__name__)
            return SourceCoverage(requested=requested, returned=0, failed=True,
                                  error_code=type(ex).__name__ if not isinstance(ex, _MalformedPayload)
                                  else "MALFORMED_PAYLOAD", as_of=None)
        return SourceCoverage(requested=requested, returned=returned, failed=False, error_code=None,
                              as_of=_as_of(payload))

    @staticmethod
    def _row(rows: dict[str, _Row], symbol: str, origin: str) -> _Row:
        row = rows.setdefault(symbol, _Row(symbol))
        if origin not in row.origins:
            row.origins.append(origin)
        return row

    def _add_movers(self, rows: dict[str, _Row], payload: Any) -> int:
        numbers = ("price", "percent_change")
        added = [(origin, entry) for key, origin in (("gainers", "gainer"), ("losers", "loser"))
                 for entry in _entries(payload, key, numbers)]
        for origin, entry in added:
            row = self._row(rows, entry["symbol"], origin)
            row.price = row.price if row.price is not None else float(entry["price"])
            row.change_pct = row.change_pct if row.change_pct is not None else float(entry["percent_change"])
            row.as_of = row.as_of or _as_of(payload)
        return len(added)

    def _add_actives(self, rows: dict[str, _Row], payload: Any) -> int:
        added = _entries(payload, "most_actives", ("volume",))
        for entry in added:
            row = self._row(rows, entry["symbol"], "most_active")
            row.volume = row.volume if row.volume is not None else float(entry["volume"])
            row.as_of = row.as_of or _as_of(payload)
        return len(added)

    # -- candidates ------------------------------------------------------------------

    def _candidate(self, row: _Row, resolution: SymbolResolution, rule: Any, budget: list[int]) -> dict:
        contract = resolution.contract
        return {"symbol": row.symbol, "origins": list(row.origins), "conid": resolution.conid,
                "resolution": resolution.status,
                "primary_exchange": None if contract is None else contract.primary_exchange,
                "stock_type": None if contract is None else contract.stock_type,
                "price": row.price, "change_pct": row.change_pct, "volume": row.volume,
                "source_timestamp": row.as_of, "delayed": True,
                "scope_precheck": self._precheck(row, resolution, rule, budget),
                "news_status": "SKIPPED", "news": []}

    def _precheck(self, row: _Row, resolution: SymbolResolution, rule: Any, budget: list[int]) -> dict:
        """Ruling 13: every part except liquidity, on delayed data. Admission re-checks all of it."""
        def result(status: str, part: Optional[str], reason: str, median: Optional[float] = None) -> dict:
            return {"status": status, "part": part, "reason": reason, "median_dollar_volume_20d": median}

        if resolution.status != "RESOLVED" or resolution.contract is None:
            return result("NOT_CHECKED", None, f"symbol {resolution.status.lower()}")
        static = static_scope_refusal(rule, resolution.contract)
        if static is not None:
            return result("FAIL", *static)
        if row.price is not None and row.price < rule.min_price:
            return result("FAIL", "price", f"delayed price {row.price} is below {rule.min_price}")
        volume = self._volumes.cached(resolution.conid)
        if volume is None and budget[0] > 0:
            budget[0] -= 1
            try:
                volume = self._volumes.twenty_sessions(resolution.conid, resolution.contract.symbol)
            except ScopeEvidenceUnavailable as ex:
                return result("NOT_CHECKED", "dollar_volume", ex.reason)
        if volume is None:
            return result("NOT_CHECKED", "dollar_volume", "volume fetch budget for this read used up")
        median, floor = volume.median_dollar_volume, effective_dollar_volume_floor(rule)
        if median < floor:
            return result("FAIL", "dollar_volume", f"median {median:,.0f} is below {floor:,.0f}", median)
        try:
            denied = self._filter_refusal(resolution.contract, row.price or 0.0)
        except Exception as ex:
            denied = f"trading filter unavailable: {type(ex).__name__}"
        if denied:
            return result("FAIL", "trading_filter", denied, median)
        if row.price is None:
            return result("NOT_CHECKED", "price", "the discovery source gave no price", median)
        return result("PASS", None, "in scope on delayed data; liquidity is checked at admission", median)

    def _news(self, candidates: list[dict], request: DiscoverAiCandidatesRequest) -> NewsCoverage:
        chosen = [] if request.news_per_symbol == 0 else [
            c for c in candidates if c["conid"] is not None and c["scope_precheck"]["status"] != "FAIL"
        ][:request.news_symbols_max]
        if not chosen:
            return NewsCoverage(requested_symbols=0, returned_symbols=0, failed_symbols=[])
        provider = self._provider(Capability.NEWS)
        failed: list[str] = []
        for candidate in chosen:
            try:
                articles = _articles(provider.news_payload(candidate["symbol"], request.news_per_symbol))
                items = news_items(articles, request.news_per_symbol)
                candidate["news"] = [_news_item(item) for item in items]
                candidate["news_status"] = "OK"
            except Exception as ex:      # noqa: BLE001 - one symbol's bad reply is its own failure, never the read's
                logger.error("discover_ai_candidates: news for %s failed: %s", candidate["symbol"],
                             type(ex).__name__)
                candidate["news"], candidate["news_status"] = [], "FAILED"
                failed.append(candidate["symbol"])
        return NewsCoverage(requested_symbols=len(chosen), returned_symbols=len(chosen) - len(failed),
                            failed_symbols=failed)


def _news_item(article: dict) -> dict:
    return {name: str(article.get(name) or "") for name in ("id", "published", "title", "summary", "url", "source")}


def _resolution_coverage(resolutions: Iterable[SymbolResolution]) -> ResolutionCoverage:
    statuses = [r.status for r in resolutions]
    resolved = statuses.count("RESOLVED")
    failed = statuses.count("RESOLUTION_FAILED")
    return ResolutionCoverage(requested=len(statuses), resolved=resolved, failed=failed,
                              unresolved=len(statuses) - resolved - failed)
