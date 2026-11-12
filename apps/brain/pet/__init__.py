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

__version__ = "0.3.0"

#: 许可证（2026-11-12 用户要求改成「不能商用」）。
#:
#: CC BY-NC 4.0 = 署名 + **禁止商业使用**。
#:
#: ⚠️ 严格说它**不是 OSI 定义的开源许可**（OSI 要求必须允许商用）。
#:    准确说法是「源码公开 + 非商业许可」。这里用 SPDX 标识符是为了
#:    让工具链（pip / npm / license 扫描器）能认出来。
__license__ = "CC-BY-NC-4.0"

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
