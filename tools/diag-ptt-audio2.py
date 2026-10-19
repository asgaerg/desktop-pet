"""看采集统计 + sidecar 侧"为什么没出识别"。

## 已知（上一版诊断）

闸门开着（`pttActive: true`）、音频帧到了 sidecar（6602 条 opcode=0x2）——
所以链路是通的，但没产出识别结果。

## 这一版查什么

  ① `window.__voiceStats` —— 前端采集侧的帧计数（帧率对不对）
  ② sidecar 侧：闸门开着那段时间有没有**端点**在攒句子
     （`pet.voice` 的 endpointer 日志、`静音`、`收尾`）
  ③ **闸门关掉时那半句是不是被丢掉了**

## ③ 为什么要查

`set_capture(False)` 的语义里写着"关闭时**丢弃**半句而不是收尾" ——
`gateway` 先 `force_end()` 再 `set_capture(False)`，按理不会丢。
但如果 `force_end()` 时**句子太短**（低于最小语音长度），它可能直接被丢掉，
表现就是"按了、说了、没反应"。
"""

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
from tool_bootstrap import ROOT, setup  # noqa: E402

setup(verbose=False)

LOG = ROOT / "apps" / "desktop" / "pet-data" / "main.log"


def ws_call(url: str, expr: str, timeout: int = 40):
    rest = url.split("://", 1)[1]
    hp, path = rest.split("/", 1)
    h, p = hp.split(":")
    k = base64.b64encode(os.urandom(16)).decode()
    s = socket.create_connection((h, int(p)), timeout=10)
    s.sendall(
        (
            f"GET /{path} HTTP/1.1\r\nHost: {hp}\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {k}\r\n"
            f"Sec-WebSocket-Version: 13\r\n\r\n"
        ).encode()
    )
    b = b""
    while b"\r\n\r\n" not in b:
        b += s.recv(4096)
    s.settimeout(timeout)
    try:
        pl = json.dumps(
            {"id": 1, "method": "Runtime.evaluate",
             "params": {"expression": expr, "returnByValue": True, "awaitPromise": True}}
        ).encode()
        hd = bytearray([0x81])
        n = len(pl)
        if n < 126:
            hd.append(0x80 | n)
        elif n < 65536:
            hd.append(0x80 | 126)
            hd += struct.pack(">H", n)
        mk = os.urandom(4)
        hd += mk
        s.sendall(bytes(hd) + bytes(b ^ mk[i % 4] for i, b in enumerate(pl)))

        def rd(n):
            o = b""
            while len(o) < n:
                c = s.recv(n - len(o))
                if not c:
                    raise ConnectionError
                o += c
            return o

        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            b0, b1 = rd(2)
            ln = b1 & 127
            masked = b1 & 128
            if ln == 126:
                ln = struct.unpack(">H", rd(2))[0]
            elif ln == 127:
                ln = struct.unpack(">Q", rd(8))[0]
            m = rd(4) if masked else b""
            pay = rd(ln) if ln else b""
            if masked:
                pay = bytes(b ^ m[i % 4] for i, b in enumerate(pay))
            if b0 & 15 != 1:
                continue
            o = json.loads(pay.decode())
            if o.get("id") == 1:
                r = o.get("result", {})
                if "exceptionDetails" in r:
                    return "ERR " + json.dumps(r["exceptionDetails"], ensure_ascii=False)[:200]
                return r.get("result", {}).get("value")
        return "(超时)"
    finally:
        try:
            s.close()
        except OSError:
            pass


pages = json.loads(
    urllib.request.urlopen("http://127.0.0.1:9222/json/list", timeout=5).read().decode()
)
win = next((x for x in pages if "index.html" in (x.get("url") or "")), None)
WS = win["webSocketDebuggerUrl"]

print("  ═══ ① 前端采集统计 ═══")
print(f"     __voiceStats = {ws_call(WS, 'JSON.stringify(window.__voiceStats || null)')}")
print(f"     __captureGate = {ws_call(WS, 'JSON.stringify(window.__captureGate || null)')}")

print()
print("  ═══ ② sidecar 侧：端点/收尾/静音相关日志 ═══")
lines = LOG.read_text(encoding="utf-8", errors="replace").split("\n")
# 只看非帧、非 [brain]
clean = [l.strip() for l in lines if "帧头" not in l and "[brain]" not in l]
kws = ("端点", "收尾", "静音", "丢弃", "句子", "太短", "语音识别（", "采集闸门", "按住说话")
hits = [l for l in clean if any(k in l for k in kws)]
print(f"     共 {len(hits)} 条，最后 20 条：")
for l in hits[-20:]:
    print(f"       {l[:110]}")

print()
print("  ═══ ③ 最近一次「闸门开 -> 闸门关」之间发生了什么 ═══")
opens = [i for i, l in enumerate(lines) if "采集闸门：开" in l and "[brain]" not in l]
if opens:
    o = opens[-1]
    # 找它之后第一条「采集闸门：关」
    closes = [i for i in range(o + 1, len(lines)) if "采集闸门：关" in lines[i] and "[brain]" not in lines[i]]
    c = closes[0] if closes else len(lines) - 1
    print(f"     闸门开 L{o + 1} -> 关 L{c + 1}")
    print(f"     这段时间的**非帧**日志：")
    seg = [l.strip() for l in lines[o : c + 1] if "帧头" not in l and "[brain]" not in l]
    for l in seg[:20]:
        print(f"       {l[:108]}")
    if not seg:
        print("       （没有任何非帧日志 —— 端点没在报什么）")
