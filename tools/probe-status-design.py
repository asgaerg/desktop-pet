r"""实测状态条的外观是否符合设计稿（2026-10-12）。

## 为什么必须实测

CSS 改动**不会报错** —— 尺寸算错、灯色写错、文字溢出，
全都表现为"看起来不对"，而没有任何日志。

判据（全部对着设计稿的数字）：

  · 胶囊 40x76（量的 PDF 路径包围盒；描边 42x78）
  · 圆角 20px（半宽 = 胶囊形）
  · 灯 10x10、距顶 12px、水平居中
  · 文字距灯底约 14px
  · **四档的灯色**（聆听已删，见 §110）
  · **文字不溢出**（最多 2 个字）

## 怎么测

用 CDP 连上状态条窗口，直接在页面里量 DOM，并逐个切换 `data-state`。

## 🔴 2026-10-16：CDP 辅助函数改成**自带**（原来是从别的文件抽出来的）

原来这里是这样拿 `pages()` / `js()` 的：

```python
src = (ROOT / "tools" / "check-tts-layout.py").read_text(encoding="utf-8")
mp = re.search(r"(def pages\(\):.*?)(?=\\ndef )", src, re.S)
mj = re.search(r"(def js\(.*?)(?=\\n[a-zA-Z@#]|\\Z)", src, re.S)
exec(mp.group(1), ns)
exec(mj.group(1), ns)
```

⚠️ 这是**从另一个脚本里"抠"代码出来 exec** —— 非常脆：

  · 删掉那个文件，这个探针**直接崩**（清探针时就是这么崩的）
  · 那边改个函数名/缩进，正则匹配不上，`mp` 或 `mj` 是 `None`，
    然后 `AttributeError` —— 报错信息完全指不到真正的原因
  · 依赖方向反了：**一个检查不该依赖另一个一次性脚本的内部实现**

✅ 所以现在**自带**一个最小的 CDP 客户端（下面那段）。
自包含、零外部文件依赖、报错清楚。
"""

from __future__ import annotations

import base64
import json
import os
import re
import socket
import struct
import sys
import time
import urllib.request

sys.path.insert(0, ".")
from tool_bootstrap import ROOT, setup  # noqa: E402

setup(verbose=False)

CDP = "http://127.0.0.1:9222"


