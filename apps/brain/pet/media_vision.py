"""用户图片 / 表情包的**识别与标注**（2026-11-02 用户要求）。

## 用户原话

> 「做图片精准识别解析功能。」
>
> 「可以用图片和用户聊天，比如表情包，代替一些开心、悲伤、无语、
>   不理你这种简单和常见的状态表情。」
>
> 「用户自己放，放完之后让宠物熟悉下，自己个图片的内容打标注，
>   这样用的时候就能看标识自己选用了」

## 三个用途

  ① **用户发来的图片** -> 详细描述（给对话当上下文）
  ② **用户放进来的表情包** -> 打情绪分类 + 一句话标注（供以后挑选）
  ③ **宠物自己下载的表情包** -> 同样标注后才入库

## ⚠️ 与 `content.py` / `screen_chat.py` 的区别

那两套是"看**屏幕**"：从 `Frame`（灰度网格 + 可选 PNG）出发，走
`ContentPipeline` 那条链，还带变化检测和隐私闸门。

这里是"看**一张图**"：没有屏幕、没有变化检测、没有隐私闸门
（用户主动发的东西，本来就是要给宠物看的）。

所以不复用那条链，而是直接用 `OpenAIVisionExtractor` ——
它已经把"怎么调 OpenAI 兼容的多模态接口"封装好了，
包括那个踩过坑的思考模式开关（见它 `__init__` 的说明）。
"""

from __future__ import annotations

import logging
import re as _re
from typing import Any, Optional

log = logging.getLogger("pet.media_vision")

#: 描述用户图片的提示词。
#:
#: ⚠️ 要求它"具体"而不是"概括" —— 用户要的是「精准识别解析」，
#:    泛泛一句"一张图片"没有意义。但也不能漫无边际（会吃满 token）。
DESCRIBE_PROMPT = (
    "请详细描述这张图片。按这个顺序说，能说多少说多少：\n"
    "1. 这是什么（照片/截图/插画/表情包/文字图）\n"
    "2. 画面里有什么：主体是什么、在做什么、在哪里\n"
    "3. 关键细节：颜色、数量、文字内容（有字就照抄出来）、表情、动作\n"
    "4. 如果是表情包：它表达什么情绪或态度\n"
    "\n"
    "只说你在图上真正看到的东西，不要猜测画外的事。\n"
    "如果图上有文字，**把文字原样抄下来**。\n"
    "不要用「这是一张图片」这种空话开头。"
)

#: 给表情包分类的提示词。
#:
#: ## 🔴 2026-11-02 实测：模型**不会**严格遵守"只回两行"的格式
#
# 第一版要求它回「类别：xxx / 描述：xxx」两行，实测它直接写成了一段
# 散文（"用户正在使用一款极简风格的桌面时钟…"），类别一个字都没给。
#
# 所以解析必须**两种都能认**：
#   ① 有「类别：」就按格式解析
#   ② 没有的话，就在整段文字里**找关键词**（出现"开心"就归 happy）
#      —— 这比"解析失败就留空"好得多：留空等于这次识别白花钱。
LABEL_PROMPT = (
    "这是一个表情包。请回答两个问题。\n"
    "\n"
    "问题一：它最接近下面哪种情绪？\n"
    "  A 开心   B 悲伤   C 无语   D 不理你   E 其它\n"
    "\n"
    "问题二：用一句话（不超过 30 字）说清它画的是什么、什么情绪。\n"
    "\n"
    "回答格式（严格两行）：\n"
    "类别：<A/B/C/D/E 中的一个字母>\n"
    "描述：<一句话>"
)

#: 看图的中文类别 -> 内部 key（`media.EMOTIONS` 的键）。
_LABEL_TO_KEY = {
    "开心": "happy",
    "悲伤": "sad",
    "无语": "speechless",
    "不理你": "ignore",
    "其它": "other",
}

