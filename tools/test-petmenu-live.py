"""自绘菜单全链路实测（2026-10-15）。

## 为什么单独测这个

另一个 AI 在 10-14/15 新建/改写了 8 个菜单相关文件：

```
src/main/petMenuWindow.ts      11044 B   无边框置顶窗口 + 定位
src/main/petMenuTemplate.ts    27390 B   菜单结构（数据）
src/main/petMenu.ts            20609 B   原生菜单退路
src/preload/menu.ts             3737 B   IPC 通道
src/renderer/petMenu.html       2563 B
src/renderer/petMenu.js        26878 B   渲染 + 交互
src/renderer/petMenu.css       25201 B   样式（已定稿）
src/renderer/petMenu.icons.css 27358 B   图标（evenodd 合并层）
```

**这一整套我只在 §93~95 里做过"视觉稿"，从没验过它在应用里真的能弹、能点。**

## ⚠️ 为什么必须用 CDP 而不是眼看

菜单**一闪而过**（点别处就关），没法量、没法截图对位。CDP 能：

```
① 发 pet:context-menu 触发 openPetMenu()
② 连上 petMenu 窗口的 CDP target，读 DOM 的实际几何
③ 断言：面板数、行高、图标是否渲染出图形、文字位置
④ 点一个开关项，确认 action 真的回报到主进程
```

⚠️ `.backup` 里有 10-14 的版本，改动前的对照只能靠 §95 记的数字。
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

LOG = pathlib.Path(r"F:\ai项目\apps\desktop\dist\startup.log")


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


def find_pet() -> dict | None:
    for target in cdp_targets():
        if str(target.get("url", "")).endswith("index.html"):
            return target
    return None


print("═" * 78)
print("① 从宠物页触发 openPetMenu（发 pet:context-menu IPC）")
print("═" * 78)

pet = find_pet()
if pet is None:
    print("  ❌ 没有宠物页")
    raise SystemExit(1)

trigger = Cdp(pet)
# pet:context-menu 是 ipcRenderer.send（单向），preload 里可能有封装，也可能要直接调
sent = trigger.evaluate(
    chr(10).join(
        [
            "(function(){",
            "  var p = window.pet || {};",
            "  if (typeof p.openContextMenu === 'function') { p.openContextMenu(); return 'via pet.openContextMenu'; }",
            "  return 'NO_API:' + Object.keys(p).join(',');",
            "})()",
        ]
    )
)
print(f"  触发结果：{sent}")
trigger.close()

time.sleep(2.5)

print()
print("═" * 78)
print("② 菜单窗口出现了吗")
print("═" * 78)

targets = cdp_targets()
print(f"  CDP target 数：{len(targets)}")
menu = None
for target in targets:
    url = str(target.get("url", ""))
    name = url.rsplit("/", 1)[-1].split("?")[0]
    print(f"    · {name:<22} {url[-58:]}")
    if name == "petMenu.html":
        menu = target

if menu is None:
    print()
    print("  ⚠️ 没看到 petMenu.html —— 可能：")
    print("     · 菜单窗口用的是 show:false 建的（不出现在 CDP 列表里）")
    print("     · 或者退回原生菜单了（看 startup.log 的 [菜单] 行）")
    print()
    print("  startup.log 里的菜单相关行：")
    if LOG.exists():
        hits = [
            line.strip()
            for line in LOG.read_text(encoding="utf-8", errors="replace").split("\n")
            if "[菜单]" in line
        ]
        for line in hits[-8:]:
            print(f"    {line[:150]}")
        if not hits:
            print("    （一条都没有）")
    raise SystemExit(0)

client = Cdp(menu)
print()
print("═" * 78)
print("③ 菜单的实际几何（拿 §95 记的数字对）")
print("═" * 78)

geom = client.evaluate(
    chr(10).join(
        [
            "(function(){",
            "  var root = document.getElementById('root') || document.body.firstElementChild;",
            "  var out = { rootFound: !!root };",
            "  if (!root) return JSON.stringify(out);",
            "  var r = root.getBoundingClientRect();",
            "  out.panel = { w: Math.round(r.width), h: Math.round(r.height),",
            "                x: Math.round(r.left), y: Math.round(r.top) };",
            "  /* 面板数（主面板 + 已展开的二级） */",
            "  var panels = root.querySelectorAll('[class*=panel]');",
            "  out.panelCount = panels.length;",
            "  /* 行 */",
            "  var rows = root.querySelectorAll('[class*=row]');",
            "  out.rowCount = rows.length;",
            "  if (rows.length > 1) {",
            "    var r0 = rows[0].getBoundingClientRect();",
            "    var r1 = rows[1].getBoundingClientRect();",
            "    out.rowHeight = Math.round(r1.top - r0.top);",
            "    out.row0Height = Math.round(r0.height);",
            "  }",
            "  /* 图标：有没有真的画出图形（这是最容易静默失败的一环） */",
            "  var paths = root.querySelectorAll('path, svg');",
            "  out.svgCount = root.querySelectorAll('svg').length;",
            "  out.pathCount = paths.length;",
            "  out.pathsWithD = 0;",
            "  for (var i = 0; i < paths.length; i++) {",
            "    if (paths[i].tagName === 'path' && paths[i].getAttribute('d')) out.pathsWithD++;",
            "  }",
            "  /* 文本位置（设计稿：x43 / 字号 13） */",
            "  var labels = root.querySelectorAll('[class*=label]');",
            "  out.labelCount = labels.length;",
            "  if (labels.length) {",
            "    var lb = labels[0];",
            "    var lr = lb.getBoundingClientRect();",
            "    out.firstLabelLeft = Math.round(lr.left - r.left);",
            "    out.firstLabelFont = getComputedStyle(lb).fontSize;",
            "    out.firstLabelText = lb.textContent.slice(0, 24);",
            "  }",
            "  /* 面板圆角（设计稿 R14） */",
            "  out.borderRadius = getComputedStyle(root).borderRadius;",
            "  return JSON.stringify(out);",
            "})()",
        ]
    )
)
print(f"  {geom}")

print()
print("═" * 78)
print("④ 菜单项文字（确认内容对得上）")
print("═" * 78)
rows_text = client.evaluate(
    chr(10).join(
        [
            "(function(){",
            "  var root = document.getElementById('root') || document.body.firstElementChild;",
            "  if (!root) return '[]';",
            "  var rows = root.querySelectorAll('[class*=row]');",
            "  var out = [];",
            "  for (var i = 0; i < rows.length; i++) {",
            "    var t = (rows[i].textContent || '').trim().replace(/\\s+/g, ' ');",
            "    if (t) out.push(t.slice(0, 40));",
            "  }",
            "  return JSON.stringify(out);",
            "})()",
        ]
    )
)
try:
    items = json.loads(str(rows_text))
    for i, item in enumerate(items):
        print(f"    {i:>2}. {item}")
except (json.JSONDecodeError, TypeError):
    print(f"    {rows_text}")

print()
print("═" * 78)
print("⑤ 点击一个开关项，确认 action 回报到主进程")
print("═" * 78)

before = LOG.stat().st_size if LOG.exists() else 0
clicked = client.evaluate(
    chr(10).join(
        [
            "(function(){",
            "  var root = document.getElementById('root') || document.body.firstElementChild;",
            "  if (!root) return 'no root';",
            "  /* 找「允许看屏幕」那一行 —— 它是个 checkbox，点了会写设置 */",
            "  var rows = root.querySelectorAll('[class*=row]');",
            "  for (var i = 0; i < rows.length; i++) {",
            "    if ((rows[i].textContent || '').indexOf('允许看屏幕') >= 0) {",
            "      rows[i].dispatchEvent(new MouseEvent('click', {bubbles: true}));",
            "      return 'clicked row ' + i;",
            "    }",
            "  }",
            "  return 'row not found';",
            "})()",
        ]
    )
)
print(f"  {clicked}")
time.sleep(2.0)

print()
print("  主进程日志新增（看有没有收到这个 action）：")
if LOG.exists():
    with LOG.open("r", encoding="utf-8", errors="replace") as handle:
        handle.seek(before)
        text = handle.read()
    for line in text.split("\n"):
        if line.strip():
            print(f"    {line.strip()[:150]}")
    if not text.strip():
        print("    （没有新增）")

client.close()

print()
print("═" * 78)
print("⑥ 菜单窗口的尺寸 vs 设计稿")
print("═" * 78)
print("  设计稿：面板 300x440（§95 实测 437），行高 38，图标左 x18，文字 x43，R14")
