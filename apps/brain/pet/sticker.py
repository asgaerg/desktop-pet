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
#: ⚠️ `neutral` 仍然**不映射**：「平静」没有对应表情，硬发一张反而怪。
#:    那种情况交给 `intent_to_sticker`（看回复内容）判断。
_EMOTION_TO_STICKER: dict[str, str] = {
    "excited": "excited",
    "happy": "happy",
    "annoyed": "annoyed",
    "sad": "sad",
    "sleepy": "sleepy",
    "curious": "question",
}

#: 回复长度上限：超过这个字数就不配表情包了。
#:
#: 理由：长回复本身信息量够，再配一张图显得碎。
MAX_TEXT_FOR_STICKER = 60

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
    (("为什么", "怎么会", "真的吗", "？"), "question"),
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
    # ① 意图命中 or 心情能映射，都算"有可用类别"
    if not intent_to_sticker(reply_text) and not emotion_to_sticker(dominant):
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
    # ② 心情兜底
    mood_key = emotion_to_sticker(dominant)
    if mood_key and mood_key not in candidates:
        candidates.append(mood_key)

    for key in candidates:
        try:
            item = store.pick_sticker(key, rng=rng)
        except Exception:  # noqa: BLE001 —— 挑图失败不该影响回复
            log.warning("挑表情包失败（已忽略）", exc_info=True)
            item = None
        if item is not None:
            if key != candidates[0]:
                log.debug("表情包：意图类 %s 没素材，退回心情类 %s", candidates[0], key)
            return item
        log.debug("表情包：%s 类还没有素材", key)

    # ③ 都没有：安静地不发（不打 warning —— 只用文字是很正常的状态）
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
