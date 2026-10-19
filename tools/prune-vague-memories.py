r"""安全删除记忆条目（2026-10-17）。

## 🔴 为什么不能直接 `DELETE FROM memory`

`memory_vec` 是 **`vec0` 虚表**（sqlite-vec 的向量索引），它和 `memory` 的关系是：

```sql
CREATE TABLE memory_vec (
    memory_id INTEGER PRIMARY KEY REFERENCES memory(id) ON DELETE CASCADE,
    dim       INTEGER NOT NULL,
    vector    BLOB    NOT NULL
)
```

于是直接删 `memory` 会踩两个坑：

```
① `PRAGMA foreign_keys` **默认是 OFF** —— CASCADE 不生效，
   `memory` 删了、`memory_vec` 留下孤儿向量（条数对不上，检索会返回幽灵）

② `vec0` 虚表不接受 `DELETE ... WHERE memory_id = ?` 这种写法 ——
   它要按**主键**删，而且删的时候会重建索引，于是触发分词器：
       sqlite3.OperationalError: no such tokenizer: simple
   因为 FTS/vec 索引用了 `simple.dll`（中文分词），
   而这个脚本**没有加载那个扩展**
```

## ✅ 正确做法

```python
conn.execute("PRAGMA foreign_keys = ON")   # 让 CASCADE 真的生效
conn.execute("DELETE FROM memory WHERE id = ?", (mid,))
# memory_vec 由 CASCADE 按主键删 —— 不需要（也不能）自己写 DELETE
```

⚠️ 但仍要先加载 `simple.dll`：`vec0` 删除时若涉及 FTS 索引重建会要它。
最稳的是**照搬 `pet/store.py` 的加载方式**（`load_simple_extension`）。

## 用法

```bash
python tools/prune-vague-memories.py --dry-run     # 只看
python tools/prune-vague-memories.py               # 删（先备份）
python tools/prune-vague-memories.py --probe       # 在副本上演一遍，验证删法可行
```
"""

from __future__ import annotations

import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(r"F:\ai项目")
DB = ROOT / "apps" / "desktop" / "pet-data" / "memory" / "pet.db"
VENDOR = ROOT / "apps" / "desktop" / "pet-data" / "vendor"
BACKUP_DIR = ROOT / "apps" / "desktop" / "pet-data" / "backups"
SIMPLE_DLL = VENDOR / "simple" / "libsimple-windows-x64" / "simple.dll"

# ── 要删的 id（逐条判过）──
DELETE: dict[int, str] = {
    # ① 没有专有名词的泛称
    52: "可能涉及游戏剧情讨论 —— 哪个游戏？",
    54: "对游戏机关机制感兴趣 —— 哪个游戏？§79 原文点的反例",
    57: "可能在讨论沟通技巧 —— 泛称 + 可能",
    70: "玩Steam游戏 —— 平台不是游戏",
    71: "玩横版跑图游戏 —— 哪个？",
    73: "计划过年才玩某款游戏 —— 某款=没说",
    76: "玩终末地 —— 与 #86/#99 重复",
    89: "用户在玩游戏 —— 用户点名的反例",
    101: "关注国际新闻 —— 用户点名的反例",
    103: "关注新游戏发售信息 —— 泛称",
    108: "关注二游 —— 用户点名的反例",
    111: "关注二游测试动态 —— 与 #98 重复且泛",
    115: "常看B站视频 —— 与 #120 重复",
    120: "常刷B站 —— 与 #115 重复",
    125: "刷短视频 —— 泛称",
    127: "最近在连续玩恐怖游戏 —— 哪个？",
    # ② 采访式揣测 / 对话风格观察
    46: "偏好与宠物频繁互动 —— 揣测",
    48: "偏好文字而非语音 —— 与 #41 矛盾",
    55: "使用口语化表达方式 —— 说话方式不是记忆点",
    56: "倾向于模糊表述 —— 评价用户且负面",
    58: "偏好简体中文字幕 —— 太细碎",
    59: "可能隔十几小时才来 —— 可能 + 揣测",
    83: "喜欢思考人生意义 —— 与 #129 重复且泛",
    112: "反复询问相同的二游测试问题 —— 评价用户",
    # ③ 提炼过程的自我描述（垃圾）
    47: "用户没有提及… —— 模型在解释自己没抽到",
    # ④ 重复 / 矛盾
    64: "关注《银松镇》 —— 与 #60/#110/#114 重复",
    69: "已购买 Silver Pines —— 与 #60 同一件事",
    79: "喜欢阅读幻梦猎人的作品 —— 与 #78 重复",
    81: "在本机运行本地服务 —— 与 #80 重复",
    86: "关注终末地小祀主C玩法 —— 与 #74/#97/#99 重复",
    98: "关注二次元游戏测试动态 —— 与 #111 重复，都是泛称",
    102: "倾向按地区定向获取新闻 —— 与 #101 同句，重复且泛",
    107: "关注 GTA6 发售信息 —— 与 #104 重复",
    113: "有银松镇但可能还没开玩 —— 与 #60/#110 矛盾",
    114: "对银松镇相关作品感兴趣 —— 与 #60/#110 重复",
    116: "关注一个up的视频 —— 没说清是哪个",
    118: "关注抽象生活先生的作品 —— 与 #119 重复",
    122: "关注特斯拉的新闻 —— 与 #123 重复且泛",
    124: "经济状况不宽裕 —— 敏感 + 揣测",
    130: "常用美团和饿了么 —— 两个都提等于没说偏好",
    132: "使用 DeepSeek —— 不是生活记忆",
}


