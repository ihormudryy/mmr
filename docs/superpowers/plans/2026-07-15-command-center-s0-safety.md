# S0 Semantic Safety Corrections Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the verified approval, expiry, risk, account-value, confidence, strategy-state, and order-status semantic defects without waiting for realtime infrastructure.

**Architecture:** Preserve current SDK-side approval temporarily, but move expiry into atomic `ProposalStore` operations and invoke the sweep from the existing strategy reconciliation cadence. Normalize transport-independent display values in the SDK and make the legacy dashboard render failures explicitly.

**Tech Stack:** CPython 3.12.13, DuckDB, current SDK/RPC layer, FastAPI/Jinja2, pytest.

## Global Constraints

- Part of the command-center suite indexed by `2026-07-15-command-center-plan-index.md`; the source specification is `docs/superpowers/specs/2026-07-15-realtime-trading-command-center-design.md` at or after commit `740a242`. This is the `[S0]` workstream and is cherry-pickable ahead of the rest.
- This workstream has no dependency on the journal, SSE, session-cookie, or command-center UI work.
- Approval must never place an order unless one atomic row claim proves the proposal is still `PENDING` and unexpired.
- A missing expiry remains valid for legacy manual proposals; a present naive, invalid, or elapsed expiry fails closed as `EXPIRED`.
- An RPC or `SuccessFail` failure must never render as success or as a clean risk pass.
- Stored `EXECUTED` means order submitted, not filled; the public display value is `ORDER_SUBMITTED` until broker fill evidence exists.
- Commits are independently cherry-pickable and tagged `[S0]` in their messages.
- Environment bootstrap: this workstream ships before `[G0]` pins CI, so first run
  `uv sync --extra test` once and verify with
  `uv run --frozen pytest tests/test_proposal_store.py -q` (must PASS before any
  change). If the host uv environment cannot resolve, run every pytest command in
  this plan inside the container instead:
  `docker exec mmr-mmr-1 bash -c 'cd /home/trader/mmr && python3 -m pytest <same args>'`
  after syncing code with `./docker.sh -s`.

---

### Task 1: Atomic proposal expiry and approval claim

**Files:**
- Modify: `trader/data/proposal_store.py:1-285`
- Modify: `tests/test_proposal_store.py`

**Interfaces:**
- Produces: `ApprovalClaimResult(str, Enum)` with `CLAIMED`, `EXPIRED`, `NOT_PENDING`, and `NOT_FOUND`.
- Produces: `ProposalStore.claim_for_approval(id: int, now: datetime) -> ApprovalClaimResult`.
- Produces: `ProposalStore.expire_stale_pending(now: datetime) -> list[int]`.

- [ ] **Step 1: Write failing atomic-claim tests**

Add tests that create proposals with `metadata={"expires_at": value}` and assert the exact result and persisted status:

```python
def test_claim_for_approval_expires_elapsed_row(proposal_store):
    pid = proposal_store.add(_make_proposal(source="strategy:orb"))
    proposal_store.update_metadata(pid, {"expires_at": "2026-07-15T10:00:00Z"})
    result = proposal_store.claim_for_approval(
        pid, dt.datetime(2026, 7, 15, 10, 0, 1, tzinfo=dt.timezone.utc)
    )
    assert result is ApprovalClaimResult.EXPIRED
    assert proposal_store.get(pid).status == "EXPIRED"


def test_claim_for_approval_allows_missing_legacy_expiry(proposal_store):
    pid = proposal_store.add(_make_proposal(source="manual"))
    result = proposal_store.claim_for_approval(
        pid, dt.datetime(2026, 7, 15, 10, 0, tzinfo=dt.timezone.utc)
    )
    assert result is ApprovalClaimResult.CLAIMED
    assert proposal_store.get(pid).status == "APPROVED"


def test_claim_for_approval_rejects_naive_expiry(proposal_store):
    pid = proposal_store.add(_make_proposal(source="strategy:orb"))
    proposal_store.update_metadata(pid, {"expires_at": "2026-07-15T10:30:00"})
    result = proposal_store.claim_for_approval(
        pid, dt.datetime(2026, 7, 15, 10, 0, tzinfo=dt.timezone.utc)
    )
    assert result is ApprovalClaimResult.EXPIRED
```

- [ ] **Step 2: Run the focused tests and verify failure**

Run: `uv run --frozen pytest tests/test_proposal_store.py -q`

Expected: FAIL because `ApprovalClaimResult`, `claim_for_approval`, and `expire_stale_pending` do not exist.

- [ ] **Step 3: Implement the one-statement claim and unlimited sweep**

Add the enum and use DuckDB JSON extraction plus `TRY_CAST` in the guarded update:

