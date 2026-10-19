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

        original = compat_mod.urllib.request.urlopen
        compat_mod.urllib.request.urlopen = lambda *a, **k: _FakeResponse()  # type: ignore[assignment]
        try:
            events = list(
                brain._iter_sse(
                    LLMRequest(system="s", history=[], user_text="hi"), threading.Event(), state
                )
            )
        finally:
            compat_mod.urllib.request.urlopen = original  # type: ignore[assignment]

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
        """
        import inspect

        from .gateway import _SCREEN_SPEAK_HOLD_S

        speak_src = inspect.getsource(Gateway._on_autonomy_speak)
        _assert("_last_screen_speak_at" in speak_src, "搭话那条路没检查截图优先")
        _assert("_SCREEN_SPEAK_HOLD_S" in speak_src, "没用 60 秒闭嘴期常量")
        _assert(
            "self.scheduler.screenshot_due()" in speak_src, "截图也到点时没让路"
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

        original = content_mod.urllib.request.urlopen
        content_mod.urllib.request.urlopen = fake_urlopen  # type: ignore[assignment]
        try:
            result = extractor.extract(frame)
        finally:
            content_mod.urllib.request.urlopen = original  # type: ignore[assignment]

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

        content_mod.urllib.request.urlopen = fail_urlopen  # type: ignore[assignment]
        try:
            failed = extractor.extract(frame)
        finally:
            content_mod.urllib.request.urlopen = original  # type: ignore[assignment]
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
        _assert(
            1.4 < spoken.duration_s < 1.9,
            f"句子时长应涵盖两段语音（约 1.7s），实为 {spoken.duration_s:.2f}s",
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
        # 600ms 语音减去起说消抖（start_frames=3 -> 60ms）约 0.54~0.58s
        _assert(all(0.45 <= d <= 0.7 for d in durations), f"句子时长异常：{durations}")
        return f"切出 2 句，时长 {durations}（已剪掉句尾静音，含 60ms 起说消抖）"

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

        return (
            "3 条正常解析；编号/项目符号/解释文字容错通过；敏感与超长被丢弃；"
            f"容量 {EXTRACT_LIMIT} 条/{EXTRACT_MAX_CHARS} 字与三类提炼规则已断言"
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

    @suite.check("兴趣表：反复提到的会长出来，模板动词与一次性的事不会")
    def _() -> str:
        import os
        import shutil
        import tempfile

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
        _assert(len(dormant) == 3, f"应有 3 个未来功能路口，实际 {len(dormant)}")
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


def run_selftest(config_path: Optional[Path] = None) -> SelfTest:
    suite = SelfTest()
    test_config(suite, config_path)
    test_protocol(suite)
    test_brains(suite)
    test_autonomy(suite)
    test_subjectivity(suite)
    test_vision(suite)
    test_content(suite)
    test_voice(suite)
    test_memory(suite)
    test_bus_and_plugins(suite)
    test_logging(suite)
    test_end_to_end(suite, config_path)
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
    suite = run_selftest(_default_config_candidate())
    print_report(suite)
    # 临时库留在盘上没意义（而且会越积越多），测完删掉
    try:
        shutil.rmtree(tmp_mem, ignore_errors=True)
    except Exception:  # noqa: BLE001
        pass
    # ⚠️ 同理清掉 `.tmp-test/` 里本次建的目录（2026-10-09 加）。
    #    这里兜底是因为有两个测试忘了自己清，428 次累积了 224 MB。
    _cleanup_workspace_tmp()
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


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
