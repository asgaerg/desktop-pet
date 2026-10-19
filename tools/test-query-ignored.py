"""验证「查询没起作用」检测（2026-10-12，2026-10-15 重构）。

## 为什么要测

判据本身很便宜（比 URL 集合），但**误报的代价很大**：
如果正常的第二次搜索被误判成"查询没起作用"，
模型就会收到一句误导性的提示，然后放弃本来有效的搜索。

所以要测**两边**：

```
✅ 该报的报出来   —— 同一批结果
❌ 不该报的不报   —— 换了真正不同的词 / 结果太少 / 第一次搜索
```

## 🔴 2026-10-15 重构：从"真联网"改成"注入"

### 原来的问题

判据内联在 `_web_search()` 里，所以**测试只能靠真联网**。而它其实混了两件事：

```
① 判据的数学对不对        <- 确定性的
② Bing 这次返回了什么      <- 不确定的
```

于是测试会**因为搜索引擎变差而失败**。实测就撞上了：

```
'幻梦猎人'          -> 晶圆厂 / Epic Games 文档   （完全无关）
'幻梦猎人 新书'      -> 百度知道问答页             （完全无关）
交集 0 -> 断言失败，而代码一个字都没改
```

⚠️ **这种测试比没有更糟**：它给出一个假警报，让人去查一个不存在的 bug。

### 现在的结构

```
① 判据的纯函数      <- 注入 URL 集合，**永远可重复**（主体，必须全过）
② 模块级状态流转     <- 注入，验 _last_search_urls 的读写
③ 真联网的冒烟检查   <- **只打印结果、不计入通过数**（Bing 变差不该让测试红）
```

判据本身抽到了 `pet.tools.query_looks_ignored()`。
"""

from __future__ import annotations

import asyncio
import json
import re
import sys

sys.path.insert(0, ".")
from tool_bootstrap import setup  # noqa: E402

setup(verbose=False)

from pet import tools  # noqa: E402

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


HINT_MARK = "搜索引擎**没看懂这个查询**"
predicate = tools.query_looks_ignored


def u(*names: str) -> set[str]:
    """把短名变成像 URL 的字符串（判据只比集合，内容无所谓）。"""
    return {f"https://example.com/{name}" for name in names}


print("=" * 74)
print("① 判据的纯函数（主体 —— 注入集合，不联网）")
print("=" * 74)

# ── 该报的 ──────────────────────────────────────────────────────────────
check(
    "完全相同的 5 条 -> 报",
    predicate(u("a", "b", "c", "d", "e"), u("a", "b", "c", "d", "e")) is True,
    "同一批结果没认出来",
)
check(
    "重叠 4/5 = 0.8（正好在阈值上）-> 报",
    predicate(u("a", "b", "c", "d", "x"), u("a", "b", "c", "d", "e")) is True,
    "0.8 是 >= 的边界，应该算重叠",
)
check(
    "8 条重叠 7 = 0.875（真实的浮动样子）-> 报",
    predicate(
        u("a", "b", "c", "d", "e", "f", "g", "x"),
        u("a", "b", "c", "d", "e", "f", "g", "h"),
    )
    is True,
    "这正是 2026-10-12 实测遇到的 7/8，必须认出来",
)
check(
    "3 条全同 -> 报（刚好到最小条数）",
    predicate(u("a", "b", "c"), u("a", "b", "c")) is True,
    "3 条是 >= 的边界，应该算",
)
check(
    "两边条数不同：较小者 4 条全命中 -> 报",
    predicate(u("a", "b", "c", "d", "e"), u("a", "b", "c", "d")) is True,
    "较小者是 4，命中 4/4 = 1.0",
)

# ── 不该报的（防误报）────────────────────────────────────────────────────
check(
    "重叠 3/5 = 0.6 -> 不报",
    predicate(u("a", "b", "c", "x", "y"), u("a", "b", "c", "d", "e")) is False,
    "0.6 明显低于 0.8，不该报",
)
check(
    "交集 0 -> 不报",
    predicate(u("x", "y", "z", "w", "v"), u("a", "b", "c", "d", "e")) is False,
    "完全不同的结果不该报",
)
check(
    "只有 2 条且全同 -> 不报（统计意义不足）",
    predicate(u("a", "b"), u("a", "b")) is False,
    "2 条结果时「重叠 2/2」没有意义，报了就是误报",
)
check(
    "只有 1 条且相同 -> 不报",
    predicate(u("a"), u("a")) is False,
    "1 条更不该报",
)
check(
    "第一次搜索（上一次是空集）-> 不报",
    predicate(u("a", "b", "c", "d"), set()) is False,
    "第一次没有「上一次」可比",
)
check(
    "这次空结果 -> 不报",
    predicate(set(), u("a", "b", "c", "d")) is False,
    "空结果走「搜索没有结果」那条分支，不该在这里报",
)
check(
    "两次都空 -> 不报",
    predicate(set(), set()) is False,
    "两个空集不该算「高度重叠」",
)
check(
    "4 条只有 1 条相同（换了主题）-> 不报",
    predicate(u("a", "x", "y", "z"), u("a", "b", "c", "d")) is False,
    "只重合 1/4 = 0.25，不该报",
)

