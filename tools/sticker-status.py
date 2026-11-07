#!/usr/bin/env python
"""看一眼表情包分类的状态。

## 用法

    python tools/sticker-status.py

直接输出：

    全部 / 可用 / other / 未分类
    每个类别有多少张
    哪些类别素材太少（宠物可能反复发同一张）
    标注循环现在是不是在跑

## 为什么要有这个工具

用户问：「我要怎么才能知道什么时候分类完啊」。

在那之前，分类进度**只有通过对话问我才能知道** —— 这明显不合理。
用户应该能自己一条命令看到，不用等我回话。

## 为什么不放进设置页

放进设置页要动前端（HTML + JS + IPC + 契约测试），
而这是一个**诊断/运维**用途的工具，命令行更直接。
设置页以后要加也行，接口已经在 `MediaStore.stats()` 里了。
"""

from __future__ import annotations

import collections
import sys
import time
from pathlib import Path

sys.stdout = open(sys.stdout.fileno(), "w", encoding="utf-8", errors="replace", closefd=False)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "apps" / "brain"))

from pet.media import EMOTIONS, MediaStore  # noqa: E402


def human_ago(ts: float) -> str:
    if not ts:
        return "从未"
    d = time.time() - ts
    if d < 60:
        return f"{int(d)} 秒前"
    if d < 3600:
        return f"{int(d/60)} 分钟前"
    if d < 86400:
        return f"{int(d/3600)} 小时前"
    return f"{int(d/86400)} 天前"


def main() -> int:
    store = MediaStore(ROOT / "pet-media")
    items = [it for it in store.load().values() if it.path.startswith("stickers/")]
    if not items:
        print("   pet-media/stickers/ 里还没有图片。")
        print("   把表情包放进 pet-media/stickers/ 下的任意子目录就行。")
        return 0

    usable = [it for it in items if it.caption and it.emotion and it.emotion != "other"]
    other = [it for it in items if it.emotion == "other"]
    unlabeled = [it for it in items if not it.caption or not it.emotion]

    print()
    print("  ══════════════════════════════════════════════════════")
    print("   表情包分类状态")
    print("  ══════════════════════════════════════════════════════")
    print()
    total = len(items)
    print(f"   全部图片      {total:5d}")
    print(f"   有明确类别     {len(usable):5d}   ({len(usable)*100//total}%)  ← 精确匹配")
    print(f"   other         {len(other):5d}   ({len(other)*100//total}%)  ← 随性发（约 18% 概率）")
    print(f"   未分类        {len(unlabeled):5d}", end="")
    if unlabeled:
        print(f"   ⏳ 还在处理（还有 {len(unlabeled)} 张没标完）")
    else:
        print("   ✅ 全部处理完")
    print()

    if una := unlabeled:
        # 估算剩余时间：实测约 1.5 秒/张，3 路并发
        eta_min = len(una) * 1.5 / 3 / 60
        print(f"   预计还需约 {eta_min:.0f} 分钟（按 1.5 秒/张、3 路并发估）")
        print()

    # ── 分类分布 ──
    counts: collections.Counter[str] = collections.Counter(
        it.emotion for it in usable
    )
    print("  ── 每个类别有多少张 ──")
    print()
    if counts:
        for k, n in counts.most_common():
            label = EMOTIONS.get(k, k)
            bar = "█" * min(int(n / 3), 40)
            print(f"   {label:8s} {n:4d}  {bar}")
    else:
        print("   （还没有任何可用素材）")
    print()

    # ── 素材偏少的类别 ──
    zero = [EMOTIONS[k] for k in EMOTIONS if k != "other" and k not in counts]
    few = [(EMOTIONS.get(k, k), n) for k, n in counts.items() if n < 3]
    if zero or few:
        print("  ── 需要注意的类别 ──")
        print()
        if zero:
            print(f"   零素材（{len(zero)} 类）: {'、'.join(zero)}")
            print("     这些类别触发时会自动发语义相邻的图（不会报错）")
        if few:
            print(f"   少于 3 张（{len(few)} 类）:")
            print("     " + "、".join(f"{lbl}({n})" for lbl, n in few))
            print("     宠物可能反复发同一张；想补就放图进对应目录")
        print()

    # ── 标注循环有没有在跑 ──
    print("  ── 自动标注现在在做什么 ──")
    print()
    log_path = ROOT / "apps" / "desktop" / "pet-data" / "main.log"
    if log_path.is_file():
        text = log_path.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()[-4000:]
        last = ""
        for line in reversed(lines):
            if "表情包标注" in line:
                last = line.strip()
                break
        if last:
            # 日志行前面有时间戳，截取有用的部分
            idx = last.find("表情包标注")
            print(f"   {last[idx:][:100]}")
        else:
            print("   日志里没有标注记录（可能应用没启动过）")
        # 最后一条「发了」的时间
        last_send = ""
        for line in reversed(lines):
            if "表情包：发了" in line:
                last_send = line.strip()
                break
        if last_send:
            idx = last_send.find("表情包：发了")
            print(f"   {last_send[idx:][:90]}")
    else:
        print("   找不到 main.log（应用没启动过？）")
    print()
    print("  ══════════════════════════════════════════════════════")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
