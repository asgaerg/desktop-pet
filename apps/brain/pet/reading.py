"""网上阅读：自主找书/看帖子，读完跟用户提一句（2026-10-31）。

## 用户原话

> 「用户长时间不和宠物聊天，宠物会自行上网搜索看小说阅读，包括看小说，
>   看出版书，看小黑盒帖子上的杂谈趣事等等，看的小说和出版书要下载到
>   本地指定位置，下载好之后看，书源下载的书没文字或错误就换源下载看。」

四条硬需求：

1. **空闲触发**（不是定时）—— 用户不理它才去读
2. **书要落到本地**，落盘之后才"读"
3. **书源是外部配置**（用户说「我来提供书源」）
4. **抓到没文字/乱码就换源重试**

## 三个关键设计决定

### [1] 正文校验不能只看"有没有内容"，要看"像不像正文"

实测踩到的坑：用 `#content` 选择器直接取，抓到的是**导航栏 + 推荐书单**
（那个站每页都有 `#content`）。日志会显示"读到了东西"，
实际内容是"首页 | 玄幻 | 武侠 | ..."，宠物会拿这个去聊天。

所以校验用三个可量化指标：

    中文字符占比 >= 0.5     排除英文页/乱码
    标点密度 >= 0.5%        正文有逗号句号，列表没有
    链接文字占比 <= 30%     导航块全是 <a>，正文几乎没有

**标点密度是唯一能区分导航栏和正文的指标** —— 两者中文字数都很多，
只有正文才有逗号句号。

### [2] 换源是逐源尝试，不是重试同一个源

一个源对某本书失效（没正文/404/反爬）时，重试同一个源只会一直失败。
所以配置里 `sources` 是列表，按顺序试，第一个通过校验的就用它。

### [3] 社区帖子不下载

小黑盒/B站帖子读完就没了，没必要落盘。走现成的 `web_search` + `web_fetch`，
不引入新的抓取逻辑 —— 少一套代码就少一类 bug。

## 内容合规

书源由用户提供。本模块**不做任何站点发现**，只按
`config/book-sources.yaml` 里写明的源工作。

实测教训（2026-10-31）：某些免费小说站的推荐位混有成人内容。
如果宠物把那些念出来会很难堪。所以正文里出现明显越界词汇时放弃这本。
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from . import paths

log = logging.getLogger(__name__)

# 搜索结果里常见的"操作按钮"文本，不是书名。
#
# ⚠️ 实测（2026-10-31）：思兔阅读的每张卡片里既有书名链接，也有一个
# 文字为「阅读」的同 URL 链接。不过滤的话搜索结果里一半是重复的「阅读」，
# 选第一本时可能选中它 —— 书名就变成「阅读」了。
_ACTION_WORDS = frozenset(
    {"阅读", "查看", "详情", "免费阅读", "立即阅读", "开始阅读", "更多"}
)

# 🔴 曾经有过 `_UNFIT_WORDS` 内容黑名单，**已删除**（2026-10-31 用户决定）
#
# 用户原话：「不用写黑名单吧，就直接下载就好了，我自己看小说内容判断」
#
# 为什么删：
#   · 它反复误杀正常小说。第一版用单字（淫/裸），《诡秘之主》因为
#     「裸露着齿轮和轴承」被拦；改成词组后「他赤裸着上身站在雨里」
#     这种正常描写又会被拦。
#   · 中文里几乎没法用词表可靠区分"正常描写"和"越界内容" ——
#     同一个词在不同语境下意思完全不同。
#   · 而且**误杀的代价是用户看到"这功能坏了"**，比漏掉一本糟糕得多。
#
# 现在的分工：程序只负责**把书下载到本地**，内容好坏由用户自己看。
# 这既符合用户的要求，也避免了"程序替用户做价值判断"这种不可靠的事。



@dataclass(slots=True)
class SourceSpec:
    """一个书源的解析规则。"""

    name: str
    base: str
    enabled: bool = True
    encoding: str = ""

    search_method: str = "get"
    search_path: str = ""
    keyword_field: str = "q"
    search_extra: dict[str, str] = field(default_factory=dict)
    book_link: str = ""

    toc_url: str = ""
    chapter_link: str = ""

    selectors: list[str] = field(default_factory=list)
    min_chars: int = 500
    # 正文清洗正则（在取完文本**之前**作用于 HTML）。
    #
    # 为什么需要：实测某些站在正文里插了推广段
    # （`<p class="readinline">…</p>`），不清掉会跟着正文一起被"读"出来，
    # 宠物可能把广告词当情节复述给用户。
    drop_patterns: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SourceSpec":
        """从 YAML 的一条记录造出来。

        所有字段都走 `or 默认`：用户手写的配置一定有缺字段、有 None、
        有类型写错。能手写的东西就一定会写错，所以宁可兜底也不要抛 ——
        抛了就整个阅读功能没了。
        """
        s = d.get("search") or {}
        t = d.get("toc") or {}
        c = d.get("content") or {}
        return cls(
            name=str(d.get("name") or "未命名源"),
            base=str(d.get("base") or "").rstrip("/") + "/",
            enabled=bool(d.get("enabled", True)),
            encoding=str(d.get("encoding") or ""),
            search_method=str(s.get("method") or "get").lower(),
            search_path=str(s.get("path") or ""),
            keyword_field=str(s.get("keyword_field") or "q"),
            search_extra={str(k): str(v) for k, v in (s.get("extra") or {}).items()},
            book_link=str(s.get("book_link") or ""),
            toc_url=str(t.get("url") or ""),
            chapter_link=str(t.get("chapter_link") or ""),
            selectors=[str(x) for x in (c.get("selectors") or [])],
            min_chars=int(c.get("min_chars") or 500),
            drop_patterns=[str(x) for x in (c.get("drop_patterns") or [])],
        )


@dataclass(slots=True)
class ReadingConfig:
    idle_after_s: float = 1800.0
    chunk_chars: int = 1500
    same_book_cooldown_s: float = 7200.0
    max_chapters_per_session: int = 1
    min_body_chars: int = 500
    max_source_retries: int = 3
    community_enabled: bool = True
    community_sites: list[dict[str, str]] = field(default_factory=list)
    community_topics: list[str] = field(default_factory=list)
    community_share: bool = True

    @classmethod
    def from_config(cls, cfg: Any) -> "ReadingConfig":
        """读 `config/book-sources.yaml` 里的 `reading:` 和 `community:` 两段。

        ## 🔴 之前这里有个静默失效的 bug（2026-10-31 调试时抓到）

        原实现：

            rd = dict(cfg.get("reading") or {}) if hasattr(cfg, "get") else {}

        而 `Config` 对象**没有 `.get` 方法**（它只有 `.root` / `.providers`
        这些属性）。于是 `hasattr(cfg, "get")` 为 False，整段配置
        **被静默跳过**，全部走 dataclass 默认值。

        症状：改了 yaml 里的 `idle_after_s: 120`，日志还是「空闲 1800 秒」，
        而且**没有任何报错** —— 典型的"配置是假开关"。

        ## 正确的读法

        配置在 `book-sources.yaml`（不是 `persona.yaml`）——
        书源和阅读节奏放一起更好维护。所以这里直接读那个文件，
        不依赖传进来的 `cfg`（参数留着是为了将来可能挪回主配置）。
        """
        data: dict[str, Any] = {}
        try:
            path = paths.project_root() / "config" / "book-sources.yaml"
            if path.is_file():
                import yaml

                data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception as exc:  # noqa: BLE001
            log.warning("网上阅读：阅读配置读不了（%s），用默认值", exc)

        rd = dict(data.get("reading") or {})
        cm = dict(data.get("community") or {})
        return cls(
            idle_after_s=float(rd.get("idle_after_s", 1800) or 1800),
            chunk_chars=int(rd.get("chunk_chars", 1500) or 1500),
            same_book_cooldown_s=float(rd.get("same_book_cooldown_s", 7200) or 7200),
            max_chapters_per_session=int(rd.get("max_chapters_per_session", 1) or 1),
            min_body_chars=int(rd.get("min_body_chars", 500) or 500),
            max_source_retries=int(rd.get("max_source_retries", 3) or 3),
            community_enabled=bool(cm.get("enabled", True)),
            community_sites=[dict(x) for x in (cm.get("sites") or [])],
            community_topics=[str(x) for x in (cm.get("topics") or [])],
            community_share=bool(cm.get("share", True)),
        )


@dataclass(slots=True)
class BookChunk:
    """读完的一段内容。"""

    title: str
    source: str
    text: str
    kind: str = "book"
    url: str = ""
    saved_path: str = ""
    chars: int = 0

    def summary_for_prompt(self) -> str:
        """给模型的提示词片段。

        不要把整段正文都塞进去 —— 那会吃光上下文，
        而且宠物不需要背原文，它需要"看了之后有点想法"。
        """
        head = self.text[:600]
        kind_word = "书" if self.kind == "book" else "帖子"
        return (
            f"你刚看了一段{kind_word}：《{self.title}》（来自{self.source}）。\n"
            f"内容大意：{head}\n"
        )


def _looks_like_front_matter(name: str, body: str) -> bool:
    """这两章是"版权页/作品相关"这类**非正文**吗？

    ## 🔴 为什么要按内容判断，而不是无条件跳前两章（2026-11-02 修）

    原来两处都是硬编码：

        idx = done if done >= 2 else 2      # 前两章常是版权页

    这个判断来自**新笔趣阁**的实测（它的第 1 章标题就叫「版权信息」），
    但被当成所有书源的通用规则了。实测后果：

        思兔阅读《天官赐邪》
          第1章 洞天福地   2599 字  评分 237.8   ← 正常正文
          第2章 红衣吊祟   2323 字  评分 213.5   ← 正常正文
          -> 两章被**无条件跳过**，用户发现时问「前面2章消失了」

    而这两章是故事的开头（主角第一次进福地、遇到第一个邪祟），
    跳过等于从第三章中间开始看 —— 比少读两章严重得多。

    ## 判据

    只有**同时满足**下面两条才认为是非正文：

      ① 标题像版权页（版权/声明/作品相关/关于/简介/目录/楔子以外的说明性标题）
      ② 内容不像正文（`body_score` 为 0 —— 太短/中文太少/没标点）

    ⚠️ 两条都要满足。只看标题的话，有的站第一章标题就叫「序」但内容是正文；
    只看内容的话，版权页有时也有几百字（出版社信息、ISBN、免责声明），
    标点密度够，会被误判成正文。

    ⚠️ 标题里出现「序章/楔子/引子/前言」时**一律当正文**：
    这些是故事的一部分（很多小说把关键伏笔放楔子）。
    """
    title = (name or "").strip()
    # 明确是正文的标题 -> 直接放行
    for good in ("序章", "楔子", "引子", "前言", "第一章", "第二章"):
        if good in title:
            return False
    # 明确像版权页的标题
    bad_words = ("版权", "声明", "作品相关", "关于本书", "免责", "公告", "上架感言", "致读者")
    name_suspicious = any(w in title for w in bad_words)
    # 内容不像正文（0 分 = 太短/中文少/没标点）
    score, _why = body_score(body)
    body_unfit = score <= 0
    # 🔴 空章节一律跳过（2026-11-02 实测）
    #
    # 新笔趣阁《诡秘之主》的目录第一条叫「新书已发」，点进去**一个字都没有**
    # （0 字）。它不是版权页，也不是正文 —— 就是一个占位条目。
    #
    # 不跳的话它会作为"第 1 章"落盘，用户看到的是一个只有标题的空章，
    # 而且进度会停在那里（下次从第 2 章继续，但文件里第 1 章是空的）。
    if not (body or "").strip():
        return True
    return name_suspicious and body_unfit


def _clip_at_sentence(text: str, limit: int) -> str:
    """把文本截到 `limit` 字以内，但**必须落在句子边界**上。

    ## 为什么不能直接 `text[:limit]`

    2026-11-02 用户翻记忆时问：「最新一条是没说完话吗」。

    实际写进去的是阅读历史，用 `chunk.text[:600]` 生切的，结尾长这样：

        据说罗塞尔大帝曾经看过"亵渎石板"，据说他制造的秘

    停在「秘」字上 —— **看起来就是话说了一半**。

    而且这个印象会传到模型那里：那一轮回复是紧接在这条记忆后面
    生成的，模型看到一段断掉的话，可能跟着"补全"或者自己说话也说一半。

    ## 怎么找边界

    从 `limit` 往回找**最近的**句末标点，在它后面截断：

        。！？…  —— 中文常用句末
        .!?      —— 英文/数字结尾
        "』」）  —— 引号/括号收尾（对话体小说大量使用）

    ⚠️ 往回找的上限是 40% —— 再往前就没有"截断"的意义了（保留太少）。
    找不到就硬切，并补一个省略号，至少让"这里是断的"是**明确**的，
    而不是看起来像话说了一半。

    ## 为什么不用 `textwrap` / 正则整段切

    只需要"别停在半个字上"这一件事。引入更复杂的切句逻辑
    （考虑省略号连用、引号嵌套）收益很低 —— 记忆里那段只是给模型
    看的摘要，不是要出版。
    """
    if limit <= 0 or not text:
        return ""
    if len(text) <= limit:
        return text
    head = text[:limit]
    # 从末尾往回找句末标点（最多回退 40%）
    floor = max(0, int(limit * 0.6))
    # ⚠️ 用集合而不是字符串字面量：中文收尾引号（”』」）本身就在
    #    字符串里，写成 `in "”』」"` 会让引号嵌套、语法直接崩。
    ENDS = set("。！？…!?.")
    TAILS = set("”』」）)】")
    for i in range(len(head) - 1, floor - 1, -1):
        if head[i] in ENDS:
            # 把紧随其后的收尾引号/括号也带上（对话体很常见）
            j = i + 1
            while j < len(head) and head[j] in TAILS:
                j += 1
            return head[:j]
    # 找不到句末 -> 硬切 + 省略号（明确表示"这里是断的"）
    return head + "…"


def body_score(text: str) -> tuple[float, str]:
    """给一段文字打"像不像小说正文"的分。0 分 = 不可用。

    三个指标都来自实测：

        长度           < 100 字的多半是导航/提示语
        中文占比       < 50% 说明是英文页或乱码
        **标点密度**    < 0.5% 说明是列表/导航 —— 这是最关键的一条

    为什么标点密度关键：导航栏（「首页 玄幻 武侠 都市 …」）中文占比也很高、
    字数也不少，单看中文占比分不出来。但导航栏里没有一个逗号句号，
    而正文标点密集。

    所以这条阈值不能去掉。回归测试里有一个导航栏反例守着它：
    把阈值改成 0 之后那个反例会被判成正文。
    """
    n = len(text)
    if n < 100:
        return 0.0, f"太短({n}字)"
    zh = len(re.findall(r"[\u4e00-\u9fff]", text))
    zh_ratio = zh / n
    if zh_ratio < 0.5:
        return 0.0, f"中文占比低({zh_ratio:.0%})"
    punct = len(re.findall(r"[，。！？、；：""''…—]", text))
    punct_ratio = punct / n
    if punct_ratio < 0.005:
        return 0.0, f"标点太少({punct_ratio:.3%})"
    return zh * punct_ratio, f"{n}字 中文{zh_ratio:.0%} 标点{punct}"


class BookSource:
    """一个书源的抓取器。

    用同步 httpx，不在这里写 async。抓取是 CPU/IO 混合的重活，
    塞进事件循环会卡住语音和聊天。异步调用方用 asyncio.to_thread
    包一层（见 Reader.read_book）。
    """

    def __init__(self, spec: SourceSpec) -> None:
        self.spec = spec

    def _client(self) -> Any:
        import httpx

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
            ),
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": self.spec.base,
        }
        return httpx.Client(
            headers=headers,
            timeout=20,
            follow_redirects=True,
            verify=False,
        )

    def _decode(self, resp: Any) -> str:
        """按配置编码解码；没配就自动探测。"""
        if self.spec.encoding:
            try:
                return resp.content.decode(self.spec.encoding, errors="replace")
            except LookupError:
                pass
        try:
            import charset_normalizer as cn

            guess = cn.from_bytes(resp.content).best()
            if guess is not None:
                return str(guess)
        except Exception:
            pass
        return resp.text

    def search(self, query: str) -> list[tuple[str, str]]:
        """搜书，返回 [(书名, 绝对URL)]。

        ## 关键词可以放路径，也可以放参数

        两种写法都支持（实测两种都用得上）：

            参数式   path: 'search'             + keyword_field: keyword
                     -> /search?keyword=三体            （新笔趣阁）

            路径式   path: 'search/{q}.html'
                     -> /search/三体.html                （思兔阅读）

        ⚠️ 路径里含 `{q}` 时**不能再往 params 里塞一次**关键词 ——
        否则会拼出 `/search/三体.html?_unused=三体`，
        某些站会因此返回 404 或热门页（思兔就是这样）。
        """
        import urllib.parse

        if not self.spec.search_path:
            return []
        q = urllib.parse.quote(query)
        path = self.spec.search_path.replace("{q}", q)
        url = self.spec.base + path.lstrip("/")
        # 关键词已经在路径里了 -> 只带固定附加参数，不再传 keyword_field
        in_path = "{q}" in self.spec.search_path

        with self._client() as c:
            if self.spec.search_method == "post":
                data = dict(self.spec.search_extra)
                if not in_path:
                    data[self.spec.keyword_field] = query
                r = c.post(url, data=data)
            else:
                params = dict(self.spec.search_extra)
                if not in_path:
                    params[self.spec.keyword_field] = query
                r = c.get(url, params=params)
            html = self._decode(r)

        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "lxml")
        pat = re.compile(self.spec.book_link) if self.spec.book_link else None
        out: list[tuple[str, str]] = []
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if pat and not pat.search(href):
                continue
            title = self._pick_title(a)
            if not title:
                continue
            # 过滤阅读/查看这类操作按钮的文本
            #
            # ⚠️ 实测（2026-10-31）：思兔阅读的每张卡片里既有书名链接，
            #    也有一个文字为「阅读」的同 URL 链接。不过滤的话搜索结果里
            #    会出现一堆叫「阅读」的重复项，_read_book_sync 选第一本时
            #    可能选中它 —— 书名就变成「阅读」了，很难看也对不上。
            if title in _ACTION_WORDS:
                continue
            full = href if href.startswith("http") else self.spec.base.rstrip("/") + (
                href if href.startswith("/") else "/" + href
            )
            out.append((title, full))
        return list(dict.fromkeys(out))

    @staticmethod
    def _pick_title(a: Any) -> str:
        """从一个搜索结果链接里取出**干净的书名**。

        ## 🔴 为什么不能直接用 `a.get_text()`

        实测（2026-10-31，新笔趣阁）：搜索结果里 `<a>` 包着**整张卡片**
        —— 书名 + 作者 + 简介全在里面：

            三体（全集）刘慈欣刘慈欣著作《三体》是一本非常好看的…

        第一版直接把 `a.get_text()` 当书名，再用 `len > 40` 过滤，
        结果 7 本全被滤掉 -> 日志显示"搜不到这本书"，
        而实际上**搜索是成功的**（正则匹配到 7 个链接）。

        这种失败最难查：搜索、正则、网络都正常，只是标题脏。

        ## 取法（按可靠性排序）

        1. 卡片内的标题节点（`.title` / `h1`~`h4` / `.c_subject` 等）
        2. `<a>` 自己的 title 属性
        3. 退回到 `<a>` 的文本，但**截断到第一个换行或 30 字**

        ## 🔴 书名可能在**兄弟节点**里，不在 `<a>` 内部（2026-10-31 补）

        实测（阅读库 yeudusk）：搜索结果的一张卡片长这样：

            <div class="c_row">
              <div class="fl"><a href="/book/510881/"><img .../></a></div>
              <div>
                <div><span class="c_subject"><a href="/book/510881/">夜玄</a></span> /
                     <a href="/html/510/510881/">目录</a></div>
                ...

        于是：
          · 第一个 `<a>` 只包着**封面图**，`get_text()` 是**空串**
          · 书名在 `<span class="c_subject">` 里，是它的**兄弟**
          · 而 `/book/{id}/` 和 `/html/{a}/{id}/` 两个链接**都指向这本书**

        如果只在 `<a>` 自己内部找标题，拿到的是空串 -> 这本书被跳过
        -> 表现为「搜到了 100 条，但一本都没解析出来」。

        所以：先在 `<a>` 内部找，找不到就**往上走几层卡片**再找。
        往上走有风险（可能抓到整个列表的标题），所以只走 3 层，
        并且要求找到的文本**够短**（书名不会超过 60 字）。
        """
        # ① 卡片里的标题节点 —— 先在自己内部找
        for sel in (".title", "h1", "h2", "h3", "h4", ".bookname", ".name", ".c_subject"):
            node = a.select_one(sel)
            if node is not None:
                txt = node.get_text(strip=True)
                if txt:
                    return txt[:60]
        # ①-b 再往上走 3 层找（书名常在与 <a> 同级的 span 里）
        node = a
        for _ in range(3):
            node = node.parent
            if node is None:
                break
            for sel in (".c_subject", ".title", ".bookname"):
                found = node.select_one(sel)
                if found is not None:
                    txt = found.get_text(strip=True)
                    # ⚠️ 必须够短：往上走可能抓到整个列表的标题，
                    #    那种是"书名 作者 类别 …"拼起来的一大串
                    if txt and len(txt) <= 60:
                        return txt
        # ② title 属性
        attr = a.get("title")
        if attr and str(attr).strip():
            return str(attr).strip()[:60]
        # ③ 退回 a 的文本：按换行切，取第一段；再兜底截断
        raw = a.get_text("\n", strip=True)
        if not raw:
            return ""
        first = raw.splitlines()[0].strip()
        # 卡片文本常常是"书名+作者+简介"连在一起，
        # 这里没法可靠切分，所以只在够短时才用
        if len(first) <= 30:
            return first
        # 太长就当作没取到 —— 宁可跳过这本书，也别把一坨简介当书名
        return ""

    def chapter_urls(self, book_url: str) -> list[tuple[str, str]]:
        """拿章节列表。先试 toc_url，失败再直接扫书页。

        ## 🔴 `book_id` 的抽取必须宽容（2026-10-31 实测）

        三个站的书籍 URL 形态完全不同：

            /book/gtfft/                       小写 + 末尾斜杠
            /50606                             纯数字，无前缀
            /book/16ABo1xDXh4nyeBPyapArh.html  混合大小写 + .html 后缀

        第一版写死 `r"/book/([a-z0-9]+)/"`，结果：
          · 大写的抓不到（`16ABo1...` 里的大写字母不在 `[a-z0-9]`）
          · 没有末尾斜杠的抓不到（`/book/xxx.html`）

        抽不到 book_id 的后果是 `toc_url` 拼不出来，退化成"直接扫书页"，
        而那些站的章节链接根本不在书页上 —— 表现为"这本书没有章节"。

        所以这里试三种模式，按顺序取第一个能匹配的。
        """
        from bs4 import BeautifulSoup

        book_id = ""
        for pat in (
            r"/book/([A-Za-z0-9]+)\.html?",  # /book/xxx.html  或 /book/xxx/
            r"/book/([A-Za-z0-9]+)/",
            r"/([A-Za-z0-9]+)\.html$",        # /<id>.html
            r"/([A-Za-z0-9]{4,})/?$",          # /<id>  或 /<id>/
        ):
            m = re.search(pat, book_url)
            if m:
                book_id = m.group(1)
                break

        ch_pat = re.compile(self.spec.chapter_link) if self.spec.chapter_link else None
        targets: list[str] = []
        if self.spec.toc_url and book_id:
            targets.append(
                self.spec.base.rstrip("/")
                + "/"
                + self.spec.toc_url.replace("{book_id}", book_id)
                .replace("{book_url}", book_url)
                .lstrip("/")
            )
        targets.append(book_url)

        for url in targets:
            try:
                with self._client() as c:
                    html = self._decode(c.get(url))
            except Exception:
                continue
            soup = BeautifulSoup(html, "lxml")
            chaps: list[tuple[str, str]] = []
            for a in soup.find_all("a", href=True):
                href = a["href"]
                title = a.get_text(strip=True)
                if not title or len(title) > 60:
                    continue
                if ch_pat is not None:
                    if not ch_pat.search(href):
                        continue
                elif not re.search(r"\.html?$", href):
                    continue
                full = href if href.startswith("http") else self.spec.base.rstrip("/") + (
                    href if href.startswith("/") else "/" + href
                )
                chaps.append((title, full))
            chaps = list(dict.fromkeys(chaps))
            if chaps:
                return chaps
        return []

    def chapter_body(self, url: str) -> tuple[str, str]:
        """取一章正文。返回 (正文, 命中的选择器)。

        为什么按选择器逐个试还不够：某些站每页都有 #content，
        但书页/目录页里那个 div 装的是导航和推荐书单。所以这里在
        候选之上再算两件事：

          链接文字占比 —— 导航块几乎全是 a 标签，正文几乎没有
          body_score   —— 中文占比 + 标点密度

        取分数最高的那个，而不是第一个匹配到的。

        ## base64 混淆（2026-10-31 实测）

        部分站点把正文切成几十段 base64，用随机函数名动态写入：

            document.writeln(qryxkm.kagvx('PHA+5aSn56yU6Laj...'));
            document.writeln(fgqm.eep('PHA+5q+V56uf5piv...'));

        页面上 `#content` 只剩 149 字符的导航，**真正的正文一个字都不在
        HTML 里**。函数名每页随机（`kagvx` / `eep` / …），所以不能按名字
        匹配 —— 只能匹配结构：`任意标识符.任意标识符('base64')`。

        纯选择器方案对这类页面**永远抓不到**，而且症状很像"这站没正文"，
        于是会一直换源而不自知。所以这里先解混淆，再走选择器。
        """
        from bs4 import BeautifulSoup

        with self._client() as c:
            html = self._decode(c.get(url))

        # ⓪ 先按配置清洗 HTML（去广告段/结束标记）
        #
        # ⚠️ 必须在**取文本之前**做，而且作用于 HTML 而不是纯文本：
        #    广告段是 `<p class="readinline">…</p>`，剥完标签就看不出边界了。
        for pat in self.spec.drop_patterns:
            try:
                html = re.sub(pat, "", html)
            except re.error as exc:
                log.warning("网上阅读：正文清洗正则写错了（%s）：%s", pat[:30], exc)

        # ① 再解 base64 混淆，把解出来的 HTML 片段也当成候选来源
        deob = _decode_obfuscated_blocks(html)
        if deob:
            try:
                frag_soup = BeautifulSoup(deob, "lxml")
                text = frag_soup.get_text("\n", strip=True)
                score, _ = body_score(text)
                if score > 0:
                    log.info("网上阅读：页面是 base64 混淆的，已解出 %d 字", len(text))
                    return text, "(base64 解混淆)"
            except Exception:  # noqa: BLE001
                pass

        soup = BeautifulSoup(html, "lxml")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()

        best: tuple[float, str, str] = (0.0, "", "")
        sels = self.spec.selectors or ["#content", "article", ".content"]
        for sel in sels:
            try:
                nodes = soup.select(sel)
            except Exception:
                continue
            for node in nodes:
                text = node.get_text("\n", strip=True)
                if len(text) < self.spec.min_chars:
                    continue
                link_len = sum(len(a.get_text(strip=True)) for a in node.find_all("a"))
                if link_len > len(text) * 0.3:
                    continue
                score, _ = body_score(text)
                if score > best[0]:
                    best = (score, text, sel)

        if best[0] <= 0:
            for tag in ("div", "article", "section", "td"):
                for node in soup.find_all(tag):
                    text = node.get_text("\n", strip=True)
                    if len(text) < self.spec.min_chars:
                        continue
                    link_len = sum(
                        len(a.get_text(strip=True)) for a in node.find_all("a")
                    )
                    if link_len > len(text) * 0.3:
                        continue
                    score, _ = body_score(text)
                    if score > best[0]:
                        ident = (
                            node.get("id")
                            or " ".join(node.get("class") or [])
                            or tag
                        )
                        best = (score, text, f"(兜底){ident}")
        return best[1], best[2]


class Reader:
    """阅读调度：选源 -> 抓 -> 校验 -> 落盘 -> 产出可分享的片段。"""

    def __init__(self, cfg: Any) -> None:
        self.config = ReadingConfig.from_config(cfg)
        self.sources: list[SourceSpec] = []
        self._load_sources()
        self._last_read: dict[str, float] = {}
        self._read_titles: set[str] = set()

    def _load_sources(self) -> None:
        """从 config/book-sources.yaml 读源。文件不存在就是空列表。"""
        path = paths.project_root() / "config" / "book-sources.yaml"
        if not path.is_file():
            log.info("网上阅读：没有 %s，书源为空（只能看社区帖子）", path.name)
            return
        try:
            import yaml

            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception as exc:
            log.warning("网上阅读：书源配置读不了（%s）", exc)
            return
        specs = []
        for item in data.get("sources") or []:
            if not isinstance(item, dict):
                continue
            try:
                specs.append(SourceSpec.from_dict(item))
            except Exception as exc:
                log.warning("网上阅读：跳过格式不对的源（%s）", exc)
        self.sources = [s for s in specs if s.enabled]
        log.info("网上阅读：载入 %d 个书源（共 %d 条）", len(self.sources), len(specs))

    def has_book_sources(self) -> bool:
        return bool(self.sources)

    def books_dir(self) -> Path:
        """书存放位置。

        用户 2026-10-31 明确要求放项目里（F:/ai/pet-books/），
        而不是 %APPDATA% —— 他想自己去翻。

        这个目录已在 .gitignore 里。否则下几本书就会把仓库撑爆，
        而且盗版书进了公开仓库很麻烦。
        """
        d = paths.project_root() / "pet-books"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _book_path(self, title: str) -> Path:
        """一本书对应**一个**文件（按章节追加，不是每章一个文件）。

        用户 2026-10-31 要求：「按章节来看，所以一章一章下载进去，
        中间插个下载时间的日志就好了」

        所以文件名里**不能带时间戳** —— 带的话每次抓章都生成新文件，
        就变成"一堆碎片"而不是"一本在连载的书"。
        """
        safe = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", title).strip()[:80] or "untitled"
        return self.books_dir() / f"{safe}.txt"

    def _next_chapter_index(self, title: str) -> int:
        """这本书已经下到第几章了。用来决定下一次抓哪一章。

        判断依据是文件里的 `<!-- chapter:N -->` 标记（见 `_save`）。
        读不到就是 0，从头开始。
        """
        path = self._book_path(title)
        if not path.is_file():
            return 0
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            return 0
        found = re.findall(r"<!-- chapter:(\d+) -->", content)
        if not found:
            return 0
        return max(int(x) for x in found)

    def _save(
        self,
        title: str,
        source: str,
        text: str,
        chapter_no: int = 0,
        chapter_name: str = "",
    ) -> str:
        """把一章**追加**到这本书的文件里，返回路径。

        ## 文件格式（用户要求的"中间插个下载时间的日志"）

            《诡秘之主》
            来源：新笔趣阁
            开始：2026-10-31 19:51:00

            <!-- chapter:3 -->
            ──────────────────────────────────────────
            第 3 章  小丑
            下载于 2026-10-31 20:15:33
            ──────────────────────────────────────────
            （正文…）

        `<!-- chapter:N -->` 是给程序认的（用来知道下次从第几章开始），
        HTML 注释形式在纯文本里看不出来，不影响阅读。

        ⚠️ **追加而不是覆盖**：一本书读到一半，下次要接着往下读，
           不能把前面的章节冲掉。
        """
        path = self._book_path(title)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")

        if not path.is_file():
            head = f"《{title}》\n来源：{source}\n开始：{stamp}\n\n"
            path.write_text(head, encoding="utf-8")

        line = "─" * 52
        # 标题行：站点给的章节名**本身常带「第一章」**，再加「第 N 章」就重复了。
        #
        # 实测（2026-10-31）：
        #     第 3 章  第一章 绯红        <- 难看，而且两个编号对不上
        #     第一章 绯红（全书第 3 章）   <- 这样才清楚
        #
        # 所以：章节名里已经有「第X章」就把它当主标题，把序号放括号里。
        name = (chapter_name or "").strip()
        if re.match(r"^第[0-9一二三四五六七八九十百千零两]+[章节回]", name):
            head_line = f"{name}（全书第 {chapter_no} 章）"
        else:
            head_line = f"第 {chapter_no} 章  {name}" if name else f"第 {chapter_no} 章"
        block = (
            f"\n<!-- chapter:{chapter_no} -->\n"
            f"{line}\n"
            f"{head_line}\n"
            f"下载于 {stamp}\n"
            f"{line}\n\n"
            f"{text}\n"
        )
        with path.open("a", encoding="utf-8") as fh:
            fh.write(block)
        return str(path)

    def _read_book_sync(self, query: str) -> Optional[BookChunk]:
        """同步抓一本书。逐源尝试，直到拿到合格正文。"""
        import urllib3

        urllib3.disable_warnings()

        tries = 0
        for spec in self.sources:
            if tries >= self.config.max_source_retries:
                break
            tries += 1
            src_name = spec.name
            try:
                src = BookSource(spec)
                books = src.search(query)
                if not books:
                    log.info("网上阅读：%s 搜不到「%s」", src_name, query)
                    continue
                """
                🔴 必须**真的匹配上书名**，不能退回 `books[0]`（2026-10-31 修，真 bug）

                原实现：

                    title, url = next((b for b in books if query in b[0]), books[0])
                                                                        ^^^^^^^^^

                那个兜底看起来无害（"实在没有就随便拿一本"），实测后果是：

                    用户让它读《夜玄》
                    -> 新笔趣阁搜「夜玄」返回一堆**带"玄"字的无关书**
                    -> 一本都不含「夜玄」
                    -> 兜底取了 books[0]
                    -> 落盘的是《让你御兽，你融合斩魄刀？》

                而日志会显示「网上阅读：✅《让你御兽…》读了 3 章」——
                看起来一切正常，**只是读错了书**。

                用户说的是「我让他搜搜看，他就能去搜索」，读到一本完全不相干的
                小说比读不到糟糕得多：它会拿那本书的情节跟用户聊。

                所以：**匹配不上就换下一个源**，宁可读不到。
                """
                matched = next((b for b in books if query in b[0]), None)
                if matched is None:
                    log.info(
                        "网上阅读：%s 搜到 %d 本，但没有一本书名含「%s」—— 换源",
                        src_name, len(books), query,
                    )
                    continue
                title, url = matched
                chaps = src.chapter_urls(url)
                if not chaps:
                    log.info("网上阅读：%s 的《%s》没有章节", src_name, title)
                    continue

                # 🔴 接着上次的进度往下读（2026-10-31 用户要求）
                #
                # 用户原话：「按章节来看，所以一章一章下载进去，
                # 中间插个下载时间的日志就好了」
                #
                # 所以每次只抓**下一章**，追加到同一个文件里。
                # 进度存在文件内（`<!-- chapter:N -->`），不另存状态文件 ——
                # 状态文件会和实际内容脱节（用户手动删了文件，状态还在）。
                done = self._next_chapter_index(title)
                # 🔴 起点统一由 `_start_index` 算（2026-11-02 抽出）
                #
                # 原来这里内联了一段"无条件跳过前两章"的代码：
                #
                #     idx = done if done >= 2 else 2      # 前两章常是版权页
                #
                # 那条规则来自**新笔趣阁**的实测（它的第 1 章叫「版权信息」），
                # 却被当成所有书源的通用规则。实测翻车：
                #
                #     思兔阅读《天官赐邪》第 1、2 章是正常正文
                #     （2599 字 / 2323 字，评分 237 / 213），被一起跳掉，
                #     用户发现时问「天官赐邪的前面2章消失了」。
                #
                # 现在改成按内容判断，且**两处共用同一个方法** ——
                # 原来两处各写一份，改一处必然漏另一处。
                idx = self._start_index(src, chaps, title, done, src_name)
                if idx >= len(chaps):
                    log.info(
                        "网上阅读：%s 的《%s》已读完（%d/%d 章）",
                        src_name, title, done, len(chaps),
                    )
                    # 读完了：清掉进度，下次从头（或者换个词再搜）
                    self._last_read[title] = time.monotonic()
                    return None

                ct, cu = chaps[idx]
                body, _sel = src.chapter_body(cu)
                if not body:
                    log.info(
                        "网上阅读：%s 的《%s》第 %d 章抓不到正文，换源",
                        src_name, title, idx + 1,
                    )
                    continue
                score, why = body_score(body)
                if score <= 0:
                    log.info(
                        "网上阅读：%s 的《%s》第 %d 章正文不合格（%s），换源",
                        src_name, title, idx + 1, why,
                    )
                    continue
                if len(body) < self.config.min_body_chars:
                    log.info(
                        "网上阅读：%s 的《%s》第 %d 章正文太短（%d 字），换源",
                        src_name, title, idx + 1, len(body),
                    )
                    continue

                # 交给模型的是**摘要长度**，落盘的是**整章**
                #
                # ⚠️ 这两个长度故意不同：
                #    · `chunk_chars`（1500）是给模型看的量，多了吃光上下文
                #    · 落盘要保留整章，否则用户下载下来的书是残缺的
                text = body[: self.config.chunk_chars]
                saved = self._save(
                    title, src_name, body,
                    chapter_no=idx + 1, chapter_name=ct,
                )
                log.info(
                    "网上阅读：%s 《%s》第 %d/%d 章「%s」（%s，%d 字）-> %s",
                    src_name, title, idx + 1, len(chaps), ct[:20], why,
                    len(body), Path(saved).name,
                )
                self._read_titles.add(title)
                self._last_read[title] = time.monotonic()
                return BookChunk(
                    title=title,
                    source=src_name,
                    text=text,
                    kind="book",
                    url=url,
                    saved_path=saved,
                    chars=len(body),
                )
            except Exception as exc:
                log.info(
                    "网上阅读：%s 出错（%s: %s），换源",
                    src_name, type(exc).__name__, str(exc)[:60],
                )
                continue
        return None

    async def read_book(self, query: str = "") -> Optional[BookChunk]:
        """异步入口：asyncio.to_thread 包一层，绝不阻塞事件循环。

        为什么不写 async 抓取：抓一本要串行发十几个请求（搜索->目录->章节），
        httpx.Client 是同步的。放进事件循环会把语音识别一起卡住 ——
        而"用户在说话"恰恰是最不能卡的时刻。
        """
        if not self.sources:
            return None
        if not query:
            query = self._pick_query()
        if not query:
            return None
        return await asyncio.to_thread(self._read_book_sync, query)

    # ------------------------------------------------------ 模型主动调用（多章）

    def read_many(
        self, query: str, chapters: int = 1
    ) -> Optional[tuple[str, int, int, str, list[tuple[str, str]]]]:
        """读**若干章**，从上次读到的地方接着读。同步方法（调用方丢线程）。

        返回 `(书名, 本次读了几章, 总字数, 文件路径, [(章名, 片段), ...])`。

        ## 用户要求（2026-10-31）

        > 「我的意思是自己选一本书读，有时候会换着读，有时候会读很多章，
        >   比如一次几十章几万字。」
        >
        > 「继续读之前的书的话要按照之前的内容继续读」

        所以这个方法的两个要点：

        **① 能一次读很多章**
           循环抓，每章追加进同一个文件。中途某章失败就**停在那里**
           （不跳过 —— 跳过会在书里留个洞，用户看的时候莫名其妙断掉）。

        **② 必须接着上次的进度**
           进度从文件里的 `<!-- chapter:N -->` 读出来（`_next_chapter_index`）。
           不另存状态文件：用户可能手动删了 txt，那时状态文件还在，
           就会出现"文件没了但程序以为读到第 50 章"。

        ## 为什么中途失败要停

        假设读 20 章，第 8 章抓不到。如果跳过继续读第 9 章：
          · 文件里第 8 章的位置是个洞
          · 下次 `_next_chapter_index` 读到第 20 章，第 8 章永远补不回来
        停在第 7 章的话，下次会重试第 8 章 —— 这才是对的。
        """
        import urllib3

        urllib3.disable_warnings()

        if not self.sources:
            return None
        want = max(1, int(chapters))

        tries = 0
        for spec in self.sources:
            if tries >= self.config.max_source_retries:
                break
            tries += 1
            src_name = spec.name
            try:
                src = BookSource(spec)
                books = src.search(query)
                if not books:
                    log.info("网上阅读：%s 搜不到「%s」", src_name, query)
                    continue
                # ⚠️ 同样**不能**退回 `books[0]`（见 `read_book` 里那段说明）：
                #    匹配不上就换源，宁可读不到，也别读错书。
                matched = next((b for b in books if query in b[0]), None)
                if matched is None:
                    log.info(
                        "网上阅读：%s 搜到 %d 本，但没有一本书名含「%s」—— 换源",
                        src_name, len(books), query,
                    )
                    continue
                title, url = matched
                chaps = src.chapter_urls(url)
                if not chaps:
                    log.info("网上阅读：%s 的《%s》没有章节", src_name, title)
                    continue

                # 进度：接着上次读
                done = self._next_chapter_index(title)
                # ⚠️ 和 `read_book` 用**同一个**起点计算（`_start_index`），
                #    不要再各写一份 —— 上面那个"无条件跳两章"的 bug
                #    就是因为两处各写了一份，改一处漏另一处。
                idx = self._start_index(src, chaps, title, done, src_name)
                if idx >= len(chaps):
                    log.info(
                        "网上阅读：《%s》已读完（%d/%d 章）",
                        title, done, len(chaps),
                    )
                    return (title, 0, 0, str(self._book_path(title)), [])

                start_idx = idx
                total_chars = 0
                got = 0
                samples: list[tuple[str, str]] = []
                last_path = str(self._book_path(title))

                while idx < len(chaps) and got < want:
                    ct, cu = chaps[idx]
                    try:
                        body, _sel = src.chapter_body(cu)
                    except Exception as exc:  # noqa: BLE001
                        log.info(
                            "网上阅读：《%s》第 %d 章请求失败（%s），停在这里",
                            title, idx + 1, type(exc).__name__,
                        )
                        break
                    if not body:
                        log.info(
                            "网上阅读：《%s》第 %d 章没正文，停在这里（下次重试）",
                            title, idx + 1,
                        )
                        break
                    score, _why = body_score(body)
                    if score <= 0 or len(body) < self.config.min_body_chars:
                        log.info(
                            "网上阅读：《%s》第 %d 章正文不合格，停在这里",
                            title, idx + 1,
                        )
                        break

                    last_path = self._save(
                        title, src_name, body,
                        chapter_no=idx + 1, chapter_name=ct,
                    )
                    got += 1
                    total_chars += len(body)
                    if len(samples) < 3:
                        snip = re.sub(r"\s+", " ", body[:100]).strip()
                        samples.append((ct[:20], snip))
                    log.info(
                        "网上阅读：%s 《%s》第 %d/%d 章「%s」（%d 字）",
                        src_name, title, idx + 1, len(chaps), ct[:20], len(body),
                    )
                    idx += 1

                if got == 0:
                    log.info("网上阅读：%s 的《%s》这一轮一章都没读到，换源", src_name, title)
                    continue

                self._read_titles.add(title)
                self._last_read[title] = time.monotonic()
                log.info(
                    "网上阅读：✅《%s》读了 %d 章（第 %d~%d 章），共 %d 字",
                    title, got, start_idx + 1, start_idx + got, total_chars,
                )
                return (title, got, total_chars, last_path, samples)
            except Exception as exc:  # noqa: BLE001
                log.info(
                    "网上阅读：%s 出错（%s: %s），换源",
                    src_name, type(exc).__name__, str(exc)[:60],
                )
                continue
        return None

    def search_books(self, query: str, limit: int = 8) -> list[dict[str, Any]]:
        """搜书，返回 `[{title, url, source, chapters}]`。给 `search_book` 工具用。

        ## 为什么单独开一个公开方法，而不是让工具直接调 `BookSource.search`

        工具层不该知道 `SourceSpec` / `BookSource` 这些内部结构 ——
        它只需要"给我一批书，告诉我每本有多少章"。这样以后换书源实现、
        加缓存、加去重，都不用动工具代码。

        ## ⚠️ 章数要**真的去问一次目录**

        用户说「搜搜看这本书」时，最想知道的是"能不能看、有多少章"。
        只回书名的话，模型还得再调一次 `read_book` 才知道这本书行不行 ——
        而 `read_book` 会**直接下载**，等于用户只是问问就先落了盘。

        所以这里对**前几本**顺手查一下目录（`limit` 控制，默认最多 8 本，
        但查目录只查前 3 本 —— 每本都是一个额外 HTTP 请求，
        全查一遍在慢站上会拖到十几秒）。

        ## 失败要能区分"搜不到"和"书源挂了"

        返回空列表 = 这本书在所有书源都搜不到。
        但如果是**所有书源都连不上**，也返回空 —— 这时模型会说"没找到"，
        而真实原因是网络/站点挂了。调用方看日志能分辨（每个源都有记录），
        但工具返回的文本里应该带一句提示（见 `reading_tools._search_book`）。
        """
        if not self.sources:
            return []

        seen: set[str] = set()
        out: list[dict[str, Any]] = []
        tried = 0
        for spec in self.sources:
            if tried >= self.config.max_source_retries:
                break
            tried += 1
            try:
                src = BookSource(spec)
                books = src.search(query)
            except Exception as exc:  # noqa: BLE001
                log.info(
                    "搜书：%s 出错（%s: %s）",
                    spec.name, type(exc).__name__, str(exc)[:60],
                )
                continue
            if not books:
                log.info("搜书：%s 搜不到「%s」", spec.name, query)
                continue
            for title, url in books:
                key = title.strip()
                if not key or key in seen:
                    continue
                seen.add(key)
                item: dict[str, Any] = {
                    "title": key,
                    "url": url,
                    "source": spec.name,
                    "chapters": 0,
                }
                # 只给前 3 本查目录（每本一次请求，全查会很慢）
                if len(out) < 3:
                    try:
                        item["chapters"] = len(src.chapter_urls(url))
                    except Exception:  # noqa: BLE001
                        item["chapters"] = 0
                out.append(item)
                if len(out) >= limit:
                    break
            if len(out) >= limit:
                break
        return out

    def _parse_chapters(self, title: str) -> list[dict[str, Any]]:
        """把一本书的 txt 拆成章节列表。给 `review_book` 工具用。

        返回 `[{no, name, text, chars, downloaded_at}]`，按章号升序。

        ## 文件格式（`_save` 写出来的）

            《书名》
            来源：某站
            开始：2026-11-02 10:00:00


            <!-- chapter:3 -->
            ────────────────────────────────────
            第3章  章节名
            下载于 2026-11-02 10:00:01
            ────────────────────────────────────

            正文…

        ## ⚠️ 解析必须宽容

        用户可能手动编辑过这个文件（删掉某章、改标题、粘贴别的东西进来）。
        解析失败时**返回能解析出来的部分**，而不是抛异常 ——
        "回看"是只读功能，它失败不该影响任何别的事。

        ⚠️ 章号可能**不连续**（用户删过中间的章节），不要假设 `no == 下标 + 1`。
        """
        path = self._book_path(title)
        if not path.is_file():
            return []
        try:
            raw = path.read_text(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            return []

        parts = re.split(r"<!-- chapter:(\d+) -->", raw)
        out: list[dict[str, Any]] = []
        i = 1
        while i + 1 < len(parts) + 1 and i + 1 <= len(parts) - 1:
            try:
                no = int(parts[i])
            except (ValueError, IndexError):
                break
            body = parts[i + 1] if i + 1 < len(parts) else ""

            # 从正文块里拆出"标题行 / 下载时间 / 正文"
            name = ""
            downloaded = ""
            text_lines: list[str] = []
            in_body = False
            for line in body.splitlines():
                s = line.strip()
                if s.startswith("─"):
                    # 第二条分隔线之后才是正文
                    if in_body is False and name:
                        in_body = True
                    continue
                if not in_body:
                    if s.startswith("下载于"):
                        downloaded = s.replace("下载于", "").strip()
                    elif s and not name:
                        name = s
                    continue
                if s:
                    text_lines.append(s)

            text = "\n".join(text_lines)
            out.append(
                {
                    "no": no,
                    "name": name,
                    "text": text,
                    "chars": len(text),
                    "downloaded_at": downloaded,
                }
            )
            i += 2

        out.sort(key=lambda c: c["no"])
        return out

    def review_book(
        self,
        title: str,
        from_chapter: int = 0,
        to_chapter: int = 0,
        limit_chars: int = 0,
    ) -> dict[str, Any]:
        """回看已下载的章节（2026-11-02 用户要求）。

        用户原话：「给它加一个能看过往章节的功能吧，这样方便回去看伏笔和故事精彩描写」。

        ## 为什么不重新联网抓

        已经下载的章节**就在本地 txt 里**。联网重抓有三个问题：
          · 慢（每章一个 HTTP 请求）
          · 书源可能已经失效（免费站寿命短，昨天能抓今天挂）
          · 站点内容可能改过，回看应该看到**当初读到的那一版**

        所以这个功能是纯本地的：读文件、切片、返回。

        ## 参数

        · `from_chapter` / `to_chapter`：章号区间，**含两端**。
          都给 0 = 全书目录（只列标题，不带正文）。
        · `limit_chars`：正文最多返回多少字（0 = 每章全文）。
          伏笔回看通常只要片段，给个上限能省上下文。

        返回 `{"ok", "title", "total", "chapters": [...], "message"}`。
        """
        chapters = self._parse_chapters(title)
        if not chapters:
            return {
                "ok": False,
                "title": title,
                "total": 0,
                "chapters": [],
                "message": f"本地没有《{title}》的下载记录。",
            }

        nums = [c["no"] for c in chapters]
        # 都不给 -> 只回目录（这是"我想知道自己读到哪了"的最常见用法）
        if from_chapter <= 0 and to_chapter <= 0:
            return {
                "ok": True,
                "title": title,
                "total": len(chapters),
                "range": [nums[0], nums[-1]],
                "chapters": [
                    {"no": c["no"], "name": c["name"], "chars": c["chars"]}
                    for c in chapters
                ],
                "message": f"《{title}》共 {len(chapters)} 章（第 {nums[0]}~{nums[-1]} 章）",
            }

        lo = from_chapter if from_chapter > 0 else nums[0]
        hi = to_chapter if to_chapter > 0 else nums[-1]
        if hi < lo:
            lo, hi = hi, lo

        picked = [c for c in chapters if lo <= c["no"] <= hi]
        if not picked:
            return {
                "ok": False,
                "title": title,
                "total": len(chapters),
                "chapters": [],
                "message": (
                    f"《{title}》本地只有第 {nums[0]}~{nums[-1]} 章，"
                    f"没有 {lo}~{hi} 这一段。"
                ),
            }

        out_chapters = []
        for c in picked:
            text = c["text"]
            if limit_chars > 0 and len(text) > limit_chars:
                text = _clip_at_sentence(text, limit_chars)
            out_chapters.append(
                {
                    "no": c["no"],
                    "name": c["name"],
                    "chars": c["chars"],
                    "downloaded_at": c["downloaded_at"],
                    "text": text,
                }
            )

        return {
            "ok": True,
            "title": title,
            "total": len(chapters),
            "range": [nums[0], nums[-1]],
            "chapters": out_chapters,
            "message": f"《{title}》第 {lo}~{hi} 章（共 {len(out_chapters)} 章）",
        }

    def list_downloaded(self) -> list[dict[str, Any]]:
        """列出本地已下载的书和进度。给 `list_books` 工具用。"""
        out: list[dict[str, Any]] = []
        d = self.books_dir()
        if not d.is_dir():
            return out
        for f in sorted(d.glob("*.txt")):
            try:
                content = f.read_text(encoding="utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                continue
            found = re.findall(r"<!-- chapter:(\d+) -->", content)
            out.append(
                {
                    "title": f.stem,
                    "chapter": max((int(x) for x in found), default=0),
                    "size_kb": f.stat().st_size / 1024,
                    "updated": time.strftime(
                        "%Y-%m-%d %H:%M", time.localtime(f.stat().st_mtime)
                    ),
                }
            )
        return out

    def _start_index(
        self,
        src: "BookSource",
        chaps: list[tuple[str, str]],
        title: str,
        done: int,
        src_name: str,
    ) -> int:
        """这次从第几章开始读（0 基下标）。

        ## 为什么抽成方法

        `read_book` 和 `read_many` 原来各写了一份：

            idx = done if done >= 2 else 2      # 前两章常是版权页

        两处各写一份的直接后果就是：改了一处、漏了另一处
        （这个 bug 已经发生过 —— 见下面）。

        ## 🔴 "无条件跳前两章"是错的（2026-11-02 修）

        那条规则来自**新笔趣阁**的实测（它的第 1 章标题就叫「版权信息」），
        却被当成所有书源的通用规则。

        实测翻车：思兔阅读《天官赐邪》

            第1章 洞天福地   2599 字  评分 237.8   ← 正常正文
            第2章 红衣吊祟   2323 字  评分 213.5   ← 正常正文

        两章被一起跳掉，用户发现时问「天官赐邪的前面2章消失了」。
        而那是故事开头（主角第一次进福地、遇到第一个邪祟），
        跳过等于从第三章中间开始看。

        ## 现在的判据

        从 `done` 开始（没读过就是 0），**逐章检查**前几章：
        是版权页就跳过，是正文就照读（见 `_looks_like_front_matter`）。

        ⚠️ 检查要抓正文（每章一个 HTTP 请求），所以只在
        `done == 0`（从头开始）时做，且最多看 3 章 ——
        续读时不会白抓一遍。

        ## 还要去重（2026-11-02 实测）

        新笔趣阁《诡秘之主》的目录前三条：

            [0] 新书已发        0 字      <- 空占位，跳过
            [1] 开始阅读     2493 字      <- 和下面**内容完全一样**
            [2] 第一章 绯红   2493 字      <- 正文

        站点把「开始阅读」也当成一个目录条目，指向同一章。
        不去重的话第一章会落盘两遍（用户看到同一段文字重复），
        而且进度会多算一章。

        判据：**正文前 80 字完全相同**就算重复。不做全文比较 ——
        正文可能有细微差异（空格、广告），但开头一致基本就是同一章。
        """
        idx = max(0, int(done))
        if idx > 0:
            return idx          # 已经读过 -> 直接接着读，不回头判

        seen_heads: list[str] = []
        for _ in range(3):
            if idx >= len(chaps):
                break
            ct0, cu0 = chaps[idx]
            try:
                body0, _ = src.chapter_body(cu0)
            except Exception:  # noqa: BLE001 —— 抓不到就不跳
                break

            # ① 空章 / 版权页 -> 跳过
            if _looks_like_front_matter(ct0, body0):
                log.info(
                    "网上阅读：%s 的《%s》第 %d 章「%s」是版权/说明/空章，跳过",
                    src_name, title, idx + 1, ct0[:16],
                )
                idx += 1
                continue

            # ② 和前面某一章开头相同 -> 重复条目，跳过
            head = re.sub(r"\s+", "", (body0 or "")[:80])
            if head and head in seen_heads:
                log.info(
                    "网上阅读：%s 的《%s》第 %d 章「%s」与前面重复，跳过",
                    src_name, title, idx + 1, ct0[:16],
                )
                idx += 1
                continue

            # ③ 是真正文 -> 从这一章开始
            if head:
                seen_heads.append(head)
            break
        return idx

    def _pick_query(self) -> str:
        """挑一个搜索词。

        从配置里的 queries 随机挑；没配就返回空（调用方决定放弃）。

        不做站点发现 —— 见模块顶部合规说明。搜索词必须由用户提供，
        程序不会自己去找"哪里有免费小说"。
        """
        try:
            path = paths.project_root() / "config" / "book-sources.yaml"
            if not path.is_file():
                return ""
            import yaml

            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            queries = [
                str(x).strip() for x in (data.get("queries") or []) if str(x).strip()
            ]
            return random.choice(queries) if queries else ""
        except Exception:
            return ""

    async def read_post(self, search: Any, fetch: Any) -> Optional[BookChunk]:
        """读一条社区帖子。

        走现成的 web_search + web_fetch，不下载（帖子读完就没必要留）。

        search / fetch 是注入的协程函数：
            search(query: str, limit: int) -> list[dict]
            fetch(url: str) -> str
        这样 reading.py 不用知道工具层怎么实现的。
        """
        if not self.config.community_enabled:
            return None
        topics = self.config.community_topics or ["游戏杂谈"]
        topic = random.choice(topics)
        sites = self.config.community_sites or []
        hint = ""
        if sites:
            site = random.choice(sites)
            hint = str(site.get("query_hint") or "")
        query = f"{topic} {hint}".strip()

        try:
            results = await search(query, 5)
        except Exception as exc:
            log.info("网上阅读：社区搜索失败（%s）", type(exc).__name__)
            return None
        if not results:
            return None

        for item in results[:3]:
            url = str(item.get("url") or item.get("link") or "")
            title = str(item.get("title") or "")
            if not url:
                continue
            try:
                page = await fetch(url)
            except Exception:
                continue
            text = _html_to_text(page)
            score, why = body_score(text)
            if score <= 0:
                continue
            chunk = text[: self.config.chunk_chars]
            log.info("网上阅读：读帖子《%s》（%s，%d 字）", title[:30], why, len(chunk))
            return BookChunk(
                title=title or url,
                source="社区",
                text=chunk,
                kind="post",
                url=url,
                chars=len(chunk),
            )
        return None


def _decode_obfuscated_blocks(html: str) -> str:
    """解"base64 分段 + 随机函数名 writeln"的正文混淆（2026-10-31）。

    ## 为什么需要

    实测某站一个章节页的 HTML 里**一个字正文都没有**，全是：

        document.writeln(qryxkm.kagvx('PHA+5aSn56yU6Laj5bCP6K+0572RPC9wPg=='));
        document.writeln(qryxkm.kagvx('PHA+5pi+54S26ZmI6JCN6JCN55qE6K+d77yM...'));

    30 段 base64，每段解出来是一个 `<p>…</p>`。函数名（`qryxkm.kagvx`）
    **每页随机**，所以不能按名字匹配，只能匹配结构：

        任意标识符 . 任意标识符 ( 'base64' )

    ## 返回

    拼好的 HTML 片段（可能包含 `<p>` 标签）。**没找到就返回空串**，
    让调用方安静地走普通选择器路径 —— 绝大多数站没有这个混淆。
    """
    import base64

    if not html or "base64" not in html and "'" not in html:
        return ""

    # 结构匹配：xxx.yyy('BASE64')。不锚定函数名。
    pat = re.compile(
        r"[\w$]+\.\w+\(\s*'([A-Za-z0-9+/]{16,}={0,2})'\s*\)"
    )
    parts: list[str] = []
    for m in pat.finditer(html):
        b64 = m.group(1)
        try:
            frag = base64.b64decode(b64).decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001 —— 不是 base64 就跳过
            continue
        # 只收看起来像正文片段的（含中文），避免把统计代码也收进来
        if re.search(r"[\u4e00-\u9fff]", frag):
            parts.append(frag)

    if not parts:
        return ""
    # 至少要有几段，才值得当成"整章正文"
    if len(parts) < 3:
        return ""
    return "".join(parts)


def _html_to_text(html: str) -> str:
    """把网页转成纯文本。去脚本/样式/导航。

    社区帖子（小黑盒/B站专栏）用这个转。有些站把正文放在 <article>，
    有些只有 <body>，所以按优先级找。
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript", "nav", "header", "footer"]):
        tag.decompose()
    main = soup.find("article") or soup.find("main") or soup.body or soup
    return main.get_text("\n", strip=True)
