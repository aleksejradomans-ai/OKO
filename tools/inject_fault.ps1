# <repo>\tools\inject_fault.ps1   (<repo> — корень репозитория Ока)
# Шаг 2 стресс-теста: контролируемый обрыв Ollama и запись реакции TransportGate.
# Меняет ТОЛЬКО состояние службы OllamaService (nssm stop -> nssm start). OkoBot, SQLite, Chroma не трогаются.
# Запуск (PowerShell ОТ АДМИНИСТРАТОРА):
#   powershell -NoProfile -ExecutionPolicy Bypass -File <repo>\tools\inject_fault.ps1
# Результат: <repo>\fault_report.txt ; код выхода 0 = все проверки пройдены, 1 = нет, 2 = предусловия не выполнены.
#
# ВАЖНО: TransportGate считает только сбои реальных запросов к модели. Пока Ollama стоит, боту нужно
# написать 3–4 коротких сообщения в Telegram, а после старта Ollama — ещё одно. Скрипт подскажет, когда.

param(
    [int]$OutageMaxSec  = 180,                            # сколько максимум держать Ollama остановленной
    [int]$RecoverMaxSec = 300,                            # сколько ждать возврата в CLOSED после старта Ollama
    [int]$OllamaUpMaxSec = 180                            # сколько ждать готовности Ollama после старта (поиск GPU ~1.5 мин)
)

$ErrorActionPreference = 'Stop'
$Root   = Split-Path -Parent $PSScriptRoot                # корень репозитория
$Nssm   = Join-Path $Root 'nssm.exe'
$Report = Join-Path $Root 'fault_report.txt'
$out    = New-Object System.Collections.Generic.List[string]
$script:offset  = [int64]0                                # позиция в логе, с которой читаем новые строки
$script:carry   = ''                                      # недописанный хвост последней строки
$script:rotated = $false
$script:seen    = New-Object System.Collections.Generic.List[string]  # все новые строки за время теста

function Add-Line([string]$s) { $out.Add($s); Write-Host $s }
function Hide-Ids([string]$s) { return ($s -replace '\d{6,}', '<id>') }
function Stamp { return (Get-Date -Format 'HH:mm:ss') }

function Get-OkoLogPath {
    $p = ((& $Nssm get OkoBot AppStderr) -join '') -replace "`0", ''
    return $p.Trim()
}

function Read-NewLines([string]$path) {                   # дочитывает лог с прошлой позиции (не мешает NSSM)
    $fs = [IO.File]::Open($path, 'Open', 'Read', 'ReadWrite')
    try {
        if ($fs.Length -lt $script:offset) { $script:offset = 0; $script:carry = ''; $script:rotated = $true }
        [void]$fs.Seek($script:offset, 'Begin')
        $len = $fs.Length - $script:offset
        $buf = New-Object byte[] $len
        $n = $fs.Read($buf, 0, $buf.Length)
        $script:offset += $n
    } finally { $fs.Close() }
    if ($n -le 0) { return @() }
    $text = $script:carry + [Text.Encoding]::UTF8.GetString($buf, 0, $n)
    $parts = $text -split "`r?`n"
    $script:carry = $parts[-1]                            # последняя часть может быть недописана
    $new = @()
    if ($parts.Count -gt 1) { $new = $parts[0..($parts.Count - 2)] }
    foreach ($l in $new) { $script:seen.Add($l) }
    return ,$new
}

function Get-Transition([string]$line) {                  # -> OPEN / CLOSED / HALF_OPEN / $null
    if ($line -notmatch '\[TRANSPORT_GATE\]') { return $null }
    $m = [regex]::Matches($line, '(?:->|→)\s*(HALF_OPEN|CLOSED|OPEN)\b')
    if ($m.Count -gt 0) { return $m[$m.Count - 1].Groups[1].Value }
    if ($line -match '\bOPEN\b') { return 'OPEN' }
    return $null
}

function Test-Ollama {                                    # та же проба, что у TransportGate
    try { [void](Invoke-RestMethod -Uri 'http://127.0.0.1:11434/api/version' -TimeoutSec 3 -UseBasicParsing); return $true }
    catch { return $false }
}

function Wait-Service([string]$name, [string]$want, [int]$sec) {
    $t = [Diagnostics.Stopwatch]::StartNew()
    while ($t.Elapsed.TotalSeconds -lt $sec) {
        if ([string](Get-Service $name).Status -eq $want) { return $true }
        Start-Sleep -Milliseconds 500
    }
    return $false
}

function Get-BotPid {                                     # PID python-процесса OkoBot (дочерний у nssm)
    $svc = Get-CimInstance Win32_Service -Filter "Name='OkoBot'"
    if (-not $svc -or -not $svc.ProcessId) { return $null }
    $child = Get-CimInstance Win32_Process -Filter "ParentProcessId=$($svc.ProcessId)" | Select-Object -First 1
    if ($child) { return $child.ProcessId } else { return $null }
}

