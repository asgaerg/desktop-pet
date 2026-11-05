"""宠物自己翻知识档案的工具（2026-11-05 用户要求）。

## 用户原话

> 「主要是让宠物想找某一个事件的时候要有自己搜索寻找到的功能，
>   不用用户说继续下去才一级一级找到相关信息」

## 为什么单独一个模块

`reading_tools` 里那几个工具是"读书"（联网下载章节），
而这里是"翻自己的档案"（本地检索）。两件事：

    读书    会联网、慢、有成本
    翻档案  纯本地、快、免费

混在一起的话，模型想"回忆一下"时会以为要联网，于是不去用 ——
实测那种"工具能调用但模型不敢用"的情况很常见（描述里写着"联网"就会犹豫）。

## 三个工具，对应三种"找"的粒度

    search_knowledge   我不知道在哪 -> 按关键词找
    recall_knowledge   我知道是哪本/哪条 -> 看它的完整轨迹
    read_knowledge     我知道是哪一版 -> 读它的全文

⚠️ 分三级而不是一个大工具：模型每次只需要一级，
   全塞进一个工具会让它一次拿太多（上下文被挤爆）。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

log = logging.getLogger("pet.knowledge_tools")


def build_tools(store: Any) -> list[Any]:
    """造出知识检索工具。`store` 是 `KnowledgeStore` 实例。

    ⚠️ 传实例而不是让工具自己 new：知识库是**有状态的**（内存里存着索引），
       每个工具各建一个会重复读文件、而且互相看不到对方的写入。
    """
    from .tools import Tool

    async def _search_knowledge(args: dict[str, Any]) -> str:
        """按关键词找自己归档过的内容。"""
        query = str(args.get("query") or "").strip()
        if not query:
            return "错误：要给一个查询词（query 参数）。"

        try:
            limit = int(args.get("limit") or 3)
        except (TypeError, ValueError):
            limit = 3
        limit = max(1, min(8, limit))

        hits = store.recall(query, limit=limit)
        if not hits:
            total = store.stats().get("entities", 0)
            if total == 0:
                return (
                    "你的知识档案还是空的 —— 还没读过书、逛过帖子或者搜过资料。"
                    "先去读点什么，之后这里就会有内容。"
                )
            return (
                f"你没找到跟「{query}」有关的东西。"
                f"（档案里现有 {total} 条记录）\n"
                "换个说法试试，比如用人名、书名或者事件里的关键词。"
            )

        lines = [f"找到 {len(hits)} 条跟「{query}」有关的：", ""]
        for i, h in enumerate(hits, 1):
            head = f"{i}. 《{h['title']}》（{h['category_label']}"
            if h.get("chapter_from"):
                head += f" · 读到第 {h['chapter_to']} 章"
            if h.get("versions", 1) > 1:
                head += f" · 共 {h['versions']} 次记录"
            head += "）"
            lines.append(head)
            if h.get("analysis"):
                lines.append(f"   我当时想的：{h['analysis']}")
            if h.get("thoughts"):
                lines.append(f"   我的感受：{h['thoughts']}")
            lines.append("")
        lines.append("想看某一条的完整经过，用 recall_knowledge（给它标题）。")
        return "\n".join(lines)

    async def _recall_knowledge(args: dict[str, Any]) -> str:
        """看某一条的完整轨迹（每次读/搜各写了什么）。"""
        title = str(args.get("title") or "").strip()
        if not title:
            return "错误：要给一个标题（title 参数）。"

        # 先按 key 精确找，再按标题包含匹配
        from .knowledge import normalize_key

        entity = store.get(normalize_key(title))
        if entity is None:
            subs = [e for e in store.entities() if title in e.title]
            if len(subs) == 1:
                entity = subs[0]
            elif len(subs) > 1:
                names = "、".join(f"《{e.title}》" for e in subs[:5])
                return f"「{title}」匹配到多本：{names}。说得再具体一点。"
            else:
                return (
                    f"档案里没有《{title}》。"
                    "可以先 search_knowledge 找找看叫什么名字。"
                )

        timeline = store.timeline(entity.key)
        if not timeline:
            return f"《{entity.title}》还没有任何记录。"

        lines = [f"《{entity.title}》—— 我一共记录了 {len(timeline)} 次：", ""]
        for t in timeline:
            when = t.get("when", "")
            scope = t.get("scope") or ""
            head = f"· 第 {t['version']} 次（{when}）"
            if scope:
                head += f" —— {scope}"
            lines.append(head)
            if t.get("analysis"):
                lines.append(f"    想法：{t['analysis']}")
            if t.get("thoughts"):
                lines.append(f"    感受：{t['thoughts']}")
            lines.append("")

        # 顺带给相关条目，支持"连锁回想"
        rel = store.related(entity.key, limit=3)
        if rel:
            lines.append(
                "另外这些跟它有点像："
                + "、".join(f"《{e.title}》" for e in rel)
            )
        return "\n".join(lines)

    async def _read_knowledge(args: dict[str, Any]) -> str:
        """读某一版的完整内容（分析 + 想法 + 原文）。"""
        title = str(args.get("title") or "").strip()
        if not title:
            return "错误：要给一个标题（title 参数）。"

        from .knowledge import normalize_key

        entity = store.get(normalize_key(title))
        if entity is None:
            subs = [e for e in store.entities() if title in e.title]
            entity = subs[0] if len(subs) == 1 else None
        if entity is None:
            return f"档案里没有《{title}》。先用 search_knowledge 找找。"

        try:
            chapter = int(args.get("chapter") or 0)
        except (TypeError, ValueError):
            chapter = 0

        rev = store.revision_at(entity.key, chapter)
        if rev is None:
            return f"《{entity.title}》还没有可读的记录。"

        text = store.read_version(rev)
        if not text:
            return f"《{entity.title}》第 {rev.n} 次的文件读不出来（可能被删了）。"

        # 控制体积：正文可能很长，截断并提示
        limit = 6000
        if len(text) > limit:
            text = text[:limit] + f"\n\n……（还有 {len(text) - limit} 字，需要的话再说）"
        note = ""
        if chapter:
            note = f"（第 {chapter} 章那次的记录：第 {rev.n} 次，{rev.scope}）\n\n"
        return note + text

    return [
        Tool(
            name="search_knowledge",
            description=(
                "在自己**归档过的**内容里找东西（本地，不联网，很快）。\n"
                "\n"
                "## 什么时候用\n"
                "\n"
                "想知道「我以前是不是看过/搜过跟这个有关的」时就查它。\n"
                "比如用户提到一个话题，你隐约记得读过相关的，但想不起是哪本 ——\n"
                "**先查这里，不要直接说「我忘了」**，也不要问用户「是哪本书」。\n"
                "\n"
                "## 查什么\n"
                "\n"
                "· 人名、书名、事件名、关键物件（「红衣」「吴献」「量子纠缠」）\n"
                "· 一两句描述也行（「那个讲暴雨夜的失踪案」）\n"
                "\n"
                "## 返回什么\n"
                "\n"
                "匹配到的条目 + 你当时写的分析和感受。不含原文 ——\n"
                "想看细节再用 recall_knowledge / read_knowledge。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "关键词"},
                    "limit": {
                        "type": "integer",
                        "description": "最多返回几条（默认 3，上限 8）",
                    },
                },
                "required": ["query"],
            },
            handler=_search_knowledge,
        ),
        Tool(
            name="recall_knowledge",
            description=(
                "看某一条档案的**完整经过**：你每次读/搜它时分别写了什么。\n"
                "\n"
                "## 为什么它比 search 有用\n"
                "\n"
                "如果你读过同一本书好几次，这个会列出**每一次**的分析和感受 ——\n"
                "于是你能看出自己的想法怎么变的（「我一开始以为是 A，后来发现是 B」）。\n"
                "那种变化本身就值得说，不要只说最新那次。\n"
                "\n"
                "## 什么时候用\n"
                "\n"
                "· 用户问「这本书你怎么看」「读到哪里了」\n"
                "· 用户问「你之前不是觉得…」（想确认你的旧想法）\n"
                "· 你自己想回顾「上次想到哪儿了」再接着聊\n"
                "\n"
                "## 附加\n"
                "\n"
                "返回末尾会带上**相关的其它条目**（标签相同的）——\n"
                "可以用来做联想，但不要硬扯。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "标题/书名（可简称）"}
                },
                "required": ["title"],
            },
            handler=_recall_knowledge,
        ),
        Tool(
            name="read_knowledge",
            description=(
                "读某一版档案的**完整全文**（分析 + 想法 + 原文）。\n"
                "\n"
                "## 什么时候用\n"
                "\n"
                "只在**用户要细节**的时候用：\n"
                "\n"
                "· 「详细说说」「具体第几章怎么回事」\n"
                "· 「原话是怎么写的」「把那段念给我听」\n"
                "\n"
                "⚠️ **不要习惯性调用它** —— 返回的可能几千字，会把对话挤掉。\n"
                "默认用 summary 就够了（用户没说要细节，就说明他只要大概）。\n"
                "\n"
                "## chapter 参数\n"
                "\n"
                "给章号时会自动找**覆盖那一章**的那一次记录，\n"
                "而不是永远给最新那次。所以「第 7 章那次我怎么想的」也能答。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "标题/书名（可简称）"},
                    "chapter": {
                        "type": "integer",
                        "description": "想回看的章号（可选）。不传 = 最新那次",
                    },
                },
                "required": ["title"],
            },
            handler=_read_knowledge,
        ),
    ]
