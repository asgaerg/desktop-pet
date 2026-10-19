"""记忆数据库：五张表 + 中文全文检索 + 向量索引。

## 为什么从 JSONL 换成 SQLite

原设计（`memory.py`）用 JSONL，理由写在那个文件的注释里：「记忆规模是几十到几千条，
JSONL 追加写入崩溃不会毁库、可人工打开检查」。那时候是对的。
但用户把需求提到了另一个量级 —— **原文全量永久保留**、**珍藏记忆**、**时间窗口注入**、
**按标签分类** —— JSONL 就开始难受了：

  · 按时间/标签筛选要**全文件扫描**（实测 SQLite 索引查询 1.07ms，JSONL 得读完整个文件）
  · 向量在 JSON 里是文本（`[0.12, -0.83, ...]`），**体积是二进制的 2 倍**
  · 没有事务，写到一半崩溃要靠"按 id 取最后一条"来兜（现有代码就是这么兜的）
  · "原文全量保留"会让文件涨到几十 MB，每次读都要全量解析

SQLite 同时满足"零第三方依赖"（`sqlite3` 是标准库）和以上全部。

## 五张表的职责（用户定的分层）

    ① recent_turn  最近记忆：完整轮次，**不提炼**，按时间窗口**整段注入**
    ② raw_log      原文记忆：一字不改的全部对话，**永不淘汰**，不参与检索
    ③ cherished    珍藏记忆：用户手动标记的段落，**永不淘汰**，可固定注入
    ④ memory       检索记忆：**唯一**被提炼的一层，走搜索
    ⑤ memory_vec   向量索引：与 ④ 分开，换 embedding 模型时只重建这张

用户特别强调的分工：
  · 最近 / 原文 / 珍藏 **都是纯文本，都不提炼** —— 只有 ④ 需要提炼和向量
  · 时间戳**不进向量**，它是"标记和索引"，供后续分类查看
  · 向量与文本分表，靠 id 联表 —— 这样"时间也分开"、大数据量也能快速 JOIN

## ⚠️ 中文全文检索：`simple` 扩展的正确用法

`simple.dll` 的设计是「**索引按单字建立，精度靠查询改写**」（作者本人的说明）。
所以有两条必须遵守：

    ① 索引用 `tokenize='simple'`
    ② 查询必须用 `jieba_query()` 改写，**不能**直接把用户输入丢给 MATCH
       错误：`WHERE fts MATCH '咖啡'`              -> 单字 AND，噪音大
       正确：`WHERE fts MATCH jieba_query('咖啡')`  -> 词组匹配

    ③ cppjieba 用**相对路径** `./dict/jieba.dict.utf8` 找词典 ——
       加载扩展前必须把 cwd 切到 DLL 所在目录，否则**静默退化**成逐字切
       （这件事由 `sidecar.py`/启动侧负责，本模块只做校验并**大声报警**）
"""

from __future__ import annotations

from . import paths

import logging
import os
import re
import sqlite3
import struct
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

log = logging.getLogger("pet.store")

# schema 版本（只增不减的整数，写进 meta 表）。
#
# **当前没有自动迁移** —— 走的是 `CREATE TABLE IF NOT EXISTS` 的
# "加表/加索引自动生效"路线，够用且不会误删数据。真正需要改列时才写迁移
# （那时版本号才有实际用处）。
#
# 变更记录：
#   1 —— 初版五表（recent_turn / raw_log / cherished / memory / memory_vec）
#   2 —— 加 `cherished_turn`（珍藏的每一轮单独存行，支持复现对话与逐个删除
#        段落；`cherished.text` 变成由它派生的、注入用的拼接结果）
SCHEMA_VERSION = 5


def _iso(ts: Optional[float] = None) -> str:
    """把时间戳转成**带本地时区**的 ISO8601。

    为什么不用 UTC：用户要"按时间分类查看"，看到的时间必须是他自己的钟点。
    为什么带时区：跨时区/夏令时下不带时区的字符串排序会错。
    """
    t = time.time() if ts is None else ts
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t)) + time.strftime("%z", time.localtime(t))


SCHEMA = """
PRAGMA journal_mode = WAL;      -- 读写并发：设置页读的同时后台可以写
PRAGMA synchronous = NORMAL;    -- WAL 下够安全，且比 FULL 快得多
PRAGMA foreign_keys = ON;

-- ① 最近记忆：完整轮次，不提炼，按时间窗口整段注入
CREATE TABLE IF NOT EXISTS recent_turn (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id  TEXT    NOT NULL,
  role        TEXT    NOT NULL,              -- user / assistant
  text        TEXT    NOT NULL,
  created_at  TEXT    NOT NULL,              -- ISO8601 带时区
  ts          REAL    NOT NULL               -- 同一时间戳的数值形式，便于范围查询
);
CREATE INDEX IF NOT EXISTS idx_rt_time    ON recent_turn(ts);
CREATE INDEX IF NOT EXISTS idx_rt_session ON recent_turn(session_id);

-- ② 原文记忆：一字不改的全部对话，永不淘汰，不参与检索
CREATE TABLE IF NOT EXISTS raw_log (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id  TEXT    NOT NULL,
  role        TEXT    NOT NULL,
  text        TEXT    NOT NULL,
  kind        TEXT    NOT NULL DEFAULT 'text',   -- text / image（图片存描述或路径）
  created_at  TEXT    NOT NULL,
  ts          REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_raw_time    ON raw_log(ts);
CREATE INDEX IF NOT EXISTS idx_raw_session ON raw_log(session_id);

-- ③ 珍藏记忆：用户手动标记，永不淘汰
--
-- ## 为什么还要有 ③b（`cherished_turn`）
--
-- 用户要求（2026-10-05）：
--   · 「珍藏回忆是一段对话，也可以是用户或者ai的单独一句话」
--   · 「点击哪条珍藏记忆数据，哪条数据的几轮对话或者一句话就会展现出来」
--   · 「也可以用语音和文字窗口复现」
--   · 「勾选结束添加完成也可以删除不需要的对话段落」
--
-- 这四件事都要求**每一轮是独立的行**（能单独显示、单独删、按角色着色，
-- 将来还要单独合成语音）。把整段塞进一个 `text` 字段做不到 ——
-- 那只能"整段显示"，删也只能整段删。
--
-- `text` 仍然保留，作为**注入用的拼接结果**（`memory2.inject_hint` 直接用它）。
-- 它是 `cherished_turn` 的物化视图，每次改动后由 `_resync_cherished_text()` 重算。
-- 冗余一份是**有意的**：注入是热路径（每轮对话都跑），不值得为了范式每次都 JOIN。
CREATE TABLE IF NOT EXISTS cherished (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  title       TEXT    NOT NULL DEFAULT '',
  text        TEXT    NOT NULL,              -- 注入用：各轮拼接后的结果（由子表重算）
  session_id  TEXT,
  src_from    INTEGER,                       -- 来源 raw_log.id 范围（可空，仅作线索）
  src_to      INTEGER,
  pinned      INTEGER NOT NULL DEFAULT 1,    -- 是否固定注入
  created_at  TEXT    NOT NULL,
  ts          REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ch_time ON cherished(ts);

-- ③b 珍藏的每一轮
--
-- ⚠️⚠️ **这里存的是「引用 + 副本」，不是原文本身。**
--
-- 用户明确要求（2026-10-05）：
--   「这些删除的操作不作用于原文记忆本身的数据，只是从那些需要删除操作的
--     珍藏记忆中消失去除掉而已，不动原文记忆本身数据。」
--
-- 所以：
--   · 从珍藏里"删除某一段" = **只删这一行**（`cherished_turn`），
--     `raw_log` 里那条原文**一个字都不动**；
--   · `src_raw_id` 只是"这段是从哪条原文来的"的线索（用来防重复添加、
--     也用来以后回溯），**不是外键约束** —— 原文永远在，而线索可有可无；
--   · 因此这里**故意不写** `REFERENCES raw_log(id) ON DELETE CASCADE`：
--     万一哪天有人给 raw_log 加清理策略，级联会把珍藏一起带走。
--     珍藏是"永不淘汰"的那一层，不能被原文的寿命牵连。
CREATE TABLE IF NOT EXISTS cherished_turn (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  cherished_id INTEGER NOT NULL REFERENCES cherished(id) ON DELETE CASCADE,
  ordinal      INTEGER NOT NULL,             -- 段内顺序（0 起）
  role         TEXT    NOT NULL,             -- user / assistant
  text         TEXT    NOT NULL,
  created_at   TEXT    NOT NULL,             -- 原文当时的时间（复现时显示）
  ts           REAL    NOT NULL,
  src_raw_id   INTEGER                       -- 来自 raw_log.id（可空：手动写入的没有）
);
CREATE INDEX IF NOT EXISTS idx_ct_parent ON cherished_turn(cherished_id, ordinal);
-- 建索引防重复添加：同一条原文轮次不该在同一段珍藏里出现两次
CREATE UNIQUE INDEX IF NOT EXISTS idx_ct_unique ON cherished_turn(cherished_id, src_raw_id)
  WHERE src_raw_id IS NOT NULL;

-- ④ 检索记忆：唯一被提炼的一层
CREATE TABLE IF NOT EXISTS memory (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  summary     TEXT    NOT NULL,              -- 提炼后的短事实（<=200 字，会被索引）
  kind        TEXT    NOT NULL DEFAULT 'fact',   -- fact/habit/persona/phrase/episode
  tags        TEXT    NOT NULL DEFAULT '',       -- 逗号分隔，供分类与固定注入筛选
  level       TEXT    NOT NULL DEFAULT 'recent', -- fixed(固定注入)/recent/archived
  importance  REAL    NOT NULL DEFAULT 0.4,
  hits        INTEGER NOT NULL DEFAULT 0,
  -- ⚠️ 压缩标记（schema v3）。**不删只标记** —— 用户明确要求：
  -- 「防止一些重要信息提炼后恰好被重复度选中后丢失」，所以要能还原。
  --
  --   merged_into IS NULL  -> 活跃，参与检索
  --   merged_into = <id>   -> 已被压缩进那条（不参与检索，可一键还原）
  --
  -- 与 `level` **正交**：level 是用户手动定的，merged_into 是压缩产生的。
  merged_into INTEGER,
  merged_at   TEXT,
  src_table   TEXT,                          -- 指向原文：raw_log / cherished / NULL
  src_id      INTEGER,
  created_at  TEXT    NOT NULL,
  updated_at  TEXT,
  ts          REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mem_time  ON memory(ts);
CREATE INDEX IF NOT EXISTS idx_mem_kind  ON memory(kind);
CREATE INDEX IF NOT EXISTS idx_mem_level ON memory(level);
CREATE INDEX IF NOT EXISTS idx_mem_tags  ON memory(tags);
-- ⚠️ `idx_mem_merged` **不在这里建** —— 它依赖 v3 新增的 `merged_into` 列，
-- 而老库的 `memory` 表已存在（`CREATE TABLE IF NOT EXISTS` 跳过），
-- 于是这条 `CREATE INDEX` 会撞 `no such column: merged_into`，
-- **整个记忆库起不来**（实测踩过）。它由 `_migrate_schema()` 负责建。

-- ⑤ 向量索引：与 ④ 分开存
CREATE TABLE IF NOT EXISTS memory_vec (
  memory_id   INTEGER PRIMARY KEY REFERENCES memory(id) ON DELETE CASCADE,
  dim         INTEGER NOT NULL,
  vector      BLOB    NOT NULL               -- float32 小端打包
);

-- 元信息（schema 版本等）
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

-- ⑥ 兴趣表（2026-10-17，用户要求「爱好由对话形成」）
--
-- 用户原话：
--   「爱好由我后续对话形成，利用检索层自发形成，就是需要搞搞注意力系统」
--
-- 设计取舍：**独立一张表，不塞进 memory**。
--   · memory 的行是"关于用户的事实"，检索时按语义匹配；
--   · interest 的行是"它自己的关注点"，用权重排序、用来决定"想聊什么"。
-- 两者用途不同，混在一起会让"用户喜欢恐怖游戏"和"它对恐怖游戏感兴趣"
-- 分不清 —— 而后者本该是**它自己的**（可以是相反的："用户怕，它更怕"）。
CREATE TABLE IF NOT EXISTS interest (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  topic        TEXT    NOT NULL UNIQUE,       -- 规范化词（银松镇 / GTA6）
  weight       REAL    NOT NULL DEFAULT 0.3,  -- 0~1，越高越"爱聊"
  hits         INTEGER NOT NULL DEFAULT 0,    -- 被提到/检索到几次
  strength     TEXT    NOT NULL DEFAULT 'weak', -- strong(引号/ASCII) / weak(分词)
  created_at   REAL    NOT NULL,
  last_seen_at REAL    NOT NULL,
  source       TEXT    NOT NULL DEFAULT ''    -- 最近一次来自哪条记忆/哪次检索
);

CREATE INDEX IF NOT EXISTS idx_interest_weight ON interest(weight DESC);

-- ⑦ 随机事件日志（2026-10-17，用户要求「不定时的随机事件也写进去」）
--
-- 用户原话：
--   「不定时的随机事件也写进去，比如：想和用户一起打游戏（后续会做…先留个路口）、
--     大声唱用户喜欢的歌搞怪（…先留个路口）、去网上搜索哲学语句和用户交流体验、
--     去网上观看自己喜欢的小说（…先留个路口），做这些事也会相应的增加玩伴
--     对用户的印象、玩伴的情绪变化，把这些记录下来，顺便将随机事件列为重点功能」
--
-- 为什么要**记下来**而不是"发生了就发生了"：
--   ① 它得有"我昨天搜到一句话想跟你说"这种连续性；不记就每次都是新的；
--   ② 用户要能看到它偷偷做了什么（可信度问题 —— 桌宠偷偷联网很容易让人不安）；
--   ③ 调参需要：哪个事件触发太频繁，看这张表就知道。
CREATE TABLE IF NOT EXISTS event_log (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  kind           TEXT    NOT NULL,          -- 事件的 id（philosophy / music_sing …）
  label          TEXT    NOT NULL,          -- 中文名（给人看）
  detail         TEXT    NOT NULL DEFAULT '', -- 它具体做了什么
  mood_delta     REAL    NOT NULL DEFAULT 0,
  energy_delta   REAL    NOT NULL DEFAULT 0,
  intimacy_delta REAL    NOT NULL DEFAULT 0,
  attention_delta REAL   NOT NULL DEFAULT 0,
  created_at     REAL    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_event_created ON event_log(created_at DESC);

-- ⑧ 印象（2026-10-17）
--
-- 用户最初说「印象系统先记着」，后来在随机事件里要求
-- 「做这些事也会相应的增加玩伴对用户的印象」—— 所以先把最小的落地做出来：
-- **只存文本 + 出处，不参与语义检索**（避免挤掉事实类记忆，那正是之前清库的原因）。
CREATE TABLE IF NOT EXISTS impression (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  text       TEXT    NOT NULL,
  source     TEXT    NOT NULL DEFAULT '',   -- 来自哪个事件 / 哪段对话
  created_at REAL    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_impression_created ON impression(created_at DESC);
"""


