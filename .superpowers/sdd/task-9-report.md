# Task 9 Report: Docs runbook + HMAC key-file fix

## Completed

- Replaced stale `MMR_HMAC_SECRET` references in the paper automation guide and
  root project guidance with the `service_hmac.key` / `MMR_SERVICE_HMAC_KEY_FILE`
  typed-RPC mechanism.
- Added the requested Paper Docker e2e runbook, including normal, live-order,
  and restart invocations plus the stack-down skip behavior.

## Validation

- Confirmed no `MMR_HMAC_SECRET` references remain in
  `docs/PAPER_AUTOMATION_SETUP.md` or `CLAUDE.md`.
