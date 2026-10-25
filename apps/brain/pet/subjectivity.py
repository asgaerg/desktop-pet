r"""人格主观层（2026-10-17 用户要求）。

## 这是什么

把「拟人特征」集中到一个地方，而不是散落在 gateway / autonomy / memory 里：

```
SubjectiveState
  ├─ emotion     情绪三维（精力 / 心情 / 亲近度）—— 复用已有 EmotionState，组合进来
  ├─ attention   注意力：**集中在"自己"身上的程度**（0=全在你，1=全在自己）
  ├─ vitality    活泼度镜像（从 activity.overall 同步，供 Autonomy 读）
  └─ （预留）impressions  对你的印象
```

用户原话：

> 「将这三个和印象一起做成单独的人格主观情绪层，后续可能会添加更多拟人特征逻辑」

所以这个模块的**主要价值是"以后往哪加"有答案**：新的拟人特征就是
`SubjectiveState` 上的一个字段 + 一个驱动器 + 一个消费者，不牵动别处。

## 🔴 注意力为什么是「在自己身上」

用户给的漂移区间是 `[-0.05, +0.07]`（每 3 分钟随机取一个）。这个区间用
「在自己身上」当正向时**语义才通顺**：

```
大部分时候随机到负值  -> 注意力离开自己，回到用户身上
偶尔随机到正值        -> 忽然走神想自己的事
均值 +0.01            -> 没人理时整体缓慢偏向自己
```

如果反过来（0=自己、1=用户），均值 +0.01 就成了「没人理时反而越来越关注用户」，
语义是反的。

## 🔴 随机漂移而不是"向基线靠拢"

第一版我设计成 `clamp(基线-当前, -0.05, +0.07)`（向基线收拢）。
用户明确要求**在区间里随机取一个值**。随机游走的行为差别是：

```
向基线收拢  -> 稳定停在基线，行为可预测
随机游走    -> 会有"忽然想起你"或"忽然走神"的意外感
```

⚠️ **随机游走的风险：正偏均值会让它长期贴到 1.0**（全在自己）。
所以留了两个保险，见 `ATTENTION_CEILING` 的说明。

## 不碰 IO

这个模块**只算数**：不读文件、不连数据库、不发事件。
调用方（gateway）负责把事件接进来、把状态读出去。
这样它能被 selftest 用假时钟完整验证。
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from .brains.base import EmotionState
from .protocol import clamp01

# ── 注意力漂移（用户 2026-10-17 定的数值）────────────────────────────────
#
# ⚠️ 每 3 分钟评估一次，**在区间里随机取一个值**（不是固定值）。
ATTENTION_IDLE_INTERVAL_S = 180.0
ATTENTION_IDLE_MIN = -0.05
ATTENTION_IDLE_MAX = +0.07
#
# 用户一句话能把注意力大幅拉到用户身上（负向）。
ATTENTION_ON_USER_MESSAGE = -0.25
#
# 🔴 用户开口后，注意力**一定**降到这个上限以下（硬钳制）。
#
# ## 为什么必须有这条（2026-10-17 模拟抓到的 bug）
#
# 只要"每 3 分钟随机漂移 + 均值 +0.01"，挂机几小时后注意力必然贴到 1.0。
# 而那时 `-0.25` 只能把它拉到 0.75 —— **仍在"想自己"档（>=0.65）**。
#
# 实测就发生过：
#
#     注意力 1.00 -> 说话后 0.75 档位=self   ❌ 用户开口了它还在走神
#     注意力 0.95 -> 说话后 0.70 档位=self   ❌
#     注意力 0.90 -> 说话后 0.65 档位=self   ❌
#
# 单测抓不到：单测只验证"一句话让数值下降"，而数值下降 0.25 是**对的**。
# 错的是"降了也没用" —— 这要跨档位才看得出来。
#
# 所以：不管原来多高，用户说话后一律 `min(现値, 0.50)`。
# 0.50 在 mixed 档（0.35~0.65）偏用户一侧，语义是"被叫了一声，回过神来"。
ATTENTION_AFTER_USER_CAP = 0.50
# 自己说完，回到自己的事上一点。
ATTENTION_ON_REPLY_DONE = +0.05
# 检索命中"自己的兴趣"，进一步加强自我沉浸。
ATTENTION_ON_SELF_HIT = +0.20
# 用户主动给宠物看屏幕（截图对话，2026-10-17）。
#
# 为什么是一个**很低**的值：注意力 0 = 全在用户身上、1 = 全在自己身上
# （见本文件顶部的语义说明）。用户把屏幕推到它面前时，注意力应该
# **几乎全部**转过去 —— 这是"好奇"最直白的表达。
ATTENTION_ON_SCREEN = 0.06
#
# 🔴 天花板：只在**极端**生效，防随机游走贴着 1.0 不下来。
#
# 纯随机游走的均值为 +0.01/3分钟，理论上几小时后会长期吸在 1.0 ——
# 那时注意力永远"全在自己"，用户说话也只能压到 0.75，回不到"关注用户"。
# 所以：超过 0.92 之后，正向漂移**按比例压缩**（越接近 1 越压到 0）。
# 这仍然保留了随机性（负向完全不受影响），只是把"贴顶"变成"顶附近徘徊"。
ATTENTION_CEILING = 0.92

# ── 注意力的三个档位（供提示词与门控用）────────────────────────────────
# 数值是"在**自己**身上的程度"，所以 high = 在想自己的事。
ATTENTION_SELF_THRESHOLD = 0.65     # >= 这个值才算"主要在想自己"
ATTENTION_USER_THRESHOLD = 0.35     # <= 这个值才算"主要在想用户"

# ── 活泼度 -> 注意力基线 ──
#
# 用户要求主观层和活泼度打通。语义：活泼度高 = 更愿意往外看（基线偏低）。
# ⚠️ 现在只用来决定"随机漂移的均值往哪偏"，不直接覆盖注意力值。
VITALITY_BASELINE_LOW = 0.75        # 活泼度 0 时的基线（偏自己）
VITALITY_BASELINE_HIGH = 0.35       # 活泼度 1 时的基线（偏用户）


def attention_band(attention: float) -> str:
    """把连续值分成三档，给提示词用（比直接塞数字好读）。"""
    value = clamp01(attention)
    if value >= ATTENTION_SELF_THRESHOLD:
        return "self"
    if value <= ATTENTION_USER_THRESHOLD:
        return "user"
    return "mixed"


@dataclass
class SubjectiveState:
    """
    人格主观状态：情绪 + 注意力 + 活泼度。

    ## 为什么 emotion 是**组合**而不是搬进来

    `EmotionState` 是 `Brain` 工厂签名的第二个参数
    （`build_brain(config, emotion)`），插件注册表、`mock.py`、
    `openai_compat.py`、selftest 都在按这个签名调用。

    搬走它会变成 breaking change，而主观层只需要**包住**它。
    所以：`self.emotion` 仍是那个 `EmotionState` 实例，外部照旧可用。

    ## 时间用手动推进（`tick(now=...)`），不自己读时钟

    这样测试能用假时钟精确验证"每 3 分钟漂移一次"，
    不用真的等 3 分钟（也不用 monkeypatch `time.time`）。
    """

    emotion: EmotionState = field(default_factory=EmotionState)

    # 注意力：0 = 全在用户身上，1 = 全在自己的事上
    attention: float = 0.45

    # 活泼度镜像（0~1）。由调用方从 `activity.overall` 同步过来。
    vitality: float = 0.6

    # 此刻在想自己的什么（检索命中的兴趣短语，供提示词用）
    focus: list[str] = field(default_factory=list)

    # 印象（预留，2026-10-17 用户要求"先记着"）
    impressions: list[str] = field(default_factory=list)

    # 内部：上次漂移的时刻 + 上次 tick 的时刻
    _last_idle_at: float = field(default_factory=time.time, repr=False)
    _last_tick_at: float = field(default_factory=time.time, repr=False)
    # 注入随机源，测试可以塞一个确定性的（比如定值 0.0 的 lambda）
    _rand: Any = field(default=None, repr=False)

    # 累计统计（诊断用）
    idle_ticks: int = 0
    last_delta: float = 0.0

    # ------------------------------------------------------------------ 随机源

    def _uniform(self, lo: float, hi: float) -> float:
        """在区间里取随机值。测试可替换 `_rand` 来消除随机性。"""
        rng = self._rand
        if callable(rng):
            return float(rng(lo, hi))
        return random.uniform(lo, hi)

    # ------------------------------------------------------------------ 动力学

    def tick(self, now: Optional[float] = None) -> float:
        """
        时间推进。返回本次对注意力的总改动（0 表示没到 3 分钟，没动）。

        ## 两件事同时做

        · **情绪**：交给 `EmotionState.tick()`（它自己按真实时间衰减）
        · **注意力**：每满 `ATTENTION_IDLE_INTERVAL_S` 漂移一次

        ⚠️ 用 `while` 而不是 `if`：如果两次 tick 间隔了 10 分钟，
        应该补上 3 次漂移，而不是只漂一次。否则挂机久了注意力就不动了。
        ⚠️ 但要限上限（`max_catchup`），否则关了几天再开，
        一次补几百次漂移 —— 既没意义又会让随机结果完全不可复现。
        """
        moment = time.time() if now is None else float(now)
        elapsed_tick = moment - self._last_tick_at
        self._last_tick_at = moment
        if elapsed_tick > 0:
            self.emotion.tick()

        total = 0.0
        max_catchup = 8
        guard = 0
        while (
            moment - self._last_idle_at >= ATTENTION_IDLE_INTERVAL_S
            and guard < max_catchup
        ):
            self._last_idle_at += ATTENTION_IDLE_INTERVAL_S
            total += self._drift_once()
            guard += 1
        # 积压太多就把它对齐到"现在"，免得后面永远在补
        if moment - self._last_idle_at > ATTENTION_IDLE_INTERVAL_S * max_catchup:
            self._last_idle_at = moment

        self.last_delta = total
        return total

    def _drift_once(self) -> float:
        """漂移一次。这是整个模块唯一有随机性的地方。"""
        delta = self._uniform(ATTENTION_IDLE_MIN, ATTENTION_IDLE_MAX)

        # 天花板压缩：越接近 ATTENTION_CEILING，正向漂移越弱。
        # 负向不压缩（注意力随时可以被拉回用户身上）。
        if delta > 0 and self.attention > ATTENTION_CEILING:
            headroom = max(0.0, 1.0 - self.attention) / max(1e-6, 1.0 - ATTENTION_CEILING)
            delta *= clamp01(headroom)

        # 活泼度高 -> 整体略偏向用户（把漂移往负方向推一点点）
        # 这只是"倾向"，幅度很小，不会盖掉随机性。
        tilt = (self.vitality - 0.5) * 0.02
        delta -= tilt

        before = self.attention
        self.attention = clamp01(self.attention + delta)
        self.idle_ticks += 1
        return self.attention - before

    def on_user_message(self, text: str = "", attention: Optional[float] = None) -> None:
        """
        用户说话：注意力拉向用户（attention 变小）。

        ⚠️ 先减再**硬钳制**到 `ATTENTION_AFTER_USER_CAP` —— 理由见那个常量的注释：
        挂机久了注意力会贴到 1.0，光减 0.25 只到 0.75，仍在"想自己"档，
        表现为"用户开口了它还在走神"。钳制保证不管原来多高，一律回到关注用户。

        ## `attention` 参数（2026-10-23「被打扰」）

        传进来的是**改动前**的注意力。为什么需要它：心情惩罚的幅度取决于
        "它当时有多专注" —— 心不在焉时被叫一声几乎不烦，完全沉浸时被叫会很烦。

        ⚠️ 调用方必须在**本方法之前**读注意力。如果传 `self.attention` 那也行
        （Python 先求值实参），但**绝不能**让本方法自己去读 —— 那时注意力
        已经被拉向用户了，惩罚永远接近 0（顺序写反的经典 bug）。

        不传时按 0 处理（= 不惩罚），保持向后兼容（旧调用点、测试）。
        """
        before = self.attention if attention is None else float(attention)
        self.attention = min(
            clamp01(self.attention + ATTENTION_ON_USER_MESSAGE),
            ATTENTION_AFTER_USER_CAP,
        )
        self.emotion.on_user_message(text, attention=before)
        # 一旦用户开口，"自己的事"就不再是当前焦点
        self.focus.clear()

    def on_reply_done(self, length: int = 0) -> None:
        """自己说完：回到自己的事上一点（attention 变大）。"""
        self.attention = clamp01(self.attention + ATTENTION_ON_REPLY_DONE)
        self.emotion.on_reply_done(length)

    def on_self_hit(self, topics: Optional[list[str]] = None) -> None:
        """
        检索命中了"自己的兴趣"：进一步沉浸（attention 变大）。

        `topics` 只是给提示词用的短语（如「银松镇」），不参与数值计算。
        """
        self.attention = clamp01(self.attention + ATTENTION_ON_SELF_HIT)
        if topics:
            for t in topics:
                t = str(t).strip()
                if t and t not in self.focus:
                    self.focus.append(t)
            # 只留最近几条，提示词里塞太多没意义
            del self.focus[:-4]

    def set_vitality(self, value: float) -> None:
        """同步活泼度（来自 `activity.overall`）。"""
        self.vitality = clamp01(value)

    # ---------------------------------------------------- 随机事件的影响（2026-10-17）

    def apply_event_effect(
        self,
        *,
        mood: float = 0.0,
        energy: float = 0.0,
        intimacy: float = 0.0,
        attention: float = 0.0,
    ) -> dict[str, float]:
        """
        把一次随机事件的 delta 施加到状态上，返回**实际生效**的改动。

        ## 🔴 为什么返回"实际"而不是"请求"

        `clamp01` 会把越界的部分吃掉。如果日志记的是"亲密 +0.05"，
        而实际只涨了 0.001（因为已经到 1.0），用户看日志就会以为系统没生效。

        所以：**日志必须记录真实变化量**。这也是 `EmotionState` 那条
        "情绪只缓慢变化"的延伸 —— 状态到顶了就该看得出来。

        ## 为什么 attention 用加号

        用户要求"做这些事也会相应的增加玩伴对用户的印象、情绪变化"。
        做自己的事 -> 更沉浸在自己这边（attention 变大）。
        但「唱歌给用户听」是例外，那条事件的 `attention_delta` 是负的 ——
        事件定义里自己决定方向，这里不预设。
        """
        before = {
            "mood": self.emotion.mood,
            "energy": self.emotion.energy,
            "intimacy": self.emotion.intimacy,
            "attention": self.attention,
        }
        # 情绪走原有字段，但**不走** `on_user_message`（那会连带加精力）
        self.emotion.mood = clamp01(self.emotion.mood + mood)
        self.emotion.energy = clamp01(self.emotion.energy + energy)
        self.emotion.intimacy = clamp01(self.emotion.intimacy + intimacy)
        self.attention = clamp01(self.attention + attention)
        return {
            "mood": self.emotion.mood - before["mood"],
            "energy": self.emotion.energy - before["energy"],
            "intimacy": self.emotion.intimacy - before["intimacy"],
            "attention": self.attention - before["attention"],
        }

    def record_impression(self, text: str) -> None:
        """
        在内存里留一条印象（持久化由调用方写库）。

        ⚠️ 只留最近几条：印象进提示词，塞太多会让它复述。
        持久化那份在 `store.impression` 表里，用户可以查、可以删。
        """
        body = str(text or "").strip()
        if not body:
            return
        if body in self.impressions:
            return
        self.impressions.append(body)
        del self.impressions[:-6]

    # ------------------------------------------------------------------ 读

    @property
    def band(self) -> str:
        """注意力三档：'user' / 'mixed' / 'self'。"""
        return attention_band(self.attention)

    def on_user_shows_screen(self) -> None:
        """
        用户**主动**给宠物看屏幕（截图对话）。

        与 `on_user_message()` 的区别：那条是"说了一句话"，这条是"把屏幕
        推到你面前" —— 后者应该把注意力**直接拉到贴脸**，而不是一点点挪过去。
        所以不是减 0.25，而是直接压到 `ATTENTION_ON_SCREEN`。

        顺带做三件事：
          · `focus` 记一句"在看用户的屏幕"（进提示词，让它顺着画面聊）
          · 情绪上精神一振（看图是件让它来劲的事）
          · 亲近度微涨（被主动分享是一件亲近的事）
        """
        self.tick()
        self.attention = min(self.attention, ATTENTION_ON_SCREEN)
        self.focus = ["用户刚把屏幕给你看"] + [f for f in self.focus if "屏幕" not in f][:3]
        self.emotion.energy = clamp01(self.emotion.energy + 0.08)
        self.emotion.mood = clamp01(self.emotion.mood + 0.03)
        self.emotion.intimacy = clamp01(self.emotion.intimacy + 0.02)
        # 🔴 好奇表情**在这里设**，不在调用方设（2026-10-17 踩过）。
        #
        # 第一版是 gateway 在调完这个方法之后再单独调一次
        # emotion.on_curious() —— 结果自检直接抓到表情没变好奇：
        # 两件事分开写，就一定会有人在新增调用点时漏掉其中一件。
        # 这在本项目里已经是一条规律：**两处分别写就是漏的一半。**
        try:
            self.emotion.on_curious(90.0)
        except Exception:  # noqa: BLE001 —— 表情失败不该影响状态更新
            pass

    def may_talk_about_self(self, in_conversation: bool = False) -> bool:
        """
        现在能不能聊**自己的事**（用户 2026-10-17 要求）。

        > 「注意力在我身上的时候，它几乎不会聊自己的事，
        >   只有在用户结束或者不在聊天时，聊自己的事」

        所以是**硬门**，不是概率：

        ```
        在对话中                  -> 不能（哪怕注意力在自己身上）
        注意力主要在自己(>=0.65)   -> 能
        否则                      -> 不能
        ```

        ⚠️ 返回 True 只代表"允许"，不代表"一定会说" ——
        还要过 Autonomy 的意愿阈值。
        """
        if in_conversation:
            return False
        return self.attention >= ATTENTION_SELF_THRESHOLD

    def describe(self) -> str:
        """
        给提示词看的一段自然语言。

        ⚠️ 刻意**不给数字**：模型看到"注意力 0.73"只会瞎编，看到
        "你现在心思在自己这边"才知道怎么说话。与 `EmotionState.describe`
        同一条取舍（那里也明确写了"不要在回复里报数字"）。
        """
        band = self.band
        if band == "self":
            head = "你现在的心思主要在自己这边——在琢磨自己的事、自己的兴趣。"
        elif band == "user":
            head = "你现在的心思主要在对方身上——在关注他、想他刚才说的话。"
        else:
            head = "你现在的心思一半在自己、一半在对方，比较放松。"

        if self.focus:
            head += "你此刻脑子里转的是：" + "、".join(self.focus[:3]) + "。"

        if self.emotion.energy < 0.35:
            head += "你有点累了，不想太费劲。"
        return head

    def as_dict(self) -> dict[str, Any]:
        """给事件 / 设置页 / 日志用的快照（数字在这里，提示词里不放）。"""
        return {
            "attention": round(self.attention, 4),
            "attention_band": self.band,
            "vitality": round(self.vitality, 4),
            "focus": list(self.focus),
            "may_talk_about_self": self.may_talk_about_self(False),
            "emotion": self.emotion.as_dict(),
            "idle_ticks": self.idle_ticks,
        }