def open_db(path: Path) -> sqlite3.Connection:
    """按 `pet/store.py` 的方式打开：加载 simple.dll + 打开外键。"""
    conn = sqlite3.connect(path)
    if SIMPLE_DLL.exists():
        try:
            conn.enable_load_extension(True)
            conn.load_extension(str(SIMPLE_DLL))
        except sqlite3.Error as exc:
            print(f"  ⚠️ 加载 simple.dll 失败（{exc}）—— vec0 删除可能失败")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def do_delete(conn: sqlite3.Connection, ids: list[int]) -> tuple[int, int]:
    """删一批 id，返回 (删掉的 memory 行数, 删掉的 vec 行数)。"""
    before_mem = conn.execute("SELECT COUNT(*) FROM memory").fetchone()[0]
    before_vec = conn.execute("SELECT COUNT(*) FROM memory_vec").fetchone()[0]
    conn.execute("BEGIN")
    try:
        for mid in ids:
            # ⚠️ 只删 memory —— memory_vec 走 ON DELETE CASCADE（按主键删）
            conn.execute("DELETE FROM memory WHERE id = ?", (mid,))
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        raise
    after_mem = conn.execute("SELECT COUNT(*) FROM memory").fetchone()[0]
    after_vec = conn.execute("SELECT COUNT(*) FROM memory_vec").fetchone()[0]
    return (before_mem - after_mem, before_vec - after_vec)


def probe() -> int:
    """在**副本**上验证删法可行（不碰真库）。"""
    print("  ═══ 探针：在临时副本上验证删法 ═══")
    with tempfile.TemporaryDirectory() as td:
        copy = Path(td) / "probe.db"
        shutil.copy2(DB, copy)
        conn = open_db(copy)
        try:
            ids = sorted(DELETE)
            mem_gone, vec_gone = do_delete(conn, ids)
            print(f"    删除 {len(ids)} 条 -> memory 少 {mem_gone}，memory_vec 少 {vec_gone}")
            if mem_gone == len(ids) and vec_gone == len(ids):
                print("    ✅ 删法可行：memory 和 memory_vec 同步减少（CASCADE 生效）")
                ok = True
            else:
                print("    ❌ 两边减少量不一致 —— CASCADE 没生效")
                ok = False
            # 孤儿检查
            orphans = conn.execute(
                "SELECT COUNT(*) FROM memory_vec WHERE memory_id NOT IN (SELECT id FROM memory)"
            ).fetchone()[0]
            print(f"    孤儿向量：{orphans}")
            if orphans:
                ok = False
            return 0 if ok else 1
        finally:
            conn.close()


def main() -> int:
    args = set(sys.argv[1:])
    if "--probe" in args:
        return probe()

    if not DB.exists():
        print(f"  ❌ 数据库不存在：{DB}")
        return 1

    conn = open_db(DB)
    try:
        rows = {int(r[0]): str(r[1]) for r in conn.execute("SELECT id, summary FROM memory")}
        total_before = len(rows)

        missing = sorted(set(DELETE) - set(rows))
        if missing:
            print(f"  ⚠️ 库里没有这些 id（可能已删过）：{missing}")

        targets = sorted(set(DELETE) & set(rows))
        survivors = {i: s for i, s in rows.items() if i not in DELETE}

        print(f"  库里现有 {total_before} 条 -> 将删除 {len(targets)} 条，留 {len(survivors)} 条")
        print()
        print("  ── 删除清单 ──")
        for mid in targets:
            print(f"    #{mid:<4} {rows[mid]}")
            print(f"          ↳ {DELETE[mid]}")
        print()
        print("  ── 保留清单 ──")
        for mid, summary in sorted(survivors.items()):
            print(f"    #{mid:<4} {summary}")

        if "--dry-run" in args:
            print()
            print("  （--dry-run：没有真的删）")
            return 0

        # ── 备份 ──
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = BACKUP_DIR / f"pet-db-before-prune-{stamp}.db"
        conn.close()
        shutil.copy2(DB, backup)
        print()
        print(f"  ✅ 已备份 -> {backup.relative_to(ROOT)}  ({backup.stat().st_size // 1024} KB)")

        # ── 删除 ──
        conn = open_db(DB)
        try:
            mem_gone, vec_gone = do_delete(conn, targets)
            print(f"  ✅ 已删 {mem_gone} 条记忆（memory_vec 同步少 {vec_gone} 条）")
        finally:
            conn.close()

        # ── 验证 ──
        conn = open_db(DB)
        try:
            now = {int(r[0]) for r in conn.execute("SELECT id FROM memory")}
            now_vec = {int(r[0]) for r in conn.execute("SELECT memory_id FROM memory_vec")}
            orphans = now_vec - now
            leftover = sorted(set(DELETE) & now)
            lost = sorted(set(survivors) - now)
        finally:
            conn.close()

        print()
        bad = False
        if leftover:
            print(f"  ❌ 还有没删掉的：{leftover}")
            bad = True
        if lost:
            print(f"  ❌ 误删了该留的：{lost}")
            bad = True
        if orphans:
            print(f"  ❌ 孤儿向量：{sorted(orphans)[:10]}")
            bad = True
        if not bad:
            print(f"  ✅ 校验通过：删 {len(targets)} 留 {len(survivors)}，无孤儿向量")
        return 1 if bad else 0
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass


raise SystemExit(main())
