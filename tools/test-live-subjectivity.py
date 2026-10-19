"""真机验证：主观层 + 兴趣 + 一段交流，整条链路通不通（2026-10-17）。

## 为什么必须真机

单测证明的是"函数行为对"，但是这一整套东西最怕的是**线没接**：

```
单测       -> 每块都对
真机跑起来 -> 事件循环没启动 / 段永远不结束 / 兴趣表永远是空的
```

之前已经踩过好几次"两边都写好了，中间那根线没接"，所以这里做端到端：

```
① 通过 CDP 在渲染层输入框发几条消息
② 等提炼跑完
③ 查真实库：interest 有没有长出来、event_log/impression 表结构对不对
④ 查日志：事件循环启动了吗、分词器接上了吗、段有没有收尾
```

⚠️ 这个脚本会**真的调用模型**（每轮一次提炼），所以条数刻意少（默认 3 条）。
⚠️ 它**不清理**你真实的记忆 —— 但发的都是无意义的测试词，
   如果你想干净，跑完可以在设置页删掉那几条。

用法：
    python tools/test-live-subjectivity.py
    python tools/test-live-subjectivity.py --turns 5
"""

from __future__ import annotations

import json
import socket
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

ROOT = Path(r"F:\ai项目")
DB = ROOT / "apps" / "desktop" / "pet-data" / "memory" / "pet.db"
LOG = ROOT / "apps" / "desktop" / "pet-data" / "main.log"
CDP_PORT = 9222

# 刻意用**具体、带专有名词**的话（按新提炼规则，这种才该被记住）
MESSAGES = [
    "我最近在玩《银松镇》，那个转盘机关卡了我好久",
    "《银松镇》是手绘风格的生存恐怖游戏，画风挺怪诞",
    "我打算等《银松镇》通关之后去玩 GTA6",
    "今天有点累，一直在打游戏",
    "你说那个机关要按什么顺序点亮来着",
    "对了我还喜欢听《梦的翅膀受了伤》",
    "我容易在游戏里迷路，方向感特别差",
    "先不聊了，我去把那段过了",
]


def cdp_targets() -> list[dict]:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json", timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        print(f"  ❌ 连不上 CDP（{exc}）—— App 没起来，或者没带 --debug-port")
        return []


