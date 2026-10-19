"""把仓库该提交的东西整理干净，并做一次"密钥扫描"（2026-10-19）。

要解决的事：

1. 重新应用 .gitignore（去掉 32MB 的 websearch-mcp.exe、构建产物）
2. 统计**真正要提交**的体积与文件数
3. 扫一遍这些文件的文本，找硬编码的密钥（sk-…、ghp_…、AKIA… 等）
4. 打印最大的 12 个文件，人工确认没有"不该进库的大件"
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = r"F:\ai"
GIT = r"D:\Git\cmd\git.exe"


def git(*args: str, check: bool = True) -> bytes:
    r = subprocess.run([GIT, *args], cwd=ROOT, capture_output=True)
    if check and r.returncode != 0:
        raise RuntimeError(r.stderr.decode("utf-8", "replace"))
    return r.stdout


# ── 1. 重新 add，让 .gitignore 生效
git("rm", "-r", "--cached", "-q", ".", check=False)
git("add", "-A")

names = git("diff", "--cached", "--name-only", "-z").decode("utf-8", "replace").split("\0")
names = [n for n in names if n]

total = 0
big: list[tuple[int, str]] = []
for n in names:
    p = os.path.join(ROOT, n)
    try:
        size = os.path.getsize(p)
    except OSError:
        continue
    total += size
    big.append((size, n))

print(f"  要提交：{len(names)} 个文件，合计 {total / 1024 / 1024:,.1f} MB")
print("  ── 最大的 12 个 ──")
for size, n in sorted(big, reverse=True)[:12]:
    print(f"   {size / 1024 / 1024:7.2f} MB  {n}")

# ── 2. 密钥扫描
PATTERNS = [
    (re.compile(r"\bsk-[A-Za-z0-9]{20,}"), "OpenAI/DeepSeek 风格的 sk- 密钥"),
    (re.compile(r"\bghp_[A-Za-z0-9]{20,}"), "GitHub PAT"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), "GitHub PAT(新)"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AWS Access Key"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"), "Slack token"),
    (re.compile(r"(?i)(api[_-]?key|secret|token|password)\s*[:=]\s*['\"][A-Za-z0-9_\-]{24,}['\"]"), "赋值式密钥"),
]

TEXT_EXT = {
    ".ts", ".js", ".mjs", ".cjs", ".json", ".yaml", ".yml", ".py", ".md",
    ".html", ".css", ".txt", ".ps1", ".cmd", ".bat", ".vbs", ".toml", ".ini", ".cfg",
}
SKIP_DIRS = ("node_modules", ".venv", "release", "dist", ".git", "goruntime")

hits: list[str] = []
scanned = 0
for n in names:
    if os.path.splitext(n)[1].lower() not in TEXT_EXT:
        continue
    if any(part in SKIP_DIRS for part in n.split("/")):
        continue
    p = os.path.join(ROOT, n)
    try:
        text = open(p, "r", encoding="utf-8", errors="ignore").read()
    except OSError:
        continue
    scanned += 1
    for pat, label in PATTERNS:
        for m in pat.finditer(text):
            snippet = m.group(0)[:24]
            line = text[: m.start()].count("\n") + 1
            hits.append(f"{n}:{line}  {label}  {snippet}…")

print(f"\n  扫描了 {scanned} 个文本文件")
if hits:
    print(f"  ⚠️ 命中 {len(hits)} 处（需要人工确认）")
    for h in hits[:30]:
        print("   ", h)
else:
    print("  ✅ 没有发现硬编码密钥")

# ── 3. 确认敏感文件没进去
FORBIDDEN = ("credentials.json", "settings.json", ".env", ".db", ".log", "websearch-mcp.exe", "pet.exe")
bad = [n for n in names if any(n.endswith(f) or f in n for f in FORBIDDEN)]
print("\n  ── 敏感文件检查 ──")
if bad:
    for b in bad[:10]:
        print("   ⚠️", b)
else:
    print("   ✅ 没有凭据/数据库/日志/二进制程序")
