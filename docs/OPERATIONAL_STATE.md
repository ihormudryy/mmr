# MMR Operational State

Living snapshot of the **deployed/running** state (config, strategies, data, infra)
and the reasoning behind it. Distinct from `AUDIT_ROADMAP.md` (code backlog).
Update the date + relevant sections when the running config changes.

How to arm unattended paper automation from scratch:
[`PAPER_AUTOMATION_SETUP.md`](PAPER_AUTOMATION_SETUP.md).

**Last updated: 2026-10-04 (Sun) — paper trading, command-center stack.**

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
ports 42104/42105 (private Compose network). Use the one-shot `cli` service
(it holds `cli.key`, `trader.pub` and `strategy.pub` only):

```bash
docker compose run --rm cli strategies
docker compose run --rm cli strategies enable momentum
```

or the `/cc` Strategies panel. Exec'ing the CLI inside the `trader` container
is refused: no long-lived container holds the `cli` key. Trader ports
42101/42102 **are** published, so `mmr status` / `portfolio-snapshot` /
`propose` / `approve` work from the host (signed as `cli`).

---

## Armed paper automation (current)

> **Since 2026-10 (real paper evidence, Phase A):** Activate only arms a bundle
> produced by `mmr research evaluate` → `research review submit` →
> `research attest bundle`, bound to the strategy's file, class, params, conids
> and bar size, with qualified (non-fixture) `paper-v1` evidence. The old fixture
> bundle fails both the provenance and the binding check, so the `momentum` arm
> stops after this ships. Phase B is implemented: `research evaluate`
> computes the liquidity, benchmark and regime evidence from SPY daily bars, so
> eligibility now depends on the data (SPY bars are required; download them
> first). The soak stays blocked by the automated-exit fix (and the split-Docker evidence gap below). Run strategies
> with `auto_execute: propose` meanwhile.

### Known blockers (paper automation)

Armed paper automation cannot exit safely yet, and the evidence step has a gap in split Docker.

- **Automated exits are not safe.**
  - A SELL without a quantity is rejected at approval (`QUANTITY_REQUIRED` in
    `trader/automation/production_evidence.py`), before `session_risk` could
    size it to the held position. An exit signal without a size therefore does
    not close an automated position; the entry's protective stop or a manual
    close does.
  - A SELL with a quantity goes through `build_bracket_plan`
    (`trader/automation/protective_order_saga.py`), which attaches a reverse BUY
    stop. The entry bracket's protective SELL stop also stays working, because
    `reducible_quantity` (`trader/data/broker_state.py`) sums positions and
    ignores working orders; approval's `LONG_ONLY` check (sell ≤ held) does not
    cancel it. A full close could later re-open a long or leave the paper account
    short. The fix needs a plain close order that cancels the entry's protective
    stop, and a quantity for an unsized SELL from the fenced position.
- **`research evaluate` does not run in split Docker yet.** The bars and the
  research DB live in the `mmr_db_data` volume, but the read-only trader
  container does not mount `~/.local/share/mmr/reports`, where the command writes
  its report. The image also has no `.git` (`.dockerignore`), so the family
  commit would be `unknown`. Run it on the host against copies of both the
  history DB (bars) and the main DB (universes); it also writes the research DB
  (`research_duckdb_path`, or `MMR_RESEARCH_DUCKDB`), so point that at a host
  path. Or fix the mounts in a follow-up.

Resolved: the production approval no longer builds an empty broker snapshot.
`production_evidence.py` (2026-10-04) captures the fenced broker snapshot, a live
executable quote and what-if margin, a durable high-water mark, 20-session
liquidity from local daily bars, and the attested allocation (capped at 6%). So
an automated BUY also needs a live quote in continuous trading and the 20 latest
closed daily TRADES bars for the conid in the local DB, or approval rejects it
(`FEED_NOT_LIVE`, `QUOTE_SESSION_INVALID`, `HISTORY_INVALID`, ...).

### Current arms

| Strategy | Role | Notes |
|---|---|---|
| **momentum** | Single auto slot | Armed with the old fixture bundle, which no longer passes the provenance or binding check (see the 2026-10 note). `auto_execute` off. Enable it before a soak (`INSTALLED` ≠ dispatchable). |
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

- **ORB on ASX is marginal universe-wide.** Top-20 leaderboard: 11/20 positive,
  median PF 0.89, negative median per-trade expectancy. Only BHP (PF 1.26) and
  WDS (PF 1.68) have a genuine per-name edge. **Param sweep** (RANGE_MINUTES ×
  VOLUME_MULT) confirmed the DEFAULTS (30 / 1.5) are already the best combo and
  still only marginal (medPF 1.075, mean return −0.58%) — **tuning does not
  rescue ORB on ASX**. Treat ORB-ASX as a per-name edge (BHP, WDS), not a
  universe strategy.
