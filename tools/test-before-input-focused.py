"""验证 `before-input-event` 在窗口**聚焦**时是否给 keydown + keyup。

## 上一轮的结果与它的局限

上一轮测的是"**失焦**时"，结果 **0 个事件** —— 与用户的观察一致
（"只有在 Electron 窗口内才可以触发事件"）。

但那个测试**没有回答关键问题**：聚焦时它给不给 **keyup**？

如果给，那么方案就成立了：

```
点一下宠物（或按全局热键把宠物叫到前台）
   -> 宠物窗口获得焦点
   -> 按住右 Ctrl / 右 Alt  = 开始采集（keydown）
   -> 松开                   = 结束采集（keyup）   <- 真正的「按住说话」
```

而且 `before-input-event` 读的是 `input.control` / `input.alt`，
**单个修饰键也能识别** —— 这正是 accelerator 做不到的。

## 这个测试怎么设计

  ① 窗口**显示并聚焦**（`show: true` + `focus()`）
  ② 挂 `before-input-event`，记录 key/ctrl/alt/type
  ③ 为了让我这边能自动判定而不必你按：**同时**用 `webContents.sendInputEvent`
     合成一次 keyDown + keyUp，看它是否也走这个事件（这只是辅助，
     真正的判据是你**真按**的那几下）
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
OUT = TMP / "_beforeinput2.log"
MAIN = TMP / "_beforeinput2.cjs"

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
  const win = new BrowserWindow({
    show: true,          // 这次**显示**它 —— 要聚焦才好按键
    width: 420,
    height: 240,
    title: 'before-input-event 测试窗口 —— 请在这个窗口里按键',
    webPreferences: { backgroundThrottling: false },
  });
  win.loadURL(
    'data:text/html,' +
      encodeURIComponent(
        '<body style="font:16px/1.6 system-ui;padding:16px;background:#1b1f24;color:#e6edf3">' +
        '<h3>请在这个窗口里按键</h3>' +
        '<p>按住 <b>右 Ctrl</b> 一秒再松开<br>再按住 <b>右 Alt</b> 一秒再松开<br>再随便打个字母</p>' +
        '</body>',
      ),
  );
  win.once('ready-to-show', () => {
    win.show();
    win.focus();
    log('WINDOW-SHOWN-FOCUSED');
  });

  win.webContents.on('before-input-event', (event, input) => {
    log(
      'INPUT type=' + input.type +
      ' key=' + JSON.stringify(input.key) +
      ' code=' + JSON.stringify(input.code) +
      ' ctrl=' + input.control +
      ' alt=' + input.alt +
      ' shift=' + input.shift +
      ' meta=' + input.meta +
      ' autoRepeat=' + input.isAutoRepeat
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

print("  ═══ 起一个**显示并聚焦**的 Electron 窗口 ═══")
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
for l in boot.split("\n"):
    if l.strip() and not l.startswith("TICK"):
        print(f"    {l.strip()[:100]}")

print()
print("  ══════════════════════════════════════════════════════════")
print("  👉 屏幕上会出现一个标题为「before-input-event 测试窗口」的窗口。")
print("     **点一下它让它获得焦点**，然后：")
print("       · 按住 右 Ctrl 一秒再松开")
print("       · 按住 右 Alt  一秒再松开")
print("       · 随便打个字母")
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
    keys = [l for l in ls if l.startswith("INPUT")]
    if len(keys) > seen:
        for l in keys[seen:]:
            print(f"     [{i + 1:2d}s] ⌨️  {l[:114]}")
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
print(f"     keyDown {len(downs)} / keyUp {len(ups)}")
for l in inputs[:14]:
    print(f"       {l[:118]}")

print()
print("  ═══ 判读 ═══")
if downs and ups:
    print("     🎉🎉 **聚焦时 keydown 和 keyup 都有！**")
    print("        -> **真正的「按住说话」成立**，而且不需要任何原生钩子")
    ctrl = [l for l in inputs if "ctrl=true" in l]
    alt = [l for l in inputs if "alt=true" in l]
    if ctrl:
        print(f"        ✅ 右 Ctrl 识别到了（ctrl=true，{len(ctrl)} 个事件）")
    if alt:
        print(f"        ✅ 右 Alt 识别到了（alt=true，{len(alt)} 个事件）")
    rep = [l for l in inputs if "autoRepeat=true" in l]
    print(f"        ℹ️ 自动重复事件 {len(rep)} 个（按住时会连发，需要去重）")
elif downs:
    print("     ⚠️ 有 keydown 没有 keyup —— 只能做切换式")
elif inputs:
    print("     ⚠️ 收到事件但没有 type=keyDown/keyUp —— 看上面的实际字段")
elif "LISTENER-ATTACHED" in "\n".join(ls):
    print("     ❌ 聚焦时也收不到 —— 这条路不行")
else:
    print("     ❌ 没起来，看启动信息")

for p in (MAIN,):
    try:
        p.unlink()
    except OSError:
        pass
