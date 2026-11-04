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


def _parse_label(text: str) -> tuple[str, str]:
    """从模型回复里解析出 `(emotion_key, caption)`。

    ## 三层兜底（2026-11-02 实测出来的）

    实测这个模型**根本不按要求的格式回**。第一版只要「类别：」解析，
    结果一段散文进来、类别解析成空 —— 那次识别就白花钱了。

    现在三层：

      ① 有「类别：」-> 解析它（可能给字母 A~E，也可能给中文名）
      ② 没有 -> 在**整段文字**里找情绪关键词（"它看起来很开心" -> happy）
      ③ 还找不到 -> `other`（不是空！空会让它留在 _inbox 反复被识别）

    ⚠️ ③ 用 `other` 而不是空串：空串在 `media.unlabeled()` 里算"没标注"，
       下次扫描又会被拿去识别一遍，反复烧钱。
    """
    emotion = ""
    caption = ""

    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        # 「类别：A」/「类别: A」/「类别：开心」
        if s.startswith("类别") and ("：" in s or ":" in s):
            value = s.split("：", 1)[-1] if "：" in s else s.split(":", 1)[-1]
            value = value.strip().strip("。 ，,.、")
            # 可能是字母
            if value[:1].upper() in _LETTER_TO_KEY:
                emotion = _LETTER_TO_KEY[value[:1].upper()]
            else:
                for cn, key in _LABEL_TO_KEY.items():
                    if cn in value:
                        emotion = key
                        break
        elif s.startswith("描述") and ("：" in s or ":" in s):
            caption = (s.split("：", 1)[-1] if "：" in s else s.split(":", 1)[-1]).strip()

    # ② 关键词兜底
    if not emotion:
        for words, key in _KEYWORD_TO_KEY:
            if any(w in text for w in words):
                emotion = key
                break

    # ③ 最终兜底
    if not emotion:
        emotion = "other"

    if not caption:
        # 没有「描述：」就把整段压成一句（去掉过长的部分）
        flat = " ".join(x.strip() for x in text.splitlines() if x.strip())
        caption = flat[:180]
    return emotion, caption[:200]


def label_sticker(image_bytes: bytes, config: Any) -> tuple[str, str, str]:
    """给表情包分类 + 标注。返回 `(emotion_key, caption, 失败原因)`。

    `emotion_key` 是 `media.EMOTIONS` 的键（`happy` / `sad` / …）。
    失败（**网络/模型不可用**）时返回 `("", "", 原因)` —— 这种情况调用方
    应该保留原图在 `_inbox`，下次再试。

    ⚠️ 和"分类不确定"要区分开：不确定时返回 `("other", 描述, "")`，
       那也是一次**成功**（有描述就能用，只是归到其它）。
    """
    text, why = describe_image(
        image_bytes, config, prompt=LABEL_PROMPT, max_chars=400
    )
    if not text:
        return "", "", why
    emotion, caption = _parse_label(text)
    return emotion, caption, ""
