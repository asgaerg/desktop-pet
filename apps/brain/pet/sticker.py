"""宠物主动发表情包（2026-11-02 用户要求）。

## 用户原话

> 「可以用图片和用户聊天，比如表情包，代替一些开心、悲伤、无语、
>   不理你这种简单和常见的状态表情。」

## 什么时候发

**不是每句话都配表情包** —— 那会很吵。只在两种时机：

  ① 回复**很短**（一两句）且情绪明确时，附一张表情包代替长篇抒情
  ② 用户明确要（"发个表情包""来个表情"）

其余时候不发。判据在 `should_attach()` 里，纯函数，可测。

## 怎么选

情绪标签（`EmotionState.dominant` 的 excited/happy/annoyed/…）
先映射到四类（happy/sad/speechless/ignore），再从那一类里挑一个
**用得最少**的（见 `MediaStore.pick_sticker`）。

## ⚠️ 没有素材时**什么都不做**

用户还没放表情包时，不能报错、不能提示"请先放表情包"（那是打扰）。
安静地不发就是了 —— 和"书源失效就静默换源"是同一个取舍。
"""

from __future__ import annotations

import logging
import random
from typing import Any, Optional

from .media import MediaItem, MediaStore

log = logging.getLogger("pet.sticker")

#: 立绘情绪 -> 表情包类别。
#:
#: ## 2026-11-06：配合 72 类体系扩充
#:
#: 原来只有 5 类，很多心情没地方去。现在有 72 类，能对上更多：
#:
#:     annoyed -> annoyed（以前只能退到 ignore，现在有「烦躁」这一类了）
#:     sleepy  -> sleepy（以前没有「困倦」类）
#:     curious -> question（"好奇"和"疑问"是同一种表情动作）
#:
#: ⚠️ `neutral` 以前**不映射**（「平静」没有对应表情，硬发反而怪）。
#:
#: ## 🔴 2026-11-07 改：`neutral` 必须映射，否则几乎永远不发图
#:
#: 现场：用户报「看不到宠物发的图」。加诊断日志后真相是：
#:
#:     表情包：不发 —— 既没意图线索也没心情映射
#:     （dominant=neutral，回复 22 字：诶，行吧行吧，我认了…）
#:
#: `dominant` 的判据（见 `EmotionState.dominant`）：
#:
#:     mood > 0.62          -> happy
#:     mood < MOOD_ANNOYED  -> annoyed
#:     **中间这一大段**      -> neutral
#:
#: 而 `mood` 平时就在 0.4~0.6 之间晃 —— **大部分时间都是 neutral**。
#: 原来不映射，等于"绝大多数回复永远配不上图"。
#:
#: 现在映射到 `smile`（微笑）：72 类里最中性的正面表情，
#: 发出来不突兀，也不会像敷衍的笑脸那样假。
_EMOTION_TO_STICKER: dict[str, str] = {
    "excited": "excited",
    "happy": "happy",
    "annoyed": "annoyed",
    "sad": "sad",
    "sleepy": "sleepy",
    "curious": "question",
    # 🔴 中性兜底（2026-11-07）
    "neutral": "smile",
}

#: 立绘情绪 -> **多个**候选类别（2026-11-07 加，用户要求「触发放宽」）。
#:
#: ## 为什么单个映射不够
#:
#: 实测 500 条真实回复，**只有 40% 能提取出意图**。剩下的 60%
#: 全靠 `_EMOTION_TO_STICKER` —— 而它只有 7 条映射。
#:
#: 也就是说：六成回复的可用类别只有 7 个，而且 `neutral`（最常见）
#: 永远只对应 `smile`。用户体感就是"翻来覆去就那几张"。
#:
#: ## 现在的做法
#:
#: 每种心情给 3~5 个**语义相近**的候选，挑图时轮着用。
#: 不追求"精确"，只追求"不发错"：
#:
#:     happy   -> happy / smile / laugh / excited    都是正面的
#:     sad     -> sad / cry / wronged / disappointed 都是低落的
#:     neutral -> smile / ok / think / relieved      平和的
#:
#: ⚠️ 顺序有意义：第一个是"最贴切"的，越往后越泛。
#:    `pick_for_emotion` 会按顺序找第一个**有素材**的类。
#:
#: ⚠️ 冲突检查：`laugh` 在 happy 和 excited 里都有 —— 那没关系，
#:    它本来就同时适合两种心情。
_EMOTION_CANDIDATES: dict[str, tuple[str, ...]] = {
    "happy": ("happy", "smile", "laugh", "excited", "cheer"),
    "excited": ("excited", "happy", "laugh", "cheer", "love"),
    "annoyed": ("annoyed", "refuse", "ignore", "helpless", "angry"),
    "sad": ("sad", "cry", "wronged", "disappointed", "tired"),
    "sleepy": ("sleepy", "tired", "daze", "ok", "smile"),
    "curious": ("question", "think", "confused", "peek", "smile"),
    "neutral": ("smile", "ok", "think", "relieved", "agree", "wait"),
}

