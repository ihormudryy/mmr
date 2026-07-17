# Command Center Retirement Checklist — [COMPAT] gate record

Spec: `docs/superpowers/specs/2026-07-15-realtime-trading-command-center-design.md`
§14.1, §13.3, §10. The single operator records every item; any unchecked item
or triggered no-go condition blocks live enablement and legacy retirement.

Operator: ____________________  Record opened: ____________________

## A. Eight-hour paper soak (§13.3)

Run with `scripts/run_paper_soak.py --hours 8` (COMPAT Task 4) against a healthy
paper Compose stack with `[M1-C]` paper commands enabled on the command center
only. The runner writes `~/.local/share/mmr/reports/soak_report_<ts>.json`; it
exits 0 (and `"passed": true`) only when every threshold AND every scenario
passes (fail-closed).

- [ ] Soak report path: ____________________ (`"passed": true`)
- [ ] RSS growth ≤ 20% after the warm-up hour (observed: ______)
- [ ] Average dashboard CPU < 1 core (observed: ______)
- [ ] p95 critical-event latency ≤ 500 ms (observed: ______)
- [ ] Zero unhandled errors and zero unresolved test commands
- [ ] Replay ring / client FIFO / terminal-row bounds respected
- [ ] Scenarios trader_outage, strategy_outage, broker_disconnect,
      dashboard_restart each ended `coherent` or `explicit-degraded`

> **TOOLING STATUS (must be resolved before this soak is a MEANINGFUL gate).**
> As of COMPAT Task 4, only **p95 critical-event latency** has a live metric
> exporter (from `scripts/soak_harness.py`'s `--report` JSON). The runner
> `evaluate_soak` is fail-closed, so the following five checks currently report
> `observed=None, passed=False` (they do NOT silently pass) until an exporter is
> added:
> - `unhandled_errors`, `unresolved_commands`, `max_replay_ring_events`,
>   `max_client_fifo_depth`, `max_terminal_rows`.
>
> Additionally, the `strategy_outage` scenario has **no live health signal** today
> and falls back to the post-recovery `parity_compare` coherence check alone.
>
> Required follow-up (release-gate blocker): export the ring/FIFO/terminal-row
> counts on `GET /api/cc-health` (they exist as private in-process state in
> `web/command_center/state.py` / `sse.py`), expose `unresolved_commands` from the
> trader command-ledger via a query RPC, and add a strategy-service health signal.
> Until then, treat the five unmetered thresholds + `strategy_outage` as **manually
> verified** (attach evidence) or the soak gate is incomplete.

## B. Live read-only market session (both surfaces)

- [ ] Session date + market: ____________________
- [ ] `DASHBOARD_COMMANDS_ENABLED=false` and
      `DASHBOARD_LIVE_COMMANDS_ENABLED=false` for the entire session
- [ ] Every scheduled `parity_compare` run exited 0 (report files listed below)
- [ ] Account and mode reconcile: ____
- [ ] Cash and net liquidation reconcile: ____
- [ ] Positions reconcile: ____
- [ ] Proposals reconcile (storage + display status): ____
- [ ] Strategy state and parameters reconcile: ____
- [ ] Risk warnings and limits reconcile: ____
- [ ] Orders reconcile: ____
- [ ] Fills reconcile: ____

Parity report files: ____________________

> **PARITY-TOOL STATUS.** `scripts/parity_compare.py` (COMPAT Task 3) has its
> comparison core unit-tested, but its `collect_legacy` / `collect_center`
> runtime wiring (field mappings + webapp fetcher names) is NOT yet verified
> against a live pair of surfaces — run one live smoke comparison and reconcile
> any divergence (real bug vs documented `--allow`) BEFORE trusting the scheduled
> runs above.

## C. Capability disposition (§8.5 [COMPAT] items)

| Capability | Disposition | Owning surface | Verified |
|---|---|---|---|
| Proposal reasoning/rationale detail | migrated | `/cc` proposal drawer | [ ] |
| Sanitized Markdown popups | migrated | `/cc` | [ ] |
| Approve / reject | migrated | `/cc` commands | [ ] |
| Strategy enable/disable | migrated | `/cc` commands | [ ] |
| Schema-driven parameter editing | migrated | `/cc` commands | [ ] |
| Strategy discovery + deploy-from-disk | retained via coordinator | `/manage` (Task 6) | [ ] |
| Watchlist CRUD | retained | `/manage` (Task 6) | [ ] |
| CSV import | retained | `/manage` (Task 6) | [ ] |

## D. Rollback drill (recorded observations)

- [ ] Live-command flag disabled; only the `dashboard` container was
      recreated (container-id diff for trader/strategy/data was empty)
- [ ] Dashboard dropped to read-only: `POST /api/commands/...` returned 403
      with a `COMMANDS_DISABLED` / `LIVE_COMMANDS_DISABLED` code
- [ ] Trading services continued: `mmr --json status` healthy, container
      restart counts unchanged
- [ ] Legacy read-only view exercised at `/` (renders; no active mutation
      surface — CLI remains the fallback command path)

## E. Automatic no-go triggers (all must be clean)

- [ ] No duplicate command (query below returned 0 rows)
- [ ] No unresolved command older than 15 minutes (query below returned 0 rows)
- [ ] No source-coherence failure during soak or live session
- [ ] No audit write failure in trader logs
- [ ] No bypass-capability exposure ([G0] negative security suite green)
- [ ] No violated soak threshold

Ledger queries (run inside the trader container against the trader DB):

```sql
SELECT order_ref, COUNT(DISTINCT command_id) AS n
  FROM command_ledger GROUP BY order_ref HAVING n > 1;

SELECT command_id, state, created_at FROM command_ledger
 WHERE state NOT IN ('RESOLVED', 'REJECTED')
   AND created_at < now() - INTERVAL 15 MINUTE;
```

## F. MMR_WEB_TOKEN credential rotation (§10 — before live enablement)

- [ ] New canonical token generated (never the recycled `MMR_WEB_TOKEN` value)
- [ ] `MMR_WEB_TOKEN` removed from `.env`
- [ ] Old credential verified rejected (URL and header forms)

## G. Live enablement gate (single operator go/no-go)

- [ ] `DASHBOARD_LIVE_ACCOUNT_ID` = ____________ (exact IB account, no wildcard)
- [ ] `DASHBOARD_MAX_ORDER_NOTIONAL` = ____________ (mandatory; no fallback)
- [ ] Existing broker/risk limits confirmed active
- [ ] Go / no-go decision: ____________  Date: ____________

## H. Documentation and operator surface (verified at Task 6)

- [ ] `docs/OPERATIONAL_STATE.md` updated
- [ ] Runbooks and browser bookmarks point at `/cc` (and `/manage`)
- [ ] Health checks reference `/healthz`, `/readyz`, `/api/health` only

## I. Sign-off

Legacy dashboard retirement approved by: ____________  Date: ____________

---

### Execution procedure (recorded into the sections above as it runs)

The full step-by-step operational procedure — the eight-hour soak invocation,
the live read-only session, the rollback drill, the credential rotation, and the
live-enablement gate configuration — is specified verbatim in the COMPAT plan
(`docs/superpowers/plans/2026-07-15-command-center-compat-rollout.md`, Task 5,
Steps 3–8). Those steps require a live paper Compose stack and a real market
session, so they are executed by the operator and their results recorded above;
they are intentionally NOT run from CI. This record is the sole artifact that
gates COMPAT Task 6 (legacy retirement) and live enablement.
