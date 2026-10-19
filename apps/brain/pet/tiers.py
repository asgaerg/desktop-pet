"""
活泼度四档 —— **档位是一等公民**（2026-10-17）。

## 为什么要有这个模块

以前活泼度只是一个数字（`activity.overall`），四处各自解释它：

```
speak_threshold()        把 overall 线性映射成开口阈值
activity_for_prompt()    按 0.25 / 0.55 / 0.8 三段切文案
scheduler.evaluate()     读 autonomy.idle_after_s 这个**全局**值
```

于是"活泼度 0.8 到底是什么状态"散落在三处，谁也不知道另外两处怎么理解的。
用户 2026-10-17 给了四档的定义，这里把它变成一个**表**：名字、阈值、
自己玩不玩、能不能自主截图、到期回落到哪 —— 全在一行里能读完。

## 用户的原话（权威定义，改动前先读这段）

> 「四档按我的理解，要做成
> 1、安静：玩伴不怎么说话，也不会自主截图。
> 2、无聊：会自己想自己要干什么（看小说或者和用户聊天打游戏，或者看用户打游戏顺便解说），偶尔发呆。
> 3、好奇：部分注意力在用户身上，比较好奇用户在干什么，偶尔搭话或截图和用户聊天。
> 4、自言自语：玩伴开始神神叨叨的，嘴里念叨着一些它自己也不懂的话……犹如唐僧念经，
>    不过此状态下注意力掉的很快，20分钟不到就会回到安静状态，因为情绪发泄完了，
>    开始平静的不想说话了。」
>
> 补充：「自言自语下，玩伴还是能自助截图聊天的，不过频率和好奇差不多。」

## 两个容易搞错的点

```
① 「无聊」是 0.5 那一档（老文案叫「普通」），不是第五档。
② 「自言自语」的自主截图频率**和「好奇」差不多**，不是更频繁 ——
   §115 里那句"更频繁"已作废。
```

## ⚠️ 档位只是**频率**，不是权限

`Tier.screenshot` 表示"这一档**愿意**自己截图"，但它**不是许可**。
真正的许可是用户在设置里打开的「允许偶尔截图」（`providers.vision.enabled`）。

```
权限（用户开关，默认关）  ──┐
                            ├─> 两个都为真才会自主截图
档位（Tier.screenshot）   ──┘
```

不守这条 = 用户拖着滑杆就能绕过隐私开关。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Tier:
    """一个活泼度档位。字段都是"这一档怎么做"，不含任何全局状态。"""

    key: str
    """内部标识，写进日志和消息里（`quiet` / `bored` / `curious` / `monologue`）。"""

    name: str
    """菜单上显示的名字（用户看到的四个名字之一）。"""

    overall: float
    """对应的 `activity.overall` 数值。菜单点档位时写入的就是它。"""

    idle_after_s: float
    """空闲多久才开始"想说话"。安静档几乎不主动，所以给得很大。"""

    screenshot: bool
    """这一档是否**愿意**自主截图（仍需用户权限 `vision.enabled`）。"""

    idle_full_s: float = 900.0
    """
    空闲信号从 0 爬到满要多少秒（决定"多久憋不住"）。

    ⚠️ 只有 `idle_after_s` 是不够的（2026-10-18 实测踩到）：它只决定**几时开始爬**，
    爬多快由这个决定。当时四档共用配置里的 900 秒，结果「自言自语」等了
    5 分钟意愿才 0.096、阈值 0.22 —— **一句话都念不出来**，配着一档念经却哑着。
    """

    talk_interval_s: float = 0.0
    """
    两次**自主开口**之间至少隔多久（秒，2026-10-18 用户给的数字）。

    安静 20 分钟 / 无聊 15 分钟 / 好奇 10 分钟 / 自言自语 2 分钟。

    ⚠️ 这是**下限**不是"到点必说"：到点之后仍然要过意愿与冷却那几道关。
    反过来做（到点就说）半夜也会准点吵人。
    """

    screenshot_interval_s: Optional[float] = None
    """
    自主截图的最小间隔（秒）；None = **这一档不自主截图**。

    好奇 30 分钟 / 自言自语 10 分钟；安静与无聊不截。

    ⚠️ 间隔只是"愿意"，真正的许可仍是 `providers.vision.enabled`
    （用户设置里的「允许偶尔截图」）。两个条件都满足才会真去抓帧。
    """

    chatter_gain: float = 1.0
    """
    空闲意愿的**放大倍数**（这一档有多爱说话）。

    阈值曲线管"多难开口"，这个管"多想开口"。两者都得随档位变，
    否则会出现"阈值降下来了、意愿却永远不够"。
    """

    style: str = ""
    """注入提示词的行为描述（自主开口时用）。"""

    decay_to: Optional[str] = None
    """到期自动回落到哪个档（`key`）。None = 不会自己变。"""

    decay_s: float = 0.0
    """进入这一档后多久回落（秒）。只有会回落的档才有意义。"""

    @property
    def label(self) -> str:
        """
        日志里用的一行摘要。

        ⚠️ **不写具体分钟数**（2026-10-17 改）：回落时长是可配的
        （ctivity.monologue_decay_s），这里写档位表里的 20 分钟会在
        用户改成 1 分钟时给出**错误信息** —— 实测就踩了：日志说
        「20分钟回落」，实际 1 分钟就落了，而测试脚本还把这条日志
        当成了已经回落的证据（假阳性）。
        具体时长只在真正回落那一刻由 check_tier_decay 打出来。
        """
        bits = [f"{self.name}({self.overall:.1f})"]
        if self.screenshot:
            bits.append("可自主截图")
        if self.talk_interval_s > 0:
            bits.append(f"每 {self.talk_interval_s / 60:.0f} 分钟开口")
        if self.screenshot_interval_s:
            bits.append(f"每 {self.screenshot_interval_s / 60:.0f} 分钟截图")
        if self.decay_to:
            bits.append("会回落")
        return " ".join(bits)


TIERS: tuple[Tier, ...] = (
    Tier(
        key="quiet",
        name="安静",
        overall=0.1,
        # 30 分钟：这一档"不怎么说话"，但不等于永不开口（用户戳它它还是要应）。
        idle_after_s=1800.0,
        talk_interval_s=1200.0,
        screenshot_interval_s=None,
        idle_full_s=5400.0,
        chatter_gain=4.2,
        screenshot=False,
        style=(
            "你现在很安静，基本不主动开口。用户跟你说话你就应着，说完就安静待着，"
            "不要自己找话题。"
        ),
    ),
    Tier(
        key="bored",
        name="无聊",
        overall=0.5,
        idle_after_s=720.0,
        talk_interval_s=900.0,
        screenshot_interval_s=None,
        idle_full_s=1800.0,
        chatter_gain=2.8,
        screenshot=False,
        style=(
            "你有点无聊，会自己想想待会儿想干什么 —— 看小说、等用户一起打游戏、"
            "或者看用户打游戏顺便解说两句都行。想想自己的事，偶尔发个呆。"
            "想开口就说一句，不用硬找话题。"
        ),
    ),
    Tier(
        key="curious",
        name="好奇",
        overall=0.8,
        idle_after_s=300.0,
        talk_interval_s=600.0,
        screenshot_interval_s=1800.0,
        idle_full_s=900.0,
        chatter_gain=1.7,
        screenshot=True,
        style=(
            "你有点好奇用户在干什么，可以偶尔搭一句话问问，"
            "或者看一眼屏幕再就着画面聊两句。注意别打扰太频繁。"
        ),
    ),
    Tier(
        key="monologue",
        name="自言自语",
        overall=1.0,
        # ⚠️ 这两个值是"20 分钟至少 9 次"倒推出来的（2026-10-18 用户反馈"频率不对"）。
        #
        # 关键事实：**每次开口后空闲计时会重置**（说完话算一次"有动静"），
        # 所以下一次开口 = 间隔门(120s) 与 "重新爬到阈值" 两者取晚的那一个。
        # 原来 idle_after=120 + idle_full=180 => 要再爬 120 + 0.84×60 ≈ 175 秒，
        # 实测 20 分钟只有 7 次（间隔 175 秒）。现在 0 + 140 => 约 118 秒，
        # 间隔门 120 秒成为瓶颈 => 20 分钟约 10 次。
        idle_after_s=0.0,
        talk_interval_s=120.0,
        screenshot_interval_s=600.0,
        idle_full_s=140.0,
        chatter_gain=1.0,
        screenshot=True,
        style=(
            "你现在有点神神叨叨：念叨着道经、佛经或圣经里的句子，"
            "也会冒出刚学到的哲学句子，边念边琢磨自己的理解。"
            "也会跟用户搭话，但大部分时间在讲自己读了什么、觉得什么。"
            "像念经一样絮絮叨叨地念下去，不要停下来问用户问题。"
        ),
        # 用户原话：「20分钟不到就会回到安静状态，因为情绪发泄完了，
        # 开始平静的不想说话了。」
        decay_to="quiet",
        decay_s=1200.0,
    ),
)

_BY_KEY = {tier.key: tier for tier in TIERS}


def nearest(overall: object) -> Tier:
    """
    按数值取**最近的档位**。

    为什么要"最近"而不是"等于"：滑杆能停在 0.6、0.73 这种中间值，
    那时四个档一个都不精确相等 —— 若返回 None，调用方到处都要处理空值。
    取最近档的语义是"这个数值最接近哪种状态"，和菜单右侧显示的名字一致。
    """
    try:
        value = float(overall)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        value = 0.5
    value = max(0.0, min(1.0, value))
    return min(TIERS, key=lambda tier: abs(tier.overall - value))


def by_key(key: str) -> Optional[Tier]:
    return _BY_KEY.get(key)


def decay_due(tier: Tier, elapsed_s: float) -> bool:
    """
    `tier` 这一档待了 `elapsed_s` 秒，是不是该回落了。

    纯函数（不读时钟）—— 因为"到点没有"这件事必须能被测试钉住，
    而拿真实时钟测 20 分钟显然不现实。
    """
    if not tier.decay_to or tier.decay_s <= 0:
        return False
    return elapsed_s >= tier.decay_s


def decay_target(tier: Tier) -> Optional[Tier]:
    """回落目标档；不会回落的档返回 None。"""
    if not tier.decay_to:
        return None
    return by_key(tier.decay_to)
