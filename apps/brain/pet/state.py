"""状态机：安静 / 无聊 / 好奇 / 自言自语（2026-10-23）。

## 这是什么

原来叫"活泼度"（`activity.overall` 0~1，用户拖滑杆切档）。
现在改成**宠物的内在状态**：由它自己按事件切换，用户顶多能拨一下。

用户原话（第七轮定稿）：

> 「主观层现在设计成1：状态，下辖安静、无聊、好奇、自言自语四个状态，
> 将活泼度替换成状态」

## 状态与情绪的关系（最容易搞混的地方）

```
情绪（energy / mood / intimacy）   连续、慢变    "我感觉怎么样"
状态（quiet / bored / ...）        离散、会切换   "我现在想干什么"
```

**不合并**：心情好可以安静地开心，心情差也可以赌气去查个明白。
状态由「情绪 + 时间 + 事件」共同推动，但它是独立的一层。

## 四条核心规则（全部来自用户口述）

### ① 精力 < 0.4 转安静，且**粘滞**

> 「精力这个低于0.4就转为安静状态，进入安静状态后精力高于0.4也仍旧是安静状态，
> 除非用户切换状态或者随机事件触发状态等等。」

粘滞是**单向门**：精力回升不代表"想动了"。累过之后就算缓过来，也还是想安静待着。

### ② 有事在做时，精力再低也不打断

> 「无聊状态下当宠物有在进行的事件时…就算精力下降至0.4以下，
> 仍然不变化状态至安静，必须等事件结束或功能退出，才能进行精力等判定转换状态。」

所以"低精力转安静"只在**没事做**（安静 / 无聊）时才生效。

### ③ 进无聊只有一条路：用户 30 分钟没聊天

> 「用户不与宠物聊天累计30分钟后自动进入无聊状态。」

⚠️ "聊天"只算**主动截屏对话 / 文本对话 / 语音对话**；
点它、拖它**不算**（用户第七轮明确）。所以调用方要用 `mark_chat()`，
不能用 `mark_interaction()`（那个是点/拖/说话全都算的）。

> 原来还有一条「精力>0.5 且 心情>0.5 持续 10 分钟进无聊」，第七轮已废弃删除。

### ④ 好奇由随机事件或用户触发，不是"无聊久了自然变"

> 「好奇状态由随机事件触发，或用户自己切换状态。」

### ⑤ 好奇/自言自语的退出要按精力结算

> 「若此时精力大于0.4，则返回到无聊状态，若结束好奇状态时精力低于0.4，
> 则按照精力判定回到安静状态，后续的粘滞依旧存在。」

## 为什么单独一个模块

状态机是**纯逻辑**（不碰网络、不碰数据库、不碰 UI），
所以能用一个假时钟精确测试每一次切换 —— 这是整个主观层里最该被测住的部分。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

# ── 状态键 ──────────────────────────────────────────────────────────────────
QUIET = "quiet"
BORED = "bored"
CURIOUS = "curious"
MONOLOGUE = "monologue"

#: 中文名（日志 / 提示词 / 设置页用）
STATE_NAMES: dict[str, str] = {
    QUIET: "安静",
    BORED: "无聊",
    CURIOUS: "好奇",
    MONOLOGUE: "自言自语",
}

ALL_STATES = (QUIET, BORED, CURIOUS, MONOLOGUE)

# ── 定稿常量（用户第七轮）──────────────────────────────────────────────────
#: 精力低于这个值就转安静。用户原话：「低于0.4就转为安静状态」。
ENERGY_LOW = 0.4

#: 用户多久没聊天就进无聊（秒）。用户原话：「累计30分钟后自动进入无聊状态」。
IDLE_TO_BORED_S = 30 * 60.0

#: 哪些状态算"有事在做"（精力低也不打断）。
#:
#: ⚠️ 好奇和自言自语都在里面 —— 用户原话：
#:    「好奇、自言自语期间除非用户切换状态，精力下降至0.4时不会将状态切换回安静」
BUSY_STATES = frozenset({CURIOUS, MONOLOGUE})

#: 「有事在做」的**兜底超时**（秒）。
#:
#: ## 为什么必须有这个
#:
#: 好奇/自言自语期间的正常退出，是由**业务流程**主动调的
#: （搜索结束 -> `set_state`、自言自语念完 -> `set_state`）。
#: 但只要有一个异常分支忘了调（比如搜索抛异常、TTS 卡住），
#: 宠物就会**永久卡在好奇状态** —— 表现是"它一整天都在搜东西/自言自语"，
#: 而且因为 `busy` 让它豁免了精力判定，连累了都不会安静下来。
#:
#: 所以这里加一条硬兜底：超过这个时长，`busy` 自动失效，
#: 让精力/无聊判定重新接管。**这是安全网，不是正常路径。**
BUSY_TIMEOUT_S = 20 * 60.0

#: 每个状态给提示词的一段说明。
#:
#: 写这些句子的原则与 `expression.py` 一致：**描述行为**而不是描述参数。
#: 中性/默认状态也要写 —— 状态是**始终存在**的（不像语言层旋钮能"关掉"）。
_STATE_PROMPTS: dict[str, str] = {
    QUIET: (
        "你现在很安静，不太想说话，也不想折腾。"
        "用户跟你说话你就应着，说完就安静待着，不要自己找话题。"
    ),
    BORED: (
        "你现在有点无聊，想找点事做但还没找到目标。"
        "会自己想想待会儿干什么，偶尔发个呆，想到什么说一句也行。"
    ),
    CURIOUS: (
        "你现在很好奇 —— 对某个具体的东西想知道更多。"
        "如果联网搜索开着，你会自己去查；没开的话你会缠着用户问。"
    ),
    MONOLOGUE: (
        "你现在心情很亢奋，沉浸在自个儿的念头里，会说给自己听。"
        "可以自言自语、可以哼歌，不用管用户有没有在听。"
    ),
}


@dataclass
class StateChange:
    """一次状态切换（给日志 / 事件流用）。

    做成可打印的，便于排查"它为什么突然不说话了" —— 状态切换是黑盒里
    最需要可观测的东西。
    """

    from_state: str
    to_state: str
    reason: str
    at: float = field(default_factory=time.time)

    def __str__(self) -> str:
        return (
            f"{STATE_NAMES.get(self.from_state, self.from_state)}"
            f" -> {STATE_NAMES.get(self.to_state, self.to_state)}"
            f"（{self.reason}）"
        )


class StateMachine:
    """四状态机。

    ## 用法

        sm = StateMachine(energy_provider=lambda: emotion.energy)
        sm.mark_chat()                  # 用户真的聊天了（截屏/文本/语音）
        change = sm.tick()              # 每个调度周期调一次
        if change: log(change)
        text = sm.describe()            # 拼进 system prompt

    ## 为什么精力用 provider 而不是传值

    状态机每次 `tick()` 都要读精力，而精力由 `EmotionState` 持有。
    传 provider 让它"要的时候自己取"，避免调用方忘了同步（这类漏同步的 bug
    在项目里出过好几次，比如活泼度镜像）。
    """

    def __init__(
        self,
        energy_provider: Optional[Callable[[], float]] = None,
        initial: str = BORED,
        clock: Callable[[], float] = time.time,
        idle_to_bored_s: float = IDLE_TO_BORED_S,
        energy_low: float = ENERGY_LOW,
        busy_timeout_s: float = BUSY_TIMEOUT_S,
    ) -> None:
        self._energy_provider = energy_provider
        self._clock = clock
        # ⚠️ 以下三个参数**保留但已不参与判定**（2026-10-25）：
        #    判定逻辑搬到了 `state_rules.py`，那边有自己的常量。
        #    保留它们是为了不破坏构造签名（测试和旧调用点还在传），
        #    但它们在这里只用于 `as_dict()` 的诊断输出。
        self._idle_to_bored_s = idle_to_bored_s
        self._energy_low = energy_low
        self._busy_timeout_s = busy_timeout_s
        # "是否占槽"的查询函数（由事件模块注入，见 `busy` 的说明）
        self._busy_provider: Optional[Callable[[], bool]] = None

        self.state = initial if initial in ALL_STATES else BORED
        self.started_at = clock()
        # 「上次聊天」的时刻。⚠️ 只有真正的对话才更新，点/拖不算。
        self.last_chat_at = clock()
        # 「当前正在进行的事件」—— **只用于提示词描述**（"你正在做：读小说"）。
        # ⚠️ 它不再是"是否 busy"的判据，判据只有事件模块的槽位。
        self.current_event = ""
        # 进入当前 busy 状态的时刻（诊断用）
        self._busy_since = clock()
        # 最近一次切换的原因（诊断用）
        self.last_reason = "启动"
        # 切过多少次（诊断用）
        self.changes = 0

    # ------------------------------------------------------------------ 外部输入

    def mark_chat(self) -> None:
        """用户**真的聊天了**（截屏对话 / 文本 / 语音）。

        ⚠️ 点它、拖它**不要**调这个 —— 用户第七轮明确"点它、拖它不算聊天"。
        """
        self.last_chat_at = self._clock()

    def begin_event(self, description: str) -> None:
        """标记"有事在做"。期间精力再低也不转安静。"""
        if not self.current_event:
            self._busy_since = self._clock()
        self.current_event = description or "进行中的事件"

    def end_event(self) -> None:
        """事件结束。**下一次 tick 才会**按精力结算（用户原话「必须等事件结束」）。"""
        self.current_event = ""

    def set_state(self, state: str, reason: str = "用户切换") -> Optional[StateChange]:
        """用户手动切 / 随机事件触发。"""
        if state not in ALL_STATES:
            return None
        change = self._switch(state, reason)
        if change is not None:
            self._busy_since = self._clock()
        return change

    def trigger_curious(self, reason: str = "随机事件") -> Optional[StateChange]:
        """随机事件触发好奇。用户原话：「好奇状态由随机事件触发」。"""
        if self.state == CURIOUS:
            return None
        change = self._switch(CURIOUS, reason)
        if change is not None:
            self._busy_since = self._clock()
        return change

    # ------------------------------------------------------------------ 内部

    def _energy(self) -> float:
        if self._energy_provider is None:
            return 1.0
        try:
            value = float(self._energy_provider())
        except Exception:  # noqa: BLE001
            return 1.0
        if value != value:  # NaN
            return 1.0
        return max(0.0, min(1.0, value))

    def _switch(self, state: str, reason: str) -> Optional[StateChange]:
        # ⚠️ 校验状态名：以前 `set_state` 在外面校验，现在 `switch` 是公开入口，
        #    校验必须收进来 —— 否则拼错的状态名会被真的写进 `self.state`，
        #    之后所有 `STATE_NAMES.get()` / 提示词都会落到兜底分支，
        #    表现是"它突然变成哑巴，而且菜单显示的状态名是空白"。
        if state not in ALL_STATES:
            return None
        if state == self.state:
            return None
        change = StateChange(self.state, state, reason, self._clock())
        self.state = state
        self.last_reason = reason
        self.changes += 1
        return change

    def switch(self, state: str, reason: str) -> Optional[StateChange]:
        """公开的切换入口（2026-10-25）。

        ## 状态机现在是**纯容器**

        以前 `tick()` 里写死了判定逻辑，状态机既管"该不该切"又管"怎么切"。
        用户要求把判定抽到事件模块，所以现在只剩"怎么切"：

            事件模块（events.py + state_rules.py）  判定该不该切
            状态机（本类）                          执行切换 + 粘滞语义 + 快照

        ⚠️ 用户手动切换、随机事件触发、条件规则触发**都走这个方法**，
        这样 `changes` / `last_reason` 的统计才是全的。
        """
        return self._switch(state, reason)

    @property
    def busy(self) -> bool:
        """是不是"有事在做"。

        ## 2026-10-25 改成读事件模块的槽位

        以前是"状态是好奇/自言自语 or 有 current_event"。那个定义**不准**：

            好奇状态可能已经搜完了、只是还没切状态 —— 那时它其实没事做，
            却因为 `state in BUSY_STATES` 豁免了精力判定。

        现在真正的"有事在做"是**事件占着槽**。状态只是结果。
        所以这里读事件模块（由调用方注入 provider），拿不到就退回旧逻辑。

        ⚠️ 保留 `current_event` 字段作为**提示词**用的描述（"你正在做：读小说"），
        但它不再是"是否 busy"的判据 —— 判据只有一个：槽位。
        """
        provider = self._busy_provider
        if provider is not None:
            try:
                return bool(provider())
            except Exception:  # noqa: BLE001 —— 读不到就当没事在做
                return False
        # 没有 provider 时的兜底（测试 / 独立使用）
        return bool(self.current_event)

    def set_busy_provider(self, provider: Optional[Callable[[], bool]]) -> None:
        """注入"事件模块是否占槽"的查询函数（见 `busy` 的说明）。"""
        self._busy_provider = provider

    # ------------------------------------------------------------------ 输出

    def describe(self) -> str:
        """拼进 system prompt 的一段。状态始终存在，所以**总会有内容**。"""
        text = _STATE_PROMPTS.get(self.state, "")
        if self.current_event:
            text += f"（你正在做：{self.current_event}。）"
        return text

    def as_dict(self) -> dict[str, Any]:
        """快照（事件 / 设置页 / 日志用）。"""
        now = self._clock()
        return {
            "state": self.state,
            "name": STATE_NAMES.get(self.state, self.state),
            "busy": self.busy,
            "current_event": self.current_event,
            "idle_since_chat_s": round(now - self.last_chat_at, 1),
            "last_reason": self.last_reason,
            "changes": self.changes,
        }


def plan_wait_seconds(has_tts: bool, rng: Any, segments: int = 9) -> list[float]:
    """规划「催用户」之间的 9 段等待（用户第八轮确认用归一化，算法 A）。

    ## 规则（用户第七轮口述）

    > 「这20和40秒我觉得用随机值好，最低不滴7秒，最高不高于60秒，
    > 语音与文本总等待时间设为250秒和400秒，分属9轮等待。」

    所以：单段 ∈ [7, 60]，共 9 段，**总时长**收敛到 250（开语音）/ 400（纯文本）。

    ## 为什么必须归一化

    纯随机取 9 个 [7,60] 的值，总和期望 301 秒、最大 540 秒 —— 完全不受控。
    用户要的是"总等待 250/400"，所以先随机、再按比例缩放。

    ## 两个坑（都踩得到）

    1. **夹取会破坏总和**：缩放后若有段超出 60 被夹回，总和就小于预算。
       所以夹取后要再摊一次差额（`_rebalance`）。
    2. **预算必须可达**：`segments × 7 <= target <= segments × 60`。
       越界直接抛 `ValueError`，而不是死循环 —— 将来有人把预算改成 50 秒
       会在调用点立刻炸出来，而不是把 sidecar 卡死。
    """
    target = 250.0 if has_tts else 400.0
    return plan_wait_for_target(target, rng, segments)


def plan_wait_for_target(target: float, rng: Any, segments: int = 9) -> list[float]:
    """`plan_wait_seconds` 的实现（可指定任意预算，便于测试边界）。"""
    lo, hi = 7.0, 60.0
    if segments <= 0:
        raise ValueError("segments 必须为正")
    if not (segments * lo <= target <= segments * hi):
        raise ValueError(
            f"预算 {target} 不可达：{segments} 段 × [{lo}, {hi}] 的范围是 "
            f"[{segments * lo}, {segments * hi}]"
        )

    def _rand() -> float:
        # 支持传 random.Random 实例，也支持传 random 模块
        if hasattr(rng, "uniform"):
            return float(rng.uniform(lo, hi))
        return lo

    raw = [_rand() for _ in range(segments)]
    total = sum(raw) or 1.0
    scaled = [max(lo, min(hi, v * target / total)) for v in raw]
    return _rebalance(scaled, target, lo, hi)


def _rebalance(values: list[float], target: float, lo: float, hi: float) -> list[float]:
    """把夹取造成的差额摊到还有余量的段上。

    ⚠️ 用**按余量比例**分配而不是平均分配：平均分会把本来已经贴边的段又推出界，
    于是要来回好几轮。按余量比例分配通常一轮就收敛。
    """
    for _ in range(8):  # 兜底轮数，正常一两轮就够
        diff = target - sum(values)
        if abs(diff) < 1e-6:
            break
        if diff > 0:
            room = [hi - v for v in values]
        else:
            room = [v - lo for v in values]
        total_room = sum(room)
        if total_room <= 1e-9:
            break  # 已经顶到边界，无解（理论上进不来，预算检查拦住了）
        values = [
            max(lo, min(hi, v + diff * (r / total_room)))
            for v, r in zip(values, room)
        ]
    return values
