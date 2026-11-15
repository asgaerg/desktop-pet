"""
屏幕采集的 Windows 实现。

这一层是**薄适配器**，故意不含任何决策逻辑：抓到像素、压成灰度网格、交给
`vision.ChangeDetector`。所有"值不值得看/该不该说"的判断都在 vision.py 里，
那样才能离线测试。

设计约束（每条都有代价，写清楚以免后人误改）：

1. **依赖 Pillow，但不强求。** 它已经随环境提供，`ImageGrab` 内部就是 GDI，
   拿到的正是我们需要的像素缓冲。缺了 Pillow 就返回 None（等于"看不到屏幕"），
   绝不让宠物起不来 —— 看屏幕是锦上添花，不是生存必需。

2. **必须显式设置 DPI 感知。** 不设的话，在缩放 125%/150% 的显示器上
   Windows 会返回被系统拉伸过的虚拟分辨率，抓到的图是模糊的，
   感知哈希会因此变得迟钝。这是很典型的"看起来能用、效果莫名差"的坑。

3. **只取主显示器。** 多屏拼接后的坐标系更复杂，而且宠物"看屏幕"的语义
   本来就是"看用户在用的那块"。多屏支持留到有真实需求时再做。

4. **失败一律返回 None，不抛异常。** 屏幕锁定、远程桌面断连、权限受限都是
   正常情况，不是错误。异常只留给真正的程序缺陷。
"""

from __future__ import annotations

import ctypes
import logging
import re
import sys
from datetime import datetime
from typing import Any, Optional

from .vision import Frame, ScreenCapturer, grid_from_rgb

log = logging.getLogger("pet.vision.capture")

# 缩放相关：不设置 DPI 感知时，Windows 会把我们的坐标当成"逻辑像素"，
# 抓出来的图在 125% 缩放下实际是被拉伸过的。
try:  # pragma: no cover - 平台相关
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
except Exception:  # noqa: BLE001
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:  # noqa: BLE001
        pass


def _clean_title(raw: str) -> str:
    """
    清洗窗口标题。

    真实的窗口标题里常混着零宽字符（U+200B、U+FEFF）、软连字符、以及各种
    控制字符 —— 常见于 Electron 应用的标题栏。它们会带来两个实际麻烦：
      · 污染提示词（模型会看到一堆不可见字符）；
      · 触发编码错误（GBK 控制台直接抛 UnicodeEncodeError，把日志搞崩）。

    所以在这里一次性剥掉，而不是指望下游各处都做防护。
    """
    if not raw:
        return ""
    # 先干掉明确的零宽/格式类字符，再去掉控制字符
    for ch in ("\u200b", "\u200c", "\u200d", "\ufeff", "\u00ad", "\u2060"):
        raw = raw.replace(ch, "")
    cleaned = "".join(ch for ch in raw if ch.isprintable())
    return re.sub(r"\s+", " ", cleaned).strip()


def _window_titles(limit: int = 3) -> tuple[str, ...]:
    """
    取当前可见的顶层窗口标题。这是**零成本**的语义信息：
    比多模态模型便宜几个数量级，能用就别动用视觉模型。

    只取有标题且可见的窗口，按 Z 序（前台在前）。标题会做清洗（见 _clean_title）。
    """
    if sys.platform != "win32":
        return ()
    try:
        user32 = ctypes.windll.user32
    except Exception:  # noqa: BLE001
        return ()

    titles: list[str] = []
    # WNDENUMPROC 回调：顶层窗口按 Z 序枚举
    EnumWindowsProc = ctypes.WINFUNCTYPE(
        ctypes.c_bool, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)
    )

    def callback(hwnd: Any, _lparam: Any) -> bool:
        if len(titles) >= limit:
            return False
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        title = _clean_title(buffer.value)
        # 过滤掉空标题、纯符号标题，以及明显的系统浮层
        if title and len(title) >= 2 and title not in titles:
            titles.append(title)
        return True

    try:
        user32.EnumWindows(EnumWindowsProc(callback), None)
    except Exception:  # noqa: BLE001
        log.debug("枚举窗口标题失败", exc_info=True)
    return tuple(titles)


