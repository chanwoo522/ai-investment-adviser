param(
    [ValidateSet("fast","smart","refresh")]
    [string]$Mode = "smart",

    [string]$ASOF = "2026-03-29",
    [string]$TARGET = "2026-03-31",
    [string]$METRIC = "revenue_op",
    [string]$STRAT = "D_quality_filter_debt_profitaccel_liq",

    [int]$McapTop = 800,
    [double]$TrdBot = 0.1,

    [int]$PX_V = 1,
    [int]$RET_V = 1,
    [int]$FACTOR_V = 3291,
    [int]$FEAT_V = 3291,
    [int]$ACTION_V = 3291,
    [int]$EXEC_V = 3291,
    [int]$REPORT_V = 3291,

    [double]$TOTAL_VALUE = 100000000,
    [string]$HOLDINGS_CSV = "",

    [switch]$ProtectHoldings,
    [switch]$SkipExecutionPlan,
    [switch]$SkipReport
)

$ErrorActionPreference = "Stop"

function Resolve-Python {
    $cands = @(".\.venv\Scripts\python.exe", ".\.venv\bin\python", "python")
    foreach($c in $cands){
        if($c -eq "python"){ return $c }
        if(Test-Path $c){ return (Resolve-Path $c).Path }
    }
    return "python"
}

function Find-FirstExisting {
    param([string[]]$Paths)
    foreach($p in $Paths){
        if($p -and (Test-Path $p)){ return (Resolve-Path $p).Path }
    }
    return $null
}

function Ensure-Dir {
    param([string]$Path)
    if(!(Test-Path $Path)){ New-Item -ItemType Directory -Path $Path -Force | Out-Null }
}

function Find-Holdings {
    param([string]$Explicit)
    if($Explicit -and (Test-Path $Explicit)){ return (Resolve-Path $Explicit).Path }
    $paths = @(
        ".\current_portfolio\20260314_holdings_clean_manual.csv",
        ".\data\processed\current_holdings_manual.csv",
        ".\data\processed\my_current_holdings.csv",
        ".\data\processed\empty_holdings.csv"
    )
    $p = Find-FirstExisting -Paths $paths
    if($null -eq $p){ throw "No holdings csv found." }
    return $p
}

function Find-ExecutionConfig {
    $paths = @(
        ".\configs\execution.yaml",
        ".\configs\execution_config.yaml",
        ".\configs\strategy_config.yaml",
        ".\configs\config.yaml"
    )
    return Find-FirstExisting -Paths $paths
}

function Find-ExactProcessedFile {
    param(
        [string]$Prefix,
        [string]$AsOf,
        [string]$Ext = "parquet"
    )
    $pat = "$Prefix" + "__asof=$AsOf__*.$Ext"
    $files = Get-ChildItem .\data\processed -File -ErrorAction SilentlyContinue | Where-Object { $_.Name -like $pat } | Sort-Object LastWriteTime -Descending
    if($files.Count -eq 0){ return $null }
    return $files[0].FullName
}

function Run-Step {
    param(
        [string]$Label,
        [string]$ScriptPath,
        [string[]]$StepArgs,
        [switch]$Optional
    )
    $cmd = @($script:PY, $ScriptPath) + $StepArgs
    Write-Host "[RUN][$Label] $($cmd -join ' ')"
    & $script:PY $ScriptPath @StepArgs
    $rc = $LASTEXITCODE
    if($rc -ne 0){
        if($Optional){
            Write-Host "[WARN][$Label] failed rc=$rc -> continue"
            return $false
        }
        throw "$Label failed (rc=$rc)"
    }
    return $true
}

function Assert-File {
    param([string]$Path, [string]$Label)
    if(!(Test-Path $Path)){ throw "$Label not found: $Path" }
}

function Assert-Universe-Usable {
    param([string]$UniversePath)
    $code = @"
import pandas as pd, sys
p = r'''$UniversePath'''
df = pd.read_parquet(p) if p.lower().endswith('.parquet') else pd.read_csv(p, dtype={'ticker':str})
ticks = df['ticker'].astype(str).str.zfill(6).nunique() if 'ticker' in df.columns else 0
print(f'rows={len(df)} tickers={ticks}')
sys.exit(0 if len(df) > 0 else 7)
"@
    & $script:PY -c $code
    if($LASTEXITCODE -ne 0){ throw "Universe unusable (0 rows): $UniversePath" }
}

