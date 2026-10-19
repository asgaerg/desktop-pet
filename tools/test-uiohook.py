"""最小验证：`uiohook-napi` 在本机能不能收到**按下和松开**。

## 为什么先单独测

前面栽了两次，都是"钩子装上了但收不到键"：
  · PowerShell 轮询 `GetAsyncKeyState` —— 180 秒心跳正常、用户按了很多次、0 个事件
  · PowerShell `WH_KEYBOARD_LL` 钩子 —— 同样：TICK 在流、0 个键事件

所以**先证明 uiohook 能用**，再往产品里接。而且这个测试要在
**两种环境下都跑**：
  · 从 pwsh/python 起（我的环境，之前全都收不到）
  · 从 Electron 起（用户的真实环境）

如果它在我这儿都收得到，那它和前面两个方案的差别就是本质的。

## 怎么跑

先按提示跑 pwsh 版；如果你按了有反应，就不用管 Electron 版。
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
TMP = ROOT / "apps" / "desktop" / "pet-data" / "ptt-tmp"
TMP.mkdir(parents=True, exist_ok=True)
OUT = TMP / "uiohook-test.log"

# 找到 uiohook-napi 的真实路径（pnpm 的 .pnpm 目录）
cands = sorted(
    (ROOT / "node_modules" / ".pnpm").glob("uiohook-napi@*/node_modules/uiohook-napi")
)
if not cands:
    print("  ❌ 找不到 uiohook-napi（pnpm add 没成功？）")
    raise SystemExit(1)
PKG = cands[0]
print(f"  uiohook-napi: {PKG}")

SCRIPT = r"""
const fs = require('fs');
const path = process.env.UIOHOOK_PKG;
const out = process.env.UIOHOOK_OUT;
const { uIOhook, UiohookKey } = require(path);

const w = fs.createWriteStream(out, { flags: 'w' });
const log = (s) => { w.write(s + '\n'); };

log('READY');
// 心跳：证明进程活着（和钩子脚本同样的思路）
setInterval(() => log('TICK'), 3000);

uIOhook.on('keydown', (e) => log('DOWN ' + e.keycode));
uIOhook.on('keyup', (e) => log('UP ' + e.keycode));
uIOhook.on('mousedown', (e) => log('MOUSE-DOWN ' + e.button));

uIOhook.start();
log('STARTED');
"""

sjs = TMP / "uiohook-probe.js"
sjs.write_text(SCRIPT, encoding="utf-8")

env = {**os.environ, "UIOHOOK_PKG": str(PKG), "UIOHOOK_OUT": str(OUT)}
try:
    OUT.unlink()
except OSError:
    pass

print()
print("  ═══ 起 uiohook 探针（Node 24，不是 Electron）═══")
proc = subprocess.Popen(
    [NODE24, str(sjs)],
    stdout=subprocess.DEVNULL,
    stderr=subprocess.PIPE,
    env=env,
)
print(f"  PID {proc.pid}")
time.sleep(3)

print()
print("  ══════════════════════════════════════════════════════════")
print("  👉 请**按住右 Alt 一秒再松开**，然后**按住右 Ctrl 一秒再松开**")
print("     （鼠标也点一下 —— 那个 API 也在测）")
print("  ══════════════════════════════════════════════════════════")
print()

seen = 0
for i in range(25):
    time.sleep(1)
    try:
        lines = [l.strip() for l in OUT.read_text(encoding="utf-8", errors="replace").split("\n") if l.strip()]
    except OSError:
        lines = []
    keys = [l for l in lines if l.startswith(("DOWN", "UP", "MOUSE"))]
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
lines = [l.strip() for l in OUT.read_text(encoding="utf-8", errors="replace").split("\n") if l.strip()]
downs = [l for l in lines if l.startswith("DOWN")]
ups = [l for l in lines if l.startswith("UP")]
mouse = [l for l in lines if l.startswith("MOUSE")]
ticks = [l for l in lines if l == "TICK"]
started = "STARTED" in lines
print(f"     STARTED={started}  TICK={len(ticks)}  DOWN={len(downs)}  UP={len(ups)}  MOUSE={len(mouse)}")
print(f"     DOWN 的键码: {[l.split()[1] for l in downs]}")
print(f"     UP   的键码: {[l.split()[1] for l in ups]}")
if err:
    print(f"     stderr: {err[:400]}")

print()
if started and downs:
    print("     🎉 **uiohook 收到了按键**（在我这个环境里！）")
    print("        -> 它和前面两个方案的差别是本质的")
    if ups:
        print("        ✅ 而且**松开也收到了** —— 真正的「按住说话」成立")
else:
    print("     ❌ 没收到按键")
    print("        （STARTED 有 = 库装起来了；所以是收不到事件）")

for p in (sjs, OUT):
    try:
        p.unlink()
    except OSError:
        pass
