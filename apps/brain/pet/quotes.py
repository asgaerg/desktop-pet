"""
「玩伴觉得好的句子」表 —— 「自言自语」档念的东西（2026-10-17 用户要求）。

## 用户原话（权威）

> 「自言自语讲的话来自于一个表里面，这里面会存储玩伴觉得好的句子，比如：
>   我个人认为这个意大利面就应该拌42号混凝土……这类的话，包括小说里面一些
>   经典知名度很广的句子比如：手握日月摘星辰，世间无我这般人，后续看小说功能
>   写好，还能让它自己选着自己喜欢的句子加进去。当然，也可以偶尔讲讲
>   道经 / 佛经 / 圣经 ，其中哲学句子多，不乏古诗。」

三句话三个约束：

```
① 念什么由**表**决定，不让模型自由编 —— 编出来的经文是假的，用户会听出来
② 哲学最多，古诗也算（权重 + 条目数双重倾斜）
③ 这张表**会变长**：以后"看小说"功能会让它自己往里加喜欢的句子
```

## 为什么放在 `config/quotes.yaml` 而不是代码里

① 用户可以直接加句子（不用改代码、不用重新打包）；
② 以后"看小说自动收集"要往里**追加**，代码里的常量追加不了；
③ 和 `persona.yaml` 一样属于"用户能看懂也能改"的内容。

## ⚠️ 挑选必须"上一句不要马上重复"

念经的观感全靠"每次都不一样"。随机挑到刚念过的那句会立刻露馅 ——
所以 `pick()` 带一个 `recent` 参数，调用方把最近念过的传进来。
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional


@dataclass(frozen=True)
class Quote:
    """表里的一句。"""

    text: str
    kind: str
    source: str = ""
    # 数据库里的行号（yaml 导入的句子没有，挑出来记 used_count 时用）
    id: Optional[int] = None

    @property
    def label(self) -> str:
        """日志用：`哲学/尼采/人生而自由…`"""
        bits = [self.kind]
        if self.source:
            bits.append(self.source)
        return f"{'/'.join(bits)}/{self.text[:22]}…"


@dataclass
class QuoteTable:
    """一整张表。加载期做一次校验，之后只挑不改。"""

    quotes: list[Quote] = field(default_factory=list)
    # kind -> 权重
    weights: dict[str, float] = field(default_factory=dict)
    # kind -> 中文名（日志里用）
    labels: dict[str, str] = field(default_factory=dict)
    path: Optional[Path] = None

    def __len__(self) -> int:
        return len(self.quotes)

    def by_kind(self, kind: str) -> list[Quote]:
        return [q for q in self.quotes if q.kind == kind]

    def kinds(self) -> list[str]:
        """有权重且**真的有句子**的类别（空类别不参与挑选，否则概率被吃掉）。"""
        return [k for k in self.weights if self.by_kind(k)]

    def pick(self, rng: Optional[random.Random] = None, recent: Iterable[str] = ()) -> Optional[Quote]:
        """
        按类别权重挑一句。

        `recent`：最近念过的原文，**优先回避**（全都念过时才允许重复）。
        返回 None 表示表是空的 —— 调用方自己决定"没词儿了"怎么办。
        """
        rng = rng or random
        kinds = self.kinds()
        if not kinds:
            return None
        recent_set = {r for r in recent if r}
        # ⚠️ **先过滤掉"这一类的句子已经全在 recent 里"的类别**（2026-10-17 改）。
        #
        # 早先是"挑类别 -> 类内回避 -> 回避失败就退回整类"，结果：胡话只有 3 句，
        # 最近 8 句里恰好都在，于是**必然重复**（自检抓到：12 次里重复 1 次）。
        # 念经的观感全靠"每次不一样"，宁可换一类，也不要撞同一句。
        pooled: dict[str, list[Quote]] = {}
        for kind in kinds:
            fresh = [q for q in self.by_kind(kind) if q.text not in recent_set]
            if fresh:
                pooled[kind] = fresh
        if not pooled:
            # 整张表都念过了（表很小才会这样）—— 允许重复，总比哑巴强
            pooled = {kind: self.by_kind(kind) for kind in kinds}
        kinds_weighted = [k for k in pooled for _ in range(max(1, int(self.weights[k])))]
        kind = rng.choice(kinds_weighted)
        return rng.choice(pooled[kind])

    def summary(self) -> str:
        """一行摘要，写日志用：`26 句（哲学 8 / 古诗 6 / 经文 6 / 小说 3 / 胡话 3）`"""
        parts = [f"{self.labels.get(k, k)} {len(self.by_kind(k))}" for k in self.weights]
        return f"{len(self.quotes)} 句（{' / '.join(parts)}）"


def parse_table(data: Any, path: Optional[Path] = None) -> QuoteTable:
    """
    把 yaml 读出来的字典变成 `QuoteTable`。

    ⚠️ 校验很啰嗦，但值得：这张表是**用户手改**的（还可能被"看小说"功能
    程序追加）。一条 kind 写错、或漏了 categories 里的项，都会让某一类
    句子永远挑不到 —— 而那种错**不会报错**，只是"怎么老念哲学"。
    """
    if not isinstance(data, dict):
        raise ValueError("quotes.yaml 顶层必须是映射")

    raw_categories = data.get("categories") or {}
    if not isinstance(raw_categories, dict):
        raise ValueError("quotes.yaml: categories 必须是映射")

    weights: dict[str, float] = {}
    labels: dict[str, str] = {}
    for kind, info in raw_categories.items():
        if not isinstance(info, dict):
            raise ValueError(f"quotes.yaml: categories.{kind} 必须是映射")
        weight = info.get("weight", 1)
        try:
            weight = float(weight)
        except (TypeError, ValueError) as error:
            raise ValueError(f"quotes.yaml: categories.{kind}.weight 不是数字") from error
        if weight <= 0:
            raise ValueError(f"quotes.yaml: categories.{kind}.weight 必须 > 0")
        weights[str(kind)] = weight
        labels[str(kind)] = str(info.get("label") or kind)

    raw_quotes = data.get("quotes") or []
    if not isinstance(raw_quotes, list):
        raise ValueError("quotes.yaml: quotes 必须是列表")

    quotes: list[Quote] = []
    for index, item in enumerate(raw_quotes, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"quotes.yaml: 第 {index} 条不是映射")
        text = str(item.get("text") or "").strip()
        if not text:
            raise ValueError(f"quotes.yaml: 第 {index} 条没有 text")
        if "\n" in text:
            # 换行会让"念一句"变成念一段，而且日志里也没法一行看完
            raise ValueError(f"quotes.yaml: 第 {index} 条里有换行，请写成一句")
        kind = str(item.get("kind") or "").strip()
        if kind not in weights:
            raise ValueError(
                f"quotes.yaml: 第 {index} 条的 kind={kind!r} 不在 categories 里"
                f"（可选：{', '.join(weights) or '（空）'}）"
            )
        quotes.append(Quote(text=text, kind=kind, source=str(item.get("source") or "").strip()))

    return QuoteTable(quotes=quotes, weights=weights, labels=labels, path=path)


def load_table(path: Path) -> QuoteTable:
    """读表。文件不存在时返回**空表**而不是抛错 —— 自言自语不是核心功能，不该拖垮启动。"""
    try:
        import yaml  # 延迟导入：没有 pyyaml 的环境仍能跑其它自检
    except ImportError:  # pragma: no cover
        return QuoteTable(path=path)

    if not path.exists():
        return QuoteTable(path=path)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001 —— 表坏了就当空表，别让启动失败
        return QuoteTable(path=path)
    try:
        return parse_table(data, path)
    except ValueError:
        return QuoteTable(path=path)


def display_text(quote: Quote) -> str:
    """
    念出来/照抄出来的样子：`句子 —— 出处`（2026-10-18 用户要求）。

    出处规则（用户按类别定的）：
        哲学      —— 作者名            （尼采）
        道经/佛经 —— 书名              （《道德经》）
        圣经      —— 书名 + 第几页第几段（《传道书》1:9）
        古诗      —— 朝代·作者          （唐·李白）；不知道朝代就只写作者

    ⚠️ 没有出处就**不要**留个光秃秃的破折号 —— 那比没有更难看。
    """
    if not quote.source:
        return quote.text
    return f"{quote.text} —— {quote.source}"


def direction_for(quote: Quote) -> str:
    """
    把一句句子变成"念经"的舞台提示（**用户轮**，比 system 更管用）。

    ## 为什么强调"别解释出处、别问问题"

    · 念经的味道在于**自顾自地念**。一旦它开始"这句出自《道德经》第八章"，
      就变成讲课了；
    · 问问题会把话题抢回给用户 —— 而「自言自语」这一档大部分时间该聊自己
      （用户原话：「大部分时间都在聊自己读了什么什么」）。
    """
    source = f"（你记得它出自{quote.source}）" if quote.source else ""
    return (
        f"你现在在自言自语，刚想起一句你很喜欢的话：「{quote.text}」{source}"
        "你把这句话念出来，然后说说你自己读到它时想到的东西 —— 可以胡乱联想、"
        "可以说其实没读懂、可以扯到别的句子上去。"
        "别解释这句是哪来的、别考据、别问用户问题，念完就继续说下一句。"
    )


def hint_for(quote: Quote, table: Optional[QuoteTable] = None) -> str:
    """
    进 **system / context_hint** 的那段（告诉模型"你在念表里的句子"）。

    和 `direction_for` 分开是有原因的：`context_hint` 是背景知识，
    放用户轮容易被当成"用户说的"；而"念哪一句"必须是用户轮（见 §129 的教训：
    system 里的内容会被记忆压过去）。
    """
    import contextlib

    del contextlib  # 只为说明此处不依赖外部状态
    count = f"（这张表里现在有 {len(table)} 句）" if table is not None else ""
    return (
        "你正在翻自己收集的好句子 —— 有哲学、古诗、小说里的句子、经文，"
        f"也有你自己都觉得离谱的胡话{count}。你就是靠念这些东西自娱自乐的。"
    )


# ══════════════════════════ 数据库版（2026-10-18） ══════════════════════════
#
# 用户：「这个表太慢，不好维护，用数据库单开一张表来存储」。
# 于是 yaml 退成**一次性种子**，真源变成 `quote` 表。


DEFAULT_KINDS: tuple[tuple[str, str, float], ...] = (
    # (name, label, weight)
    #
    # 权重 = **类别**被抽中的相对概率（用户 2026-10-18 指定，合计 100）：
    #   古诗 30% / 小说 25% / 哲学 21% / 胡话 5% / 经文 5% / 道经 5% / 佛经 5% / 圣经 4%
    #   （哲学 21、圣经 4：用户要求「圣经下去个1%，给哲学多些」）
    #
    # ⚠️ 它不等于"每条句子的概率"：古诗 14,633 条挤在 30% 里，
    # 每首诗被念到的机会依然很小；而佛经只有 20 条，那 20 句会轮得快。
    # `quote.used_count` 会优先挑用得最少的，算是一层补偿。
    ("absurd", "胡话", 5.0),
    ("novel", "小说名句", 25.0),
    ("philosophy", "哲学", 21.0),
    ("poem", "古诗", 30.0),
    ("scripture", "经文", 5.0),
    # 2026-10-18：经文拆成三小类。原因：圣经节数上万，混在同一个 "scripture"
    # 里会把道经/佛经彻底淹没（同一类内等概率），用户要的是三类都能念到。
    ("dao", "道经", 5.0),
    ("buddhist", "佛经", 5.0),
    ("bible", "圣经", 4.0),
)


def choose_mode(rng: random.Random | None = None) -> str:
    """
    念法：`copy`（原样照抄）还是 `muse`（念自己的心得）。

    用户：「一半可能会念自己的心得，一半可能直接按原本照抄输出，其他什么也不加。」
    所以正好 50/50。

    ⚠️ `copy` 必须是**逐字照抄**：不过模型。一旦过模型，它一定会加
    "嗯……"、"这句话让我想起"之类的东西 —— 那就不是"什么也不加"了。
    """
    rng = rng or random
    return "copy" if rng.random() < 0.5 else "muse"


class QuoteRepo:
    """
    `quote` / `quote_kind` 两张表的读写门面。

    ## 连接策略

    自己开一条 sqlite 连接（WAL 下读写都能和主库并存），**惰性**打开：
    构造时不碰磁盘，第一次用时才连 —— 自检里可以塞一个内存库进来。
    """

    def __init__(self, path: Path | str | None = None, conn: Any | None = None) -> None:
        self._path = path
        self._conn = conn
        self.rng = random.Random()

    # ------------------------------------------------------------------ 连接
    def conn(self) -> Any:
        if self._conn is None:
            import sqlite3

            self._conn = sqlite3.connect(str(self._path), timeout=5.0)
            self._conn.row_factory = sqlite3.Row
        return self._conn

    def ensure_kinds(self) -> None:
        """补齐类别（权重/中文名），不动已有行 —— 用户可能改过权重。"""
        try:
            for index, (name, label, weight) in enumerate(DEFAULT_KINDS):
                self.conn().execute(
                    "INSERT OR IGNORE INTO quote_kind(name, label, weight, sort) VALUES (?,?,?,?)",
                    (name, label, weight, index),
                )
            self.conn().commit()
        except Exception:  # noqa: BLE001
            pass

    def import_seed(self, path: Path) -> int:
        """
        把 `config/quotes.yaml` **一次性**导入空表。返回导入条数。

        只在表为空时导入：用户后来在库里加/删的句子，不该被 yaml 覆盖。
        """
        try:
            if self.count() > 0:
                return 0
            table = load_table(path)
            if not len(table):
                return 0
            conn = self.conn()
            for quote in table.quotes:
                conn.execute(
                    "INSERT OR IGNORE INTO quote(kind, text, source) VALUES (?,?,?)",
                    (quote.kind, quote.text, quote.source),
                )
            conn.commit()
            return len(table)
        except Exception:  # noqa: BLE001
            return 0

    def import_corpus(self, path: Path, version: str = "1") -> int:
        """
        把随包发布的语料文件（`config/quotes-corpus.jsonl`）一次性导入。

        ## 为什么要它

        `config/quotes.yaml` 只是 26 条种子，真正在念的 18206 条存在 `quote` 表里
        （古诗 14633 / 圣经 2500 / 道经 462 / 佛经 277 / 哲学 112 / 小说 102 /
        经文 89 / 胡话 31）。打包版拿到的是全新的空库 —— 不导入的话自言自语
        几轮就开始重复。

        ## 幂等

        用 `meta.quote_corpus_version` 记版本：同一个版本只导一次，
        所以升级不会重复灌，用户删过的句子也不会被加回来。
        """
        try:
            if not path or not Path(path).exists():
                return 0
            conn = self.conn()
            row = conn.execute(
                "SELECT value FROM meta WHERE key = 'quote_corpus_version'"
            ).fetchone()
            if row is not None and str(row[0]) == str(version):
                return 0

            imported = 0
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        item = json.loads(line)
                    except ValueError:
                        continue
                    kind = str(item.get("kind") or "poem")
                    text = str(item.get("text") or "").replace("\n", " ").strip()
                    if not text:
                        continue
                    source = str(item.get("source") or "")
                    cur = conn.execute(
                        "INSERT OR IGNORE INTO quote(kind, text, source) VALUES (?,?,?)",
                        (kind, text, source),
                    )
                    imported += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
            conn.execute(
                "INSERT INTO meta(key, value) VALUES('quote_corpus_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(version),),
            )
            conn.commit()
            return imported
        except Exception:  # noqa: BLE001
            # 导入失败不该影响启动 —— 退化成"只有 yaml 种子"的旧行为
            return 0

    # ------------------------------------------------------------------ 读
    def count(self) -> int:
        try:
            row = self.conn().execute("SELECT COUNT(*) FROM quote").fetchone()
            return int(row[0]) if row else 0
        except Exception:  # noqa: BLE001
            return 0

    def weights(self) -> dict[str, float]:
        try:
            rows = self.conn().execute("SELECT name, weight FROM quote_kind ORDER BY sort").fetchall()
            return {str(r[0]): float(r[1]) for r in rows}
        except Exception:  # noqa: BLE001
            return {}

    def pick(self, recent: Iterable[str] = ()) -> Optional[Quote]:
        """
        按类别权重挑一句，**回避最近念过的**（和 yaml 版同一套规则）。

        再做一步 yaml 版没有的：**优先挑用得少的**（`used_count` 升序）。
        表会越来越长（以后"看小说自动收集"），只靠随机的话新句子很久都轮不到。
        """
        try:
            conn = self.conn()
            kinds = [
                str(r[0])
                for r in conn.execute(
                    "SELECT k.name FROM quote_kind k "
                    "WHERE EXISTS (SELECT 1 FROM quote q WHERE q.kind = k.name) ORDER BY k.sort"
                ).fetchall()
            ]
            if not kinds:
                return None
            weight_map = self.weights()
            recent_set = {r for r in recent if r}
            # 优先：没念过的类别
            pooled: dict[str, list[Any]] = {}
            for kind in kinds:
                rows = conn.execute(
                    "SELECT id, kind, text, source FROM quote WHERE kind = ? "
                    "ORDER BY used_count ASC, id ASC LIMIT 200",
                    (kind,),
                ).fetchall()
                fresh = [r for r in rows if str(r[2]) not in recent_set]
                if fresh:
                    pooled[kind] = fresh
            if not pooled:
                pooled = {
                    kind: conn.execute(
                        "SELECT id, kind, text, source FROM quote WHERE kind = ? LIMIT 200", (kind,)
                    ).fetchall()
                    for kind in kinds
                }
            weighted = [k for k in pooled for _ in range(max(1, int(weight_map.get(k, 1.0))))]
            kind = self.rng.choice(weighted)
            # 类内也不再纯随机：前 3 个（用得最少的）里挑
            rows = pooled[kind]
            quote_id, _, text, source = self.rng.choice(rows[:3])
            return Quote(text=str(text), kind=str(kind), source=str(source or ""), id=int(quote_id))
        except Exception:  # noqa: BLE001
            return None

    def mark_used(self, quote_id: Optional[int]) -> None:
        """记一次"念过"（下次优先挑用得少的）。"""
        if not quote_id:
            return
        try:
            self.conn().execute(
                "UPDATE quote SET used_count = used_count + 1, "
                "last_used_at = datetime('now','localtime') WHERE id = ?",
                (int(quote_id),),
            )
            self.conn().commit()
        except Exception:  # noqa: BLE001
            pass

    def summary(self) -> str:
        try:
            labels = {
                str(r[0]): str(r[1])
                for r in self.conn().execute("SELECT name, label FROM quote_kind").fetchall()
            }
            rows = self.conn().execute(
                "SELECT kind, COUNT(*) FROM quote GROUP BY kind"
            ).fetchall()
            parts = [f"{labels.get(str(r[0]), str(r[0]))} {r[1]}" for r in rows]
            return f"{self.count()} 句（{' / '.join(parts)}）"
        except Exception:  # noqa: BLE001
            return "0 句"
