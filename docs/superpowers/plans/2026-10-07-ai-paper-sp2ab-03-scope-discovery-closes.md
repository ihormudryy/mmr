# AI Paper SP2 — Plan 3: Trader: discretionary scope rule, discovery read, model-driven closes — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the bot trade its own ideas only inside an operator-registered `discretionary` deployment whose scope is a rule the trader proves at admission and again at dispatch; give `ai_supervisor` a read-only, trader-owned Alpaca discovery read with honest coverage; and close the two gaps in SP1's model-driven closes (ownership by the experiment, and a partial close that can move protection).

**Architecture:** Five new trader modules in `trader/automation/`: `discretionary_deployment.py` (sealed rule + attestation), `scope_evidence.py` (exact IB contract details, the 20-session dollar volume, symbol → conid), `discretionary_scope.py` (pure rule evaluation, the check table, the admission service, the dispatch gate), `ai_discovery_wire.py` (strict wire models shared with Plan 6) and `ai_discovery.py` (the reader). `AiDeploymentStore` gets a `kind` column and `get_sealed_any`. `AiPaperDecisionService` branches on the deployment kind in `_deployment`, runs the scope check in `_execute_entry`, reads the dispatch verdict in `_start_saga`, and checks ownership in `_execute_reduction`. Two typed RPC methods: `register_discretionary_deployment` (command, `cli`) and `discover_ai_candidates` (query, `ai_supervisor`, run on a worker thread).

**Tech Stack:** Python 3.12, DuckDB (`SchemaMigrator`, `DuckDBConnection.transaction` / `execute`), pydantic v2 strict models, `ib_async` (`reqContractDetailsAsync` only), the existing Alpaca adapters in `trader/data_providers/alpaca/`, pytest. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-10-07-ai-paper-sp2ab-autonomous-loop-design.md`. Binding: section 6.4 (model-driven closes), 6.5 (discovery read), 6.6 (discretionary deployment kind), 10 (data sources); the "Discovery partial / failed" row of section 9; the "Initialization" and "Discovery route" paragraphs of section 12. Index: `docs/superpowers/plans/2026-10-07-ai-paper-sp2ab-00-index.md` (method names, refusal code, `detail.part` values, migrations 100–104).

## Global Constraints

- Base: master after SP1 Plans 3–6. Read code at `/private/tmp/sp1-impl6` until then. Code is cited by file and function name.
- Plans 1 and 3 both edit `trader/automation/ai_paper_decision.py`, `trader/messaging/production_api.py` and `trader/messaging/principals.py`. This plan touches only: `AiPaperDecision._check_shape`, `DecisionRow`, `_ROW_COLUMNS`, the migration-56 CREATE, `AiPaperDecisionStore` (one new method, `links_for_order_ref`), `_Refusal`, `AiPaperDecisionService.__init__` / `attach_scope` / `_deployment` / `_execute_entry` / `_start_saga` / `_execute_reduction`, `deployment_binding`. No test here sends `submit_ai_paper_decision` over RPC, so Plan 1's epoch envelope does not affect them.
- No legacy data (owner, 2026-10-07): changed SP1 tables edit their CREATE in place (migration 55 `ai_deployments` gains `kind`; migration 56 `ai_paper_decisions` gains `experiment_id`, `deployment_kind`). No ALTER, no backfill. One new table: migration **100** `discretionary_scope_checks`. 101–104 stay unused.
- Rights: `register_discretionary_deployment` = `{"cli"}`; `discover_ai_candidates` = `{"ai_supervisor"}`. Each handler also checks its principal in-process.
- Paper only: registration refuses a non-paper account (`ACCOUNT_NOT_PAPER`); the `ai_paper` services exist only on paper (`_ai_paper_config`).
- Refusal for the rule: `OUT_OF_DISCRETIONARY_SCOPE`, `outcome.detail = {"part", "reason", "phase", "check_id"}`, `part` ∈ `exchange, instrument_type, price, dollar_volume, liquidity, trading_filter, evidence_stale`.
- Reductions (`CLOSE`, `PARTIAL_CLOSE`) never read the scope rule, the trading filter or discovery.
- Discovery never calls the IB scanner. Alpaca keys stay in the trader process: `Trader.alpaca_api_key_id` / `Trader.alpaca_api_secret_key` (shared with Plan 2, same names). Blank keys fail the read loudly (`DISCOVERY_SOURCE_UNAVAILABLE`). No key value is logged or returned.
- Price history is not touched: Alpaca daily bars used by the rule live in memory only.
- Strict wire models: `ConfigDict(extra="forbid", strict=True)`; domain code re-checks types (`type(x) is int`, never bool).
- DuckDB only through `DuckDBConnection.execute` / `transaction`; `yaml.safe_load` only (the trading filter already does).
- Per task: `.venv/bin/python -m pytest <files> -q --timeout=30`. Full suite once, in Task 11: `.venv/bin/python -m pytest tests/ -q --timeout=60 --ignore=tests/test_ibrx_async.py`.
- Commit subjects `feat: ...`, lowercase, imperative. Every message ends with a blank line and `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- No container restart, broker order or deploy is authorized by this plan.

## Rulings

