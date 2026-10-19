"""实测 `pet.web`：搜索 + 读网页（2026-10-12）。

## 为什么这个测试要**真联网**

`parse_bing` / `parse_360` 解析的是**外部站点的 HTML**，而那东西随时会变。
用假 HTML 测只能证明"我的正则能匹配我手写的那段样本" —— **零价值**。

所以这里真发请求，判据是**结构性的**（而不是"等于某个固定标题"）：

  · 返回 >= 3 条结果
  · 每条都有非空 title 和 http(s) 开头的 url
  · 失败时能给出原因

搜索引擎改版时这个测试会红 —— 那正是它该做的事。

## 不测什么

不测"搜到的内容对不对"（那是搜索引擎的事）。这里只保证
**管道通 + 解析出结构正确的数据**。
"""

from __future__ import annotations

import sys
import time

sys.path.insert(0, ".")
from tool_bootstrap import setup  # noqa: E402

setup(verbose=False)

from pet import web  # noqa: E402

passed = 0
failed: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    global passed
    if ok:
        passed += 1
        print(f"  OK   {name}")
    else:
        failed.append(f"{name} —— {detail}")
        print(f"  FAIL {name}\n       {detail}")


print("  ═══ ① 纯函数：编码识别 ═══")
if True:   # ⚠️ Python 没有花括号块（我第一版写成了 C 风格）
    # GBK 中文
    gbk = "中关村在线".encode("gbk")
    text, enc = web.decode_bytes(gbk, {})
    check("GBK 字节能正确解出中文", "中关村在线" in text, f"enc={enc} text={text!r}")

    utf8 = "中关村在线".encode("utf-8")
    text2, enc2 = web.decode_bytes(utf8, {})
    check("UTF-8 字节能正确解出中文", "中关村在线" in text2, f"enc={enc2} text={text2!r}")

    # 响应头里声明了 charset 时优先用它
    text3, enc3 = web.decode_bytes(gbk, {"content-type": "text/html; charset=gbk"})
    check("响应头的 charset 优先", enc3.lower() == "gbk", f"实际 {enc3}")

print()
print("  ═══ ② 纯函数：正文提取 ═══")
if True:   # ⚠️ Python 没有花括号块（我第一版写成了 C 风格）
    html = (
        "<html><head><title>标题在这</title><style>.a{color:red}</style></head>"
        "<body><script>var x=1;</script><p>第一段</p><p>第二段</p></body></html>"
    )
    body = web.strip_html(html)
    check("去掉 script 内容", "var x" not in body, f"实际 {body!r}")
    check("去掉 style 内容", "color:red" not in body, f"实际 {body!r}")
    check("保留正文", "第一段" in body and "第二段" in body, f"实际 {body!r}")
    check("标题能抽出来", web._extract_title(html) == "标题在这")

print()
print("  ═══ ③ 真联网：Bing 搜索（主后端）═══")
t0 = time.monotonic()
outcome = web.search("RTX 5090 参数", limit=5)
dt = (time.monotonic() - t0) * 1000
print(f"     引擎={outcome.engine!r}  结果={len(outcome.hits)} 条  {dt:.0f}ms")
for hit in outcome.hits[:3]:
    print(f"       · {hit.title[:60]}")
    print(f"         {hit.url[:88]}")
if not outcome.ok:
    print(f"     尝试记录：{outcome.attempts}")

