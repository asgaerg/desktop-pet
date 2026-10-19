"""在 Electron 里测 `iohook` —— 用**独立的小脚本**，不动产品代码。

## 为什么要单独测

产品里已经换过 4 个方案了，每一次都是"装上了、启动了、但收不到键"。
在改产品之前，先用一个最小 Electron 程序确认 `iohook` 在**这个 Electron**
里到底收不收得到键 —— 收到了再改产品，收不到就不用白改。

## `iohook` 的官方 API（用户给的文档）

```js
const ioHook = require('iohook');
ioHook.on('keydown', (e) => console.log(e.keycode));
ioHook.on('keyup',   (e) => console.log(e.keycode));
ioHook.start();
```

⚠️ 它默认按 `keycode` 匹配，但有些环境要用 `rawcode`
（文档里有 `ioHook.useRawcode(true)`）—— 所以这个测试**两个都打出来**。

## 怎么跑

用 Electron 起（不是 Node）—— 因为 `iohook` 必须为 Electron 的 ABI 编译。
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

TMP = ROOT / "apps" / "desktop" / "pet-data" / "ptt-tmp"
TMP.mkdir(parents=True, exist_ok=True)
OUT = TMP / "_iohook-electron.log"
MAIN = TMP / "_iohook-electron.cjs"

ELECTRON = (
    ROOT / "node_modules" / ".pnpm" / "electron@32.3.3" / "node_modules"
    / "electron" / "dist" / "electron.exe"
)
if not ELECTRON.is_file():
    print(f"  ❌ 找不到 electron: {ELECTRON}")
    raise SystemExit(1)

MAIN_SRC = r'''
const fs = require('fs');
const path = require('path');
const { app } = require('electron');

const out = process.env.IOHOOK_OUT;
const w = fs.createWriteStream(out, { flags: 'w' });
const log = (s) => { try { w.write(s + '\n'); } catch (e) {} };

log('ELECTRON-READY');

let ioHook = null;
try {
  // 用产品的 node_modules 解析（pnpm 的符号链接）
  const req = require('module').createRequire(
    path.join(process.env.APP_DIR, 'package.json'),
  );
  ioHook = req('iohook');
  log('REQUIRE-OK');
} catch (e) {
  log('REQUIRE-FAIL ' + String(e && e.message).slice(0, 300));
  app.quit();
}

if (ioHook) {
  try {
    ioHook.on('keydown', (e) => log('DOWN keycode=' + e.keycode + ' rawcode=' + e.rawcode));
    ioHook.on('keyup',   (e) => log('UP keycode=' + e.keycode + ' rawcode=' + e.rawcode));
    ioHook.start();
    log('START-OK');
  } catch (e) {
    log('START-FAIL ' + String(e && e.message).slice(0, 300));
  }
  setInterval(() => log('TICK'), 2000);
}

// 别显示任何窗口 —— 这个测试只要键盘钩子
app.on('window-all-closed', () => {});
'''

MAIN.write_text(MAIN_SRC, encoding="utf-8")
try:
    OUT.unlink()
except OSError:
    pass

env = {
    **os.environ,
    "IOHOOK_OUT": str(OUT),
    "APP_DIR": str(ROOT / "apps" / "desktop"),
}
env.pop("ELECTRON_RUN_AS_NODE", None)

print("  ═══ 用 Electron 起 iohook 测试 ═══")
proc = subprocess.Popen(
    [str(ELECTRON), str(MAIN), "--no-sandbox"],
    cwd=str(TMP),
    env=env,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
print(f"  PID {proc.pid}")
time.sleep(6)

print()
print("  ══════════════════════════════════════════════════════════")
print("  👉 请**按住右 Alt 两秒再松开**，然后**随便按几个键**（Q/A/空格）")
print("     等 25 秒")
print("  ══════════════════════════════════════════════════════════")
print()

seen = 0
for i in range(25):
    time.sleep(1)
    try:
        ls = [l.strip() for l in OUT.read_text(encoding="utf-8", errors="replace").split("\n") if l.strip()]
    except OSError:
        ls = []
    keys = [l for l in ls if l.startswith(("DOWN", "UP", "START", "REQUIRE"))]
    if len(keys) > seen:
        for l in keys[seen:]:
            print(f"     [{i + 1:2d}s] 🔑 {l}")
        seen = len(keys)

try:
    proc.terminate()
    proc.wait(timeout=5)
except Exception:  # noqa: BLE001
    try:
        proc.kill()
    except Exception:  # noqa: BLE001
        pass

print()
print("  ═══ 结果 ═══")
try:
    ls = [l.strip() for l in OUT.read_text(encoding="utf-8", errors="replace").split("\n") if l.strip()]
except OSError:
    ls = []
for l in ls:
    if not l.startswith("TICK"):
        print(f"     {l}")

downs = [l for l in ls if l.startswith("DOWN")]
ups = [l for l in ls if l.startswith("UP")]
print()
print("  ═══ 判读 ═══")
if any(l.startswith("REQUIRE-FAIL") for l in ls):
    print("     ❌ iohook 加载失败 —— 看上面的 REQUIRE-FAIL（多半是 ABI 不匹配，要重编译）")
elif any(l.startswith("START-FAIL") for l in ls):
    print("     ❌ iohook.start() 失败 —— 看 START-FAIL")
elif downs:
    print(f"     🎉🎉 **iohook 收到了按键！DOWN {len(downs)} / UP {len(ups)}**")
    print("        -> 它和前面 4 个方案的差别是本质的，可以接进产品")
    print()
    print("        原始值（用来确定该用哪套编码匹配）：")
    for l in downs[:8]:
        print(f"          {l}")
elif any(l.startswith("START-OK") for l in ls):
    print("     ❌ 启动成功但**一个按键都没收到**（和 uiohook 一样）")
else:
    print("     ❌ 连日志都没写出来 —— 进程可能没起来")

for p in (MAIN,):
    try:
        p.unlink()
    except OSError:
        pass
