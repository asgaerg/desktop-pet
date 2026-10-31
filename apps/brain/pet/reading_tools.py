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

                chunk = BookChunk(
                    title=title,
                    source="read_book",
                    text="\n".join(f"{n}：{s}" for n, s in samples),
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
            name="read_book",
            description=(
                "去网上读一本书，下载到本地。**你自己决定读什么、读多少**。\n"
                "\n"
                "什么时候用：用户很久没说话、你闲着、想找点事做的时候。\n"
                "也可以主动去读某本书（用户提到过、你感兴趣、或者想换个话题）。\n"
                "\n"
                "怎么用：\n"
                "· `title` 给书名。不用很精确，程序会搜索匹配。\n"
                "· `chapters` 是**这次读几章**。可以是 1，也可以是几十。\n"
                "  想慢慢看就传 1~3；想一口气看很多就传 20~50。\n"
                "  程序会**接着上次的进度往下读**，不会重复读同一章。\n"
                "\n"
                "读到的内容会存成 txt，你可以引用其中片段跟用户聊，\n"
                "但**不要整段复述**——像看完书随口提一句那样。"
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
                            "想慢慢看就少传，想一口气看很多就多传。"
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