function Assert-Fundamentals-Usable {
    param([string]$Path)
    $code = @"
import pandas as pd, sys
p = r'''$Path'''
df = pd.read_parquet(p)
need = {'ticker','year','quarter'}
ok = need.issubset(df.columns) and len(df) > 0
ticks = df['ticker'].astype(str).str.zfill(6).nunique() if 'ticker' in df.columns else 0
print(f'rows={len(df)} tickers={ticks}')
sys.exit(0 if ok else 8)
"@
    & $script:PY -c $code
    if($LASTEXITCODE -ne 0){ throw "Fundamentals unusable: $Path" }
}

function Ensure-UniverseContainsHoldings {
    param(
        [string]$UniversePath,
        [string]$MarketdataPath,
        [string]$HoldingsPath
    )
    $code = @"
import pandas as pd
from pathlib import Path
u_path = Path(r'''$UniversePath''')
m_path = Path(r'''$MarketdataPath''')
h_path = Path(r'''$HoldingsPath''')
u = pd.read_parquet(u_path) if u_path.suffix.lower()=='.parquet' else pd.read_csv(u_path, dtype={'ticker':str})
m = pd.read_parquet(m_path)
h = pd.read_csv(h_path, dtype={'ticker':str})
for df in (u, m, h):
    if 'ticker' in df.columns:
        df['ticker'] = df['ticker'].astype(str).str.extract(r'(\d+)')[0].str.zfill(6)
hold = set(h['ticker'].dropna().astype(str))
cur = set(u['ticker'].dropna().astype(str))
missing = sorted(hold - cur)
print(f'holdings_total={len(hold)} universe_rows={len(u)} missing_holdings={len(missing)}')
if missing:
    add = m[m['ticker'].isin(missing)].copy()
    if len(add) > 0:
        if 'cap_bucket' not in add.columns:
            add['cap_bucket'] = 'holdings_guard'
        for col in u.columns:
            if col not in add.columns:
                add[col] = pd.NA
        add = add[u.columns]
        u2 = pd.concat([u, add], ignore_index=True).drop_duplicates(subset=['ticker'], keep='first')
        if u_path.suffix.lower() == '.parquet':
            u2.to_parquet(u_path, index=False)
        else:
            u2.to_csv(u_path, index=False, encoding='utf-8-sig')
        print(f'[OK] appended_missing_holdings={len(add)}')
    else:
        print('[WARN] no matching rows found in marketdata for missing holdings')
"@
    & $script:PY -c $code
    if($LASTEXITCODE -ne 0){ throw "Ensure-UniverseContainsHoldings failed" }
}

function Health-Check {
    param(
        [string]$FundPath,
        [string]$ScorePath,
        [string]$HoldingsPath
    )
    $code = @"
import pandas as pd
fund = pd.read_parquet(r'''$FundPath''')
score = pd.read_csv(r'''$ScorePath''', dtype={'ticker':str})
hold = pd.read_csv(r'''$HoldingsPath''', dtype={'ticker':str})
for df in (fund, score, hold):
    if 'ticker' in df.columns:
        df['ticker'] = df['ticker'].astype(str).str.extract(r'(\d+)')[0].str.zfill(6)
fund_t = set(fund['ticker']) if 'ticker' in fund.columns else set()
score_t = set(score['ticker']) if 'ticker' in score.columns else set()
hold_t = set(hold['ticker']) if 'ticker' in hold.columns else set()
missing_in_fund = sorted(hold_t - fund_t)
missing_in_score = sorted(hold_t - score_t)
print(f'fund_rows={len(fund)} fund_tickers={len(fund_t)}')
print(f'score_rows={len(score)} score_tickers={len(score_t)}')
print(f'holdings_rows={len(hold)} holdings_tickers={len(hold_t)}')
print(f'missing_holdings_in_fundamentals={missing_in_fund}')
print(f'missing_holdings_in_scores={missing_in_score}')
"@
    & $script:PY -c $code
}

$PY = Resolve-Python
$holdings = Find-Holdings -Explicit $HOLDINGS_CSV
$config = Find-ExecutionConfig

Ensure-Dir ".\data\live\reports"
Ensure-Dir ".\data\live\actions"
Ensure-Dir ".\data\live\candidates"

