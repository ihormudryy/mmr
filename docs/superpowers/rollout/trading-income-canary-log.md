# Trading income — live canary soak log

> **Operational only.** Software cannot mark `CANARY_PASSED`. Fill after real live
> sessions under a signed 6% canary authority. Commit digests and decisions only.

## Prerequisites

- [ ] Paper soak signed off (`trading-income-paper-log.md`)
- [ ] Production permissions, market data, backups, emergency contact verified
- [ ] Signed canary attestation activated via `mmr activate-canary` (operator source)
- [ ] Limits: ≤6% gross, ≤3 positions, ≤5% per position, ≤0.20% trade risk, ≤0.50% daily loss
- [ ] Start at minimum order size

## Simultaneous floors (all required)

| Floor | Required | Observed | Met? |
|-------|----------|----------|------|
| Live sessions | ≥ 30 | | |
| Round trips | ≥ 75 | | |
| Instruments | ≥ 5 | | |
| Capital-safety incidents | = 0 | | |

## Session log (sanitized)

| Date (UTC) | Pre/post digests | Replay | Flat | Drawdown % | After-cost expectancy | Incidents |
|------------|------------------|--------|------|------------|------------------------|-----------|
| | | | | | | |

## Canary gate adjudication

| Gate | Result | Evidence digest | Reviewer |
|------|--------|-----------------|----------|
| Zero capital-safety incidents | | | |
| Complete resolution / flat / replay | | | |
| Positive after-cost expectancy | | | |
| Prediction envelope + cost limits | | | |
| Drawdown &lt; 3% | | | |
| Concentration / anti-luck gates | | | |
| Independent historical/paper/live deviation review | | | |

## Decision

- [ ] **SUSPEND / COLLECT** — economic or safety failure (never loosen risk)
- [ ] **CANARY_PASSED** — prepare Scale 1 allocation authority (do not auto-activate)

Authority digests (sanitized):

- Canary activation: `________________________________`
- Independent review: `________________________________`

Operator signatures: ______________________ / ______________________ date: __________