#: 字母 -> key（新提示词用字母，比中文类别名更不容易被模型改写）。
_LETTER_TO_KEY = {
    "A": "happy",
    "B": "sad",
    "C": "speechless",
    "D": "ignore",
    "E": "other",
}

#: 同义词表：模型/用户可能用的说法 -> 类别 key（2026-11-06 加）。
#:
#: ## 为什么需要它
#:
#: 实测「想吃东西」匹配不到「饿了」（标签只有两个字，而模型是自由发挥）。
#: 三档包含匹配能覆盖一部分，但**语义相同、字面不同**的情况
#: （吃 ↔ 饿、开心 ↔ 兴奋、流泪 ↔ 哭泣）必须显式列出来。
#:
#: ⚠️ 这张表是**补充**，不是替代：`_match_category` 的包含匹配先跑，
#:    匹配不到才查这里。所以只放"字面上确实对不上"的那些。
#:
#: ⚠️ 键是**类别 key**，值是可能的说法（一个类别可以有很多）。
_SYNONYMS: dict[str, tuple[str, ...]] = {
    "hungry": ("想吃", "吃东西", "馋", "干饭", "美食", "饿"),
    "eat": ("吃", "进食", "啃", "干饭"),
    "excited": ("激动", "雀跃", "按捺不住", "兴奋"),
    "laugh": ("爆笑", "笑死", "狂笑", "滑稽"),
    "smile": ("浅笑", "笑眯眯", "温柔"),
    "cry": ("流泪", "掉眼泪", "嚎啕", "抽泣", "哭泣"),
    "sad": ("难过", "伤心", "沮丧", "低落"),
    "angry": ("发火", "火大", "气死", "恼火"),
    "rage": ("暴怒", "炸了", "狂怒"),
    "annoyed": ("烦", "厌烦", "嫌烦", "不耐烦"),
    "mischief": ("捣乱", "调皮", "使坏", "搞怪", "整蛊", "捣蛋"),
    "prank": ("整人", "恶作剧", "捉弄"),
    "tease": ("逗", "撩", "撩拨"),
    "proud": ("自豪", "牛气", "得意洋洋", "炫耀"),
    "smug": ("傲娇", "嘴硬", "别扭"),
    "shy": ("脸红", "羞涩", "不好意思"),
    "love": ("爱心", "喜欢", "心动", "比心"),
    "touched": ("感动", "泪目", "破防"),
    "grateful": ("谢谢", "感谢", "致谢", "多谢"),
    "comfort": ("安慰", "摸摸", "安抚", "求安慰"),
    "hug": ("抱", "拥抱", "求抱"),
    "pat": ("摸头", "摸摸头", "拍头"),
    "sorry": ("道歉", "对不起", "认错", "抱歉"),
    "beg": ("求饶", "求你", "拜托", "跪求"),
    "forgive": ("原谅", "饶了我", "下不为例"),
    "pitiful": ("卖惨", "可怜", "凄惨", "装可怜"),
    "wronged": ("冤枉", "受气"),
    "speechless": ("哑口无言", "无话可说"),
    "helpless": ("没办法", "摊手"),
    "awkward": ("汗", "社死"),
    "speechless_laugh": ("哭笑不得",),
    "confused": ("不解", "懵", "迷糊"),
    "question": ("问号", "为什么", "真的吗"),
    "shocked": ("吃惊", "难以置信", "目瞪口呆"),
    "contempt": ("嫌弃", "翻白眼", "不屑"),
    "refuse": ("不要", "不行"),
    "disagree": ("反对", "不同意"),
    "approve": ("赞成", "可以", "行"),
    "agree": ("同意", "没错"),
    "ok": ("好的", "收到", "了解"),
    "cheer": ("打气", "冲鸭", "助威"),
    "encourage": ("鼓励", "你可以的", "别放弃"),
    "work": ("干活", "工作", "打工", "忙"),
    "lazy": ("偷懒", "划水"),
    "slack": ("躺平", "放弃治疗"),
    "tired": ("累", "没力气"),
    "sleepy": ("打哈欠", "想睡", "犯困"),
    "sleep": ("睡着", "安睡"),
    "daze": ("放空", "愣神"),
    "think": ("琢磨", "想办法"),
    "wait": ("等待", "候着"),
    "run": ("开溜", "逃走"),
    "hide": ("躲", "缩起来"),
    "peek": ("偷看", "瞄", "探头"),
    "hello": ("你好", "嗨", "挥手"),
    "bye": ("拜拜", "走了"),
    "goodnight": ("好梦",),
    "poke": ("戳", "点一下"),
    "watch": ("看戏", "吃瓜"),
    "cheer_on": ("起哄",),
    "ask_money": ("给我", "分我"),
    "complain": ("吐槽", "牢骚"),
    "boast": ("自夸", "吹牛"),
    "threaten": ("警告", "吓唬"),
    "relieved": ("放心", "松口气"),
    "anxious": ("紧张", "坐立不安"),
    "scared": ("恐惧", "瑟瑟发抖", "害怕"),
    "lonely": ("寂寞",),
    "disappointed": ("扫兴",),
    "ignore": ("无视", "冷漠"),
}

