"""把「在线找书」做成宠物能用的工具（起点 / 番茄）。

## 用户要求（2026-11-10）

> 「现在我需要加一个新模块，能够帮我搜索分析小说的功能，帮我找到我喜欢的小说」
> 「起点的、番茄的」
> 「用起点官方网站，番茄官方网站，这样搜到书名可以免费看几章」
> 「搜索的频率降低」

## 和已有的 `reading_tools` 什么关系

`reading_tools`（`search_book` / `read_book` / `list_books` / `review_book`）
是**盗版书源 + 本地缓存**那套：搜到就下到 `pet-books/`，能整本读。

这套是**正版站**：只做搜索、看简介、看目录、试读几章。
不下整本、不绕过付费。

两者**互补**，不是替代：
· 想真的读完一本 -> 旧工具（但书源不稳定）
· 想找书、看简介判断值不值得读 -> 新工具（正版、稳定、有简介标签）

## 为什么放在新文件里

`reading_tools.py` 已经 456 行，而且它的三个工具都围绕
"下载到本地"这个模型。混进来会让"下线哪本书"的语义乱掉。

工具描述里也写清了区别，模型不会选错。
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from . import books_online as bo
from .tools import MAX_RESULT_CHARS, Tool

log = logging.getLogger(__name__)

#: 一次最多返回几本书（太多会占满上下文，而且用户看不完）
MAX_HITS = 6

#: 试读最多读几章
MAX_PREVIEW = 3


def _fmt_book(h: bo.BookHit, i: int) -> str:
    """一本书的展示行。"""
    bits = [f"{i}. 《{h.title}》  [{h.site}]"]
    meta: list[str] = []
    if h.author:
        meta.append(h.author)
    if h.status:
        meta.append(h.status)
    if h.words:
        meta.append(f"{h.words / 10000:.1f}万字")
    if h.tags:
        meta.append("、".join(h.tags[:4]))
    if meta:
        bits.append("   " + " · ".join(meta))
    if h.intro:
        bits.append(f"   简介：{h.intro[:120]}")
    bits.append(f"   链接：{h.url}")
    return "\n".join(bits)


def build_tools() -> list[Tool]:
    """构造两个工具：`find_novel` 和 `preview_novel`。"""

    async def _find_novel(args: dict[str, Any]) -> str:
        keyword = str(args.get("keyword") or "").strip()
        site = str(args.get("site") or "both").strip()
        try:
            limit = int(args.get("limit") or MAX_HITS)
        except (TypeError, ValueError):
            limit = MAX_HITS
        limit = max(1, min(limit, MAX_HITS))

        # 🔴 番茄没有真搜索（见 `books_online.fanqie_search` 的说明）。
        #    所以：
        #      · 没给关键词 -> 列番茄热门（这是它能提供的）
        #      · 给了关键词 -> 只在能看到的书里筛，同时建议用起点搜
        if not keyword:
            hot = await bo.fanqie_hot(limit)
            if not hot:
                return "番茄首页暂时读不到，稍后再试。"
            lines = [f"番茄现在首页/榜上的书（{len(hot)} 本）：", ""]
            for i, h in enumerate(hot, 1):
                lines.append(_fmt_book(h, i))
                lines.append("")
            lines.append("要判断哪本好看，用 `preview_novel` 看简介和开头。")
            return "\n".join(lines)[:MAX_RESULT_CHARS]

        hits = await bo.search(keyword, site=site, limit=limit)

        if not hits:
            return (
                f"没搜到「{keyword}」。\n"
                "\n"
                "## 为什么\n"
                "\n"
                "**番茄网页端没有搜索功能**（搜索只在它 APP 里），"
                "我只能从番茄首页和排行榜的书里按书名筛。\n"
                "关键词不在那些书里就搜不到 —— 这是番茄的限制，不是出错。\n"
                "\n"
                "## 你有几个选择\n"
                "\n"
                "1. 换起点搜（`site='起点'`）—— 起点有真正的搜索\n"
                "2. 告诉我准确书名，我直接开那本书看简介和目录\n"
                "3. 想看番茄有什么热门的，不带关键词搜（`keyword` 留空）\n"
                "\n"
                "⚠️ 如实告诉用户这个限制，**绝对不要编造书名**。"
            )

        lines = [
            f"搜「{keyword}」找到 {len(hits)} 本：",
            "",
        ]
        for i, h in enumerate(hits, 1):
            lines.append(_fmt_book(h, i))
            lines.append("")
        # 番茄那种"从首页筛"的结果要提醒
        if all(h.site == "番茄" for h in hits):
            lines.append(
                "⚠️ 这些是从**番茄首页/排行榜**的书里按书名筛出来的"
                "（番茄网页端没有真搜索）。想看更多得换起点。"
            )
        lines.append("想看哪本的简介/目录/开头几章，用 `preview_novel`。")
        out = "\n".join(lines)
        return out[:MAX_RESULT_CHARS]

    async def _preview_novel(args: dict[str, Any]) -> str:
        title = str(args.get("title") or "").strip()
        site = str(args.get("site") or "番茄").strip()
        if not title:
            return "错误：没有给书名。请用 title 参数传入书名。"

        # 先搜一把定位 book_id
        hits = await bo.search(title, site=site, limit=3)
        if not hits:
            return f"没找到《{title}》。先用 `find_novel` 搜一下拿到准确书名。"
        # 优先书名完全相等的，否则取第一个
        hit = next((h for h in hits if h.title == title), hits[0])

        try:
            book, chapters = await bo.open_book(hit.site, hit.book_id)
        except Exception as exc:  # noqa: BLE001
            return f"打开《{hit.title}》失败：{type(exc).__name__}: {exc}"

        lines = [
            f"《{book.title}》",
            f"站点：{book.site}"
            + (f" · {book.status}" if book.status else "")
            + (f" · {book.words / 10000:.1f}万字" if book.words else ""),
        ]
        if book.tags:
            lines.append("标签：" + "、".join(book.tags))
        if book.intro:
            lines.append("")
            lines.append("简介：" + book.intro[:300])
        lines.append("")
        lines.append(f"目录：共 {len(chapters)} 章")
        # 列前几章标题（让模型知道质量/文风）
        for c in chapters[:5]:
            lines.append(f"  {c.title}")
        if len(chapters) > 5:
            lines.append(f"  ...（还有 {len(chapters) - 5} 章）")
        lines.append("")

        # 试读前几章
        lines.append(f"── 试读前 {MAX_PREVIEW} 章 ──")
        lines.append("")
        got = 0
        for c in chapters[:MAX_PREVIEW]:
            text = await bo.read_chapter(book.site, c.url)
            if not text:
                lines.append(f"（{c.title} 读不到 —— 可能是付费章，或需要登录）")
                continue
            lines.append(f"【{c.title}】（{len(text)} 字）")
            lines.append(text[:1500])
            lines.append("")
            got += 1

        if got == 0:
            lines.append(
                "⚠️ 一章正文都没读到。可能原因：\n"
                "· 这本书开头就是付费章\n"
                "· 站点要求登录（设置 → 联网 → 浏览器兜底要开着）\n"
                "如实告诉用户读不到，**不要编造情节**。"
            )
        else:
            lines.append(
                "⚠️ 正文可能有缺字（站点用自定义字体反爬，部分汉字取不到）。"
                "分析文风/题材够用，**不要把它当精确原文引用**。"
            )

        out = "\n".join(lines)
        return out[:MAX_RESULT_CHARS]

    return [
        Tool(
            name="find_novel",
            description=(
                "在**起点 / 番茄官方网站**搜小说，帮用户找到想看的书。\n"
                "\n"
                "## 🔴 必须真的调用（2026-11-10 实测的坑）\n"
                "\n"
                "用户说「帮我找本书」「番茄上有什么好看的」时，**必须真的调用这个工具**。\n"
                "\n"
                "实测踩过：用户说「帮我在番茄看看有什么好看的小说」，\n"
                "模型**没调工具**，直接凭记忆编了《十日终焉》《夜无疆》这些书名 ——\n"
                "整段回复只花了 3 秒，全是编的。\n"
                "\n"
                "**从记忆里报书名 = 撒谎**。榜单每周都在变，你不查就不知道现在有什么。\n"
                "哪怕你觉得自己知道某本书在榜上，也要先查再说。\n"
                "\n"
                "## 什么时候用\n"
                "\n"
                "· 用户说「帮我找本书」「有没有讲XX的小说」「起点/番茄上有什么好看的」\n"
                "· 用户想知道某本书**在不在**、字数多少、完结没、什么标签\n"
                "· 用户让你推荐小说\n"
                "\n"
                "## 和 `search_book` 的区别（重要）\n"
                "\n"
                "· `find_novel`（这个）—— **正版站**（起点/番茄）。有简介、标签、字数、状态。\n"
                "  不下整本，只能试读几章。**推荐用这个**。\n"
                "· `search_book` —— 盗版书源，能下整本但**没有简介和标签**，书源也常挂。\n"
                "\n"
                "用户问「有什么好看的」「这本书怎么样」时用这个 —— 因为判断好不好看\n"
                "需要简介和标签，而 `search_book` 给不了。\n"
                "\n"
                "## 注意（重要，别踩）\n"
                "\n"
                "· **番茄网页端没有搜索功能**（它的搜索只在 APP 里）。\n"
                "  番茄只能从首页/排行榜里按书名筛，关键词不在里面就搜不到。\n"
                "  **要按关键词真搜，用 `site='起点'`。**\n"
                "· 用户说「推荐点好看的」时，**不要给 keyword**（留空）——\n"
                "  那会列出番茄首页/榜上的热门书，这才是番茄能提供的。\n"
                "· 起点要过浏览器挑战，慢（十几秒），一次别搜太多次。\n"
                "· **一次只搜 1~2 个关键词**，搜到了就停下来分析，不要连着换词试探。\n"
                "· 搜不到就如实说，**绝对不要编造书名**。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "keyword": {
                        "type": "string",
                        "description": (
                            "书名或题材关键词，2~6 个字最好。"
                            "**留空 = 列番茄首页/榜上的热门书**（番茄没有真搜索，"
                            "留空反而最有用来做推荐）"
                        ),
                    },
                    "site": {
                        "type": "string",
                        "enum": ["both", "番茄", "起点"],
                        "description": (
                            "搜哪个站。**要按关键词真搜 -> 用「起点」**；"
                            "番茄只能从首页/榜里筛。默认 both"
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "description": f"最多几本，默认 {MAX_HITS}，上限 {MAX_HITS}",
                    },
                },
                "required": [],
            },
            handler=_find_novel,
        ),
        Tool(
            name="preview_novel",
            description=(
                "看一本小说的**简介 + 目录 + 开头几章**（起点的 / 番茄的）。\n"
                "\n"
                "## 什么时候用\n"
                "\n"
                "· 用户想知道某本书讲什么、值不值得看\n"
                "· 用户让你「分析一下这本书」「看看合不合我口味」\n"
                "· 用户让你推荐书，你需要先看几本的内容再挑\n"
                "\n"
                "## 返回\n"
                "\n"
                "书名、站点、状态、字数、标签、简介、目录前几章、**试读前 3 章正文**。\n"
                "\n"
                "## 重要\n"
                "\n"
                "· 只能读**免费试读章**。付费章节服务端就不返回，读不到是正常的。\n"
                "· 正文**可能有缺字**（站点用自定义字体反爬）。分析题材文风够用，\n"
                "  但**不要当精确原文引用**。\n"
                "· 读不到就如实告诉用户，**绝对不要编造情节**。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "书名（用 find_novel 搜到的准确书名最好）",
                    },
                    "site": {
                        "type": "string",
                        "enum": ["番茄", "起点"],
                        "description": "哪个站，默认番茄",
                    },
                },
                "required": ["title"],
            },
            handler=_preview_novel,
        ),
    ]
