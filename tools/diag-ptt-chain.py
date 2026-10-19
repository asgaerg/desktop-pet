"""查「监视器收到了按键，但应用没反应」断在哪一环。

## 已知（关键线索）

监视器的输出文件 `pet-data/ptt-tmp/probe-key.out`：

```
READY 165
DOWN 165        <- 按键被监听到了！
UP 165
DOWN 165
UP 165          <- 最后写入 19:41:03，之后再没更新
```

## 链路（`PushToTalk` 是"每 80ms 轮询读这个文件"）

```
PowerShell 轮询 GetAsyncKeyState（15ms）
   -> 写 probe-key.out
      -> Node 每 80ms 读文件新增的行
         -> onPress / onRelease
            -> petWindow.sendToRenderer('pet:ptt', {phase})
               -> 渲染层 applyCaptureGate()
                  -> pet.sendVoicePtt() -> 主进程 -> sidecar
```

## 这个脚本查什么

  ① `probe-key.out` **现在还在不在长**（文件没长 = 监视器死了/被挂起）
  ② monitor 进程还在不在、CPU 有没有在动
  ③ 主进程的 `TICK` 心跳（`probe-key-heartbeat.ps1` 才打 TICK，
     产品用的 `probe-key.ps1` 不打 —— 所以只能靠 ①）
  ④ `main.log` 里 `[按住说话]` 相关行的时间分布
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
MLOG = ROOT / "apps" / "desktop" / "pet-data" / "main.log"


def probe_stat() -> tuple[int, float]:
    try:
        st = PROBE.stat()
        return st.st_size, st.st_mtime
    except OSError:
        return -1, 0.0


print("  ═══ ① probe-key.out 还在长吗 ═══")
size0, mtime0 = probe_stat()
print(f"     现在：{size0} B，最后写入 {time.strftime('%H:%M:%S', time.localtime(mtime0))}")
print("     等 8 秒（这期间**请按住右 Alt 说一句**）…")
time.sleep(8)
size1, mtime1 = probe_stat()
print(f"     8 秒后：{size1} B，最后写入 {time.strftime('%H:%M:%S', time.localtime(mtime1))}")
print()
if size1 > size0:
    print("     ✅ 文件在长 —— 监视器活着，按键被记下来了")
    print("        那么断点在**后面**（读文件 / IPC / 渲染层 / sidecar）")
elif size1 == size0:
    print("     ⚠️ 文件没长 —— 两种可能：")
    print("        · 你这 8 秒没按键（那就再试一次）")
    print("        · **监视器循环停了**（那就是真凶：进程活着但不轮询了）")

print()
print("  ═══ ② 监视器进程 + CPU ═══")
ps = subprocess.run(
    [
        "powershell", "-NoProfile", "-Command",
        "Get-CimInstance Win32_Process -Filter \"Name='powershell.exe'\" | "
        "Where-Object { $_.CommandLine -match 'probe-key' } | "
        "Select-Object ProcessId,ParentProcessId,CreationDate,"
        "@{n='CPU_s';e={[math]::Round($_.UserModeTime/10000000,1)}} | ConvertTo-Json -Compress",
    ],
    capture_output=True, text=True, encoding="utf-8", errors="replace",
)
print(f"     {ps.stdout.strip()[:300]}")
if ps.stderr.strip():
    print(f"     stderr: {ps.stderr.strip()[:200]}")

print()
print("  ═══ ③ main.log 里按键相关的时间分布 ═══")
try:
    lines = MLOG.read_text(encoding="utf-8", errors="replace").split("\n")
except OSError:
    lines = []
hits = [
    l.strip()
    for l in lines
    if "[brain]" not in l and any(k in l for k in ("按住说话", "渲染层发来", "采集闸门"))
]
print(f"     共 {len(hits)} 条：")
for l in hits[-16:]:
    print(f"       {l[:112]}")

print()
print("  ═══ ④ 判读 ═══")
print("     若 ① 文件在长、但 ③ 里没有对应的「[按住说话] 渲染层发来」：")
print("       -> 断点在 **Node 侧读文件到 IPC** 这一段")
print("     若 ③ 里有「渲染层发来 start/end」但没有「采集闸门」：")
print("       -> 断点在 **主进程 -> sidecar**")
