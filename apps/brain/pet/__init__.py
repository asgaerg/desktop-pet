"""
pet —— 桌面宠物的 Python 大脑（sidecar）。

对外只暴露四样东西：版本、配置、网关、插件基类。其余都是内部实现，
前端永远只通过 WebSocket 协议与这里交互。

入口：
    python -m pet                      启动 sidecar
    python -m pet --selftest           离线自检（不需要前端、不需要密钥）
    python -m pet --check-config       只校验配置并打印生效值
"""

from __future__ import annotations

__version__ = "0.1.0"

from .config import Config, ConfigError, load_config
from .gateway import Conversation, Gateway
from .plugins import Plugin, PluginHub, PluginContext, ToolSpec
from .protocol import PROTOCOL_VERSION

__all__ = [
    "__version__",
    "PROTOCOL_VERSION",
    "Config",
    "ConfigError",
    "load_config",
    "Gateway",
    "Conversation",
    "Plugin",
    "PluginHub",
    "PluginContext",
    "ToolSpec",
]
