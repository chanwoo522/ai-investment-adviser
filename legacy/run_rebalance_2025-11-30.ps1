$ErrorActionPreference = "Stop"

$PY = ".venv\Scripts\python.exe"

$ASOF = "2025-11-16"
$TARGET = "2025-11-30"
$TRADE = "2025-12-01"
$EVAL = "2026-03-23"

$METRIC = "revenue_op"
$STRAT = "D_quality_filter_debt_profitaccel_liq"

$FUND_V = 2
$FACTOR_V = 311
$ACTION_V = 11
$EXEC_V = 11
$REPORT_V = 11
$PRICE_V_ASOF = 1
$PRICE_V_EVAL = 10
$TOTAL_CAPITAL = 100000000

function Step($Title, [scriptblock]$Body) {
    Write-Host ""
    Write-Host ("=" * 80) -ForegroundColor DarkGray
    Write-Host "[STEP] $Title" -ForegroundColor Cyan
    Write-Host ("=" * 80) -ForegroundColor DarkGray
    & $Body
    if ($LASTEXITCODE -ne 0) {
        throw "Step failed: $Title (exit code=$LASTEXITCODE)"
    }
}

Write-Host "Starting rebalance pipeline..." -ForegroundColor Green
Write-Host "ASOF=$ASOF TARGET=$TARGET TRADEDATE=$TRADE EVAL=$EVAL METRIC=$METRIC STRAT=$STRAT"

Step "Create empty holdings" {
    New-Item -ItemType Directory -Force -Path ".\data\processed" | Out-Null
    @"
ticker,shares
"@ | Out-File -FilePath ".\data\processed\empty_holdings.csv" -Encoding utf8
}

Step "Build universe" {
    & $PY .\scripts\build_universe.py --asof $ASOF
}

Step "Make prices daily to ASOF" {
    & $PY .\scripts\make_prices_daily.py --start 20160101 --asof $ASOF --metric $METRIC --out_v $PRICE_V_ASOF
}

Write-Host ""
Write-Host ("=" * 80) -ForegroundColor DarkGray
Write-Host "[STEP] Collect KRX marketdata" -ForegroundColor Cyan
Write-Host ("=" * 80) -ForegroundColor DarkGray
& $PY .\scripts\collect_krx_marketdata.py --asof $ASOF --metric $METRIC --out_v 1 --prices_v $PRICE_V_ASOF --synth_mcap
if ($LASTEXITCODE -ne 0) {
    Write-Host "[WARN] collect_krx_marketdata failed; continuing with existing fallback parquet if present." -ForegroundColor Yellow
}

Step "Collect fundamentals quarterly" {
    & $PY .\scripts\collect_fundamentals_quarterly.py --asof $ASOF --start_year 2016 --end_year 2025 --fs_div CFS --force
}

Step "Merge fundamentals quarterly" {
    & $PY .\scripts\merge_fundamentals_quarterly.py --asof $ASOF --fs_div CFS --out_v $FUND_V
}

Step "Build factors ttm acc2" {
    & $PY .\scripts\build_factors_ttm_acc2.py --asof $ASOF --metric $METRIC --in_v $FUND_V --out_v $FACTOR_V
}

Step "Make features live" {
    & $PY .\scripts\make_features_live.py --asof $ASOF --metric $METRIC --in_v $FACTOR_V --out_v $FACTOR_V --save_meta
}

