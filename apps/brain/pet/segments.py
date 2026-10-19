r"""「一段交流」的边界（2026-10-17）。

## 用户的要求

> 「一大段关联的交流结束后 AI 可自发提炼和强化」

所以需要一个"段"的概念：**段内**是连着聊的，**段结束**才做一次
"提炼印象 + 强化记忆"。不能每轮都做 —— 那是白烧钱，而且印象会被
单句反复强化成偏见。

## 段什么时候结束

```
① 时间断点：距上次互动 >= gap_s（默认 10 分钟）
② 轮数断点：段内累积 >= max_turns（默认 8 轮）
```

两个条件**任一满足**就算结束。为什么两条都要：

```
只有时间断点  -> 一口气聊 60 轮也不结束，印象永远不提炼
只有轮数断点  -> 聊两句就走，段一直在，从不结束
```

⚠️ 还有第三种"结束"：**用户关掉 App**。那种情况在 `shutdown()` 里补一次
收尾（见 gateway），否则"今天聊了一晚上"的印象永远不落地。

## 为什么单独一个类而不是几个变量散在 gateway

因为"段"有**状态 + 判定 + 收尾**三件事，散在对话流程里很容易漏
（比如只在一处 `+= 1`，另一处忘了重置）。而且这样能被假时钟精确单测。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class SegmentTracker:
    """跟踪"当前这一段交流"的轮数、起始时间与命中的记忆。"""

    # 距上次互动多久算"这一段结束了"（秒）
    gap_s: float = 600.0
    # 段内最多多少轮，超过就切段
    max_turns: int = 8
    # 段内至少要几轮才值得提炼（太短的段提炼不出东西）
    min_turns: int = 2

    # 段内状态
    turns: int = 0
    started_at: float = 0.0
    last_at: float = 0.0
    # 段内被检索命中的记忆 id（强化用）
    hit_ids: list[int] = field(default_factory=list)
    # 段内的对话（(role, text)），提炼印象用
    turns_text: list[tuple[str, str]] = field(default_factory=list)

    # 统计（诊断）
    segments_done: int = 0

    # ---------------------------------------------------------------- 记录

    def begin(self, now: float) -> None:
        """开一段新的。"""
        self.turns = 0
        self.started_at = now
        self.last_at = now
        self.hit_ids = []
        self.turns_text = []

    def add_turn(
        self,
        now: float,
        *,
        user_text: str = "",
        reply_text: str = "",
        hit_ids: Optional[list[int]] = None,
    ) -> None:
        """记一轮。`hit_ids` 是这一轮记忆检索命中的 id。"""
        if self.turns == 0:
            self.started_at = now
        self.turns += 1
        self.last_at = now
        if user_text:
            self.turns_text.append(("user", user_text))
        if reply_text:
            self.turns_text.append(("assistant", reply_text))
        # 段内的对话留一截就够（提炼只看最近这段）
        if len(self.turns_text) > 40:
            del self.turns_text[:-40]
        for mid in hit_ids or []:
            if mid not in self.hit_ids:
                self.hit_ids.append(int(mid))

    # ---------------------------------------------------------------- 判定

    def should_close(self, now: float) -> tuple[bool, str]:
        """
        这一段该不该收尾。返回 `(是否, 原因)`。

        原因会进日志 —— "它为什么这时候提炼了印象"要能查。
        """
        if self.turns <= 0:
            return False, "empty"
        if now - self.last_at >= self.gap_s:
            return True, "time_gap"
        if self.turns >= self.max_turns:
            return True, "max_turns"
        return False, "ongoing"

    def worth_consolidating(self) -> bool:
        """轮数太少就不值得提炼（"聊了两句"没有可观察的）。"""
        return self.turns >= self.min_turns

    # ---------------------------------------------------------------- 收尾

    def take(self) -> dict:
        """
        取走这一段的内容并**重置**。返回给调用方去做提炼/强化。

        ⚠️ 先取走再处理：如果提炼过程中又来了新一轮，
        那些内容属于**下一段**，不该被这次吃掉。
        """
        snapshot = {
            "turns": self.turns,
            "started_at": self.started_at,
            "last_at": self.last_at,
            "hit_ids": list(self.hit_ids),
            "turns_text": list(self.turns_text),
        }
        self.segments_done += 1
        self.turns = 0
        self.hit_ids = []
        self.turns_text = []
        return snapshot

    def snapshot(self) -> dict:
        return {
            "turns": self.turns,
            "hit_ids": len(self.hit_ids),
            "segments_done": self.segments_done,
        }
