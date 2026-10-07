# AI Paper SP2 — Plan 4: ai foundations: config, model client and adapters, store, journal, budget, gateway, replay primitives — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the base layer of the `ai` container as a new package `trader/ai/`: a validated `ai.yaml`, one provider-neutral model client with OpenRouter, Bedrock and Azure adapters, the `ai.duckdb` store, the attempt journal, the durable budget, the single gateway call path with one decision deadline, replay primitives, and the untrusted-input parsers. No trader RPC and no trading logic live here. Plans 5 and 6 build the controller and the decisions on top.

**Architecture:** Every module takes an injectable `Clock`. Money is integer micro-USD. `AiStore` wraps `DuckDBConnection` (one lock per file, `asyncio.to_thread` for all blocking work). `AttemptJournal` and `Budget` expose `*_in_tx(conn, ...)` methods so `ModelGateway` can commit "reserve + journal begin" as one transaction and "journal finish + settle + cost event" as another. Adapters take an injectable transport (an `httpx.AsyncClient` with `httpx.MockTransport`, or a `converse` callable) so tests run the real adapters against fake providers. Replay serves recorded attempts, tool results and clock values and has no way to fetch.

**Tech Stack:** Python 3.12, DuckDB, pydantic v2, `httpx`, `boto3` (new dependency, Bedrock Converse), `pyyaml` (`safe_load`), pytest, pytest-asyncio, hypothesis. No model call, no network and no IB connection in any test.

**Spec:** `docs/superpowers/specs/2026-10-07-ai-paper-sp2ab-autonomous-loop-design.md`. Binding: section 4 (components `ModelClient`, `AttemptJournal`, `Budget`, `ai.duckdb`), 5.4 (Budget), 8 (Untrusted input), 11 (Replay), 12 (Budget, Config, Replay and Durability test parts). Index: `docs/superpowers/plans/2026-10-07-ai-paper-sp2ab-00-index.md` (rulings on model ids, token and rate defaults, prices, Bedrock, migrations 1-9). Base: master after SP1 Plans 3-6.

## Global Constraints

- **Package:** `trader/ai/`. Tests in `tests/ai/`. The package imports only `trader.data.duckdb_store`, `trader.data.schema_migrations`, pydantic, `httpx`, `yaml`, `boto3`/`botocore` (lazy) and the standard library. It must not import `ib_async`, `trader.trading`, `trader.messaging`, `trader.data_providers` or `trader.container` (`tests/ai/test_isolation.py`).
- **Migrations:** `ai.duckdb` migrations 1-5 (Plan 4 holds 1-9; 6-9 stay free). Each table is one plain `CREATE`. There is no legacy data: no `ALTER`, no backfill, no compatibility step.
- **Money:** integer micro-USD (`*_micros`). Costs round up, the configured cap rounds down. Never `float` for money inside budget, journal or gateway.
- **Window:** `America/New_York` calendar date, computed in Python from the injected clock with `zoneinfo`, stored as `VARCHAR`. Never read a date out of a DuckDB timestamp (DuckDB returns `TIMESTAMPTZ` in the process time zone); compare instants only as aware datetimes through `to_utc`.
- **Plan text and tests.** Subtle code (budget, gateway, journal, replay core, parsers) is given in full. Mechanical code is described exactly. Tests that pin a rule are given in full; the others are listed by name and each asserts what its name says. Every task's "Expected" test count assumes all listed tests are written.
- **No AI principal changes the cap.** `Budget.set_cap` is called only at startup from `ai.yaml` and by a future operator command. Plan 5 must not route it through any `ai_supervisor` or `ai_research` path (spec 5.4).
- **Defaults (index):** `max_output_tokens` 4000, `max_input_tokens` 60000, `calls_per_hour` 120, `max_in_flight` 2 (never above 2), `decision_deadline_seconds` 60, daily cap 2000 USD. No default model ids anywhere in code.
- **Credentials:** only from the environment of the `ai` container (`OPENROUTER_API_KEY`; `AWS_REGION` plus the standard AWS chain; `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_API_VERSION`). They never appear in `ai.yaml`, an exception message, a repr, a log line or any `ai.duckdb` column (`test_credentials_never_reach_the_database`, `test_credentials_never_appear_in_errors_or_repr`).
- **Config:** `yaml.safe_load` only. Unknown keys are errors and error text never echoes a file value. No environment override of the file. Missing, unsupported or incompatible config fails at startup with an `AiConfigError(code, message)`; there is no fallback model, backend or price.
- **Usage parsing is strict:** token counts must be plain `int` (not `bool`, `float` or text), input at least 1, output at least 0. Anything else is `MalformedUsageError`, which keeps the full reservation. It is never zero cost.
- **Wire and output models:** strict pydantic (`extra="forbid"`, `strict=True`); model output is validated with `model_validate_json`, not on a dict.
- **DuckDB** only through `DuckDBConnection.execute` / `transaction`. No long-lived connection.
- Per-task tests: `.venv/bin/python -m pytest <file> -q --timeout=60`. Full suite once, in Task 10: `.venv/bin/python -m pytest tests/ -q --timeout=60 --ignore=tests/test_ibrx_async.py`. Every async test carries `@pytest.mark.asyncio` (the repo does not set `asyncio_mode`); async fixtures use `pytest_asyncio.fixture`.
- Commit subjects `feat:` / `test:` lowercase, imperative. Every commit message ends with a blank line and `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- No container, deploy, push or real provider call is authorized by this plan.

## Rulings (spec silent, or the code forces a choice)

1. **Cap source (OWNER TO CONFIRM).** Spec 5.4 names `ai_paper.model_budget_usd_per_day` in `trader.yaml` or an operator `cli` command. The `ai` container does not read `trader.yaml` and Plan 4 has no trader RPC. So Plan 4 reads the cap from `ai.yaml` key `budget.model_budget_usd_per_day` (default 2000) and calls `Budget.set_cap(micros)` at every start. `set_cap` is also the single entry point a later operator path (Plan 5 or SP2e) can call. *If wrong:* the owner edits `ai.yaml` instead of `trader.yaml`; no data or code is lost, only the place of the setting changes.
2. **Windows and reservations.** A reservation belongs to the New York date in which it was made, for life. A call that crosses midnight settles in its own window, and an UNKNOWN reservation stays in its window; it does not carry into the next day. *If wrong:* an unknown call from yesterday would have lowered today's cap; the spec text ("never reset accounting twice or erase a reservation") is met either way.
3. **Four failure outcomes.** `NOT_SENT` and `REJECTED` (HTTP 4xx except 408; Bedrock `Validation`, `AccessDenied`, `ResourceNotFound`, `Throttling`, `ServiceQuotaExceeded`) release the reservation; `UNKNOWN` (read timeout, 5xx, 408, dropped connection, caller cancel, restart) and `COST_UNKNOWN` (a response arrived but the usage is bad or the body is unusable) keep it at the worst case. *If wrong:* a provider that bills a 4xx would be under-counted by that call's cost.
4. **Late usage.** `ModelGateway.report_late_usage(attempt_key, usage)` settles an UNKNOWN or COST_UNKNOWN reservation once, at the current `ai.yaml` price, and writes a `CORRECTION` cost event. Who calls it (an operator tool, a provider usage lookup) is Plan 5 or later. *If wrong:* unknown calls stay counted at the worst case until a caller exists; the budget is then too cautious, never too loose.
5. **In-flight slot before reservation.** The gateway waits for a slot first, then reserves, so a queued call never holds budget. The hourly-limit wait releases the slot. *If wrong:* a queued call would hold budget for up to the decision deadline; no overspend, only earlier refusals.
6. **Hourly limit** is a sliding hour over reservation creation times, excluding calls proven not sent. If the wait fits in the decision deadline the gateway sleeps through the injected clock; if not it refuses with `HOURLY_LIMIT_EXCEEDS_DEADLINE` and `retry_at` (about an hour at most, never the daily reset). *If wrong:* a call may be refused that a longer sleep would have served; the decision deadline already expires such work.
7. **Input limit.** The gateway estimates input tokens as one per 3 UTF-8 bytes plus 4 per message and refuses above `max_input_tokens` (`INPUT_TOO_LARGE`). The reservation always uses the role limits, as the index says. A provider that reports more tokens than the limits is settled at the real figure (`overrun=True`), not hidden. *If wrong:* the byte estimate is conservative for English, so it may refuse a very long non-English prompt that would have fit; the owner raises `max_input_tokens`.
8. **Missing price is a call-time refusal, not a startup error.** The service can start (and reconcile) with an unpriced model; every call to it is `CallRefused("PRICE_UNAVAILABLE")` and writes nothing. An explicit price of `0` is allowed (free models); a negative, NaN or non-number price is a startup error. *If wrong:* the owner wants a hard stop at startup; one more check in `_check`.
9. **`ReplayIncomplete` is an exception**, caught by `ReplaySession.run` / `arun` into a `ReplayResult(status="INCOMPLETE", missing=...)`. Reason: any code that needs the evidence must not be able to continue with an invented value. *If wrong:* a caller that prefers a returned value wraps the call in `run`; no data is affected.
10. **Request and attempt keys.** The caller builds `request_key = "<decision_id>/<role>/<call_seq>"` (no `#`). The journal assigns `attempt_key = "<request_key>#<n>"`. Replay finds a decision's evidence by the prefix `"<decision_id>/"`. Plans 5 and 6 must follow this. *If wrong:* replay cannot find a decision's attempts; the fix is a prefix change in `ReplayEvidence.load`.
11. **Restart recovery** assumes one live `ai` process (the leader). `start()` turns every `STARTED` attempt and `OPEN` reservation into `UNKNOWN`. It must run before any call and never while calls are in flight. *If wrong:* two live processes on one file would mark each other's calls UNKNOWN; Plan 5's epoch lease prevents a second leader.
12. **Bedrock** runs the synchronous Converse call in a worker thread; the boto3 client has `max_attempts: 1` and a read timeout equal to the role timeout. **Azure** sends `max_completion_tokens` (current API versions). A wrong `api_version` shows up as a loud 400, not a silent change. *If wrong:* older Azure API versions reject `max_completion_tokens`; one line in `AzureOpenAIAdapter._body`.
13. **Config extras.** `database_path` (default `~/.local/share/mmr_ai/ai.duckdb`; Plan 5 points it at the `mmr_ai_data` volume), role `call_timeout_seconds` (default 45, must not exceed the decision deadline), `max_in_flight` above 2 is refused. `check_credentials` names missing variables only. *If wrong:* a different default path or timeout is a one-line config change.
14. **First-run copy.** `config_defaults/*.yaml` is copied by glob (`Dockerfile`, `docker.sh`, `start_mmr.sh`, `scripts/docker-entrypoint.sh`), so the new `ai.yaml` is copied with no script change. It ships with blank model ids, so a fresh `ai` service refuses to start until the owner fills them in. *If wrong:* an owner who wants a runnable default must pick the model ids first (index ruling: no default ids).

## Cross-plan additions

Everything Plans 5 and 6 import from this plan. These are pinned by `tests/ai/test_public_api.py`.

**Conventions**
- `request_key = f"{decision_id}/{role}/{call_seq}"`; `attempt_key = f"{request_key}#{n}"` (journal-assigned, n starts at 1).
- Roles are `"orchestrator"` and `"jev"` (`trader.ai.config.ROLE_NAMES`).
- Money is integer micro-USD; `micros_to_usd_str(micros) -> str` gives `"0.240000"` for the trader's cost ingestion.
- Call failures never authorize a submit: on `CallFailed` or `CallRefused` the caller abandons the action and journals it before any trader submission.

**`trader.ai.clock`:** `Clock` protocol (`now() -> datetime` aware UTC, `monotonic() -> float`, `async sleep(seconds)`), `SystemClock()`.

**`trader.ai.config`:** `load_ai_config(path: str = "~/.config/mmr/ai.yaml") -> AiConfig`; `AiConfig(roles, prices, budget, database_path)` with `.role(name) -> RoleConfig`, `.digest() -> str`; `RoleConfig(backend, model, max_input_tokens, max_output_tokens, call_timeout_seconds)`; `BudgetConfig(model_budget_usd_per_day, calls_per_hour, max_in_flight, decision_deadline_seconds)`; `PriceBook.price_for(backend, model) -> Optional[ModelPrice]`; `ModelPrice.cost_micros(input_tokens, output_tokens) -> int`; `AiConfigError(code, message)`; `check_credentials(config, environ, *, aws_credentials_present=...) -> None`; `usd_to_micros_floor(usd) -> int`; `micros_to_usd_str(micros) -> str`.

**`trader.ai.model_client`:** `ChatMessage(role, content)`; `ModelRequest(request_key, messages, max_output_tokens, temperature=0.0, attempt_key=None)`; `Usage(input_tokens, output_tokens)`; `ModelResponse(text, usage, model, backend, finish_reason, provider_request_id)`; `ModelClient` protocol; errors `ModelCallError`, `NotSentError`, `ProviderRejectedError`, `OutcomeUnknownError`, `MalformedResponseError`, `MalformedUsageError` (each `.code`, `.detail`, `.outcome`); `OpenRouterAdapter`, `AzureOpenAIAdapter`, `BedrockAdapter`; `build_model_client(role, *, environ, http_client=None, bedrock_converse=None)`; `estimate_input_tokens(messages) -> int`.

**`trader.ai.store` / `trader.ai.schema`:** `AiStore(path, *, clock)` with `.db`, `.migrate(migrations=FOUNDATION_MIGRATIONS) -> list[int]`, `.transaction(work)`, `async .atransaction(work)`, `async .aquery(sql, params=None, *, fetch="all")`; `Migration(version: int, name: str, statements: tuple[str, ...])`; `to_utc(datetime)`. Plan 5 passes `Migration`s numbered 10-19, Plan 6 20-29.

**`trader.ai.journal`:** `AttemptJournal(store)`; `AttemptRecord` (fields `attempt_key, request_key, attempt_no, role, backend, model, status, reservation_id, request_json, request_sha256, response_text, finish_reason, provider_request_id, input_tokens, output_tokens, error_code, error_detail, started_at, finished_at`); statuses `STARTED | SUCCEEDED | NOT_SENT | REJECTED | UNKNOWN | COST_UNKNOWN | RECONCILED`; `get(attempt_key)`, `async aget(attempt_key)`, `attempts_with_prefix(prefix)`, `unknown_attempts()`; **for the reporting outbox:** `async acost_events_after(event_seq: int, limit: int = 100) -> list[CostEvent]` where `CostEvent(event_seq, event_id, attempt_key, role, backend, model, kind, cost_micros, input_tokens, output_tokens, occurred_at)` and `kind` is `CONFIRMED | ESTIMATED_UNKNOWN | NONE | CORRECTION`. `event_id = f"{attempt_key}:{kind}"` is stable and unique: use it as the idempotency key for `record_ai_cost`. Cursors tolerate gaps.

**`trader.ai.budget`:** `Budget(store, clock, *, calls_per_hour)` with `async set_cap(requested_micros) -> str`, `async snapshot() -> BudgetSnapshot(window_date, effective_cap_micros, pending_cap_micros, pending_window_date, committed_micros, open_reservations, unknown_reservations, next_reset_at)`; `window_date(instant) -> str`, `next_window_start(instant) -> datetime`; `BudgetExhausted`, `HourlyLimitReached`, `BudgetConflict`.

**`trader.ai.gateway`:** `ModelGateway(*, config, store, clock, clients, budget=None, journal=None)` with `async start()`, `async recover_after_restart()`, `new_deadline(label: str = "") -> DecisionDeadline`, `async call(role: str, request: ModelRequest, deadline: DecisionDeadline) -> GatewayResult`, `async report_late_usage(attempt_key: str, usage: Usage) -> bool`, and attributes `.budget`, `.journal`; `build_gateway(config, *, store, clock, environ, clients=None) -> ModelGateway`; `DecisionDeadline(clock, seconds, label="")` (`.remaining() -> float`, `.expired`); `GatewayResult(response, attempt_key, cost_micros, reserved_micros, overrun=False, replayed=False)`; `CallRefused(code, detail="", *, retry_at=None)` with codes `ROLE_UNKNOWN | OUTPUT_LIMIT_ABOVE_ROLE | INPUT_TOO_LARGE | PRICE_UNAVAILABLE | DEADLINE_EXPIRED | IN_FLIGHT_LIMIT_DEADLINE | HOURLY_LIMIT_EXCEEDS_DEADLINE | BUDGET_EXHAUSTED | ATTEMPT_UNKNOWN`; `CallFailed(code, *, outcome, attempt_key, detail="")` with `.outcome_unknown`; `ModelCaller` protocol (`new_deadline`, `async call`). Plans 5 and 6 take a `ModelCaller`, so replay swaps in `ReplayGateway`.

**`trader.ai.replay`:** `ReplayRecorder(store)` (`async record_tool_result(decision_key, tool, args, result)`, `async record_clock_values(decision_key, values)`, `async record_manifest(decision_key, *, code_version, config_digest)`); `RecordingClock(inner)` (`.values`); `ReplayEvidence.load(store, decision_key)`; `ReplaySession(evidence, counter=None)` (`.clock`, `.gateway`, `.counter`, `.tool_result(tool, args)`, `.run(work)`, `async .arun(work)`, `.assert_no_external_calls()`); `ReplayResult(status, value, missing)` with `COMPLETE` / `INCOMPLETE`; `ReplayIncomplete(missing)`, `ReplayDiverged`, `ExternalCallInReplay`; `ExternalAdapterCounter` (`.total`, `.count(name)`, `.instrument(name, client)`, `.tripwire(name)`); `ReplayModelClient`, `ReplayGateway`.

**`trader.ai.untrusted`:** `StrictModelOutput` (base for every model-filled schema; refuses code-owned field names), `parse_model_output(text, schema) -> ParsedOutput[T] | OutputRefusal`, `ParsedOutput(value)`, `OutputRefusal(code, detail)` with codes `OUTPUT_EMPTY | OUTPUT_TOO_LARGE | OUTPUT_NO_JSON | OUTPUT_MULTIPLE_JSON | OUTPUT_BAD_JSON | OUTPUT_DUPLICATE_KEY | OUTPUT_NOT_OBJECT | OUTPUT_TOO_DEEP | OUTPUT_SCHEMA_VIOLATION`, `fence_untrusted(label, text, *, max_chars) -> str`, `RESERVED_FIELD_NAMES`.

## Review Focus

1. **Concurrent reservations across roles.** Many parallel `reserve` calls from Jev and the orchestrator must never pass the shared cap. → Task 6 `test_concurrent_reservations_across_roles_cannot_exceed_the_cap`.
2. **A response that arrives with bad usage.** Must keep the full worst-case reservation, journal `COST_UNKNOWN`, and never settle at zero. → Task 7 `test_malformed_usage_keeps_the_full_reservation`; Task 2 `test_malformed_usage_is_an_error_never_zero_cost`.
3. **A call that is cancelled, times out or dies with the process.** Must end `UNKNOWN`, free the in-flight slot, keep its reservation, and survive a restart as a counted unknown. → Task 7 `test_cancelling_a_call_records_unknown_and_keeps_the_reservation`, `test_timeout_is_unknown_keeps_the_reservation_and_a_late_report_reconciles_once`, `test_restart_turns_a_half_finished_call_into_a_counted_unknown`.
4. **A raised cap across a restart and across New York midnight, DST and process time zone.** A restart must not apply a raise early or move its date; a midnight-spanning call stays in its window; the 25-hour day does not reset twice; stored instants compare the same under any `TZ`. → Task 6 `test_restart_never_applies_a_raise_early_or_moves_its_date`, `test_restart_after_midnight_applies_the_pending_raise_before_comparing`, `test_a_call_spanning_midnight_stays_in_its_own_window`, `test_a_25_hour_day_does_not_reset_twice`; Task 4 `test_stored_instants_compare_the_same_under_any_process_time_zone`.
5. **Replay must never fetch.** With the network blocked, a full replay makes zero adapter invocations (asserted by the counter, with a positive control), and missing evidence is an explicit incomplete result. → Task 8 `test_replay_reproduces_the_decision_with_zero_external_calls`, `test_the_counter_really_counts_a_live_adapter`, `test_missing_evidence_is_an_incomplete_result_and_never_a_fetch`.