def pack_vector(vec: Iterable[float]) -> bytes:
    """float32 小端打包。比 JSON 文本省 2 倍（实测 512 维：2048 vs 4096 字节）。"""
    items = list(vec)
    return struct.pack(f"<{len(items)}f", *items)


def unpack_vector(blob: bytes) -> list[float]:
    if not blob:
        return []
    n = len(blob) // 4
    return list(struct.unpack(f"<{n}f", blob[: n * 4]))


@dataclass
class BackupResult:
    ok: bool
    message: str
    path: str = ""
    """
    快照已生成、但**还没到目标文件夹**，只落在 `path`（staging 位置）。

    出现这种结果的原因：**Python 子进程对目标文件夹没有写权限，而 Electron 主进程有**
    （实测：同一台机器上，主进程能把文件复制进 `F:\\ai数据库备份`，Python 写就
    `PermissionError`）。所以改成两段式：

        ① Python 用 `VACUUM INTO` 在**它写得进去的地方**生成一致快照；
        ② 主进程把文件复制到用户选的文件夹。

    置位时 `ok=True`（快照本身是成功的），由主进程负责第二段。
    """
    staged: bool = False
    """`staged=True` 时，快照最终该被复制到的文件夹（主进程据此搬运）。"""
    target_dir: str = ""


