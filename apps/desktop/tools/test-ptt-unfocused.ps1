# 自动测：「前台是别的应用」时，GetAsyncKeyState 轮询还收不收得到键？
#
# ## 为什么这样测（2026-10-08）
#
# 用户反馈：聚焦不在宠物身上时按 PTT 没用。已知监视器进程活着、API 与焦点无关，
# 所以要在**受控条件**下测：让**记事本保持前台**，再用 .NET 的 SendKeys
# 发一个真实按键，看监视器收不收得到。
#
# ⚠️ SendKeys 发的键**同一个 session 内是真实的**（走 Win32 输入队列），
# 所以 `GetAsyncKeyState` 应当能看到它 —— 这也是本次实验成立的前提。
# 如果连这个都收不到，说明问题比"焦点"更基础。

param(
    [int]$Vk = 0x41   # A 键（用一个不常用的键，避免干扰用户的右 Alt）
)

$ErrorActionPreference = 'Continue'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$sig = @'
using System;
using System.Runtime.InteropServices;
using System.Text;
public static class KB2 {
    [DllImport("user32.dll")]
    public static extern short GetAsyncKeyState(int vKey);
    [DllImport("user32.dll")]
    public static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    public static extern int GetWindowTextW(IntPtr hWnd, StringBuilder text, int count);
    public static string Fg() {
        var sb = new StringBuilder(128);
        GetWindowTextW(GetForegroundWindow(), sb, 128);
        return sb.ToString();
    }
}
'@
Add-Type -TypeDefinition $sig -ErrorAction Stop

$probe = Join-Path $PSScriptRoot 'probe-key.ps1'

# ---------------------------------------------------------------- 起记事本
Write-Host "=== ① 起一个记事本并让它保持前台 ==="
$np = Start-Process notepad -PassThru
Start-Sleep -Seconds 2
if ($np.HasExited) { Write-Host "  记事本立刻退出了（这台机器可能没有）"; }
$fg = [KB2]::Fg()
Write-Host "  当前前台窗口：$fg"
$npFocus = $fg -like '*记事本*' -or $fg -like '*Notepad*'
Write-Host "  前台是记事本吗：$npFocus"
Write-Host ""

# ------------------------------------------------- 起监视器，读它的 stdout
Write-Host "=== ② 起监视器（读它的 stdout）==="
$tmpOut = Join-Path $env:TEMP "probe-out-$PID.txt"
$tmpErr = Join-Path $env:TEMP "probe-err-$PID.txt"
$p = Start-Process powershell -PassThru -WindowStyle Hidden `
    -RedirectStandardOutput $tmpOut -RedirectStandardError $tmpErr `
    -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $probe, '-Vk', $Vk, '-PollMs', '15')
Write-Host "  监视器 pid = $($p.Id)"
Start-Sleep -Seconds 2
Write-Host "  启动输出：$((Get-Content $tmpOut -Raw -ErrorAction SilentlyContinue))"
Write-Host ""

# --------------------------------------- 在记事本仍在前台时发一个真实按键
Write-Host "=== ③ 在**记事本仍在前台**时发一个真实按键（'a'）==="
$fgBefore = [KB2]::Fg()
Write-Host "  发键前的前台窗口：$fgBefore"

# SendKeys 需要一个有消息循环的前台窗口 —— 记事本正好是
[System.Windows.Forms.SendKeys]::SendWait('{a}') 2>$null
if (-not $?) {
    Add-Type -AssemblyName System.Windows.Forms -ErrorAction SilentlyContinue
    [System.Windows.Forms.SendKeys]::SendWait('{a}')
}
Start-Sleep -Milliseconds 300
Write-Host "  已发出 'a'（前台仍是：$([KB2]::Fg())）"
Write-Host ""

# ------------------------------------------------------------ 收结果
Start-Sleep -Seconds 1
Write-Host "=== ④ 监视器的完整输出 ==="
$out = Get-Content $tmpOut -ErrorAction SilentlyContinue
if ($out) { $out | ForEach-Object { Write-Host "    $_" } } else { Write-Host "    （空）" }
$errText = Get-Content $tmpErr -Raw -ErrorAction SilentlyContinue
if ($errText) { Write-Host "    stderr: $($errText.Substring(0, [Math]::Min(200, $errText.Length)))" }
Write-Host ""

# ------------------------------------------------------------ 清理
try { $p.Kill() } catch { }
try { if (-not $np.HasExited) { $np.Kill() } } catch { }
Remove-Item $tmpOut, $tmpErr -Force -ErrorAction SilentlyContinue

Write-Host "=== 判读 ==="
Write-Host "  看到 DOWN/UP -> 前台是别的应用时**照样收得到**，问题在应用层（IPC/渲染）"
Write-Host "  只有 READY   -> **收不到**，问题在按键监视这一层"
