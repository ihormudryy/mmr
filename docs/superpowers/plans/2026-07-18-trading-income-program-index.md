# Trading Income Foundation Implementation Plan Suite

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement these plans task-by-task. Use `superpowers:test-driven-development` for every behavior change and `superpowers:verification-before-completion` before each commit.

**Goal:** Turn the approved trading-income design into one deterministic, fully audited, long-only intraday strategy that earns promotion through research, paper evidence, a tightly bounded live canary, and deliberate capital scaling.

**Architecture:** Complete and activate the existing trader-owned command plane first. Build research evidence in a separate DuckDB, then route signed eligible strategy artifacts through the same command coordinator used by operators. Broker state and fills remain authoritative; research, paper, canary, and scaling consume immutable attestations and authoritative attribution rather than dashboard or strategy-local state.

**Tech Stack:** CPython 3.12.13, DuckDB, dataclasses/Pydantic, canonical JSON, Ed25519 via `cryptography`, `exchange_calendars` XNYS, NumPy/SciPy/pandas, ZeroMQ typed RPC, IBKR, pytest, Hypothesis, Docker Compose.

## Global Constraints

- Source design: `docs/superpowers/specs/2026-07-18-trading-income-foundation-design.md` at or after commit `e83dd58`.
- Initial market: liquid US equities and ETFs, long-only, regular-session intraday, broker-confirmed flat every session.
- Initial runtime: exactly one automated strategy globally through Canary and Scale 1.
- LLMs may support research and review. No LLM output may call, approve, retry, cancel, resize, promote, reset, or allocate a production order.
- No program may introduce a second broker-order path. Human and automated actions enter `TradingCommandCoordinator` and use the same command ledger, audit, dispatch, and reconciliation.
- Passing software tests permits paper operation only. Paper and live promotion are evidence gates with elapsed-session requirements; they cannot be compressed into CI.

---

## Delivery graph

```text
[P1] safety + command-plane activation ──────────────┐
                                                     v
[P2] research evidence + signed eligibility ──> [P3] deterministic automation
                                                     │
                                                     v
                                          [P4] paper + live canary
                                                     │
                                                     v
                                          [P5] controlled scaling
```

P1 and P2 may be implemented in parallel in isolated worktrees because P2 writes only the research database and exports frozen attestation contracts. P3 may begin only after P1's production activation gate and P2's signature/eligibility contracts are merged. P4 consumes a complete P3 vertical slice. P5 begins only after the P4 live evidence gate is implemented and the first strategy has accumulated qualifying evidence.

## Plans

1. [P1 — Safety and command-plane completion](2026-07-18-trading-income-p1-safety-command-plane.md)
2. [P2 — Research evidence foundation](2026-07-18-trading-income-p2-research-evidence.md)
3. [P3 — Deterministic automated execution](2026-07-18-trading-income-p3-deterministic-automation.md)
4. [P4 — Paper and live-canary operations](2026-07-18-trading-income-p4-paper-live-canary.md)
5. [P5 — Controlled scaling and portfolio reuse](2026-07-18-trading-income-p5-scaling-portfolio.md)

## Frozen cross-program contracts

Do not modify the already-frozen `CommandReceipt`, `DomainEvent`, or `SnapshotWithCursor` dataclasses. New contracts are additive:

```python
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal

@dataclass(frozen=True)
class BrokerPositionEvidence:
    conid: int
    quantity: Decimal
    market_value: Decimal
    source_timestamp: datetime

@dataclass(frozen=True)
class BrokerOrderEvidence:
    order_entity_id: str
    conid: int
    action: Literal["BUY", "SELL"]
    total_quantity: Decimal
    filled_quantity: Decimal
    status: str
    source_timestamp: datetime

@dataclass(frozen=True)
class BrokerRiskSnapshot:
    generation_id: int
    source_cursor: int
    promoted_at: datetime
    account_id: str
    account_mode: Literal["paper", "live"]
    net_liquidation: Decimal
    daily_pnl: Decimal
    positions: tuple[BrokerPositionEvidence, ...]
    working_orders: tuple[BrokerOrderEvidence, ...]

@dataclass(frozen=True)
class EligibilityAttestation:
    attestation_id: str
    artifact_digest: str
    dataset_manifest_digest: str
    allowlist_digest: str
    ruleset_name: str
    ruleset_version: str
    ruleset_digest: str
    state: Literal["CANDIDATE", "PAPER_ELIGIBLE", "CANARY_ELIGIBLE", "SUSPENDED", "RETIRED"]
    account_mode: Literal["research", "paper", "live"]
    max_gross_allocation: Decimal
    permitted_conids: tuple[int, ...]
    evidence_refs: tuple[str, ...]
    created_at: datetime
    expires_at: datetime
    operator_review_id: str | None
    signature: str

@dataclass(frozen=True)
class EntryPolicy:
    order_type: Literal["LIMIT", "MARKETABLE_LIMIT"]
    limit_offset_bps: Decimal
    tif: Literal["DAY"]

@dataclass(frozen=True)
class StopPolicy:
    stop_price: Decimal
    order_type: Literal["STP", "STP_LMT"]

@dataclass(frozen=True)
class TargetPolicy:
    target_price: Decimal
    order_type: Literal["LMT"]

@dataclass(frozen=True)
class TimeExitPolicy:
    max_hold_bars: int | None
    close_by: datetime

@dataclass(frozen=True)
class ExecutionIntent:
    artifact_id: str
    session_id: str
    bar_id: str
    signal_id: str
    intent_id: str
    command_id: str
    account_mode: Literal["paper", "live"]
    conid: int
    side: Literal["BUY", "SELL"]
    requested_quantity: Decimal | None
    risk_fraction: Decimal
    entry_policy: EntryPolicy
    stop_policy: StopPolicy
    target_policy: TargetPolicy | None
    time_exit_policy: TimeExitPolicy
    artifact_digest: str
    eligibility_attestation_digest: str
    signal_timestamp: datetime
    completed_bar_timestamp: datetime
```

