param(
  [Parameter(Mandatory=$true)][string]$ASOF,
  [string]$Metric = "revenue_op",
  [switch]$DryRun,
  # KeepMode = "whitelist" (화이트리스트만 남김) | "bad_only" (.BAD만 지움)
  [ValidateSet("whitelist","bad_only")][string]$KeepMode = "whitelist"
)

$ErrorActionPreference = "Stop"

function Info($msg){ Write-Host "[INFO] $msg" }
function Ok($msg){ Write-Host "[OK]   $msg" }
function Dry($msg){ Write-Host "[DRY]  $msg" }

$root      = (Resolve-Path ".").Path
$processed = Join-Path $root "data\processed"
$archiveDir= Join-Path $root "data\_archive"
$ts        = Get-Date -Format "yyyyMMdd_HHmmss"
$zipPath   = Join-Path $archiveDir ("processed__asof={0}__metric={1}__archived_at={2}.zip" -f $ASOF,$Metric,$ts)

Info "root      = $root"
Info "processed = $processed"
Info "archive   = $zipPath"
Info "DryRun    = $DryRun"
Info "KeepMode  = $KeepMode"

if (!(Test-Path $processed)) {
  throw "processed folder not found: $processed"
}
New-Item -ItemType Directory -Force -Path $archiveDir | Out-Null

# 0) 먼저 백업(zip)
if ($DryRun) {
  Dry "Would compress: $processed -> $zipPath"
} else {
  if (Test-Path $zipPath) { Remove-Item -Force $zipPath }
  Compress-Archive -Path (Join-Path $processed "*") -DestinationPath $zipPath -Force
  Ok "Compressed processed -> $zipPath"
}

# 1) 깨진 파일(.BAD) 삭제
$badFiles = Get-ChildItem -Path $processed -File -Filter "*.BAD" -ErrorAction SilentlyContinue
if ($badFiles.Count -gt 0) {
  Info ("Found .BAD files: {0}" -f $badFiles.Count)
  foreach ($f in $badFiles) {
    if ($DryRun) { Dry "Would remove: $($f.FullName)" }
    else { Remove-Item -Force $f.FullName; Ok "Removed: $($f.Name)" }
  }
} else {
  Info "No .BAD files found."
}

if ($KeepMode -eq "bad_only") {
  Ok "KeepMode=bad_only done."
  exit 0
}

# 2) 화이트리스트(남길 파일 패턴)
# 오빠 현재 “실전 확정” 기준으로 최소 셋 + B(k=20) 비용스윕(v=41/43/45) 남기기
$keepPatterns = @(
  # 핵심 입력/가공
  "fundamentals_quarterly__asof=$ASOF__src=dart__fs=CFS__y=2016-2024__v=1.parquet",
  "universe__asof=$ASOF__metric=$Metric__v=1.csv",
  "factors_ttm_acc2__asof=$ASOF__metric=$Metric__v=2.parquet",
  "features_live__asof=$ASOF__metric=$Metric__v=2.parquet",

  # 가격/수익률
  "prices_daily__src=pykrx__start=20160101__asof=$ASOF__metric=$Metric__v=1.parquet",
  "returns_monthly__src=pykrx__asof=$ASOF__metric=$Metric__v=1.parquet",

  # 최종: B k=20 비용스윕 결과(41/43/45)
  "bt__asof=$ASOF__metric=$Metric__k=20__strat=B_growth_plus_quality__v=41.csv",
  "bt__asof=$ASOF__metric=$Metric__k=20__strat=B_growth_plus_quality__v=43.csv",
  "bt__asof=$ASOF__metric=$Metric__k=20__strat=B_growth_plus_quality__v=45.csv",

  "picks__asof=$ASOF__metric=$Metric__k=20__strat=B_growth_plus_quality__v=41.parquet",
  "picks__asof=$ASOF__metric=$Metric__k=20__strat=B_growth_plus_quality__v=43.parquet",
  "picks__asof=$ASOF__metric=$Metric__k=20__strat=B_growth_plus_quality__v=45.parquet",

  "report__asof=$ASOF__metric=$Metric__k=20__strat=B_growth_plus_quality__v=41.csv",
  "report__asof=$ASOF__metric=$Metric__k=20__strat=B_growth_plus_quality__v=43.csv",
  "report__asof=$ASOF__metric=$Metric__k=20__strat=B_growth_plus_quality__v=45.csv"

  # (옵션) 누락재무 필터 결과도 남기고 싶으면 주석 해제
  # "exclude_missing_fin__asof=$ASOF__lbq=8__minv=4__bad=0.5__metric=revenue_op__v=1.csv",
  # "keep_missing_fin__asof=$ASOF__lbq=8__minv=4__bad=0.5__metric=revenue_op__v=1.csv"
)

$keepSet = New-Object 'System.Collections.Generic.HashSet[string]'
foreach ($p in $keepPatterns) { [void]$keepSet.Add($p.ToLower()) }

$files = Get-ChildItem -Path $processed -File
$toDelete = @()

foreach ($f in $files) {
  $name = $f.Name.ToLower()
  if ($keepSet.Contains($name)) {
    continue
  }
  # whitelist 외 전부 삭제 후보
  $toDelete += $f
}

Info ("Whitelist keep count: {0}" -f $keepSet.Count)
Info ("Delete candidates: {0}" -f $toDelete.Count)

foreach ($f in $toDelete) {
  if ($DryRun) { Dry "Would remove: $($f.FullName)" }
  else { Remove-Item -Force $f.FullName; Ok "Removed: $($f.Name)" }
}

Ok "step7 archive & prune processed completed."
