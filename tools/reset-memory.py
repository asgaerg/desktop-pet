"""
清空记忆库，把宠物恢复到"刚认识你"的状态。

## ⚠️ 这是**不可逆**操作

走产品代码 `MemoryService.clear()` —— 它会按正确顺序清五张表、
rebuild FTS 索引、再 `VACUUM` 把页里的残留真正释放掉。

**清掉的包括 `raw_log`（原文底本）** —— 这是刻意的：
`raw_log` 平时"永不淘汰"，但用户主动说"忘掉一切"时它必须也删，
否则"记忆里还留着我们全部对话"就是假的。

## 用法

    python tools/reset-memory.py                # 只看会删什么，不动数据
    python tools/reset-memory.py --backup       # 先备份再清空（推荐）
    python tools/reset-memory.py --backup --yes # 一条命令走完

**加 `--yes` 才真的删** —— 没有它只打印清单。这样手滑跑一次不会毁掉数据。
"""

from __future__ import annotations

import argparse
import datetime
import sqlite3
import sys
from pathlib import Path

from tool_bootstrap import ROOT, setup  # noqa: E402

setup(verbose=False)

from pet.config import load_config  # noqa: E402
from pet.memory2 import MemoryService  # noqa: E402

TABLES = ("memory_vec", "memory", "cherished", "recent_turn", "raw_log")


def db_file() -> Path:
    return ROOT / "apps" / "desktop" / "pet-data" / "memory" / "pet.db"


def counts(db: Path) -> dict[str, int]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    out: dict[str, int] = {}
    try:
        for t in TABLES:
            try:
                out[t] = int(con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])
            except sqlite3.Error:
                out[t] = -1
    finally:
        con.close()
    return out


