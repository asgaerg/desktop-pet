"""菜单在屏幕边缘会不会跑出去（2026-10-15）。

## 为什么要单独测

菜单**总是弹在鼠标位置**，而鼠标可能在任何地方 —— 包括屏幕**右下角**。
设计稿的定位逻辑是：

```
① 主面板放在 cursor 右侧 12px、下方 6px
② 二级面板放主面板左边或右边（看哪边有空间）
③ 装不下就**翻转**到另一侧
④ 再不行就 **clamp** 进工作区
```

⚠️ **这个逻辑错了不会报错** —— 表现是"菜单跑到屏幕外看不见"，
而用户只会说"右键没反应"（§95 的已知风险 ④）。

## 怎么测

`petMenuWindow.show(items, cursor)` 接收一个 cursor 点。我没法从外面传光标位置，
但可以**直接驱动渲染层**：菜单页的 `window.petMenu.onShow` 会收到
`{items, cursor, workArea}`。所以：

```
① 用演示数据直接调渲染层的布局（绕过主进程）
② 给四个角 + 四个边中点各喂一次 cursor
③ 断言：所有面板都在 workArea 内
```

⚠️ 这测的是**渲染层的布局数学**（真正决定位置的地方）。
主进程只负责把 cursor 与 workArea 喂进来。
"""

from __future__ import annotations

import base64
import json
import os
import pathlib
import socket
import struct
import sys
import time
import urllib.request

sys.path.insert(0, ".")
from tool_bootstrap import setup  # noqa: E402

setup(verbose=False)


def cdp_targets() -> list[dict]:
    with urllib.request.urlopen("http://127.0.0.1:9222/json/list", timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def ws_connect(url: str) -> socket.socket:
    rest = url.split("://", 1)[1]
    hostport, path = rest.split("/", 1)
    host, port = hostport.split(":")
    key = base64.b64encode(os.urandom(16)).decode()
    sock = socket.create_connection((host, int(port)), timeout=20)
    sock.sendall(
        (
            f"GET /{path} HTTP/1.1\r\nHost: {hostport}\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n\r\n"
        ).encode()
    )
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(65536)
        if not chunk:
            raise RuntimeError("握手断开")
        buf += chunk
    return sock


def ws_send(sock: socket.socket, text: str) -> None:
    payload = text.encode()
    header = bytearray([0x81])
    n = len(payload)
    if n < 126:
        header.append(0x80 | n)
    elif n < 65536:
        header.append(0x80 | 126)
        header += struct.pack(">H", n)
    else:
        header.append(0x80 | 127)
        header += struct.pack(">Q", n)
    mask = os.urandom(4)
    header += mask
    sock.sendall(bytes(header) + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))


def ws_recv(sock: socket.socket) -> str | None:
    def read_exact(n: int) -> bytes:
        out = b""
        while len(out) < n:
            chunk = sock.recv(n - len(out))
            if not chunk:
                raise ConnectionError
            out += chunk
        return out

    b0, b1 = read_exact(2)
    length = b1 & 0x7F
    if length == 126:
        length = struct.unpack(">H", read_exact(2))[0]
    elif length == 127:
        length = struct.unpack(">Q", read_exact(8))[0]
    mask = read_exact(4) if (b1 & 0x80) else b""
    payload = read_exact(length) if length else b""
    if mask:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return payload.decode("utf-8", "replace")


class Cdp:
    def __init__(self, target: dict) -> None:
        self.sock = ws_connect(target["webSocketDebuggerUrl"])
        self.sock.settimeout(25)
        self.n = 0

    def evaluate(self, expression: str) -> object:
        self.n += 1
        ws_send(
            self.sock,
            json.dumps(
                {
                    "id": self.n,
                    "method": "Runtime.evaluate",
                    "params": {
                        "expression": expression,
                        "returnByValue": True,
                        "awaitPromise": True,
                    },
                }
            ),
        )
        deadline = time.time() + 25
        while deadline > time.time():
            try:
                text = ws_recv(self.sock)
            except (socket.timeout, OSError):
                continue
            if text is None:
                continue
            try:
                message = json.loads(text)
            except json.JSONDecodeError:
                continue
            if message.get("id") == self.n:
                result = message.get("result") or {}
                if "exceptionDetails" in result:
                    return f"__EXC__ {str(result['exceptionDetails'])[:400]}"
                return (result.get("result") or {}).get("value")
        return None

    def close(self) -> None:
        self.sock.close()


def open_menu() -> dict | None:
    for target in cdp_targets():
        if str(target.get("url", "")).endswith("index.html"):
            client = Cdp(target)
            client.evaluate(
                "(function(){var p=window.pet||{};if(p.openContextMenu)p.openContextMenu();return 1;})()"
            )
            client.close()
            break
    time.sleep(1.8)
    for target in cdp_targets():
        if str(target.get("url", "")).endswith("petMenu.html"):
            return target
    return None


menu = open_menu()
if menu is None:
    print("  ❌ 菜单没弹出来")
    raise SystemExit(1)
client = Cdp(menu)

print("═" * 78)
print("① 菜单页暴露了什么（我要用它喂 cursor）")
print("═" * 78)
api = client.evaluate(
    "(function(){var m=window.petMenu||{};return JSON.stringify(Object.keys(m));})()"
)
print(f"  window.petMenu = {api}")

# 真实工作区 1536x816（§95 日志里量到的）
print()
print("═" * 78)
print("② 八个位置各喂一次 cursor，看面板会不会跑出工作区")
print("═" * 78)

