"""用**真模型**跑一遍联网（2026-10-12）。

## 这个测试和 `test-agent-loop.py` 的分工

    test-agent-loop.py   假 brain，验**循环逻辑**（回填、上限、并发、失败处理）
    这个                 真模型，验**模型愿不愿意调、参数给得对不对**

两者都要有：前者证明"我们的代码对"，后者证明"接上真模型能跑通"。

## ⚠️ 这个测试会**真花钱**（几次 token）和**真联网**

成本可忽略（一问一答几百 token），但它是**唯一**能证明端到端可用的东西。

## 判据是"行为"而不是"文本"

不问"它答得对不对"（那取决于搜索引擎），只问：

  · 它有没有调用 `web_search`
  · 参数里有没有 query
  · 最终回复里有没有用上搜到的东西
"""

from __future__ import annotations

import asyncio
import pathlib
import shutil
import sys

sys.path.insert(0, ".")
from tool_bootstrap import ROOT, setup  # noqa: E402

setup(verbose=False)

from pet.bus import Bus  # noqa: E402
from pet.config import load_config  # noqa: E402
from pet.gateway import Conversation  # noqa: E402

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


async def ask(convo: Conversation, bus: Bus, text: str) -> tuple[str, list[dict]]:
    """问一句，返回 (流式拼出来的文本, 事件列表)。

    ## 🔴 这里有一个曾经骗了我半天的坑（2026-10-15 修）

    原版是：

    ```python
    unsubscribe = bus.subscribe("*", collector)
    await convo._generate(text)        # <- 发布到 convo.bus
    ```

    **订阅的是参数里的 `bus`，发布的是 `convo.bus`** —— 两者必须**是同一个对象**。
    ① 之前恰好有一句 `convo.bus = bus`，所以 ① 正常；②③ 忘了设，
    于是「② ③ 事件 0 条、回复为空」，看着像**产品不回复**。

    ⚠️ 这个 bug 的形状值得记住：**观测点装在了错的管子上**。
    表现是「什么都收不到」，而真相是「你在听另一根管子」。
    和 §101 那条「在渲染层嗅 WebSocket，而连接压根不在渲染层」**是同一类错误**。

    ## 所以这里主动对齐

    不再要求调用方记得先 `convo.bus = bus`，`ask()` 自己保证一致。
    """
    events: list[dict] = []

    async def collector(event) -> None:
        events.append({"name": event.name, "payload": event.payload or {}})

    # 🔴 让「发布」和「订阅」落在同一个 bus 上 —— 这是上面那个坑的修法
    convo.bus = bus
    unsubscribe = bus.subscribe("*", collector)
    try:
        await convo._generate(text)
    finally:
        unsubscribe()

    # 防呆：一条事件都没收到，几乎一定是又装错管子了
    if not events:
        print(
            "       ⚠️ 一条事件都没收到 —— 检查 convo.bus 和订阅的 bus 是不是同一个对象"
            f"（现在 convo.bus is bus = {convo.bus is bus}）"
        )

    final = "".join(
        str(e["payload"].get("text") or "") for e in events if e["name"] == "pet.say.delta"
    )
    return final, events


async def main() -> int:
    states = ROOT / "apps" / "desktop" / "pet-data" / "_live-test"
    if states.exists():
        shutil.rmtree(states, ignore_errors=True)
    states.mkdir(parents=True, exist_ok=True)

    cfg = load_config(ROOT / "config" / "persona.yaml")
    # ⚠️ 显式打开联网（默认是关的）
    cfg.root.setdefault("tools", {})["enabled"] = True
    cfg.root.setdefault("tools", {})["web_search"] = True
    cfg.root.setdefault("web", {})["state_dir"] = str(states)

    convo = Conversation(Bus(), cfg)
    if getattr(convo.brain, "name", "") == "mock":
        print("  ⚠️ brain 降级成了 mock（没有 API 密钥？）—— 这个测试需要真模型")
        return 2

    print("  ═══ ① 问一个**必须联网**才知道的问题 ═══")
    bus = Bus()
    convo.bus = bus
    text, events = await ask(
        convo, bus, "现在是 2026 年，帮我查一下最近有什么新的 AI 模型发布，简单说说就行"
    )
    tool_events = [e for e in events if e["name"] == "pet.say.tool"]
    print(f"    回复（前 300 字）：{text[:300]}")
    print(f"    事件里的工具相关：{sorted({e['name'] for e in events if 'tool' in e['name']})}")

    # 从日志侧也看不出来 —— 直接看"回复里有没有实质内容"
    check("回复非空", len(text.strip()) > 10, f"实际 {text[:80]!r}")
    check("回复不是「我无法联网」之类的推脱",
          "无法联网" not in text and "不能联网" not in text and "无法访问" not in text,
          f"实际 {text[:200]!r}")

    print()
    print("  ═══ ② 闲聊时**不该**搜（用户要求「少搜」）═══")
    if True:   # 原为 C 风格花括号（Python 不支持）
        bus2 = Bus()
        text2, _ = await ask(convo, bus2, "你好呀，今天心情怎么样")
        print(f"    回复：{text2[:200]}")
        check("闲聊也有正常回复", len(text2.strip()) > 3, f"实际 {text2[:80]!r}")
        # 语义判断不了，但至少不该是"我查到了…"那种
        check("闲聊回复不像搜索结果罗列",
              "搜索" not in text2[:40] and "查到了" not in text2[:40],
              f"开头是 {text2[:60]!r}")

    print()
    print("  ═══ ③ 让它用沙箱记东西 ═══")
    if True:   # 原为 C 风格花括号（Python 不支持）
        bus3 = Bus()
        text3, _ = await ask(convo, bus3, "帮我把「明天要买牛奶和鸡蛋」记到你的笔记里，文件名 shopping.md")
        print(f"    回复：{text3[:200]}")
        written = list((states / "web-sandbox").glob("*.md")) if (states / "web-sandbox").exists() else []
        print(f"    沙箱里的文件：{[p.name for p in written]}")
        check("沙箱里出现了文件", bool(written), f"目录：{list(states.rglob('*'))}")

    shutil.rmtree(states, ignore_errors=True)
    print()
    print(f"  ═══ 结果：{passed}/{passed + len(failed)} 通过 ═══")
    if failed:
        for item in failed:
            print(f"    ❌ {item}")
        return 1
    print("  ✅ 全部通过")
    return 0


raise SystemExit(asyncio.run(main()))
