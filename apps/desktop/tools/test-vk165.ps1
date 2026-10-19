# 直接测 `probe-key.ps1 -Vk 165` 在**右 Alt** 上到底会不会输出 DOWN/UP。
#
# ## 为什么（2026-10-08）
#
# 应用日志里**从来没有「按下」**（连用户物理按键也没有）—— 说明
# 监视器 -> 主进程 这一段断了。而前面已证明监视器**对 A 键是好的**
# （`tools/test-ptt-unfocused.ps1` 里收到了 `DOWN 65 / UP 65`）。
#
# 所以怀疑点收窄到：**VK 0xA5（右 Alt）这个键本身**。
#
# 这个脚本把监视器的原始 stdout 抓下来，同时用 **keybd_event 合成**和
# **真实手指按**两条路各试一次，直接看它输出什么。
#
# 用法：`powershell -File tools/test-vk165.ps1`

param(
    [int]$Vk = 165,
    [int]$Seconds = 12
)

$ErrorActionPreference = 'Continue'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$probe = Join-Path $PSScriptRoot 'probe-key.ps1'
if (-not (Test-Path $probe)) { Write-Host "  ❌ 找不到 $probe"; exit 1 }

$sig = @'
using System;
using System.Runtime.InteropServices;
using System.Text;
public static class VK {
    [DllImport("user32.dll")]
    public static extern short GetAsyncKeyState(int vKey);
    [DllImport("user32.dll")]
    public static extern void keybd_event(byte bVk, byte bScan, uint dwFlags, UIntPtr dwExtraInfo);
    [DllImport("user32.dll")]
    public static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    public static extern int GetWindowTextW(IntPtr hWnd, StringBuilder text, int count);
    public static string Fg() {
        var sb = new StringBuilder(120);
        GetWindowTextW(GetForegroundWindow(), sb, 120);
        return sb.ToString();
    }
}
'@
Add-Type -TypeDefinition $sig -ErrorAction Stop

$tmpOut = Join-Path $env:TEMP "vk165-out-$PID.txt"
$tmpErr = Join-Path $env:TEMP "vk165-err-$PID.txt"
Remove-Item $tmpOut, $tmpErr -Force -ErrorAction SilentlyContinue

Write-Host "=== 起监视器：probe-key.ps1 -Vk $Vk ==="
$p = Start-Process powershell -PassThru -WindowStyle Hidden `
    -RedirectStandardOutput $tmpOut -RedirectStandardError $tmpErr `
    -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $probe, '-Vk', "$Vk", '-PollMs', '15')
Write-Host "  pid = $($p.Id)"
Start-Sleep -Seconds 2
Write-Host "  启动输出：$((Get-Content $tmpOut -Raw -ErrorAction SilentlyContinue))"
Write-Host ""

# 本进程直接读同一个键做对照
Write-Host "=== 对照：本进程直接读 GetAsyncKeyState($Vk) ==="
Write-Host "  现在（没人按键）最高位：$(('{0:X4}' -f ([int][VK]::GetAsyncKeyState($Vk) -band 0xFFFF)))"
Write-Host ""

# ---------------- ① 合成按键 ----------------
Write-Host "=== ① 用 keybd_event 合成「右 Alt」按住 600ms ==="
Write-Host "  前台窗口：$([VK]::Fg())"
[VK]::keybd_event([byte]$Vk, 0, 0x0001, [UIntPtr]::Zero)          # 扩展键按下
for ($i = 0; $i -lt 12; $i++) {
    $st = ('{0:X4}' -f ([int][VK]::GetAsyncKeyState($Vk) -band 0xFFFF))
    Write-Host "    按住中 第$($i+1)次采样 GetAsyncKeyState = $st"
    Start-Sleep -Milliseconds 50
}
[VK]::keybd_event([byte]$Vk, 0, 0x0001 -bor 0x0002, [UIntPtr]::Zero)  # 松开
Write-Host "  已松开"
Start-Sleep -Milliseconds 600

Write-Host ""
Write-Host "=== ② 现在请**用真实手指按住右 Alt 3 秒**（脚本在等）==="
$sw = [System.Diagnostics.Stopwatch]::StartNew()
$seen = $false
while ($sw.Elapsed.TotalSeconds -lt 8) {
    $st = [int][VK]::GetAsyncKeyState($Vk)
    if (($st -band 0x8000) -ne 0) {
        Write-Host "  ✅ 本进程看到真实按下（t=$([math]::Round($sw.Elapsed.TotalSeconds,1))s）"
        $seen = $true
        break
    }
    Start-Sleep -Milliseconds 20
}
if (-not $seen) { Write-Host "  （8 秒内没检测到真实按下 —— 没按或这键不是 VK $Vk）" }
Start-Sleep -Seconds 1

# ---------------- 收监视器的输出 ----------------
Write-Host ""
Write-Host "=== 监视器的原始 stdout ==="
$out = Get-Content $tmpOut -ErrorAction SilentlyContinue
if ($out) { $out | ForEach-Object { Write-Host "    $_" } } else { Write-Host "    （空）" }
$errText = Get-Content $tmpErr -Raw -ErrorAction SilentlyContinue
if ($errText) { Write-Host "    stderr: $($errText.Substring(0, [Math]::Min(300, $errText.Length)))" }

try { $p.Kill() } catch { }
Remove-Item $tmpOut, $tmpErr -Force -ErrorAction SilentlyContinue

Write-Host ""
Write-Host "=== 判读 ==="
Write-Host "  · 监视器输出了 DOWN/UP -> 监视器没问题，断点在 Node 侧的解析/回调"
Write-Host "  · 只有 READY、本进程却看得到按下 -> **监视器收不到这个键**（VK/扩展键问题）"
Write-Host "  · 本进程也看不到按下 -> 这个键根本没送到任何进程（键盘/系统层面的问题）"
