"""模拟注意力随机游走的行为，验证它不会飘到极端下不来（2026-10-17）。

## 为什么要模拟而不是"看看单测过没过"

用户定的是**随机游走**（每 3 分钟在 [-0.05, +0.07] 里随机取一个）。
随机游走有个隐蔽的性质：**均值不为 0 就会漂**。

    均值 = (-0.05 + 0.07) / 2 = **+0.01**

每 3 分钟平均 +0.01，24 小时 = 480 次 = 期望 +4.8 —— 远超量程 [0, 1]。
也就是说**纯随机游走会长期贴在 1.0**（注意力永远全在自己），
而那时用户说话也只能压低到 0.75，回不到"关注用户"的状态。

单测抓不到这个：单测测的是"一次漂移在范围内"，
而这个问题只在**几百次连续漂移**后暴露。

所以这个脚本跑三组模拟，把曲线打出来：

```
A. 纯随机游走（无任何约束）        -> 看它多久贴顶
B. 现有实现（天花板压缩 + 活泼度倾斜） -> 看分布是否健康
C. 真实使用节奏（白天聊天、夜里挂机）  -> 看会不会卡在某个极端
```

⚠️ 用真实 `random`，但固定 seed 保证可复现。

用法：
    python tools/simulate-attention.py
"""

from __future__ import annotations

import random
import statistics
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

ROOT = Path(r"F:\ai项目")
sys.path.insert(0, str(ROOT / "apps" / "brain"))

from pet.subjectivity import (  # noqa: E402
    ATTENTION_IDLE_INTERVAL_S,
    ATTENTION_IDLE_MAX,
    ATTENTION_IDLE_MIN,
    SubjectiveState,
)

STEP = ATTENTION_IDLE_INTERVAL_S  # 180s
HOURS = 24
STEPS = int(HOURS * 3600 / STEP)  # 480


def clamp01(v: float) -> float:
    return max(0.0, min(1.0, v))


def simulate_pure(steps: int, seed: int = 20261017) -> list[float]:
    """A. 纯随机游走：只 clamp，没有天花板压缩、没有活泼度倾斜。"""
    rng = random.Random(seed)
    value = 0.45
    out = [value]
    for _ in range(steps):
        value = clamp01(value + rng.uniform(ATTENTION_IDLE_MIN, ATTENTION_IDLE_MAX))
        out.append(value)
    return out


def simulate_real(steps: int, seed: int = 20261017, vitality: float = 0.6) -> list[float]:
    """B. 现有实现。用真实的 SubjectiveState，塞确定性随机源。"""
    rng = random.Random(seed)
    state = SubjectiveState(
        attention=0.45,
        vitality=vitality,
        _last_idle_at=0.0,
        _last_tick_at=0.0,
        _rand=lambda lo, hi: rng.uniform(lo, hi),
    )
    out = [state.attention]
    for i in range(steps):
        state.tick(now=(i + 1) * STEP)
        out.append(state.attention)
    return out


def simulate_day(seed: int = 20261017) -> tuple[list[float], int]:
    """C. 真实节奏：白天每 20 分钟聊一句，夜里 8 小时纯挂机。"""
    rng = random.Random(seed)
    state = SubjectiveState(
        attention=0.45,
        vitality=0.6,
        _last_idle_at=0.0,
        _last_tick_at=0.0,
        _rand=lambda lo, hi: rng.uniform(lo, hi),
    )
    out = [state.attention]
    # 一天 = 480 个 3 分钟片段。假设 09:00-23:00 活跃，23:00-09:00 挂机。
    chat_every = 7  # 约 21 分钟说一句
    night_steps = 0
    for i in range(STEPS):
        hour = (i * STEP / 3600.0) % 24
        is_night = hour >= 23 or hour < 9
        if is_night:
            night_steps += 1
        if (not is_night) and i % chat_every == 0:
            state.on_user_message("随便说一句")
            state.on_reply_done(20)
        state.tick(now=(i + 1) * STEP)
        out.append(state.attention)
    return out, night_steps


