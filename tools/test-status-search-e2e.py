"""实测：宠物联网搜索时，状态条真的会变蓝 + 显示「搜索」吗（2026-10-16）。

## 用户怎么报的

> 「现在联网搜索有问题了，状态栏不显示，你问问宠物其他需要搜索的问题……
>   看看它联网搜索的时候状态栏有没有文字显示联网搜索中」

## 查出来的根因

`pet.tool` 事件**两边都写好了，中间那根线没接**：

```
tools.run_tool()            一直在发 pet.tool                    ✅
gateway._BUS_TO_WIRE        没登记 -> _on_bus_event 直接 return   ❌
index.ts                    message.type === 'pet.tool'           ✅
```

修法是加一行登记。**这个脚本验的就是那一行**：
真的问宠物、真的让它联网、盯着**状态条窗口的 DOM**看。

## ⚠️ 判据装在哪个管子上（这是关键）

不能看"日志里出现了搜索" —— 那只能证明侧车发了事件。
**必须盯 `status.html` 的 `#light.dataset.state` 和 `#text.textContent`**，
因为用户看到的就是那个。本项目踩过三次「观测点装在错的管子上」。

搜索是瞬时的（MCP 大概 1~2 秒），所以用 60ms 高频轮询 + 记录**所有变化**，
而不是等它结束再看一眼（那会漏掉整个状态）。
"""

from __future__ import annotations

import base64
import json
import os
import socket
import struct
import sys
import time
import urllib.request
from pathlib import Path

CDP = "http://127.0.0.1:9222"

# 用户点名要问的那几个（都是"必须联网才知道"的，问不出记忆里的答案）
QUERIES = [
    "今天国际上有什么大事发生吗？",
    "现在有什么二游在测试吗？",
    "年前有什么新游戏会出来吗？",
    "gta6 什么时候出来？",
]


class MiniWs:
    def __init__(self, url: str, timeout: float = 60.0) -> None:
        rest = url.split("://", 1)[1]
        host_port, _, path = rest.partition("/")
        host, _, port = host_port.partition(":")
        self.host, self.port, self.path, self.timeout = host, int(port or 80), "/" + path, timeout
        self.buf = bytearray()
        self._id = 0

    def connect(self) -> None:
        self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self.sock.settimeout(self.timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((
            f"GET {self.path} HTTP/1.1\r\nHost: {self.host}:{self.port}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        ).encode())
        head = b""
        while b"\r\n\r\n" not in head:
            head += self.sock.recv(4096)
        self.buf.extend(head.split(b"\r\n\r\n", 1)[1])

    def send(self, text: str) -> None:
        data = text.encode()
        mask = os.urandom(4)
        header = bytearray([0x81])
        n = len(data)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", n))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", n))
        header.extend(mask)
        self.sock.sendall(bytes(header) + bytes(b ^ mask[i & 3] for i, b in enumerate(data)))

    def _exact(self, n: int) -> bytes:
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("closed")
            self.buf.extend(chunk)
        out = bytes(self.buf[:n])
        del self.buf[:n]
        return out

    def recv(self) -> str:
        while True:
            first, second = self._exact(2)
            opcode = first & 0x0F
            length = second & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._exact(8))[0]
            payload = self._exact(length)
            if opcode == 0x1:
                return payload.decode("utf-8", "replace")

    def evaluate(self, expression: str, timeout: float = 30.0):
        self._id += 1
        call_id = self._id
        self.send(json.dumps({
            "id": call_id, "method": "Runtime.evaluate",
            "params": {"expression": expression, "returnByValue": True, "awaitPromise": True},
        }))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            message = json.loads(self.recv())
            if message.get("id") != call_id:
                continue
            result = message.get("result") or {}
            if result.get("exceptionDetails"):
                details = result["exceptionDetails"]
                raise RuntimeError((details.get("exception") or {}).get("description") or str(details))
            return (result.get("result") or {}).get("value")
        raise TimeoutError("CDP 没回话")


def connect(match: str) -> MiniWs:
    with urllib.request.urlopen(f"{CDP}/json/list", timeout=8) as response:
        items = json.loads(response.read().decode("utf-8"))
    item = next((t for t in items if match in (t.get("url") or "")), None)
    if item is None:
        raise SystemExit(f"找不到 {match}（App 起来了吗？带 --debug-port=9222 了吗？）")
    ws = MiniWs(item["webSocketDebuggerUrl"])
    ws.connect()
    return ws


READ_STATUS = """
(() => {
  const l = document.getElementById('light');
  const t = document.getElementById('text');
  if (!l || !t) return JSON.stringify({state: '(没有灯)', text: '(没有文字)', color: ''});
  return JSON.stringify({
    state: l.dataset.state || '',
    text: t.textContent || '',
    color: getComputedStyle(l).backgroundColor
  });
})()
"""


