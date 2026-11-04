"""宠物自己上网找表情包（2026-11-03 用户要求）。

## 用户原话

> 「宠物也拥有能自己上网补充自己的表情包，小黑盒、B站评论区、
>   抖音评论区，网上搜索都可以，就是给于它下载的功能，
>   这个下载功能也做成模块，搜索模块搜索到之后，
>   它自己分辨下载标注就好了」

## 流程

    搜索（复用 tools 的 web_search）
      -> 从结果里挑**图片 URL**（见 `_image_urls_from`）
      -> 下载（严格限制：https + 大小上限 + 类型校验）
      -> 交给 `media_vision.label_sticker` 分类标注
      -> 存进 pet-media/stickers/<emotion>/

## 🔴 安全边界（这一节是重点）

"从网上下载图片"是**主动出站请求**，而且下下来的东西会被显示在
用户屏幕上。所以有几条硬约束，**一条都不能松**：

  ① **只允许 http(s)**，绝不允许 `file://` / `data:` / 其它协议
     —— 否则一个被构造的搜索结果就能读本地文件

  ② **大小上限**（`MAX_DOWNLOAD_BYTES`）
     —— 防"下了一个 500 MB 的东西把盘塞满"

  ③ **内容类型必须真是图片**（看 magic bytes，不看 `Content-Type`）
     —— 服务端可以撒谎；magic bytes 不能

  ④ **不跟随到内网地址**
     —— 防止 SSRF：搜索结果里塞一个 `http://127.0.0.1:8080/...`
        就能打到本机服务上

  ⑤ **默认关闭**（模块 `stickers` 默认 False）
     —— 这是主动联网行为，必须用户点头

## ⚠️ 不做"抖音/小黑盒专用爬虫"

用户提到那几个站，但它们的图片都有防盗链 / 签名 / 需要登录。
写专用爬虫维护成本极高而且随时会坏（这个项目在小说书源上已经
体会过一次）。

所以：**走通用搜索**（`web_search` 找图源 + 直接抓图片 URL），
这样任何站都能覆盖，坏的概率低得多。用户原话里也说了
「网上搜索都可以」，所以这条路是他认可的。
"""

from __future__ import annotations

import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

log = logging.getLogger("pet.sticker_fetch")

#: 单张下载上限。表情包一般 < 500 KB，给 4 MB 已经很宽松。
MAX_DOWNLOAD_BYTES = 4 * 1024 * 1024

#: 一次最多下载几张（防止"一口气下 200 张"）。
MAX_PER_ROUND = 3

#: 允许的图片 magic bytes -> 扩展名。
#:
#: ⚠️ 看**文件头**而不是 `Content-Type`：服务端可以撒谎，
#:    而 magic bytes 是文件自己的事实。
_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpg"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
    (b"RIFF", "webp"),          # 还要再验 WEBP 标记，见 `_sniff`
    (b"BM", "bmp"),
)

#: 搜索时用的查询词模板（挑"表情包"类的结果）。
#:
#: ⚠️ 刻意**不带具体情绪词**：让搜索先拿到一批泛化的表情包，
#:    具体归哪一类交给视觉模型判断（那是它的活）。
SEARCH_QUERIES: tuple[str, ...] = (
    "表情包 搞笑 图片",
    "表情包 无语 图片",
    "沙雕表情包 高清",
)

#: 判定"这个 URL 像图片"的后缀（搜索结果的链接常常不是图片本身）。
_IMAGE_EXT = re.compile(r"\.(png|jpe?g|gif|webp|bmp)(\?|#|$)", re.I)

#: 内网/本机地址（防 SSRF）。
_PRIVATE_HOST = re.compile(
    r"^(localhost|127\.|0\.0\.0\.0|10\.|192\.168\.|169\.254\.|"
    r"172\.(1[6-9]|2\d|3[01])\.|\[::1\]|\[fc|\[fd)",
    re.I,
)


def _sniff(data: bytes) -> str:
    """从文件头判断图片类型；不是图片返回空串。"""
    if not data:
        return ""
    for magic, ext in _MAGIC:
        if data.startswith(magic):
            if ext == "webp":
                # RIFF 也用于 wav/avi，必须再验 WEBP 标记
                return "webp" if data[8:12] == b"WEBP" else ""
            return ext
    return ""


