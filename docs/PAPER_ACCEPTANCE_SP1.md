# SP1 paper acceptance — owner runbook

One real IB paper session proves SP1 before SP2 trades (spec section 6). Only the
owner runs it. Every order in it comes from `mmr experiment acceptance run
--place-orders`; nothing else places orders. Times are US Eastern on a normal
(non-half) XNYS day. Paper account only.

## Before the day (any time)

| Step | Command | Pass | Fail → |
|---|---|---|---|
| P0.1 build | `./docker.sh -b` | exit 0 | stop, fix the build |
| P0.2 keys | `./docker.sh -k` | `~/.config/mmr/keys/rpc/` has `trader`, `strategy`, `cli`, `dashboard`, `ai_supervisor`, `ai_research`, `research` `.key` (0600) and `.pub` (also `mmr keys init-signing` once, for `keys/private/signing.pem`) | stop |
| P0.3 cutover gate (Plan 2) | `./docker.sh -K` (runs alone; needs `signing.pem`). One short-lived container per service (`trader strategy dashboard cli scheduler data ai research`) runs `mmr keys check-mount`: it must see exactly its own `.key`, its own `.pub` and its peers' `.pub`, the keys must load as at startup, and the retired HMAC file must read empty. It starts no service and does not touch the running stack. The old `fullstack-tests` profile is no longer the gate | "Key check passed for every service" | stop; do not `-u` |
| P0.4 config | in `~/.config/mmr/trader.yaml`: `trading_mode: paper`, `ai_paper.enabled: true`, `ai_paper.telegram.enabled: false` (unless you supplied a bot and chat id; the report then says `telegram_live_delivery: UNTESTED`), `ai_paper.acceptance_probe: true` (needed by P4S; set it back to `false` after P6.4; Plan 3 parses it, default `false`, so the key is legal), `experiment_kill_drawdown_pct` as you choose | after P1.2: `mmr status` shows the trader up, and `mmr --json experiment acceptance preflight` shows `"acceptance_probe": true` in both readings (an unknown-key config error at start means the key is misspelled) | fix the file |
| P0.5 history | after P1.2, inside the scheduler container (the trader reads the `mmr_db_data` volume, not a host file): `docker compose run --rm scheduler mmr data download SPY --bar-size "1 day" --days 30` | rows written | scoreboard SPY shows unknown; not a blocker |
| P0.5a judged deployment (SP2c Plan 2) | take the `version_digest` from the SP2c registration outcome; `mmr ai-deployment show <base digest>` shows the base deployment. No CLI shows a version's state yet. | you hold a version digest (`sha256:...`) whose base deployment lists AAPL (265598) and MSFT (272093); check again on the session day that it was not withdrawn or renewed | stop: the harness registers nothing. P4.2 refuses `DEPLOYMENT_VERSION_REQUIRED` without a version, and its `deployment` step stops at `DEPLOYMENT_NOT_USABLE`, before any write, when the version is not `ACTIVE`, today is outside its first and expiry session, or a conid is missing. Until SP2c research registers a version, SP1 acceptance cannot run. |
| P0.6 operator key | `mkdir -p ~/.config/mmr/keys/acceptance && openssl genpkey -algorithm ed25519 -out ~/.config/mmr/keys/acceptance/operator.key && openssl pkey -in ~/.config/mmr/keys/acceptance/operator.key -pubout -out ~/.config/mmr/keys/acceptance/operator.pub && chmod 600 ~/.config/mmr/keys/acceptance/operator.key` (once; keep both files, back them up; never use an RPC key) | both files exist | stop: the real report cannot be signed |

## Session day

### P1 — start and smoke (09:00–09:30)

| Step | Command | Pass | Fail → |
|---|---|---|---|
| P1.1 backup | `./docker.sh -B before_acceptance` | backup file named | stop |
| P1.2 start | `./docker.sh -u` | containers up | abort A1 |
| P1.3 status | `mmr status` | trader reachable, `ib_upstream_connected: true`, paper | VNC to the gateway (port 5901), then retry; else stop |
| P1.4 live feed | `mmr --json snapshot AAPL --source ib` and `mmr --json snapshot MSFT --source ib` | bid and ask present, not delayed | stop: every `ENTER` would be refused `FEED_NOT_LIVE` |
| P1.5 read smoke | `scripts/paper_e2e.sh tests/paper_e2e/test_stack_gate.py tests/paper_e2e/test_typed_rpc_queries.py` (with `MMR_PAPER_E2E_LIVE_ORDERS` **unset**) | all pass or skip with a reason | stop |