## Coverage map

| Spec part | Where |
|---|---|
| 4 `ModelClient`, three adapters, Jev pinned to openrouter, fail-loud config | Tasks 1, 2, 3 |
| 4 `AttemptJournal` (before / after, unknown stays unknown, no credentials) | Task 5 |
| 4 `ai.duckdb` (serialized writes, work off the event loop) | Task 4 |
| 5.4 effective cap, raise at midnight, worst-case reservation, restart, DST, unknown, missing price, hourly delay, 2 in flight, one deadline | Tasks 6, 7 |
| 8 untrusted input | Task 9 |
| 11 replay: zero adapter calls, incomplete result | Task 8 |
| 12 Budget, Config, Replay test parts | tests in Tasks 1, 6, 7, 8 |

## File map

| File | Responsibility | Task |
|---|---|---|
| `trader/ai/clock.py`, `config.py`, `config_defaults/ai.yaml` | clock, `ai.yaml` loader, prices, money helpers | 1 |
| `trader/ai/model_client.py` | types, errors, strict usage, three adapters, `build_model_client` | 2, 3 |
| `pyproject.toml`, `uv.lock` | `boto3` | 3 |
| `trader/ai/schema.py`, `store.py` | migrations 1-5, `AiStore` | 4 |
| `trader/ai/journal.py` | attempts and cost events | 5 |
| `trader/ai/budget.py` | cap, windows, reservations, hourly limit | 6 |
| `trader/ai/gateway.py` | call path, deadline, slots, late usage | 7 |
| `trader/ai/replay.py` | recorder, evidence, replay client and gateway, counter | 8 |
| `trader/ai/untrusted.py` | strict output parsing, fencing | 9 |
| `tests/ai/*` | all tests; `fakes.py`, `world.py`, `conftest.py` are shared helpers | all |

---

### Task 1: package skeleton, clock, `ai.yaml` config and template

**Files:**
- Create: `trader/ai/__init__.py`, `trader/ai/clock.py`, `trader/ai/config.py`, `config_defaults/ai.yaml`
- Create (tests): `tests/ai/__init__.py`, `tests/ai/fakes.py`, `tests/ai/test_config.py`

**Interfaces:**
- Consumes: `yaml.safe_load`, pydantic v2.
- Produces: `Clock` / `SystemClock`; `AiConfig`, `RoleConfig`, `BudgetConfig`, `ModelPrice`, `PriceBook`, `AiConfigError(code, message)`, `ROLE_NAMES`, `load_ai_config(path=DEFAULT_CONFIG_PATH) -> AiConfig`, `check_credentials(config, environ, *, aws_credentials_present=...) -> None`, `usd_to_micros_floor`, `micros_to_usd_str`; test helpers `FakeClock`, `config_text(...)`, `write_config`, `load_test_config`.

The shipped template has blank model ids on purpose: the `ai` service must refuse to start until the owner fills them in. `config_defaults/*.yaml` is copied to `~/.config/mmr/` by `Dockerfile`, `docker.sh`, `start_mmr.sh` and `scripts/docker-entrypoint.sh` with a glob, so `ai.yaml` needs no script change. Nothing in the trader, strategy or data services reads it.

- [ ] **Step 1: Write the failing tests.** Create an empty `tests/ai/__init__.py`, then:

`tests/ai/fakes.py` (the `FakeProvider` part is added in Task 7):

```python
"""Shared test doubles for the ai package."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

from trader.ai.config import AiConfig, load_ai_config

UTC = timezone.utc


class FakeClock:
    def __init__(self, start: datetime):
        self._now = start
        self._mono = 1000.0

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._mono

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)
        self._mono += seconds

    async def sleep(self, seconds: float) -> None:
        self.advance(seconds)
        await asyncio.sleep(0)


def config_text(
    *,
    cap: str = "2000",
    calls_per_hour: int = 120,
    deadline: int = 60,
    max_in_flight: int = 2,
    orchestrator_backend: str = "openrouter",
    orchestrator_model: str = "vendor/orch-1",
    jev_backend: str = "openrouter",
    jev_model: str = "vendor/jev-1",
    call_timeout: str = "45",
    extra_top_level: str = "",
) -> str:
    return f"""
roles:
  orchestrator: {{backend: {orchestrator_backend}, model: "{orchestrator_model}", call_timeout_seconds: {call_timeout}}}
  jev: {{backend: {jev_backend}, model: "{jev_model}", call_timeout_seconds: {call_timeout}}}
pricing:
  openrouter:
    "vendor/orch-1": {{input_usd_per_million: 3.0, output_usd_per_million: 15.0}}
    "vendor/jev-1": {{input_usd_per_million: 1.0, output_usd_per_million: 5.0}}
budget:
  model_budget_usd_per_day: {cap}
  calls_per_hour: {calls_per_hour}
  max_in_flight: {max_in_flight}
  decision_deadline_seconds: {deadline}
{extra_top_level}
"""


def write_config(tmp_path: Path, text: str | None = None) -> Path:
    path = tmp_path / "ai.yaml"
    path.write_text(text if text is not None else config_text())
    return path


def load_test_config(tmp_path: Path, **kwargs) -> AiConfig:
    return load_ai_config(str(write_config(tmp_path, config_text(**kwargs))))


# 240_000 micro-USD: 60000 input tokens at $3/M plus 4000 output tokens at $15/M.
ORCHESTRATOR_WORST_CASE_MICROS = 240_000
# 80_000 micro-USD: 60000 at $1/M plus 4000 at $5/M.
JEV_WORST_CASE_MICROS = 80_000
```

`tests/ai/test_config.py`:

```python
from decimal import Decimal
from pathlib import Path
import pytest
from tests.ai.fakes import config_text, load_test_config, write_config
from trader.ai.config import (
    AiConfigError,
    check_credentials,
    load_ai_config,
    micros_to_usd_str,
    usd_to_micros_floor,
)


TEMPLATE = Path(__file__).resolve().parents[2] / "config_defaults" / "ai.yaml"


def refused(tmp_path, text: str) -> AiConfigError:
    with pytest.raises(AiConfigError) as caught:
        load_ai_config(str(write_config(tmp_path, text)))
    return caught.value


def test_shipped_template_refuses_to_start_without_model_ids():
    with pytest.raises(AiConfigError) as caught:
        load_ai_config(str(TEMPLATE))
    assert caught.value.code == "ROLE_MODEL_MISSING"


def test_jev_must_use_openrouter(tmp_path):
    assert refused(tmp_path, config_text(jev_backend="azure")).code == "JEV_BACKEND_NOT_OPENROUTER"
```

**Also write these tests** (each asserts what its name says; the rules above and the helper functions in the file are all they need): `test_valid_config_loads_with_spec_defaults`, `test_missing_file_is_a_loud_error`, `test_orchestrator_may_use_bedrock_or_azure`, `test_unsupported_backend_is_refused`, `test_blank_model_is_refused_and_names_the_role`, `test_max_in_flight_above_two_is_refused`, `test_unknown_key_is_refused_and_the_value_is_not_echoed`, `test_budget_must_be_a_real_non_negative_number`, `test_integer_cap_is_accepted_as_a_float`, `test_missing_price_is_not_a_load_error_but_the_price_is_absent`, `test_cost_rounds_up_to_whole_micro_usd`, `test_money_helpers`, `test_digest_changes_when_a_price_changes`, `test_credentials_are_checked_by_name_only`, `test_azure_and_bedrock_credentials`.

- [ ] **Step 2: Run them and see them fail.**
```bash
.venv/bin/python -m pytest tests/ai/test_config.py -q --timeout=60
```
Expected: FAIL, `ModuleNotFoundError: No module named 'trader.ai'`.

- [ ] **Step 3: Implement.**

`trader/ai/__init__.py`:

```python
"""Model plumbing for the AI paper bot (SP2a/b). No trader RPC lives here."""
```

`trader/ai/clock.py`:

```python
"""One injectable clock. Every ai module takes it; tests use a fake."""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """Timezone-aware UTC instant."""

    def monotonic(self) -> float:
        """Seconds on a clock that never goes backwards."""

    async def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def monotonic(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(max(0.0, seconds))
```

`trader/ai/config.py`:

```python
from __future__ import annotations
import hashlib
import json
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Annotated, Callable, Mapping, Optional
import yaml
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, StrictStr, ValidationError


SUPPORTED_BACKENDS = ("openrouter", "bedrock", "azure")
ROLE_NAMES = ("orchestrator", "jev")
MAX_IN_FLIGHT_LIMIT = 2
MICROS_PER_USD = 1_000_000
DEFAULT_CONFIG_PATH = "~/.config/mmr/ai.yaml"


class AiConfigError(ValueError):
    """A startup problem. The message never contains a value read from the file."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def _as_int(value: object) -> int:
    if type(value) is not int:
        raise ValueError("must be an integer")
    return value


def _as_number(value: object) -> float:
    if type(value) not in (int, float):
        raise ValueError("must be a number")
    return float(value)


Whole = Annotated[int, BeforeValidator(_as_int)]
Number = Annotated[float, BeforeValidator(_as_number)]


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, protected_namespaces=())


class RoleConfig(_Section):
    backend: StrictStr = ""
    model: StrictStr = ""
    max_input_tokens: Whole = Field(60000, gt=0, le=2_000_000)
    max_output_tokens: Whole = Field(4000, gt=0, le=200_000)
    call_timeout_seconds: Number = Field(45.0, gt=0, le=600, allow_inf_nan=False)


class BudgetConfig(_Section):
    model_budget_usd_per_day: Number = Field(2000.0, ge=0, allow_inf_nan=False)
    calls_per_hour: Whole = Field(120, gt=0)
    max_in_flight: Whole = Field(MAX_IN_FLIGHT_LIMIT, gt=0)
    decision_deadline_seconds: Number = Field(60.0, gt=0, le=600, allow_inf_nan=False)


class _PriceRow(_Section):
    input_usd_per_million: Number = Field(ge=0, allow_inf_nan=False)
    output_usd_per_million: Number = Field(ge=0, allow_inf_nan=False)


class _RawConfig(_Section):
    roles: dict[StrictStr, RoleConfig]
    pricing: dict[StrictStr, dict[StrictStr, _PriceRow]] = Field(default_factory=dict)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    database_path: StrictStr = "~/.local/share/mmr_ai/ai.duckdb"


@dataclass(frozen=True)
class ModelPrice:
    input_usd_per_million: Decimal
    output_usd_per_million: Decimal

    def cost_micros(self, input_tokens: int, output_tokens: int) -> int:
        """USD per million tokens equals micro-USD per token. Always rounds up."""
        total = Decimal(input_tokens) * self.input_usd_per_million
        total += Decimal(output_tokens) * self.output_usd_per_million
        return int(total.to_integral_value(rounding=ROUND_CEILING))


@dataclass(frozen=True)
class PriceBook:
    rows: Mapping[tuple[str, str], ModelPrice]

    def price_for(self, backend: str, model: str) -> Optional[ModelPrice]:
        return self.rows.get((backend, model))


@dataclass(frozen=True)
class AiConfig:
    roles: Mapping[str, RoleConfig]
    prices: PriceBook
    budget: BudgetConfig
    database_path: str

    def role(self, name: str) -> RoleConfig:
        try:
            return self.roles[name]
        except KeyError:
            raise AiConfigError("ROLE_UNKNOWN", f"unknown role {name!r}") from None

    def digest(self) -> str:
        """Stable hash of everything that is not a secret. Replay records it."""
        body = {
            "roles": {name: role.model_dump() for name, role in sorted(self.roles.items())},
            "prices": {
                f"{backend}/{model}": [str(p.input_usd_per_million), str(p.output_usd_per_million)]
                for (backend, model), p in sorted(self.prices.rows.items())
            },
            "budget": self.budget.model_dump(),
        }
        return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


def load_ai_config(path: str = DEFAULT_CONFIG_PATH) -> AiConfig:
    file = Path(path).expanduser()
    if not file.is_file():
        raise AiConfigError("AI_CONFIG_NOT_FOUND", f"{file} does not exist")
    try:
        raw = yaml.safe_load(file.read_text())
    except yaml.YAMLError as exc:
        raise AiConfigError("AI_CONFIG_INVALID", f"not valid YAML ({type(exc).__name__})") from None
    if not isinstance(raw, dict):
        raise AiConfigError("AI_CONFIG_INVALID", "the file must be a mapping")
    try:
        parsed = _RawConfig.model_validate(raw)
    except ValidationError as exc:
        where = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['type']}" for e in exc.errors())
        raise AiConfigError("AI_CONFIG_INVALID", where) from None
    return _check(parsed)


def _check(parsed: _RawConfig) -> AiConfig:
    for name in ROLE_NAMES:
        if name not in parsed.roles:
            raise AiConfigError("ROLE_MISSING", f"role {name!r} is not configured")
    for name, role in parsed.roles.items():
        if name not in ROLE_NAMES:
            raise AiConfigError("ROLE_UNKNOWN", f"unknown role {name!r}")
        if not role.model.strip():
            raise AiConfigError("ROLE_MODEL_MISSING", f"role {name!r} has no model id")
        if role.backend not in SUPPORTED_BACKENDS:
            raise AiConfigError(
                "ROLE_BACKEND_UNSUPPORTED",
                f"role {name!r}: backend must be one of {', '.join(SUPPORTED_BACKENDS)}",
            )
        if role.call_timeout_seconds > parsed.budget.decision_deadline_seconds:
            raise AiConfigError("CALL_TIMEOUT_ABOVE_DEADLINE", f"role {name!r} call timeout is above the decision deadline")
    if parsed.roles["jev"].backend != "openrouter":
        raise AiConfigError("JEV_BACKEND_NOT_OPENROUTER", "Jev runs on openrouter only")
    if parsed.budget.max_in_flight > MAX_IN_FLIGHT_LIMIT:
        raise AiConfigError("MAX_IN_FLIGHT_ABOVE_LIMIT", f"max_in_flight may not exceed {MAX_IN_FLIGHT_LIMIT}")
    rows: dict[tuple[str, str], ModelPrice] = {}
    for backend, models in parsed.pricing.items():
        if backend not in SUPPORTED_BACKENDS:
            raise AiConfigError("PRICING_BACKEND_UNSUPPORTED", f"pricing names an unsupported backend {backend!r}")
        for model, row in models.items():
            rows[(backend, model)] = ModelPrice(
                Decimal(repr(row.input_usd_per_million)), Decimal(repr(row.output_usd_per_million))
            )
    return AiConfig(dict(parsed.roles), PriceBook(rows), parsed.budget, parsed.database_path)
```

**Not shown, write exactly as described** (small and mechanical):
- `usd_to_micros_floor(usd: float | Decimal) -> int`: `int((Decimal(str(usd)) * MICROS_PER_USD).to_integral_value(rounding=ROUND_FLOOR))`.
- `micros_to_usd_str(micros: int) -> str`: `f"{Decimal(micros) / MICROS_PER_USD:.6f}"`.
- `_aws_chain_has_credentials() -> bool`: imports `boto3` inside the function and returns `boto3.Session().get_credentials() is not None`.
- `check_credentials(config, environ, *, aws_credentials_present=_aws_chain_has_credentials) -> None`: for each distinct backend used by the roles, collect what is missing: openrouter `OPENROUTER_API_KEY`; azure `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_API_VERSION`; bedrock `AWS_REGION` (or `AWS_DEFAULT_REGION`) and, when `aws_credentials_present()` is false, the text `AWS credentials (AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY or AWS_PROFILE)`. If anything is missing raise `AiConfigError("CREDENTIALS_MISSING", "missing: " + ", ".join(missing))`. Names only, never values.

`config_defaults/ai.yaml`:

```yaml
# AI paper bot (SP2a/b): models, prices and limits. Read only by the `ai` container.
# Credentials are NOT here. They come from the environment of the `ai` container:
#   openrouter: OPENROUTER_API_KEY
#   bedrock:    AWS_REGION + the standard AWS chain (keys or AWS_PROFILE)
#   azure:      AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_API_KEY, AZURE_OPENAI_API_VERSION
#
# There are no default model ids. An empty `model` stops the service at startup.
# The owner picks them. Example ids (not defaults): "anthropic/claude-sonnet-4.5" on
# openrouter, an inference-profile id on bedrock, a deployment name on azure.
roles:
  orchestrator:
    backend: openrouter        # openrouter | bedrock | azure
    model: ""
    max_input_tokens: 60000
    max_output_tokens: 4000
    call_timeout_seconds: 45
  jev:
    backend: openrouter        # Jev must stay on openrouter
    model: ""
    max_input_tokens: 60000
    max_output_tokens: 4000
    call_timeout_seconds: 45

# USD per million tokens, per backend and model id. A model with no row here is
# refused at call time (nothing is reserved).
pricing:
  openrouter: {}
  bedrock: {}
  azure: {}
  # openrouter:
  #   "vendor/model-id": {input_usd_per_million: 3.0, output_usd_per_million: 15.0}

budget:
  model_budget_usd_per_day: 2000   # raising applies at the next 00:00 America/New_York
  calls_per_hour: 120
  max_in_flight: 2                 # 2 is the maximum
  decision_deadline_seconds: 60    # one deadline spans orchestrator plus Jev

database_path: ~/.local/share/mmr_ai/ai.duckdb
```

- [ ] **Step 4: Run and see them pass.**
```bash
.venv/bin/python -m pytest tests/ai/test_config.py -q --timeout=60
```
Expected: 20 passed.

- [ ] **Step 5: Commit.**
```bash
git add trader/ai config_defaults/ai.yaml tests/ai
git commit -m "feat: add the ai package with its config loader and clock" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```


---

### Task 2: model client types, error taxonomy, strict usage and the OpenRouter adapter

**Files:**
- Create: `trader/ai/model_client.py`
- Create (tests): `tests/ai/test_openrouter_adapter.py`

**Interfaces:**
- Consumes: `trader.ai.config` (`AiConfigError`, `RoleConfig`), `httpx`.
- Produces: `ChatMessage(role, content)`, `ModelRequest(request_key, messages, max_output_tokens, temperature=0.0, attempt_key=None)`, `Usage(input_tokens, output_tokens)`, `ModelResponse(text, usage, model, backend, finish_reason, provider_request_id)`, the `ModelClient` protocol (`backend`, `model_id`, `async complete(request, *, timeout_seconds) -> ModelResponse`, `async aclose()`), errors `ModelCallError` > `NotSentError` / `ProviderRejectedError` / `OutcomeUnknownError` > `MalformedResponseError` > `MalformedUsageError` (each has `.code`, `.detail`, `.outcome` in `NOT_SENT | REJECTED | UNKNOWN | COST_UNKNOWN`), `parse_usage`, `redact`, `estimate_input_tokens`, `OpenRouterAdapter(model_id, api_key, http_client, base_url=...)`.

How the error classes split (the journal and budget rely on this):
- `NotSentError`: only when the request provably never left the process (connect failure, bad local parameters). The reservation is released.
- `ProviderRejectedError`: a definite refusal (HTTP 4xx except 408). No tokens were made. The reservation is released.
- `OutcomeUnknownError`: anything else after the transport started (read timeout, 5xx, dropped connection). The reservation stays counted.
- `MalformedResponseError` / `MalformedUsageError`: a response arrived but it cannot be used or priced. Outcome `COST_UNKNOWN`. The reservation stays counted at the worst case. Malformed usage is never zero cost.

