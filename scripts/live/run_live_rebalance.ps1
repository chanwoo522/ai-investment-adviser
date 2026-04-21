
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

$rootScript = Join-Path (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path "run_full_live_rebalance.ps1"
if(!(Test-Path $rootScript)){ throw "root run_full_live_rebalance.ps1 not found: $rootScript" }

$args = @(
    "-Mode",$Mode,
    "-ASOF",$ASOF,
    "-TARGET",$TARGET,
    "-METRIC",$METRIC,
    "-STRAT",$STRAT,
    "-RunScope",$RunScope,
    "-DataRoot",$DataRoot,
    "-McapTop",$McapTop,
    "-TrdBot",$TrdBot,
    "-PX_V",$PX_V,
    "-RET_V",$RET_V,
    "-FACTOR_V",$FACTOR_V,
    "-FEAT_V",$FEAT_V,
    "-ACTION_V",$ACTION_V,
    "-EXEC_V",$EXEC_V,
    "-REPORT_V",$REPORT_V,
    "-TOTAL_VALUE",$TOTAL_VALUE,
    "-HOLDINGS_CSV",$HOLDINGS_CSV,
    "-PerformanceEndDate",$PerformanceEndDate,
    "-BenchmarkName",$BenchmarkName,
    "-BenchmarkTicker",$BenchmarkTicker
)
if($ProtectHoldings){ $args += "-ProtectHoldings" }
if($SkipExecutionPlan){ $args += "-SkipExecutionPlan" }
if($SkipReport){ $args += "-SkipReport" }
if($SkipPerformance){ $args += "-SkipPerformance" }

& powershell -ExecutionPolicy Bypass -File $rootScript @args
exit $LASTEXITCODE
