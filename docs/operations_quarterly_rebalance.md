# Quarterly Rebalance Operations

## Purpose

This document defines the practical quarterly rebalance loop for the current main live strategy:

- main strategy: `factor_composite`
- AI strategies: `experimental`

## Full Quarterly Loop

1. `prepare_asof`
2. feature stack build
3. `run_strategy_compare`
4. `factor_composite` live scoring pipeline
5. live action generation
6. rebalance report generation
7. actual execution
8. live performance tracking

## Step 1. prepare_asof

Representative command:

```powershell
.\.venv\Scripts\python.exe .\scripts\data_pipeline\prepare_asof.py `
  --asof 2026-04-15 `
  --metric revenue_op `
  --universe_v 1 `
  --mcap_top 800 `
  --trd_bot 0.1 `
  --px_v 1 `
  --ret_v 1 `
  --lookback_years 15 `
  --start 20110101 `
  --fund_v 1 `
  --factor_in_v 1 `
  --factor_out_v 3291 `
  --fs_div CFS `
  --with_shares_industry `
  --shares_out_v 9201 `
  --fund_mode union
```

Output expectation:

- `krx_master`
- `krx_marketdata`
- `prices_daily`
- `returns_monthly`
- fundamentals

## Step 2. Feature Stack

Representative commands:

```powershell
.\.venv\Scripts\python.exe .\scripts\data_pipeline\build_features_phase1.py --asof 2026-04-15
.\.venv\Scripts\python.exe .\scripts\data_pipeline\build_factors_ttm_acc2.py --asof 2026-04-15 --metric revenue_op --in_v 1 --out_v 3291
.\.venv\Scripts\python.exe .\scripts\data_pipeline\make_features_live.py --asof 2026-04-15 --metric revenue_op --in_v 3291 --out_v 3291 --save_meta
```

## Step 3. Strategy Comparison

Representative command:

```powershell
.\.venv\Scripts\python.exe .\scripts\backtest\run_strategy_compare.py `
  --asof 2026-04-15 `
  --metric revenue_op `
  --strategy D_quality_filter_debt_profitaccel_liq `
  --k 10 `
  --feat_v 3291 `
  --ret_v 1 `
  --factor_v 3291 `
  --raw_px_v 1 `
  --model ridge `
  --feature_profile compact_dailyagg `
  --min_train_rows 20 `
  --valid_rows 4 `
  --temperature 1.0 `
  --cap_profit_accel_delta -1.0 `
  --ai_overlay_strength 1.0 `
  --mcap_top 800 `
  --trd_bot 0.1 `
  --start 20110101 `
  --lookback_years 15 `
  --out_dir artifacts\strategy_compare\asof=2026-04-15
```

If data and feature stack are already ready, reuse them:

```powershell
.\.venv\Scripts\python.exe .\scripts\backtest\run_strategy_compare.py --skip_prepare ...
```

## Step 4. factor_composite Live Pipeline

Representative wrapper:

```powershell
.\scripts\live\run_factor_composite_pipeline.ps1 `
  -ASOF 2026-07-15 `
  -TARGET 2026-08-15 `
  -METRIC revenue_op `
  -STRAT factor_composite `
  -FACTOR_V 3291 `
  -FEAT_V 3291 `
  -ACTION_V 9201 `
  -EXEC_V 9201 `
  -REPORT_V 9201 `
  -TOTAL_VALUE 100000000 `
  -HOLDINGS_CSV .\dist\ai_inv_adv_github_min\data\portfolio\current\20260330_holdings_clean.csv `
  -PerfHoldingsCsv .\dist\ai_inv_adv_github_min\data\portfolio\current\20260330_holdings_clean.csv `
  -PrevRebalDate 2026-03-31
```

Minimum rerun command after upstream data is already ready:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\live\run_factor_composite_pipeline.ps1 -ASOF YYYY-MM-DD -TARGET YYYY-MM-DD -SkipPrepare
```

Operational note:

- as of the validated dry-run, the next target rebalance month after `2026-03-31` is `2026-05-31`
- this project uses the report-lag rebalance convention, not plain quarter-end
- when `HOLDINGS_CSV` is omitted, the wrapper auto-selects the latest eligible `*holdings_clean.csv`

## Step 5. Live Action Generation

Expected live outputs:

- `data/live/scores/...`
- `data/live/actions/...`
- `data/processed/execution_plan...`

The action layer should be reviewed before trading:

- BUY
- SELL
- HOLD
- REVIEW

## Step 6. Report Generation

Expected report outputs:

- markdown report
- html report
- detail csv
- KPI snapshot csv

The report should confirm:

- strategy = `factor_composite`
- data version
- provisional status
- fallback source if any

KPI snapshot output:

- `data/live/kpi_snapshots/kpi_snapshot__asof=YYYY-MM-DD__target=YYYY-MM-DD.csv`

## Step 7. Actual Execution

Execution is outside git and outside generated report artifacts.

Minimum checklist:

- verify target date
- verify current holdings input
- verify total capital
- verify execution plan lot sizes
- verify restricted or illiquid names
- record actual fills and slippage

## Step 8. Performance Tracking

After execution:

- update actual holdings record
- track benchmark-relative return
- record turnover and cost
- record drift before next rebalance

See also:

- [live_performance_tracking.md](live_performance_tracking.md)

## Failure Triage Order

When the pipeline fails, check in this order:

1. environment / venv / dependency issue
2. `DART_API_KEY` presence
3. KRX / DART network access
4. `prepare_asof` outputs existence
5. `features_live` existence
6. `run_strategy_compare` output integrity
7. holdings input path and schema
8. report generation dependencies

## When `--skip_prepare` Is Allowed

Use `--skip_prepare` only when all required upstream artifacts already exist and are trusted:

- `krx_master`
- `krx_marketdata`
- `prices_daily`
- `returns_monthly`
- fundamentals
- `features_live`
- AI raw prices needed for walk-forward scoring

Do **not** use `--skip_prepare` if:

- collection status is unknown
- upstream data was partially regenerated
- asof / version alignment is unclear

For practical dry-runs, `--skip_prepare` is the intended mode after:

1. `prepare_asof`
2. feature stack build
3. strategy comparison

have already completed successfully for the same `asof`.

## Provisional / Fallback Handling

If fallback or provisional status appears:

- keep `provisional=true`
- keep the fallback source in metadata and report text
- keep benchmark fallback metadata as well when live benchmark fetch fails
- do not treat the result as final production truth
- rerun after primary `KRX / DART` recovery

## Holdings Auto-Selection Priority

When `HOLDINGS_CSV` is not explicitly passed, the live wrapper searches in this order:

1. `data/portfolio/current`
2. `data/portfolio/history`
3. `dist/ai_inv_adv_github_min/data/portfolio/current`

Within those locations it picks the latest `*holdings_clean.csv` whose embedded date is less than or equal to the inferred previous rebalance date.

## PrevRebalDate Auto-Inference

When `PrevRebalDate` is not explicitly passed, the wrapper infers it from `TARGET`:

- `YYYY-03-31 -> previous YYYY-11-30`
- `YYYY-05-31 -> previous YYYY-03-31`
- `YYYY-08-31 -> previous YYYY-05-31`
- `YYYY-11-30 -> previous YYYY-08-31`

This lets the same wrapper be rerun next quarter by changing only `ASOF` and `TARGET`.
