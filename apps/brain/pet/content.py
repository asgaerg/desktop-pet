"""
屏幕内容理解（P4 的内容层）—— 把"看到了一帧画面"变成"知道上面是什么"。

这一层解决的问题是：变化检测只能告诉你"屏幕变了"，窗口标题只覆盖了很小一部分
信息（而且标题经常是"未命名 - 编辑器"这种废话）。要真正"看懂"，需要读画面内容。

三级内容抽取，逐级更贵、也更准：

    L1 窗口标题   WindowTitleExtractor   零成本、零延迟。默认就开着。
    L2 本地 OCR   OcrExtractor           免费、离线；需要装 OCR 引擎。
    L3 视觉模型   OpenAIVisionExtractor  能真正看懂画面；按量计费。
    兜底          NullExtractor          什么都不抽（不看屏幕时的实现）。

**成本控制的关键不在这一层，而在变化检测**：VLM 按调用收费，所以
`vision.ChangeDetector` 先把"画面没变"的情况挡掉，内容抽取只会发生在
真正有变化的帧上。自检里有一条断言专门验证这个比例（实测能省约 90%）。

设计约束：
  · 抽取器**不得抛异常**表达"抽不出来" —— 返回空串即可。屏幕锁定、模型限流、
    额度用尽都是正常情况，宠物照常工作，只是话变少了。
  · 抽取是**有超时**的，且由上层异步执行，绝不阻塞感知循环。
"""

from __future__ import annotations

import base64
import json
import logging
import os
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Protocol

from . import netutil
from .vision import Frame

log = logging.getLogger("pet.content")

# 抽取结果的文本上限。再长对"评论一句"也没有帮助，只是白烧 token。
MAX_CONTENT_CHARS = 600


@dataclass(slots=True)
class Extraction:
    """一次内容抽取的结果。`source` 用于日志与统计（哪一级在起作用、贵不贵）。"""

    text: str = ""
    source: str = "none"
    elapsed_ms: float = 0.0
    ok: bool = True
    error: str = ""

    def __bool__(self) -> bool:
        return bool(self.text.strip())


class ContentExtractor(Protocol):
    """内容抽取器。实现者**不得抛异常**表达失败。"""

    name: str

    def available(self) -> bool:  # pragma: no cover - 协议
        ...

    def extract(self, frame: Frame) -> Extraction:  # pragma: no cover - 协议
        ...


# --------------------------------------------------------------------------- #
# L1：窗口标题（零成本）
# --------------------------------------------------------------------------- #


class WindowTitleExtractor:
    """直接用采集阶段顺带拿到的窗口标题。没有额外开销，所以永远开着。"""

    name = "title"

    def available(self) -> bool:
        return True

    def extract(self, frame: Frame) -> Extraction:
        hint = frame.text_hint
        return Extraction(text=hint, source=self.name, ok=bool(hint))


class NullExtractor:
    """什么都不抽。用于"不看屏幕"以及测试里隔离内容层。"""

    name = "none"

    def available(self) -> bool:
        return True

    def extract(self, frame: Frame) -> Extraction:
        return Extraction(text="", source=self.name)


# --------------------------------------------------------------------------- #
# L2：本地 OCR
# --------------------------------------------------------------------------- #


def _tesseract_binary() -> Optional[str]:
    """
    找一个可用的 tesseract 可执行文件。

    除了 PATH，还试几个常见安装位置 —— Windows 上 tesseract 的安装器默认不写 PATH，
    用户装了却"用不上"会很困惑。
    """
    from shutil import which

    found = which("tesseract")
    if found:
        return found
    for candidate in (
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        os.path.expanduser(r"~\AppData\Local\Tesseract-OCR\tesseract.exe"),
    ):
        if os.path.exists(candidate):
            return candidate
    return None


