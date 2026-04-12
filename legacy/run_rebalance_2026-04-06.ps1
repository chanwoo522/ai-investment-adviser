$ErrorActionPreference = "Stop"

# =========================
# User-configurable vars
# =========================
$ASOF    = "2026-03-31"
$TARGET  = "2026-03-31"
$METRIC  = "revenue_op"
$STRAT   = "D_quality_filter_debt_profitaccel_liq"

$MASTER_V = 1
$MKT_V    = 1
$MERGED_V = 2
$FACTOR_V = 209
$FEAT_V   = 209
$ACTION_V = 5
$REPORT_V = 9

$HOLDINGS = ".\current_portfolio\20260314_holdings_clean_manual.csv"
$TOTAL    = 100000000
$EXEC_V   = 2

# Optional: targeted patch tickers for late/missing filings
$PATCH_TICKERS = "058470,214150"
$RUN_TARGET_PATCH = $true

# Python executable
$PY = ".\.venv\Scripts\python.exe"

# =========================
# Helpers
# =========================
function Run-Step {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Title,

        [Parameter(Mandatory = $true)]
        [string]$Exe,

        [Parameter(Mandatory = $true)]
        [string[]]$ScriptArgs
    )

    Write-Host ""
    Write-Host "============================================================" -ForegroundColor Cyan
    Write-Host "[STEP] $Title" -ForegroundColor Cyan
    Write-Host "============================================================" -ForegroundColor Cyan
    Write-Host ($Exe + " " + ($ScriptArgs -join " ")) -ForegroundColor DarkGray

    & $Exe @ScriptArgs
    if ($LASTEXITCODE -ne 0) {
        throw "Step failed: $Title (exit code=$LASTEXITCODE)"
    }
}

function Assert-Exists($PathText) {
    if (-not (Test-Path $PathText)) {
        throw "Required file not found: $PathText"
    }
}

# =========================
# Pre-checks
# =========================
Write-Host ""
Write-Host "Starting rebalance pipeline..." -ForegroundColor Green
Write-Host "ASOF=$ASOF TARGET=$TARGET METRIC=$METRIC STRAT=$STRAT" -ForegroundColor Green

Assert-Exists $PY
Assert-Exists $HOLDINGS

# =========================
# 1) KRX master / marketdata / universe
# =========================
Run-Step -Title "Collect KRX master" -Exe $PY -ScriptArgs @(
    ".\scripts\collect_krx_master.py",
    "--asof", $ASOF,
    "--out_v", "$MASTER_V",
    "--src", "pykrx"
)

Run-Step -Title "Collect KRX marketdata" -Exe $PY -ScriptArgs @(
    ".\scripts\collect_krx_marketdata.py",
    "--asof", $ASOF,
    "--metric", $METRIC,
    "--out_v", "$MKT_V",
    "--soft_fail",
    "--synth_mcap",
    "--prices_v", "1"
)

Run-Step -Title "Build universe" -Exe $PY -ScriptArgs @(
    ".\scripts\build_universe.py",
    "--asof", $ASOF,
    "--mcap_top", "800",
    "--trd_bot", "0.1"
)

# =========================
# 2) Fundamentals collection
# =========================
Run-Step -Title "Collect fundamentals (missing_only, broad)" -Exe $PY -ScriptArgs @(
    ".\scripts\collect_fundamentals_quarterly.py",
    "--asof", $ASOF,
    "--metric", $METRIC,
    "--mode", "missing_only",
    "--fs_div", "CFS",
    "--merge_existing"
)

if ($RUN_TARGET_PATCH -and $PATCH_TICKERS -ne "") {
    Run-Step -Title "Collect fundamentals (targeted patch tickers)" -Exe $PY -ScriptArgs @(
        ".\scripts\collect_fundamentals_quarterly.py",
        "--asof", $ASOF,
        "--metric", $METRIC,
        "--mode", "missing_only",
        "--tickers", $PATCH_TICKERS,
        "--start_year", "2023",
        "--end_year", "2025",
        "--fs_div", "CFS",
        "--merge_existing",
        "--force"
    )
}

# =========================
# 3) Merge fundamentals
# =========================
Run-Step -Title "Merge fundamentals" -Exe $PY -ScriptArgs @(
    ".\scripts\merge_fundamentals_quarterly.py",
    "--asof", $ASOF,
    "--fs_div", "CFS",
    "--out_v", "$MERGED_V"
)

# =========================
# 4) Build factors
# =========================
Run-Step -Title "Build factors (TTM / acc2)" -Exe $PY -ScriptArgs @(
    ".\scripts\build_factors_ttm_acc2.py",
    "--asof", $ASOF,
    "--metric", $METRIC,
    "--in_v", "$MERGED_V",
    "--out_v", "$FACTOR_V"
)

# =========================
# 5) Build features_live
# =========================
Run-Step -Title "Build features_live" -Exe $PY -ScriptArgs @(
    ".\scripts\make_features_live.py",
    "--asof", $ASOF,
    "--metric", $METRIC,
    "--in_v", "$FACTOR_V",
    "--out_v", "$FEAT_V",
    "--save_meta",
    "--master_src", "pykrx",
    "--master_v", "1"
)