The `attempt_key` field is set by the gateway only. Adapters never read it; replay does.

- [ ] **Step 1: Write the failing tests.** `tests/ai/test_openrouter_adapter.py`:

```python
import httpx
import pytest
from trader.ai.model_client import (
    ChatMessage,
    MalformedResponseError,
    MalformedUsageError,
    ModelRequest,
    NotSentError,
    OpenRouterAdapter,
    OutcomeUnknownError,
    ProviderRejectedError,
    Usage,
    estimate_input_tokens,
)


API_KEY = "sk-or-test-key-123456"


def request(**kwargs) -> ModelRequest:
    values = dict(request_key="d1/jev/1", messages=(ChatMessage("user", "hello"),), max_output_tokens=100)
    values.update(kwargs)
    return ModelRequest(**values)


def completion(usage="default", content="hi", **extra) -> dict:
    body = {
        "id": "gen-1", "model": "vendor/orch-1",
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5} if usage == "default" else usage,
    }
    body.update(extra)
    return body


def adapter(handler) -> OpenRouterAdapter:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return OpenRouterAdapter(model_id="vendor/orch-1", api_key=API_KEY, http_client=client)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "usage",
    [None, {}, {"prompt_tokens": 10}, {"prompt_tokens": "10", "completion_tokens": 5},
     {"prompt_tokens": 10.0, "completion_tokens": 5}, {"prompt_tokens": True, "completion_tokens": 5},
     {"prompt_tokens": -1, "completion_tokens": 5}, {"prompt_tokens": 0, "completion_tokens": 5},
     {"prompt_tokens": 10, "completion_tokens": None}, "free"],
)
async def test_malformed_usage_is_an_error_never_zero_cost(usage):
    with pytest.raises(MalformedUsageError) as caught:
        await adapter(lambda r: httpx.Response(200, json=completion(usage=usage))).complete(request(), timeout_seconds=5)
    assert isinstance(caught.value, OutcomeUnknownError) and caught.value.outcome == "COST_UNKNOWN"


@pytest.mark.asyncio
async def test_credentials_never_appear_in_errors_or_repr():
    echo = httpx.Response(401, text=f"bad key {API_KEY} for you")
    target = adapter(lambda r: echo)
    with pytest.raises(ProviderRejectedError) as caught:
        await target.complete(request(), timeout_seconds=5)
    assert API_KEY not in str(caught.value) and API_KEY not in caught.value.detail
    assert API_KEY not in repr(target)
```

**Also write these tests** (each asserts what its name says; the rules above and the helper functions in the file are all they need): `test_success_parses_text_and_strict_usage`, `test_unusable_200_body_is_a_malformed_response`, `test_http_status_classification`, `test_transport_errors_split_not_sent_from_unknown`, `test_request_validation_is_strict`, `test_input_estimate_is_an_upper_bound_style_guess`.

- [ ] **Step 2: Run and see them fail.**
```bash
.venv/bin/python -m pytest tests/ai/test_openrouter_adapter.py -q --timeout=60
```
Expected: FAIL, `ModuleNotFoundError: No module named 'trader.ai.model_client'`.

- [ ] **Step 3: Implement.** `trader/ai/model_client.py` (Task 3 appends to this file):

```python
from __future__ import annotations
import asyncio
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional, Protocol
import httpx
from trader.ai.config import AiConfigError, RoleConfig


NOT_SENT = "NOT_SENT"
REJECTED = "REJECTED"
UNKNOWN = "UNKNOWN"
COST_UNKNOWN = "COST_UNKNOWN"

MAX_REPORTED_TOKENS = 100_000_000
MAX_DETAIL_CHARS = 300
CHAT_ROLES = ("system", "user", "assistant")


class ModelCallError(Exception):
    outcome = ""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


class NotSentError(ModelCallError):
    outcome = NOT_SENT


class ProviderRejectedError(ModelCallError):
    outcome = REJECTED


class OutcomeUnknownError(ModelCallError):
    outcome = UNKNOWN


class MalformedResponseError(OutcomeUnknownError):
    outcome = COST_UNKNOWN


class MalformedUsageError(MalformedResponseError):
    pass


def redact(text: str, secrets: Iterable[str]) -> str:
    for secret in secrets:
        if len(secret) >= 4:
            text = text.replace(secret, "***")
    return text[:MAX_DETAIL_CHARS]


def parse_usage(raw: object, *, input_key: str, output_key: str) -> Usage:
    """Strict. A missing, non-integer, negative or zero-input usage is an error, never zero cost."""
    if not isinstance(raw, Mapping):
        raise MalformedUsageError("USAGE_MISSING")
    counts = []
    for key in (input_key, output_key):
        value = raw.get(key)
        if type(value) is not int or value < 0 or value > MAX_REPORTED_TOKENS:
            raise MalformedUsageError("USAGE_INVALID", f"{key} is not a valid token count")
        counts.append(value)
    if counts[0] < 1:
        raise MalformedUsageError("USAGE_INVALID", f"{input_key} must be at least 1")
    return Usage(counts[0], counts[1])


def classify_http_status(status: int, detail: str) -> ModelCallError:
    if status == 408 or status >= 500 or status < 400:
        return OutcomeUnknownError(f"HTTP_{status}", detail)
    return ProviderRejectedError(f"HTTP_{status}", detail)


def classify_httpx_error(exc: Exception) -> ModelCallError:
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.UnsupportedProtocol)):
        return NotSentError("CONNECT_FAILED", type(exc).__name__)
    if isinstance(exc, httpx.InvalidURL):
        return NotSentError("INVALID_URL", type(exc).__name__)
    return OutcomeUnknownError("TRANSPORT_FAILED", type(exc).__name__)


class _ChatCompletionsAdapter:
    """OpenAI-style chat completions over httpx. OpenRouter and Azure differ only in URL and headers."""

    backend = ""

    def __init__(self, *, model_id: str, api_key: str, http_client: httpx.AsyncClient):
        if not model_id or not api_key:
            raise ValueError("model_id and api_key are required")
        self.model_id = model_id
        self._api_key = api_key
        self._http = http_client

    def __repr__(self) -> str:
        return f"{type(self).__name__}(model_id={self.model_id!r})"

    def _url(self) -> str:
        raise NotImplementedError

    def _headers(self) -> dict[str, str]:
        raise NotImplementedError

    def _params(self) -> dict[str, str]:
        return {}

    def _body(self, request: ModelRequest) -> dict[str, Any]:
        raise NotImplementedError

    def _secrets(self) -> list[str]:
        return [self._api_key]

    async def complete(self, request: ModelRequest, *, timeout_seconds: float) -> ModelResponse:
        timeout = httpx.Timeout(timeout_seconds, connect=min(5.0, timeout_seconds))
        try:
            response = await self._http.post(
                self._url(), headers=self._headers(), params=self._params(), json=self._body(request), timeout=timeout
            )
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            raise classify_httpx_error(exc) from None
        return self._parse(response)

    def _parse(self, response: httpx.Response) -> ModelResponse:
        if not 200 <= response.status_code < 300:
            raise classify_http_status(response.status_code, redact(response.text, self._secrets()))
        try:
            data = response.json()
        except ValueError:
            raise MalformedResponseError("RESPONSE_NOT_JSON") from None
        if not isinstance(data, dict) or "error" in data:
            raise MalformedResponseError("RESPONSE_SHAPE")
        usage = parse_usage(data.get("usage"), input_key="prompt_tokens", output_key="completion_tokens")
        try:
            choice = data["choices"][0]
            text = choice["message"]["content"]
            finish_reason = choice.get("finish_reason") or ""
        except (KeyError, IndexError, TypeError, AttributeError):
            raise MalformedResponseError("RESPONSE_SHAPE") from None
        if not isinstance(text, str) or not isinstance(finish_reason, str):
            raise MalformedResponseError("RESPONSE_SHAPE")
        request_id = data.get("id")
        model = data.get("model")
        return ModelResponse(
            text=text,
            usage=usage,
            model=model if isinstance(model, str) and model else self.model_id,
            backend=self.backend,
            finish_reason=finish_reason,
            provider_request_id=request_id if isinstance(request_id, str) else None,
        )

    async def aclose(self) -> None:
        await self._http.aclose()


def _wire_messages(request: ModelRequest) -> list[dict[str, str]]:
    return [{"role": m.role, "content": m.content} for m in request.messages]


class OpenRouterAdapter(_ChatCompletionsAdapter):
    backend = "openrouter"

    def __init__(self, *, model_id: str, api_key: str, http_client: httpx.AsyncClient,
                 base_url: str = "https://openrouter.ai/api/v1"):
        super().__init__(model_id=model_id, api_key=api_key, http_client=http_client)
        self._base_url = base_url.rstrip("/")

    def _url(self) -> str:
        return f"{self._base_url}/chat/completions"

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}

    def _body(self, request: ModelRequest) -> dict[str, Any]:
        return {
            "model": self.model_id,
            "messages": _wire_messages(request),
            "max_tokens": request.max_output_tokens,
            "temperature": request.temperature,
        }
```

**Not shown, write exactly as described:**
- `ChatMessage(role: str, content: str)`: frozen dataclass. `__post_init__` raises `ValueError` unless `role` is `system`, `user` or `assistant` and `content` is a `str`.
- `ModelRequest(request_key, messages, max_output_tokens, temperature=0.0, attempt_key=None)`: frozen dataclass. `__post_init__` raises `ValueError` when `request_key` is not a non-empty `str` without `#`, `messages` is empty or holds a non-`ChatMessage`, `max_output_tokens` is not a plain positive `int` (`True` is refused), or `temperature` is not an `int`/`float` between 0 and 2.
- `Usage(input_tokens, output_tokens)`: frozen dataclass. `__post_init__` raises `ValueError` unless both are plain `int` (`type(v) is int`) between 0 and `MAX_REPORTED_TOKENS`.
- `ModelResponse(text, usage, model, backend, finish_reason, provider_request_id=None)`: frozen dataclass.
- `ModelClient`: `typing.Protocol` with attributes `backend: str`, `model_id: str`, `async complete(request, *, timeout_seconds) -> ModelResponse`, `async aclose() -> None`.
- `estimate_input_tokens(messages) -> int`: the sum over messages of `ceil(len(content.encode("utf-8")) / 3) + 4`.
- The module docstring repeats the error-class rule from this task's Interfaces.

- [ ] **Step 4: Run and see them pass.**
```bash
.venv/bin/python -m pytest tests/ai/test_openrouter_adapter.py -q --timeout=60
```
Expected: 29 passed.

- [ ] **Step 5: Commit.**
```bash
git add trader/ai/model_client.py tests/ai/test_openrouter_adapter.py
git commit -m "feat: add the model client types and the openrouter adapter" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```


---

### Task 3: Azure and Bedrock adapters, `boto3` dependency, `build_model_client`

**Files:**
- Modify: `trader/ai/model_client.py` (append), `pyproject.toml`, `uv.lock`
- Create (tests): `tests/ai/test_azure_bedrock_adapters.py`

**Interfaces:**
- Consumes: Task 2 types; `botocore.exceptions` (lazy import inside `classify_botocore_error`).
- Produces: `AzureOpenAIAdapter(deployment, endpoint, api_key, api_version, http_client)`, `BedrockAdapter(model_id, converse)` where `converse` is `boto3.client("bedrock-runtime").converse` or a fake, `default_bedrock_converse(region, timeout_seconds)`, `classify_botocore_error(exc)`, `build_model_client(role, *, environ, http_client=None, bedrock_converse=None) -> ModelClient`.

Notes: the Converse call is synchronous, so the adapter runs it with `asyncio.to_thread`. The boto3 client is built with `max_attempts: 1` so the SDK never retries behind the journal's back, and `read_timeout` equals the role's `call_timeout_seconds`. A call abandoned by the gateway timeout keeps its worker thread until that read timeout ends it; its outcome stays unknown.

- [ ] **Step 1: Add the dependency.** In `pyproject.toml` add `"boto3>=1.35",` to `[project] dependencies` (next to `"httpx>=0.27",`). Then:

```bash
uv lock
git diff --stat -- pyproject.toml uv.lock
uv sync --python 3.12.13 --frozen --extra test
.venv/bin/python -c "import boto3, botocore; print(boto3.__version__)"
```
Expected: the `uv.lock` diff only adds `boto3`, `botocore`, `jmespath`, `s3transfer` (and nothing is upgraded). CI runs `uv sync --frozen`, so `uv.lock` must be committed with `pyproject.toml`.

- [ ] **Step 2: Write the failing tests.** `tests/ai/test_azure_bedrock_adapters.py`:

```python
import httpx
import pytest
from botocore import exceptions as bx
from trader.ai.config import AiConfigError, RoleConfig
from trader.ai.model_client import (
    AzureOpenAIAdapter,
    BedrockAdapter,
    ChatMessage,
    MalformedResponseError,
    MalformedUsageError,
    ModelRequest,
    NotSentError,
    OpenRouterAdapter,
    OutcomeUnknownError,
    ProviderRejectedError,
    Usage,
    build_model_client,
)


KEY = "azure-secret-key-9876"


def request() -> ModelRequest:
    return ModelRequest(request_key="d/o/1", max_output_tokens=50,
                        messages=(ChatMessage("system", "be brief"), ChatMessage("user", "hi")))


def bedrock_reply(**overrides) -> dict:
    reply = {"output": {"message": {"role": "assistant", "content": [{"text": "done"}]}},
             "usage": {"inputTokens": 11, "outputTokens": 3, "totalTokens": 14},
             "stopReason": "end_turn", "ResponseMetadata": {"RequestId": "req-9"}}
    reply.update(overrides)
    return reply


def client_error(code: str) -> bx.ClientError:
    return bx.ClientError({"Error": {"Code": code, "Message": "m"}}, "Converse")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error,expected",
    [(client_error("ThrottlingException"), ProviderRejectedError),
     (client_error("ValidationException"), ProviderRejectedError),
     (client_error("ModelTimeoutException"), OutcomeUnknownError),
     (client_error("InternalServerException"), OutcomeUnknownError),
     (bx.NoCredentialsError(), NotSentError),
     (bx.EndpointConnectionError(endpoint_url="https://x"), NotSentError),
     (bx.ReadTimeoutError(endpoint_url="https://x"), OutcomeUnknownError),
     (RuntimeError("boom"), OutcomeUnknownError)],
)
async def test_bedrock_error_classification(error, expected):
    def converse(**arguments):
        raise error

    with pytest.raises(expected) as caught:
        await BedrockAdapter(model_id="p", converse=converse).complete(request(), timeout_seconds=5)
    assert type(caught.value) is expected
```

**Also write these tests** (each asserts what its name says; the rules above and the helper functions in the file are all they need): `test_azure_uses_deployment_url_api_version_and_api_key_header`, `test_azure_error_body_that_echoes_the_key_is_redacted`, `test_bedrock_converse_arguments_and_parse`, `test_bedrock_malformed_usage_is_an_error`, `test_bedrock_response_without_text_is_malformed`, `test_build_model_client_picks_the_adapter_and_checks_env`.

- [ ] **Step 3: Run and see them fail.**
```bash
.venv/bin/python -m pytest tests/ai/test_azure_bedrock_adapters.py -q --timeout=60
```
Expected: FAIL, `ImportError: cannot import name 'AzureOpenAIAdapter'`.

- [ ] **Step 4: Implement.** Append to `trader/ai/model_client.py`:

```python
class AzureOpenAIAdapter(_ChatCompletionsAdapter):
    """`deployment` is the Azure deployment name; it is the configured model id."""

    backend = "azure"

    def __init__(self, *, deployment: str, endpoint: str, api_key: str, api_version: str,
                 http_client: httpx.AsyncClient):
        super().__init__(model_id=deployment, api_key=api_key, http_client=http_client)
        if not endpoint or not api_version:
            raise ValueError("endpoint and api_version are required")
        self._endpoint = endpoint.rstrip("/")
        self._api_version = api_version

    def _url(self) -> str:
        return f"{self._endpoint}/openai/deployments/{self.model_id}/chat/completions"

    def _params(self) -> dict[str, str]:
        return {"api-version": self._api_version}

    def _headers(self) -> dict[str, str]:
        return {"api-key": self._api_key, "Content-Type": "application/json"}

    def _body(self, request: ModelRequest) -> dict[str, Any]:
        return {
            "messages": _wire_messages(request),
            "max_completion_tokens": request.max_output_tokens,
            "temperature": request.temperature,
        }


_BEDROCK_REJECTED_CODES = frozenset({
    "ValidationException", "AccessDeniedException", "ResourceNotFoundException",
    "ThrottlingException", "ServiceQuotaExceededException",
})


def classify_botocore_error(exc: Exception) -> ModelCallError:
    from botocore import exceptions as bx

    if isinstance(exc, bx.ClientError):
        code = str(exc.response.get("Error", {}).get("Code", "")) or "ERROR"
        if code in _BEDROCK_REJECTED_CODES:
            return ProviderRejectedError(f"BEDROCK_{code}")
        return OutcomeUnknownError(f"BEDROCK_{code}")
    not_sent = (bx.NoCredentialsError, bx.PartialCredentialsError, bx.NoRegionError,
                bx.EndpointConnectionError, bx.ConnectTimeoutError, bx.ParamValidationError)
    if isinstance(exc, not_sent):
        return NotSentError("BEDROCK_NOT_SENT", type(exc).__name__)
    return OutcomeUnknownError("BEDROCK_TRANSPORT", type(exc).__name__)


class BedrockAdapter:
    """AWS Bedrock Converse. `converse` is `boto3.client("bedrock-runtime").converse` or a fake.

    boto3 is synchronous, so the call runs in a worker thread. A call abandoned by a timeout
    keeps running until the client's own read timeout ends it; its outcome stays unknown."""

    backend = "bedrock"

    def __init__(self, *, model_id: str, converse: Callable[..., Mapping[str, Any]]):
        if not model_id:
            raise ValueError("model_id is required")
        self.model_id = model_id
        self._converse = converse

    def __repr__(self) -> str:
        return f"BedrockAdapter(model_id={self.model_id!r})"

    async def complete(self, request: ModelRequest, *, timeout_seconds: float) -> ModelResponse:
        system = [{"text": m.content} for m in request.messages if m.role == "system"]
        turns = [{"role": m.role, "content": [{"text": m.content}]} for m in request.messages if m.role != "system"]
        arguments: dict[str, Any] = {
            "modelId": self.model_id,
            "messages": turns,
            "inferenceConfig": {"maxTokens": request.max_output_tokens, "temperature": request.temperature},
        }
        if system:
            arguments["system"] = system
        try:
            raw = await asyncio.to_thread(self._converse, **arguments)
        except Exception as exc:
            raise classify_botocore_error(exc) from None
        return self._parse(raw)

    def _parse(self, raw: object) -> ModelResponse:
        if not isinstance(raw, Mapping):
            raise MalformedResponseError("RESPONSE_SHAPE")
        usage = parse_usage(raw.get("usage"), input_key="inputTokens", output_key="outputTokens")
        try:
            blocks = raw["output"]["message"]["content"]
            text = "".join(block["text"] for block in blocks if "text" in block)
            stop_reason = raw.get("stopReason") or ""
            request_id = raw.get("ResponseMetadata", {}).get("RequestId")
        except (KeyError, TypeError, AttributeError):
            raise MalformedResponseError("RESPONSE_SHAPE") from None
        if not text or not isinstance(stop_reason, str):
            raise MalformedResponseError("RESPONSE_SHAPE")
        return ModelResponse(
            text=text, usage=usage, model=self.model_id, backend=self.backend, finish_reason=stop_reason,
            provider_request_id=request_id if isinstance(request_id, str) else None,
        )

    async def aclose(self) -> None:
        return None


def default_bedrock_converse(*, region: str, timeout_seconds: float) -> Callable[..., Mapping[str, Any]]:
    import boto3
    from botocore.config import Config

    config = Config(connect_timeout=5, read_timeout=timeout_seconds,
                    retries={"mode": "standard", "max_attempts": 1})
    return boto3.client("bedrock-runtime", region_name=region, config=config).converse
```

