"""完整链路：后端合成 -> 前端真的播了没有（2026-10-15）。

## 为什么还要这一步

上一轮证明**音频能播**（注入测试音成功、AudioContext running）。
但那绕过了整条链路。要验的是：

```
后端合成 -> voice.audio -> 主进程转发 -> pet:voice-audio -> VoiceIO.play() -> 出声
```

⚠️ 关键：**渲染层有没有真的收到 `pet:voice-audio` 并调用 `play()`**。

## 怎么观察

在宠物页里**包一层探针**：替换 `VoiceIO.prototype.play`，
记下每次调用的字节数、格式、以及**成功/失败**。

⚠️ 这不改产品代码 —— 只是运行时在原型上套一层，刷新页面就没了。
"""

from __future__ import annotations

import base64
import hashlib
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

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
LOG = pathlib.Path(r"F:\ai项目\apps\desktop\pet-data\main.log")


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
        self.sock.settimeout(30)
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
        deadline = time.time() + 40
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
                    return f"__EXC__ {str(result['exceptionDetails'])[:400]}"
                return (result.get("result") or {}).get("value")
        return None

    def close(self) -> None:
        self.sock.close()


targets = cdp_targets()
pet = next((t for t in targets if str(t.get("url", "")).endswith("index.html")), None)
if pet is None:
    print("  ❌ 没有宠物页")
    raise SystemExit(1)

client = Cdp(pet)

print("  ═══ ① 在宠物页里挂 play() 探针 ═══")
probe = client.evaluate(
    chr(10).join(
        [
            "(function(){",
            "  if (window.__playProbe) return 'already';",
            "  window.__playProbe = [];",
            "  /* 从 app.js 的模块作用域拿不到 VoiceIO，但可以从原型上钩 */",
            "  var proto = null;",
            "  /* 遍历所有已加载的模块拿不到，所以换个办法：",
            "     钩 AudioContext.createBufferSource，看有没有人调 start() */",
            "  var origCBS = AudioContext.prototype.createBufferSource;",
            "  AudioContext.prototype.createBufferSource = function(){",
            "    var src = origCBS.call(this);",
            "    var origStart = src.start.bind(src);",
            "    src.start = function(){",
            "      window.__playProbe.push({",
            "        at: Date.now(),",
            "        duration: src.buffer ? Math.round(src.buffer.duration * 1000) : 0,",
            "        channels: src.buffer ? src.buffer.numberOfChannels : 0,",
            "      });",
            "      return origStart.apply(null, arguments);",
            "    };",
            "    return src;",
            "  };",
            "  return 'probe installed';",
            "})()",
        ]
    )
)
print(f"    {probe}")

offset = LOG.stat().st_size
print()
print("  ═══ ② 发一句话让宠物说话 ═══")
client.evaluate(
    chr(10).join(
        [
            "(function(){var p=window.pet||{};",
            "if(typeof p.sendUserText==='function'){p.sendUserText('说句短的话');return 'sent';}",
            "return 'NO_API';})()",
        ]
    )
)

print("    等 30 秒…")
time.sleep(30)

print()
print("  ═══ ③ 前端有没有真的 start() 一次音频 ═══")
plays = client.evaluate(
    "JSON.stringify((window.__playProbe || []).map(function(x){"
    "return x.duration + 'ms/' + x.channels + 'ch';}))"
)
print(f"    {plays}")

print()
print("  ═══ ④ 后端这次做了什么 ═══")
with LOG.open("r", encoding="utf-8", errors="replace") as handle:
    handle.seek(offset)
    text = handle.read()
for line in text.split("\n"):
    if any(k in line for k in ("收到用户消息", "发送语音", "合成失败", "合成", "voice.audio")):
        print(f"    {line.strip()[:150]}")

print()
print("  ═══ ⑤ 主进程有没有把音频转给渲染层 ═══")
sp = pathlib.Path(r"F:\ai项目\apps\desktop\dist\startup.log")
st = sp.read_text(encoding="utf-8", errors="replace")
hits = [l.strip() for l in st.split("\n") if "voice-audio" in l or "语音" in l]
for line in hits[-8:]:
    print(f"    {line[:150]}")
print(f"    （共 {len(hits)} 条）")

client.close()
