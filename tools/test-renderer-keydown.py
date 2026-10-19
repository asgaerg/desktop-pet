"""验证：宠物窗口（即使失焦）能不能收到键盘事件？

## 为什么突然要测这个

时间线显示：**18:47 之后监视器再没认到过右 Alt**：

```
18:47:47   监视器输出 <- UP 165      <- 最后一次
19:18:30   应用重启
之后        0 条
```

而 `GetAsyncKeyState` 是**系统级 API**，读不到键只有两种解释：
  · PowerShell 子进程被限制了（但心跳证明循环在跑）
  · **这个键状态在它的进程里就是不可见**

**但主进程启动日志里这三行永远是成功的**：

```
注册快捷键 CommandOrControl+Alt+P（显示/隐藏宠物）：成功
注册快捷键 CommandOrControl+Alt+L（聚焦输入框）：成功
注册快捷键 CommandOrControl+Alt+Q（免打扰开关）：成功
```

那是 **Electron 的 `globalShortcut`** —— 走的是另一套机制。

## 所以这个脚本测什么

从 CDP 往宠物窗口**注入键盘事件**（`Input.dispatchKeyEvent`），
看 `app.js` 里 `document.addEventListener('keydown')` 收不收得到。

## 判据

  · 收得到 -> **可以用"窗口键盘事件"这条更可靠的路**替代 PowerShell 轮询
  · 收不到 -> 这条路也不通，得另想

⚠️ 这个测试**只验机制可行性**（能不能收到事件），
   不代表"失焦时也能收到" —— 那要靠 `globalShortcut` 或窗口内事件。
   宠物窗口大部分时间是**失焦**的（它 `skipTaskbar` + 穿透），
   所以真正可用的只有 `globalShortcut`。这个脚本同时确认
   `document.hasFocus()` 的当前值，供判断。
"""

from __future__ import annotations

import base64
import json
import os
import socket
import struct
import sys
import time
import urllib.request

sys.path.insert(0, ".")
from tool_bootstrap import ROOT, setup  # noqa: E402

setup(verbose=False)


def ws_open(url: str, timeout: int = 30):
    rest = url.split("://", 1)[1]
    hp, path = rest.split("/", 1)
    h, p = hp.split(":")
    k = base64.b64encode(os.urandom(16)).decode()
    s = socket.create_connection((h, int(p)), timeout=10)
    s.sendall(
        (
            f"GET /{path} HTTP/1.1\r\nHost: {hp}\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {k}\r\n"
            f"Sec-WebSocket-Version: 13\r\n\r\n"
        ).encode()
    )
    b = b""
    while b"\r\n\r\n" not in b:
        b += s.recv(4096)
    s.settimeout(timeout)
    return s


def cdp(url: str, method: str, params: dict | None = None, timeout: int = 20):
    s = ws_open(url, timeout)
    try:
        pl = json.dumps({"id": 1, "method": method, "params": params or {}}).encode()
        hd = bytearray([0x81])
        n = len(pl)
        if n < 126:
            hd.append(0x80 | n)
        elif n < 65536:
            hd.append(0x80 | 126)
            hd += struct.pack(">H", n)
        mk = os.urandom(4)
        hd += mk
        s.sendall(bytes(hd) + bytes(b ^ mk[i % 4] for i, b in enumerate(pl)))

        def rd(n):
            o = b""
            while len(o) < n:
                c = s.recv(n - len(o))
                if not c:
                    raise ConnectionError
                o += c
            return o

        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            b0, b1 = rd(2)
            ln = b1 & 127
            masked = b1 & 128
            if ln == 126:
                ln = struct.unpack(">H", rd(2))[0]
            elif ln == 127:
                ln = struct.unpack(">Q", rd(8))[0]
            m = rd(4) if masked else b""
            pay = rd(ln) if ln else b""
            if masked:
                pay = bytes(b ^ m[i % 4] for i, b in enumerate(pay))
            if b0 & 15 != 1:
                continue
            o = json.loads(pay.decode())
            if o.get("id") == 1:
                r = o.get("result", {})
                if "exceptionDetails" in r:
                    return {"__err": json.dumps(r["exceptionDetails"], ensure_ascii=False)[:200]}
                return r
        return {"__err": "(超时)"}
    finally:
        try:
            s.close()
        except OSError:
            pass


