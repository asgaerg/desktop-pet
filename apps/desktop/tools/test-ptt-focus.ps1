# 实测：别的应用聚焦时，probe-key 的轮询循环**还在跑吗**？
#
# ## 为什么要这么测（2026-10-08）
#
# 用户反馈：「如果聚焦不在宠物身上，点击其他应用，按下按键没用」。
#
# `GetAsyncKeyState` 是**全局** API，与焦点无关 —— 所以问题只可能是
# **轮询循环停了**（子进程被杀 / 线程被挂起 / 定时器被节流）。
#
# 这个脚本做**受控实验**：监视器是自己起的（同一个 shell 里），
# 所以能同时看到"监视器的输出"和"谁在前台"。
#
# 跑法：`powershell -File tools/test-ptt-focus.ps1`
# 它会监视右 Alt 15 秒，期间请：
#   ① 对着这个窗口按一下右 Alt
#   ② 点开别的应用（记事本/浏览器），在那边再按一下右 Alt
#
# 两份输出都会带上前台窗口标题，一眼就能看出是不是焦点相关。

param(
    [int]$Vk = 0xA5,
    [int]$Seconds = 15
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$sig = @'
using System;
using System.Runtime.InteropServices;
using System.Text;
public static class Fg {
    [DllImport("user32.dll")]
    public static extern short GetAsyncKeyState(int vKey);
    [DllImport("user32.dll")]
    public static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    public static extern int GetWindowTextW(IntPtr hWnd, StringBuilder text, int count);
    [DllImport("user32.dll")]
    public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint pid);
    public static string Title() {
        var sb = new StringBuilder(256);
        IntPtr h = GetForegroundWindow();
        GetWindowTextW(h, sb, 256);
        uint pid;
        GetWindowThreadProcessId(h, out pid);
        return pid + ":" + sb.ToString();
    }
}
'@
Add-Type -TypeDefinition $sig -ErrorAction Stop

$probe = Join-Path $PSScriptRoot 'probe-key.ps1'
if (-not (Test-Path $probe)) { Write-Host "找不到 $probe"; exit 1 }

Write-Host "=== 监视右 Alt（VK 0x$('{0:X}' -f $Vk)）$Seconds 秒 ==="
Write-Host "请：① 先在这个窗口按一下右 Alt"
Write-Host "    ② 再点开别的应用（如记事本），在那边按一下右 Alt"
Write-Host ""

# 用 .NET 直接起进程，**不重定向 stdout**（保持继承，避免管道问题）
$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName = 'powershell.exe'
$psi.Arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$probe`" -Vk $Vk -PollMs 15"
$psi.UseShellExecute = $false
$psi.RedirectStandardOutput = $false
$psi.RedirectStandardError = $false
$psi.CreateNoWindow = $true

$child = [System.Diagnostics.Process]::Start($psi)
Write-Host "  监视器 pid = $($child.Id)"
Write-Host ""

$wasDown = $false
$t0 = Get-Date
$ticks = 0
while (((Get-Date) - $t0).TotalSeconds -lt $Seconds) {
    $state = [int][Fg]::GetAsyncKeyState($Vk)
    $down = ($state -band 0x8000) -ne 0
    if ($down -ne $wasDown) {
        $fg = [Fg]::Title()
        $mark = if ($down) { 'DOWN' } else { 'UP  ' }
        Write-Host ("  [{0:HH:mm:ss.fff}] {1}   前台窗口 = {2}" -f (Get-Date), $mark, $fg)
        $wasDown = $down
    }
    Start-Sleep -Milliseconds 15
    $ticks++
}

Write-Host ""
Write-Host "  本地轮询了 $ticks 次"
Write-Host "  子进程还活着吗：$(-not $child.HasExited)"
try { $child.Kill() } catch { }
Write-Host ""
Write-Host "=== 判读 ==="
Write-Host "  · 两次都看到 DOWN/UP -> 监视器与焦点无关（问题在别处）"
Write-Host "  · 只在**本窗口**聚焦时看到 -> 焦点相关，得查子进程/线程被挂起"
Write-Host "  · 一次都没看到 -> 右 Alt 被别的软件占了（或键盘没有右 Alt）"
