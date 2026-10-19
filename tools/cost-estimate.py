"""按**实测用量**算这个桌宠的对话成本，并对比几个便宜选项。

## 实测用量（tools/measure-usage.py 量出来的）

    每轮输入 ≈ 600 token   （其中系统提示词 555 固定部分 —— 能命中缓存）
    每轮输出 ≈  80 token
    固定部分占输入的 92%

## 为什么"缓存命中率"是这里的关键

系统提示词每轮都要重发，但它**前缀不变** -> 命中提示缓存。
DeepSeek 的缓存价是未命中的 1/50（0.04 vs 2 元/百万）——
所以**同一份用量，有没有缓存能差一个数量级**。

⚠️ 这也是"别只看单价"的原因：某些便宜的第三方推理服务不一定有
同等的前缀缓存，实际账单可能反而更贵。

## 单价来源

DeepSeek 官方「模型 & 价格」页（2026-10 抓取）：
  · deepseek-flash   : 缓存命中 0.02~0.04 / 未命中 1~2 / 输出 4~8（空闲~高峰）
  · deepseek-v4-pro  : 缓存命中 0.15~0.30 / 未命中 4.5~9 / 输出 13.5~27
空闲时段 = 高峰的**一半**；高峰 = 周一至周五 9-12、14-18（北京时间）。
"""

from __future__ import annotations

IN_TOK = 600  # 每轮输入（实测）
OUT_TOK = 80  # 每轮输出（实测）
CACHED = 555  # 输入里能命中缓存的固定部分（系统提示词）
UNCACHED = IN_TOK - CACHED  # 真正新增的（用户消息 + 历史增量）

# 元 / 百万 token
MODELS = {
    "deepseek-flash（空闲）": dict(hit=0.02, miss=1.0, out=4.0),
    "deepseek-flash（高峰）": dict(hit=0.04, miss=2.0, out=8.0),
    "deepseek-v4-pro（空闲）": dict(hit=0.15, miss=4.5, out=13.5),
    "deepseek-v4-pro（高峰）": dict(hit=0.30, miss=9.0, out=27.0),
}


def cost(p: dict, turns: int = 1000) -> tuple[float, float, float]:
    hit = CACHED * turns / 1e6 * p["hit"]
    miss = UNCACHED * turns / 1e6 * p["miss"]
    out = OUT_TOK * turns / 1e6 * p["out"]
    return hit, miss, out


print("  ═══════════════════════════════════════════════════════════════")
print(f"  实测用量：每轮 输入 {IN_TOK} / 输出 {OUT_TOK} token")
print(f"            其中 {CACHED} token 是固定前缀（能命中缓存，占 {CACHED/IN_TOK:.0%}）")
print("  ═══════════════════════════════════════════════════════════════")
print()

for name, p in MODELS.items():
    h, m, o = cost(p)
    total = h + m + o
    print(f"  {name}")
    print(f"    缓存命中输入 {h:>7.3f} 元   （{CACHED*1000/1e6:.2f}M token × {p['hit']}）")
    print(f"    未命中输入   {m:>7.3f} 元   （{UNCACHED*1000/1e6:.2f}M token × {p['miss']}）")
    print(f"    输出         {o:>7.3f} 元   （{OUT_TOK*1000/1e6:.2f}M token × {p['out']}）")
    print(f"    {'-' * 40}")
    print(f"    每 1000 轮   {total:>7.2f} 元")
    print(f"    每天 100 轮  {total * 0.1:>7.3f} 元")
    print()

print("  ═══════════════════════════════════════════════════════════════")
print("  如果**没有**缓存（对比用 —— 说明缓存值多少钱）")
print("  ═══════════════════════════════════════════════════════════════")
print()
for name, p in MODELS.items():
    no_cache = IN_TOK * 1000 / 1e6 * p["miss"] + OUT_TOK * 1000 / 1e6 * p["out"]
    with_cache = sum(cost(p))
    print(f"  {name:<26} 无缓存 {no_cache:>7.2f} 元   有缓存 {with_cache:>7.2f} 元"
          f"   （缓存省了 {no_cache - with_cache:.2f} 元，{(1 - with_cache/no_cache)*100:.0f}%）")