### P2 — the clean-account gate (#34: no IB reset by default)

| Step | Command | Pass | Fail → |
|---|---|---|---|
| P2.1 gate | `mmr experiment acceptance preflight` | prints `PASS`, exit 0 | prints `STOP` and the reasons: **stop here**. Cleanup is your decision: `mmr flatten --reason "pre-acceptance cleanup" --wait`, cancel orders in the dashboard (`/cc` → cancel all), `mmr reconcile`; or an IB paper-account reset in Client Portal (takes effect the next day). Then run P2.1 again. |

The gate passes only if: paper account (`DU…`); no position; no working order;
no unresolved command; no open liquidation or exit owner; breaker clear; net
liquidation finite, above zero, at most 5 minutes old, stable within 0.1 % over
30 seconds; daily P&L known; base currency USD or a USD rate.

### P3 — arm (09:35–09:40)

| Step | Command | Pass | Fail → |
|---|---|---|---|
| P3.1 arm | `mmr experiment start --reason "sp1 acceptance"` | state `ARMED`, start net liquidation shown | the refusal code says why; fix, re-run P2.1, retry |
| P3.2 confirm | `mmr --json experiment status` | `ARMED`, `kill_line.active` as configured, `pending_restart: false`, `mode_conflict: null` | `mmr experiment stop --reason "arm check failed"`; stop |

### P4 — the scenario (09:40–14:30)

| Step | Command | Pass | Fail → |
|---|---|---|---|
| P4.1 dry run | `mmr experiment acceptance run` | prints the planned calls, "dry run", sends nothing | stop |
| P4.2 run | `mmr experiment acceptance run --place-orders --deployment-version <version digest> --confirm-account <DU account> --signing-key ~/.config/mmr/keys/acceptance/operator.key --report ~/.local/share/mmr/acceptance/run-report.json` | every step `PASS` including `shrink_proof` (`oca_shrink: PROVEN`) and `settle_s`; prints `report passed: True` and ends with "B open, protected; waiting for the session flatten" (exit 0 only when the signed report passed); note the printed `run_id`. `OCA_SHRINK_UNPROVEN`: the session is **not** acceptance; flatten (A2), stop, try another day; do not retry today. `OCA_SIBLING_NOT_SHRUNK` or `PROBE_OCA_LOST`: abort A4 at once | abort A2 |
| P4.3 orders | `mmr --json orders` | only B's protective stop is working | abort A2 |

What P4.2 does, in order: two preflight reads 30 s apart (it refuses
`PROBE_NOT_ENABLED` here, before any order, if `ai_paper.acceptance_probe` is
off); read the `--deployment-version` and its base deployment
(`ai_supervisor`; it must be `ACTIVE` and cover A and B, else
`DEPLOYMENT_NOT_USABLE`; the harness registers nothing); publish the paper
limits (`ai_supervisor`); every `ENTER` names that version and the base
deployment's `strategy_digest`; `ENTER` A (3 shares, with a stop and a target) and B (1 share) with explicit
sizes; `PARTIAL_CLOSE` 1 share of A (the existing stop and target re-placed for
2 shares in one OCA group; a partial close carries no prices); `CLOSE` the rest of A; then P4S below. B stays open. Before each
entry the harness reads the live ask and refuses `HARNESS_NOTIONAL_TOO_SMALL`
when `quantity x ask` is above the 2,000 USD notional. It never raises the
notional. Every request is written to `~/.local/share/mmr/acceptance/<run_id>/journal.jsonl`
before it is sent; `mmr experiment acceptance status --run-id <run_id>` shows
the steps from that file.

