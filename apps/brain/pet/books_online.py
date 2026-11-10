"""起点 / 番茄：搜书、看简介、看目录、读试读章节。

## 用户要求（2026-11-10）

> 「现在我需要加一个新模块，能够帮我搜索分析小说的功能，帮我找到我喜欢的小说」
> 「起点的、番茄的」
> 「用起点官方网站，番茄官方网站，这样搜到书名可以免费看几章」
> 「搜索的频率降低」
> 「我可以登录网站给予账号登录」

## 边界（用户明确选定）

**只做公开可见的部分**：书名、作者、简介、标签、字数、目录、免费试读章节。

不做、也不试图绕过：
· 付费章节（起点的订阅章、番茄的 SVIP 章）
· 任何形式的付费墙绕过

这不是保守，是**做了也没用**：付费内容在服务端就不返回，
客户端怎么改都拿不到。而且那会违反站点条款。

## 两个站的关键差异（实测）

| | 番茄 | 起点 |
|---|---|---|
| 首页 | ✅ 200，146 KB | ❌ 202 WAF |
| 书籍页 | ✅ 200，229 KB | ❌ 202 WAF |
| 章节目录 | ✅ 全部在 HTML 里 | ❌ |
| 章节正文 | ⚠️ 免费章有，VIP 章空 | ❌ |
| 需要 | 无（或登录看更多） | **真实浏览器过 WAF** |

番茄是**服务端渲染**的，`urllib` 就能读。起点有 WAF（HTTP 202 +
`probe.js` 挑战），纯 HTTP 一律读不到，必须走主进程的 Chromium。

所以本模块的策略是「**先直连，失败才走浏览器**」——
番茄大多数请求根本不需要惊动浏览器（快），起点才用（慢但能过）。

## 频率限制（用户要求「搜索的频率降低」）

两个站都是正版站，爬太快会被封。所以：

· 同一时间只允许 **1 个**请求在飞（进程内互斥）
· 两次请求之间至少间隔 `MIN_INTERVAL_S`（默认 2.5 秒）
· 每轮搜索最多翻 `MAX_PAGES` 页（默认 1 页）

⚠️ 这些不是"性能妥协"，是**必须**的。盗版站封了换个域名就行，
   正版站封了就是封了 —— 而且会连带影响用户自己的账号。
"""
from __future__ import annotations

import asyncio
import gzip
import json
import logging
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Optional

log = logging.getLogger(__name__)

#: 两次请求之间至少间隔多少秒（用户要求「搜索的频率降低」）。
#:
#: 2.5 秒的选择：一次搜索通常要 1~3 个请求（搜索页 + 书籍页 + 章节页），
#: 连起来约 5~8 秒。这个速度用户不会觉得卡，对被搜站点也算克制。
MIN_INTERVAL_S = 2.5

#: 每轮搜索最多翻几页。1 页 = 20 本左右，足够。
MAX_PAGES = 1

#: 单次请求超时（秒）
TIMEOUT_S = 20.0

#: 试读最多读几章。
#:
#: ⚠️ 只读**免费章节**。番茄的 VIP 章在 HTML 里就是空的，
#:    起点要订阅。读到空就是到边界了，不去绕。
MAX_FREE_CHAPTERS = 5

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


@dataclass(slots=True)
class BookHit:
    """搜索到的一本书（**只有列表里能看到的元信息**）。"""

    site: str
    title: str
    author: str = ""
    book_id: str = ""
    url: str = ""
    intro: str = ""
    tags: list[str] = field(default_factory=list)
    words: int = 0
    status: str = ""
    cover: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "site": self.site,
            "title": self.title,
            "author": self.author,
            "book_id": self.book_id,
            "url": self.url,
            "intro": self.intro,
            "tags": list(self.tags),
            "words": self.words,
            "status": self.status,
        }


