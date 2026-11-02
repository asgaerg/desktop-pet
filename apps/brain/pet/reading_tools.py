"""给宠物一套**自己控制**的阅读工具（2026-10-31 用户要求）。

## 用户原话

> 「我的意思是自己选一本书读，有时候会换着读，有时候会读很多章，
>   比如一次几十章几万字。」

所以阅读不是"程序定时抓一章"，而是**模型自己决定**：

  · 读哪本书（可以随时换）
  · 一次读几章（1 章到几十章都行）
  · 读完要不要说点什么

## 为什么做成工具而不是定时任务

定时任务只能做"抓一章"这种固定动作。而用户要的是"这次读三章，
下次换本书读十章" —— 这是**判断**，只有模型能做。

工具化的好处：
  · 模型按自己的意愿调用，程序不预设节奏
  · 读多少由 `chapters` 参数决定，想读 50 章就传 50
  · 和现有 `web_search` / `web_fetch` 是同一套机制，不需要新协议

## 两个工具

  `read_book`     读一本书的若干章（会追加到同一个文件）
  `list_books`    看已经下载了哪些书、各自读到第几章

## 与自主流程的关系

`gateway._run_reading_forever` 仍然存在，但改成：
空闲时**给模型一次机会**，由它决定要不要读、读什么。
模型不调用工具就是"这次不想读"，程序不强迫。
"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

# 一次最多读多少章。
#
# ⚠️ 上限不是"技术限制"，是**礼貌和可用性**的平衡：
#    · 每章要发一个 HTTP 请求，50 章就是 50 个请求
#    · 用户说"几十章几万字"是合理的，但 500 章会拖很久也会被站点限流
#    · 真要读更多，让模型分多次调用（它自己会判断）
MAX_CHAPTERS_PER_CALL = 50


def build_reading_tools(reader: Any, on_read: Any = None) -> list[Any]:
    """造两个阅读工具。

    `reader` 是 `reading.Reader` 实例。传进来而不是在这里 new，
    是为了让工具和自主循环**共用同一份进度状态**（`_next_chapter_index` 等）。

    `on_read` 是可选的同步回调：每次 `read_book` 成功后会调用
    `on_read(BookChunk)`。网关用它把"读了什么"记下来，
    好在下次自主发言时提到（见 `gateway._reading_via_model`）。
    """
    from .tools import Tool

    async def _search_book(args: dict[str, Any]) -> str:
        query = str(args.get("title") or "").strip()
        if not query:
            return "错误：要给一个书名（title 参数）。"
        try:
            limit = int(args.get("limit") or 8)
        except (TypeError, ValueError):
            limit = 8
        limit = max(1, min(20, limit))

        import asyncio

        try:
            found = await asyncio.to_thread(reader.search_books, query, limit)
        except Exception as exc:  # noqa: BLE001
            log.warning("search_book 失败：%s: %s", type(exc).__name__, exc)
            return f"搜「{query}」时出错了（{type(exc).__name__}）。稍后再试。"

        if not found:
            return (
                f"在现有书源里没搜到「{query}」。\n"
                "可能是：书名不完全对（试试更短的关键词）、这本书这些站没有、"
                "或者书源临时连不上。\n"
                "⚠️ 别直接说「这本书不存在」—— 你只知道**这几个免费书源里没有**。"
            )

        lines = [f"搜到 {len(found)} 本（来自免费书源）："]
        for i, b in enumerate(found, 1):
            ch = b.get("chapters") or 0
            ch_txt = f"{ch} 章" if ch else "章数未知"
            lines.append(f"  {i}. 《{b['title']}》 —— {ch_txt}（{b['source']}）")
        lines.append("")
        lines.append(
            "想读哪本就调 read_book（title 用书名，chapters 给这次读几章）。"
            "只是想聊聊的话，看这个列表就够了。"
        )
        return "\n".join(lines)

    async def _read_book(args: dict[str, Any]) -> str:
        query = str(args.get("title") or "").strip()
        if not query:
            return "错误：要给一个书名（title 参数）。"

        try:
            want = int(args.get("chapters") or 1)
        except (TypeError, ValueError):
            want = 1
        want = max(1, min(MAX_CHAPTERS_PER_CALL, want))

        # ⚠️ 抓取是同步 IO，必须丢到线程里 ——
        #    50 章可能要几分钟，塞进事件循环会卡死语音和聊天。
        import asyncio

        try:
            result = await asyncio.to_thread(
                reader.read_many, query, want
            )
        except Exception as exc:  # noqa: BLE001 —— 工具不能把整个对话搞崩
            log.warning("read_book 失败：%s: %s", type(exc).__name__, exc)
            return f"读《{query}》失败了（{type(exc).__name__}）。换个书源或稍后再试。"

        if result is None:
            return (
                f"没能读到《{query}》—— 所有书源都没给出合格正文。"
                "可能是书源失效，或者这本书本身没有内容。可以换一本试试。"
            )

        title, got, total_chars, path, samples = result

        # 通知网关"读了什么"（网关会把它挂成下次自主发言的话题）
        if on_read is not None and got > 0:
            try:
                from .reading import BookChunk

                # 🔴 要给**真正的正文**，不是摘要片段（2026-11-02 修）
                #
                # ## 原来的 bug
                #
                # 第一版写的是：
                #
                #     text="\n".join(f"{n}：{s}" for n, s in samples)
                #
                # 而 `samples` 是 `read_many` 里每章取 100 字的**摘要**
                # （`body[:100]`），用于给模型"感受一下内容"。
                #
                # 后果：这条 chunk 被 `_remember_reading` 写进记忆后长这样：
                #
                #     （我读了书《诡秘之主》——我自己找的）
                #     第三十三章 "开关"（第一更求推荐票）：老尼尔话音刚落…
                #     第三十四章 预付的报酬（第二更求推荐票）：没有窗户的地下室内…
                #
                # 两段各 100 字，第二段停在一句话中间。用户翻记忆时问：
                # 「最新一条是没说完话吗」—— 是的，因为**它本来就只有 100 字**。
                #
                # 正文其实已经完整落在 `path` 里了，读出来就行。
                body = ""
                try:
                    from pathlib import Path as _P

                    raw = _P(path).read_text(encoding="utf-8", errors="replace")
                    # 去掉文件头（书名/来源/开始时间）和章节标记行，
                    # 只留正文 —— 记忆里不需要"下载于 2026-11-02"这种噪音。
                    keep: list[str] = []
                    for line in raw.splitlines():
                        s = line.strip()
                        if not s:
                            continue
                        if s.startswith("<!-- chapter:"):
                            continue
                        if s.startswith("──") or s.startswith("《"):
                            continue
                        if s.startswith("来源：") or s.startswith("开始：") or s.startswith("下载于"):
                            continue
                        keep.append(s)
                    body = "\n".join(keep)
                except Exception:  # noqa: BLE001 —— 读不到就退回摘要
                    log.warning("读正文文件失败，退回摘要片段", exc_info=True)
                    body = "\n".join(f"{n}：{s}" for n, s in samples)

                chunk = BookChunk(
                    title=title,
                    source="read_book",
                    text=body,
                    kind="book",
                    saved_path=path,
                    chars=total_chars,
                )
                on_read(chunk)
            except Exception:  # noqa: BLE001 —— 回调失败不影响工具返回
                log.warning("阅读回调失败（已忽略）", exc_info=True)

        lines = [
            f"读了《{title}》{got} 章，共 {total_chars} 字。",
            f"文件：{path}",
        ]
        if samples:
            lines.append("")
            lines.append("这几章的片段（用来感受内容，不是让你复述）：")
            for name, snip in samples[:3]:
                lines.append(f"  · {name}：{snip}")
        return "\n".join(lines)

    async def _list_books(args: dict[str, Any]) -> str:
        books = reader.list_downloaded()
        if not books:
            return "本地还没有下载过任何书。"
        lines = [f"本地有 {len(books)} 本书："]
        for b in books:
            lines.append(
                f"  · 《{b['title']}》 读到第 {b['chapter']} 章"
                f"（{b['size_kb']:.0f} KB，最后更新 {b['updated']}）"
            )
        return "\n".join(lines)

    return [
        Tool(
            name="search_book",
            description=(
                "在**小说书源**里搜一本书（只搜，不下载）。\n"
                "\n"
                "## 什么时候用（重点）\n"
                "\n"
                "用户提到某本**小说/网文**、想知道有没有、好不好看、能不能看时，"
                "用这个 —— **不要用 `web_search`**。\n"
                "\n"
                "`web_search` 搜的是网页摘要（百科、书评、盗版站列表），"
                "它**不会把书读进来**。用户说「你去搜搜这本书」通常不是要看书评，"
                "而是要你**真的去看内容**。\n"
                "\n"
                "## 什么时候不用\n"
                "\n"
                "· 用户问的是**这本书的评价/作者/出版信息** -> 用 `web_search`\n"
                "· 用户问的是**非小说**（技术文档、新闻、论文）-> 用 `web_search`\n"
                "\n"
                "## 返回什么\n"
                "\n"
                "书名列表 + 每本多少章 + 来自哪个源。只搜不下载，很轻。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "书名或关键词，不用很精确（会搜索匹配）",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "最多返回几本，默认 8",
                    },
                },
                "required": ["title"],
            },
            handler=_search_book,
        ),
        Tool(
            name="read_book",
            description=(
                "去网上读一本**小说**，把正文下载到本地。\n"
                "\n"
                "## 什么时候用（重点）\n"
                "\n"
                "**用户让你看书 / 提到某本小说 / 让你去搜某本书的内容时，用这个。**\n"
                "对话里用户说「你去搜搜 XX」「XX 这本书好看」「你读过 XX 吗」——\n"
                "他通常是要你**真的去看**，而不是搜一段网页摘要回来。\n"
                "\n"
                "⚠️ **不要用 `web_search` 代替它**。`web_search` 搜到的是书评、"
                "百科、盗版站列表 —— 你依然没看过这本书，聊起来只能空泛地附和。\n"
                "而 `read_book` 会真的把章节正文下载下来，你能聊具体情节。\n"
                "\n"
                "用户很久没说话、你自己闲着的时候也可以读 —— 那是你自己的事。\n"
                "\n"
                "## 怎么用\n"
                "\n"
                "· `title` 给书名，不用很精确（程序会搜索匹配）。\n"
                "· `chapters` 是**这次读几章**。\n"
                "  用户在跟你聊 -> 读 1~3 章就够聊了，别让他干等。\n"
                "  你自己闲着 -> 想读多少读多少，几十章也行。\n"
                "· 程序会**接着上次的进度往下读**，不会重复读同一章。\n"
                "\n"
                "## ⚠️ 它很慢\n"
                "\n"
                "每章一个 HTTP 请求。读 5 章大概 10 秒，读 50 章要几分钟。\n"
                "对话中读之前**先跟用户说一句**（「我去看看」），别让他以为你卡住了。\n"
                "\n"
                "读到的内容会存成 txt。聊的时候**挑一个具体的点**说，"
                "不要整段复述原文。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "书名，不用很精确（会搜索匹配）",
                    },
                    "chapters": {
                        "type": "integer",
                        "description": (
                            f"这次读几章，默认 1，最多 {MAX_CHAPTERS_PER_CALL}。"
                            "对话里读 1~3 章就够；自己闲着可以读几十章。"
                        ),
                    },
                },
                "required": ["title"],
            },
            handler=_read_book,
        ),
        Tool(
            name="list_books",
            description=(
                "看本地已经下载了哪些书、每本读到第几章。\n"
                "想看自己读过什么、或者接着读某本时先确认进度，就用它。"
            ),
            parameters={"type": "object", "properties": {}},
            handler=_list_books,
        ),
    ]
