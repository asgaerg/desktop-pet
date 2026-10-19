"""菜单点击真的生效了吗 —— 查副作用（2026-10-15）。

## 为什么不能只看日志

主进程 `menu:action` 的日志**只在两种异常情况**下才写：

```
没有处理器表   -> log('[菜单] 收到点击但还没有处理器表')
找不到处理器   -> log('[菜单] 这个动作没有处理器')
```

⚠️ 走通的路**一行日志都没有** —— 所以「点击后没日志」**不能**推断"点击没生效"。
（我第一版就是这么误判的。）

## 正确办法：查副作用

菜单里那些开关项点下去都会**落盘或改窗口状态**。所以：

```
① 记下点之前的 settings.json / 窗口可见性
② 从菜单里点那一项
③ 再读一遍 —— 变了就是通了
```

⚠️ **只点幂等的、可恢复的项**（比如「允许看屏幕」「边缘探头」），
点完再点回来 —— 不要点「退出」「隐藏宠物」这种一去不回的。
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

SETTINGS = pathlib.Path(r"F:\ai项目\apps\desktop\pet-data\settings.json")
CONFIG = pathlib.Path(r"F:\ai项目\config\persona.yaml")


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
        self.sock.settimeout(20)
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
        deadline = time.time() + 20
        while time.time() < deadline:
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
                    return f"__EXC__ {str(result['exceptionDetails'])[:300]}"
                return (result.get("result") or {}).get("value")
        return None

    def close(self) -> None:
        self.sock.close()


def find(name: str) -> dict | None:
    for target in cdp_targets():
        if str(target.get("url", "")).endswith(name):
            return target
    return None


def read_settings() -> dict:
    try:
        return json.loads(SETTINGS.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def open_menu() -> dict | None:
    """触发菜单并返回菜单页的 CDP target。"""
    pet = find("index.html")
    if pet is None:
        return None
    client = Cdp(pet)
    client.evaluate(
        "(function(){var p=window.pet||{};if(p.openContextMenu)p.openContextMenu();return 1;})()"
    )
    client.close()
    time.sleep(1.8)
    return find("petMenu.html")


def click_row(label: str) -> str:
    """在菜单里点某个 label 对应的行。"""
    menu = find("petMenu.html")
    if menu is None:
        return "菜单窗口不在"
    client = Cdp(menu)
    result = client.evaluate(
        chr(10).join(
            [
                "(function(){",
                f"  var want = {json.dumps(label, ensure_ascii=False)};",
                "  var root = document.body.firstElementChild;",
                "  var rows = root.querySelectorAll('.row');",
                "  for (var i = 0; i < rows.length; i++) {",
                "    var t = (rows[i].textContent || '').trim();",
                "    if (t.indexOf(want) === 0 && rows[i].offsetParent !== null) {",
                "      rows[i].dispatchEvent(new MouseEvent('click', {bubbles: true}));",
                "      return 'clicked: ' + t.slice(0, 30);",
                "    }",
                "  }",
                "  return 'NOT_FOUND: ' + want;",
                "})()",
            ]
        )
    )
    client.close()
    return str(result)


print("═" * 78)
print("① 「允许看屏幕」开关 —— 点一下，看配置有没有真的变")
print("═" * 78)

before = read_settings()
vision_before = before.get("visionEnabled")
print(f"  点之前 settings.json: visionEnabled = {vision_before}")

opened = open_menu()
if opened is None:
    print("  ❌ 菜单没弹出来")
    raise SystemExit(1)
print("  菜单已弹出 ✅")

result = click_row("允许看屏幕")
print(f"  点击结果：{result}")
time.sleep(2.0)

after = read_settings()
vision_after = after.get("visionEnabled")
print(f"  点之后 settings.json: visionEnabled = {vision_after}")
if vision_before != vision_after:
    print(f"  ✅ **通了** —— 菜单点击真的改了配置（{vision_before} -> {vision_after}）")
else:
    print("  ❌ **没变** —— 菜单点击没有回报到主进程")
    print("     （或者 visionEnabled 落盘时机不同，需要看主进程 handleVision）")

print()
print("  再点回来（恢复原状）…")
if open_menu() is None:
    print("    ⚠️ 菜单没再弹出来")
else:
    print(f"    {click_row('允许看屏幕')}")
    time.sleep(1.5)
    restored = read_settings().get("visionEnabled")
    print(f"    visionEnabled = {restored} {'✅ 已恢复' if restored == vision_before else '⚠️ 没恢复回原值'}")

print()
print("═" * 78)
print("② 菜单窗口的几何（这次只量主面板）")
print("═" * 78)

if open_menu() is None:
    print("  ❌ 菜单没弹出来")
    raise SystemExit(0)

menu = find("petMenu.html")
client = Cdp(menu)
geom = client.evaluate(
    chr(10).join(
        [
            "(function(){",
            "  var out = {};",
            "  var root = document.body.firstElementChild;",
            "  /* 主面板 = 第一个可见的 .panel */",
            "  var panels = root.querySelectorAll('.panel');",
            "  out.panelCount = panels.length;",
            "  for (var i = 0; i < panels.length; i++) {",
            "    var p = panels[i];",
            "    if (p.offsetParent === null) continue;",
            "    var r = p.getBoundingClientRect();",
            "    out.mainPanel = { w: Math.round(r.width), h: Math.round(r.height),",
            "                      x: Math.round(r.left), y: Math.round(r.top) };",
            "    out.mainRadius = getComputedStyle(p).borderRadius;",
            "    out.mainBg = getComputedStyle(p).backgroundColor;",
            "    /* 第一行的图标与文字位置 */",
            "    var rows = p.querySelectorAll('.row');",
            "    out.rowsInPanel = rows.length;",
            "    if (rows.length) {",
            "      var ic = rows[0].querySelector('[class*=icon]');",
            "      if (ic) {",
            "        var ir = ic.getBoundingClientRect();",
            "        out.iconLeft = Math.round(ir.left - r.left);",
            "        out.iconW = Math.round(ir.width);",
            "        var cs = getComputedStyle(ic);",
            "        out.iconMask = (cs.maskImage || cs.webkitMaskImage || 'none').slice(0, 40);",
            "        out.iconBg = cs.backgroundColor;",
            "      }",
            "      var lb = rows[0].querySelector('.label') || rows[0].querySelector('[class*=label]');",
            "      if (lb) {",
            "        var lr = lb.getBoundingClientRect();",
            "        out.labelLeft = Math.round(lr.left - r.left);",
            "        out.labelFont = getComputedStyle(lb).fontSize;",
            "      }",
            "    }",
            "    break;",
            "  }",
            "  return JSON.stringify(out);",
            "})()",
        ]
    )
)
print(f"  {geom}")
try:
    parsed = json.loads(str(geom))
    print()
    print("  对照 §95 记录的数字：")
    print("    面板  设计 300x440（实测 437）")
    print("    行高  设计 38")
    print("    图标  左边缘 x18")
    print("    文字  x43，字号 13")
    print("    圆角  R14")
except (json.JSONDecodeError, TypeError):
    pass

client.close()
