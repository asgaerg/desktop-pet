# Freeze the Python sidecar (2026-10-18).
#
# Output: apps/brain/dist/pet/pet.exe  (onedir, NOT onefile)
#
# Why onedir: onefile unpacks 300MB+ to temp on every launch; the sidecar starts
# at boot, so that would add seconds. onedir is just a folder.
#
# Why CPU-only for v1: ctranslate2 dlopen()s CUDA libs at runtime, so PyInstaller
# cannot see them. A GPU build would need explicit --add-binary for nvidia/*/bin/*.dll.
# CPU build = smaller, runs anywhere.
#
# Usage (from repo root):
#   powershell -NoProfile -ExecutionPolicy Bypass -File build\sidecar.ps1
#
# NOTE: keep this file ASCII-only. Windows PowerShell 5.1 reads BOM-less .ps1
# files as ANSI; Chinese text then breaks the parser (hit that once already).

param(
  [switch]$Gpu
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$pythonExe = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $pythonExe)) {
  Write-Host "Missing venv python: $pythonExe"
  exit 1
}

$argsList = @(
  '--noconfirm', '--clean',
  '--onedir', '--name', 'pet',
  '--distpath', 'apps\brain\dist',
  '--workpath', 'build\pyinstaller\work',
  '--specpath', 'build\pyinstaller',
  '--paths', 'apps\brain',
  '--collect-all', 'onnxruntime',
  '--collect-all', 'ctranslate2',
  '--collect-all', 'edge_tts',
  '--collect-all', 'jieba',
  '--exclude-module', 'matplotlib',
  '--exclude-module', 'pandas',
  '--exclude-module', 'scipy',
  'build\pyinstaller\launch_pet.py'
)

Write-Host "Freezing sidecar (CPU build)..."
& '.\.venv\Scripts\python.exe' -m PyInstaller @argsList

if ($LASTEXITCODE -ne 0) {
  Write-Host "PyInstaller failed (exit $LASTEXITCODE)"
  exit $LASTEXITCODE
}

$exe = 'apps\brain\dist\pet\pet.exe'
if (-not (Test-Path $exe)) {
  Write-Host "Missing $exe"
  exit 1
}
$size = (Get-ChildItem 'apps\brain\dist\pet' -Recurse -File | Measure-Object Length -Sum).Sum
Write-Host ("OK: {0} ({1:N0} MB)" -f $exe, ($size / 1MB))
