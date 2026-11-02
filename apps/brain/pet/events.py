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

import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from .protocol import clamp01

log = logging.getLogger(__name__)

# ── 能力表：True = 现在就能做 ──
#
# 🔴 这是唯一的"开关面板"。未来做完一个功能，把对应项改成 True 即可，
#    不需要改任何事件定义。
CAPABILITIES: dict[str, bool] = {
    "web_search": True,     # 已有 MCP 搜索
    "music_app": False,     # TODO: 拉起音乐 App，读用户喜欢的歌，唱几段
    "coop_game": False,     # TODO: 和用户一起打游戏
    # 🔴 2026-11-02 置为 True：小说阅读已经实现了（reading.py + reading_tools.py）。
    #
    # 这里一直是 False，所以 `reading` 事件是 `dormant`（注册着但永不触发）——
    # 阅读功能之前**只由 `_run_reading_forever` 那个独立循环驱动**，
    # 和事件模块没有关系。
    #
    # 用户现在要求把它并入自主行动：「将整个搜索、看小说的功能作为
    # 自主行动中搜索小说功能的模块」。
    "reading": True,
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
    # 这次事件最多占槽多久（秒）。None = 瞬时事件，不占槽。
    #
    # ⚠️ 只有"进行中"的事件才占槽（好奇搜索、看小说这类）。
    #    唱歌、想一起打游戏是**瞬时**的 —— 说完就完了，占槽会让它
    #    在接下来几分钟里什么都不能做。
    duration_s: Optional[float] = None

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
            # 🔴 2026-11-02 用户指定权重 7（和 community_chat 的 3 是 7:3）。
            #
            # 原话：「分成小黑盒3，小说7权重，小说搜索模块关闭时
            # 自然就只触发小黑盒杂谈事件了」。
            #
            # ⚠️ 权重要和 `community_chat` 一起看 —— 它们是这一对的两端。
            #    改一个就要回头看另一个，否则比例就偏了。
            weight=7.0,
            # 冷却比小黑盒长：读小说是"进行中"的重事件（要几十秒到几分钟），
            # 连着读会让宠物长时间不理用户。
            cooldown_s=7200.0,
            mood_delta=0.07,
            energy_delta=-0.03,
            attention_delta=0.18,
            impression="它有自己想看的东西，不是为了你才去看的",
            detail="它自己找了点东西看，看得入神",
            # ⚠️ 必须占槽且给足时间：读一本书可能几分钟（每章一个请求）。
            #    给短了会被超时兜底提前掐断，表现为"读到一半就不读了"。
            duration_s=10 * 60.0,
            # 🔴 执行器由 gateway 注入（`ctx["read_novel"]`）。
            #
            # 这里写 `None`，真正的绑定在 `_specs()` 之后由
            # `bind_handlers()` 完成 —— 因为 events.py 不该 import gateway
            # （那会循环依赖），而 handler 需要 Gateway 的模型/记忆/工具。
            handler=None,
        ),
        # ── 小黑盒杂谈（2026-11-02 加，用户指定 3 的权重）──
        #
        # 用户原话：「看小黑盒杂谈那个还是30分钟触发，不和这个在一个模块，
        # 那个属于是事件模块中用户30分钟不与宠物聊天，宠物自动触发」。
        #
        # 所以它是**轻事件**：只是去看看帖子、说两句，
        # 不下载、不落盘，几十秒内结束。
        #
        # ⚠️ 和 `reading` 的区别（用户特意强调要分开）：
        #
        #     reading        读小说 -> 要下载、占盘、几分钟
        #     community_chat 看杂谈 -> 只读网页、不落盘、几十秒
        #
        # 两者比例 7:3（用户指定）。关掉「小说阅读」模块后，
        # `reading` 因为 needs=("reading",) 而变成 dormant，
        # 只剩这个 —— 正是用户要的效果。
        EventSpec(
            kind="community_chat",
            label="去小黑盒看看大家在聊什么",
            needs=("web_search",),
            weight=3.0,
            cooldown_s=1800.0,
            mood_delta=0.05,
            attention_delta=0.12,
            impression="它会自己去看别人在聊什么，有自己的消遣",
            detail="它翻了翻社区里的杂谈，看得挺有意思",
            # 轻事件但也要占槽：读网页要几秒到几十秒，
            # 占槽期间不该同时去唱歌（用户要「同一时间不存在两个事件」）。
            duration_s=2 * 60.0,
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
        # ── 好奇的自主搜索（2026-10-25 从独立流程并进来）──
        #
        # 用户原话：「把好奇搜索也拿出来加入事件模块，主要是后面如果触发了
        # 其他事件，难不成每次都要写防止冲突方法，所以单独立成模块，
        # 要就调用，**同一时间不存在两个事件就可以了**。」
        #
        # ⚠️ `duration_s` 是这次并入的关键：搜索/催促是**进行中**的事件，
        #    要占槽。占着槽的时候别的随机事件抽不到、精力判定也不会打断它。
        #
        # ⚠️ 给 15 分钟：催 10 次最长要 400 秒（纯文本）+ 每次生成台词的时间，
        #    再加搜索那 5 轮（每轮可能 10~20 秒）。给短了会被超时兜底提前掐断。
        EventSpec(
            kind="curious_search",
            label="自己去查个明白",
            needs=("web_search",),
            weight=1.4,          # 比其它事件权重高：用户把它当重点功能
            cooldown_s=1800.0,
            mood_delta=0.08,
            energy_delta=-0.05,
            attention_delta=0.20,
            impression="它有自己的好奇心，会为了搞明白一件事去查",
            detail="它自己上网查了点东西",
            duration_s=15 * 60.0,
        ),
    ]


