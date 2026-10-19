# Promote the pet's tray icon to the always-visible area of the taskbar.
#
# Why this is needed: Windows 11 puts newly appearing tray icons into the
# overflow ("hidden icons") flyout by default, and only remembers a choice once
# the user drags the icon out. Until then the icon is recorded in
#   HKCU\Control Panel\NotifyIconSettings\<key>
# WITHOUT an IsPromoted value, which means "hidden".
#
# That is exactly what happened here: the entry existed, the icon snapshot was
# saved, but there was no IsPromoted -- so the icon lived in the overflow and the
# user could not find the only way to quit the pet (its window is skipTaskbar).
#
# This script finds the entry whose ExecutablePath matches the pet's Electron
# binary and sets IsPromoted = 1, which is equivalent to dragging the icon onto
# the taskbar. It is per-user (HKCU) and reversible: set it back to 0 or just
# drag the icon back into the overflow.
#
# IMPORTANT: keep this file ASCII-only (Windows PowerShell 5.1 reads .ps1 using
# the OEM codepage when there is no BOM).

$ErrorActionPreference = 'Stop'

$key = 'HKCU:\Control Panel\NotifyIconSettings'
if (-not (Test-Path $key)) {
    Write-Host "No NotifyIconSettings key - this Windows build does not use it."
    Write-Host "Promote the icon manually: click the '^' arrow in the taskbar and drag"
    Write-Host "the pet icon onto the taskbar."
    exit 1
}

# Find every entry that points at the pet's Electron binary.
$matches = @()
foreach ($child in Get-ChildItem $key) {
    $props = Get-ItemProperty -Path $child.PSPath -ErrorAction SilentlyContinue
    if (-not $props) { continue }
    $exe = [string]$props.ExecutablePath
    # 注意用反斜杠：ExecutablePath 是 Windows 路径，
    # 早先这里写成正斜杠 '*' 匹配不上，脚本会报"找不到条目"（实际存在）。
    if ($exe -match '\\electron\.exe$' -and ($exe -match 'node_modules' -or $exe -match 'ai项目')) {
        $matches += [PSCustomObject]@{
            Key = $child.PSPath
            Id  = $child.PSChildName
            Exe = $exe
            Promoted = $props.IsPromoted
        }
    }
}

if ($matches.Count -eq 0) {
    Write-Host "No tray entry found for the pet's Electron binary."
    Write-Host "Start the pet at least once, then run this script again."
    Write-Host ''
    Write-Host "Meanwhile you can promote it manually: click the '^' arrow in the"
    Write-Host "taskbar and drag the pet icon out onto the taskbar."
    exit 1
}

Write-Host "Found $($matches.Count) tray entry/entries for the pet:"
foreach ($m in $matches) {
    Write-Host "  id=$($m.Id)"
    Write-Host "    exe      = $($m.Exe)"
    Write-Host "    promoted = $(if ($null -eq $m.Promoted) { '(absent = hidden)' } else { $m.Promoted })"
}
Write-Host ''

foreach ($m in $matches) {
    New-ItemProperty -Path $m.Key -Name 'IsPromoted' -Value 1 -PropertyType DWord -Force | Out-Null
    $check = (Get-ItemProperty -Path $m.Key).IsPromoted
    Write-Host "  set IsPromoted=1 on $($m.Id)  (read back: $check)"
}

Write-Host ''
Write-Host 'Restarting Explorer so the change takes effect...'
Stop-Process -Name explorer -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 4
if (-not (Get-Process explorer -ErrorAction SilentlyContinue)) {
    # Explorer does not always auto-restart when killed programmatically; make sure
    # the user is not left without a taskbar.
    Write-Host '  Explorer did not come back on its own - starting it...'
    Start-Process explorer.exe
    Start-Sleep -Seconds 2
}
Write-Host ''
Write-Host 'Done. The pet icon should now sit in the always-visible tray area.'
Write-Host 'If you would rather keep the tray clean: right-click the icon -> Quit, then'
Write-Host 'drag it back into the overflow (Windows remembers that too).'
