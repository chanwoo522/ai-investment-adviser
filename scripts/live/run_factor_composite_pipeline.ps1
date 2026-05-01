param(
    [Parameter(Mandatory = $true)]
    [string]$ASOF,
    [Parameter(Mandatory = $true)]
    [string]$TARGET,
    [string]$METRIC = "revenue_op",
    [string]$STRAT = "factor_composite",
    [int]$McapTop = 800,
    [double]$TrdBot = 0.1,
    [int]$PX_V = 1,
    [int]$RET_V = 1,
    [int]$FACTOR_V = 3291,
    [int]$FEAT_V = 3291,
    [int]$ACTION_V = 9201,
    [int]$EXEC_V = 9201,
    [int]$REPORT_V = 9201,
    [int]$FUND_V = 1,
    [int]$SHARES_OUT_V = 9201,
    [double]$TOTAL_VALUE = 100000000,
    [switch]$SkipPrepare,
    [string]$HOLDINGS_CSV = "",
    [Alias("PERF_HOLDINGS_CSV")]
    [string]$PerfHoldingsCsv = "",
    [Alias("PREV_REBAL_DATE")]
    [string]$PrevRebalDate = "",
    [string]$PerformanceEndDate = "",
    [string]$BenchmarkName = "KOSDAQ150",
    [string]$BenchmarkTicker = "229200",
    [string]$DataRoot = ".\\data"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
if($PSVersionTable.PSVersion.Major -ge 7){ $PSNativeCommandUseErrorActionPreference = $false }
$env:PYTHONWARNINGS = "ignore::SyntaxWarning"

function Resolve-Python {
    $cands = @(".\.venv\Scripts\python.exe", ".\.venv\bin\python", "python")
    foreach($c in $cands){
        if($c -eq "python"){ return $c }
        if(Test-Path $c){ return (Resolve-Path $c).Path }
    }
    throw "python executable not found. Expected .venv\\Scripts\\python.exe"
}

function Ensure-Dir([string]$Path) {
    if(!(Test-Path $Path)){ New-Item -ItemType Directory -Path $Path -Force | Out-Null }
}

function Assert-File([string]$Path,[string]$Label) {
    if([string]::IsNullOrWhiteSpace($Path)){ throw "$Label is empty" }
    if(!(Test-Path $Path)){ throw "$Label not found: $Path" }
}

function Parse-IsoDate([string]$Value) {
    if($Value -match '^\d{8}$'){
        return [datetime]::ParseExact($Value, 'yyyyMMdd', $null).ToString('yyyy-MM-dd')
    }
    return [datetime]::Parse($Value).ToString('yyyy-MM-dd')
}

function Infer-PrevRebalanceDate([string]$TargetDate) {
    $dt = [datetime]::Parse($TargetDate)
    $m = $dt.Month
    $y = $dt.Year
    if($m -eq 3){ return ([datetime]::new($y - 1, 11, 30)).ToString('yyyy-MM-dd') }
    if($m -eq 5){ return ([datetime]::new($y, 3, 31)).ToString('yyyy-MM-dd') }
    if($m -eq 8){ return ([datetime]::new($y, 5, 31)).ToString('yyyy-MM-dd') }
    if($m -eq 11){ return ([datetime]::new($y, 8, 31)).ToString('yyyy-MM-dd') }
    return $TargetDate
}

function Find-FirstExisting([string[]]$Paths) {
    foreach($p in $Paths){
        if($p -and (Test-Path $p)){ return (Resolve-Path $p).Path }
    }
    return $null
}

function Resolve-AutoHoldingsCsv([string]$HintDate) {
    $bases = @(
        @{ Path = ".\\data\\portfolio\\current"; Priority = 0 },
        @{ Path = ".\\data\\portfolio\\history"; Priority = 1 },
        @{ Path = ".\\dist\\ai_inv_adv_github_min\\data\\portfolio\\current"; Priority = 9 }
    )
    $cands = New-Object System.Collections.ArrayList
    foreach($baseInfo in $bases){
        $base = [string]$baseInfo.Path
        $priority = [int]$baseInfo.Priority
        if(!(Test-Path $base)){ continue }
        $files = Get-ChildItem -LiteralPath $base -File -Filter "*holdings_clean.csv" -ErrorAction SilentlyContinue
        foreach($f in $files){
            $m = [regex]::Match($f.Name, '(\d{8})')
            if(-not $m.Success){ continue }
            try {
                $d = [datetime]::ParseExact($m.Groups[1].Value, 'yyyyMMdd', $null)
            } catch {
                continue
            }
            [void]$cands.Add([pscustomobject]@{
                Path = $f.FullName
                FileDate = $d
                Priority = $priority
            })
        }
    }
    if($cands.Count -eq 0){ throw "Could not auto-resolve holdings csv from data/portfolio/current, history, or dist fallback." }

    $hint = [datetime]::Parse($HintDate)
    $eligible = @($cands | Where-Object { $_.FileDate -le $hint } | Sort-Object FileDate, @{ Expression = "Priority"; Descending = $true }, Path)
    if($eligible.Count -gt 0){ return $eligible[-1].Path }

    $latest = @($cands | Sort-Object FileDate, @{ Expression = "Priority"; Descending = $true }, Path)
    return $latest[-1].Path
}

function Write-Json([string]$Path, $Object) {
    $parent = Split-Path -Parent $Path
    if($parent){ Ensure-Dir $parent }
    ($Object | ConvertTo-Json -Depth 8) | Set-Content -Path $Path -Encoding UTF8
}

function Stage-Copy([string]$Source,[string]$Destination) {
    Assert-File $Source "stage source"
    $destDir = Split-Path -Parent $Destination
    if($destDir){ Ensure-Dir $destDir }
    Copy-Item -LiteralPath $Source -Destination $Destination -Force
    return $Destination
}

function Invoke-Step {
    param(
        [string]$Name,
        [string]$ScriptPath,
        [string[]]$Arguments,
        [hashtable]$Manifest
    )
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    Write-Host "[RUN][$Name] $script:PY $ScriptPath $($Arguments -join ' ')"
    $output = & $script:PY $ScriptPath @Arguments 2>&1
    $code = $LASTEXITCODE
    if($null -ne $output){
        foreach($line in $output){
            Write-Host $line
        }
    }
    $sw.Stop()
    $step = [ordered]@{
        name = $Name
        script = $ScriptPath
        args = $Arguments
        exit_code = $code
        duration_sec = [math]::Round($sw.Elapsed.TotalSeconds, 2)
    }
    $Manifest.steps.Add($step) | Out-Null
    if($code -ne 0){
        throw "$Name failed (rc=$code)"
    }
}

$ASOF = Parse-IsoDate $ASOF
$TARGET = Parse-IsoDate $TARGET
$PrevRebalDate = if([string]::IsNullOrWhiteSpace($PrevRebalDate)){ Infer-PrevRebalanceDate $TARGET } else { Parse-IsoDate $PrevRebalDate }
$HOLDINGS_CSV = if([string]::IsNullOrWhiteSpace($HOLDINGS_CSV)){ Resolve-AutoHoldingsCsv $PrevRebalDate } else { $HOLDINGS_CSV }
if([string]::IsNullOrWhiteSpace($PerfHoldingsCsv)){ $PerfHoldingsCsv = $HOLDINGS_CSV }
$PrevRebalDate = Parse-IsoDate $PrevRebalDate
$PerfEnd = if($PerformanceEndDate){ Parse-IsoDate $PerformanceEndDate } else { $ASOF }

$script:PY = Resolve-Python
Assert-File $script:PY "python"
if([string]::IsNullOrWhiteSpace($env:DART_API_KEY)){
    throw "DART_API_KEY env var is required."
}

$repoRoot = (Get-Location).Path
$dataRootAbs = if(Test-Path $DataRoot){ (Resolve-Path $DataRoot).Path } else { Join-Path $repoRoot "data" }

$requiredDirs = @(
    "artifacts\\logs",
    "artifacts\\pipeline_runs\\asof=$ASOF",
    "data\\features",
    "data\\features\\features_live",
    "data\\processed",
    "data\\live\\scores",
    "data\\live\\actions",
    "data\\live\\reports",
    "data\\live\\performance",
    "data\\live\\kpi_snapshots",
    "data\\portfolio\\current",
    "data\\portfolio\\history"
)
foreach($d in $requiredDirs){ Ensure-Dir $d }

$holdingsResolved = if($HOLDINGS_CSV){ (Resolve-Path $HOLDINGS_CSV).Path } else { $null }
$perfHoldingsResolved = if($PerfHoldingsCsv){ (Resolve-Path $PerfHoldingsCsv).Path } else { $null }
if($holdingsResolved){ Assert-File $holdingsResolved "HOLDINGS_CSV" }
if($perfHoldingsResolved){ Assert-File $perfHoldingsResolved "PerfHoldingsCsv" }

$manifestPath = "artifacts\\pipeline_runs\\asof=$ASOF\\pipeline_manifest.json"
$summaryPath = "artifacts\\pipeline_runs\\asof=$ASOF\\pipeline_summary.md"
$manifest = [ordered]@{
    asof = $ASOF
    target = $TARGET
    metric = $METRIC
    strategy = $STRAT
    data_root = $dataRootAbs
    created_at = (Get-Date).ToString("s")
    provisional = $false
    provisional_source = $null
    steps = New-Object System.Collections.ArrayList
    outputs = [ordered]@{}
}

if(-not $SkipPrepare){
    Invoke-Step "prepare_asof" "scripts\\data_pipeline\\prepare_asof.py" @(
        "--asof", $ASOF,
        "--metric", $METRIC,
        "--universe_v", "1",
        "--mcap_top", "$McapTop",
        "--trd_bot", "$TrdBot",
        "--px_v", "$PX_V",
        "--ret_v", "$RET_V",
        "--lookback_years", "15",
        "--start", "20110101",
        "--fund_v", "$FUND_V",
        "--factor_in_v", "1",
        "--factor_out_v", "$FACTOR_V",
        "--fs_div", "CFS",
        "--with_shares_industry",
        "--shares_out_v", "$SHARES_OUT_V",
        "--allow_fallback",
        "--fund_mode", "union"
    ) $manifest
}
else {
    $manifest.steps.Add([ordered]@{
        name = "prepare_asof"
        script = "scripts\\data_pipeline\\prepare_asof.py"
        args = @("--asof", $ASOF, "--metric", $METRIC)
        exit_code = 0
        duration_sec = 0.0
        skipped = $true
        reason = "SkipPrepare switch"
    }) | Out-Null
}

Invoke-Step "build_features_phase1" "scripts\\data_pipeline\\build_features_phase1.py" @(
    "--asof", $ASOF
) $manifest

Invoke-Step "build_factors_ttm_acc2" "scripts\\data_pipeline\\build_factors_ttm_acc2.py" @(
    "--asof", $ASOF,
    "--metric", $METRIC,
    "--in_v", "1",
    "--out_v", "$FACTOR_V"
) $manifest

Invoke-Step "make_features_live" "scripts\\data_pipeline\\make_features_live.py" @(
    "--asof", $ASOF,
    "--metric", $METRIC,
    "--in_v", "$FACTOR_V",
    "--out_v", "$FEAT_V",
    "--save_meta"
) $manifest

Invoke-Step "score_latest_rebalance" "scripts\\live\\score_latest_rebalance.py" @(
    "--asof", $ASOF,
    "--target_date", $TARGET,
    "--metric", $METRIC,
    "--strategy", $STRAT,
    "--feat_v", "$FEAT_V",
    "--holdings_csv", $holdingsResolved
) $manifest

Invoke-Step "generate_live_actions" "scripts\\live\\generate_live_actions.py" @(
    "--asof", $ASOF,
    "--metric", $METRIC,
    "--strategy", $STRAT,
    "--feat_v", "$FEAT_V",
    "--target_date", $TARGET,
    "--holdings_csv", $holdingsResolved,
    "--out_v", "$ACTION_V",
    "--save_candidates"
) $manifest

$execConfig = Find-FirstExisting @(
    ".\\configs\\execution.yaml",
    ".\\configs\\execution_config.yaml",
    ".\\configs\\strategy_config.yaml",
    ".\\configs\\config.yaml"
)
if(-not $execConfig){
    throw "No execution config yaml found under configs/."
}

$actionsPath = "data/live/actions/live_actions__asof=${ASOF}__metric=${METRIC}__strat=${STRAT}__target=${TARGET}__v=${ACTION_V}.csv"
Invoke-Step "make_execution_plan" "scripts\\live\\make_execution_plan.py" @(
    "--actions_csv", $actionsPath,
    "--total_capital", "$([int][math]::Round($TOTAL_VALUE,0))",
    "--config", $execConfig,
    "--out_v", "$EXEC_V",
    "--asof", $ASOF,
    "--metric", $METRIC,
    "--ret_v", "$RET_V",
    "--price_date", $TARGET
) $manifest

$benchmarkPath = "data\\processed\\benchmark__ticker=${BenchmarkTicker}__target=${TARGET}.csv"
Invoke-Step "build_benchmark" "scripts\\live\\build_benchmark_kosdaq150.py" @(
    "--start", $PrevRebalDate,
    "--end", $PerfEnd,
    "--ticker", $BenchmarkTicker,
    "--benchmark_name", $BenchmarkName,
    "--output_csv", $benchmarkPath
) $manifest

$pricesDailyPath = Find-FirstExisting @(
    "data\\processed\\prices_daily__src=pykrx__start=20110101__asof=${ASOF}__metric=${METRIC}__v=${RET_V}.parquet",
    "data\\processed\\prices_daily__src=pykrx__start=20160101__asof=${ASOF}__metric=${METRIC}__v=${RET_V}.parquet"
)
if(-not $pricesDailyPath){
    throw "prices_daily parquet not found for asof=$ASOF metric=$METRIC ret_v=$RET_V"
}

$perfTag = "asof=${ASOF}__target=${TARGET}"
Invoke-Step "calc_live_performance" "scripts\\live\\calc_live_performance.py" @(
    "--holdings_csv", $perfHoldingsResolved,
    "--prices_daily", $pricesDailyPath,
    "--total_capital", "$TOTAL_VALUE",
    "--capital_mode", "total",
    "--target_date", $PrevRebalDate,
    "--end_date", $PerfEnd,
    "--benchmark", $benchmarkPath,
    "--benchmark_name", $BenchmarkName,
    "--output_dir", "data/live/performance",
    "--tag", $perfTag
) $manifest

$scoresPath = "data/live/scores/latest_scores__asof=${ASOF}__metric=${METRIC}__strat=${STRAT}__featv=${FEAT_V}__target=${TARGET}__full_universe.csv"
$featuresLivePath = "data/features/features_live/features_live__asof=${ASOF}__metric=${METRIC}__v=${FEAT_V}.parquet"
$execPath = "data/processed/execution_plan__total=$([int][math]::Round($TOTAL_VALUE,0))__v=${EXEC_V}.csv"
$reportMd = "data/live/reports/rebalance_report__asof=${ASOF}__target=${TARGET}__metric=${METRIC}__strat=${STRAT}__v=${REPORT_V}.md"
$reportHtml = "data/live/reports/rebalance_report__asof=${ASOF}__target=${TARGET}__metric=${METRIC}__strat=${STRAT}__v=${REPORT_V}.html"
$reportDetail = "data/live/reports/rebalance_report_detail__asof=${ASOF}__target=${TARGET}__metric=${METRIC}__strat=${STRAT}__v=${REPORT_V}.csv"
$perfSummarySource = "data/live/performance/live_performance_summary__$perfTag.json"
$perfDailySource = "data/live/performance/live_performance_daily__$perfTag.csv"
$perfContribSource = "data/live/performance/live_contribution__$perfTag.csv"

Invoke-Step "generate_rebalance_report" "scripts\\live\\generate_rebalance_report.py" @(
    "--actions_csv", $actionsPath,
    "--features_live", $featuresLivePath,
    "--execution_plan", $execPath,
    "--strategy", $STRAT,
    "--asof", $ASOF,
    "--target_date", $TARGET,
    "--metric", $METRIC,
    "--output_md", $reportMd,
    "--output_csv", $reportDetail,
    "--output_html", $reportHtml,
    "--scores_csv", $scoresPath,
    "--performance_summary", $perfSummarySource,
    "--performance_daily", $perfDailySource,
    "--performance_contrib", $perfContribSource,
    "--performance_title", "직전 리밸런싱 이후 성과 평가",
    "--report_title", "Factor Composite Rebalance Report"
) $manifest

$perfSummaryOut = "data/live/performance/performance_summary__asof=${ASOF}__target=${TARGET}.json"
$perfDailyOut = "data/live/performance/performance_daily__asof=${ASOF}__target=${TARGET}.csv"
$perfContribOut = "data/live/performance/performance_contrib__asof=${ASOF}__target=${TARGET}.csv"
Stage-Copy $perfSummarySource $perfSummaryOut | Out-Null
Stage-Copy $perfDailySource $perfDailyOut | Out-Null
Stage-Copy $perfContribSource $perfContribOut | Out-Null

$krxMetaPath = "data/processed/krx_master__asof=${ASOF}__src=pykrx__v=1.meta.json"
if(Test-Path $krxMetaPath){
    $krxMeta = Get-Content $krxMetaPath -Raw | ConvertFrom-Json
    $manifest.provisional = [bool]$krxMeta.provisional
    $manifest.provisional_source = $krxMeta.source
}

$kpiSnapshotPath = "data/live/kpi_snapshots/kpi_snapshot__asof=${ASOF}__target=${TARGET}.csv"
$kpiArgs = @(
    "--asof", $ASOF,
    "--target", $TARGET,
    "--metric", $METRIC,
    "--strategy", $STRAT,
    "--total_capital", "$TOTAL_VALUE",
    "--performance_summary", $perfSummaryOut,
    "--actions_csv", $actionsPath,
    "--execution_plan", $execPath,
    "--output_csv", $kpiSnapshotPath,
    "--provisional_source", "$($manifest.provisional_source)"
)
if($manifest.provisional){
    $kpiArgs += "--provisional"
}
Invoke-Step "save_kpi_snapshot" "scripts\\live\\save_kpi_snapshot.py" $kpiArgs $manifest

$manifest.outputs.features_live = $featuresLivePath
$manifest.outputs.scores_csv = $scoresPath
$manifest.outputs.actions_csv = $actionsPath
$manifest.outputs.execution_plan = $execPath
$manifest.outputs.performance_summary = $perfSummaryOut
$manifest.outputs.performance_daily = $perfDailyOut
$manifest.outputs.performance_contrib = $perfContribOut
$manifest.outputs.report_md = $reportMd
$manifest.outputs.report_html = $reportHtml
$manifest.outputs.report_detail_csv = $reportDetail
$manifest.outputs.kpi_snapshot = $kpiSnapshotPath

Write-Json $manifestPath $manifest

$summaryLines = New-Object System.Collections.Generic.List[string]
$summaryLines.Add("# factor_composite pipeline summary")
$summaryLines.Add("")
$summaryLines.Add("- asof: **$ASOF**")
$summaryLines.Add("- target: **$TARGET**")
$summaryLines.Add("- metric: **$METRIC**")
$summaryLines.Add("- strategy: **$STRAT**")
$summaryLines.Add("- provisional: **$($manifest.provisional)**")
$summaryLines.Add("- fallback source: **$($manifest.provisional_source)**")
$summaryLines.Add("")
$summaryLines.Add("## Outputs")
$summaryLines.Add("")
foreach($k in $manifest.outputs.Keys){
    $summaryLines.Add(("- {0}: ``{1}``" -f $k, $manifest.outputs[$k]))
}
$summaryLines.Add("")
$summaryLines.Add("## Steps")
$summaryLines.Add("")
foreach($s in $manifest.steps){
    $summaryLines.Add("- $($s.name): exit_code=$($s.exit_code), duration_sec=$($s.duration_sec)")
}
Set-Content -Path $summaryPath -Value ($summaryLines -join [Environment]::NewLine) -Encoding UTF8

Write-Host "[OK] manifest : $manifestPath"
Write-Host "[OK] summary  : $summaryPath"
