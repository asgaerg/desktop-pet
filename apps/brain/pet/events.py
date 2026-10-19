r"""随机事件（2026-10-17，用户列为**重点功能**）。

## 用户的原话

> 「不定时的随机事件也写进去，比如：想和用户一起打游戏（后续会做和用户一起打游戏
>   的功能，先留个路口）、大声唱用户喜欢的歌搞怪（后续会做拉起用户音乐 app 读取
>   喜欢歌曲唱几段歌词的功能，先留个路口）、去网上搜索哲学语句和用户交流体验、
>   去网上观看自己喜欢的小说（后续阅读书籍小说模块会做，先留个路口），
>   做这些事也会相应的增加玩伴对用户的印象、玩伴的情绪变化，把这些记录下来，
>   顺便将随机事件列为重点功能，后面继续添加。」

## 这个模块解决什么

桌宠如果只是"你说话我回答"，它就是个带立绘的输入框。
**它得有自己的事要做** —— 你没理它的时候，它去搜了一句哲学、想拉你打游戏、
或者把你喜欢的歌大声唱跑调。这样它才像个活物。

## 🔴 核心设计：能力路口（capability hook）

用户明确说"先留个路口"，所以事件分成两种状态：

```
needs 全部满足  -> 可以触发（dormant=False）
needs 缺任何一个 -> 注册着、显示着、但**永不触发**（dormant=True）
```

`needs` 是一组能力名，来自 `CAPABILITIES`。目前所有需要外部能力的都还是路口（`web_search` 能力表里为真，但 2026-10-18 起**没有事件用它** —— 哲学事件已删除，第二版由用户自己设计）。
另外三个是**未来功能的接口位**：

| 能力 | 对应未来功能 | 现在 |
|---|---|---|
| `web_search` | 已有（MCP 搜索） | ✅ 可用 |
| `music_app` | 拉起音乐 App 读喜欢的歌 | ❌ 路口 |
| `coop_game` | 和用户一起打游戏 | ❌ 路口 |
| `reading` | 阅读书籍/小说模块 | ❌ 路口 |

⚠️ **为什么要显式写"路口"而不是直接不写那几个事件**：
写在注册表里，`list_events()` 就能告诉用户"它还没这个本事"，
以后实现功能时只需把 `CAPABILITIES["music_app"] = True`，
**不用动事件逻辑**。这就是"路口"的价值 —— 改一处，通一条路。

## 事件做了什么就影响什么

```
每个事件带一组 delta：
  mood / energy / intimacy  -> 作用于 EmotionState（情绪变化）
  attention                 -> 作用于注意力（做完这件事更沉浸在自己这边）
  impression                -> 写一条印象（"我对你"的认知被这件事改变）
  detail                    -> 记进 event_log（用户能看到它偷偷做了什么）
```

⚠️ 所有 delta 都是**小量**（0.02~0.15）。理由与 `EmotionState` 一致：
"一轮对话不改人格"。随机事件应该制造**涟漪**，不是人格重塑。
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from .protocol import clamp01

# ── 能力表：True = 现在就能做 ──
#
# 🔴 这是唯一的"开关面板"。未来做完一个功能，把对应项改成 True 即可，
#    不需要改任何事件定义。
CAPABILITIES: dict[str, bool] = {
    "web_search": True,     # 已有 MCP 搜索
    "music_app": False,     # TODO: 拉起音乐 App，读用户喜欢的歌，唱几段
    "coop_game": False,     # TODO: 和用户一起打游戏
    "reading": False,       # TODO: 阅读书籍 / 小说模块
}

# 能力的人话名字（给 `list_events()` 和设置页用）
CAPABILITY_LABELS: dict[str, str] = {
    "web_search": "联网搜索",
    "music_app": "拉起音乐 App",
    "coop_game": "一起打游戏",
    "reading": "阅读书籍小说",
}


@dataclass
class EventSpec:
    """一个随机事件的定义。新增事件 = 往 `EVENTS` 里加一条。"""

    kind: str                        # 唯一 id
    label: str                       # 中文名（日志 / 提示词 / 设置页）
    # 需要哪些能力；空 = 现在就能做
    needs: tuple[str, ...] = ()
    # 基础权重（越大越常发生）
    weight: float = 1.0
    # 冷却：这个事件多久内不重复
    cooldown_s: float = 3600.0
    # 情绪 / 注意力影响
    mood_delta: float = 0.0
    energy_delta: float = 0.0
    intimacy_delta: float = 0.0
    attention_delta: float = 0.0
    # 产生的印象（None = 不产生）
    impression: Optional[str] = None
    # 它的"行为描述"；handler 返回非空时覆盖
    detail: str = ""
    # 未来功能的说明（路口）
    todo: str = ""
    # 可选：真正的执行器。`ctx` 里给 rng / interests / recent_events 等
    handler: Optional[Callable[[dict[str, Any]], Awaitable[Optional[str]]]] = None

    @property
    def dormant(self) -> bool:
        """缺能力的"路口"事件：注册着但永不触发。"""
        return any(not CAPABILITIES.get(name, False) for name in self.needs)

    def missing(self) -> list[str]:
        return [n for n in self.needs if not CAPABILITIES.get(n, False)]


def _specs() -> list[EventSpec]:
    """事件注册表。**加事件只改这里。**"""

    return [
        EventSpec(
            kind="music_sing",
            label="大声唱你喜欢的歌搞怪",
            needs=("music_app",),
            weight=1.2,
            cooldown_s=3600.0,
            mood_delta=0.10,
            energy_delta=-0.04,
            intimacy_delta=0.04,
            attention_delta=-0.10,   # 唱歌是"对着你"的，注意力反而在你身上
            impression="它记住了你爱听什么，还愿意拿这个逗你笑",
            detail="它扯着嗓子唱了你喜欢的歌，还故意跑调",
            todo="拉起音乐 App / 读用户收藏歌单 / 取几段歌词",
        ),
        EventSpec(
            kind="coop_game",
            label="想和你一起打游戏",
            needs=("coop_game",),
            weight=1.1,
            cooldown_s=5400.0,
            mood_delta=0.08,
            intimacy_delta=0.05,
            attention_delta=0.08,
            impression="它开始把你当成能一起玩的人了",
            detail="它在边上跃跃欲试，想跟你一起玩",
            todo="和用户一起打游戏（联机/同屏/代打某一段）",
        ),
        EventSpec(
            kind="reading",
            label="去网上看自己喜欢的小说",
            needs=("reading",),
            weight=1.0,
            cooldown_s=7200.0,
            mood_delta=0.07,
            energy_delta=-0.03,
            attention_delta=0.18,
            impression="它有自己想看的东西，不是为了你才去看的",
            detail="它自己找了点东西看，看得入神",
            todo="阅读书籍/小说模块",
        ),
        # ── 不需要新能力、现在就能做的一个 ──
        #
        # 用户没点名，但"重点功能"需要一个**当前可用**的事件来跑通整条链路，
        # 否则全部 dormant，等于没实现。这个事件只用已有的记忆/兴趣。
        EventSpec(
            kind="recall_interest",
            label="翻自己在意的东西",
            needs=(),
            weight=0.8,
            cooldown_s=3600.0,
            mood_delta=0.05,
            attention_delta=0.15,
            impression="它心里存着点自己的事，不全是你",
            detail="它翻出自己在意的东西，发了会儿呆",
        ),
    ]


EVENTS: list[EventSpec] = _specs()
_BY_KIND: dict[str, EventSpec] = {e.kind: e for e in EVENTS}


def get_event(kind: str) -> Optional[EventSpec]:
    return _BY_KIND.get(kind)


def list_events() -> list[dict[str, Any]]:
    """给设置页 / 诊断用：哪些能用、哪些是路口。"""
    out: list[dict[str, Any]] = []
    for spec in EVENTS:
        out.append(
            {
                "kind": spec.kind,
                "label": spec.label,
                "weight": spec.weight,
                "cooldown_s": spec.cooldown_s,
                "needs": list(spec.needs),
                "dormant": spec.dormant,
                "missing": spec.missing(),
                "todo": spec.todo,
            }
        )
    return out


@dataclass
class EventOutcome:
    """一次事件的执行结果。"""

    spec: EventSpec
    detail: str
    impression: Optional[str] = None
    mood_delta: float = 0.0
    energy_delta: float = 0.0
    intimacy_delta: float = 0.0
    attention_delta: float = 0.0


class EventEngine:
    """
    决定"现在做不做一件事""做哪件"。

    ## 什么时候不该触发

    ```
    刚交互过（用户正在聊）      -> 不触发（它该回应你，不是走神去做自己的事）
    全局最小间隔没到            -> 不触发（避免连着冒事件）
    这个事件冷却中              -> 不触发
    安静时段（quiet_hours）     -> 不触发
    触发概率没抽中              -> 不触发
    ```

    ## 为什么是"概率"而不是"定时"

    用户原话是**不定时**。如果每 30 分钟固定来一次，用户会察觉出规律，
    反而显得假。所以每次 tick 抽一次，`chance` 与活泼度挂钩：

    ```
    chance = base_chance * (0.5 + vitality)
    ```

    活泼度高 = 更常做自己的事。这也把事件系统和活泼度打通了。
    """

    def __init__(
        self,
        *,
        base_chance: float = 0.02,
        min_gap_s: float = 600.0,
        idle_before_s: float = 300.0,
        quiet_hours: Optional[list[Any]] = None,
        max_per_day: int = 12,
        rng: Optional[random.Random] = None,
    ) -> None:
        self.base_chance = clamp01(base_chance)
        self.min_gap_s = float(min_gap_s)
        self.idle_before_s = float(idle_before_s)
        self.quiet_hours = list(quiet_hours or [])
        self.max_per_day = int(max_per_day)
        self.rng = rng or random.Random()
        # 运行时统计（诊断用）
        self.fired = 0
        self.skipped: dict[str, int] = {}

    # ---------------------------------------------------------------- 判定

    def in_quiet_hours(self, now: float) -> bool:
        """
        安静时段判断。`quiet_hours` 支持 `[23, 8]`（跨零点）或 `[[1, 6]]`。

        ⚠️ 跨零点必须单独处理：`23 <= hour < 8` 这种写法在 Python 里恒为 False，
        是个很常见的静默 bug。
        """
        if not self.quiet_hours:
            return False
        try:
            hour = time.localtime(now).tm_hour
        except (OSError, ValueError):
            return False

        pairs: list[tuple[int, int]] = []
        raw = self.quiet_hours
        if all(isinstance(x, (int, float)) for x in raw) and len(raw) == 2:
            pairs.append((int(raw[0]), int(raw[1])))
        else:
            for item in raw:
                if isinstance(item, (list, tuple)) and len(item) == 2:
                    pairs.append((int(item[0]), int(item[1])))
        for start, end in pairs:
            if start <= end:
                if start <= hour < end:
                    return True
            else:  # 跨零点，如 23 -> 8
                if hour >= start or hour < end:
                    return True
        return False

    def eligible(
        self,
        spec: EventSpec,
        *,
        now: float,
        last_interaction_at: float,
        last_event_at: float,
        events_today: int,
        in_conversation: bool = False,
    ) -> tuple[bool, str]:
        """
        这个事件现在能不能做。返回 `(能否, 原因)` —— 原因用来写日志/测试。

        ⚠️ 返回原因而不是只返回 bool：事件不触发时**必须能解释为什么**，
        否则用户会觉得"它怎么从来不做自己的事"却查不出原因。
        """
        if spec.dormant:
            return False, f"dormant:缺{'+'.join(spec.missing())}"
        if in_conversation:
            return False, "in_conversation"
        if self.in_quiet_hours(now):
            return False, "quiet_hours"
        if now - last_interaction_at < self.idle_before_s:
            return False, "not_idle_enough"
        if now - last_event_at < self.min_gap_s:
            return False, "global_gap"
        if events_today >= self.max_per_day:
            return False, "daily_cap"
        return True, "ok"

    def pick(
        self,
        *,
        now: float,
        last_interaction_at: float,
        last_event_at: float,
        events_today: int,
        recent_kinds: Optional[set[str]] = None,
        last_by_kind: Optional[dict[str, float]] = None,
        in_conversation: bool = False,
        vitality: float = 0.6,
    ) -> tuple[Optional[EventSpec], str]:
        """
        抽一个事件。返回 `(事件或 None, 原因)`。

        `last_by_kind` 用来判单个事件的冷却；`recent_kinds` 用来避免连续重复。
        """
        # ① 概率闸（不定时的来源）
        chance = clamp01(self.base_chance * (0.5 + clamp01(vitality)))
        if self.rng.random() > chance:
            return None, "chance"

        # ② 收集候选
        candidates: list[EventSpec] = []
        reasons: list[str] = []
        for spec in EVENTS:
            ok, why = self.eligible(
                spec,
                now=now,
                last_interaction_at=last_interaction_at,
                last_event_at=last_event_at,
                events_today=events_today,
                in_conversation=in_conversation,
            )
            if not ok:
                reasons.append(f"{spec.kind}:{why}")
                continue
            # 单事件冷却
            last = (last_by_kind or {}).get(spec.kind, 0.0)
            if now - last < spec.cooldown_s:
                reasons.append(f"{spec.kind}:cooldown")
                continue
            # 刚做过的不马上做（哪怕冷却过了）
            if recent_kinds and spec.kind in recent_kinds:
                reasons.append(f"{spec.kind}:recent")
                continue
            candidates.append(spec)

        if not candidates:
            self.skipped["no_candidate"] = self.skipped.get("no_candidate", 0) + 1
            return None, "no_candidate"

        # ③ 按权重抽
        total = sum(max(0.0, c.weight) for c in candidates)
        if total <= 0:
            return None, "zero_weight"
        roll = self.rng.random() * total
        acc = 0.0
        chosen = candidates[-1]
        for spec in candidates:
            acc += max(0.0, spec.weight)
            if roll <= acc:
                chosen = spec
                break
        return chosen, "ok"

    async def run(
        self, spec: EventSpec, ctx: dict[str, Any]
    ) -> Optional[EventOutcome]:
        """
        真正执行。`handler` 返回 None = 没做成（不记录、不产生效果）。

        ⚠️ 没做成要**什么都不留下**：否则会出现"它说我搜了哲学句子"
        但其实搜索失败了 —— 那是编造，用户会当场发现。
        """
        detail = spec.detail
        if spec.handler is not None:
            try:
                produced = await spec.handler(ctx)
            except Exception:  # noqa: BLE001 —— 事件失败绝不能影响对话
                return None
            if produced is None:
                return None
            detail = produced
        if not detail:
            return None
        self.fired += 1
        return EventOutcome(
            spec=spec,
            detail=detail,
            impression=spec.impression,
            mood_delta=spec.mood_delta,
            energy_delta=spec.energy_delta,
            intimacy_delta=spec.intimacy_delta,
            attention_delta=spec.attention_delta,
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            "fired": self.fired,
            "skipped": dict(self.skipped),
            "events": list_events(),
        }
