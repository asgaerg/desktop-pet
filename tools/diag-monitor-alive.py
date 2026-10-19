"""监视器到底怎么了 —— 进程活着但循环不转了？

## 现象（刚刚实测，150 秒）

用户按住右 Alt 说话，而**监视器一个 DOWN 都没认到**：

```
① 监视器认到 DOWN : ❌
② Node 读到       : ❌
③ 主进程转发      : ❌
④ sidecar 收到    : ❌
```

## 要查

  ① `probe-key.ps1` 进程还在不在、**CPU 时间有没有在涨**
     （`GetAsyncKeyState` 轮询是忙等，CPU 应当持续增长；
       不涨 = 循环停了）
  ② `probe-key.out` 的最后修改时间
  ③ 用 `probe-key-heartbeat.ps1`（打 TICK 的版本）
     **独立跑一次**，一边跑一边按键 —— 这能确定
     "到底是循环停了，还是这个进程读不到键"

## ③ 是关键

`probe-key.ps1` 只在 DOWN/UP 时写文件 —— 循环停了**看不出来**。
`probe-key-heartbeat.ps1` 每 N 次轮询打一个 TICK，所以：
  · TICK 在来 + 按键有 DOWN  -> 监视器正常，问题在产品代码
  · TICK 在来 + 按键**没** DOWN -> 循环活着但**读不到键**（权限/会话隔离）
  · TICK 不来                -> 循环停了
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

PROBE = ROOT / "apps" / "desktop" / "pet-data" / "ptt-tmp" / "probe-key.out"
SCRIPT = ROOT / "apps" / "desktop" / "tools" / "probe-key-heartbeat.ps1"


def probe_state() -> tuple[int, float]:
    try:
        st = PROBE.stat()
        return st.st_size, st.st_mtime
    except OSError:
        return -1, 0.0


print("  ═══ ① 产品用的监视器进程 ═══")
ps = subprocess.run(
    ["powershell", "-NoProfile", "-Command",
     "Get-CimInstance Win32_Process -Filter \"Name='powershell.exe'\" | "
     "Where-Object { $_.CommandLine -match 'probe-key.ps1' } | "
     "ForEach-Object { \"$($_.ProcessId)|$($_.ParentProcessId)|"
     "$([math]::Round($_.UserModeTime/10000000,2))|$([math]::Round($_.KernelModeTime/10000000,2))\" }"],
    capture_output=True, text=True, encoding="utf-8", errors="replace",
)
rows = [r for r in ps.stdout.strip().split("\n") if "|" in r]
if not rows:
    print("     ❌ 监视器进程**不存在**了 —— 循环当然不转")
else:
    for r in rows:
        pid, ppid, user, kern = (r.split("|") + ["", "", "", ""])[:4]
        print(f"     PID {pid}（父 {ppid}）  CPU 用户 {user}s / 内核 {kern}s")
    # 隔 6 秒再看，CPU 涨不涨
    print("     等 6 秒看 CPU 有没有涨（不涨 = 循环停了）…")
    time.sleep(6)
    ps2 = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "Get-CimInstance Win32_Process -Filter \"Name='powershell.exe'\" | "
         "Where-Object { $_.CommandLine -match 'probe-key.ps1' } | "
         "ForEach-Object { \"$($_.ProcessId)|$([math]::Round($_.UserModeTime/10000000,2))\" }"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    rows2 = [r for r in ps2.stdout.strip().split("\n") if "|" in r]
    for r in rows2:
        pid, user = (r.split("|") + ["", ""])[:2]
        old = next((x.split("|")[2] for x in rows if x.startswith(pid + "|")), None)
        if old is not None:
            delta = float(user) - float(old)
            flag = "✅ 在转" if delta > 0.001 else "🔴 **停了**（CPU 不涨）"
            print(f"     PID {pid}: CPU {old}s -> {user}s（+{delta:.2f}s）  {flag}")

print()
print("  ═══ ② probe-key.out ═══")
size, mtime = probe_state()
print(f"     {size} B，最后写入 {time.strftime('%H:%M:%S', time.localtime(mtime))}")
print(f"     距今 {time.time() - mtime:.0f} 秒")

print()
print("  ═══ ③ 独立跑带心跳的监视器（15 秒，请按键）═══")
if not SCRIPT.is_file():
    print(f"     ❌ 找不到 {SCRIPT}")
    raise SystemExit(0)
print(f"     {SCRIPT.name}")
print("     **现在按住右 Alt 说一句**（脚本跑 15 秒，只统计不干预产品）")
print()
try:
    r = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
         "-File", str(SCRIPT), "-Vk", "0xA5", "-Seconds", "15"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=40,
    )
    out = (r.stdout or "") + (r.stderr or "")
except subprocess.TimeoutExpired:
    out = "(超时)"
except Exception as e:  # noqa: BLE001
    out = f"(跑不起来：{type(e).__name__}: {e})"

ticks = len(re.findall(r"TICK", out))
downs = len(re.findall(r"DOWN", out))
ups = len(re.findall(r"UP", out))
print(f"     TICK {ticks} 个，DOWN {downs} 个，UP {ups} 个")
for l in out.strip().split("\n")[:6]:
    print(f"       {l.strip()[:100]}")
if len(out.strip().split("\n")) > 6:
    print(f"       …（共 {len(out.strip().split(chr(10)))} 行，只显示前 6 行）")

print()
print("  ═══ ④ 判读 ═══")
if ticks == 0:
    print("     🔴 连 TICK 都没有 —— 独立跑的监视器也没起来（脚本/权限问题）")
elif downs == 0:
    print("     🔴 **TICK 在来但没有 DOWN** —— 循环活着，但**读不到这个键**")
    print("        这说明不是「循环停了」，而是 `GetAsyncKeyState(0xA5)` 读不到。")
    print("        可能：右 Alt 被别的程序吃掉 / 键盘布局 / 该键不在当前会话")
    print("        -> 换个键试（比如 F9 = 0x78）能立刻区分")
else:
    print(f"     ✅ 独立监视器收到了 {downs} 个 DOWN —— 说明**键本身没问题**")
    print("        那么产品里那个监视器是**停了**（对比 ① 的 CPU 结论）")
