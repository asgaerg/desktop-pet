"""
联网搜索 + 读网页（2026-10-12）。

## 为什么有这个模块

用户要求给宠物加「联网功能」：「让他自己去网上搜索，就像小鲸你一样，
自己调用，自己修改……开启之后可以自由的搜索相关信息」。

实测确认了两件硬事实（`docs/进度交接.md` 第七十九节）：

  · **模型支持函数调用**（`deepseek-flash` 一次就发了两个 `web_search` 调用）
  · **模型自己不能联网**（答"无法确认"；它的知识停在 2024-06，而现在是 2026-10）

所以"联网"必须由我们提供工具，模型只负责决定**搜什么**。

## 后端选型（实测出来的，不是偏好）

| 后端 | 搜索 | 真实 URL | 正文可抓 | 结论 |
|---|---|---|---|---|
| **Bing（cn）** | ✅ 10 条 | ✅ 直接给 | ✅ 4/4 | **主** |
| 360 | ✅ 11 条 | ❌ 跳转跟不到 | ❌ | **备**（只给标题）|
| DuckDuckGo | ❌ **TCP 超时** | — | — | 弃用 |

DDG 是 DNS 通、**TCP 握手就被拦**（不是限流、不是验证码）——
`probe-search-backends.py`（历史实测脚本）有分层诊断。

## ⚠️ 解析 HTML 是**脆**的，所以设计上必须容错

搜索引擎随时改版，改版后的表现是**静默返回 0 条**（不报错）。
所以：

  · 两个后端**互为备份**，一个 0 条就切另一个
  · 每个后端的结果数与解析规则分开，便于单独修
  · **失败要能上抛**，让模型知道"我搜不到"而不是让它编一个答案

## 编码必须自动识别

实测：中关村在线是 **GBK**，硬用 UTF-8 解会全是乱码（中文站的经典坑）。
`fetch()` 按 响应头 -> HTML meta -> 常见中文编码 依次尝试，
并用"替换字符数量"判断解对没有。

## 三类页面要显式处理（实测都能识别，不是静默失败）

  · **403**（知乎）—— 明确拒绝
  · **PDF** —— 按 Content-Type 拦掉，不能把二进制喂给模型
  · **纯 JS 站**（小红书）—— 只读到页脚，正文太短 -> 判为"读不到"
"""

from __future__ import annotations

import asyncio
import gzip
import html as html_module
import io
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

log = logging.getLogger("pet.web")

"""
## 🔴 浏览器兜底（2026-10-13 用户要求）

### 它解决什么

有些站用**要执行 JS 的 WAF 挑战**挡住抓取，最典型是起点中文网：

```
HTTP 202
set-cookie: x-waf-captcha-referer=
正文只有：<script src="/C2WF946J0/probe.js"></script>
```

纯 HTTP 客户端（本模块用的 urllib）**过不去**，但 Chromium 能过 ——
而这个项目本来就是 Electron，所以主进程开个**隐藏窗口**就能读
（实测 `probe-qidian-browser.py`（历史实测脚本）拿到了 22,161 字正文）。

### ⚠️ 为什么由**主进程**做，而不是这里装个浏览器

① 我们本来就是 Electron（`new BrowserWindow()` 就行）
② Python 那边要装 Playwright + 一个 200MB 的 Chromium
③ **隐私口径**：走浏览器等于"以用户的身份浏览"（带真实 UA、留 cookie），
   所以必须由用户显式在设置里打开（`tools.browser_fallback`，**默认关**）。

### ⚠️ 这里只做「转发」，不做「决定」

本模块**不认识 Gateway**（保持单向依赖，和 `voice.py` 一样）——
所以用**注入**的方式拿到"发消息给主进程"的函数，见 `attach_browser()`。

⚠️ 而且**开关的判定不在这里** —— 这里只负责把请求转过去。
主进程那边会**再判一次**（隐私开关多判一次的成本是零，漏判的代价很大）。

## 浏览器兜底：注入 + 请求-等回执

⚠️ 这一段是**模块级的**（不是类）—— 和 `voice.py` 的 `attach(send)`
同一个模式：模块不认识 Gateway，靠注入拿到发送能力。
"""

""" 把一条消息发给主进程。由 `attach_browser()` 注入。"""
_browser_send: Optional[Callable[[dict[str, Any]], Any]] = None

