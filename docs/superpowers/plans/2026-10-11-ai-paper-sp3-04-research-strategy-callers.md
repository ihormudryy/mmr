# AI Paper SP3 — Plan 4: Research and strategy callers of the sandbox — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the `research` service evaluate an authored strategy and the `strategy` service run a deployed one, both only through the sandbox client (Plan 2), so no authored code runs in either process. Research gets the `STARTED` report, a smoke stage, availability retries, the cross-file trial count on Jev's case, attestation and the shadow replay for authored keys. The strategy service gets one live session per deployment per trading day (it serves every conid of the deployment, decision D1), the isolation and digest gates, an alarm on availability errors and retirement on a sandbox fault.

**Architecture:** Two caller modules plus small branches in SP2c code. Research: `trader/research/authored_backtest.py` (a proxy `Strategy` the unchanged backtester runs; it streams each conid's bars through one research session and answers `on_bar(i)` with the sandbox's decision for bar i), `trader/research/authored_evaluation.py` (`AuthoredEvaluator`: `STARTED` report, smoke stage, then the SP2c cohort evaluation; sandbox errors become an `AuthoredEnd`), `trader/research/authored_outcome.py` (the end, its durable marker, the report bodies). `EvaluationService` reports an end to the trader (`report_authored_evaluation`, Plan 3) and closes the claim FAILED without a case. Strategy: `trader/strategy/authored_runtime.py` (`AuthoredLiveStrategy`, the live proxy; `AuthoredDeployments`, the loader with the isolation and digest gates and the retirement queue). Shared: `trader/authored/streaming.py` (bar fields, a strictly ordered `BarStream` per conid of a session, `Decision` to `Signal`). Plan 3 widens the active-deployment wire model for the authored path (decision D4).

**Tech Stack:** Python 3.12, pandas, the SP2c research stack (`ExperimentRegistry`, `evaluate_cohort`, `EvaluationService`, `ShadowReplay`, `JudgmentAttest`), the backtester, the strategy runtime, typed RPC clients, pytest. No new dependency.

**Spec:** `docs/superpowers/specs/2026-10-11-ai-paper-sp3-sandboxed-codegen-design.md`, binding sections 7.6, 7.7, 8 and the section 10 bullets *pools*, *lookahead by construction*, *equivalence*, *trial count on the case*. Index: `docs/superpowers/plans/2026-10-11-ai-paper-sp3-00-index.md`. Depends on Plans 1, 2 and 3 (read as drafted on 2026-10-11).

## Global Constraints