# =========================
# 6) Score latest rebalance
# =========================
Run-Step -Title "Score latest rebalance" -Exe $PY -ScriptArgs @(
    ".\scripts\score_latest_rebalance.py",
    "--asof", $ASOF,
    "--metric", $METRIC,
    "--feat_v", "$FEAT_V",
    "--strategy", $STRAT,
    "--k", "10",
    "--max_per_group", "2",
    "--target_date", $TARGET,
    "--current_csv", $HOLDINGS
)

# =========================
# 7) Spot check important holdings
# =========================
Run-Step -Title "Spot check important holdings" -Exe $PY -ScriptArgs @(
    "-c",
    "import pandas as pd; p=r'.\data\processed\latest_scores__asof=2026-03-31__metric=revenue_op__strat=D_quality_filter_debt_profitaccel_liq__featv=209__target=2026-03-31__current_holdings_scored.csv'; df=pd.read_csv(p,dtype={'ticker':str}); df['ticker']=df['ticker'].str.zfill(6); print(df[df['ticker'].isin(['058470','214150','005930'])].to_string(index=False))"
)

# =========================
# 8) Generate live actions
# =========================
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

# =========================
# 9) Generate report
# =========================
Run-Step -Title "Generate rebalance report" -Exe $PY -ScriptArgs @(
    ".\scripts\generate_rebalance_report.py",
    "--actions_csv", ".\data\processed\live_actions__asof=${ASOF}__metric=${METRIC}__strat=${STRAT}__target=${TARGET}__v=${ACTION_V}.csv",
    "--features_live", ".\data\processed\features_live__asof=${ASOF}__metric=${METRIC}__v=${FEAT_V}.parquet",
    "--execution_plan", ".\data\processed\execution_plan__total=${TOTAL}__v=${EXEC_V}.csv",
    "--supp_file", ".\data\processed\krx_marketdata__asof=${ASOF}__src=pykrx__lookback=365d__v=1.parquet",
    "--strategy", $STRAT,
    "--asof", $ASOF,
    "--target_date", $TARGET,
    "--output_md", ".\data\processed\rebalance_report__asof=${ASOF}__target=${TARGET}__strat=${STRAT}__v=${REPORT_V}.md",
    "--output_csv", ".\data\processed\rebalance_report_detail__asof=${ASOF}__target=${TARGET}__strat=${STRAT}__v=${REPORT_V}.csv",
    "--output_html", ".\data\processed\rebalance_report__asof=${ASOF}__target=${TARGET}__strat=${STRAT}__v=${REPORT_V}.html"
)

# =========================
# 10) Final summaries
# =========================
Run-Step -Title "Action counts + HOLD_REVIEW check" -Exe $PY -ScriptArgs @(
    "-c",
    "import pandas as pd; p=r'.\data\processed\live_actions__asof=2026-03-31__metric=revenue_op__strat=D_quality_filter_debt_profitaccel_liq__target=2026-03-31__v=5.csv'; df=pd.read_csv(p,dtype={'ticker':str}); print(df['action'].value_counts(dropna=False).to_string()); print(); print('--- HOLD_REVIEW ---'); x=df[df['action']=='HOLD_REVIEW']; print(x.to_string(index=False) if len(x) else 'None')"
)

Write-Host ""
Write-Host "============================================================" -ForegroundColor Green
Write-Host "[DONE] Rebalance pipeline completed successfully." -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Green
Write-Host "Key outputs:" -ForegroundColor Yellow
Write-Host " - .\data\processed\latest_scores__asof=${ASOF}__metric=${METRIC}__strat=${STRAT}__featv=${FEAT_V}__target=${TARGET}__current_holdings_scored.csv"
Write-Host " - .\data\processed\latest_scores__asof=${ASOF}__metric=${METRIC}__strat=${STRAT}__featv=${FEAT_V}__target=${TARGET}__full_universe.csv"
Write-Host " - .\data\processed\latest_scores__asof=${ASOF}__metric=${METRIC}__strat=${STRAT}__featv=${FEAT_V}__target=${TARGET}__topk.csv"
Write-Host " - .\data\processed\live_actions__asof=${ASOF}__metric=${METRIC}__strat=${STRAT}__target=${TARGET}__v=${ACTION_V}.csv"
Write-Host " - .\data\processed\live_candidates__asof=${ASOF}__metric=${METRIC}__strat=${STRAT}__target=${TARGET}__v=${ACTION_V}.csv"
Write-Host " - .\data\processed\rebalance_report__asof=${ASOF}__target=${TARGET}__strat=${STRAT}__v=${REPORT_V}.md"
Write-Host " - .\data\processed\rebalance_report__asof=${ASOF}__target=${TARGET}__strat=${STRAT}__v=${REPORT_V}.html"
Write-Host " - .\data\processed\rebalance_report_detail__asof=${ASOF}__target=${TARGET}__strat=${STRAT}__v=${REPORT_V}.csv"