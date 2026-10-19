"""在**干净的 Electron** 里测 `uiohook-napi` —— 这是唯一还没排除的可能性。

## 为什么之前那个否定结论不可靠

`uiohook-napi` 我只在**自己起的 Node 进程**里测过，结果 0 个事件。
但低层键盘钩子（`WH_KEYBOARD_LL`）**依赖调用线程有消息循环**：

```
pwsh 脚本 / 纯 Node 进程  -> 没有 GUI 消息循环  -> 钩子收不到事件
Electron 主进程           -> 有消息循环         -> 可能就收得到
```

而实测确实出现过这个分野：同一个 PowerShell 钩子脚本，
**Electron 起的时候收到过** `DOWN 163` / `UP 165`，我起的时候 0 个。

所以"uiohook 收不到"这个结论**必须用 Electron 重测**才能算数。

## 这个测试做什么

最小 Electron 程序（不显示窗口、不加载任何产品代码）：
  · `require('uiohook-napi')` -> `start()`
  · 记录**每一个** keydown/keyup 的原始 keycode
  · 写日志文件，我这边读

## 要回答的

  ① Electron 里 uiohook 收不收得到键（是/否）
  ② 它报的 keycode 属于哪套编码（`UiohookKey` 表 vs Windows VK）
     —— 这直接决定产品里 `vkToUiohook()` 该怎么写
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
OUT = TMP / "_uiohook-electron.log"
MAIN = TMP / "_uiohook-electron.cjs"
PKG = next(
    (ROOT / "node_modules" / ".pnpm").glob("uiohook-napi@*/node_modules/uiohook-napi")
)

ELECTRON = (
    ROOT / "node_modules" / ".pnpm" / "electron@32.3.3" / "node_modules"
    / "electron" / "dist" / "electron.exe"
)
if not ELECTRON.is_file():
    print(f"  ❌ 找不到 electron: {ELECTRON}")
    raise SystemExit(1)

MAIN_SRC = r'''
const fs = require('fs');
const { app, BrowserWindow } = require('electron');

const out = process.env.UIO_OUT;
const w = fs.createWriteStream(out, { flags: 'w' });
const log = (s) => { try { w.write(s + '\n'); } catch (e) {} };

log('ELECTRON-READY');

// 有窗口才算"正常的 Electron GUI 进程"（低层钩子依赖 GUI 消息循环）
app.whenReady().then(() => {
  const win = new BrowserWindow({ show: false, width: 200, height: 200 });
  log('WINDOW-CREATED');

  let uIOhook, UiohookKey;
  try {
    const m = require(process.env.UIO_PKG);
    uIOhook = m.uIOhook;
    UiohookKey = m.UiohookKey;
    log('REQUIRE-OK');
  } catch (e) {
    log('REQUIRE-FAIL ' + String(e && e.message).slice(0, 300));
    app.quit();
    return;
  }

  log('TABLE Q=' + UiohookKey.Q + ' A=' + UiohookKey.A + ' F9=' + UiohookKey.F9 +
      ' AltRight=' + UiohookKey.AltRight + ' CtrlRight=' + UiohookKey.CtrlRight);

  uIOhook.on('keydown', (e) => log('DOWN ' + e.keycode));
  uIOhook.on('keyup',   (e) => log('UP ' + e.keycode));

  try { uIOhook.start(); log('START-OK'); }
  catch (e) { log('START-FAIL ' + String(e && e.message).slice(0, 300)); }

  setInterval(() => log('TICK'), 2000);
});

app.on('window-all-closed', () => {});
'''

MAIN.write_text(MAIN_SRC, encoding="utf-8")
try:
    OUT.unlink()
except OSError:
    pass

env = {
    **os.environ,
    "UIO_OUT": str(OUT),
    "UIO_PKG": str(PKG),
}
env.pop("ELECTRON_RUN_AS_NODE", None)

print("  ═══ 起干净的 Electron + uiohook ═══")
proc = subprocess.Popen(
    [str(ELECTRON), str(MAIN), "--no-sandbox"],
    cwd=str(TMP), env=env,
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
print(f"  PID {proc.pid}")
time.sleep(6)

try:
    boot = OUT.read_text(encoding="utf-8", errors="replace")
except OSError:
    boot = ""
print("  启动：")
for l in boot.split("\n"):
    if l.strip() and not l.startswith("TICK"):
        print(f"    {l.strip()[:110]}")

if "START-OK" not in boot:
    print()
    print("  ❌ 没起来 —— 不用按键了")
    try:
        proc.terminate()
    except Exception:  # noqa: BLE001
        pass
    raise SystemExit(0)

print()
print("  ══════════════════════════════════════════════════════════")
print("  👉 请**按住右 Alt 两秒再松开**，然后按 Q / A / 空格 / F9")
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
    keys = [l for l in ls if l.startswith(("DOWN", "UP"))]
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
downs = [int(l.split()[1]) for l in ls if l.startswith("DOWN")]
ups = [int(l.split()[1]) for l in ls if l.startswith("UP")]
print(f"     DOWN {len(downs)} 个: {downs}")
print(f"     UP   {len(ups)} 个: {ups}")

print()
print("  ═══ 判读 ═══")
if downs:
    print("     🎉🎉 **Electron 里 uiohook 收到了按键！**")
    print("        -> 之前'收不到'是因为**没有消息循环**，不是库的问题")
    print()
    print(f"     实际 keycode: {sorted(set(downs))}")
    print("     UiohookKey 参考: Q=16 A=30 Space=57 F9=67 AltRight=3640 CtrlRight=3613")
    print("     Windows VK 参考: Q=81 A=65 Space=32 F9=120 右Alt=165 右Ctrl=163")
    if any(v in (16, 30, 57, 67, 3640, 3613) for v in downs):
        print("     -> 是 **UiohookKey 那套**（我的映射思路对）")
    elif any(v in (81, 65, 32, 120, 165, 163) for v in downs):
        print("     -> 是 **Windows VK** —— 映射表要改成直接用 VK")
    else:
        print("     -> 都不是已知参考值，看上面的实际数字")
    if ups:
        print()
        print("     ✅ **而且松开也收到了** —— 真正的「按住说话」成立")
else:
    print("     ❌ Electron 里也收不到 —— uiohook 这条路可以彻底排除了")

for p in (MAIN,):
    try:
        p.unlink()
    except OSError:
        pass
