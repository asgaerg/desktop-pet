# 一次性诊断：**你按的键到底有没有被采到**，以及应用有没有接到。
#
# ## 为什么需要这个（2026-10-08）
#
# 应用日志里，**19:00:03 那次按下是成功的**（有「按下」、有闸门、有识别）。
# 之后几次按键**完全没有记录**。同一个应用、同一个监视器脚本，
# 差别只可能是"键没被采到"或"采到了但没传上去"。
#
# 这个脚本把**两件事同时测**，一次跑完就能分清：
#
#   ① **独立**起一个监视器（和无关的进程无关），列出它收到的所有键事件
#   ② 同时看**应用自己**的日志有没有新增「按下」
#
# 用法（关键：按提示操作，别提前按）：
#
#     powershell -File tools/diagnose-ptt-now.ps1
#
# 跑起来后会倒数 3 秒，然后进入 20 秒采样期 —— **请在这 20 秒内**：
#   · 先按一下**右 Alt**（按住 1 秒）
#   · 再按一下**左 Alt**（按住 1 秒）—— 用来区分"是右 Alt 这个键的问题"
#   · 再随便按个字母键（比如 A）—— 用来确认"监视机制本身是活的"

param(
    [int]$Vk = 165,          # 应用监听的是右 Alt
    [int]$Seconds = 20
)

$ErrorActionPreference = 'Continue'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$sig = @'
using System;
using System.Runtime.InteropServices;
using System.Text;
public static class Probe {
    [DllImport("user32.dll")] public static extern short GetAsyncKeyState(int vKey);
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    public static extern int GetWindowTextW(IntPtr hWnd, StringBuilder text, int count);
    public static string Fg() {
        var sb = new StringBuilder(100);
        GetWindowTextW(GetForegroundWindow(), sb, 100);
        var s = sb.ToString();
        return s.Length > 30 ? s.Substring(0, 30) : s;
    }
}
'@
Add-Type -TypeDefinition $sig -ErrorAction Stop

# 要看的一批键：右Alt(A5) 左Alt(A4) 右Ctrl(A3) A(41) 空格(20)
$keys = [ordered]@{
    '右 Alt (A5, 应用监听的)' = 0xA5
    '左 Alt (A4)'             = 0xA4
    'A (41)'                  = 0x41
    '空格 (20)'               = 0x20
}

$log = 'F:\ai项目\apps\desktop\dist\startup.log'
function AppPressCount {
    (Get-Content $log -Encoding UTF8 -ErrorAction SilentlyContinue |
        Select-String '按住说话\] 按下' | Measure-Object).Count
}

Write-Host "================================================================"
Write-Host " 按键诊断：本脚本采到的键  vs  应用收到的「按下」"
Write-Host "================================================================"
Write-Host ""
$appBefore = AppPressCount
Write-Host "  应用日志里的「按下」条数（开始前）= $appBefore"
Write-Host ""

Write-Host "  倒计时 3 秒，然后有 $Seconds 秒采样窗口。请按顺序按："
Write-Host "    ① 右 Alt（按住 1 秒）   ② 左 Alt（按住 1 秒）   ③ 字母 A"
Write-Host ""
for ($i = 3; $i -ge 1; $i--) { Write-Host "    $i…"; Start-Sleep -Seconds 1 }
Write-Host "    >>> 开始！现在按 <<<"
Write-Host ""

$wasDown = @{}
foreach ($k in $keys.Values) { $wasDown[$k] = $false }
$hits = @{}
foreach ($name in $keys.Keys) { $hits[$name] = 0 }

$sw = [System.Diagnostics.Stopwatch]::StartNew()
while ($sw.Elapsed.TotalSeconds -lt $Seconds) {
    foreach ($name in $keys.Keys) {
        $vk = $keys[$name]
        $down = (([int][Probe]::GetAsyncKeyState($vk)) -band 0x8000) -ne 0
        if ($down -ne $wasDown[$vk]) {
            $wasDown[$vk] = $down
            if ($down) { $hits[$name]++ }
            $mark = if ($down) { 'DOWN' } else { 'UP  ' }
            Write-Host ("  [{0,5:N1}s] {1}  {2}   前台={3}" -f $sw.Elapsed.TotalSeconds, $mark, $name, [Probe]::Fg())
        }
    }
    Start-Sleep -Milliseconds 12
}
Write-Host ""
Write-Host "=== 本脚本采到的按键次数 ==="
foreach ($name in $keys.Keys) { Write-Host ("  {0,-26} {1} 次" -f $name, $hits[$name]) }

Start-Sleep -Milliseconds 800
$appAfter = AppPressCount
Write-Host ""
Write-Host "  应用日志里的「按下」条数（结束后）= $appAfter（新增 $($appAfter - $appBefore)）"
Write-Host ""
Write-Host "=== 判读 ==="
if ($hits['右 Alt (A5, 应用监听的)'] -eq 0 -and $hits['左 Alt (A4)'] -gt 0) {
    Write-Host "  🔴 **你按的其实是左 Alt**（A4），而应用监听的是右 Alt（A5）——"
    Write-Host "     这就是「按下没用」的原因：键不对。"
    Write-Host "     修法：把配置里的 vk 改成 164（左 Alt），或在设置页重新录一次键。"
} elseif ($hits['右 Alt (A5, 应用监听的)'] -eq 0 -and $hits['左 Alt (A4)'] -eq 0) {
    Write-Host "  ⚠️ 左右 Alt 都没采到 —— 键盘上可能没有独立的右 Alt，"
    Write-Host "     或者按的时候没对准。看上面 A / 空格 有没有采到："
    Write-Host "     采到 -> 监视机制是活的，只是 Alt 收不到；没采到 -> 采样本身有问题。"
} elseif ($hits['右 Alt (A5, 应用监听的)'] -gt 0 -and ($appAfter - $appBefore) -gt 0) {
    Write-Host "  ✅ **完整链路通了**：右 Alt 被采到，应用也记下了「按下」"
} elseif ($hits['右 Alt (A5, 应用监听的)'] -gt 0 -and ($appAfter - $appBefore) -eq 0) {
    Write-Host "  🔴 **右 Alt 被采到了，但应用没记下「按下」** ——"
    Write-Host "     断点在「监视器 -> 主进程」。这是产品的 bug，请把这整段输出发我。"
} else {
    Write-Host "  看上面的明细。"
}
