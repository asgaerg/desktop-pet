"""记录**所有**按键的原始 keycode —— 你随便按几个键就行。

## 要回答的两个问题

  ① `uiohook-napi` 到底收不收得到键（之前 `keyTap()` 自测失败，但那可能是
     合成没生效，不是钩子不工作 —— 两者必须分开）
  ② `e.keycode` 报的是哪套编码。我按 `UiohookKey` 表把 VK 165（右 Alt）
     映射成 3640，但产品里按右 Alt **0 次触发**。如果它其实报 165，那我就
     一直拿 3640 去比 —— 永远不可能相等。

## 为什么要你按

**已确认的事实**：我从 pwsh/python 起的进程收不到真实按键
（轮询和 `WH_KEYBOARD_LL` 钩子都是 0 个事件）。所以这个脚本要能看到结果，
就得靠你按 —— 而它记录的是**原始 keycode**，不管按什么键都有用。

**随便按 Q、A、空格、右 Alt 都行** —— 我要的是"值长什么样"，不是"哪个键"。
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

NODE24 = r"C:\Users\tianyi\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\node\bin\node.exe"
PKG = next(
    (ROOT / "node_modules" / ".pnpm").glob("uiohook-napi@*/node_modules/uiohook-napi")
)
TMP = ROOT / "apps" / "desktop" / "pet-data" / "ptt-tmp"
TMP.mkdir(parents=True, exist_ok=True)
OUT = TMP / "_allkeys.log"

SCRIPT = r'''
const fs = require('fs');
const { uIOhook, UiohookKey } = require(process.argv[2]);
const out = process.argv[3];
const w = fs.createWriteStream(out, { flags: 'w' });
const log = (s) => { try { w.write(s + '\n'); } catch (e) {} };

log('READY');
setInterval(() => log('TICK'), 2000);

uIOhook.on('keydown', (e) => log('DOWN ' + e.keycode));
uIOhook.on('keyup',   (e) => log('UP ' + e.keycode));
try { uIOhook.start(); log('STARTED'); }
catch (e) { log('START-FAIL ' + String(e).slice(0, 200)); }
'''

sjs = TMP / "_allkeys.cjs"
sjs.write_text(SCRIPT, encoding="utf-8")
try:
    OUT.unlink()
except OSError:
    pass

print("  ═══ 起监听（记录所有按键）═══")
proc = subprocess.Popen(
    [NODE24, str(sjs), str(PKG), str(OUT)],
    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
)
print(f"  PID {proc.pid}")
time.sleep(3)

print()
print("  ══════════════════════════════════════════════════════════")
print("  👉 请**随便按几个键**（Q / A / 空格 / 右 Alt 各按一下）")
print("     等 30 秒")
print("  ══════════════════════════════════════════════════════════")
print()

seen = 0
for i in range(30):
    time.sleep(1)
    try:
        ls = [l.strip() for l in OUT.read_text(encoding="utf-8", errors="replace").split("\n") if l.strip()]
    except OSError:
        ls = []
    keys = [l for l in ls if l.startswith(("DOWN", "UP", "START"))]
    if len(keys) > seen:
        for l in keys[seen:]:
            print(f"     [{i + 1:2d}s] 🔑 {l}")
        seen = len(keys)

proc.terminate()
try:
    proc.wait(timeout=5)
except subprocess.TimeoutExpired:
    proc.kill()

try:
    err = proc.stderr.read().decode("utf-8", "replace").strip() if proc.stderr else ""
except Exception:  # noqa: BLE001
    err = ""

print()
print("  ═══ 结果 ═══")
ls = [l.strip() for l in OUT.read_text(encoding="utf-8", errors="replace").split("\n") if l.strip()]
downs = [int(l.split()[1]) for l in ls if l.startswith("DOWN")]
ups = [int(l.split()[1]) for l in ls if l.startswith("UP")]
print(f"     STARTED={'STARTED' in ls}  TICK={sum(1 for l in ls if l == 'TICK')}")
print(f"     DOWN {len(downs)} 个: {downs}")
print(f"     UP   {len(ups)} 个: {ups}")
if err:
    print(f"     stderr: {err[:300]}")

print()
print("  ═══ 判读 ═══")
if "START-FAIL" in "\n".join(ls):
    print("     ❌ uiohook.start() 失败 —— 看上面的 START-FAIL 内容")
elif downs:
    print("     ✅✅ **收到按键了** —— uiohook 在这个环境里工作")
    print()
    print("     现在回答第 ② 问：这些值属于哪套编码？")
    print(f"       实际收到: {sorted(set(downs))}")
    print("       UiohookKey 参考: Q=16 A=30 Space=57 F9=67 AltRight=3640 CtrlRight=3613")
    print("       Windows VK 参考: Q=81 A=65 Space=32 F9=120 右Alt=165 右Ctrl=163")
    inter_vk = [v for v in set(downs) if v in (81, 65, 32, 120, 165, 163)]
    inter_ui = [v for v in set(downs) if v in (16, 30, 57, 67, 3640, 3613)]
    if inter_ui and not inter_vk:
        print("       -> 是 **UiohookKey 那套**（我的映射思路对）")
    elif inter_vk and not inter_ui:
        print("       -> 是 **Windows VK**！**我的映射全错了** —— 直接用 VK 匹配即可")
    elif inter_vk and inter_ui:
        print("       -> 两套都有值出现，需要逐个键对照（见上面清单）")
    else:
        print("       -> 都不匹配已知参考值，看上面的实际数字")
elif "READY" in ls:
    print("     ❌ 进程起来了但**一个按键都没收到**")
    print("        （TICK 在流 = 进程活着；所以是钩子收不到事件）")
else:
    print("     ❌ 进程连 READY 都没写 —— 启动就失败了")

for p in (sjs,):
    try:
        p.unlink()
    except OSError:
        pass
