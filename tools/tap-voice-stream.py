"""跟 sidecar 的实时消息流，抓 VAD 能量 / 状态。

## 怎么用

**先按住右 Alt 说话**，然后跑这个脚本 —— 它会：
  ① 连上 sidecar，读 12 秒消息
  ② 报告收到哪些类型的消息
  ③ 特别关注 `voice.state`（可能带 vad 字段）和任何含
     `energy` / `threshold` / `noise_floor` 的负载

## 判据

按住说话时如果 `last_energy` **低于** `threshold`，就是"麦克风太小声 /
增益不够" —— VAD 判成静音，端点不攒句子，`force_end()` 返回 None，
表现就是"按了、说了、没反应"。

## 为什么不用 `voice.stats` 那条

`stats()` 只在 `ready` 消息里发，而那是**前端连上时**推的一次。
我从旁路连上去拿不到（推的是 `pet.emotion` 这类广播）。
`voice.state` 是持续的，所以走它。
"""

import base64
import json
import os
import socket
import struct
import sys
import time

sys.path.insert(0, ".")
from tool_bootstrap import ROOT, setup  # noqa: E402

setup(verbose=False)

PORT = 56606
READ_SECONDS = 14


def ws_connect(port: int, timeout: float = 8.0):
    k = base64.b64encode(os.urandom(16)).decode()
    s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    s.sendall(
        (
            f"GET / HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {k}\r\n"
            f"Sec-WebSocket-Version: 13\r\n\r\n"
        ).encode()
    )
    b = b""
    while b"\r\n\r\n" not in b:
        c = s.recv(4096)
        if not c:
            raise ConnectionError("握手失败")
        b += c
    s.settimeout(timeout)
    return s


def ws_send(s, obj) -> None:
    pl = json.dumps(obj).encode()
    hd = bytearray([0x81])
    n = len(pl)
    if n < 126:
        hd.append(0x80 | n)
    elif n < 65536:
        hd.append(0x80 | 126)
        hd += struct.pack(">H", n)
    else:
        hd.append(0x80 | 127)
        hd += struct.pack(">Q", n)
    mk = os.urandom(4)
    hd += mk
    s.sendall(bytes(hd) + bytes(b ^ mk[i % 4] for i, b in enumerate(pl)))


def ws_recv(s, timeout: float = 3.0):
    s.settimeout(timeout)
    try:
        hdr = s.recv(2)
    except socket.timeout:
        return None
    if len(hdr) < 2:
        return None
    b0, b1 = hdr[0], hdr[1]
    ln = b1 & 127
    masked = b1 & 128
    if ln == 126:
        ln = struct.unpack(">H", s.recv(2))[0]
    elif ln == 127:
        ln = struct.unpack(">Q", s.recv(8))[0]
    m = s.recv(4) if masked else b""
    pay = b""
    while len(pay) < ln:
        c = s.recv(ln - len(pay))
        if not c:
            break
        pay += c
    if masked:
        pay = bytes(b ^ m[i % 4] for i, b in enumerate(pay))
    if (b0 & 15) == 1:
        try:
            return json.loads(pay.decode("utf-8"))
        except json.JSONDecodeError:
            return None
    return None


sock = ws_connect(PORT)
print(f"  已连 127.0.0.1:{PORT}，读 {READ_SECONDS} 秒消息…")
print("  ⚠️ **现在按住右 Alt 说话**（脚本会记录这段时间的消息）")
print()

types: dict[str, int] = {}
interesting: list[dict] = []
t0 = time.monotonic()
while time.monotonic() - t0 < READ_SECONDS:
    m = ws_recv(sock, 2)
    if not m:
        continue
    t = str(m.get("type") or "?")
    types[t] = types.get(t, 0) + 1
    # 任何带音频/能量信息的都记下来
    blob = json.dumps(m, ensure_ascii=False)
    if any(k in blob for k in ("energy", "threshold", "noise_floor", "vad", "dropped")):
        interesting.append(m)
    if t in ("voice.state", "voice.stats", "pet.say.start", "pet.say.end"):
        print(f"    [{time.monotonic() - t0:5.1f}s] {t}: {blob[:150]}")

print()
print("  ═══ 收到的消息类型 ═══")
for t, n in sorted(types.items(), key=lambda kv: -kv[1]):
    print(f"    {t:<24} {n}")

print()
print("  ═══ 含能量/阈值信息的消息 ═══")
if interesting:
    for m in interesting[:6]:
        print(f"    {json.dumps(m, ensure_ascii=False)[:200]}")
else:
    print("    （没有 —— 说明 voice.state 不带 vad 字段）")
    print("    那就只能靠 sidecar 日志：见下面的建议")

sock.close()

print()
print("  ═══ 下一步 ═══")
print("    如果 voice.state 不带能量，最直接的办法是**在 sidecar 侧临时把 VAD 判定记进日志**。")
print("    要我加吗？（加一条：按住说话期间每 200ms 记一次 energy/threshold）")
