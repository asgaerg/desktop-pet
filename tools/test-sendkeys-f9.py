"""注入 F9（走真实输入队列），看 `globalShortcut` 收不收得到。

## 为什么用 SendKeys 而不是 keybd_event

前面已经证明 **`keybd_event` 在这个环境里对产品进程无效**
（往产品监视器注入四个键，一个都没认到）。

而 `SendKeys` 走的是 **Win32 输入队列**（和真人按键同一条路），
`test-ptt-unfocused.ps1` 的历史注释里也确认过这一点：

```
⚠️ SendKeys 发的键**同一个 session 内是真实的**（走 Win32 输入队列），
   所以 `GetAsyncKeyState` 应当能看到它 —— 这也是本次实验成立的前提。
```

⚠️ 但 SendKeys 需要一个**有消息循环的前台窗口**接收它。
所以脚本会先开一个记事本并激活它 —— 顺便也构造了"前台是别的应用"
这个条件，正好测"失焦时热键还灵不灵"。

## 判据

  · main.log 出现「按下（F9）」-> **globalShortcut 收到了** -> 热键可用
  · 没有                        -> 注册了但收不到 -> 换机制
"""

from __future__ import annotations

import pathlib
import re
import subprocess
import sys
import time

sys.path.insert(0, ".")
from tool_bootstrap import ROOT, setup  # noqa: E402

setup(verbose=False)

MLOG = ROOT / "apps" / "desktop" / "pet-data" / "main.log"


def log_hits() -> list[str]:
    try:
        return [
            l.strip()
            for l in MLOG.read_text(encoding="utf-8", errors="replace").split("\n")
            if "[brain]" not in l and "帧头" not in l
            and any(k in l for k in ("按住说话", "采集闸门", "主动收尾", "语音识别"))
        ]
    except OSError:
        return []


before = log_hits()
n0 = len(before)
print(f"  起始 {n0} 条相关日志")
for l in before[-3:]:
    print(f"     {l[:104]}")
print()

PS = r'''
# 开一个记事本并激活它 —— 既是 SendKeys 的接收窗口，也构造"前台是别的应用"
$np = Start-Process notepad -PassThru
Start-Sleep -Milliseconds 900
Add-Type -AssemblyName System.Windows.Forms
# 用它自己的窗口当接收者（前台）
[System.Windows.Forms.SendKeys]::SendWait('{F9}')
Start-Sleep -Milliseconds 700
[System.Windows.Forms.SendKeys]::SendWait('{F9}')
Start-Sleep -Milliseconds 500
Stop-Process -Id $np.Id -Force -ErrorAction SilentlyContinue
Write-Output "sent"
'''

print("  ═══ 注入 F9 两次（间隔 0.7 秒）—— 期间前台是记事本 ═══")
r = subprocess.run(
    ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", PS],
    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
)
print(f"     SendKeys: {(r.stdout or '').strip()[:60]}")
if (r.stderr or "").strip():
    print(f"     stderr: {r.stderr.strip()[:200]}")

time.sleep(3)

print()
print("  ═══ 注入后新增的日志 ═══")
after = log_hits()
new = after[n0:]
if new:
    for l in new:
        print(f"     {l[:112]}")
else:
    print("     （没有新增）")

print()
print("  ═══ 判读 ═══")
joined = "\n".join(new)
if "按下（F9）" in joined:
    print("     ✅ **globalShortcut 收到了 F9**")
    print("        而且前台是记事本（失焦）—— 说明热键**与焦点无关**")
    print("        => 你之前按 F9 没反应，可能只是**时机**（监视窗口错过了）")
elif "按住说话已启动" in joined:
    print("     ⚠️ 只看到启动日志，没看到「按下（F9）」")
    print("        -> 注册了但收不到（SendKeys 也被系统丢了？）")
else:
    print("     ❌ 没有任何按键日志")
    print("        -> `globalShortcut` 在这个环境里**收不到 F9**")
    print("        结论：热键这条路不可靠，应当加**菜单入口**（那 100% 可靠）")

print()
print("  ═══ 附：应用还在跑吗 ═══")
out = subprocess.run(
    ["powershell", "-NoProfile", "-Command",
     "Get-CimInstance Win32_Process -Filter \"Name='electron.exe'\" | "
     "Where-Object { $_.CommandLine -notmatch '--type=' } | "
     "Select-Object -First 1 -ExpandProperty ProcessId"],
    capture_output=True, text=True,
).stdout.strip()
print(f"     Electron PID = {out or '（没在跑！）'}")
