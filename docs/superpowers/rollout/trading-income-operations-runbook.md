# Trading-Income Operations Runbook — P1 Command Plane + P3 Automation

> Scope: activating the trader-owned command authority (proposal → approve →
> broker order → correlated terminal state) in **paper** mode, and the P3
> one-strategy deterministic automation vertical slice (also **paper** only).
> Live activation is out of scope for P1/P3 and stays refused at startup /
> `automation.live_enabled: false`.
>
> Design: `docs/superpowers/specs/2026-07-18-command-plane-activation-design.md`
> Plan (P1): `docs/superpowers/plans/2026-07-18-trading-income-p1-safety-command-plane.md`
> Plan (P3): `docs/superpowers/plans/2026-07-18-trading-income-p3-deterministic-automation.md`

## What "activated" means

The command authority is the ONLY production surface that turns a human/LLM
approval into a real broker order. When disabled (the default), the trader is
read-only + strategy-state-ack: no proposal can be approved into an order. When
enabled in paper, `approve_proposal` runs the full risk-gated, generation-fenced,
pre-dispatch-revalidated saga and dispatches through `TradingRuntimeOrderDispatch`.

P3 automation adds a second *typed* entry into that same coordinator:
`execute_automated_intent` (strategy-service principal only). Strategies emit
canonical `ExecutionIntent` messages after completed session-valid bars; they
never construct IB orders, never use legacy dill RPC, and never mutate the
domain journal. The trader re-verifies the signed artifact, claims the
deterministic command ID, runs session/liquidity risk, and dispatches the
protective-order saga on the existing expressive-order path.

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

### P3 automation config (defaults — all false/empty)

```yaml
automation:
  enabled: false
  live_enabled: false          # stays false through P3; P4 owns live canary
  artifact_bundle_path: ''     # e.g. ~/.local/share/mmr/artifacts/<id>
  public_key_ring_path: ''     # directory of trusted *.pem verification keys
  expected_artifact_id: ''     # exact artifact id; no fuzzy match
  strategy_name: ''            # exact one-strategy name allowed to emit intents
```

Compose mounts `~/.local/share/mmr/artifacts` read-only into `trader` and
`strategy`. Do not enable `automation.enabled` until the synthetic automation
drill is green **and** the manual IB paper soak (below) is recorded.

## The release gate — two halves, both required

Run the unified gate:

```bash
python3 scripts/p1_release_gate.py --synthetic-only --json --output p1-synthetic.json
# Full manual gate during XNYS RTH with command authority enabled (automation OFF):
python3 scripts/p1_release_gate.py --ib-paper --watch-minutes 390 --json --output p1-manual-soak.json
```

### 1. Synthetic failure drills

#### P1 command plane — `scripts/command_plane_drill.py`

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

#### P3 automation vertical slice — `scripts/automation_paper_drill.py`

In-process battery over intent emission → coordinator → protective saga →
broker events → attribution → flatten → sealed replay. No IB. Proves:

- duplicate completed bar → identical intent/command → one bracket submit;
- typed `execute_automated_intent` only (no legacy mutation path);
- stale quote / rejected stop / ambiguous submit / disconnect+duplicate /
  crash-restart / missed deadline → breaker or no duplicate exposure;
- sealed forensic replay matches.

```bash
python3 scripts/automation_paper_drill.py
python3 scripts/automation_paper_drill.py --json --output automation-drill.json
python3 scripts/automation_paper_drill.py --soak-seconds 120   # ~2 min synthetic soak
pytest tests/integration/test_automated_vertical_slice.py tests/automation/ -q --timeout=60
```

### 2. Manual IB-paper session soak — non-fungible P3 release gate

During market hours, against the real paper stack, run **one complete IB paper
session** with automation allocation set to the minimum safe test size, and
record:

- sealed replay bundle path + manifest digest
- automation drill report path (synthetic half)
- `~/.config/mmr/trader.yaml` automation block (enabled, artifact id, strategy name)
- `git rev-parse HEAD` commit digest
- config digest (`sha256` of the deployed trader.yaml)

Suggested soak driver (P1 soak script; extend with automation allocation notes):

