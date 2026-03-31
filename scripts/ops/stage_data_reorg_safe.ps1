param(
    [string]$ProjectRoot = "C:\Users\chanw\ai_inv_adv",
    [switch]$WhatIfMode
)

$ErrorActionPreference = "Stop"

Set-Location $ProjectRoot

$dataRoot = Join-Path $ProjectRoot "data"
$processedRoot = Join-Path $dataRoot "processed"

if (-not (Test-Path $processedRoot)) {
    throw "processed folder not found: $processedRoot"
}

$logDir = Join-Path $ProjectRoot "logs\data_reorg"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

$ts = Get-Date -Format "yyyyMMdd_HHmmss"
$manifestCsv = Join-Path $logDir "data_reorg_manifest__$ts.csv"
$skipCsv     = Join-Path $logDir "data_reorg_skipped__$ts.csv"
$failCsv     = Join-Path $logDir "data_reorg_failed__$ts.csv"

$results = New-Object System.Collections.Generic.List[object]
$skips   = New-Object System.Collections.Generic.List[object]
$fails   = New-Object System.Collections.Generic.List[object]

function Ensure-Dir([string]$Path) {
    New-Item -ItemType Directory -Force -Path $Path | Out-Null
}

function Copy-Safely([string]$SourcePath, [string]$DestDir) {
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
            }
            else {
                $destPath = Join-Path $DestDir ("DUP__" + $fileName)
            }
        }

        if ($WhatIfMode) {
            $results.Add([pscustomobject]@{
                source = $SourcePath
                destination = $destPath
                status = "whatif"
            })
        }
        else {
            Copy-Item -Path $SourcePath -Destination $destPath -Force
            $results.Add([pscustomobject]@{
                source = $SourcePath
                destination = $destPath
                status = "copied"
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

function Resolve-Destination([string]$FileName) {
    switch -Regex ($FileName) {
        '^fundamentals_quarterly__.*\.(parquet|csv)$' {
            return Join-Path $dataRoot 'interim\fundamentals_quarterly'
        }
        '^fundamentals_merged__.*\.(parquet|csv)$' {
            return Join-Path $dataRoot 'interim\fundamentals_merged'
        }
        '^factors_ttm_acc2__.*\.(parquet|csv)$' {
            return Join-Path $dataRoot 'features\factors_ttm_acc2'
        }
        '^features_live__.*\.parquet$' {
            return Join-Path $dataRoot 'features\features_live'
        }
        '^features_live__.*\.meta\.json$' {
            return Join-Path $dataRoot 'features\features_live'
        }
        '^latest_scores__.*\.(csv|json)$' {
            return Join-Path $dataRoot 'live\scores'
        }
        '^live_actions__.*\.csv$' {
            return Join-Path $dataRoot 'live\actions'
        }
        '^live_candidates__.*\.csv$' {
            return Join-Path $dataRoot 'live\candidates'
        }
        '^rebalance_report_detail__.*\.csv$' {
            return Join-Path $dataRoot 'live\reports'
        }
        '^rebalance_report__.*\.(html|md)$' {
            return Join-Path $dataRoot 'live\reports'
        }
        '^execution_plan__.*\.csv$' {
            return Join-Path $dataRoot 'live\execution'
        }
        '^prices_daily__.*\.parquet$' {
            return Join-Path $dataRoot 'interim\prices_daily'
        }
        '^prices_raw__.*\.parquet$' {
            return Join-Path $dataRoot 'raw\krx'
        }
        '^shares_industry__.*\.parquet$' {
            return Join-Path $dataRoot 'interim\shares_industry'
        }
        '^krx_marketdata__.*\.parquet$' {
            return Join-Path $dataRoot 'interim\marketdata'
        }
        '^krx_master__.*\.parquet$' {
            return Join-Path $dataRoot 'interim\marketdata'
        }
        '^universe__.*\.(parquet|csv)$' {
            return Join-Path $dataRoot 'interim\universe'
        }
        '^summary_reports__.*\.(csv|parquet)$' {
            return Join-Path $dataRoot 'backtest\summaries'
        }
        '^report__.*\.(csv|parquet|html|md|json)$' {
            return Join-Path $dataRoot 'backtest\reports'
        }
        '^rebalance_audit__.*\.(csv|parquet)$' {
            return Join-Path $dataRoot 'backtest\audits'
        }
        '^walk_forward__.*\.(csv|parquet|json)$' {
            return Join-Path $dataRoot 'backtest\walk_forward'
        }
        '^dart_financials__.*\.parquet$' {
            return Join-Path $dataRoot 'raw\dart'
        }
        '^dart_corp_list__.*\.parquet$' {
            return Join-Path $dataRoot 'raw\dart'
        }
        default {
            return Join-Path $dataRoot 'archive\deprecated_processed'
        }
    }
}

$targets = Get-ChildItem -Path $processedRoot -File

Write-Host ""
Write-Host "[INFO] processed files found: $($targets.Count)" -ForegroundColor Cyan
Write-Host "[INFO] WhatIfMode: $($WhatIfMode.IsPresent)" -ForegroundColor Cyan
Write-Host ""

foreach ($f in $targets) {
    $destDir = Resolve-Destination $f.Name
    Copy-Safely -SourcePath $f.FullName -DestDir $destDir
}

$results | Export-Csv -NoTypeInformation -Encoding UTF8 $manifestCsv
$skips   | Export-Csv -NoTypeInformation -Encoding UTF8 $skipCsv
$fails   | Export-Csv -NoTypeInformation -Encoding UTF8 $failCsv

Write-Host ""
Write-Host "[DONE] manifest : $manifestCsv" -ForegroundColor Green
Write-Host "[DONE] skipped  : $skipCsv" -ForegroundColor Yellow
Write-Host "[DONE] failed   : $failCsv" -ForegroundColor Red
Write-Host "[COUNT] copied/whatif = $($results.Count)" -ForegroundColor Green
Write-Host "[COUNT] skipped       = $($skips.Count)" -ForegroundColor Yellow
Write-Host "[COUNT] failed        = $($fails.Count)" -ForegroundColor Red
Write-Host ""
Write-Host "IMPORTANT: 아직 원본 삭제/이동은 하지 마." -ForegroundColor Magenta