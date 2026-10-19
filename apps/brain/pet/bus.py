"""
事件总线（Bus）—— 全项目唯一的模块间耦合点。

铁律：模块之间不直接互相调用。输入、视觉、大脑、自主调度、插件全部只
publish / subscribe 事件。这条规则是"后续可扩充维护"的全部代价所在：

  · 加新功能 = 订阅几个既有事件 + 发布自己的事件，主循环零改动；
  · 换实现（本地 ASR 换云端）= 替换一个订阅者，其他人无感；
  · 出问题 = 打开事件日志看事件流，而不是打断点跟调用栈。

设计取舍：
  · 一个订阅者抛异常绝不影响其他订阅者（宠物不能因为插件崩了就哑巴）；
  · 断言防呆：订阅时若 topic 不在已知表里，打警告 —— 拼错事件名是最常见的坑；
  · 事件带 name 与 payload 两部分，payload 用 dataclass，类型明确。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

log = logging.getLogger("pet.bus")

Handler = Callable[["Event"], Awaitable[None]]

# 已知事件名登记表。新增事件请在这里登记，否则订阅时会收到警告。
KNOWN_TOPICS: frozenset[str] = frozenset(
    {
        # 连接生命周期
        "client.connected",
        "client.disconnected",
        # 输入侧
        "user.text",
        "user.voice",
        "user.interrupt",
        "user.poke",
        # 配置
        "config.updated",
        # 大脑输出
        "pet.say.start",
        "pet.say.delta",
        "pet.say.end",
        "pet.emotion",
        # 一轮对话结束（含自主发言），调度器据此重置冷却
        "conversation.finished",
        # 语音链路（P3）
        "voice.state",
        "voice.transcript",
        "voice.audio",
        # 感知侧（P4 起用）
        "vision.observation",
        # 自主行为（P5）
        "autonomy.tick",
        "autonomy.speak",
        # 系统
        "system.error",
    }
)


@dataclass(slots=True)
class Event:
    """一条事件。`name` 用于订阅匹配，`payload` 为具体数据。"""

    name: str
    payload: Any = None
    ts: float = field(default_factory=time.time)

    def __repr__(self) -> str:  # pragma: no cover - 仅用于日志可读性
        return f"<Event {self.name} {self.payload!r}>"


class Bus:
    """
    异步发布订阅总线。

    订阅支持精确名与 `前缀.*` 通配：
        bus.subscribe("pet.say.delta", h)     # 精确
        bus.subscribe("pet.*", h)             # 该命名空间下全部
        bus.subscribe("*", h)                 # 全都收（用于事件日志/录制）
    """

    def __init__(self, strict_topics: bool = True) -> None:
        self._handlers: dict[str, list[Handler]] = {}
        self._strict = strict_topics
        self.delivered = 0
        self.errors = 0

    def subscribe(self, topic: str, handler: Handler) -> Callable[[], None]:
        """订阅，返回取消订阅的函数。"""
        if self._strict and topic not in KNOWN_TOPICS and not topic.endswith(".*") and topic != "*":
            log.warning("订阅了未登记的事件名 %r —— 多半是拼错了（见 bus.KNOWN_TOPICS）", topic)
        self._handlers.setdefault(topic, []).append(handler)

        def unsubscribe() -> None:
            handlers = self._handlers.get(topic)
            if handlers and handler in handlers:
                handlers.remove(handler)

        return unsubscribe

    def _matching(self, name: str) -> list[Handler]:
        out: list[Handler] = list(self._handlers.get(name, ()))
        if "." in name:
            ns = name.split(".", 1)[0] + ".*"
            out.extend(self._handlers.get(ns, ()))
        out.extend(self._handlers.get("*", ()))
        return out

    async def publish(self, name: str, payload: Any = None) -> None:
        """发布事件。串行 await 所有订阅者，保证同一事件内的处理顺序确定。"""
        event = Event(name=name, payload=payload)
        for handler in self._matching(name):
            try:
                await handler(event)
                self.delivered += 1
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 —— 隔离订阅者故障
                self.errors += 1
                log.exception("事件 %s 的订阅者 %r 抛异常（已隔离）", name, getattr(handler, "__qualname__", handler))

    def publish_soon(self, name: str, payload: Any = None) -> None:
        """不阻塞地发布（用于不能 await 的回调里，例如连接建立钩子）。"""
        asyncio.get_running_loop().create_task(self.publish(name, payload))

    def stats(self) -> dict[str, int]:
        return {
            "topics": len(self._handlers),
            "handlers": sum(len(v) for v in self._handlers.values()),
            "delivered": self.delivered,
            "errors": self.errors,
        }
