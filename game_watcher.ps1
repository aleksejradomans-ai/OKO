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

    Candidate logging (CANDIDATE_CPU, 2026-10-25): a process becomes a
    candidate when it is not in games.txt, not in the name allowlist, its
    executable is outside the OS paths below, and it uses at least
    $CandidateCpuThreshold % of ONE logical core for $CandidateHotPolls
    consecutive polls. It is logged once to <repo>\games_candidates.log and
    offered to the operator in Telegram (Da/Net). Nothing gets added to
    games.txt by itself. Before 2026-10-25 the criterion was MainWindowTitle,
    which is always empty for a service in Session 0 (LocalSystem) - no
    candidate was ever produced while running as the OkoGameWatcher service.

    Only OkoBot is stopped, not OkoWatcher - GPU is used exclusively by
    Ollama inference inside bot.py. Verified live on 2026-10-03: loading
    qwen2.5:14b-instruct-q4_K_M used 6299MiB VRAM; keep_alive=0 returned
    it to 51MiB baseline.

    WATCHER_LOG (2026-10-25): every event goes to <repo>\logs\game_watcher.log
    (appended and closed per line, UTF-8 without BOM, OkoBot log format
    "yyyy-MM-dd HH:mm:ss,fff [LEVEL] game_watcher: [TAG] ..."), so the lines
    carry a date and can be parsed by sleep_digest/HUD. Write-Host is kept
    and does reach the NSSM log (OkoGameWatcher.stdout.log) immediately.
    START_RETRY: if Start-Service fails, the bot stays "suspended" and the
    start is retried on the next poll (before: flag reset -> bot stayed down).
    Failures are logged on attempts 1-3, then every 25th (no log flood).

    SUSPEND_FLAG (W4, 2026-10-25): "the watcher stopped OkoBot" is persisted in
    <repo>\data\game_mode.flag (written on suspend, removed after a successful
    start). On watcher start the flag restores $botSuspended, so a restart of
    this service during or after a game no longer leaves OkoBot down forever.
    Without the flag the watcher does NOT start a stopped OkoBot: a manual stop
    by the operator (maintenance, Disabled service) is respected.
#>

$ErrorActionPreference = "Continue"

# PUBLISH_PREP: repo root = folder of this script (run with -File).
$RepoRoot          = $(if ($PSScriptRoot) { $PSScriptRoot } else { (Get-Location).Path })
$ServiceToStop     = "OkoBot"
$PollIntervalSec   = 12
$GamesFile         = Join-Path $RepoRoot "games.txt"
$CandidatesLog     = Join-Path $RepoRoot "games_candidates.log"
$CandidateCpuThreshold = 20     # % of ONE logical core, averaged over one poll interval
$CandidateHotPolls     = 2      # consecutive polls above the threshold
$CandidateExcludedPathPrefixes = @(
    $env:windir,
    $(if ($env:ProgramFiles) { Join-Path $env:ProgramFiles "WindowsApps" }),
    $(if ($env:ProgramFiles) { Join-Path $env:ProgramFiles "Common Files" }),
    $(if (${env:ProgramFiles(x86)}) { Join-Path ${env:ProgramFiles(x86)} "Common Files" }),
    $(if ($env:ProgramData) { Join-Path $env:ProgramData "Microsoft" })
) | Where-Object { $_ }
$script:CpuPrev  = @{}
$script:HotPolls = @{}
$LogDir            = Join-Path $RepoRoot "logs"
$LogFile           = Join-Path $LogDir "game_watcher.log"
$LogMaxBytes       = 5MB
$Utf8NoBom         = New-Object System.Text.UTF8Encoding $false
$SuspendFlag       = Join-Path (Join-Path $RepoRoot "data") "game_mode.flag"

