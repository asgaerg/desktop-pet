"""实测 pet.video：B 站视频解析 + 站内搜索（2026-10-12）。

用户的需求是「让他读懂 b 站上的信息」。这个测试验：

  ① 从各种链接格式认出 BV 号（含带追踪参数的分享链接）
  ② 视频详情 / 标签能不能拿到
  ③ 站内搜索能不能用（实测不用签名，意外）
  ④ 抖音明确返回读不了，而不是空
  ⑤ 输出里必须写明这不是官方信息

⚠️ 本文件里的 " 是**双引号占位符**，写完由脚本替换成真正的引号。

（为什么要这样：往文件里写直引号在这个环境里会被转成中文引号，而 Python
代码里的字符串定界符必须是直引号。）
"""

from __future__ import annotations

import sys

sys.path.insert(0, ".")

from tool_bootstrap import setup  # noqa: E402

setup(verbose=False)

from pet import video  # noqa: E402

passed = 0
failed: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    global passed
    if ok:
        passed += 1
        print(f"  OK   {name}")
    else:
        failed.append(f"{name} —— {detail}")
        print(f"  FAIL {name}")
        print(f"       {detail}")


print("  ═══ ① 认站点与 BV 号（纯函数）═══")
if True:
    check(
        "认得出 bilibili.com",
        video.site_of("https://www.bilibili.com/video/BV1xx") == "bilibili",
    )
    check("认得出 b23.tv 短链", video.site_of("https://b23.tv/abc") == "bilibili")
    check("认得出抖音", video.site_of("https://www.douyin.com/video/123") == "douyin")
    check("普通网页认不出", video.site_of("https://example.com") == "")

    kind, value = video.bilibili_id("https://www.bilibili.com/video/BV1UmH96xERT/")
    check(
        "从裸链接取 BV 号",
        (kind, value) == ("bvid", "BV1UmH96xERT"),
        f"{kind}/{value}",
    )

    messy = (
        "https://www.bilibili.com/video/BV1fkHD6wEXt/"
        "?spm_id_from=333.788.videopod.episodes&vd_source=f31746903ad444e4fedf53942af"
    )
    kind2, value2 = video.bilibili_id(messy)
    check("带追踪参数也取得到 BV 号", value2 == "BV1fkHD6wEXt", f"实际 {value2!r}")

    check(
        "非 B 站链接取不出 id",
        video.bilibili_id("https://example.com") == ("", ""),
    )

print()
print("  ═══ ② 视频详情（真联网）═══")
if True:
    info = video.parse("https://www.bilibili.com/video/BV1UmH96xERT/")
    check("parse 认得出这是视频站", info is not None)
    if info:
        print(f"    ok={info.ok} 站点={info.site}")
        print(f"    标题={info.title[:50]!r}")
        print(f"    UP={info.author!r}  发布={info.published}  时长={info.duration}s")
        print(f"    播放={info.view} 点赞={info.like} 评论={info.reply}")
        print(f"    标签={info.tags}")
        check("拿到了标题", bool(info.title), f"实际 {info.title!r}")
        check("拿到了 UP 主", bool(info.author), f"实际 {info.author!r}")
        check("拿到了发布时间", bool(info.published), f"实际 {info.published!r}")
        check("拿到了播放数", info.view > 0, f"实际 {info.view}")
        check(
            "拿到了标签（信息量最大的一环）",
            len(info.tags) > 0,
            f"实际 {info.tags}",
        )
        check("标签里有实质内容", any(len(t) >= 2 for t in info.tags), f"{info.tags}")

print()
print("  ═══ ③ 站内搜索（真联网，不用签名）═══")
if True:
    hits, reason = video.search_bilibili("终末地 新角色", 6)
    print(f"    搜到 {len(hits)} 条" + (f"（{reason}）" if reason else ""))
    for hit in hits[:4]:
        print(f"      · {hit.title[:56]}")
        print(f"        {hit.url}  {hit.author}  {hit.play}播放")
    check("站内搜索能用", len(hits) > 0, f"reason={reason}")
    check(
        "每条都有标题和链接",
        all(h.title and h.url.startswith("http") for h in hits),
        "有空的标题或链接",
    )
    check(
        "标题里的 HTML 高亮标签被剥掉了",
        not any("<" in h.title for h in hits),
        "还有标题带尖括号",
    )
    check(
        "带了播放量（排序依据）",
        any(h.play for h in hits),
        f"{[h.play for h in hits]}",
    )

print()
print("  ═══ ④ 抖音：必须说得清为什么读不了 ═══")
if True:
    info = video.parse("https://www.douyin.com/video/7000000000000000000")
    check("抖音返回了结果对象（不是 None）", info is not None)
    if info:
        check("ok=False", not info.ok)
        check(
            "给出了原因（含糊的失败会让模型编造）",
            bool(info.reason),
            f"reason={info.reason!r}",
        )
        check(
            "原因里说明了读不了、并让换个来源",
            "读不了" in info.reason and "换" in info.reason,
            f"实际 {info.reason!r}",
        )

print()
print("  ═══ ⑤ 输出里必须写明这不是官方信息 ═══")
if True:
    info = video.parse("https://www.bilibili.com/video/BV1UmH96xERT/")
    text = video.format_video(info) if info else ""
    print("    格式化的前 320 字：")
    for line in text[:320].split("\n"):
        print(f"      {line}")
    check(
        "写明了这是视频页面上的公开信息",
        "视频页面上的公开信息" in text,
        "少了这句，模型会把社区讨论当官方设定",
    )
    check("提示了标签只是社区在讨论", "社区在讨论" in text)
    check("提示了不要当成确定的事实", "不要把这些当成确定的事实" in text)

    hits, _ = video.search_bilibili("终末地", 3)
    hit_text = video.format_hits("终末地", hits) if hits else ""
    check(
        "搜索结果的输出也带同样的提醒",
        "不等于官方信息" in hit_text,
        f"实际结尾：{hit_text[-140:]!r}",
    )

print()
print("  ═══ ⑥ 坏链接要给原因，不是空 ═══")
if True:
    info = video.parse("https://www.bilibili.com/video/BV1AAAAAAAAAA/")
    check("坏 BV 号返回 ok=False", info is not None and not info.ok)
    check("有原因", info is not None and bool(info.reason))

    info2 = video.parse("https://www.bilibili.com/")
    check(
        "没 BV 号的链接也给原因",
        info2 is not None and not info2.ok and bool(info2.reason),
    )

    check(
        "非视频站返回 None（好落到通用抓取）",
        video.parse("https://example.com") is None,
    )

print()
print(f"  ═══ 结果：{passed}/{passed + len(failed)} 通过 ═══")
if failed:
    for item in failed:
        print(f"    ❌ {item}")
    raise SystemExit(1)
print("  ✅ 全部通过")
