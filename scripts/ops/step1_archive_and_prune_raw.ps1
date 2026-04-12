# scripts\ops\step1_archive_and_prune_raw.ps1
# 목적:
#  - data/raw/dart 를 zip으로 보관하고 (재현성/백업)
#  - 실전 작업 폴더에서는 raw를 제거해서 프로젝트를 가볍게 유지
#  - 작은/깨진 parquet(.BAD 등)도 같이 정리

param(
  [string]$ASOF = "2026-02-18",
  [switch]$DryRun
)

$ErrorActionPreference = "Stop"

$root = (Get-Location).Path
$dataRaw = Join-Path $root "data\raw"
$dartRaw = Join-Path $dataRaw "dart"

$archiveDir = Join-Path $root "data\_archive"
$stamp = (Get-Date -Format "yyyyMMdd_HHmmss")
$zipPath = Join-Path $archiveDir ("raw_dart__asof={0}__archived_at={1}.zip" -f $ASOF, $stamp)

Write-Host "[INFO] root      = $root"
Write-Host "[INFO] dartRaw   = $dartRaw"
Write-Host "[INFO] archive   = $zipPath"
Write-Host "[INFO] DryRun    = $DryRun"

if (!(Test-Path $dartRaw)) {
  Write-Host "[WARN] $dartRaw not found. Nothing to archive."
  exit 0
}

New-Item -ItemType Directory -Force -Path $archiveDir | Out-Null

# 1) raw/dart 압축 보관
if ($DryRun) {
  Write-Host "[DRY] Would compress: $dartRaw -> $zipPath"
} else {
  if (Test-Path $zipPath) { Remove-Item $zipPath -Force }
  Compress-Archive -Path $dartRaw -DestinationPath $zipPath -CompressionLevel Optimal
  Write-Host "[OK] Compressed raw dart -> $zipPath"
}

# 2) raw/dart 제거 (실전 폴더 슬림화)
if ($DryRun) {
  Write-Host "[DRY] Would remove folder: $dartRaw"
} else {
  Remove-Item $dartRaw -Recurse -Force
  Write-Host "[OK] Removed raw dart folder: $dartRaw"
}

# 3) (옵션) 너무 작은 parquet 파일 탐지해서 .BAD 처리 (주로 깨진 산출물)
#    기준: 4KB 미만 parquet를 BAD로 rename
$processed = Join-Path $root "data\processed"
if (Test-Path $processed) {
  $small = Get-ChildItem -Path $processed -Filter "*.parquet" -File -Recurse |
           Where-Object { $_.Length -lt 4096 }

  if ($small.Count -gt 0) {
    Write-Host "[INFO] Small parquet files (<4KB): $($small.Count)"
    foreach ($f in $small) {
      $bad = "$($f.FullName).BAD"
      if ($DryRun) {
        Write-Host "[DRY] Would rename: $($f.FullName) -> $bad"
      } else {
        Rename-Item -Path $f.FullName -NewName ([IO.Path]::GetFileName($bad)) -Force
        Write-Host "[OK] Renamed to BAD: $($f.FullName)"
      }
    }
  } else {
    Write-Host "[INFO] No small parquet files found."
  }
}

Write-Host "[DONE] step1 archive & prune raw completed."
