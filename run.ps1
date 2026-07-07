# Launches TrackingBox and blackbox-runner together for local dev.
#
# Assumes TrackingBox lives in a sibling folder next to this repo (i.e. both
# under "Enter The Blackbox\"). Run from either repo's root - it finds the
# other by relative path.

$ErrorActionPreference = "Stop"

$RunnerRoot = $PSScriptRoot
$TrackingBoxRoot = Join-Path (Split-Path $RunnerRoot -Parent) "TrackingBox"

if (-not (Test-Path $TrackingBoxRoot)) {
    Write-Error "TrackingBox not found at $TrackingBoxRoot (expected as a sibling of $RunnerRoot)"
    exit 1
}

$trackingBoxExe = Join-Path $TrackingBoxRoot ".venv\Scripts\audience-tracker.exe"
$runnerPython = Join-Path $RunnerRoot ".venv\Scripts\python.exe"

Write-Host "Starting TrackingBox (serve --config config.json) in $TrackingBoxRoot ..."
$trackingBox = Start-Process -FilePath $trackingBoxExe `
    -ArgumentList "serve", "--config", "config.json" `
    -WorkingDirectory $TrackingBoxRoot `
    -PassThru -NoNewWindow

Write-Host "Waiting for TrackingBox's /api/zones to come up..."
$ready = $false
for ($i = 0; $i -lt 30; $i++) {
    try {
        Invoke-WebRequest -Uri "http://localhost:8000/api/zones" -UseBasicParsing -TimeoutSec 2 | Out-Null
        $ready = $true
        break
    } catch {
        Start-Sleep -Seconds 1
    }
}
if (-not $ready) {
    Write-Error "TrackingBox did not come up on http://localhost:8000 in time"
    Stop-Process -Id $trackingBox.Id -Force -ErrorAction SilentlyContinue
    exit 1
}

Write-Host "Starting blackbox-runner (uvicorn) in $RunnerRoot ..."
$runner = Start-Process -FilePath $runnerPython `
    -ArgumentList "-m", "uvicorn", "server.app:app", "--host", "0.0.0.0", "--port", "8100" `
    -WorkingDirectory $RunnerRoot `
    -PassThru -NoNewWindow

Write-Host "Both services are up. TrackingBox: http://localhost:8000  blackbox-runner: http://localhost:8100"
Write-Host "Press Ctrl+C to stop both."

try {
    Wait-Process -Id $trackingBox.Id, $runner.Id
} finally {
    Write-Host "Stopping services..."
    Stop-Process -Id $trackingBox.Id -Force -ErrorAction SilentlyContinue
    Stop-Process -Id $runner.Id -Force -ErrorAction SilentlyContinue
}