class MiniWs:
    """最小的 CDP WebSocket 客户端（项目里没有 websockets 包）。

    ⚠️ 为什么自己写而不用库：这个项目**故意不引入 websockets / websocket-client**
    （依赖越少越好，而且 CDP 握手就那几步）。`pet/testing.py` 里那份是给
    sidecar 用的，这儿只需要「发一条 Runtime.evaluate、收一条结果」。

    ⚠️ 必须自己实现帧的编解码：
      · 客户端发的帧**必须加掩码**（WebSocket 协议要求，不加服务器直接断）
      · 收的帧可能分片、可能是 ping（心跳），要跳过非文本帧
    """

    def __init__(self, url: str, timeout: float = 30.0) -> None:
        rest = url.split("://", 1)[1]
        host_port, _, path = rest.partition("/")
        host, _, port = host_port.partition(":")
        self.host, self.port, self.path = host, int(port or 80), "/" + path
        self.timeout = timeout
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

    def _send(self, text: str) -> None:
        data = text.encode()
        mask = os.urandom(4)
        header = bytearray([0x81])  # FIN + 文本帧
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
                raise ConnectionError("CDP 连接断了")
            self.buf.extend(chunk)
        out = bytes(self.buf[:n])
        del self.buf[:n]
        return out

    def _recv(self) -> str:
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
            # 其它帧（ping/pong/close）跳过 —— 不跳会把它们当成 JSON 解析

    def call(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        self._send(json.dumps({"id": self._id, "method": method, "params": params or {}}))
        while True:
            message = json.loads(self._recv())
            if message.get("id") == self._id:
                return message.get("result") or {}


def pages() -> list[dict]:
    """问 CDP 要页面列表。"""
    with urllib.request.urlopen(f"{CDP}/json/list", timeout=8) as response:
        return json.loads(response.read().decode("utf-8"))


def js(ws_url: str, expression: str):
    """在一个页面里打一段 JS，返回它的值。"""
    ws = MiniWs(ws_url)
    ws.connect()
    try:
        result = ws.call("Runtime.evaluate", {
            "expression": expression, "returnByValue": True, "awaitPromise": True,
        })
        if result.get("exceptionDetails"):
            details = result["exceptionDetails"]
            raise RuntimeError(
                (details.get("exception") or {}).get("description") or str(details)
            )
        return (result.get("result") or {}).get("value")
    finally:
        try:
            ws.sock.close()
        except OSError:
            pass


status = next((p for p in pages() if "status.html" in (p.get("url") or "")), None)
if not status:
    print("  ❌ 找不到状态条窗口（应用在跑吗？带 --debug-port=9222 了吗？）")
    raise SystemExit(1)

ws = status["webSocketDebuggerUrl"]

MEASURE = r"""
JSON.stringify((() => {
  const bar = document.getElementById('bar');
  const light = document.getElementById('light');
  const text = document.getElementById('text');
  const rb = bar.getBoundingClientRect();
  const rl = light.getBoundingClientRect();
  const rt = text.getBoundingClientRect();
  const cs = getComputedStyle(bar);
  const bodyBg = getComputedStyle(document.body).backgroundColor;
  return {
    bar: [Math.round(rb.width), Math.round(rb.height)],
    barTop: Math.round(rb.top),
    radius: cs.borderRadius,
    barBg: cs.backgroundColor,
    light: [Math.round(rl.width), Math.round(rl.height)],
    lightTop: Math.round(rl.top - rb.top),
    lightCenterOffset: Math.round((rl.left - rb.left) - (rb.width - rl.width) / 2),
    lightColor: getComputedStyle(light).backgroundColor,
    textH: Math.round(rt.height),
    textGap: Math.round(rt.top - rl.bottom),
    text: text.textContent,
    bodyBg: bodyBg,
    viewport: [window.innerWidth, window.innerHeight],
  };
})())
"""


def measure(state=None, label=None) -> dict:
    if state is not None:
        script = (
            f"document.getElementById('light').dataset.state = {json.dumps(state)};"
            + (f"document.getElementById('text').textContent = {json.dumps(label)};" if label else "")
            + "1"
        )
        js(ws, script)
        time.sleep(0.25)
    return json.loads(js(ws, MEASURE))


passed = 0
failed: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    global passed
    if ok:
        passed += 1
        print(f"  OK   {name}")
    else:
        failed.append(f"{name} —— {detail}")
        print(f"  FAIL {name}\n       {detail}")


print("  ═══ ① 几何（对着设计稿的数字）═══")
data = measure()
print(f"    视口 {data['viewport']}  胶囊 {data['bar']}  圆角 {data['radius']}")
print(f"    灯 {data['light']} 距顶 {data['lightTop']} 水平偏移 {data['lightCenterOffset']}px")
print(f"    文字高 {data['textH']} 距灯底 {data['textGap']}px")
print(f"    body 背景 {data['bodyBg']}   胶囊底色 {data['barBg']}")

# 🔴 2026-10-17：原来写死 `== [40, 76]`。现在 `.bar` 的高度是
# `calc(100vh - 2px)`，视口高度随标签字数变，所以**不能写死 76**。
# 宽度仍固定 40（只长高不长宽）；高度改为"跟着视口走"（见 ④ 那段）。
check("胶囊宽 40px（设计稿的胶囊本体；高度随字数变，见 ④）",
      data["bar"][0] == 40, f"实际 {data['bar']}")
# ⚠️ 判据是「**装得下**」，而不是"等于某个数"。
#
# 窗口设成 42x78（胶囊 40x76 + 四周 1px 边框），但 125% DPI 下
# `window.innerWidth` 实测是 **43**（0.8px 的边框取整到 1）。
# 那个 1px 是**显示器的属性**，不是 bug —— 用 `== [42, 78]` 比会得到一个
# 永远修不掉的假失败（我为此白查了一轮）。
#
# 真正要保证的是：**不裁**（视口不小于胶囊）且**不留透明带**（不显著更大）。
_vp_w, _vp_h = data["viewport"]
check(
    "视口装得下胶囊，且不留多余透明带",
    _vp_w - data["bar"][0] <= 4 and _vp_h - data["bar"][1] <= 4,
    f"视口 {data['viewport']}，胶囊 {data['bar']} —— 差值过大说明窗口尺寸与 CSS 不一致",
)
check("圆角 20px（半宽 = 胶囊形）", data["radius"].startswith("20px"), f"实际 {data['radius']}")
check("灯 10x10（设计稿量出来的：x15..25、y12..22）",
      data["light"] == [10, 10], f"实际 {data['light']}")
check("灯距顶 12px（设计稿 y0=12）", abs(data["lightTop"] - 12) <= 1,
      f"实际 {data['lightTop']}px")
"""
⚠️ 容差 ±1 是**必需的**，不是偷懒。

这台机器是 **125% DPI 缩放**，实测 `borderTop: 1px` 会渲染成 **0.8px**
（1 / 1.25）。于是 `padding: 13px` + `border 0.8px` = **13.8px** ——
与设计稿的 14px 差 0.2px，但 `===` 比较会判它失败。

我为此**白改了一轮 CSS**（以为 padding 算错了）。
**几何断言必须留容差** —— DPI 缩放、子像素取整都会引入零点几的误差，
而那是显示器的属性，不是 bug。
"""
check("灯水平居中", abs(data["lightCenterOffset"]) <= 1, f"偏移 {data['lightCenterOffset']}px")
check("文字距灯底约 14px（设计稿 26 -> 40.8）",
      11.5 <= data["textGap"] <= 14, f"实际 {data['textGap']}px")
check("**body 背景透明**（用户要求「背景记得搞透明的」）",
      "rgba(0, 0, 0, 0)" in data["bodyBg"], f"实际 {data['bodyBg']}")
check("胶囊底色 #1e2229 = rgb(30, 34, 41)",
      data["barBg"] == "rgb(30, 34, 41)", f"实际 {data['barBg']}")

print()
print("  ═══ ② 五档状态的灯色（对着设计稿）═══")
"""
🔴🔴 2026-10-17：**六档 -> 五档**，`voice`（语音准备中）被有意删除。

其他 AI 在 10-17 把状态条整个重做了（见
`docs/开发日志/开发日志-2026-10-17.md` §4）。变化有两处：

    ① `voice`（语音准备中）删了 —— 用户拍板：它和"思考中"重复，
       TTS 合成期并入 `thinking`；
    ② `speaking` 的灯色 **琥珀 -> 青**（`#32ade6`），
       文字 **「说话」->「说话中」**。

⚠️ 注意有个巧合，很容易骗过眼睛：**原来 `voice` 的青色 `#32ade6`
现在给了 `speaking`**。所以如果有人只查"青色还在不在"，会以为是
voice 还在 —— 必须同时断言**档位名集合**（下面 ④ 那段）。

## 2026-10-16 那次的教训（仍然有效，只是集合变了）

当时发现不是四档而是六档，漏了 `thinking`（紫）和 `voice`（青）——
因为检查只盯着"我恰好想到的那几档"。

✅ 判据不变：**断言完整集合，而不是我想到的几档**。
只是集合本身从六档变成了五档。
"""
EXPECT = {
    "idle": ("rgb(52, 199, 89)", "空闲"),
    "thinking": ("rgb(191, 122, 240)", "思考"),
    "searching": ("rgb(47, 134, 237)", "联网搜索中"),
    "speaking": ("rgb(50, 173, 230)", "说话中"),
    "error": ("rgb(229, 72, 77)", "异常"),
}
for state, (want_color, want_label) in EXPECT.items():
    d = measure(state, want_label)
    ok = d["lightColor"] == want_color and d["text"] == want_label
    mark = "OK  " if ok else "FAIL"
    if ok:
        passed += 1
    else:
        failed.append(f"{state} 灯色/文字 —— 期望 {want_color}/{want_label}，实际 {d['lightColor']}/{d['text']}")
    print(f"    {mark} {state:<10} 灯 {d['lightColor']:<22} 文字 {d['text']!r}")
    if not ok:
        print(f"         期望 {want_color} / {want_label!r}")

print()
print("  ═══ ③ 🔵 蓝色**只**给联网搜索（用户明确要求）═══")
"""
⚠️ 2026-10-16 扩到**六档**，2026-10-17 收敛到**五档**（voice 已删）。
「只有搜索是蓝色」这条断言如果只数部分档位，
没被数到的那几档**没被看着** —— 万一哪天有人给它们也写成蓝色，检查不会响。
"""
_ALL = list(EXPECT.items())
_measured = {kind: measure(kind, label) for kind, label in _ALL}

searching = _measured["searching"]
speaking = _measured["speaking"]
check("搜索是蓝色", searching["lightColor"] == "rgb(47, 134, 237)", searching["lightColor"])
check("⚠️ 说话**不是**蓝色（用户：说话的时候就不蓝色了）",
      speaking["lightColor"] != "rgb(47, 134, 237)",
      f"说话仍然是 {speaking['lightColor']}")
check("说话是青色 #32ade6（2026-10-17 由琥珀改青）",
      speaking["lightColor"] == "rgb(50, 173, 230)", speaking["lightColor"])
_blue_states = [k for k, d in _measured.items() if d["lightColor"] == "rgb(47, 134, 237)"]
check(f"**只有搜索是蓝色**（{len(_ALL)} 档里只数出 1 个）",
      len(_blue_states) == 1,
      "蓝色出现在了不止一个状态里：" + str(_blue_states))
check(f"{len(_ALL)} 档的灯色**两两不同**（否则用户分不清）",
      len({d["lightColor"] for d in _measured.values()}) == len(_ALL),
      "有重复：" + str(sorted({d["lightColor"] for d in _measured.values()})))
"""
⚠️ 顺带验"聆听真的没了" —— **必须读文件，不能只看运行时**。

## 🔴 为什么运行时那条检查是**假的**（我写错过一版，记下来）

第一版是「塞一个不存在的 state，看它是不是黄灯」。它**恒绿**：

```
塞 'listening' / '不存在的状态' / ''  ->  全都是 rgb(52,199,89)（基础绿）
```

因为**已加载的页面看不到磁盘上的 CSS 改动** —— `pet://` 协议层会缓存
样式表，改完文件不 reload 是不生效的。

于是我把"聆听规则"注入 CSS 跑了一遍：**检查照样通过**。
也就是说那条检查**永远不会失败** —— 比没有还糟（它给人虚假的安全感）。

## ✅ 改成读文件

"CSS 里有没有这条规则"本来就是个**文件层面**的事实，
直接读 `status.css` 断言最快也最准：

```
真规则 = 选择器后面跟 `{`   （注释里提到名字不算）
```
"""
import re
from pathlib import Path

print()
print("  ═══ ③b 聆听**确实删干净了**（读文件断言，不靠运行时）═══")

_renderer = ROOT / "apps" / "desktop" / "src" / "renderer"
_sel_listen = ".light[data-state='listening']"

"""
🔴 2026-10-16 纠正：**灯色规则不是四档，是六档**
🔴🔴 2026-10-17 再纠正：**现在是五档**（`voice` 删除）

我原来以为 `StatusKind` 只有 `idle/speaking/searching/error` 四档，
于是下面只断言四条规则 + 四档类型。那是我**看漏了**：

    10-17 之后的五档：idle / thinking / searching / speaking / error
                       ↑ 空闲   ↑ 思考      ↑ 联网搜索  ↑ 说话     ↑ 异常
    （10-17 删掉的：voice「语音准备中」）

⚠️ 「彻底删除聆听」删掉的是**第七档 `listening`**，不是把档数砍到四。
我当时只查了「`'listening'` 在不在集合里」，**没核对完整集合**，
于是把"六档"误记成"四档"写进了文档 —— 而检查只盯着那四档，
**漏掉的两档（thinking / voice）等于没人看着**。

✅ 所以现在：断言**完整集合**，而不是"我恰好想到的那几档"。
这样多出来或少掉任何一档都会报。
"""
_EXPECTED_KINDS = {"idle", "thinking", "searching", "speaking", "error"}
_EXPECTED_SELECTORS = [f".light[data-state='{k}']" for k in sorted(_EXPECTED_KINDS)]

_css = (_renderer / "status.css").read_text(encoding="utf-8")
# 只看"真正的规则"（选择器 + {），注释里提到名字的不算
_real_listen = re.findall(re.escape(_sel_listen) + r"\s*\{", _css)
check("status.css 里没有『聆听』的灯色规则",
      not _real_listen,
      f"还有 {len(_real_listen)} 条 —— 删干净才对（用户要求「彻底删除聆听」）")

# 双向：这几档的规则都必须在（防止删多了）
for _sel in _EXPECTED_SELECTORS:
    _hits = re.findall(re.escape(_sel) + r"\s*\{", _css)
    check(f"status.css 保留了 {_sel.split(chr(39))[1]} 的规则",
          bool(_hits), "没找到 —— 别把该留的也删了")

# 反向：CSS 里不能有**这几档之外的**灯色规则（比如偷偷加回来一档）
_found_states = set(re.findall(r"\.light\[data-state='(\w+)'\]\s*\{", _css))
check(f"status.css 的灯色规则正好是那 {len(_EXPECTED_KINDS)} 档",
      _found_states == _EXPECTED_KINDS,
      f"多出来 {sorted(_found_states - _EXPECTED_KINDS)}，"
      f"少了 {sorted(_EXPECTED_KINDS - _found_states)}")

_ts = (ROOT / "apps" / "desktop" / "src" / "main" / "statusWindow.ts").read_text(encoding="utf-8")
"""
⚠️ 正则要**跨行**（`re.S`）—— 类型定义可能是多行的：

    export type StatusKind =
      | 'idle'
      | 'speaking'
      ...

第一版没加 `re.S`，匹配出来是空集合，于是「剩下的正是那几档」这条
**永远失败**（`实际 []`）—— 而它本来是个有效的检查。是它自己报出来的。

⚠️ 而且 `[^;]+` 会**贪到下一个类型**。第一版用 `StatusKind = ([^;]+);`，
本意是取到分号，但 `StatusKind` 和 `VoiceState` 挨着写的时候会把两个
类型都吃进来。所以额外断言 **`StatusKind` 和 `VoiceState` 不能混淆** ——
`listening` 在 `VoiceState` 里是**对的**（语音管线要它），在 `StatusKind`
里才是错的。这条区分是 §110 的核心，必须测。
"""
_m = re.search(r"export type StatusKind =([^;]+);", _ts, re.S)
"""
🔴🔴 必须**先剥注释**再抽成员 —— 这里已经栽了第三次同类坑。

`StatusKind` 的定义里，被删掉的 `'voice'` **留了一条注释**说明为什么删：

    export type StatusKind =
      | 'idle'
      ...
      | 'searching'
      // 🔴 'voice'（语音准备中）2026-10-17 删 —— 和思考中重复（用户拍板）。
      | 'speaking'
      | 'error';

而 `[^;]+` 把这段注释也吃进来了，于是 `re.findall(r"'(\\w+)'")` 抽到
`voice` —— 检查报「多出来 voice」，**代码其实早已删干净**。

⚠️ 这个坑的三次记录：
    ① 10-16 `statusKindFor` 注释里的「聆听」被当成代码
    ② 10-17 `dist/main.cjs` 里 `tray.ts` 的注释被当成菜单项
    ③ 10-17 本条

✅ 结论：**凡是"某个词不该存在"的检查，一律先剥注释**。
   注释里提到已删的东西是**好事**（说明白为什么删）。
"""
_ts_code = re.sub(r"/\*[\s\S]*?\*/", " ", _ts)   # 块注释
_ts_code = re.sub(r"(?m)//[^\n]*", " ", _ts_code)  # 行注释
_m = re.search(r"export type StatusKind =([^;]+);", _ts_code, re.S)
_kinds = set(re.findall(r"'(\w+)'", _m.group(1))) if _m else set()
check("StatusKind 里没有 'listening'",
      "listening" not in _kinds,
      f"实际 {sorted(_kinds)}")
check(f"StatusKind 正好是那 {len(_EXPECTED_KINDS)} 档",
      _kinds == _EXPECTED_KINDS,
      f"多出来 {sorted(_kinds - _EXPECTED_KINDS)}，"
      f"少了 {sorted(_EXPECTED_KINDS - _kinds)}")
# ⚠️ 2026-10-17 新增：`voice` 必须**不在** StatusKind 里（它是被有意删除的，
#    而它的青色现在给了 speaking —— 只查颜色会以为它还在）
check("StatusKind 里没有 'voice'（2026-10-17 有意删除）",
      "voice" not in _kinds,
      "`voice` 又回来了 —— 用户拍板它和思考中重复，不该有这一档")

# ⚠️ 关键区分：`VoiceState` **仍然要有** `listening`（语音管线在用它）
_ts_voice = re.search(r"type VoiceState =([^;]+);", _ts, re.S)
if _ts_voice:
    _voice_kinds = set(re.findall(r"'(\w+)'", _ts_voice.group(1)))
    check("VoiceState 里**保留**了 'listening'（语音管线要用，不能删）",
          "listening" in _voice_kinds,
          f"实际 {sorted(_voice_kinds)} —— 删了语音管线会坏")

_idx = (ROOT / "apps" / "desktop" / "src" / "main" / "index.ts").read_text(encoding="utf-8")
"""
⚠️ 只查**真实代码**，不查注释 —— 否则会误报。

第一版写的是 `not re.search(r"聆听|在听", _idx)`，结果**误报**了：
我在 `statusKindFor` 上面留了一段注释解释"为什么删掉聆听那一行"，
里面就有「聆听」两个字。注释里提名字是好事（说明白为什么删），
但检查得把它排除掉。

判据：`kind: 'listening'` / `label: '聆听'` 这种**产出一个状态**的代码。
"""
check("statusKindFor 里没有产出 'listening' 的代码",
      not re.search(r"kind:\s*'listening'", _idx),
      "还有一处 `kind: 'listening'` —— 会产出已经删掉的状态档")
check("statusKindFor 里没有产出「聆听」文字的代码",
      not re.search(r"label:\s*'聆听'", _idx),
      "还有一处 `label: '聆听'`")

"""
⚠️ 运行时的"塞个不存在的 state"仍然测一下（当**冒烟**用）：
它应该落到基础色，**和 idle 一样**。这条抓不到"CSS 里加了规则"
（缓存原因），但能抓到"基础色被改坏了"这类事。
"""
ghost = measure("listening", "聆听")
idle_now = measure("idle", "空闲")
check("冒烟：未知 state 落到基础色（和 idle 一致）",
      ghost["lightColor"] == idle_now["lightColor"],
      f"聆听 {ghost['lightColor']} vs 空闲 {idle_now['lightColor']}")

print("  ═══ ④ ⚠️ 文字不能溢出（窗口高度随字数伸缩）═══")
"""
🔴 2026-10-17 改：原来写的是「胶囊固定，只放得下 2 个字」，断言
`bar == [40, 76]`。**那已经不成立了** —— 其他 AI 把窗口改成

    heightForLabel(label) = 78 + max(0, 字数-2) × 15.3

即窗口高度**按文字算**（「联网搜索中」5 个字 -> 78 + 3×15.3 ≈ 124px），
`.bar` 用 `calc(100vh - 2px)` 跟着窗口。

⚠️ 但**运行中的页面窗口尺寸不会因为 `textContent` 变了就自己变** ——
窗口大小是主进程 `setBounds` 决定的。所以在已加载的页面上改文字，
只能验"文字不溢出、胶囊跟着视口高"，**验不了**"主进程有没有按公式算窗口"。

✅ 因此拆成两条：
    · 运行时：胶囊高度 == 视口高 - 2px（证明 `.bar` 是 `calc(100vh-2px)`）
    · 文件层：`heightForLabel` 的公式本身（不依赖窗口，纯函数）
"""
long_text = "联网搜索中"
d = measure("searching", long_text)
print(f"    塞进去 {long_text!r} -> 胶囊 {d['bar']}  视口 {d['viewport']}")
check(
    "胶囊高度跟着视口（`.bar` 是 calc(100vh - 2px)）",
    abs(d["bar"][1] - (d["viewport"][1] - 2)) <= 2,
    f"胶囊高 {d['bar'][1]}，视口高 {d['viewport'][1]}",
)
check(
    "窗口宽度仍是 40px（只长高、不长宽）",
    d["bar"][0] == 40,
    f"实际宽 {d['bar'][0]}",
)

# ── 文件层：heightForLabel 的公式（纯函数，不依赖窗口）──
_sw_src = (ROOT / "apps" / "desktop" / "src" / "main" / "statusWindow.ts").read_text(
    encoding="utf-8",
)
check(
    "statusWindow 里有 heightForLabel（窗口高度按文字算）",
    "export function heightForLabel" in _sw_src,
    "找不到 —— 长标签会溢出胶囊",
)
# 「联网搜索中」5 个字 -> 78 + 3*15.3 = 123.9 -> round = 124
_expected_long = 124
check(
    f"heightForLabel('联网搜索中') 应为 {_expected_long}px（5 字）",
    bool(re.search(r"return Math\.round\(STATUS_HEIGHT \+ Math\.max\(0, n - 2\) \* 15\.3\)",
                   _sw_src)),
    "公式变了 —— 上面的期望值 124 也在变，两处要一起改",
)

# 恢复成空闲
measure("idle", "空闲")

print()
print(f"  ═══ 结果：{passed}/{passed + len(failed)} 通过 ═══")
if failed:
    for item in failed:
        print(f"    ❌ {item}")
    raise SystemExit(1)
print("  ✅ 全部通过")
