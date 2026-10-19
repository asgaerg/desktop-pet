"""测 `keyhook.ps1`：低级键盘钩子能不能同时给 DOWN 和 UP。

## 为什么要这个

用户说得对：**"按住说话"本质上需要 keydown + keyup**。而现有两条路都给不了：

  · `globalShortcut` —— **只有按下、没有松开**（所以才被迫改成切换式）
  · `GetAsyncKeyState` 轮询 —— 本机实测**读不到任何键**

低级键盘钩子（`WH_KEYBOARD_LL`）是**事件驱动**的，按下和松开都给。

## ⚠️ 这个脚本踩过的坑（不要重犯）

第一版清理时用了：

```powershell
Get-Process powershell | Where-Object { $_.StartTime -gt (Get-Date).AddSeconds(-20) } | Stop-Process
```

**那个过滤条件匹配到了正在运行的 pwsh 本身**（它也是 powershell），
于是脚本自杀、exit code 4294967295。

**修**：保存 `Start-Process -PassThru` 返回的对象，**按 PID 停**。
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

SCRIPT = ROOT / "apps" / "desktop" / "tools" / "keyhook.ps1"
TMP = ROOT / "apps" / "desktop" / "pet-data" / "ptt-tmp"
TMP.mkdir(parents=True, exist_ok=True)
OUT = TMP / "keyhook-test.out"
ERR = TMP / "keyhook-test.err"
for p in (OUT, ERR):
    try:
        p.unlink()
    except OSError:
        pass

VK = 0xA3  # 右 Ctrl（用户现在用这个）
print(f"  === 起钩子：VK {VK}（F9），输出到 {OUT.name} ===")
# ⚠️ TEMP 必须指向可写目录（Add-Type 编译要写临时文件）
env = {**os.environ, "TEMP": str(TMP), "TMP": str(TMP)}

with open(ERR, "w", encoding="utf-8") as errf:
    proc = subprocess.Popen(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
         "-File", str(SCRIPT), "-Vk", str(VK), "-OutFile", str(OUT)],
        stdout=subprocess.DEVNULL,
        stderr=errf,
        env=env,
    )

print(f"  PID = {proc.pid}")
time.sleep(3)

print()
print("  ═══ 请按 **右 Ctrl** 两次（各按住约 1 秒再松开）—— 等 25 秒 ═══")
print()
for i in range(25):
    time.sleep(1)
    try:
        txt = OUT.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        txt = ""
    lines = [l.strip() for l in txt.split("\n") if l.strip()]
    if lines and i % 3 == 2:
        print(f"     [{i + 1:2d}s] 现在有 {len(lines)} 行：{lines[-3:]}")

# ⚠️ 按 PID 停 —— 不要用 StartTime 过滤（那会匹配到调用者自己）
proc.terminate()
try:
    proc.wait(timeout=5)
except subprocess.TimeoutExpired:
    proc.kill()

print()
print("  ═══ 结果 ═══")
try:
    final = OUT.read_text(encoding="utf-8-sig", errors="replace")
except OSError:
    final = ""
lines = [l.strip() for l in final.split("\n") if l.strip()]
for l in lines:
    print(f"     {l}")
if not lines:
    print("     （文件是空的）")

print()
print("  ═══ stderr ═══")
try:
    e = ERR.read_text(encoding="utf-8", errors="replace").strip()
except OSError:
    e = ""
print(f"     {e[:500] if e else '（空）'}")

print()
print("  ═══ 判读 ═══")
downs = [l for l in lines if l.startswith("DOWN")]
ups = [l for l in lines if l.startswith("UP")]
ready = any(l.startswith("READY") for l in lines)

if not ready:
    print("     ❌ 连 READY 都没有 —— 钩子没装起来")
    print(f"        看 stderr：{e[:200]}")
elif not downs:
    print("     ❌ 装了钩子（READY 在）但**一个 DOWN 都没有**")
    print("        -> 和 `GetAsyncKeyState` 一样的现象（这个进程收不到键盘事件）")
elif downs and ups:
    print(f"     🎉 **成功！DOWN {len(downs)} 个 / UP {len(ups)} 个**")
    print("        -> 低级键盘钩子**能同时给按下和松开**")
    print("        -> 可以恢复真正的「按住说话」语义（不用切换式了）")
elif downs and not ups:
    print(f"     ⚠️ 有 DOWN（{len(downs)}）但没有 UP —— 松开没被报到")
    print("        -> 可能你一直按着没松，或者 KEYUP 被过滤了")
