"""
协议常量与消息构造/解析（Python 侧）。

单一来源：packages/protocol/protocol.json
TS 对应实现：packages/protocol/src/index.ts

约定：上行消息（前端 -> sidecar）用 `parse_incoming` 严格解析，字段缺失或
类型不对就丢弃并返回错误；下行消息（sidecar -> 前端）用 `msg()` 构造，
保证 `type` 与 `ts` 一定存在。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Optional

PROTOCOL_VERSION = 1
DEFAULT_PORT = 8931

# 情绪枚举（与 TS 侧 Emotion 保持一致）
EMOTIONS = ("neutral", "happy", "curious", "sleepy", "annoyed", "excited")
PRIORITIES = ("low", "normal", "high")

# 上行消息类型白名单
INCOMING_TYPES = frozenset(
    {
        "hello",
        "user.text",
        "user.interrupt",
        "user.poke",
        "voice.control",
        # 按住说话（用户要求「按下按键发声，停下结束」）。
        #
        # ## ⚠️ 加处理分支之前**必须先加白名单**（2026-10-07 踩到）
        #
        # 我在 `gateway.py` 里写好了 `voice.ptt` 的处理分支、也加了 TS 类型，
        # 但**漏了这一处** —— 于是消息在解析阶段就被拒，那个分支永远不执行。
        #
        # 表现极具迷惑性：前端日志一切正常（按下/松开都打出来了）、
        # 后端也没有崩溃，只有一行：
        #
        #     丢弃不合法的上行消息：未知消息类型：'voice.ptt'
        #
        # 而用户看到的是"松开之后没反应" —— 很容易误判成"识别不准"或
        # "音频没送过去"。**新增上行类型时，白名单是第一处要改的。**
        "voice.ptt",
        # 语音合成的**回执**（Electron -> Python）。
        #
        # ⚠️ 这是我第三次踩同一个坑 —— 上一行 `voice.ptt` 的注释就在警告它：
        # **加了处理分支、也加了 TS 类型，但漏了白名单 -> 消息在解析阶段被拒**。
        #
        # 为什么 `tts.result` 是"上行"（Electron -> Python）而不是下行：
        # 请求 `tts.synthesize` 是 **Python 发起的**（它要说话时请求主进程合成，
        # 因为本地 SAPI 只有 Node 起的 PowerShell 能用），所以回执方向相反。
        # 与 `memory.backup` / `memory.backup.done` 那种"主进程发起、Python 回执"正好反着。
        "tts.result",
        # 浏览器兜底的**回执**（Electron -> Python，2026-10-13）。
        #
        # ⚠️ 第四次踩同一个坑了 —— 上面 `voice.ptt` 和 `tts.result` 的注释
        # 都在警告这件事，我加处理分支时**还是漏了白名单**。
        # 而且这次是**自检替我抓住的**（`pet.selftest` 里那条
        # 「每一个被处理的上行类型都在白名单里」）—— 那条测试就是为这个坑写的。
        #
        # 方向同 `tts.result`：请求 `web.browser.fetch` 是 **Python 发起的**
        # （它读不了有 WAF 挑战的站，只有主进程的 Chromium 能过），
        # 所以回执方向相反。
        "web.browser.result",
        # 设置页「试听」：让宠物读一句话。走的是和真实说话**完全相同**的
        # `VoiceSession.speak()`，所以试听通了就等于真能出声。
        # （教训见上面 voice.ptt 那段：加处理分支之前必须先加白名单。）
        "voice.say",
        "memory.clear",
        # 手动备份：只有 sidecar 能给出真实结果（要 VACUUM INTO，见 gateway.py）
        "memory.backup",
        # 记忆管理界面（设置页的「记忆」「珍藏」两个标签）。
        #
        # ## 为什么是**一个**类型带 action，而不是十个类型
        #
        # 记忆管理会持续加操作（以后还有"合并两条""导出""按标签筛选"……）。
        # 每个操作一个消息类型，就要同步改三处：Python 白名单、TS 联合类型、
        # protocol.json —— 加得越多越容易漏，而漏了的表现是"点了没反应"。
        #
        # 一个 `memory.query` 带 `action` 字段，新增操作只需在 gateway 里加一个
        # 分支，协议层一行都不用动。
        "memory.query",
        # 「截图对话」（2026-10-17 用户要求）：主进程发来的**一次性**抓屏请求。
        #
        # ## ⚠️ 加处理分支之前**必须先加白名单**
        #
        # 这个文件里已经因为这个坑道歉过四次（见上面 voice.ptt / tts.result /
        # web.browser.result 的注释）。症状永远是同一个：
        # 处理分支写好了、TS 类型也加了，但消息在**解析阶段**就被拒，
        # 日志里只有一句"未知消息类型"，而界面上是"点了没反应"。
        #
        # 所以这次先加白名单。
        "vision.snap",
        # ⚠️ 和它配对的回执 `vision.snap.done` 是 **Python -> 主进程**，
        #    方向相反，不在这个白名单里（那边要看 `protocol.json` 的 toMain）。
        # 模块开关（2026-10-18，模块化阶段二）。
        # ⚠️ 这个文件里"加处理分支忘了加白名单"已经栽过四次 ——
        #    症状永远是"点了没反应"，日志里只有一句未知消息类型。
        "modules.query",
        "modules.set",
        "config.update",
    }
)


def msg(type_: str, **fields: Any) -> dict[str, Any]:
    """构造下行消息：自动补 ts，字段顺序稳定。"""
    out: dict[str, Any] = {"type": type_, "ts": int(time.time() * 1000)}
    out.update(fields)
    return out


def encode(payload: dict[str, Any]) -> str:
    """序列化为紧凑 JSON（ensure_ascii=False 让中文直接可读，便于抓包排查）。"""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


@dataclass(slots=True)
class Incoming:
    """解析后的上行消息。"""

    type: str
    raw: dict[str, Any]
    text: str = ""
    patch: dict[str, Any] = field(default_factory=dict)
    request_id: str = ""

    @property
    def ts(self) -> int:
        value = self.raw.get("ts")
        return int(value) if isinstance(value, (int, float)) else 0


class ProtocolError(Exception):
    """上行消息不符合协议。"""


def parse_incoming(raw_text: str) -> Incoming:
    """
    解析并校验一条上行消息；不合规抛 ProtocolError。

    前端与 sidecar 是两个进程，任何输入都当作不可信数据：类型必须已知，
    必填字段必须存在且类型正确，长度做上限保护。
    """
    if len(raw_text) > 256 * 1024:
        raise ProtocolError("消息过大")
    try:
        obj = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"不是合法 JSON：{exc}") from exc
    if not isinstance(obj, dict):
        raise ProtocolError("消息根节点必须是对象")

    type_ = obj.get("type")
    if not isinstance(type_, str) or type_ not in INCOMING_TYPES:
        raise ProtocolError(f"未知消息类型：{type_!r}")

    if type_ == "user.text":
        text = obj.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ProtocolError("user.text 缺少非空 text")
        if len(text) > 8000:
            raise ProtocolError("user.text 过长")
        return Incoming(type=type_, raw=obj, text=text, request_id=_str(obj.get("requestId")))

    if type_ == "voice.say":
        # 设置页「试听」。和 user.text 一样要取 text —— 不解析的话
        # `incoming.text` 是空串，gateway 那边虽然回退到 raw 取值，
        # 但**长度校验就绕过去了**（8000 字上限）。
        text = obj.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ProtocolError("voice.say 缺少非空 text")
        if len(text) > 8000:
            raise ProtocolError("voice.say 过长")
        return Incoming(type=type_, raw=obj, text=text, request_id=_str(obj.get("requestId")))

    if type_ == "config.update":
        patch = obj.get("patch")
        if not isinstance(patch, dict):
            raise ProtocolError("config.update 缺少对象 patch")
        return Incoming(type=type_, raw=obj, patch=patch)

    if type_ == "hello":
        version = obj.get("protocolVersion")
        if not isinstance(version, (int, float)):
            raise ProtocolError("hello 缺少 protocolVersion")
        if int(version) != PROTOCOL_VERSION:
            raise ProtocolError(
                f"协议版本不匹配：前端 {int(version)}，sidecar {PROTOCOL_VERSION}。请重新构建前端。"
            )
        return Incoming(type=type_, raw=obj)

    return Incoming(type=type_, raw=obj, request_id=_str(obj.get("requestId")))


def _str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """
    递归合并配置补丁，返回新字典（不改动入参）。

    config.update 支持两种写法，都要能用：
        {"activity": {"overall": 0.8}}   嵌套
        {"activity.overall": 0.8}        点号路径
    """
    out = dict(base)
    for key, value in patch.items():
        if "." in key:
            head, _, tail = key.partition(".")
            child = out.get(head)
            child = dict(child) if isinstance(child, dict) else {}
            out[head] = deep_merge(child, {tail: value})
        elif isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def clamp01(value: Any, default: float = 0.0) -> float:
    """把任意输入压到 0~1；非数值回退默认值。UI 滑杆越界不算错误。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return max(0.0, min(1.0, float(value)))
