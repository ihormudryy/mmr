"""Method-restricted typed RPC clients of the ai service (SP2 spec 4, Plan 5 Rulings 1-3).

Two principals in one process (spec 4 accepted limit: not process isolation).
Each client refuses a method outside its set before anything is signed or
sent. TypedRpcClient is blocking and holds a lock per call, so every call runs
on a worker thread, and the 90 s discovery read has its own socket.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

SUPERVISOR_COMMANDS = frozenset({
    "grant_ai_controller_epoch", "submit_ai_paper_decision", "record_ai_cost", "record_simulated_decision"})
SUPERVISOR_QUERIES = frozenset({
    "read_ai_signals", "get_ai_paper_decision", "get_experiment", "get_experiment_trips", "get_ai_risk_policy",
    "get_ai_deployment", "get_snapshot", "get_positions", "get_ai_model_budget",
    "get_account_values", "get_ai_entry_quote"})                          # the last two: SP2 Plan 6 Ruling 4
SUPERVISOR_SLOW_QUERIES: Mapping[str, float] = {"discover_ai_candidates": 90.0}
RESEARCH_COMMANDS = frozenset({"register_ai_deployment"})
RESEARCH_QUERIES = frozenset({"get_ai_deployment"})
EPOCH_METHODS = frozenset({"submit_ai_paper_decision", "read_ai_signals", "get_ai_paper_decision"})
EPOCH_REFUSALS = frozenset({"CONTROLLER_EPOCH_MISSING", "CONTROLLER_EPOCH_STALE"})
ENGINE_QUERIES = (SUPERVISOR_QUERIES | frozenset(SUPERVISOR_SLOW_QUERIES)) - EPOCH_METHODS


class RpcNotSent(Exception):
    """Proven: the trader never received this request (local refusal, or no route before the send)."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code, self.detail = code, detail


class MethodNotAllowedLocally(RpcNotSent):
    """The method is outside this principal's client set (Ruling 2)."""


class RpcOutcomeUnknown(Exception):
    """The request may have reached the trader; only a read can tell what happened."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code, self.detail = code, detail


class RpcRefused(Exception):
    """The trader answered ok=False: it handled the request and refused it."""

    def __init__(self, code: str, message: str = "", details: Optional[dict] = None):
        super().__init__(f"{code}: {message}" if message else code)
        self.code, self.message, self.details = code, message, details


def _call_blocking(client: Any, method: str, body: dict, timeout: float, options: dict) -> dict:
    from trader.messaging.typed_rpc import TypedRpcRemoteError
    try:
        return client.call(method, body, dict, timeout, **options)
    except TypedRpcRemoteError as exc:
        raise RpcRefused(exc.code, exc.message, exc.details) from None
    except ConnectionError as exc:
        # TypedRpcClient.call raises ConnectionError only before the send: no socket, or IMMEDIATE=1 refused.
        raise RpcNotSent("TRADER_UNREACHABLE", str(exc)) from None
    except TimeoutError as exc:
        raise RpcOutcomeUnknown("REPLY_TIMEOUT", str(exc)) from None
    except Exception as exc:                       # a reply that failed verification, or anything after the send
        raise RpcOutcomeUnknown("REPLY_UNTRUSTED", type(exc).__name__) from None


class PrincipalClient:
    def __init__(self, principal: str, *, command: Any, query: Any, commands: frozenset[str],
                 queries: frozenset[str], timeout: float, slow_query: Any = None,
                 slow_queries: Optional[Mapping[str, float]] = None):
        self.principal = principal
        slow = dict(slow_queries or {})
        if slow and slow_query is None:
            raise ValueError("slow queries need their own socket")
        self._routes = {**{m: command for m in commands}, **{m: query for m in queries},
                        **{m: slow_query for m in slow}}
        self._timeouts = slow
        self._timeout = timeout
        self._epoch: Callable[[], Optional[int]] = lambda: None
        self._sockets = [s for s in (command, query, slow_query) if s is not None]

    @property
    def methods(self) -> frozenset[str]:
        return frozenset(self._routes)

    def bind_epoch(self, source: Callable[[], Optional[int]]) -> None:
        self._epoch = source

    async def call(self, method: str, body: dict, *, epoch: Optional[int] = None) -> dict:
        client = self._routes.get(method)
        if client is None:
            raise MethodNotAllowedLocally("METHOD_NOT_IN_CLIENT_SET", f"{self.principal} may not call {method}")
        options: dict = {}
        if method in EPOCH_METHODS:
            held = epoch if epoch is not None else self._epoch()
            if held is None:
                raise RpcNotSent("NOT_LEADER", f"{method} needs a held controller epoch")
            options["controller_epoch"] = held
        timeout = self._timeouts.get(method, self._timeout)
        return await asyncio.to_thread(_call_blocking, client, method, body, timeout, options)

    def close(self) -> None:
        for socket in self._sockets:
            socket.close()


class ReadOnlySupervisor:
    """What a DecisionEngine may ask the trader: queries only, never a command, never an epoch method."""

    def __init__(self, supervisor: PrincipalClient):
        self._supervisor = supervisor

    async def call(self, method: str, body: dict) -> dict:
        if method not in ENGINE_QUERIES:
            raise MethodNotAllowedLocally("METHOD_NOT_FOR_ENGINES", f"an engine may not call {method}")
        return await self._supervisor.call(method, body)


@dataclass
class AiRpcClients:
    supervisor: PrincipalClient
    research: PrincipalClient

    @classmethod
    def from_sockets(cls, *, supervisor_command: Any, supervisor_query: Any, supervisor_discovery: Any,
                     research_command: Any, research_query: Any, timeout: float) -> "AiRpcClients":
        supervisor = PrincipalClient("ai_supervisor", command=supervisor_command, query=supervisor_query,
                                     slow_query=supervisor_discovery, commands=SUPERVISOR_COMMANDS,
                                     queries=SUPERVISOR_QUERIES, slow_queries=SUPERVISOR_SLOW_QUERIES,
                                     timeout=timeout)
        research = PrincipalClient("ai_research", command=research_command, query=research_query,
                                   commands=RESEARCH_COMMANDS, queries=RESEARCH_QUERIES, timeout=timeout)
        return cls(supervisor, research)

    @classmethod
    def connect(cls, *, keys_dir: Optional[str], address: str, query_port: int, command_port: int,
                timeout: float) -> "AiRpcClients":
        """Load both key pairs (startup only) and open five sockets. ZMQ connects lazily: no trader needed yet."""
        from trader.messaging.typed_rpc import ServiceIdentity, TypedRpcClient

        identities = {p: ServiceIdentity.load(p, keys_dir) for p in ("ai_supervisor", "ai_research")}

        def socket(principal: str, role: str) -> Any:
            port = command_port if role == "command" else query_port
            client = TypedRpcClient(role, identities[principal], server="trader", address=address, port=port,
                                    timeout=timeout)
            client.connect()
            return client
        return cls.from_sockets(
            supervisor_command=socket("ai_supervisor", "command"), supervisor_query=socket("ai_supervisor", "query"),
            supervisor_discovery=socket("ai_supervisor", "query"),
            research_command=socket("ai_research", "command"), research_query=socket("ai_research", "query"),
            timeout=timeout)

    def close(self) -> None:
        self.supervisor.close()
        self.research.close()
