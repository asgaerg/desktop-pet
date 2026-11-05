"""读完/搜完之后的"分析、总结、发散思维"（2026-11-05 用户要求）。

## 用户原话

> 「宠物搜索完之后会对信息进行分析、总结、发散思维，这些都记在里面，
>   比如小说读后的感觉等等」
> 「最后联网搜索小说或者大段文字这种信息如果用户不要求详细说明
>   就用分析总结好的信息回复」

## 两件事

  ① `analyze()`    给一段长文本 -> (分析, 想法)
  ② `summarize_for_reply()`  把长文本压成"能直接拿去回复"的摘要

## 🔴 为什么需要 ②：6000 字的硬限制

`tools.MAX_RESULT_CHARS = 6000` —— 工具结果喂回模型时超过就截断。
一次 `read_book` 读 3 章约 6000~7000 字，正好撞上。

撞上之后模型看到的是**半截内容**，但它不知道被截了 ——
于是会一本正经地评论一本它只读了一半的书。

所以：**读完之后先自己总结一遍**，之后无论是对话还是回看，
用的都是那份总结（几百字，稳在限制内）。

## ⚠️ 成本

每读一次书 = 一次额外的模型调用（总结）。这钱该花：

    · 不总结 -> 模型只看到前 6000 字，而且记忆里存的也是半截
    · 总结   -> 多花一次调用，但记住的是完整内容 + 它的想法

而且只在**长文本**时才总结（见 `should_analyze`），短的不折腾。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

log = logging.getLogger("pet.knowledge_analysis")

#: 超过这个字数才值得单独总结。
#:
#: ⚠️ 门槛设在 1500：短内容（一条搜索结果、一段帖子）直接进上下文就好了，
#:    额外调一次模型纯属浪费钱。
MIN_CHARS_TO_ANALYZE = 1500

#: 喂给总结模型的正文上限。
#:
#: 一本书几十万字，整本喂进去又贵又慢。取**开头 + 结尾**各一万字 ——
#: 开篇定基调（文风、人物），结尾揭示走向，中间通常是展开过程。
#: 这是"读一本书"在有限预算下的合理近似。
MAX_BODY_FOR_ANALYSIS = 20000

ANALYZE_PROMPT = """你在读一段内容，读完后要写下你自己的想法。

## 要求

**分析**（"## 我的分析"下）：
- 这段内容讲了什么，用你自己的话说，不要复述原文
- 关键人物/论点/事件有哪几个
- 300 字以内

**想法**（"## 我的想法"下）：
- 这让你想到什么？可以联想到别的东西
- 你的感受是什么（觉得好看/无聊/有道理/扯淡，都可以说）
- 如果你对它有疑问或期待，也写在这里
- 200 字以内

## 格式（严格遵守）

直接输出两段，每段以标题行开头：

## 我的分析
（内容）

## 我的想法
（内容）

