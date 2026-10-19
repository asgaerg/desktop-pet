"""查最近几轮对话里**模型实际收到的 system prompt**。

## 为什么要直接查库

用户报：「它回答我说现在是 12 点，时间还是有问题」——而系统时间是 21:06。

两种可能，**必须靠原文区分**：

  ① 提示词里给的时间就是错的（我的实现有问题）
  ② 提示词里给的是对的，模型**自己说错了**（那是模型幻觉，另一类问题）

不去看原文就只能猜。
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
import sys

sys.path.insert(0, ".")
from tool_bootstrap import ROOT, setup  # noqa: E402

setup(verbose=False)

DB = ROOT / "apps" / "desktop" / "pet-data" / "memory" / "pet.db"
print(f"  库: {DB}  （{DB.stat().st_size // 1024}KB）")

con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
con.row_factory = sqlite3.Row

tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
print(f"  表: {tables}")
print()

# 找带 ts / text / raw 的表
for table in tables:
    cols = [r[1] for r in con.execute(f"PRAGMA table_info({table})")]
    if not cols:
        continue
    print(f"  {table}: {cols}")

print()
print("  ═══ 最近 6 条记录（找 system prompt）═══")
for table in tables:
    cols = [r[1] for r in con.execute(f"PRAGMA table_info({table})")]
    if "text" not in cols and "raw" not in cols:
        continue
    order = "ts DESC" if "ts" in cols else "rowid DESC"
    try:
        rows = list(con.execute(f"SELECT * FROM {table} ORDER BY {order} LIMIT 6"))
    except sqlite3.Error as e:
        print(f"    {table}: 查询失败 {e}")
        continue
    for r in rows:
        blob = ""
        for key in ("raw", "text", "payload", "data"):
            if key in cols and r[key]:
                blob = str(r[key])
                break
        if not blob:
            continue
        # 只关心带 system prompt 的
        if "当前的真实时间" in blob or "system" in blob[:200].lower():
            print(f"    --- {table} ts={r['ts'] if 'ts' in cols else '?'} ---")
            try:
                obj = json.loads(blob)
                msgs = obj.get("messages") or obj.get("request", {}).get("messages") or []
                for m in msgs:
                    if m.get("role") == "system":
                        content = str(m.get("content") or "")
                        print("      【system prompt 原文】")
                        for line in content.split("\n"):
                            print(f"        {line[:150]}")
                        break
                else:
                    print(f"      （这条没有 system；顶层键：{list(obj)[:8]}）")
            except json.JSONDecodeError:
                idx = blob.find("当前的真实时间")
                if idx >= 0:
                    print(f"      【原文片段】{blob[idx:idx + 200]}")
                else:
                    print(f"      （非 JSON；前 160 字）{blob[:160]}")

con.close()