def report(name: str, series: list[float], note: str = "") -> None:
    n = len(series)
    at_ceiling = sum(1 for v in series if v >= 0.999)
    at_floor = sum(1 for v in series if v <= 0.001)
    last_100 = series[-100:]
    print(f"  ── {name} ──")
    if note:
        print(f"     {note}")
    print(f"     均值 {statistics.mean(series):.3f}   中位 {statistics.median(series):.3f}")
    print(f"     贴顶(>=0.999) {at_ceiling:>4}/{n} = {at_ceiling / n:5.1%}")
    print(f"     贴底(<=0.001) {at_floor:>4}/{n} = {at_floor / n:5.1%}")
    print(f"     最后 100 步均值 {statistics.mean(last_100):.3f}")
    print()


def main() -> int:
    print("=" * 78)
    print(f"  注意力随机游走模拟：{HOURS} 小时 / 每 {STEP:.0f}s 一次 = {STEPS} 次")
    print(f"  区间 [{ATTENTION_IDLE_MIN:+.2f}, {ATTENTION_IDLE_MAX:+.2f}]"
          f"  均值 {(ATTENTION_IDLE_MIN + ATTENTION_IDLE_MAX) / 2:+.3f}")
    print("=" * 78)
    print()

    pure = simulate_pure(STEPS)
    report("A. 纯随机游走（无护栏）", pure, "这就是不加约束会变成的样子")

    real = simulate_real(STEPS)
    report("B. 现有实现（天花板压缩 + 活泼度倾斜）", real)

    day, night_steps = simulate_day()
    report("C. 真实节奏（白天聊、夜里挂机）", day,
           f"其中 {night_steps} 步是夜间挂机")

    print("=" * 78)
    print("  结论")
    print("=" * 78)
    pure_top = sum(1 for v in pure[-100:] if v >= 0.999) / 100
    real_top = sum(1 for v in real[-100:] if v >= 0.999) / 100
    if pure_top > 0.3:
        print(f"  ✅ 证实了担忧：纯随机游走最后 100 步有 {pure_top:.0%} 贴在 1.0")
        print("     —— 所以天花板压缩是必要的")
    else:
        print(f"  （纯随机游走最后 100 步贴顶率 {pure_top:.0%}，比预期轻）")
    if real_top < 0.2:
        print(f"  ✅ 有护栏的实现没贴顶（最后 100 步 {real_top:.0%}）")
    else:
        print(f"  ⚠️ 有护栏仍然贴顶 {real_top:.0%} —— 护栏要调强")

    # ⚠️ B 是「24 小时**完全没有**用户说话」——它 95% 在"想自己"是**正确**的，
    #    不是缺陷。单看 B 会误判，所以档位分布要看 **C（有真实对话节奏）**。
    print()
    print("  ⚠️ B 场景（24h 无人说话）95% 在 self 档是**预期行为**：")
    print("     没人在，它当然想自己的事。这不是 bug，别照这个调参。")

    from pet.subjectivity import attention_band

    bands = {"user": 0, "mixed": 0, "self": 0}
    for v in day:
        bands[attention_band(v)] += 1
    total = len(day)
    print()
    print(f"  C 场景（真实节奏）三档分布："
          f"user {bands['user'] / total:.0%} · "
          f"mixed {bands['mixed'] / total:.0%} · "
          f"self {bands['self'] / total:.0%}")
    healthy = all(v > 0.05 for v in bands.values())
    print(f"  {'✅ 三档都待过（不会卡死在某一档）' if healthy else '⚠️ 有档位几乎没去过'}")

    # 用户说话后的即时反应（这才是用户能直接感知到的）
    print()
    print("  用户说话后的即时反应（硬钳制生效）：")
    from pet.subjectivity import ATTENTION_SELF_THRESHOLD

    worst = 0.0
    for start in (1.0, 0.95, 0.9, 0.8, 0.7, 0.5):
        s = SubjectiveState(attention=start, _last_idle_at=0.0, _last_tick_at=0.0)
        s.on_user_message("你好")
        worst = max(worst, s.attention)
    ok = worst < ATTENTION_SELF_THRESHOLD
    print(f"     最坏情况（注意力 1.0 时说话）-> {worst:.2f}，"
          f"档位 {'仍想自己 ❌' if not ok else '已回到关注用户 ✅'}")
    print("=" * 78)
    return 0


raise SystemExit(main())
