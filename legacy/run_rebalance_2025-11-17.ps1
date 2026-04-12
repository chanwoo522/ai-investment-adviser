$ErrorActionPreference = "Stop"

# ======================================
# run_rebalance__asof=2025-11-16__target=2025-11-30__metric=revenue_op__strat=D_quality_filter_debt_profitaccel_liq.ps1
# 목적:
#  - 2025-11-16 시점에서 2025년 2Q/3Q까지 이용 가능한 정보로 리밸런싱 재현
#  - 2026-03-23 기준 성과 확인
# ======================================

$ASOF      = "2025-11-16"
$TARGET    = "2025-11-30"
$TRADEDATE = "2025-12-01"
$EVAL      = "2026-03-23"
$METRIC    = "revenue_op"
$STRAT     = "D_quality_filter_debt_profitaccel_liq"

$MKT_V    = 1
$MERGED_V = 10
$FACTOR_V = 311
$FEAT_V   = 311
$ACTION_V = 11
$EXEC_V   = 11
$REPORT_V = 11
$PRICE_V  = 10

$TOTAL    = 100000000
$HOLDINGS = ".\data\processed\empty_holdings.csv"
$PY = ".\.venv\Scripts\python.exe"

function Run-Step {
    param([string]$Title,[string]$Exe,[string[]]$ScriptArgs)
    Write-Host ""
    Write-Host "============================================================" -ForegroundColor Cyan
    Write-Host "[STEP] $Title" -ForegroundColor Cyan
    Write-Host "============================================================" -ForegroundColor Cyan
    Write-Host ($Exe + " " + ($ScriptArgs -join " ")) -ForegroundColor DarkGray
    & $Exe @ScriptArgs
    if ($LASTEXITCODE -ne 0) { throw "Step failed: $Title (exit code=$LASTEXITCODE)" }
}

function Assert-Exists($PathText) {
    if (-not (Test-Path $PathText)) { throw "Required file not found: $PathText" }
}

Write-Host "Starting time-travel rebalance pipeline..." -ForegroundColor Green
Write-Host "ASOF=$ASOF TARGET=$TARGET TRADEDATE=$TRADEDATE EVAL=$EVAL METRIC=$METRIC STRAT=$STRAT" -ForegroundColor Green

Assert-Exists $PY

# 0) empty holdings
& $PY -c "from pathlib import Path; Path(r'.\data\processed\empty_holdings.csv').write_text('ticker,shares\n', encoding='utf-8')"
if ($LASTEXITCODE -ne 0) { throw "Failed to create empty holdings" }

# 1) universe 준비
Run-Step -Title "Build universe" -Exe $PY -ScriptArgs @(
    ".\scripts\build_universe.py",
    "--asof", $ASOF,
    "--metric", $METRIC
)

# 2) marketdata 준비
Run-Step -Title "Collect KRX marketdata" -Exe $PY -ScriptArgs @(
    ".\scripts\collect_krx_marketdata.py",
    "--asof", $ASOF,
    "--metric", $METRIC,
    "--out_v", "$MKT_V"
)

# 3) fundamentals 수집
Run-Step -Title "Collect fundamentals quarterly" -Exe $PY -ScriptArgs @(
    ".\scripts\collect_fundamentals_quarterly.py",
    "--asof", $ASOF,
    "--start_year", "2016",
    "--end_year", "2025",
    "--fs_div", "CFS",
    "--force"
)

# 4) fundamentals 병합
Run-Step -Title "Merge fundamentals quarterly" -Exe $PY -ScriptArgs @(
    ".\scripts\merge_fundamentals_quarterly.py",
    "--asof", $ASOF,
    "--fs_div", "CFS",
    "--out_v", "$MERGED_V"
)

# 5) factor 생성
Run-Step -Title "Build factors (TTM / acc2)" -Exe $PY -ScriptArgs @(
    ".\scripts\build_factors_ttm_acc2.py",
    "--asof", $ASOF,
    "--metric", $METRIC,
    "--in_v", "$MERGED_V",
    "--out_v", "$FACTOR_V"
)

# 6) features_live 생성
Run-Step -Title "Build features_live" -Exe $PY -ScriptArgs @(
    ".\scripts\make_features_live.py",
    "--asof", $ASOF,
    "--metric", $METRIC,
    "--in_v", "$FACTOR_V",
    "--out_v", "$FEAT_V",
    "--save_meta",
    "--master_src", "pykrx",
    "--master_v", "$MKT_V"
)

# 7) 스코어 계산
Run-Step -Title "Score latest rebalance" -Exe $PY -ScriptArgs @(
    ".\scripts\score_latest_rebalance.py",
    "--asof", $ASOF,
    "--metric", $METRIC,
    "--feat_v", "$FEAT_V",
    "--strategy", $STRAT,
    "--k", "10",
    "--max_per_group", "2",
    "--target_date", $TARGET,
    "--clip_z", "3.0",
    "--use_robust_z"
)

# 8) 액션 생성
Run-Step -Title "Generate live actions" -Exe $PY -ScriptArgs @(
    ".\scripts\generate_live_actions.py",
    "--asof", $ASOF,
    "--metric", $METRIC,
    "--strategy", $STRAT,
    "--feat_v", "$FEAT_V",
    "--target_date", $TARGET,
    "--holdings_csv", $HOLDINGS,
    "--out_v", "$ACTION_V",
    "--save_candidates"
)

