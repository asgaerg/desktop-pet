"""到底哪一环断了：音频有没有真的播出去（2026-10-15）。

## 已知事实

```
后端      ✅ 合成成功（68256 / 96912 字节 mp3）
主进程    ✅ 收到 voice.audio，转发 pet:voice-audio 给渲染层
渲染层    ?  差这一步
```

`voice-io.js` 的 `play()` 代码**看起来是对的**（AudioContext + decodeAudioData + start）。

## 🔴 但有一个大嫌疑：**AudioContext 被浏览器策略挂起**

```javascript
if (!this.context) this.context = new AudioContext();
if (context.state === 'suspended') await context.resume();
```

⚠️ Chromium 的自动播放策略：**没有用户手势的 AudioContext 是 `suspended` 的**，
而 `resume()` **在无手势时会被拒绝**（不报错、静默保持 suspended）。

而这个应用的特殊之处：**用户从来没有在页面上点过任何东西** ——
消息是通过 `sendUserText` 发的，宠物页是透明的、点不到的空窗口。

所以 `resume()` 很可能**静默失败** → `source.start()` 无声。

## 这个脚本怎么验

```
① 读当前 AudioContext 的状态
② 直接往渲染层注一段音频，看能不能播（绕过整条链路）
③ 读控制台错误
```
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import struct
import sys
import time
import urllib.request

sys.path.insert(0, ".")
from tool_bootstrap import setup  # noqa: E402

setup(verbose=False)

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


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
                        "userGesture": True,
                    },
                }
            ),
        )
        deadline = time.time() + 30
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

print("  ═══ ① 页面上有没有 AudioContext，状态如何 ═══")
state = client.evaluate(
    "(function(){"
    "  var out = { hasAudioCtx: typeof AudioContext, "
    "    instances: (window.__pet && window.__pet.audio) ? 'voiceIO 存在' : '没有 audio 实例' };"
    "  try {"
    "    var c = new AudioContext();"
    "    out.newContextState = c.state;"
    "    out.sampleRate = c.sampleRate;"
    "    out.destinationChannels = c.destination.channelCount;"
    "    c.close();"
    "  } catch (e) { out.error = String(e); }"
    "  return JSON.stringify(out);"
    "})()"
)
print(f"    {state}")

print()
print("  ═══ ② app.js 里的 VoiceIO 实例（audio 变量）能不能读到 ═══")
instance = client.evaluate(
    "(function(){"
    "  /* audio 在模块作用域里，外面拿不到 —— 但 __pet 暴露了 audioStats */"
    "  var p = window.__pet || {};"
    "  return JSON.stringify({ hasPet: !!window.__pet, audioStats: p.audioStats || null });"
    "})()"
)
print(f"    {instance}")

print()
print("  ═══ ③ 直接注入一段音频，看能不能播（绕过整条链路）═══")
play_test = client.evaluate(
    chr(10).join(
        [
            "(function(){",
            "  return new Promise(function(resolve){",
            "    try {",
            "      var ctx = new AudioContext();",
            "      var stateBefore = ctx.state;",
            "      var rate = ctx.sampleRate;",
            "      var buf = ctx.createBuffer(1, Math.floor(rate * 0.4), rate);",
            "      var ch = buf.getChannelData(0);",
            "      for (var i = 0; i < ch.length; i++) {",
            "        ch[i] = Math.sin(2 * Math.PI * 440 * i / rate) * 0.25;",
            "      }",
            "      var src = ctx.createBufferSource();",
            "      src.buffer = buf;",
            "      src.connect(ctx.destination);",
            "      src.start();",
            "      src.onended = function(){",
            "        resolve(JSON.stringify({stateBefore: stateBefore, stateAfter: ctx.state, played: true}));",
            "      };",
            "      setTimeout(function(){",
            "        resolve(JSON.stringify({stateBefore: stateBefore, stateAfter: ctx.state, played: true, timedOut: true}));",
            "      }, 1500);",
            "    } catch (e) { resolve(String(e)); }",
            "  });",
            "})()",
        ]
    )
)
print(f"    {play_test}")

print()
print("  ═══ ④ 有没有控制台错误 ═══")
errors = client.evaluate(
    "(function(){"
    "  if (!window.__errHook) {"
    "    window.__errHook = [];"
    "    window.addEventListener('error', function(e){ window.__errHook.push(String(e.message||e.type)); });"
    "    window.addEventListener('unhandledrejection', function(e){ window.__errHook.push('rej: ' + String(e.reason).slice(0,150)); });"
    "  }"
    "  return JSON.stringify(window.__errHook.slice(-10));"
    "})()"
)
print(f"    {errors}")

print()
print("  ═══ ⑤ 系统音频设备（Windows）═══")
client.close()

import subprocess  # noqa: E402

result = subprocess.run(
    [
        "powershell", "-NoProfile", "-Command",
        "Get-CimInstance Win32_SoundDevice | Select-Object Name,Status | Format-Table -AutoSize | Out-String -Width 120",
    ],
    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
)
print("    " + (result.stdout or result.stderr or "(没有输出)").strip().replace("\n", "\n    "))