def is_safe_url(url: str) -> tuple[bool, str]:
    """URL 能不能下。返回 `(可以, 原因)`。

    这是**安全边界的第一道**，单独抽成函数是为了能被测试钉住
    （不联网也能验证"内网地址被拒"这类规则）。
    """
    try:
        parsed = urllib.parse.urlsplit(url)
    except Exception:  # noqa: BLE001
        return False, "URL 解析失败"
    if parsed.scheme not in ("http", "https"):
        # ⚠️ 只允许这两种。`file://` 能读本地文件，`data:` 能绕过所有检查。
        return False, f"协议不允许：{parsed.scheme or '（空）'}"
    host = parsed.hostname or ""
    if not host:
        return False, "没有主机名"
    if _PRIVATE_HOST.match(host):
        # 防 SSRF：搜索结果里塞个 127.0.0.1 就能打到本机服务
        return False, f"内网地址不允许：{host}"
    return True, ""


def _image_urls_from(search_text: str, limit: int = 8) -> list[str]:
    """从 `web_search` 的**格式化文本**里挑出候选 URL。

    ## 🔴 2026-11-03 实测：搜索结果里几乎**没有**图片直链

    第一次实现只留 `.png/.jpg` 结尾的 URL，实测抽出 **0 个**。
    真实返回长这样：

        1. PNG搞笑表情包图片
           https://www.pngsucai.com/png/11916677.html      <- 是**页面**
        2. 下载《夸张笑脸表情包…》
           https://www.mksucai.com/...                      <- 也是页面

    搜索引擎返回的是**网页**，图片在网页里面。所以要两步走：

        ① 这里先收集**所有** http(s) URL（直链和页面都收）
        ② 调用方对每个 URL：是图片直链就直接下；是页面就抓下来再找图

    ## ⚠️ 直链优先

    图片直链（`.png` 结尾）排在前面 —— 它们一步就能下，
    不用多抓一次页面，省时间也省流量。
    """
    urls = re.findall(r"https?://[^\s\)\]\"'<>]+", search_text or "")
    direct: list[str] = []
    pages: list[str] = []
    seen: set[str] = set()
    for u in urls:
        # 去掉结尾常见的标点（Markdown 里常常粘着）
        u = u.rstrip(".,;:、。）】")
        if u in seen:
            continue
        seen.add(u)
        # 先过一遍安全闸门：不安全的一律不进来
        if not is_safe_url(u)[0]:
            continue
        if _IMAGE_EXT.search(u):
            direct.append(u)
        else:
            pages.append(u)
    # 直链在前，页面在后
    return (direct + pages)[:limit]


def extract_images_from_html(html: str, base_url: str = "", limit: int = 6) -> list[str]:
    """从一个网页里找出图片直链。

    ## 为什么需要它

    见 `_image_urls_from` 的说明：搜索结果给的是**页面**，图在里面。

    ## 挑法

      · `src` / `data-src` / `srcset` 里的图片 URL
      · 相对路径用 `base_url` 补全
      · **跳过**明显的装饰图（1x1 像素、logo、icon、avatar、sprite）
      · 跳过过小的（`width=16` 这种）

    ⚠️ 这些过滤是**启发式**的，不可能完美。真正的把关在下载之后：
      `download()` 会验 magic bytes 和大小，所以就算这里挑错了，
      也不会把垃圾存进用户的文件夹。
    """
    if not html:
        return []

    # 装饰图/小图标的关键词（路径里出现就跳过）
    junk = (
        "logo", "icon", "avatar", "sprite", "placeholder", "loading",
        "blank", "spacer", "pixel", "badge", "btn", "button", "arrow",
        "emoji-sprite", "1x1", "16x16", "32x32",
    )

    cands: list[str] = []
    # src / data-src / data-original / srcset
    for m in re.finditer(
        r"""(?:src|data-src|data-original|data-lazy-src)\s*=\s*["']([^"']+)["']""",
        html,
        re.I,
    ):
        cands.append(m.group(1))
    # srcset="a.png 1x, b.png 2x" -> 取第一个
    for m in re.finditer(r"""srcset\s*=\s*["']([^"']+)["']""", html, re.I):
        first = m.group(1).split(",")[0].strip().split(" ")[0]
        if first:
            cands.append(first)

    out: list[str] = []
    seen: set[str] = set()
    for raw in cands:
        u = raw.strip()
        if not u or u.startswith("data:"):
            continue
        # 补全相对路径
        if u.startswith("//"):
            u = "https:" + u
        elif u.startswith("/"):
            base = urllib.parse.urlsplit(base_url)
            u = f"{base.scheme}://{base.netloc}{u}"
        elif not u.startswith("http"):
            if base_url:
                u = urllib.parse.urljoin(base_url, u)
            else:
                continue
        if u in seen:
            continue
        seen.add(u)
        low = u.lower()
        if any(w in low for w in junk):
            continue
        # 只留图片后缀（查询串允许跟在后面）
        if not _IMAGE_EXT.search(u):
            continue
        if not is_safe_url(u)[0]:
            continue
        out.append(u)
        if len(out) >= limit:
            break
    return out