#: 关键词兜底：描述里出现这些词就归到对应情绪。
#:
#: ⚠️ 顺序有意义（先匹配到的赢），而且"不理你"要排在"无语"前面 ——
#:    「翻白眼不理人」既像无语又像不理你，后者更具体。
_KEYWORD_TO_KEY: tuple[tuple[tuple[str, ...], str], ...] = (
    (("不理", "白眼", "无视", "冷漠", "扭头", "走开"), "ignore"),
    (("无语", "尴尬", "汗", "沉默", "扶额", "无奈", "呆"), "speechless"),
    (("笑", "开心", "高兴", "兴奋", "鼓掌", "欢呼", "喜", "哈哈"), "happy"),
    (("哭", "悲伤", "难过", "沮丧", "垂泪", "伤心", "失落"), "sad"),
)


def build_vision(config: Any) -> tuple[Optional[Any], str]:
    """按配置建一个视觉抽取器。返回 `(extractor 或 None, 不可用原因)`。

    ## 为什么复用 `screen_chat._build_extractor`

    它已经把"用哪家、哪个模型、密钥从哪来"这套决议逻辑写好了，
    而且默认**复用大脑那一家**（这台机器上就是 deepseek-flash，
    实测支持图片）。再写一份必然和它漂移。
    """
    try:
        from .screen_chat import _build_extractor

        return _build_extractor(config)
    except Exception as exc:  # noqa: BLE001 —— 视觉不可用不该让主流程崩
        return None, f"构建视觉抽取器失败：{type(exc).__name__}: {exc}"


def describe_image(
    image_bytes: bytes,
    config: Any,
    *,
    prompt: str = DESCRIBE_PROMPT,
    max_chars: int = 800,
) -> tuple[str, str]:
    """描述一张图片。返回 `(描述, 失败原因)`；成功时第二个是空串。

    ## 🔴 直接调 extractor，不经过 `Frame`

    `OpenAIVisionExtractor.extract()` 要一个 `Frame`，而 `Frame` 的
    `grid` 是给变化检测用的（64 个灰度整数）。用户发来的单张图根本
    没有"上一帧"，硬造一个 grid 只是在骗自己。

    所以这里直接调它内部的 HTTP 请求那段（见 `_request_via_extractor`）。

    ## ⚠️ 失败要返回原因，不要静默

    "宠物看不见图"和"宠物看不懂图"在用户那边感觉一样，但排查完全不同。
    返回原因能让调用方决定是「告诉用户没看成」还是「照常回话」。
    """
    extractor, why = build_vision(config)
    if extractor is None:
        return "", why or "没有可用的视觉模型"
    return _request_via_extractor(extractor, image_bytes, prompt, max_chars)


