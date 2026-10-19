# 发一次合成按键（按下 -> 停一会儿 -> 松开），用于自测"按住说话"。
#
# ⚠️ 为什么是独立文件而不是 `powershell -Command "<这里写脚本>"`：
#    那样要把 C# 的 here-string、`$` 变量、引号全部套进一层命令行字符串里，
#    转义层数一多必然出错（实测就是这么失败的：`Command failed` 而看不出原因）。
#    独立文件可以被单独跑、单独看报错。
#
# ⚠️ 必须带 UTF-8 BOM（PowerShell 5.1 对无 BOM 的 .ps1 按 ANSI 解析）。

param(
    [Parameter(Mandatory = $true)][int]$Vk,
    [int]$HoldMs = 500,
    # 送两次（用于验证"重复按下"的去重逻辑）
    [int]$Times = 1
)

$ErrorActionPreference = 'Stop'

$sig = @'
using System;
using System.Runtime.InteropServices;
public static class KeySend {
    [DllImport("user32.dll")]
    public static extern void keybd_event(byte bVk, byte bScan, uint dwFlags, UIntPtr dwExtraInfo);
    public const uint KEYUP = 0x0002;
}
'@
Add-Type -TypeDefinition $sig -ErrorAction Stop

for ($n = 0; $n -lt $Times; $n++) {
    [KeySend]::keybd_event([byte]$Vk, 0, 0, [UIntPtr]::Zero)
    Start-Sleep -Milliseconds $HoldMs
    [KeySend]::keybd_event([byte]$Vk, 0, [KeySend]::KEYUP, [UIntPtr]::Zero)
    Start-Sleep -Milliseconds 200
}

Write-Output "SENT $Vk x$Times (hold ${HoldMs}ms)"