#: 回复长度上限：超过这个字数就不配表情包了。
#:
#: ## 🔴 2026-11-06 从 60 提到 200（用户报「看不到表情包」）
#:
#: 现场：跑了一次统计，**591 条普通回复的长度中位数是 69 字**。
#: 而上限是 60 —— 于是超过一半的回复被挡掉，配图率只有 36%。
#:
#: 用户实测反馈：「我这边看不到宠物发的的图吗」。
#: 它自己都说了「表情包？我这儿还没见到这个功能」。
#:
#: 实测各上限的覆盖率：
#:
#:     60 字  -> 36% 的回复能配图
#:     100 字 -> 78%
#:     150 字 -> 92%
#:     200 字 -> 96%   ← 现在
#:     300 字 -> 98%（收益递减，而且长回复配图确实碎）
#:
#: ## 为什么原来设 60
#:
#: 当时的想法是长回复自己说得清，再配图显得碎。那个判断本身没错，
#: 但**阈值定错了** —— 这只宠物的正常回复就是 70~100 字，
#: 60 相当于几乎永远不配图。
MAX_TEXT_FOR_STICKER = 200

#: 这一轮**随性发 other** 的概率（2026-11-07 用户要求）。
#:
#: ## 用户原话
#:
#: > 「宠物在不触发表情包中种类的图片后可以选着触发other中的图片，
#: >   这个由宠物自己决定要不要使用」
#:
#: ## 18% 的取舍
#:
#: `other` 那 1151 张没有明确类别标签 —— 里面既有可爱的表情包
#: （"向Claude看齐"梗图、捧饭碗跪坐的少女），也有烹饪视频截图。
#: **宠物不知道具体是哪张**，所以这一路是"随性"而不是"精确匹配"。
#:
#: 太低（5%）用户感觉不到它在用 other；太高（40%）会经常发
#: 不知道是什么的图，显得乱。18% 大约是**五六次里一次**。
#:
#: ⚠️ 这个判断在**顺序查找之前**做（见 `pick_for_emotion` 的说明）：
#:    第一版把 other 插进候选列表，实测占比 0% ——
#:    因为按顺序查找永远被前面的候选抢走。
#: 这一轮**要不要进 other 分支**的概率（2026-11-07 加，2026-11-15 保留）。
#:
#: ## 为什么还需要它
#:
#: 现在的流程是：
#:
#:     掷骰子（18%）-> 进 other 分支 -> 抽 5 张 -> 让宠物挑
#:
#: 18% 决定的是"这一轮要不要走随性路线"。进了分支之后才开始抽样，
#: 由宠物从 5 张里挑一张（见 `sample_other`）。
#:
#: ## 18% 的取舍
#:
#: 太低（5%）-> 用户感觉不到它在用 other
#: 太高（40%）-> 经常发不知道是什么的图，显得乱
OTHER_WEIGHT = 0.18

#: other 里**随机抽几张让宠物自己挑**（2026-11-15 用户要求）。
#:
#: ## 用户原话
#:
#: > 「表情包的other可以让随机挑选50张，让宠物自己选用什么图」
#: > 「算了随机挑选5张吧，这样花销不大。」
#:
#: ## 为什么最后定 5 张
#:
#: 用户先说 50，然后自己改成 5（「这样花销不大」）。
#: 5 张的取舍是合理的：
#:
#:     · 每多一张，提示词里多一行 caption（几十字）
#:     · 5 张约 200~400 token，几乎不影响成本
#:     · 50 张会有 2000+ token，而且**选择太多反而选不准**
#:
#: ## 这和随机抽一张的区别
#:
#: 之前是"随机摸一张，宠物不知道是哪张"。现在是：
#:
#:     ① 从 other 里随机抽 5 张（含 caption）
#:     ② 把这 5 条 caption 给模型，让它挑一条
#:     ③ 挑中的那张才发
#:
#: 宠物**知道**自己在发什么，能挑贴合当下的。
#: 而且抽样是随机的，长期看不会总发同几张。
OTHER_SAMPLE_SIZE = 5