function Write-Log {
    param([string]$Message, [string]$Level = "INFO")
    $line = "{0} [{1}] game_watcher: {2}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss,fff"), $Level, $Message
    try {
        if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }
        if ((Test-Path $LogFile) -and ((Get-Item $LogFile).Length -gt $LogMaxBytes)) {
            Move-Item -Path $LogFile -Destination "$LogFile.1" -Force
        }
        [System.IO.File]::AppendAllText($LogFile, $line + "`r`n", $Utf8NoBom)
    } catch {
        # logging must never stop the watcher loop
    }
    Write-Host $line
}

$KnownNonGameNames = @(
    "dwm", "LockApp", "CrossDeviceResume", "PhoneExperienceHost",
    "SearchHost", "StartMenuExperienceHost", "WidgetBoard",
    "EdgeGameAssist", "msedgewebview2", "TextInputHost",
    "python", "nssm", "powershell", "pwsh", "explorer",
    "svchost", "System", "Idle", "llama-server", "ollama",
    "SearchIndexer", "RuntimeBroker", "ShellExperienceHost",
    "ApplicationFrameHost", "conhost", "WindowsTerminal",
    "pythonw", "msedge", "chrome", "firefox", "Discord", "Telegram",
    "WhatsApp.Root", "steam", "steamwebhelper", "EpicGamesLauncher",
    "EpicWebHelper", "Code", "Taskmgr", "MsMpEng", "NisSrv",
    "NVIDIA Share", "nvcontainer", "NVDisplay.Container", "ollama app"
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
        Write-Log "[GAME_CANDIDATE] notify=skip reason=no_token_or_admins name=$ProcessName" "WARNING"
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
            Write-Log "[GAME_CANDIDATE] notify=sent name=$ProcessName"
        } catch {
            Write-Log "[GAME_CANDIDATE] notify=fail name=$ProcessName err=$($_.Exception.GetType().Name)" "WARNING"
        }
    }
}

