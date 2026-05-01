# Live Performance Tracking

## Purpose

This document describes how to track the actual live portfolio after execution and how to connect that history to the next rebalance.

## What To Record

For each live cycle, record:

- rebalance date
- asof date
- target date
- actual holdings after execution
- benchmark level / return
- turnover
- slippage
- realized trading cost
- portfolio drift before the next rebalance
- KPI snapshot

## Actual Portfolio Return Recording

Track performance using actual post-trade holdings, not model-only target weights.

Recommended outputs:

- daily NAV series
- cumulative NAV
- period return
- benchmark-relative active return
- KPI snapshot csv

Snapshot location:

- `data/live/kpi_snapshots/kpi_snapshot__asof=YYYY-MM-DD__target=YYYY-MM-DD.csv`

## Benchmark Comparison

At minimum, record:

- portfolio return
- benchmark return
- active return = portfolio return - benchmark return

Use a consistent benchmark definition across quarters.

## Turnover Tracking

Each rebalance should record:

- target turnover
- realized turnover
- names added
- names removed
- names kept

This helps explain differences between model return and actual live return.

Turnover formula used in the live wrapper snapshot:

- `turnover = sum(abs(trade_value)) / (2 * total_capital)`

## Slippage / Trading Cost Tracking

Record separately:

- broker fee / tax if applicable
- estimated model transaction cost
- realized execution slippage

This should be compared against the backtest `tcost_bps` assumption.

## Portfolio Drift Tracking

Before the next rebalance, record how much the live portfolio drifted from:

- original target weights
- sector / industry mix
- concentration limits

Useful drift measures:

- weight drift by name
- top holding weight drift
- industry allocation drift

## previous holdings Connection

The next rebalance should use the latest actual holdings snapshot as the `previous holdings` input.

Practical flow:

1. save clean executed holdings
2. normalize to `ticker,name,shares`
3. feed the cleaned holdings file into the next live pipeline
4. use the previous rebalance date consistently

This connection matters because:

- holding bonus depends on continuity
- turnover depends on prior holdings
- real execution history should drive the next cycle, not old target files

If the wrapper is run without `HOLDINGS_CSV`, it auto-selects the latest eligible `*holdings_clean.csv` using:

1. `data/portfolio/current`
2. `data/portfolio/history`
3. `dist/ai_inv_adv_github_min/data/portfolio/current`

## Recommended Minimal File Set Per Cycle

Keep a minimal live archive for each rebalance:

- actual holdings snapshot
- execution summary
- performance daily file
- contribution file
- benchmark comparison note

These may live outside git if they are generated operational files.

## KPI Snapshot Columns

The current live wrapper stores the following snapshot columns:

- `nav`
- `cum_return`
- `benchmark_return`
- `active_return`
- `turnover`
- `topk_keep_rate`
- `provisional`
- `provisional_source`

## topk_keep_rate Formula

- `topk_keep_rate = |current_holdings ∩ target_topk| / K`

This measures how much of the model target basket is already retained in the actual starting portfolio.

## KPI Alert Thresholds

Suggested first-pass alert thresholds:

- `live_cagr - backtest_cagr_common < -0.05` for two consecutive quarters
- `tracking_error` above expected backtest dispersion band
- `turnover_ratio = realized_turnover / model_turnover > 1.25`
- `slippage_dev_bps = realized_slippage_bps - assumed_tcost_bps > +20bp`
- `topk_keep_rate < 0.80`

If benchmark generation falls back from live fetch to local data, the resulting KPI snapshot should remain provisional.