- **US ORB:** GOOGL strong (PF 2.45), PLTR/XLK positive, GLD losing.
- **VWAP:** works on CAT (PF 1.44, +18.9bps); loses on every other US name tried
  → correctly deployed only on CAT.
- **Metric interpretation:** `expectancy_bps` was a plain average of per-SELL
  returns, so it could disagree in sign with return and PF. Since 2026-10-04 it
  is dollar-weighted (total net P&L / total entry notional of closed SELLs), so
  its sign matches PF > 1. Expectancy figures in stored runs and in the notes
  above predate the change; rerun before comparing. Total return also includes
  unrealized P&L. See [metric semantics and examples](BACKTEST_METRICS.md).
- **Not done:** statistical-confidence tests (PSR/t-test/bootstrap) — the script
  hung on MC/bootstrap over large trade sets after ~3 of 6 survivors. Rerun with
  iteration caps + per-strategy timeouts if wanted. No results saved; nothing
  relies on partial numbers.

---

## Data coverage

- **ASX 1-min:** 53 universe symbols, ~1 year as of 2026-07-02.
- **US 1-min:** deep history for the historically deployed names.
- DuckDB lives in named volume `mmr_db_data`. Backup: `./docker.sh -B` /
  `mmr data backup`. Nightly pycron `db_backup`.

---

## History data source (Alpaca default)

Code default for `data download` and US `data refresh` jobs is now **Alpaca**
(free Basic plan, SIP, split-adjusted). Config templates are copied only on
first run, so the live host config does **not** change by itself. Operator steps:

1. Backup first: `./docker.sh -B before_alpaca`.
2. Check the adjustment basis on the stored data. Inside the trader container
   (`./docker.sh -e`) run
   `mmr --json data query NVDA --bar-size "1 day" --days 900` (or run it against
   the `-B` snapshot) and read the closes around 2024-06-07. About $120 means
   split-adjusted. About $1,200 means not adjusted: **stop**, the Alpaca switch
   would splice two price bases.
3. Put the keys in the repo `.env`: `ALPACA_API_KEY_ID` and
   `ALPACA_API_SECRET_KEY`. Docker compose passes them to the services.
   `alpaca_api_key_id` / `alpaca_api_secret_key` in `~/.config/mmr/trader.yaml`
   also work, because empty env values are ignored.
4. Switch: set `default_data_source: alpaca` in `~/.config/mmr/trader.yaml` and
   change US jobs to `source: alpaca` in `~/.config/mmr/data_refresh.yaml`.
   A US job without Alpaca keys fails loudly (the job and the batch are marked
   failed, no silent fallback).
5. Optional: a one-time forced US refetch (`force: true` on the job) so stored
   history is one source within each job's `days` window. Older stored bars stay
   TwelveData/Massive.

Until step 5, stored US history may be a TwelveData/Massive to Alpaca splice.
The adjustment basis is documented as the same (TwelveData default
`adjust=splits`, Massive `adjusted` = splits only). It has not yet been checked
against stored data in the Docker volume: that is step 2. The vendor still
differs, so small differences at the seam are possible.

Alpaca returns only completed NYSE sessions (after 20:16 ET). TwelveData and
Massive remain opt-in via `--source`. Known quirk, left as is: TwelveData
returns nothing for intraday bars when start == end, so a single-day gap can
be skipped with `--source twelvedata`.

### Movers and news (phase 3a)

From phase 3a, `mmr movers` and `mmr news` default to Alpaca **without any
config edit** (they ignore `default_data_source`). The CLI needs the Alpaca
keys: export `ALPACA_API_KEY_ID` / `ALPACA_API_SECRET_KEY`, or put them in the
live `~/.config/mmr/trader.yaml` (empty env values no longer blank YAML keys).
To keep Massive: set `data_providers: {movers: massive, news: polygon}` in the
live `trader.yaml`.

Live checks (2026-10-04): all gated Alpaca live tests passed (quotes, news,
movers filter, crypto movers, history). Weekday intraday movers check: not run
(outside US market hours, it was Sunday 08:16 ET).

- TODO (operator): Confirm Alpaca movers `last_updated` is intraday on a weekday (spec 3a check)

### Idea scanner (phase 3b)

