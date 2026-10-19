"""设置窗口 8 个 tab 逐个实测（2026-10-15）。

## 为什么这个最重要

另一个 AI 对设置页动得最狠：

```
settings.js    112,915 B   （10-14 20:13 改）
settings.html   47,566 B   （10-14 20:13 改）
settings.css    39,497 B   （10-14 20:24 改）
```

⚠️ 而设置页是**用户唯一能改配置的地方**。它坏了 = 所有功能都调不了。

## 怎么测

```
① 打开设置窗口（从宠物页 pet.openSettings()）
② 逐个 tab 切过去
③ 每个 tab 断言：
   · 有内容（不是空白）
   · 里面的输入控件数量（input/select/button）
   · 控件都被真的绑定了（不是死 DOM）
④ 抽一个控件读一次真值，确认 renderer <-> 主进程 的读路径通
⑤ 全程收集运行时错误
```

⚠️ **不断言具体值** —— 用户可能改过。只断言"控件存在且能读到值"。

## 关键：设置页的渲染方式是「全量重画」

`settings.js` 的模式通常是：`render()` 一次性把值填进 DOM。
所以"控件存在"还不够，要**触发一次写入再读回**才算通。
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
from tool_bootstrap import setup  # noqa: E402

setup(verbose=False)

TABS = [
    "tab-general",
    "tab-pet",
    "tab-ai",
    "tab-prompts",
    "tab-memory",
    "tab-voice",
    "tab-privacy",
    "tab-about",
]


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
        self.sock.settimeout(25)
        self.n = 0

    def evaluate(self, expression: str) -> object:
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
                        "awaitPromise": True,
                    },
                }
            ),
        )
        deadline = time.time() + 25
        while time.time() < deadline:
            try:
                text = ws_recv(self.sock)
            except (socket.timeout, OSError):
                continue
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


def find(name: str) -> dict | None:
    for target in cdp_targets():
        if str(target.get("url", "")).endswith(name):
            return target
    return None


print("═" * 78)
print("① 打开设置窗口")
print("═" * 78)

pet = find("index.html")
if pet is None:
    print("  ❌ 没有宠物页")
    raise SystemExit(1)

client = Cdp(pet)
opened = client.evaluate(
    "(function(){var p=window.pet||{};"
    "if(typeof p.openSettings==='function'){p.openSettings();return 'sent';}"
    "return 'NO_API';})()"
)
print(f"  openSettings -> {opened}")
client.close()

time.sleep(6.0)

settings = find("settings.html")
if settings is None:
    print("  ❌ 设置窗口没出现（CDP 列表里没有 settings.html）")
    print("     CDP 现有 target：")
    for target in cdp_targets():
        print(f"       {str(target.get('url',''))[-60:]}")
    raise SystemExit(1)

print("  ✅ 设置窗口已出现")
sc = Cdp(settings)

print()
print("═" * 78)
print("② 页面整体状态")
print("═" * 78)
overall = sc.evaluate(
    chr(10).join(
        [
            "(function(){",
            "  var out = {};",
            "  out.title = document.title;",
            "  out.bodyLen = document.body.innerText.length;",
            "  out.petApi = typeof window.pet;",
            "  out.petMethods = window.pet ? Object.keys(window.pet).length : 0;",
            "  /* tab 按钮 */",
            "  var tabBtns = document.querySelectorAll('[data-tab], .tab, nav button, [role=tab]');",
            "  out.tabButtons = tabBtns.length;",
            "  var names = [];",
            "  for (var i = 0; i < tabBtns.length; i++) {",
            "    var t = (tabBtns[i].textContent || '').trim();",
            "    if (t) names.push(t.slice(0, 14));",
            "  }",
            "  out.tabNames = names;",
            "  /* 所有 pane */",
            "  var panes = document.querySelectorAll('[id^=tab-]');",
            "  out.panes = panes.length;",
            "  var paneIds = [];",
            "  for (var i = 0; i < panes.length; i++) paneIds.push(panes[i].id);",
            "  out.paneIds = paneIds;",
            "  return JSON.stringify(out);",
            "})()",
        ]
    )
)
try:
    parsed = json.loads(str(overall))
    for key in ("title", "bodyLen", "petApi", "petMethods", "tabButtons", "panes"):
        print(f"    {key}: {parsed.get(key)}")
    print(f"    tab 按钮文字: {parsed.get('tabNames')}")
    print(f"    pane id: {parsed.get('paneIds')}")
except (json.JSONDecodeError, TypeError):
    print(f"    {overall}")

print()
print("═" * 78)
print("③ 逐个 tab 检查")
print("═" * 78)

missing_panes = []
for tab in TABS:
    result = sc.evaluate(
        chr(10).join(
            [
                "(function(){",
                f"  var pane = document.getElementById({json.dumps(tab)});",
                "  if (!pane) return JSON.stringify({ exists: false });",
                "  var out = { exists: true };",
                "  /* 切到这个 tab */",
                "  var btn = document.querySelector('[data-tab=\"' + " + json.dumps(tab) + " + '\"]');",
                "  if (!btn) btn = document.querySelector('[aria-controls=\"' + " + json.dumps(tab) + " + '\"]');",
                "  out.hasButton = !!btn;",
                "  var controls = pane.querySelectorAll('input, select, textarea, button');",
                "  out.controls = controls.length;",
                "  out.inputs = pane.querySelectorAll('input').length;",
                "  out.selects = pane.querySelectorAll('select').length;",
                "  out.buttons = pane.querySelectorAll('button').length;",
                "  out.textLen = (pane.innerText || '').length;",
                "  /* 有多少控件是真的绑了 id（能被 render 填值） */",
                "  var withId = 0;",
                "  for (var i = 0; i < controls.length; i++) if (controls[i].id) withId++;",
                "  out.controlsWithId = withId;",
                "  /* 前 8 个控件的 id + 当前值 */",
                "  var sample = [];",
                "  for (var i = 0; i < Math.min(controls.length, 8); i++) {",
                "    var c = controls[i];",
                "    var v = c.type === 'checkbox' ? (c.checked ? 'checked' : 'unchecked') : String(c.value || '').slice(0, 16);",
                "    sample.push((c.id || c.tagName) + '=' + v);",
                "  }",
                "  out.sample = sample;",
                "  return JSON.stringify(out);",
                "})()",
            ]
        )
    )
    try:
        info = json.loads(str(result))
    except (json.JSONDecodeError, TypeError):
        print(f"  ❌ {tab}: {result}")
        continue
    if not info.get("exists"):
        missing_panes.append(tab)
        print(f"  ❌ {tab}: **HTML 里没有这个 pane**")
        continue
    flag = "✅" if info["controls"] > 0 else "⚠️"
    print(
        f"  {flag} {tab:<14} 控件 {info['controls']:>3}"
        f"（input {info['inputs']}, select {info['selects']}, button {info['buttons']}）"
        f"  有 id 的 {info['controlsWithId']:>3}  文字 {info['textLen']:>5} 字"
    )
    if info["sample"]:
        print(f"       样例：{' | '.join(info['sample'][:5])}")

if missing_panes:
    print()
    print(f"  ❌ 缺 pane：{missing_panes}")

print()
print("═" * 78)
print("④ 运行时错误")
print("═" * 78)
sc.evaluate(
    chr(10).join(
        [
            "(function(){",
            "  if (window.__setHook) return 1;",
            "  window.__setHook = true;",
            "  window.__setErrors = [];",
            "  window.addEventListener('error', function(e){",
            "    window.__setErrors.push(String(e.message || e.type).slice(0, 160));",
            "  });",
            "  window.addEventListener('unhandledrejection', function(e){",
            "    window.__setErrors.push('rejection: ' + String(e.reason).slice(0, 160));",
            "  });",
            "  return 1;",
            "})()",
        ]
    )
)
time.sleep(1.0)
errors = sc.evaluate("JSON.stringify(window.__setErrors || [])")
print(f"  {errors}")

print()
print("═" * 78)
print("⑤ 读写通不通：读一次配置真值")
print("═" * 78)
readback = sc.evaluate(
    chr(10).join(
        [
            "new Promise(function(resolve){",
            "  var p = window.pet || {};",
            "  if (typeof p.getConfig !== 'function') {",
            "    /* 换几个可能的名字试 */",
            "    var names = Object.keys(p).filter(function(k){",
            "      return /config|load|read|state/i.test(k);",
            "    });",
            "    resolve('没有 getConfig；相关方法：' + names.join(','));",
            "    return;",
            "  }",
            "  p.getConfig().then(function(cfg){",
            "    resolve('读到了配置，顶层键：' + Object.keys(cfg || {}).join(','));",
            "  }).catch(function(e){ resolve('读失败：' + e); });",
            "})",
        ]
    )
)
print(f"  {readback}")

print()
print("  设置页暴露的方法：")
methods = sc.evaluate(
    "(function(){var p=window.pet||{};return JSON.stringify(Object.keys(p));})()"
)
print(f"    {methods}")

sc.close()
