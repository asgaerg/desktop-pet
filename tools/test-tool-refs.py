r"""检查：源码里有没有**指向已删/不存在文件**的引用（2026-10-16 加）。

## 为什么需要一条常驻检查

2026-10-16 清掉了 129 个一次性探针（`tools/probe-*.py` 等）。
删完发现产品源码里有 **40 处**注释还在说「实测用 `tools/xxx.py` 验证过」——
那些文件已经不存在了，将来有人去找会扑空。

⚠️ 这类"死引用"很隐蔽：不影响运行（只是注释），所以**任何测试都不会报**。

## 🔴 第一版误报 91 处 —— 三个坑（记下来）

### 坑 ① **项目里有两个 `tools/` 目录**

```
F:\ai项目\tools\                 根的（Python 脚本）
F:\ai项目\apps\desktop\tools\   桌面的（门禁 .mjs）
```

`apps/desktop/package.json` 里的 `node tools/check-renderer.mjs` 指的是
**同目录下那个**（相对路径），不是根的。第一版只查根目录，于是把门禁的
**全部 .mjs 都误报成死引用** —— 而门禁刚刚还跑通过，明显不对。

✅ 修法：按**引用者所在目录**解析 —— 引用者自己那层的 `tools/` 优先级最高。

### 坑 ② `tools/list`、`tools/call` 不是文件

那是 **MCP 协议的方法名**（JSON-RPC）。第一版把它们当成文件名查，
于是 `mcp_search.py` 里报了 6 处"死引用"。

✅ 修法：只认**带扩展名的**名字（`.py` / `.mjs` / `.ps1` …），
`tools/list` 没有扩展名，自然排除。

### 坑 ③ `.pnpm-store` 里是第三方包的源码

那里面的 `tools/hex2bin.js` 说的是**别的项目**的 tools。
而且它是包缓存，不该被扫。

## 判据

```
① 只认带扩展名的：`tools/xxx.py` / `tools/xxx.mjs` / 反斜杠那种 .ps1
② 按引用者所在目录解析（自己那层 tools/ 优先，再回退到根）
③ 跳过包缓存、依赖、构建产物、历史文档
```

⚠️ 写这个文件时又踩一次：文档串里写了 Windows 反斜杠路径，
`\x` 被 Python 当成转义序列 -> `SyntaxError: truncated \xXX escape`。
**文档串里写反斜杠路径要用双反斜杠。**
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(r"F:\ai项目")

SKIP_DIRS = {"node_modules", ".venv", "__pycache__", "dist", ".test-build",
             ".backup", "pet-data", "gopath", "goruntime", ".git",
             ".bak-refs", "uv-runtime", ".pnpm-store", "menupreview"}
SKIP_FILES = {"进度交接.md"}  # 历史文档记的是"当时用了什么"，提到已删文件是对的
SKIP_SUFFIX = {".png", ".jpg", ".jpeg", ".wav", ".mp3", ".exe", ".db", ".log",
               ".ico", ".ttf", ".woff", ".woff2", ".map", ".lock"}

# ⚠️ 允许在行内写 `ref-check: ignore` 显式排除。
# 用在**故意举的例子**上（比如本文档里的 `tools/xxx.py` 就是占位符，不是引用）。
# 比起猜"这个名字像不像例子"，显式标记更可靠。
IGNORE_MARK = "ref-check: ignore"

# ⚠️ 必须有扩展名 —— `tools/list`（MCP 方法名）不能被当文件
PATTERN = re.compile(r"`?tools[/\\]([\w.\-]+\.[a-z0-9]{1,5})`?", re.I)

# 🔴 2026-10-16 补：还要认**代码里拼路径**的写法
#
#     src = (ROOT / "tools" / "check-tts-layout.py").read_text()
#     p = Path(root) / "tools" / "foo.py"
#
# ⚠️ 为什么会漏：第一版只匹配文本里的 `` `tools/xxx` ``。
# 清探针时 `probe-status-design.py` 用上面这种写法引用
# `check-tts-layout.py`，**没被查出来**，于是我把它删了 ——
# 结果那个探针直接崩（`FileNotFoundError`）。
#
# 教训：**"没查出来"和"不存在"是两件事**。匹配模式覆盖不到的地方，
# 检查会安静地放过去 —— 而安静正是最危险的。
PATH_PARTS = re.compile(
    r"""["']tools["']\s*/\s*["']([\w.\-]+\.[a-z0-9]{1,5})["']""", re.I,
)


