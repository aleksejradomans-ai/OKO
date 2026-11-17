<#
.SYNOPSIS
    Game watcher v2 (GAME_MODE v2, 2026-11-02): while a game from games.txt is
    running, OkoBot keeps running (devices, TV, Garmin, print) but its brain
    sleeps, and every model is kept out of VRAM. OkoBot is NOT stopped anymore
    (decision X5 cancelled by the operator 2026-11-02).

.NOTES
    Contract with OkoBot (files in <repo>\data, exception from X4):
      game_mode.flag   - written by THIS script while a game runs, rewritten every
                         poll ("v2 since=<unix> game=<name> beat=<unix>"): its mtime
                         is the heartbeat. Bot: older than OKO_GAME_FLAG_STALE_S
                         (120 s) = watcher dead -> brain wakes (fail-open, R1).
      brain_force.flag - written by OkoBot on /force_brain ("v2 until=<unix>").
                         While valid this script does NOT unload models (P9).
                         Deleted by this script when the game session ends (P10).

    Transition safeguards (TZ_game_mode_v2_2026-11-02.md):
      P1 enter: flag first, then unload (the bot stops starting LLM calls first).
      P2 unload with verification: every poll in game GET /api/ps; every model
         with size_vram>0 (and the main OLLAMA_MODEL copy in RAM) gets
         keep_alive=0, then /api/ps again; "vram=free" logged once.
      P3 Ollama unreachable: flag stays, P2 retried every poll, WARNING on
         attempts 1-3 then every 25th.
      P4 leave: flag removed only after OKO_GAME_OFF_POLLS (default 2 = 24 s)
         polls in a row without a game; heartbeat continues meanwhile.
      P5 several games: game mode holds while any games.txt process runs.
      P6 heartbeat: see game_mode.flag above.
      P7 watcher restart: state restored from the v2 flag + running processes.
      P8 migration: a v1 flag (no "v2" prefix) means the old watcher stopped
         OkoBot -> one Start-Service if OkoBot is Stopped, then the flag is removed.
      Rollback: put back the v1 game_watcher.ps1 from _quarantine_* AND set
         OKO_GAME_MODE=0 in .env (otherwise the bot loads the model in game and
         this script unloads it every poll).

    Candidate logging (CANDIDATE_CPU, 2026-10-25) and WATCHER_LOG are unchanged.
    Text is ASCII on purpose (PowerShell 5 reads scripts without BOM as ANSI).
#>

$ErrorActionPreference = "Continue"

# PUBLISH_PREP: repo root = folder of this script (run with -File).
$RepoRoot          = $(if ($PSScriptRoot) { $PSScriptRoot } else { (Get-Location).Path })
$BotService        = "OkoBot"
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
$DataDir           = Join-Path $RepoRoot "data"
$GameFlag          = Join-Path $DataDir "game_mode.flag"
$ForceFlag         = Join-Path $DataDir "brain_force.flag"

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

