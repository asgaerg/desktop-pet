"""诊断「按了键但没识别」—— 第 2 版（修掉上一版的变量遮蔽）。

## 上一版为什么崩

```python
pet = next(...)                       # pet 是个 dict（调试目标）
...
js(pet, ...)                          # 而 js() 内部又用 pet 做别的事
```

`js()` 的形参名和目标变量名撞了。这版统一叫 `url` / `target`。

## 查什么

  ① `window.__voiceDiag` —— 采集侧发了多少帧（`app.js` 里的诊断出口）
  ② `window.__captureGate` —— 闸门该不该开
  ③ sidecar 收到二进制音频帧了吗
  ④ **最后一次「闸门开」之后有没有音频帧** —— 这是判据
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
if not win:
    print("  ❌ 找不到宠物窗口")
    raise SystemExit(1)
WS_URL = win["webSocketDebuggerUrl"]

print(f"  窗口: {win['url']}")
print()
print("  ═══ ① 采集侧诊断（app.js 的 window.__voiceDiag）═══")
print(f"     {ws_call(WS_URL, 'JSON.stringify(window.__voiceDiag || \"(没有这个变量)\")')}")
print()
print("  ═══ ② 我加的诊断 ═══")
print(f"     __captureGate  = {ws_call(WS_URL, 'JSON.stringify(window.__captureGate || null)')}")
print(f"     __lastSetVoice = {ws_call(WS_URL, 'JSON.stringify(window.__lastSetVoice || null)')}")
print(f"     __pttConfigGot = {ws_call(WS_URL, 'JSON.stringify(window.__pttConfigGot || null)')}")
print()
print("  ═══ ③ 页面里的窗口变量（找采集状态）═══")
print(
    "     "
    + str(
        ws_call(
            WS_URL,
            "JSON.stringify(Object.keys(window).filter(k => /voice|audio|mic|frame/i.test(k)))",
        )
    )
)

print()
print("  ═══ ④ sidecar 的音频帧（opcode=0x2 是二进制音频）═══")
try:
    lines = LOG.read_text(encoding="utf-8", errors="replace").split("\n")
    frames = [l for l in lines if "opcode=0x2" in l]
    print(f"     二进制帧日志 {len(frames)} 条")
    gates = [i for i, l in enumerate(lines) if "采集闸门：开" in l]
    if gates:
        g = gates[-1]
        after = [l for l in lines[g:] if "opcode=0x2" in l]
        print(f"     最后一次「闸门开」在 L{g + 1}（{lines[g].strip()[:60]}）")
        print(f"     那之后有 {len(after)} 条二进制帧")
        if not after:
            print("     ❌ **闸门开着却零音频帧** —— 麦克风没在采（这就是「按了没识别」的原因）")
        else:
            print(f"     ✅ 有帧：{after[-1].strip()[:80]}")
except OSError as e:
    print(f"     读日志失败：{e}")
