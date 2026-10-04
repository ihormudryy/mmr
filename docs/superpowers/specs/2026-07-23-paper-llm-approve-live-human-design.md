# Paper LLM Evaluate-then-Approve / Live Human-Only

**Date:** 2026-07-23  
**Status:** approved for implementation  
**Related:** hybrid paper-auto / live-propose (`2026-07-20-hybrid-paper-auto-live-propose-design.md`), signal→propose bridge (`2026-07-15-signal-propose-bridge-design.md`)

## Problem

Operators want the LLM to **evaluate** pending proposals on paper and then
`approve` or `reject`. On live, only a human (Command Center + preflight) may
approve. Today `approve_proposal` hard-codes `source="dashboard"` and has no
actor gate — live relies on preflight ceremony alone, which is not a principal
check.

## Decision

| Mode | Propose | Approve |
|------|---------|---------|
| Paper | LLM / strategies may create PENDING | LLM/SDK may approve **or** reject after checklist evaluation |
| Live | LLM / strategies may propose (existing authority gates) | **Human only** (`source=dashboard` + preflight); SDK/CLI/LLM approve refused |

This is **not** blind auto-approve. `auto_approve` stays false.

## Binding rules

### R1 — Actor stamp on approve

`ApproveProposalRequest.source` is optional; default `"dashboard"` for the web
gateway. SDK/CLI send `"sdk"` (or `"cli"` / `"llm"` when tagged).

### R2 — Live non-human refuse

If `account_mode == "live"` and `source ∈ {sdk, cli, llm}` → reject with
`LLM_LIVE_APPROVE_FORBIDDEN` (not retryable) before dispatch.

`source=dashboard` on live still requires preflight (unchanged).

### R3 — Paper allows non-human approve

Same sources are allowed on paper; existing risk/quote/revision guards still apply.

### R4 — Reject stays open

LLM may `reject` on paper and live (hygiene for PENDING). Only **approve** is
live-gated for non-humans.

### R5 — Distinct from paper automation

Paper LLM evaluate-approve uses `approve_proposal`. Unattended paper automation
uses `execute_automated_intent` for one signed strategy — a different path.

## Evaluation checklist (skill policy)

Before paper approve/reject the LLM must:

1. `proposals show N` — sizing, reasoning, brackets  
2. `portfolio-risk` / snapshot — concentration, capacity  
3. Quote / session sanity (or document after-hours caveat)  
4. Then `approve(N)` **or** `reject(N, reason=…)`

## Out of scope

- Blind auto-approve of all PENDING proposals  
- Changing IntentEmitter / paper automation Activate  
- Letting LLM mint live preflight nonces  
- `auto_execute: true`