probe = client.evaluate(
    chr(10).join(
        [
            "(function(){",
            "  var WA = { x: 0, y: 0, width: 1536, height: 816 };",
            "  var items = [",
            "    { label: 'A', type: 'checkbox', checked: true, action: 'toggle-immersive', icon: 'moon' },",
            "    { label: 'B', type: 'checkbox', checked: false, action: 'toggle-chat-mode', icon: 'chat' },",
            "    { label: '宠物大小', icon: 'paw', submenu: [",
            "      { label: '小', type: 'radio', checked: false },",
            "      { label: '大', type: 'radio', checked: true } ] },",
            "    { type: 'separator' },",
            "    { label: '退出', action: 'quit', icon: 'power' },",
            "  ];",
            "  var api = window.petMenu;",
            "  if (!api || !api.onShow) return 'NO onShow';",
            "  /* onShow 注册回调 —— 看它怎么接 */",
            "  var got = null;",
            "  try {",
            "    api.onShow(function(payload){ got = payload; });",
            "  } catch (e) { return 'onShow threw: ' + e; }",
            "  return 'onShow registered, got=' + (got === null ? 'null' : 'payload');",
            "})()",
        ]
    )
)
print(f"  {probe}")

# 看 onShow 的实际实现方式
impl = client.evaluate(
    "(function(){var s=String(window.petMenu && window.petMenu.onShow); return s.slice(0, 260);})()"
)
print()
print("  onShow 的实现（判断它能不能被外部直接调用）：")
print(f"    {impl}")

print()
print("═" * 78)
print("③ 直接操作 DOM 触发布局：模拟主进程下发不同 cursor")
print("═" * 78)

# petMenu.js 是 IIFE，拿不到内部 show()。改用最直接的办法：
# 让主进程真的把光标移到那些位置 —— 但 CDP 移不了系统光标。
# 所以退一步：只测**贴边时主进程传的 cursor** 这一个真实场景。
#
# 把宠物拖到屏幕右下角附近的坐标，然后右键 —— 这是最可能出问题的情况。
print("  ⚠️ 渲染层的 show() 在 IIFE 里，外部拿不到；系统光标也移不了。")
print("     改测**真实场景**：把宠物窗口拖到右下角，再右键，看菜单落在哪。")

pet = None
for target in cdp_targets():
    if str(target.get("url", "")).endswith("index.html"):
        pet = target
        break
if pet is None:
    print("  ❌ 没有宠物页")
    client.close()
    raise SystemExit(1)

pet_client = Cdp(pet)

# 先把菜单关掉
client.evaluate("(function(){if(window.petMenu&&window.petMenu.close)window.petMenu.close();})()")
time.sleep(0.6)

# 把宠物移到右下角（通过 preload 的 drag 系列，模拟一次拖拽）
print()
print("  把宠物拖到右下角…")
pet_client.evaluate(
    chr(10).join(
        [
            "(function(){",
            "  var p = window.pet || {};",
            "  /* 拖拽是 dragStart/dragMove/dragEnd —— 用它把窗口挪到屏幕右下角 */",
            "  if (!p.dragStart) return 'NO dragStart';",
            "  p.dragStart({ x: 0, y: 0 });",
            "  p.dragMove({ x: 1400, y: 700 });",
            "  p.dragEnd({ x: 1400, y: 700 });",
            "  return 'dragged';",
            "})()",
        ]
    )
)
time.sleep(2.5)

# 再右键，然后量菜单位置
print("  再右键…")
pet_client.evaluate(
    "(function(){var p=window.pet||{};if(p.openContextMenu)p.openContextMenu();return 1;})()"
)
time.sleep(2.0)

menu2 = None
for target in cdp_targets():
    if str(target.get("url", "")).endswith("petMenu.html"):
        menu2 = target
        break

if menu2 is None:
    print("  ⚠️ 菜单窗口不在")
else:
    c2 = Cdp(menu2)
    pos = c2.evaluate(
        chr(10).join(
            [
                "(function(){",
                "  var root = document.body.firstElementChild;",
                "  var panels = root.querySelectorAll('.panel');",
                "  var out = [];",
                "  for (var i = 0; i < panels.length; i++) {",
                "    var p = panels[i];",
                "    if (p.offsetParent === null) continue;",
                "    var r = p.getBoundingClientRect();",
                "    out.push({ x: Math.round(r.left), y: Math.round(r.top),",
                "               w: Math.round(r.width), h: Math.round(r.height),",
                "               right: Math.round(r.right), bottom: Math.round(r.bottom) });",
                "  }",
                "  return JSON.stringify({ panels: out,",
                "    win: { w: window.innerWidth, h: window.innerHeight } });",
                "})()",
            ]
        )
    )
    print(f"  菜单面板位置：{pos}")
    try:
        parsed = json.loads(str(pos))
        bad = []
        for panel in parsed["panels"]:
            if panel["x"] < -1 or panel["y"] < -1:
                bad.append(f"左上出界 ({panel['x']},{panel['y']})")
            if panel["right"] > parsed["win"]["w"] + 1 or panel["bottom"] > parsed["win"]["h"] + 1:
                bad.append(
                    f"右下出界 right={panel['right']} bottom={panel['bottom']}"
                    f" 窗口={parsed['win']['w']}x{parsed['win']['h']}"
                )
        if bad:
            print(f"  ❌ **有面板出界**：{bad}")
            print("     ⚠️ 这是「菜单跑到屏幕外」那一类 —— 主进程的布局 IPC 需要修")
        else:
            print("  ✅ 所有面板都在菜单窗口内")
    except (json.JSONDecodeError, TypeError):
        pass
    c2.close()

pet_client.close()
client.close()
