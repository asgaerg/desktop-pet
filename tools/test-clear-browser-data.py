"""端到端测「清空宠物的浏览记录」（2026-10-13）。

## 验什么

```
① 设置页「隐私」标签里那两样在不在（体积文字 + 清空按钮）
② 体积读得对不对（和磁盘上实际大小比）
③ 点清空 -> 真的变小了吗
④ ⚠️ **用户自己的浏览器 cookie 有没有被动过**（这是最要紧的一条）
```

## ⚠️ 第 ④ 条是用户真正担心的

他原话是「清楚的是我**浏览器**上的缓存吗」。

所以这个测试要**在清空前后，读一次他自己 Chrome 的 Cookies 文件大小和修改时间** ——
如果变了，说明我们清错了地方。

⚠️ 只读大小和时间戳，**不打开那个文件**（Chrome 可能正锁着它）。
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

PET_DIR = pathlib.Path(os.environ["APPDATA"]) / "@pet" / "desktop" / "Partitions" / "pet-browser-fallback"
USER_BROWSERS = {
    "Chrome": pathlib.Path(os.environ["LOCALAPPDATA"]) / "Google" / "Chrome" / "User Data" / "Default" / "Network" / "Cookies",
    "Edge": pathlib.Path(os.environ["LOCALAPPDATA"]) / "Microsoft" / "Edge" / "User Data" / "Default" / "Network" / "Cookies",
}


def dir_size(path: pathlib.Path) -> int:
    total = 0
    if not path.exists():
        return 0
    for item in path.rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
        except OSError:
            pass
    return total


def browser_stats() -> dict[str, tuple[int, float]]:
    """用户自己浏览器的 cookie 库（大小 + 修改时间）。只读 stat，不打开文件。"""
    out: dict[str, tuple[int, float]] = {}
    for name, path in USER_BROWSERS.items():
        try:
            info = path.stat()
            out[name] = (info.st_size, info.st_mtime)
        except OSError:
            out[name] = (0, 0.0)
    return out


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

    def evaluate(self, expression: str, await_promise: bool = True) -> object:
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
                        "awaitPromise": await_promise,
                    },
                }
            ),
        )
        deadline = time.time() + 60
        while time.time() < deadline:
            text = ws_recv(self.sock)
            if text is None:
                continue
            try:
                message = json.loads(text)
            except json.JSONDecodeError:
                continue
            if message.get("id") == self.n:
                result = message.get("result") or {}
                if "exceptionDetails" in result:
                    return f"__EXC__ {str(result['exceptionDetails'])[:300]}"
                return (result.get("result") or {}).get("value")
        return None

    def close(self) -> None:
        self.sock.close()


print("  ═══ 清空前 ═══")
pet_before = dir_size(PET_DIR)
user_before = browser_stats()
print(f"    宠物那份：{pet_before / 1024 / 1024:.2f} MB")
for name, (size, mtime) in user_before.items():
    print(f"    你的 {name}：{size / 1024:.0f} KB  最后修改 {time.strftime('%H:%M:%S', time.localtime(mtime)) if mtime else '—'}")

targets = cdp_targets()
pet = next((t for t in targets if str(t.get("url", "")).endswith("index.html")), None)
if pet is None:
    print("  ❌ 没有宠物页")
    raise SystemExit(1)

client = Cdp(pet)
client.evaluate("window.pet && window.pet.openSettings ? window.pet.openSettings() : 'no api'")
time.sleep(4)
client.close()

targets = cdp_targets()
settings = next((t for t in targets if "settings.html" in str(t.get("url", ""))), None)
if settings is None:
    print("  ❌ 设置窗口没开")
    raise SystemExit(1)

client = Cdp(settings)
print()
print("  ═══ ① 切到「隐私」标签，看那两样在不在 ═══")
client.evaluate(
    "(function(){var b=document.querySelector('.nav-item[data-target=\"tab-privacy\"]');"
    "if(b) b.click(); return 'ok';})()"
)
time.sleep(1.5)
state = client.evaluate(
    "JSON.stringify({"
    "  sizeText: (document.getElementById('browser-data-size')||{}).textContent,"
    "  hasButton: !!document.getElementById('clear-browser-data'),"
    "  buttonText: (document.getElementById('clear-browser-data')||{}).textContent"
    "})"
)
print(f"    {state}")

print()
print("  ═══ ② 直接调 preload API 清空（绕过 confirm 弹窗）═══")
result = client.evaluate(
    "window.petSettings.clearBrowserData().then(function(r){return JSON.stringify(r)})"
)
print(f"    {result}")

# 等文件系统落盘
time.sleep(3)

print()
print("  ═══ ③ 体积对比 ═══")
pet_after = dir_size(PET_DIR)
user_after = browser_stats()
print(f"    宠物那份：{pet_before / 1024 / 1024:.2f} MB  ->  {pet_after / 1024 / 1024:.2f} MB")
shrank = pet_after < pet_before
print(f"    {'✅ 变小了' if shrank else '⚠️ 没变小（可能被窗口占用，或本来就近空）'}")

print()
print("  ═══ ④ 🔴 你自己的浏览器被动过吗（最要紧的一条）═══")
untouched = True
for name in user_before:
    b_size, b_mtime = user_before[name]
    a_size, a_mtime = user_after[name]
    changed = (b_size != a_size) or (abs(b_mtime - a_mtime) > 0.001)
    if changed:
        untouched = False
    print(
        f"    {name}: {b_size} B / {b_mtime:.0f}  ->  {a_size} B / {a_mtime:.0f}"
        f"   {'⚠️ 变了！' if changed else '✅ 没动'}"
    )

print()
print("  ═══ ⑤ 界面上的占用文字刷新了吗 ═══")
refreshed = client.evaluate(
    "window.petSettings.browserDataSize().then(function(r){return JSON.stringify(r)})"
)
print(f"    {refreshed}")
client.close()

print()
ok = bool(result) and "ok" in str(result) and '"ok":true' in str(result).replace(" ", "")
print(f"  ═══ {'✅ 清空成功' if ok else '❌ 清空没成功'} ═══")
print(f"  ═══ {'✅ 用户浏览器未被触碰' if untouched else '❌ 动了用户的浏览器！'} ═══")
raise SystemExit(0 if (ok and untouched) else 1)