```python
class ApprovalClaimResult(str, Enum):
    CLAIMED = "CLAIMED"
    EXPIRED = "EXPIRED"
    NOT_PENDING = "NOT_PENDING"
    NOT_FOUND = "NOT_FOUND"


_EXPIRY_SQL = "json_extract_string(metadata, '$.expires_at')"
_AWARE_EXPIRY_SQL = (
    "regexp_matches(" + _EXPIRY_SQL + ", '(Z|[+-][0-9]{2}:[0-9]{2})$') "
    "AND TRY_CAST(" + _EXPIRY_SQL + " AS TIMESTAMPTZ) IS NOT NULL"
)


def claim_for_approval(self, id: int, now: dt.datetime) -> ApprovalClaimResult:
    now = now.astimezone(dt.timezone.utc)

    def _claim(conn):
        rows = conn.execute(
            f"""
            UPDATE trade_proposals
               SET status = CASE
                   WHEN {_EXPIRY_SQL} IS NULL THEN 'APPROVED'
                   WHEN {_AWARE_EXPIRY_SQL}
                        AND TRY_CAST({_EXPIRY_SQL} AS TIMESTAMPTZ) > ?
                     THEN 'APPROVED'
                   ELSE 'EXPIRED'
               END,
                   updated_at = ?
             WHERE id = ? AND status = 'PENDING'
         RETURNING status
            """,
            [now, now.replace(tzinfo=None), id],
        ).fetchall()
        if rows:
            return ApprovalClaimResult.CLAIMED if rows[0][0] == "APPROVED" else ApprovalClaimResult.EXPIRED
        row = conn.execute("SELECT status FROM trade_proposals WHERE id = ?", [id]).fetchone()
        return ApprovalClaimResult.NOT_PENDING if row else ApprovalClaimResult.NOT_FOUND

    return self.db.execute_atomic(_claim)


def expire_stale_pending(self, now: dt.datetime) -> list[int]:
    now = now.astimezone(dt.timezone.utc)

    def _expire(conn):
        return [
            row[0]
            for row in conn.execute(
                f"""
                UPDATE trade_proposals
                   SET status = 'EXPIRED', updated_at = ?
                 WHERE status = 'PENDING'
                   AND {_EXPIRY_SQL} IS NOT NULL
                   AND (NOT ({_AWARE_EXPIRY_SQL})
                        OR TRY_CAST({_EXPIRY_SQL} AS TIMESTAMPTZ) <= ?)
             RETURNING id
                """,
                [now.replace(tzinfo=None), now],
            ).fetchall()
        ]

    return self.db.execute_atomic(_expire)
```

Import `Enum` and expose both methods on `ProposalStore`. Do not add a query limit or filter by source.

- [ ] **Step 4: Run store tests**

Run: `uv run --frozen pytest tests/test_proposal_store.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add trader/data/proposal_store.py tests/test_proposal_store.py
git commit -m "fix(s0): claim only unexpired proposals atomically"
```

### Task 2: Periodic expiry through strategy reconciliation

**Files:**
- Modify: `trader/strategy/signal_proposer.py:80-215`
- Modify: `trader/strategy/strategy_runtime.py` in `_reconcile()` and startup reconciliation
- Modify: `tests/test_signal_proposer.py`
- Modify: `tests/test_strategy_runtime_reconcile.py`

**Interfaces:**
- Consumes: `ProposalStore.expire_stale_pending(now)` from Task 1.
- Produces: `SignalProposer.expire_stale(now: datetime | None = None) -> list[int]`.

- [ ] **Step 1: Write failing reconciliation tests**

```python
def test_reconcile_sweeps_expired_proposals(runtime):
    runtime.signal_proposer.expire_stale = Mock(return_value=[11, 12])
    asyncio.run(runtime._reconcile())
    runtime.signal_proposer.expire_stale.assert_called_once()


def test_expire_stale_delegates_without_limit(proposer, proposal_store):
    proposal_store.expire_stale_pending = Mock(return_value=[4])
    assert proposer.expire_stale() == [4]
    proposal_store.expire_stale_pending.assert_called_once()
```

- [ ] **Step 2: Run the focused tests and verify failure**

Run: `uv run --frozen pytest tests/test_signal_proposer.py tests/test_strategy_runtime_reconcile.py -q`

Expected: FAIL because the public sweep does not exist and `_reconcile()` does not call it.

- [ ] **Step 3: Replace signal-trigger-only expiry with the shared sweep**

```python
def expire_stale(self, now: dt.datetime | None = None) -> list[int]:
    effective_now = now or dt.datetime.now(dt.timezone.utc)
    expired = self.proposal_store.expire_stale_pending(effective_now)
    if expired:
        logging.info("expired stale proposals: %s", expired)
    return expired
```

