"""
「截图对话」—— 用户主动按下按钮/快捷键，抓一张屏，交给模型看，然后聊。

## 和 `vision.py`（后台看屏幕）的区别，这是本模块存在的全部理由

```
vision.py          后台**持续**看屏幕：每 20 秒一帧、变化检测、隐私过滤、
                   只在"值得说"时才开口。它属于"宠物自己在看"。
screen_chat.py     用户**主动**按一下：抓一帧、立刻看懂、立刻开口。
                   它属于"用户给宠物看"。
```

两者共用同一套底层件（`PillowCapturer` / `OpenAIVisionExtractor` /
`PrivacyRules`），但**生命周期完全不同**：一个是常驻循环，一个是单次动作。
把单次动作塞进 `VisionLoop` 会很别扭（要临时改 watching、改间隔、还要
区分"这帧是不是用户要的"），所以单独一个模块。

## 🔴 为什么这里**不看** `vision.enabled`

`vision.enabled=false` 的语义是「**别自己偷偷看我的屏幕**」（默认关闭，
用户要求）。而按下这个按钮是**用户明确说"看这个"** —— 两件事的授权来源不同。

如果这里也读 `vision.enabled`，那么默认配置下这个功能**永远不可用**，
而用户点了按钮却什么都没发生（表现是"功能坏了"，不是"被设置挡住了"）。

⚠️ 但 `privacy.*`（密码 / 银行 / 网银 这类黑名单）**仍然生效** ——
见下面 `capture_and_describe` 里的说明：那是"绝不能送出去"的硬约束，
不是"别自己看"的意愿开关。

## 🔴 看图必须关掉思考模式

2026-10-17 实测（`deepseek-flash`，它支持图片输入）：

```
max_tokens=200,  thinking 不传      -> 正文 0 字（推理吃了 200 token）
max_tokens=1200, thinking 不传      -> 正文 144 字，推理 960 token，6.2 秒
max_tokens=400,  thinking disabled  -> 正文 99 字，  推理 0，       1.3 秒
```

第一行不报错、不抛异常，只是正文是空的 —— 看起来像"模型看不了图"，
实际是**思考模式把 token 吃完了**。所以默认 `thinking=False`。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from .content import OpenAIVisionExtractor
from .vision import Frame, PrivacyRules
from .vision_capture import PillowCapturer

log = logging.getLogger("pet.screen_chat")

# 送给视觉模型的提示词。刻意要求"具体、不要客套"，因为它的产物会直接
# 变成宠物说的话的**事实依据** —— 含糊的描述会让它只能回一句"嗯，看到了"。
# 内置兜底：与 `config/persona.yaml` 的 `prompts.screen_chat` **必须逐字一致**。
#
# 为什么两处都要有、而且要一致：yaml 读不到（用户打错缩进）时要退回它，
# 那时**不能悄悄用一套旧规则** —— 那正是「记忆又长出模糊条目」那类
# 静默降级的成因。`tools/check-extract-prompt.py` 会比对这两处。
DESCRIBE_PROMPT = (
    "这是用户电脑此刻的屏幕截图。请用 1~2 句话描述你看到的画面：\n"
    "他在用什么程序、正在看什么内容、有没有什么显眼的细节。\n"
    "要求：具体（说出程序名/页面/内容要点），不要客套话，不要猜测他的情绪。\n"
    "⚠️ 如果画面里有密码、密钥、身份证号、银行卡号之类的信息，一个字都不要复述。"
)


def _describe_prompt(config: Any = None) -> str:
    """取画面描述提示词；用户可在 `prompts.screen_chat` 覆盖（失败退回内置）。"""
    root = getattr(config, "root", None)
    section = (root or {}).get("prompts") if isinstance(root, dict) else None
    raw = (section or {}).get("screen_chat") if isinstance(section, dict) else None
    if isinstance(raw, str) and raw.strip():
        return raw
    return DESCRIBE_PROMPT


@dataclass
class SnapResult:
    """一次「截图对话」的结果。**把失败原因带出来**，不能只返回空。"""

    ok: bool
    # 给模型看/给自己看的画面描述（视觉模型产出，失败时退化为窗口标题）
    description: str = ""
    # 描述是从哪来的：vlm / titles / none
    source: str = "none"
    # 失败或降级的原因（会进日志、也会告诉用户）
    message: str = ""
    titles: tuple[str, ...] = ()
    width: int = 0
    height: int = 0
    elapsed_ms: float = 0.0
    # 抓到的原始图像字节（**不落盘**，只在内存里活过一次调用）
    # 原图字节（JPEG）：autosnap 说话时写进原文记忆，用户能回看"它当时看到什么"
    image: bytes = b""
    image_bytes: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "source": self.source,
            "message": self.message,
            "width": self.width,
            "height": self.height,
            "elapsed_ms": round(self.elapsed_ms, 1),
            "titles": list(self.titles),
        }


def _build_extractor(config: Any) -> tuple[Optional[OpenAIVisionExtractor], str]:
    """
    按配置建视觉抽取器。返回 `(抽取器或 None, 不可用原因)`。

    ## 默认**复用大脑那一家**（不另配一个供应商）

    用户这台机器上大脑是 DeepSeek，而 `deepseek-flash` 支持图片输入
    （2026-10-17 实测）。所以默认情况下**不需要任何额外配置**就能看图 ——
    这比"再让用户去配一个硅基流动的视觉模型"体验好得多。

    三处都可以单独覆盖：
        base_url      -> screenshot.base_url      -> brain.base_url
        model         -> screenshot.model         -> brain.model
        api_key       -> screenshot.api_key_env   -> 该厂商的凭据
    """
    section = dict(getattr(config, "screenshot", None) or {})
    brain = dict((getattr(config, "providers", None) or {}).get("brain") or {})

    base_url = str(section.get("base_url") or "").strip()
    model = str(section.get("model") or "").strip()
    if not base_url or not model:
        # 取"当前生效的那一家"（named/active），而不是平铺旧字段
        profile = {}
        try:
            profile = dict(config.brain_profile() or {})
        except Exception:  # noqa: BLE001 —— 取不到就退回平铺字段
            profile = {}
        base_url = base_url or str(profile.get("base_url") or brain.get("base_url") or "").strip()
        model = model or str(profile.get("model") or brain.get("model") or "").strip()

    key_env = str(section.get("api_key_env") or "").strip()
    api_key = ""
    if key_env:
        import os

        api_key = os.environ.get(key_env, "").strip()
        if not api_key:
            return None, f"环境变量 {key_env} 为空"
    else:
        try:
            api_key = config.api_key()
        except Exception:  # noqa: BLE001
            api_key = ""

    if not base_url:
        return None, "没有 base_url（screenshot.base_url 与 providers.brain 都为空）"
    if not model:
        return None, "没有 model（screenshot.model 与 providers.brain 都为空）"
    if not api_key:
        return None, "没有可用的 API 密钥"

    extractor = OpenAIVisionExtractor(
        base_url=base_url,
        model=model,
        api_key=api_key,
        prompt=str(section.get("prompt") or _describe_prompt(config)),
        timeout_s=float(section.get("timeout_s") or 45.0),
        max_tokens=int(section.get("max_tokens") or 400),
        # 🔴 看图必须关思考：默认 False（见模块顶部的实测数据）
        thinking=bool(section.get("thinking", False)),
    )
    return extractor, ""


def capture_once(config: Any) -> Optional[Frame]:
    """
    抓一帧（**带图像**）。阻塞调用 —— 调用方要放到线程里跑。

    `keep_image=True` 是必须的：不带图像字节的话，视觉模型那一层会
    直接返回"没有可发送的图像数据"，而它看起来像"模型不支持"。
    """
    section = dict(getattr(config, "screenshot", None) or {})
    capturer = PillowCapturer(
        include_titles=True,
        keep_image=True,
        max_edge_px=int(section.get("max_edge_px") or 768),
    )
    if not capturer.available:
        log.warning("截图不可用（Pillow 没有 ImageGrab）")
        return None
    return capturer.grab()


# ## 单飞（single-flight）：同一时间只允许一个看图请求（2026-10-18）
#
# ## 为什么需要它（实测）
#
# 用户反馈「时不时截图变慢」。量出来的事实：
#
# ```
# 空闲时用同一个抽取器连测 6 次：875 ~ 1421 ms（中位 1172）
# 日志里 16 点那一小时：27 次自主截图，每次 12~19 秒
# ```
#
# 两者唯一的区别是**并发**：自主截图循环每 2 分钟发一次看图，每次要 15 秒，
# 于是几乎总有一个请求在飞；手动截图这时发出去就排在后面，
# 用户看到的是"按了 Ctrl+Alt+S，等 15 秒"。
#
# ## 规则
#
# ```
# 自主截图   wait=False —— 发现已经有一个在跑就**跳过这一次**，绝不排队
# 手动截图   wait=True  —— 最多等手上那一个跑完（因为自主截图不排队，所以不会被堆住）
# ```
#
# ⚠️ 不要给自主截图也开 wait=True：那会变成"每 2 分钟排一个 15 秒的队"，
# 队列只会越积越长 —— 排队解决不了排队，**少发**才是解法。

_VISION_LOCK: Optional[asyncio.Lock] = None


def _vision_lock() -> asyncio.Lock:
    """懒建锁：不要在 import 时创建（那时可能还没有事件循环）。"""
    global _VISION_LOCK
    if _VISION_LOCK is None:
        _VISION_LOCK = asyncio.Lock()
    return _VISION_LOCK


async def capture_and_describe(config: Any, *, wait: bool = True) -> SnapResult:
    """
    抓屏 + 看懂。**这是「截图对话」的全部后端动作。**

    `wait=False` 是给自主截图用的：**忙就跳过，不排队**（见上面单飞的说明）。
    """
    lock = _vision_lock()
    if lock.locked() and not wait:
        log.info("看图：上一个还在跑，这次跳过（自主截图不排队）")
        return SnapResult(ok=False, message="正在看别的东西，这次跳过")
    if lock.locked():
        log.info("看图：等前一个请求结束（手动截图排在下一个）")
    async with lock:
        return await _capture_and_describe_locked(config)


async def _capture_and_describe_locked(config: Any) -> SnapResult:
    """
    单飞保护内的真正实现（不要再直接调这个 —— 走 `capture_and_describe`）。

    ## 顺序是刻意的：隐私检查在**看懂之前**

    `PrivacyRules` 命中就要在把图送给模型**之前**停下 —— 一旦发出去就收不回来了。
    所以这里先取窗口标题做判定，通过了才调视觉模型。

    ## 隐私在这里的取舍（与后台看屏幕不同，见模块顶部）

    后台看屏幕：命中 -> 静默跳过（用户不需要知道）。
    这里：命中 -> **告诉用户**"被隐私规则挡住了，关掉那个窗口再试"。
    因为这是用户主动发起的动作，静默失败会被当成"功能坏了"。
    """
    started = time.monotonic()
    section = dict(getattr(config, "screenshot", None) or {})

    # ① 抓屏（阻塞，放线程）
    try:
        frame = await asyncio.to_thread(capture_once, config)
    except Exception as exc:  # noqa: BLE001 —— 抓屏失败不能影响任何别的功能
        log.debug("截图异常", exc_info=True)
        return SnapResult(ok=False, message=f"截图失败：{type(exc).__name__}: {exc}")
    if frame is None:
        return SnapResult(ok=False, message="截图失败（拿不到画面：屏幕锁定 / 远程会话 / 没有权限）")

    elapsed = (time.monotonic() - started) * 1000
    base = SnapResult(
        ok=False,
        titles=tuple(frame.titles),
        width=frame.width,
        height=frame.height,
        image=frame.image or b"",
        image_bytes=len(frame.image or b""),
    )

    # ② 隐私检查（**在发给模型之前**）
    rules = PrivacyRules.from_config((getattr(config, "privacy", None) or {}))
    if section.get("respect_privacy", True):
        verdict = rules.check(titles=frame.titles)
        if not verdict:
            log.info("截图被隐私规则挡住：%s", verdict.reason)
            base.message = (
                f"这个画面被隐私规则挡住了（命中「{verdict.reason.split(':', 1)[-1]}」）。"
                "关掉那个窗口再试一次。"
            )
            base.elapsed_ms = elapsed
            return base

    # ③ 看懂（视觉模型）
    extractor, reason = _build_extractor(config)
    if extractor is None:
        # 没有视觉模型时**不报死**：退回窗口标题，仍然能聊两句
        titles = [t for t in frame.titles if t.strip()]
        if titles:
            base.ok = True
            base.source = "titles"
            base.description = "用户在看着这些窗口：" + "、".join(titles[:5])
            base.message = f"没有可用的视觉模型（{reason}），只能看到窗口标题"
        else:
            base.message = f"没有可用的视觉模型（{reason}），也拿不到窗口标题"
        base.elapsed_ms = elapsed
        log.info("截图对话降级到标题级：%s", base.message)
        return base

    try:
        extraction = await asyncio.to_thread(extractor.extract, frame)
    except Exception as exc:  # noqa: BLE001
        log.debug("看图异常", exc_info=True)
        base.message = f"看图失败：{type(exc).__name__}: {exc}"
        base.elapsed_ms = (time.monotonic() - started) * 1000
        return base

    base.elapsed_ms = (time.monotonic() - started) * 1000
    if extraction.ok and extraction.text.strip():
        base.ok = True
        base.source = "vlm"
        base.description = extraction.text.strip()
        log.info("看图成功（%.0fms）：%s", base.elapsed_ms, base.description[:60])
        return base

    # 视觉模型失败：仍然退回标题级，**不要因为看不懂就什么都不说**
    titles = [t for t in frame.titles if t.strip()]
    base.message = extraction.error or "视觉模型没有给出描述"
    if titles:
        base.ok = True
        base.source = "titles"
        base.description = "用户在看着这些窗口：" + "、".join(titles[:5])
        base.message = base.message + "（已退化为只看窗口标题）"
    log.warning("看图失败，降级：%s", base.message)
    return base


def describe_for_prompt(result: SnapResult) -> str:
    """
    把结果整理成给模型看的**那一段话**。

    ⚠️ 刻意不写"AI 描述"这类元信息：宠物应该像是在**自己看**，
    而不是在念一份检测报告。所以是"你看到：…"，不是"视觉模型输出：…"。
    """
    if not result.ok or not result.description:
        return ""
    hint = (
        f"你刚刚看了用户的屏幕，你看到的是：{result.description}\n"
        "现在**主动**跟他说说这个画面（一两句话，顺着内容聊，别像在念检测报告）。"
    )
    if result.source == "titles":
        hint += "\n（只拿到了窗口标题，画面内容你不清楚 —— 别装作看懂了细节）"
    return hint


def stage_direction_for_self(result: SnapResult) -> str:
    """
    **自己**起意看了一眼屏幕时的那条 user 消息（2026-10-18）。

    和 `stage_direction_for`（用户把屏幕给你看）的区别在**责任方**：
    那份是"你刚看了我的屏幕"，这份是"你自己想看看"。措辞混了会很怪 ——
    用户没说给你看，你却说"你刚把屏幕给我看"。

    ⚠️ 仍然要求"说画面里的东西、别换话题"：自己看屏幕时最容易变成
    自言自语地聊自己，那就白看了。
    """
    if not result.ok or not result.description:
        return ""
    detail = result.description
    if result.source == "titles":
        return (
            "（你自己起意瞥了一眼屏幕，但只看清了窗口标题："
            f"{detail}。画面里具体是什么你不清楚，别装作看懂了细节。）"
        )
    return (
        f"（你自己起意看了一眼屏幕，看到的是：{detail}"
        "\n想说一句就说，说你看到的这个画面 —— 别换成别的话题，也别考据。）"
    )


def stage_direction_for(result: SnapResult) -> str:
    """
    自主发言时那条 user 消息（**模型最当真的位置**）。

    ## 为什么和 `describe_for_prompt` 是两份

    `describe_for_prompt` 进 system，是**背景**；这一份进 user 轮，是**指令**。
    同一个事实写两遍不是冗余 —— 实测（2026-10-17）只放 system 时，
    模型会被同一段里的记忆召回带偏，完全无视画面描述：

        用户截图 Steam 商店页 -> 它回「命令行滚成那样，是又在跑构建吗」

    所以这里要写成**祈使句**，明确"就这个说，别换话题"，并给出画面本身。

    ⚠️ 措辞上不写"AI 描述/识别结果"这类元信息：它应该像是在自己看。
    """
    if not result.ok or not result.description:
        return ""
    detail = result.description
    if result.source == "titles":
        # 只看得到标题时，必须明确"你不知道画面细节"，否则它会编
        return (
            "（你刚瞥了一眼我的屏幕，但只看清了窗口标题："
            f"{detail}。画面里具体是什么你不清楚，别装作看懂了细节。）"
        )
    return (
        f"（你刚看了我的屏幕，你看到的是：{detail}"
        "\n就着这个画面跟我说一两句 —— 说画面里的东西，别换成别的话题。）"
    )
