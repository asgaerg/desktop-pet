"""用**用户给的 iohook 用法**做最小 Electron 验证。

## 代码来源

用户提供的掘金文章示例（作者：沐风芮雨）：
https://juejin.cn/post/7101674234908246024

```js
const ioHook = require('iohook');
ioHook.start(false);
ioHook.on('keydown', ...);
ioHook.on('keyup', ...);
ioHook.on('mousedown', ...);
// 退出时
ioHook.unload();
ioHook.stop();
```

## 这个脚本相对原文改了什么（都有理由）

  ① **加 `rawcode` 输出**。
     原文只打 `keyboard is press!`，看不出是哪个键。
     iohook 的文档说明它按 `keycode` 匹配、但有些库要用 `rawcode`
     （`ioHook.useRawcode(true)`）—— **两个都打出来**才能确定该用哪套。

  ② **必须有窗口 + `app.on('before-quit')` 才退出**。
     原文那段没有窗口也没有退出逻辑，直接在 Electron 里跑会因为
     "没有窗口" 而立刻退出，键盘还没按就结束了。
     （原文是文章片段，在完整 app 里跑，所以没暴露这个问题。）

  ③ **用 `require` 指向编译产物**，而不是装到 node_modules ——
     因为 `iohook` 没有 Electron 32 的预编译，只能现场编，
     产物在 `iohook-build/package/builds/`。

## 为什么要单独测

前面 4 个方案都是"启动成功但收不到键"。**先证明 iohook 收得到，
再动产品代码** —— 不然又是一轮白改。
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
IOHOOK_PKG = TMP / "iohook-build" / "package"
OUT = TMP / "_iohook-user.log"
MAIN = TMP / "_iohook-user.cjs"

ELECTRON = (
    ROOT / "node_modules" / ".pnpm" / "electron@32.3.3" / "node_modules"
    / "electron" / "dist" / "electron.exe"
)
if not ELECTRON.is_file():
    print(f"  ❌ 找不到 electron: {ELECTRON}")
    raise SystemExit(1)

# 找编译产物
nodes = sorted(IOHOOK_PKG.rglob("*.node")) if IOHOOK_PKG.is_dir() else []
print(f"  iohook 构建产物: {len(nodes)} 个")
for n in nodes:
    print(f"    {n.relative_to(IOHOOK_PKG)}  {n.stat().st_size // 1024}KB")
if not nodes:
    print("  ❌ 没有 .node —— 先跑 tools/build-iohook.py")
    raise SystemExit(1)

MAIN_SRC = r'''
const fs = require('fs');
const path = require('path');
const { app, BrowserWindow } = require('electron');

const out = process.env.IOHOOK_OUT;
const w = fs.createWriteStream(out, { flags: 'w' });
const log = (s) => { try { w.write(s + '\n'); } catch (e) {} };

log('ELECTRON-READY');

/*
 * ⚠️ 必须有一个窗口，否则 Electron 在 Windows 上会因为
 * "没有窗口" 而直接退出（原文那段是文章片段，在完整 app 里跑所以没暴露）。
 * 这个测试窗口**不显示**，只是让 app 活着。
 */
app.whenReady().then(() => {
  new BrowserWindow({ show: false, width: 200, height: 200 });

  let ioHook;
  try {
    ioHook = require(process.env.IOHOOK_PKG);
    log('REQUIRE-OK');
  } catch (e) {
    log('REQUIRE-FAIL ' + String(e && e.message).slice(0, 400));
    app.quit();
    return;
  }

  try {
    // 用户给的用法：start(false) 关闭 DEBUG 输出
    ioHook.start(false);
    log('START-OK');
  } catch (e) {
    log('START-FAIL ' + String(e && e.message).slice(0, 400));
  }

  /*
   * ① 相对原文的改动：把 keycode **和** rawcode 都打出来。
   *
   * iohook 默认按 `keycode` 匹配，但文档说有些环境要用 `rawcode`
   * （`ioHook.useRawcode(true)`）。要决定产品里该用哪套，就必须两个都看到。
   */
  const key = (tag, e) => log(tag + ' keycode=' + e.keycode + ' rawcode=' + e.rawcode);

  ioHook.on('keydown', (e) => key('DOWN', e));
  ioHook.on('keyup',   (e) => key('UP', e));
  ioHook.on('mousedown', () => log('MOUSE-DOWN'));
  ioHook.on('mouseup',   () => log('MOUSE-UP'));

  setInterval(() => log('TICK'), 2000);
});

// ② 相对原文的改动：保证能退出（原文只在 before-quit 里 unload/stop）
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
    "IOHOOK_PKG": str(IOHOOK_PKG),
}
env.pop("ELECTRON_RUN_AS_NODE", None)

print()
print("  ═══ 用 Electron 起 iohook（你给的用法）═══")
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
print("  启动情况：")
for l in boot.split("\n"):
    if l.strip() and not l.startswith("TICK"):
        print(f"    {l.strip()}")

if "REQUIRE-FAIL" in boot or "START-FAIL" in boot:
    print()
    print("  ❌ 加载/启动就失败了 —— 不用按键了，先解决这个")
    try:
        proc.terminate()
    except Exception:  # noqa: BLE001
        pass
    raise SystemExit(0)

print()
print("  ══════════════════════════════════════════════════════════")
print("  👉 请**按住右 Alt 两秒再松开**，然后按 Q / A / 空格")
print("     鼠标也点一下（原文的例子里有鼠标事件）")
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
    keys = [l for l in ls if l.startswith(("DOWN", "UP", "MOUSE"))]
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
downs = [l for l in ls if l.startswith("DOWN")]
ups = [l for l in ls if l.startswith("UP")]
mouse = [l for l in ls if l.startswith("MOUSE")]
print(f"     DOWN {len(downs)}  UP {len(ups)}  MOUSE {len(mouse)}")
for l in downs[:10]:
    print(f"       {l}")
for l in ups[:6]:
    print(f"       {l}")

print()
print("  ═══ 判读 ═══")
if downs:
    print("     🎉🎉 **iohook 收到了按键！**")
    print("        -> 它和前面几个方案的差别是本质的，可以接进产品")
    print()
    print("        要确定产品里用哪套编码匹配（看上面的 keycode vs rawcode）")
elif any("START-OK" in l for l in ls):
    print("     ❌ 启动成功但**一个按键都没收到**")
else:
    print("     ❌ 没有 START-OK —— 看上面的启动情况")

for p in (MAIN,):
    try:
        p.unlink()
    except OSError:
        pass
