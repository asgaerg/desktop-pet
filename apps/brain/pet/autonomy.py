"""
自主行为调度器（P5）—— "什么时候自己开口"。

这是"活泼度可调"真正落地的地方。`activity` 只描述说话的风格，本模块决定
**是否开口、何时开口**。设计要点：

1. **决策完全确定，不含随机数**。
   开口意愿是一个加权分（空闲 / 观察新鲜度 / 亲近度 − 冷却惩罚）。刻意不引入
   随机数：用户拖活泼度滑杆时，行为变化必须是可预期的。想调"偶尔安静一下"的
   感觉，调的是阈值和权重曲线，不是掷骰子。

2. **抑制优先于意愿**。免打扰、静音时段、全屏专注、缺少观察 —— 命中任一条件
   意愿归零，直接返回"不说"。安全判断永远压在主动性之上。

3. **冷却比阈值更重要**。宠物最讨人厌的失败模式不是"不说话"，而是"说个不停"。
   交互后、开口后都有强制冷却，且冷却**不可被活泼度覆盖**。

4. **时钟与"当前时刻"分开注入**。一个用来算时长（单调时钟），一个用来判断
   免打扰时段（墙钟）。否则自检只能靠"等 15 分钟看它说不说"，那等于没有测试。

5. **`interrupt_prob` 不属于这里**。那个参数是"用户正在说话时插嘴"的倾向，
   属于 P3 语音阶段（需要音频流状态才能判断）。自主发言用它做闸门是语义错位，
   所以本模块不读它 —— 别为了"用上某个参数"而硬塞。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, replace, field
from datetime import datetime, timedelta
from typing import Any, Callable, Optional

from .bus import Bus
from . import tiers
from .config import Config
from .protocol import clamp01

log = logging.getLogger("pet.autonomy")

Clock = Callable[[], float]
WallClock = Callable[[], datetime]

_WEEKDAYS = {
    "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
    "周一": 0, "周二": 1, "周三": 2, "周四": 3, "周五": 4, "周六": 5, "周日": 6, "周天": 6,
}


# --------------------------------------------------------------------------- #
# 免打扰时段
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class QuietWindow:
    """一个免打扰时段。支持跨零点（如 23:30-07:30）与限定星期（如 mon-fri）。"""

    start: int  # 距零点的分钟数
    end: int
    days: Optional[set[int]] = None  # None 表示每天

    def contains(self, moment: datetime) -> bool:
        if self.days is not None and moment.weekday() not in self.days:
            return False
        minutes = moment.hour * 60 + moment.minute
        if self.start <= self.end:
            return self.start <= minutes < self.end
        # 跨零点：区间被午夜切成两段
        return minutes >= self.start or minutes < self.end

    def __str__(self) -> str:
        days = ""
        if self.days is not None:
            names = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
            days = "/".join(names[d] for d in sorted(self.days)) + " "
        return f"{days}{self.start // 60:02d}:{self.start % 60:02d}-{self.end // 60:02d}:{self.end % 60:02d}"


def parse_quiet_hours(entries: Any) -> list[QuietWindow]:
    """
    解析免打扰时段。非法项**跳过并告警**，不抛错 —— 时段写错不该让宠物起不来
    （与 activity 数值不同：那类错误会让行为完全不符预期，必须报错；
    而这里跳过一条时段，用户能立刻从日志和界面上看出"没生效"）。

    支持：
        "23:30-07:30"            每天，跨零点自动识别
        "mon-fri 09:00-18:00"    限定星期（也接受 周一-周五）
        "sat,sun 10:00-12:00"    列举星期
    """
    if not entries:
        return []
    if isinstance(entries, str):
        entries = [entries]
    if not isinstance(entries, (list, tuple)):
        log.warning("quiet_hours 类型不支持（%r），已忽略", type(entries).__name__)
        return []

    windows: list[QuietWindow] = []
    for raw in entries:
        text = str(raw).strip()
        if not text:
            continue
        try:
            windows.append(_parse_one_quiet(text))
        except (ValueError, TypeError) as exc:
            log.warning("无法解析免打扰时段 %r：%s（已跳过）", text, exc)
    return windows


def _parse_one_quiet(text: str) -> QuietWindow:
    days: Optional[set[int]] = None
    body = text
    if " " in text:
        day_part, _, rest = text.partition(" ")
        days = _parse_days(day_part)
        body = rest.strip()
    if "-" not in body:
        raise ValueError("缺少 - 分隔的开始与结束时间")
    start_text, _, end_text = body.partition("-")
    return QuietWindow(start=_parse_hhmm(start_text), end=_parse_hhmm(end_text), days=days)


def _parse_hhmm(text: str) -> int:
    cleaned = text.strip()
    if ":" not in cleaned:
        raise ValueError(f"时间格式应为 HH:MM，收到 {cleaned!r}")
    hour_text, _, minute_text = cleaned.partition(":")
    hour, minute = int(hour_text), int(minute_text)
    if not (0 <= hour <= 24) or not (0 <= minute < 60):
        raise ValueError(f"时间越界：{cleaned!r}")
    total = hour * 60 + minute
    return 0 if total >= 24 * 60 else total  # 24:00 视作次日零点


def _parse_days(text: str) -> set[int]:
    cleaned = text.strip().lower()
    if cleaned in ("每天", "daily", "all", "*"):
        return set(range(7))
    if "-" in cleaned:
        head, _, tail = cleaned.partition("-")
        first, last = _weekday(head), _weekday(tail)
        if first <= last:
            return set(range(first, last + 1))
        return set(list(range(first, 7)) + list(range(0, last + 1)))  # 跨周边界
    if "," in cleaned:
        return {_weekday(part) for part in cleaned.split(",") if part.strip()}
    return {_weekday(cleaned)}


def _weekday(text: str) -> int:
    key = text.strip().lower()
    if key in _WEEKDAYS:
        return _WEEKDAYS[key]
    raise ValueError(f"无法识别的星期：{text!r}")


# --------------------------------------------------------------------------- #
# 调度器
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class Decision:
    """一次意愿评估的结果。做成可打印的，便于日志排查与自检断言。"""

    speak: bool
    desire: float
    threshold: float
    reason: str
    signals: dict[str, float] = field(default_factory=dict)

    def __str__(self) -> str:
        parts = " ".join(f"{k}={v:.2f}" for k, v in self.signals.items())
        return (
            f"desire={self.desire:.3f} threshold={self.threshold:.3f} "
            f"speak={self.speak} reason={self.reason} [{parts}]"
        )


class Scheduler:
    """
    带冷却的加权意愿调度器。

        evaluate()          -> 现在该不该说话（纯计算，无副作用，可放心多调）
        maybe_speak()       -> 该说就发 autonomy.speak 事件
        mark_interaction()  -> 用户刚说话 / 宠物刚说完，重置冷却
    """

    def __init__(
        self,
        config: Config,
        bus: Bus,
        clock: Clock = time.monotonic,
        wall_clock: WallClock = datetime.now,
        intimacy_provider: Optional[Callable[[], float]] = None,
    ) -> None:
        self.config = config
        self.bus = bus
        self._clock = clock
        self._wall = wall_clock
        # 亲近度由情绪状态机提供；不传就当作 0（不影响独立测试）
        self._intimacy_provider = intimacy_provider

        self.started_at = clock()
        # 启动时**不算**处于冷却中：否则宠物开机后 45 秒内绝不开口，
        # 而"刚看到屏幕有变化想评论"这种最该立刻反应的情况会被无谓地压掉。
        # 做法是把"上次交互"往前推一个冷却时长，等价于"已经安静够了"。
        self._startup_cooldown = float(config.autonomy.get("cooldown_after_interaction_s", 45.0))
        self.last_interaction_at = clock() - self._startup_cooldown
        self.last_spoke_at = -1e9  # 从未说过，不能因此被当成"冷却中"
        self.muted = False  # 用户手动免打扰
        self.focus_guard_active = False  # 由 P4 的全屏/会议检测置位
        self._quiet = parse_quiet_hours(config.activity.get("quiet_hours"))
        self.last_decision: Optional[Decision] = None
        self.spoken_count = 0
        self.skipped_count = 0
        # 档位计时（2026-10-17）：「自言自语」20 分钟后要自动回落到安静，
        # 所以得记住"什么时候进的这一档"。
        # 自主开口的节奏门（2026-10-18 用户给的数字：2/10/15/20 分钟）
        self.last_autonomy_speak_at = -1e9
        # 自主截图的上次时刻（None 档位不截；权限另判）
        self.last_autosnap_at = -1e9
        self._tier_key = self.config.activity_tier.key
        self._tier_since = clock()

    # ------------------------------------------------------------------ 状态

    def reload(self) -> None:
        """配置热更新后重新解析依赖项（免打扰时段是字符串，改配置必须重解析）。"""
        self._quiet = parse_quiet_hours(self.config.activity.get("quiet_hours"))

    def mark_interaction(self) -> None:
        self.last_interaction_at = self._clock()

    def mark_spoke(self) -> None:
        now = self._clock()
        self.last_interaction_at = now
        self.last_spoke_at = now
        self.spoken_count += 1

    def set_muted(self, muted: bool) -> None:
        if muted != self.muted:
            log.info("免打扰：%s", "开启" if muted else "关闭")
        self.muted = muted

    def set_wall_clock(self, wall_clock: WallClock) -> None:
        """替换"当前时刻"的来源。生产环境不用；测试里用来把一天压缩到毫秒。"""
        self._wall = wall_clock

    def set_intimacy_provider(self, provider: Optional[Callable[[], float]]) -> None:
        """接入亲近度来源（生产环境由情绪状态机提供）。"""
        self._intimacy_provider = provider

    def in_quiet_hours(self, moment: Optional[datetime] = None) -> bool:
        now = moment or self._wall()
        return any(window.contains(now) for window in self._quiet)

    # ------------------------------------------------------------------ 评估

    def suppression_reason(self, include_disabled: bool = True) -> Optional[str]:
        """
        当前是否被抑制，以及原因；None 表示允许开口。

        `include_disabled=False` 用于**手动**触发：`enabled` 关掉的是
        "自动找机会说话"，不该连"用户戳它一下想听它说"也一起禁掉 ——
        那是两个不同的诉求，混在一起会让用户觉得开关坏了。
        免打扰/静音时段/专注保护则对两者一律生效。
        """
        return self._suppression(include_disabled=include_disabled)

    def _suppression(self, include_disabled: bool = True) -> Optional[str]:
        """返回抑制原因；None 表示未被抑制。判断顺序即优先级。"""
        if include_disabled and not self.config.autonomy.get("enabled", True):
            return "autonomy.disabled"
        if self.muted:
            return "muted"
        if self.config.activity.get("focus_guard", True) and self.focus_guard_active:
            return "focus_guard"
        if self.in_quiet_hours():
            return "quiet_hours"
        return None

    def evaluate(self, observation_age_s: Optional[float] = None) -> Decision:
        """
        计算当前开口意愿。

        observation_age_s：距离"值得评论的屏幕变化"过去了多少秒；
                           None 表示没有可用观察（P4 接上之前一直是 None）。
        """
        auto = self.config.autonomy
        threshold = self.config.speak_threshold()

        blocked = self._suppression()
        if blocked:
            return Decision(False, 0.0, threshold, blocked, {"suppressed": 1.0})

        now = self._clock()

        # ── 档位节奏门（2026-10-18 用户给的数字）──
        #
        # 「安静 20 分钟聊一次 / 无聊 15 / 好奇 10 / 自言自语 2」是**最小间隔**。
        # 没有这一道门的话，意愿曲线会按秒级的东西说话 —— 和用户要的节奏
        # 完全是两回事（他给的是"多久一次"，不是"多容易开口"）。
        # utonomy.talk_interval_s 可以显式覆盖（测试/调参用；0 = 关掉这道门）。
        talk_override = self.config.autonomy.get("talk_interval_s")
        tier_interval = (
            float(talk_override)
            if isinstance(talk_override, (int, float))
            else float(self.config.activity_tier.talk_interval_s or 0.0)
        )
        if tier_interval > 0:
            since_talk = now - self.last_autonomy_speak_at
            if since_talk < tier_interval:
                return Decision(
                    False,
                    0.0,
                    threshold,
                    "tier_interval",
                    {
                        "since_autonomy_speak_s": round(since_talk, 1),
                        "tier_interval_s": tier_interval,
                    },
                )

        idle_s = max(0.0, now - self.last_interaction_at)

        # 空闲信号：安静够久才开始爬升，到 idle_full_s 饱和
        # 空闲门（2026-10-17）：**默认由档位决定** —— 安静档 30 分钟才想开口，
        # 自言自语档 2 分钟就憋不住。`autonomy.idle_after_s` 只有在配置里
        # **显式写了数字**时才覆盖（默认 null），避免"档位说安静、
        # 调度器按 120 秒催它"的两份真源。
        tier = self.config.activity_tier
        override = auto.get("idle_after_s")
        idle_after = (
            float(override) if isinstance(override, (int, float)) else float(tier.idle_after_s)
        )
        # 爬升速度同样由档位决定（2026-10-18）：只给 idle_after_s 的话，
        # 四档共用 900 秒饱和 —— 「自言自语」憋 5 分钟意愿还不到阈值，
        # 结果是配了念经却一句话不说。utonomy.idle_full_s 仍是显式覆盖。
        full_override = auto.get("idle_full_s")
        idle_full = (
            float(full_override)
            if isinstance(full_override, (int, float))
            else float(tier.idle_full_s)
        )
        if idle_full <= idle_after:
            idle_full = idle_after + 1.0
        idle_signal = clamp01((idle_s - idle_after) / max(1.0, idle_full - idle_after))

        # 观察信号：有屏幕变化且新鲜时最想评论，新鲜度随时间线性衰减
        observation_signal = 0.0
        if observation_age_s is not None:
            decay = float(auto.get("observation_decay_s", 180.0))
            observation_signal = clamp01(1.0 - max(0.0, observation_age_s) / max(1.0, decay))

        # 冷却惩罚：刚交互过就闭嘴。这是不可被活泼度覆盖的安全阀。
        cooldown = float(auto.get("cooldown_after_interaction_s", 45.0))
        delta = now - self.last_interaction_at
        log.debug(
            "冷却计算：now=%.3f last_interaction=%.3f delta=%.3f cooldown=%.3f",
            now,
            self.last_interaction_at,
            delta,
            cooldown,
        )
        cooldown_penalty = clamp01(1.0 - delta / cooldown) if cooldown > 0 else 0.0
        speak_cooldown = float(auto.get("cooldown_after_speak_s", 60.0))
        if speak_cooldown > 0:
            since_speak = now - self.last_spoke_at
            if since_speak < speak_cooldown:
                cooldown_penalty = max(cooldown_penalty, clamp01(1.0 - since_speak / speak_cooldown))

        activity = self.config.activity
        w_observation = 0.9 * float(activity.get("screen_watch_prob", 0.4))
        # 爱说话的倍数也随档位（chatter_gain）：阈值与意愿必须一起变，
        # 只降阈值会让"容易开口"变成"永远开不了口"。
        w_idle = 0.75 * float(activity.get("idle_chatter_rate", 0.35)) * float(tier.chatter_gain)
        w_intimacy = 0.20
        intimacy = clamp01(self._intimacy_provider(), 0.0) if self._intimacy_provider else 0.0

        raw = w_observation * observation_signal + w_idle * idle_signal + w_intimacy * intimacy
        desire = clamp01(raw * (1.0 - cooldown_penalty))

        signals = {
            "idle_signal": idle_signal,
            "observation": observation_signal,
            "cooldown_penalty": cooldown_penalty,
            "intimacy": intimacy,
            "idle_s": min(idle_s, 99999.0),
        }

        # 硬门：要求必须有观察时不看意愿分（P4 之后的"必须看到东西才评论"模式）
        if auto.get("require_observation", False) and observation_signal <= 0.0:
            return Decision(False, desire, threshold, "need_observation", signals)

        if desire < threshold:
            return Decision(False, desire, threshold, "below_threshold", signals)

        return Decision(True, desire, threshold, "speak", signals)

    # ------------------------------------------------------------------ 执行

    async def maybe_speak(self, observation_age_s: Optional[float] = None) -> bool:
        """
        评估并按反应延迟安排一次自主发言。返回是否已安排（不代表已说完）。

        反应延迟让宠物看起来是"想了一下才开口"，而不是被定时器一推就蹦字。
        延迟结束后要**重新评估**：等待期间用户可能说了话，那时冷却已重置，
        绝不能在用户刚说完之后立刻抢话。
        """
        decision = self.evaluate(observation_age_s)
        self.last_decision = decision
        if not decision.speak:
            self.skipped_count += 1
            log.debug("自主评估：不说 —— %s", decision)
            return False

        latency = float(self.config.activity.get("reaction_latency_ms", 600)) / 1000.0
        log.info("自主评估：开口 —— %s（反应延迟 %.2fs）", decision, latency)
        if latency > 0:
            await asyncio.sleep(latency)

        recheck = self.evaluate(observation_age_s)
        if not recheck.speak:
            log.debug("等待反应期间状态变化，取消本次开口：%s", recheck.reason)
            self.last_decision = recheck
            self.skipped_count += 1
            return False

        self.last_autonomy_speak_at = self._clock()
        await self.bus.publish(
            "autonomy.speak",
            {
                "reason": decision.reason,
                "desire": round(decision.desire, 4),
                "threshold": round(decision.threshold, 4),
                "signals": {k: round(v, 4) for k, v in decision.signals.items()},
            },
        )
        return True

    async def check_tier_decay(self) -> Optional[dict[str, Any]]:
        """
        档位到期检查：`自言自语` 待满 20 分钟就回落到安静。

        ## 用户原话（2026-10-17）

        > 「此状态下注意力掉的很快，20分钟不到就会回到安静状态，
        >   因为情绪发泄完了，开始平静的不想说话了。」

        ## 为什么是"回落"而不是"定时静音"

        静音是**压制**（意愿还在，只是被按住）；回落是**状态本身变了** ——
        情绪发泄完了，是真的不想说了。所以改的是 `activity.overall`，
        菜单上那一项也会跟着跑到「安静」。

        ## ⚠️ 只改内存，不写 `config/persona.yaml`

        活泼度本来就是运行时状态（`settingsStore` 里刻意不存它，见那里注释），
        重启后回到 yaml 的值。这里要是偷偷写文件，就成了"它半夜自己改配置"。

        ## ⚠️ 必须在免打扰**之前**判

        `tick()` 里先做抑制检查再发言 —— 但 20 分钟的计时不该被免打扰冻结：
        情绪发泄是随时间走的，用户静音了不代表它还在自言自语。
        所以回落检查放在抑制检查**之前**。

        返回回落描述（供调用方广播）；没到期返回 None。
        """
        tier = self.config.activity_tier
        now = self._clock()
        if tier.key != self._tier_key:
            self._tier_key = tier.key
            self._tier_since = now
            log.info("活泼度：进入「%s」（%s）", tier.name, tier.label)
            return None
        # 时长可配（默认取档位表里的 1200 秒）：用户在 yaml 里改
        # `activity.monologue_decay_s` 就立刻生效，不用改代码。
        override_s = self.config.activity.get("monologue_decay_s")
        effective = tier
        if isinstance(override_s, (int, float)):
            effective = replace(tier, decay_s=float(override_s))
        if not tiers.decay_due(effective, now - self._tier_since):
            return None

        target = tiers.decay_target(tier)
        if target is None:  # pragma: no cover —— decay_due 已保证有目标
            return None
        # ⚠️ 走 apply_patch：夹取与校验都在那边，别直接改字典。
        self.config.apply_patch({"activity.overall": target.overall})
        self._tier_key = target.key
        self._tier_since = now
        # ⚠️ 日志用**生效的**时长（effective），不是档位表里的默认值。
        # 早先这里写的是 	ier.decay_s，把时长调成 8 秒实测时日志却说
        # 「待满 20 分钟」—— 日志和事实不符比没日志更坏。
        log.info(
            "活泼度：「%s」待满 %.0f 秒（%.0f 分钟），回落到「%s」",
            tier.name,
            effective.decay_s,
            effective.decay_s / 60,
            target.name,
        )
        return {
            "from": tier.key,
            "fromName": tier.name,
            "to": target.key,
            "toName": target.name,
            "overall": target.overall,
            "seconds": round(tier.decay_s, 1),
        }

    async def tick(self, observation_age_s: Optional[float] = None) -> bool:
        """一个调度周期。返回是否安排了发言。"""
        await self.bus.publish(
            "autonomy.tick",
            {"idle_s": round(self._clock() - self.last_interaction_at, 3), "muted": self.muted},
        )
        # 回落优先于抑制检查：免打扰不该冻结 20 分钟的计时（见方法说明）。
        decayed = await self.check_tier_decay()
        if decayed:
            await self.bus.publish("autonomy.tier_decayed", decayed)
            return False
        if self._suppression() is not None:
            return False
        return await self.maybe_speak(observation_age_s)

    async def run_forever(
        self, observation_age_provider: Optional[Callable[[], Optional[float]]] = None
    ) -> None:
        """
        常驻调度循环。周期**每轮重新读取**——用户把 tick_s 从 5 秒改成 1 秒要能
        立刻生效，不用重启。
        """
        log.info(
            "自主调度循环启动：活泼度 %.2f -> 开口阈值 %.2f",
            clamp01(self.config.activity.get("overall"), 0.5),
            self.config.speak_threshold(),
        )
        while True:
            interval = max(0.5, float(self.config.autonomy.get("tick_s", 5.0)))
            try:
                age = observation_age_provider() if observation_age_provider else None
                await self.tick(age)
            except asyncio.CancelledError:
                log.info("自主调度循环已停止")
                raise
            except Exception:  # noqa: BLE001 —— 调度器绝不能因单轮异常而死
                log.exception("自主调度单轮异常（已忽略，继续下一轮）")
            await asyncio.sleep(interval)

    def screenshot_due(self) -> tuple[bool, str]:
        """
        现在该不该**自己**截一帧。

        两道条件都必须满足（这是隐私边界，不能松）：

        ```
        档位允许     好奇 30 分钟 / 自言自语 10 分钟；安静与无聊永不
        用户许可     providers.vision.enabled（设置里的「允许偶尔截图」）
        ```

        额外排除免打扰/静音时段/专注保护 —— 那些连"说话"都不许，
        更不该偷偷看屏幕。
        """
        tier = self.config.activity_tier
        # 显式覆盖优先（utonomy.screenshot_interval_s；null = 跟随档位）
        shot_override = self.config.autonomy.get("screenshot_interval_s")
        interval = (
            float(shot_override)
            if isinstance(shot_override, (int, float))
            else tier.screenshot_interval_s
        )
        if not interval:
            return False, "tier_no_screenshot"
        vision_cfg = self.config.providers.get("vision", {})
        if not bool(vision_cfg.get("enabled", False)):
            return False, "permission_off"
        blocked = self._suppression()
        if blocked:
            return False, blocked
        now = self._clock()
        if now - self.last_autosnap_at < float(interval):
            return False, "interval"
        return True, "due"

    def mark_autosnap(self) -> None:
        """记一次"已经自己截过了"，下一次要等间隔。"""
        self.last_autosnap_at = self._clock()

    def snapshot(self) -> dict[str, Any]:
        decision = self.last_decision
        return {
            "enabled": bool(self.config.autonomy.get("enabled", True)),
            "muted": self.muted,
            "focus_guard_active": self.focus_guard_active,
            "quiet_hours": [str(w) for w in self._quiet],
            "in_quiet_hours": self.in_quiet_hours(),
            "threshold": round(self.config.speak_threshold(), 3),
            "spoken_count": self.spoken_count,
            "skipped_count": self.skipped_count,
            "last_reason": decision.reason if decision else None,
            "last_desire": round(decision.desire, 3) if decision else None,
        }


def next_quiet_end(windows: list[QuietWindow], moment: datetime) -> Optional[datetime]:
    """当前免打扰时段的结束时刻，用于界面显示"几点之后再来找我"。"""
    for window in windows:
        if not window.contains(moment):
            continue
        minutes = moment.hour * 60 + moment.minute
        end = moment.replace(
            hour=window.end // 60, minute=window.end % 60, second=0, microsecond=0
        )
        if window.end <= window.start or window.end <= minutes:
            end += timedelta(days=1)
        return end
    return None
