# Reproduce 2026-04-15

This document records the successful procedure used around the `2026-04-15` archive state.

The result set was provisional because `krx_master` used:

- `provisional=true`
- `fallback_seed_from_2026-03-31`

Therefore this document describes the procedure, not a final canonical result artifact to commit.

## Scope

- asof: `2026-04-15`
- metric: `revenue_op`
- feature version: `3291`
- live strategy candidate: `factor_composite`
- comparison strategies:
  - `quant_only`
  - `factor_composite`
  - `ai_wf_topk`
  - `combo_overlay_replace`

## 1. Environment

```powershell
cd .
.\.venv\Scripts\Activate.ps1
$env:DART_API_KEY="YOUR_DART_KEY"
```

## 2. Fundamentals and Feature Stack

The downstream feature stack was rebuilt with:

```powershell
.\.venv\Scripts\python.exe .\scripts\data_pipeline\build_features_phase1.py --asof 2026-04-15
.\.venv\Scripts\python.exe .\scripts\data_pipeline\build_factors_ttm_acc2.py --asof 2026-04-15 --metric revenue_op --in_v 1 --out_v 3291
.\.venv\Scripts\python.exe .\scripts\data_pipeline\make_features_live.py --asof 2026-04-15 --metric revenue_op --in_v 3291 --out_v 3291 --save_meta
```

Expected outputs:

- `data/features/features_phase1__asof=2026-04-15__src=phase1__v=1.parquet`
- `data/processed/factors_ttm_acc2__asof=2026-04-15__metric=revenue_op__v=3291.parquet`
- `data/features/features_live/features_live__asof=2026-04-15__metric=revenue_op__v=3291.parquet`

## 3. 4-Strategy Comparison

Because the feature stack already existed, the successful run reused it and skipped internal preparation:

```powershell
.\.venv\Scripts\python.exe .\scripts\backtest\run_strategy_compare.py `
  --skip_prepare `
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

Expected outputs:

- `artifacts/strategy_compare/asof=2026-04-15/strategy_comparison.csv`
- `artifacts/strategy_compare/asof=2026-04-15/best_strategy.json`

Observed selection:

- `factor_composite`

Observed provisional note:

- `fallback_seed_from_2026-03-31`

## 4. Factor Composite Live Smoke

The live-side wrapper was validated in skip-prepare mode:

```powershell
.\scripts\live\run_factor_composite_pipeline.ps1 `
  -ASOF 2026-04-15 `
  -TARGET 2026-03-31 `
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
  -PrevRebalDate 2026-03-31 `
  -SkipPrepare
```

Expected outputs:

- `data/live/scores/latest_scores__asof=2026-04-15__metric=revenue_op__strat=factor_composite__featv=3291__target=2026-03-31__full_universe.csv`
- `data/live/actions/live_actions__asof=2026-04-15__metric=revenue_op__strat=factor_composite__target=2026-03-31__v=9201.csv`
- `data/live/performance/performance_summary__asof=2026-04-15__target=2026-03-31.json`
- `data/live/reports/rebalance_report__asof=2026-04-15__target=2026-03-31__metric=revenue_op__strat=factor_composite__v=9201.md`
- `artifacts/pipeline_runs/asof=2026-04-15/pipeline_manifest.json`

## 5. Important Warning

Do not treat the provisional `2026-04-15` result files as the permanent canonical output to publish.

Instead:

- publish the code and documentation
- publish the reproduction procedure
- rerun the same commands after KRX live collection is restored
- compare provisional versus refreshed results before declaring a final canonical archive
