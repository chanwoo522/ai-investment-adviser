param(
    [string]$ProjectRoot = "C:\Users\chanw\ai_inv_adv",
    [switch]$WhatIfMode
)

$ErrorActionPreference = "Stop"
Set-Location $ProjectRoot

$rawDartRoot = Join-Path $ProjectRoot "data\raw\dart"
$archiveRoot = Join-Path $ProjectRoot "data\archive\raw_dart"
$logDir = Join-Path $ProjectRoot "logs\archive_raw_dart"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

$ts = Get-Date -Format "yyyyMMdd_HHmmss"
$manifestCsv = Join-Path $logDir "archive_raw_dart_manifest__$ts.csv"
$skipCsv     = Join-Path $logDir "archive_raw_dart_skipped__$ts.csv"
$failCsv     = Join-Path $logDir "archive_raw_dart_failed__$ts.csv"

$results = New-Object System.Collections.Generic.List[object]
$skips   = New-Object System.Collections.Generic.List[object]
$fails   = New-Object System.Collections.Generic.List[object]

if (-not (Test-Path $rawDartRoot)) {
    throw "raw dart folder not found: $rawDartRoot"
}

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

$targets = Get-ChildItem -Path $rawDartRoot -Recurse -File

Write-Host ""
Write-Host "[INFO] raw dart files: $($targets.Count)" -ForegroundColor Cyan
Write-Host "[INFO] WhatIfMode: $($WhatIfMode.IsPresent)" -ForegroundColor Cyan
Write-Host ""

foreach ($f in $targets) {
    Move-Safely -SourcePath $f.FullName -DestDir $archiveRoot
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
Write-Host "IMPORTANT: raw dart는 archive로만 이동, 삭제 아님." -ForegroundColor Magenta