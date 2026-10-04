# Paper automation setup guide

Operator guide for running **unattended paper trading** on MMR as implemented
today. For deep release-gate / recovery detail see
[`superpowers/rollout/trading-income-operations-runbook.md`](superpowers/rollout/trading-income-operations-runbook.md).
Design: [`superpowers/specs/2026-07-20-hybrid-paper-auto-live-propose-design.md`](superpowers/specs/2026-07-20-hybrid-paper-auto-live-propose-design.md).

Living deployed state (armed names, accounts) lives in
[`OPERATIONAL_STATE.md`](OPERATIONAL_STATE.md) — update that when you arm something.

---

## What “fully automatic” means here

| Goal | Supported? |
|------|------------|
| **One** paper strategy places protected orders without human approve | Yes — **paper automation** (`execute_automated_intent`) |
| Many strategies all auto-trade unsupervised | No — **exactly one** `automation.strategy_name` |
| `auto_execute: true` (blind auto) | No — refused at load |
| Other strategies queue trades for you | Yes — `auto_execute: propose` → PENDING; on **paper** the LLM may evaluate then approve/reject; on **live** a human must approve |
| Same automation on **live** | No — `automation.live_enabled` must stay `false` |
| LLM approve on paper after evaluation | Yes — not blind auto-approve; see `2026-07-23-paper-llm-approve-live-human-design.md` |
| LLM approve on live | No — `LLM_LIVE_APPROVE_FORBIDDEN`; Command Center + preflight only |

Paper LLM evaluate-approve (`approve_proposal` with `source=sdk`) is **not** the same as paper automation (`execute_automated_intent`).

---

## Info you need before starting

### Interactive Brokers

- Paper username / password
- Paper account id (usually `DU…`)
- Market-data subscriptions for the symbols you’ll trade
- Confirmation you’re OK with paper only (`TRADING_MODE=paper`)

### Strategy choice (pick one for automation)