""" requestId -> 等待中的 Future。"""
_browser_pending: dict[str, "asyncio.Future[dict[str, Any]]"] = {}

""" requestId 计数器（不用随机数：日志里递增更易读）。"""
_browser_seq = 0

"""
 * 注入发送函数。`Gateway.attach()` 时调用。
 *
 * ⚠️ 用注入而不是让本模块 import Gateway：那会形成**循环依赖**
 * （gateway 已经 import 了 web），而且这条单向依赖是 `voice.py`
 * 立下的规矩，这里跟着走。
"""
def attach_browser(send: Callable[[dict[str, Any]], Any]) -> None:
    global _browser_send
    _browser_send = send


""" 有没有接上主进程。接不上就老实说"读不了"，而不是抛异常炸掉对话。"""
def browser_available() -> bool:
    return _browser_send is not None


"""
 * 主进程回了 `web.browser.result` -> 唤醒对应的等待者。
 *
 * 返回**是否有人认领** —— 没人认领说明是过期回执（超时之后才到的），
 * 那种情况只该记日志，不该当成错误。
"""
def resolve_browser_result(request_id: str, payload: dict[str, Any]) -> bool:
    future = _browser_pending.pop(str(request_id or ""), None)
    if future is None or future.done():
        return False
    future.set_result(payload)
    return True


"""
 * 请主进程用**隐藏浏览器**读一个页面。
 *
 * ⚠️ **失败也返回 dict（带 `reason`），不抛异常。**
 * 抛异常会把整条工具调用链炸掉，而"读不了"本来是一种正常结果 ——
 * 调用方据此回退到普通抓取，或者告诉用户"这个站读不了"。
"""
async def browser_fetch(
    url: str,
    timeout_s: float = 30.0,
) -> dict[str, Any]:
    global _browser_seq
    if _browser_send is None:
        return {"ok": False, "reason": "没有连上主进程，用不了浏览器兜底"}

    _browser_seq += 1
    request_id = f"wb{_browser_seq}"
    loop = asyncio.get_running_loop()
    future: "asyncio.Future[dict[str, Any]]" = loop.create_future()
    _browser_pending[request_id] = future

    request = {
        "type": "web.browser.fetch",
        "url": url,
        # ⚠️ 字段名必须是 requestId（驼峰）—— 主进程的
        #    `requestFromBrain` 从 `obj["requestId"]` 取。叫 reqId 会永远超时。
        "requestId": request_id,
        # 主进程侧的超时要**比整体超时短**，留出"回执走完 IPC"的时间。
        #
        # ⚠️ 原来写的是 `int(max(5.0, timeout_s - 3.0) * 1000)` ——
        #    那个 `max(5.0, ...)` 的**下限**会让短超时失效：整体设 5 秒时
        #    它也变成 5 秒，两边**同时到点**，于是浏览器的真实失败原因
        #    （"页面几乎是空的" / "反爬没过"）永远拿不到，
        #    用户只看到笼统的"超时"。
        #    **这个 bug 是 `tools/test-browser-bridge.py` 抓到的。**
        "timeoutMs": max(500, int(timeout_s * 1000) - 2000),
    }

    log.info("浏览器兜底请求 %s：%s", request_id, url[:80])
    try:
        sent = _browser_send(request)
        # ⚠️ sender 可能是协程（gateway 的 broadcast 是 async）——
        #    照 `voice.py` 的做法：是协程就 await。不 await 会报
        #    "coroutine was never awaited"，消息根本发不出去。
        if asyncio.iscoroutine(sent):
            await sent
        return await asyncio.wait_for(future, timeout=timeout_s)
    except asyncio.TimeoutError:
        log.info("浏览器兜底超时（%s，%.0fs）", request_id, timeout_s)
        return {"ok": False, "reason": f"浏览器兜底超时（{timeout_s:.0f} 秒）"}
    except Exception as error:  # noqa: BLE001
        log.info("浏览器兜底出错（%s）：%s", request_id, error)
        return {"ok": False, "reason": f"浏览器兜底出错：{error}"}
    finally:
        # ⚠️ 一定要清 —— 不清的话超时过的 requestId 会一直留在字典里
        _browser_pending.pop(request_id, None)

