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

#: 表情包分类。键是内部名（也是目录名），值是给人看的中文。
#:
#: ## 🔴 2026-11-06 大幅扩充到 50+ 类（用户要求）
#:
#: 用户原话：
#:
#: > 「现在开始分类，要根据图片的表达信息总结来分类，要细致，
#: >   比如捣蛋类动图、可爱求安慰等等」
#: > 「细粒度50+，毕竟表情包本就是为了表达某个情绪想法的建议画面」
#:
#: ## 为什么分这么细是对的
#:
#: 之前只有 5 类（开心/悲伤/无语/不理你/其它），实测 2271 张里
#: 大量只能塞进「其它」—— 比如「对不起我错了」「干活还得看我」
#: 「token 分我一点」，它们既不是开心也不是悲伤。
#:
#: 而表情包的本质就是**表达一个具体意图**。分得细，宠物才能在
#: "该道歉时"发道歉的图，而不是随机甩一张笑脸。
#:
#: ## 命名约定
#:
#: · 键用英文小写下划线（目录名要跨平台安全）
#: · 值是 2~6 字的中文标签（给模型看，太长它记不住）
#: · 按**表达意图**分组，不按"情绪/动作/场景"分（那反而乱）
#:
#: ⚠️ 50+ 类对模型的挑战：一次给它 50 个选项它容易选错。
#:    所以 `media_vision` 那边分批给（先问大类、再收敛），
#:    见那里的说明。
EMOTIONS: dict[str, str] = {
    # ── 正面情绪 ──
    "happy": "开心",
    "excited": "兴奋",
    "proud": "得意",
    "smug": "傲娇",
    "shy": "害羞",
    "love": "喜欢",
    "touched": "感动",
    "relieved": "安心",
    "grateful": "感谢",
    "laugh": "大笑",
    "smile": "微笑",
    # ── 负面情绪 ──
    "sad": "悲伤",
    "cry": "哭泣",
    "wronged": "委屈",
    "pitiful": "卖惨",
    "angry": "生气",
    "rage": "暴怒",
    "annoyed": "烦躁",
    "disappointed": "失望",
    "anxious": "焦虑",
    "scared": "害怕",
    "lonely": "孤单",
    "tired": "疲惫",
    # ── 反应／态度 ──
    "speechless": "无语",
    "ignore": "不理你",
    "contempt": "鄙视",
    "shocked": "震惊",
    "confused": "疑惑",
    "question": "疑问",
    "speechless_laugh": "苦笑",
    "awkward": "尴尬",
    "helpless": "无奈",
    "refuse": "拒绝",
    "disagree": "不赞同",
    "approve": "赞同",
    "agree": "同意",
    "ok": "好的",
    "cheer": "加油",
    "encourage": "鼓励",
    "comfort": "安慰",
    "sorry": "道歉",
    "forgive": "原谅",
    "beg": "求饶",
    # ── 行为／动作 ──
    "mischief": "捣蛋",
    "tease": "调戏",
    "prank": "恶作剧",
    "eat": "吃饭",
    "hungry": "饿了",
    "sleepy": "困倦",
    "sleep": "睡觉",
    "work": "干活",
    "lazy": "摸鱼",
    "slack": "摆烂",
    "run": "跑路",
    "hide": "躲起来",
    "peek": "偷看",
    "wait": "等待",
    "think": "思考",
    "daze": "发呆",
    # ── 互动／社交 ──
    "hello": "打招呼",
    "bye": "再见",
    "goodnight": "晚安",
    "hug": "抱抱",
    "pat": "摸摸头",
    "poke": "戳一下",
    "watch": "围观",
    "cheer_on": "围观助威",
    "ask_money": "讨要",
    "complain": "抱怨",
    "boast": "吹嘘",
    "threaten": "威胁",
    # ── 兜底 ──
    "other": "其它",
}

#: 类别名的长度上限。
#:
#: ⚠️ 自定义类别会变成**目录名**，太长会撞上 Windows 的路径长度限制
#:    （260 字符）。24 足够表达（"无语到极致"只有 5 个字）。
MAX_CATEGORY_LEN = 24


