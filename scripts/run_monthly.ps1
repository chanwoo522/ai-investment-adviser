param(
  [string]$AsOf = (Get-Date -Format "yyyy-MM-dd")
)

$ErrorActionPreference = "Stop"

$base = "C:\Users\chanw\ai_inv_adv"
$py   = "$base\.venv\Scripts\python.exe"

Write-Host "=== MONTHLY RUN asof=$AsOf ==="

& $py "$base\scripts\collect_krx_master.py" --asof $AsOf
& $py "$base\scripts\collect_krx_marketdata.py" --asof $AsOf --lookback_days 365
& $py "$base\scripts\collect_prices.py" --asof $AsOf --freq d --lookback_years 15

& $py "$base\scripts\build_universe.py" --asof $AsOf --mcap_top_pct 0.70 --trd_bottom_pct 0.30 --cap_buckets 4 --min_peer 20
& $py "$base\scripts\build_features_phase1.py" --asof $AsOf
& $py "$base\scripts\report_snapshot.py" --asof $AsOf

Write-Host "=== MONTHLY RUN DONE ==="