# 浏览器 UA。⚠️ 实测很多搜索端点对 python-urllib 的默认 UA 直接返 403。
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# 单次请求超时。搜索/抓取都在生成回复的路径上，不能让用户等太久。
#
# 🔴 2026-10-15：12.0 -> 20.0（加了 DuckDuckGo 主引擎之后必须加长）。
#
# 实测（走系统代理时）各引擎的**真实**延迟：
#
# ```
# DuckDuckGo   中位 15.4 秒（13.6~16.2，6 个查询）
# Bing           ~1.0 秒（直连，快但中文结果常崩）
# ```
#
# ⚠️ 12 秒会把 DDG **全部掐死** —— 表现是"换了主引擎但一条都搜不到"，
# 而日志里只有一句含糊的超时。这类"超时值比实际延迟短"的 bug 很难查，
# 因为代码看起来完全正常。
#
# ⚠️ 这个值只影响**单次请求**；`search()` 是逐个引擎串行尝试的，
# 所以最坏情况的总耗时是「引擎数 × 这个值」。DDG 失败时退到 Bing 很快，
# 所以实际不会等 40 秒。
DEFAULT_TIMEOUT_S = 20.0

# 正文截断长度。喂给模型的文本不是越长越好 —— 太长的网页会挤掉对话历史，
# 而且模型对超长上下文的中间部分本来就不敏感。
MAX_CONTENT_CHARS = 4000


@dataclass(slots=True)
class SearchHit:
    """一条搜索结果。"""

    title: str
    url: str
    snippet: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"title": self.title, "url": self.url, "snippet": self.snippet}


@dataclass(slots=True)
class FetchResult:
    """一次网页抓取的结果。"""

    ok: bool
    url: str
    title: str = ""
    text: str = ""
    # 失败原因，给模型看的（它据此决定换个链接还是放弃）
    reason: str = ""
    # 实际用的编码（排查乱码时有用）
    encoding: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "url": self.url,
            "title": self.title,
            "text": self.text,
            "reason": self.reason,
        }


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #


