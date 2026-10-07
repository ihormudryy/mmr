"""The real ai service in a child process, for spec 12 crash test 2 (run as `python -m`)."""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import logging
import sys
import time
from pathlib import Path

from tests.ai.runtime.scripted_engine import ScriptedEngine
from trader.ai_service import ServiceSettings, run_service


class OffsetClock:
    """Real time, shifted so the child starts at the served trader's instant."""

    def __init__(self, start: dt.datetime):
        self._start, self._t0 = start, time.monotonic()

    def now(self) -> dt.datetime:
        return self._start + dt.timedelta(seconds=time.monotonic() - self._t0)

    def monotonic(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


def block_after_submit(marker: Path):
    """Once the trader's reply to a submit arrives: write it to the marker and hang. The receipt is never saved."""
    def wrap(clients):
        real_call = clients.supervisor.call

        async def call(method, body, *, epoch=None):
            reply = await real_call(method, body, epoch=epoch)
            if method == "submit_ai_paper_decision":
                marker.write_text(json.dumps(reply))
                await asyncio.Event().wait()
            return reply
        clients.supervisor.call = call
        return clients
    return wrap


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--keys-dir", required=True)
    parser.add_argument("--clock-start", required=True)
    parser.add_argument("--script", required=True)
    parser.add_argument("--block-after-submit")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    marker = Path(args.block_after_submit) if args.block_after_submit else None
    return run_service(ServiceSettings(config_path=args.config, keys_dir=args.keys_dir,
                                       trader_address="tcp://127.0.0.1"),
                       engine_factory=lambda deps: ScriptedEngine.from_file(args.script),
                       clock=OffsetClock(dt.datetime.fromisoformat(args.clock_start)),
                       wrap_clients=block_after_submit(marker) if marker else None)


if __name__ == "__main__":
    sys.exit(main())
