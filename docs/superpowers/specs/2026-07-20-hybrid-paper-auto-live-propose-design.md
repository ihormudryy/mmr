# Hybrid Paper Automation / Live Human-Approve Design

**Date:** 2026-07-20  
**Status:** approved (user chose hybrid option 3)  
**Supersedes (partially):** paper-only gate in `docs/superpowers/specs/2026-07-15-signal-propose-bridge-design.md` §Config semantics — live propose is now an explicit goal under the constraints below.  
**Depends on:** command-plane activation (`2026-07-18-command-plane-activation-design.md`), trading-income foundation (`2026-07-18-trading-income-foundation-design.md`), signal→propose bridge (`2026-07-15-signal-propose-bridge-design.md`).

## Problem

Operators want two different risk postures on the same codebase:

1. **Paper:** unattended deterministic automation (P3) for exactly one signed strategy.
2. **Live:** strategies may queue PENDING proposals; a human must approve every order. No unattended live automation.

Today those postures conflict:

- `auto_execute: propose` is hard-gated to paper (`SignalProposer._gate`) — live signals are ignored.
- Nested `automation:` YAML in `config_defaults/trader.yaml` is easy to copy into user config, but `MMRConfig.from_yaml` only applies **flat** `automation_*` keys (nested `automation:` alone is inert).
- `register_command_authority(..., automated_intent_service=...)` exists, but production `build_command_stack` must actually construct and pass `AutomatedIntentCommandService` or paper automation never registers.
- `_dispatch_signal` can run propose **and** intent emission for the same signal — dual-enable risks double exposure.

## Decision

**Hybrid mode matrix (fail-closed):**

| Concern | Paper | Live |
|---------|-------|------|
| `trading_mode` | `paper` | `live` |
| `command_authority.enabled` | true | true |
| `command_authority.live_enabled` | false | true (pinned `live_account_id` + `max_order_notional`) |
| `automation.enabled` / `automation_enabled` | true (one strategy) | false (recommended) |
| `automation.live_enabled` / `automation_live_enabled` | false | **always false** |
| Automated strategy | IntentEmitter only; `auto_execute` not `propose` | N/A (or propose-only if listed; never emitter) |
| Other strategies | `propose` or off | `auto_execute: propose` → human approve |
| Order path | `execute_automated_intent` → protective saga | `approve_proposal` only |

**Invariant:** automation never places live orders. Live exposure increases only through authenticated approve (with live preflight ceremony where required).

## Binding rules

### R1 — Dispatch exclusivity

For a given strategy name on a given completed bar:

- If IntentEmitter is armed for that name → emit intent; **do not** call `SignalProposer`.
- Else if `auto_execute == 'propose'` → propose only.
- Else → record/publish signal only.

Never both propose and auto-intent for the same signal.

### R2 — Live propose gate (replaces paper-only silence)

`SignalProposer` may create proposals when **any** of:

- `paper_trading` is true, or
- trader is live **and** command authority policy has `enabled=true` and `live_enabled=true` with a validated account pin.

Otherwise refuse loudly (log once per strategy + skip), same as today on live without authority.

Live propose still goes through typed `create_proposal`; approve remains the only execution step.

### R3 — Automation live refusal

- Startup refuses `automation_live_enabled=true` until a future canary program explicitly opts in (out of scope here).
- IntentEmitter already refuses live intents unless `live_enabled`; keep that and add trader-side refuse if account mode is live.
- If `trading_mode=live` and `automation_enabled=true`, startup may allow the flag to load for config sharing but **must not** arm IntentEmitter (`account_mode=live` + `live_enabled=false`). Prefer documenting `automation_enabled: false` on live deployments.

### R4 — Config loading

Support both:

- Flat keys: `automation_enabled`, `automation_live_enabled`, …
- Nested map: `automation: { enabled: … }` (merge into `AutomationConfig` in `from_yaml`)

Release-gate scripts and runtime must agree. Document the preferred user-config form as nested `command_authority` + nested `automation` **after** the loader fix; keep flat keys as env-override compatible aliases.

### R5 — One paper automated strategy

- Exactly one `automation_strategy_name`.
- That strategy’s YAML entry sets `params.artifact_bundle_path` to the signed bundle dir.
- That strategy must **not** use `auto_execute: propose` while automation is armed (R1 makes this redundant but config review should still fail load or warn).

## Components

### 1. Live-capable `SignalProposer`

- Inject or query whether live command authority is enabled (policy snapshot or constructor flag from strategy_runtime).
- Replace `_gate` paper-only check with R2.
- Tests: paper always on; live without live_enabled → skip + warn; live with live_enabled → create_proposal called.

### 2. Production automation wiring

- `build_command_stack` constructs `AutomatedIntentCommandService` when command authority is enabled **and** automation is enabled (paper), with artifact verifier + protective saga ports.
- Pass into `register_command_authority(..., automated_intent_service=...)`.
- Capability manifest includes `execute_automated_intent` only when the service is real.

### 3. Config + Container

- `MMRConfig.from_yaml` reads nested `automation:` block.
- Startup validation: live + `automation_live_enabled` → hard error; paper + missing artifact fields when automation enabled → hard error.

### 4. Strategy YAML / ops

- Paper activation: one automated strategy (e.g. `orb_googl`) with artifact path; other strategies remain propose or disabled.
- Live activation: strategies use `auto_execute: propose`; automation off; `command_authority.live_enabled` with pin + notional.

### 5. Signed paper artifact + soak

- Generate ops Ed25519 keypair; public PEMs in `~/.config/mmr/keys/`.
- Export one `PAPER_ELIGIBLE` bundle under `~/.local/share/mmr/artifacts/<artifact_id>/` (fixture OK for activation drill; not P4 evidence unless re-attested from qualified research).
- Sequence: P1 synthetic → P1 IB-paper soak (authority on, automation off) → enable automation + artifact → P3 synthetic → P3 IB-paper soak.

## Non-goals

- Unattended live / canary (`automation_live_enabled`).
- Multi-strategy paper automation.
- Full `auto_execute: true` without the signed-artifact path.
- Treating fixture/synthetic attestations as P4 promotion evidence.
- Enabling `DASHBOARD_LIVE_COMMANDS_ENABLED` without the live account pin (dashboard remains a separate UI kill switch).

## Verification

| Gate | Command / test |
|------|----------------|
| Exclusivity | Unit: same signal never propose+intent |
| Live propose | Unit: live + live_enabled creates proposal; live without skips |
| Config nested | Unit: nested `automation:` populates `AutomationConfig` |
| Stack wiring | Integration: enabled paper stack registers `execute_automated_intent` |
| Synthetic | `scripts/p1_release_gate.py --synthetic-only`, `scripts/p3_release_gate.py --synthetic-only` |
| Manual paper | `p1_release_gate.py --ib-paper`, then `p3_release_gate.py --ib-paper` during XNYS RTH |
| Live propose ops | Paper-validated first; live session with propose→approve only (no automation) |

## Success definition

- Paper: one signed strategy places protected orders without human approve; other strategies do not auto-execute.
- Live: strategies emit PENDING proposals; human approve places orders; automation cannot submit.
- No dual-path double exposure; no silent ignore of live propose when authority is correctly enabled.

## Open follow-ups (not blocking this design)

- Exit-trigger round-trip on proposals (`max_hold_bars` / `close_by_time`) remains a known gap from the propose-bridge spec.
- `reproduce_experiment.py` is still scaffolding; P2 reproduction gate is separate.
- Dashboard pause/resume has no CLI twin.
