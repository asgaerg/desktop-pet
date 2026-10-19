# 带心跳的按键监视器：用来判断**轮询循环有没有卡住**。
#
# ## 为什么需要它（2026-10-08）
#
# 用户反馈：「聚焦不在宠物身上、点了别的应用之后，按下按键没用」。
#
# 已排除的：
#   · `GetAsyncKeyState` 是全局 API，与焦点无关
#   · 监视器进程**确实还活着**（`probe-key.ps1 -Vk 165 -PollMs 15` 在进程表里）
#   · `petWindow` 的 `backgroundThrottling` 已经是 `false`
#
# 所以只剩一种可能：**循环本身停了或变慢了**。这个脚本每 N 次轮询打一个
# `TICK` 行（带时间戳与前台窗口标题），一眼就能看出：
#
#   · TICK 一直来、但按键没反应 -> 循环正常，问题在别处（IPC / 渲染层）
#   · TICK 停了               -> 循环被挂起/节流，找到真凶
#   · TICK 变慢很多           -> 被节流了（数值能看出来慢了还是停了）
#
# ## 用法
#
#     powershell -File tools/probe-key-heartbeat.ps1 -Vk 0xA5 -Seconds 30
#
# 跑起来之后：**先在本窗口按一下，再点开别的应用按一下** —— 对比两段的 TICK 间隔。

param(
    [int]$Vk = 0xA5,
    [int]$PollMs = 15,
    [int]$Seconds = 30,
    [int]$HeartbeatEvery = 20
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$sig = @'
using System;
using System.Runtime.InteropServices;
using System.Text;
public static class KB {
    [DllImport("user32.dll")]
    public static extern short GetAsyncKeyState(int vKey);
    [DllImport("user32.dll")]
    public static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    public static extern int GetWindowTextW(IntPtr hWnd, StringBuilder text, int count);
    public static string Fg() {
        var sb = new StringBuilder(128);
        GetWindowTextW(GetForegroundWindow(), sb, 128);
        var s = sb.ToString();
        return s.Length > 28 ? s.Substring(0, 28) : s;
    }
}
'@
Add-Type -TypeDefinition $sig -ErrorAction Stop

Write-Host "READY $Vk  （心跳每 $HeartbeatEvery 次轮询一条，共 $Seconds 秒）"
Write-Host ""

$wasDown = $false
$n = 0
$sw = [System.Diagnostics.Stopwatch]::StartNew()
$lastBeat = $sw.ElapsedMilliseconds

while ($sw.Elapsed.TotalSeconds -lt $Seconds) {
    $state = [int][KB]::GetAsyncKeyState($Vk)
    $down = ($state -band 0x8000) -ne 0

    if ($down -ne $wasDown) {
        $mark = if ($down) { 'DOWN' } else { 'UP  ' }
        Write-Host ("{0,7:N0}ms  {1}   fg={2}" -f $sw.ElapsedMilliseconds, $mark, [KB]::Fg())
        $wasDown = $down
    }

    $n++
    if ($n % $HeartbeatEvery -eq 0) {
        $now = $sw.ElapsedMilliseconds
        $gap = $now - $lastBeat
        $lastBeat = $now
        Write-Host ("{0,7:N0}ms  TICK #{1,-5} 上{2}次用时 {3}ms（期望约 {4}ms）  fg={5}" -f `
            $now, $n, $HeartbeatEvery, $gap, ($HeartbeatEvery * $PollMs), [KB]::Fg())
    }

    Start-Sleep -Milliseconds $PollMs
}

Write-Host ""
Write-Host "=== 判读 ==="
Write-Host "  · 每行「上20次用时」都接近期望值（如 300ms）-> 循环正常"
Write-Host "  · 某些行明显偏大（几秒）-> 循环被**节流/挂起**了"
Write-Host "  · DOWN/UP 只在某个 fg 下出现 -> 与前台窗口相关"