Call `self.signal_proposer.expire_stale()` at the beginning of every `_reconcile()` invocation. Keep the call from `on_signal()` as harmless defense in depth until `[M1-F3]` moves ownership to trader service.

- [ ] **Step 4: Run reconciliation tests**

Run: `uv run --frozen pytest tests/test_signal_proposer.py tests/test_strategy_runtime_reconcile.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add trader/strategy/signal_proposer.py trader/strategy/strategy_runtime.py tests/test_signal_proposer.py tests/test_strategy_runtime_reconcile.py
git commit -m "fix(s0): sweep proposal expiry during reconciliation"
```

### Task 3: SDK approval truth and ambiguous outcome preservation

**Files:**
- Modify: `trader/sdk.py:1488-1620`
- Modify: `tests/test_propose_approve_integration.py`

**Interfaces:**
- Consumes: `ApprovalClaimResult` and `claim_for_approval()` from Task 1.
- Preserves: timeout leaves the row `APPROVED`; explicit send failure may transition it to `FAILED`.

- [ ] **Step 1: Add expired and concurrent approval regressions**

```python
def test_expired_proposal_never_calls_order_rpc(mmr, rpc, proposal_store):
    pid = proposal_store.add(_proposal(expires_at="2026-07-15T10:00:00Z"))
    mmr._utcnow = lambda: dt.datetime(2026, 7, 15, 10, 1, tzinfo=dt.timezone.utc)
    result = mmr.approve(pid)
    assert not result.is_success()
    assert proposal_store.get(pid).status == "EXPIRED"
    assert not [call for call in rpc.calls if call["method"] == "place_expressive_order"]
```

- [ ] **Step 2: Run the approval integration tests and verify failure**

Run: `uv run --frozen pytest tests/test_propose_approve_integration.py -q`

Expected: FAIL because `approve()` still claims with status-only `try_transition`.

- [ ] **Step 3: Use the atomic expiry-aware claim**

Replace the pre-read/status-only claim with:

```python
claim = store.claim_for_approval(proposal_id, self._utcnow())
if claim is ApprovalClaimResult.NOT_FOUND:
    return SuccessFail.fail(error=f"Proposal #{proposal_id} not found")
if claim is ApprovalClaimResult.EXPIRED:
    return SuccessFail.fail(error=f"Proposal #{proposal_id} expired before approval")
if claim is ApprovalClaimResult.NOT_PENDING:
    current = store.get(proposal_id)
    state = current.status if current else "missing"
    return SuccessFail.fail(error=f"Proposal #{proposal_id} is {state}, not PENDING")
proposal = store.get(proposal_id)
if proposal is None:
    return SuccessFail.fail(error=f"Proposal #{proposal_id} disappeared after claim")
```

Add `_utcnow()` as a small SDK seam returning `datetime.now(timezone.utc)`. Keep the existing timeout, connection failure, explicit failure, and successful order-ID transitions unchanged.

- [ ] **Step 4: Run approval and proposal-store tests**

Run: `uv run --frozen pytest tests/test_propose_approve_integration.py tests/test_proposal_store.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add trader/sdk.py tests/test_propose_approve_integration.py
git commit -m "fix(s0): reject expired proposals at approval"
```

### Task 4: Transport-independent portfolio, proposal, and strategy values

**Files:**
- Modify: `trader/sdk.py:1008-1070,1390-1430,2170-2225`
- Modify: `trader/trading/strategy.py`
- Modify: `tests/test_sdk.py`

**Interfaces:**
- Produces: `proposal_display_status(storage_status: str) -> str`.
- Produces: `is_dispatchable_strategy_state(state: str | StrategyState) -> bool`.
- Produces proposal rows with numeric `confidence` and both `storage_status` and `display_status`.

- [ ] **Step 1: Write failing domain-value tests**

```python
def test_flat_portfolio_keeps_account_net_liquidation(mmr):
    mmr.portfolio = Mock(return_value=pd.DataFrame())
    mmr._rpc.rpc().get_account_values.return_value = {
        "NetLiquidation": {"value": "50000", "currency": "USD"}
    }
    assert mmr.portfolio_snapshot()["net_liquidation"] == 50000.0


def test_proposal_rows_keep_numeric_confidence_and_map_submission(mmr, proposal_store):
    pid = proposal_store.add(_proposal(confidence=0.73))
    proposal_store.update_status(pid, "APPROVED")
    proposal_store.update_status(pid, "EXECUTED", order_ids=[17])
    row = mmr.proposals().iloc[0]
    assert row["confidence"] == 0.73
    assert row["storage_status"] == "EXECUTED"
    assert row["display_status"] == "ORDER_SUBMITTED"
```