def fetch_raw(
    url: str,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> tuple[int, bytes, dict[str, str]]:
    """发一次 GET，返回 `(状态码, 原始字节, 响应头)`。

    ⚠️ **不抛异常** —— 网络错误统一变成状态码 `-1` + 错误文本。
    调用方（搜索/抓取）要能区分"没搜到"和"网络不通"，所以错误必须变成数据。

    gzip/deflate 在这里解掉：不少站点只认 `Accept-Encoding`，不回压缩就会
    发未压缩内容（反而更慢），回了就必须自己解。
    """
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": _UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            headers = {k.lower(): v for k, v in response.headers.items()}
            encoding = headers.get("content-encoding", "").lower()
            if "gzip" in encoding:
                try:
                    raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
                except OSError:
                    pass  # 解不开就用原样，让上层去报"读不到"
            elif "deflate" in encoding:
                try:
                    raw = zlib.decompress(raw, -zlib.MAX_WBITS)
                except zlib.error:
                    pass
            return int(response.status), raw, headers
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read()[:2000], {}
    except Exception as exc:  # noqa: BLE001 —— 网络错误一律当数据
        return -1, f"{type(exc).__name__}: {exc}".encode(), {}


def decode_bytes(raw: bytes, headers: dict[str, str]) -> tuple[str, str]:
    """把响应字节解成文本，返回 `(文本, 用了哪个编码)`。

    ## 顺序：响应头 -> HTML meta -> 常见中文编码

    中文站有相当一部分还是 GBK。硬用 UTF-8 解不会抛异常（有 `errors=replace`），
    但会得到满屏 `\ufffd` —— 那种内容喂给模型比不喂还糟。

    判据是**替换字符的数量**：解对了几乎没有，解错了成百上千。
    """
    content_type = headers.get("content-type", "")
    candidates: list[str] = []
    match = re.search(r"charset=([\w-]+)", content_type, re.I)
    if match:
        candidates.append(match.group(1))
    head = raw[:4096].decode("ascii", "ignore")
    match = re.search(r'charset=["\']?([\w-]+)', head, re.I)
    if match:
        candidates.append(match.group(1))
    candidates += ["utf-8", "gb18030", "gbk", "big5"]

    for encoding in candidates:
        try:
            text = raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
        bad = text.count("\ufffd")
        # 阈值随长度放宽：长文本偶尔一两个坏字节是正常的
        if bad <= max(2, len(text) // 2000):
            return text, encoding
    return raw.decode("utf-8", "replace"), "utf-8(replace)"


def strip_html(body: str) -> str:
    """极简正文提取：去脚本/样式/标签，压缩空白。

    ⚠️ 刻意保持"笨"：装 readability/lxml 会引入依赖，而实测这个程度
    已经够用（`probe-web-fetch-quality.py`（历史实测脚本）里 4/4 站点读出了实质正文）。

    保留段落换行 —— 模型对"分段的文本"理解明显好于"一大坨"。
    """
    text = re.sub(r"(?is)<(script|style|noscript|svg|head)[^>]*>.*?</\1>", " ", body)
    text = re.sub(r"(?is)<!--.*?-->", " ", text)
    text = re.sub(r"(?i)</(p|div|li|h[1-6]|tr|br)[^>]*>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_module.unescape(text)
    text = re.sub(r"[ \t\u00a0]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


def _extract_title(body: str) -> str:
    match = re.search(r"(?is)<title[^>]*>(.*?)</title>", body)
    if not match:
        return ""
    return html_module.unescape(re.sub(r"\s+", " ", match.group(1))).strip()[:200]


# --------------------------------------------------------------------------- #
# 搜索结果解析
# --------------------------------------------------------------------------- #

# Bing 的结果容器 class。⚠️ 改版会变 —— 所以有 `search()` 里的"0 条就换后端"。
_BING_ITEM = re.compile(r'<li[^>]*class="[^"]*b_algo[^"]*"[^>]*>(.*?)</li>', re.S | re.I)
# 360 的结果容器
_SO_ITEM = re.compile(r'<li[^>]*class="[^"]*res-list[^"]*"[^>]*>(.*?)</li>', re.S | re.I)

"""
DuckDuckGo 的结果容器（2026-10-15 加）。

⚠️ 用 `html.duckduckgo.com/html/` 那个**无 JS** 的版本（不是 `duckduckgo.com`）。
它的结果块长这样：

```html
<div class="result results_links results_links_deep web-result">
  <div class="links_main links_deep result__body">
    <h2 class="result__title">
      <a class="result__a" href="//duckduckgo.com/l/?uddg=<URL编码的真实地址>&rut=...">标题</a>
    </h2>
    <a class="result__url" href="...">www.example.com/path/</a>
    <a class="result__snippet" href="...">摘要文字</a>
```

⚠️ 按 `web-result` 切块，**不能按 `</div>`** —— 嵌套很深，非贪婪匹配会切错。
"""
_DDG_ITEM = re.compile(
    r'<div[^>]*class="[^"]*web-result[^"]*"[^>]*>(.*?)(?=<div[^>]*class="[^"]*web-result|$)',
    re.S | re.I,
)
# 标题链接
_DDG_TITLE = re.compile(r'<a[^>]*class="[^"]*result__a[^"]*"[^>]*>(.*?)</a>', re.S | re.I)
# 摘要
_DDG_SNIPPET = re.compile(
    r'<a[^>]*class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</a>', re.S | re.I
)
# 显示用的域名（`www.example.com/path/`）
_DDG_URLTEXT = re.compile(
    r'<a[^>]*class="[^"]*result__url[^"]*"[^>]*>(.*?)</a>', re.S | re.I
)
# 跳转包装里的真实地址：`?uddg=<URL编码>`
_DDG_UDDG = re.compile(r'[?&]uddg=([^&"]+)')


def _ddg_real_url(href: str) -> str:
    """把 DuckDuckGo 的跳转包装还原成真实地址。

    ## 🔴 必须还原（2026-10-15）

    DDG 的 `result__a` 里 `href` **不是真地址**：

    ```
    //duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.shubenshu.com%2FlCgr%2F&rut=8e8cae...
    ```

    直接把 `uddg` 参数里的 URL 解码出来。

    ⚠️ **不还原的后果比"地址难看"严重得多**：模型会拿这个包装地址去
    `web_fetch`，而那个地址需要 JS/cookie 才能跳转 ——
    于是"搜到了却读不出内容"，且原因看不出来。

    ⚠️ 兼容两种形状：
      · `//duckduckgo.com/l/?uddg=...`   协议相对
      · `/l/?uddg=...`                   相对
      · 罕见的直接真地址（新版本偶尔给）   -> 原样返回
    """
    if not href:
        return ""
    match = _DDG_UDDG.search(href)
    if match:
        real = urllib.parse.unquote(match.group(1))
        if real.startswith(("http://", "https://")):
            return real
    if href.startswith("//"):
        return "https:" + href
    return href


def _first_href(chunk: str) -> str:
    match = re.search(r'href="([^"]+)"', chunk)
    return html_module.unescape(match.group(1)) if match else ""


def _clean_text(chunk: str, limit: int = 300) -> str:
    return strip_html(chunk).replace("\n", " ")[:limit]


def parse_bing(body: str, limit: int = 8) -> list[SearchHit]:
    """从 Bing 结果页解析。实测给的是**真实 URL**（不用跟跳转）。"""
    hits: list[SearchHit] = []
    for chunk in _BING_ITEM.findall(body):
        url = _first_href(chunk)
        if not url.startswith("http"):
            continue
        # 标题在 <h2> 里
        title_match = re.search(r"(?is)<h2[^>]*>(.*?)</h2>", chunk)
        title = _clean_text(title_match.group(1), 160) if title_match else ""
        if not title:
            continue
        # 摘要：h2 之后剩下的文字
        rest = chunk[title_match.end():] if title_match else chunk
        hits.append(SearchHit(title=title, url=url, snippet=_clean_text(rest)))
        if len(hits) >= limit:
            break
    return hits


def parse_ddg(body: str, limit: int = 8) -> list[SearchHit]:
    """从 DuckDuckGo 的 html 版结果页解析（2026-10-15 加）。

    ## 🔴 为什么加这个引擎

    用户报「联网搜索搜不出东西」。查实是 **Bing 对中文查询经常返回完全无关的结果**：

    ```
    搜索 '天官赐邪 小说'（Bing 回显查询词正确、HTTP 200、100KB、6 条）
    -> 结果：汉典「天」的字典解释
             「天 的意思, 天 的解释, 天 的拼音, 天 的部首…」
    ```

    ⚠️ **这种失败是静默的** —— 状态码、字节数、条数全都正常，
    只有"内容不对"。所以代码和肉眼都看不出异常，模型拿到垃圾硬答。

    （顺带修正一条旧记录：§97 曾把模型对此的描述判为"幻觉"，
    其实模型描述的是**真实返回的结果**，是搜索本身坏了。）

    ## 实测对比（2026-10-15，走系统代理）

    ```
    查询               Bing            DuckDuckGo
    '天官赐邪'           ✅              ✅
    '天官赐邪 作者'       ❌「天」字典页     ✅
    '幻梦猎人 新书'       ❌ Britannica美学  ✅ 起点作者页
    '银松镇 游戏'         ✅              ✅
    ```

    DDG 在**所有**测试查询上都给出了相关结果，Bing 在带第二个词的
    中文查询上经常崩。所以 DDG 当主引擎。

    ⚠️ 代价是**慢**：中位 15.4 秒（Bing 约 1 秒）。这也是
    `DEFAULT_TIMEOUT_S` 从 12 提到 20 的原因。
    """
    hits: list[SearchHit] = []
    for chunk in _DDG_ITEM.findall(body):
        title_match = _DDG_TITLE.search(chunk)
        if not title_match:
            continue
        title = _clean_text(title_match.group(1), 160)
        if not title:
            continue

        # 真实地址要从跳转包装里还原（见 `_ddg_real_url`）
        href = _first_href(title_match.group(0))
        url = _ddg_real_url(href)
        if not url.startswith("http"):
            continue

        snippet_match = _DDG_SNIPPET.search(chunk)
        snippet = _clean_text(snippet_match.group(1)) if snippet_match else ""
        if not snippet:
            # 有些块没有 result__snippet，退回 result__url 的显示文本
            url_match = _DDG_URLTEXT.search(chunk)
            snippet = _clean_text(url_match.group(1), 120) if url_match else ""

        hits.append(SearchHit(title=title, url=url, snippet=snippet))
        if len(hits) >= limit:
            break
    return hits


def parse_360(body: str, limit: int = 8) -> list[SearchHit]:
    """从 360 结果页解析。

    ⚠️ 360 给的是 `so.com/link?m=...` 跳转包装，而实测**跟不到真实地址**
    （要 JS）。所以这里保留包装 URL：模型点不开，但**标题**有参考价值。
    这也是为什么 360 只当备用、Bing 当主。
    """
    hits: list[SearchHit] = []
    for chunk in _SO_ITEM.findall(body):
        title_match = re.search(r"(?is)<h3[^>]*>(.*?)</h3>", chunk)
        if not title_match:
            continue
        title = _clean_text(title_match.group(1), 160)
        if not title or title == "其他人还搜了":
            continue  # 那是"相关搜索"，不是结果
        url = _first_href(title_match.group(0)) or _first_href(chunk)
        hits.append(SearchHit(title=title, url=html_module.unescape(url), snippet=""))
        if len(hits) >= limit:
            break
    return hits


# --------------------------------------------------------------------------- #
# 搜索入口
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class SearchOutcome:
    """
    一次搜索的完整结果。

    ## 🔴 `query_ignored`：查询**没被理解**（2026-10-12 新加的判据）

    ### 为什么需要它

    搜索的失败模式是**静默的**：返回 HTTP 200、返回 6 条结果，
    **看起来成功了**，只有内容不对（比如全是字典页）。

    模型看到「6 条结果」会以为搜到了，然后拿垃圾内容硬答。

    ### 判据：换个词但结果**一模一样**

    实测（`probe-dup-detect.py`，历史实测脚本）：

    ```
    '幻梦猎人'       -> 8 条
    '幻梦猎人 新书'   -> 相同=True  重叠=8/8
    '幻梦猎人 小说'   -> 相同=True  重叠=8/8
    '幻梦猎人 作品'   -> 相同=True  重叠=8/8
    ```

    Bing 把「幻梦猎人」拆成了「幻梦」，于是**换了词结果不变** ——
    也就是搜索引擎**根本没把这个查询当查询**。

    ### ⚠️ 为什么这个判据比「看结果是字典页」好

    「结果是字典页」要靠**猜关键词黑名单**（汉典 / 百度百科…），
    而黑名单永远不全。而「结果没变」是**结构性**的 ——
    它直接说明查询**没有影响力**，与具体站点无关。

    ### 怎么用

    由调用方（`tools._web_search`）和**上一次**的结果集对比；
    `web.py` 自己不做对比（它无状态）。
    """

    hits: list[SearchHit] = field(default_factory=list)
    engine: str = ""
    # 每个后端的尝试记录，失败时给模型看
    attempts: list[str] = field(default_factory=list)
    query_ignored: bool = False

    @property
    def ok(self) -> bool:
        return bool(self.hits)

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "engine": self.engine,
            "hits": [hit.to_dict() for hit in self.hits],
            "note": "" if self.ok else "；".join(self.attempts) or "没有搜到结果",
        }

    def urls(self) -> set[str]:
        """结果 URL 集合 —— 用来和另一次搜索对比「结果是否没变」。"""
        return {hit.url for hit in self.hits}


# (引擎名, URL 模板, 解析函数)
ENGINES: tuple[tuple[str, str, object], ...] = (
    # 🔴 DuckDuckGo 当**主引擎**（2026-10-15）。
    # 理由见 parse_ddg 的说明：Bing 对带第二个词的中文查询经常返回
    # 完全无关的结果（静默失败）。代价是慢（中位 15.4 秒）。
    #
    # ⚠️ 顺序就是优先级，search() 逐个试，第一个出结果的赢。
    # ⚠️ DDG 的 html 版（无 JS）—— duckduckgo.com 那个是 JS 渲染的，抓不到结果。
    ("duckduckgo", "https://html.duckduckgo.com/html/?q={q}", parse_ddg),
    ("bing", "https://cn.bing.com/search?q={q}", parse_bing),
    ("360", "https://www.so.com/s?q={q}", parse_360),
)


def search(query: str, limit: int = 8, timeout: float = DEFAULT_TIMEOUT_S) -> SearchOutcome:
    """搜一次，**主后端 0 条就自动换备用**。

    ⚠️ 这个"换后端"不是锦上添花，是必需的：解析 HTML 的规则随时可能因
    改版失效，而失效的表现是**静默返回 0 条**（不报错）。没有备份的话，
    用户看到的是"宠物突然搜不到东西了"，而没有任何线索。
    """
    outcome = SearchOutcome()
    cleaned = str(query or "").strip()
    if not cleaned:
        outcome.attempts.append("搜索词是空的")
        return outcome

    for name, template, parser in ENGINES:
        url = template.format(q=urllib.parse.quote(cleaned))
        status, raw, headers = fetch_raw(url, timeout=timeout)
        if status != 200:
            outcome.attempts.append(f"{name}: HTTP {status}")
            continue
        body, _encoding = decode_bytes(raw, headers)
        hits = parser(body, limit=limit)  # type: ignore[operator]
        if hits:
            outcome.hits = hits
            outcome.engine = name
            return outcome
        outcome.attempts.append(f"{name}: 解析到 0 条（改版了？）")

    log.info("搜索「%s」没有结果：%s", cleaned, "；".join(outcome.attempts))
    return outcome


# --------------------------------------------------------------------------- #
# 读网页
# --------------------------------------------------------------------------- #


def fetch_page(url: str, timeout: float = DEFAULT_TIMEOUT_S) -> FetchResult:
    """抓一个网页并提取正文。

    失败**一定给得出原因** —— 模型据此决定"换个链接"还是"告诉用户我读不到"。
    含糊的失败（只说 ok=false）会让模型以为内容为空，进而编造。
    """
    target = str(url or "").strip()
    if not target.startswith(("http://", "https://")):
        return FetchResult(ok=False, url=target, reason="地址不是 http/https")

    status, raw, headers = fetch_raw(target, timeout=timeout)
    if status != 200:
        """
        ## 🔴 认得出「WAF 验证码」要说清（2026-10-12）

        实测起点（`qidian.com`）**全部路径**都返回：

        ```
        HTTP 202
        set-cookie: x-waf-captcha-referer=
        server: Lego Server
        正文只有：<script src="/C2WF946J0/probe.js"></script>
        ```

        那是**要在浏览器里执行 JS 才能过**的 WAF 挑战，
        纯 HTTP 客户端过不去（不是"被拒绝"，也不是"需要登录"）。

        ⚠️ **为什么要单独认出来**：含糊的「HTTP 202」会让模型以为
        "这页暂时打不开，再试一次" —— 于是它**反复重试同一个站**，
        白烧几轮。明确告诉它"这个站读不了、换别的站"才有用。

        （`x-waf-captcha-referer` 这个 cookie 名是起点 WAF 的，
        但用通用的判据——响应头里含 `waf` 或正文里有 `probe.js`——
        才不会只对一个站生效。）
        """
        header_blob = " ".join(f"{k}:{v}" for k, v in headers.items()).lower()
        body_head = (raw[:400].decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)).lower()
        looks_like_waf = (
            "waf" in header_blob
            or "captcha" in header_blob
            or "probe.js" in body_head
            or "/cdn-cgi/" in body_head
        )
        if looks_like_waf:
            return FetchResult(
                ok=False,
                url=target,
                reason=(
                    f"HTTP {status}：这个站有**反爬验证**（要浏览器执行 JS 才能过），"
                    "纯抓取读不到。**换个站的同类页面**，别再试这个站。"
                ),
            )
        return FetchResult(ok=False, url=target, reason=f"HTTP {status}（可能被拒绝或需要登录）")

    content_type = headers.get("content-type", "").lower()
    # ⚠️ PDF 必须按 Content-Type 拦掉。实测 arxiv 的 PDF 会被当成 HTML 处理，
    #    吐出 `%PDF-1.5 ...` 一堆二进制 —— 那东西喂给模型纯属浪费 token。
    if "application/pdf" in content_type:
        return FetchResult(ok=False, url=target, reason="这是一个 PDF 文件，读不了正文")

    body, encoding = decode_bytes(raw, headers)
    if "<html" not in body[:2000].lower() and "<body" not in body[:2000].lower():
        # 不是 HTML（可能是 JSON / 纯文本 / 别的二进制）
        text = body[:MAX_CONTENT_CHARS].strip()
        if len(text) < 40:
            return FetchResult(ok=False, url=target, reason="返回的内容不是网页，也没有可读文本")
        return FetchResult(ok=True, url=target, text=text, encoding=encoding)

    title = _extract_title(body)
    text = strip_html(body)
    # ⚠️ 纯 JS 站（实测小红书）只能读到页脚，正文很短。
    #    不拦的话模型会拿页脚当内容回答 —— 那比"读不到"更糟。
    if len(text) < 120:
        return FetchResult(
            ok=False,
            url=target,
            title=title,
            reason="页面正文太短（可能是纯 JS 渲染的站点，抓到的是空壳）",
        )

    truncated = len(text) > MAX_CONTENT_CHARS
    return FetchResult(
        ok=True,
        url=target,
        title=title,
        text=text[:MAX_CONTENT_CHARS] + ("\n…（内容过长，已截断）" if truncated else ""),
        encoding=encoding,
    )
