"""
把 `inject_hint()` 的产出**按段拆开**，量出每段各占多少字 ——
回答"8173 字到底花在哪"。

## 为什么要拆
粗看是"最近记忆 8173 字"，但对库一查：注入窗口内 120 条 recent_turn 只有 3210 字。
差 4963 字 —— 所以那个结论是错的，必须按段量。
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

# ⚠️ 必须先复刻 sidecar 的运行环境（cwd 给分词 DLL、注入凭据）。
#    不这么做会**静默降级**：嵌入器回落到 256 维哈希 -> 往真实库写一条错维向量
#    -> 下次应用启动触发全库重建。实测踩过，见 tool_bootstrap.py 的说明。
sys.path.insert(0, str(Path(__file__).resolve().parent))
from tool_bootstrap import ROOT, setup  # noqa: E402

setup()

TOKENS_PER_CHAR = 1.25


def main() -> int:
    from pet.config import load_config
    from pet.memory2 import MemoryService

    cfg = load_config(ROOT / "config" / "persona.yaml")
    mem = MemoryService(cfg)

    # 逐段单独取（用产品代码自己的方法，不重造）
    db = mem.db
    assert db is not None

    import time

    since = time.time() - mem.recent_window_days() * 86400
    limit = int((cfg.memory or {}).get("recent_inject_limit") or 120)

    parts: list[tuple[str, str]] = []

    fixed = db.fixed_memories()
    if fixed:
        parts.append(("① 固定注入", "\n".join(f"- {r['summary']}" for r in fixed[:40])))

    pinned = db.pinned_cherished()
    if pinned:
        parts.append(("② 珍藏记忆", "\n".join(str(r["text"]) for r in pinned[:5])))

    turns = db.recent_turns(since, limit=limit)
    if turns:
        # ⚠️ 要走去重 —— 真实注入路径会调 `_dedupe_recent`，
        #    不跟着走的话这一段的字符数会比实产大一截，看着像"提示词没变小"。
        rows = [f"{t['role']}: {t['text']}" for t in turns]
        deduped = MemoryService._dedupe_recent(rows)
        parts.append(
            (
                "③ 最近记忆(原文)",
                "\n".join(deduped),
            )
        )
        print(f"  （③ 去重：{len(rows)} 行 -> {len(deduped)} 行，省 {len(rows) - len(deduped)} 行）")

    # ④ 检索：拿一个真实查询
    hits = mem._retrieve_hits("最近有什么好玩的游戏吗")
    if hits:
        parts.append(("④ 检索结果", "\n".join(str(h.get("text") or "") for h in hits)))

    full = mem.inject_hint("最近有什么好玩的游戏吗")

    print("=" * 78)
    print("各段实际体积")
    print("=" * 78)
    print(f"  {'段':<20} {'条数':>5} {'字符':>7} {'≈token':>8}   占比")
    total = 0
    for name, body in parts:
        n = len(body)
        total += n
        cnt = body.count("\n") + 1 if body else 0
        print(f"  {name:<20} {cnt:>5} {n:>7} {int(n * TOKENS_PER_CHAR):>8}   {n / len(full) * 100 if full else 0:>4.0f}%")
    print(f"  {'各段合计(近似)':<20} {'':>5} {total:>7} {int(total * TOKENS_PER_CHAR):>8}")
    print()
    print(f"  **inject_hint() 实产: {len(full)} 字 ≈ {int(len(full) * TOKENS_PER_CHAR)} token**")
    print(f"  差额 {len(full) - total} 字 = 各段的**头部/说明行**与模板")
    print()
    print("  ⚠️ 上面各段是**自己拼的近似**（为了分项计量）—— 与真实产物有出入，")
    print("     真实产物还带各段头部、注入用的截断与模板。")
    print("     **以 inject_hint() 实产为准**，不要拿『各段合计』去质疑它：")
    print("     我第一版在这里打了个『差 N 字』的误报警，实际只是少算了说明行。")

    print("=" * 78)
    print("③ 最近记忆 的重复情况（这是最可能有问题的一段）")
    print("=" * 78)
    if turns:
        lines = [f"{t['role']}: {t['text']}" for t in turns]
        uniq = set(lines)
        print(f"  行数 {len(lines)}  不同 {len(uniq)}  重复 {len(lines) - len(uniq)} 条")
        chars = sum(len(x) for x in lines)
        uchars = sum(len(x) for x in uniq)
        print(f"  字符 {chars} -> 去重后 {uchars}  （**可省 {chars - uchars} 字 ≈ {int((chars - uchars) * TOKENS_PER_CHAR)} token**）")
        from collections import Counter

        c = Counter(lines)
        print("  逐字重复最多的：")
        for t, n in c.most_common(5):
            if n > 1:
                print(f"    ×{n:<3} {t[:56]}")

    print()
    print("=" * 78)
    print("每段的说明行/头部（也是要发过去的字）")
    print("=" * 78)
    for line in full.split("\n"):
        if line.startswith("（") or line.startswith("【") or line.startswith("你记得"):
            print(f"  {len(line):>4} 字  {line[:64]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
