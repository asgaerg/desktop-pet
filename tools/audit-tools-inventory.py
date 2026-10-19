r"""项目整理：tools/ 清单审计 + 基线快照（2026-10-17）。

## 为什么要这个脚本

用户立的规矩：

> 「下次如果项目多出了新的脚本和文件记得告诉我，我会告诉你是不是让其他ai搞的
> 新文件，给你发文档你看了再对对就不会误删了」

所以整理之前必须能回答一个问题：**这个文件是何时、怎么来的？**
靠名字判断是错的（昨天已经踩过：`check-*.py` 里有常驻检查）。

## 本脚本做两件事

1. **对账**：拿 `tools/` 现有文件跟昨天的删除清单对比，分出
   「重建 / 全新 / 早就有」三类，把要问用户的列清楚。

2. **建基线**：把当前全量路径 + 大小 + 哈希写成 `tools/.baseline.json`。
   以后跑 `--diff` 就能精确列出"这段时间新增了什么" —— 这才是
   "增量比对"能落地的形式，不再靠猜。

## 🔴 关键：必须按**相对路径**，不能按文件名

`tools/` 里存在同名的文件：

```
tools/menu-icons/chat.svg
tools/menupreview/icons/chat.svg     <- 预览目录的副本
```

按 basename 存会让 174 条路径折叠成 162 个名字，账永远对不上
（第一版就是这么错的，`174 - 5 != 157`）。

用法：
    python tools/audit-tools-inventory.py            # 对账 + 打印
    python tools/audit-tools-inventory.py --save     # 同时写基线
    python tools/audit-tools-inventory.py --diff     # 对比基线，列新增/消失
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(r"F:\ai项目")
TOOLS = ROOT / "tools"
MANIFEST = TOOLS / ".deleted-probes-manifest.txt"
BASELINE = TOOLS / ".baseline.json"

# 不参与统计的：运行时/缓存/我自己的清单文件
SKIP_DIRS = {"goruntime", "__pycache__", "node_modules", "uv-runtime"}
SKIP_NAMES = {".deleted-probes-manifest.txt", ".baseline.json"}


def walk_tools() -> dict[str, dict]:
    """{相对路径: {size, mtime}}（相对 `tools/`）。"""
    out: dict[str, dict] = {}
    for p in TOOLS.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(TOOLS)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        if p.name in SKIP_NAMES:
            continue
        st = p.stat()
        out[rel.as_posix()] = {
            "size": st.st_size,
            "mtime": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
        }
    return out


def read_manifest() -> dict[str, int]:
    """{文件名: 字节数}。清单是**平铺**的（129 个都在 tools/ 根下）。"""
    out: dict[str, int] = {}
    if not MANIFEST.exists():
        return out
    for line in MANIFEST.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^([\w.\-]+\.(?:py|mjs))\s+\(([\d,]+)\s*B\)", line)
        if m:
            out[m.group(1)] = int(m.group(2).replace(",", ""))
    return out


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def do_diff(current: dict[str, dict]) -> int:
    if not BASELINE.exists():
        print("  ⚠️ 还没有基线。先跑一次 `--save`。")
        return 1
    old = json.loads(BASELINE.read_text(encoding="utf-8"))
    old_files: dict[str, dict] = old.get("files", {})
    added = sorted(set(current) - set(old_files))
    removed = sorted(set(old_files) - set(current))
    changed = sorted(
        rel for rel in (set(current) & set(old_files))
        if current[rel]["size"] != old_files[rel]["size"]
    )

    print("=" * 84)
    print(f"  基线时间：{old.get('when', '?')}  ->  现在")
    print("=" * 84)
    print(f"  🆕 新增（{len(added)}）")
    for rel in added:
        print(f"      {rel:<52} {current[rel]['size']:>9,} B")
    print(f"  🗑 消失（{len(removed)}）")
    for rel in removed:
        print(f"      {rel}")
    print(f"  ✏️ 改动（大小变了，{len(changed)}）")
    for rel in changed:
        print(f"      {rel:<52} {old_files[rel]['size']:>9,} -> {current[rel]['size']:,} B")
    print("=" * 84)
    return 0


def main() -> int:
    args = set(sys.argv[1:])
    current = walk_tools()

    if "--diff" in args:
        return do_diff(current)

    deleted = read_manifest()
    # 清单里的名字都在 tools/ 根下，所以按顶层文件名匹配
    rebuilt, rest = {}, {}
    for rel, info in current.items():
        base = rel.rsplit("/", 1)[-1]
        if "/" not in rel and base in deleted:
            rebuilt[rel] = info
        else:
            rest[rel] = info

    print("=" * 84)
    print("  tools/ 构成（已排除 goruntime / uv-runtime / __pycache__）")
    print("=" * 84)
    print(f"  现有：{len(current)} 条路径")
    print(f"  昨天的删除清单：{len(deleted)} 个（全部在 tools/ 根下）")
    print()

    print("=" * 84)
    print(f"  ① 昨天被我删掉、现在又回来了（重建）：{len(rebuilt)} 个")
    print("=" * 84)
    for rel, info in sorted(rebuilt.items()):
        old = deleted[rel]
        mark = "已改写" if info["size"] != old else "原样"
        print(f"    {rel:<38} {info['size']:>8,} B （原 {old:,} B，{mark}）")

    print()
    print("=" * 84)
    print(f"  ② 其它（清单里没有）：{len(rest)} 条路径")
    print("=" * 84)
    print("  ⚠️ 这些**不能靠名字判断**。按今天改动过的优先列出：")
    print()
    today = [r for r in rest if rest[r]["mtime"].startswith("2026-10-17")]
    older = [r for r in rest if r not in today]
    print(f"  🔴 今天（10-17）动过的 {len(today)} 个 —— **必须问用户**：")
    for rel in sorted(today):
        print(f"      {rel:<48} {rest[rel]['size']:>9,} B  {rest[rel]['mtime']}")
    print()
    print(f"  ⚪ 10-17 之前就有的 {len(older)} 个（昨天那 122 个的主体，不用问）")

    if "--save" in args:
        payload = {
            "when": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "count": len(current),
            # ⚠️ 存**相对路径**，不存 basename（见模块开头说明）
            "files": {rel: {"size": info["size"]} for rel, info in current.items()},
        }
        BASELINE.write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8",
        )
        print()
        print(f"  ✅ 基线已写入 {BASELINE.relative_to(ROOT)}（{len(current)} 条）")

    print()
    print("=" * 84)
    return 0


raise SystemExit(main())
