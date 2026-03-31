param(
    [string]$ProjectRoot = "C:\Users\chanw\ai_inv_adv",
    [string]$KeepAsof = "2025-11-16",
    [string]$KeepFeatV = "311",
    [string]$KeepScoreTarget = "2025-11-30",
    [string]$KeepReportV = "12",
    [switch]$WhatIfMode
)

$ErrorActionPreference = "Stop"
Set-Location $ProjectRoot

$dataRoot = Join-Path $ProjectRoot "data"
$logDir = Join-Path $ProjectRoot "logs\archive_versions"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

$ts = Get-Date -Format "yyyyMMdd_HHmmss"
$manifestCsv = Join-Path $logDir "archive_versions_manifest__$ts.csv"
$skipCsv     = Join-Path $logDir "archive_versions_skipped__$ts.csv"
$failCsv     = Join-Path $logDir "archive_versions_failed__$ts.csv"

$results = New-Object System.Collections.Generic.List[object]
$skips   = New-Object System.Collections.Generic.List[object]
$fails   = New-Object System.Collections.Generic.List[object]

function Ensure-Dir([string]$Path) {
    New-Item -ItemType Directory -Force -Path $Path | Out-Null
}

function Move-Safely([string]$SourcePath, [string]$DestDir) {
    try {
        Ensure-Dir $DestDir
        $fileName = Split-Path $SourcePath -Leaf
        $destPath = Join-Path $DestDir $fileName

        if (Test-Path $destPath) {
            $srcInfo = Get-Item $SourcePath
            $dstInfo = Get-Item $destPath
            if ($srcInfo.Length -eq $dstInfo.Length) {
                $skips.Add([pscustomobject]@{
                    source = $SourcePath
                    destination = $destPath
                    reason = "already_exists_same_size"
                })
                return
            } else {
                $destPath = Join-Path $DestDir ("DUP__" + $fileName)
            }
        }

        if ($WhatIfMode) {
            $results.Add([pscustomobject]@{
                source = $SourcePath
                destination = $destPath
                status = "whatif"
            })
        } else {
            Move-Item -Path $SourcePath -Destination $destPath -Force
            $results.Add([pscustomobject]@{
                source = $SourcePath
                destination = $destPath
                status = "moved"
            })
        }
    }
    catch {
        $fails.Add([pscustomobject]@{
            source = $SourcePath
            destination = $DestDir
            error = $_.Exception.Message
        })
    }
}

function Should-Keep([System.IO.FileInfo]$File) {
    $name = $File.Name

    # KEEP: 실전 기준 features_live
    if ($name -match "^features_live__asof=$([regex]::Escape($KeepAsof))__metric=revenue_op__v=$([regex]::Escape($KeepFeatV))(\.meta\.json|\.parquet)$") {
        return $true
    }

    # KEEP: 실전 기준 latest_scores
    if ($name -match "^latest_scores__asof=$([regex]::Escape($KeepAsof))__metric=revenue_op__strat=D_quality_filter_debt_profitaccel_liq__featv=$([regex]::Escape($KeepFeatV))__target=$([regex]::Escape($KeepScoreTarget))__(coverage\.json|current_holdings_scored\.csv|full_universe\.csv|topk\.csv)$") {
        return $true
    }

    # KEEP: 실전 기준 report
    if ($name -match "^rebalance_report(__detail)?__asof=$([regex]::Escape($KeepAsof))__target=$([regex]::Escape($KeepScoreTarget))__metric=revenue_op__strat=D_quality_filter_debt_profitaccel_liq__v=$([regex]::Escape($KeepReportV))\.(html|md|csv)$") {
        return $true
    }

    # KEEP: 실전 기준 fundamentals
    if ($name -match "^fundamentals_quarterly__asof=$([regex]::Escape($KeepAsof))__src=dart_merged__fs=CFS__y=2015-2025__v=2\.parquet$") {
        return $true
    }

    # KEEP: 최신 실전 marketdata
    if ($name -match "^krx_marketdata__asof=$([regex]::Escape($KeepAsof))__src=pykrx__lookback=365d__v=1\.parquet$") {
        return $true
    }

    # KEEP: 실행계획
    if ($name -match "^execution_plan__.*\.csv$") {
        return $true
    }

    return $false
}