check("搜索能拿到结果（>=3 条）", len(outcome.hits) >= 3, f"只拿到 {len(outcome.hits)} 条；{outcome.attempts}")
check(
    "每条结果都有非空标题",
    all(hit.title.strip() for hit in outcome.hits),
    f"空标题的：{[h.url for h in outcome.hits if not h.title.strip()]}",
)
check(
    "每条结果的 url 都是 http(s)",
    all(hit.url.startswith("http") for hit in outcome.hits),
    f"异常的：{[h.url for h in outcome.hits if not h.url.startswith('http')]}",
)
check(
    "说了是哪个引擎给的",
    # 🔴 2026-10-15：加了 DuckDuckGo 主引擎。这个白名单必须同步 ——
    # 忘了同步的表现是「测试红了但代码是对的」，会白费一轮排查。
    outcome.engine in ("duckduckgo", "bing", "360"),
    f"engine={outcome.engine!r}",
)
check(
    "主引擎是 DuckDuckGo（Bing 对中文查询常返回无关结果）",
    web.ENGINES[0][0] == "duckduckgo",
    f"第一个引擎是 {web.ENGINES[0][0]!r} —— Bing 当主时会崩",
)
check(
    "仍保留 bing / 360 作为退路",
    [name for name, _, _ in web.ENGINES] == ["duckduckgo", "bing", "360"],
    f"实际顺序：{[name for name, _, _ in web.ENGINES]}",
)
check(
    "超时够 DDG 用（实测中位 15.4 秒）",
    web.DEFAULT_TIMEOUT_S >= 18.0,
    f"DEFAULT_TIMEOUT_S={web.DEFAULT_TIMEOUT_S} —— 会全部超时，表现是「一条都搜不到」",
)

print()
print("  ═══ ④ 真联网：读网页正文（含 GBK 站点）═══")
if True:   # ⚠️ Python 没有花括号块（我第一版写成了 C 风格）
    # 中关村在线 —— 实测是 **GBK**，正好验编码识别在真实站上过不过
    target = ""
    for hit in outcome.hits:
        if "zol.com.cn" in hit.url:
            target = hit.url
            break
    if not target:
        target = next((h.url for h in outcome.hits if h.url.startswith("http")), "")

    if not target:
        check("读网页：有可用的目标链接", False, "搜索结果里没有 http 链接")
    else:
        result = web.fetch_page(target)
        print(f"     目标 {target[:80]}")
        print(f"     ok={result.ok}  编码={result.encoding}  正文 {len(result.text)} 字")
        if not result.ok:
            print(f"     原因：{result.reason}")
        check("读网页成功", result.ok, f"reason={result.reason}")
        check("读到实质正文（>=300 字）", len(result.text) >= 300, f"只有 {len(result.text)} 字")
        check("正文里没有大量乱码", result.text.count("\ufffd") < 10, f"替换字符 {result.text.count(chr(0xFFFD))} 个")

print()
print("  ═══ ⑤ 失败路径必须**给得出原因** ═══")
if True:   # ⚠️ Python 没有花括号块（我第一版写成了 C 风格）
    bad = web.fetch_page("not-a-url")
    check("非 http 地址被拒", not bad.ok, "应当拒绝")
    check("非 http 地址给了原因", bool(bad.reason), "reason 是空的 —— 模型会以为内容就是空的")

    # 一个明确会 403 的（知乎实测 403）
    zhihu = web.fetch_page("https://www.zhihu.com/question/19550224")
    check("403 被识别成失败", not zhihu.ok, f"ok={zhihu.ok} reason={zhihu.reason}")
    check("403 的原因说得清", "HTTP" in zhihu.reason, f"reason={zhihu.reason!r}")

    # PDF 必须被 Content-Type 拦住
    pdf = web.fetch_page("https://arxiv.org/pdf/1706.03762")
    check("PDF 被拦下（不喂二进制给模型）", not pdf.ok, f"ok={pdf.ok}")
    check("PDF 的原因说得清", "PDF" in pdf.reason, f"reason={pdf.reason!r}")

    empty = web.search("   ")
    check("空搜索词被拒", not empty.ok, "空的搜索词不该发请求")

print()
print(f"  ═══ 结果：{passed}/{passed + len(failed)} 通过 ═══")
if failed:
    for item in failed:
        print(f"    ❌ {item}")
    raise SystemExit(1)
print("  ✅ 全部通过")
