# Trading-Income Operations Runbook — P1 Command Plane

> Scope: activating the trader-owned command authority (proposal → approve →
> broker order → correlated terminal state) in **paper** mode. Live activation
> is out of scope for P1 and stays refused at startup.
>
> Design: `docs/superpowers/specs/2026-07-18-command-plane-activation-design.md`
> Plan: `docs/superpowers/plans/2026-07-18-trading-income-p1-safety-command-plane.md`

## What "activated" means

The command authority is the ONLY production surface that turns a human/LLM
approval into a real broker order. When disabled (the default), the trader is
read-only + strategy-state-ack: no proposal can be approved into an order. When
enabled in paper, `approve_proposal` runs the full risk-gated, generation-fenced,
pre-dispatch-revalidated saga and dispatches through `TradingRuntimeOrderDispatch`.

## Activation prerequisites (paper)

Do NOT set `command_authority.enabled: true` until ALL of these hold:

1. **Config is coherent and trader-owned.** `command_authority.live_enabled:
   false`; `live_account_id` empty; startup validates the policy fail-closed
   (`load_and_validate_command_policy`). A live authority without a matching
   account + notional ceiling refuses to start — this is intended.
2. **The synthetic drill gate is green** (see below) at the deploying commit.
3. **A manual IB-paper session soak has been recorded** for the deploying commit
   (see below). This gate is non-fungible: synthetic drills do not replace it.
4. **Journal + broker state are healthy.** The domain journal is writable, a
   promoted broker generation exists, and reconciliation backlog is clear.
5. **The dashboard kill switch is understood.** `DASHBOARD_COMMANDS_ENABLED` is
   only a UI kill switch; risk-increasing commands are gated by the trader
   policy regardless. Turning the UI flag off hides the buttons; it does not
   relax the trader-side gate.

Keep `command_authority.enabled: false` in `config_defaults/trader.yaml`. Enable
per-deployment via the user config (`~/.config/mmr/trader.yaml`) or env, never in
the checked-in defaults.

## The release gate — two halves, both required

### 1. Synthetic failure drills — `scripts/command_plane_drill.py`

In-process battery over a real DuckDB journal + command ledger + coordinator +
proposal/approval services + reconciler, behind deterministic fake broker/quote/
order ports. No IB, no live dispatch. Proves the recovery invariants:

- **one idempotent command history** — a command_id resolves to exactly one
  terminal ledger row; an exact replay mints no second proposal/order;
- **durable audit** — every command transition is journalled;
- **no duplicate order reference** — recovery never re-dispatches an order_ref;
- **coherent terminal state** — ledger state and proposal status agree.

Run it:

```bash
python3 scripts/command_plane_drill.py                 # human summary + exit code
python3 scripts/command_plane_drill.py --json --output drill.json
python3 scripts/command_plane_drill.py --scenarios happy_path,restart_unresolved
```

Exit code is non-zero if any runnable scenario fails, so it is a CI/pre-activation
gate. The JSON report carries `commit_digest` + `config_digest` — staple it to the
release record. A release is blocked if any required scenario fails or is
reported `pending`; scenarios are never silently skipped.

The pytest wrapper (also part of the gate):

```bash
pytest tests/integration/test_command_plane_activation.py -q
```

### 2. Manual IB-paper session soak — `scripts/run_paper_soak.py`

During market hours, against the real paper stack, run one complete session soak
and record the report path, commit digest, and config digest. This exercises the
real IB feed, the real dispatch path, and real reconciliation latency — none of
which the synthetic drills cover. **This manual gate may not be replaced by
synthetic fixtures.**

Also run before sign-off:

```bash
pytest tests/ --timeout=30 -q --ignore=tests/test_ibrx_async.py   # canonical suite
docker compose config --quiet                                     # compose validity
```

## Recovery invariants the drills exercise

| Scenario | Proves |
|----------|--------|
| `happy_path` | create → approve → submit → resolve; one order; proposal EXECUTED |
| `duplicate_create_idempotent` | exact replay returns the recorded receipt; one proposal |
| `ambiguous_submit_reconciles` | a lost ack → OUTCOME_UNKNOWN (never false SUBMITTED); reconciler resolves from broker truth with no re-send |
| `restart_unresolved` | crash between claim and ack → rescan_on_startup + reconcile after restart; exactly one order, no resubmission |

| `liquidation_flat_only_from_broker_truth` | FLAT needs a fresh broker snapshot with zero positions and working orders; never an RPC ack |
| `circuit_breaker_trips_and_persists` | a critical liquidation failure trips and persists the breaker |
| `semantic_readiness_gates_activation` | a tripped breaker makes automation semantically unready |

Automation stays prohibited until all drills are green and the manual IB-paper
soak is signed off.

## Rollback / kill switch

- **Immediate:** set `DASHBOARD_COMMANDS_ENABLED=false` (UI) and pause new
  exposure via `pause_trading` (risk-reducing; no preflight required).
- **Full disable:** set `command_authority.enabled: false` and restart the
  trader. In-flight commands remain durable in the ledger and are resolved by the
  reconciler on next start — disabling the authority does not orphan them.