class PetDatabase:
    """记忆数据库的句柄。

    刻意做成"一个类、一个连接"：SQLite 的写是串行的，多连接并发写只会互相锁。
    读多写少的场景下，单连接 + WAL 是最省心的组合。
    """

    def _ensure_raw_image_columns(self) -> None:
        """给 `raw_log` 补 image / image_mime 两列（2026-10-18）。

        ## 为什么要往原文记忆里存图

        用户要求：「截图传给 api 给的回复记录进原文记忆时要带上图片，
        这样就知道截图回复的是什么图片了」，并且明确「图片放进数据库存储」。

        所以图是 **BLOB 存在库里**，不是文件路径 —— 备份/迁移只有 .db 一个文件，
        不会出现"库里有记录、图片文件却被删了"的断链。

        ## 为什么用 ALTER 而不是改 CREATE

        老库（用户的 pet.db 已经有上万条原文）不能重建表 ——
        重建就要搬数据，搬错就是不可恢复的记忆丢失。加列是原地操作。
        """
        try:
            cols = {row[1] for row in self.conn.execute("PRAGMA table_info(raw_log)")}
            if "image" not in cols:
                self.conn.execute("ALTER TABLE raw_log ADD COLUMN image BLOB")
            if "image_mime" not in cols:
                self.conn.execute("ALTER TABLE raw_log ADD COLUMN image_mime TEXT")
            self.conn.commit()
        except sqlite3.Error:  # 老库只读/被占用时不要连累启动
            pass

    def _ensure_quote_tables(self) -> None:
        """
        「自言自语」念的句子表（2026-10-18 用户要求：**用数据库单开一张表**）。

        用户原话：「这个表太慢，不好维护，用数据库单开一张表来存储，
        按那几个分类以自言自语的默认频率输出就可以了」。

        ## 为什么单独一张表而不是塞进 memory

        `memory` 是"记忆"（检索用、会被压缩/淘汰/加向量）。句子是**素材**：
        不该被淘汰、不需要向量、条目要按类别做权重。混进去会让两边都别扭
        —— 最直接的问题：记忆压缩会把句子当成"旧记忆"压掉。

        ## 与 `config/quotes.yaml` 的关系

        yaml 只做**首次导入的种子**（`QuoteRepo.import_seed`）。导入后以库为准，
        以后改句子改库（或者将来做界面改库），不再依赖那个 yaml。
        """
        try:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS quote_kind (
                  name    TEXT PRIMARY KEY,
                  label   TEXT NOT NULL DEFAULT '',
                  weight  REAL NOT NULL DEFAULT 1.0,
                  sort    INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS quote (
                  id           INTEGER PRIMARY KEY AUTOINCREMENT,
                  kind         TEXT NOT NULL,
                  text         TEXT NOT NULL,
                  source       TEXT NOT NULL DEFAULT '',
                  created_at   TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                  used_count   INTEGER NOT NULL DEFAULT 0,
                  last_used_at TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_quote_text ON quote(text);
                CREATE INDEX IF NOT EXISTS idx_quote_kind ON quote(kind, used_count);
                """
            )
            self.conn.commit()
        except sqlite3.Error:
            # 句子表坏了不该拖垮记忆库启动 —— 自言自语不是核心功能
            pass

    def __init__(
        self,
        path: Path,
        tokenizer: Optional[str] = None,
        simple_dll: Optional[Path] = None,
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row

        self._ensure_raw_image_columns()
        self._ensure_quote_tables()
        self.conn.execute("PRAGMA busy_timeout = 5000")  # 别立刻报 database is locked

        # ⚠️ 扩展是**按连接**加载的，不是进程级（实测：另一条连接加载过，这条仍然没有）。
        # 所以每个 PetDatabase 实例都必须自己加载一次，否则 `simple` 分词器不可用、
        # FTS 建表直接失败（`no such tokenizer: simple`），中文检索全部落空。
        self.simple_loaded = False
        if simple_dll is not None:
            self.simple_loaded = load_simple_extension(self.conn, Path(simple_dll))

        # ⚠️ 这三个**必须在这里先初始化**：`_create_vec_index()` 里有多个提前
        # `return` 的分支（DLL 缺失 / 加载失败 / 空库），不走到底就不会设它们。
        # 而 `vec_search` / `vec_upsert` 随时可能被调用 —— 属性不存在会直接
        # `AttributeError`，把"可选优化没启用"变成"整个记忆库崩了"。
        #
        # `_vec_ready` = DLL 加载成功（**可以**建索引）
        # `_vec_ok`    = 索引表已就绪（**可以**查）
        self._vec_ok = False
        self._vec_ready = False
        self._vec_dll: Optional[Path] = None

        # ⚠️ **顺序**：先 `executescript(SCHEMA)`，**再** `_migrate_schema()`。
        #
        # 两个方向都踩过，说清楚为什么是这个顺序：
        #
        #   · 迁移放**前**面：全新库那时连 `memory` 表都没有，
        #     `ALTER TABLE memory ADD COLUMN` 直接 `no such table: memory`
        #     （被 try 吞掉），**新库永远拿不到新列** —— 实测就是这个崩的。
        #   · 迁移放**后**面 + 索引写在 `SCHEMA` 里：老库的 `memory` 已存在
        #     （`CREATE TABLE IF NOT EXISTS` 跳过），于是 `SCHEMA` 里那条
        #     `CREATE INDEX ... ON memory(merged_into)` 会撞
        #     `no such column: merged_into`，**整个库起不来**。
        #
        # 所以正确的分工是：
        #   · `SCHEMA` 只管**建表 + 不依赖新列的索引**（新库一步到位）；
        #   · `_migrate_schema()` 在表已存在之后跑，补**缺失的列 + 依赖它们的索引**
        #     （老库平滑升级）。两边合起来，新库和老库都能到同一个终态。
        self.conn.executescript(SCHEMA)
        self._migrate_schema()
        # ⚠️ 这里**必须再调一次**：新库在第 408 行那次调用时 `raw_log` 还没建出来
        # （`SCHEMA` 还没执行），ALTER 会以 "no such table" 静默跳过。
        self._ensure_raw_image_columns()
        self._tokenizer = tokenizer or "unicode61"
        self._create_fts()
        # ⚠️ 用 `INSERT ... ON CONFLICT DO UPDATE` 而**不是** `INSERT OR IGNORE`。
        #
        # `OR IGNORE` 在键已存在时**什么都不做** —— 于是升级 `SCHEMA_VERSION`
        # 之后，老库里的版本号永远停在旧值（实测：改成 2 之后库里还是 1）。
        # 现在没有自动迁移所以不影响功能，但**将来要靠它判断"该不该迁移"**，
        # 一个永远不更新的版本号会让迁移被静默跳过。
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES('schema_version', ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(SCHEMA_VERSION),),
        )
        self.conn.commit()

    # ------------------------------------------------------------ 架构迁移

    def _migrate_schema(self) -> None:
        """把**老库**平滑升到当前 `SCHEMA_VERSION`（幂等，可重复跑）。

        ## 为什么需要它（`CREATE TABLE IF NOT EXISTS` 不够）

        `SCHEMA` 里的 `CREATE TABLE IF NOT EXISTS` 对**已存在的表什么都不做** ——
        所以往 `SCHEMA` 里加字段**不会**影响老库：老库还是老结构，
        而新代码会去读一个不存在的列，报 `no such column: merged_into`。

        ## 做法：逐列探测 + `ALTER TABLE ADD COLUMN`

        `ALTER TABLE ADD COLUMN` 在 SQLite 里是**幂等不了**的（重复加会报错），
        所以先查 `PRAGMA table_info` 看列在不在。这是 SQLite 官方推荐的
        轻量迁移方式（不用重建表）。

        ⚠️ **本项目的改库纪律**（用户 2026-10-05 要求）：
        schema 变更要**一轮做完** —— 设计 / SCHEMA / 迁移 / 回填 / 写入 / 读取 /
        中间层 / 测量 / 文档，不能只改一半。本项目已因"只改一半"踩过：
          · `hits` 加了但生产路径不累加（死字段）
          · `memory_vec0`（KNN）加了但迁移没跟上 → 重建成 0 条
          · 四个配置项定义了但从没被读过
        """

        def has_column(table: str, column: str) -> bool:
            try:
                rows = self.conn.execute(f"PRAGMA table_info({table})").fetchall()
            except sqlite3.Error:
                return False
            return any(str(r["name"]) == column for r in rows)

        # ---- v3：记忆压缩标记 ----
        added: list[str] = []
        if not has_column("memory", "merged_into"):
            self.conn.execute("ALTER TABLE memory ADD COLUMN merged_into INTEGER")
            added.append("merged_into")
        if not has_column("memory", "merged_at"):
            self.conn.execute("ALTER TABLE memory ADD COLUMN merged_at TEXT")
            added.append("merged_at")
        # 索引**必须在这里建**（不在 `SCHEMA` 里）—— 它依赖 v3 的新列，
        # 而老库的表已存在，放在 `SCHEMA` 里会在列还没加时就建索引。
        # 按"是否已压缩"过滤是检索热路径（每次 `_retrieve` 都要），所以要有索引。
        if has_column("memory", "merged_into"):
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_mem_merged ON memory(merged_into)"
            )

        if added:
            self.conn.commit()
            log.info(
                "记忆库已迁移到 v%d（新增列：%s）",
                SCHEMA_VERSION,
                "、".join(added),
            )

        # ---- 回填：新列的初始值 ----
        #
        # ⚠️ 历史数据的 `merged_into` 是 `NULL` —— 语义正好就是"活跃"，**不用回填**。
        # 但要说清楚：`NULL` 有两个来源（"从没压缩过"和"迁移时新增的"），
        # 在这里它们**语义相同**，所以不需要区分。将来若加"压缩历史"字段，
        # 才需要区分这两者。

    # ------------------------------------------------------------ 全文检索

    def _create_fts(self) -> None:
        """建检索记忆的 FTS 索引。

        `external content` 表：索引表**不存正文**，只存倒排索引 —— 官方推荐做法，
        也正好符合用户"用 id 锁定、联表取正文"的要求。

        ## ⚠️ 这里有一道**防崩护栏**，不能删

        `simple` 分词器在**第一次被编译**时（即 `CREATE VIRTUAL TABLE`）就会去加载
        cppjieba 词典，路径是硬编码的相对路径 `./dict/jieba.dict.utf8`。
        如果此时 cwd 不对，cppjieba 会调 **`log.FATAL`** ——

            整个 Python 进程**直接退出**，不是抛异常，`try/except` 拦不住。

        实测 cwd 设在仓库根时，进程输出这一行后立刻死掉：

            DictTrie.hpp:203 FATAL exp: [ifs.is_open()] false. open ./dict/jieba.dict.utf8 failed.

        所以**必须先用纯文件检查确认词典可达**，绝不能让 `CREATE VIRTUAL TABLE`
        去撞运气。cwd 由启动侧保证（见 `sidecar.ts`）；这里只做
        "看得见就放心用，看不见就退到 unicode61"的判断。
        """
        want_simple = self._tokenizer == "simple" and self._dict_reachable()
        if want_simple:
            try:
                self.conn.execute(
                    """
                    CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
                      summary,
                      content='memory',
                      content_rowid='id',
                      tokenize='simple'
                    )
                    """
                )
                self._fts_ok = True
            except sqlite3.OperationalError as exc:
                log.warning("simple 分词器不可用，退回 unicode61：%s", exc)
                want_simple = False

        if not want_simple:
            if self._tokenizer == "simple":
                log.warning(
                    "中文分词词典不可达（cwd=%s 下没有 ./dict/jieba.dict.utf8）——"
                    " 中文检索会退化为逐字切，请检查 sidecar 启动时的 cwd",
                    Path.cwd(),
                )
            self.conn.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
                  summary, content='memory', content_rowid='id', tokenize='unicode61'
                )
                """
            )
            self._fts_ok = False
        # 与 memory 表保持同步（external content 表必须自己维护）
        self.conn.executescript(
            """
            CREATE TRIGGER IF NOT EXISTS memory_ai AFTER INSERT ON memory BEGIN
              INSERT INTO memory_fts(rowid, summary) VALUES (new.id, new.summary);
            END;
            CREATE TRIGGER IF NOT EXISTS memory_ad AFTER DELETE ON memory BEGIN
              INSERT INTO memory_fts(memory_fts, rowid, summary) VALUES('delete', old.id, old.summary);
            END;
            CREATE TRIGGER IF NOT EXISTS memory_au AFTER UPDATE ON memory BEGIN
              INSERT INTO memory_fts(memory_fts, rowid, summary) VALUES('delete', old.id, old.summary);
              INSERT INTO memory_fts(rowid, summary) VALUES (new.id, new.summary);
            END;
            """
        )
        self._create_vec_index()
        self.conn.commit()

    # ------------------------------------------------------ 向量索引（sqlite-vec）

    def _create_vec_index(self) -> None:
        """建 `memory_vec0` —— sqlite-vec 的 KNN 索引（**可选**）。

        ## 为什么上它（用户 2026-10-05 指定）

        在此之前，向量检索是**把全部向量读进 Python 再逐条算余弦**
        （`all_vectors_raw` + `_cosine_batch_raw`）。它被优化过
        （2000 条 4.6ms，见 `tools/bench-vector.py`），但本质是 **O(n) 线性扫描**：

            · 每次检索都要把 n 条向量从 SQLite 读进内存；
            · 计算在 Python/C 边界上来回，规模一大就越过越慢。

        sqlite-vec 把这件事**下推到 SQL 层**（C 实现），实测 2000 条 512 维：

            Python 线性扫描（优化后）  4.6 ms
            sqlite-vec KNN            2.34 ms

        现在两者都不慢，选它的理由是**结构**：计算留在存储层、
        不必把全表搬进内存，规模再大也不用改应用层。

        ## ⚠️ 三道护栏（缺一个就会崩或者静默失效）

        ① **DLL 不在就跳过**。`vec0` 是扩展提供的虚拟表模块，
           没加载就 `CREATE VIRTUAL TABLE` 会抛 `no such module: vec0`。
        ② **维度从库里现有向量推断**。`vec0` 的维度在建表时固定，
           写错了整张表就废了（插不进去）。没有向量时（空库）**不建表**，
           等第一次写入再建 —— 否则会按默认维度建错。
        ③ **建表失败只记 warning，不影响启动**。KNN 只是"更快的路"，
           没有它照样能用 `_cosine_batch_raw` 线性扫描 ——
           **不能让一个可选优化把整个 sidecar 拖崩**。

        ## 距离度量用 cosine

        `vec0` 默认是 L2。这里显式指定 `distance_metric=cosine`，
        这样 `distance = 1 - 余弦相似度`，与 `_cosine` 的定义一致。
        实测等价性（`check-vec-equivalence.py`，历史实测脚本）：
        排序前 20 **完全一致**，数值最大差 **1.09e-07**。
        """
        self._vec_ok = False
        dll = self._resolve_vec_dll()
        if dll is None:
            log.info(
                "sqlite-vec 未启用（没找到 vec0.dll）—— 向量检索走 Python 线性扫描"
            )
            return
        try:
            self.conn.enable_load_extension(True)
            self.conn.load_extension(str(dll))
            version = self.conn.execute("SELECT vec_version()").fetchone()[0]
        except Exception as exc:  # noqa: BLE001
            log.warning("sqlite-vec 加载失败（%s）—— 向量检索走 Python 线性扫描", exc)
            return

        # ⚠️ **先记下 DLL 和 ready**，再往下走。
        #
        # 第一版把 `self._vec_dll = dll` 写在下面各个分支里，而
        # `vec_rebuild()` 一开头就读 `self._vec_dll` —— 那时它还是 None，
        # 于是"重建"必然返回 False（实测日志：`重建失败（现有 0 维，需要 512 维）`，
        # 而库里其实根本没有 memory_vec0 表，压根不需要重建）。
        #
        # 教训：**先建立不变量，再做依赖它的判断**。
        self._vec_dll = dll
        self._vec_ready = True

        # 维度从现有向量推断；空库则先不建（等第一次写入）
        dim = 0
        try:
            row = self.conn.execute(
                "SELECT LENGTH(vector) AS n FROM memory_vec LIMIT 1"
            ).fetchone()
            if row and row["n"]:
                dim = int(row["n"]) // 4
        except sqlite3.Error:
            dim = 0

        if dim <= 0:
            log.info(
                "sqlite-vec %s 已加载，但库里还没有向量 —— KNN 索引等首次写入时建",
                version,
            )
            return

        # ---- 分三种情况：表不存在 / 维度不符 / 表在但结构残缺 ----
        #
        # ⚠️ **`_vec_ok` 必须在调 `vec_sync()` 之前就置位。**
        #
        # 踩过两次同一个形态的坑：
        #   · 第一次：`_vec_dll` 在分支里赋值，而 `vec_rebuild()` 一开头就读它；
        #   · 第二次：`_vec_ok = True` 写在 `vec_sync(dim)` **之后**，而
        #     `vec_sync()` 开头有 `if not self._vec_ok: return 0` ——
        #     于是"补齐"永远插 0 条，KNN 索引是空的、所有语义检索返回空。
        #     实测日志一切正常（"就绪"照打），只有对着 `vec0=0` 才看得出来。
        #
        # 规则：**先建立不变量，再做依赖它的判断/调用。**
        self._vec_ok = True

        have = self._vec_table_dim()
        if have == 0:
            # 表还不存在（老库第一次升级上来）—— 直接建，不需要"重建"
            if not self._create_vec_table(dim):
                self._vec_ok = False
                log.warning("建 KNN 索引失败 —— 本次运行走 Python 线性扫描")
                return
        elif have != dim:
            # 换过 embedding 实现（256 -> 512）—— 必须重建
            self._vec_ok = False  # 交给 vec_rebuild 自己置位
            if not self.vec_rebuild(dim):
                log.warning(
                    "KNN 索引维度不符且重建失败（现有 %d 维，需要 %d 维）—— "
                    "本次运行走 Python 线性扫描；**删掉库里的 memory_vec0 后重启即可恢复**",
                    have,
                    dim,
                )
                return
        elif not self._vec_probe(dim):
            # 表记录在，但内部结构（vec0 的 chunk / shadow 表）不完整 ——
            # 常见于"上一次重建半途失败"。同样只能重建。
            log.warning("KNN 索引内部结构不完整，正在重建")
            self._vec_ok = False
            if not self.vec_rebuild(dim):
                log.warning("KNN 索引修复失败 —— 本次运行走 Python 线性扫描")
                return

        # ⚠️ **不管走哪条分支，最后都补齐一次。**
        #
        # 踩过：真实库里留着一张"表建好了但一条数据都没进去"的空表
        # （早期失败尝试的残留）。上面的分支只看**维度**，维度对就认为
        # "已就绪"，于是这张空表被一直当成好的 —— 所有语义检索返回空。
        # 日志一切正常（"就绪"照打），只有对着 `vec0=0` 才看得出来。
        #
        # `vec_sync` 用**差集**补齐，幂等，所以每次开库都跑一次是安全且廉价的
        # （一次 `NOT IN` 子查询）。它顺手把"表在但数据不全"这类状态也修好了。
        added = self.vec_sync(dim)
        if added:
            log.info("KNN 索引补齐 %d 条", added)

        self._vec_ok = True
        log.info("sqlite-vec %s 就绪：KNN 索引 memory_vec0（%d 维）", version, dim)

    def _create_vec_table(self, dim: int) -> bool:
        """建 KNN 索引表（**只建，不回灌** —— 回灌交给 `vec_sync`）。

        回灌分开是有原因的：`vec_upsert` 会先插当前这条再建表，
        如果建表顺带全量回灌就会**撞主键**
        （实测 `Internal sqlite-vec error: Could not insert a new vector chunk`）。
        """
        try:
            self.conn.execute(
                f"CREATE VIRTUAL TABLE memory_vec0 USING vec0("
                f"memory_id INTEGER PRIMARY KEY, emb float[{dim}] distance_metric=cosine)"
            )
            self.conn.commit()
            log.info("已建 KNN 索引 memory_vec0（%d 维）", dim)
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("建 KNN 索引失败（%s）", exc)
            return False

    def _vec_table_dim(self) -> int:
        """已有 KNN 索引表的维度；没有表或读不出来返回 0。"""
        try:
            row = self.conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='memory_vec0'"
            ).fetchone()
        except sqlite3.Error:
            return 0
        if not row:
            return 0
        m = re.search(r"float\[(\d+)\]", str(row["sql"] or ""))
        return int(m.group(1)) if m else 0

    def _vec_probe(self, dim: int) -> bool:
        """**实测** KNN 表能不能查（而不是只看 `sqlite_master` 有没有记录）。

        vec0 是虚拟表：`sqlite_master` 里有记录**不代表**能查 ——
        `_info` / `_rowids` / `_chunks` 那几个影子表缺一个就会报
        `no such table: main.memory_vec0_rowids`（实测踩过）。
        只有真发一次查询才知道，所以这里花一次空查询换取可靠性。
        """
        if dim <= 0:
            return False
        try:
            self.conn.execute(
                "SELECT memory_id FROM memory_vec0 WHERE emb MATCH ? LIMIT 1",
                (pack_vector([0.0] * dim),),
            ).fetchone()
            return True
        except sqlite3.Error:
            return False

    def vec_sync(self, dim: int) -> int:
        """把 `memory_vec` 里**缺失**的向量补进 KNN 索引，返回补齐条数。

        ⚠️ 用**差集**（`NOT IN (SELECT memory_id FROM memory_vec0)`）判断缺哪些，
        而不是"数量不等就全灌"。数量比较在"插入中途失败"时会永远修不好：
        数量一直不等 -> 每次都想重灌 -> 重灌又撞主键。
        实测就是这个循环产生了 `Could not insert a new vector chunk`。
        """
        if dim <= 0 or not self._vec_ok:
            return 0
        try:
            # ⚠️ 加 `m.merged_into IS NULL`：**已压缩的不该进 KNN 索引**
            # （用户选定的 A 方案）。少了这个 JOIN，压缩过的条目会被回灌进
            # 索引，于是"压缩了但还能被语义检索命中" —— 压缩等于没做。
            cur = self.conn.execute(
                "INSERT INTO memory_vec0(memory_id, emb) "
                "SELECT v.memory_id, v.vector FROM memory_vec v "
                "JOIN memory m ON m.id = v.memory_id "
                "WHERE LENGTH(v.vector) = ? AND m.merged_into IS NULL "
                "  AND v.memory_id NOT IN (SELECT memory_id FROM memory_vec0)",
                (dim * 4,),
            )
            self.conn.commit()
            return int(cur.rowcount or 0)
        except Exception as exc:  # noqa: BLE001
            log.debug("KNN 索引补齐失败（%s）", exc)
            return 0

    def _resolve_vec_dll(self) -> Optional[Path]:
        """找 `vec0.dll`。

        落点与 `simple.dll` 同级（`pet-data/vendor/`），
        也可以由 `PET_VEC_DLL` 环境变量显式指定。
        """
        raw = os.environ.get("PET_VEC_DLL", "").strip()
        if raw:
            p = Path(raw)
            return p if p.is_file() else None
        # 从 cwd 反推（sidecar 的 cwd 是 simple.dll 的父目录，即 vendor 下的一个子目录）
        candidates = [
            Path.cwd().parent / "sqlite-vec" / "vec0.dll",
            Path.cwd() / "sqlite-vec" / "vec0.dll",
        ]
        # 再试从本文件位置推（测试场景下 cwd 未必对）
        root = Path(__file__).resolve().parents[3]
        candidates.append(root / "apps" / "desktop" / "pet-data" / "vendor" / "sqlite-vec" / "vec0.dll")
        for c in candidates:
            if c.is_file():
                return c
        return None

    def vec_search(self, query: list[float], limit: int) -> Optional[list[tuple[int, float]]]:
        """KNN 检索，返回 `[(memory_id, 余弦相似度), ...]`（按相似度降序）。

        **返回 `None` 表示 KNN 不可用** —— 调用方应当退回线性扫描。
        返回 `[]` 则表示可用但没有命中（这与 `None` 是不同的事，
        调用方不该把两者混为一谈）。
        """
        if not self._vec_ok or not query:
            return None
        try:
            packed = pack_vector(query)
            rows = self.conn.execute(
                "SELECT memory_id, distance FROM memory_vec0 "
                "WHERE emb MATCH ? ORDER BY distance LIMIT ?",
                (packed, int(limit)),
            ).fetchall()
            # vec0 的 cosine 距离 = 1 - 余弦相似度
            return [(int(r["memory_id"]), 1.0 - float(r["distance"])) for r in rows]
        except Exception as exc:  # noqa: BLE001
            log.debug("KNN 检索失败（%s），退回线性扫描", exc)
            return None

    def vec_upsert(self, memory_id: int, vector: list[float]) -> None:
        """把一条向量写进 KNN 索引（失败只记日志，不影响主流程）。"""
        if not vector:
            return
        # ⚠️ 判据是 `_vec_ready`（DLL 加载成功）而**不是** `_vec_ok`（索引表已就绪）。
        #
        # 空库启动时索引表还没建（维度无从推断），此时 `_vec_ok=False` ——
        # 若在这里就 return，首次写入永远不会触发建表，KNN 就**永远是关的**。
        if not self._vec_ready:
            return
        if not self._vec_ok:
            # 表还不存在（空库的首次写入）—— 这时才知道维度。
            # 注意：**只处理"表不存在"**；维度不符的情况在开库那一步已经解决了，
            # 不该在写入路径上动表结构（半失败会污染当前这条写入）。
            try:
                self.conn.execute(
                    f"CREATE VIRTUAL TABLE IF NOT EXISTS memory_vec0 USING vec0("
                    f"memory_id INTEGER PRIMARY KEY, emb float[{len(vector)}] "
                    f"distance_metric=cosine)"
                )
                self.conn.commit()
                self._vec_ok = True
            except Exception as exc:  # noqa: BLE001
                log.debug("建 KNN 索引失败（%s），本次运行走线性扫描", exc)
                return
        try:
            self.conn.execute(
                "INSERT OR REPLACE INTO memory_vec0(memory_id, emb) VALUES (?, ?)",
                (int(memory_id), pack_vector(vector)),
            )
        except Exception as exc:  # noqa: BLE001
            # 走到这里说明索引表状态不对（维度不符 / 影子表残缺）。
            # ⚠️ **不在这里重建**（那会在写入路径上动表结构），只记一条日志并
            # 关掉 KNN —— 本次运行退回线性扫描，下次开库时 `_create_vec_index`
            # 会用 `_vec_probe` 发现并干净地重建。功能不受影响，只是慢一点。
            log.warning(
                "写 KNN 索引失败（%s）—— 本次运行向量检索走 Python 线性扫描，"
                "重启一次会自动重建索引",
                exc,
            )
            self._vec_ok = False

    def vec_rebuild(self, dim: int) -> bool:
        """删掉 KNN 索引表并按 `dim` 重建、回灌。

        ## ⚠️ 只用最朴素的一招：`DROP TABLE`

        我试过"换个名字重建再改名"（因为以为虚拟表 DROP 不掉）。
        **那条路更糟** —— vec0 会连带建 `_info` / `_rowids` / `_chunks` 等
        影子表，半途失败就留下一堆残留，再想重建会撞
        `Could not create '_info' shadow table: table "..." already exists`，
        于是**越修越坏**（实测）。

        现在就是 `DROP TABLE` + `CREATE`：成功了就继续，失败就**如实报告
        False 并放弃**（本次运行退回线性扫描）。不尝试花招、不留半成品。

        ## ⚠️ 调用时机只有两处

          · `_create_vec_index()` —— **开库时**，此时还没有并发写入，最干净；
          · `vec_upsert()` —— 首次写入（表都还不存在，没有"重建"问题）。

        维度变化的热重建**不在** `vec_upsert` 里做（那样会在写入路径上
        操作表结构，半失败就污染了当前这条的写入）。维度不符时开库那一步
        已经处理掉了。

        `memory_vec` 是**真相**（原始 BLOB），所以重建无损。
        """
        if dim <= 0:
            return False
        dll = getattr(self, "_vec_dll", None)
        if dll is None:
            return False
        try:
            self.conn.execute("DROP TABLE IF EXISTS memory_vec0")
            self.conn.commit()
        except Exception as exc:  # noqa: BLE001
            log.warning("删不掉旧 KNN 索引（%s）", exc)
            return False
        if not self._create_vec_table(dim):
            return False
        self._vec_ok = True
        self._vec_ready = True
        added = self.vec_sync(dim)
        log.info("KNN 索引已按 %d 维重建（回灌 %d 条）", dim, added)
        return True

    def vec_drop(self, memory_id: int) -> None:
        """从 KNN 索引里删一条（同样不影响主流程）。"""
        if not self._vec_ok:
            return
        try:
            self.conn.execute("DELETE FROM memory_vec0 WHERE memory_id = ?", (int(memory_id),))
        except Exception as exc:  # noqa: BLE001
            log.debug("删 KNN 索引失败（%s）", exc)

    def rebuild_fts(self) -> None:
        """整体重建 FTS 索引（换分词器后需要）。"""
        self.conn.execute("INSERT INTO memory_fts(memory_fts) VALUES('rebuild')")
        self.conn.commit()

    def _dict_reachable(self) -> bool:
        """cppjieba 词典是否在**当前 cwd 下**可达。

        纯文件系统检查，**不会**触发 cppjieba 初始化 —— 这正是它的价值：
        用一次 stat 换掉一次"整个进程崩溃"的风险。
        """
        try:
            return (Path.cwd() / "dict" / "jieba.dict.utf8").exists()
        except OSError:
            return False

    def has_jieba(self) -> bool:
        """`jieba_query()` 是否可用 —— 决定查询能不能做词组匹配。

        ⚠️ 只有在确认词典可达时才调用：词典不可达时这个函数会**崩掉进程**。
        """
        if not self._dict_reachable():
            return False
        try:
            self.conn.execute("SELECT jieba_query('测试')").fetchone()
            return True
        except sqlite3.OperationalError:
            return False

    def query_expr(self, text: str) -> str:
        """把用户输入转成 FTS5 的 MATCH 表达式。

        ⚠️ **必须**走 `jieba_query()`：`simple` 是按单字建索引的，
        直接把原文丢给 MATCH 会退化成"单字 AND"，噪音极大
        （「冰咖啡」会命中「冰美式咖啡」这种无关内容）。

        词典不可达时退回**引号短语**：至少让"连续的几个字"成为一个短语，
        比裸单字 AND 精确得多，而且绝不会崩进程。
        """
        fallback = '"' + text.replace('"', '""') + '"'
        if not self._dict_reachable():
            return fallback
        try:
            row = self.conn.execute("SELECT jieba_query(?)", (text,)).fetchone()
            rewritten = row[0] if row else ""
            return rewritten or fallback
        except sqlite3.OperationalError:
            return fallback

    # ------------------------------------------------------------ 写入

    def add_turn(
        self,
        session_id: str,
        role: str,
        text: str,
        ts: Optional[float] = None,
        *,
        image: Optional[bytes] = None,
        image_mime: Optional[str] = None,
    ) -> int:
        """写一轮对话 —— **同时**进"最近记忆"和"原文记忆"。

        两张表都要写，因为它们用途不同：
          · recent_turn 会被时间窗口整段注入（有 Trim 策略，会归档）
          · raw_log 是永久底本（只追加，永不删）
        """
        t = time.time() if ts is None else ts
        iso = _iso(t)
        cur = self.conn.execute(
            "INSERT INTO recent_turn(session_id, role, text, created_at, ts) VALUES (?,?,?,?,?)",
            (session_id, role, text, iso, t),
        )
        # 截图回复的句子把原图一起写进原文记忆（BLOB）——
        # 「这样就知道截图回复的是什么图片了」（用户 2026-10-18）
        if image:
            # mime 按**字节魔数**判，不信任调用方传的字符串：
            # 实测 ImageGrab 给的是 PNG，而调用处写的是 image/jpeg，
            # 存进去就是"PNG 字节 + jpeg 标签"。
            if bytes(image[:8]).startswith(b"\x89PNG"):
                image_mime = "image/png"
            elif bytes(image[:3]) == b"\xff\xd8\xff":
                image_mime = "image/jpeg"
            self.conn.execute(
                "INSERT INTO raw_log(session_id, role, text, kind, created_at, ts, image, image_mime)"
                " VALUES (?,?,?,'image',?,?,?,?)",
                (session_id, role, text, iso, t, sqlite3.Binary(image), image_mime or "image/jpeg"),
            )
        else:
            self.conn.execute(
                "INSERT INTO raw_log(session_id, role, text, kind, created_at, ts) VALUES (?,?,?,'text',?,?)",
                (session_id, role, text, iso, t),
            )
        self.conn.commit()
        return int(cur.lastrowid or 0)

    def add_memory(
        self,
        summary: str,
        *,
        kind: str = "fact",
        tags: str = "",
        level: str = "recent",
        importance: float = 0.4,
        src_table: Optional[str] = None,
        src_id: Optional[int] = None,
        vector: Optional[list[float]] = None,
        ts: Optional[float] = None,
    ) -> int:
        """写一条**检索记忆**（唯一被提炼的一层）。"""
        t = time.time() if ts is None else ts
        cur = self.conn.execute(
            """
            INSERT INTO memory(summary, kind, tags, level, importance, hits,
                               src_table, src_id, created_at, updated_at, ts)
            VALUES (?,?,?,?,?,0,?,?,?,?,?)
            """,
            (summary, kind, tags, level, importance, src_table, src_id, _iso(t), _iso(t), t),
        )
        mid = int(cur.lastrowid or 0)
        if vector:
            self.set_vector(mid, vector)
        self.conn.commit()
        return mid

    def set_vector(self, memory_id: int, vector: list[float]) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO memory_vec(memory_id, dim, vector) VALUES (?,?,?)",
            (memory_id, len(vector), pack_vector(vector)),
        )
        # ⚠️ KNN 索引要一起维护。两处都写是有意的：
        #   · `memory_vec` 是**真相**（原始 BLOB，线性扫描与重建都靠它）；
        #   · `memory_vec0` 是**索引**（KNN 用，可随时从 memory_vec 重建）。
        # 只写索引会让"库被换掉/维度变了"时无法恢复；只写真相同样查不到。
        self.vec_upsert(memory_id, vector)

    # ------------------------------------------------------------ 管理界面用的读写
    #
    # 下面这批方法是给设置页的「记忆」「珍藏」两个标签用的：
    # 列原文、从原文里挑一段存成珍藏、翻看/删除检索记忆。
    #
    # ## 为什么单开一节
    #
    # 上面的方法都是**对话路径**在用的（每轮都会跑，要快、要稳）；
    # 下面这些是**用户手动操作**才触发的，可以慢一点、可以返回更完整的数据
    # （比如带上 id 供界面定位）。混在一起会让"哪些是热路径"变模糊。

    def recent_turns_page(
        self,
        limit: int = 50,
        before_ts: Optional[float] = None,
        since_ts: Optional[float] = None,
        until_ts: Optional[float] = None,
        role: Optional[str] = None,
    ) -> list[sqlite3.Row]:
        """按时间**倒序**取原文，供界面翻看（最近的在最前）。

        ## 分页用时间戳游标，不用 OFFSET

        `before_ts` 是"只取比这个时间更早的"。用游标而不是 OFFSET：
        翻页期间有新对话写进来时，OFFSET 会让同一行被看到两次或漏掉。

        ## 时间范围（用户要求：「加入时间搜索，方便通过时间查询」）

        `since_ts` / `until_ts` 是**闭区间**。两个都可以单独给
        （"从某天起"或"到某天为止"），也可以一起给（"这几天"）。
        它们在 SQL 里过滤而不是取回来再筛 —— 后者会让 LIMIT 先截断，
        范围外的行把范围内挤掉。
        """
        # `has_image` 只暴露"有没有图"，**不把 BLOB 带进列表** ——
        # 一页 50 条各带一张截图，列表接口会变成几十兆。看图另走 `raw_image`。
        sql = (
            "SELECT id, session_id, role, text, kind, created_at, ts,"
            " (image IS NOT NULL) AS has_image FROM raw_log WHERE 1=1"
        )
        args: list[Any] = []
        if before_ts is not None:
            sql += " AND ts < ?"
            args.append(before_ts)
        if since_ts is not None:
            sql += " AND ts >= ?"
            args.append(since_ts)
        if until_ts is not None:
            sql += " AND ts <= ?"
            args.append(until_ts)
        if role in ("user", "assistant"):
            sql += " AND role = ?"
            args.append(role)
        sql += " ORDER BY ts DESC LIMIT ?"
        args.append(max(1, min(1000, int(limit))))
        return list(self.conn.execute(sql, args))

    def raw_image(self, raw_id: int) -> Optional[tuple[bytes, str]]:
        """取某条原文的截图（返回 `(字节, mime)`；没有图返回 None）。"""
        row = self.conn.execute(
            "SELECT image, image_mime FROM raw_log WHERE id = ?", (int(raw_id),)
        ).fetchone()
        if row is None or row[0] is None:
            return None
        return bytes(row[0]), str(row[1] or "image/jpeg")

    def raw_neighborhood(
        self,
        raw_id: int,
        before: int = 5,
        after: int = 5,
    ) -> list[sqlite3.Row]:
        """取某条原文**上下 N 条**的上下文（用户要求：「上下 1-100 条的数据范围」）。

        返回的是**按时间正序**的连续片段（复现对话要按发生顺序读）。

        ## 为什么要按 `id` 而不是按时间戳取邻域

        `id` 是自增的，**天然就是写入顺序**；而时间戳是浮点数，同一毫秒内
        两条记录的先后不可靠（实测同一轮对话的 user/assistant 是同一秒写入的）。
        用 id 取邻域才能保证"上一条"就是真的上一条。

        `before` / `after` 夹在 0~100 —— 用户说「1-100 条」，而 0 是有意义的
        （只要下一条 / 只要上一条）。
        """
        b = max(0, min(100, int(before)))
        a = max(0, min(100, int(after)))
        rows = list(
            self.conn.execute(
                "SELECT id, session_id, role, text, kind, created_at, ts FROM raw_log"
                " WHERE id BETWEEN ? AND ? ORDER BY id ASC",
                (int(raw_id) - b, int(raw_id) + a),
            )
        )
        return rows

    def raw_by_ids(self, ids: Iterable[int]) -> list[sqlite3.Row]:
        """按 id 批量取原文，**按传入顺序**返回。

        为什么要保持传入顺序：界面上的勾选顺序就是用户心里的顺序
        （他先看到哪条、后勾哪条）。按 id 排序会打乱"我想让它按这个次序读"。
        """
        wanted = [int(i) for i in ids]
        if not wanted:
            return []
        placeholders = ",".join("?" * len(wanted))
        rows = {
            int(r["id"]): r
            for r in self.conn.execute(
                "SELECT id, session_id, role, text, kind, created_at, ts FROM raw_log"
                f" WHERE id IN ({placeholders})",
                wanted,
            )
        }
        return [rows[i] for i in wanted if i in rows]

    def get_raw(self, raw_id: int) -> Optional[sqlite3.Row]:
        row = self.conn.execute(
            "SELECT id, session_id, role, text, kind, created_at, ts FROM raw_log WHERE id = ?",
            (int(raw_id),),
        ).fetchone()
        return row

    def search_raw(
        self,
        query: str,
        limit: int = 50,
        *,
        role: Optional[str] = None,
    ) -> list[sqlite3.Row]:
        """在**原文**里找对话。

        ⚠️ 这里**故意不用 FTS** —— `memory_fts` 只索引提炼层（用户的决定：
        「原文记忆是原文本，是全部所有的记忆」，不建索引）。所以这里退化成
        `LIKE` 全表扫。

        代价：行数上万后每次搜索几十毫秒。可接受，因为这是**用户手动搜索**
        （不是每轮都跑的路径），而且 `raw_log` 是纯文本、增长很慢
        （实测 304 轮对话只有十几 KB 文字）。

        `role` 用来只看"AI 说的"或"用户说的" —— 固定注入提示词那个页面要
        专门找**宠物的口头禅**（role='assistant'）或**用户的习惯**（role='user'）。
        在 SQL 里过滤而不是取回来再筛：LIMIT 是在 SQL 层生效的，
        先取 50 条再筛可能一条都不剩。
        """
        like = f"%{query.strip()}%"
        sql = "SELECT id, session_id, role, text, kind, created_at, ts FROM raw_log WHERE text LIKE ?"
        args: list[Any] = [like]
        if role in ("user", "assistant"):
            sql += " AND role = ?"
            args.append(role)
        sql += " ORDER BY ts DESC LIMIT ?"
        args.append(max(1, min(200, int(limit))))
        return list(self.conn.execute(sql, args))

    def list_cherished(self, limit: int = 200) -> list[sqlite3.Row]:
        """全部珍藏记忆（最新在前）。

        带上每段的**轮数** —— 列表里显示"3 轮 / 5 轮"比什么都不显示有用：
        用户能一眼看出哪条是单独一句、哪条是一整段对话。
        """
        return list(
            self.conn.execute(
                """
                SELECT c.id, c.title, c.text, c.session_id, c.src_from, c.src_to,
                       c.pinned, c.created_at, c.ts,
                       (SELECT COUNT(*) FROM cherished_turn t WHERE t.cherished_id = c.id)
                         AS turn_count
                FROM cherished c ORDER BY c.ts DESC LIMIT ?
                """,
                (max(1, min(1000, int(limit))),),
            )
        )

    def get_cherished(self, cher_id: int) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT id, title, text, pinned, created_at FROM cherished WHERE id = ?",
            (int(cher_id),),
        ).fetchone()

    # ------------------------------------------------------------ 珍藏的轮次

    def cherished_turns(self, cher_id: int) -> list[sqlite3.Row]:
        """某段珍藏里的全部轮次（按段内顺序）。

        这是"点开一条珍藏、复现那几轮对话"的数据来源 ——
        `role` 决定气泡方向（用户在右、宠物在左，与微信一致），
        `created_at` 是**原文当时的时间**（复现时要显示当时的时间，
        而不是"什么时候收藏的"）。
        """
        return list(
            self.conn.execute(
                "SELECT id, cherished_id, ordinal, role, text, created_at, ts, src_raw_id"
                " FROM cherished_turn WHERE cherished_id = ? ORDER BY ordinal ASC",
                (int(cher_id),),
            )
        )

    def _resync_cherished_text(self, cher_id: int) -> None:
        """把子表的轮次重新拼回 `cherished.text`（注入用）。

        ⚠️ 每次改动轮次后**必须**调它。忘了调的表现很隐蔽：
        界面上珍藏内容变了，但宠物**注入的还是旧内容** ——
        用户会以为"它没记住我刚加的那几句"。

        拼接格式对齐 `memory2.inject_hint` 的预期（`谁：内容` 逐行），
        让注入出去的样子和原文注入一致。
        """
        rows = self.cherished_turns(cher_id)
        joined = "\n".join(
            f"{'用户' if r['role'] == 'user' else '宠物'}：{r['text']}" for r in rows
        )
        self.conn.execute("UPDATE cherished SET text = ? WHERE id = ?", (joined, int(cher_id)))

    def _next_ordinal(self, cher_id: int) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(MAX(ordinal), -1) + 1 AS n FROM cherished_turn WHERE cherished_id = ?",
            (int(cher_id),),
        ).fetchone()
        return int(row["n"]) if row else 0

    def add_cherished_turns(
        self,
        cher_id: int,
        turns: Iterable[dict[str, Any]],
    ) -> int:
        """往一段珍藏里追加若干轮（用户要求：勾选多条 / 扩充上下文）。

        ⚠️ **不动 `raw_log`**：这里只是把原文的**副本**写进珍藏。

        每条 turn 是 `{role, text, created_at, ts, src_raw_id}`。
        带 `src_raw_id` 的会**跳过已存在的**（唯一索引兜底）——
        用户可能反复点"扩充"，重复添加会让复现里出现两遍同一句话。

        返回实际写入的条数（可能少于传入数，因为有去重）。
        """
        cid = int(cher_id)
        if not self.cherished_turns(cid) and not self.conn.execute(
            "SELECT 1 FROM cherished WHERE id = ?", (cid,)
        ).fetchone():
            return 0

        written = 0
        ordinal = self._next_ordinal(cid)
        existing = {
            int(r["src_raw_id"])
            for r in self.conn.execute(
                "SELECT src_raw_id FROM cherished_turn WHERE cherished_id = ? AND src_raw_id IS NOT NULL",
                (cid,),
            )
        }
        for turn in turns:
            src = turn.get("src_raw_id")
            if src is not None and int(src) in existing:
                continue
            ts = float(turn.get("ts") or time.time())
            self.conn.execute(
                """
                INSERT INTO cherished_turn(cherished_id, ordinal, role, text, created_at, ts, src_raw_id)
                VALUES (?,?,?,?,?,?,?)
                """,
                (
                    cid,
                    ordinal,
                    "user" if turn.get("role") == "user" else "assistant",
                    str(turn.get("text") or ""),
                    str(turn.get("created_at") or _iso(ts)),
                    ts,
                    int(src) if src is not None else None,
                ),
            )
            if src is not None:
                existing.add(int(src))
            ordinal += 1
            written += 1
        if written:
            self._resync_cherished_text(cid)
        self.conn.commit()
        return written

    def remove_cherished_turn(self, turn_id: int) -> bool:
        """从珍藏里**移除某一轮**。

        ⚠️⚠️ **只删 `cherished_turn` 这一行，`raw_log` 一个字都不动。**

        用户明确要求：「这些删除的操作不作用于原文记忆本身的数据，
        只是从那些需要删除操作的珍藏记忆中消失去除掉而已」。

        所以这里**没有**任何针对 raw_log 的删除。删完重算父行的 `text`
        （否则注入的还是包含被删那段的内容）。
        """
        row = self.conn.execute(
            "SELECT cherished_id FROM cherished_turn WHERE id = ?", (int(turn_id),)
        ).fetchone()
        if row is None:
            return False
        cid = int(row["cherished_id"])
        self.conn.execute("DELETE FROM cherished_turn WHERE id = ?", (int(turn_id),))
        self._resync_cherished_text(cid)
        self.conn.commit()
        return True

    def add_cherished(
        self,
        text: str,
        *,
        title: str = "",
        pinned: bool = True,
        session_id: Optional[str] = None,
        src_from: Optional[int] = None,
        src_to: Optional[int] = None,
        turns: Optional[Iterable[dict[str, Any]]] = None,
    ) -> int:
        """新建一段**珍藏记忆**（永久保留，可选固定注入）。

        `turns` 给了就把它作为这段的轮次（复现对话用）；不给则退化成
        "只有一段纯文本"（手动新建走这条路）。

        ⚠️ 有 `turns` 时 `text` 参数会被忽略并在写完后由子表重算 ——
        两处都能提供内容时以**轮次为准**，避免"text 与 turns 不一致"
        （那种不一致会让复现和注入显示不同的东西）。
        """
        t = time.time()
        cur = self.conn.execute(
            """
            INSERT INTO cherished(title, text, session_id, src_from, src_to, pinned, created_at, ts)
            VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                title.strip(),
                text,
                session_id,
                src_from,
                src_to,
                1 if pinned else 0,
                _iso(t),
                t,
            ),
        )
        cid = int(cur.lastrowid or 0)
        if turns is not None:
            self.conn.commit()  # 先落父行，子表要引用它
            self.add_cherished_turns(cid, turns)
            # 轮次为空（比如全被去重了）时别把内容清空 —— 保留传进来的 text
            if not self.cherished_turns(cid) and text.strip():
                self.conn.execute("UPDATE cherished SET text = ? WHERE id = ?", (text, cid))
                self.conn.commit()
        else:
            self.conn.commit()
        return cid

    def remove_cherished(self, cher_id: int) -> bool:
        cur = self.conn.execute("DELETE FROM cherished WHERE id = ?", (int(cher_id),))
        self.conn.commit()
        return bool(cur.rowcount)

    def set_cherished_pinned(self, cher_id: int, pinned: bool) -> bool:
        """切换"是否固定注入"。

        ⚠️ 不用 `pinned = ?` 直接写 0/1 之外的任何东西 —— 表上没加
        CHECK 约束，写进字符串会让 `pinned = 1` 的查询永远匹配不上，
        表现为"珍藏了但宠物从来不提"。所以这里强制转成 int。
        """
        cur = self.conn.execute(
            "UPDATE cherished SET pinned = ? WHERE id = ?",
            (1 if pinned else 0, int(cher_id)),
        )
        self.conn.commit()
        return bool(cur.rowcount)

    def update_cherished(self, cher_id: int, *, title: Optional[str] = None, text: Optional[str] = None) -> bool:
        sets: list[str] = []
        args: list[Any] = []
        if title is not None:
            sets.append("title = ?")
            args.append(title.strip())
        if text is not None:
            sets.append("text = ?")
            args.append(text)
        if not sets:
            return False
        args.append(int(cher_id))
        cur = self.conn.execute(f"UPDATE cherished SET {', '.join(sets)} WHERE id = ?", args)
        self.conn.commit()
        return bool(cur.rowcount)

    def list_memory(self, limit: int = 200, level: Optional[str] = None) -> list[sqlite3.Row]:
        """列出**检索记忆**（可只列某一层）。"""
        sql = (
            "SELECT id, summary, kind, tags, level, importance, hits, created_at"
            " FROM memory"
        )
        args: list[Any] = []
        if level:
            sql += " WHERE level = ?"
            args.append(level)
        sql += " ORDER BY ts DESC LIMIT ?"
        args.append(max(1, min(2000, int(limit))))
        return list(self.conn.execute(sql, args))

    def remove_memory(self, memory_id: int) -> bool:
        """删一条检索记忆。

        `memory_vec` 有 `ON DELETE CASCADE`，但 SQLite 的外键约束**默认是关的**
        —— 必须显式 `PRAGMA foreign_keys = ON` 才生效。所以这里手动删向量，
        不依赖那个可能没打开的约束（否则会留下孤儿向量，检索时命中不存在的 id）。
        """
        mid = int(memory_id)
        self.conn.execute("DELETE FROM memory_vec WHERE memory_id = ?", (mid,))
        # KNN 索引也要删 —— 否则它会命中一个不存在的 memory id
        # （那种"检索到幽灵条目"最难查：正文取不回来，但排名占着位置）
        self.vec_drop(mid)
        cur = self.conn.execute("DELETE FROM memory WHERE id = ?", (mid,))
        self.conn.commit()
        return bool(cur.rowcount)

    def set_memory_level(self, memory_id: int, level: str) -> bool:
        """改检索记忆的层级 —— `fixed` 会变成每次对话无条件注入。"""
        if level not in ("fixed", "recent", "archived"):
            raise ValueError(f"非法层级：{level}")
        cur = self.conn.execute(
            "UPDATE memory SET level = ?, updated_at = ? WHERE id = ?",
            (level, _iso(time.time()), int(memory_id)),
        )
        self.conn.commit()
        return bool(cur.rowcount)

    # ------------------------------------------------------ 记忆压缩（schema v3）

    def merge_memory_into(self, source_ids: list[int], target_id: int) -> int:
        """把若干条记忆标记为**已压缩进** `target_id`（**不删数据**）。

        ## 语义（用户 2026-10-05 拍板）

        「不删只标记」—— 因为「防止一些重要信息提炼后恰好被重复度选中后丢失」。
        被标记的条目：
          · **不参与检索**（`_retrieve` / KNN 都排除，见 A 方案）；
          · 但在**检索层 tab 里看得到**，可一键还原。

        ## ⚠️ 三条护栏

        ① **不能压进自己**（`merged_into = id` 会让过滤逻辑把它自己排除掉）。
        ② **目标必须是活跃的**：不能指向另一个已压缩的条目 —— 那会形成
           链式（A→B→C），查询时要么递归要么漏掉，是 bug 温床。
           所以这里做**扁平化**：目标本身若已被压缩，改指向它的最终目标。
        ③ **一个条目只能属于一个组**（单值字段）。已经在别的组里的，
           要么先还原、要么明确改指 —— 这里直接覆盖并记日志。

        返回实际改动的行数。
        """
        tid = int(target_id)
        if not source_ids:
            return 0
        # 护栏 ②：把目标解析到最终目标（防链）
        row = self.conn.execute(
            "SELECT merged_into FROM memory WHERE id = ?", (tid,)
        ).fetchone()
        if row is None:
            return 0
        seen = {tid}
        while row is not None and row["merged_into"] is not None:
            tid = int(row["merged_into"])
            if tid in seen:  # 数据异常成环 —— 断开，避免死循环
                log.warning("检测到 compressed 链成环（id=%d），已断开", tid)
                break
            seen.add(tid)
            row = self.conn.execute(
                "SELECT merged_into FROM memory WHERE id = ?", (tid,)
            ).fetchone()

        now = _iso(time.time())
        changed = 0
        for sid in source_ids:
            sid = int(sid)
            # 护栏 ①：不能压进自己
            if sid == tid:
                continue
            cur = self.conn.execute(
                "UPDATE memory SET merged_into = ?, merged_at = ?, updated_at = ? WHERE id = ?",
                (tid, now, now, sid),
            )
            changed += int(cur.rowcount or 0)
            # KNN 索引同步：已压缩的不该再被 KNN 命中（A 方案）
            self.vec_drop(sid)
        self.conn.commit()
        return changed

    def unmerge_memory(self, memory_ids: Optional[list[int]] = None) -> int:
        """**还原**：清除压缩标记（`merged_into` 置回 NULL）。

        `memory_ids=None` 表示**全部还原**（UI 上的「全部还原」按钮）。
        返回改动行数。
        """
        now = _iso(time.time())
        if memory_ids is None:
            cur = self.conn.execute(
                "UPDATE memory SET merged_into = NULL, merged_at = NULL, updated_at = ?"
                " WHERE merged_into IS NOT NULL",
                (now,),
            )
            self.conn.commit()
            n = int(cur.rowcount or 0)
            if n:
                self.vec_sync_from_memory()
            return n
        n = 0
        for mid in memory_ids:
            cur = self.conn.execute(
                "UPDATE memory SET merged_into = NULL, merged_at = NULL, updated_at = ?"
                " WHERE id = ?",
                (now, int(mid)),
            )
            n += int(cur.rowcount or 0)
        self.conn.commit()
        if n:
            self.vec_sync_from_memory()
        return n

    def vec_sync_from_memory(self, dim: Optional[int] = None) -> int:
        """把**活跃**（未压缩）的向量补进 KNN 索引，并把不该在的拿掉。

        还原之后必须调它 —— 否则还原的条目虽然活跃了，但 KNN 里没有它，
        **语义检索永远找不回来**（关键词路还能，所以症状很隐蔽）。
        """
        if not self._vec_ok:
            return 0
        try:
            if dim is None:
                row = self.conn.execute(
                    "SELECT LENGTH(vector) AS n FROM memory_vec LIMIT 1"
                ).fetchone()
                dim = (int(row["n"]) // 4) if row and row["n"] else 0
            if dim <= 0:
                return 0
            # ① 补缺失的（活跃且维度正确）
            cur = self.conn.execute(
                "INSERT INTO memory_vec0(memory_id, emb) "
                "SELECT v.memory_id, v.vector FROM memory_vec v "
                "JOIN memory m ON m.id = v.memory_id "
                "WHERE LENGTH(v.vector) = ? AND m.merged_into IS NULL "
                "  AND v.memory_id NOT IN (SELECT memory_id FROM memory_vec0)",
                (dim * 4,),
            )
            added = int(cur.rowcount or 0)
            self.conn.commit()
            if added:
                log.info("KNN 索引补齐（活跃）%d 条", added)
            return added
        except Exception as exc:  # noqa: BLE001
            log.debug("KNN 索引补齐失败（%s）", exc)
            return 0

    def list_merged(self, limit: int = 200) -> list[sqlite3.Row]:
        """列出**已压缩**的条目（供 UI 显示与还原）。"""
        return list(
            self.conn.execute(
                "SELECT m.id, m.summary, m.kind, m.level, m.merged_into, m.merged_at,"
                "       m.created_at, m.ts, t.summary AS target_summary"
                "  FROM memory m LEFT JOIN memory t ON t.id = m.merged_into"
                " WHERE m.merged_into IS NOT NULL"
                " ORDER BY m.merged_at DESC, m.id DESC LIMIT ?",
                (int(limit),),
            )
        )

    def bump_hits(self, memory_ids: list[int]) -> int:
        """把命中过的记忆 `hits` 加一。

        ## ⚠️ 这是**补上一个死字段**（2026-10-05 实测发现）

        `hits` 列从建表起就有，注释写着「被检索命中过多少次（用于淘汰时判断
        '有没有用'）」—— 但**生产路径从来不累加**：
        只有旧的 `memory.py`（已不是生产路径）会加，`memory2._retrieve` 不加。

        后果：`hits` 永远是 0，任何"按使用频率"的判断都无从下手 ——
        包括记忆压缩里"保留哪条作为代表"（最可靠的判据就是"它被用过"）。

        实现用**一条 SQL 批量加**，不是逐条 UPDATE（那是 N 次往返）。
        """
        ids = [int(i) for i in memory_ids if i is not None]
        if not ids:
            return 0
        try:
            ph = ",".join("?" * len(ids))
            cur = self.conn.execute(
                f"UPDATE memory SET hits = hits + 1 WHERE id IN ({ph})", ids
            )
            self.conn.commit()
            return int(cur.rowcount or 0)
        except Exception as exc:  # noqa: BLE001 —— 计数失败绝不该影响对话
            log.debug("累加命中次数失败（%s）", exc)
            return 0

    def bump_importance(self, memory_ids: list[int], delta: float = 0.05) -> int:
        """
        给若干记忆加"重要性"（**强化**，2026-10-17）。

        ## 为什么要这个东西

        用户要求「一大段关联的交流结束后 AI 可自发提炼和强化」。

        `importance` 列从建表起就有，但**生产路径只写过一次 0.5** 就再没变过 ——
        和当初的 `hits` 一样是个死字段。而"强化"恰恰最自然的落点就是它：

        ```
        一段交流里反复被想起的记忆 -> importance 上升 -> 以后更容易被想起
        ```

        这样"聊得多的东西"会自然浮上来，不需要另建一套打分系统。

        ⚠️ 封顶 1.0，且**一次只加一点点**（默认 0.05）。
        强化应该像肌肉记忆那样慢慢积累，不能一次对话就把某条记忆顶到最高。
        """
        ids = [int(i) for i in memory_ids if i is not None]
        if not ids:
            return 0
        try:
            ph = ",".join("?" * len(ids))
            cur = self.conn.execute(
                f"UPDATE memory SET importance = MIN(1.0, importance + ?) WHERE id IN ({ph})",
                [float(delta), *ids],
            )
            self.conn.commit()
            return int(cur.rowcount or 0)
        except Exception as exc:  # noqa: BLE001 —— 强化失败不该影响任何主流程
            log.debug("强化记忆失败（%s）", exc)
            return 0

    # ---------------------------------------------------------- 兴趣（2026-10-17）

    def bump_interests(
        self, terms: list[str], *, strength: str = "weak", source: str = "", delta: float = 0.12
    ) -> int:
        """
        给若干主题加权。不存在就插入，存在就 `weight += delta`、`hits += 1`。

        ## 权重怎么涨（"爱好由对话形成"的具体机制）

        ```
        初始 strong（引号/ASCII）  0.55
        初始 weak（中文分词）       0.25
        每再出现一次               +delta（默认 0.12）
        封顶                       1.0
        ```

        所以「银松镇」要变成明显的爱好，需要反复被提到 —— 这正好符合
        "聊得多的才是兴趣"，而不是提一次就固化成标签。

        ⚠️ 用 `INSERT ... ON CONFLICT(topic) DO UPDATE` 一条语句搞定，
        不做"先查再插"（那是两次往返，且并发下会有竞态）。
        """
        now = time.time()
        cleaned: list[str] = []
        for term in terms or []:
            t = str(term or "").strip()
            if t and len(t) <= 40 and t not in cleaned:
                cleaned.append(t)
        if not cleaned:
            return 0
        base = 0.55 if strength == "strong" else 0.25
        count = 0
        try:
            for topic in cleaned:
                self.conn.execute(
                    """
                    INSERT INTO interest
                        (topic, weight, hits, strength, created_at, last_seen_at, source)
                    VALUES (?, ?, 1, ?, ?, ?, ?)
                    ON CONFLICT(topic) DO UPDATE SET
                        weight       = MIN(1.0, interest.weight + ?),
                        hits         = interest.hits + 1,
                        last_seen_at = ?,
                        source       = ?,
                        strength     = CASE
                                          WHEN ? = 'strong' THEN 'strong'
                                          ELSE interest.strength
                                       END
                    """,
                    (topic, base, strength, now, now, source,
                     delta, now, source, strength),
                )
                count += 1
            self.conn.commit()
            return count
        except Exception as exc:  # noqa: BLE001 —— 兴趣是加分项，失败不能影响对话
            log.debug("更新兴趣失败（%s）", exc)
            try:
                self.conn.rollback()
            except Exception:  # noqa: BLE001
                pass
            return 0

    def top_interests(self, limit: int = 8, min_weight: float = 0.35) -> list[dict[str, Any]]:
        """按权重取前几条兴趣（供提示词与注意力用）。"""
        try:
            rows = self.conn.execute(
                """
                SELECT topic, weight, hits, strength, last_seen_at
                  FROM interest
                 WHERE weight >= ?
                 ORDER BY weight DESC, hits DESC
                 LIMIT ?
                """,
                (float(min_weight), int(limit)),
            ).fetchall()
        except Exception as exc:  # noqa: BLE001
            log.debug("读取兴趣失败（%s）", exc)
            return []
        return [
            {
                "topic": r["topic"],
                "weight": round(float(r["weight"]), 4),
                "hits": int(r["hits"]),
                "strength": r["strength"],
                "last_seen_at": float(r["last_seen_at"]),
            }
            for r in rows
        ]

    def decay_interests(self, factor: float = 0.98) -> int:
        """时间衰减：久不聊的兴趣慢慢变淡，低于阈值就删掉。"""
        try:
            self.conn.execute("UPDATE interest SET weight = weight * ?", (float(factor),))
            cur = self.conn.execute("DELETE FROM interest WHERE weight < 0.15")
            self.conn.commit()
            return int(cur.rowcount or 0)
        except Exception as exc:  # noqa: BLE001
            log.debug("兴趣衰减失败（%s）", exc)
            return 0

    # ------------------------------------------------------ 随机事件（2026-10-17）

    def record_event(
        self,
        kind: str,
        label: str,
        detail: str = "",
        *,
        mood_delta: float = 0.0,
        energy_delta: float = 0.0,
        intimacy_delta: float = 0.0,
        attention_delta: float = 0.0,
        now: Optional[float] = None,
    ) -> int:
        """记一条"它自己做过的事"。返回新行的 id（失败返回 0）。"""
        try:
            cur = self.conn.execute(
                """
                INSERT INTO event_log
                    (kind, label, detail, mood_delta, energy_delta,
                     intimacy_delta, attention_delta, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(kind),
                    str(label),
                    str(detail or ""),
                    float(mood_delta),
                    float(energy_delta),
                    float(intimacy_delta),
                    float(attention_delta),
                    float(now if now is not None else time.time()),
                ),
            )
            self.conn.commit()
            return int(cur.lastrowid or 0)
        except Exception as exc:  # noqa: BLE001 —— 记录失败绝不能影响主流程
            log.debug("记录随机事件失败（%s）", exc)
            return 0

    def recent_events(self, limit: int = 5) -> list[dict[str, Any]]:
        """最近发生过的事件（供提示词与设置页）。"""
        try:
            rows = self.conn.execute(
                """
                SELECT id, kind, label, detail, created_at
                  FROM event_log
                 ORDER BY created_at DESC
                 LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        except Exception as exc:  # noqa: BLE001
            log.debug("读取事件失败（%s）", exc)
            return []
        return [
            {
                "id": int(r["id"]),
                "kind": r["kind"],
                "label": r["label"],
                "detail": r["detail"],
                "created_at": float(r["created_at"]),
            }
            for r in rows
        ]

    def event_count(self, kind: str, since: float) -> int:
        """某个事件在 `since` 之后触发过几次（冷却判断用）。"""
        try:
            row = self.conn.execute(
                "SELECT COUNT(*) FROM event_log WHERE kind = ? AND created_at >= ?",
                (str(kind), float(since)),
            ).fetchone()
            return int(row[0]) if row else 0
        except Exception as exc:  # noqa: BLE001
            log.debug("统计事件次数失败（%s）", exc)
            return 0

    def last_event_at(self) -> float:
        """最近一次事件的时间戳（没有则 0）。全局最小间隔用。"""
        try:
            row = self.conn.execute("SELECT MAX(created_at) FROM event_log").fetchone()
            return float(row[0] or 0.0) if row else 0.0
        except Exception as exc:  # noqa: BLE001
            return 0.0

    def count_events_since(self, since: float) -> int:
        """`since` 之后一共触发过几次（日上限判断用）。"""
        try:
            row = self.conn.execute(
                "SELECT COUNT(*) FROM event_log WHERE created_at >= ?", (float(since),)
            ).fetchone()
            return int(row[0]) if row else 0
        except Exception as exc:  # noqa: BLE001
            log.debug("统计事件总数失败（%s）", exc)
            return 0

    # ------------------------------------------------------------ 印象

    def add_impression(self, text: str, source: str = "", now: Optional[float] = None) -> int:
        """加一条对用户的印象。**去重**由文本精确匹配完成（同一句话不重复记）。"""
        body = str(text or "").strip()
        if not body:
            return 0
        try:
            exists = self.conn.execute(
                "SELECT id FROM impression WHERE text = ?", (body,)
            ).fetchone()
            if exists:
                return int(exists["id"])
            cur = self.conn.execute(
                "INSERT INTO impression (text, source, created_at) VALUES (?, ?, ?)",
                (body, str(source or ""), float(now if now is not None else time.time())),
            )
            self.conn.commit()
            return int(cur.lastrowid or 0)
        except Exception as exc:  # noqa: BLE001
            log.debug("写入印象失败（%s）", exc)
            return 0

    def recent_impressions(self, limit: int = 6) -> list[dict[str, Any]]:
        try:
            rows = self.conn.execute(
                """
                SELECT id, text, source, created_at
                  FROM impression
                 ORDER BY created_at DESC
                 LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        except Exception as exc:  # noqa: BLE001
            log.debug("读取印象失败（%s）", exc)
            return []
        return [
            {
                "id": int(r["id"]),
                "text": r["text"],
                "source": r["source"],
                "created_at": float(r["created_at"]),
            }
            for r in rows
        ]

    def count_active(self) -> int:
        """参与检索的条数（未压缩）。界面用它显示"净化后还剩多少"。"""
        try:
            return int(
                self.conn.execute(
                    "SELECT COUNT(*) FROM memory WHERE merged_into IS NULL"
                ).fetchone()[0]
            )
        except sqlite3.Error:
            return 0

    def count_merged(self) -> int:
        """已压缩的条数。"""
        try:
            return int(
                self.conn.execute(
                    "SELECT COUNT(*) FROM memory WHERE merged_into IS NOT NULL"
                ).fetchone()[0]
            )
        except sqlite3.Error:
            return 0

    def find_memory_by_text(self, text: str, level: Optional[str] = None) -> Optional[sqlite3.Row]:
        """按**正文精确匹配**找一条记忆。

        ## 为什么需要它（而不是靠列表里的 id）

        「固定注入提示词」那个页面是从**原文搜索结果**出发的 —— 同一句话可能
        被反复加入，或者在别处删掉了、这里还留着选中状态。

        用一个"按内容查重"的入口，让"加入固定注入"这个动作**天然幂等**：
        已经在了就返回那一条，而不是每次都新增一份（否则同一句口头禅会
        在注入里出现十遍，很占提示词）。
        """
        sql = "SELECT id, summary, kind, tags, level, importance, hits, created_at FROM memory WHERE summary = ?"
        args: list[Any] = [text]
        if level:
            sql += " AND level = ?"
            args.append(level)
        sql += " LIMIT 1"
        return self.conn.execute(sql, args).fetchone()

    # ------------------------------------------------------------ 读取

    def recent_turns(self, since_ts: float, limit: int = 500) -> list[sqlite3.Row]:
        """取时间窗口内的对话 —— 这就是"最近记忆"的注入来源。

        走 `idx_rt_time` 索引，实测 3000 行里取 1 个月只要 ~1ms。
        """
        return list(
            self.conn.execute(
                "SELECT id, session_id, role, text, created_at, ts FROM recent_turn"
                " WHERE ts >= ? ORDER BY ts ASC LIMIT ?",
                (since_ts, limit),
            )
        )

    def fixed_memories(self) -> list[sqlite3.Row]:
        """固定注入的记忆（习惯 / 人设 / 常说的话）—— 每次对话无条件带上。

        ⚠️ 返回 `ts` / `created_at`：提示词要给**每条记忆**带时间（用户要求），
        固定注入里的条目可能是很久以前定的，AI 需要知道。
        """
        return list(
            self.conn.execute(
                "SELECT id, summary, kind, tags, level, importance, hits, ts, created_at"
                "  FROM memory"
                " WHERE level = 'fixed' AND merged_into IS NULL"
                " ORDER BY importance DESC"
            )
        )

    def pinned_cherished(self) -> list[sqlite3.Row]:
        """固定注入的珍藏记忆（原文，可以很长）。

        ⚠️ 也带上 `ts`（原来是只有 `created_at`）—— 两种时间格式都要，
        `ts` 好算差值（"3 天前"），`created_at` 是给人看的 ISO 串。
        """
        return list(
            self.conn.execute(
                "SELECT id, title, text, created_at, ts FROM cherished"
                " WHERE pinned = 1 ORDER BY ts DESC"
            )
        )

    def search_memory(self, query: str, limit: int = 5) -> list[sqlite3.Row]:
        """关键词检索（中文走 `jieba_query` 改写，精度靠它）。

        ⚠️ **排除已压缩的**（`merged_into IS NOT NULL`）—— 用户选定的 A 方案：
        压缩过的从检索池里拿掉，只有检索层 tab 能看到并还原。
        """
        expr = self.query_expr(query)
        try:
            return list(
                self.conn.execute(
                    """
                    SELECT m.id, m.summary, m.kind, m.tags, m.level, m.importance,
                           m.src_table, m.src_id, m.created_at,
                           bm25(memory_fts) AS score
                    FROM memory_fts f
                    JOIN memory m ON m.id = f.rowid
                    WHERE memory_fts MATCH ?
                      AND m.merged_into IS NULL
                    ORDER BY score
                    LIMIT ?
                    """,
                    (expr, limit),
                )
            )
        except sqlite3.OperationalError as exc:
            log.warning("全文检索失败（已忽略）：%s", exc)
            return []

    def all_vectors(self) -> list[tuple[int, list[float]]]:
        """取出全部向量，给"语义检索"用。

        ⚠️ 现在是 Python 侧线性扫描 —— 3000 条约 142ms（实测）。
        规模再大要换 `sqlite-vec` 之类的扩展把计算下推到 SQL 层。

        ⚠️ **检索路径不要用这个方法** —— 它逐条 `unpack_vector` 成 Python
        list，而调用方（numpy 批量余弦）又要转回数组，等于白绕一趟。
        实测 2000 条时这一步占 **61.5ms**（是当时的最大单项开销）。
        检索请用 `all_vectors_raw()`。
        """
        out: list[tuple[int, list[float]]] = []
        for row in self.conn.execute("SELECT memory_id, vector FROM memory_vec"):
            out.append((int(row["memory_id"]), unpack_vector(row["vector"])))
        return out

    def all_vectors_raw(self, active_only: bool = True) -> tuple[list[int], list[bytes], int]:
        """取向量的**原始 BLOB**，供 numpy 批量计算用。

        返回 `(ids, blobs, dim)`；`dim` 是 0 表示库里没有向量。

        ## `active_only`（默认 True）

        **排除已压缩的条目**（`merged_into IS NOT NULL`）—— 用户选定的 A 方案。
        这条 JOIN 是"净化召回"真正的落点：压缩之后，那些冗余条目
        **不再占检索名额**。

        传 `False` 可以拿到全量（压缩页的"扫描重复"要看到所有条目，
        包括已经压过的，否则重复扫描会漏掉它们）。

        ## 为什么快

        `vector` 列存的就是 `float32` 的紧凑字节（`pack_vector` 写的
        `<{n}f`），而 numpy 能**直接读同一块内存**（`np.frombuffer`）——
        不需要逐条 `struct.unpack` 成 Python float 列表再转回来。

        实测 2000 条（512 维）：**61.5ms -> 约 3ms**。

        ## 兼容性

        行长度不一致时以**第一行**的维度为准；维度不符的行由调用方
        （`_retrieve` / `retrieve_debug`）筛掉并计数 —— 那种情况意味着
        换过 embedding 实现或库被换成旧备份，需要显式暴露而不是静默。
        """
        ids: list[int] = []
        blobs: list[bytes] = []
        dim = 0
        sql = (
            "SELECT v.memory_id, v.vector FROM memory_vec v "
            "JOIN memory m ON m.id = v.memory_id "
        )
        if active_only:
            sql += "WHERE m.merged_into IS NULL "
        sql += "ORDER BY v.memory_id"
        for row in self.conn.execute(sql):
            blob = bytes(row["vector"] or b"")
            width = len(blob) // 4
            if dim == 0:
                dim = width
            elif width != dim:
                # 维度混杂：仍然返回，但让调用方按 dim 筛（它会看见长度不符）
                pass
            ids.append(int(row["memory_id"]))
            blobs.append(blob)
        return ids, blobs, dim

    def count(self) -> dict[str, int]:
        tables = (
            "recent_turn",
            "raw_log",
            "cherished",
            "cherished_turn",
            "memory",
            "memory_vec",
        )
        out: dict[str, int] = {}
        for t in tables:
            out[t] = int(self.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])
        return out

    # ------------------------------------------------------------ 备份

    def _snapshot_into(self, folder: Path, stamp: str) -> Path:
        """把库用 `VACUUM INTO` 快照到 `folder`，返回实际产出的路径。

        失败抛 `sqlite3.OperationalError`（调用方决定怎么解释）。
        目标已存在时换一个带毫秒的名字 —— 同一秒内连续备份两次会撞名。
        """
        folder.mkdir(parents=True, exist_ok=True)
        # ⚠️ `VACUUM INTO` 不能在事务里跑（实测报 `cannot VACUUM from within a transaction`）。
        # 前面有未提交的写入时必须先落地，否则备份功能整个不可用。
        try:
            self.conn.commit()
        except sqlite3.Error:
            pass
        for suffix in ("", f"-{int(time.time() * 1000) % 1000:03d}"):
            target = folder / f"pet-{stamp}{suffix}.db"
            try:
                self.conn.execute("VACUUM INTO ?", (str(target),))
                return target
            except sqlite3.OperationalError:
                continue
        # 两次都失败 -> 抛出最后一次的错误，由调用方组织消息
        target = folder / f"pet-{stamp}.db"
        self.conn.execute("VACUUM INTO ?", (str(target),))
        return target  # pragma: no cover —— 上面没抛就说明成功了

    def _prune_backups(self, folder: Path, keep: int) -> int:
        """按 `keep` 清理旧备份。`keep <= 0` 表示**保留全部**。

        返回删掉的份数。
        """
        if keep <= 0:
            return 0
        try:
            olds = sorted(folder.glob("pet-*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
            removed = 0
            for old in olds[keep:]:
                old.unlink(missing_ok=True)
                removed += 1
            return removed
        except OSError:
            return 0

    def backup_to(self, folder: Path, keep: int = 20, stage_dir: Optional[Path] = None) -> BackupResult:
        """备份到指定文件夹，并只保留最近 `keep` 份。

        ## 两段式：Python 生成快照 -> （必要时）主进程搬运

        用 SQLite 官方的 `VACUUM INTO`，而**不是**直接复制文件：
        直接复制正在写入的库（尤其 WAL 模式下还有 -wal/-shm）可能拿到不一致的快照，
        而 `VACUUM INTO` 产出的是一份**完整且一致**的新库。

        ## ⚠️ 第一段失败时退到 staging，而不是直接报错

        实测：**Python 子进程对某些文件夹没有写权限，而 Electron 主进程有**
        （同一台机器、同一个文件夹，Python `PermissionError`，主进程复制成功）。
        所以这里先往 `stage_dir`（项目内，必然可写）生成快照，
        然后返回 `staged=True` 让主进程完成"复制到目标文件夹"。

        这不是降级 —— **快照仍然是一致且完整的**，只是搬运工换了个人。
        """
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())

        try:
            target = self._snapshot_into(folder, stamp)
        except OSError as exc:
            # 文件夹建不出来（权限）—— 直接走 staging
            if stage_dir is None:
                return BackupResult(False, f"备份文件夹不可写：{exc}")
            return self._stage(folder, stage_dir, stamp, f"目标文件夹不可写（{exc}）")
        except sqlite3.OperationalError as exc:
            if stage_dir is None:
                return BackupResult(False, f"备份失败：{exc}")
            return self._stage(folder, stage_dir, stamp, f"直接写入失败（{exc}）")

        removed = self._prune_backups(folder, keep)
        size_kb = target.stat().st_size / 1024 if target.exists() else 0
        extra = f"，清理旧备份 {removed} 份" if removed else ""
        return BackupResult(True, f"已备份（{size_kb:.0f} KB）{extra}", str(target))

    def _stage(self, folder: Path, stage_dir: Path, stamp: str, why: str) -> BackupResult:
        """在 staging 目录生成快照，交给主进程复制过去。"""
        try:
            staged = self._snapshot_into(stage_dir, stamp)
        except (OSError, sqlite3.OperationalError) as exc:
            return BackupResult(False, f"备份失败（{why}；暂存也失败：{exc}）")
        size_kb = staged.stat().st_size / 1024 if staged.exists() else 0
        return BackupResult(
            True,
            f"已生成快照（{size_kb:.0f} KB），待复制到 {folder}",
            str(staged),
            staged=True,
            target_dir=str(folder),
        )

    def close(self) -> None:
        try:
            self.conn.commit()
            self.conn.close()
        except sqlite3.Error:
            pass


def default_db_path(project_root: Path) -> Path:
    """默认落点：`<数据目录>/memory/pet.db`（用户指定"记忆留在本地"）。

    为什么不用 `~/.desktop-pet/`：用户要求"记忆全部保留在本地项目中"，
    而且实测受限环境里写用户目录会 EPERM。

    ⚠️ 2026-10-18 打包时发现必须走 `paths.data_dir()`：
    打包态 `project_root` 是 `resources/`（只读资源根），照它算就会把记忆库
    建进安装目录 —— 升级/卸载直接丢数据。`data_dir()` 在开发态仍等于
    `<repo>/apps/desktop/pet-data`，行为不变。
    """
    return paths.data_dir() / "memory" / "pet.db"


def load_simple_extension(conn: sqlite3.Connection, dll: Path) -> bool:
    """尝试加载 `simple.dll`，成功返回 True。

    ⚠️ **调用前必须把进程 cwd 切到 DLL 所在目录** —— cppjieba 用相对路径
    `./dict/jieba.dict.utf8` 找词典，cwd 不对会**静默**退化成逐字切。
    本函数会检查 cwd 并打日志，让"退化"这件事可见。
    """
    if not dll.exists():
        log.warning("未找到 %s —— 中文检索将退化为逐字", dll)
        return False
    expected = dll.parent / "dict" / "jieba.dict.utf8"
    if not expected.exists():
        log.warning("未找到词典 %s —— 中文检索将退化为逐字", expected)
    cwd = Path.cwd()
    if cwd != dll.parent:
        log.warning(
            "⚠️ 当前 cwd=%s，而分词词典要求 cwd=%s —— 中文分词会退化为逐字切",
            cwd,
            dll.parent,
        )
    try:
        conn.enable_load_extension(True)
        conn.load_extension(str(dll))
        return True
    except (sqlite3.OperationalError, AttributeError) as exc:
        log.warning("加载 simple 扩展失败：%s", exc)
        return False