Evidence to look at during P4: after the partial close, the report's
`order_evidence` (from the typed broker-order evidence read; `mmr orders` does
not show OCA fields) lists A's stop and target with the same OCA group,
`oca_type` 2 and remaining 2 each. If an entry filled only partly and the AI
cutoff cancel (15:30) cancelled the rest, record whether IB shrank the bracket
children.

#### P4S — the live OCA shrink proof (mandatory; part of P4.2, spec 5.1)

The harness does this inside `run` after `CLOSE` A. It is not optional, and a session
without `PROVEN` is not SP1 acceptance.

| Step | What happens | Pass | Fail → |
|---|---|---|---|
| P4S.1 enter S | `ENTER` AAPL 3 shares (`<run_id>-e-s`) with stop at ask x 0.98 and target at ask x 1.02; the bot places both in one `ocaType=2` group | evidence read: stop and target both acknowledged and working, same non-empty group, `oca_type` 2, quantity 3, position 3; only then does the harness send the mark and the probe | `S_NOT_PROTECTED_WITH_TARGET`: abort A2 |
| P4S.2 probe | the harness sends `acceptance_mark_start` (your `cli` key), then `acceptance_shrink_probe`: the trader modifies a copy of S's working target — limit at the bid, display size 1, every other field kept, OCA link included — and re-reads the broker | command accepted, `oca_type` still 2 | `PROBE_OCA_LOST`: abort A4 at once; another `PROBE_*` refusal: abort A2; the gate stays open |
| P4S.3 observe | the harness reads the evidence every 0.5 s for 60 s and journals each reading | a target event with 0 < filled < 3 and a later stop event with remaining equal to 3 - filled, same generation, position matching: `oca_shrink: PROVEN` | no partial event: `OCA_SHRINK_UNPROVEN`, settle (P4S.4), flatten (A2), no retry today. Partial but stop unchanged: `OCA_SIBLING_NOT_SHRUNK`, abort A4 |
| P4S.4 settle | after `PROVEN` or `OCA_SHRINK_UNPROVEN` the harness runs `settle_s`; it never waits for the target to finish. If a promoted generation already shows S flat with no working S orders (e.g. a whole target fill), it records `settled_by: already_flat` and sends **no** CLOSE (a CLOSE with no position is refused `NOT_A_REDUCTION`). Otherwise it sends the stored `CLOSE <run_id>-c-s` once (the safe close cancels or joins the live stop and target) and records `settled_by: close` | position 0 and no working order for S on a promoted generation; for `close` also a complete close root. UNPROVEN still fails the gate | `S_NOT_FLAT`: abort A2 |

Manual cross-check (keep it with the evidence): in Client Portal, Reports, Trade
Confirmation, S's target shows executions of 1 share, and TWS/Gateway order
history shows the stop quantity falling with them. The harness never engineers a
partial fill by size, symbol or price beyond the one display-size change above.

### P5 — the session flatten (15:30–15:55, hands off)

| Time | What happens | Check |
|---|---|---|
| 15:30 | entry cutoff; AI entries cancelled | `mmr --json orders`: no AI entry working |
| 15:45 | session flatten starts | — |
| by 15:55 | flat proven on a broker generation | `mmr --json portfolio`: no position; `mmr --json orders`: nothing working |

Do not trade in this window. If 15:55 passes with a position or order: abort A3.

### P6 — finish and verify (after 15:55)

| Step | Command | Pass | Fail → |
|---|---|---|---|
| P6.1 finish | `mmr experiment acceptance finish --run-id <run_id> --signing-key ~/.config/mmr/keys/acceptance/operator.key --report ~/.local/share/mmr/acceptance/finish-report.json` | every end check `PASS`: `equity_daily` row `FLAT` with `open_positions` 0, no position and no working order, trips through `get_experiment_trips` (A one closed trip of 3 shares, S one closed trip of 3 shares — the partial fills plus the settle close — and B one closed trip of 1 share), `oca_shrink` `PROVEN`, no incidents; exit 0 only when the signed report says `passed: True` | record; the run failed |
| P6.2 verify | `mmr scoreboard verify` | exit 0, no mismatch | record; the run failed |
| P6.3 scoreboard | `mmr --json scoreboard > ~/.local/share/mmr/acceptance/scoreboard.json` | label `PAPER`, today `FLAT` | record |
| P6.4a verify the report | `mmr experiment acceptance verify-report ~/.local/share/mmr/acceptance/finish-report.json --public-key ~/.config/mmr/keys/acceptance/operator.pub` | `signature OK`, `signing_key: operator`, `evidence_source: ib_paper`, `deployment_record: judged_version`, `live_restart_recovery: NOT_PROVEN`, `oca_shrink: PROVEN`, `passed: True` | the run failed |
| P6.4 stop | `mmr experiment stop --reason "sp1 acceptance done"` | `STOPPED` | `NOT_FLAT`: abort A3 |
| P6.5 backup | `./docker.sh -B after_acceptance` | backup file named | — |

