"""纯标准库的"轻量语义层"能做到什么程度 —— 作为换 ONNX 的对照。

## 背景

项目的硬约束是**大脑零第三方依赖**（连 WebSocket 都手写 RFC 6455）。
所以在装 `onnxruntime`（13.6MB wheel，运行时 ~50MB）之前，先量清楚
**不装依赖能到什么程度**，让取舍有数据支撑。

## 做法

纯标准库的"语义"只能靠**同义词/上下位词映射**做**查询扩展**：
把查询里的词换成/补上库里可能用的词，再走现有的字符 n-gram 检索。

这不是真语义（没有泛化能力），但针对"口语提问 vs 陈述式记忆"这个
**特定场景**，它能补上相当一部分 —— 因为那两类说法用的词是可枚举的：

    他爱喝什么饮料  ->  喝 饮料 -> 喝 咖啡 茶
    他作息怎么样    ->  作息   -> 睡 点 熬夜

## 这份脚本量的是

  ① 同义词表能覆盖多少（人工列一个，看命中率提升）
  ② 它引入多少误报（这是关键 —— 扩展太多会把噪音放进来）
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "apps" / "brain"))

_DLL = ROOT / "apps" / "desktop" / "pet-data" / "vendor" / "simple" / "libsimple-windows-x64"
if (_DLL / "dict" / "jieba.dict.utf8").is_file():
    os.chdir(_DLL)

from pet.embedding import HashingEmbedder  # noqa: E402

# 口语提问 -> 库里可能用的词。**人工枚举**，这是它的上限来源。
SYNONYMS: dict[str, tuple[str, ...]] = {
    "饮料": ("咖啡", "茶", "水", "美式", "喝"),
    "喝的": ("咖啡", "茶", "美式", "喝"),
    "作息": ("睡", "点", "凌晨", "熬夜", "起"),
    "睡觉": ("睡", "凌晨", "点"),
    "忙": ("项目", "做", "开发"),
    "做": ("开发", "项目", "用"),
    "开发": ("Electron", "代码", "项目", "写"),
    "配色": ("深色", "浅色", "界面", "主题"),
    "界面": ("深色", "主题", "配色"),
    "风格": ("深色", "浅色", "主题"),
}

CASES: list[tuple[str, str, bool]] = [
    ("他爱喝什么饮料", "用户每天早上喝一杯冰美式", True),
    ("他作息怎么样", "用户通常凌晨两点多睡", True),
    ("他平时用什么做开发", "用户使用 Electron 开发", True),
    ("界面配色是什么风格", "用户偏好深色界面", True),
    ("他最近在忙什么", "用户在做桌面宠物项目", True),
    # 不该命中的（测误报）
    ("他爱喝什么饮料", "用户在做桌面宠物项目", False),
    ("他作息怎么样", "用户偏好深色界面", False),
    ("今天天气怎么样", "用户使用 Electron 开发", False),
    ("周末去哪玩", "用户通常凌晨两点多睡", False),
]


def expand(query: str) -> str:
    """把查询里的同义词**追加**上去（不替换，保留原词）。"""
    extra: list[str] = []
    for key, words in SYNONYMS.items():
        if key in query:
            extra.extend(words)
    return f"{query} {' '.join(extra)}" if extra else query


def main() -> int:
    emb = HashingEmbedder(dim=256)
    threshold = 0.18

    print("=" * 92)
    print("纯标准库「同义词查询扩展」的效果（对照：不改动 vs 扩展后）")
    print("=" * 92)
    print(f"  同义词表：{len(SYNONYMS)} 个词条，共 {sum(len(v) for v in SYNONYMS.values())} 个映射")
    print()

    print(f"  {'查询':<20}{'目标':<26}{'原始':>8}{'扩展后':>8}{'期望':>6}  变化")
    print("  " + "-" * 88)

    base_ok = exp_ok = 0
    sem_base = sem_exp = 0
    false_base = false_exp = 0
    for q, target, want in CASES:
        s0 = emb.similarity(q, target)
        s1 = emb.similarity(expand(q), target)
        g0, g1 = s0 > threshold, s1 > threshold
        if g0 == want:
            base_ok += 1
        if g1 == want:
            exp_ok += 1
        if want:
            sem_base += 1 if g0 else 0
            sem_exp += 1 if g1 else 0
        else:
            false_base += 1 if g0 else 0
            false_exp += 1 if g1 else 0
        arrow = "→ 命中" if (g1 and not g0) else ("→ **丢失**" if (g0 and not g1) else ("→ 仍误报" if (g1 and not want) else ""))
        print(
            f"  {q:<20}{target:<26}{s0:>8.3f}{s1:>8.3f}{'是' if want else '否':>6}  {arrow}"
        )

    print()
    print(f"  判定正确   原始 {base_ok}/{len(CASES)}   ->   扩展后 {exp_ok}/{len(CASES)}")
    print(f"  该命中的   原始 {sem_base}/5   ->   扩展后 {sem_exp}/5")
    print(f"  误报       原始 {false_base}/4   ->   扩展后 {false_exp}/4")
    print()
    print("=" * 92)
    print("怎么读这份数据")
    print("=" * 92)
    print("  · 「该命中的」提升明显、且「误报」不增加 -> 纯标准库方案有实用价值")
    print("  · 若误报增加 -> 扩展词把噪音带进来了，要先收窄同义词表")
    print("  · ⚠️ 但要记住它的**根本局限**：同义词表靠人工枚举，")
    print("    遇到表里没有的说法（换个领域、换个说法）就完全失效 ——")
    print("    这是「查表」和「真语义」的本质差距，不是调参能补的。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