# 9) execution plan
Run-Step -Title "Make execution plan" -Exe $PY -ScriptArgs @(
    ".\scripts\live\make_execution_plan.py",
    "--actions_csv", ".\data\processed\live_actions__asof=${ASOF}__metric=${METRIC}__strat=${STRAT}__target=${TARGET}__v=${ACTION_V}.csv",
    "--total_capital", "$TOTAL",
    "--config", ".\configs\execution.yaml",
    "--out_v", "$EXEC_V",
    "--asof", $ASOF,
    "--metric", $METRIC,
    "--ret_v", "1",
    "--price_date", $TRADEDATE
)

# 10) 보고서 생성
Run-Step -Title "Generate rebalance report" -Exe $PY -ScriptArgs @(
    ".\scripts\generate_rebalance_report.py",
    "--actions_csv", ".\data\processed\live_actions__asof=${ASOF}__metric=${METRIC}__strat=${STRAT}__target=${TARGET}__v=${ACTION_V}.csv",
    "--features_live", ".\data\processed\features_live__asof=${ASOF}__metric=${METRIC}__v=${FEAT_V}.parquet",
    "--execution_plan", ".\data\processed\execution_plan__total=${TOTAL}__v=${EXEC_V}.csv",
    "--supp_file", ".\data\processed\krx_marketdata__asof=${ASOF}__src=pykrx__lookback=365d__v=${MKT_V}.parquet",
    "--supp_file", ".\data\processed\latest_scores__asof=${ASOF}__metric=${METRIC}__strat=${STRAT}__featv=${FEAT_V}__target=${TARGET}__full_universe.csv",
    "--supp_file", ".\data\processed\fundamentals_quarterly__asof=${ASOF}__src=dart_merged__fs=CFS__y=2016-2025__v=${MERGED_V}.parquet",
    "--strategy", $STRAT,
    "--asof", $ASOF,
    "--target_date", $TARGET,
    "--output_md", ".\data\processed\rebalance_report__asof=${ASOF}__target=${TARGET}__metric=${METRIC}__strat=${STRAT}__v=${REPORT_V}.md",
    "--output_csv", ".\data\processed\rebalance_report_detail__asof=${ASOF}__target=${TARGET}__metric=${METRIC}__strat=${STRAT}__v=${REPORT_V}.csv",
    "--output_html", ".\data\processed\rebalance_report__asof=${ASOF}__target=${TARGET}__metric=${METRIC}__strat=${STRAT}__v=${REPORT_V}.html"
)

# 11) 2026-03-23 성과 확인용 가격 준비
Run-Step -Title "Make prices daily through eval date" -Exe $PY -ScriptArgs @(
    ".\scripts\make_prices_daily.py",
    "--start", "20250101",
    "--asof", $EVAL,
    "--metric", $METRIC,
    "--out_v", "$PRICE_V"
)

# 12) 성과 계산
Run-Step -Title "Evaluate forward returns to 2026-03-23" -Exe $PY -ScriptArgs @(
    "-c",
    "import pandas as pd, numpy as np; picks=pd.read_csv(r'.\data\processed\latest_scores__asof=2025-11-16__metric=revenue_op__strat=D_quality_filter_debt_profitaccel_liq__featv=311__target=2025-11-30__topk.csv',dtype={'ticker':str}); px=pd.read_parquet(r'.\data\processed\prices_daily__src=pykrx__start=20160101__asof=2026-03-23__metric=revenue_op__v=10.parquet'); px['ticker']=px['ticker'].astype(str).str.extract(r'(\d+)')[0].str.zfill(6); px['date']=pd.to_datetime(px['date']); entry=pd.Timestamp('2025-12-01'); exit=pd.Timestamp('2026-03-23'); rows=[]; [rows.append({'ticker':t,'entry_price':(lambda d: d.loc[d['date']>=entry,'close'].iloc[0] if len(d.loc[d['date']>=entry]) else np.nan)(px[px['ticker']==t].sort_values('date')),'exit_price':(lambda d: d.loc[d['date']<=exit,'close'].iloc[-1] if len(d.loc[d['date']<=exit]) else np.nan)(px[px['ticker']==t].sort_values('date'))}) for t in picks['ticker'].astype(str).str.zfill(6)]; res=pd.DataFrame(rows); res['return']=np.where((res['entry_price'].notna())&(res['exit_price'].notna())&(res['entry_price']!=0), res['exit_price']/res['entry_price']-1, np.nan); print(res.to_string(index=False)); print('\n===== SUMMARY ====='); print('Mean   :', round(res['return'].mean()*100,2), '%'); print('Median :', round(res['return'].median()*100,2), '%'); print('WinRate:', round((res['return']>0).mean()*100,2), '%')"
)

Write-Host ""
Write-Host "DONE" -ForegroundColor Green
Write-Host "Report HTML: .\data\processed\rebalance_report__asof=${ASOF}__target=${TARGET}__metric=${METRIC}__strat=${STRAT}__v=${REPORT_V}.html" -ForegroundColor Green
