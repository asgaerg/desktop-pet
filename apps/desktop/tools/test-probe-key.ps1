# 验证 probe-key.ps1 真能检测到按键 —— 用合成按键给自己发一个。
#
# ⚠️ 为什么必须验：`GetAsyncKeyState` 有个经典陷阱 —— 它对**合成按键**
# （`keybd_event` / `SendInput`）的响应与真实按键**不完全一致**，而且
# 在会话隔离（服务/沙箱/远程桌面）下可能完全读不到。
# 不验就往上搭 UI，最后会发现"按了没反应"却查不出是哪一层断的。

param(
    [int]$Vk = 0x41,      # 默认 A
    [int]$HoldMs = 400
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$sig = @'
using System;
using System.Runtime.InteropServices;
public static class KeySim {
    [DllImport("user32.dll")]
    public static extern void keybd_event(byte bVk, byte bScan, uint dwFlags, UIntPtr dwExtraInfo);
    public const uint KEYEVENTF_KEYUP = 0x0002;
}
'@
Add-Type -TypeDefinition $sig -ErrorAction Stop

$probe = Join-Path $PSScriptRoot 'probe-key.ps1'
Write-Host "启动监视器（VK=$Vk）…"

# 把监视器的输出接到一个临时文件，避免管道缓冲把它憋住
$out = Join-Path $env:TEMP "keyprobe-$PID.txt"
if (Test-Path $out) { Remove-Item $out -Force }

$proc = Start-Process -FilePath 'powershell' `
    -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $probe, '-Vk', "$Vk", '-PollMs', '10') `
    -RedirectStandardOutput $out -NoNewWindow -PassThru

Start-Sleep -Milliseconds 1200
Write-Host "发送合成按键（按住 ${HoldMs}ms）…"
[KeySim]::keybd_event([byte]$Vk, 0, 0, [UIntPtr]::Zero)
Start-Sleep -Milliseconds $HoldMs
[KeySim]::keybd_event([byte]$Vk, 0, [KeySim]::KEYEVENTF_KEYUP, [UIntPtr]::Zero)
Start-Sleep -Milliseconds 800

if (-not $proc.HasExited) { $proc.Kill() }
Start-Sleep -Milliseconds 200

Write-Host "=== 监视器输出 ==="
if (Test-Path $out) {
    Get-Content $out | ForEach-Object { Write-Host "  $_" }
    $lines = Get-Content $out
    $down = @($lines | Where-Object { $_ -like 'DOWN*' }).Count
    $up = @($lines | Where-Object { $_ -like 'UP*' }).Count
    Write-Host ""
    if ($down -ge 1 -and $up -ge 1) {
        Write-Host "结果：✅ 检测到 DOWN $down 次 / UP $up 次"
    } else {
        Write-Host "结果：❌ 没检测到（DOWN=$down UP=$up）"
    }
} else {
    Write-Host "  ⚠️ 没有输出文件"
}
Remove-Item $out -Force -ErrorAction SilentlyContinue
