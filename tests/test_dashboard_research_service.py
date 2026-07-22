import asyncio
import threading

import pytest

from trader.tools.massive_research import ResearchResult
from web.command_center.research import DEFAULT_TIMEOUTS, ResearchError, ResearchService


@pytest.mark.asyncio
async def test_success_envelope_contains_metadata():
    service = ResearchService(lambda: object(), workers=1,
                              clock=lambda: "2026-07-22T12:00:00Z")
    body = await service.run("news", lambda provider: ResearchResult(
        [{"title": "x"}], "News: AAPL"))
    assert body == {
        "data": [{"title": "x"}], "title": "News: AAPL",
        "meta": {"tool": "news", "provider": "massive",
                 "observed_at": "2026-07-22T12:00:00Z", "notice": None},
    }
    service.close()


@pytest.mark.asyncio
async def test_saturation_fails_fast_and_timeout_holds_slot_until_exit():
    started = threading.Event()
    release = threading.Event()
    service = ResearchService(lambda: object(), workers=1,
                              timeouts={"snapshot": 0.01})

    async def first():
        with pytest.raises(ResearchError) as caught:
            await service.run("snapshot", lambda provider: (
                started.set(), release.wait(), ResearchResult({}, "Snapshot"))[2])
        assert caught.value.code == "RESEARCH_TIMEOUT"

    task = asyncio.create_task(first())
    await asyncio.to_thread(started.wait, 1)
    await task
    with pytest.raises(ResearchError) as busy:
        await service.run("snapshot", lambda provider: ResearchResult({}, "Snapshot"))
    assert busy.value.code == "RESEARCH_BUSY"
    release.set()
    await asyncio.sleep(0.02)
    service.close()


@pytest.mark.asyncio
async def test_missing_provider_maps_to_configuration_error():
    service = ResearchService(lambda: None)
    with pytest.raises(ResearchError) as caught:
        await service.run("news", lambda provider: ResearchResult([], "News"))
    assert (caught.value.status, caught.value.code, caught.value.retryable) == (
        503, "MASSIVE_NOT_CONFIGURED", False)
    service.close()


@pytest.mark.asyncio
async def test_provider_exception_is_sanitized():
    service = ResearchService(lambda: object(), workers=1)
    with pytest.raises(ResearchError) as caught:
        await service.run("news", lambda provider: (_ for _ in ()).throw(
            RuntimeError("secret vendor response")))
    assert caught.value.code == "RESEARCH_UPSTREAM_ERROR"
    assert "secret vendor response" not in caught.value.message
    service.close()


@pytest.mark.asyncio
async def test_log_contains_timing_and_sanitized_params(caplog):
    service = ResearchService(lambda: object(), workers=1)
    with caplog.at_level("INFO", logger="web.command_center.research"):
        await service.run("news", lambda provider: ResearchResult([], "News"),
                          log_params={"ticker": "AAPL", "limit": 10,
                                      "api_key": "must-not-appear"})
    message = caplog.records[-1].getMessage()
    assert "tool=news" in message and "outcome=ok" in message
    assert "ticker" in message and "AAPL" in message and "duration_ms=" in message
    assert "api_key" not in message and "must-not-appear" not in message
    service.close()


@pytest.mark.parametrize("tool,seconds", [
    ("snapshot", 10.0), ("movers", 15.0), ("news", 15.0), ("ideas", 30.0),
])
def test_default_timeout_budgets(tool, seconds):
    assert DEFAULT_TIMEOUTS[tool] == seconds


@pytest.mark.asyncio
async def test_close_rejects_new_work():
    service = ResearchService(lambda: object(), workers=1)
    service.close()
    with pytest.raises(ResearchError) as caught:
        await service.run("news", lambda provider: ResearchResult([], "News"))
    assert caught.value.code == "RESEARCH_BUSY"


@pytest.mark.asyncio
async def test_submit_failure_releases_admission_slot():
    service = ResearchService(lambda: object(), workers=1)

    class FailingExecutor:
        def submit(self, work):
            raise RuntimeError("executor unavailable")

        def shutdown(self, **kwargs):
            pass

    service._executor.shutdown(wait=False)
    service._executor = FailingExecutor()
    with pytest.raises(ResearchError) as caught:
        await service.run("news", lambda provider: ResearchResult([], "News"))
    assert caught.value.code == "RESEARCH_UPSTREAM_ERROR"
    assert service._slots.acquire(blocking=False)
    service._slots.release()
    service.close()
