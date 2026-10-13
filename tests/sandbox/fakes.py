"""Test-only in-process spawner (ruling 12). The production entry never constructs it."""
from __future__ import annotations

from datetime import datetime
from typing import Mapping

from trader.sandbox.child import ChildRuntime, SpawnSpec


class _InProcessChild:
    pid = -1   # never a real pid: the supervisor must only ever call close(), never signal a pid itself

    def __init__(self, spec: SpawnSpec, spawner: "InProcessSpawner"):
        # In process (pytest), NEVER skip the static check, even for a probe: hostile escape
        # code must never run inside the test runner. A clean probe (benign_orb) passes
        # require_clean anyway; a hostile one is refused here, so only the real forked child
        # (container tests) ever runs it with the check skipped.
        self._spawner = spawner
        self._closed = False
        self._runtime = ChildRuntime(source=spec.source, class_name=spec.class_name,
                                     params=spec.params, lookback=spec.lookback, conids=spec.conids,
                                     skip_static_check=False)
        self._runtime.load()
        self.lookback = self._runtime.lookback

    def send_bar(self, conid: int, t: datetime, ohlcv: Mapping[str, float]) -> dict:
        return self._runtime.on_bar(conid, t, ohlcv)

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._spawner.closed += 1


class InProcessSpawner:
    def __init__(self):
        self.spawned = 0
        self.closed = 0
        self.specs: list[SpawnSpec] = []

    def spawn(self, spec: SpawnSpec):
        self.spawned += 1
        self.specs.append(spec)
        return _InProcessChild(spec, self)
