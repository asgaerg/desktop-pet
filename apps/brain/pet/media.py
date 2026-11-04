"""图片 / 表情包的存储与索引（2026-11-02 用户要求）。

## 用户原话（权威定义）

> 「做图片精准识别解析功能。」
>
> 「可以用图片和用户聊天，比如表情包，代替一些开心、悲伤、无语、
>   不理你这种简单和常见的状态表情。表情包也能进入原文记忆存放，
>   表情包和图片专门在项目下里一个文件夹储存。」
>
> 「1、用户自己放，放完之后让宠物熟悉下，自己个图片的内容打标注，
>   这样用的时候就能看标识自己选用了，2、宠物也拥有能自己上网补充
>   自己的表情包，小黑盒、B站评论区，抖音评论区，网上搜索都可以，
>   就是给于它下载的功能，这个下载功能也做成模块，
>   搜索模块搜索到之后，它自己分辨下载标注就好了」

## 目录布局（用户要"项目下一个文件夹"）

    <repo>/pet-media/
      ├─ stickers/           表情包（按状态分子目录）
      │   ├─ happy/          开心
      │   ├─ sad/            悲伤
      │   ├─ speechless/     无语
      │   ├─ ignore/         不理你
      │   └─ _inbox/         用户刚放进去、**还没标注**的
      ├─ images/             用户发给宠物的图片（对话用）
      └─ index.json          元数据索引（标注、来源、时间）

## 🔴 为什么元数据单独存 index.json 而不是靠文件名

文件名只能塞下很少信息，而且用户会重命名。真正要记的是：

    这张图表达什么情绪？有没有人（或宠物）给它打过标注？
    从哪儿来的？什么时候进来的？

这些必须结构化存。而**文件本身**放磁盘上（不塞进 SQLite 的 BLOB）——
用户要能自己去翻、自己去放，BLOB 做不到这一点。

## ⚠️ index.json 是**可重建**的缓存，不是唯一真源

真源是**磁盘上的文件**。index 丢了/坏了，`scan()` 能重新扫出来
（只是标注会丢，因为标注只存在 index 里）。

所以这个设计允许"手工删 index 重建"，也允许用户直接往 stickers/ 里丢图。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("pet.media")

#: 用户说的四种"简单常见状态"。键是内部名，值是给人看的中文。
#:
#: ⚠️ 只保留用户点名的四个 + `other` 兜底。不要擅自扩成十几个 ——
#:    分类越细，模型越难选对，而且素材根本凑不齐。
EMOTIONS: dict[str, str] = {
    "happy": "开心",
    "sad": "悲伤",
    "speechless": "无语",
    "ignore": "不理你",
    "other": "其它",
}

#: 允许的图片扩展名（大小写不敏感）。
IMAGE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"})

#: 单张图片的大小上限（字节）。表情包一般 < 500 KB。
#:
#: ⚠️ 设上限不是为了省盘，而是防用户误拖一个 20 MB 的 PSD/截图进来，
#:    之后每次识别都要传上去（token + 时间全爆）。
MAX_IMAGE_BYTES = 8 * 1024 * 1024

#: GIF 动图只取第一帧做标注（模型看不了动图）。
MAX_ANIMATED_BYTES = 8 * 1024 * 1024


def _sha1(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def _safe_name(name: str) -> str:
    """把任意名字洗成安全的文件名（保留中文）。"""
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name or "")
    name = name.strip(". ")
    return name[:80] or "untitled"


@dataclass
class MediaItem:
    """一张图片 / 一个表情包的元数据。"""

    #: 相对 `pet-media/` 的路径（用 `/` 分隔，跨平台稳定）
    path: str
    #: 内容 sha1，用来去重（同一张图放两次只算一个）
    sha1: str = ""
    #: 表情包属于哪一类（见 `EMOTIONS`）；普通图片为 ""
    emotion: str = ""
    #: 标注（这张图表达什么/画的是什么）。空 = 还没人标过
    caption: str = ""
    #: 谁标的：`user`（用户手动）/ `auto`（视觉模型自动）/ ""
    labeled_by: str = ""
    #: 来源：`user-put`（用户放进来）/ `downloaded`（宠物自己下的）/ `chat`（对话里发的）
    source: str = ""
    #: 来源 URL（下载来的才有）
    origin_url: str = ""
    size_bytes: int = 0
    added_at: float = field(default_factory=time.time)
    #: 用过几次（宠物发出去过几次）—— 用来避免老是发同一张
    used_count: int = 0
    last_used_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "sha1": self.sha1,
            "emotion": self.emotion,
            "caption": self.caption,
            "labeled_by": self.labeled_by,
            "source": self.source,
            "origin_url": self.origin_url,
            "size_bytes": self.size_bytes,
            "added_at": self.added_at,
            "used_count": self.used_count,
            "last_used_at": self.last_used_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "MediaItem":
        """宽容构造：字段缺失/类型不对都不抛。

        ⚠️ 这是**用户可手改的文件**（index.json 就在项目里，
           用户完全可能打开看看，手滑改坏）。解析不能崩。
        """
        def _s(key: str) -> str:
            return str(d.get(key) or "")

        def _f(key: str) -> float:
            try:
                return float(d.get(key) or 0.0)
            except (TypeError, ValueError):
                return 0.0

        def _i(key: str) -> int:
            try:
                return int(d.get(key) or 0)
            except (TypeError, ValueError):
                return 0

        return cls(
            path=_s("path"),
            sha1=_s("sha1"),
            emotion=_s("emotion"),
            caption=_s("caption"),
            labeled_by=_s("labeled_by"),
            source=_s("source"),
            origin_url=_s("origin_url"),
            size_bytes=_i("size_bytes"),
            added_at=_f("added_at"),
            used_count=_i("used_count"),
            last_used_at=_f("last_used_at"),
        )


class MediaStore:
    """`pet-media/` 的读写门面。

    ## 为什么不用数据库

    这个项目其它数据（记忆、事件）都在 SQLite 里。但图片不一样：

      · 用户要能**直接去文件夹里看/放/删**（原话「专门在项目下里一个文件夹储存」）
      · 图片本体是文件，塞进 BLOB 之后用户就没法翻了
      · 元数据只有几百条，JSON 完全够

    所以：**文件在磁盘、元数据在 index.json**。简单、可手改、可重建。
    """

    def __init__(self, root: Optional[Path] = None) -> None:
        """
        `root` 是 `pet-media/` 的绝对路径。不传则用项目根下的 `pet-media/`
        （和 `pet-books/` 同级，用户要求"项目下一个文件夹"）。
        """
        if root is None:
            from . import paths

            root = paths.project_root() / "pet-media"
        self.root = Path(root)
        self.index_path = self.root / "index.json"
        self._items: dict[str, MediaItem] = {}
        self._loaded = False

    # ------------------------------------------------------------ 目录

    def stickers_dir(self, emotion: str = "") -> Path:
        """表情包目录。给 `emotion` 就返回那一类的子目录。"""
        base = self.root / "stickers"
        if emotion:
            base = base / _safe_name(emotion)
        base.mkdir(parents=True, exist_ok=True)
        return base

    def inbox_dir(self) -> Path:
        """用户刚放进来、**还没标注**的表情包。"""
        d = self.root / "stickers" / "_inbox"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def images_dir(self) -> Path:
        """用户发给宠物的图片（对话用）。"""
        d = self.root / "images"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def ensure_dirs(self) -> None:
        """建好所有标准目录（含每个情绪的子目录）。

        ⚠️ 情绪子目录也要建：用户进去放图时能直接看到"该放哪个文件夹"，
           比让他先读文档再自己 mkdir 友好得多。
        """
        self.root.mkdir(parents=True, exist_ok=True)
        self.images_dir()
        self.inbox_dir()
        for key in EMOTIONS:
            if key != "other":
                self.stickers_dir(key)

    # ------------------------------------------------------------ 索引

    def load(self) -> dict[str, MediaItem]:
        """读 index.json。文件不存在/坏了都返回空（然后由 `scan` 重建）。"""
        if self._loaded:
            return self._items
        self._loaded = True
        if not self.index_path.is_file():
            return self._items
        try:
            raw = json.loads(self.index_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 —— 用户可能手改坏了
            log.warning("pet-media/index.json 读不了，稍后会重建", exc_info=True)
            return self._items
        items = raw.get("items") if isinstance(raw, dict) else raw
        if not isinstance(items, list):
            return self._items
        for d in items:
            if not isinstance(d, dict):
                continue
            item = MediaItem.from_dict(d)
            if item.path:
                self._items[item.path] = item
        return self._items

    def save(self) -> None:
        """写回 index.json（原子写：先写 .tmp 再替换）。"""
        self.root.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "updated_at": time.time(),
            "items": [it.to_dict() for it in self._items.values()],
        }
        tmp = self.index_path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(self.index_path)

    def scan(self) -> dict[str, MediaItem]:
        """**以磁盘为准**重建索引（保留已有的标注）。

        ## 什么时候需要它

        · 用户直接往 `stickers/happy/` 里丢了图（最常见）
        · index.json 被删/被改坏
        · 有人手工挪了文件

        ## 为什么保留标注

        标注只存在 index 里（磁盘上只有图）。重建时如果直接清空，
        宠物辛苦标的东西全没了。所以按 `sha1` 认回：
        **同一张图换个位置也还是那张图**。
        """
        self.load()
        by_sha: dict[str, MediaItem] = {
            it.sha1: it for it in self._items.values() if it.sha1
        }
        found: dict[str, MediaItem] = {}
        for path in self._iter_files():
            rel = path.relative_to(self.root).as_posix()
            try:
                data = path.read_bytes()
            except Exception:  # noqa: BLE001
                continue
            sha = _sha1(data)
            # 认回旧标注（按 sha1）
            old = by_sha.get(sha)
            emotion = self._emotion_from_path(rel)
            if old is not None:
                old.path = rel
                old.size_bytes = len(data)
                old.sha1 = sha
                if emotion and not old.emotion:
                    old.emotion = emotion
                found[rel] = old
            else:
                found[rel] = MediaItem(
                    path=rel,
                    sha1=sha,
                    emotion=emotion,
                    source="user-put" if rel.startswith("stickers/") else "chat",
                    size_bytes=len(data),
                )
        self._items = found
        self.save()
        return self._items

    def _iter_files(self) -> list[Path]:
        out: list[Path] = []
        if not self.root.is_dir():
            return out
        for p in self.root.rglob("*"):
            if p.is_file() and p.suffix.lower() in IMAGE_EXTS:
                # 跳过 index.json 之类（扩展名已经过滤了，这里防御）
                out.append(p)
        return out

    @staticmethod
    def _emotion_from_path(rel: str) -> str:
        """从路径推情绪：`stickers/happy/x.png` -> `happy`。

        推不出来（在 `_inbox` 或 `images/` 里）就返回空。
        """
        parts = rel.split("/")
        if len(parts) >= 3 and parts[0] == "stickers":
            name = parts[1]
            if name in EMOTIONS:
                return name
            if name.startswith("_"):
                return ""       # _inbox 之类
            # 用户可能自己建了中文目录（`stickers/开心/`）-> 反查
            for key, label in EMOTIONS.items():
                if name == label:
                    return key
        return ""

    # ------------------------------------------------------------ 增删改

    def add_file(
        self,
        data: bytes,
        filename: str,
        *,
        emotion: str = "",
        source: str = "user-put",
        origin_url: str = "",
        caption: str = "",
        labeled_by: str = "",
    ) -> Optional[MediaItem]:
        """把一张图存进 `pet-media/` 并登记。返回 `MediaItem`；失败返回 None。

        ## 去重

        同一张图（sha1 相同）只存一份：第二次放进来直接返回已有的那条，
        **不重复写文件**。用户反复拖同一张图不该把文件夹撑爆。

        ## 存哪儿

          · 有情绪 -> `stickers/<emotion>/`
          · 没情绪、来源是对话 -> `images/`
          · 没情绪、来源是用户放 -> `stickers/_inbox/`（等标注）
        """
        if not data:
            return None
        if len(data) > MAX_IMAGE_BYTES:
            log.info("图片太大（%.1f MB），不收", len(data) / 1024 / 1024)
            return None

        self.load()
        sha = _sha1(data)
        # 去重
        for it in self._items.values():
            if it.sha1 == sha:
                return it

        ext = Path(filename or "").suffix.lower()
        if ext not in IMAGE_EXTS:
            ext = ".png"
        stem = _safe_name(Path(filename or "img").stem)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
        name = f"{stamp}-{stem[:40]}{ext}"

        if emotion and emotion in EMOTIONS:
            target = self.stickers_dir(emotion) / name
        elif source == "chat":
            target = self.images_dir() / name
        else:
            target = self.inbox_dir() / name

        try:
            target.write_bytes(data)
        except Exception:  # noqa: BLE001
            log.warning("写图片失败：%s", target, exc_info=True)
            return None

        rel = target.relative_to(self.root).as_posix()
        item = MediaItem(
            path=rel,
            sha1=sha,
            emotion=emotion if emotion in EMOTIONS else "",
            caption=caption,
            labeled_by=labeled_by,
            source=source,
            origin_url=origin_url,
            size_bytes=len(data),
        )
        self._items[rel] = item
        self.save()
        log.info("图片入库：%s（%d KB，%s）", rel, len(data) // 1024, source)
        return item

    def move_to_emotion(self, item: MediaItem, emotion: str) -> bool:
        """把一个表情包挪进某个情绪目录（标注完成后调）。

        ## 为什么标注后要挪

        用户放图时是扔进 `_inbox/` 的。标完情绪再留在 `_inbox` 里，
        下次识别又会把它当"未标注"处理一遍 —— 白花钱。
        挪进 `stickers/<emotion>/` 之后，`_emotion_from_path` 就能认出来。
        """
        if emotion not in EMOTIONS:
            return False
        src = self.root / item.path
        if not src.is_file():
            return False
        dst_dir = self.stickers_dir(emotion)
        dst = dst_dir / src.name
        if dst == src:
            item.emotion = emotion
            self.save()
            return True
        try:
            if dst.exists():        # 同名的已存在 -> 加个后缀
                dst = dst_dir / f"{src.stem}-{int(time.time())}{src.suffix}"
            shutil.move(str(src), str(dst))
        except Exception:  # noqa: BLE001
            log.warning("挪动表情包失败：%s -> %s", src, dst, exc_info=True)
            return False
        self._items.pop(item.path, None)
        item.path = dst.relative_to(self.root).as_posix()
        item.emotion = emotion
        self._items[item.path] = item
        self.save()
        return True

    def annotate(self, item: MediaItem, caption: str, *, by: str = "auto") -> None:
        """给一张图写标注。"""
        item.caption = str(caption or "")[:200]
        item.labeled_by = by
        self.save()

    def mark_used(self, item: MediaItem) -> None:
        """记一次"宠物发出去了"（用于轮换，避免老发同一张）。"""
        item.used_count += 1
        item.last_used_at = time.time()
        self.save()

    # ------------------------------------------------------------ 查询

    def unlabeled(self) -> list[MediaItem]:
        """还没标注的表情包（用户新放进去的）。

        判据是 `caption` 为空 —— 不看 `labeled_by`：
        用户手动标过的和模型自动标的都算"标了"。
        """
        self.load()
        return [
            it
            for it in self._items.values()
            if it.path.startswith("stickers/") and not it.caption
        ]

    def stickers_for(self, emotion: str) -> list[MediaItem]:
        """某一类情绪下、**已标注**的表情包。

        ⚠️ 只要标注过的：没标注的不知道画的是什么，
           发出去可能闹笑话（用户看到的是一张莫名其妙的图）。
        """
        self.load()
        return [
            it
            for it in self._items.values()
            if it.path.startswith("stickers/")
            and it.emotion == emotion
            and it.caption
        ]

    def pick_sticker(self, emotion: str, *, rng: Any = None) -> Optional[MediaItem]:
        """挑一个表情包发出去。

        ## 选法：优先没怎么用过的

        老发同一张图会很呆。按 `used_count` 升序，前几名里随机挑 ——
        既保证轮换，又不是死板地按顺序发。
        """
        import random as _random

        pool = self.stickers_for(emotion)
        if not pool:
            return None
        pool.sort(key=lambda it: (it.used_count, it.last_used_at))
        top = pool[: max(1, min(3, len(pool)))]
        return (rng or _random).choice(top)

    def stats(self) -> dict[str, Any]:
        """给设置页/日志看的概览。"""
        self.load()
        by_emotion: dict[str, int] = {}
        for it in self._items.values():
            if it.path.startswith("stickers/"):
                key = it.emotion or "未分类"
                by_emotion[key] = by_emotion.get(key, 0) + 1
        return {
            "root": str(self.root),
            "total": len(self._items),
            "stickers": sum(by_emotion.values()),
            "unlabeled": len(self.unlabeled()),
            "by_emotion": by_emotion,
        }
