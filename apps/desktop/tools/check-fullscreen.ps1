# 全屏判据验证脚本（独立跑，不依赖 Electron）
# 用法：powershell -NoProfile -ExecutionPolicy Bypass -File check-fullscreen.ps1
# 说明：把当前前台窗口的矩形 / 屏幕 / 标题栏样式打出来，
#       并逐条给出判据结果，用来确认"全屏"到底卡在哪一条。

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

Add-Type @"
using System;
using System.Runtime.InteropServices;
public class FsCheck {
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] public static extern int GetWindowLong(IntPtr h, int i);
  [DllImport("user32.dll")] public static extern int GetWindowText(IntPtr h, System.Text.StringBuilder s, int n);
  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left, Top, Right, Bottom; }
}
"@

Add-Type -AssemblyName System.Windows.Forms

$h = [FsCheck]::GetForegroundWindow()
if ($h -eq [IntPtr]::Zero) { Write-Output "没有前台窗口"; exit }

$r = New-Object FsCheck+RECT
[void][FsCheck]::GetWindowRect($h, [ref]$r)
$s = [System.Windows.Forms.Screen]::FromHandle($h).Bounds
$sb = New-Object System.Text.StringBuilder 300
[void][FsCheck]::GetWindowText($h, $sb, 300)
$style = [FsCheck]::GetWindowLong($h, -16)
$hasCaption = (($style -band 0x00C00000) -ne 0)

$rw = $r.Right - $r.Left
$rh = $r.Bottom - $r.Top

Write-Output "标题     : $($sb.ToString())"
Write-Output "窗口矩形 : $($r.Left),$($r.Top),$($r.Right),$($r.Bottom)   (${rw}x${rh})"
Write-Output "屏幕     : $($s.X),$($s.Y),$($s.Width),$($s.Height)"
Write-Output "有标题栏 : $hasCaption"
Write-Output ""
Write-Output "--- 判据逐条 ---"

$c1 = ($r.Left - $s.X) -le 2
$c2 = ($r.Top - $s.Y) -le 2
$c3 = (($s.X + $s.Width) - $r.Right) -le 2
$c4 = (($s.Y + $s.Height) - $r.Bottom) -le 2
Write-Output "覆盖左 : $c1   (差 $($r.Left - $s.X))"
Write-Output "覆盖上 : $c2   (差 $($r.Top - $s.Y))"
Write-Output "覆盖右 : $c3   (差 $(($s.X + $s.Width) - $r.Right))"
Write-Output "覆盖下 : $c4   (差 $(($s.Y + $s.Height) - $r.Bottom))"

$b1 = $rw -ge ($s.Width * 0.8)
$b2 = $rh -ge ($s.Height * 0.8)
Write-Output "够宽   : $b1   ($rw >= $([math]::Round($s.Width * 0.8)))"
Write-Output "够高   : $b2   ($rh >= $([math]::Round($s.Height * 0.8)))"

$coversScreen = $c1 -and $c2 -and $c3 -and $c4
$bigEnough = $b1 -and $b2
$isFull = $coversScreen -and $bigEnough -and (-not $hasCaption)

Write-Output ""
Write-Output "coversScreen = $coversScreen"
Write-Output "bigEnough    = $bigEnough"
Write-Output "无标题栏     = $(-not $hasCaption)"
Write-Output ""
Write-Output "==> 判定为全屏: $isFull"
