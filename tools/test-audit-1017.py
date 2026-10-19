r"""自测 audit-devlog-2026-10-17.py 的检查逻辑（2026-10-17）。

## 为什么单独自测而不是真的改产品代码

那个审计的关键是 `strip_comments()` —— 它决定"注释里提到已删的东西"算不算残留。
这**纯字符串逻辑**，改产品代码去验它既不必要又有风险。

所以直接喂它几段构造的文本，断言判定结果：

```python
断言 1  代码里有 DETACHED_PROCESS           -> 算命中（该红）
断言 2  只有注释里有 DETACHED_PROCESS        -> 不算命中（该绿）  <- 第一版栽在这
断言 3  Python 字符串里含 # 不被当注释切掉    -> tokenize 而非正则
断言 4  TS 的块注释被剥掉
断言 5  CSS 的 data-state='voice' 在代码里 -> 命中
```

⚠️ 本项目规矩：**每个新检查都要证明它能红**。这里是给"审计器"本身做双向验证。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

HERE = Path(__file__).parent
spec = importlib.util.spec_from_file_location(
    "audit_1017", HERE / "audit-devlog-2026-10-17.py",
)
# ⚠️ 那个模块结尾就 raise SystemExit(main())，不能直接 import 执行 —— 
#    所以这里只把它的源码读出来，单独取 strip_comments。
source = (HERE / "audit-devlog-2026-10-17.py").read_text(encoding="utf-8")
if "raise SystemExit(main())" in source:
    source = source.replace("raise SystemExit(main())", "")
namespace: dict = {"__name__": "audit_1017"}
exec(compile(source, "audit-devlog-2026-10-17.py", "exec"), namespace)
strip_comments = namespace["strip_comments"]

passed = 0
failed: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global passed
    if cond:
        passed += 1
        print(f"  ✅ {name}")
    else:
        failed.append(name)
        print(f"  ❌ {name}")
        if detail:
            print(f"       {detail}")


print("=" * 78)
print("  自测：strip_comments 的四种形态")
print("=" * 78)

# ── 1. Python：代码里的常量必须留下 ──
py_code = (
    'flags = 0\n'
    'for name in ("DETACHED_PROCESS", "CREATE_NO_WINDOW"):\n'
    '    flags |= getattr(subprocess, name, 0)\n'
)
out = strip_comments(py_code, ".py")
check(
    "Python 代码里的 DETACHED_PROCESS 会被检出（该红）",
    "DETACHED_PROCESS" in out,
    f"剥离后：{out!r}",
)

# ── 2. Python：只在注释里必须被剥掉（这是第一版假阳性的根源）──
py_comment = (
    "# ⚠️ 之前这里还加了 DETACHED_PROCESS —— 但它和 CREATE_NO_WINDOW 互斥\n"
    'for name in ("CREATE_NO_WINDOW", "CREATE_NEW_PROCESS_GROUP"):\n'
    '    flags |= getattr(subprocess, name, 0)\n'
)
out = strip_comments(py_comment, ".py")
check(
    "只出现在注释里的 DETACHED_PROCESS 不被检出（该绿）",
    "DETACHED_PROCESS" not in out,
    f"剥离后仍含：{out!r}",
)

# ── 3. Python：字符串里的 # 不能被当注释（正则实现会误切）──
py_hash = 'msg = "# 这不是注释"  # 这才是注释\n'
out = strip_comments(py_hash, ".py")
check(
    "Python 字符串里的 # 不被当注释切掉",
    "这不是注释" in out,
    f"剥离后：{out!r}",
)

# ── 4. TS：块注释 + 行注释都要剥，字符串常量要留 ──
ts = (
    '/**\n * 🔴 \'voice\'（语音准备中）2026-10-17 删\n */\n'
    "export type StatusKind = 'idle' | 'speaking';\n"
    "// 'voice' 也删了\n"
)
out = strip_comments(ts, ".ts")
check(
    "TS 块注释里的 'voice' 被剥掉",
    "2026-10-17 删" not in out,
    f"剥离后：{out!r}",
)
check(
    "TS 代码里的 'speaking' 保留",
    "'speaking'" in out,
    f"剥离后：{out!r}",
)

# ── 5. CSS：真的 data-state='voice' 算命中 ──
css_code = ".light[data-state='voice'] { color: #32ade6; }\n"
out = strip_comments(css_code, ".css")
check(
    "CSS 代码里的 data-state='voice' 会被检出（该红）",
    "state='voice'" in out,
    f"剥离后：{out!r}",
)

# ── 6. CSS：注释里的 data-state='voice' 不算 ──
css_comment = "/* 删掉了 .light[data-state='voice'] 这条规则 */\n.light[data-state='idle'] { color: #34c759; }\n"
out = strip_comments(css_comment, ".css")
check(
    "CSS 注释里的 data-state='voice' 不被检出（该绿）",
    "state='voice'" not in out,
    f"剥离后：{out!r}",
)

print()
print("=" * 78)
print(f"  结果：{passed}/{passed + len(failed)} 通过")
if failed:
    for name in failed:
        print(f"    ❌ {name}")
print("=" * 78)
raise SystemExit(1 if failed else 0)
