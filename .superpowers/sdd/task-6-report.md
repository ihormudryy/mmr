# Task 6 Report — Research GETs + propose→reject

**Date:** 2026-07-23  
**Branch:** feat/command-center-foundation  
**Base:** `7cc0ed5`

## Delivered

- `tests/paper_e2e/test_dashboard_research.py` — presets, ideas/movers/snapshot/news soft-pass, unauth 401/403, resolve→`POST /api/commands/proposals`→reject with `e2e_id` tags.
- Product fixes discovered by the live run:
  - `ProposalRepository.reserve_id` skips ids already present in `trade_proposals` / `domain_event_journal` (sequence can lag after WAL quarantine; DuckDB has no `setval`).
  - Research entitlement → HTTP **403** (`RESEARCH_NOT_ENTITLED`); rate limit → HTTP **429** (`RESEARCH_RATE_LIMITED`) so e2e soft-pass matches design “not 5xx”.
  - E2E asserts `group` via top-level or `metadata.group` (wire stores group in metadata).

## Verification

```text
./scripts/paper_e2e.sh tests/paper_e2e/test_dashboard_research.py -v
7 passed
```

Unit:

```text
tests/test_proposal_command_service.py (incl. sequence-lag case)
tests/test_dashboard_research_service.py::test_entitlement_and_rate_limit_map_to_stable_codes
13 passed
```

## Notes

- First live run failed on `EventIdentityConflict: proposal:2:1` (seq behind max id) and research 502 rate-limits; both fixed before closeout.
- Left-over PENDING from the mid-fix attempt is cleaned by e2e teardown / reject path.
