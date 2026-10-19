# Create desktop shortcuts for the desktop pet.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\make-shortcuts.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\make-shortcuts.ps1 -Root "D:\elsewhere"
#
# Creates two shortcuts:
#   - "Desktop Pet"           hidden console, for daily use
#   - "Desktop Pet (Debug)"   keeps the console, for troubleshooting
#
# Why a debug variant: the pet is a long-running process. If we only shipped the
# hidden-console version, a failed launch would look like "double-clicked, nothing
# happened" with no error text at all.
#
# IMPORTANT: keep this file ASCII-only.
# Windows PowerShell 5.1 reads .ps1 files using the system OEM codepage (GBK on
# Chinese Windows) when there is no BOM, so non-ASCII text here gets mangled into
# garbage -- including inside strings, which then breaks variables and messages.
# Chinese shortcut names are therefore built with [char] escapes below.

param(
    [string]$Root = (Split-Path -Parent $PSScriptRoot)
)

$ErrorActionPreference = 'Stop'

# Shortcut display names, built from code points so this file stays ASCII.
$nameMain = [string]([char]0x684C) + [char]0x9762 + [char]0x5BA0 + [char]0x7269
# 桌面宠物
$nameDebug = $nameMain + [string]([char]0xFF08) + [char]0x8C03 + [char]0x8BD5 + [char]0xFF09
# 桌面宠物（调试）

$Root = (Resolve-Path $Root).Path
$devCmd = Join-Path $Root 'dev.cmd'
# 启动宠物.vbs
$vbsName = [string]([char]0x542F) + [char]0x52A8 + [char]0x5BA0 + [char]0x7269 + '.vbs'
$vbs = Join-Path $Root $vbsName
$electron = Join-Path $Root 'apps\desktop\node_modules\electron\dist\electron.exe'
$desktop = [Environment]::GetFolderPath('Desktop')

Write-Host "Project root : $Root"
Write-Host "Desktop      : $desktop"
Write-Host "VBS launcher : $vbs"
Write-Host ''

foreach ($required in @($devCmd, $vbs)) {
    if (-not (Test-Path $required)) {
        throw "Missing required file: $required"
    }
}

# Icon: prefer the character-based .ico generated from the art assets, so the
# shortcut is recognisable next to everything else on the desktop. Falls back to
# Electron's own icon (guaranteed to exist) and then to a shell32 icon.
$trayIcon = Join-Path $Root 'apps\desktop\assets\tray.ico'
if (Test-Path $trayIcon) {
    $icon = "$trayIcon,0"
    Write-Host "Icon: character icon ($trayIcon)"
} elseif (Test-Path $electron) {
    $icon = "$electron,0"
    Write-Host "Icon: Electron default (run tools\make-tray-icon.py for the character icon)"
} else {
    $icon = "$env:SystemRoot\System32\shell32.dll,13"
    Write-Host "Icon: shell32 fallback"
}

function New-PetShortcut {
    param(
        [string]$Name,
        [string]$Target,
        [string]$Arguments,
        [string]$WorkingDirectory,
        [string]$Description
    )

    $path = Join-Path $desktop "$Name.lnk"
    if (Test-Path $path) {
        Remove-Item $path -Force
        Write-Host "  overwriting existing: $Name.lnk"
    }

    $shell = New-Object -ComObject WScript.Shell
    $lnk = $shell.CreateShortcut($path)
    $lnk.TargetPath = $Target
    $lnk.Arguments = $Arguments
    $lnk.WorkingDirectory = $WorkingDirectory
    $lnk.Description = $Description
    $lnk.IconLocation = $icon
    $lnk.WindowStyle = 1
    $lnk.Save()

    # Release the COM object so the file is not left locked.
    [void][Runtime.InteropServices.Marshal]::ReleaseComObject($shell)

    Write-Host "  created: $path"
    return $path
}

Write-Host 'Creating shortcuts:'

$wscript = Join-Path $env:SystemRoot 'System32\wscript.exe'
$cmdExe = Join-Path $env:SystemRoot 'System32\cmd.exe'

$main = New-PetShortcut `
    -Name $nameMain `
    -Target $wscript `
    -Arguments "`"$vbs`"" `
    -WorkingDirectory $Root `
    -Description 'Desktop AI pet (hidden console)'

$debug = New-PetShortcut `
    -Name $nameDebug `
    -Target $cmdExe `
    -Arguments "/k `"`"$devCmd`"`"" `
    -WorkingDirectory $Root `
    -Description 'Desktop AI pet - keeps console output for troubleshooting'

Write-Host ''
Write-Host 'Verifying (reading the shortcuts back):'
foreach ($path in @($main, $debug)) {
    $shell = New-Object -ComObject WScript.Shell
    $lnk = $shell.CreateShortcut($path)
    Write-Host ("  {0}" -f (Split-Path -Leaf $path))
    Write-Host ("    target    = {0}" -f $lnk.TargetPath)
    Write-Host ("    arguments = {0}" -f $lnk.Arguments)
    Write-Host ("    workdir   = {0}" -f $lnk.WorkingDirectory)
    Write-Host ("    icon      = {0}" -f $lnk.IconLocation)
    [void][Runtime.InteropServices.Marshal]::ReleaseComObject($shell)
}

Write-Host ''
Write-Host 'Done. Double-click the shortcut on your desktop to launch the pet.'
