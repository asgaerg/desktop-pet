"""读前端的实时状态：闸门、推帧数、麦克风。

## 已知（这一步要验证的猜想）

`main.log` 里 19:41:00-03 那次：
```
19:41:00  phase='start' -> 采集闸门：开        <- 主进程链路全通
19:41:03  phase='end'   -> 松开收尾没拿到句子 —— 门态=idle 已攒语音=0ms
                          ^^^^^^^^^^^^^^^^^^ 端点一帧都没收到
```

**闸门开了、但音频帧没到端点。** 所以断点在"前端推帧"这一段。

## 要读的三个值

  · `__captureGate.shouldListen` —— 前端认为该不该推
  · `__voiceStats.framesSent`     —— 累计推了多少帧（>0 说明推过）
  · `__voiceStats.active` / `ctxState` —— 麦克风/AudioContext 活着没

## 判据

  · `shouldListen=false` 而 `framesSent` 不涨 -> 闸门逻辑把它关着（不该）
  · `shouldListen=true`  而 `framesSent` 不涨 -> **`gate` 字段没被 setGate 打开**
  · `framesSent` 在涨     但端点收不到     -> 主进程/sidecar 转发断了
  · `active=false` / `ctxState != running` -> 麦克风或 AudioContext 死了
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
from tool_bootstrap import ROOT, setup  # noqa: E402

setup(verbose=False)

LOG = ROOT / "apps" / "desktop" / "pet-data" / "main.log"


def ws_open(url: str, timeout: int = 40):
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
    return s


def ws_eval(url: str, expr: str, timeout: int = 30):
    s = ws_open(url, timeout)
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
U = win["webSocketDebuggerUrl"]


def snap() -> dict:
    return {
        "gate": ws_eval(U, "JSON.stringify(window.__captureGate || null)"),
        "stats": ws_eval(U, "JSON.stringify((window.__voiceStats && window.__voiceStats()) || null)"),
        "focus": ws_eval(U, "document.hasFocus()"),
        "pttCfg": ws_eval(U, "JSON.stringify(window.__pttConfigGot || null)"),
    }


print("  ═══ ① 按之前 ═══")
a = snap()
for k, v in a.items():
    print(f"     {k:<8} = {v}")

print()
print("  ═══ ② 现在请按住右 Alt 说一句（脚本等 15 秒）═══")
frames0 = sum(
    1
    for l in LOG.read_text(encoding="utf-8", errors="replace").split("\n")
    if "opcode=0x2" in l and "[brain]" not in l
)
time.sleep(15)

print()
print("  ═══ ③ 按之后 ═══")
b = snap()
for k, v in b.items():
    print(f"     {k:<8} = {v}")
frames1 = sum(
    1
    for l in LOG.read_text(encoding="utf-8", errors="replace").split("\n")
    if "opcode=0x2" in l and "[brain]" not in l
)
print(f"     帧日志增量 = {frames1 - frames0}")

print()
print("  ═══ ④ 这一轮的日志 ═══")
lines = LOG.read_text(encoding="utf-8", errors="replace").split("\n")
recent = [
    l.strip()
    for l in lines
    if "[brain]" not in l
    and "帧头" not in l
    and any(k in l for k in ("按住说话", "渲染层发来", "采集闸门", "主动收尾", "开始识别", "语音识别", "收尾没拿到"))
]
for l in recent[-14:]:
    print(f"     {l[:114]}")

print()
print("  ═══ ⑤ 判读 ═══")
try:
    st = json.loads(b["stats"]) if b["stats"] and b["stats"].startswith("{") else {}
    gt = json.loads(b["gate"]) if b["gate"] and b["gate"].startswith("{") else {}
except (ValueError, TypeError):
    st, gt = {}, {}
print(f"     active={st.get('active')}  ctxState={st.get('ctxState')}  framesSent={st.get('framesSent')}")
print(f"     shouldListen={gt.get('shouldListen')}  pttActive={gt.get('pttActive')}")
print()
if not st.get("active"):
    print("     ❌ 麦克风没开（active=false）—— 那前端一帧都推不出去")
elif st.get("framesSent", 0) == 0:
    print("     ❌ **一帧都没推过**（framesSent=0）—— 麦克风开着但闸门没放行")
    print("        看 shouldListen：false 就是闸门逻辑把它关着")
elif frames1 == frames0 and gt.get("pttActive"):
    print("     ❌ 按着键（pttActive=true）但**没有新帧到 sidecar**")
    print("        -> 断点在 主进程转发 / sidecar 收包")
else:
    print("     ✅ 有帧在流动")
