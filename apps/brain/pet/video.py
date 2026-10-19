"""
视频站点解析（B 站 / 抖音，2026-10-12）。

## 用户的要求

> 「我让它搜索终末地的新女角色信息它搜索不出来，你能不能找找有什么解析
>   B 站和抖音的 api 给他，**让他读懂 b 站上的信息**，这样好理解」

## 为什么要单独一个模块

`web.py` 是**通用**网页抓取（任何站点）。而视频站点有**结构化接口**，
比抓 HTML 好得多 —— HTML 里全是导航和推荐列表。

实测对比（同一个 B 站视频）：

```
web.fetch_page()  -> 1185 字，但里面是「首页/番剧/直播/…」+ 推荐列表
bilibili API      -> 标题、标签、UP 主、发布时间、播放/点赞/评论数、分P
```

## 🔬 实测确认的可用接口（**不带签名**）

```
✅ api.bilibili.com/x/web-interface/view?bvid=      视频详情
✅ api.bilibili.com/x/tag/archive/tags?bvid=         视频标签
✅ api.bilibili.com/x/web-interface/search/all/v2    搜索（意外地不用签名）
✅ api.bilibili.com/x/v2/reply                      评论（视频关了评论就取不到）
❌ api.bilibili.com/x/player/v2                     字幕 → **要登录 Cookie**（三个视频都是空）
❌ 抖音全部接口                                      风控页，要签名
```

## ⚠️ 字幕拿不到 —— 这是最大的遗憾，说清楚

**信息在视频里**（UP 主说的话），而字幕接口从 2022 年起收紧了：
不带 Cookie 时 `subtitle.subtitles` 恒为空数组（实测三个不同类型的视频都是）。

那还能"读懂"吗？**够用，而且实测有效**：

```
模型问「终末地新女角色是谁」-> 搜到 B 站视频
  -> 视频标题「终末地1.6新角色养成材料总览」
  -> 标签  祀 / 明河 / 明日方舟 / 终末地
  -> 相关视频标题里大量「祀」「明河」「冰火双辅」「物理电队」
  -> 答出「1.6 新角色是祀和明河」✅
```

**⚠️ 但要诚实**：那些标签和推荐标题只能说明"**社区在讨论**"，
**不能说明官方设定**。模型如果把它们当官方信息说出去就是"看着有据其实没据"。
所以拼出来的摘要里要**标明来源性质**（见 `_format_video`）。

## 抖音：暂时放弃，但要**说得出原因**

抖音的 web 接口全部要 `X-Bogus` / `a_bogus` 签名 + 风控，
不带签名拿回来的是空壳 HTML（实测）。

⚠️ **不要为了跑通去破解签名** —— 那既脆弱（随时失效）又不合适。
所以 `parse_douyin()` 会**明确返回"读不了"**，让模型知道要换个来源，
而不是让它以为"这个链接没内容"从而编造。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from . import web

log = logging.getLogger("pet.video")

# 单次接口调用超时。比普通网页抓取放宽一点：接口偶尔慢。
API_TIMEOUT_S = 12.0


@dataclass(slots=True)
class VideoInfo:
    """一个视频的结构化信息。"""

    ok: bool
    site: str = ""
    url: str = ""
    title: str = ""
    author: str = ""
    # 秒
    duration: int = 0
    published: str = ""
    view: int = 0
    like: int = 0
    reply: int = 0
    tags: list[str] = field(default_factory=list)
    # UP 主写的简介（常常是空的，实测那个终末地视频就是空）
    description: str = ""
    # 分P 标题（多 P 视频里每一集的标题，信息量大）
    parts: list[str] = field(default_factory=list)
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "site": self.site,
            "url": self.url,
            "title": self.title,
            "author": self.author,
            "tags": self.tags,
            "view": self.view,
            "like": self.like,
            "reason": self.reason,
        }


# --------------------------------------------------------------------------- #
# B 站
# --------------------------------------------------------------------------- #

_BV_RE = re.compile(r"(BV[0-9A-Za-z]{10})")
_AV_RE = re.compile(r"av(\d+)", re.I)


def bilibili_id(url: str) -> tuple[str, str]:
    """从 URL 里认出 B 站视频 id。返回 `(类型, 值)`，认不出就 `("", "")`。

    ## ⚠️ 为什么要单独抽出来

    用户给的可能是一长串带参数的分享链接：

        https://www.bilibili.com/video/BV1UmH96xERT/?spm_id_from=333.788&vd_source=f31...

    直接拿整串去请求接口会 400。所以**只取 BV 号**。
    （实测用户给过一条带 `vd_source` 的链接，那是分享追踪参数。）
    """
    match = _BV_RE.search(url)
    if match:
        return "bvid", match.group(1)
    match = _AV_RE.search(url)
    if match:
        return "aid", match.group(1)
    return "", ""


def _api(path: str) -> Optional[dict[str, Any]]:
    """调一个 B 站接口，返回 `data` 字段。**任何异常/非 0 code 都返回 None**。"""
    status, raw, _headers = web.fetch_raw(f"https://api.bilibili.com{path}", timeout=API_TIMEOUT_S)
    if status != 200:
        log.info("B站接口 HTTP %s：%s", status, path[:80])
        return None
    try:
        payload = json.loads(raw.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        log.info("B站接口返回的不是 JSON：%s", path[:80])
        return None
    if payload.get("code") != 0:
        log.info("B站接口 code=%s message=%s", payload.get("code"), payload.get("message"))
        return None
    return payload.get("data")


def _format_count(value: Any) -> str:
    """把播放数变成人看得懂的形式（12345 -> 1.2万）。"""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return "?"
    if number >= 100000000:
        return f"{number / 100000000:.1f}亿"
    if number >= 10000:
        return f"{number / 10000:.1f}万"
    return str(number)


def _stamp(value: Any) -> str:
    """Unix 秒 -> `YYYY-MM-DD`。"""
    try:
        import time as _time

        return _time.strftime("%Y-%m-%d", _time.localtime(int(value)))
    except (TypeError, ValueError, OSError):
        return ""


def parse_bilibili(url: str) -> VideoInfo:
    """解析一个 B 站视频链接。

    ## 两次接口调用

    ```
    ① /x/web-interface/view?bvid=   标题/UP/统计/分P
    ② /x/tag/archive/tags?bvid=     标签
    ```

    ② 是单独的接口 —— 标签**信息量很大**（实测终末地那个视频的标签就是
    `祀 / 明河 / 明日方舟 / 终末地`，直接给出了新角色的名字）。
    """
    kind, value = bilibili_id(url)
    if not kind:
        return VideoInfo(
            ok=False, site="bilibili", url=url,
            reason="这个链接里找不到 B 站视频号（BV 号）。请给完整视频链接。",
        )

    detail = _api(f"/x/web-interface/view?{kind}={value}")
    if not detail:
        return VideoInfo(
            ok=False, site="bilibili", url=url,
            reason="B 站接口没返回视频详情（视频可能已删除，或者被风控了）。",
        )

    owner = detail.get("owner") or {}
    stat = detail.get("stat") or {}
    pages = detail.get("pages") or []

    info = VideoInfo(
        ok=True,
        site="bilibili",
        url=str(detail.get("short_link_v2") or f"https://www.bilibili.com/video/{detail.get('bvid')}"),
        title=str(detail.get("title") or ""),
        author=str(owner.get("name") or ""),
        duration=int(detail.get("duration") or 0),
        published=_stamp(detail.get("pubdate")),
        view=int(stat.get("view") or 0),
        like=int(stat.get("like") or 0),
        reply=int(stat.get("reply") or 0),
        description=str(detail.get("desc") or "").strip(),
        # ⚠️ 分P 只在**多 P** 时才有意义；单 P 时它等于标题，是噪音
        parts=[
            str(page.get("part") or "")
            for page in pages
            if len(pages) > 1 and str(page.get("part") or "")
        ][:12],
    )

    # 标签（单独接口；失败不影响主信息）
    tags = _api(f"/x/tag/archive/tags?{kind}={value}")
    if isinstance(tags, list):
        info.tags = [str(tag.get("tag_name") or "") for tag in tags if tag.get("tag_name")][:12]

    return info


# --------------------------------------------------------------------------- #
# 抖音
# --------------------------------------------------------------------------- #


def parse_douyin(url: str) -> VideoInfo:
    """抖音 —— **暂时读不了**，但要说得清为什么。

    ## 实测结论

    抖音的 web 接口全部要 `X-Bogus` / `a_bogus` 签名 + 风控。
    不带签名拿回来的是**空壳 HTML**：

    ```html
    <html><head><meta charset="UTF-8" /></head><body></body><script>…</script>
    ```

    ⚠️ **不要为了跑通去破解签名** —— 那既脆弱（随时失效，且要跟着改）
    又不合适。

    ## ⚠️ 为什么仍要返回一个"说得清"的结果

    含糊的失败（比如返回空字符串）会让模型以为"这个链接没内容"，
    进而**编造**里面的内容。所以这里明确告诉它**读不了、请换来源**。
    """
    return VideoInfo(
        ok=False,
        site="douyin",
        url=url,
        reason=(
            "抖音的页面要登录和签名才能读，我读不了。"
            "如果是想了解视频里说的东西，换 B 站链接或者直接搜关键词都行。"
        ),
    )


# --------------------------------------------------------------------------- #
# 统一入口
# --------------------------------------------------------------------------- #

SITE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("bilibili", re.compile(r"(bilibili\.com|b23\.tv|acg\.tv)", re.I)),
    ("douyin", re.compile(r"(douyin\.com|iesdouyin\.com|v\.douyin)", re.I)),
)


def site_of(url: str) -> str:
    """认出手里这个链接属于哪个视频站（认不出返回空串）。"""
    for name, pattern in SITE_PATTERNS:
        if pattern.search(url):
            return name
    return ""


def parse(url: str) -> Optional[VideoInfo]:
    """如果这个链接是视频站的，解析它；不是就返回 `None`。

    ⚠️ 返回 `None` 而不是"空的 VideoInfo" ——
    调用方据此决定"走通用网页抓取"，两条路不会混。
    """
    site = site_of(url)
    if site == "bilibili":
        return parse_bilibili(url)
    if site == "douyin":
        return parse_douyin(url)
    return None


@dataclass(slots=True)
class VideoHit:
    """一条视频搜索结果。"""

    title: str
    url: str
    author: str = ""
    duration: str = ""
    play: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "title": self.title,
            "url": self.url,
            "author": self.author,
            "duration": self.duration,
            "play": self.play,
        }


def search_bilibili(keyword: str, limit: int = 6) -> tuple[list[VideoHit], str]:
    """搜 B 站视频。返回 `(结果, 失败原因)`。

    ## 为什么值得单独做（而不是只靠 web_search）

    `web_search` 也能搜到 B 站视频（实测搜到过），但那走的是**搜索引擎的索引**——
    只有被 Bing 收录的视频才搜得到，而且给的是网页链接。

    而 B 站的搜索接口给的是**站内结果**：按站内热度排、带播放量、带时长。
    实测这个接口**不需要 wbi 签名**（意外，2023 年后 B 站给不少接口加了签名）。

    ## ⚠️ 返回 `(结果, 原因)` 而不是只返回结果

    与 `web.py` 里"失败一定给得出原因"同一条原则：
    含糊的失败会让模型**编一个答案**。

    ## ⚠️ 标题里带 `<em class="keyword">` 高亮标签

    接口返回的标题带 HTML 高亮（搜索引擎那种 `<em>`）。
    不剥掉的话模型会读到一串标签，而且可能学着输出 HTML。
    """
    cleaned = str(keyword or "").strip()
    if not cleaned:
        return [], "搜索词是空的"

    import urllib.parse

    encoded = urllib.parse.quote(cleaned)
    data = _api(f"/x/web-interface/search/all/v2?keyword={encoded}&page=1")
    if not data:
        return [], "B 站搜索接口没返回结果（可能被风控了）"

    hits: list[VideoHit] = []
    for group in data.get("result") or []:
        if group.get("result_type") != "video":
            continue
        for item in group.get("data") or []:
            title = re.sub(r"<[^>]+>", "", str(item.get("title") or "")).strip()
            bvid = str(item.get("bvid") or "")
            if not title or not bvid:
                continue
            hits.append(
                VideoHit(
                    title=title,
                    url=f"https://www.bilibili.com/video/{bvid}",
                    author=str(item.get("author") or ""),
                    duration=str(item.get("duration") or ""),
                    play=_format_count(item.get("play")),
                )
            )
            if len(hits) >= limit:
                break
        break

    if not hits:
        return [], "B 站上没搜到这个关键词的视频"
    return hits, ""


def format_hits(keyword: str, hits: list[VideoHit]) -> str:
    """把搜索结果拼成给模型看的文本。"""
    lines = [f"B 站搜「{keyword}」的视频："]
    for index, hit in enumerate(hits, 1):
        lines.append(f"{index}. {hit.title}")
        lines.append(f"   {hit.url}")
        bits = [bit for bit in (hit.author, hit.play and f"{hit.play}播放", hit.duration) if bit]
        if bits:
            lines.append(f"   {' · '.join(bits)}")
    lines.append("")
    lines.append(
        "⚠️ 这些是**视频标题**。标题只说明 UP 主怎么起名和社区在聊什么，"
        "**不等于官方信息** —— 要确认设定/数据得看视频内容或官方渠道。"
        "想看某个视频的详细页面信息（标签/简介），用 web_fetch 传它的链接。"
    )
    return "\n".join(lines)


def format_video(info: VideoInfo) -> str:
    """把 `VideoInfo` 拼成给模型看的文本。

    ## 🔴 必须**标明来源性质**，否则模型会把社区讨论当官方设定

    实测（`probe-bili-live.py`，历史实测脚本）：模型靠标签和推荐标题答出了
    「1.6 新角色是祀和明河」—— **结论对**，但它的依据是
    "UP 主的标签 + 相关视频标题"，而不是官方公告。

    那些依据只能说明「**社区在讨论**」。所以这里明确写出
    「以下是视频页面上的公开信息」，并**不把标签说成"角色设定"**。

    含糊的措辞会让模型把"有人在猜"说成"官方说"——
    那是比"答不出来"更糟的错。
    """
    if not info.ok:
        return f"读不了这个视频：{info.reason}"

    lines = [f"【B 站视频】{info.title}"]
    meta = [f"UP 主：{info.author}"] if info.author else []
    if info.published:
        meta.append(f"发布：{info.published}")
    if info.duration:
        meta.append(f"时长：{info.duration // 60}分{info.duration % 60}秒")
    if meta:
        lines.append("  " + " · ".join(meta))

    stats = []
    if info.view:
        stats.append(f"播放 {_format_count(info.view)}")
    if info.like:
        stats.append(f"点赞 {_format_count(info.like)}")
    if info.reply:
        stats.append(f"评论 {_format_count(info.reply)}")
    if stats:
        lines.append("  " + " · ".join(stats))

    if info.tags:
        # ⚠️ 措辞是「页面标签」而不是「角色/设定」—— 见函数头的说明
        lines.append(f"  页面标签：{'、'.join(info.tags)}")
    if info.parts:
        lines.append("  分集标题：")
        for part in info.parts:
            lines.append(f"    - {part}")
    if info.description:
        lines.append(f"  简介：{info.description[:400]}")

    lines.append("")
    lines.append(
        "⚠️ 以上是**视频页面上的公开信息**（标题/标签/UP 主），"
        "不是官方公告。标签和标题只能说明**社区在讨论什么** —— "
        "要确认具体设定/数据，得看简介、评论区或官方渠道，"
        "不要把这些当成确定的事实说出去。"
    )
    return "\n".join(lines)
