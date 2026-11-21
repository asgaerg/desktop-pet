"""Legado 书源规则解释器（2026-11-20）。

## 为什么需要它

用户给了 601 个来自 yckceo 的书源（`1340.json`），全是 **Legado 格式**。
而项目原来的 `config/book-sources.yaml` 是自定义的简单 YAML ——
两者语法完全不同，直接导进去没用。

## 范围：只实现**纯选择器**那部分

实测这批源：

    核心规则含 JS 的      320 个      <- 放弃
    核心规则纯选择器的    267 个      <- 本模块覆盖

JS 那 320 个不做的理由：Legado 的 JS 是任意脚本（含 `java.*` 桥），
要跑就得嵌 JS 引擎，那是另一个数量级的工作量，而且书源脚本随时会变。
267 个纯选择器源已经足够覆盖大部分书。

## Legado 规则语法（本项目实现的子集）

一条规则形如：

    .bookbox@css:.bookname@text@put:{u:"a.0@href"}
    #content@textNodes##/快眼小说网.*/|{{baseUrl}}
    chapterList: :正文卷[\\s\\S]*?/dl&&href ="([^"]+)">([^<]+)

拆解成**流水线**，从左到右依次执行：

    ① 选择器         `.bookbox` / `#id` / `[attr=x]` / `tag.class`
    ② 后缀操作符     `@text` `@html` `@textNodes` `@href` `@src` `@content`
    ③ 变量           `@get:{u}` 取、`@put:{u:"a.0@href"}` 存
    ④ 正则替换       `##pattern` 或 `##pattern##replacement`
    ⑤ 组合           `&&`（先跑左边，再用左边的结果跑右边）`||`（左边空就用右边）
    ⑥ 模板           `{{baseUrl}}` `{{key}}`
    ⑦ 数组下标       `.0` `.1`（Legado 用 `span.0` 表示第 0 个）

## ⚠️ 三个容易做错的地方

**① `.0` 是下标不是类名**

Legado 里 `span.0@text` = "第一个 span 的文本"。而 CSS 里 `.0`
会被当成 class="0"。所以要在解析前把 `tag.N` 形式转成下标操作。

**② `##` 后面可能还有 `##`**

`##a##b` 是"用正则 a 替换成 b"，而 `##a` 只是"删掉匹配 a 的部分"。
split 时不能无脑取第一段。

**③ `&&` 的右边是在**左边结果**上继续解析**

不是"两个独立匹配取交集"。这决定了必须按流水线执行而不是并行。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

import lxml.html as lxml_html
from lxml import etree


# --------------------------------------------------------------------------- #
# 数据
# --------------------------------------------------------------------------- #


@dataclass
class RuleContext:
    """一次规则求值的上下文。"""

    #: 页面 URL（`{{baseUrl}}` 用）
    base_url: str = ""
    #: 搜索关键词（`{{key}}` 用）
    key: str = ""
    #: `@put` 存下来的变量
    variables: dict[str, str] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# HTML -> 节点
# --------------------------------------------------------------------------- #


class HtmlDoc:
    """一个解析好的 HTML 文档，支持按 Legado 规则取节点。

    ⚠️ 用 lxml 而不是 bs4：lxml 的 CSS 选择器（cssselect）更快，
        而且能保留原始文本节点（`@textNodes` 需要）。
    """

    def __init__(self, html: str) -> None:
        # `recover=True` 很重要：这些盗版站 HTML 经常标签不闭合，
        # 严格模式会直接抛异常，而浏览器能显示、说明宽松解析才对。
        #: 原始 HTML 文本。**必须保留** —— 有些书源规则是
        #: :正文卷[\s\S]*?/dl&&href ="([^"]+)">([^<]+) 这种
        #: "在整页原文上跑正则" 的写法，而 etree.tostring() 重新序列化
        #: 会把 href ="..."（带空格）输成 href="..."（无空格），
        #: 正则就再也匹配不上 —— 表现为"目录 0 章"。
        self.raw = html or ""
        parser = lxml_html.HTMLParser(recover=True, encoding="utf-8")
        self.root = lxml_html.fromstring(html or "<html></html>", parser=parser)
        # 有些站返回的是 GBK，lxml 按 utf-8 读会乱码 —— 这里不用管，
        # 上层拿到的已经是 decoded 字符串（见 `_decode`）。

    def css(self, selector: str) -> list[Any]:
        """按 CSS 选择器取节点。选择器为空 = 取根。"""
        sel = (selector or "").strip()
        if not sel:
            return [self.root]
        try:
            return list(self.root.cssselect(sel))
        except Exception:  # noqa: BLE001 —— 选择器非法就返回空，不要炸调用方
            return []


def node_text(node: Any) -> str:
    """取节点文本（Legado 的 `@text`）。"""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    try:
        return " ".join("".join(node.itertext()).split())
    except Exception:  # noqa: BLE001
        return ""


def node_html(node: Any) -> str:
    """取节点内部 HTML（Legado 的 `@html`）。"""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    try:
        return "".join(
            etree.tostring(c, encoding="unicode", method="html") for c in node
        )
    except Exception:  # noqa: BLE001
        return ""


def node_attr(node: Any, name: str) -> str:
    """取属性（`@href` / `@src` / `@content` …）。"""
    if node is None or isinstance(node, str):
        return ""
    try:
        return str(node.get(name) or "")
    except Exception:  # noqa: BLE001
        return ""


# --------------------------------------------------------------------------- #
# 规则求值
# --------------------------------------------------------------------------- #

#: Legado 里"取内容"的后缀操作符（后面跟 `@` 或直接结尾）。
_ATTR_OPS = {
    "text": "__text__",
    "textNodes": "__textnodes__",
    "html": "__html__",
    "outerHtml": "__html__",
    "ownText": "__text__",
}

#: `@put:{name:"rule"}` —— 存变量
_RE_PUT = re.compile(r"@put:\{([^}]*)\}")
#: `@get:{name}` —— 取变量
_RE_GET = re.compile(r"@get:\{([^}]*)\}")
#: `{{xxx}}` —— 模板占位
_RE_TPL = re.compile(r"\{\{(\w+)\}\}")


def render_template(text: str, ctx: RuleContext) -> str:
    """把 `{{baseUrl}}` / `{{key}}` 换成实际值。"""

    def sub(m: re.Match[str]) -> str:
        name = m.group(1)
        if name == "baseUrl" or name == "baseURL":
            return ctx.base_url
        if name == "key":
            return ctx.key
        if name == "page":
            return "1"
        return ctx.variables.get(name, "")

    return _RE_TPL.sub(sub, text or "")


def normalize_pattern(pattern: str) -> str:
    """把书源里的正则还原成 Python 能用的形式。

    ## 为什么需要（2026-11-20 实测）

    书源 JSON 里的正则是**双重转义**的：

        文件里存的:  双反斜杠 + s（JSON 里这样存）
        实际想要的:  单反斜杠 + s（Python 正则的写法）

    Legado 读的时候会做一次转义还原，而我们是直接读 JSON ——
    拿到的是字面 `\\s`，在 Python 正则里表示"反斜杠 + s"，
    于是 `[\\s\\S]` 变成"匹配反斜杠或 s 或 ..."，永远匹配不到正文。

    实测表现：「目录 0 章」，而页面上明明有 2700 多章。

    ⚠️ 只把双反斜杠还原成单反斜杠，不要动单个的 ——
       单反斜杠本来就是合法正则语法（`\d` `\w`）。
    """
    # ⚠️ Python 里 "\\\\" 字面量表示两个反斜杠字符，
    #    "\\" 表示一个。所以这里是把"两个"换成"一个"。
    return (pattern or "").replace("\\\\", "\\")


def apply_regex(value: str, pattern: str, replacement: str = "") -> str:
    """应用 `##pattern##replacement` 的正则替换。"""
    if not pattern:
        return value
    pattern = normalize_pattern(pattern)
    try:
        return re.sub(pattern, replacement, value)
    except re.error:
        # 书源里的正则经常有方言差异，编不过就当"没匹配"处理，
        # **绝不抛异常** —— 否则一个坏源会让整次搜索失败
        return value


def _split_regex(rule: str) -> tuple[str, str, str]:
    """把 `A##pattern##replacement` 拆成三段。

    ## ⚠️ 不能无脑 `split("##")`

    `##` 后面可能还有 `##`：

        内容##\\s+##            -> 两段（替换成空）
        内容##a.*?b            -> 两段（删掉匹配）
        内容##(a)##$1<br>      -> 三段

    所以按 `##` 切，第一段是指令，第二段是正则，第三段（如果有）是替换。
    注意正则里本身可能含 `##`（罕见但存在），这里按"最多切 2 次"处理。
    """
    if "##" not in rule:
        return rule, "", ""
    parts = rule.split("##", 2)
    head = parts[0]
    pattern = parts[1] if len(parts) > 1 else ""
    repl = parts[2] if len(parts) > 2 else ""
    return head, pattern, repl


def _strip_put(rule: str, ctx: RuleContext, doc: Optional[HtmlDoc]) -> str:
    """处理 `@put:{u:"a.0@href"}` —— 求值并存进变量。

    ⚠️ 必须在**主体选择器之前**处理：`name: .bookname@text@put:{u:"a.0@href"}`
       的意思是"用 `.bookname` 取文本，同时把 `a.0@href` 存到 u"。
       两者作用在**同一个节点集合**上。
    """
    while True:
        m = _RE_PUT.search(rule)
        if not m:
            return rule
        body = m.group(1)
        # 形如 `u:"a.0@href"`，可能有多个（逗号分隔）
        for pair in re.finditer(r'(\w+)\s*:\s*"([^"]*)"', body):
            var, sub_rule = pair.group(1), pair.group(2)
            if doc is not None:
                ctx.variables[var] = evaluate(sub_rule, doc, ctx, _depth=1)
        rule = (rule[: m.start()] + rule[m.end():]).strip()


def _split_combine(rule: str) -> tuple[str, str, str]:
    """拆 `&&` / `||` 组合。

    Legado 语义：

        A && B   -> 在 **A 的结果** 上继续跑 B
        A || B   -> A 没结果就用 B

    ⚠️ `||` 优先级比 `&&` 低，但实际书源里几乎不混用，所以按
       "先找 `&&`，没有再找 `||`" 处理即可。
    """
    i = rule.find("&&")
    if i >= 0:
        return rule[:i], "&&", rule[i + 2:]
    i = rule.find("||")
    if i >= 0:
        return rule[:i], "||", rule[i + 2:]
    return rule, "", ""


def _pick_nodes(doc: HtmlDoc, selector: str, ctx: RuleContext) -> list[Any]:
    """按选择器取节点，支持 Legado 的 `tag.N` 下标写法。

    ## ⚠️ `.0` 是下标不是类名

    Legado 里 `span.0@text` = "第一个 span 的文本"。
    而 CSS 里 `.0` 会被当成 `class="0"`。所以先把 `tag.N` 转成
    "选 tag 然后取第 N 个"。
    """
    sel = selector.strip()
    if not sel:
        return [doc.root]

    # `+@css:` 前缀 —— 明确指定用 CSS（本项目只有 CSS，剥掉即可）
    for prefix in ("+@css:", "@css:"):
        if sel.startswith(prefix):
            sel = sel[len(prefix):].strip()

    # `:正文卷[\s\S]*?/dl` 这种是**正则**不是选择器
    if sel.startswith(":"):
        body, pat, repl = _split_regex(sel)
        node_text_all = " ".join("".join(doc.root.itertext()).split())
        return [apply_regex(node_text_all, pat, repl)] if pat else []

    # 处理尾部的 `.N` 下标：`span.3` / `a.0`
    m = re.match(r"^([a-zA-Z][\w-]*)\.(\d+)$", sel)
    if m:
        tag, idx = m.group(1), int(m.group(2))
        nodes = doc.css(tag)
        return [nodes[idx]] if idx < len(nodes) else []

    # `tag.a` 是 Legado 的"类型 a 的标签"，CSS 里就是 `a`
    if sel.startswith("tag."):
        sel = sel[4:]

    return doc.css(sel)


def evaluate(
    rule: str,
    doc: HtmlDoc,
    ctx: RuleContext,
    *,
    _depth: int = 0,
) -> str:
    """求一条 Legado 规则，返回**字符串**结果。

    流程（顺序很重要，见模块顶部说明）：

        1. 模板替换 `{{baseUrl}}` / `{{key}}`
        2. `@get:{u}` 取变量（可能整条规则就是个变量）
        3. 剥 `@put`（求值 + 存变量）
        4. 拆 `##` 正则
        5. 拆 `&&` / `||`
        6. 剩下的部分是「选择器 + @操作符」

    ⚠️ `_depth` 防递归：`@put` 里引用的子规则可能又含 `@put`。
    """
    if _depth > 5 or not rule:
        return ""

    rule = render_template(rule, ctx)

    # ② 整条就是个 `@get:{u}` -> 直接返回值
    m = _RE_GET.fullmatch(rule.strip())
    if m:
        return ctx.variables.get(m.group(1).strip(), "")

    # ③ 剥 @put
    rule = _strip_put(rule, ctx, doc)
    if not rule:
        return ""

    # ④ 拆正则
    rule, pattern, repl = _split_regex(rule)

    # ⑤ 拆组合
    left, op, right = _split_combine(rule)
    if op == "||":
        val_l = evaluate(left, doc, ctx, _depth=_depth + 1)
        if val_l.strip():
            return _finish(val_l, pattern, repl)
        return _finish(evaluate(right, doc, ctx, _depth=_depth + 1), pattern, repl)
    if op == "&&":
        # `A && B`：A 通常是"裁出一段 HTML 文本"，B 是"在这段文本里再用正则取"
        a = evaluate(left, doc, ctx, _depth=_depth + 1)
        if a.strip():
            # B 直接当正则作用在 A 的结果上（Legado 的常见用法）
            b_head, b_pat, b_repl = _split_regex(right)
            if b_pat:
                # `href ="([^"]+)">([^<]+)` 这种是"取捕获组"
                got = _apply_capture(a, b_pat + ("##" + b_repl if b_repl else ""))
                if got:
                    return _finish(got, pattern, repl)
            got2 = evaluate(right, doc, ctx, _depth=_depth + 1)
            if got2.strip():
                return _finish(got2, pattern, repl)
            return _finish(a, pattern, repl)
        return _finish(evaluate(right, doc, ctx, _depth=_depth + 1), pattern, repl)

    # ⑥ 选择器 + @操作符
    val = _eval_selector_ops(rule, doc, ctx)
    return _finish(val, pattern, repl)


def _finish(value: str, pattern: str, repl: str) -> str:
    """最后统一应用 `##` 正则。"""
    if not pattern:
        return value
    return apply_regex(value, pattern, repl)


def _apply_capture(text: str, spec: str) -> str:
    """在文本上跑正则并取捕获组。

    `href ="([^"]+)">([^<]+)` 这种规则在 Legado 里表示
    "匹配到的第 1、2 组"。调用方通常用 `$1` / `$2` 引用。
    """
    head, pattern, repl = _split_regex(spec)
    if not pattern:
        return ""
    try:
        m = re.search(normalize_pattern(pattern), text or "")
    except re.error:
        return ""
    if not m:
        return ""
    if not repl:
        return m.group(0)
    # 把 `$1` 换成 `\1`
    py_repl = re.sub(r"\$(\d)", r"\\\1", repl)
    try:
        return m.expand(py_repl)
    except Exception:  # noqa: BLE001
        return m.group(0)


def _eval_selector_ops(rule: str, doc: HtmlDoc, ctx: RuleContext) -> str:
    """求「选择器 + @操作符」这一层。

    ## 形式

        .bookname@text          取文本
        .bookname a@href        取属性
        #content@textNodes      取全部文本节点
        img@src
        .a@text@put:{...}       （@put 已在前面剥掉）

    ## ⚠️ `@` 的最后一段可能是属性名，也可能是操作符

    先认操作符（`text`/`html`/`textNodes`），认不出就当**属性名**。
    """
    if not rule.strip():
        return ""

    # 以 `@` 开头 = 在根上直接取属性
    parts = rule.split("@")
    selector = parts[0].strip()
    ops = [p.strip() for p in parts[1:] if p.strip()]

    nodes = _pick_nodes(doc, selector, ctx)
    if not nodes:
        return ""

    # 认第一个操作
    op = ops[0] if ops else ""
    rest = ops[1:]

    if op in _ATTR_OPS:
        kind = _ATTR_OPS[op]
        if kind == "__text__":
            val = node_text(nodes[0])
        elif kind == "__textnodes__":
            val = node_text(nodes[0])
        else:
            val = node_html(nodes[0])
    elif op:
        # 当属性名
        val = node_attr(nodes[0], op)
    else:
        # 没有操作符：取文本
        val = node_text(nodes[0])

    # 还有剩余操作（少见）：继续当属性/文本处理
    for nxt in rest:
        if nxt in _ATTR_OPS:
            val = node_text(nodes[0]) if _ATTR_OPS[nxt] == "__text__" else node_html(nodes[0])
        else:
            val = node_attr(nodes[0], nxt) or val

    return val.strip()


# --------------------------------------------------------------------------- #
# 适配层：把 Legado 书源包成和 `reading.BookSource` 同一套接口
# --------------------------------------------------------------------------- #


class LegadoSource:
    """一个 Legado 书源，接口与 `reading.BookSource` 对齐。

    ## 为什么要适配而不是改造 `reading.BookSource`

    那个类是为**自定义 YAML** 写的（`SourceSpec` 里是一堆结构化字段）。
    而 Legado 的规则是**字符串表达式**，语义完全不同 —— 塞进 `SourceSpec`
    只会让两边都变难懂。

    但对外两者要能互换，都提供：

        search(query)        -> [(title, url), ...]
        chapter_urls(url)    -> [(name, url), ...]
        chapter_body(url)    -> (title, text)

    ## ⚠️ 只支持"纯选择器"源

    含 `@js:` / `<js>` / `java.*` 的源会被 `is_supported()` 判掉 ——
    那需要内嵌 JS 引擎，是另一个数量级的工作量。
    实测这批源里 320/601 含 JS，剩下的 267 个纯选择器够用。
    """

    def __init__(self, raw: dict[str, Any]) -> None:
        self.raw = raw
        self.name = str(raw.get("bookSourceName") or "未命名源")
        # `bookSourceUrl` 有时带 `#后缀`（作者的标记），要剥掉
        self.base = str(raw.get("bookSourceUrl") or "").split("#")[0].rstrip("/") + "/"
        self.search_url = str(raw.get("searchUrl") or "")
        self.rule_search = raw.get("ruleSearch") or {}
        self.rule_book = raw.get("ruleBookInfo") or {}
        self.rule_toc = raw.get("ruleToc") or {}
        self.rule_content = raw.get("ruleContent") or {}

    # ------------------------------------------------------------------ 判定

    @staticmethod
    def core_rule_blob(raw: dict[str, Any]) -> str:
        """把搜索/目录/正文三组规则拼成一个字符串（判定用）。"""
        rs = raw.get("ruleSearch") or {}
        rt = raw.get("ruleToc") or {}
        rc = raw.get("ruleContent") or {}
        return " ".join(
            str(v) for v in list(rs.values()) + list(rt.values()) + list(rc.values())
        )

    @classmethod
    def is_supported(cls, raw: dict[str, Any]) -> bool:
        """这个源能不能用本模块跑。

        三个条件：
          ① 搜索/目录/正文三组规则都在（缺一不可）
          ② 有 searchUrl
          ③ 核心规则**不含 JS**
        """
        if not isinstance(raw, dict) or not raw.get("enabled"):
            return False
        rs = raw.get("ruleSearch") or {}
        rt = raw.get("ruleToc") or {}
        rc = raw.get("ruleContent") or {}
        if not (rs.get("bookList") and rt.get("chapterList") and rc.get("content")):
            return False
        if not raw.get("searchUrl"):
            return False
        blob = cls.core_rule_blob(raw)
        return not ("@js:" in blob or "<js>" in blob or "java." in blob)

    # ------------------------------------------------------------------ 网络

    def _abs(self, url: str) -> str:
        from urllib.parse import urljoin

        u = (url or "").strip()
        if not u:
            return ""
        if u.startswith("http"):
            return u
        return urljoin(self.base, u)

    def _fetch(self, url: str) -> str:
        """同步抓一页。用 requests —— 调用方已在线程里跑。"""
        try:
            import requests

            r = requests.get(
                url,
                timeout=18,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
                    )
                },
            )
            if r.status_code != 200:
                return ""
            # 让 requests 自己判编码（盗版站 GBK/GB18030 很常见）
            if not r.encoding or r.encoding.lower() == "iso-8859-1":
                r.encoding = r.apparent_encoding or "utf-8"
            return r.text
        except Exception:  # noqa: BLE001
            return ""

    # ------------------------------------------------------------------ 内部

    def _select_list(self, doc: HtmlDoc, rule: str) -> list[Any]:
        """按 `bookList` / `chapterList` 规则取节点列表。

        Legado 的写法有几种，这里都试一遍：

            `.bookbox`          纯选择器
            `+@css:.bookbox`    显式 CSS
            `class.block`       `class.X` = `.X`
            `tag.li`            `tag.X` = `X`
        """
        sel = (rule or "").strip()
        for prefix in ("+@css:", "@css:"):
            if sel.startswith(prefix):
                sel = sel[len(prefix):].strip()
        if sel.startswith("class."):
            sel = "." + sel[len("class."):]
        elif sel.startswith("tag."):
            sel = sel[len("tag."):]
        if not sel:
            return []
        return doc.css(sel)

    @staticmethod
    def _sub_doc(node: Any) -> "HtmlDoc":
        """以某个节点为根造一个 HtmlDoc（规则里写的是相对路径）。"""
        sub = HtmlDoc.__new__(HtmlDoc)
        sub.root = node
        return sub

    # ------------------------------------------------------------------ 接口

    def search(self, query: str) -> list[tuple[str, str]]:
        """搜书。返回 `[(书名, 详情页URL), ...]`。"""
        surl = self.search_url
        if not surl.startswith("http"):
            surl = self._abs(surl)
        url = (
            surl.replace("{{key}}", query)
            .replace("{{page}}", "1")
            .replace("{{baseUrl}}", self.base)
        )
        html = self._fetch(url)
        if not html:
            return []
        doc = HtmlDoc(html)

        sel = str(self.rule_search.get("bookList") or "")
        nodes = self._select_list(doc, sel)
        out: list[tuple[str, str]] = []
        for node in nodes:
            sub = self._sub_doc(node)
            # ⚠️ 每本书一个**独立**变量作用域：`@put` 不能串到下一本
            item_ctx = RuleContext(base_url=self.base, key=query)
            title = evaluate(str(self.rule_search.get("name") or ""), sub, item_ctx)
            link = evaluate(str(self.rule_search.get("bookUrl") or ""), sub, item_ctx)
            if not title or not link:
                continue
            out.append((title.strip(), self._abs(link)))
        # 去重（同一本书可能重复出现在结果里）
        return list(dict.fromkeys(out))

    def chapter_urls(self, book_url: str) -> list[tuple[str, str]]:
        """取目录。返回 `[(章节名, URL), ...]`。"""
        html = self._fetch(book_url)
        if not html:
            return []
        doc = HtmlDoc(html)
        ctx = RuleContext(base_url=book_url)

        # 有些源要先从详情页取到"目录页"地址（tocUrl）
        toc_url = str(self.rule_toc.get("tocUrl") or "")
        if toc_url:
            got = evaluate(toc_url, doc, ctx)
            if got:
                target = self._abs(got)
                if target and target != book_url:
                    html2 = self._fetch(target)
                    if html2:
                        doc = HtmlDoc(html2)
                        ctx = RuleContext(base_url=target)

        sel = str(self.rule_toc.get("chapterList") or "")
        name_rule = str(self.rule_toc.get("chapterName") or "")
        url_rule = str(self.rule_toc.get("chapterUrl") or "")

        # ① 规则是"在整页文本上用正则"（如 `:正文卷...&&href ="(...)"`）
        if sel.startswith(":") or "&&" in sel:
            return self._chapters_by_regex(doc, sel)

        # ② 常规：选一堆节点，各自取名字和链接
        out: list[tuple[str, str]] = []
        for node in self._select_list(doc, sel):
            name = self._group_from(node, name_rule) or node_text(node)
            link = self._group_from(node, url_rule) or node_attr(node, "href")
            if not link:
                continue
            out.append((str(name).strip()[:80], self._abs(link)))
        return [(n, u) for n, u in out if n and u]

    def _group_from(self, node: Any, rule: str) -> str:
        """规则形如 `$2##修正` 时，从节点的 href/文本里取捕获组。

        Legado 约定：`$1` = 链接、`$2` = 章节名（配合 `chapterList` 的
        正则捕获组一起用）。
        """
        if not rule or not rule.startswith("$"):
            return ""
        m = re.match(r"\$(\d+)", rule)
        if not m:
            return ""
        idx = int(m.group(1))
        href = node_attr(node, "href")
        text = node_text(node)
        base = href if idx == 1 else text
        rest = rule[m.end():]
        if rest.startswith("##"):
            _, pat, repl = _split_regex(rest)
            base = apply_regex(base, pat, repl)
        return base

    def _chapters_by_regex(self, doc: HtmlDoc, rule: str) -> list[tuple[str, str]]:
        """处理 `:正文卷[\\s\\S]*?/dl&&href ="([^"]+)">([^<]+)` 这种。

        ## 语义

        `:` 开头 = 在**整页 HTML 文本**上跑正则。
        `&&` 右边是"从匹配到的片段里再取捕获组"。

        这里简化实现：把 `&&` 右边的正则套在左边截出的那段上。
        `$1` = 链接、`$2` = 章节名。
        """
        raw = doc.raw
        left, op, right = _split_combine(rule)
        # ⚠️ :xxx 这种写法里，**冒号后面整段就是正则**，不是选择器。
        #    _split_regex 只在有 ## 时才分三段，这里要单独处理冒号形式。
        head, pat, _ = _split_regex(left)
        if head.startswith(":") and not pat:
            pat = head[1:]
        pat = normalize_pattern(pat)
        if not pat:
            return []
        try:
            m = re.search(pat, raw)
        except re.error:
            return []
        if not m:
            return []
        segment = m.group(0)
        if op != "&&" or not right:
            return []

        # ⚠️ `right` 通常只是一段正则（没有 ##），而 `_split_regex` 在没有
        #    `##` 时会返回 `(rule, "", "")` —— 那样 r_pat 就是空的了。
        #    所以这里要单独判：没有 `##` 就整段当正则。
        #    所以这里要单独判：没有 ## 就整段当正则。
        r_head, r_pat, r_repl = _split_regex(right)
        if not r_pat:
            r_pat = r_head
        out: list[tuple[str, str]] = []
        try:
            for mm in re.finditer(normalize_pattern(r_pat), segment):
                link = mm.group(1) if mm.lastindex and mm.lastindex >= 1 else ""
                name = mm.group(2) if mm.lastindex and mm.lastindex >= 2 else ""
                if r_repl:
                    name = apply_regex(name, r_repl, "")
                if link:
                    out.append((str(name).strip()[:80], self._abs(link)))
        except re.error:
            return []
        return out

    def chapter_body(self, url: str) -> tuple[str, str]:
        """取正文。返回 `(标题, 正文文本)`。"""
        html = self._fetch(url)
        if not html:
            return "", ""
        doc = HtmlDoc(html)
        ctx = RuleContext(base_url=url)
        rule = str(self.rule_content.get("content") or "")

        rule = render_template(rule, ctx)
        rule = _strip_put(rule, ctx, doc)
        head, pattern, repl = _split_regex(rule)

        # ⚠️ `head` 可能带 `@操作符`（`#content@textNodes`），
        #    不能直接丢给 CSS 选择器 —— `_pick_nodes` 不认 `@`。
        #    所以走 `_eval_selector_ops`（它同时处理选择器 + 操作符）。
        text = _eval_selector_ops(head, doc, ctx)
        if not text:
            # 兜底：纯选择器再试一次
            nodes = _pick_nodes(doc, head.split("@")[0], ctx)
            if nodes:
                text = node_text(nodes[0])
        if pattern:
            text = apply_regex(text, pattern, repl)

        # 标题：优先 `ruleContent.title`，没有就取页面 <title>
        title_rule = str(self.rule_content.get("title") or "")
        title = evaluate(title_rule, doc, ctx) if title_rule else ""
        if not title:
            ts = doc.css("title")
            title = node_text(ts[0]) if ts else ""

        return title.strip(), (text or "").strip()


def load_legado_sources(path: Any) -> list[LegadoSource]:
    """从 `1340.json` 这类文件里读出**可用**的 Legado 书源。"""
    import json
    from pathlib import Path as _P

    p = _P(str(path))
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []
    if not isinstance(data, list):
        return []
    return [LegadoSource(raw) for raw in data if LegadoSource.is_supported(raw)]
