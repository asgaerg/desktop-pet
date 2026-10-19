"""查最近的对话轮次 —— 看模型**实际回答**了什么关于时间的说法。

## 为什么要查这个

用户报：「它回答我说现在是 12 点，时间还是有问题」。

我需要区分：
  ① 模型答了「12 点」而提示词里写的是 21:xx  -> **模型幻觉**（另一类问题）
  ② 用户问的不是"几点"（比如问"中午吃啥"），模型答了 12 点 -> 正常
  ③ 提示词里的时间真的错了 -> 我的实现问题

（注意：`raw_log` 存的是**对话轮次**，不含 system prompt ——
system prompt 每轮现拼、不落库。所以这里只能看模型说了什么。）
"""

from __future__ import annotations

import pathlib
import sqlite3
import sys

sys.path.insert(0, ".")
from tool_bootstrap import ROOT, setup  # noqa: E402

setup(verbose=False)

DB = ROOT / "apps" / "desktop" / "pet-data" / "memory" / "pet.db"
con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
con.row_factory = sqlite3.Row

print("  ═══ 最近 14 条对话（时间倒序）═══")
rows = list(con.execute("SELECT role, text, created_at FROM raw_log ORDER BY id DESC LIMIT 14"))
for r in reversed(rows):
    who = "我" if r["role"] == "user" else "宠"
    text = (r["text"] or "").replace("\n", " ")
    print(f"    [{r['created_at']}] {who}: {text[:110]}")

print()
print("  ═══ 含时间说法的回答 ═══")
import re

hits = list(
    con.execute(
        "SELECT role, text, created_at FROM raw_log "
        "WHERE text LIKE '%点%' OR text LIKE '%时间%' OR text LIKE '%现在%' "
        "ORDER BY id DESC LIMIT 12"
    )
)
if not hits:
    print("    （没有含「点/时间/现在」的记录）")
for r in hits:
    who = "我" if r["role"] == "user" else "宠"
    print(f"    [{r['created_at']}] {who}: {(r['text'] or '')[:120]}")

print()
print("  ═══ 各表里有没有存过 system prompt ═══")
for table in ("recent_turn", "raw_log", "meta"):
    try:
        cols = [r[1] for r in con.execute(f"PRAGMA table_info({table})")]
        like = " OR ".join(f"{c} LIKE '%当前的真实时间%'" for c in cols if c != "id")
        n = con.execute(f"SELECT COUNT(*) FROM {table} WHERE {like}").fetchone()[0]
        print(f"    {table}: {n} 条含「当前的真实时间」")
    except sqlite3.Error as e:
        print(f"    {table}: {e}")

con.close()