def _request_via_extractor(
    extractor: Any, image_bytes: bytes, prompt: str, max_chars: int
) -> tuple[str, str]:
    """用已有的 `OpenAIVisionExtractor` 发一次请求。

    ## 为什么要绕一下（而不是直接 `extractor.extract(frame)`）

    看 `content.py` 里那个方法：它接收 `Frame`，从 `frame.image` 取字节，
    然后调 `self._request(png)`。我们只需要**后半段**。

    `_request` 是私有方法，但在同一个项目里直接用它比复制一份 HTTP 代码好 ——
    复制的话，那个踩过坑的「思考模式吃光 token」的处理会漂移成两份。

    ⚠️ 拿不到 `_request` 就退回消息里说清楚，不要假装成功。
    """
    req = getattr(extractor, "_request", None)
    if req is None:
        return "", "视觉抽取器没有 _request 方法（版本不匹配？）"
    try:
        text = str(req(image_bytes) or "").strip()
    except Exception as exc:  # noqa: BLE001 —— 网络/限流/余额
        return "", f"{type(exc).__name__}: {str(exc)[:120]}"
    if not text:
        # ⚠️ 实测遇到过：思考模式把 max_tokens 吃光，正文是空的。
        #    `OpenAIVisionExtractor` 已经处理了这个（thinking 显式关掉），
        #    但服务端仍可能因为别的原因返回空 —— 那就如实说。
        return "", "模型返回了空内容（可能是思考模式吃光了 token）"
    return text[:max_chars], ""


def _parse_label(
    text: str,
    *,
    letter_map: Optional[dict[str, str]] = None,
    categories: Optional[dict[str, str]] = None,
) -> tuple[str, str]:
    """从模型回复里解析出 `(emotion_key, caption)`。

    ## 🔴 2026-11-06 改成**意图关键词 -> 类别**的两步匹配

    提示词现在要模型写「意图：捣蛋、得意」，而不是选字母。所以这里：

        ① 读「意图：」那一行，拆成关键词列表
        ② 拿每个关键词去**类别标签表**里找（精确 -> 包含 -> 反向包含）
        ③ 一个都对不上 -> 再看「描述：」那一行按同样规则找
        ④ 还不行 -> 用降级的关键词表（`_KEYWORD_TO_KEY`）兜一层
        ⑤ 最后 -> `other`

    ## 为什么匹配要分三档

    各类说法和标签不会完全一致：

        模型写「求安慰」  标签是「安慰」    -> 反向包含（标签在关键词里）
        模型写「开心」    标签是「开心」    -> 精确
        模型写「开心的笑」标签是「大笑」    -> 包含/关键词表

    只做精确匹配的话，实测能匹配上的不到三成。

    ## `letter_map` / 旧路径

    保留参数是为了兼容旧调用方（那时用字母 A~E）。有 letter_map 就优先走它。

    ⚠️ ③ 用 `other` 而不是空串：空串在 `media.unlabeled()` 里算"没标注"，
       下次扫描又会被拿去识别一遍，反复烧钱。
    """
    caption = ""
    intent_line = ""
    desc_line = ""

    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        low = s.lower()
        if (s.startswith("意图") or low.startswith("intent")) and ("：" in s or ":" in s):
            intent_line = s.split("：", 1)[-1] if "：" in s else s.split(":", 1)[-1]
        elif (s.startswith("描述") or low.startswith("desc")) and ("：" in s or ":" in s):
            desc_line = s.split("：", 1)[-1] if "：" in s else s.split(":", 1)[-1]

    caption = desc_line.strip()

    # ── 旧路径：字母表（有才用）──
    if letter_map:
        for line in text.splitlines():
            s = line.strip()
            if s.startswith("类别") and ("：" in s or ":" in s):
                value = (s.split("：", 1)[-1] if "：" in s else s.split(":", 1)[-1]).strip()
                value = value.strip("。 ，,.、")
                if value[:1].upper() in letter_map:
                    key = letter_map[value[:1].upper()]
                    return key, (caption or text.strip())[:200]
        for words, key in _KEYWORD_TO_KEY:
            if key in set(letter_map.values()) and any(w in text for w in words):
                return key, (caption or text.strip())[:200]

    # ── 新路径：意图关键词 -> 类别 ──
    #
    # 🔴 2026-11-06 实测：这个模型**根本不按两行格式回**
    #
    # 要它写「意图：」和「描述：」两行，它给的是一个整段：
    #
    #     画面是一张二次元表情包：蓝发女仆装角色坐在超市货架上，
    #     气泡文字为「真当我是便宜货啊」，旁边标牌写着「计划近期…
    #     DeepSeek API 服务的定价」。
    #
    # 一个字都没按格式。所以解析必须**在整段文字里**找意图，
    # 不能只读「意图：」那一行 —— 否则所有图都归 other
    # （实测 20 张全归 `?`，一张都没分类）。
    #
    # 顺序还是"有格式优先、没格式兜底"，两者都试。
    cats = categories or {}
    if cats:
        key = (
            _match_category(intent_line, cats)
            or _match_category(desc_line, cats)
            # ⚠️ 兜底：整段文字（模型没按格式时唯一能用的信号）
            or _match_category(text, cats)
        )
        if key:
            return key, (caption or text.strip())[:200]

    # ── 降级：内置关键词表 ──
    for words, key in _KEYWORD_TO_KEY:
        if any(w in text for w in words):
            return key, (caption or text.strip())[:200]

    if not caption:
        # 没有「描述：」就把整段压成一句（去掉过长的部分）
        caption = " ".join(x.strip() for x in text.splitlines() if x.strip())[:180]
    return "other", caption[:200]


