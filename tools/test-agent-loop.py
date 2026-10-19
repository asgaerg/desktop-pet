"""agent loop 端到端测试（2026-10-12）。

## 为什么用**假 brain** 而不是真模型

真模型的行为**不确定**（它可能这次调用工具、下次不调），拿它测"循环逻辑对不对"
会得到**时红时绿**的结果 —— 那种测试没人信，最后会被忽略掉。

所以这里用一个**脚本化的假 brain**：它按预设的剧本吐事件
（第一轮：说"我查一下" + 要求调用工具；第二轮：说结果）。
这样每一步都可断言，而且毫秒级跑完、不花钱。

## 真模型的验证放在别处

`tools/test-agent-loop-live.py` 用真模型跑一遍 —— 那个验的是
"模型愿不愿意调、参数给得对不对"，不是循环逻辑。
"""

from __future__ import annotations

import asyncio
import shutil
import sys
import pathlib

sys.path.insert(0, ".")
from tool_bootstrap import ROOT, setup  # noqa: E402

setup(verbose=False)

from pet.bus import Bus  # noqa: E402
from pet.brains.base import Brain, LLMRequest  # noqa: E402
from pet.config import load_config  # noqa: E402
from pet.gateway import MAX_TOOL_ROUNDS, Conversation  # noqa: E402

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


class ScriptedBrain(Brain):
    """按剧本吐事件的假 brain。

    `script` 是每轮的 `(要说的话, 工具调用列表)`。工具调用为 `None` 表示
    这一轮不调用工具（循环应当就此结束）。

    ⚠️ 它记录每一轮收到的 `req`，测试据此断言"回填有没有真的送进去"。

    ## `truncate_below`（2026-10-12 加）

    模拟"思考模式把 `max_tokens` 吃光"：**`max_tokens` 小于这个阈值时，
    只吐 `finish=length` 和一个字都不吐**。

    这是用户报的「我说了话，但是它没有回答」的成因，所以必须能测 ——
    没有它的话，"重试"那一段代码一行都覆盖不到。
    """

    name = "scripted"

    def __init__(
        self,
        script: list[tuple[str, list[dict] | None]],
        truncate_below: int = 0,
    ) -> None:
        self.script = script
        self.requests: list[LLMRequest] = []
        self.round = 0
        self.truncate_below = truncate_below
        # 被截断了几次（测试断言"确实重试过"）
        self.truncated = 0

    async def stream(self, req: LLMRequest):  # type: ignore[override]
        self.requests.append(req)
        index = min(self.round, len(self.script) - 1)
        text, calls = self.script[index]
        self.round += 1

        # 预算太小 -> 只出 reasoning，正文一个字都没有（真实行为）
        if self.truncate_below and int(req.max_tokens) < self.truncate_below:
            self.truncated += 1
            yield {"kind": "finish", "finish_reason": "length"}
            yield {"kind": "done", "text": ""}
            return

        if text:
            yield {"kind": "delta", "text": text}
        if calls:
            yield {"kind": "tool_calls", "calls": calls}
        yield {"kind": "finish", "finish_reason": "stop"}
        yield {"kind": "done", "text": text}


async def run_round(convo: Conversation, text: str, bus: Bus) -> list[dict]:
    """跑一轮对话，收集 bus 上的事件。

    ⚠️ 用 `*` 订阅**全部**事件，而不是列一串话题名 —— 我第一版列了一串，
    其中 `pet.thinking` **不在** `KNOWN_TOPICS` 里（于是被警告），
    而且漏列的任何一个话题都会让断言**静默**失效（看起来像"没发"）。

    ⚠️ `Event` 的字段是 **`name` / `payload`**，不是 `topic` / `data`（我猜错过一次）。
    """
    events: list[dict] = []

    async def collector(event) -> None:
        events.append({"name": event.name, "payload": event.payload or {}})

    unsubscribe = bus.subscribe("*", collector)
    try:
        await convo._generate(text)
    finally:
        unsubscribe()
    return events


def say_text(events: list[dict]) -> str:
    return "".join(
        str(e["payload"].get("text") or "") for e in events if e["name"] == "pet.say.delta"
    )


