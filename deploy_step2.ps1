﻿# =============================================================
# deploy_step2.ps1 — раскладка файлов патча из архива по местам
# Строго по подтверждённому списку из третьего чата (20.07.2026).
# =============================================================

$ErrorActionPreference = "Stop"

$zipPath = "C:\Users\Алёша Пряник\Downloads\files (19).zip"
$extractDir = "C:\oko_patch_extract_temp"

Write-Host "=== Шаг 2: раскладка файлов патча ===" -ForegroundColor Cyan

# --- 1. Распаковка во временную папку ---
if (Test-Path $extractDir) {
    Remove-Item $extractDir -Recurse -Force
}
New-Item -ItemType Directory -Path $extractDir | Out-Null
Expand-Archive -Path $zipPath -DestinationPath $extractDir -Force
Write-Host "Распаковано во временную папку: $extractDir" -ForegroundColor Green

# --- 2. Показать, что реально внутри (на случай вложенных подпапок в zip) ---
Write-Host "`nСодержимое архива:" -ForegroundColor Yellow
Get-ChildItem -Path $extractDir -Recurse -File | Select-Object FullName

# --- 3. Функция поиска файла внутри распакованной структуры ---
# Файлы могли оказаться либо в корне $extractDir, либо в подпапке src/ —
# ищем по имени рекурсивно, чтобы не зависеть от точной структуры zip.
function Find-ExtractedFile {
    param([string]$FileName)
    $found = Get-ChildItem -Path $extractDir -Recurse -File -Filter $FileName -ErrorAction SilentlyContinue
    if ($found.Count -eq 0) {
        Write-Host "  [!] НЕ НАЙДЕН: $FileName" -ForegroundColor Red
        return $null
    }
    if ($found.Count -gt 1) {
        Write-Host "  [!] Найдено НЕСКОЛЬКО копий $FileName — беру первую:" -ForegroundColor Yellow
        $found | ForEach-Object { Write-Host "      $($_.FullName)" }
    }
    return $found[0].FullName
}

# --- 4. Файлы, которые идут в C:\oko\src\ ---
$srcFiles = @(
    "governor.py",
    "bot.py",
    "knowledge_indexer.py",
    "process_watchdog.py",
    "cli_patch.py",
    "static_analyzer.py",
    "test_static_analyzer.py",
    "staging_manager.py",
    "staging_repository.py"
)

Write-Host "`n--- Копирование в C:\oko\src\ ---" -ForegroundColor Cyan
foreach ($f in $srcFiles) {
    $source = Find-ExtractedFile -FileName $f
    if ($source) {
        Copy-Item -Path $source -Destination "C:\oko\src\$f" -Force
        Write-Host "  OK: $f -> C:\oko\src\$f" -ForegroundColor Green
    }
}

# --- 5. oko_rag/indexer.py ---
Write-Host "`n--- Копирование в C:\oko\oko_rag\ ---" -ForegroundColor Cyan
$indexerSource = Find-ExtractedFile -FileName "indexer.py"
if ($indexerSource) {
    Copy-Item -Path $indexerSource -Destination "C:\oko\oko_rag\indexer.py" -Force
    Write-Host "  OK: indexer.py -> C:\oko\oko_rag\indexer.py" -ForegroundColor Green
}

# --- 6. migrate_pending_tasks.py в корень ---
Write-Host "`n--- Копирование в корень C:\oko\ ---" -ForegroundColor Cyan
$migrateSource = Find-ExtractedFile -FileName "migrate_pending_tasks.py"
if ($migrateSource) {
    Copy-Item -Path $migrateSource -Destination "C:\oko\migrate_pending_tasks.py" -Force
    Write-Host "  OK: migrate_pending_tasks.py -> C:\oko\migrate_pending_tasks.py" -ForegroundColor Green
}

# --- 7. security_tools\update_manifest.ps1 (с переименованием) ---
Write-Host "`n--- Создание C:\oko\security_tools\ ---" -ForegroundColor Cyan
New-Item -ItemType Directory -Path "C:\oko\security_tools" -Force | Out-Null
$manifestSource = Find-ExtractedFile -FileName "security_tools_update_manifest.ps1"
if ($manifestSource) {
    Copy-Item -Path $manifestSource -Destination "C:\oko\security_tools\update_manifest.ps1" -Force
    Write-Host "  OK: security_tools_update_manifest.ps1 -> C:\oko\security_tools\update_manifest.ps1 (переименован)" -ForegroundColor Green
}

# --- 8. Итоговая сверка ---
Write-Host "`n=== Итоговая проверка результата ===" -ForegroundColor Cyan
$expectedFiles = @(
    "C:\oko\src\governor.py",
    "C:\oko\src\bot.py",
    "C:\oko\src\knowledge_indexer.py",
    "C:\oko\src\process_watchdog.py",
    "C:\oko\src\cli_patch.py",
    "C:\oko\src\static_analyzer.py",
    "C:\oko\src\test_static_analyzer.py",
    "C:\oko\src\staging_manager.py",
    "C:\oko\src\staging_repository.py",
    "C:\oko\oko_rag\indexer.py",
    "C:\oko\migrate_pending_tasks.py",
    "C:\oko\security_tools\update_manifest.ps1"
)

$allOk = $true
foreach ($f in $expectedFiles) {
    if (Test-Path $f) {
        $info = Get-Item $f
        $modTime = $info.LastWriteTime
        Write-Host "  [OK] $f  (izmenen: $modTime)" -ForegroundColor Green
    } else {
        Write-Host "  [OTSUTSTVUET] $f" -ForegroundColor Red
        $allOk = $false
    }
}

if ($allOk) {
    Write-Host "`n=== Все файлы на месте. Можно переходить к Шагу 3 (миграция). ===" -ForegroundColor Cyan
} else {
    Write-Host "`n=== ВНИМАНИЕ: не все файлы скопированы — проверь вывод выше. ===" -ForegroundColor Red
}

# --- 9. Очистка временной папки ---
Remove-Item $extractDir -Recurse -Force
Write-Host "`nВременная папка распаковки удалена." -ForegroundColor Gray
