# MMR Operational State

Living snapshot of the **deployed/running** state (config, strategies, data, infra)
and the reasoning behind it. Distinct from `AUDIT_ROADMAP.md` (code backlog).
Update the date + relevant sections when the running config changes.

How to arm unattended paper automation from scratch:
[`PAPER_AUTOMATION_SETUP.md`](PAPER_AUTOMATION_SETUP.md).

**Last updated: 2026-08-18 (Tue) — paper trading, command-center stack.**

---

## How this stack actually trades (2026)

The July 5 notes below described five strategies with `auto_execute=True`.
That path is **obsolete**: `auto_execute: true` is refused at load.

Current model:

| Mode | What happens |
|------|----------------|
| **Paper automation** | Exactly **one** strategy (`automation.strategy_name`) may place protected orders without human approve (`execute_automated_intent`). |
| **Propose** | Other strategies may set `auto_execute: propose` → PENDING cards on `/cc` for approve/reject. |
| **Live** | `automation.live_enabled` stays `false`. Human approve only. |

Config (host, not git): `~/.config/mmr/trader.yaml` + `strategy_runtime.yaml`.

- `command_authority.enabled: true`, `live_enabled: false`
- `automation.enabled: true`, `strategy_name: momentum`
- `momentum` must **not** also have `auto_execute: propose` (rule R1)

Dashboard: Scaling tab → `lifecycle=armed` for momentum. Trading tab Strategies
list comes from journaled `strategy.updated` (not from host `mmr strategies`).

**Host CLI caveat:** `mmr strategies` from the Mac cannot reach strategy typed
ports 42104/42105 (private Compose network). Use:

```bash
docker compose exec trader python -m trader.mmr_cli strategies
```

or the `/cc` Strategies panel. Trader ports 42101/42102 **are** published, so
`mmr status` / `portfolio-snapshot` / `propose` / `approve` work from the host.

---

## Armed paper automation (current)

| Strategy | Role | Notes |
|---|---|---|
| **momentum** | Single auto slot | Artifact bundle on disk; `auto_execute` off. Enable it before a soak (`INSTALLED` ≠ dispatchable). |
| orb_* / ensemble | Optional propose | Human review on `/cc` if `auto_execute: propose`. |
| global | Always present | Enable/Disable/Undeploy hidden by design. |

Kill switches: Scaling **Deactivate**; `pause_trading`; `automation.enabled: false` + restart strategy.

---

## Historical note — 2026-07-05 paper book (superseded)

The following was the **old** auto-execute book on account `DUM422056`. Kept as
backtest evidence only — do **not** re-arm with `auto_execute=True`.

| Strategy | Sym | conId | Class | 1yr return | PF | Notes |
|---|---|---|---|---|---|---|
| orb_googl | GOOGL | 208813719 | OpeningRangeBreakout | +13.9% | 2.45 | strongest |
| orb_pltr | PLTR | 444857009 | OpeningRangeBreakout | +7.5% | 1.44 | |
| orb_wds | WDS | 564155292 | OpeningRangeBreakout | +7.1% | 1.68 | ASX |
| vwap_reclaim_cat | CAT | 5437 | VwapReclaim | +4.7% | 1.44 | |
| orb_bhp | BHP | 4036812 | OpeningRangeBreakout | +4.7% | 1.26 | ASX |

Disabled after that session (do not re-arm without new evidence): orb_cba,
orb_rio, orb_fmg, orb_csl, orb_gld (losing), orb_xlk (too much drawdown).

---

## Backtest findings (2026-07-04/05, 1yr 1-min)

- **ORB on ASX is marginal universe-wide.** Per-name edge only (BHP, WDS).
- **US ORB:** GOOGL strong; GLD losing.
- **VWAP:** CAT only among names tried.
- Treat return + PF as the reliable metrics until `expectancy_bps` is reconciled.

---

## Data coverage

- **ASX 1-min:** 53 universe symbols, ~1 year as of 2026-07-02.
- **US 1-min:** deep history for the historically deployed names.
- DuckDB lives in named volume `mmr_db_data`. Backup: `./docker.sh -B` /
  `mmr data backup`. Nightly pycron `db_backup`.

---

## Infrastructure

- Split compose: ib-gateway, trader, strategy, data, dashboard, scheduler.
- Restart policy: `unless-stopped`.
- Paper mode; typed HMAC RPC. Dashboard on host loopback (default 7424).

---

## Next operator session (paper soak)

1. `./docker.sh -b -u` after this polish (baked dashboard image).
2. Confirm `mmr status` → IB upstream connected.
3. Enable **momentum** from `/cc` or `docker compose exec trader … strategies enable momentum`.
4. Scaling: `lifecycle=armed`, operating mode Auto.
5. Leave the stack up through US RTH. Watch intents / protectives / fills.
6. Host monitors: `mmr --json portfolio-snapshot`, `portfolio-diff`, `/cc` Risk bars.

Do not arm a second automatic strategy. Do not set `automation.live_enabled`.
