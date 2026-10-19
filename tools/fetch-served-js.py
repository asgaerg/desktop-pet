"""取**服务出去的** `settings.js` 源码，和磁盘上的比对。

## 为什么要这么做

`pet://` 已经给 `settings.js` 加了 `?v=`，served mtime 也和磁盘对得上 ——
但界面行为还是旧的（`renderTts` 好像没跑）。

所以别再推测了：**把浏览器实际拿到的源码抓下来，直接看**。
用 CDP 的 `Debugger.scriptParsed` 事件拿 scriptId，再 `getScriptSource`。
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


def conn(url, timeout=60):
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


def snd(s, o):
    pl = json.dumps(o).encode()
    hd = bytearray([0x81])
    n = len(pl)
    if n < 126:
        hd.append(0x80 | n)
    elif n < 65536:
        hd.append(0x80 | 126)
        hd += struct.pack(">H", n)
    m = os.urandom(4)
    hd += m
    s.sendall(bytes(hd) + bytes(b ^ m[i % 4] for i, b in enumerate(pl)))


def rcv(s):
    def rd(n):
        o = b""
        while len(o) < n:
            c = s.recv(n - len(o))
            if not c:
                raise ConnectionError
            o += c
        return o

    b0, b1 = rd(2)
    ln = b1 & 127
    mk = b1 & 128
    if ln == 126:
        ln = struct.unpack(">H", rd(2))[0]
    elif ln == 127:
        ln = struct.unpack(">Q", rd(8))[0]
    m = rd(4) if mk else b""
    pay = rd(ln) if ln else b""
    if mk:
        pay = bytes(b ^ m[i % 4] for i, b in enumerate(pay))
    return b0 & 15, pay


pages = json.loads(
    urllib.request.urlopen("http://127.0.0.1:9222/json/list", timeout=5).read().decode()
)
st = next(p for p in pages if (p.get("url") or "").startswith("pet://"))
s = conn(st["webSocketDebuggerUrl"], 90)

parsed: dict[str, str] = {}
try:
    snd(s, {"id": 1, "method": "Debugger.enable"})
    snd(s, {"id": 2, "method": "Page.enable"})
    snd(s, {"id": 3, "method": "Page.reload", "params": {"ignoreCache": True}})

    t0 = time.monotonic()
    src_id = None
    while time.monotonic() - t0 < 25:
        try:
            op, pay = rcv(s)
        except (socket.timeout, ConnectionError):
            break
        if op != 1:
            continue
        o = json.loads(pay.decode())
        if o.get("method") == "Debugger.scriptParsed":
            prm = o["params"]
            if "settings.js" in (prm.get("url") or ""):
                parsed[prm["scriptId"]] = prm["url"]
        if o.get("id") == 3:
            continue
    print(f"  抓到 settings.js 的 scriptId: {list(parsed.items())}")

    if parsed:
        src_id = list(parsed)[0]
        snd(s, {"id": 9, "method": "Debugger.getScriptSource", "params": {"scriptId": src_id}})
        t1 = time.monotonic()
        while time.monotonic() - t1 < 20:
            op, pay = rcv(s)
            if op != 1:
                continue
            o = json.loads(pay.decode())
            if o.get("id") == 9:
                src = o.get("result", {}).get("scriptSource", "")
                out = ROOT / "apps" / "desktop" / "pet-data" / "served-settings.js"
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(src, encoding="utf-8")
                print(f"  抓到 {len(src)} 字符，存到 {out}")
                print(f"  含新注释'值不在选项里' : {'值不在选项里时要保留它' in src}")
                print(f"  含 renderTtsState       : {'function renderTtsState' in src}")
                break
finally:
    try:
        s.close()
    except OSError:
        pass
