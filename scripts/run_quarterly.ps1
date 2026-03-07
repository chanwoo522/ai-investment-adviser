param(
  [string]$AsOf = (Get-Date -Format "yyyy-MM-dd"),
  [int]$StartYear = 2012
)

$ErrorActionPreference = "Stop"

$base = "C:\Users\chanw\ai_inv_adv"
$py   = "$base\.venv\Scripts\python.exe"

Write-Host "=== QUARTERLY RUN asof=$AsOf start_year=$StartYear ==="

& $py "$base\scripts\collect_dart_financials.py" --asof $AsOf --start_year $StartYear --freq y
& $py "$base\scripts\build_features_phase1.py" --asof $AsOf
& $py "$base\scripts\run_rolling_backtest.py" --asof $AsOf --start_year $StartYear --end_year 2022

Write-Host "=== QUARTERLY RUN DONE ==="
