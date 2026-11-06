<#
    oko-ctl.ps1 — управление службами проекта Oko.

    Использование:
        .\oko-ctl.ps1 start      — запустить всё (Ollama -> Watcher -> Bot)
        .\oko-ctl.ps1 stop       — остановить всё (Bot -> Watcher -> Ollama)
        .\oko-ctl.ps1 restart    — перезапустить всё
        .\oko-ctl.ps1 status     — показать статус всех служб
        .\oko-ctl.ps1 logs       — показать последние строки логов бота (stderr)

    Без аргумента — по умолчанию показывает status.
#>

param(
    [Parameter(Position = 0)]
    [ValidateSet("start", "stop", "restart", "status", "logs")]
    [string]$Action = "status"
)

# Принудительная UTF-8 кодировка вывода — без этого кириллица в Write-Host
# может отображаться битыми символами в некоторых консолях Windows,
# в зависимости от текущей кодовой страницы (chcp) сессии.
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$nssm = "C:\oko\nssm.exe"

# Порядок важен: Ollama должна подняться раньше бота (wait_for_model в bot.py
# её ждёт при старте), поэтому при запуске идём Ollama -> Watcher -> Bot,
# а при остановке — в обратном порядке (Bot -> Watcher -> Ollama), чтобы не
# оставлять бота без LLM-бэкенда, пока он ещё может получать запросы.
$servicesUp = @("OllamaService", "OkoSystemWatcher", "OkoBotService")
$servicesDown = @("OkoBotService", "OkoSystemWatcher", "OllamaService")

function Show-Status {
    Write-Host "`n=== Статус служб Oko ===" -ForegroundColor Cyan
    foreach ($svc in $servicesUp) {
        # nssm status иногда возвращает вывод как массив строк — Out-String
        # + Trim() гарантированно даёт одну чистую строку вместо "System.Object[]".
        $status = (& $nssm status $svc 2>$null | Out-String).Trim()
        $color = switch ($status) {
            "SERVICE_RUNNING" { "Green" }
            "SERVICE_STOPPED" { "Yellow" }
            default { "Red" }
        }
        Write-Host ("{0,-20} {1}" -f $svc, $status) -ForegroundColor $color
    }
    Write-Host ""
}

switch ($Action) {
    "start" {
        Write-Host "Запуск служб (Ollama -> Watcher -> Bot)..." -ForegroundColor Cyan
        foreach ($svc in $servicesUp) {
            Write-Host "  -> $svc"
            & $nssm start $svc
            Start-Sleep -Seconds 2
        }
        Show-Status
    }
    "stop" {
        Write-Host "Остановка служб (Bot -> Watcher -> Ollama)..." -ForegroundColor Cyan
        foreach ($svc in $servicesDown) {
            Write-Host "  -> $svc"
            & $nssm stop $svc
        }
        Show-Status
    }
    "restart" {
        Write-Host "Перезапуск всех служб..." -ForegroundColor Cyan
        foreach ($svc in $servicesDown) {
            & $nssm stop $svc
        }
        Start-Sleep -Seconds 2
        foreach ($svc in $servicesUp) {
            Write-Host "  -> $svc"
            & $nssm start $svc
            Start-Sleep -Seconds 2
        }
        Show-Status
    }
    "status" {
        Show-Status
    }
    "logs" {
        Write-Host "`n=== Последние строки bot_stderr.log ===" -ForegroundColor Cyan
        Get-Content "C:\oko\logs\bot_stderr.log" -Tail 25
    }
}