def _match_category(phrase: str, categories: dict[str, str]) -> str:
    """把一句话/一组关键词匹配到某个类别 key。匹配不到返回空串。

    ## 三档匹配（从严到松）

        ① 标签**原样出现**在短语里        「捣蛋」出现在「捣蛋、得意」  -> 命中
        ② 短语里某个词**包含**标签        「求安慰」包含「安慰」        -> 命中
        ③ 标签**包含**短语里某个词        「大笑」包含「笑」            -> 命中

    ⚠️ 顺序有意义：先精确再模糊，避免"求安慰"被"安慰"以外的类别抢走。

    ⚠️ ③ 是最松的一档，容易误判（比如「笑」会命中「大笑」「苦笑」「微笑」）。
       所以它**最后**才用，而且只看前面都没命中时的结果。
    """
    hay = str(phrase or "").strip()
    if not hay:
        return ""
    # 拆成候选词（按顿号/逗号/空格切）
    parts = [p.strip() for p in _re.split(r"[、,，/|\s]+", hay) if p.strip()]
    if not parts:
        parts = [hay]

    # ① 精确：某个候选词 == 标签
    for key, label in categories.items():
        if label in parts:
            return key
    # ② 标签被包含在候选词里（模型写"求安慰"，标签是"安慰"）
    best = ""
    best_len = 0
    for key, label in categories.items():
        for p in parts:
            if label and label in p and len(label) > best_len:
                best, best_len = key, len(label)
    if best:
        return best
    # ③ 候选词被包含在标签里（模型写"笑"，标签是"大笑"）
    for key, label in categories.items():
        for p in parts:
            if len(p) >= 2 and p in label:
                return key
    """
    ④ 同义词表（2026-11-06 加）

    前三档都是**字面**匹配，覆盖不了"吃 ↔ 饿"这类语义相同、字面不同的情况。
    实测「想吃东西」匹配不到「饿了」，所以补这一层。

    ⚠️ 放在最后：同义词是"兜底"，字面能对上时优先信字面
       （否则「吃饭」可能被同义词表抢到 hungry，而它其实该是 eat）。
    """
    for key, words in _SYNONYMS.items():
        if key not in categories:
            continue
        for p in parts:
            for w in words:
                if w in p or p in w:
                    return key
    return ""