- Strategy **name** as it appears in `strategy_runtime.yaml` (e.g. `orb_googl`)
- Module / class / `conids` / `bar_size` already evaluated: you need a signed bundle
  for exactly this strategy (see [Evidence before automation](#evidence-before-automation))
- That strategy must **not** use `auto_execute: propose` while automation is armed (rule **R1**)

### Host / Docker

- Working Docker split stack (`./docker.sh -g` or `-b -u`)
- `~/.config/mmr/service_hmac.key` (mode `0600`), exposed to services through
  `MMR_SERVICE_HMAC_KEY_FILE` (typed RPC)
- Writable: `~/.config/mmr/`, `~/.local/share/mmr/` (artifacts + logs)

### Optional API keys

Massive and/or TwelveData if you still scan/download US data — not required for
the automation order path itself.

### Risk knobs you’ll set

- Position sizing / daily loss / max positions (`position_sizing.yaml`, risk gate)
- Willingness to run release gates (synthetic + one RTH paper soak)

---

## Evidence before automation

> Phase A leaves the liquidity, benchmark and regime evidence missing, so
> `evaluate` stops at stage `pre_holdout` (state `CANDIDATE`) until Phase B and
> Activate refuses every strategy. Even with an eligible bundle, automated exits
> are not safe yet, and `research evaluate` does not run in split Docker: see
> **Known blockers (paper automation)** in
> [`OPERATIONAL_STATE.md`](OPERATIONAL_STATE.md#known-blockers-paper-automation).

1. Write a spec (see `research/example_spec.yaml`): strategy file (inside
   `strategies/`, committed and clean), class, params, a neighbourhood, at least
   8 distinct XNYS conids, bar size (15 minutes or shorter), period, walk-forward
   settings, order notional and account equity. You also need 1-min (or your bar
   size) bars in the local DuckDB for every conid (`mmr data download` /
   `mmr data refresh`), every conid in a local universe, and a `calendar: XNYS`
   key on the US venue in `~/.config/mmr/execution_costs.yaml` (older copies lack it;
   `evaluate` refuses and names the key).
2. `mmr research evaluate research/my_spec.yaml --dry-run` — validate, count jobs.
   It does not read bars. `--workers N` sets the parallel backtest processes.
3. `mmr research evaluate research/my_spec.yaml` — runs walk-forward backtests at
   realistic costs (1x, 1.5x, 2x for the main point; neighbours at 1x) under the
   live paper rules. The holdout is opened only if every other paper-v1 rule
   passes. A failed holdout (no round trip, expectancy at or below zero, or a
   drawdown over 3%) retires the artifact: stage `holdout_failed`, state
   `RETIRED`, no decision, and it can never be attested. It prints the report
   path; read it. The report is written to
   `~/.local/share/mmr/reports/evaluation_<name>_<time>_<id>.md` (and `.json`).
4. If the stage is `complete` and the state `PAPER_ELIGIBLE`:
   `mmr research review submit --artifact-id ... --decision-id ... --reviewer-kind human|llm ...`
   (`--artifact-id` is the evaluate output's `artifact_id`, `--decision-id` its
   `decision_digest`. The review also needs `--reviewer`, the eight review fields
   and `--holdout-opened-once`. An LLM may review paper bundles; live needs a
   human. The reviewer name `bootstrap` marks old fixtures and is refused at
   Activate and dispatch.)
5. `mmr research attest bundle <artifact_id>` — signs and exports
   `~/.local/share/mmr/artifacts/sha256_<digest>/` (the digest is the bundle
   manifest digest). The first run also creates the signing key under
   `~/.config/mmr/keys/`.
6. Activate from `/cc`. It refuses (`NO_ELIGIBLE_BUNDLE`) unless that bundle is
   bound to the strategy's current YAML entry and file. Editing the strategy
   file, its upper-case params, conids or bar size needs a new evaluation. An
   automated strategy cannot set lower-case params at all: the spec refuses
   them, so the binding refuses any lower-case key in the YAML entry. The
   refusal quotes the latest evaluation for that strategy.

Bundles expire after 90 days. A decision is attested only once, so
`research attest bundle` refuses to export an expired attestation again, or one
signed by a different key than the current signing key. Renewal means
evaluating again over a newer period, then reviewing and attesting the new
artifact. `mmr research evaluations` lists past runs (`--limit N`, default 20).

---

## Step-by-step

### 1. Bring up paper Docker

```bash
cd /path/to/mmr
./docker.sh -g   # first time: prompts for IB creds → writes .env
# or after code changes:
./docker.sh -b -u
```

Confirm in `.env` / compose:

- `TRADING_MODE=paper`
- `IB_ACCOUNT=<your DU… paper account>`
- `~/.config/mmr/service_hmac.key` exists with mode `0600` and is mounted as
  `MMR_SERVICE_HMAC_KEY_FILE`
- Later: `DASHBOARD_COMMANDS_ENABLED=true` if you’ll Activate from the UI

Gateway up, VNC if needed (`vnc://localhost:5901`), `mmr status` shows IB
upstream healthy.

### 2. Deploy / enable the strategy you want automated

```bash
mmr strategies deploy YOUR_STRATEGY --conids <conId> --paper
# or edit ~/.config/mmr/strategy_runtime.yaml
mmr strategies reload
mmr strategies   # confirm it’s listed / enabled
```

Keep historical data warm for that conId (`mmr data refresh` / download).

### 3. Enable command authority (required before any auto order)

In **`~/.config/mmr/trader.yaml`** (user config, **not** `config_defaults/`):

```yaml
command_authority:
  enabled: true
  live_enabled: false   # must stay false for paper automation
```

Restart **trader** (and strategy if you changed strategy YAML).

Without this, approve / automation cannot turn signals into broker orders.

### 4. Run the P1 release gates (command plane)

From the host (project venv) or a service shell as you usually run scripts:

```bash
python3 scripts/p1_release_gate.py --synthetic-only
# During US RTH, with paper IB + authority on, automation still OFF:
python3 scripts/p1_release_gate.py --ib-paper --watch-minutes 390
```

Do **not** arm automation until synthetic is green and you’ve done (or at least
scheduled) the paper soak.

### 5. Arm paper automation (one strategy)

Activation **does not create evidence or signing keys**. It needs a bundle from
`mmr research attest bundle` (see [Evidence before automation](#evidence-before-automation))
under `~/.local/share/mmr/artifacts/sha256_<digest>/` and the matching public key
in `~/.config/mmr/keys/verify/`. `research attest bundle` keeps the private
signing key in `~/.config/mmr/keys/private/`; never commit it. If trader and
strategy run on another host, install only the bundle and the public key ring
there. The activation request needs only `strategy_name` and `reason`, never a
private key or other raw secret.

Activate checks every `sha256_*` bundle and arms the newest (by expiry) that:

- verifies in paper mode: integrity, signature by a trusted key, expiry, not
  `RETIRED`, holdout passed;
- carries qualified research evidence (`require_qualified_research_evidence`):
  a complete, passing, current `paper-v1` decision and no bootstrap or
  offline-fixture provenance;
- is bound to the strategy's current entry: file content hash, `class_name`,
  `params` (exact, both directions, lower-case keys included; only the transport
  key `artifact_bundle_path` is ignored), `conids` and `bar_size`. No fuzzy path,
  parameter-name or identifier matching is performed.

It then writes `automation.artifact_bundle_path` (that bundle directory),
`public_key_ring_path`, `expected_artifact_id` and `strategy_name`, and sets the
strategy's `params.artifact_bundle_path`. If no bundle qualifies it refuses with
`NO_ELIGIBLE_BUNDLE` before any YAML or hot-arm commit; the message quotes the
latest evaluation for the strategy and each bundle's reason. The checks run again
on Activate retries; hot-arm refuses `AUTOMATION_ALREADY_BOUND` when a retry finds
a different bundle than the armed one (Deactivate first). Order dispatch repeats
the signature and qualified-evidence checks on the configured bundle directory.
Other runtime/account/live gates remain in force; a valid bundle does not bypass
them.

A signature proves origin and integrity, **not that metrics were measured**.
The operator must audit the underlying trials, datasets, holdout, costs and
review (the evaluation report). Neither fixture data nor a successful plumbing
drill is promotion evidence.

Strategies that already declare `params.artifact_bundle_path` still **load**
when `automation.enabled` is false (soft-load): they appear in Strategies /
Scaling so you can Activate them. Attestation runs on Activate / when
automation is enabled — not at cold load while disarmed.

#### Preferred — dashboard (Phase 2 hot-arm)

1. Open the web dashboard / command center **Scaling** tab
2. **Paper automation** → select the strategy
3. **Activate paper automation** (preflight / confirm)
4. Expect `lifecycle=armed` (no restart). If you see `armed_unpersisted`, click Activate again to finish YAML persist. `restart_required` only appears for incomplete configs or until services have loaded YAML-enabled automation after a cold start.
5. Confirm lifecycle shows **armed** (not `armed_unpersisted` / `failed`)

See also [`DASHBOARD_USER_GUIDE.md`](DASHBOARD_USER_GUIDE.md).

#### Offline verification / configuration helper

Needs a bundle from `mmr research attest bundle` first (see
[Evidence before automation](#evidence-before-automation)). The script only
prints; it writes nothing:

```bash
python3 scripts/bootstrap_paper_automation.py \
  --bundle ~/.local/share/mmr/artifacts/sha256_<digest> --strategy-name YOUR_STRATEGY
```

It reads the public key that `mmr research attest bundle` created and refuses
unless that key signed the bundle. It then verifies the whole bundle (signature,
expiry, qualified `paper-v1` evidence, no fixture provenance) and prints
**disabled** configuration. It creates no keys or evidence, changes no YAML and
does not arm anything. It does not have the strategy YAML, so Activate checks the
strategy binding. Never commit these:

- `~/.config/mmr/keys/private/signing.pem`
- `~/.config/mmr/keys/verify/*.pem`
- `~/.local/share/mmr/artifacts/sha256_<digest>/`

Paste what the script prints into user config, e.g.:

```yaml
automation:
  enabled: false  # Activate after strategy/evidence preflight
  live_enabled: false
  artifact_bundle_path: /Users/you/.local/share/mmr/artifacts/sha256_<digest>
  public_key_ring_path: /Users/you/.config/mmr/keys/verify
  expected_artifact_id: <id>
  strategy_name: YOUR_STRATEGY   # exact name, only one
```

And on that strategy entry in `strategy_runtime.yaml`:

```yaml
params:
  artifact_bundle_path: /Users/you/.local/share/mmr/artifacts/sha256_<digest>
# do NOT set auto_execute: propose on this strategy
```

Use dashboard Activate after configuring both services. If hot-arm is unavailable,
the activation service returns `restart_required`; restart trader + strategy then.

#### No fixture bundles

Production code no longer exports fixture bundles; the made-up-evidence fixture
lives only in `tests/automation/fixture_bundle.py`. Bundles from older bootstrap
runs (reviewer `bootstrap`, or `evidence_kind: offline_fixture`) are refused even
if their signatures still verify. Do not add fixture public keys to the trust ring.

Activate and order dispatch run `require_qualified_research_evidence(bundle_path)`
from `paper_materials` **after** full `ArtifactVerifier` verification. Strategy
load (cold start) runs verification and the binding check, not this provenance
check. It does not replace signature, expiry, revocation, live/account or risk
verification.

### 6. Run the P3 automation gates

```bash
python3 scripts/p3_release_gate.py --synthetic-only
# During RTH with automation enabled:
python3 scripts/p3_release_gate.py --ib-paper --watch-minutes 390
```

Optional drill:

```bash
python3 scripts/automation_paper_drill.py
```

### 7. Day-to-day operation

- Leave the stack up across the session (`unless-stopped` / host stays on).
- Monitor: dashboard, `mmr --json portfolio-snapshot`, logs under
  `~/.local/share/mmr/logs/`.
- Other strategies may use `auto_execute: propose` if you want human review
  alongside the one auto strategy.
- Long soak evidence (optional promotion path):
  [`superpowers/rollout/trading-income-paper-log.md`](superpowers/rollout/trading-income-paper-log.md).

### 8. Kill switches (know these before you arm)

| Action | Effect |
|--------|--------|
| Dashboard **Deactivate paper automation** | Tears down in-memory arm immediately; clears durable enable |
| `automation.enabled: false` + restart strategy | Stops intent emission |
| `pause_trading` | Risk-reducing pause |
| `DASHBOARD_COMMANDS_ENABLED=false` | Hides UI commands only |
| `command_authority.enabled: false` + restart trader | No new approve/auto dispatch |

---

## Paper Docker e2e

With the paper stack up (`./docker.sh -b -u`, IB upstream connected):

```bash
./scripts/paper_e2e.sh
# optional:
MMR_PAPER_E2E_LIVE_ORDERS=1 ./scripts/paper_e2e.sh
MMR_PAPER_E2E_RESTART=1 ./scripts/paper_e2e.sh -k restart
```

Uses `~/.config/mmr/service_hmac.key` (mode `0600`) and dashboard
`DASHBOARD_TOKEN`.

Stack down → all tests skip (exit 0).

---

## Checklist summary

1. Paper IB + Docker healthy
2. One validated strategy deployed
3. `command_authority.enabled: true`, `live_enabled: false`
4. P1 synthetic (+ paper soak)
5. Evaluated, reviewed and attested bundle for that strategy; then Activate
   **that one** name
6. Restart trader + strategy
7. P3 synthetic (+ paper soak)
8. Monitor + know kill switches

---

## Hybrid mode reminder

| Mode | Automation | Human approve |
|------|------------|---------------|
| **Paper** | One signed strategy → `execute_automated_intent` | Other strategies may use `auto_execute: propose` |
| **Live** | `automation.enabled: false` (never `live_enabled`) | `auto_execute: propose` + `command_authority.live_enabled` |

Rules:

- **R1:** The automated strategy must not also set `auto_execute: propose`.
- **R3:** Never set `automation.live_enabled: true` (startup refuses).
- **R5:** Exactly one `automation.strategy_name`.
