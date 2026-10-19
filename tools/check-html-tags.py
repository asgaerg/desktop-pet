"""设置页 HTML 的标签配平检查（可当门禁反复用）。

## 为什么需要

我手工重排了 `settings.html`（把语音相关的块从「隐私」搬到新的「语音」标签）。
**手工搬 HTML 最容易留下未闭合的 `<div>`** —— 而浏览器会默默容错，
症状可能是"某个区块莫名其妙被包进了上一个区块"，
看起来完全不像标签问题（这正是这个项目里反复吃亏的那类 bug）。

所以搬完必须机器校验一次，而不是靠眼睛扫。
"""

from __future__ import annotations

import pathlib
import re
import sys

VOID = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
}


def check(path: pathlib.Path) -> int:
    s = path.read_text(encoding="utf-8")
    # 去掉注释（注释里的标签不算）
    s = re.sub(r"<!--[\s\S]*?-->", lambda m: "\n" * m.group(0).count("\n"), s)
    # 只看 <body> 里面（DOCTYPE/html/head 由别处保证）
    m = re.search(r"<body[^>]*>([\s\S]*)</body>", s)
    if not m:
        print("  ⚠️ 找不到 <body>…</body> —— 只检查全文")
        body, base_line = s, 1
    else:
        body, base_line = m.group(1), s[: m.start(1)].count("\n") + 1

    stack: list[tuple[str, int]] = []
    problems: list[str] = []
    for tag_m in re.finditer(r"<(/?)([a-zA-Z][a-zA-Z0-9]*)((?:\"[^\"]*\"|'[^']*'|[^>\"'])*?)(/?)>", body):
        closing, tag, _attrs, selfclose = tag_m.groups()
        tag = tag.lower()
        line = base_line + body[: tag_m.start()].count("\n")
        if tag in VOID or selfclose:
            continue
        if closing:
            if not stack:
                problems.append(f"L{line}: 多余的 </{tag}>")
                continue
            if stack[-1][0] != tag:
                problems.append(
                    f"L{line}: </{tag}> 对不上 —— 当前开着的是 <{stack[-1][0]}>（L{stack[-1][1]}）"
                )
                # 尽量恢复：把栈顶弹掉再看
                stack.pop()
                continue
            stack.pop()
        else:
            stack.append((tag, line))

    if stack:
        problems.append("未闭合: " + ", ".join(f"<{t}>（L{l}）" for t, l in stack))

    if problems:
        for p in problems[:12]:
            print(f"  ❌ {p}")
        return 1
    print("  ✅ 标签完全配平")
    return 0


if __name__ == "__main__":
    target = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "apps/desktop/src/renderer/settings.html")
    print(f"  ═══ {target} ═══")
    raise SystemExit(check(target))
