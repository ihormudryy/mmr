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
- **Live blocker: the experiment kill line reads IB account updates (about 3
  minutes).** `trader/automation/kill_monitor.py` evaluates every fenced broker
  capture every 5 s, but `NetLiquidation` arrives with IB's account-update
  cadence, so a kill can be detected about 3 minutes late. Accepted for paper
  only (SP1 Plan 4 K1). A faster, verified detector is required before any live
  use. Check the active line and its pending edit with `mmr experiment status`;
  a `trader.yaml` edit applies only after a trader_service restart.
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

### Paper quotes from Alpaca IEX (issue #74)

The owner's IB paper account gets only delayed quotes (IB error 354), so every
automated entry is refused `FEED_NOT_LIVE`. On paper you can use Alpaca's free
real-time IEX quote instead:

```yaml
# ~/.config/mmr/trader.yaml (then restart trader_service)
automation:
  quote_fallback: alpaca_iex
```

It needs `ALPACA_API_KEY_ID` and `ALPACA_API_SECRET_KEY` (env or `trader.yaml`);
blank keys or any other value stop the trader at startup. The trader asks IB
first and uses Alpaca only when the IB quote is missing or not `live`. A live
account never calls Alpaca, even with the setting on.

- The quote is labelled `iex_realtime`, never `live`. Only paper with the setting
  accepts it in the dispatch guard, approval evidence and liquidity policy.
  Proposals record it in `reference_feed_type`.
- Only US stocks the universe resolves (`STK`, `USD`, US primary exchange) get a
  quote. `BRK B` is asked as `BRK.B`; any other symbol shape gets no quote.
- The 5 s freshness, crossed-book, 15 bps spread and regular-session checks are
  unchanged. IEX is one venue, so expect more `SPREAD_BPS` refusals, and
  `QUOTE_STALE` on names whose last IEX quote is older than 5 s.
- **Gap: no halt flag.** Alpaca quotes do not say when a stock is halted.
  `session_state` comes from the XNYS calendar only (continuous inside the
  regular session, else `closed`), so a halt during the session is not seen.
  When IB itself reports a halt (on any feed, delayed too), the IB quote is kept
  and the entry is refused; IEX never replaces it.
- At dispatch, automated and live entries re-check the 15 bps spread and the
  crossed-side depth (it must cover the whole order) on the final quote
  (`SPREAD_BPS`, `DEPTH_EXCEEDED`, retryable). Manual paper proposals do not.
- **Sizes are probably round lots.** An Alpaca forum answer (2022) says bid/ask
  sizes are round lots; not yet checked on a real IEX response. They are used
  unchanged as shares, the safe direction: top-of-book depth may be understated
  (up to 100×), so IEX entries size to a few shares or hit `DEPTH_EXCEEDED`.
  Converting needs the per-stock round-lot size, which Alpaca does not send.
- The quote source differs from IB's paper fill engine; fills can differ.

### Current arms

| Strategy | Role | Notes |
|---|---|---|
| **momentum** | Single auto slot | Armed with the old fixture bundle, which no longer passes the provenance or binding check (see the 2026-10 note). `auto_execute` off. Enable it before a soak (`INSTALLED` ≠ dispatchable). |
| orb_* / ensemble | Optional propose | Human review on `/cc` if `auto_execute: propose`. |
| global | Always present | Enable/Disable/Undeploy hidden by design. |
| `ai` service (SP2) | AI paper decision loop | Not started. Needs model ids and prices in `ai.yaml` and the discretionary digest (see "Starting the AI paper decision loop"); strategy BUYs come only from ACTIVE judged versions. |

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
`ai_supervisor`, `ai_research`, `research`. Each container mounts only its own key pair
and the public keys it needs (tmpfs overlay + per-file read-only binds). A
service refuses to start when its own `.pub` is missing or does not match its
`.key`.
`./docker.sh -u` refuses to start while any key file the compose file names
is missing. RPC keys and bundle-signing keys (`keys/verify`, `keys/private`)
are separate; each loader refuses the other kind.