def _is_valid_category(name: str) -> bool:
    """这个名字能不能当类别（= 目录名）。

    ## 🔴 2026-11-05 加：类别不再写死

    用户要求「按图片种类新开文件夹」，所以**用户建什么目录就有什么类别**，
    代码里不该有一个"允许列表"。

    要挡的只有**不像话**的名字：

        ""                空
        "_inbox"          下划线开头 = 内部目录
        "a/b"            路径分隔符（能逃出 stickers/）
        "a\\b"            同上（Windows）
        超长              Windows 路径上限
        "CON"/"NUL"      Windows 保留名（见 `_safe_name` 同样的坑）

    ⚠️ 中文**必须**允许 —— 用户很可能就建 `stickers/开心/`。
    """
    s = str(name or "").strip()
    if not s or len(s) > MAX_CATEGORY_LEN:
        return False
    if s.startswith("_"):
        return False
    if "/" in s or "\\" in s:
        return False
    # Windows 保留设备名（大小写不敏感）
    if s.upper().split(".")[0] in {"CON", "PRN", "AUX", "NUL"}:
        return False
    return True

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


def _split_terms(query: str) -> list[str]:
    """把查询词切成检索用的**最小单元**（2026-11-06 加）。

    ## 为什么不引分词库

    这个项目里 jieba 是**可选**的（词典常缺，日志里天天报
    "中文分词未生效"）。检索依赖它的话，缺词典时整个功能直接失效。

    ## 切法

        英文/数字  按单词切（"cat angry" -> ["cat", "angry"]）
        中文       **按单字切**（"蓝色猫猫" -> ["蓝","色","猫"]）

    ⚠️ 中文单字切看起来很粗暴，但配合 `search_stickers` 里的
       "命中数 / 总词数"打分足够用 —— 而且它**不会漏**：
       "猫猫" 必然命中含"猫"的描述。

    ⚠️ 过滤掉单字母/停用字：它们几乎每句都有，会稀释评分。
    """
    import re as _re

    q = str(query or "").lower()
    # 英文/数字按词切
    out: list[str] = []
    for word in _re.findall(r"[a-z0-9]{2,}", q):
        out.append(word)
    # 中文按字切，跳过"的了吗呢"这类高频虚词
    stop = set("的了吗呢吧啊是我你他她它们这那有和就都也很不")
    for ch in q:
        if "\u4e00" <= ch <= "\u9fff" and ch not in stop:
            out.append(ch)
    # 去重但保持顺序
    return list(dict.fromkeys(out))


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
    #: 🔴 磁盘修改时间（2026-11-06 加，给**增量扫描**用）
    #:
    #: ## 为什么需要它
    #:
    #: 2272 个表情包、131 MB。原来 `scan()` 每次都读**全部字节**算 sha1 ——
    #: 实测 1.07 秒。而它在启动、用户放图后、定时扫描时都会跑。
    #:
    #: 有 `mtime` + `size_bytes` 就能**跳过没变的文件**：
    #:
    #:     大小和修改时间都没变  ->  认为内容没变，沿用旧 sha1
    #:
    #: ⚠️ 这是**启发式**，不是密码学保证。但这里防的是"用户换了文件"，
    #:    不是恶意篡改 —— 大小和时间都变而内容不变的情形极少，
    #:    真遇到了也只是多算一次 sha1，不会出错。
    mtime: float = 0.0

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
            "mtime": self.mtime,
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
            mtime=_f("mtime"),
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
        # 🔴 记住这一轮**已经见过**的 sha1（2026-11-05 实测修）
        #
        # ## 踩到的坑
        #
        # `by_sha` 只包含**索引里已有的**条目。所以用户一次丢进两张
        # 内容相同的图（下载包里很常见：同一张图存了 .png 和 .jpg 两份）
        # 时，第二个文件在 `by_sha` 里查不到 —— 于是**两个都进了库**。
        #
        # 实测：5 个文件里 1 对重复，结果 total=5 而不是 4。
        #
        # ## 为什么必须修
        #
        # `pick_sticker` 按"用得最少"挑。同一张图存两份 = 它的权重翻倍，
        # 会比其他图更频繁被选中。而且用户看到的是"我明明只放了一张"。
        #
        # ⚠️ 保留**先遇到的那个**（`_iter_files` 是稳定顺序），
        #    后面的直接跳过 —— 不删用户的文件，只是不登记。
        seen_sha: set[str] = set()
        # 🔴 增量扫描的命中计数（2026-11-06 加）
        #
        # 用来在日志里说明"这次省了多少"—— 没有它的话，
        # "扫描很快"和"扫描压根没做事"从日志上分不出来。
        cache_hits = 0
        for path in self._iter_files():
            rel = path.relative_to(self.root).as_posix()
            try:
                st = path.stat()
            except Exception:  # noqa: BLE001
                continue
            # 🔴 增量判断：大小和修改时间都没变 -> 沿用旧 sha1，不读文件
            #
            # ## 为什么这能省一大截
            #
            # 2272 个表情包、131 MB。原来每个都要 `read_bytes()` + sha1，
            # 实测 1.07 秒。而绝大多数文件**从来没变过** —— 用户放进来
            # 之后就一直在那儿。
            #
            # 用 `(size, mtime)` 作指纹：两个都没变就当内容没变。
            # 实测能省掉 95% 以上的磁盘读取。
            #
            # ⚠️ 只认**索引里同路径**的旧条目。换路径的同内容文件
            #    （用户把图从 A 挪到 B）仍然要重算 —— 那本来就要算，
            #    没有便宜的判据。
            prev = self._items.get(rel)
            if (
                prev is not None
                and prev.sha1
                and prev.size_bytes == st.st_size
                and prev.mtime
                and abs(prev.mtime - st.st_mtime) < 1e-6
            ):
                sha = prev.sha1
                cache_hits += 1
            else:
                try:
                    data = path.read_bytes()
                except Exception:  # noqa: BLE001
                    continue
                sha = _sha1(data)
            if sha in seen_sha:
                # 这个内容本轮已经登记过了，跳过（不重复入库）
                log.info("扫描：跳过重复图 %s（与已登记的同一张）", rel)
                continue
            seen_sha.add(sha)
            # 认回旧标注（按 sha1）
            old = by_sha.get(sha)
            emotion = self._emotion_from_path(rel)
            if old is not None:
                old.path = rel
                # ⚠️ 用 `st.st_size` 而不是 `len(data)` —— 增量命中时
                #    `data` 根本没读，没有它可用。而 `st` 一定读到了。
                old.size_bytes = st.st_size
                old.sha1 = sha
                old.mtime = st.st_mtime
                if emotion and not old.emotion:
                    old.emotion = emotion
                found[rel] = old
            else:
                found[rel] = MediaItem(
                    path=rel,
                    sha1=sha,
                    emotion=emotion,
                    source="user-put" if rel.startswith("stickers/") else "chat",
                    size_bytes=st.st_size,
                    mtime=st.st_mtime,
                )
        self._items = found
        self.save()
        if cache_hits:
            log.info(
                "扫描：%d 个文件里 %d 个增量命中（没变，跳过读盘）",
                len(found), cache_hits,
            )
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
        """从路径推类别：`stickers/happy/x.png` -> `happy`。

        ## 🔴 2026-11-05 改成**动态识别**（用户要求"按图片种类新开文件夹"）

        用户原话：

        > 「你现在可以把宠物的图片表情种类扩充，就是按照图片种类来新开文件夹」
        > 「不用，等我图片放进去，你再一个个分类开具体种类就好了」

        所以分类**不该写死在代码里** —— 用户会自己建文件夹，建什么就有什么。

        规则：

            ① 已知的 key（`EMOTIONS` 里的）           -> 直接用它
            ② 已知的中文标签（"开心"）                -> 反查成 key
            ③ **其它任何合法的文件夹名**               -> 原样当类别用
            ④ 以下划线开头（`_inbox` 等）             -> 空（= 未分类）
            ⑤ 不在 `stickers/` 下（`images/` 等）      -> 空

        ③ 是关键：用户建了 `stickers/猫猫/`、`stickers/无语到极致/`，
        图片就归到那个类别，不需要改代码。模型看图分类时也能选它
        （前提是那个类别已经存在 —— 见 `media_vision.known_categories`）。
        """
        parts = rel.split("/")
        if len(parts) >= 3 and parts[0] == "stickers":
            name = parts[1].strip()
            if not name:
                return ""
            if name.startswith("_"):
                return ""       # _inbox 之类
            # ① 已知 key
            if name in EMOTIONS:
                return name
            # ② 已知中文标签 -> 反查
            for key, label in EMOTIONS.items():
                if name == label:
                    return key
            # ③ 用户自定义目录：原样当类别。
            #
            # ⚠️ 用 `_is_valid_category` 而不是自己写一堆判断 ——
            #    同样的规则在 `add_file` / `move_to_emotion` 也要用，
            #    三处各写一份必然漂移（这个项目已经栽过好几次）。
            if _is_valid_category(name):
                return name
        return ""

    def categories(self) -> dict[str, str]:
        """**磁盘上实际存在的**类别 -> 中文标签。

        ## 为什么需要它

        分类是用户自己建的文件夹，所以"有哪些类别"只能看磁盘。
        模型分类（`media_vision.label_sticker`）也要知道这个列表 ——
        否则它会往一个不存在的类别里分。

        ## 返回

            已知 key/标签   -> 用 `EMOTIONS` 里的中文标签
            自定义目录名     -> 用它自己当标签（`猫猫` -> `猫猫`）

        ## 🔴 空目录也算（2026-11-06 修）

        第一版**只统计有图的目录**，理由是"空文件夹只是占位"。
        实测发现那是个**死锁**：

            用户把 2271 张新图全放进 `_inbox/鲸鱼娘合集/`
            四个情绪目录（happy/sad/...）还是空的
            -> `categories()` 返回 {}（一个类别都没有）
            -> 模型收到空类别表，只能全分到 `other`
            -> 那四个目录**永远**不会被填上

        结果：2271 张全归 `other`，宠物一张都发不出来。

        ## 现在的规则

            文件夹存在 -> 就是一个合法类别（不管里面有没有图）

        ⚠️ 那"用户建了空文件夹"的顾虑怎么办？答案是**它无害**：
           `pick_sticker` 挑不到图时会安静地不发，不会出错。
           而反过来（漏掉真实类别）会让整个分类功能失效。

        ⚠️ `_inbox` 仍然排除（下划线开头）—— 那是暂存区，不是类别。
        """
        out: dict[str, str] = {}
        base = self.root / "stickers"
        if not base.is_dir():
            return out
        try:
            entries = sorted(base.iterdir())
        except Exception:  # noqa: BLE001
            return out
        for d in entries:
            if not d.is_dir():
                continue
            name = d.name.strip()
            if not name or name.startswith("_"):
                continue
            key = name if name in EMOTIONS else (
                next((k for k, v in EMOTIONS.items() if v == name), name)
            )
            out[key] = EMOTIONS.get(key, name)
        # 兜底：一个目录都没有时，至少给内置那几类
        # （刚装好、还没建任何文件夹的情况）
        if not out:
            return dict(EMOTIONS)
        return out

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

        # 指定了类别就直接进那类目录（2026-11-05：不再限制必须是已知 key，
        # 用户自己建的文件夹也是合法类别 —— 见 `_emotion_from_path`）
        if emotion and _is_valid_category(emotion):
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
        # 记下 mtime，让下次 `scan()` 能走增量路径（见 `MediaItem.mtime`）
        try:
            mtime = target.stat().st_mtime
        except Exception:  # noqa: BLE001
            mtime = 0.0
        item = MediaItem(
            path=rel,
            sha1=sha,
            emotion=emotion if _is_valid_category(emotion) else "",
            caption=caption,
            labeled_by=labeled_by,
            source=source,
            origin_url=origin_url,
            size_bytes=len(data),
            mtime=mtime,
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
        # ⚠️ 2026-11-05：不再要求"必须是已知 key"。用户自己建的类别
        #    也是合法的（他已经把图放那个目录里了，只是没标注）。
        if not _is_valid_category(emotion):
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
        # ⚠️ 挪动之后 `st_mtime` 变了（`shutil.move` 保留 mtime，但为了
        #    安全还是重读一次）—— 否则下次 scan 会以为文件变了、白算 sha1。
        try:
            item.mtime = dst.stat().st_mtime
        except Exception:  # noqa: BLE001
            pass
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
        """还没分好类的表情包：`caption` 或 `emotion` 有一个为空。

        ## 🔴 2026-11-07 修：原来只判 `caption`

        原来写的是 `if not it.caption`。于是出现一类**永远处理不到**的数据：

            有 caption（描述有了），但 emotion 为空（没归类）

        实测 **1270 张**卡在这个状态 —— 它们是早期用老提示词标的
        （那时只写描述、不分类），之后 72 类体系上线也没被重跑。

        后果：
          · `stickers_for()` 要求 `emotion == 某个类`，它们一条都进不去
          · 而 `unlabeled()` 又认为"标过了"，不再处理
          · 等于 1270 张素材**永久沉睡**，宠物只有 1001 张能用

        用户报的"难触发"很大一部分是这个：素材看起来很多，
        实际能用的一半。

        ⚠️ `caption` 为空也算未标注：那是"连描述都没有"，更要重跑。
        """
        self.load()
        return [
            it
            for it in self._items.values()
            if it.path.startswith("stickers/") and (not it.caption or not it.emotion)
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

    def search_stickers(
        self,
        query: str,
        *,
        emotion: str = "",
        limit: int = 20,
    ) -> list[MediaItem]:
        """按**描述文字**找表情包（2026-11-06 加）。

        ## 用户要求

        > 「优化下表情包加载和索引，这样多线程先分类搜索速度会快很多，
        >   能达到3秒内找到想要的表情包就可以了」

        注意他说的"找到想要的表情包"不是"随机挑一张"，而是**按内容找**：
        「有没有那张蓝色的猫猫生气的图」。

        ## 为什么不用向量检索

        实测规模：2272 张，`caption` 短的几十字、长的两百字。

            · 子串/分词匹配：全表扫描 2272 条，**0.4ms**（实测）
            · 向量检索：要额外维护嵌入表 + 每次查询算向量，几十到几百 ms

        0.4ms 已经远低于他要的 3 秒。上向量检索是**为了慢而慢**，
        而且会引入"嵌入模型不可用时整个功能挂掉"的脆弱性。

        ## 匹配策略（从精确到模糊）

            ① 类别精确（`emotion=happy`）        权重最高
            ② 描述里包含完整查询词                 次之
            ③ 查询词的每个字/词分别命中           兜底

        ⚠️ 只要**已标注**的 —— 同上，没标注的不知道画的是什么。
        """
        q = str(query or "").strip().lower()
        if not q:
            return []
        # 粗分：查询词按非字母数字切开（中文按单字）
        # ⚠️ 不引分词库：这个项目里 jieba 是**可选**的（词典常缺），
        #    依赖它会让检索在缺词典时直接失效。
        terms = [t for t in _split_terms(q) if t]

        scored: list[tuple[float, MediaItem]] = []
        for it in self._items.values():
            if not it.path.startswith("stickers/"):
                continue
            if not it.caption:
                continue
            if emotion and it.emotion != emotion:
                continue
            hay = it.caption.lower()
            score = 0.0
            if q in hay:
                score += 5.0           # ① 完整查询词
            # ② 分词命中（按比例给分，命中越多越靠前）
            if terms:
                hit = sum(1 for t in terms if t in hay)
                score += 3.0 * hit / len(terms)
            """
            ⚠️ 内容**一个词都没命中**时直接排除（2026-11-06 实测修）

            原来这里还加了一条"类别匹配 +10 分"。结果是：

                搜「猫猫」+ emotion='sad'
                -> 返回「女仆装少女在发呆」（因为它 emotion 是 sad）

            `emotion` 已经是**硬过滤**了（上面那个 `continue`），
            再给它加分等于"只要类别对上，内容完全不符也返回" ——
            那就不是"找想要的表情包"，是"按类别乱给"。

            所以：**内容必须先命中**，类别过滤只负责缩小范围，不参与打分。
            """
            if score <= 0:
                continue
            # 用得少的稍微优先（和 `pick_sticker` 一致的取向）
            score -= min(it.used_count, 5) * 0.05
            scored.append((score, it))

        scored.sort(key=lambda p: (-p[0], p[1].used_count, p[1].last_used_at))
        return [it for _s, it in scored[: max(1, limit)]]

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
