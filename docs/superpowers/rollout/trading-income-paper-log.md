# Trading income — paper evidence soak log

> **Operational only.** Software cannot mark `PAPER_PASSED`. Fill rows only after
> real paper sessions. Never commit account IDs, order IDs, or market-data payloads —
> record digests and pass/fail only.

## Prerequisites

- [ ] P3 automation stack running in **paper** mode
- [ ] Signed `PAPER_ELIGIBLE` artifact bundle mounted read-only
- [ ] One-strategy config only
- [ ] Pre/post session checklists wired (`session_open_check` / `session_close_check`)
- [ ] Fault drills green on disposable volumes (`automation_fault_drill.py`)

## Simultaneous floors (all required)

| Floor | Required | Observed | Met? |
|-------|----------|----------|------|
| Calendar days | ≥ 30 | | |
| Completed sessions | ≥ 20 | | |
| Round trips | ≥ 50 | | |
| Instruments | ≥ 5 | | |

## Daily session log (sanitized)

| Date (UTC) | Pre-check digest | Post-check digest | Replay seal | Flat OK | Incidents | Notes |
|------------|------------------|-------------------|-------------|---------|-----------|-------|
| | | | | | | |

## Gate adjudication

| Gate | Result | Evidence digest | Reviewer |
|------|--------|-----------------|----------|
| No divergence / duplicate / unresolved alert | | | |
| No missed flat / replay mismatch | | | |
| Stressed-cost / expectancy / concentration | | | |
| Paper evidence report (`paper_evidence_report.py`) | | | |

## Decision

- [ ] **HOLD** — floors or safety gates not met (extend stage; do not edit counters)
- [ ] **READY TO PREPARE CANARY** — prepare unsigned canary authority only; do **not** activate

Paper review signature (operator): ______________________ date: __________

Canary payload digest (unsigned): `________________________________`
