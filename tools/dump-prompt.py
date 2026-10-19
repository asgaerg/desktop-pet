"""
抓一次**真实**发给 AI 的请求体 —— 回答两个问题：
  ① 提示词是怎么组成的
  ② 有没有真的发过去

## 做法
**不 mock、不复刻字符串**：走产品代码
  · `Conversation.build_system_prompt()`  —— system 段
  · `MemoryService.inject_hint()`         —— 记忆段（读真实数据库 + 真实嵌入）
  · `OpenAIBrain._body()`                 —— 最终请求体
然后把 `_body()` 的产物**真的发一次**给 DeepSeek，把响应读回来。

⚠️ 会花一次极短请求的钱（几分钱）。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "apps" / "brain"))

# ⚠️ 必须先复刻 sidecar 的运行环境（cwd 给分词 DLL、注入凭据）。
#    不这么做会**静默降级**：嵌入器回落到 256 维哈希 -> 往真实库写一条错维向量
#    -> 下次应用启动触发全库重建。实测踩过，见 tool_bootstrap.py 的说明。
sys.path.insert(0, str(Path(__file__).resolve().parent))
from tool_bootstrap import setup  # noqa: E402

setup()

from pet.brains.base import LLMRequest  # noqa: E402
from pet.brains.openai_compat import OpenAIBrain  # noqa: E402
from pet.config import load_config  # noqa: E402
from pet.gateway import Conversation  # noqa: E402


def hr(title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def main() -> int:
    cfg = load_config(ROOT / "config" / "persona.yaml")
    persona = cfg.persona
    brain_cfg = cfg.providers.get("brain", {})
    key_env = str(brain_cfg.get("api_key_env") or "")
    # ⚠️ 不要自己读 `os.environ` —— 密钥的真源是 `config.api_key()`，
    #    它会按 PET_CREDENTIALS_FILE -> ~/.desktop-pet/ -> pet-data/ 的顺序找。
    #    我第一版直接读环境变量，于是"明明配好了却说没密钥"。
    api_key = cfg.api_key()
    has_key = bool(api_key)

    hr("配置")
    print(f"  provider      = {brain_cfg.get('provider')}")
    print(f"  model         = {brain_cfg.get('model')}")
    print(f"  base_url      = {brain_cfg.get('base_url')}")
    print(f"  api_key_env   = {key_env}")
    print(f"  api_key()     = {'已解析（' + str(len(api_key)) + ' 字符）' if has_key else '**空**'}")
    print(f"  system_prompt_enabled = {persona.get('system_prompt_enabled')!r}")
    # ⚠️ 「显式为假」才关 —— 与 gateway.build_system_prompt 同一判据
    enabled_raw = persona.get("system_prompt_enabled")
    enabled = True
    if enabled_raw is not None:
        enabled = str(enabled_raw).strip().lower() not in ("false", "no", "0", "off")
    print(f"  -> 人格段落{'**已关闭**（只发状态+记忆）' if not enabled else '开启'}")
    print(f"  temperature   = {brain_cfg.get('temperature')}   max_tokens = {brain_cfg.get('max_tokens')}")

    # ---- 真实记忆服务（读真库、真嵌入） ----
    from pet.memory2 import MemoryService  # noqa: PLC0415

    mem = MemoryService(cfg)
    conv = Conversation(bus=None, config=cfg, memory=mem, brain=OpenAIBrain(cfg))  # type: ignore[arg-type]

    hr("① system prompt（每轮现拼 —— 走产品代码 Conversation.build_system_prompt）")
    sys_prompt = conv.build_system_prompt(idle=False)
    print(sys_prompt)
    print(f"\n  [长度 {len(sys_prompt)} 字]")

    hr("② 记忆段（MemoryService.inject_hint —— 真实数据库 + 真实嵌入检索）")
    query = "最近有什么好玩的游戏吗"
    recall = mem.inject_hint(query)
    print(f"  查询：{query!r}")
    print("-" * 78)
    print(recall if recall else "（空 —— 没有可注入的记忆）")
    print("-" * 78)
    print(f"  [长度 {len(recall)} 字]")

    # ---- 组装并真发 ----
    hr("③ 最终请求体（OpenAIBrain._body 的产物）")
    brain = OpenAIBrain(cfg)
    req = LLMRequest(
        system=sys_prompt,
        history=[],
        user_text=query,
        context_hint=recall,
        max_tokens=120,
    )
    body = brain._body(req)
    print(f"  model       = {body['model']}")
    print(f"  stream      = {body['stream']}")
    print(f"  messages    = {len(body['messages'])} 条")
    for i, m in enumerate(body["messages"]):
        print(f"    [{i}] role={m['role']:<9} content {len(m['content'])} 字")
    print(f"  system 全长 = {len(body['messages'][0]['content'])} 字")
    print("\n  ── system 全文（这就是真实发出去的那一份） ──")
    print(body["messages"][0]["content"])
    print("  ── 完 ──")

    hr("④ 真的发一次，看响应")
    if not has_key:
        print("  ⚠️ 没有密钥，跳过真实发送（上面 ③ 已经是最终产物，只差最后一步 POST）")
        return 1

    async def send() -> str:
        parts: list[str] = []
        async for ev in brain.stream(req):
            if ev.get("kind") == "delta":
                parts.append(str(ev.get("text") or ""))
            elif ev.get("kind") == "error":
                return f"**错误** {ev.get('message')}"
        return "".join(parts)

    text = asyncio.run(send())
    print(f"  响应：{text!r}")
    print(f"  [长度 {len(text)} 字]")
    print("\n  -> 说明：如果这里有正常中文回复，就证明**整份提示词真的发过去了**")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
