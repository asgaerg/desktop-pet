"""验证「强制回答」：模型陷入搜索循环时，用户还拿不拿得到回答（2026-10-12）。

## 背景

实测失败模式：模型**陷在"搜索模式"里出不来** —— 用户问一个它不确定的东西，
它一轮又一轮换词搜，宁可继续搜也不肯用已有的东西回答。撞 3 轮上限时
原来的代码只是 `break`，**用户什么都没看到**。

## 这个脚本做什么

跑**多个**查询（有的是真事、有的是编的），每次都统计：

  · 用了几轮工具
  · **有没有触发强制回答**
  · 最终**有没有正文**

判据是"**最终一定要有正文**" —— 那是用户唯一在意的事。

## ⚠️ 为什么跑多个查询

单跑一个会因为结果缓存 / 模型随机性而时好时坏（我已经被坑过一次：
诊断脚本跑出"没复现"，而用户那边确实复现了）。
多跑几个才能看出"强制回答"这条兜底是不是**真的兜住了**。
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import sys

sys.path.insert(0, ".")
from tool_bootstrap import ROOT, setup  # noqa: E402

setup(verbose=False)

logging.basicConfig(level=logging.WARNING, format="  [%(name)s] %(message)s")

from pet.bus import Bus  # noqa: E402
from pet.config import load_config  # noqa: E402
from pet.gateway import Conversation  # noqa: E402

QUERIES = [
    # 编造的游戏名（最可能触发"搜不到 -> 反复搜"）
    "新出的银松镇你知道吗，我看了视频才决定买的",
    # 另一个编造的
    "紫月山谷这个游戏怎么样，值得买吗",
    # 真事但需要联网
    "最近有什么新的显卡发布吗",
]

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


async def main() -> int:
    states = ROOT / "apps" / "desktop" / "pet-data" / "_force-test"
    if states.exists():
        shutil.rmtree(states, ignore_errors=True)
    states.mkdir(parents=True, exist_ok=True)

    cfg = load_config(ROOT / "config" / "persona.yaml")
    cfg.root.setdefault("tools", {})["enabled"] = True
    cfg.root["tools"]["web_search"] = True
    cfg.root.setdefault("web", {})["state_dir"] = str(states)

    for index, user_text in enumerate(QUERIES, 1):
        print()
        print(f"  ═══ 查询 {index}：{user_text} ═══")
        bus = Bus()
        convo = Conversation(bus, cfg)
        convo.attach()

        events: list[dict] = []

        async def collector(event) -> None:
            events.append({"name": event.name, "payload": event.payload or {}})

        bus.subscribe("*", collector)

        # 统计工具轮数
        tool_rounds = {"n": 0}
        original_execute = convo._execute_tool_calls

        async def counting_execute(calls, req, reasoning=""):
            tool_rounds["n"] += 1
            return await original_execute(calls, req, reasoning)

        convo._execute_tool_calls = counting_execute  # type: ignore[method-assign]

        # 统计有没有触发强制回答（它会把 tools 清空）
        forced = {"done": False}
        original_run = convo._run_tool_round

        async def watching_run(req, say_id, collected):
            if req.tools is None and not forced["done"]:
                forced["done"] = True
            return await original_run(req, say_id, collected)

        convo._run_tool_round = watching_run  # type: ignore[method-assign]

        await convo._generate(user_text)

        body = "".join(
            str(e["payload"].get("text") or "") for e in events if e["name"] == "pet.say.delta"
        )
        end = next((e for e in events if e["name"] == "pet.say.end"), None)
        errs = [e["payload"].get("message") for e in events if e["name"] == "system.error"]

        print(f"    工具轮 {tool_rounds['n']} 轮 · 触发强制回答={forced['done']} · "
              f"正文 {len(body)} 字")
        print(f"    正文：{body[:220]!r}")
        if errs:
            print(f"    ⚠️ system.error：{errs}")

        check(f"查询 {index}：**最终有正文**（用户不会看到沉默）",
              len(body.strip()) > 0,
              f"正文为空；工具轮={tool_rounds['n']} 强制回答={forced['done']}")
        check(f"查询 {index}：没有 system.error", not errs, f"{errs}")
        check(f"查询 {index}：say.end 的 reason 是 done",
              (end or {}).get("payload", {}).get("reason") == "done",
              f"实际 {(end or {}).get('payload', {}).get('reason')!r}")

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
