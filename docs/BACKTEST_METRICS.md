# Backtester: expectancy, profit factor and return

This describes the implementation in
[`trader/simulation/backtester.py`](../trader/simulation/backtester.py), not a
new strategy evaluation. The deterministic examples are exercised through real
`Backtester.run` calls in
[`tests/test_backtester_metric_semantics.py`](../tests/test_backtester_metric_semantics.py),
using temporary DuckDB bars, `next_open` fills and zero slippage. They do not
validate the historical strategy results or deployed state in
[`OPERATIONAL_STATE.md`](OPERATIONAL_STATE.md).

## Observation and cost basis

For each executed SELL closing quantity `q` of a long position:

```text
entry_price = quantity-weighted average fill price of the remaining position
entry_fee_per_share = allocated average BUY commission per remaining share
net_pnl = (sell_fill_price - entry_price) * q
          - sell_commission - entry_fee_per_share * q
entry_notional = entry_price * q
```

Additional BUYs update the quantity-weighted entry basis. Partial SELLs reduce
held quantity without changing the remaining per-share basis; closing the entire
position resets it. Each SELL is one observation, including partial exits. It is
not necessarily a complete flat-to-flat position cycle. `total_trades`, by
contrast, counts executed BUY and SELL fills.

Both metrics below use **net** P&L after allocated entry and exit commissions.
Slippage is already embedded in the fill prices. The expectancy denominator is
entry fill notional, **excluding** commissions, not initial account capital or
the full original position size when only part is sold.

## Different weighting, not an automatic contradiction

```text
profit_factor = sum(positive net_pnl) / abs(sum(negative net_pnl))
expectancy_bps = mean(net_pnl / entry_notional across SELLs) * 10,000
```

PF aggregates cash gains and losses. Expectancy equally weights each SELL's
return on its closed entry notional. Unequal notionals can therefore give
opposite profitability indications in either direction. A fixed share count is
not a fixed notional when entry prices vary. Fixed intended dollar sizing also
need not mean identical executed notionals after share rounding or partial exits.

For example, with commission of $0.01/share on each side:

| Round trip | Entry notional | Net P&L | Net return on entry notional |
|---|---:|---:|---:|
| Buy 100 @ $100, sell 100 @ $102 | $10,000 | +$198.00 | +198 bps |
| Buy 10 @ $100, sell 10 @ $95 | $1,000 | −$50.20 | −502 bps |

The closed cash P&L is **+$147.80**, PF is **198 / 50.2 ≈ 3.9442**, but
expectancy is **(198 − 502) / 2 = −152 bps**. This is a valid result, not evidence
of an expectancy calculation defect.

If every SELL has exactly the same positive entry notional, expectancy has the
same sign as summed closed net P&L. With at least one net loss, PF > 1 then implies
positive expectancy, PF = 1 implies zero expectancy, and PF < 1 implies negative
expectancy. These are accounting relationships, not evidence of statistical
significance or future profitability.

## Partial exits and commissions

Buying 100 @ $100, then selling 90 @ $102 and 10 @ $95, with the same fees, gives:

- Net cash P&Ls +$178.20 and −$50.20, with allocated entry fees $0.90 and $0.10.
- PF ≈ 3.5498 and total closed cash profit $128.00.
- Expectancy −152 bps: the small losing exit has the same observation weight as
  the large winning exit.

Splitting that 90-share winning exit into nine 10-share SELLs at the same price
leaves cash P&L, proportional commissions and PF unchanged, but changes expectancy
to **+128 bps**. This metric is sensitive to exit segmentation by definition; do
not interpret it as a quantity-weighted portfolio return or mean position-cycle
return. Compare like-for-like exit policies when using it to compare strategies.

A positive price move need not be a net winner. Buying 10 @ $100 and selling
10 @ $100.05 earns $0.50 before costs. With $0.03/share commissions on each side,
net P&L is −$0.10, expectancy is −1 bp, and PF is zero (no net winning SELL).
Without commissions the same fills give +5 bps and infinite PF.

## Portfolio return and boundary conventions

`total_return = final_equity / initial_capital - 1`, where final equity is cash
plus remaining positions marked at their latest closes. It includes unrealized
P&L and entry fees on still-open positions; no hypothetical exit commission is
charged for an unclosed position. PF and expectancy include only closed SELL
quantities. Even with equal closed entry notionals, an open position can therefore
make total return disagree with the closed-trade metrics.

Implementation conventions:

- No closed SELLs: PF and expectancy are both zero, even if marked equity changes.
- Positive net SELL P&L with no net losses: PF is positive infinity.
- No positive or negative net SELL P&L: PF is zero (including all-breakeven SELLs).
- Losses but no net wins: PF is zero.

## Investigation outcome and verification

No expectancy/PF calculation defect was reproduced in these deterministic cases;
production metric code was left unchanged. Coverage includes both directions of
sign disagreement, fixed shares versus fixed notional, both commission sides,
partial exits, exit segmentation, weighted entries/additions after partial sales,
basis reset after going flat, and unrealized-versus-realized returns.

Run the characterization and existing backtester/metric tests locally:

```bash
OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 .venv/bin/python -m pytest \
  tests/test_backtester_metric_semantics.py tests/test_backtester.py \
  tests/test_backtest_metrics.py -q
```

The original historical runs were not rerun. Explaining a particular historical
disagreement still requires its actual fill trace, entry notionals, commissions,
partial-exit grouping and remaining open positions. Neither return/PF nor
expectancy should be declared universally more reliable merely because their
signs differ.
