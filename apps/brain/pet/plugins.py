"""
插件机制 —— "后续可扩充维护"的落地接口。

约定（整个项目的扩展哲学）：
  · 插件**不修改主循环**，只订阅事件、发布事件、向大脑注册工具；
  · 插件的异常被总线隔离，一个插件崩了不会让宠物哑巴；
  · 插件拿到的 `PluginContext` 是能力白名单：发消息、说话、读写自己的配置、
    定时器 —— 想拿网络或文件权限必须显式声明（P6 再加权限清单）。
  · 插件配置在 `config.yaml` 的 `plugins.<name>` 下，随主配置热更新。

一个完整插件的例子见 `plugins_examples/pomodoro.py`。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional

if TYPE_CHECKING:  # 避免与 bus 循环导入
    from .bus import Bus, Event
    from .gateway import Gateway

log = logging.getLogger("pet.plugins")


@dataclass(slots=True)
class ToolSpec:
    """
    暴露给大脑的可调用工具。P2 起，人格大脑可据此自动生成 function-calling 描述，
    插件因此能"被宠物主动使用"，而不只是被动响应事件。
    """

    name: str
    description: str
    parameters: dict[str, Any] = field(default_factory=dict)
    handler: Optional[Callable[..., Awaitable[Any]]] = None


class Plugin:
    """
    插件基类。子类至少实现 `name`，按需覆盖下列钩子。

    生命周期：
        on_load(ctx)          插件被启用时调用一次
        subscribe() -> [topic] 声明要听的事件
        on_event(event)        收到订阅的事件
        on_tick(now)           周期性心跳（需在 subscribe 里声明 "autonomy.tick"）
        tools() -> [ToolSpec]  暴露给大脑的工具
        on_unload()            停用时清理（定时器、句柄）
    """

    name: str = "unnamed"
    version: str = "0.1.0"
    description: str = ""

    def __init__(self) -> None:
        self.ctx: Optional["PluginContext"] = None
        self.enabled: bool = True

    # --- 生命周期 --------------------------------------------------------- #

    async def on_load(self, ctx: "PluginContext") -> None:
        self.ctx = ctx

    async def on_unload(self) -> None:
        return None

    # --- 事件 ------------------------------------------------------------- #

    def subscribe(self) -> list[str]:
        return []

    async def on_event(self, event: "Event") -> None:
        return None

    # --- 能力 ------------------------------------------------------------- #

    def tools(self) -> list[ToolSpec]:
        return []

    # --- 便捷访问 --------------------------------------------------------- #

    @property
    def config(self) -> dict[str, Any]:
        """本插件的配置段（config.yaml -> plugins.<name>）。"""
        if self.ctx is None:
            return {}
        return self.ctx.plugin_config(self.name)


@dataclass
class PluginContext:
    """
    交给插件的能力句柄。故意做窄：插件只能通过这些方法影响外界，
    不能直接摸 bus / 连接 / 大脑内部状态。
    """

    bus: "Bus"
    gateway: "Gateway"
    config_root: dict[str, Any]
    _timers: list[asyncio.Task] = field(default_factory=list, repr=False)

    async def say(self, text: str, priority: str = "low") -> None:
        """让宠物说一句话（走正常发言通道，前端表现与大脑发言一致）。"""
        from .protocol import msg as make_msg

        say_id = self.gateway.new_say_id()
        await self.bus.publish("pet.say.start", make_msg("pet.say.start", id=say_id, priority=priority))
        await self.bus.publish("pet.say.delta", make_msg("pet.say.delta", id=say_id, text=text))
        await self.bus.publish("pet.say.end", make_msg("pet.say.end", id=say_id, text=text, reason="done"))

    async def toast(self, text: str, level: str = "info") -> None:
        """给用户一条系统级提示（不占宠物发言位）。"""
        await self.bus.publish("system.error", {"level": level, "message": text, "where": "plugin"})

    def plugin_config(self, name: str) -> dict[str, Any]:
        plugins = self.config_root.get("plugins") or {}
        section = plugins.get(name)
        return section if isinstance(section, dict) else {}

    def every(self, seconds: float, callback: Callable[[], Awaitable[None]]) -> None:
        """注册周期任务；插件卸载时由 PluginHub 统一取消，避免任务泄漏。"""
        if seconds <= 0:
            raise ValueError("周期必须大于 0 秒")

        async def loop() -> None:
            while True:
                await asyncio.sleep(seconds)
                try:
                    await callback()
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001
                    log.exception("插件定时任务异常（已隔离）")

        self._timers.append(asyncio.get_running_loop().create_task(loop()))

    async def cancel_timers(self) -> None:
        for task in self._timers:
            task.cancel()
        if self._timers:
            await asyncio.gather(*self._timers, return_exceptions=True)
        self._timers.clear()


class PluginHub:
    """插件的装载、卸载、事件分发与工具汇总。"""

    def __init__(self, bus: "Bus", gateway: "Gateway", config_root: dict[str, Any]) -> None:
        self.bus = bus
        self.gateway = gateway
        self.config_root = config_root
        self._plugins: dict[str, Plugin] = {}
        self._unsubscribers: list[Callable[[], None]] = []
        self._loaded = False

    # ------------------------------------------------------------------ 装载

    async def load(self, plugins: list[Plugin]) -> None:
        """装载插件列表。单个插件失败只跳过它，不影响其他插件。"""
        ctx = PluginContext(bus=self.bus, gateway=self.gateway, config_root=self.config_root)
        for plugin in plugins:
            key = plugin.name.lower()
            if key in self._plugins:
                log.warning("插件 %s 重复注册，跳过后者", plugin.name)
                continue
            try:
                if inspect.iscoroutinefunction(plugin.on_load):
                    await plugin.on_load(ctx)
                else:  # 允许同步实现，降低写插件的门槛
                    plugin.on_load(ctx)  # type: ignore[misc]
                for topic in plugin.subscribe():
                    self._unsubscribers.append(self.bus.subscribe(topic, plugin.on_event))
                self._plugins[key] = plugin
                log.info(
                    "插件已装载：%s v%s%s",
                    plugin.name,
                    plugin.version,
                    f"（订阅 {len(plugin.subscribe())} 个事件）" if plugin.subscribe() else "",
                )
            except Exception:  # noqa: BLE001
                log.exception("插件 %s 装载失败，已跳过", plugin.name)
        self._loaded = True

    async def unload_all(self) -> None:
        for unsubscribe in self._unsubscribers:
            try:
                unsubscribe()
            except Exception:  # noqa: BLE001
                pass
        self._unsubscribers.clear()
        for plugin in self._plugins.values():
            try:
                if plugin.ctx is not None:
                    await plugin.ctx.cancel_timers()
                await plugin.on_unload()
            except Exception:  # noqa: BLE001
                log.exception("插件 %s 卸载异常", plugin.name)
        self._plugins.clear()

    # ------------------------------------------------------------------ 查询

    def all_tools(self) -> list[ToolSpec]:
        tools: list[ToolSpec] = []
        for plugin in self._plugins.values():
            if not plugin.enabled:
                continue
            try:
                tools.extend(plugin.tools())
            except Exception:  # noqa: BLE001
                log.exception("插件 %s 的 tools() 异常", plugin.name)
        return tools

    def status(self) -> list[dict[str, Any]]:
        """给设置面板用：已装插件一览。"""
        return [
            {
                "name": p.name,
                "version": p.version,
                "description": p.description,
                "enabled": p.enabled,
                "topics": p.subscribe(),
                "tools": [t.name for t in p.tools()],
            }
            for p in self._plugins.values()
        ]

    def get(self, name: str) -> Optional[Plugin]:
        return self._plugins.get(name.lower())

    def __len__(self) -> int:
        return len(self._plugins)
