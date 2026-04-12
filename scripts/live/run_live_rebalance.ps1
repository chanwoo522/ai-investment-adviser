param(
    [string]$ASOF,
    [string]$TARGET,
    [string]$METRIC = "revenue_op",
    [string]$STRAT = "D_quality_filter_debt_profitaccel_liq",
    [string]$HOLDINGS,
    [int]$FEATV,
    [int]$ACTIONV,
    [int]$REPORTV
)

# ===== 필수 체크 =====
if (-not $ASOF) { throw "ASOF is required" }
if (-not $TARGET) { throw "TARGET is required" }
if (-not $HOLDINGS) { throw "HOLDINGS is required" }
if (-not $FEATV) { throw "FEATV is required" }
if (-not $ACTIONV) { throw "ACTIONV is required" }
if (-not $REPORTV) { throw "REPORTV is required" }

Write-Host ""
Write-Host "=============================" -ForegroundColor Cyan
Write-Host " LIVE REBALANCE START"
Write-Host "============================="
Write-Host "ASOF     : $ASOF"
Write-Host "TARGET   : $TARGET"
Write-Host "HOLDINGS : $HOLDINGS"
Write-Host "FEATV    : $FEATV"
Write-Host "ACTIONV  : $ACTIONV"
Write-Host "REPORTV  : $REPORTV"
Write-Host ""

# ===== STEP 0. NAV 계산 =====
Write-Host "[STEP 0] CALCULATE NAV..." -ForegroundColor Yellow

$MARKETDATA = ".\data\processed\krx_marketdata__asof=${ASOF}__src=pykrx__lookback=365d__v=2.parquet"

$NAV_CODE = @'
import pandas as pd
import sys

holdings = sys.argv[1]
marketdata = sys.argv[2]

df = pd.read_csv(holdings, dtype={'ticker': str})
px = pd.read_parquet(marketdata)

if 'ticker' not in px.columns:
    raise ValueError('krx_marketdata missing ticker column')

price_col = None
for c in ['close', 'Close', 'price']:
    if c in px.columns:
        price_col = c
        break

if price_col is None:
    raise ValueError('krx_marketdata missing price column')

df['ticker'] = df['ticker'].astype(str).str.zfill(6)
px['ticker'] = px['ticker'].astype(str).str.zfill(6)

m = df.merge(px[['ticker', price_col]], on='ticker', how='left')
m[price_col] = pd.to_numeric(m[price_col], errors='coerce')
m['shares'] = pd.to_numeric(m['shares'], errors='coerce')

if m[price_col].isna().any():
    bad = m.loc[m[price_col].isna(), ['ticker', 'name']]
    raise ValueError('missing close price for tickers:\n' + bad.to_string(index=False))

m['value'] = m['shares'] * m[price_col]
nav = int(round(float(m['value'].sum())))
print(nav)
'@

$NAV_RAW = python -c $NAV_CODE $HOLDINGS $MARKETDATA
$NAV_TEXT = ($NAV_RAW | Out-String).Trim()

if (-not $NAV_TEXT) {
    throw "[FAIL] NAV calculation returned empty output"
}

if ($NAV_TEXT -notmatch '^\d+$') {
    Write-Host "[DEBUG] NAV raw output:" -ForegroundColor Red
    Write-Host $NAV_TEXT -ForegroundColor Red
    throw "[FAIL] NAV calculation did not return a clean integer"
}

$NAV = [int64]$NAV_TEXT

if ($NAV -le 0) {
    throw "[FAIL] NAV must be > 0"
}

Write-Host "[INFO] MARKETDATA = $MARKETDATA"
Write-Host "[INFO] NAV = $NAV"
Write-Host ""

# ===== STEP 1. SCORE =====
Write-Host "[STEP 1] SCORE..." -ForegroundColor Yellow

python .\scripts\live\score_latest_rebalance.py `
--asof $ASOF `
--metric $METRIC `
--feat_v $FEATV `
--strategy $STRAT `
--target_date $TARGET `
--holdings_csv $HOLDINGS

if ($LASTEXITCODE -ne 0) {
    throw "[FAIL] score_latest_rebalance failed"
}

Write-Host ""

# ===== STEP 2. ACTIONS =====
Write-Host "[STEP 2] ACTIONS..." -ForegroundColor Yellow

