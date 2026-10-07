# AI Paper Bot SP2a + SP2b: Plan Index

**Spec:** `docs/superpowers/specs/2026-10-07-ai-paper-sp2ab-autonomous-loop-design.md`
(approved by the owner and by both reviewers, PR #69). Ticket #36.

**Base:** master after SP1 Plans 3–6 (#55–#58) are merged. Until then, read the code
at `/private/tmp/sp1-impl6` (master + Plans 3–6).

## Plans and order

| # | Plan | Spec | Depends on |
|---|------|------|------------|
| 1 | Trader: controller epoch, signal record, operator initial policy | 5.1, 6.1, 6.2, 6.2b, 6.7 | SP1 |
| 2 | Trader: cost and simulation ingestion, separate baseline books, simulator | 6.3, 6.8, 7 | SP1 |
| 3 | Trader: discretionary scope rule, discovery read, model-driven closes | 6.4, 6.5, 6.6, 10 | SP1 |
| 4 | `ai` foundations: config, model client + adapters, store, journal, budget, gateway, replay primitives | 4, 5.4, 8, 11 | — |
| 5 | `ai` controller runtime: RPC clients, leadership, schedule, submitter, outbox, signal intake, service, container | 4, 5.1–5.3, 9 | 1, 2, 4 |
| 6 | `ai` decisions: discovery client, orchestrator + Jev, parsers, baselines, flows, acceptance | 5.5, 7, 8, 12 | 3, 5 |

Plans 1–4 can be implemented in parallel. Each plan is one PR.

## Shared interfaces (all plans use these exact names)

Typed RPC methods (allow-list in `trader/messaging/principals.py`; read and mutation rights separate):

| Method | Kind | Principals | Plan |
|---|---|---|---|
| `grant_ai_controller_epoch` | command | `ai_supervisor` | 1 |
| `read_ai_signals` | query | `ai_supervisor` | 1 |
| `publish_ai_risk_policy` | command | `cli` added (operator initial policy); `ai_supervisor` keeps it for SP2d, but SP2a/b code never calls it | 1 |
| `record_ai_cost` | command | `ai_supervisor` | 2 |
| `record_simulated_decision` | command | `ai_supervisor` | 2 |
| `get_ai_model_budget` | query | `ai_supervisor` (read only; no principal writes the cap) | 2 |
| `register_discretionary_deployment` | command | `cli` | 3 |
| `discover_ai_candidates` | query | `ai_supervisor` | 3 |
| `get_ai_entry_quote` | query | `ai_supervisor` | 3 |
| `submit_ai_paper_decision` | command | `ai_supervisor` (existing; Plan 1 adds the epoch check, Plan 3 the scope rule) | 1, 3 |

- `TypedRpcRequest.controller_epoch: Optional[int] = None`. It is always a key in
  `rpc_signing_bytes` (`null` when absent). `submit_ai_paper_decision` refuses a missing
  epoch (`CONTROLLER_EPOCH_MISSING`) or a not-current one (`CONTROLLER_EPOCH_STALE`),
  checked in the same transaction as the command claim. Reconcile reads used by the
  controller also require it.
- Epoch grant: `{holder_id: str, current_epoch: Optional[int], lease_seconds: int}` →
  `{epoch: int, lease_expires_at: iso}`. Same holder + current epoch + live lease → renew
  (same epoch). No live lease → new epoch = previous + 1. Another holder's live lease →
  refused `CONTROLLER_EPOCH_HELD`. Default lease 60 s, renew every 20 s.
- Signal read: `{after_cursor: int, limit: int (1..500)}` →
  `{signals: [{cursor, source_event_id, strategy_name, conid, action: "BUY"|"SELL", probability, signal_time, recorded_at}], next_cursor, oldest_retained_cursor, gap: bool}`.
  `gap` is true when signals after `after_cursor` were already removed by retention.
- Baseline ids (versioned): `follow_signal.v1`, `fixed_rule.v1`, `no_trade.v1`,
  `matched_entry_bracket_exit.v1`. Cohorts: `strategy_signal`, `self_found`, `model_close`.
- `record_simulated_decision` extra fields (Plan 2 Cross-plan additions): `deployment_digest`,
  `linked_round_trip_id` (matched-entry only), `incomplete_reason` (one of `quote_unavailable`,
  `feed_not_accepted`, `budget_refused`, `model_failed`, `sizing_unavailable`). `follow_signal.v1` and
  `fixed_rule.v1` are sent with `quantity: null`; the trader sizes them.
- Model budget cap: `get_ai_model_budget` → `{model_budget_usd_per_day: float, source: "trader.yaml"}`
  (Plan 2). Plan 5 reads it at start and every 60 s and calls Plan 4's `Budget.set_cap`; `ai.yaml`
  has no cap. A failed read refuses model calls (`BUDGET_CAP_UNKNOWN`) until a read succeeds.
- Entry quote: `get_ai_entry_quote {conid}` → `{conid, read_at, account_mode, accepted_feeds: [..],
  quote: null | {bid, ask, bid_size, ask_size, market_timestamp, feed, session_state}}` (Plan 3), from
  the trader's quote authority and its accepted-feed set. Plan 6 reads entry evidence only from it.
- Refusal code for the scope rule: `OUT_OF_DISCRETIONARY_SCOPE`, with `detail.part` one of
  `exchange`, `instrument_type`, `price`, `dollar_volume`, `liquidity`, `trading_filter`,
  `evidence_stale`.
- The `ai` package lives in `trader/ai/`; the service entry point is `trader/ai_service.py`;
  its database is `ai.duckdb` on the named volume `mmr_ai_data`.

## Migrations and tests

- **No legacy data (owner, 2026-10-07):** the trader journal and `ai.duckdb` start from
  scratch. No ALTER, backfill or old-row compatibility steps. A change to an SP1 table
  edits that table's CREATE statement in place; a new table is one plain CREATE in the
  module's existing migration style. Price history in `mmr_db_data` is not touched.
- Trader journal migration numbers (version slots for new tables only): Plan 1 holds 90–94, Plan 2 95–99, Plan 3 100–104
  (SP1 uses 35–38, 54–56, 60–64, 70, 80–81). `ai.duckdb` has its own sequence starting at 1
  (Plan 4 holds 1–9, Plan 5 10–19, Plan 6 20–29).
- Per task: targeted pytest only. Full suite once, in each plan's last task:
  `.venv/bin/python -m pytest tests/ -q --timeout=60 --ignore=tests/test_ibrx_async.py`.

## Owner changes after the plans were written (2026-10-07)

- **Quotes (#74, PR #76).** Executable quote evidence comes from the trader's quote
  authority, not from IB alone. On a **paper** account with
  `automation.quote_fallback: alpaca_iex`, a quote may carry feed `iex_realtime`
  (Alpaca IEX) when IB has no live feed; the dispatch guard, `validate_approval` and
  `LiquidityPolicy` accept `{live, iex_realtime}` there and only `{live}` everywhere
  else (`trader/trading/quote_feeds.py`, `accepted_feeds(account_mode,
  quote_fallback)`). The plan bodies now follow this: Plan 3 Ruling 4 (the scope
  rule's price and evidence check uses the quote authority and the accepted set; a
  feed outside it is `evidence_stale`) and Ruling 18 (`get_ai_entry_quote`); Plan 6
  Ruling 3 (the feed is checked against the set in the trader's reply and kept in the
  evidence and its digest). PR #76 is a base dependency of Plans 2, 3 and 6.
- **Budget cap.** The owner's cap is `ai_paper.model_budget_usd_per_day` in
  `trader.yaml` (default 2000), as spec 5.4 says; only an operator edit of
  `trader.yaml` plus a trader restart changes it, no AI principal can. Plan 2 serves it
  (`get_ai_model_budget`), Plan 4 has no cap setting, Plan 5 applies it (Ruling 19).

## Rulings (spec section 14 open questions and gaps; the owner may change them)

- **Model ids:** no defaults in code. `config_defaults/ai.yaml` names example ids in
  comments only; a missing id fails loudly at startup. Credentials reach only the `ai`
  container, by env: `OPENROUTER_API_KEY`; Bedrock through the standard AWS chain
  (`AWS_REGION`, `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` or a profile);
  `AZURE_OPENAI_ENDPOINT` + `AZURE_OPENAI_API_KEY` + `AZURE_OPENAI_API_VERSION`.
- **Token and rate defaults:** `max_output_tokens: 4000`, `max_input_tokens: 60000` per
  call, `calls_per_hour: 120`, `max_in_flight: 2`, `decision_deadline_seconds: 60`.
- **Prices:** per-model USD per million input/output tokens in `ai.yaml`. A model with no
  price is refused (no reservation). Worst case = `max_input_tokens` × input price +
  `max_output_tokens` × output price.
- **Bedrock:** add `boto3` as a dependency (Converse API). OpenRouter and Azure use `httpx`.
- **Who simulates baselines:** the `ai` service sends the hypothetical decision (conid,
  side, reference price, stop, target, decided_at, deployment digest; quantity only for
  the matched-entry baseline). The **trader** sizes the follow-signal and fixed-rule
  records at ingestion (a linked follow-signal record takes the real ENTER's own size;
  otherwise SP1's entry sizing on a fresh broker snapshot, Plan 2 Ruling 19) and computes
  the simulated outcome at session end from 1-minute bars it can read (local DuckDB, else
  its Alpaca history provider). Missing bars, missing evidence (sent with
  `incomplete_reason`) or a size that cannot be computed → the record and its book are
  `incomplete`. Models never author fills or P&L.
- **Fixed rule (`fixed_rule.v1`):** per entry cycle, among the candidates that passed the
  scope rule, the one with the highest `change_pct` (ties: higher dollar volume, then
  symbol). Stop = reference × 0.98, target = reference × 1.04, quantity from the same SP1
  risk sizing a real ENTER would get, computed by the trader at ingestion. Values live in
  `ai.yaml`; changing them bumps the version.
- **Matched-entry, bracket-only exit:** the same entry the model closed, held with only its
  original stop and target until the session flatten (15:45 ET on a normal day). One record
  per model CLOSE / PARTIAL_CLOSE: opportunity = the close's decision id, the round trip id
  as linkage, reference = the entry fill, quantity = the shares that close asked to remove
  (the trader clips it so a trip's records never add up to more than its entry quantity).
- **No-trade:** zero P&L, one record per self-found opportunity, always complete.
- **Follow the signal:** the strategy's BUY taken at the reference price with the
  deployment's stop/target policy and SP1 sizing (by the trader), regardless of Jev's ruling.
