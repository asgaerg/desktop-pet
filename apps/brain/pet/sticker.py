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

#: 情绪标签 -> 表情包类别。
#:
#: ⚠️ `annoyed` 映射到 `ignore` 而不是 `speechless`：
#:    annoyed 是"不爽"，最贴切的表现是「不理你」而不是「无语」。
#:    `curious` / `neutral` / `sleepy` **不映射** —— 那三种情绪没有
#:    对应的表情包类别，硬映射只会发出不贴切的图。
_EMOTION_TO_STICKER: dict[str, str] = {
    "excited": "happy",
    "happy": "happy",
    "annoyed": "ignore",
    "sad": "sad",
}

#: 回复长度上限：超过这个字数就不配表情包了。
#:
#: 理由：长回复本身信息量够，再配一张图显得碎。
MAX_TEXT_FOR_STICKER = 60


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

      ① 情绪能映射到某个类别（`neutral`/`curious`/`sleepy` 不行）
      ② 回复够短（<= `MAX_TEXT_FOR_STICKER`）—— 长回复自己说得清
      ③ 概率抽中（`rate`，默认 0.35）

    ⚠️ 概率**必须 < 1**：每句都配图会让人觉得很吵、很假。
       0.35 的含义是"大约三句里有一句带图"，那才像人在聊天。
    """
    if not emotion_to_sticker(dominant):
        return False
    if len(str(reply_text or "").strip()) > MAX_TEXT_FOR_STICKER:
        return False
    r = rng or random
    return r.random() < max(0.0, min(1.0, rate))


def pick_for_emotion(
    store: MediaStore, dominant: str, rng: Optional[random.Random] = None
) -> Optional[MediaItem]:
    """按情绪挑一个表情包；没有就返回 None（安静地不发）。

    ## 为什么按情绪而不是按内容相似度

    用户要的是「代替简单状态」。状态就是情绪，不需要语义匹配 ——
    而语义匹配（嵌入向量）对表情包这种短内容反而不可靠，
    还会引入一次额外的模型调用。
    """
    key = emotion_to_sticker(dominant)
    if not key:
        return None
    try:
        item = store.pick_sticker(key, rng=rng)
    except Exception:  # noqa: BLE001 —— 挑图失败不该影响回复
        log.warning("挑表情包失败（已忽略）", exc_info=True)
        return None
    if item is None:
        # 这一类还没素材：安静地不发。**不要**打 warning ——
        # 用户可能就是想只用文字，没放表情包是很正常的状态。
        log.debug("表情包：%s 类还没有素材", key)
        return None
    return item


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
