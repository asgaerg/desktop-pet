"""验证「联网搜索」开关的**热更新**真的生效（2026-10-12）。

## 为什么单独验这一条

这是这个项目里**反复出现**的一类 bug：界面上勾打上了，实际没生效
（语音引擎、VAD 参数、edge 音色都栽过）。

而这次的机制是两段：

    主进程  setWebSearch()  -> writeDeepScalar 落盘 + send(config.update)
    sidecar _on_config_tools() -> 清掉 _tools 缓存 -> 下次生成时重建

**任何一段断了，表现都是"勾打上了但宠物不用工具"，而且不报错。**

## 怎么验（不依赖界面）

直接按 sidecar 的方式模拟：

  ① 造一个 config，`web_search: false` -> 断言工具清单里**没有** web_search
  ② `apply_patch({tools: {web_search: true}})` + 发 config.updated
  ③ 断言工具清单里**有** web_search（证明缓存被清了）
  ④ 再关回去，断言又没有了

第 ③ 步就是"热更新"本身 —— 不做的话 `_tools` 缓存还在，第 ③ 步会失败。
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


def tool_names(convo: Conversation) -> list[str]:
    return [t.name for t in convo._tool_list()]


async def main() -> int:
    states = ROOT / "apps" / "desktop" / "pet-data" / "_toggle-test"
    if states.exists():
        shutil.rmtree(states, ignore_errors=True)
    states.mkdir(parents=True, exist_ok=True)

    cfg = load_config(ROOT / "config" / "persona.yaml")
    cfg.root.setdefault("web", {})["state_dir"] = str(states)
    # ⚠️ 从"关"开始 —— 与配置文件里的默认值一致
    cfg.root.setdefault("tools", {})["enabled"] = True
    cfg.root["tools"]["web_search"] = False

    bus = Bus()
    convo = Conversation(bus, cfg)
    """
    ⚠️ **必须调 `attach()`** —— 订阅是在那里挂的，**不在 `__init__` 里**。

    第一版漏了它，于是 `config.updated` 根本没有订阅者，测试全红
    而代码其实是对的（我为此改了两次 gateway，都是白改）。

    生产路径由 `Gateway.__init__` 调 `self.conversation.attach()`，
    所以真实运行时没问题 —— 但**测试里必须自己调**，
    否则测的不是同一条路径。
    """
    convo.attach()

    print("  ═══ ① 关着的时候：没有联网工具 ═══")
    names = tool_names(convo)
    print(f"    工具：{names}")
    check("关着时**没有** web_search", "web_search" not in names, f"实际 {names}")
    check("关着时**没有** web_fetch", "web_fetch" not in names, f"实际 {names}")
    check("关着时沙箱工具仍然有（不受联网开关影响）",
          "sandbox_write" in names and "sandbox_read" in names, f"实际 {names}")
    check("_tool_schemas() 返回的清单里也没有",
          not any("web_search" in str(s) for s in (convo._tool_schemas() or [])),
          f"实际 {convo._tool_schemas()}")

    print()
    print("  ═══ ② 打开联网（走真实的热更新路径）═══")
    # ① 先确认缓存**确实被填过**（否则第 ③ 步证明不了什么）
    check("缓存已填充（_tools 不是 None）", convo._tools is not None,
          "没填过的话，下面清缓存那一步是空操作，测不出东西")

    # ② 按主进程的方式改配置
    effective = cfg.apply_patch({"tools": {"web_search": True}})
    print(f"    apply_patch 结果：{effective}")
    # ③ 按主进程的方式广播（这一步会触发 _on_config_tools）
    from pet.protocol import msg  # noqa: E402

    await bus.publish("config.updated", msg("config.updated", **effective) if isinstance(effective, dict) else {})

    names2 = tool_names(convo)
    print(f"    工具：{names2}")
    check("打开后**有** web_search", "web_search" in names2,
          f"实际 {names2} —— 缓存没清的话这里就会失败（假开关）")
    check("打开后**有** web_fetch", "web_fetch" in names2, f"实际 {names2}")

    print()
    print("  ═══ ③ 再关回去（两个方向都要能热更新）═══")
    cfg.apply_patch({"tools": {"web_search": False}})
    await bus.publish("config.updated", msg("config.updated"))
    names3 = tool_names(convo)
    print(f"    工具：{names3}")
    check("关回去后**又没有** web_search", "web_search" not in names3, f"实际 {names3}")

    print()
    print("  ═══ ④ 工具描述里必须写「少搜」（用户明确要求）═══")
    cfg.apply_patch({"tools": {"web_search": True}})
    await bus.publish("config.updated", msg("config.updated"))
    tool = next((t for t in convo._tool_list() if t.name == "web_search"), None)
    check("拿得到 web_search 工具", tool is not None)
    if tool:
        desc = tool.description
        print(f"    描述：{desc}")
        check("描述了**什么时候该用**", "需要" in desc or "查" in desc, desc[:80])
        check("描述了**不该滥用**（用户要求「少搜」）",
              "不要" in desc or "不该" in desc, f"没有约束语：{desc[:120]}")
        check("参数里 query 是必填",
              "query" in (tool.parameters.get("required") or []),
              f"实际 {tool.parameters}")

    print()
    print("  ═══ ⑤ 总开关也能关掉一切 ═══")
    cfg.apply_patch({"tools": {"enabled": False}})
    await bus.publish("config.updated", msg("config.updated"))
    names5 = tool_names(convo)
    check("关掉总开关后一个工具都没有", names5 == [], f"实际 {names5}")

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
