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

    # ④ 都没有：安静地不发（只用文字是很正常的状态）
    return None


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