Canonical digests use the repository's UTF-8 sorted canonical JSON rules: no NaN/Infinity, timestamps normalized to UTC `Z`, decimals serialized as strings, and sequence order preserved. P2 adds golden fixtures before these rules become an artifact contract. IDs are lowercase hex SHA-256 digests with stable prefixes and no colon: `intent-<32hex>`, `auto-<32hex>`, `artifact-<32hex>`, `attest-<32hex>`.

## Storage ownership and migration ranges

| Store | Owner | Version range |
|---|---|---|
| `mmr_journal.duckdb` | `trader_service` only | existing 1-23; P1 uses 24-29; P3 uses 30-39; P4 uses 40-49; P5 uses 50-59 |
| `mmr_research.duckdb` | offline research CLI/process only | independent versions beginning at 1 |
| `mmr_history.duckdb` | existing history writers/readers | unchanged |
| strategy-service revision DB | `strategy_service` | unchanged; automated runtime does not store authority here |

The research database path is `storage.research_duckdb_path` and defaults to `~/.local/share/mmr/data/mmr_research.duckdb`. It is backed up, but never opened by `trader_service`; production receives immutable signed artifacts and attestations through files mounted read-only.

## Global safety invariants

- Missing, stale, non-finite, crossed, halted, wrong-mode, wrong-account, unverified, or internally inconsistent evidence rejects exposure-increasing commands.
- Broker snapshot, market quote, what-if response, calendar decision, risk policy, and artifact/attestation digests are recorded with the command before dispatch.
- Initial approval and immediate pre-dispatch revalidation are separate checks. Failure at either point rejects without submission.
- Reducing behavior is allowed during degradation only after the trader proves `abs(resulting_position) < abs(current_position)` and no sign flip.
- `OUTCOME_UNKNOWN` is never retried under a new command ID and blocks equivalent target commands until reconciliation.
- Restarts do not clear pause, suspension, circuit breaker, unresolved commands, allocation, or evidence counters.
- Live allocation and reset require authenticated operator action and Ed25519-signed authority created outside the trader container.
- Final paper/canary gates use real Compose services and IB paper evidence; mocks and synthetic soak are development gates only.

## Canonical verification commands

Focused tasks use the commands in each plan. Every program ends with:

```bash
uv sync --frozen --extra test
uv run --frozen --extra test pytest tests/ --timeout=30 -q --ignore=tests/test_ibrx_async.py
docker compose config --quiet
```

The repository currently has two documented pre-existing timezone/DST failures. A worker must reproduce them on the program's base commit before classifying them as unrelated; no new failure may be waived.

## Program release gates

- [ ] P1: production command socket is unified and semantically ready; fenced broker evidence, dispatch revalidation, durable breaker, restart reconciliation, and verified flatten drills pass.
- [ ] P2: one complete experiment family, including failures, is reproducible from an immutable manifest; quantitative and signed qualitative gates produce a verifiable `PAPER_ELIGIBLE` artifact.
- [ ] P3: one eligible strategy produces deterministic intents, enters the existing coordinator, places protection, reconciles, flattens, attributes, and replays without an alternate order path.
- [ ] P4 paper: at least 30 calendar days, 20 sessions, 50 round trips, and five instruments satisfy every safety and evidence gate.
- [ ] P4 live canary: authenticated `CANARY_ELIGIBLE` authority plus at least 30 live sessions, 75 round trips, five instruments, zero capital-safety incidents, and signed deviation review.
- [ ] P5: allocation changes are signed, bounded to the 6%/9%/13.5%/15% ladder, reversible, and portfolio-aware before a second strategy is accepted.

## Spec coverage audit

| Design section | Owning tasks |
|---|---|
| §2-3 binding decisions/safety boundaries | Index global invariants; P1 T1-T8; P3 T2-T6 |
| §4.1-4.3 dataset, experiments, eligibility | P2 T1-T8 |
| §4.4-4.6 intent, coordinator, approval context | P1 T1-T4; P3 T1-T3 |
| §4.7-4.8 session risk/broker truth | P1 T6-T7; P3 T4-T6 |
| §4.9-4.10 attribution/promotion | P3 T7-T8; P4 T1-T7 |
| §5 runtime flow | P3 T2-T9 integration gate |
| §6 stale/failure/breaker semantics | P1 T4-T8; P3 T4-T6; P4 T3-T7 |
| §7 audit/forensic replay | P3 T7-T8; P4 T2/T7 |
| §8 research/paper eligibility | P2 T2-T8; P4 T1-T2/T8 |
| §9 live canary | P3 T4-T6; P4 T3-T9 |
| §10 scaling/second strategy | P5 T1-T9 |
| §12 verification hierarchy | Per-task tests; P1 T8; P3 T9; P4 T4/T8/T9; P5 T9 |
| §13 daily operations | P4 T7-T9 |
| §14 success definition | Program release gates and P4/P5 exit criteria |
| §15 non-goals | Index scope and each program's global constraints |
