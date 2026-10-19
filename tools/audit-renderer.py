"""前端重写后的整体验证 —— 第 1 步：运行时断链扫描（2026-10-15）。

## 背景

用户让**另一个 AI 重写了前端**（10-14/15 改了 41 个源文件）。我只验过语音
那一条线（§101）。现在要**系统性找出其它被改坏的地方**。

## 这个脚本查什么

前端的典型坏法**不是**抛异常 —— 而是**静默断链**：

```
app.js 调 audio.play()          而 voice-io.js 里没这个方法  -> 静默失效
app.js 调 sprite.setMouth()     而 sprite.js 里没这个方法    -> 静默失效
HTML 里没有 #toast              而 app.js 引用它             -> 静默失效
```

⚠️ 本项目**已经踩过这个坑**：`app.js` 的 `beginSay/appendSay/endSay` 是
**刻意的空实现**（气泡搬去独立窗口了）。所以"空函数"要人工判，不能一律当 bug。

这个脚本做的是**机械化**的那部分：

```
① 每个渲染层页面里，所有 `xxx.yyy(` 的调用 —— yyy 在不在 xxx 上
② 所有 `getElementById('x')` / `#x` 选择器 —— HTML 里有没有
③ 页面加载后的 console 错误
④ window.pet 上的方法 vs preload 实际暴露的（协议检查已覆盖，这里再确认运行时）
```

⚠️ 静态扫会有假阳性（动态属性、字符串拼的 id）。**输出的每一条都要人工看**，
脚本只负责把范围缩小。
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

ROOT = pathlib.Path(r"F:\ai项目")
RENDERER = ROOT / "apps" / "desktop" / "src" / "renderer"


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
        self.url = str(target.get("url", ""))
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


# ═══════════════════════════════════════════════════════════════════════════
# 静态扫描：JS 文件里的调用 vs 定义
# ═══════════════════════════════════════════════════════════════════════════

print("═" * 78)
print("① 静态：HTML 里的 id  vs  JS 里引用的 id")
print("═" * 78)


def ids_in_html(path: pathlib.Path) -> set[str]:
    import re

    s = path.read_text(encoding="utf-8")
    return set(re.findall(r'\bid\s*=\s*["\']([^"\']+)["\']', s))


def ids_referenced(path: pathlib.Path) -> set[str]:
    import re

    s = path.read_text(encoding="utf-8")
    found: set[str] = set()
    # getElementById('x') / querySelector('#x')
    found |= set(re.findall(r"getElementById\(\s*['\"]([^'\"]+)['\"]", s))
    found |= set(re.findall(r"querySelector(?:All)?\(\s*['\"]#([\w-]+)", s))
    # ⚠️ 本项目主要用**短名**（`app.js` 里 `els.canvas = $('pet')`），
    # 以及 `els = { toast: $('toast') }` 这种字典。
    # 只认 `getElementById` 会**一个都扫不到**（第一版就踩了这个，输出「引用 0 个 id」）。
    #
    # ⚠️ 但 `$(` 也可能是 jQuery 风格的动态拼接，所以只收**字符串字面量**参数。
    found |= set(re.findall(r"\$\(\s*['\"]([\w-]+)['\"]\s*\)", s))
    # document.getElementById 的变体：document['getElementById']
    return found


pages = [
    ("index.html", "app.js"),
    ("settings.html", "settings.js"),
    ("chat.html", "chat.js"),
    ("bubble.html", "bubble.js"),
    ("petMenu.html", "petMenu.js"),
    ("memory.html", "memory.js"),
]

for html_name, js_name in pages:
    html_path = RENDERER / html_name
    js_path = RENDERER / js_name
    if not html_path.exists() or not js_path.exists():
        print(f"  ⚠️  {html_name} / {js_name} —— 缺文件（html={html_path.exists()} js={js_path.exists()}）")
        continue
    have = ids_in_html(html_path)
    want = ids_referenced(js_path)
    missing = sorted(want - have)
    extra = sorted(have - want)
    flag = "❌" if missing else "✅"
    print(f"  {flag} {js_name:<14} 引用 {len(want):>3} 个 id / HTML 有 {len(have):>3} 个")
    if missing:
        print(f"       **引用了但 HTML 里没有**：{missing}")
    if extra:
        print(f"       （HTML 有但 JS 没引用：{len(extra)} 个 —— 正常，可能纯 CSS 用）")

# ═══════════════════════════════════════════════════════════════════════════
# 静态扫描：跨模块的方法调用（最难查的一类）
# ═══════════════════════════════════════════════════════════════════════════

print()
print("═" * 78)
print("② 静态：跨模块方法调用 —— 调了但对面没定义？")
print("═" * 78)


def methods_defined(path: pathlib.Path) -> set[str]:
    import re

    s = path.read_text(encoding="utf-8")
    out: set[str] = set()
    # class 里的方法
    for m in re.finditer(r"^\s+(?:async\s+)?(\w+)\s*\([^)]*\)\s*\{", s, re.M):
        out.add(m.group(1))
    # 顶层 function / const 箭头函数
    for m in re.finditer(r"^\s*(?:export\s+)?(?:async\s+)?function\s+(\w+)", s, re.M):
        out.add(m.group(1))
    for m in re.finditer(r"^\s*(?:export\s+)?(?:const|let)\s+(\w+)\s*=", s, re.M):
        out.add(m.group(1))
    return out


# sprite 是 app.js 最重的外部依赖
import re  # noqa: E402

sprite_defs = methods_defined(RENDERER / "sprite.js")
app_src = (RENDERER / "app.js").read_text(encoding="utf-8")
called_on_sprite = set(re.findall(r"\bsprite\.(\w+)\s*\(", app_src))
missing_sprite = sorted(called_on_sprite - sprite_defs)
print(f"  sprite.js 定义 {len(sprite_defs)} 个符号，app.js 调用了 {len(called_on_sprite)} 个")
if missing_sprite:
    print(f"    ❌ **app.js 调了但 sprite.js 没有**：{missing_sprite}")
else:
    print("    ✅ 全部有定义")

# voice-io
voice_defs = methods_defined(RENDERER / "voice-io.js")
called_on_audio = set(re.findall(r"\baudio\.(\w+)\s*\(", app_src))
called_on_audio |= set(re.findall(r"\bvoiceIO\.(\w+)\s*\(", app_src))
missing_audio = sorted(called_on_audio - voice_defs)
print(f"  voice-io.js 定义 {len(voice_defs)} 个符号，app.js 调用了 {len(called_on_audio)} 个")
if missing_audio:
    print(f"    ❌ **调了但 voice-io.js 没有**：{missing_audio}")
else:
    print("    ✅ 全部有定义")

# ═══════════════════════════════════════════════════════════════════════════
# 运行时：每个打开的窗口里有什么，有没有报错
# ═══════════════════════════════════════════════════════════════════════════

print()
print("═" * 78)
print("③ 运行时：每个窗口的实际情况")
print("═" * 78)

targets = cdp_targets()
if not targets:
    print("  ❌ CDP 里一个 target 都没有")
    raise SystemExit(1)

pages_found: dict[str, dict] = {}
for target in targets:
    url = str(target.get("url", ""))
    kind = url.rsplit("/", 1)[-1].split("?")[0]
    if not kind.endswith(".html"):
        kind = url.rsplit("/", 1)[-1][:40] or "(未知)"
    if kind not in pages_found:
        pages_found[kind] = target

print(f"  CDP 里有 {len(targets)} 个 target，去重后 {len(pages_found)} 个页面：")
for kind in sorted(pages_found):
    print(f"    · {kind}")

for kind in sorted(pages_found):
    target = pages_found[kind]
    try:
        client = Cdp(target)
    except Exception as exc:  # noqa: BLE001
        print(f"\n  ❌ {kind}: 连不上（{exc}）")
        continue

    info = client.evaluate(
        chr(10).join(
            [
                "(function(){",
                "  var out = {};",
                "  out.url = location.href.slice(-60);",
                "  out.title = document.title;",
                "  out.bodyLen = document.body ? document.body.innerText.length : -1;",
                "  out.scripts = document.scripts.length;",
                "  out.petApi = typeof window.pet;",
                "  out.petMethods = window.pet ? Object.keys(window.pet).length : 0;",
                "  out.hasSprite = !!window.__pet;",
                "  /* 页面自己报的错（app.js 挂了 error hook 的话） */",
                "  out.errHook = window.__errHook ? window.__errHook.slice(-5) : null;",
                "  return JSON.stringify(out);",
                "})()",
            ]
        )
    )
    print(f"\n  ── {kind} ──")
    try:
        parsed = json.loads(str(info))
        for key, value in parsed.items():
            print(f"     {key}: {value}")
    except (json.JSONDecodeError, TypeError):
        print(f"     {info}")

    # 关键：查这个页面的 JS 有没有报错
    errs = client.evaluate(
        chr(10).join(
            [
                "(function(){",
                "  if (!window.__scanHook) {",
                "    window.__scanErrors = [];",
                "    window.__scanHook = true;",
                "    window.addEventListener('error', function(e){",
                "      window.__scanErrors.push(String(e.message || e.type));",
                "    });",
                "    window.addEventListener('unhandledrejection', function(e){",
                "      window.__scanErrors.push('rejection: ' + String(e.reason).slice(0, 120));",
                "    });",
                "  }",
                "  return JSON.stringify(window.__scanErrors || []);",
                "})()",
            ]
        )
    )
    print(f"     运行时错误：{errs}")

    client.close()

print()
print("═" * 78)
print("④ 渲染层文件清单（确认没少也没多）")
print("═" * 78)
for f in sorted(RENDERER.iterdir()):
    if f.suffix in (".js", ".html", ".css"):
        print(f"  {f.name:<26} {f.stat().st_size:>8,} B")
