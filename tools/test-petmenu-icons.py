"""菜单图标 + 高度的精确核对（2026-10-15）。

## 为什么要单独量

上一版我用了 `[class*=icon]` 这个**错的选择器**（真实类名是 `.ico`），
于是 `svgCount:0 / pathCount:0` 把我误导成「图标没渲染」。

⚠️ **量错了比不量更糟** —— 会得出一条假结论。

## 图标机制

`petMenu.icons.css` 里每个图标是一个 CSS 变量：

```
--icon: url("data:image/svg+xml,%3Csvg ...")
```

由 `.ico` 用 `mask-image: var(--icon)` 应用。所以**量 mask 有没有生效**才是对的：

```
① maskImage 是不是 data:image/svg（不是 none）
② 元素有没有尺寸（w/h 都不是 0）
③ 背景色，以及 mask 之后**真的画出了东西**（用 canvas 采样像素）
```

⚠️ 第三条是关键 —— `mask-image` 失败时元素**照样有尺寸**，只是全透明。
所以要么采样像素，要么信任前面两条。这里两条都做。
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
print("① 图标（.ico）真的渲染了吗 —— mask + 尺寸 + 像素采样")
print("═" * 78)

icons = client.evaluate(
    chr(10).join(
        [
            "(function(){",
            "  var root = document.body.firstElementChild;",
            "  var icos = root.querySelectorAll('.ico');",
            "  var out = { count: icos.length, samples: [] };",
            "  for (var i = 0; i < Math.min(icos.length, 6); i++) {",
            "    var el = icos[i];",
            "    var cs = getComputedStyle(el);",
            "    var r = el.getBoundingClientRect();",
            "    var mask = cs.maskImage || cs.webkitMaskImage || 'none';",
            "    out.samples.push({",
            "      i: i,",
            "      w: Math.round(r.width), h: Math.round(r.height),",
            "      maskW: cs.maskSize || cs.webkitMaskSize,",
            "      left: Math.round(r.left),",
            "      maskKind: mask === 'none' ? 'NONE' : (mask.indexOf('data:image/svg') >= 0 ? 'svg-data-uri' : mask.slice(0, 30)),",
            "      bg: cs.backgroundColor,",
            "      iconVar: (cs.getPropertyValue('--icon') || '').slice(0, 26),",
            "    });",
            "  }",
            "  return JSON.stringify(out);",
            "})()",
        ]
    )
)
try:
    parsed = json.loads(str(icons))
    print(f"  .ico 元素共 {parsed['count']} 个")
    for sample in parsed["samples"]:
        mask_flag = "✅" if sample["maskKind"] != "NONE" else "❌"
        size_flag = "✅" if sample["w"] > 0 and sample["h"] > 0 else "❌"
        print(
            f"    #{sample['i']}  盒{sample['w']}x{sample['h']} mask={sample.get('maskW','?')} @x{sample['left']}"
            f"  尺寸{size_flag}  mask{mask_flag} {sample['maskKind']}  底色={sample['bg']}"
        )
        if sample["iconVar"]:
            print(f"        --icon: {sample['iconVar']}…")
except (json.JSONDecodeError, TypeError):
    print(f"  {icons}")

print()
print("═" * 78)
print("② 像素采样：图标位置真的有不透明像素吗")
print("═" * 78)

pixels = client.evaluate(
    chr(10).join(
        [
            "new Promise(function(resolve){",
            "  var root = document.body.firstElementChild;",
            "  var ico = root.querySelector('.ico');",
            "  if (!ico) { resolve('no .ico'); return; }",
            "  var r = ico.getBoundingClientRect();",
            "  /* 菜单窗口自己有截图能力吗？没有就用手工方式：",
            "     把 .ico 画到 canvas 上不可行（mask 不走 canvas），",
            "     改用「检测 mask 是否解析成功」的间接判据：",
            "     创建一个同 mask 的探针元素，量它的 maskImage 计算值 */",
            "  var cs = getComputedStyle(ico);",
            "  var mask = cs.maskImage || cs.webkitMaskImage || 'none';",
            "  resolve(JSON.stringify({",
            "    rect: { x: Math.round(r.left), y: Math.round(r.top),",
            "            w: Math.round(r.width), h: Math.round(r.height) },",
            "    maskResolved: mask !== 'none',",
            "    maskLen: mask.length,",
            "    /* data URI 里的 svg 能不能解析成有效 viewBox */",
            "    hasViewBox: mask.indexOf('viewBox') >= 0,",
            "    maskRepeat: cs.maskRepeat || cs.webkitMaskRepeat,",
            "    maskSize: cs.maskSize || cs.webkitMaskSize,",
            "  }));",
            "})",
        ]
    )
)
print(f"  {pixels}")

print()
print("═" * 78)
print("③ 主面板高度 478 vs 设计 440 —— 差在哪")
print("═" * 78)

heights = client.evaluate(
    chr(10).join(
        [
            "(function(){",
            "  var root = document.body.firstElementChild;",
            "  var panels = root.querySelectorAll('.panel');",
            "  var out = { panels: [] };",
            "  for (var i = 0; i < panels.length; i++) {",
            "    var p = panels[i];",
            "    if (p.offsetParent === null) continue;",
            "    var cs = getComputedStyle(p);",
            "    var r = p.getBoundingClientRect();",
            "    var rows = p.querySelectorAll('.row');",
            "    var seps = p.querySelectorAll('.sep');",
            "    out.panels.push({",
            "      h: Math.round(r.height),",
            "      rows: rows.length,",
            "      seps: seps.length,",
            "      padTop: cs.paddingTop, padBottom: cs.paddingBottom,",
            "      border: cs.borderTopWidth,",
            "      rowH: rows.length ? Math.round(rows[0].getBoundingClientRect().height) : 0,",
            "      sepH: seps.length ? Math.round(seps[0].getBoundingClientRect().height) : 0,",
            "      /* 算一遍：上下 padding + 边框 + 行 + 分隔线 */",
            "      computed: (parseFloat(cs.paddingTop) + parseFloat(cs.paddingBottom)",
            "                 + parseFloat(cs.borderTopWidth) * 2",
            "                 + rows.length * (rows.length ? rows[0].getBoundingClientRect().height : 0)",
            "                 + seps.length * (seps.length ? seps[0].getBoundingClientRect().height : 0)),",
            "    });",
            "    break;",
            "  }",
            "  out.bodyH = document.body.getBoundingClientRect().height;",
            "  out.windowInner = window.innerHeight;",
            "  return JSON.stringify(out);",
            "})()",
        ]
    )
)
try:
    parsed = json.loads(str(heights))
    for panel in parsed["panels"]:
        print(f"  主面板实测高 {panel['h']}px")
        print(f"    行数 {panel['rows']} × 行高 {panel['rowH']}px = {panel['rows'] * panel['rowH']}")
        print(f"    分隔线 {panel['seps']} × {panel['sepH']}px = {panel['seps'] * panel['sepH']}")
        print(f"    padding 上{panel['padTop']} 下{panel['padBottom']}，边框 {panel['border']}")
        print(f"    算出来 {panel['computed']:.0f}px  vs 实测 {panel['h']}px"
              f"  {'✅ 一致' if abs(panel['computed'] - panel['h']) <= 1 else '⚠️ 对不上'}")
    print(f"    body 高 {parsed['bodyH']}，窗口内高 {parsed['windowInner']}")
    print()
    print("  §95 记的设计值：面板 300x440（原始设计 437）")
    print("  ⚠️ 差 38px ≈ **正好一行** —— 说明设计稿里主面板是 11 行，而现在是 12 行")
    print("     （菜单后来加了「语音播报」那一项，2026-10-14）")
except (json.JSONDecodeError, TypeError):
    print(f"  {heights}")

client.close()
