# SP2 paper deploy checklist

First paper deployment of SP2 (AI decision loop and backtest judge). Follow the steps top to bottom.
Do not skip a "check:". Paper only. Never paste a key, token or `.env` value into chat or a commit.

- Run commands from the repo root, on your Mac.
- `mmr` means the host command (`pip install -e .` in a venv). It talks to the trader on 127.0.0.1.
- **OWNER ONLY** marks a step that uses a signing key, arms trading or places IB orders.
- Details live in [OPERATIONAL_STATE.md](OPERATIONAL_STATE.md) and [PAPER_ACCEPTANCE_SP1.md](PAPER_ACCEPTANCE_SP1.md).

## Why the order is like this

- SP1 acceptance needs a judged deployment version. Only the research cycle makes one.
- The research cycle needs a started experiment, so step 7 starts one and pauses it. A pause stops every entry
  not yet being sent. The pause receipt names any entry already being sent (`entries_in_flight`): check it and
  cancel or flatten it (#124). In step 7 there is none, because no policy and no discretionary digest exist yet.
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
   from trader.config import MMRConfig
   storage = MMRConfig.from_yaml(os.path.expanduser('~/.config/mmr/trader.yaml')).storage
   print('journal:', storage.journal_duckdb_path, '| main:', storage.duckdb_path)
   for path in (storage.journal_duckdb_path, storage.duckdb_path):
       assert os.path.exists(path), path + ' is missing: check trader.yaml before dropping anything'
   j = duckdb.connect(storage.journal_duckdb_path)
   for t in ('ai_paper_decisions', 'ai_deployments', 'ai_costs', 'simulated_books'):  # simulated_books: a v0.2.0 table SP2 no longer creates; its replacements simulated_decisions/outcomes (95, 96) are new and never edited
       j.execute('DROP TABLE IF EXISTS ' + t)
   j.execute('DELETE FROM schema_migrations WHERE version IN (55, 56, 63)')
   j.close()
   m = duckdb.connect(storage.duckdb_path)
   for t in ('strategy_signal_record', 'strategy_signal_record_state', 'strategy_signal_record_generation'):
       m.execute('DROP TABLE IF EXISTS ' + t)
   m.close()"
   ```
   - check: no Python error, and the printed `journal:` path is the file your trader uses. If it says the file is
     locked, step 1 was not complete: repeat step 1.
   - Without `journal_duckdb_path` in `trader.yaml` the journal is `<duckdb_path>.journal` (#174). If both that file
     and `mmr_journal.duckdb` exist, the config refuses to load until you set `journal_duckdb_path` to the right one.
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
   - check: `ls ~/.config/mmr/keys/rpc` shows `.key` and `.pub` for `trader strategy cli dashboard ai_supervisor ai_research ai_author research`.
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
   Bars are stored by conId, so each symbol must resolve first (#178). The trader runs since step 4.2. Put every
   research symbol and SPY in the `downloads` universe through the `cli` service (it holds the RPC key and the data
   volume). `--currency USD` picks the US listing; most US stocks also have listings abroad (#179):
   ```bash
   docker compose --profile tools run --rm cli universe add downloads SPY AAPL MSFT <more symbols> --currency USD
   docker compose --profile tools run --rm cli universe show downloads
   ```
   - check: one row per symbol, each with a US `primaryExchange` (NASDAQ, NYSE, ARCA) and the conId you put in
     `research.universes`. A symbol with several listings is refused and every listing is named: pick one.
   Then download inside the scheduler container (the bars live in a Docker volume, not on your Mac). It finds each
   symbol in `downloads`, so the bars are stored under its conId:
   ```bash
   docker compose run --rm scheduler mmr data download SPY --bar-size "1 day" --days 1400
   docker compose run --rm scheduler mmr data download AAPL MSFT <more symbols> --bar-size "1 day" --days 1050
   docker compose run --rm scheduler mmr data download AAPL MSFT <more symbols> --bar-size "1 min" --days 1050
   ```
   - check: each command exits 0. A symbol that is not in `downloads` is refused (`did not resolve to a conId`) and
     nothing is stored for it (#177: any failed symbol exits 1).
   - The evaluation reads about 690 sessions of each `bar_size`; SPY needs 220 more daily sessions. Both day counts are my estimate.
   - The scheduler refreshes the `downloads` universe daily at 20:30 ET (`us_top20_daily`).
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
   - If the log says `Cannot open file ".../mmr_research.duckdb": Permission denied`: the `mmr_research_data` volume was
     created by an image before #180 and is owned by root. A volume keeps its owner, so fix it once while it is empty:
     `docker run --rm -v mmr_mmr_research_data:/v alpine chown 1000:1000 /v`, then start `research` again.
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
   - `ai.duckdb` has migrations 1-5, 10-18, 20-22, 30-34, 40 and the research DB 1-11, 20-22. No `mmr` command lists them.
6. `mmr --json breaker status`
   - check: `state` is `CLEAR`. A `TRIPPED` breaker refuses `experiment start` in step 7 (`BREAKER_TRIPPED`). Review the
     trip reason with `mmr reconcile unknown`, `mmr --json portfolio` and `mmr --json orders`, then, during the
     session (09:30 to 16:00 ET), `mmr breaker reset --reason "<what you checked>"` (issue #181).

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
| No new AI entries | `mmr experiment pause --reason "<why>"`. A pause stops every entry not yet being sent; the receipt names any entry already being sent (`entries_in_flight`, also printed by the CLI): check `mmr portfolio` and `mmr orders`, then cancel or flatten it (#124) |
| Stop the AI service | `docker compose --profile ai stop ai` |
| Stop the research cycle only | `research.enabled: false` in `ai.yaml`, then step 8.4 |
| End one judged version | `mmr ai-deployment withdraw sha256:<version> --reason "<why>"` |
| Close everything (paper) | `mmr flatten --reason "<why>" --wait` (must print FLAT), then `mmr reconcile`, then `mmr experiment stop --reason "<why>"` |
| Stop the whole stack | `docker compose --profile ai stop ai research`, then `./docker.sh -d` |

A KILLED experiment is never resumed: flatten, `mmr reconcile`, `mmr experiment stop`, then a new `experiment start`.
`./docker.sh -d` keeps all data. Never run `./docker.sh -c` or `-f` here: they delete volumes.

## 10. SP3: AI-written strategies (OWNER ONLY)

Built and merged with the switch off and the author model empty. Turn it on in this order. Do it after sections 1 to 9
work. Never write a key or a token into a file you share.

1. Keys. `./docker.sh -k` creates the missing pairs, including `ai_author.key` and `ai_author.pub`. Only the `ai`
   service mounts `ai_author.key`; the trader mounts `ai_author.pub`. Then `./docker.sh -K`.
   - check: `ls ~/.config/mmr/keys/rpc` shows `ai_author.key` and `ai_author.pub`; `-K` prints "Key check passed for
     every service". A mount of `ai_author.key` outside `ai` fails it. On a failure, stop.
2. Build and start the sandbox. `./docker.sh -b -u` as in section 5 (the build id comes from `./docker.sh -b`; a plain
   `docker build` gives a sandbox that does not start). Leave the switch off: no `authored_strategies` block in
   `~/.config/mmr/trader.yaml`, or `ai_paper.authored_strategies.enabled: false`. Leave `roles.author.model` empty in
   `~/.config/mmr/ai.yaml`. `sandbox` is in compose profile `ai` (also `test`), so `./docker.sh -u` does not start it:
   `docker compose --profile ai up -d sandbox`, then `docker compose --profile ai up -d --force-recreate ai research`
   (as in section 5).
   - check: `docker compose --profile ai ps` shows `sandbox`, `research` and `ai` as "healthy" within about 2 minutes.
   - check: `docker compose --profile ai exec -T sandbox cat /etc/mmr-build-id` prints a build id (git sha and a time),
     not `unset`.
3. Check the authored store mount. The trader writes `/store`, the sandbox reads it. A missing or unreadable store
   file is `SANDBOX_DIGEST_MISMATCH`, and that code retires the deployment. So a wrong `/store` mount would retire every
   deployed authored file (OPERATIONAL_STATE.md, "AI-written strategies", known limits). Look at both mounts:
   ```bash
   docker inspect --format '{{.Name}}: {{range .Mounts}}{{if eq .Destination "/store"}}{{.Name}} rw={{.RW}}{{end}}{{end}}' \
       $(docker compose --profile ai ps -q trader sandbox)
   docker compose --profile ai exec -T trader ls -l /store
   docker compose --profile ai exec -T sandbox ls -l /store
   ```
   - check: two lines, both with the same volume name (it ends in `mmr_authored_strategies`); the trader line says
     `rw=true`, the sandbox line says `rw=false`. The two `ls` listings are the same: empty on a first turn-on; later one
     `<64 hex>.py` file per stored strategy, mode `-rw-r--r--`. Any difference: stop, fix the mount, do not go on.
     Run this step again after every rebuild and before step 6.
4. Prove the isolation of the running sandbox (host only): `mmr ai-author prove-isolation`. Run it only while no
   live sandbox session is open (owner ruling 2026-10-11: a hostile probe must never run next to a live strategy's
   child). On first turn-on there is none. Later (after a rebuild): set `ai_paper.authored_strategies.enabled: false`,
   `docker compose restart trader`, wait one strategy reconcile (30 s) so the strategy service closes its authored
   sessions, run the command, then turn the switch back on. If compose says it has no service `research`, run
   `mmr ai-author prove-isolation --compose "docker compose --profile ai"`.
   - check: exit code 0. It prints `RECORDED` (or `EXISTING`) with a proof whose `passed` is `true` and whose
     `image_digest` is the build id from step 2. It never prints "the sandbox image is NOT proven". Until a pass exists
     for that build the trader refuses every authored registration (`AUTHORED_ISOLATION_UNPROVEN`) and the strategy
     service opens no live session for an authored deployment. A rebuilt sandbox image needs a new run.
   - check (only if a live session is open): it fails with `LIVE_SESSIONS_OPEN` (the in-container step exits 3, the
     host command exits 1) and records nothing. Switch authoring off, wait, run again.
5. Set the author model. In `~/.config/mmr/ai.yaml`:
   - `roles.author.backend` and `roles.author.model` (the model id).
   - Its price row under `pricing.<backend>`: `"vendor/model-id": {input_usd_per_million: X, output_usd_per_million: Y}`.
     Take X and Y from the provider page.
   - The provider key in the repo `.env`, the same one `roles.orchestrator` uses: `OPENROUTER_API_KEY` for
     `openrouter`; `AWS_REGION` with `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` for `bedrock`; `AZURE_OPENAI_ENDPOINT`,
     `AZURE_OPENAI_API_KEY` and `AZURE_OPENAI_API_VERSION` for `azure`. Set no new key if that one is already there.
   - Keep `research.enabled: true`. Leave `authoring:` at its defaults (`run_after_close_minutes: 60`,
     `call_timeout_seconds: 300`; do not set `call_timeout_seconds` under `roles.author`).
   - Then `docker compose --profile ai up -d --force-recreate ai` (a bind-mounted file is not re-read by a plain restart).
   - check: `docker compose --profile ai logs --tail 50 ai` has no `AUTHOR_PRICE_MISSING`, `AUTHORING_NEEDS_RESEARCH` or
     `AUTHOR_TIMEOUT_IN_ROLE`, and the service stays "healthy".
   - check: `docker compose --profile ai exec -T ai cat /tmp/mmr_ai_heartbeat.json` has an `author` object
     (`unsubmitted` 0, `last` null before the first night), not `null`.
6. Turn the switch on. Check step 3 again first. In `~/.config/mmr/trader.yaml` under the existing `ai_paper:`:
   `authored_strategies: {enabled: true, max_active: 3}` (it needs `ai_paper.enabled: true`; keep `bar_sizes`
   overlapping `research.bar_sizes` in `ai.yaml`), then `docker compose restart trader`.
   - check: `mmr --json ai-author list` shows `"enabled": true`, `"max_active": 3` and no records yet. An unknown key
     stops the trader at start and the error names it.
7. The next evening (one hour after the close): the heartbeat `author.last` shows `aut-YYYYMMDD` with state `STORED`
   (or a `SKIPPED`, `FAILED` or `REFUSED` code, read OPERATIONAL_STATE.md "AI-written strategies"); `mmr ai-author
   list` shows the file; the research cycle evaluates it the same night (state `EVALUATING`, then `SHADOW` or a final
   state); Jev judges it; a DEPLOY registers (state `DEPLOYED`) and trades from the next session.
   - check: `mmr --json ai-author list` has one record for the file, with its digest, class and state. At most one new
     file per night.
8. Watch `mmr scoreboard`: "AI-written vs catalogue" says `yes` only with at least 20 closed trips each side, a higher
   daily-P&L Sharpe and no worse drawdown. It is a report; it changes no gate.
   - check: the line starts "AI-written vs catalogue (PAPER)" and says `not enough data` with a reason (for example
     `TOO_FEW_TRIPS`) until both sides have 20 closed trips.

Stop: `ai_paper.authored_strategies.enabled: false` and `docker compose restart trader` (no new authoring, evaluation
or authored entries; closes go on; records stay).
   - check: `mmr --json ai-author list` shows `"enabled": false`.

Or empty `roles.author.model` and `docker compose --profile ai up -d --force-recreate ai` (no new authoring; stored files
are not offered for evaluation).
   - check: the heartbeat `author` entry is `null`.
