"""The acceptance port: the typed RPC calls the scenario needs (Plan 6 rulings 1, 2 and 23).

Two signers. ``supervisor`` signs as ``ai_supervisor`` (publish, decide and
every read), ``operator`` as ``cli`` and only for ``acceptance_mark_start`` and
``acceptance_shrink_probe``. Nothing registers a deployment: the run trades
under an operator-given judged version (SP2c Plan 2 ruling 18). A port built
without the operator client (a run without ``--place-orders``) refuses operator
calls with ``OperatorChannelUnavailable``.
"""
from __future__ import annotations

import datetime as dt
import time
import uuid
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
    def supervisor(self, method: str, body: dict) -> dict: ...

    def operator(self, method: str, body: dict) -> dict: ...

    def evidence(self, conid: Optional[int] = None) -> dict: ...

    def trips(self, experiment_id: str) -> dict: ...

    def now(self) -> dt.datetime: ...

    def sleep(self, seconds: float) -> None: ...


ACCEPTANCE_LEASE_SECONDS = 60
HELD_RETRY_SECONDS = 5.0


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class RpcAcceptancePort:
    """``AcceptancePort`` over ``TypedRpcClient``s; nothing here knows which stack answers."""

    def __init__(self, supervisor_command: Any, supervisor_query: Any,
                 operator_client: Any = None, *, now: Callable[[], dt.datetime] = _utc_now,
                 sleep: Callable[[float], None] = time.sleep):
        self._supervisor_command = supervisor_command
        self._supervisor_query = supervisor_query
        self._operator = operator_client
        self._now = now
        self._sleep = sleep
        self._holder_id = f"acceptance-{uuid.uuid4().hex[:12]}"
        self._epoch: Optional[int] = None

    @property
    def has_operator(self) -> bool:
        return self._operator is not None

    def supervisor(self, method: str, body: dict) -> dict:
        client = self._supervisor_command if method in SUPERVISOR_COMMANDS else self._supervisor_query
        if method == "submit_ai_paper_decision":
            return self._call(client, method, body, controller_epoch=self._controller_epoch())
        return self._call(client, method, body)

    def _controller_epoch(self) -> int:
        """SP2 spec 5.1: the harness is a controller; grant or renew before each decision (Plan 1 Ruling 16)."""
        waited = 0.0
        while True:
            try:
                grant = self._call(self._supervisor_command, "grant_ai_controller_epoch", {
                    "holder_id": self._holder_id, "current_epoch": self._epoch,
                    "lease_seconds": ACCEPTANCE_LEASE_SECONDS})
            except RemoteRefusal as refusal:
                if refusal.code != "CONTROLLER_EPOCH_HELD" or waited >= ACCEPTANCE_LEASE_SECONDS:
                    raise
                self._sleep(HELD_RETRY_SECONDS)
                waited += HELD_RETRY_SECONDS
                continue
            self._epoch = grant["epoch"]
            return self._epoch

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
    def _call(client: Any, method: str, body: dict, **options: Any) -> dict:
        from trader.messaging.typed_rpc import TypedRpcRemoteError
        try:
            return client.call(method, body, dict, **options)
        except TypedRpcRemoteError as exc:
            raise RemoteRefusal(exc.code, str(exc)) from exc