⚠️ 不要写别的标题，不要加"好的，我来分析"这种开场白，直接从
「## 我的分析」开始。
"""


def should_analyze(text: str) -> bool:
    """这段内容够不够长到值得单独总结。"""
    return len(str(text or "")) >= MIN_CHARS_TO_ANALYZE


def _clip_for_analysis(body: str) -> str:
    """一本书几十万字，全文喂进去不现实。取头尾近似"读完了"。

    ## 为什么是头 + 尾

        开头      定基调（文风、人物、时代）
        结尾      揭示走向和结局
        中间      通常是展开过程，信息密度低

    这和"读过一本书的人能聊什么"最接近 —— 记得开头怎么起、后来怎么了，
    中间细节模糊但不影响表达观感。
    """
    text = str(body or "")
    if len(text) <= MAX_BODY_FOR_ANALYSIS:
        return text
    half = MAX_BODY_FOR_ANALYSIS // 2
    head = text[:half]
    tail = text[-half:]
    return (
        f"{head}\n\n"
        f"……（中间省略 {len(text) - MAX_BODY_FOR_ANALYSIS} 字）……\n\n"
        f"{tail}"
    )


def _split_sections(text: str) -> tuple[str, str]:
    """把模型回复拆成 (分析, 想法)。拆不出来就整段当分析。"""
    raw = str(text or "").strip()
    if not raw:
        return "", ""

    import re

    # 找「## 我的想法」的位置，前一半是分析
    m = re.search(r"^##+\s*我的想法\s*$", raw, re.M)
    if m:
        analysis_part = raw[: m.start()]
        thoughts_part = raw[m.end() :]
    else:
        analysis_part = raw
        thoughts_part = ""

    def _clean(chunk: str) -> str:
        # 去掉可能的「## 我的分析」标题行
        chunk = re.sub(r"^##+\s*我的分析\s*$", "", chunk, flags=re.M)
        return chunk.strip()

    return _clean(analysis_part), thoughts_part.strip()


async def analyze(
    body: str,
    *,
    brain: Any,
    title: str = "",
    kind: str = "内容",
) -> tuple[str, str]:
    """让模型读一遍，产出 `(分析, 想法)`。失败返回 `("", "")`。

    ## 为什么用 `brain.stream()` 而不是 `Conversation.ask`

    🔴 2026-11-05 实测踩到：`Brain` **没有 `chat()` 方法** ——
       它的接口只有 `async def stream(req)`。第一版写的是
       `brain.chat(...)`，结果每次归档都失败（而且失败被 catch 吞掉，
       日志里只有一行"总结失败"，归档照常写但分析是空的）。

    ## 为什么不走 `Conversation.ask`

    `ask` 会写历史、发 `pet.say.*`、走完整对话流程 —— 那是**跟用户说话**。
    而这里只是"自己消化一下"，不该出现在聊天记录里，也不该让用户看见
    气泡说"我正在分析"。

    ⚠️ 失败**不能抛**：总结只是加分项，读不到书才是大事。
    """
    text = _clip_for_analysis(body)
    if not text.strip():
        return "", ""

    head = f"你刚读完了{kind}《{title}》。" if title else f"你刚读完一段{kind}。"
    prompt = f"{head}\n\n---\n\n{text}\n\n---\n\n{ANALYZE_PROMPT}"

    try:
        from .brains.base import LLMRequest, Turn

        req = LLMRequest(
            # ⚠️ system 要极简：这里不需要人设/记忆，只要"读并写下想法"。
            #    塞人设反而会让它扮演角色、写出"在跟用户说话"的语气。
            system="你正在整理自己读到的东西。直接输出要求的两段，不要寒暄。",
            history=[],
            user_text=prompt,
            temperature=0.8,
            # 900 够写"分析 300 + 想法 200"+ 一点余量。
            # ⚠️ 给太小会被推理吃掉（这个项目在 50 token 上踩过）
            max_tokens=900,
            timeout_s=60.0,
        )
        collected: list[str] = []
        async for item in brain.stream(req):
            if item.get("kind") == "delta":
                piece = str(item.get("text") or "")
                if piece:
                    collected.append(piece)
        reply = "".join(collected)
    except Exception as exc:  # noqa: BLE001 —— 总结失败不该影响读
        log.warning(
            "总结失败（%s: %s），这条只存原文",
            type(exc).__name__,
            str(exc)[:80],
        )
        return "", ""

    analysis, thoughts = _split_sections(str(reply or ""))
    if not analysis and not thoughts:
        log.info("总结没产出内容（模型可能返回了空）")
        return "", ""
    return analysis, thoughts


def summarize_for_reply(analysis: str, thoughts: str, *, max_chars: int = 400) -> str:
    """把分析和想法压成一句能直接拿去回复用户的话。

    ## 用户要求

    > 「最后联网搜索小说或者大段文字这种信息如果用户不要求详细说明
    >   就用分析总结好的信息回复」

    所以默认回复的是**总结**，不是原文。用户明确要细节时才去读原文。

    ⚠️ 不拼接成"分析：xx 想法：xx"那种标签体 —— 那不像说话。
       取分析的首句 + 想法的首句，用自然的连接词。
    """
    a = str(analysis or "").strip()
    t = str(thoughts or "").strip()

    def _first_sentence(text: str) -> str:
        """取第一句（到句末标点）。"""
        import re

        m = re.search(r"[^。！？\n]*[。！？]", text)
        return (m.group(0) if m else text.split("\n")[0]).strip()

    parts: list[str] = []
    if a:
        parts.append(_first_sentence(a))
    if t:
        parts.append(_first_sentence(t))
    joined = "".join(parts) if parts else ""
    return joined[:max_chars]


def key_for_text(title: str, category: str) -> str:
    """给一段内容算实体 key。

    ⚠️ 书用书名、网页用 URL 哈希（见 `knowledge`）。这里是通用兜底：
       没书名也没 URL 时，用类别 + 标题拼一个。
    """
    from .knowledge import normalize_key

    base = normalize_key(title)
    if base:
        return base
    return f"{category}-{abs(hash(title)) % 10**8:08d}"
