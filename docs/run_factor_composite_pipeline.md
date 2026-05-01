# Run Factor Composite Pipeline

## Purpose

This guide explains how to run the `factor_composite` pipeline end to end for live rebalance preparation.

The goal is reproducibility:

- collect or reuse the required market and fundamentals data
- build the downstream feature stack
- score the full universe with `factor_composite`
- generate live actions, an execution plan, a performance section, and a rebalance report

## Pipeline Steps

1. Environment check
2. `prepare_asof.py`
3. `build_features_phase1.py`
4. `build_factors_ttm_acc2.py`
5. `make_features_live.py`
6. `score_latest_rebalance.py --strategy factor_composite`
7. `generate_live_actions.py`
8. `make_execution_plan.py`
9. `calc_live_performance.py`
10. `generate_rebalance_report.py`
11. `pipeline_manifest.json` and `pipeline_summary.md`

## Preconditions

- Python `3.12`
- Windows PowerShell
- local virtual environment at `.venv`
- `DART_API_KEY` available in the shell session

Setup:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:DART_API_KEY="YOUR_DART_KEY"
```

## Required Inputs

- current holdings CSV with at least:
  - `ticker`
  - `name`
  - `shares`
- strategy configuration from `configs/strategies.yaml`
- execution configuration from `configs/execution.yaml` or equivalent

## Main Wrapper

Use:

- `scripts/live/run_factor_composite_pipeline.ps1`

The wrapper writes logs to `artifacts/logs` and a step manifest to:

- `artifacts/pipeline_runs/asof={ASOF}/pipeline_manifest.json`
- `artifacts/pipeline_runs/asof={ASOF}/pipeline_summary.md`

## Full Example

```powershell
cd .
.\.venv\Scripts\Activate.ps1

$ASOF="2026-07-15"
$TARGET="2026-08-15"
$METRIC="revenue_op"
$STRAT="factor_composite"
$FACTOR_V="3291"
$FEAT_V="3291"
$ACTION_V="9201"
$EXEC_V="9201"
$REPORT_V="9201"
$TOTAL_VALUE="100000000"
$HOLDINGS_CSV=".\data\portfolio\current\20260330_holdings_clean.csv"
$PREV_REBAL_DATE="2026-03-31"

$logDir = "artifacts\logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$ts = Get-Date -Format "yyyyMMdd_HHmmss"
$logPath = Join-Path $logDir ("factor_composite_pipeline__asof=$ASOF__$ts.log")

.\scripts\live\run_factor_composite_pipeline.ps1 `
  -ASOF $ASOF `
  -TARGET $TARGET `
  -METRIC $METRIC `
  -STRAT $STRAT `
  -FACTOR_V $FACTOR_V `
  -FEAT_V $FEAT_V `
  -ACTION_V $ACTION_V `
  -EXEC_V $EXEC_V `
  -REPORT_V $REPORT_V `
  -TOTAL_VALUE $TOTAL_VALUE `
  -HOLDINGS_CSV $HOLDINGS_CSV `
  -PerfHoldingsCsv $HOLDINGS_CSV `
  -PrevRebalDate $PREV_REBAL_DATE *> $logPath

Write-Output "LOG_PATH=$logPath"
Write-Output "EXIT_CODE=$LASTEXITCODE"
```

## Skip-Prepare Smoke Mode

If data for the same `asof` is already prepared, you can validate the live side only:

```powershell
.\scripts\live\run_factor_composite_pipeline.ps1 `
  -ASOF "2026-04-15" `
  -TARGET "2026-03-31" `
  -METRIC "revenue_op" `
  -STRAT "factor_composite" `
  -FACTOR_V 3291 `
  -FEAT_V 3291 `
  -ACTION_V 9201 `
  -EXEC_V 9201 `
  -REPORT_V 9201 `
  -TOTAL_VALUE 100000000 `
  -HOLDINGS_CSV ".\dist\ai_inv_adv_github_min\data\portfolio\current\20260330_holdings_clean.csv" `
  -PerfHoldingsCsv ".\dist\ai_inv_adv_github_min\data\portfolio\current\20260330_holdings_clean.csv" `
  -PrevRebalDate "2026-03-31" `
  -SkipPrepare
```

## Expected Outputs

- `data/features/features_live/features_live__asof={ASOF}__metric={METRIC}__v={FEAT_V}.parquet`
- `data/live/scores/latest_scores__asof={ASOF}__metric={METRIC}__strat=factor_composite__featv={FEAT_V}__target={TARGET}__full_universe.csv`
- `data/live/actions/live_actions__asof={ASOF}__metric={METRIC}__strat=factor_composite__target={TARGET}__v={ACTION_V}.csv`
- `data/processed/execution_plan__total={TOTAL_VALUE}__v={EXEC_V}.csv`
- `data/live/performance/performance_summary__asof={ASOF}__target={TARGET}.json`
- `data/live/performance/performance_daily__asof={ASOF}__target={TARGET}.csv`
- `data/live/performance/performance_contrib__asof={ASOF}__target={TARGET}.csv`
- `data/live/reports/rebalance_report__asof={ASOF}__target={TARGET}__metric={METRIC}__strat=factor_composite__v={REPORT_V}.md`
- `data/live/reports/rebalance_report__asof={ASOF}__target={TARGET}__metric={METRIC}__strat=factor_composite__v={REPORT_V}.html`
- `data/live/reports/rebalance_report_detail__asof={ASOF}__target={TARGET}__metric={METRIC}__strat=factor_composite__v={REPORT_V}.csv`

## Provisional Interpretation

If the run falls back to a seeded `krx_master`, treat the result as provisional.

Typical markers:

- `provisional=true`
- `provisional_source=fallback_seed_from_2026-03-31`

When that happens:

- keep the result for auditability
- do not treat it as final reference output
- rerun the same command after KRX live collection is restored

## Troubleshooting Order

1. verify `.venv\Scripts\python.exe`
2. verify `DART_API_KEY`
3. inspect the latest log under `artifacts/logs`
4. inspect `krx_master` metadata for provisional fallback
5. inspect `features_live` metadata and score coverage JSON
6. inspect `pipeline_manifest.json` step exit codes
