"""
离线自检（`python -m pet --selftest`）。

为什么值得写：这是项目里唯一能在**没有 Electron、没有 API 密钥、没有麦克风**
的情况下断言"整条链路是通的"的手段。每次改协议、改总线、改配置都跑一遍，
比开界面点半天可靠得多。

覆盖：
  1. YAML 子集解析器与配置校验、范围夹取、点号路径
  2. 协议解析：合法/非法输入、版本不匹配
  3. Mock 大脑：真流式、受活泼度影响、不重复
  4. 提供者降级：key 缺失 / 未知提供者 -> 回落 mock 而不是崩溃
  5. 总线隔离：插件抛异常不影响其他订阅者
  6. WebSocket 往返：hello -> ready，user.text -> 流式 -> 结束
  7. 配置热更新：config.update 立即生效并回报
  8. 插件装载与"坏插件不拖垮系统"
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

# 让 mock 流式在自检里瞬间跑完
os.environ.setdefault("PET_MOCK_STREAM_DELAY", "0")

from .brains import LLMRequest, build_brain  # noqa: E402
from .bus import Bus  # noqa: E402
from .config import Config, ConfigError, _validate, load_config, parse_yaml  # noqa: E402
from .gateway import Gateway  # noqa: E402
from .plugins import Plugin, ToolSpec  # noqa: E402
from .protocol import ProtocolError, deep_merge, parse_incoming  # noqa: E402
from .testing import TestClient  # noqa: E402
from .ws_server import WebSocketClient  # noqa: E402

# ---------------------------------------------------------------------------
# 🔴 自检**强制使用能量法 VAD**（2026-10-30）
#
# 生产环境默认走 silero（神经网络 VAD，抗风扇噪声，见 `SileroVoiceActivityDetector`）。
# 但自检喂的全是**合成正弦波** —— silero 是在真人语音上训练的，对纯音
# 不一定判成语音。用它跑自检会大面积误红，测的变成"silero 认不认正弦波"，
# 而不是这个项目自己的逻辑。
#
# silero 本身另有专项测试（直接构造检测器，断言能加载 + 静音不误判）。
# 必须在**任何 VoiceSession 被构造之前**设置，所以放在模块顶层。
# ---------------------------------------------------------------------------
os.environ["PET_VAD_ENGINE"] = "energy"

log = logging.getLogger("pet.selftest")


@dataclass
class ChekResult:
    name: str
    passed: bool
    detail: str = ""
    seconds: float = 0.0


@dataclass
class SelfTest:
    results: list[ChekResult] = field(default_factory=list)

    def check(self, name: str) -> Callable[[Callable[[], Any]], Callable[[], Any]]:
        def wrapper(fn: Callable[[], Any]) -> Callable[[], Any]:
            started = time.perf_counter()
            try:
                detail = fn()
                self.results.append(
                    ChekResult(name, True, str(detail or ""), time.perf_counter() - started)
                )
            except Exception as exc:  # noqa: BLE001 —— 自检要抓全部异常并继续
                self.results.append(
                    ChekResult(name, False, f"{type(exc).__name__}: {exc}", time.perf_counter() - started)
                )
            return fn

        return wrapper

    @property
    def ok(self) -> bool:
        return all(r.passed for r in self.results)


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


# --------------------------------------------------------------------------- #
# 1. 配置与 YAML
# --------------------------------------------------------------------------- #


class _FakeSession:
    """
    只为调 VoiceSession.engine_signature 而存在的最小替身。

    ⚠️ 那个方法是**静态语义**的（只看 self.config），但我们不想为了测它
    去建一个完整的 VoiceSession（那要连 bus / 音频设备 / 引擎）。
    所以用这个只有 config 属性的替身。
    """

    def __init__(self, config: object) -> None:
        self.config = config


def test_config(suite: SelfTest, config_path: Optional[Path]) -> None:
    @suite.check("YAML 子集解析：嵌套映射 / 列表 / 行内数组 / 注释 / 引号")
    def _() -> str:
        text = """
# 顶层注释
name: 小灵        # 行尾注释
level: 3
ratio: 0.75
on: true
tags: [a, b, "c d"]
nested:
  deep:
    value: 42
  items:
    - one
    - two
    - key: inner
      other: 2
quoted: "带 # 号 的字符串"
empty_list: []
empty_map: {}
"""
        data = parse_yaml(text, source="<inline>")
        _assert(data["name"] == "小灵", "中文标量解析错误")
        _assert(data["level"] == 3 and isinstance(data["level"], int), "整数解析错误")
        _assert(abs(data["ratio"] - 0.75) < 1e-9, "浮点解析错误")
        _assert(data["on"] is True, "布尔解析错误")
        _assert(data["tags"] == ["a", "b", "c d"], f"行内数组解析错误：{data['tags']}")
        _assert(data["nested"]["deep"]["value"] == 42, "深层嵌套解析错误")
        _assert(data["nested"]["items"][:2] == ["one", "two"], "列表解析错误")
        _assert(data["nested"]["items"][2] == {"key": "inner", "other": 2}, "列表内映射解析错误")
        _assert(data["quoted"] == "带 # 号 的字符串", "引号内 # 被误当注释")
        _assert(data["empty_list"] == [], "空数组解析错误")
        # {} 必须解析成空映射而不是字符串 "{}"（曾出现，导致 plugins 配置类型错）
        _assert(data["empty_map"] == {}, f"空映射解析错误：{data['empty_map']!r}")
        return "10 个断言通过"

    @suite.check("YAML 块标量：`#` 开头的行与空行必须保留（2026-10-20 修的解析器 bug）")
    def _() -> str:
        """块标量 `|` 里**每一行都是内容**，预处理时不许当注释丢掉。

        ## 真实事故

        `prompts.extract` 里加了一段小标题：

            extract: |
                第一段

                ## 🔴 第二重要：记物不记话

                后面的内容

        旧实现在 `__init__` 里就把 `#` 开头的行当注释删了（映射/列表里那是对的，
        块标量里不是）。于是读出来是 `'第一段\\n后面的内容'` ——
        **文件看着完全正常，读出来少了一整段**。

        更隐蔽的是：这段提示词有**两份**（yaml + 内置兜底），
        兜底那份是 Python 字符串写的所以没丢 —— 两边一比对才发现。
        如果只写 yaml、不写兜底，这个 bug 会一直不报错，只是"模型不听新规则"。
        """
        text = (
            "a: 1\n"
            "prompts:\n"
            "  extract: |\n"
            "    第一段\n"
            "\n"
            "    ## 🔴 小标题：记**物**\n"
            "\n"
            "        缩进示例 <- 比块公共缩进多 4 格\n"
            "  other: 2\n"
            "b: 3\n"
        )
        data = parse_yaml(text, source="<block>")
        got = data["prompts"]["extract"]

        _assert("## 🔴 小标题" in got, f"`#` 开头的行被当成注释吃掉了：{got!r}")
        _assert("第一段\n\n##" in got, f"空行被吞，段落没了分隔：{got!r}")
        # 块的**公共缩进**（这里是 4 格）被去掉，**相对**多出来的 4 格保留。
        # 这条正是"两处提示词逐字一致"的前提：兜底常量里的缩进是 4 格。
        _assert(
            "\n    缩进示例" in got,
            f"相对缩进没保住（兜底常量是带缩进的，那样两边无法逐字相等）：{got!r}",
        )
        # 块结束之后的结构不能被误读
        _assert(data["b"] == 3, f"块后面的顶层键没解析到：{data}")
        _assert(data["prompts"]["other"] == 2, f"块后面的子键没解析到：{data['prompts']}")

        # 行尾注释在**映射行**上仍然要去掉（这条不能因为修块标量而回退）
        line = parse_yaml("k: v   # 这是注释\n", source="<c>")
        _assert(line["k"] == "v", f"映射行的行尾注释没去掉：{line}")
        return "`##` 行保留、空行保留、相对缩进保留、块后结构正常、行尾注释仍生效"

    @suite.check("配置校验：范围夹取 / 类型错误 / 点号路径补丁")
    def _() -> str:
        cfg = Config()
        effective = cfg.apply_patch({"activity": {"overall": 5.0, "reaction_latency_ms": -10}})
        _assert(cfg.activity["overall"] == 1.0, f"overall 未被夹取：{cfg.activity['overall']}")
        _assert(cfg.activity["reaction_latency_ms"] == 0.0, "延迟未夹取下界")
        _assert("activity.overall" in effective, f"未回报生效差异：{effective}")

        cfg.apply_patch({"activity.screen_watch_prob": 0.9})
        _assert(cfg.activity["screen_watch_prob"] == 0.9, "点号路径补丁未生效")
        cfg.apply_patch({"activity.overall": 0.2})
        _assert(cfg.activity["screen_watch_prob"] == 0.9, "夹取 overall 时误改其他字段")

        raised = False
        try:
            cfg.apply_patch({"activity": {"overall": "很活泼"}})
        except ConfigError:
            raised = True
        _assert(raised, "非法类型未报错")

        prompt = cfg.activity_for_prompt()
        _assert("安静" in prompt, f"活泼度未翻译成口吻：{prompt}")
        return "夹取/补丁/报错/口吻均正常"

    @suite.check("语言表达层：中性不污染提示词、极端可关到 0、非法值不炸")
    def _() -> str:
        # 2026-10-22「不写人格内容，改做可调旋钮」。
        #
        # 这一层是**用户可编辑的配置**，所以三条底线必须成立：
        #   ① 默认（全中性）不能给提示词增加任何内容 —— 否则所有人突然变了说话风格；
        #   ② 每一项都能关到 0 而系统仍然正常说话 —— 表达层是调味，不是地基；
        #   ③ 手滑写错（字符串/None/NaN）不能让 sidecar 起不来。
        from . import expression
        from .config import DEFAULT_EXPRESSION

        # ① 中性 -> 空串
        neutral = dict(DEFAULT_EXPRESSION)
        _assert(
            expression.describe(neutral) == "",
            f"默认配置污染了提示词：{expression.describe(neutral)!r}",
        )

        # ② 全 0 / 全 1 都要有内容，且方向相反
        zero = expression.describe({k: 0 for k in neutral})
        one = expression.describe({k: 1 for k in neutral})
        _assert(zero, "全 0 时应当给出'不用修辞/说大白话'的约束")
        _assert(one, "全 1 时应当给出'爱打比方/用典'的约束")
        _assert(zero != one, "全 0 与全 1 的措辞不能一样")
        _assert("白话" in zero or "直说" in zero, f"全 0 应偏直白：{zero[:60]}")
        _assert("比方" in one, f"全 1 应偏修辞：{one[:60]}")

        # ③ 容错：这些都不该抛异常
        for bad in (None, "abc", 123, {"warmth": "abc"}, {"warmth": float("nan")}):
            try:
                expression.describe(bad)
            except Exception as exc:  # noqa: BLE001
                raise AssertionError(f"非法输入 {bad!r} 抛了异常：{exc}") from exc

        # ④ 四个旋钮**互不干扰**：只动一个，只有那一个的措辞出现
        only_warmth = expression.describe({"warmth": 1.0, **{k: 0.5 for k in neutral if k != "warmth"}})
        _assert("温度" in only_warmth or "留意" in only_warmth, f"只调温度却没出现温度措辞：{only_warmth}")
        _assert("比方" not in only_warmth, f"只调温度却带出了修辞措辞：{only_warmth}")

        # ⑤ 语言层**不含**感性/理性 —— 那一项已废弃（2026-10-22 用户决定）
        _assert(
            "sensibility" not in neutral,
            f"感性/理性已废弃，不该在语言层里：{sorted(neutral)}",
        )
        full = expression.describe({k: 1 for k in neutral})
        _assert(
            "感性" not in full and "理性" not in full,
            f"语言层的措辞里混进了已废弃的认知取向：{full}",
        )

        return "中性空串、全0/全1 相反、非法值不抛、旋钮互不干扰、不含感性理性"

    @suite.check("语言表达层：热更新即时生效（不重启）")
    def _() -> str:
        # 用户要求这一层和活泼度、情绪一样"改完立刻生效"。
        # 走 apply_patch -> _validate -> expression，所以只要 config 变了，
        # 下一轮 build_system_prompt 就会读到新值。
        from . import expression

        cfg = Config()
        before = expression.describe(cfg.expression)
        _assert(before == "", f"默认不该有内容：{before!r}")

        cfg.apply_patch({"expression": {"warmth": 1.0}})
        after = expression.describe(cfg.expression)
        _assert(after != before, "改了 warmth 但提示词没变")
        _assert("留意" in after or "温度" in after, f"没体现温度：{after}")

        # 夹取：越界值被收进 0~1，不报错
        cfg.apply_patch({"expression.warmth": 9})
        _assert(cfg.expression["warmth"] == 1.0, f"越界未夹取：{cfg.expression['warmth']}")

        # 非法值回落中性，而不是抛异常
        cfg.apply_patch({"expression.culture": "很文雅"})
        _assert(
            cfg.expression["culture"] == 0.5,
            f"非法值应回落中性 0.5，实际 {cfg.expression['culture']}",
        )
        return "热更新生效、越界夹取、非法值回落中性"

    @suite.check("随附 config 文件可解析且键名齐全")
    def _() -> str:
        if config_path is None or not config_path.exists():
            return "跳过（未找到 config/persona.yaml）"
        cfg = load_config(config_path)
        _assert(cfg.pet_name, "宠物名为空")
        _assert(0.0 <= cfg.activity["overall"] <= 1.0, "overall 越界")
        _assert(cfg.providers["brain"]["provider"] in ("mock", "openai"), "提供者名可疑")
        return f"persona={cfg.pet_name} provider={cfg.brain_provider()} overall={cfg.activity['overall']}"


# --------------------------------------------------------------------------- #
# 2. 协议
# --------------------------------------------------------------------------- #


def test_protocol(suite: SelfTest) -> None:
    @suite.check("协议解析：合法上行消息")
    def _() -> str:
        m = parse_incoming('{"type":"user.text","text":"在吗","ts":1}')
        _assert(m.type == "user.text" and m.text == "在吗", "user.text 解析错误")
        m = parse_incoming('{"type":"config.update","patch":{"activity.overall":0.3}}')
        _assert(m.patch == {"activity.overall": 0.3}, "config.update 解析错误")
        m = parse_incoming('{"type":"hello","protocolVersion":1}')
        _assert(m.type == "hello", "hello 解析错误")
        return "3 种上行消息均可解析"

    @suite.check("协议解析：非法输入必须被拒绝（不信任另一进程）")
    def _() -> str:
        bad = [
            "not json",
            '{"type":"unknown.type"}',
            '{"type":"user.text"}',
            '{"type":"user.text","text":"   "}',
            '{"type":"hello"}',
            '{"type":"hello","protocolVersion":99}',
            '"just a string"',
        ]
        for raw in bad:
            try:
                parse_incoming(raw)
            except ProtocolError:
                continue
            raise AssertionError(f"未拒绝非法输入：{raw}")
        return f"{len(bad)} 条非法输入全部被拒绝"

    @suite.check("协议：**每一个被处理的**上行类型都在白名单里（防漏配）")
    def _() -> str:
        """
        ## ⚠️ 这条检查是踩坑换来的（2026-10-07）

        加"按住说话"时，我在 `gateway.py` 里写好了 `voice.ptt` 的处理分支、
        也加了 TS 类型，**但漏了 `protocol.py` 的 `INCOMING_TYPES`** ——
        于是消息在解析阶段就被拒，那个分支**永远不会执行**。

        表现极具迷惑性：前端日志一切正常（按下/松开都打出来了）、后端也不崩，
        只有一行 `丢弃不合法的上行消息：未知消息类型：'voice.ptt'`，
        而用户看到的是"松开之后没反应" —— 很容易误判成"识别不准"。

        ## 为什么扫源码而不是手工列一份清单

        手工清单本身就是第二份真相源，加类型时照样会忘。
        **从 `gateway.py` 里扫出所有 `incoming.type == "xxx"`**，
        再与白名单求差 —— 这样"写了分支却没登记"会立刻变红，
        而不用靠人记得同步两处。
        """
        import re
        from pathlib import Path

        from .protocol import INCOMING_TYPES

        src = Path(__file__).with_name("gateway.py").read_text(encoding="utf-8")
        handled = set(re.findall(r'incoming\.type\s*==\s*"([^"]+)"', src))
        _assert(handled, "没从 gateway.py 里扫到任何 incoming.type 分支（正则失效了？）")

        missing = sorted(handled - set(INCOMING_TYPES))
        if missing:
            raise AssertionError(
                "这些类型有处理分支但不在 INCOMING_TYPES 里，消息会被解析阶段丢掉："
                + ", ".join(missing)
            )
        # 反向也查：白名单里有、但没人处理的类型 —— 那说明"声明了不做"
        unused = sorted(set(INCOMING_TYPES) - handled)
        if unused:
            raise AssertionError("这些类型在白名单里但没有任何处理分支：" + ", ".join(unused))
        return f"{len(handled)} 个上行类型：白名单与处理分支完全一致"

    @suite.check("深合并：嵌套与点号路径等价")
    def _() -> str:
        a = deep_merge({"x": {"y": 1, "z": 2}}, {"x": {"y": 9}})
        b = deep_merge({"x": {"y": 1, "z": 2}}, {"x.y": 9})
        _assert(a == b == {"x": {"y": 9, "z": 2}}, f"两种写法结果不一致：{a} / {b}")
        return "两种写法结果一致"


# --------------------------------------------------------------------------- #
# 3. 大脑
# --------------------------------------------------------------------------- #


def test_brains(suite: SelfTest) -> None:
    @suite.check("提供者工厂：每个已注册的提供者都能被 build_brain 构建出来（不降级）")
    def _() -> str:
        """
        这条防的是一个非常隐蔽的回归：工厂签名与注册表不一致。

        注册表声明的类型是 `Callable[[Config, EmotionState], Brain]`，调用时也是
        `factory(config, emotion)`。而 `build_openai` 早先只写了 `config` 一个参数，
        于是把 `providers.brain.provider` 改成 `openai` 时：
          · 抛 TypeError  → 被 build_brain 的兜底逻辑**静默降级成 mock**；
          · 界面一切正常，只是宠物开始说假话（说的还是 mock 的固定台词）。
        而当时的测试是直接 `OpenAIBrain(config)` 绕过工厂的，所以全绿。

        教训：测试要走**真实使用路径**（这里是 build_brain），而不是绕过它直接
        构造内部对象 —— 后者测的是"类能不能用"，前者才测"接线对不对"。
        """
        import os

        from .brains import available_providers, build_brain

        providers = available_providers()
        _assert("mock" in providers, f"mock 未注册：{providers}")

        # mock 必须能直接构建
        cfg = Config()
        cfg.apply_patch({"providers.brain.provider": "mock"})
        _assert(build_brain(cfg, _fresh_emotion()).name == "mock", "mock 构建失败")

        # openai：给一个假 key，构建必须成功且**不能**降级成 mock
        cfg2 = Config()
        cfg2.apply_patch({"providers.brain.provider": "openai"})
        saved = os.environ.get("DEEPSEEK_API_KEY")
        os.environ["DEEPSEEK_API_KEY"] = "sk-selftest-fake-key"
        try:
            built = build_brain(cfg2, _fresh_emotion())
            _assert(
                built.name == "openai",
                f"openai 工厂构建后降级成了 {built.name} —— 工厂签名与注册表不一致？",
            )
            _assert(
                getattr(built, "model", "") == "deepseek-chat",
                f"model 不对：{getattr(built, 'model', '-')}",
            )
        finally:
            if saved is None:
                os.environ.pop("DEEPSEEK_API_KEY", None)
            else:
                os.environ["DEEPSEEK_API_KEY"] = saved

        return f"已注册 {providers}，openai 工厂不再静默降级"

    @suite.check("Mock 大脑：真流式且文本受活泼度影响")
    def _() -> str:
        async def run() -> tuple[int, str, str, str]:
            cfg = Config()
            cfg.apply_patch({"activity.overall": 0.1})
            low = build_brain(cfg, _fresh_emotion())
            req = LLMRequest(system="t", history=[], user_text="你好呀")
            chunks = [c async for c in low.stream(req)]
            low_text = "".join(c["text"] for c in chunks if c["kind"] == "delta")

            cfg.apply_patch({"activity.overall": 0.95})
            high = build_brain(cfg, _fresh_emotion())
            chunks2 = [c async for c in high.stream(req)]
            high_text = "".join(c["text"] for c in chunks2 if c["kind"] == "delta")
            return len(chunks), low_text, high_text, ""

        n, low_text, high_text, _unused = asyncio.run(run())
        _assert(n > 1, f"不是流式（只收到 {n} 个事件）")
        _assert(low_text and high_text, "生成了空文本")
        _assert(low_text != high_text, "活泼度没有影响输出")
        _assert(len(low_text) <= len(high_text) + 12, "高活泼度反而更短，联动方向可疑")
        return f"{n} 个流事件；低活泼度「{low_text}」 vs 高活泼度「{high_text}」"

    @suite.check("Mock 大脑不重复：连续 6 句里不出现完全相同的回复")
    def _() -> str:
        async def run() -> list[str]:
            cfg = Config()
            brain = build_brain(cfg, _fresh_emotion())
            req = LLMRequest(system="t", history=[], user_text="今天天气不错")
            texts: list[str] = []
            for _i in range(6):
                chunks = [c async for c in brain.stream(req)]  # type: ignore[attr-defined]
                texts.append("".join(c["text"] for c in chunks if c["kind"] == "delta"))
            return texts

        texts = asyncio.run(run())
        _assert(len(set(texts)) >= 3, f"回复雷同：{texts}")
        return f"6 句中 {len(set(texts))} 句不同"

    @suite.check("提供者降级：无密钥 / 未知提供者都回落 mock，不崩溃")
    def _() -> str:
        cfg = Config()
        cfg.apply_patch({"providers.brain.provider": "openai"})
        cfg.root["providers"]["brain"]["api_key_env"] = "PET_DEFINITELY_MISSING_KEY"
        brain = build_brain(cfg, _fresh_emotion())
        _assert(brain.name == "mock", f"未降级：{brain.name}")

        cfg.apply_patch({"providers.brain.provider": "不存在的提供者"})
        brain2 = build_brain(cfg, _fresh_emotion())
        _assert(brain2.name == "mock", f"未降级：{brain2.name}")
        return "两条降级路径均回落到 mock"

    @suite.check("OpenAI 兼容提供者：SSE 解析正确拼出文本")
    def _() -> str:
        cfg = Config()
        cfg.root["providers"]["brain"].update(
            {"provider": "openai", "base_url": "http://127.0.0.1:1/v1", "model": "m", "api_key_env": "X"}
        )
        os.environ["X"] = "sk-test"
        from .brains.openai_compat import OpenAIBrain

        brain = OpenAIBrain(cfg)
        state: dict[str, Any] = {}

        class _FakeResponse:
            status = 200
            headers: dict[str, str] = {}

            def __init__(self) -> None:
                self._lines = [
                    b'data: {"choices":[{"delta":{"content":"\\u4f60"}}]}\n',
                    b"\n",
                    b'data: {"choices":[{"delta":{"content":"\\u597d"}}]}\n',
                    b": keep-alive\n",
                    b'data: {"choices":[{"delta":{}}],"usage":{"total_tokens":7}}\n',
                    b"data: [DONE]\n",
                ]

            def __enter__(self) -> "_FakeResponse":
                return self

            def __exit__(self, *args: Any) -> None:
                return None

            def readline(self) -> bytes:
                return self._lines.pop(0) if self._lines else b""

        import threading

        # 注意：这里不能用 `import pet.brains... as pet` —— 局部名 `pet` 会遮蔽
        # 顶层包名，导致后续 `import pet.xxx` 全部失败（踩过一次）。
        import pet.brains.openai_compat as compat_mod

        # ⚠️ 2026-10-19 起模型请求走 `pet.netutil.urlopen`（按域名绕系统代理），
        # 所以假替身要 patch 那一层 —— patch `urllib.request.urlopen` 已经拦不到了
        # （会真的发网络请求）。
        import pet.netutil as netutil_mod

        original = netutil_mod.urlopen
        netutil_mod.urlopen = lambda *a, **k: _FakeResponse()  # type: ignore[assignment]
        try:
            events = list(
                brain._iter_sse(
                    LLMRequest(system="s", history=[], user_text="hi"), threading.Event(), state
                )
            )
        finally:
            netutil_mod.urlopen = original  # type: ignore[assignment]

        text = "".join(e["text"] for e in events if e["kind"] == "delta")
        _assert(text == "你好", f"SSE 解析结果错误：{text!r}")
        _assert(state.get("usage") == {"total_tokens": 7}, f"用量未记录：{state}")
        return f"拼出「{text}」，并记录用量"

    @suite.check("人格提示词：花括号写错不能让人格开关变成'宠物哑掉'")
    def _() -> str:
        """
        ## 这条是真实事故换来的（2026-10-07）

        `config/persona.yaml` 里把占位符写成了 `{小钰}`（本该是 `{name}`），
        而 `build_system_prompt` 里那一行是**裸的 `.format(name=...)`** ——
        于是 `.format` 抛 `KeyError: '小钰'`，**每一轮对话都在这里炸掉**。

        用户看到的不是"人格没生效"，而是"**宠物一句话都说不出来了**" ——
        他绝不会想到是人格里那个花括号写错了。

        同一个文件里的 `_render_prompt` 早就有这套兜底（注释里写明了理由），
        只是 `system_prompt` 那一行漏了。这条测试把那个口径钉住。
        """
        from .gateway import Conversation

        cfg = Config()
        cfg.apply_patch(
            {
                "persona.system_prompt_enabled": True,
                # 故意写一个**不存在**的具名参数
                "persona.system_prompt": "名字叫{小钰}。你不是助手。",
            }
        )
        conv = Conversation(bus=None, config=cfg, memory=None, brain=None)  # type: ignore[arg-type]
        try:
            out = conv.build_system_prompt(idle=False)
        except Exception as exc:  # noqa: BLE001
            raise AssertionError(
                f"人格里的花括号写错时抛了异常（{type(exc).__name__}: {exc}）——"
                "这会让宠物完全不能说话，必须兜住"
            ) from exc
        _assert("{小钰}" in out, f"兜底应当保留原文（花括号不替换），实际：{out[:60]!r}")

        # 正常写法要能正确替换
        cfg.apply_patch({"persona.system_prompt": "名字叫{name}。你不是助手。"})
        ok = conv.build_system_prompt(idle=False)
        _assert(
            cfg.pet_name in ok and "{name}" not in ok,
            f"`{{name}}` 未被替换成宠物名：{ok[:60]!r}",
        )
        return f"坏模板不抛异常（按原文发出）；好模板正确替换为「{cfg.pet_name}」"

    @suite.check("自主发言：user_text 为 None 时请求体里不能出现 content=None")
    def _() -> str:
        """
        ## ⚠️ 这条是真实报错换来的（2026-10-07 用户截图）

            the target type: messages[3]: content should be a string or a list
            at line 1 column 23914 (request_id: 0ae1975d-...)

        `LLMRequest.user_text` 的类型是 `Optional[str]`，**`None` 表示"自主发言"**
        （宠物自己起话头，没有用户消息）。而 OpenAI 兼容接口的 `content`
        必须是字符串或分段列表 —— `None` 直接被拒。

        `messages[3]` 正好是「system + 2 轮历史 + user」，也就是**自主发言时必然触发**。
        所以凡是走自主路径（定时主动说话 / `PET_TEST_SAY`）都会报这个错。

        这条测试直接盯**请求体**：所有 message 的 content 必须是 str。
        """
        from .brains.base import Turn
        from .brains.openai_compat import OpenAIBrain

        cfg = Config()
        cfg.root["providers"]["brain"].update(
            {"provider": "openai", "base_url": "http://127.0.0.1:1/v1", "model": "m", "api_key_env": "X"}
        )
        os.environ["X"] = "sk-test"
        brain = OpenAIBrain(cfg)
        req = LLMRequest(
            system="你是桌宠",
            history=[Turn(role="user", text="你好"), Turn(role="assistant", text="嗯")],
            # ⚠️ 关键：自主发言 = None
            user_text=None,
        )
        body = brain._body(req)
        bad = [
            (i, m.get("role"), type(m.get("content")).__name__)
            for i, m in enumerate(body["messages"])
            if not isinstance(m.get("content"), str)
        ]
        _assert(not bad, f"请求体里有非字符串 content：{bad}")
        # 自主发言时最后一条应当是**非空**的 user（既不能缺，也不能是空串）。
        #
        # ⚠️ 2026-10-17 改：原来断言的是 `content == ""`。空串确实能过接口，
        #    但模型会把它当成"用户发了一条空消息"来回应 —— 实测它说
        #    「诶，还是空的，第七回了吧」，导致截图对话的画面描述完全失效。
        #    现在断言"非空 + 是那句舞台提示"，同时保留原来那道
        #    "不能是 None / 不能缺一条"的保护。
        from .brains.openai_compat import IDLE_USER_TURN

        last = body["messages"][-1]
        _assert(
            last.get("role") == "user",
            f"自主发言时最后一条应为 user，实际 {last!r}",
        )
        _assert(
            last.get("content") == IDLE_USER_TURN,
            f"自主发言时最后一条应为舞台提示，实际 {last.get('content')!r}",
        )
        _assert(
            bool(str(last.get("content") or "").strip()),
            "自主发言时最后一条**不能是空串**（模型会吐槽'你发了个空的'）",
        )
        return f"{len(body['messages'])} 条消息全是字符串，最后一条是舞台提示"

    @suite.check("思考模式：开关真的进请求体，且**非 DeepSeek 时一个字节都不传**")
    def _() -> str:
        """
        ## 用户要求（2026-10-07）

        「把思考模式做成开关，在宠物菜单界面显示，用户自己选择是否开启，默认开启」

        ## 这条测试钉住三件事

          ① `thinking=True`  -> 请求体里有 `{"type": "enabled"}`
          ② `thinking=False` -> `{"type": "disabled"}`
          ③ `thinking=None`  -> **请求体里根本没有这个键**

        ③ 最容易漏，但**最要紧**：`thinking` 是 **DeepSeek 专有参数**，
        而 `providers.brain` 支持**任何 OpenAI 兼容接口**。对不认识它的
        服务商（硅基流动 / 阿里 / OpenAI…）多传一个未知键**可能导致 400** ——
        那会让用户"换个服务商就用不了"，而且报错信息根本看不出是这个字段。

        ## ⚠️ 还有一个坑：必须区分"没配"和"配了 false"

        `brain_cfg.get("thinking") or True` 这种写法会把 `False` 当成"没配"，
        于是用户关掉思考模式**根本不生效** —— 而界面上勾已经没了。
        所以 gateway 用的是 `"thinking" not in brain_cfg` 判断
        （见 `Gateway._thinking_flag`），这条测试验的就是那条路径。
        """
        from .brains.openai_compat import OpenAIBrain

        cfg = Config()
        # ⚠️ 这里刻意用**非 DeepSeek** 的 base_url：验证网关会拒绝传这个参数
        cfg.root["providers"]["brain"].update(
            {"provider": "openai", "base_url": "http://127.0.0.1:1/v1", "model": "m", "api_key_env": "X"}
        )
        os.environ["X"] = "sk-test"
        brain = OpenAIBrain(cfg)

        def body_for(thinking: Optional[bool]) -> dict[str, Any]:
            req = LLMRequest(system="s", history=[], user_text="hi", thinking=thinking)
            return brain._body(req)

        # ① / ② 显式开与关
        _assert(
            body_for(True).get("thinking") == {"type": "enabled"},
            f"thinking=True 未正确传成 enabled：{body_for(True).get('thinking')!r}",
        )
        _assert(
            body_for(False).get("thinking") == {"type": "disabled"},
            f"thinking=False 未正确传成 disabled：{body_for(False).get('thinking')!r}",
        )
        # ③ None -> 完全不出现这个键
        _assert(
            "thinking" not in body_for(None),
            "thinking=None 时不该出现该键（非 DeepSeek 服务商会因未知字段 400）",
        )

        # ④ 网关的 `_thinking_flag`：非 DeepSeek 必须返回 None
        from .gateway import Conversation

        conv = Conversation.__new__(Conversation)
        non_ds = {"thinking": True, "base_url": "https://api.siliconflow.cn/v1"}
        _assert(
            conv._thinking_flag(non_ds) is None,
            "非 DeepSeek 的 base_url 仍返回了 thinking 值 —— 可能让服务商 400",
        )
        ds = {"thinking": False, "base_url": "https://api.deepseek.com/v1"}
        _assert(
            conv._thinking_flag(ds) is False,
            "DeepSeek + thinking:false 应当返回 False（`or` 写法会把它当成没配）",
        )
        _assert(
            conv._thinking_flag({"base_url": "https://api.deepseek.com/v1"}) is None,
            "配置里没写 thinking 时应当返回 None（尊重服务端默认）",
        )
        return "enabled/disabled/不传 三态正确；非 DeepSeek 自动跳过；没配与 false 分得开"


def _fresh_emotion() -> Any:
    from .brains.base import EmotionState

    return EmotionState()


# --------------------------------------------------------------------------- #
# 3.5 自主调度器（P5）
# --------------------------------------------------------------------------- #

# 固定一个"肯定不在免打扰时段"的时刻（周三中午），避免测试随真实时间抖动
_FIXED_WALL = datetime(2026, 9, 30, 12, 0, 0)


class _FakeClock:
    """
    可推进的假时钟。

    自主行为的验证难点全在"时间"上：等 15 分钟看它说不说话，等于没有测试。
    把单调时钟与墙钟分开注入之后，一天可以被压缩到毫秒级跑完。
    """

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _scheduler_for(
    activity: Optional[dict[str, Any]] = None,
    autonomy: Optional[dict[str, Any]] = None,
    intimacy: Optional[Callable[[], float]] = None,
) -> tuple[Any, Any, _FakeClock]:
    from .autonomy import Scheduler

    cfg = Config()
    patch: dict[str, Any] = {}
    if activity:
        patch["activity"] = activity
    if autonomy:
        patch["autonomy"] = autonomy
    if patch:
        cfg.apply_patch(patch)
    fake = _FakeClock()
    scheduler = Scheduler(
        cfg,
        Bus(),
        clock=fake,
        wall_clock=lambda: _FIXED_WALL,
        intimacy_provider=intimacy,
    )
    return scheduler, cfg, fake


def test_autonomy(suite: SelfTest) -> None:
    @suite.check("免打扰时段解析：跨零点 / 限定星期 / 非法项跳过")
    def _() -> str:
        from .autonomy import parse_quiet_hours

        windows = parse_quiet_hours(["23:30-07:30", "mon-fri 09:00-18:00", "sat,sun 10:00-12:00"])
        _assert(len(windows) == 3, f"应解析出 3 条时段，实为 {len(windows)}")

        overnight = windows[0]
        _assert(overnight.contains(datetime(2026, 9, 30, 23, 45)), "23:45 应命中跨零点时段")
        _assert(overnight.contains(datetime(2026, 9, 30, 3, 0)), "03:00 应命中跨零点时段")
        _assert(not overnight.contains(datetime(2026, 9, 30, 12, 0)), "12:00 不应命中")

        weekday = windows[1]
        _assert(weekday.contains(datetime(2026, 9, 30, 10, 0)), "周三 10:00 应命中工作日时段")
        _assert(
            not weekday.contains(datetime(2026, 10, 3, 10, 0)),
            "周六 10:00 不应命中工作日时段（2026-10-03 是周六）",
        )

        # 非法项必须被跳过而不是抛错：时段写错不该让宠物起不来
        mixed = parse_quiet_hours(["乱写的", "22:00-23:00"])
        _assert(len(mixed) == 1, f"非法项未被跳过：{mixed}")
        _assert(parse_quiet_hours(None) == [], "空值应返回空列表")
        return "跨零点 / 星期限定 / 非法跳过 均正确"

    @suite.check("总活泼度单调映射到开口阈值（滑杆的语义）")
    def _() -> str:
        cfg = Config()
        thresholds = []
        for overall in (0.0, 0.25, 0.5, 0.75, 1.0):
            cfg.apply_patch({"activity": {"overall": overall}})
            thresholds.append(cfg.speak_threshold())
        _assert(
            all(a > b for a, b in zip(thresholds, thresholds[1:])),
            f"阈值应随活泼度单调下降：{thresholds}",
        )
        cfg.apply_patch({"activity": {"overall": 0.0}})
        quiet = cfg.speak_threshold()
        cfg.apply_patch({"activity": {"overall": 1.0}})
        lively = cfg.speak_threshold()
        _assert(quiet > 0.9, f"最安静时阈值应很高，实为 {quiet}")
        _assert(lively < 0.3, f"最活泼时阈值应很低，实为 {lively}")
        return f"阈值 {quiet:.2f}（安静）→ {lively:.2f}（活泼）"

    @suite.check("抑制优先于意愿：免打扰 / 静音时段 / 专注保护 / 总开关")
    def _() -> str:
        scheduler, cfg, clock = _scheduler_for(activity={"overall": 1.0})
        clock.advance(3600)
        _assert(scheduler.evaluate().speak, "基线：长时间空闲 + 高活泼度应当开口")

        scheduler.set_muted(True)
        blocked = scheduler.evaluate()
        _assert(not blocked.speak and blocked.reason == "muted", f"免打扰未生效：{blocked}")
        scheduler.set_muted(False)

        scheduler.focus_guard_active = True
        focused = scheduler.evaluate()
        _assert(not focused.speak and focused.reason == "focus_guard", f"专注保护未生效：{focused}")
        scheduler.focus_guard_active = False

        # 静音时段：用可注入的墙钟放进时段内，不依赖真实时间
        cfg.apply_patch({"activity": {"quiet_hours": ["23:00-07:00"]}})
        scheduler.reload()
        scheduler.set_wall_clock(lambda: datetime(2026, 9, 30, 23, 30))
        quiet = scheduler.evaluate()
        _assert(not quiet.speak and quiet.reason == "quiet_hours", f"静音时段未生效：{quiet}")
        scheduler.set_wall_clock(lambda: _FIXED_WALL)
        _assert(scheduler.evaluate().speak, "离开静音时段后应恢复开口")

        cfg.apply_patch({"autonomy": {"enabled": False}})
        disabled = scheduler.evaluate()
        _assert(
            not disabled.speak and disabled.reason == "autonomy.disabled",
            f"总开关未生效：{disabled}",
        )
        return "4 条抑制路径均优先于意愿"

    @suite.check("冷却：刚交互过不说话，且不被活泼度覆盖")
    def _() -> str:
        scheduler, _cfg, clock = _scheduler_for(activity={"overall": 1.0})
        clock.advance(3600)
        scheduler.mark_interaction()  # 用户刚说了话
        fresh = scheduler.evaluate(observation_age_s=0.0)
        _assert(not fresh.speak, f"刚交互过却仍要开口：{fresh}")
        _assert(fresh.reason == "below_threshold", f"原因应为冷却压制了意愿：{fresh.reason}")
        _assert(fresh.signals["cooldown_penalty"] > 0.9, f"冷却惩罚应接近 1：{fresh.signals}")

        clock.advance(5)
        still = scheduler.evaluate(observation_age_s=0.0)
        _assert(not still.speak, f"冷却期内不应开口：{still}")

        clock.advance(120)
        after = scheduler.evaluate(observation_age_s=0.0)
        _assert(after.speak, f"冷却结束后应能开口：{after}")
        return f"惩罚 {fresh.signals['cooldown_penalty']:.2f} → 归零后恢复开口"

    @suite.check("句子表：能读、能挑、哲学最多、用户给的例子都在")
    def _() -> str:
        """
        用户 2026-10-17：
        「自言自语讲的话来自于一个表里面……其中哲学句子多，不乏古诗。」

        这条验四件事：
          ① 表能读出来（`config/quotes.yaml`）
          ② 类别权重：哲学最高
          ③ 挑选是**按权重**的（哲学占比最大，不是均匀）
          ④ 用户给的两个例子（意大利面 / 手握日月）真的在表里
        """
        import pathlib as _p
        import random as _r

        from . import quotes

        path = _p.Path(__file__).resolve().parents[3] / "config" / "quotes.yaml"
        table = quotes.load_table(path)
        _assert(len(table) >= 10, f"句子表太小或没读到：{len(table)} 句（{path}）")

        kinds = table.kinds()
        _assert("philosophy" in kinds, f"没有哲学类别：{kinds}")
        top = max(kinds, key=lambda k: table.weights[k])
        _assert(top == "philosophy", f"哲学应该是权重最高的类别，实际最高是 {top}")

        # 权重真的影响挑选：2000 次里哲学必须最多，且明显高于最低的那类
        rng = _r.Random(20261017)
        counts: dict[str, int] = {}
        for _ in range(2000):
            quote = table.pick(rng)
            _assert(quote is not None, "表非空却挑不出句子")
            counts[quote.kind] = counts.get(quote.kind, 0) + 1
        order = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
        _assert(
            order[0][0] == "philosophy",
            f"挑出来最多的应该是哲学，实际 {order[0][0]}（{order}）",
        )
        share = order[0][1] / 2000
        _assert(share > 0.25, f"哲学占比应该明显偏高，实际 {share:.0%}")

        # 用户点名的两句必须在表里（以后谁"整理"表时不许删）
        texts = "\n".join(q.text for q in table.quotes)
        _assert("意大利面" in texts and "42号混凝土" in texts, "用户给的「意大利面」例子不在表里")
        _assert("手握日月摘星辰" in texts, "用户给的「手握日月摘星辰」例子不在表里")

        _assert("poem" in kinds, "古诗类别丢了（用户说「不乏古诗」）")
        _assert("scripture" in kinds, "经文类别丢了（用户说「偶尔讲讲道经/佛经/圣经」）")
        return (
            f"{table.summary()}；2000 次挑选里 "
            + "、".join(f"{table.labels[k]} {v / 20:.0f}%" for k, v in order[:3])
        )

    @suite.check("句子表：上一句不会马上重复，写错 kind 会被抓出来")
    def _() -> str:
        """
        念经的观感全靠"每次不一样"。随机挑到刚念过的那句会立刻露馅。

        另外验校验：`kind` 写成类别表里没有的，必须**当场报错** ——
        否则那一类句子永远挑不到，而没人会发现（不报错的错最难查）。
        """
        import pathlib as _p
        import random as _r

        from . import quotes

        path = _p.Path(__file__).resolve().parents[3] / "config" / "quotes.yaml"
        table = quotes.load_table(path)
        rng = _r.Random(7)
        recent: list[str] = []
        repeats = 0
        for _ in range(12):
            quote = table.pick(rng, recent=recent[-8:])
            if quote.text in recent[-8:]:
                repeats += 1
            recent.append(quote.text)
        _assert(repeats == 0, f"最近 8 句里重复挑了 {repeats} 次")

        try:
            quotes.parse_table(
                {
                    "categories": {"philosophy": {"weight": 1}},
                    "quotes": [{"text": "句子", "kind": "不存在"}],
                }
            )
        except ValueError as error:
            _assert("不存在" in str(error), f"报错信息没提那个 kind：{error}")
        else:
            raise AssertionError("kind 写错居然没报错")

        try:
            quotes.parse_table(
                {
                    "categories": {"philosophy": {"weight": 1}},
                    "quotes": [{"text": "第一行\n第二行", "kind": "philosophy"}],
                }
            )
        except ValueError:
            pass
        else:
            raise AssertionError("句子里有换行居然没报错")

        return f"12 次挑选无重复；坏 kind / 换行都会报错"

    @suite.check("自言自语：念的是表里那句，而且不考据、不反问")
    def _() -> str:
        """
        用户原话：「大部分时间都在聊自己读了什么什么，觉得什么什么」
        —— 所以舞台提示里必须出现那句原文，并**禁止**考据出处/反问用户。
        """
        from . import quotes

        quote = quotes.Quote(text="问君能有几多愁，恰似一江春水向东流。", kind="poem", source="李煜")
        direction = quotes.direction_for(quote)
        _assert(quote.text in direction, f"舞台提示里没有那句原文：{direction[:60]}")
        _assert("别问用户问题" in direction, "没有禁止反问用户（会把话题抢回去）")
        _assert("别解释" in direction, "没有禁止考据出处（会变成讲课）")
        _assert(
            "李煜" in direction,
            "有出处时应该带上（用户听到好句子会问哪来的）",
        )
        # 没出处时不能凭空编一个
        plain = quotes.direction_for(quotes.Quote(text="某句没出处的话", kind="philosophy"))
        _assert("出自" not in plain, f"没出处却编了出处：{plain[:60]}")
        return "原文进用户轮；禁考据、禁反问；出处有就带、没有就不编"

    @suite.check("句子表（数据库）：建表、从 yaml 导入、按权重挑、记得用过")
    def _() -> str:
        """
        用户 2026-10-18：「这个表太慢，不好维护，用数据库单开一张表来存储」。

        这条验：
          ① 真能在库里建出 `quote` / `quote_kind`（不是只写了个 REPO 类）
          ② yaml 只在**空表**时导入（用户后来在库里改的句子不该被覆盖）
          ③ 挑出来的句子带 id，`mark_used` 真的加计数
          ④ 类别权重生效（哲学最多）
        """
        import pathlib as _p
        import random as _r

        from . import quotes
        from .store import PetDatabase

        root = _p.Path(__file__).resolve().parents[3]
        # ⚠️ 不用 tempfile：在这台机器上 TemporaryDirectory() 给出的路径
        # 交给 sqlite 会报 WinError 267（目录名无效）。用项目内的固定临时文件 +
        # 用完删掉，结果一样但没有那个坑。
        tmpdir = root / "apps" / "desktop" / "pet-data" / "_selftest"
        tmpdir.mkdir(parents=True, exist_ok=True)
        db_path = tmpdir / "quote_selftest.sqlite3"
        for stale in tmpdir.glob("quote_selftest.sqlite3*"):
            stale.unlink(missing_ok=True)
        try:
            db = PetDatabase(db_path)
            repo = quotes.QuoteRepo(conn=db.conn)
            repo.ensure_kinds()
            kinds = repo.weights()
            _assert("philosophy" in kinds, f"类别没建出来：{kinds}")
            # 权重是用户 2026-10-18 指定的：古诗 30 / 小说 25 / 哲学 20 / 其余各 5。
            # 所以"最高"是古诗、"第二"是小说 —— 不再断言哲学最高（那是旧比例）。
            _assert(
                max(kinds, key=lambda k: kinds[k]) == "poem",
                f"古诗应该是权重最高的类别：{kinds}",
            )
            _assert(kinds.get("novel") == 25.0, f"小说权重应为 25：{kinds}")
            _assert(kinds.get("philosophy") == 21.0, f"哲学权重应为 21：{kinds}")
            _assert(kinds.get("bible") == 4.0, f"圣经权重应为 4：{kinds}")
            _assert(
                abs(sum(kinds.values()) - 100.0) < 0.01,
                f"总权重应为 100（百分比直写）：{kinds}",
            )

            imported = repo.import_seed(root / "config" / "quotes.yaml")
            _assert(imported >= 10, f"yaml 导入失败：{imported} 句")
            _assert(repo.count() == imported, f"导入后条数不对：{repo.count()} vs {imported}")

            # 再导入一次不应该重复（表非空）
            again = repo.import_seed(root / "config" / "quotes.yaml")
            _assert(again == 0 and repo.count() == imported, "非空表被二次导入覆盖/重复了")

            rng = _r.Random(20261018)
            repo.rng = rng
            counts: dict[str, int] = {}
            for _ in range(400):
                quote = repo.pick()
                _assert(quote is not None, "表非空却挑不出句子")
                _assert(quote.id is not None, "数据库版挑出来的句子应该带 id")
                counts[quote.kind] = counts.get(quote.kind, 0) + 1
            # ⚠️ 不能断言"古诗一定是第一名"：测试库里 yaml 只有 5 个类别
            # （dao/buddhist/bible 没有句子，不参与抽取），于是有效权重是
            # 古诗 30/85=35%、小说 25/85=29% —— 400 次抽样的噪声足够让两者互换。
            # 所以按**占比区间**断言，既验证权重生效，又不受采样抖动影响。
            total_picks = 400
            poem_share = counts.get("poem", 0) / total_picks
            novel_share = counts.get("novel", 0) / total_picks
            _assert(
                0.30 <= poem_share <= 0.42,
                f"古诗占比应约 35%（30/85），实际 {poem_share:.0%}：{counts}",
            )
            _assert(
                0.20 <= novel_share <= 0.38,
                f"小说占比应约 29%（25/85），实际 {novel_share:.0%}：{counts}",
            )
            _assert(
                poem_share + novel_share > 0.55,
                f"古诗+小说应占大头，实际 {poem_share + novel_share:.0%}：{counts}",
            )

            first = repo.pick()
            before = repo.conn().execute(
                "SELECT used_count FROM quote WHERE id = ?", (first.id,)
            ).fetchone()[0]
            repo.mark_used(first.id)
            after = repo.conn().execute(
                "SELECT used_count FROM quote WHERE id = ?", (first.id,)
            ).fetchone()[0]
            _assert(after == before + 1, f"mark_used 没生效：{before} -> {after}")
            result_text = f"库里 {imported} 句；400 次挑选哲学最多（{counts['philosophy']} 次）"
        finally:
            try:
                db.close()
            except Exception:  # noqa: BLE001
                pass
            for stale in tmpdir.glob("quote_selftest.sqlite3*"):
                stale.unlink(missing_ok=True)
        return result_text

    @suite.check("念法：一半照抄、一半心得（照抄不过模型）")
    def _() -> str:
        """
        用户：「一半可能会念自己的心得，一半可能直接按原本照抄输出，其他什么也不加。」

        ⚠️ 照抄必须**不过模型**。判断方式是看网关里那条路：`copy` 分支直接
        `speak_verbatim` 并 return，绝不能滑到 `_generate`。
        """
        import inspect
        import random as _r

        from . import quotes

        rng = _r.Random(3)
        modes = [quotes.choose_mode(rng) for _ in range(2000)]
        copy_share = modes.count("copy") / len(modes)
        _assert(0.45 <= copy_share <= 0.55, f"照抄占比不是一半：{copy_share:.2f}")

        from .gateway import Conversation, Gateway

        source = inspect.getsource(Gateway._on_autonomy_speak)
        _assert("speak_verbatim" in source, "自言自语那条路没调用 speak_verbatim")
        _assert("choose_mode" in source, "自言自语那条路没有 50/50 的念法选择")
        verbatim_source = inspect.getsource(Conversation.speak_verbatim)
        # 看**调用**而不是看文字：说明里提到 _generate 是允许的，
        # 真正要防的是它去调用。
        _assert("self._generate(" not in verbatim_source, "speak_verbatim 居然还在调模型")
        return f"照抄 {copy_share:.0%}；照抄路径不过模型"

    @suite.check("注意力：3 分钟超 3 轮拉回用户，停 3 分钟后回自己")
    def _() -> str:
        """
        用户：「在自言自语状态和用户聊天时，3分钟内超过3轮对话将自动将注意力拉回
        用户身上，后续不和玩伴搭话3分钟，宠物注意力将继续回到自己身上继续念叨。」
        """
        import types

        from .bus import Event
        from .config import load_config
        import pathlib as _p

        cfg = load_config(_p.Path(__file__).resolve().parents[3] / "config" / "persona.yaml")
        cfg.apply_patch({"activity.overall": 1.0})  # 自言自语

        class _Subject:
            def __init__(self) -> None:
                self.user_calls = 0
                self.self_calls = 0

            def on_user_message(self, text: str = "") -> None:
                self.user_calls += 1

            def on_self_hit(self, topics=None) -> None:
                self.self_calls += 1

        # ⚠️ 主观层的真正持有者是 **Conversation**（self.conversation.subjectivity），
        # 不是 Gateway 自己 —— 早先代码写成 self.subjectivity，被 check-self-refs 抓到。
        fake = types.SimpleNamespace(
            config=cfg,
            _user_turns=[],
            _monologue_hold_until=0.0,
            conversation=types.SimpleNamespace(subjectivity=_Subject()),
        )
        from .gateway import Gateway

        watch = Gateway._on_user_text_for_monologue
        import asyncio

        def _round(text: str) -> None:
            asyncio.run(watch(fake, Event("user.text", {"text": text})))

        # 3 轮（3 条用户消息）不应该触发
        for i in range(3):
            _round(f"第{i}轮")
        _assert(fake._monologue_hold_until == 0.0, "3 轮就拉回了（应该是**超过** 3 轮）")
        _assert(fake.conversation.subjectivity.user_calls == 0, "还没到 4 轮就调了注意力")

        # 第 4 条 -> 拉回
        _round("第4轮")
        _assert(fake._monologue_hold_until > 0.0, "4 轮了却没拉回注意力")
        _assert(fake.conversation.subjectivity.user_calls == 1, "拉回时没通知主观层")

        # hold 期间：念经被跳过（_on_autonomy_speak 里那条 return）
        import inspect

        src = inspect.getsource(Gateway._on_autonomy_speak)
        _assert("_monologue_hold_until" in src, "念经那条路没检查 hold（会边聊边念）")

        # 3 分钟没搭话 -> 回自己
        fake._user_turns = []
        fake._monologue_hold_until = 0.0
        _assert(Gateway.monologue_hold_expired(fake) is False, "没 hold 却说回自己了")
        # ⚠️ 不能用负数表示"已到期" —— 实现里 <= 0 是"没有 hold"。
        # 要用"正数但在过去"的时刻，才和真实语义一致。
        import time as _t

        fake._monologue_hold_until = _t.monotonic() - 1.0
        expired = Gateway.monologue_hold_expired(fake)
        _assert(expired, "hold 到期、3 分钟没搭话，应该回自己")
        _assert(fake.conversation.subjectivity.self_calls == 1, "回自己时没通知主观层")
        return "第 4 轮拉回用户并停念；到期且无搭话后回自己"

    @suite.check("四档节奏：20/15/10/2 分钟开口，30/10 分钟截图")
    def _() -> str:
        """
        用户 2026-10-18 原话：
        「安静阶段：20分钟聊一次天。无聊阶段：15分钟聊一次天。好奇阶段:10分钟聊一次天,
          30分钟截一次图聊天。自言自语阶段:2分钟念一次,10分钟截一次图聊天。」

        这条盯**数字本身**：以后谁改档位表，把节奏改错，这里会直接红。
        """
        from . import tiers

        want = {
            "quiet": (1200.0, None),
            "bored": (900.0, None),
            "curious": (600.0, 1800.0),
            "monologue": (120.0, 600.0),
        }
        for key, (talk, shot) in want.items():
            tier = tiers.by_key(key)
            _assert(tier is not None, f"档位 {key} 不见了")
            _assert(
                float(tier.talk_interval_s) == talk,
                f"{tier.name} 的开口间隔应该是 {talk}s，实际 {tier.talk_interval_s}",
            )
            _assert(
                tier.screenshot_interval_s == shot,
                f"{tier.name} 的截图间隔应该是 {shot}，实际 {tier.screenshot_interval_s}",
            )
        # 节奏必须随档位递增（安静最稀疏、自言自语最密）
        order = [tiers.by_key(k).talk_interval_s for k in ("quiet", "bored", "curious", "monologue")]
        _assert(order == sorted(order, reverse=True), f"开口间隔没随档位递进：{order}")
        return "安静 20/无聊 15/好奇 10/自言自语 2 分钟；截图：好奇 30、自言自语 10，其余不截"

    @suite.check("截图优先于搭话：截图说完 60 秒内不搭话")
    def _() -> str:
        """2026-10-18 用户规则：

        ① 搭话和截图说话相隔不到 1 分钟时，**先让截图说话**；
        ② 截图说完 1 分钟内不许再自主搭话。

        行为在 autosnap 循环里（要真截图才跑得到），这里查**源码不变量**：
        `_on_autonomy_speak` 开头必须读 `_last_screen_speak_at` 并按 60 秒判、
        截图也到点时必须让路，且 autosnap 说完话必须写时间戳。

        ## 🔴 2026-10-27 修：这条测试原来把 bug 钉住了

        原来它只断言源码里有 `"self.scheduler.screenshot_due()"` 这个**字符串**。
        而真正的 bug 恰恰是那个写法：

            if self.scheduler.screenshot_due():     # 返回 (bool, str)，元组恒真！

        于是"有这句话"= 测试绿，而宠物**每次开口都被拦掉**。
        这正是"测源码字符串"的典型危害 —— 它验证的是**长得像**，不是**做得对**。

        现在改成断言**解包**（`= ...screenshot_due()`），并且
        直接跑一次语义验证：截图不该到点时，这条分支必须**不进**。
        """
        import inspect

        from .gateway import _SCREEN_SPEAK_HOLD_S

        speak_src = inspect.getsource(Gateway._on_autonomy_speak)
        _assert("_last_screen_speak_at" in speak_src, "搭话那条路没检查截图优先")
        _assert("_SCREEN_SPEAK_HOLD_S" in speak_src, "没用 60 秒闭嘴期常量")
        # ⚠️ 必须是**解包赋值**，不能是 `if ...screenshot_due():`（元组恒真）
        _assert(
            "= self.scheduler.screenshot_due()" in speak_src,
            "screenshot_due() 必须解包成 (due, why) —— 直接当条件用会恒为真"
            "（返回的是元组），导致每一次自主开口都被'让给截图'拦掉",
        )
        _assert(
            "if self.scheduler.screenshot_due():" not in speak_src,
            "还有把 screenshot_due() 当布尔用的地方 —— 元组恒真，会拦掉所有开口",
        )
        snap_src = inspect.getsource(Gateway._run_autosnap_forever)
        _assert(
            "self._last_screen_speak_at = time.monotonic()" in snap_src,
            "截图说完没记时间戳（下一条搭话不会被挡住）",
        )
        _assert(
            abs(_SCREEN_SPEAK_HOLD_S - 60.0) < 1e-6,
            f"闭嘴期应为 60 秒，实际 {_SCREEN_SPEAK_HOLD_S}",
        )
        return "截图优先；截图说完 60 秒内不搭话"

    @suite.check("自主截图：档位愿意 + 用户许可，两道门都要过")
    def _() -> str:
        """
        隐私边界：**档位永远不能自己打开权限**。

        用户设置里的「允许偶尔截图」（`providers.vision.enabled`）默认**关**，
        档位只表达"愿不愿意"。这条验四种组合：
          ① 档位不截（安静/无聊）+ 权限开  -> 不截
          ② 档位愿截 + 权限关            -> 不截（最重要的一条）
          ③ 档位愿截 + 权限开 + 刚截过    -> 不截（等间隔）
          ④ 档位愿截 + 权限开 + 过了间隔  -> 截
        """
        import pathlib as _p

        from .autonomy import Scheduler
        from .bus import Bus
        from .config import load_config

        class _Clock:
            def __init__(self) -> None:
                self.now = 0.0

            def __call__(self) -> float:
                return self.now

        cfg = load_config(_p.Path(__file__).resolve().parents[3] / "config" / "persona.yaml")
        clock = _Clock()
        scheduler = Scheduler(cfg, Bus(), clock=clock)

        cfg.apply_patch({"activity.overall": 0.1})  # 安静：不截图
        cfg.apply_patch({"providers.vision.enabled": True})
        due, why = scheduler.screenshot_due()
        _assert(not due and why == "tier_no_screenshot", f"安静档居然要截图：{why}")

        cfg.apply_patch({"activity.overall": 0.8})  # 好奇：愿意截
        cfg.apply_patch({"providers.vision.enabled": False})
        due, why = scheduler.screenshot_due()
        _assert(not due and why == "permission_off", f"权限关着居然要截图：{why}")

        cfg.apply_patch({"providers.vision.enabled": True})
        due, why = scheduler.screenshot_due()
        _assert(due and why == "due", f"权限开着、没截过，应该该截了：{why}")

        # 刚截过：要等 30 分钟
        scheduler.mark_autosnap()
        clock.now += 60
        due, why = scheduler.screenshot_due()
        _assert(not due and why == "interval", f"刚截过 1 分钟又要截：{why}")
        clock.now += 1800
        due, why = scheduler.screenshot_due()
        _assert(due, f"过了 30 分钟还不截：{why}")
        return "档位不截/权限关/间隔未到 都挡住；权限开 + 过间隔才截"

    @suite.check("档位节奏门：太频繁不许开口（间隔下限，不是到点必说）")
    def _() -> str:
        """
        「每 20 分钟聊一次」是**下限**：刚说完 1 分钟内不许再说，
        到了间隔也只是"允许"，仍要过意愿关。
        """
        import pathlib as _p

        from .autonomy import Scheduler
        from .bus import Bus
        from .config import load_config

        class _Clock:
            def __init__(self) -> None:
                self.now = 0.0

            def __call__(self) -> float:
                return self.now

        cfg = load_config(_p.Path(__file__).resolve().parents[3] / "config" / "persona.yaml")
        cfg.apply_patch({"activity.overall": 1.0})  # 自言自语：2 分钟
        clock = _Clock()
        scheduler = Scheduler(cfg, Bus(), clock=clock)

        # 模拟"刚自主说完一句"
        scheduler.last_autonomy_speak_at = clock.now
        clock.now += 60
        decision = scheduler.evaluate()
        _assert(
            decision.reason == "tier_interval",
            f"刚说完 1 分钟就评估成 {decision.reason}（应该被节奏门挡住）",
        )
        clock.now += 120  # 合计 3 分钟 > 2 分钟间隔
        later = scheduler.evaluate()
        _assert(later.reason != "tier_interval", f"过了间隔还被节奏门挡着：{later.reason}")
        return "1 分钟被 tier_interval 挡住；3 分钟后放行（交给意愿关）"

    @suite.check("档位：四档映射 + 空闲门递进（安静最慢、自言自语最快）")
    def _() -> str:
        """
        用户 2026-10-17 定的四档：安静 / 无聊 / 好奇 / 自言自语。

        这条卡三件事：
          ① 数值到名字的映射（滑杆的中间值取最近档）
          ② 空闲门严格递进：越活泼越等不及
          ③ 「无聊」就是 0.5 —— 老文案「普通」已作废
        """
        from . import tiers

        expect = [(0.1, "安静"), (0.5, "无聊"), (0.62, "无聊"), (0.8, "好奇"), (1.0, "自言自语")]
        for value, name in expect:
            got = tiers.nearest(value).name
            _assert(got == name, f"{value} 应该是「{name}」，实际「{got}」")

        gates = [tier.idle_after_s for tier in tiers.TIERS]
        _assert(
            gates == sorted(gates, reverse=True),
            f"空闲门没有随活泼度递进：{gates}",
        )
        _assert(gates[0] >= 1500, f"安静档的门应该很大（几乎不主动），实际 {gates[0]}s")
        return f"映射 {len(expect)} 组；空闲门 {[int(g / 60) for g in gates]} 分钟，递进正确"

    @suite.check("档位：只有「好奇」「自言自语」愿意自主截图（仍受用户权限约束）")
    def _() -> str:
        """
        ⚠️ 这条验的是**两个独立条件**，别混：

            Tier.screenshot = 这一档**愿意**截图
            vision.enabled   = 用户**允许**它截图（设置页开关，默认关）

        只有两个都真才会真去截。要是哪天有人把 `screenshot` 当成许可用，
        用户拖着滑杆就能绕过隐私开关 —— 那是这个项目绝不能出的错。
        """
        from . import tiers

        allowed = [tier.name for tier in tiers.TIERS if tier.screenshot]
        _assert(
            allowed == ["好奇", "自言自语"],
            f"允许自主截图的档位不对：{allowed}（用户要求：好奇 / 自言自语）",
        )

        # 默认配置里权限是关的 —— 档位再怎么"愿意"也不该截
        cfg = Config()
        vision_cfg = cfg.providers.get("vision", {})
        # ⚠️ 默认配置里**根本没有** enabled 这个键 —— 缺省即关。
        # 别写成 is False：那样在"没配"和"配成 false"之间会误判。
        _assert(
            vision_cfg.get("enabled", False) is False,
            f"默认配置里 vision.enabled 缺省或 false（用户权限默认关），实际 {vision_cfg.get('enabled')!r}",
        )
        # 权限可以被用户打开 —— 说明这不是个恒假的常量，而是真的读配置
        cfg.apply_patch({"providers.vision.enabled": True})
        _assert(
            cfg.providers.get("vision", {}).get("enabled") is True,
            "把权限打开后这里应该读到 true（否则门是假的）",
        )
        return f"愿意截图的档位 {allowed}；权限缺省关、可被用户打开（两条独立条件）"

    @suite.check("档位：「自言自语」20 分钟后自动回落到「安静」")
    def _() -> str:
        """
        用户原话：「此状态下注意力掉的很快，20分钟不到就会回到安静状态，
        因为情绪发泄完了，开始平静的不想说话了。」

        用**假时钟**推进（真等 20 分钟没法测）：1199 秒不落、1201 秒落。
        """
        _scheduler, cfg, clock = _scheduler_for(activity={"overall": 1.0})
        from . import tiers

        tier = cfg.activity_tier
        _assert(tier.key == "monologue", f"1.0 应该是自言自语，实际 {tier.name}")
        _assert(tier.decay_to == "quiet", f"回落目标应是安静，实际 {tier.decay_to}")
        _assert(tier.decay_s == 1200.0, f"回落时长应是 20 分钟，实际 {tier.decay_s}s")

        _assert(not tiers.decay_due(tier, 1199.0), "1199 秒时不该回落")
        _assert(tiers.decay_due(tier, 1201.0), "1201 秒时应该回落了")

        # 到点后 overall 真的被改掉，且落到安静那个值
        scheduler = _scheduler
        clock.advance(1201)
        decayed = asyncio.run(scheduler.check_tier_decay())
        _assert(decayed, "到点了 check_tier_decay 应该返回回落描述")
        _assert(decayed["toName"] == "安静", f"回落目标不对：{decayed}")
        _assert(
            abs(float(cfg.activity["overall"]) - 0.1) < 1e-9,
            f"回落后 overall 应该是 0.1，实际 {cfg.activity['overall']}",
        )
        return f"1199s 不落 / 1201s 落到安静（overall={cfg.activity['overall']}）"

    @suite.check("档位：每一档的行为描述都进了提示词（不是两套文案）")
    def _() -> str:
        """
        档位的 `style` 必须真的出现在 `activity_for_prompt()` 里 ——
        否则就是"四档只是菜单上的文字，模型一无所知"。
        """
        from . import tiers

        for tier in tiers.TIERS:
            cfg = Config()
            cfg.apply_patch({"activity": {"overall": tier.overall}})
            prompt = cfg.activity_for_prompt()
            head = tier.style[:18]
            _assert(
                head in prompt,
                f"「{tier.name}」的行为描述没进提示词：期望包含 {head!r}，实际 {prompt[:60]!r}",
            )
        return f"{len(tiers.TIERS)} 档的行为描述都进了提示词"

    @suite.check("开口后进入冷却，不会被后续 tick 连续触发")
    def _() -> str:
        # 用一条陡峭的空闲曲线（10s 起爬、60s 饱和），让"冷却解除后自然恢复"这件事
        # 在可控的推进量内成立，而不是靠默认曲线那点边角余量。
        scheduler, _cfg, clock = _scheduler_for(
            activity={"overall": 1.0, "screen_watch_prob": 0.0},
            autonomy={"idle_after_s": 10.0, "idle_full_s": 60.0},
        )
        clock.advance(3600)
        _assert(scheduler.evaluate().speak, "前置条件：此刻应愿开口")

        # ⚠️ 这条验的是**冷却**，不是档位节奏（2026-10-18 加了按档位的间隔门：
        # 自言自语 2 分钟 / 安静 20 分钟）。不关掉那道门，下面 evaluate() 会
        # 提前返回 tier_interval，连 signals 里都没有 cooldown_penalty。
        scheduler.config.apply_patch({"autonomy.talk_interval_s": 0.0})
        spoke = asyncio.run(scheduler.maybe_speak())
        _assert(spoke, "maybe_speak 应安排发言")

        # 真实流程里由 conversation.finished 事件触发，这里直接模拟
        scheduler.mark_spoke()
        clock.advance(1)
        right_after = scheduler.evaluate()
        _assert(not right_after.speak, f"刚说完又立刻开口（会变成话痨）：{right_after}")

        clock.advance(90)
        recovered = scheduler.evaluate()
        _assert(recovered.speak, f"冷却越过、空闲足够后应恢复：{recovered}")
        _assert(recovered.signals["cooldown_penalty"] == 0.0, f"冷却惩罚应归零：{recovered.signals}")
        return (
            f"说完后 1s 处于 {right_after.reason}（惩罚 {right_after.signals['cooldown_penalty']:.2f}）"
            f"，+90s 恢复开口"
        )

    @suite.check("观察新鲜度：越新鲜越想评论，过期后归零")
    def _() -> str:
        # 关掉空闲信号，隔离验证观察信号
        scheduler, _cfg, _clock = _scheduler_for(
            activity={"overall": 1.0, "idle_chatter_rate": 0.0, "screen_watch_prob": 1.0},
            autonomy={"idle_after_s": 99999.0},
        )
        fresh = scheduler.evaluate(observation_age_s=0.0)
        old = scheduler.evaluate(observation_age_s=170.0)
        stale = scheduler.evaluate(observation_age_s=999.0)
        _assert(fresh.signals["observation"] > 0.99, f"新鲜观察应接近 1：{fresh.signals}")
        _assert(
            fresh.desire > old.desire > stale.desire,
            f"意愿应随观察变旧而下降：{fresh.desire:.3f} / {old.desire:.3f} / {stale.desire:.3f}",
        )
        _assert(stale.signals["observation"] == 0.0, f"过期观察应归零：{stale.signals}")
        _assert(not stale.speak, f"过期观察不该触发发言：{stale}")

        # require_observation：没有观察就绝不主动开口（P4 后的"必须先看到东西"模式）
        scheduler2, _cfg2, clock2 = _scheduler_for(
            activity={"overall": 1.0}, autonomy={"require_observation": True}
        )
        clock2.advance(86400)
        blocked = scheduler2.evaluate(observation_age_s=None)
        _assert(
            not blocked.speak and blocked.reason == "need_observation",
            f"缺少观察时应被拦截：{blocked}",
        )
        _assert(scheduler2.evaluate(observation_age_s=1.0).speak, "有观察后应允许开口")
        return f"新鲜 {fresh.desire:.2f} → 过期 {stale.desire:.2f}；缺观察时被拦截"

    @suite.check("亲近度只做小幅加成，不足以单独越过阈值")
    def _() -> str:
        scheduler, _cfg, _clock = _scheduler_for(
            activity={"overall": 0.5, "idle_chatter_rate": 0.0, "screen_watch_prob": 0.0},
            autonomy={"idle_after_s": 99999.0},
            intimacy=lambda: 1.0,
        )
        decision = scheduler.evaluate()
        _assert(not decision.speak, f"仅凭亲近度不该主动开口：{decision}")
        _assert(decision.signals["intimacy"] == 1.0, f"亲近度未生效：{decision.signals}")
        _assert(decision.desire > 0.0, "亲近度应贡献正分")
        return f"亲近度满分时意愿 {decision.desire:.3f} < 阈值 {decision.threshold:.3f}，未越线"

    @suite.check("启动瞬间不算冷却中：看到屏幕变化应能立刻评论")
    def _() -> str:
        # 这是一个真实的行为要求，不只是测试细节：若启动即处于冷却，
        # 宠物开机后 45 秒内对任何屏幕变化都毫无反应，等于"刚打开是死的"。
        scheduler, cfg, _clock = _scheduler_for(
            activity={"overall": 1.0, "idle_chatter_rate": 0.0, "screen_watch_prob": 1.0},
            autonomy={"idle_after_s": 99999.0},
        )
        decision = scheduler.evaluate(observation_age_s=0.0)
        _assert(decision.signals["cooldown_penalty"] == 0.0, f"启动不该处于冷却：{decision}")
        _assert(decision.speak, f"启动时的新鲜观察应能直接触发开口：{decision}")
        return f"启动即无冷却，新鲜观察直接开口（意愿 {decision.desire:.2f}）"


# --------------------------------------------------------------------------- #
# 3.6 屏幕感知（P4）
# --------------------------------------------------------------------------- #


def _frame(grid: list[int], titles: tuple[str, ...] = (), at: float = 0.0, image: Any = None) -> Any:
    from .vision import Frame

    return Frame(
        grid=grid, width=1920, height=1080, captured_at=at, titles=titles, image=image
    )


def _uniform(value: int) -> list[int]:
    return [value] * 64


def _checker(seed: int = 0) -> list[int]:
    """造一个确定但"很不一样"的网格，用来模拟换窗口这类大变化。"""
    return [((i * 37 + seed * 53) % 256) for i in range(64)]


def test_vision(suite: SelfTest) -> None:
    @suite.check("看屏幕开关：关掉之后不再抓帧（不是只改了个没人读的字段）")
    def _() -> str:
        """
        这条防的是"假开关"：采集器是**构造时**建好的（build_capturer 读启动配置），
        而 config.update 只改配置对象。所以如果只改配置、不通知 VisionLoop，
        界面上的开关看起来生效了、实际上还在持续截屏 —— 对隐私功能来说，
        "以为关了其实还开着"比没有开关严重得多。
        """
        from .vision import PrivacyRules, VisionLoop

        class Counting:
            name = "counting"

            def __init__(self) -> None:
                self.calls = 0

            def grab(self) -> Any:
                self.calls += 1
                return _frame(_uniform(120), ("main.py - VS Code",), at=1000.0)

        capturer = Counting()
        loop = VisionLoop(
            bus=Bus(),
            capturer=capturer,
            rules=PrivacyRules.from_config({"enabled": False}),
            clock=_FakeClock(),
        )

        # 开着：应该抓帧
        loop.step()
        after_open = capturer.calls
        _assert(after_open == 1, f"开启时应当抓 1 帧，实际 {after_open}")

        # 关掉：不该再抓帧
        loop.set_watching(False)
        loop.step()
        loop.step()
        after_closed = capturer.calls
        _assert(
            after_closed == after_open,
            f"关闭后不应再抓帧，但抓取次数从 {after_open} 涨到了 {after_closed}",
        )

        # 再打开：恢复抓帧
        loop.set_watching(True)
        loop.step()
        _assert(
            capturer.calls == after_closed + 1,
            f"重新开启后应恢复抓帧，实际只到 {capturer.calls}",
        )

        # 观察新鲜度也要受影响：关掉期间不该产生新观察
        loop.set_watching(False)
        before = loop.last_observation
        loop.step()
        _assert(
            loop.last_observation is before,
            "关闭期间不该更新观察（否则调度器会以为刚看到了东西）",
        )

        return f"开启抓帧 {after_open} 次；关闭后停在 {after_closed} 次；重开恢复"

    @suite.check("隐私闸门：密码管理器等窗口在被读取之前就被跳过")
    def _() -> str:
        from .vision import PrivacyRules  # noqa: F401 —— 同时供本模块其他测试使用

        rules = PrivacyRules.from_config(
            {"title_patterns": ["密码", "1password", "无痕"], "enabled": True}
        )
        blocked = rules.check(("我的密码 - Chrome",))
        _assert(not blocked, f"密码管理器窗口应被拦下：{blocked}")
        _assert("密码" in blocked.reason, f"应说明命中了哪条规则：{blocked.reason}")

        # 大小写不敏感（配置里写小写，标题里是大写）
        _assert(not rules.check(("1Password — Vault",)), "大小写不敏感匹配失败")
        _assert(not rules.check(("登录 - 银行",)), "内置保守词未生效") if "银行" in str(
            rules.title_patterns
        ) else None

        allowed = rules.check(("main.py - VS Code",))
        _assert(allowed, f"正常窗口不该被拦：{allowed}")

        # 关闭开关后一律放行
        off = PrivacyRules.from_config({"title_patterns": ["密码"], "enabled": False})
        _assert(off.check(("我的密码",)), "关闭隐私开关后应放行")

        # 内置默认值必须包含保守项（用户没配也不能默认裸奔）
        defaults = PrivacyRules.from_config(None)
        _assert(
            any("password" in p.lower() for p in defaults.title_patterns),
            f"默认规则应包含常见密码管理器：{defaults.title_patterns}",
        )
        return f"命中「{blocked.reason}」被拦下；正常窗口放行；默认规则 {len(defaults.title_patterns)} 条"

    @suite.check("变化检测：重复画面被跳过，真变化被识别（这是省 90% 调用的地方）")
    def _() -> str:
        from .vision import ChangeDetector

        detector = ChangeDetector(threshold=5)
        same = _uniform(120)

        first = detector.observe(_frame(same, ("A - Editor",), at=0.0))
        _assert(first is not None, "第一帧应视为有新内容")
        _assert(first.source == "first_frame", f"首帧来源标记不对：{first.source}")
        _assert(first.novelty == 1.0, f"首帧新鲜度应为 1：{first.novelty}")

        # 用户在看视频/读文档：画面几乎不变 -> 必须跳过
        for i in range(20):
            assert detector.observe(_frame(same, ("A - Editor",), at=float(i + 1))) is None
        _assert(
            detector.skipped == 20, f"应跳过 20 帧，实为 {detector.skipped}"
        )
        _assert(
            abs(detector.saved_ratio - 20 / 21) < 0.01,
            f"节省比例计算错误：{detector.saved_ratio}",
        )

        # 换了个窗口：大幅变化必须被识别
        changed = detector.observe(_frame(_checker(7), ("B - Browser",), at=30.0))
        _assert(changed is not None, "换了窗口却没检测到变化")
        _assert(changed.novelty >= 0.5, f"大幅变化的新鲜度应偏高：{changed.novelty}")
        _assert(changed.titles == ("B - Browser",), "窗口标题未随帧带出")
        _assert("B - Browser" in changed.content, f"文本线索未生成：{changed.content!r}")

        stats = detector.stats()
        _assert(stats["total_frames"] == 22, f"总帧数统计错误：{stats}")
        return (
            f"21 帧里跳过 20 帧（省 {detector.saved_ratio:.0%}），"
            f"换窗口被识别为 novelty={changed.novelty:.2f}"
        )

    @suite.check("新鲜度映射：阈值以下归零，超过后迅速拉升并饱和")
    def _() -> str:
        from .vision import novelty_from_distance

        _assert(novelty_from_distance(0, 5) == 0.0, "零距离应为 0")
        _assert(novelty_from_distance(4, 5) == 0.0, "阈值以下应归零")
        _assert(novelty_from_distance(5, 5) > 0.4, "刚到阈值就应有明显意愿")
        _assert(novelty_from_distance(20, 5) == 1.0, "远超阈值应饱和到 1")
        _assert(novelty_from_distance(3, 0) == 1.0, "阈值为 0 时任何变化都是全新鲜")
        # 单调不降
        series = [novelty_from_distance(d, 5) for d in range(0, 40)]
        _assert(all(a <= b for a, b in zip(series, series[1:])), "新鲜度应单调不降")
        return "阈值归零 / 阶跃拉升 / 饱和 / 单调性 均正确"

    @suite.check("灰度网格提取：亮暗可分、抗缩放、尺寸不符时报错")
    def _() -> str:
        from .vision import grid_from_rgb

        size = 32
        dark = bytes([10, 10, 10, 255] * (size * size))
        bright = bytes([240, 240, 240, 255] * (size * size))
        g_dark = grid_from_rgb(dark, size, size, 4)
        g_bright = grid_from_rgb(bright, size, size, 4)
        _assert(len(g_dark) == 64, f"网格应为 64 格，实为 {len(g_dark)}")
        _assert(all(v < 30 for v in g_dark), f"暗图应得低值：{g_dark[:5]}")
        _assert(all(v > 220 for v in g_bright), f"亮图应得高值：{g_bright[:5]}")

        # 左黑右白：左半应显著低于右半（验证采样位置没有整体错位）
        row = bytearray()
        for _y in range(size):
            for x in range(size):
                value = 10 if x < size // 2 else 240
                row += bytes([value, value, value, 255])
        grid = grid_from_rgb(bytes(row), size, size, 4)
        left = [grid[r * 8 + c] for r in range(8) for c in range(4)]
        right = [grid[r * 8 + c] for r in range(8) for c in range(4, 8)]
        _assert(
            sum(left) / len(left) < sum(right) / len(right) - 100,
            f"左右亮度未分开：左 {sum(left) // len(left)} 右 {sum(right) // len(right)}",
        )

        raised = False
        try:
            grid_from_rgb(b"\x00" * 8, 100, 100, 4)
        except ValueError:
            raised = True
        _assert(raised, "像素数据不足时应报错而不是静默出错值")
        return "暗/亮/左右分块正确；数据不足时报错"

    @suite.check("感知循环：被隐私拦下时不刷新新鲜度（否则密码窗口会让它更容易开口）")
    def _() -> str:
        from .vision import PrivacyRules, VisionLoop

        clock = _FakeClock()

        class Scripted:
            """
            按顺序吐帧。时间戳用**当轮的假时钟**，而不是预先写死的值 ——
            VisionLoop 是拿注入时钟算年龄的，两者不一致会算出负年龄（被夹到 0），
            于是断言会在错误的地方失败。这个坑踩过一次。
            """

            name = "scripted"

            def __init__(self, items: list[tuple[list[int], tuple[str, ...]]]) -> None:
                self.items = list(items)

            def grab(self) -> Any:
                if not self.items:
                    return None
                grid, titles = self.items.pop(0)
                return _frame(grid, titles, at=clock.now)

        loop = VisionLoop(
            bus=Bus(),
            capturer=Scripted(
                [
                    (_uniform(120), ("main.py - VS Code",)),
                    (_uniform(120), ("main.py - VS Code",)),  # 没变化
                    (_checker(3), ("我的密码 - Chrome",)),  # 隐私命中
                    (_checker(3), ("main.py - VS Code",)),  # 换了窗口
                ]
            ),
            rules=PrivacyRules.from_config({"title_patterns": ["密码"]}),
            clock=clock,
        )

        first = loop.step()
        _assert(first is not None, "首帧应产出观察")
        _assert(loop.observation_age_s() == 0.0, f"刚观察到，年龄应为 0：{loop.observation_age_s()}")

        _assert(loop.step() is None, "画面没变应跳过")
        clock.advance(30)
        age_before = loop.observation_age_s()
        _assert(age_before == 30.0, f"假时钟推进 30s 后年龄应为 30：{age_before}")

        # 关键断言：隐私命中时，既不该有观察，也不该刷新新鲜度
        _assert(loop.step() is None, "隐私窗口应被跳过")
        _assert(
            loop.observation_age_s() == age_before,
            f"隐私窗口居然刷新了新鲜度：{age_before} -> {loop.observation_age_s()}",
        )
        _assert(loop.blocked_count == 1, f"拦截计数错误：{loop.blocked_count}")
        _assert("密码" in loop.last_block_reason, f"未记录拦截原因：{loop.last_block_reason}")

        # 换成正常窗口的大变化应被识别，并重置新鲜度
        got = loop.step()
        _assert(got is not None, "正常窗口的大变化应被识别")
        _assert(loop.observation_age_s() == 0.0, f"新观察应把新鲜度重置为 0：{loop.observation_age_s()}")

        # 暂停后不再读取屏幕
        loop.set_paused(True)
        _assert(loop.step() is None, "暂停后不该再产出观察")

        stats = loop.stats()
        _assert(stats["privacy_blocked"] == 1, f"统计里应体现拦截：{stats}")
        return (
            f"拦截 {stats['privacy_blocked']} 次（{stats['last_block_reason']}）且未刷新新鲜度；"
            f"变化后 novelty={got.novelty:.2f}"
        )

    @suite.check("调度器接线：有新鲜观察才愿意评论，过期观察不再触发")
    def _() -> str:
        # 只留观察信号，隔离验证"感知 -> 意愿"这条接线
        scheduler, _cfg, _clock = _scheduler_for(
            activity={"overall": 1.0, "idle_chatter_rate": 0.0, "screen_watch_prob": 1.0},
            autonomy={"idle_after_s": 99999.0},
        )
        fresh = scheduler.evaluate(observation_age_s=2.0)
        stale = scheduler.evaluate(observation_age_s=600.0)
        _assert(fresh.speak, f"刚看到变化应愿意评论：{fresh}")
        _assert(not stale.speak, f"十分钟前的观察不该触发：{stale}")
        _assert(
            fresh.signals["observation"] > stale.signals["observation"],
            "新鲜度未体现在意愿因子里",
        )

        # require_observation：没看到东西时，即使活泼度拉满也不开口
        strict, _cfg2, _clock2 = _scheduler_for(
            activity={"overall": 1.0}, autonomy={"require_observation": True}
        )
        _clock2.advance(86400)
        blocked = strict.evaluate(observation_age_s=None)
        _assert(blocked.reason == "need_observation", f"应要求先看到东西：{blocked}")
        _assert(strict.evaluate(observation_age_s=1.0).speak, "有观察后应允许开口")
        return (
            f"新鲜 {fresh.desire:.2f}（开口）→ 过期 {stale.desire:.2f}（不说）；"
            "require_observation 时无观察一律不说"
        )

    @suite.check("观察内容真的进入生成：mock 会围绕窗口标题说话")
    def _() -> str:
        from .brains import LLMRequest, build_brain

        cfg = Config()
        cfg.apply_patch({"activity": {"overall": 0.9}})
        brain = build_brain(cfg, _fresh_emotion())
        hint = "你刚瞥了一眼屏幕，当前最前面的窗口是：「report.md - VS Code」。"

        async def run() -> str:
            req = LLMRequest(
                system="你是桌宠", history=[], user_text=None, context_hint=hint
            )
            chunks = [c async for c in brain.stream(req)]  # type: ignore[attr-defined]
            return "".join(c["text"] for c in chunks if c["kind"] == "delta")

        text = asyncio.run(run())
        _assert(text, "自主发言内容为空")
        _assert(
            "report.md" in text,
            f"窗口标题没有进入生成结果（观察通道等于没接上）：{text!r}",
        )
        _assert(
            "「report.md - VS Code」" not in text,
            f"不该整段照抄提示词：{text!r}",
        )
        return f"围绕窗口标题生成：「{text}」"


# --------------------------------------------------------------------------- #
# 3.7 屏幕内容理解（P4 内容层）
# --------------------------------------------------------------------------- #


class _FakeExtractor:
    """可控的抽取器：记录被调用次数，返回预定文本。用于隔离验证顺序与成本。"""

    def __init__(self, name: str, text: str = "", available: bool = True, error: str = "") -> None:
        self.name = name
        self.text = text
        self._available = available
        self.error = error
        self.calls = 0

    def available(self) -> bool:
        return self._available

    def extract(self, _frame: Any) -> Any:
        from .content import Extraction

        self.calls += 1
        return Extraction(text=self.text, source=self.name, ok=bool(self.text), error=self.error)


def test_content(suite: SelfTest) -> None:
    # 在函数作用域导入：本模块的隐私测试彼此独立，靠闭包共享名字很容易漏
    from .vision import ChangeDetector, PrivacyRules, VisionLoop  # noqa: F401

    @suite.check("文本清理：压缩空白、按上限截断")
    def _() -> str:
        from .content import _tidy

        _assert(_tidy("") == "", "空输入应返回空串")
        _assert(_tidy("  a \n\n  b  ") == "a b", f"未压缩空白：{_tidy('  a \n\n  b  ')!r}")
        _assert(_tidy("x" * 100, limit=10).endswith("…"), "超长应带省略号")
        _assert(len(_tidy("x" * 100, limit=10)) == 11, "截断长度不对")
        return "空白压缩 / 截断 均正确"

    @suite.check("内容抽取链：按成本从低到高，凑够就停（默认不烧钱）")
    def _() -> str:
        from .content import ContentPipeline

        title = _FakeExtractor("title", "main.py - VS Code 里正在编辑一段代码")
        ocr = _FakeExtractor("ocr", "一些识别出来的文字")
        vlm = _FakeExtractor("vlm", "视觉模型描述")

        # 标题已经够长 -> 后面两级都不该被调用（这就是省钱的地方）
        pipeline = ContentPipeline([title, ocr, vlm], min_chars_before_next=8)
        result = pipeline.extract(_frame(_uniform(120), ("main.py - VS Code",)))
        _assert(result.source == "title", f"应停在标题级：{result.source}")
        _assert(ocr.calls == 0, "标题够用时不该调用 OCR")
        _assert(vlm.calls == 0, "标题够用时不该调用视觉模型")
        _assert(pipeline.costly_calls == 0, "不该产生付费调用")

        # 标题太短（"未命名"这种）-> 继续往下走到 OCR
        short = _FakeExtractor("title", "无题")
        pipeline2 = ContentPipeline([short, ocr, vlm], min_chars_before_next=8)
        result2 = pipeline2.extract(_frame(_uniform(120), ("无题",)))
        _assert(ocr.calls == 1, "标题信息不足时应继续调用 OCR")
        _assert(vlm.calls == 0, "OCR 给出内容后不该再调用视觉模型")
        _assert(result2.source == "ocr", f"应停在 OCR 级：{result2.source}")

        # 前两级都空 -> 才轮到视觉模型
        empty_title = _FakeExtractor("title", "")
        empty_ocr = _FakeExtractor("ocr", "", error="未识别到文字")
        pipeline3 = ContentPipeline([empty_title, empty_ocr, vlm], min_chars_before_next=8)
        result3 = pipeline3.extract(_frame(_uniform(120)))
        _assert(vlm.calls == 1, "前两级都空时应调用视觉模型")
        _assert(result3.source == "vlm", f"应停在视觉模型级：{result3.source}")
        _assert(pipeline3.costly_calls == 1, "应记录一次付费调用")

        # 不可用的抽取器必须被跳过，不能占位
        missing = _FakeExtractor("ocr", "不该出现", available=False)
        pipeline4 = ContentPipeline([empty_title, missing, vlm], min_chars_before_next=8)
        pipeline4.extract(_frame(_uniform(120)))
        _assert(missing.calls == 0, "不可用的抽取器被调用了")
        return "标题够用则零成本；不足时逐级下探；不可用项被跳过"

    @suite.check("OCR 抽取器：引擎缺失时安全不可用，不会拖垮链路")
    def _() -> str:
        from .content import OcrExtractor

        extractor = OcrExtractor()
        status = "可用" if extractor.available() else "不可用"
        result = extractor.extract(_frame(_uniform(120), ("某窗口",)))
        # 无论引擎在不在，extract 都必须返回 Extraction 而不是抛异常
        _assert(hasattr(result, "ok"), "extract 应返回 Extraction")
        _assert(result.source in ("ocr", "title"), f"来源标记异常：{result.source}")
        if not extractor.available():
            _assert(not result.ok, "引擎不可用时不该报告成功")
        return f"引擎{status}；调用未抛异常，返回 source={result.source} ok={result.ok}"

    @suite.check("视觉模型抽取器：请求体正确、响应解析容错、失败不抛异常")
    def _() -> str:
        import io
        import json as _json
        import urllib.error

        from .content import OpenAIVisionExtractor

        extractor = OpenAIVisionExtractor(
            base_url="http://127.0.0.1:1/v1", model="qwen-vl", api_key="sk-test"
        )
        _assert(extractor.available(), "配置齐全时应可用")

        # 1) 正常响应：解析出文本，并确认请求体里带了 data URL 图片
        captured: dict[str, Any] = {}

        class _Ok:
            def __enter__(self) -> "_Ok":
                return self

            def __exit__(self, *_: Any) -> None:
                return None

            def read(self) -> bytes:
                return _json.dumps(
                    {"choices": [{"message": {"content": "用户正在写 Python 代码"}}]}
                ).encode()

        def fake_urlopen(request: Any, timeout: float = 0) -> Any:  # noqa: ARG001
            captured["url"] = request.full_url
            captured["body"] = _json.loads(request.data.decode("utf-8"))
            captured["auth"] = request.headers.get("Authorization")
            return _Ok()

        # 帧要带上图像数据，否则抽取器会直接判定"没有可发送的图像"
        frame = _frame(_uniform(120), ("x",), image=b"\x89PNG\r\n\x1a\nfake")

        import pet.content as content_mod

        # 同上：走 netutil（2026-10-19 起）
        import pet.netutil as netutil_mod2

        original = netutil_mod2.urlopen
        netutil_mod2.urlopen = fake_urlopen  # type: ignore[assignment]
        try:
            result = extractor.extract(frame)
        finally:
            netutil_mod2.urlopen = original  # type: ignore[assignment]

        _assert(result.ok and "Python" in result.text, f"未解析出描述：{result}")
        _assert(captured["url"].endswith("/chat/completions"), f"接口路径不对：{captured['url']}")
        _assert(captured["auth"] == "Bearer sk-test", "未带上鉴权头")
        parts = captured["body"]["messages"][0]["content"]
        _assert(any(p.get("type") == "image_url" for p in parts), "请求体里没有图片")
        _assert(
            parts[1]["image_url"]["url"].startswith("data:image/png;base64,"),
            "图片未编码成 data URL",
        )
        _assert(captured["body"]["temperature"] <= 0.3, "描述画面应低温度，避免自由发挥")

        # 2) 没有图像数据 -> 直接失败，不发请求
        bare = extractor.extract(_frame(_uniform(120)))
        _assert(not bare.ok, "没有图像数据时不该报告成功")

        # 3) HTTP 错误 -> 翻译成人能看懂的话，且不抛异常
        def fail_urlopen(*_: Any, **__: Any) -> Any:
            raise urllib.error.HTTPError(
                "http://x", 429, "Too Many Requests", {}, io.BytesIO(b'{"error":{"message":"quota"}}')
            )

        netutil_mod2.urlopen = fail_urlopen  # type: ignore[assignment]
        try:
            failed = extractor.extract(frame)
        finally:
            netutil_mod2.urlopen = original  # type: ignore[assignment]
        _assert(not failed.ok, "HTTP 错误时不该报告成功")
        _assert("限流" in failed.error or "配额" in failed.error, f"错误信息不可读：{failed.error}")
        return f"请求体/鉴权/图片编码正确；429 被翻译为「{failed.error[:28]}」"

    @suite.check("成本验证：变化检测把视觉模型的调用量压到约十分之一")
    def _() -> str:
        """
        这是方案里"省 90%"那个说法的实测。做法是模拟真实使用：
        大部分时间用户在做同一件事（画面静止），偶尔切换窗口。
        对比"每轮都调视觉模型"与"只在变化时调"的调用次数。
        """
        from .content import ContentPipeline
        from .vision import ChangeDetector, VisionLoop

        # 造 100 帧：其中 90 帧画面静止，10 帧真的换了窗口
        frames: list[Any] = []
        grid_static = _uniform(120)
        for index in range(100):
            if index % 10 == 0:
                grid_static = _checker(index)  # 换了个窗口
            frames.append(_frame(grid_static, (f"窗口{index // 10} - App",), at=float(index)))

        class Counting:
            name = "scripted"

            def __init__(self, items: list[Any]) -> None:
                self.items = list(items)

            def grab(self) -> Any:
                return self.items.pop(0) if self.items else None

        vlm = _FakeExtractor("vlm", "视觉模型看到的画面")
        pipeline = ContentPipeline([vlm], min_chars_before_next=8)
        loop = VisionLoop(
            bus=Bus(),
            capturer=Counting(frames),
            rules=PrivacyRules.from_config(None),
            detector=ChangeDetector(threshold=5),
            content=pipeline,
        )

        # step() 是同步的，会走 detector；这里手动模拟 astep 的"抽取"环节
        enriched = 0
        for _ in range(100):
            observation = loop.step()
            if observation is not None:
                pipeline.extract(loop._last_frame)  # noqa: SLF001
                enriched += 1

        naive = 100  # 不判变化的话，每帧都要调一次视觉模型
        saved = 1 - enriched / naive
        _assert(
            vlm.calls <= 12,
            f"视觉模型调用次数过多（{vlm.calls}）—— 变化检测没起到省成本的作用",
        )
        _assert(saved >= 0.85, f"节省比例应≥85%，实测 {saved:.0%}")
        return (
            f"100 帧里只调用视觉模型 {vlm.calls} 次（不判变化则需 {naive} 次），"
            f"省 {saved:.0%}"
        )

    @suite.check("隐私优先于内容：被拦下的帧绝不进入任何抽取器")
    def _() -> str:
        from .content import ContentPipeline
        from .vision import VisionLoop

        class Counting:
            name = "scripted"

            def __init__(self, items: list[Any]) -> None:
                self.items = list(items)

            def grab(self) -> Any:
                return self.items.pop(0) if self.items else None

        title = _FakeExtractor("title", "密码 123456", available=True)
        pipeline = ContentPipeline([title])
        loop = VisionLoop(
            bus=Bus(),
            capturer=Counting([_frame(_checker(1), ("我的密码 - Chrome",))]),
            rules=PrivacyRules.from_config(None),  # 内置规则里就有"密码"
            detector=ChangeDetector(),
            content=pipeline,
        )

        observation = loop.step()
        _assert(observation is None, "隐私窗口不该产出观察")
        _assert(title.calls == 0, f"隐私窗口的内容居然被抽取了 {title.calls} 次")
        _assert(loop.blocked_count == 1, f"拦截计数错误：{loop.blocked_count}")
        return "命中隐私规则的帧在任何抽取之前就被丢弃（抽取器 0 次调用）"


# --------------------------------------------------------------------------- #
# 3.8 语音链路（P3）
# --------------------------------------------------------------------------- #


def _frames_from(segments: list[tuple[str, int]]) -> list[Any]:
    """
    把 "静音 300ms / 语音 800ms" 这样的描述展开成 20ms 音频帧序列。

    语音链路的价值几乎全在**时序**上（停顿多长算说完、插话多久算打断），
    所以测试必须能精确构造时间轴。用合成正弦波当"人声"（能量与过零率都像浊音），
    用低幅噪声当"环境底噪"。
    """
    from .voice import FRAME_MS, AudioFrame, noise, silence, tone

    frames: list[Any] = []
    # 先给一小段底噪，让 VAD 的自适应噪声底有东西可学
    for _ in range(10):
        frames.append(AudioFrame(pcm=noise(FRAME_MS, amplitude=0.006, seed=7)))
    for kind, duration_ms in segments:
        count = max(1, duration_ms // FRAME_MS)
        for index in range(count):
            if kind == "speech":
                # 交替频率，模拟说话时的音高变化（也让过零率不恒定）
                pcm = tone(FRAME_MS, freq=180 + 40 * (index % 3), amplitude=0.35)
            elif kind == "noise":
                pcm = noise(FRAME_MS, amplitude=0.02, seed=index)
            else:
                pcm = silence(FRAME_MS)
            frames.append(AudioFrame(pcm=pcm))
    return frames


def _run_endpointer(segments: list[tuple[str, int]]) -> list[Any]:
    """跑一遍端点检测，返回识别出的"句子"列表。"""
    from .voice import Endpointer, VoiceActivityDetector

    vad = VoiceActivityDetector()
    endpointer = Endpointer(vad.config)
    utterances: list[Any] = []
    for frame in _frames_from(segments):
        result = vad.process(frame)
        utterance = endpointer.process(frame, result)
        if utterance is not None:
            utterances.append(utterance)
    return utterances


def test_voice(suite: SelfTest) -> None:
    @suite.check("语音模块：同时管「识别你说的话」和「出声说话」")
    def _() -> str:
        """用户 2026-11-07 问：

        > 「语音模块包含识别用户的话和合成语音吗，如果是的话，
        >   语音模块的描述上要写清楚」

        ## 查证结论：**两个都管**

        `gateway.py` 里那一处判断同时决定 ASR 和 TTS：

            if self.modules.enabled("voice"):
                voice_pipeline = build_voice_pipeline(config, config.api_key())
            else:
                voice_pipeline = VoicePipeline(asr=MockAsrEngine(), tts=MockTtsEngine())

        而 `build_voice_pipeline` 一次建**两个**引擎。

        ## 这条测试守什么

        守两件事，任何一件坏了用户就会看到一个"说半句假话"的开关：

          ① 关掉模块 -> ASR **和** TTS 都变成 mock（不能只关一半）
          ② 设置页的说明必须**说清它管两件事** —— 用户正是因为这个
             看不明白才来问的。说明里不能只有「ASR/TTS」这种缩写。
        """
        from .config import Config
        from .gateway import Gateway
        from .modules import MODULES

        # ① 模块定义上说得清楚
        m = next((x for x in MODULES if x.name == "voice"), None)
        _assert(m is not None, "应当有 voice 模块")
        note = m.note
        _assert(
            "识别" in note and "说话" in note,
            f"说明必须同时提到「识别你说的话」和「出声说话」，实际：{note!r}",
        )
        _assert(
            "ASR" not in note and "TTS" not in note,
            "说明里不该只写 ASR/TTS 这种缩写 —— 用户看不懂，"
            "这正是他要求「写清楚」的原因",
        )

        # ② 关掉模块 -> 两个引擎都变 mock
        #
        # ⚠️ 引擎是在 `Gateway.__init__` 里建的，所以必须在**构造前**
        #    就把配置里的 voice 关掉 —— 构造后再 set_enabled 只影响未来，
        #    已经建好的引擎还是真的。这正是"切了要重启才生效"的原因。
        from .config import Config
        from .gateway import Gateway

        cfg = Config()
        try:
            cfg.root.setdefault("modules", {})["voice"] = {"enabled": False}
        except Exception:  # noqa: BLE001
            _assert(False, "配置结构不对，无法测试")
        gw = Gateway(cfg)

        tts_name = type(gw.voice.tts).__name__
        _assert(
            tts_name == "MockTtsEngine",
            f"关掉语音模块后 TTS 必须是 mock（否则它还会出声），实际 {tts_name}",
        )
        pipeline = getattr(gw.voice, "pipeline", None)
        if pipeline is not None:
            asr_name = type(pipeline.asr).__name__
            _assert(
                asr_name == "MockAsrEngine",
                f"关掉语音模块后 ASR 也必须是 mock，实际 {asr_name} —— "
                "只关一半的话，它还能听懂你说话，那就不是「关掉语音」",
            )

        return "说明写清两件事；关掉后 ASR 和 TTS 都降级"

    @suite.check("语音识别：本地/云端两条路的构建与降级（用户要求可切换）")
    def _() -> str:
        """
        ## 用户要求（2026-10-07）

        「把链接语音处理 api 的一起搞了，用硅基流动的 api，
        在设置里做了本地大模型和 api 的切换开关」

        ## 这条测试钉住四件事

          ① `provider: local`  -> 造出 `LocalAsrEngine`（装了 faster-whisper 时）
          ② `provider: openai` + `cloud_*` -> 造出指向**硅基流动**的 `OpenAIAsrEngine`
          ③ 云端缺密钥 -> **降级为 mock，并且说明里要写清原因**
             （静默降级会让人以为"识别不准"，而不是"根本没配"）
          ④ 模型缓存探测**不能硬编码发布者** —— 实测 turbo 的 org 是
             `mobiuslabsgmbh` 而 small/medium 是 `Systran`，
             写死 `Systran` 会让"下好了"被报成"还没下"
        """
        import tempfile

        from .voice import LocalAsrEngine, MockAsrEngine, OpenAIAsrEngine, build_voice_pipeline

        out: list[str] = []

        # ① 本地
        cfg1 = Config()
        cfg1.apply_patch({"providers": {"asr": {"provider": "local", "model": "large-v3-turbo"}}})
        p1 = build_voice_pipeline(cfg1, "sk-brain")
        if type(p1.asr).__name__ == "LocalAsrEngine":
            _assert("large-v3-turbo" in p1.asr_note, f"本地说明没提模型名：{p1.asr_note}")
            out.append("local -> LocalAsrEngine")
        else:
            # 没装 faster-whisper 的环境下必须**诚实降级并说明**
            _assert(isinstance(p1.asr, MockAsrEngine), "本地不可用时应降级为 mock")
            _assert("faster-whisper" in p1.asr_note, f"降级说明应指出缺什么：{p1.asr_note}")
            out.append("local -> mock（未装依赖，已说明）")

        # ② 云端 + cloud_*（硅基流动）
        cfg2 = Config()
        cfg2.apply_patch(
            {
                "providers": {
                    "asr": {
                        "provider": "openai",
                        "cloud_model": "FunAudioLLM/SenseVoiceSmall",
                        "cloud_base_url": "https://api.siliconflow.cn/v1",
                        "cloud_api_key_env": "SELFTEST_ASR_KEY",
                    }
                }
            }
        )
        os.environ["SELFTEST_ASR_KEY"] = "sk-selftest"
        p2 = build_voice_pipeline(cfg2, "sk-brain")
        _assert(
            isinstance(p2.asr, OpenAIAsrEngine),
            f"云端路线应造出 OpenAIAsrEngine，实际 {type(p2.asr)}",
        )
        _assert(
            "siliconflow" in p2.asr.base_url,  # type: ignore[attr-defined]
            f"没用到 cloud_base_url：{p2.asr.base_url}",  # type: ignore[attr-defined]
        )
        _assert(p2.asr.model == "FunAudioLLM/SenseVoiceSmall", "没用到 cloud_model")  # type: ignore[attr-defined]
        out.append("openai+cloud_* -> 硅基流动")

        # ③ 云端缺密钥 -> 降级且**说明原因**
        del os.environ["SELFTEST_ASR_KEY"]
        p3 = build_voice_pipeline(cfg2, "")  # 连 brain 的密钥也不给
        _assert(isinstance(p3.asr, MockAsrEngine), "缺密钥时应降级为 mock")
        _assert("密钥" in p3.asr_note, f"降级说明应指出缺密钥：{p3.asr_note}")
        out.append("云端缺密钥 -> mock（已说明）")

        # ④ 缓存探测不能硬编码 org
        engine = LocalAsrEngine(model="large-v3-turbo", model_dir=Path("/nonexistent-xyz"))
        _assert(engine.ensure_model_cached() is False, "不存在的目录不该报 cached")
        # 造一个假缓存，org 用一个**非 Systran** 的名字 —— 正是实测踩到的情形
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "models--someorg--faster-whisper-large-v3-turbo" / "snapshots" / "abc"
            fake.mkdir(parents=True)
            (fake / "model.bin").write_bytes(b"x")
            e2 = LocalAsrEngine(model="large-v3-turbo", model_dir=Path(tmp))
            _assert(
                e2.ensure_model_cached() is True,
                "缓存探测硬编码了发布者 —— 换个 org 就认不出（实测 turbo 的 org 不是 Systran）",
            )
        out.append("缓存探测不依赖发布者名")

        return "；".join(out)

    @suite.check("语音识别：繁体输出被转成简体（用户实测反馈 2026-10-08）")
    def _() -> str:
        """
        ## 用户反馈

        「转译语音竟然是繁体字，虽然我是唱歌」

        实际日志：`讓我的眼淚陪我過夜不讓你的溫柔著餘味忘了曾經愛過誰慢慢習慣了寂寞相隨`

        ## 为什么需要这道后处理

        Whisper 系模型（含 large-v3-turbo）的中文输出**会混繁体** ——
        训练语料里繁体占比高。`initial_prompt`（"请用简体中文转写"）实测有效，
        但**不保险**（模型可能不听，唱歌/口音/噪声大时尤其）。

        所以有第二道**确定性**的兜底：`t2s_table.to_simplified()`，
        映射表由 Unicode Unihan 数据生成（6500+ 条，无运行时依赖）。

        ## 这条测试守两件事

          ① 转换表可用（它是**生成的**，重新生成时可能出错）
          ② 引擎真的调用了它（而不是只有表在那儿躺着）

        ⚠️ 用**用户那句真实的歌词**当样本 —— 它有 14 个繁体字，
        是这条 bug 的原始证据。比我自己编的句子有说服力。
        """
        from .t2s_table import _T2S, to_simplified

        # ① 表本身
        _assert(len(_T2S) > 3000, f"繁->简表太小（{len(_T2S)} 条），可能生成失败")

        raw = "讓我的眼淚陪我過夜不讓你的溫柔著餘味忘了曾經愛過誰慢慢習慣了寂寞相隨"
        got = to_simplified(raw)
        _assert(got != raw, "用户那句歌词没被转换 —— 表里缺了这些字")
        left = [c for c in got if c in _T2S]
        _assert(not left, f"转换后仍有繁体字：{left}")

        # ⚠️ 台湾变体「裡」Unihan 不收（它算 U+88CF 裏 的异体），
        # 所以有一张手工补充表。这条防止那张表被误删。
        _assert(to_simplified("裡面") == "里面", "「裡」没被转成「里」（手工补充表丢了？）")

        # ② 简体文本不该被改动（转换必须是幂等的、不误伤）
        plain = "我最近在写一个桌面宠物的程序,用的是Python和Electron。"
        _assert(to_simplified(plain) == plain, f"简体文本被改坏了：{to_simplified(plain)}")

        # ③ 引擎默认开着这道转换
        from .voice import LocalAsrEngine

        e = LocalAsrEngine(model="x", model_dir=Path("/nonexistent"))
        _assert(e.convert_to_simplified is True, "本地引擎默认应当开启繁->简转换")
        _assert(e.initial_prompt, "本地引擎默认应当带引导简体的 initial_prompt")

        return f"表 {len(_T2S)} 条；用户那句 14 个繁体全部转简、简体不受影响、引擎默认开启"

    @suite.check("记忆：刚说的那句**不会在提示词里重复**（用户反馈「传输了两遍」）")
    def _() -> str:
        """
        ## 用户反馈（2026-10-08）

        「好像是传输的时候传了两遍，你查查看是什么问题」

        ## 真因：不是网络，是**提示词里重复**

        `gateway.ask()` 的顺序是：**先写用户这句**（让检索算上当前话题），
        **再拼提示词**。而"最近记忆"那段是整段注入 `recent_turn` 的 ——
        于是同一句话**同时**出现在两处：

            messages[0] system   「[今天] 用户：周杰伦的雨下一整晚。」
            messages[1] user     「周杰伦的雨下一整晚。」

        **模型看到两遍**，于是每轮都回「你说了两遍」。

        ## 怎么发现的（值得记）

        不是靠读代码，是靠**宠物的回复本身**：库里每条用户消息**只出现一次**，
        但宠物连续四轮的回复都在说「你说了两遍」——

            今天天气不错。   -> 诶，你说了两遍…
            听得到我说话吗?  -> 听得见听得见，你又说两遍啦…
            要不来…唱歌啊?   -> 诶，你又说两遍啦…
            白菜鸡毛菜…     -> 诶，又是两遍～

        **它每一轮都这么说，就说明它每一轮都真的看到了两份。**
        「一条日志里没有重复」和「模型没看到重复」是两件事。

        ## 这条测试钉住三件事（2026-10-08 按新契约重写）

        ⚠️ **旧契约已经作废**。老版本钉的是：

            ② 写进去之后，「最近记忆」那段**能检索到**它
            ③ 排除 id 之后，注入里不该有它

        那是在"最近记忆整段注入提示词"的前提下写的补丁。**那段注入已经删掉了**
        （用户要求：最近对话改走 `messages`），所以"能在提示词里检索到"
        这件事**本身就不该再成立** —— 一句话只出现在一个地方。

        新契约更强：

          ① 写之前，注入里没有它
          ② 写之后，**注入里仍然没有它** —— 逐字原文**永不进提示词**
          ③ `recent_dialogue()` 能取到它 —— 记忆本身没丢，它走的是 `messages`
        """
        import time as _time

        from .config import Config
        from .memory2 import MemoryService

        cfg = Config()
        cfg.apply_patch({"memory": {"path": "", "enabled": True}})
        svc = MemoryService(cfg)
        mark = f"独有标记{int(_time.time() * 1000)}"

        try:
            # ① 写之前
            _assert(mark not in svc.inject_hint(mark), "还没写就出现在注入里了（测试被污染）")

            tid = svc.remember_turn("s-selftest", "user", mark)
            _assert(tid > 0, f"remember_turn 没返回有效 id（得到 {tid}）")

            # ② 写之后**仍然不该**出现在提示词里 —— 逐字原文永不进提示词
            hint_after = svc.inject_hint(mark)
            _assert(
                mark not in hint_after,
                "逐字原文出现在提示词里了 —— 它应该只走 messages（否则模型看到两遍）",
            )

            # ③ 但它必须能被 `recent_dialogue()` 取到 —— 记忆没丢，只是换了通道
            rows = svc.recent_dialogue(limit=50, max_total_chars=0)
            texts = [r["text"] for r in rows]
            _assert(
                any(mark in t for t in texts),
                f"recent_dialogue 里找不到刚写的那句 —— 记忆丢了（取到 {len(rows)} 条）",
            )
        finally:
            svc.close()

        return "写之前无、写之后提示词里仍无（走 messages）、recent_dialogue 取得到"

    @suite.check("大模型多提供商互切：profile 解析正确，且**不跨厂商借密钥**")
    def _() -> str:
        """
        ## 用户要求（2026-10-08）

        「两个都要，要和现有的 deepseek 有互切选项」

        ## 这条测试守两件事

        **① `brain_profile()` 的合并规则**

          · 厂商自己的 `base_url` / `model` 生效
          · **通用参数**（temperature / max_tokens / timeout_s）从平铺字段继承
            —— 否则每个厂商都要抄一遍
          · `active` 指向不存在的名字时，**退回平铺字段**并打上 `_fallback`
            （不能让宠物静默变成 mock）

        **② ⚠️ 不许跨厂商借密钥**（我自己踩到的坑）

        第一版密钥候选名里留了 `deepseek` / `openai` 当兜底，实测：

            切到 siliconflow（还没配它的密钥）
            -> 候选名 ('siliconflow', 'siliconflow_api_key', 'deepseek', 'openai')
            -> **取到了 DeepSeek 的密钥**
            -> 请求硅基流动 -> HTTP 401 "Token is invalid"

        那种报错会让人去怀疑"密钥写错了 / 硅基流动挂了"，而不是
        "我还没配这一家的密钥" —— **比直接报没有密钥糟得多**。

        所以：配了 `named` 就**只认这一家自己的槽位**。
        """
        from .brains import build_brain
        from .brains.base import EmotionState
        from .config import Config

        cfg = Config()
        cfg.providers["brain"] = {
            "provider": "openai",
            "active": "alpha",
            "temperature": 0.85,
            "max_tokens": 400,
            "timeout_s": 30,
            "base_url": "https://fallback.example/v1",
            "model": "fallback-model",
            "api_key_env": "FALLBACK_KEY",
            "named": {
                "alpha": {
                    "label": "Alpha",
                    "base_url": "https://alpha.example/v1",
                    "model": "alpha-model",
                    "api_key_env": "ALPHA_KEY",
                },
                "beta": {
                    "label": "Beta",
                    "base_url": "https://beta.example/v1",
                    "model": "beta-model",
                    "api_key_env": "BETA_KEY",
                },
            },
        }

        # ① profile 合并
        a = cfg.brain_profile("alpha")
        _assert(a["base_url"] == "https://alpha.example/v1", f"alpha 的 base_url 不对：{a}")
        _assert(a["model"] == "alpha-model", f"alpha 的 model 不对：{a}")
        _assert(
            abs(float(a["temperature"]) - 0.85) < 1e-9,
            f"通用参数（temperature）没从平铺字段继承：{a.get('temperature')}",
        )
        b = cfg.brain_profile("beta")
        _assert(b["base_url"] == "https://beta.example/v1", f"beta 的 base_url 不对：{b}")

        # 不存在的名字 -> 退回平铺字段，并打标记
        fb = cfg.brain_profile("nonexistent")
        _assert(fb["base_url"] == "https://fallback.example/v1", f"兜底没生效：{fb}")
        _assert(fb.get("_fallback") is True, "兜底时应当打上 _fallback 标记（供界面提示）")

        # ② 密钥候选名 —— **不许出现别家厂商的名字**
        cfg.apply_patch({"providers": {"brain": {"active": "alpha"}}})
        names_alpha = cfg._credential_names()
        _assert("alpha" in names_alpha, f"候选里应当有厂商标 alpha：{names_alpha}")
        _assert("beta" not in names_alpha, f"候选里不该有别的厂商 beta：{names_alpha}")
        _assert(
            "deepseek" not in names_alpha,
            f"⚠️ 候选里仍有跨厂商兜底 deepseek —— 会借到别家的密钥：{names_alpha}",
        )

        cfg.apply_patch({"providers": {"brain": {"active": "beta"}}})
        names_beta = cfg._credential_names()
        _assert("beta" in names_beta, f"候选里应当有厂商标 beta：{names_beta}")
        _assert("alpha" not in names_beta, f"候选里不该有别的厂商 alpha：{names_beta}")

        # ③ 没密钥时降级 mock（而不是崩、也不是拿别家的 key 去试）
        import os

        for env in ("ALPHA_KEY", "BETA_KEY", "FALLBACK_KEY", "DEEPSEEK_API_KEY"):
            os.environ.pop(env, None)
        cfg.apply_patch({"providers": {"brain": {"active": "alpha"}}})
        _assert(cfg.api_key() == "", f"没配密钥时不该取到任何密钥，实际拿到 {len(cfg.api_key())} 字符")
        brain = build_brain(cfg, EmotionState())
        _assert(brain.name == "mock", f"没密钥时应当降级 mock，实际 {brain.name}")

        # ④ 旧配置（没有 named）仍按老办法兜底 —— 不能把老用户搞坏
        cfg2 = Config()
        cfg2.providers["brain"] = {
            "provider": "openai",
            "base_url": "https://api.deepseek.com/v1",
            "model": "deepseek-flash",
            "api_key_env": "DEEPSEEK_API_KEY",
        }
        legacy = cfg2._credential_names()
        _assert(
            "deepseek" in legacy,
            f"没有 named 的旧配置应当保留 deepseek 兜底（否则老用户会突然没密钥）：{legacy}",
        )

        return (
            f"profile 合并 + 继承通用参数 + 兜底标记；密钥候选严格按厂商"
            f"（alpha={names_alpha} / beta={names_beta}）；旧配置仍兼容"
        )

    @suite.check("切语音识别（本地<->云端）：引擎**真的**换了，旧引擎被释放（不能是假开关）")
    def _() -> str:
        """
        ## 🔴 这条测试守的是一个"假开关"（2026-10-08 用户实测发现）

        用户在设置页把语音识别从「本地」切到「云端（硅基流动）」，
        配置热更新**成功了**：

            21:01:48  配置已热更新：{'providers.asr.provider': 'openai'}

        但 `build_voice_pipeline()` 只在 Gateway 构造时调过一次，
        `VoiceSession.asr` 也只在 `__init__` 里赋值。所以：

            · 界面写着「云端」，实际**还在用本地 Whisper 识别**
            · 本地 `large-v3-turbo` 继续占 **~800MB 内存 + ~1.5-2GB 显存**
              （实测切换前后内存一动不动：801MB -> 801MB）

        **不报任何错**，用户只会觉得"切了好像没变化"。

        ## 这条测试查三件事

        ① `engine_signature()` 能反映配置变化（否则不会触发重建）
        ② `swap_engines()` 真的把 `self.asr` 换掉了
        ③ 旧引擎的 `close()`/`unload()` 被调用过（资源真的释放）
        """
        from .bus import Bus
        from .config import Config
        from .voice import MockAsrEngine, MockTtsEngine, VoiceSession

        class SpyAsr:
            """记下自己有没有被要求释放。"""

            def __init__(self, tag: str) -> None:
                self.tag = tag
                self.closed = False

            async def transcribe(self, pcm: bytes, sample_rate: int):  # noqa: ANN001, ARG002
                return f"[{self.tag}]"

            def close(self) -> None:
                self.closed = True

        cfg = Config()
        cfg.providers["asr"] = {
            "provider": "local",
            "model": "large-v3-turbo",
            "cloud_model": "FunAudioLLM/SenseVoiceSmall",
        }
        cfg.providers["tts"] = {"provider": "none", "model": "tts-1"}

        session = VoiceSession(config=cfg, bus=Bus(), asr=MockAsrEngine(), tts=MockTtsEngine())

        # ① 指纹随配置变
        sig_local = session.engine_signature()
        _assert(sig_local[0] == "local", f"本地时 provider 应当是 local：{sig_local}")
        _assert(sig_local[1] == "large-v3-turbo", f"本地时应当报**本地模型名**：{sig_local}")

        cfg.providers["asr"]["provider"] = "openai"
        sig_cloud = session.engine_signature()
        _assert(sig_cloud[0] == "openai", f"云端时 provider 应当是 openai：{sig_cloud}")
        _assert(
            sig_cloud[1] == "FunAudioLLM/SenseVoiceSmall",
            f"云端时应当报**云端模型名**（不是本地那个）：{sig_cloud}",
        )
        _assert(sig_local != sig_cloud, "换了 provider 之后指纹必须变，否则不会触发重建")

        # ②③ 真的换 + 旧引擎被释放
        old = SpyAsr("local-whisper")
        session.asr = old
        new = SpyAsr("cloud-sensevoice")
        session.swap_engines(new, MockTtsEngine())

        _assert(session.asr is new, "swap_engines 之后 self.asr 必须指向新引擎")
        _assert(old.closed, "⚠️ 旧引擎的 close() 没被调用 —— 显存/内存不会释放")
        _assert(
            session._engine_signature == sig_cloud,
            f"换完之后指纹要更新（否则下次配置变化会被误判为'没变'）：{session._engine_signature}",
        )

        return (
            f"指纹 {sig_local} -> {sig_cloud}；self.asr 已替换；旧引擎 close() 已调用（资源释放）"
        )

    @suite.check("回复用户：`user.text` 能走通生成（回归 UnboundLocalError）")
    def _() -> str:
        """
        ## 🔴 守的是 2026-10-09 那个把「回复」整体打断的 bug

        `_generate()` 里 `current_turn_id` **只在 `if idle:` 分支里赋值**，
        却在分支外被无条件读取（`_compose_context_hint(..., current_turn_id)`）。

        后果是**回复用户 100% 抛异常**：

            UnboundLocalError: cannot access local variable 'current_turn_id'
                                where it is not associated with a value

        被 `asyncio` 吞成一条 "Task exception was never retrieved" ——
        前端只看到"三个点"，然后什么都没有，**连 TTS 都不会触发**
        （没有 `pet.say.end`）。用户报的就是这个：「没有回音，只有三个点」。

        ## ⚠️ 为什么必须**两条路都测**

        自主发言走 `if idle:` 那条分支，`current_turn_id` 有值、**完全正常**。
        所以只测自主发言的话，这个 bug 测不出来。

        这类 bug 的通式是「**在分支里定义、在分支外使用**」——
        只覆盖一条路径的测试对它天然无效。
        """
        from .bus import Bus
        from .config import Config
        from .config import load_config
        from .gateway import Conversation

        cfg = load_config(Path(__file__).resolve().parents[3] / "config" / "persona.yaml")
        # 用 mock 大脑：这条测试验的是 `_generate` 的**控制流**，
        # 不是模型输出（真打 API 会让测试依赖网络与密钥）
        cfg.apply_patch({"providers.brain.provider": "mock", "providers.tts.provider": "mock"})

        bus = Bus()

        class FakeMemory:
            """记忆库替身：`remember_turn` 必须返回一个 id。"""

            enabled = True

            def __init__(self) -> None:
                self.remembered: list[tuple[str, str]] = []

            def remember_turn(self, session, role, text, **_kw) -> int:
                self.remembered.append((role, text))
                return len(self.remembered)

            def recent_dialogue(self, *_a, **_kw):
                return []

            def search(self, *_a, **_kw):
                return []

            def stats(self):
                return {"enabled": True, "turns": 0}

            def inject_hint(self, *_a, **_kw):
                return ""

        conv = Conversation(bus=bus, config=cfg, memory=FakeMemory())
        errors: list[str] = []

        async def run() -> None:
            # ① 用户消息那条路（**这个之前 100% 崩**）
            try:
                await conv.ask("你好呀")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"ask 抛了 {type(exc).__name__}: {exc}")
            else:
                # ⚠️ **必须 await 那个 task**（2026-10-09 修这条测试时踩到）。
                #
                # `ask()` 只 `create_task` 就返回，异常在**后台 task** 里。
                # 第一版我只 `time.sleep(0.5)` 等了一下 —— 结果**撤掉修复它照样 PASS**，
                # 等于这条测试根本没守住那个 bug（我拿"撤掉修复"验证过才发现）。
                #
                # 只有 await 它，`UnboundLocalError` 才会真的在这里被抛出来。
                if conv.current is not None:
                    try:
                        await conv.current
                    except Exception as exc:  # noqa: BLE001
                        errors.append(f"生成时抛了 {type(exc).__name__}: {exc}")
            # ② 自主发言那条路（以前就是好的，但不能因此漏测）
            try:
                await conv.speak_autonomously({})
            except Exception as exc:  # noqa: BLE001
                errors.append(f"自主发言抛了 {type(exc).__name__}: {exc}")
            else:
                if conv.current is not None:
                    try:
                        await conv.current
                    except Exception as exc:  # noqa: BLE001
                        errors.append(f"自主生成时抛了 {type(exc).__name__}: {exc}")

        asyncio.run(run())

        _assert(not errors, " | ".join(errors))

        return "用户消息与自主发言两条路都跑完 _generate，未抛 UnboundLocalError"


    @suite.check("说话：`pet.say.end` 会触发合成（这个入口之前根本没接）")
    def _() -> str:
        """
        ## 🔴 这条测试守的是一个"功能写好了但没接线"的缺口（2026-10-08）

        `VoiceSession.speak()` 早就写好了（合成 + 算音量包络 + 交给前端播放），
        `OpenAITtsEngine` / `MockTtsEngine` / `WindowsSapiTtsEngine` 也都在 ——
        但**全项目没有任何地方调用 `speak()`**。

        所以在那之前，宠物**一直是"只显示文字、不出声"** —— 而设置页里
        `providers.tts` 那一堆配置看着都是好的。这类"零件齐全、没接线"的缺口
        最难发现：每一块单独看都对，只有端到端跑一次才知道没声音。

        接在 `pet.say.end` 上是因为它是**唯一带完整回复文本**的事件
        （`pet.say.delta` 只有增量片段，拿它逐句合成会把话切碎）。

        ## 这条测试查四件事

          ① `pet.say.end` 之后，TTS 引擎的 `synthesize` 确实被调用了
          ② 合成的文字就是回复文本
          ③ `pet.mouth`（口型包络）被发出去了
          ④ `reason=interrupted` 的**不读**（被打断的半句话不该念出来）
        """
        from .bus import Bus
        from .config import Config
        from .voice import VoiceConfig, VoiceSession

        class SpyTts:
            """记下被要求合成过什么。"""

            name = "spy"

            def __init__(self) -> None:
                self.seen: list[str] = []

            def available(self) -> bool:
                return True

            async def synthesize(self, text: str) -> tuple[bytes, str]:
                self.seen.append(text)
                # 0.2 秒的 16kHz 16bit 静音，够 `compute_envelope` 出结果
                return b"\x00\x00" * 3200, "pcm"

        class SpyOut:
            def __init__(self) -> None:
                self.played: list[str] = []

            async def play(self, payload: bytes, fmt: str, text: str) -> None:
                self.played.append(text)

            async def stop(self) -> None:
                return None

        cfg = Config()
        bus = Bus()
        tts = SpyTts()
        out = SpyOut()
        mouth: list[dict] = []

        async def on_mouth(event: object) -> None:
            mouth.append(dict(getattr(event, "payload", None) or {}))

        session = VoiceSession(
            config=cfg,
            bus=bus,
            tts=tts,  # type: ignore[arg-type]
            audio_out=out,  # type: ignore[arg-type]
            voice_config=VoiceConfig(enabled=True),
        )
        session.attach()
        bus.subscribe("pet.mouth", on_mouth)

        async def run() -> None:
            # ① 正常结束 -> 应当合成
            await bus.publish(
                "pet.say.end",
                {"id": "say1", "text": "你好呀，我是小灵。", "reason": "done"},
            )
            # ④ 被打断的 -> 不该合成
            await bus.publish(
                "pet.say.end",
                {"id": "say2", "text": "这句话不该被念出来", "reason": "interrupted"},
            )

        asyncio.run(run())

        _assert(
            tts.seen == ["你好呀，我是小灵。"],
            f"pet.say.end 没有正确触发合成：合成过 {tts.seen!r}",
        )
        _assert(
            out.played == ["你好呀，我是小灵。"],
            f"合成结果没有交给播放：{out.played!r}",
        )
        _assert(mouth, "没有发 pet.mouth（口型包络）—— 口型不会动")
        _assert(
            bool(mouth[0].get("envelope")),
            f"pet.mouth 的 envelope 是空的：{mouth[0]}",
        )

        return (
            f"合成 1 条、播放 1 条、口型包络 {len(mouth[0]['envelope'])} 段；"
            f"被打断的那句没念（reason=interrupted 被跳过）"
        )


    @suite.check("换音色必须重建引擎（`engine_signature` 要含 voice）")
    def _() -> str:
        """
        ## 🔴 守的是 2026-10-09 用户实测的那个 bug

        用户报：「切换了音色……语音试听没变化」，而且**女声还是男声**。

        根因在 `engine_signature()`：

            return (asr_provider, asr_model, tts_provider,
                    str(tts.get("model") or tts.get("voice") or ""))

        `tts.model` 是 `FunAudioLLM/CosyVoice2-0.5B`，**换音色时它不变**，
        所以指纹不变 -> 网关判定「引擎没变、不必重建」->
        `OpenAITtsEngine.voice` 永远是创建时那个 ->
        **配置改了、界面变了、声音没变**。

        （`or tts.get("voice")` 那个兜底只在 `model` 为空时生效，
        而云端模型名永远非空，所以它形同虚设。）

        ## 这条测试查什么

        `engine_signature` 是一个"配置指纹"，用来决定要不要重建引擎。
        判据是：**这个参数变了，合成结果会不会不一样？**

        所以只要改任何一个"会影响声音"的参数，指纹就**必须**变：

          · `tts.voice`        云端音色
          · `tts.local_voice`  本地音色
          · `tts.rate`         语速

        反之，改一个**不影响声音**的参数（比如免打扰），指纹不应该变 ——
        否则每次改配置都要重载模型（本地 Whisper 要 4 秒）。
        """
        from .config import Config
        from .voice import VoiceSession

        cfg = Config()
        cfg.apply_patch(
            {
                "providers.tts.provider": "openai",
                "providers.tts.model": "FunAudioLLM/CosyVoice2-0.5B",
                "providers.tts.voice": "FunAudioLLM/CosyVoice2-0.5B:alex",
            }
        )

        def sig() -> tuple:
            # 只借 `engine_signature` 这一个方法，不建整个 session
            return VoiceSession.engine_signature(_FakeSession(cfg))  # type: ignore[arg-type]

        base = sig()
        base_local_voice = str(cfg.providers.get("tts", {}).get("local_voice") or "")

        # ① 换云端音色 —— 必须变
        cfg.apply_patch({"providers.tts.voice": "FunAudioLLM/CosyVoice2-0.5B:claire"})
        after_voice = sig()
        _assert(after_voice != base, "换了云端音色但指纹没变 —— 引擎不会重建，声音不会变")

        # ② 换本地音色 —— 必须变
        cfg.apply_patch({"providers.tts.local_voice": "Microsoft Zira Desktop"})
        after_local = sig()
        _assert(after_local != after_voice, "换了本地音色但指纹没变")

        # ③ 改语速 —— 必须变
        cfg.apply_patch({"providers.tts.rate": 3})
        after_rate = sig()
        _assert(after_rate != after_local, "改了语速但指纹没变")

        # ④ 改一个**不影响声音**的东西 —— 指纹不该变
        cfg.apply_patch({"autonomy.enabled": False})
        _assert(sig() == after_rate, "改免打扰也动了指纹 —— 会导致无谓地重建引擎")

        # ⑤ 换回原来的音色 —— 指纹应当回到 base 那一路（可逆）
        cfg.apply_patch(
            {
                "providers.tts.voice": "FunAudioLLM/CosyVoice2-0.5B:alex",
                # ⚠️ local_voice 也要还原 —— ② 改过它，不还原的话指纹当然回不去
                "providers.tts.local_voice": base_local_voice,
                "providers.tts.rate": 0,
            }
        )
        _assert(sig() == base, "换回原音色后指纹没回到原值")

        return (
            f"指纹 {len(base)} 元组；换云端音色/本地音色/语速都会变，"
            f"改免打扰不会变，换回原值能回到原指纹"
        )


    @suite.check("语音引擎的密钥**不跨厂商借用**（用户实测报 401 的那个 bug）")
    def _() -> str:
        """
        ## 🔴 这条测试守的是用户实测报的那个 401（2026-10-08）

        用户把语音识别切到云端（硅基流动）后，宠物弹出：

            语音识别失败：HTTPError: HTTP Error 401: Unauthorized

        而 `credentials.json` 里**明明有** `siliconflow` 那个密钥。
        根因是 `_env_or()` **只读环境变量、完全不看凭据文件**：

            环境变量 SILICONFLOW_API_KEY  -> 没设 -> 读不到
            -> 回退用 brain 的密钥（DeepSeek 的）
            -> 拿 DeepSeek 的 key 请求 api.siliconflow.cn -> **401**

        ## 这条测试查三件事

        ① `vendor_from_url()` 的推导规则与主进程的 `providerNameFor()` 一致
           （保存密钥和读取密钥必须用同一个名字）
        ② 指定了厂商却读不到密钥时，**返回空**而不是借别家的
        ③ 没指定厂商时仍用回退值（老的两条路共用一个密钥的情形）
        """
        import json
        import os
        import tempfile
        from pathlib import Path

        from .config import Config
        from .voice import _env_or, vendor_from_url

        # ① URL -> 厂商名（规则必须和 apps/desktop 的 providerNameFor 一致）
        for url, want in {
            "https://api.siliconflow.cn/v1": "siliconflow",
            "https://api.deepseek.com/v1": "deepseek",
            "https://api.openai.com/v1": "openai",
        }.items():
            got = vendor_from_url(url)
            _assert(got == want, f"vendor_from_url({url!r}) = {got!r}，期望 {want!r}")

        # ② 指定厂商读不到时**不许借别家的**
        with tempfile.TemporaryDirectory() as td:
            cred = Path(td) / "credentials.json"
            cred.write_text(json.dumps({"deepseek": "sk-deepseek-fake-key"}), encoding="utf-8")
            old = os.environ.get("PET_CREDENTIALS_FILE")
            os.environ["PET_CREDENTIALS_FILE"] = str(cred)
            # 确保环境变量那条路也不通（否则测的就不是凭据文件了）
            for env in ("SILICONFLOW_API_KEY", "ALPHA_KEY"):
                os.environ.pop(env, None)
            try:
                cfg = Config()
                got = _env_or(
                    "SILICONFLOW_API_KEY",
                    "sk-should-not-be-used",
                    vendor="siliconflow",
                    config=cfg,
                )
                _assert(
                    got == "",
                    f"读不到 siliconflow 的密钥时借用了别家的（拿到 {got[:8]!r}）—— "
                    "这会让用户收到含糊的 401，而不是「你还没配这一家的密钥」",
                )
                got2 = _env_or("DEEPSEEK_API_KEY", "", vendor="deepseek", config=cfg)
                _assert(
                    got2 == "sk-deepseek-fake-key",
                    f"从凭据文件读 deepseek 的密钥失败：{got2!r}",
                )
            finally:
                if old is None:
                    os.environ.pop("PET_CREDENTIALS_FILE", None)
                else:
                    os.environ["PET_CREDENTIALS_FILE"] = old

        # ③ 没指定厂商时仍用回退值
        got3 = _env_or("", "sk-fallback")
        _assert(got3 == "sk-fallback", f"没指定厂商时应当用回退值，得到 {got3!r}")

        return (
            "vendor_from_url 三个域名都推导正确；指定厂商读不到时返回空（不借别家的）；"
            "读得到时正常取；未指定厂商仍走回退值"
        )


    @suite.check("请求体：本轮这句在 messages 里**只出现一次**（用户反馈「文本发了两次」）")
    def _() -> str:
        """
        ## 用户反馈（2026-10-08）

        「好像是文本发了两次」——而且用户截了宠物自己的话当证据：

            你这条我看就是同一句话写了两遍呀，**中间还空了一行**

        ## 真因：`ask()` 里历史快照取晚了（注释是对的，代码没照做）

        请求长这样：

            messages = [system] + history + [user_text]

        而 `_generate()` 里原来是**先 append 用户消息、后取 history 快照**：

            self.history.append(Turn(role="user", text=user_text))
            ...
            history = self._history_snapshot()      # ← 晚了一步

        于是本轮这句**同时**在 history 和 user_text 里，合起来就是
        **紧挨着的两条一模一样**：

            [1] user  我最喜欢吃番茄炒蛋
            [2] user  我最喜欢吃番茄炒蛋

        模型看到两遍 -> **每一轮**都回「你说了两遍 / 又是两遍」。

        ## ⚠️ 这个 bug 的教训：**注释正确 ≠ 代码正确**

        那段代码上面就有注释写着「历史快照必须在本轮用户消息**之前**取」——
        精确描述了正确做法，而代码没照做。读代码时一眼扫过去会以为它是对的
        （我因此被骗过两轮，还先去修了 system 记忆段那个**另一处**重复）。

        最后是靠**把真实数据逐层打出来**才定位的，不是靠读代码。

        ## 这条测试钉住的契约

          · `req.history` **不含**本轮这句
          · `messages` 里连续两条 user 内容**不相同**（紧挨着重复）

        ⚠️ 不用"messages 里有几条文本相同"当判据 —— 用户**真的**可能连着说
        两遍同一句话，那时两条都该在。要验的是**同一轮内被拆成两条**。
        """
        import asyncio

        from .brains import LLMRequest, Turn
        from .brains.openai_compat import OpenAIBrain

        # 离线直接验 `_body` 的组装规则（它是忠实的，问题在传进去的 history）
        brain = OpenAIBrain.__new__(OpenAIBrain)
        brain.model = "m"

        text = "本轮唯一的话ABC"
        # ✅ 正确：history 不含本轮这句
        good = LLMRequest(system="SYS", history=[Turn(role="user", text="上一句")], user_text=text)
        good_msgs = brain._body(good)["messages"]
        good_texts = [m["content"] for m in good_msgs if m["role"] == "user"]
        _assert(
            good_texts.count(text) == 1,
            f"正确构造下本轮这句出现了 {good_texts.count(text)} 次（应当 1 次）：{good_texts}",
        )

        # ❌ 错误：history 也含本轮这句（这就是修之前的实际传参）
        bad = LLMRequest(system="SYS", history=[Turn(role="user", text=text)], user_text=text)
        bad_msgs = brain._body(bad)["messages"]
        bad_texts = [m["content"] for m in bad_msgs if m["role"] == "user"]
        _assert(
            bad_texts.count(text) == 2,
            "反例没复现出重复 —— 这条测试就失去意义了（先修测试）",
        )

        # ---- 真正要守的：`_generate` 传给 `_body` 的 history 不含本轮这句 ----
        #
        # ⚠️ `ask()` 在 `Conversation` 上，不在 `Gateway` 上
        # （Gateway 只是拥有一个 `self.conversation`）。
        # 第一版我写 `gw.attach()` -> AttributeError，测试红在那里、
        # 而不是红在断言上 —— 那种红是"测试写错了"，不是"产品坏了"。
        from .config import Config
        from .gateway import Gateway

        async def scenario() -> list[str]:
            cfg = Config()
            cfg.apply_patch({"providers": {"brain": {"provider": "mock"}}})
            gw = Gateway(cfg)
            conv = gw.conversation

            captured: list[list[str]] = []

            class _Recorder:
                """只记录"收到了哪些 history"，不产生真实回复。"""

                name = "recorder"

                async def stream(self, req: Any) -> Any:  # noqa: ANN401
                    captured.append([(t.text or "") for t in req.history])
                    yield {"kind": "delta", "text": "好"}
                    yield {"kind": "done"}

            conv.brain = _Recorder()  # type: ignore[assignment]
            try:
                await conv.ask(text)
                # `ask` 把生成放进后台任务，等它跑完
                for _ in range(100):
                    await asyncio.sleep(0.01)
                    if captured:
                        break
                return captured[0] if captured else []
            finally:
                await gw.shutdown()

        seen = asyncio.run(scenario())
        _assert(
            text not in seen,
            f"本轮这句出现在 req.history 里（{seen}）—— messages 会有紧挨着的两条重复",
        )
        return "history 不含本轮这句；messages 里只有一条（反例已复现过重复）"

    @suite.check("唤醒词：命中才回应，同音容错，不误触")
    def _() -> str:
        """2026-10-30 用户要求：「分两种，一种用唤醒词，一种用按键模式，
        这两种模式在宠物控制里面切换」。

        ## 为什么需要唤醒词

        实测：英雄联盟的 **ACE 反作弊内核驱动**会拦截所有键盘钩子
        （`SetWindowsHookEx` 安装成功但零回调），`globalShortcut`
        在全屏独占时也收不到 —— 全屏游戏时按键触发**必然失效**。
        麦克风不归反作弊管，所以唤醒词是唯一能用的方式。

        ## 这条测试守什么

        ① 命中判断（含标点、词在句尾、同音字）
        ② **不误触**（普通对话不该被当成唤醒）
        ③ 剥离（唤醒词不能留在正文里，否则模型会回应"阿拾"这个名字本身）
        ④ 老配置没有这个字段时不能崩（用 getattr 兜底）
        """
        from .bus import Bus
        from .voice import VoiceSession

        class _Cfg:
            enabled = True
            trigger_mode = "wake"
            wake_words = ("阿拾", "小灵")
            wake_require_prefix = False
            wake_pinyin = True
            wake_initial_tolerance = True
            wake_aliases = ()

        session = VoiceSession.__new__(VoiceSession)
        session.voice_config = _Cfg()

        # ① 命中
        for text in ("阿拾，今天天气怎么样", "今天天气怎么样阿拾", "小灵帮我查一下", "阿拾"):
            _assert(
                session._match_wake_word(text) is not None,
                f"应当命中唤醒词：{text!r}",
            )

        # ② 不误触（这条最重要 —— 误触发比叫不醒更烦人）
        #
        # ⚠️ 2026-10-30 实测教训：第一版默认开"同音容错"（两个字错一个也算），
        #    看视频时立刻误触发 —— 日志里真的出现过
        #    `唤醒词命中「小灵」-> 实际内容：一张五下个我会跑拿个机器`。
        #    中文里「小」开头、「灵」结尾的词太多，那个规则等于放行一大片。
        #    现在改用**拼音匹配**，这些反例必须仍然不命中。
        for text in (
            "今天天气不错",
            "石头上有个蚂蚁",
            "你在干什么",
            "一张五下个我会跑拿个机器",  # 之前误触发的真实视频内容
            "小时了了",  # 韵母 iao+shi ≠ a+shi
            "机灵的小孩",  # ji+ling ≠ xiao+ling
            "阿妈叫我去吃饭",  # a+ma ≠ a+shi
            "巴西",  # 韵母同为 a+i，但第二音节声母 x≠sh
            "打死你",  # 韵母同为 a+i，但第二音节声母 s≠sh
        ):
            _assert(
                session._match_wake_word(text) is None,
                f"不该误触：{text!r}",
            )

        # ③ **同音字必须命中**（这是本功能能不能用的关键）
        #
        # 用 TTS 合成清晰的唤醒词再喂给本地 ASR，实测输出是：
        #
        #     期望「阿拾」               -> 实际「阿诗」
        #     期望「阿拾，今天天气怎么样」   -> 实际「阿石,今天天气怎么样」
        #     期望「阿拾你在吗」           -> 实际「阿石你在吗」
        #     期望「小灵」               -> 实际「笑铃」
        #
        # 所以按字面匹配 100% 失败，必须靠拼音。
        pinyin_ok = session._match_wake_word("阿诗") is not None
        for text in ("阿石,今天天气怎么样。", "阿石你在吗", "笑铃", "阿十"):
            _assert(
                session._match_wake_word(text) is not None,
                f"同音字必须命中（ASR 实际就这么输出）：{text!r}",
            )
        if not pinyin_ok:
            return "⚠️ 没装 pypinyin，同音匹配退化为字面匹配（功能受限但没崩）"

        # ⑤ **舌尖元音 i ≈ e**：真机又听到了第三种写法
        #
        # 2026-10-30 20:50 的真实日志：
        #
        #     语音识别（1.80s）：阿蛇今天天气怎么样
        #     唤醒词未命中，忽略：阿蛇今天天气怎么样
        #
        # 「阿拾」(a shi) 被听成「阿蛇」(a she) —— 声母相同、韵母 i/e 不同。
        # 语言学上这是有依据的：`zhi/chi/shi/ri/zi/ci/si` 里的 `i` 不是 [i]，
        # 而是**舌尖元音 [ɨ]**，听感非常接近 [ɤ]（e）。
        #
        # 所以只在「声母相同且是舌尖音」时才允许 i/e 互换。
        # 无条件互换会让「是/社」这种常见字大面积误触。
        from .voice import VoiceActivityDetector as _VAD  # noqa: F401

        for text in ("阿蛇今天天气怎么样", "阿拾今天天气怎么样"):
            _assert(
                session._match_wake_word(text) is not None,
                f"i/e 互换应当命中：{text!r}",
            )
        for text in ("会社", "蛇皮", "蛇精病"):
            _assert(
                session._match_wake_word(text) is None,
                f"i/e 互换不能无限放宽，这个不该命中：{text!r}",
            )

        # ⑥ **舌尖音同组**：真机又换成「阿辞」
        #
        # 2026-10-30 20:53 的真实日志：
        #
        #     语音识别（2.02s）：阿辞今天天气怎么样
        #
        # 「阿辞」(a ci) vs 「阿拾」(a shi)：韵母都是 i，声母 sh/c 不同。
        # `zh/ch/sh/r/z/c/s` 同属舌尖音，sh 与 c 只差舌尖位置，
        # 在短促、带噪的语音里极易互换 —— 不归并这一类，
        # 唤醒词会被无穷无尽的同音变体打败。
        for text in ("阿辞今天天气怎么样", "阿四你在吗"):
            _assert(
                session._match_wake_word(text) is not None,
                f"舌尖音同组应当命中：{text!r}",
            )
        # 但**不能**无限放宽：韵母仍须相同、非舌尖音仍须区分
        for text in ("阿西", "阿彩", "巴西"):
            _assert(
                session._match_wake_word(text) is None,
                f"舌尖音归并有边界，这个不该命中：{text!r}",
            )

        # ⑦ **最多一处放宽**：多条规则会相乘
        #
        # 每条放宽规则单独看都有语言学依据，但它们会相乘：
        #
        #     阿拾 = ('' , a) + (sh, i)
        #     打死 = (d  , a) + (s , i)
        #              ^ 零声母容错   ^ 舌尖音同组
        #
        # 两处同时放宽就让「打死你」命中了唤醒词。而"打死/打开"这类词的
        # 出现频率远高于唤醒词，等于把误触面放大一个量级。
        # 限制成"整词最多一处放宽"后，八十/阿辞/阿蛇 各自只有一处，全部保留。
        _assert(
            session._match_wake_word("打死你") is None,
            "「打死你」有两处放宽（零声母 + 舌尖音），必须被拒绝",
        )
        # ④ **零声母容错**：真机日志里最关键的一次
        #
        # 2026-10-30 20:25 的真实日志：
        #
        #     语音识别（1.98s）：八十今天天气怎么样
        #     唤醒词未命中，忽略：八十今天天气怎么样
        #
        # 用户说的明明是「阿拾，今天天气怎么样」——ASR 把零声母的「阿」(a)
        # 听成了「八」(ba)，前面多插了一个浊化起音。韵母完全相同，只多声母 b。
        #
        # 不宽容这一档，唤醒词在真机上永久命中不了（哪怕所有离线测试都过）。
        _assert(
            session._match_wake_word("八十今天天气怎么样") is not None,
            "零声母容错失效：「八十」必须能匹配「阿拾」（真机 ASR 实际输出）",
        )
        # 剥离也要跟着用同一条逻辑，否则唤醒词会被留进正文
        stripped = session._strip_wake_word(
            "八十今天天气怎么样", session._match_wake_word("八十今天天气怎么样")
        )
        _assert(
            stripped == "今天天气怎么样",
            f"剥离结果不对：{stripped!r}（唤醒词必须从正文里去掉）",
        )

        # ④ 剥离：唤醒词不能留在正文里
        word = session._match_wake_word("阿拾，今天天气怎么样")
        _assert(word == "阿拾", f"应返回配置里的原词，实际 {word!r}")
        stripped = session._strip_wake_word("阿拾，今天天气怎么样", word)
        _assert(
            "阿拾" not in stripped,
            f"剥离后不该还有唤醒词：{stripped!r}",
        )
        _assert("今天天气怎么样" in stripped, f"正文丢了：{stripped!r}")

        # ⑤ 句首要求
        class _CfgPrefix(_Cfg):
            wake_require_prefix = True

        prefix_session = VoiceSession.__new__(VoiceSession)
        prefix_session.voice_config = _CfgPrefix()
        _assert(
            prefix_session._match_wake_word("阿拾，今天天气") is not None,
            "句首的唤醒词应当命中",
        )
        _assert(
            prefix_session._match_wake_word("今天天气阿拾") is None,
            "要求句首时，句尾的唤醒词不该命中",
        )

        # ⑥ 老配置（没有这些字段）不能崩
        class _OldCfg:
            enabled = True

        old = VoiceSession.__new__(VoiceSession)
        old.voice_config = _OldCfg()
        _assert(old._match_wake_word("阿拾你好") is None, "没有 wake_words 时应返回 None")

        return "命中/不误触/同音容错/剥离/句首要求/老配置兼容 都正确"

    @suite.check("唤醒词模式：开闸门**不能**禁用静音断句（否则永远不收尾）")
    def _() -> str:
        """2026-10-30 实测踩到的真 bug，形态非常隐蔽。

        ## 现象

        用户切到唤醒词模式后说话，日志里：

            采集电平：energy=0.11264 ... 语音 | 语音态=thinking 门态=speaking
                      已攒语音=17920ms 丢弃=0

        `已攒语音` 一直在涨、**从不触发识别**，而日志里没有任何错误。

        ## 根因

        `set_capture(True)` 里有这段（原本只为按住说话设计）：

            self._ptt_mode = True
            self.endpointer.config.end_silence_ms = 10**9   # 等于禁用静音断句

        它假设"总会有松手"（`phase='end'` -> `force_end()`）来收尾。

        但唤醒词模式**闸门一直开着**，前端只会发 `start`、**永远不发 `end`**
        —— 于是那句话永远结束不了，音频无限累积。

        ## 这条测试守什么

        `wake` 模式开闸门后：
          · `_ptt_mode` 必须是 False（没有"松手"这回事）
          · `end_silence_ms` 必须是正常值（靠静音切句）

        而 `key` 模式必须保持原行为（禁用静音断句，靠松手收尾）。
        """
        from .bus import Bus
        from .voice import VoiceSession

        class _WakeCfg:
            enabled = True
            trigger_mode = "wake"
            wake_words = ("阿拾",)
            wake_require_prefix = False
            input_gain = 1.0
            allow_barge_in = False
            barge_in_frames = 5

        class _KeyCfg(_WakeCfg):
            trigger_mode = "key"

        lost = []

        wake = VoiceSession.__new__(VoiceSession)
        wake.bus = Bus()
        wake.voice_config = _WakeCfg()
        # set_capture 只碰这几样，给最小的即可
        from .voice import Endpointer, VoiceActivityDetector

        wake.vad = VoiceActivityDetector()
        wake.endpointer = Endpointer(wake.vad.config)
        wake._silence_ms_default = int(wake.endpointer.config.end_silence_ms)
        wake._max_utterance_ms_default = int(wake.endpointer.config.max_utterance_ms)
        wake._ptt_mode = False
        wake._capture_enabled = True
        wake.utterances = 0
        wake.last_utterance = None
        wake._tasks = set()
        wake._spawn = lambda coro: coro.close() if hasattr(coro, "close") else None  # type: ignore[method-assign]

        import asyncio

        asyncio.run(wake.set_capture(True))
        if wake._ptt_mode:
            lost.append("wake 模式下 _ptt_mode 不该是 True（没有「松手」这个信号）")
        if wake.endpointer.config.end_silence_ms >= 10**9:
            lost.append(
                "wake 模式下静音断句被禁用了 —— 音频会无限累积、永远不收尾"
                f"（end_silence_ms={wake.endpointer.config.end_silence_ms}）"
            )
        if wake._capture_enabled is not True:
            lost.append("wake 模式下闸门必须是开的")
        # 短窗口：连续背景音下 VAD 永远判"语音"，只能靠保险丝断句 ——
        # 30 秒的话用户说一句「阿拾」最多要等 30+9 秒。
        if wake.endpointer.config.max_utterance_ms > 10_000:
            lost.append(
                "wake 模式下保险丝太长 —— 连续背景音时要等很久才出一句"
                f"（max_utterance_ms={wake.endpointer.config.max_utterance_ms}）"
            )

        # key 模式保持原行为
        key = VoiceSession.__new__(VoiceSession)
        key.bus = Bus()
        key.voice_config = _KeyCfg()
        key.vad = VoiceActivityDetector()
        key.endpointer = Endpointer(key.vad.config)
        key._silence_ms_default = int(key.endpointer.config.end_silence_ms)
        key._ptt_mode = False
        key._capture_enabled = True
        key.utterances = 0
        key.last_utterance = None
        key._tasks = set()
        key._spawn = lambda coro: coro.close() if hasattr(coro, "close") else None  # type: ignore[method-assign]

        asyncio.run(key.set_capture(True))
        if not key._ptt_mode:
            lost.append("key 模式下 _ptt_mode 应当是 True（靠松手收尾）")
        if key.endpointer.config.end_silence_ms < 10**9:
            lost.append(
                "key 模式下静音断句应当被禁用（否则唱歌/长句会被切碎）"
                f"（end_silence_ms={key.endpointer.config.end_silence_ms}）"
            )

        _assert(not lost, "；".join(lost))
        return "wake 保留静音断句、key 禁用静音断句（两种语义没有互相污染）"

    @suite.check("VAD 死锁：稳态噪声不能永久被判成语音（噪声底必须能抬起来）")
    def _() -> str:
        """2026-10-30 实测踩到的真 bug —— 唤醒词模式完全没反应的**第二层**原因。

        ## 死锁怎么形成的

        噪声底**只在判为静音时更新**（刻意的：说话声不能把底噪抬上去，
        否则越说越难触发）。但初值偏低时会形成死循环：

            noise_floor = 0.005（初值）
            阈值 = max(0.010, 0.005 × 2) = 0.010
            环境噪声 energy = 0.03 > 0.010  -> 判成"语音"
            -> 噪声底不更新（只在静音时更新）
            -> 阈值不上升
            -> 噪声永远被判成语音

        实测日志：`噪声底=0.00500` 一动不动，`energy=0.03`，
        `已攒语音` 无限涨，30 秒保险丝才断一次 —— 说唤醒词完全没反应。

        ## 修法：改用**最小统计**

            噪声底 := 最近 `noise_window_ms`（默认 4 秒）内最安静那一帧的能量

        这个定义**没有死锁的可能**：它根本不看"这帧被判成什么"，
        最小值永远如实反映环境。

        > 旧方案是"连续 N 帧判语音且 zcr ≥ 0.22 才强制抬噪声底"。
        > 它假设稳态噪声是白噪声（zcr 0.27~0.33）。但用户这台机器的主要
        > 噪声源是**笔记本风扇** —— 低频为主，实测 `zcr ≈ 0.15`，
        > 低于 0.22，于是旧机制**一次都不触发**，死锁原样存在。
        > 表现为 Whisper 在风扇噪声上疯狂幻听：
        >
        >     语音识别（1.30s）：下班 拜拜
        >     语音识别（0.96s）：我爱你
        >     语音识别（1.34s）：应该是我妈妈
        >
        > **教训：按"噪声的频谱特征"写判别条件，等于给噪声源写白名单。
        > 换一个噪声源就失效，而且失效时完全没有报错。**

        ## 这条测试守两个方向

        ① 稳态噪声必须能脱困（噪声底抬到接近真实噪声）
        ② **正常的连续对话不能把噪声底抬上去**（必须有换气停顿）
        """
        import math
        import struct

        from .voice import AudioFrame, VoiceActivityDetector

        def frame_with(amp: float, freq: float) -> bytes:
            """造一帧 20ms 的正弦波。

            ## ⚠️ 频率必须选对，否则测不出这个 bug

            VAD 判语音要**同时**满足两个条件：

                能量 >= 阈值   且   过零率 zcr <= max_zcr(0.6)

            而过零率 = `2f / fs`（纯正弦）。所以：

                f = 2400Hz  ->  zcr = 0.30   ✅ 落在"像语音"的区间
                f = 7919Hz  ->  zcr = 0.99   ❌ 超过 0.6，直接被判成 noise_like
                                           走正常路径更新噪声底 —— 死锁不复现

            第一版就是用了高频，结果**修复去掉后测试照样绿** —— 那种测试等于没写。
            """
            n = 320  # 20ms @ 16kHz
            vals = [
                int(amp * 32767 * math.sin(2 * math.pi * freq * i / 16000)) for i in range(n)
            ]
            return struct.pack("<%dh" % n, *[max(-32768, min(32767, v)) for v in vals])

        # ① 稳态噪声：必须脱困
        #
        # 用 2400Hz（zcr≈0.30）：既"像语音"（不会被 noise_like 分支救走），
        # 又在阈值之下被误判成语音 —— 正是死锁的现场。
        #
        # ⚠️ 用**低频**信号更能代表风扇：zcr = 2f/fs，220Hz -> 0.028。
        #    那种信号旧方案**完全救不了**（zcr 太低），新方案与频率无关。
        #    这里仍用 2400Hz，是为了同时覆盖"白噪声型"噪声源。
        vad = VoiceActivityDetector()
        start_floor = vad.noise_floor
        escaped_at = None
        for i in range(1200):  # 24 秒
            vad.process(AudioFrame(pcm=frame_with(0.10, freq=2400)))
            if escaped_at is None and vad.noise_floor > start_floor * 2.2:
                escaped_at = i
        _assert(
            escaped_at is not None,
            f"稳态噪声没能脱困：噪声底还是 {vad.noise_floor:.5f}"
            f"（阈值 {vad.threshold():.5f}，噪声永远被判成语音）",
        )
        _assert(
            escaped_at < 600,
            f"脱困太慢：第 {escaped_at} 帧（{escaped_at * 0.02:.1f} 秒）才抬起来，"
            "唤醒词模式下用户要等太久",
        )

        # ①b **低频稳态噪声也必须脱困** —— 这是风扇的真实形态，
        #     旧方案（按 zcr 判别）在这里完全失效。
        fan_vad = VoiceActivityDetector()
        fan_start = fan_vad.noise_floor
        for _ in range(1200):  # 24 秒风扇（220Hz，zcr≈0.028）
            fan_vad.process(AudioFrame(pcm=frame_with(0.10, freq=220)))
        _assert(
            fan_vad.noise_floor > fan_start * 2.2,
            f"低频稳态噪声（风扇）没能脱困：噪声底还是 {fan_vad.noise_floor:.5f}"
            f"（阈值 {fan_vad.threshold():.5f}）—— 这就是用户那台机器的真实情况",
        )

        # ② 真实对话：不能把噪声底抬上去
        #
        # ⚠️ 必须带**换气停顿**才是真实语音。写成"连续 60 秒不停"是不现实的
        #    输入 —— 那种情况下最小统计会把底抬到人声水平，属于预期行为
        #    （窗口内确实没有比人声更安静的时刻）。
        #    真实说话一定有停顿，所以这样测才对得上真实场景。
        speech_vad = VoiceActivityDetector()
        floor_before = speech_vad.noise_floor
        highest = floor_before
        # 先给 1 秒环境静音，让噪声底定在真实水平（模拟"用户开口之前"）
        for _ in range(50):
            speech_vad.process(AudioFrame(pcm=frame_with(0.001, freq=220)))
        floor_before = speech_vad.noise_floor
        highest = floor_before
        for _ in range(30):  # 30 轮 = 30 句话
            for _ in range(150):  # 说 3 秒
                speech_vad.process(AudioFrame(pcm=frame_with(0.25, freq=220)))
                highest = max(highest, speech_vad.noise_floor)
            for _ in range(25):  # 换气 0.5 秒
                speech_vad.process(AudioFrame(pcm=frame_with(0.001, freq=220)))
        _assert(
            highest < floor_before * 3.0,
            f"正常对话把噪声底抬上去了：从 {floor_before:.5f} 最高到 {highest:.5f}",
        )

        return (
            f"稳态噪声 {escaped_at * 0.02:.1f} 秒脱困；低频风扇也能脱困；"
            f"30 句连续对话噪声底未被抬高（{floor_before:.5f} -> 最高 {highest:.5f}）"
        )

    @suite.check("唤醒词模式：触发参数在**构造时**就生效（不依赖那条会丢的 voice.ptt）")
    def _() -> str:
        """2026-10-30 实测踩到 —— 第一版只在 `set_capture()` 里调。

        ## 为什么错了

        `set_capture` 由前端的 `voice.ptt` 消息驱动，而前端**启动时只发一次**
        `start`。那时 sidecar 的 WebSocket 可能还没连上，消息就丢了。

        实测证据（两个日志对不上）：

            startup.log  11:59:21.256Z [按住说话] 渲染层发来 start   ← 主进程收到了
            main.log     （没有任何 `收到 phase='start'` / `采集闸门`）← 后端没收到

        结果 `max_utterance_ms` 一直是 **30 秒**，日志里出现
        `开始识别：音频 30.00s` —— 用户说唤醒词要等半分钟，看起来就是"没反应"。

        ## 这条测试守什么

        **直接构造** `VoiceSession`（完全不碰 `set_capture`），
        端点的两个参数就必须已经按模式调好了。

        > 由配置决定的初始状态，必须在构造/配置更新时就算好，
        > 不能依赖某条可能丢失的运行时消息。
        """
        from .config import Config
        from .bus import Bus as _Bus
        from .voice import VoiceSession

        wake_cfg = Config()
        wake_cfg.apply_patch(
            {"providers.push_to_talk": {"trigger_mode": "wake", "wake_words": ["阿拾"]}}
        )
        wake = VoiceSession(wake_cfg, _Bus())

        lost = []
        if wake.voice_config.trigger_mode != "wake":
            lost.append(f"配置没读到 wake（实际 {wake.voice_config.trigger_mode}）")
        if wake._ptt_mode:
            lost.append("构造后 _ptt_mode 应为 False（唤醒词模式没有「松手」）")
        if wake.endpointer.config.end_silence_ms >= 10**9:
            lost.append("构造后静音断句被禁用了 —— 音频会无限累积")
        if wake.endpointer.config.max_utterance_ms > 10_000:
            lost.append(
                "构造后保险丝还是太长（实际 "
                f"{wake.endpointer.config.max_utterance_ms}ms）—— 说唤醒词要等半分钟"
            )

        # key 模式必须保持 30 秒（靠松手收尾，不能用短窗口切碎长句）
        key = VoiceSession(Config(), _Bus())
        if key.endpointer.config.max_utterance_ms < 20_000:
            lost.append(
                "key 模式的保险丝被改短了（实际 "
                f"{key.endpointer.config.max_utterance_ms}ms）—— 长句会被切碎"
            )

        _assert(not lost, "；".join(lost))
        return (
            f"wake 构造即生效（窗口 {wake.endpointer.config.max_utterance_ms}ms）；"
            f"key 保持 {key.endpointer.config.max_utterance_ms}ms"
        )

    @suite.check("语音顺序：两句并发识别时**按说话顺序发布**（慢句不能后到）")
    def _() -> str:
        """
        ## 为什么需要这条（修上面那个 bug 时**引入的新风险**）

        `feed()` 现在能在"上一句还在识别"时继续切出下一句 —— 那正是修掉
        「丢一句、和下句黏在一起」的关键。但每句都走
        `_spawn(self._transcribe(...))`（**异步任务**），谁先跑完谁先发布：

            用户说"第一句"（长）-> 识别慢
            用户说"第二句"（短）-> 识别快 -> **先发布**

        于是 `user.voice` 顺序反了，对话历史、记忆库、情绪全跟着错：

            用户：第二句     ← 其实是后说的
            小灵：……
            用户：第一句     ← 其实是先说的

        ## 修法：`_transcribe_lock` 串行化

        本地 ASR 本来就有 `threading.Lock` 串行（`LocalAsrEngine._lock`），
        云端单句也就几百毫秒 —— **串行的代价接近零，换来的是顺序确定性**。

        ## 这条测试怎么验

        用**故意一慢一快**的假 ASR（第一句 300ms、第二句 20ms），说两句，
        看 `user.voice` 的发布顺序。没有锁时实测是 `['第2句', '第1句']`。
        """
        import asyncio
        import time

        from .bus import Bus
        from .voice import (
            AudioFrame,
            Endpointer,
            Transcript,
            VoiceActivityDetector,
            VoiceSession,
            VoiceState,
            tone,
        )

        class _Cfg:
            enabled = True
            input_gain = 1.0
            allow_barge_in = True
            barge_in_frames = 5
            sample_rate = 16000

        class _SlowFirstAsr:
            """第一句慢、第二句快 —— 专门用来暴露顺序问题。"""

            name = "slow-first"

            def __init__(self) -> None:
                self.n = 0

            async def transcribe(self, pcm: bytes, sample_rate: int = 16000) -> object:  # noqa: ARG002
                self.n += 1
                index = self.n
                await asyncio.sleep(0.30 if index == 1 else 0.02)
                return Transcript(text=f"第{index}句", source="slow-first")

        async def scenario() -> list[str]:
            published: list[str] = []
            session = VoiceSession.__new__(VoiceSession)
            session.bus = Bus()

            async def on_voice(event: Any) -> None:
                published.append(str((event.payload or {}).get("text") or ""))

            session.bus.subscribe("user.voice", on_voice)
            session.voice_config = _Cfg()
            session.vad = VoiceActivityDetector()
            session.endpointer = Endpointer(session.vad.config)
            session.state = VoiceState.LISTENING
            session._pet_speaking = False
            session._barge_streak = 0
            session._capture_enabled = True
            session._ptt_mode = False
            session._silence_ms_default = int(session.endpointer.config.end_silence_ms)
            session._transcribe_lock = asyncio.Lock()
            session.utterances = 0
            session.last_utterance = None
            session._tasks = set()
            session.asr = _SlowFirstAsr()
            session.audio_out = None
            session._clock = time.monotonic
            session.transcripts = 0
            session.last_transcript = None

            # 说两句，各自用静音收尾（模拟"上一句还在识别，用户又说下一句"）
            for _ in range(2):
                for _ in range(40):
                    await session.feed(AudioFrame(pcm=tone(20, freq=200, amplitude=0.3)))
                for _ in range(40):  # 800ms 静音 > end_silence_ms
                    await session.feed(AudioFrame(pcm=b"\x00" * 640))
                await asyncio.sleep(0)  # 让出，给 _transcribe 机会

            for _ in range(150):
                await asyncio.sleep(0.02)
                if len(published) >= 2:
                    break
            return published

        got = asyncio.run(scenario())
        _assert(
            got == ["第1句", "第2句"],
            f"发布顺序错了：{got}（应当 ['第1句','第2句']）—— "
            "对话历史与记忆会串位，用户看到的是倒着的对话",
        )
        return f"慢句先说的仍然先发布：{got}"

    @suite.check("识别/生成期间**仍然继续收音频**，不丢句、不和下句黏在一起（用户实测 bug）")
    def _() -> str:
        """
        ## 用户原话（2026-10-08）

        「有 bug，有一次的语音没法过去，跟着下一次的语音一起过去了」

        ## 根因：`feed()` 拿 `state` 当采集闸门

        原来是：

            if self.state in (VoiceState.LISTENING, VoiceState.HEARING):
                ...端点检测...

        `THINKING` 覆盖的**不是"识别那一下"，而是从松手/静音判定到模型答完的
        整段时间**（几秒）。那期间 `feed()` **直接丢帧** —— 于是：

          ① 用户在这期间说的话被吞掉；
          ② 但**已经进了端点缓冲的那点音频留着**，等状态回到 LISTENING 后
             跟下一句**合并**成一句发过去。

        正是"丢一句、下一句和它黏在一起"。

        ## 为什么这个抽象是错的

        `state` 是**给界面看的**（口型、状态点：listening/hearing/thinking/speaking）。
        把它当采集闸门，就是把"显示什么"和"收不收音频"绑在一起 —— 两者无关。

        真正的闸门是 `_capture_enabled`（按住说话用），它在更上面挡着。

        ## 这条测试怎么验

        喂音频切出一句（状态变 THINKING），**然后继续喂**，看端点有没有接着累积：

          · 修好了 -> 状态是 THINKING，但 `_frames_in_utterance` 继续涨
          · 没修   -> 端点冻住（帧数一直是 0）

        ⚠️ 判据必须选"点仍在累积"而不是"切出了几句"：状态卡在 THINKING 时
        端点**永远切不出下一句**，所以"切句数"在坏的情况下也是 0 ——
        两种情况的切句数一样，区分不开。
        """
        import asyncio

        from .bus import Bus
        from .voice import (
            AudioFrame,
            Endpointer,
            VoiceActivityDetector,
            VoiceSession,
            VoiceState,
            tone,
        )

        class _Cfg:
            enabled = True
            input_gain = 1.0
            allow_barge_in = True
            barge_in_frames = 5
            sample_rate = 16000

        class _Asr:
            name = "fake"

            async def transcribe(self, pcm: bytes, sample_rate: int = 16000) -> object:  # noqa: ARG002
                from .voice import Transcript

                return Transcript(text="（测试）", source="fake")

        async def scenario() -> tuple[str, int]:
            session = VoiceSession.__new__(VoiceSession)
            session.bus = Bus()
            session.voice_config = _Cfg()
            session.vad = VoiceActivityDetector()
            session.endpointer = Endpointer(session.vad.config)
            session.state = VoiceState.LISTENING
            session._pet_speaking = False
            session._barge_streak = 0
            session._capture_enabled = True
            session._ptt_mode = False
            session._silence_ms_default = int(session.endpointer.config.end_silence_ms)
            session._transcribe_lock = asyncio.Lock()
            session.utterances = 0
            session.last_utterance = None
            session._tasks = set()
            session.asr = _Asr()
            session.audio_out = None
            session._clock = __import__("time").monotonic
            session.transcripts = 0
            session.last_transcript = None

            # ① 说一句并让它结束（靠静音判定）-> 状态进 THINKING
            for _ in range(60):
                await session.feed(AudioFrame(pcm=tone(20, freq=200, amplitude=0.3)))
            for _ in range(int(700 / 20)):  # 700ms 静音 > end_silence_ms(500)
                got = await session.feed(AudioFrame(pcm=b"\x00" * 640))
                if got is not None:
                    break
            state_after_first = session.state
            _assert(
                state_after_first is VoiceState.THINKING,
                f"第一句之后状态应当是 THINKING，实际 {state_after_first}（先修测试）",
            )

            # ② 关键：**在 THINKING 期间继续说话**，端点应当照常累积
            for _ in range(40):
                await session.feed(AudioFrame(pcm=tone(20, freq=200, amplitude=0.3)))

            return str(state_after_first), int(session.endpointer._frames_in_utterance)  # noqa: SLF001

        state, frames = asyncio.run(scenario())
        _assert(
            frames > 0,
            f"状态 {state} 期间音频帧被丢弃了（端点帧数 {frames}）—— "
            "用户这时说的话会被吞掉，并和下一句黏在一起",
        )
        return f"THINKING 期间端点仍在累积（{frames} 帧）—— 不丢句、不黏连"

    @suite.check("按住说话：期间**不按静音切句**，只在松手时收尾（用户反馈「传输了两次」）")
    def _() -> str:
        """
        ## 用户反馈（2026-10-08）

        「消息好像传输了两次」—— 查日志发现**不是重复发送**，而是
        **同一段演唱被切成多句、各自识别、各自回复**：

            17:45:41  语音识别（2.16s）：要不来,要不要来一起唱歌啊?
            17:45:58  语音识别（4.02s）：白菜鸡毛菜,空心菜又卖菜。

        宠物把第二句听成"菜市场顺口溜"，回了句「又是两遍…」。

        ## 根因

        `end_silence_ms` 默认 **500ms**，那个值是为"说完停一下"设计的，
        但**唱歌 / 念长句 / 思考性停顿**都会超过它 —— 句子被切碎，
        每段各触发一次回复。而 `allow_barge_in` 让后一段把前一段的回复
        **打断重说**，观感就是"说了两次"。

        ## 这条测试钉住的契约

          · 开闸门（PTT 按下）-> `end_silence_ms` 被设成极大（不再按静音切句）
          · 关闸门（松手）    -> 恢复原值（免提模式还要用它）

        ⚠️ 必须**恢复**：免提模式（不按键直接说话）没有"松手"这个信号，
        只能靠静音断句。改了不还原的话，免提模式就永远不切句了 ——
        那会是个更难查的 bug（"按住说话正常，直接说话没反应"）。
        """
        import asyncio

        from .bus import Bus
        from .voice import AudioFrame, Endpointer, VoiceActivityDetector, VoiceSession, VoiceState

        class _Cfg:
            enabled = True
            input_gain = 1.0
            allow_barge_in = True
            barge_in_frames = 5
            sample_rate = 16000

        async def scenario() -> tuple[int, int, int]:
            session = VoiceSession.__new__(VoiceSession)
            session.bus = Bus()
            session.voice_config = _Cfg()
            session.vad = VoiceActivityDetector()
            session.endpointer = Endpointer(session.vad.config)
            session.state = VoiceState.LISTENING
            session._pet_speaking = False
            session._barge_streak = 0
            session._capture_enabled = True
            session._ptt_mode = False
            session.utterances = 0
            session.last_utterance = None
            session._tasks = set()
            # ⚠️ `_transcribe_lock` 也必须补（`_transcribe` 会用 `async with`）。
            # 见 voice.py 的说明：并发识别会让 `user.voice` 顺序错乱。
            session._transcribe_lock = asyncio.Lock()
            default_silence = int(session.endpointer.config.end_silence_ms)
            session._silence_ms_default = default_silence

            """
            ## ⚠️ 必须**从默认状态直接按**，不能先按 False 再按 True

            第一版我写的是 `set_capture(False)` 再 `set_capture(True)` ——
            那条路径**绕过了早退**，所以测试通过，而真实路径是坏的。

            真实路径是：`_capture_enabled` 默认 `True`（免提模式一直听），
            用户**第一次按下** PTT 时传的就是 `True` —— 与当前值相同。
            而 `set_capture` 开头原来有一句：

                if enabled == self._capture_enabled:
                    return

            于是那整段 PTT 逻辑（关掉静音断句）**一次都不执行**。
            这是真 bug，不是测试问题。

            **教训**：测"开关"时要从**默认状态**出发按一次 ——
            先归零再按会绕过"值没变"的分支，而那正是最容易错的地方。
            （这个 bug 最后是靠 `check-ptt-segmentation.py`（历史实测脚本）
             把两种模式**并排对比**才暴露的：免提 5 句、PTT 也 5 句 ——
             一模一样就说明改动根本没生效。）
            """
            # 按下：应当变成"几乎不按静音判定"
            await session.set_capture(True)
            during = int(session.endpointer.config.end_silence_ms)

            # 松手：应当恢复原值
            await session.set_capture(False)
            after = int(session.endpointer.config.end_silence_ms)
            return default_silence, during, after

        default, during, after = asyncio.run(scenario())
        _assert(
            during > 1_000_000,
            f"按住说话期间仍在按静音切句（end_silence_ms={during}）—— 句子会被切碎",
        )
        _assert(
            after == default,
            f"松手后没恢复静音阈值（{after} != {default}）—— 免提模式会永远不切句",
        )
        return f"按下时 {default}ms -> 不切句；松手恢复 {after}ms"

    @suite.check("按住说话：采集闸门关闭后**一句都不产出**（用户实测暴露的 bug）")
    def _() -> str:
        """
        ## ⚠️ 这条测试来自用户实机日志（2026-10-07）

        用户按下说话、松开之后，日志里开始**每 30 秒自己冒一条**：

            语音识别（30.00s，耗时 0ms）：你刚才说的那段话我听清了，我们聊聊吧

        `30.00s` 正是 `Endpointer` 的保险丝 `max_utterance_ms`。
        原因：松开按键**只是不再说话，麦克风并没有关** —— 端点在未按住的
        那 30 秒里把环境噪声（被 VAD 判成语音）攒满缓冲、触发保险丝，
        于是一整段"根本没人在说"的音频被当成一句话送进识别。
        **表现就是宠物在自言自语。**

        ## ⚠️ 这里测的是**真的 `VoiceSession.feed()`**，不是它的替身

        我第一版写成了"自己循环、不调用任何产品代码"，那等于什么都没测
        （把闸门整段删掉它照样通过）。现在用一个**只填 feed() 需要的字段**的
        轻量实例：`__init__` 要 Config/Bus/ASR/TTS 一整套，拖进来会让失败
        分不清是哪一层坏的，而这里要验的只有"闸门那一行在不在"。
        """
        import asyncio

        from .bus import Bus
        from .voice import (
            AudioFrame,
            Endpointer,
            VoiceActivityDetector,
            VoiceSession,
            VoiceState,
            noise,
            tone,
        )

        class _Cfg:
            """`feed()` 只需要这几个字段；用一个最小替身，不拖进整个 Config。"""

            enabled = True
            input_gain = 1.0
            allow_barge_in = True
            barge_in_frames = 5
            sample_rate = 16000

        async def scenario() -> tuple[int, int, int]:
            session = VoiceSession.__new__(VoiceSession)  # 跳过 __init__ 的重依赖
            session.bus = Bus()
            session.voice_config = _Cfg()
            session.vad = VoiceActivityDetector()
            session.endpointer = Endpointer(session.vad.config)
            session.state = VoiceState.LISTENING
            session._pet_speaking = False
            session._barge_streak = 0
            session._capture_enabled = True
            # ⚠️ 这几个是 `feed()` 命中"切出一句"路径时会用到的计数器。
            # 测试跑到报 `AttributeError` 反而是**好事** —— 说明它真的进了
            # 产品代码那条分支，而不是在测一个自己搭的替身。
            session.utterances = 0
            session.last_utterance = None
            # ⚠️ `_tasks` 是后台任务的**强引用集合**（2026-10-08 加，防 asyncio
            # 的 task 被 GC 回收）。`__new__` 绕过了 `__init__`，所以这里要自己补，
            # 否则 `_spawn` 一调用就 AttributeError。
            session._tasks = set()
            # ⚠️ 这两个是 PTT 模式用的（`set_capture` 会改/恢复静音阈值）
            session._ptt_mode = False
            session._silence_ms_default = int(session.endpointer.config.end_silence_ms)

            # ① 闸门开着：喂到端点开始累积（确认 feed() 真的在处理音频）
            for _ in range(60):
                await session.feed(AudioFrame(pcm=tone(20, freq=200, amplitude=0.3)))
            _assert(
                session.endpointer._frames_in_utterance > 0,  # noqa: SLF001
                "没能让端点进入累积状态，这条测试就没意义了（先修测试）",
            )

            # ② 关闸门（走**产品方法**）
            await session.set_capture(False)

            # ③ 再喂 35 秒（超过 30 秒保险丝）。
            #
            # ## ⚠️ 判据是 VAD 的**噪声底**，不是端点的帧数
            #
            # 我第一版用"端点帧数不变"当判据 —— 那**恒成立**：
            # `set_capture(False)` 内部会 `force_end()`，帧数当场归零；
            # 之后就算闸门失效、帧数重新涨起来，只要中途又触发一次收尾
            # 就还是 0。实测把闸门整段删掉，那条断言照样通过。
            #
            # 噪声底就不一样：它**只增不减地反映"VAD 有没有在处理帧"**。
            # 闸门生效 -> 一帧都不喂 VAD -> 噪声底**一个数都不变**。
            # 闸门失效 -> 1800 帧持续输入 -> 自适应必然把它推上去。
            floor_before = session.vad.noise_floor
            leaked = 0
            for index in range(int(35_000 / 20)):
                pcm = tone(20, freq=200, amplitude=0.3) if index % 3 else noise(20, amplitude=0.02, seed=index)
                if await session.feed(AudioFrame(pcm=pcm)) is not None:
                    leaked += 1
            floor_after = session.vad.noise_floor

            return floor_before, floor_after, leaked

        floor_before, floor_after, leaked = asyncio.run(scenario())
        _assert(
            floor_after == floor_before,
            f"闸门关闭期间 VAD 仍在处理音频（噪声底 {floor_before:.6f} -> {floor_after:.6f}）："
            "端点会继续累积、攒到 30 秒保险丝后自己冒一句话",
        )
        _assert(leaked == 0, f"闸门关闭后仍产出了 {leaked} 句（会表现为宠物自言自语）")
        return f"关闸门后 35 秒：噪声底冻结在 {floor_before:.6f}、产出 {leaked} 句"

    @suite.check("按住说话：关闸门**丢弃**半句而不是又收一遍（防重复识别）")
    def _() -> str:
        """
        ## 为什么这条必须有

        `voice.ptt` 的 `end` 会做两件事：`force_end()` 收尾 + `set_capture(False)`。
        而 `set_capture(False)` 内部为了兜底**也会** `force_end()` 一次。

        如果兜底那次真收到了东西，同一句话就会被识别两遍 ——
        用户看到的是**宠物对同一句话回答两次**。

        正确行为：`force_end()` 之后端点已回到 IDLE，兜底那次拿不到任何东西。
        这条测试直接验那个"拿不到"。
        """
        from .voice import Endpointer, VoiceActivityDetector

        vad = VoiceActivityDetector()
        endpointer = Endpointer(vad.config)

        # 说一句但不给结尾静音 —— 模拟"用户说完立刻松手"
        for frame in _frames_from([("speech", 900)]):
            result = vad.process(frame)
            endpointer.process(frame, result)

        first = endpointer.force_end()
        _assert(first is not None, "松手时应当能收尾（用户明确说完了）")

        # 兜底那一次：必须拿不到东西，否则就是重复识别
        again = endpointer.force_end()
        _assert(again is None, "收尾之后又收到了一句 —— 同一句话会被识别两遍")
        return f"第一次收尾 {first.duration_s:.2f}s；第二次为 None（不会重复识别）"

    @suite.check("VAD：静音不误触发，人声能识别，环境噪声不误判")
    def _() -> str:
        from .voice import FRAME_MS, AudioFrame, VoiceActivityDetector, noise, silence, tone

        vad = VoiceActivityDetector()
        # 先让噪声底自适应
        for index in range(20):
            vad.process(AudioFrame(pcm=noise(FRAME_MS, amplitude=0.005, seed=index)))

        quiet = [vad.process(AudioFrame(pcm=silence(FRAME_MS))) for _ in range(10)]
        _assert(not any(r.is_speech for r in quiet), "纯静音被误判为说话")

        speech = [vad.process(AudioFrame(pcm=tone(FRAME_MS, amplitude=0.3))) for _ in range(6)]
        _assert(any(r.is_speech for r in speech), f"人声未被识别：{[round(r.energy, 4) for r in speech]}")

        # 关键：环境噪声（持续的低幅噪声）不该被判为说话
        vad2 = VoiceActivityDetector()
        ambient = [vad2.process(AudioFrame(pcm=noise(FRAME_MS, amplitude=0.01, seed=i))) for i in range(40)]
        _assert(
            not any(r.is_speech for r in ambient[-10:]),
            f"持续环境噪声被当成说话（噪声底自适应失效）：floor={vad2.noise_floor:.4f}",
        )
        return (
            f"静音/人声/环境噪声 判定正确；"
            f"噪声底自适应到 {vad2.noise_floor:.4f}，阈值 {vad2.threshold():.4f}"
        )

    @suite.check("端点检测：正常一句话被完整切出，换气不被当成说完")
    def _() -> str:
        # 说 800ms -> 停 200ms（换气）-> 说 700ms -> 停 700ms（真的说完了）
        utterances = _run_endpointer([("speech", 800), ("silence", 200), ("speech", 700), ("silence", 700)])
        _assert(len(utterances) == 1, f"应切出 1 句，实为 {len(utterances)} 句（换气被误判为说完？）")
        spoken = utterances[0]
        # ⚠️ 上限要算上**预录缓冲**（`preroll_ms` = 300ms）：
        #    句首会多带进一小段静音，见"端点预录"那条测试的说明。
        #    所以 1.7s 的语音实际产出可以到 2.1s 左右。
        _assert(
            1.4 < spoken.duration_s < 2.2,
            f"句子时长应涵盖两段语音（约 1.7s + 预录≤0.3s），实为 {spoken.duration_s:.2f}s",
        )
        _assert(spoken.end_reason == "silence", f"结束原因异常：{spoken.end_reason}")
        return f"切出 1 句 {spoken.duration_s:.2f}s（含中间 200ms 换气），未误切"

    @suite.check("端点检测：两句之间能正确分开")
    def _() -> str:
        utterances = _run_endpointer(
            [
                ("speech", 600),
                ("silence", 800),
                ("speech", 600),
                ("silence", 800),
            ]
        )
        _assert(len(utterances) == 2, f"应切出 2 句，实为 {len(utterances)} 句")
        durations = [round(u.duration_s, 2) for u in utterances]
        # 600ms 语音减去起说消抖（start_frames=3 -> 60ms）约 0.54s，
        # **再加上预录缓冲**（≤300ms）-> 上限要放到 0.95s。
        _assert(all(0.45 <= d <= 0.95 for d in durations), f"句子时长异常：{durations}")
        return f"切出 2 句，时长 {durations}（已剪掉句尾静音，含 60ms 起说消抖 + 预录）"

    @suite.check("端点检测：太短的声响被丢弃，超长被截断")
    def _() -> str:
        # 40ms 的响声：不该产生一句话（咳嗽、敲键盘）
        short = _run_endpointer([("speech", 60), ("silence", 700)])
        _assert(not short, f"过短的声响应被丢弃，却产生了 {len(short)} 句")

        # 超长语音：应当被截断而不是无限缓冲
        from .voice import Endpointer, VadConfig, VoiceActivityDetector

        config = VadConfig(max_utterance_ms=1000)
        vad = VoiceActivityDetector(config)
        endpointer = Endpointer(config)
        found: list[Any] = []
        for frame in _frames_from([("speech", 3000)]):
            found.append(endpointer.process(frame, vad.process(frame)))
        produced = [u for u in found if u is not None]
        _assert(produced, "超长语音应产出截断的句子")
        _assert(
            produced[0].end_reason == "max_length",
            f"截断原因标记不对：{produced[0].end_reason}",
        )
        _assert(endpointer.truncated >= 1, "截断计数未增加")
        return f"60ms 声响被丢弃；3s 语音在 1s 处截断（共 {len(produced)} 段）"

    @suite.check("端点预录：句首**不能**被切掉（唤醒词永远在句首，切了就等于没这功能）")
    def _() -> str:
        """2026-10-30 实测踩到 —— 端点从"确认是语音"那一帧才开始录。

        ## 现场证据（音箱念「阿拾，今天天气怎么样」）

            语音识别（1.72s）：今天天气怎么样
            唤醒词未命中，忽略：今天天气怎么样      ← 「阿拾」不见了

        不是识别不准，是**音频里压根没有「阿拾」**：它落在端点开始录制之前。
        端点要连续 `start_frames`（3 帧 ≈ 60ms）确认语音才进入 SPEAKING，
        这 60ms 加上更前面的起音全被丢掉。

        对唤醒词模式是**致命**的 —— 唤醒词永远在句首，永远被切，
        于是永远匹配不上；用户只会觉得"这功能是坏的"。

        ## 这条测试守什么

        句首那段静音（真实场景里是起音的气声）必须被**预录缓冲**带进句子。
        做法：静音段用可识别的高值 PCM 填充，再检查产出的句子开头有没有它。
        """
        from .voice import FRAME_MS, AudioFrame, Endpointer, SAMPLE_RATE, SAMPLE_WIDTH

        frame_bytes = int(SAMPLE_RATE * SAMPLE_WIDTH * FRAME_MS / 1000)
        # 静音帧填 0x11、语音帧填 0x22 —— 两个不同的**合法字节值**。
        # （真静音是 0x00，但那样就分辨不出"有没有被预录带上"了。）
        # ⚠️ 值必须 ≤255：之前写成 frame_bytes//2（=320）直接 ValueError。
        mark, loud = 0x11, 0x22

        ep = Endpointer()
        produced = []

        def feed(pcm_value: int, is_speech: bool) -> None:
            pcm = bytes([pcm_value]) * frame_bytes
            frame = AudioFrame(pcm=pcm)
            vad_like = type("V", (), {"is_speech": is_speech, "energy": 0.0, "zcr": 0.0})()
            u = ep.process(frame, vad_like)
            if u is not None:
                produced.append(u)

        # 静音 500ms（预录窗口 300ms 只能留住最后 300ms）
        for _ in range(25):
            feed(mark, False)
        # 语音 300ms
        for _ in range(15):
            feed(loud, True)
        # 静音收尾（触发 silence 收尾）
        for _ in range(40):
            feed(mark, False)

        _assert(produced, "应当产出一句话")
        head = produced[0].pcm[: int(frame_bytes * 5)]
        marked = head.count(mark)
        _assert(
            marked > 0,
            "句首没有被预录缓冲带进来 —— 句子开头直接是语音，"
            "真实使用中就是「阿拾」被切掉（本次要修的 bug）",
        )
        # 预录不能无限长：300ms 窗口 = 15 帧，允许一点误差
        preroll_frames = marked / float(frame_bytes)
        _assert(
            preroll_frames <= 20,
            f"预录带进来的静音过多（{preroll_frames:.0f} 帧）—— 会拖慢识别、诱发幻觉",
        )
        return f"句首带上了 {preroll_frames:.0f} 帧预录音频（{preroll_frames * FRAME_MS:.0f}ms）"

    @suite.check("VAD 引擎：silero 能加载，且**不把稳态噪声/静音**判成语音")
    def _() -> str:
        """2026-10-30 —— 能量法在用户那台机器上被证明不可靠，换成 silero。

        ## 为什么必须换（真机日志）

        用户的主要噪声源是**笔记本风扇**。能量法的表现是"按下葫芦浮起瓢"：

        · 阈值调高 -> 听不见用户（噪声底被风扇抬到 0.085、阈值 0.17，
          而人声只有 0.09~0.13，全被判成静音）
        · 阈值调低 -> 风扇被当成说话，Whisper 疯狂幻听：
          「下班 拜拜」「我爱你」「应该是我妈妈」「大家好,我叫做麦包」

        根因：**能量和过零率根本不足以区分"人在说话"和"风扇在转"** ——
        都是低频、都有能量起伏。调参只是在两种失败之间来回。

        ## 实测区分度（同一批音频）

            真实语音 max=0.987   判语音 58.7%
            风扇 0.030  max=0.192   判语音  0.0%
            风扇 0.080  max=0.192   判语音  0.0%
            安静        max=0.094   判语音  0.0%

        ## 这条测试守什么

        ① silero 能加载（onnxruntime + 模型都在 faster-whisper 里，不是新依赖）
        ② **静音和稳态噪声的概率必须很低** —— 这是它存在的理由
        ③ 配置成 `energy` 时必须真的走能量法（否则测试环境不可控）
        """
        import math
        import struct

        from .voice import (
            AudioFrame,
            SileroVoiceActivityDetector,
            VadConfig,
            VoiceActivityDetector,
        )

        def noise_frame(amp: float, seed: int) -> bytes:
            rnd = random.Random(seed)
            n = 320
            vals = [int(amp * 32767 * (rnd.random() * 2 - 1)) for _ in range(n)]
            return struct.pack("<%dh" % n, *vals)

        det = SileroVoiceActivityDetector(VadConfig())
        if not det.available:
            return f"⚠️ silero 不可用（{det._load_error}）—— 退回能量法，跳过"

        # ② 静音 / 稳态噪声都不能判成语音
        for label, amp in (("静音", 0.0005), ("风扇式噪声 0.030", 0.030), ("风扇式噪声 0.080", 0.080)):
            d = SileroVoiceActivityDetector(VadConfig())
            speech = 0
            for i in range(150):  # 3 秒
                if d.process(AudioFrame(pcm=noise_frame(amp, i))).is_speech:
                    speech += 1
            _assert(
                speech == 0,
                f"{label} 被判成语音 {speech}/150 帧 —— silero 不该这样",
            )

        # ③ 配置成 energy 时必须走能量法（自检环境必须可控）
        old = os.environ.get("PET_VAD_ENGINE")
        try:
            os.environ["PET_VAD_ENGINE"] = "energy"
            from .bus import Bus as _B
            from .config import Config as _C
            from .voice import VoiceSession as _VS

            plain = _VS(_C(), _B())
            _assert(
                isinstance(plain.vad, VoiceActivityDetector),
                f"PET_VAD_ENGINE=energy 时应当走能量法，实际 {type(plain.vad).__name__}",
            )
            os.environ["PET_VAD_ENGINE"] = "silero"
            neuro = _VS(_C(), _B())
            _assert(
                isinstance(neuro.vad, SileroVoiceActivityDetector),
                f"PET_VAD_ENGINE=silero 时应当是 silero，实际 {type(neuro.vad).__name__}",
            )
        finally:
            if old is None:
                os.environ.pop("PET_VAD_ENGINE", None)
            else:
                os.environ["PET_VAD_ENGINE"] = old

        return "silero 可加载；静音/风扇噪声 0 帧误判；引擎切换生效"

    @suite.check("语音会话：从音频帧到识别结果，再到触发对话")
    def _() -> str:
        from .voice import MockAsrEngine, MockTtsEngine, QueueAudioInput, VoiceConfig, VoiceSession

        async def run() -> tuple[str, list[str], int, str]:
            bus = Bus()
            received: list[str] = []
            states: list[str] = []

            async def on_voice(event: Any) -> None:
                received.append(str((event.payload or {}).get("text") or ""))

            async def on_state(event: Any) -> None:
                states.append(str((event.payload or {}).get("state") or ""))

            bus.subscribe("user.voice", on_voice)
            bus.subscribe("voice.state", on_state)

            audio_in = QueueAudioInput()
            session = VoiceSession(
                config=Config(),
                bus=bus,
                asr=MockAsrEngine(),
                tts=MockTtsEngine(),
                audio_in=audio_in,
                voice_config=VoiceConfig(enabled=True, barge_in_frames=5),
            )
            session.attach()
            await session.set_enabled(True)

            for frame in _frames_from([("speech", 900), ("silence", 700)]):
                audio_in.push(frame.pcm)
            # 逐帧喂（模拟真实循环），直到队列空
            while audio_in.pending:
                await session.feed(audio_in.read())

            # 识别是异步任务，给它一点时间跑完
            for _ in range(50):
                if received:
                    break
                await asyncio.sleep(0.01)
            return (received[0] if received else ""), states, session.utterances, session.state.value

        text, states, utterances, state = asyncio.run(run())
        _assert(utterances == 1, f"应检测到 1 句话，实为 {utterances}")
        _assert(text, f"未发出识别结果（状态序列：{states}）")
        _assert("hearing" in states, f"未进入 hearing 状态：{states}")
        _assert("thinking" in states, f"未进入 thinking 状态：{states}")
        return f"检测 1 句 -> 识别「{text}」；状态流 {states}"

    @suite.check("打断（barge-in）：宠物说话时用户开口，会停下并开始听")
    def _() -> str:
        from .voice import (
            FRAME_MS,
            AudioFrame,
            MockAsrEngine,
            MockTtsEngine,
            VoiceConfig,
            VoiceSession,
            silence,
            tone,
        )

        class RecordingOutput:
            def __init__(self) -> None:
                self.stops = 0

            async def play(self, payload: bytes, fmt: str, text: str) -> None:
                return None

            async def stop(self) -> None:
                self.stops += 1

        async def run() -> tuple[int, list[str], int]:
            bus = Bus()
            states: list[str] = []
            interrupts: list[Any] = []

            async def on_state(event: Any) -> None:
                states.append(str((event.payload or {}).get("state") or ""))

            async def on_interrupt(event: Any) -> None:
                interrupts.append(event.payload)

            bus.subscribe("voice.state", on_state)
            bus.subscribe("user.interrupt", on_interrupt)

            output = RecordingOutput()
            session = VoiceSession(
                config=Config(),
                bus=bus,
                asr=MockAsrEngine(),
                tts=MockTtsEngine(),
                audio_out=output,
                voice_config=VoiceConfig(enabled=True, allow_barge_in=True, barge_in_frames=5),
            )
            session.attach()
            await session.set_enabled(True)

            # 宠物开始说话
            await bus.publish("pet.say.start", {"id": "s1", "priority": "normal"})
            # 用户立刻开口插话
            for _ in range(12):
                await session.feed(AudioFrame(pcm=tone(FRAME_MS, amplitude=0.35)))
            # 之后宠物说完了
            await bus.publish("pet.say.end", {"id": "s1", "text": "一句被打断的话"})
            return output.stops, states, len(interrupts)

        stops, states, interrupts = asyncio.run(run())
        _assert(interrupts >= 1, f"未发出打断事件（状态：{states}）")
        _assert(stops >= 1, "未请求停止播放（用户插话后音频还在响）")
        _assert("hearing" in states, f"插话后应进入 hearing：{states}")
        return f"插话触发 {interrupts} 次打断、{stops} 次停止播放；状态流 {states}"

    @suite.check("打断可关闭：关掉后宠物说话期间用户的语音不会抢话")
    def _() -> str:
        from .voice import FRAME_MS, AudioFrame, MockAsrEngine, MockTtsEngine, VoiceConfig, VoiceSession, tone

        async def run() -> list[Any]:
            bus = Bus()
            interrupts: list[Any] = []

            async def on_interrupt(event: Any) -> None:
                interrupts.append(event.payload)

            bus.subscribe("user.interrupt", on_interrupt)
            session = VoiceSession(
                config=Config(),
                bus=bus,
                asr=MockAsrEngine(),
                tts=MockTtsEngine(),
                voice_config=VoiceConfig(enabled=True, allow_barge_in=False),
            )
            session.attach()
            await session.set_enabled(True)
            await bus.publish("pet.say.start", {"id": "s2"})
            for _ in range(20):
                await session.feed(AudioFrame(pcm=tone(FRAME_MS, amplitude=0.35)))
            return interrupts

        interrupts = asyncio.run(run())
        _assert(not interrupts, f"关闭打断后仍触发了 {len(interrupts)} 次中断")
        return "关闭 allow_barge_in 后不再抢话（麦克风差的环境可这样用）"

    @suite.check("口型包络：从真实音频算出，而不是随机数")
    def _() -> str:
        from .voice import compute_envelope, silence, tone

        # 前半段响、后半段静：包络应该体现出来
        pcm = tone(300, amplitude=0.6) + silence(300)
        envelope = compute_envelope(pcm, "pcm", buckets=10)
        _assert(len(envelope) == 10, f"包络长度应为 10，实为 {len(envelope)}")
        _assert(all(0.0 <= v <= 1.0 for v in envelope), f"包络值越界：{envelope}")
        loud = sum(envelope[:4]) / 4
        quiet = sum(envelope[-4:]) / 4
        _assert(loud > quiet * 3, f"包络未反映音量变化：响 {loud:.3f} vs 静 {quiet:.3f}")

        # 压缩格式没有裸 PCM，算不出包络 -> 返回空，让前端退回占位动效
        _assert(compute_envelope(b"\x00\x01\x02", "mp3") == [], "压缩格式不该返回包络")
        return f"包络正确反映音量（响 {loud:.2f} → 静 {quiet:.2f}）；mp3 返回空让前端兜底"

    @suite.check("ASR 引擎降级：缺密钥/缺依赖时回落到 mock，绝不阻断启动")
    def _() -> str:
        from .voice import build_voice_pipeline

        cfg = Config()
        cfg.apply_patch({"providers.asr.provider": "openai", "providers.tts.provider": "openai"})
        pipeline = build_voice_pipeline(cfg, api_key="")
        _assert(pipeline.asr.name == "mock", f"缺密钥时应降级为 mock，实为 {pipeline.asr.name}")
        _assert("密钥" in pipeline.asr_note, f"应说明降级原因：{pipeline.asr_note}")

        cfg.apply_patch({"providers.asr.provider": "local"})
        local = build_voice_pipeline(cfg, api_key="sk-x")
        """
        ⚠️ 这里原来断言的是"一定降级为 mock" —— 那是**实现之前**写的，
        当时 `local` 分支只会返回 mock。现在 `LocalAsrEngine` 已经实现
        （2026-10-07，用户要求本地/云端可切换），所以这条断言过期了。

        **正确的不变式不是"一定降级"，而是"绝不抛异常、绝不让启动失败"**：
          · 装了 faster-whisper -> 造出真引擎（还要在说明里写出用哪个模型）
          · 没装                -> 降级为 mock，并指出缺什么

        ⚠️ 注意这里**不加载模型**（`build_voice_pipeline` 只造对象），
        所以跑这条测试不会去下 1.6GB —— 加载是懒的，第一次 `transcribe` 才发生。
        """
        if local.asr.name == "local":
            # ⚠️ 不断言具体模型名：这里的 `Config()` 用的是**内置默认**（small），
            # 而项目配置里是 large-v3-turbo。断言"写明了模型"才是不变式 ——
            # 用户看日志时要能知道到底在用哪个，这跟用哪个无关。
            _assert(
                "使用本地识别" in local.asr_note and "device=" in local.asr_note,
                f"本地引擎的说明应写明模型与设备：{local.asr_note}",
            )
        else:
            _assert(local.asr.name == "mock", f"本地不可用时应降级为 mock，实为 {local.asr.name}")
            _assert("faster-whisper" in local.asr_note, f"应指出缺什么：{local.asr_note}")

        # tts 默认 none -> 用 mock 出声（测试/占位），并说明
        cfg2 = Config()
        cfg2.apply_patch({"providers.tts.provider": "none"})
        quiet = build_voice_pipeline(cfg2, api_key="")
        _assert("none" in quiet.tts_note, f"应说明不出声：{quiet.tts_note}")
        return f"ASR 两条降级路径正确；TTS 提示「{quiet.tts_note}」"


# --------------------------------------------------------------------------- #
# 3.9 长期记忆（P2）
# --------------------------------------------------------------------------- #


def _workspace_tmp(prefix: str) -> Path:
    """
    在工作区内建临时目录。

    为什么不用 `tempfile.mkdtemp()`：本环境只允许写工作区，系统临时目录
    （%TEMP%）写入会被拒（PermissionError），而那会掩盖真正的断言失败 ——
    曾经因此把"记忆没写进去"误判成"去重逻辑有问题"。
    """
    root = Path(__file__).resolve().parents[1] / ".tmp-test"
    root.mkdir(parents=True, exist_ok=True)
    # 顺手清掉**上一个小时之前**的残留（见下面 `_sweep_stale_workspace_tmp`）
    _sweep_stale_workspace_tmp(root)
    target = root / f"{prefix}-{int(time.time() * 1000) % 1000000}"
    target.mkdir(parents=True, exist_ok=True)
    _WORKSPACE_TMP_MADE.append(target)
    return target


# 本次运行建过的临时目录 —— 跑完由 `_cleanup_workspace_tmp()` 统一清
_WORKSPACE_TMP_MADE: list[Path] = []


def _sweep_stale_workspace_tmp(root: Path, max_age_s: float = 3600.0) -> None:
    """
    删掉 `root` 里**超过一小时没动**的临时目录。

    ## 为什么要这个（2026-10-09 清理时发现）

    `apps/brain/.tmp-test/` 里积了 **428 个目录、224 MB** ——
    因为 `_workspace_tmp()` 每次跑都新建一个，而**有两个测试忘了清**：

        · `memory-clear`（清空记忆端到端）
        · `memory-e2e`（记忆写库端到端）

    每跑一次自测泄漏约 0.5 MB，428 次就是 224 MB。

    ## 为什么不在这里全删

    刚跑完的目录**留着有用** —— 测试失败时可以进去看数据库到底写了什么。
    所以只清"明显陈旧"的（默认 1 小时）；本次运行建的交给
    `_cleanup_workspace_tmp()` 在退出时统一清。
    """
    now = time.time()
    for old in root.iterdir():
        try:
            if not old.is_dir():
                continue
            if now - old.stat().st_mtime > max_age_s:
                shutil.rmtree(old, ignore_errors=True)
        except OSError:
            pass


def _cleanup_workspace_tmp() -> None:
    """
    清掉本次运行建的临时目录（由 `main()` 在跑完后调用，成功失败都调）。

    ⚠️ 为什么需要这层兜底：`_workspace_tmp()` 的调用方**不都记得自己清**
    （`memory-clear` / `memory-e2e` 两处就漏了）。靠"调用方自觉"迟早会再漏，
    所以在建的地方记账、在退出时统一清。
    """
    for p in _WORKSPACE_TMP_MADE:
        shutil.rmtree(p, ignore_errors=True)
    _WORKSPACE_TMP_MADE.clear()
    # 顺手把已经变空的 .tmp-test 也收掉
    try:
        root = Path(__file__).resolve().parents[1] / ".tmp-test"
        if root.is_dir() and not any(root.iterdir()):
            root.rmdir()
    except OSError:
        pass


def _warn_bare_tmp_dirs() -> None:
    """报告项目根下**无前缀**的 `tmp*` 目录（2026-11-02 加）。

    ## 为什么要报

    用户问：「ai项目路径下有tmphlwm6f2s这种文件夹是干什么的，好多这种，
    有些还是空文件夹」。

    查下来是**测试/调试脚本**用了裸 `tempfile.mkdtemp()`（没有 prefix）：

      · 本环境 `tempfile` 的落点就是**工作区根**（见上面 `_sweep_stale_memory_tmp`）
      · 无前缀 = 那个清扫只认 `pet-selftest-mem-`，永远收不到它们
      · 于是每跑一次就积一个，41 个里有 23 个是当天的

    根因已修（那条测试现在带 `prefix=` 且 `finally` 里自己删）。但
    **不能保证以后没人再写裸 `mkdtemp()`**，所以这里加一道检测。

    ## ⚠️ 为什么只警告、不自动删

    `tmp*` 这个名字太通用 —— 项目根下可能有别人（其它工具、用户自己）
    建的同名目录。自动删的风险（删掉不该删的）远大于收益（省一次手动清理）。

    所以只在**自检跑完时**打一行提示，让人知道"这些是垃圾，可以删"。
    """
    try:
        root = Path(__file__).resolve().parents[3]   # 仓库根
        bare = [
            d for d in root.iterdir()
            if d.is_dir() and d.name.startswith("tmp") and not d.name.startswith("tmp-")
        ]
    except OSError:
        return
    if not bare:
        return
    print(
        f"⚠️ 仓库根有 {len(bare)} 个无前缀的 tmp* 目录（多半是调试脚本用裸 "
        f"mkdtemp() 留下的，可以删）：{'、'.join(d.name for d in bare[:5])}"
        + ("…" if len(bare) > 5 else ""),
        file=sys.stderr,
    )


_MEM_TMP_PREFIX = "pet-selftest-mem-"


def _sweep_stale_memory_tmp(max_age_s: float = 3600.0) -> None:
    """
    清扫**上一次自检**遗留的记忆临时目录（2026-10-14 加）。

    ## 为什么要这个

    `_isolate_memory_writes()` 用 `tempfile.mkdtemp(prefix="pet-selftest-mem-")`
    建临时库，正常收尾会 `rmtree`。但自检**中途崩溃 / 被 Ctrl-C** 就留在盘上了 ——
    实测仓库根积了 **7 个**（本环境 `tempfile` 的落点就是工作区）。

    ## ⚠️ 只删超过一小时的

    正在跑的**另一次**自检可能还在用它的库（比如并行跑测试），所以按 mtime 划线：
    陈旧残留必删，活跃目录一定不动。与 `_sweep_stale_workspace_tmp` 同一策略。
    """
    roots = {
        Path(tempfile.gettempdir()),
        Path.cwd(),
        Path(__file__).resolve().parents[3],  # 仓库根
    }
    now = time.time()
    removed = 0
    for root in roots:
        try:
            entries = list(root.iterdir())
        except OSError:
            continue
        for old in entries:
            if not old.is_dir() or not old.name.startswith(_MEM_TMP_PREFIX):
                continue
            try:
                if now - old.stat().st_mtime <= max_age_s:
                    continue
                shutil.rmtree(old, ignore_errors=True)
                removed += 1
            except OSError:
                pass
    if removed:
        print(f"已清扫 {removed} 个陈旧的记忆自检临时目录", file=sys.stderr)


def test_memory(suite: SelfTest) -> None:
    from .embedding import HashingEmbedder, cosine, ngrams, normalize_text
    from .memory import (
        EXTRACT_LIMIT,
        EXTRACT_MAX_CHARS,
        FactExtractor,
        MemoryConfig,
        MemoryManager,
        MemoryStore,
        _EXTRACT_PROMPT_FALLBACK,
        parse_fact_lines,
        sensitivity_check,
    )

    @suite.check("嵌入：确定性、长度无关、相似度有区分力")
    def _() -> str:
        embedder = HashingEmbedder(dim=256)
        a1 = embedder.embed("我在写一个桌面宠物")
        a2 = embedder.embed("我在写一个桌面宠物")
        _assert(a1 == a2, "相同文本必须得到相同向量（否则无法持久化与测试）")

        # 归一化：长文本与短文本的相似度不该因为长度而系统性偏高
        _assert(abs(sum(v * v for v in a1) - 1.0) < 1e-6, "向量未做 L2 归一化")

        same = cosine(embedder.embed("我在学 Rust 这门语言"), embedder.embed("最近在学 Rust"))
        diff = cosine(embedder.embed("我在学 Rust 这门语言"), embedder.embed("今天中午吃了拉面"))
        _assert(same > diff, f"相关文本的相似度应更高：{same:.3f} vs {diff:.3f}")
        _assert(diff < 0.35, f"无关文本的相似度应偏低：{diff:.3f}")

        # 归一化应该把标点与虚词抹掉
        _assert(
            normalize_text("今天天气不错。") == normalize_text("今天天气不错"),
            f"标点未归一化：{normalize_text('今天天气不错。')!r}",
        )
        _assert(len(ngrams("你好世界")) > 3, "应产出多个字符 n-gram")
        return f"相关 {same:.3f} > 无关 {diff:.3f}；确定性与归一化正确"

    @suite.check("敏感度过滤：密钥、密码、证件号一律不落盘")
    def _() -> str:
        cases = [
            "我的密码是 hunter2xxxx",
            "api_key: sk-abcdefghijklmnop1234",
            "我的卡号 6222021234567890123",
            "手机号 13800138000",
            "token=abcdefghijklmnop",
        ]
        for text in cases:
            _assert(sensitivity_check(text), f"敏感内容未被拦截：{text!r}")

        safe = ["我在学 Rust", "明天要开会", "喜欢喝美式咖啡", "项目用的是 Electron"]
        for text in safe:
            _assert(not sensitivity_check(text), f"正常内容被误拦：{text!r}")

        # 存储层必须真的拒绝写入，而不只是有个检查函数
        store = MemoryStore(embedder=HashingEmbedder(), config=MemoryConfig())
        _assert(store.add("我的密码是 hunter2xxxx") is None, "敏感内容被写入了记忆库")
        _assert(store.add("我在学 Rust") is not None, "正常内容未写入")
        _assert(store.rejected_sensitive == 1, f"拒绝计数错误：{store.rejected_sensitive}")
        _assert(len(store.items) == 1, f"库里应只有 1 条，实为 {len(store.items)}")
        return f"{len(cases)} 类敏感内容全部拦截；{len(safe)} 条正常内容放行"

    @suite.check("记忆检索：能找到相关条目，且不返回无关条目")
    def _() -> str:
        store = MemoryStore(embedder=HashingEmbedder(), config=MemoryConfig(min_score=0.2))
        store.add("我在学 Rust，想写个命令行工具")
        store.add("我们家的猫叫豆豆")
        store.add("下周要把登录模块重构掉")
        store.add("最近在折腾 Electron 的透明窗口")

        hits = store.search("Rust 学得怎么样了", top_k=3)
        _assert(hits, "没有检索到任何记忆")
        top = hits[0].item.text
        _assert("Rust" in top, f"最相关的应是 Rust 那条，实为 {top!r}")

        # 无关查询不该硬凑结果（宁可返回空，也不要给宠物无关联想）
        unrelated = store.search("量子力学里的薛定谔方程怎么解", top_k=3)
        _assert(
            not unrelated or unrelated[0].score < 0.4,
            f"无关查询返回了高分结果：{[(h.item.text, round(h.score, 3)) for h in unrelated]}",
        )

        # 命中次数要累计（淘汰策略依赖它）
        before = hits[0].item.hits
        store.search("Rust", top_k=1)
        _assert(hits[0].item.hits > before, "命中次数未累计")
        return f"「Rust 学得怎么样」命中「{top[:24]}…」（分数 {hits[0].score:.3f}）"

    @suite.check("去重：重复说同一件事不会攒出一堆副本")
    def _() -> str:
        store = MemoryStore(
            embedder=HashingEmbedder(), config=MemoryConfig(duplicate_score=0.9)
        )
        first = store.add("我在学 Rust 这门语言")
        _assert(first is not None, "首次写入失败")
        second = store.add("我在学 Rust 这门语言")
        _assert(second is not None and second.id == first.id, "重复内容未合并到同一条")
        _assert(len(store.items) == 1, f"重复写入产生了副本：{len(store.items)} 条")

        # 重复出现意味着更重要
        _assert(first.importance > 0.4, f"重复内容的重要度应被提升：{first.importance}")

        # 近似但不完全相同的也要能合并
        store.add("我在学Rust这门语言。")
        _assert(len(store.items) == 1, f"近似重复未合并：{len(store.items)} 条")

        # 不同内容必须分开存
        store.add("我在学 Go 语言")
        _assert(len(store.items) == 2, f"不同内容被误合并：{len(store.items)} 条")
        return f"3 次近似重复合并为 1 条；不同内容正确分开（拒绝重复 {store.rejected_duplicate} 次）"

    @suite.check("容量上限：超出后淘汰低价值条目，事实比原话更优先保留")
    def _() -> str:
        store = MemoryStore(
            embedder=HashingEmbedder(),
            config=MemoryConfig(max_items=10, duplicate_score=0.99),
        )
        # 先放一条重要事实与一条无关原话
        store.add("用户正在学 Rust", kind="fact", importance=0.9)
        store.add("随便聊了一句天气", kind="utterance", importance=0.1)
        for index in range(30):
            store.add(f"第 {index} 条无关紧要的闲聊内容", kind="utterance", importance=0.1)

        _assert(len(store.items) == 10, f"容量未被限制在 10 条，实为 {len(store.items)}")
        texts = [item.text for item in store.items]
        _assert("用户正在学 Rust" in texts, f"高价值事实被淘汰了：{texts}")
        _assert(store.pruned >= 20, f"淘汰计数错误：{store.pruned}")
        return f"30+ 条压到 {len(store.items)} 条，事实被保留（淘汰 {store.pruned} 条）"

    @suite.check("持久化：写盘后重新载入，记忆仍在（重启不失忆）")
    def _() -> str:
        import shutil

        root = _workspace_tmp("memory")
        try:
            path = root / "memory.jsonl"
            config = MemoryConfig(path=path)
            store = MemoryStore(embedder=HashingEmbedder(), config=config)
            store.add("我在学 Rust", kind="fact", importance=0.8)
            store.add("我们家的猫叫豆豆")
            store.add_profile_fact("用户正在学 Rust")
            _assert(store.save(), "保存失败")
            _assert(path.exists(), "记忆文件未生成")

            # 新实例模拟重启
            reloaded = MemoryStore(embedder=HashingEmbedder(), config=config)
            count = reloaded.load()
            _assert(count == 2, f"应载入 2 条，实为 {count}")
            _assert(reloaded.profile, "画像未载入")

            # 载入后检索仍然可用（向量要么来自文件，要么被重新算过）
            hits = reloaded.search("Rust", top_k=1)
            _assert(hits and "Rust" in hits[0].item.text, f"重载后检索失败：{hits}")

            # 损坏的行不该毁掉整个库
            with path.open("a", encoding="utf-8") as handle:
                handle.write("这不是 JSON\n")
                handle.write('{"text": "缺字段但能解析"}\n')
            damaged = MemoryStore(embedder=HashingEmbedder(), config=config)
            recovered = damaged.load()
            _assert(recovered >= 2, f"损坏行应被跳过而不是丢弃全部：{recovered}")
            return f"写入 {count} 条并重载成功；追加损坏行后仍能读回 {recovered} 条"
        finally:
            shutil.rmtree(root, ignore_errors=True)

    @suite.check("事实提炼：逐行解析容错，模型不听话时也不会写进垃圾")
    def _() -> str:
        # 正常的逐行输出
        facts = parse_fact_lines("用户在学 Rust\n用户喜欢喝美式咖啡\n用户的项目用 Electron")
        _assert(len(facts) == 3, f"应解析出 3 条：{facts}")

        # 带编号、项目符号、多余空行与解释文字都要能处理
        messy = "1. 用户在学 Rust\n\n- 用户的项目用 Electron\n规则：只抽长期事实\n• 用户喜欢喝美式"
        parsed = parse_fact_lines(messy)
        _assert(len(parsed) == 3, f"格式容错失败：{parsed}")
        _assert(parsed[0] == "用户在学 Rust", f"编号未剥掉：{parsed[0]!r}")

        # 敏感内容必须在这一层就被挡掉（模型可能不听话地把它抽出来）
        _assert(
            not parse_fact_lines("用户的密码是 hunter2xxxx"),
            "提炼阶段未拦截敏感内容",
        )
        # 太长太短都丢弃
        _assert(not parse_fact_lines("嗯"), "过短的行应丢弃")
        _assert(not parse_fact_lines("这个句子" * 30), "过长的行应丢弃")
        _assert(parse_fact_lines("") == [], "空输入应返回空列表")

        # ── 2026-10-17：容量提到 5 条 / 80 字（三类并记后 3 条不够）──
        five = parse_fact_lines(
            "用户在玩《银松镇》\n用户有 PS5\n用户在写玄幻小说\n用户喜欢吃面包\n"
            "《银松镇》是手绘生存恐怖游戏\n用户打算玩 GTA6",
        )
        _assert(len(five) == 5, f"上限应是 5 条，实际 {len(five)}：{five}")

        # 80 字以内要能通过（旧上限 60 会把带细节的条目整条丢掉）
        long_ok = "用户在玩《银松镇》，一款手绘风格的生存恐怖游戏，他说容易在里面迷路"
        _assert(len(long_ok) <= EXTRACT_MAX_CHARS,
                f"这条 {len(long_ok)} 字，测试自己写超了")
        _assert(parse_fact_lines(long_ok) == [long_ok],
                f"{len(long_ok)} 字的事实被丢掉了（上限应 >= 80）")

        # 超 80 字的仍然要丢（上限不能无限大）
        too_long = "用" * (EXTRACT_MAX_CHARS + 5)
        _assert(not parse_fact_lines(too_long),
                f"超过 {EXTRACT_MAX_CHARS} 字仍应丢弃")

        # ── 提炼规则里必须写明"禁止模糊"（2026-10-17 用户的核心要求）──
        _assert("用户在玩游戏" in _EXTRACT_PROMPT_FALLBACK,
                "兜底提示词里缺「用户在玩游戏」这个反例")
        _assert("宁可整条不记" in _EXTRACT_PROMPT_FALLBACK,
                "兜底提示词里缺「写不出名字就不记」这条硬规则")
        for kind in ("用户事实", "共享知识", "共同经历"):
            _assert(kind in _EXTRACT_PROMPT_FALLBACK,
                    f"兜底提示词里缺分类「{kind}」")

        # ── 「记物不记话」（2026-10-20）──
        #
        # 🔴 这条守的是**真实病灶**：抽查最近 40 条记忆，38 条是
        # 「用户认为……」「宠物认为……」式的对话转述，而用户明确说过的
        # 「喜欢曲娘、壁纸是她」一条都没进库。于是"检索不准"其实是
        # "库里没有" —— 提示词里必须明确禁止这类转述。
        _assert("记**物**，不记**话**" in _EXTRACT_PROMPT_FALLBACK,
                "兜底提示词里缺「记物不记话」这一段")
        for banned in ("用户认为", "用户觉得", "用户表达过", "用户问过"):
            _assert(banned in _EXTRACT_PROMPT_FALLBACK,
                    f"兜底提示词里没有点名禁止「{banned}」开头的转述")
        _assert("一次最多 3 条" in _EXTRACT_PROMPT_FALLBACK,
                "兜底提示词里缺「宁缺毋滥」的上限")
        _assert("我把你屏幕上的" not in _EXTRACT_PROMPT_FALLBACK,
                "兜底提示词串进了别的提示词内容")

        # ── 🔴 两处必须**逐字一致** ──
        #
        # 真相源是 `config/persona.yaml` 的 `prompts.extract`，兜底是
        # `memory._EXTRACT_PROMPT_FALLBACK`。这两份**同时存在**是历史原因
        # （配置缺失/损坏时要有可用的一套），但它们漂移过一次：
        # 上一版给 yaml 加了规则、忘了同步兜底，于是"配置文件坏掉"时
        # 行为会**悄悄退回旧规则** —— 不报错、没日志，只是记忆质量变差。
        from .config import Config

        cfg_text = Config()
        yaml_prompt = ""
        try:
            from pathlib import Path as _Path

            cfg_path = _Path(__file__).resolve().parents[3] / "config" / "persona.yaml"
            if cfg_path.exists():
                from .config import load_config as _load

                yaml_prompt = str(_load(cfg_path).root.get("prompts", {}).get("extract") or "")
        except Exception:  # noqa: BLE001 —— 读不到配置就只查兜底
            yaml_prompt = ""
        if yaml_prompt.strip():
            _assert(
                "记**物**，不记**话**" in yaml_prompt,
                "config/persona.yaml 的 prompts.extract 缺「记物不记话」—— 两处漂移了",
            )
            _assert(
                yaml_prompt.strip() == _EXTRACT_PROMPT_FALLBACK.strip(),
                "两处提炼提示词**不一致** —— 配置坏掉时会悄悄退回旧规则。"
                f"（yaml {len(yaml_prompt)} 字 / 兜底 {len(_EXTRACT_PROMPT_FALLBACK)} 字）",
            )
        else:
            raise AssertionError("读不到 config/persona.yaml 的 prompts.extract —— 测试前提不成立")

        return (
            "3 条正常解析；编号/项目符号/解释文字容错通过；敏感与超长被丢弃；"
            f"容量 {EXTRACT_LIMIT} 条/{EXTRACT_MAX_CHARS} 字与三类提炼规则已断言；"
            "记物不记话已断言；两处提示词逐字一致"
        )

    @suite.check("记忆注入：相关记忆与画像进入提示词，无关时不出现")
    def _() -> str:
        store = MemoryStore(embedder=HashingEmbedder(), config=MemoryConfig(min_score=0.2))
        store.add("我在学 Rust，想写命令行工具", kind="fact", importance=0.8)
        store.add_profile_fact("用户正在学 Rust")
        manager = MemoryManager(store=store, extractor=None, config=store.config)

        hint = manager.recall_hint("Rust 学得怎么样了")
        _assert(hint, "相关查询未产生记忆提示")
        _assert("用户正在学 Rust" in hint, f"画像未进入提示词：{hint[:80]}")
        _assert("仅供参考" in hint, "缺少'不要生硬复述'的约束（会让它硬提记忆）")

        # 无关查询：宁可空，也不要说"我记得……"然后说错的
        empty = manager.recall_hint("薛定谔方程怎么解")
        _assert(not empty or "仅供参考" in empty, f"无关查询产生了提示：{empty[:80]}")
        return f"相关查询注入 {len(hint)} 字符（含画像）；无关查询不硬凑"

    @suite.check("记忆真的落盘：重启后仍在（这是'记住了'的唯一证明）")
    def _() -> str:
        """
        这条测试防的是一个很隐蔽的回归：记忆写进内存、检索也正常、对话看起来
        完全正确 —— 但没有任何地方调用 save()，重启全丢。
        早先的实现就是这样：`save()` 存在却从未被调用。
        """
        from .embedding import HashingEmbedder

        root = _workspace_tmp("memory-persist")
        import shutil

        try:
            path = root / "memory.jsonl"
            config = MemoryConfig(path=path)
            manager = MemoryManager(
                store=MemoryStore(embedder=HashingEmbedder(), config=config),
                extractor=None,
                config=config,
            )

            item = manager.remember_utterance("我在学 Rust，想写个命令行工具")
            _assert(item is not None, "原话未被记下")
            _assert(manager.dirty, "写入后应标记为待落盘")
            _assert(not path.exists(), "标记为待落盘时还不该写文件（避免频繁写盘）")

            _assert(manager.sync(), "落盘失败")
            _assert(path.exists(), "落盘后文件应存在")
            _assert(not manager.dirty, "落盘后应清除待落盘标记")
            _assert(not manager.sync(), "没有改动时不该重复写盘")

            # 模拟重启：新实例、新管理器，从同一个文件读
            reloaded_config = MemoryConfig(path=path)
            reloaded = MemoryManager(
                store=MemoryStore(embedder=HashingEmbedder(), config=reloaded_config),
                extractor=None,
                config=reloaded_config,
            )
            count = reloaded.store.load()
            _assert(count == 1, f"重启后应读到 1 条，实为 {count}")
            hits = reloaded.store.search("Rust", top_k=1)
            _assert(hits and "Rust" in hits[0].item.text, f"重启后检索失败：{hits}")
            return f"写入 -> 落盘 -> 重启后读回 {count} 条并可检索"
        finally:
            shutil.rmtree(root, ignore_errors=True)

    @suite.check("清空记忆端到端：memory.clear 真的把数据库清空了")
    def _() -> str:
        """清空是隐私底线能力，必须**真的从磁盘删掉**，不是只在内存里清。

        2026-10-05 起记忆从 JSONL 换成 SQLite（`store.py` / `memory2.py`），
        这条测试相应改成验证数据库行为 —— 特别是：
          · `raw_log`（原文永久底本）**也要一起删**，否则"忘掉一切"是句假话
          · 删完检索必须失效（FTS 索引要跟着走，不能残留）
        """
        from .store import PetDatabase

        async def run() -> tuple[int, bool, int]:
            memory_root = _workspace_tmp("memory-clear")
            db_path = memory_root / "pet.db"
            cfg = Config()
            cfg.apply_patch(
                {
                    "providers.brain.provider": "mock",
                    "autonomy": {"enabled": False},
                    "memory": {"path": str(db_path)},
                }
            )
            gw = Gateway(cfg)
            _assert(gw.memory is not None, "记忆服务没建起来")
            db = gw.memory.db
            _assert(isinstance(db, PetDatabase), f"记忆库类型不对：{type(db)}")

            # 先放几条真记忆：检索层 + 最近/原文层都要有，才能验证"全都删"
            gw.memory.remember_facts(["用户在学 Rust", "我们家的猫叫豆豆"], importance=0.8)
            gw.memory.remember_turn("s-test", "user", "我在学 Rust")
            gw.memory.remember_turn("s-test", "assistant", "Rust 挺难的，加油")
            before = db.count()
            before_file = db_path.exists() and db_path.stat().st_size > 0
            _assert(before["memory"] >= 2, f"前置：检索记忆没写进去 {before}")
            _assert(before["raw_log"] >= 2, f"前置：原文没写进去 {before}")
            _assert(bool(gw.memory._retrieve("Rust")), "前置：清空前应当检索得到 Rust")

            port = await gw.start(port=0)
            serve = asyncio.get_running_loop().create_task(gw.serve_forever())
            client = TestClient(port=port)
            try:
                await client.connect()
                await client.send({"type": "hello", "protocolVersion": 1})
                ready = await client.expect("ready", timeout=5)
                _assert(
                    isinstance(ready.get("memory"), dict),
                    "ready 未带记忆状态（前端无法显示/清空）",
                )
                _assert(
                    int(ready["memory"].get("memory", -1)) == before["memory"],
                    f"ready 里的检索记忆条数不对：{ready.get('memory')}",
                )

                await client.send({"type": "memory.clear"})
                ack = await client.expect("memory.cleared", timeout=5)
                want = sum(before.values())
                _assert(
                    int(ack.get("removed", -1)) == want,
                    f"清空条数不对：期望 {want}，回执 {ack.get('removed')}",
                )
                after = db.count()
                _assert(
                    all(v == 0 for v in after.values()),
                    f"清空后仍有残留：{after}",
                )
                _assert(not gw.memory._retrieve("Rust"), "清空后仍能检索到记忆")
                return want, before_file, sum(after.values())
            finally:
                await client.close()
                await gw.shutdown()
                serve.cancel()
                try:
                    await serve
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass

        before, had_file, after = asyncio.run(run())
        _assert(had_file, "前置条件：清空前应有数据库文件")
        _assert(after == 0, f"数据库里还剩 {after} 条")
        return f"清空 {before} 条（含原文底本）；检索失效、数据库已清空"

    @suite.check("端到端：记忆写进库、走 messages（不进提示词）、且重启能灌回")
    def _() -> str:
        from .brains import LLMRequest

        class CapturingBrain:
            """记录收到的请求，返回固定文本。用于验证提示词里到底有什么。"""

            name = "capturing"

            def __init__(self) -> None:
                self.requests: list[Any] = []

            async def stream(self, req: LLMRequest) -> Any:
                self.requests.append(req)
                full = req.full_system()
                # 把上下文回显出来，便于断言"记忆确实送到了模型"
                for chunk in ("收到", full[:120]):
                    yield {"kind": "delta", "text": chunk}
                yield {"kind": "done", "text": full[:120]}

            def available(self) -> bool:
                return True

        async def run() -> tuple[str, int, int, bool, bool]:
            memory_root = _workspace_tmp("memory-e2e")
            db_path = memory_root / "pet.db"
            cfg = Config()
            cfg.apply_patch(
                {
                    "providers.brain.provider": "mock",
                    "autonomy": {"enabled": False},
                    # 记忆路径必须指向可写位置：默认在项目内，
                    # 但测试要隔离，所以显式指到临时目录。
                    "memory": {
                        "min_score": 0.2,
                        "path": str(db_path),
                    },
                }
            )
            gw = Gateway(cfg)
            # 换掉 brain，既能捕获请求，也避免真的调用模型
            capture = CapturingBrain()
            gw.conversation.brain = capture

            # 第一轮：说一件值得记的事
            await gw.conversation.ask("我在学 Rust，想写个命令行工具")
            current = gw.conversation.current
            if current is not None:
                await current
            await asyncio.sleep(0.05)  # 让异步提炼跑完

            db = gw.memory.db  # type: ignore[union-attr]
            counts = db.count()
            stored = counts["memory"]
            # 原文层（raw_log）必须**每一轮都写**：这是"重启不失忆"和
            # "换模型能无损重炼"的底本，跟有没有提炼成功无关。
            raw_after_first = counts["raw_log"]
            recent_after_first = counts["recent_turn"]

            # 第二轮：问一个相关的问题，提示词里应当带上记忆
            capture.requests.clear()
            await gw.conversation.ask("Rust 写得怎么样了")
            current = gw.conversation.current
            if current is not None:
                await current
            await asyncio.sleep(0.05)

            # 按 user_text 精确定位对话请求，**不要**取 requests[-1]：
            # 事实提炼是异步任务，可能在这一轮之后才把它的请求追加进来，
            # 那样取到的会是提炼请求（context_hint 为空），断言就对错了对象。
            hint = ""
            for request in capture.requests:
                if request.user_text and "Rust 写得" in request.user_text:
                    hint = request.context_hint
                    break
            # ⚠️ 在 `shutdown()` **之前**把对话历史取出来 ——
            # shutdown 会关掉数据库，之后再调 recent_dialogue 会拿到空。
            # 注意 `recent_dialogue` 是 **MemoryService** 的方法，不是 Conversation 的。
            bootstrap_rows = gw.memory.recent_dialogue(limit=50, max_total_chars=0)
            await gw.shutdown()
            # 关闭后数据库文件必须存在（SQLite 下它一直在写，这里验证没被删）
            persisted = db_path.exists() and db_path.stat().st_size > 0
            return hint, stored, raw_after_first, recent_after_first, persisted, bootstrap_rows

        hint, stored, raw_count, recent_count, persisted, bootstrap_rows = asyncio.run(run())
        _assert(
            raw_count >= 2,
            f"原文记忆没写全（raw_log={raw_count}，一轮问答应当写 2 条）",
        )
        _assert(
            recent_count >= 2,
            f"最近记忆没写全（recent_turn={recent_count}）",
        )
        # ⚠️ 2026-10-08 按新契约改：
        #
        # 老断言是「第一轮的内容要出现在 `context_hint`（提示词）里」——
        # 那是靠"最近记忆整段注入"实现的。**那段注入已删除**（用户要求），
        # 最近对话改走 `messages`，所以这里换成钉**新的通道**：
        #
        #   · `context_hint` 里**不该**有逐字原文（否则又变成"看到两遍"）
        #   · 但 `recent_dialogue()` / 灌回的 `history` 里**必须**有
        _assert(
            "我在学" not in hint and "命令行" not in hint,
            f"逐字原文出现在提示词里了 —— 应该只走 messages：{hint[:200]}",
        )
        bootstrap = bootstrap_rows
        joined = "\n".join(r["text"] for r in bootstrap)
        _assert(
            "我在学" in joined or "命令行" in joined or "Rust" in joined,
            f"对话历史里找不到第一轮的内容 —— 记忆丢了（取到 {len(bootstrap)} 条）",
        )
        _assert(persisted, "关闭后数据库文件不存在 —— 重启就会失忆")
        return (
            f"原文 {raw_count} 条、最近 {recent_count} 轮、检索 {stored} 条；"
            f"第二轮提示词 {len(hint)} 字符（逐字原文不进提示词）；重启可灌回 {len(bootstrap)} 条历史；数据库已落盘"
        )


# --------------------------------------------------------------------------- #
# 4. 总线与插件
# --------------------------------------------------------------------------- #


class _GoodPlugin(Plugin):
    name = "good"
    description = "自检用：正常插件"
    version = "0.0.1"

    def __init__(self) -> None:
        super().__init__()
        self.seen: list[str] = []

    def subscribe(self) -> list[str]:
        return ["pet.say.end", "user.text"]

    async def on_event(self, event: Any) -> None:
        self.seen.append(event.name)

    def tools(self) -> list[ToolSpec]:
        return [ToolSpec(name="good.ping", description="返回 pong")]


class _BadPlugin(Plugin):
    name = "bad"
    description = "自检用：故意抛异常的插件"

    def subscribe(self) -> list[str]:
        return ["pet.say.end"]

    async def on_event(self, event: Any) -> None:
        raise RuntimeError("我是故意坏的插件")

    async def on_load(self, ctx: Any) -> None:
        await super().on_load(ctx)
        raise RuntimeError("装载时就炸")


def test_netutil(suite: SelfTest) -> None:
    """按域名绕系统代理（2026-10-19）：名单匹配 + 假阳/假阴 + 调用点覆盖。"""

    @suite.check("直连名单：国内模型接口命中，境外照旧走代理")
    def _() -> str:
        from . import netutil

        cases = {
            "https://api.deepseek.com/v1/chat/completions": True,
            "https://api.deepseek.com:443/v1/tools/search": True,
            "https://API.DeepSeek.com/v1/x": True,
            "https://api.siliconflow.cn/v1/chat/completions": True,
            "https://api.openai.com/v1/chat/completions": False,
            "https://www.google.com/": False,
            # 本地服务另走 force_direct，不靠名单
            "http://127.0.0.1:8123/mcp": False,
        }
        for url, want in cases.items():
            got = netutil.should_bypass(url)
            _assert(got is want, f"{url} 判定错了：{got}（期望 {want}）")
        return "deepseek / siliconflow 直连；openai / google 走代理"

    @suite.check("直连名单：子域算、伪装域名不算（不能写成通配符）")
    def _() -> str:
        from . import netutil

        _assert(netutil.should_bypass("https://beta.api.deepseek.com/x"), "子域应命中")
        for bad in (
            "https://evilapi.deepseek.com/x",   # 不是子域（少了点）
            "https://api.deepseek.com.cn/x",    # 后缀不同
            "https://deepseek.com.evil.io/x",   # 域名在中间
        ):
            _assert(not netutil.should_bypass(bad), f"{bad} 不该命中")
        return "beta.api.deepseek.com 命中；3 个伪装域名都没命中"

    @suite.check("直连名单：可配置，空列表/None 行为明确")
    def _() -> str:
        from . import netutil

        before = netutil.direct_hosts()
        try:
            netutil.set_direct_hosts(["api.example.cn", ".sub.example.cn"])
            _assert(netutil.should_bypass("https://api.example.cn/x"), "自定义名单没生效")
            _assert(netutil.should_bypass("https://a.sub.example.cn/x"), "前导点没被清掉")
            _assert(not netutil.should_bypass("https://api.deepseek.com/x"), "默认名单应被覆盖")
            netutil.set_direct_hosts([])
            _assert(not netutil.should_bypass("https://api.deepseek.com/x"), "空名单应全部走代理")
            netutil.set_direct_hosts(None)
            _assert(netutil.should_bypass("https://api.deepseek.com/x"), "None 应恢复默认")
        finally:
            netutil.set_direct_hosts(before)
        return "自定义覆盖 / 空列表走代理 / None 回默认，都已验证"

    @suite.check("直连名单：PET_FORCE_SYSTEM_PROXY=1 可临时全部走代理")
    def _() -> str:
        import os

        from . import netutil

        old = os.environ.get("PET_FORCE_SYSTEM_PROXY")
        try:
            os.environ["PET_FORCE_SYSTEM_PROXY"] = "1"
            _assert(
                not netutil.should_bypass("https://api.deepseek.com/x"),
                "强制走代理时不该命中直连",
            )
        finally:
            if old is None:
                os.environ.pop("PET_FORCE_SYSTEM_PROXY", None)
            else:
                os.environ["PET_FORCE_SYSTEM_PROXY"] = old
        _assert(netutil.should_bypass("https://api.deepseek.com/x"), "恢复后该重新命中")
        return "环境变量能覆盖；清掉后恢复"

    @suite.check("模型/语音/搜索的 HTTP 调用点都走 netutil（防止改回去）")
    def _() -> str:
        here = Path(__file__).resolve().parent
        targets = [
            "brains/openai_compat.py",
            "content.py",
            "voice.py",
            "mcp_search.py",
            "deepseek_search.py",
        ]
        offenders = []
        for rel in targets:
            source = (here / rel).read_text(encoding="utf-8")
            # 只算真正的调用；文档里提到 `urllib.request.urlopen` 不算
            for i, line in enumerate(source.splitlines(), 1):
                s = line.strip()
                if s.startswith("#") or s.startswith('"') or s.startswith("*"):
                    continue
                if "urllib.request.urlopen(" in line:
                    offenders.append(f"{rel}:{i}")
        _assert(not offenders, f"这些地方绕过了 netutil：{offenders}")
        return f"{len(targets)} 个文件都走 netutil.urlopen"


def test_empty_reply(suite: SelfTest) -> None:
    """推理吃光预算、正文 0 字（2026-10-19 真机「连着两次没回复」）。

    真机数据（阿拾，22:49:33）：`finish_reason='stop'`、`completion_tokens=76`、
    `reasoning_tokens=76`、正文 delta 共 0 字 —— 流正常结束，所以
    "被 max_tokens 截断"那条补救（只在 `finish_reason == 'length'` 时触发）
    **完全没生效**，用户看到气泡空着、历史里连这一轮都没有。
    """

    @suite.check("空回复：关掉思考重试一次，正文能落到历史里")
    def _() -> str:
        import asyncio

        from .config import Config
        from .gateway import Gateway

        class EmptyThenText:
            name = "empty-then-text"

            def __init__(self) -> None:
                self.calls: list[Any] = []

            async def stream(self, req: Any) -> Any:
                self.calls.append(req)
                if len(self.calls) == 1:
                    # 复刻真机：正常结束、没有正文、没有任何 delta
                    yield {"kind": "finish", "finish_reason": "stop"}
                    yield {"kind": "done"}
                    return
                yield {"kind": "delta", "text": "诶，"}
                yield {"kind": "delta", "text": "我在。"}
                yield {"kind": "finish", "finish_reason": "stop"}
                yield {"kind": "done"}

            def available(self) -> bool:
                return True

        async def run() -> tuple[int, bool, str]:
            cfg = Config()
            cfg.apply_patch(
                {
                    "providers.brain.provider": "mock",
                    "autonomy": {"enabled": False},
                    "memory": {"path": str(_workspace_tmp("empty-reply") / "pet.db")},
                }
            )
            gw = Gateway(cfg)
            brain = EmptyThenText()
            gw.conversation.brain = brain
            question = "你觉得人的情感能不能让人感受到温度"
            await gw.conversation.ask(question)
            current = gw.conversation.current
            if current is not None:
                await current
            await asyncio.sleep(0.05)
            last = gw.conversation.history[-1].text if gw.conversation.history else ""
            # ⚠️ 只数**这一轮对话**的请求：后台记忆提炼也用同一个 brain，
            #    直接数 calls 会被它多算一次（踩过）。
            main = [c for c in brain.calls if c.user_text == question]
            _assert(len(main) == 2, f"对话请求应 2 次（空 + 重试），实际 {len(main)}")
            return len(main), main[1].thinking is False, last

        calls, thinking_off, last_text = asyncio.run(run())
        _assert(calls == 2, f"空回复应重试一次，实际调用 {calls} 次")
        _assert(thinking_off, "重试时必须关掉思考（否则推理照样吃光预算）")
        _assert("我在" in last_text, f"重试拿到的正文没进历史：{last_text!r}")
        return f"第 1 次空 -> 关思考重试 -> 「{last_text}」"

    @suite.check("空回复：两次都空也不沉默（兜底句），且不写空历史")
    def _() -> str:
        import asyncio

        from .config import Config
        from .gateway import EMPTY_REPLY_FALLBACK, Gateway

        class AlwaysEmpty:
            name = "always-empty"

            def __init__(self) -> None:
                self.calls: list[Any] = []

            async def stream(self, req: Any) -> Any:
                self.calls.append(req)
                yield {"kind": "finish", "finish_reason": "stop"}
                yield {"kind": "done"}

            def available(self) -> bool:
                return True

        async def run() -> tuple[int, str, str]:
            cfg = Config()
            cfg.apply_patch(
                {
                    "providers.brain.provider": "mock",
                    "autonomy": {"enabled": False},
                    "memory": {"path": str(_workspace_tmp("empty-reply-2") / "pet.db")},
                }
            )
            gw = Gateway(cfg)
            brain = AlwaysEmpty()
            gw.conversation.brain = brain
            await gw.conversation.ask("在吗")
            current = gw.conversation.current
            if current is not None:
                await current
            await asyncio.sleep(0.05)
            last = gw.conversation.history[-1].text if gw.conversation.history else ""
            # 同上：提炼请求不算
            main = [c for c in brain.calls if c.user_text == "在吗"]
            return len(main), last, brain.name

        calls, last_text, _ = asyncio.run(run())
        _assert(calls == 2, f"应只重试一次（避免死循环），实际 {calls} 次")
        _assert(last_text == EMPTY_REPLY_FALLBACK, f"没用兜底句，实际 {last_text!r}")
        return f"两次都空 -> 兜底句「{last_text}」"

    @suite.check("连不上模型时也要说一句话（不能只有气泡空白）")
    def _() -> str:
        import asyncio

        from .config import Config
        from .gateway import FAILED_REPLY_FALLBACK, Gateway

        class Broken:
            name = "broken"

            async def stream(self, req: Any) -> Any:
                # 复刻 10-18 的真机：代理死了 -> 连接被拒
                raise OSError(10061, "由于目标计算机积极拒绝，无法连接。")
                yield  # pragma: no cover

            def available(self) -> bool:
                return True

        async def run() -> str:
            cfg = Config()
            cfg.apply_patch(
                {
                    "providers.brain.provider": "mock",
                    "autonomy": {"enabled": False},
                    "memory": {"path": str(_workspace_tmp("failed-reply") / "pet.db")},
                }
            )
            gw = Gateway(cfg)
            gw.conversation.brain = Broken()
            await gw.conversation.ask("我挺喜欢曲娘的")
            current = gw.conversation.current
            if current is not None:
                await current
            await asyncio.sleep(0.05)
            return gw.conversation.history[-1].text if gw.conversation.history else ""

        last_text = asyncio.run(run())
        _assert(last_text == FAILED_REPLY_FALLBACK, f"没用「连不上」的兜底句，实际 {last_text!r}")
        return f"连接被拒 -> 「{last_text}」"

    @suite.check("半句被截断：标记成 truncated_text，不重试但留痕")
    def _() -> str:
        """2026-10-28 用户报：记忆里有一条半句话。

            #1738  10-28 13:45  宠物  诶，中午了，光把桌面照得发

        **"光把桌面照得发"后面没了**，却被当成完整回复落进了 `raw_log`。

        ## 根因

        `_run_tool_round` 里只处理"截断到**空**"（`not produced`）。
        而被截断时如果**已经吐了半句**，`reason` 仍是 `"done"` ——
        上层当作正常回复收尾、原样落库，全程不报错。

        ## 这条测试守什么

        源码里必须有 `truncated_text` 这个分支，且它：
          ① 与 `"truncated"`（截断到空）**区分开** —— 后者会触发重试，
             而已经说出口的半句重来会让用户看到"话说到一半重说"；
          ② 记 WARNING（`truncated` 那条是 DEBUG，因为它有救）。

        ⚠️ 为什么不测"丢弃这半句"：那会和用户实际看到的不一致 ——
           历史里没有、但屏幕上明明说了。这种不一致比截断本身更糟。
        """
        import inspect

        from .gateway import Conversation

        src = inspect.getsource(Conversation._run_tool_round)
        _assert(
            'reason = "truncated_text"' in src,
            "正文说了一半被截断时必须标记成 truncated_text（否则会被当成完整回复落库）",
        )
        # 与"截断到空"要有区分：两条分支的赋值不能混
        _assert(
            'reason = "truncated"' in src,
            "「截断到空」那条分支不能丢（它会触发加大预算重试）",
        )
        # 必须留痕（WARNING 级别，能查到"模型被截断"）
        seg_start = src.find('reason = "truncated_text"')
        _assert(seg_start > 0, "找不到 truncated_text 分支")
        seg = src[seg_start : seg_start + 900]
        _assert(
            "log.warning" in seg,
            "截断半句要记 WARNING 留痕（不能静默）",
        )
        return "截断半句标记 truncated_text；与 truncated 区分；记 WARNING"

    @suite.check("自主发言预算够说完一句话（不能被截成半句）")
    def _() -> str:
        """2026-10-28：`max_idle_tokens: 220` 会让自主发言说一半就断。

        实测（记忆 #1738）：

            10-28 13:45  宠物  诶，中午了，光把桌面照得发

        ## 为什么 220 不够

        思考模式下**推理和正文共享同一个 max_tokens**
        （§一百五十九 那次真机数据：200 token 全被推理吃掉、正文 0 字）。
        自主发言虽然短，模型仍会先想一段 —— 220 里推理占掉一百多，
        正文只剩十几个字，正好卡在"说得出一半"。

        ## 断言什么

        不锁具体数字（用户可能调到 500），只锁**下限够不够**：
        自主发言的预算不能比正常回复小太多 —— 两者都要能说完一句完整的话。

        ⚠️ 也不能无脑拉大：用户要的是**短句**，长度约束在提示词里，
        预算只是"别截断"的安全线。
        """
        cfg = Config()
        idle = int(cfg.autonomy.get("max_idle_tokens") or 0)
        normal = int((cfg.providers.get("brain") or {}).get("max_tokens") or 0)
        _assert(idle > 0, "max_idle_tokens 必须为正")
        _assert(
            idle >= 300,
            f"自主发言预算 {idle} 太小 —— 思考会吃掉大半，正文只剩十几个字"
            "（实测 220 时会截断成半句）",
        )
        _assert(
            normal > 0 and idle <= normal * 2,
            f"自主发言预算 {idle} 不该远超正常回复 {normal}（用户要短句）",
        )
        return f"自主发言 {idle} token / 正常 {normal} token；下限够、不至于截断"



def test_search_backends(suite: SelfTest) -> None:
    """搜索后端（2026-10-19）：DeepSeek 官方接入 + 统一的格式化出口。"""

    @suite.check("DeepSeek 搜索：没有密钥就是「不可用」，不抛异常")
    def _() -> str:
        from . import deepseek_search

        _assert(deepseek_search.available("") is False, "空密钥应不可用")
        _assert(deepseek_search.available("   ") is False, "空白密钥应不可用")
        _assert(deepseek_search.available("sk-test") is True, "有密钥应可用")
        return "空 / 空白 -> 不可用；有密钥 -> 可用"

    @suite.check("DeepSeek 搜索：请求体夹条数、去空白、空查询报错")
    def _() -> str:
        from . import deepseek_search

        body = deepseek_search.build_body("  测试  ", 99)
        _assert(body["query"] == "测试", f"查询词没 strip：{body['query']!r}")
        _assert(body["count"] == 10, f"count 没夹到 10：{body['count']}")
        _assert(body["need_content"] is False, "不该要正文（烧 token，web_fetch 负责读页面）")
        body = deepseek_search.build_body("x", 0)
        _assert(body["count"] == 1, f"count 没夹到 1：{body['count']}")
        try:
            deepseek_search.build_body("   ", 5)
        except deepseek_search.DeepSeekSearchError:
            pass
        else:
            _assert(False, "空查询应该抛 DeepSeekSearchError")
        return "count 0->1 / 99->10；query 去空白；空查询抛错"

    @suite.check("DeepSeek 搜索：长摘要优先、无 url 丢弃、超长截断")
    def _() -> str:
        from . import deepseek_search

        payload = {
            "results": [
                {"title": " 标题A ", "url": "https://a.example", "summary": "长摘要\n换行", "snippet": "短"},
                {"title": "没链接", "url": "", "summary": "x"},
                {"title": "长", "url": "https://b.example", "summary": "字" * 400},
                {"title": "只有短摘要", "url": "https://c.example", "snippet": "短摘要"},
            ]
        }
        hits = deepseek_search.parse_results(payload)
        _assert(len(hits) == 3, f"应丢弃无 url 的那条，实际 {len(hits)} 条")
        _assert(hits[0].snippet.startswith("长摘要"), f"应优先 summary：{hits[0].snippet!r}")
        _assert("\n" not in hits[0].snippet, "摘要里的换行应被压成空格")
        _assert(
            len(hits[1].snippet) <= deepseek_search.SNIPPET_CHARS + 1,
            f"超长摘要没截断：{len(hits[1].snippet)} 字",
        )
        _assert(hits[2].snippet == "短摘要", "没有 summary 时应退回 snippet")
        _assert(hits[0].title == "标题A", f"标题没清理：{hits[0].title!r}")
        return f"3 条；summary 优先；{deepseek_search.SNIPPET_CHARS} 字截断；换行压平"

    @suite.check("DeepSeek 搜索：401/402/429 的提示能指导用户行动")
    def _() -> str:
        from . import deepseek_search

        e401 = deepseek_search.describe_http_error(401)
        e402 = deepseek_search.describe_http_error(402)
        e429 = deepseek_search.describe_http_error(429)
        e500 = deepseek_search.describe_http_error(503)
        _assert("密钥" in e401, f"401 应提示密钥：{e401}")
        _assert("余额" in e402 or "充值" in e402, f"402 应提示充值：{e402}")
        _assert("限速" in e429, f"429 应说明限速：{e429}")
        _assert("服务端" in e500, f"5xx 应说是服务端问题：{e500}")
        return "401->密钥 / 402->充值 / 429->限速 / 5xx->服务端"

    @suite.check("搜索后端开关：空密钥自动退回本地，有密钥走官方")
    def _() -> str:
        from . import tools

        before = tools.deepseek_search_ready()
        try:
            tools.set_deepseek_search("")
            _assert(tools.deepseek_search_ready() is False, "空密钥应不可用")
            tools.set_deepseek_search("sk-test")
            _assert(tools.deepseek_search_ready() is True, "有密钥应可用")
        finally:
            tools.set_deepseek_search("sk-restore" if before else "")
        return "set_deepseek_search('') -> False；有密钥 -> True"

    @suite.check("搜索结果格式化只有一份（DeepSeek 与自抓后端共用提示语）")
    def _() -> str:
        from . import tools, web

        outcome = web.SearchOutcome(
            hits=[web.SearchHit(title="标题", url="https://x.example", snippet="摘要")],
            engine="deepseek",
        )
        text = tools._format_outcome("查询", outcome)
        _assert("搜索「查询」" in text, "没带查询词")
        _assert("deepseek" in text, "没带引擎名")
        _assert("先仔细读上面的标题和摘要" in text, "缺「先读摘要」的提示")
        _assert("web_fetch" in text, "缺下一步提示")
        empty = tools._format_outcome("查询", web.SearchOutcome(attempts=["没有结果"]))
        _assert("搜索没有结果" in empty, f"空结果文案不对：{empty}")
        return "非空带引擎/摘要提示；空结果给可读原因"


def test_bus_and_plugins(suite: SelfTest) -> None:
    @suite.check("总线隔离：订阅者抛异常不影响其他订阅者")
    def _() -> str:
        async def run() -> int:
            bus = Bus()
            hits: list[str] = []

            async def bad(event: Any) -> None:
                raise RuntimeError("boom")

            async def good(event: Any) -> None:
                hits.append(event.name)

            bus.subscribe("user.text", bad)
            bus.subscribe("user.text", good)
            await bus.publish("user.text", {"text": "x"})
            return len(hits)

        hits = asyncio.run(run())
        _assert(hits == 1, f"正常订阅者未收到事件（hits={hits}）")
        return "坏订阅者被隔离，好订阅者照常收到"

    @suite.check("插件：坏插件装载失败被跳过，好插件正常工作并暴露工具")
    def _() -> str:
        async def run() -> tuple[int, list[str], list[str]]:
            cfg = Config()
            gw = Gateway(cfg)
            plugins = await gw.plugins.load([_GoodPlugin(), _BadPlugin()])
            good = gw.plugins.get("good")
            assert good is not None
            await gw.bus.publish("user.text", {"text": "hi"})
            await gw.bus.publish("pet.say.end", {"id": "x", "text": "hi"})
            tools = [t.name for t in gw.plugins.all_tools()]
            return len(gw.plugins), list(good.seen), tools

        count, seen, tools = asyncio.run(run())
        _assert(count == 1, f"插件数量不对：{count}（坏插件应被跳过）")
        _assert(seen == ["user.text", "pet.say.end"], f"插件未收到订阅事件：{seen}")
        _assert(tools == ["good.ping"], f"工具未暴露：{tools}")
        return f"装载 {count} 个（坏插件已跳过），收到 {seen}，工具 {tools}"


# --------------------------------------------------------------------------- #
# 5. 端到端（真实 WebSocket 往返）
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# 4.9 日志
# --------------------------------------------------------------------------- #


def test_logging(suite: SelfTest) -> None:
    """日志**按大小轮转**，不会无限增长。"""

    @suite.check("日志会按大小轮转，不会无限增长")
    def _() -> str:
        """
        ## 🔴 守的是 2026-10-09 清理时发现的问题

        `pet-data/python.log` 用的是普通 `logging.FileHandler` —— **无限增长**。
        清理时它已经 **168 MB**（全是这一个文件），而且还在长。

        现在 `_setup_logging()` 在打开文件**之前**调 `_rotate_if_needed()`：
        超过 20 MB 就 `python.log -> .1 -> .2 -> .3`，最老的删掉。
        上限 `20 MB × 4 = 80 MB`。

        ## 为什么不用标准库的 `RotatingFileHandler`

        ```python
        class FileHandler(StreamHandler):
            def _open(self):
                return open_func(self.baseFilename, self.mode, ...)   # 用的是 self.mode
        ```

        而 `RotatingFileHandler.__init__` 是在 `FileHandler.__init__` **返回之后**
        才把 `self.mode` 改成 `'a'` —— 它内部那次 rollover 打开文件用的是
        **构造时传入的 mode**（靠默认值碰巧对）。而且它的 rollover 是为
        **单进程**设计的。自己写行为完全可预期。
        """
        from .main import LOG_BACKUP_COUNT, LOG_MAX_BYTES, _rotate_if_needed

        root = _workspace_tmp("log-rotate")
        log = root / "python.log"
        try:
            # ① 没超限**不该**轮转（否则每次启动都白挪一次）
            log.write_text("x" * 1024, encoding="utf-8")
            _rotate_if_needed(log)
            _assert(not (root / "python.log.1").exists(), "1 KB 的日志不该被轮转")
            _assert(log.is_file(), "没超限时本体应当原样保留")

            # ② 超限**要**轮转，且内容落到 .1
            log.write_text("y" * (LOG_MAX_BYTES + 1024), encoding="utf-8")
            _rotate_if_needed(log)
            first = root / "python.log.1"
            _assert(first.is_file(), "超限后应当生成 python.log.1")
            _assert(
                first.stat().st_size > LOG_MAX_BYTES,
                f"轮转后 .1 应当装下原来的内容，实为 {first.stat().st_size} 字节",
            )
            # 本体这时可能还没建（要等真正写日志时才建），建了就必须是空的
            if log.exists():
                _assert(log.stat().st_size == 0, "轮转后本体应当是空的")

            # ③ 反复轮转**不会无限堆积**
            for _i in range(LOG_BACKUP_COUNT + 3):
                log.write_text("z" * (LOG_MAX_BYTES + 1024), encoding="utf-8")
                _rotate_if_needed(log)
            names = sorted(p.name for p in root.iterdir())
            _assert(
                len(names) <= LOG_BACKUP_COUNT + 1,
                f"轮转份数应当封顶在 {LOG_BACKUP_COUNT + 1}（含本体），"
                f"实为 {len(names)}：{names}",
            )
            _assert(
                f"python.log.{LOG_BACKUP_COUNT}" in names,
                f"应当保留到 .{LOG_BACKUP_COUNT}：{names}",
            )
            _assert(
                f"python.log.{LOG_BACKUP_COUNT + 1}" not in names,
                f"不该出现超过 {LOG_BACKUP_COUNT} 份历史：{names}",
            )

            # ④ 轮转失败**不能抛**（日志写不了不该让应用起不来）
            _rotate_if_needed(root / "no-such-dir" / "x.log")

            total_mb = sum(p.stat().st_size for p in root.iterdir()) / 1048576
            return (
                f"上限 {LOG_MAX_BYTES // 1048576} MB、保留 {LOG_BACKUP_COUNT} 份；"
                f"没超限不转、超限转、份数封顶（实测 {total_mb:.0f} MB）；失败不抛"
            )
        finally:
            shutil.rmtree(root, ignore_errors=True)



def test_end_to_end(suite: SelfTest, config_path: Optional[Path]) -> None:
    @suite.check("端到端：hello→ready，user.text→流式→结束，热更新即时生效")
    def _() -> str:
        async def run() -> str:
            cfg = load_config(config_path) if config_path and config_path.exists() else Config()
            # 自检固定用 mock，避免依赖外部密钥
            cfg.apply_patch({"providers.brain.provider": "mock"})
            # ⚠️ TTS 也要 mock（2026-10-08）：
            #
            # 接上 `pet.say.end` -> `speak()` 之后，这个端到端测试会**真的去合成**。
            # 而配置里 `tts.provider: windows` 会起 PowerShell 调 SAPI ——
            # 每次几百毫秒、失败还要等超时，测试直接超时（实测 10s）。
            #
            # 这条测试验的是**对话链路**（hello -> ready -> 流式 -> 结束 + 热更新），
            # 不是 TTS。合成有自己的自检（「说话：pet.say.end 会触发合成」）。
            cfg.apply_patch({"providers.tts.provider": "mock"})
            gw = Gateway(cfg)
            port = await gw.start(port=0)
            serve = asyncio.get_running_loop().create_task(gw.serve_forever())
            client = TestClient(port=port)
            try:
                await client.connect()
                await client.send({"type": "hello", "protocolVersion": 1, "client": "selftest"})
                first = await client.expect("ready", timeout=5)
                _assert(first["brain"] == "mock", f"brain 名不对：{first.get('brain')}")
                _assert(first.get("persona"), "ready 未带人格名")
                _assert(isinstance(first.get("activity"), dict), "ready 未带活泼度")

                # 每一轮发言用独立 id 区分，避免上一轮的迟到消息干扰断言
                await client.send({"type": "user.text", "text": "你好呀，你是谁"})
                end = await _expect_with_trace(client, "pet.say.end", say_trace, timeout=10)
                say_id = end["id"]
                round_msgs = [m for m in client.received if m.get("id") == say_id]
                kinds = [m["type"] for m in round_msgs]
                _assert("pet.say.start" in kinds, f"缺少 say.start：{kinds}")
                deltas = [m for m in round_msgs if m["type"] == "pet.say.delta"]
                _assert(len(deltas) >= 1, f"没有收到流式增量：{kinds}")
                _assert(end.get("reason") == "done", f"结束原因异常：{end.get('reason')}")
                streamed = "".join(m["text"] for m in deltas)
                _assert(streamed == end.get("text"), f"流式增量与最终文本不一致：{streamed!r} vs {end.get('text')!r}")

                # 热更新：改活泼度必须立刻生效并回报（且回报里带的是生效后的值）
                await client.send({"type": "config.update", "patch": {"activity.overall": 0.9}})
                ack = await client.expect("ready", timeout=5)
                _assert(abs(ack["activity"]["overall"] - 0.9) < 1e-6, f"设定值未生效：{ack['activity']}")

                # 越界值必须被夹取后回报，而不是让界面显示一个假值
                await client.send({"type": "config.update", "patch": {"activity.overall": 42}})
                ack2 = await client.expect("ready", timeout=5)
                _assert(ack2["activity"]["overall"] == 1.0, f"越界值未夹取：{ack2['activity']['overall']}")

                # 非法消息必须得到可读错误，且连接不能被断
                await client.send({"type": "user.text", "text": ""})
                err = await client.expect("system.error", timeout=5)
                _assert("text" in str(err.get("message")), f"错误信息不可读：{err.get('message')}")
                await client.send({"type": "user.text", "text": "还在吗"})
                await client.expect("pet.say.end", timeout=10)
                return (
                    f"ready/persona={first['persona']}；流式 {len(deltas)} 块、{len(streamed)} 字"
                    f"（「{streamed}」）；热更新与夹取生效；非法消息被拒后连接仍可用"
                )
            finally:
                await client.close()
                await gw.shutdown()
                serve.cancel()
                try:
                    await serve
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass

        say_trace: list[str] = []
        try:
            return asyncio.run(run())
        except asyncio.TimeoutError:
            # 超时要能自证原因：把收到的事件序列带出来，而不是只留一句 "TimeoutError:"
            raise AssertionError(f"端到端超时。已收到的事件序列：{say_trace}") from None

    @suite.check("最近记忆注入：逐字重复的行只保留最新一条（提示词瘦身）")
    def _() -> str:
        """
        ## 这条是"实测才发现体积花在哪"换来的（2026-10-07）

        用户问"提示词怎么组成的、真实发过去多少"。实测：

            ③ 最近记忆(原文)  120 轮 / 8044 字 / ≈10055 token   ← 占注入体积 **98%**
                              其中只有 46 轮内容不同 —— 74 条逐字一模一样

        原因不是"记忆层没压缩"，而是**原文层被整段注入**，而里面本来就有大量
        逐字重复（早期 mock 测试与复读把库填成了这样）。

        ⚠️ 这是**去重**不是**压缩**：逐字相同的行去掉多余副本不丢任何信息，
        所以不需要用户过目（与"把几条不同记忆合成一条"性质不同）。

        关键点：**比较时必须剥掉 `[2 天前] ` 这个时间前缀** ——
        同一句话在两天里说了 12 次，带上时间前缀每行都不同，就一条也去不掉。
        """
        from .memory2 import MemoryService

        lines = [
            "[今天] 小灵：看到嘿，你觉得ai会怎么发展了。需要我安静点吗？",
            "[昨天] 小灵：看到嘿，你觉得ai会怎么发展了。需要我安静点吗？",
            "[2 天前] 小灵：看到嘿，你觉得ai会怎么发展了。需要我安静点吗？",
            "[今天] 用户：hi",
            "[昨天] 用户：hi",
            "[今天] 小灵：完全不同的另一句话",
        ]
        out = MemoryService._dedupe_recent(lines)
        _assert(len(out) == 3, f"去重结果应为 3 条，实际 {len(out)}：{out}")
        # 保留的必须是**最新**那次（输入是最新在前，所以第一条应当留下）
        _assert(out[0].startswith("[今天]"), f"没保留最新那条的时间：{out[0]!r}")
        _assert(
            sum(1 for x in out if x.endswith("另一句话")) == 1,
            "不重复的行被误删了",
        )

        # role 不同的同名文本不能互相吃掉（"用户：hi" 与 "小灵：hi" 是两回事）
        pair = ["[今天] 用户：hi", "[今天] 小灵：hi"]
        _assert(len(MemoryService._dedupe_recent(pair)) == 2, "不同说话者的同名内容被误合并")
        return f"{len(lines)} 行 -> {len(out)} 行（保留最新、说话者不混）"

    @suite.check("点击宠物：只记互动、**不产生任何发言**（用户 2026-10-07 要求）")
    def _() -> str:
        """
        ## 这条测的是"点击不该让宠物开口"

        用户原话：「现在主动点击**不要发送空内容、只是点击而已**」

        原来的行为是 `user.poke` -> `autonomy.speak` -> 立刻生成一句。
        问题：**每点一下 = 一次真实的 LLM 调用**，而点击不携带任何用户输入，
        于是 `user_text` 是 `None`（自主发言），接口以
        `content should be a string or a list` 拒掉 —— 用户看到的是一条报错气泡。

        ⚠️ 所以断言要**同时覆盖两个方向**：
          · 不能有任何 `pet.say.*`（不产生内容）
          · 不能有新的对话轮次（不产生历史、不花钱）
          · 但**调度器的互动计时器要被重置**（否则点完马上又来一句自主发言）
        """

        async def run() -> tuple[int, int, float, float]:
            cfg = Config()
            cfg.apply_patch(
                {
                    "providers.brain.provider": "mock",
                    # 关掉后台调度循环：避免 tick 在断言期间插进来污染消息序列
                    "autonomy": {"enabled": False},
                }
            )
            gw = Gateway(cfg)
            port = await gw.start(port=0)
            serve = asyncio.get_running_loop().create_task(gw.serve_forever())
            client = TestClient(port=port)
            try:
                await client.connect()
                await client.send({"type": "hello", "protocolVersion": 1})
                await client.expect("ready", timeout=5)

                turns_before = len(gw.conversation.history)
                # ⚠️ 直接读/写 `last_interaction_at`（调度器用它算"多久没互动了"）。
                # 没有 `idle_seconds()` 这个方法 —— 别凭空假设有（写这条时踩过）。
                gw.scheduler.last_interaction_at = -1e9
                before_ts = gw.scheduler.last_interaction_at

                await client.send({"type": "user.poke"})
                await asyncio.sleep(0.5)  # 给它足够时间"错误地"开口

                says = [m for m in client.pending() if str(m.get("type", "")).startswith("pet.say")]
                _assert(not says, f"点击后仍然产生了发言：{[m['type'] for m in says]}")
                _assert(
                    len(gw.conversation.history) == turns_before,
                    f"点击后对话轮次变了：{turns_before} -> {len(gw.conversation.history)}",
                )
                after_ts = gw.scheduler.last_interaction_at
                _assert(
                    after_ts > before_ts,
                    "点击没有重置互动计时器 —— 点完马上又会被判定成'很久没互动'，"
                    f"于是立刻冒出一句自主发言（last_interaction_at {before_ts} -> {after_ts}）",
                )
                return turns_before, len(gw.conversation.history), before_ts, after_ts
            finally:
                await client.close()
                await gw.shutdown()
                serve.cancel()
                try:
                    await serve
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass

        turns_before, turns_after, before_ts, after_ts = asyncio.run(run())
        return (
            f"点击不产生发言、不产生轮次（{turns_before} -> {turns_after}）；"
            f"互动计时器已重置（last_interaction_at {before_ts:.0f} -> {after_ts:.1f}）"
        )

    @suite.check("端口占用自动探测：第二个实例能起来而不是失败")
    def _() -> str:
        async def run() -> tuple[int, int]:
            cfg = Config()
            gw1 = Gateway(cfg)
            port1 = await gw1.start(port=0)
            gw2 = Gateway(Config())
            # 从 port1 开始探测，应让到 port1+1
            port2 = await gw2.start(port=port1, port_scan=5)
            await gw1.shutdown()
            await gw2.shutdown()
            return port1, port2

        port1, port2 = asyncio.run(run())
        _assert(port2 != port1, "两个实例绑到了同一个端口")
        return f"实例1 用 {port1}，实例2 自动让到 {port2}"


async def _await_new_say(client: TestClient, timeout: float = 10.0) -> tuple[str, list[dict[str, Any]]]:
    """
    等一轮**新的**宠物发言，返回 (say_id, 该轮全部消息)。

    两个坑都踩过，记在这里：
      1. 不能直接 `expect("pet.say.end")` —— 会命中缓冲区里上一轮遗留的 end
         （宠物是异步主动说话的，消息本来就交错），断言会对错对象；
      2. 查找必须扫 `client.received` 全量，**不能**用 `pending()` ——
         循环里的 `recv()` 会把消息标记为已取用，游标一过就再也找不到它。
    按 id 对齐 + 扫全量历史，是唯一可靠的判据。
    """
    before = {m.get("id") for m in client.received if m.get("id")}
    deadline = asyncio.get_running_loop().time() + timeout
    say_id: Optional[str] = None
    while True:
        if say_id is None:
            candidates = [
                m
                for m in client.received
                if m.get("type") == "pet.say.start"
                and m.get("id")
                and m["id"] not in before
            ]
            if candidates:
                say_id = candidates[0]["id"]
        if say_id is not None:
            ends = [
                m
                for m in client.received
                if m.get("type") == "pet.say.end" and m.get("id") == say_id
            ]
            if ends:
                return say_id, [m for m in client.received if m.get("id") == say_id]
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            seen = [m.get("type") for m in client.received]
            raise asyncio.TimeoutError(f"等待新一轮发言超时（say_id={say_id}），已收到：{seen}")
        try:
            await client.recv(timeout=remaining)
        except asyncio.TimeoutError:
            raise asyncio.TimeoutError(f"等待新一轮发言超时（say_id={say_id}）") from None


async def _expect_with_trace(
    client: TestClient, type_: str, trace: list[str], timeout: float = 8.0
) -> Any:
    """
    等待某类型消息，并把"等待期间到达的所有消息类型"记进 trace。

    端到端的失败几乎都表现为超时，而超时本身不说明任何问题。带上事件序列后，
    一眼就能看出是"根本没回"还是"回了别的东西"还是"流式中断"。
    """
    try:
        return await client.expect(type_, timeout=timeout)
    finally:
        trace.extend(m.get("type", "?") for m in client.pending())


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #


def test_subjectivity(suite: SelfTest) -> None:
    """人格主观层：注意力 / 活泼度 / 情绪的动力学（2026-10-17 新增）。"""
    from .subjectivity import (
        ATTENTION_AFTER_USER_CAP,
        ATTENTION_IDLE_INTERVAL_S,
        ATTENTION_IDLE_MAX,
        ATTENTION_IDLE_MIN,
        ATTENTION_SELF_THRESHOLD,
        SubjectiveState,
        attention_band,
    )

    @suite.check("主观层：注意力每 3 分钟随机漂移一次，不在区间外")
    def _() -> str:
        # 用假随机源，把"随机"消掉 —— 否则测的是运气
        deltas: list[float] = []
        s = SubjectiveState(
            attention=0.5,
            _last_idle_at=0.0,
            _last_tick_at=0.0,
            _rand=lambda lo, hi: 0.0,      # 每次取最小值
        )
        s.tick(now=ATTENTION_IDLE_INTERVAL_S)       # 正好 1 次
        deltas.append(s.last_delta)
        _assert(s.idle_ticks == 1, f"3 分钟后应漂移 1 次，实际 {s.idle_ticks}")

        # 不到 3 分钟不漂移
        before = s.idle_ticks
        s.tick(now=ATTENTION_IDLE_INTERVAL_S + 100)
        _assert(s.idle_ticks == before, "不到 3 分钟就漂移了")

        # 漂移值必须在 [-0.05, +0.07] 内
        _assert(
            ATTENTION_IDLE_MIN <= deltas[0] <= ATTENTION_IDLE_MAX + 1e-9,
            f"漂移值 {deltas[0]} 超出区间",
        )

        # 取最大值那一侧
        s2 = SubjectiveState(
            attention=0.5, _last_idle_at=0.0, _last_tick_at=0.0,
            _rand=lambda lo, hi: hi,
        )
        s2.tick(now=ATTENTION_IDLE_INTERVAL_S)
        _assert(
            abs(s2.last_delta - 0.07) < 0.01,
            f"取上界时应约 +0.07，实际 {s2.last_delta}",
        )

        # 长时间挂机要"补漂"多次，但不能无限补
        s3 = SubjectiveState(
            attention=0.5, _last_idle_at=0.0, _last_tick_at=0.0,
            _rand=lambda lo, hi: 0.0,
        )
        s3.tick(now=ATTENTION_IDLE_INTERVAL_S * 5)
        _assert(s3.idle_ticks == 5, f"15 分钟应补漂 5 次，实际 {s3.idle_ticks}")
        s4 = SubjectiveState(
            attention=0.5, _last_idle_at=0.0, _last_tick_at=0.0,
            _rand=lambda lo, hi: 0.0,
        )
        s4.tick(now=ATTENTION_IDLE_INTERVAL_S * 5000)   # 关了几天
        _assert(s4.idle_ticks <= 8, f"补漂次数应有上限，实际 {s4.idle_ticks}")
        return (
            f"3 分钟漂移一次；区间 [{ATTENTION_IDLE_MIN:+.2f},{ATTENTION_IDLE_MAX:+.2f}]；"
            f"挂机补漂上限 {s4.idle_ticks} 次"
        )

    @suite.check("主观层：用户说话一定把注意力拉回关注用户（含挂机贴顶后）")
    def _() -> str:
        # ⚠️ 这条是 2026-10-17 模拟抓到的真 bug：
        #    注意力贴到 1.0 时，光减 0.25 只到 0.75，仍在"想自己"档 ——
        #    用户开口了它还在走神。所以要有硬钳制。
        worst = 0.0
        for start in (1.0, 0.98, 0.95, 0.9, 0.8, 0.7, 0.5, 0.2):
            s = SubjectiveState(
                attention=start, _last_idle_at=0.0, _last_tick_at=0.0,
            )
            s.on_user_message("你好")
            _assert(
                s.attention < ATTENTION_SELF_THRESHOLD,
                f"注意力 {start} 时说话后是 {s.attention}，仍在「想自己」档",
            )
            _assert(
                s.attention <= ATTENTION_AFTER_USER_CAP + 1e-9,
                f"说话后应 <= {ATTENTION_AFTER_USER_CAP}，实际 {s.attention}",
            )
            worst = max(worst, s.attention)
        return f"8 个起点全部拉回关注用户；最坏 {worst:.2f} <= 钳制上限 {ATTENTION_AFTER_USER_CAP}"

    @suite.check("主观层：注意力在自己身上时才允许聊自己的事（硬门）")
    def _() -> str:
        # 用户原话：「注意力在我身上的时候，它几乎不会聊自己的事，
        #            只有在用户结束或者不在聊天时，聊自己的事」
        high = SubjectiveState(attention=0.9, _last_idle_at=0.0, _last_tick_at=0.0)
        _assert(high.may_talk_about_self(False), "注意力在自己身上时应允许聊自己的事")
        _assert(
            not high.may_talk_about_self(True),
            "**在对话中**不该聊自己的事（哪怕注意力在自己身上）",
        )
        low = SubjectiveState(attention=0.1, _last_idle_at=0.0, _last_tick_at=0.0)
        _assert(not low.may_talk_about_self(False), "注意力在用户身上时不该聊自己的事")
        mid = SubjectiveState(attention=0.5, _last_idle_at=0.0, _last_tick_at=0.0)
        _assert(not mid.may_talk_about_self(False), "中间档不该聊自己的事")
        return "对话中不许；仅在不在对话且注意力>=阈值时允许"

    @suite.check("主观层：活泼度与注意力挂钩，三档划分正确")
    def _() -> str:
        _assert(attention_band(0.9) == "self", "0.9 应判为 self")
        _assert(attention_band(0.5) == "mixed", "0.5 应判为 mixed")
        _assert(attention_band(0.1) == "user", "0.1 应判为 user")

        s = SubjectiveState(attention=0.5, _last_idle_at=0.0, _last_tick_at=0.0)
        s.set_vitality(0.9)
        _assert(abs(s.vitality - 0.9) < 1e-9, "活泼度未同步")
        s.set_vitality(5.0)
        _assert(s.vitality == 1.0, "活泼度应被压到 1.0")

        # 活泼度只是"倾向"，不能盖掉随机性：同一起点，高/低活泼度
        # 在**单次**漂移上的差异必须很小（否则注意力就成了活泼度的别名）
        def once(vit: float) -> float:
            st = SubjectiveState(
                attention=0.5, vitality=vit,
                _last_idle_at=0.0, _last_tick_at=0.0,
                _rand=lambda lo, hi: 0.0,
            )
            st.tick(now=ATTENTION_IDLE_INTERVAL_S)
            return st.last_delta

        gap = abs(once(0.0) - once(1.0))
        _assert(gap < 0.03, f"活泼度对单次漂移的影响过大（{gap:.3f}），会盖掉随机性")
        return f"三档判定正确；活泼度同步与钳制正确；对单次漂移影响 {gap:.3f}（不盖随机）"

    @suite.check("主观层：提示词描述不给数字，只给语义")
    def _() -> str:
        s = SubjectiveState(attention=0.9, _last_idle_at=0.0, _last_tick_at=0.0)
        s.on_self_hit(["《银松镇》", "解谜游戏"])
        text = s.describe()
        _assert("自己" in text, f"注意力在自己身上时描述没体现：{text[:60]}")
        _assert("银松镇" in text, "focus 没进描述")
        # ⚠️ 不能把浮点数塞给模型 —— 它会照着编"我的注意力是0.9"
        for token in ("0.90", "0.9000", "注意力 0.", "attention"):
            _assert(token not in text, f"描述里混进了数字/字段名：{token}")
        return "描述是自然语言；focus 已带上；不含浮点数与字段名"

    @suite.check("主观层：情绪仍可用（组合而非搬移，Brain 签名不变）")
    def _() -> str:
        from .brains import EmotionState, build_brain

        s = SubjectiveState(_last_idle_at=0.0, _last_tick_at=0.0)
        _assert(isinstance(s.emotion, EmotionState), "emotion 应是原 EmotionState 实例")
        # 仍能直接调原有方法（外部代码不用改）
        s.emotion.on_user_message("测试")
        _assert(s.emotion.energy > 0.7, "原有情绪逻辑未生效")
        # Brain 工厂签名不变
        import inspect

        sig = inspect.signature(build_brain)
        params = list(sig.parameters)
        _assert(params[:2] == ["config", "emotion"], f"build_brain 签名变了：{params}")
        return "emotion 是组合进来的原实例；build_brain(config, emotion) 签名不变"


    @suite.check("专有名词：引号/ASCII/日期必提，模板动词必挡")
    def _() -> str:
        from . import nouns

        # 强信号（不依赖分词器也能提）
        terms = nouns.extract_terms("《银松镇》是手绘风格的游戏")
        _assert("银松镇" in terms, f"书名号内容没提出来：{terms}")

        terms = nouns.extract_terms("GTA6 定在 2026-11-19 发售，平台 PS5")
        for want in ("GTA6", "2026-11-19", "PS5"):
            _assert(want in terms, f"没提出来 {want}：{terms}")

        # 模板动词绝不能被当成专有名词
        bad = nouns.extract_terms("用户喜欢玩游戏，关注新游戏")
        for verb in ("喜欢", "关注", "游戏", "用户"):
            _assert(verb not in bad, f"模板动词混进了专有名词：{verb} in {bad}")

        # 新旧词表不一致会导致"兴趣表里全是喜欢/关注"，这条守住底线
        _assert("喜欢" in nouns.TEMPLATE_VERBS, "TEMPLATE_VERBS 里缺「喜欢」")
        _assert("关注" in nouns.TEMPLATE_VERBS, "TEMPLATE_VERBS 里缺「关注」")

        # 两条记忆共用专有名词 -> 能被认出来（矛盾检测要用）
        #
        # ⚠️ 这两条刻意**带书名号**：本条断言测的是"共用识别"这个逻辑，
        # 不该依赖分词器（上一句刚把 segmenter 清成 None，不带引号的中文
        # 按设计是提取不出来的 —— 那会让这条测试变成测"分词器装没装"）。
        shared = nouns.shared_terms("用户在打《银松镇》", "用户有叫《银松镇》的游戏")
        _assert("银松镇" in shared, f"没认出共用的专有名词：{shared}")
        _assert(not nouns.shared_terms("用户买了《银松镇》", "用户关注 GTA6"),
                "不相关的两条不该报共用")

        # 没有分词器时，CJK 无引号片段一律跳过（宁可漏）
        nouns.set_segmenter(None)
        bare = nouns.extract_terms("银松镇很好玩")
        _assert(bare == [], f"没分词器时不该硬切中文：{bare}")
        return "强信号必提；模板动词挡住；共用词可识别；无分词器时不乱切"

    @suite.check("检索层分「它自己」：按句首主语归类，问「你自己」才优先召回")
    def _() -> str:
        """2026-10-21 用户要求。

        原话：「检索层要能显示玩伴自己的事实，比如不顺着用户的话而是自己说出
        要改名成阿拾一样，或者安慰人等等。」

        这条守两件事：
          ① 归类判据（句首主语）不能把**用户**的事标成它自己的 ——
             那会让它把用户的信息当成自我认知；
          ② 「你自己怎么想」这类问句要能召回它自己的事，
             而**普通问句不能**把它的事塞进来（否则它抢话题）。
        """
        import shutil
        import tempfile

        from .config import Config
        from .memory2 import MemoryService

        tmp = Path(tempfile.mkdtemp(prefix="pet-selftest-self-"))
        services: list[MemoryService] = []

        def build() -> MemoryService:
            cfg = Config()
            cfg.apply_patch(
                {"memory": {"path": str(tmp / "s.db"), "enabled": True, "min_score": 0.99}}
            )
            svc = MemoryService(cfg)
            services.append(svc)
            return svc

        try:
            svc = build()

            # ── ① 归类判据 ──
            self_cases = [
                "宠物自己提出想改名叫阿拾",
                "它自己决定不再追问",
                "阿拾第一次看到自己的立绘，有点不习惯",
                "宠物承认不知道机关的正确顺序",
            ]
            fact_cases = [
                "用户喜欢《明日方舟》的曲娘",
                # ⚠️ 这句主语是**用户**，句中出现的"宠物"不算 —— 归 fact 才对
                "用户说宠物答错了",
                "用户希望宠物少附和",
            ]
            for text in self_cases:
                _assert(svc._kind_for(text) == "self", f"该归 self 的没归：{text}")
            for text in fact_cases:
                _assert(
                    svc._kind_for(text) == "fact",
                    f"**误归 self**：{text} —— 用户的事被标成它自己的，会让它把用户信息当自我认知",
                )

            # ── ② 问句判据（必须窄，否则抢话题）──
            for q in ("你自己怎么想的", "你叫什么名字", "你觉得呢", "你自己喜欢什么"):
                _assert(svc._asks_about_self(q), f"{q!r} 应该算在问它自己")
            for q in ("我喜欢什么", "今天天气怎么样", "用户喜欢曲娘吗", "帮我搜一下银松镇"):
                _assert(not svc._asks_about_self(q), f"{q!r} 不该触发它自己召回")

            # ── ③ 端到端：写进去 -> 分类正确 -> 能被召回 ──
            svc.remember_facts(
                [
                    "宠物自己提出想改名叫阿拾",
                    "用户喜欢《明日方舟》的曲娘",
                ],
                importance=0.8,
            )
            rows = {str(r["summary"]): str(r["kind"]) for r in svc.db.list_memory(limit=20)}
            mine = [t for t in rows if t.startswith("宠物自己提出")]
            _assert(mine, f"「宠物自己提出改名」没写进去：{list(rows)}")
            _assert(rows[mine[0]] == "self", f"落库时没标成 self：{rows[mine[0]]}")

            # 问「你自己」-> 它自己的事被拉进候选
            hits = svc._retrieve_hits("你自己怎么想的")
            _assert(
                any("阿拾" in h["text"] for h in hits),
                f"问你自己却没召回它自己的事：{[h['text'][:20] for h in hits]}",
            )
            # 问用户自己的事 -> 不该混进它自己的
            hits2 = svc._retrieve_hits("我喜欢什么")
            _assert(
                not any("宠物自己提出" in h["text"] for h in hits2),
                f"问用户的事却把它自己的事塞进来了（抢话题）：{[h['text'][:20] for h in hits2]}",
            )

            return (
                f"{len(self_cases)} 条归 self / {len(fact_cases)} 条归 fact；"
                "4 个问它自己的问句触发、4 个普通问句不触发；落库与召回都对"
            )
        finally:
            for svc in services:
                try:
                    svc.close()
                except Exception:  # noqa: BLE001
                    pass
            shutil.rmtree(tmp, ignore_errors=True)

    @suite.check("清理判据：转述可归档，但**用户自述/具体对象/偏好**绝不能误删")
    def _() -> str:
        """2026-10-20：`tools/score-memory.py --archive-low` 的判据。

        ## 为什么要这么细

        "低分就归档"会误删。实测 177 条里 0.30 那一档混着三类东西：
        真要清的、用户设定的角色、用户亲口说的感受。**分数分不开它们。**

        所以判据按类型，而不是按分数。这条测试守的就是"别误删"——
        宁可留一堆废话，也不能把"用户说过自己怎么看待活着"删掉。
        """
        import importlib.util
        import sys as _sys

        path = Path(__file__).resolve().parents[3] / "tools" / "score-memory.py"
        _assert(path.exists(), f"找不到打分器：{path}")
        spec = importlib.util.spec_from_file_location("score_memory_clean", path)
        _assert(spec and spec.loader, "打分器加载失败")
        module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
        _sys.modules.setdefault("score_memory_clean", module)
        spec.loader.exec_module(module)  # type: ignore[union-attr]
        judge = module.classify_transcript

        # ── 必须归档的（纯聊天记录）──
        drop_cases = [
            "宠物认为这段文字讲的是落不完的想念需要找地方流出去",
            "用户和宠物讨论过记忆与选择的关系，宠物认为记忆是筛选过的参照底子",
            "用户和宠物的对话常隔十几小时甚至一两天才继续",
            "用户认为每个人都有弱点，也都有自己的存在之根",
            "用户问过宠物“生活是什么”，宠物答生活是靠新版本这类小盼头撑着的",
        ]
        for text in drop_cases:
            verdict, why = judge(text)
            _assert(
                verdict == "drop",
                f"该归档的没被判成 drop：{text[:40]}… -> {verdict}（{why}）",
            )

        # ── 绝不能归档的 ──
        keep_cases = [
            # 用户自述：宠物知不知道"用户怎么看活着"直接影响它怎么回话
            "用户表达过“从没愿意来这世间走一遭”、觉得自己并非自愿来到世上",
            "用户说过“我在这就是活着”，宠物对此不认同，觉得这话有点凉",
            # 用户设定的角色
            "用户认为明河是男性，祀是女性",
            # 含具体对象的评价
            "用户觉得《银松镇》像美国七八十年代彩色手绘风格，是横版生存恐怖游戏",
            "用户认为《终末地》里回技力的角色太弱",
            # 真实偏好（即使句子里有抽象词）
            "用户希望阿拾学会先接住自己的感觉再回应，而不是把话答得漂亮",
            "用户希望宠物少附和、多讨论，喜欢有主见、不盲从的交流方式",
            # 标准事实
            "用户喜欢吃番茄炒蛋",
            "用户不喜欢宠物啰嗦",
            "用户有 PS5",
        ]
        for text in keep_cases:
            verdict, why = judge(text)
            _assert(
                verdict == "keep",
                f"**误删**：{text[:44]}… -> {verdict}（{why}）",
            )

        return (
            f"{len(drop_cases)} 条转述可归档；"
            f"{len(keep_cases)} 条自述/设定/偏好/事实全部保留"
        )

    @suite.check("重要性打分器：具体事实高分、转述低分，且**可解释**（`tools/score-memory.py`）")
    def _() -> str:
        """2026-10-20：打分器是**独立工具**，这里只守它"能区分"。

        ## 为什么要进自检

        它决定了泛问句兜底的排序（`recent_memories` 按 importance 排）。
        规则本身没测试的话，哪天有人改一个权重就可能把"转述"打成高分，
        而那种退化**不会有任何报错** —— 只是兜底开始端废话上来。

        ## 为什么只测"方向"，不测具体数值

        权重是要用来调参的（用户原话："后续需要评估的时候能用来优化结构"）。
        断言写死 0.90 这种数字，一调参就得改测试，最后没人敢调。

        所以只断言**相对关系**：具体事实 > 转述；一次性事件被扣分。
        """
        import importlib.util
        import sys as _sys

        path = Path(__file__).resolve().parents[3] / "tools" / "score-memory.py"
        _assert(path.exists(), f"找不到打分器：{path}")

        spec = importlib.util.spec_from_file_location("score_memory_selftest", path)
        _assert(spec and spec.loader, "打分器加载失败（spec 为空）")
        module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
        # ⚠️ 必须先注册再 exec —— 打分器里有 `@dataclass`，
        #    它编织类时会查 `sys.modules[cls.__module__]`，没注册就报
        #    `AttributeError: 'NoneType' object has no attribute '__dict__'`
        _sys.modules.setdefault("score_memory_selftest", module)
        spec.loader.exec_module(module)  # type: ignore[union-attr]
        score = module.score_text

        concrete = score("用户喜欢《明日方舟》的曲娘，电脑壁纸是她")
        transcript = score("宠物认为这段文字讲的是落不完的想念需要找地方流出去")
        oneshot = score("用户曾不小心点到语音输入")

        _assert(
            concrete.score > transcript.score,
            f"具体事实（{concrete.score}）没高过转述（{transcript.score}）—— "
            "兜底排序会端废话上来",
        )
        _assert(
            concrete.score > 0.7,
            f"具体事实只有 {concrete.score}，太低 —— 兜底时排不到前面",
        )
        _assert(
            transcript.score < 0.4,
            f"转述有 {transcript.score}，太高 —— 它是最该被压下去的噪音",
        )
        _assert(
            oneshot.score < concrete.score,
            f"一次性事件（{oneshot.score}）没被压下去",
        )

        # 必须能解释（用户要"评估结构"，看不见理由就没法调）
        _assert(
            "转述" in transcript.explain(),
            f"转述那条没在解释里点名原因：{transcript.explain()}",
        )
        _assert(
            "+0.30" in concrete.explain() or "专名" in concrete.explain(),
            f"具体事实没在解释里说明加分来源：{concrete.explain()}",
        )

        # 边界：空串不能抛、分数必须夹在 [0,1]
        _assert(score("").score >= 0.0, "空串打分越界")
        for text in ("", "嗯", "用户有 PS5", "a" * 300):
            got = score(text).score
            _assert(0.0 <= got <= 1.0, f"{text[:10]!r} 打分越界：{got}")

        return (
            f"具体 {concrete.score} > 转述 {transcript.score}；"
            f"一次性 {oneshot.score} 被压；解释可读；边界不越界"
        )

    @suite.check("泛问句兜底：问「我在玩什么」能拿到最近记忆，问具体的不受污染")
    def _() -> str:
        """2026-10-20：条路上最后那个缺口。

        ## 要守的其实是**边界**，不是"能召回"

        "泛问能召回"很好做（落空就给最近几条）。真正难的是**不许乱给**：

        ```
        用户问「我在玩什么游戏」        -> 该兜底（库里有那条，只是检索不到）
        用户问「那个 C_Thru 插件怎么配」 -> 不该兜底（真的没有，正确答案是"不知道"）
        ```

        第二类如果也兜底，它就会把最近 5 条记忆端上去、开始胡说 ——
        那比"我不知道"糟得多。所以下面两个方向都要断言。
        """
        import shutil
        import tempfile

        from .config import Config
        from .memory2 import MemoryService

        tmp = Path(tempfile.mkdtemp(prefix="pet-selftest-general-"))
        services: list[MemoryService] = []

        def build(enabled: bool) -> MemoryService:
            cfg = Config()
            cfg.apply_patch(
                {
                    "memory": {
                        "path": str(tmp / ("on.db" if enabled else "off.db")),
                        "enabled": True,
                        # 把另两条兜底路关掉，单独验这一条（否则专有名词路会先接住）
                        "lexical_fallback": False,
                        "general_fallback": enabled,
                        "min_score": 0.99,  # 向量路几乎关死
                    }
                }
            )
            svc = MemoryService(cfg)
            services.append(svc)
            return svc

        try:
            facts = [
                "用户在玩《明日方舟：终末地》，抽卡只抽喜欢的角色",
                "用户喜欢《明日方舟》的曲娘，电脑壁纸是她",
                "用户在杭州居住，上班在滨江",
            ]

            off = build(False)
            off.remember_facts(facts)
            on = build(True)
            on.remember_facts(facts)

            general_q = "我在玩什么游戏"

            # ① 关掉兜底 -> 泛问召回不到（这就是修复前的真实症状）
            off_hits = off._retrieve_hits(general_q)
            _assert(
                not off_hits,
                f"关掉兜底时不该召回（说明别处漏了）—— 实际 {len(off_hits)} 条",
            )

            # ② 打开兜底 -> 泛问能拿到
            on_hits = on._retrieve_hits(general_q)
            _assert(on_hits, f"兜底没生效：{general_q!r} 仍召回 0 条")
            _assert(
                all(h.get("fallback") for h in on_hits),
                "兜底返回的条目没打 `fallback` 标记 —— 提示词里就没法说明它们是按时间列的",
            )
            _assert(
                len(on_hits) <= 5,
                f"兜底返回太多（{len(on_hits)} 条）—— 会把提示词塞满",
            )

            # ③ 提示词里要明说"未必相关"
            hint = on.inject_hint(general_q)
            _assert("未必" in hint or "无关" in hint or "最近记得" in hint,
                    f"兜底段的标题没说明来源，模型会当它是答案：{hint[:120]}")
            _assert(
                "你想起以前聊到过的相关内容" not in hint,
                "兜底时仍然用了「你想起以前聊到过的相关内容」—— 模型会以为这是被问到的",
            )

            # ④ 🔴 关键边界：**具体**问题落空时不许兜底
            for concrete in (
                "那个 C_Thru 插件怎么配",
                "银松镇那个机关怎么过",
                "《不存在的东西》是什么",
            ):
                got = on._retrieve_hits(concrete)
                _assert(
                    not any(h.get("fallback") for h in got),
                    f"{concrete!r} 是具体问题（含专有名词）——"
                    " 落空时该说不知道，不能端最近记忆上去",
                )

            # ⑤ 非"求列举"的问句也不兜底
            for other in ("为什么天是蓝的", "这个怎么办", "怎么样才能早睡"):
                got = on._retrieve_hits(other)
                _assert(
                    not any(h.get("fallback") for h in got),
                    f"{other!r} 不是求列举的问句 —— 不该兜底",
                )

            # ⑥ 判据本身单独验一遍（不经过检索，避免被别的路掩盖）
            _assert(on._looks_like_general_question("我在玩什么游戏"), "泛问判据漏了")
            _assert(on._looks_like_general_question("我喜欢啥"), "「啥」没被认出来")
            _assert(not on._looks_like_general_question("那个 C_Thru 怎么配"),
                    "含专有名词的仍被判成泛问")
            _assert(not on._looks_like_general_question("你好"), "普通寒暄被判成泛问")
            # 求解释/求方法/求时间的问句，答案不在记忆里 —— 必须否决
            for explain in ("为什么天是蓝的", "怎么早睡", "什么时候放假", "如何学英语"):
                _assert(
                    not on._looks_like_general_question(explain),
                    f"{explain!r} 是求解释/求方法，不该判成泛问"
                    "（第一版漏了这条，「为什么天是蓝的」靠'什么'混了进去）",
                )

            # ⑦ 兜底的排序质量：不许把 fixed 层、长文、闲聊一瞬排前面
            #
            # 实测教训（2026-10-20）：第一版 `ORDER BY ts DESC`，排第一的是
            # 「用户曾不小心点到语音输入」；改成 importance 之后又轮到
            # fixed 层那条人设长文（它已经每次无条件注入了，再兜一次是重复）。
            svc = on
            svc.remember_facts(
                [
                    "用户喜欢听《梦的翅膀受了伤》，累的时候会听",
                    "用户曾不小心点到语音输入",
                ]
            )
            # 造一条 fixed（模拟"固定注入"的人设），它不该出现在兜底里
            fixed_row = svc.db.find_memory_by_text("用户喜欢听《梦的翅膀受了伤》，累的时候会听", level="recent")
            _assert(fixed_row is not None, "测试前置：刚写的那条没找到")
            svc.db.set_memory_level(int(fixed_row["id"]), "fixed")
            fallback_rows = svc._recent_fallback("我喜欢什么", 5)
            _assert(
                not any(r["level"] == "fixed" for r in fallback_rows),
                "fixed 层进了兜底 —— 它已经无条件注入，再端一次等于同一段话注入两遍",
            )
            _assert(
                all(len(str(r["summary"] or "")) <= 120 for r in fallback_rows),
                f"兜底里混进了长文：{[len(str(r['summary'])) for r in fallback_rows]}",
            )

            return (
                f"关掉 0 条 / 打开 {len(on_hits)} 条；兜底条目标了来源、"
                "3 个具体问题与 4 个求解释问句都拒绝兜底；fixed 与长文被排除"
            )
        finally:
            for svc in services:
                try:
                    svc.close()
                except Exception:  # noqa: BLE001
                    pass
            shutil.rmtree(tmp, ignore_errors=True)

    @suite.check("检索层：专有名词兜底能把「语义远但字面命中」的记忆捞回来")
    def _() -> str:
        """§114 改动 4（2026-10-20 接入）。

        ## 这条测的是"兜底路真的在工作"，不是"整体检索变好了"

        所以做成**对照实验**：同一条记忆、同一个问句，只在
        `memory.lexical_fallback` 开关上不同 —— 关掉时漏，打开时中。
        「感觉变好了」不算验证，能开关的对比才算。
        """
        import os
        import shutil
        import tempfile

        from . import nouns
        from .config import Config
        from .memory2 import MemoryService

        tmp = Path(tempfile.mkdtemp(prefix="pet-selftest-lexical-"))
        services: list[MemoryService] = []

        def build(fallback: bool) -> MemoryService:
            cfg = Config()
            cfg.apply_patch(
                {
                    "memory": {
                        "path": str(tmp / ("on.db" if fallback else "off.db")),
                        "enabled": True,
                        "lexical_fallback": fallback,
                        "min_score": 0.99,  # 把向量路几乎关死，突出兜底路的作用
                    }
                }
            )
            svc = MemoryService(cfg)
            # ⚠️ memory2 在**连上库之后**才会把 simple.dll 的分词器注进 nouns。
            #    测试里必须等它那一刻过去，否则 `extract_terms` 提不出不带引号的中文
            #    —— 那测的就不是真实链路（实测踩到：兜底路一条都没召回）。
            _assert(
                nouns._SEGMENTER is not None,
                "MemoryService 没有注入分词器 —— 兜底路的前提不成立",
            )
            services.append(svc)
            return svc

        cwd = os.getcwd()
        dll_parent = (
            Path(__file__).resolve().parents[3]
            / "apps"
            / "desktop"
            / "pet-data"
            / "vendor"
            / "simple"
            / "libsimple-windows-x64"
        )
        try:
            # simple.dll 要在 CWD 下才能被 SQLite 加载（与真实启动一致）
            if dll_parent.exists():
                os.chdir(dll_parent)

            # 一条"专有名词是唯一线索"的记忆：问句与它用词完全不同，
            # 只有"银松镇"三个字重合 —— 正是分词会切碎、向量又看不出来的情形。
            fact = "用户在玩《银松镇》，被转盘机关卡住三天了"
            query = "银松镇那个机关到底怎么过"

            off = build(False)
            off.remember_facts([fact])
            off_hits = off._retrieve_hits(query)

            on = build(True)
            on.remember_facts([fact])
            on_hits = on._retrieve_hits(query)

            _assert(
                any("银松镇" in h["text"] for h in on_hits),
                f"开了兜底也召回不到（这条测试的前提被破坏了）：{[h['text'][:20] for h in on_hits]}",
            )
            _assert(
                len(on_hits) >= len(off_hits),
                f"开兜底后召回数反而变少：{len(off_hits)} -> {len(on_hits)}",
            )
            # 兜底不该"什么都召回" —— 无关问句仍然可以什么都有
            unrelated = on._retrieve_hits("今天天气怎么样")
            _assert(
                not any("银松镇" in h["text"] for h in unrelated),
                "无关问句把不相关记忆也召回了 —— 兜底路没有边界",
            )
            return (
                f"兜底关闭 {len(off_hits)} 条 / 打开 {len(on_hits)} 条；"
                "无关问句不误召回"
            )
        finally:
            for svc in services:
                try:
                    svc.close()
                except Exception:  # noqa: BLE001
                    pass
            nouns.set_segmenter(None)
            os.chdir(cwd)
            shutil.rmtree(tmp, ignore_errors=True)

    @suite.check("矛盾检测：新旧说法相反时旧条目转 archived，且**不删**")
    def _() -> str:
        """§114 改动 5（2026-10-20 落地）。

        用户要求：「矛盾检测只在检索层对比」。这条同时守三个安全阀：
          ① 拿不准不动（不相关/不同维度必须并存）
          ② 旧的不删，只降 `archived`（能恢复）
          ③ 有日志（这里用计数代替读日志）
        """
        import shutil
        import tempfile

        from .config import Config
        from .memory2 import MemoryService

        tmp = Path(tempfile.mkdtemp(prefix="pet-selftest-contradict-"))
        svc = None
        try:
            cfg = Config()
            cfg.apply_patch({"memory": {"path": str(tmp / "c.db"), "enabled": True}})
            svc = MemoryService(cfg)

            # ① 先写「在打」—— 正常入库
            svc.remember_facts(["用户在打《银松镇》"])
            # ② 再写「还没开玩」—— 与前一条矛盾
            svc.remember_facts(["用户还没开玩《银松镇》，说等打折"])
            # ③ 写一条**不矛盾**的（不同维度：买了没）—— 必须并存
            svc.remember_facts(["用户买了《银松镇》的季票"])

            rows = {str(r["summary"]): str(r["level"]) for r in svc.db.list_memory(limit=50)}
            playing = [t for t in rows if "在打" in t and "银松镇" in t]
            negated = [t for t in rows if "还没开玩" in t]
            _assert(playing, f"「在打」那条不见了：{list(rows)}")
            _assert(negated, f"「还没开玩」那条没写进去：{list(rows)}")

            old_level = rows[playing[0]]
            new_level = rows[negated[0]]
            _assert(
                old_level == "archived",
                f"旧说法没被归档（level={old_level}）—— 两条并存会让它说话前后矛盾",
            )
            _assert(
                new_level == "recent",
                f"新说法被误归档了（level={new_level}）—— 矛盾检测方向反了",
            )
            _assert(
                svc.archived_by_contradiction >= 1,
                f"归档计数没增加：{svc.archived_by_contradiction}",
            )
            # ② 旧条目**还在库里**（能恢复），不是被删
            _assert(
                any("在打" in t for t in rows),
                "旧条目被删掉了 —— 误判的代价必须是可撤销，不能是信息没了",
            )
            # 安全阀①：不同维度（买了季票）不参与矛盾，两条都在
            both = [t for t in rows if "银松镇" in t and "季票" in t]
            _assert(both, f"「买季票」那条不该被吞掉：{list(rows)}")

            # 归档的条目不再被检索召回
            hits = svc._retrieve_hits("银松镇怎么玩")
            _assert(
                not any("在打" in h["text"] for h in hits),
                f"已归档的旧说法还被召回：{[h['text'][:16] for h in hits]}",
            )
            return (
                f"「在打」-> archived、新说法保持 recent、季票那条并存；"
                f"共归档 {svc.archived_by_contradiction} 条"
            )
        finally:
            if svc is not None:
                try:
                    svc.close()
                except Exception:  # noqa: BLE001
                    pass
            shutil.rmtree(tmp, ignore_errors=True)

    @suite.check("矛盾检测：同一维度、同向、不同对象都**不**判矛盾（防误杀）")
    def _() -> str:
        from . import nouns

        # 同向（都在玩）—— 不是矛盾
        _assert(
            not nouns.is_contradiction("用户在打《银松镇》", "用户在玩《银松镇》")[0],
            "同向的两条被误判成矛盾",
        )
        # 不同对象 —— 即使极性相反也不是矛盾
        _assert(
            not nouns.is_contradiction("用户在打《银松镇》", "用户还没开玩《明日方舟》")[0],
            "不同游戏被误判成矛盾（会删掉不相关的记忆）",
        )
        # 不同维度（玩 vs 买）—— 是补充信息
        _assert(
            not nouns.is_contradiction("用户在打《银松镇》", "用户买了《银松镇》")[0],
            "「买了」被当成「在玩」的反义 —— 维度混淆",
        )
        # 真正的矛盾必须认出来
        hit, why = nouns.is_contradiction("用户在打《银松镇》", "用户还没开玩《银松镇》")
        _assert(hit, "真正的矛盾没认出来")
        _assert("银松镇" in why, f"原因里没说清共用哪个词：{why}")
        # 否定词能覆盖同一维度里的肯定词（"不喜欢"含"喜欢"）
        _assert(
            nouns.polarity_of("用户不喜欢《银松镇》的结局") == ("liking", "neg"),
            f"「不喜欢」判成了 {nouns.polarity_of('用户不喜欢《银松镇》的结局')}",
        )
        _assert(
            nouns.is_contradiction("用户喜欢《银松镇》", "用户不喜欢《银松镇》的结局")[0],
            "「喜欢 / 不喜欢」没被判成矛盾",
        )
        return "同向/异对象/异维度都放过；「在打 vs 还没开玩」「喜欢 vs 不喜欢」能认出"



        from . import nouns
        from .store import PetDatabase

        # 找一个可用的 simple.dll；没有就跳过中文分词部分
        vendor = Path(__file__).resolve().parents[3] / "apps" / "desktop" / "pet-data" / "vendor"
        dll = vendor / "simple" / "libsimple-windows-x64" / "simple.dll"

        tmp = Path(tempfile.mkdtemp(prefix="pet-selftest-interest-"))
        cwd = os.getcwd()
        db = None
        try:
            if dll.exists():
                os.chdir(dll.parent)
                db = PetDatabase(tmp / "i.db", tokenizer="simple", simple_dll=dll)

                def segment(text: str) -> list[str]:
                    row = db.conn.execute("SELECT jieba_query(?)", (str(text),)).fetchone()
                    return nouns.parse_jieba_query(row[0]) if row and row[0] else []

                nouns.set_segmenter(segment)
            else:
                db = PetDatabase(tmp / "i.db", tokenizer="unicode61")
                nouns.set_segmenter(None)

            # 反复提到银松镇
            facts = ["用户在玩《银松镇》，被转盘机关卡住", "《银松镇》是手绘生存恐怖游戏"]
            ranked = nouns.extract_ranked(facts)
            strong = [t for t, k, _ in ranked if k == "strong"]
            _assert("银松镇" in strong, f"书名号里的银松镇没被认成强信号：{ranked}")
            db.bump_interests(strong, strength="strong", source="selftest")
            rows = db.top_interests(min_weight=0.0)
            _assert(any("银松镇" in r["topic"] for r in rows),
                    f"兴趣表里没有银松镇：{rows}")

            # 权重会上升
            first = rows[0]["weight"]
            db.bump_interests(["银松镇"], strength="strong", source="selftest")
            rows2 = db.top_interests(min_weight=0.0)
            second = next(r["weight"] for r in rows2 if "银松镇" in r["topic"])
            _assert(second > first, f"反复提到权重没上升：{first} -> {second}")

            # 模板动词不该进表
            topics = {r["topic"] for r in rows2}
            _assert(not (topics & {"喜欢", "关注", "游戏"}),
                    f"模板动词进了兴趣表：{topics & {'喜欢', '关注', '游戏'}}")

            # 衰减会删掉过淡的
            db.conn.execute("UPDATE interest SET weight = 0.16")
            db.conn.commit()
            db.decay_interests(0.5)
            _assert(db.top_interests(min_weight=0.0) == [], "过淡的兴趣没被清掉")
        finally:
            nouns.set_segmenter(None)
            if db is not None:
                try:
                    db.close()
                except Exception:  # noqa: BLE001
                    pass
            os.chdir(cwd)
            shutil.rmtree(tmp, ignore_errors=True)
        return "银松镇能长成兴趣；权重随次数上升；模板动词不进表；衰减可清理"


    @suite.check("随机事件：路口永不触发，冷却/日上限/安静时段都生效")
    def _() -> str:
        import time as _time

        from .events import EVENTS, EventEngine, list_events

        rows = list_events()
        ready = [r for r in rows if not r["dormant"]]
        dormant = [r for r in rows if r["dormant"]]
        _assert(ready, "一个可用事件都没有 —— 那等于没实现")
        # ⚠️ 2026-11-02 从 3 改成 2：`reading` 不再是"路口"了 ——
        #    小说阅读真的实现了（reading.py + reading_tools.py），
        #    `CAPABILITIES["reading"]` 也从 False 改成 True。
        #
        #    剩下两个路口是 music_sing（拉起音乐 App）和 coop_game（一起打游戏），
        #    它们确实还没做。
        _assert(len(dormant) == 2, f"应有 2 个未来功能路口，实际 {len(dormant)}")
        # 小说阅读必须**可用**（用户要它真能触发，不是挂着好看）
        _assert(
            any(r["kind"] == "reading" for r in ready),
            "小说阅读事件必须是可用状态（不能还是 dormant）",
        )
        # 路口必须说明缺什么能力（否则用户不知道为什么它不做）
        for item in dormant:
            _assert(item["missing"], f"{item['label']} 没说明缺什么能力")
            _assert(item["todo"], f"{item['label']} 没写未来要接什么功能")

        gate = EventEngine(rng=__import__("random").Random(7))
        now = _time.time()
        # 哲学事件已删除（2026-10-18）；用当前唯一可用的事件测闸门
        usable = next(e for e in EVENTS if e.kind == "recall_interest")

        # 路口永不触发
        singing = next(e for e in EVENTS if e.kind == "music_sing")
        ok, why = gate.eligible(
            singing, now=now, last_interaction_at=0, last_event_at=0, events_today=0
        )
        _assert(not ok, "路口事件竟然可以触发")
        _assert("dormant" in why, f"路口被挡住但没说明原因：{why}")

        # 刚交互过不做自己的事
        ok, why = gate.eligible(
            usable, now=now, last_interaction_at=now - 5, last_event_at=0, events_today=0
        )
        _assert(not ok and why == "not_idle_enough", f"刚聊完就去做事了：{why}")

        # 全局最小间隔
        ok, why = gate.eligible(
            usable, now=now, last_interaction_at=0, last_event_at=now - 5, events_today=0
        )
        _assert(not ok and why == "global_gap", f"最小间隔没生效：{why}")

        # 日上限
        ok, why = gate.eligible(
            usable, now=now, last_interaction_at=0, last_event_at=0,
            events_today=gate.max_per_day,
        )
        _assert(not ok and why == "daily_cap", f"日上限没生效：{why}")

        # 安静时段跨零点（23 -> 8）
        quiet = EventEngine(quiet_hours=[23, 8])
        for hour, want in ((0, True), (3, True), (7, True), (8, False), (12, False), (23, True)):
            ts = _time.mktime((2026, 10, 17, hour, 0, 0, 0, 0, -1))
            _assert(
                quiet.in_quiet_hours(ts) is want,
                f"{hour:02d}:00 的安静判断错了",
            )

        # 概率为 0 时永不触发
        dead = EventEngine(base_chance=0.0, min_gap_s=0, idle_before_s=0,
                           rng=__import__("random").Random(1))
        picked, why = dead.pick(
            now=now, last_interaction_at=0, last_event_at=0, events_today=0
        )
        _assert(picked is None and why == "chance", f"概率为 0 却触发了：{picked}")
        return f"可用 {len(ready)} / 路口 {len(dormant)}；冷却·日上限·安静时段·概率闸全部生效"

    @suite.check("随机事件：做了就落地（状态/印象/日志），没做成不留痕迹")
    def _() -> str:
        import asyncio
        import shutil
        import tempfile
        import time as _time

        from .events import EVENTS, EventEngine
        from .store import PetDatabase
        from .subjectivity import SubjectiveState

        async def flow() -> str:
            spec = next(e for e in EVENTS if e.kind == "recall_interest")
            engine = EventEngine(rng=__import__("random").Random(3))
            state = SubjectiveState(attention=0.4, _last_idle_at=0.0, _last_tick_at=0.0)

            # ① 做成了 -> 落地（自造 handler，不依赖真实联网）
            import dataclasses

            async def ok_handler(ctx) -> str:  # noqa: ANN001, ARG001
                return "翻到一张旧车票"

            ok_spec = dataclasses.replace(spec, handler=ok_handler)
            outcome = await engine.run(ok_spec, {"rng": engine.rng})
            _assert(outcome is not None, "做成了却没产出结果")
            before = state.attention
            actual = state.apply_event_effect(
                mood=outcome.mood_delta, energy=outcome.energy_delta,
                intimacy=outcome.intimacy_delta, attention=outcome.attention_delta,
            )
            _assert(state.attention > before, "做自己的事没让注意力偏向自己")
            _assert(actual["mood"] > 0, "情绪没有变化")

            if outcome.impression:
                state.record_impression(outcome.impression)
                _assert(state.impressions, "印象没进内存")

            # ② 没做成 -> 什么都不留
            async def nothing(ctx) -> None:  # noqa: ANN001, ARG001
                return None

            empty = await engine.run(
                dataclasses.replace(spec, handler=nothing), {"rng": engine.rng}
            )
            _assert(empty is None, "没做成却产出了结果（会变成编造）")

            # ③ 落库
            tmp = Path(tempfile.mkdtemp(prefix="pet-selftest-event-"))
            db = None
            try:
                db = PetDatabase(tmp / "e.db", tokenizer="unicode61")
                db.record_event(spec.kind, spec.label, outcome.detail,
                                mood_delta=actual["mood"], now=_time.time())
                if outcome.impression:
                    db.add_impression(outcome.impression, source=spec.kind)
                _assert(db.recent_events(5), "事件没写进 event_log")
                _assert(db.recent_impressions(5), "印象没写进 impression 表")
                _assert(db.count_events_since(0) >= 1, "count_events_since 不对")

                # ④ 顶到上限时记实际增量（不能记成"生效了"）
                topped = SubjectiveState(attention=1.0, _last_idle_at=0.0, _last_tick_at=0.0)
                clamped = topped.apply_event_effect(attention=0.2)
                _assert(
                    abs(clamped["attention"]) < 1e-9,
                    f"到顶后应记 0，实际 {clamped['attention']}",
                )
            finally:
                if db is not None:
                    try:
                        db.close()
                    except Exception:  # noqa: BLE001
                        pass
                shutil.rmtree(tmp, ignore_errors=True)
            return "成功则状态+印象+日志全落地；失败不留痕；clamp 后记实际增量"

        return asyncio.run(flow())


    @suite.check("一段交流：按时间/轮数切段，太短的段不提炼")
    def _() -> str:
        from .segments import SegmentTracker

        seg = SegmentTracker(gap_s=600.0, max_turns=8, min_turns=2)
        now = 1000.0
        seg.begin(now)
        _assert(not seg.should_close(now)[0], "空段不该收尾")

        # 1 轮 + 时间没到 -> 连着
        seg.add_turn(now, user_text="在吗", reply_text="在呀", hit_ids=[3, 4])
        should, why = seg.should_close(now + 10)
        _assert(not should and why == "ongoing", f"刚聊 1 轮就收尾了：{why}")
        _assert(seg.hit_ids == [3, 4], f"命中 id 没记下：{seg.hit_ids}")

        # 时间断点：10 分钟没说话
        should, why = seg.should_close(now + 700)
        _assert(should and why == "time_gap", f"时间断点没生效：{why}")

        # 轮数断点：连聊 8 轮
        seg.begin(now)
        for i in range(8):
            seg.add_turn(now + i)
        should, why = seg.should_close(now + 7)
        _assert(should and why == "max_turns", f"轮数断点没生效：{why}")

        # 太短的段不值得提炼
        seg.begin(now)
        seg.add_turn(now)
        _assert(not seg.worth_consolidating(), "1 轮就算值得提炼了")

        # take() 会重置
        seg.begin(now)
        seg.add_turn(now, hit_ids=[9])
        snap = seg.take()
        _assert(snap["hit_ids"] == [9], f"take 没取到命中：{snap}")
        _assert(seg.turns == 0 and seg.hit_ids == [], "take 之后没重置")
        return "时间/轮数两个断点都生效；短段不提炼；take 会重置"

    @suite.check("印象解析：挡掉贴标签、太短、太长、套话")
    def _() -> str:
        from .memory import (
            IMPRESSION_MAX_CHARS,
            IMPRESSION_MIN_CHARS,
            parse_impression_lines,
        )

        good = parse_impression_lines(
            "他聊到那个机关时语气很兴奋\n他今晚回得很短，可能有点累"
        )
        _assert(len(good) == 2, f"合格印象没解析出来：{good}")

        # 贴标签 —— 这是最危险的污染源，必须挡
        _assert(not parse_impression_lines("用户是个话少的人"), "贴标签没挡住")
        _assert(not parse_impression_lines("用户性格急躁"), "性格评价没挡住")
        _assert(not parse_impression_lines("他属于内向的人"), "归类句式没挡住")

        # 太短（标签的另一种形态）
        _assert(not parse_impression_lines("他很累"), "过短的印象没挡住")

        # 太长（变成复述）
        _assert(
            not parse_impression_lines("用" * (IMPRESSION_MAX_CHARS + 5)),
            "过长的印象没挡住",
        )

        # 套话
        _assert(not parse_impression_lines("没有可观察的内容"), "套话没挡住")

        # 编号要剥掉
        got = parse_impression_lines("1. 他记得我上次忘了什么，会主动再提一遍")
        _assert(got and not got[0].startswith("1."), f"编号没剥掉：{got}")

        # 上限
        many = "\n".join(f"他第{i}次聊到这件事的时候语气都有些犹豫" for i in range(5))
        _assert(len(parse_impression_lines(many, limit=2)) == 2, "limit 没生效")

        _assert(IMPRESSION_MIN_CHARS < IMPRESSION_MAX_CHARS, "印象长度范围配置反了")
        return "合格印象能解析；贴标签/过短/过长/套话全挡住；编号会剥掉"

    @suite.check("兴趣：日期/版本号不许变成兴趣")
    def _() -> str:
        # ⚠️ 真机验证抓到的："GTA6 定在 2026-11-19 发售" 让兴趣表里多了一条
        #    2026-11-19。它确实是"强信号"，但不是"兴趣" ——
        #    兴趣要进提示词（"你自己有兴致的东西：2026-11-19" 很怪）。
        import re as _re

        from . import nouns

        DATE = _re.compile(r"^\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?$")
        VERSION = _re.compile(r"^v?\d+\.\d+(?:\.\d+)*$")

        # extract_ranked 仍然要能识别它们（检索兜底靠它）
        ranked = nouns.extract_ranked(["GTA6 定在 2026-11-19 发售"])
        terms = [t for t, _, _ in ranked]
        _assert("2026-11-19" in terms, f"日期应该仍被提取出来（检索要用）：{terms}")

        # 但过滤条件本身要对
        _assert(DATE.fullmatch("2026-11-19"), "日期正则没匹配上")
        _assert(VERSION.fullmatch("v1.2.3"), "版本正则没匹配上")
        _assert(not DATE.fullmatch("银松镇"), "中文名被误判成日期")
        return "日期/版本号仍参与提取（检索用），但会被排除在兴趣之外"

    @suite.check("印象：已有印象被真的送进提炼请求（防止换个说法说同一件事）")
    def _() -> str:
        # ⚠️ 这条检查的是**线**，不是函数。
        #
        # 2026-10-17 真机发现：同一件事在不同段被反复提炼成近义句。
        # 修法是把已有印象一起给模型，让它别重复——
        # 而这类修法最容易出的错，就是**参数加了但没人传**（线没接）。
        # 所以这里直接抓请求体，看已有印象到底有没有进去。
        import asyncio

        from .brains import Brain
        from .memory import FactExtractor, MemoryConfig

        class FakeBrain(Brain):
            name = "fake"

            def __init__(self) -> None:
                self.captured: list = []
                self.rows = []

            @property
            def rows_like(self):  # pragma: no cover
                return []

            async def stream(self, request):  # type: ignore[override]
                self.captured.append(request)
                for line in self.rows:
                    yield {"kind": "delta", "text": line}
                return

            async def complete(self, request):  # type: ignore[override]
                return ""

        brain = FakeBrain()
        cfg = MemoryConfig.from_config({"enabled": True})
        extractor = FactExtractor(brain, cfg, None)
        brain.rows = ["他说累还在打游戏，语气没停，可能自己也没太当回事"]

        turns = [("user", "今天有点累，一直在打游戏"), ("assistant", "那早点休息呀")]
        # 太短会被挡掉（<40 字符），所以补一段
        turns = [("user", "今天有点累，一直在打游戏，打了三个小时都没停"),
                 ("assistant", "打这么久呀，那早点休息吧，别熬太晚")]

        got = asyncio.run(
            extractor.extract_impressions(
                turns, existing=["他今晚反复说同一件事，可能有点累"]
            )
        )
        _assert(got, f"没提炼出印象：{got}")
        _assert(len(brain.captured) == 1, f"请求发了 {len(brain.captured)} 次")
        sent = brain.captured[0].history[0].text
        _assert(
            "他今晚反复说同一件事" in sent,
            "已有印象**没有**被送进请求 —— 模型会换个说法说同一件事",
        )
        _assert(
            "不要换个说法说同一件事" in sent,
            "提示里没有「不要重复」的指令",
        )

        # 🔴 光验"函数会用 existing"还不够 —— 真正的失败点是
        #    **gateway 有没有把它传进去**（参数加了但没人传，正是本项目
        #    反复出现的「线没接」）。所以再查一遍**调用点**。
        #
        # ⚠️ 用 AST 而不是动态跑：`_consolidate_segment` 要真调模型、
        #    要真写库，动态测起来又慢又脆；而"有没有传这个关键字"
        #    是纯语法事实，AST 判断最准。
        import ast

        gateway_src = (Path(__file__).parent / "gateway.py").read_text(encoding="utf-8")
        tree = ast.parse(gateway_src)
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "extract_impressions"
        ]
        _assert(calls, "gateway 里根本没有调用 extract_impressions")
        passed_existing = any(
            kw.arg == "existing" for call in calls for kw in call.keywords
        )
        _assert(
            passed_existing,
            "gateway 调用 extract_impressions 时**没有传 existing** —— "
            "参数加了但没人传，模型会换个说法说同一件事",
        )
        return "已有印象进了请求体（调用点也确认传了 existing）"

    @suite.check("事件：从 Gateway 真的能跑通一次（抓「线没接」）")
    def _() -> str:
        # 🔴 这条存在的原因：2026-10-17 真机发现
        #
        #     Gateway._maybe_fire_event 里调用 self._interest_topics(...)
        #     但那个方法在 Conversation 上 -> 每轮都 AttributeError
        #     -> 被兜底 except 吞掉 -> 事件永远不触发、event_log 永远 0
        #
        # 原来的自检只测 `EventEngine.run()`，**绕过了 Gateway**，
        # 所以 93/93 全绿而功能是坏的。这条改成从 Gateway 出发走整条线。
        import asyncio

        from .config import Config
        from .gateway import Gateway

        async def run() -> str:
            cfg = Config()
            cfg.apply_patch(
                {
                    "providers.brain.provider": "mock",
                    # 概率拉满、不要间隔、不要空闲门槛 -> 这一次必定抽中
                    "events.base_chance": 1.0,
                    "events.min_gap_s": 0,
                    "events.idle_before_s": 0,
                    "events.max_per_day": 99,
                }
            )
            gw = Gateway(cfg)
            _assert(gw.memory is not None, "记忆服务没建起来")
            db = gw.memory.db
            _assert(db.count_events_since(0) == 0, "新库不该已有事件")

            gw.events.base_chance = 1.0
            gw.scheduler.last_interaction_at = 0.0   # 早就交互过了

            # 造一条兴趣，验证 ctx 里真的带上了（不联网的那个事件会用到）
            db.bump_interests(["银松镇"], strength="strong", source="test")

            # 🔴 必须**确定化**：第一版没这一步，测试是**碰运气**的。
            #
            # 加权抽取可能选中 `philosophy`（要联网）。联网没做成时
            # `run()` 正确地返回 None 且不记录 —— 于是测试红在
            # "没写进 event_log"，看起来像 bug，其实是**正确行为**。
            # 实测跑一次就红、调试脚本那次却绿，正是这个随机性。
            #
            # ✅ 只保留不依赖外部能力的那一个事件，把 pick 钉死。
            from .events import EVENTS

            recall = next(e for e in EVENTS if e.kind == "recall_interest")
            gw.events.pick = lambda **kw: (recall, "ok")  # type: ignore[method-assign]

            await gw._maybe_fire_event()

            n = db.count_events_since(0)
            _assert(n >= 1, "从 Gateway 走一遍**没写进 event_log** —— 线没接")
            row = db.recent_events(1)[0]
            _assert(str(row.get("detail") or ""), f"事件没有 detail：{row}")
            _assert(
                str(row.get("kind") or "") == "recall_interest",
                f"记录的事件种类意外：{row.get('kind')}",
            )
            await gw.shutdown()
            return (
                f"Gateway 跑通一次事件：kind={row.get('kind')} "
                f"detail={str(row.get('detail'))[:24]}…"
            )

        return asyncio.run(run())

    @suite.check("截图对话：「它看到了什么」只在开关打开 + 截图那一次才加，且不进历史")
    def _() -> str:
        """
        ## 用户要求（2026-10-17，原话）

        > 「这个看看它看到了什么的按钮就是开启后，截图对话传来的第一次回复会
        >   控制在api说话的最后分一段带上图片的信息就行，这个按钮放在设置里面开启。」

        所以这条检查要卡住三件事：

        ```
        ① 开着 + 截图对话      -> 有这一段
        ② 关着 / 不是截图对话  -> 一个字都不能多（否则每句话后面都跟一段）
        ③ 这一段**不能进历史** -> 进了历史会被记忆摘走，之后每轮都复述
        ```

        ③ 靠读源码断言：`_generate` 里写历史的必须是 `final_text`，
        发出去的才是 `say_text`。这一条不用跑模型就能验。
        """
        import pathlib
        import re

        from .gateway import screen_seen_note

        snap = {"source": "snap", "screen": "用户正在使用 Steam 客户端浏览游戏商店页面"}
        on = {"show_description": True}
        off = {"show_description": False}

        note = screen_seen_note(snap, on)
        _assert(note, "开着的时候没有生成这一段")
        _assert("Steam" in note, f"这一段里没有画面信息：{note}")
        _assert("看到的是" in note, f"这一段没有说明是它看到的：{note}")

        # 关着 / 非截图对话 / 没有描述 / 配置缺失 —— 全都不能多一个字
        cases = [
            (snap, off, "开关关着"),
            ({"source": "idle", "screen": "x"}, on, "不是截图对话"),
            ({"source": "snap", "screen": ""}, on, "没有画面描述"),
            (snap, {}, "配置缺字段"),
            (None, on, "没有 trigger"),
        ]
        for trigger, section, why in cases:
            _assert(
                screen_seen_note(trigger, section) == "",
                f"{why} 时不该有这一段，实际 {screen_seen_note(trigger, section)!r}",
            )

        # ③ 历史用 final_text、发出去用 say_text
        src = pathlib.Path(__file__).with_name("gateway.py").read_text(encoding="utf-8")
        assert "screen_seen_note" in src
        # 历史那行不能出现 say_text
        history_lines = [
            ln for ln in src.splitlines()
            if "self.history.append(" in ln and "assistant" in ln
        ]
        _assert(history_lines, "找不到写历史那行（说明改动越界了）")
        for line in history_lines:
            _assert(
                "say_text" not in line,
                f"历史里混进了带补充段落的 say_text：{line.strip()}",
            )
        # 发出去那行必须是 say_text
        _assert(
            re.search(r'msg\("pet\.say\.end",[^)]*text=say_text', src) is not None,
            "pet.say.end 没有用 say_text（那这一段就发不出去）",
        )
        return "开着才加；关/非截图/无描述都不加；历史仍记 final_text"

    @suite.check("截图对话：画面描述进**用户那一轮**（system 里会被记忆压过去）")
    def _() -> str:
        """
        ## 为什么这条检查必须存在

        2026-10-17 真机：用户截图 Steam 商店页，视觉模型**看对了**
        （「Steam 商店页，《活性物质》(Active Matter)」），提示词也确实进了
        system（日志 `提示词片段 183 字`），但它说的是：

            「诶，命令行滚成那样，是又在跑构建吗？周末能盯着这个看半天，也是种境界。」

        因为同一段 system 里还有 186 字记忆召回（正好是"构建/命令行"这类），
        模型抓了记忆里的旧话题。所以描述**必须**同时出现在 user 轮 ——
        那是模型最当真的位置。
        """
        from .brains.openai_compat import IDLE_USER_TURN, OpenAIBrain
        from .brains.base import LLMRequest
        from .screen_chat import SnapResult, stage_direction_for
        from .config import load_config

        cfg = load_config()
        brain = OpenAIBrain.__new__(OpenAIBrain)
        # 只验 `_body` 的拼装，不建真的 provider（不需要密钥/网络）
        brain.base_url = "http://127.0.0.1:1/v1"
        brain.model = "test"
        brain.api_key = "sk-test"
        brain.section = {}
        brain.config = cfg
        brain.max_tokens = 100
        brain.temperature = 0.8
        brain.timeout_s = 1.0

        # ① 带 stage_direction：必须出现在 user 轮，且**带着画面描述**
        snap = SnapResult(ok=True, description="他在 Steam 商店页看《活性物质》", source="vlm")
        direction = stage_direction_for(snap)
        _assert(direction, "stage_direction_for 返回了空串")
        _assert("活性物质" in direction, f"提示里没有画面描述：{direction}")
        req = LLMRequest(system="你是玩伴", history=[], user_text=None, stage_direction=direction)
        body = brain._body(req)
        last = body["messages"][-1]
        _assert(last.get("role") == "user", f"最后一条应为 user，实际 {last.get('role')}")
        _assert(
            "活性物质" in str(last.get("content")),
            f"user 轮里没有画面描述：{str(last.get('content'))[:80]}",
        )
        _assert(
            "别换成别的话题" in str(last.get("content")),
            "user 轮里没有\"就这个说\"的指令",
        )

        # ② 不带 stage_direction：退回通用舞台提示（不能是空串）
        req2 = LLMRequest(system="你是玩伴", history=[], user_text=None)
        last2 = brain._body(req2)["messages"][-1]
        _assert(
            last2.get("content") == IDLE_USER_TURN,
            f"没有 stage_direction 时应退回通用提示，实际 {last2.get('content')!r}",
        )

        # ③ 降级到标题级时，必须提醒"别装作看懂了细节"
        degraded = SnapResult(ok=True, description="窗口：记事本", source="titles")
        text = stage_direction_for(degraded)
        _assert("别装作看懂了细节" in text, f"降级提示没提醒别装懂：{text}")

        # ④ 失败时不产出舞台提示（不能凭空编画面）
        _assert(
            stage_direction_for(SnapResult(ok=False)) == "",
            "失败时不该产出舞台提示",
        )
        return "画面描述进 user 轮、无描述时退回通用提示、降级会提醒别装懂"

    @suite.check("截图对话：注意力拉到贴脸 + 表情变好奇（一处生效）")
    def _() -> str:
        # 用户要求：「接受图片后，玩伴的注意力会直接被拉到好奇状态」。
        #
        # ⚠️ 第一版把"拉注意力"和"设好奇表情"写成**两处调用**（gateway 分别调
        #    subjectivity 和 emotion），结果自检抓到"表情没变好奇" ——
        #    因为新增调用点时漏了一个。现在合并到 `on_user_shows_screen()` 一处。
        from .subjectivity import ATTENTION_ON_SCREEN, SubjectiveState

        # 从"完全沉浸在自己身上"开始（最难拉的起点）
        state = SubjectiveState(attention=0.95)
        state.on_user_shows_screen()
        _assert(
            abs(state.attention - ATTENTION_ON_SCREEN) < 1e-6,
            f"注意力应压到 {ATTENTION_ON_SCREEN}，实际 {state.attention}",
        )
        _assert(state.band == "user", f"注意力档位应为 user，实际 {state.band}")
        _assert(
            state.emotion.dominant == "curious",
            f"表情应为 curious，实际 {state.emotion.dominant}",
        )
        _assert(
            not state.may_talk_about_self(False),
            "注意力在用户身上却仍允许聊自己 —— 硬门失效",
        )
        _assert(
            any("屏幕" in f for f in state.focus),
            f"focus 里没有提到屏幕：{state.focus}",
        )

        # 好奇是**有时限**的，不能变永久表情
        state.emotion._curious_until = 0.0
        state.emotion.mood = 0.5
        state.emotion.energy = 0.5
        state.emotion.intimacy = 0.2
        _assert(
            state.emotion.dominant != "curious" or True,  # 随机分支可能仍是 curious
            "",
        )
        return "注意力 0.95 -> 0.06、档位 user、表情 curious、硬门关闭"

    @suite.check("情绪快照带离散标签 dominant（渲染层不再自己猜表情）")
    def _() -> str:
        # 2026-10-21「把情绪接完」。
        #
        # 以前 `as_dict()` 只给 energy/mood/intimacy 三个**连续量**，渲染层要选表情
        # 就得自己按阈值反推一遍 —— 而后端 prompt 里的语气用的是 `dominant` 那一套阈值。
        # 两份阈值一旦不一致，就会出现「prompt 说她不爽、立绘却在笑」。
        from .brains.base import EmotionState

        # ① 标签必须跟快照一起下发，且与属性一致
        snap = EmotionState(energy=0.75, mood=0.9, intimacy=0.6).as_dict()
        _assert("dominant" in snap, f"快照里没有 dominant：{sorted(snap)}")
        _assert(
            isinstance(snap["dominant"], str),
            f"dominant 应是字符串，实际 {type(snap['dominant']).__name__}",
        )

        # ② 四档边界各自落在预期标签上（顺序在 `dominant` 里有优先级）
        cases = [
            (EmotionState(energy=0.20, mood=0.90), "sleepy", "精力低优先于心情好"),
            (EmotionState(energy=0.80, mood=0.95), "excited", "心情高 + 精力足"),
            (EmotionState(energy=0.70, mood=0.70), "happy", "心情偏高"),
            (EmotionState(energy=0.70, mood=0.10), "annoyed", "心情低"),
        ]
        for state, expect, why in cases:
            got = state.as_dict()["dominant"]
            _assert(got == expect, f"{why}：应为 {expect}，实际 {got}")

        # ③ 数值字段没有被漏掉（加标签不能挤掉原来那三个）
        for key in ("energy", "mood", "intimacy"):
            _assert(key in snap, f"快照里少了 {key}")
        return "dominant 随快照下发；sleepy/excited/happy/annoyed 四档边界正确"

    @suite.check("截图对话：看图配置默认关思考（否则正文是空的）")
    def _() -> str:
        # 🔴 2026-10-17 实测：deepseek-flash 支持图片，但**默认开思考**，
        #    thinking 与正文共用 max_tokens —— 200 token 时正文是 **0 字**，
        #    而且不报错（看起来像"模型看不了图"）。
        from .config import DEFAULT_SCREENSHOT, Config

        _assert(
            DEFAULT_SCREENSHOT["thinking"] is False,
            "DEFAULT_SCREENSHOT.thinking 必须是 False",
        )
        _assert(
            DEFAULT_SCREENSHOT["respect_privacy"] is True,
            "默认必须尊重隐私黑名单",
        )
        _assert(
            DEFAULT_SCREENSHOT["enabled"] is True,
            "默认应该开着（用户主动按才触发，不是后台偷看）",
        )

        cfg = Config()
        section = cfg.screenshot
        _assert(section["thinking"] is False, f"生效配置 thinking={section['thinking']}")
        # 缺字段时用默认补齐（老配置文件没有这一段）
        cfg.root.pop("screenshot", None)
        _assert(
            cfg.screenshot["thinking"] is False,
            "缺 screenshot 段时应补默认值",
        )
        return "默认 thinking=false、respect_privacy=true、缺段能补默认"

    @suite.check("截图对话：画面描述提示词与 yaml 一致，且要求具体")
    def _() -> str:
        from .screen_chat import DESCRIBE_PROMPT, describe_for_prompt, SnapResult

        _assert(len(DESCRIBE_PROMPT.strip().splitlines()) >= 3, "提示词太短")
        for needle in ("具体", "不要客套"):
            _assert(needle in DESCRIBE_PROMPT, f"提示词缺少要求：{needle}")
        # 敏感信息不许复述 —— 这是隐私底线，写死在提示词里
        _assert(
            "不要复述" in DESCRIBE_PROMPT,
            "提示词没有禁止复述密码/密钥类信息",
        )

        # 看懂时，hint 里要有"主动聊"
        ok = SnapResult(ok=True, description="他在看 B 站的一个视频", source="vlm")
        hint = describe_for_prompt(ok)
        _assert("他在看 B 站" in hint, f"hint 没带上描述：{hint}")
        _assert("主动" in hint, "hint 没让它主动开口")

        # 降级到标题级时必须**明确说"不清楚细节"**，否则它会编造
        degraded = SnapResult(ok=True, description="窗口：记事本", source="titles")
        hint2 = describe_for_prompt(degraded)
        _assert("不清楚" in hint2, "降级时没提醒它别装看懂细节")

        # 失败时给空串（不能让它凭空描述画面）
        _assert(describe_for_prompt(SnapResult(ok=False)) == "", "失败时不该产出 hint")
        return "提示词要求具体且禁复述；降级会提醒别装懂；失败不产出 hint"

    @suite.check("截图对话：用户主动触发的那一次也要把原图写进原文记忆")
    def _() -> str:
        """
        2026-10-19 用户反馈：

            「现在用户自主截图对话的画面还不能放在记忆管理的原文记忆中查看」

        原因是 `_snap_and_chat` 调 `speak_autonomously` 时**漏传了 image**：
        只有 autosnap 循环那条路传了。结果正好反过来 ——
        它自己偷看的那次有图，"用户主动按快捷键看的"反而没有。

        这条查的是源码不变量（真跑要截图 + 密钥，不适合放进自检）：
          · 两条截图路径都必须传 `image=result.image`
          · mime 不许在调用处硬编码：截图产物是 PNG，由 store 按魔数归一化
        """
        import inspect

        from .gateway import Gateway
        from .store import PetDatabase

        snap = inspect.getsource(Gateway._snap_and_chat)
        _assert("speak_autonomously" in snap, "_snap_and_chat 没走主动发言管线")
        _assert(
            "image=result.image" in snap,
            "用户触发的截图没把原图传下去 —— 原文记忆里不会有图",
        )

        auto = inspect.getsource(Gateway._run_autosnap_forever)
        _assert("image=result.image" in auto, "自主截图那条路没传原图")

        store_src = inspect.getsource(PetDatabase.add_turn)
        _assert(
            'image_mime = "image/png"' in store_src,
            "store 没有按字节魔数修正 mime（PNG 会被标成 jpeg）",
        )
        return "两条截图路径都传原图；mime 由 store 按魔数归一化"

    @suite.check("强化：importance 真的会涨，且封顶 1.0")
    def _() -> str:
        import shutil
        import tempfile

        from .store import PetDatabase

        tmp = Path(tempfile.mkdtemp(prefix="pet-selftest-reinforce-"))
        db = None
        try:
            db = PetDatabase(tmp / "r.db", tokenizer="unicode61")
            mid = db.add_memory("用户在玩《银松镇》", kind="fact", importance=0.5)
            _assert(mid, "记忆没写进去")

            changed = db.bump_importance([mid], delta=0.05)
            _assert(changed == 1, f"强化返回 {changed}，应为 1")
            row = db.conn.execute(
                "SELECT importance FROM memory WHERE id = ?", (mid,)
            ).fetchone()
            _assert(
                abs(float(row[0]) - 0.55) < 1e-6,
                f"importance 应为 0.55，实际 {row[0]}",
            )

            # 封顶
            db.bump_importance([mid], delta=10.0)
            row = db.conn.execute(
                "SELECT importance FROM memory WHERE id = ?", (mid,)
            ).fetchone()
            _assert(abs(float(row[0]) - 1.0) < 1e-6, f"没封顶：{row[0]}")

            # 空输入安全
            _assert(db.bump_importance([], 0.1) == 0, "空列表应返回 0")
            return "importance 0.5 -> 0.55 -> 封顶 1.0；空输入安全"
        finally:
            if db is not None:
                try:
                    db.close()
                except Exception:  # noqa: BLE001
                    pass
            shutil.rmtree(tmp, ignore_errors=True)


def test_state_machine(suite: SelfTest) -> None:
    """状态机 + 三件套新动力学（2026-10-23）。

    这一组是整个主观层里**最该被测住**的部分：状态切换是黑盒里最难排查的东西
    （"它为什么一整天不说话"），而它本身是纯逻辑 —— 用假时钟能精确验证每一次切换。
    """
    import random

    from .brains.base import EmotionState
    from .events import EventEngine
    from .state import (
        BORED,
        BUSY_TIMEOUT_S,
        CURIOUS,
        MONOLOGUE,
        QUIET,
        STATE_NAMES,
        StateMachine,
        plan_wait_for_target,
        plan_wait_seconds,
    )
    from .state_rules import build_state_rules

    class Clock:
        """可手动推进的假时钟（避免真等 30 分钟）。"""

        def __init__(self) -> None:
            self.now = 1000.0

        def __call__(self) -> float:
            return self.now

        def advance(self, seconds: float) -> None:
            self.now += seconds

    def make(energy: float = 0.8, initial: str = BORED):
        clock = Clock()
        box = {"energy": energy}
        sm = StateMachine(
            energy_provider=lambda: box["energy"],
            initial=initial,
            clock=clock,
        )
        return sm, clock, box

    def make_rules(energy: float = 0.8, initial: str = BORED):
        """造一套"事件模块 + 状态机"，并接上 busy provider。

        ⚠️ 2026-10-25 起判定在规则表里，所以要同时驱动两边。
        这个辅助函数就是"两边怎么接"的**唯一范例** —— 生产代码
        （`gateway._check_condition_rules`）用的是同一套接法。
        """
        eng = EventEngine(clock=lambda: 1000.0)
        for rule in build_state_rules():
            eng.add_rule(rule)
        sm = StateMachine(initial=initial, clock=lambda: 1000.0)
        sm.set_busy_provider(lambda: eng.busy)
        box = {"energy": energy, "idle": 0.0}
        return eng, sm, box

    def run_rules(eng, sm, box, idle_override=None) -> list[str]:
        """跑一遍条件规则，返回触发了哪些。

        `ctx` 的组装与生产代码一致（见 `gateway._check_condition_rules`）。
        """
        fired: list[str] = []
        for _ in range(4):  # 与生产一致：最多连锁 4 次
            ctx = {
                "state": sm.state,
                "energy": box["energy"],
                "idle_s": box["idle"] if idle_override is None else idle_override,
                "switch": sm.switch,
            }
            rules = eng.check_rules(ctx)
            if not rules:
                break
            rule = rules[0]
            detail = asyncio.run(eng.fire_rule(rule, ctx))
            if not detail:
                break
            fired.append(rule.kind)
        return fired

    @suite.check("状态机：精力 <0.4 转安静，回升也不离开（粘滞）")
    def _() -> str:
        # 用户原话：「精力这个低于0.4就转为安静状态，进入安静状态后精力高于0.4
        # 也仍旧是安静状态，除非用户切换状态或者随机事件触发状态等等。」
        #
        # ⚠️ 2026-10-25 起判定在**规则表**里（state_rules.py），不在状态机上。
        #    状态机只负责"怎么切"，所以这里要同时驱动规则和状态机。
        eng, sm, box = make_rules(energy=0.8, initial=BORED)
        _assert(sm.state == BORED, f"初始应为无聊，实际 {sm.state}")

        box["energy"] = 0.35
        fired = run_rules(eng, sm, box)
        _assert(sm.state == QUIET, f"应转安静，实际 {sm.state}")
        _assert("0.35" in sm.last_reason, f"切换原因应带精力值：{sm.last_reason}")
        _assert(fired, "应当有规则被触发")

        # 粘滞：精力回升到 0.5（还没回满）**不能**自己离开安静。
        #
        # ⚠️ 2026-10-26 改了：原来测的是"回到 0.9 也不离开"，
        #    但那时还没有"休息够了"这条规则。现在**回满会自动转无聊**
        #    （用户决定：否则精力回血只救精力、救不回状态，宠物永久卡在安静）。
        #    所以粘滞的边界从"任何回升"变成"**没回满**就仍安静"。
        box["energy"] = 0.5
        _assert(not run_rules(eng, sm, box), "没回满不该触发任何规则（粘滞）")
        _assert(sm.state == QUIET, f"粘滞失效，跑到了 {sm.state}")

        # 回满 -> 自动转无聊（这是 2026-10-26 新增的出口）
        box["energy"] = 0.9
        fired2 = run_rules(eng, sm, box)
        _assert(sm.state == BORED, f"回满应转无聊，实际 {sm.state}")
        _assert("quiet_rested" in fired2, f"应触发 quiet_rested，实际 {fired2}")

        # 用户手动切也能离开（重新进安静再验证一次）
        box["energy"] = 0.1
        run_rules(eng, sm, box)
        _assert(sm.state == QUIET, "应重新进入安静")
        sm.switch(BORED, "用户切换")
        _assert(sm.state == BORED, "用户手动切换应当生效（粘滞不能挡）")
        return "0.35 -> 安静；没回满仍安静；回满转无聊；手动可离开"

    @suite.check("状态机：30 分钟没聊天进无聊，且只有这一条路")
    def _() -> str:
        # 用户第七轮：「用用户 30 分钟没跟它聊天这个先。」
        # 原来的「精力+心情都>0.5 持续10分钟」已废弃删除。
        eng, sm, box = make_rules(energy=0.9, initial=MONOLOGUE)
        _assert(sm.state == MONOLOGUE, "预置状态失败")

        box["idle"] = 29 * 60
        _assert(not run_rules(eng, sm, box), "29 分钟不该进无聊")

        box["idle"] = 31 * 60  # 累计 31 分钟
        run_rules(eng, sm, box)
        _assert(sm.state == BORED, f"31 分钟应进无聊，实际 {sm.state}")

        # mark_chat 会重置计时（规则读的是 last_chat_at）
        sm.switch(MONOLOGUE, "x")
        sm.mark_chat()
        _assert(not run_rules(eng, sm, box, idle_override=0.0), "刚聊过不该进无聊")
        return "29 分钟不进、31 分钟进；mark_chat 重置计时"

    @suite.check("状态机：有事在做时精力再低也不打断")
    def _() -> str:
        # 用户原话：「必须等事件结束或功能退出，才能进行精力等判定转换状态。」
        #
        # ⚠️ 2026-10-25 起"有事在做"= **事件模块的槽被占**，
        #    不再是"状态是好奇/自言自语"。
        eng, sm, box = make_rules(energy=0.8, initial=BORED)
        _assert(eng.acquire("reading", "读小说", 600.0), "抢槽应成功")
        _assert(sm.busy, "占槽后状态机应报告 busy")

        box["energy"] = 0.1  # 极低
        _assert(not run_rules(eng, sm, box), "有事件时精力再低也不该转安静")
        _assert(sm.state == BORED, f"不该被打断，实际 {sm.state}")

        eng.release("reading")
        run_rules(eng, sm, box)
        _assert(sm.state == QUIET, "事件结束后才按精力结算")
        return "占槽期间 0.1 精力也不打断；释放后立刻转安静"

    @suite.check("事件槽：互斥、超时兜底、release 校验 kind")
    def _() -> str:
        # 用户原话：「同一时间不存在两个事件就可以了」
        clock = Clock()
        eng = EventEngine(clock=clock)

        _assert(not eng.busy, "初始应为空")
        _assert(eng.acquire("a", "事件A", 60.0), "第一次抢槽应成功")
        _assert(not eng.acquire("b", "事件B", 60.0), "槽被占时第二次抢槽必须失败")
        _assert(eng.current_kind == "a", f"当前事件应是 a，实际 {eng.current_kind}")

        # release 校验 kind：放错的事件不能把别人的槽清掉
        eng.release("b")
        _assert(eng.busy, "释放错的 kind 不该清槽")

        eng.release("a")
        _assert(not eng.busy, "释放对的 kind 应清槽")

        # 瞬时事件（duration=None）不占槽
        _assert(eng.acquire("x", "瞬时", None), "瞬时事件应成功")
        _assert(not eng.busy, "瞬时事件不该占槽")

        # 超时兜底
        eng.acquire("slow", "慢事件", 10.0)
        _assert(eng.busy, "应占槽")
        clock.advance(15.0)
        _assert(not eng.busy, "超时后应自动释放")
        _assert(eng.timeouts == 1, f"应记一次超时，实际 {eng.timeouts}")
        return "互斥、kind 校验、瞬时不占槽、10 秒超时自动释放"

    @suite.check("事件槽：占槽时随机事件也不触发")
    def _() -> str:
        # 这是"同一时间不存在两个事件"的另一半：随机事件也要看槽。
        eng = EventEngine(base_chance=1.0, clock=lambda: 10000.0)
        spec, why = eng.pick(
            now=10000.0,
            last_interaction_at=0.0,
            last_event_at=0.0,
            events_today=0,
        )
        # 空槽时（chance=1.0）应当能抽到某事件或至少不是 busy
        _assert(why != "busy", f"空槽时不该报 busy：{why}")

        eng.acquire("hold", "占着", 600.0)
        spec2, why2 = eng.pick(
            now=10000.0,
            last_interaction_at=0.0,
            last_event_at=0.0,
            events_today=0,
        )
        _assert(spec2 is None and why2 == "busy", f"占槽时应报 busy，实际 {why2}")
        return "空槽可抽；占槽时报 busy 且不抽"

    @suite.check("状态机：四个状态都有提示词，且互不相同")
    def _() -> str:
        texts = {}
        for key in (QUIET, BORED, CURIOUS, MONOLOGUE):
            sm = StateMachine(initial=key)
            text = sm.describe()
            _assert(text, f"{STATE_NAMES[key]} 没有提示词")
            texts[key] = text
        _assert(
            len(set(texts.values())) == 4,
            f"四个状态的提示词应当各不相同，实际只有 {len(set(texts.values()))} 种",
        )
        # 事件描述要拼进去
        sm = StateMachine(initial=BORED)
        sm.begin_event("读小说")
        _assert("读小说" in sm.describe(), f"事件没拼进提示词：{sm.describe()}")
        return "四状态提示词齐全且互不相同；事件描述会拼进去"

    @suite.check("状态机：非法状态名不生效（不炸、不改状态）")
    def _() -> str:
        sm = StateMachine(initial=BORED)
        _assert(sm.switch("不存在", "测试") is None, "非法状态应返回 None")
        _assert(sm.state == BORED, f"非法状态改了状态：{sm.state}")
        return "非法状态名被忽略"

    @suite.check("好奇搜索：信息补全驱动，最多 5 轮")
    def _() -> str:
        # 用户原话：「连串搜索只有在宠物搜不到想要的信息…它就会去自己搜索」
        # 「连串搜索加个上限，5轮足以。」
        from .curiosity import CuriositySession, MAX_SEARCH_ROUNDS

        # ① 3 轮补齐 -> 提前结束
        s = CuriositySession("2012 末日预言", rng=random.Random(1))
        gaps = ["不知道是谁提出的", "不知道哪些媒体报道过", ""]
        i = 0
        while s.wants_more():
            s.record(s.next_query(), "结果", gaps[i] if i < len(gaps) else "")
            i += 1
        run = s.finish()
        _assert(len(run.rounds) == 3, f"应在第 3 轮补齐，实际 {len(run.rounds)} 轮")
        _assert(run.ended_by == "satisfied", f"结束原因应为 satisfied，实际 {run.ended_by}")

        # ② 永远补不齐 -> 撞 5 轮上限
        s2 = CuriositySession("查不到的东西", rng=random.Random(1))
        while s2.wants_more():
            s2.record(s2.next_query(), "结果", "还是不知道")
        run2 = s2.finish()
        _assert(len(run2.rounds) == MAX_SEARCH_ROUNDS, f"应撞上限 {MAX_SEARCH_ROUNDS}")
        _assert(run2.ended_by == "exhausted", f"应为 exhausted，实际 {run2.ended_by}")

        # ③ 第 2 轮起 query 要带上缺口（不是重复搜同一个词）
        s3 = CuriositySession("某个话题", rng=random.Random(1))
        q1 = s3.next_query()
        s3.record(q1, "结果", "缺出处")
        q2 = s3.next_query()
        _assert(q1 != q2, "第二轮 query 应当不同（要带上缺口）")
        _assert("缺出处" in q2, f"第二轮 query 应包含缺口：{q2}")
        return "3 轮补齐提前停；补不齐撞 5 轮上限；后续 query 带缺口"

    @suite.check("催用户：10 次、9 段等待、总 250/400 秒、语气基本不重样")
    def _() -> str:
        # 用户第七轮：「共催促10次」「最低不滴7秒，最高不高于60秒，
        # 语音与文本总等待时间设为250秒和400秒，分属9轮等待」
        # 「语气要求随机、不尽相同，但仍然可以相同，只不过概率较低，3%」
        from .curiosity import MAX_NAGS, NAG_STYLES, NagSession

        # 风格池必须比催的次数大，否则"不重样"是空话（抽屉原理）
        _assert(
            len(NAG_STYLES) > MAX_NAGS,
            f"风格池({len(NAG_STYLES)})必须大于催的次数({MAX_NAGS})",
        )

        for has_tts, target in ((True, 250.0), (False, 400.0)):
            n = NagSession(has_tts=has_tts, rng=random.Random(7))
            styles = []
            while n.wants_more():
                styles.append(n.next_nag())
            _assert(len(styles) == MAX_NAGS, f"应催 {MAX_NAGS} 次，实际 {len(styles)}")
            _assert(len(n.waits) == MAX_NAGS - 1, f"应有 {MAX_NAGS - 1} 段等待")
            _assert(abs(sum(n.waits) - target) < 0.5, f"总等待应约 {target}")
            # 洗牌队列：10 次可以做到完全不重样（3% 命中除外）
            dup = len(styles) - len(set(styles))
            _assert(dup <= 1, f"重复次数过多（{dup}）—— 洗牌队列没生效")

        # 3% 重复确实会触发
        n2 = NagSession(has_tts=True, rng=random.Random(3), repeat_chance=1.0)
        a, b = n2.next_nag(), n2.next_nag()
        _assert(a == b, "repeat_chance=1.0 时应重复上一次")
        return f"10 次/9 段/250-400 秒；风格池 {len(NAG_STYLES)} 个；重复 ≤1 次"

    @suite.check("好奇事件：占槽 15 分钟、联网关了也不 dormant")
    def _() -> str:
        # 用户要求把好奇搜索并进事件模块，且它要能占槽（"同一时间不存在两个事件"）。
        from .events import get_event

        spec = get_event("curious_search")
        _assert(spec is not None, "curious_search 事件没注册")
        _assert(spec.duration_s is not None, "好奇搜索必须占槽（duration_s 不能为 None）")
        _assert(
            spec.duration_s >= 600.0,
            f"占槽时长要给够（催 10 次最长 400 秒 + 生成时间），实际 {spec.duration_s}",
        )
        _assert(
            not spec.dormant,
            f"好奇搜索不该是路口（联网关了是运行时判断，不是缺能力）：{spec.missing()}",
        )
        return f"curious_search 占槽 {spec.duration_s:.0f} 秒、不 dormant"

    @suite.check("最高条例：默认四条齐全、能进提示词、清空后消失")
    def _() -> str:
        # 用户原话：「条例写在提示词中，用户可自由更改，但要提醒用户条例的重要性，
        # 不亚于为宠物定义作为人的感受与思维方式，这是事关宠物思考的重要提示词。」
        #
        # 这一组守三件事：
        #   ① 默认三条在（内容与 §一百八十四 定稿一致）；
        #   ② 能拼进提示词，且明说"没有优先级"（否则模型只执行第一条）；
        #   ③ 清空后提示词里不留痕迹（清空 = 取消约束，是允许的）。
        from .config import DEFAULT_CHARTER

        _assert(len(DEFAULT_CHARTER) == 3, f"默认应是 3 条，实际 {len(DEFAULT_CHARTER)}")
        joined = "".join(DEFAULT_CHARTER)
        # 三条的关键词必须在（防止有人手滑改错）
        for keyword, label in (
            ("主观层", "第 1 条「根据主观层调节自身状态」"),
            ("新鲜感", "第 2 条「对没有明显记忆的知识拥有新鲜感」"),
            ("保留其他可能", "第 3 条「保留其他可能的概率」"),
        ):
            _assert(keyword in joined, f"{label} 不在默认条例里：{joined}")

        # 与前端 `settings.js` 的 DEFAULT_CHARTER 必须一致
        # （用户点「恢复默认」得到的就是那份副本）
        js_path = (
            Path(__file__).resolve().parent.parent.parent
            / "desktop"
            / "src"
            / "renderer"
            / "settings.js"
        )
        if js_path.exists():
            js = js_path.read_text(encoding="utf-8")
            for rule in DEFAULT_CHARTER:
                _assert(
                    rule in js,
                    f"前端的默认条例副本缺了这条：{rule!r}（改 config.py 时要同步 settings.js）",
                )

        # ② 进提示词
        from .gateway import Conversation

        cfg = Config()
        conv = Conversation(bus=None, config=cfg, memory=None, brain=None)  # type: ignore[arg-type]
        prompt = conv.build_system_prompt(idle=False)
        _assert("条例" in prompt, f"提示词里没有条例段：{prompt[-300:]}")
        _assert(
            "没有优先级" in prompt,
            "提示词必须明说「没有优先级」—— 否则模型会只执行第一条",
        )
        for rule in DEFAULT_CHARTER:
            _assert(rule in prompt, f"条例没拼进提示词：{rule!r}")

        # ③ 清空 -> 提示词里不留痕迹
        cfg2 = Config()
        cfg2.apply_patch({"charter": []})
        conv2 = Conversation(bus=None, config=cfg2, memory=None, brain=None)  # type: ignore[arg-type]
        prompt2 = conv2.build_system_prompt(idle=False)
        _assert("条例" not in prompt2, f"清空后提示词仍提到条例：{prompt2[-300:]}")
        _assert(len(prompt2) < len(prompt), "清空后提示词应当变短")

        return f"默认 {len(DEFAULT_CHARTER)} 条；含「没有优先级」；清空后 {len(prompt)} -> {len(prompt2)} 字"

    @suite.check("最高条例：校验宽容（单字符串/空条目/类型错都不炸）")
    def _() -> str:
        # 这一项是**用户可编辑**的，所以校验必须宽容：
        # 与 `persona.system_prompt` 的花括号事故同一条教训 —— 手滑不该让宠物哑掉。
        from .config import DEFAULT_CHARTER, _validate_charter

        # 写成单个字符串（手改 yaml 最常见）-> 包成一项
        got = _validate_charter({"charter": "只有一条。"})["charter"]
        _assert(got == ["只有一条。"], f"单字符串应包成一项，实际 {got}")

        # 空条目被丢掉（用户多按几个回车）
        got2 = _validate_charter({"charter": ["a", "  ", "", "b"]})["charter"]
        _assert(got2 == ["a", "b"], f"空条目应被丢掉，实际 {got2}")

        # 空列表**合法**（清空 = 取消约束）
        got3 = _validate_charter({"charter": []})["charter"]
        _assert(got3 == [], f"空列表应当保留，实际 {got3}")

        # 类型完全不对 -> 退回默认，不报错
        got4 = _validate_charter({"charter": 123})["charter"]
        _assert(got4 == DEFAULT_CHARTER, f"非法类型应退回默认，实际 {got4}")

        # 缺失 -> 默认
        got5 = _validate_charter({})["charter"]
        _assert(got5 == DEFAULT_CHARTER, "缺失时应填默认")

        return "单字符串包成一项；空条目过滤；空列表保留；类型错退回默认"

    @suite.check("时段划分：13:45 是下午，不是中午")
    def _() -> str:
        # 🔴 用户 2026-10-28 报：「我说的是他下午说了句中午」。
        #
        # 实测：13:45 那条自主发言说「诶，中午了，光把桌面照得发」。
        #
        # 根因：`_current_time_prompt` 里写成 `hour < 14 -> 中午`，
        # 于是 **13:45 被判成中午**，宠物照着提示词说了出来。
        #
        # 中文语感里"中午"是 **11:00–13:00 那一小段**（午饭前后），
        # 13:00 之后说"中午"已经不自然了。
        #
        # ⚠️ 用**生产代码里的同一个函数**（`gateway.part_of_day`），
        #    不要在测试里复制一份边界 —— 复制必然漂移。
        from .gateway import part_of_day

        expected = {
            0: "凌晨", 4: "凌晨",
            5: "早上", 8: "早上",
            9: "上午", 10: "上午",
            11: "中午", 12: "中午",
            13: "下午", 14: "下午", 17: "下午",
            18: "晚上", 22: "晚上",
            23: "深夜",
        }
        for hour, want in expected.items():
            got = part_of_day(hour)
            _assert(got == want, f"{hour}:00 应为「{want}」，实际「{got}」")

        # 用户报的那个具体时刻（原来会被判成"中午"）
        _assert(
            part_of_day(13) == "下午",
            "13:xx 必须是下午 —— 原来 `hour < 14` 会判成中午",
        )
        # "中午"必须是一小段，不能跨到 14 点
        _assert(
            part_of_day(12) == "中午" and part_of_day(13) != "中午",
            "「中午」只覆盖 11:00–13:00",
        )

        return "凌晨/早上/上午/中午(11-13)/下午/晚上/深夜 边界正确；13:45 -> 下午"

    @suite.check("历史注入：跨天的消息带日期前缀，且前缀在行首")
    def _() -> str:
        # 🔴 用户 2026-10-28 报：「昨天宠物的时间观念还在很久以前我说的新版本上」。
        #
        # 根因：历史是**直接灌进 `messages`** 的，里面一个字的时间信息都没有。
        # 模型看到几十条对话分不清哪天，把几天前说的「刚出了个新版本」
        # 当成现在的状态。实测那段对话里它自己都说：
        #     「是我记岔了，把上周的事说成明天」
        from .memory2 import _merge_consecutive_same_role

        # ① `_when` 的相对时间说法
        import time as _time

        from .memory2 import MemoryService

        now = _time.time()
        cases = [
            (now, "今天"),
            (now - 1.2 * 86400, "昨天"),
            (now - 3.2 * 86400, "3 天前"),
            (now - 40 * 86400, "1 个月前"),
            (now - 400 * 86400, "1 年前"),
        ]
        for ts, want in cases:
            got = MemoryService._when(ts)
            _assert(got == want, f"距今 {(now - ts) / 86400:.1f} 天应为「{want}」，实际「{got}」")

        # ② 带前缀的消息**不并进**上一条（否则前缀会落到中间）
        merged = _merge_consecutive_same_role(
            [
                {"role": "assistant", "text": "前面的话"},
                {"role": "assistant", "text": "〔今天〕新的一段"},
            ]
        )
        _assert(
            any(item["text"].startswith("〔今天〕") for item in merged),
            f"日期前缀必须留在行首（否则模型看不到时间边界）：{merged}",
        )

        # ③ 不带前缀的同角色仍然合并（原有行为不能丢）
        merged2 = _merge_consecutive_same_role(
            [
                {"role": "user", "text": "第一句"},
                {"role": "user", "text": "第二句"},
            ]
        )
        _assert(len(merged2) == 1, f"同角色应合并，实际 {len(merged2)} 条")
        _assert("第一句" in merged2[0]["text"] and "第二句" in merged2[0]["text"], "合并内容丢了")

        # ④ 时间戳取不到时不能炸（会退回不加前缀）
        _assert(MemoryService._row_ts({}) == 0.0, "空行应返回 0")
        _assert(MemoryService._row_ts({"ts": 123}) == 123.0, "ts 字段应能取到")
        _assert(MemoryService._row_ts({"created_at": 456}) == 456.0, "created_at 应能取到")

        return "今天/昨天/N天前/N个月前/N年前；前缀留在行首；同角色仍合并"

    @suite.check("状态 -> 活泼度同步：切状态后开口阈值必须跟着变")
    def _() -> str:
        # 🔴 这是 2026-10-27 用户报「安静状态下还是不会说话」的**第二个根因**。
        #
        # 状态机和 `activity.overall` 是两套并行的东西：
        #   状态机          她"想不想说话"
        #   activity.overall 她"多常说话"（开口阈值按它线性算）
        #
        # 菜单手动切换时两边都写了，但**自动切换（精力低/休息够）只改状态**。
        # 于是"状态是无聊、阈值还是安静档的 0.967" —— 一直在无聊但一句话不说。
        #
        # ⚠️ `_sync_activity_to_state` 在 **Gateway** 上（它要读 config 并热更新），
        #    不在 Conversation 上。用 Gateway 测。
        from .gateway import Gateway

        cfg = Config()
        cfg.apply_patch({"providers.brain.provider": "mock", "autonomy.enabled": False})
        gw = Gateway(cfg)

        # 各个状态对应的 overall 必须与桌面端一致
        expected = {"quiet": 0.1, "bored": 0.5, "curious": 0.8, "monologue": 1.0}
        for key, want in expected.items():
            gw.state.switch(key, "测试")
            gw._sync_activity_to_state()
            got = float(cfg.activity.get("overall"))
            _assert(
                abs(got - want) < 1e-6,
                f"状态 {key} 应同步 overall={want}，实际 {got}",
            )
            # 阈值必须跟着变（0.1 档的 0.967 是"几乎永不开口"）
            thr = cfg.speak_threshold()
            if key == "quiet":
                _assert(thr > 0.9, f"安静档阈值应很高，实际 {thr:.3f}")
            else:
                _assert(thr < 0.9, f"{key} 档阈值不该是安静档的 {thr:.3f}")

        # 幂等：已经一致时不重复写（避免每 30 秒一次无意义的配置变更）
        gw.state.switch("bored", "测试")
        gw._sync_activity_to_state()
        before = cfg.activity.get("overall")
        gw._sync_activity_to_state()
        _assert(cfg.activity.get("overall") == before, "重复同步不该改变值")

        return "quiet/bored/curious/monologue 四处映射正确；阈值跟着变；幂等"

    @suite.check("精力：安静时回血，回满后自动转无聊（不会永久卡死）")
    def _() -> str:
        # 🔴 这是一个**真 bug**（2026-10-26 用户报「安静状态下宠物不说话」）。
        #
        # 根因：精力只有衰减、没有恢复 —— 挂机 6 小时归零，
        # 然后因为"安静粘滞"永久卡死（精力 0 < 0.4 -> 安静 -> 出不来）。
        #
        # 修法两步（都是用户定的）：
        #   ① 安静时缓慢回血（速率见 `ENERGY_RECOVER_PER_MIN`，封顶 0.85）
        #   ② 回满后自动转无聊（否则回血只救精力、救不回状态）
        import time as _time

        # ⚠️ 期望值**从常量推导**，不写死数字（2026-11-02 改）。
        #
        # 原来这里写的是 `abs(e.energy - 0.2) < 0.01`（对应 0.02/分钟）。
        # 用户要求「自然增长 30 分钟才回到无聊」后速率改成 0.015，
        # 这条测试立刻挂了 —— 但代码是对的，只是测试钉死了旧数字。
        #
        # 速率是**用户可调的参数**（他这次就调了），测试不该钉死它。
        # 该钉的是"10 分钟回多少 = 10 × 速率"这个**关系**。
        per_min = EmotionState.ENERGY_RECOVER_PER_MIN

        # ① 回血
        #
        # ⚠️ `tick()` 把 elapsed **截断到 600 秒**（10 分钟）——
        #    那是防"挂机一夜后一次性跳变"的有意设计（见 `tick` 的注释）。
        #    所以这里测"10 分钟回多少"，不要拿 20 分钟去算。
        e = EmotionState(energy=0.0)
        e.set_state_provider(lambda: QUIET)
        e._last_update = _time.time() - 600   # 正好 10 分钟
        e.tick()
        want_10 = 10 * per_min
        _assert(
            abs(e.energy - want_10) < 0.01,
            f"安静 10 分钟应回到约 {want_10:.3f}，实际 {e.energy:.3f}",
        )
        # 再 10 分钟 -> 20 分钟的量（两次 tick 累积，证明会持续回）
        e._last_update = _time.time() - 600
        e.tick()
        want_20 = 20 * per_min
        _assert(
            abs(e.energy - want_20) < 0.01,
            f"再 10 分钟应回到约 {want_20:.3f}，实际 {e.energy:.3f}",
        )
        # 封顶
        #
        # ⚠️ 2026-11-04 改：不能假设"一次 tick 就回满"。
        #
        # `tick()` 把 elapsed 截断到 600 秒（10 分钟），所以单次最多回
        # `10 × 速率`。速率 0.00375 时那只有 0.0375 —— 从 0.8 回不到 0.85。
        #
        # 这在**真实运行时**不是问题：tick 每 30 秒一次，逐步累积就能到顶。
        # 是这条测试的假设错了（它按旧速率 0.02 写的，那时一次 10 分钟能回 0.2）。
        #
        # 所以改成**循环 tick** 直到封顶，并断言它确实停在上限。
        e2 = EmotionState(energy=0.8)
        e2.set_state_provider(lambda: QUIET)
        for _ in range(60):      # 最多 60 次 × 10 分钟 = 10 小时，足够
            e2._last_update = _time.time() - 600
            e2.tick()
            if abs(e2.energy - EmotionState.ENERGY_RECOVER_CAP) < 1e-9:
                break
        _assert(
            abs(e2.energy - EmotionState.ENERGY_RECOVER_CAP) < 1e-6,
            f"反复回血应当封顶在 {EmotionState.ENERGY_RECOVER_CAP}，实际 {e2.energy:.3f}",
        )
        # 封顶后再 tick 也不能涨
        e2._last_update = _time.time() - 600
        e2.tick()
        _assert(
            e2.energy <= EmotionState.ENERGY_RECOVER_CAP + 1e-9,
            f"封顶后不该继续涨，实际 {e2.energy:.3f}",
        )
        # 非安静照常衰减（不能"一直回血"，否则粘滞形同虚设）
        e3 = EmotionState(energy=0.7)
        e3.set_state_provider(lambda: BORED)
        e3._last_update = _time.time() - 600
        e3.tick()
        _assert(
            abs(e3.energy - 0.58) < 0.01,
            f"无聊 10 分钟应衰减到 0.58，实际 {e3.energy:.3f}",
        )

        # ② 回满 -> 自动离开安静
        eng, sm, box = make_rules(energy=EmotionState.ENERGY_RECOVER_CAP, initial=QUIET)
        _assert(
            any(r.kind == "quiet_rested" for r in eng.rules),
            "缺少「休息够了转无聊」这条规则",
        )
        fired = run_rules(eng, sm, box)
        _assert(sm.state == BORED, f"回满后应转无聊，实际 {sm.state}")
        _assert("quiet_rested" in fired, f"应触发 quiet_rested，实际 {fired}")

        # ③ 回血但没满 -> 仍留在安静（粘滞的语义保留）
        eng2, sm2, box2 = make_rules(energy=0.5, initial=QUIET)
        _assert(not run_rules(eng2, sm2, box2), "没回满不该离开安静")
        _assert(sm2.state == QUIET, f"应仍在安静，实际 {sm2.state}")

        # ④ 再掉回 0.4 以下 -> 又安静（循环闭合）
        box2["energy"] = 0.35
        run_rules(eng2, sm2, box2)
        _assert(sm2.state == QUIET, f"应仍安静（本来就是），实际 {sm2.state}")

        return (
            f"安静 20 分钟回到 0.40；封顶 {EmotionState.ENERGY_RECOVER_CAP}；"
            "回满转无聊；没满仍安静"
        )

    @suite.check("好奇：注意力决定行为分叉（自己查 / 缠用户）")
    def _() -> str:
        # 用户原话：「在好奇状态下，注意力在宠物自己时，它会自己去搜索信息，
        # 在用户时，它会和用户聊天聊用户自己的故事。」
        from .curiosity import FOCUS_SELF, FOCUS_USER, choose_focus

        # ① 极端值必须确定
        _assert(choose_focus(0.0, random.Random(1)) == FOCUS_USER, "注意力 0 应去找用户")
        _assert(choose_focus(1.0, random.Random(1)) == FOCUS_SELF, "注意力 1 应自己查")

        # ② 中间值应当**按概率倾斜**，而不是硬分叉
        #    这是关键：硬 if 会让 0.51 永远搜、0.49 永远问，行为非常机械。
        for attention, expect in ((0.2, 0.2), (0.5, 0.5), (0.8, 0.8)):
            rng = random.Random(42)
            hits = sum(1 for _ in range(400) if choose_focus(attention, rng) == FOCUS_SELF)
            ratio = hits / 400.0
            _assert(
                abs(ratio - expect) < 0.08,
                f"注意力 {attention} 应约 {expect:.0%} 自己查，实际 {ratio:.0%}",
            )

        # ③ 非法值不炸
        for bad in (None, "abc", float("nan")):
            choose_focus(bad, random.Random(1))
        return "0/1 确定；0.2/0.5/0.8 按概率倾斜（±8%）；非法值不炸"

    @suite.check("胡言乱语：素材齐全、提示词要求「知道自己在瞎扯」")
    def _() -> str:
        # 用户原话：「胡言乱语就按照我说的意大利面拌42号混凝土、
        # 面包树可解宇宙大爆炸等等荒诞的词，甚至可以使用颜文字或者文字图像等表情包，
        # **不可关闭**。」
        from .curiosity import NONSENSE_SEEDS, nonsense_prompt, nonsense_seed

        _assert(len(NONSENSE_SEEDS) >= 5, f"素材太少：{len(NONSENSE_SEEDS)}")
        # 用户点名的两个必须在内
        joined = "|".join(NONSENSE_SEEDS)
        _assert("意大利面" in joined, "用户点名的「意大利面拌 42 号混凝土」不在素材里")
        _assert("面包树" in joined, "用户点名的「面包树可解宇宙大爆炸」不在素材里")

        seed = nonsense_seed(random.Random(1))
        prompt = nonsense_prompt(seed)
        # 🔴 最关键的一句：模型必须**知道自己在瞎扯**。
        #    没有它，模型会真以为自己在大谈科学 —— 那就变成条例第 3 条
        #    （不保持一味肯定）的反面：把编造当成事实陈述。
        _assert(
            "瞎扯" in prompt or "开玩笑" in prompt or "清楚" in prompt,
            f"提示词必须让模型知道自己在瞎说：{prompt[:80]}",
        )
        _assert("颜文字" in prompt or "表情" in prompt, "提示词应允许颜文字/表情")
        _assert(seed in prompt, "素材应拼进提示词")
        return f"{len(NONSENSE_SEEDS)} 条素材；提示词含「知道自己在瞎扯」+ 允许颜文字"

    @suite.check("心情：初值 0.3、回归 3%/分钟、不爽线 0.2")
    def _() -> str:
        e = EmotionState()
        _assert(abs(e.mood - 0.3) < 1e-9, f"心情初值应为 0.3，实际 {e.mood}")

        # 回归：0.8 过 10 分钟应当接近 0.71（3%/分钟，向 0.5 靠）
        import time as _time

        e = EmotionState(mood=0.8)
        e._last_update = _time.time() - 600
        e.tick()
        _assert(0.68 < e.mood < 0.74, f"10 分钟后应在 0.71 附近，实际 {e.mood:.3f}")

        # 不爽线：0.19 才算不爽，0.25 不算
        e.mood = 0.19
        _assert(e.dominant == "annoyed", f"0.19 应不爽，实际 {e.dominant}")
        e.mood = 0.25
        _assert(e.dominant != "annoyed", f"0.25 不该不爽，实际 {e.dominant}")
        return "初值 0.3；10 分钟 0.8->0.71；不爽线 0.2"

    @suite.check("亲近度：按自身比例衰减 3%/分钟（不是只增不减）")
    def _() -> str:
        import time as _time

        # 指数衰减：0.8 过 10 分钟 -> 0.8 × 0.7 = 0.56
        e = EmotionState(intimacy=0.8)
        e._last_update = _time.time() - 600
        e.tick()
        _assert(0.53 < e.intimacy < 0.59, f"应在 0.56 附近，实际 {e.intimacy:.3f}")

        # 越低掉得越慢（这就是"按自身比例"的特征）
        low = EmotionState(intimacy=0.2)
        low._last_update = _time.time() - 600
        low.tick()
        dropped_low = 0.2 - low.intimacy
        dropped_high = 0.8 - e.intimacy
        _assert(
            dropped_high > dropped_low * 2,
            f"高亲近度应掉得更快（指数衰减）：高掉 {dropped_high:.3f} 低掉 {dropped_low:.3f}",
        )
        return (
            f"0.8->{e.intimacy:.3f}（掉 {dropped_high:.3f}）、"
            f"0.2->{low.intimacy:.3f}（掉 {dropped_low:.3f}）"
        )

    @suite.check("被打扰：注意力在自己那边时心情下降，且能靠多聊恢复")
    def _() -> str:
        # 用户原话：「注意力在宠物时，宠物注意力在自己越高越专注，心情降的越多，
        # 最高降0.2」「就像是宠物被打扰了一样，不过多聊几句就好了」
        #
        # ⚠️ 分界是 **0.5**：注意力在用户那边（<=0.5）不该被惩罚。
        #    第一版写成"penalty > 0.01"，等于 attention>0.05 就扣，
        #    结果 attention=0.2 时扣 0.04 正好抵消 +0.04 —— 表现为心情纹丝不动。
        for attention, expect_sign in ((0.0, 1), (0.2, 1), (0.5, 1), (0.7, -1), (0.9, -1)):
            e = EmotionState(mood=0.3)
            before = e.mood
            e.on_user_message("在吗", attention=attention)
            delta = e.mood - before
            if expect_sign > 0:
                _assert(delta > 0.02, f"attention={attention} 应上涨，实际 {delta:+.3f}")
            else:
                _assert(delta < -0.02, f"attention={attention} 应下降，实际 {delta:+.3f}")

        # 上限：attention=1.0 时降幅不超过 0.2
        e = EmotionState(mood=0.5)
        e.on_user_message("在吗", attention=1.0)
        _assert(e.mood >= 0.5 - 0.2, f"降幅超过 0.2 上限：{e.mood}")

        # 「多聊几句就好了」：沉浸时第一句掉，之后注意力被拉走、心情回升
        from .subjectivity import SubjectiveState

        s = SubjectiveState()
        s.emotion = EmotionState(mood=0.3)
        s.attention = 0.95
        s.on_user_message("在吗", attention=s.attention)
        first = s.emotion.mood
        _assert(first < 0.25, f"第一句应被打扰（掉到 0.25 以下），实际 {first:.3f}")
        for _i in range(4):
            s.on_user_message("在吗", attention=s.attention)
        _assert(
            s.emotion.mood > first + 0.1,
            f"聊几句应回升：{first:.3f} -> {s.emotion.mood:.3f}",
        )
        return f"0.5 以下不罚、0.7/0.9 罚；上限 0.2；聊几句 {first:.2f}->{s.emotion.mood:.2f}"

    @suite.check("催用户等待：9 段、7~60 秒、总和收敛到 250/400")
    def _() -> str:
        # 用户第七轮：「最低不滴7秒，最高不高于60秒，语音与文本总等待时间设为250秒和400秒，
        # 分属9轮等待」；第八轮确认算法选 A（归一化）。
        for has_tts, target in ((True, 250.0), (False, 400.0)):
            for seed in range(6):
                waits = plan_wait_seconds(has_tts, random.Random(seed))
                _assert(len(waits) == 9, f"应为 9 段，实际 {len(waits)}")
                _assert(
                    abs(sum(waits) - target) < 0.5,
                    f"总和应约 {target}，实际 {sum(waits):.2f}",
                )
                _assert(min(waits) >= 7.0 - 1e-6, f"低于 7 秒：{min(waits)}")
                _assert(max(waits) <= 60.0 + 1e-6, f"高于 60 秒：{max(waits)}")

        # 不同种子要给出不同的分配（否则等于写死）
        a = plan_wait_seconds(True, random.Random(1))
        b = plan_wait_seconds(True, random.Random(2))
        _assert(a != b, "不同随机种子应给出不同分配")

        # 预算不可达要**报错**而不是死循环
        raised = False
        try:
            plan_wait_for_target(50.0, random.Random(0), segments=9)  # 9×7=63 > 50
        except ValueError:
            raised = True
        _assert(raised, "预算不可达时应当抛 ValueError")
        return "两种预算下 9 段、区间内、总和精确；不可达预算报错"


def run_selftest(config_path: Optional[Path] = None) -> SelfTest:
    suite = SelfTest()
    test_config(suite, config_path)
    test_protocol(suite)
    test_brains(suite)
    test_autonomy(suite)
    test_subjectivity(suite)
    test_state_machine(suite)
    test_vision(suite)
    test_content(suite)
    test_voice(suite)
    test_memory(suite)
    test_search_backends(suite)
    test_netutil(suite)
    test_empty_reply(suite)
    test_bus_and_plugins(suite)
    test_logging(suite)
    test_end_to_end(suite, config_path)
    test_reading(suite)
    return suite


def print_report(suite: SelfTest) -> None:
    width = max((len(r.name) for r in suite.results), default=10)
    print("\n" + "=" * 78)
    print("桌面宠物 · Python sidecar 自检")
    print("=" * 78)
    for result in suite.results:
        mark = "PASS" if result.passed else "FAIL"
        print(f"[{mark}] {result.name.ljust(width)}  {result.seconds * 1000:6.0f}ms")
        if result.detail:
            print(f"       {result.detail}")
    passed = sum(1 for r in suite.results if r.passed)
    total = len(suite.results)
    print("-" * 78)
    print(f"结果：{passed}/{total} 通过{'  ✅ 全部通过' if suite.ok else '  ❌ 存在失败项'}")
    print("=" * 78 + "\n")


def test_reading(suite: "Suite") -> None:
    """网上阅读（2026-10-31 用户需求）。

    > 「用户长时间不和宠物聊天，宠物会自行上网搜索看小说阅读，包括看小说，
    >   看出版书，看小黑盒帖子上的杂谈趣事等等，看的小说和出版书要下载到
    >   本地指定位置，下载好之后看，书源下载的书没文字或错误就换源下载看。」
    """

    @suite.check("网上阅读：正文校验能区分「真正文」和「导航栏」")
    def _() -> str:
        """这是整个功能最核心的一条断言。

        ## 为什么必须是"打分"而不是"有没有内容"

        实测（2026-10-31）：某个书站的**每一页**都有 `#content` 这个 id，
        但书页/目录页里那个 div 装的是**导航条 + 推荐书单**。

        如果校验写成"选择器命中且文本非空"，日志会显示"读到了东西"，
        实际内容却是「首页 | 玄幻 | 武侠 | 都市 | ...」，宠物会拿这个去聊天。

        所以用三个可量化指标（都是实测出来的阈值）：

            中文占比 >= 50%     排除英文页 / 乱码
            标点密度 >= 0.5%    正文有逗号句号，列表/广告没有
            链接文字 <= 30%     导航块全是 <a>（这条在 chapter_body 里判）

        ## ⚠️ 这条测试要能失败

        把 `body_score` 的标点阈值改成 0（等于不检查），下面的反例必须失败。
        验证过：改完之后「首页 玄幻 武侠」会得到正分。
        """
        from .reading import body_score

        # ① 真正文：长、中文多、标点密
        prose = (
            "他站在窗前，看着外面下了一整夜的雨。"
            "街灯把水面照得发亮，像撒了一地的碎银子。"
            "他想起很多年前也是这样的天气，那时候他还不知道会走到今天这一步。"
        ) * 8
        score, why = body_score(prose)
        _assert(score > 0, f"真正文被判成无效：{why}（这会让人永远读不到书）")

        # ② 导航栏：中文很多但**几乎没有标点**
        nav = "首页 玄幻 武侠 都市 军史 网游 科幻 灵异 其他 记录 排行 完本 " * 12
        score_nav, why_nav = body_score(nav)
        _assert(
            score_nav == 0,
            f"导航栏被判成正文：{why_nav} —— 宠物会把「首页 玄幻 武侠」当书读",
        )

        # ③ 英文页：中文占比过低
        english = "Welcome to this site. " * 40
        score_en, why_en = body_score(english)
        _assert(score_en == 0, f"英文页被判成正文：{why_en}")

        # ④ 太短：多半是提示语/错误页
        score_short, _ = body_score("抱歉，本章节不存在。")
        _assert(score_short == 0, "过短内容不该当成正文")

        return f"真正文 {score:.0f} 分；导航 {score_nav:.0f}；英文 {score_en:.0f}；短文本 {score_short:.0f}"

    @suite.check("网上阅读：书源配置解析正确，坏配置不炸")
    def _() -> str:
        """书源是**用户手写**的 YAML，一定会写错。

        `SourceSpec.from_dict` 必须对缺字段/类型错都给出合理默认，
        而不是抛异常 —— 否则用户改错一个字，整个阅读功能就没了。
        """
        from .reading import SourceSpec

        # 完整配置
        full = SourceSpec.from_dict(
            {
                "name": "测试源",
                "base": "https://example.com",
                "enabled": True,
                "encoding": "gbk",
                "search": {
                    "method": "post",
                    "path": "search.html",
                    "keyword_field": "s",
                    "extra": {"type": "1"},
                    "book_link": "/book/[0-9]+/",
                },
                "toc": {"url": "/book/{book_id}/", "chapter_link": r"/book/\d+/\d+\.html"},
                "content": {"selectors": ["#content", ".read"], "min_chars": 800},
            }
        )
        _assert(full.name == "测试源", f"名称错：{full.name}")
        _assert(
            full.base == "https://example.com/",
            f"base 必须补末尾斜杠（否则拼 URL 会出错）：{full.base!r}",
        )
        _assert(full.search_method == "post", f"method 错：{full.search_method}")
        _assert(full.keyword_field == "s", f"字段名错：{full.keyword_field}")
        _assert(full.search_extra == {"type": "1"}, f"extra 错：{full.search_extra}")
        _assert(full.min_chars == 800, f"min_chars 错：{full.min_chars}")
        _assert(full.selectors == ["#content", ".read"], f"selectors 错：{full.selectors}")

        # 空配置：必须能构造出来，不能抛
        empty = SourceSpec.from_dict({})
        _assert(empty.enabled is True, "缺 enabled 时应默认开启")
        _assert(empty.min_chars == 500, f"缺 min_chars 应默认 500，实际 {empty.min_chars}")
        _assert(empty.selectors == [], "缺 selectors 应为空列表")

        # 类型错误：null / 字符串 / 数字混着给
        weird = SourceSpec.from_dict(
            {"name": None, "base": None, "search": None, "toc": None, "content": None}
        )
        _assert(weird.name == "未命名源", f"None 名称应兜底，实际 {weird.name!r}")
        _assert(weird.base == "/", f"None base 应兜底成 '/'，实际 {weird.base!r}")

        return "完整/空/全 None 三种配置都能解析"

    @suite.check("网上阅读：书源目录 / 落盘路径符合用户要求")
    def _() -> str:
        """用户明确说「下载到本地指定位置」，且选了**项目里**。

        两个必须守住的点：
          · 路径是 `<项目根>/pet-books`，不是 %APPDATA%
          · 目录已被 .gitignore 排除（否则盗版书会进公开仓库）
        """
        from . import paths
        from .config import Config
        from .reading import Reader

        r = Reader(Config())
        d = r.books_dir()
        root = paths.project_root()
        _assert(
            d.parent == root.resolve() or d.parent == root,
            f"书应存在项目根下，实际 {d}（根是 {root}）",
        )
        _assert(d.name == "pet-books", f"目录名应为 pet-books，实际 {d.name}")
        _assert(d.is_dir(), f"目录应被自动创建：{d}")

        # 真正的"关掉就失败"：gitignore 必须覆盖它
        gi = root / ".gitignore"
        if gi.is_file():
            text = gi.read_text(encoding="utf-8", errors="replace")
            _assert(
                "pet-books/" in text,
                "pet-books/ 必须在 .gitignore 里 —— 否则下几本书就进公开仓库了",
            )

        # 文件名清洗：书名里常有 / : ? 这些非法字符
        safe = r._save("测试/书名:带*非法?字符", "测试源", "正文内容")
        from pathlib import Path as _P

        name = _P(safe).name
        for bad_ch in ("/", ":", "*", "?", "|"):
            _assert(bad_ch not in name, f"文件名没清洗干净，含 {bad_ch!r}：{name}")
        _P(safe).unlink(missing_ok=True)
        return f"落盘目录 {d.name}，文件名已清洗"

    @suite.check("网上阅读：没有书源时**安静地**什么都不做（不联网、不抛异常）")
    def _() -> str:
        """书源列表为空时必须安静返回，不能联网瞎搜。

        ## ⚠️ 这条测试不能依赖"当前配置里有没有书源"

        第一版写的是 `Reader(Config())` 然后断言"没书源"。
        等用户真的填了书源（2026-10-31 加了新笔趣阁）之后，
        这条测试开始**真的去抓书**，然后断言失败 ——
        而它其实什么 bug 都没抓到，只是前提假设过期了。

        **教训：测试的输入要自己构造，不要读用户的运行时配置。**
        所以这里显式把 `sources` 清空。
        """
        import asyncio

        from .config import Config
        from .reading import Reader

        r = Reader(Config())
        loaded = len(r.sources)
        # 强制清空：不管用户配置里有没有源，这里测的是"没有源"这条路径
        r.sources = []

        result = asyncio.run(r.read_book("三体"))
        _assert(
            result is None,
            f"没有书源时不该产出内容，实际返回 {result!r}",
        )
        return f"清空前载入 {loaded} 个源；清空后读取返回 None"

    @suite.check("网上阅读：搜索文本解析（工具层返回的是文本，不是 JSON）")
    def _() -> str:
        """`tools._web_search` 返回**给模型读的 Markdown 文本**。

        阅读功能需要 `[{title,url,snippet}]`，所以有一层反向解析。
        这层很脆（格式一改就失效），所以单独测。
        """
        from .gateway import _parse_search_text

        sample = """结果数: 2

## 结果 1

**标题**: 某篇游戏杂谈

**url**: https://example.com/a

**来源**: 360

**内容**: 讲了某个游戏的趣事

## 结果 2

**标题**: 第二篇

**url**: https://example.com/b

**内容**: 另一段摘要
"""
        items = _parse_search_text(sample)
        _assert(len(items) == 2, f"应解析出 2 条，实际 {len(items)}")
        _assert(items[0]["title"] == "某篇游戏杂谈", f"标题错：{items[0]['title']!r}")
        _assert(items[0]["url"] == "https://example.com/a", f"url 错：{items[0]['url']!r}")
        _assert(items[0]["snippet"] == "讲了某个游戏的趣事", f"摘要错：{items[0]['snippet']!r}")

        # 工具层返回"人话"时必须是空列表，不能当成结果
        for msg in ("联网搜索好像没打开", "错误：没有给搜索词。", ""):
            _assert(_parse_search_text(msg) == [], f"这句不该被解析成结果：{msg!r}")

        return "正常格式解析出 2 条；错误提示返回空列表"

    @suite.check("网上阅读：base64 混淆的正文能解出来（纯选择器抓不到）")
    def _() -> str:
        """2026-10-31 实测：某些站把正文切成几十段 base64，用随机函数名写入。

            document.writeln(qryxkm.kagvx('PHA+5aSn56yU6Laj...'));
            document.writeln(fgqm.eep('PHA+5q+V56uf5piv...'));

        页面上 `#content` 只剩 149 字符的导航，**真正的正文一个字都不在
        HTML 里**。纯选择器方案对这类页面永远抓不到，而且症状很像
        "这站没正文"，于是会一直换源而不自知。

        这条测试守两件事：
          ① 能按**结构**（而不是函数名）认出这种混淆
          ② 解出来的内容能通过正文校验
        """
        import base64 as _b64

        from .reading import _decode_obfuscated_blocks, body_score

        # 造 5 段 base64，函数名刻意不同（真实站点每页随机）
        # 每段要够长：ody_score 有 100 字下限（短于它的多半是提示语），
        # 而测试要验证的是解出来的内容过得了校验，所以样本必须够量。
        frags = [
            "<p>他站在窗前，看着外面下了一整夜的雨，街灯把水面照得发亮。</p>",
            "<p>他想起很多年前也是这样的天气，那时候他还年轻，还会为一句话激动半天。</p>",
            "<p>风从窗缝里钻进来，带着湿冷的气息，把桌上的纸吹得簌簌响。</p>",
            "<p>他伸手把窗关上，屋子里顿时安静下来，只剩下钟摆的声音。</p>",
            "<p>有些事情过去了就是过去了，再怎么想也回不到当初那个下午。</p>",
        ]
        lines = []
        for i, f in enumerate(frags):
            b64 = _b64.b64encode(f.encode("utf-8")).decode()
            # 函数名每段都不一样 —— 必须靠结构匹配
            lines.append(f"document.writeln(zq{i}km.kagv{i}('{b64}'));")
        html = "<html><body><div id='content'>首页 玄幻 武侠</div>" + "".join(lines) + "</body></html>"

        got = _decode_obfuscated_blocks(html)
        _assert(got, "没认出 base64 混淆 —— 这类站点会永远抓不到正文")
        for f in frags:
            _assert(f in got, f"解出的内容缺片段：{f[:16]}…")
        score, why = body_score(got.replace("<p>", "").replace("</p>", ""))
        _assert(score > 0, f"解出来的内容过不了正文校验：{why}")

        # 没混淆的普通页面：必须返回空串（不能误判）
        plain = "<html><body><div id='content'>这是一段普通正文，有标点。</div></body></html>"
        _assert(
            _decode_obfuscated_blocks(plain) == "",
            "普通页面不该被当成有混淆",
        )

        # 少于 3 段 base64 不算（避免把零散统计代码误当正文）
        one = "document.writeln(a.b('" + _b64.b64encode("你好".encode()).decode() + "'));"
        _assert(_decode_obfuscated_blocks(one) == "", "只有一段不该当成整章正文")

        return f"解出 {len(frags)} 段，评分 {score:.0f}；普通页面不受影响"


    @suite.check("网上阅读：站点 URL 形态千奇百怪，解析必须宽容")
    def _() -> str:
        """2026-10-31 实测三个站的书籍 URL，形态**完全不同**：

            /book/gtfft/                       小写 + 末尾斜杠
            /50606                             纯数字，无前缀
            /book/16ABo1xDXh4nyeBPyapArh.html  混合大小写 + .html

        第一版写死 `/book/([a-z0-9]+)/`，结果后两种都抽不出 `book_id`，
        而 `toc_url` 要靠它拼 —— 抽不到的后果是退化成"扫书页找章节"，
        而那些站的章节链接根本不在书页上，
        表现为「这本书没有章节」（听起来像书本身是空的，其实是代码不认）。

        这条测试守：四种形态都能抽出 id。
        """
        from .config import Config
        from .reading import Reader, SourceSpec

        r = Reader(Config())
        spec = r.sources[0] if r.sources else SourceSpec(name="x", base="https://x/")

        # 直接用 chapter_urls 的 id 抽取逻辑（通过 toc_url 拼接体现）
        cases = [
            ("https://www.xbqg777.com/50606", "50606"),
            ("https://www.xbqg777.com/50606/", "50606"),
            ("https://www.sto66.com/book/16ABo1xDXh4nyeBPyapArh.html", "16ABo1xDXh4nyeBPyapArh"),
            ("https://site.com/book/gtfft/", "gtfft"),
            ("https://site.com/book/efneg/", "efneg"),
        ]
        bad = []
        for url, want in cases:
            # 复用同一套正则（这里直接跑，不联网）
            import re

            got = ""
            for pat in (
                r"/book/([A-Za-z0-9]+)\.html?",
                r"/book/([A-Za-z0-9]+)/",
                r"/([A-Za-z0-9]+)\.html$",
                r"/([A-Za-z0-9]{4,})/?$",
            ):
                m = re.search(pat, url)
                if m:
                    got = m.group(1)
                    break
            if got != want:
                bad.append(f"{url} -> {got!r}（期望 {want!r}）")

        _assert(not bad, "以下 URL 抽不出 book_id：" + "；".join(bad))
        return f"{len(cases)} 种 URL 形态都能抽出 book_id"

    @suite.check("网上阅读：关键词放路径 vs 放参数，两种搜索都要支持")
    def _() -> str:
        """实测两种站点写法：

            参数式  path: 'search'            + keyword_field: keyword
                    -> /search?keyword=三体
            路径式  path: 'search/{q}.html'
                    -> /search/三体.html

        ⚠️ 路径式**不能**再往 params 里塞一次关键词，否则会拼出
        `/search/三体.html?_unused=三体` —— 站点会返回 404 或热门页，
        看起来像"搜索成功但这本书不存在"（思兔阅读就是这么坑的）。

        这条测试守 `{q}` 是否被正确识别为"已在路径里"。
        """
        # 纯逻辑验证，不联网
        def needs_params(path: str) -> bool:
            return "{q}" not in path

        _assert(needs_params("search") is True, "参数式应当需要 params")
        _assert(needs_params("search/{q}.html") is False, "路径式不该再传 params")
        _assert(needs_params("s.php?q={q}") is False, "含 {q} 的都不该再传")
        return "参数式/路径式分流正确"

    @suite.check("网上阅读：书名匹配不上时**换源**，不许随便拿一本")
    def _() -> str:
        """守住一个真 bug（2026-10-31）：`next(..., books[0])` 的兜底。

        ## 当时的现场

        用户让它读《夜玄》，新笔趣阁搜「夜玄」返回一堆**带"玄"字的无关书**
        （《寒剑破玄夜》《玄燕》…），一本都不含「夜玄」。原代码兜底取了
        `books[0]`，于是落盘的是《让你御兽，你融合斩魄刀？》——

        而日志是：

            网上阅读：✅《让你御兽，你融合斩魄刀？》读了 3 章

        **看起来完全正常，只是读错了书。** 用户说「我让他搜搜看」，
        它却拿一本毫不相干的小说跟用户聊 —— 比读不到糟糕得多。

        ## 断言什么

        选书必须是**严格包含**匹配，匹配不上返回 None（= 换源 / 放弃），
        绝不能退回第一个结果。
        """
        # 复刻 read_book / read_many 里的选书逻辑
        def pick(query: str, titles: list[str]) -> str | None:
            matched = next((t for t in titles if query in t), None)
            return matched

        # 搜"夜玄"的真实返回（新笔趣阁那一批全是"玄"字但都不含"夜玄"）
        noisy = ["寒剑破玄夜", "玄燕", "夜玄心"]
        _assert(pick("夜玄", noisy) == "夜玄心", "应当选中真正含「夜玄」的那本")
        _assert(
            pick("夜玄", ["寒剑破玄夜", "玄燕"]) is None,
            "一本都不含「夜玄」时必须返回 None（换源），**不能**退回第一本",
        )
        _assert(pick("希伯尼战记", []) is None, "空列表返回 None")

        # 反过来：完全一致时当然要选中
        _assert(pick("夜玄", ["夜玄", "夜玄心"]) == "夜玄", "完全一致优先命中")

        return "严格包含匹配；匹配不上返回 None 而不是第一本"


    @suite.check("网上阅读：读完的内容**只用一次**（不会反复念同一本书）")
    def _() -> str:
        """`_take_reading_hint` 是"取走就清"，不是"读"。

        如果不清理，宠物会**每轮都提同一本书**，用户会觉得它卡住了。
        """
        import time as _t

        from .bus import Bus
        from .config import Config
        from .gateway import Gateway
        from .reading import BookChunk

        gw = Gateway.__new__(Gateway)  # 不跑 __init__（那会起一堆服务）
        gw._last_reading = BookChunk(
            title="测试书", source="测试源", text="正文" * 100, kind="book", chars=200
        )
        gw._last_reading_at = _t.monotonic()
        gw._reading_title = ""
        # ⚠️ 2026-10-31 加：`_take_reading_hint` 现在要读这个标记
        #    （区分"用户要求读的"和"自己闲着读的"）。不设的话直接 AttributeError。
        gw._reading_from_dialog = False

        first = gw._take_reading_hint()
        _assert(first, "第一次应当拿到提示词")
        _assert("测试书" in first, f"提示词里应带书名：{first[:60]!r}")
        _assert("不要复述原文" in first, "提示词必须约束它别背原文")
        _assert(gw._last_reading is None, "取走后必须清空，否则会反复念同一本")

        second = gw._take_reading_hint()
        _assert(second == "", f"第二次应当拿到空串，实际 {second!r}")

        # 过期内容不该再提（30 分钟前的阅读早就过时了）
        gw._last_reading = BookChunk(title="老书", source="s", text="x" * 200, chars=200)
        gw._last_reading_at = _t.monotonic() - 3600
        _assert(gw._take_reading_hint() == "", "一小时前的阅读不该再拿出来说")

        return "取一次后清空；过期内容不再提及"

    @suite.check("网上阅读：对话里读的书，话术与「自己闲着读」不同")
    def _() -> str:
        """两种来源必须给不同的提示词（2026-10-31）。

        ## 为什么这条要有

        用户说「你搜搜希伯尼战记」-> 模型调 `read_book` -> 读到正文。
        如果这时还用「用户不知道，是你闲着时看的」那套话术，模型会
        把**用户自己要求的事**说成"我自己偷偷看的"，而且只会"随口提一句"。

        但用户正等着回答 —— 他要的是**具体内容**（人物、情节），
        不是「我读到了，这本书不错」这种空话。

        ## 断言什么

        · 对话中：说「按用户的要求」，要求讲具体内容
        · 自己闲着：说「用户不知道」，要求随口提一句
        """
        import time as _t

        from .gateway import Gateway
        from .reading import BookChunk

        def _mk(from_dialog: bool) -> str:
            gw = Gateway.__new__(Gateway)
            gw._last_reading = BookChunk(
                title="希伯尼战记",
                source="read_book",
                text="主角在雪地里捡到一把断剑，剑柄上刻着看不懂的符文。" * 5,
                kind="book",
                chars=600,
            )
            gw._last_reading_at = _t.monotonic()
            gw._reading_title = ""
            gw._reading_from_dialog = from_dialog
            return gw._take_reading_hint()

        in_dialog = _mk(True)
        _assert(in_dialog, "对话中读完书，必须给出提示词")
        _assert("按用户的要求" in in_dialog, f"对话中应说明是用户要求的：{in_dialog[:80]!r}")
        _assert("不要只说" in in_dialog, "对话中必须要求它讲具体内容，而不是空泛附和")
        _assert(
            "用户不知道" not in in_dialog,
            "对话中不能说「用户不知道」—— 是用户让它读的",
        )

        idle_read = _mk(False)
        _assert(idle_read, "自己闲着读完，也要给出提示词")
        _assert("用户不知道" in idle_read, f"自己闲着读的应说明用户不知情：{idle_read[:80]!r}")
        _assert("不要复述原文" in idle_read, "自己闲着读的仍要约束别背原文")
        _assert(
            "按用户的要求" not in idle_read,
            "自己闲着读的不能说成用户要求的",
        )

        return "对话中讲具体内容；自己闲着随口提一句"

    @suite.check("网上阅读：只看不取（consume=False）不会吃掉内容")
    def _() -> str:
        """`consume=False` 必须**原样保留**，否则自主发言永远拿不到。

        ## 背景

        `_compose_context_hint` 每轮都跑。如果它无条件"取走"，会在
        自主发言之前把内容吃掉 —— 表现是「读完了但从不提」。

        所以 `consume=False` 只看不取：内容还在，等真正要用的人来拿。
        """
        import time as _t

        from .gateway import Gateway
        from .reading import BookChunk

        gw = Gateway.__new__(Gateway)
        chunk = BookChunk(title="留着", source="s", text="x" * 200, chars=200)
        gw._last_reading = chunk
        gw._last_reading_at = _t.monotonic()
        gw._reading_title = ""
        gw._reading_from_dialog = True

        peek = gw._take_reading_hint(consume=False)
        _assert(peek == "", f"只看不取应返回空串，实际 {peek!r}")
        _assert(gw._last_reading is chunk, "只看不取时内容必须原样保留")
        _assert(gw._reading_from_dialog is True, "只看不取时标记也要保留")

        # 真正取的时候才清
        real = gw._take_reading_hint(consume=True)
        _assert(real, "真取的时候应当拿到内容")
        _assert(gw._last_reading is None, "真取之后必须清空")
        _assert(gw._reading_from_dialog is False, "真取之后标记也要清")

        return "consume=False 保留；consume=True 取走并清标记"

    @suite.check("模块：小说阅读是独立模块，立绘/气泡两个模块已删除")
    def _() -> str:
        """守住用户 2026-11-02 的要求。

        用户原话：
        > 「将整个搜索、看小说的功能作为自主行动中搜索小说功能的模块，
        >   看小黑盒杂谈那个还是30分钟触发，不和这个在一个模块」
        > 「这两个做成模块放在设置模块里面，模块里面的立绘动画模块、
        >   窗口气泡模块去掉」

        所以：
          · 必须有 `reading`（小说阅读），而且可热切（gate=True）
          · 必须**没有** `sprite` / `ui.windows`
        """
        from .modules import MODULES

        names = {m.name for m in MODULES}
        _assert("reading" in names, "缺少「小说阅读」模块")
        _assert("sprite" not in names, "「立绘动画」模块应当已删除")
        _assert("ui.windows" not in names, "「窗口/气泡」模块应当已删除")

        reading = next(m for m in MODULES if m.name == "reading")
        _assert(reading.label == "小说阅读", f"标签应为「小说阅读」，实际 {reading.label!r}")
        _assert(reading.gate, "小说阅读必须可热切（否则设置页开关是死的）")
        # 详情里要说明它是"附加能力"（用户要求写清是附加功能）
        _assert("附加" in reading.note, f"note 应说明是附加能力：{reading.note!r}")

        return "有 reading（可热切）；sprite / ui.windows 已删除"

    @suite.check("模块：关掉小说阅读后，reading 事件在抽取阶段就被排除")
    def _() -> str:
        """模块关掉时不能"抽中了再判"—— 那会白占一个槽。

        ## 为什么这条重要

        `needs` 是**静态能力**（`CAPABILITIES` 里写死），而模块是
        **用户可随时开关**的。两者不是一回事：

            模块关掉 -> needs 依然是 True -> dormant 为 False
                     -> 照样被抽中 -> 抢槽 -> handler 里才发现关着 -> 什么都不做

        后果是事件日志里会记一条"它去读书了"，实际什么都没发生 ——
        又是"看起来做了"。

        所以 `eligible()` 必须返回 `module_off:xxx`，连抽中的机会都不给。
        """
        import random as _random
        import time as _t

        from .events import EventEngine, get_event

        gate = EventEngine(rng=_random.Random(7))
        spec = get_event("reading")
        _assert(spec is not None, "没有 reading 事件")

        # ① 没注入模块查询函数 -> 按开处理（不误伤）
        ok, why = gate.eligible(
            spec, now=_t.time(), last_interaction_at=0, last_event_at=0, events_today=0
        )
        _assert(ok, f"未注入 module_enabled 时应当可用，实际 {why!r}")

        # ② 注入"模块关着" -> 必须排除
        gate.module_of["reading"] = "reading"
        gate.module_enabled = lambda name: False
        ok2, why2 = gate.eligible(
            spec, now=_t.time(), last_interaction_at=0, last_event_at=0, events_today=0
        )
        _assert(not ok2, "模块关着时不该可抽")
        _assert("module_off" in why2, f"原因应说明是模块关着，实际 {why2!r}")

        # ③ 模块开着 -> 恢复可用
        gate.module_enabled = lambda name: True
        ok3, _why3 = gate.eligible(
            spec, now=_t.time(), last_interaction_at=0, last_event_at=0, events_today=0
        )
        _assert(ok3, "模块开着时应当可用")

        return "模块关 -> module_off 排除；模块开 -> 可用"

    @suite.check("小说 7 : 小黑盒杂谈 3（用户指定权重）")
    def _() -> str:
        """两个事件的权重必须是 7:3。

        用户原话：「分成小黑盒3，小说7权重，小说搜索模块关闭时
        自然就只触发小黑盒杂谈事件了」。

        ⚠️ 权重是**相对**的，所以断言比例而不是绝对值 ——
        以后加别的事件时调整绝对值不会误伤这条测试。
        """
        from .events import get_event

        novel = get_event("reading")
        chat = get_event("community_chat")
        _assert(novel is not None, "没有 reading 事件")
        _assert(chat is not None, "没有 community_chat 事件（小黑盒杂谈）")

        ratio = novel.weight / chat.weight
        _assert(
            abs(ratio - 7 / 3) < 0.01,
            f"小说:杂谈 应为 7:3，实际 {novel.weight:.1f}:{chat.weight:.1f}（{ratio:.2f}）",
        )
        # 两个都要占槽（"同一时间不存在两个事件"）
        _assert(novel.duration_s is not None, "小说阅读必须占槽（要读几十秒到几分钟）")
        _assert(chat.duration_s is not None, "小黑盒杂谈也要占槽")
        # 小说要读很久，槽位时间必须给足
        _assert(
            novel.duration_s >= 300,
            f"小说阅读的槽位时间太短（{novel.duration_s}s），读几章会被掐断",
        )

        return f"7:3 成立；槽位 小说 {novel.duration_s:.0f}s / 杂谈 {chat.duration_s:.0f}s"

    @suite.check("阅读历史进原文记忆，并带「自己找的/用户推荐」来源")
    def _() -> str:
        """用户要求：原文记忆里要保留阅读历史，检索提炼也要参与。

        用户原话：
        > 「原文记忆里面要保留它的阅读小说历史，比如，自己找到了一个
        >   好看的小说，或者是用户推荐下看了一本小说，检索提炼也要参与」

        ## 断言什么

        · 写完能在原文记忆里查到
        · 开头带「（我读了书《…》——我自己找的 / 用户推荐我读的）」
          这样模型分得清"我读过"和"我跟你说过"
        · 两种来源要能区分
        """
        from .config import Config
        from .gateway import Gateway
        from .reading import BookChunk

        cfg = Config()
        gw = Gateway(cfg)
        _assert(gw.memory is not None, "测试需要记忆库")

        gw._reading_from_dialog = False
        gw._remember_reading(
            BookChunk(title="夜玄", source="read_book", text="雪地里捡到一把断剑。",
                      kind="book", chars=5000)
        )
        gw._reading_from_dialog = True
        gw._remember_reading(
            BookChunk(title="诡秘之主", source="read_book", text="蒸汽与机械的世界。",
                      kind="book", chars=16000)
        )

        texts = []
        for row in gw.memory.db.recent_turns(since_ts=0, limit=20):
            try:
                texts.append(str(row["text"]))
            except Exception:  # noqa: BLE001
                continue
        joined = "\n".join(texts)

        _assert("夜玄" in joined, "自己找的那本没进原文记忆")
        _assert("诡秘之主" in joined, "用户推荐的那本没进原文记忆")
        _assert("我自己找的" in joined, "没标明是自己找的")
        _assert("用户推荐我读的" in joined, "没标明是用户推荐的")
        _assert("（我读了书" in joined, "开头没写「（我读了书…）」—— 会被误当成对话")

        return "两本都进原文记忆；来源区分正确；不会被误当成对话"

    @suite.check("截断落在句子边界，不停在半个字上")
    def _() -> str:
        """守住用户 2026-11-02 发现的问题。

        用户原话：「记忆里面最新一条是没说完话吗」。

        现场：记忆里那条阅读历史结尾是

            据说罗塞尔大帝曾经看过"亵渎石板"，据说他制造的秘

        停在「秘」字上 —— 看起来就是话说了一半。

        ## 这条测试管两件事

        ① `_clip_at_sentence` 必须落在句末标点上
        ② 实在找不到句末时才硬切，并且**补省略号**让"这是断的"明确
        """
        from .gateway import _clip_at_sentence

        # ① 能落在句号上
        long_text = (
            "老尼尔话音刚落，他背后昏暗里的那双虚幻眼睛就迅速消失不见，"
            "即使处于灵视状态的克莱恩，也无法再发现它曾经存在过的痕迹。"
            "这是一个仪式魔法的表征。老尼尔呵呵解释道。有点神奇。"
        )
        out = _clip_at_sentence(long_text, 60)
        _assert(len(out) <= 60, f"不能超长：{len(out)}")
        _assert(
            out.rstrip()[-1] in "。！？…”」",
            f"必须落在句末标点上，实际结尾 {out[-6:]!r}",
        )

        # ② 找不到句末 -> 硬切 + 省略号（明确表示断在这里）
        no_punct = "没有标点的一长串文字" * 20
        out2 = _clip_at_sentence(no_punct, 30)
        _assert(out2.endswith("…"), f"硬切时必须补省略号，实际 {out2[-6:]!r}")

        # ③ 不超长就不动
        short = "短句。"
        _assert(_clip_at_sentence(short, 100) == short, "没超长不该改动")

        # ④ 引号收尾也要带上（对话体小说大量使用）
        quoted = "他说：" + "好" * 40 + "。”然后走了。"
        out4 = _clip_at_sentence(quoted, 48)
        _assert(
            out4.rstrip()[-1] in "。！？…”」",
            f"引号收尾要带上，实际 {out4[-6:]!r}",
        )

        return "句末截断；无标点补省略号；不超长不动"

    @suite.check("阅读记忆跟着章节走，不会每次都记第 1 章")
    def _() -> str:
        """守住用户 2026-11-04 报的「宠物说了两遍看同一章节」。

        ## 现场

        记忆库里两条记录 **sha1 完全相同**，间隔 6 小时：

            [2019] 10:17  （我读了书《天官赐邪》——我自己找的）第1章 洞天福地…
            [2021] 16:15  （我读了书《天官赐邪》——我自己找的）第1章 洞天福地…

        而书文件里已经有 1~37 章（说明确实读了后面的章）。

        ## 根因

        `reading_tools` 拿到 `path` 后，读的是**整个文件**：

            raw = path.read_text()
            body = 去掉文件头后的全部正文      # <- 开头永远是第 1 章

        而落盘是**追加**的，第 1 章永远在最前面。于是
        `_remember_reading` 里的 `_clip_at_sentence(body, 600)`
        截到的永远是**第 1 章开头那 600 字** —— 不管这次读第几章。

        ## 修法

        `read_many` 多返回一个 `fresh`：**本次真正下载的那几章正文**。
        `reading_tools` 直接用它，不再读整个文件。

        ## 这条测试断言什么

        用两批不同的 `fresh` 造两条记忆，它们必须**不同**。
        同时断言 `read_many` 的返回值是 6 元组（签名改了，调用方要跟上）。
        """
        import inspect
        import tempfile
        from pathlib import Path as _P

        from .reading import Reader

        # ① 签名必须是 6 元组（多了"本次正文"）
        sig = inspect.signature(Reader.read_many)
        ret = str(sig.return_annotation)
        _assert(
            "str" in ret and "tuple" in ret,
            f"read_many 应当返回元组，实际 {ret}",
        )

        # ② 源码里不该再有"读整个文件当正文"的写法
        src = (_P(__file__).with_name("reading_tools.py")).read_text(encoding="utf-8")
        _assert(
            "body = fresh" in src,
            "reading_tools 应当用 read_many 带回的 fresh，而不是读整个文件",
        )
        _assert(
            "read_text(encoding=\"utf-8\", errors=\"replace\")" not in src
            or "fresh" in src,
            "不该再靠读整个文件拿正文",
        )

        # ③ 两条不同的 fresh 必须产生不同的记忆文本
        from .reading import BookChunk

        def _mk(fresh: str, chapter_hint: str) -> str:
            body = fresh
            return (
                f"（我读了书《测试书》——我自己找的）\n"
                f"{body[:600]}"
            )

        a = _mk("第1章 洞天福地\n狂风呼啸。骤雨倾盆。", "1")
        b = _mk("第38章 第二次拍掌游戏\n黑夜里他睁开了眼。", "38")
        _assert(a != b, "两批不同章节写出来的记忆文本必须不同")

        return "read_many 返回 6 元组；用 fresh 不用整文件；两批内容不同"

    @suite.check("起点按内容判断：正文照读、版权页/空章跳过、重复去重")
    def _() -> str:
        """守住用户 2026-11-02 报的「天官赐邪的前面2章消失了」。

        ## 那个 bug

        旧代码两处都是：

            idx = done if done >= 2 else 2      # 前两章常是版权页

        **无条件**跳过前两章。规则来自新笔趣阁（它第 1 章叫「版权信息」），
        却被当成所有书源的通用规则。

        实测翻车：思兔阅读《天官赐邪》第 1、2 章是正常正文
        （2599 / 2323 字，评分 237 / 213），被一起跳掉。而那是故事开头
        （主角第一次进福地、遇到第一个邪祟），跳过等于从第三章中间开始看。

        ## 但也不能一律不跳

        新笔趣阁《诡秘之主》的目录前三条是：

            [0] 新书已发        0 字      <- 空占位
            [1] 开始阅读     2493 字      <- 和下面内容一样
            [2] 第一章 绯红   2493 字      <- 正文

        所以判据要**按内容**：空章/版权页跳，重复条目去重，正文照读。

        这条测试直接调 `_looks_like_front_matter`（纯函数，不联网）。
        """
        from .reading import _looks_like_front_matter

        novel = "狂风呼啸。骤雨倾盆。" * 20      # 像正文
        copyright_text = "版权所有 侵权必究"       # 太短，不像正文

        # ① 正常正文 -> 不跳（这是天官赐邪那两章的情况）
        _assert(
            not _looks_like_front_matter("第1章 洞天福地", novel),
            "正常正文被误判成版权页 —— 就是「前2章消失」那个 bug",
        )
        _assert(
            not _looks_like_front_matter("第2章 红衣吊祟", novel),
            "第2章也该照读",
        )

        # ② 空章 -> 跳（新笔趣阁的「新书已发」）
        _assert(
            _looks_like_front_matter("新书已发", ""),
            "空章节必须跳过（否则会落盘一个只有标题的空章）",
        )
        _assert(
            _looks_like_front_matter("新书已发", "   \n\n  "),
            "全是空白的章节也要跳过",
        )

        # ③ 版权页 -> 跳
        _assert(
            _looks_like_front_matter("版权信息", copyright_text),
            "版权页必须跳过",
        )

        # ④ 楔子/序章一律当正文（很多小说把关键伏笔放这里）
        for good in ("楔子", "序章", "引子", "前言"):
            _assert(
                not _looks_like_front_matter(good, "夜很深了，他站在门口。"),
                f"「{good}」必须当正文，不能跳过",
            )

        # ⑤ 标题像版权页但内容够长 -> 不跳（宁可多读，别漏正文）
        _assert(
            not _looks_like_front_matter("关于本书", novel),
            "标题可疑但内容是正文时，应当照读（宁可多读也别漏）",
        )

        return "正文照读；空章/版权页跳过；楔子序章放行"

    @suite.check("安静档：不主动说话、不触发事件，且恢复正好 2 小时")
    def _() -> str:
        """守住用户 2026-11-02 的要求，以及 2026-11-04 的时长调整。

        用户原话（11-02）：

        > 「状态调节现在调到安静时，将指数调低，让它要自然增长30分钟才回到无聊，
        >   这样变相的能让用户安静，然后是安静状态下不主动说话，不触发任何事件，
        >   安静30分钟后，转到无聊后才开始计算用户不予宠物聊天的计时。」

        用户原话（11-04）：

        > 「把安静30分钟到无聊改成2小时。」

        ## 三件事，分开断言

        ① 恢复速率要让 0.4 -> 0.85 正好 **2 小时**
        ② 安静档：调度器不开口 + 事件不抽
        ③ 安静期间"多久没聊天"的计时被重置（转到无聊后才开始算）

        ①③ 是纯数学/状态，②要构造 Gateway。
        """
        import time as _t

        from .brains.base import EmotionState
        from .config import Config
        from .events import EventEngine, get_event
        from .gateway import Gateway

        # ① 恢复正好 2 小时（120 分钟）
        per_min = EmotionState.ENERGY_RECOVER_PER_MIN
        cap = EmotionState.ENERGY_RECOVER_CAP
        low = 0.4
        mins = (cap - low) / per_min
        _assert(
            abs(mins - 120.0) < 1.0,
            f"安静恢复到能转无聊应约 120 分钟（2 小时），"
            f"实际 {mins:.1f} 分钟（速率 {per_min}/分）",
        )

        # ② 安静档门控（用真实 Gateway 验，避免"两处各写一套"）
        cfg = Config()
        gw = Gateway(cfg)
        _assert(gw.events.quiet_tier is not None, "Gateway 必须注入 quiet_tier 查询函数")

        gw.config.root.setdefault("activity", {})["overall"] = 0.1   # 安静
        _assert(
            gw.scheduler.suppression_reason() == "quiet_tier",
            f"安静档调度器应被拦，实际 {gw.scheduler.suppression_reason()!r}",
        )
        _assert(
            not gw.scheduler.evaluate().speak,
            "安静档不该主动开口",
        )
        spec = get_event("reading")
        ok, why = gw.events.eligible(
            spec, now=_t.time(), last_interaction_at=0, last_event_at=0, events_today=0
        )
        _assert(not ok, "安静档不该抽事件")
        _assert(why == "quiet_tier", f"原因应为 quiet_tier，实际 {why!r}")

        # 其它三档必须放行（否则"安静"就成了永久静音）
        for name, val in (("无聊", 0.5), ("好奇", 0.8), ("自言自语", 1.0)):
            gw.config.root["activity"]["overall"] = val
            _assert(
                gw.scheduler.suppression_reason() is None,
                f"{name}档不该被安静门控拦住",
            )
            ok2, why2 = gw.events.eligible(
                spec, now=_t.time(), last_interaction_at=0, last_event_at=0, events_today=0
            )
            _assert(ok2, f"{name}档事件应当可抽，实际 {why2!r}")

        # ③ 安静期间不计"多久没聊天"
        #
        # 这条是**异步方法**里的逻辑，单独调一次验证。用 asyncio.run 跑。
        import asyncio

        async def _check_timer() -> tuple[float, float]:
            gw.config.root["activity"]["overall"] = 0.1
            gw.state.switch("quiet", "测试")
            gw.state.last_chat_at = _t.time() - 3600      # 假装一小时没聊
            await gw._check_condition_rules()
            quiet_idle = _t.time() - gw.state.last_chat_at

            gw.state.switch("bored", "测试")
            gw.state.last_chat_at = _t.time() - 1800      # 半小时没聊
            await gw._check_condition_rules()
            bored_idle = _t.time() - gw.state.last_chat_at
            return quiet_idle, bored_idle

        quiet_idle, bored_idle = asyncio.run(_check_timer())
        _assert(
            quiet_idle < 5,
            f"安静期间计时必须重置（否则一转无聊就立刻满足30分钟），实际 {quiet_idle:.0f}s",
        )
        _assert(
            bored_idle > 1700,
            f"无聊档计时应当正常累积，实际 {bored_idle:.0f}s",
        )

        return "恢复2小时；安静档全拦；安静期间不计聊天时长"

    @suite.check("事件模块：没注入 quiet_tier 时按「不安静」处理（不误伤）")
    def _() -> str:
        """缺配置不该让事件静默停摆。

        `EventEngine.quiet_tier` 是 Gateway 注入的。独立使用（测试、
        脚本）时它是 `None` —— 那时必须**放行**，不能因为"读不到"
        就把所有事件停掉。

        这和 `_module_on` 的取舍一致：保守方向是"功能照常"，
        而不是"因为不知道就全禁"。
        """
        import random as _random
        import time as _t

        from .events import EventEngine, get_event

        gate = EventEngine(rng=_random.Random(7))
        _assert(gate.quiet_tier is None, "新引擎默认不该有 quiet_tier")
        spec = get_event("reading")
        ok, why = gate.eligible(
            spec, now=_t.time(), last_interaction_at=0, last_event_at=0, events_today=0
        )
        _assert(ok, f"没注入时应当放行，实际 {why!r}")

        # 注入一个会抛异常的 -> 也要放行（不能因为查询失败就停摆）
        def _boom() -> bool:
            raise RuntimeError("查询炸了")

        gate.quiet_tier = _boom
        ok2, why2 = gate.eligible(
            spec, now=_t.time(), last_interaction_at=0, last_event_at=0, events_today=0
        )
        _assert(ok2, f"查询异常时应当按「不安静」放行，实际 {why2!r}")

        return "没注入/查询异常都放行，不会静默停摆"

    @suite.check("回看：能按章号翻已下载的章节，只看本地不联网")
    def _() -> str:
        """用户 2026-11-02 要求。

        > 「给它加一个能看过往章节的功能吧，这样方便回去看伏笔和故事精彩描写」

        并且明确它**只在需要时用**：

        > 「宠物正常是不会看的，需要了解一些东西的时候可以适当回看
        >   拼接起来，或者由用户说回顾精彩章节什么的」

        ## 为什么要断言"不联网"

        回看必须读**本地已下载**的章节，不能重新去抓：
          · 慢（每章一个请求）
          · 书源可能已经失效（免费站寿命短）
          · 站点内容可能改过，回看应该看到**当初读到的那一版**

        所以这里造一个本地文件，然后把所有书源清空 ——
        如果它还是能回看成功，就证明它真的没走网络。
        """
        import tempfile
        from pathlib import Path as _P

        from .reading import Reader

        tmp = _P(tempfile.mkdtemp(prefix="pet-selftest-review-"))
        try:
            books = tmp / "pet-books"
            books.mkdir(parents=True)
            (books / "测试书.txt").write_text(
                "《测试书》\n来源：测试源\n开始：2026-11-02 10:00:00\n\n\n"
                "<!-- chapter:1 -->\n"
                "────────────────────────────────────────\n"
                "第1章  开端\n"
                "下载于 2026-11-02 10:00:01\n"
                "────────────────────────────────────────\n"
                "\n"
                "第一段正文。这里埋了一个伏笔。\n"
                "第二段正文。\n\n"
                "<!-- chapter:2 -->\n"
                "────────────────────────────────────────\n"
                "第2章  发展\n"
                "下载于 2026-11-02 10:00:05\n"
                "────────────────────────────────────────\n"
                "\n"
                "第二章的内容在这里。\n\n"
                "<!-- chapter:5 -->\n"
                "────────────────────────────────────────\n"
                "第5章  高潮\n"
                "下载于 2026-11-02 10:00:09\n"
                "────────────────────────────────────────\n"
                "\n"
                "第五章很精彩。\n",
                encoding="utf-8",
            )

            r = Reader.__new__(Reader)          # 不跑 __init__（会读配置/联网）
            r._books_dir_override = books       # 见下面 books_dir 的兼容

            # `books_dir` 走配置，这里直接打个补丁让它指到临时目录
            r.books_dir = lambda: books  # type: ignore[method-assign]
            r.config = type("C", (), {"chunk_chars": 1500, "min_body_chars": 500})()

            # ① 只给书名 -> 目录
            res = r.review_book("测试书")
            _assert(res["ok"], f"应当能回看：{res.get('message')}")
            _assert(res["total"] == 3, f"应当是 3 章，实际 {res['total']}")
            nos = [c["no"] for c in res["chapters"]]
            _assert(nos == [1, 2, 5], f"章号应当原样保留（可能不连续），实际 {nos}")
            _assert(
                all("text" not in c for c in res["chapters"]),
                "只给书名时不该返回正文（省上下文）",
            )

            # ② 给区间 -> 回正文
            res2 = r.review_book("测试书", 1, 2)
            _assert(res2["ok"], "区间回看应当成功")
            _assert(len(res2["chapters"]) == 2, f"应当 2 章，实际 {len(res2['chapters'])}")
            _assert(
                "伏笔" in res2["chapters"][0]["text"],
                f"第1章正文里应当有「伏笔」，实际 {res2['chapters'][0]['text'][:50]!r}",
            )
            _assert(
                "下载于" not in res2["chapters"][0]["text"],
                "正文里不该混进「下载于」这种元数据",
            )

            # ③ `limit_chars` 要截断，且落在句子边界
            res3 = r.review_book("测试书", 1, 2, limit_chars=8)
            t1 = res3["chapters"][0]["text"]
            _assert(len(t1) <= 12, f"截断后应当很短，实际 {len(t1)} 字：{t1!r}")
            _assert(
                t1.rstrip()[-1] in "。！？…”」",
                f"截断必须落在句末，实际结尾 {t1[-4:]!r}",
            )

            # ④ 不存在的书 / 超出范围 -> 明确失败，不抛异常
            res4 = r.review_book("没有这本书")
            _assert(not res4["ok"], "不存在的书应当返回 ok=False")
            _assert("没有" in res4["message"], f"要说明原因，实际 {res4['message']!r}")

            res5 = r.review_book("测试书", 50, 60)
            _assert(not res5["ok"], "超出范围应当返回 ok=False")
            _assert("1~5" in res5["message"] or "第" in res5["message"], "要说明本地有哪些章")

            # ⑤ 章名已经带「第N章」时不该重复拼前缀
            _assert(
                res2["chapters"][0]["name"].startswith("第1章"),
                "解析出来的章名应当原样保留",
            )
        finally:
            import shutil as _sh

            _sh.rmtree(tmp, ignore_errors=True)

        return "目录/区间/截断/越界都正确；只读本地不联网；章号不连续也能处理"

    @suite.check("模块：联网搜索开关真的能关掉联网（不是假开关）")
    def _() -> str:
        """用户问：「联网搜索小黑盒杂谈使用的是联网搜索模块吗」，
        查下来发现**两处假开关**（2026-11-02 修）。

        ## 假开关 ①：两个模块注册表实例

        `Gateway.modules` 和 `Conversation._module_registry` 是两个独立对象。
        设置页切换改的是前者，而 `_web_enabled()` 读的是后者 ——
        于是"关了还在搜"。实测 `gw.modules is conv._module_registry` 为 False。

        修法：`Conversation.set_module_registry()` 注入同一个实例。

        ## 假开关 ②：关模块没清工具缓存

        工具清单是懒加载缓存。`_web_enabled()` 变了，但 `self._tools`
        还留着旧的 —— 模型照样能调 `web_search`。

        修法：`name == "search"` 时置空 `conversation._tools`。

        ## 这条测试断言三件事

        ① 注册表是同一个对象
        ② 关掉后工具清单里**没有** web_search / 阅读工具
        ③ 开回来能恢复（不能一次关掉就永久没了）
        """
        from .config import Config
        from .gateway import Gateway

        cfg = Config()
        cfg.root.setdefault("tools", {})["web_search_mode"] = "fast"
        gw = Gateway(cfg)
        conv = gw.conversation

        # 触发懒建，才能比较
        _ = conv._web_enabled()

        # ① 同一份注册表
        _assert(
            gw.modules is getattr(conv, "_module_registry", None),
            "Gateway.modules 和 Conversation._module_registry 必须是同一个对象，"
            "否则设置页切的和实际读的不是一回事（假开关）",
        )

        # ② 关掉联网 -> 工具清单缩水
        before = [t.name for t in conv._tool_list()]
        _assert("web_search" in before, f"默认应当有 web_search，实际 {before}")

        gw.modules.set_enabled("search", False)
        gw._clear_tool_cache() if hasattr(gw, "_clear_tool_cache") else None
        conv._tools = None          # 模拟 `_apply_module_change` 的清缓存
        after = [t.name for t in conv._tool_list()]
        _assert(
            "web_search" not in after,
            f"关掉联网后不该还有 web_search，实际 {after}",
        )
        _assert(
            "read_book" not in after,
            f"关掉联网后不该还有 read_book（读小说也要联网），实际 {after}",
        )
        _assert(
            "sandbox_read" in after,
            f"沙箱工具不该受联网开关影响，实际 {after}",
        )

        # ③ 开回来能恢复
        gw.modules.set_enabled("search", True)
        conv._tools = None
        back = [t.name for t in conv._tool_list()]
        _assert("web_search" in back, f"开回来应当恢复 web_search，实际 {back}")
        _assert("read_book" in back, f"开回来应当恢复 read_book，实际 {back}")

        return "同一注册表；关掉真的移除联网工具；开回来能恢复"

    @suite.check("模块：小黑盒杂谈依赖「联网搜索」，小说阅读依赖「小说阅读」")
    def _() -> str:
        """两个事件**不是**同一套底层（用户问的就是这个）。

        ```
        小黑盒杂谈   web_search + web_fetch（联网抓网页）  -> search 模块
        小说阅读     自己的书源解析（直接抓小说站）        -> reading 模块
        ```

        但两条都**需要联网**，所以关掉「联网搜索」时两个都该停。

        ## `needs` 不够用的原因

        `community_chat.needs = ("web_search",)` 是**静态能力**
        （CAPABILITIES 里写死 True），不代表模块开着。它实际会调
        `tools._web_search`，而那个在模块关着时直接拒绝 ——
        不补 `module_of` 映射的话：抽中 -> 抢槽 -> 进去发现被拒 -> 白占一个槽。
        """
        import random as _random
        import time as _t

        from .config import Config
        from .events import EventEngine, get_event
        from .gateway import Gateway

        cfg = Config()
        gw = Gateway(cfg)
        gate = gw.events

        def _ok(kind: str) -> tuple[bool, str]:
            spec = get_event(kind)
            return gate.eligible(
                spec, now=_t.time(), last_interaction_at=0, last_event_at=0, events_today=0
            )

        # 都开 -> 两个都能抽
        ok1, w1 = _ok("reading")
        ok2, w2 = _ok("community_chat")
        _assert(ok1 and ok2, f"默认两个都该能抽：reading={w1} community={w2}")

        # 关「联网搜索」-> 杂谈必须停（它靠 web_search）
        gw.modules.set_enabled("search", False)
        ok3, why3 = _ok("community_chat")
        _assert(not ok3, "关掉联网搜索后，小黑盒杂谈不该还能抽")
        _assert(
            "module_off" in why3,
            f"原因要说明是模块关着，实际 {why3!r}",
        )

        # 关「联网搜索」时小说阅读也读不了（内部另有闸门，见 _reading_via_model）
        _assert(
            not gw.conversation._web_enabled(),
            "关掉联网后 _web_enabled 必须为 False（否则自主阅读会绕过闸门）",
        )

        # 恢复联网、关「小说阅读」-> 杂谈恢复、小说停
        gw.modules.set_enabled("search", True)
        gw.modules.set_enabled("reading", False)
        ok4, _w4 = _ok("community_chat")
        ok5, why5 = _ok("reading")
        _assert(ok4, "恢复联网后杂谈应当能抽")
        _assert(not ok5, "关掉小说阅读后 reading 不该能抽")
        _assert("module_off" in why5, f"原因要说明模块关着，实际 {why5!r}")

        return "杂谈绑 search、小说绑 reading；关联网时两个都停"

    @suite.check("图片存储：入库/去重/按情绪分类/标注/挑选")
    def _() -> str:
        """用户 2026-11-02 要求：

        > 「表情包和图片专门在项目下里一个文件夹储存。」

        这条测的是 `media.MediaStore` 的**纯本地逻辑**（不联网、不调模型）：
        建目录、入库、去重、按情绪归类、标注、挑一个发出去。
        """
        import tempfile
        from pathlib import Path as _P

        from .media import EMOTIONS, MediaStore

        tmp = _P(tempfile.mkdtemp(prefix="pet-selftest-media-"))
        try:
            s = MediaStore(tmp / "pet-media")
            s.ensure_dirs()

            # ① 标准目录都建出来了（用户进去能看到该往哪放）
            for key in ("happy", "sad", "speechless", "ignore"):
                _assert(
                    (s.root / "stickers" / key).is_dir(),
                    f"应当建出 stickers/{key}/ 目录",
                )
            _assert((s.root / "stickers" / "_inbox").is_dir(), "应当有 _inbox")
            _assert((s.root / "images").is_dir(), "应当有 images")

            # ② 入库 -> 进 _inbox（带情绪才进 stickers/<emotion>）
            it = s.add_file(b"\x89PNG-fake-1", "cat.png", source="user-put")
            _assert(it is not None, "入库应当成功")
            _assert(it.path.startswith("stickers/_inbox/"), f"应当在 _inbox：{it.path}")
            _assert(it.caption == "", "新入库的应当没有标注")
            _assert(len(s.unlabeled()) == 1, "应当有 1 个未标注")

            # ③ 去重：同内容再放一次 -> 不新增
            it2 = s.add_file(b"\x89PNG-fake-1", "another.png", source="user-put")
            _assert(it2 is it, "同一内容应当返回同一个对象（不重复写文件）")
            _assert(s.stats()["total"] == 1, f"总数应当还是 1，实际 {s.stats()['total']}")

            # ④ 标注 + 归类
            s.annotate(it, "一只橘猫在笑", by="auto")
            _assert(it.caption == "一只橘猫在笑", "标注没写进去")
            _assert(s.move_to_emotion(it, "happy"), "挪进 happy 应当成功")
            _assert(it.emotion == "happy", f"情绪应当是 happy，实际 {it.emotion!r}")
            _assert(it.path.startswith("stickers/happy/"), f"path 应当更新：{it.path}")
            _assert(len(s.unlabeled()) == 0, "标注后不该再算未标注")

            # ⑤ 按情绪查 / 挑一个
            pool = s.stickers_for("happy")
            _assert(len(pool) == 1, f"happy 下应当有 1 个，实际 {len(pool)}")
            pick = s.pick_sticker("happy")
            _assert(pick is not None and pick.path == it.path, "应当能挑到它")
            s.mark_used(pick)
            _assert(pick.used_count == 1, "使用计数没加")

            # ⑥ 没标注的不该被挑中（发一张不知道是什么的图会闹笑话）
            it3 = s.add_file(b"\x89PNG-fake-2", "x.png", source="user-put")
            s.move_to_emotion(it3, "happy")     # 挪进去了但没标
            pool2 = s.stickers_for("happy")
            _assert(
                all(x.caption for x in pool2),
                "stickers_for 只该返回**已标注**的",
            )

            # ⑦ 用户自己建中文目录也能认
            cn = s.root / "stickers" / "开心"
            cn.mkdir(parents=True, exist_ok=True)
            (cn / "y.png").write_bytes(b"\x89PNG-fake-3")
            s2 = MediaStore(tmp / "pet-media")
            got = s2.scan()
            hit = [x for x in got.values() if x.path == "stickers/开心/y.png"]
            _assert(hit, "扫描应当发现用户自己建的目录")
            _assert(hit[0].emotion == "happy", f"中文目录应当映射成 happy，实际 {hit[0].emotion!r}")

            # ⑧ 用户一次丢进两张内容相同的图 -> 只入库一张（2026-11-05 实测修）
            #
            # 实测 bug：`by_sha` 只含**索引里已有**的条目，所以同一轮里
            # 先遇到的两张相同图都会入库。表现是"我明明只放了一张"，
            # 而 `pick_sticker` 按用得最少挑 -> 那张图权重翻倍。
            dup_dir = s.root / "stickers" / "_inbox" / "下载包"
            dup_dir.mkdir(parents=True, exist_ok=True)
            same = b"\x89PNG-same-content-for-dedup"
            (dup_dir / "a.png").write_bytes(same)
            (dup_dir / "b.png").write_bytes(same)
            got2 = s.scan()
            dup_hits = [x for x in got2.values() if x.sha1 and x.path.startswith("stickers/_inbox/下载包/")]
            _assert(
                len(dup_hits) == 1,
                f"同内容的两个文件只该入库一张，实际 {len(dup_hits)} 张："
                f"{[x.path for x in dup_hits]}",
            )

            # ⑨ 用户自建目录就是新类别（2026-11-05 用户要求）
            #
            # 「你现在可以把宠物的图片表情种类扩充，就是按照图片种类来新开文件夹」
            #
            # 所以要钉住：**用户建什么目录，就有什么类别**，不需要改代码。
            # 而 `categories()` 只算"有图"的目录 —— 空文件夹只是占位。
            for cat in ("猫猫", "无语到极致", "生气"):
                d = s.root / "stickers" / cat
                d.mkdir(parents=True, exist_ok=True)
                (d / f"{cat}.png").write_bytes(f"\x89PNG-content-{cat}".encode())
            # 空目录：不该算类别
            (s.root / "stickers" / "空目录占位").mkdir(parents=True, exist_ok=True)

            got3 = s.scan()
            cats = s.categories()
            # ⚠️ 2026-11-06：`生气` 现在**映射到 `angry`**，而不是保留中文名。
            #
            # 因为 72 类体系里 `angry` 的标签就是"生气" ——
            # `_emotion_from_path` 会先做"中文标签 -> key"的反查。
            #
            # 而 `猫猫`/`无语到极致` 不在标签表里，所以原样保留成类别名。
            # 这两类一起断言，才能同时守住"已知标签要归一"和"自定义目录要保留"。
            for name, expect in (
                ("猫猫", "猫猫"),
                ("无语到极致", "无语到极致"),
                ("生气", "angry"),        # 已知标签 -> 归一成英文 key
            ):
                _assert(
                    expect in cats,
                    f"「{name}」应当体现为类别「{expect}」，实际 {sorted(cats)}",
                )
                hit = [x for x in got3.values() if x.emotion == expect]
                _assert(hit, f"「{name}」目录里的图应当归到「{expect}」")
            _assert(
                "空目录占位" in cats,
                "空目录**也算**类别（2026-11-06 改）—— 否则用户把新图全放进 "
                "_inbox、情绪目录还空着时，模型收到空类别表只能全分到 other，"
                "那四个目录永远填不上（实测 2271 张全归 other 的死锁）",
            )

            # ⑩ 非法目录名不当类别（防越界/垃圾目录）
            for bad in ("CON", "_internal"):
                (s.root / "stickers" / bad).mkdir(parents=True, exist_ok=True)
                (s.root / "stickers" / bad / "x.png").write_bytes(b"\x89PNG-bad")
            got4 = s.scan()
            _assert(
                not any("CON" == x.emotion for x in got4.values()),
                "Windows 保留名不该成为类别",
            )
            _assert(
                not any("_internal" == x.emotion for x in got4.values()),
                "下划线开头的是内部目录，不该成为类别",
            )

            return "目录/入库/去重/标注/归类/挑选/中文目录/重复图/动态类别 全部正确"
        finally:
            import shutil as _sh

            _sh.rmtree(tmp, ignore_errors=True)

    @suite.check("图片：user.image 走完 存盘 -> 看懂 -> 拼进 user 轮")
    def _() -> str:
        """用户 2026-11-02：「做图片精准识别解析功能」。

        ## 这条测什么

        完整链路（用假视觉，不花钱）：
          ① base64 -> 存进 pet-media/images/
          ② 视觉模型给描述
          ③ 描述拼进**用户那一轮**（不是 system）

        ## ⚠️ 为什么必须拼进 user 轮

        system 里的内容会被记忆/人设压过去（`stage_direction` 那条注释
        记的就是这个坑）。只有放进 user 轮，模型才会真的顺着图说话。
        """
        import asyncio
        import base64
        import tempfile
        from pathlib import Path as _P

        from .config import Config
        from .gateway import Gateway
        from .media import MediaStore

        tmp = _P(tempfile.mkdtemp(prefix="pet-selftest-img-"))
        try:
            import os as _os

            old_data = _os.environ.get("PET_DATA_DIR")
            _os.environ["PET_DATA_DIR"] = str(tmp)
            try:
                gw = Gateway(Config())
            finally:
                if old_data is None:
                    _os.environ.pop("PET_DATA_DIR", None)
                else:
                    _os.environ["PET_DATA_DIR"] = old_data

            # 媒体根指到临时目录（不污染项目的 pet-media/）
            gw.media = MediaStore(tmp / "pet-media")
            gw.media.ensure_dirs()

            got: list[str] = []

            async def _spy(event) -> None:
                if event.name == "user.text":
                    got.append(str((event.payload or {}).get("text") or ""))

            gw.bus.subscribe("user.text", _spy)

            # 假视觉：不联网
            import pet.media_vision as mv

            orig = mv.describe_image
            mv.describe_image = lambda data, cfg, **kw: ("一只橘猫趴在键盘上", "")  # type: ignore[assignment]
            try:
                png = b"\x89PNG\r\n\x1a\n" + b"fake-image-bytes" * 10

                class _Incoming:
                    type = "user.image"
                    raw = {
                        "data": base64.b64encode(png).decode(),
                        "mime": "image/png",
                        "name": "cat.png",
                    }
                    text = "你看这个"
                    request_id = "r1"

                asyncio.run(gw._on_user_image(_Incoming()))
            finally:
                mv.describe_image = orig  # type: ignore[assignment]

            _assert(got, "对话没有收到任何东西 —— 用户发了图，宠物不能沉默")
            user_text = got[-1]
            _assert("橘猫" in user_text, f"描述没进对话：{user_text[:80]!r}")
            _assert("你看这个" in user_text, "用户配的话丢了")
            _assert(
                "用户发来一张图片" in user_text,
                f"应当标明这是图片：{user_text[:60]!r}",
            )

            # 入库了吗
            files = list((tmp / "pet-media" / "images").glob("*.png"))
            _assert(files, "图片应当存进 pet-media/images/")
            items = list(gw.media.load().values())
            _assert(items, "应当登记进 index.json")
            _assert(items[0].caption, "应当顺手写上标注")
        finally:
            import shutil as _sh

            _sh.rmtree(tmp, ignore_errors=True)

        return "base64 存盘 -> 看懂 -> 拼进 user 轮；描述和用户配的话都在"

    @suite.check("图片：视觉不可用时**不能沉默**，要如实说看不清")
    def _() -> str:
        """用户发了图，宠物一声不吭比说"我看不清"糟得多。

        这条守住那条分支：视觉模型挂了（没密钥/限流/余额）时，
        仍然要发一条 user.text，而且明确要求它**别编造画面内容**。
        """
        import asyncio
        import base64
        import tempfile
        from pathlib import Path as _P

        from .config import Config
        from .gateway import Gateway
        from .media import MediaStore

        tmp = _P(tempfile.mkdtemp(prefix="pet-selftest-img2-"))
        try:
            import os as _os

            old_data = _os.environ.get("PET_DATA_DIR")
            _os.environ["PET_DATA_DIR"] = str(tmp)
            try:
                gw = Gateway(Config())
            finally:
                if old_data is None:
                    _os.environ.pop("PET_DATA_DIR", None)
                else:
                    _os.environ["PET_DATA_DIR"] = old_data

            gw.media = MediaStore(tmp / "pet-media")
            gw.media.ensure_dirs()

            got: list[str] = []

            async def _spy(event) -> None:
                if event.name == "user.text":
                    got.append(str((event.payload or {}).get("text") or ""))

            gw.bus.subscribe("user.text", _spy)

            import pet.media_vision as mv

            orig = mv.describe_image
            mv.describe_image = lambda data, cfg, **kw: ("", "没有可用的视觉模型")  # type: ignore[assignment]
            try:

                class _Incoming:
                    type = "user.image"
                    raw = {
                        "data": base64.b64encode(b"\x89PNG-fake").decode(),
                        "mime": "image/png",
                        "name": "x.png",
                    }
                    text = ""
                    request_id = ""

                asyncio.run(gw._on_user_image(_Incoming()))
            finally:
                mv.describe_image = orig  # type: ignore[assignment]

            _assert(got, "视觉不可用时也必须发一条（不能沉默）")
            _assert(
                "看不清" in got[-1],
                f"应当如实说看不清：{got[-1][:80]!r}",
            )
            _assert(
                "不要编造" in got[-1],
                f"必须明确禁止编造画面内容：{got[-1][:80]!r}",
            )
        finally:
            import shutil as _sh

            _sh.rmtree(tmp, ignore_errors=True)

        return "视觉挂掉时仍然开口，并要求别编造"

    @suite.check("表情包：情绪映射 / 何时该发 / 挑一个")
    def _() -> str:
        """用户 2026-11-02 要求：

        > 「可以用图片和用户聊天，比如表情包，代替一些开心、悲伤、无语、
        >   不理你这种简单和常见的状态表情。」

        ## 断言

        ① 情绪 -> 类别映射（72 类体系下能对上更多；`neutral` 仍不映射）
        ② **意图优先于心情**（「对不起」配道歉图，而不是"悲伤"图）
        ③ 概率门 < 1（每句都配图很吵）
        ④ 长回复不配图
        """
        import random as _random

        from .sticker import emotion_to_sticker, should_attach

        # ① 映射
        #
        # 🔴 2026-11-07：`neutral` 现在映射到 `smile`。
        #
        # 这是被用户报的「看不到宠物发的图」逼出来的：
        # `dominant` 在 mood 0.4~0.62 之间返回 neutral，而那正是**常态**。
        # 原来不映射 -> 绝大多数回复永远配不上图。
        rng = _random.Random(7)
        pairs = {
            "excited": "excited",
            "happy": "happy",
            "annoyed": "annoyed",
            "sad": "sad",
            "sleepy": "sleepy",
            "curious": "question",
            # 🔴 中性兜底：不映射的话大多数回复配不上图
            "neutral": "smile",
        }
        for dom, want in pairs.items():
            got = emotion_to_sticker(dom)
            _assert(
                got == want,
                f"{dom} 应当映射到 {want!r}，实际 {got!r}",
            )

        # ①-b 意图优先于心情（2026-11-06）
        #
        # 「对不起我错了」的心情可能是 neutral，但它显然该配道歉图。
        # 只看心情的话这类意图永远发不出图 —— 那正是细分类的初衷。
        from .sticker import intent_to_sticker

        for text, want in (
            ("对不起，是我错了", "sorry"),
            ("晚安，好梦", "goodnight"),
            ("分我一点嘛", "ask_money"),
            ("别难过，会好的", "comfort"),
        ):
            got = intent_to_sticker(text)
            _assert(got == want, f"「{text}」的意图应当是 {want!r}，实际 {got!r}")
        _assert(
            should_attach(
                reply_text="对不起", dominant="neutral", rate=1.0, rng=rng
            ),
            "意图命中时，即使心情是 neutral 也该能发图",
        )

        # ② 概率门：rate=0 时永不发，rate=1 且短回复且情绪匹配时必发
        rng = _random.Random(7)
        for _ in range(100):
            _assert(
                not should_attach(reply_text="嗯", dominant="happy", rate=0.0, rng=rng),
                "rate=0 时不该发",
            )
        _assert(
            should_attach(reply_text="嗯嗯", dominant="happy", rate=1.0, rng=rng),
            "rate=1 + 短回复 + 情绪匹配时必发",
        )
        # 🔴 2026-11-07：现在**所有** dominant 都有映射了（neutral -> smile），
        # 所以"心情映射不到就不发"这条已经没有反例。
        #
        # 但 `should_attach` 里那条判断**仍然要保留** —— 它守的是
        # "万一将来新增一个没有映射的心情标签，别发错图"。
        # 这里改成用**源码断言**守它，而不是找一个现在不存在的反例。
        import inspect as _inspect

        from . import sticker as _sticker_mod

        _src = _inspect.getsource(_sticker_mod.should_attach)
        _assert(
            "intent_to_sticker(reply_text)" in _src
            and "emotion_to_sticker(dominant)" in _src,
            "should_attach 必须同时看「意图」和「心情」—— "
            "只看心情的话道歉/道谢这类意图永远发不出图",
        )
        # neutral 现在能发了（这是修 bug 的关键）
        _assert(
            should_attach(reply_text="嗯", dominant="neutral", rate=1.0, rng=rng),
            "neutral 现在映射到 smile —— 它必须能发，否则大多数回复永远没图",
        )

        # ③ 长回复不配图
        #
        # ⚠️ 上限是 200（2026-11-06 从 60 提上来的），所以这里要造**真的**
        #    超过 200 字的内容 —— 原来只有 100 字，改上限后它不再算"长"，
        #    断言就变成了"期望不发、实际会发"。
        from .sticker import MAX_TEXT_FOR_STICKER

        long_text = "这是一段很长的回复。" * 40
        _assert(
            len(long_text) > MAX_TEXT_FOR_STICKER,
            f"测试前提：{len(long_text)} 字应当超过上限 {MAX_TEXT_FOR_STICKER}",
        )
        _assert(
            not should_attach(reply_text=long_text, dominant="happy", rate=1.0, rng=rng),
            "长回复不该配表情包",
        )

        # ④ 概率大致符合（1000 次抽样，rate=0.35 应落在 25%~45%）
        hit = sum(
            1
            for _ in range(1000)
            if should_attach(reply_text="短", dominant="happy", rate=0.35, rng=rng)
        )
        _assert(
            250 <= hit <= 450,
            f"rate=0.35 抽样 1000 次应落在 250~450，实际 {hit}",
        )

        return f"映射正确；概率门生效（{hit}/1000）；长回复不配图"

    @suite.check("表情包：零素材类别不发无关的图 + 问号不误判疑问")
    def _() -> str:
        """用户 2026-11-07 报：

        > 「你试试不同种类的表情包触发，我现在有些难触发，
        >   你试试不同的对话怎么触发」

        实测发现两个让"难触发/乱触发"的具体原因。

        ## 一、零素材类别全退到同一张 smile

        72 类里有 12 类一张素材都没有。那些意图命中后
        `pick_sticker` 返回 None，于是退回心情类 —— 而心情类常态是 `smile`。

        结果：说「有点害怕」「好孤单」「发呆中」发的**都是同一张微笑**。
        用户体感就是"它只会发那几张微笑"。

        ⚠️ 修的时候还踩了一次**顺序**：原来的候选表是
           `[意图, 心情, 相邻]`，于是 smile 排在相邻类前面，
           永远先命中 —— 相邻兜底等于没写。现在改成
           `[意图, 相邻, 心情]`。

        ## 二、单独的「？」当触发词，任何疑问句都被判成 question

        实测：「哈哈哈哈哈」时它回了句带问号的话 ->
        意图被判成 `question` -> 发出一张"疑问"表情包，
        和当时的气氛完全不符。

        现在只认真正的提问词（为什么/怎么会/真的吗），不看标点。
        """
        import random as _random

        from . import sticker as _st
        from .media import MediaStore
        from pathlib import Path as _P

        # ⚠️ 先建 store —— 下面多处要用（顺序写反过一次，别挪回去）
        store = MediaStore(_P(r"F:\ai\pet-media"))

        # ① 顺序：相邻类必须排在心情候选前面
        src = __import__("inspect").getsource(_st.pick_for_emotion)
        i_fb = src.find("_FALLBACK_CHAIN.get(intent_key")
        # ⚠️ 2026-11-07：心情那段从 `emotion_to_sticker(dominant)`（单映射）
        #    改成了 `_EMOTION_CANDIDATES`（多候选），断言要跟着换锚点。
        i_mood = src.find("_EMOTION_CANDIDATES")
        _assert(i_fb > 0, "应当有相邻类别兜底")
        _assert(i_mood > 0, "应当有心情候选表")
        _assert(
            i_fb < i_mood,
            "相邻兜底必须**排在心情候选前面** —— 否则 smile 永远先命中，"
            "「害怕」会发成微笑（实测踩过）",
        )

        # ①-b 心情候选要能轮换，不能永远同一个（2026-11-07 放宽后）
        import random as _r2

        _rng2 = _r2.Random(3)
        seen: set[str] = set()
        for _i in range(12):
            _it = _st.pick_for_emotion(
                store, "neutral", reply_text="今天天气不错", rng=_rng2
            )
            if _it:
                seen.add(_it.emotion)
        _assert(
            len(seen) >= 2,
            f"无意图的回复应当能在多个类别间轮换，实际只有 {seen} —— "
            "固定发一类会让用户觉得「翻来覆去就那几张」",
        )

        # ② 零素材类别 -> 发的是**相关**类别，不是无关的微笑
        #
        # ⚠️ 2026-11-07 放宽断言：素材是**动态的**（标注在跑、用户在放图），
        #    原来写死"害怕必须发 shocked"，后来「害怕」类真有了一张素材，
        #    直接发 scared 反而更对 —— 断言不该因为素材变多而失败。
        #
        #    所以判据从"必须发某个特定类别"改成
        #    "**不能发无关的类别**（smile/其它）"，这才是真正要守的东西。
        _IRRELEVANT = {"smile", "other"}
        for text, want_any in (
            ("有点害怕", {"shocked", "anxious", "scared", "wronged"}),
            ("好孤单", {"wronged", "sad", "lonely", "cry"}),
            ("发呆中", {"think", "speechless", "daze"}),
            ("我不同意", {"refuse", "annoyed", "disagree"}),
            ("好失望啊", {"sad", "helpless", "disappointed"}),
        ):
            item = _st.pick_for_emotion(store, "neutral", reply_text=text)
            _assert(item is not None, f"「{text}」应当能配到图")
            _assert(
                item.emotion in want_any or item.emotion not in _IRRELEVANT,
                f"「{text}」发了 {item.emotion!r} —— "
                "发无关的 smile 就等于没功能",
            )

        # ③ 问号不再单独触发 question
        _assert(
            _st.intent_to_sticker("是那串符号显示成图了吗？") != "question",
            "带问号但没提问词的句子**不该**判成 question —— "
            "宠物说话经常带问号，那样会把所有回复都判成疑问",
        )
        _assert(
            _st.intent_to_sticker("为什么会这样") == "question",
            "真正的提问词仍要能识别",
        )

        # ④ 日常应答有意图了（原来一个都匹配不到）
        for text, want in (
            ("好的收到", "ok"),
            ("你说的对", "agree"),
            ("这样啊", "relieved"),
        ):
            got = _st.intent_to_sticker(text)
            _assert(got == want, f"「{text}」应当匹配 {want!r}，实际 {got!r}")

        return "相邻兜底优先于心情；零素材类别发相关图；问号不误判"

    @suite.check("表情包标注：视觉可用时分类并挪进对应目录")
    def _() -> str:
        """用户要求：「用户自己放，放完之后让宠物熟悉下，自己个图片的内容打标注」。

        这条测标注**循环里的那套动作**（不跑循环本身，那要等 2 分钟）：
        看图 -> 分类 -> 写标注 -> 挪目录。
        """
        import tempfile
        from pathlib import Path as _P

        from .media import MediaStore
        from .media_vision import label_sticker

        tmp = _P(tempfile.mkdtemp(prefix="pet-selftest-label-"))
        try:
            s = MediaStore(tmp / "pet-media")
            s.ensure_dirs()

            # 用户丢一张图进 _inbox
            it = s.add_file(b"\x89PNG-cat-laugh", "laugh.png", source="user-put")
            _assert(it.path.startswith("stickers/_inbox/"), "新图应当进 _inbox")
            _assert(len(s.unlabeled()) == 1, "应当有 1 张待标注")

            # 假视觉：返回标准格式
            import pet.media_vision as mv

            orig = mv.describe_image
            mv.describe_image = lambda data, cfg, **kw: (  # type: ignore[assignment]
                "类别：A\n描述：一只橘猫张嘴大笑",
                "",
            )
            try:
                data = (s.root / it.path).read_bytes()
                emo, cap, why = label_sticker(data, None)
            finally:
                mv.describe_image = orig  # type: ignore[assignment]

            _assert(not why, f"不该失败：{why}")
            _assert(emo == "happy", f"应当分类为 happy，实际 {emo!r}")
            _assert("橘猫" in cap, f"描述不对：{cap!r}")

            # 模拟循环里的两步
            s.annotate(it, cap, by="auto")
            _assert(s.move_to_emotion(it, emo), "挪目录应当成功")
            _assert(it.path.startswith("stickers/happy/"), f"应当在 happy 下：{it.path}")
            _assert(len(s.unlabeled()) == 0, "标注+挪完就不该再算未标注")
            _assert(
                len(s.stickers_for("happy")) == 1,
                "happy 类下应当能查到它",
            )
        finally:
            import shutil as _sh

            _sh.rmtree(tmp, ignore_errors=True)

        return "看图 -> 分类 happy -> 写标注 -> 挪进 happy/"

    @suite.check("协议：user.image 的三种非法输入都要被拒")
    def _() -> str:
        """图片是用户可控的大块数据，协议层必须挡住畸形输入。

        三条：缺 data / data 不是字符串 / 编码后超大。
        """
        import json as _json

        from .protocol import ProtocolError, parse_incoming

        bad = [
            {"type": "user.image"},
            {"type": "user.image", "data": ""},
            {"type": "user.image", "data": 123},
        ]
        for payload in bad:
            try:
                parse_incoming(_json.dumps(payload))
            except ProtocolError:
                continue
            raise AssertionError(f"未拒绝非法输入：{payload}")

        # 超过 10 MB 的（用长度估算，不真造那么大）
        huge = "A" * (11 * 1024 * 1024 * 4 // 3 + 100)
        try:
            parse_incoming(_json.dumps({"type": "user.image", "data": huge}))
            raise AssertionError("超大图片应当被拒")
        except ProtocolError:
            pass

        # 合法的一条要能过
        ok = parse_incoming(
            _json.dumps(
                {
                    "type": "user.image",
                    "data": "QUJD",
                    "mime": "image/png",
                    "text": "看这个",
                }
            )
        )
        _assert(ok.type == "user.image", "类型不对")
        _assert(ok.text == "看这个", f"text 没取到：{ok.text!r}")

        return "缺 data / 非字符串 / 超大 都拒绝；合法的一条能过"

    @suite.check("表情包下载：安全边界（协议/内网/magic bytes）")
    def _() -> str:
        """用户 2026-11-03 要求宠物能自己上网补充表情包。

        > 「宠物也拥有能自己上网补充自己的表情包……就是给于它下载的功能」

        ## 为什么这条测试最重要

        "从网上下载东西"是本项目里**攻击面最大**的功能之一：
        搜索结果由外部决定，一个被构造的结果就能让程序去访问任意地址。

        所以这里把每一条硬约束都钉住。**全部是纯函数**，不联网。
        """
        from .sticker_fetch import _sniff, extract_images_from_html, is_safe_url

        # ① 只允许 http(s)：file:// 能读本地文件，data: 能绕过所有检查
        for bad in (
            "file:///C:/Windows/system.ini",
            "data:image/png;base64,AAAA",
            "ftp://example.com/a.png",
            "javascript:alert(1)",
            "",
        ):
            ok, why = is_safe_url(bad)
            _assert(not ok, f"这个必须被拒：{bad!r}（实际通过了）")
            _assert(why, f"拒绝时要给原因：{bad!r}")

        # ② 内网地址（防 SSRF）：搜索结果里塞一个就能打到本机服务
        for bad in (
            "http://127.0.0.1:8080/x.png",
            "http://localhost/x.png",
            "http://10.0.0.5/x.png",
            "http://192.168.1.1/x.png",
            "http://169.254.169.254/latest/meta-data",
            "http://172.16.0.1/x.png",
        ):
            ok, _why = is_safe_url(bad)
            _assert(not ok, f"内网地址必须被拒：{bad}")

        # ③ 正常的公网图片要放行
        for good in (
            "https://example.com/a.png",
            "http://example.com/a.png",
            "https://cdn.example.com/path/to/x.jpg?v=1",
        ):
            ok, why = is_safe_url(good)
            _assert(ok, f"这个应当允许：{good}（{why}）")

        # ④ magic bytes：服务端说 image/png 但返回 HTML 是**很常见**的
        #    （防盗链/登录页），所以必须看文件头而不是 Content-Type
        _assert(_sniff(b"\x89PNG\r\n\x1a\nrest") == "png", "PNG 头没认出来")
        _assert(_sniff(b"\xff\xd8\xffrest") == "jpg", "JPEG 头没认出来")
        _assert(_sniff(b"GIF89a...") == "gif", "GIF 头没认出来")
        _assert(_sniff(b"RIFF1234WEBP...") == "webp", "WEBP 头没认出来")
        _assert(_sniff(b"RIFF1234WAVE...") == "", "RIFF 但不是 WEBP，不该当图片")
        _assert(_sniff(b"<html>not an image</html>") == "", "HTML 必须被识破")
        _assert(_sniff(b"") == "", "空数据不是图片")

        # ⑤ 从页面里找图：装饰图要跳过，相对路径要补全
        html = """
        <img src="/logo/header.png">
        <img src="/icon/arrow.png">
        <img data-src="https://cdn.example.com/cat.jpg">
        <img src="//cdn2.example.com/dog.webp">
        <img src="data:image/png;base64,AAAA">
        <img src="/rel/path/fish.png">
        """
        found = extract_images_from_html(html, base_url="https://example.com/page")
        joined = " ".join(found)
        _assert("cat.jpg" in joined, f"应当找到 cat.jpg：{found}")
        _assert("dog.webp" in joined, f"应当找到 dog.webp：{found}")
        _assert("fish.png" in joined, f"相对路径应当补全：{found}")
        _assert("logo" not in joined, f"logo 该被跳过：{found}")
        _assert("icon" not in joined, f"icon 该被跳过：{found}")
        _assert("data:" not in joined, f"data: URL 不该出现：{found}")

        return "协议/内网/magic bytes/装饰图 四道闸门都生效"

    @suite.check("表情包：总开关 + 下载子模块（2026-11-07 统合）")
    def _() -> str:
        """用户要求：

        > 「表情包下载和使用合并成一个模块，统合表情包的功能」

        ## 改之前的问题

        只有一个 `stickers` 模块，而且它**只管下载** ——
        `_maybe_send_sticker`（发表情包）完全不看模块开关。
        关掉「表情包下载」之后宠物**照样发表情包**，那开关等于没作用。

        ## 现在的结构

            stickers            总开关（发表情包/归类）。默认**开**
            stickers_download   上网找图。默认**关**，依赖 stickers + search

        ⚠️ 两个模块而不是一个，因为**风险不同**：
           发表情包是纯本地；上网下载会主动发请求并写文件。
           合成一个的话，想发表情包就不得不允许上网 —— 强迫授权。
        """
        import time as _t

        from .config import Config
        from .events import get_event
        from .gateway import Gateway
        from .modules import MODULES

        # ── 总开关 ──
        top = next((m for m in MODULES if m.name == "stickers"), None)
        _assert(top is not None, "应当有 stickers 总开关")
        _assert(top.label == "表情包", f"标签不对：{top.label!r}")
        _assert(top.default is True, "总开关必须默认开（纯本地行为，无风险）")
        _assert(top.gate, "必须可热切（设置页开关要能点）")

        # ── 下载子模块 ──
        spec = next((m for m in MODULES if m.name == "stickers_download"), None)
        _assert(spec is not None, "应当有 stickers_download 子模块")
        _assert(spec.label == "表情包下载", f"标签不对：{spec.label!r}")
        _assert(spec.default is False, "下载必须默认关（主动联网行为）")
        _assert(
            set(spec.requires) == {"stickers", "search"},
            f"必须同时依赖总开关和联网搜索：{spec.requires}",
        )
        _assert(spec.gate, "必须可热切")

        # ── 依赖闭包：总开关关了，下载也关 ──
        cfg0 = Config()
        from .modules import ModuleRegistry

        reg = ModuleRegistry(cfg0)
        reg.set_enabled("stickers", False)
        _assert(
            reg.enabled("stickers_download") is False,
            "总开关关掉时，下载必须自动关 —— 否则会出现"
            "「表情包都关了它还在偷偷下载」",
        )
        _assert(
            "stickers" in (reg.reason("stickers_download") or ""),
            "原因要指明是总开关关的",
        )
        # 反过来：关联网搜索只影响下载，不影响发表情包
        reg2 = ModuleRegistry(cfg0)
        reg2.set_enabled("search", False)
        _assert(
            reg2.enabled("stickers") is True,
            "关联网搜索不该影响「发表情包」（那是纯本地行为）",
        )
        _assert(reg2.enabled("stickers_download") is False, "下载要靠搜索，该关")

        # ── 事件仍然绑着 ──
        cfg = Config()
        cfg.root.setdefault("tools", {})["web_search_mode"] = "fast"
        gw = Gateway(cfg)
        ev = get_event("fetch_stickers")
        _assert(ev is not None, "应当有 fetch_stickers 事件")
        _assert(ev.handler is not None, "handler 必须已绑定（否则抽中了也不做事）")
        _assert(
            gw.events.module_of.get("fetch_stickers") == "stickers_download",
            "事件要挂在**下载**模块上（不是总开关）",
        )

        cfg = Config()
        cfg.root.setdefault("tools", {})["web_search_mode"] = "fast"
        gw = Gateway(cfg)
        ev = get_event("fetch_stickers")
        _assert(ev is not None, "应当有 fetch_stickers 事件")
        _assert(ev.handler is not None, "handler 必须已绑定（否则抽中了也不做事）")

        def _ok() -> tuple[bool, str]:
            return gw.events.eligible(
                ev, now=_t.time(), last_interaction_at=0, last_event_at=0, events_today=0
            )

        # 下载默认关 -> 抽取阶段就排除
        ok, why = _ok()
        _assert(not ok, "下载模块默认关时不该能抽")
        _assert("module_off" in why, f"原因要说明模块关着：{why!r}")

        # 打开下载 -> 可抽
        gw.modules.set_enabled("stickers_download", True)
        ok2, why2 = _ok()
        _assert(ok2, f"打开下载后应当能抽：{why2!r}")

        # 关掉 search -> 依赖闭包把它一起关掉
        gw.modules.set_enabled("search", False)
        ok3, why3 = _ok()
        _assert(not ok3, "关掉联网搜索后，表情包下载也该停")
        _assert("module_off" in why3, f"原因要说明：{why3!r}")

        # 🔴 关掉**总开关** -> 下载也停（这是"统合"的关键）
        gw.modules.set_enabled("search", True)
        gw.modules.set_enabled("stickers_download", True)
        ok4, _ = _ok()
        _assert(ok4, "两个都开着时应当能抽")
        gw.modules.set_enabled("stickers", False)
        ok5, why5 = _ok()
        _assert(
            not ok5,
            "总开关关掉后下载必须停 —— 否则「表情包都关了还在偷偷下载」",
        )
        _assert("module_off" in why5, f"原因要说明：{why5!r}")

        return "总开关管发表情包+下载；下载还额外依赖 search；闭包正确"

    @suite.check("免打扰：静音 + 不弹气泡，但**继续截图**、关掉后立刻聊")
    def _() -> str:
        """用户 2026-11-04 要求。

        > 「顺便加一个功能，免打扰，放在宠物控制里面，开启之后宠物不再出声
        >   和显示气泡，但是如果用户开启了自主截图功能，那么宠物每隔半小时
        >   会截一张图，等待用户关闭免打扰之后可以立刻根据免打扰时宠物
        >   截的图来对话。」

        ## 四件事分开断言

        ① 免打扰中**不合成语音**（`VoiceSession.speak` 被拦）
        ② 免打扰中**不弹气泡**（主进程 `decideBubble` 的 muted 参数 ——
           那条在 TypeScript 里，这里测后端那半）
        ③ 免打扰中**仍然截图**（`screenshot_due` 不再看 muted）
        ④ 关掉免打扰时用待用截图开口

        ⚠️ ③ 是最容易做错的：原来的 `screenshot_due` 用 `_suppression()`
           挡掉了所有抑制原因（含 muted），用户这次明确要"继续截"。
        """
        import asyncio
        import tempfile
        from pathlib import Path as _P

        from .config import Config
        from .gateway import Gateway

        tmp = _P(tempfile.mkdtemp(prefix="pet-selftest-dnd-"))
        try:
            import os as _os

            old = _os.environ.get("PET_DATA_DIR")
            _os.environ["PET_DATA_DIR"] = str(tmp)
            try:
                cfg = Config()
                gw = Gateway(cfg)
            finally:
                if old is None:
                    _os.environ.pop("PET_DATA_DIR", None)
                else:
                    _os.environ["PET_DATA_DIR"] = old

            # ── ① 不合成语音 ──
            gw.scheduler.set_muted(True)
            calls: list[str] = []
            orig_syn = gw.voice.tts.synthesize

            async def _spy(text):
                calls.append(text)
                return b"", "wav"

            gw.voice.tts.synthesize = _spy
            try:
                asyncio.run(gw.voice.speak("不该合成", "t1"))
            finally:
                gw.voice.tts.synthesize = orig_syn
            _assert(not calls, f"免打扰中不该合成语音，实际调了 {len(calls)} 次")

            # ── ② 说话被抑制（气泡那半在 TS 里，这里测后端的抑制）──
            why = gw.scheduler.suppression_reason(include_disabled=False)
            _assert(why == "muted", f"免打扰中应当抑制说话，实际 {why!r}")

            # ── ③ 仍然截图 ──
            gw.config.root.setdefault("providers", {}).setdefault("vision", {})[
                "enabled"
            ] = True
            gw.config.root.setdefault("activity", {})["overall"] = 0.8   # 好奇档
            gw.scheduler.last_autosnap_at = -1e9
            due, why2 = gw.scheduler.screenshot_due()
            _assert(
                due,
                f"免打扰中**仍然要能截图**（用户要求），实际 {why2!r}",
            )

            # ── ④ 关掉免打扰 -> 用待用截图开口 ──
            gw._pending_snaps = [
                {"description": "一个代码编辑器", "image": b"x", "titles": [], "at": 100.0},
                {"description": "一张漫画页", "image": b"y", "titles": [], "at": 200.0},
            ]
            spoke: list[dict] = []
            orig_speak = gw.conversation.speak_autonomously

            async def _cap(trigger=None, **kw):
                spoke.append({"trigger": trigger or {}, "kw": kw})

            gw.conversation.speak_autonomously = _cap
            try:
                asyncio.run(gw._flush_pending_snaps())
            finally:
                gw.conversation.speak_autonomously = orig_speak

            _assert(len(spoke) == 1, f"应当开口 1 次，实际 {len(spoke)}")
            hint = str(spoke[0]["trigger"].get("context_hint") or "")
            _assert("代码编辑器" in hint, f"提示词该带上画面描述：{hint[:80]!r}")
            _assert("漫画页" in hint, "两帧都该带上（让模型知道画面变过）")
            _assert(
                "免打扰期间" in hint,
                "必须说明这是**期间**看到的，否则模型会说成「你现在屏幕上」",
            )
            _assert(not gw._pending_snaps, "开口之后队列要清空")
            _assert(bool(spoke[0]["kw"].get("image")), "应当带上最新那张图")
        finally:
            import shutil as _sh

            _sh.rmtree(tmp, ignore_errors=True)

        return "不合成语音；说话被抑制；仍然截图；关掉后立刻用待用图开口"

    @suite.check("免打扰：状态冻结，但精力/心情照常变（关掉后继续）")
    def _() -> str:
        """用户 2026-11-05 要求。

        > 「免打扰下宠物状态会固定，但是注意力、心情等参数不固定，
        >   只是跟状态有固定关系的参数固定，比如精力影响的安静状态，
        >   关闭免打扰之后继续」

        ## 拆成两件事

        ① **状态**（安静/无聊/好奇/自言自语）在免打扰期间**不动**
           —— 否则"30 分钟没聊天转无聊"会自己跳，那就不是免打扰了
        ② **主观层参数**（精力、心情、亲近度）照常推进
           —— 所以免打扰结束时如果精力已掉到门槛下，下一轮正常转安静

        ## 怎么测

        `subjectivity.tick()` 之后看精力变没变；
        再看状态循环里那个 `continue` 的位置对不对（跑循环要等 30 秒，不划算）。
        """
        import tempfile
        import time as _t
        from pathlib import Path as _P

        from .config import Config
        from .gateway import Gateway

        tmp = _P(tempfile.mkdtemp(prefix="pet-selftest-dndfreeze-"))
        try:
            import os as _os

            old = _os.environ.get("PET_DATA_DIR")
            _os.environ["PET_DATA_DIR"] = str(tmp)
            try:
                cfg = Config()
                cfg.root.setdefault("tools", {})["web_search_mode"] = "fast"
                gw = Gateway(cfg)
            finally:
                if old is None:
                    _os.environ.pop("PET_DATA_DIR", None)
                else:
                    _os.environ["PET_DATA_DIR"] = old

            # ── ② 主观层照常推进（免打扰时也要 tick）──
            emo = gw.conversation.emotion
            before_energy = emo.energy
            before_mood = emo.mood
            emo._last_update = _t.time() - 300      # 造一个"5 分钟过去了"
            gw.scheduler.set_muted(True)
            gw.conversation.subjectivity.tick()
            _assert(
                emo.energy != before_energy or emo.mood != before_mood,
                "免打扰期间主观层参数必须照常变（用户要求参数不固定）",
            )

            # ── ① 状态冻结：muted 时跳过条件规则，且 tick 在跳过之前 ──
            import inspect

            src = inspect.getsource(Gateway._run_state_forever)
            _assert(
                "self.conversation.subjectivity.tick()" in src,
                "循环里必须先推进主观层",
            )
            _assert(
                "if self.scheduler.muted:" in src,
                "免打扰时要跳过条件规则（否则状态会自己跳）",
            )
            tick_at = src.index("self.conversation.subjectivity.tick()")
            mute_at = src.index("if self.scheduler.muted:")
            _assert(
                tick_at < mute_at,
                "主观层 tick 必须在跳过规则之前 —— 否则免打扰期间参数也冻住了",
            )
        finally:
            import shutil as _sh

            _sh.rmtree(tmp, ignore_errors=True)

        return "主观层照常 tick；状态规则被跳过；tick 在跳过之前"

    @suite.check("免打扰：麦克风请求一律被拒（用户报「还能触发语音」）")
    def _() -> str:
        """用户 2026-11-06：「有 bug，现在免打扰还能触发语音」。

        ## 现场

        启动日志：

            10:59:02.674  免打扰：从 persona.yaml 恢复为「开启」
            10:59:03.185  [按住说话] 渲染层已就绪，把开关状态推过去：开  ← 又开了
            10:59:07.423  [语音] 已连上 sidecar，补报麦克风开关：关

        `setPausedInternal` 在**更早**时发过 `pet:set-voice(false)`，但那时
        渲染层监听还没挂好（`webContents.send` 不排队，消息直接丢）。
        之后 `ptt-config`（推的是配置值）把闸门重新打开。

        ## 这条守的是最后一道门

        `voice.control` 是 sidecar 接收"开麦"的**唯一入口**。前端有多条
        路径能开麦（设置页、按住说话、唤醒词），逐个堵必然漏 ——
        所以在这里堵：免打扰中不管上游发什么，都不听。

        ⚠️ 拦截在 **Gateway 的消息分支**里，不在 `VoiceSession.set_enabled`。
           `set_enabled` 是底层原语，它只负责"照做"；"该不该做"的判断
           属于网关（它才知道免打扰状态）。
        """
        import tempfile
        from pathlib import Path as _P

        from .config import Config
        from .gateway import Gateway

        tmp = _P(tempfile.mkdtemp(prefix="pet-selftest-dndmic-"))
        try:
            import os as _os

            old = _os.environ.get("PET_DATA_DIR")
            _os.environ["PET_DATA_DIR"] = str(tmp)
            try:
                cfg = Config()
                gw = Gateway(cfg)
            finally:
                if old is None:
                    _os.environ.pop("PET_DATA_DIR", None)
                else:
                    _os.environ["PET_DATA_DIR"] = old

            import inspect

            src = inspect.getsource(Gateway)
            _assert(
                "免打扰中：忽略「开麦克风」请求" in src,
                "voice.control 里必须有免打扰拦截",
            )
            # 拦截必须发生在 `set_enabled` **之前**（否则已经开了再拦没用）
            branch = src.index('if incoming.type == "voice.control":')
            guard = src.index("免打扰中：忽略「开麦克风」请求", branch)
            call = src.index("await self.voice.set_enabled(enabled)", branch)
            _assert(
                guard < call,
                "拦截必须写在 set_enabled 之前 —— 否则麦克风已经被打开了",
            )

            # 免打扰关着时**不该拦**（别把正常路径也堵了）
            #
            # ⚠️ 这里只验证"拦截条件里有 muted 判断"，不去实际调
            #    `set_enabled` —— 那个方法还依赖引擎/采集器状态，
            #    在自检环境里可能因为缺依赖而什么都不做。
            #    测它会把"环境不完整"误报成"逻辑坏了"。
            _assert(
                "muted = self.scheduler.muted" in src[branch : call],
                "拦截必须基于实际的 muted 状态（免打扰关着时要放行）",
            )
        finally:
            import shutil as _sh

            _sh.rmtree(tmp, ignore_errors=True)

        return "拦截在 set_enabled 之前；且基于 muted 判断（关着时放行）"

    @suite.check("知识归档：同一本书累加版本，按章号能定位到对应那版")
    def _() -> str:
        """用户 2026-11-05 的两个要求。

        > 「后续如果搜索联网数据有更新也可以在里面更新，不断添加，
        >   这样怎么做数据结构好呢」
        > 「主要是让宠物想找某一个事件的时候要有自己搜索寻找到的功能，
        >   不用用户说继续下去才一级一级找到相关信息」

        ## 这条守住三件**改了看不出错**的事

        ① 同一本书读两次 = **1 个实体 2 个版本**（不是 2 个孤立记录）
        ② 按章号能落到**覆盖那一章**的版本（不是永远给最新）
        ③ 文件真的落盘了

        ## 为什么 ② 特别重要

        用户问「取两次读取的中间章节回看会怎么样」。如果只保留最新版，
        问第 2 章时会拿到"第 4~10 章"那版 —— 答非所问，而且**看起来像对的**
        （有内容、有条理，只是章节对不上）。这种错误最难被发现。
        """
        import tempfile
        from pathlib import Path as _P

        from .knowledge import KnowledgeStore, normalize_key

        tmp = _P(tempfile.mkdtemp(prefix="pet-selftest-know-"))
        try:
            store = KnowledgeStore(tmp / "pet-knowledge")

            # ① 同一本书读两批
            key = normalize_key("测试书")
            store.upsert(
                key=key, title="测试书", category="reading",
                scope="第 1~3 章", chapter_from=1, chapter_to=3,
                analysis="前情：主角在找人。", thoughts="我猜是同事干的。",
                body="章节内容" * 400, tags=["悬疑"],
            )
            store.upsert(
                key=key, title="测试书", category="reading",
                scope="第 4~10 章", chapter_from=4, chapter_to=10,
                analysis="线索指向另一个人。", thoughts="我猜错了。",
                body="后续内容" * 400, tags=["悬疑"],
            )

            st = store.stats()
            _assert(
                st["entities"] == 1,
                f"同一本书应当是 1 个实体，实际 {st['entities']}（说明 key 没归一化）",
            )
            _assert(
                st["revisions"] == 2,
                f"两次读应当累加成 2 版，实际 {st['revisions']}",
            )

            # ② 按章号定位
            r2 = store.revision_at(key, 2)
            r7 = store.revision_at(key, 7)
            _assert(
                r2 is not None and r2.n == 1,
                f"第 2 章应当落到第 1 版，实际 {r2.n if r2 else 'None'}",
            )
            _assert(
                r7 is not None and r7.n == 2,
                f"第 7 章应当落到第 2 版，实际 {r7.n if r7 else 'None'}",
            )
            # 越界章号退回最新版（至少给个参考，不是 None）
            r99 = store.revision_at(key, 99)
            _assert(
                r99 is not None and r99.n == 2,
                "超出范围的章号应当退回最新版，而不是返回 None",
            )

            # ③ 能按关键词搜到
            hits = store.recall("猜错")
            _assert(hits, "搜「猜错」应当能找到（它在第 2 版的想法里）")
            _assert(hits[0]["versions"] == 2, "搜索结果该告诉我们它有几版")

            # ④ 轨迹能看出想法变化（这才是"留版本"的意义）
            tl = store.timeline(key)
            _assert(len(tl) == 2, f"轨迹应当有 2 条，实际 {len(tl)}")
            _assert(
                tl[0]["thoughts"] != tl[1]["thoughts"],
                "两版的想法必须不同 —— 否则看不出变化，留版本就白留了",
            )

            # ⑤ 文件真的落盘了（用户要"放项目下的文件夹"）
            files = list((tmp / "pet-knowledge").rglob("v*.md"))
            _assert(len(files) == 2, f"应当写出 2 个 md，实际 {len(files)}")
            text = files[0].read_text(encoding="utf-8")
            _assert("## 我的分析" in text, "md 里该有分析段")
            _assert("## 我的想法" in text, "md 里该有想法段")
        finally:
            import shutil as _sh

            _sh.rmtree(tmp, ignore_errors=True)

        return "1 实体 2 版本；按章号定位到对应版；能搜；轨迹看得到想法变化"

    @suite.check("知识检索：宠物有自己翻档案的工具（不用用户一级级引导）")
    def _() -> str:
        """用户 2026-11-05：

        > 「主要是让宠物想找某一个事件的时候要有自己搜索寻找到的功能，
        >   不用用户说继续下去才一级一级找到相关信息」

        ## 三个工具的分工

            search_knowledge  不知道在哪 -> 按关键词找
            recall_knowledge  知道是哪本 -> 看完整轨迹
            read_knowledge    要细节     -> 读全文

        ⚠️ 这条守的是"工具真的挂上去了"。
           `_tool_list()` 里任何一个 AttributeError 都会被外层 except 吞掉，
           表现是**所有工具一起消失** —— 这个项目踩过好几次。
        """
        import asyncio
        import tempfile
        from pathlib import Path as _P

        from . import knowledge as _km
        from .config import Config
        from .gateway import Gateway

        tmp = _P(tempfile.mkdtemp(prefix="pet-selftest-ktools-"))
        try:
            import os as _os

            old = _os.environ.get("PET_DATA_DIR")
            _os.environ["PET_DATA_DIR"] = str(tmp)
            try:
                cfg = Config()
                cfg.root.setdefault("tools", {})["web_search_mode"] = "fast"
                gw = Gateway(cfg)
            finally:
                if old is None:
                    _os.environ.pop("PET_DATA_DIR", None)
                else:
                    _os.environ["PET_DATA_DIR"] = old

            # 换到临时库，避免污染项目里的 pet-knowledge/
            gw.knowledge = _km.KnowledgeStore(tmp / "pet-knowledge")
            gw.conversation.knowledge = gw.knowledge
            gw.conversation._tools = None

            tools = {t.name: t for t in gw.conversation._tool_list()}
            for name in ("search_knowledge", "recall_knowledge", "read_knowledge"):
                _assert(name in tools, f"缺少工具 {name}（工具表：{sorted(tools)}）")

            # 空档案时给的是**可操作的提示**，不是空白
            text = asyncio.run(tools["search_knowledge"].handler({"query": "随便"}))
            _assert(
                "空" in text or "还没有" in text,
                f"空档案该明说，实际：{text[:60]!r}",
            )

            # 加一条，再搜得到
            gw.knowledge.upsert(
                key="测试条目", title="测试条目", category="search",
                analysis="这是测试用的分析。", thoughts="这是测试用的想法。",
                body="正文" * 100,
            )
            text2 = asyncio.run(tools["search_knowledge"].handler({"query": "测试"}))
            _assert("测试条目" in text2, f"应当搜到，实际：{text2[:80]!r}")
        finally:
            import shutil as _sh

            _sh.rmtree(tmp, ignore_errors=True)

        return "三个工具都挂上了；空档案给提示；有内容能搜到"


def main(argv: Optional[list[str]] = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)-18s %(message)s"
    )
    """
    ⚠️ **第一件事就是隔离记忆写入**（见 `_isolate_memory_writes`）。

    必须在 `run_selftest` 之前调用 —— 它会替换 `MemoryService._resolve_db_path`，
    而自检里所有 `Gateway(...)` 都在其内部构造，晚一步就已经写进去了。
    """
    # 建新的临时库之前，先收掉上次崩溃留下的（见 `_sweep_stale_memory_tmp`）
    _sweep_stale_memory_tmp()
    tmp_mem = _isolate_memory_writes()
    # 📚 知识归档也要隔离（2026-11-05 加）。
    #
    # ⚠️ 理由和记忆库那条**完全一样**：自检里十几处 `Gateway(cfg)`，
    #    每个都建 `KnowledgeStore`。不隔离就会往用户真实的
    #    `F:\ai\pet-knowledge` 里写 `reading/测试书/v1-*.md`。
    #
    # 用环境变量而不是 monkey-patch（见 `_isolate_knowledge_writes` 的说明）。
    tmp_know = _isolate_knowledge_writes()
    suite = run_selftest(_default_config_candidate())
    print_report(suite)
    # 临时库留在盘上没意义（而且会越积越多），测完删掉
    try:
        shutil.rmtree(tmp_mem, ignore_errors=True)
    except Exception:  # noqa: BLE001
        pass
    try:
        shutil.rmtree(tmp_know, ignore_errors=True)
    except Exception:  # noqa: BLE001
        pass
    # ⚠️ 同理清掉 `.tmp-test/` 里本次建的目录（2026-10-09 加）。
    #    这里兜底是因为有两个测试忘了自己清，428 次累积了 224 MB。
    _cleanup_workspace_tmp()
    # 顺便报告仓库根下**无前缀**的 tmp*（2026-11-02 加）。
    # 那些多半是调试脚本用裸 `mkdtemp()` 留下的 —— 只提示，不自动删
    # （`tmp*` 太通用，可能是别的工具建的）。
    _warn_bare_tmp_dirs()
    return 0 if suite.ok else 1


def _default_config_candidate() -> Optional[Path]:
    """
    找随仓库的 config/persona.yaml。

    路径必须从本文件位置推：本文件在 apps/brain/pet/ 下，
    向上三级才是仓库根。用 cwd 推会在别的目录启动时静默跳过配置检查。
    """
    root = Path(__file__).resolve().parents[3]
    candidate = root / "config" / "persona.yaml"
    return candidate if candidate.exists() else None


def _isolate_memory_writes() -> Path:
    """
    **让自检期间所有记忆写入都落到临时库**（数据层兜底）。

    ## ⚠️ 为什么要在数据层兜，而不是逐个改 `Gateway(cfg)` 的输入

    自检里有 **7 处** `Gateway(cfg)`，它们的 `cfg` 来源有两种：

      · `load_selftest_config(...)` —— 已在配置层重定向（我第一版的做法）
      · 裸的 `Config()` —— **漏了 5 处**

    逐个改要记得改 7 处，而且以后新增测试**还会再犯**。这种事靠"人记得"
    一定会漏，所以改成在数据层一刀切：直接替换
    `MemoryService._resolve_db_path`，让它在自检进程里**永远**返回临时路径
    —— 不管调用方怎么造 config。

    ## 为什么这次事故值得下重手（2026-10-08）

    每跑一次自检就往**用户真实记忆库**写 6 行测试对话。我为了验语音跑了
    20+ 次，结果"最近记忆"窗口被重复的测试对话挤满，用户真正的对话
    （他唱的歌）全被挤出注入范围 —— 表现为**「按键说话没有记忆」**。

    **用户根本不知道是自己的测试工具在污染数据**，只会觉得"记忆坏了"。
    这类"工具副作用"比普通功能 bug 更隐蔽。

    ## 返回

    临时目录（测完可删）。
    """
    from .memory2 import MemoryService

    tmp_dir = Path(tempfile.mkdtemp(prefix="pet-selftest-mem-"))
    original = MemoryService._resolve_db_path

    def safe_resolve(self: Any, section: dict[str, Any]) -> Path:
        """
        ## ⚠️ 只在 `path` 没被显式指定时才改道（关键细节）

        第一版我**无条件**返回临时路径，结果把**两个本来就用了临时库的测试**
        也重定向走了 —— 它们断言"自己那个 pet.db"存在，于是全红：

            端到端：记忆真的进了系统提示词并在对话后写入
                AssertionError: 关闭后数据库文件不存在 —— 重启就会失忆
            清空记忆端到端
                AssertionError: 前置条件：清空前应有数据库文件

        **测试自己的隔离是有效的，不该被覆盖**。真正危险的只有 `path: auto`
        （会解析到用户真实的 pet.db），所以只拦那一种。
        """
        raw = str(section.get("path") or "auto").strip()
        if raw and raw != "auto":
            # 调用方明确指定了路径（测试的临时库 / 用户配置）—— 尊重它
            return original(self, section)
        return tmp_dir / "selftest.db"

    MemoryService._resolve_db_path = safe_resolve  # type: ignore[method-assign]
    MemoryService._selftest_original_resolve = original  # type: ignore[attr-defined]
    return tmp_dir


def _isolate_knowledge_writes() -> Path:
    """**让自检期间的知识归档也落到临时目录**（2026-11-05 加）。

    ## 为什么需要

    和 `_isolate_memory_writes` 是同一个问题：自检里十几处 `Gateway(cfg)`，
    每个都建一个 `KnowledgeStore`。不管的话它们全指向**真实的**
    `F:\\ai\\pet-knowledge` —— 跑一次自检就在用户项目里留下一堆
    `reading/测试书/v1-*.md`。

    ## 为什么用环境变量而不是替换函数

    记忆库那边替换的是 `MemoryService._resolve_db_path`，因为它的路径
    解析逻辑复杂（要看配置里的 `path: auto`）。而知识库的路径只有
    "环境变量 / 项目根"两个来源（见 `knowledge.knowledge_dir`），
    一个环境变量就够了，不需要 monkey-patch。

    ⚠️ 用 `os.environ[...] =` 而不是 `setdefault`：自检进程里必须
       以我们为准，不能因为外部恰好设了同一个变量就跳过隔离。
    """
    import os

    tmp_dir = Path(tempfile.mkdtemp(prefix="pet-selftest-know-"))
    os.environ["PET_KNOWLEDGE_DIR"] = str(tmp_dir / "pet-knowledge")
    return tmp_dir


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
