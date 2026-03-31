param(
    [string]$RepoRoot = "."
)

$ErrorActionPreference = "Stop"
$repo = (Resolve-Path $RepoRoot).Path
$archiveDir = Join-Path $repo "scripts/archive"
$backtestDir = Join-Path $repo "scripts/backtest"

New-Item -ItemType Directory -Force -Path $backtestDir | Out-Null

$files = @(
    "backtest_quarterly_rebalance_v2.py",
    "run_rolling_backtest.py"
)

foreach ($name in $files) {
    $src = Join-Path $archiveDir $name
    $dst = Join-Path $backtestDir $name
    if (-not (Test-Path $src)) {
        throw "Missing archive file: $src"
    }
    Copy-Item $src $dst -Force
    Write-Host "[RESTORED] $dst"
}

Write-Host "[DONE] backtest files restored from scripts/archive -> scripts/backtest"