Write-Host "=== SMART LIVE REBALANCE START ==="
Write-Host "Mode=$Mode ASOF=$ASOF TARGET=$TARGET METRIC=$METRIC STRAT=$STRAT"
Write-Host "[INFO] python   : $PY"
Write-Host "[INFO] holdings : $holdings"
if($config){ Write-Host "[INFO] config   : $config" } else { Write-Host "[WARN] config not found" }

if(($Mode -ne "fast") -and (-not $env:DART_API_KEY)){
    throw "DART_API_KEY env var not set."
}

$script:PY = $PY

$fundExact = Find-ExactProcessedFile -Prefix "fundamentals_quarterly" -AsOf $ASOF -Ext "parquet"
$masterExact = Find-ExactProcessedFile -Prefix "krx_master" -AsOf $ASOF -Ext "parquet"
$marketExact = Find-ExactProcessedFile -Prefix "krx_marketdata" -AsOf $ASOF -Ext "parquet"

$needPrepare = $false
if($Mode -eq "refresh"){ $needPrepare = $true }
elseif($Mode -eq "smart"){
    if((-not $fundExact) -or (-not $masterExact) -or (-not $marketExact)){ $needPrepare = $true }
}

if($needPrepare){
    Run-Step -Label "prepare_asof" -ScriptPath ".\scripts\data_pipeline\prepare_asof.py" -StepArgs @("--asof",$ASOF,"--metric",$METRIC)
    $fundExact = Find-ExactProcessedFile -Prefix "fundamentals_quarterly" -AsOf $ASOF -Ext "parquet"
    $masterExact = Find-ExactProcessedFile -Prefix "krx_master" -AsOf $ASOF -Ext "parquet"
    $marketExact = Find-ExactProcessedFile -Prefix "krx_marketdata" -AsOf $ASOF -Ext "parquet"
}

Assert-File $fundExact "Exact fundamentals"
Assert-File $marketExact "Exact marketdata"
Assert-Fundamentals-Usable -Path $fundExact

Run-Step -Label "build_universe" -ScriptPath ".\scripts\data_pipeline\build_universe.py" -StepArgs @("--asof",$ASOF,"--mcap_top",$McapTop,"--trd_bot",$TrdBot)
$universeParquet = ".\data\processed\universe__asof=${ASOF}__src=phase1__mcap_top=${McapTop}__trd_bot=${TrdBot}__v=1.parquet"
Assert-File $universeParquet "Universe parquet"

if($ProtectHoldings){
    Ensure-UniverseContainsHoldings -UniversePath $universeParquet -MarketdataPath $marketExact -HoldingsPath $holdings
}

Assert-Universe-Usable -UniversePath $universeParquet

if(Test-Path ".\scripts\ops\export_universe_csv.py"){
    Run-Step -Label "export_universe_csv" -ScriptPath ".\scripts\ops\export_universe_csv.py" -StepArgs @("--asof",$ASOF,"--metric",$METRIC,"--out_v","1") -Optional
}

Run-Step -Label "build_factors" -ScriptPath ".\scripts\data_pipeline\build_factors_ttm_acc2.py" -StepArgs @("--asof",$ASOF,"--metric",$METRIC,"--input_parquet",$fundExact,"--out_v",$FACTOR_V)
Run-Step -Label "make_features_live" -ScriptPath ".\scripts\data_pipeline\make_features_live.py" -StepArgs @("--asof",$ASOF,"--metric",$METRIC,"--in_v",$FACTOR_V,"--out_v",$FEAT_V,"--save_meta")
Run-Step -Label "score_latest_rebalance" -ScriptPath ".\scripts\live\score_latest_rebalance.py" -StepArgs @("--asof",$ASOF,"--metric",$METRIC,"--feat_v",$FEAT_V,"--strategy",$STRAT,"--target_date",$TARGET,"--current_csv",$holdings)
Run-Step -Label "generate_live_actions" -ScriptPath ".\scripts\live\generate_live_actions.py" -StepArgs @("--asof",$ASOF,"--metric",$METRIC,"--strategy",$STRAT,"--feat_v",$FEAT_V,"--target_date",$TARGET,"--holdings_csv",$holdings,"--out_v",$ACTION_V,"--save_candidates")

