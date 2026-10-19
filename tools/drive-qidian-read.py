"""通过 CDP 让宠物真去读起点（2026-10-13）。

## 这一步验什么

上一步验了"开关能打开"。这一步验**真的会走浏览器兜底**：

```
① 焦点落在对话条
② 输入一句话（里面带 qidian.com 链接）
③ 回车发送
④ 等它跑完，看日志里有没有「浏览器兜底」
```

## ⚠️ 为什么要用真 UI 而不是直接发 WebSocket

因为**渲染层到 sidecar 那条链路也是要验的一部分** ——
用户在界面上打字是真实路径。直接发 WS 会跳过它。

⚠️ 而且这个脚本**不会**清日志：日志被 Electron 持有，
删不掉（Windows），所以只读**新增部分**。
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
from tool_bootstrap import ROOT, setup  # noqa: E402

setup(verbose=False)

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
LOG = ROOT / "apps" / "desktop" / "pet-data" / "main.log"

QUESTION = "帮我读一下 https://www.qidian.com/book/1037780233/ 这个页面，作者是谁、还在写什么"


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


def evaluate(target: dict, expression: str, wait: float = 2.0) -> object:
    sock = ws_connect(target["webSocketDebuggerUrl"])
    sock.settimeout(30)
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
    deadline = time.time() + 40
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
                raise RuntimeError(str(result["exceptionDetails"])[:400])
            return (result.get("result") or {}).get("value")
    sock.close()
    return None


def new_log_text(offset: int) -> str:
    if not LOG.exists():
        return ""
    try:
        with LOG.open("r", encoding="utf-8", errors="replace") as handle:
            handle.seek(offset)
            return handle.read()
    except OSError:
        return ""


def main() -> int:
    if not LOG.exists():
        print("  ❌ 找不到 main.log")
        return 1
    offset = LOG.stat().st_size
    print(f"  日志起点 {offset} 字节")

    targets = cdp_targets()
    pet = next((t for t in targets if str(t.get("url", "")).endswith("index.html")), None)
    chat = next((t for t in targets if "chat.html" in str(t.get("url", ""))), None)
    print(f"  宠物页 {bool(pet)}  对话条 {bool(chat)}")

    if pet is None:
        print("  ❌ 没有宠物页 target")
        return 1

    print()
    print(f"  提问：{QUESTION}")
    """
    ⚠️ 用**真正的 preload API**（`sendUserText`），走的是和用户打字
    完全相同的入口。

    写成"两个名字都试"是为了**容错**：这个渲染层页面可能是
    `lastReady` 之后重建的，如果我改了 preload 而页面没重载，
    就会看到 `is not a function`。那样至少能读出是哪个名字可用，
    而不是一口咬定"链路断了"。
    """
    call = json.dumps(QUESTION, ensure_ascii=False)
    result = evaluate(
        pet,
        "(function(){var p=window.pet||{};"
        f"if(typeof p.sendUserText==='function') return p.sendUserText({call});"
        f"if(typeof p.sendText==='function') return p.sendText({call});"
        "return 'no send API: ' + Object.keys(p).join(',');})()",
        wait=3.0,
    )
    print(f"    API 返回：{result!r}")

    print()
    print("  等它跑完（最多 150 秒，浏览器兜底本身要 15~25 秒）…")
    deadline = time.time() + 150
    last_len = 0
    while time.time() < deadline:
        time.sleep(5)
        text = new_log_text(offset)
        if len(text) != last_len:
            last_len = len(text)
            for line in text.split("\n"):
                if "浏览器兜底" in line or "web_fetch" in line or "搜索中" in line:
                    print(f"    {line.strip()[:150]}")
        if "浏览器兜底] 成功" in text or "浏览器兜底] 失败" in text:
            break
        if text.count("工具轮 3 完成") or "强制回答" in text:
            time.sleep(8)
            break

    text = new_log_text(offset)
    print()
    print("  ═══ 关键日志 ═══")
    hit = False
    for line in text.split("\n"):
        if "浏览器兜底" in line or "web_fetch" in line or "工具轮" in line or "强制回答" in line:
            print(f"    {line.strip()[:160]}")
            hit = True
    if not hit:
        print("    （这次运行里没有相关行）")

    print()
    if "浏览器兜底] 成功" in text:
        print("  ═══ ✅ 浏览器兜底真的读到了起点 ═══")
        return 0
    if "浏览器兜底] 收到请求" in text:
        print("  ═══ ⚠️ 请求走到了主进程，但没读到（看上面的失败原因）═══")
        return 1
    print("  ═══ ❌ 没走到浏览器兜底 —— 看日志判断卡在哪 ═══")
    return 1


raise SystemExit(main())
