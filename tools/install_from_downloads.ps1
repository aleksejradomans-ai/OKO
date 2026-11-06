# PATH: C:\oko\tools\install_from_downloads.ps1
# Moves delivered files from the real Downloads folder into C:\oko by SHA256.
# All-or-nothing: if any file is missing / hash differs -> nothing is moved, exit 2.
# Accepts browser duplicates "name (1).ext"; picks the newest copy with the right hash.
# If the target already has the right hash -> ALREADY (no-op). Existing target -> backup
# copy into _quarantine_<yyyyMMdd>\<name>.bak_<HHmmss> before replacing.
#
# Usage (Spec = "file|relative\target|HASH16" separated by ';'):
#   powershell -NoProfile -ExecutionPolicy Bypass -File C:\oko\tools\install_from_downloads.ps1 `
#     -Spec "a.py|src\a.py|0123456789ABCDEF;b.py|b.py|FEDCBA9876543210"
# Exit code: 0 OK, 2 STOP (nothing moved), 3 bad arguments.

param(
    [string]$Spec = '',
    [string]$Root = 'C:\oko',
    [string]$Downloads = ''
)

if (-not $Spec.Trim()) { 'usage: -Spec "file|relative\target|HASH16;..."'; exit 3 }

$dl = $Downloads
if (-not $dl) {
    try { $dl = (New-Object -ComObject Shell.Application).NameSpace('shell:Downloads').Self.Path }
    catch { $dl = Join-Path $env:USERPROFILE 'Downloads' }
}
if (-not (Test-Path -LiteralPath $dl)) { "Downloads folder not found: $dl"; exit 3 }
$quar = Join-Path $Root ('_quarantine_' + (Get-Date -Format 'yyyyMMdd'))
"Downloads: $dl"

$plan = @()
$bad = $false
foreach ($item in ($Spec -split ';' | Where-Object { $_.Trim() })) {
    $parts = $item.Trim() -split '\|'
    if ($parts.Count -ne 3) { "ARGS     bad item: $item"; exit 3 }
    $name = $parts[0].Trim()
    $rel = $parts[1].Trim()
    $want = $parts[2].Trim().ToUpper()
    if ($want.Length -lt 16) { "ARGS     hash too short: $item"; exit 3 }
    $dest = Join-Path $Root $rel

    if ((Test-Path -LiteralPath $dest) -and
        (Get-FileHash -LiteralPath $dest -Algorithm SHA256).Hash.StartsWith($want)) {
        "ALREADY  $rel"
        continue
    }

    $base = [IO.Path]::GetFileNameWithoutExtension($name)
    $ext = [IO.Path]::GetExtension($name)
    $rx = '^' + [regex]::Escape($base) + '( \(\d+\))?' + [regex]::Escape($ext) + '$'
    $cands = @(Get-ChildItem -LiteralPath $dl -File -ErrorAction SilentlyContinue |
               Where-Object { $_.Name -match $rx } |
               Sort-Object LastWriteTime -Descending)
    $hit = $cands | Where-Object {
        (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.StartsWith($want)
    } | Select-Object -First 1

    if (-not $hit) {
        "BAD      $name  (candidates in Downloads: $($cands.Count), none with hash $want)"
        foreach ($c in $cands) {
            "           {0}  {1}" -f (Get-FileHash -LiteralPath $c.FullName -Algorithm SHA256).Hash.Substring(0, 16), $c.Name
        }
        $bad = $true
        continue
    }
    "READY    $($hit.Name) -> $rel"
    $plan += [pscustomobject]@{ Src = $hit.FullName; Dest = $dest; Rel = $rel }
}

if ($bad) { 'RESULT: STOP, nothing moved'; exit 2 }

foreach ($p in $plan) {
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $p.Dest) | Out-Null
    if (Test-Path -LiteralPath $p.Dest) {
        New-Item -ItemType Directory -Force -Path $quar | Out-Null
        $bak = Join-Path $quar ((Split-Path -Leaf $p.Dest) + '.bak_' + (Get-Date -Format 'HHmmss'))
        Copy-Item -LiteralPath $p.Dest -Destination $bak -Force
        "BACKUP   $($p.Rel) -> $bak"
    }
    Move-Item -LiteralPath $p.Src -Destination $p.Dest -Force
    "MOVED    $($p.Rel)"
}
'RESULT: OK'
exit 0