class OcrExtractor:
    """
    本地 OCR。优先用 pytesseract，其次直接调用 tesseract 可执行文件。

    引擎缺失时 `available()` 返回 False，上层会自然跳过它 —— 所以配上这一级
    永远是安全的：装了就用，没装就不占任何开销。
    """

    name = "ocr"

    def __init__(
        self,
        language: str = "chi_sim+eng",
        binary: Optional[str] = None,
        timeout_s: float = 6.0,
        max_chars: int = MAX_CONTENT_CHARS,
    ) -> None:
        self.language = language
        self.binary = binary or _tesseract_binary()
        self.timeout_s = timeout_s
        self.max_chars = max_chars
        self._pytesseract: Any = None
        try:  # pragma: no cover - 取决于环境
            import pytesseract  # noqa: PLC0415

            self._pytesseract = pytesseract
        except Exception:  # noqa: BLE001
            self._pytesseract = None

    def available(self) -> bool:
        return self._pytesseract is not None or self.binary is not None

    def extract(self, frame: Frame) -> Extraction:
        started = time.monotonic()
        image = frame.image
        if not image:
            # 没有图像数据（采集时未保留）时只能退回标题
            return Extraction(text=frame.text_hint, source="title", ok=False)

        text = ""
        error = ""
        try:
            if self._pytesseract is not None:
                text = self._via_pytesseract(image)
            elif self.binary:
                text = self._via_binary(image)
        except Exception as exc:  # noqa: BLE001 —— 抽取失败不能影响感知循环
            error = f"{type(exc).__name__}: {exc}"
            log.debug("OCR 抽取失败：%s", error)

        elapsed = (time.monotonic() - started) * 1000
        cleaned = _tidy(text, self.max_chars)
        if not cleaned and not error:
            error = "未识别到文字"
        return Extraction(text=cleaned, source=self.name, elapsed_ms=elapsed, ok=bool(cleaned), error=error)

    def _via_pytesseract(self, image: bytes) -> str:
        import io  # noqa: PLC0415

        from PIL import Image  # noqa: PLC0415

        with Image.open(io.BytesIO(image)) as img:
            return str(self._pytesseract.image_to_string(img, lang=self.language))

    def _via_binary(self, image: bytes) -> str:
        """
        直接调用 tesseract：把图片写到临时文件，用 `tesseract <in> stdout` 读文本。

        刻意不加 `-l` 之外的参数：语言包缺失时 tesseract 会报错，
        而我们要的是"失败就返回空"，不是让整条链路崩掉。
        """
        import tempfile  # noqa: PLC0415

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as handle:
            handle.write(image)
            path = handle.name
        try:
            result = subprocess.run(  # noqa: S603 - 参数是我们自己拼的，无用户输入
                [self.binary, path, "stdout", "-l", self.language],
                capture_output=True,
                timeout=self.timeout_s,
                check=False,
            )
            if result.returncode != 0:
                raise RuntimeError(result.stderr.decode("utf-8", errors="replace")[:200])
            return result.stdout.decode("utf-8", errors="replace")
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass


# --------------------------------------------------------------------------- #
# L3：视觉模型
# --------------------------------------------------------------------------- #

_DEFAULT_VISION_PROMPT = (
    "这是用户电脑屏幕的截图。用一句中文描述用户在做什么，"
    "并列出画面上最显眼的少量文字（窗口标题、报错、代码片段等）。"
    "只描述你确实看到的内容，不要推测用户意图，不要给建议。"
    "总长度不超过 120 字。"
)


