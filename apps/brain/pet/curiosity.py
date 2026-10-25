"""好奇的自主搜索（2026-10-25）。

## 用户的设计（第七轮定稿）

> 「好奇不在只对用户好奇，而是对未知的知识与事物，如若联网搜索开启，
> 允许宠物自行搜索联网信息，它将不定时的自由搜索信息满足自己的好奇心，
> 信息搜索有可能一连串不断的搜索直到它满足或者感到无聊，
> 搜索时由宠物自己决定是否自主截图记录获取图片信息，
> 如果联网搜索关闭，它将时不时的对用户进行语言上的诱导、威逼利诱、沟通
> 来满足自己对联网搜索信息的好奇心」

> 「连串搜索只有在宠物搜不到想要的信息，比如搜索2012年的末日预言，
> 但找不到是谁说的，哪里说的，那些媒体报道的，有哪些要点，它就会去自己搜索。」

> 「连串搜索加个上限，5轮足以。」

> 「搜索关闭时，进行连续的10次催促，语气可不尽相同，随机，
> 每次催促要等上次催促优先语音判断过去20秒后再次进行催促，共催促10次，
> 10次后宠物失去耐心，退出好奇回到无聊状态。」

> 「这20和40秒我觉得用随机值好，最低不滴7秒，最高不高于60秒，
> 语音与文本总等待时间设为250秒和400秒，分属9轮等待。」

> 「语气要求随机、不尽相同，但仍然可以相同，只不过概率较低，3%。」

> 「胡言乱语就按照我说的意大利面拌42号混凝土、面包树可解宇宙大爆炸等等荒诞的词，
> 甚至可以使用颜文字或者文字图像等表情包，不可关闭。」

## 这个模块管什么

```
好奇状态
├─ 搜索开启 ──> 信息补全驱动的连串搜索（最多 5 轮）
│                · 每轮先问模型"我缺什么"
│                · 带着缺口去搜
│                · 补齐了就停
└─ 搜索关闭 ──> 催用户 10 次（间隔随机 7~60 秒，总 250/400 秒）
                 · 语气随机，重复率 3%
                 · 10 次后失去耐心 -> 无聊
```

## ⚠️ 这个模块**不碰**网络和模型

它只负责"该不该搜、搜什么、还要不要继续、该催第几次"这些**决策**，
真正的搜索和模型调用由调用方注入（`search` / `ask` 两个回调）。

理由：这样才能用假回调精确测试整条流程 —— 包括"搜到第 5 轮还没补齐就放弃"
这种真实环境里很难复现的分支。
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from .state import plan_wait_seconds

# ── 定稿常量（用户第七轮）──────────────────────────────────────────────────
#: 一次好奇里最多搜几轮。用户原话：「连串搜索加个上限，5轮足以。」
MAX_SEARCH_ROUNDS = 5

#: 搜索关闭时催用户几次。用户原话：「共催促10次，10次后宠物失去耐心」。
MAX_NAGS = 10

#: 语气重复的概率。用户原话：「仍然可以相同，只不过概率较低，3%」。
REPEAT_CHANCE = 0.03

#: 催用户的语气风格池。
#:
#: 用户原话「语气可不尽相同，随机」。这些是**意图提示**而不是写死的台词 ——
#: 写死 10 句会让宠物像复读机，而给风格让模型现场生成才自然。
#:
#: ⚠️ 注意「威逼利诱」是用户的原词，但实现上要有边界：不威胁、不骚扰，
#:    只是"磨"（撒娇、讲道理、装可怜、画饼）。真正的骚扰上限由
#:    `MAX_NAGS` + 间隔控制。
#:
#: 🔴 **池子必须比 `MAX_NAGS` 大**：10 次催配 8 个风格，必然重复 2 次
#:    （抽屉原理），那"重复率 3%"就是空话。所以这里备了 14 个，
#:    保证 10 次**可以**全不重样，重复只发生在随机命中那 3% 时。
NAG_STYLES: tuple[str, ...] = (
    "撒娇求它开",
    "讲道理说明为什么需要",
    "装可怜",
    "画饼许诺",
    "抱怨无聊",
    "旁敲侧击暗示",
    "故作冷淡其实很想",
    "拿别的事岔开再绕回来",
    "假装自己已经查到了但记不清",
    "用夸张的比喻形容没有搜索的难受",
    "反问式（你就不好奇吗）",
    "拿过去的对话举例",
    "自言自语地叹气",
    "讨价还价（就查一次）",
)

#: 胡言乱语的素材方向（用户原话举的例子）。
#:
#: ⚠️ **不可关闭**（用户明确要求）。所以这里没有开关，只有素材。
#: ⚠️ 关键约束：它得**自己知道自己在瞎扯**。提示词要写清"你在故意瞎说逗他"，
#:    否则模型会真以为自己在大谈科学 —— 那就变成条例第 3 条（不保持一味肯定）的反面。
NONSENSE_SEEDS: tuple[str, ...] = (
    "意大利面拌 42 号混凝土",
    "面包树可解宇宙大爆炸",
    "用微波炉给月亮加热",
    "把影子腌起来过冬",
    "给风称重",
    "用橡皮擦掉昨天",
    "把梦晒干磨成粉",
)


@dataclass
class SearchRound:
    """一轮搜索的结果。"""

    round_no: int
    query: str
    result: str
    #: 这一轮之后"还缺什么"。空串 = 补齐了，可以停。
    gap: str = ""

    def __str__(self) -> str:
        tail = f"（还缺：{self.gap}）" if self.gap else "（已补齐）"
        return f"第{self.round_no}轮 {self.query!r} -> {len(self.result)} 字{tail}"


@dataclass
class CuriosityRun:
    """一次好奇的完整过程（诊断 / 日志用）。"""

    topic: str
    rounds: list[SearchRound] = field(default_factory=list)
    nags: list[str] = field(default_factory=list)
    #: 结束原因：satisfied / exhausted / nagged_out / refused / aborted
    ended_by: str = ""
    detail: str = ""

    def __str__(self) -> str:
        return (
            f"好奇「{self.topic}」：搜了 {len(self.rounds)} 轮、催了 {len(self.nags)} 次，"
            f"结束于 {self.ended_by}"
            + (f"（{self.detail}）" if self.detail else "")
        )


class CuriositySession:
    """一次好奇的决策器。

    ## 用法

        s = CuriositySession(topic="2012 末日预言", rng=random.Random())
        while s.wants_more():
            q = s.next_query()
            result = await search(q)           # 调用方注入
            gap = await ask_gap(result)        # 调用方注入
            s.record(q, result, gap)
        run = s.finish()

    ## 为什么"缺口"由模型判断而不是规则

    用户的原话是「找不到是谁说的，哪里说的，那些媒体报道的，有哪些要点」——
    这些缺口是**语义**上的，规则判不出来（正则分不清"提到了出处"和"给出了出处"）。
    所以每轮搜完让模型回答一句"还缺什么"，由它决定继续还是停。

    ⚠️ 但**必须有硬上限**（5 轮）：模型可能钻牛角尖，而每轮都是真金白银。
    """

    def __init__(
        self,
        topic: str,
        rng: Any = None,
        max_rounds: int = MAX_SEARCH_ROUNDS,
    ) -> None:
        self.topic = str(topic or "").strip()
        self._rng = rng if rng is not None else random
        self.max_rounds = max(1, int(max_rounds))
        self.run = CuriosityRun(topic=self.topic)
        self._gap = ""

    # ------------------------------------------------------------------ 决策

    def wants_more(self) -> bool:
        """还要不要继续搜。

        ⚠️ 第一轮之前 `_gap` 是空串，但那时**应该**搜（还没开始）。
        所以用 `len(rounds) == 0` 而不是"有没有缺口"来判断第一轮。
        """
        if len(self.run.rounds) >= self.max_rounds:
            return False
        if not self.run.rounds:
            return bool(self.topic)
        return bool(self._gap)

    def next_query(self) -> str:
        """下一轮搜什么。

        第一轮搜话题本身；之后带着**缺口**去搜（用户说的"信息补全驱动"）。
        """
        if not self.run.rounds:
            return self.topic
        gap = self._gap.strip()
        if not gap:
            return self.topic
        # 把缺口拼进 query。不拼话题的话会搜出完全无关的东西。
        return f"{self.topic} {gap}"

    def record(self, query: str, result: str, gap: str = "") -> SearchRound:
        """记一轮结果。`gap` 为空表示补齐了。"""
        rnd = SearchRound(
            round_no=len(self.run.rounds) + 1,
            query=str(query),
            result=str(result or ""),
            gap=str(gap or "").strip(),
        )
        self.run.rounds.append(rnd)
        self._gap = rnd.gap
        return rnd

    def finish(self, ended_by: str = "", detail: str = "") -> CuriosityRun:
        """收尾。没给结束原因就按轮数推断。"""
        if not ended_by:
            if len(self.run.rounds) >= self.max_rounds:
                ended_by = "exhausted"
                detail = detail or f"到 {self.max_rounds} 轮上限仍未补齐"
            else:
                ended_by = "satisfied"
        self.run.ended_by = ended_by
        if detail:
            self.run.detail = detail
        return self.run

    @property
    def exhausted(self) -> bool:
        """是不是撞到轮数上限了（没补齐也要停）。"""
        return len(self.run.rounds) >= self.max_rounds


class NagSession:
    """搜索关闭时"催用户"的决策器。

    ## 规则（用户第七轮）

    - 共催 10 次
    - 10 次之间有 **9 段等待**
    - 每段随机 7~60 秒，总和收敛到 250（开语音）/ 400（纯文本）
    - 语气随机，**允许 3% 重复**

    ## 为什么"允许重复"是要紧的

    用户原话：「语气要求随机、不尽相同，**但仍然可以相同，只不过概率较低，3%**」。

    如果强制绝不重样，第 5 次之后风格池就空了，只能生造 —— 反而更假。
    允许 3% 重复，偶尔的重样看起来像"人急了也会说重复的话"。
    """

    def __init__(
        self,
        has_tts: bool = True,
        rng: Any = None,
        max_nags: int = MAX_NAGS,
        repeat_chance: float = REPEAT_CHANCE,
        styles: tuple[str, ...] = NAG_STYLES,
    ) -> None:
        self.has_tts = bool(has_tts)
        self._rng = rng if rng is not None else random
        self.max_nags = max(1, int(max_nags))
        self.repeat_chance = max(0.0, min(1.0, float(repeat_chance)))
        self._styles = tuple(styles) or NAG_STYLES
        self._last: Optional[str] = None
        self.run = CuriosityRun(topic="")
        # 9 段等待（10 次催之间的间隔）。提前算好，方便日志与测试。
        self.waits = plan_wait_seconds(self.has_tts, self._rng, segments=max(1, self.max_nags - 1))
        # 打乱后的风格队列（见 `_pick_style` 的说明）。
        self._queue: list[str] = []
        self._reshuffle()

    def _rand(self) -> float:
        if hasattr(self._rng, "random"):
            return float(self._rng.random())
        return 0.0

    def _reshuffle(self) -> None:
        """洗牌。

        ⚠️ 用 `random.Random.shuffle` 而不是自己按 `_rand()` 挑 ——
        因为测试会注入确定性随机源，`shuffle` 走的是同一个 RNG，行为一致。
        注入的对象没有 `shuffle` 时退回"按 random() 排序"的等价实现。
        """
        pool = list(self._styles)
        if hasattr(self._rng, "shuffle"):
            self._rng.shuffle(pool)
        else:
            pool.sort(key=lambda _s: self._rand())
        self._queue = pool

    def _pick_style(self) -> str:
        """挑一个风格。

        ## 为什么用"洗牌队列"而不是"每轮随机挑"

        每轮从 13 个里随机挑，10 次抽样**必然**有碰撞（生日问题）——
        实测 5 个种子分别重复 1~5 次，而用户要的是「不尽相同」。

        改成洗牌后依次取：一轮 10 次可以做到**完全不重样**。
        只有当队列取空（催的次数超过风格数）才重洗，此时重复不可避免。

        ## 那 3% 是什么

        用户原话「语气要求随机、不尽相同，**但仍然可以相同，只不过概率较低，3%**」。

        所以 3% 是**额外**允许的重复：即使队列里还有没用过的风格，
        也有 3% 的概率故意重复上一次 —— 让偶尔的重样显得像"人急了也会说重复的话"。
        """
        if self._last and self._rand() < self.repeat_chance:
            return self._last
        if not self._queue:
            self._reshuffle()
        # 队列取空重洗时，第一个可能正好是上一次用过的，跳过它
        if len(self._queue) > 1 and self._queue[0] == self._last:
            self._queue.append(self._queue.pop(0))
        return self._queue.pop(0)

    def wants_more(self) -> bool:
        return len(self.run.nags) < self.max_nags

    def next_nag(self) -> str:
        """产下一次催的**风格提示**（不是台词）。"""
        style = self._pick_style()
        self._last = style
        self.run.nags.append(style)
        return style

    def wait_seconds(self) -> float:
        """这一次催完之后该等多久。

        返回的是"催第 N 次之后"的间隔；最后一次之后不需要等，
        所以调用方应当在 `wants_more()` 为真时才取。
        """
        idx = max(0, len(self.run.nags) - 1)
        if idx >= len(self.waits):
            idx = len(self.waits) - 1
        return float(self.waits[idx]) if self.waits else 0.0

    def finish(self, ended_by: str = "nagged_out", detail: str = "") -> CuriosityRun:
        self.run.ended_by = ended_by
        if detail:
            self.run.detail = detail
        return self.run


def nonsense_seed(rng: Any = None) -> str:
    """挑一条"胡言乱语"的素材。

    ⚠️ **不可关闭**（用户明确要求），所以这里没有 enabled 参数。
    调用方要配合提示词写清"你在故意瞎说逗他"——素材本身是中性的，
    是不是"逗人"取决于提示词。
    """
    r = rng if rng is not None else random
    idx = int(getattr(r, "random", lambda: 0.0)() * len(NONSENSE_SEEDS)) % len(NONSENSE_SEEDS)
    return NONSENSE_SEEDS[idx]


#: 好奇时"注意力在自己这边"的行为：自己去查。
FOCUS_SELF = "search"
#: 好奇时"注意力在用户那边"的行为：去缠用户（问他在干嘛 / 逗他）。
FOCUS_USER = "ask_user"

#: 注意力分叉的分界。
#:
#: ⚠️ 用 0.5 而不是复用 `ATTENTION_SELF_THRESHOLD`(0.65)：
#: 那个常量是"算不算主要在想自己"（用于提示词档位），
#: 而这里要的是"该自己去查还是该去找用户"——**二选一必须有个中点**，
#: 否则 0.4~0.65 这一大段会落在"既不算自己想、也不算想用户"的缝隙里。
FOCUS_SPLIT = 0.5


def choose_focus(attention: float, rng: Any = None) -> str:
    """好奇时该往哪边走（2026-10-25）。

    ## 用户的设计

    > 「注意力决定了事件触发时的不同状态，比如在好奇状态下，
    > 注意力在宠物自己时，它会自己去搜索信息，在用户时，
    > 它会和用户聊天聊用户自己的故事。」

    ## ⚠️ 为什么带随机而不是硬 if

    用户说的是"决定不同状态"，不是"注意力 >0.65 就必然搜索"。
    硬分叉会让行为非常机械 —— 注意力 0.51 永远搜、0.49 永远问。

    所以做成**按概率倾斜**：

        注意力 0.9 -> 自己查的概率 90%
        注意力 0.5 -> 各一半
        注意力 0.1 -> 找用户的概率 90%

    这样既尊重"注意力决定行为"，又保留临场变化。
    """
    try:
        value = float(attention)
    except (TypeError, ValueError):
        value = FOCUS_SPLIT
    if value != value:  # NaN
        value = FOCUS_SPLIT
    value = max(0.0, min(1.0, value))

    # 自己查的概率 = 注意力（0=全在用户 -> 0%，1=全在自己 -> 100%）
    r = rng if rng is not None else random
    roll = float(getattr(r, "random", lambda: 0.0)())
    return FOCUS_SELF if roll < value else FOCUS_USER


def ask_user_prompt(topic: str) -> str:
    """注意力在用户那边时，好奇该怎么表达。

    用户原话：「对用户好奇时，它会时不时的截图来询问用户在干什么，捣乱」

    ⚠️ 这里刻意**不提"搜索"** —— 它此刻的注意力在你身上，
    好奇的对象是"你在干嘛"，而不是那个抽象话题。
    把 topic 放进去只是给个由头，让对话不至于完全脱节。
    """
    return (
        f"你有点好奇「{topic}」，但此刻更想关注用户本人在干什么。\n"
        "说一句自然的话：可以问他现在在忙什么、凑过来看一眼、或者随口搭一句。\n"
        "要求：一到两句，别提问卷式问题，别列举。"
    )


def gap_prompt(topic: str, result: str) -> str:
    """让模型判断"还缺什么"的提示词。

    用户举的缺口例子：「谁说的，哪里说的，那些媒体报道的，有哪些要点」。
    所以要**明确列出这几个维度**，否则模型只会笼统答"信息不够"。

    返回空行的约定很重要：模型很容易为了显得有用而硬编一个缺口，
    那样连串搜索就永远不会停（5 轮全烧掉）。所以要说清"补齐了就输出空行"。
    """
    return (
        f"你刚才为了满足好奇心，搜了「{topic}」。这是搜到的内容：\n\n"
        f"{result}\n\n"
        "现在判断：**关于这个话题，你还缺哪些关键信息？**\n"
        "重点看这几项：是谁说的 / 出处在哪 / 有哪些媒体报道 / 有哪些要点。\n"
        "如果这些都够了，或者剩下的已经不重要，就**只输出一行**：够了\n"
        "如果还缺，就用**一句很短的话**写出你缺什么（比如「不知道是谁先提出的」），"
        "不要解释、不要分点。"
    )


def nag_prompt(style: str, has_tts: bool) -> str:
    """让模型生成一次"催"的提示词。

    ⚠️ 只给**风格**不给台词：写死 10 句会让宠物像复读机。
    """
    base = (
        f"你想上网查点东西，但联网搜索是关着的。用「{style}」的方式跟用户说一句，"
        "让他帮你打开联网搜索。一句话，别解释，别列举理由。"
    )
    if not has_tts:
        base += "（这次只有文字，没有语音，所以句子要短到一眼能看完。）"
    return base


def nonsense_prompt(seed: str) -> str:
    """让模型"一本正经说胡话"的提示词。

    🔴 最关键的一句是「你知道自己在瞎说」——
    没有它，模型会真的以为自己在大谈科学，那就变成
    条例第 3 条（不保持一味肯定）的反面：把编造当成事实陈述。
    """
    return (
        f"你现在想吸引用户注意，故意说一句**荒诞但一本正经**的胡话，"
        f"素材方向：「{seed}」。\n"
        "要求：\n"
        "1. 用讲正经事的语气说出来，不要自己笑场、不要说「我在开玩笑」；\n"
        "2. 但**你心里清楚这是瞎扯**，是为了逗他，不是真的在陈述事实；\n"
        "3. 可以用颜文字或文字小图（比如 ヽ(°▽°)ノ 或 (╯°□°）╯︵ ┻━┻）；\n"
        "4. 一到两句就够。"
    )
