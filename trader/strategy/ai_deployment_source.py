"""The strategy service's second source: active AI deployments from the trader (SP2c spec 5.4).

Paper only. An instance runs the exact bytes whose hash the trader marked active,
under a name derived from the version digest; anything else is unloaded.
"""
from __future__ import annotations

import hmac
import logging
from dataclasses import dataclass
from typing import Any, Callable, Sequence

from trader.data.backtest_store import compute_strategy_hash
from trader.messaging.typed_rpc import TypedRpcRemoteError
from trader.strategy.history_depth import ib_requests_for

AI_INSTANCE_PREFIX = "aidv-"
# The least history an AI instance loads; a strategy's MIN_BARS can raise it (history_depth).
AI_HISTORY_DAYS = 5
# Bounds one instance's backfill: a few IB history requests at ib_async's 60 s default each.
AI_HISTORY_TIMEOUT_S = 180
AI_HISTORY_REQUEST_TIMEOUT_S = 60


@dataclass(frozen=True)
class AiInstanceBinding:
    version_digest: str
    base_digest: str
    strategy_digest: str


def ai_history_timeout_s(bar_size: Any, warmup_days: int, conid_count: int) -> int:
    """The backfill budget: AI_HISTORY_TIMEOUT_S, more only when a declared warm-up needs many IB requests
    (a strategy without MIN_BARS has warmup_days 0 and keeps the old budget)."""
    if warmup_days <= 0:
        return AI_HISTORY_TIMEOUT_S
    requests = ib_requests_for(bar_size, warmup_days) * max(1, conid_count)
    return max(AI_HISTORY_TIMEOUT_S, requests * AI_HISTORY_REQUEST_TIMEOUT_S)


def ai_instance_name(version_digest: str) -> str:
    return AI_INSTANCE_PREFIX + version_digest.split(":", 1)[1][:16]


def source_unchanged(instance: Any) -> bool:
    """True while the file on disk still hashes to the digest the instance was loaded from."""
    current = compute_strategy_hash(getattr(instance, "ai_source_path", ""))
    return bool(current) and hmac.compare_digest("sha256:" + current, instance.ai_source_digest)


class AiDeploymentSource:
    def __init__(self, *, runtime: Any, read_active: Callable[[], Sequence[Any]], paper: bool):
        self._runtime, self._read_active, self._paper = runtime, read_active, paper

    def reconcile(self) -> None:
        loaded = self._runtime.ai_instances()
        if not self._paper:
            for name in loaded:
                self._runtime.unload_strategy(name)
            return
        try:
            active = list(self._read_active())
        except (TimeoutError, ConnectionError) as ex:
            logging.warning("active AI deployments unavailable; keeping the loaded set: %s", ex)
            return
        except TypedRpcRemoteError as ex:
            if ex.code != "METHOD_NOT_ALLOWED":
                raise
            active = []                                   # ai_paper is off: nothing may run
        wanted = {ai_instance_name(d.version_digest): d for d in active}
        for name, instance in loaded.items():
            if name not in wanted:
                logging.info("unloading AI deployment %s: no longer active", name)
                self._runtime.unload_strategy(name)
            elif not source_unchanged(instance):
                logging.error("unloading AI deployment %s: its file changed after load", name)
                self._runtime.unload_strategy(name)
        for name, deployment in wanted.items():
            if self._runtime.get_strategy(name) is None and not self._runtime.load_ai_deployment(deployment):
                logging.error("AI deployment %s did not load", deployment.version_digest)