class OpenAIVisionExtractor:
    """
    调用 OpenAI 兼容的多模态接口看图。

    这是内容层里唯一"真能看懂画面"的实现，也是唯一按量收费的，所以：
      · 只在变化检测判定"有变化"之后才被调用（上层保证）；
      · 图片在采集时就压到 `max_edge_px`，token 消耗降一个数量级；
      · 失败（限流、余额、断网）一律返回空结果，宠物继续靠标题说话。
    """

    name = "vlm"

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str,
        prompt: str = _DEFAULT_VISION_PROMPT,
        timeout_s: float = 30.0,
        max_tokens: int = 220,
        max_chars: int = MAX_CONTENT_CHARS,
        thinking: Optional[bool] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.prompt = prompt
        self.timeout_s = timeout_s
        self.max_tokens = max_tokens
        self.max_chars = max_chars
        self.calls = 0
        """
        ## 🔴 看图必须能关掉思考模式（2026-10-17 实测）

        `deepseek-flash` **支持图片输入**，但默认开了思考模式，而
        `thinking` 的 token 与正文**共用 `max_tokens`**。实测三种取值：

            200  token，thinking 不传       -> 正文 **0 字**（全被推理吃掉）
            1200 token，thinking 不传       -> 正文 144 字，推理 960 token，6.2 秒
            400  token，thinking 显式关掉   -> 正文 99 字，  推理 0，       1.3 秒

        第一行就是"模型返回空内容"这个错误最可能的真因 —— 它不报错、
        不抛异常，只是正文是空的，而日志里只留一句"模型返回空内容"。

        `None` 表示**不传这个字段**（别的 OpenAI 兼容服务不一定认它，
        传了可能直接 400）—— 与 `LLMRequest.thinking` 的三态语义完全一致。
        """
        self.thinking = thinking

    def available(self) -> bool:
        return bool(self.base_url and self.model and self.api_key)

    def extract(self, frame: Frame) -> Extraction:
        started = time.monotonic()
        # 字段名是 frame.image。早期这里写成 frame.jpeg，而 Frame 上并没有该字段，
        # getattr 的默认值把问题掩盖成了"没有可发送的图像数据" —— 视觉模型因此
        # 永远不会被真正调用。属性名不一致时不要用 getattr 兜底，让它直接报错更好。
        image = frame.image
        if not image:
            return Extraction(source=self.name, ok=False, error="没有可发送的图像数据")
        if not self.available():
            return Extraction(source=self.name, ok=False, error="视觉模型未配置完整（缺 base_url/model/key）")

        try:
            payload = self._request(image)
        except urllib.error.HTTPError as exc:
            return Extraction(
                source=self.name,
                elapsed_ms=(time.monotonic() - started) * 1000,
                ok=False,
                error=_humanize_http(exc),
            )
        except Exception as exc:  # noqa: BLE001
            return Extraction(
                source=self.name,
                elapsed_ms=(time.monotonic() - started) * 1000,
                ok=False,
                error=f"{type(exc).__name__}: {exc}",
            )

        self.calls += 1
        text = _tidy(payload, self.max_chars)
        return Extraction(
            text=text,
            source=self.name,
            elapsed_ms=(time.monotonic() - started) * 1000,
            ok=bool(text),
            error="" if text else "模型返回空内容",
        )

    def _request(self, image: bytes) -> str:
        """
        组装并发送一次 chat/completions 请求（把图片作为 data URL 塞进 content 数组）。
        沿用与文本大脑相同的 OpenAI 兼容约定，换供应商只改 base_url/model。
        """
        data_url = f"data:image/png;base64,{base64.b64encode(image).decode('ascii')}"
        body = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": self.prompt},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                }
            ],
            "max_tokens": self.max_tokens,
            "temperature": 0.2,  # 描述画面要稳，不要"创作"
        }
        # 思考模式：None = 不传字段（对其他服务商最安全），
        # False = 显式关掉（看图时**必须**关，否则正文会被推理吃掉，见 __init__）
        if self.thinking is not None:
            body["thinking"] = {"type": "enabled" if self.thinking else "disabled"}
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
                "User-Agent": "desktop-pet/0.1",
            },
        )
        with netutil.urlopen(request, timeout=self.timeout_s) as response:
            parsed = json.loads(response.read().decode("utf-8", errors="replace"))
        choices = parsed.get("choices") or []
        if not choices:
            raise RuntimeError(f"响应里没有 choices：{str(parsed)[:200]}")
        message = choices[0].get("message") or {}
        content = message.get("content")
        if isinstance(content, list):  # 有的供应商返回分段内容
            return "".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
        return str(content or "")


def _humanize_http(exc: urllib.error.HTTPError) -> str:
    detail = ""
    try:
        parsed = json.loads(exc.read().decode("utf-8", errors="replace"))
        error = parsed.get("error")
        detail = (error.get("message") if isinstance(error, dict) else "") or parsed.get("message", "")
    except Exception:  # noqa: BLE001
        detail = ""
    table = {
        401: "视觉模型密钥无效",
        402: "视觉模型余额不足",
        403: "该密钥无权使用此视觉模型",
        404: "视觉模型名或 base_url 不对",
        413: "截图太大被拒（可调小 vision.max_edge_px）",
        429: "视觉模型限流或配额用尽",
    }
    hint = table.get(exc.code, f"HTTP {exc.code}")
    return f"{hint}{('：' + detail) if detail else ''}"


