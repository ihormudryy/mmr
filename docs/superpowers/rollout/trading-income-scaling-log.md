# Trading income scaling rollout log

| Date | Stage | Action | Authority digest (sanitized) | Operator |
|------|-------|--------|------------------------------|----------|
| 2026-07-20 | P5 software | Scaling gate, degradation, capacity, portfolio admission, risk budget, dashboard Scaling tab, fault drill shipped in CI | n/a (synthetic tests only) | automation |

## Upstream evidence (must exist before Scale 1)

- Paper soak: [`trading-income-paper-log.md`](trading-income-paper-log.md)
- Live canary soak: [`trading-income-canary-log.md`](trading-income-canary-log.md)

Real allocation increases require signed offline authority, flat/reconciled broker
state, and accumulated live evidence — not this log entry alone.