function Resolve-ArchiveDest([System.IO.FileInfo]$File) {
    $name = $File.Name

    if ($name -match "^features_live__") {
        return Join-Path $dataRoot "archive\old_versions\features_live"
    }
    if ($name -match "^latest_scores__") {
        return Join-Path $dataRoot "archive\old_versions\latest_scores"
    }
    if ($name -match "^rebalance_report") {
        return Join-Path $dataRoot "archive\old_versions\reports"
    }
    if ($name -match "^fundamentals_quarterly__") {
        return Join-Path $dataRoot "archive\old_versions\fundamentals_quarterly"
    }
    if ($name -match "^factors_ttm_acc2__") {
        return Join-Path $dataRoot "archive\old_versions\factors_ttm_acc2"
    }
    if ($name -match "^live_actions__") {
        return Join-Path $dataRoot "archive\old_versions\live_actions"
    }
    if ($name -match "^live_candidates__") {
        return Join-Path $dataRoot "archive\old_versions\live_candidates"
    }
    if ($name -match "^krx_marketdata__") {
        return Join-Path $dataRoot "archive\old_versions\marketdata"
    }
    if ($name -match "^universe__") {
        return Join-Path $dataRoot "archive\old_versions\universe"
    }
    return Join-Path $dataRoot "archive\old_versions\misc"
}

$targets = Get-ChildItem -Path $dataRoot -Recurse -File | Where-Object {
    $_.FullName -notmatch "\\archive\\"
}

Write-Host ""
Write-Host "[INFO] candidate files: $($targets.Count)" -ForegroundColor Cyan
Write-Host "[INFO] keep asof=$KeepAsof feat_v=$KeepFeatV report_v=$KeepReportV" -ForegroundColor Cyan
Write-Host "[INFO] WhatIfMode: $($WhatIfMode.IsPresent)" -ForegroundColor Cyan
Write-Host ""

foreach ($f in $targets) {
    if (Should-Keep $f) {
        $skips.Add([pscustomobject]@{
            source = $f.FullName
            destination = "-"
            reason = "keep_rule_matched"
        })
        continue
    }

    # archive 대상 패턴만 이동
    if ($f.Name -match "^(features_live__|latest_scores__|rebalance_report|fundamentals_quarterly__|factors_ttm_acc2__|live_actions__|live_candidates__|krx_marketdata__|universe__)") {
        $destDir = Resolve-ArchiveDest $f
        Move-Safely -SourcePath $f.FullName -DestDir $destDir
    }
}

$results | Export-Csv -NoTypeInformation -Encoding UTF8 $manifestCsv
$skips   | Export-Csv -NoTypeInformation -Encoding UTF8 $skipCsv
$fails   | Export-Csv -NoTypeInformation -Encoding UTF8 $failCsv

Write-Host ""
Write-Host "[DONE] manifest : $manifestCsv" -ForegroundColor Green
Write-Host "[DONE] skipped  : $skipCsv" -ForegroundColor Yellow
Write-Host "[DONE] failed   : $failCsv" -ForegroundColor Red
Write-Host "[COUNT] moved/whatif = $($results.Count)" -ForegroundColor Green
Write-Host "[COUNT] skipped      = $($skips.Count)" -ForegroundColor Yellow
Write-Host "[COUNT] failed       = $($fails.Count)" -ForegroundColor Red
Write-Host ""
Write-Host "IMPORTANT: archive 이동 후에도 아직 코드 경로는 안 바꾼 상태일 수 있음." -ForegroundColor Magenta