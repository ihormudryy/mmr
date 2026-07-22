"""Bounded, isolated execution for Command Center research requests."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from trader.tools.massive_research import MassiveResearch, ResearchResult


logger = logging.getLogger("web.command_center.research")

DEFAULT_TIMEOUTS = {
    "snapshot": 10.0,
    "movers": 15.0,
    "news": 15.0,
    "ideas": 30.0,
}
_SENSITIVE_PARAM_PARTS = (
    "apikey",
    "accesskey",
    "authorization",
    "credentials",
    "credential",
    "password",
    "secret",
    "token",
)


@dataclass(frozen=True)
class ResearchError(Exception):
    status: int
    code: str
    message: str
    retryable: bool


def _safe_log_params(params: dict[str, Any] | None) -> dict[str, Any]:
    """Keep request telemetry useful without risking credential disclosure."""
    if not params:
        return {}
    return {
        str(key): _safe_log_value(value)
        for key, value in params.items()
        if not _is_sensitive_param_key(key)
    }


def _is_sensitive_param_key(key: Any) -> bool:
    canonical = "".join(character for character in str(key).lower() if character.isalnum())
    return any(part in canonical for part in _SENSITIVE_PARAM_PARTS)


def _safe_log_value(value: Any) -> Any:
    if isinstance(value, dict):
        return _safe_log_params(value)
    if isinstance(value, (list, tuple)):
        return [_safe_log_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return f"<{type(value).__name__}>"


class ResearchService:
    """Run slow provider calls outside the dashboard's normal request workers."""

    def __init__(
        self,
        provider_factory: Callable[[], MassiveResearch | None],
        *,
        workers: int = 4,
        timeouts: dict[str, float] | None = None,
        clock: Callable[[], str] | None = None,
    ):
        self._provider_factory = provider_factory
        self._provider: MassiveResearch | None = None
        self._provider_lock = threading.Lock()
        self._executor = ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="cc-research"
        )
        self._slots = threading.BoundedSemaphore(workers)
        self._wrapped_futures: set[asyncio.Future[ResearchResult]] = set()
        self._wrapped_futures_lock = threading.Lock()
        self._timeouts = {**DEFAULT_TIMEOUTS, **(timeouts or {})}
        self._clock = clock or self._utc_now
        self._closed = False

    @staticmethod
    def _utc_now() -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    def _get_provider(self) -> MassiveResearch:
        with self._provider_lock:
            if self._provider is None:
                self._provider = self._provider_factory()
            if self._provider is None:
                raise ResearchError(
                    503,
                    "MASSIVE_NOT_CONFIGURED",
                    "Massive API key is not configured.",
                    False,
                )
            return self._provider

    def _release_slot(self, completed: Future[ResearchResult]) -> None:
        del completed
        self._slots.release()

    def _retain_and_drain(self, wrapped: asyncio.Future[ResearchResult]) -> None:
        """Consume a late provider failure after its request waiter has left."""
        with self._wrapped_futures_lock:
            self._wrapped_futures.add(wrapped)

        def drain(completed: asyncio.Future[ResearchResult]) -> None:
            try:
                completed.exception()
            except asyncio.CancelledError:
                pass
            finally:
                with self._wrapped_futures_lock:
                    self._wrapped_futures.discard(completed)

        wrapped.add_done_callback(drain)

    async def run(
        self,
        tool: str,
        operation: Callable[[MassiveResearch], ResearchResult],
        *,
        log_params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        started = time.monotonic()
        outcome = "rejected"
        params = _safe_log_params(log_params)
        if self._closed or not self._slots.acquire(blocking=False):
            logger.info(
                "research tool=%s outcome=busy duration_ms=0 params=%r", tool, params
            )
            raise ResearchError(
                503,
                "RESEARCH_BUSY",
                "Research workers are busy; retry shortly.",
                True,
            )

        try:
            try:
                future = self._executor.submit(lambda: operation(self._get_provider()))
            except Exception as exc:
                self._slots.release()
                outcome = "upstream_error"
                logger.warning("research %s submit failed: %s", tool, type(exc).__name__)
                raise ResearchError(
                    502,
                    "RESEARCH_UPSTREAM_ERROR",
                    f"{tool.title()} provider request failed.",
                    True,
                ) from exc

            future.add_done_callback(self._release_slot)
            wrapped = asyncio.wrap_future(future)
            self._retain_and_drain(wrapped)
            try:
                result = await asyncio.wait_for(
                    asyncio.shield(wrapped), self._timeouts[tool]
                )
                outcome = "ok"
            except asyncio.TimeoutError as exc:
                outcome = "timeout"
                raise ResearchError(
                    504,
                    "RESEARCH_TIMEOUT",
                    f"{tool.title()} did not complete within {self._timeouts[tool]:g} seconds.",
                    True,
                ) from exc
            except ResearchError:
                outcome = "configuration_error"
                raise
            except Exception as exc:
                outcome = "upstream_error"
                logger.warning("research %s failed: %s", tool, type(exc).__name__)
                raise ResearchError(
                    502,
                    "RESEARCH_UPSTREAM_ERROR",
                    f"{tool.title()} provider request failed.",
                    True,
                ) from exc
        finally:
            logger.info(
                "research tool=%s outcome=%s duration_ms=%d params=%r",
                tool,
                outcome,
                int((time.monotonic() - started) * 1000),
                params,
            )

        return {
            "data": result.data,
            "title": result.title,
            "meta": {
                "tool": tool,
                "provider": result.provider,
                "observed_at": self._clock(),
                "notice": result.notice,
            },
        }

    def presets(self) -> dict[str, Any]:
        result = MassiveResearch(object()).presets()
        return {
            "data": result.data,
            "title": result.title,
            "meta": {
                "tool": "presets",
                "provider": "local",
                "observed_at": self._clock(),
                "notice": None,
            },
        }

    def close(self) -> None:
        self._closed = True
        self._executor.shutdown(wait=False, cancel_futures=True)


def build_research_service() -> ResearchService:
    """Build a service without touching config or the Massive SDK until work arrives."""

    def provider_factory() -> MassiveResearch | None:
        try:
            from trader.container import Container

            api_key = Container.instance().config().get("massive_api_key", "")
        except Exception:
            # Config failures must not stop the dashboard from starting; a request
            # receives the stable MASSIVE_NOT_CONFIGURED response instead.
            return None
        if not isinstance(api_key, str) or not api_key.strip():
            return None

        from massive import RESTClient

        return MassiveResearch(RESTClient(api_key=api_key))

    return ResearchService(provider_factory)