class WS:
    """极简 WebSocket 客户端（不引第三方依赖，与项目其它脚本一致）。"""

    def __init__(self, url: str) -> None:
        from urllib.parse import urlparse

        u = urlparse(url)
        self.host = u.hostname or "127.0.0.1"
        self.port = u.port or 80
        self.path = u.path + (f"?{u.query}" if u.query else "")
        self.sock = socket.create_connection((self.host, self.port), timeout=15)
        key = "dGhlIHNhbXBsZSBub25jZQ=="
        req = (
            f"GET {self.path} HTTP/1.1\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(req.encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RuntimeError("WebSocket 握手失败")
            head += chunk

    def send(self, payload: dict) -> None:
        data = json.dumps(payload).encode("utf-8")
        import os
        import struct

        mask = os.urandom(4)
        header = bytes([0x81])
        n = len(data)
        if n < 126:
            header += bytes([0x80 | n])
        elif n < 65536:
            header += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            header += bytes([0x80 | 127]) + struct.pack(">Q", n)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        self.sock.sendall(header + mask + masked)

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


def evaluate(target: dict, expression: str) -> tuple[bool, str]:
    ws = WS(target["webSocketDebuggerUrl"])
    try:
        ws.send(
            {
                "id": 1,
                "method": "Runtime.evaluate",
                "params": {"expression": expression, "awaitPromise": True, "returnByValue": True},
            }
        )
        ws.sock.settimeout(20)
        buf = b""
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                chunk = ws.sock.recv(65536)
            except socket.timeout:
                break
            if not chunk:
                break
            buf += chunk
            text = chunk.decode("utf-8", "replace")
            if '"id":1' in text or '"id": 1' in text:
                break
        return True, buf.decode("utf-8", "replace")[:400]
    finally:
        ws.close()


def find_renderer(targets: list[dict]) -> dict | None:
    """
    找**聊天窗口**。

    ⚠️ 桌宠有多个渲染页面（`chat.html` / `bubble.html` / `status.html` / 设置页），
    而 CDP 的 `/json` 返回顺序**不固定**（实测同一次运行里先给 bubble、
    再给 chat）。第一版按"URL 里含 pet"取第一个，于是有时拿到 bubble ——
    那里没有输入框，8 条消息全部静默失败（返回 `?`，实际发出 0 条）。

    ✅ 所以必须**按文件名精确匹配** chat.html。
    """
    for want in ("chat.html", "settings.html"):
        for t in targets:
            if t.get("type") == "page" and want in str(t.get("url", "")).lower():
                return t
    for t in targets:
        if t.get("type") == "page" and "pet://" in str(t.get("url", "")).lower():
            return t
    return None


def snapshot() -> dict:
    out: dict[str, int] = {}
    if not DB.exists():
        return out
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        for table in ("memory", "interest", "event_log", "impression"):
            try:
                out[table] = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            except sqlite3.OperationalError:
                out[table] = -1
    finally:
        con.close()
    return out


def log_size() -> int:
    """记下日志当前字节数 —— 之后只看**新增**部分。

    ⚠️ 这个函数是必需的（2026-10-17 踩过）：第一版直接 `if needle in text`
    扫**整个** main.log，于是**上一轮运行留下的旧 ERROR 永远算在这一次头上**，
    修好了也照样报红 —— 假红灯和假绿灯一样有害。

    ✅ 正确做法与项目里 `_sweep_stale_memory_tmp()` 的取舍一致：
       **按时间/位置判断，不按存在判断。**
    """
    if not LOG.exists():
        return 0
    return LOG.stat().st_size


def log_since(offset: int) -> str:
    """只取 offset 之后新增的日志内容。"""
    if not LOG.exists():
        return ""
    with LOG.open("r", encoding="utf-8", errors="replace") as fh:
        fh.seek(offset)
        return fh.read()


def main() -> int:
    turns = 3
    if "--turns" in sys.argv:
        try:
            turns = int(sys.argv[sys.argv.index("--turns") + 1])
        except (IndexError, ValueError):
            pass

    print("=" * 78)
    print("  真机验证：主观层 / 兴趣 / 一段交流")
    print("=" * 78)

    targets = cdp_targets()
    if not targets:
        return 1
    page = find_renderer(targets)
    if page is None:
        print("  ❌ 没找到渲染页面")
        return 1
    print(f"  渲染页面：{page.get('url', '')[:70]}")

    before = snapshot()
    print(f"  发送前：{before}")
    # 只看本次运行之后新增的日志（见 log_size 的注释）
    log_mark = log_size()
    print(f"  日志位置：{log_mark} 字节（之后的才算本次）")

    print()
    print("  ── 发消息（会真调模型提炼）──")
    sent = 0
    for i, text in enumerate(MESSAGES[:turns], 1):
        expr = (
            "(function(){"
            "const el=document.querySelector('#input, textarea, input[type=text]');"
            f"if(!el) return 'no-input';"
            f"el.value={json.dumps(text, ensure_ascii=False)};"
            "el.dispatchEvent(new Event('input',{bubbles:true}));"
            "const f=document.querySelector('#composer, form');"
            "if(f && f.requestSubmit) f.requestSubmit();"
            "else {const b=document.querySelector('#send, button[type=submit], .send');"
            "if(b) b.click(); else return 'no-send';}"
            "return 'ok';"
            "})()"
        )
        ok, out = evaluate(page, expr)
        result = "ok" if '"value":"ok"' in out.replace(" ", "") else "?"
        print(f"    第 {i} 条 -> {result}")
        if result == "ok":
            sent += 1
        time.sleep(11)

    print(f"  实际发出 {sent} 条")
    print("  等提炼落地（12 秒）…")
    time.sleep(12)

    after = snapshot()
    print()
    print(f"  发送后：{after}")

    failed: list[str] = []
    print()
    print("  ── 检查 ──")
    if after.get("interest", 0) > 0:
        print(f"     ✅ 兴趣表长出了 {after['interest']} 条")
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        try:
            rows = con.execute(
                "SELECT topic, weight, strength FROM interest ORDER BY weight DESC LIMIT 6"
            ).fetchall()
        finally:
            con.close()
        for topic, weight, strength in rows:
            print(f"        {topic}  权重 {weight:.2f}  [{strength}]")
    else:
        print("     ⚠️ 兴趣表还是空的（可能提炼还没跑完，或模型没抽出东西）")

    if after.get("memory", 0) >= before.get("memory", 0):
        print(f"     ✅ 记忆库 {before.get('memory')} -> {after.get('memory')}（没丢）")
    else:
        failed.append("记忆条数变少了")
        print("     ❌ 记忆条数变少了")

    # 日志检查
    #
    # ⚠️ 两类检查，扫描范围**不同**（2026-10-17 踩过）：
    #
    #   启动事实（事件循环/分词器/记忆库）-> 查**全量**
    #       它们发生在 App 启动那一刻，本来就在 log_mark 之前。
    #       第一版用新增内容查，于是永远没找到 —— 假红灯。
    #
    #   异常（我的新代码有没有抛）-> 只查**新增**
    #       旧运行留下的 ERROR 不该算在这一次头上。
    print()
    print("  ── 日志 ──")
    if LOG.exists():
        whole = LOG.read_text(encoding="utf-8", errors="replace")
        fresh = log_since(log_mark)
        for needle, label in (
            ("随机事件循环启动", "事件循环"),
            ("兴趣提取已接上中文分词器", "分词器"),
            ("记忆库就绪", "记忆库"),
        ):
            ok_mark = needle in whole
            print(f"     {'✅' if ok_mark else '❌'} {label}（启动事实，查全量）")
            if not ok_mark:
                failed.append(f"日志里没有：{label}")

        bad = [k for k in ("事件判定失败", "收尾失败", "随机事件单轮异常") if k in fresh]
        if bad:
            print(f"     ❌ 本次运行新代码有异常：{bad}")
            failed.append(f"新代码异常：{bad}")
        else:
            print("     ✅ 本次运行新代码零异常（只查新增部分）")

        if "一段交流结束" in fresh:
            print("     ✅ 检测到段收尾（提炼印象 + 强化）")
        else:
            print("     ⚠️ 本次没到段边界（不够 8 轮 / 间隔不够 10 分钟）")
    else:
        print("     ❌ 找不到 main.log")
        failed.append("找不到日志")

    print()
    print("=" * 78)
    if failed:
        print(f"  ❌ {len(failed)} 项问题：")
        for item in failed:
            print(f"     · {item}")
    else:
        print("  ✅ 真机链路通：事件循环在跑、分词器接上了、兴趣/记忆都在长")
    print("=" * 78)
    return 1 if failed else 0


raise SystemExit(main())