def main() -> int:
    status_ws = connect("status.html")
    chat_ws = connect("chat.html")
    print("  ✅ 连上状态条 + 对话窗")

    before = json.loads(status_ws.evaluate(READ_STATUS))
    print(f"    起始状态：state={before['state']!r} text={before['text']!r} {before['color']}")

    failures: list[str] = []
    saw_search_state = False
    saw_speaking = False
    rounds_with_search = 0
    not_searched: list[str] = []

    for index, query in enumerate(QUERIES, 1):
        print()
        print("=" * 90)
        print(f"  第 {index} 问：{query}")
        print("=" * 90)

        # 把问题打进输入框并发送（走真实 UI 路径，不是直接调 API）
        sent = chat_ws.evaluate(f"""
        (() => {{
          const input = document.getElementById('input');
          const send = document.getElementById('send');
          if (!input || !send) return 'no-input';
          input.value = {json.dumps(query, ensure_ascii=False)};
          input.dispatchEvent(new Event('input', {{ bubbles: true }}));
          send.click();
          return 'sent';
        }})()
        """)
        if sent != "sent":
            failures.append(f"第 {index} 问没能提交（{sent}）")
            print(f"    ❌ 提交失败：{sent}")
            continue
        print("    📤 已提交，开始盯状态条（60ms 轮询，最多 75 秒）…")

        # 高频轮询，记录**每一次变化**（搜索是瞬时的，只看结果会漏掉）
        transitions: list[tuple[float, str, str, str]] = []
        last = None
        started = time.monotonic()
        while time.monotonic() - started < 75.0:
            try:
                info = json.loads(status_ws.evaluate(READ_STATUS))
            except Exception as exc:  # noqa: BLE001
                print(f"    ⚠️ 读状态条出错：{exc}")
                break
            key = (info["state"], info["text"])
            if key != last:
                elapsed = time.monotonic() - started
                transitions.append((elapsed, info["state"], info["text"], info["color"]))
                mark = ""
                if info["state"] == "searching":
                    mark = "   ← 🔵🔵 搜索！"
                    saw_search_state = True
                elif info["state"] == "speaking":
                    mark = "   ← 🟡 说话"
                    saw_speaking = True
                print(f"      [{elapsed:5.1f}s] {info['state']:<10} {info['text']:<6} "
                      f"{info['color']}{mark}")
                last = key
            # 回到空闲且已经等了足够久 -> 这一轮结束
            if last == ("idle", "空闲") and time.monotonic() - started > 12.0:
                break
            time.sleep(0.06)

        # 看这一轮到底有没有出现过搜索状态
        kinds = [t[1] for t in transitions]
        if "searching" in kinds:
            print("    ✅ 这一轮状态条**出现过**「搜索」（蓝色）")
            rounds_with_search += 1
        else:
            """
            ⚠️ **这一轮没搜，不等于 bug。**

            模型有权不调工具 —— 实测 GTA6 那问它就直接凭记忆答了
            （4 问里 3 问搜了）。**"模型不调工具"无法用来验证功能**，
            这是本项目反复踩到的教训（§108 第七节）。

            所以判据是「**至少有一轮**真的搜过并点亮了搜索状态」，
            而不是"每一轮都必须搜" —— 后者会把模型的自由意志
            误报成产品缺陷，然后逼着人去"修"一个没坏的东西。
            """
            print("    ⚠️ 这一轮模型没调搜索（凭记忆答的）—— 不算失败")
            not_searched.append(query)
        time.sleep(1.5)

    print()
    print("=" * 90)
    print("  汇总")
    print("=" * 90)
    print(f"    {len(QUERIES)} 问里，{rounds_with_search} 问真的联网搜了")
    if not_searched:
        print(f"    没搜的（模型自己的选择，不算 bug）：")
        for item in not_searched:
            print(f"      · {item}")
    print()
    print(f"    出现过「搜索」状态：{'✅ 是' if saw_search_state else '❌ 否'}")
    print(f"    出现过「说话」状态：{'✅ 是' if saw_speaking else '⚠️ 否（可能没开语音）'}")

    # 顺带看日志里侧车发了几个 pet.tool。
    #
    # ⚠️ 主进程的 `log()` 走两个落点（见 `index.ts` 第 186 行那段说明）：
    #    · `app.getPath('userData')/logs/pet.log`  —— 正常日志
    #    · `apps/desktop/dist/startup.log`        —— **保证可写的兜底**
    # 状态条那几条 `[工具]` 打在每个落点上，两个都要找。
    candidates = [
        Path(r"F:\ai项目\apps\desktop\dist\startup.log"),
        Path(os.environ.get("APPDATA", "")) / "desktop-pet" / "logs" / "pet.log",
    ]
    log_path = next((p for p in candidates if p.exists()), candidates[0])
    try:
        with open(log_path, encoding="utf-8", errors="replace") as handle:
            tool_lines = [ln.strip() for ln in handle if "[工具]" in ln][-10:]
        print()
        print(f"    日志里的 [工具] 事件（{log_path.name}，最近 {len(tool_lines)} 条）：")
        for line in tool_lines:
            print(f"      {line[:140]}")
    except OSError as exc:
        print(f"    ⚠️ 读日志失败（{log_path}）：{exc}")

    print()
    """
    ⚠️ 判据：**至少有一轮**真的点亮了搜索状态。

    一轮都没点亮 -> 那根线还是断的（真 bug）
    点亮了       -> 接线对了。剩下的"没搜"是模型的选择，归它自己
    """
    if not saw_search_state:
        print("  ❌ 没有任何一轮点亮「搜索」—— 接线还是断的")
        for item in failures:
            print(f"      - {item}")
        return 1
    print(f"  ✅ 通过 —— 联网搜索时状态条会显示蓝色「搜索」"
          f"（{rounds_with_search}/{len(QUERIES)} 轮触发）")
    return 0


raise SystemExit(main())
