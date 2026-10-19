"""验证 `before-input-event` —— 一条**没用过**的路（Electron 官方文档指出的）。

## 为什么它可能解决问题

官方文档（Keyboard Shortcuts 教程）原文：

> The `before-input-event` event is emitted **before dispatching `keydown` and
> `keyup`** events in the renderer process.

两个关键点：

  ① **它同时给 keydown 和 keyup** —— 而 `globalShortcut` **只有按下**，
     这正是"按住说话"一直缺的那一半。
  ② 事件对象里有 `input.control` / `input.alt` / `input.shift` / `input.meta`
     —— **单个修饰键（右 Ctrl / 右 Alt）也能识别**，不需要 key code。

而 accelerator 的死结是（同一份文档）：

> These strings can contain multiple modifier keys and **a single key code**

修饰键列表（`Control` / `Alt` / `Shift` / `Super`…）里**没有一个是 key code**，
所以 `'Ctrl'` 这种字符串非法 —— `globalShortcut` 收不了单独的修饰键。

## ⚠️ 先验证最大的未知数：**窗口失焦时它还触发吗？**

这是成败关键。用户之前的观察是"只有在 Electron 窗口内才可以触发事件"。

## 这个测试怎么设计

起一个**不显示、不聚焦**的 Electron 窗口，挂上 `before-input-event`，
打印每一个事件（含 key/control/alt/shift/type）。
**你什么都不用做，只要在别的程序里按几下键** —— 如果窗口失焦时也能收到，
就说明这条路能替代全局钩子。
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
OUT = TMP / "_beforeinput.log"
MAIN = TMP / "_beforeinput.cjs"

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

const out = process.env.BI_OUT;
const w = fs.createWriteStream(out, { flags: 'w' });
const log = (s) => { try { w.write(s + '\n'); } catch (e) {} };

log('ELECTRON-READY');

app.whenReady().then(() => {
  /*
   * ⚠️ 刻意**不显示窗口**（show:false）。
   *
   * 要验证的正是"窗口失焦/不可见时 before-input-event 还触发吗" ——
   * 这是这条路能不能替代全局钩子的唯一关键。
   */
  const win = new BrowserWindow({
    show: false,
    width: 300,
    height: 200,
    webPreferences: { backgroundThrottling: false },
  });
  win.loadURL('data:text/html,<h1>probe</h1>');
  log('WINDOW-CREATED');

  win.webContents.on('before-input-event', (event, input) => {
    log(
      'INPUT type=' + input.type +
      ' key=' + JSON.stringify(input.key) +
      ' code=' + JSON.stringify(input.code) +
      ' ctrl=' + input.control +
      ' alt=' + input.alt +
      ' shift=' + input.shift +
      ' meta=' + input.meta +
      ' isAutoRepeat=' + input.isAutoRepeat
    );
  });
  log('LISTENER-ATTACHED');

  setInterval(() => log('TICK'), 2000);
});

app.on('window-all-closed', () => {});
'''

MAIN.write_text(MAIN_SRC, encoding="utf-8")
try:
    OUT.unlink()
except OSError:
    pass

env = {**os.environ, "BI_OUT": str(OUT)}
env.pop("ELECTRON_RUN_AS_NODE", None)

print("  ═══ 起一个**不可见、不聚焦**的 Electron 窗口 ═══")
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
        print(f"    {l.strip()[:100]}")

if "LISTENER-ATTACHED" not in boot:
    print()
    print("  ❌ 监听没挂上")
    try:
        proc.terminate()
    except Exception:  # noqa: BLE001
        pass
    raise SystemExit(0)

print()
print("  ══════════════════════════════════════════════════════════")
print("  👉 请**切到别的程序**（浏览器/记事本都行），然后按：")
print("       · 右 Ctrl 一下")
print("       · 右 Alt 一下")
print("       · 再随便打个字母（比如 a）")
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
    keys = [l for l in ls if l.startswith("INPUT")]
    if len(keys) > seen:
        for l in keys[seen:]:
            print(f"     [{i + 1:2d}s] ⌨️  {l[:112]}")
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
inputs = [l for l in ls if l.startswith("INPUT")]
downs = [l for l in inputs if "type=keyDown" in l]
ups = [l for l in inputs if "type=keyUp" in l]
print(f"     收到 {len(inputs)} 个事件（keyDown {len(downs)} / keyUp {len(ups)}）")
for l in inputs[:10]:
    print(f"       {l[:116]}")

print()
print("  ═══ 判读 ═══")
if inputs and ups:
    print("     🎉🎉 **窗口失焦时也收得到，而且 keydown + keyup 都有！**")
    print("        -> 这条路能替代全局钩子，而且**支持单个修饰键**")
    ctrl = [l for l in inputs if "ctrl=true" in l]
    alt = [l for l in inputs if "alt=true" in l]
    if ctrl:
        print(f"        ✅ 右 Ctrl 被识别到了（{len(ctrl)} 个事件，ctrl=true）")
    if alt:
        print(f"        ✅ 右 Alt 被识别到了（{len(alt)} 个事件，alt=true）")
elif inputs:
    print("     ⚠️ 收到事件但**没有 keyUp** —— 那就只能做切换式")
elif "LISTENER-ATTACHED" in "\n".join(ls):
    print("     ❌ 失焦时**收不到任何事件** —— 这条路只在窗口聚焦时有效")
else:
    print("     ❌ 没收到，看上面的启动信息")

for p in (MAIN,):
    try:
        p.unlink()
    except OSError:
        pass