Step "Score latest rebalance" {
    & $PY .\scripts\score_latest_rebalance.py `
        --asof $ASOF `
        --metric $METRIC `
        --feat_v $FACTOR_V `
        --strategy $STRAT `
        --k 10 `
        --max_per_group 2 `
        --target_date $TARGET `
        --clip_z 3.0 `
        --use_robust_z
}

Step "Generate live actions" {
    & $PY .\scripts\generate_live_actions.py `
        --asof $ASOF `
        --metric $METRIC `
        --strategy $STRAT `
        --feat_v $FACTOR_V `
        --target_date $TARGET `
        --holdings_csv .\data\processed\empty_holdings.csv `
        --out_v $ACTION_V `
        --save_candidates
}

Step "Make execution plan" {
    & $PY .\scripts\live\make_execution_plan.py `
        --actions_csv .\data\processed\live_actions__asof=${ASOF}__metric=${METRIC}__strat=${STRAT}__target=${TARGET}__v=${ACTION_V}.csv `
        --total_capital $TOTAL_CAPITAL `
        --config .\configs\execution.yaml `
        --out_v $EXEC_V `
        --asof $ASOF `
        --metric $METRIC `
        --ret_v 1 `
        --price_date $TRADE
}

Step "Generate rebalance report" {
    & $PY .\scripts\generate_rebalance_report.py `
        --actions_csv .\data\processed\live_actions__asof=${ASOF}__metric=${METRIC}__strat=${STRAT}__target=${TARGET}__v=${ACTION_V}.csv `
        --features_live .\data\processed\features_live__asof=${ASOF}__metric=${METRIC}__v=${FACTOR_V}.parquet `
        --execution_plan .\data\processed\execution_plan__total=100000000__v=${EXEC_V}.csv `
        --supp_file .\data\processed\krx_marketdata__asof=${ASOF}__src=pykrx__lookback=365d__v=1.parquet `
        --supp_file .\data\processed\latest_scores__asof=${ASOF}__metric=${METRIC}__strat=${STRAT}__featv=${FACTOR_V}__target=${TARGET}__full_universe.csv `
        --supp_file .\data\processed\fundamentals_quarterly__asof=${ASOF}__src=dart_merged__fs=CFS__y=2016-2025__v=${FUND_V}.parquet `
        --strategy $STRAT `
        --asof $ASOF `
        --target_date $TARGET `
        --output_md .\data\processed\rebalance_report__asof=${ASOF}__target=${TARGET}__metric=${METRIC}__strat=${STRAT}__v=${REPORT_V}.md `
        --output_csv .\data\processed\rebalance_report_detail__asof=${ASOF}__target=${TARGET}__metric=${METRIC}__strat=${STRAT}__v=${REPORT_V}.csv `
        --output_html .\data\processed\rebalance_report__asof=${ASOF}__target=${TARGET}__metric=${METRIC}__strat=${STRAT}__v=${REPORT_V}.html
}

Step "Make prices daily to EVAL" {
    & $PY .\scripts\make_prices_daily.py --start 20160101 --asof $EVAL --metric $METRIC --out_v $PRICE_V_EVAL
}

Step "Evaluate forward returns" {
    & $PY -c @"
import pandas as pd
import numpy as np
from pathlib import Path

picks_path = r'.\data\processed\latest_scores__asof=2025-11-16__metric=revenue_op__strat=D_quality_filter_debt_profitaccel_liq__featv=311__target=2025-11-30__topk.csv'
price_path = r'.\data\processed\prices_daily__src=pykrx__start=20160101__asof=2026-03-23__metric=revenue_op__v=10.parquet'
out_path   = r'.\data\processed\forward_returns__asof=2025-11-16__target=2025-11-30__eval=2026-03-23__v=1.csv'

picks = pd.read_csv(picks_path, dtype={'ticker': str})
px = pd.read_parquet(price_path)

picks['ticker'] = picks['ticker'].astype(str).str.extract(r'(\d+)')[0].str.zfill(6)
px['ticker'] = px['ticker'].astype(str).str.extract(r'(\d+)')[0].str.zfill(6)
px['date'] = pd.to_datetime(px['date'])

price_candidates = ['close', 'Close', 'adj_close', 'Adj Close', '종가']
price_col = None
for c in price_candidates:
    if c in px.columns:
        price_col = c
        break

if price_col is None:
    raise ValueError(f'No usable price column found. Available columns: {list(px.columns)}')

entry = pd.Timestamp('2025-12-01')
exit_ = pd.Timestamp('2026-03-23')

rows = []
for t in picks['ticker']:
    df = px[px['ticker'] == t].sort_values('date').copy()

    sub0 = df[df['date'] >= entry]
    sub1 = df[df['date'] <= exit_]

    p0 = sub0[price_col].iloc[0] if len(sub0) else np.nan
    p1 = sub1[price_col].iloc[-1] if len(sub1) else np.nan

    r = (p1 / p0 - 1.0) if pd.notna(p0) and pd.notna(p1) and p0 != 0 else np.nan

    rows.append({
        'ticker': t,
        'entry_price': p0,
        'exit_price': p1,
        'return': r,
    })

res = pd.DataFrame(rows)
res.to_csv(out_path, index=False, encoding='utf-8-sig')

print(f'[INFO] price column used: {price_col}')
print(res.to_string(index=False))
print('\n===== SUMMARY =====')
print('Mean   :', round(res['return'].mean() * 100, 2), '%')
print('Median :', round(res['return'].median() * 100, 2), '%')
print('WinRate:', round((res['return'] > 0).mean() * 100, 2), '%')
print(f'[OK] saved: {out_path}')
"@
}

Write-Host ""
Write-Host "DONE" -ForegroundColor Green
Write-Host "Report HTML: .\data\processed\rebalance_report__asof=${ASOF}__target=${TARGET}__metric=${METRIC}__strat=${STRAT}__v=${REPORT_V}.html" -ForegroundColor Green