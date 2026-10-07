"""Replay primitives (spec 11): serve recorded model attempts, tool results and clock values.

Replay has no fetch path at all: `tool_result` only reads recorded rows, the replay gateway has
no adapter, and `tripwire` is the stand-in for any live tool (it counts the call, then raises).
`ReplayIncomplete` is an exception because code that needs evidence cannot go on with an
invented value; `ReplaySession.run` / `arun` turn it into an explicit INCOMPLETE result.
"""
from __future__ import annotations
import dataclasses
import hashlib
import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable, Mapping, NoReturn, Optional, Sequence
from trader.ai.clock import Clock
from trader.ai.gateway import CallFailed, DecisionDeadline, GatewayResult
from trader.ai.journal import SUCCEEDED, AttemptJournal, canonical_request_json, request_sha256
from trader.ai.model_client import (
    COST_UNKNOWN, NOT_SENT, REJECTED, MalformedResponseError, ModelCallError, ModelClient, ModelRequest,
    ModelResponse, NotSentError, OutcomeUnknownError, ProviderRejectedError, Usage,
)
from trader.ai.store import AiStore, to_utc


COMPLETE = "COMPLETE"
INCOMPLETE = "INCOMPLETE"
KIND_TOOL = "tool"
KIND_CLOCK = "clock"
KIND_MANIFEST = "manifest"
NO_ARGS_SHA = hashlib.sha256(b"{}").hexdigest()


class ReplayIncomplete(Exception):
    def __init__(self, missing: str):
        super().__init__(f"replay evidence is missing: {missing}")
        self.missing = missing


class ReplayDiverged(ReplayIncomplete):
    """Evidence exists but the replayed request differs from the recorded one."""


class ExternalCallInReplay(Exception):
    pass


@dataclass(frozen=True)
class ReplayResult:
    status: str
    value: Any = None
    missing: tuple[str, ...] = ()


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)


def _args_sha(args: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical(dict(args)).encode("utf-8")).hexdigest()


