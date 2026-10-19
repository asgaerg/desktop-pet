"""轻量入口：只算「天数窗口内有多少对话」，输出 JSON。**不加载任何重型依赖**。

## 为什么需要它（2026-10-08）

设置页要显示用户要的那行数字：

    最近 30 天: 共 90 条 / 4545 字 / ≈4545 token
                实际发送 4545 token（占预算 30000 的 15%、占窗口 1M 的 0.45%）

而设置页每次刷新都去起一个 Python 的话，**不能走 `pet.selftest` 那条路**
（它要 import 整个 pet 包，`faster_whisper` / torch 加起来 3 秒）——
拖个天数输入框等 3 秒才出数字，体验很糟。

所以这里**刻意只 import 两个轻的东西**：
`pet.config`（读 yaml）和 `pet.store`（SQLite 直查），
**不碰 voice / brains / gateway**。实测这个入口 < 200ms。

## 用法（主进程这么调）

    python -m pet.usage --config <persona.yaml> --days 30 --budget 30000 --json

输出一行 JSON（方便 Node 侧 `JSON.parse`）：

    {"days":30,"turns":90,"chars":4545,"tokens":4545,
     "sentTokens":4545,"truncated":false,
     "budget":30000,"budgetPct":15.2,"window":1000000,"windowPct":0.45}

## ⚠️ 口径必须与 `memory2.MemoryService.history_usage` 一致

两处算的是同一个东西（一边给设置页看，一边给 Gateway 灌回用）。
不一致的话会出现"界面说发 4545，实际发了别的"——那种不一致最难查。

所以这里复用**同一个 `recent_dialogue` 逻辑**：直接 import `pet.memory2`
里的实现。它虽然比 `pet.store` 重一点（要 embedding），但
`history_usage` 本身**不碰嵌入模型**（那要 `_has_local_embedder` 才走），
实测 import 得很快。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="pet.usage", description="统计对话历史用量")
    ap.add_argument("--config", required=True, help="persona.yaml 的路径")
    ap.add_argument("--days", type=int, default=30, help="天数窗口（0 = 不限）")
    ap.add_argument("--budget", type=int, default=0, help="token 预算（0 = 不限）")
    ap.add_argument("--json", action="store_true", help="输出一行 JSON（默认给人看的）")
    args = ap.parse_args(argv)

    from .config import load_config
    from .memory2 import MemoryService

    cfg = load_config(Path(args.config))
    brain = cfg.brain_profile()

    # 预算/窗口：命令行优先（设置页可能还没保存），否则读配置
    budget = args.budget or int(brain.get("history_token_budget") or 0)
    window = int(brain.get("context_window") or 0)
    days = args.days if args.days is not None else int((cfg.memory or {}).get("history_days") or 30)

    svc = MemoryService(cfg)
    try:
        usage = svc.history_usage(days, budget=budget)
    finally:
        svc.close()

    payload = {
        **usage,
        "budget": budget,
        "window": window,
        "budgetPct": round(usage["sentTokens"] / budget * 100, 1) if budget else None,
        "windowPct": round(usage["sentTokens"] / window * 100, 2) if window else None,
    }

    if args.json:
        # 一行 JSON，Node 侧直接 JSON.parse
        print(json.dumps(payload, ensure_ascii=False))
    else:
        label = f"{days} 天" if days else "不限天数"
        print(f"  {label}: 共 {usage['turns']} 条 / {usage['chars']} 字 / ≈{usage['tokens']} token")
        print(f"  实际发送 {usage['sentTokens']} token", end="")
        if usage["truncated"]:
            print("（⚠️ 已被预算截断）", end="")
        print()
        if budget:
            print(f"  占预算 {payload['budgetPct']}%", end="")
        if window:
            print(f"  占窗口 {payload['windowPct']}%", end="")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
