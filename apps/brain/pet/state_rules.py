"""状态规则：把"达到什么条件就切什么状态"做成事件模块里的**数据**（2026-10-25）。

## 为什么单独一个文件

用户原话：

> 「现在的情绪等主观层面的事件全部改成统一的事件模块，达到什么条件就触发什么事件，
> 不要塞在主观层里面写，抽出来和随机事件一起做成事件模块。」

以前这三条规则是 `state.py::tick()` 里一段**写死的 if 链**：

```python
if self.busy: return None
if energy < 0.4: ...          # 规则 1
if self.state == QUIET: ...   # 规则 2（粘滞）
if idle >= 30分钟: ...        # 规则 3
```

问题：
  · 加一条规则要改状态机本体；
  · 规则顺序只存在于**代码行序**里，没有任何声明；
  · 想关掉某条做不到；
  · 看不到"为什么没触发"；
  · 和随机事件是两套机制（那套有注册表、冷却、诊断，这套什么都没有）。

现在三条规则变成 `Rule` 数据，状态机退化成**纯状态容器**。

## 三条规则的语义（不能改，改了要重新问用户）

### 规则 1：精力 < 0.4 -> 安静

> 「精力这个低于0.4就转为安静状态」

### 规则 2：安静粘滞

> 「进入安静状态后精力高于0.4也仍旧是安静状态，
> 除非用户切换状态或者随机事件触发状态等等。」

🔴 **粘滞不是一条独立规则**，而是规则 1 的**结果**：
规则 1 只在 `state != QUIET` 时才动作，所以已经在安静里就不会再触发、
也不会自己出去。这一点极其容易写错 —— 如果写成"精力>0.4 -> 离开安静"，
就变成了双向门，用户要的单向门就没了。

所以这里**没有**"粘滞规则"，只有一条注释说明它靠规则 1 的条件保证。

### 规则 3：30 分钟没聊天 -> 无聊

> 「用户不与宠物聊天累计30分钟后自动进入无聊状态。」

⚠️ "聊天"只算**截屏对话 / 文本对话 / 语音对话**；
点它、拖它不算（用户第七轮明确）。所以 `last_chat_at` 只在真对话时更新。
"""

from __future__ import annotations

import logging
from typing import Any

from .events import Rule

log = logging.getLogger(__name__)

#: 精力低于这个值就转安静。用户原话：「低于0.4就转为安静状态」。
ENERGY_LOW = 0.4

#: 用户多久没聊天就进无聊（秒）。用户原话：「累计30分钟后自动进入无聊状态」。
IDLE_TO_BORED_S = 30 * 60.0

# 状态键（与 `state.py` 保持一致）
QUIET = "quiet"
BORED = "bored"
CURIOUS = "curious"
MONOLOGUE = "monologue"

STATE_NAMES: dict[str, str] = {
    QUIET: "安静",
    BORED: "无聊",
    CURIOUS: "好奇",
    MONOLOGUE: "自言自语",
}


def build_state_rules() -> list[Rule]:
    """造出三条状态规则。

    ## ctx 里有什么

    调用方（gateway）每 tick 组装一次：

        {
          "state": "bored",              # 当前状态
          "energy": 0.35,                # 精力
          "idle_s": 1800.0,              # 距上次"聊天"多少秒
          "switch": callable(to, reason) # 切状态的函数
        }

    ⚠️ `switch` 由调用方注入，而不是规则自己去 import 状态机 ——
    这样规则表是**纯数据 + 纯函数**，测试时塞一个假的 switch 就能验证。
    """

    async def _to_quiet(ctx: dict[str, Any]) -> str | None:
        energy = float(ctx.get("energy", 1.0))
        switch = ctx.get("switch")
        if switch is None:
            return None
        if ctx.get("state") == QUIET:
            # 已经在安静里：不动作（这就是"粘滞"）。
            return None
        if energy >= ENERGY_LOW:
            return None
        switch(QUIET, f"精力 {energy:.2f} < {ENERGY_LOW}")
        return f"精力 {energy:.2f} 低于 {ENERGY_LOW}，转为安静"

    def _when_low_energy(ctx: dict[str, Any]) -> bool:
        """规则 1 的条件。

        ⚠️ 条件里**必须**包含 `state != QUIET`：
        否则规则会每 tick 都"成立"，日志里刷满"该转安静了"而实际没动。
        把"是否已在安静"放进条件，`check_rules` 返回的列表才是干净的。
        """
        if ctx.get("state") == QUIET:
            return False
        try:
            return float(ctx.get("energy", 1.0)) < ENERGY_LOW
        except (TypeError, ValueError):
            return False

    async def _to_bored(ctx: dict[str, Any]) -> str | None:
        idle = float(ctx.get("idle_s", 0.0))
        switch = ctx.get("switch")
        if switch is None:
            return None
        if ctx.get("state") == BORED:
            return None
        switch(BORED, f"用户 {idle / 60.0:.0f} 分钟没聊天")
        return f"用户 {idle / 60.0:.0f} 分钟没聊天，转无聊"

    def _when_idle(ctx: dict[str, Any]) -> bool:
        if ctx.get("state") == BORED:
            return False
        # 安静粘滞：精力低的时候不该被"30 分钟没聊天"拉去无聊。
        # 用户原话「进入安静状态后精力高于0.4也仍旧是安静状态」——
        # 那么精力低时更不该离开安静。
        if ctx.get("state") == QUIET:
            return False
        try:
            return float(ctx.get("idle_s", 0.0)) >= IDLE_TO_BORED_S
        except (TypeError, ValueError):
            return False

    return [
        Rule(
            # priority 小的先判。
            #
            # 精力规则**必须在**无聊规则之前：精力低是"身体状态"，更紧迫。
            # 反过来的话，一个人 40 分钟没聊天 + 精力 0.1，
            # 会先被判成"无聊"而不是"累得安静了"。
            priority=10,
            kind="energy_low_quiet",
            label="精力低转安静",
            detail=f"精力 < {ENERGY_LOW} 时转为安静（进入后粘滞，不会自己出去）",
            when=_when_low_energy,
            then=_to_quiet,
        ),
        Rule(
            priority=20,
            kind="idle_to_bored",
            label="太久没聊天转无聊",
            detail=f"用户 {IDLE_TO_BORED_S / 60:.0f} 分钟没聊天时转无聊",
            when=_when_idle,
            then=_to_bored,
        ),
    ]