EVENTS: list[EventSpec] = _specs()
_BY_KIND: dict[str, EventSpec] = {e.kind: e for e in EVENTS}


def bind_handler(kind: str, ctx_key: str) -> None:
    """把某个事件的 handler 绑成"从 ctx 里取 `ctx_key` 并调用它"。

    ## 为什么需要这一层（2026-11-02）

    `EventSpec.handler` 是个 `handler(ctx) -> Optional[str]` 的函数。
    但小说阅读的 handler 需要 Gateway 的模型/记忆/工具 ——
    而 events.py **不能 import gateway**（循环依赖）。

    所以：events.py 只声明"我要一个 handler"，真正的实现在 Gateway；
    由 Gateway 在构造事件引擎时调 `bind_handler("reading", "read_novel")`，
    这里生成的函数只做一件事 —— 从 `ctx` 里把那个实现捞出来调。

    ## 为什么不直接在 `_specs()` 里写 lambda

    `_specs()` 是模块级函数，它拿不到 `ctx`（那是每次执行时才有）。
    而且写 lambda 的话"这个事件到底绑没绑上"在代码里看不出来 ——
    现在 `bind_handler` 被调用过就是绑上了，没调用就是 `handler is None`
    （事件照样占槽、照样返回 detail，只是不会真的去做事）。

    ⚠️ 如果 `ctx_key` 不在 ctx 里，返回 None（= 没做成，不记录）。
    这比抛 KeyError 好：抛了会被 `run()` 的 except 吞掉并记一条 ERROR，
    看起来像 bug，而实际只是"这个入口没接"。
    """

    async def _dispatch(ctx: dict[str, Any]) -> Optional[str]:
        # ⚠️ 必须是 `async def`：`EventEngine.run()` 里写的是
        #    `produced = await spec.handler(ctx)` —— 同步函数返回的
        #    普通值会让 `await` 抛 TypeError，而那个异常会被
        #    `run()` 的 except 吞掉、只记一条 ERROR，表现是"事件永远没做成"。
        fn = ctx.get(ctx_key)
        if fn is None:
            log.warning(
                "事件 %s 的 handler 没接上（ctx 里没有 %r）—— 这次跳过",
                kind, ctx_key,
            )
            return None
        return await fn(ctx)

    spec = _BY_KIND.get(kind)
    if spec is None:
        log.warning("bind_handler：没有名为 %s 的事件", kind)
        return
    spec.handler = _dispatch


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


@dataclass
class EventRun:
    """一次"进行中的事件"（占用槽位的那一个）。

    ## 为什么需要这个类型而不是只存一个字符串

    因为它要能**自动超时**。事件执行中抛异常/卡住时，如果只存了 kind，
    槽会永久被占 —— 表现是"它再也不做任何事了"（所有事件都判定"有事在做"）。
    带上 `ends_at` 才能兜底。
    """

    kind: str
    label: str
    started_at: float
    #: 预计结束时刻（超过它视为卡死，自动释放）
    ends_at: float
    detail: str = ""

    def expired(self, now: float) -> bool:
        return now >= self.ends_at

    def __str__(self) -> str:
        return f"{self.label}（{self.kind}）"


