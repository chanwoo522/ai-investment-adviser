param(
    [ValidateSet("fast","smart","refresh","dryrun")]
    [string]$Mode = "smart",

    [string]$ASOF = "2026-03-29",
    [string]$TARGET = "2026-03-31",
    [string]$METRIC = "revenue_op",
    [string]$STRAT = "D_quality_filter_debt_profitaccel_liq",

    [ValidateSet("production","sandbox")]
    [string]$RunScope = "sandbox",

    [string]$DataRoot = ".\data",

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
    [switch]$SkipReport,
    [switch]$SkipPerformance,
    [string]$PerformanceEndDate = "",
    [string]$BenchmarkName = "KOSDAQ150",
    [string]$BenchmarkTicker = "229200"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Resolve-Python {
    $cands = @(".\.venv\Scripts\python.exe", ".\.venv\bin\python", "python")
    foreach($c in $cands){
        if($c -eq "python"){ return $c }
        if(Test-Path $c){ return (Resolve-Path $c).Path }
    }
    return "python"
}

function Ensure-Dir([string]$Path) {
    if(!(Test-Path $Path)){ New-Item -ItemType Directory -Path $Path -Force | Out-Null }
}

function Assert-PathExists([string]$Path, [string]$Label) {
    if(-not $Path){ throw "$Label is empty" }
    if(!(Test-Path $Path)){ throw "$Label not found: $Path" }
}

function Find-FirstExisting([string[]]$Paths) {
    foreach($p in $Paths){
        if($p -and (Test-Path $p)){ return (Resolve-Path $p).Path }
    }
    return $null
}

function To-Date([string]$Ymd) {
    return [datetime]::ParseExact($Ymd, 'yyyy-MM-dd', $null)
}

function Get-TaggedVersion([string]$Name) {
    $m = [regex]::Match($Name, "__v=(\d+)")
    if($m.Success){ return [int]$m.Groups[1].Value }
    return -1
}

function Sanitize-Tag([string]$Text) {
    return (($Text -replace "[^A-Za-z0-9_-]", "_").ToLower())
}

function Build-PerformanceTag([string]$AsOf,[string]$Target,[int]$Version,[string]$EvalEnd) {
    return "asof=${AsOf}__target=${Target}__eval_end=${EvalEnd}__v=${Version}"
}

function Find-LatestMatchingFile {
    param([string]$Directory,[string]$Pattern,[switch]$Recurse)
    if(!(Test-Path $Directory)){ return $null }
    $items = if($Recurse){
        Get-ChildItem $Directory -File -Recurse -ErrorAction SilentlyContinue | Where-Object { $_.Name -like $Pattern }
    } else {
        Get-ChildItem $Directory -File -ErrorAction SilentlyContinue | Where-Object { $_.Name -like $Pattern }
    }
    $best = $items |
        Sort-Object @{Expression={ Get-TaggedVersion $_.Name }; Descending=$true}, @{Expression={ $_.LastWriteTimeUtc }; Descending=$true} |
        Select-Object -First 1
    if($best){ return $best.FullName }
    return $null
}

function Find-ProcessedByPrefixAsOf {
    param([string]$Prefix,[string]$AsOf,[string]$Ext = "parquet")
    $pat1 = "$Prefix" + "__asof=${AsOf}__*.$Ext"
    $x = Find-LatestMatchingFile -Directory (Join-Path $script:DataRootAbs "processed") -Pattern $pat1
    if($x){ return $x }
    $pat2 = "$Prefix" + "__asof=$AsOf`_*.$Ext"
    $x = Find-LatestMatchingFile -Directory (Join-Path $script:DataRootAbs "processed") -Pattern $pat2
    if($x){ return $x }
    return $null
}

function Find-Holdings {
    param([string]$Explicit)
    if($Explicit -and (Test-Path $Explicit)){ return (Resolve-Path $Explicit).Path }
    $paths = @(
        ".\current_portfolio\20260314_holdings_clean_manual.csv",
        ".\data\portfolio\current\current_holdings.csv",
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

function Run-Step {
    param([string]$Label,[string]$ScriptPath,[string[]]$StepArgs,[switch]$Optional,[switch]$DryRun)
    Assert-PathExists -Path $ScriptPath -Label "$Label script"
    Write-Host "[RUN][$Label] $script:PY $ScriptPath $($StepArgs -join ' ')"
    if($DryRun){ return $true }
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

function Find-LatestScoresCsv {
    param([string]$AsOf,[string]$Metric,[string]$Strategy,[int]$FeatV,[string]$TargetDate)
    $name = "latest_scores__asof=${AsOf}__metric=${Metric}__strat=${Strategy}__featv=${FeatV}__target=${TargetDate}__full_universe.csv"
    $paths = @(
        (Join-Path $script:ScopeScoresDir $name)
        (Join-Path (Join-Path $script:DataRootAbs "live\scores") $name)
        (Join-Path (Join-Path $script:DataRootAbs "processed") $name)
    )
    return Find-FirstExisting -Paths $paths
}

function Get-RunRoot {
    param([string]$Scope,[string]$AsOf,[string]$Target)
    if($Scope -eq "production"){
        return Join-Path $script:DataRootAbs ("production\runs\target=${Target}\asof=${AsOf}")
    }
    return Join-Path $script:DataRootAbs ("sandbox\target=${Target}\asof=${AsOf}")
}

function Initialize-RunDirs {
    param([string]$RunRoot)
    $dirs = @{
        inputs      = Join-Path $RunRoot "inputs"
        actions     = Join-Path $RunRoot "actions"
        scores      = Join-Path $RunRoot "scores"
        execution   = Join-Path $RunRoot "execution"
        reports     = Join-Path $RunRoot "reports"
        performance = Join-Path $RunRoot "performance"
        manifests   = Join-Path $RunRoot "manifests"
    }
    foreach($d in $dirs.Values){ Ensure-Dir $d }
    return $dirs
}

function Stage-Artifact {
    param([string]$SourcePath,[string]$DestinationDir,[switch]$DryRun)
    if(-not $SourcePath){ return $null }
    if(!(Test-Path $SourcePath)){ return $null }
    Ensure-Dir $DestinationDir
    $dest = Join-Path $DestinationDir (Split-Path $SourcePath -Leaf)
    if($DryRun){
        Write-Host "[DRYRUN][stage] $SourcePath -> $dest"
        return $dest
    }
    Copy-Item -LiteralPath $SourcePath -Destination $dest -Force
    return $dest
}

function Find-ScopedExecutionPlan {
    param([string]$RunRoot,[int]$Version,[double]$TotalValue)
    $rounded = [int][math]::Round($TotalValue,0)
    $p1 = Find-LatestMatchingFile -Directory (Join-Path $RunRoot "execution") -Pattern "*__v=${Version}.csv"
    if($p1){ return $p1 }
    $p2 = Join-Path (Join-Path $script:DataRootAbs "processed") ("execution_plan__total=${rounded}__v=${Version}.csv")
    if(Test-Path $p2){ return $p2 }
    return $null
}

function Find-PreviousCycle {
    param([string]$CurrentTarget,[string]$Metric,[string]$Strategy,[double]$TotalValue,[string]$Scope)
    if($Scope -ne "production"){ return $null }
    $root = Join-Path $script:DataRootAbs "production"
    if(!(Test-Path $root)){ return $null }

    $currentTargetDt = To-Date $CurrentTarget
    $items = Get-ChildItem $root -File -Recurse -Filter "live_actions__asof=*__metric=*__strat=*__target=*__v=*.csv" -ErrorAction SilentlyContinue
    $rows = @()

    foreach($it in $items){
        $name = $it.Name
        $m = [regex]::Match($name, '^live_actions__asof=(?<asof>\d{4}-\d{2}-\d{2})__metric=(?<metric>.+?)__strat=(?<strat>.+?)__target=(?<target>\d{4}-\d{2}-\d{2})__v=(?<v>\d+)\.csv$')
        if(-not $m.Success){ continue }
        if($m.Groups['metric'].Value -ne $Metric){ continue }
        if($m.Groups['strat'].Value -ne $Strategy){ continue }
        $targetVal = $m.Groups['target'].Value
        if((To-Date $targetVal) -ge $currentTargetDt){ continue }
        $v = [int]$m.Groups['v'].Value
        $actionDir = Split-Path $it.FullName -Parent
        $runRoot = Split-Path $actionDir -Parent
        $execPath = Find-ScopedExecutionPlan -RunRoot $runRoot -Version $v -TotalValue $TotalValue
        $rows += [pscustomobject]@{
            AsOf = $m.Groups['asof'].Value
            Target = $targetVal
            V = $v
            Actions = $it.FullName
            ExecPlan = $execPath
            RunRoot = $runRoot
        }
    }

    return ($rows | Sort-Object @{Expression={ To-Date $_.Target }; Descending=$true}, @{Expression={ $_.V }; Descending=$true} | Select-Object -First 1)
}

function Write-CycleManifest {
    param(
        [string]$ManifestDir,[string]$AsOf,[string]$Target,[string]$Metric,[string]$Strategy,[string]$Scope,
        [int]$Version,[string]$ExecutionPlan,[string]$ActionsCsv,[string]$ScoresCsv,[string]$PricesDaily,[double]$TotalValue
    )
    Ensure-Dir $ManifestDir
    $p = Join-Path $ManifestDir ("live_cycle__scope=${Scope}__asof=${AsOf}__target=${Target}__v=${Version}.json")
    $obj = [ordered]@{
        scope = $Scope
        asof = $AsOf
        target = $Target
        metric = $Metric
        strategy = $Strategy
        version = $Version
        execution_plan = $ExecutionPlan
        actions_csv = $ActionsCsv
        scores_csv = $ScoresCsv
        prices_daily = $PricesDaily
        total_capital = $TotalValue
        created_at = (Get-Date).ToString("s")
        status = if($Scope -eq "production") { "active" } else { "reference_only" }
    }
    ($obj | ConvertTo-Json -Depth 4) | Set-Content -Path $p -Encoding UTF8
    return $p
}

$script:PY = Resolve-Python
$script:DataRootAbs = (Resolve-Path $DataRoot).Path
$RunRoot = Get-RunRoot -Scope $RunScope -AsOf $ASOF -Target $TARGET
$Dirs = Initialize-RunDirs -RunRoot $RunRoot
$script:ScopeScoresDir = $Dirs.scores

# legacy dirs still used by underlying scripts
Ensure-Dir (Join-Path $script:DataRootAbs "live\reports")
Ensure-Dir (Join-Path $script:DataRootAbs "live\actions")
Ensure-Dir (Join-Path $script:DataRootAbs "live\candidates")
Ensure-Dir (Join-Path $script:DataRootAbs "live\scores")
Ensure-Dir (Join-Path $script:DataRootAbs "live\performance")
Ensure-Dir (Join-Path $script:DataRootAbs "live\manifests")
Ensure-Dir (Join-Path $script:DataRootAbs "processed")
Ensure-Dir (Join-Path $script:DataRootAbs "processed\benchmarks")

$holdings = Find-Holdings -Explicit $HOLDINGS_CSV
$config = Find-ExecutionConfig
$IsDryRun = ($Mode -eq "dryrun")

Write-Host "=== SMART LIVE REBALANCE START ==="
Write-Host "Mode=$Mode Scope=$RunScope ASOF=$ASOF TARGET=$TARGET METRIC=$METRIC STRAT=$STRAT"
Write-Host "[INFO] run_root  : $RunRoot"
Write-Host "[INFO] python    : $script:PY"
Write-Host "[INFO] holdings  : $holdings"

$fundExact = Find-ProcessedByPrefixAsOf -Prefix "fundamentals_quarterly" -AsOf $ASOF -Ext "parquet"
$marketExact = Find-ProcessedByPrefixAsOf -Prefix "krx_marketdata" -AsOf $ASOF -Ext "parquet"
$pricesExact = Find-ProcessedByPrefixAsOf -Prefix "prices_daily" -AsOf $ASOF -Ext "parquet"

$needPrepare = $false
if($Mode -eq "refresh"){ $needPrepare = $true }
elseif($Mode -eq "smart"){
    if((-not $fundExact) -or (-not $marketExact) -or (-not $pricesExact)){ $needPrepare = $true }
}

if($needPrepare){
    $prepareArgs = @("--asof",$ASOF,"--metric",$METRIC)
    if($Mode -eq "smart"){
        # smart는 기존 fundamentals를 최대한 재활용하고 부족한 분기만 채우도록 유도
        $prepareArgs += @("--fund_mode","missing_only")
    }

    Run-Step -Label "prepare_asof" -ScriptPath ".\scripts\data_pipeline\prepare_asof.py" -StepArgs $prepareArgs -DryRun:$IsDryRun

    $fundExact = Find-ProcessedByPrefixAsOf -Prefix "fundamentals_quarterly" -AsOf $ASOF -Ext "parquet"
    $marketExact = Find-ProcessedByPrefixAsOf -Prefix "krx_marketdata" -AsOf $ASOF -Ext "parquet"
    $pricesExact = Find-ProcessedByPrefixAsOf -Prefix "prices_daily" -AsOf $ASOF -Ext "parquet"
}

Run-Step -Label "build_universe" -ScriptPath ".\scripts\data_pipeline\build_universe.py" -StepArgs @("--asof",$ASOF,"--mcap_top",$McapTop,"--trd_bot",$TrdBot) -DryRun:$IsDryRun
Run-Step -Label "build_factors" -ScriptPath ".\scripts\data_pipeline\build_factors_ttm_acc2.py" -StepArgs @("--asof",$ASOF,"--metric",$METRIC,"--input_parquet",$fundExact,"--out_v",$FACTOR_V) -DryRun:$IsDryRun
Run-Step -Label "make_features_live" -ScriptPath ".\scripts\data_pipeline\make_features_live.py" -StepArgs @("--asof",$ASOF,"--metric",$METRIC,"--in_v",$FACTOR_V,"--out_v",$FEAT_V,"--save_meta") -DryRun:$IsDryRun
Run-Step -Label "score_latest_rebalance" -ScriptPath ".\scripts\live\score_latest_rebalance.py" -StepArgs @("--asof",$ASOF,"--metric",$METRIC,"--feat_v",$FEAT_V,"--strategy",$STRAT,"--target_date",$TARGET,"--holdings_csv",$holdings) -DryRun:$IsDryRun
Run-Step -Label "generate_live_actions" -ScriptPath ".\scripts\live\generate_live_actions.py" -StepArgs @("--asof",$ASOF,"--metric",$METRIC,"--strategy",$STRAT,"--feat_v",$FEAT_V,"--target_date",$TARGET,"--holdings_csv",$holdings,"--out_v",$ACTION_V,"--save_candidates") -DryRun:$IsDryRun

$legacyActionsCsv = Join-Path (Join-Path $script:DataRootAbs "live\actions") "live_actions__asof=${ASOF}__metric=${METRIC}__strat=${STRAT}__target=${TARGET}__v=${ACTION_V}.csv"
$actionsCsv = Stage-Artifact -SourcePath $legacyActionsCsv -DestinationDir $Dirs.actions -DryRun:$IsDryRun

$featuresLive = Join-Path $script:DataRootAbs "features\features_live\features_live__asof=${ASOF}__metric=${METRIC}__v=${FEAT_V}.parquet"
$featuresLiveScoped = Stage-Artifact -SourcePath $featuresLive -DestinationDir $Dirs.inputs -DryRun:$IsDryRun
$phase1 = Join-Path $script:DataRootAbs "features\features_phase1__asof=${ASOF}__src=phase1__v=1.parquet"
$phase1Scoped = Stage-Artifact -SourcePath $phase1 -DestinationDir $Dirs.inputs -DryRun:$IsDryRun

$scoreCsvLegacy = Find-LatestScoresCsv -AsOf $ASOF -Metric $METRIC -Strategy $STRAT -FeatV $FEAT_V -TargetDate $TARGET
$scoreCsv = Stage-Artifact -SourcePath $scoreCsvLegacy -DestinationDir $Dirs.scores -DryRun:$IsDryRun

$roundedTotal = [int][math]::Round($TOTAL_VALUE,0)
$legacyExecPlan = Join-Path (Join-Path $script:DataRootAbs "processed") "execution_plan__total=${roundedTotal}__v=${EXEC_V}.csv"
if((-not $SkipExecutionPlan) -and $config){
    Run-Step -Label "make_execution_plan" -ScriptPath ".\scripts\live\make_execution_plan.py" -StepArgs @("--actions_csv",$actionsCsv,"--total_capital",$TOTAL_VALUE,"--config",$config,"--out_v",$EXEC_V,"--asof",$ASOF,"--metric",$METRIC,"--ret_v",$RET_V,"--price_date",$TARGET) -Optional -DryRun:$IsDryRun
}
$execPlan = Stage-Artifact -SourcePath $legacyExecPlan -DestinationDir $Dirs.execution -DryRun:$IsDryRun

$reportBase = "rebalance_report__asof=${ASOF}__target=${TARGET}__metric=${METRIC}__strat=${STRAT}__v=${REPORT_V}"
$outputMd = Join-Path $Dirs.reports ($reportBase + ".md")
$outputCsv = Join-Path $Dirs.reports ("rebalance_report_detail__asof=${ASOF}__target=${TARGET}__metric=${METRIC}__strat=${STRAT}__v=${REPORT_V}.csv")
$outputHtml = Join-Path $Dirs.reports ($reportBase + ".html")

$perfEvalEnd = if([string]::IsNullOrWhiteSpace($PerformanceEndDate)){ $ASOF } else { $PerformanceEndDate }
$benchTag = Sanitize-Tag $BenchmarkName

$prevPerfSummary = $null
$prevPerfDaily = $null
$prevPerfContrib = $null
$prevPerfTitle = "직전 리밸런싱 이후 성과 평가"

if(-not $SkipPerformance){
    $prevCycle = Find-PreviousCycle -CurrentTarget $TARGET -Metric $METRIC -Strategy $STRAT -TotalValue $TOTAL_VALUE -Scope $RunScope
    if($prevCycle){
        $prevTargetDt = To-Date $prevCycle.Target
        $evalEndDt = To-Date $perfEvalEnd
        if($evalEndDt -ge $prevTargetDt -and $pricesExact -and $prevCycle.ExecPlan){
            $prevTag = Build-PerformanceTag -AsOf $prevCycle.AsOf -Target $prevCycle.Target -Version $prevCycle.V -EvalEnd $perfEvalEnd
            $prevBenchCsv = Join-Path (Join-Path $script:DataRootAbs "processed\benchmarks") "benchmark_${benchTag}__start=$($prevCycle.Target)__end=${perfEvalEnd}.csv"
            $prevPerfDir = Join-Path $prevCycle.RunRoot "performance"
            Ensure-Dir $prevPerfDir
            $prevPerfSummary = Join-Path $prevPerfDir "live_performance_summary__${prevTag}.json"
            $prevPerfDaily = Join-Path $prevPerfDir "live_performance_daily__${prevTag}.csv"
            $prevPerfContrib = Join-Path $prevPerfDir "live_contribution__${prevTag}.csv"

            if($IsDryRun){
                Run-Step -Label "build_benchmark_previous" -ScriptPath ".\scripts\live\build_benchmark_kosdaq150.py" -StepArgs @("--start",$prevCycle.Target,"--end",$perfEvalEnd,"--ticker",$BenchmarkTicker,"--benchmark_name",$BenchmarkName,"--output_csv",$prevBenchCsv) -Optional -DryRun:$true
                Run-Step -Label "calc_previous_live_performance" -ScriptPath ".\scripts\live\calc_live_performance.py" -StepArgs @("--execution_plan",$prevCycle.ExecPlan,"--prices_daily",$pricesExact,"--total_capital",$TOTAL_VALUE,"--target_date",$prevCycle.Target,"--end_date",$perfEvalEnd,"--benchmark",$prevBenchCsv,"--output_dir",$prevPerfDir,"--tag",$prevTag) -Optional -DryRun:$true
            } elseif(-not (Test-Path $prevPerfSummary)) {
                Run-Step -Label "build_benchmark_previous" -ScriptPath ".\scripts\live\build_benchmark_kosdaq150.py" -StepArgs @("--start",$prevCycle.Target,"--end",$perfEvalEnd,"--ticker",$BenchmarkTicker,"--benchmark_name",$BenchmarkName,"--output_csv",$prevBenchCsv) -Optional
                Run-Step -Label "calc_previous_live_performance" -ScriptPath ".\scripts\live\calc_live_performance.py" -StepArgs @("--execution_plan",$prevCycle.ExecPlan,"--prices_daily",$pricesExact,"--total_capital",$TOTAL_VALUE,"--target_date",$prevCycle.Target,"--end_date",$perfEvalEnd,"--benchmark",$prevBenchCsv,"--output_dir",$prevPerfDir,"--tag",$prevTag) -Optional
            }
        }
    }
}

if(-not $SkipReport){
    $argsReport = @(
        "--actions_csv",$actionsCsv,
        "--features_live",$featuresLiveScoped,
        "--strategy",$STRAT,
        "--asof",$ASOF,
        "--target_date",$TARGET,
        "--output_md",$outputMd,
        "--output_csv",$outputCsv,
        "--output_html",$outputHtml
    )
    if($execPlan){ $argsReport += @("--execution_plan",$execPlan) }
    if($phase1Scoped){ $argsReport += @("--supp_file",$phase1Scoped) }
    if($scoreCsv){ $argsReport += @("--scores_csv",$scoreCsv) }
    if($prevPerfSummary){
        $argsReport += @("--performance_summary",$prevPerfSummary)
        if($prevPerfDaily){ $argsReport += @("--performance_daily",$prevPerfDaily) }
        if($prevPerfContrib){ $argsReport += @("--performance_contrib",$prevPerfContrib) }
        $argsReport += @("--performance_title",$prevPerfTitle)
    }
    Run-Step -Label "generate_report" -ScriptPath ".\scripts\live\generate_rebalance_report.py" -StepArgs $argsReport -DryRun:$IsDryRun
}

$manifestPath = Write-CycleManifest -ManifestDir $Dirs.manifests -AsOf $ASOF -Target $TARGET -Metric $METRIC -Strategy $STRAT -Scope $RunScope -Version $REPORT_V -ExecutionPlan $execPlan -ActionsCsv $actionsCsv -ScoresCsv $scoreCsv -PricesDaily $pricesExact -TotalValue $TOTAL_VALUE
Write-Host "[OK] manifest : $manifestPath"
Write-Host "[OUT] actions  : $actionsCsv"
Write-Host "[OUT] scores   : $scoreCsv"
Write-Host "[OUT] exec     : $execPlan"
Write-Host "[OUT] reports  : $Dirs.reports"