From phase 3b, bare `mmr ideas` uses Alpaca **without any config edit** (it
ignores `default_data_source`). It needs the Alpaca keys, like `movers` and
`news`. Discovery is movers + most-actives (about 100-150 symbols, not the full
market) with 15-minute-delayed prices. To keep Massive, set
`data_providers: {ideas: massive}` in the live `trader.yaml`, or pass
`--source massive|twelvedata` per call. `--fundamentals` needs one of those two
until phase 4.

Live checks (2026-10-04, Sunday): the two gated Alpaca ideas tests passed
(momentum scan, explicit tickers with an unknown symbol). Weekday intraday
quality was not checked.

- TODO (operator): The weekday intraday movers check above also gates `ideas` discovery quality (same screener endpoints)

---

### Options (phase 5)

`mmr options expirations|chain|snapshot|implied` default to Alpaca's free
indicative feed **without any config edit** (options ignore
`default_data_source`). Indicative (Alpaca's free, delayed estimate — not
tradable quotes) is not the OPRA NBBO (the official consolidated best
bid/offer): quotes are derived, trades delayed, greeks/IV only on liquid
contracts (on 2026-10-04 AAPL
2026-11-06 calls: 26 of 61 strikes had no usable IV, 35 were used by
`implied`). Rows say `feed: indicative`. To keep Massive: set
`data_providers: {options: massive}` in the live `trader.yaml`. This key's
Massive plan returns NOT_AUTHORIZED for option snapshots (2026-10-04), so
Massive options need a paid options plan, and Massive options are only tested
with fakes. `options buy|sell` still go to IB.

Live checks (2026-10-04, a Sunday, markets closed): 11 gated Alpaca live tests
passed (history, quotes, news, movers, and the five new options tests); the
Massive options test was skipped (no Massive key exported). Real CLI run:
expirations, chain, snapshot (`O:` form), implied and the indicative-feed
table worked. Quotes were the Friday 2026-10-02 close (`quote_time`
19:59:59Z). The CLI `--source massive` call returned the NOT_AUTHORIZED
message with no traceback.

---

## Forex and index movers (phase 6)

`forex convert`, `forex snapshot-all` and `forex movers` default to free ECB daily reference rates (Frankfurter, no key) — one rate per business day about 16:00 CET, labelled with its date, not live. `movers --market indices` defaults to ETF proxies from Alpaca IEX prices (needs the Alpaca keys). `forex snapshot` / `forex quote` still default to IB. Users who relied on `default_data_source: twelvedata` for forex: set `data_providers: {forex: twelvedata}` in the live `~/.config/mmr/trader.yaml`.

- `data_providers.forex: ib` breaks `forex convert` and `forex snapshot-all` (they need a REST forex source).
- `data_providers: {movers: massive}` keeps Massive for index and forex movers too. To set them one by one: `movers_indices: massive` / `movers_forex: massive` (these win over `movers`).
- Massive may return no forex bid/ask; not checked live.
- Checked live on 2026-10-04 (Sunday): Frankfurter returns the Friday 2026-10-02 rate as the latest; Alpaca IEX ETF quotes are Friday's prices.
- Massive forex bid/ask is read from the documented forex snapshot keys (the Massive key is not entitled). To check with an entitled key: `MMR_LIVE_TESTS=1 MASSIVE_API_KEY=... pytest -m live tests/data_providers/test_live_massive_forex.py`.
- TODO (operator): when IB Gateway is up, verify mmr forex snapshot EURUSD and mmr forex quote EUR USD (IB IDEALPRO CASH path; not live-verified in phase 6).

---

## Infrastructure

- Split compose: ib-gateway, trader, strategy, data, dashboard, scheduler.
- Restart policy: `unless-stopped`.
- Paper mode; typed Ed25519 RPC (one key per principal, allow-list in
  `trader/messaging/principals.py`). Dashboard on host loopback (default 7424).

## RPC keys (SP1 Plan 2)

Keys live in `~/.config/mmr/keys/rpc/`: `<principal>.key` (mode 0600) and
`<principal>.pub` (0644) for `trader`, `strategy`, `cli`, `dashboard`,
`ai_supervisor`, `ai_research`. Each container mounts only its own key pair
and the public keys it needs (tmpfs overlay + per-file read-only binds). A
service refuses to start when its own `.pub` is missing or does not match its
`.key`.
`./docker.sh -u` refuses to start while any key file the compose file names
is missing. RPC keys and bundle-signing keys (`keys/verify`, `keys/private`)
are separate; each loader refuses the other kind.

