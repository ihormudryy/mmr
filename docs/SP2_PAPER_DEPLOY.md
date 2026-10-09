# SP2 paper deploy checklist

First paper deployment of SP2 (AI decision loop and backtest judge). Follow the steps top to bottom.
Do not skip a "check:". Paper only. Never paste a key, token or `.env` value into chat or a commit.

- Run commands from the repo root, on your Mac.
- `mmr` means the host command (`pip install -e .` in a venv). It talks to the trader on 127.0.0.1.
- **OWNER ONLY** marks a step that uses a signing key, arms trading or places IB orders.
- Details live in [OPERATIONAL_STATE.md](OPERATIONAL_STATE.md) and [PAPER_ACCEPTANCE_SP1.md](PAPER_ACCEPTANCE_SP1.md).

## Why the order is like this

- SP1 acceptance needs a judged deployment version. Only the research cycle makes one.
- The research cycle needs a started experiment, so step 7 starts one and pauses it. A paused experiment cannot enter.
- Acceptance needs the `ai` service stopped and no active experiment. So SP2 trades only in step 8.
- What holds entries off in step 7: the experiment is PAUSED. The `ai` service judges a BUY only when the experiment is ARMED
  (`EXPERIMENT_PAUSED`), the trader refuses an ENTER unless it is ARMED (`EXPERIMENT_NOT_ARMED`), and the discretionary
  digest is still null and no policy is published. The research cycle needs only that an experiment exists
  (`NO_EXPERIMENT` otherwise).

Tickets: checklist #104, deploy #105, paper soak #106, SP1 acceptance #35.

## 0. Prerequisites

