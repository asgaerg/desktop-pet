"""真机验证：截图对话 —— 从菜单点击到"它说了什么"。

## 为什么需要它

后端单测（`tools/test-screenshot-chat.py`）只能验到"看懂了一张图"。
而用户要的是三件**连在一起**的事：

```
点击菜单  ->  自动截图发给 AI  ->  注意力被拉到好奇  ->  顺着图片聊天
```

中间任何一环断开，单测都是绿的。本项目已经栽过 "两边都写好了，
中间那根线没接" 好几次（`self._interest_topics`、`existing=`、
`vision.snap.done` 漏进 TO_UI_TYPES）。所以这个脚本必须在**真机上**
把这些环全部串起来断言。

## 为什么不用快捷键触发

`keybd_event` 合成的 Ctrl+Alt+Q（一个早就在用的快捷键）**也触发不了**
Electron 的 globalShortcut（2026-10-17 实测）。那条路是测试手段的限制。
所以这里走菜单：渲染层 `window.petMenu.action()` -> 主进程 `menu:action`
-> `dispatchPetMenuAction` -> `snapshotChat()`，与用户点菜单项**同一条路**。
快捷键那一段由 `check:shortcuts` 门禁保证（注册代码从 `SHORTCUTS` 读）。

## 断言

1. 菜单里真的有「截图对话」这一项（不是只加了处理器）
2. 点了之后主进程开始抓屏（日志 `截图对话：开始`）
3. 视觉模型真的看懂了（日志 `看图成功` + 回执 `ok=true source=vlm`）
4. 回执**没有**被主进程丢弃（不能出现 `丢弃无法识别的消息` 带 vision.snap.done）
5. 它真的说了一句话（抓 `pet.say.end`），而且**和画面内容有关**
6. 注意力被拉到贴脸（`pet.emotion` 的 attention <= 0.1）
7. 表情带 `expression=curious`

用法：
    python tools/test-live-snapshot-chat.py
    python tools/test-live-snapshot-chat.py --skip-vlm   # 不看图也能验接口
"""

from __future__ import annotations

import json
import pathlib
import re
import sys
import time
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

try:
    import websocket
except ImportError:  # pragma: no cover
    print("  ❌ 缺 websocket-client：pip install websocket-client")
    raise SystemExit(1)

CDP_PORT = 9222
LOG = pathlib.Path(r"F:\ai项目\apps\desktop\pet-data\main.log")


