#!/usr/bin/env python3
"""Command-center performance/soak harness (spec §13.3).

Drives the real app path (fake journal feed -> DashboardEventBridge -> reducer
-> SseFanout -> /api/events) entirely in-process:

  100 active instruments x 4 quote updates/s, 20 domain events/s, 3 SSE tabs.

Measures producer-commit -> client-reducer latency (the tab's JSON parse
stands in for the browser reducer application) against the 500 ms p95 target
and samples process RSS/CPU for the soak thresholds: RSS growth <= 20% after
the warm-up hour, average CPU < 1 core. The 8-hour execution belongs to
[COMPAT]; smoke:

  MMR_DILL_STRICT=1 uv run --frozen python scripts/soak_harness.py --minutes 2
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import socket
import sys
import threading
import time
from pathlib import Path

import httpx
import psutil
import uvicorn

from trader.domain.events import (
    DomainEvent,
    ReadDomainEventsResult,
    SnapshotWithCursor,
)

TOKEN = "soak-token"
SECRET = "s" * 64
P95_TARGET_MS = 500.0
RSS_GROWTH_LIMIT_PCT = 20.0
CPU_LIMIT_CORES = 1.0
WARMUP_CAP_SECONDS = 3600.0


class FakeJournalFeed:
    """Blocking long-poll feed over an in-memory journal (commit == append)."""

    def __init__(self):
        self._events: list[DomainEvent] = []
        self._cond = threading.Condition()
        self.closed = False

    def append(self, event: DomainEvent) -> None:
        with self._cond:
            self._events.append(event)
            self._cond.notify_all()

    def call(self, method, body, response_model):
        assert method == "read_domain_events"
        after, limit = body["after_cursor"], body["limit"]
        deadline = time.monotonic() + body["wait_ms"] / 1000.0
        with self._cond:
            while True:
                pending = [e for e in self._events if e.source_cursor > after][:limit]
                if pending or self.closed:
                    newest = pending[-1].source_cursor if pending else after
                    return ReadDomainEventsResult(
                        events=tuple(pending), newest_cursor=newest)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return ReadDomainEventsResult(events=(), newest_cursor=after)
                self._cond.wait(timeout=remaining)

    def close(self):
        with self._cond:
            self.closed = True
            self._cond.notify_all()


class FakeQuery:
    def __init__(self, instruments: int):
        self._instruments = instruments

    def call(self, method, body, response_model):
        if method == "snapshot_with_cursor":
            return SnapshotWithCursor(source_cursor=0, broker_generation=1, entities={
                "account": [{"entity_id": "DU111", "entity_revision": 1,
                             "mode": "paper", "net_liquidation": 1_000_000.0,
                             "currency": "USD"}],
                "position": [{"entity_id": f"DU111:{conid}", "entity_revision": 1,
                              "conid": conid, "symbol": f"SYM{conid}",
                              "quantity": 100, "currency": "USD"}
                             for conid in range(1, self._instruments + 1)]})
        if method == "get_quotes_snapshot":
            return {"quotes": {}}
        raise AssertionError(method)

    def close(self):
        pass


class DomainProducer(threading.Thread):
    def __init__(self, feed: FakeJournalFeed, instruments: int, eps: float,
                 stop: threading.Event):
        super().__init__(daemon=True, name="soak-domain-producer")
        self._feed, self._instruments = feed, instruments
        self._eps, self._stop = eps, stop
        self.emitted = 0

    def run(self):
        cursor, revisions = 0, {}
        interval = 1.0 / self._eps
        while not self._stop.is_set():
            cursor += 1
            conid = (cursor % self._instruments) + 1
            entity = f"DU111:{conid}"
            revisions[entity] = revisions.get(entity, 1) + 1
            self._feed.append(DomainEvent(
                event_id=f"evt-{cursor}", source_cursor=cursor,
                entity_revision=revisions[entity],
                event_type="position.updated", entity_type="position",
                entity_id=entity, operation="upsert", account_id="DU111",
                source="soak",
                source_timestamp=dt.datetime.now(dt.timezone.utc),
                correlation_id=None,
                payload={"quantity": cursor, "conid": conid,
                         "harness_emitted_at": time.time()}))  # commit time
            self.emitted += 1
            self._stop.wait(interval)


class SyntheticQuotePlane(threading.Thread):
    """Same deliver contract as QuotePlane; emits synthetic conflated batches."""

    def __init__(self, loop, deliver, instruments: int, hz: float,
                 stop: threading.Event):
        super().__init__(daemon=True, name="soak-quote-producer")
        self._loop, self._deliver = loop, deliver
        self._instruments, self._interval = instruments, 1.0 / hz
        self._stop = stop
        self.dropped = 0
        self.emitted = 0

    def run(self):
        tick = 0
        while not self._stop.is_set():
            tick += 1
            batch = {str(conid): {"instrument_id": str(conid), "bid": 99.9,
                                  "ask": 100.1, "last": 100.0 + (tick % 7) * 0.01,
                                  "market_timestamp": None, "feed_type": "synthetic"}
                     for conid in range(1, self._instruments + 1)}
            self._loop.call_soon_threadsafe(self._deliver, batch)
            self.emitted += self._instruments
            self._stop.wait(self._interval)

    def stop(self, timeout: float = 5.0):
        self._stop.set()


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def run_tab(client: httpx.AsyncClient, latencies: list[float],
                  stop_at: float) -> None:
    async with client.stream("GET", "/api/events") as response:
        async for line in response.aiter_lines():
            if time.time() >= stop_at:
                return
            if not line.startswith("data:"):
                continue
            payload = json.loads(line[5:])
            emitted = (payload.get("payload") or {}).get("harness_emitted_at")
            if emitted is not None:
                latencies.append((time.time() - emitted) * 1000.0)


def _pct(sorted_ms: list[float], q: float) -> float | None:
    if not sorted_ms:
        return None
    return sorted_ms[min(len(sorted_ms) - 1, int(len(sorted_ms) * q))]


def _report(latencies: list[float], rss: list[tuple[float, int]],
            warmup: float, cpu_cores: float, args) -> tuple[dict, list[str]]:
    ordered = sorted(latencies)
    post = [r for t, r in rss if t >= warmup] or [r for _, r in rss]
    growth = ((post[-1] - post[0]) / post[0] * 100.0) if post and post[0] else 0.0
    p95 = _pct(ordered, 0.95)
    violations = []
    if p95 is None or p95 > P95_TARGET_MS:
        violations.append(f"p95 {p95} ms exceeds {P95_TARGET_MS} ms target")
    if growth > RSS_GROWTH_LIMIT_PCT:
        violations.append(f"RSS grew {growth:.1f}% after warm-up (limit 20%)")
    if cpu_cores >= CPU_LIMIT_CORES:
        violations.append(f"avg CPU {cpu_cores:.2f} cores (limit < 1)")
    report = {
        "config": {"minutes": args.minutes, "instruments": args.instruments,
                   "quote_hz": args.quote_hz, "domain_eps": args.domain_eps,
                   "tabs": args.tabs},
        "domain_events_received": len(latencies),
        "latency_ms": {"p50": _pct(ordered, 0.50), "p95": p95,
                       "p99": _pct(ordered, 0.99),
                       "max": ordered[-1] if ordered else None},
        "rss_bytes": {"first_post_warmup": post[0] if post else None,
                      "final": post[-1] if post else None,
                      "growth_pct": round(growth, 2)},
        "cpu_avg_cores": round(cpu_cores, 3),
        "violations": violations,
    }
    return report, violations


async def amain(args) -> int:
    os.environ.setdefault("MMR_DILL_STRICT", "1")
    from web.app import create_app
    from web.command_center import CommandCenter, CommandCenterConfig
    from web.command_center.session import DashboardCredentials

    feed = FakeJournalFeed()
    stop = threading.Event()

    cc = CommandCenter(
        CommandCenterConfig(),
        credentials_loader=lambda: DashboardCredentials(
            token=TOKEN, session_secret=SECRET.encode(), legacy_alias_used=False),
        query_client_factory=lambda: FakeQuery(args.instruments),
        feed_client_factory=lambda: feed,
        quote_plane_factory=lambda loop, deliver: SyntheticQuotePlane(
            loop, deliver, args.instruments, args.quote_hz, stop))
    app = create_app(cc)

    process = psutil.Process()
    latencies: list[float] = []
    rss_samples: list[tuple[float, int]] = []
    duration = args.minutes * 60.0
    warmup = min(WARMUP_CAP_SECONDS, duration * 0.2)

    # A real, locally-bound uvicorn server -- deliberately NOT
    # httpx.ASGITransport. ASGITransport collects an ASGI app's entire
    # response into an in-memory list before ever handing any of it back to
    # the client, so it cannot stream an indefinitely-lived SSE connection
    # (the harness would sit at domain_events_received == 0 for the whole
    # run: the tab's aiter_lines() sees nothing until the server-side
    # generator finishes, which for /api/events never happens on its own).
    # A real socket delivers each SSE chunk as it's flushed, exactly like a
    # browser tab would see it -- and it exercises uvicorn's own lifespan
    # handling, so `create_app`'s lifespan (which starts the bridge/quote
    # plane) runs the same way it does under `python3 -m web.app`.
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    uv_server = uvicorn.Server(config)
    thread = threading.Thread(target=uv_server.run, daemon=True, name="soak-uvicorn")
    thread.start()
    deadline = time.time() + 15.0
    while not uv_server.started and time.time() < deadline:
        await asyncio.sleep(0.05)
    if not uv_server.started:
        raise RuntimeError("soak harness: uvicorn server did not start within 15s")

    try:
        producer = DomainProducer(feed, args.instruments, args.domain_eps, stop)
        producer.start()
        started = time.time()
        stop_at = started + duration
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}",
                                     timeout=None) as client:
            await client.post("/session", data={"token": TOKEN})
            tabs = [asyncio.create_task(run_tab(client, latencies, stop_at))
                    for _ in range(args.tabs)]
            process.cpu_percent()  # prime the interval counter
            while time.time() < stop_at:
                await asyncio.sleep(min(10.0, max(0.1, stop_at - time.time())))
                rss_samples.append(
                    (time.time() - started, process.memory_info().rss))
            cpu_cores = process.cpu_percent() / 100.0
            stop.set()
            feed.close()
            for tab in tabs:
                tab.cancel()
            await asyncio.gather(*tabs, return_exceptions=True)
    finally:
        uv_server.should_exit = True
        thread.join(timeout=10.0)

    report, violations = _report(latencies, rss_samples, warmup, cpu_cores, args)
    print(json.dumps(report, indent=2))
    if args.report:
        Path(args.report).expanduser().write_text(json.dumps(report, indent=2))
    return 1 if violations else 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Command-center perf/soak harness (spec §13.3)")
    parser.add_argument("--minutes", type=float, default=5.0)
    parser.add_argument("--instruments", type=int, default=100)
    parser.add_argument("--quote-hz", type=float, default=4.0)
    parser.add_argument("--domain-eps", type=float, default=20.0)
    parser.add_argument("--tabs", type=int, default=3)
    parser.add_argument("--report", default="")
    return asyncio.run(amain(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
