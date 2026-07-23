# Task 6 Report — Verification + docs closeout

**Date:** 2026-07-20  
**Branch:** feat/command-center-foundation

## Research GETs + propose→reject (2026-07-23)

**Base:** `7cc0ed5032b1ef3886da463853018e0a99000ced`

Added `tests/paper_e2e/test_dashboard_research.py` with 180-second limits:

- Authenticated `presets`, `ideas`, `movers`, `snapshot`, and `news` reads.
  Success must be JSON data; a structured non-5xx entitlement response is
  accepted. HTML and all 5xx responses fail.
- A fresh client receives 401/403 for a research GET.
- Resolves AAPL through typed RPC, creates a tagged proposal only at
  `POST /api/commands/proposals`, and rejects it through the command API.
  Both `group` and `reasoning` include the run `e2e_id`.

### Paper-stack verification

`scripts/paper_e2e.sh tests/paper_e2e/test_dashboard_research.py`

Result: **3 passed, 4 failed**. The failures are intentional hard failures
against the currently running stack: `movers` returned
`502 RESEARCH_RATE_LIMITED`; `snapshot` and `news` returned 5xx research
errors; proposal creation returned `502 INTERNAL_ERROR`. No proposal was
created, so no cleanup action was required. The test suite correctly rejects
these server-side failures rather than treating them as entitlement soft-passes.

## Status

Phase 1 verification complete. Focused suite green; synthetic release gates exit 0. Spec status updated and committed.

## Focused test suite

```text
113 passed, 2 warnings in 5.07s
```

Files:
- `tests/automation/test_paper_materials.py`
- `tests/automation/test_paper_activation.py`
- `tests/test_command_stack.py`
- `tests/test_command_routes.py`

Exit code: **0**

Warnings (non-blocking): eventkit deprecation; Starlette TestClient httpx deprecation.

## Synthetic release gates

### P1 (`scripts/p1_release_gate.py --synthetic-only`)

- Exit code: **0** (synthetic-only; overall label FAILED due to pending manual soak)
- Elapsed: 3.156s
- Phases:
  - `command_plane_drill`: PASSED
  - `pytest:test_command_plane_activation`: PASSED
  - `docker_compose_config`: PASSED
  - `manual_ib_paper_soak`: PENDING

### P3 (`scripts/p3_release_gate.py --synthetic-only`)

- Exit code: **0** (synthetic-only; overall label FAILED due to pending manual soak)
- Elapsed: 130.71s
- Phases:
  - `p1_command_plane_drill`: PASSED
  - `p3_automation_drill`: PASSED
  - `pytest:test_command_plane_activation,test_automated_vertical_slice`: PASSED
  - `pytest:automation`: PASSED
  - `docker_compose_config`: PASSED
  - `manual_ib_paper_soak`: PENDING

## Docs

Updated `docs/superpowers/specs/2026-07-20-dashboard-paper-automation-activation-design.md`:

> **Status:** approved; Phase 1 implemented (restart-required activate/deactivate + Scaling UI). Phase 2 hot-arm is follow-up.

## Commits

- `docs(ops): Phase 1 dashboard paper automation Activate` — spec status line only

## Concerns / follow-ups

- Manual IB paper soak remains **PENDING** for both P1 and P3 (expected; run during XNYS RTH with live stack).
- Gate human-readable output still prints `FAILED` when manual soak is pending even though `--synthetic-only` exits 0 — by design per `release_gate_common.is_synthetic_ok`.
- Phase 2 hot-arm (in-process commit/verify/persist, chaos tests) not in scope; documented as follow-up in spec.