@dataclass
class Rule:
    """一条**条件事件**：条件满足就执行（不是概率抽签）。

    ## 为什么条件事件和随机事件要分开

    用户原话：「达到什么条件就触发什么事件」。

    但两者的调度方式**根本不同**：

    | | 条件事件（本类） | 随机事件（`EventSpec`） |
    |---|---|---|
    | 触发 | 条件满足**就**触发 | 概率抽签 |
    | 频率 | 每次 tick 都判定 | 有全局间隔 + 日上限 |
    | 重复 | 条件不成立才停 | 有 cooldown |

    如果混成一个列表，条件事件会被"全局间隔"和"概率"卡住 ——
    表现是**"精力都 0.1 了还不安静"**。所以同一个模块、两套调度。

    ## `priority` 替代了原来的 if 链行序

    以前状态切换的规则顺序只存在于 `state.py::tick()` 的代码行序里，
    没有任何声明。现在变成数据字段：可读、可测、可调。
    """

    kind: str
    label: str
    #: 条件。返回 True 表示"该触发了"。
    when: Callable[[dict[str, Any]], bool]
    #: 动作。返回一段说明（记日志用）；返回 None 表示"其实不用做"。
    then: Callable[[dict[str, Any]], Awaitable[Optional[str]]]
    #: 排序依据（小的先判）。同值按注册顺序。
    priority: int = 100
    #: 说明（设置页 / 诊断用）
    detail: str = ""
    #: 是否启用（用户可关掉某条规则）
    enabled: bool = True


