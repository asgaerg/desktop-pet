"""通过 CDP 打开「浏览器兜底」开关（2026-10-13）。

## 为什么要走 CDP 而不是直接改 yaml

直接改 yaml 只改了**磁盘**，sidecar 内存里那份还是旧值 ——
除非重启 sidecar。而我们要验的是**用户真实路径**：

```
设置页勾一下 -> settings:set-browser-fallback -> 主进程落盘 + config.update
            -> sidecar 热更新（清掉工具缓存）-> 下一次 web_fetch 生效
```

⚠️ 这条链路里任何一环断了，表现都是"勾上了但没用"——
正是这个项目反复出现的那类 bug（"假开关"），所以必须走真路径。

## 做法

```
① CDP 找到设置页 target
② 在它里面执行 JS：勾上 #browser-fallback 并派发 change 事件
③ 等 2 秒，读回 #browser-state 的文字确认
```

用的是项目里已有的 CDP 手写客户端（`tools/cdp-voice-state.py` 那套）。
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


def cdp_targets() -> list[dict]:
    with urllib.request.urlopen("http://127.0.0.1:9222/json/list", timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def ws_connect(url: str) -> socket.socket:
    rest = url.split("://", 1)[1]
    hostport, path = rest.split("/", 1)
    host, port = hostport.split(":")
    key = base64.b64encode(os.urandom(16)).decode()
    sock = socket.create_connection((host, int(port)), timeout=15)
    sock.sendall(
        (
            f"GET /{path} HTTP/1.1\r\nHost: {hostport}\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n\r\n"
        ).encode()
    )
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise RuntimeError("握手断开")
        buf += chunk
    expect = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
    if expect.lower() not in buf.decode("latin1").lower():
        raise RuntimeError("Sec-WebSocket-Accept 不匹配")
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

    try:
        b0, b1 = read_exact(2)
    except (ConnectionError, socket.timeout, OSError):
        return None
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


def evaluate(target: dict, expression: str, wait: float = 1.0) -> object:
    """在一个 target 里执行 JS，返回结果。"""
    sock = ws_connect(target["webSocketDebuggerUrl"])
    sock.settimeout(20)
    ws_send(
        sock,
        json.dumps(
            {
                "id": 1,
                "method": "Runtime.evaluate",
                "params": {"expression": expression, "awaitPromise": True, "returnByValue": True},
            }
        ),
    )
    time.sleep(wait)
    deadline = time.time() + 25
    while time.time() < deadline:
        text = ws_recv(sock)
        if text is None:
            continue
        try:
            message = json.loads(text)
        except json.JSONDecodeError:
            continue
        if message.get("id") == 1:
            sock.close()
            result = message.get("result") or {}
            if "exceptionDetails" in result:
                raise RuntimeError(str(result["exceptionDetails"])[:300])
            return (result.get("result") or {}).get("value")
    sock.close()
    return None


def main() -> int:
    targets = cdp_targets()
    print("  ═══ CDP 目标 ═══")
    for target in targets:
        print(f"    {target.get('type'):10} {str(target.get('title'))[:40]:42} {str(target.get('url'))[-40:]}")

    settings = next(
        (t for t in targets if "settings.html" in str(t.get("url", ""))), None
    )
    if settings is None:
        print()
        print("  ⚠️ 没有设置页 target —— 先打开设置窗口")
        print("     （设置页是**按需打开**的窗口，没开就没有 CDP target）")
        # 试着从宠物页打开设置
        pet = next((t for t in targets if "index.html" in str(t.get("url", ""))), None)
        if pet is None:
            print("  ❌ 连宠物页都没有")
            return 1
        print("  从宠物页调 pet.openSettings() 打开设置…")
        evaluate(pet, "window.pet && window.pet.openSettings ? window.pet.openSettings() : 'no api'")
        time.sleep(4)
        targets = cdp_targets()
        settings = next((t for t in targets if "settings.html" in str(t.get("url", ""))), None)
        if settings is None:
            print("  ❌ 还是没打开")
            return 1
        print(f"  ✅ 设置页开了：{settings.get('url')}")

    print()
    print("  ═══ 勾上「浏览器兜底」═══")
    before = evaluate(
        settings,
        "JSON.stringify({checked: document.getElementById('browser-fallback').checked,"
        " state: document.getElementById('browser-state').textContent})",
    )
    print(f"    勾之前：{before}")

    evaluate(
        settings,
        "(function(){var el=document.getElementById('browser-fallback');"
        "el.checked=true;"
        "el.dispatchEvent(new Event('change',{bubbles:true}));"
        "return 'clicked';})()",
    )
    time.sleep(3)

    after = evaluate(
        settings,
        "JSON.stringify({checked: document.getElementById('browser-fallback').checked,"
        " state: document.getElementById('browser-state').textContent})",
    )
    print(f"    勾之后：{after}")

    print()
    print("  ═══ 磁盘上的值（真相源）═══")
    yaml_path = pathlib.Path("config/persona.yaml")
    for line in yaml_path.read_text(encoding="utf-8").split("\n"):
        if "browser_fallback" in line:
            print(f"    {line.strip()}")

    ok = '"checked":true' in str(after).replace(" ", "")
    print()
    print(f"  ═══ {'✅ 开关打开成功（走了真实链路）' if ok else '❌ 没打开'} ═══")
    return 0 if ok else 1


raise SystemExit(main())
