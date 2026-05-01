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

## Actual Portfolio Return Recording

Track performance using actual post-trade holdings, not model-only target weights.

Recommended outputs:

- daily NAV series
- cumulative NAV
- period return
- benchmark-relative active return

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

## Recommended Minimal File Set Per Cycle

Keep a minimal live archive for each rebalance:

- actual holdings snapshot
- execution summary
- performance daily file
- contribution file
- benchmark comparison note

These may live outside git if they are generated operational files.
