"""
把 `data/corpus/<类别>/*.txt` 里的句子灌进 `quote` 表（2026-10-18）。

用户要「几百几千个」句子。**不在代码里硬编码**，原因：
  ① 用户以后还会加（看小说自动收集、自己贴喜欢的句子）；
  ② 语料可以随时替换/扩充，代码不用动；
  ③ 硬编码几千行会让这个仓库难以维护（用户自己也说"太慢，不好维护"）。

## 目录约定

```
data/corpus/
  philosophy/   哲学句子（一行一句）
  poem/         古诗（一行一句；长诗按联拆，一行不超过 ~40 字）
  scripture/    经文（一行一段；道德经按章、金刚经按分）
  joke/         段子 / 冷笑话 / 胡话（一行一条）
  novel/        小说名句
```

文件名随意，内容按**行**切；空行和 `#` 开头的行会被跳过。

## 用法

```
python tools/seed-quotes.py            # 灌库，打印每类多少条
python tools/seed-quotes.py --dry-run  # 只看统计，不写库
```

去重：`quote.text` 上有唯一索引，重复句子会被 `INSERT OR IGNORE` 跳过，
所以**可以反复跑**，也可以每次只往目录里加新文件。

## 分类权重

类别不存在时会自动按 `quotes.DEFAULT_KINDS` 补上（胡话/小说/哲学/古诗/经文），
`joke` 这个目录名映射到数据库的 `absurd`（用户说的"胡话"）。
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sqlite3
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "brain"))

# 目录名 -> 数据库里的类别名
DIR_KIND = {
    "philosophy": "philosophy",
    "poem": "poem",
    "scripture": "scripture",
    "joke": "absurd",
    "absurd": "absurd",
    "novel": "novel",
    # 2026-10-18 拆开：圣经节数上万，和道经佛经放一类会把它们淹没
    "dao": "dao",
    "buddhist": "buddhist",
    "bible": "bible",
}

MAX_LEN = 120  # 超过这个长度的"句子"多半是整段没拆，跳过（念经念不了那么长）
MIN_LEN = 4


ATTR_SEP = " —— "


def split_attribution(text: str) -> tuple[str, str]:
    """
    把 `句子 —— 出处` 拆成 (句子, 出处)。

    用户要求每类句子后面都要带出处（哲学=作者名，道佛经=书名，圣经=书名+章节，
    古诗=朝代·作者）。所以语料文件里**直接写全**，由这里切开入库。
    """
    index = text.rfind(ATTR_SEP)
    if index <= 0:
        return text, ""
    body = text[:index].strip()
    source = text[index + len(ATTR_SEP) :].strip()
    if not body or not source:
        return text, ""
    return body, source


CJK = re.compile(r"[\u4e00-\u9fff]")
# 抓取/转换过程会在语料里留下错误页（`WikimediaError。`、`404: Not Found`、CSS 片段）。
# 这些一律不能进库 —— 用户已经在收藏列表里看到过一句 `WikimediaError。 —— 《心經》`。
JUNK_MARKERS = ("Wikimedia", "NotFound", "Not Found", "404", "{", "}", "@media", "http")


def iter_lines(path: pathlib.Path) -> list[str]:
    """
    读一个文件里的句子：按行，去空白、去 `#` 注释、去重。

    ⚠️ 两道**永久闸门**（2026-10-18 加，起因是错误页被当成经文入库）：
      ① 必须含中文 —— 我们的语料全是中文，出现纯英文/纯符号就是抓坏了；
      ② 不含错误页特征（`Wikimedia`/`NotFound`/CSS 花括号/URL）。
    """
    out: list[str] = []
    try:
        raw = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raw = path.read_text(encoding="gbk", errors="replace")
    for line in raw.splitlines():
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        # 去掉行首的序号（"1. " / "一、" 之类），句子本身才是内容
        for sep in ("、", ".", "．"):
            head, _, rest = text.partition(sep)
            if rest and head.strip().isdigit():
                text = rest.strip()
                break
        if not (MIN_LEN <= len(text) <= MAX_LEN):
            continue
        if not CJK.search(text):
            continue
        if any(marker in text for marker in JUNK_MARKERS):
            continue
        out.append(text)
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--corpus", default=str(ROOT / "data" / "corpus"))
    args = parser.parse_args()

    from pet import quotes
    from pet.store import PetDatabase, default_db_path

    corpus = pathlib.Path(args.corpus)
    if not corpus.exists():
        print(f"语料目录不存在：{corpus}")
        return 1

    db_path = default_db_path(ROOT)
    db = PetDatabase(db_path)
    repo = quotes.QuoteRepo(conn=db.conn)
    repo.ensure_kinds()

    total_new = 0
    for folder in sorted(p for p in corpus.iterdir() if p.is_dir()):
        kind = DIR_KIND.get(folder.name)
        if kind is None:
            print(f"  跳过未知类别目录：{folder.name}")
            continue
        lines: list[str] = []
        for file in sorted(folder.rglob("*.txt")):
            lines.extend(iter_lines(file))
        # 同一类内部先去重
        seen: set[str] = set()
        uniq = [x for x in lines if not (x in seen or seen.add(x))]
        if args.dry_run:
            print(f"  {kind:<11} {len(uniq):>5} 条（未写库）")
            continue
        new = 0
        for raw in uniq:
            body, source = split_attribution(raw)
            cur = db.conn.execute(
                "INSERT OR IGNORE INTO quote(kind, text, source) VALUES (?,?,?)",
                (kind, body, source),
            )
            new += cur.rowcount
            # 已存在但出处为空的，补上（例题：道德经那批先入库、后加书名）
            if source:
                db.conn.execute(
                    "UPDATE quote SET source = ? WHERE text = ? AND (source IS NULL OR source = '')",
                    (source, body),
                )
        db.conn.commit()
        total_new += new
        print(f"  {kind:<11} 读到 {len(uniq):>5} 条，新增 {new:>5} 条")

    print()
    print(f"  本次新增 {total_new} 条；库里共 {repo.count()} 条 —— {repo.summary()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
