"""Token 用量 -> 人民币。集中一处，改价只改这里。

## 为什么单独一个模块

价格会变（厂商调价、换厂商），而**用量不会**。所以：

    · 存储层只记 `prompt_tokens` / `completion_tokens`（`knowledge.Revision`）
    · 换算成钱是**展示时**做的事，规则集中在这里

这样历史数据的"成本"会跟着新价格自动重算 —— 而不是把当年的价格
焊死在每一条记录里（那样调价后老数据全是错的）。

## ⚠️ 价格来源

DeepSeek 官方价目表（2026 年，人民币）：

    输入  ¥2 / 百万 token
    输出  ¥8 / 百万 token

界面里「截图要花多少钱」那段也引用了同样的数字 —— 改这里时
那个说明文字要一起改。
"""
from __future__ import annotations

#: 输入（prompt）每百万 token 的价格，人民币
PRICE_IN_PER_M = 2.0
#: 输出（completion）每百万 token 的价格，人民币
PRICE_OUT_PER_M = 8.0


def cost_of(prompt_tokens: int, completion_tokens: int) -> float:
    """算一次调用花了多少钱（人民币）。"""
    p = max(0, int(prompt_tokens or 0))
    c = max(0, int(completion_tokens or 0))
    return p / 1_000_000 * PRICE_IN_PER_M + c / 1_000_000 * PRICE_OUT_PER_M


def fmt_cost(value: float) -> str:
    """把金额格式化成好读的形式。

    小额（不到 1 分）用 4 位小数 —— 归档一次通常 ¥0.001 级别，
    显示成 ¥0.00 会让人以为"免费"。
    """
    v = max(0.0, float(value or 0))
    if v == 0:
        return "¥0"
    if v < 0.01:
        return f"¥{v:.4f}"
    if v < 1:
        return f"¥{v:.3f}"
    return f"¥{v:.2f}"