0. **First setup / cutover (owner-run, in this order):**
   1. `./docker.sh -b` (image with `age` and the keygen entry point).
   2. `./docker.sh -k` (creates every missing keypair, as your host user).
   3. Cutover gate: `./docker.sh -K` (key check). It runs one short-lived
      container per service (`trader`, `strategy`, `dashboard`, `cli`,
      `scheduler`, `data`) in a separate compose project `mmr-keycheck`, with
      `docker-compose.test.override.yml` (fake broker, `--simulation True`).
      Each container only runs `mmr keys check-mount <service>`: it must see
      exactly its own `.key`, its own `.pub` and its peers' `.pub`, and
      `service_hmac.key` must read empty. No service process starts, no port
      is published and the running `mmr` stack is not touched. `-K` runs
      alone: combined with any other option (e.g. `-K -d`) it refuses before
      any Docker call. Abort the
      cutover on any failure. (The `fullstack-tests` profile is no longer the
      cutover gate: it stops the dashboard and signals trader PID 1.)
   4. `./docker.sh -u`.
   5. Check `mmr status` from the host and the `/cc` dashboard.
1. **Rotation** (one principal, e.g. a suspected leak):
   `./docker.sh -k --backup` first, then `./docker.sh -k --rotate <principal>`.
   There is no overlap window: restart every service it lists **together**
   (a bind mount keeps the old inode until restart). Requests in flight
   during the switch fail with `AUTHENTICATION_ERROR`; clients retry. Then
   check `mmr status`, `/cc`, and `docker compose run --rm cli strategies`.
   Rotation writes both new files to temp names first, then renames `.pub`
   and last `.key`. If it is cut off between the two renames, the pair does
   not match: the service refuses to start and `-k` reports it. Run the same
   `--rotate` again to repair it.
2. **Lost private key or lost host:** restore from the encrypted backup
   (below). Without a backup, rotate that principal (all services that trust
   it restart together).
3. **Backup and restore** (age; the identity lives only in 1Password):
   - Save the age **recipient** (public key) once at
     `~/.config/mmr/keys/rpc_backup_recipient.txt`.
   - Backup: `./docker.sh -k --backup` writes
     `~/.local/share/mmr/backups/rpc_keys/rpc_keys_<UTC>.tar.age` (0600, dir
     0700). Only `keys/rpc` is in it; DuckDB backups (`-B`, `data backup`)
     never include RPC keys.
   - Restore into an empty `keys/rpc`:
     `op read <item> | ./docker.sh -k --restore FILE`. The identity streams
     from stdin into the container; no tool stores, logs or prints it. With
     `--identity-file PATH` the file is mounted read-only and deleted only
     with `--delete-identity-file`.
4. **HMAC retirement.** No code reads `service_hmac.key` any more and every
   container sees `/dev/null` in its place. No tool deletes it. Delete it by
   hand only after (a) the trust-matrix tests pass, (b) a manual `mmr status`
   / `/cc` check on the running stack, and (c) the rollback decision is made
   (rollback = checking out the pre-cutover commit, which needs the file):
   `rm ~/.config/mmr/service_hmac.key`. If the file is already gone, Docker
   may leave an empty placeholder at that path; it is harmless.
5. Local hybrid (`./start_mmr.sh`) runs `mmr keys init` on the host when a
   key is missing; all services run as one user there, so keys are not
   isolated from each other.

---

## Next operator session (paper soak)

The automation soak below is blocked until the exit fix
and the split-Docker evidence gap are resolved (see Known blockers). Until then, run strategies with `auto_execute: propose`
and approve on `/cc`.

1. `./docker.sh -b -u` after this polish (baked dashboard image).
2. Confirm `mmr status` → IB upstream connected.
3. Enable **momentum** from `/cc` or `docker compose run --rm cli strategies enable momentum`.
4. Scaling: `lifecycle=armed`, operating mode Auto.
5. Leave the stack up through US RTH. Watch intents / protectives / fills.
6. Host monitors: `mmr --json portfolio-snapshot`, `portfolio-diff`, `/cc` Risk bars.

Do not arm a second automatic strategy. Do not set `automation.live_enabled`.

---

## Open items / follow-ups (offline)

- Cluster G (AUDIT_ROADMAP): G1 mass-enable RPC timeout, G2 IB farm-status log noise.
- Historical `expectancy_bps`/PF disagreements: reconcile original trade traces,
  entry notionals and partial exits before alleging a calculation defect (see above).
- Statistical-confidence script needs timeouts/caps before rerun.
- ORB-ASX: consider a proper train/test split before trusting BHP/WDS edges live.