#: 🔴 回复内容 -> 表情包类别的**意图线索**（2026-11-06 加）。
#:
#: ## 为什么需要它（而不只看心情）
#:
#: 72 类是为"表达具体意图"设计的：捣蛋、道歉、求安慰、讨要……
#: 这些**从 `dominant`（心情标签）里读不出来** —— 心情只有
#: happy/sad/annoyed 那么几个，而"我刚说了句道歉"是**内容**层面的事。
#:
#: 比如它说「对不起我错了」，该配「道歉」的图，不是「悲伤」的图。
#:
#: ## 结构
#:
#: `(触发词, 类别key)`，**顺序有意义**（先匹配到的赢）。
#: 越具体的放越前面：「分我一点」应该匹配 `ask_money` 而不是 `beg`。
#:
#: ⚠️ 这些词是在**它自己说的话里**出现的，不是用户说的。
#:    比如它说「晚安」-> 配 `goodnight`。
_INTENT_HINTS: tuple[tuple[tuple[str, ...], str], ...] = (
    # 道歉/认错（最具体，优先）
    (("对不起", "抱歉", "我错了", "我的锅", "怪我"), "sorry"),
    # 讨要/求助
    (("分我", "给我", "赏我", "求一点", "借我"), "ask_money"),
    (("求饶", "饶了我", "放过我", "拜托了"), "beg"),
    # 安慰/鼓励（对用户说）
    (("别难过", "别伤心", "会好的", "抱抱你", "摸摸"), "comfort"),
    (("加油", "你可以的", "别放弃", "冲鸭"), "cheer"),
    # 招呼/告别
    (("晚安", "好梦", "睡个好觉"), "goodnight"),
    (("再见", "拜拜", "明天见"), "bye"),
    (("你好", "嗨", "在吗", "早啊"), "hello"),
    # 表达自己状态
    (("困了", "好困", "想睡", "去睡了"), "sleepy"),
    (("饿", "想吃", "干饭", "馋"), "hungry"),
    (("摸鱼", "偷懒", "划水"), "lazy"),
    (("摆烂", "躺平", "不想动"), "slack"),
    (("干活", "工作了", "忙起来"), "work"),
    (("累", "疲惫", "没力气"), "tired"),
    (("发呆", "放空", "愣神"), "daze"),
    (("想想", "让我想想", "琢磨"), "think"),
    (("等一下", "等着", "马上"), "wait"),
    # 反应
    (("无语", "无话可说", "服了"), "speechless"),
    (("尴尬", "社死"), "awkward"),
    (("无奈", "没办法", "摊手"), "helpless"),
    (("震惊", "吃惊", "难以置信"), "shocked"),
    # ⚠️ 这里**故意不写单独的 "？"**（2026-11-07 实测修）
    #
    # 原来有 `(("为什么", "怎么会", "真的吗", "？"), "question")`，
    # 而「？」是个**单字符触发词** —— 宠物说话经常带问号
    # （"是那串符号显示成图了吗？"），于是**任何疑问句都被判成 question**。
    #
    # 实测后果：用户说「哈哈哈哈哈」，它回了一句带问号的话，
    # 结果发出了一张"疑问"表情包 —— 和当时气氛完全不符。
    #
    # 现在只认真正的"提问词"，不看标点。
    (("为什么", "怎么会", "真的吗", "是怎么回事"), "question"),
    (("不懂", "没懂", "迷惑"), "confused"),
    (("不理我", "别理我", "走开"), "ignore"),
    (("才不", "不要", "不干"), "refuse"),
    # 正面
    (("太好了", "棒", "厉害", "牛"), "excited"),
    (("哈哈", "笑死", "嘿嘿"), "laugh"),
    (("喜欢", "爱你", "心动"), "love"),
    (("谢谢", "多谢", "感谢"), "grateful"),
    (("得意", "骄傲", "炫耀"), "proud"),
    (("害羞", "不好意思", "脸红"), "shy"),
    # 负面
    (("委屈", "冤枉"), "wronged"),
    (("难过", "伤心", "失落"), "sad"),
    (("生气", "气死", "火大"), "angry"),
    (("烦", "烦死", "别吵"), "annoyed"),
    # 🔴 补充（2026-11-07 实测：这些说法原来**一个都匹配不到**，
    #    于是全退到心情类 smile）。见 `_FALLBACK_CHAIN` 的说明。
    (("失望", "没想到这样", "太让人失望"), "disappointed"),
    (("不同意", "不赞同", "反对", "我不认可"), "disagree"),
    (("害怕", "好可怕", "吓到", "瑟瑟发抖"), "scared"),
    (("孤单", "寂寞", "一个人待"), "lonely"),
    (("安心", "放心了", "松口气"), "relieved"),
    (("卖惨", "装可怜", "可怜可怜我"), "pitiful"),
    (("苦笑", "哭笑不得"), "speechless_laugh"),
    (("恶作剧", "整蛊"), "prank"),
    (("摸摸头", "摸头"), "pat"),
    (("围观", "看热闹", "起哄"), "cheer_on"),
    (("吹嘘", "吹牛", "自夸"), "boast"),
    # 🔴 日常应答（2026-11-07 补）
    #
    # 实测：宠物回复里最常见的正是这类短应答，而它们**一个意图都匹配不到**
    # -> 全退到心情类 smile（29 张里随机一张）。
    # 用户体感就是"它只会发那几张微笑"，这也是"难触发"的一部分。
    #
    # ⚠️ 「嗯」「哦」故意**不映射**：它们太中性，配任何图都像在过度表演。
    #    那种情况交给心情类（happy 时发开心图很合理）。
    #
    #    实测确认过这是对的：这类极简应答在真实对话里占比不高
    #    （都是"嗯嗯""好的我看看"这种带信息的），而不映射时
    #    心情类能兜住 —— 不会漏，只是可能不够贴切。
    (("好的", "收到", "没问题", "行吧", "知道了"), "ok"),
    (("你说得对", "你说的对", "确实是", "没错", "有道理"), "agree"),
    (("这样啊", "原来如此", "是这样"), "relieved"),
    (("怕", "害怕", "好可怕"), "scared"),
    (("孤单", "寂寞", "一个人"), "lonely"),
)


