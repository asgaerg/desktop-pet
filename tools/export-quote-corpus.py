"""把开发库里的语录表导出成可发布的语料文件（2026-10-18）。

## 为什么需要导出

"自言自语"念的句子存在 `pet.db` 的 `quote` 表里（18206 条：古诗 14633、
圣经 2500、道经 462、佛经 277、哲学 112、小说 102、经文 89、胡话 31）。
而 `config/quotes.yaml` 只是 **26 条种子**。

打包时记忆库按设计不进包（那是用户数据），于是**装好的程序只有 26 条**，
自言自语会立刻重复。所以要把这张表导出成第三方可发布的语料文件，
随包带上，首启动导入。

用法：

    python tools/export-quote-corpus.py                 # 写 config/quotes-corpus.jsonl
    python tools/export-quote-corpus.py --db 某库.db

输出格式：每行一个 JSON 对象，字段 kind / text / source。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import pathlib

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "apps" / "desktop" / "pet-data" / "memory" / "pet.db"
OUT = ROOT / "config" / "quotes-corpus.jsonl"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args()

    db = pathlib.Path(args.db)
    if not db.exists():
        print(f"  ❌ 找不到数据库 {db}")
        return 1

    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = con.execute(
        "SELECT kind, text, source FROM quote WHERE text IS NOT NULL AND text <> '' ORDER BY id"
    ).fetchall()
    con.close()

    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8", newline="\n") as fh:
        for kind, text, source in rows:
            text = str(text).replace("\n", " ").strip()
            if not text:
                continue
            fh.write(json.dumps({"kind": kind, "text": text, "source": source or ""}, ensure_ascii=False) + "\n")

    size = out.stat().st_size / 1024
    print(f"  ✅ {out.relative_to(ROOT)}  {len(rows)} 条  {size:,.0f} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
