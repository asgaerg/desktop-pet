"""设置页 API 真的能读写吗（2026-10-15）。

## ⚠️ 上一版我测错了名字

我断言 `window.pet` 是 undefined，得出"设置页没有 API" —— **错的**。

```
设置页的 API 挂在 `window.petSettings`（contextBridge.exposeInMainWorld('petSettings', ...)）
宠物页的才是 `window.pet`
```

⚠️ **"没找到"和"不存在"是两件事** —— 这个坑本项目在 §101 刚踩过一次
（渲染层嗅不到 WebSocket，因为连接在主进程）。

## 这个脚本查什么

```
① window.petSettings 在不在，有几个方法
② 每个 tab 的关键控件能不能读到**真值**（不是空）
③ 读写往返：读 -> 写同一个值 -> 再读（值不变 = 通道双向通）
④ 运行时错误
```

⚠️ **不写新值** —— 用户可能正在用。只做"写回原值"的幂等往返。
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
                    return f"__EXC__ {str(result['exceptionDetails'])[:400]}"
                return (result.get("result") or {}).get("value")
        return None

    def close(self) -> None:
        self.sock.close()


def find(name: str) -> dict | None:
    for target in cdp_targets():
        if str(target.get("url", "")).endswith(name):
            return target
    return None


settings = find("settings.html")
if settings is None:
    print("  设置窗口不在，先打开它…")
    pet = find("index.html")
    if pet is None:
        print("  ❌ 宠物页也不在")
        raise SystemExit(1)
    client = Cdp(pet)
    client.evaluate("(function(){window.pet.openSettings();return 1;})()")
    client.close()
    time.sleep(6.0)
    settings = find("settings.html")

if settings is None:
    print("  ❌ 设置窗口打不开")
    raise SystemExit(1)

sc = Cdp(settings)

print("═" * 78)
print("① window.petSettings 在不在")
print("═" * 78)
info = sc.evaluate(
    chr(10).join(
        [
            "(function(){",
            "  var ps = window.petSettings;",
            "  if (!ps) return JSON.stringify({ present: false,",
            "    globals: Object.keys(window).filter(function(k){",
            "      return /pet/i.test(k); }) });",
            "  var keys = Object.keys(ps);",
            "  return JSON.stringify({ present: true, count: keys.length,",
            "    keys: keys.slice(0, 70) });",
            "})()",
        ]
    )
)
try:
    parsed = json.loads(str(info))
except (json.JSONDecodeError, TypeError):
    print(f"  {info}")
    parsed = {}

if not parsed.get("present"):
    print(f"  ❌ window.petSettings 不存在")
    print(f"     window 上带 pet 的全局：{parsed.get('globals')}")
    sc.close()
    raise SystemExit(1)

print(f"  ✅ window.petSettings 有 {parsed['count']} 个方法")
print(f"     {parsed['keys']}")

print()
print("═" * 78)
print("② load() 能读到真配置吗")
print("═" * 78)
loaded = sc.evaluate(
    chr(10).join(
        [
            "new Promise(function(resolve){",
            "  window.petSettings.load().then(function(cfg){",
            "    if (!cfg) { resolve('load() 返回空'); return; }",
            "    var keys = Object.keys(cfg);",
            "    var summary = { top: keys.slice(0, 20) };",
            "    /* 抓几个具体值，证明不是空壳 */",
            "    try {",
            "      summary.petScale = cfg.petScale;",
            "      summary.activity = cfg.activity ? (cfg.activity.overall !== undefined ? cfg.activity.overall : JSON.stringify(cfg.activity).slice(0,40)) : undefined;",
            "      summary.theme = cfg.theme;",
            "      summary.vision = cfg.visionEnabled;",
            "      summary.bubble = cfg.bubbleVisible;",
            "      summary.provider = cfg.providers ? (cfg.providers.brain ? cfg.providers.brain.provider + '/' + cfg.providers.brain.model : '?') : undefined;",
            "      summary.tts = cfg.providers && cfg.providers.tts ? cfg.providers.tts.provider : undefined;",
            "      summary.tools = cfg.tools ? JSON.stringify(cfg.tools) : undefined;",
            "      summary.memDays = cfg.memory ? cfg.memory.history_days : undefined;",
            "    } catch (e) { summary.err = String(e); }",
            "    resolve(JSON.stringify(summary));",
            "  }).catch(function(e){ resolve('load() 失败：' + e); });",
            "})",
        ]
    )
)
try:
    cfg = json.loads(str(loaded))
    print(f"  顶层键：{cfg.get('top')}")
    for key in (
        "petScale",
        "activity",
        "theme",
        "vision",
        "bubble",
        "provider",
        "tts",
        "tools",
        "memDays",
    ):
        print(f"    {key:<10} = {cfg.get(key)}")
except (json.JSONDecodeError, TypeError):
    print(f"  {loaded}")

print()
print("═" * 78)
print("③ 幂等往返：读 -> 写回原值 -> 再读（值不变 = 双向通道通）")
print("═" * 78)

roundtrip = sc.evaluate(
    chr(10).join(
        [
            "(function(){",
            "  var ps = window.petSettings;",
            "  var results = [];",
            "  return ps.load().then(function(cfg){",
            "    var tasks = [];",
            "    /* 挑几个 setter，把**读到的原值**写回去 —— 幂等，不会改坏用户配置 */",
            "    if (typeof ps.setPetScale === 'function' && cfg.petScale !== undefined) {",
            "      tasks.push(ps.setPetScale(cfg.petScale).then(function(){",
            "        return ps.petScale().then(function(v){",
            "          results.push('setPetScale(' + cfg.petScale + ') -> 读回 ' + v);",
            "        });",
            "      }).catch(function(e){ results.push('setPetScale 失败: ' + e); }));",
            "    }",
            "    if (typeof ps.setActivity === 'function') {",
            "      var act = cfg.activity && cfg.activity.overall !== undefined ? cfg.activity.overall : 0.6;",
            "      tasks.push(ps.setActivity(act).then(function(){",
            "        return ps.activity().then(function(v){",
            "          results.push('setActivity(' + act + ') -> 读回 ' + JSON.stringify(v).slice(0, 40));",
            "        });",
            "      }).catch(function(e){ results.push('setActivity 失败: ' + e); }));",
            "    }",
            "    if (typeof ps.setBubbleLinger === 'function' && cfg.bubbleLingerMs !== undefined) {",
            "      tasks.push(ps.setBubbleLinger(cfg.bubbleLingerMs).then(function(){",
            "        return ps.bubbleLinger().then(function(v){",
            "          results.push('setBubbleLinger(' + cfg.bubbleLingerMs + ') -> 读回 ' + v);",
            "        });",
            "      }).catch(function(e){ results.push('setBubbleLinger 失败: ' + e); }));",
            "    }",
            "    if (!tasks.length) { results.push('没有可用的 setter/getter 对'); return JSON.stringify(results); }",
            "    return Promise.all(tasks).then(function(){ return JSON.stringify(results); });",
            "  }).catch(function(e){ return 'ERR ' + e; });",
            "})()",
        ]
    )
)
try:
    items = json.loads(str(roundtrip))
    for item in items:
        ok = "✅" if "->" in str(item) and "失败" not in str(item) else "❌"
        print(f"  {ok} {item}")
except (json.JSONDecodeError, TypeError):
    print(f"  {roundtrip}")

print()
print("═" * 78)
print("④ 8 个 tab 的控件都有真值吗（不是空）")
print("═" * 78)

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
for tab in TABS:
    result = sc.evaluate(
        chr(10).join(
            [
                "(function(){",
                f"  var pane = document.getElementById({json.dumps(tab)});",
                "  if (!pane) return 'MISSING';",
                "  var cs = pane.querySelectorAll('input, select, textarea');",
                "  if (!cs.length) return 'NO_CONTROLS';",
                "  var filled = 0, empty = 0, names = [];",
                "  for (var i = 0; i < cs.length; i++) {",
                "    var c = cs[i];",
                "    var v = c.type === 'checkbox' ? String(c.checked)",
                "          : String(c.value === undefined ? '' : c.value);",
                "    if (v !== '' && v !== 'undefined') { filled++; } else { empty++; }",
                "    if (i < 6) names.push((c.id || c.tagName) + '=' + v.slice(0, 14));",
                "  }",
                "  return JSON.stringify({ total: cs.length, filled: filled, empty: empty,",
                "                          sample: names });",
                "})()",
            ]
        )
    )
    if result in ("MISSING", "NO_CONTROLS"):
        print(f"  ⚠️ {tab:<14} {result}")
        continue
    try:
        r = json.loads(str(result))
        flag = "✅" if r["empty"] == 0 else ("⚠️" if r["filled"] > 0 else "❌")
        print(
            f"  {flag} {tab:<14} {r['filled']}/{r['total']} 有值"
            f"（空 {r['empty']}）  {' | '.join(r['sample'][:4])}"
        )
    except (json.JSONDecodeError, TypeError):
        print(f"  {result}")

print()
print("═" * 78)
print("⑤ 运行时错误")
print("═" * 78)
sc.evaluate(
    chr(10).join(
        [
            "(function(){",
            "  if (window.__h2) return 1;",
            "  window.__h2 = true; window.__e2 = [];",
            "  window.addEventListener('error', function(e){ window.__e2.push(String(e.message||e.type).slice(0,150)); });",
            "  window.addEventListener('unhandledrejection', function(e){ window.__e2.push('rej: ' + String(e.reason).slice(0,150)); });",
            "  return 1;",
            "})()",
        ]
    )
)
time.sleep(1.2)
print(f"  {sc.evaluate('JSON.stringify(window.__e2 || [])')}")

sc.close()
