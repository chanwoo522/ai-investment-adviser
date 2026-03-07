param(
  [Parameter(Mandatory=$true)][string]$ASOF,
  [string]$Metric = "revenue_op",
  [int]$DataASOF = "2026-02-18",
  [int]$FeatInV = 2,
  [int]$FeatOutV = 2,
  [int]$PxV = 1,
  [int]$RetV = 1,
  [int]$OutV = 100,
  [int]$K = 20,
  [string]$Strategy = "B_growth_plus_quality",
  [double]$HoldBonus = 0.30,
  [double]$EntryGap = 0.05,
  [int]$TCostBps = 30
)

$ErrorActionPreference = "Stop"

Write-Host "[INFO] ASOF=$ASOF Metric=$Metric OutV=$OutV K=$K Strat=$Strategy TCostBps=$TCostBps"

# 0) (선택) raw 정리/백업은 월별에도 가능하지만, 보통 분기 리밸런싱 때만 해도 됨.
# powershell -ExecutionPolicy Bypass -File .\scripts\ops\step1_archive_and_prune_raw.ps1 -ASOF $ASOF

# --- ensure static artifacts exist for ASOF by copying from DataASOF ---
$proc = ".\data\processed"

$srcUni = Join-Path $proc ("universe__asof={0}__metric={1}__v=1.csv" -f $DataASOF,$Metric)
$dstUni = Join-Path $proc ("universe__asof={0}__metric={1}__v=1.csv" -f $ASOF,$Metric)

$srcFac = Join-Path $proc ("factors_ttm_acc2__asof={0}__metric={1}__v=2.parquet" -f $DataASOF,$Metric)
$dstFac = Join-Path $proc ("factors_ttm_acc2__asof={0}__metric={1}__v=2.parquet" -f $ASOF,$Metric)

if (!(Test-Path $dstUni)) { Copy-Item $srcUni $dstUni -Force; Write-Host "[OK] copied universe -> $dstUni" }
if (!(Test-Path $dstFac)) { Copy-Item $srcFac $dstFac -Force; Write-Host "[OK] copied factors  -> $dstFac" }

# 1) features_live 생성(이미 factors 만들어져있다는 전제)
python .\scripts\make_features_live.py --asof $ASOF --metric $Metric --in_v $FeatInV --out_v $FeatOutV

# 2) 가격 수집/저장 (이미 make_prices_daily.py가 있는 전제)
# start는 고정해도 되고, 최초 실행 이후엔 더 똑똑하게 증분수집으로 바꿔도 됨.
python .\scripts\make_prices_daily.py --asof $ASOF --metric $Metric --start 20160101 --out_v $PxV

# 3) 월별 수익률 생성
python .\scripts\make_returns_monthly.py --asof $ASOF --metric $Metric --px_v $PxV --out_v $RetV --start 20160101

# 4) 백테스트/리포트 (실전 메인: B + hysteresis + 비용)
python .\scripts\backtest_quarterly_rebalance_v2.py `
  --asof $ASOF `
  --metric $Metric `
  --feat_v $FeatOutV `
  --ret_v $RetV `
  --k $K `
  --strategy $Strategy `
  --hold_bonus $HoldBonus `
  --entry_gap $EntryGap `
  --tcost_bps $TCostBps `
  --out_v $OutV

# 5) report 요약 테이블 갱신
python .\scripts\ops\summarize_reports.py --asof $ASOF --metric $Metric

Write-Host "[DONE] monthly live run completed."
