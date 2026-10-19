# 全自动测：让**当前正在跑的监视器**失焦，看它的轮询循环有没有被节流。
#
# ## 为什么要自动化（2026-10-08）
#
# 手动版（`test-ptt-cpu-throttle.ps1`）要用户配合点别的应用，容易漏。
# 这个版本**自己开一个记事本并激活它**，所以"失焦"这个条件是受控的：
#
#   前半段：宠物持有焦点（基线）
#   后半段：记事本被激活（真正失焦）
#
# 对比两段的 **CPU 时间增速** —— 循环被节流会立刻体现在这个数上。
#
# 用法：`powershell -File tools/test-ptt-throttle-auto.ps1`

param(
    [int]$IntervalMs = 2000,
    [int]$Rounds = 10   # 前 5 轮基线，后 5 轮失焦
)

$ErrorActionPreference = 'Continue'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$sig = @'
using System;
using System.Runtime.InteropServices;
using System.Text;
public static class FgA {
    [DllImport("user32.dll")]
    public static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    public static extern int GetWindowTextW(IntPtr hWnd, StringBuilder text, int count);
    public static string Title() {
        var sb = new StringBuilder(160);
        GetWindowTextW(GetForegroundWindow(), sb, 160);
        return sb.ToString();
    }
}
'@
Add-Type -TypeDefinition $sig -ErrorAction Stop

$watch = Get-CimInstance Win32_Process -Filter "Name = 'powershell.exe'" |
    Where-Object { $_.CommandLine -and $_.CommandLine -match 'probe-key' } |
    Select-Object -First 1
if (-not $watch) { Write-Host "  ❌ 没找到正在跑的 probe-key 监视器"; exit 1 }

$monPid = $watch.ProcessId
Write-Host "  监视器 pid = $monPid  （$(($watch.CommandLine -split '-Vk ')[-1])）"
Write-Host ""

# 开记事本，但**先不激活**（前半段要宠物保持焦点）
$np = Start-Process notepad -PassThru
Start-Sleep -Seconds 2
Write-Host "  已起记事本 pid=$($np.Id)（暂不激活，前 $($Rounds/2) 轮是基线）"
Write-Host ""

$sw = New-Object -ComObject WScript.Shell
$prev = (Get-Process -Id $monPid).TotalProcessorTime.TotalMilliseconds
$baseline = @()
$throttled = @()

for ($i = 1; $i -le $Rounds; $i++) {
    if ($i -eq [math]::Floor($Rounds / 2) + 1) {
        # 切换点：激活记事本 -> 宠物失焦
        [void]$sw.AppActivate($np.Id)
        Start-Sleep -Milliseconds 600
        Write-Host "  --- 已激活记事本（宠物应当失焦了）---"
    }
    Start-Sleep -Milliseconds $IntervalMs
    try { $p = Get-Process -Id $monPid -ErrorAction Stop } catch { Write-Host "  ⚠️ 监视器进程消失了"; break }
    $now = $p.TotalProcessorTime.TotalMilliseconds
    $delta = [math]::Round($now - $prev, 1)
    $prev = $now
    $fg = [FgA]::Title()
    if ($fg.Length -gt 24) { $fg = $fg.Substring(0, 24) }
    $phase = if ($i -le [math]::Floor($Rounds / 2)) { '基线' } else { '失焦' }
    Write-Host ("  [{0}] CPU 增量 {1,7} ms / {2}ms   前台={3}" -f $phase, $delta, $IntervalMs, $fg)
    if ($phase -eq '基线') { $baseline += $delta } else { $throttled += $delta }
}

Write-Host ""
$avgB = if ($baseline.Count) { [math]::Round(($baseline | Measure-Object -Average).Average, 1) } else { 0 }
$avgT = if ($throttled.Count) { [math]::Round(($throttled | Measure-Object -Average).Average, 1) } else { 0 }
Write-Host "  基线平均 CPU 增量 = $avgB ms / 失焦平均 = $avgT ms"
Write-Host ""
if ($avgB -gt 0 -and $avgT -lt $avgB * 0.5) {
    Write-Host "  ✅ **确认**：失焦后监视器循环被节流（CPU 增速掉到一半以下）"
} elseif ($avgT -eq 0 -and $baseline.Count -gt 0) {
    Write-Host "  ✅ **确认**：失焦后监视器**完全停止**（CPU 增量为 0）"
} else {
    Write-Host "  ⚠️ 没看出节流（两段接近）-> 问题不在监视器的循环速度"
}

try { if (-not $np.HasExited) { $np.Kill() } } catch { }