class PillowCapturer:
    """基于 Pillow ImageGrab 的采集器。"""

    name = "pillow"

    def __init__(
        self,
        include_titles: bool = True,
        keep_image: bool = False,
        max_edge_px: int = 0,
        hd_edge_px: int = 0,
    ) -> None:
        """
        :param keep_image: 是否保留编码后的图像。只有内容层里配了 OCR 或视觉模型时
            才该打开 —— 否则每帧都白白编码一张 PNG，纯属浪费。
        :param max_edge_px: 保留图像时长边压到多少。**0 = 不缩放，原像素**
            （2026-11-15 用户要求改成默认）。
        :param hd_edge_px: **给人看的**那份长边多少。**0 = 不缩放**。

        ## 🔴 2026-11-15：默认从 768 改成**不缩放**

        用户原话：

        > 「所以图片是模糊截图的吗，难怪有时候它图片识别不太清楚，
        >   这样，所有截图全都用原图像素」

        ## 原来 768 的代价（两个都中招）

        那个数字是**为省 token** 定的：压到长边 768，一次视觉调用的
        图像 token 少很多。但它同时影响了两件不该被压的事：

            ① 存进原文记忆的那张 —— 用户在记忆管理点开看，糊
            ② **喂给视觉模型的那张** —— 小字、代码、UI 细节全糊，
               识别准确率因此下降（用户说的"识别不太清楚"）

        ② 才是真损失：省的是钱，赔的是准确率。而分辨率的收益
        不是线性的 —— 768 到 1920，小字从"糊成一团"到"看得清"，
        这一步的差别远大于 1920 到 4K。

        ## 现在的策略

        默认**原像素不缩放**（1080p 屏幕就是 1920x1080）。
        `max_edge_px` 留着当"上限保险"：设了才生效，设 0 不缩放。
        """
        self.include_titles = include_titles
        self.keep_image = keep_image
        self.max_edge_px = max(0, max_edge_px)
        # 高清那份：0 也当"不缩放"。两者默认一致（都是原图），
        # 编两份纯粹是历史包袱 —— 但保留字段免得下游全改。
        self.hd_edge_px = max(0, hd_edge_px)
        self._grabber: Any = None
        self._import_error: Optional[str] = None
        try:
            from PIL import ImageGrab  # noqa: PLC0415

            self._grabber = ImageGrab
        except Exception as exc:  # noqa: BLE001
            self._import_error = str(exc)

    @property
    def available(self) -> bool:
        return self._grabber is not None

    def grab(self) -> Optional[Frame]:
        if self._grabber is None:
            return None
        try:
            image = self._grabber.grab()
        except Exception as exc:  # noqa: BLE001
            # 屏幕锁定 / 远程会话断开都会走到这里，属正常情况，只记一次调试日志
            log.debug("截屏失败：%s", exc)
            return None
        if image is None:
            return None
        try:
            rgb = image.convert("RGB")
            grid = grid_from_rgb(rgb.tobytes(), rgb.width, rgb.height, channels=3)
            # 🔴 2026-11-15：**只编一份**（原像素）。
            #
            # 原来编两份（768 喂模型 + 1600 存记忆）。用户要求
            # 「所有截图全都用原图像素」之后，两者变成同一张 ——
            # 再编两次就是纯浪费（一张 1080p PNG 编码要几十毫秒）。
            #
            # ⚠️ 保留 `image_hd` 字段并指向同一份字节：下游
            #    （`gateway._remember_dnd_snap` 等）还在读它，
            #    指同一份等于"高清版就是原图"，语义也对。
            payload = self._encode(rgb) if self.keep_image else None
        except Exception:  # noqa: BLE001
            log.debug("像素转换失败", exc_info=True)
            return None
        return Frame(
            grid=grid,
            width=rgb.width,
            height=rgb.height,
            titles=_window_titles() if self.include_titles else (),
            image=payload,
            image_hd=payload,
        )

    def _encode(self, image: Any, *, edge: Optional[int] = None) -> Optional[bytes]:
        """编码成 PNG。**默认不缩放**（原像素）。

        :param edge: 长边上限。不传用 `max_edge_px`；两者都是 0（默认）
            时不缩放。设了就按那个上限等比缩。

        ⚠️ 缩放用 LANCZOS —— 默认的 NEAREST 缩小后会明显锯齿/发糊，
           而那正是用户报「挺糊的」的一部分原因。
        """
        import io  # noqa: PLC0415

        limit = int(self.max_edge_px if edge is None else edge)
        try:
            if limit > 0:
                longest = max(image.width, image.height)
                if longest > limit:
                    scale = limit / float(longest)
                    target = (
                        max(1, int(image.width * scale)),
                        max(1, int(image.height * scale)),
                    )
                    from PIL import Image as _Image  # noqa: PLC0415

                    image = image.resize(target, _Image.LANCZOS)
            buffer = io.BytesIO()
            image.save(buffer, format="PNG", optimize=True)
            return buffer.getvalue()
        except Exception:  # noqa: BLE001
            log.debug("图像编码失败", exc_info=True)
            return None

    def describe(self) -> dict[str, Any]:
        return {
            "impl": self.name,
            "available": self.available,
            "error": self._import_error,
        }


