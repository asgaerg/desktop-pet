"""确定性地验 cookie 往返：本地 HTTP 服务 + 兜底窗口（2026-10-13）。

## 为什么要这样验

前两版都**不可靠**：

```
第一版  靠模型去调 web_fetch   -> 模型可以选择不调（日志证明它没调）
第二版  想用 CDP 在兜底窗口里种 cookie -> `show: false` 的窗口不在 CDP target 列表里
```

## 这一版的办法

起一个**本地 HTTP 服务**，让兜底窗口去读它：

```
① 本地服务返回一个页面，页面的 JS 里种 3 条 cookie
② 触发兜底去读 http://127.0.0.1:<port>/
   （⚠️ `file://` 不能种 cookie，**必须走 HTTP** —— 这是关键）
③ 读 browserDataSize() -> 应该数出 >= 3 条
④ 清空 -> 回到 0
```

## ⚠️ 怎么触发兜底去读本地服务

`_web_fetch` 只在**普通抓取失败**（WAF / HTTP 202）时才走兜底 ——
而本地服务返回 200，所以不会走。

所以这里**直接调 `fetchPageViaBrowser`** —— 但那在主进程里，
渲染层够不到。

**办法：让本地服务返回 `HTTP 202`**（模拟 WAF）！

```
本地服务对普通抓取返回 202 + probe.js 样式的内容   -> 判为 WAF
对浏览器返回 200 + 种 cookie 的页面               -> 兜底成功
```

判据：看 UA 或 Referer 区分两种请求。简单点：**第一次请求返回 202，
第二次（带 cookie 或不同 UA）返回 200**。

⚠️ 但兜底窗口和普通抓取是**不同的 session/UA** —— 兜底那个 UA 我写死成
真 Chrome，普通抓取用的是 `web.py` 里的 `_UA`（Chrome 126）。
所以可以用 UA 版本号区分：`Chrome/128` 是兜底，其余是普通抓取。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import struct
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, ".")
from tool_bootstrap import setup  # noqa: E402

setup(verbose=False)

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

passed = 0
failed: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    global passed
    if ok:
        passed += 1
        print(f"  OK   {name}")
    else:
        failed.append(f"{name} —— {detail}")
        print(f"  FAIL {name}")
        print(f"       {detail}")


# ── 本地服务：普通抓取给 202（WAF 样式），浏览器给 200 + 种 cookie ──────────


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        ua = self.headers.get("User-Agent", "")
        # 兜底窗口的 UA 是写死的 Chrome/128；普通抓取是 Chrome/126
        is_browser = "Chrome/128" in ua
        print(f"    [服务] {self.path}")
        print(f"           UA={ua!r}")
        print(f"           is_browser={is_browser}  -> {200 if is_browser else 202}")
        if is_browser:
            body = (
                "<html><head><title>探针页</title></head><body>"
                "<h1>cookie 探针</h1><p>" + ("填充正文。" * 60) + "</p>"
                "<script>"
                "document.cookie='probe_a=1; path=/';"
                "document.cookie='probe_b=2; path=/';"
                "document.cookie='probe_c=3; path=/';"
                "</script></body></html>"
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            # 模拟 WAF：202 + 一个 probe.js 样式的脚本
            body = (
                '<!DOCTYPE html><html><head><meta charset="UTF-8">'
                '<script>var buid="ffff"</script>'
                '<script src="/probe.js?v=x"></script></head><body></body></html>'
            ).encode("utf-8")
            self.send_response(202)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Set-Cookie", "x-waf-captcha-referer=; Path=/; Max-Age=60")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def log_message(self, *args) -> None:  # noqa: ANN002
        """静音默认日志（我们自己打）。"""


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
port = server.server_address[1]
threading.Thread(target=server.serve_forever, daemon=True).start()
print(f"  本地服务：http://127.0.0.1:{port}/")


# ── CDP ────────────────────────────────────────────────────────────────────


def cdp_targets() -> list[dict]:
    with urllib.request.urlopen("http://127.0.0.1:9222/json/list", timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def ws_connect(url: str) -> socket.socket:
    rest = url.split("://", 1)[1]
    hostport, path = rest.split("/", 1)
    host, port_text = hostport.split(":")
    key = base64.b64encode(os.urandom(16)).decode()
    sock = socket.create_connection((host, int(port_text)), timeout=20)
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
        self.sock.settimeout(120)
        self.n = 0

    def evaluate(self, expression: str) -> object:
        self.n += 1
        ws_send(
            self.sock,
            json.dumps(
                {
                    "id": self.n,
                    "method": "Runtime.evaluate",
                    "params": {"expression": expression, "returnByValue": True, "awaitPromise": True},
                }
            ),
        )
        deadline = time.time() + 150
        while time.time() < deadline:
            text = ws_recv(self.sock)
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


def settings_client() -> Cdp:
    target = next(
        (t for t in cdp_targets() if "settings.html" in str(t.get("url", ""))), None
    )
    if target is None:
        pet = next(
            (t for t in cdp_targets() if str(t.get("url", "")).endswith("index.html")), None
        )
        client = Cdp(pet)
        client.evaluate("window.pet.openSettings()")
        time.sleep(4)
        client.close()
        target = next(
            (t for t in cdp_targets() if "settings.html" in str(t.get("url", ""))), None
        )
    if target is None:
        raise SystemExit("  ❌ 设置窗口开不了")
    return Cdp(target)


client = settings_client()

print()
print("  ═══ ① 从干净状态开始 ═══")
client.evaluate("window.petSettings.clearBrowserData()")
time.sleep(2)
info = json.loads(
    str(
        client.evaluate(
            "window.petSettings.browserDataSize().then(function(r){return JSON.stringify(r)})"
        )
    )
)
print(f"    cookie={info.get('cookies')}")
check("清空后 cookie = 0", int(info.get("cookies", -1)) == 0, f"实际 {info.get('cookies')}")

print()
print("  ═══ ② 让兜底窗口去读本地探针页 ═══")
# ⚠️ 用 browserProbe 而不是"让模型去 web_fetch" —— 后者不可靠（模型可以选择不调）
probe = client.evaluate(
    "window.petSettings.browserProbe(" + json.dumps(f"http://127.0.0.1:{port}/") + ")"
    ".then(function(r){return JSON.stringify(r)})"
)
print(f"    probe = {probe}")
time.sleep(3)
info = json.loads(
    str(
        client.evaluate(
            "window.petSettings.browserDataSize().then(function(r){return JSON.stringify(r)})"
        )
    )
)
print(f"    cookie={info.get('cookies')}")
check(
    "**兜底窗口种下的 cookie 被数出来了**",
    int(info.get("cookies", 0)) >= 3,
    f"实际 {info.get('cookies')} —— 期望 >= 3（探针页种了 3 条）",
)

print()
print("  ═══ ③ 清空 -> 应回 0 ═══")
client.evaluate("window.petSettings.clearBrowserData()")
time.sleep(2)
info = json.loads(
    str(
        client.evaluate(
            "window.petSettings.browserDataSize().then(function(r){return JSON.stringify(r)})"
        )
    )
)
print(f"    cookie={info.get('cookies')}")
check("清空后 cookie = 0", int(info.get("cookies", -1)) == 0, f"实际 {info.get('cookies')}")

client.close()
server.shutdown()

print()
print(f"  ═══ 结果：{passed}/{passed + len(failed)} 通过 ═══")
if failed:
    for item in failed:
        print(f"    ❌ {item}")
    raise SystemExit(1)
print("  ✅ 全部通过")