1. Docker Desktop has 28 GB RAM or more. `./docker.sh -u` warns if not.
2. `.env` in the repo has the IB paper login, `TRADING_MODE=paper` and an `IB_ACCOUNT` that starts with `DU`.
3. Add your model key to `.env`, for example `OPENROUTER_API_KEY=...`. Compose passes it to the `ai` container.
4. Alpaca keys are set (`./docker.sh -u` prints "set" for Alpaca). Research and discovery need them.
5. One-strategy automation is off: `automation.enabled: false` in `~/.config/mmr/trader.yaml`. Both on refuses every entry.
6. Gross reservations for in-flight entries (issue #49) are fixed by merged PR #52. Nothing to do.
7. The paper account is flat: `mmr --json portfolio` shows no position, `mmr --json orders` shows no working order.
8. Back up: `./docker.sh -B before_sp2`
   - check: it prints "Backup complete". It copies the `*.duckdb` files of the data volume only (not `ai.duckdb`, not the research DB).

## 1. Stop everything

1. `docker compose --profile ai stop ai research`
2. `docker compose --profile ai rm -f ai research`
   - Needed: a stopped container still holds its volume (step 2.2 would fail).
3. `./docker.sh -d`
   - check: `docker compose --profile ai ps` lists nothing.

## 2. Fresh tables and volumes

Some tables were edited in place by the SP2 and SP2c plans. DuckDB keeps the old shape, so the new code fails on them.
Owner rule: no legacy data, no upgrade. Drop them and let the migrations create them again.

1. Drop the journal tables and the signal record tables (journal migrations 55, 56, 63):
   ```bash
   docker compose run --rm --no-deps --entrypoint python scheduler -c "
   import duckdb, os
   d = '/home/trader/.local/share/mmr/data/'
   if os.path.exists(d + 'mmr_journal.duckdb'):
       j = duckdb.connect(d + 'mmr_journal.duckdb')
       for t in ('ai_paper_decisions', 'ai_deployments', 'ai_costs', 'simulated_books'):
           j.execute('DROP TABLE IF EXISTS ' + t)
       j.execute('DELETE FROM schema_migrations WHERE version IN (55, 56, 63)')
       j.close()
   if os.path.exists(d + 'mmr.duckdb'):
       m = duckdb.connect(d + 'mmr.duckdb')
       for t in ('strategy_signal_record', 'strategy_signal_record_state', 'strategy_signal_record_generation'):
           m.execute('DROP TABLE IF EXISTS ' + t)
       m.close()"
   ```
   - check: no Python error. If it says the file is locked, step 1 was not complete: repeat step 1.
   - Paths are the `trader.yaml` defaults. Use yours if you changed `duckdb_path` or `journal_duckdb_path`.
2. Delete the old ai volume. Skip if `docker volume ls | grep mmr_ai_data` shows nothing:
   `docker volume rm mmr_mmr_ai_data`
   - check: no error. "volume in use" means a container still exists: repeat step 1.2.
3. Do not touch the research volume `mmr_research_data`. It is new, so it starts empty.

## 3. Keys

1. `./docker.sh -b` (the keygen entry point is in the image).
2. **OWNER ONLY:** `mmr keys init-signing`
   - Creates `~/.config/mmr/keys/private/signing.pem` (0600) and `keys/verify/paper-automation.pem`. It never overwrites.
3. `./docker.sh -k`
   - Creates every missing RPC pair, including `research`. It never overwrites.
   - check: `ls ~/.config/mmr/keys/rpc` shows `.key` and `.pub` for `trader strategy cli dashboard ai_supervisor ai_research research`.
4. `./docker.sh -K`
   - It runs alone and does not touch the live stack.
   - check: "Key check passed for every service". On a failure, stop. Do not go on.
   - This replaces gate P0.3 in the acceptance runbook.
5. Never print the `.key` files. Back up `keys/` (`./docker.sh -k --backup` needs an age recipient file).

## 4. Config

Config templates are copied only when a file is missing, so your old files lack the new keys. Edit by hand.

1. `~/.config/mmr/trader.yaml` (edit existing top-level keys, do not add a second `ai_paper:`):
   ```yaml
   trading_mode: paper
   ai_paper:
     enabled: true
     experiment_kill_drawdown_pct: 20     # your choice, percent
     model_budget_usd_per_day: 50         # your choice, default is 2000
     acceptance_probe: false              # true only on the acceptance day
     backtest_judge:
       strategy_allowlist:                # the authority; empty = nothing is evaluated
         - "strategies/opening_range_breakout.py:OpeningRangeBreakout"
   automation:
     quote_fallback: alpaca_iex           # only if IB paper gives delayed quotes (step 7.0)
   ```
   - An unknown key stops the trader at start and the error names it.
   - Leave `ai_paper.telegram.enabled` false.
2. `./docker.sh -b -u` once to copy `ai.yaml` to `~/.config/mmr/` (it also starts the stack, without the `ai` profile).
   Then edit `~/.config/mmr/ai.yaml`:
   - `roles.orchestrator` and `roles.jev`: `backend` and `model`. A blank model stops the service. Jev stays on `openrouter`.
   - `pricing:` one row per model: `"vendor/model-id": {input_usd_per_million: X, output_usd_per_million: Y}`. Take X and Y from the provider page. No price means the call is refused.
   - `research:` block. Set `enabled: true`, and:
     - `strategy_keys`: the same keys as the allowlist above.
     - `universes`: 8 to 20 conids. It must include AAPL 265598 and MSFT 272093 (acceptance trades them). Check each conid: `mmr resolve AAPL`.
     - `bar_sizes`: only sizes you download in step 4.3.
     - `max_cohort_points` at or below `ai_paper.backtest_judge.max_cohort_points` (3).
   - Leave `decisions.discretionary_deployment_digest: null` for now (step 8).
3. Bars for research. Every research conid and SPY need history, and the daily bars must be refreshed every day.
   Without them the evaluation stops ("download them before evaluating") or an ENTER is refused `HISTORY_INVALID`.
   Run inside the scheduler container, because the bars live in a Docker volume, not on your Mac:
   ```bash
   docker compose run --rm scheduler mmr data download SPY --bar-size "1 day" --days 1400
   docker compose run --rm scheduler mmr data download AAPL MSFT <more symbols> --bar-size "1 day" --days 1050
   docker compose run --rm scheduler mmr data download AAPL MSFT <more symbols> --bar-size "15 mins" --days 1050
   ```
   - The evaluation reads about 690 sessions of each `bar_size`; SPY needs 220 more daily sessions. Both day counts are my estimate.
   - `mmr data download` puts the symbols in the `downloads` universe. The scheduler refreshes it daily at 20:30 ET (`us_top20_daily`).
   - Only `1 day` (365 days) and `1 min` (90 days) have refresh jobs (ticket #111 tracks research bar sizes). For another bar size, add a job to `~/.config/mmr/data_refresh.yaml` and its name to
     `data_refresh_us` in `~/.config/mmr/pycron.yaml`.
   - check: `docker compose run --rm scheduler mmr data status` shows a recent last bar date for every universe.

## 5. Build and start

1. `./docker.sh -b -u`
2. `docker compose --profile ai up -d --force-recreate ai research`
   - `./docker.sh -u` does not start profile services. Run this line after every rebuild and every `ai.yaml` edit.
     A bind-mounted file is not re-read by a plain restart.
   - check: `docker compose --profile ai ps` shows `ai` and `research` as "healthy" within about 2 minutes.
   - If `research` exits in a loop: the trader has `ai_paper.enabled` off, or `signing.pem` is missing or not owned by you. Read `docker compose --profile ai logs research`.
   - If `ai` exits: the log names the bad field in `ai.yaml` (`RESEARCH_*` codes for the research block).

## 6. Checks

1. `mmr status`
   - check: trader reachable, `ib_upstream_connected: true`, paper. If IB is not connected: VNC to `localhost:5901`, log in, retry.
2. `mmr --json experiment status`
   - check: `mode_conflict` is null, `kill_line.pending_restart` is false.
3. `docker compose --profile ai exec -T ai cat /tmp/mmr_ai_heartbeat.json`
   - check: `epoch` is a number (the `ai` service waits up to 60 s for it), `budget_cap_ready` is true, `research` is an object (not null).
4. `./docker.sh -B after_sp2_start`, then read the migration list from that backup copy (no lock on the live file):
   ```bash
   docker compose run --rm --no-deps --entrypoint python scheduler -c "
   import duckdb
   c = duckdb.connect('/home/trader/.local/share/mmr/backups/after_sp2_start/mmr_journal.duckdb', read_only=True)
   print(sorted(r[0] for r in c.execute('SELECT version FROM schema_migrations').fetchall() if r[0] >= 90))"
   ```
   - check: the list contains 90, 95, 96, 100, 110, 111, 115, 116, 120, 125.
5. `docker compose --profile ai logs --tail 50 ai research` has no ERROR lines.
   - `ai.duckdb` has migrations 1-5, 10-17, 20-22, 30-34 and the research DB 1-11, 20-22. No `mmr` command lists them.

## 7. SP1 paper acceptance (OWNER ONLY, places IB orders)

SP1 must be accepted before SP2 trades. Everything is in [PAPER_ACCEPTANCE_SP1.md](PAPER_ACCEPTANCE_SP1.md). It needs:
the paper account `DU...`, a live (not delayed) IB feed, an operator signing key (P0.6), `acceptance_probe: true` for the day,
a normal US trading day, and a judged deployment version for AAPL and MSFT.

0. Live feed: `mmr --json snapshot AAPL --source ib`. If bid and ask are delayed, `automation.quote_fallback: alpaca_iex` (step 4.1)
   is the owner's workaround from issue #74. P1.4 of the runbook still wants a live IB feed. You decide.
1. Get the judged version (needs a night):
   - `mmr experiment start --reason "sp2 research first night"` (**OWNER ONLY**)
   - `mmr experiment pause --reason "research only until acceptance"`
   - The research slot opens 30 minutes after the close and ends 30 minutes before the next open. Come back the next morning.
   - Read the result:
     ```bash
     docker compose --profile ai exec -T ai python -c "
     import duckdb
     c = duckdb.connect('/home/trader/.local/share/mmr_ai/ai.duckdb', read_only=True)
     for r in c.execute('SELECT state, line_state, version_digest, expiry_session, error_code FROM ai_research_registrations').fetchall(): print(r)"
     ```
   - check: a row with `state` REGISTERED and a `version_digest` (`sha256:...`). Then `mmr ai-deployment version <version_digest>` shows it.
     It becomes ACTIVE at the next session.
   - A lock error: run it again, or stop `ai` first. No row after a few nights: read `ai_research_cycles` (`reason`) and `docker compose --profile ai logs ai`.
     Jev may answer SHADOW or REJECT. Nothing registers a version for you; the harness does not.
2. Make room for acceptance (once the version is ACTIVE on a session day):
   - `mmr experiment stop --reason "before sp1 acceptance"` (must be flat; the harness arms its own experiment)
   - `docker compose --profile ai stop ai` (else `CONTROLLER_EPOCH_HELD`)
3. Set `ai_paper.acceptance_probe: true`, then `docker compose restart trader`. Follow the runbook from P0.4 (skip P0.3, done by `-K`).
   Pass `--deployment-version <version_digest>` to the run.
4. After P6.4: set `acceptance_probe: false` again, `docker compose restart trader`.
   - check: `mmr --json experiment status` shows the acceptance experiment as STOPPED.

## 8. Start the AI loop (OWNER ONLY)

1. Policy. Make `policy.yaml` with exactly these seven limits (the paper limits) and publish it:
   ```yaml
   limits: {max_positions: 3, position_fraction: 0.05, gross_fraction: 0.06, trade_risk_fraction: 0.002,
            daily_loss_fraction: 0.005, drawdown_fraction: 0.03, max_pending_entry_orders: 3}
   ```
   `mmr ai-policy show` first. If a policy is already published, check it instead. Then:
   `mmr ai-policy publish policy.yaml --reason "initial paper policy"`
   - check: `mmr ai-policy show` lists the limits as effective (or queued until the next session).
2. `mmr ai-deployment register-discretionary --operator <your name> --statement "<why>"`
   - check: it prints a digest. `mmr ai-deployment show <digest>` shows kind `discretionary`.
3. Put that digest in `~/.config/mmr/ai.yaml` under `decisions.discretionary_deployment_digest`.
   Optional: `decisions.ai_deployments` brackets (stop and target fractions).
4. `docker compose --profile ai up -d --force-recreate ai research`
5. `mmr experiment start --reason "sp2 paper"`
   - check: `mmr --json experiment status` says ARMED, `entry_block` null. A refusal code says why (`NOT_FLAT`, `ONE_STRATEGY_ARMED`, `AI_PAPER_DISABLED`, `BREAKER_TRIPPED`).
   - Entries come from `aidv-` strategy instances (ACTIVE judged versions) and from the discretionary discovery cycles.
     A plain `decisions.strategies` entry does not trade.

## 9. First week

Every morning:
- `docker compose --profile ai ps` and the heartbeat (step 6.3). `open_candidates` and `unrecorded_judgments` should reach 0 overnight.
- `mmr --json experiment status` (state, kill line) and `mmr scoreboard` (every view says PAPER; books per baseline; AI cost with status).
- `mmr scoreboard verify` exits 0.
- `mmr --json orders` and `mmr --json portfolio`: after 15:55 ET nothing open (the session flatten runs 15:45).
- Logs: `docker compose --profile ai logs --since 24h ai research | grep -E "ERROR|WARNING"` and `~/.local/share/mmr/logs/trader_service_*.log`.
- Research tables in `ai.duckdb`: `ai_research_cycles`, `ai_research_candidates`, `ai_backtest_judgments`, `ai_research_registrations`. Codes are in OPERATIONAL_STATE.md ("AI research cycle").
- Spend against `model_budget_usd_per_day`. A spent budget gives `NO_VERDICT` and no entries.
- A DEPLOY version expires after 20 sessions and is renewed on forward evidence. Keep the old `keys/verify/*.pem` files.

Stop commands:

| Goal | Command |
|---|---|
| No new AI entries | `mmr experiment pause --reason "<why>"` |
| Stop the AI service | `docker compose --profile ai stop ai` |
| Stop the research cycle only | `research.enabled: false` in `ai.yaml`, then step 8.4 |
| End one judged version | `mmr ai-deployment withdraw sha256:<version> --reason "<why>"` |
| Close everything (paper) | `mmr flatten --reason "<why>" --wait` (must print FLAT), then `mmr reconcile`, then `mmr experiment stop --reason "<why>"` |
| Stop the whole stack | `docker compose --profile ai stop ai research`, then `./docker.sh -d` |

A KILLED experiment is never resumed: flatten, `mmr reconcile`, `mmr experiment stop`, then a new `experiment start`.
`./docker.sh -d` keeps all data. Never run `./docker.sh -c` or `-f` here: they delete volumes.