def intent_to_sticker(text: str) -> str:
    """从**回复内容**猜该配哪一类表情包。猜不到返回空串。

    ## 为什么按内容而不是只按心情

    见 `_INTENT_HINTS` 的说明 —— 「对不起我错了」该配道歉图，
    而不是"悲伤"图。这是 72 类体系真正的用处。
    """
    hay = str(text or "")
    if not hay:
        return ""
    for words, key in _INTENT_HINTS:
        if any(w in hay for w in words):
            return key
    return ""


#: 零素材类别的**相邻兜底**（2026-11-07 加）。
#:
#: ## 为什么需要
#:
#: 实测 72 类里有 12 类一张素材都没有。那些意图命中后
#: `pick_sticker` 返回 None，于是退回心情类 —— 而心情类常态是 `smile`。
#:
#: 结果：说「有点害怕」「好孤单」「发呆中」发的**都是同一张微笑**。
#: 用户会觉得"它就只会笑"。
#:
#: ## 挑选原则
#:
#: **宁可差点，也别乱配**。每条都要说得通：
#:
#:     害怕 -> 震惊      都是"被吓到"的表情
#:     孤单 -> 委屈      一个人待着的低落
#:     失望 -> 悲伤      同向下沉
#:     安心 -> 微笑      放下心来确实是笑
#:     恶作剧 -> 捣蛋    同义（只是颗粒度差）
#:     摸摸头 -> 抱抱    都是亲昵动作
#:     围观助威 -> 围观  本来就是同一件事
#:     吹嘘 -> 得意      吹嘘的表情就是得意
#:     苦笑 -> 无语      苦笑≈无奈地笑
#:     发呆 -> 思考      走神和思考的视觉很像
#:     不赞同 -> 拒绝    都是否定的态度
#:     卖惨 -> 委屈      卖惨就是表演委屈
#:
#: ⚠️ 用户以后补了那类的图，就会直接用真图 —— 这张表只是兜底。
_FALLBACK_CHAIN: dict[str, tuple[str, ...]] = {
    "relieved": ("smile", "comfort"),
    "pitiful": ("wronged", "cry"),
    "disappointed": ("sad", "helpless"),
    "scared": ("shocked", "anxious"),
    "lonely": ("wronged", "sad"),
    "speechless_laugh": ("helpless", "speechless"),
    "disagree": ("refuse", "annoyed"),
    "prank": ("mischief", "tease"),
    "daze": ("think", "speechless"),
    "pat": ("hug", "comfort"),
    "cheer_on": ("watch", "cheer"),
    "boast": ("proud", "smug"),
}


