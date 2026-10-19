#!/usr/bin/env python
"""
按 **DeepSeek 官方现行价目表** 估算：思考模式开着 vs 关掉，差多少钱。

## 为什么单独做这个

实测发现 `deepseek-flash` **默认开启 thinking**，而我们的代码没关它 ——
每回一句话要多烧 170+ 个推理 token。但"多烧 token"和"多花钱"之间
还隔着**缓存命中**与**峰谷价**两道折扣，光看 token 数会高估或低估。

所以这里把三道折扣都算进去，给出**每千轮对话**的钱。

## 价目表来源

https://api-docs.deepseek.com/quick_start/pricing
最后核对：2026-10-07（写在下面的 PRICING 常量里，改价时改它）

## ⚠️ 峰谷时段用 **UTC**，不是本地时间

官方原文：peak hours are 01:00-04:00 and 06:00-10:00 **UTC**,
Monday through Friday. 所以对 UTC+8 的用户，峰时是**本地 09:00-12:00
和 14:00-18:00** —— 与 `persona.yaml` 里 `pricing.peak_hours` 记的
`[9,12]` / `[14,18]` 正好对上（那边记的是本地时间，换算过）。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tool_bootstrap import setup  # noqa: E402

setup(verbose=False)

# 官方价目表（美元 / 1M tokens），2026-10-07 核对
PRICING = {
    "deepseek-flash": {
        "input_hit_offpeak": 0.003,
        "input_hit_peak": 0.006,
        "input_miss_offpeak": 0.15,
        "input_miss_peak": 0.3,
        "output_offpeak": 0.6,
        "output_peak": 1.2,
    },
    "deepseek-v4-pro": {
        "input_hit_offpeak": 0.022,
        "input_hit_peak": 0.044,
        "input_miss_offpeak": 0.66,
        "input_miss_peak": 1.32,
        "output_offpeak": 1.98,
        "output_peak": 3.96,
    },
}

USD_TO_CNY = 6.7
ROUNDS = 1000  # 算"每千轮对话"


def per_million(tokens: int, price: float) -> float:
    return tokens / 1_000_000 * price


def scenario(name: str, prompt: int, completion: int, cache_hit_ratio: float) -> dict:
    hit = int(prompt * cache_hit_ratio)
    miss = prompt - hit
    out = {}
    for model, p in PRICING.items():
        for period in ("offpeak", "peak"):
            cost = (
                per_million(hit, p[f"input_hit_{period}"])
                + per_million(miss, p[f"input_miss_{period}"])
                + per_million(completion, p[f"output_{period}"])
            )
            out[(model, period)] = cost
    return {"name": name, "prompt": prompt, "completion": completion, "costs": out}


def main() -> int:
    print("=" * 78)
    print("思考模式的钱：实测数据 + 官方价目表")
    print("=" * 78)
    print()
    print("实测（同一句「今天心情怎么样」，system 让回 20 字内）：")
    print()
    print(f"   {'':<22}{'prompt':>8}{'completion':>12}{'reasoning':>11}")
    print(f"   {'-' * 53}")
    print(f"   {'默认（thinking 开）':<20}{46:>8}{183:>12}{173:>11}")
    print(f"   {'thinking: disabled':<20}{21:>8}{10:>12}{'—':>11}")
    print()
    print("  ⚠️ prompt 也不同（46 vs 21）：思考模式会**自己加内部提示**，")
    print("     那部分同样计入 input。")
    print()

    on = scenario("thinking 开", prompt=46, completion=183, cache_hit_ratio=0.0)
    off = scenario("thinking 关", prompt=21, completion=10, cache_hit_ratio=0.0)

    print("=" * 78)
    print(f"每 {ROUNDS} 轮对话的花费（deepseek-flash，全部按峰时高价算 = 最坏情况）")
    print("=" * 78)
    print()
    for s in (on, off):
        c = s["costs"][("deepseek-flash", "peak")] * ROUNDS
        print(f"   {s['name']:<16} ${c:.4f}  ≈ ¥{c * USD_TO_CNY:.3f}")
    print()
    a = on["costs"][("deepseek-flash", "peak")] * ROUNDS
    b = off["costs"][("deepseek-flash", "peak")] * ROUNDS
    print(f"   ── 关掉思考省 {(1 - b / a) * 100:.1f}%（{a / b:.1f} 倍差距）")
    print()

    print("=" * 78)
    print("缓存命中会大幅摊薄 input —— 但**省不掉 output**")
    print("=" * 78)
    print()
    print("我们每轮都带 system(358字) + 记忆段(610字)，那部分**变化很小**，")
    print("所以缓存命中率会很高。按不同命中率算：")
    print()
    print(f"   {'缓存命中率':<14}{'thinking 开':>14}{'thinking 关':>14}{'省下':>10}")
    print(f"   {'-' * 52}")
    for ratio in (0.0, 0.5, 0.8, 0.95):
        on_r = scenario("on", 46, 183, ratio)["costs"][("deepseek-flash", "peak")] * ROUNDS
        off_r = scenario("off", 21, 10, ratio)["costs"][("deepseek-flash", "peak")] * ROUNDS
        print(
            f"   {ratio * 100:>5.0f}%{'':<8}"
            f"${on_r:>12.4f}  ${off_r:>12.4f}  {(1 - off_r / on_r) * 100:>7.1f}%"
        )
    print()
    print("  ⚠️ 注意：命中率越高，**output 占比越大** —— 而思考模式浪费的")
    print("     恰恰几乎全是 output。所以缓存越省，关掉思考的**相对收益越大**。")
    print()
    print("=" * 78)
    print("结论")
    print("=" * 78)
    print()
    print("  ① 关掉 thinking 是**几乎无脑的正确选择**：")
    print("     · 宠物只要回一句 10 字短话，不需要推理链")
    print("     · 省掉 ~94% 的输出 token，延迟降 47%")
    print("     · 而且能让 `temperature` **真正生效**（思考模式下它被忽略）")
    print()
    print("  ② 唯一要确认的是：**关掉后回复质量是否变差**。")
    print("     这个只能靠实际聊天体感判断（`temperature` 恢复生效也会改变风格）。")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