**Upgrade (SP2c Plan 1):** every trader now needs
`~/.config/mmr/keys/rpc/research.pub` (the trader container mounts it). Run
`./docker.sh -k` (or `mmr keys init` on a host install) before you redeploy;
`./docker.sh -u` refuses to start while it is missing.

**Research service (SP2c Plan 3): not armed.** The `research` container is in the
opt-in `ai` profile and nothing starts it yet. It holds the research signing key,
so it needs one more file than the other services. First start:
1. `mmr keys init-signing` once on the host. It creates
   `~/.config/mmr/keys/private/signing.pem` (mode 0600) and
   `keys/verify/paper-automation.pem`, and never overwrites a private key.
2. `./docker.sh -k` creates `research.key` and `research.pub`.
3. `./docker.sh -K` must pass for `research` and every other service.
4. Deploy the trader before or with research: `./docker.sh -b -u`, then
   `docker compose --profile ai up -d research`. Two different failures:
   - A trader build older than research (the row body shape differs): each
     row is refused for good, kept in `shadow_failures` and never resent.
   - A trader without `ai_paper` enabled serves none of the research
     methods (`METHOD_NOT_ALLOWED`). Only the shadow worker waits: it logs
     ERROR "does not serve this call" every tick and retries. With a pending
     evaluation (a claim to recover or an end report owed), recovery and the
     report fail loudly, the service exits and compose restarts research in
     a loop until `ai_paper` is on. Nothing is lost. Enable `ai_paper` on the
     trader before you start research.
   `./docker.sh -u` refuses to start while the ai profile is active and
   `signing.pem` is missing. A direct `docker compose` start skips that check.
5. The research DB is fresh: `mmr_research_data` (`mmr_research.duckdb`). Its
   ports 42106/42107 are not published to the host.

