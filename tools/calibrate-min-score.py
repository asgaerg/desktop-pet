"""标定 `memory.min_score` —— bge 的相似度分布与哈希**完全不同**。

## 为什么必须重新标定

`min_score: 0.18` 是**为哈希实现调的**。实测：

    哈希：无关句对 ≈ 0（-0.05 ~ 0.05）
    bge ：无关句对 ≈ 0.22 ~ 0.33      ← 整体抬高了

原因是 mean-pooled 的句向量**天然有较高的基线相似度**（它们都在
同一个语义空间里，任何两句话都共享一部分"通用语义"）。

所以沿用 0.18 的后果是：**什么查询都返回全部记忆** ——
向量路退化成"把库全倒给模型"，白占提示词、还稀释了真正相关的信息。

## 标定方法

量两组分布，找一个能分开它们的阈值：
  · **正例**：查询与目标确实相关
  · **负例**：查询与目标无关

然后取两者的分界（并留一点余量）。
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

from pet.embedding import build_embedder  # noqa: E402
from pet.embedding_local import model_dir_for  # noqa: E402

# 正例：查询 <-> 相关事实
POS: list[tuple[str, str]] = [
    ("他爱喝什么饮料", "用户每天早上喝一杯冰美式"),
    ("他作息怎么样", "用户通常凌晨两点多睡"),
    ("他平时用什么做开发", "用户使用 Electron 开发"),
    ("界面配色是什么风格", "用户偏好深色界面"),
    ("他最近在忙什么", "用户在做桌面宠物项目"),
    ("他喝咖啡吗", "用户每天早上喝一杯冰美式"),
    ("他熬夜吗", "用户通常凌晨两点多睡"),
    ("他做什么项目", "用户在做桌面宠物项目"),
    ("我用什么写代码", "用户使用 Electron 开发"),
    ("他喜欢什么颜色", "用户偏好深色界面"),
]

# 负例：查询 <-> 无关事实（**刻意跨领域**）
NEG: list[tuple[str, str]] = [
    ("他爱喝什么饮料", "用户在做桌面宠物项目"),
    ("他爱喝什么饮料", "用户使用 Electron 开发"),
    ("他作息怎么样", "用户偏好深色界面"),
    ("他作息怎么样", "用户在做桌面宠物项目"),
    ("他平时用什么做开发", "用户偏好深色界面"),
    ("他平时用什么做开发", "用户通常凌晨两点多睡"),
    ("界面配色是什么风格", "用户通常凌晨两点多睡"),
    ("界面配色是什么风格", "用户在做桌面宠物项目"),
    ("今天天气怎么样", "用户使用 Electron 开发"),
    ("周末去哪玩", "用户通常凌晨两点多睡"),
    ("他养宠物吗", "用户在做桌面宠物项目"),
    ("他喜欢什么音乐", "用户偏好深色界面"),
    ("午饭吃什么", "用户使用 Electron 开发"),
    ("他几点上班", "用户在做桌面宠物项目"),
]


def main() -> int:
    emb = build_embedder({"provider": "local", "dim": 512}, project_root=ROOT)
    name = type(emb).__name__
    print("=" * 80)
    print(f"min_score 标定（嵌入器：{name}）")
    print("=" * 80)

    pos = sorted(emb.similarity(q, t) for q, t in POS)
    neg = sorted(emb.similarity(q, t) for q, t in NEG)

    def pct(vals: list[float], p: float) -> float:
        if not vals:
            return 0.0
        i = min(len(vals) - 1, max(0, int(round(p * (len(vals) - 1)))))
        return vals[i]

    print()
    print("  ── 正例分布（该命中的）──")
    print(f"    最低 {pos[0]:.3f}   P10 {pct(pos, .1):.3f}   中位 {pct(pos, .5):.3f}   最高 {pos[-1]:.3f}")
    print("  ── 负例分布（无关的）──")
    print(f"    最低 {neg[0]:.3f}   P90 {pct(neg, .9):.3f}   中位 {pct(neg, .5):.3f}   最高 {neg[-1]:.3f}")

    # 分界：负例的 P90 与正例的 P10 之间取中点
    hi_neg = pct(neg, 0.9)
    lo_pos = pct(pos, 0.1)
    print()
    if lo_pos > hi_neg:
        suggest = round((lo_pos + hi_neg) / 2, 3)
        print(f"  ✅ 可分：负例 P90 = {hi_neg:.3f} < 正例 P10 = {lo_pos:.3f}")
        print(f"     建议 min_score ≈ **{suggest}**（取两者中点）")
    else:
        suggest = round(pct(neg, 0.75), 3)
        print(f"  ⚠️ **重叠**：负例 P90 = {hi_neg:.3f} > 正例 P10 = {lo_pos:.3f}")
        print(f"     没有任何阈值能完美分开。挑一个偏向「高精度」的：**{suggest}**")
        print("     （宁可漏召回，也不要把无关记忆塞进提示词 —— 那会稀释真正相关的信息）")

    # 各阈值下的表现
    print()
    print("  ── 不同阈值下的表现 ──")
    print(f"    {'阈值':>7}{'正例召回':>10}{'负例误报':>10}")
    for th in [0.18, 0.25, 0.30, 0.35, 0.40, 0.45, 0.50]:
        r = sum(1 for v in pos if v > th) / len(pos)
        f = sum(1 for v in neg if v > th) / len(neg)
        mark = "  ← 当前" if abs(th - 0.18) < 1e-9 else ("  ← 建议" if abs(th - suggest) < 0.03 else "")
        print(f"    {th:>7.2f}{r:>9.0%}{f:>10.0%}{mark}")

    print()
    print("=" * 80)
    print("怎么读")
    print("=" * 80)
    print("  · 0.18（当前的、为哈希调的）会让**全部记忆**都通过 —— 形同没有过滤")
    print("  · 阈值越高越保守：漏召回好过把无关内容塞进提示词")
    print("  · 样本只有 24 对，**这个数字是起点不是终点**；")
    print("    对话量上来后要重跑（`--only BC` 的探针也能用来复核）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
