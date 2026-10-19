# 常驻按键监视器：检测**单个键**的按下与松开，输出到 stdout。
#
# ## 为什么不用 SetWindowsHookEx（低级键盘钩子）
#
# 那个方案需要一条消息泵 + 回调委托，在 PowerShell 里跨线程回调很容易被 GC
# 回收掉（表现为"钩子装上了但收不到事件"）。而**轮询 GetAsyncKeyState** 足够：
# 按住说话的延迟容忍度是几十毫秒，15ms 的轮询完全够用，而且实现简单、不会
# 因为回调生命周期问题静默失效。
#
# ## 用法
#
#     powershell -File probe-key.ps1 -Vk 0x41        # A 键
#     powershell -File probe-key.ps1 -Vk 0xA5 -PollMs 15
#
# 输出（每行一条，便于 Node 侧按行解析）：
#
#     DOWN <vk>
#     UP <vk>
#     READY <vk>        <- 首行，告诉你监视器起来了
#
# ## ⚠️ 为什么必须显式指定虚拟键码（VK）
#
# 不能用"字符"当参数：同一个人按 A 键，在大写锁定/按住 Shift 时字符不同，
# 而 VK 是**物理键**的标识，与修饰键无关 —— 这正是"按住说话"需要的语义。
#
# ⚠️ 脚本必须带 UTF-8 BOM：PowerShell 5.1 对没有 BOM 的 .ps1 按 ANSI 解析，
#    中文注释会变乱码（见 fullscreenWatcher.ts 里同一条踩坑记录）。

# `-OutFile`：给了就**直接写文件**（不走 stdout）—— 见下面的长说明。
#
# ⚠️ **不要在 `param()` 块里插 `#` 注释行**（2026-10-08 踩过）：
# PowerShell 的参数列表里，注释会让**上一行末尾的逗号变成悬空逗号**，
# 直接报 `","后面缺少表达式` 而整个脚本起不来。注释只能放在 param 块**外面**。
param(
    [Parameter(Mandatory = $true)][int]$Vk,
    [int]$PollMs = 15,
    [string]$OutFile = ''
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$sig = @'
using System;
using System.Runtime.InteropServices;
public static class KeyPoll {
    [DllImport("user32.dll")]
    public static extern short GetAsyncKeyState(int vKey);
}
'@
Add-Type -TypeDefinition $sig -ErrorAction Stop

# --------------------------------------------------------------------------- #
# 输出方式：**文件直写优先**，没有 -OutFile 才用 stdout
# --------------------------------------------------------------------------- #
#
# ## ⚠️ 为什么必须支持直接写文件（2026-10-08，查了很久）
#
# 实测三件事叠在一起，最后只有"直写文件"这条路是通的：
#
#   ① Electron spawn 出来、stdout **走管道**的监视器：**一个键都收不到**
#      （连 `ready` 都只有一行）—— 而同一个脚本由 PowerShell 起就正常。
#      已排除：会话隔离、`-NonInteractive`、读键能力本身、轮询是否在跑。
#
#   ② 为了绕开管道，改成 `cmd /c label > out.txt`（文件重定向）：
#      **输出文件是 0 字节** —— PowerShell 在 stdout 被重定向到文件时
#      会做块缓冲，`[Console]::Out.Flush()` 对它**无效**。
#      （`README` 级的常识：`[Console]::Out` 的 Flush 不保证落盘。）
#
#   ③ 所以：**让脚本自己写文件**，每次都 `Flush()` 到底层 FileStream。
#      这样既不经过 stdout、也不经过管道，Node 侧只读文件。
#
# ## 兼容
#
# 不传 `-OutFile` 时行为与原来完全一致（写 stdout）——
# 手工排查、`test-probe-key.ps1` 那些用法不受影响。
# --------------------------------------------------------------------------- #

$writer = $null
if ($OutFile) {
    # 覆盖写（不是追加）：Node 侧按偏移量读，文件从头开始更简单
    $writer = New-Object System.IO.StreamWriter($OutFile, $false, [System.Text.Encoding]::UTF8)
    $writer.AutoFlush = $true          # <- 关键：每行都落盘
}

function Write-Line([string]$text) {
    if ($writer) {
        $writer.WriteLine($text)
        $writer.Flush()
    } else {
        [Console]::Out.WriteLine($text)
        [Console]::Out.Flush()
    }
}

# READY line: the Node side uses it to confirm the monitor really started,
# instead of silently doing nothing (that case is the hardest to debug).
Write-Line "READY $Vk"

# ---------------------------------------------------------------------------
# Heartbeat (added 2026-10-10) -- fixes "key only works when the pet is focused"
#
# WHY IT IS NEEDED
#
#   The user reported that push-to-talk only works while the pet window is
#   focused. Measured evidence:
#
#       PID 17704   CPU user 3.14s
#       6 seconds later: 3.14s -> 3.14s (+0.00s)   <- the loop had STOPPED
#       probe-key.out last written 233 seconds ago
#
#   The process was alive but the `while` loop was not running. The old script
#   only wrote to the file on DOWN/UP, so a stalled loop was INVISIBLE, and the
#   Node side had no detection at all -- the key just silently stopped working
#   forever, with no error and no log line.
#
# HOW IT IS USED
#
#   Every $heartbeatEvery polls we append one "TICK <ms>" line. The Node side
#   IGNORES the contents and only uses it as a liveness signal; if no line
#   arrives for ~8 seconds it kills and restarts this process.
#
# NOTE: keep this comment ASCII-only.
#   Windows PowerShell 5.1 reads .ps1 files as ANSI unless they have a UTF-8
#   BOM, so non-ASCII text here gets mangled. Mangling inside a block comment
#   can even terminate it early and break the whole script (this happened once
#   while writing this very change). Line comments that start at column 0 are
#   safe -- a mangled line is still a comment line -- but block comments are not.
# ---------------------------------------------------------------------------
$heartbeatEvery = 200      # 200 polls x 15ms = about one TICK every 3 seconds
$tick = 0
$sw = [System.Diagnostics.Stopwatch]::StartNew()

$wasDown = $false
while ($true) {
    # The high bit (0x8000) means "currently held". GetAsyncKeyState returns a
    # short, so convert to int before testing or negative comparisons break.
    $state = [int][KeyPoll]::GetAsyncKeyState($Vk)
    $down = ($state -band 0x8000) -ne 0

    if ($down -ne $wasDown) {
        if ($down) { Write-Line "DOWN $Vk" }
        else       { Write-Line "UP $Vk" }
        $wasDown = $down
    }

    $tick++
    if ($tick % $heartbeatEvery -eq 0) {
        Write-Line ("TICK {0}" -f $sw.ElapsedMilliseconds)
    }
    Start-Sleep -Milliseconds $PollMs
}
