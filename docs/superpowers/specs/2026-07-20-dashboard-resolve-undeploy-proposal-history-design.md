# Dashboard M2-lite: resolve, undeploy, proposal history

**Date:** 2026-07-20  
**Status:** implemented (approach A)  
**Scope:** `/cc` command center only.

## Decision

Thin HTTP on existing typed/manage surfaces (not new command-center command types).

### 1. Symbol → conId in New proposal

- `GET /api/resolve?symbol=&exchange=&currency=&sec_type=` (session-gated)
- Calls `discover_instrument` via manage client
- Returns instruments list (conId, symbol, exchange, currency, …)
- New proposal drawer: symbol + optional exchange/currency, **Resolve** fills confirmed conId; create still submits conId only

### 2. Undeploy on Deploy tab

- `POST /strategies/{name}/undeploy` (session + CSRF)
- Remove entry from `strategy_runtime.yaml`, call `reload_strategies`
- Confirm dialog on each deployed row; mirror deploy flash → `#deploy`

### 3. Proposal history + detail

- Action queue: status filter (Pending / All / terminal statuses)
- Detail drawer from `get_proposal` (or snapshot row + `GET /api/proposals/{id}`)
- Show sizing_result, source, status, order refs when present

## Out of scope

Limit/bracket propose, portfolio-risk UI, groups, charts, multi-strategy automation.

## Tests

- Resolve API happy/empty/bad input
- Undeploy removes YAML + reload call
- History filter / detail endpoint or render smoke