Add-Line "=== FAULT INJECTION $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
Set-Location $Root
Add-Line ("HEAD: " + ((git rev-parse --short HEAD) -join ''))

# --- 0. Предусловия (ничего не меняем, если хоть одно не выполнено) ---
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
$pre = @()
if (-not $isAdmin) { $pre += 'нужен PowerShell от администратора (nssm stop/start)' }
foreach ($svc in 'OkoBot', 'OllamaService') {
    $s = Get-Service -Name $svc -ErrorAction SilentlyContinue
    if (-not $s) { $pre += "служба $svc не найдена" } elseif ([string]$s.Status -ne 'Running') { $pre += "$svc не Running ($($s.Status))" }
}
if (-not (Test-Ollama)) { $pre += 'Ollama не отвечает до начала теста' }
$log = Get-OkoLogPath
if (-not $log -or -not (Test-Path $log)) { $pre += "лог OkoBot не найден ('$log')" }
if ($pre.Count -gt 0) {
    Add-Line ('PRECONDITIONS FAILED: ' + ($pre -join '; '))
    Add-Line "Ничего не изменено. Если OllamaService осталась остановленной прошлым прогоном: $Nssm start OllamaService"
    $out | Out-File -FilePath $Report -Encoding UTF8
    exit 2
}
$botPidBefore = Get-BotPid
Add-Line ("LOG: $log ; OkoBot pid=$botPidBefore")
$script:offset = (Get-Item $log).Length                   # всё, что было до теста, не учитываем
$tStart = Get-Date

$sawOpen = $false; $sawHalf = $false; $sawClosed = $false
$procOutage = 0; $procRecover = 0; $warnedNoMsg = $false   # сколько сообщений бот реально начал обрабатывать
$tOpen = $null; $tOllamaUp = $null; $tClosed = $null

# --- 1. Обрыв: остановить Ollama и дождаться OPEN (try/finally гарантирует обратный старт) ---
try {
    Add-Line "[$(Stamp)] nssm stop OllamaService"
    & $Nssm stop OllamaService | Out-Null
    if (-not (Wait-Service 'OllamaService' 'Stopped' 30)) { throw 'OllamaService не остановилась за 30 с' }
    Add-Line ("[$(Stamp)] OllamaService Stopped; /api/version отвечает: " + (Test-Ollama))
    Write-Host ''
    Write-Host '>>> СЕЙЧАС: отправь боту в Telegram 3–4 коротких сообщения подряд (например: "тест 1", "тест 2"...).' -ForegroundColor Yellow
    Write-Host ">>> Жду переход TransportGate в OPEN (до $OutageMaxSec с)..." -ForegroundColor Yellow
    $t = [Diagnostics.Stopwatch]::StartNew()
    while (-not $sawOpen -and $t.Elapsed.TotalSeconds -lt $OutageMaxSec) {
        foreach ($l in (Read-NewLines $log)) {
            if ($l -match 'Processing message from') { $procOutage++ }
            $tr = Get-Transition $l
            if ($tr -eq 'OPEN' -and -not $sawOpen) { $sawOpen = $true; $tOpen = Get-Date; Add-Line "[$(Stamp)] OPEN: $(Hide-Ids $l)" }
        }
        if (-not $warnedNoMsg -and $procOutage -eq 0 -and $t.Elapsed.TotalSeconds -ge 60) {
            $warnedNoMsg = $true
            Add-Line "[$(Stamp)] ВНИМАНИЕ: за 60 с бот не начал обрабатывать ни одного сообщения (нет 'Processing message'). Проверь, что пишешь именно этому боту."
        }
        Start-Sleep -Seconds 1
    }
    if (-not $sawOpen) { Add-Line "[$(Stamp)] OPEN не появился за $OutageMaxSec с (сообщения боту отправлялись?)" }
    else { Start-Sleep -Seconds 5 }                       # дать записаться следующим строкам backoff
}
finally {
    # --- 2. Восстановление: выполняется ВСЕГДА, даже при ошибке или Ctrl+C ---
    Add-Line "[$(Stamp)] nssm start OllamaService"
    & $Nssm start OllamaService | Out-Null
    [void](Wait-Service 'OllamaService' 'Running' 60)
    $t2 = [Diagnostics.Stopwatch]::StartNew()
    while (-not (Test-Ollama) -and $t2.Elapsed.TotalSeconds -lt $OllamaUpMaxSec) { Start-Sleep -Seconds 2 }
    if (Test-Ollama) { $tOllamaUp = Get-Date; Add-Line ("[$(Stamp)] Ollama снова отвечает (через {0:N0} с после старта)" -f $t2.Elapsed.TotalSeconds) }
    else { Add-Line "[$(Stamp)] ВНИМАНИЕ: Ollama не отвечает через $OllamaUpMaxSec с после старта" }
}

