"""重启 + 用 SendKeys 做完整端到端测试（等于真人按 F9）。

## ⚠️ 这个脚本踩过两次"自杀"，都记下来

### 第一次：`taskkill /F /IM python.exe`

那连**正在运行这个脚本的 Python** 一起杀 —— 没有任何输出、exit 1。

### 第二次（更隐蔽）：`taskkill /F /T` 匹配到**本脚本自己**

清理残留监视器时用了：

```powershell
Where-Object { $_.CommandLine -like '*probe-key.ps1*' }
```

而**这个 Python 文件的注释里就含 `probe-key.ps1` 字样**，
被 PowerShell 子进程读到了它自己的命令行 —— 于是 `/T` 顺着调用链
找到并杀掉了这个脚本。

**修**：全部改用 `Stop-Process -Id`（按 PID 杀，**不用 `/T`**）——
`/T` 是"连子孙一起杀"，而在这个调用链里"子孙"包括调用者自己。

> 教训：**任何"杀进程"的代码，过滤条件都可能匹配到调用者**。
> 用 `/T` 之前先想清楚它会顺着哪条链往上走。
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import time

sys.path.insert(0, ".")
from tool_bootstrap import ROOT, setup  # noqa: E402

setup(verbose=False)

DESKTOP = ROOT / "apps" / "desktop"
MLOG = DESKTOP / "pet-data" / "main.log"
SLOG = DESKTOP / "dist" / "startup.log"
ME = os.getpid()

KEYS = ("按住说话", "采集闸门", "采集电平", "主动收尾", "开始识别", "语音识别",
        "以语音输入", "收尾没拿到", "补报", "语音总开关")


def lines() -> list[str]:
    out: list[str] = []
    for p in (MLOG, SLOG):
        try:
            out += [
                l.strip()
                for l in p.read_text(encoding="utf-8", errors="replace").split("\n")
                if "[brain]" not in l and "帧头" not in l
            ]
        except OSError:
            pass
    return [l for l in out if any(k in l for k in KEYS)]


print("  ═══ ① 清理（只按进程名，**不做任何命令行匹配**）═══")
# ⚠️ 这里刻意**不匹配命令行**：本文件里含 'probe' 字样，
#    而命令行匹配会让 PowerShell 子进程读到自己 -> taskkill /T 自杀（踩过两次）。
PY_NAMES = "'electron.exe','pythonw.exe'"
ps = subprocess.run(
    ["powershell", "-NoProfile", "-Command",
     f"Get-Process electron -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue; "
     f"Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
     f"Where-Object {{ .ProcessId -ne {ME} }} | "
     f"ForEach-Object {{ Stop-Process -Id .ProcessId -Force -ErrorAction SilentlyContinue }}; "
     "'cleaned'"],
    capture_output=True, text=True, encoding="utf-8", errors="replace",
)
print(f"     {(ps.stdout or '').strip()[:60]}")
time.sleep(3)

print()
print("  ═══ ② 清日志并启动 ═══")
for p in (MLOG, SLOG):
    try:
        p.unlink()
    except OSError:
        pass
env = {k: v for k, v in os.environ.items() if k != "ELECTRON_RUN_AS_NODE"}
subprocess.Popen(
    [str(ROOT / "node_modules" / ".pnpm" / "electron@32.3.3" / "node_modules"
         / "electron" / "dist" / "electron.exe"),
     ".", "--dev", "--no-sandbox", "--debug-port=9222"],
    cwd=str(DESKTOP), env=env,
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    creationflags=0x00000008,
)
print("     等 45 秒…")
time.sleep(45)

print()
print("  ═══ ③ 启动后的关键日志 ═══")
startup = lines()
for l in startup[-8:]:
    print(f"     {l[:112]}")

n0 = len(startup)
print()
print("  ═══ ④ 用 SendKeys 发两次 F9（= 一次完整切换）═══")
PS = (
    "$np = Start-Process notepad -PassThru; "
    "Start-Sleep -Milliseconds 1000; "
    "Add-Type -AssemblyName System.Windows.Forms; "
    "[System.Windows.Forms.SendKeys]::SendWait('{F9}'); "
    "Start-Sleep -Seconds 2; "
    "[System.Windows.Forms.SendKeys]::SendWait('{F9}'); "
    "Start-Sleep -Milliseconds 600; "
    "Stop-Process -Id $np.Id -Force -ErrorAction SilentlyContinue; "
    "Write-Output 'sent'"
)
r = subprocess.run(
    ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", PS],
    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90,
)
print(f"     {(r.stdout or '').strip()[:50]}")

for _ in range(14):
    time.sleep(1.5)
    if any("语音识别（" in x or "收尾没拿到" in x or "开始识别" in x for x in lines()[n0:]):
        time.sleep(2)
        break

print()
print("  ═══ ⑤ 这一轮新增的日志 ═══")
new = lines()[n0:]
levels = 0
for l in new:
    if "采集电平" in l:
        levels += 1
        continue
    print(f"     {l[:114]}")
if levels:
    print(f"     （另有 {levels} 条采集电平 —— 有电平就说明帧喂到端点了）")
if not new:
    print("     （没有新增）")

print()
print("  ═══ ⑥ 判读 ═══")
j = "\n".join(new)
checks = [
    ("补报了麦克风开关", "补报麦克风开关"),
    ("F9 被收到（phase=start）", "phase='start'"),
    ("闸门打开", "采集闸门：开"),
    ("有采集电平（帧在喂端点）", "采集电平"),
    ("收尾并送识别", "主动收尾"),
    ("识别出结果", "语音识别（"),
]
for label, needle in checks:
    print(f"     {'OK  ' if needle in j else 'FAIL'} {label}")

print()
if "语音识别（" in j:
    print("     🎉 **端到端成功** —— 热键 + 补报 + 本地 GPU 全通")
elif "主动收尾" in j:
    print("     ✅ 走到「主动收尾」—— 链路通，只差真人说话")
elif "采集电平" in j:
    print("     ✅ 帧真的喂到端点了（补报修好了）")
elif "闸门打开" in j:
    print("     ⚠️ 闸门开了但没有采集电平 —— 帧没到端点")
else:
    print("     ❌ 连 phase=start 都没有 —— F9 这次没被收到")
