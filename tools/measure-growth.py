"""记忆规模的增长模型 —— 压缩方案要按数据说话，不能拍脑袋。

## 量什么

  ① 现在实际有多少（原文 / 检索层）
  ② 每轮对话产生多少条提炼事实（实测比例）
  ③ 按这个速率，多久会到需要压缩的规模
  ④ 各层的体积（向量占多少）

## 为什么要先量

"记忆会膨胀"是常识，但**膨胀多快**决定了压缩该多重：
  · 一年才 500 条 -> 不需要压缩，加个上限就行；
  · 一个月 5000 条 -> 必须做合并 + 归档。

拍脑袋设计压缩，很容易做出一个"永远不会触发的复杂机制"。
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "apps" / "desktop" / "pet-data" / "memory" / "pet.db"

_DLL = ROOT / "apps" / "desktop" / "pet-data" / "vendor" / "simple" / "libsimple-windows-x64"
if (_DLL / "dict" / "jieba.dict.utf8").is_file():
    os.chdir(_DLL)


def main() -> int:
    if not DB.is_file():
        print(f"  ❌ 找不到记忆库：{DB}")
        return 1
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    print("=" * 78)
    print("记忆规模与增长")
    print("=" * 78)

    counts = {}
    for t in ("raw_log", "recent_turn", "memory", "memory_vec", "cherished", "cherished_turn"):
        counts[t] = int(conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])
        print(f"  {t:<16}{counts[t]:>7}")

    # ---- 时间跨度 ----
    span = conn.execute("SELECT MIN(ts), MAX(ts) FROM raw_log").fetchone()
    if span and span[0] and span[1]:
        import datetime as dt

        lo, hi = float(span[0]), float(span[1])
        days = max((hi - lo) / 86400, 1 / 24)
        turns = counts["raw_log"]
        print()
        print(f"  时间跨度      {days:.2f} 天（{dt.datetime.fromtimestamp(lo):%m-%d %H:%M} ~ "
              f"{dt.datetime.fromtimestamp(hi):%m-%d %H:%M}）")
        print(f"  平均          {turns / days:.0f} 轮/天（原文按「轮」算，一轮=一条）")
        print(f"  即            {turns / days / 24:.1f} 轮/小时")

    # ---- 提炼比例 ----
    print()
    print("  ── 提炼产出比例 ──")
    # 用真实数据估：memory 里的 src_id 指向 raw_log（如果记了的话）
    have_src = conn.execute(
        "SELECT COUNT(*) FROM memory WHERE src_id IS NOT NULL"
    ).fetchone()[0]
    print(f"  带来源标记的检索记忆  {have_src}/{counts['memory']}")
    if counts["raw_log"]:
        ratio = counts["memory"] / counts["raw_log"]
        print(f"  提炼比例（memory/raw_log）  {ratio:.3f} 条/轮")
        print("     ⚠️ 这个比例**严重偏低**，因为提炼是最近才接上的（之前 provider=mock 不提炼）")

    # ---- 体积 ----
    print()
    print("  ── 体积 ──")
    page = conn.execute("PRAGMA page_count").fetchone()[0]
    psize = conn.execute("PRAGMA page_size").fetchone()[0]
    print(f"  库文件        {DB.stat().st_size / 1024:.0f} KB（{page} 页 × {psize}B）")
    vec_bytes = conn.execute("SELECT COALESCE(SUM(LENGTH(vector)),0) FROM memory_vec").fetchone()[0]
    raw_bytes = conn.execute("SELECT COALESCE(SUM(LENGTH(text)),0) FROM raw_log").fetchone()[0]
    print(f"  向量数据      {vec_bytes / 1024:.1f} KB")
    print(f"  原文文本      {raw_bytes / 1024:.1f} KB")
    if counts["memory"]:
        print(f"  每条向量      {vec_bytes / counts['memory']:.0f} B（512 维 float32 = 2048B）")

    # ---- 外推 ----
    print()
    print("=" * 78)
    print("外推：按当前速率")
    print("=" * 78)
    if span and span[0] and span[1]:
        per_day = counts["raw_log"] / days
        # 提炼比例按"接上 DeepSeek 之后"的真实值估：
        # 一次用户发言通常提炼 0~3 条，取实测均值 1.2（见第十七节样本）
        facts_per_turn = 1.2
        user_turns_per_day = per_day / 2  # 一半是用户说的
        facts_per_day = user_turns_per_day * facts_per_turn

        print(f"  按 {per_day:.0f} 轮/天、每轮用户发言提炼 {facts_per_turn} 条估：")
        print()
        print(f"  {'时间':<10}{'原文轮数':>10}{'检索层条数':>12}{'向量体积':>12}{'库大小约':>12}")
        for label, d in (("1 周", 7), ("1 月", 30), ("3 月", 90), ("1 年", 365), ("3 年", 1095)):
            raw_n = per_day * d
            mem_n = facts_per_day * d
            vec_mb = mem_n * 2048 / 1024 / 1024
            # 原文每轮约 30 字（实测 437 条时约 60KB 文本）
            raw_mb = raw_n * 140 / 1024 / 1024
            db_mb = vec_mb + raw_mb + mem_n * 100 / 1024 / 1024
            print(f"  {label:<10}{raw_n:>10.0f}{mem_n:>12.0f}{vec_mb:>11.1f}MB{db_mb:>11.1f}MB")

        print()
        print("  ⚠️ 注意两点：")
        print("    · `max_items: 2000` 是**检索层**的上限 —— 按上面速率，")
        print(f"      大约 **{2000 / facts_per_day:.0f} 天**（{2000 / facts_per_day / 30:.0f} 个月）就会撞上")
        print("    · **原文层没有任何上限** —— 它是线性增长的，3 年约 "
              f"{per_day * 1095 * 140 / 1024 / 1024:.0f}MB 文本")

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