$actionsCsv = ".\data\live\actions\live_actions__asof=${ASOF}__metric=${METRIC}__strat=${STRAT}__target=${TARGET}__v=${ACTION_V}.csv"
$featuresLive = ".\data\features\features_live\features_live__asof=${ASOF}__metric=${METRIC}__v=${FEAT_V}.parquet"
$scoreCsv = ".\data\live\scores\latest_scores__asof=${ASOF}__metric=${METRIC}__strat=${STRAT}__featv=${FEAT_V}__target=${TARGET}__full_universe.csv"
$phase1 = ".\data\features\features_phase1__asof=${ASOF}__src=phase1__v=1.parquet"
$execPlan = ".\data\processed\execution_plan__total=$([int][math]::Round($TOTAL_VALUE,0))__v=${EXEC_V}.csv"
$outputMd = ".\data\live\reports\rebalance_report__asof=${ASOF}__target=${TARGET}__metric=${METRIC}__strat=${STRAT}__v=${REPORT_V}.md"
$outputCsv = ".\data\live\reports\rebalance_report_detail__asof=${ASOF}__target=${TARGET}__metric=${METRIC}__strat=${STRAT}__v=${REPORT_V}.csv"
$outputHtml = ".\data\live\reports\rebalance_report__asof=${ASOF}__target=${TARGET}__metric=${METRIC}__strat=${STRAT}__v=${REPORT_V}.html"

Assert-File $actionsCsv "Actions CSV"
Assert-File $featuresLive "Features live parquet"
Assert-File $scoreCsv "Latest score CSV"

Write-Host "[STEP] health_check"
Health-Check -FundPath $fundExact -ScorePath $scoreCsv -HoldingsPath $holdings

if((-not $SkipExecutionPlan) -and $config){
    Run-Step -Label "make_execution_plan" -ScriptPath ".\scripts\live\make_execution_plan.py" -StepArgs @("--actions_csv",$actionsCsv,"--total_capital",$TOTAL_VALUE,"--config",$config,"--out_v",$EXEC_V,"--asof",$ASOF,"--metric",$METRIC,"--ret_v","1","--price_date",$TARGET) -Optional
}

if(-not $SkipReport){
    if((Test-Path $execPlan) -and (Test-Path $phase1)){
        Run-Step -Label "generate_report" -ScriptPath ".\scripts\live\generate_rebalance_report.py" -StepArgs @("--actions_csv",$actionsCsv,"--features_live",$featuresLive,"--execution_plan",$execPlan,"--supp_file",$phase1,"--strategy",$STRAT,"--asof",$ASOF,"--target_date",$TARGET,"--output_md",$outputMd,"--output_csv",$outputCsv,"--output_html",$outputHtml)
    } elseif (Test-Path $execPlan) {
        Run-Step -Label "generate_report" -ScriptPath ".\scripts\live\generate_rebalance_report.py" -StepArgs @("--actions_csv",$actionsCsv,"--features_live",$featuresLive,"--execution_plan",$execPlan,"--strategy",$STRAT,"--asof",$ASOF,"--target_date",$TARGET,"--output_md",$outputMd,"--output_csv",$outputCsv,"--output_html",$outputHtml)
    } elseif (Test-Path $phase1) {
        Run-Step -Label "generate_report" -ScriptPath ".\scripts\live\generate_rebalance_report.py" -StepArgs @("--actions_csv",$actionsCsv,"--features_live",$featuresLive,"--supp_file",$phase1,"--strategy",$STRAT,"--asof",$ASOF,"--target_date",$TARGET,"--output_md",$outputMd,"--output_csv",$outputCsv,"--output_html",$outputHtml)
    } else {
        Run-Step -Label "generate_report" -ScriptPath ".\scripts\live\generate_rebalance_report.py" -StepArgs @("--actions_csv",$actionsCsv,"--features_live",$featuresLive,"--strategy",$STRAT,"--asof",$ASOF,"--target_date",$TARGET,"--output_md",$outputMd,"--output_csv",$outputCsv,"--output_html",$outputHtml)
    }
}

Write-Host ""
Write-Host "=== DONE ==="
Write-Host "[OUT] fundamentals : $fundExact"
Write-Host "[OUT] universe     : $universeParquet"
Write-Host "[OUT] actions      : $actionsCsv"
Write-Host "[OUT] scores       : $scoreCsv"
if(Test-Path $execPlan){ Write-Host "[OUT] exec         : $execPlan" }
if(-not $SkipReport){
    Write-Host "[OUT] report       : $outputMd"
    Write-Host "[OUT] detail       : $outputCsv"
    Write-Host "[OUT] html         : $outputHtml"
}
