$ErrorActionPreference = "Stop"

$ASOF   = "2026-02-18"
$METRIC = "revenue_op"
$STRAT  = "B_growth_plus_quality"
$FEATV  = 2
$RETV   = 1
$TCOST  = 30
$OUTV_BASE = 3000
$MAX_PER_GROUP = 2

$KS = @(7, 10, 15)

Write-Host "[START] k robustness test"
Write-Host "[INFO] max_per_group=$MAX_PER_GROUP"

foreach ($K in $KS) {
    $OUTV = $OUTV_BASE + $K
    Write-Host ""
    Write-Host "=== RUN k=$K out_v=$OUTV ==="

    python .\scripts\backtest_quarterly_rebalance_v2.py `
      --asof $ASOF `
      --metric $METRIC `
      --feat_v $FEATV `
      --ret_v $RETV `
      --k $K `
      --strategy $STRAT `
      --out_v $OUTV `
      --tcost_bps $TCOST `
      --max_per_group $MAX_PER_GROUP `
      --save_holdings `
      --save_inputs

    if ($LASTEXITCODE -ne 0) {
        throw "backtest failed at k=$K"
    }
}

Write-Host ""
Write-Host "[DONE] all runs finished"

python .\scripts\ops\summarize_reports.py --asof $ASOF --metric $METRIC

Write-Host ""
Write-Host "[CHECK] summary file:"
Write-Host "data\processed\summary_reports__asof=$ASOF__metric=$METRIC.csv"