class EventEngine:
    """
    决定"现在做不做一件事""做哪件"，并保证**同一时间只有一个事件**。

    ## 什么时候不该触发

    ```
    刚交互过（用户正在聊）      -> 不触发（它该回应你，不是走神去做自己的事）
    全局最小间隔没到            -> 不触发（避免连着冒事件）
    这个事件冷却中              -> 不触发
    安静时段（quiet_hours）     -> 不触发
    触发概率没抽中              -> 不触发
    **槽位被占**                -> 不触发（2026-10-25 加）
    ```

    ## 🔴 单占位槽：为什么不用"每个事件自己判断别人在不在"

    用户原话：

    > 「后面如果触发了其他事件，难不成每次都要写防止冲突方法，
    > 所以单独立成模块，要就调用，**同一时间不存在两个事件就可以了**。」

    如果每个事件都写 `if not busy_with_B and not busy_with_C: ...`，
    加第 N 个事件要改 N-1 处 —— N² 的复杂度，而且必然漏。
    漏了的表现是"它一边搜着东西一边开始自言自语"。

    用一个槽位表达之后：每个事件只问"槽空不空"，加新事件**零改动**。
    冲突不再是"规则问题"，而是**结构上不可能**。

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
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.base_chance = clamp01(base_chance)
        self.min_gap_s = float(min_gap_s)
        self.idle_before_s = float(idle_before_s)
        self.quiet_hours = list(quiet_hours or [])
        self.max_per_day = int(max_per_day)
        self.rng = rng or random.Random()
        self._clock = clock
        # 运行时统计（诊断用）
        self.fired = 0
        self.skipped: dict[str, int] = {}
        # 🔴 单占位槽（2026-10-25）：同一时间只允许一个事件。
        self.current: Optional[EventRun] = None
        #: 超时被强制释放的次数（诊断"duration_s 给短了"的关键指标）
        self.timeouts = 0
        #: 条件事件表（2026-10-25）。按 priority 排序。
        self.rules: list[Rule] = []
        #: 事件 kind -> 依赖的模块名（2026-11-02 加）。
        #:
        #: ## 为什么需要它（而不是只用 `needs`）
        #:
        #: `needs` 是**静态能力**（写死在 `CAPABILITIES` 里，运行期不变），
        #: 而模块是**用户可随时开关**的。两者不是一回事：
        #:
        #:     小说阅读模块关掉后，`reading` 的 needs=("reading",) 依然是 True
        #:     -> `dormant` 为 False -> 照样被抽中 -> 占槽 -> handler 里再判模块
        #:
        #: 后果是**白占一个槽**：抽中、抢到槽、进去发现模块关着、什么都不做。
        #: 而且事件日志会记一条"它去读书了"（detail 非空），实际什么都没发生。
        #:
        #: 所以由 Gateway 在构造时填这张表，`eligible()` 据此在**抽取阶段**排除。
        self.module_of: dict[str, str] = {}
        #: 模块开关的查询函数（Gateway 注入 `lambda name: modules.enabled(name)`）。
        #:
        #: ⚠️ 用函数而不是直接持有 ModuleRegistry：events.py 不该依赖 modules.py
        #:    的具体类型，而且用户改模块开关后要能**立刻**生效（现读配置）。
        self.module_enabled: Optional[Callable[[str], bool]] = None
        #: 安静档查询函数（Gateway 注入，2026-11-02）。
        #:
        #: 用户要求「安静状态下不触发任何事件」。用函数而不是布尔：
        #: 用户随时可能切档，值必须**现读**，缓存就成假开关了。
        self.quiet_tier: Optional[Callable[[], bool]] = None

    def _module_on(self, name: str) -> bool:
        """模块是否开着。没注入查询函数时按**开**处理（不误伤）。"""
        if self.module_enabled is None:
            return True
        try:
            return bool(self.module_enabled(name))
        except Exception:  # noqa: BLE001 —— 模块查询失败不该让事件全停
            log.debug("模块开关查询失败（按开处理）", exc_info=True)
            return True

    def _quiet_tier(self) -> bool:
        """宠物现在是不是「安静」档（2026-11-02 用户要求）。

        ## 用户原话

        > 「安静状态下不主动说话，不触发任何事件，安静30分钟后，
        >   转到无聊后才开始计算用户不予宠物聊天的计时」

        ## 为什么用注入的函数，而不是让 events.py 读配置

        `events.py` 不该知道 `config.activity_tier` 长什么样 ——
        它只管"抽不抽事件"。由 Gateway 注入
        `lambda: config.activity_tier.key == "quiet"`。

        ⚠️ 没注入时按**不安静**处理（保守：不误伤，事件照常跑）。
        这和 `_module_on` 的取舍一致 —— 缺配置不该让功能静默停摆。
        """
        if self.quiet_tier is None:
            return False
        try:
            return bool(self.quiet_tier())
        except Exception:  # noqa: BLE001
            log.debug("安静档查询失败（按不安静处理）", exc_info=True)
            return False

    # ------------------------------------------------------- 单占位槽（互斥）

    @property
    def busy(self) -> bool:
        """有没有事件正在进行。

        ⚠️ 读的时候**顺便检查超时**：如果只在 tick 里检查，那么
        "事件卡死 -> 谁都不做事"会一直持续到下一次 tick（最长 30 秒）。
        放在 property 里，任何一处读 `busy` 都会触发清理。
        """
        self._expire_if_needed()
        return self.current is not None

    @property
    def current_kind(self) -> str:
        self._expire_if_needed()
        return self.current.kind if self.current else ""

    def _expire_if_needed(self) -> None:
        run = self.current
        if run is None:
            return
        now = self._clock()
        if run.expired(now):
            self.timeouts += 1
            self.current = None
            # ⚠️ 必须记日志：否则"事件为什么提前结束"永远查不到。
            #    超时率还是诊断"duration_s 给短了"的关键指标。
            log.warning(
                "事件「%s」超时被强制释放（占槽 %.0f 秒）—— 检查 duration_s 是否给短了",
                run.label,
                now - run.started_at,
            )

    def acquire(self, kind: str, label: str, duration_s: Optional[float]) -> bool:
        """抢槽。槽空则成功；**槽被占直接返回 False（不排队）**。

        ## 为什么不排队

        排队解决不了排队。上一个事件还在搜，排队的第三个事件等到槽空时
        情境早就变了 —— 直接丢弃更符合"它没顾上"。

        `duration_s=None` 表示瞬时事件，**不占槽**（唱歌、想打游戏这类
        说完就完了，占槽会让它接下来几分钟什么都不能做）。
        """
        self._expire_if_needed()
        if duration_s is None:
            return True
        if self.current is not None:
            return False
        now = self._clock()
        self.current = EventRun(
            kind=kind,
            label=label,
            started_at=now,
            ends_at=now + max(1.0, float(duration_s)),
        )
        return True

    def release(self, kind: str) -> None:
        """放槽。

        ⚠️ **必须校验 kind**：事件 A 超时被释放、B 抢到了槽，这时 A 结束
        去 release()，如果不校验就会把 B 的槽清掉 ——
        表现是"B 做到一半突然不算在做了"。
        """
        if self.current is not None and self.current.kind == kind:
            self.current = None

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
        # 🔴 模块关着 -> 不抽它（2026-11-02 加）
        #
        # `needs` 是**静态能力**（写死在 CAPABILITIES 里，运行期不变），
        # 而模块是**用户可随时开关**的。两者不是一回事：
        #
        #     小说阅读模块关掉后，`reading` 的 needs=("reading",) 依然是 True
        #     -> dormant 为 False -> 照样被抽中 -> 占槽 -> handler 里再判模块
        #
        # 后果是**白占一个槽**：抽中、抢到槽、进去发现模块关着、什么都不做。
        # 而且事件日志会记一条"它去读书了"（detail 非空），实际什么都没发生。
        #
        # 所以在这里就排除，连抽中的机会都不给。
        mod = self.module_of.get(spec.kind)
        if mod and not self._module_on(mod):
            return False, f"module_off:{mod}"
        # 🔴 安静档不触发任何事件（2026-11-02 用户要求）
        #
        # 用户原话：「安静状态下不主动说话，不触发任何事件，
        # 安静30分钟后，转到无聊后才开始计算用户不予宠物聊天的计时」。
        #
        # ## 为什么放在这里（而不是各个 handler 里各判一次）
        #
        # `eligible()` 是**唯一的入口** —— 抽事件之前必然过这里。
        # 放在这里就等于"安静档下所有事件一视同仁地不抽"，
        # 以后新加事件也不用记得再判一次。
        #
        # ## 和 `quiet_hours` 的区别
        #
        #   quiet_hours  用户设的**时间段**（比如 23:00-08:00，按钟点）
        #   quiet_tier   宠物自己的**档位**（用户点了「安静」或精力低）
        #
        # 两者独立，都要拦。
        if self._quiet_tier():
            return False, "quiet_tier"
        if in_conversation:
            return False, "in_conversation"
        # 🔴 槽位被占 -> 不触发（2026-10-25 单占位槽）。
        #    这一条就是"同一时间不存在两个事件"的全部实现 ——
        #    不需要每个事件自己去判断别人在不在。
        if self.busy:
            return False, "busy"
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
        # ⚠️ 槽位被占时**连抽都不抽** —— 先判这个比先抽更省（也更准：
        #    日志里"busy"比"chance"更能解释"它为什么没做事"）。
        if self.busy:
            return None, "busy"
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

        ## 占槽（2026-10-25）

        带 `duration_s` 的事件（好奇搜索这类"进行中"的）在**执行期间**占槽：

            抢到槽 -> 执行 handler -> 释放槽

        抢不到（槽已被别的占用）就直接放弃 —— 不排队。
        `duration_s=None` 的瞬时事件不占槽（唱歌、想打游戏说完就完了）。

        ⚠️ `finally` 里释放：handler 抛异常也必须把槽还回去，
        否则这个事件永久占着，之后所有事件都判定"有事在做"。
        """
        owns_slot = spec.duration_s is not None
        if owns_slot:
            if not self.acquire(spec.kind, spec.label, spec.duration_s):
                log.debug("事件「%s」抢槽失败（已有事件在进行）", spec.label)
                return None
        try:
            detail = spec.detail
            if spec.handler is not None:
                try:
                    produced = await spec.handler(ctx)
                except Exception:  # noqa: BLE001 —— 事件失败绝不能影响对话
                    log.exception("事件「%s」执行失败（已忽略）", spec.label)
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
        finally:
            if owns_slot:
                self.release(spec.kind)

    # --------------------------------------------------- 条件事件（2026-10-25）

    def add_rule(self, rule: Rule) -> None:
        """注册一条条件事件。"""
        self.rules.append(rule)
        self.rules.sort(key=lambda r: r.priority)

    def check_rules(self, ctx: dict[str, Any]) -> list[Rule]:
        """返回**所有条件成立**的规则（按 priority 排序）。

        ## 为什么返回列表而不是只返回第一条

        条件事件之间通常互斥（精力低 -> 安静，30 分钟没聊天 -> 无聊），
        但**不保证**。调用方拿到列表后按顺序执行、执行完重判，
        这样"两条都成立"时不会丢信息。

        ## ⚠️ 槽位被占时一条都不触发

        用户原话：「就算精力下降至0.4以下，仍然不变化状态至安静，
        必须等事件结束或功能退出，才能进行精力等判定转换状态。」

        所以"有事在做"是**所有**条件规则的共同前置，不是某几条的。
        放在这里统一判，避免每条规则各写一遍（那正是用户要消灭的东西）。
        """
        if self.busy:
            return []
        out: list[Rule] = []
        for rule in self.rules:
            if not rule.enabled:
                continue
            try:
                if rule.when(ctx):
                    out.append(rule)
            except Exception:  # noqa: BLE001 —— 一条规则写错不能拖垮其它规则
                log.exception("条件事件「%s」的 when 抛异常（已跳过）", rule.kind)
        return out

    async def fire_rule(self, rule: Rule, ctx: dict[str, Any]) -> Optional[str]:
        """执行一条条件事件，返回说明（None = 其实不用做）。"""
        try:
            detail = await rule.then(ctx)
        except Exception:  # noqa: BLE001 —— 规则失败不能影响对话
            log.exception("条件事件「%s」的 then 抛异常（已忽略）", rule.kind)
            return None
        if detail:
            self.fired += 1
        return detail

    def snapshot(self) -> dict[str, Any]:
        return {
            "fired": self.fired,
            "skipped": dict(self.skipped),
            "events": list_events(),
        }