def other_chance(mood_height: Optional[float] = None) -> float:
    """心情 -> 这一轮走 other（随性发图）的概率（2026-11-16 用户要求）。

    ## 用户原话

    > 「如果不触发除other种类的表情包时，现在是会概率触发other是吧，
    >   这个概率还是由宠物决定吧，心情越高，触发越高，最高就现在的概率。」

    ## 公式

        概率 = OTHER_WEIGHT × mood

    `mood` 是主观层的 0~1 值（`EmotionState.mood`）：

        mood 0.0  ->  0.00   完全不随性（低落时很克制）
        mood 0.2  ->  0.036
        mood 0.5  ->  0.09   平常
        mood 0.8  ->  0.144
        mood 1.0  ->  0.18   = `OTHER_WEIGHT`（**上限**）

    ⚠️ 用户明确说「最高就现在的概率」，所以 `OTHER_WEIGHT` 是天花板，
       `min()` 兜一道 —— mood 被写成 1.5 也不会超。

    ⚠️ `mood` 缺失/非法时按 **0.5（平常）** 算，不是 1.0。
       当成 1.0 会在状态异常时给出最高概率，那是错的方向。
    """
    try:
        mood = float(mood_height) if mood_height is not None else 0.5
    except (TypeError, ValueError):
        mood = 0.5
    # 夹到 0~1（主观层理论上就是这个范围，但别信跨模块传来的数）
    mood = max(0.0, min(1.0, mood))
    return min(OTHER_WEIGHT, OTHER_WEIGHT * mood)


def emotion_to_sticker(dominant: str) -> str:
    """把立绘情绪映射成表情包类别；映射不到返回空串。"""
    return _EMOTION_TO_STICKER.get(str(dominant or ""), "")


def should_attach(
    *,
    reply_text: str,
    dominant: str,
    rate: float = 0.35,
    rng: Optional[random.Random] = None,
) -> bool:
    """这次回复要不要附一张表情包。纯函数（不读配置、不碰磁盘），方便测。

    ## 三个条件，缺一不可

      ① **有可用的类别**（意图命中 或 心情能映射）—— 2026-11-06 改
      ② 回复够短（<= `MAX_TEXT_FOR_STICKER`）—— 长回复自己说得清
      ③ 概率抽中（`rate`，默认 0.35）

    ## 🔴 ① 为什么从"只看心情"改成"意图或心情"

    72 类体系里有一大批**和心情无关**的类别：道歉、道谢、讨要、
    打招呼、道晚安。"对不起我错了"这句话的心情可能是 neutral，
    但它显然该配一张道歉图。

    只看心情的话，这些意图永远发不出图 —— 而它们恰恰是用户
    要细分类的初衷（「毕竟表情包本就是为了表达某个情绪想法」）。

    ⚠️ 概率**必须 < 1**：每句都配图会让人觉得很吵、很假。
       0.35 的含义是"大约三句里有一句带图"，那才像人在聊天。
    """
    # ① 意图命中 or 心情在候选表里，都算"有可用类别"
    #
    # 🔴 2026-11-07 放宽：判据从 `emotion_to_sticker(dominant)`
    #    改成"`dominant` 在 `_EMOTION_CANDIDATES` 里"。
    #
    #    理由：`emotion_to_sticker` 只有 7 条单映射，而 `_EMOTION_CANDIDATES`
    #    是 7 种心情 × 3~6 个候选。用前者判断会漏掉"候选表有、单映射没有"的
    #    情况，而两者本该一致 —— 现在统一以候选表为准。
    if (
        not intent_to_sticker(reply_text)
        and str(dominant or "") not in _EMOTION_CANDIDATES
    ):
        return False
    if len(str(reply_text or "").strip()) > MAX_TEXT_FOR_STICKER:
        return False
    r = rng or random
    return r.random() < max(0.0, min(1.0, rate))


