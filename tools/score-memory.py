"""记忆重要性打分器（独立工具，**不进热路径**）。

用户要求（2026-10-20）：

> 「提炼打分那个可以做成工具，后续需要评估的时候能用来优化结构」

所以它不是一个"顺手接上去"的模块，而是：

  · **可单独运行**：给任意一批记忆条目打分 / 打分并报告分布
  · **可评估**：同一批条目用不同参数跑，对比结果，用来调规则
  · **可回写**：`--apply` 把分数写回库（之后 `recent_memories` 的排序才有意义）
  · **不花 API 的钱**：纯规则打分，不调模型

## 为什么用规则，不用模型打分

1. **要能复现**：同一批条目两次跑必须同一个结果，否则"评估"无从谈起；
2. **要能调**：规则在下面一张表里，改一处就能看效果；
3. **不能花钱**：回填 800 条原文时如果每条都调一次模型打分，成本和慢都受不了。

模型打分更"准"，但它是黑盒 —— 用它来"评估结构"会变成"用另一个黑盒验证黑盒"。
规则打分的分数不完美，但**每一次调整都可解释**。

## 用法

    cd apps/brain
    $env:PYTHONPATH="F:\\ai\\apps\\brain"

    # ① 看看库里现在的分布（不改数据）
    python F:\\ai\\tools\\score-memory.py

    # ② 给一批文本打分（评估用，不碰库）
    python F:\\ai\\tools\\score-memory.py --text "用户喜欢《明日方舟》的曲娘" --text "用户认为人生岔路上的选择由自己做出"

    # ③ 把分数写回库（`recent_memories` 的排序靠它）
    python F:\\ai\\tools\\score-memory.py --apply
    python F:\\ai\\tools\\score-memory.py --apply --dry-run    # 先看会改哪些

    # ④ 评估：对比两种权重方案（不写库）
    python F:\\ai\\tools\\score-memory.py --compare

退出码 0 正常。
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

# ⚠️ 只在**作为脚本运行**时才改 stdout 编码（2026-10-20）。
#    被 ackfill-memory.py import 时不能改调用方的 stdout —— 那会把它的输出接管掉。
#    这个模块被设计成既能跑、也能被导入，两边都要能用。
if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ───────────────────────────────────────────────────────── 打分规则
#
# ## 分数怎么来的
#
# 起点 0.5（`remember_facts` 原来的默认值），然后按四组信号加减：
#
#   · 具体实体（作品名/人名/设备/软件）      +0.18 ~ +0.30   <- 最有信息量
#   · 长期偏好 / 身份（喜欢、在玩、住在）    +0.10
#   · 转述对话（"用户认为…""聊过"）          -0.25 ~ -0.35   <- 实测最大噪音源
#   · 一次性事件（"今天""刚才""不小心"）      -0.15
#
# 分数夹在 0.05 .. 0.95，避免出现"绝对可信/完全无用"这种断言。
#
# ⚠️ **这张表就是"结构"**。用户要"评估时能优化结构"，改的就是这里。
#    改完用 `--compare` 或 `--apply --dry-run` 看分布变化，再决定要不要落地。

BASE = 0.5
FLOOR = 0.05
CEIL = 0.95

# 具体实体：这些出现说明记的是"能指着认出来的东西"
ENTITY_BOOST = 0.30
# 长期偏好/身份
PREFERENCE_BOOST = 0.10
# 转述对话（实测最大噪音源，见 §一百七十六）
TRANSCRIPT_PENALTY = 0.30
# 一次性事件
ONESHOT_PENALTY = 0.15

# 引号/书名号里的名字（最强信号：作者已经标注了"这是个名字"）
_QUOTED = re.compile(
    r"《[^》]{1,40}》"
    r"|「[^」]{1,40}」"
    r"|“[^”]{1,40}”"
    r"|\"[^\"]{1,40}\""
)
# 设备 / 软件 / 作品名（ASCII 专名）
_ASCII_NAME = re.compile(r"\b[A-Za-z][A-Za-z0-9_+.\-]{1,30}\b")

# 长期偏好 / 身份
_PREFERENCE_WORDS = (
    "喜欢", "爱玩", "爱听", "爱好", "在玩", "在写", "在做", "在学", "习惯",
    "住在", "上班", "职业", "专业", "养了", "收藏", "关注", "常用", "用",
)
# 转述对话：以这些开头/包含这些，说明记的是"聊过什么"而不是"是什么"
_TRANSCRIPT_PREFIX = (
    "用户认为", "用户觉得", "用户表达过", "用户问过", "用户说过",
    "用户希望", "宠物认为", "宠物觉得", "用户和宠物", "用户向宠物",
)
_TRANSCRIPT_CONTAINS = (
    "聊过", "讨论过", "问过宠物", "宠物说", "宠物表示", "宠物答",
    "说起来", "提到过", "回应了", "夸宠物",
)
# 一次性事件：今天/刚才/这一次
_ONESHOT_WORDS = (
    "今天", "昨天", "刚才", "刚刚", "这次", "这一次", "不小心", "一时",
    "今天中午", "早上", "晚上", "此刻",
)

# 停用词：这些 ASCII 词不算"具体实体"（技术名词太泛，天天出现）
_ASCII_STOP = {
    "ai", "api", "the", "and", "for", "you", "not", "but", "all",
    "electron", "python", "sqlite", "json", "http", "https", "url",
    "ok", "yes", "no", "is", "in", "on", "at", "to", "of", "it",
}


@dataclass
class ScoreDetail:
    """一条记忆的评分明细（**可解释**是这个工具的核心要求）。"""

    text: str
    score: float
    signals: list[tuple[str, float]] = field(default_factory=list)

    def explain(self) -> str:
        if not self.signals:
            return f"{self.score:.2f}  （无信号，取基准 {BASE}）"
        parts = " ".join(f"{name}{delta:+.2f}" for name, delta in self.signals)
        return f"{self.score:.2f}  {parts}"


def score_text(text: str, weights: dict[str, float] | None = None) -> ScoreDetail:
    """给一条记忆打分，返回**带明细**的结果。"""
    w = weights or {}
    entity = w.get("entity", ENTITY_BOOST)
    pref = w.get("preference", PREFERENCE_BOOST)
    transcript = w.get("transcript", TRANSCRIPT_PENALTY)
    oneshot = w.get("oneshot", ONESHOT_PENALTY)

    raw = str(text or "").strip()
    detail = ScoreDetail(text=raw, score=BASE)
    if not raw:
        detail.score = FLOOR
        return detail

    def bump(name: str, delta: float) -> None:
        detail.signals.append((name, delta))
        detail.score += delta

    # ① 具体实体
    quoted = [m.group(0) for m in _QUOTED.finditer(raw)]
    ascii_names = [
        m.group(0) for m in _ASCII_NAME.finditer(raw)
        if m.group(0).lower() not in _ASCII_STOP and len(m.group(0)) >= 2
    ]
    if quoted:
        bump("书名/引号专名", entity)
    elif ascii_names:
        bump("英文专名", entity * 0.8)
    elif re.search(r"[\u4e00-\u9fff]{2,}(镇|城|岛|游戏|公司|大学|区|路)", raw):
        # 中文明名（"银松镇""滨江区"）—— 弱一点的信号
        bump("中文专名", entity * 0.55)

    # ② 长期偏好 / 身份
    if any(word in raw for word in _PREFERENCE_WORDS):
        bump("长期偏好/身份", pref)

    # ③ 转述对话（实测最大噪音源）
    if raw.startswith(_TRANSCRIPT_PREFIX):
        bump("转述对话", -transcript)
    elif any(word in raw for word in _TRANSCRIPT_CONTAINS):
        bump("转述对话(含)", -transcript * 0.7)

    # ④ 一次性事件
    if any(word in raw for word in _ONESHOT_WORDS):
        bump("一次性事件", -oneshot)

    # ⑤ 长度信号：一句话事实最有用；超长的多半是转述
    if len(raw) > 100:
        bump("过长", -0.08)
    elif len(raw) < 10:
        bump("过短", -0.10)

    detail.score = max(FLOOR, min(CEIL, round(detail.score, 3)))
    return detail


# ───────────────────────────────────────────── 判定"纯聊天记录"（2026-10-20）
#
# ## 为什么不能只靠分数
#
# 实测 177 条里，0.30 那一档**混着两类东西**：
#
#     用户认为明河是男性，祀是女性               <- 用户设定的角色，要留
#     用户希望宠物少附和、多讨论，喜欢有主见       <- 真实偏好，要留
#     宠物认为这段文字讲的是落不完的想念           <- 它的感受，可归档
#
# 所以清理**按类型判，不按分数**。分数只用来排序（先看最像废话的）。
#
# ## 判据（宁可留，不可误删）
#
#   ① 宠物视角            -> 归档（记的是它的感受，不是用户的事）
#   ② 对话元信息          -> 归档（"聊过什么""隔多久"，不是用户的信息）
#   ③ 用户观点 + 纯抽象词  -> 归档（"每个人都有的存在之根"）
#   ④ 用户观点 + 具体对象  -> **保留**（"《终末地》里回技力的角色太弱"是真评价）
#   ⑤ 含偏好词            -> **保留**（即使是"希望…学会"这种抽象表达）

_PET_SUBJECT = re.compile(r"^(宠物|它|助手)认为|宠物(说|表示|答|觉得)|宠物念过")

_USER_OPINION = re.compile(r"^用户(认为|觉得|表达过|问过|说过|希望)")

_ABSTRACT_WORDS = (
    "人生", "意义", "存在", "价值", "哲学", "生命", "活着", "死亡", "灵魂",
    "世界", "宇宙", "每个人", "本质", "真理", "自由", "孤独", "痛苦", "温度",
    "习惯", "选择", "记忆", "情感", "感觉", "心事", "想念", "渴望", "期盼",
    "没法计算", "相互映照", "参照", "重量", "精华", "骨架", "血肉",
)

_CONCRETE = re.compile(
    r"《[^》]+》"
    r"|[\u4e00-\u9fff]{2,}(游戏|视频|番|版本|角色|立绘|壁纸|面包|菜|小说|书)"
    r"|\b[A-Za-z]{2,}\b"
)

# 偏好表达：这些是"用户的设定/口味"，即使没有具体名词也要留
_PREFERENCE = re.compile(r"(喜欢|不喜欢|讨厌|爱吃|爱听|习惯|希望|想要|打算|自称|不盲从)")

# 「用户认为X是Y」这种**角色/世界观设定**要留
_SETTING_PATTERN = re.compile(r"(是|叫|姓)[^，。]{1,12}(的|，|$)")

# 「用户自述」——用户**本人说的话/感受**，即使抽象也要留（2026-10-20 加）
#
# ## 为什么单独一类
#
# 第一版把这些也判成"纯抽象观点"要归档：
#
#     用户表达过"从没愿意来这世间走一遭"、觉得自己并非自愿来到世上
#     用户说过"我在这就是活着"，宠物对此不认同，觉得这话有点凉
#
# 但它们**是用户亲口说的**，不是宠物在自说自话。宠物知不知道
# "用户怎么看活着"会直接影响它怎么回话 —— 这类信息价值很高，
# 只是**抽象**，不等于**没用**。
#
# 区分点：
#   · 「宠物认为…」-> 它的感受 -> 可归档
#   · 「用户表达过/说过『…』」-> 用户的原话 -> **留**
_USER_DISCLOSURE = re.compile(
    r"用户(表达过|说过|说过自己|坦白过|说|讲|提到过)"
    r"|用户(觉得|认为自己)(?![^，。]*?(游戏|视频|角色|版本))"
)

_META_PATTERNS = (
    "常隔十几小时", "对话常", "聊过", "讨论过", "问过宠物", "读了一段",
    "把之前那段", "逐张截图", "说蓝发女仆",
)


def _has_concrete(raw: str) -> bool:
    if _CONCRETE.search(raw):
        return True
    # 名字式设定：「认为 X 是 Y」，X 是 2~6 字中文（"明河""祀"）
    if _SETTING_PATTERN.search(raw) and re.search(r"认为[^，。]{2,6}是", raw):
        return True
    return False


def classify_transcript(text: str) -> tuple[str, str]:
    """判定这条记忆是不是**纯聊天记录**。返回 `("keep"|"drop", 理由)`。

    `drop` = 归档它不会让宠物少知道用户任何事。
    """
    raw = str(text or "").strip()
    if not raw:
        return "drop", "空"

    # ① 宠物视角
    #
    # ⚠️ 包含「用户和宠物讨论过…宠物认为…」这种**混合句**（2026-10-20 自检抓到）。
    #
    # 它既有"讨论过"（元信息）又有"具体对象"（记忆/选择/习惯），
    # 按旧顺序会被元信息分支里的"含具体对象就留"救下来 —— 但它记录的是
    # **宠物怎么想的**，不是用户的信息。
    #
    # 判据：句子里出现"宠物认为/宠物说/宠物答"这类**宠物作主语**的片段，
    # 就说明这条的主语已经跑到宠物那边去了。
    if _PET_SUBJECT.search(raw) or re.search(r"宠物(认为|觉得|说|表示|答|念过)", raw):
        return "drop", "宠物视角（它的感受，不是用户的事实）"

    # ② 用户自述（用户亲口说的话/感受）—— **必须**放在最前面，且不依赖引号。
    #
    # ## 两个实测坑（2026-10-20）
    #
    # ① 「用户表达过"从没愿意来这世间走一遭"」—— 提入库时**引号被剥掉了**，
    #    所以"必须有引号"这个条件会让它漏判；
    # ② 「用户说过"我在这就是活着"，宠物对此不认同」—— 按顺序先命中了
    #    后面的"用户观点"分支，自述判据根本没被执行到。
    #
    # 结论：**自述是身份级信息，应该无条件优先**，不要求引号证据。
    # 它和「宠物认为…」的区别在于**主语**：用户亲口说的是用户的事。
    if _USER_DISCLOSURE.search(raw):
        return "keep", "用户自述（用户亲口说的，即使抽象也是他的事）"

    # ③ 对话元信息
    for pat in _META_PATTERNS:
        if pat in raw:
            if _has_concrete(raw):
                return "keep", "含元信息但也含具体对象"
            return "drop", f"对话元信息（{pat}）"

    # ④ 用户观点
    if _USER_OPINION.search(raw):
        # ⚠️ 先看偏好：含抽象词的"希望…学会"是**行为偏好**，要留
        if _PREFERENCE.search(raw):
            return "keep", "真实偏好（即使句子含抽象词）"
        if _has_concrete(raw):
            return "keep", "含具体对象（即使是评价，也有信息量）"
        abstracts = [w for w in _ABSTRACT_WORDS if w in raw]
        if abstracts:
            return "drop", f"纯抽象观点（{abstracts[0]}）"
        return "drop", "无具体对象的观点"

    return "keep", ""


# ───────────────────────────────────────────────────────── 库操作

# `simple` 分词器的 dll（库的 FTS 表用了它）
# ⚠️ 连库后必须显式 load_extension，否则**写操作**会报 `no such tokenizer: simple`
_DLL_PATH = Path(
    r"F:\ai\apps\desktop\pet-data\vendor\simple\libsimple-windows-x64\simple.dll"
)


def _prepare_tokenizer(conn: sqlite3.Connection) -> None:
    """给连接装上 `simple` 分词器扩展（2026-10-20 踩到）。

    ## 为什么会报 `no such tokenizer: simple`

    这个库的 `memory_fts` 表是用 `tokenizer="simple"` 建的（cppjieba 扩展）。
    SQLite 扩展是**按连接**加载的 —— sidecar 建库时会
    `enable_load_extension(True)` + `load_extension(simple.dll)`，
    而这个独立工具**没做那一步**，于是：

      · 读：多数查询能跑（不碰 FTS 表时）；
      · 写：`UPDATE memory` 会触发 FTS 索引维护 -> 需要分词器 -> 直接抛错。

    报错点在 `conn.execute("UPDATE ...")` 那一行，**完全看不出**根因是
    "扩展没加载"。第一次跑 `--apply` 就是被这个挡住的。

    ⚠️ 只 `chdir` 到 dll 目录是**不够**的 —— 那只影响"能不能找到"，
    不会自动加载。必须显式 `load_extension`。
    """
    if not _DLL_PATH.exists():
        return
    try:
        conn.enable_load_extension(True)
        conn.load_extension(str(_DLL_PATH))
        conn.enable_load_extension(False)
    except Exception:  # noqa: BLE001 —— 加载失败不致命（纯读也能用）
        pass


def open_db(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise SystemExit(f"找不到数据库：{path}")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    _prepare_tokenizer(conn)
    return conn


def open_db_rw(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise SystemExit(f"找不到数据库：{path}")
    conn = sqlite3.connect(str(path))
    _prepare_tokenizer(conn)
    return conn


def default_db() -> Path:
    """库的默认落点（与 `memory2._resolve_db_path` 同一处）。"""
    candidates = [
        Path(r"F:\ai\apps\desktop\pet-data\memory\pet.db"),
        Path.home() / "AppData" / "Roaming" / "DesktopPet" / "pet-data" / "memory" / "pet.db",
    ]
    for cand in candidates:
        if cand.exists():
            return cand
    raise SystemExit(f"找不到记忆库，试过：{[str(c) for c in candidates]}")


def fetch_rows(conn: sqlite3.Connection, level: str | None = None) -> list[tuple[int, str, str, float]]:
    sql = "SELECT id, summary, level, importance FROM memory WHERE merged_into IS NULL"
    args: list[Any] = []
    if level:
        sql += " AND level = ?"
        args.append(level)
    sql += " ORDER BY id"
    return [(int(r[0]), str(r[1] or ""), str(r[2] or ""), float(r[3] or 0.0)) for r in conn.execute(sql, args)]


def histogram(scores: Iterable[float]) -> str:
    """把分布画成 10 个桶（评估用，一眼看出"是不是全挤在 0.5"）。"""
    buckets = [0] * 10
    for s in scores:
        idx = min(9, max(0, int(s * 10)))
        buckets[idx] += 1
    if not any(buckets):
        return "  （没有数据）"
    peak = max(buckets) or 1
    lines = []
    for i, count in enumerate(buckets):
        lo = i / 10
        bar = "█" * max(0, round(count / peak * 34))
        lines.append(f"    {lo:.1f}-{lo + 0.1:.1f}  {count:>4}  {bar}")
    return "\n".join(lines)


# ───────────────────────────────────────────────────────── 四种模式

def mode_inspect(db: Path) -> int:
    conn = open_db(db)
    rows = fetch_rows(conn)
    conn.close()
    if not rows:
        print("  库里没有记忆")
        return 0

    scored = [score_text(text) for _, text, _, _ in rows]
    print(f"  库：{db}")
    print(f"  条目：{len(rows)}  当前 importance 分布：")
    print(histogram([imp for _, _, _, imp in rows]))
    print("\n  按**新规则**打分后：")
    print(histogram([d.score for d in scored]))

    print("\n  ── 分数最高 10 条（该被优先记住的）──")
    for detail in sorted(scored, key=lambda d: -d.score)[:10]:
        print(f"    {detail.score:.2f}  {detail.text[:66]}")
    print("\n  ── 分数最低 10 条（多半是转述/一次性事件）──")
    for detail in sorted(scored, key=lambda d: d.score)[:10]:
        print(f"    {detail.score:.2f}  {detail.text[:66]}")
    return 0


def mode_text(texts: list[str]) -> int:
    print(f"  给 {len(texts)} 条文本打分：\n")
    for text in texts:
        detail = score_text(text)
        print(f"    {detail.explain()}")
        print(f"        {detail.text[:76]}")
        print()
    return 0


def mode_apply(db: Path, dry_run: bool) -> int:
    conn = open_db_rw(db) if not dry_run else open_db(db)
    rows = fetch_rows(conn)
    if not rows:
        print("  库里没有记忆")
        return 0

    changes: list[tuple[int, float, float, str]] = []
    for mid, text, _level, old in rows:
        new = score_text(text).score
        # 只在差异足够大时才改 —— 避免把 0.5 抖成 0.48 这种无意义的写
        if abs(new - old) >= 0.05:
            changes.append((mid, old, new, text))

    print(f"  库：{db}")
    print(f"  条目 {len(rows)}，需要改 {len(changes)} 条"
          f"{'（dry-run，不写库）' if dry_run else ''}\n")
    for mid, old, new, text in changes[:20]:
        arrow = "↑" if new > old else "↓"
        print(f"    #{mid:<5} {old:.2f} {arrow} {new:.2f}   {text[:62]}")
    if len(changes) > 20:
        print(f"    …还有 {len(changes) - 20} 条")

    if dry_run:
        print("\n（dry-run：没有写入。去掉 --dry-run 才真写）")
        conn.close()
        return 0

    for mid, _old, new, _text in changes:
        conn.execute("UPDATE memory SET importance = ?, updated_at = ? WHERE id = ?",
                     (new, _now_iso(), mid))
    conn.commit()
    conn.close()
    print(f"\n  ✅ 已写入 {len(changes)} 条")
    return 0


def _now_iso() -> str:
    import datetime

    return datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def mode_compare(db: Path) -> int:
    """对比两套权重方案 —— 这就是"评估结构"的用处。"""
    conn = open_db(db)
    rows = fetch_rows(conn)
    conn.close()
    if not rows:
        print("  库里没有记忆")
        return 0

    schemes = {
        "默认（转述 -0.30）": {},
        "强惩罚转述（-0.45）": {"transcript": 0.45},
        "弱惩罚转述（-0.15）": {"transcript": 0.15},
        "重实体（+0.40）": {"entity": 0.40},
    }
    print(f"  库：{db}（{len(rows)} 条）\n")
    print(f"  {'方案':<22}{'均值':>7}{'高分(>0.7)':>11}{'低分(<0.3)':>11}")
    for name, weights in schemes.items():
        scores = [score_text(text, weights).score for _, text, _, _ in rows]
        mean = sum(scores) / len(scores)
        high = sum(1 for s in scores if s > 0.7)
        low = sum(1 for s in scores if s < 0.3)
        print(f"  {name:<22}{mean:>7.3f}{high:>11}{low:>11}")

    print("\n  ⚠️ 怎么读这张表：")
    print("     · 「高分」太少 -> 兜底排序没得挑（现在就是这个问题，多数条目挤在 0.5）")
    print("     · 「低分」太多 -> 惩罚过重，可能把真事实也压下去了")
    print("     · 均值接近 0.5 且两头都少 -> 规则**没有区分力**，需要加信号")
    return 0


def mode_archive_low(db: Path, dry_run: bool, limit: int | None = None) -> int:
    """把"纯聊天记录"归档（`level='archived'`）—— **不删**，界面里能恢复。

    ## 为什么不是"低分就归档"

    实测踩到：0.30 那档混着「用户认为明河是男性，祀是女性」（用户设定的角色，
    不该删）和「宠物认为这段文字讲的是落不完的想念」（该删）。
    **分数不能区分这两类**，所以改按 `classify_transcript` 的类型判。

    顺序按分数升序（先看最像废话的），`--limit` 可只处理前 N 条。

    ## ⚠️ 只动 `recent`

    `fixed`（用户手选的固定注入）和已经 `archived` 的不碰 —— 前者是用户设定，
    后者已经归档过了。
    """
    conn = open_db(db) if dry_run else open_db_rw(db)
    rows = [
        (int(mid), str(text or ""), float(imp or 0.0))
        for mid, text, imp in conn.execute(
            "SELECT id, summary, importance FROM memory"
            " WHERE level = 'recent' AND merged_into IS NULL"
            " ORDER BY importance ASC, ts ASC"
        )
    ]
    drops = []
    for mid, text, imp in rows:
        verdict, why = classify_transcript(text)
        if verdict == "drop":
            drops.append((mid, text, imp, why))
    if limit:
        drops = drops[:limit]

    print(f"  库：{db}")
    print(f"  recent {len(rows)} 条 -> 判定可归档 {len(drops)} 条"
          f"{'（dry-run，不写库）' if dry_run else ''}\n")
    for mid, text, imp, why in drops:
        print(f"    #{mid:<5} [{imp:.2f}] {text[:62]}")
        print(f"            理由：{why}")

    if dry_run:
        print("\n  （dry-run：没有写入。去掉 --dry-run 才真归档）")
        conn.close()
        return 0

    for mid, _text, _imp, _why in drops:
        conn.execute(
            "UPDATE memory SET level = 'archived', updated_at = ? WHERE id = ?",
            (_now_iso(), mid),
        )
    conn.commit()
    conn.close()
    print(f"\n  ✅ 已归档 {len(drops)} 条（**没有删除**，记忆管理里勾「显示已归档」能看见并恢复）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="记忆重要性打分器（独立工具，不花 API 的钱）")
    ap.add_argument("--db", type=Path, default=None, help="记忆库路径（默认自动找）")
    ap.add_argument("--text", action="append", default=[], help="给一条文本打分（可重复）")
    ap.add_argument("--apply", action="store_true", help="把分数写回库")
    ap.add_argument("--dry-run", action="store_true", help="配合 --apply：只看会改什么")
    ap.add_argument("--compare", action="store_true", help="对比几套权重方案")
    ap.add_argument(
        "--archive-low",
        action="store_true",
        help="把「纯聊天记录」归档（不删，可恢复）；配合 --dry-run 先看",
    )
    ap.add_argument("--limit", type=int, default=None, help="配合 --archive-low：只处理前 N 条")
    ap.add_argument("--level", default=None, help="只看某一层（fixed/recent/archived）")
    args = ap.parse_args()

    if args.text:
        return mode_text(args.text)

    db = args.db or default_db()
    if args.compare:
        return mode_compare(db)
    if args.archive_low:
        return mode_archive_low(db, args.dry_run, args.limit)
    if args.apply:
        return mode_apply(db, args.dry_run)
    return mode_inspect(db)


if __name__ == "__main__":
    raise SystemExit(main())
