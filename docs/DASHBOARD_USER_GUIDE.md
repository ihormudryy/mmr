# Dashboard user guide

Operator-facing guide for **http://127.0.0.1:7424/cc**.

The interactive version (with hover **i** info bubbles, same pattern as Strategies /
Portfolios) lives on the **Guide** tab and is authored in
[`web/templates/_guide_tab.html`](../web/templates/_guide_tab.html).

This markdown file is a readable copy for the repo; prefer the Guide tab in the UI.

## Quick pointers

| Topic | Where |
|-------|--------|
| Paper vs live, vocabulary, safety | Guide tab → beginners section |
| Propose / approve / close / pause | Guide tab → Trading examples A–D |
| Strategies + portfolios | Guide tab → Strategies / Portfolios examples |
| Allocation activate / suspend | Guide tab → Scaling |
| Unattended paper automation (Activate / bootstrap) | [`PAPER_AUTOMATION_SETUP.md`](PAPER_AUTOMATION_SETUP.md) |

## CLI companions

```bash
mmr resolve AAPL
mmr --json portfolio-snapshot
mmr research allocation prepare …
mmr research allocation sign …
mmr activate-allocation …
mmr suspend-allocation --reason "…"
mmr strategies undeploy NAME
```
