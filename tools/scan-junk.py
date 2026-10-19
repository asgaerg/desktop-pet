"""扫描项目里的冗余数据（只报告，不删）。

## 分类

  ① **构建产物 / 测试产物** —— `.test-build/`、`dist/` 之类
  ② **缓存** —— `__pycache__`、`.pytest_cache`、`node_modules/.cache`
  ③ **临时目录** —— `pet-data/*-tmp`、`_staging`、日志
  ④ **旧备份** —— `*.bak`、`backup-before-*`
  ⑤ **长期未改动的可疑文件** —— 按 mtime 排序，人工判断
  ⑥ **大文件**（> 20 MB）—— 逐个说明它是什么、还有没有用

## 原则

**只报告 + 标注依据**（谁引用它、多久没动），删除决定单独做。
上次我按"看起来没用"删过东西，差点误伤 —— 这次每一项都要有依据。
"""

from __future__ import annotations

import pathlib
import re
from collections import defaultdict

ROOT = pathlib.Path(r"F:\ai项目")


def size_of(p: pathlib.Path) -> int:
    if p.is_file():
        try:
            return p.stat().st_size
        except OSError:
            return 0
    total = 0
    for f in p.rglob("*"):
        try:
            if f.is_file():
                total += f.stat().st_size
        except OSError:
            pass
    return total


def mb(n: int) -> str:
    return f"{n / 1048576:,.1f} MB"


def mtime(p: pathlib.Path) -> str:
    try:
        import datetime

        return datetime.datetime.fromtimestamp(p.stat().st_mtime).strftime("%m-%d %H:%M")
    except OSError:
        return "?"


print("  ═══════════════════════════════════════════════════════════")
print("  ① 构建 / 测试产物")
print("  ═══════════════════════════════════════════════════════════")
groups: dict[str, list[pathlib.Path]] = {
    ".test-build 下每个测试目录": sorted(
        (ROOT / "apps" / "desktop" / ".test-build").glob("*")
    ),
    "dist（构建输出）": [ROOT / "apps" / "desktop" / "dist"],
}
for name, paths in groups.items():
    print(f"\n  ── {name} ──")
    total = 0
    for p in paths:
        if not p.exists():
            continue
        s = size_of(p)
        total += s
        print(f"    {mb(s):>12}  {mtime(p)}  {p.name}")
    print(f"    {'-' * 12}")
    print(f"    {mb(total):>12}  合计")

print()
print("  ═══════════════════════════════════════════════════════════")
print("  ② 缓存")
print("  ═══════════════════════════════════════════════════════════")
caches: dict[str, list[pathlib.Path]] = defaultdict(list)
for pat in ("__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache"):
    for p in ROOT.rglob(pat):
        if "node_modules" in p.parts or ".venv" in p.parts:
            continue
        caches[pat].append(p)
for name, paths in caches.items():
    total = sum(size_of(p) for p in paths)
    print(f"    {mb(total):>12}  {len(paths):>4} 个  {name}")

print()
print("  ═══════════════════════════════════════════════════════════")
print("  ③ pet-data 下的临时 / 备份目录")
print("  ═══════════════════════════════════════════════════════════")
pd = ROOT / "apps" / "desktop" / "pet-data"
for p in sorted(pd.iterdir()):
    if not p.is_dir():
        continue
    s = size_of(p)
    n = sum(1 for _ in p.rglob("*") if _.is_file())
    flag = ""
    if "tmp" in p.name or "staging" in p.name or "backup-before" in p.name:
        flag = "  <- 临时/备份"
    print(f"    {mb(s):>12}  {n:>5} 文件  {mtime(p)}  {p.name}{flag}")

print()
print("  ═══════════════════════════════════════════════════════════")
print("  ④ 散落的备份 / 临时文件")
print("  ═══════════════════════════════════════════════════════════")
junk: list[pathlib.Path] = []
for pat in ("*.bak", "*.bak.*", "*.orig", "*.old", "*~", "*.tmp", "*.log"):
    for p in ROOT.rglob(pat):
        if any(x in p.parts for x in ("node_modules", ".venv", ".test-build", ".git")):
            continue
        junk.append(p)
junk.sort()
for p in junk:
    print(f"    {mb(size_of(p)):>12}  {mtime(p)}  {p.relative_to(ROOT)}")
if not junk:
    print("    （无）")

print()
print("  ═══════════════════════════════════════════════════════════")
print("  ⑤ 大文件（> 20 MB，node_modules/.venv 除外）")
print("  ═══════════════════════════════════════════════════════════")
big: list[tuple[int, pathlib.Path]] = []
for p in ROOT.rglob("*"):
    if not p.is_file():
        continue
    if any(x in p.parts for x in ("node_modules", ".venv", ".git")):
        continue
    s = size_of(p)
    if s > 20 * 1048576:
        big.append((s, p))
big.sort(reverse=True)
grouped: dict[str, list[tuple[int, pathlib.Path]]] = defaultdict(list)
for s, p in big:
    rel = p.relative_to(ROOT)
    # 按"顶层数据目录"归组，避免刷屏
    key = str(rel.parts[0]) + "/" + (str(rel.parts[1]) if len(rel.parts) > 1 else "")
    grouped[key].append((s, p))
for key in sorted(grouped, key=lambda k: -sum(s for s, _ in grouped[k])):
    items = grouped[key]
    total = sum(s for s, _ in items)
    print(f"\n    {mb(total):>12}  {key}  （{len(items)} 个文件）")
    for s, p in items[:4]:
        print(f"        {mb(s):>12}  {p.relative_to(ROOT)}")
    if len(items) > 4:
        print(f"        … 还有 {len(items) - 4} 个")