- **Base:** master after SP3 Plans 1, 2 and 3 are merged. Code read at `d69fdcf2`.
- **No migrations.** Trader journal, `ai.duckdb` and the research DB get no table and no `ALTER` (index). The authored end is kept in the existing `research_requests.code` / `detail` / `pending_report` columns.
- **No new RPC method.** Plan 4 calls Plan 3's `report_authored_evaluation` (`research`), `retire_authored_strategy` (`strategy`) and `get_isolation_proof` (`strategy`, Plan 3 ruling 13). Plan 3's wire shape wins; each shape is built or read in one function: `started_body`, `AuthoredEnd.report_body`, `AuthoredEvaluator._start`, `EvaluationService._authored_outcome_delivered`, `AuthoredDeployments._send_retirement`, `proof_passes`.
- **Names from Plan 1, exactly:** `trader.authored.api.Decision`; `trader.authored.keys.{source_digest, authored_key, parse_authored_key, is_authored_key}`; `trader.authored.static_check.require_clean`.
- **Names from Plan 2, exactly:** `trader.authored.sandbox_client.{SandboxClient, SandboxInfo, SandboxUnavailable, SandboxFault, SandboxProtocolError}` (`SandboxFault(detail, *, code)`, attribute `code`; `SandboxInfo(image_digest, boot_id, pool, free, size, posture, live_open)`; `SandboxSession.image_digest`, the build `OPENED` reported, Plan 2 ruling 29); `SandboxClient.ping()`, `SandboxClient.open_session(digest, class_name, params, bar_size, conids)`, `SandboxSession.bar(conid, t, open, high, low, close, volume) -> Decision | None`, `SandboxSession.conids`, `SandboxSession.close()` (Plan 2 ruling 26: a live session holds one deployment's conids, a research session one conid); `trader.sandbox.protocol.{LIVE_SOCKET, RESEARCH_SOCKET, SANDBOX_ERROR, SANDBOX_BUSY, SANDBOX_DIGEST_MISMATCH, MAX_SESSION_CONIDS, parse_request}`; `trader.sandbox.supervisor.{Supervisor, LIVE_POOL_SIZE, RESEARCH_POOL_SIZE}`, `tests.sandbox.fakes.InProcessSpawner` (Task 11 only).
- **Names from Plan 3, exactly:** `trader.research.strategy_key.{AUTHORED_PATH, is_authored_path, authored_strategy_path, authored_digest_of_path, split_evaluation_key}` (an authored strategy path is `authored:sha256:<hex>`); `trader.messaging.ai_deployment_wire.ACTIVE_STRATEGY_PATH` (Plan 3 Task 3 Step 7); the wire shapes of `report_authored_evaluation`, `retire_authored_strategy` and `get_isolation_proof` in Plan 3 "Cross-plan additions".
- **Names from Plan 1, test fixture:** `tests/authored/fixtures/orb_authored.py` (`OpeningRangeBreakout`, with `MIN_BARS = 40`, Plan 1 ruling 10) for the equivalence test (decision D8).
- **No authored code in `research`, `strategy`, `trader` or `ai` processes** (spec 4). No `exec`, `import` or read of an authored file outside the sandbox. Tests run fixture sources only through `tests/authored/fake_sandbox.py` or Plan 2's in-process spawner.
- **Fail loudly.** A sandbox error ends the evaluation with its event, or it alarms (an ERROR log with a stable code) and is retried. Nothing returns an empty result in place of an error.
- **Paper only.** No order path changes. A fault never sends a close: open positions keep their stops (spec 7.6).
- **DuckDB** only through `DuckDBConnection`, as the SP2c modules already do.
- Test-first. Per task run only the listed tests. Full suite once, in Task 13, under the shared lock. No Docker, no IB, no network, no real model.
- Commit subjects `feat:` / `test:` / `docs:`, lowercase, imperative. Every message ends with a blank line and `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- No container restart, deploy, push or GitHub post is authorized by this plan.

## Rulings (spec silent, or the code forces a choice)

Task 13 checks these lines against the index.

1. **Plan 4 ruling 1:** The research proxy streams each conid's window through one session inside `precompute`, bar by bar in time order, and `on_bar(prices, state, i)` returns the decision for bar i; `state` holds decisions and bar times only. Streaming inside `on_bar` would hold 8–20 sessions at once (the backtester interleaves conids) and the research pool has 4. A decision for bar i was made when the session had been sent bars 0..i only, so lookahead stays impossible. "`precompute` returns {}" is not kept. The backtester's `precompute` does not name its conid, so a research session holds the one label `RESEARCH_SESSION_CONID = 1`; the strategy never sees a conid, and each conid still gets its own session (decision D1).
2. **Plan 4 ruling 2:** One research session is open at a time per job; authored cohorts run with `max_workers=1`; with the shadow replay thread, research holds at most 2 of its 4 sessions.
3. **Plan 4 ruling 3:** The authored strategy path is Plan 3's `authored:sha256:<hex>`; Plan 4 replaces the research-side `split_strategy_key` calls on case and request keys with `split_evaluation_key` (`cohort.py`, `case_builder.py`, `judgment_attest.py`, `shadow_replay.py`), as Plan 3 ruling 1 assigns.
4. **Plan 4 ruling 4:** `ActiveAiDeployment.strategy_path` accepts the authored path. The trader validates its own `get_active_ai_deployments` reply with this model and the strategy service parses it, so without the change one authored row breaks the reply for every deployment (Plan 3 widened only the registration validator).
5. **Plan 4 ruling 5:** For an authored key research checks no allowlist and scans no tunables (it has no store and never imports the file): the names in cohort point 0 are the tunables, and the trader's claim checks the switch, the record, the names and the ranges (Plan 3 ruling 7).
6. **Plan 4 ruling 6:** Before anything runs, research pings the research socket and reports `STARTED` with that image digest. A ping failure ends the run `SANDBOX_UNAVAILABLE`; a refused `STARTED` or a wire error parks the request (`ServiceStateError`); an unreachable trader ends the worker (SP2c infrastructure rule) and the restart reports again (`UNCHANGED`).
7. **Plan 4 ruling 7:** The smoke stage is one sandbox session over the first 20 XNYS sessions of the evaluation period (all before the holdout), on the cohort's smallest conid at cohort point 0, through the normal window job, before any family, trial or holdout exists.
8. **Plan 4 ruling 8:** Any `SANDBOX_ERROR` or `SANDBOX_DIGEST_MISMATCH` in an authored evaluation (smoke, folds or holdout) is reported `SANDBOX_ERROR` (`REJECTED`) with no case; a holdout opened before the fault stays revealed.
9. **Plan 4 ruling 9:** An availability error is reported `SANDBOX_UNAVAILABLE` (back to `STORED`; the trader counts attempts) when no holdout of this file opened during the run; after one opened it passes through and SP2c signs a FAILED case (spec 8).
10. **Plan 4 ruling 10:** The end is written to `research_requests.code = 'AUTHORED_ENDED'` / `detail` before anything is sent; the report goes before the claim's FAILED update, both resent every tick through `pending_report`; a restart with the marker never evaluates again. A final (non-retryable) refusal of the end is not delivery (review r1 #7): the request stays RUNNING with its claim open, an `AUTHORED_REPORT_REFUSED` ERROR names the request and digest, and the end is resent hourly and at every restart until the trader accepts it (the reconciliation path: repair or move the trader record, see `mmr ai-author list`).
11. **Plan 4 ruling 11:** An authored end has no case: `get_evaluation` reads FAILED with no case, the SP2c controller closes the candidate `EVALUATION_FAILED_NO_CASE`, and Jev is never asked.
12. **Plan 4 ruling 12:** For an authored key `strategy_trials` = the `judged_count` in the `STARTED` reply (Plan 3 ruling 19) + this file's finished trials. The same number is the deflated statistic's `n_trials`; the Sharpe spread comes from this file's trials. A catalogue key keeps today's count; a non-zero count with a catalogue spec is refused.
13. **Plan 4 ruling 13:** The research socket path is `EvaluationPaths.sandbox_socket` (default `RESEARCH_SOCKET`), so tests and Plan 5's world set one field and every caller that holds `paths` uses it.
14. **Plan 4 ruling 14 (decision D1):** One live session per deployment per New York date, opened with all of the deployment's conids. The first bar of each conid in a session (first bar, new date, after an availability error) streams that conid's trailing completed bars, at most 2000 (the largest `LOOKBACK_BARS`); only the newest bar's decision counts; then one new bar per call. A session rolls to a new date when any conid's newest bar is on a later New York date. An availability error or a fault ends the whole session (all conids).
15. **Plan 4 ruling 15:** A `Decision` becomes a `Signal` with `risk = 0.0` and no `close_by_time` (Jev and the risk gate size; the live rules flatten at 15:45 ET in the backtester and live).
16. **Plan 4 ruling 16:** A bar with a missing or non-finite field, or a time not after the last sent bar, is not sent and decides nothing. `BarStream` is the only guard against Plan 2's `SandboxProtocolError`, so every send goes through it.
17. **Plan 4 ruling 17:** An alarm is an ERROR log line that starts with a stable code: `AUTHORED_SANDBOX_UNAVAILABLE` (once per outage per instance, an INFO line on recovery), `AUTHORED_SANDBOX_FAULT`, `AUTHORED_ISOLATION_UNPROVEN`, `AUTHORED_DIGEST_MISMATCH`. The strategy service has no other alarm channel.
18. **Plan 4 ruling 18:** A fault disables the instance at once and sends nothing on the tick thread; the reconcile thread sends `retire_authored_strategy` until `UPDATED`/`UNCHANGED` (a `REFUSED` with `retryable: true`, such as `WITHDRAWAL_ENTRY_IN_FLIGHT`, is sent again; a non-retryable refusal stops the resend, logged). This process never reloads a retired digest. Plan 3 moves the record to `RETIRED` and withdraws the deployment version (Plan 3 ruling 18, decision D4), so `get_active_ai_deployments` stops listing it at once and a restart does not load it again.
19. **Plan 4 ruling 19 (review r1 #6, r2 #4):** The proof is never cached. `AuthoredDeployments.is_proven(build)` reads the authoritative `get_isolation_proof` for that build from the trader on **every** call and is fail-closed (False on a refusal, a transport error or a non-passing/other-build proof). The live proxy holds `is_proven` as its gate: it pings the live socket before every new session (first OPEN, reconnect, day rollover) and, if unproven, opens nothing; and on every bar it calls the gate for the build its session's `OPENED` reported (Plan 2 ruling 29). So a proof withdrawn for a build already running stops the next OPEN and the next bar with zero stale bars — no reconcile needed. An unproven or withdrawn build ends the session with an `AUTHORED_ISOLATION_UNPROVEN` alarm and no signal; the strategy stays deployed and nothing is retired. At load, no passing proof, an unreachable sandbox or an unreachable trader means not loaded, retried next reconcile.
20. **Plan 4 ruling 20:** At load the path's digest must equal the deployment's `strategy_digest` (else refused, alarm, retired `SANDBOX_DIGEST_MISMATCH`); on every dispatch `source_unchanged` asks the instance (not faulted, still the bound digest); the sandbox re-hashes the stored file at every OPEN.
21. **Plan 4 ruling 21:** An authored line is not renewed: SP2c renewal refuses a key off the allowlist (an authored key never is on it) and the file's one holdout is spent; the line ends `EXPIRED`.
22. **Plan 4 ruling 22 (resolved by decision D1, spec section 14):** Spec 7.5 sized the live pool at 9 as "3 deploys × up to 3 conids", but `max_cohort_points` counts parameter points and a deployment binds 8–20 conids. The spec is amended: one live session per deployment holds all its conids (Plan 2 ruling 26), the live pool is 3 and the trader caps `max_active` at 3 (controller decision C2). `SANDBOX_BUSY` live stays an availability error.
23. **Plan 4 ruling 23:** `attest_from_judgment` for an authored key checks the path's digest against the case instead of reading a file (no plan in the index owns this; without it an authored DEPLOY never gets a bundle).
24. **Plan 4 ruling 24 (dropped by decision D8):** the equivalence test uses Plan 1's `tests/authored/fixtures/orb_authored.py`, which now has the catalogue's `MIN_BARS = 40` gate (Plan 1 ruling 10) and matches the catalogue ORB bar for bar on this plan's synthetic bars. Plan 4 has no ORB port of its own.
25. **Plan 4 ruling 25 (owner ruling 2026-10-11, spec 14):** an authored evaluation uses the most recent `research_service.authored_evaluation_sessions` (default 302) sessions, with SP2c's folds and embargo and the holdout scaled in proportion (39 of 302); a catalogue evaluation keeps `period_sessions` (Task 12).

## Plan 2 handoff (2026-10-12)

What Plan 2 shipped that this plan's code and tests must follow:

- A sandbox-side failure (fork or socketpair, the child's privilege drop, rlimits or setup, an internal error) is the new code `SANDBOX_UNAVAILABLE` (Plan 2 ruling 34); `SandboxClient` raises `SandboxUnavailable` for it, as for `SANDBOX_BUSY`. Only `SANDBOX_ERROR` and `SANDBOX_DIGEST_MISMATCH` are faults of the strategy (rulings 8, 9 here are unchanged).
- An in-process harness that calls `Supervisor.handle` must give each session its own `conn_id` (one session per connection, Plan 2 ruling 7) and pass `wiper=lambda slot: True` (the real wiper forks a helper); Task 11's harness (`tests/authored/test_orb_equivalence.py`) does both.
- A harness that serves sockets (`trader.sandbox.server.serve`) must pass `socket_gid=os.getgid()` and use a short socket directory (`tempfile.mkdtemp(prefix="sbx", dir="/tmp")`): macOS caps a Unix socket path at 104 bytes.
- Plan 3 writes store files as regular 0644 files: the sandbox opens them with `O_NOFOLLOW` and refuses a symlink, a FIFO or an unreadable file (`SANDBOX_DIGEST_MISMATCH`, Plan 2 ruling 9).

## Cross-plan requirements

All six were settled at integration (2026-10-11); kept here so a reviewer can trace them.

- **R1 (Plan 2):** `OPEN` accepts any finite number for a declared tunable: research neighbours move ±10 % and may leave the range the author declared. *Done (decision D6): Plan 2 Task 1 `test_open_accepts_a_param_outside_any_declared_range`; the trader's claim checks the cohort points against the ranges (Plan 3 ruling 7).*
- **R2 (Plan 2, ruling 22):** the live pool size. *Done (decision D1): one live session per deployment, pool 3 (Plan 2 ruling 26); `max_active` ≤ 3 (C2).*
- **R3 (Plan 5):** the research socket is `EvaluationPaths.sandbox_socket` (ruling 13), not a keyword of `build_cohort_spec` or `evaluate_cohort`, so `ResearchWorld.research_options()` can stay `{}`. The live socket: `runtime._authored_deployments = AuthoredDeployments(client=SandboxClient(<live socket>), trader_query=..., trader_command=...)`. *Done (decision D7): Plan 5 sets `EvaluationPaths.sandbox_socket`.*
- **R4 (Plan 1, ruling 24):** add `MIN_BARS = 40` and its gate to `orb_authored.py`. *Done (decision D8): Plan 1 ruling 10.*
- **R5 (Plan 3, ruling 4):** Plan 3's tests that list an authored deployment through `get_active_ai_deployments` need the wider wire model. *Done (decision D4): the change moved to Plan 3 Task 3 Step 7; this plan's Task 1 only checks it.*
- **R6 (Plan 3, ruling 18):** `retire_authored_strategy` should also withdraw the active deployment version. *Done (decision D4): Plan 3 ruling 18.*

## Review Focus

1. **One authored row in `get_active_ai_deployments` must not stop catalogue deployments.** → Plan 3 `test_the_reply_carries_a_catalogue_and_an_authored_row` (run in Task 1); Task 10 `test_a_catalogue_and_an_authored_deployment_load_side_by_side`.
2. **A live frame with a NaN field or a repeated bar time must not reach the sandbox or end the session.** → Task 2 `test_a_bar_with_a_missing_value_is_not_sent`, `test_a_bar_not_after_the_last_sent_is_not_sent`; Task 9 `test_a_nan_newest_bar_gives_no_signal_and_keeps_the_session`.
3. **A window over 8–20 conids never holds more than one research session.** → Task 3 `test_an_authored_window_trades_like_the_catalogue_strategy`; Task 6 `test_an_authored_cohort_scores_like_the_catalogue_strategy` (`most_open == 1`).
4. **A fault on the tick thread never blocks on a trader call, and the faulted file never reloads in this process.** → Task 9 `test_a_fault_retires_through_the_callback_and_stops`; Task 10 `test_a_fault_retires_once_and_the_file_never_reloads`, `test_reconcile_refreshes_the_proof_and_sends_pending_retirements`.
5. **A crash between deciding an authored end and telling the trader never runs the evaluation again and never closes the claim before the record moved.** → Task 7 `test_an_end_decided_before_a_crash_is_never_evaluated_again`, `test_a_lost_report_holds_the_claim_open_until_the_next_tick`.

## File map

| File | Responsibility | Task |
|---|---|---|
| — (Plan 3 Task 3 Step 7 owns `trader/messaging/ai_deployment_wire.py`) | check the authored path in the active-deployment reply | 1 |
| `trader/authored/streaming.py`, `tests/authored/fake_sandbox.py`, `tests/authored/callers/*.py` | bar fields, ordered stream, decision to signal; the in-process fake and fixture sources | 2 |
| `trader/research/authored_backtest.py`, `trader/research/evaluation_jobs.py` | research proxy, window job branch | 3 |
| `trader/research/evaluation_spec.py`, `trader/research/cohort.py`, `trader/research/evaluation.py`, `trader/research/cohort_evaluation.py`, `tests/research/authored_fixtures.py` | authored cohort spec, family digest, commit, socket, run environment | 4 |
| `trader/research/evaluation.py`, `trader/research/cohort_evaluation.py`, `trader/research/case_builder.py` | trial count | 5 |
| `trader/research/authored_outcome.py`, `trader/research/authored_evaluation.py`, `trader/research/trader_port.py`, `tests/research/service_fakes.py` | `STARTED`, smoke stage, ends, `AuthoredEvaluator` | 6 |
| `trader/research/evaluation_service.py`, `trader/research/service_store.py`, `trader/research_service.py` | end report, FAILED without a case, wiring | 7 |
| `trader/research/judgment_attest.py`, `trader/research/shadow_replay.py` | attestation and shadow replay of authored keys | 8 |
| `trader/strategy/authored_runtime.py` (`AuthoredLiveStrategy`) | live proxy | 9 |
| `trader/strategy/authored_runtime.py` (`AuthoredDeployments`), `trader/strategy/ai_deployment_source.py`, `trader/strategy/strategy_runtime.py` | loading, gates, retirement, wiring | 10 |
| `tests/authored/test_orb_equivalence.py` | ORB equivalence through both proxies and Plan 2's child runtime | 11 |
| `trader/research/service_config.py`, `trader/research/cohort.py`, `config_defaults/trader.yaml`, `tests/research/test_authored_evaluation_length.py` | authored evaluation length (owner ruling, 302 sessions) | 12 |
| `docs/superpowers/plans/2026-10-11-ai-paper-sp3-00-index.md`, full suite | rulings for the owner | 13 |

---

### Task 1: The active-deployment reply carries authored rows (built in Plan 3)

Decision D4 moved this change to Plan 3 Task 3 Step 7 (`ACTIVE_STRATEGY_PATH`, `tests/strategy/test_authored_wire.py`): the trader validates its own `get_active_ai_deployments` reply with `ActiveAiDeployment`, so the trader's plan owns it. This task only checks that the base has it before the strategy-side tasks rely on it.

**Files:** none changed.

**Interfaces:**
- Consumes: Plan 3 `trader.messaging.ai_deployment_wire.ACTIVE_STRATEGY_PATH`, `ActiveAiDeployment`, `GetActiveAiDeploymentsResponse`; `tests/strategy/test_authored_wire.py`.
- Produces: nothing new.

- [ ] **Step 1: Run Plan 3's wire tests on this plan's base**

Run: `.venv/bin/python -m pytest tests/strategy/test_authored_wire.py tests/test_strategy_ai_deployments.py -q`
Expected: PASS. If `tests/strategy/test_authored_wire.py` does not exist, Plan 3 is not merged: stop and rebase on it (Global Constraints); do not add the change here.

---

### Task 2: Streaming helpers and the in-process fake sandbox

**Files:**
- Create: `trader/authored/streaming.py`, `tests/authored/fake_sandbox.py`, `tests/authored/callers/counts_bars.py`, `tests/authored/callers/faults_above_limit.py`, `tests/authored/callers/time_of_day_port.py` (`tests/authored/__init__.py` and the ORB fixture `tests/authored/fixtures/orb_authored.py` exist from Plan 1; decision D8)
- Test: `tests/authored/test_streaming.py`

**Interfaces:**
- Consumes: Plan 1 `Decision`, `source_digest(bytes) -> str`, `require_clean(source) -> str`, the ORB fixture file; Plan 2 `SandboxFault(detail, *, code)`, `SandboxUnavailable`, `SandboxProtocolError`, `SandboxInfo`, protocol codes, `MAX_SESSION_CONIDS`.
- Produces:
  - `trader.authored.streaming`: `OHLCV`, `AUTHORED_SIGNAL_RISK = 0.0`, `utc_time(stamp) -> datetime`, `bar_fields(stamp, values) -> Optional[dict]`, `BarStream(session, conid)` (one conid of a session) with `conid`, `last_sent`, `send(stamp, values) -> Optional[Decision]`, `send_frame(frame) -> tuple[Optional[Decision], ...]`, `close()`; `decision_signal(decision, source_name) -> Optional[Signal]`.
  - `tests.authored.fake_sandbox`: `FakeSandbox(pool=4, image_digest=IMAGE_DIGEST, max_conids=MAX_SESSION_CONIDS)` with `add(source) -> digest`, `ping() -> SandboxInfo`, `open_session(digest, class_name, params, bar_size, conids)`, knobs `unavailable`, `refuse_opens_after`, `tampered`, records `sessions`, `open_sessions`, `most_open`; `FakeSession.conids`, `.image_digest` (the sandbox's build at OPEN), `.stamps` (every bar sent, in order), `.stamps_of(conid)`, `.first_close`, `.closed`; `caller_source(name) -> str`, `orb_fixture_source() -> str`, `fault(code)`, `IMAGE_DIGEST`.

- [ ] **Step 1: Write the fixture sources**

They are authored files: each must pass Plan 1's static check (the fake runs `require_clean` at OPEN, as the sandbox does).

```python
# tests/authored/callers/counts_bars.py
from trader.authored.api import AuthoredStrategy, Decision


class CountsBars(AuthoredStrategy):
    LOOKBACK_BARS = 50

    def on_bar(self, history, params):
        return Decision("BUY", len(history) / 10000.0)
```

```python
# tests/authored/callers/faults_above_limit.py
from trader.authored.api import AuthoredStrategy, Decision


class FaultsAboveLimit(AuthoredStrategy):
    LOOKBACK_BARS = 5
    LIMIT = 150.0

    def on_bar(self, history, params):
        if history["close"].iloc[-1] > params["LIMIT"]:
            return Decision("BUY", 2.0)
        return None
```

```python
# tests/authored/callers/time_of_day_port.py
from trader.authored.api import AuthoredStrategy, Decision


class TimeOfDayPort(AuthoredStrategy):
    LOOKBACK_BARS = 1
    ENTRY_MINUTE = 600
    EXIT_MINUTE = 660

    def on_bar(self, history, params):
        local = history.index.tz_convert("America/New_York")
        minute = local.hour[-1] * 60 + local.minute[-1]
        if minute == params["ENTRY_MINUTE"]:
            return Decision("BUY", 0.5)
        if minute == params["EXIT_MINUTE"]:
            return Decision("SELL", 0.5)
        return None
```

- [ ] **Step 2: Write the fake**

```python
# tests/authored/fake_sandbox.py
"""Tests only: Plan 2's SandboxClient interface, served in this process.

No supervisor, no uid switch, no socket. It keeps the protocol rules both callers depend on (spec 7.5 as amended,
Plan 2 ruling 26): one class per session and one strategy instance and history per conid of the session, BAR times
strictly increasing per conid, the history trimmed to LOOKBACK_BARS, a conid outside the session a protocol error,
a full pool refused at once, and any exception or bad output a SandboxFault(SANDBOX_ERROR) that ends the whole
session. ``params`` are exactly the OPEN params, as Plan 2's child passes them (no class defaults). It loads only
fixture sources that pass Plan 1's static check.
"""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Mapping, Optional

import pandas as pd

from trader.authored.keys import source_digest
from trader.authored.sandbox_client import SandboxFault, SandboxInfo, SandboxProtocolError, SandboxUnavailable
from trader.authored.static_check import require_clean
from trader.sandbox.protocol import MAX_SESSION_CONIDS, SANDBOX_BUSY, SANDBOX_DIGEST_MISMATCH, SANDBOX_ERROR

CALLERS_DIR = Path(__file__).parent / "callers"
ORB_FIXTURE = Path(__file__).parent / "fixtures" / "orb_authored.py"     # Plan 1's ORB port (decision D8)
COLUMNS = ("open", "high", "low", "close", "volume")
IMAGE_DIGEST = "a1b2c3d-20261011T220000Z"            # a Plan 2 build id, as PONG reports it (decision D2)


def caller_source(name: str) -> str:
    return (CALLERS_DIR / f"{name}.py").read_text()


def orb_fixture_source() -> str:
    return ORB_FIXTURE.read_text()


def fault(code: str) -> SandboxFault:
    return SandboxFault(f"fake sandbox: {code}", code=code)


def _valid(decision: Any) -> bool:
    probability = getattr(decision, "probability", None)
    return (getattr(decision, "action", None) in ("BUY", "SELL")
            and isinstance(probability, (int, float)) and not isinstance(probability, bool)
            and math.isfinite(probability) and 0.0 <= probability <= 1.0)


class _ConidBook:
    def __init__(self, strategy: Any):
        self.strategy = strategy
        self.rows: list[tuple] = []
        self.stamps: list[pd.Timestamp] = []


class FakeSession:
    def __init__(self, sandbox: "FakeSandbox", strategy_class: type, params: Mapping[str, float], digest: str,
                 conids: tuple[int, ...]):
        self._sandbox, self.digest, self.conids = sandbox, digest, conids
        self.image_digest = sandbox.image_digest          # the build OPENED reported (Plan 2 ruling 29)
        self._params = dict(params)
        self._lookback = int(strategy_class.LOOKBACK_BARS)
        self._books = {conid: _ConidBook(strategy_class()) for conid in conids}
        self.stamps: list[pd.Timestamp] = []            # every bar sent, any conid, in send order
        self.first_close: Optional[float] = None
        self.closed = False

    def stamps_of(self, conid: int) -> list[pd.Timestamp]:
        return list(self._books[conid].stamps)

    def bar(self, conid, t, open, high, low, close, volume):
        if self.closed or self._sandbox.unavailable:
            raise SandboxUnavailable("fake: the session is gone")
        book = self._books.get(conid)
        if book is None:
            raise SandboxProtocolError(f"conid {conid} is not in this session")
        stamp = pd.Timestamp(t)
        assert stamp.tzinfo is not None, "BAR times are UTC-aware"
        assert not book.stamps or stamp > book.stamps[-1], "BAR times must strictly increase per conid"
        book.stamps.append(stamp)
        self.stamps.append(stamp)
        self.first_close = close if self.first_close is None else self.first_close
        book.rows = (book.rows + [(open, high, low, close, volume)])[-self._lookback:]
        history = pd.DataFrame(book.rows, columns=list(COLUMNS),
                               index=pd.DatetimeIndex(book.stamps[-len(book.rows):], name="date"))
        try:
            decision = book.strategy.on_bar(history, dict(self._params))
        except Exception as exc:
            self.close()
            raise fault(SANDBOX_ERROR) from exc
        if decision is not None and not _valid(decision):
            self.close()
            raise fault(SANDBOX_ERROR)
        return decision

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self._sandbox.open_sessions.remove(self)


class FakeSandbox:
    def __init__(self, *, pool: int = 4, image_digest: str = IMAGE_DIGEST, max_conids: int = MAX_SESSION_CONIDS):
        self.pool, self.image_digest, self.max_conids = pool, image_digest, max_conids
        self.sources: dict[str, str] = {}
        self.sessions: list[FakeSession] = []           # every session ever opened, in order
        self.open_sessions: list[FakeSession] = []
        self.most_open = 0
        self.unavailable = False                        # the socket is gone: every call raises SandboxUnavailable
        self.refuse_opens_after: Optional[int] = None   # after this many opens, every OPEN is unavailable
        self.tampered: set[str] = set()                 # stored files that no longer hash to their digest

    def add(self, source: str) -> str:
        digest = source_digest(source.encode())
        self.sources[digest] = source
        return digest

    def ping(self) -> SandboxInfo:
        if self.unavailable:
            raise SandboxUnavailable("fake: socket unreachable")
        return SandboxInfo(image_digest=self.image_digest, boot_id="fake", pool="fake",
                           free=self.pool - len(self.open_sessions), size=self.pool, posture={},
                           live_open=len(self.open_sessions))

    def open_session(self, digest: str, class_name: str, params: Mapping[str, float], bar_size: str,
                     conids) -> FakeSession:
        conids = tuple(int(conid) for conid in conids)
        if not 1 <= len(conids) <= self.max_conids or len(set(conids)) != len(conids):
            raise SandboxProtocolError(f"conids must be 1-{self.max_conids} distinct conids")
        refused = self.refuse_opens_after is not None and len(self.sessions) >= self.refuse_opens_after
        if self.unavailable or refused:
            raise SandboxUnavailable("fake: socket unreachable")
        if len(self.open_sessions) >= self.pool:
            raise SandboxUnavailable(SANDBOX_BUSY)
        if digest in self.tampered:
            raise fault(SANDBOX_DIGEST_MISMATCH)
        source = self.sources[digest]
        if require_clean(source) != class_name:
            raise fault(SANDBOX_ERROR)
        namespace: dict = {}
        exec(compile(source, f"<authored {digest}>", "exec"), namespace)   # fixture sources, test process only
        session = FakeSession(self, namespace[class_name], params, digest, conids)
        self.sessions.append(session)
        self.open_sessions.append(session)
        self.most_open = max(self.most_open, len(self.open_sessions))
        return session
```

- [ ] **Step 3: Write the failing tests**

```python
# tests/authored/test_streaming.py
import datetime as dt
import math

import pandas as pd
import pytest

from tests.authored.fake_sandbox import FakeSandbox, caller_source
from trader.authored.api import Decision
from trader.authored.sandbox_client import SandboxFault, SandboxProtocolError, SandboxUnavailable
from trader.authored.streaming import AUTHORED_SIGNAL_RISK, BarStream, bar_fields, decision_signal
from trader.objects import Action
from trader.sandbox.protocol import SANDBOX_ERROR

UTC = dt.timezone.utc
CONID, OTHER = 265598, 76792991


def bars(count, start="2024-03-04 14:30", close=100.0):
    index = pd.date_range(start, periods=count, freq="1min", tz="UTC", name="date")
    closes = [close + i for i in range(count)]
    return pd.DataFrame({"open": closes, "high": closes, "low": closes, "close": closes, "volume": 1000.0},
                        index=index)


def session_of(sandbox, name, class_name, params=None, conids=(CONID,)):
    return sandbox.open_session(sandbox.add(caller_source(name)), class_name, params or {}, "1 min", conids)


def test_bar_fields_are_floats_at_a_utc_time():
    fields = bar_fields(pd.Timestamp("2024-03-04 14:30"), (1, 2, 0.5, 1.5, 100))
    assert fields == {"t": dt.datetime(2024, 3, 4, 14, 30, tzinfo=UTC), "open": 1.0, "high": 2.0, "low": 0.5,
                      "close": 1.5, "volume": 100.0}


@pytest.mark.parametrize("values", [(1, 2, 0.5, math.nan, 100), (1, 2, 0.5, 1.5, None), (1, 2, 0.5, 1.5, math.inf),
                                    (1, 2, 0.5, 1.5)])
def test_a_bar_with_a_missing_value_is_not_sent(values):
    assert bar_fields(pd.Timestamp("2024-03-04 14:30", tz="UTC"), values) is None


def test_a_bar_not_after_the_last_sent_is_not_sent():
    sandbox = FakeSandbox()
    stream = BarStream(session_of(sandbox, "counts_bars", "CountsBars"), CONID)
    frame = bars(2)
    assert stream.send_frame(frame)[-1].probability == 2 / 10000
    assert stream.send(frame.index[0], (1, 1, 1, 1, 1)) is None
    assert len(sandbox.sessions[0].stamps) == 2


def test_the_history_is_the_bars_sent_capped_at_lookback():
    sandbox = FakeSandbox()
    decisions = BarStream(session_of(sandbox, "counts_bars", "CountsBars"), CONID).send_frame(bars(60))
    assert [d.probability for d in decisions] == [min(i + 1, 50) / 10000 for i in range(60)]


def test_two_conids_of_one_session_keep_their_own_history_and_clock():
    sandbox = FakeSandbox()
    session = session_of(sandbox, "counts_bars", "CountsBars", conids=(CONID, OTHER))
    first, second = BarStream(session, CONID), BarStream(session, OTHER)
    assert first.send_frame(bars(30))[-1].probability == 30 / 10000
    assert second.send_frame(bars(5))[-1].probability == 5 / 10000      # same times as CONID's first bars
    assert len(sandbox.sessions) == 1 and session.conids == (CONID, OTHER)


def test_a_conid_outside_the_session_is_a_protocol_error():
    sandbox = FakeSandbox()
    stream = BarStream(session_of(sandbox, "counts_bars", "CountsBars"), OTHER)
    with pytest.raises(SandboxProtocolError):
        stream.send_frame(bars(1))


def test_a_fault_ends_the_session_and_frees_its_slot():
    sandbox = FakeSandbox(pool=1)
    stream = BarStream(session_of(sandbox, "faults_above_limit", "FaultsAboveLimit", {"LIMIT": 101.5}), CONID)
    with pytest.raises(SandboxFault) as raised:
        stream.send_frame(bars(5))
    assert raised.value.code == SANDBOX_ERROR
    assert sandbox.open_sessions == []
    stream.close()                                   # closing a dead session is not an error


def test_a_full_pool_is_refused_at_once():
    sandbox = FakeSandbox(pool=1)
    session_of(sandbox, "counts_bars", "CountsBars")
    with pytest.raises(SandboxUnavailable):
        session_of(sandbox, "counts_bars", "CountsBars")


@pytest.mark.parametrize("action, expected", [("BUY", Action.BUY), ("SELL", Action.SELL)])
def test_a_decision_becomes_a_signal_without_risk(action, expected):
    signal = decision_signal(Decision(action, 0.25), "aidv-1")
    assert (signal.source_name, signal.action, signal.probability, signal.risk) == (
        "aidv-1", expected, 0.25, AUTHORED_SIGNAL_RISK)
    assert signal.close_by_time is None
    assert decision_signal(None, "aidv-1") is None
```

- [ ] **Step 4: Run them to verify they fail**

Run: `.venv/bin/python -m pytest tests/authored/test_streaming.py -q`
Expected: FAIL (`ModuleNotFoundError: No module named 'trader.authored.streaming'`).

- [ ] **Step 5: Implement `trader/authored/streaming.py`**

```python
"""Bars to a sandbox session and decisions back (SP3 spec 7.6), shared by the research and the live caller.

Both callers send the same fields the same way, so a strategy sees the same bars in a backtest and live. Every
send goes through BarStream: it is what keeps a caller inside the protocol (Plan 4 ruling 16).
"""
from __future__ import annotations

import datetime as dt
import logging
import math
from typing import Any, Optional, Sequence

import pandas as pd

from trader.authored.api import Decision
from trader.authored.sandbox_client import SandboxFault, SandboxUnavailable
from trader.objects import Action
from trader.trading.strategy import Signal

logger = logging.getLogger(__name__)
OHLCV = ("open", "high", "low", "close", "volume")
# A Decision carries no risk; Jev and the risk gate size the trade (Plan 4 ruling 15).
AUTHORED_SIGNAL_RISK = 0.0


def utc_time(stamp: Any) -> dt.datetime:
    moment = pd.Timestamp(stamp)
    moment = moment.tz_localize("UTC") if moment.tzinfo is None else moment.tz_convert("UTC")
    return moment.to_pydatetime()


def bar_fields(stamp: Any, values: Sequence[Any]) -> Optional[dict]:
    """The BAR fields of one completed bar, or None when a value is missing or not finite."""
    try:
        numbers = [float(value) for value in values]
    except (TypeError, ValueError):
        return None
    if len(numbers) != len(OHLCV) or not all(math.isfinite(number) for number in numbers):
        return None
    return {"t": utc_time(stamp), **dict(zip(OHLCV, numbers))}


class BarStream:
    """One conid of a sandbox session, fed strictly in time order. A bar that cannot be sent decides nothing.

    A live session holds every conid of its deployment, so several streams may share one session; a research
    session holds one (Plan 2 ruling 26)."""

    def __init__(self, session: Any, conid: int):
        self.session = session
        self.conid = conid
        self.last_sent: Optional[dt.datetime] = None

    def send(self, stamp: Any, values: Sequence[Any]) -> Optional[Decision]:
        fields = bar_fields(stamp, values)
        if fields is None or (self.last_sent is not None and fields["t"] <= self.last_sent):
            return None
        decision = self.session.bar(self.conid, **fields)
        self.last_sent = fields["t"]
        return decision

    def send_frame(self, frame: pd.DataFrame) -> tuple[Optional[Decision], ...]:
        """One decision per row of ``frame``, in row order."""
        rows = frame.loc[:, list(OHLCV)].itertuples(index=False, name=None)
        return tuple(self.send(stamp, values) for stamp, values in zip(frame.index, rows))

    def close(self) -> None:
        """Best effort: closes the whole session (every conid); a session whose child already died has nothing
        left to close."""
        try:
            self.session.close()
        except (SandboxFault, SandboxUnavailable) as exc:
            logger.debug("sandbox session close: %s", exc)


def decision_signal(decision: Optional[Decision], source_name: str) -> Optional[Signal]:
    if decision is None:
        return None
    action = Action.BUY if decision.action == "BUY" else Action.SELL
    return Signal(source_name=source_name, action=action, probability=float(decision.probability),
                  risk=AUTHORED_SIGNAL_RISK)
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/authored/test_streaming.py -q`
Expected: PASS. If `require_clean` refuses a fixture source, the fixture breaks Plan 1's rules: fix the fixture, never the checker.

- [ ] **Step 7: Commit**

```bash
git add trader/authored/streaming.py tests/authored/fake_sandbox.py tests/authored/callers/ tests/authored/test_streaming.py
git commit -m "feat: stream bars to a sandbox session in order and map decisions to signals

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: The research proxy and the window job

**Files:**
- Create: `trader/research/authored_backtest.py`
- Modify: `trader/research/evaluation_jobs.py` (`RunEnvironment`, `run_window_job`)
- Test: `tests/research/test_authored_backtest.py`

**Interfaces:**
- Consumes: Task 2 `BarStream`, `decision_signal`; Plan 2 `SandboxClient`; `Backtester.run(strategy, conids)`; Plan 3 `authored_strategy_path`.
- Produces: `trader.research.authored_backtest.connect_sandbox(socket_path) -> SandboxClient` (tests patch it), `RESEARCH_SESSION_CONID = 1` (ruling 1), `AuthoredProxyStrategy(*, client, digest, class_name, params, bar_size)`, `AuthoredReplayError`, `run_authored_backtest(backtester, env, params, universe_accessor) -> BacktestResult`; `RunEnvironment.authored_digest: Optional[str] = None`, `RunEnvironment.sandbox_socket: Optional[str] = None`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/research/test_authored_backtest.py
import datetime as dt
import pickle
from dataclasses import replace

import pandas as pd
import pytest

from tests.authored.fake_sandbox import FakeSandbox, caller_source
from tests.research.evaluation_fixtures import (CONIDS, build_spec_file, write_costs_config, write_trend_bars,
                                               write_universe)
from trader.authored.sandbox_client import SandboxFault, SandboxUnavailable
from trader.research.authored_backtest import RESEARCH_SESSION_CONID, AuthoredProxyStrategy, AuthoredReplayError
from trader.research.evaluation_jobs import RunEnvironment, WindowJob, run_window_job
from trader.research.strategy_key import authored_strategy_path

UTC = dt.timezone.utc
FEB = (dt.datetime(2024, 2, 1, tzinfo=UTC), dt.datetime(2024, 2, 9, 23, 59, tzinfo=UTC))
PARAMS = {"ENTRY_MINUTE": 600, "EXIT_MINUTE": 660}


def bars(count, start="2024-03-04 14:30"):
    index = pd.date_range(start, periods=count, freq="1min", tz="UTC", name="date")
    closes = [100.0 + i for i in range(count)]
    return pd.DataFrame({"open": closes, "high": closes, "low": closes, "close": closes, "volume": 1000.0},
                        index=index)


@pytest.fixture
def sandbox(monkeypatch):
    fake = FakeSandbox()
    monkeypatch.setattr("trader.research.authored_backtest.connect_sandbox", lambda socket_path: fake)
    return fake


def proxy(sandbox, name, class_name, params=None):
    return AuthoredProxyStrategy(client=sandbox, digest=sandbox.add(caller_source(name)), class_name=class_name,
                                 params=params or {}, bar_size="1 min")


def test_each_decision_saw_only_the_bars_up_to_it(sandbox):
    frame = bars(60)
    strategy = proxy(sandbox, "counts_bars", "CountsBars")
    state = strategy.precompute(frame)
    probabilities = [strategy.on_bar(frame.iloc[:i + 1], state, i).probability for i in range(len(frame))]
    assert probabilities == [min(i + 1, 50) / 10000 for i in range(len(frame))]
    assert [len(s.stamps) for s in sandbox.sessions] == [60]
    assert [s.conids for s in sandbox.sessions] == [(RESEARCH_SESSION_CONID,)]    # one conid per research session
    assert sandbox.open_sessions == []


def test_a_fault_is_raised_at_the_first_bar_and_later_conids_open_nothing(sandbox):
    frame = bars(5)
    strategy = proxy(sandbox, "faults_above_limit", "FaultsAboveLimit", {"LIMIT": 101.5})
    state = strategy.precompute(frame)
    with pytest.raises(SandboxFault):
        strategy.on_bar(frame.iloc[:1], state, 0)
    assert strategy.precompute(frame) == {}
    assert len(sandbox.sessions) == 1 and sandbox.open_sessions == []


def test_an_unreachable_sandbox_is_an_availability_error(sandbox):
    sandbox.unavailable = True
    frame = bars(3)
    strategy = proxy(sandbox, "counts_bars", "CountsBars")
    state = strategy.precompute(frame)
    with pytest.raises(SandboxUnavailable):
        strategy.on_bar(frame.iloc[:1], state, 0)


def test_a_bar_the_decisions_were_not_made_for_is_refused(sandbox):
    strategy = proxy(sandbox, "counts_bars", "CountsBars")
    state = strategy.precompute(bars(3))
    with pytest.raises(AuthoredReplayError):
        strategy.on_bar(bars(3, start="2024-03-05 14:30").iloc[:2], state, 1)


def test_the_proxy_never_runs_live(sandbox):
    with pytest.raises(AuthoredReplayError):
        proxy(sandbox, "counts_bars", "CountsBars").on_prices(bars(3))


@pytest.fixture
def market(tmp_path, tmp_duckdb_path):
    write_universe(tmp_duckdb_path)
    write_trend_bars(tmp_duckdb_path, drift=0.0006)
    costs = write_costs_config(tmp_path / "execution_costs.yaml")
    build_spec_file(tmp_path)
    return RunEnvironment(
        history_db=tmp_duckdb_path, universe_db=tmp_duckdb_path, universe_library="Universes",
        execution_costs_path=str(costs), strategy_file=str(tmp_path / "strategies" / "time_of_day.py"),
        class_name="TimeOfDay", conids=tuple(CONIDS), bar_size="15 mins", order_notional=1900.0,
        account_equity=100_000.0, max_gross_allocation=0.05, regular_session_calendar="XNYS")


def authored(env, sandbox):
    digest = sandbox.add(caller_source("time_of_day_port"))
    return replace(env, strategy_file=authored_strategy_path(digest), class_name="TimeOfDayPort",
                   authored_digest=digest, sandbox_socket="/run/sandbox-research/sandbox.sock")


def job():
    return WindowJob(point_key="p", params=PARAMS, window_kind="fold", window_index=0, start=FEB[0], end=FEB[1],
                     cost_multiplier=1.0)


def fills(outcome):
    return [(t["timestamp"], t["conid"], t["action"], t["quantity"], t["price"]) for t in outcome.trades]


@pytest.mark.timeout(240)
def test_an_authored_window_trades_like_the_catalogue_strategy(market, sandbox):
    catalogue = run_window_job(market, job())
    ported = run_window_job(authored(market, sandbox), job())
    assert fills(ported) == fills(catalogue) and fills(ported)
    assert ported.net_pnl == pytest.approx(catalogue.net_pnl)
    assert len(sandbox.sessions) == len(CONIDS)
    assert sandbox.most_open == 1                    # Review Focus 3: one session at a time fits the pool of 4


def test_an_authored_environment_pickles_for_the_process_pool(market, sandbox):
    env = authored(market, sandbox)
    assert pickle.loads(pickle.dumps(env)) == env
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -m pytest tests/research/test_authored_backtest.py -q`
Expected: FAIL (`ModuleNotFoundError: No module named 'trader.research.authored_backtest'`).

- [ ] **Step 3: Implement `trader/research/authored_backtest.py`**

```python
"""Backtests of an authored strategy (SP3 spec 7.6): its code runs only in the research sandbox.

The backtester walks all conids together, bar by bar. Holding one session per conid through that walk would need
up to 20 sessions at once; the research pool has 4. So ``precompute`` streams each conid's bars through one
session, one bar at a time in time order, and keeps only the decisions; ``on_bar(prices, state, i)`` returns the
decision for bar i. When the sandbox decided bar i it had been sent bars 0..i only, so no decision can look ahead
(Plan 4 ruling 1). Fills at the next open, costs, live rules and P&L stay the backtester's.
"""
from __future__ import annotations

import logging
from typing import Any, Mapping, Optional

import pandas as pd

from trader.authored.sandbox_client import SandboxClient, SandboxFault, SandboxUnavailable
from trader.authored.streaming import BarStream, decision_signal
from trader.objects import BarSize
from trader.simulation.backtester import Backtester, BacktestResult
from trader.trading.strategy import Signal, Strategy, StrategyContext, StrategyState


class AuthoredReplayError(RuntimeError):
    """The proxy holds no decision for a bar the backtester asked about: a bug or a failure outside the
    sandbox, never a strategy fault."""


# The backtester's precompute does not name its conid. A research session holds exactly one conid and the
# strategy never sees it, so one fixed label is enough; each conid still gets its own session (ruling 1, D1).
RESEARCH_SESSION_CONID = 1


def connect_sandbox(socket_path: str) -> SandboxClient:
    """The research socket's client. Tests replace this function with an in-process fake."""
    return SandboxClient(socket_path)


class AuthoredProxyStrategy(Strategy):
    def __init__(self, *, client: Any, digest: str, class_name: str, params: Mapping[str, float], bar_size: str):
        super().__init__()
        self._client, self._digest, self._class_name = client, digest, class_name
        self._params, self._bar_size = dict(params), bar_size
        self._failure: Optional[Exception] = None

    def precompute(self, prices: pd.DataFrame) -> dict:
        """One session for this conid's bars. A sandbox error is kept and raised by ``on_bar``, because the
        backtester logs and drops what ``precompute`` raises."""
        if self._failure is not None:
            return {}
        stream = None
        try:
            session = self._client.open_session(self._digest, self._class_name, self._params, self._bar_size,
                                                [RESEARCH_SESSION_CONID])
            stream = BarStream(session, RESEARCH_SESSION_CONID)
            return {"stamps": prices.index, "decisions": stream.send_frame(prices)}
        except (SandboxFault, SandboxUnavailable) as exc:
            self._failure = exc
            return {}
        finally:
            if stream is not None:
                stream.close()

    def on_bar(self, prices: pd.DataFrame, state: dict, index: int) -> Optional[Signal]:
        if not state:
            raise self._failure or AuthoredReplayError(
                f"no sandbox decisions for this conid at bar {index}: precompute failed outside the sandbox")
        if prices.index[-1] != state["stamps"][index]:
            raise AuthoredReplayError(
                f"bar {index} is {prices.index[-1]}, its decision was made for {state['stamps'][index]}")
        return decision_signal(state["decisions"][index], self.name)

    def on_prices(self, prices: pd.DataFrame) -> Optional[Signal]:
        raise AuthoredReplayError("the research proxy runs inside the backtester only")


def run_authored_backtest(backtester: Backtester, env: Any, params: Mapping[str, float],
                          universe_accessor: Any) -> BacktestResult:
    proxy = AuthoredProxyStrategy(client=connect_sandbox(env.sandbox_socket), digest=env.authored_digest,
                                  class_name=env.class_name, params=params, bar_size=env.bar_size)
    proxy.install(StrategyContext(
        name=env.class_name, bar_size=BarSize.parse_str(env.bar_size), conids=list(env.conids), universe=None,
        historical_days_prior=0, paper_only=False, storage=backtester.storage, universe_accessor=universe_accessor,
        logger=logging, module=env.strategy_file, class_name=env.class_name))
    proxy.state = StrategyState.RUNNING
    return backtester.run(proxy, list(env.conids))
```

- [ ] **Step 4: Branch the window job**

In `trader/research/evaluation_jobs.py` add the import and the two fields, and replace the backtest call:

```python
from trader.research.authored_backtest import run_authored_backtest
```

```python
@dataclass(frozen=True)
class RunEnvironment:
    history_db: str
    universe_db: str
    universe_library: str
    execution_costs_path: str
    strategy_file: str
    class_name: str
    conids: tuple[int, ...]
    bar_size: str
    order_notional: float
    account_equity: float
    max_gross_allocation: float
    # An evaluation backtests regular-session bars only, the bars its dataset sealed; None reads every stored bar.
    regular_session_calendar: Optional[str] = None
    # An authored strategy (SP3): strategy_file is then its store path, read by the sandbox only.
    authored_digest: Optional[str] = None
    sandbox_socket: Optional[str] = None
```

```python
def run_window_job(env: RunEnvironment, job: WindowJob) -> WindowOutcome:
    storage = TickStorage(env.history_db)
    if env.regular_session_calendar is not None:
        storage = RegularSessionStorage(storage, env.regular_session_calendar)
    accessor = UniverseAccessor(env.universe_db, env.universe_library)
    costs = build_realistic_costs(
        env.conids, accessor, load_execution_costs_config(env.execution_costs_path)
    ).scaled(job.cost_multiplier)
    config = BacktestConfig(
        start_date=job.start, end_date=job.end, initial_capital=env.account_equity,
        bar_size=BarSize.parse_str(env.bar_size), cost_model=costs,
        order_notional=env.order_notional, trading_start=job.trading_start,
        live_rules=PaperAutomationRules(max_gross_allocation=env.max_gross_allocation))
    backtester = Backtester(storage, config)
    if env.authored_digest is None:
        result = backtester.run_from_module(
            env.strategy_file, env.class_name, list(env.conids),
            universe_accessor=accessor, params=dict(job.params))
    else:
        result = run_authored_backtest(backtester, env, dict(job.params), accessor)
    _require_every_conid_loaded(env, job, result.loaded_conids)
```

The rest of `run_window_job` (from `curve = result.equity_curve`) is unchanged.

- [ ] **Step 5: Run the tests and the existing job tests**

Run: `.venv/bin/python -m pytest tests/research/test_authored_backtest.py tests/research/test_evaluation_jobs.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add trader/research/authored_backtest.py trader/research/evaluation_jobs.py tests/research/test_authored_backtest.py
git commit -m "feat: backtest authored strategies through the research sandbox one session at a time

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: The authored cohort spec, family, socket and run environment

**Files:**
- Modify: `trader/research/evaluation_spec.py` (`EvaluationSpec`, `build_evaluation_spec`, new `_authored_strategy`), `trader/research/cohort.py` (`build_cohort_spec`), `trader/research/evaluation.py` (`EvaluationPaths`, `_family`, `run_environment`, `evaluate`; new `AUTHORED_COMMIT`, `_commit_of`, `_source_digest`), `trader/research/cohort_evaluation.py` (commit call)
- Create: `tests/research/authored_fixtures.py`
- Test: `tests/research/test_authored_spec.py`

**Interfaces:**
- Consumes: Plan 3 `authored_strategy_path`, `split_evaluation_key`; Plan 1 `is_authored_key`, `parse_authored_key`, `authored_key`; Plan 2 `RESEARCH_SOCKET`.
- Produces:
  - `EvaluationSpec.authored_digest: Optional[str] = None`; `build_evaluation_spec(raw, *, universe_accessor, costs_config, repo_root, authored_digest=None)`.
  - `EvaluationPaths.sandbox_socket: str = RESEARCH_SOCKET` (ruling 13).
  - `trader.research.evaluation.AUTHORED_COMMIT = "authored"`, `_commit_of(spec, repo_root) -> str`, `_source_digest(spec) -> str`; `run_environment` fills `authored_digest` and `sandbox_socket`.
  - `tests.research.authored_fixtures`: `NOW`, `RESEARCH_DAY`, `POINT`, `CATALOGUE_KEY`, `install_fake_sandbox(monkeypatch) -> FakeSandbox`, `time_of_day_key(sandbox, variant="") -> str`, `request_body(key, cohort=(POINT,))`, `research_world(tmp_path, tmp_duckdb_path) -> SimpleNamespace` (`build(key, cohort)`, `build_body(body)`, `paths`, `db`, `registry`, `root`).

- [ ] **Step 1: Write the shared fixtures**

```python
# tests/research/authored_fixtures.py
"""Tests only: authored strategies on the synthetic research market of evaluation_fixtures."""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

from tests.authored.fake_sandbox import FakeSandbox, caller_source
from tests.research.evaluation_fixtures import (CONIDS, build_spec_file, write_costs_config, write_trend_bars,
                                               write_universe)
from trader.authored.keys import authored_key
from trader.automation.backtest_judge_config import BacktestJudgeConfig
from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator
from trader.data.universe import UniverseAccessor
from trader.research.cohort import build_cohort_spec
from trader.research.evaluation import EvaluationPaths
from trader.research.evaluation_request import EvaluationRequestBody
from trader.research.experiment_registry import ExperimentRegistry
from trader.research.schema import apply_research_migrations
from trader.research.service_config import ResearchServiceConfig
from trader.simulation.execution_costs import load_execution_costs_config

NOW = dt.datetime(2024, 3, 29, 14, tzinfo=dt.timezone.utc)          # 10:00 New York, 2024-03-29
RESEARCH_DAY = "2024-03-29"
POINT = {"ENTRY_MINUTE": 600, "EXIT_MINUTE": 660}
CATALOGUE_KEY = "strategies/time_of_day.py:TimeOfDay"
CONFIG = ResearchServiceConfig(period_sessions=38, folds=2, embargo_sessions=1, holdout_sessions=5)


def install_fake_sandbox(monkeypatch) -> FakeSandbox:
    fake = FakeSandbox()
    monkeypatch.setattr("trader.research.authored_backtest.connect_sandbox", lambda socket_path: fake)
    return fake


def time_of_day_key(sandbox: FakeSandbox, variant: str = "") -> str:
    """``variant`` (a comment) makes another file with the same behaviour and another digest."""
    return authored_key(sandbox.add(caller_source("time_of_day_port") + variant), "TimeOfDayPort")


def request_body(key: str, cohort=(POINT,)) -> EvaluationRequestBody:
    return EvaluationRequestBody.model_validate(
        {"strategy_key": key, "cohort": [dict(point) for point in cohort], "conids": CONIDS,
         "bar_size": "15 mins", "research_day": RESEARCH_DAY})


def research_world(tmp_path, tmp_duckdb_path, *, drift: float = 0.0006) -> SimpleNamespace:
    write_universe(tmp_duckdb_path)
    write_trend_bars(tmp_duckdb_path, drift=drift)
    build_spec_file(tmp_path)                                        # the catalogue TimeOfDay, for comparisons
    costs_path = write_costs_config(tmp_path / "execution_costs.yaml")
    db = DuckDBConnection.get_instance(str(tmp_path / "research.duckdb"))
    apply_research_migrations(SchemaMigrator(db))
    registry = ExperimentRegistry(db)
    paths = EvaluationPaths(history_db=tmp_duckdb_path, universe_db=tmp_duckdb_path, universe_library="Universes",
                            execution_costs=str(costs_path), repo_root=tmp_path, reports_dir=tmp_path / "reports",
                            summaries_dir=tmp_path / "summaries")
    judge = BacktestJudgeConfig(strategy_allowlist=(CATALOGUE_KEY,), max_cohort_points=3)

    def build_body(body: EvaluationRequestBody):
        return build_cohort_spec(body, config=CONFIG, judge=judge,
                                 universe_accessor=UniverseAccessor(tmp_duckdb_path, "Universes"),
                                 costs_config=load_execution_costs_config(str(costs_path)), repo_root=tmp_path,
                                 registry=registry, history_db=tmp_duckdb_path)

    def build(key: str, cohort=(POINT,)):
        return build_body(request_body(key, cohort))

    return SimpleNamespace(build=build, build_body=build_body, paths=paths, db=db, registry=registry, root=tmp_path)
```

- [ ] **Step 2: Write the failing tests**

```python
# tests/research/test_authored_spec.py
from dataclasses import replace

import pytest

from tests.authored.fake_sandbox import FakeSandbox
from tests.research.authored_fixtures import CATALOGUE_KEY, research_world, time_of_day_key
from trader.authored.keys import parse_authored_key
from trader.research.cohort import RequestRefused
from trader.research.evaluation import AUTHORED_COMMIT, _commit_of, _family, run_environment
from trader.research.evaluation_spec import EvaluationSpecError, build_evaluation_spec
from trader.research.strategy_key import authored_strategy_path
from trader.sandbox.protocol import RESEARCH_SOCKET

KEY = time_of_day_key(FakeSandbox())
DIGEST = parse_authored_key(KEY)[0]


@pytest.fixture
def world(tmp_path, tmp_duckdb_path):
    return research_world(tmp_path, tmp_duckdb_path)


def test_an_authored_key_needs_no_allowlist_no_file_and_no_import(world):
    spec = world.build(KEY)
    assert spec.base.authored_digest == DIGEST
    assert spec.base.strategy_path == authored_strategy_path(DIGEST)
    assert spec.file_hash == DIGEST


def test_a_point_naming_another_tunable_is_refused(world):
    with pytest.raises(RequestRefused) as refused:
        world.build(KEY, cohort=({"ENTRY_MINUTE": 600}, {"ENTRY_MINUTE": 615, "OTHER": 1}))
    assert refused.value.code == "COHORT_POINT_INVALID"


def test_the_family_names_the_digest_and_no_commit(world):
    base = world.build(KEY).base
    assert _commit_of(base, world.root) == AUTHORED_COMMIT
    assert _family(base, "m", AUTHORED_COMMIT, world.paths).source_tree_digest == DIGEST.removeprefix("sha256:")


def test_the_run_environment_names_the_research_socket(world):
    base = world.build(KEY).base
    env = run_environment(base, world.paths)
    assert (env.authored_digest, env.sandbox_socket) == (DIGEST, RESEARCH_SOCKET)
    assert env.strategy_file == authored_strategy_path(DIGEST)
    moved = run_environment(base, replace(world.paths, sandbox_socket="/tmp/s/research.sock"))
    assert moved.sandbox_socket == "/tmp/s/research.sock"


def test_a_catalogue_environment_names_no_sandbox(world):
    env = run_environment(world.build(CATALOGUE_KEY).base, world.paths)
    assert (env.authored_digest, env.sandbox_socket) == (None, None)


def test_a_spec_whose_path_is_not_the_digest_is_refused(world):
    raw = {"name": "x", "strategy": "authored:sha256:" + "0" * 64, "class": "TimeOfDayPort",
           "params": {"ENTRY_MINUTE": 600}}
    with pytest.raises(EvaluationSpecError, match="not the authored file"):
        build_evaluation_spec(raw, universe_accessor=None, costs_config=None, repo_root=world.root,
                              authored_digest=DIGEST)
```

- [ ] **Step 3: Run them to verify they fail**

Run: `.venv/bin/python -m pytest tests/research/test_authored_spec.py -q`
Expected: FAIL (`ImportError: cannot import name 'AUTHORED_COMMIT'`).

- [ ] **Step 4: Implement the spec change**

In `trader/research/evaluation_spec.py` add `Optional` to the `typing` import and `from trader.research.strategy_key import authored_strategy_path`; the field goes last in `EvaluationSpec`:

```python
    calendar: str
    # An authored file (SP3): named by its digest, read by the sandbox only.
    authored_digest: Optional[str] = None
```

Add next to `_strategy_file`:

```python
def _authored_strategy(raw: Mapping[str, Any], authored_digest: str) -> tuple[str, Path]:
    """An authored file is named by its digest and never read here (Plan 4 rulings 3 and 5)."""
    path = authored_strategy_path(authored_digest)
    if str(_require(raw, 'strategy')) != path:
        raise EvaluationSpecError(f"strategy: {raw['strategy']} is not the authored file {path}")
    return path, Path(path)
```

Replace the head of `build_evaluation_spec` through the `unknown` check:

```python
def build_evaluation_spec(raw: Mapping[str, Any], *, universe_accessor: Any,
                          costs_config: ExecutionCostsConfig, repo_root: Path,
                          authored_digest: Optional[str] = None) -> EvaluationSpec:
    name = str(_require(raw, 'name'))
    if authored_digest is None:
        strategy_path, strategy_file = _strategy_file(str(_require(raw, 'strategy')), repo_root)
    else:
        strategy_path, strategy_file = _authored_strategy(raw, authored_digest)
    class_name = str(_require(raw, 'class'))
    params = dict(_mapping(_require(raw, 'params'), 'params'))
    # Never import an authored file: the trader's claim matched its names to the record (Plan 4 ruling 5).
    tunables = set(params) if authored_digest else declared_tunables(strategy_file, class_name)
    unknown = sorted((k for k in params if k not in tunables), key=str)
    if unknown:
        raise EvaluationSpecError(f'params: not upper-case tunables of {class_name}: {unknown}')
```

The rest of the body is unchanged; the final `return EvaluationSpec(...)` gains `authored_digest=authored_digest,` after `calendar=calendar,`.

- [ ] **Step 5: Implement the cohort branch**

In `trader/research/cohort.py` replace `from trader.research.strategy_key import split_strategy_key` with `from trader.research.strategy_key import split_evaluation_key` and add `from trader.authored.keys import is_authored_key, parse_authored_key`. In `build_cohort_spec`:

```python
def build_cohort_spec(body: EvaluationRequestBody, *, config: Any, judge: Any, universe_accessor: Any,
                      costs_config: Any, repo_root: Path, registry: Any, history_db: str) -> CohortSpec:
    authored_digest = parse_authored_key(body.strategy_key)[0] if is_authored_key(body.strategy_key) else None
    # An authored key is claimed under the owner switch, not the allowlist (Plan 4 ruling 5).
    if authored_digest is None and not judge.allows(body.strategy_key):
        raise RequestRefused("STRATEGY_NOT_ALLOWED", f"{body.strategy_key} is not on the allowlist")
```

`path, class_name = split_strategy_key(body.strategy_key)` becomes `path, class_name = split_evaluation_key(body.strategy_key)`. Then:

```python
    try:
        base = build_evaluation_spec(raw, universe_accessor=universe_accessor, costs_config=costs_config,
                                     repo_root=repo_root, authored_digest=authored_digest)
    except EvaluationSpecError as exc:
        raise RequestRefused("SPEC_INVALID", str(exc)) from None
    tunables = set(first) if authored_digest else declared_tunables(base.strategy_file, base.class_name)
```

and the return:

```python
    return CohortSpec(request_id=evaluation_request_id(body), body=body, strategy_key=body.strategy_key,
                      base=base, cohort=tuple(cohort), neighbours=neighbours,
                      file_hash=authored_digest or _file_hash(base.strategy_file))
```

- [ ] **Step 6: Implement the paths, family, commit and environment**

In `trader/research/evaluation.py` add `from trader.sandbox.protocol import RESEARCH_SOCKET`; `EvaluationPaths` gains a last field:

```python
    summaries_dir: Path
    # The research sandbox socket an authored strategy runs behind (Plan 4 ruling 13).
    sandbox_socket: str = RESEARCH_SOCKET
```

Add:

```python
# An authored file is not in git; its digest pins it (the family's source digest).
AUTHORED_COMMIT = 'authored'


def _commit_of(spec: EvaluationSpec, repo_root: Path) -> str:
    if spec.authored_digest is not None:
        return AUTHORED_COMMIT
    return _repository_commit(repo_root, spec.strategy_file)


def _source_digest(spec: EvaluationSpec) -> str:
    if spec.authored_digest is not None:
        return spec.authored_digest.removeprefix('sha256:')
    return compute_strategy_hash(str(spec.strategy_file))
```

In `evaluate()` replace `repository_commit = _repository_commit(paths.repo_root, spec.strategy_file)` with `repository_commit = _commit_of(spec, paths.repo_root)`. In `trader/research/cohort_evaluation.py` replace `repository_commit = _repository_commit(paths.repo_root, base.strategy_file)` with `repository_commit = _commit_of(base, paths.repo_root)` and swap `_repository_commit` for `_commit_of` in its import from `trader.research.evaluation`. In `_family`, `source_tree_digest=compute_strategy_hash(str(spec.strategy_file)),` becomes `source_tree_digest=_source_digest(spec),`. `run_environment`:

```python
def run_environment(spec: EvaluationSpec, paths: EvaluationPaths) -> RunEnvironment:
    return RunEnvironment(
        history_db=paths.history_db, universe_db=paths.universe_db,
        universe_library=paths.universe_library, execution_costs_path=paths.execution_costs,
        strategy_file=str(spec.strategy_file), class_name=spec.class_name,
        conids=tuple(spec.conids), bar_size=spec.bar_size, order_notional=spec.order_notional,
        account_equity=spec.account_equity, max_gross_allocation=spec.max_gross_allocation,
        regular_session_calendar=spec.calendar, authored_digest=spec.authored_digest,
        sandbox_socket=None if spec.authored_digest is None else paths.sandbox_socket)
```

- [ ] **Step 7: Run the tests and the SP2c spec, cohort and commit tests**

Run: `.venv/bin/python -m pytest tests/research/test_authored_spec.py tests/research/test_evaluation_spec.py tests/research/test_cohort_request.py tests/research/test_evaluation_commit.py tests/research/test_evaluation_jobs.py -q`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add trader/research/evaluation_spec.py trader/research/cohort.py trader/research/evaluation.py \
        trader/research/cohort_evaluation.py tests/research/authored_fixtures.py tests/research/test_authored_spec.py
git commit -m "feat: build an evaluation spec for an authored key without reading or importing the file

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: The trial count on an authored case

**Files:**
- Modify: `trader/research/evaluation.py` (new `TrialDenominator`, `trial_denominator`; `_walk_forward_evidence`, `evaluate`), `trader/research/cohort_evaluation.py` (`_verdict`, `evaluate_cohort`), `trader/research/case_builder.py` (`evaluation_summary`)
- Test: `tests/research/test_authored_trials.py`

**Interfaces:**
- Consumes: Task 4 `EvaluationSpec.authored_digest`, `research_world`, `install_fake_sandbox`, `time_of_day_key`; Plan 3 `split_evaluation_key`.
- Produces: `trader.research.evaluation.TrialDenominator(count: int, sharpes: tuple[float, ...])`, `trial_denominator(registry, spec, authored_judged_count: int = 0) -> TrialDenominator`; `_walk_forward_evidence(spec, main, neighbours, trials: TrialDenominator, regimes, liquidity)`; `evaluate_cohort(..., authored_judged_count: int = 0)`. `CohortResult.strategy_trials` is `trials.count`, so the case evidence and `evaluation_summary(...)["strategy_trials"]` carry it to Jev (spec 7.7).

- [ ] **Step 1: Write the failing tests**

```python
# tests/research/test_authored_trials.py
"""SP3 spec 7.7 and 10 (trial count on the case), Plan 4 ruling 12."""
from types import SimpleNamespace

import pytest

from tests.research.authored_fixtures import NOW, RESEARCH_DAY, install_fake_sandbox, research_world, time_of_day_key
from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator
from trader.research.artifact import ExperimentFamily
from trader.research.case_builder import build_initial_case, evaluation_summary
from trader.research.cohort_evaluation import evaluate_cohort
from trader.research.evaluation import TrialDenominator, trial_denominator
from trader.research.experiment_registry import ExperimentRegistry
from trader.research.schema import apply_research_migrations

PATH = "authored:sha256:" + "a" * 64


@pytest.fixture
def registry(tmp_path):
    db = DuckDBConnection.get_instance(str(tmp_path / "research.duckdb"))
    apply_research_migrations(SchemaMigrator(db))
    return ExperimentRegistry(db)


def trials_of(registry, path, class_name, sharpes):
    family_id = registry.create_family(ExperimentFamily(
        strategy_path=path, class_name=class_name, repository_commit="authored", source_tree_digest="s",
        dependency_lock_digest="d", container_digest="unpinned", dataset_manifest_digest="m",
        search_space={"path": path}, cost_model={}, validation_protocol={}), created_at=NOW)
    for index, sharpe in enumerate(sharpes):
        trial_id = registry.start_trial(family_id, trial_key=f"t{index}", parameters={}, started_at=NOW)
        registry.finish_trial(trial_id, status="SUCCEEDED", finished_at=NOW, metrics={"daily_sharpe": sharpe})


def test_an_authored_key_adds_the_judged_files_to_its_own_trials(registry):
    trials_of(registry, PATH, "Port", [0.2, -0.1])
    spec = SimpleNamespace(strategy_path=PATH, class_name="Port", authored_digest="sha256:" + "a" * 64)
    assert trial_denominator(registry, spec, authored_judged_count=3) == TrialDenominator(5, (0.2, -0.1))


def test_a_catalogue_key_counts_only_its_own_trials(registry):
    trials_of(registry, "strategies/x.py", "X", [0.4])
    spec = SimpleNamespace(strategy_path="strategies/x.py", class_name="X", authored_digest=None)
    assert trial_denominator(registry, spec) == TrialDenominator(1, (0.4,))
    with pytest.raises(ValueError):
        trial_denominator(registry, spec, authored_judged_count=1)


@pytest.mark.timeout(300)
def test_the_next_files_case_counts_the_judged_file(tmp_path, tmp_duckdb_path, monkeypatch):
    world = research_world(tmp_path, tmp_duckdb_path)
    sandbox = install_fake_sandbox(monkeypatch)
    spec = world.build(time_of_day_key(sandbox))
    result = evaluate_cohort(spec, research_db=world.db, paths=world.paths, now=lambda: NOW,
                             authored_judged_count=1)
    own = world.registry.strategy_trials(spec.base.strategy_path, "TimeOfDayPort")
    assert own and result.strategy_trials == 1 + len(own)
    case = build_initial_case(spec, RESEARCH_DAY, result, created_at=NOW, warmup_sessions=5)
    summary = evaluation_summary(case, order_notional=1900.0)
    assert summary["strategy_trials"] >= 1 + len(own)
    assert (summary["strategy_path"], summary["class_name"]) == (spec.base.strategy_path, "TimeOfDayPort")
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -m pytest tests/research/test_authored_trials.py -q`
Expected: FAIL (`ImportError: cannot import name 'TrialDenominator'`).

- [ ] **Step 3: Implement the denominator**

In `trader/research/evaluation.py`, next to `PointResult`:

```python
@dataclass(frozen=True)
class TrialDenominator:
    """The multiple-testing denominator of one strategy key: the trials it counts and their Sharpes."""
    count: int
    sharpes: tuple[float, ...]


def trial_denominator(registry: ExperimentRegistry, spec: EvaluationSpec,
                      authored_judged_count: int = 0) -> TrialDenominator:
    """A catalogue key counts its own trials. An authored key also counts every authored file the trader has
    judged (SP3 spec 7.7, Plan 4 ruling 12)."""
    if spec.authored_digest is None and authored_judged_count:
        raise ValueError("a judged authored count belongs to an authored key only")
    own = registry.strategy_trials(spec.strategy_path, spec.class_name)
    sharpes = tuple(t.metrics['daily_sharpe'] for t in own
                    if isinstance(t.metrics.get('daily_sharpe'), (int, float)))
    return TrialDenominator(authored_judged_count + len(own), sharpes)
```

`_walk_forward_evidence` takes the denominator:

```python
def _walk_forward_evidence(spec: EvaluationSpec, main: PointResult,
                           neighbours: Sequence[PointResult], trials: TrialDenominator,
                           regimes: mcx.RegimeEvidence,
                           liquidity: mcx.LiquidityEnvelope) -> EligibilityEvidence:
    trips = {m: _round_trips(main.outcomes[m]) for m in COST_MULTIPLIERS}
    baseline = trips[1.0]
    returns = ev.session_returns([o.equity_series() for o in main.outcomes[1.0]],
                                 starting_equity=spec.account_equity)
    sharpe_ci = annualized_sharpe_ci(returns, periods_per_year=ev.TRADING_DAYS_PER_YEAR, seed=0)
    return EligibilityEvidence(
        n_round_trips=len(baseline),
        n_instruments=len(set(spec.conids)),
        expectancy_bps_baseline=ev.dollar_weighted_expectancy_bps(baseline),
        expectancy_bps_1_5x=ev.dollar_weighted_expectancy_bps(trips[1.5]),
        expectancy_bps_2x=ev.dollar_weighted_expectancy_bps(trips[2.0]),
        selection_adjusted_confidence=ev.selection_confidence(
            returns, n_trials=trials.count, trial_sharpes=list(trials.sharpes)),
```

(the remaining `EligibilityEvidence` fields are unchanged; the old local `trial_sharpes` goes away). In `evaluate()`:

```python
    evidence = _walk_forward_evidence(
        spec, main, neighbours, trial_denominator(registry, spec), context.regimes, context.liquidity)
```

In `trader/research/cohort_evaluation.py` import `TrialDenominator, trial_denominator` from `trader.research.evaluation`; `_verdict` names the type of its `trials` argument (body unchanged):

```python
def _verdict(index, base, plan, point, neighbours, trials: TrialDenominator, benchmark_closes, bars,
             ruleset) -> PointVerdict:
```

`evaluate_cohort` gains a keyword and uses it:

```python
def evaluate_cohort(spec: CohortSpec, *, research_db: Any, paths: EvaluationPaths,
                    now: Callable[[], dt.datetime], ruleset=PAPER_V1, max_workers: int = 1,
                    authored_judged_count: int = 0) -> CohortResult:
```

```python
    trials = trial_denominator(registry, base, authored_judged_count)   # one denominator for all points
    verdicts = tuple(_verdict(i, base, plan, p, ns, trials, benchmark_closes, bars, ruleset)
                     for i, (p, ns) in enumerate(zip(points, neighbours, strict=True)))
    selected = select_point(verdicts)
    common = dict(family_id=family_id, verdicts=verdicts, strategy_trials=trials.count,
```

In `trader/research/case_builder.py` replace the import of `split_strategy_key` with `split_evaluation_key` and, in `evaluation_summary`, `path, class_name = split_strategy_key(case.strategy_key)` with `path, class_name = split_evaluation_key(case.strategy_key)` (ruling 3).

- [ ] **Step 4: Run the tests and the SP2c evaluation tests**

Run: `.venv/bin/python -m pytest tests/research/test_authored_trials.py tests/research/test_strategy_trials.py tests/research/test_selection_adjustment.py tests/research/test_cohort_evaluation.py tests/research/test_evaluation.py tests/research/test_case_builder.py -q`
Expected: PASS (catalogue keys keep today's count).

- [ ] **Step 5: Commit**

```bash
git add trader/research/evaluation.py trader/research/cohort_evaluation.py trader/research/case_builder.py \
        tests/research/test_authored_trials.py
git commit -m "feat: count the judged authored files in an authored key's trial denominator

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: STARTED, the smoke stage and the authored evaluator

**Files:**
- Create: `trader/research/authored_outcome.py`, `trader/research/authored_evaluation.py`
- Modify: `trader/research/trader_port.py` (new `report_authored_evaluation`), `tests/research/service_fakes.py` (`FakeTrader`)
- Test: `tests/research/test_authored_evaluation.py`

**Interfaces:**
- Consumes: Task 3 `connect_sandbox`, window job; Task 4 spec and `EvaluationPaths.sandbox_socket`; Task 5 `evaluate_cohort(authored_judged_count=)`; `ExperimentRegistry.opened_holdout_windows`; `ServiceStateError` (`trader/research/evaluation_service.py`); Plan 3 `report_authored_evaluation`.
- Produces:
  - `trader.research.authored_outcome`: `AUTHORED_END_CODE = "AUTHORED_ENDED"`, `STARTED`, `SANDBOX_ERROR_EVENT`, `SANDBOX_UNAVAILABLE_EVENT`; `started_body(request_id, digest, image_digest) -> dict`; `AuthoredEnd(event, detail)` with `marker()`, `from_row(row)`, `report_body(request_id, digest)`; `AuthoredEvaluationEnded(Exception)` (attribute `end`); `sandbox_cause(exc)`; `end_for(cause) -> AuthoredEnd`.
  - `trader.research.authored_evaluation`: `SMOKE_SESSIONS = 20`, `smoke_window(spec, paths)`, `AuthoredEvaluator(*, research_db, paths, now, trader, ruleset=PAPER_V1, run_cohort=evaluate_cohort, run_window=run_window_job)` with `evaluate(spec) -> CohortResult`.
  - `TraderPort.report_authored_evaluation(body) -> dict`; `FakeTrader.report_authored_evaluation`, `.authored_reports`, `.judged_count`, knobs `lose_authored_report`, `refuse_authored_report`, flag `reported_after_close`.

- [ ] **Step 1: Extend the fake trader and the port**

In `tests/research/service_fakes.py`, `FakeTrader.__init__` gains:

```python
        self.authored_reports, self.judged_count = [], 0
        self.lose_authored_report, self.refuse_authored_report = False, None
        self.reported_after_close = False      # Plan 4 ruling 10: an end must reach the trader before FAILED
```

and a method:

```python
    def report_authored_evaluation(self, body):
        """Plan 3's report_authored_evaluation: UPDATED once per body, then UNCHANGED; judged_count rides along."""
        claim = self.claims.get(body["request_id"])
        if body["event"] != "STARTED" and claim is not None and claim["state"] == "FAILED":
            self.reported_after_close = True
        if self.lose_authored_report:
            self.lose_authored_report = False
            raise TraderUnavailable("report_authored_evaluation: request lost")
        if self.refuse_authored_report is not None:
            code, retryable = self.refuse_authored_report
            return {"status": "REFUSED", "code": code, "detail": "refused by the fake", "retryable": retryable,
                    "record": None, "judged_count": None}
        status = "UNCHANGED" if body in self.authored_reports else "UPDATED"
        if status == "UPDATED":
            self.authored_reports.append(dict(body))
        return {"status": status, "code": None, "detail": None, "retryable": False, "record": None,
                "judged_count": self.judged_count}
```

In `trader/research/trader_port.py`:

```python
    def report_authored_evaluation(self, body: dict) -> dict:
        """Plan 3's facts about one authored evaluation (STARTED or a sandbox end)."""
        return self._call(self._command, "report_authored_evaluation", body)
```

- [ ] **Step 2: Write the failing tests**

```python
# tests/research/test_authored_evaluation.py
"""SP3 spec 7.6 and 8: STARTED, the smoke stage and the end of a sandbox error in an authored evaluation."""
import pandas as pd
import pytest

from tests.authored.fake_sandbox import IMAGE_DIGEST, caller_source, fault
from tests.research.authored_fixtures import (CATALOGUE_KEY, NOW, install_fake_sandbox, research_world,
                                             time_of_day_key)
from tests.research.service_fakes import FakeTrader
from trader.authored.keys import authored_key
from trader.authored.sandbox_client import SandboxUnavailable
from trader.messaging.typed_rpc import TypedRpcRemoteError
from trader.research.artifact import ExperimentFamily
from trader.research.authored_evaluation import SMOKE_SESSIONS, AuthoredEvaluator
from trader.research.authored_outcome import AuthoredEvaluationEnded, started_body
from trader.research.evaluation import EvaluationError
from trader.research.evaluation_service import ServiceStateError
from trader.research.experiment_registry import ExperimentRegistry
from trader.research.trader_port import TraderUnavailable
from trader.sandbox.protocol import SANDBOX_ERROR

BARS_PER_SESSION = 26                                   # 15-minute bars, 09:30-16:00 New York


@pytest.fixture
def world(tmp_path, tmp_duckdb_path):
    return research_world(tmp_path, tmp_duckdb_path)


@pytest.fixture
def sandbox(monkeypatch):
    return install_fake_sandbox(monkeypatch)


def evaluator(world, trader=None, **overrides):
    return AuthoredEvaluator(research_db=world.db, paths=world.paths, now=lambda: NOW,
                             trader=trader or FakeTrader(), **overrides)


def ended(world, spec, **overrides):
    with pytest.raises(AuthoredEvaluationEnded) as raised:
        evaluator(world, **overrides).evaluate(spec)
    return raised.value.end


def nothing_spent(world, spec):
    base = spec.base
    return (world.registry.strategy_trials(base.strategy_path, base.class_name) == []
            and world.registry.opened_holdout_windows(base.strategy_path, base.class_name) == [])


def recording(calls):
    def run_cohort(spec, **kwargs):
        calls.append(kwargs)
    return run_cohort


def opens_a_holdout_then(error):
    def run_cohort(spec, *, research_db, **_):
        registry = ExperimentRegistry(research_db)
        family_id = registry.create_family(ExperimentFamily(
            strategy_path=spec.base.strategy_path, class_name=spec.base.class_name, repository_commit="authored",
            source_tree_digest="s", dependency_lock_digest="d", container_digest="unpinned",
            dataset_manifest_digest="m", search_space={}, cost_model={}, validation_protocol={}),
            created_at=NOW, validation_folds=[{"kind": "holdout", "start": "2024-03-21", "end": "2024-03-28"}])
        trial_id = registry.start_trial(family_id, trial_key="t", parameters={}, started_at=NOW)
        registry.finish_trial(trial_id, status="SUCCEEDED", finished_at=NOW, metrics={"daily_sharpe": 0.1})
        artifact = registry.seal_artifact(family_id, selected_trial_id=trial_id, selected_parameters={},
                                          sealed_at=NOW)
        registry.begin_holdout(artifact, opened_at=NOW)
        raise EvaluationError("backtest failed") from error
    return run_cohort


@pytest.mark.timeout(300)
def test_an_authored_cohort_scores_like_the_catalogue_strategy(world, sandbox):
    catalogue = evaluator(world).evaluate(world.build(CATALOGUE_KEY))
    authored = evaluator(world).evaluate(world.build(time_of_day_key(sandbox)))
    assert authored.stage == catalogue.stage
    assert ([v.point.metrics["n_round_trips"] for v in authored.verdicts]
            == [v.point.metrics["n_round_trips"] for v in catalogue.verdicts])
    assert ([v.point.metrics["oos_net_pnl"] for v in authored.verdicts]
            == pytest.approx([v.point.metrics["oos_net_pnl"] for v in catalogue.verdicts]))
    assert sandbox.most_open == 1                        # Review Focus 3


def test_a_catalogue_key_reports_nothing_and_runs_no_smoke_stage(world, sandbox):
    calls, trader = [], FakeTrader()
    evaluator(world, trader, run_cohort=recording(calls)).evaluate(world.build(CATALOGUE_KEY))
    assert sandbox.sessions == [] and trader.authored_reports == [] and len(calls) == 1


def test_started_names_the_research_image_and_carries_the_judged_count(world, sandbox):
    calls, trader = [], FakeTrader()
    trader.judged_count = 2
    spec = world.build(time_of_day_key(sandbox))
    evaluator(world, trader, run_cohort=recording(calls)).evaluate(spec)
    assert trader.authored_reports == [started_body(spec.request_id, spec.base.authored_digest, IMAGE_DIGEST)]
    assert (calls[0]["authored_judged_count"], calls[0]["max_workers"]) == (2, 1)


def test_smoke_streams_the_first_sessions_of_the_smallest_conid(world, sandbox):
    evaluator(world, run_cohort=recording([])).evaluate(world.build(time_of_day_key(sandbox)))
    smoke = sandbox.sessions[0]
    assert len(sandbox.sessions) == 1
    assert len(smoke.stamps) == SMOKE_SESSIONS * BARS_PER_SESSION
    assert smoke.stamps[0] == pd.Timestamp("2024-02-05 14:30", tz="UTC")    # the first of the 38-session period
    assert smoke.first_close < 110.0                     # conid 1001 trades near 100, the next conid near 110


def test_an_unreachable_sandbox_at_start_returns_the_file_to_stored_and_starts_nothing(world, sandbox):
    sandbox.unavailable = True
    trader = FakeTrader()
    spec = world.build(time_of_day_key(sandbox))
    end = ended(world, spec, trader=trader)
    assert end.event == "SANDBOX_UNAVAILABLE"
    assert trader.authored_reports == [] and nothing_spent(world, spec)


def test_a_refused_start_runs_nothing(world, sandbox):
    trader = FakeTrader()
    trader.refuse_authored_report = ("AUTHORED_STATE_CONFLICT", False)
    with pytest.raises(ServiceStateError):
        evaluator(world, trader).evaluate(world.build(time_of_day_key(sandbox)))
    assert sandbox.sessions == []


def test_a_wire_error_at_start_parks_instead_of_signing_a_case(world, sandbox):
    trader = FakeTrader()

    def wire_error(body):
        raise TypedRpcRemoteError("VALIDATION_ERROR", "invalid request body")
    trader.report_authored_evaluation = wire_error
    with pytest.raises(ServiceStateError):
        evaluator(world, trader).evaluate(world.build(time_of_day_key(sandbox)))


def test_an_unreachable_trader_at_start_ends_the_worker(world, sandbox):
    trader = FakeTrader()
    trader.lose_authored_report = True
    with pytest.raises(TraderUnavailable):
        evaluator(world, trader).evaluate(world.build(time_of_day_key(sandbox)))
    assert sandbox.sessions == []


def test_a_fault_in_the_smoke_stage_rejects_without_a_trial_or_a_holdout(world, sandbox):
    spec = world.build(authored_key(sandbox.add(caller_source("faults_above_limit")), "FaultsAboveLimit"),
                       cohort=({"LIMIT": 1.0},))
    end = ended(world, spec)
    assert (end.event, end.detail) == ("SANDBOX_ERROR", SANDBOX_ERROR)
    assert nothing_spent(world, spec) and len(sandbox.sessions) == 1


def test_an_unreachable_sandbox_at_the_smoke_stage_returns_the_file_to_stored(world, sandbox):
    sandbox.refuse_opens_after = 0                       # PING answers, no session opens
    spec = world.build(time_of_day_key(sandbox))
    assert ended(world, spec).event == "SANDBOX_UNAVAILABLE"
    assert nothing_spent(world, spec)


@pytest.mark.timeout(240)
def test_an_unreachable_sandbox_before_the_holdout_returns_the_file_to_stored(world, sandbox):
    sandbox.refuse_opens_after = 1                       # the smoke session opens, no fold session does
    spec = world.build(time_of_day_key(sandbox))
    assert ended(world, spec).event == "SANDBOX_UNAVAILABLE"
    assert world.registry.opened_holdout_windows(spec.base.strategy_path, spec.base.class_name) == []


def test_an_unreachable_sandbox_after_the_holdout_opened_is_the_sp2c_failure(world, sandbox):
    with pytest.raises(EvaluationError):
        evaluator(world, run_cohort=opens_a_holdout_then(SandboxUnavailable("gone"))).evaluate(
            world.build(time_of_day_key(sandbox)))


def test_a_fault_after_the_holdout_opened_still_rejects(world, sandbox):
    end = ended(world, world.build(time_of_day_key(sandbox)), run_cohort=opens_a_holdout_then(fault(SANDBOX_ERROR)))
    assert end.event == "SANDBOX_ERROR"


def test_an_error_outside_the_sandbox_passes_through(world, sandbox):
    def broken(spec, **_):
        raise EvaluationError("market context unavailable")
    with pytest.raises(EvaluationError):
        evaluator(world, run_cohort=broken).evaluate(world.build(time_of_day_key(sandbox)))
```

- [ ] **Step 3: Run them to verify they fail**

Run: `.venv/bin/python -m pytest tests/research/test_authored_evaluation.py -q`
Expected: FAIL (`ModuleNotFoundError: No module named 'trader.research.authored_evaluation'`).

- [ ] **Step 4: Implement `trader/research/authored_outcome.py`**

```python
"""What research tells the trader about an authored evaluation (SP3 spec 7.6, 8; Plan 4 rulings 6-11).

The end of a run stopped by the sandbox is kept in ``research_requests.code`` / ``detail`` before anything is
sent, so a restart resends the report and never evaluates the file again.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional

from trader.authored.sandbox_client import SandboxFault, SandboxUnavailable

AUTHORED_END_CODE = "AUTHORED_ENDED"
STARTED = "STARTED"
SANDBOX_ERROR_EVENT = "SANDBOX_ERROR"
SANDBOX_UNAVAILABLE_EVENT = "SANDBOX_UNAVAILABLE"
DETAIL_MAX = 120


def started_body(request_id: str, digest: str, image_digest: str) -> dict:
    """Plan 3's report_authored_evaluation STARTED body; its shape wins."""
    return {"digest": digest, "request_id": request_id, "event": STARTED, "sandbox_image_digest": image_digest,
            "detail": ""}


@dataclass(frozen=True)
class AuthoredEnd:
    event: str          # SANDBOX_ERROR (REJECTED) or SANDBOX_UNAVAILABLE (back to STORED)
    detail: str         # a code or an exception type, never free text

    def marker(self) -> str:
        return f"{self.event}|{self.detail}"

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> Optional["AuthoredEnd"]:
        if row.get("code") != AUTHORED_END_CODE:
            return None
        event, detail = str(row["detail"]).split("|", 1)
        return cls(event, detail)

    def report_body(self, request_id: str, digest: str) -> dict:
        """Plan 3's report_authored_evaluation body for an end; its shape wins."""
        return {"digest": digest, "request_id": request_id, "event": self.event, "sandbox_image_digest": None,
                "detail": self.detail}


class AuthoredEvaluationEnded(Exception):
    def __init__(self, end: AuthoredEnd):
        super().__init__(f"{end.event}: {end.detail}")
        self.end = end


def sandbox_cause(exc: BaseException) -> Optional[Exception]:
    """The sandbox error behind ``exc``: the SP2c evaluation wraps backtest failures in EvaluationError."""
    seen: set[int] = set()
    current: Optional[BaseException] = exc
    while current is not None and id(current) not in seen:
        if isinstance(current, (SandboxFault, SandboxUnavailable)):
            return current
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return None


def end_for(cause: Exception) -> AuthoredEnd:
    if isinstance(cause, SandboxFault):
        return AuthoredEnd(SANDBOX_ERROR_EVENT, str(cause.code)[:DETAIL_MAX])
    return AuthoredEnd(SANDBOX_UNAVAILABLE_EVENT, type(cause).__name__)
```

- [ ] **Step 5: Implement `trader/research/authored_evaluation.py`**

```python
"""The research service's ``evaluate`` (SP3 spec 7.6): a catalogue key exactly as SP2c; an authored key is
reported STARTED, smoke-tested, then evaluated by the same cohort evaluation through the research sandbox."""
from __future__ import annotations

import datetime as dt
from dataclasses import replace
from typing import Any, Callable

import pandas as pd

from trader.authored.sandbox_client import SandboxUnavailable
from trader.messaging.typed_rpc import TypedRpcRemoteError
from trader.research import authored_backtest            # module reference: tests replace connect_sandbox
from trader.research.authored_outcome import AuthoredEvaluationEnded, end_for, sandbox_cause, started_body
from trader.research.cohort import CohortSpec
from trader.research.cohort_evaluation import CohortResult, evaluate_cohort
from trader.research.evaluation import (EvaluationPaths, _day_end, _day_start, _period_session_dates, _point_key,
                                        run_environment)
from trader.research.evaluation_jobs import RunEnvironment, WindowJob, run_window_job
from trader.research.evaluation_service import ServiceStateError
from trader.research.experiment_registry import ExperimentRegistry
from trader.research.rulesets.paper_v1 import PAPER_V1
from trader.research.validation import generate_walk_forward

SMOKE_SESSIONS = 20


def smoke_window(spec: CohortSpec, paths: EvaluationPaths) -> tuple[RunEnvironment, WindowJob]:
    """Plan 4 ruling 7: the first sessions of the period, all before the holdout, on the smallest conid."""
    base = spec.base
    plan = generate_walk_forward((base.period_start, base.period_end), n_folds=base.folds,
                                 embargo=base.embargo_sessions, holdout=base.holdout_sessions,
                                 calendar_name=base.calendar)
    holdout_start = pd.Timestamp(plan.holdout.start).date()
    sessions = [day for day in _period_session_dates(base) if day < holdout_start][:SMOKE_SESSIONS]
    params = dict(spec.cohort[0])
    env = replace(run_environment(base, paths), conids=(min(base.conids),))
    job = WindowJob(_point_key(params), params, "smoke", 0, _day_start(sessions[0]), _day_end(sessions[-1]), 1.0)
    return env, job


class AuthoredEvaluator:
    def __init__(self, *, research_db: Any, paths: EvaluationPaths, now: Callable[[], dt.datetime], trader: Any,
                 ruleset=PAPER_V1, run_cohort: Callable[..., Any] = evaluate_cohort,
                 run_window: Callable[[RunEnvironment, WindowJob], Any] = run_window_job):
        self._db, self._paths, self._now, self._trader, self._ruleset = research_db, paths, now, trader, ruleset
        self._run_cohort, self._run_window = run_cohort, run_window

    def evaluate(self, spec: CohortSpec) -> CohortResult:
        if spec.base.authored_digest is None:
            return self._run_cohort(spec, research_db=self._db, paths=self._paths, now=self._now,
                                    ruleset=self._ruleset)
        judged_count = self._start(spec)
        self._smoke(spec)
        return self._evaluate_authored(spec, judged_count)

    def _start(self, spec: CohortSpec) -> int:
        """Plan 4 ruling 6: STARTED names the image the research sandbox runs; the reply carries the count of
        judged authored files (spec 7.7). An unreachable trader (TraderUnavailable) ends the worker."""
        digest = spec.base.authored_digest
        try:
            image_digest = authored_backtest.connect_sandbox(self._paths.sandbox_socket).ping().image_digest
        except SandboxUnavailable as exc:
            raise AuthoredEvaluationEnded(end_for(exc)) from exc
        try:
            reply = self._trader.report_authored_evaluation(started_body(spec.request_id, digest, image_digest))
        except TypedRpcRemoteError as remote:
            raise ServiceStateError(f"STARTED for {digest}: {remote.code}: {remote.message}") from remote
        if reply.get("status") not in ("UPDATED", "UNCHANGED"):
            raise ServiceStateError(f"the trader refused STARTED for {digest}: {reply.get('code')}: "
                                    f"{reply.get('detail')}")
        judged_count = reply.get("judged_count")
        if type(judged_count) is not int or judged_count < 0:
            raise ServiceStateError(f"STARTED for {digest}: judged_count {judged_count!r} is not a count")
        return judged_count

    def _smoke(self, spec: CohortSpec) -> None:
        """Before any family, trial or holdout exists: a fault here spends nothing (spec 7.6)."""
        env, job = smoke_window(spec, self._paths)
        try:
            self._run_window(env, job)
        except Exception as exc:
            cause = sandbox_cause(exc)
            if cause is None:
                raise
            raise AuthoredEvaluationEnded(end_for(cause)) from exc

    def _evaluate_authored(self, spec: CohortSpec, judged_count: int) -> CohortResult:
        base = spec.base
        registry = ExperimentRegistry(self._db)
        holdouts_before = len(registry.opened_holdout_windows(base.strategy_path, base.class_name))
        try:
            return self._run_cohort(spec, research_db=self._db, paths=self._paths, now=self._now,
                                    ruleset=self._ruleset, max_workers=1, authored_judged_count=judged_count)
        except Exception as exc:
            cause = sandbox_cause(exc)
            if cause is None:
                raise
            holdouts_after = len(registry.opened_holdout_windows(base.strategy_path, base.class_name))
            if isinstance(cause, SandboxUnavailable) and holdouts_after > holdouts_before:
                raise                       # spec 8: the window counts as revealed; SP2c signs a FAILED case
            raise AuthoredEvaluationEnded(end_for(cause)) from exc
```

- [ ] **Step 6: Run the tests**

Run: `.venv/bin/python -m pytest tests/research/test_authored_evaluation.py tests/research/test_evaluation_service.py -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add trader/research/authored_outcome.py trader/research/authored_evaluation.py trader/research/trader_port.py \
        tests/research/service_fakes.py tests/research/test_authored_evaluation.py
git commit -m "feat: report started, smoke-test and map sandbox errors of an authored evaluation

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: Report the end to the trader and close the claim without a case

**Files:**
- Modify: `trader/research/evaluation_service.py` (`_run_row`, `_evaluate_and_sign`, `_report_end`; new `_end_without_case`, `_authored_outcome_delivered`), `trader/research/service_store.py` (`hold_report` annotation), `trader/research_service.py` (wiring)
- Test: `tests/research/test_authored_evaluation_service.py`

**Interfaces:**
- Consumes: Task 6 `AuthoredEnd`, `AuthoredEvaluationEnded`, `AUTHORED_END_CODE`, `AuthoredEvaluator`, `TraderPort.report_authored_evaluation`, `FakeTrader`; Plan 1 `parse_authored_key`.
- Produces: research's `evaluate` is `AuthoredEvaluator.evaluate`; an authored end reads FAILED with no case; `_evaluate_and_sign` lets `AuthoredEvaluationEnded`, `ServiceStateError` and `TraderUnavailable` through.

- [ ] **Step 1: Write the failing tests**

```python
# tests/research/test_authored_evaluation_service.py
"""Plan 4 rulings 6, 10 and 11: an authored end reaches the trader, then the claim closes FAILED without a case."""
import logging

import pytest

from tests.authored.fake_sandbox import FakeSandbox
from tests.research.authored_fixtures import NOW, research_world, time_of_day_key
from tests.research.evaluation_fixtures import CONIDS
from tests.research.service_fakes import AI, FakeTrader
from trader.authored.keys import parse_authored_key
from trader.research.authored_outcome import AUTHORED_END_CODE, AuthoredEnd, AuthoredEvaluationEnded
from trader.research.evaluation_service import EvaluationService, ServiceStateError
from trader.research.service_store import ResearchStore
from trader.research.signing import AttestationSigner
from trader.research.trader_port import TraderUnavailable

SIGNER = AttestationSigner.generate()
KEY = time_of_day_key(FakeSandbox())
DIGEST = parse_authored_key(KEY)[0]
UNAVAILABLE_END = AuthoredEnd("SANDBOX_UNAVAILABLE", "SandboxUnavailable")
ERROR_END = AuthoredEnd("SANDBOX_ERROR", "SANDBOX_ERROR")


@pytest.fixture
def world(tmp_path, tmp_duckdb_path):
    return research_world(tmp_path, tmp_duckdb_path)


def raising(error):
    def evaluate(spec):
        raise error
    return evaluate


def never(spec):
    raise AssertionError("an authored end was evaluated again")


def service(world, trader, evaluate, store=None):
    return EvaluationService(store=store or ResearchStore(world.db), trader=trader, build_spec=world.build_body,
                             evaluate=evaluate, signer=SIGNER, artifacts_root=world.root / "artifacts",
                             warmup_sessions=5, order_notional=1900.0, queue_max=2, now=lambda: NOW)


def submit(svc):
    reply = svc.submit({"kind": "INITIAL", "strategy_key": KEY, "cohort": [{"ENTRY_MINUTE": 600, "EXIT_MINUTE": 660}],
                        "conids": CONIDS, "bar_size": "15 mins"}, AI)
    assert reply["status"] == "ACCEPTED", reply
    return reply["request_id"]


def no_case_signed(world):
    cases = world.root / "artifacts" / "cases"
    return not cases.exists() or not any(cases.iterdir())


def test_an_authored_end_reaches_the_trader_then_the_claim_fails_without_a_case(world):
    trader = FakeTrader()
    svc = service(world, trader, raising(AuthoredEvaluationEnded(ERROR_END)))
    request_id = submit(svc)
    assert svc.run_next()
    assert trader.authored_reports == [ERROR_END.report_body(request_id, DIGEST)]
    assert trader.claims[request_id]["state"] == "FAILED" and not trader.reported_after_close
    assert svc.get(request_id, AI) == {"found": True, "request_id": request_id, "state": "FAILED",
                                       "case_digest": None, "summary": None}
    assert no_case_signed(world)


def test_a_lost_report_holds_the_claim_open_until_the_next_tick(world):
    trader = FakeTrader()
    trader.lose_authored_report = True
    svc = service(world, trader, raising(AuthoredEvaluationEnded(UNAVAILABLE_END)))
    request_id = submit(svc)
    svc.run_next()
    assert trader.authored_reports == [] and trader.claims[request_id]["state"] != "FAILED"
    assert svc.get(request_id, AI)["state"] == "RUNNING"
    svc.run_next()
    assert [r["event"] for r in trader.authored_reports] == ["SANDBOX_UNAVAILABLE"]
    assert trader.claims[request_id]["state"] == "FAILED" and not trader.reported_after_close


def test_an_end_decided_before_a_crash_is_never_evaluated_again(world):
    trader, store = FakeTrader(), ResearchStore(world.db)
    request_id = submit(service(world, trader, never, store=store))
    store.set_state(request_id, "RUNNING", now=NOW, code=AUTHORED_END_CODE, detail=UNAVAILABLE_END.marker())
    restarted = service(world, trader, never, store=store)
    restarted.recover()
    restarted.run_next()
    assert [r["event"] for r in trader.authored_reports] == ["SANDBOX_UNAVAILABLE"]
    assert trader.claims[request_id]["state"] == "FAILED"


def test_a_final_refusal_parks_the_claim_visibly_and_reconciles_later(world, caplog, monkeypatch):  # review r1 #7
    trader = FakeTrader()
    trader.refuse_authored_report = ("AUTHORED_STATE_CONFLICT", False)
    svc = service(world, trader, raising(AuthoredEvaluationEnded(ERROR_END)))
    request_id = submit(svc)
    with caplog.at_level(logging.ERROR):
        svc.run_next()
        svc.run_next()                                        # parked: not resent on every tick
    assert trader.claims[request_id]["state"] != "FAILED"     # the claim is not closed on a refusal
    assert svc.get(request_id, AI)["state"] == "RUNNING"      # visible: the request stays open, no case
    assert caplog.text.count("AUTHORED_REPORT_REFUSED") == 1 and "AUTHORED_STATE_CONFLICT" in caplog.text
    assert no_case_signed(world)
    monkeypatch.setattr("trader.research.evaluation_service.PARKED_RESEND_SECONDS", 0)
    trader.refuse_authored_report = None                      # the operator repaired the trader record
    svc.run_next()
    assert [r["event"] for r in trader.authored_reports] == ["SANDBOX_ERROR"]
    assert trader.claims[request_id]["state"] == "FAILED"


def test_a_retryable_refusal_is_sent_again(world):
    trader = FakeTrader()
    trader.refuse_authored_report = ("TRADER_BUSY", True)
    svc = service(world, trader, raising(AuthoredEvaluationEnded(ERROR_END)))
    request_id = submit(svc)
    svc.run_next()
    assert trader.claims[request_id]["state"] != "FAILED"
    trader.refuse_authored_report = None
    svc.run_next()
    assert trader.claims[request_id]["state"] == "FAILED" and len(trader.authored_reports) == 1


def test_a_refused_start_parks_the_request_without_a_case(world):
    svc = service(world, FakeTrader(), raising(ServiceStateError("the trader refused STARTED")))
    request_id = submit(svc)
    svc.run_next()
    view = svc.get(request_id, AI)
    assert (view["state"], view["case_digest"]) == ("FAILED", None) and no_case_signed(world)


def test_an_unreachable_trader_while_starting_ends_the_worker_and_signs_nothing(world):
    svc = service(world, FakeTrader(), raising(TraderUnavailable("report_authored_evaluation: no route")))
    submit(svc)
    with pytest.raises(TraderUnavailable):
        svc.run_next()
    assert no_case_signed(world)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -m pytest tests/research/test_authored_evaluation_service.py -q`
Expected: FAIL (the first test signs a FAILED case: the generic handler catches `AuthoredEvaluationEnded`).

- [ ] **Step 3: Implement the service change**

In `trader/research/evaluation_service.py` add:

```python
from trader.authored.keys import parse_authored_key
from trader.research.authored_outcome import AUTHORED_END_CODE, AuthoredEnd, AuthoredEvaluationEnded

# Review r1 #7: an authored end the trader refused for good is parked, resent hourly (and at a restart), and the
# claim stays open; refusal is never delivery.
PARKED_RESEND_SECONDS = 3600
```

(add `import datetime as dt` if the module lacks it), and at the end of `EvaluationService.__init__`:

```python
        self._refused_at: dict[str, dt.datetime] = {}         # request id -> when the trader last refused its end
```

Replace `_run_row`:

```python
    def _run_row(self, request_id: str) -> None:
        row = self._store.get(request_id)
        if row["state"] not in OPEN_STATES or row["pending_report"] is not None:
            return
        if AuthoredEnd.from_row(row) is not None:             # decided before a crash: never run again
            self._end_without_case(request_id)
            return
        self._store.set_state(request_id, "RUNNING", now=self._now())
        signed = self._store.case(request_id=request_id)
        if signed is None:
            self._report_running(request_id)
            try:
                digest, case = self._evaluate_and_sign(row)
            except AuthoredEvaluationEnded as ended:
                logger.warning("authored evaluation %s ended %s (%s); no case is signed", request_id,
                               ended.end.event, ended.end.detail)
                self._store.set_state(request_id, "RUNNING", now=self._now(), code=AUTHORED_END_CODE,
                                      detail=ended.end.marker())
                self._end_without_case(request_id)
                return
        else:                                                   # signed before a crash: never run twice
            digest = signed["case_digest"]
            case = load_verified_case(self._cases_dir, digest, {self._signer.public_key_id: self._signer.public_key})
        final = "FAILED" if case.stage == "FAILED" else "DONE"   # the trader refuses CASE_CLAIM_MISMATCH otherwise
        self._store.hold_report(request_id, final, now=self._now(), case_digest=digest,
                                summary=evaluation_summary(case, order_notional=self._order_notional))
        self._report_end(request_id, final)

    def _end_without_case(self, request_id: str) -> None:
        """Plan 4 ruling 11: get_evaluation reads FAILED with no case, so no model ever judges it."""
        self._store.hold_report(request_id, "FAILED", now=self._now(), case_digest=None, summary=None)
        self._report_end(request_id, "FAILED")
```

In `_evaluate_and_sign`, the first handler of the `try` becomes:

```python
        except (AuthoredEvaluationEnded, ServiceStateError, TraderUnavailable):
            raise                       # not a case: an authored end, a parked request or an unreachable trader
        except RequestRefused as refusal:
```

`_report_end` gets a first step:

```python
    def _report_end(self, request_id: str, final: str) -> bool:
        """Ruling 24: confirmed by UPDATED or UNCHANGED, or after a lost reply by reading the claim back."""
        if not self._authored_outcome_delivered(request_id):
            return False
        try:
```

(the rest is unchanged), and the new method:

```python
    def _authored_outcome_delivered(self, request_id: str) -> bool:
        """Plan 4 ruling 10: an authored end reaches the trader before its claim closes, so the record never
        stays EVALUATING behind a closed claim. Resent every tick until the trader answers; a final refusal parks
        the request (ruling 10, review r1 #7): never treated as delivered."""
        row = self._store.get(request_id)
        end = AuthoredEnd.from_row(row)
        if end is None:
            return True
        refused_at = self._refused_at.get(request_id)
        if refused_at is not None and self._now() < refused_at + dt.timedelta(seconds=PARKED_RESEND_SECONDS):
            return False
        digest, _ = parse_authored_key(row["strategy_key"])
        try:
            reply = self._trader.report_authored_evaluation(end.report_body(request_id, digest))
        except TraderUnavailable:
            logger.warning("authored evaluation %s: %s not reported yet; sent again next tick", request_id, end.event)
            return False
        if reply.get("status") in ("UPDATED", "UNCHANGED"):
            return True
        if reply.get("retryable"):
            logger.warning("authored evaluation %s: the trader asks to resend (%s); sent again next tick",
                           request_id, reply.get("code"))
            return False
        self._refused_at[request_id] = self._now()
        logger.error("AUTHORED_REPORT_REFUSED: authored evaluation %s: the trader refused %s (%s: %s); the claim "
                     "stays open and the end is sent again in %s s (after a restart: at once); check "
                     "`mmr ai-author list` for digest %s", request_id, end.event, reply.get("code"),
                     reply.get("detail"), PARKED_RESEND_SECONDS, digest)
        return False
```

In `trader/research/service_store.py` the signature of `hold_report` becomes `def hold_report(self, request_id: str, final: str, *, now: dt.datetime, case_digest: Optional[str], summary: Optional[dict]) -> None:` (an authored end holds neither; `json.dumps(None)` stores `null`, which `_row` reads back as `None`).

In `trader/research_service.py` import `AuthoredEvaluator` from `trader.research.authored_evaluation`, drop the `evaluate_cohort` import, and build the service with it:

```python
    evaluator = AuthoredEvaluator(research_db=db, paths=paths, now=now, trader=trader)
    evaluations = EvaluationService(
        store=store, trader=trader, build_spec=build_spec, evaluate=evaluator.evaluate, signer=signer,
        artifacts_root=ARTIFACTS_ROOT, warmup_sessions=judge.shadow_warmup_sessions,
        order_notional=config.order_notional, queue_max=config.queue_max, now=now, renewals=renewals)
```

- [ ] **Step 4: Run the tests and the SP2c service tests**

Run: `.venv/bin/python -m pytest tests/research/test_authored_evaluation_service.py tests/research/test_evaluation_service.py tests/research/test_research_service_process.py tests/research/test_research_surface.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add trader/research/evaluation_service.py trader/research/service_store.py trader/research_service.py \
        tests/research/test_authored_evaluation_service.py
git commit -m "feat: report a sandbox end of an authored evaluation and close its claim without a case

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8: Attestation and the shadow replay of authored keys

**Files:**
- Modify: `trader/research/judgment_attest.py` (`_check_binding`, new `_require_judged_source`), `trader/research/shadow_replay.py` (`_source_problem`, `_environment`, `_row`, new `replay_failure_code`)
- Test: `tests/research/test_authored_attest_shadow.py`

**Interfaces:**
- Consumes: Plan 3 `split_evaluation_key`, `is_authored_path`, `authored_digest_of_path`; Task 4 `EvaluationPaths.sandbox_socket`; Task 6 `sandbox_cause`.
- Produces: `JudgmentAttest._require_judged_source(path, file_hash)` (raises `_Refused("STRATEGY_SOURCE_CHANGED", ...)`); `shadow_replay.replay_failure_code(exc) -> str` (`SANDBOX_UNAVAILABLE` or `REPLAY_ERROR`); the shadow `RunEnvironment` of an authored case names the digest and `paths.sandbox_socket`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/research/test_authored_attest_shadow.py
"""Plan 4 ruling 23 (attest) and spec 7.6 (shadow replay through the sandbox) for authored keys."""
from types import SimpleNamespace

import pytest

from tests.authored.fake_sandbox import fault
from trader.authored.keys import authored_key
from trader.authored.sandbox_client import SandboxUnavailable
from trader.research.evaluation import EvaluationError
from trader.research.judgment_attest import JudgmentAttest, _Refused
from trader.research.shadow_replay import ShadowReplay, replay_failure_code
from trader.research.strategy_key import authored_strategy_path
from trader.sandbox.protocol import RESEARCH_SOCKET, SANDBOX_ERROR

DIGEST = "sha256:" + "c" * 64
KEY = authored_key(DIGEST, "OpeningRangeBreakout")
PATH = authored_strategy_path(DIGEST)


def attest(tmp_path):
    instance = object.__new__(JudgmentAttest)
    instance._repo_root = tmp_path
    return instance


def test_an_authored_source_is_checked_by_its_digest_without_a_read(tmp_path):
    attest(tmp_path)._require_judged_source(PATH, DIGEST)       # no file exists anywhere


def test_an_authored_source_with_another_digest_is_refused(tmp_path):
    with pytest.raises(_Refused) as refused:
        attest(tmp_path)._require_judged_source(PATH, "sha256:" + "d" * 64)
    assert refused.value.code == "STRATEGY_SOURCE_CHANGED"


def test_a_catalogue_source_is_still_read_from_the_repo(tmp_path):
    with pytest.raises(_Refused) as refused:
        attest(tmp_path)._require_judged_source("strategies/gone.py", DIGEST)
    assert refused.value.code == "STRATEGY_SOURCE_CHANGED"


def shadow(tmp_path):
    instance = object.__new__(ShadowReplay)
    instance._paths = SimpleNamespace(repo_root=tmp_path, history_db="h", universe_db="u", universe_library="U",
                                      execution_costs="c.yaml", sandbox_socket=RESEARCH_SOCKET)
    return instance


def case(file_hash=DIGEST):
    return SimpleNamespace(strategy_key=KEY, strategy_file_hash=file_hash, conids=[1001, 1002], bar_size="1 min")


def test_the_shadow_replay_of_an_authored_case_runs_behind_the_research_socket(tmp_path):
    family = SimpleNamespace(cost_model={"order_notional": 1900.0, "account_equity": 100000.0,
                                         "max_gross_allocation": 0.05})
    env = shadow(tmp_path)._environment(case(), family)
    assert (env.authored_digest, env.sandbox_socket, env.strategy_file) == (DIGEST, RESEARCH_SOCKET, PATH)


def test_the_shadow_source_check_uses_the_digest(tmp_path):
    assert shadow(tmp_path)._source_problem(case()) is None
    assert shadow(tmp_path)._source_problem(case("sha256:" + "d" * 64)).startswith("STRATEGY_SOURCE_CHANGED")


def test_an_unreachable_sandbox_names_itself_in_the_shadow_row():
    assert replay_failure_code(EvaluationError("x")) == "REPLAY_ERROR"
    try:
        raise EvaluationError("backtest failed") from SandboxUnavailable("gone")
    except EvaluationError as wrapped:
        assert replay_failure_code(wrapped) == "SANDBOX_UNAVAILABLE"
    assert replay_failure_code(fault(SANDBOX_ERROR)) == "REPLAY_ERROR"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -m pytest tests/research/test_authored_attest_shadow.py -q`
Expected: FAIL (`ImportError: cannot import name 'replay_failure_code'`).

- [ ] **Step 3: Implement the attestation check**

In `trader/research/judgment_attest.py` replace the import of `split_strategy_key` with `from trader.research.strategy_key import authored_digest_of_path, is_authored_path, split_evaluation_key`; in `_check_binding`, `path, class_name = split_strategy_key(case.strategy_key)` becomes `path, class_name = split_evaluation_key(case.strategy_key)`, and its end becomes:

```python
        if not bound:
            raise _Refused("JUDGMENT_MISMATCH", "the case and the registry name different bindings")
        self._require_judged_source(path, case.strategy_file_hash)
        return artifact

    def _require_judged_source(self, path: str, file_hash: str) -> None:
        """The judged bytes are still the strategy. An authored file is content-addressed: its path names its
        digest, and research holds no store to read (Plan 4 ruling 23)."""
        if is_authored_path(path):
            if authored_digest_of_path(path) != file_hash:
                raise _Refused("STRATEGY_SOURCE_CHANGED", "the authored file is not the evaluated one")
            return
        try:
            current = (Path(self._repo_root) / path).read_bytes()
        except OSError:
            raise _Refused("STRATEGY_SOURCE_CHANGED", "the strategy file is no longer readable") from None
        if "sha256:" + hashlib.sha256(current).hexdigest() != file_hash:
            raise _Refused("STRATEGY_SOURCE_CHANGED", "the strategy file changed after the evaluation")
```

(the old file read at the end of `_check_binding` moves into `_require_judged_source`).

- [ ] **Step 4: Implement the shadow replay changes**

In `trader/research/shadow_replay.py` replace the `split_strategy_key` import and add:

```python
from trader.authored.sandbox_client import SandboxUnavailable
from trader.research.authored_outcome import sandbox_cause
from trader.research.strategy_key import authored_digest_of_path, is_authored_path, split_evaluation_key
```

Add next to `bar_seconds`:

```python
def replay_failure_code(exc: BaseException) -> str:
    """An unreachable sandbox is not the strategy's fault: the row says so (it waits, then reads INCOMPLETE)."""
    return "SANDBOX_UNAVAILABLE" if isinstance(sandbox_cause(exc), SandboxUnavailable) else "REPLAY_ERROR"
```

In `_row`:

```python
        except Exception as exc:                        # strategy code or data: the row says why, the log says more
            logger.exception("shadow replay of %s on %s failed", member["judgment_id"], session)
            return self._incomplete(member, case, session, f"{replay_failure_code(exc)}: {type(exc).__name__}")
```

`_source_problem`:

```python
    def _source_problem(self, case: Any) -> Optional[str]:
        path, _ = split_evaluation_key(case.strategy_key)
        if is_authored_path(path):                      # content-addressed; only the sandbox reads it
            if authored_digest_of_path(path) != case.strategy_file_hash:
                return "STRATEGY_SOURCE_CHANGED: the authored file is not the judged one"
            return None
        try:
            source = (Path(self._paths.repo_root) / path).read_bytes()
        except FileNotFoundError:
            return "STRATEGY_SOURCE_CHANGED: the strategy file is gone"
        if "sha256:" + hashlib.sha256(source).hexdigest() != case.strategy_file_hash:
            return "STRATEGY_SOURCE_CHANGED: the strategy file differs from the judged one"
        return None
```

`_environment`:

```python
    def _environment(self, case: Any, family: Any) -> RunEnvironment:
        cost = family.cost_model
        path, class_name = split_evaluation_key(case.strategy_key)
        authored = is_authored_path(path)
        return RunEnvironment(
            history_db=self._paths.history_db, universe_db=self._paths.universe_db,
            universe_library=self._paths.universe_library, execution_costs_path=self._paths.execution_costs,
            strategy_file=path if authored else str(Path(self._paths.repo_root) / path), class_name=class_name,
            conids=tuple(case.conids), bar_size=case.bar_size, order_notional=float(cost["order_notional"]),
            account_equity=float(cost["account_equity"]),
            max_gross_allocation=float(cost["max_gross_allocation"]),
            regular_session_calendar=SHADOW_CALENDAR,          # the bars the evaluation backtested
            authored_digest=authored_digest_of_path(path) if authored else None,
            sandbox_socket=self._paths.sandbox_socket if authored else None)
```

- [ ] **Step 5: Run the tests and the SP2c attest and shadow tests**

Run: `.venv/bin/python -m pytest tests/research/test_authored_attest_shadow.py tests/research/test_judgment_attest.py tests/research/test_shadow_replay.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add trader/research/judgment_attest.py trader/research/shadow_replay.py tests/research/test_authored_attest_shadow.py
git commit -m "feat: attest and shadow-replay authored strategies by digest through the research sandbox

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---
### Task 9: The live proxy

**Files:**
- Create: `trader/strategy/authored_runtime.py` (constants and `AuthoredLiveStrategy`)
- Test: `tests/strategy/test_authored_live.py`

**Interfaces:**
- Consumes: Task 2 `BarStream`, `decision_signal`, `utc_time`; Plan 2 `SandboxClient.open_session(..., conids)`, `SandboxFault.code`, `SandboxUnavailable`. The runtime stamps `_dispatch_conid` before `on_prices` and calls it once per new completed bar (`_on_tick_for_strategy`), on one tick thread.
- Produces: `trader.strategy.authored_runtime`: `WARMUP_BARS = 2000`, alarm codes `SANDBOX_UNAVAILABLE_ALARM`, `SANDBOX_FAULT_ALARM`, `ISOLATION_ALARM`, `DIGEST_ALARM`; `AuthoredLiveStrategy(*, client, digest, class_name, params, bar_size, conids, on_fault: Callable[[str, str], None], proof_gate: Callable[[str], bool])` with `digest`, `conids`, `on_prices(prices) -> Optional[Signal]`, `authored_binding_intact() -> bool`, `close_sessions()`. One live session per instance (one deployment) per New York day, serving all its conids (ruling 14, decision D1).

- [ ] **Step 1: Write the failing tests**

```python
# tests/strategy/test_authored_live.py
"""SP3 spec 7.6 as amended (live): one session per deployment per New York day for all its conids, a warm-up for
each conid's first bar in a session, an alarm on availability errors, retirement on a fault. Plan 4 rulings 14-18."""
import logging

import numpy as np
import pandas as pd

from tests.authored.fake_sandbox import FakeSandbox, caller_source
from trader.objects import Action, BarSize
from trader.sandbox.protocol import SANDBOX_DIGEST_MISMATCH, SANDBOX_ERROR
from trader.strategy.authored_runtime import (ISOLATION_ALARM, SANDBOX_FAULT_ALARM, SANDBOX_UNAVAILABLE_ALARM,
                                              AuthoredLiveStrategy)
from trader.trading.strategy import StrategyContext, StrategyState

CONID, OTHER = 265598, 76792991


def bars(count, start="2024-03-04 14:30"):
    index = pd.date_range(start, periods=count, freq="1min", tz="UTC", name="date")
    closes = [100.0 + i for i in range(count)]
    return pd.DataFrame({"open": closes, "high": closes, "low": closes, "close": closes, "volume": 1000.0},
                        index=index)


def live(sandbox, faults, name="counts_bars", class_name="CountsBars", params=None, conids=(CONID,),
         proven=None):
    digest = sandbox.add(caller_source(name))
    proven = {sandbox.image_digest} if proven is None else proven     # the builds with a passing proof
    strategy = AuthoredLiveStrategy(client=sandbox, digest=digest, class_name=class_name, params=params or {},
                                    bar_size="1 min", conids=conids,
                                    on_fault=lambda d, code: faults.append((d, code)),
                                    proof_gate=lambda build: build in proven)
    strategy.install(StrategyContext(name="aidv-test", bar_size=BarSize.Mins1, conids=list(conids), universe=None,
                                     historical_days_prior=5, paper_only=True, storage=None, universe_accessor=None,
                                     logger=logging))
    strategy.ai_source_digest = digest
    strategy.enable()
    strategy._dispatch_conid = CONID
    return strategy


def test_the_first_bar_warms_the_session_and_answers_for_the_newest_only():
    sandbox, faults = FakeSandbox(), []
    strategy, frame = live(sandbox, faults), bars(30)
    signal = strategy.on_prices(frame.iloc[:20])
    assert (signal.action, signal.probability) == (Action.BUY, 20 / 10000)
    assert len(sandbox.sessions) == 1 and len(sandbox.sessions[0].stamps) == 20
    assert strategy.on_prices(frame.iloc[:21]).probability == 21 / 10000
    assert len(sandbox.sessions) == 1 and len(sandbox.sessions[0].stamps) == 21


def test_the_conids_of_a_deployment_share_one_session_with_their_own_histories():
    sandbox, faults = FakeSandbox(), []
    strategy = live(sandbox, faults, conids=(CONID, OTHER))
    strategy._dispatch_conid = CONID
    assert strategy.on_prices(bars(20)).probability == 20 / 10000
    strategy._dispatch_conid = OTHER
    assert strategy.on_prices(bars(5)).probability == 5 / 10000          # OTHER warms with its own bars only
    strategy._dispatch_conid = CONID
    assert strategy.on_prices(bars(21)).probability == 21 / 10000
    assert len(sandbox.sessions) == 1 and sandbox.sessions[0].conids == (CONID, OTHER)
    assert (len(sandbox.sessions[0].stamps_of(CONID)), len(sandbox.sessions[0].stamps_of(OTHER))) == (21, 5)


def test_a_conid_the_deployment_does_not_bind_gets_no_signal_and_sends_nothing():
    sandbox, faults = FakeSandbox(), []
    strategy = live(sandbox, faults)
    strategy._dispatch_conid = OTHER
    assert strategy.on_prices(bars(5)) is None
    assert sandbox.sessions == [] and faults == []


def test_no_signal_from_a_session_on_an_unproven_build_and_the_strategy_stays(caplog):     # review r1 #6
    sandbox, faults, proven = FakeSandbox(), [], set()
    strategy = live(sandbox, faults, proven=proven)
    with caplog.at_level(logging.ERROR):
        assert strategy.on_prices(bars(5)) is None                         # the first OPEN: build not proven
    assert ISOLATION_ALARM in caplog.text and sandbox.sessions == [] and faults == []   # never OPENED
    proven.add(sandbox.image_digest)
    assert strategy.on_prices(bars(6)).probability == 6 / 10000           # proven now: a fresh warmed session
    assert strategy.state == StrategyState.RUNNING


def test_a_reconnect_to_a_rebuilt_unproven_sandbox_gives_no_signal(caplog):              # review r1 #6
    sandbox, faults = FakeSandbox(), []
    proven = {sandbox.image_digest}
    strategy = live(sandbox, faults, proven=proven)
    assert strategy.on_prices(bars(3)).probability == 3 / 10000
    sandbox.unavailable = True
    assert strategy.on_prices(bars(4)) is None
    sandbox.unavailable, sandbox.image_digest = False, "e4f5a6b-20261012T090000Z"   # restarted on build B
    with caplog.at_level(logging.ERROR):
        assert strategy.on_prices(bars(5)) is None
    assert ISOLATION_ALARM in caplog.text and len(sandbox.sessions) == 1 and sandbox.open_sessions == []
    proven.add(sandbox.image_digest)
    assert strategy.on_prices(bars(6)).probability == 6 / 10000 and len(sandbox.sessions) == 2


def test_a_restart_between_ping_and_open_is_caught_by_the_opened_build():
    sandbox, faults = FakeSandbox(), []
    proven = {sandbox.image_digest}
    strategy = live(sandbox, faults, proven=proven)
    real_open = sandbox.open_session

    def restart_then_open(*args, **kwargs):
        sandbox.image_digest = "e4f5a6b-20261012T090000Z"                # the supervisor restarted on build B
        return real_open(*args, **kwargs)
    sandbox.open_session = restart_then_open
    assert strategy.on_prices(bars(5)) is None and sandbox.open_sessions == []


def test_a_proof_withdrawn_mid_session_stops_the_signals_at_the_next_bar():
    sandbox, faults = FakeSandbox(), []
    proven = {sandbox.image_digest}
    strategy = live(sandbox, faults, proven=proven)
    assert strategy.on_prices(bars(3)) is not None
    proven.clear()                                                          # a later failing run of that build
    assert strategy.on_prices(bars(4)) is None and sandbox.open_sessions == []


def test_each_signal_saw_exactly_the_bars_sent_capped_at_lookback():
    sandbox, faults = FakeSandbox(), []
    strategy, frame = live(sandbox, faults), bars(60)
    probabilities = [strategy.on_prices(frame.iloc[:i]).probability for i in range(1, 61)]
    assert probabilities == [min(i, 50) / 10000 for i in range(1, 61)]


def test_bars_skipped_between_calls_are_sent_and_only_the_newest_counts():
    sandbox, faults = FakeSandbox(), []
    strategy, frame = live(sandbox, faults), bars(30)
    strategy.on_prices(frame.iloc[:20])
    assert strategy.on_prices(frame.iloc[:23]).probability == 23 / 10000
    assert len(sandbox.sessions[0].stamps) == 23


def test_a_reconnect_gives_the_same_signal_as_no_drop(caplog):
    sandbox, faults = FakeSandbox(), []
    strategy, frame = live(sandbox, faults), bars(30)
    strategy.on_prices(frame.iloc[:20])
    sandbox.unavailable = True
    with caplog.at_level(logging.INFO):
        assert strategy.on_prices(frame.iloc[:21]) is None
        assert strategy.on_prices(frame.iloc[:22]) is None
        sandbox.unavailable = False
        signal = strategy.on_prices(frame.iloc[:23])
    assert signal.probability == 23 / 10000 and len(sandbox.sessions) == 2
    assert caplog.text.count(SANDBOX_UNAVAILABLE_ALARM) == 1
    assert "answers again" in caplog.text
    assert strategy.state == StrategyState.RUNNING and faults == []


def test_a_new_new_york_day_opens_a_new_warmed_session():
    sandbox, faults = FakeSandbox(), []
    strategy = live(sandbox, faults)
    frame = pd.concat([bars(10, start="2024-03-04 20:50"), bars(1, start="2024-03-05 14:30")])
    strategy.on_prices(frame.iloc[:10])
    signal = strategy.on_prices(frame)
    assert len(sandbox.sessions) == 2 and sandbox.sessions[0].closed
    assert signal.probability == 11 / 10000


def test_a_conid_still_on_yesterday_does_not_roll_the_session_back():
    sandbox, faults = FakeSandbox(), []
    strategy = live(sandbox, faults, conids=(CONID, OTHER))
    today = pd.concat([bars(10, start="2024-03-04 20:50"), bars(1, start="2024-03-05 14:30")])
    strategy._dispatch_conid = CONID
    strategy.on_prices(today)                                            # opens the 2024-03-05 session
    strategy._dispatch_conid = OTHER
    assert strategy.on_prices(bars(10, start="2024-03-04 20:50")).probability == 10 / 10000
    assert len(sandbox.sessions) == 1 and not sandbox.sessions[0].closed


def test_a_full_live_pool_is_an_availability_error_and_the_strategy_stays(caplog):
    sandbox, faults = FakeSandbox(pool=0), []
    strategy = live(sandbox, faults)
    with caplog.at_level(logging.ERROR):
        assert strategy.on_prices(bars(5)) is None
    assert SANDBOX_UNAVAILABLE_ALARM in caplog.text
    assert strategy.state == StrategyState.RUNNING and faults == []


def test_a_fault_on_one_conid_stops_every_conid_of_the_deployment():
    sandbox, faults = FakeSandbox(), []
    strategy = live(sandbox, faults, "faults_above_limit", "FaultsAboveLimit", {"LIMIT": 104.5},
                    conids=(CONID, OTHER))
    strategy._dispatch_conid = OTHER
    assert strategy.on_prices(bars(3)) is None
    strategy._dispatch_conid = CONID
    assert strategy.on_prices(bars(10)) is None                          # close 105 > 104.5: a fault
    assert faults == [(strategy.digest, "SANDBOX_ERROR")] and sandbox.open_sessions == []
    strategy._dispatch_conid = OTHER
    assert strategy.on_prices(bars(4)) is None and len(sandbox.sessions) == 1


def test_a_fault_retires_through_the_callback_and_stops(caplog):
    sandbox, faults = FakeSandbox(), []
    strategy = live(sandbox, faults, "faults_above_limit", "FaultsAboveLimit", {"LIMIT": 104.5})
    frame = bars(10)
    assert strategy.on_prices(frame.iloc[:3]) is None
    with caplog.at_level(logging.ERROR):
        assert strategy.on_prices(frame.iloc[:6]) is None              # close 105 > 104.5
    assert faults == [(strategy.digest, SANDBOX_ERROR)]
    assert strategy.state == StrategyState.DISABLED and not strategy.authored_binding_intact()
    assert SANDBOX_FAULT_ALARM in caplog.text and sandbox.open_sessions == []
    sent = len(sandbox.sessions[0].stamps)
    assert strategy.on_prices(frame) is None                            # nothing more is sent, no SELL either
    assert len(sandbox.sessions) == 1 and len(sandbox.sessions[0].stamps) == sent


def test_a_stored_file_that_changed_retires_as_a_digest_fault():
    sandbox, faults = FakeSandbox(), []
    strategy = live(sandbox, faults)
    sandbox.tampered.add(strategy.digest)
    assert strategy.on_prices(bars(3)) is None
    assert faults == [(strategy.digest, SANDBOX_DIGEST_MISMATCH)]


def test_a_nan_newest_bar_gives_no_signal_and_keeps_the_session():
    sandbox, faults = FakeSandbox(), []
    strategy, frame = live(sandbox, faults), bars(21)
    frame.iloc[-1, frame.columns.get_loc("volume")] = np.nan
    strategy.on_prices(frame.iloc[:20])
    assert strategy.on_prices(frame) is None
    session = sandbox.sessions[0]
    assert len(sandbox.sessions) == 1 and not session.closed and len(session.stamps) == 20


def test_closing_the_sessions_frees_the_slots():
    sandbox, faults = FakeSandbox(), []
    strategy = live(sandbox, faults)
    strategy.on_prices(bars(3))
    strategy.close_sessions()
    assert sandbox.open_sessions == []


def test_the_binding_is_the_deployments_digest():
    sandbox, faults = FakeSandbox(), []
    strategy = live(sandbox, faults)
    assert strategy.authored_binding_intact()
    strategy.ai_source_digest = "sha256:" + "0" * 64
    assert not strategy.authored_binding_intact()
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -m pytest tests/strategy/test_authored_live.py -q`
Expected: FAIL (`ModuleNotFoundError: No module named 'trader.strategy.authored_runtime'`).

- [ ] **Step 3: Implement `trader/strategy/authored_runtime.py`**

```python
"""Authored strategies in the strategy service (SP3 spec 7.6, 7.9, 8). No authored code runs in this process.

``AuthoredLiveStrategy`` is the instance the runtime dispatches for one deployment: it holds one session on the live
sandbox socket for the New York trading day, opened with every conid the deployment binds, and streams each
completed bar of each conid to it (decision D1; spec 7.5 as amended). ``AuthoredDeployments`` builds
it only when the running sandbox image has a passing isolation proof and the deployment's digest is the file's,
and asks the trader to retire it after a sandbox fault. Positions are never touched here: they keep their stops.
"""
from __future__ import annotations

import datetime as dt
import hmac
import logging
import threading
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo

import pandas as pd

from trader.authored.api import Decision
from trader.authored.sandbox_client import SandboxFault, SandboxUnavailable
from trader.authored.streaming import BarStream, decision_signal, utc_time
from trader.trading.strategy import Signal, Strategy

NEW_YORK = ZoneInfo("America/New_York")
# The largest LOOKBACK_BARS an authored file may declare (spec 7.1): what a new session is warmed with.
WARMUP_BARS = 2000
SANDBOX_UNAVAILABLE_ALARM = "AUTHORED_SANDBOX_UNAVAILABLE"
SANDBOX_FAULT_ALARM = "AUTHORED_SANDBOX_FAULT"
ISOLATION_ALARM = "AUTHORED_ISOLATION_UNPROVEN"
DIGEST_ALARM = "AUTHORED_DIGEST_MISMATCH"


class _UnprovenBuild(Exception):
    def __init__(self, build: str):
        super().__init__(build)
        self.build = build


@dataclass
class _DaySession:
    """One live session for one New York day: one BarStream per conid of the deployment, all on one session."""
    session: Any
    streams: dict[int, BarStream]
    day: dt.date


def _unsent(prices: pd.DataFrame, last_sent: Optional[dt.datetime]) -> pd.DataFrame:
    """The bars a conid's stream has not had yet; its first bar in a session brings the trailing WARMUP_BARS
    (Plan 4 ruling 14)."""
    if last_sent is None:
        return prices.tail(WARMUP_BARS)
    index = pd.DatetimeIndex(prices.index)
    index = index.tz_localize("UTC") if index.tz is None else index.tz_convert("UTC")
    return prices[index > pd.Timestamp(last_sent)]


class AuthoredLiveStrategy(Strategy):
    def __init__(self, *, client: Any, digest: str, class_name: str, params: Mapping[str, float], bar_size: str,
                 conids: Sequence[int], on_fault: Callable[[str, str], None], proof_gate: Callable[[str], bool]):
        super().__init__()
        # Plan 4 ruling 19 (review r1 #6): True only for a build with a passing isolation proof. Asked on every
        # bar for the build the session's OPENED reported, so a first OPEN, a reconnect, a day rollover or a
        # proof withdrawn mid-session never sends a bar to an unproven build.
        self._proof_gate = proof_gate
        self._in_unproven = False
        self.digest = digest
        self.conids = tuple(int(conid) for conid in conids)
        self._client, self._class_name, self._params, self._bar_size = client, class_name, dict(params), bar_size
        self._on_fault = on_fault
        self._current: Optional[_DaySession] = None
        # The tick thread sends; the reconcile thread may close. One bar at a time on the session (Plan 2 ruling 25).
        self._lock = threading.Lock()
        self._faulted = False
        self._in_outage = False

    def on_prices(self, prices: pd.DataFrame) -> Optional[Signal]:
        conid = self.dispatch_conid
        if conid is None or prices is None or prices.empty:
            return None
        if conid not in self.conids:
            logging.error("%s: authored strategy %s was dispatched conid %s, which its deployment does not bind; "
                          "no signal", SANDBOX_FAULT_ALARM, self.name, conid)
            return None
        with self._lock:
            if self._faulted:
                return None
            try:
                decision = self._decide(conid, prices)
            except SandboxUnavailable as exc:
                self._end_session()
                self._raise_outage_alarm(exc)
                return None
            except _UnprovenBuild as unproven:
                self._end_session()
                self._raise_unproven_alarm(unproven.build)
                return None
            except SandboxFault as fault:
                self._fault(fault)
                return None
            self._clear_outage_alarm()
            self._in_unproven = False
        return decision_signal(decision, self.name)

    def authored_binding_intact(self) -> bool:
        """The dispatch check (Plan 4 ruling 20): never after a fault, and only for the digest the deployment bound."""
        return not self._faulted and hmac.compare_digest(self.digest, getattr(self, "ai_source_digest", ""))

    def close_sessions(self) -> None:
        with self._lock:
            self._end_session()

    def _decide(self, conid: int, prices: pd.DataFrame) -> Optional[Decision]:
        day = utc_time(prices.index[-1]).astimezone(NEW_YORK).date()
        if self._current is not None and day > self._current.day:      # a conid still on yesterday never rolls back
            self._end_session()
        if self._current is None:
            build = self._client.ping().image_digest          # no OPEN (no class load) on an unproven build
            if not self._proof_gate(build):
                raise _UnprovenBuild(build)
            session = self._client.open_session(self.digest, self._class_name, self._params, self._bar_size,
                                                list(self.conids))
            self._current = _DaySession(session, {c: BarStream(session, c) for c in self.conids}, day)
        # The OPENED build, checked on every bar: a restart between PING and OPEN, or a proof withdrawn
        # mid-session, sends nothing (fail closed).
        if not self._proof_gate(self._current.session.image_digest):
            raise _UnprovenBuild(self._current.session.image_digest)
        stream = self._current.streams[conid]
        decisions = stream.send_frame(_unsent(prices, stream.last_sent))
        return decisions[-1] if decisions else None

    def _end_session(self) -> None:
        current, self._current = self._current, None
        if current is not None:
            next(iter(current.streams.values())).close()                   # closes the session for every conid

    def _raise_unproven_alarm(self, build: str) -> None:
        if not self._in_unproven:
            self._in_unproven = True
            logging.error("%s: authored strategy %s: the live sandbox runs build %s, which has no passing isolation "
                          "proof; no signal until `mmr ai-author prove-isolation` passes for it", ISOLATION_ALARM,
                          self.name, build)

    def _raise_outage_alarm(self, exc: Exception) -> None:
        if not self._in_outage:
            self._in_outage = True
            logging.error("%s: authored strategy %s gets no decision from the live sandbox (%s); no signal, it "
                          "reconnects on the next bar and stays deployed", SANDBOX_UNAVAILABLE_ALARM, self.name, exc)

    def _clear_outage_alarm(self) -> None:
        if self._in_outage:
            self._in_outage = False
            logging.info("authored strategy %s: the live sandbox answers again", self.name)

    def _fault(self, fault: SandboxFault) -> None:
        self._faulted = True
        self._end_session()                                                # a fault ends the session for every conid
        self.disable()
        logging.error("%s: authored strategy %s (%s) failed in the sandbox (%s); it stops and is retired, open "
                      "positions keep their stops", SANDBOX_FAULT_ALARM, self.name, self.digest, fault.code)
        self._on_fault(self.digest, str(fault.code))
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/strategy/test_authored_live.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add trader/strategy/authored_runtime.py tests/strategy/test_authored_live.py
git commit -m "feat: run a deployed authored strategy through one live sandbox session per deployment and day

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 10: Loading authored deployments: gates, retirement and wiring

**Files:**
- Modify: `trader/strategy/authored_runtime.py` (add `AuthoredDeployments`, `proof_passes`), `trader/strategy/ai_deployment_source.py` (`source_unchanged`), `trader/strategy/strategy_runtime.py` (`connect`, `_load_ai_instance`, new `_build_ai_instance`, `_build_authored_instance`, `unload_strategy`, `_reconcile_sync`)
- Test: `tests/strategy/test_authored_deployments.py`

**Interfaces:**
- Consumes: Plan 3's widened `ActiveAiDeployment` (checked in Task 1); Task 9 `AuthoredLiveStrategy`; Plan 3 `is_authored_path`, `authored_digest_of_path`, `authored_strategy_path`, `get_isolation_proof` (`{image_digest}` → `{found, proof: {image_digest, passed, ...}}`), `retire_authored_strategy` (`{digest, code, detail}` → `{status: UPDATED|UNCHANGED|REFUSED, code, detail, retryable, record}`); Plan 2 `SandboxClient(LIVE_SOCKET)`, `ping().image_digest`.
- Produces: `AuthoredDeployments(*, client, trader_query, trader_command)` with `build(name, deployment, *, storage, universe_accessor) -> Optional[AuthoredLiveStrategy]`, `is_proven(build) -> bool` (the live proxies' `proof_gate`; reads the trader fresh every call, no cache — review r2 #4), `request_retirement(digest, code)`, `send_retirements()`; `proof_passes(reply, image_digest) -> bool`; `source_unchanged(instance)` asks an authored instance; `StrategyRuntime._authored_deployments`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/strategy/test_authored_deployments.py
"""SP3 spec 7.6, 7.9 and 8 (strategy side): loading an authored deployment, its gates, its retirement.
Plan 4 rulings 18-20."""
import logging
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from tests.authored.fake_sandbox import IMAGE_DIGEST, FakeSandbox, caller_source
from tests.test_strategy_artifact_soft_load import _make_runtime, _write_strategy
from trader.data.backtest_store import compute_strategy_hash
from trader.data.duckdb_store import DuckDBConnection
from trader.data.strategy_signal_record import StrategySignalRecord
from trader.messaging.ai_deployment_wire import ActiveAiDeployment
from trader.messaging.typed_rpc import TypedRpcRemoteError
from trader.research.strategy_key import authored_strategy_path
from trader.strategy.ai_deployment_source import AiDeploymentSource, ai_instance_name, source_unchanged
from trader.strategy.authored_runtime import ISOLATION_ALARM, AuthoredDeployments, AuthoredLiveStrategy, proof_passes
from trader.trading.strategy import StrategyState

VERSION, CATALOGUE_VERSION = "sha256:" + "d" * 64, "sha256:" + "f" * 64
BASE = "sha256:" + "b" * 64
CONID = 265598
RETIRED = {"status": "UPDATED", "code": None, "detail": None, "retryable": False, "record": None}


class FakeTraderRpc:
    """A typed RPC client double: replies per method, in order (the last repeats); an exception is raised."""

    def __init__(self, **replies):
        self.calls, self._replies = [], {method: list(values) for method, values in replies.items()}

    def answer(self, method, *replies):
        self._replies[method] = list(replies)

    def call(self, method, body, response_model, timeout=None):
        self.calls.append((method, dict(body)))
        queue = self._replies[method]
        reply = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(reply, Exception):
            raise reply
        return reply


def proof(image=IMAGE_DIGEST, passed=True):
    return {"found": True, "proof": {"proof_id": "sha256:" + "9" * 64, "image_digest": image, "passed": passed,
                                     "checks": [], "ran_at": "2026-10-12T01:00:00+00:00",
                                     "recorded_at": "2026-10-12T01:00:01+00:00"}}


def bars(count):
    index = pd.date_range("2024-03-04 14:30", periods=count, freq="1min", tz="UTC", name="date")
    closes = [100.0 + i for i in range(count)]
    return pd.DataFrame({"open": closes, "high": closes, "low": closes, "close": closes, "volume": 1000.0},
                        index=index)


@pytest.fixture
def world(tmp_path, tmp_duckdb_path):
    rt = _make_runtime(tmp_path, tmp_duckdb_path, automation_enabled=False)
    rt._load_enabled = lambda name: None
    rt._last_dispatched_bar = {}
    rt._warmup_shortfalls = {}
    rt._hist_bar_days = {}
    rt.signal_record = StrategySignalRecord(DuckDBConnection.get_instance(tmp_duckdb_path))
    rt.event_store = SimpleNamespace(append=lambda event: None)
    rt.zmq_messagebus_client = SimpleNamespace(write=lambda *args: None)
    rt._load_ai_history = lambda instance: None                     # no IB in a node test
    sandbox = FakeSandbox()
    query = FakeTraderRpc(get_isolation_proof=[proof()])
    command = FakeTraderRpc(retire_authored_strategy=[RETIRED])
    rt._authored_deployments = AuthoredDeployments(client=sandbox, trader_query=query, trader_command=command)
    return SimpleNamespace(rt=rt, sandbox=sandbox, query=query, command=command)


def authored_active(sandbox, name="counts_bars", class_name="CountsBars", *, params=None, digest=None):
    file_digest = sandbox.add(caller_source(name))
    return ActiveAiDeployment(version_digest=VERSION, base_digest=BASE,
                              strategy_path=authored_strategy_path(file_digest),
                              strategy_digest=digest or file_digest, class_name=class_name, params=params or {},
                              conids=[CONID], bar_size="1 min", expiry_session="2026-11-06")


def reconcile(world, deployments):
    AiDeploymentSource(runtime=world.rt, read_active=lambda: deployments, paper=True).reconcile()


def instance_of(world, version=VERSION):
    return world.rt.get_strategy(ai_instance_name(version))


def retirements(world):
    return [body for method, body in world.command.calls if method == "retire_authored_strategy"]


def test_an_authored_deployment_loads_without_any_file(world):
    reconcile(world, [authored_active(world.sandbox)])
    instance = instance_of(world)
    assert isinstance(instance, AuthoredLiveStrategy) and instance.state == StrategyState.RUNNING
    assert instance.ai_deployment_version == VERSION and instance.paper_only
    assert not any(Path(world.rt.strategies_directory).iterdir())
    assert world.query.calls == [("get_isolation_proof", {"image_digest": IMAGE_DIGEST})]


def test_its_signals_take_the_existing_signal_path(world):
    deployment = authored_active(world.sandbox)
    reconcile(world, [deployment])
    instance = instance_of(world)
    instance._dispatch_conid = CONID
    frame = bars(3)
    world.rt._dispatch_once(instance, instance.on_prices(frame), CONID, frame)
    entries = world.rt.signal_record.read(0, 10).signals
    assert [(e.action, e.source_digest, e.deployment_version) for e in entries] == [
        ("BUY", deployment.strategy_digest, VERSION)]


def test_a_digest_that_is_not_the_files_is_refused_and_retired(world, caplog):
    file_digest = authored_active(world.sandbox).strategy_digest
    with caplog.at_level(logging.ERROR):
        reconcile(world, [authored_active(world.sandbox, digest="sha256:" + "0" * 64)])
    assert world.rt.ai_instances() == {} and "AUTHORED_DIGEST_MISMATCH" in caplog.text
    world.rt._authored_deployments.send_retirements()
    assert [(body["digest"], body["code"]) for body in retirements(world)] == [
        (file_digest, "SANDBOX_DIGEST_MISMATCH")]


@pytest.mark.parametrize("reply", [proof(passed=False), proof(image="sha256:" + "f" * 64),
                                   {"found": False, "proof": None}])
def test_no_passing_proof_for_the_running_image_loads_nothing_and_retires_nothing(world, reply, caplog):
    world.query.answer("get_isolation_proof", reply)
    with caplog.at_level(logging.ERROR):
        reconcile(world, [authored_active(world.sandbox)])
    assert world.rt.ai_instances() == {} and ISOLATION_ALARM in caplog.text
    world.rt._authored_deployments.send_retirements()
    assert retirements(world) == []


def test_a_sandbox_rebuilt_after_the_load_gives_no_signal_until_its_build_is_proven(world):   # review r1 #6
    reconcile(world, [authored_active(world.sandbox)])
    instance = instance_of(world)
    instance._dispatch_conid = CONID
    world.sandbox.image_digest = "e4f5a6b-20261012T090000Z"              # build B replaced the proven build
    world.query.answer("get_isolation_proof", {"found": False, "proof": None})   # B has no proof yet
    frame = bars(3)
    assert instance.on_prices(frame) is None and world.sandbox.sessions == []
    assert world.rt.signal_record.read(0, 10).signals == []
    world.query.answer("get_isolation_proof", proof(image="e4f5a6b-20261012T090000Z"))
    signal = instance.on_prices(frame)                                   # read fresh: no reconcile needed
    world.rt._dispatch_once(instance, signal, CONID, frame)
    assert len(world.rt.signal_record.read(0, 10).signals) == 1


def test_a_failed_proof_for_the_same_build_before_first_open_gives_no_signal(world):      # review r2 #4
    reconcile(world, [authored_active(world.sandbox)])                   # build A proven, no session opened yet
    instance = instance_of(world)
    instance._dispatch_conid = CONID
    world.query.answer("get_isolation_proof", proof(passed=False))       # A's proof withdrawn; no reconcile
    assert instance.on_prices(bars(3)) is None                          # the gate re-reads, so no first OPEN
    assert world.sandbox.sessions == [] and world.rt.signal_record.read(0, 10).signals == []


def test_a_later_failing_proof_stops_the_running_instance_at_the_next_bar(world):         # review r2 #4
    reconcile(world, [authored_active(world.sandbox)])
    instance = instance_of(world)
    instance._dispatch_conid = CONID
    assert instance.on_prices(bars(3)) is not None                      # opened on the proven build
    world.query.answer("get_isolation_proof", proof(passed=False))       # the same build's proof is withdrawn
    assert instance.on_prices(bars(4)) is None and world.sandbox.open_sessions == []   # next bar re-reads, stops


def test_a_trader_that_refuses_the_proof_read_loads_nothing(world, caplog):
    world.query.answer("get_isolation_proof", TypedRpcRemoteError("PERMISSION_DENIED", "cli only"))
    with caplog.at_level(logging.ERROR):
        reconcile(world, [authored_active(world.sandbox)])
    assert world.rt.ai_instances() == {} and "PERMISSION_DENIED" in caplog.text


def test_an_unreachable_live_sandbox_waits_for_the_next_reconcile(world):
    world.sandbox.unavailable = True
    reconcile(world, [authored_active(world.sandbox)])
    assert world.rt.ai_instances() == {}
    world.sandbox.unavailable = False
    reconcile(world, [authored_active(world.sandbox)])
    assert isinstance(instance_of(world), AuthoredLiveStrategy)


def test_a_fault_retires_once_and_the_file_never_reloads(world):
    deployment = authored_active(world.sandbox, "faults_above_limit", "FaultsAboveLimit", params={"LIMIT": 104.5})
    reconcile(world, [deployment])
    instance = instance_of(world)
    instance._dispatch_conid = CONID
    assert instance.on_prices(bars(10)) is None                  # the warm-up reaches close 105: a fault
    assert retirements(world) == []                              # nothing is sent on the tick thread
    assert not source_unchanged(instance)
    reconcile(world, [deployment])
    world.rt._authored_deployments.send_retirements()
    reconcile(world, [deployment])
    world.rt._authored_deployments.send_retirements()
    assert world.rt.ai_instances() == {}
    assert [(body["digest"], body["code"]) for body in retirements(world)] == [
        (deployment.strategy_digest, "SANDBOX_ERROR")]


def test_a_lost_retirement_is_sent_again_next_reconcile(world):
    world.command.answer("retire_authored_strategy", ConnectionError("trader down"), RETIRED)
    world.rt._authored_deployments.request_retirement("sha256:" + "a" * 64, "SANDBOX_ERROR")
    for _ in range(3):
        world.rt._authored_deployments.send_retirements()
    assert len(retirements(world)) == 2


def test_a_final_refusal_stops_the_resend(world, caplog):
    world.command.answer("retire_authored_strategy", {"status": "REFUSED", "code": "AUTHORED_STATE_CONFLICT",
                                                      "detail": "not deployed", "retryable": False, "record": None})
    world.rt._authored_deployments.request_retirement("sha256:" + "a" * 64, "SANDBOX_ERROR")
    with caplog.at_level(logging.ERROR):
        world.rt._authored_deployments.send_retirements()
        world.rt._authored_deployments.send_retirements()
    assert len(retirements(world)) == 1 and "AUTHORED_STATE_CONFLICT" in caplog.text


def test_reconcile_sends_pending_retirements(world):
    world.rt._config_mtime = None
    world.rt._authored_deployments.request_retirement("sha256:" + "a" * 64, "SANDBOX_ERROR")
    world.rt._reconcile_sync()
    assert len(retirements(world)) == 1


def test_is_proven_reads_the_trader_every_call_and_never_caches(world):                   # review r2 #4
    deployments = world.rt._authored_deployments
    world.query.answer("get_isolation_proof", proof(), proof(passed=False))   # pass once, then fail (repeats)
    assert deployments.is_proven(IMAGE_DIGEST) is True
    assert deployments.is_proven(IMAGE_DIGEST) is False                  # a second call re-reads, not cached
    reads = [body for method, body in world.query.calls if method == "get_isolation_proof"]
    assert reads[-2:] == [{"image_digest": IMAGE_DIGEST}, {"image_digest": IMAGE_DIGEST}]


def test_unloading_closes_its_sessions(world):
    reconcile(world, [authored_active(world.sandbox)])
    instance = instance_of(world)
    instance._dispatch_conid = CONID
    instance.on_prices(bars(3))
    world.rt.unload_strategy(instance.name)
    assert world.sandbox.open_sessions == []


def test_a_catalogue_and_an_authored_deployment_load_side_by_side(world):
    path = _write_strategy(world.rt.strategies_directory)
    catalogue = ActiveAiDeployment(version_digest=CATALOGUE_VERSION, base_digest=BASE,
                                   strategy_path="strategies/" + Path(path).name,
                                   strategy_digest="sha256:" + compute_strategy_hash(path),
                                   class_name="VwapReclaimCat", params={}, conids=[CONID], bar_size="1 min",
                                   expiry_session="2026-11-06")
    reconcile(world, [catalogue, authored_active(world.sandbox)])
    assert set(world.rt.ai_instances()) == {ai_instance_name(CATALOGUE_VERSION), ai_instance_name(VERSION)}
    assert source_unchanged(instance_of(world, CATALOGUE_VERSION))


def test_proof_passes_needs_found_passed_and_the_same_image():
    assert proof_passes(proof(), IMAGE_DIGEST)
    assert not proof_passes({"found": True, "proof": {**proof()["proof"], "passed": "yes"}}, IMAGE_DIGEST)
    assert not proof_passes({"found": True, "proof": None}, IMAGE_DIGEST)
    assert not proof_passes({}, IMAGE_DIGEST)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -m pytest tests/strategy/test_authored_deployments.py -q`
Expected: FAIL (`ImportError: cannot import name 'AuthoredDeployments'`).

- [ ] **Step 3: Implement `AuthoredDeployments`**

In `trader/strategy/authored_runtime.py`, `from trader.trading.strategy import Signal, Strategy` becomes `from trader.trading.strategy import Signal, Strategy, StrategyContext`, and these imports join the others:

```python
from trader.messaging.typed_rpc import TypedRpcError, TypedRpcRemoteError
from trader.objects import BarSize
from trader.research.strategy_key import authored_digest_of_path
from trader.strategy.ai_deployment_source import AI_HISTORY_DAYS
```

At the end of the module:

```python
TRADER_TIMEOUT_SECONDS = 15.0
TRANSPORT_ERRORS = (ConnectionError, TimeoutError, OSError, TypedRpcError)
RETIRE_DETAILS = {
    "SANDBOX_ERROR": "the live session failed in the sandbox",
    "SANDBOX_DIGEST_MISMATCH": "the deployment or the stored file is not the authored digest",
}


def proof_passes(reply: Mapping[str, Any], image_digest: str) -> bool:
    """Plan 3's get_isolation_proof reply; its shape wins and this is the one place that reads it."""
    proof = reply.get("proof")
    return (reply.get("found") is True and isinstance(proof, Mapping) and proof.get("passed") is True
            and proof.get("image_digest") == image_digest)


class AuthoredDeployments:
    def __init__(self, *, client: Any, trader_query: Any, trader_command: Any):
        self._client, self._query, self._command = client, trader_query, trader_command
        self._lock = threading.Lock()
        self._retiring: dict[str, str] = {}           # digest -> fault code, until the trader answers
        self._retired: set[str] = set()
        self._unproven_build: Optional[str] = None    # the build the last is_proven call found unproven (alarm once)

    def build(self, name: str, deployment: Any, *, storage: Any,
              universe_accessor: Any) -> Optional[AuthoredLiveStrategy]:
        """The instance for one active authored deployment, or None (logged) when a gate refuses it."""
        digest = authored_digest_of_path(deployment.strategy_path)
        if self._stopped(digest):
            logging.info("authored deployment %s is not loaded: %s was retired in this process", name, digest)
            return None
        if not hmac.compare_digest(digest, deployment.strategy_digest):
            logging.error("%s: deployment %s binds %s to %s; not loaded, the file is retired", DIGEST_ALARM,
                          deployment.version_digest, deployment.strategy_digest, deployment.strategy_path)
            self.request_retirement(digest, "SANDBOX_DIGEST_MISMATCH")
            return None
        try:
            build = self._client.ping().image_digest
        except SandboxUnavailable as exc:
            logging.error("%s: the live sandbox does not answer (%s); authored deployment %s waits",
                          SANDBOX_UNAVAILABLE_ALARM, exc, name)
            return None
        if not self.is_proven(build):                 # a fresh trader read; no cache (review r2 #4)
            return None
        instance = AuthoredLiveStrategy(client=self._client, digest=digest, class_name=deployment.class_name,
                                        params=deployment.params, bar_size=deployment.bar_size,
                                        conids=deployment.conids, on_fault=self.request_retirement,
                                        proof_gate=self.is_proven)
        instance.install(StrategyContext(
            name=name, bar_size=BarSize.parse_str(deployment.bar_size), conids=list(deployment.conids),
            universe=None, historical_days_prior=AI_HISTORY_DAYS, paper_only=True, storage=storage,
            universe_accessor=universe_accessor, logger=logging, module=deployment.strategy_path,
            class_name=deployment.class_name, description=f"authored AI deployment {deployment.version_digest}",
            params=dict(deployment.params)))
        instance.ai_deployment_version = deployment.version_digest
        instance.ai_deployment_digest = deployment.base_digest
        instance.ai_source_digest = deployment.strategy_digest
        instance.ai_source_path = deployment.strategy_path
        instance.enable()
        return instance

    def request_retirement(self, digest: str, code: str) -> None:
        """Called on the tick thread: record only; the reconcile thread sends (Plan 4 ruling 18)."""
        with self._lock:
            if digest not in self._retired:
                self._retiring.setdefault(digest, code)

    def send_retirements(self) -> None:
        with self._lock:
            pending = list(self._retiring.items())
        for digest, code in pending:
            if self._send_retirement(digest, code):
                with self._lock:
                    self._retiring.pop(digest, None)
                    self._retired.add(digest)

    def _stopped(self, digest: str) -> bool:
        with self._lock:
            return digest in self._retiring or digest in self._retired

    def _send_retirement(self, digest: str, code: str) -> bool:
        """Plan 3's retire_authored_strategy; true when no resend is due."""
        body = {"digest": digest, "code": code, "detail": RETIRE_DETAILS[code]}
        try:
            reply = self._command.call("retire_authored_strategy", body, dict, timeout=TRADER_TIMEOUT_SECONDS)
        except TypedRpcRemoteError as remote:
            logging.error("%s: retire_authored_strategy for %s failed (%s: %s); sent again next reconcile",
                          SANDBOX_FAULT_ALARM, digest, remote.code, remote.message)
            return False
        except TRANSPORT_ERRORS as exc:
            logging.warning("retire_authored_strategy for %s not delivered (%s); sent again next reconcile",
                            digest, type(exc).__name__)
            return False
        if reply.get("status") in ("UPDATED", "UNCHANGED"):
            logging.info("authored strategy %s is retired at the trader (%s)", digest, reply.get("status"))
            return True
        if reply.get("retryable"):
            logging.warning("the trader asks to resend the retirement of %s (%s)", digest, reply.get("code"))
            return False
        logging.error("%s: the trader refused to retire %s (%s: %s); not sent again", SANDBOX_FAULT_ALARM,
                      digest, reply.get("code"), reply.get("detail"))
        return True

    def is_proven(self, build: Optional[str]) -> bool:
        """Review r2 #4: the authoritative latest proof for ``build``, read from the trader on every call (no
        cache), so a proof withdrawn for a build already running stops the next OPEN and the next bar. A read
        failure or a refusal is fail-closed (False). The gate the live proxy holds is this method; it is called
        before every new session and on every bar for the build the session's OPENED reports."""
        if build is None:
            return False
        try:
            reply = self._query.call("get_isolation_proof", {"image_digest": build}, dict,
                                     timeout=TRADER_TIMEOUT_SECONDS)
        except TypedRpcRemoteError as remote:
            logging.error("%s: get_isolation_proof refused (%s: %s); authored deployments give no signal",
                          ISOLATION_ALARM, remote.code, remote.message)
            return False
        except TRANSPORT_ERRORS as exc:
            logging.warning("isolation proof not readable (%s); authored deployments give no signal this bar",
                            type(exc).__name__)
            return False
        passed = proof_passes(reply, build)
        if not passed:
            if self._unproven_build != build:
                self._unproven_build = build
                logging.error("%s: no passing isolation proof for sandbox build %s; run `mmr ai-author "
                              "prove-isolation`", ISOLATION_ALARM, build)
        else:
            self._unproven_build = None
        return passed
```

- [ ] **Step 4: Ask the instance in `source_unchanged`**

In `trader/strategy/ai_deployment_source.py`:

```python
def source_unchanged(instance: Any) -> bool:
    """True while the instance still runs the judged bytes. An authored instance answers itself (its file is in
    the sandbox's store, not here); any other re-hashes its file on disk."""
    authored_binding_intact = getattr(instance, "authored_binding_intact", None)
    if authored_binding_intact is not None:
        return authored_binding_intact()
    current = compute_strategy_hash(getattr(instance, "ai_source_path", ""))
    return bool(current) and hmac.compare_digest("sha256:" + current, instance.ai_source_digest)
```

- [ ] **Step 5: Wire the runtime**

In `trader/strategy/strategy_runtime.py` add imports:

```python
from trader.authored.sandbox_client import SandboxClient
from trader.research.strategy_key import is_authored_path
from trader.sandbox.protocol import LIVE_SOCKET
from trader.strategy.authored_runtime import AuthoredDeployments, AuthoredLiveStrategy
```

In `connect()`, right after `self._ai_deployment_source = AiDeploymentSource(...)`:

```python
            self._authored_deployments = AuthoredDeployments(
                client=SandboxClient(LIVE_SOCKET), trader_query=self._trader_query_client,
                trader_command=self._trader_command_client)
```

Replace `_load_ai_instance` and add two methods:

```python
    def _load_ai_instance(self, name: str, deployment) -> bool:
        instance = self._build_ai_instance(name, deployment)
        if instance is None:
            return False
        try:
            self._load_ai_history(instance)
        except Exception as ex:
            logging.error('AI_HISTORY_BACKFILL_FAILED: %s (conids %s, %s): %s; not loaded, the next reconcile '
                          'retries', name, instance.conids, instance.bar_size, ex)
            sys.modules.pop(f'_mmr_strategy_{name}', None)
            return False
        self.strategy_implementations.append(instance)
        return True

    def _build_ai_instance(self, name: str, deployment) -> Optional[Strategy]:
        if is_authored_path(deployment.strategy_path):
            return self._build_authored_instance(name, deployment)
        return self.load_strategy(
            name=name, bar_size_str=deployment.bar_size, conids=list(deployment.conids), universe=None,
            historical_days_prior=AI_HISTORY_DAYS, module=deployment.strategy_path.removeprefix('strategies/'),
            class_name=deployment.class_name, description=f'AI deployment {deployment.version_digest}',
            paper_only=True, auto_execute=False, params=dict(deployment.params),
            ai_binding=AiInstanceBinding(deployment.version_digest, deployment.base_digest,
                                         deployment.strategy_digest))

    def _build_authored_instance(self, name: str, deployment) -> Optional[Strategy]:
        """No file is read or run here: the authored code stays in the sandbox (SP3 spec 7.6)."""
        instance = self._authored_deployments.build(name, deployment, storage=self.storage,
                                                    universe_accessor=self.universe_accessor)
        if instance is None:
            return None
        instance.history_depth = self._history_depth_for(instance, AI_HISTORY_DAYS)
        if self._load_enabled(name) is False:
            instance.disable()
        return instance
```

In `unload_strategy`, after the `with self._signal_hold.no_dispatch():` block:

```python
        if isinstance(instance, AuthoredLiveStrategy):
            instance.close_sessions()
```

In `_reconcile_sync`, right after the `source.reconcile()` block:

```python
        authored = getattr(self, '_authored_deployments', None)
        if authored is not None:
            try:
                authored.send_retirements()      # the proof is read fresh at each OPEN and bar, not cached
            except Exception as ex:
                logging.warning('authored retirements not sent (will retry next cycle): %s', ex)
```

- [ ] **Step 6: Run the tests and the SP2c strategy tests**

Run: `.venv/bin/python -m pytest tests/strategy/test_authored_deployments.py tests/strategy/test_authored_live.py tests/test_strategy_ai_deployments.py tests/test_strategy_artifact_soft_load.py tests/test_strategy_runtime.py -q`
Expected: PASS. If `test_reconcile_sends_pending_retirements` fails on a runtime attribute the test runtime lacks, set it in that test the way `tests/test_strategy_runtime.py` builds its runtime; do not change `_reconcile_sync` for the test.

- [ ] **Step 7: Commit**

```bash
git add trader/strategy/authored_runtime.py trader/strategy/ai_deployment_source.py \
        trader/strategy/strategy_runtime.py tests/strategy/test_authored_deployments.py
git commit -m "feat: load authored deployments behind the isolation and digest gates and retire them on a fault

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 11: ORB equivalence through both proxies and Plan 2's child runtime

**Files:**
- Test: `tests/authored/test_orb_equivalence.py`

**Interfaces:**
- Consumes: Plan 1's ORB fixture `tests/authored/fixtures/orb_authored.py` (`OpeningRangeBreakout`, `MIN_BARS = 40`; decision D8) through Task 2 `orb_fixture_source`; Task 2 `FakeSandbox`; Task 3 `AuthoredProxyStrategy`, `RESEARCH_SESSION_CONID`; Task 9 `AuthoredLiveStrategy`; the catalogue `strategies/opening_range_breakout.py`; Plan 2 `Supervisor`, `LIVE_POOL_SIZE`, `RESEARCH_POOL_SIZE`, `InProcessSpawner`, `parse_request`, `SANDBOX_BUSY`, `SANDBOX_UNAVAILABLE`, `SANDBOX_ERROR`.
- Produces: nothing new (spec 10 *equivalence* and *lookahead by construction*).

The port sends no `close_by_time` and no risk (ruling 15), so trades differ from the catalogue's; the claim is the signal (action and probability) on every bar. The third test runs the port in Plan 2's real child runtime (static check, import guard, reduced built-ins) through the supervisor with its in-process spawner (Plan 2 ruling 12).

- [ ] **Step 1: Write the test**

```python
# tests/authored/test_orb_equivalence.py
"""SP3 spec 10 (equivalence): ORB ported to AuthoredStrategy gives the same signals bar for bar through the
sandbox as the catalogue ORB, in a backtest (research proxy), live (strategy proxy) and in Plan 2's child runtime."""
import itertools
import logging

import exchange_calendars as xcals
import numpy as np
import pandas as pd

from strategies.opening_range_breakout import OpeningRangeBreakout
from tests.authored.fake_sandbox import IMAGE_DIGEST, FakeSandbox, orb_fixture_source
from tests.sandbox.fakes import InProcessSpawner
from trader.authored.api import Decision
from trader.authored.keys import source_digest
from trader.authored.sandbox_client import SandboxFault, SandboxUnavailable
from trader.objects import BarSize
from trader.research.authored_backtest import AuthoredProxyStrategy
from trader.sandbox import protocol
from trader.sandbox.supervisor import LIVE_POOL_SIZE, RESEARCH_POOL_SIZE, Supervisor
from trader.strategy.authored_runtime import AuthoredLiveStrategy
from trader.trading.strategy import StrategyContext

DAYS = ("2024-03-04", "2024-03-05", "2024-03-06")
CONID = 265598
# Callers always send the full point (SP2c); Plan 2's child passes OPEN params only.
ORB_PARAMS = {"RANGE_MINUTES": 30, "VOLUME_MULT": 1.5, "RTH_OPEN_MIN": 570, "RTH_CLOSE_MIN": 960,
              "VOLUME_SMA_BARS": 20, "MIN_BARS": 40}
PORT_CLASS = "OpeningRangeBreakout"          # Plan 1's fixture class; the catalogue class has the same name


def orb_session_bars(seed=7):
    """Three regular sessions of 1-minute bars with a loud breakout up and a slide down each day."""
    calendar = xcals.get_calendar("XNYS")
    rng = np.random.default_rng(seed)
    frames, price = [], 100.0
    for day in DAYS:
        index = pd.date_range(calendar.session_open(day), calendar.session_close(day) - pd.Timedelta(minutes=1),
                              freq="1min", tz="UTC")
        steps = rng.normal(0.0, 0.08, len(index))
        steps[45:60] += 0.12
        steps[200:230] -= 0.15
        closes = price + np.cumsum(steps)
        price = float(closes[-1])
        volume = rng.uniform(800, 1200, len(index))
        volume[45:60] *= 3.0
        volume[200:230] *= 3.0
        frames.append(pd.DataFrame({"open": closes - 0.02, "high": closes + 0.05, "low": closes - 0.05,
                                    "close": closes, "volume": volume}, index=index))
    frame = pd.concat(frames)
    frame.index.name = "date"
    return frame


def signal_key(signal):
    return None if signal is None else (str(signal.action), signal.probability)


def catalogue_signals(frame):
    orb = OpeningRangeBreakout()
    state = orb.precompute(frame)
    return [signal_key(orb.on_bar(frame.iloc[:i + 1], state, i)) for i in range(len(frame))]


def research_signals(client, digest, frame):
    proxy = AuthoredProxyStrategy(client=client, digest=digest, class_name=PORT_CLASS, params=ORB_PARAMS,
                                  bar_size="1 min")
    state = proxy.precompute(frame)
    return [signal_key(proxy.on_bar(frame.iloc[:i + 1], state, i)) for i in range(len(frame))]


# Plan 2 ruling 7: one session per connection. Each session gets its own connection id, as the server gives it.
_connection_ids = itertools.count(1)


class SupervisorSession:
    """Plan 2's supervisor called in this process; maps replies as Plan 2's SandboxClient does."""

    def __init__(self, supervisor, conn_id, session_id):
        self._supervisor, self._conn_id, self._id = supervisor, conn_id, session_id

    def bar(self, conid, t, open, high, low, close, volume):
        reply = _call(self._supervisor, self._conn_id,
                      {"type": "BAR", "session": self._id, "conid": conid, "t": t.isoformat(),
                       "open": open, "high": high, "low": low, "close": close, "volume": volume})
        if reply["type"] != "SIGNAL":
            _raise_for(reply)
        return None if reply["action"] is None else Decision(action=reply["action"],
                                                             probability=float(reply["probability"]))

    def close(self):
        _call(self._supervisor, self._conn_id, {"type": "CLOSE", "session": self._id})


class SupervisorClient:
    def __init__(self, supervisor):
        self._supervisor = supervisor

    def open_session(self, digest, class_name, params, bar_size, conids):
        conn_id = next(_connection_ids)
        reply = _call(self._supervisor, conn_id, {"type": "OPEN", "digest": digest, "class": class_name,
                                                  "params": dict(params), "bar_size": bar_size,
                                                  "conids": list(conids)})
        if reply["type"] != "OPENED":
            _raise_for(reply)
        return SupervisorSession(self._supervisor, conn_id, reply["session"])


def _call(supervisor, conn_id, request):
    return supervisor.handle("research", conn_id, protocol.parse_request({"v": 1, **request}))


def _raise_for(reply):
    # Availability, as the client maps it (Plan 2 ruling 34): BUSY and UNAVAILABLE are never the strategy's fault.
    if reply.get("code") in (protocol.SANDBOX_BUSY, protocol.SANDBOX_UNAVAILABLE):
        raise SandboxUnavailable(reply.get("detail", ""))
    raise SandboxFault(reply.get("detail", ""), code=reply.get("code", protocol.SANDBOX_ERROR))


def plan_2_sandbox(store_dir, source):
    """If Plan 2's Supervisor constructor or parse_request differ on the base, adapt only this and _call."""
    digest = source_digest(source.encode())
    (store_dir / f"{digest.split(':')[1]}.py").write_text(source)
    supervisor = Supervisor(spawners={"live": InProcessSpawner(), "research": InProcessSpawner()},
                            pool_sizes={"live": LIVE_POOL_SIZE, "research": RESEARCH_POOL_SIZE},
                            store_dir=str(store_dir),
                            image_digest=IMAGE_DIGEST, posture={},
                            wiper=lambda slot: True)   # the real wiper forks a helper against /tmp/s2000X
    return SupervisorClient(supervisor), digest


def test_the_research_proxy_matches_the_catalogue_orb_bar_for_bar():
    frame, sandbox = orb_session_bars(), FakeSandbox()
    expected = catalogue_signals(frame)
    assert {s[0] for s in expected if s} == {"BUY", "SELL"}        # not a vacuous comparison
    assert research_signals(sandbox, sandbox.add(orb_fixture_source()), frame) == expected
    assert sandbox.most_open == 1


def test_the_live_proxy_matches_the_catalogue_orb_bar_for_bar():
    frame, sandbox = orb_session_bars(), FakeSandbox()
    live = AuthoredLiveStrategy(client=sandbox, digest=sandbox.add(orb_fixture_source()), class_name=PORT_CLASS,
                                params=ORB_PARAMS, bar_size="1 min", conids=[CONID],
                                on_fault=lambda digest, code: None, proof_gate=lambda build: True)
    live.install(StrategyContext(name="aidv-orb", bar_size=BarSize.Mins1, conids=[CONID], universe=None,
                                 historical_days_prior=5, paper_only=True, storage=None, universe_accessor=None,
                                 logger=logging))
    live.enable()
    live._dispatch_conid = CONID
    ported = [signal_key(live.on_prices(frame.iloc[:i + 1])) for i in range(len(frame))]
    assert ported == catalogue_signals(frame)
    assert len(sandbox.sessions) == len(DAYS)                       # one session per deployment per New York day


def test_plan_2s_child_runtime_gives_the_catalogue_signals(tmp_path):
    frame = orb_session_bars()
    client, digest = plan_2_sandbox(tmp_path, orb_fixture_source())
    assert research_signals(client, digest, frame) == catalogue_signals(frame)
```

- [ ] **Step 2: Run it**

Run: `.venv/bin/python -m pytest tests/authored/test_orb_equivalence.py -q`
Expected: PASS. Plan 1's port (with `MIN_BARS`) was checked against the catalogue ORB on these bars at integration (1170 bars, one BUY and three SELLs, equal on every bar; Plan 1 ruling 10). A failure in the first two tests is a real difference between the callers or the port; a failure only in the third is a difference between the fake and Plan 2's child runtime (history columns, index time zone, params): find it before changing anything.

- [ ] **Step 3: Commit**

```bash
git add tests/authored/test_orb_equivalence.py
git commit -m "test: ported orb gives the catalogue orb's signals through every sandbox caller

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 12: The authored evaluation length (owner ruling: 302 sessions)

The owner confirmed on 2026-10-11 (Plan 1 ruling 1, spec section 14): an authored strategy is evaluated on the most recent 302 sessions; a catalogue strategy keeps the full SP2c length. SP2c's evaluation defaults live in the `research_service:` block of `trader.yaml` (`trader/research/service_config.py`, SP2c Plan 3 ruling 3), so the owner value goes there. The proportions stay SP2c's: the same folds and embargo, and the holdout scaled by `authored_evaluation_sessions / period_sessions` (690 → 302 gives a 39-session holdout; 263 pre-holdout sessions, 37 per segment).

**Files:**
- Modify: `trader/research/service_config.py` (`ResearchServiceConfig.authored_evaluation_sessions`, `authored_walk_forward`, the load check), `trader/research/cohort.py` (`build_cohort_spec`), `config_defaults/trader.yaml`, `tests/research/authored_fixtures.py` (`research_world(..., config=CONFIG)`)
- Test: `tests/research/test_authored_evaluation_length.py`

**Interfaces:**
- Consumes: Task 4 `build_cohort_spec` authored branch and `tests/research/authored_fixtures.py`; SP2c `evaluation_period`, `generate_walk_forward`, `_require_buildable_walk_forward`.
- Produces: `ResearchServiceConfig.authored_evaluation_sessions: int = 302` (1..`period_sessions`); `authored_walk_forward(config) -> ResearchServiceConfig` (the same config with `period_sessions = authored_evaluation_sessions` and `holdout_sessions = max(1, round(holdout_sessions × authored / period))`); an authored `CohortSpec.base` whose period and holdout come from it.

- [ ] **Step 1: Pin a valid authored length in the fixture and let it take a config** (review r1: the 302 default would not fit the fixture's 38-session period). In `tests/research/authored_fixtures.py` change `CONFIG` to

```python
CONFIG = ResearchServiceConfig(period_sessions=38, folds=2, embargo_sessions=1, holdout_sessions=5,
                               authored_evaluation_sessions=38)   # equal to the period: Tasks 3-8 keep their windows
```

change the signature to `def research_world(tmp_path, tmp_duckdb_path, *, drift: float = 0.0006, config=CONFIG) -> SimpleNamespace:` and in `build_body` pass `config=config` instead of `config=CONFIG`.

- [ ] **Step 2: Write the failing tests**

```python
# tests/research/test_authored_evaluation_length.py
"""Owner ruling 2026-10-11 (spec 14): an authored evaluation spans the most recent authored_evaluation_sessions
sessions with SP2c's fold and holdout proportions; a catalogue evaluation is unchanged."""
from dataclasses import replace

import exchange_calendars as xcals
import pandas as pd
import pytest

from tests.authored.fake_sandbox import FakeSandbox
from tests.research.authored_fixtures import CATALOGUE_KEY, CONFIG, research_world, time_of_day_key
from trader.research.service_config import (ResearchConfigError, ResearchServiceConfig, authored_walk_forward,
                                            load_research_service_config)

SHORT = replace(CONFIG, authored_evaluation_sessions=20)        # 38 sessions, holdout 5 -> 20 sessions, holdout 3


def sessions(start, end) -> int:
    return len(xcals.get_calendar("XNYS").sessions_in_range(pd.Timestamp(start), pd.Timestamp(end)))


def test_the_default_is_302_with_sp2c_proportions():
    config = load_research_service_config({})
    assert (config.period_sessions, config.authored_evaluation_sessions) == (690, 302)
    short = authored_walk_forward(config)
    assert (short.period_sessions, short.holdout_sessions, short.folds, short.embargo_sessions) == (302, 39, 6, 5)
    assert authored_walk_forward(replace(config, authored_evaluation_sessions=690)) == config


def test_a_config_built_in_code_with_a_shorter_period_keeps_its_period():
    # A test or SP2c world that builds ResearchServiceConfig directly (period 38 or 40) keeps the 302 default;
    # the authored length never exceeds the period.
    short = ResearchServiceConfig(period_sessions=38, folds=2, embargo_sessions=1, holdout_sessions=5)
    assert authored_walk_forward(short) == short


@pytest.mark.parametrize("value", [0, True, 691, "302"])
def test_a_bad_authored_length_fails_load(value):
    with pytest.raises(ResearchConfigError, match="authored_evaluation_sessions"):
        load_research_service_config({"research_service": {"authored_evaluation_sessions": value}})


def test_a_length_too_short_for_the_folds_fails_load():
    with pytest.raises(ResearchConfigError, match="authored_evaluation_sessions"):
        load_research_service_config({"research_service": {"authored_evaluation_sessions": 20}})


def test_an_authored_evaluation_spans_the_authored_length(tmp_path, tmp_duckdb_path):
    world = research_world(tmp_path, tmp_duckdb_path, config=SHORT)
    spec = world.build(time_of_day_key(FakeSandbox()))
    assert sessions(spec.base.period_start, spec.base.period_end) == 20
    assert (spec.base.holdout_sessions, spec.base.folds, spec.base.embargo_sessions) == (3, 2, 1)


def test_a_catalogue_evaluation_keeps_the_full_length(tmp_path, tmp_duckdb_path):
    world = research_world(tmp_path, tmp_duckdb_path, config=SHORT)
    spec = world.build(CATALOGUE_KEY)
    assert sessions(spec.base.period_start, spec.base.period_end) == 38
    assert (spec.base.holdout_sessions, spec.base.folds, spec.base.embargo_sessions) == (5, 2, 1)


def test_both_periods_end_on_the_same_last_session(tmp_path, tmp_duckdb_path):
    world = research_world(tmp_path, tmp_duckdb_path, config=SHORT)
    authored, catalogue = world.build(time_of_day_key(FakeSandbox())), world.build(CATALOGUE_KEY)
    assert authored.base.period_end == catalogue.base.period_end         # "the most recent" sessions
    assert authored.base.period_start > catalogue.base.period_start
```

- [ ] **Step 3: Run them to verify they fail**

Run: `.venv/bin/python -m pytest tests/research/test_authored_evaluation_length.py -q`
Expected: FAIL (`ImportError: cannot import name 'authored_walk_forward'`).

- [ ] **Step 4: Implement the config value** (`trader/research/service_config.py`)

Add `"authored_evaluation_sessions"` to `_INTEGER_FIELDS`, and the field after `shadow_incomplete_after_hours`:

```python
    # Owner ruling 2026-10-11 (SP3 spec 14): an authored strategy is evaluated on the most recent N sessions.
    authored_evaluation_sessions: int = 302
```

Add below `_require_buildable_walk_forward`:

```python
def authored_walk_forward(config: ResearchServiceConfig) -> ResearchServiceConfig:
    """The research config an authored evaluation uses: the most recent ``authored_evaluation_sessions`` sessions,
    the same folds and embargo, and the holdout scaled in proportion (SP2c's 90 of 690 becomes 39 of 302)."""
    sessions = min(config.authored_evaluation_sessions, config.period_sessions)
    if sessions == config.period_sessions:
        return config
    holdout = max(1, round(config.holdout_sessions * sessions / config.period_sessions))
    return replace(config, period_sessions=sessions, holdout_sessions=holdout)


def _require_buildable_authored_walk_forward(config: ResearchServiceConfig) -> None:
    if config.authored_evaluation_sessions > config.period_sessions:
        raise ResearchConfigError(
            f"research_service.authored_evaluation_sessions: at most period_sessions ({config.period_sessions})")
    try:
        _require_buildable_walk_forward(authored_walk_forward(config))
    except ResearchConfigError as exc:
        raise ResearchConfigError(f"research_service.authored_evaluation_sessions: {exc}") from None
```

change the import to `from dataclasses import dataclass, fields, replace`, and in `load_research_service_config` call `_require_buildable_authored_walk_forward(config)` right after `_require_buildable_walk_forward(config)`.

- [ ] **Step 5: Use it for an authored key** (`trader/research/cohort.py`)

Add `authored_walk_forward` to the import from `trader.research.service_config` (add the import if the module has none), and in `build_cohort_spec`, right after the `authored_digest = ...` line of Task 4:

```python
    if authored_digest is not None:
        config = authored_walk_forward(config)          # owner ruling: 302 recent sessions, SP2c proportions
```

Everything after it (the period, the walk-forward plan, the fresh-holdout check, `raw["walk_forward"]`) reads this `config`, so the windows and the holdout follow; a catalogue key never reaches the line.

- [ ] **Step 6: Document the owner value** (`config_defaults/trader.yaml`, in the `research_service:` block after `shadow_incomplete_after_hours`):

```yaml
  authored_evaluation_sessions: 302   # SP3: AI-written strategies use the most recent N sessions (holdout scaled; at most period_sessions)
```

- [ ] **Step 7: Run the tests and the SP2c config, cohort and Task 4 tests**

Run: `.venv/bin/python -m pytest tests/research/test_authored_evaluation_length.py tests/research/test_authored_spec.py tests/research/test_authored_backtest.py tests/research/test_authored_trials.py tests/research/test_authored_evaluation.py tests/research/test_authored_evaluation_service.py tests/research/test_authored_attest_shadow.py tests/research/test_service_config.py tests/research/test_cohort.py -q` (the authored smoke, cohort and service tests of Tasks 3-8 must still reach their assertions with the pinned length) (skip a listed SP2c file that does not exist on the base; `grep -rln research_service tests/research` finds the config tests).
Expected: PASS. A shipped `trader.yaml` without the key loads with 302.

- [ ] **Step 8: Commit**

```bash
git add trader/research/service_config.py trader/research/cohort.py config_defaults/trader.yaml \
        tests/research/authored_fixtures.py tests/research/test_authored_evaluation_length.py
git commit -m "feat: evaluate authored strategies on the most recent 302 sessions

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 13: Rulings for the owner and the full suite

**Files:**
- Modify: `docs/superpowers/plans/2026-10-11-ai-paper-sp3-00-index.md` (section "Owner rulings to confirm")

- [ ] **Step 1: Copy the rulings into the index**

The integration of 2026-10-11 already put this plan's owner-facing rulings in the index ("Owner rulings to confirm"). Check that each line there still matches this plan's "Rulings" section; if a ruling changed during implementation, update its index line with the same text. R1-R6 are settled (see "Cross-plan requirements").

- [ ] **Step 2: Full suite** under the shared lock (index rule):

```bash
until mkdir /private/tmp/mmr-suite.lock 2>/dev/null; do sleep 30; done
.venv/bin/python -m pytest tests/ -q -n 8 --timeout=120 --ignore=tests/test_ibrx_async.py \
    --basetemp=/private/tmp/mmr-sp3-04-suite; status=$?
rmdir /private/tmp/mmr-suite.lock; exit $status
```

(without pytest-xdist: drop `-n 8` and use `--timeout=60`). Then `.venv/bin/python -m pytest tests/test_ibrx_async.py --timeout=30 -q` alone. Expected: all pass. Read the summary line; do not trust a count in a doc.

- [ ] **Step 3: Commit**

```bash
git add docs/superpowers/plans/2026-10-11-ai-paper-sp3-00-index.md
git commit -m "docs: list the sp3 plan 4 rulings for the owner

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

## Self-review

- **Spec 7.6, research:** the proxy sends bar i only and returns the decision as a `Signal` (Task 3, ruling 1); fills, costs, folds, holdout, determinism check and shadow replay stay in research (Tasks 3, 8); each authored file is its own family with its own holdout (Task 4: family per `authored:sha256:<hex>`); the smoke stage of 20 sessions of one research conid, a `SANDBOX_ERROR` there ending `REJECTED` without holdout or trial (Task 6); an availability error before the holdout back to `STORED`, the trader counting attempts (Tasks 6, 7; Plan 3 ruling 17); after the holdout the SP2c rule (Task 6). **Spec 7.6, strategy (as amended, section 14):** one live session per deployment for the trading day, holding all its conids, each completed bar streamed (Task 9, decision D1); the digest at load and on every dispatch (Task 10, ruling 20); an availability error gives no signal, an alarm, a reconnect, and stays `DEPLOYED` (Task 9); `SANDBOX_ERROR` retires, positions untouched (Tasks 9, 10). **Spec 7.7:** the case field Jev reads (Task 5, with `judged_count` from Task 6). **Spec 7.9:** the strategy service refuses an authored deployment without a passing proof for the running image (Task 10); research reports the running research image in `STARTED` (Task 6, Plan 3 ruling 12). **Spec 8 rows owned here:** sandbox unavailable before and after the holdout, `SANDBOX_ERROR` in evaluation, live busy or unreachable, live `SANDBOX_ERROR`, digest mismatch at load and dispatch (Tasks 6, 7, 9, 10). **Spec 10:** pools (research one session at a time, Tasks 3, 6; live busy, Task 9; the live-versus-research socket test is Plan 2's), lookahead by construction (Tasks 3, 9), equivalence (Task 11), trial count on the case (Task 5).
- **Not here:** the trader's states, attempts and refusals (Plan 3); the sandbox service and its real-socket tests (Plan 2); the author loop, the scoreboard and the end-to-end acceptance over signed RPC (Plan 5).
- **Placeholders:** none; every code step has its code. Each shape owned by another plan is built or read in one function (Global Constraints).
- **Module names:** the proxy lives in `trader/research/authored_backtest.py` (imported by `evaluation_jobs.py`, which `authored_evaluation.py` imports, so one file would be an import cycle); the index lists it (decision D10). `trader/research/authored_outcome.py` and `trader/authored/streaming.py` are also Plan 4's. Plan 5 uses `trader.research.authored_backtest.connect_sandbox` only in its acceptance test seam.
- **Names:** `BarStream.send_frame` and `last_sent` (Task 2) in Tasks 3 and 9; `RunEnvironment.authored_digest` / `sandbox_socket` (Task 3) in Tasks 4 and 8; `EvaluationPaths.sandbox_socket` (Task 4) in Tasks 6 and 8; `TrialDenominator` and `evaluate_cohort(authored_judged_count=)` (Task 5) in Task 6; `AuthoredEnd`, `AuthoredEvaluationEnded`, `AUTHORED_END_CODE`, `started_body` (Task 6) in Task 7; `AuthoredLiveStrategy(conids=...)`, `close_sessions` / `authored_binding_intact` (Task 9) in Tasks 10 and 11; `RESEARCH_SESSION_CONID` (Task 3) in Task 11; `orb_fixture_source` (Task 2) in Task 11.