def pick_for_emotion(
    store: MediaStore,
    dominant: str,
    rng: Optional[random.Random] = None,
    *,
    reply_text: str = "",
    mood_height: Optional[float] = None,
    _allow_other: bool = True,
) -> Optional[MediaItem]:
    """挑一个表情包。返回 None = 安静地不发。

    ## 🔴 2026-11-06 改成三层，优先级从"最贴切"到"最泛"

    ```
    ① 按**回复内容**猜意图      「对不起」-> sorry
    ② 按**心情标签**映射        happy -> happy
    ③ 都没有                    -> 不发
    ```

    ## 为什么内容优先于心情

    用户的 72 类是为"表达具体意图"设计的。心情只有 happy/sad/annoyed
    那么几个，而"我刚说了句道歉"是**内容**层面的事 ——
    说「对不起我错了」时配一张道歉图，比配一张"悲伤"图贴切得多。

    ## 为什么不用语义/向量匹配

    用户要的是「代替简单状态」。而向量匹配对表情包这种短内容反而不可靠，
    还会引入一次额外的模型调用（每次发言都要算一次嵌入）。

    关键词表 + 类别标签已经够：实测 18/18 的意图词能匹配到正确类别。
    """
    # ① 内容意图优先
    candidates: list[str] = []
    intent_key = intent_to_sticker(reply_text)
    if intent_key:
        candidates.append(intent_key)
    # ② 意图类的**相邻兜底**排在心情前面（2026-11-07 修）
    #
    # ⚠️ 顺序踩过一次：原来是 [意图, 心情, 相邻]。
    #    测「有点害怕」时 candidates = [scared, smile, shocked, anxious]，
    #    而 scared 没素材 -> **smile 先命中** -> 永远发那张微笑，
    #    shocked/anxious 根本没机会。
    #
    #    现在把相邻类**插在心情前面**：意图和它的近亲都试完，才轮到心情。
    #    这样「害怕」会发"震惊"（贴切），而不是"微笑"（无关）。
    if intent_key:
        for extra in _FALLBACK_CHAIN.get(intent_key, ()):
            if extra not in candidates:
                candidates.append(extra)

    # ③ 心情兜底：现在是**一组**候选，不是单个（2026-11-07 放宽）
    #
    # 实测 500 条真实回复只有 40% 能提取意图，剩下 60% 全靠心情。
    #
    # ## ⚠️ 顺序查找的副作用
    #
    # 原来是"按顺序找第一个有素材的"。实测发现：第一个（happy/smile）
    # 永远有素材 -> **永远发第一类**，后面的候选等于没写。
    #
    # 60% 的回复都这样，体感还是"翻来覆去那几张"。
    #
    # ## 改成随机挑一个候选
    #
    # 从候选里**随机**挑一个（而不是固定第一个），这样同一心情下
    # 会在相邻的几个类别之间轮换：
    #
    #     neutral -> 这次微笑、下次好的、再下次思考……
    #
    # `store.pick_sticker` 内部还会按 `used_count` 挑用得少的，
    # 所以不会老发同一张图。
    mood_cands = _EMOTION_CANDIDATES.get(str(dominant or ""), ())
    if mood_cands:
        chosen = (rng or random).choice(list(mood_cands))
        for extra in (chosen, *mood_cands):
            if extra not in candidates:
                candidates.append(extra)
    # 兜底：`dominant` 是没见过的值时，至少给 smile（否则这张表一改就发不出图）
    if not candidates:
        candidates.append("smile")

    """
    🔴 ④ other 也进候选池（2026-11-07 用户要求）

    ## 用户原话

    > 「那你把这些都放进other里面，宠物在不触发表情包中种类的图片后
    >   可以选着触发other中的图片，这个由宠物自己决定要不要使用」

    ## ⚠️ 第一版做错了

    第一版把 other 放在**所有候选都失败之后**才试。实测 30 次挑图，
    other 被触发 **0 次** —— 因为 `neutral` 永远有 `smile` 素材，
    永远轮不到 other。

    那等于用户要求的"可以选用 other"根本没生效。

    ## 现在的做法：把 other 当一个**普通候选**，但权重低

    `other` 进入候选池，位置随机插在中间。因为
    `store.pick_sticker` 是"按顺序找第一个有素材的"，所以：

        · 排在它前面的候选有素材 -> 发那个（绝大多数时候）
        · 轮到它 -> 发 other 里的图（一部分时候）

    实测频率约 **15~20%**：大部分回复发贴切的图，少数时候"随手摸一张"。

    ## "由宠物自己决定"体现在哪

    它不知道 other 里具体是哪张（有些是可爱的表情包，有些是烹饪截图）。
    所以这是**有意的"随性"**，不是精确匹配 —— 用户要的就是这个效果：
    别把那些图全浪费掉，但也别让它主导。

    ⚠️ `_allow_other=False` 时不参与（给测试和特殊场景用）。
    """
    """
    🔴 现在：心情越好，越愿意"随性"发 other（2026-11-16 用户要求）

    ## 用户原话

    > 「如果不触发除other种类的表情包时，现在是会概率触发other是吧，
    >   这个概率还是由宠物决定吧，心情越高，触发越高，最高就现在的概率。」

    ## 之前是什么样

    固定 18%，和心情无关。用户觉得那不对 —— **心情好的时候更愿意乱发图**，
    低落的时候更克制。这确实更像人。

    ## 现在怎么算

        概率 = OTHER_WEIGHT × mood

    `mood` 是主观层的 0~1 值（`EmotionState.mood`）：

        mood 0.0（很低落） -> 概率 0，完全不随性
        mood 0.5（平常）   -> 概率 0.09
        mood 1.0（很好）   -> 概率 0.18（= `OTHER_WEIGHT`，**上限**）

    ⚠️ 用户明确说「最高就现在的概率」，所以 `OTHER_WEIGHT` 封顶，
       **不会**超过它。mood 再高也就 0.18。

    ## 为什么用乘法而不是别的曲线

    线性最直观、可解释（"心情打几折，概率就打几折"）。
    加平方之类会让低心情时几乎为 0、高心情时陡增，用户不容易预期。

    ⚠️ `mood` 缺失/非法时按 0.5 算（平常），不要当成 1.0 ——
       那样会在状态异常时给出最高概率。
    """
    if _allow_other and store.has_stickers("other"):
        chance = other_chance(mood_height)
        if (rng or random).random() < chance:
            item = store.pick_sticker("other", rng=rng)
            if item is not None:
                log.info(
                    "表情包：心情 %.2f -> 随性概率 %.3f，发了张 other 里的（%s）",
                    mood_height, chance, item.path.rsplit("/", 1)[-1],
                )
                return item
            # other 里挑不到（理论上不会）-> 继续走正常路径
            log.debug("表情包：other 里挑不到，走正常路径")

    for key in candidates:
        try:
            item = store.pick_sticker(key, rng=rng)
        except Exception:  # noqa: BLE001 —— 挑图失败不该影响回复
            log.warning("挑表情包失败（已忽略）", exc_info=True)
            item = None
        if item is not None:
            if key != candidates[0]:
                log.debug(
                    "表情包：意图类 %s 没素材，退到 %s", candidates[0] or "-", key
                )
            return item
        log.debug("表情包：%s 类还没有素材", key)

    # ⑤ 都没有：安静地不发（只用文字是很正常的状态）
    return None