def fetch_page(url: str, timeout_s: float = 15.0, max_bytes: int = 512 * 1024) -> str:
    """抓一个网页的 HTML（用来从里面找图片）。失败返回空串。

    ⚠️ 只读前 `max_bytes`：一个页面可能有几 MB，而我们只要 `<img>` 标签。
       读完整个页面既慢又浪费（有些站还有 10 MB 的 JS bundle）。
    """
    ok, _why = is_safe_url(url)
    if not ok:
        return ""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            raw = resp.read(max_bytes)
        return raw.decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 —— 抓不到页面是常态
        return ""


def download(url: str, timeout_s: float = 15.0) -> tuple[bytes, str, str]:
    """下一张图。返回 `(字节, 扩展名, 失败原因)`；成功时第三个是空串。

    ## 为什么不用 httpx

    `tools.py` 那边用 httpx（异步）。但这里是**同步**下载（会被
    `asyncio.to_thread` 包起来跑，见调用方），用 urllib 就够了，
    少一个依赖层次。

    ## ⚠️ 大小限制是**边读边判**

    不能先 `read()` 再看长度 —— 那样一个 500 MB 的响应会先占满内存。
    这里按块读，超了立刻停。
    """
    ok, why = is_safe_url(url)
    if not ok:
        return b"", "", why

    req = urllib.request.Request(
        url,
        headers={
            # ⚠️ 带 UA：很多图床对空 UA 直接 403
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
            ),
            "Accept": "image/*,*/*",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            # ⚠️ 不信任 Content-Type，但可以用它**提前拒绝**明显不对的
            ctype = str(resp.headers.get("Content-Type") or "").lower()
            if ctype and not ctype.startswith(("image/", "application/octet-stream")):
                return b"", "", f"不是图片类型：{ctype[:40]}"

            chunks: list[bytes] = []
            total = 0
            while True:
                block = resp.read(64 * 1024)
                if not block:
                    break
                total += len(block)
                if total > MAX_DOWNLOAD_BYTES:
                    # 超了就**立刻停**，不要读完再判（那样内存先爆）
                    return b"", "", f"图片太大（超过 {MAX_DOWNLOAD_BYTES // 1024 // 1024} MB）"
                chunks.append(block)
            data = b"".join(chunks)
    except urllib.error.HTTPError as exc:
        return b"", "", f"HTTP {exc.code}"
    except Exception as exc:  # noqa: BLE001 —— 网络问题一律当"这次没下成"
        return b"", "", f"{type(exc).__name__}: {str(exc)[:80]}"

    ext = _sniff(data)
    if not ext:
        # ⚠️ 这里是最重要的一道：服务端说 image/png，实际返回一段 HTML
        #    （防盗链/登录页）的情况非常常见。magic bytes 不会撒谎。
        return b"", "", "内容不是图片（可能被防盗链拦了）"
    return data, ext, ""
