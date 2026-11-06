# C:\oko\security_tools\update_manifest.ps1
#
# ОБНОВЛЕНО (см. историю решений — аудит блоков №3-4): добавлены файлы,
# затронутые правками этой сессии: static_analyzer.py уже был в блоке №1
# (изменён повторно — SYS_PATH_ALLOWED_FILES), новые записи —
# knowledge_indexer.py (threading.Lock), process_watchdog.py (resilient
# main loop), oko_rag/indexer.py (SHA256).
#
# ВАЖНО, не проверено на момент написания: пути knowledge_indexer.py,
# process_watchdog.py и oko_rag/indexer.py указаны здесь ОТНОСИТЕЛЬНО
# корня репозитория (C:\oko\), НЕ src\ — это отражает их фактическое
# расположение по исходному листингу файлов проекта. Если PatchApplier
# инициализируется с project_root=C:\oko\src\ (см. docstring
# SandboxEnvironment: "project_root — обычно oko/src/"), эти три файла
# физически ВНЕ зоны, которую PatchApplier способен патчить — guard_path
# внутри _commit() отклонит любую попытку записи туда независимо от
# манифеста. В этом случае их присутствие здесь безвредно, но не даёт
# реальной защиты ОТ PatchApplier (они и так недостижимы для него) —
# хотя по-прежнему имеет смысл, если manifest.sha256 используется шире,
# чем только для контроля PatchApplier (например, как общий integrity-
# monitor для критичных файлов проекта). Подтверди фактический
# project_root перед тем, как полагаться на эту защиту как единственную.
$files = @(
    "src\patch_applier.py",
    "src\static_analyzer.py",
    "src\sandbox_environment.py",
    "src\path_guard.py",
    "src\snapshot_manager.py",
    "src\test_patch_applier.py",
    "src\test_static_analyzer.py",
    "src\test_sandbox_environment.py",
    "src\test_path_guard.py",
    "src\test_snapshot_manager.py",
    "src\\knowledge_indexer.py",
    "src\\process_watchdog.py",
    "oko_rag\indexer.py"
)
$manifestPath = "security_tools\manifest.sha256"

$hashList = @()
foreach ($file in $files) {
    if (Test-Path $file) {
        $hash = (Get-FileHash -Path $file -Algorithm SHA256).Hash
        $hashList += "$hash $file"
    } else {
        Write-Error "Файл $file не найден!"
        exit 1
    }
}

$hashList | Out-File -FilePath $manifestPath -Encoding utf8
Write-Host "Манифест обновлен: $manifestPath ($($files.Count) файлов)"