**Not shown, write exactly as described:**
- `_required(environ, name) -> str`: the value, or `AiConfigError("CREDENTIALS_MISSING", f"missing: {name}")` when empty.
- `build_model_client(role: RoleConfig, *, environ, http_client=None, bedrock_converse=None) -> ModelClient`: `openrouter` returns `OpenRouterAdapter(model_id=role.model, api_key=_required(environ, "OPENROUTER_API_KEY"), http_client=http_client or httpx.AsyncClient())`; `azure` returns `AzureOpenAIAdapter(deployment=role.model, endpoint=_required(environ, "AZURE_OPENAI_ENDPOINT"), api_key=_required(environ, "AZURE_OPENAI_API_KEY"), api_version=_required(environ, "AZURE_OPENAI_API_VERSION"), http_client=http_client or httpx.AsyncClient())`; `bedrock` takes the region from `AWS_REGION` or `AWS_DEFAULT_REGION` (else `AiConfigError("CREDENTIALS_MISSING", "missing: AWS_REGION")`) and returns `BedrockAdapter(model_id=role.model, converse=bedrock_converse or default_bedrock_converse(region=region, timeout_seconds=role.call_timeout_seconds))`; any other backend raises `AiConfigError("ROLE_BACKEND_UNSUPPORTED", ...)`.

- [ ] **Step 5: Run and see them pass.**
```bash
.venv/bin/python -m pytest tests/ai/test_openrouter_adapter.py tests/ai/test_azure_bedrock_adapters.py -q --timeout=60
```
Expected: 46 passed.

- [ ] **Step 6: Commit.**
```bash
git add trader/ai/model_client.py tests/ai/test_azure_bedrock_adapters.py pyproject.toml uv.lock
git commit -m "feat: add the azure and bedrock adapters and the boto3 dependency" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```


---

### Task 4: `ai.duckdb` schema (migrations 1-5) and `AiStore`

**Files:**
- Create: `trader/ai/schema.py`, `trader/ai/store.py`
- Create (tests): `tests/ai/conftest.py`, `tests/ai/test_store.py`

**Interfaces:**
- Consumes: `DuckDBConnection` (`trader/data/duckdb_store.py`), `SchemaMigrator` (`trader/data/schema_migrations.py`), `Clock`.
- Produces: `Migration(version, name, statements)`, `FOUNDATION_MIGRATIONS` (versions 1-5), `AiStore(path, *, clock)` with `.db`, `.migrate(migrations=FOUNDATION_MIGRATIONS) -> list[int]`, `.transaction(work)`, `async .atransaction(work)`, `async .aquery(sql, params=None, *, fetch="all")`, and `to_utc(datetime)`.

Tables (one plain `CREATE` each; there is no legacy data):

| Version | Table | Owner module |
|---|---|---|
| 1 | `ai_model_attempts` | journal |
| 2 | `ai_budget_state`, `ai_budget_cap_events` | budget |
| 3 | `ai_budget_reservations` | budget |
| 4 | `ai_cost_events` (+ sequence `ai_cost_event_seq`) | journal |
| 5 | `ai_replay_evidence` | replay |

Versions 6-9 stay free. Plan 5 passes its own migrations (10-19) and Plan 6 (20-29) to `AiStore.migrate`. Every write runs through `DuckDBConnection.transaction`, which holds one lock per file, so all `AiStore` instances of one path serialize. Blocking work always goes through `asyncio.to_thread`. DuckDB returns `TIMESTAMPTZ` in the process time zone, so code compares instants only as aware datetimes (`to_utc`) and never takes a date from a stored timestamp.

- [ ] **Step 1: Write the failing tests.**

`tests/ai/conftest.py` (the `no_network` fixture is added in Task 8):

```python
from datetime import datetime, timezone

import pytest

from tests.ai.fakes import FakeClock
from trader.ai.store import AiStore


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(datetime(2026, 7, 1, 15, 0, tzinfo=timezone.utc))  # 11:00 New York


@pytest.fixture
def store(tmp_path, clock) -> AiStore:
    created = AiStore(tmp_path / "ai.duckdb", clock=clock)
    created.migrate()
    return created
```

`tests/ai/test_store.py`:

```python
import asyncio
import time
from datetime import datetime, timezone
import pytest
from trader.ai.schema import FOUNDATION_MIGRATIONS, Migration
from trader.ai.store import AiStore, to_utc


@pytest.mark.asyncio
async def test_concurrent_writes_are_serialized_across_store_instances(tmp_path, clock):
    first = AiStore(tmp_path / "w.duckdb", clock=clock)
    second = AiStore(tmp_path / "w.duckdb", clock=clock)
    first.migrate([Migration(1, "t", ("CREATE TABLE counter (n INTEGER)", "INSERT INTO counter VALUES (0)"))])

    def increment(conn):
        value = conn.execute("SELECT n FROM counter").fetchone()[0]
        time.sleep(0.002)
        conn.execute("UPDATE counter SET n = ?", [value + 1])

    await asyncio.gather(*[(first if i % 2 else second).atransaction(increment) for i in range(30)])
    assert (await first.aquery("SELECT n FROM counter", fetch="one"))[0] == 30


@pytest.mark.parametrize("zone", ["America/New_York", "Europe/Berlin", "UTC"])
def test_stored_instants_compare_the_same_under_any_process_time_zone(store, monkeypatch, zone):
    monkeypatch.setenv("TZ", zone)
    time.tzset()
    try:
        instant = datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc)
        store.db.execute(
            "INSERT INTO ai_cost_events (event_id, attempt_key, role, backend, model, kind, cost_micros, occurred_at)"
            " VALUES ('e', 'a', 'r', 'b', 'm', 'k', 1, ?)", [instant])
        stored = store.db.execute("SELECT occurred_at FROM ai_cost_events", fetch="one")[0]
        assert to_utc(stored) == instant
    finally:
        monkeypatch.undo()
        time.tzset()
```

**Also write these tests** (each asserts what its name says; the rules above and the helper functions in the file are all they need): `test_plan_4_migrations_stay_inside_1_to_9`, `test_migrate_is_idempotent_and_survives_reopen`, `test_other_plans_add_their_own_range_and_bad_versions_are_refused`, `test_blocking_work_does_not_block_the_event_loop`, `test_a_failed_transaction_rolls_back`.

- [ ] **Step 2: Run and see them fail.**
```bash
.venv/bin/python -m pytest tests/ai/test_store.py -q --timeout=60
```
Expected: FAIL, `ModuleNotFoundError: No module named 'trader.ai.schema'`.

- [ ] **Step 3: Implement.**

`trader/ai/schema.py`:

```python
"""ai.duckdb schema owned by Plan 4: migrations 1-5 (6-9 stay free). Plain CREATE only."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    statements: tuple[str, ...]


FOUNDATION_MIGRATIONS: tuple[Migration, ...] = (
    Migration(1, "ai_model_attempts", ("""
        CREATE TABLE ai_model_attempts (
            attempt_key VARCHAR PRIMARY KEY,
            request_key VARCHAR NOT NULL,
            attempt_no INTEGER NOT NULL,
            role VARCHAR NOT NULL, backend VARCHAR NOT NULL, model VARCHAR NOT NULL,
            status VARCHAR NOT NULL,
            reservation_id VARCHAR NOT NULL,
            request_json VARCHAR NOT NULL, request_sha256 VARCHAR NOT NULL,
            response_text VARCHAR, finish_reason VARCHAR, provider_request_id VARCHAR,
            input_tokens BIGINT, output_tokens BIGINT,
            error_code VARCHAR, error_detail VARCHAR,
            started_at TIMESTAMPTZ NOT NULL, finished_at TIMESTAMPTZ,
            UNIQUE (request_key, attempt_no))""",)),
    Migration(2, "ai_budget_state", ("""
        CREATE TABLE ai_budget_state (
            id INTEGER PRIMARY KEY,
            effective_cap_micros BIGINT NOT NULL,
            pending_cap_micros BIGINT,
            pending_window_date VARCHAR,
            updated_at TIMESTAMPTZ NOT NULL)""", """
        CREATE TABLE ai_budget_cap_events (
            occurred_at TIMESTAMPTZ NOT NULL, kind VARCHAR NOT NULL,
            requested_micros BIGINT NOT NULL, effective_micros BIGINT NOT NULL,
            pending_micros BIGINT, pending_window_date VARCHAR)""")),
    Migration(3, "ai_budget_reservations", ("""
        CREATE TABLE ai_budget_reservations (
            reservation_id VARCHAR PRIMARY KEY,
            window_date VARCHAR NOT NULL,
            role VARCHAR NOT NULL, backend VARCHAR NOT NULL, model VARCHAR NOT NULL,
            state VARCHAR NOT NULL,
            reserved_micros BIGINT NOT NULL, actual_micros BIGINT,
            input_tokens BIGINT, output_tokens BIGINT,
            release_reason VARCHAR,
            created_at TIMESTAMPTZ NOT NULL, closed_at TIMESTAMPTZ)""",)),
    Migration(4, "ai_cost_events", ("CREATE SEQUENCE ai_cost_event_seq START 1", """
        CREATE TABLE ai_cost_events (
            event_seq BIGINT PRIMARY KEY DEFAULT nextval('ai_cost_event_seq'),
            event_id VARCHAR NOT NULL UNIQUE,
            attempt_key VARCHAR NOT NULL,
            role VARCHAR NOT NULL, backend VARCHAR NOT NULL, model VARCHAR NOT NULL,
            kind VARCHAR NOT NULL, cost_micros BIGINT NOT NULL,
            input_tokens BIGINT, output_tokens BIGINT,
            occurred_at TIMESTAMPTZ NOT NULL)""")),
    Migration(5, "ai_replay_evidence", ("""
        CREATE TABLE ai_replay_evidence (
            decision_key VARCHAR NOT NULL, kind VARCHAR NOT NULL, name VARCHAR NOT NULL,
            args_sha256 VARCHAR NOT NULL, ordinal INTEGER NOT NULL,
            payload_json VARCHAR NOT NULL, recorded_at TIMESTAMPTZ NOT NULL,
            PRIMARY KEY (decision_key, kind, name, args_sha256, ordinal))""",)),
)
```

`trader/ai/store.py`:

```python
"""ai.duckdb access. DuckDB only through DuckDBConnection; blocking work runs off the event loop."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from trader.ai.clock import Clock
from trader.ai.schema import FOUNDATION_MIGRATIONS, Migration
from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator


def to_utc(value: datetime) -> datetime:
    """DuckDB returns TIMESTAMPTZ in the process time zone. Normalise before comparing."""
    return value.astimezone(timezone.utc)


class AiStore:
    """One writer at a time: all instances of one path share DuckDBConnection's lock."""

    def __init__(self, path: str | Path, *, clock: Clock):
        self.path = str(Path(path).expanduser())
        self.db = DuckDBConnection.get_instance(self.path)
        self.clock = clock

    def migrate(self, migrations: Sequence[Migration] = FOUNDATION_MIGRATIONS) -> list[int]:
        """Apply each version once, in order. Plans 5 and 6 pass their own ranges (10-19, 20-29)."""
        versions = [m.version for m in migrations]
        if versions != sorted(set(versions)) or any(type(v) is not int or v < 1 for v in versions):
            raise ValueError("migration versions must be unique, ascending positive integers")
        migrator = SchemaMigrator(self.db)
        return [m.version for m in migrations if migrator.apply(m.version, m.name, m.statements)]

    def transaction(self, work: Callable[[Any], Any]) -> Any:
        return self.db.transaction(work)

    async def atransaction(self, work: Callable[[Any], Any]) -> Any:
        return await asyncio.to_thread(self.db.transaction, work)

    async def aquery(self, sql: str, params: Optional[list] = None, *, fetch: str = "all") -> Any:
        return await asyncio.to_thread(self.db.execute, sql, params, fetch)
```

- [ ] **Step 4: Run and see them pass.**
```bash
.venv/bin/python -m pytest tests/ai/test_store.py -q --timeout=60
```
Expected: 9 passed.

- [ ] **Step 5: Commit.**
```bash
git add trader/ai/schema.py trader/ai/store.py tests/ai/conftest.py tests/ai/test_store.py
git commit -m "feat: add the ai.duckdb schema and store" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```


---

### Task 5: `AttemptJournal`

**Files:**
- Create: `trader/ai/journal.py`
- Create (tests): `tests/ai/test_journal.py`

**Interfaces:**
- Consumes: `AiStore`, `ModelRequest`, `ModelResponse`, `Usage`, tables from migrations 1 and 4.
- Produces: `AttemptJournal(store)`; `*_in_tx(conn, ...)` methods for the gateway: `begin_in_tx(conn, *, request, role, backend, model, reservation_id, now) -> AttemptRecord`, `get_in_tx`, `finish_success_in_tx(conn, attempt_key, response, now)`, `finish_failure_in_tx(conn, attempt_key, *, outcome, error_code, error_detail, now)`, `mark_started_as_unknown_in_tx(conn, now) -> list[AttemptRecord]`, `reconcile_late_usage_in_tx(conn, attempt_key, usage, now) -> bool`, `add_cost_event_in_tx(conn, *, attempt, kind, cost_micros, usage, now)`; reads `get`, `aget`, `attempts_with_prefix(prefix)`, `unknown_attempts()`, `async acost_events_after(event_seq, limit=100) -> list[CostEvent]`; `AttemptRecord`, `CostEvent`, `JournalError(code)`, `canonical_request_json`, `request_sha256`, and the cost kinds `CONFIRMED`, `ESTIMATED_UNKNOWN`, `NONE`, `CORRECTION`.

Rules pinned by the tests: the attempt row exists before the call (`STARTED`); `attempt_key = "<request_key>#<n>"`; an unknown outcome never becomes `SUCCEEDED`; only a late usage report moves `UNKNOWN` / `COST_UNKNOWN` to `RECONCILED`, once; the stored request has no headers or credentials. Every finished attempt writes one cost event with a stable id (`<attempt_key>:<kind>`) for Plan 5's reporting outbox.

- [ ] **Step 1: Write the failing tests.** `tests/ai/test_journal.py`:

```python
import json
import pytest
from trader.ai.journal import (
    COST_CONFIRMED, COST_CORRECTION, RECONCILED, STARTED, SUCCEEDED, AttemptJournal, JournalError,
    canonical_request_json,
)
from trader.ai.model_client import ChatMessage, ModelRequest, ModelResponse, Usage


def make_request(key="d1/jev/1", text="hello") -> ModelRequest:
    return ModelRequest(request_key=key, messages=(ChatMessage("user", text),), max_output_tokens=10)


def response() -> ModelResponse:
    return ModelResponse("answer", Usage(10, 5), "m", "openrouter", "stop", "gen-1")


@pytest.fixture
def journal(store) -> AttemptJournal:
    return AttemptJournal(store)


def begin(store, journal, clock, request=None, reservation="r1"):
    request = request or make_request()
    return store.transaction(lambda conn: journal.begin_in_tx(
        conn, request=request, role="jev", backend="openrouter", model="m", reservation_id=reservation, now=clock.now()))


def test_restart_turns_started_into_unknown_and_unknown_never_becomes_success(store, journal, clock):
    record = begin(store, journal, clock)
    marked = store.transaction(lambda conn: journal.mark_started_as_unknown_in_tx(conn, clock.now()))
    assert [m.attempt_key for m in marked] == [record.attempt_key]
    assert journal.get(record.attempt_key).status == "UNKNOWN"
    assert journal.get(record.attempt_key).error_code == "PROCESS_RESTARTED"
    with pytest.raises(JournalError):
        store.transaction(lambda conn: journal.finish_success_in_tx(conn, record.attempt_key, response(), clock.now()))
    assert [a.attempt_key for a in journal.unknown_attempts()] == [record.attempt_key]
```

**Also write these tests** (each asserts what its name says; the rules above and the helper functions in the file are all they need): `test_begin_records_the_request_before_any_call`, `test_attempt_numbers_count_per_request_key`, `test_hash_ignores_the_attempt_key`, `test_success_stores_the_response_and_blocks_a_second_finish`, `test_failure_outcomes_are_validated_and_detail_is_bounded`, `test_late_usage_reconciles_once`, `test_late_usage_is_refused_for_a_succeeded_attempt`, `test_cost_events_have_stable_ids_and_a_cursor`.

- [ ] **Step 2: Run and see them fail.**
```bash
.venv/bin/python -m pytest tests/ai/test_journal.py -q --timeout=60
```
Expected: FAIL, `ModuleNotFoundError: No module named 'trader.ai.journal'`.

- [ ] **Step 3: Implement.** `trader/ai/journal.py`:

```python
from __future__ import annotations
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional
from trader.ai.model_client import (
    COST_UNKNOWN, NOT_SENT, REJECTED, UNKNOWN, ModelRequest, ModelResponse, Usage,
)
from trader.ai.store import AiStore, to_utc


STARTED = "STARTED"
SUCCEEDED = "SUCCEEDED"
RECONCILED = "RECONCILED"
FAILURE_STATUSES = frozenset({NOT_SENT, REJECTED, UNKNOWN, COST_UNKNOWN})
LATE_USAGE_STATUSES = frozenset({UNKNOWN, COST_UNKNOWN})
MAX_ERROR_DETAIL = 500

COST_CONFIRMED = "CONFIRMED"
COST_ESTIMATED_UNKNOWN = "ESTIMATED_UNKNOWN"
COST_NONE = "NONE"
COST_CORRECTION = "CORRECTION"


class JournalError(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


_ATTEMPT_COLUMNS = (
    "attempt_key, request_key, attempt_no, role, backend, model, status, reservation_id, request_json, "
    "request_sha256, response_text, finish_reason, provider_request_id, input_tokens, output_tokens, "
    "error_code, error_detail, started_at, finished_at"
)
_COST_COLUMNS = ("event_seq, event_id, attempt_key, role, backend, model, kind, cost_micros, "
                 "input_tokens, output_tokens, occurred_at")


def canonical_request_json(request: ModelRequest) -> str:
    """What the model was asked. Excludes attempt_key so the hash is stable across replays."""
    body = {
        "messages": [{"role": m.role, "content": m.content} for m in request.messages],
        "max_output_tokens": request.max_output_tokens,
        "temperature": request.temperature,
    }
    return json.dumps(body, sort_keys=True, ensure_ascii=False, allow_nan=False)


def request_sha256(request_json: str) -> str:
    return hashlib.sha256(request_json.encode("utf-8")).hexdigest()


def _attempt(row: tuple) -> AttemptRecord:
    values = list(row)
    values[17] = to_utc(values[17])
    values[18] = to_utc(values[18]) if values[18] is not None else None
    return AttemptRecord(*values)


def _cost_event(row: tuple) -> CostEvent:
    values = list(row)
    values[10] = to_utc(values[10])
    return CostEvent(*values)


class AttemptJournal:
    def __init__(self, store: AiStore):
        self.store = store

    def begin_in_tx(self, conn: Any, *, request: ModelRequest, role: str, backend: str, model: str,
                    reservation_id: str, now: datetime) -> AttemptRecord:
        top = conn.execute("SELECT COALESCE(MAX(attempt_no), 0) FROM ai_model_attempts WHERE request_key = ?",
                           [request.request_key]).fetchone()[0]
        attempt_no = top + 1
        attempt_key = f"{request.request_key}#{attempt_no}"
        request_json = canonical_request_json(request)
        conn.execute(
            "INSERT INTO ai_model_attempts (attempt_key, request_key, attempt_no, role, backend, model, status, "
            "reservation_id, request_json, request_sha256, started_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [attempt_key, request.request_key, attempt_no, role, backend, model, STARTED, reservation_id,
             request_json, request_sha256(request_json), now])
        return self.get_in_tx(conn, attempt_key)

    def get_in_tx(self, conn: Any, attempt_key: str) -> Optional[AttemptRecord]:
        row = conn.execute(f"SELECT {_ATTEMPT_COLUMNS} FROM ai_model_attempts WHERE attempt_key = ?",
                           [attempt_key]).fetchone()
        return _attempt(row) if row else None

    def _require_status(self, conn: Any, attempt_key: str, allowed: frozenset[str]) -> AttemptRecord:
        record = self.get_in_tx(conn, attempt_key)
        if record is None:
            raise JournalError("ATTEMPT_UNKNOWN", attempt_key)
        if record.status not in allowed:
            raise JournalError("ATTEMPT_STATE", f"{attempt_key} is {record.status}")
        return record

    def finish_success_in_tx(self, conn: Any, attempt_key: str, response: ModelResponse, now: datetime) -> None:
        self._require_status(conn, attempt_key, frozenset({STARTED}))
        conn.execute(
            "UPDATE ai_model_attempts SET status = ?, response_text = ?, finish_reason = ?, provider_request_id = ?, "
            "input_tokens = ?, output_tokens = ?, finished_at = ? WHERE attempt_key = ?",
            [SUCCEEDED, response.text, response.finish_reason, response.provider_request_id,
             response.usage.input_tokens, response.usage.output_tokens, now, attempt_key])

    def finish_failure_in_tx(self, conn: Any, attempt_key: str, *, outcome: str, error_code: str,
                             error_detail: str, now: datetime) -> None:
        if outcome not in FAILURE_STATUSES:
            raise JournalError("OUTCOME_INVALID", outcome)
        self._require_status(conn, attempt_key, frozenset({STARTED}))
        conn.execute("UPDATE ai_model_attempts SET status = ?, error_code = ?, error_detail = ?, finished_at = ? "
                     "WHERE attempt_key = ?", [outcome, error_code, error_detail[:MAX_ERROR_DETAIL], now, attempt_key])

    def mark_started_as_unknown_in_tx(self, conn: Any, now: datetime) -> list[AttemptRecord]:
        """Restart only. A process that died mid-call cannot know what the provider did."""
        rows = conn.execute(f"SELECT {_ATTEMPT_COLUMNS} FROM ai_model_attempts WHERE status = ? ORDER BY started_at",
                            [STARTED]).fetchall()
        conn.execute("UPDATE ai_model_attempts SET status = ?, error_code = 'PROCESS_RESTARTED', finished_at = ? "
                     "WHERE status = ?", [UNKNOWN, now, STARTED])
        return [self.get_in_tx(conn, _attempt(row).attempt_key) for row in rows]

    def reconcile_late_usage_in_tx(self, conn: Any, attempt_key: str, usage: Usage, now: datetime) -> bool:
        """UNKNOWN or COST_UNKNOWN -> RECONCILED, once. A repeat with the same numbers is a no-op."""
        record = self._require_status(conn, attempt_key, LATE_USAGE_STATUSES | {RECONCILED})
        if record.status == RECONCILED:
            if (record.input_tokens, record.output_tokens) == (usage.input_tokens, usage.output_tokens):
                return False
            raise JournalError("LATE_USAGE_CONFLICT", attempt_key)
        conn.execute("UPDATE ai_model_attempts SET status = ?, input_tokens = ?, output_tokens = ?, finished_at = ? "
                     "WHERE attempt_key = ?", [RECONCILED, usage.input_tokens, usage.output_tokens, now, attempt_key])
        return True

    def add_cost_event_in_tx(self, conn: Any, *, attempt: AttemptRecord, kind: str, cost_micros: int,
                             usage: Optional[Usage], now: datetime) -> None:
        conn.execute(
            "INSERT INTO ai_cost_events (event_id, attempt_key, role, backend, model, kind, cost_micros, "
            "input_tokens, output_tokens, occurred_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [f"{attempt.attempt_key}:{kind}", attempt.attempt_key, attempt.role, attempt.backend, attempt.model,
             kind, cost_micros, usage.input_tokens if usage else None, usage.output_tokens if usage else None, now])

    async def acost_events_after(self, event_seq: int, limit: int = 100) -> list[CostEvent]:
        """For the reporting outbox (Plan 5). Cursor = last seen event_seq; gaps are normal."""
        rows = await self.store.aquery(
            f"SELECT {_COST_COLUMNS} FROM ai_cost_events WHERE event_seq > ? ORDER BY event_seq LIMIT ?",
            [event_seq, limit])
        return [_cost_event(row) for row in rows]
```

**Not shown, write exactly as described:**
- `AttemptRecord`: frozen dataclass with the 19 columns of `ai_model_attempts` in table order (`attempt_key, request_key, attempt_no, role, backend, model, status, reservation_id, request_json, request_sha256, response_text, finish_reason, provider_request_id, input_tokens, output_tokens, error_code, error_detail, started_at, finished_at`).
- `CostEvent`: frozen dataclass with `event_seq, event_id, attempt_key, role, backend, model, kind, cost_micros, input_tokens, output_tokens, occurred_at`.
- Reads, all through `self.store.db.execute(sql, params, fetch="all")` and `_attempt`: `get(attempt_key)` is `self.store.transaction(lambda conn: self.get_in_tx(conn, attempt_key))`; `async aget` is the same through `atransaction`; `attempts_with_prefix(prefix)` selects `WHERE starts_with(request_key, ?) ORDER BY request_key, attempt_no`; `unknown_attempts()` selects `WHERE status IN ('UNKNOWN', 'COST_UNKNOWN') ORDER BY started_at`.

- [ ] **Step 4: Run and see them pass.**
```bash
.venv/bin/python -m pytest tests/ai/test_journal.py -q --timeout=60
```
Expected: 9 passed.

- [ ] **Step 5: Commit.**
```bash
git add trader/ai/journal.py tests/ai/test_journal.py
git commit -m "feat: add the model attempt journal" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```


---

### Task 6: `Budget` (durable admission, spec 5.4)

**Files:**
- Create: `trader/ai/budget.py`
- Create (tests): `tests/ai/test_budget.py`

**Interfaces:**
- Consumes: `AiStore`, `Clock`, tables from migrations 2 and 3.
- Produces: `Budget(store, clock, *, calls_per_hour)`; `set_cap_in_tx(conn, requested_micros, now) -> str` and `async set_cap(requested_micros) -> str` (returns `INITIALIZED | LOWERED | UNCHANGED | RAISE_SCHEDULED | RAISE_KEPT`); `reserve_in_tx(conn, *, reservation_id, role, backend, model, worst_case_micros, now) -> Reservation`; `release_in_tx(conn, reservation_id, *, reason, now)`; `mark_unknown_in_tx(conn, reservation_id, now)`; `settle_in_tx(conn, reservation_id, *, actual_micros, input_tokens, output_tokens, now, late=False) -> bool`; `recover_open_in_tx(conn, now) -> list[str]`; `get_in_tx`; `snapshot_in_tx` / `async snapshot() -> BudgetSnapshot`; `window_date(instant)`, `next_window_date`, `next_window_start`; `BudgetRefusal` > `BudgetExhausted(retry_at)`, `HourlyLimitReached(retry_at, retry_after_seconds)`, `BudgetNotInitialized`; `BudgetConflict(code)`.

The rules, each pinned by a test:
- **Atomic worst case.** The window total, the cap check, the hourly check and the insert are one transaction. 60 concurrent reservations from two roles never pass the cap.
- **Cap.** The first `set_cap` initializes. Lower applies at once. Higher is stored as a pending raise with the next New York date and applies at the first call at or after 00:00 America/New_York. A restart calls `set_cap` again with the same number and gets `RAISE_KEPT`: it neither applies the raise early nor moves its date. A restart after midnight rolls the raise first, then compares.
- **Windows.** The window is the New York date computed in Python and stored as text. A reservation keeps its creation window for its whole life, so a midnight-spanning call settles in its own window and nothing is reset twice or erased. The 23-hour and 25-hour DST days are covered.
- **Unknown.** `UNKNOWN` keeps its reserved amount. A late report settles it once (`late=True`); a different figure later is `SETTLEMENT_CONFLICT`.
- **Hourly limit.** `HourlyLimitReached` carries the time the oldest counted call leaves the hour. It is a delay, never `BudgetExhausted`. A call proven not sent (`release reason NOT_SENT`) does not count.
- **Overrun.** Actual cost above the reservation is recorded and counted.

- [ ] **Step 1: Write the failing tests.** `tests/ai/test_budget.py`:

```python
import asyncio
from datetime import datetime, timezone
import pytest
from tests.ai.fakes import FakeClock
from trader.ai.budget import (
    Budget, BudgetConflict, BudgetExhausted, BudgetNotInitialized, HourlyLimitReached, next_window_start, window_date,
)
from trader.ai.store import AiStore


UTC = timezone.utc
USD = 1_000_000


def make_budget(store, clock, calls_per_hour=120) -> Budget:
    return Budget(store, clock, calls_per_hour=calls_per_hour)


def reserve(budget, store, clock, rid, amount=100_000, role="orchestrator"):
    return store.transaction(lambda conn: budget.reserve_in_tx(
        conn, reservation_id=rid, role=role, backend="openrouter", model="m", worst_case_micros=amount, now=clock.now()))


def settle(budget, store, clock, rid, actual, late=False):
    return store.transaction(lambda conn: budget.settle_in_tx(
        conn, rid, actual_micros=actual, input_tokens=1, output_tokens=1, now=clock.now(), late=late))


def tx(store, fn):
    return store.transaction(fn)


@pytest.mark.asyncio
async def test_restart_never_applies_a_raise_early_or_moves_its_date(tmp_path, clock):
    path = tmp_path / "r.duckdb"
    first = AiStore(path, clock=clock)
    first.migrate()
    await make_budget(first, clock).set_cap(1 * USD)
    await make_budget(first, clock).set_cap(5 * USD)
    clock.advance(6 * 3600)
    restarted = AiStore(path, clock=clock)
    restarted.migrate()
    again = make_budget(restarted, clock)
    assert await again.set_cap(5 * USD) == "RAISE_KEPT"
    snapshot = await again.snapshot()
    assert (snapshot.effective_cap_micros, snapshot.pending_window_date) == (1 * USD, "2026-07-02")


@pytest.mark.asyncio
async def test_restart_after_midnight_applies_the_pending_raise_before_comparing(store, clock):
    budget = make_budget(store, clock)
    await budget.set_cap(1 * USD)
    await budget.set_cap(5 * USD)
    clock.advance(14 * 3600)  # 01:00 next day in New York
    assert await make_budget(store, clock).set_cap(5 * USD) == "UNCHANGED"
    assert (await budget.snapshot()).effective_cap_micros == 5 * USD


@pytest.mark.asyncio
async def test_concurrent_reservations_across_roles_cannot_exceed_the_cap(store, clock):
    budget = make_budget(store, clock, calls_per_hour=1000)
    await budget.set_cap(1 * USD)

    async def attempt(i):
        role, amount = ("orchestrator", 240_000) if i % 2 else ("jev", 80_000)
        try:
            await store.atransaction(lambda conn: budget.reserve_in_tx(
                conn, reservation_id=f"r{i}", role=role, backend="openrouter", model="m",
                worst_case_micros=amount, now=clock.now()))
            return amount
        except BudgetExhausted:
            return 0

    granted = await asyncio.gather(*[attempt(i) for i in range(60)])
    snapshot = await budget.snapshot()
    assert sum(granted) == snapshot.committed_micros <= 1 * USD
    assert snapshot.committed_micros > 900_000  # the cap was actually used


@pytest.mark.asyncio
async def test_hourly_limit_is_a_delay_not_a_block_until_reset(store, clock):
    budget = make_budget(store, clock, calls_per_hour=3)
    await budget.set_cap(100 * USD)
    for i, gap in enumerate((0, 600, 600)):
        clock.advance(gap)
        reserve(budget, store, clock, f"r{i}", 1000)
    with pytest.raises(HourlyLimitReached) as caught:
        reserve(budget, store, clock, "r3", 1000)
    assert caught.value.retry_after_seconds == pytest.approx(3600 - 1200)
    assert caught.value.retry_at < next_window_start(clock.now())
    clock.advance(caught.value.retry_after_seconds + 1)
    reserve(budget, store, clock, "r3", 1000)  # admitted after the oldest call leaves the hour


@pytest.mark.asyncio
async def test_a_call_spanning_midnight_stays_in_its_own_window(store):
    clock = FakeClock(datetime(2026, 7, 2, 3, 59, 50, tzinfo=UTC))  # 23:59:50 EDT on July 1
    store.clock = clock
    budget = make_budget(store, clock)
    await budget.set_cap(300_000)
    reserve(budget, store, clock, "r1", 240_000)
    clock.advance(20)  # 00:00:10 EDT on July 2
    assert window_date(clock.now()) == "2026-07-02"
    reserve(budget, store, clock, "r2", 240_000)  # the new window starts at zero
    settle(budget, store, clock, "r1", 50_000)
    row = store.db.execute("SELECT window_date, state, actual_micros FROM ai_budget_reservations "
                           "WHERE reservation_id = 'r1'", fetch="one")
    assert row == ("2026-07-01", "SETTLED", 50_000)
    assert (await budget.snapshot()).committed_micros == 240_000  # only r2 counts today
    assert store.db.execute("SELECT count(*) FROM ai_budget_reservations", fetch="one")[0] == 2


@pytest.mark.asyncio
async def test_a_25_hour_day_does_not_reset_twice(store):
    clock = FakeClock(datetime(2026, 11, 1, 4, 30, tzinfo=UTC))  # 00:30 EDT, Nov 1 (25-hour day)
    budget = make_budget(store, clock)
    await budget.set_cap(300_000)
    reserve(budget, store, clock, "r1", 240_000)
    clock.advance(60 * 60)  # 01:30 EDT
    clock.advance(60 * 60)  # 01:30 EST (the repeated hour)
    with pytest.raises(BudgetExhausted):
        reserve(budget, store, clock, "r2", 240_000)
```

**Also write these tests** (each asserts what its name says; the rules above and the helper functions in the file are all they need): `test_reserving_before_a_cap_exists_is_a_loud_error`, `test_first_cap_is_applied_and_lowering_applies_at_once`, `test_raising_waits_for_the_next_new_york_midnight`, `test_reverting_the_config_cancels_a_pending_raise`, `test_reservation_over_the_cap_is_refused_with_the_reset_time`, `test_lowering_below_what_is_committed_refuses_new_work_and_keeps_old`, `test_reservations_survive_a_restart`, `test_settle_replaces_the_reservation_with_actual_cost_once`, `test_actual_cost_above_the_reservation_is_counted_not_hidden`, `test_release_returns_the_money_and_is_not_a_second_release`, `test_unknown_keeps_its_reservation_and_a_late_report_reconciles_once`, `test_a_normal_settle_cannot_close_an_unknown_reservation`, `test_recover_marks_open_reservations_unknown_and_counts_them`, `test_calls_proven_not_sent_do_not_use_the_hourly_allowance`, `test_windows_follow_new_york_local_dates_across_dst`.

- [ ] **Step 2: Run and see them fail.**
```bash
.venv/bin/python -m pytest tests/ai/test_budget.py -q --timeout=60
```
Expected: FAIL, `ModuleNotFoundError: No module named 'trader.ai.budget'`.

- [ ] **Step 3: Implement.** `trader/ai/budget.py`:

```python
"""Durable model budget (spec 5.4). Money is integer micro-USD.

Rules, all persisted in ai.duckdb:
- The daily window is the America/New_York calendar date. It is computed in Python from the
  injected clock and stored as text. Never read a date back from a DuckDB timestamp.
- A reservation belongs to the window it was made in, for its whole life. A call that crosses
  midnight settles in its own window. Nothing is reset twice or erased.
- Lowering the cap applies at once. Raising it applies at the first 00:00 New York after the
  request, and a restart never advances or delays that date.
- Worst-case reservation, the window total, the cap check and the hourly check run in one
  transaction, so concurrent roles cannot overspend.
- UNKNOWN keeps its reserved amount. A later usage report settles it once.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from trader.ai.clock import Clock
from trader.ai.store import AiStore, to_utc

NEW_YORK = ZoneInfo("America/New_York")

OPEN = "OPEN"
UNKNOWN_STATE = "UNKNOWN"
SETTLED = "SETTLED"
RELEASED = "RELEASED"

RELEASE_NOT_SENT = "NOT_SENT"
RELEASE_REJECTED = "REJECTED"


def window_date(instant: datetime) -> str:
    return instant.astimezone(NEW_YORK).date().isoformat()


def next_window_date(instant: datetime) -> str:
    return (instant.astimezone(NEW_YORK).date() + timedelta(days=1)).isoformat()


def next_window_start(instant: datetime) -> datetime:
    midnight = datetime.combine(instant.astimezone(NEW_YORK).date() + timedelta(days=1), time(0), tzinfo=NEW_YORK)
    return midnight.astimezone(timezone.utc)


class BudgetRefusal(Exception):
    code = ""


class BudgetNotInitialized(BudgetRefusal):
    code = "BUDGET_NOT_INITIALIZED"


class BudgetExhausted(BudgetRefusal):
    code = "BUDGET_EXHAUSTED"

    def __init__(self, *, window: str, cap_micros: int, committed_micros: int, needed_micros: int,
                 retry_at: datetime):
        super().__init__(f"window {window}: {committed_micros} + {needed_micros} > cap {cap_micros} micro-USD")
        self.window, self.cap_micros = window, cap_micros
        self.committed_micros, self.needed_micros, self.retry_at = committed_micros, needed_micros, retry_at


class HourlyLimitReached(BudgetRefusal):
    """A delay, not a block: the call may be retried at `retry_at`."""

    code = "HOURLY_LIMIT"

    def __init__(self, *, retry_at: datetime, retry_after_seconds: float):
        super().__init__(f"calls_per_hour reached; retry in {retry_after_seconds:.0f}s")
        self.retry_at, self.retry_after_seconds = retry_at, retry_after_seconds


class BudgetConflict(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


@dataclass(frozen=True)
class Reservation:
    reservation_id: str
    window_date: str
    reserved_micros: int
    state: str = OPEN
    actual_micros: Optional[int] = None


@dataclass(frozen=True)
class BudgetSnapshot:
    window_date: str
    effective_cap_micros: int
    pending_cap_micros: Optional[int]
    pending_window_date: Optional[str]
    committed_micros: int
    open_reservations: int
    unknown_reservations: int
    next_reset_at: datetime


class Budget:
    def __init__(self, store: AiStore, clock: Clock, *, calls_per_hour: int):
        if type(calls_per_hour) is not int or calls_per_hour < 1:
            raise ValueError("calls_per_hour must be a positive integer")
        self.store = store
        self.clock = clock
        self.calls_per_hour = calls_per_hour

    # --- cap -------------------------------------------------------------

    def _state(self, conn: Any) -> Optional[tuple]:
        return conn.execute("SELECT effective_cap_micros, pending_cap_micros, pending_window_date "
                            "FROM ai_budget_state WHERE id = 1").fetchone()

    def _event(self, conn: Any, kind: str, requested: int, now: datetime) -> None:
        effective, pending, pending_window = self._state(conn)
        conn.execute("INSERT INTO ai_budget_cap_events VALUES (?, ?, ?, ?, ?, ?)",
                     [now, kind, requested, effective, pending, pending_window])

    def _roll_in_tx(self, conn: Any, now: datetime) -> None:
        state = self._state(conn)
        if state is None or state[1] is None:
            return
        if window_date(now) >= state[2]:
            conn.execute("UPDATE ai_budget_state SET effective_cap_micros = ?, pending_cap_micros = NULL, "
                         "pending_window_date = NULL, updated_at = ? WHERE id = 1", [state[1], now])
            self._event(conn, "RAISE_APPLIED", state[1], now)

    def set_cap_in_tx(self, conn: Any, requested_micros: int, now: datetime) -> str:
        """Owner setting from config. Call at every start and whenever the owner changes it."""
        if type(requested_micros) is not int or requested_micros < 0:
            raise ValueError("the cap must be a non-negative integer of micro-USD")
        state = self._state(conn)
        if state is None:
            conn.execute("INSERT INTO ai_budget_state VALUES (1, ?, NULL, NULL, ?)", [requested_micros, now])
            self._event(conn, "INITIALIZED", requested_micros, now)
            return "INITIALIZED"
        self._roll_in_tx(conn, now)
        effective, pending, pending_window = self._state(conn)
        if requested_micros < effective:
            conn.execute("UPDATE ai_budget_state SET effective_cap_micros = ?, pending_cap_micros = NULL, "
                         "pending_window_date = NULL, updated_at = ? WHERE id = 1", [requested_micros, now])
            decision = "LOWERED"
        elif requested_micros == effective:
            conn.execute("UPDATE ai_budget_state SET pending_cap_micros = NULL, pending_window_date = NULL, "
                         "updated_at = ? WHERE id = 1", [now])
            decision = "UNCHANGED"
        elif pending == requested_micros:
            return "RAISE_KEPT"
        else:
            conn.execute("UPDATE ai_budget_state SET pending_cap_micros = ?, pending_window_date = ?, "
                         "updated_at = ? WHERE id = 1", [requested_micros, next_window_date(now), now])
            decision = "RAISE_SCHEDULED"
        self._event(conn, decision, requested_micros, now)
        return decision

    async def set_cap(self, requested_micros: int) -> str:
        now = self.clock.now()
        return await self.store.atransaction(lambda conn: self.set_cap_in_tx(conn, requested_micros, now))

    # --- reservations ----------------------------------------------------

    def _committed(self, conn: Any, window: str) -> int:
        return conn.execute(
            "SELECT COALESCE(SUM(CASE state WHEN 'SETTLED' THEN actual_micros WHEN 'RELEASED' THEN 0 "
            "ELSE reserved_micros END), 0) FROM ai_budget_reservations WHERE window_date = ?", [window]).fetchone()[0]

    def _check_hourly(self, conn: Any, now: datetime) -> None:
        rows = conn.execute(
            "SELECT created_at FROM ai_budget_reservations WHERE created_at > ? "
            "AND NOT (state = 'RELEASED' AND release_reason = 'NOT_SENT') ORDER BY created_at",
            [now - timedelta(hours=1)]).fetchall()
        if len(rows) >= self.calls_per_hour:
            leaves = to_utc(rows[len(rows) - self.calls_per_hour][0]) + timedelta(hours=1)
            raise HourlyLimitReached(retry_at=leaves, retry_after_seconds=max(0.0, (leaves - now).total_seconds()))

    def reserve_in_tx(self, conn: Any, *, reservation_id: str, role: str, backend: str, model: str,
                      worst_case_micros: int, now: datetime) -> Reservation:
        if type(worst_case_micros) is not int or worst_case_micros < 0:
            raise ValueError("worst_case_micros must be a non-negative integer")
        if self._state(conn) is None:
            raise BudgetNotInitialized("call set_cap at startup")
        self._roll_in_tx(conn, now)
        window = window_date(now)
        cap = self._state(conn)[0]
        committed = self._committed(conn, window)
        if committed + worst_case_micros > cap:
            raise BudgetExhausted(window=window, cap_micros=cap, committed_micros=committed,
                                  needed_micros=worst_case_micros, retry_at=next_window_start(now))
        self._check_hourly(conn, now)
        conn.execute(
            "INSERT INTO ai_budget_reservations (reservation_id, window_date, role, backend, model, state, "
            "reserved_micros, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [reservation_id, window, role, backend, model, OPEN, worst_case_micros, now])
        return Reservation(reservation_id, window, worst_case_micros)

    def get_in_tx(self, conn: Any, reservation_id: str) -> Reservation:
        row = conn.execute("SELECT reservation_id, window_date, reserved_micros, state, actual_micros "
                           "FROM ai_budget_reservations WHERE reservation_id = ?", [reservation_id]).fetchone()
        if row is None:
            raise BudgetConflict("RESERVATION_UNKNOWN", reservation_id)
        return Reservation(*row)

    def release_in_tx(self, conn: Any, reservation_id: str, *, reason: str, now: datetime) -> None:
        """Only for a call that provably produced no tokens: not sent, or rejected by the provider."""
        row = self.get_in_tx(conn, reservation_id)
        if row.state != OPEN:
            raise BudgetConflict("RESERVATION_STATE", f"{reservation_id} is {row.state}")
        conn.execute("UPDATE ai_budget_reservations SET state = ?, release_reason = ?, closed_at = ? "
                     "WHERE reservation_id = ?", [RELEASED, reason, now, reservation_id])

    def mark_unknown_in_tx(self, conn: Any, reservation_id: str, now: datetime) -> None:
        row = self.get_in_tx(conn, reservation_id)
        if row.state != OPEN:
            raise BudgetConflict("RESERVATION_STATE", f"{reservation_id} is {row.state}")
        conn.execute("UPDATE ai_budget_reservations SET state = ?, closed_at = ? WHERE reservation_id = ?",
                     [UNKNOWN_STATE, now, reservation_id])

    def settle_in_tx(self, conn: Any, reservation_id: str, *, actual_micros: int, input_tokens: int,
                     output_tokens: int, now: datetime, late: bool = False) -> bool:
        """Settle once. `late=True` is a usage report for an UNKNOWN reservation.
        Returns False for an exact repeat; a different figure is a conflict."""
        row = self.get_in_tx(conn, reservation_id)
        if row.state == SETTLED:
            if row.actual_micros == actual_micros:
                return False
            raise BudgetConflict("SETTLEMENT_CONFLICT", reservation_id)
        if row.state != (UNKNOWN_STATE if late else OPEN):
            raise BudgetConflict("RESERVATION_STATE", f"{reservation_id} is {row.state}")
        conn.execute("UPDATE ai_budget_reservations SET state = ?, actual_micros = ?, input_tokens = ?, "
                     "output_tokens = ?, closed_at = ? WHERE reservation_id = ?",
                     [SETTLED, actual_micros, input_tokens, output_tokens, now, reservation_id])
        return True

    def recover_open_in_tx(self, conn: Any, now: datetime) -> list[str]:
        """Process start only: nothing is in flight, so every OPEN reservation is UNKNOWN."""
        ids = [r[0] for r in conn.execute("SELECT reservation_id FROM ai_budget_reservations WHERE state = ?",
                                          [OPEN]).fetchall()]
        conn.execute("UPDATE ai_budget_reservations SET state = ?, closed_at = ? WHERE state = ?",
                     [UNKNOWN_STATE, now, OPEN])
        return ids

    def snapshot_in_tx(self, conn: Any, now: datetime) -> BudgetSnapshot:
        if self._state(conn) is None:
            raise BudgetNotInitialized("call set_cap at startup")
        self._roll_in_tx(conn, now)
        effective, pending, pending_window = self._state(conn)
        window = window_date(now)
        counts = dict(conn.execute("SELECT state, count(*) FROM ai_budget_reservations WHERE window_date = ? "
                                   "GROUP BY state", [window]).fetchall())
        return BudgetSnapshot(window, effective, pending, pending_window, self._committed(conn, window),
                              counts.get(OPEN, 0), counts.get(UNKNOWN_STATE, 0), next_window_start(now))

    async def snapshot(self) -> BudgetSnapshot:
        now = self.clock.now()
        return await self.store.atransaction(lambda conn: self.snapshot_in_tx(conn, now))
```

- [ ] **Step 4: Run and see them pass.**
```bash
.venv/bin/python -m pytest tests/ai/test_budget.py -q --timeout=60
```
Expected: 21 passed.

- [ ] **Step 5: Commit.**
```bash
git add trader/ai/budget.py tests/ai/test_budget.py
git commit -m "feat: add the durable model budget" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```


---

### Task 7: `ModelGateway`, `DecisionDeadline` and the call path

**Files:**
- Create: `trader/ai/gateway.py`
- Modify (tests): `tests/ai/fakes.py` (append `FakeProvider`)
- Create (tests): `tests/ai/world.py`, `tests/ai/test_gateway.py`

**Interfaces:**
- Consumes: `AiConfig`, `AiStore`, `Budget`, `AttemptJournal`, `ModelClient` adapters, `Clock`.
- Produces: `ModelGateway(*, config, store, clock, clients, budget=None, journal=None)` with `async start()`, `async recover_after_restart()`, `new_deadline(label="") -> DecisionDeadline`, `async call(role, request, deadline) -> GatewayResult`, `async report_late_usage(attempt_key, usage) -> bool`; `build_gateway(config, *, store, clock, environ, clients=None)`; `DecisionDeadline(clock, seconds, label="")` with `.remaining()` and `.expired`; `GatewayResult(response, attempt_key, cost_micros, reserved_micros, overrun=False, replayed=False)`; `CallRefused(code, detail, retry_at)`; `CallFailed(code, outcome, attempt_key, detail)` with `.outcome_unknown`; the `ModelCaller` protocol (`new_deadline`, `call`) that replay also implements.

Call path, in order:
1. Check role, output limit, input estimate and **price** (no database write yet). A missing price is `CallRefused("PRICE_UNAVAILABLE")` with no reservation.
2. Wait for one of at most `max_in_flight` (at most 2) slots, within the decision deadline.
3. One transaction: reserve the worst case and write the journal `STARTED` row. An hourly limit releases the slot, sleeps through the clock and retries if the wait fits the deadline; if not, `CallRefused("HOURLY_LIMIT_EXCEEDS_DEADLINE", retry_at=...)`. The daily cap gives `CallRefused("BUDGET_EXHAUSTED", retry_at=<next 00:00 New York>)`.
4. Call the adapter with `timeout = min(role timeout, deadline.remaining())`.
5. One transaction: journal finish, settle or release or mark unknown, and the cost event. Cancellation and timeouts become `UNKNOWN` and keep the reservation.

One `DecisionDeadline` object is passed to every call of a decision, so orchestrator plus Jev share one clock.

- [ ] **Step 1: Write the failing tests.**

Append to `tests/ai/fakes.py`:

```python
class FakeProvider:
    """A fake OpenRouter server behind a real httpx client and a real OpenRouterAdapter."""

    def __init__(self, *, usage=(1000, 200), clock=None, advance_seconds: float = 0.0, model="vendor/orch-1"):
        self.usage, self.clock, self.advance_seconds, self.model = usage, clock, advance_seconds, model
        self.requests: list = []
        self.in_flight = 0
        self.max_in_flight = 0
        self.hold: asyncio.Event | None = None
        self.respond = None  # optional callable(request) -> httpx.Response, or raises

    async def __call__(self, request):
        import httpx

        self.requests.append(request)
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if self.hold is not None:
                await self.hold.wait()
            if self.advance_seconds:
                self.clock.advance(self.advance_seconds)
            if self.respond is not None:
                return self.respond(request)
            return httpx.Response(200, json={
                "id": f"gen-{len(self.requests)}", "model": self.model,
                "choices": [{"message": {"content": '{"ok": true}'}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": self.usage[0], "completion_tokens": self.usage[1]}})
        finally:
            self.in_flight -= 1

    def adapter(self, model_id: str, api_key: str = "sk-test-key-0001"):
        import httpx

        from trader.ai.model_client import OpenRouterAdapter

        client = httpx.AsyncClient(transport=httpx.MockTransport(self))
        return OpenRouterAdapter(model_id=model_id, api_key=api_key, http_client=client)
```

`tests/ai/world.py`:

```python
"""A gateway wired to real adapters and fake providers, for gateway, replay and flow tests."""
from tests.ai.fakes import FakeProvider, load_test_config
from trader.ai.gateway import ModelGateway
from trader.ai.model_client import ChatMessage, ModelRequest
from trader.ai.store import AiStore


def request(key="d1/orchestrator/1", text="find ideas", max_output_tokens=500) -> ModelRequest:
    return ModelRequest(request_key=key, messages=(ChatMessage("user", text),), max_output_tokens=max_output_tokens)


class World:
    def __init__(self, tmp_path, clock, **config):
        self.clock = clock
        self.config = load_test_config(tmp_path, **config)
        self.store = AiStore(tmp_path / "ai.duckdb", clock=clock)
        orchestrator_model = self.config.role("orchestrator").model
        self.orchestrator = FakeProvider(clock=clock, model=orchestrator_model)
        self.jev = FakeProvider(clock=clock, model="vendor/jev-1")
        self.gateway = ModelGateway(
            config=self.config, store=self.store, clock=clock,
            clients={"orchestrator": self.orchestrator.adapter(orchestrator_model),
                     "jev": self.jev.adapter("vendor/jev-1")})

    def rows(self, sql):
        return self.store.db.execute(sql, fetch="all")
```

`tests/ai/test_gateway.py`:

```python
import asyncio
from datetime import datetime, timezone
import httpx
import pytest
import pytest_asyncio
from tests.ai.fakes import (
    JEV_WORST_CASE_MICROS, ORCHESTRATOR_WORST_CASE_MICROS,
)
from tests.ai.world import World, request
from trader.ai.config import AiConfigError
from trader.ai.gateway import CallFailed, CallRefused, ModelGateway, build_gateway
from trader.ai.model_client import Usage


SECRET = "sk-test-key-0001"
UTC = timezone.utc


@pytest_asyncio.fixture
async def world(tmp_path, clock):
    built = World(tmp_path, clock)
    await built.gateway.start()
    return built


@pytest.mark.asyncio
async def test_missing_price_refuses_and_leaves_no_trace(tmp_path, clock):
    built = World(tmp_path, clock, orchestrator_model="vendor/unpriced")
    await built.gateway.start()
    with pytest.raises(CallRefused) as caught:
        await built.gateway.call("orchestrator", request(), built.gateway.new_deadline())
    assert caught.value.code == "PRICE_UNAVAILABLE"
    assert built.rows("SELECT count(*) FROM ai_budget_reservations") == [(0,)]
    assert built.rows("SELECT count(*) FROM ai_model_attempts") == [(0,)]
    assert built.orchestrator.requests == []


@pytest.mark.asyncio
async def test_timeout_is_unknown_keeps_the_reservation_and_a_late_report_reconciles_once(tmp_path, clock):
    world = World(tmp_path, clock, call_timeout="0.05")
    await world.gateway.start()
    world.orchestrator.hold = asyncio.Event()  # never released: the call times out
    with pytest.raises(CallFailed) as caught:
        await world.gateway.call("orchestrator", request(), world.gateway.new_deadline())
    assert caught.value.outcome == "UNKNOWN" and caught.value.outcome_unknown and caught.value.code == "CALL_TIMEOUT"
    snapshot = await world.gateway.budget.snapshot()
    assert (snapshot.committed_micros, snapshot.unknown_reservations) == (ORCHESTRATOR_WORST_CASE_MICROS, 1)
    assert world.rows("SELECT kind, cost_micros FROM ai_cost_events") == [("ESTIMATED_UNKNOWN", ORCHESTRATOR_WORST_CASE_MICROS)]
    key = caught.value.attempt_key
    assert await world.gateway.report_late_usage(key, Usage(1000, 200)) is True
    assert await world.gateway.report_late_usage(key, Usage(1000, 200)) is False
    assert (await world.gateway.budget.snapshot()).committed_micros == 6000
    assert [r[0] for r in world.rows("SELECT kind FROM ai_cost_events ORDER BY event_seq")] == ["ESTIMATED_UNKNOWN", "CORRECTION"]


@pytest.mark.asyncio
async def test_malformed_usage_keeps_the_full_reservation(world):
    world.orchestrator.respond = lambda r: httpx.Response(200, json={
        "choices": [{"message": {"content": "x"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": "many"}})
    with pytest.raises(CallFailed) as caught:
        await world.gateway.call("orchestrator", request(), world.gateway.new_deadline())
    assert caught.value.outcome == "COST_UNKNOWN" and caught.value.outcome_unknown
    assert world.rows("SELECT status FROM ai_model_attempts") == [("COST_UNKNOWN",)]
    assert (await world.gateway.budget.snapshot()).committed_micros == ORCHESTRATOR_WORST_CASE_MICROS


@pytest.mark.asyncio
async def test_cancelling_a_call_records_unknown_and_keeps_the_reservation(world):
    world.orchestrator.hold = asyncio.Event()
    task = asyncio.create_task(world.gateway.call("orchestrator", request(), world.gateway.new_deadline()))
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert world.rows("SELECT status, error_code FROM ai_model_attempts") == [("UNKNOWN", "CALLER_CANCELLED")]
    assert (await world.gateway.budget.snapshot()).committed_micros == ORCHESTRATOR_WORST_CASE_MICROS
    follow_up = await world.gateway.call("jev", request("d/j/1"), world.gateway.new_deadline())  # the slot was freed
    assert follow_up.response.text


@pytest.mark.asyncio
async def test_restart_turns_a_half_finished_call_into_a_counted_unknown(tmp_path, clock):
    first = World(tmp_path, clock)
    await first.gateway.start()
    # simulate a crash after "reserve + journal begin" committed and before the adapter answered
    await first.gateway._reserve_and_begin("orchestrator", first.config.role("orchestrator"), request(),
                                           ORCHESTRATOR_WORST_CASE_MICROS)
    second = World(tmp_path, clock)  # new process, same ai.duckdb
    await second.gateway.start()
    assert second.rows("SELECT status, error_code FROM ai_model_attempts") == [("UNKNOWN", "PROCESS_RESTARTED")]
    snapshot = await second.gateway.budget.snapshot()
    assert (snapshot.unknown_reservations, snapshot.committed_micros) == (1, ORCHESTRATOR_WORST_CASE_MICROS)
    assert second.rows("SELECT kind FROM ai_cost_events") == [("ESTIMATED_UNKNOWN",)]
```

**Also write these tests** (each asserts what its name says; the rules above and the helper functions in the file are all they need): `test_success_prices_settles_and_journals`, `test_attempt_key_reaches_the_adapter_request_log_but_not_the_wire`, `test_request_limits_are_enforced_before_any_reservation`, `test_daily_cap_blocks_with_the_reset_time_and_sends_nothing`, `test_not_sent_and_rejected_release_the_reservation`, `test_at_most_two_calls_are_in_flight`, `test_one_deadline_spans_orchestrator_and_jev`, `test_hourly_limit_delays_inside_the_deadline_and_refuses_beyond_it`, `test_provider_billing_more_than_the_reservation_is_counted_as_an_overrun`, `test_config_and_client_must_agree`, `test_credentials_never_reach_the_database`.

- [ ] **Step 2: Run and see them fail.**
```bash
.venv/bin/python -m pytest tests/ai/test_gateway.py -q --timeout=60
```
Expected: FAIL, `ModuleNotFoundError: No module named 'trader.ai.gateway'`.

- [ ] **Step 3: Implement.** `trader/ai/gateway.py`:

```python
"""The one model call path: price -> slot -> reserve + journal begin -> adapter -> journal finish + settle.

Order matters. The price is looked up before anything is written, so a missing price leaves
no trace. Reservation and journal begin commit together, so there is no reservation without
an attempt row. Journal finish, settlement and the cost event commit together.
Callers (Plans 5 and 6) depend on the `ModelCaller` protocol, so replay can swap the gateway.
"""
from __future__ import annotations

import asyncio
import dataclasses
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Optional, Protocol

from trader.ai.budget import Budget, BudgetExhausted, HourlyLimitReached, RELEASE_NOT_SENT, RELEASE_REJECTED
from trader.ai.clock import Clock
from trader.ai.config import ROLE_NAMES, AiConfig, AiConfigError, ModelPrice, RoleConfig, usd_to_micros_floor
from trader.ai.journal import (
    COST_CONFIRMED, COST_CORRECTION, COST_ESTIMATED_UNKNOWN, COST_NONE, AttemptJournal, AttemptRecord,
)
from trader.ai.model_client import (
    NOT_SENT, REJECTED, ModelCallError, ModelClient, ModelRequest, ModelResponse, NotSentError,
    OutcomeUnknownError, Usage, build_model_client, estimate_input_tokens,
)
from trader.ai.store import AiStore

TIMEOUT_GRACE_SECONDS = 0.25


class CallRefused(Exception):
    """Refused before anything was reserved or sent. Safe to try again later."""

    def __init__(self, code: str, detail: str = "", *, retry_at: Optional[datetime] = None):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code, self.detail, self.retry_at = code, detail, retry_at


class CallFailed(Exception):
    """The call was reserved and journaled, and did not produce a usable answer.
    `outcome` is NOT_SENT, REJECTED, UNKNOWN or COST_UNKNOWN. Nothing may be submitted
    to the trader on the strength of a failed call."""

    def __init__(self, code: str, *, outcome: str, attempt_key: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code, self.outcome, self.attempt_key, self.detail = code, outcome, attempt_key, detail

    @property
    def outcome_unknown(self) -> bool:
        return self.outcome not in (NOT_SENT, REJECTED)


class DecisionDeadline:
    """One deadline for a whole decision. Pass the same object to the orchestrator and Jev calls."""

    def __init__(self, clock: Clock, seconds: float, label: str = ""):
        self._clock = clock
        self._ends_at = clock.monotonic() + seconds
        self.seconds = seconds
        self.label = label

    def remaining(self) -> float:
        return max(0.0, self._ends_at - self._clock.monotonic())

    @property
    def expired(self) -> bool:
        return self.remaining() <= 0.0


@dataclass(frozen=True)
class GatewayResult:
    response: ModelResponse
    attempt_key: str
    cost_micros: int
    reserved_micros: int
    overrun: bool = False
    replayed: bool = False


class ModelCaller(Protocol):
    def new_deadline(self, label: str = "") -> DecisionDeadline: ...

    async def call(self, role: str, request: ModelRequest, deadline: DecisionDeadline) -> GatewayResult: ...


@dataclass(frozen=True)
class _Begun:
    reservation_id: str
    reserved_micros: int
    attempt: AttemptRecord


class ModelGateway:
    def __init__(self, *, config: AiConfig, store: AiStore, clock: Clock, clients: Mapping[str, ModelClient],
                 budget: Optional[Budget] = None, journal: Optional[AttemptJournal] = None):
        for name in ROLE_NAMES:
            role, client = config.role(name), clients.get(name)
            if client is None or client.backend != role.backend or client.model_id != role.model:
                raise AiConfigError("CLIENT_ROLE_MISMATCH", f"the client for role {name!r} does not match ai.yaml")
        self.config, self.store, self.clock = config, store, clock
        self._clients = dict(clients)
        self.journal = journal or AttemptJournal(store)
        self.budget = budget or Budget(store, clock, calls_per_hour=config.budget.calls_per_hour)
        self._slots = asyncio.Semaphore(config.budget.max_in_flight)

    # --- lifecycle -------------------------------------------------------

    async def start(self) -> None:
        """Migrate, apply the configured cap, then turn leftovers of a dead process into UNKNOWN."""
        await asyncio.to_thread(self.store.migrate)
        await self.budget.set_cap(usd_to_micros_floor(self.config.budget.model_budget_usd_per_day))
        await self.recover_after_restart()

    async def recover_after_restart(self) -> None:
        now = self.clock.now()

        def work(conn: Any) -> None:
            for attempt in self.journal.mark_started_as_unknown_in_tx(conn, now):
                reserved = self.budget.get_in_tx(conn, attempt.reservation_id).reserved_micros
                self.journal.add_cost_event_in_tx(conn, attempt=attempt, kind=COST_ESTIMATED_UNKNOWN,
                                                  cost_micros=reserved, usage=None, now=now)
            self.budget.recover_open_in_tx(conn, now)

        await self.store.atransaction(work)

    def new_deadline(self, label: str = "") -> DecisionDeadline:
        return DecisionDeadline(self.clock, self.config.budget.decision_deadline_seconds, label)

    # --- the call --------------------------------------------------------

    async def call(self, role: str, request: ModelRequest, deadline: DecisionDeadline) -> GatewayResult:
        role_config, client, price = self._prepare(role, request)
        worst_case = price.cost_micros(role_config.max_input_tokens, role_config.max_output_tokens)
        begun = await self._acquire_slot_and_begin(role, role_config, request, worst_case, deadline)
        try:
            return await self._send_and_settle(client, role_config, price, request, begun, deadline)
        finally:
            self._slots.release()

    def _prepare(self, role: str, request: ModelRequest) -> tuple[RoleConfig, ModelClient, ModelPrice]:
        if role not in ROLE_NAMES:
            raise CallRefused("ROLE_UNKNOWN", role)
        role_config = self.config.role(role)
        if request.max_output_tokens > role_config.max_output_tokens:
            raise CallRefused("OUTPUT_LIMIT_ABOVE_ROLE", f"{request.max_output_tokens} > {role_config.max_output_tokens}")
        if estimate_input_tokens(request.messages) > role_config.max_input_tokens:
            raise CallRefused("INPUT_TOO_LARGE", f"limit {role_config.max_input_tokens} tokens")
        price = self.config.prices.price_for(role_config.backend, role_config.model)
        if price is None:
            raise CallRefused("PRICE_UNAVAILABLE", f"{role_config.backend}/{role_config.model} has no price")
        return role_config, self._clients[role], price

    def _require_time(self, deadline: DecisionDeadline) -> None:
        if deadline.expired:
            raise CallRefused("DEADLINE_EXPIRED", deadline.label)

    async def _acquire_slot_and_begin(self, role: str, role_config: RoleConfig, request: ModelRequest,
                                      worst_case: int, deadline: DecisionDeadline) -> _Begun:
        """Returns holding one in-flight slot. The caller releases it."""
        while True:
            self._require_time(deadline)
            try:
                await asyncio.wait_for(self._slots.acquire(), timeout=deadline.remaining())
            except TimeoutError:
                raise CallRefused("IN_FLIGHT_LIMIT_DEADLINE", f"at most {self.config.budget.max_in_flight} calls at once") from None
            try:
                return await self._reserve_and_begin(role, role_config, request, worst_case)
            except HourlyLimitReached as limit:
                self._slots.release()
                if limit.retry_after_seconds > deadline.remaining():
                    raise CallRefused("HOURLY_LIMIT_EXCEEDS_DEADLINE", retry_at=limit.retry_at) from None
                await self.clock.sleep(limit.retry_after_seconds + 0.001)
            except BudgetExhausted as exhausted:
                self._slots.release()
                raise CallRefused("BUDGET_EXHAUSTED", str(exhausted), retry_at=exhausted.retry_at) from None
            except BaseException:
                self._slots.release()
                raise

    async def _reserve_and_begin(self, role: str, role_config: RoleConfig, request: ModelRequest,
                                 worst_case: int) -> _Begun:
        now = self.clock.now()
        reservation_id = uuid.uuid4().hex

        def work(conn: Any) -> _Begun:
            reservation = self.budget.reserve_in_tx(
                conn, reservation_id=reservation_id, role=role, backend=role_config.backend,
                model=role_config.model, worst_case_micros=worst_case, now=now)
            attempt = self.journal.begin_in_tx(
                conn, request=request, role=role, backend=role_config.backend, model=role_config.model,
                reservation_id=reservation_id, now=now)
            return _Begun(reservation_id, reservation.reserved_micros, attempt)

        return await self.store.atransaction(work)

    async def _send_and_settle(self, client: ModelClient, role_config: RoleConfig, price: ModelPrice,
                               request: ModelRequest, begun: _Begun, deadline: DecisionDeadline) -> GatewayResult:
        timeout = min(role_config.call_timeout_seconds, deadline.remaining())
        if timeout <= 0:
            return await self._fail(begun, NotSentError("DEADLINE_EXPIRED", "no time left to send"))
        keyed = dataclasses.replace(request, attempt_key=begun.attempt.attempt_key)
        try:
            response = await asyncio.wait_for(client.complete(keyed, timeout_seconds=timeout),
                                              timeout + TIMEOUT_GRACE_SECONDS)
        except asyncio.CancelledError:
            await asyncio.shield(self._record_failure(begun, OutcomeUnknownError("CALLER_CANCELLED")))
            raise
        except TimeoutError:
            return await self._fail(begun, OutcomeUnknownError("CALL_TIMEOUT"))
        except ModelCallError as error:
            return await self._fail(begun, error)
        except Exception as error:
            return await self._fail(begun, OutcomeUnknownError("ADAPTER_FAILURE", type(error).__name__))
        return await self._record_success(price, begun, response)

    async def _record_success(self, price: ModelPrice, begun: _Begun, response: ModelResponse) -> GatewayResult:
        now = self.clock.now()
        cost = price.cost_micros(response.usage.input_tokens, response.usage.output_tokens)

        def work(conn: Any) -> None:
            self.journal.finish_success_in_tx(conn, begun.attempt.attempt_key, response, now)
            self.budget.settle_in_tx(conn, begun.reservation_id, actual_micros=cost,
                                     input_tokens=response.usage.input_tokens,
                                     output_tokens=response.usage.output_tokens, now=now)
            self.journal.add_cost_event_in_tx(conn, attempt=begun.attempt, kind=COST_CONFIRMED,
                                              cost_micros=cost, usage=response.usage, now=now)

        await self.store.atransaction(work)
        return GatewayResult(response, begun.attempt.attempt_key, cost, begun.reserved_micros,
                             overrun=cost > begun.reserved_micros)

    async def _record_failure(self, begun: _Begun, error: ModelCallError) -> None:
        now = self.clock.now()
        provably_none = error.outcome in (NOT_SENT, REJECTED)

        def work(conn: Any) -> None:
            self.journal.finish_failure_in_tx(conn, begun.attempt.attempt_key, outcome=error.outcome,
                                              error_code=error.code, error_detail=error.detail, now=now)
            if provably_none:
                self.budget.release_in_tx(conn, begun.reservation_id, reason=(
                    RELEASE_NOT_SENT if error.outcome == NOT_SENT else RELEASE_REJECTED), now=now)
                self.journal.add_cost_event_in_tx(conn, attempt=begun.attempt, kind=COST_NONE,
                                                  cost_micros=0, usage=None, now=now)
            else:
                self.budget.mark_unknown_in_tx(conn, begun.reservation_id, now)
                self.journal.add_cost_event_in_tx(conn, attempt=begun.attempt, kind=COST_ESTIMATED_UNKNOWN,
                                                  cost_micros=begun.reserved_micros, usage=None, now=now)

        await self.store.atransaction(work)

    async def _fail(self, begun: _Begun, error: ModelCallError) -> GatewayResult:
        await self._record_failure(begun, error)
        raise CallFailed(error.code, outcome=error.outcome, attempt_key=begun.attempt.attempt_key,
                         detail=error.detail)

    # --- later usage -----------------------------------------------------

    async def report_late_usage(self, attempt_key: str, usage: Usage) -> bool:
        """Usage for an UNKNOWN attempt arrived later. Settles its reservation once.
        Returns False for an exact repeat. Priced at the current ai.yaml price."""
        now = self.clock.now()

        def work(conn: Any) -> bool:
            attempt = self.journal.get_in_tx(conn, attempt_key)
            if attempt is None:
                raise CallRefused("ATTEMPT_UNKNOWN", attempt_key)
            price = self.config.prices.price_for(attempt.backend, attempt.model)
            if price is None:
                raise CallRefused("PRICE_UNAVAILABLE", f"{attempt.backend}/{attempt.model} has no price")
            if not self.journal.reconcile_late_usage_in_tx(conn, attempt_key, usage, now):
                return False
            cost = price.cost_micros(usage.input_tokens, usage.output_tokens)
            self.budget.settle_in_tx(conn, attempt.reservation_id, actual_micros=cost,
                                     input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
                                     now=now, late=True)
            self.journal.add_cost_event_in_tx(conn, attempt=attempt, kind=COST_CORRECTION,
                                              cost_micros=cost, usage=usage, now=now)
            return True

        return await self.store.atransaction(work)


def build_gateway(config: AiConfig, *, store: AiStore, clock: Clock, environ: Mapping[str, str],
                  clients: Optional[Mapping[str, ModelClient]] = None) -> ModelGateway:
    """Build real adapters from env unless the caller passes ready clients (tests do)."""
    built = {name: (clients or {}).get(name) or build_model_client(config.role(name), environ=environ)
             for name in ROLE_NAMES}
    return ModelGateway(config=config, store=store, clock=clock, clients=built)
```

- [ ] **Step 4: Run and see them pass.**
```bash
.venv/bin/python -m pytest tests/ai/test_gateway.py -q --timeout=60
```
Expected: 16 passed.

- [ ] **Step 5: Commit.**
```bash
git add trader/ai/gateway.py tests/ai/fakes.py tests/ai/world.py tests/ai/test_gateway.py
git commit -m "feat: add the model gateway with deadline, slots and settlement" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```


---

### Task 8: replay primitives

**Files:**
- Create: `trader/ai/replay.py`
- Modify (tests): `tests/ai/conftest.py` (append `no_network`)
- Create (tests): `tests/ai/test_replay.py`

**Interfaces:**
- Consumes: `AttemptJournal`, `AiStore`, `GatewayResult`, `CallFailed`, `DecisionDeadline`, tables from migrations 1 and 5.
- Produces: `ReplayRecorder(store)` with `async record_tool_result(decision_key, tool, args, result)`, `async record_clock_values(decision_key, values)`, `async record_manifest(decision_key, *, code_version, config_digest)`; `RecordingClock(inner)` (`.values`); `ReplayEvidence.load(store, decision_key)`; `ReplayModelClient(evidence)` (a `ModelClient` serving by `request.attempt_key`); `ReplayGateway(evidence, clock)` (a `ModelCaller`); `ReplayClock`; `ExternalAdapterCounter` (`record`, `total`, `count(name)`, `instrument(name, client)`, `tripwire(name)`); `ReplaySession(evidence, counter=...)` with `.clock`, `.gateway`, `.tool_result(tool, args)`, `run(work)`, `async arun(work)`, `assert_no_external_calls()`; `ReplayResult(status, value, missing)` with `COMPLETE` / `INCOMPLETE`; `ReplayIncomplete(missing)`, `ReplayDiverged`, `ExternalCallInReplay`.

Why `ReplayIncomplete` is an exception: any code that needs evidence cannot go on with an invented value. `ReplaySession.run` / `arun` catch it and return the explicit `INCOMPLETE` result with the missing item named. Replay has no fetch path at all: `tool_result` only reads recorded rows, the replay gateway has no adapter, and `tripwire` is the stand-in for any live tool (it counts the call and then raises).

- [ ] **Step 1: Write the failing tests.**

Append to `tests/ai/conftest.py`:

```python
@pytest.fixture
def no_network(monkeypatch):
    """Any socket connect or name lookup fails the test."""
    import socket

    def blocked(*args, **kwargs):
        raise AssertionError("network access during an offline test")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
```

`tests/ai/test_replay.py`:

```python
import httpx
import pytest
import pytest_asyncio
from tests.ai.world import World, request
from trader.ai.gateway import CallFailed
from trader.ai.replay import (
    COMPLETE, INCOMPLETE, ExternalAdapterCounter, ExternalCallInReplay, RecordingClock, ReplayDiverged,
    ReplayEvidence, ReplayIncomplete, ReplayRecorder, ReplaySession,
)


DECISION = "dec-1"


async def live_decision(world):
    """One live decision: a tool read, two clock reads, an orchestrator call and a Jev call."""
    recorder = ReplayRecorder(world.store)
    clock = RecordingClock(world.clock)
    started = clock.now()
    await recorder.record_tool_result(DECISION, "quote", {"conid": 4391}, {"last": 12.5, "delayed": True})
    await recorder.record_tool_result(DECISION, "quote", {"conid": 4391}, {"last": 12.6, "delayed": True})
    first = await world.gateway.call("orchestrator", request(f"{DECISION}/orchestrator/1", "ideas?"),
                                     world.gateway.new_deadline())
    second = await world.gateway.call("jev", request(f"{DECISION}/jev/1", "judge"), world.gateway.new_deadline())
    clock.now()
    await recorder.record_clock_values(DECISION, clock.values)
    await recorder.record_manifest(DECISION, code_version="abc123", config_digest=world.config.digest())
    return started, first, second


@pytest_asyncio.fixture
async def decided(tmp_path, clock):
    world = World(tmp_path, clock)
    await world.gateway.start()
    return world, await live_decision(world)


@pytest.mark.asyncio
async def test_replay_reproduces_the_decision_with_zero_external_calls(decided, no_network):
    world, (started, first, second) = decided
    provider_calls = len(world.orchestrator.requests) + len(world.jev.requests)
    session = ReplaySession(ReplayEvidence.load(world.store, DECISION))
    session.counter.instrument("openrouter", world.gateway._clients["orchestrator"])  # a live adapter that replay must never call

    async def decide(replay: ReplaySession):
        assert replay.clock.now() == started
        quote_1 = replay.tool_result("quote", {"conid": 4391})
        quote_2 = replay.tool_result("quote", {"conid": 4391})
        a = await replay.gateway.call("orchestrator", request(f"{DECISION}/orchestrator/1", "ideas?"),
                                      replay.gateway.new_deadline())
        b = await replay.gateway.call("jev", request(f"{DECISION}/jev/1", "judge"), replay.gateway.new_deadline())
        return quote_1["last"], quote_2["last"], a, b

    result = await session.arun(decide)
    assert result.status == COMPLETE
    last_1, last_2, a, b = result.value
    assert (last_1, last_2) == (12.5, 12.6)
    assert a.response == first.response and b.response == second.response and a.replayed
    assert session.evidence.manifest == {"code_version": "abc123", "config_digest": world.config.digest()}
    assert session.counter.total == 0
    session.assert_no_external_calls()
    assert len(world.orchestrator.requests) + len(world.jev.requests) == provider_calls


@pytest.mark.asyncio
async def test_the_counter_really_counts_a_live_adapter(decided):
    world, _ = decided
    counter = ExternalAdapterCounter()
    live = counter.instrument("openrouter", world.gateway._clients["jev"])
    await live.complete(request("probe/jev/1"), timeout_seconds=5)
    assert (counter.total, counter.count("openrouter")) == (1, 1)
    session = ReplaySession(ReplayEvidence.load(world.store, DECISION), counter)
    with pytest.raises(AssertionError):
        session.assert_no_external_calls()


@pytest.mark.asyncio
async def test_missing_evidence_is_an_incomplete_result_and_never_a_fetch(decided, no_network):
    world, _ = decided
    session = ReplaySession(ReplayEvidence.load(world.store, DECISION))
    fetch = session.counter.tripwire("alpaca")

    async def wants_more(replay: ReplaySession):
        replay.tool_result("quote", {"conid": 4391})
        replay.tool_result("quote", {"conid": 4391})
        return replay.tool_result("quote", {"conid": 4391})  # a third read was never recorded

    result = await session.arun(wants_more)
    assert (result.status, result.missing) == (INCOMPLETE, ("tool_result:quote#3",))
    assert session.counter.total == 0  # the code never reached for the live tool
    with pytest.raises(ExternalCallInReplay):
        fetch()
    assert session.counter.count("alpaca") == 1
```

