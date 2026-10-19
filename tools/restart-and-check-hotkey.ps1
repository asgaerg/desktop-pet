# 重启应用并验证 globalShortcut 版 PTT。
#
# ## ⚠️ 为什么拆成"PowerShell 清理 + Python 启动/验证"
#
# 原来一个 Python 脚本全做，结果**它把自己杀了**：
# `taskkill /F /IM python.exe` 连运行它的那个 Python 一起杀
# （即使加了排除自身的逻辑，PowerShell 子进程的进程树仍可能被连带）。
# 表现是**没有任何输出、exit 1**，看起来像脚本崩溃。
#
# 同样的坑在别处也踩过：`taskkill /T` 会顺着调用链找到调用者自己。
#
# 所以：**杀进程的活全交给这个 PowerShell 脚本做**（它自己也是 powershell，
# 过滤器要排除自身），Python 那边只做"启动 + 读日志验证"。

$ErrorActionPreference = 'Continue'

Write-Host "  === 1. 停 electron ==="
Get-Process electron -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Write-Host "      done"

Write-Host "  === 2. 停 python（排除自身不是 python，安全）==="
# 这个脚本是 powershell，不是 python —— 所以按名字杀 python 不会自杀
Get-Process python,pythonw -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Write-Host "      done"

Write-Host "  === 3. 停残留的 probe-key 监视器（排除自身）==="
$me = $PID
Get-CimInstance Win32_Process -Filter "Name = 'powershell.exe'" |
  Where-Object { $_.CommandLine -like '*probe-key.ps1*' -and $_.ProcessId -ne $me } |
  ForEach-Object {
    Write-Host "      kill $($_.ProcessId)"
    taskkill /F /T /PID $_.ProcessId 2>&1 | Out-Null
  }

Start-Sleep -Seconds 3

Write-Host "  === 4. 清日志 ==="
Remove-Item 'F:\ai项目\apps\desktop\pet-data\main.log' -Force -ErrorAction SilentlyContinue
Remove-Item Env:\ELECTRON_RUN_AS_NODE -ErrorAction SilentlyContinue
Write-Host "      done"

Write-Host "  === 5. 启动（等 45 秒）==="
$electron = 'F:\ai项目\node_modules\.pnpm\electron@32.3.3\node_modules\electron\dist\electron.exe'
Start-Process -FilePath $electron -ArgumentList '.','--dev','--no-sandbox','--debug-port=9222' `
  -WorkingDirectory 'F:\ai项目\apps\desktop' -WindowStyle Hidden
Start-Sleep -Seconds 45

Write-Host "  === 6. 热键 / 按住说话 ==="
$log = 'F:\ai项目\apps\desktop\pet-data\main.log'
if (Test-Path $log) {
  Get-Content $log -Encoding UTF8 |
    Where-Object { $_ -notmatch '\[brain\]' -and $_ -notmatch '帧头' } |
    Where-Object { $_ -match '按住说话|快捷键|热键|全局' } |
    Select-Object -Last 20 |
    ForEach-Object { Write-Host "      $($_.Trim().Substring(0, [Math]::Min(116, $_.Trim().Length)))" }
} else {
  Write-Host "      （没有 main.log）"
}

Write-Host "  === 7. 还有没有 probe-key 监视器（新方案应当 0 个）==="
$n = (Get-CimInstance Win32_Process -Filter "Name = 'powershell.exe'" |
  Where-Object { $_.CommandLine -like '*probe-key.ps1*' -and $_.ProcessId -ne $me } |
  Measure-Object).Count
Write-Host "      监视器数量 = $n"
