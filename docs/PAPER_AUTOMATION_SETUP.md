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
| Other strategies queue trades for you | Yes — `auto_execute: propose` → `approve` (CLI or dashboard) |
| Same automation on **live** | No — `automation.live_enabled` must stay `false` |

---

## Info you need before starting

### Interactive Brokers

- Paper username / password
- Paper account id (usually `DU…`)
- Market-data subscriptions for the symbols you’ll trade
- Confirmation you’re OK with paper only (`TRADING_MODE=paper`)

### Strategy choice (pick one for automation)

- Strategy **name** as it appears in `strategy_runtime.yaml` (e.g. `orb_googl`)
- Module / class / `conids` / `bar_size` already validated in backtests
- That strategy must **not** use `auto_execute: propose` while automation is armed (rule **R1**)

### Host / Docker

- Working Docker split stack (`./docker.sh -g` or `-b -u`)
- `MMR_HMAC_SECRET` in `.env` (typed RPC)
- Writable: `~/.config/mmr/`, `~/.local/share/mmr/` (artifacts + logs)

### Optional API keys

Massive and/or TwelveData if you still scan/download US data — not required for
the automation order path itself.

### Risk knobs you’ll set

- Position sizing / daily loss / max positions (`position_sizing.yaml`, risk gate)
- Willingness to run release gates (synthetic + one RTH paper soak)

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
- `MMR_HMAC_SECRET` set
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

#### Preferred — dashboard (Phase 2 hot-arm)

1. Open the web dashboard / command center **Scaling** tab
2. **Paper automation** → select the strategy
3. **Activate paper automation** (preflight / confirm)
4. Expect `lifecycle=armed` (no restart). If you see `armed_unpersisted`, click Activate again to finish YAML persist. `restart_required` only appears for incomplete configs or until services have loaded YAML-enabled automation after a cold start.
5. Confirm lifecycle shows **armed** (not `armed_unpersisted` / `failed`)

See also [`DASHBOARD_USER_GUIDE.md`](DASHBOARD_USER_GUIDE.md).

#### Offline equivalent

```bash
python3 scripts/bootstrap_paper_automation.py --strategy-name YOUR_STRATEGY
```

That creates (never commit these):

- `~/.config/mmr/keys/private/signing.pem`
- `~/.config/mmr/keys/verify/*.pem`
- `~/.local/share/mmr/artifacts/<artifact_id>/`

Paste what the script prints into user config, e.g.:

```yaml
automation:
  enabled: true
  live_enabled: false
  artifact_bundle_path: /Users/you/.local/share/mmr/artifacts/<id>
  public_key_ring_path: /Users/you/.config/mmr/keys/verify
  expected_artifact_id: <id>
  strategy_name: YOUR_STRATEGY   # exact name, only one
```

And on that strategy entry in `strategy_runtime.yaml`:

```yaml
params:
  artifact_bundle_path: /Users/you/.local/share/mmr/artifacts/<id>
# do NOT set auto_execute: propose on this strategy
```

Restart trader + strategy again.

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

## Checklist summary

1. Paper IB + Docker healthy
2. One validated strategy deployed
3. `command_authority.enabled: true`, `live_enabled: false`
4. P1 synthetic (+ paper soak)
5. Activate / bootstrap automation for **that one** name
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
