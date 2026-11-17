# <repo>\tools\baseline_check.ps1   (<repo> — корень репозитория Ока)
# Шаг 1 стресс-теста: снимок исходного состояния (только чтение, ничего не меняет).
# Запуск: powershell -NoProfile -ExecutionPolicy Bypass -File <repo>\tools\baseline_check.ps1
# Результат: <repo>\baseline_report.txt ; код выхода 0 = BASELINE OK, 1 = есть отклонения.
# Совместимость: Windows PowerShell 5.1. Файл сохранён в UTF-8 с BOM (иначе PS 5.1 исказит кириллицу).

param(
    [int]$TailKB = 2048                                   # сколько последних КБ лога разбирать
)

$ErrorActionPreference = 'Stop'                           # любая необработанная ошибка = остановка
$Root   = Split-Path -Parent $PSScriptRoot                # корень репозитория (скрипт лежит в <repo>\tools)
$Nssm   = Join-Path $Root 'nssm.exe'                      # NSSM из корня проекта
$Report = Join-Path $Root 'baseline_report.txt'           # куда пишется отчёт
$out    = New-Object System.Collections.Generic.List[string]  # строки отчёта
$fail   = New-Object System.Collections.Generic.List[string]  # причины «не OK»

function Add-Line([string]$s) { $out.Add($s); Write-Host $s }   # пишет и в отчёт, и на экран

function Hide-Ids([string]$s) {                           # маскирует длинные числа (Telegram user_id и т.п.)
    return ($s -replace '\d{6,}', '<id>')
}

function Get-OkoLogPath {                                 # путь лога OkoBot = NSSM AppStderr
    $p = ((& $Nssm get OkoBot AppStderr) -join '') -replace "`0", ''   # вывод nssm содержит NUL-символы
    return $p.Trim()
}

function Read-LogTail([string]$path, [int]$kb) {          # читает хвост файла, не мешая NSSM писать в него
    $fs = [IO.File]::Open($path, 'Open', 'Read', 'ReadWrite')
    try {
        $start = [Math]::Max([int64]0, $fs.Length - [int64]$kb * 1024)
        [void]$fs.Seek($start, 'Begin')
        $buf = New-Object byte[] ($fs.Length - $start)
        $n = $fs.Read($buf, 0, $buf.Length)
        $text = [Text.Encoding]::UTF8.GetString($buf, 0, $n)
    } finally { $fs.Close() }
    $lines = $text -split "`r?`n"
    if ($start -gt 0 -and $lines.Count -gt 0) { $lines = $lines[1..($lines.Count - 1)] }  # первая строка обрезана
    return ,$lines
}

function Get-GateState([string[]]$lines) {                # состояние TransportGate по последнему переходу в логе
    $last = $null
    foreach ($l in $lines) { if ($l -match '\[TRANSPORT_GATE\]') { $last = $l } }
    if (-not $last) { return @('CLOSED', 'нет строк [TRANSPORT_GATE] в хвосте лога — переходов не было') }
    $m = [regex]::Matches($last, '(?:->|→)\s*(HALF_OPEN|CLOSED|OPEN)\b')
    if ($m.Count -gt 0) { return @($m[$m.Count - 1].Groups[1].Value, $last) }
    if ($last -match '\bOPEN\b') { return @('OPEN', $last) }
    return @('UNKNOWN', $last)
}

Add-Line "=== BASELINE $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
Set-Location $Root
Add-Line ("HEAD: " + ((git rev-parse --short HEAD) -join ''))   # точка отсчёта кода

# --- 1. Службы ---
foreach ($svc in 'OkoBot', 'OllamaService') {
    $s = Get-Service -Name $svc -ErrorAction SilentlyContinue
    $st = if ($s) { [string]$s.Status } else { 'NOT_FOUND' }
    Add-Line ("service {0}: {1}" -f $svc, $st)
    if ($st -ne 'Running') { $fail.Add("$svc не Running ($st)") }
}

# --- 2. Ollama отвечает (та же проба, что у TransportGate: /api/version) ---
try {
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $v = Invoke-RestMethod -Uri 'http://127.0.0.1:11434/api/version' -TimeoutSec 3 -UseBasicParsing
    Add-Line ("ollama /api/version: {0} ({1} ms)" -f $v.version, $sw.ElapsedMilliseconds)
} catch {
    Add-Line "ollama /api/version: НЕТ ОТВЕТА ($($_.Exception.GetType().Name))"
    $fail.Add('Ollama не отвечает')
}

# --- 3. Лог ---
$log = Get-OkoLogPath
if (-not $log -or -not (Test-Path $log)) {
    Add-Line "LOG: не найден ('$log')"
    $fail.Add('лог OkoBot не найден')
    $lines = @()
} else {
    $fi = Get-Item $log
    Add-Line ("LOG: {0}  size={1}  modified={2:yyyy-MM-dd HH:mm:ss}" -f $log, $fi.Length, $fi.LastWriteTime)
    $lines = Read-LogTail $log $TailKB
    Add-Line ("tail: {0} КБ, строк {1}" -f $TailKB, $lines.Count)
}

# --- 4. TransportGate ---
$gate = Get-GateState $lines
Add-Line ("TRANSPORT_GATE: {0}" -f $gate[0])
Add-Line ("  основание: " + (Hide-Ids $gate[1]))
if ($gate[0] -ne 'CLOSED') { $fail.Add("TransportGate = $($gate[0])") }

# --- 5. Красные строки (F1: при штатной работе их быть не должно) ---
$red = @($lines | Where-Object { $_ -match '^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3} \[(ERROR|CRITICAL)\]' })
Add-Line ("red lines in tail: {0}" -f $red.Count)
foreach ($r in ($red | Select-Object -Last 5)) {               # только начало строки, id замаскированы
    $short = if ($r.Length -gt 140) { $r.Substring(0, 140) + '…' } else { $r }
    Add-Line ("  " + (Hide-Ids $short))
}

# --- 6. Метрики M6 и S ---
$met = @($lines | Where-Object { $_ -match '\[METRICS\] point=' })
Add-Line ("[METRICS] lines in tail: {0}" -f $met.Count)
foreach ($m in ($met | Select-Object -Last 3)) { Add-Line ("  " + ($m -replace '^.*?\[METRICS\]', '[METRICS]')) }
$mfail = @($lines | Where-Object { $_ -match '\[METRICS_FAIL\]' })
Add-Line ("[METRICS_FAIL] lines in tail: {0}" -f $mfail.Count)
$vs = @($lines | Where-Object { $_ -match '\[VECTOR_S\]' })
if ($vs.Count -eq 0) { Add-Line '[VECTOR_S]: метрика S ещё не реализована (claude/TZ_vector_marker_S_2026-10-21.md) — проверять нечего' }
else { Add-Line ("[VECTOR_S] lines in tail: {0}" -f $vs.Count) }

# --- 7. Итог ---
Add-Line ''
if ($fail.Count -eq 0) { Add-Line 'VERDICT: BASELINE OK' }
else { Add-Line ('VERDICT: BASELINE NOT OK — ' + ($fail -join '; ')) }

$out | Out-File -FilePath $Report -Encoding UTF8          # отчёт для аудита
Write-Host "report: $Report"
if ($fail.Count -eq 0) { exit 0 } else { exit 1 }