python .\scripts\live\generate_live_actions.py `
--asof $ASOF `
--metric $METRIC `
--strategy $STRAT `
--feat_v $FEATV `
--target_date $TARGET `
--holdings_csv $HOLDINGS `
--out_v $ACTIONV `
--save_candidates

if ($LASTEXITCODE -ne 0) {
    throw "[FAIL] generate_live_actions failed"
}

Write-Host ""

# ===== 공통 경로 변수 =====
$ACTIONS = ".\data\live\actions\live_actions__asof=${ASOF}__metric=${METRIC}__strat=${STRAT}__target=${TARGET}__v=${ACTIONV}.csv"
$FEATURES = ".\data\features\features_live\features_live__asof=${ASOF}__metric=${METRIC}__v=${FEATV}.parquet"
$EXEC = ".\data\processed\execution_plan__total=${NAV}__v=${REPORTV}.csv"

$REPORT_MD = ".\data\live\reports\rebalance_report__asof=${ASOF}__target=${TARGET}__metric=${METRIC}__strat=${STRAT}__v=${REPORTV}.md"
$REPORT_CSV = ".\data\live\reports\rebalance_report_detail__asof=${ASOF}__target=${TARGET}__metric=${METRIC}__strat=${STRAT}__v=${REPORTV}.csv"
$REPORT_HTML = ".\data\live\reports\rebalance_report__asof=${ASOF}__target=${TARGET}__metric=${METRIC}__strat=${STRAT}__v=${REPORTV}.html"
$SCORES = ".\data\live\scores\latest_scores__asof=${ASOF}__metric=${METRIC}__strat=${STRAT}__featv=${FEATV}__target=${TARGET}__full_universe.csv"

Write-Host "[INFO] ACTIONS   = $ACTIONS"
Write-Host "[INFO] FEATURES  = $FEATURES"
Write-Host "[INFO] EXEC      = $EXEC"
Write-Host "[INFO] REPORT_MD = $REPORT_MD"
Write-Host "[INFO] SCORES    = $SCORES"
Write-Host ""

if (-not (Test-Path $ACTIONS)) {
    throw "[FAIL] actions csv missing before execution: $ACTIONS"
}

if (-not (Test-Path $FEATURES)) {
    throw "[FAIL] features file missing before execution: $FEATURES"
}

if (-not (Test-Path ".\configs\execution.yaml")) {
    throw "[FAIL] config missing: .\configs\execution.yaml"
}

# ===== STEP 3. EXECUTION =====
Write-Host "[STEP 3] EXECUTION..." -ForegroundColor Yellow

python .\scripts\live\make_execution_plan.py `
--actions_csv $ACTIONS `
--total_capital $NAV `
--config ".\configs\execution.yaml" `
--asof $ASOF `
--metric $METRIC `
--out_v $REPORTV

if ($LASTEXITCODE -ne 0) {
    throw "[FAIL] execution failed"
}

Write-Host ""

# ===== STEP 4. REPORT =====
Write-Host "[STEP 4] REPORT..." -ForegroundColor Yellow

python .\scripts\live\generate_rebalance_report.py `
--actions_csv $ACTIONS `
--features_live $FEATURES `
--execution_plan $EXEC `
--strategy $STRAT `
--asof $ASOF `
--target_date $TARGET `
--output_md $REPORT_MD `
--output_csv $REPORT_CSV `
--output_html $REPORT_HTML `
--scores_csv $SCORES

if ($LASTEXITCODE -ne 0) {
    throw "[FAIL] report failed"
}

Write-Host ""

# ===== STEP 5. VALIDATION =====
Write-Host "[STEP 5] VALIDATION..." -ForegroundColor Yellow

python .\scripts\ops\validate_run_outputs.py `
--asof $ASOF `
--target_date $TARGET `
--metric $METRIC `
--strategy $STRAT `
--feat_v $FEATV `
--action_v $ACTIONV `
--report_v $REPORTV `
--total_amount $NAV `
--holdings_csv $HOLDINGS

if ($LASTEXITCODE -ne 0) {
    throw "[FAIL] validation failed"
}

Write-Host ""
Write-Host "=============================" -ForegroundColor Green
Write-Host " REBALANCE COMPLETED"
Write-Host "============================="
Write-Host ""