**Also write these tests** (each asserts what its name says; the rules above and the helper functions in the file are all they need): `test_missing_attempt_and_missing_clock_are_incomplete`, `test_a_changed_prompt_is_a_divergence_not_a_silent_answer`, `test_recorded_failures_replay_as_failures_and_unknown_stays_unknown`, `test_a_tool_result_that_is_not_plain_json_is_refused_when_recorded`.

- [ ] **Step 2: Run and see them fail.**
```bash
.venv/bin/python -m pytest tests/ai/test_replay.py -q --timeout=60
```
Expected: FAIL, `ModuleNotFoundError: No module named 'trader.ai.replay'`.

- [ ] **Step 3: Implement.** `trader/ai/replay.py`:

```python
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

    def run(self, work: Callable[["ReplaySession"], Any]) -> ReplayResult:
        try:
            return ReplayResult(COMPLETE, work(self))
        except ReplayIncomplete as incomplete:
            return ReplayResult(INCOMPLETE, missing=(incomplete.missing,))

    async def arun(self, work: Callable[["ReplaySession"], Awaitable[Any]]) -> ReplayResult:
        try:
            return ReplayResult(COMPLETE, await work(self))
        except ReplayIncomplete as incomplete:
            return ReplayResult(INCOMPLETE, missing=(incomplete.missing,))

    def assert_no_external_calls(self) -> None:
        if self.counter.total:
            raise AssertionError(f"replay made {self.counter.total} external adapter calls")
```

**Not shown, write exactly as described:**
- `ReplayRecorder(store)`. Private `_insert_in_tx(conn, decision_key, kind, name, args_sha, payload, now)` picks `ordinal = COALESCE(MAX(ordinal), 0) + 1` over the same `(decision_key, kind, name, args_sha256)` and inserts `(decision_key, kind, name, args_sha, ordinal, _canonical(payload), now)` into `ai_replay_evidence`. `async record_tool_result(decision_key, tool, args, result)` first calls `_canonical(result)` (so a NaN raises `ValueError` and a non-JSON object `TypeError` before any write), then inserts with `kind=KIND_TOOL, name=tool, args_sha=_args_sha(args)`. `async record_clock_values(decision_key, values)` inserts one row per value (`KIND_CLOCK`, name `"now"`, `NO_ARGS_SHA`, payload `to_utc(value).isoformat()`). `async record_manifest(decision_key, *, code_version, config_digest)` inserts one `KIND_MANIFEST` row, name `"versions"`, payload `{"code_version": ..., "config_digest": ...}`. All run through `self.store.atransaction` and stamp `self.store.clock.now()`.
- `RecordingClock(inner)`: a `Clock` whose `now()` appends the inner value to `.values` and returns it; `monotonic()` and `sleep()` delegate.

- [ ] **Step 4: Run and see them pass.**
```bash
.venv/bin/python -m pytest tests/ai/test_replay.py -q --timeout=60
```
Expected: 7 passed.

- [ ] **Step 5: Commit.**
```bash
git add trader/ai/replay.py tests/ai/conftest.py tests/ai/test_replay.py
git commit -m "feat: add the replay primitives" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```


---

### Task 9: untrusted-input helpers (spec 8)

**Files:**
- Create: `trader/ai/untrusted.py`
- Create (tests): `tests/ai/test_untrusted.py`

**Interfaces:**
- Consumes: pydantic v2, `hypothesis` (already a test dependency).
- Produces: `StrictModelOutput` (base: `extra="forbid"`, `strict=True`, `frozen=True`; refuses at class definition any field named in `RESERVED_FIELD_NAMES`), `parse_model_output(text, schema) -> ParsedOutput[T] | OutputRefusal`, `ParsedOutput(value)`, `OutputRefusal(code, detail)`, `fence_untrusted(label, text, *, max_chars) -> str`, `RESERVED_FIELD_NAMES`.

Rules: exactly one JSON object, either the whole text or the single fenced block (prose around a bare object is refused; two blocks are refused); duplicate keys, `NaN`, `Infinity`, arrays at the top level and nesting deeper than 12 are refused; the object is then validated with `schema.model_validate_json` so `"5"`, `5.0` and `true` are not accepted for an int and a value outside a `Literal` menu is refused; refusal details name the field and error type, never the value. The function never raises for model text (a non-`StrictModelOutput` schema is a programming error and raises `TypeError`). Plan 6 builds the Jev and orchestrator schemas on this base, so code-owned fields (`decision_id`, `command_id`, `experiment_id`, `account_id`, `controller_epoch`, `epoch`, `evidence`, `policy_revision`, `expires_at`, `principal`, `attempt_key`, `request_key`) cannot appear in a model schema at all.

- [ ] **Step 1: Write the failing tests.** `tests/ai/test_untrusted.py`:

```python
from typing import Literal
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from trader.ai.untrusted import (
    OutputRefusal, ParsedOutput, StrictModelOutput, fence_untrusted, parse_model_output,
)


class Ruling(StrictModelOutput):
    verdict: Literal["TAKE", "SKIP", "REDUCE"]
    quantity: int | None = None
    reason: str


GOOD = '{"verdict": "SKIP", "reason": "thin volume"}'


def refusal(text: str) -> OutputRefusal:
    result = parse_model_output(text, Ruling)
    assert isinstance(result, OutputRefusal), result
    return result


@pytest.mark.parametrize(
    "text,code",
    [("", "OUTPUT_EMPTY"), ("   ", "OUTPUT_EMPTY"), ("x" * 200_001, "OUTPUT_TOO_LARGE"),
     ("I think we should TAKE it", "OUTPUT_NO_JSON"),
     (f"Sure. {GOOD}", "OUTPUT_NO_JSON"),                       # prose around a bare object
     (f"```json\n{GOOD}\n```\n```json\n{GOOD}\n```", "OUTPUT_MULTIPLE_JSON"),
     ('{"verdict": "TAKE", "reason": "a"', "OUTPUT_BAD_JSON"),
     (GOOD + " trailing", "OUTPUT_BAD_JSON"),
     ('{"verdict": "TAKE", "verdict": "SKIP", "reason": "a"}', "OUTPUT_DUPLICATE_KEY"),
     ('{"verdict": "TAKE", "reason": "a", "quantity": NaN}', "OUTPUT_BAD_JSON"),
     ('{"verdict": "TAKE", "reason": "a", "quantity": Infinity}', "OUTPUT_BAD_JSON"),
     ("```json\n[1, 2]\n```", "OUTPUT_NOT_OBJECT"),
     ("{" * 5000, "OUTPUT_BAD_JSON"),
     ('{"a":' * 20 + "1" + "}" * 20, "OUTPUT_TOO_DEEP")],
)
def test_malformed_output_is_a_typed_refusal(text, code):
    assert refusal(text).code == code


@pytest.mark.parametrize(
    "text",
    ['{"verdict": "BUY", "reason": "a"}',                                    # off the menu
     '{"verdict": "take", "reason": "a"}',                                   # case matters
     '{"verdict": "REDUCE", "quantity": "5", "reason": "a"}',                # string is not an int
     '{"verdict": "REDUCE", "quantity": true, "reason": "a"}',               # bool is not an int
     '{"verdict": "REDUCE", "quantity": 5.0, "reason": "a"}',                # float is not an int
     '{"verdict": "TAKE"}',                                                  # missing field
     '{"verdict": "TAKE", "reason": "a", "decision_id": "other-id"}',        # code-owned field injected
     '{"verdict": "TAKE", "reason": "a", "limit_price": 1}'],                # unknown field
)
def test_off_menu_or_wrongly_typed_output_is_a_schema_violation(text):
    result = refusal(text)
    assert result.code == "OUTPUT_SCHEMA_VIOLATION"
    assert "other-id" not in result.detail  # values are never echoed


def test_a_schema_cannot_declare_a_code_owned_field():
    with pytest.raises(TypeError, match="decision_id"):
        class Bad(StrictModelOutput):
            decision_id: str
```

**Also write these tests** (each asserts what its name says; the rules above and the helper functions in the file are all they need): `test_plain_json_and_one_fenced_block_are_accepted`, `test_the_schema_must_be_a_strict_output_model`, `test_arbitrary_text_never_raises_and_never_yields_a_ruling_by_accident`, `test_untrusted_text_cannot_close_its_block_or_carry_control_characters`, `test_untrusted_text_is_capped_and_labels_are_checked`.

- [ ] **Step 2: Run and see them fail.**
```bash
.venv/bin/python -m pytest tests/ai/test_untrusted.py -q --timeout=60
```
Expected: FAIL, `ModuleNotFoundError: No module named 'trader.ai.untrusted'`.

- [ ] **Step 3: Implement.** `trader/ai/untrusted.py`:

```python
"""Untrusted-input helpers (spec 8). Model text grants no authority.

`parse_model_output` never raises on bad model text. It returns a typed OutputRefusal for
anything malformed, off-menu or not exactly what the schema allows. Callers must treat a
refusal as "do not act" (never as TAKE, never as a default).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Generic, TypeVar, Union

from pydantic import BaseModel, ConfigDict, ValidationError

MAX_OUTPUT_CHARS = 200_000
MAX_DEPTH = 12
MAX_DETAIL_CHARS = 300

# Names that only code may set. A model schema may not declare them, so model output can never
# carry a value that code would then trust.
RESERVED_FIELD_NAMES = frozenset({
    "decision_id", "command_id", "experiment_id", "account_id", "controller_epoch", "epoch",
    "evidence", "policy_revision", "expires_at", "principal", "attempt_key", "request_key",
})

_FENCE = re.compile(r"```(?:json)?[ \t]*\r?\n(.*?)\r?\n[ \t]*```", re.DOTALL | re.IGNORECASE)
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

OUTPUT_EMPTY = "OUTPUT_EMPTY"
OUTPUT_TOO_LARGE = "OUTPUT_TOO_LARGE"
OUTPUT_NO_JSON = "OUTPUT_NO_JSON"
OUTPUT_MULTIPLE_JSON = "OUTPUT_MULTIPLE_JSON"
OUTPUT_BAD_JSON = "OUTPUT_BAD_JSON"
OUTPUT_DUPLICATE_KEY = "OUTPUT_DUPLICATE_KEY"
OUTPUT_NOT_OBJECT = "OUTPUT_NOT_OBJECT"
OUTPUT_TOO_DEEP = "OUTPUT_TOO_DEEP"
OUTPUT_SCHEMA_VIOLATION = "OUTPUT_SCHEMA_VIOLATION"


class StrictModelOutput(BaseModel):
    """Base for every schema a model must fill. Unknown fields, wrong types and coercions are errors."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    @classmethod
    def __pydantic_init_subclass__(cls, **kwargs: Any) -> None:
        super().__pydantic_init_subclass__(**kwargs)
        names = set(cls.model_fields) | {f.alias for f in cls.model_fields.values() if f.alias}
        clash = RESERVED_FIELD_NAMES & names
        if clash:
            raise TypeError(f"{cls.__name__} declares code-owned field(s): {', '.join(sorted(clash))}")


T = TypeVar("T", bound=StrictModelOutput)


@dataclass(frozen=True)
class ParsedOutput(Generic[T]):
    value: T


@dataclass(frozen=True)
class OutputRefusal:
    code: str
    detail: str = ""


class _Refuse(Exception):
    def __init__(self, code: str, detail: str = ""):
        self.code, self.detail = code, detail


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)):
        raise _Refuse(OUTPUT_DUPLICATE_KEY)
    return dict(pairs)


def _reject_constant(name: str) -> Any:
    raise _Refuse(OUTPUT_BAD_JSON, f"{name} is not allowed")


def _depth(value: Any) -> int:
    if isinstance(value, dict):
        return 1 + max((_depth(v) for v in value.values()), default=0)
    if isinstance(value, list):
        return 1 + max((_depth(v) for v in value), default=0)
    return 0


def extract_json_text(text: str) -> str:
    """Accept exactly one JSON object: the whole text, or the one fenced block in it.
    Prose around a bare object is refused, as are two blocks."""
    if not isinstance(text, str) or not text.strip():
        raise _Refuse(OUTPUT_EMPTY)
    if len(text) > MAX_OUTPUT_CHARS:
        raise _Refuse(OUTPUT_TOO_LARGE)
    stripped = text.strip()
    if stripped.startswith("{"):
        return stripped
    blocks = _FENCE.findall(stripped)
    if len(blocks) > 1:
        raise _Refuse(OUTPUT_MULTIPLE_JSON)
    if not blocks:
        raise _Refuse(OUTPUT_NO_JSON)
    return blocks[0].strip()


def parse_model_output(text: str, schema: type[T]) -> Union[ParsedOutput[T], OutputRefusal]:
    if not (isinstance(schema, type) and issubclass(schema, StrictModelOutput)):
        raise TypeError("schema must subclass StrictModelOutput")  # a programming error, not model input
    try:
        candidate = extract_json_text(text)
        try:
            loaded = json.loads(candidate, object_pairs_hook=_no_duplicate_keys, parse_constant=_reject_constant)
        except (json.JSONDecodeError, RecursionError):
            raise _Refuse(OUTPUT_BAD_JSON) from None
        if not isinstance(loaded, dict):
            raise _Refuse(OUTPUT_NOT_OBJECT)
        if _depth(loaded) > MAX_DEPTH:
            raise _Refuse(OUTPUT_TOO_DEEP)
        try:
            return ParsedOutput(schema.model_validate_json(candidate))
        except ValidationError as error:
            where = "; ".join(f"{'.'.join(map(str, e['loc']))}:{e['type']}" for e in error.errors())
            raise _Refuse(OUTPUT_SCHEMA_VIOLATION, where[:MAX_DETAIL_CHARS]) from None
    except _Refuse as refusal:
        return OutputRefusal(refusal.code, refusal.detail)


def fence_untrusted(label: str, text: str, *, max_chars: int) -> str:
    """Wrap untrusted text (news, headlines) for a prompt. The text cannot close the block,
    control characters are removed and the length is capped. The wrapper grants no authority."""
    if not re.fullmatch(r"[a-z0-9_]{1,40}", label):
        raise ValueError("label must be lower-case letters, digits or underscore")
    cleaned = _CONTROL.sub("", text)
    cleaned = re.sub(r"</?\s*untrusted[^>]*>", "[removed]", cleaned, flags=re.IGNORECASE)
    if len(cleaned) > max_chars:
        cleaned = cleaned[:max_chars] + " [truncated]"
    return f'<untrusted source="{label}">\n{cleaned}\n</untrusted>'
```

- [ ] **Step 4: Run and see them pass.**
```bash
.venv/bin/python -m pytest tests/ai/test_untrusted.py -q --timeout=60
```
Expected: 28 passed.

- [ ] **Step 5: Commit.**
```bash
git add trader/ai/untrusted.py tests/ai/test_untrusted.py
git commit -m "feat: add the untrusted model output helpers" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```


---

### Task 10: public API pin, container isolation, docs line and the full suite

**Files:**
- Modify: `AGENTS.md` (one line in "Top-level directories")
- Create (tests): `tests/ai/test_public_api.py`, `tests/ai/test_isolation.py`

**Interfaces:**
- Consumes: every module above.
- Produces: a test that fails when a name Plans 5 and 6 import is renamed or removed, and a test that fails when `trader.ai` starts importing the trading runtime.

- [ ] **Step 1: Write the tests.**

`tests/ai/test_public_api.py`:

```python
"""The names Plans 5 and 6 import. Renaming one of these breaks them, so the test pins them."""
import importlib

import pytest

PUBLIC_NAMES = {
    "trader.ai.clock": ["Clock", "SystemClock"],
    "trader.ai.config": ["AiConfig", "AiConfigError", "BudgetConfig", "ModelPrice", "PriceBook", "RoleConfig",
                         "ROLE_NAMES", "check_credentials", "load_ai_config", "micros_to_usd_str",
                         "usd_to_micros_floor"],
    "trader.ai.model_client": ["AzureOpenAIAdapter", "BedrockAdapter", "ChatMessage", "MalformedResponseError",
                               "MalformedUsageError", "ModelCallError", "ModelClient", "ModelRequest",
                               "ModelResponse", "NotSentError", "OpenRouterAdapter", "OutcomeUnknownError",
                               "ProviderRejectedError", "Usage", "build_model_client", "estimate_input_tokens"],
    "trader.ai.schema": ["FOUNDATION_MIGRATIONS", "Migration"],
    "trader.ai.store": ["AiStore", "to_utc"],
    "trader.ai.journal": ["AttemptJournal", "AttemptRecord", "CostEvent", "JournalError", "COST_CONFIRMED",
                          "COST_CORRECTION", "COST_ESTIMATED_UNKNOWN", "COST_NONE"],
    "trader.ai.budget": ["Budget", "BudgetConflict", "BudgetExhausted", "BudgetSnapshot", "HourlyLimitReached",
                         "next_window_start", "window_date"],
    "trader.ai.gateway": ["CallFailed", "CallRefused", "DecisionDeadline", "GatewayResult", "ModelCaller",
                          "ModelGateway", "build_gateway"],
    "trader.ai.replay": ["ExternalAdapterCounter", "ExternalCallInReplay", "RecordingClock", "ReplayClock",
                         "ReplayDiverged", "ReplayEvidence", "ReplayGateway", "ReplayIncomplete",
                         "ReplayModelClient", "ReplayRecorder", "ReplayResult", "ReplaySession"],
    "trader.ai.untrusted": ["OutputRefusal", "ParsedOutput", "RESERVED_FIELD_NAMES", "StrictModelOutput",
                            "fence_untrusted", "parse_model_output"],
}


@pytest.mark.parametrize("module,names", sorted(PUBLIC_NAMES.items()))
def test_public_names_exist(module, names):
    loaded = importlib.import_module(module)
    assert [name for name in names if not hasattr(loaded, name)] == []
```

`tests/ai/test_isolation.py`:

```python
"""The ai container holds model credentials only. Importing the package must not pull in the
trading runtime, the broker library or the market-data providers."""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FORBIDDEN = ("ib_async", "trader.trading", "trader.trader_service", "trader.data_providers",
             "trader.messaging", "trader.container", "alpaca")


def test_importing_the_ai_package_stays_clear_of_the_trading_runtime():
    code = (
        "import sys, importlib\n"
        "for name in ('clock','config','model_client','schema','store','journal','budget','gateway','replay','untrusted'):\n"
        "    importlib.import_module('trader.ai.' + name)\n"
        f"bad = [m for m in sys.modules if m.startswith({FORBIDDEN!r})]\n"
        "print(','.join(sorted(bad)))\n"
    )
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""
```

- [ ] **Step 2: Run them.**
```bash
.venv/bin/python -m pytest tests/ai/test_public_api.py tests/ai/test_isolation.py -q --timeout=60
```
Expected: 11 passed (the modules already exist, so these pass at once; they guard later changes).

- [ ] **Step 3: Document the package.** In `AGENTS.md`, under "Top-level directories", extend the `trader/` line: add `ai/` (model plumbing for the AI paper bot: config, model adapters, budget, journal, replay; no trader RPC; its database is `ai.duckdb`) to the list of sub-directories. Do not describe anything not built yet.

- [ ] **Step 4: Run the whole `tests/ai` directory and then the full suite once.**
```bash
.venv/bin/python -m pytest tests/ai -q --timeout=60
.venv/bin/python -m pytest tests/ -q --timeout=60 --ignore=tests/test_ibrx_async.py
```
Expected: `tests/ai`: 167 passed. Full suite: green. If a test outside `tests/ai` fails, check first whether it is a known flaky one on master before changing anything.

- [ ] **Step 5: Commit.**
```bash
git add AGENTS.md tests/ai/test_public_api.py tests/ai/test_isolation.py
git commit -m "test: pin the ai public api and keep the package off the trading runtime" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