function Test-ExcludedPath {
    param([string]$Path)
    foreach ($prefix in $CandidateExcludedPathPrefixes) {
        if ($Path.StartsWith($prefix.TrimEnd('\') + '\', [System.StringComparison]::OrdinalIgnoreCase)) {
            return $true
        }
    }
    return $false
}

function Write-CandidateIfNew {
    param([string]$ProcessName, [int]$CpuPct = -1)

    if (Test-Path $CandidatesLog) {
        $already = Select-String -Path $CandidatesLog -Pattern "^$([regex]::Escape($ProcessName))$" -Quiet -ErrorAction SilentlyContinue
        if ($already) { return }
    }
    Add-Content -Path $CandidatesLog -Value $ProcessName -Encoding ASCII
    Write-Log "[GAME_CANDIDATE] new name=$ProcessName cpu_pct=$CpuPct (review $CandidatesLog)"
    Send-GameCandidateNotification -ProcessName $ProcessName
}

function Update-GameCandidates {
    param([string[]]$KnownGameNames)

    $allowlist = $KnownNonGameNames + $KnownGameNames
    $now = Get-Date
    $seen = @{}
    try {
        foreach ($p in (Get-Process -ErrorAction SilentlyContinue)) {
            if ($p.Name -in $allowlist) { continue }
            $path = $null
            $cpu = $null
            try {
                $path = $p.Path
                $cpu = $p.TotalProcessorTime.TotalSeconds
            } catch {
                continue   # protected process: no access to path/CPU
            }
            if (-not $path -or $null -eq $cpu) { continue }
            if (Test-ExcludedPath -Path $path) { continue }
            $key = [string]$p.Id
            $seen[$key] = $true
            $prev = $script:CpuPrev[$key]
            $script:CpuPrev[$key] = @{ Cpu = $cpu; At = $now }
            if (-not $prev) { continue }
            $dt = ($now - $prev.At).TotalSeconds
            if ($dt -le 0) { continue }
            $pct = [int][math]::Round(100 * ($cpu - $prev.Cpu) / $dt)
            if ($pct -ge $CandidateCpuThreshold) {
                $script:HotPolls[$key] = 1 + [int]$script:HotPolls[$key]
                if ($script:HotPolls[$key] -ge $CandidateHotPolls) {
                    Write-CandidateIfNew -ProcessName $p.Name -CpuPct $pct
                }
            } else {
                $script:HotPolls.Remove($key)
            }
        }
        foreach ($k in @($script:CpuPrev.Keys)) {
            if (-not $seen.ContainsKey($k)) {
                $script:CpuPrev.Remove($k)
                $script:HotPolls.Remove($k)
            }
        }
    } catch {
        # non-fatal - candidate detection is best-effort, but no longer silent
        Write-Log "[GAME_CANDIDATE] scan=fail err=$($_.Exception.GetType().Name)" "WARNING"
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
        Write-Log "[GAME_MODE] vram_unload=ok model=$OllamaModel done_reason=$($result.done_reason)"
    } catch {
        Write-Log "[GAME_MODE] vram_unload=fail model=$OllamaModel err=$($_.Exception.GetType().Name)" "WARNING"
    }
}

Write-Log "[GAME_WATCHER] started games=$GamesFile poll_s=$PollIntervalSec service=$ServiceToStop model=$OllamaModel candidate_cpu_pct=$CandidateCpuThreshold hot_polls=$CandidateHotPolls"

function Set-SuspendFlag {
    param([string]$Game)
    try {
        $dir = Split-Path $SuspendFlag -Parent
        if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
        $stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
        [System.IO.File]::WriteAllText($SuspendFlag, "$stamp game=$Game`r`n", $Utf8NoBom)
    } catch {
        Write-Log "[GAME_MODE] flag=write_fail err=$($_.Exception.GetType().Name)" "WARNING"
    }
}

function Clear-SuspendFlag {
    try {
        if (Test-Path $SuspendFlag) { Remove-Item -Path $SuspendFlag -Force }
    } catch {
        Write-Log "[GAME_MODE] flag=remove_fail err=$($_.Exception.GetType().Name)" "WARNING"
    }
}

# SUSPEND_FLAG: restore "bot stopped by watcher" across watcher restarts.
$botSuspended = Test-Path $SuspendFlag
if ($botSuspended) {
    $flagText = ""
    try { $flagText = ([System.IO.File]::ReadAllText($SuspendFlag)).Trim() } catch { }
    Write-Log "[GAME_WATCHER] resume suspended=1 flag=($flagText) - OkoBot will be started when no game is running"
}
$startFailures = 0

while ($true) {
    $gameNames = Get-GameList
    $detectedGame = Test-GameRunning -GameNames $gameNames

    if ($detectedGame -and -not $botSuspended) {
        Write-Log "[GAME_MODE] on game=$detectedGame - stopping $ServiceToStop"
        try {
            Stop-Service -Name $ServiceToStop -Force -ErrorAction Stop
            Write-Log "[GAME_MODE] service=$ServiceToStop stopped"
        } catch {
            Write-Log "[GAME_MODE] stop=fail service=$ServiceToStop err=$($_.Exception.GetType().Name) (administrator?)" "WARNING"
        }
        Unload-OllamaModel
        Set-SuspendFlag -Game $detectedGame
        $botSuspended = $true
    }
    elseif (-not $detectedGame -and $botSuspended) {
        if ($startFailures -eq 0) { Write-Log "[GAME_MODE] off - starting $ServiceToStop" }
        try {
            Start-Service -Name $ServiceToStop -ErrorAction Stop
            Write-Log "[GAME_MODE] service=$ServiceToStop started (attempts=$($startFailures + 1))"
            Clear-SuspendFlag
            $botSuspended = $false
            $startFailures = 0
        } catch {
            # START_RETRY: stay suspended -> this branch runs again on the next poll
            $startFailures++
            if ($startFailures -le 3 -or ($startFailures % 25) -eq 0) {
                Write-Log "[GAME_MODE] start=fail n=$startFailures service=$ServiceToStop err=$($_.Exception.GetType().Name) retry_in_s=$PollIntervalSec" "WARNING"
            }
        }
    }

    if (-not $detectedGame) {
        Update-GameCandidates -KnownGameNames $gameNames
    }

    Start-Sleep -Seconds $PollIntervalSec
}
