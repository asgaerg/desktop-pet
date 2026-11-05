"""宠物联网搜到的信息：归档 + 分析 + 持续更新（2026-11-05 用户要求）。

## 用户原话

> 「将看小说和小黑盒杂谈等联网搜索到的信息通通放在项目下的一个文件夹中，
>   这里面专门存放宠物对于联网搜索到的新信息，宠物搜索完之后会对信息进行
>   分析、总结、发散思维，这些都记在里面，比如小说读后的感觉等等」
> 「这个存储联网搜索信息和总结的功能就做成信息存储模块吧，
>   后续如果搜索联网数据有更新也可以在里面更新，不断添加，
>   这样怎么做数据结构好呢」

## 🔴 数据结构：**实体 + 版本**，不是追加日志

这是本模块最重要的设计决定。用户明确要「后续更新也可以在里面更新，不断添加」，
所以不能简单地"每搜一次写一个文件"——那样同一本书读两次会变成两条互不相干的记录。

### 两层结构

```
    Entity（实体）        一个"东西"：某本书 / 某个网页 / 某个搜索主题
      └─ Revision（版本）  它每次被重新读/重新搜时产生的一版总结
```

    天官赐邪
      ├─ v1  2026-11-05  第 1~3 章  → "暴雨夜，吴献调查失踪案…"
      ├─ v2  2026-11-06  第 4~10 章 → "案情指向一个叫『红衣』的东西…"
      └─ v3  2026-11-08  第 11~20 章 → "…"
    （`current` 永远指向最新那版）

### 为什么不是"一个文件一直追加"

那正是 `pet-books/` 的做法，但**那里是正文、这里是总结**：

    正文      追加是对的 —— 第 5 章接在第 4 章后面，天然有序
    总结      追加是错的 —— 第二版总结**推翻**第一版，不是接在它后面
                            （"我原以为凶手是 A，读到 10 章发现是 B"）

所以总结要**留版本**：既能看到"我现在怎么想"，也能回看"我以前怎么想"。
用户要的"发散思维"特别需要这个 —— 前后想法的变化本身就是有趣的。

### key 怎么定（实体身份）

    reading    书名归一化            "天官赐邪"
    community  URL 的 sha1 前 12 位   "u-3f2a1c9d8e7b"
    search     查询词归一化            "量子纠缠是什么"

⚠️ 网页用 URL 哈希而不是 URL 本身当 key：URL 可能很长（带一堆参数）、
   含 `/` `?` 这类文件名非法字符。哈希短、安全、且同样稳定。

## 存储：文件存内容，JSON 存索引

用户问过「放数据库好还是怎么好」。实测依据：

    当前记忆库 16.2 MB（1123 条原文 + 317 条提炼）
    一本书 50 章约 90 KB，一次网页全文几十 KB

如果这些进数据库，几个月就是几百 MB，备份和检索都会变慢。
而且用户明确要"放在项目下的一个文件夹"——他要能自己去翻。

所以：

    正文 + 分析 + 想法   -> **文件**（.md）
    索引（key/标题/时间） -> **index.json**

⚠️ 索引**不含正文**，只含摘要（几百字）。检索走内存，够快。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("pet.knowledge")

#: 四大类，各占一个子目录。
#:
#: ⚠️ 分类不是"好看"，是**检索时的第一层过滤**：
#:    「我上次看的那本书」和「我搜过的资料」不该混在一起。
CATEGORIES: dict[str, str] = {
    "reading": "小说读后",
    "community": "社区杂谈",
    "search": "自主搜索",
    "other": "其它",
}

INDEX_NAME = "index.json"

#: 每个实体最多留几版。
#:
#: ⚠️ 有上限：一本书读 50 次就有 50 版总结，那些文件加起来很可观，
#:    而且用户真正会翻的通常是"最近几次"。超了就删最老的
#:    （**正文不删** —— 正文在 `pet-books/`，这里只删总结）。
MAX_REVISIONS_PER_ENTITY = 12


def knowledge_dir(root: Optional[Path] = None) -> Path:
    """归档根目录（不存在就建）。

    ## 路径优先级

        ① 显式传的 `root`
        ② 环境变量 `PET_KNOWLEDGE_DIR`
        ③ `<项目根>/pet-knowledge`

    ## ⚠️ 为什么要有 ②

    自检（`selftest`）会构造十几个 Gateway，每个都建一个 `KnowledgeStore`。
    没有 ② 的话它们全都指向**真实的** `F:\ai\pet-knowledge` ——
    测一次就在用户的项目里留下一堆 `reading/测试书/`。

    这和 `PET_DATA_DIR`（记忆库）、`tempfile` 前缀是同一个套路：
    **测试必须能改道**，不能真的去动用户的文件。
    """
    import os

    if root is None:
        env_dir = os.environ.get("PET_KNOWLEDGE_DIR")
        if env_dir:
            root = Path(env_dir)
        else:
            from . import paths

            root = paths.project_root() / "pet-knowledge"
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    for sub in CATEGORIES:
        (root / sub).mkdir(exist_ok=True)
    return root


def normalize_key(text: str) -> str:
    """把标题/查询词归一化成稳定 key。

    只保留中英文数字，去掉空白和标点 —— 这样
    「天官赐邪」「天官赐邪 」「天官赐邪。」是同一个实体。
    """
    cleaned = re.sub(r"[^\w\u4e00-\u9fff]+", "", str(text or "")).lower()
    return cleaned[:80]


def key_for_url(url: str) -> str:
    """网页实体的 key：URL 规范化后取哈希。

    ⚠️ 不用 URL 本身当 key：可能很长、含 `/ ? & =`（文件名非法），
       而且同一篇文章的 URL 常常带不同的跟踪参数。
    """
    base = str(url or "").strip()
    # 去掉常见的跟踪参数，让同一篇文章落到同一个 key
    base = re.sub(r"[?&](utm_[^&]*|spm=[^&]*|from=[^&]*)", "", base)
    digest = hashlib.sha1(base.encode("utf-8", "replace")).hexdigest()[:12]
    return f"u-{digest}"


def _safe_name(text: str, limit: int = 50) -> str:
    """目录/文件名安全化。中文保留（用户要自己看懂）。"""
    cleaned = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", str(text or "")).strip()
    return cleaned[:limit] or "untitled"


@dataclass
class Revision:
    """一个版本：某次读/搜之后的分析与想法。"""

    #: 版本号（从 1 开始）
    n: int
    #: 相对 `pet-knowledge/` 的路径
    path: str
    added_at: float
    #: 这一版覆盖了什么（人话："第 4~10 章" / "全文"）
    scope: str = ""
    #: 模型的分析（这篇讲了什么）
    analysis: str = ""
    #: 模型的发散（读后感 / 联想）
    thoughts: str = ""
    #: 这一版的字数（原文长度）
    chars: int = 0
    #: 🔴 章节区间（2026-11-05 用户问"取两次读取的中间章节回看"时加的）
    #:
    #: 光有 `scope` 那句人话不够：用户/模型想问"第 7 章那次我怎么看的"
    #: 时，得能**按章号筛版本**，而不是只能看最新的。
    #:
    #: `0` = 不适用（网页/搜索没有章节）
    chapter_from: int = 0
    chapter_to: int = 0

    def covers(self, chapter: int) -> bool:
        """这一版是否覆盖了第 `chapter` 章。"""
        if chapter <= 0 or self.chapter_from <= 0:
            return False
        return self.chapter_from <= chapter <= self.chapter_to

    def to_dict(self) -> dict[str, Any]:
        return {
            "n": self.n,
            "path": self.path,
            "added_at": self.added_at,
            "scope": self.scope,
            "analysis": self.analysis,
            "thoughts": self.thoughts,
            "chars": self.chars,
            "chapter_from": self.chapter_from,
            "chapter_to": self.chapter_to,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Revision":
        try:
            n = int(data.get("n") or 0)
        except (TypeError, ValueError):
            n = 0
        try:
            added = float(data.get("added_at") or 0)
        except (TypeError, ValueError):
            added = 0.0
        try:
            chars = int(data.get("chars") or 0)
        except (TypeError, ValueError):
            chars = 0
        try:
            cfrom = int(data.get("chapter_from") or 0)
        except (TypeError, ValueError):
            cfrom = 0
        try:
            cto = int(data.get("chapter_to") or 0)
        except (TypeError, ValueError):
            cto = 0
        return cls(
            n=n,
            path=str(data.get("path") or ""),
            added_at=added,
            scope=str(data.get("scope") or ""),
            analysis=str(data.get("analysis") or ""),
            thoughts=str(data.get("thoughts") or ""),
            chars=chars,
            chapter_from=cfrom,
            chapter_to=cto,
        )


@dataclass
class Entity:
    """一个实体：某本书 / 某个网页 / 某个搜索主题。

    它的 `revisions` 按时间顺序排列，`latest()` 是最新那版。
    """

    key: str
    title: str
    category: str
    source: str = ""
    #: 标签，给检索用
    tags: list[str] = field(default_factory=list)
    revisions: list[Revision] = field(default_factory=list)

    def latest(self) -> Optional[Revision]:
        return self.revisions[-1] if self.revisions else None

    @property
    def updated_at(self) -> float:
        return self.revisions[-1].added_at if self.revisions else 0.0

    @property
    def first_at(self) -> float:
        return self.revisions[0].added_at if self.revisions else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "title": self.title,
            "category": self.category,
            "source": self.source,
            "tags": list(self.tags),
            "revisions": [r.to_dict() for r in self.revisions],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Entity":
        revs = data.get("revisions")
        tags = data.get("tags")
        return cls(
            key=str(data.get("key") or ""),
            title=str(data.get("title") or ""),
            category=str(data.get("category") or "other"),
            source=str(data.get("source") or ""),
            tags=[str(t) for t in tags] if isinstance(tags, list) else [],
            revisions=[
                Revision.from_dict(r) for r in revs if isinstance(r, dict)
            ]
            if isinstance(revs, list)
            else [],
        )


class KnowledgeStore:
    """归档库：文件写内容，JSON 写索引，实体带版本。"""

    def __init__(self, root: Optional[Path] = None) -> None:
        self.root = knowledge_dir(root)
        self._index_path = self.root / INDEX_NAME
        self._entities: dict[str, Entity] = {}
        self.load()

    # ------------------------------------------------------------------ 读写

    def load(self) -> None:
        """读索引。坏文件不该让宠物起不来 —— 空了就从头开始。"""
        self._entities = {}
        if not self._index_path.exists():
            return
        try:
            data = json.loads(self._index_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            log.warning("知识索引读不出来（从空开始）：%s", self._index_path, exc_info=True)
            return
        raw = data.get("entities") if isinstance(data, dict) else None
        if not isinstance(raw, list):
            return
        for item in raw:
            if not isinstance(item, dict):
                continue
            entity = Entity.from_dict(item)
            if entity.key:
                self._entities[entity.key] = entity

    def save(self) -> None:
        """原子写索引（先 .tmp 再 replace），避免断电写坏。"""
        payload = {
            "version": 2,
            "root": str(self.root),
            "entities": [e.to_dict() for e in self._entities.values()],
        }
        tmp = self._index_path.with_suffix(".json.tmp")
        try:
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            tmp.replace(self._index_path)
        except Exception:  # noqa: BLE001
            log.warning("知识索引写不进去：%s", self._index_path, exc_info=True)

    # ------------------------------------------------------------------ 写入

    def upsert(
        self,
        *,
        key: str,
        title: str,
        category: str,
        source: str = "",
        analysis: str = "",
        thoughts: str = "",
        body: str = "",
        scope: str = "",
        tags: Optional[list[str]] = None,
        chapter_from: int = 0,
        chapter_to: int = 0,
    ) -> Optional[Revision]:
        """**新增或更新**一个实体（有则加一版，无则建实体）。

        这是本模块的主入口 —— 用户要的"不断添加、可更新"就靠它。

        ## 行为

        ```
        key 不存在  -> 建实体 + v1
        key 已存在  -> 追加 v(n+1)，实体标题/标签/来源按最新一次更新
        ```

        ⚠️ 标题和标签会**被最新一次覆盖**：如果模型这次给了更好的书名，
           应该用它。但 `key` 不变，所以历史版本都还在同一个实体下。
        """
        cat = category if category in CATEGORIES else "other"
        entity = self._entities.get(key)

        if entity is None:
            entity = Entity(key=key, title=title, category=cat, source=source)
            self._entities[key] = entity
        else:
            # 标题/来源/分类以最新一次为准（模型这次可能看得更准）
            if title:
                entity.title = title
            if source:
                entity.source = source
            if cat != "other":
                entity.category = cat

        if tags:
            # 合并而不是替换：旧标签可能仍然有效
            merged = list(dict.fromkeys([*entity.tags, *tags]))
            entity.tags = merged[:20]

        n = len(entity.revisions) + 1
        stamp = time.strftime("%Y%m%d-%H%M")
        folder = self.root / cat / _safe_name(entity.title)
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except Exception:  # noqa: BLE001
            log.warning("知识实体目录建不出来：%s", folder, exc_info=True)
            return None

        filename = f"v{n}-{stamp}.md"
        target = folder / filename
        rel = str(target.relative_to(self.root)).replace("\\", "/")

        text = self._render(
            title=entity.title,
            category=cat,
            source=source or entity.source,
            scope=scope,
            revision_n=n,
            analysis=analysis,
            thoughts=thoughts,
            body=body,
        )
        try:
            target.write_text(text, encoding="utf-8")
        except Exception:  # noqa: BLE001
            log.warning("知识归档写文件失败：%s", target, exc_info=True)
            return None

        rev = Revision(
            n=n,
            path=rel,
            added_at=time.time(),
            scope=scope,
            analysis=analysis,
            thoughts=thoughts,
            chars=len(body or ""),
            chapter_from=int(chapter_from or 0),
            chapter_to=int(chapter_to or 0),
        )
        entity.revisions.append(rev)

        # 超上限就删最老的**文件**（索引里也去掉）
        while len(entity.revisions) > MAX_REVISIONS_PER_ENTITY:
            old = entity.revisions.pop(0)
            try:
                (self.root / old.path).unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass
            log.info("知识归档：%s 超出 %d 版，删掉最老的 v%d",
                     entity.title, MAX_REVISIONS_PER_ENTITY, old.n)

        self.save()
        log.info(
            "知识归档：%s v%d（%s，%s 字）",
            entity.title, n, CATEGORIES.get(cat, cat), len(body or ""),
        )
        return rev

    @staticmethod
    def _render(
        *,
        title: str,
        category: str,
        source: str,
        scope: str,
        revision_n: int,
        analysis: str,
        thoughts: str,
        body: str,
    ) -> str:
        """渲染成 Markdown。顺序按"最想看的在前"：分析 -> 想法 -> 原文。"""
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime())
        head = f"> 来源：{source or '未知'} · 第 {revision_n} 版 · 归档：{when}"
        if scope:
            head += f" · 覆盖：{scope}"
        head += f" · 类别：{CATEGORIES.get(category, category)}"

        parts = [f"# {title}", "", head, ""]
        if analysis:
            parts += ["## 我的分析", "", analysis.strip(), ""]
        if thoughts:
            parts += ["## 我的想法", "", thoughts.strip(), ""]
        if body:
            # 原文放最后，折叠起来：想看细节时才展开
            parts += ["## 原文", "", "<details>", "<summary>点开看全文</summary>", ""]
            parts += [body.strip(), "", "</details>", ""]
        return "\n".join(parts)

    # ------------------------------------------------------------------ 读取

    def entities(self, category: Optional[str] = None) -> list[Entity]:
        """按**最近更新**倒序列出。"""
        pool = list(self._entities.values())
        if category:
            pool = [e for e in pool if e.category == category]
        return sorted(pool, key=lambda e: e.updated_at, reverse=True)

    def get(self, key: str) -> Optional[Entity]:
        return self._entities.get(key)

    def latest_text(self, key: str) -> str:
        """取某个实体**最新那版**的 Markdown 全文。

        ## 这是"用总结回复而不是甩全文"的落点

        用户原话：「最后联网搜索小说或者大段文字这种信息如果用户不要求
        详细说明就用分析总结好的信息回复」。
        """
        entity = self._entities.get(key)
        if entity is None or not entity.revisions:
            return ""
        rev = entity.revisions[-1]
        try:
            return (self.root / rev.path).read_text(encoding="utf-8")
        except Exception:  # noqa: BLE001
            log.warning("知识条目读不出来：%s", rev.path, exc_info=True)
            return ""

    def search(self, query: str, limit: int = 5) -> list[Entity]:
        """在**索引**里找相关实体（标题/标签/最新分析）。

        ⚠️ 简单子串匹配，不是语义检索。够用：实体数在几百这个量级，
           而且模型拿到的本来就是它自己写过的分析。
        """
        q = str(query or "").strip().lower()
        if not q:
            return []
        out: list[tuple[int, Entity]] = []
        for e in self._entities.values():
            latest = e.latest()
            analysis = latest.analysis if latest else ""
            thoughts = latest.thoughts if latest else ""
            hay = " ".join([e.title, " ".join(e.tags), analysis, thoughts, e.source]).lower()
            if q not in hay:
                continue
            score = 2 if q in e.title.lower() else 1
            out.append((score, e))
        out.sort(key=lambda p: (p[0], p[1].updated_at), reverse=True)
        return [e for _s, e in out[:limit]]

    # ------------------------------------------------------------------ 自己翻档案
    #
    # 🔴 这一节是 2026-11-05 用户明确要求加的：
    #
    # > 「主要是让宠物想找某一个事件的时候要有自己搜索寻找到的功能，
    # >   不用用户说继续下去才一级一级找到相关信息」
    #
    # 也就是说：**它得能自己翻自己的档案**，而不是等用户一句句引导。
    #
    # 三个层次（从粗到细）：
    #
    #     recall()      按关键词找实体（"哪本书里提过红衣？"）
    #     timeline()    某个实体的完整阅读/思考轨迹（"这本书我怎么看下来的"）
    #     revision_at() 定位到具体某一章那次的总结（"第 7 章我当时怎么想的"）

    def recall(self, query: str, *, limit: int = 3) -> list[dict[str, Any]]:
        """按关键词找相关条目，返回**给模型看的**结构化摘要。

        和 `search()` 的区别：这个是"自己翻档案"的入口，要多带上下文
        （最近一版的分析、覆盖到第几章、有多少版）。

        ⚠️ 只返回**标题 + 分析 + 元信息**，不含原文。
           原文可能几万字，直接塞进上下文会把它挤爆 ——
           想看细节应该再调 `read_knowledge`。
        """
        hits = self.search(query, limit=limit)
        out: list[dict[str, Any]] = []
        for e in hits:
            latest = e.latest()
            out.append(
                {
                    "key": e.key,
                    "title": e.title,
                    "category": e.category,
                    "category_label": CATEGORIES.get(e.category, e.category),
                    "tags": list(e.tags),
                    # 覆盖到第几章（书才有意义）
                    "chapter_from": latest.chapter_from if latest else 0,
                    "chapter_to": latest.chapter_to if latest else 0,
                    "versions": len(e.revisions),
                    "updated": time.strftime(
                        "%Y-%m-%d %H:%M", time.localtime(e.updated_at)
                    )
                    if e.updated_at
                    else "",
                    "analysis": (latest.analysis if latest else "")[:400],
                    "thoughts": (latest.thoughts if latest else "")[:300],
                }
            )
        return out

    def timeline(self, key: str) -> list[dict[str, Any]]:
        """某个实体的**全部版本**，按时间顺序。

        用途：用户问「这本书你怎么看下来的」「我的想法变过吗」，
        或者模型自己想回顾"之前想到哪了"。

        ## 为什么这个比"只给最新版"有用

        用户那条「中间章节回看」的问题正是靠它解决：

            v1 第 1~3 章   我以为凶手是同事
            v2 第 4~10 章  发现是红衣 —— **想法变了**
            v3 第 11~20 章 …

        只看 v3 的话，"想法的变化"这件事就丢了，而**那恰恰是最有意思的**。
        """
        entity = self._entities.get(key)
        if entity is None:
            return []
        return [
            {
                "version": r.n,
                "when": time.strftime("%Y-%m-%d %H:%M", time.localtime(r.added_at)),
                "scope": r.scope,
                "chapter_from": r.chapter_from,
                "chapter_to": r.chapter_to,
                "analysis": r.analysis[:400],
                "thoughts": r.thoughts[:300],
            }
            for r in entity.revisions
        ]

    def revision_at(self, key: str, chapter: int) -> Optional[Revision]:
        """找到**覆盖第 `chapter` 章**的那一版。

        用户那句「取两次读取的中间章节回看」就靠它：
        比如 v1 覆盖 1~3 章、v2 覆盖 4~10 章，问第 7 章 -> 落到 v2。

        ⚠️ 找不到精确覆盖时退回**最新版**（至少有个参考），
           而不是返回 None 让它什么都拿不到。
        """
        entity = self._entities.get(key)
        if entity is None or not entity.revisions:
            return None
        if chapter > 0:
            for r in entity.revisions:
                if r.covers(chapter):
                    return r
        return entity.revisions[-1]

    def read_version(self, rev: Revision) -> str:
        """读某一版的 Markdown 全文（分析 + 想法 + 折叠的原文）。"""
        try:
            return (self.root / rev.path).read_text(encoding="utf-8")
        except Exception:  # noqa: BLE001
            log.warning("知识版本读不出来：%s", rev.path, exc_info=True)
            return ""

    def related(self, key: str, limit: int = 3) -> list[Entity]:
        """找**相关**实体（共享标签的）。

        ## 为什么需要它

        用户提到「连锁回想」—— 想到 A 就想到 B。标签是最便宜可靠的桥梁：
        读过两本都市怪谈，它们之间就有关系。

        ⚠️ 不靠语义相似度（那要额外调模型/嵌入），标签已经够了 ——
           标签是模型自己在总结时打的，本身就带语义。
        """
        me = self._entities.get(key)
        if me is None or not me.tags:
            return []
        mine = set(me.tags)
        scored: list[tuple[int, Entity]] = []
        for other in self._entities.values():
            if other.key == key or not other.tags:
                continue
            shared = mine & set(other.tags)
            if shared:
                scored.append((len(shared), other))
        scored.sort(key=lambda p: (p[0], p[1].updated_at), reverse=True)
        return [e for _n, e in scored[:limit]]

    def summary_for_prompt(self, limit: int = 3) -> str:
        """给提示词用的"我最近记了什么"（只有标题 + 分析首句，控制体积）。"""
        recent = self.entities()[:limit]
        if not recent:
            return ""
        lines = []
        for e in recent:
            latest = e.latest()
            if latest is None:
                continue
            first = (latest.analysis or latest.thoughts or "").strip().splitlines()
            brief = first[0][:80] if first else ""
            lines.append(f"· 《{e.title}》{brief}")
        return "\n".join(lines)

    def stats(self) -> dict[str, Any]:
        by_cat: dict[str, int] = {}
        total_rev = 0
        for e in self._entities.values():
            by_cat[e.category] = by_cat.get(e.category, 0) + 1
            total_rev += len(e.revisions)
        return {
            "root": str(self.root),
            "entities": len(self._entities),
            "revisions": total_rev,
            "by_category": by_cat,
        }
