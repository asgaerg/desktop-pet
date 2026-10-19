"""算清楚这个桌宠**实际**每千轮会烧多少 token。

## 为什么先算用量而不是先看报价

"哪个模型便宜"是**单价**问题，但真正决定花多少钱的是**用量**：

    花费 = 单价 × (输入 + 输出) token

而这个项目的用量有很强的特征（系统提示词很长、记忆会注入、回复很短），
所以先把用量量出来，再看单价才有意义 —— 否则容易"为了省单价换了模型，
结果因为上下文策略不同反而更贵"。

## 数据来源

  · `pet-data/memory/pet.db` 里的对话记录（真实的轮次）
  · 系统提示词（`config/persona.yaml` 的 prompts + 人格）
  · 用 `embedding_local.count_tokens()` 数（bge 的 tokenizer，**只能近似** ——
    不同模型的分词器不一样，但量级对得上）

⚠️ 数出来的是**近似值**，用来判断量级，不能当账单。
"""

from __future__ import annotations

import pathlib
import sqlite3
import sys

sys.path.insert(0, "apps/brain")

ROOT = pathlib.Path("F:/ai项目")
DB = ROOT / "apps" / "desktop" / "pet-data" / "memory" / "pet.db"

from pet.embedding_local import count_tokens  # noqa: E402


def tok(s: str) -> int:
    try:
        return count_tokens(s or "")
    except Exception:  # noqa: BLE001
        # 退回粗估：中文 ≈ 1 字 1 token
        return len(s or "")


print("  ═══ 数据库里的真实对话 ═══")
if not DB.is_file():
    print(f"  ❌ 找不到 {DB}")
    raise SystemExit(1)

con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
cur = con.cursor()

tables = [r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'")]
print(f"  表: {tables}")

# 找消息表
msg_table = next((t for t in tables if "message" in t.lower() or "turn" in t.lower()), None)
if not msg_table:
    print("  ⚠️ 找不到消息表，列出每表行数：")
    for t in tables:
        n = cur.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
        print(f"    {t}: {n} 行")
    raise SystemExit(0)

cols = [r[1] for r in cur.execute(f'PRAGMA table_info("{msg_table}")')]
print(f"  用表 `{msg_table}`，列: {cols}")

text_col = next((c for c in ("text", "content", "body") if c in cols), None)
role_col = next((c for c in ("role", "author", "kind") if c in cols), None)
if not text_col:
    print(f"  ⚠️ 找不到文本列（{cols}）")
    raise SystemExit(0)

rows = cur.execute(
    f'SELECT {role_col + ", " if role_col else ""}{text_col} FROM "{msg_table}"'
    f'{f" WHERE {role_col} IN (\"user\",\"assistant\")" if role_col else ""}'
).fetchall()

user_rows = [r for r in rows if not role_col or r[0] == "user"]
asst_rows = [r for r in rows if not role_col or r[0] == "assistant"]
u_tok = sum(tok(r[-1]) for r in user_rows)
a_tok = sum(tok(r[-1]) for r in asst_rows)
n_turns = min(len(user_rows), len(asst_rows))

print()
print(f"  用户消息 {len(user_rows)} 条，合计 {u_tok:,} token")
print(f"  宠物回复 {len(asst_rows)} 条，合计 {a_tok:,} token")
print(f"  约 {n_turns} 轮")
if n_turns:
    print(f"  平均每轮：用户 {u_tok / max(1, len(user_rows)):.0f} token，回复 {a_tok / max(1, len(asst_rows)):.0f} token")

# ── 系统提示词有多大 ───────────────────────────────────────────────────
print()
print("  ═══ 系统提示词（每轮都要重发）═══")
sys.path.insert(0, "apps/brain")
from pet.config import load_config  # noqa: E402

cfg = load_config(ROOT / "config" / "persona.yaml")
prompts = cfg.root.get("prompts", {}) or {}
total = 0
for k, v in prompts.items():
    if isinstance(v, str) and v.strip():
        n = tok(v)
        total += n
        print(f"    {k:<28} {n:>6} token")
persona = cfg.root.get("persona", {}) or {}
for k, v in persona.items():
    if isinstance(v, str) and v.strip():
        n = tok(v)
        total += n
        print(f"    persona.{k:<20} {n:>6} token")
print(f"    {'-' * 36}")
print(f"    合计 {total:,} token（每轮输入都要带）")

con.close()