class ReplayRecorder:
    def __init__(self, store: AiStore):
        self.store = store

    def _insert_in_tx(self, conn: Any, decision_key: str, kind: str, name: str, args_sha: str,
                      payload: Any, now: datetime) -> None:
        ordinal = conn.execute(
            "SELECT COALESCE(MAX(ordinal), 0) FROM ai_replay_evidence "
            "WHERE decision_key = ? AND kind = ? AND name = ? AND args_sha256 = ?",
            [decision_key, kind, name, args_sha]).fetchone()[0] + 1
        conn.execute(
            "INSERT INTO ai_replay_evidence (decision_key, kind, name, args_sha256, ordinal, payload_json, "
            "recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [decision_key, kind, name, args_sha, ordinal, _canonical(payload), now])

    async def record_tool_result(self, decision_key: str, tool: str, args: Mapping[str, Any], result: Any) -> None:
        _canonical(result)  # a NaN raises ValueError and a non-JSON object TypeError before any write
        args_sha = _args_sha(args)
        now = self.store.clock.now()
        await self.store.atransaction(
            lambda conn: self._insert_in_tx(conn, decision_key, KIND_TOOL, tool, args_sha, result, now))

    async def record_clock_values(self, decision_key: str, values: Sequence[datetime]) -> None:
        now = self.store.clock.now()

        def work(conn: Any) -> None:
            for value in values:
                self._insert_in_tx(conn, decision_key, KIND_CLOCK, "now", NO_ARGS_SHA,
                                   to_utc(value).isoformat(), now)

        await self.store.atransaction(work)

    async def record_manifest(self, decision_key: str, *, code_version: str, config_digest: str) -> None:
        now = self.store.clock.now()
        payload = {"code_version": code_version, "config_digest": config_digest}
        await self.store.atransaction(
            lambda conn: self._insert_in_tx(conn, decision_key, KIND_MANIFEST, "versions", NO_ARGS_SHA, payload, now))


class RecordingClock:
    """Wraps a live clock and remembers every `now()` it hands out, for `record_clock_values`."""

    def __init__(self, inner: Clock):
        self._inner = inner
        self.values: list[datetime] = []

    def now(self) -> datetime:
        value = self._inner.now()
        self.values.append(value)
        return value

    def monotonic(self) -> float:
        return self._inner.monotonic()

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


@dataclass(frozen=True)
class RecordedAttempt:
    attempt_key: str
    role: str
    request_sha256: str
    status: str
    response: Optional[ModelResponse]
    error_code: str
    error_detail: str


@dataclass(frozen=True)
class ReplayEvidence:
    decision_key: str
    attempts: Mapping[str, RecordedAttempt]
    tool_results: Mapping[tuple[str, str, int], Any]
    clock_values: tuple[datetime, ...]
    manifest: Optional[Mapping[str, str]]

    @classmethod
    def load(cls, store: AiStore, decision_key: str) -> "ReplayEvidence":
        attempts: dict[str, RecordedAttempt] = {}
        for record in AttemptJournal(store).attempts_with_prefix(f"{decision_key}/"):
            response = None
            if record.status == SUCCEEDED:
                response = ModelResponse(record.response_text or "", Usage(record.input_tokens, record.output_tokens),
                                         record.model, record.backend, record.finish_reason or "",
                                         record.provider_request_id)
            attempts[record.attempt_key] = RecordedAttempt(
                record.attempt_key, record.role, record.request_sha256, record.status, response,
                record.error_code or "", record.error_detail or "")
        tools: dict[tuple[str, str, int], Any] = {}
        clocks: list[datetime] = []
        manifest = None
        rows = store.db.execute(
            "SELECT kind, name, args_sha256, ordinal, payload_json FROM ai_replay_evidence WHERE decision_key = ? "
            "ORDER BY kind, name, args_sha256, ordinal", [decision_key], fetch="all")
        for kind, name, args_sha, ordinal, payload_json in rows:
            payload = json.loads(payload_json)
            if kind == KIND_TOOL:
                tools[(name, args_sha, ordinal)] = payload
            elif kind == KIND_CLOCK:
                clocks.append(datetime.fromisoformat(payload))
            elif kind == KIND_MANIFEST:
                manifest = payload
        return cls(decision_key, attempts, tools, tuple(clocks), manifest)


def _error_for(recorded: RecordedAttempt) -> ModelCallError:
    if recorded.status == NOT_SENT:
        return NotSentError(recorded.error_code, recorded.error_detail)
    if recorded.status == REJECTED:
        return ProviderRejectedError(recorded.error_code, recorded.error_detail)
    if recorded.status == COST_UNKNOWN:
        return MalformedResponseError(recorded.error_code, recorded.error_detail)
    return OutcomeUnknownError(recorded.error_code or "UNKNOWN", recorded.error_detail)  # UNKNOWN, RECONCILED, STARTED


class ReplayModelClient:
    """A ModelClient that serves recorded outcomes by `request.attempt_key`. It has no transport."""

    backend = "replay"
    model_id = "replay"

    def __init__(self, evidence: ReplayEvidence):
        self._evidence = evidence

    async def complete(self, request: ModelRequest, *, timeout_seconds: float) -> ModelResponse:
        if request.attempt_key is None:
            raise ReplayIncomplete("attempt_key_not_set")
        recorded = self._evidence.attempts.get(request.attempt_key)
        if recorded is None:
            raise ReplayIncomplete(f"attempt:{request.attempt_key}")
        if request_sha256(canonical_request_json(request)) != recorded.request_sha256:
            raise ReplayDiverged(f"request_changed:{request.attempt_key}")
        if recorded.response is not None:
            return recorded.response
        raise _error_for(recorded)

    async def aclose(self) -> None:
        return None


class ReplayClock:
    def __init__(self, values: Sequence[datetime]):
        self._values = list(values)
        self._next = 0

    def now(self) -> datetime:
        if self._next >= len(self._values):
            raise ReplayIncomplete(f"clock_value#{self._next + 1}")
        self._next += 1
        return self._values[self._next - 1]

    def monotonic(self) -> float:
        return 0.0

    async def sleep(self, seconds: float) -> None:
        return None


class ReplayGateway:
    """Same `ModelCaller` shape as ModelGateway. No budget, no journal writes, no adapter."""

    def __init__(self, evidence: ReplayEvidence, clock: ReplayClock):
        self._evidence = evidence
        self._client = ReplayModelClient(evidence)
        self._clock = clock
        self._served: Counter[str] = Counter()

    def new_deadline(self, label: str = "") -> DecisionDeadline:
        return DecisionDeadline(self._clock, 1e9, label)

    async def call(self, role: str, request: ModelRequest, deadline: DecisionDeadline) -> GatewayResult:
        number = self._served[request.request_key] + 1
        attempt_key = f"{request.request_key}#{number}"
        recorded = self._evidence.attempts.get(attempt_key)
        if recorded is not None and recorded.role != role:
            raise ReplayDiverged(f"role_changed:{attempt_key}")
        keyed = dataclasses.replace(request, attempt_key=attempt_key)
        try:
            response = await self._client.complete(keyed, timeout_seconds=0.0)
        except ModelCallError as error:
            self._served[request.request_key] = number
            raise CallFailed(error.code, outcome=error.outcome, attempt_key=attempt_key, detail=error.detail) from None
        self._served[request.request_key] = number
        return GatewayResult(response, attempt_key, 0, 0, replayed=True)


class ExternalAdapterCounter:
    """Counts every use of an external adapter, so a test can assert zero."""

    def __init__(self) -> None:
        self._counts: Counter[str] = Counter()

    def record(self, name: str) -> None:
        self._counts[name] += 1

    @property
    def total(self) -> int:
        return sum(self._counts.values())

    def count(self, name: str) -> int:
        return self._counts[name]

    def instrument(self, name: str, client: ModelClient) -> ModelClient:
        return _CountingClient(name, client, self)

    def tripwire(self, name: str) -> Callable[..., NoReturn]:
        """Stand-in for a live tool during replay: counts the call, then fails loudly."""

        def trip(*args: Any, **kwargs: Any) -> NoReturn:
            self.record(name)
            raise ExternalCallInReplay(name)

        return trip


class _CountingClient:
    def __init__(self, name: str, inner: ModelClient, counter: ExternalAdapterCounter):
        self._name, self._inner, self._counter = name, inner, counter
        self.backend, self.model_id = inner.backend, inner.model_id

    async def complete(self, request: ModelRequest, *, timeout_seconds: float) -> ModelResponse:
        self._counter.record(self._name)
        return await self._inner.complete(request, timeout_seconds=timeout_seconds)

    async def aclose(self) -> None:
        await self._inner.aclose()


@dataclass
class ReplaySession:
    evidence: ReplayEvidence
    counter: ExternalAdapterCounter = field(default_factory=ExternalAdapterCounter)

    def __post_init__(self) -> None:
        self._tool_cursor: Counter[tuple[str, str]] = Counter()
        self.clock = ReplayClock(self.evidence.clock_values)
        self.gateway = ReplayGateway(self.evidence, self.clock)

    def tool_result(self, tool: str, args: Mapping[str, Any]) -> Any:
        """The recorded result. There is no fetch path: a missing one is ReplayIncomplete."""
        args_sha = _args_sha(args)
        ordinal = self._tool_cursor[(tool, args_sha)] + 1
        try:
            result = self.evidence.tool_results[(tool, args_sha, ordinal)]
        except KeyError:
            raise ReplayIncomplete(f"tool_result:{tool}#{ordinal}") from None
        self._tool_cursor[(tool, args_sha)] = ordinal
        return result

    def _result(self, value: Any, missing: tuple[str, ...]) -> ReplayResult:
        """A decision with no recorded manifest (code and config versions) is never COMPLETE."""
        if self.evidence.manifest is None:
            missing = missing + ("manifest",)
        if missing:
            return ReplayResult(INCOMPLETE, missing=missing)
        return ReplayResult(COMPLETE, value)

    def run(self, work: Callable[["ReplaySession"], Any]) -> ReplayResult:
        try:
            return self._result(work(self), ())
        except ReplayIncomplete as incomplete:
            return self._result(None, (incomplete.missing,))

    async def arun(self, work: Callable[["ReplaySession"], Awaitable[Any]]) -> ReplayResult:
        try:
            return self._result(await work(self), ())
        except ReplayIncomplete as incomplete:
            return self._result(None, (incomplete.missing,))

    def assert_no_external_calls(self) -> None:
        if self.counter.total:
            raise AssertionError(f"replay made {self.counter.total} external adapter calls")