@dataclass(slots=True)
class Chapter:
    """一章（试读用）。"""

    index: int
    title: str
    url: str = ""
    chars: int = 0
    #: 从标题里解析出的章号（0 = 解析不出）。
    #:
    #: ⚠️ 单独一个字段，**不要塞进 `chars`** —— 一个是"第几章"，
    #:    一个是"多少字"，混用会在下游算出荒唐的"本章 351 字"。
    number: int = 0
    #: 正文。**空 = 读不到**（付费章 / 要登录）。
    text: str = ""

    @property
    def readable(self) -> bool:
        return bool(self.text.strip())


def _chapter_no(title: str) -> int:
    """从「第351章 xxx」里抠出 351。抠不出返回 0。

    ⚠️ 中文数字（「第一百章」）也认 —— 番茄有些老书用中文数字。
    """
    m = re.search(r"第\s*(\d+)\s*章", title)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            return 0
    # 中文数字：只处理常见的百/十以内 + 百位
    m = re.search(r"第\s*([零一二三四五六七八九十百千]+)\s*章", title)
    if not m:
        return 0
    cn = m.group(1)
    digits = {"零": 0, "一": 1, "二": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    total = 0
    section = 0
    number = 0
    for ch in cn:
        if ch in digits:
            number = digits[ch]
        elif ch == "十":
            section += (number or 1) * 10
            number = 0
        elif ch == "百":
            section += (number or 1) * 100
            number = 0
        elif ch == "千":
            section += (number or 1) * 1000
            number = 0
    total = section + number
    return total


# --------------------------------------------------------------------------- #
# 请求节流
# --------------------------------------------------------------------------- #

class _Throttle:
    """进程内节流：串行 + 固定间隔。

    ## 为什么用互斥而不是信号量

    用户要的是「搜索的频率降低」，不是"最多 N 路并发"。
    对正版站来说，**同时两个请求**和"一秒两个请求"一样不礼貌。
    所以干脆串行，间隔写死。
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def __aenter__(self) -> "_Throttle":
        await self._lock.acquire()
        gap = time.monotonic() - self._last
        if gap < MIN_INTERVAL_S:
            await asyncio.sleep(MIN_INTERVAL_S - gap)
        return self

    async def __aexit__(self, *exc: Any) -> None:
        self._last = time.monotonic()
        self._lock.release()


_throttle = _Throttle()


# --------------------------------------------------------------------------- #
# 抓取：先直连，失败才走浏览器
# --------------------------------------------------------------------------- #

def _fetch_direct(url: str) -> tuple[int, str]:
    """普通 HTTP 抓取。返回 `(状态码, 正文)`；失败抛异常。

    ⚠️ 起点会返回 **202 + 挑战页**（`probe.js`），那不是"成功"。
       所以 202 也算失败 —— 交给浏览器兜底。
    """
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": _UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip",
        },
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
        raw = resp.read()
        enc = (resp.headers.get("Content-Encoding") or "").lower()
        if "gzip" in enc:
            raw = gzip.decompress(raw)
        elif "deflate" in enc:
            import zlib

            raw = zlib.decompress(raw)
        text = raw.decode("utf-8", errors="replace")
        status = resp.status
    if status >= 400:
        raise RuntimeError(f"HTTP {status}")
    # 202 = WAF 挑战页（起点）
    if status == 202:
        raise RuntimeError("HTTP 202（WAF 挑战，需要真实浏览器）")
    return status, text


async def _fetch(url: str, *, prefer_browser: bool = False) -> str:
    """抓一个页面。**默认先直连，失败/被挑战才走浏览器**。

    ## 为什么这个顺序

    番茄直连就够（快，几十毫秒）；起点必须过 WAF（慢，15~25 秒）。

    反过来先走浏览器的话，番茄每次都要等十几秒 —— 用户会觉得卡。

    ## 🔴 `prefer_browser=True`：正文先试浏览器

    ⚠️ **但实测发现浏览器也解决不了字体混淆**（2026-11-10 二次验证）。

    第一版我以为 `innerText` 是"渲染后的文字"、字体加载后就正常了。
    **不对**。浏览器兜底返回 5.2 秒成功，可取回的文本里私用区字符
    仍占 **48%**（1861/3836）。

    原因：`innerText` 取的是**文本层**，不是字形。
    字形要正确显示，得靠 CSS `@font-face` 里那个 `woff2` 把码点映射过去；
    而 `innerText` 拿到的是**映射前**的原始码点。

    所以番茄正文的实际可用率只有 **32%**（实测第 1 章：
    常用汉字 1413 / 私用区 2244）。

    ## 现在的处理

    仍然走浏览器（起点必须要，而且它比直连多拿些内容），
    但**取回后检查私用区比例**，超过 30% 就判定"正文不可用"、返回空串。

    ⚠️ 返回空串而不是残缺文本，是为了让调用方（模型）知道
       "读不到"，从而**如实告诉用户**，而不是把一堆空白当成原文分析。
    """
    async with _throttle:
        if not prefer_browser:
            try:
                _status, text = await asyncio.to_thread(_fetch_direct, url)
                if text and len(text) > 2000:
                    log.debug("直连成功：%s（%d 字）", url[:60], len(text))
                    return text
                log.debug("直连内容太短（%d 字），改走浏览器：%s", len(text), url[:60])
            except Exception as exc:  # noqa: BLE001
                log.debug("直连失败（%s），改走浏览器：%s", exc, url[:60])

        # 走主进程的真实浏览器（过 WAF / 带登录态 / 解决字体混淆）
        from . import web as web_mod

        if not web_mod.browser_available():
            if prefer_browser:
                # 正文必须要浏览器 —— 拿不到就如实说，别返回一堆空白字符
                raise RuntimeError(
                    "读正文需要浏览器兜底，但主进程没连上。"
                    "（设置 → 联网 → 浏览器兜底要开着）"
                )
            raise RuntimeError("直连读不到，而且浏览器兜底没接上（主进程没连）")

        result = await web_mod.browser_fetch(url, timeout_s=45.0)
        if not result.get("ok"):
            raise RuntimeError(str(result.get("reason") or "浏览器读取失败"))
        text = str(result.get("text") or "")
        if not text:
            raise RuntimeError("浏览器读到了空页面")
        return text


# --------------------------------------------------------------------------- #
# 番茄
# --------------------------------------------------------------------------- #

#: 番茄的书号是 19 位数字（实测：/page/7111200276232539166）
_FQ_BOOK_RE = re.compile(r"/page/(\d{15,25})")
#: 番茄章节 id 也是 19 位
_FQ_READER_RE = re.compile(r"/reader/(\d{15,25})")


def _strip_tags(html: str) -> str:
    """粗暴去标签 + 压空白。够用（我们只要文字）。"""
    text = re.sub(r"<script[^>]*>.*?</script>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</p>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    # HTML 实体（`&#x27;` 这种番茄很常见）
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    text = text.replace("&lt;", "<").replace("&gt;", ">")
    text = text.replace("&quot;", '"').replace("&#x27;", "'").replace("&#39;", "'")
    text = re.sub(r"&#x([0-9a-fA-F]+);", lambda m: chr(int(m.group(1), 16)), text)
    text = re.sub(r"&#(\d+);", lambda m: chr(int(m.group(1))), text)
    text = re.sub(r"[ \t\u3000]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _find_json_state(html: str) -> dict[str, Any]:
    """从 `<script>window.__INITIAL_STATE__=...` 里抠出 JSON。

    番茄把书籍信息放在这里（服务端渲染）。抠不出来就当没有 —— 退回
    正则解析，不要让整个功能挂掉。
    """
    m = re.search(r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\})\s*;?\s*</script>", html, re.S)
    if not m:
        return {}
    try:
        data = json.loads(m.group(1))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


async def fanqie_search(keyword: str, limit: int = 8) -> list[BookHit]:
    """在番茄**首页 + 排行榜**里按书名筛。

    ## 🔴 番茄没有公开的搜索接口（实测，别再试）

    试过的所有路子：

        /search?word=xxx        -> 404
        /search?q=xxx           -> 404
        /api/author/search/...  -> 200 但**返回空**
        /api/search/book?query= -> 404

    它的搜索做在 APP 里（要登录 + 客户端签名）。网页端就是没有。

    ## 所以这里只能"在能看到的书里筛"

    抓 `/`（首页 23 本）+ `/rank`（榜 10 本），按书名/标签匹配关键词。

    ⚠️ 这意味着：**关键词不在这些书里就搜不到**。
       调用方必须如实告诉用户"没搜到"，而不是编一个书名。
       这是能力边界，不是 bug。

    ⚠️ 如果用户明确要"搜某本书"，而番茄搜不到 ——
       应该建议他：① 换起点搜 ② 直接在番茄 APP 里搜 ③ 提供准确书名让我直接开。
    """
    hits: list[BookHit] = []
    kw = keyword.strip()
    seen: set[str] = set()

    for page in ("https://fanqienovel.com/", "https://fanqienovel.com/rank"):
        try:
            html = await _fetch(page)
        except Exception as exc:  # noqa: BLE001
            log.info("番茄 %s 读不到：%s", page, exc)
            continue

        # 书名 + 链接。
        #
        # 🔴 实测首页卡片结构（2026-11-10）：
        #
        #     <a href="/page/<id>">
        #       <div class="muye-book-cover ...">
        #         <img class="book-cover-img" src="..." alt="部族荣光"/>
        #       ...
        #       <h3 class="title">部族荣光</h3>
        #       <span ...>丧狐</span>            ← 作者，在 title 之后
        #
        # ⚠️ 直接取 `<a>` 的文字会得到「部族荣光丧狐」（书名+作者粘一起）。
        #    要取 `<h3 class="title">`，或者图片的 alt。
        for m in re.finditer(
            r'<a[^>]+href="(/page/(\d{15,25}))"[^>]*>(.*?)</a>', html, re.S
        ):
            href, bid, inner = m.group(1), m.group(2), m.group(3)
            if bid in seen:
                continue

            # 书名：h3.title -> img[alt] -> 整个链接文字（最后手段，要切）
            title = ""
            tm = re.search(r'<h3[^>]*class="[^"]*title[^"]*"[^>]*>(.*?)</h3>', inner, re.S)
            if tm:
                title = _strip_tags(tm.group(1)).strip()
            if not title:
                tm = re.search(r'<img[^>]+alt="([^"]+)"', inner)
                if tm:
                    title = tm.group(1).strip()
            if not title:
                title = _strip_tags(inner).strip()
            if not title or len(title) < 2:
                continue
            # 按关键词筛（不区分大小写；没给关键词就当"列热门"）
            if kw and kw.lower() not in title.lower():
                continue
            seen.add(bid)
            hits.append(
                BookHit(
                    site="番茄",
                    title=title[:60],
                    book_id=bid,
                    url=f"https://fanqienovel.com{href}",
                )
            )
            if len(hits) >= limit:
                break
        if len(hits) >= limit:
            break

    log.info("番茄筛 %r -> %d 本（首页+榜，非真搜索）", keyword, len(hits))
    return hits


async def fanqie_hot(limit: int = 12) -> list[BookHit]:
    """番茄首页 + 排行榜的书（**不带关键词**）。

    给"推荐点好看的"这种需求用 —— 那才是番茄能提供的。
    """
    return await fanqie_search("", limit)


async def fanqie_book(book_id: str) -> tuple[BookHit, list[Chapter]]:
    """读番茄书籍页：书名/状态/标签/字数/简介 + 完整目录。

    ## 实测的页面结构（2026-11-10，别再猜）

        <div class="info">
          <div class="info-name"><h1>部族荣光</h1></div>
          <div class="info-label">
            <span class="info-label-yellow">已完结</span>
            <span class="info-label-grey">传统玄幻</span> ...
          </div>
          <div class="info-count">
            <div class="info-count-word">
              <span class="detail">286.5</span><span class="text">万字</span>
            </div>
          </div>
        </div>
        <div class="page-abstract-content">简介正文</div>

    ⚠️ 书名**必须取 `<h1>`**，不能用 `<title>` ——
       `<title>` 是「部族荣光丧狐完整版在线免费阅读_...」这种，
       中间会粘上作者名（实测「部族荣光丧狐」）。
    """
    url = f"https://fanqienovel.com/page/{book_id}"
    html = await _fetch(url)

    # ── 书名：优先 h1（干净），退回 title（要切干净）──
    title = ""
    m = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.S)
    if m:
        title = _strip_tags(m.group(1)).strip()
    if not title:
        m = re.search(r"<title>([^<]*)</title>", html)
        if m:
            # 「书名作者名完整版在线免费阅读_...」-> 只留第一段
            title = m.group(1).split("完整版")[0].split("在线免费阅读")[0]
            title = re.split(r"[_|]", title)[0].strip()

    # ── 状态 + 标签 ──
    status = ""
    tags: list[str] = []
    m = re.search(r"<div[^>]*class=\"[^\"]*info-label[^\"]*\"[^>]*>(.*?)</div>", html, re.S)
    if m:
        block = m.group(1)
        for sm in re.finditer(r'<span[^>]*class="([^"]*)"[^>]*>(.*?)</span>', block, re.S):
            cls, txt = sm.group(1), _strip_tags(sm.group(2)).strip()
            if not txt:
                continue
            if "yellow" in cls:
                status = txt
            else:
                tags.append(txt)
        tags = tags[:8]

    # ── 字数（「286.5」+「万字」）──
    words = 0
    m = re.search(
        r'<span class="detail">([\d.]+)</span><span class="text">([^<]*)</span>', html
    )
    if m:
        try:
            num = float(m.group(1))
            unit = m.group(2).strip()
            if "万" in unit:
                num *= 10000
            elif "亿" in unit:
                num *= 100000000
            words = int(num)
        except (TypeError, ValueError):
            words = 0

    # ── 简介 ──
    intro = ""
    m = re.search(
        r'<div[^>]*class="[^"]*page-abstract-content[^"]*"[^>]*>(.*?)</div>', html, re.S
    )
    if m:
        intro = _strip_tags(m.group(1))[:400]

    # ── 目录 ──
    #
    # 🔴 实测：章节标题**就在 <a> 的文字里**：
    #
    #     <a href="/reader/7069952706521236001" ...>第1章 部族生死战</a>
    #     <a href="/reader/7535344092251685400" ...>最近更新：第351章 ...</a>
    #
    # ⚠️ 第一个链接是「最近更新」（第 351 章），**不是第 1 章**。
    #    按页面顺序取会把目录取反，所以要从标题里解析章号排序。
    chapters: list[Chapter] = []
    seen: set[str] = set()
    for m in re.finditer(
        r'<a[^>]+href="(/reader/(\d{15,25}))"[^>]*>(.*?)</a>', html, re.S
    ):
        href, cid, inner = m.group(1), m.group(2), m.group(3)
        if cid in seen:
            continue
        seen.add(cid)
        raw_title = _strip_tags(inner).strip()
        # 去掉「最近更新：」前缀
        raw_title = re.sub(r"^最近更新[：:]\s*", "", raw_title)
        if not raw_title:
            continue
        chapters.append(
            Chapter(
                index=0,  # 稍后按章号填
                title=raw_title[:80],
                url=f"https://fanqienovel.com{href}",
                number=_chapter_no(raw_title),
            )
        )

    # 按章号排序（解析不出章号的排最后，保持原顺序）
    chapters.sort(key=lambda c: (c.number if c.number > 0 else 10**9))
    for i, c in enumerate(chapters, 1):
        c.index = i

    book = BookHit(
        site="番茄",
        title=title or book_id,
        book_id=book_id,
        url=url,
        intro=intro,
        tags=tags,
        words=words,
        status=status,
    )
    log.info(
        "番茄书籍 %s -> 《%s》%d 章 (%s, %.1f 万字)",
        book_id, book.title, len(chapters), status or "?", words / 10000,
    )
    return book, chapters


async def fanqie_chapter(chapter_url: str) -> str:
    """读番茄一章正文。**读不到就返回空串**。

    ## 🔴 两个真实限制（实测，不是 bug）

    ### ① VIP 章在 HTML 里就是空的

    章节页有正文容器，但里面没有字。页面原话：

        扫码下载APP免费读，SVIP网页畅读
        会员登录后，可在网页畅读全文

    这是**服务端不返回**，客户端改不了。

    ### ② 免费章有**字体混淆**，会缺字

    实测第 1 章 4223 字能拿到，但部分汉字是**自定义字体**里的码点：

        @font-face{font-family:DNMrHsV173Pd4pgy;
          src:url(https://lf6-awef.bytetos.com/.../dc027189e0ba4cd.woff2)}

    那些字形只在 `woff2` 里，纯文本取出来就是空。

        原文「乾皇朝，疆域亿**里**」-> 取到「乾皇朝，疆域亿」

    这是**有意的反爬**。破解它要下载并映射字体表 —— 那违反站点条款，
    所以**不做**。能读到大部分已经够判断"这本书好不好看"。

    ⚠️ 调用方要知道正文可能不规则，别把它当精确原文引用。
    """
    try:
        html = await _fetch(chapter_url, prefer_browser=True)
    except Exception as exc:  # noqa: BLE001
        log.info("番茄章节读不到：%s（%s）", chapter_url[-24:], exc)
        return ""

    # ⚠️ 走浏览器时拿到的是 `innerText`（**纯文本，没有 HTML 标签**），
    #    所以不能再用正则找 `<div class="muye-reader-content">`。
    #    两条路的结果形态不同，要分别处理。

    body = ""
    if "<div" in html or "<p>" in html:
        # 直连路径：还是 HTML
        m = re.search(
            r'muye-reader-content\s+noselect"[^>]*>(.*?)</div>\s*<div', html, re.S
        )
        if m:
            body = _strip_tags(m.group(1))
    else:
        # 浏览器路径：已经是纯文本。正文在「本章字数：」之后、
        # 「扫码下载APP」之类推广之前。
        text = html
        # 去掉页面头部的标题/字数行
        text = re.sub(r"^[\s\S]{0,200}?本章字数[：:]\s*\d+\s*字?", "", text, count=1)
        # 去掉尾部的推广
        for tail in ("扫码下载", "番茄小说APP", "下载番茄", "上一章", "目录"):
            idx = text.find(tail)
            if idx > 500:
                text = text[:idx]
        body = text.strip()

    # 去掉残留的导航/推广行
    lines = []
    for line in body.splitlines():
        s = line.strip()
        if not s:
            continue
        if re.match(r"^(上一章|下一章|目录|加入书架|投推荐票|举报|手机版|电脑版)$", s):
            continue
        if "本章字数" in s or "更新时间" in s:
            continue
        lines.append(s)
    body = "\n".join(lines)

    # 私用区字符还是太多 -> 说明没走成浏览器，如实告诉调用方
    priv = sum(1 for ch in body if 0xE000 <= ord(ch) <= 0xF8FF)
    if body and priv > len(body) * 0.3:
        log.info(
            "番茄章节字体混淆严重（私用区 %d/%d），正文不可用",
            priv, len(body),
        )
        return ""

    return body.strip()[:6000]


# --------------------------------------------------------------------------- #
# 起点
# --------------------------------------------------------------------------- #

_QD_BOOK_RE = re.compile(r"/book/(\d{4,12})")


async def qidian_search(keyword: str, limit: int = 8) -> list[BookHit]:
    """搜起点的书。

    ⚠️ 起点有 WAF，这一路**必然**走浏览器（15~25 秒）。
       所以调用方要能接受慢。
    """
    url = f"https://www.qidian.com/so/{urllib.parse.quote(keyword)}.html"
    hits: list[BookHit] = []
    try:
        html = await _fetch(url)
    except Exception as exc:  # noqa: BLE001
        log.info("起点搜索读不到：%s", exc)
        return hits

    # 起点搜索结果是服务端渲染的，书名在 h3/a 里
    for m in re.finditer(r'<h3[^>]*>\s*<a[^>]+href="(//book\.qidian\.com/book/(\d+)/?)"[^>]*>(.*?)</a>', html, re.S):
        href, bid, inner = m.group(1), m.group(2), m.group(3)
        title = _strip_tags(inner).strip()
        if not title:
            continue
        if any(h.book_id == bid for h in hits):
            continue
        hits.append(
            BookHit(
                site="起点",
                title=title[:60],
                book_id=bid,
                url="https:" + href if href.startswith("//") else href,
            )
        )
        if len(hits) >= limit:
            break

    log.info("起点搜索 %r -> %d 本", keyword, len(hits))
    return hits


async def qidian_book(book_id: str) -> tuple[BookHit, list[Chapter]]:
    """读起点书籍页：简介 + 目录。

    ⚠️ 同样必然走浏览器。
    """
    url = f"https://book.qidian.com/info/{book_id}/"
    html = await _fetch(url)

    title = ""
    m = re.search(r"<title>([^<]*)</title>", html)
    if m:
        title = m.group(1).split("_")[0].split("-")[0].strip()

    intro = ""
    m = re.search(r'<p[^>]*class="[^"]*intro[^"]*"[^>]*>(.*?)</p>', html, re.S)
    if m:
        intro = _strip_tags(m.group(1))[:300]

    # 目录在 #j-catalogList 里，章节链接是 /chapter/<bookid>/<chapterid>/
    chapters: list[Chapter] = []
    seen: set[str] = set()
    for m in re.finditer(r'href="(//read\.qidian\.com/chapter/(\d+)/(\d+)/?)"', html):
        href, _bid, cid = m.group(1), m.group(2), m.group(3)
        if cid in seen:
            continue
        seen.add(cid)
        chapters.append(
            Chapter(
                index=len(chapters) + 1,
                title=f"第{len(chapters)+1}章",
                url="https:" + href if href.startswith("//") else href,
            )
        )

    book = BookHit(site="起点", title=title or book_id, book_id=book_id, url=url, intro=intro)
    log.info("起点书籍 %s -> %r，%d 章", book_id, book.title, len(chapters))
    return book, chapters


async def qidian_chapter(chapter_url: str) -> str:
    """读起点一章正文。读不到返回空串（未订阅 / 要登录）。"""
    try:
        html = await _fetch(chapter_url)
    except Exception as exc:  # noqa: BLE001
        log.info("起点章节读不到：%s（%s）", chapter_url[-24:], exc)
        return ""
    m = re.search(r'<div[^>]*class="[^"]*read-content[^"]*"[^>]*>(.*?)</div>', html, re.S)
    body = ""
    if m:
        body = _strip_tags(m.group(1))
    if not body:
        ps = re.findall(r"<p[^>]*>(.*?)</p>", html, re.S)
        body = "\n".join(_strip_tags(p) for p in ps)
    return body.strip()[:6000]


# --------------------------------------------------------------------------- #
# 对外的统一入口
# --------------------------------------------------------------------------- #

async def search(keyword: str, site: str = "both", limit: int = 8) -> list[BookHit]:
    """搜书。`site` 取 `fanqie` / `qidian` / `both`。

    ⚠️ `both` 时**先番茄后起点**：番茄快（直连），起点慢（过 WAF）。
       先出快的，用户能早点看到东西。
    """
    out: list[BookHit] = []
    site = (site or "both").lower()
    if site in ("both", "fanqie", "番茄"):
        out.extend(await fanqie_search(keyword, limit))
    if site in ("both", "qidian", "起点") and len(out) < limit:
        try:
            out.extend(await qidian_search(keyword, limit - len(out)))
        except Exception:  # noqa: BLE001
            log.info("起点搜索失败（已忽略）", exc_info=True)
    return out[:limit]


async def open_book(site: str, book_id: str) -> tuple[BookHit, list[Chapter]]:
    """按站点开一本书，返回 (书信息, 目录)。"""
    if "起点" in site or "qidian" in site.lower():
        return await qidian_book(book_id)
    return await fanqie_book(book_id)


async def read_chapter(site: str, chapter_url: str) -> str:
    if "起点" in site or "qidian" in site.lower():
        return await qidian_chapter(chapter_url)
    return await fanqie_chapter(chapter_url)