1. **Record.** A discretionary deployment is its own dataclass (`DiscretionaryDeployment`: `kind`, `style`, `scope_rule`, `attestation`) with its own digest domain `mmr.ai-discretionary-deployment.v1\x00`, stored in `ai_deployments` with `kind = 'discretionary'` and provenance `OPERATOR_ATTESTED`. Insert-only, like strategy rows. `get_sealed` stays strategy-only (`DEPLOYMENT_KIND_MISMATCH` on a discretionary digest), so no SP1 caller can mistake one for the other; `get_sealed_any` serves both. `AiDeployment.from_json` already demands its exact key set, so `ai_research`'s `register_ai_deployment` cannot register a discretionary body. *Cost if wrong:* none to SP1 rows.
2. **Narrow-only rule.** The operator may narrow every part but not widen past the spec default: `primary_exchanges ⊆ {NYSE, NASDAQ, ARCA}`, `stock_types ⊆ {COMMON, ETF}`, `min_price ≥ 5`, `min_median_dollar_volume ≥ 20_000_000`, `0 < max_order_share_of_dollar_volume ≤ 0.01`. The 20-session window is fixed. The trading filter always applies. *Cost:* ADRs, REITs, preferreds, ETNs and closed-end funds are out until the owner widens the code sets (owner to confirm).
3. **Instrument identity proof.** At each admission the trader asks IB `reqContractDetails(Contract(conId=N))` (5 s timeout) and needs exactly one detail whose `contract.conId == N`. Exchange = `contract.primaryExchange` (never `exchange`/`SMART`). Type = `contract.secType == "STK"`, `contract.currency == "USD"` and `ContractDetails.stockType ∈ rule.stock_types`. Warrants (`secType` `WAR`, or `stockType` `WARRANT`), rights (`RIGHT`), units (`UNIT`) and OTC (`primaryExchange` `PINK`/`OTC…`) fail by these allow-lists. A blank `stockType` (stub or CSV universe rows, an old gateway) refuses `instrument_type`; the local universe row is never used as proof. The IB definition is also remembered in the `_instruments` universe, so quotes and the SP1 entry filter can resolve the conid.
4. **Price part** compares the fresh IB **bid** with `min_price` (the quote has no `last`; the bid is the stricter side). *Cost:* a $5.00 last with a $4.99 bid is refused.
5. **20-session dollar volume (source and freshness).** The 20 latest *closed* XNYS sessions at `now`, each `close × volume`, median. Source: the trader's local daily bars (`TickStorage` `BarSize.Days1`, TRADES), else the trader's Alpaca history adapter (SIP daily bars, split-adjusted), held in memory and cached per `(conid, last closed session)`. Fresh means: the 20 sessions are exactly `latest_closed_sessions(now)`; a new session close makes it stale. Neither source complete → `evidence_stale`. *Cost:* the first entry per symbol per day costs one Alpaca call.
6. **Effective dollar-volume floor.** SP1's `session_risk` applies `LiquidityPolicy.evaluate` to every entry (`MIN_MEDIAN_DOLLAR_VOLUME = 50_000_000`). "≥ $20M plus SP1's liquidity rule" is a conjunction, so the `dollar_volume` part compares with `max(rule.min_median_dollar_volume, 50_000_000)` and names both in `reason`. The sealed default stays $20M (owner to confirm; the effective floor is $50M until SP1's floor changes).
7. **Liquidity part.** Order notional ≤ `max_order_share_of_dollar_volume × median` (default 1%). SP1 applies `LIVE_NOTIONAL_TOLERANCE` (5%) to the attested notional in `prepare_entry` and `session_risk`, so the trader passes `cap / 1.05` as the attested notional; the enforced bound is exactly the cap. A requested quantity over it refuses `liquidity` (not `ORDER_EXCEEDS_ATTESTED_NOTIONAL`). SP1's 0.25 % ADV share cap still applies on top.
8. **Admission vs dispatch.** Admission: fresh IB contract details, fresh IB quote, volume source. Dispatch (inside the saga's entry lock): no IB call and no history read. Price, liquidity and the trading filter use the guard's fresh quote; exchange, type and dollar volume reuse the admission evidence carried on the approval (`ApprovalContext.discretionary_scope`) if it is at most 60 s old and the volume window is still current; otherwise `evidence_stale`. Admission and dispatch run in one `execute` call, so this is seconds. The gate's only I/O is one `kind_of` journal read per AI entry (to fail closed when a discretionary approval lost its evidence) and one check-row write.
9. **`detail.part` at dispatch.** The saga keeps only `error_code`. The dispatch gate writes its verdict to `discretionary_scope_checks` (phase `dispatch`) before it returns the code; `_start_saga` reads it back. A missing row still refuses, with `part = evidence_stale` and reason `dispatch check left no record`. Phases: `admission`, `sizing`, `dispatch`; one row per `(command_id, phase)`.
10. **Fail closed on kind.** A request whose `deployment_digest` is discretionary but whose approval carries no scope evidence is refused at dispatch (`evidence_stale`).
11. **Discovery sources.** Only Alpaca, named explicitly (no registry default, no fallback): movers and most-actives through new raw methods on `AlpacaMovers` (same client, Alpaca's own `last_updated` kept), news through `AlpacaNews.news(symbol, n)`. Everything is labelled `delayed: true, delay_minutes: 15`. A source that raises `ProviderError` is reported (`failed: true`, `error_code` = exception class name, ERROR log) and the read still returns with `complete: false` (spec 9). Blank keys refuse the whole read (`DISCOVERY_SOURCE_UNAVAILABLE`).
12. **Symbol → conid.** Exact only: symbols outside `^[A-Z]{1,5}$` (class shares such as `BRK.B`) are `SYMBOL_FORM_UNSUPPORTED` without a lookup. Others go to IB `reqContractDetails(Contract(symbol=S, secType="STK", exchange="SMART", currency="USD"))`; rows with `contract.symbol == S` only; one distinct conId → `RESOLVED`, none → `NOT_FOUND`, more → `AMBIGUOUS`. Cached per ET session date. At most 40 lookups and 30 s per read; the rest are `RESOLUTION_BUDGET` (and `complete: false`). An IB error is `RESOLUTION_FAILED` and is not cached. *Cost:* the first read of a day may leave symbols for the next cycle.
13. **Discovery precheck.** Per resolved candidate: exchange and type from the IB details, price from the delayed Alpaca price, dollar volume from the cache or (at most 20 fetches per read) the volume source, and the trading filter. `PASS` means every part except `liquidity` passed on delayed data; `FAIL` names the part; `NOT_CHECKED` names what is missing. The trader re-checks everything at admission.
14. **6.4 gap: PARTIAL_CLOSE cannot move protection.** SP1 R16 allowed `stop_price` / `target_price` on `PARTIAL_CLOSE`, and `LiquidationService._cancel_conid_orders` prefers them over the saga handover. This plan refuses them (`DECISION_INVALID`) and passes none, so the remainder is re-protected at the existing stop and target. No existing stop → SP1's `STOP_PRICE_MISSING` escalation to a full close. The one-strategy SELL path is unchanged.
15. **6.4 gap: ownership.** A reduction needs an `ENTER` decision row of the *current* experiment on that conid whose protective saga reports `filled_quantity > 0`; otherwise `POSITION_NOT_OWNED`. It applies in every experiment state (a non-owned CLOSE while `KILLED` gains nothing). Ownership is per conid, not per share: with SP1's "the bot owns the paper account alone", a human lot added to an owned conid is closed with it.
16. **Labels.** `get_ai_deployment` returns `kind`; `DecisionLink` gains `deployment_kind`; a discretionary ENTER's link reports `strategy_version = "discretionary"`, so the scoreboard splits and `mmr` show the word. `mmr ai-deployment show` prints `DISCRETIONARY (operator attested, no backtest evidence)`.
17. **Discovery runs on a worker thread** (`execution="thread"`): network I/O must not block the trader loop that `ib_async` uses. It exists only when `ai_paper.enabled` built the services.

## Cross-plan additions

Plan 6 (discovery client, parsers) and Plan 5 (RPC clients) use these exact names.

```python
# trader/automation/ai_discovery_wire.py  (pydantic v2, every model ConfigDict(extra="forbid", strict=True))
Origin = Literal["gainer", "loser", "most_active", "watchlist"]
Resolution = Literal["RESOLVED", "NOT_FOUND", "AMBIGUOUS", "SYMBOL_FORM_UNSUPPORTED",
                     "RESOLUTION_BUDGET", "RESOLUTION_FAILED"]
ScopePart = Literal["exchange", "instrument_type", "price", "dollar_volume", "liquidity",
                    "trading_filter", "evidence_stale"]

class DiscoverAiCandidatesRequest(BaseModel):
    deployment_digest: str            # ^sha256:[0-9a-f]{64}$, must be a discretionary deployment
    movers_top: int                   # 1..50 (gainers and losers each)
    most_actives_top: int             # 1..100
    watchlist: list[str]              # <= 25 items, each ^[A-Z]{1,5}$
    news_per_symbol: int              # 0..10
    news_symbols_max: int             # 0..30

class SourceCoverage(BaseModel):
    requested: int; returned: int; failed: bool; error_code: Optional[str]; as_of: Optional[str]
class NewsCoverage(BaseModel):
    requested_symbols: int; returned_symbols: int; failed_symbols: list[str]
class ResolutionCoverage(BaseModel):
    requested: int; resolved: int; unresolved: int; failed: int
class DiscoveryCoverage(BaseModel):
    movers: SourceCoverage; most_actives: SourceCoverage; watchlist: SourceCoverage
    news: NewsCoverage; resolution: ResolutionCoverage; complete: bool
class ScopePrecheck(BaseModel):
    status: Literal["PASS", "FAIL", "NOT_CHECKED"]; part: Optional[ScopePart]; reason: str
    median_dollar_volume_20d: Optional[float]
class DiscoveryNewsItem(BaseModel):
    id: str; published: str; title: str; summary: str; url: str; source: str
class DiscoveryCandidate(BaseModel):
    symbol: str; origins: list[Origin]; conid: Optional[int]; resolution: Resolution
    primary_exchange: Optional[str]; stock_type: Optional[str]
    price: Optional[float]; change_pct: Optional[float]; volume: Optional[float]
    source_timestamp: Optional[str]; delayed: bool
    scope_precheck: ScopePrecheck
    news_status: Literal["OK", "FAILED", "SKIPPED"]; news: list[DiscoveryNewsItem]
class DiscoverAiCandidatesResponse(BaseModel):
    read_at: str; source: Literal["alpaca"]; delayed: bool; delay_minutes: int
    deployment_digest: str; coverage: DiscoveryCoverage; candidates: list[DiscoveryCandidate]
```

- `discover_ai_candidates` errors: `DISCOVERY_SOURCE_UNAVAILABLE` (Alpaca keys blank), `DEPLOYMENT_KIND_MISMATCH`, `DEPLOYMENT_NOT_SEALED`, `DEPLOYMENT_TAMPERED`. The read can take up to about 40 s on the first cycle of a day: Plan 5's query client for this method uses a 90 s timeout. Plan 6 drops `FAIL` and every candidate whose `conid` is null before any model call.
- `register_discretionary_deployment` request: `{"deployment": {"kind": "discretionary", "style": "intraday_long", "scope_rule": {"primary_exchanges": [...], "stock_types": [...], "min_price": float, "min_median_dollar_volume": float, "max_order_share_of_dollar_volume": float}, "attestation": {"operator": str, "statement": str, "attested_at": iso-with-offset}}}` → `CommandReceipt` with `outcome = {"digest", "created", "kind": "discretionary"}`. `trader.automation.discretionary_deployment.DEFAULT_SCOPE_RULE` holds the spec default.
- `get_ai_deployment` response gains `"kind": "strategy" | "discretionary" | None`.
- `submit_ai_paper_decision`: an ENTER naming a discretionary digest may fail `OUT_OF_DISCRETIONARY_SCOPE` with `outcome.detail = {"part": ScopePart, "reason": str, "phase": "admission" | "sizing" | "dispatch", "check_id": str | None}`. A `PARTIAL_CLOSE` must carry `stop_price = target_price = null` (else `DECISION_INVALID`). A reduction of a conid the current experiment never filled is `POSITION_NOT_OWNED`.
- `DecisionLink.deployment_kind: Optional[str] = None` (last field).
- Compose: the `ai` service must not inherit `x-mmr-common-env` (it carries `ALPACA_API_KEY_ID` / `ALPACA_API_SECRET_KEY`, `docker-compose.yml`).
- `Trader.alpaca_api_key_id` / `Trader.alpaca_api_secret_key` (shared with Plan 2; whichever lands first adds them, Task 10 here).

## Review Focus

1. **A blank `stockType`** (stub universe row, CSV import or an old gateway) must never pass. → Task 6 `test_blank_stock_type_is_instrument_type`, Task 7 `test_each_failed_part_is_refused_with_its_code[blank-type]`.
2. **The bid falls below the floor between admission and dispatch.** Refused at dispatch with `part = price`, `phase = dispatch`, no order sent. → Task 8 `test_price_falling_below_the_floor_at_dispatch_is_refused_with_its_part`.
3. **Quantity one share over 1 % of the median** (the 5 % tolerance trap). → Task 7 `test_liquidity_boundary_is_exact`.
4. **Alpaca movers fail while most-actives works.** Coverage shows the failure, `complete` is false, only seen candidates are returned, the IB scanner is untouched. → Task 9 `test_a_failed_source_is_reported_not_hidden`, Task 10 `test_discovery_over_signed_rpc_reports_partial_coverage_and_never_scans`.
5. **A close of a position the experiment never bought**, and a partial close that tries to move the stop. → Task 2 `test_a_position_the_experiment_never_entered_is_not_closed`, Task 1 `test_partial_close_with_prices_is_refused`.

## Spec 6.4 audit (model-driven closes)

| 6.4 bullet | Status | Proof |
|---|---|---|
| Only positions attributed to this experiment | **Gap** → Task 2 | — |
| Only through SP1's scoped safe close | Covered | `tests/automation/test_ai_paper_reductions.py::test_close_never_builds_a_bracket` |
| Size from fresh broker evidence | Covered | `start_broker_proven_close` captures a fresh snapshot; `test_close_without_a_broker_position_is_refused`; `LiquidationService._admit_partial` re-captures (`tests/test_liquidation_service.py::test_partial_quantity_edge_cases`) |
| Partial cannot exceed held or reverse | Covered | `test_partial_larger_than_the_position_is_refused`, `test_buy_side_close_of_a_long_is_not_a_reduction`, `tests/test_liquidation_service.py::test_live_position_at_or_below_q_at_dispatch_is_fully_closed` |
| Remainder protected at the **existing** stop/target | **Gap** → Task 1 | — |
| Entry restrictions and loss breaches do not block | Covered | `test_close_works_while_paused_and_after_a_daily_loss_breach`, `test_a_reduction_of_a_denylisted_symbol_still_passes`; pinned for the scope rule in Task 11 |
| After the entry cutoff | Behaviour present, no test | pinned in Task 11 `test_close_after_the_entry_cutoff_is_admitted` |
| Kill and session flatten take precedence | Covered for kill | `test_close_joins_the_kill_flatten_while_killed`, `test_killed_without_a_flatten_yet_is_refused_retryable`, `test_close_joins_a_time_exit_root`, `test_partial_during_another_owners_close_is_exit_in_progress`; session flatten while ARMED pinned in Task 11 |
| Uncertain closes never duplicate | Covered | `test_new_decision_on_a_conid_with_a_pending_close_is_refused`, `tests/test_liquidation_service.py::test_partial_retry_after_the_fill_returns_its_root` |
| A stale close racing a stop fill cannot oversell | Covered | `tests/test_liquidation_service.py::test_close_ends_closed_without_reduce_when_the_stop_filled_in_the_cancel_race`, `::test_a_stop_fill_after_the_admission_hold_sends_no_target_of_the_old_size`, `tests/test_safe_close_integration.py::test_a_stop_fill_after_the_final_admission_hold_is_refused_at_the_order_boundary` |

---

## File map

| File | Task |
|---|---|
| `trader/automation/ai_paper_decision.py` | 1, 2, 7, 8 |
| `tests/automation/ai_paper_world.py`, `tests/automation/test_ai_paper_reductions.py` | 1, 2, 8, 11 |
| `trader/automation/discretionary_deployment.py` (new), `trader/automation/ai_deployments.py` | 3 |
| `trader/automation/ai_paper_actions.py`, `trader/messaging/production_api.py`, `trader/messaging/principals.py`, `trader/sdk.py`, `trader/mmr_cli.py` | 4, 10 |
| `trader/automation/scope_evidence.py` (new), `trader/automation/production_evidence.py` | 5 |
| `trader/automation/discretionary_scope.py` (new), `trader/data/schema_migrations.py` (docstring) | 6, 7, 8 |
| `trader/automation/ai_paper_evidence.py`, `trader/trading/approval_context.py`, `trader/trading/command_stack.py` | 7, 8, 10 |
| `trader/automation/ai_discovery_wire.py`, `trader/automation/ai_discovery.py` (new), `trader/data_providers/alpaca/movers.py` | 9 |
| `trader/trading/trading_runtime.py`, `trader/trader_service.py` (none: the Container fills `Trader`) | 10 |
| `AGENTS.md`, `docs/CLI_REFERENCE.md` | 11 |

---

### Task 1: A partial close keeps the existing stop and target

**Files:**
- Modify: `trader/automation/ai_paper_decision.py` (`AiPaperDecision._check_shape`, `AiPaperDecisionService._execute_reduction`)
- Test: `tests/automation/test_ai_paper_reductions.py`

**Interfaces:**
- Consumes: `LiquidationService.start(...)`, `LiquidationService.attach_protection`, `tests/test_liquidation_service.py::_Protection`.
- Produces: `PARTIAL_CLOSE` with a non-null `stop_price` or `target_price` → `DECISION_INVALID`.

- [ ] **Step 1: Write the failing tests.** Replace `test_partial_close_reaches_the_scoped_partial` and drop `stop_price=97.5` from `test_partial_equal_to_the_position_is_a_full_close`:

```python
from tests.test_liquidation_service import _Protection


@pytest.mark.parametrize("prices", [{"stop_price": 97.5}, {"target_price": 120.0}])
def test_partial_close_with_prices_is_refused(world, prices):          # spec 6.4: no protection edits
    receipt = world.submit(partial_body(world, 100, **prices))
    assert (receipt.state, receipt.error_code) == ("REJECTED", "DECISION_INVALID")
    assert world.liquidation_runs() == set()


def test_partial_close_reprotects_at_the_existing_stop_and_target(world):
    world.liquidation.attach_protection(_Protection(stop_price=95.0, target_price=110.0))
    receipt = world.submit(partial_body(world, 100))
    run = world.liquidation.receipt_for(receipt.outcome["close_root_id"])
    assert (run.goal, run.goal_quantity, run.stop_price, run.target_price) == ("partial", 100.0, 95.0, 110.0)


def test_partial_equal_to_the_position_is_a_full_close(world):
    receipt = world.submit(partial_body(world, 300))
    assert world.liquidation.receipt_for(receipt.outcome["close_root_id"]).goal == "zero"
```

- [ ] **Step 2: Run, expect FAIL.** `.venv/bin/python -m pytest tests/automation/test_ai_paper_reductions.py -q --timeout=30` → the price case is `CLOSE_PENDING`, and the re-protect test sees the decision's prices.
- [ ] **Step 3: Implement.** In `_check_shape`, replace the `PARTIAL_CLOSE` line:

```python
        if self.action == "PARTIAL_CLOSE":
            if self.quantity is None:
                raise DecisionInvalid("PARTIAL_CLOSE needs a quantity")
            if self.stop_price is not None or self.target_price is not None:
                raise DecisionInvalid("PARTIAL_CLOSE keeps the existing stop and target; "
                                      "stop_price and target_price must be null")
```

In `_execute_reduction`, drop the two price arguments, so the handover's prices size the re-protect:

```python
        close = start_broker_proven_close(
            liquidation=self._liquidation, broker=self._broker, account_id=self._account_id,
            command_id=cmd.command_id, conid=decision.conid, side=decision.side,
            quantity=float(decision.quantity) if partial else None,
            deadline=self._steps.now_utc() + dt.timedelta(seconds=self._close_deadline_seconds))
```

- [ ] **Step 4: Run.** The same file plus `tests/automation/test_automated_command_boundary.py` (one-strategy SELL unchanged) → PASS.
- [ ] **Step 5: Commit** — `feat: keep the existing stop and target on an ai_paper partial close`.

---

### Task 2: Only a position the experiment bought can be reduced

**Files:**
- Modify: `trader/automation/ai_paper_decision.py` (migration-56 CREATE adds `experiment_id VARCHAR`; `_ROW_COLUMNS` and `DecisionRow` add `experiment_id: Optional[str] = None`; `AiPaperDecisionStore.experiment_owns_conid_in_tx`; `_execute_entry` and `_execute_reduction` stamp the experiment; the reduction `owner_check`)
- Modify: `tests/automation/ai_paper_world.py` (`World.owned`)
- Test: `tests/automation/test_ai_paper_reductions.py` (fixture switches to `owned`; new tests)

**Interfaces:**
- Produces: `AiPaperDecisionStore.experiment_owns_conid_in_tx(conn, account_id: str, experiment_id: str, conid: int) -> bool`; refusal `POSITION_NOT_OWNED`; `World.owned(conid=CONID, quantity=300.0, decision_id="dec-owned-00")`.

- [ ] **Step 1: Write the failing tests.** First the helper in `World`:

```python
    def owned(self, conid=CONID, quantity=300.0, decision_id="dec-owned-00"):
        """An ENTER of the current experiment whose saga reports a fill, then its broker position."""
        assert self.submit(decision_id=decision_id, conid=conid).state == "SUBMITTED"
        command_id = command_id_for(decision_id)
        (raw,) = self.db.execute("SELECT payload FROM automated_order_sagas WHERE command_id = ?",
                                 [command_id], fetch="one")
        payload = {**json.loads(raw), "filled_quantity": str(quantity)}
        self.db.execute("UPDATE automated_order_sagas SET payload = ? WHERE command_id = ?",
                        [json.dumps(payload), command_id])
        self.held(conid, quantity)
        self.dispatch.plans.clear()
        self.scheduled.clear()
```

The reductions fixture becomes `w.owned(CONID, 300.0)`. New tests:

```python
def test_a_position_the_experiment_never_entered_is_not_closed(tmp_path):      # spec 6.4
    w = World(tmp_path, real_liquidation=True)
    w.held(CONID, 300.0)                                     # e.g. a manual proposal: no ENTER of this experiment
    assert w.submit(close_body(w)).error_code == "POSITION_NOT_OWNED"
    assert w.submit(partial_body(w, decision_id="dec-00000002")).error_code == "POSITION_NOT_OWNED"
    assert w.liquidation_runs() == set()


def test_an_entry_of_another_experiment_does_not_own_the_position(world):
    world.experiments.view = ExperimentView("exp2", "ARMED")
    assert world.submit(close_body(world)).error_code == "POSITION_NOT_OWNED"


def test_an_entry_that_never_filled_does_not_own_the_position(tmp_path):
    w = World(tmp_path, real_liquidation=True)
    assert w.submit(decision_id="dec-owned-01").state == "SUBMITTED"
    w.broker.show_working_entry("og-aip-dec-owned-01", quantity=499, filled=0.0)
    w.held(CONID, 300.0)
    assert w.submit(close_body(w)).error_code == "POSITION_NOT_OWNED"


def test_the_decision_row_records_its_experiment(world):
    assert world.decisions.row("dec-owned-00").experiment_id == "exp1"
    world.submit(close_body(world))
    assert world.decisions.row("dec-00000001").experiment_id == "exp1"
```

- [ ] **Step 2: Run, expect FAIL** (`AttributeError: experiment_id`, then `CLOSE_PENDING` instead of `POSITION_NOT_OWNED`).
- [ ] **Step 3: Implement.**

```python
    def experiment_owns_conid_in_tx(self, conn, account_id: str, experiment_id: str, conid: int) -> bool:
        """Spec 6.4: an ENTER of this experiment on the conid whose protective saga reports a fill."""
        rows = conn.execute(
            "SELECT s.payload FROM ai_paper_decisions d "
            "JOIN automated_order_sagas s ON s.command_id = d.command_id "
            "WHERE d.account_id = ? AND d.experiment_id = ? AND d.conid = ? AND d.action = 'ENTER'",
            [account_id, experiment_id, conid]).fetchall()
        return any(Decimal(str(json.loads(payload).get("filled_quantity") or "0")) > 0 for (payload,) in rows)
```

In `_execute_entry`, right after `experiment = self._experiment(allow=("ARMED",))`:
`admission.row = replace(admission.row, experiment_id=experiment.experiment_id)`. In `_execute_reduction`, the same line after its `_experiment(...)` call, and the validation becomes:

```python
        self._validate(cmd, admission, decision.conid, snapshot, working_entry_blocks=False,
                       owner_check=lambda conn: self._not_owned_in_tx(conn, experiment, decision.conid))

    def _not_owned_in_tx(self, conn, experiment: Any, conid: int) -> Optional[str]:
        owned = self._decisions.experiment_owns_conid_in_tx(conn, self._account_id, experiment.experiment_id, conid)
        return None if owned else "POSITION_NOT_OWNED"
```

- [ ] **Step 4: Run** `tests/automation/test_ai_paper_reductions.py tests/automation/test_ai_paper_decision.py tests/test_ai_paper_rpc.py` → PASS.
- [ ] **Step 5: Commit** — `feat: reduce only positions the current experiment bought`.

---

### Task 3: The discretionary deployment record and its sealed storage

**Files:**
- Create: `trader/automation/discretionary_deployment.py`
- Modify: `trader/automation/ai_deployments.py` (migration-55 CREATE adds `kind VARCHAR NOT NULL`; `STRATEGY_KIND`, `DISCRETIONARY_KIND`, `OPERATOR_ATTESTED`; `register` writes `kind`; `register_discretionary`, `get_sealed_any`, `kind_of`; `get_sealed` refuses another kind)
- Test: `tests/automation/test_discretionary_deployment.py`

**Interfaces:**
- Produces: `DiscretionaryScopeRule(primary_exchanges: tuple[str, ...], stock_types: tuple[str, ...], min_price: float, min_median_dollar_volume: float, max_order_share_of_dollar_volume: float)` with `from_json` / `to_json`; `OperatorAttestation(operator: str, statement: str, attested_at: str)`; `DiscretionaryDeployment(style: str, scope_rule, attestation, kind: str = "discretionary")` with `from_json` / `to_json`; `discretionary_digest(deployment) -> str`; `DEFAULT_SCOPE_RULE`; `AiDeploymentStore.register_discretionary(deployment, *, principal, command_id) -> tuple[str, bool]`, `get_sealed_any(digest) -> AiDeployment | DiscretionaryDeployment`, `kind_of(digest) -> str`.

- [ ] **Step 1: Write the failing tests** (real DuckDB in `tmp_path`, `apply_ai_deployment_migration`):

```python
BODY = {"kind": "discretionary", "style": "intraday_long", "scope_rule": DEFAULT_SCOPE_RULE.to_json(),
        "attestation": {"operator": "owner", "statement": "paper only; rule as sealed",
                        "attested_at": "2026-07-17T10:00:00-04:00"}}


def test_round_trip_and_stable_digest():
    dep = DiscretionaryDeployment.from_json(BODY)
    reordered = {**BODY, "scope_rule": {**BODY["scope_rule"], "primary_exchanges": ["NYSE", "ARCA", "NASDAQ"]}}
    assert dep.to_json() == DiscretionaryDeployment.from_json(reordered).to_json()
    assert discretionary_digest(dep) == discretionary_digest(DiscretionaryDeployment.from_json(reordered))


@pytest.mark.parametrize("rule", [
    {"primary_exchanges": ["NYSE", "AMEX"]}, {"primary_exchanges": ["PINK"]}, {"primary_exchanges": []},
    {"stock_types": ["COMMON", "WARRANT"]}, {"stock_types": ["ADR"]}, {"stock_types": ["ETF", "ETF"]},
    {"min_price": 4.99}, {"min_price": True}, {"min_median_dollar_volume": 19_999_999.0},
    {"max_order_share_of_dollar_volume": 0.011}, {"max_order_share_of_dollar_volume": 0.0},
    {"min_price": float("nan")}])
def test_the_rule_can_only_narrow(rule):
    with pytest.raises(DeploymentRefused) as exc:
        DiscretionaryDeployment.from_json({**BODY, "scope_rule": {**BODY["scope_rule"], **rule}})
    assert exc.value.code == "DEPLOYMENT_INVALID"


@pytest.mark.parametrize("body", [
    {**BODY, "extra": 1}, {k: v for k, v in BODY.items() if k != "attestation"}, {**BODY, "kind": "strategy"},
    {**BODY, "style": "swing_long"}, {**BODY, "attestation": {**BODY["attestation"], "attested_at": "2026-07-17"}},
    {**BODY, "attestation": {**BODY["attestation"], "statement": ""}}])
def test_shape_is_exact(body):
    with pytest.raises(DeploymentRefused):
        DiscretionaryDeployment.from_json(body)


def test_a_narrower_rule_is_accepted():
    dep = DiscretionaryDeployment.from_json({**BODY, "scope_rule": {**BODY["scope_rule"], "stock_types": ["ETF"],
                                                                   "min_price": 10.0}})
    assert dep.scope_rule.stock_types == ("ETF",) and dep.scope_rule.min_price == 10.0


def test_store_seals_both_kinds_apart(store):
    dep = DiscretionaryDeployment.from_json(BODY)
    digest, created = store.register_discretionary(dep, principal="cli", command_id="aidep-1")
    assert created and store.register_discretionary(dep, principal="cli", command_id="aidep-1") == (digest, False)
    assert store.get_sealed_any(digest) == dep and store.kind_of(digest) == "discretionary"
    assert store.provenance(digest) == "OPERATOR_ATTESTED"
    with pytest.raises(DeploymentRefused) as exc:
        store.get_sealed(digest)
    assert exc.value.code == "DEPLOYMENT_KIND_MISMATCH"
    strategy_digest, _ = store.register(AiDeployment.from_json(GOOD), principal="ai_research", command_id="s-1")
    assert store.kind_of(strategy_digest) == "strategy" and store.get_sealed(strategy_digest).conids


def test_a_tampered_discretionary_row_is_refused(store):
    digest, _ = store.register_discretionary(DiscretionaryDeployment.from_json(BODY), principal="cli",
                                             command_id="aidep-1")
    store._db.execute("UPDATE ai_deployments SET record_json = replace(record_json, '\"owner\"', '\"other\"') "
                      "WHERE digest = ?", [digest])
    with pytest.raises(DeploymentRefused) as exc:
        store.get_sealed_any(digest)
    assert exc.value.code == "DEPLOYMENT_TAMPERED"


def test_research_cannot_send_a_discretionary_body_as_a_strategy():
    with pytest.raises(DeploymentRefused) as exc:
        AiDeployment.from_json(BODY)
    assert exc.value.code == "DEPLOYMENT_INVALID"
```

- [ ] **Step 2: Run, expect FAIL** (`ModuleNotFoundError: trader.automation.discretionary_deployment`).
- [ ] **Step 3: Implement** `discretionary_deployment.py`:

```python
"""The operator's discretionary deployment (SP2 spec 6.6): a sealed scope rule plus an attestation.

The rule may only narrow the spec default (Plan 3 ruling 2). Sealing and reads live in
``AiDeploymentStore``; this module owns the record, its checks and its digest.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import math
import re
from dataclasses import dataclass
from typing import Any, Callable

from trader.automation.ai_deployments import DISCRETIONARY_KIND, DeploymentRefused
from trader.automation.ai_paper_config import SUPPORTED_STYLES
from trader.research.canonical import canonical_json_bytes

ALLOWED_EXCHANGES = frozenset({"NYSE", "NASDAQ", "ARCA"})
ALLOWED_STOCK_TYPES = frozenset({"COMMON", "ETF"})
PRICE_FLOOR = 5.0
DOLLAR_VOLUME_FLOOR = 20_000_000.0
ORDER_SHARE_CEILING = 0.01
VOLUME_SESSIONS = 20
_DIGEST_DOMAIN = b"mmr.ai-discretionary-deployment.v1\x00"
_OPERATOR = re.compile(r"^[A-Za-z0-9_.@-]{1,64}$")
_KEYS = frozenset({"kind", "style", "scope_rule", "attestation"})


def _invalid(name: str, rule: str) -> DeploymentRefused:
    return DeploymentRefused("DEPLOYMENT_INVALID", f"{name} {rule}")


def _exact_keys(name: str, value: Any, keys: frozenset) -> dict:
    if not isinstance(value, dict) or set(value) != keys:
        raise _invalid(name, f"must have exactly the keys {sorted(keys)}")
    return value


def _subset(name: str, value: Any, allowed: frozenset) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not value or not all(isinstance(v, str) for v in value):
        raise _invalid(name, "must be a non-empty list of strings")
    if len(set(value)) != len(value) or not set(value) <= allowed:
        raise _invalid(name, f"must name distinct values from {sorted(allowed)}")
    return tuple(sorted(value))


def _bounded(name: str, value: Any, *, low: float, high: float = math.inf, open_low: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise _invalid(name, "must be a finite number")
    if (value <= low if open_low else value < low) or value > high:
        raise _invalid(name, f"must be in {'(' if open_low else '['}{low}, {high}]")
    return float(value)


@dataclass(frozen=True)
class DiscretionaryScopeRule:
    primary_exchanges: tuple[str, ...]
    stock_types: tuple[str, ...]
    min_price: float
    min_median_dollar_volume: float
    max_order_share_of_dollar_volume: float

    def __post_init__(self):
        checks: dict[str, Callable[[str, Any], Any]] = {
            "primary_exchanges": lambda n, v: _subset(n, v, ALLOWED_EXCHANGES),
            "stock_types": lambda n, v: _subset(n, v, ALLOWED_STOCK_TYPES),
            "min_price": lambda n, v: _bounded(n, v, low=PRICE_FLOOR),
            "min_median_dollar_volume": lambda n, v: _bounded(n, v, low=DOLLAR_VOLUME_FLOOR),
            "max_order_share_of_dollar_volume": lambda n, v: _bounded(n, v, low=0.0, high=ORDER_SHARE_CEILING,
                                                                      open_low=True),
        }
        for name, check in checks.items():
            object.__setattr__(self, name, check(name, getattr(self, name)))

    @classmethod
    def from_json(cls, value: Any) -> "DiscretionaryScopeRule":
        return cls(**_exact_keys("scope_rule", value, frozenset(cls.__dataclass_fields__)))

    def to_json(self) -> dict:
        return {"primary_exchanges": list(self.primary_exchanges), "stock_types": list(self.stock_types),
                "min_price": self.min_price, "min_median_dollar_volume": self.min_median_dollar_volume,
                "max_order_share_of_dollar_volume": self.max_order_share_of_dollar_volume}


DEFAULT_SCOPE_RULE = DiscretionaryScopeRule(("ARCA", "NASDAQ", "NYSE"), ("COMMON", "ETF"), PRICE_FLOOR,
                                            DOLLAR_VOLUME_FLOOR, ORDER_SHARE_CEILING)


@dataclass(frozen=True)
class OperatorAttestation:
    operator: str
    statement: str
    attested_at: str

    def __post_init__(self):
        if not isinstance(self.operator, str) or not _OPERATOR.match(self.operator):
            raise _invalid("attestation.operator", f"must match {_OPERATOR.pattern}")
        if (not isinstance(self.statement, str) or not 1 <= len(self.statement) <= 500
                or not self.statement.isprintable()):
            raise _invalid("attestation.statement", "must be 1-500 printable characters")
        try:
            aware = dt.datetime.fromisoformat(self.attested_at).utcoffset() is not None
        except (TypeError, ValueError):
            aware = False
        if not aware:
            raise _invalid("attestation.attested_at", "must be ISO-8601 with a UTC offset")

    def to_json(self) -> dict:
        return {"operator": self.operator, "statement": self.statement, "attested_at": self.attested_at}


@dataclass(frozen=True)
class DiscretionaryDeployment:
    style: str
    scope_rule: DiscretionaryScopeRule
    attestation: OperatorAttestation
    kind: str = DISCRETIONARY_KIND

    def __post_init__(self):
        if self.kind != DISCRETIONARY_KIND:
            raise _invalid("kind", f"must be {DISCRETIONARY_KIND!r}")
        if self.style not in SUPPORTED_STYLES:
            raise _invalid("style", f"must be one of {sorted(SUPPORTED_STYLES)}")
        if not isinstance(self.scope_rule, DiscretionaryScopeRule) or not isinstance(self.attestation,
                                                                                     OperatorAttestation):
            raise _invalid("deployment", "needs a scope rule and an attestation")

    @classmethod
    def from_json(cls, value: Any) -> "DiscretionaryDeployment":
        body = _exact_keys("deployment", value, _KEYS)
        attestation = _exact_keys("attestation", body["attestation"], frozenset({"operator", "statement",
                                                                                 "attested_at"}))
        return cls(style=body["style"], scope_rule=DiscretionaryScopeRule.from_json(body["scope_rule"]),
                   attestation=OperatorAttestation(**attestation), kind=body["kind"])

    def to_json(self) -> dict:
        return {"kind": self.kind, "style": self.style, "scope_rule": self.scope_rule.to_json(),
                "attestation": self.attestation.to_json()}


def discretionary_digest(deployment: DiscretionaryDeployment) -> str:
    return "sha256:" + hashlib.sha256(_DIGEST_DOMAIN + canonical_json_bytes(deployment.to_json())).hexdigest()
```

In `ai_deployments.py`: add `kind VARCHAR NOT NULL` to the CREATE; constants `STRATEGY_KIND = "strategy"`, `DISCRETIONARY_KIND = "discretionary"`, `OPERATOR_ATTESTED = "OPERATOR_ATTESTED"`; one shared insert:

```python
    def _seal(self, digest: str, record: dict, kind: str, provenance: str, principal: str, command_id: str):
        record_json = canonical_json_bytes(record).decode("utf-8")
        now = self._now()

        def write(conn) -> bool:
            if conn.execute("SELECT 1 FROM ai_deployments WHERE digest = ?", [digest]).fetchone():
                return False
            conn.execute("INSERT INTO ai_deployments (digest, record_json, principal, command_id, sealed_at, "
                         "strategy_digest_provenance, kind) VALUES (?, ?, ?, ?, ?, ?, ?)",
                         [digest, record_json, principal, command_id, now, provenance, kind])
            return True
        return digest, self._db.transaction(write)

    def register_discretionary(self, deployment, *, principal: str, command_id: str) -> tuple[str, bool]:
        from trader.automation.discretionary_deployment import DiscretionaryDeployment, discretionary_digest
        if not isinstance(deployment, DiscretionaryDeployment):
            raise DeploymentRefused("DEPLOYMENT_INVALID", "deployment must be DiscretionaryDeployment")
        return self._seal(discretionary_digest(deployment), deployment.to_json(), DISCRETIONARY_KIND,
                          OPERATOR_ATTESTED, principal, command_id)

    def get_sealed_any(self, digest: str):
        from trader.automation.discretionary_deployment import DiscretionaryDeployment, discretionary_digest
        kind, raw = self._row(digest, "kind, record_json")
        parsers = {STRATEGY_KIND: (AiDeployment.from_json, deployment_digest),
                   DISCRETIONARY_KIND: (DiscretionaryDeployment.from_json, discretionary_digest)}
        if kind not in parsers:
            raise DeploymentRefused("DEPLOYMENT_TAMPERED", f"stored kind {kind!r} is unknown")
        parse, digest_of = parsers[kind]
        try:
            deployment = parse(json.loads(raw))
        except Exception:
            raise DeploymentRefused("DEPLOYMENT_TAMPERED", "stored record does not parse") from None
        if not hmac.compare_digest(digest_of(deployment), digest):
            raise DeploymentRefused("DEPLOYMENT_TAMPERED", "stored record does not match its digest")
        return deployment

    def get_sealed(self, digest: str) -> AiDeployment:
        deployment = self.get_sealed_any(digest)
        if not isinstance(deployment, AiDeployment):
            raise DeploymentRefused("DEPLOYMENT_KIND_MISMATCH", "this digest names a discretionary deployment")
        return deployment

    def kind_of(self, digest: str) -> str:
        return self._row(digest, "kind")[0]
```

`register` keeps its checks and ends with `return self._seal(digest, deployment.to_json(), STRATEGY_KIND, STRATEGY_DIGEST_PROVENANCE, principal, command_id)`.

- [ ] **Step 4: Run** the new file, `tests/automation/test_ai_deployments.py`, `tests/automation/test_ai_paper_decision.py` → PASS.
- [ ] **Step 5: Commit** — `feat: seal operator discretionary deployments next to strategy deployments`.

---

### Task 4: `register_discretionary_deployment` (cli only), readback and CLI

**Files:**
- Modify: `trader/automation/ai_paper_actions.py` (`OPERATOR = "cli"`, `REGISTER_DISCRETIONARY_ACTION`, `AiPaperActions(..., account_mode: str)`, `register_discretionary`, `deployment_view` adds `kind`)
- Modify: `trader/trading/command_stack.py` (`_build_ai_paper_services` passes `account_mode` to `AiPaperActions`)
- Modify: `trader/messaging/production_api.py` (`RegisterDiscretionaryDeploymentRequest`, `_register_discretionary_rpc_handler`, registration in `register_ai_paper_authority`)
- Modify: `trader/messaging/principals.py` (`("command", "register_discretionary_deployment"): frozenset({"cli"})`)
- Modify: `trader/sdk.py` (`register_discretionary_deployment`, `ai_deployment`), `trader/mmr_cli.py` (`ai-deployment register-discretionary | show`)
- Test: `tests/test_ai_paper_rpc.py`, `tests/test_rpc_acl.py` (the pinned table gains the row), `tests/test_mmr_cli_ai_deployment.py` (new)

**Interfaces:**
- Produces: action `register_discretionary_deployment` returning `{"digest", "created", "kind"}`; `get_ai_deployment` → `{"digest", "kind", "deployment", "strategy_digest_provenance", "error_code"}`; `MMRHelpers.register_discretionary_deployment(*, operator: str, statement: str, rule: Optional[dict] = None, style: str = "intraday_long") -> SuccessFail`; `MMRHelpers.ai_deployment(digest: str) -> dict`.

- [ ] **Step 1: Write the failing tests** (in `tests/test_ai_paper_rpc.py`):

```python
from trader.automation.discretionary_deployment import DEFAULT_SCOPE_RULE


def discretionary_body(**rule):
    return {"kind": "discretionary", "style": "intraday_long", "scope_rule": {**DEFAULT_SCOPE_RULE.to_json(), **rule},
            "attestation": {"operator": "owner", "statement": "paper only; rule as sealed",
                            "attested_at": "2026-07-17T10:00:00-04:00"}}


def register_discretionary(served, **rule):
    return command(served, "cli").call("register_discretionary_deployment",
                                       {"deployment": discretionary_body(**rule)}, dict)


def test_operator_registers_and_everyone_reads_the_discretionary_label(served):
    receipt = register_discretionary(served)
    assert receipt["state"] == "RESOLVED" and receipt["outcome"]["kind"] == "discretionary"
    view = query(served, "ai_supervisor").call("get_ai_deployment", {"digest": receipt["outcome"]["digest"]}, dict)
    assert (view["kind"], view["strategy_digest_provenance"]) == ("discretionary", "OPERATOR_ATTESTED")
    assert view["deployment"]["scope_rule"] == DEFAULT_SCOPE_RULE.to_json()
    assert query(served, "ai_research").call("get_ai_deployment", {"digest": register(served)}, dict)["kind"] == "strategy"


def test_registering_twice_replays_one_command(served):
    first, again = register_discretionary(served), register_discretionary(served)
    assert again["command_id"] == first["command_id"] and again["outcome"] == first["outcome"]


@pytest.mark.parametrize("principal", ["ai_supervisor", "ai_research", "dashboard", "strategy"])
def test_only_the_cli_registers_a_discretionary_deployment(served, principal):
    with pytest.raises(TypedRpcRemoteError) as exc:
        command(served, principal).call("register_discretionary_deployment",
                                        {"deployment": discretionary_body()}, dict)
    assert exc.value.code == "PERMISSION_DENIED"


def test_the_service_refuses_a_bypass_of_the_allow_list(served):
    receipt = served.coordinator.execute(CommandRequest(
        command_id="bypass-d", action="register_discretionary_deployment", account_id=ACCOUNT,
        target_type="ai_deployment", target_id="x", expected_version=None, body=discretionary_body(),
        source="ai_research", principal="ai_research"))
    assert receipt.error_code == "PRINCIPAL_FORBIDDEN"


@pytest.mark.parametrize("method,principal", [("register_discretionary_deployment", "cli"),
                                              ("register_ai_deployment", "ai_research")])
def test_a_wider_or_foreign_body_is_refused_on_the_wire(served, method, principal):
    body = discretionary_body(stock_types=["COMMON", "WARRANT"])
    with pytest.raises(TypedRpcRemoteError) as exc:
        command(served, principal).call(method, {"deployment": body}, dict)
    assert exc.value.code == "VALIDATION_ERROR"


def test_a_live_account_never_registers(served):
    actions = replace_account_mode(served.stack.ai_paper.actions, "live")      # helper: copy.copy + _account_mode
    request = CommandRequest(command_id="d-live", action="register_discretionary_deployment", account_id=ACCOUNT,
                             target_type="ai_deployment", target_id="x", expected_version=None,
                             body=discretionary_body(), source="cli", principal="cli")
    with pytest.raises(CommandValidationError) as exc:
        actions.register_discretionary(request)
    assert exc.value.code == "ACCOUNT_NOT_PAPER"
```

`replace_account_mode(actions, mode)` is a three-line test helper (`clone = copy.copy(actions); clone._account_mode = mode; return clone`). `tests/test_mmr_cli_ai_deployment.py` parses `ai-deployment register-discretionary --operator owner --statement "x" --stock-types ETF` with a fake SDK and asserts the call `register_discretionary_deployment(operator="owner", statement="x", rule={...stock_types: ["ETF"]...})`, and that `show` prints `DISCRETIONARY (operator attested, no backtest evidence)`.

- [ ] **Step 2: Run, expect FAIL** (`METHOD_NOT_ALLOWED`, missing `kind`).
- [ ] **Step 3: Implement.** In `ai_paper_actions.py`:

```python
OPERATOR = "cli"
REGISTER_DISCRETIONARY_ACTION = "register_discretionary_deployment"

    def register_discretionary(self, cmd: CommandRequest) -> dict:
        if cmd.principal != OPERATOR:
            raise CommandValidationError("PRINCIPAL_FORBIDDEN", "only the cli operator registers a discretionary deployment")
        if self._account_mode != "paper" or not str(self._account_id).startswith("DU"):
            raise CommandValidationError("ACCOUNT_NOT_PAPER", "discretionary deployments are paper only")
        try:
            deployment = DiscretionaryDeployment.from_json(cmd.body)
        except DeploymentRefused as ex:
            raise CommandValidationError(ex.code, ex.message) from None
        if deployment.style not in self._config.styles:
            raise CommandValidationError("STYLE_NOT_ENABLED", f"style {deployment.style!r} is not enabled")
        digest, created = self._deployments.register_discretionary(deployment, principal=cmd.principal,
                                                                   command_id=cmd.command_id)
        return {"digest": digest, "created": created, "kind": DISCRETIONARY_KIND}

    def deployment_view(self, digest: str) -> dict:
        try:
            deployment = self._deployments.get_sealed_any(digest)
        except DeploymentRefused as ex:
            return {"digest": digest, "kind": None, "deployment": None, "strategy_digest_provenance": None,
                    "error_code": ex.code}
        return {"digest": digest, "kind": self._deployments.kind_of(digest), "deployment": deployment.to_json(),
                "strategy_digest_provenance": self._deployments.provenance(digest), "error_code": None}
```

In `production_api.py` a strict `RegisterDiscretionaryDeploymentRequest(deployment: dict)` whose validator calls `DiscretionaryDeployment.from_json` (a `DeploymentRefused` becomes `ValueError` → `VALIDATION_ERROR`), and a handler that mirrors `_register_ai_deployment_rpc_handler` with `discretionary_digest`, `deployment_command_id(digest)` and `REGISTER_DISCRETIONARY_ACTION`. In `register_ai_paper_authority`: `coordinator.register_action(REGISTER_DISCRETIONARY_ACTION, ai_paper.actions.register_discretionary, requires_preflight=False)` and `registry.register("command", "register_discretionary_deployment", RegisterDiscretionaryDeploymentRequest, dict, handler, with_caller=True)`. SDK: `register_discretionary_deployment` builds the body from `DEFAULT_SCOPE_RULE.to_json()` updated by `rule`, sets `attested_at = datetime.now().astimezone().isoformat(timespec="seconds")`, calls `self._typed_command.call(..., CommandReceipt)` and maps the receipt like `_experiment_command`. CLI: subcommand `ai-deployment` with `register-discretionary` (`--operator`, `--statement` required; `--exchanges`, `--stock-types` comma lists; `--min-price`, `--min-dollar-volume`, `--max-order-share` floats) and `show DIGEST`; `show` prints the label from ruling 16 when `kind == "discretionary"`.
- [ ] **Step 4: Run** `tests/test_ai_paper_rpc.py tests/test_rpc_acl.py tests/test_mmr_cli_ai_deployment.py` → PASS.
- [ ] **Step 5: Commit** — `feat: let the operator register a discretionary deployment over signed rpc`.

---

### Task 5: Scope evidence — exact IB contract details and the 20-session dollar volume

**Files:**
- Create: `trader/automation/scope_evidence.py`
- Modify: `trader/automation/production_evidence.py` (extract `latest_closed_sessions`, `TwentySessionVolume`, `twenty_sessions_from_frame`, `local_twenty_sessions`, `liquidity_from_sessions`; `liquidity_from_history` delegates, behaviour unchanged)
- Modify: `trader/messaging/production_api.py` (`_INSTRUMENTS_UNIVERSE` imports `INSTRUMENTS_UNIVERSE`)
- Test: `tests/automation/test_scope_evidence.py`; existing `tests/automation/test_production_evidence.py`, `tests/automation/test_ai_paper_evidence.py`

**Interfaces:**
- Produces (`production_evidence.py`): `latest_closed_sessions(now, count=20) -> tuple[date, ...]`; `@dataclass(frozen=True) TwentySessionVolume(conid: int, sessions: tuple[date, ...], closes: tuple[float, ...], volumes: tuple[float, ...], source: str)` with `median_dollar_volume`, `adv_shares`, `is_current(now) -> bool`, `to_json()`; `twenty_sessions_from_frame(frame, conid, expected, source) -> TwentySessionVolume` (raises `ApprovalContextError("HISTORY_INVALID", ...)`); `local_twenty_sessions(history, conid, now)`; `liquidity_from_sessions(volume, quote) -> LiquidityEvidence`.
- Produces (`scope_evidence.py`): `INSTRUMENTS_UNIVERSE = "_instruments"`, `CONTRACT_DETAILS_TIMEOUT_SECONDS = 5.0`, `ScopeEvidenceUnavailable(reason)`, `ContractEvidence(conid, symbol, sec_type, currency, primary_exchange, stock_type, fetched_at)`, `SymbolResolution(symbol, status, conid, contract, reason="")`, `IbContractEvidenceSource(*, request_details: Callable[[Contract], list], remember: Callable[[Any], None], now)` with `by_conid(conid) -> ContractEvidence` and `by_symbol(symbol) -> SymbolResolution`; `DollarVolumeSource(*, history, alpaca_history: Callable[[], Any], now)` with `twenty_sessions(conid, symbol) -> TwentySessionVolume` and `cached(conid) -> Optional[TwentySessionVolume]`.

- [ ] **Step 1: Write the failing tests:**

```python
def details(conid=CONID, symbol="AAPL", primary="NASDAQ", stock_type="COMMON", sec_type="STK"):
    return SimpleNamespace(contract=SimpleNamespace(conId=conid, symbol=symbol, secType=sec_type, currency="USD",
                                                    primaryExchange=primary), stockType=stock_type)


def source(rows, remembered=None):
    calls = []
    def request(contract):
        calls.append(contract)
        if isinstance(rows, Exception):
            raise rows
        return rows
    src = IbContractEvidenceSource(request_details=request, remember=(remembered or []).append, now=lambda: NOW)
    return src, calls


def test_by_conid_needs_exactly_one_matching_detail():
    remembered = []
    src, _ = source([details(), details(conid=CONID + 1)], remembered)
    assert src.by_conid(CONID).primary_exchange == "NASDAQ" and len(remembered) == 1
    for rows in ([], [details(), details()]):
        with pytest.raises(ScopeEvidenceUnavailable):
            source(rows)[0].by_conid(CONID)


def test_an_ib_error_names_only_its_type():
    with pytest.raises(ScopeEvidenceUnavailable) as exc:
        source(TimeoutError("token=abc"))[0].by_conid(CONID)
    assert "TimeoutError" in exc.value.reason and "abc" not in exc.value.reason


@pytest.mark.parametrize("rows,status", [
    ([details(), details(conid=1, symbol="AAPLW")], "RESOLVED"), ([], "NOT_FOUND"),
    ([details(), details(conid=2, primary="BATS")], "AMBIGUOUS")])
def test_by_symbol_is_exact(rows, status):
    assert source(rows)[0].by_symbol("AAPL").status == status


def test_class_shares_are_not_looked_up():
    src, calls = source([details()])
    assert src.by_symbol("BRK.B").status == "SYMBOL_FORM_UNSUPPORTED" and calls == []


def test_volume_prefers_local_bars_then_alpaca_and_caches_per_session(tmp_path):
    clock = Clock()
    alpaca = FakeAlpacaHistory(lambda: daily_frame(clock()))        # counts get_history calls
    volumes = DollarVolumeSource(history=make_history(str(tmp_path / "h.duckdb")), alpaca_history=lambda: alpaca,
                                 now=clock)
    assert volumes.twenty_sessions(CONID, "AAPL").source == "local_daily_bars"
    unknown = volumes.twenty_sessions(4242, "XYZ")
    assert unknown.source == "alpaca_daily_bars" and unknown.median_dollar_volume == 100_000_000.0
    assert volumes.twenty_sessions(4242, "XYZ") is unknown and alpaca.calls == 1
    clock.advance(days=3)                                            # a new session closed: stale
    volumes.twenty_sessions(4242, "XYZ")
    assert alpaca.calls == 2


def test_a_missing_session_anywhere_is_unavailable(tmp_path):
    alpaca = FakeAlpacaHistory(lambda: daily_frame().iloc[1:])
    volumes = DollarVolumeSource(history=make_history(str(tmp_path / "h.duckdb")), alpaca_history=lambda: alpaca,
                                 now=Clock())
    with pytest.raises(ScopeEvidenceUnavailable) as exc:
        volumes.twenty_sessions(4242, "XYZ")
    assert "local" in exc.value.reason and "alpaca" in exc.value.reason


def test_liquidity_from_history_is_unchanged(tmp_path):
    history = make_history(str(tmp_path / "h.duckdb"))
    old = liquidity_from_history(history, CONID, quote(), NOW)
    assert old == liquidity_from_sessions(local_twenty_sessions(history, CONID, NOW), quote())
```

`FakeAlpacaHistory(build)` has `calls` and `get_history(ticker, bar_size, start, end)` returning `build()` (so a later clock gets a later window); `Clock`, `daily_frame`, `make_history`, `quote` come from the ai_paper test helpers.
- [ ] **Step 2: Run, expect FAIL** (`ModuleNotFoundError`).
- [ ] **Step 3: Implement.** In `production_evidence.py`, split `liquidity_from_history` without changing its codes or order (depth first, then the history read):

```python
def latest_closed_sessions(now: dt.datetime, count: int = 20) -> tuple[dt.date, ...]:
    calendar = xcals.get_calendar("XNYS")
    today = now.astimezone(ET).date()
    sessions = calendar.sessions_in_range(today - dt.timedelta(days=90), today)
    return tuple(day.date() for day in sessions if calendar.session_close(day) < pd.Timestamp(now))[-count:]


@dataclass(frozen=True)
class TwentySessionVolume:
    conid: int
    sessions: tuple[dt.date, ...]
    closes: tuple[float, ...]
    volumes: tuple[float, ...]
    source: str

    @property
    def median_dollar_volume(self) -> float:
        return float(statistics.median(c * v for c, v in zip(self.closes, self.volumes)))

    @property
    def adv_shares(self) -> float:
        return sum(self.volumes) / len(self.volumes)

    def is_current(self, now: dt.datetime) -> bool:
        return self.sessions == latest_closed_sessions(now, len(self.sessions))

    def to_json(self) -> dict:
        return {"source": self.source, "first_session": self.sessions[0].isoformat(),
                "last_session": self.sessions[-1].isoformat(), "median_dollar_volume": self.median_dollar_volume}
```

`twenty_sessions_from_frame` holds today's frame checks (columns, tz-aware index, `bar_size == "1 day"`, exactly the expected 20 dates, TRADES provenance, positive closes and volumes); `local_twenty_sessions` wraps the `TickStorage` read in `HISTORY_UNAVAILABLE` as today; `liquidity_from_sessions` builds `LiquidityEvidence` from `volume.median_dollar_volume`, `volume.adv_shares` and the quote (spread, `ask_size` depth via the existing `DEPTH_INVALID` check). In `scope_evidence.py`:

```python
class IbContractEvidenceSource:
    """Ruling 3: exact IB contract details; each definition found is remembered in the trader universe."""

    def __init__(self, *, request_details: Callable[[Any], list], remember: Callable[[Any], None],
                 now: Callable[[], dt.datetime]):
        self._request_details, self._remember, self._now = request_details, remember, now

    def by_conid(self, conid: int) -> ContractEvidence:
        from ib_async import Contract
        rows = [d for d in self._request(Contract(conId=conid))
                if d.contract is not None and type(d.contract.conId) is int and d.contract.conId == conid]
        if len(rows) != 1:
            raise ScopeEvidenceUnavailable(f"IB returned {len(rows)} contract details for conid {conid}")
        return self._evidence(rows[0])

    def by_symbol(self, symbol: str) -> SymbolResolution:
        from ib_async import Contract
        if not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol):
            return SymbolResolution(symbol, "SYMBOL_FORM_UNSUPPORTED", None, None, "only ^[A-Z]{1,5}$ is looked up")
        rows = [d for d in self._request(Contract(symbol=symbol, secType="STK", exchange="SMART", currency="USD"))
                if d.contract is not None and d.contract.symbol == symbol]
        conids = {int(d.contract.conId) for d in rows}
        if not conids:
            return SymbolResolution(symbol, "NOT_FOUND", None, None, "IB has no USD stock with this symbol")
        if len(conids) > 1:
            return SymbolResolution(symbol, "AMBIGUOUS", None, None, f"IB returned conids {sorted(conids)}")
        evidence = self._evidence(rows[0])
        return SymbolResolution(symbol, "RESOLVED", evidence.conid, evidence)

    def _request(self, contract) -> list:
        try:
            return list(self._request_details(contract) or [])
        except Exception as ex:
            # Provider text is opaque and may carry credentials: the type name only.
            raise ScopeEvidenceUnavailable(f"IB contract details failed: {type(ex).__name__}") from None

    def _evidence(self, details) -> ContractEvidence:
        try:
            self._remember(details)
        except Exception as ex:
            logger.warning("could not remember conid %s in %s: %s", details.contract.conId, INSTRUMENTS_UNIVERSE,
                           type(ex).__name__)
        c = details.contract
        return ContractEvidence(conid=int(c.conId), symbol=str(c.symbol or ""), sec_type=str(c.secType or ""),
                                currency=str(c.currency or ""), primary_exchange=str(c.primaryExchange or ""),
                                stock_type=str(getattr(details, "stockType", "") or ""), fetched_at=self._now())


class DollarVolumeSource:
    """Ruling 5: local daily bars, else Alpaca SIP daily bars held in memory (price history is not written)."""

    def __init__(self, *, history: Any, alpaca_history: Callable[[], Any], now: Callable[[], dt.datetime]):
        self._history, self._alpaca_history, self._now = history, alpaca_history, now
        self._cache: dict[int, TwentySessionVolume] = {}
        self._lock = threading.Lock()

    def cached(self, conid: int) -> Optional[TwentySessionVolume]:
        with self._lock:
            found = self._cache.get(conid)
        return found if found is not None and found.is_current(self._now()) else None

    def twenty_sessions(self, conid: int, symbol: str) -> TwentySessionVolume:
        found = self.cached(conid)
        if found is not None:
            return found
        now = self._now()
        problems: list[str] = []
        try:
            volume = local_twenty_sessions(self._history, conid, now)
        except ApprovalContextError as ex:
            problems.append(f"local: {ex.code}")
            volume = self._from_alpaca(conid, symbol, latest_closed_sessions(now), problems)
        with self._lock:
            self._cache[conid] = volume
        return volume

    def _from_alpaca(self, conid, symbol, expected, problems) -> TwentySessionVolume:
        try:
            frame = self._alpaca_history().get_history(
                symbol, BarSize.Days1, dt.datetime.combine(expected[0], dt.time(), ET),
                dt.datetime.combine(expected[-1], dt.time(), ET))
            return twenty_sessions_from_frame(frame, conid, expected, "alpaca_daily_bars")
        except ApprovalContextError as ex:
            problems.append(f"alpaca: {ex.code}")
        except Exception as ex:
            problems.append(f"alpaca: {type(ex).__name__}")
        raise ScopeEvidenceUnavailable("20-session dollar volume unavailable (" + "; ".join(problems) + ")")
```

- [ ] **Step 4: Run** the new file, `tests/automation/test_production_evidence.py`, `tests/automation/test_ai_paper_evidence.py` → PASS.
- [ ] **Step 5: Commit** — `feat: add exact ib contract and twenty-session volume evidence for the scope rule`.

---

### Task 6: The scope rule — pure evaluation and the check table (migration 100)

**Files:**
- Create: `trader/automation/discretionary_scope.py` (first part)
- Modify: `trader/data/schema_migrations.py` (docstring: "SP2 Plan 3 uses **100** (discretionary_scope_checks).")
- Test: `tests/automation/test_discretionary_scope.py`

**Interfaces:**
- Produces: `OUT_OF_DISCRETIONARY_SCOPE`, `SCOPE_PARTS`, `SCOPE_CHECK_MIGRATION_VERSION = 100`, `apply_scope_check_migration(migrator) -> bool`; `ScopeInputs(contract, quote, volume, order_notional: Optional[float], filter_refusal, missing: tuple[str, ...] = ())`; `ScopeVerdict(part: Optional[str], reason: str, evidence: Mapping)` with `passed`; `static_scope_refusal(rule, contract) -> Optional[tuple[str, str]]`; `effective_dollar_volume_floor(rule) -> float`; `quote_problem(quote, now) -> Optional[str]`; `evaluate_scope(rule, inputs, now) -> ScopeVerdict`; `trading_filter_refusal(load_filter) -> Callable[[ContractEvidence, float], Optional[str]]`; `ScopeCheckStore(db, now)` with `record(*, command_id, phase, deployment_digest, conid, verdict) -> dict` and `detail(command_id, phase) -> Optional[dict]`.

- [ ] **Step 1: Write the failing tests** (`inputs(**changes)` builds a passing `ScopeInputs`: NASDAQ COMMON contract fetched at `NOW`, `quote()` with bid 99.95, a current 20-session volume of $100M median, `order_notional=50_000.0`, a filter that allows all; `contract(**fields)`, `quote(**fields)` and `volume(median=100e6, sessions_ending_days_ago=0)` are local builders, the last one ending its 20 sessions that many calendar days before `latest_closed_sessions(NOW)[-1]`):

```python
def test_a_good_instrument_passes():
    assert evaluate_scope(DEFAULT_SCOPE_RULE, inputs(), NOW).passed


@pytest.mark.parametrize("changes,part", [
    ({"contract": contract(primary_exchange="AMEX")}, "exchange"),
    ({"contract": contract(primary_exchange="SMART")}, "exchange"),
    ({"contract": contract(primary_exchange="PINK")}, "exchange"),
    ({"contract": contract(sec_type="WAR")}, "instrument_type"),
    ({"contract": contract(stock_type="RIGHT")}, "instrument_type"),
    ({"contract": contract(stock_type="UNIT")}, "instrument_type"),
    ({"contract": contract(stock_type="ADR")}, "instrument_type"),
    ({"quote": quote(bid=4.99, ask=5.0)}, "price"),
    ({"volume": volume(median=19_000_000.0)}, "dollar_volume"),
    ({"volume": volume(median=30_000_000.0)}, "dollar_volume"),        # above the rule, below SP1's floor
    ({"order_notional": 1_000_001.0}, "liquidity"),                    # 1% of $100M is $1,000,000
    ({"filter_refusal": lambda c, p: "symbol AAPL is denied"}, "trading_filter"),
    ({"contract": None, "missing": ("IB timeout",)}, "evidence_stale"),
    ({"quote": quote(age=6.0)}, "evidence_stale"),
    ({"quote": quote(feed="delayed")}, "evidence_stale"),
    ({"volume": None, "missing": ("no bars",)}, "evidence_stale"),
    ({"volume": volume(sessions_ending_days_ago=3)}, "evidence_stale"),
])
def test_each_part(changes, part):
    verdict = evaluate_scope(DEFAULT_SCOPE_RULE, inputs(**changes), NOW)
    assert (verdict.passed, verdict.part) == (False, part)


def test_blank_stock_type_is_instrument_type():                       # review focus 1
    verdict = evaluate_scope(DEFAULT_SCOPE_RULE, inputs(contract=contract(stock_type="")), NOW)
    assert verdict.part == "instrument_type" and "(blank)" in verdict.reason


def test_the_dollar_volume_reason_names_both_floors():
    reason = evaluate_scope(DEFAULT_SCOPE_RULE, inputs(volume=volume(median=30_000_000.0)), NOW).reason
    assert "20,000,000" in reason and "50,000,000" in reason


def test_exactly_one_percent_passes():
    assert evaluate_scope(DEFAULT_SCOPE_RULE, inputs(order_notional=1_000_000.0), NOW).passed


def test_a_raising_filter_is_a_trading_filter_refusal():
    def broken(contract, price):
        raise OSError("unreadable")
    assert evaluate_scope(DEFAULT_SCOPE_RULE, inputs(filter_refusal=broken), NOW).part == "trading_filter"


def test_filter_uses_the_ib_identity(tmp_path):
    path = tmp_path / "trading_filters.yaml"
    path.write_text('denylist: ["AAPL"]\n')
    refusal = trading_filter_refusal(MtimeCachedFilterLoader(str(path)))
    assert refusal(contract(), 100.0) and refusal(contract(symbol="MSFT"), 100.0) is None


def test_checks_are_recorded_once_per_phase(db):
    store = ScopeCheckStore(db, now=lambda: NOW)
    verdict = evaluate_scope(DEFAULT_SCOPE_RULE, inputs(quote=quote(bid=4.0, ask=4.1)), NOW)
    detail = store.record(command_id="aip-d1", phase="admission", deployment_digest=DIGEST, conid=CONID,
                          verdict=verdict)
    assert detail == {"part": "price", "reason": verdict.reason, "phase": "admission",
                      "check_id": "admission:aip-d1"}
    store.record(command_id="aip-d1", phase="admission", deployment_digest=DIGEST, conid=CONID,
                 verdict=evaluate_scope(DEFAULT_SCOPE_RULE, inputs(), NOW))
    assert store.detail("aip-d1", "admission")["part"] == "price"          # a replay keeps the first verdict
    assert db.execute("SELECT version FROM schema_migrations WHERE version = 100", fetch="one")
```

- [ ] **Step 2: Run, expect FAIL** (`ModuleNotFoundError`).
- [ ] **Step 3: Implement:**

```python
OUT_OF_DISCRETIONARY_SCOPE = "OUT_OF_DISCRETIONARY_SCOPE"
SCOPE_PARTS = ("exchange", "instrument_type", "price", "dollar_volume", "liquidity", "trading_filter",
               "evidence_stale")
SCOPE_CHECK_MIGRATION_VERSION = 100
PHASES = ("admission", "sizing", "dispatch")


def apply_scope_check_migration(migrator: SchemaMigrator) -> bool:
    return migrator.apply(SCOPE_CHECK_MIGRATION_VERSION, "sp2_discretionary_scope_checks", (
        """CREATE TABLE IF NOT EXISTS discretionary_scope_checks (
            command_id VARCHAR NOT NULL, phase VARCHAR NOT NULL, deployment_digest VARCHAR NOT NULL,
            conid BIGINT NOT NULL, passed BOOLEAN NOT NULL, part VARCHAR, reason VARCHAR NOT NULL,
            evidence_json VARCHAR NOT NULL, checked_at TIMESTAMPTZ NOT NULL,
            PRIMARY KEY (command_id, phase))""",
    ))


def effective_dollar_volume_floor(rule: DiscretionaryScopeRule) -> float:
    """Ruling 6: SP1's session_risk floor applies to every entry, so the stricter floor is the real one."""
    return max(rule.min_median_dollar_volume, MIN_MEDIAN_DOLLAR_VOLUME)


def static_scope_refusal(rule, contract: ContractEvidence) -> Optional[tuple[str, str]]:
    if contract.primary_exchange not in rule.primary_exchanges:
        return "exchange", f"primary listing {contract.primary_exchange or '(blank)'} is not in {list(rule.primary_exchanges)}"
    if contract.sec_type != "STK" or contract.currency != "USD":
        return "instrument_type", f"{contract.sec_type or '(blank)'}/{contract.currency or '(blank)'} is not a USD stock"
    if contract.stock_type not in rule.stock_types:
        return "instrument_type", f"IB stockType {contract.stock_type or '(blank)'} is not in {list(rule.stock_types)}"
    return None


def quote_problem(quote: Any, now: dt.datetime) -> Optional[str]:
    if quote is None:
        return "no executable IB quote"
    age = (now - quote.market_timestamp).total_seconds()
    if age > MAX_QUOTE_AGE_SECONDS or age < -MAX_SOURCE_CLOCK_SKEW_SECONDS:
        return f"IB quote is {age:.1f} s old"
    if quote.feed_type != "live" or quote.session_state != "continuous":
        return f"IB quote is {quote.feed_type}/{quote.session_state}, not live continuous trading"
    if not (_finite_positive(quote.bid) and _finite_positive(quote.ask)) or quote.ask < quote.bid:
        return "IB quote has no valid bid and ask"
    return None


def evaluate_scope(rule, inputs: ScopeInputs, now: dt.datetime) -> ScopeVerdict:
    evidence = _evidence_json(inputs)

    def refuse(part: str, reason: str) -> ScopeVerdict:
        return ScopeVerdict(part, reason, evidence)

    if inputs.contract is None:
        return refuse("evidence_stale", "; ".join(inputs.missing) or "no IB contract details")
    static = static_scope_refusal(rule, inputs.contract)
    if static is not None:
        return refuse(*static)
    problem = quote_problem(inputs.quote, now)
    if problem is not None:
        return refuse("evidence_stale", problem)
    if inputs.quote.bid < rule.min_price:
        return refuse("price", f"IB bid {inputs.quote.bid} is below {rule.min_price}")
    volume = inputs.volume
    if volume is None or not volume.is_current(now):
        return refuse("evidence_stale", "; ".join(inputs.missing) or "20-session volume is not the latest window")
    median, floor = volume.median_dollar_volume, effective_dollar_volume_floor(rule)
    if median < floor:
        return refuse("dollar_volume", f"20-session median dollar volume {median:,.0f} is below {floor:,.0f} "
                                       f"(rule {rule.min_median_dollar_volume:,.0f}, SP1 {MIN_MEDIAN_DOLLAR_VOLUME:,.0f})")
    cap = rule.max_order_share_of_dollar_volume * median
    if inputs.order_notional is not None and inputs.order_notional > cap:
        return refuse("liquidity", f"order notional {inputs.order_notional:,.2f} is above "
                                   f"{rule.max_order_share_of_dollar_volume:.2%} of {median:,.0f}")
    try:
        denied = inputs.filter_refusal(inputs.contract, float(inputs.quote.ask))
    except Exception as ex:
        denied = f"trading filter unavailable: {type(ex).__name__}"
    if denied:
        return refuse("trading_filter", denied)
    return ScopeVerdict(None, "in scope", evidence)
```

`ScopeCheckStore.record` inserts with `ON CONFLICT (command_id, phase) DO NOTHING`, then returns `detail(command_id, phase)`, which reads the row back as `{"part", "reason", "phase", "check_id": f"{phase}:{command_id}"}`; `phase` must be in `PHASES` (`ValueError` otherwise). `_evidence_json` stores the contract fields, bid/ask/quote time, `volume.to_json()` and the order notional. Register `apply_scope_check_migration` next to `apply_ai_paper_decision_migration` in `build_command_stack` (`# 100`).
- [ ] **Step 4: Run** the new file → PASS.
- [ ] **Step 5: Commit** — `feat: evaluate the discretionary scope rule and record every check`.

---

### Task 7: The scope rule at admission

**Files:**
- Modify: `trader/automation/discretionary_scope.py` (`DiscretionaryScopeEvidence`, `AdmissionScope`, `ScopeRefused`, `DiscretionaryScopeService`)
- Modify: `trader/automation/ai_paper_decision.py` (migration-56 CREATE adds `deployment_kind VARCHAR`; `DecisionRow.deployment_kind`; `_Refusal.detail: Optional[str | dict]`; `attach_scope`; `_deployment`; `_execute_entry`; `deployment_binding(*, digest, allowlist, notional, limits, expires_at)`)
- Modify: `trader/automation/ai_paper_evidence.py` (`prepare_entry(..., volume=None, scope_evidence=None)`)
- Modify: `trader/trading/approval_context.py` (`ApprovalContext.discretionary_scope: Optional[Any] = None`)
- Modify: `trader/trading/command_stack.py` (`_AiPaperParts` gains `deployments`, `scope_checks`, `filter_refusal`; `_build_ai_paper_services` builds the sources and the service and calls `decisions.attach_scope`)
- Create: `tests/automation/discretionary_world.py`; Test: `tests/automation/test_discretionary_admission.py`

**Interfaces:**
- Produces: `DiscretionaryScopeEvidence(deployment_digest, rule, contract, volume)`; `AdmissionScope(evidence, attested_notional: float)`; `ScopeRefused(detail: dict)`; `DiscretionaryScopeService(*, contracts, volumes, quotes, filter_refusal, checks, now)` with `check_admission(*, command_id, digest, deployment, conid) -> AdmissionScope`, `refuse_size(*, command_id, scope, conid, reason) -> dict` and the property `checks`; `AiPaperDecisionService.attach_scope(scope) -> None`.

- [ ] **Step 1: Write the failing tests.** `discretionary_world(tmp_path, *, rule=None, **world_kwargs) -> World` registers a discretionary deployment (`world.ddigest`), applies migration 100, attaches `FakeContracts` (`set(conid, **fields)`, `fail_with(reason)`, `calls`), `FakeVolumes` (`set(volume=...)`, `fail_with(reason)`; 20 current sessions at close 100.0) and a `DiscretionaryScopeService` with `world.quotes` and a filter over `world.filter_file`. `World.Quotes` gains `set(**changes)` (price follows ask) applied in `executable_quote`.

```python
@pytest.fixture
def world(tmp_path):
    return discretionary_world(tmp_path)


def enter(world, **changes):
    return world.submit(deployment_digest=world.ddigest, **changes)


def test_an_in_scope_self_found_entry_is_submitted_and_labelled(world):
    receipt = enter(world)
    assert receipt.state == "SUBMITTED", receipt
    row = world.decisions.row("dec-00000001")
    assert (row.deployment_kind, row.strategy_digest, row.style) == ("discretionary", None, "intraday_long")
    assert world.scope.checks.detail(receipt.command_id, "admission")["part"] is None


@pytest.mark.parametrize("change,part", [
    (lambda w: w.contracts.set(CONID, primary_exchange="AMEX"), "exchange"),
    (lambda w: w.contracts.set(CONID, stock_type="WARRANT"), "instrument_type"),
    pytest.param(lambda w: w.contracts.set(CONID, stock_type=""), "instrument_type", id="blank-type"),
    (lambda w: w.quotes.set(bid=4.98, ask=5.0), "price"),
    (lambda w: w.volumes.set(volume=300_000.0), "dollar_volume"),
    (lambda w: w.filter_file.write(denylist=["AAPL"]), "trading_filter"),
    (lambda w: w.contracts.fail_with("IB contract details failed: TimeoutError"), "evidence_stale"),
    (lambda w: w.volumes.fail_with("no bars"), "evidence_stale"),
    (lambda w: setattr(w.quotes, "age", 6.0), "evidence_stale"),
])
def test_each_failed_part_is_refused_with_its_code(world, change, part):
    change(world)
    receipt = enter(world)
    assert (receipt.state, receipt.error_code) == ("REJECTED", "OUT_OF_DISCRETIONARY_SCOPE")
    detail = receipt.outcome["detail"]
    assert (detail["part"], detail["phase"]) == (part, "admission")
    assert world.decisions.row("dec-00000001").error_code == "OUT_OF_DISCRETIONARY_SCOPE"
    assert world.dispatch.plans == []


def test_liquidity_boundary_is_exact(tmp_path):                     # review focus 3
    assert planned_entry_limit(100.0, 99.95, Decimal("10")) == 100.10      # the constants below rest on it
    world = discretionary_world(tmp_path)
    world.volumes.set(volume=600_000.0)                             # median $60M: cap $600,000 at 1%
    # 5,994 x 100.10 = 599,999.40 is inside the cap; other SP1 limits refuse that size.
    assert enter(world, quantity=5_994).error_code == "QUANTITY_ABOVE_MAXIMUM"
    receipt = enter(world, decision_id="dec-00000002", quantity=5_995)   # 600,099.50
    assert receipt.error_code == "OUT_OF_DISCRETIONARY_SCOPE"
    assert (receipt.outcome["detail"]["part"], receipt.outcome["detail"]["phase"]) == ("liquidity", "sizing")


def test_sizing_never_passes_the_cap(tmp_path):
    world = discretionary_world(tmp_path, rule={"max_order_share_of_dollar_volume": 0.0004})   # $40,000
    receipt = enter(world)
    assert receipt.state == "SUBMITTED"
    assert 0 < receipt.outcome["quantity"] * 100.10 <= 40_000.0


def test_a_strategy_deployment_is_unchanged(world):
    receipt = world.submit()                                        # world.digest: the SP1 strategy deployment
    assert receipt.state == "SUBMITTED" and world.contracts.calls == 0
    assert world.decisions.row("dec-00000001").deployment_kind == "strategy"


def test_without_a_wired_scope_service_a_discretionary_entry_fails_closed(tmp_path):
    world = discretionary_world(tmp_path)
    world.service.attach_scope(None)
    receipt = enter(world)
    assert (receipt.error_code, receipt.outcome["detail"]["part"]) == ("OUT_OF_DISCRETIONARY_SCOPE", "evidence_stale")
```

The sized quantity is the `quantity` that `_start_saga` writes into the SUBMITTED outcome.
- [ ] **Step 2: Run, expect FAIL** (the discretionary digest refuses `DEPLOYMENT_KIND_MISMATCH` through `get_sealed`).
- [ ] **Step 3: Implement.** The service:

```python
class DiscretionaryScopeService:
    """Ruling 8 at admission: fresh IB contract details, a fresh IB quote and the volume source."""

    def __init__(self, *, contracts, volumes, quotes, filter_refusal, checks: ScopeCheckStore, now):
        self._contracts, self._volumes, self._quotes = contracts, volumes, quotes
        self._filter_refusal, self._checks, self._now = filter_refusal, checks, now

    @property
    def checks(self) -> ScopeCheckStore:
        return self._checks

    def check_admission(self, *, command_id: str, digest: str, deployment, conid: int) -> AdmissionScope:
        missing: list[str] = []
        contract = self._fetch(lambda: self._contracts.by_conid(conid), missing)
        volume = None if contract is None else self._fetch(
            lambda: self._volumes.twenty_sessions(conid, contract.symbol), missing)
        quote = self._fetch(lambda: self._quotes.executable_quote(conid, side="BUY"), missing)
        rule = deployment.scope_rule
        verdict = evaluate_scope(rule, ScopeInputs(contract, quote, volume, None, self._filter_refusal,
                                                   tuple(missing)), self._now())
        detail = self._checks.record(command_id=command_id, phase="admission", deployment_digest=digest,
                                     conid=conid, verdict=verdict)
        if not verdict.passed:
            raise ScopeRefused(detail)
        cap = rule.max_order_share_of_dollar_volume * volume.median_dollar_volume
        # Ruling 7: SP1 adds LIVE_NOTIONAL_TOLERANCE to the attested notional; this keeps the bound at the cap.
        return AdmissionScope(DiscretionaryScopeEvidence(digest, rule, contract, volume),
                              cap / (1.0 + LIVE_NOTIONAL_TOLERANCE))

    def refuse_size(self, *, command_id: str, scope: AdmissionScope, conid: int, reason: str) -> dict:
        verdict = ScopeVerdict("liquidity", reason, {"volume": scope.evidence.volume.to_json()})
        return self._checks.record(command_id=command_id, phase="sizing",
                                   deployment_digest=scope.evidence.deployment_digest, conid=conid, verdict=verdict)

    # Order matters: the contract fetch remembers the conid in the universe, which the quote read needs.

    @staticmethod
    def _fetch(read, missing: list[str]):
        try:
            return read()
        except ScopeEvidenceUnavailable as ex:
            missing.append(ex.reason)
        except Exception as ex:
            missing.append(f"evidence read failed: {type(ex).__name__}")
        return None
```

In `AiPaperDecisionService`: `scope: Any = None` constructor argument and `attach_scope(scope)`; `_deployment` uses `get_sealed_any` and branches:

```python
    def _deployment(self, decision: AiPaperDecision, admission: _Admission) -> Any:
        try:
            deployment = self._deployments.get_sealed_any(decision.deployment_digest)
        except DeploymentRefused as ex:
            raise _Refusal(ex.code, detail=ex.message) from None
        if isinstance(deployment, DiscretionaryDeployment):
            admission.row = replace(admission.row, style=deployment.style, deployment_kind=DISCRETIONARY_KIND)
        else:
            admission.row = replace(admission.row, strategy_digest=deployment.strategy_digest,
                                    style=deployment.style, deployment_kind=STRATEGY_KIND)
            if deployment.decider_verdict != "DEPLOY":
                raise _Refusal("DEPLOYMENT_NOT_DEPLOYABLE")
            if decision.conid not in deployment.conids:
                raise _Refusal("CONID_NOT_IN_DEPLOYMENT")
        if deployment.style not in self._config.styles:
            raise _Refusal("STYLE_NOT_ENABLED")
        return deployment
```

`_execute_entry`, from the side check on:

```python
        if decision.side != "BUY":
            raise _Refusal("SIDE_NOT_ENABLED")
        scope = self._admit_scope(cmd, decision, deployment)
        notional = deployment.evidence_order_notional if scope is None else scope.attested_notional
        try:
            prepared = self._evidence.prepare_entry(
                conid=decision.conid, stop_price=float(decision.stop_price), requested_quantity=decision.quantity,
                limits=limits, session=session, notional=notional, experiment_id=experiment.experiment_id,
                volume=None if scope is None else scope.evidence.volume,
                scope_evidence=None if scope is None else scope.evidence)
        except ApprovalContextError as ex:
            if scope is not None and ex.code == "ORDER_EXCEEDS_ATTESTED_NOTIONAL":
                detail = self._scope.refuse_size(command_id=cmd.command_id, scope=scope, conid=decision.conid,
                                                 reason=ex.message)
                raise _Refusal(OUT_OF_DISCRETIONARY_SCOPE, detail=detail) from None
            raise _Refusal(ex.code, detail=ex.message) from None
        self._claim(cmd, admission, require_unpaused=True)
        order = entry_order_for(decision, command_id=cmd.command_id, quantity=prepared.quantity,
                                limits=limits, deployment_digest=decision.deployment_digest)
        allowlist = ((str(decision.conid),) if scope is not None
                     else tuple(str(conid) for conid in deployment.conids))
        binding = deployment_binding(digest=decision.deployment_digest, allowlist=allowlist, notional=notional,
                                     limits=limits, expires_at=decision.expires_at)
        return self._start_saga(cmd, admission, decision, order, binding, prepared)

    def _admit_scope(self, cmd, decision, deployment) -> Optional[AdmissionScope]:
        if not isinstance(deployment, DiscretionaryDeployment):
            return None
        if self._scope is None:
            raise _Refusal(OUT_OF_DISCRETIONARY_SCOPE, detail={
                "part": "evidence_stale", "reason": "the scope service is not wired", "phase": "admission",
                "check_id": None})
        try:
            return self._scope.check_admission(command_id=cmd.command_id, digest=decision.deployment_digest,
                                               deployment=deployment, conid=decision.conid)
        except ScopeRefused as refused:
            raise _Refusal(OUT_OF_DISCRETIONARY_SCOPE, detail=refused.detail) from None
```

`prepare_entry` uses `liquidity_from_sessions(volume, quote)` when `volume` is given (else `liquidity_from_history` as today) and adds `discretionary_scope=scope_evidence` to the `replace(approval, ...)` that sets `entry_limits`. In `command_stack.py`: `_build_ai_paper_parts` creates one `MtimeCachedFilterLoader()` shared by `AiEntryFilter(universe=..., load_filter=loader)` and `trading_filter_refusal(loader)`, plus `AiDeploymentStore(trader.journal_db, now=now)` and `ScopeCheckStore(trader.journal_db, now=now)`; `_build_ai_paper_services` reuses `parts.deployments` and builds:

```python
    contracts = IbContractEvidenceSource(
        request_details=_contract_details_port(trader), remember=lambda details: _remember_instrument(trader, details),
        now=now)
    volumes = DollarVolumeSource(history=getattr(trader, "data", None),
                                 alpaca_history=lambda: _alpaca_provider(trader, Capability.HISTORY), now=now)
    decisions.attach_scope(DiscretionaryScopeService(
        contracts=contracts, volumes=volumes, quotes=quotes, filter_refusal=parts.filter_refusal,
        checks=parts.scope_checks, now=now))
```

with `_contract_details_port(trader)` = `getattr(trader, "contract_details_port", None)` (test seam) or `lambda contract: _run_on_trader_loop(trader, trader.client.ib.reqContractDetailsAsync(contract), timeout=CONTRACT_DETAILS_TIMEOUT_SECONDS)`; `_remember_instrument` inserts `SecurityDefinition.from_contract_details(details)` into `INSTRUMENTS_UNIVERSE` when the conid is not there (same logic as `production_api._cache_resolved_instrument`); `_alpaca_provider(trader, capability)` = `getattr(trader, "provider_factory", None)` (test seam) or `ProviderRegistry.from_config({"alpaca_api_key_id": trader.alpaca_api_key_id, "alpaca_api_secret_key": trader.alpaca_api_secret_key}).get(capability, "alpaca")` (Task 10 adds the two `Trader` fields; until then `getattr(trader, name, "")`). `contracts`, `volumes` are kept on `AiPaperServices` (`scope_contracts`, `scope_volumes`, both default `None`) for Task 10.
- [ ] **Step 4: Run** the new file, `tests/automation/test_ai_paper_decision.py`, `tests/automation/test_ai_paper_evidence.py`, `tests/test_ai_paper_rpc.py`, `tests/test_command_stack.py` → PASS.
- [ ] **Step 5: Commit** — `feat: check the discretionary scope rule at ai_paper entry admission`.

---

### Task 8: The scope rule at dispatch, and the discretionary label

**Files:**
- Modify: `trader/automation/discretionary_scope.py` (`DISPATCH_EVIDENCE_MAX_AGE`, `compose_entry_gates`, `discretionary_scope_gate`)
- Modify: `trader/automation/ai_paper_decision.py` (`_start_saga` reads the dispatch detail; `links_for_order_ref` label; `DecisionLink.deployment_kind: Optional[str] = None`)
- Modify: `trader/trading/command_stack.py` (`_ai_paper_guard_options` composes the gates)
- Modify: `tests/automation/ai_paper_world.py` (the guard's `ai_entry_gate` = `compose_entry_gates(self._scope_gate, ai_entry_gate(...))`, `self.scope_gate = None`); `tests/automation/discretionary_world.py` sets `world.scope_gate`
- Test: `tests/automation/test_discretionary_dispatch.py`

**Interfaces:**
- Produces: `compose_entry_gates(*gates) -> gate`; `discretionary_scope_gate(*, kind_of: Callable[[str], str], checks: ScopeCheckStore, filter_refusal) -> Callable[[request, approval, quote, now], Optional[str]]`.

- [ ] **Step 1: Write the failing tests:**

```python
def test_price_falling_below_the_floor_at_dispatch_is_refused_with_its_part(tmp_path):    # review focus 2
    world = discretionary_world(tmp_path, rule={"min_price": 99.97})
    world.quotes.set(bid=99.98, ask=100.0)
    world.on_before_guard(lambda: world.quotes.set(bid=99.96, ask=100.0))     # no drift: the ask is unchanged
    receipt = world.submit(deployment_digest=world.ddigest)
    assert (receipt.state, receipt.error_code) == ("REJECTED", "OUT_OF_DISCRETIONARY_SCOPE")
    assert (receipt.outcome["detail"]["part"], receipt.outcome["detail"]["phase"]) == ("price", "dispatch")
    assert world.dispatch.plans == []
    assert world.decisions.row("dec-00000001").error_code == "OUT_OF_DISCRETIONARY_SCOPE"


def test_an_ask_rise_that_breaks_the_cap_is_refused_at_dispatch(tmp_path):
    assert planned_entry_limit(100.20, 100.15, Decimal("10")) == 100.30    # 399 x 100.30 = 40,019.70
    world = discretionary_world(tmp_path, rule={"max_order_share_of_dollar_volume": 0.0004})   # $40,000
    world.on_before_guard(lambda: world.quotes.set(bid=100.15, ask=100.20))   # 20 bps: inside the drift limit
    receipt = world.submit(deployment_digest=world.ddigest)
    assert (receipt.outcome["detail"]["part"], receipt.outcome["detail"]["phase"]) == ("liquidity", "dispatch")


def test_admission_evidence_older_than_sixty_seconds_is_stale_at_dispatch(tmp_path):
    world = discretionary_world(tmp_path)
    world.on_before_guard(lambda: world.clock.advance(seconds=61))
    receipt = world.submit(deployment_digest=world.ddigest)
    assert receipt.outcome["detail"]["part"] == "evidence_stale"


def test_dispatch_makes_no_ib_or_history_call(tmp_path):
    world = discretionary_world(tmp_path)
    world.on_before_guard(lambda: (world.contracts.fail_with("must not be called"),
                                   world.volumes.fail_with("must not be called")))
    assert world.submit(deployment_digest=world.ddigest).state == "SUBMITTED"


def test_a_discretionary_request_without_scope_evidence_fails_closed(tmp_path):
    world = discretionary_world(tmp_path)
    request = SimpleNamespace(action=AI_PAPER_ACTION, command_id="aip-x",
                              body={"deployment_digest": world.ddigest})
    approval = SimpleNamespace(discretionary_scope=None, conid=CONID, quantity=10.0)
    assert world.scope_gate(request, approval, quote(), NOW) == "OUT_OF_DISCRETIONARY_SCOPE"
    assert world.scope.checks.detail("aip-x", "dispatch")["part"] == "evidence_stale"


def test_a_strategy_request_passes_the_gate_untouched(tmp_path):
    world = discretionary_world(tmp_path)
    request = SimpleNamespace(action=AI_PAPER_ACTION, command_id="aip-y", body={"deployment_digest": world.digest})
    assert world.scope_gate(request, SimpleNamespace(discretionary_scope=None), quote(), NOW) is None


def test_the_trip_link_says_discretionary(tmp_path):
    world = discretionary_world(tmp_path)
    world.submit(deployment_digest=world.ddigest)
    (link,) = world.decisions.links_for_order_ref("mmr:og-aip-dec-00000001")
    assert (link.strategy_version, link.deployment_kind) == ("discretionary", "discretionary")
    assert DecisionStoreAttribution(world.decisions).links_for_order_ref(
        "mmr:og-aip-dec-00000001").strategy_version == "discretionary"
```

- [ ] **Step 2: Run, expect FAIL** (the price case is `SUBMITTED`).
- [ ] **Step 3: Implement:**

```python
DISPATCH_EVIDENCE_MAX_AGE = dt.timedelta(seconds=60)


def compose_entry_gates(*gates):
    def gate(request, approval, quote, now) -> Optional[str]:
        for each in gates:
            code = each(request, approval, quote, now)
            if code:
                return code
        return None
    return gate


def discretionary_scope_gate(*, kind_of: Callable[[str], str], checks: ScopeCheckStore, filter_refusal):
    """Ruling 8 at dispatch: price, liquidity and the filter on the guard's fresh quote; the rest from
    the admission evidence on the approval. No IB call and no history read under the entry lock."""

    def gate(request, approval, quote, now: dt.datetime) -> Optional[str]:
        if getattr(request, "action", None) != AI_PAPER_ACTION:
            return None
        digest = (getattr(request, "body", None) or {}).get("deployment_digest")
        evidence = getattr(approval, "discretionary_scope", None)
        if evidence is None:
            if digest is None or kind_of(digest) != DISCRETIONARY_KIND:
                return None
            verdict = ScopeVerdict("evidence_stale", "the approval carries no scope evidence", {})
        elif evidence.deployment_digest != digest:
            verdict = ScopeVerdict("evidence_stale", "the scope evidence names another deployment", {})
        else:
            verdict = evaluate_scope(evidence.rule, _dispatch_inputs(evidence, approval, quote, now,
                                                                     filter_refusal), now)
        checks.record(command_id=request.command_id, phase="dispatch", deployment_digest=str(digest),
                      conid=int(approval.conid), verdict=verdict)
        return None if verdict.passed else OUT_OF_DISCRETIONARY_SCOPE

    return gate


def _dispatch_inputs(evidence, approval, quote, now, filter_refusal) -> ScopeInputs:
    age = now - evidence.contract.fetched_at
    fresh = dt.timedelta(seconds=-MAX_SOURCE_CLOCK_SKEW_SECONDS) <= age <= DISPATCH_EVIDENCE_MAX_AGE
    notional = None
    if quote_problem(quote, now) is None:
        limit = planned_entry_limit(float(quote.ask), float(quote.bid), AI_ENTRY_POLICY.limit_offset_bps)
        notional = abs(float(approval.quantity)) * limit
    return ScopeInputs(contract=evidence.contract if fresh else None, quote=quote, volume=evidence.volume,
                       order_notional=notional, filter_refusal=filter_refusal,
                       missing=() if fresh else (f"admission evidence is {age.total_seconds():.0f} s old",))
```

A raising gate is already `AI_ENTRY_GATE_UNAVAILABLE` in `DispatchGuard._run_ai_entry_gate` (fail closed). In `_start_saga`:

```python
        if saga_state.state == "CLOSED" and saga_state.error_code:
            if saga_state.error_code in LATCH_CODES:
                self._latch(saga_state.error_code, decision)
            raise _Refusal(saga_state.error_code, detail=self._dispatch_detail(cmd, saga_state.error_code))

    def _dispatch_detail(self, cmd: CommandRequest, code: str) -> Optional[dict]:
        if code != OUT_OF_DISCRETIONARY_SCOPE:
            return None
        found = None if self._scope is None else self._scope.checks.detail(cmd.command_id, "dispatch")
        return found or {"part": "evidence_stale", "reason": "the dispatch check left no record",
                         "phase": "dispatch", "check_id": None}
```

`links_for_order_ref` selects `CASE WHEN p.kind = 'discretionary' THEN 'discretionary' ELSE d.strategy_digest END` as the strategy version and appends `p.kind`. `_ai_paper_guard_options` sets `"ai_entry_gate": compose_entry_gates(discretionary_scope_gate(kind_of=parts.deployments.kind_of, checks=parts.scope_checks, filter_refusal=parts.filter_refusal), ai_entry_gate(entry_filter=parts.entry_filter))`.
- [ ] **Step 4: Run** the new file, `tests/automation/test_discretionary_admission.py`, `tests/automation/test_ai_paper_decision.py`, `tests/scoreboard/`, `tests/test_dispatch_guard.py` → PASS.
- [ ] **Step 5: Commit** — `feat: re-check the discretionary scope rule at dispatch and label its trips`.

---

### Task 9: The discovery reader (Alpaca movers, most-actives, news; exact symbols)

**Files:**
- Create: `trader/automation/ai_discovery_wire.py` (the models in Cross-plan additions), `trader/automation/ai_discovery.py`
- Modify: `trader/data_providers/alpaca/movers.py` (`MAX_MOST_ACTIVES = 100`, `AlpacaMovers.screener(top) -> dict`, `AlpacaMovers.most_actives(top) -> dict`)
- Test: `tests/automation/test_ai_discovery.py`

**Interfaces:**
- Produces: `DELAY_MINUTES = 15`, `MAX_IB_LOOKUPS = 40`, `LOOKUP_DEADLINE_SECONDS = 30.0`, `MAX_VOLUME_FETCHES = 20`; `DiscoveryRefused(code, message)`; `SymbolResolver(*, contracts, now, max_lookups=MAX_IB_LOOKUPS, deadline_seconds=LOOKUP_DEADLINE_SECONDS, clock=time.monotonic)` with `resolve_all(symbols) -> dict[str, SymbolResolution]`; `AiDiscoveryReader(*, providers: Callable[[Capability], Any], resolver, volumes, deployments, filter_refusal, now, volume_budget=MAX_VOLUME_FETCHES)` with `read(request: DiscoverAiCandidatesRequest) -> DiscoverAiCandidatesResponse`.

- [ ] **Step 1: Write the failing tests.** Real `AlpacaMovers` / `AlpacaNews` over `AlpacaClient("k", "s", session=FakeSession(routes))`; `FakeSession.get(url, params, headers, timeout)` answers by path, and a route may be an HTTP status. `FakeContracts.by_symbol` resolves `AAPL`, `MSFT`, `SPY` (ETF, ARCA), `PINKY` (PINK), and `DUAL` → `AMBIGUOUS`.

```python
MOVERS = {"gainers": [{"symbol": "AAPL", "price": 101.0, "change": 2.0, "percent_change": 2.0},
                      {"symbol": "BRK.B", "price": 400.0, "change": 4.0, "percent_change": 1.0}],
          "losers": [{"symbol": "PINKY", "price": 7.0, "change": -1.0, "percent_change": -12.5}],
          "last_updated": "2026-07-17T14:59:00Z"}
ACTIVES = {"most_actives": [{"symbol": "SPY", "volume": 9e7, "trade_count": 1}, {"symbol": "AAPL", "volume": 5e7,
                                                                                 "trade_count": 1}],
           "last_updated": "2026-07-17T14:58:00Z"}


def request(**changes):
    return DiscoverAiCandidatesRequest(**{"deployment_digest": DIGEST, "movers_top": 10, "most_actives_top": 10,
                                          "watchlist": ["MSFT"], "news_per_symbol": 2, "news_symbols_max": 5,
                                          **changes})


def test_candidates_are_timestamped_delayed_and_merged(reader):
    out = reader(movers=MOVERS, actives=ACTIVES).read(request())
    assert (out.source, out.delayed, out.delay_minutes) == ("alpaca", True, 15)
    by = {c.symbol: c for c in out.candidates}
    assert by["AAPL"].origins == ["gainer", "most_active"] and by["AAPL"].conid == CONID
    assert by["AAPL"].source_timestamp == "2026-07-17T14:59:00Z" and by["AAPL"].delayed
    assert by["MSFT"].origins == ["watchlist"]
    assert [c.symbol for c in out.candidates] == ["AAPL", "BRK.B", "PINKY", "SPY", "MSFT"]


def test_unresolved_symbols_are_reported_not_guessed(reader):
    out = reader(movers={**MOVERS, "losers": [{"symbol": "DUAL", "price": 9.0, "change": 1, "percent_change": 1}]},
                 actives=ACTIVES).read(request())
    by = {c.symbol: c for c in out.candidates}
    assert (by["BRK.B"].resolution, by["BRK.B"].conid) == ("SYMBOL_FORM_UNSUPPORTED", None)
    assert (by["DUAL"].resolution, by["DUAL"].conid) == ("AMBIGUOUS", None)
    assert out.coverage.resolution.unresolved == 2


def test_the_precheck_names_the_failed_part(reader):
    by = {c.symbol: c for c in reader(movers=MOVERS, actives=ACTIVES).read(request()).candidates}
    assert (by["PINKY"].scope_precheck.status, by["PINKY"].scope_precheck.part) == ("FAIL", "exchange")
    assert by["AAPL"].scope_precheck.status == "PASS"
    assert (by["MSFT"].scope_precheck.status, by["MSFT"].scope_precheck.part) == ("NOT_CHECKED", "price")


def test_a_failed_source_is_reported_not_hidden(reader):                    # review focus 4
    out = reader(movers=500, actives=ACTIVES).read(request())
    assert out.coverage.movers.failed and out.coverage.movers.error_code == "ProviderError"
    assert not out.coverage.most_actives.failed and out.coverage.complete is False
    assert {c.symbol for c in out.candidates} == {"SPY", "AAPL", "MSFT"}


def test_news_is_per_symbol_and_its_failures_count(reader):
    out = reader(movers=MOVERS, actives=ACTIVES, news={"AAPL": [ARTICLE], "SPY": 500}).read(request())
    by = {c.symbol: c for c in out.candidates}
    assert by["AAPL"].news_status == "OK" and by["AAPL"].news[0].title == ARTICLE["headline"]
    assert by["SPY"].news_status == "FAILED" and out.coverage.news.failed_symbols == ["SPY"]
    assert by["PINKY"].news_status == "SKIPPED"                                   # a FAIL gets no news call
    assert out.coverage.complete is False


def test_the_lookup_budget_is_reported(reader):
    out = reader(movers=MOVERS, actives=ACTIVES, max_lookups=1).read(request())
    assert any(c.resolution == "RESOLUTION_BUDGET" for c in out.candidates) and not out.coverage.complete


def test_blank_keys_fail_the_read_loudly(reader):
    with pytest.raises(DiscoveryRefused) as exc:
        reader(keys=False).read(request())
    assert exc.value.code == "DISCOVERY_SOURCE_UNAVAILABLE"


def test_a_strategy_digest_is_refused(reader):
    with pytest.raises(DiscoveryRefused) as exc:
        reader(movers=MOVERS, actives=ACTIVES).read(request(deployment_digest=STRATEGY_DIGEST))
    assert exc.value.code == "DEPLOYMENT_KIND_MISMATCH"


def test_resolutions_are_cached_for_the_session(reader):
    r = reader(movers=MOVERS, actives=ACTIVES)
    r.read(request())
    calls = r.contracts.calls
    r.read(request())
    assert r.contracts.calls == calls
```

`reader(...)` is a fixture factory building `AiDiscoveryReader` with a real `AiDeploymentStore` holding `DIGEST` (discretionary) and `STRATEGY_DIGEST`, a `DollarVolumeSource` over `make_history` plus a fake Alpaca history, and `providers=lambda capability: {MOVERS: AlpacaMovers(client), NEWS: AlpacaNews(client)}[capability]`; `keys=False` makes `providers` raise `ProviderNotConfigured("alpaca", [...])`.
- [ ] **Step 2: Run, expect FAIL** (`ModuleNotFoundError`).
- [ ] **Step 3: Implement.** `AlpacaMovers`:

```python
    def screener(self, top: int) -> dict:
        """The raw stocks movers payload: ``gainers``, ``losers`` and Alpaca's ``last_updated``."""
        return self._client.get_json('/v1beta1/screener/stocks/movers', {'top': min(int(top), MAX_TOP)})

    def most_actives(self, top: int) -> dict:
        """The raw most-actives payload by volume: ``most_actives`` and ``last_updated``."""
        return self._client.get_json('/v1beta1/screener/stocks/most-actives',
                                     {'by': 'volume', 'top': min(int(top), MAX_MOST_ACTIVES)})
```

The reader (core paths in full; `_Row` is a small mutable dataclass: `symbol, origins, price, change_pct, volume, as_of`):

```python
class AiDiscoveryReader:
    def read(self, request: DiscoverAiCandidatesRequest) -> DiscoverAiCandidatesResponse:
        rule = self._rule(request.deployment_digest)
        movers_provider = self._provider(Capability.MOVERS)
        rows: dict[str, _Row] = {}
        movers = self._source("movers", 2 * request.movers_top,
                              lambda: movers_provider.screener(request.movers_top),
                              lambda payload: self._add_movers(rows, payload))
        actives = self._source("most_actives", request.most_actives_top,
                               lambda: movers_provider.most_actives(request.most_actives_top),
                               lambda payload: self._add_actives(rows, payload))
        for symbol in request.watchlist:
            self._row(rows, symbol, "watchlist")
        watchlist = SourceCoverage(requested=len(request.watchlist), returned=len(request.watchlist),
                                   failed=False, error_code=None, as_of=None)
        resolutions = self._resolver.resolve_all(list(rows))
        budget = [self._volume_budget]
        candidates = [self._candidate(row, resolutions[row.symbol], rule, budget) for row in rows.values()]
        news = self._news(candidates, request)
        resolution = _resolution_coverage(resolutions.values())
        complete = not (movers.failed or actives.failed or news.failed_symbols or resolution.failed)
        return DiscoverAiCandidatesResponse(
            read_at=self._now().isoformat(), source="alpaca", delayed=True, delay_minutes=DELAY_MINUTES,
            deployment_digest=request.deployment_digest,
            coverage=DiscoveryCoverage(movers=movers, most_actives=actives, watchlist=watchlist, news=news,
                                       resolution=resolution, complete=complete),
            candidates=[DiscoveryCandidate(**c) for c in candidates])

    def _provider(self, capability: Capability):
        try:
            return self._providers(capability)
        except ProviderNotConfigured:
            raise DiscoveryRefused("DISCOVERY_SOURCE_UNAVAILABLE", "Alpaca keys are not configured in the trader") from None

    def _source(self, name, requested, fetch, add) -> SourceCoverage:
        try:
            payload = fetch()
        except ProviderError as ex:
            # Spec 9: a failed source is recorded with real coverage, never presented as complete.
            logger.error("discover_ai_candidates: %s failed: %s", name, type(ex).__name__)
            return SourceCoverage(requested=requested, returned=0, failed=True, error_code=type(ex).__name__,
                                  as_of=None)
        return SourceCoverage(requested=requested, returned=add(payload), failed=False, error_code=None,
                              as_of=payload.get("last_updated"))

    def _rule(self, digest: str):
        try:
            deployment = self._deployments.get_sealed_any(digest)
        except DeploymentRefused as ex:
            raise DiscoveryRefused(ex.code, ex.message) from None
        if not isinstance(deployment, DiscretionaryDeployment):
            raise DiscoveryRefused("DEPLOYMENT_KIND_MISMATCH", "discovery needs a discretionary deployment")
        return deployment.scope_rule

    def _precheck(self, row: _Row, resolution: SymbolResolution, rule, budget: list[int]) -> dict:
        def result(status, part, reason, median=None):
            return {"status": status, "part": part, "reason": reason, "median_dollar_volume_20d": median}

        if resolution.status != "RESOLVED":
            return result("NOT_CHECKED", None, f"symbol {resolution.status.lower()}")
        static = static_scope_refusal(rule, resolution.contract)
        if static is not None:
            return result("FAIL", *static)
        if row.price is not None and row.price < rule.min_price:
            return result("FAIL", "price", f"delayed price {row.price} is below {rule.min_price}")
        volume = self._volumes.cached(resolution.conid)
        if volume is None and budget[0] > 0:
            budget[0] -= 1
            try:
                volume = self._volumes.twenty_sessions(resolution.conid, resolution.contract.symbol)
            except ScopeEvidenceUnavailable as ex:
                return result("NOT_CHECKED", "dollar_volume", ex.reason)
        if volume is None:
            return result("NOT_CHECKED", "dollar_volume", "volume fetch budget for this read used up")
        median, floor = volume.median_dollar_volume, effective_dollar_volume_floor(rule)
        if median < floor:
            return result("FAIL", "dollar_volume", f"median {median:,.0f} is below {floor:,.0f}", median)
        try:
            denied = self._filter_refusal(resolution.contract, row.price or 0.0)
        except Exception as ex:
            denied = f"trading filter unavailable: {type(ex).__name__}"
        if denied:
            return result("FAIL", "trading_filter", denied, median)
        if row.price is None:
            return result("NOT_CHECKED", "price", "the discovery source gave no price", median)
        return result("PASS", None, "in scope on delayed data; liquidity is checked at admission", median)
```

`_add_movers` adds gainers then losers (origin `gainer` / `loser`, price, `percent_change`, `as_of = last_updated`) and returns the row count; `_add_actives` adds most-actives (origin `most_active`, volume) the same way. Order is first sight. `_news` takes, in candidate order, at most `news_symbols_max` candidates with a conid and a precheck that is not `FAIL`, calls `self._provider(Capability.NEWS).news(symbol, news_per_symbol)` per symbol, marks `OK` / `FAILED` (a `ProviderError`, logged at ERROR) and leaves the rest `SKIPPED`; `news_per_symbol == 0` skips all. `SymbolResolver.resolve_all` follows ruling 12: a cache keyed by the ET session date (cleared when the date changes), `SYMBOL_FORM_UNSUPPORTED` without counting, `RESOLUTION_BUDGET` after `max_lookups` lookups or `deadline_seconds`, and `RESOLUTION_FAILED` (not cached) when `by_symbol` raises `ScopeEvidenceUnavailable`.
- [ ] **Step 4: Run** the new file and `tests/data_providers/` → PASS.
- [ ] **Step 5: Commit** — `feat: add the trader-owned alpaca discovery reader with honest coverage`.

---

### Task 10: `discover_ai_candidates` over signed RPC; the trader's Alpaca keys

**Files:**
- Modify: `trader/trading/trading_runtime.py` (`Trader.__init__(..., alpaca_api_key_id: str = '', alpaca_api_secret_key: str = '')` stored as attributes; the Container fills them from config or `ALPACA_API_KEY_ID` / `ALPACA_API_SECRET_KEY`; shared with Plan 2, same names — skip if Plan 2 landed)
- Modify: `trader/trading/command_stack.py` (`AiPaperServices.discovery: Any = None`; `_build_ai_paper_services` builds `AiDiscoveryReader` with `providers=lambda capability: _alpaca_provider(trader, capability)`, `SymbolResolver(contracts=contracts, now=now)`, `volumes`, `parts.deployments`, `parts.filter_refusal`)
- Modify: `trader/messaging/production_api.py` (`_discover_ai_candidates_handler`; `registry.register("query", "discover_ai_candidates", DiscoverAiCandidatesRequest, DiscoverAiCandidatesResponse, handler, execution="thread")` in `register_ai_paper_authority`)
- Modify: `trader/messaging/principals.py` (`("query", "discover_ai_candidates"): frozenset({"ai_supervisor"})`)
- Test: `tests/test_ai_discovery_rpc.py` (new), `tests/test_rpc_acl.py`, `tests/test_ai_paper_rpc.py` (`_served` gains `prepare: Callable[[Any], None] = lambda trader: None`, called before `build_command_stack`)

- [ ] **Step 1: Write the failing tests:**

```python
@pytest.fixture
def served(tmp_path, monkeypatch):
    for name in ("ALPACA_API_KEY_ID", "ALPACA_API_SECRET_KEY"):
        monkeypatch.delenv(name, raising=False)                     # the caller side holds no Alpaca key
    session = FakeSession({"/v1beta1/screener/stocks/movers": 500, "/v1beta1/screener/stocks/most-actives": ACTIVES,
                           "/v1beta1/news": {"news": []}})
    client = AlpacaClient("k", "s", session=session)

    def prepare(trader):
        trader.provider_factory = lambda capability: {Capability.MOVERS: AlpacaMovers(client),
                                                      Capability.NEWS: AlpacaNews(client),
                                                      Capability.HISTORY: FakeAlpacaHistory(daily_frame)}[capability]
        trader.contract_details_port = FakeIbDetails({"SPY": details(conid=756733, symbol="SPY", primary="ARCA",
                                                                     stock_type="ETF"),
                                                      "AAPL": details()})
        for name in ("reqScannerDataAsync", "reqScannerSubscription", "reqScannerData"):
            monkeypatch.setattr(trader.client.ib, name,
                                lambda *a, **k: pytest.fail("discovery used the IB scanner"), raising=False)
    stack = _served(tmp_path, monkeypatch, AiPaperConfig(enabled=True), prepare=prepare)
    yield stack
    stack.close()


def test_discovery_over_signed_rpc_reports_partial_coverage_and_never_scans(served):   # review focus 4
    digest = register_discretionary(served)["outcome"]["digest"]
    body = {"deployment_digest": digest, "movers_top": 5, "most_actives_top": 5, "watchlist": [],
            "news_per_symbol": 1, "news_symbols_max": 3}
    out = query(served, "ai_supervisor").call("discover_ai_candidates", body, DiscoverAiCandidatesResponse)
    assert out.coverage.movers.failed and not out.coverage.complete and out.delayed
    assert {c.symbol for c in out.candidates} == {"SPY", "AAPL"}
    assert {c.conid for c in out.candidates} == {756733, CONID}


@pytest.mark.parametrize("principal", ["ai_research", "cli", "dashboard", "strategy"])
def test_only_the_supervisor_reads_discovery(served, principal):
    with pytest.raises(TypedRpcRemoteError) as exc:
        query(served, principal).call("discover_ai_candidates", {"deployment_digest": "sha256:" + "a" * 64,
                                      "movers_top": 1, "most_actives_top": 1, "watchlist": [],
                                      "news_per_symbol": 0, "news_symbols_max": 0}, dict)
    assert exc.value.code == "PERMISSION_DENIED"


@pytest.mark.parametrize("patch", [{"movers_top": 0}, {"movers_top": True}, {"watchlist": ["brk.b"]},
                                   {"news_symbols_max": 31}, {"extra": 1}])
def test_the_request_is_strict(served, patch):
    body = {"deployment_digest": "sha256:" + "a" * 64, "movers_top": 1, "most_actives_top": 1, "watchlist": [],
            "news_per_symbol": 0, "news_symbols_max": 0, **patch}
    with pytest.raises(TypedRpcRemoteError) as exc:
        query(served, "ai_supervisor").call("discover_ai_candidates", body, dict)
    assert exc.value.code == "VALIDATION_ERROR"


def test_blank_trader_keys_fail_loudly(tmp_path, monkeypatch):
    stack = _served(tmp_path, monkeypatch, AiPaperConfig(enabled=True))   # no provider_factory: real registry, '' keys
    try:
        digest = register_discretionary(stack)["outcome"]["digest"]
        with pytest.raises(TypedRpcRemoteError) as exc:
            query(stack, "ai_supervisor").call("discover_ai_candidates", {
                "deployment_digest": digest, "movers_top": 1, "most_actives_top": 1, "watchlist": [],
                "news_per_symbol": 0, "news_symbols_max": 0}, dict)
        assert exc.value.code == "DISCOVERY_SOURCE_UNAVAILABLE"
    finally:
        stack.close()


def test_the_trader_takes_its_alpaca_keys_by_these_names(monkeypatch, tmp_path):
    parameters = inspect.signature(Trader.__init__).parameters
    assert parameters["alpaca_api_key_id"].default == "" and parameters["alpaca_api_secret_key"].default == ""

    class KeyHolder:                          # the Container fills a parameter from the upper-case env var
        def __init__(self, alpaca_api_key_id: str = "", alpaca_api_secret_key: str = ""):
            self.keys = (alpaca_api_key_id, alpaca_api_secret_key)
    monkeypatch.setenv("ALPACA_API_KEY_ID", "id-1")
    monkeypatch.setenv("ALPACA_API_SECRET_KEY", "secret-1")
    assert Container.create(write_minimal_trader_yaml(tmp_path)).resolve(KeyHolder).keys == ("id-1", "secret-1")
```

`write_minimal_trader_yaml(tmp_path)` writes a two-line `trader.yaml` (`trading_mode: paper`, `ib_account: DU111111`) and returns its path. `FakeIbDetails(rows_by_symbol)` answers `request_details(contract)` with `[rows_by_symbol[contract.symbol]]` (or `[]`). The 500 route is not retried (`call_with_retry` retries only HTTP 429), so `AlpacaClient` raises `ProviderError` at once.
- [ ] **Step 2: Run, expect FAIL** (`METHOD_NOT_ALLOWED`).
- [ ] **Step 3: Implement.** Handler:

```python
def _discover_ai_candidates_handler(reader):
    from trader.automation.ai_discovery import DiscoveryRefused

    def _handler(parsed: DiscoverAiCandidatesRequest) -> DiscoverAiCandidatesResponse:
        try:
            return reader.read(parsed)
        except DiscoveryRefused as ex:
            raise _DispatchProblem(ex.code, ex.message) from None
    return _handler
```

Register only when `ai_paper.discovery is not None`, with `execution="thread"` (ruling 17). Add the ACL row.
- [ ] **Step 4: Run** `tests/test_ai_discovery_rpc.py tests/test_ai_paper_rpc.py tests/test_rpc_acl.py tests/test_command_stack.py tests/test_container.py` → PASS.
- [ ] **Step 5: Commit** — `feat: serve discover_ai_candidates to ai_supervisor over signed rpc`.

---

### Task 11: Pin the covered 6.4 bullets, docs, full suite

**Files:**
- Test: `tests/automation/test_ai_paper_reductions.py`
- Modify: `AGENTS.md` (one bullet under "Paper vs live"), `docs/CLI_REFERENCE.md` (`ai-deployment register-discretionary`, `ai-deployment show`)

These tests pin behaviour that already exists. They should pass at once; if one fails, stop: it is a real gap and needs its own fix and review.

- [ ] **Step 1: Write the tests:**

```python
def test_close_after_the_entry_cutoff_is_admitted(world):              # spec 6.4 / 5.2: after the cutoff
    world.clock.advance(hours=4, minutes=40)                             # 15:40 ET: after the 15:30 entry cutoff
    assert world.submit(close_body(world)).error_code == "CLOSE_PENDING"


def test_close_joins_the_session_flatten_while_armed(world):            # flatten precedence
    world.liquidation.start(ACCOUNT, "session-flatten-1", DEADLINE)    # account owner, as the session flatten
    assert world.submit(close_body(world)).outcome["close_root_id"] == "session-flatten-1"
    assert world.liquidation_runs() == {"session-flatten-1"}


def test_a_reduction_ignores_the_discretionary_scope_rule(tmp_path):    # the rule is for entries only
    w = discretionary_world(tmp_path, real_liquidation=True)
    w.owned(CONID, 300.0)
    w.contracts.fail_with("must not be called")
    w.quotes.set(bid=1.0, ask=1.01)
    assert w.submit(close_body(w)).error_code == "CLOSE_PENDING"
    assert w.contracts.calls == 0
```

- [ ] **Step 2: Run** the file → PASS (expected at once, see above).
- [ ] **Step 3: Docs.** `AGENTS.md`, "Paper vs live": "Self-found ideas trade only under an operator `discretionary` deployment (`mmr ai-deployment register-discretionary`, `cli` only, paper only). The trader checks its scope rule (primary listing, stock/ETF type from IB `stockType`, bid ≥ floor, 20-session median dollar volume, ≤ 1 % of it per order, `trading_filters.yaml`) at admission and at dispatch; a miss is `OUT_OF_DISCRETIONARY_SCOPE` with the failed part." `docs/CLI_REFERENCE.md`: both subcommands, their flags and the JSON shape of `show`.
- [ ] **Step 4: Run the full suite:** `.venv/bin/python -m pytest tests/ -q --timeout=60 --ignore=tests/test_ibrx_async.py` → green. Fix only failures this plan caused.
- [ ] **Step 5: Commit** — `feat: pin model-close precedence and document the discretionary deployment`.

---
