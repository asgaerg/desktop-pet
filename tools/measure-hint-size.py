"""量提示词里各段各占多少 —— 时间戳加上后这个问题才看得见。

## 为什么要量

用户要求「每条数据的时间加到每条数据上」之后，我把实际注入内容打印出来，
发现**最近记忆那一段有 400 多轮原文**，把检索结果淹在最后。

没加时间戳时看不出来（一堆无标识的行），加上 `[昨天]` 之后
"同样一句重复十几次"变得刺眼 —— 这正好印证了用户说的：

    「记忆的重复性，比如各种琐事和常说的话搜索出来会影响找回的相关结果」

## 量什么

  ① 各段的字符数 / 估算 token 数
  ② 最近记忆里有多少轮、多少是**近乎重复**
  ③ 与 DeepSeek 的上下文上限对比
"""

from __future__ import annotations

import os
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "apps" / "brain"))

_DLL = ROOT / "apps" / "desktop" / "pet-data" / "vendor" / "simple" / "libsimple-windows-x64"
if (_DLL / "dict" / "jieba.dict.utf8").is_file():
    os.chdir(_DLL)

from pet.config import load_config  # noqa: E402
from pet.memory2 import MemoryService  # noqa: E402


def main() -> int:
    cfg = load_config(ROOT / "config" / "persona.yaml")
    svc = MemoryService(cfg)

    print("=" * 80)
    print("提示词各段体积")
    print("=" * 80)

    mem = cfg.memory or {}
    print(f"  recent_window_days    {svc.recent_window_days()}")
    print(f"  recent_inject_limit   {(mem.get('recent_inject_limit'))}")
    print(f"  top_k                 {(mem.get('top_k'))}")

    hint = svc.inject_hint("我平时喝什么")
    print()
    print(f"  整段记忆提示词  {len(hint)} 字符")

    # 分段量
    print()
    print("  ── 按段落拆开看 ──")
    sections = hint.split("\n\n")
    for i, sec in enumerate(sections):
        first = sec.split("\n")[0][:40]
        n_lines = sec.count("\n") + 1
        print(f"    [{i}] {len(sec):>6} 字符 · {n_lines:>4} 行  {first}")

    # 最近记忆段：重复度分析
    print()
    print("  ── 最近记忆段的重复度（用户说的「琐事和常说的话」）──")
    since_lines = [ln for ln in hint.split("\n") if ln.startswith("[")]
    print(f"    带时间戳的行数  {len(since_lines)}")
    if since_lines:
        # 去掉时间戳前缀后统计重复
        bodies = [ln.split("] ", 1)[-1] for ln in since_lines]
        cnt = Counter(bodies)
        dup = [(n, t) for t, n in cnt.most_common(8) if n > 1]
        print(f"    不同的行         {len(cnt)}")
        print(f"    重复的行         {len(bodies) - len(cnt)}（占 {(len(bodies) - len(cnt)) / len(bodies):.0%}）")
        if dup:
            print("    出现最多的：")
            for n, t in dup:
                print(f"      ×{n:<3} {t[:60]}")

    # token 估算（中文 1 字 ≈ 1.25 token，见第十九节实测）
    print()
    print("  ── 估算 token（中文 1 字 ≈ 1.25，见第十九节实测）──")
    est = int(len(hint) * 1.25)
    print(f"    记忆段约     {est:>6} token")
    print(f"    DeepSeek 上限 64K token（deepseek-chat）")
    print(f"    占比         {est / 65536:.1%}")

    svc.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