async def main() -> int:
    states = ROOT / "apps" / "desktop" / "pet-data" / "_loop-test"
    if states.exists():
        shutil.rmtree(states, ignore_errors=True)
    states.mkdir(parents=True, exist_ok=True)

    config_path = ROOT / "config" / "persona.yaml"
    cfg = load_config(config_path)
    # 打开工具 + 联网（沙箱指到测试目录）
    cfg.root.setdefault("tools", {})["enabled"] = True
    cfg.root.setdefault("tools", {})["web_search"] = False  # 不发真请求
    cfg.root.setdefault("web", {})["state_dir"] = str(states)

    print("  ═══ ① 不调用工具时：一轮就结束 ═══")
    if True:   # 原为 C 风格花括号（Python 不支持）
        bus = Bus()
        brain = ScriptedBrain([("普通回答", None)])
        convo = Conversation(bus, cfg, brain=brain)
        events = await run_round(convo, "你好", bus)

        check("只生成了一轮", brain.round == 1, f"实际 {brain.round} 轮")
        check("回复文本正确", say_text(events) == "普通回答", f"实际 {say_text(events)!r}")
        check("发了 pet.say.end", any(e["name"] == "pet.say.end" for e in events))
        check("发了 conversation.finished", any(e["name"] == "conversation.finished" for e in events))
        check("请求里带了 tools（沙箱工具）", bool(brain.requests[0].tools),
              f"tools={brain.requests[0].tools}")

    print()
    print("  ═══ ② 🔁 调用工具时：两轮，且结果回填 ═══")
    if True:   # 原为 C 风格花括号（Python 不支持）
        bus = Bus()
        brain = ScriptedBrain([
            ("我先记一下。", [
                {"index": 0, "id": "call_1", "name": "sandbox_write",
                 "arguments": '{"name": "todo.md", "content": "买牛奶"}'}
            ]),
            ("记好了。", None),
        ])
        convo = Conversation(bus, cfg, brain=brain)
        events = await run_round(convo, "帮我记一下买牛奶", bus)

        check("跑了两轮", brain.round == 2, f"实际 {brain.round} 轮")
        check("两轮的话都留下了（跨轮累积）",
              say_text(events) == "我先记一下。记好了。", f"实际 {say_text(events)!r}")
        check("第二轮带了 tool_results", bool(brain.requests[1].tool_results),
              "没回填的话模型看不到结果，只能瞎猜")

        results = brain.requests[1].tool_results or []
        check("回填了 1 条", len(results) == 1, f"实际 {len(results)}")
        if results:
            # ⚠️ 2026-10-12 起是 ToolExchange 数据类，不是四元组
            #    （加了 reasoning —— 思考模式下不带回去就是 HTTP 400）
            call_id, name = results[0].call_id, results[0].name
            arguments, content = results[0].arguments, results[0].content
            check("call_id 对上", call_id == "call_1", f"实际 {call_id!r}")
            check("工具名对上", name == "sandbox_write", f"实际 {name!r}")
            check("arguments 原样回填", "todo.md" in arguments, f"实际 {arguments!r}")
            check("结果文本是「已写入」", "已写入" in content, f"实际 {content!r}")
        check("文件真的写出来了", (states / "web-sandbox" / "todo.md").exists(),
              f"目录内容：{list(states.rglob('*'))}")

    print()
    print("  ═══ ③ 并发调用：一次两个工具 ═══")
    if True:   # 原为 C 风格花括号（Python 不支持）
        bus = Bus()
        brain = ScriptedBrain([
            ("", [
                {"index": 0, "id": "c_a", "name": "sandbox_write",
                 "arguments": '{"name": "a.md", "content": "A"}'},
                {"index": 1, "id": "c_b", "name": "sandbox_write",
                 "arguments": '{"name": "b.md", "content": "B"}'},
            ]),
            ("两个都写好了。", None),
        ])
        convo = Conversation(bus, cfg, brain=brain)
        await run_round(convo, "写两个文件", bus)

        results = brain.requests[1].tool_results or []
        check("两个调用都执行了", len(results) == 2, f"实际 {len(results)}")
        check("结果按 index 顺序（c_a 在前）",
              [r.call_id for r in results] == ["c_a", "c_b"], f"实际 {[r.call_id for r in results]}")
        check("a.md 写了", (states / "web-sandbox" / "a.md").exists())
        check("b.md 写了", (states / "web-sandbox" / "b.md").exists())

    print()
    print("  ═══ ④ 🔴 轮数上限（防止无限循环）═══")
    if True:   # 原为 C 风格花括号（Python 不支持）
        bus = Bus()
        # 每一轮都要求调用 —— 没有上限就是无限循环
        always = [{"index": 0, "id": "call_x", "name": "sandbox_read",
                   "arguments": '{"name": "从来没有.md"}'}]
        brain = ScriptedBrain([("再试一次。", always)] * 20)
        convo = Conversation(bus, cfg, brain=brain)
        await run_round(convo, "反复调用", bus)

        check(f"轮数被限制在 {MAX_TOOL_ROUNDS + 2} 以内",
              brain.round <= MAX_TOOL_ROUNDS + 2, f"实际跑了 {brain.round} 轮")
        check("确实跑了不止一轮（说明循环在工作）", brain.round >= 2, f"实际 {brain.round}")
        """
        ⚠️ 上限是 `MAX_TOOL_ROUNDS + 2` 而不是 `+ 1`（2026-10-12 改的）。

        多出来那一轮是**强制回答**：轮数用尽时 gateway 会把 `req.tools`
        清空再跑一次，逼模型用已查到的资料回答（见 `_force_answer`）。
        没有它的话用户看到的是**沉默** —— 那正是用户报的「它没有回应」。

        ⚠️ 这条断言当时**变红过**，而我停下来确认了新行为是不是有意的
        （是）才改的数字。**断言变红时不要条件反射地改断言** ——
        它可能正在告诉你"行为变了"。
        """
        check("最后一轮是**没有工具的强制回答**（清空了 tools）",
              any(r.tools is None for r in brain.requests),
              f"各轮 tools：{['有' if r.tools else '无' for r in brain.requests]}")

    print()
    print("  ═══ ⑤ 工具失败时**仍然回填**（不能漏一条）═══")
    if True:   # 原为 C 风格花括号（Python 不支持）
        bus = Bus()
        brain = ScriptedBrain([
            ("", [
                {"index": 0, "id": "c_ok", "name": "sandbox_read",
                 "arguments": '{"name": "没有.md"}'},
                {"index": 1, "id": "c_bad", "name": "根本没有这个工具",
                 "arguments": "{}"},
                {"index": 2, "id": "c_json", "name": "sandbox_read",
                 "arguments": "这不是 JSON"},
            ]),
            ("好了。", None),
        ])
        convo = Conversation(bus, cfg, brain=brain)
        await run_round(convo, "试试失败", bus)

        results = brain.requests[1].tool_results or []
        check("三个调用都有结果（一条都不能少）", len(results) == 3, f"实际 {len(results)}")
        check("id 全部对上",
              [r.call_id for r in results] == ["c_ok", "c_bad", "c_json"],
              f"实际 {[r.call_id for r in results]}")
        check("未知工具返回了原因", "没有名为" in results[1].content, f"实际 {results[1].content!r}")
        check("坏 JSON 返回了原因", "JSON" in results[2].content, f"实际 {results[2].content!r}")

    print()
    print("  ═══ ⑥ 退出码回归：收尾事件一件都不能少 ═══")
    if True:   # 原为 C 风格花括号（Python 不支持）
        bus = Bus()
        brain = ScriptedBrain([
            ("查一下。", [{"index": 0, "id": "c1", "name": "sandbox_write",
                          "arguments": '{"name": "z.md", "content": "z"}'}]),
            ("查到了。", None),
        ])
        convo = Conversation(bus, cfg, brain=brain)
        events = await run_round(convo, "帮我查", bus)
        topics = [e["name"] for e in events]

        for needed in ("pet.thinking", "pet.say.end", "conversation.finished"):
            check(f"发了 {needed}", needed in topics, f"实际话题：{sorted(set(topics))}")
        end = next((e for e in events if e["name"] == "pet.say.end"), None)
        end_text = (end or {}).get("payload", {}).get("text")
        check("pet.say.end 带完整文本（含两轮）",
              end_text == "查一下。查到了。",
              f"实际 {end_text!r}")
        check("回复进了 history", any(t.role == "assistant" for t in convo.history),
              f"history = {[(t.role, t.text[:20]) for t in convo.history]}")

    print()
    print("  ═══ ⑦ 🔴 思考模式吃光预算 -> 加大重试（用户报的「没有回答」）═══")
    if True:   # 原为 C 风格花括号（Python 不支持）
        bus = Bus()
        # 第一轮正常要求调用工具；第二轮撞上截断（max_tokens 太小）；
        # 重试时预算被加大，于是正常出正文
        brain = ScriptedBrain(
            [
                ("我查查。", [
                    {"index": 0, "id": "c1", "name": "sandbox_write",
                     "arguments": '{"name": "t.md", "content": "x"}'}
                ]),
                ("查到了，是这样……", None),
            ],
            truncate_below=1000,  # 预算 < 1000 就"推理吃光"
        )
        convo = Conversation(bus, cfg, brain=brain)
        events = await run_round(convo, "帮我查一下", bus)

        check("确实发生了截断（否则这条测试是空转）", brain.truncated >= 1,
              f"truncated={brain.truncated}")
        check("**加大预算重试了**（否则用户看到空回复）",
              any(int(r.max_tokens) >= 1000 for r in brain.requests),
              f"各轮 max_tokens：{[r.max_tokens for r in brain.requests]}")
        check("最终拿到了正文", len(say_text(events).strip()) > 0,
              f"实际 {say_text(events)!r}")
        end = next((e for e in events if e["name"] == "pet.say.end"), None)
        check("say.end 的 reason 不是 truncated（已救回来）",
              (end or {}).get("payload", {}).get("reason") == "done",
              f"实际 {(end or {}).get('payload', {}).get('reason')!r}")

        # 极端情况：重试也没救回来 —— 不能假装有内容
        bus2 = Bus()
        brain2 = ScriptedBrain([("x", None)], truncate_below=99999)
        convo2 = Conversation(bus2, cfg, brain=brain2)
        events2 = await run_round(convo2, "再试", bus2)
        check("重试也没救回来时仍是空正文（不假装有内容）",
              say_text(events2).strip() == "", f"实际 {say_text(events2)!r}")
        check("每轮最多重试一次（没有无限重试）", brain2.round <= 2,
              f"实际跑了 {brain2.round} 轮")

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