def _tidy(text: str, limit: int = MAX_CONTENT_CHARS) -> str:
    """压缩空白并截断。模型与 OCR 都可能吐出一堆换行和空行。"""
    if not text:
        return ""
    lines = [line.strip() for line in str(text).replace("\r", "\n").split("\n")]
    cleaned = " ".join(part for part in lines if part)
    cleaned = " ".join(cleaned.split())
    if len(cleaned) > limit:
        cleaned = cleaned[:limit].rstrip() + "…"
    return cleaned


# --------------------------------------------------------------------------- #
# 组装
# --------------------------------------------------------------------------- #


class ContentPipeline:
    """
    按成本从低到高依次尝试，凑够内容就停。

    默认顺序是 **标题 → 本地 OCR → 视觉模型**，每一步都有意义：
      · 标题常常已经够用（"main.py - VS Code"就说明了很多），零成本；
      · 标题信息不足时才动用 OCR（免费但慢）；
      · 只有前两级都给不出东西，才花钱调视觉模型。

    这个顺序是可以被配置改的，但默认值刻意保守 —— 默认不烧钱。
    """

    def __init__(
        self,
        extractors: list[ContentExtractor],
        min_chars_before_next: int = 8,
        stats_sink: Optional[Callable[[Extraction], None]] = None,
    ) -> None:
        self.extractors = extractors
        self.min_chars_before_next = min_chars_before_next
        self._stats_sink = stats_sink
        self.calls: dict[str, int] = {}
        self.costly_calls = 0

    def available_chain(self) -> list[str]:
        return [e.name for e in self.extractors if e.available()]

    def extract(self, frame: Frame) -> Extraction:
        last: Extraction = Extraction(source="none", ok=False, error="没有可用的抽取器")
        for extractor in self.extractors:
            if not extractor.available():
                continue
            result = extractor.extract(frame)
            self.calls[extractor.name] = self.calls.get(extractor.name, 0) + 1
            if extractor.name == "vlm" and result.ok:
                self.costly_calls += 1
            if self._stats_sink is not None:
                self._stats_sink(result)
            last = result
            if len(result.text) >= self.min_chars_before_next:
                return result
        return last

    def stats(self) -> dict[str, Any]:
        return {
            "chain": self.available_chain(),
            "calls": dict(self.calls),
            "costly_calls": self.costly_calls,
        }


def build_content_pipeline(
    config: dict[str, Any],
    brain_section: dict[str, Any],
    api_key: str,
) -> ContentPipeline:
    """
    按配置构建内容抽取链。

    `config` 是 providers.vision 段；`brain_section` 用于复用 base_url 与密钥配置
    （视觉与文本通常是同一个供应商，没必要配两遍）。
    """
    order = config.get("content_order") or ["title", "ocr", "vlm"]
    extractors: dict[str, ContentExtractor] = {
        "title": WindowTitleExtractor(),
        "none": NullExtractor(),
        "ocr": OcrExtractor(language=str(config.get("ocr_language") or "chi_sim+eng")),
        "vlm": OpenAIVisionExtractor(
            base_url=str(config.get("base_url") or brain_section.get("base_url") or ""),
            model=str(config.get("model") or ""),
            api_key=api_key or str(config.get("api_key") or ""),
            prompt=str(config.get("prompt") or _DEFAULT_VISION_PROMPT),
            timeout_s=float(config.get("timeout_s") or 30.0),
        ),
    }

    chain: list[ContentExtractor] = []
    for name in order:
        extractor = extractors.get(str(name).lower())
        if extractor is None:
            log.warning("未知的 vision.content_order 项 %r（已忽略）", name)
            continue
        chain.append(extractor)
    if not chain:
        chain = [WindowTitleExtractor()]
    log.info("内容抽取链：%s", " -> ".join(e.name for e in chain))
    return ContentPipeline(chain)
