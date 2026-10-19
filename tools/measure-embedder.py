"""在换 ONNX 依赖之前，先量清楚**现有哈希嵌入的真实语义能力**。

## 为什么先量

`HashingEmbedder` 不是朴素的 feature hashing —— 它是**四层加权特征**
（整串 / 整词 / 词内单字 / 字符 n-gram）。所以"零字面重合就一定失败"
这个判断**需要实测**，不能推。

如果它对"换词提问"其实能命中相当一部分，那 50MB 的 onnxruntime 依赖
就要重新权衡；如果确实不行，这份数据就是换模型的**依据**。

## 测什么

对每组 (查询, 目标)，算余弦相似度，看它在现有阈值（`min_score` 0.18）
之上还是之下。查询刻意**不含目标的任何实词**。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "apps" / "brain"))

# 与其它脚本一致：分词词典的 cwd 依赖
_DLL = ROOT / "apps" / "desktop" / "pet-data" / "vendor" / "simple" / "libsimple-windows-x64"
if (_DLL / "dict" / "jieba.dict.utf8").is_file():
    os.chdir(_DLL)

from pet.embedding import HashingEmbedder  # noqa: E402

# (查询, 目标事实, 期望能匹配吗)
CASES: list[tuple[str, str, bool]] = [
    # ── 换词提问：零字面重合，**真语义才该命中** ──
    ("他爱喝什么饮料", "用户每天早上喝一杯冰美式", True),
    ("他作息怎么样", "用户通常凌晨两点多睡", True),
    ("他平时用什么做开发", "用户使用 Electron 开发", True),
    ("界面配色是什么风格", "用户偏好深色界面", True),
    ("他最近在忙什么", "用户在做桌面宠物项目", True),
    # ── 应当**不该**命中的（防止"什么都算像"）──
    ("他爱喝什么饮料", "用户在做桌面宠物项目", False),
    ("他作息怎么样", "用户偏好深色界面", False),
    ("今天天气怎么样", "用户使用 Electron 开发", False),
    ("周末去哪玩", "用户通常凌晨两点多睡", False),
    # ── 有字面重合的（本来就该命中，作对照）──
    ("Electron", "用户使用 Electron 开发", True),
    ("深色", "用户偏好深色界面", True),
    ("桌面宠物", "用户在做桌面宠物项目", True),
    # ── 近义但不完全同词 ──
    ("喝咖啡", "用户每天早上喝一杯冰美式", False),
    ("熬夜", "用户通常凌晨两点多睡", False),
]


def main() -> int:
    emb = HashingEmbedder(dim=256)
    threshold = 0.18

    print("=" * 88)
    print("现有哈希嵌入的语义能力实测（阈值 min_score = 0.18）")
    print("=" * 88)

    rows = []
    for q, target, want in CASES:
        sim = emb.similarity(q, target)
        got = sim > threshold
        rows.append((q, target, want, sim, got))

    print(f"  {'查询':<20}{'目标事实':<26}{'余弦':>8}{'过阈值':>8}{'期望':>6}  判定")
    print("  " + "-" * 84)
    correct = 0
    for q, target, want, sim, got in rows:
        mark = "✅" if got == want else "❌ 误判"
        if got == want:
            correct += 1
        print(f"  {q:<20}{target:<26}{sim:>8.3f}{'是' if got else '否':>8}{'是' if want else '否':>6}  {mark}")

    print()
    print(f"  判定正确 {correct}/{len(rows)}")

    # 分开看两类
    sem = [r for r in rows if r[0] in ("他爱喝什么饮料", "他作息怎么样", "他平时用什么做开发",
                                       "界面配色是什么风格", "他最近在忙什么")]
    print()
    print("  ── 只看「换词提问」（真语义才该命中）──")
    hit = sum(1 for r in sem if r[4])
    print(f"    命中 {hit}/{len(sem)}")
    for q, target, _w, sim, got in sem:
        print(f"      {'✓' if got else '·'} {q}  vs  {target}   ({sim:.3f})")

    print()
    print("  ── 只看「有字面重合」（本来就该命中，作对照）──")
    lit = [r for r in rows if r[0] in ("Electron", "深色", "桌面宠物")]
    hit2 = sum(1 for r in lit if r[4])
    print(f"    命中 {hit2}/{len(lit)}")

    print()
    print("=" * 88)
    print("结论怎么读")
    print("=" * 88)
    print("  · 若「换词提问」命中率很低 -> 哈希确实做不到语义，换 bge 有实质收益")
    print("  · 若「不该命中」的组误报多   -> 阈值/特征设计有问题，换模型前要先修")
    print("  · 「有字面重合」那组应当全中   -> 否则是回归")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