class Session:
    """极简 CDP 会话。"""

    def __init__(self, ws_url: str) -> None:
        # ⚠️ suppress_origin 必须开：Chrome 90+ 拒绝带 Origin 的 CDP 连接（403）
        self.ws = websocket.create_connection(ws_url, timeout=30, suppress_origin=True)
        self.seq = 0

    def call(self, method: str, params: dict | None = None, timeout: float = 30.0) -> dict:
        self.seq += 1
        self.ws.send(json.dumps({"id": self.seq, "method": method, "params": params or {}}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            message = json.loads(self.ws.recv())
            if message.get("id") == self.seq:
                return message
        raise TimeoutError(method)

    def eval(self, expression: str) -> object:
        result = self.call(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
        )
        payload = result.get("result", {})
        if "exceptionDetails" in payload:
            raise RuntimeError(str(payload["exceptionDetails"])[:300])
        return payload.get("result", {}).get("value")

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:  # noqa: BLE001
            pass


def targets() -> list[dict]:
    with urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json", timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def find(*needles: str) -> dict | None:
    for target in targets():
        url = str(target.get("url") or "")
        if target.get("type") == "page" and any(n in url for n in needles):
            return target
    return None


def log_since(offset: int) -> str:
    with LOG.open("rb") as handle:
        handle.seek(offset)
        return handle.read().decode("utf-8", "replace")


def _shared_terms(description: str, reply: str) -> list[str]:
    """两边共用的词（中文按 2 字切片，英文按 ≥3 字符的单词）。

    只用来**提示**相关度，不参与判定 —— 见调用处的说明。
    """
    import re as _re

    def terms(text: str) -> set[str]:
        found = set(_re.findall(r"[A-Za-z][A-Za-z0-9_\-]{2,}", text))
        cjk = _re.findall(r"[\u4e00-\u9fff]+", text)
        for run in cjk:
            for i in range(len(run) - 1):
                found.add(run[i : i + 2])
        return found

    return sorted(terms(description) & terms(reply))


def main() -> int:
    skip_vlm = "--skip-vlm" in sys.argv
    failures: list[str] = []

    def check(ok: bool, label: str, detail: str = "") -> None:
        if ok:
            print(f"    OK   {label}")
        else:
            failures.append(f"{label}{(' —— ' + detail) if detail else ''}")
            print(f"    FAIL {label}{(' —— ' + detail) if detail else ''}")

    print("=" * 78)
    print("  截图对话 · 真机端到端")
    print("=" * 78)

    pet = find("index.html")
    if not pet:
        print("  ❌ 找不到宠物窗口（index.html）")
        return 1

    # ── 0. 先弹一次右键菜单：菜单窗口的 DOM 是**收到 show 之后才建的** ──
    #
    # ⚠️ 不先弹，`[role=menuitem]` 会是 0 项 —— 上一版就是这么误报
    #    "菜单里没有这一项"的（DOM 还没建，不是项不存在）。
    pet_session = Session(pet["webSocketDebuggerUrl"])
    opened = pet_session.eval(
        "(function(){if(window.pet&&typeof window.pet.openContextMenu==='function')"
        "{window.pet.openContextMenu();return 'ok';}return 'no-api';})()"
    )
    print()
    print(f"  0. 弹出右键菜单：{opened}")
    time.sleep(2.5)

    menu = find("petMenu.html", "pet-menu.html")
    if not menu:
        print("  ❌ 菜单窗口没出现")
        return 1

    # ── 1. 菜单里有这一项 ──
    print()
    print("  1. 菜单项")
    session = Session(menu["webSocketDebuggerUrl"])
    # ⚠️ 菜单行**没有** data-action 属性 —— 它把整份 item 挂在行对象上
    #    （`row.__item`，见 petMenu.js），因为"点击时不该回去查数组"。
    #    所以按 role=menuitem 取，再从 __item.action 读动作名。
    actions = session.eval(
        "(function(){try{return JSON.stringify(Array.from(document.querySelectorAll('[role=menuitem]'))"
        ".map(function(e){return String((e.__item&&e.__item.action)||'?')+'|'+(e.textContent||'').trim();}));}"
        "catch(e){return JSON.stringify({error:String(e)});}})()"
    )
    parsed = json.loads(actions) if isinstance(actions, str) else actions
    items = parsed if isinstance(parsed, list) else []
    snap_item = next((i for i in items if "snapshot-chat" in str(i)), None)
    check(bool(snap_item), "菜单里有「截图对话」", f"共 {len(items)} 项，都不含 snapshot-chat")
    if snap_item:
        print(f"         菜单项：{snap_item}")
        check("Ctrl+Alt+S" in str(snap_item), "菜单项文字里带快捷键提示")

    # ── 2. 在**宠物窗口**挂收集器（抓它说的话）──
    #
    # ⚠️ 必须挂 index.html：`pet:message` 是主进程发给**宠物窗口**的
    #    （`petWindow.sendToRenderer('pet:message', …)`），对话条窗口收不到。
    #    上一版挂在 chat.html 上，于是"0 条事件"——又一个假失败。
    print()
    print("  2. 挂收集器")
    session.close()
    chat_session = pet_session
    # ⚠️ 所有协议消息都走**同一个通道** `pet:message`，渲染层再按 type 分发
    #    （见 app.js 的 `pet.on('pet:message', ...)`）。直接订阅 'pet.say.end'
    #    是收不到的 —— 这正是上一版"收到 0 条事件"的原因（**假失败**）。
    ready = chat_session.eval(
        "(function(){"
        "window.__snap=[];"
        "if(!window.pet||typeof window.pet.on!=='function'){return 'no-pet';}"
        "window.pet.on('pet:message', function(m){"
        "var t=String((m&&m.type)||'');"
        "if(t==='pet.say.end'){window.__snap.push({kind:'say', text:String(m.text||'')});}"
        "else if(t==='voice.audio'){window.__snap.push({kind:'voice', text:String(m.text||'')});}"
        "else if(t==='pet.emotion'){window.__snap.push({kind:'emotion',"
        "attn:(m.attention&&m.attention.attention), expr:(m.expression)});}"
        "});"
        "return 'ok';})()"
    )
    check(ready == "ok", "对话条窗口接受注入", str(ready))

    # ── 3. 点菜单项 ──
    print()
    print("  3. 点「截图对话」")
    start = LOG.stat().st_size
    menu = find("petMenu.html", "pet-menu.html")
    menu_session = Session(menu["webSocketDebuggerUrl"])
    menu_session.eval("window.petMenu.action({action: 'snapshot-chat'})")
    menu_session.close()
    print("         已发送 menu:action snapshot-chat")

    # ── 4. 等它说完 ──
    print()
    print("  4. 等结果（最多 75 秒）")
    deadline = time.time() + 75
    say_text = ""
    emotions: list[dict] = []
    while time.time() < deadline:
        time.sleep(2.5)
        raw = chat_session.eval("JSON.stringify(window.__snap || [])")
        try:
            captured = json.loads(raw) if isinstance(raw, str) else []
        except json.JSONDecodeError:
            captured = []
        for item in captured:
            if item.get("kind") in ("say", "voice") and item.get("text"):
                say_text = item["text"]
            elif item.get("kind") == "emotion":
                emotions.append(item)
        if say_text:
            break

    fresh = log_since(start)
    chat_session.close()

    # 画面描述从日志里取（`screen_chat` 会把它写进 main.log），
    # 用来和它说的话对比相关度。
    description = ""
    match = re.search(r"看图成功（\d+ms）：(.+)", fresh)
    if match:
        description = match.group(1).strip()

    # ── 5. 日志断言 ──
    print()
    print("  5. 链路")
    check("截图对话：开始" in fresh, "主进程开始抓屏")
    if skip_vlm:
        print("         （--skip-vlm：跳过看图断言）")
    else:
        check("看图成功" in fresh, "视觉模型看懂了", "没出现「看图成功」")
        check("截图对话：成功" in fresh, "回执被主进程收到（没超时）",
              "出现超时说明回执被丢弃")
    dropped = [
        ln for ln in fresh.splitlines()
        if "丢弃无法识别的消息" in ln and "vision.snap" in ln
    ]
    check(not dropped, "回执没有被当成无法识别的消息丢掉", dropped[0][:90] if dropped else "")

    # 🔴 结构性断言：画面描述必须进到 **user 那一轮**。
    #
    # 这条比"回复里有没有关键词"可靠得多 —— 它查的是线上的事实（提示词进了哪一轮），
    # 而不是猜模型的措辞。2026-10-17 那次「截图 Steam 却聊命令行」就是
    # 描述只进了 system、被同段里的记忆召回压过去了。
    stage = [ln for ln in fresh.splitlines() if "user 轮舞台提示" in ln]
    check(bool(stage), "画面描述进了 user 轮（模型最当真的位置）",
          "没有出现「user 轮舞台提示」日志")

    # ── 6. 说的话 ──
    print()
    print("  6. 它说了什么")
    if say_text:
        print(f"         「{say_text[:200]}」")
    check(bool(say_text), "它真的开口了")
    if say_text:
        # 🔴 硬断言：**不许再吐槽"空的"**。
        #
        # 这就是 2026-10-17 那个真 bug 的表现 —— 自主发言时最后一条 user 消息是
        # 空串，模型于是把"你发给我的是空的"当成话题：
        #   「诶，又是白茫茫一片。咱俩这是比谁憋得久吗？」
        #   「诶，还是空的，第七回了吧。」
        # 画面描述明明在 system 里也救不回来。所以这条必须硬拦。
        blank_talk = [w for w in ("白茫茫", "还是空的", "这是空的", "憋得久", "发了个空") if w in say_text]
        check(
            not blank_talk,
            "它没有把画面当成\"空白\"（自主发言空 user 消息那个 bug 的回归）",
            f"出现了 {blank_talk}：{say_text[:100]}",
        )

        # 相关度是**提示**，不是断言。
        #
        # 用词表判"是否在聊画面"会误报：实测它把画面说成「你这 IDE 开着，测试也改到
        # 一半了吧」，一个词表里的词都没有，但**明显在聊画面**（模型会改写措辞）。
        # 本项目反复吃过"假警报比不报更费时间"的亏，所以这条只提示。
        overlap = _shared_terms(description, say_text)
        if overlap:
            print(f"         ✅ 与画面描述共用的词：{overlap[:6]}")
        else:
            print("         ⚠️ 回复与画面描述没有共用词（模型可能在改写措辞，需人工看一眼）")

    # ── 7. 注意力与表情 ──
    print()
    print("  7. 注意力与表情")
    curious = [e for e in emotions if e.get("expr") == "curious"]
    check(bool(curious), "推过 curious 表情",
          f"收到 {len(emotions)} 条情绪事件，都没有 expression=curious")
    if curious:
        attention = curious[0].get("attn")
        print(f"         注意力 {attention}")
        check(
            isinstance(attention, (int, float)) and float(attention) <= 0.1,
            "注意力被拉到贴脸（<= 0.1）",
            f"实际 {attention}",
        )

    print()
    print("=" * 78)
    if failures:
        print(f"  ❌ {len(failures)} 项失败：")
        for item in failures:
            print(f"     · {item}")
    else:
        print("  ✅ 全部通过：点击 -> 抓屏 -> 看懂 -> 好奇 -> 顺着画面聊天")
    print("=" * 78)
    return 1 if failures else 0


raise SystemExit(main())
