# startAll.ps1 — run KorASR in this PowerShell window (for debugging; Ctrl+C stops it).
#
# Everyday use: service-start.bat / service-stop.bat / service-status.bat / service-logs.bat.
# The settings come from scripts\service-env.cmd, so both ways use the same database,
# models and PATH. start.py rebuilds the frontend itself when its sources are newer.

$ErrorActionPreference = 'Stop'
$ProjDir = $PSScriptRoot
Set-Location $ProjDir

# Import the environment that scripts\service-env.cmd sets up.
$envLines = cmd /c "call `"$ProjDir\scripts\service-env.cmd`" && set"
foreach ($line in $envLines) {
    $name, $value = $line -split '=', 2
    if ($name -and $null -ne $value) { Set-Item -Path "Env:$name" -Value $value }
}

if (-not (Test-Path $env:KORASR_PY)) {
    Write-Host "[startAll] Python not found: $env:KORASR_PY (edit scripts\service-env.cmd)" -ForegroundColor Red
    exit 1
}
$listener = Get-NetTCPConnection -LocalPort $env:KORASR_PORT -State Listen -ErrorAction SilentlyContinue
if ($listener) {
    Write-Host "[startAll] Port $env:KORASR_PORT is in use (PID $($listener.OwningProcess)). Run service-stop.bat first." -ForegroundColor Yellow
    exit 1
}

Write-Host "[startAll] KorASR in the foreground: https://localhost:$($env:KORASR_PORT)  (Ctrl+C to stop)"
& $env:KORASR_PY "$ProjDir\start.py" --host $env:KORASR_HOST --port $env:KORASR_PORT