class ScriptedCapturer:
    """
    按脚本产出帧的采集器：`{时:分, 标题: [...]}` 这样描述"几点在做什么"。

    它存在的意义不是测试玩具，而是**演示与回归**：不需要真的去用别人的电脑，
    就能验证"切到写代码的窗口 -> 宠物评论一句"这条链路。也方便录制成
    fixtures 供以后做视觉模型的效果对比。
    """

    name = "scripted"

    def __init__(self, entries: list[dict[str, Any]], frame_factory: Any = None) -> None:
        self._entries = entries
        self._index = 0
        self._frame_factory = frame_factory

    def grab(self) -> Optional[Frame]:
        if self._index >= len(self._entries):
            return None
        entry = self._entries[self._index]
        self._index += 1
        factory = self._frame_factory or _frame_from_titles
        return factory(entry)


def _frame_from_titles(entry: dict[str, Any]) -> Frame:
    """从脚本条目造一帧。grid 用条目的 seed 抖动，让不同"场景"指纹不同。"""
    import random

    rng = random.Random(str(entry.get("seed", entry.get("titles", ""))))
    base = 90 + rng.randint(0, 60)
    grid = [max(0, min(255, base + rng.randint(-40, 40))) for _ in range(64)]
    titles = entry.get("titles") or ()
    if isinstance(titles, str):
        titles = (titles,)
    return Frame(grid=grid, width=1920, height=1080, titles=tuple(titles))


def build_capturer(config: dict[str, Any], keep_image: bool = False) -> ScreenCapturer:
    """
    按配置构建采集器。

    provider 取值：
      · "none"   —— 不看屏幕（默认；也用于"用户关掉看屏幕"）
      · "pillow" —— 真实截屏
      · 其它     —— 记警告并退回 none

    `keep_image` 由上层根据"内容链里有没有需要看图的抽取器"决定：
    没有 OCR / 视觉模型时保留图像纯属浪费（每帧编码一张 PNG）。
    """
    provider = str(config.get("provider") or "none").lower()
    if provider == "none":
        from .vision import NullCapturer

        return NullCapturer()
    if provider == "pillow":
        # ⚠️ 这里**不能用 `or 0` 或 `or 768`** —— `0 or X` 在 Python 里等于 X，
        #    会把"不缩放（0）"吃掉，静默变回缩放。显式判 None 才安全。
        _raw_edge = config.get("max_edge_px")
        _raw_hd = config.get("hd_edge_px")
        capturer = PillowCapturer(
            include_titles=bool(config.get("include_titles", True)),
            keep_image=keep_image,
            # 🔴 2026-11-15：默认**不缩放**（原像素）。
            # 用户原话：「所有截图全都用原图像素」。
            max_edge_px=0 if _raw_edge is None else int(_raw_edge),
            hd_edge_px=0 if _raw_hd is None else int(_raw_hd),
        )
        if capturer.available:
            log.info(
                "采屏实现：pillow（保留图像=%s，长边上限=%dpx）",
                keep_image,
                capturer.max_edge_px,
            )
            return capturer
        log.warning(
            "vision.provider=pillow 但 Pillow 不可用（%s），本次退化为不看屏幕",
            capturer.describe().get("error"),
        )
        from .vision import NullCapturer

        return NullCapturer()
    log.warning("未知的 vision.provider=%r，退化为不看屏幕", provider)
    from .vision import NullCapturer

    return NullCapturer()


def stamp() -> str:
    """给抓到的帧打时间戳（日志用）。"""
    return datetime.now().strftime("%H:%M:%S")