def tools_dirs_for(path: Path) -> list[Path]:
    """按优先级给出这个文件可能引用的 `tools/` 目录。

    自己那层优先（相对路径语义），再回退到项目根。
    """
    dirs = []
    for parent in [path.parent, *path.parents]:
        if parent == ROOT.parent:
            break
        candidate = parent / "tools"
        if candidate.is_dir():
            dirs.append(candidate)
    return dirs


def main() -> int:
    """
    ## ⚠️ 这个检查**只管产品代码**（apps/ / packages/ / config/ / docs/）

    **为什么不扫 `tools/` 自己**：

      ① `tools/` 里的脚本会互相提名字（`照 tools/cdp-voice-state.py 那套写法`），
         那是**历史记录**，不是"要去执行的引用"
      ② 我批删 129 个脚本时**刻意不碰 tools/ 内部**（改了会波及面太大），
         所以 tools/ 里必然留着指向已删脚本的话 —— 那是已知的、有意的
      ③ 让一个有 11 条已知噪声的检查常驻，结果就是**没人再看它的输出**

    ✅ 所以它只对**产品代码**负责：那里出现死引用是真该修的
       （§111 修的就是产品代码里那 40 处 + 后面补的 6 处）。
    """
    print("=" * 88)
    print("  死引用检查（只管产品代码 apps/ packages/ config/ docs/）")
    print("=" * 88)

    watched = [ROOT / "apps", ROOT / "packages", ROOT / "config", ROOT / "docs"]
    root_tools = ROOT / "tools"
    print(f"    根 tools/：{len([p for p in root_tools.iterdir() if p.is_file()])} 个文件")
    for extra in sorted(ROOT.rglob("tools")):
        if extra == root_tools or not extra.is_dir():
            continue
        if any(part in SKIP_DIRS for part in extra.parts):
            continue
        print(f"    {extra.relative_to(ROOT)}/："
              f"{len([p for p in extra.iterdir() if p.is_file()])} 个文件")
    print(f"    监视范围：{[str(w.relative_to(ROOT)) for w in watched if w.is_dir()]}")
    print()

    dead: list[tuple[str, int, str, str]] = []
    checked = 0
    for base in watched:
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            if any(part in SKIP_DIRS for part in path.parts):
                continue
            if path.name in SKIP_FILES or path.suffix in SKIP_SUFFIX:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            checked += 1
            dirs = tools_dirs_for(path)
            for number, line in enumerate(text.splitlines(), 1):
                if IGNORE_MARK in line:
                    continue
                found = [m.group(1) for m in PATTERN.finditer(line)]
                found += [m.group(1) for m in PATH_PARTS.finditer(line)]
                for name in found:
                    if any((d / name).exists() for d in dirs):
                        continue
                    if any(p.name == name for p in ROOT.rglob(name)):
                        continue
                    dead.append((str(path.relative_to(ROOT)), number, line.strip(), name))

    print(f"    扫了 {checked} 个产品文件")
    print()
    if not dead:
        print("    ✅ 产品代码里没有死引用 —— 所有 `tools/xxx.<ext>` 都真实存在")
        print("=" * 88)
        return 0

    print(f"    ❌ {len(dead)} 处死引用（指向不存在的文件）：")
    by_file: dict[str, list[tuple[int, str, str]]] = {}
    for rel, number, line, name in dead:
        by_file.setdefault(rel, []).append((number, line, name))
    for rel in sorted(by_file):
        print(f"    【{rel}】")
        for number, line, name in sorted(by_file[rel]):
            print(f"      L{number}  -> tools/{name} 不存在")
            print(f"          {line[:100]}")
    print()
    print("    修法：把引用改成历史措辞（参见 §111）。")
    print("    故意举的例子可以在行内加 `ref-check: ignore`。")
    print("=" * 88)
    return 1


raise SystemExit(main())