function Get-UnixNow { [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() }

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
    $text = "New process detected: $ProcessName`nAdd it to games.txt (game mode: brain sleeps, VRAM freed)?"

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

# --- .env ---------------------------------------------------------------------
$OllamaHost  = "localhost"
$OllamaPort  = "11434"
$GameOffPolls = 2
$envPath = Join-Path $RepoRoot ".env"
function Get-EnvValue {
    param([string]$Name)
    if (-not (Test-Path $envPath)) { return $null }
    $line = Select-String -Path $envPath -Pattern "^$Name=" -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($line) { return ($line.Line -split "=", 2)[1].Trim() }
    return $null
}
$v = Get-EnvValue "OLLAMA_HOST";  if ($v) { $OllamaHost = $v }
$v = Get-EnvValue "OLLAMA_PORT";  if ($v) { $OllamaPort = $v }
$OllamaModel = Get-EnvValue "OLLAMA_MODEL"
if (-not $OllamaModel) { $OllamaModel = "gemma2:latest" }
$v = Get-EnvValue "OKO_GAME_OFF_POLLS"
$n = 0
if ($v -and [int]::TryParse($v, [ref]$n) -and $n -ge 1) { $GameOffPolls = $n }
$OllamaBase = "http://${OllamaHost}:${OllamaPort}"

# --- flags ----------------------------------------------------------------------
function Write-GameFlag {
    param([string]$Game, [long]$Since)
    try {
        if (-not (Test-Path $DataDir)) { New-Item -ItemType Directory -Path $DataDir -Force | Out-Null }
        [System.IO.File]::WriteAllText($GameFlag, "v2 since=$Since game=$Game beat=$(Get-UnixNow)`r`n", $Utf8NoBom)
        $script:flagWriteFails = 0
    } catch {
        $script:flagWriteFails++
        if ($script:flagWriteFails -le 3 -or ($script:flagWriteFails % 25) -eq 0) {
            Write-Log "[GAME_MODE] flag=write_fail n=$($script:flagWriteFails) err=$($_.Exception.GetType().Name)" "WARNING"
        }
    }
}

function Remove-FlagFile {
    param([string]$Path, [string]$Name)
    try {
        if (Test-Path $Path) { Remove-Item -Path $Path -Force -ErrorAction Stop }
    } catch {
        Write-Log "[GAME_MODE] flag=remove_fail file=$Name err=$($_.Exception.GetType().Name)" "WARNING"
    }
}

function Get-ForceUntil {
    # P9: valid /force_brain -> unix "until"; missing / broken / expired -> 0
    if (-not (Test-Path $ForceFlag)) { return 0 }
    try {
        $t = [System.IO.File]::ReadAllText($ForceFlag)
    } catch {
        return 0
    }
    if ($t -match '(?<![\w])until=(\d{1,12})(?!\d)') {
        $until = [long]$Matches[1]
        if ($until -gt (Get-UnixNow)) { return $until }
    }
    return 0
}

# --- P2/P3: VRAM guard ----------------------------------------------------------
function Get-LoadedModels {
    $r = Invoke-RestMethod -Uri "$OllamaBase/api/ps" -Method Get -TimeoutSec 5 -ErrorAction Stop
    if ($null -eq $r -or $null -eq $r.models) { return @() }
    return @($r.models)
}

function Invoke-VramGuard {
    try {
        $models = Get-LoadedModels
        foreach ($m in $models) {
            $name = [string]$m.name
            if ([long]$m.size_vram -gt 0 -or $name -eq $OllamaModel) {
                try {
                    $body = @{ model = $name; keep_alive = 0 } | ConvertTo-Json
                    Invoke-RestMethod -Uri "$OllamaBase/api/generate" -Method Post -Body $body -ContentType "application/json" -TimeoutSec 15 | Out-Null
                } catch {
                    # counted below through the second /api/ps
                }
            }
        }
        $busy = @(Get-LoadedModels | Where-Object { [long]$_.size_vram -gt 0 } | ForEach-Object { [string]$_.name })
    } catch {
        $script:guardTries++
        $script:vramFreeLogged = $false
        if ($script:guardTries -le 3 -or ($script:guardTries % 25) -eq 0) {
            Write-Log "[GAME_MODE] vram=unknown try=$($script:guardTries) err=$($_.Exception.GetType().Name) (Ollama unreachable)" "WARNING"
        }
        return
    }
    if ($busy.Count -eq 0) {
        if (-not $script:vramFreeLogged) {
            Write-Log "[GAME_MODE] vram=free tries=$($script:guardTries + 1)"
            $script:vramFreeLogged = $true
        }
        $script:guardTries = 0
    } else {
        $script:guardTries++
        $script:vramFreeLogged = $false
        if ($script:guardTries -le 3 -or ($script:guardTries % 25) -eq 0) {
            Write-Log "[GAME_MODE] vram=busy models=$($busy -join ',') try=$($script:guardTries)" "WARNING"
        }
    }
}

# --- start: P7 / P8 -------------------------------------------------------------
$script:inGame = $false
$script:since = 0
$script:offPolls = 0
$script:forceOn = $false
$script:vramFreeLogged = $false
$script:guardTries = 0
$script:flagWriteFails = 0

Write-Log "[GAME_WATCHER] started mode=v2 games=$GamesFile poll_s=$PollIntervalSec off_polls=$GameOffPolls model=$OllamaModel ollama=$OllamaBase candidate_cpu_pct=$CandidateCpuThreshold hot_polls=$CandidateHotPolls"

if (Test-Path $GameFlag) {
    $flagText = ""
    try { $flagText = ([System.IO.File]::ReadAllText($GameFlag)).Trim() } catch { }
    if (-not $flagText.StartsWith("v2")) {
        # P8: v1 flag = the old watcher stopped OkoBot
        Write-Log "[GAME_WATCHER] migrate v1_flag=($flagText)"
        try {
            $svc = Get-Service -Name $BotService -ErrorAction Stop
            if ($svc.Status -eq "Stopped") {
                Start-Service -Name $BotService -ErrorAction Stop
                Write-Log "[GAME_WATCHER] migrate started_bot=1"
            } else {
                Write-Log "[GAME_WATCHER] migrate started_bot=0 status=$($svc.Status)"
            }
        } catch {
            Write-Log "[GAME_WATCHER] migrate start=fail service=$BotService err=$($_.Exception.GetType().Name)" "WARNING"
        }
        Remove-FlagFile -Path $GameFlag -Name "game_mode.flag"
    } else {
        # P7: resume game mode; leaves through P4 if no game is running
        $script:inGame = $true
        $script:since = $(if ($flagText -match 'since=(\d+)') { [long]$Matches[1] } else { Get-UnixNow })
        Write-Log "[GAME_WATCHER] resume in_game=1 flag=($flagText)"
    }
}

# --- loop -----------------------------------------------------------------------
while ($true) {
    $gameNames = Get-GameList
    $detectedGame = Test-GameRunning -GameNames $gameNames

    if ($detectedGame) {
        $script:offPolls = 0
        if (-not $script:inGame) {
            $script:inGame = $true
            $script:since = Get-UnixNow
            $script:vramFreeLogged = $false
            $script:guardTries = 0
            Write-Log "[GAME_MODE] on game=$detectedGame"
        }
        Write-GameFlag -Game $detectedGame -Since $script:since      # P1 + P6: flag before unload
        $forceUntil = Get-ForceUntil
        if ($forceUntil -gt 0) {
            if (-not $script:forceOn) {
                Write-Log "[GAME_MODE] force=on until=$forceUntil (models not unloaded)"
                $script:forceOn = $true
            }
        } else {
            if ($script:forceOn) {
                Write-Log "[GAME_MODE] force=expired"
                $script:forceOn = $false
                $script:vramFreeLogged = $false
                $script:guardTries = 0
            }
            Invoke-VramGuard                                          # P2 / P3
        }
    }
    elseif ($script:inGame) {
        $script:offPolls++
        if ($script:offPolls -ge $GameOffPolls) {                    # P4
            Remove-FlagFile -Path $GameFlag -Name "game_mode.flag"
            Remove-FlagFile -Path $ForceFlag -Name "brain_force.flag" # P10: force is per game session
            Write-Log "[GAME_MODE] off after_polls=$($script:offPolls) duration_s=$((Get-UnixNow) - $script:since)"
            $script:inGame = $false
            $script:forceOn = $false
            $script:offPolls = 0
        } else {
            Write-GameFlag -Game "(leaving)" -Since $script:since      # heartbeat while debouncing
        }
    }

    if (-not $detectedGame) {
        Update-GameCandidates -KnownGameNames $gameNames
    }

    Start-Sleep -Seconds $PollIntervalSec
}
