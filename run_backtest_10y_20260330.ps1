param(
    [string]$ASOF = "2026-03-29",
    [string]$METRIC = "revenue_op",
    [int]$FEATV = 3291,
    [int]$RETV = 1,
    [string]$RETSRC = "pykrx",
    [string]$STRAT = "D_quality_filter_debt_profitaccel_liq",
    [int]$K = 10,
    [int]$OUTV = 9030,
    [double]$TCOST = 30,
    [int]$MAX_PER_GROUP = 0,
    [double]$ENTRY_GAP = 0.0
)

$ErrorActionPreference = "Stop"

Write-Host "=== RESTORE BACKTEST FILES ==="
powershell -ExecutionPolicy Bypass -File .\restore_backtest_from_archive_20260330.ps1

Write-Host "=== RUN 10Y BACKTEST ==="
python .\scripts\backtest\run_rolling_backtest.py `
  --asof $ASOF `
  --metric $METRIC `
  --feat_v $FEATV `
  --ret_v $RETV `
  --ret_src $RETSRC `
  --strategy $STRAT `
  --k $K `
  --out_v $OUTV `
  --tcost_bps $TCOST `
  --max_per_group $MAX_PER_GROUP `
  --entry_gap $ENTRY_GAP `
  --filter_fallback error `
  --save_holdings `
  --save_inputs
