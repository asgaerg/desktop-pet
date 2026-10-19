#!/usr/bin/env python
"""
量一量**每一轮 API 请求到底有多大**，以及哪些参数能调。

## 为什么要做成工具

"上下文是不是在堆积"这个问题，光读代码答不准 —— 真实数字取决于
**配置 + 数据库现状 + 历史长度**三者的组合。所以这里全部走产品代码
（`Conversation.build_system_prompt` / `MemoryService.inject_hint`），
量的是**真会发出去的那一份**，不是估算。

## 用法

    python tools/measure-request.py
    python tools/measure-request.py --query "今晚吃什么"
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tool_bootstrap import ROOT, setup  # noqa: E402

setup(verbose=False)

from pet.config import load_config  # noqa: E402
from pet.gateway import HISTORY_LIMIT, Conversation, EmotionState  # noqa: E402
from pet.memory2 import MemoryService  # noqa: E402

# 中文在 DeepSeek 分词下约 1.25 token/字（实测口径，见 docs）
TOKENS_PER_CHAR = 1.25


def main() -> int:
    ap = argparse.ArgumentParser(description="量一轮 API 请求的真实构成")
    ap.add_argument("--query", default="最近有什么好玩的游戏吗", help="用来测检索的查询")
    args = ap.parse_args()

    cfg = load_config(ROOT / "config" / "persona.yaml")
    brain_cfg = cfg.providers.get("brain", {}) or {}
    persona = cfg.persona

    mem = MemoryService(cfg)
    hint = mem.inject_hint(args.query)

    conv = Conversation.__new__(Conversation)  # 只要它拼提示词的能力，不建整套
    conv.config = cfg
    conv.memory = mem
    conv.emotion = EmotionState()
    system = conv.build_system_prompt(idle=False)

    history_turns = int(brain_cfg.get("history_turns") or 12)
    counts = mem.db.count() if mem.db else {}

    def tokens(chars: int) -> int:
        return int(round(chars * TOKENS_PER_CHAR))

    print("=" * 78)
    print("每一轮 API 请求的真实构成")
    print("=" * 78)
    print()
    print("① **每个请求都会带**的部分（不随对话变长而增长）")
    print()
    print(f"   {'段落':<34}{'字符':>8}{'≈token':>9}")
    print(f"   {'-' * 50}")
    print(f"   {'system：人格 + 活泼度 + 情绪':<32}{len(system):>8}{tokens(len(system)):>9}")
    print(f"   {'记忆段（固定+最近+检索）':<31}{len(hint):>8}{tokens(len(hint)):>9}")
    fixed = len(system) + len(hint)
    print(f"   {'-' * 50}")
    print(f"   {'小计':<34}{fixed:>8}{tokens(fixed):>9}")
    print()
    print("② **随对话变长而增长**的部分")
    print()
    print(f"   对话历史：最多 {history_turns} 轮 = {history_turns * 2} 条消息")
    print(f"             硬上限 HISTORY_LIMIT = {HISTORY_LIMIT} 轮（内存里超出从头丢）")
    print(f"   本轮用户输入：1 条消息")
    print()
    print("③ 所以一轮请求 ≈")
    low, high = fixed + 40, fixed + 900
    print(f"   空闲时 {tokens(low)} token ～ 历史满时 {tokens(high)} token")
    print()
    print("=" * 78)
    print("当前配置里**真正影响请求大小**的参数")
    print("=" * 78)
    print()
    print(f"   {'参数':<34}{'当前值':>10}  说明")
    print(f"   {'-' * 72}")
    knobs = [
        (
            "persona.system_prompt_enabled",
            persona.get("system_prompt_enabled"),
            "关掉 = 人格那几段全不加（省最多）",
        ),
        ("providers.brain.history_turns", history_turns, "带几轮历史（线性增长）"),
        ("providers.brain.max_tokens", brain_cfg.get("max_tokens"), "回复长度上限（只管输出）"),
        ("providers.brain.temperature", brain_cfg.get("temperature"), "随机性，不影响大小"),
        ("memory.recent_window_days", cfg.memory.get("recent_window_days"), "最近记忆取几天"),
        ("memory.recent_inject_limit", cfg.memory.get("recent_inject_limit"), "最近记忆最多注入几条"),
        ("memory.top_k", cfg.memory.get("top_k"), "检索注入几条"),
        ("memory.snippet_chars", cfg.memory.get("snippet_chars"), "每条记忆截多长"),
        ("memory.profile_max", cfg.memory.get("profile_max"), "固定注入最多几条"),
        ("memory.keep_utterances", cfg.memory.get("keep_utterances"), "是否连用户原话一起记"),
        ("memory.max_items", cfg.memory.get("max_items"), "检索层条数上限（超出淘汰）"),
        ("autonomy.max_idle_tokens", cfg.autonomy.get("max_idle_tokens"), "主动发言的长度上限"),
    ]
    for key, value, note in knobs:
        print(f"   {key:<34}{str(value):>10}  {note}")
    print()
    print("=" * 78)
    print("数据库现状（决定记忆段能有多大）")
    print("=" * 78)
    print()
    for name, n in counts.items():
        print(f"   {name:<20}{n:>8}")
    print()
    mem.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
