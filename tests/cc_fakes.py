"""Shared no-op command-center fakes: a bridge and a quote plane that do
nothing (spec: [M1-R] Task 5).

Used by tests that need to construct a working ``CommandCenter`` without a
live typed-RPC bridge thread or a real ticker PubSub connection --
``tests/test_dashboard_snapshot_api.py`` and ``tests/test_web_dashboard.py``.
"""
from __future__ import annotations


class NullBridge:
    def health(self) -> dict:
        return {"lifecycle": "live", "reconnects": 0, "cursor": 1,
                "sources": {"journal": {"state": "ok",
                                        "last_success_age_seconds": 0.1,
                                        "last_error": None, "reconnects": 0}}}

    def stop(self, timeout: float = 5.0) -> None:
        pass


class NullQuotePlane:
    dropped = 0

    def start(self) -> None:
        pass

    def stop(self, timeout: float = 5.0) -> None:
        pass
