"""
屏幕感知（P4）—— 决定"宠物此刻看到了什么、值不值得评论"，以及"什么绝对不能看"。

这个模块的设计核心是把**采集**与**分析**彻底分开：

    采集（ScreenCapturer 实现，依赖平台，可能在受限环境里不可用）
        ↓ 交出原始帧
    分析（本模块的纯函数与状态机，零平台依赖、可完全离线测试）
        ↓ 交出两个通道
    · 信号通道：一个 0~1 的新鲜度分，喂给自主调度器决定"要不要开口"
    · 内容通道：一段文本（窗口标题 / OCR / VLM 描述），喂给大脑决定"说什么"

**为什么值得这样拆**：真正省钱、也真正决定体验的是分析这一半 ——
"屏幕没变就整轮跳过"能省掉 ~90% 的模型调用（见方案 §11.1）。而它恰好是
不需要屏幕也能测的部分。于是 P4 的价值可以在没有 GUI 的环境里被完整验证，
采集实现则退化成一层薄的、可替换的适配器。

隐私是硬约束，不是可选项：`PrivacyGuard` 在任何分析之前先判断
"这一帧能不能看"。命中黑名单时帧会被直接丢弃，连哈希都不算。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Protocol

from .bus import Bus

log = logging.getLogger("pet.vision")

# 8x8 灰度网格：64 位足够区分"换了个窗口"和"同一页面细微滚动"，
# 又足够抗噪（抗掉光标闪烁、时钟秒数这类无关变化）。
_GRID = 8
_DEFAULT_HAMMING_THRESHOLD = 5


# --------------------------------------------------------------------------- #
# 帧
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class Frame:
    """
    一帧画面。

    `grid` 是永达的：灰度网格用于变化检测，体积只有 64 个整数。
    `image` 是可选的：仅在内容层需要看图时才编码保留（PNG 字节），
    否则整张截图白占几十 MB 内存，而它可能根本没有下游用途。
    """

    grid: list[int]  # 长度 64，取值 0~255 的灰度均值
    width: int
    height: int
    captured_at: float = field(default_factory=time.monotonic)
    titles: tuple[str, ...] = ()
    # 编码后的图像字节（PNG）。只有在 vision 配置里启用了需要看图的抽取器时才有值。
    image: Optional[bytes] = None

    @property
    def signature(self) -> int:
        """把灰度网格压成 64 位指纹（感知哈希）。"""
        if not self.grid:
            return 0
        mean = sum(self.grid) / len(self.grid)
        bits = 0
        for index, value in enumerate(self.grid[:_GRID * _GRID]):
            if value >= mean:
                bits |= 1 << index
        return bits

    @property
    def text_hint(self) -> str:
        """窗口标题拼成的文本线索；没有就返回空串。"""
        return " | ".join(t for t in self.titles if t.strip())


@dataclass(slots=True)
class PrivacyVerdict:
    """能否查看这一帧，以及原因。reason 会进日志，便于用户核对规则是否生效。"""

    allowed: bool
    reason: str = ""

    def __bool__(self) -> bool:
        return self.allowed


@dataclass(slots=True)
class Observation:
    """
    一次"看到了值得说的东西"。这是交给大脑与调度器的最终产物。

    novelty 与 content 是**两条独立通道**，不要混：
      · novelty 决定要不要开口（调度器读它算意愿分）；
      · content 决定说什么（大脑读它组织语言）。
    把两者混成一个"重要程度"是常见的设计错误：一个窗口标题变化很"新鲜"
    但可能毫无内容，而一段静止的代码可能很值得评论。
    """

    novelty: float
    content: str
    titles: tuple[str, ...] = ()
    changed_ratio: float = 0.0
    captured_at: float = field(default_factory=time.monotonic)
    source: str = "grid"

    def age_s(self, now: Optional[float] = None) -> float:
        return max(0.0, (now or time.monotonic()) - self.captured_at)


# --------------------------------------------------------------------------- #
# 采集器接口
# --------------------------------------------------------------------------- #


class ScreenCapturer(Protocol):
    """
    采集一帧。实现者可以是 GDI 截屏、Electron 推流、录像回放，或测试用的假采集器。

    约定：**不得抛异常**表达"看不到"（黑名单、无权限、屏幕锁定都是正常情况），
    返回 None 即可。异常只用于真正的程序错误。
    """

    def grab(self) -> Optional[Frame]:  # pragma: no cover - 协议
        ...


class NullCapturer:
    """什么都不看的采集器。P4 之前的默认实现，也是"用户关掉看屏幕"的实现。"""

    name = "none"

    def grab(self) -> Optional[Frame]:
        return None


class ReplayCapturer:
    """
    回放一串预置帧。用于测试与演示：不需要真实屏幕就能跑完整条感知链路。
    走完一轮后返回 None（模拟"屏幕长时间没变化"）。
    """

    name = "replay"

    def __init__(self, frames: list[Frame]) -> None:
        self._frames = list(frames)
        self._index = 0

    def grab(self) -> Optional[Frame]:
        if self._index >= len(self._frames):
            return None
        frame = self._frames[self._index]
        self._index += 1
        return frame


# --------------------------------------------------------------------------- #
# 隐私闸门
# --------------------------------------------------------------------------- #


@dataclass
class PrivacyRules:
    """
    截屏黑名单。

    两个维度：
      · `title_patterns`：窗口标题命中即跳过（密码管理器、银行、聊天工具…）；
      · `process_patterns`：前台进程名命中即跳过（可选，需要平台支持）。

    匹配是**大小写不敏感的子串**而不是正则：用户写配置时不该被迫学正则，
    而且子串匹配的误报方向更安全（宁可多跳过，也不要漏看）。
    """

    title_patterns: tuple[str, ...] = ()
    process_patterns: tuple[str, ...] = ()
    enabled: bool = True
    # 命中时的处理：目前只有"跳过"。留字段是为了将来能支持"只取标题、不看画面"。
    mode: str = "skip"

    def check(self, titles: tuple[str, ...] = (), process: str = "") -> PrivacyVerdict:
        if not self.enabled:
            return PrivacyVerdict(True, "privacy.disabled")
        haystack = " ".join(titles).lower()
        for pattern in self.title_patterns:
            if pattern and pattern.lower() in haystack:
                return PrivacyVerdict(False, f"title:{pattern}")
        if process:
            lowered = process.lower()
            for pattern in self.process_patterns:
                if pattern and pattern.lower() in lowered:
                    return PrivacyVerdict(False, f"process:{pattern}")
        return PrivacyVerdict(True)

    @classmethod
    def from_config(cls, section: Any) -> "PrivacyRules":
        """
        从配置构建。**传 None 时退回配置层的内置默认规则**，而不是"没有规则"——
        用户没配隐私段时绝不能默认裸奔。这条有测试盯着。
        """
        from .config import DEFAULT_PRIVACY  # 延迟导入：避免 vision <-> config 循环

        if not isinstance(section, dict):
            section = DEFAULT_PRIVACY
            if not section.get("title_patterns"):
                return cls()
        titles = section.get("title_patterns") or ()
        procs = section.get("process_patterns") or ()
        if isinstance(titles, str):
            titles = (titles,)
        if isinstance(procs, str):
            procs = (procs,)
        return cls(
            title_patterns=tuple(str(t) for t in titles),
            process_patterns=tuple(str(p) for p in procs),
            enabled=bool(section.get("enabled", True)),
            mode=str(section.get("mode", "skip")),
        )


# --------------------------------------------------------------------------- #
# 变化检测
# --------------------------------------------------------------------------- #


def hamming(a: int, b: int) -> int:
    """两个指纹的汉明距离。Python 3.8+ 的 int.bit_count 是最快的写法。"""
    return (a ^ b).bit_count()


def novelty_from_distance(distance: int, threshold: int) -> float:
    """
    汉明距离 -> 0~1 的新鲜度。

    映射刻意做成两段：
      · distance < threshold：0（"没变化"，整轮跳过）；
      · 超过阈值后很快爬到 1，到 4×threshold 就饱和。
    这样"换了个窗口"能立刻拿到高分，而不是随距离缓慢线性上升 ——
    用户期望的是"它注意到我切窗口了"，不是"它按比例注意到"。
    """
    if threshold <= 0:
        return 1.0 if distance > 0 else 0.0
    if distance < threshold:
        return 0.0
    saturated = threshold * 4
    return min(1.0, (distance - threshold) / max(1, saturated - threshold) + 0.5)


@dataclass
class ChangeDetector:
    """
    帧间变化检测 —— 整套方案里最省钱的一个组件。

    没有它，宠物会以固定周期把截图送给视觉模型，一天下来是纯浪费；
    有了它，"屏幕没变"这一最常见的情况（用户在看视频、读文档、发呆）
    会被一次整数异或挡掉。
    """

    threshold: int = _DEFAULT_HAMMING_THRESHOLD
    min_interval_s: float = 0.0
    duplicate_hits: int = 0
    change_hits: int = 0
    total_frames: int = 0
    last_signature: Optional[int] = None
    last_change_at: Optional[float] = None

    def observe(self, frame: Frame) -> Optional[Observation]:
        """
        看一帧，返回 Observation（值得评论）或 None（没变化，跳过）。

        这是纯函数式的：同一个 detector 顺序喂同一串帧，结果完全确定。
        """
        self.total_frames += 1
        signature = frame.signature
        now = frame.captured_at

        if (
            self.last_change_at is not None
            and self.min_interval_s > 0
            and now - self.last_change_at < self.min_interval_s
        ):
            self.duplicate_hits += 1
            return None

        if self.last_signature is None:
            # 第一帧总是"新的"：否则宠物启动后要等用户先动屏幕才看得到东西
            self.last_signature = signature
            self.last_change_at = now
            self.change_hits += 1
            return Observation(
                novelty=1.0,
                content=frame.text_hint,
                titles=frame.titles,
                changed_ratio=1.0,
                captured_at=now,
                source="first_frame",
            )

        distance = hamming(signature, self.last_signature)
        novelty = novelty_from_distance(distance, self.threshold)
        self.last_signature = signature

        if novelty <= 0.0:
            self.duplicate_hits += 1
            return None

        self.change_hits += 1
        self.last_change_at = now
        return Observation(
            novelty=novelty,
            content=frame.text_hint,
            titles=frame.titles,
            changed_ratio=min(1.0, distance / 64.0),
            captured_at=now,
        )

    @property
    def skipped(self) -> int:
        """被跳过的帧数。与 duplicate_hits 同义，取这个名字是为了读起来直观。"""
        return self.duplicate_hits

    @property
    def saved_ratio(self) -> float:
        """被跳过的比例 —— 直接对应"省下多少模型调用"。"""
        if self.total_frames == 0:
            return 0.0
        return self.duplicate_hits / self.total_frames

    def stats(self) -> dict[str, Any]:
        return {
            "total_frames": self.total_frames,
            "change_hits": self.change_hits,
            "skipped": self.duplicate_hits,
            "saved_ratio": round(self.saved_ratio, 3),
            "threshold": self.threshold,
        }


# --------------------------------------------------------------------------- #
# 灰度网格提取
# --------------------------------------------------------------------------- #


def grid_from_rgb(pixels: bytes, width: int, height: int, channels: int = 4) -> list[int]:
    """
    把紧凑排列的像素缓冲压成 8x8 灰度均值网格。

    逐像素遍历整个屏幕在 Python 里太慢（1920x1080 就是两百万次循环），
    所以用**分块采样**：每个网格格子里只均匀取若干点。对"判断画面是否变化"
    这个目的来说完全够用，而且把开销从秒级降到毫秒级。
    """
    if width <= 0 or height <= 0 or channels <= 0:
        return [0] * (_GRID * _GRID)
    expected = width * height * channels
    if len(pixels) < expected:
        raise ValueError(f"像素数据不足：需要 {expected} 字节，实际 {len(pixels)}")

    row_stride = width * channels
    grid: list[int] = []
    # 每个格子取 4x4 个采样点
    samples_per_cell = 4
    for gy in range(_GRID):
        for gx in range(_GRID):
            total = 0
            count = 0
            for sy in range(samples_per_cell):
                y = min(height - 1, int((gy + (sy + 0.5) / samples_per_cell) * height / _GRID))
                for sx in range(samples_per_cell):
                    x = min(width - 1, int((gx + (sx + 0.5) / samples_per_cell) * width / _GRID))
                    offset = y * row_stride + x * channels
                    # 人眼对绿色的权重最高，用亮度近似而不是简单平均
                    r = pixels[offset]
                    g = pixels[offset + 1] if channels > 1 else r
                    b = pixels[offset + 2] if channels > 2 else r
                    total += (r * 299 + g * 587 + b * 114) // 1000
                    count += 1
            grid.append(total // max(1, count))
    return grid


def digest(text: str) -> str:
    """内容指纹，用于日志与去重（避免把整段截屏文字打进日志）。"""
    return hashlib.blake2b(text.encode("utf-8", errors="replace"), digest_size=8).hexdigest()


# --------------------------------------------------------------------------- #
# 感知循环
# --------------------------------------------------------------------------- #


class VisionLoop:
    """
    周期性看屏幕，把"值得评论的变化"变成事件。

    与自主调度器的接口只有一个：`observation_age_s()`。
    调度器靠它算"屏幕新鲜度"这一项意愿分。这样感知与决策互不知晓对方，
    任一侧替换（换采集器、换 VLM、换策略）都不影响另一侧。

    两条重要的行为约定：

    · **没有新东西时报告"陈旧"，而不是"未知"。** 如果返回 None，调度器会把
      require_observation 当成不满足；返回一个很大的秒数则表达"我看过，但没什么
      新东西" —— 这才是准确的语义，也避免了"没变化就当成没看"的逻辑错误。

    · **被隐私规则拦下时不更新观察时间。** 用户的密码管理器界面不该被当作
      "值得评论的新鲜事"，也不该刷新新鲜度让它更容易开口。
    """

    def __init__(
        self,
        bus: Bus,
        capturer: ScreenCapturer,
        rules: PrivacyRules,
        detector: Optional[ChangeDetector] = None,
        interval_s: float = 20.0,
        idle_backoff_max: int = 4,
        clock: Callable[[], float] = time.monotonic,
        content: Any = None,
        content_timeout_s: float = 20.0,
    ) -> None:
        self.bus = bus
        self.capturer = capturer
        self.rules = rules
        self.detector = detector or ChangeDetector()
        self.interval_s = max(1.0, interval_s)
        self.idle_backoff_max = max(1, idle_backoff_max)
        self._clock = clock
        # 内容抽取链（P4 内容层）。None 表示只靠窗口标题，不做额外抽取。
        self.content = content
        self.content_timeout_s = max(1.0, content_timeout_s)
        self.paused = False
        # 用户的持久意愿：是否允许看屏幕。默认 True，由配置里的 enabled 决定，
        # 并可通过 config.update 在运行时切换（见 set_watching）。
        self.watching = True
        self.last_observation: Optional[Observation] = None
        self.blocked_count = 0
        self.last_block_reason = ""
        self.content_stats: list[dict[str, Any]] = []
        # 本帧原始数据，供异步内容抽取使用（step 是同步的，无法 await 抽取）
        self._last_frame: Optional[Frame] = None

    # ------------------------------------------------------------------ 对外

    def observation_age_s(self) -> Optional[float]:
        """
        距上一次"看到值得说的东西"过了多少秒。

        · None          —— 从未看过（采集器不可用 / 刚启动），调度器当作"没有观察"
        · 很大的秒数    —— 看过，但很久没有变化
        · 小秒数        —— 刚刚有变化，此刻最想评论
        """
        if self.last_observation is None:
            return None
        return self.last_observation.age_s(self._clock())

    def set_paused(self, paused: bool) -> None:
        if paused != self.paused:
            log.info("看屏幕：%s", "已暂停" if paused else "已恢复")
        self.paused = paused

    def set_watching(self, watching: bool) -> None:
        """
        运行时开关"看屏幕"。

        与 `set_paused` 的区别是**语义**：paused 是临时打断（例如宠物自己在说话），
        watching 是用户的持久意愿（我根本不想让它看我屏幕）。两者都让 step()
        直接返回 None，但只有 watching 会写进配置、重启后依然有效。

        为什么必须有这个方法：采集器是在构造时建好的（`build_capturer` 读的是
        启动时的配置），而 `config.update` 只会改配置对象。没有这个开关的话，
        "界面上的开关"就只是改了一个没人读的字段 —— 用户以为关掉了，其实还在截屏。
        这种"看起来生效了"的假开关比没有开关更糟。
        """
        if watching != self.watching:
            log.info("看屏幕：%s", "已开启" if watching else "已关闭（不再截屏）")
        self.watching = watching

    # ------------------------------------------------------------------ 单步

    def step(self) -> Optional[Observation]:
        """
        看一帧并判定。返回 Observation（有新东西）或 None（跳过/被拦截）。

        单独暴露出来是为了测试：不需要起循环、不需要等定时器。
        """
        if self.paused or not self.watching:
            return None

        frame = self.capturer.grab()
        if frame is None:
            return None
        # 记下本帧，供 _enrich 异步做内容抽取用（step 是同步的，不能 await 抽取）
        self._last_frame = frame

        verdict = self.rules.check(frame.titles)
        if not verdict:
            self.blocked_count += 1
            if verdict.reason != self.last_block_reason:
                # 只在原因变化时打日志：否则用户开着密码管理器就会被刷屏
                log.info("因隐私规则跳过该窗口（%s），不读取画面内容", verdict.reason)
                self.last_block_reason = verdict.reason
            return None
        self.last_block_reason = ""

        observation = self.detector.observe(frame)
        if observation is None:
            return None

        self.last_observation = observation
        log.debug(
            "屏幕有变化：novelty=%.2f 变化比例=%.2f 标题=%r",
            observation.novelty,
            observation.changed_ratio,
            observation.content[:80],
        )
        return observation

    # ------------------------------------------------------------------ 内容抽取

    async def astep(self, enrich: bool = True) -> Optional[Observation]:
        """
        一次完整的感知步骤：抓帧 -> 隐私 -> 变化 -> **内容抽取**。

        抽取放在线程池里并带超时：OCR 可能慢到几百毫秒，视觉模型可能慢到几秒，
        而感知循环绝不能被它们拖住 —— 否则"看屏幕"会连带把自主调度也卡住。
        超时就放弃这一帧的内容，观测本身照样有效（标题还在）。
        """
        observation = self.step()
        if observation is None or not enrich:
            return observation
        await self._enrich(observation)
        return observation

    async def _enrich(self, observation: Observation) -> None:
        """异步补全内容。失败/超时都只是"内容空一点"，不影响观测可用。"""
        if self.content is None:
            return
        frame = getattr(self, "_last_frame", None)
        if frame is None:
            return
        try:
            extraction = await asyncio.wait_for(
                asyncio.to_thread(self.content.extract, frame),
                timeout=self.content_timeout_s,
            )
        except asyncio.TimeoutError:
            log.debug("内容抽取超时（%.0fs），本帧只用窗口标题", self.content_timeout_s)
            return
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 —— 抽取器本不该抛，这里兜底
            log.exception("内容抽取异常（已忽略）")
            return

        text = getattr(extraction, "text", "") or ""
        source = getattr(extraction, "source", "")
        if text and text != observation.content:
            observation.content = text
            observation.source = f"grid+{source}"
        self.content_stats.append(
            {
                "source": source,
                "chars": len(text),
                "elapsed_ms": round(float(getattr(extraction, "elapsed_ms", 0.0)), 1),
                "error": getattr(extraction, "error", ""),
            }
        )
        del self.content_stats[:-50]  # 只留最近 50 条，避免长期运行攒内存

    # ------------------------------------------------------------------ 循环

    async def run_forever(self) -> None:
        """
        常驻循环。画面没变化时把间隔逐步拉长到 `interval_s × idle_backoff_max`，
        一旦有变化立刻回到基准间隔。

        这是"看屏幕"这件事的开销控制：大部分时间用户在做同一件事，
        高频截屏纯属浪费电和 CPU，但变化发生后必须立刻恢复灵敏。
        """
        backoff = 1
        log.info(
            "看屏幕循环启动：实现=%s 基准间隔=%.0fs（无变化时最多拉长到 %d 倍）",
            getattr(self.capturer, "name", type(self.capturer).__name__),
            self.interval_s,
            self.idle_backoff_max,
        )
        while True:
            try:
                observation = await self.astep()
                if observation is not None:
                    backoff = 1
                    await self.bus.publish(
                        "vision.observation",
                        {
                            "novelty": round(observation.novelty, 4),
                            "content": observation.content,
                            "titles": list(observation.titles),
                            "changed_ratio": round(observation.changed_ratio, 4),
                            "source": observation.source,
                        },
                    )
                else:
                    backoff = min(self.idle_backoff_max, backoff + 1)
            except asyncio.CancelledError:
                log.info("看屏幕循环已停止")
                raise
            except Exception:  # noqa: BLE001 —— 感知故障绝不能拖垮整个宠物
                log.exception("看屏幕单轮异常（已忽略，继续下一轮）")
            await asyncio.sleep(self.interval_s * backoff)

    def stats(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "capturer": getattr(self.capturer, "name", type(self.capturer).__name__),
            "paused": self.paused,
            "privacy_blocked": self.blocked_count,
            "last_block_reason": self.last_block_reason,
            "observation_age_s": (
                None if self.observation_age_s() is None else round(self.observation_age_s() or 0.0, 1)
            ),
            **self.detector.stats(),
        }
        if self.content is not None:
            result["content"] = self.content.stats()
            if self.content_stats:
                result["last_extraction"] = self.content_stats[-1]
        return result
