"""真链路：重启应用 -> 打开浏览器兜底 -> 真去读起点（2026-10-13）。

## 为什么必须跑这一条

`test-browser-bridge.py` 验的是 Python 那一半（消息构造 / 等回执 / 超时）。
真链路还有三段它碰不到：

```
① Electron 主进程收到 `web.browser.fetch` 了吗
② 隐藏窗口真的建起来、真的过了 WAF 吗
③ 回执真的回到了 Python 吗（requestId 配对）
```

⚠️ 这三段任何一段断了，表现都是**同一个**："读不了"——
所以必须端到端跑一次，否则分不清是哪一段。

## 做法

```
① 把 config 里的 browser_fallback 临时设成 true（finally 里还原）
② 杀掉旧 Electron、清日志
③ 起 Electron（--dev，会拉起 sidecar）
④ 手写 WebSocket 连上 sidecar（不引 websockets 依赖，照 cdp-voice-state.py）
⑤ 发一条 user.text 让它去读起点书页
⑥ 看日志里有没有「浏览器兜底」成功
```

⚠️ **会真开一个隐藏浏览器窗口、真访问起点** —— 这正是这个测试要验的。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import pathlib
import re
import socket
import struct
import subprocess
import sys
import time

sys.path.insert(0, ".")
from tool_bootstrap import ROOT, setup  # noqa: E402

setup(verbose=False)

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

YAML = ROOT / "config" / "persona.yaml"
LOG = ROOT / "apps" / "desktop" / "pet-data" / "main.log"
PORT_FILE = ROOT / "apps" / "desktop" / "pet-data" / "brain-port.json"
ELECTRON = (
    ROOT / "node_modules" / ".pnpm" / "electron@32.3.3" / "node_modules"
    / "electron" / "dist" / "electron.exe"
)


# ── 手写 WebSocket（照 tools/cdp-voice-state.py，不引依赖）────────────────


def ws_connect(host: str, port: int, path: str = "/") -> socket.socket:
    key = base64.b64encode(os.urandom(16)).decode()
    sock = socket.create_connection((host, port), timeout=15)
    sock.sendall(
        (
            f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\n"
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
    opcode = b0 & 0x0F
    if opcode == 0x8:  # close
        return None
    return payload.decode("utf-8", "replace")


# ── 主流程 ─────────────────────────────────────────────────────────────


def set_flag(value: bool) -> None:
    text = YAML.read_text(encoding="utf-8")
    updated = re.sub(
        r"(\n  browser_fallback: )(true|false)",
        lambda m: m.group(1) + ("true" if value else "false"),
        text,
        count=1,
    )
    YAML.write_text(updated, encoding="utf-8")


def kill_electron() -> None:
    """只杀 electron。**绝不 taskkill python** —— 那会杀掉本脚本自己。"""
    subprocess.run(
        ["taskkill", "/F", "/IM", "electron.exe"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )


def read_port() -> int:
    raw = PORT_FILE.read_text(encoding="utf-8").strip()
    try:
        return int(json.loads(raw).get("port"))
    except (json.JSONDecodeError, AttributeError, TypeError):
        return int(raw)


def main() -> int:
    backup = YAML.read_text(encoding="utf-8")
    proc = None
    try:
        print("  ① 打开 browser_fallback")
        set_flag(True)
        found = re.search(r"\n  browser_fallback: (\w+)", YAML.read_text(encoding="utf-8"))
        print(f"     browser_fallback: {found.group(1) if found else '?'}")

        print("  ② 杀掉旧 Electron")
        kill_electron()
        time.sleep(2)
        """
        ⚠️ **不删 main.log，只记下当前长度**。

        日志文件被 Electron 持有（Windows 上删不掉 —— 会报
        `PermissionError: [WinError 32]`）。而我们要看的只是**这次运行**
        产生的行，所以从旧长度往后读就够了。

        （这也顺带避免了"删日志"这个有副作用的动作 ——
        万一排查历史问题时那份日志还有用。）
        """
        log_offset = LOG.stat().st_size if LOG.exists() else 0
        print(f"     日志起点 {log_offset} 字节")

        print("  ③ 起 Electron（--dev）")
        env = dict(os.environ)
        env.pop("ELECTRON_RUN_AS_NODE", None)
        proc = subprocess.Popen(
            [str(ELECTRON), ".", "--dev", "--no-sandbox"],
            cwd=str(ROOT / "apps" / "desktop"),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
        )
        print(f"     PID {proc.pid}")

        print("  ④ 等 sidecar 就绪（最多 45 秒）")
        deadline = time.time() + 45
        while time.time() < deadline and not PORT_FILE.exists():
            time.sleep(1)
        if not PORT_FILE.exists():
            print("  ❌ sidecar 没起来")
            return 1
        time.sleep(3)
        port = read_port()
        print(f"     sidecar 端口 {port}")

        print("  ⑤ 连上 sidecar 并驱动对话")
        sock = ws_connect("127.0.0.1", port, "/")
        sock.settimeout(5)
        ws_send(sock, json.dumps({"type": "hello", "client": "e2e-browser-test"}))

        received: list[dict] = []
        deadline = time.time() + 15
        while time.time() < deadline:
            text = ws_recv(sock)
            if text is None:
                continue
            try:
                message = json.loads(text)
            except json.JSONDecodeError:
                continue
            received.append(message)
            if message.get("type") == "ready":
                break
        print(f"     收到 {len(received)} 条（有 ready：{any(m.get('type') == 'ready' for m in received)}）")

        ws_send(
            sock,
            json.dumps(
                {
                    "type": "user.text",
                    "text": (
                        "帮我读一下这个页面 https://www.qidian.com/book/1037780233/ ，"
                        "作者是谁、还在写什么"
                    ),
                }
            ),
        )

        print("  ⑥ 等它跑完（最多 150 秒）")
        deadline = time.time() + 150
        while time.time() < deadline:
            text = ws_recv(sock)
            if text is None:
                continue
            try:
                message = json.loads(text)
            except json.JSONDecodeError:
                continue
            received.append(message)
            kind = message.get("type")
            if kind == "pet.tool":
                print(f"     [工具] {message.get('phase')} {message.get('tool')} {message.get('detail')}")
            if kind in ("pet.say.end", "system.error"):
                break
        sock.close()

        said = "".join(
            str(m.get("text") or "") for m in received if m.get("type") == "pet.say.delta"
        )
        print()
        print("  ═══ 回复 ═══")
        print(f"    {said[:400]}")
        print()
        print("  ═══ 日志里的关键行（只读这次新增的部分）═══")
        text = ""
        if LOG.exists():
            with LOG.open("r", encoding="utf-8", errors="replace") as handle:
                handle.seek(log_offset)
                text = handle.read()
        for line in text.split("\n"):
            if "浏览器兜底" in line or "web.browser" in line or "web_fetch" in line:
                print(f"    {line.strip()[:150]}")

        ok = "浏览器兜底] 成功" in text or "浏览器兜底] 收到请求" in text
        print()
        print(f"  ═══ {'✅ 浏览器兜底被走到了' if ok else '❌ 日志里没看到浏览器兜底'} ═══")
        return 0 if ok else 1
    finally:
        YAML.write_text(backup, encoding="utf-8")
        print("  ⑦ 已还原 config/persona.yaml")


raise SystemExit(main())
