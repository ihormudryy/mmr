"""The acceptance port: the typed RPC calls the scenario needs (Plan 6 rulings 1, 2 and 23).

Three signers. ``research`` signs as ``ai_research`` (register), ``supervisor`` as
``ai_supervisor`` (publish, decide and every read), ``operator`` as ``cli`` and
only for ``acceptance_mark_start`` and ``acceptance_shrink_probe``. A port built
without the operator client (a run without ``--place-orders``) refuses operator
calls with ``OperatorChannelUnavailable``.
"""
from __future__ import annotations

import datetime as dt
import time
from typing import Any, Callable, Optional, Protocol

SUPERVISOR_COMMANDS = frozenset({"publish_ai_risk_policy", "submit_ai_paper_decision", "pause_experiment"})
OPERATOR_COMMANDS = frozenset({"acceptance_mark_start", "acceptance_shrink_probe"})


class OperatorChannelUnavailable(RuntimeError):
    """The port has no ``cli`` client: this run was started without ``--place-orders``."""


class RemoteRefusal(RuntimeError):
    """The trader answered with a typed RPC error (permission, validation, ...)."""

    def __init__(self, code: str, message: str = ""):
        self.code = code
        super().__init__(f"{code}: {message}")


class AcceptancePort(Protocol):
    def research(self, method: str, body: dict) -> dict: ...

    def supervisor(self, method: str, body: dict) -> dict: ...

    def operator(self, method: str, body: dict) -> dict: ...

    def evidence(self, conid: Optional[int] = None) -> dict: ...

    def trips(self, experiment_id: str) -> dict: ...

    def now(self) -> dt.datetime: ...

    def sleep(self, seconds: float) -> None: ...


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class RpcAcceptancePort:
    """``AcceptancePort`` over ``TypedRpcClient``s; nothing here knows which stack answers."""

    def __init__(self, research_client: Any, supervisor_command: Any, supervisor_query: Any,
                 operator_client: Any = None, *, now: Callable[[], dt.datetime] = _utc_now,
                 sleep: Callable[[float], None] = time.sleep):
        self._research = research_client
        self._supervisor_command = supervisor_command
        self._supervisor_query = supervisor_query
        self._operator = operator_client
        self._now = now
        self._sleep = sleep

    @property
    def has_operator(self) -> bool:
        return self._operator is not None

    def research(self, method: str, body: dict) -> dict:
        return self._call(self._research, method, body)

    def supervisor(self, method: str, body: dict) -> dict:
        client = self._supervisor_command if method in SUPERVISOR_COMMANDS else self._supervisor_query
        return self._call(client, method, body)

    def operator(self, method: str, body: dict) -> dict:
        if self._operator is None:
            raise OperatorChannelUnavailable("the operator (cli) channel is loaded only with --place-orders")
        if method not in OPERATOR_COMMANDS:
            raise ValueError(f"{method} is not an acceptance operator command")
        return self._call(self._operator, method, body)

    def evidence(self, conid: Optional[int] = None) -> dict:
        return self.supervisor("get_broker_order_evidence", {"conid": conid})

    def trips(self, experiment_id: str) -> dict:
        return self.supervisor("get_experiment_trips", {"experiment_id": experiment_id})

    def now(self) -> dt.datetime:
        return self._now()

    def sleep(self, seconds: float) -> None:
        self._sleep(seconds)

    @staticmethod
    def _call(client: Any, method: str, body: dict) -> dict:
        from trader.messaging.typed_rpc import TypedRpcRemoteError
        try:
            return client.call(method, body, dict)
        except TypedRpcRemoteError as exc:
            raise RemoteRefusal(exc.code, str(exc)) from exc
