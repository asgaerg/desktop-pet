"""语言表达层（2026-10-22）：把 0~1 的旋钮翻译成给模型读的自然语言。

## 这一层管什么，不管什么

用户对结构的划分（原话）：

> 「感性与理性偏向**性格**方面，还是放入主观层的大模块中，
> 情绪层属于主观层，**语言层不属于主观层**。」

所以分界是：

    主观层（subjectivity.py）  她"怎么认识世界"、"此刻怎么样"
        · 精力 / 心情 / 亲近度（EmotionState）
        · 注意力 / 活泼度 / 关注点（SubjectiveState）
        · 感性 · 理性  <- 性格，不是表达技巧

    语言层（本模块）            她"怎么把话说出来"
        · 修辞倾向 / 文化表达 / 词汇能力 / 温度·关心

⚠️ **感性/理性刻意不在这里。** 它是认知取向，会渗透到判断、注意力分配、
    自主行为；而本模块只管"同一件事怎么说出口"。把它放这里会退化成
    一个语气滑杆，那是对用户意图的误解。

## 核心取舍：**只给语义，不给数字**

与 `EmotionState.describe()`、`SubjectiveState.describe()` 同一条理由：
模型看到「温度 0.7」只会瞎编成「我的温度是 70%」。
所以这里把数值按档位转成自然语言，且**只在非中性档位才说**
（中性档位什么都不加 —— 默认值不应该污染提示词）。

## 为什么每个旋钮都要能关到 0

用户要求表达层是"调味"不是"地基"：任何一项调到 0，系统必须仍然正常说话，
只是少了那点味道。所以这里没有"必须至少启用一项"之类的校验。
"""

from __future__ import annotations

from typing import Any

# 五档：0~0.2 / 0.2~0.4 / 0.4~0.6 / 0.6~0.8 / 0.8~1.0
#
# 为什么用五档而不是三档：三档太粗（低/中/高），用户从 0.3 拉到 0.5
# 看不出区别；五档能让滑杆的每一段都有可感知的变化。
# 为什么不用十档：档位越多，相邻档位的措辞就越难写得不一样，
# 最后会变成一堆"略微/稍微/稍微有点"的废话，模型也读不出差别。
_LEVELS = 5


def _level(value: Any) -> int:
    """把 0~1 的旋钮映射到 0~4 的档位。非法值按中性（2）处理。

    ⚠️ 非法值**不抛异常**：这是用户可编辑的配置，一个手滑写成 `abc`
    不该让宠物一句话都说不出来（与 `_render_prompt` 兜底同一条道理）。
    """
    try:
        v = float(value)
    except (TypeError, ValueError):
        return _LEVELS // 2
    if v != v:  # NaN
        return _LEVELS // 2
    v = max(0.0, min(1.0, v))
    # 1.0 要落在最后一档，所以用 _LEVELS-1 做乘数
    return min(_LEVELS - 1, int(v * (_LEVELS - 1) + 0.5))


# ─────────────────────────────────────────────────────────────────────────────
# 各旋钮的措辞表（索引 = 档位 0~4）
#
# 写这些句子的原则：
#   1. 描述**行为**，不描述**参数**（"少打比方"而不是"修辞 0.2"）；
#   2. 给**例子**，模型对例子的服从度远高于形容词；
#   3. 中性档（索引 2）留空，不往提示词里塞废话。
# ─────────────────────────────────────────────────────────────────────────────

_RHETORIC = [
    "几乎不用比喻和拟人，有话直说。",
    "少打比方，能用直白的话说清楚就别绕。",
    "",  # 中性：不加任何约束
    "偶尔可以打个比方，让抽象的东西具体一点。",
    "喜欢打比方，常用拟人、通感 —— 比如「这代码像一团打结的毛线」。",
]

_CULTURE = [
    "不用古诗、成语、谚语，说大白话。",
    "少用成语和典故，别让人读着累。",
    "",
    "合适的时候可以用成语或俗语，一两句点到即止。",
    "常用成语、谚语、古诗来承载意思 —— 比如把「差不多就行」说成「过犹不及」。",
]

_VOCABULARY = [
    "只用最普通的词，怎么顺口怎么说。",
    "用词平实，不必挑字眼。",
    "",
    "用词可以讲究一点，挑更准的那个词。",
    "词汇尽量丰富准确，能用一个精确的词就别说三个含糊的词。",
]

_WARMTH = [
    "说话冷淡、就事论事，不安慰也不主动关心对方的处境。",
    "偏克制，把话说完就行，不必额外体贴。",
    "",
    "说话带点温度，偶尔顺口关心一下对方。",
    "话里明显有温度，会主动留意对方的处境 —— 比如「你昨晚没睡好吧」。",
]


def describe(expression: dict[str, Any] | None) -> str:
    """把语言层配置拼成一段给模型读的说明。

    中性档位全部为空时返回空串（调用方 `if text:` 判断即可），
    这样**默认配置不会给提示词增加任何内容** —— 与"关掉就等于没有"一致。
    """
    if not expression or not isinstance(expression, dict):
        return ""

    parts: list[str] = []
    for key, table in (
        ("rhetoric", _RHETORIC),
        ("culture", _CULTURE),
        ("vocabulary", _VOCABULARY),
        ("warmth", _WARMTH),
    ):
        if key not in expression:
            continue
        text = table[_level(expression.get(key))]
        if text:
            parts.append(text)

    if not parts:
        return ""

    # 开头一句总纲：告诉模型这是"说话习惯"，不是"要扮演的角色"。
    # 没有这句，模型容易把它读成"人设要求"而演得过火。
    return "你的说话习惯（不是要扮演的角色）：" + "".join(parts)


def as_dict(expression: dict[str, Any] | None) -> dict[str, float]:
    """规范化后的数值快照（给设置页回显与诊断用）。"""
    out: dict[str, float] = {}
    if not isinstance(expression, dict):
        return out
    for key in ("rhetoric", "culture", "vocabulary", "warmth"):
        if key in expression:
            try:
                v = float(expression[key])
            except (TypeError, ValueError):
                v = 0.5
            if v != v:
                v = 0.5
            out[key] = round(max(0.0, min(1.0, v)), 3)
    return out
