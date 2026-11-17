#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Installs Project Oko as two Windows services via NSSM:
    OkoBot (src\bot.py - Telegram bot, governor, Ollama) and
    OkoWatcher (src\run_system.py - staging/backup watcher).
#>
param(
    # PUBLISH_PREP: python.exe of the project environment (conda env or venv), required.
    [Parameter(Mandatory = $true)][string]$PythonExe
)

$ErrorActionPreference = "Stop"

$RepoRoot      = $PSScriptRoot
$NssmExe       = Join-Path $RepoRoot "nssm.exe"
$LogDir        = Join-Path $RepoRoot "logs"

$Services = @(
    @{
        Name        = "OkoBot"
        ScriptPath  = Join-Path $RepoRoot "src\bot.py"
        Description = "Project Oko - Telegram bot / governor / Ollama inference"
        StdoutLog   = Join-Path $LogDir "OkoBot.stdout.log"
        StderrLog   = Join-Path $LogDir "OkoBot.stderr.log"
    },
    @{
        Name        = "OkoWatcher"
        ScriptPath  = Join-Path $RepoRoot "src\run_system.py"
        Description = "Project Oko - staging/backup file watcher"
        StdoutLog   = Join-Path $LogDir "OkoWatcher.stdout.log"
        StderrLog   = Join-Path $LogDir "OkoWatcher.stderr.log"
    }
)

if (-not (Test-Path $NssmExe)) { throw "nssm.exe not found at $NssmExe" }
if (-not (Test-Path $PythonExe)) { throw "Python interpreter not found: $PythonExe" }
foreach ($svc in $Services) {
    if (-not (Test-Path $svc.ScriptPath)) { throw "Entry point not found: $($svc.ScriptPath)" }
}
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }

foreach ($svc in $Services) {
    $name = $svc.Name
    Write-Host "`n=== Setting up service: $name ===" -ForegroundColor Cyan

    $prevEAP = $ErrorActionPreference
    $ErrorActionPreference = "SilentlyContinue"
    $existing = & $NssmExe status $name 2>$null
    $ErrorActionPreference = $prevEAP
    if ($LASTEXITCODE -eq 0) {
        Write-Host "Service $name already exists - reinstalling."
        & $NssmExe stop $name confirm 2>$null | Out-Null
        Start-Sleep -Seconds 2
        & $NssmExe remove $name confirm 2>$null | Out-Null
    }

    & $NssmExe install $name $PythonExe "-u `"$($svc.ScriptPath)`""
    & $NssmExe set $name AppDirectory $RepoRoot
    & $NssmExe set $name DisplayName "Project Oko - $name"
    & $NssmExe set $name Description $svc.Description
    & $NssmExe set $name AppEnvironmentExtra "PYTHONPATH=$RepoRoot" "PYTHONUNBUFFERED=1"
    & $NssmExe set $name AppStdout $svc.StdoutLog
    & $NssmExe set $name AppStderr $svc.StderrLog
    & $NssmExe set $name AppRotateFiles 1
    & $NssmExe set $name AppRotateOnline 1
    & $NssmExe set $name AppRotateBytes 10485760
    & $NssmExe set $name AppExitAction Default
    & $NssmExe set $name AppRestartDelay 15000
    & $NssmExe set $name AppThrottle 10000
    & $NssmExe set $name AppNoConsole 1

    Write-Host "Service $name configured." -ForegroundColor Green
}

Write-Host "`n=== Starting services ===" -ForegroundColor Cyan
foreach ($svc in $Services) {
    & $NssmExe start $svc.Name
    Start-Sleep -Seconds 3
    $status = & $NssmExe status $svc.Name
    Write-Host "$($svc.Name): $status"
}

Write-Host "`nDone. Check logs:" -ForegroundColor Yellow
foreach ($svc in $Services) {
    Write-Host "  Get-Content `"$($svc.StderrLog)`" -Tail 30"
}

