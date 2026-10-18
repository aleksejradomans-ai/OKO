<#
.SYNOPSIS
    Lightweight background watcher: when a game process is detected, stops
    OkoBot (Telegram bot + Ollama inference) and unloads the model from
    VRAM; when the game closes, starts OkoBot back up.

.NOTES
    Game list lives in <repo>\games.txt (one process name per line, no
    .exe), not hardcoded here - so adding a new game does not require
    reinstalling the service. The file is re-read on every poll cycle.

    Unverified process names in games.txt are candidates, not confirmed -
    confirm against a real running game with:
      Get-Process | Where-Object { $_.MainWindowTitle -ne "" } |
        Select-Object Name, MainWindowTitle

    Candidate logging: any process using notable CPU that is not in
    games.txt and not in the OS/background allowlist gets logged once to
    <repo>\games_candidates.log for manual review - this is assistive,
    not automatic enrollment. Nothing gets added to games.txt by itself.

    Only OkoBot is stopped, not OkoWatcher - GPU is used exclusively by
    Ollama inference inside bot.py. Verified live on 2026-10-03: loading
    qwen2.5:14b-instruct-q4_K_M used 6299MiB VRAM; keep_alive=0 returned
    it to 51MiB baseline.
#>

$ErrorActionPreference = "Continue"

# PUBLISH_PREP: repo root = folder of this script (run with -File).
$RepoRoot          = $(if ($PSScriptRoot) { $PSScriptRoot } else { (Get-Location).Path })
$ServiceToStop     = "OkoBot"
$PollIntervalSec   = 12
$GamesFile         = Join-Path $RepoRoot "games.txt"
$CandidatesLog     = Join-Path $RepoRoot "games_candidates.log"
$CandidateCpuThreshold = 20

$KnownNonGameNames = @(
    "dwm", "LockApp", "CrossDeviceResume", "PhoneExperienceHost",
    "SearchHost", "StartMenuExperienceHost", "WidgetBoard",
    "EdgeGameAssist", "msedgewebview2", "TextInputHost",
    "python", "nssm", "powershell", "pwsh", "explorer",
    "svchost", "System", "Idle", "llama-server", "ollama",
    "SearchIndexer", "RuntimeBroker", "ShellExperienceHost",
    "ApplicationFrameHost", "conhost", "WindowsTerminal"
)

function Get-GameList {
    if (-not (Test-Path $GamesFile)) {
        return @()
    }
    Get-Content $GamesFile |
        Where-Object { $_ -and $_.Trim() -ne "" -and -not $_.Trim().StartsWith("#") } |
        ForEach-Object { $_.Trim() }
}

function Test-GameRunning {
    param([string[]]$GameNames)
    foreach ($name in $GameNames) {
        if (Get-Process -Name $name -ErrorAction SilentlyContinue) {
            return $name
        }
    }
    return $null
}

function Get-TelegramConfig {
    $tokenLine = Select-String -Path $envPath -Pattern "^TELEGRAM_TOKEN=" -ErrorAction SilentlyContinue
    $adminLine = Select-String -Path $envPath -Pattern "^ADMIN_USER_IDS=" -ErrorAction SilentlyContinue
    $token = if ($tokenLine) { ($tokenLine.Line -split "=", 2)[1].Trim() } else { $null }
    $adminIds = @()
    if ($adminLine) {
        $raw = ($adminLine.Line -split "=", 2)[1].Trim()
        $adminIds = $raw.Split(",") | ForEach-Object { $_.Trim() } | Where-Object { $_ -ne "" }
    }
    return @{ Token = $token; AdminIds = $adminIds }
}

function Send-GameCandidateNotification {
    param([string]$ProcessName)

    $tg = Get-TelegramConfig
    if (-not $tg.Token -or $tg.AdminIds.Count -eq 0) {
        Write-Warning "TELEGRAM_TOKEN or ADMIN_USER_IDS missing in .env - cannot notify."
        return
    }

    $row = @(
        @{ text = "Da"; callback_data = "game_add_$ProcessName" },
        @{ text = "Net"; callback_data = "game_ignore_$ProcessName" }
    )
    $keyboard = @{ inline_keyboard = @(,$row) }
    $text = "New process detected: $ProcessName`nAdd it to games.txt (auto-suspend OkoBot when running)?"

    foreach ($adminId in $tg.AdminIds) {
        $body = @{
            chat_id = $adminId
            text = $text
            reply_markup = $keyboard
        } | ConvertTo-Json -Depth 5

        try {
            Invoke-RestMethod -Uri "https://api.telegram.org/bot$($tg.Token)/sendMessage" -Method Post -Body $body -ContentType "application/json" -TimeoutSec 30 | Out-Null
            Write-Host "[$(Get-Date -Format 'HH:mm:ss')] Telegram notification sent for candidate: $ProcessName"
        } catch {
            Write-Warning "[$(Get-Date -Format 'HH:mm:ss')] Failed to send Telegram notification: $_"
        }
    }
}