Upgrade notes:
- The backtester now orders bars with the same timestamp by conid (fix for a
  day's trades that depended on later bars). The trace signature of such runs
  changes, so re-running an old succeeded trial of an existing research family
  fails loudly ("data or code changed"). An existing family needs a new
  family (a new `mmr research evaluate`).
- If the trader is down at start, research waits and retries (2 s, doubling to
  30 s). It accepts no submit until it has recovered its open work.

Open items:
- Ruling 13: research mounts `mmr_db_data` (bars, like strategy). The owner
  still has to confirm that this volume exposure is acceptable.
- Ruling 17: renewal cases and the trader's forward evidence come with SP2c
  Plan 5. A RENEWAL request is refused until then.
- The signing key file owner inside the container: `load_signing_key` checks
  mode `0600`, so the container user must own the bind. Check this on the
  first start.
- Known limit: a deterministic trader `INTERNAL_ERROR` on one stored shadow row
  still ends the research worker, and compose restarts it in a loop. Fix the
  cause at the trader. A per-row `VALIDATION_ERROR` does not loop; it goes to
  `shadow_failures`. A busy trader (`SERVER_BUSY`) is retried like an
  unreachable one.
- Known limit: a crash, `docker compose stop` or any stop signal during an
  evaluation spends its holdout window if the holdout was already opened (the
  service waits only 5 s on a stop). An orphan case file may stay in
  `artifacts/cases` (follow-up). When another research job dies, the service
  closes its ports and waits up to 30 min for the running evaluation before it
  exits. On a host run, a stop signal during that wait does not shorten it; in
  Docker, `docker compose stop` ends it after about 10 s.
- Known limit: the holdout ledger is per research DB. A host
  `mmr research evaluate` and the research service (`mmr_research_data`) do
  not see each other's holdouts, so they can reveal the same window twice.
- Known limit: a shadow row sent before its DEPLOY judgment was registered as
  a deployment version keeps a NULL `deployment_version`; a later registration
  does not change it.

Watch items (container log: `docker compose logs research`; the research
tables are in `mmr_research.duckdb`; there is no `mmr` command for these yet):
- A finished evaluation shows as `RUNNING` until the trader confirmed its end.
  `research_requests.pending_report` is set meanwhile, and the log says
  "not confirmed by the trader yet". It is sent again every tick. If the
  trader refuses the end report, the log says "the case stays withheld".
- A request that can never finish is `PARKED` with a reason. The log says
  "evaluation ... parked ... needs the operator". It reads as `FAILED` without
  a case, and the worker goes on. The trader's claim stays open and still counts
  for the day.
- A shadow case that can never be replayed is skipped; the log says
  "shadow replay parked case". A shadow row the trader refused for good is in
  `shadow_failures` with its code. The log says "refused for good" at ERROR.
  Fix the cause, delete that row from `shadow_failures`, and the next tick
  sends it again.

0. **First setup / cutover (owner-run, in this order):**
   1. `./docker.sh -b` (image with `age` and the keygen entry point).
   2. `./docker.sh -k` (creates every missing keypair, as your host user).
   3. Cutover gate: `./docker.sh -K` (key check). It runs one short-lived
      container per service (`trader`, `strategy`, `dashboard`, `cli`,
      `scheduler`, `data`, `ai`, `research`) in a separate compose project `mmr-keycheck`, with
      `docker-compose.test.override.yml` (fake broker, `--simulation True`).
      Each container only runs `mmr keys check-mount <service>`: it must see
      exactly its own `.key`, its own `.pub` and its peers' `.pub`, those
      keys must load the way the service loads them at startup (own pair
      matches, modes, Ed25519), and `service_hmac.key` must read empty.
      `-K` needs `~/.config/mmr/keys/private/signing.pem` (run
      `mmr keys init-signing` first); `research` must load it as at startup
      (mode 0600, Ed25519, not an RPC key). No service process starts, no port
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

## Scoreboard and Telegram summary (SP1 Plan 5)

- `mmr scoreboard` / `mmr --json scoreboard` / `mmr scoreboard verify` need
  trader_service (typed RPC). The `/cc` Scoreboard tab reads the same report.
- Telegram is **off** by default. While `ai_paper.telegram.enabled` is false
  nothing is sent and no outbox row is written, so enabling it later does not
  send a backlog (at most the latest session's summary).
- **Owner-only setup** (never in chat, never in a file in the repo):
  1. Create a bot with @BotFather and note its token; find the chat id of the
     chat that should receive the summaries.
  2. `mkdir -p ~/.config/mmr/secrets && chmod 700 ~/.config/mmr/secrets`, write
     the token to `~/.config/mmr/secrets/telegram.token`, `chmod 600` it.
  3. In `~/.config/mmr/trader.yaml`:
     `ai_paper: {telegram: {enabled: true, chat_id: <id>, token_secret_file: ~/.config/mmr/secrets/telegram.token}}`.
  4. Restart trader_service. With `enabled: true` and a missing, empty,
     symlinked or group/world-readable token file, a bad chat id or an unknown
     key, the trader **stops at start** with a `TelegramConfigError`.
- Only the trader container mounts `~/.config/mmr/secrets` (read-only); every
  other service hides it behind a tmpfs. Not checked live: whether a 0600 token
  file bind-mounted from the macOS host is readable by the container user.
- Delivery is **at-least-once**: a crash between send and mark-sent repeats one
  message; every message ends with `event <id>`, so a repeat is recognisable.
  A Telegram outage only delays messages (30 s backoff doubling to 1 h).
- One summary per session end in `FLAT`, `KILLED` or `FAILED_SAFE`; an
  `UNKNOWN` row (the trader was down at the end) sends none and shows as a
  `SESSION_MISSING` incident on the scoreboard.

---

## Next operator session (paper soak)

**SP1 paper acceptance (Plan 6):** before SP2 trades, run one real IB paper
session by [`docs/PAPER_ACCEPTANCE_SP1.md`](PAPER_ACCEPTANCE_SP1.md): the
clean-account gate (`mmr experiment acceptance preflight`), arming, the
harness run with the live OCA shrink proof, the 15:45 flatten, `finish` and the
signed report. Owner only; nothing in the repo places those orders. Needs
`ai_paper.acceptance_probe: true` for the day and an operator signing key
(`~/.config/mmr/keys/acceptance/operator.key`). Not run yet. The acceptance
harness holds its own controller epoch (lease 60 s). Stop the `ai` service
before an acceptance run, or the harness waits on `CONTROLLER_EPOCH_HELD` and
then fails.

- AI paper controller (SP2): `./docker.sh -u` copies `ai.yaml` to `~/.config/mmr/` once; fill in the model ids and prices, then `docker compose --profile ai up -d ai`. The signing key matters only when the research service runs (it is in the same `ai` profile): before `./docker.sh -u` with the `ai` profile active, or any start of `research`, run `mmr keys init-signing` once (a direct `docker compose` start skips the `./docker.sh` check). Stop it with `docker compose --profile ai stop ai` (always before the SP1 acceptance run). Its health is the heartbeat file `/tmp/mmr_ai_heartbeat.json` inside the container. Its data volume `mmr_ai_data` is kept by `./docker.sh -d`; only `./docker.sh -c` removes volumes. Schema rule (no legacy data): `ai.duckdb` tables are edited in place, never upgraded; if a pre-release build ever created `ai.duckdb`, delete the `mmr_ai_data` volume (`docker volume rm mmr_mmr_ai_data`; Compose prefixes the project name `mmr`) before starting a newer one.

**Upgrade (SP2c Plan 2: judged deployments and versions).** Before the
first deploy of this build:

- Back up, stop, then start the changed tables fresh. They were edited in
  place (owner rule: no legacy data, no upgrade), and neither
  `CREATE TABLE IF NOT EXISTS` nor the migration ledger changes a table that
  already exists. In this order:
  1. `./docker.sh -B before_sp2c_plan2` (DuckDB files only; `ai.duckdb` is
     not in it and is deleted on purpose below).
  2. `docker compose --profile ai stop ai`, then
     `docker compose --profile ai rm -f ai` (a stopped container still holds
     the volume, so step 4 would fail), then `./docker.sh -d`.
  3. Drop the tables. Run this from the repo root (Compose reads
     `docker-compose.yml` there). Paths are the `trader.yaml` defaults; use
     yours if you changed `duckdb_path` or `journal_duckdb_path`:
     ```bash
     docker compose run --rm --no-deps --entrypoint python scheduler -c "
     import duckdb
     d = '/home/trader/.local/share/mmr/data/'
     j = duckdb.connect(d + 'mmr_journal.duckdb')
     j.execute('DROP TABLE IF EXISTS ai_paper_decisions')
     j.execute('DELETE FROM schema_migrations WHERE version = 56')
     j.close()
     t = duckdb.connect(d + 'mmr.duckdb')
     for name in ('strategy_signal_record', 'strategy_signal_record_state', 'strategy_signal_record_generation'):
         t.execute('DROP TABLE IF EXISTS ' + name)
     t.close()"
     ```
     The `schema_migrations` row matters: without deleting it, migration 56
     never creates `ai_paper_decisions` again (all its statements are
     `IF NOT EXISTS`, so the replay is safe). The strategy service creates
     the three `strategy_signal_record*` tables on start.
  4. Delete the ai volume: `docker volume ls | grep mmr_ai_data` shows its
     full name (Compose prefixes the project name `mmr`), then
     `docker volume rm mmr_mmr_ai_data`.
  5. `./docker.sh -b -u`. Journal migrations 115 (`ai_deployment_versions`)
     and 116 (`ai_deployment_withdrawals`) apply on start.
- Strategy entries need a judged version. A strategy-kind `ENTER` without
  `deployment_version` and `source_digest` is refused
  `DEPLOYMENT_VERSION_REQUIRED`, so BUYs of a plain config strategy listed in
  `decisions.strategies` no longer enter; only `aidv-` instances, which the
  strategy service loads from ACTIVE versions, do. Such an unbound BUY also
  costs nothing: the `ai` service notes it `STRATEGY_NOT_BOUND` and makes no
  model call and records no baseline.
- Stop a judged version (operator, final): `mmr ai-deployment withdraw sha256:<version> --reason "<why>"`.
  Its `aidv-` instance unloads and new entries are refused; exits and open
  brackets are untouched. While an entry of that version is being sent the
  withdrawal is refused (`WITHDRAWAL_ENTRY_IN_FLIGHT`): retry after the
  entry's send returns. `mmr ai-deployment version sha256:<version>` shows
  its state.
- SP1 acceptance now needs an SP2c-judged deployment version:
  `mmr experiment acceptance run --place-orders --deployment-version sha256:...`.
  The harness registers nothing and no longer uses the `ai_research` key
  (runbook P0.5a).
- Verify keys (`~/.config/mmr/keys/verify/*.pem`): when you rotate one, keep
  the old `.pem` while any version judged under it can still be renewed,
  that is, while it is `EXPIRED` and not yet `SUPERSEDED` (or withdrawn or
  ended). The trader re-reads a version's judgment and signed case with
  these keys.

**Starting the AI paper decision loop (SP2):**

1. Publish the initial risk policy (operator, once): `mmr ai-policy publish policy.yaml --reason "initial paper policy"`.
2. Register the discretionary deployment (operator, once): `mmr ai-deployment register-discretionary --operator <name> --statement "<why>"`; note the printed digest.
3. Edit `~/.config/mmr/ai.yaml`: model ids and prices (`roles:`, `pricing:`); `decisions.discretionary_deployment_digest`; the bracket of judged strategies (`decisions.ai_deployments`: stop and target fractions). Since SP2c Plan 2 only `aidv-` instances of ACTIVE judged versions are followed; `decisions.strategies` entries no longer trade and cost nothing (see the upgrade note above).
4. Start it: `docker compose --profile ai up -d ai`. If the research service runs too (same `ai` profile, e.g. `./docker.sh -u` with the profile active), run `mmr keys init-signing` once first; a direct `docker compose` start skips the `./docker.sh` check. Stop it before the SP1 acceptance run.
5. Check: the heartbeat file, `mmr scoreboard` (books per baseline, AI cost with status), and `ai_rulings` / `ai_discovery_reads` in `ai.duckdb` for refusals and discovery coverage.

The service never publishes or loosens policy. A role whose provider rejects its model is paused for 5 minutes at a time: Jev down blocks every ENTER, orchestrator down stops discovery and model closes; SP1's stops, targets and the 15:45 flatten are unaffected.

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

## AI research cycle (SP2c)

**Status: built, off by default** (`research.enabled: false` in `ai.yaml`). It runs only on paper.

### What runs

- After each session close, plus `after_close_minutes` (30), the `ai` service starts one research slot. The slot ends 30 minutes before the next open. Nothing of this runs during the session.
- In the slot the `ai` service:
  1. builds a menu from `ai.yaml` `research:` (strategy keys, universes, bar sizes, the tunables of each strategy file),
  2. asks the orchestrator for at most `max_candidates_per_cycle` candidates,
  3. screens the answer in code. One bad pick is dropped with a code and never stops the others,
  4. sends each candidate to the `research` service (`submit_evaluation`),
  5. polls the result (`get_evaluation`). When the case is ready, Jev judges it (DEPLOY, SHADOW, REJECT or NO_VERDICT),
  6. records every judgment at the trader (`record_backtest_judgment`),
  7. for a DEPLOY: the research service attests it (`attest_from_judgment`) and the trader registers the deployment (`register_ai_deployment`).
- Each step is stored in `ai.duckdb` before its call. After a lost reply the same stored body is sent again.
- Jev sees only the code-built summary of the case. The orchestrator's thesis is stored but never reaches Jev. DEPLOY is on Jev's menu only when every rule passed.
- Model calls use the same budget, cap and journal as the rest of the `ai` service.

### Turn it on

1. In `trader.yaml` put the strategy keys into `ai_paper.backtest_judge.strategy_allowlist` (`strategies/<file>.py:<Class>`). This is the authority. An empty list means nothing may be evaluated. Restart the trader (`./docker.sh -b -u`).
2. In `ai.yaml` `research:` put the same keys in `strategy_keys`. Add at least one universe of 8 to 20 conids. Check each conid with `mmr resolve SYMBOL`. Set `enabled: true`.
   - Keep `research.max_cohort_points` at or below `ai_paper.backtest_judge.max_cohort_points`. The `ai` service cannot read `trader.yaml`.
   - The strategy files are baked into the image. After you edit a strategy, rebuild (`./docker.sh -b -u`) so `ai` and `research` read the same bytes.
3. Make sure the `research` service runs (see "Research service (SP2c Plan 3)" above) and an experiment was started (`mmr experiment start`). Without an experiment a slot is `SKIPPED` (`NO_EXPERIMENT`).
4. Restart `ai` (`./docker.sh -b -u`, or `docker compose --profile ai up -d ai`). A bad `research:` block stops `ai` at start. The error names the field (`RESEARCH_MENU_EMPTY`, `RESEARCH_UNIVERSE_INVALID`, `RESEARCH_BAR_SIZE_INVALID`, `RESEARCH_BAR_SIZE_TOO_LONG`, `RESEARCH_DUPLICATE_STRATEGY`).

**Daily bars for every conid (important).** The trader checks the liquidity of an ENTER from its local daily bars only (`production_evidence.py`, `liquidity_from_history`). There is no Alpaca fallback. So every conid in a research universe must also be in a universe that the scheduler refreshes with a daily job. The jobs are in `data_refresh.yaml` (`bar_size: "1 day"`) and the cron entries in `pycron.yaml` (`data_refresh_us`, `data_refresh_asx`). A conid with no daily history gets its ENTER refused with `HISTORY_INVALID`, after a good backtest. Check with `mmr data status`.

### Watch it

- The heartbeat file `/tmp/mmr_ai_heartbeat.json` in the `ai` container has a `research` object: `open_candidates`, `unrecorded_judgments`, `pending_registrations`. It is `null` when research is off. `open_candidates` and `unrecorded_judgments` should go to 0 within a night. A candidate still open the next evening waits on purpose: a slow evaluation (closed as `EVALUATION_STALE` after `evaluation_stale_hours`), a closed budget cap gate, or a judgment row left in `JUDGING` (see "Waiting on purpose"). `pending_registrations` can stay above 0 while a DEPLOY waits for the cap (`WAITING_CAP`) or for the trader (a retryable refusal).
- Typed read calls (no CLI command yet): `get_backtest_judgment` (trader, as `cli` or `dashboard`) and `get_evaluation` (research service, as `cli`).
- `ai.duckdb` tables:
  - `ai_research_cycles`: one row per evening. `state` is RUNNING, DONE, SKIPPED, MISSED or FAILED. `reason` says why. `menu_json` is the menu. `dropped_json` lists every dropped menu entry and pick with its code.
  - `ai_research_candidates`: one row per candidate. `state` is NEW, SUBMITTED, EVALUATED or CLOSED. `end_code` says how it ended.
  - `ai_backtest_judgments`: one row per case. `state` is JUDGING, DECIDED, RECORDED or REFUSED. `verdict` and `code` hold the result.
  - `ai_research_registrations`: one row per DEPLOY. `state` is ATTESTING, REGISTERING, WAITING_CAP, REGISTERED or REFUSED. `line_state` is LIVE or ENDED.
  - `ai_research_cooldowns`: strategy keys that cool down, and until which session.
- Logs: a lost slot, a refused judgment or registration and a judgment that cannot be taken (stuck in `JUDGING`, or the case already judged) are ERROR. A lost submit reply is one WARNING when it happens. If that candidate then closes as `STALE_NOT_SUBMITTED`, that is an ERROR. A wait is WARNING: once per row (or slot) and code, except the closed budget cap gate, which logs one per pump.

### Codes you will see

Cycle `reason` (`ai_research_cycles`):
- `CANDIDATES_n`: done, n candidates stored.
- `NO_STRATEGY_ON_MENU`, `MENU_INCOMPLETE`: the menu was empty. Check `dropped_json` (`STRATEGY_NOT_FOUND`, `COOLING_DOWN`, `NO_NUMERIC_TUNABLES`, `TOO_MANY_TUNABLES`, `UNIVERSE_INVALID`, `BAR_SIZE_NOT_ELIGIBLE`, `STRATEGY_SCAN_FAILED`).
- `PROPOSAL_*`: the orchestrator's answer was refused (`PROPOSAL_OUTPUT_*`, or `PROPOSAL_MODEL_FAILED_*` after the call was sent). No candidate that night.
- `NO_EXPERIMENT` (SKIPPED), `LATE_START` (MISSED), `ENGINE_ERROR`, `PROCESS_RESTARTED` (FAILED).
- A refused or never-sent orchestrator call leaves the slot open. It is tried again while the slot is due: not before the refusal's retry time (for a spent budget, the next New York midnight), or 60 seconds later when the refusal has none. This hold-off lives in memory; a restart tries at once.

Dropped picks (`dropped_json`): `OFF_MENU_STRATEGY`, `OFF_MENU_UNIVERSE`, `OFF_MENU_BAR_SIZE`, `CANDIDATE_LIMIT`, `COHORT_CONFLICT`, `UNDECLARED_TUNABLE`, `TUNABLE_TYPE`, `POINT_INVALID`, `DUPLICATE_POINT`, `COHORT_POINT_LIMIT`, `NO_VALID_POINTS`.

Candidate `end_code`:
- `JUDGED_DEPLOY`, `JUDGED_SHADOW`, `JUDGED_REJECT`, `JUDGED_NO_VERDICT`: judged.
- `REFUSED_<code>`: the research service refused the submit. For example `REFUSED_FAMILY_COOLING_DOWN` (this also starts a cooldown here), `REFUSED_HOLDOUT_NOT_AVAILABLE`, `REFUSED_COHORT_TOO_LARGE`.
- `NOT_SUBMITTED_EVALUATION_LIMIT_REACHED`: the day's evaluation limit was hit. The other new candidates of that slot close with this code. The one that was refused has `REFUSED_EVALUATION_LIMIT_REACHED`.
- `STALE_NOT_SUBMITTED`: the candidate was still NEW after its slot window closed. It is never carried to another night. If the submit reply was lost, this is logged as ERROR, because the research service may hold an accepted evaluation.
- `EVALUATION_STALE`: no result after `evaluation_stale_hours`.
- `EVALUATION_FAILED_NO_CASE`: the evaluation failed or was parked and has no case.
- `DUPLICATE_REQUEST`: the research service mapped this candidate to a request that another candidate already holds (a submit retried past New York midnight, then the same cohort the next evening). One WARNING names both candidates. The other candidate carries the case and its judgment.
- `RPC_<code>`: the typed call was refused by the service.

Judgment `code` when the verdict is `NO_VERDICT`: `JEV_OFF_MENU`, `JEV_NARRATIVE_MISSING`, `OUTPUT_*` (bad JSON or schema), `MODEL_REFUSED_<code>` (for example the budget is spent), `MODEL_FAILED_<outcome>`, `ENGINE_ERROR`, `PROCESS_RESTARTED`. Judgment `error_code` (state REFUSED): the trader refused the record (`JUDGMENT_MENU_MISMATCH`, `DEPLOY_NOT_ALLOWED`, `RPC_*`, ...). The line ends.

Registration `state` and `error_code`:
- `WAITING_CAP` (`DEPLOY_CAP_REACHED`): the cap of active DEPLOYs is full. It is tried again at the first research slot on a later New York date (the trader keys the registration by that date, so a retry on the same date would replay the refusal). It ends when the bundle expires (`BUNDLE_EXPIRED`).
- `REGISTERING` with a retryable refusal (for example `AUDIT_UNAVAILABLE`): the trader refused for now and kept no ledger record. The same body is sent again on the next pump, with one WARNING per code. A receipt the ledger has not settled yet (`OUTCOME_UNKNOWN`) is also asked again, with one WARNING. Both end when the trader answers, at the latest with `BUNDLE_EXPIRED`.
- `ATTEST_<code>`: the research service refused the attestation for good. The line ends with ERROR.
- `ATTEST_<code>_RETRIES_EXHAUSTED`: the attestation failed `ATTEST_MAX_TRIES` (3) times. `TRADER_UNAVAILABLE` does not count as a try.
- `RPC_<code>` or the trader's own code (`JUDGMENT_*`, `BUNDLE_*`, `FAMILY_COOLING_DOWN`, `RENEWAL_PRIOR_INVALID`, ...): registration refused. The line ends.
- `line_state ENDED` with `error_code` EXPIRED, WITHDRAWN or ENDED: the deployed version is over. No renewal yet.

### Waiting on purpose

- While the budget cap gate is closed (the owner's cap is not read from the trader yet), an evaluated case waits and is not judged. You see one WARNING per pump. Jev would be refused and the case would be lost. When the gate opens, the case is judged. A budget that is really spent still gives `NO_VERDICT`.
- A judgment row left in `JUDGING` (for example a database error right after the Jev call) is logged as ERROR once per process. Only the next start of `ai` with research on settles it: `recover()` records it as `NO_VERDICT` (`PROCESS_RESTARTED`) and the pump sends that record. Jev is never asked twice. Until that restart the candidate stays `EVALUATED`.
- A case that already has a judgment for another candidate is also ERROR once per process. That candidate stays `EVALUATED`; a restart does not change it. Since `DUPLICATE_REQUEST` this should not happen. If it does, check both candidates' `request_id` and `case_digest`.

### Stop it

- Set `research.enabled: false` and restart `ai`. Nothing new starts. Recorded judgments and registered versions stay.
- Withdraw one deployment with `mmr ai-deployment withdraw VERSION_DIGEST --reason "..."` (typed `withdraw_ai_deployment`, `cli` or dashboard).
- The trader refuses the ENTER of an expired, withdrawn or cooling-down deployment on its own.

### Replay a judgment

`replay_backtest_judgment(store, judgment_id, config=...)` (`trader/ai/backtest_judge.py`) repeats the verdict from the stored evidence with zero model calls. If the config or the code changed, the result is `INCOMPLETE`. A case judged twice is also `INCOMPLETE`.

### Known limits

- No renewal before SP2c Plan 5. An expired DEPLOY ends. Only a new evaluation with a new, disjoint holdout can deploy that strategy again.
- The research service runs one evaluation at a time.
- A crash during a Jev call loses that case (`PROCESS_RESTARTED`). A crash during the orchestrator call loses that night's slot.
- A submit reply lost just before New York midnight and sent again after it is a new request and uses a second evaluation slot (Plan 3, Ruling 1).
- Calls to the research service and the trader are not fenced by the controller epoch. When a new leader runs `recover()`, a stale leader could race it for a short time (two orchestrator calls, a narrow window).
- Jev judges on the code-computed summary: stage, rule results, per-point expectancy at 1x, 1.5x and 2x cost, the selection statistic and trial counts. It has no Sharpe or drawdown figures.
- Research universes need local daily bars. See "Daily bars for every conid" above.

---

## Open items / follow-ups (offline)

- Cluster G (AUDIT_ROADMAP): G1 mass-enable RPC timeout, G2 IB farm-status log noise.
- Historical `expectancy_bps`/PF disagreements: reconcile original trade traces,
  entry notionals and partial exits before alleging a calculation defect (see above).
- Statistical-confidence script needs timeouts/caps before rerun.
- ORB-ASX: consider a proper train/test split before trusting BHP/WDS edges live.