def compact(db: Path) -> bool:
    """
    把数据库**压实**（等价于 VACUUM），返回是否成功。

    ## ⚠️ 为什么不能直接用 `VACUUM`（2026-10-07 实测踩到）

    清空之后文件大小**一点没变**（2.42 MB），而 `VACUUM` 抛：

        sqlite3.OperationalError: unable to open database file

    实测定位：**不是权限问题**（同目录能写、`TEMP` 也能写），
    而是 SQLite 给 VACUUM 建临时文件时选的那个落点在这个环境里打不开。
    而 **`VACUUM INTO '<显式路径>'` 是成功的** —— 所以改成"压实到新文件再替换"：

        ① `VACUUM INTO <库同目录的临时名>`   —— 产出一个干净的紧凑副本
        ② 老文件改名 `.old`（**留后路**，不是直接删）
        ③ 新文件改名为正式名
        ④ 删掉 `-wal` / `-shm`（它们属于老文件）

    ## 为什么必须压实（不只是为了省空间）

    不压实的话，**被删的正文还留在文件里**。实测：清空后直接搜 `pet.db`，
    `你好呀` 还能搜到 159 次（FTS 索引的词元与旧页）。行数看着是 0，
    但"数据真的没了吗"这个问题的答案是**否** —— 对"我要重新开始"这种
    带隐私诉求的操作，那是不合格的。
    """
    tmp = db.with_name(db.name + ".compact")
    old = db.with_name(db.name + ".old")
    for p in (tmp, old):
        if p.exists():
            p.unlink()

    con = sqlite3.connect(str(db))
    try:
        con.execute(f"VACUUM INTO '{tmp.as_posix()}'")
        con.commit()
    except sqlite3.Error as exc:
        print(f"  ❌ 压实失败：{type(exc).__name__}: {exc}")
        return False
    finally:
        con.close()

    if not tmp.is_file():
        print("  ❌ 压实产物没生成")
        return False

    # 安全替换：老文件先改名保留，新的再顶上
    try:
        db.rename(old)
        tmp.rename(db)
    except OSError as exc:
        print(f"  ❌ 替换失败：{exc}（老文件在 {old.name}，没有丢）")
        return False

    # WAL/SHM 属于老文件，留着会让新库读到旧内容
    for suffix in ("-wal", "-shm"):
        p = Path(str(db) + suffix)
        if p.exists():
            p.unlink()

    old.unlink()  # 替换成功后才删老文件
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="清空记忆库（不可逆）")
    ap.add_argument("--yes", action="store_true", help="确认执行（没有它只打印清单）")
    ap.add_argument("--backup", action="store_true", help="清空前先备份")
    args = ap.parse_args()

    db = db_file()
    if not db.is_file():
        print(f"  没找到数据库：{db}")
        return 1

    before_size = db.stat().st_size
    before = counts(db)
    print("=" * 78)
    print("当前记忆库")
    print("=" * 78)
    print(f"  文件: {db}")
    print(f"  大小: {db.stat().st_size / 1024 / 1024:.2f} MB")
    print()
    label = {
        "raw_log": "原文底本（一字不改的全部对话）",
        "recent_turn": "最近记忆（注入提示词用）",
        "memory": "检索层（提炼过的事实）",
        "memory_vec": "向量",
        "cherished": "珍藏记忆",
    }
    total = 0
    for t in TABLES:
        n = before.get(t, -1)
        if n > 0:
            total += n
        print(f"    {t:<14} {n:>6} 行   {label.get(t, '')}")
    print(f"    {'合计':<14} {total:>6} 行")
    print()
    print("  ⚠️ 清空后 **不可还原**（除非你之前备份过）")
    print("  ⚠️ 连原文底本一起删 —— 这是『忘掉一切』的语义，不是 bug")

    if not args.yes:
        print()
        print("  这是**预览**（没动数据）。确认要清就加 `--yes`：")
        print("      python tools/reset-memory.py --backup --yes")
        return 0

    # ---- 备份 ----
    if args.backup:
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        dst = ROOT / "apps" / "desktop" / "pet-data" / "backups" / f"pet-before-reset-{stamp}.db"
        dst.parent.mkdir(parents=True, exist_ok=True)
        s = sqlite3.connect(str(db))
        d = sqlite3.connect(str(dst))
        with d:
            s.backup(d)
        d.close()
        s.close()
        print()
        print(f"  已备份 -> {dst.relative_to(ROOT)}  {dst.stat().st_size / 1024 / 1024:.2f} MB")

    # ---- 清空（走产品代码）----
    cfg = load_config(ROOT / "config" / "persona.yaml")
    mem = MemoryService(cfg)
    print()
    print("  正在清空…")
    removed = mem.clear()
    mem.close()

    # ---- 压实（走产品代码清不干净的那一步）----
    print()
    print("  正在压实（把已删数据的页真正回收）…")
    compacted = compact(db)

    after = counts(db)
    print()
    print("=" * 78)
    print(f"完成：删了 {removed} 行" + ("，已压实" if compacted else "，⚠️ 压实失败"))
    print("=" * 78)
    ok = True
    for t in TABLES:
        n = after.get(t, -1)
        mark = "✅" if n == 0 else "❌"
        if n != 0:
            ok = False
        print(f"    {mark} {t:<14} {before.get(t, 0):>6} -> {n} 行")
    print()
    print(f"  文件大小: {db.stat().st_size / 1024 / 1024:.2f} MB（原来 {before_size / 1024 / 1024:.2f} MB）")

    # ---- 抽查：被删的正文是否真的不在文件里了 ----
    if ok and compacted:
        probe = _probe_leftover_text(db)
        print()
        if probe is None:
            print("  （没有可比对的样本，跳过残留抽查）")
        elif probe:
            ok = False
            print(f"  ⚠️ 文件里仍能搜到旧内容：{probe}")
        else:
            print("  ✅ 抽查通过：旧正文已不在文件里")

    print()
    print("  结果：" + ("✅ 已清空，可以从零开始注入新数据" if ok else "❌ 还有残留"))
    return 0 if ok else 1


def _probe_leftover_text(db: Path) -> list[str] | None:
    """
    在**备份文件**里找几条真实正文，再去新库里搜 —— 判断数据是不是真没了。

    ⚠️ 为什么不硬编码几个词：那些词只对"这一次的数据"有意义，换个库就测了个寂寞。
    正确做法是**从备份里取样本**（那是刚被删掉的东西），再拿它搜新库。
    """
    backups = sorted(
        (ROOT / "apps" / "desktop" / "pet-data" / "backups").glob("*.db"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    sample: list[str] = []
    for b in backups:
        try:
            con = sqlite3.connect(f"file:{b}?mode=ro", uri=True)
            rows = con.execute(
                "SELECT text FROM raw_log WHERE LENGTH(text) >= 6 ORDER BY id DESC LIMIT 5"
            ).fetchall()
            con.close()
        except sqlite3.Error:
            continue
        sample = [str(r[0]) for r in rows]
        if sample:
            break
    if not sample:
        return None

    blob = db.read_bytes()
    hits = [t for t in sample if t.encode("utf-8") in blob]
    return hits


if __name__ == "__main__":
    raise SystemExit(main())
