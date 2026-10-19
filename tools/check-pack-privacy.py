"""打包产物隐私体检：包里**不该有**任何用户数据（2026-10-18）。

## 为什么单独一个脚本

`check-pack-secrets.py` 查的是"API 密钥有没有泄漏"，
这个查的是"**用户数据**有没有被打进安装包" —— 两类事故，两个护栏。

## 触发它的那次事故

打完包做检查时发现：

    release/win-unpacked/resources/apps/desktop/pet-data/vendor/
      simple/libsimple-windows-x64/mig-19negrr1/t.db      2.3 MB

打开一看表结构是 `raw_log / memory / memory_vec / cherished`，
**437 轮真实对话**（2026-10-06 之前）。来源：10-06 那次
"记忆库默认路径按 cwd 算"的 bug，sidecar 的 cwd 恰好是词典目录，
于是数据库被建在了 `vendor/simple/.../mig-*/` 里；后来路径修好了，
这份旧库留在原地，而打包用的是"整个 vendor 目录照搬"，就一起带走了。

## 规则

包里只允许出现**资源**（模型 / 词典 / dll / ps1 / 配置模板），
不允许出现：数据库、日志、凭据、设置、备份。

用法：

    python tools/check-pack-privacy.py release/win-unpacked
    python tools/check-pack-privacy.py release-offline/win-unpacked
"""

from __future__ import annotations

import pathlib
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 扩展名 + 文件名黑名单
BAD_SUFFIXES = {".db", ".db-wal", ".db-shm", ".sqlite", ".sqlite3", ".jsonl", ".log", ".bak"}
BAD_NAMES = {
    "credentials.json",
    "settings.json",
    "persona.yaml.bak",
    "instance.lock.json",
}

# 允许出现的例外（配置文件模板是程序运行必需的）
ALLOW_NAMES = {
    "persona.yaml",
    "quotes.yaml",
    # 语料库：18206 条诗句/经文，**故意随包发**的初始内容（不是用户数据）。
    # 见 tools/export-quote-corpus.py 与 QuoteRepo.import_corpus()。
    "quotes-corpus.jsonl",
    "manifest.json",
    "avatar.json",
    "move_strides.json",
}


def scan(root: pathlib.Path) -> int:
    print(f"  扫描 {root}")
    bad: list[pathlib.Path] = []
    total = 0
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        total += 1
        name = p.name.lower()
        if name in ALLOW_NAMES:
            continue
        if name in BAD_NAMES or p.suffix.lower() in BAD_SUFFIXES:
            bad.append(p)
    print(f"  共 {total} 个文件")
    if not bad:
        print("  ✅ 没有数据库 / 日志 / 凭据 / 设置文件")
        return 0
    print(f"  ❌ 发现 {len(bad)} 个不该打包的文件：")
    for p in bad[:30]:
        size = p.stat().st_size / 1024
        print(f"     {p.relative_to(root)}  {size:,.0f} KB")
    if len(bad) > 30:
        print(f"     …… 还有 {len(bad) - 30} 个")
    print("\n  这些是**用户数据**，不能随安装包发出去。")
    return 1


def main(argv: list[str]) -> int:
    targets = [pathlib.Path(a) for a in argv[1:]]
    if not targets:
        print("  用法：python tools/check-pack-privacy.py <打包目录> [更多目录…]")
        return 2
    worst = 0
    for t in targets:
        if not t.exists():
            print(f"  跳过（不存在）：{t}")
            continue
        worst = max(worst, scan(t))
    return worst


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