```bash
python3 scripts/run_paper_soak.py   # during RTH; real IB paper profile
```

**This manual IB-paper session soak is the P3 release gate.** Synthetic drills
and the in-process vertical-slice suite do **not** replace it. Do not claim P3
complete, and do not treat soak data as P4 promotion evidence, unless the soak
is fully qualified under the research attestation rules.

Also run before sign-off:

```bash
python3 scripts/p3_release_gate.py --json --output p3-gate.json
# CI / pre-RTH: synthetic half only (manual soak may stay pending)
python3 scripts/p3_release_gate.py --synthetic-only --json --output p3-synthetic.json
# Full manual gate during XNYS RTH with automation enabled:
python3 scripts/p3_release_gate.py --ib-paper --watch-minutes 390 --json --output manual-soak.json
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
| `stale_quote_blocks_dispatch` | live approval refuses a stale executable quote (QUOTE_STALE); nothing dispatches |
| `notional_cap_blocks_dispatch` | the DispatchGuard rejects an over-ceiling notional (ORDER_NOTIONAL_LIMIT) before any dispatch |
| `liquidation_flat_only_from_broker_truth` | FLAT needs a fresh broker snapshot with zero positions and working orders; never an RPC ack |
| `circuit_breaker_trips_and_persists` | a critical liquidation failure trips and persists the breaker |
| `semantic_readiness_gates_activation` | a tripped breaker makes automation semantically unready |

### P3 automation drill scenarios

| Scenario | Proves |
|----------|--------|
| `happy_path` | bar → identical intent on replay → one protected order → attribution → sealed replay |
| `stale_quote` | DispatchGuard QUOTE_STALE; no bracket |
| `rejected_stop` | broker reject → REJECTED/UNKNOWN; no duplicate exposure |
| `ambiguous_submission` | OUTCOME_UNKNOWN; exact replay does not re-dispatch |
| `disconnect_duplicate_event` | duplicate broker event is idempotent |
| `crash_restart` | PROTECTED saga resumes; no second submit |
| `missed_deadline` | FLAT_DEADLINE_MISSED trips breaker |
| `emitter_no_journal_mutation` | non-allowlisted strategy cannot emit; no legacy RPC |

Automation stays prohibited until all drills are green and the manual IB-paper
soak is signed off.

## P4 paper / canary soak (operational)

Software cannot pass these gates. Use the sanitized templates and fill only digests:

- Paper: [`trading-income-paper-log.md`](trading-income-paper-log.md) — ≥30 calendar days, 20 sessions, 50 RT, 5 instruments
- Canary: [`trading-income-canary-log.md`](trading-income-canary-log.md) — ≥30 live sessions, 75 RT, 5 instruments, zero capital-safety incidents

Daily: run `scripts/session_open_check.py` / `scripts/session_close_check.py`, seal replay, and never edit counters manually after a failed gate.

## P5 scaling rollback

- **Immediate scale-down:** run `scripts/scaling_fault_drill.py --json` offline to
  validate degradation/risk gates, then apply a restrictive allocation override
  via the degradation monitor path (never increases authority).
- **Suspend trading exposure:** `pause_trading` (no preflight) plus
  `deactivate-canary` / allocation override to zero gross ceiling.
- **Authority revocation:** revoke signed allocation/canary keys in the offline
  key ring; restart trader_service so verifiers reload trusted keys.
- **Return to paper:** disable live allocation activation config, redeploy paper
  artifact bundle, and confirm `scaling.status` in `/api/snapshot` reads
  `unknown` or `inactive` before resuming research.

## Rollback / kill switch

- **Immediate:** set `DASHBOARD_COMMANDS_ENABLED=false` (UI) and pause new
  exposure via `pause_trading` (risk-reducing; no preflight required).
- **Automation off:** set `automation.enabled: false` (and/or clear
  `automation.strategy_name`) and restart strategy_service — intent emission
  stops; in-flight commands remain durable in the trader ledger.
- **Full disable:** set `command_authority.enabled: false` and restart the
  trader. In-flight commands remain durable in the ledger and are resolved by the
  reconciler on next start — disabling the authority does not orphan them.
