"""Authenticated, validated HTTP routes for dashboard research tools."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from trader.tools.idea_scanner import PRESETS
from web.command_center.research import ResearchError, ResearchService


UniverseLoader = Callable[[str], list[str]]


def _load_universe_symbols(name: str) -> list[str]:
    from trader.container import Container
    from trader.data.universe import UniverseAccessor

    config = Container.instance().config()
    accessor = UniverseAccessor(
        config["duckdb_path"], config.get("universe_library", "Universes"))
    universe = accessor.get(name.strip())
    return [definition.symbol for definition in universe.security_definitions]


def create_research_router(
    cc,
    service: ResearchService,
    *,
    load_universe_symbols: UniverseLoader = _load_universe_symbols,
) -> APIRouter:
    router = APIRouter(prefix="/api/research", tags=["research"])

    def require_session(request: Request) -> str:
        return cc.require_session(request)

    def reject_unknown(request: Request, allowed: set[str]) -> None:
        unknown = set(request.query_params.keys()) - allowed
        if unknown:
            key = sorted(unknown)[0]
            raise RequestValidationError([{
                "type": "extra_forbidden",
                "loc": ("query", key),
                "msg": "Extra inputs are not permitted",
                "input": request.query_params.get(key),
            }])

    def validation_error(
        key: str, message: str, value: Any, *, error_type: str = "value_error",
    ) -> RequestValidationError:
        return RequestValidationError([{
            "type": error_type,
            "loc": ("query", key),
            "msg": message,
            "input": value,
        }])

    async def run(call: Callable[[], Any]):
        try:
            return await call()
        except ResearchError as exc:
            return JSONResponse(status_code=exc.status, content={"error": {
                "code": exc.code,
                "message": exc.message,
                "retryable": exc.retryable,
            }})

    @router.get("/presets")
    async def presets(
        request: Request, _session: str = Depends(require_session),
    ):
        reject_unknown(request, set())
        return service.presets()

    @router.get("/snapshot")
    async def snapshot(
        request: Request,
        symbol: str = Query(
            min_length=1,
            max_length=32,
            pattern=r"^[A-Za-z][A-Za-z0-9.\-]*$",
        ),
        _session: str = Depends(require_session),
    ):
        reject_unknown(request, {"symbol"})
        return await run(lambda: service.run(
            "snapshot",
            lambda provider: provider.snapshot(symbol),
            log_params={"symbol": symbol.upper()},
        ))

    @router.get("/news")
    async def news(
        request: Request,
        ticker: str = Query(
            min_length=1,
            max_length=32,
            pattern=r"^[A-Za-z][A-Za-z0-9.\-]*$",
        ),
        limit: int = Query(10, ge=1, le=50),
        source: Literal["polygon", "benzinga"] = "polygon",
        _session: str = Depends(require_session),
    ):
        reject_unknown(request, {"ticker", "limit", "source"})
        return await run(lambda: service.run(
            "news",
            lambda provider: provider.news(ticker, limit=limit, source=source),
            log_params={
                "ticker": ticker.upper(), "limit": limit, "source": source,
            },
        ))

    @router.get("/movers")
    async def movers(
        request: Request,
        market: Literal[
            "stocks", "crypto", "indices", "options", "futures",
        ] = "stocks",
        direction: Literal["gainers", "losers"] = "gainers",
        num: int = Query(20, ge=1, le=100),
        detail: bool = False,
        _session: str = Depends(require_session),
    ):
        reject_unknown(request, {"market", "direction", "num", "detail"})
        return await run(lambda: service.run(
            "movers",
            lambda provider: provider.movers(
                market=market, direction=direction, limit=num, detail=detail),
            log_params={
                "market": market,
                "direction": direction,
                "num": num,
                "detail": detail,
            },
        ))

    @router.get("/ideas")
    async def ideas(
        request: Request,
        preset: str = Query("momentum", min_length=1, max_length=40),
        source: Literal["movers", "tickers", "universe"] = "movers",
        tickers: list[str] = Query(default=[]),
        universe: str = "",
        num: int = Query(15, ge=1, le=50),
        min_price: float | None = Query(None, gt=0),
        max_price: float | None = Query(None, gt=0),
        min_volume: int | None = Query(None, ge=0),
        min_change: float | None = None,
        max_change: float | None = None,
        fundamentals: bool = False,
        news: bool = False,
        names: bool = False,
        _session: str = Depends(require_session),
    ):
        allowed = {
            "preset", "source", "tickers", "universe", "num",
            "min_price", "max_price", "min_volume", "min_change",
            "max_change", "fundamentals", "news", "names",
        }
        reject_unknown(request, allowed)
        if preset not in PRESETS:
            raise validation_error(
                "preset",
                f"Unknown preset: {preset}. Available: {', '.join(sorted(PRESETS))}",
                preset,
            )
        normalized = list(dict.fromkeys(
            ticker.strip().upper() for ticker in tickers if ticker.strip()))
        if len(normalized) > 100:
            raise validation_error(
                "tickers", "At most 100 tickers are allowed", tickers,
                error_type="too_long")
        if source == "tickers" and not normalized:
            raise validation_error(
                "tickers", "tickers are required when source=tickers", tickers)
        if source == "universe" and not universe.strip():
            raise validation_error(
                "universe", "universe is required when source=universe", universe)
        if source == "movers" and normalized:
            raise validation_error(
                "tickers", "tickers are not allowed when source=movers", tickers)
        if source == "movers" and universe.strip():
            raise validation_error(
                "universe", "universe is not allowed when source=movers", universe)
        if source == "tickers" and universe.strip():
            raise validation_error(
                "universe", "universe is not allowed when source=tickers", universe)
        if source == "universe" and normalized:
            raise validation_error(
                "tickers", "tickers are not allowed when source=universe", tickers)
        if (
            min_price is not None
            and max_price is not None
            and max_price < min_price
        ):
            raise validation_error(
                "max_price", "max_price must be greater than or equal to min_price",
                max_price)

        filters = {
            key: value
            for key, value in {
                "min_price": min_price,
                "max_price": max_price,
                "min_volume": min_volume,
                "min_change_pct": min_change,
                "max_change_pct": max_change,
            }.items()
            if value is not None
        }

        def provider_ideas(provider):
            universe_symbols = None
            if source == "universe":
                universe_symbols = load_universe_symbols(universe)
                if not universe_symbols:
                    raise LookupError("universe was not found or contains no symbols")
            return provider.ideas(
                preset=preset,
                source=source,
                tickers=normalized or None,
                universe_symbols=universe_symbols,
                top_n=num,
                custom_filters=filters or None,
                fundamentals=fundamentals,
                news=news,
                names=names,
            )

        return await run(lambda: service.run(
            "ideas",
            provider_ideas,
            log_params={
                "preset": preset,
                "source": source,
                "tickers": normalized,
                "universe": universe,
                "num": num,
                "filters": filters,
                "fundamentals": fundamentals,
                "news": news,
                "names": names,
            },
        ))

    return router
