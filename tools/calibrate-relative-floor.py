"""标定「相对地板」——为什么绝对阈值不够用。

## 问题（实测）

同一批记忆下（6 条事实）：

    该命中的查询最高分     0.398 ~ 0.589
    不该命中的查询最高分   0.322 ~ 0.418     <- **重叠区 0.36~0.42**

所以**没有任何单一绝对阈值**能分开：

    阈值 0.40 -> "他养狗吗"（0.418，无关）照样过；
                 "他家里养了什么"（0.398，相关）被误杀

## 为什么相对判定能分开

关键在于：**无关查询的分数是"平"的**（每条都差不多），
而**相关查询会有明显的尖峰**（对的那条远高于其余）。

    相关：「家里催他什么事」 -> 妈妈催相亲 0.589，其余 ~0.33   -> 尖峰
    无关：「他养狗吗」       -> 最高 0.418，其余 ~0.38          -> 平

所以判据应当是 **「明显高于本次查询自己的噪音地板」**，而不是绝对分数。

这个脚本量的是：用哪种相对规则、取什么比例，能把两组分开。
"""

from __future__ import annotations

import os
import statistics
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "apps" / "brain"))

_DLL = ROOT / "apps" / "desktop" / "pet-data" / "vendor" / "simple" / "libsimple-windows-x64"
if (_DLL / "dict" / "jieba.dict.utf8").is_file():
    os.chdir(_DLL)

from pet.memory2 import MemoryService  # noqa: E402


class Cfg:
    root = {"prompts": {}}
    pet_name = "小灵"
    memory = {
        "enabled": True, "path": "", "simple_dll": "auto",
        "recent_window_days": 30, "recent_inject_limit": 120,
        "top_k": 5, "max_chars": 200, "min_score": 0.25,
        "embedding": {"provider": "local", "dim": 512},
    }


FACTS = [
    "用户在做桌面宠物项目",
    "用户用 Python 写过后端但改成了 WebSocket",
    "用户想试试 Rust 的 Tauri",
    "用户的妈妈在催他去相亲",
    "用户周末约了朋友去爬山",
    "用户把猫送去洗澡了",
    "用户偏好深色界面",
    "用户每天早上喝一杯冰美式",
    "用户通常凌晨两点多睡",
    "用户使用 Electron 开发",
]

POS = ["他最近在搞什么技术", "他家里养了什么", "他周末有什么安排", "家里催他什么事",
       "他考虑过别的技术方案吗", "他爱喝什么饮料", "他作息怎么样", "他最近在忙什么",
       "他的界面是什么风格", "他用什么做开发"]
NEG = ["他喜欢吃什么菜", "他开什么车", "他做什么工作", "他今天心情怎么样", "他养狗吗",
       "他喜欢什么音乐", "他身高多少", "他手机什么牌子", "他住在哪个城市", "他结婚了吗"]


def stats_for(svc, q: str) -> tuple[float, float, float]:
    """返回 (最高分, 中位数, 最高分 / 中位数)。"""
    sims = sorted((svc.embedder.similarity(q, f) for f in FACTS), reverse=True)
    top = sims[0]
    med = statistics.median(sims)
    return top, med, (top / med if med > 0 else 0.0)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="pet-thr-"))
    Cfg.memory["path"] = str(tmp / "t.db")
    svc = MemoryService(Cfg())
    svc.remember_facts(FACTS, kind="fact", level="recent")

    print("=" * 82)
    print("相对地板标定（嵌入器 %s，%d 条事实）" % (svc.embedder_name(), len(FACTS)))
    print("=" * 82)

    print()
    print("  ── 相关查询 ──")
    print(f"    {'查询':<24}{'最高':>8}{'中位':>8}{'比值':>8}")
    pos_rows = []
    for q in POS:
        top, med, ratio = stats_for(svc, q)
        pos_rows.append((top, ratio, q))
        print(f"    {q:<24}{top:>8.3f}{med:>8.3f}{ratio:>8.2f}")

    print()
    print("  ── 无关查询 ──")
    print(f"    {'查询':<24}{'最高':>8}{'中位':>8}{'比值':>8}")
    neg_rows = []
    for q in NEG:
        top, med, ratio = stats_for(svc, q)
        neg_rows.append((top, ratio, q))
        print(f"    {q:<24}{top:>8.3f}{med:>8.3f}{ratio:>8.2f}")

    # ---- 比较三种判据的分辨力 ----
    print()
    print("=" * 82)
    print("三种判据下：能不能分开两组")
    print("=" * 82)

    def report(name: str, pos_vals: list[float], neg_vals: list[float], higher_is_hit: bool) -> None:
        ps, ns = sorted(pos_vals), sorted(neg_vals)
        lo_pos = ps[max(0, int(len(ps) * 0.1))]
        hi_neg = ns[min(len(ns) - 1, int(len(ns) * 0.9))]
        if higher_is_hit:
            over = lo_pos > hi_neg
            best = (lo_pos + hi_neg) / 2
        else:
            over = lo_pos < hi_neg
            best = (lo_pos + hi_neg) / 2
        mark = "✅ 可分" if over else "❌ 重叠"
        print()
        print(f"  {name}")
        print(f"    相关 P10 = {lo_pos:.3f}    无关 P90 = {hi_neg:.3f}    {mark}")
        if over:
            print(f"    -> 建议值 {best:.3f}")

    report("① 绝对最高分（现用）", [r[0] for r in pos_rows], [r[0] for r in neg_rows], True)
    report("② 最高分 / 中位数（相对）", [r[1] for r in pos_rows], [r[1] for r in neg_rows], True)

    # ---- 各相对阈值的实测表现 ----
    print()
    print("─" * 82)
    print("② 的阈值扫描（相对判据：最高分/中位数 > X 才算命中）")
    print("─" * 82)
    print(f"    {'X':>6}{'相关命中':>10}{'无关误报':>10}")
    for x in (1.2, 1.3, 1.4, 1.5, 1.6, 1.8, 2.0):
        hit = sum(1 for _t, r, _q in pos_rows if r > x) / len(pos_rows)
        fal = sum(1 for _t, r, _q in neg_rows if r > x) / len(neg_rows)
        print(f"    {x:>6.1f}{hit:>9.0%}{fal:>10.0%}")

    svc.close()
    import shutil

    shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
