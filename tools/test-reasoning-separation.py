"""实测：思考模式/正文的分流 + 记忆提炼能不能真出结果（2026-10-12）。

## 验两件事

### ① 思考文字**不会**混进正文（用户问的）

用户问：「api 回复会不会显示思考模式与正文的分别，如果有的话，
可以将正文单独提取出来，或者将所有思考的文字略过，只输出正文」。

代码上是分开的（`delta.content` vs `delta.reasoning_content`），
但**"代码上分开"和"实际没漏"是两件事** —— 所以这里真跑一遍，
断言每一条 `delta` 事件里都**不含**推理文字的特征。

### ② 关掉思考后，记忆提炼真的能出结果

`FactExtractor` 原来是 `max_tokens=200` + **思考模式**。
实测同一个坑：`reasoning_tokens: 200` 把预算吃光，正文 0 字 ——
于是**抽出的事实永远是空的**，而失败只写进 DEBUG 日志。
表现是「聊了很久，它什么都不记得」。

这个测试用真模型抽一次事实，断言**真的抽到了东西**。
"""

from __future__ import annotations

import asyncio
import sys

sys.path.insert(0, ".")
from tool_bootstrap import ROOT, setup  # noqa: E402

setup(verbose=False)

import logging  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="  [%(name)s] %(message)s")

from pet.brains import LLMRequest, build_brain  # noqa: E402
from pet.brains.base import EmotionState  # noqa: E402
from pet.config import load_config  # noqa: E402
from pet.memory import FactExtractor  # noqa: E402

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


cfg = load_config(ROOT / "config" / "persona.yaml")
brain = build_brain(cfg, EmotionState())


async def main() -> int:
    print("  ═══ ① 思考文字有没有混进正文 ═══")
    req = LLMRequest(
        system="你是一只桌宠，回答简短。",
        history=[],
        user_text="帮我算一下 17 乘以 24 等于多少，然后解释一下怎么算的",
        temperature=0.3,
        max_tokens=1200,
        timeout_s=90.0,
        # ⚠️ 显式开思考 —— 这才是"推理和正文可能混在一起"的场景
        thinking=True,
    )

    deltas: list[str] = []
    tool_events: list[dict] = []
    finishes: list[str] = []
    async for item in brain.stream(req):
        kind = item.get("kind")
        if kind == "delta":
            deltas.append(str(item.get("text") or ""))
        elif kind == "tool_calls":
            tool_events.append(item)
        elif kind == "finish":
            finishes.append(str(item.get("finish_reason") or ""))
        elif kind == "error":
            print(f"    ❌ 出错：{item.get('message')}")
            return 1

    body = "".join(deltas)
    print(f"    正文 {len(body)} 字：{body[:160]!r}")
    print(f"    事件：delta {len(deltas)} 个 | finish={finishes}")

    check("有正文", len(body.strip()) > 0, f"实际 {body[:80]!r}")
    check("正文就是答案（含 408）", "408" in body, f"实际 {body[:160]!r}")

    # ⚠️ 关键断言：推理文字**不能**出现在 delta 里。
    #    推理是英文思考（实测："The user wants to know..."），
    #    所以用它特有的措辞当特征。
    leak_markers = [
        "The user wants",
        "Let me",
        "I need to",
        "We need to",
        "reasoning",
        "思考过程",
        "思维链",
    ]
    leaked = [m for m in leak_markers if m in body]
    check(
        "正文里**没有**推理文字的特征",
        not leaked,
        f"漏进来了：{leaked} —— 正文={body[:200]!r}",
    )

    # delta 事件里也不能有
    for i, piece in enumerate(deltas):
        for m in leak_markers:
            if m in piece:
                check(f"第 {i} 个 delta 不含推理文字", False, f"含 {m!r}：{piece[:80]!r}")
                break
        else:
            continue
        break
    else:
        check("**每一个** delta 分片都只含正文", True)

    print()
    print("  ═══ ② 关掉思考后，记忆提炼真的出结果 ═══")
    # ⚠️ 签名是 `FactExtractor(brain, config: MemoryConfig, app_config=None)` ——
    #    第一个是 brain，不是 Config（我第一版传反了）
    from pet.memory import MemoryConfig  # noqa: E402

    mem_cfg = MemoryConfig(enabled=True)
    extractor = FactExtractor(brain, mem_cfg, cfg)
    turns = [
        ("user", "我养了一只叫豆豆的橘猫，它特别喜欢吃三文鱼"),
        ("assistant", "豆豆这名字好可爱～橘猫都爱吃鱼吗"),
        ("user", "对了我在字节跳动做后端，主要写 Go"),
    ]
    facts = await extractor.extract(turns, max_facts=3)
    print(f"    抽出 {len(facts)} 条：")
    for f in facts:
        print(f"      · {f}")

    check("**真的抽到了事实**（关思考之前这里永远是空的）", len(facts) > 0,
          "一条都没抽到 —— 说明思考还在吃预算")
    joined = " ".join(facts)
    check("抽到的事实里有专有名词（否则将来检索不到）",
          any(k in joined for k in ("豆豆", "橘猫", "字节", "Go", "三文鱼")),
          f"实际 {facts}")
    check("提炼没有报失败", extractor.stats()["failures"] == 0,
          f"stats={extractor.stats()}")

    print()
    print(f"  ═══ 结果：{passed}/{passed + len(failed)} 通过 ═══")
    if failed:
        for item in failed:
            print(f"    ❌ {item}")
        return 1
    print("  ✅ 全部通过")
    return 0


raise SystemExit(asyncio.run(main()))