# --- 3. Ждать возврата в CLOSED ---
if ($tOllamaUp) {
    Write-Host ''
    Write-Host '>>> СЕЙЧАС: отправь боту ещё одно сообщение. Если ответ «автоповтор связи через ~N с» — подожди N с и отправь снова.' -ForegroundColor Yellow
    Write-Host '>>> Первый ответ после старта Ollama может идти долго: модель грузится в видеопамять.' -ForegroundColor Yellow
    $t3 = [Diagnostics.Stopwatch]::StartNew()
    while (-not $sawClosed -and $t3.Elapsed.TotalSeconds -lt $RecoverMaxSec) {
        foreach ($l in (Read-NewLines $log)) {
            if ($l -match 'Processing message from') { $procRecover++ }
            $tr = Get-Transition $l
            if ($tr -eq 'OPEN' -and -not $sawOpen) { $sawOpen = $true; $tOpen = Get-Date; Add-Line "[$(Stamp)] OPEN: $(Hide-Ids $l)" }
            if ($tr -eq 'HALF_OPEN') { $sawHalf = $true }
            if ($tr -eq 'CLOSED') { $sawClosed = $true; $tClosed = Get-Date; Add-Line "[$(Stamp)] CLOSED: $(Hide-Ids $l)" }
        }
        Start-Sleep -Seconds 1
    }
}
Start-Sleep -Seconds 2
[void](Read-NewLines $log)                                # дочитать последние строки

# --- 4. Сбор фактов из лога за время теста (только служебные строки, id замаскированы) ---
Add-Line ''
Add-Line '=== LOG EXCERPT (только [TRANSPORT_GATE], LLM_UNAVAILABLE/LLM_TIMEOUT, [TG_TRANSPORT], [ERROR]/[CRITICAL])'
$red = @()
foreach ($l in $script:seen) {
    $isRed = $l -match '^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3} \[(ERROR|CRITICAL)\]'
    if ($isRed) { $red += $l }
    if ($isRed -or $l -match '\[TRANSPORT_GATE\]|LLM_UNAVAILABLE|LLM_TIMEOUT|\[TG_TRANSPORT\]') {
        $short = if ($l.Length -gt 200) { $l.Substring(0, 200) + '…' } else { $l }
        Add-Line ('  ' + (Hide-Ids $short))
    }
}
$botPidAfter = Get-BotPid

# --- 5. Проверки ---
Add-Line ''
Add-Line '=== CHECKS'
$checks = [ordered]@{
    'A  TransportGate перешёл в OPEN при остановленной Ollama' = $sawOpen
    'B  TransportGate вернулся в CLOSED после старта Ollama'   = $sawClosed
    'C  0 красных строк [ERROR]/[CRITICAL] за время теста (F1)' = ($red.Count -eq 0)
    'D  OllamaService снова Running и отвечает'                 = (([string](Get-Service OllamaService).Status -eq 'Running') -and (Test-Ollama))
    'E  OkoBot не перезапускался (тот же PID)'                   = ($botPidBefore -and ($botPidBefore -eq $botPidAfter))
    'F  сообщения дошли до бота во время обрыва (Processing message)' = ($procOutage -gt 0)
}
foreach ($k in $checks.Keys) { Add-Line (("{0}  {1}" -f $(if ($checks[$k]) { 'PASS' } else { 'FAIL' }), $k)) }
Add-Line ("HALF_OPEN в логе: {0} (отдельная строка не обязательна)" -f $sawHalf)
Add-Line ("сообщений начато обработкой: при обрыве={0}, после старта Ollama={1}" -f $procOutage, $procRecover)
if ($procOutage -eq 0) { Add-Line 'ДИАГНОЗ: при F=FAIL проверки A/B недействительны — предохранитель не проверялся, до бота не дошло ни одного сообщения.' }
Add-Line ("red lines: {0}; лог ротирован во время теста: {1}" -f $red.Count, $script:rotated)
if ($tOpen)   { Add-Line ("время до OPEN от начала: {0:N0} с" -f ($tOpen - $tStart).TotalSeconds) }
if ($tClosed -and $tOllamaUp) { Add-Line ("время до CLOSED от подъёма Ollama: {0:N0} с" -f ($tClosed - $tOllamaUp).TotalSeconds) }
Add-Line ("OkoBot pid: до={0} после={1}" -f $botPidBefore, $botPidAfter)

$allPass = -not ($checks.Values -contains $false)
Add-Line ''
Add-Line ('VERDICT: ' + $(if ($allPass) { 'FAULT TEST PASSED' } else { 'FAULT TEST FAILED' }))
Add-Line 'Квалити-предохранитель (CIRCUIT BREAKER) по F1 от транспорта не зависит: проверь /status в Telegram — должно быть CLOSED.'
$out | Out-File -FilePath $Report -Encoding UTF8
Write-Host "report: $Report"
if ($allPass) { exit 0 } else { exit 1 }
