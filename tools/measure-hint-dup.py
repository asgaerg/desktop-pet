"""
量化记忆注入段的**体积与重复率** —— 回答"每轮到底发过去多少、其中多少是废话"。

用真实配置 + 真实数据库跑 `MemoryService.inject_hint()`，然后按行统计重复。
"""

from __future__ import annotations

import os
import re
import sys
from collections import Counter
from pathlib import Path

# ⚠️ 必须先复刻 sidecar 的运行环境（cwd 给分词 DLL、注入凭据）。
#    不这么做会**静默降级**：嵌入器回落到 256 维哈希 -> 往真实库写一条错维向量
#    -> 下次应用启动触发全库重建。实测踩过，见 tool_bootstrap.py 的说明。
sys.path.insert(0, str(Path(__file__).resolve().parent))
from tool_bootstrap import ROOT, setup  # noqa: E402

setup()

from pet.config import load_config  # noqa: E402
from pet.memory2 import MemoryService  # noqa: E402

# 与之前实测口径一致：中文 ≈ 1.25 token/字
TOKENS_PER_CHAR = 1.25


def main() -> int:
    cfg = load_config(ROOT / "config" / "persona.yaml")
    mem = MemoryService(cfg)

    for query in ("最近有什么好玩的游戏吗", ""):
        hint = mem.inject_hint(query)
        print("=" * 76)
        print(f"查询：{query!r}")
        print("=" * 76)
        if not hint:
            print("  （空）")
            continue

        chars = len(hint)
        print(f"  总长度      : {chars} 字  ≈ {int(chars * TOKENS_PER_CHAR)} token")

        # 分节（四段各有自己的标题行/说明行）
        sections = re.split(r"\n(?=（以下)", hint)
        if len(sections) > 1:
            print(f"  段数        : {len(sections)}")
            for i, sec in enumerate(sections):
                first = sec.strip().split("\n")[0][:44]
                print(f"    [{i}] {len(sec):>5} 字  {first}")

        # ⚠️ 统计前必须**剥掉时间前缀**归一化。
        #    同一句话在两天里说了 12 次，带上 `[2 天前] ` 之后每行都不同 ——
        #    按原行统计只能看到"部分重复"，测不出真正的浪费。
        def strip_when(s: str) -> str:
            return s[s.find("] ") + 2 :] if s.startswith("[") and "] " in s else s

        lines = [ln.strip() for ln in hint.split("\n") if ln.strip()]
        norm = [strip_when(ln) for ln in lines]
        uniq_norm = set(norm)
        dup_norm = len(norm) - len(uniq_norm)
        print(f"  行数        : {len(lines)}")
        print(f"  归一化后不同行: {len(uniq_norm)}")
        print(f"  **逐字重复行: {dup_norm} 条**（占 {dup_norm / max(1, len(norm)) * 100:.0f}%）")

        # 重复行浪费的字数（按归一化后的行算）
        counter_n = Counter(norm)
        wasted = sum((n - 1) * len(t) for t, n in counter_n.items() if n > 1)
        print(
            f"  **重复内容占用: {wasted} 字 ≈ {int(wasted * TOKENS_PER_CHAR)} token**"
            f"（占该段 {wasted / max(1, chars) * 100:.0f}%）"
        )

        # 最重复的几行
        top = [(n, t) for t, n in counter_n.most_common(4) if n > 1]
        if top:
            print("  重复最多的行：")
            for n, t in top:
                print(f"    ×{n:<3} {t[:56]}")

    print()
    print("=" * 76)
    print("对照：DeepSeek 上下文窗口 64K token")
    print("=" * 76)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