def build_label_prompt(categories: Optional[dict[str, str]] = None) -> tuple[str, dict[str, str]]:
    """生成分类提示词。返回 `(提示词, {})`。

    ## 🔴 2026-11-06 改成**两步分类**（用户要求细粒度 50+ 类）

    用户原话：

    > 「现在开始分类，要根据图片的表达信息总结来分类，要细致，
    >   比如捣蛋类动图、可爱求安慰等等」
    > 「细粒度50+，毕竟表情包本就是为了表达某个情绪想法的建议画面」

    ## 为什么不能直接把 72 个类别列给模型选

    实测经验：**5 类时准确率就不算高，20 类是"还能分清"的上限**。
    72 个让模型挑一个，等于抽奖。

    而且字母表只有 26 个，72 类根本编不下。

    ## 两步法的做法

        ① 先让模型**用自己的话**说这张图表达什么
           （要求 1~3 个表达意图关键词：「捣蛋」「求安慰」「得意」）
        ② 我们在本地拿这些关键词去匹配 72 个类别标签

    模型只做它擅长的（**描述**），"映射到哪个类别"由本地精确匹配 ——
    可控、可测、不会抽奖。

    ## 返回

    第二个值保留空字典，是为了兼容旧调用方的解包（`prompt, letter_map = ...`）。
    """
    cats = categories if categories else {}
    # 列出类别标签给模型**参考**（不给字母，只让它知道有什么说法）
    labels = [str(v) for v in cats.values()] if cats else []
    hint = ""
    if labels:
        # 只给前 40 个：太多会占满提示词，而且让模型分心
        hint = (
            "\n可用的说法举例（不限于这些，用你自己的话也行）：\n"
            + "、".join(labels[:40])
            + "\n"
        )

    prompt = (
        "这是一个表情包。请回答两个问题。\n"
        "\n"
        "问题一：这张图**想表达什么**？用 1~3 个**关键词**说清。\n"
        "  例如：捣蛋、得意、求安慰、无语、加油、委屈、想吃东西……\n"
        "  要选最贴切的意图，不要笼统地写「开心」「难过」。\n"
        f"{hint}"
        "\n"
        "问题二：用一句话（不超过 30 字）说清画面是什么 + 表达了什么。\n"
        "\n"
        "回答格式（严格两行）：\n"
        "意图：<1~3 个关键词，用顿号分隔>\n"
        "描述：<一句话>"
    )
    return prompt, {}


def label_sticker(
    image_bytes: bytes,
    config: Any,
    *,
    categories: Optional[dict[str, str]] = None,
) -> tuple[str, str, str]:
    """给表情包分类 + 标注。返回 `(emotion_key, caption, 失败原因)`。

    ## 参数

    `categories`：**磁盘上实际存在的**类别 `{key: 标签}`（2026-11-05 加）。
    不传则用 `media.EMOTIONS` 的默认五个。

    ⚠️ 分类是**用户自己建的目录**，所以调用方（标注循环）必须把
       `store.categories()` 传进来 —— 否则模型会往一个不存在的类别里分。

    ## 返回值

    `emotion_key` 是 `categories` 里的键（或 `other`）。
    失败（**网络/模型不可用**）时返回 `("", "", 原因)` —— 这种情况调用方
    应该保留原图在 `_inbox`，下次再试。

    ⚠️ 和"分类不确定"要区分开：不确定时返回 `("other", 描述, "")`，
       那也是一次**成功**（有描述就能用，只是归到其它）。
    """
    prompt, letter_map = build_label_prompt(categories)
    text, why = describe_image(
        image_bytes, config, prompt=prompt, max_chars=400
    )
    if not text:
        return "", "", why
    # 🔴 把类别表也传进去（2026-11-06）：解析要走"意图关键词 -> 类别"匹配
    emotion, caption = _parse_label(
        text, letter_map=letter_map, categories=categories
    )
    return emotion, caption, ""