pages = json.loads(
    urllib.request.urlopen("http://127.0.0.1:9222/json/list", timeout=5).read().decode()
)
win = next((x for x in pages if "index.html" in (x.get("url") or "")), None)
if not win:
    print("  ❌ 找不到宠物窗口")
    raise SystemExit(1)
U = win["webSocketDebuggerUrl"]

print("  ═══ ① 宠物窗口的焦点状态 ═══")
r = cdp(U, "Runtime.evaluate", {"expression": "document.hasFocus()", "returnByValue": True})
print(f"     document.hasFocus() = {r.get('result', {}).get('value')}")
print("     （宠物窗口 `skipTaskbar` + 穿透，**大部分时间失焦** ——")
print("      所以「窗口内键盘事件」这条路只在聚焦时有用）")
print()

print("  ═══ ② 装一个监听器，看能不能收到注入的键盘事件 ═══")
cdp(
    U,
    "Runtime.evaluate",
    {
        "expression": """
          window.__keyTest = [];
          if (!window.__keyTestInstalled) {
            window.__keyTestInstalled = true;
            document.addEventListener('keydown', (e) => {
              window.__keyTest.push({ type: 'keydown', code: e.code, key: e.key, keyCode: e.keyCode });
            });
            document.addEventListener('keyup', (e) => {
              window.__keyTest.push({ type: 'keyup', code: e.code, key: e.key, keyCode: e.keyCode });
            });
          }
          'installed'
        """,
        "returnByValue": True,
    },
)
print("     已装 keydown/keyup 监听")

print()
print("  ═══ ③ 注入右 Alt 的按下/松开 ═══")
# Alt 是修饰键：CDP 需要同时给 modifiers
cdp(
    U,
    "Input.dispatchKeyEvent",
    {
        "type": "rawKeyDown",
        "windowsVirtualKeyCode": 0xA5,
        "nativeVirtualKeyCode": 0xA5,
        "code": "AltRight",
        "key": "Alt",
        "modifiers": 1,  # Alt
    },
)
time.sleep(0.3)
cdp(
    U,
    "Input.dispatchKeyEvent",
    {
        "type": "keyUp",
        "windowsVirtualKeyCode": 0xA5,
        "nativeVirtualKeyCode": 0xA5,
        "code": "AltRight",
        "key": "Alt",
        "modifiers": 0,
    },
)
time.sleep(0.5)

r = cdp(
    U,
    "Runtime.evaluate",
    {"expression": "JSON.stringify(window.__keyTest || [])", "returnByValue": True},
)
got = r.get("result", {}).get("value", "[]")
print(f"     收到的事件：{got}")

print()
print("  ═══ ④ 判读 ═══")
try:
    events = json.loads(got)
except (ValueError, TypeError):
    events = []
if events:
    print(f"     ✅ 窗口能收到键盘事件（{len(events)} 个）—— 这条路是通的")
    for e in events:
        print(f"        {e}")
    print()
    print("     ⚠️ **但这只在窗口聚焦时有用**（宠物大部分时间失焦）。")
    print("        所以真正可用的方案是 **Electron `globalShortcut`** ——")
    print("        它已经在用了（Ctrl+Alt+P/L/Q 都注册成功），")
    print("        而且**不受焦点影响**。")
    print()
    print("        代价：`globalShortcut` **只有按下、没有松开**")
    print("        （见 `pushToTalk.ts` 文件头）。所以要用")
    print("        「按一次开始 / 再按一次结束」的**切换式**，")
    print("        而不是「按住」式。")
else:
    print("     ❌ 注入的键盘事件没被收到")
    print("        （也可能 CDP 的 dispatchKeyEvent 需要窗口聚焦 —— 那就没法这么测）")