## Evidence to keep

Copy into `~/.local/share/mmr/acceptance/<run_id>/` (the harness already wrote
`journal.jsonl` and the reports there):

- `run-report.json`, `finish-report.json` (signed);
- `scoreboard.json` and the output of `mmr scoreboard verify`;
- `mmr --json experiment status` before arming and after stopping;
- the `order_evidence` rows in the signed reports (OCA group, type and remaining quantity, with the `status_events` of S's target and stop; every shrink-proof reading is also in `journal.jsonl`);
- the trader log of the day: `~/.local/share/mmr/logs/trader_service_*.log`;
- the IB paper trade confirmations for the day (Client Portal → Reports →
  Trade Confirmation), downloaded by you;
- the two backup names (`before_acceptance`, `after_acceptance`).

The session passes only if P4.2 (with `oca_shrink` `PROVEN`), P6.1, P6.2 and
P6.4a all pass. One such session is enough; a session with `UNPROVEN` or
`FAILED` is not a pass. The report states that the run traded under an
operator-given SP2c-judged deployment version (`deployment_record:
judged_version`; the trader bound every `ENTER` to it and its strategy digest)
and that live restart recovery was not proven (the synthetic restart
tests are the gate). Telegram delivery stays `UNTESTED` unless you supplied a
bot and chat id and saw the messages yourself.

## Abort and flatten

| Code | When | Do |
|---|---|---|
| A1 | the stack does not start or IB is down before arming | `./docker.sh -d`; nothing was armed; retry another day |
| A2 | a harness step fails, or anything looks wrong while positions are open | 1. `mmr experiment pause --reason "abort"` (no new AI entries). 2. `mmr flatten --reason "acceptance abort" --wait` → must print `FLAT`. 3. `mmr reconcile`. 4. `mmr experiment stop --reason "aborted"`. 5. Keep the evidence. |
| A3 | the session flatten did not reach flat by 15:55, or `stop` says `NOT_FLAT` | `mmr flatten --reason "flatten missed" --wait`; if it does not print `FLAT` within 5 minutes or the trader is unreachable: close the positions by hand in IB Client Portal or TWS with the paper login (IB Gateway has no order screen), cancel every working order there, then `mmr reconcile` and `mmr experiment stop --reason "flattened by hand"`. A tripped breaker stays tripped until you clear it after a review. |
| A4 | `OCA_SIBLING_NOT_SHRUNK` or `PROBE_OCA_LOST`: IB did not shrink the sibling after a partial fill, or the modify lost the OCA link (a possible oversell risk) | 1. `mmr experiment pause --reason "oca shrink failed"`. 2. `mmr flatten --reason "oca shrink failed" --wait` → must print `FLAT`. 3. In Client Portal cancel any working S stop or target. 4. `mmr reconcile`; `mmr experiment stop --reason "aborted"`. 5. Keep the evidence and report it as a defect in the Plan 1 OCA design; SP1 is not accepted. |

Never re-run `mmr experiment acceptance run` with a new run id after a failure
on the same day; resume with `--run-id` only if the failure was the host
process (`mmr experiment acceptance run --run-id <run_id> --place-orders --deployment-version <version digest> --confirm-account <DU account> --signing-key ~/.config/mmr/keys/acceptance/operator.key`, with the same version digest; another one is refused `DEPLOYMENT_VERSION_MISMATCH`).
A resume validates the account against the journal and replays the stored
request bodies unchanged; it sends no new order and never re-sends the mark or
the probe.
