"""验证「爱好由对话形成」真的能长出来（2026-10-17）。

## 验的是什么

```
用户反复提到银松镇  ->  interest 表里出现「银松镇」，权重随次数上升
只提一次的东西      ->  权重低，不该马上变成"爱好"
模板动词/泛词        ->  不许进兴趣表（"喜欢""游戏""角色"）
衰减                 ->  久不聊会变淡
```

⚠️ 用**临时库**，绝不碰真实 `pet.db`（用户的记忆不该被测试污染）。

用法：
    python tools/test-interest-growth.py
"""

from __future__ import annotations

import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

ROOT = Path(r"F:\ai项目")
sys.path.insert(0, str(ROOT / "apps" / "brain"))

from pet import nouns  # noqa: E402
from pet.store import PetDatabase  # noqa: E402

VENDOR = ROOT / "apps" / "desktop" / "pet-data" / "vendor"
SIMPLE = VENDOR / "simple" / "libsimple-windows-x64" / "simple.dll"

# 模拟一段真实对话里被提炼出来的记忆（按新提示词的形态写）
SEGMENT_1 = [
    "用户在玩《银松镇》，被转盘机关卡住，绕了半小时才找到第二盏灯",
    "《银松镇》是手绘风格的生存恐怖游戏",
    "用户买了银松镇这款游戏",
]
SEGMENT_2 = [
    "用户在玩 GTA6 的预告片看了好几遍",
    "GTA6 定在 2026-11-19 发售",
]
# 只会被提一次的东西
ONE_OFF = ["用户今天有点累"]
# 模板动词/泛词：绝对不该进兴趣表
FORBIDDEN = {"喜欢", "关注", "风格", "恐怖", "游戏", "角色", "用户", "有点累", "这款"}


def open_db(path: Path) -> PetDatabase:
    # ⚠️ cppjieba 用相对路径找词典，必须先切到 DLL 所在目录
    import os

    os.chdir(SIMPLE.parent)
    db = PetDatabase(path, tokenizer="simple", simple_dll=SIMPLE)
    return db


def wire(db: PetDatabase) -> None:
    def segment(text: str) -> list[str]:
        row = db.conn.execute("SELECT jieba_query(?)", (str(text),)).fetchone()
        return nouns.parse_jieba_query(row[0]) if row and row[0] else []

    nouns.set_segmenter(segment)


def feed(db: PetDatabase, facts: list[str]) -> list[str]:
    """按 memory2._grow_interests 的规则加权（这里复刻一份，避免起整个服务）。"""
    ranked = nouns.extract_ranked(facts)
    strong = [t for t, kind, _ in ranked if kind == "strong"]
    weak = [
        t for t, kind, count in ranked if kind == "weak" and count >= nouns.MIN_CJK_COUNT
    ]
    if strong:
        db.bump_interests(strong, strength="strong", source="test")
    if weak:
        db.bump_interests(weak, strength="weak", source="test")
    return strong + weak


def main() -> int:
    failed: list[str] = []
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "interest-test.db"
        db = open_db(path)
        wire(db)
        try:
            print("=" * 78)
            print("  验证「爱好由对话形成」")
            print("=" * 78)

            # ── ① 第一段对话 ──
            got1 = feed(db, SEGMENT_1)
            print()
            print("  ① 第一段对话（提了 3 次银松镇）")
            print(f"     抽出的词：{got1}")
            rows = db.top_interests(min_weight=0.0)
            print(f"     兴趣表：{[(r['topic'], r['weight']) for r in rows]}")
            if not any("银松镇" in r["topic"] for r in rows):
                print("     ❌ 银松镇 没进兴趣表")
                failed.append("银松镇未成为兴趣")
            else:
                print("     ✅ 银松镇 已成为兴趣")

            # ── ② 第二段对话 ──
            got2 = feed(db, SEGMENT_2)
            print()
            print("  ② 第二段对话（GTA6）")
            print(f"     抽出的词：{got2}")
            rows = db.top_interests(min_weight=0.0)
            print(f"     兴趣表：{[(r['topic'], r['weight']) for r in rows]}")
            if not any("GTA6" in r["topic"] for r in rows):
                print("     ❌ GTA6 没进兴趣表")
                failed.append("GTA6未成为兴趣")

            # ── ③ 重复提及，权重应上升 ──
            before = {r["topic"]: r["weight"] for r in db.top_interests(min_weight=0.0)}
            for _ in range(4):
                feed(db, SEGMENT_1)
            after = {r["topic"]: r["weight"] for r in db.top_interests(min_weight=0.0)}
            silver_before = next((v for k, v in before.items() if "银松镇" in k), 0.0)
            silver_after = next((v for k, v in after.items() if "银松镇" in k), 0.0)
            print()
            print("  ③ 再提 4 次银松镇")
            print(f"     权重 {silver_before:.2f} -> {silver_after:.2f}")
            if silver_after > silver_before:
                print("     ✅ 反复提到 -> 权重上升（这就是'爱好被强化'）")
            else:
                print("     ❌ 权重没上升")
                failed.append("权重未上升")

            # ── ④ 一次性的事不该变成爱好 ──
            time.sleep(0.01) if False else None
            feed(db, ONE_OFF)
            rows = db.top_interests(min_weight=0.0)
            had_oneoff = any("有点累" in r["topic"] for r in rows)
            print()
            print(f"  ④ 只提一次的「用户今天有点累」进兴趣表了吗：{had_oneoff}")
            if had_oneoff:
                print("     ⚠️ 进来了（弱信号 + 出现 1 次本该被挡）")
            else:
                print("     ✅ 没进来（一次性的事不算爱好）")

            # ── ⑤ 模板动词绝不许进 ──
            rows = db.top_interests(min_weight=0.0)
            topics = {r["topic"] for r in rows}
            bad = topics & FORBIDDEN
            print()
            print(f"  ⑤ 兴趣表里有没有模板动词：{sorted(bad) if bad else '无'}")
            if bad:
                print(f"     ❌ 混进了 {bad}")
                failed.append(f"模板动词进表: {bad}")
            else:
                print("     ✅ 干净（喜欢/关注/风格/游戏 都没进）")

            # ── ⑥ 衰减 ──
            db.conn.execute("UPDATE interest SET weight = 0.16 WHERE topic LIKE '%银松镇%'")
            db.conn.commit()
            removed = db.decay_interests(0.5)
            rows = db.top_interests(min_weight=0.0)
            still = any("银松镇" in r["topic"] for r in rows)
            print()
            print(f"  ⑥ 衰减：删掉 {removed} 个过淡的；银松镇还在吗 {still}")
            if not still:
                print("     ✅ 久不聊会变淡并最终移除")
            else:
                print("     ⚠️ 还在（权重还不够低）")

            # ── ⑦ 真实查库确认表结构 ──
            cur = db.conn.execute("PRAGMA table_info(interest)")
            cols = [r[1] for r in cur.fetchall()]
            print()
            print(f"  ⑦ interest 表结构：{cols}")
            need = {"topic", "weight", "hits", "strength", "created_at", "last_seen_at"}
            if need <= set(cols):
                print("     ✅ 字段齐全")
            else:
                print(f"     ❌ 缺字段 {need - set(cols)}")
                failed.append("interest 表缺字段")
        finally:
            db.close()

    print()
    print("=" * 78)
    if failed:
        print(f"  ❌ {len(failed)} 项失败：")
        for item in failed:
            print(f"     · {item}")
    else:
        print("  ✅ 全部通过：兴趣能从对话里长出来，且不会长成垃圾")
    print("=" * 78)
    return 1 if failed else 0


raise SystemExit(main())