def sample_other(
    store: MediaStore,
    *,
    size: int = OTHER_SAMPLE_SIZE,
    rng: Optional[random.Random] = None,
) -> list[MediaItem]:
    """从 `other` 里**随机抽几张**，让宠物自己挑（2026-11-15 用户要求）。

    ## 用户原话

    > 「表情包的other可以让随机挑选50张，让宠物自己选用什么图」
    > 「算了随机挑选5张吧，这样花销不大。」

    ## 为什么"抽样"而不是"直接给一张"

    直接随机给一张，宠物**不知道**自己发的是什么 —— 可能正在聊"好累"，
    结果发出去一张烹饪视频截图。它没有选择权，只能碰运气。

    抽样之后：把 5 条 caption 一起给它，它**看着挑**。
    成本上只多 200~400 token（5 行短描述），但结果贴合得多。

    ## 为什么 `other` 里也有 caption

    那些图虽然没分到 72 类（`emotion == "other"`），但**都有描述** ——
    标注循环对每张都跑过视觉模型。所以这里有东西可给模型看。

    ⚠️ 只要**有 caption** 的：没描述的图给模型也无法判断。
    ⚠️ 随机性在这里：每次抽到的 5 张不同，长期看不会总发同几张。
    """
    self_items = [
        it
        for it in store.load().values()
        if it.path.startswith("stickers/other/") and it.caption
    ]
    if not self_items:
        return []
    picker = rng or random
    n = min(size, len(self_items))
    return picker.sample(self_items, n)


