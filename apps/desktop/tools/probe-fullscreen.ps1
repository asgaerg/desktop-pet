# 全屏探测（**常驻模式**）：判断前台窗口是不是全屏应用。
#
# ---- 用法 ----
#
# 由主进程（fullscreenWatcher.ts）启动**一次**，之后通过 stdin 提问：
#   往 stdin 写一行 -> 从 stdout 读一行结果
# 输出格式：<是否全屏>|<窗口矩形>|<屏幕矩形>|<窗口标题>|<有无标题栏>
# 例：1|0,0,1536,864|0,0,1536,864|YouTube|False
# 启动后会先输出一行 READY，主进程看到它才开始提问。
#
# ---- 为什么改成常驻 ----
#
# 原来是"每次检测起一个新 PowerShell 进程"，代价是：
#   · 每次约 400ms（进程启动 + `Add-Type` 重新编译一遍 C#）；
#   · 空闲轮询就吃掉约 8~20% 单核；
#   · **每次 spawn 都是一次新的失败机会**（实测在应用里就一直失败）。
# 常驻后：编译只做一次，单次查询约几毫秒，CPU 占用基本归零。
#
# ---- 判据 ----
#
# ① 窗口矩形覆盖整块屏幕（2px 容差，DPI 缩放下有 ±1 取整）
# ② 窗口足够大（滤掉 NVIDIA 覆盖层 / 输入法候选框那种贴边小窗）
# ③ 窗口**没有标题栏** —— 用来区分"全屏"和"最大化"
#
# ③ 是关键。实测数据：
#     普通窗口  rect=100,80,1002,680   有标题栏
#     最大化    rect=-7,-7,1543,823    有标题栏   <- 容易误判成全屏
#     真全屏    rect=0,0,1536,864      无标题栏
# 只靠 ①② 的话，任何最大化窗口都会被当成全屏。
#
# 也试过 SHAppBarMessage(ABM_GETTASKBARPOS) 拿任务栏再判断是否被盖住，
# 但本环境实测返回 0,0,0,0（拿不到），那条判据恒为假。
#
# ---- 编码：本文件必须带 UTF-8 BOM ----
#
# ⚠️ PowerShell 5.1 对**没有 BOM** 的 .ps1 会按 ANSI（本机 GBK）读取，
# 中文注释会被误读、可能产生破坏语法的字符，脚本直接报语法错误。
# 这个坑踩过：同一个文件用 PowerShell 直接跑正常，被 Node 拉起来就报错。
# 重新保存本文件时**务必保留 BOM**。

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

# ⚠️ 必须把临时目录挪到可写位置，否则 Add-Type 直接失败：
#   Add-Type : 对路径"C:\Users\<用户>\AppData\Local\Temp\xxxx.tmp"的访问被拒绝
# 因为 Add-Type 要编译 C#，中间产物写在 %TEMP%，而本环境 %TEMP% 不可写（EPERM）。
# 注意：只把本 .ps1 挪出 %TEMP% 是**不够**的 —— 我一开始以为够了，仍然失败，
# 直到把 stderr 原文打出来才看清真正原因。
if ($env:PET_PROBE_TMP) {
  $env:TEMP = $env:PET_PROBE_TMP
  $env:TMP = $env:PET_PROBE_TMP
}

Add-Type @"
using System;
using System.Text;
using System.Runtime.InteropServices;
public class FsProbe {
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] public static extern int GetWindowLong(IntPtr h, int i);
  [DllImport("user32.dll")] public static extern int GetWindowText(IntPtr h, System.Text.StringBuilder s, int n);
  [DllImport("user32.dll")] public static extern int GetClassName(IntPtr h, System.Text.StringBuilder s, int n);
  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left, Top, Right, Bottom; }
}
"@
Add-Type -AssemblyName System.Windows.Forms

function Get-FullscreenProbe {
  $h = [FsProbe]::GetForegroundWindow()
  if ($h -eq [IntPtr]::Zero) { return "0|||" }

  $r = New-Object FsProbe+RECT
  [void][FsProbe]::GetWindowRect($h, [ref]$r)
  $s = [System.Windows.Forms.Screen]::FromHandle($h).Bounds
  $title = New-Object System.Text.StringBuilder 300
  [void][FsProbe]::GetWindowText($h, $title, 300)

  $style = [FsProbe]::GetWindowLong($h, -16)
  $hasCaption = (($style -band 0x00C00000) -ne 0)

  # ⚠️ **系统外壳窗口必须排除**。这是踩出来的：
  # 「Program Manager」（桌面本身）无边框、铺满整屏、没有标题栏，
  # 于是完美命中下面那条判据 —— 检测结果就是**宠物在紧凑/常规之间反复抖动**，
  # 视觉上表现为"隔几帧乱飘一下"（实测日志里刷了几十条
  # 「检测到全屏应用，宠物进入紧凑模式：Program Manager」）。
  # 解决办法：按**窗口类名**排除已知的外壳/桌面窗口。
  $cls = New-Object System.Text.StringBuilder 256
  [void][FsProbe]::GetClassName($h, $cls, 256)
  $className = $cls.ToString()
  $shellClasses = @('Progman', 'WorkerW', 'Shell_TrayWnd', 'Shell_SecondaryTrayWnd',
                    'Windows.UI.Core.CoreWindow', 'MultitaskingViewFrame', 'TaskListThumbnailWnd',
                    'XamlExplorerHostIslandWindow', 'SysShadow', 'ForegroundStaging')
  if ($shellClasses -contains $className) {
    return ("0|{0},{1},{2},{3}|{4},{5},{6},{7}|{8}|{9}|SHELL:{10}" -f `
      $r.Left, $r.Top, $r.Right, $r.Bottom, $s.X, $s.Y, $s.Width, $s.Height, $title.ToString(), $hasCaption, $className)
  }

  $rw = $r.Right - $r.Left
  $rh = $r.Bottom - $r.Top

  $covers = (($r.Left - $s.X) -le 2) -and (($r.Top - $s.Y) -le 2) -and
            ((($s.X + $s.Width) - $r.Right) -le 2) -and ((($s.Y + $s.Height) - $r.Bottom) -le 2)
  $big = ($rw -ge ($s.Width * 0.8)) -and ($rh -ge ($s.Height * 0.8))

  $full = 0
  if ($covers -and $big -and (-not $hasCaption)) { $full = 1 }

  return ("{0}|{1},{2},{3},{4}|{5},{6},{7},{8}|{9}|{10}|{11}" -f `
    $full, $r.Left, $r.Top, $r.Right, $r.Bottom, `
    $s.X, $s.Y, $s.Width, $s.Height, $title.ToString(), $hasCaption, $className)
}

# 就绪标记：主进程看到这一行才认为可以提问，
# 否则可能在 Add-Type 还没编译完时发问，拿到空回复而误判为失败。
[Console]::Out.WriteLine("READY")
[Console]::Out.Flush()

while ($true) {
  $line = [Console]::In.ReadLine()
  if ($null -eq $line) { break }   # stdin 关闭 -> 退出
  try {
    $result = Get-FullscreenProbe
  } catch {
    $result = "0|||ERR:" + $_.Exception.Message
  }
  [Console]::Out.WriteLine($result)
  [Console]::Out.Flush()
}