function Write-CandidateIfNew {
    param([string]$ProcessName)

    if (Test-Path $CandidatesLog) {
        $already = Select-String -Path $CandidatesLog -Pattern "^$([regex]::Escape($ProcessName))$" -Quiet -ErrorAction SilentlyContinue
        if ($already) { return }
    }
    Add-Content -Path $CandidatesLog -Value $ProcessName -Encoding ASCII
    Write-Host "[$(Get-Date -Format 'HH:mm:ss')] New candidate process logged: $ProcessName (review $CandidatesLog)" -ForegroundColor Magenta
    Send-GameCandidateNotification -ProcessName $ProcessName
}

function Update-GameCandidates {
    param([string[]]$KnownGameNames)

    $allowlist = $KnownNonGameNames + $KnownGameNames
    try {
        $heavy = Get-Process -ErrorAction SilentlyContinue |
            Where-Object { $_.MainWindowTitle -ne "" -and $_.Name -notin $allowlist }
        foreach ($p in $heavy) {
            Write-CandidateIfNew -ProcessName $p.Name
        }
    } catch {
        # non-fatal - candidate detection is best-effort
    }
}

$OllamaHost  = "localhost"
$OllamaPort  = "11434"
$envPath = Join-Path $RepoRoot ".env"
if (Test-Path $envPath) {
    $hostLine = Select-String -Path $envPath -Pattern "^OLLAMA_HOST=" -ErrorAction SilentlyContinue
    if ($hostLine) { $OllamaHost = ($hostLine.Line -split "=", 2)[1].Trim() }
    $portLine = Select-String -Path $envPath -Pattern "^OLLAMA_PORT=" -ErrorAction SilentlyContinue
    if ($portLine) { $OllamaPort = ($portLine.Line -split "=", 2)[1].Trim() }
    $modelLine = Select-String -Path $envPath -Pattern "^OLLAMA_MODEL=" -ErrorAction SilentlyContinue
    if ($modelLine) { $OllamaModel = ($modelLine.Line -split "=", 2)[1].Trim() }
}
if (-not $OllamaModel) { $OllamaModel = "gemma2:latest" }
$OllamaUrl = "http://${OllamaHost}:${OllamaPort}/api/generate"

function Unload-OllamaModel {
    try {
        $body = @{ model = $OllamaModel; keep_alive = 0 } | ConvertTo-Json
        $result = Invoke-RestMethod -Uri $OllamaUrl -Method Post -Body $body -ContentType "application/json" -TimeoutSec 10
        Write-Host "[$(Get-Date -Format 'HH:mm:ss')] Ollama: model '$OllamaModel' unload requested (done_reason: $($result.done_reason))."
    } catch {
        Write-Warning "[$(Get-Date -Format 'HH:mm:ss')] Failed to unload model via Ollama API: $_"
    }
}

Write-Host "=== Oko Game Watcher started ===" -ForegroundColor Cyan
Write-Host "Game list source: $GamesFile (re-read every poll)"
Write-Host "Candidate log: $CandidatesLog"
Write-Host "Polling every $PollIntervalSec sec. Service under control: $ServiceToStop"
Write-Host "Ollama: $OllamaUrl (model: $OllamaModel)"

$botSuspended = $false

while ($true) {
    $gameNames = Get-GameList
    $detectedGame = Test-GameRunning -GameNames $gameNames

    if ($detectedGame -and -not $botSuspended) {
        Write-Host "[$(Get-Date -Format 'HH:mm:ss')] Game detected: $detectedGame.exe - suspending $ServiceToStop" -ForegroundColor Yellow
        try {
            Stop-Service -Name $ServiceToStop -Force -ErrorAction Stop
            Write-Host "[$(Get-Date -Format 'HH:mm:ss')] Service $ServiceToStop stopped."
        } catch {
            Write-Warning "Failed to stop service $ServiceToStop - is this script running as administrator? $_"
        }
        Unload-OllamaModel
        $botSuspended = $true
    }
    elseif (-not $detectedGame -and $botSuspended) {
        Write-Host "[$(Get-Date -Format 'HH:mm:ss')] Game closed - restoring $ServiceToStop" -ForegroundColor Green
        try {
            Start-Service -Name $ServiceToStop -ErrorAction Stop
            Write-Host "[$(Get-Date -Format 'HH:mm:ss')] Service $ServiceToStop started."
        } catch {
            Write-Warning "Failed to start service $ServiceToStop - check manually: nssm.exe status $ServiceToStop. $_"
        }
        $botSuspended = $false
    }

    if (-not $detectedGame) {
        Update-GameCandidates -KnownGameNames $gameNames
    }

    Start-Sleep -Seconds $PollIntervalSec
}




