"""端到端：发一句话，同时验「语音合成」和「前端显示」（2026-10-15）。

## 背景

用户报两个症状，而证据显示它们的**断点不一样**：

```
① 发不了声音   edge-tts 403（已用升级 7.3.1 修掉，待验）
② 回复不了信息  后端**生成过回复**（16:53:07 还去合成了语音）
                -> 那问题在**前端显示**那一环
```

## 这个脚本验什么

发一句话，然后同时看**三条线**：

```
后端日志   有没有「工具轮」「模型用量」「合成」
前端 DOM   气泡/对话条里有没有出现文字
WebSocket  服务端有没有发 pet.say.* 帧到页面
```

⚠️ 先把 `Network.enable` 打开**再**发消息 —— 上一版我在发完之后才开，
什么都没嗅到（而且那次的 0 帧其实提示了前端压根没发出去）。
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
        self.sock.settimeout(1.5)
        self.n = 0
        self.events: list[dict] = []

    def call(self, method: str, params: dict | None = None) -> dict:
        self.n += 1
        ws_send(self.sock, json.dumps({"id": self.n, "method": method, "params": params or {}}))
        deadline = time.time() + 15
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
                return message.get("result") or {}
            if message.get("method"):
                self.events.append(message)
        return {}

    def evaluate(self, expression: str) -> object:
        result = self.call(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
        )
        return (result.get("result") or {}).get("value")

    def pump(self, seconds: float) -> None:
        deadline = time.time() + seconds
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
            if message.get("method"):
                self.events.append(message)

    def close(self) -> None:
        self.sock.close()


# ── 准备 ──────────────────────────────────────────────────────────────────

offset = LOG.stat().st_size
print(f"  日志起点 {offset}")

targets = cdp_targets()
pet = next((t for t in targets if str(t.get("url", "")).endswith("index.html")), None)
if pet is None:
    print("  ❌ 没有宠物页")
    raise SystemExit(1)

client = Cdp(pet)
print("  ① 先开 Network 域（**必须在发消息之前**）")
client.call("Network.enable")
client.pump(1.0)
print(f"     events={len(client.events)}")

print()
print("  ② 发一句话")
question = "你好呀"
sent = client.evaluate(
    "(function(){var p=window.pet||{};"
    f"if(typeof p.sendUserText==='function'){{p.sendUserText({json.dumps(question, ensure_ascii=False)});"
    "return 'sent';} return 'NO_API';})()"
)
print(f"     sendUserText -> {sent!r}")

print()
print("  ③ 嗅探 35 秒")
client.pump(35)

print()
print("  ═══ 前端收到的帧 ═══")
received: list[str] = []
sent_frames: list[str] = []
for event in client.events:
    method = event.get("method", "")
    params = event.get("params") or {}
    payload = (params.get("response") or {}).get("payloadData")
    if payload is None:
        continue
    if method == "Network.webSocketFrameReceived":
        received.append(str(payload))
    elif method == "Network.webSocketFrameSent":
        sent_frames.append(str(payload))

print(f"    页面发出 {len(sent_frames)} 帧 / 收到 {len(received)} 帧")
if sent_frames:
    print("    --- 页面发出 ---")
    for item in sent_frames[:5]:
        print(f"      {item[:150]}")
if received:
    print("    --- 服务端发来（按类型去重）---")
    kinds: list[str] = []
    for item in received:
        try:
            kind = str(json.loads(item).get("type"))
        except json.JSONDecodeError:
            kind = "(binary)"
        if kind not in kinds:
            kinds.append(kind)
    print(f"      {kinds}")
    print("    --- pet.say 类的原文（前 3 条）---")
    shown = 0
    for item in received:
        if "pet.say" in item:
            print(f"      {item[:200]}")
            shown += 1
            if shown >= 3:
                break
    if not shown:
        print("      ⚠️ **一条 pet.say 都没有**")

print()
print("  ═══ 前端窗口内容 ═══")
print(f"    宠物页 bodyLen={client.evaluate('document.body.innerText.length')}")
client.close()

for target in cdp_targets():
    name = str(target.get("url", "")).rsplit("/", 1)[-1].split("?")[0]
    if name not in ("chat.html", "bubble.html"):
        continue
    other = Cdp(target)
    text = other.evaluate("document.body ? document.body.innerText : '(none)'")
    other.close()
    print(f"    {name}: {str(text)[:240]!r}")

print()
print("  ═══ 后端日志（本次新增）═══")
with LOG.open("r", encoding="utf-8", errors="replace") as handle:
    handle.seek(offset)
    text = handle.read()
interesting = [
    line.strip()
    for line in text.split("\n")
    if any(
        k in line
        for k in ("收到用户消息", "工具轮", "模型用量", "合成", "语音", "pet.say", "ERROR", "WARNING", "记忆")
    )
    and "自主评估" not in line
    and "冷却计算" not in line
]
for line in interesting[-25:]:
    print(f"    {line[:165]}")