- [ ] **Step 2: Run SDK tests and verify failure**

Run: `uv run --frozen pytest tests/test_sdk.py -q`

Expected: FAIL because an empty portfolio returns before account values are read and proposal confidence is formatted as text.

- [ ] **Step 3: Normalize values at the SDK boundary**

```python
def proposal_display_status(storage_status: str) -> str:
    return "ORDER_SUBMITTED" if storage_status == "EXECUTED" else storage_status


def is_dispatchable_strategy_state(state) -> bool:
    value = getattr(state, "value", state)
    return str(value).upper() in {"RUNNING", "WAITING_HISTORICAL_DATA"}
```

Fetch account values before the empty-position return in `portfolio_snapshot()`. Return `net_liquidation`, `total_value=0.0`, and `position_count=0` for a flat account. In `proposals()`, replace the percent string with `confidence=float(p.confidence)`, expose `storage_status=p.status`, and expose `display_status=proposal_display_status(p.status)`. Use `is_dispatchable_strategy_state` for SDK strategy rows.

- [ ] **Step 4: Run SDK and strategy tests**

Run: `uv run --frozen pytest tests/test_sdk.py tests/test_strategy_runtime.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add trader/sdk.py trader/trading/strategy.py tests/test_sdk.py
git commit -m "fix(s0): expose truthful portfolio and workflow states"
```

### Task 5: Legacy dashboard failure rendering

**Files:**
- Modify: `web/app.py:130-430`
- Modify: `web/templates/dashboard.html:150-550`
- Modify: `tests/test_web_dashboard.py`

**Interfaces:**
- Consumes: numeric proposal confidence, display status, and dispatchable strategy helper from Task 4.
- Produces: risk panel state `ok`, `warning`, or `unavailable` without inferring `ok` from a missing report.

- [ ] **Step 1: Add false-success and false-green regressions**

```python
def test_failed_approval_never_flashes_submitted(client, stub):
    stub.approve_result = SuccessFail.fail(error="broker rejected")
    response = client.post(
        "/proposals/7/approve",
        data={"csrf_token": _csrf()},
        follow_redirects=False,
    )
    assert "broker%20rejected" in response.headers["location"]
    assert "submitted" not in response.headers["location"]


def test_risk_fetch_failure_is_unavailable_not_green(client, stub):
    stub.risk_error = ConnectionError("risk RPC down")
    html = client.get("/").text
    assert "Risk unavailable" in html
    assert "No active risk warnings" not in html
```

- [ ] **Step 2: Run dashboard tests and verify failure**

Run: `uv run --frozen pytest tests/test_web_dashboard.py -q`

Expected: FAIL because approval checks a non-boolean `.success` attribute and missing risk becomes an empty warning list.

- [ ] **Step 3: Render explicit result and risk states**

Use the real result protocol:

```python
result = _call(lambda m: m.approve(pid), retry=False)
if not hasattr(result, "is_success") or not result.is_success():
    msg = f"#{pid} approve failed: {_result_error(result)}"
else:
    msg = f"#{pid} approved & order submitted"
```

Set `risk_state = "unavailable"` when `errors["risk"]` exists, `warning` when warnings exist, and `ok` only when a risk object was fetched successfully with no warnings. Update the template to show `Risk unavailable` and remove the green pass row for unavailable data. Render `p.display_status`, keep `p.storage_status` in a data attribute for diagnostics, and render numeric `p.confidence`.

- [ ] **Step 4: Run the complete S0 regression set**

Run: `uv run --frozen pytest tests/test_proposal_store.py tests/test_signal_proposer.py tests/test_strategy_runtime_reconcile.py tests/test_propose_approve_integration.py tests/test_sdk.py tests/test_web_dashboard.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add web/app.py web/templates/dashboard.html tests/test_web_dashboard.py
git commit -m "fix(s0): prevent false success and green risk states"
```

### Task 6: S0 full verification

**Files:**
- Modify: `docs/OPERATIONAL_STATE.md` only if it currently documents the old approval or expiry behavior.

**Interfaces:**
- Produces: one cherry-pickable `[S0]` series with no realtime dependencies.

- [ ] **Step 1: Run the canonical suite**

Run: `uv run --frozen pytest tests/ --timeout=30 -q --ignore=tests/test_ibrx_async.py`

Expected: PASS with no new warnings caused by the S0 changes.

- [ ] **Step 2: Run the quarantined async test**

Run: `uv run --frozen pytest tests/test_ibrx_async.py --timeout=30 -q`

Expected: PASS.

- [ ] **Step 3: Inspect the diff for scope**

Run: `git diff --check && git diff --stat`

Expected: no whitespace errors and no SSE, session-authentication, Compose, or new command-center files.