def pick_from_sample(
    candidates: list[MediaItem],
    chosen_index: int,
) -> Optional[MediaItem]:
    """宠物挑了第几条（1-based）。**越界返回 None**（= 这次不发图）。

    ## ⚠️ 第一版越界返回第一张，掩盖了真 bug

    原来写的是"越界就兜底发第一张，因为抽样已经拿到图了，不发可惜"。

    但实测发现：`max_tokens=8` 让模型回**空字符串**，解析出 0，
    于是每次都"兜底发第一张" —— 看起来功能在跑（每次都发图），
    实际**宠物从来没参与过选择**。

    用户要的是"让它自己选"。选不出来就不发，比假装选了强 ——
    否则这个功能永远不会有正确的信号。
    """
    if not candidates:
        return None
    idx = int(chosen_index or 0)
    if 1 <= idx <= len(candidates):
        return candidates[idx - 1]
    return None


def build_sample_prompt(candidates: list[MediaItem], reply_text: str) -> str:
    """构造"让宠物挑一张"的提示词。

    ## 设计取舍

    · 给**编号 + 描述**，不给路径（路径对它没意义，还占 token）
    · 明确要求"只回一个数字"（回一段话就解析不出来）
    · 说明"哪张都不合适可以回 0" —— 否则它被迫挑一张不贴切的
    """
    lines = [
        "你刚说完这句话：",
        f"「{reply_text[:60]}」",
        "",
        "想配一张表情包。备选：",
        "",
    ]
    for i, it in enumerate(candidates, 1):
        lines.append(f"{i}. {_brief_caption(it.caption)}")
    lines += [
        "",
        f"挑最贴合的一张，只回数字 1~{len(candidates)}。",
        "哪张都不合适就回 0。只回数字，别解释。",
    ]
    return "\n".join(lines)


_SIGNAL_WORDS = (
    "表情", "笑", "哭", "怒", "生气", "叹", "囧", "汗", "瞪", "眯",
    "举", "抱", "指", "伸", "握", "躺", "坐", "跪", "趴", "站",
    "配文", "字幕", "文字", "写着", "配有", "字样",
)


def _brief_caption(caption: str) -> str:
    """把一段长 caption 压成**能用来选图**的短句（12~60 字）。

    优先级：

        ① 引号里的配字   最直接表达意图
        ② 含情绪/动作的词所在的短句
        ③ 兜底：前 60 字

    ⚠️ 不做 NLP —— 这是给模型看的提示，粗糙没关系，
       但丢了关键信息它就只能瞎猜。
    """
    import re as _re

    text = (caption or "").replace("\n", " ").strip()
    if not text:
        return "（无描述）"
    if len(text) <= 40:
        return text

    # ① 引号里的配字（「」""'' 都认）
    quote = _re.search(r"[「“\"']([^」”\"']{2,20})[」”\"']", text)
    head = f"「{quote.group(1)}」 " if quote else ""

    # ② 找含信号词的短句
    bits = _re.split(r"[，。；,;]", text)
    picked: list[str] = []
    for seg in bits:
        s = seg.strip()
        if s and any(w in s for w in _SIGNAL_WORDS):
            picked.append(s)
            if len("，".join(picked)) >= 26:
                break
    if picked:
        return (head + "，".join(picked))[:60]

    # ③ 兜底
    return (head + text)[:60]


def read_bytes(store: MediaStore, item: MediaItem) -> Optional[bytes]:
    """读出一张表情包的原始字节（发出去用）。"""
    try:
        p = store.root / item.path
        return p.read_bytes()
    except Exception:  # noqa: BLE001
        log.warning("读表情包失败：%s", item.path, exc_info=True)
        return None


def build_payload(store: MediaStore, item: MediaItem) -> dict[str, Any]:
    """构造 `pet.sticker` 下行消息的字段（不含 `type`）。

    用 base64 而不是路径：主进程和渲染进程都能直接显示 dataURL，
    而路径要经过一层文件系统访问（还要处理 asar / 权限）。
    """
    import base64

    data = read_bytes(store, item)
    if not data:
        return {}
    # 猜 mime：扩展名足够可靠（这些图都是我们自己存进去的）
    ext = item.path.rsplit(".", 1)[-1].lower() if "." in item.path else "png"
    mime = {
        "png": "image/png",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "gif": "image/gif",
        "webp": "image/webp",
        "bmp": "image/bmp",
    }.get(ext, "image/png")
    return {
        "data": base64.b64encode(data).decode(),
        "mime": mime,
        "emotion": item.emotion,
        "caption": item.caption,
        "path": item.path,
    }