print()
print("=" * 74)
print("② 阈值常量（测试与实现共用，避免两处硬编码）")
print("=" * 74)
check(
    "重叠阈值是 0.8",
    tools._QUERY_IGNORED_OVERLAP == 0.8,
    f"实际 {tools._QUERY_IGNORED_OVERLAP}",
)
check(
    "最小条数是 3",
    tools._QUERY_IGNORED_MIN_HITS == 3,
    f"实际 {tools._QUERY_IGNORED_MIN_HITS}",
)

print()
print("=" * 74)
print("③ 模块级状态：`_last_search_urls` 的读写（注入，不联网）")
print("=" * 74)
saved = tools._last_search_urls
try:
    tools._last_search_urls = set()
    check("初始是空集", tools._last_search_urls == set(), "模块加载后应该是空集")

    first_urls = u("a", "b", "c", "d", "e")
    ignored1 = predicate(first_urls, tools._last_search_urls)
    tools._last_search_urls = first_urls
    check("第一次不报", ignored1 is False, "没有上一次可比")

    second_urls = u("a", "b", "c", "d", "e")
    ignored2 = predicate(second_urls, tools._last_search_urls)
    tools._last_search_urls = second_urls
    check("第二次同一批要报", ignored2 is True, "状态流转后应该认出重叠")

    third_urls = u("p", "q", "r", "s", "t")
    ignored3 = predicate(third_urls, tools._last_search_urls)
    tools._last_search_urls = third_urls
    check("第三次换主题不报", ignored3 is False, "换了主题不该报")
finally:
    tools._last_search_urls = saved

print()
print("=" * 74)
print("④ ⚠️ 真实搜索冒烟检查 —— **只打印，不计入通过数**")
print("=" * 74)
print("  理由：这一段的期望值取决于 **Bing 此刻返回什么**，而那个东西会变。")
print("       把它算进通过数 = 让搜索引擎决定测试红绿。")
print("       （2026-10-15 实测：Bing 对中文查询返回完全无关的结果）")
print()


def search(query: str) -> str:
    args = json.dumps({"query": query}, ensure_ascii=False)
    return asyncio.run(
        tools.run_tool(
            tools.build_tools("apps/desktop/pet-data/_dup", True), "web_search", args
        )
    )


try:
    tools._last_search_urls = set()
    first = search("幻梦猎人")
    print(f"  搜索「幻梦猎人」 -> {len(first)} 字")
    print(f"    {first[:140]!r}")
    print(f"    报了吗：{HINT_MARK in first}")
    print()
    second = search("幻梦猎人 新书")
    print(f"  搜索「幻梦猎人 新书」 -> {len(second)} 字")
    print(f"    {second[:140]!r}")
    print(f"    报了吗：{HINT_MARK in second}")
    print()
    urls1 = set(re.findall(r"https?://\S+", first))
    urls2 = set(re.findall(r"https?://\S+", second))
    smaller = min(len(urls1), len(urls2)) if urls1 and urls2 else 0
    overlap = len(urls1 & urls2) / smaller if smaller else 0.0
    print(f"  实际重叠率 {overlap:.2f}（{len(urls1 & urls2)}/{smaller}）")
    print("  ⚠️ 这个数字**仅供参考** —— 它反映 Bing 此刻的行为，不是代码质量。")
    print("     判据本身由上面 ① 那 13 条注入用例保证。")
except Exception as exc:  # noqa: BLE001
    print(f"  ⚠️ 真实搜索失败（不计入通过数）：{type(exc).__name__}: {exc}")

print()
print("=" * 74)
print(f"结果：{passed}/{passed + len(failed)} 通过")
if failed:
    print("  未通过：")
    for item in failed:
        print(f"    FAIL {item}")
else:
    print("  全部通过")
print("=" * 74)

sys.exit(1 if failed else 0)
