"""
生成提供者的工厂与注册表。

加新提供者（Claude / Gemini / 本地 vLLM 特殊参数 / 假数据回放）的做法：
  1. 继承 `base.Brain`，实现 `stream()`；
  2. 在文件底部登记一行；
  3. providers.yaml 里把 provider 改成新名字。
主循环一行都不用改 —— 这是"可扩充"在代码层面的具体体现。
"""

from __future__ import annotations

import logging
from typing import Callable

from ..config import Config
from .base import Brain, EmotionState, LLMRequest, StreamEvent, Turn

log = logging.getLogger("pet.brains")

_REGISTRY: dict[str, Callable[[Config, EmotionState], Brain]] = {}


def register(name: str, factory: Callable[[Config, EmotionState], Brain]) -> None:
    """登记提供者工厂。"""
    _REGISTRY[name.lower()] = factory


def build_brain(config: Config, emotion: EmotionState) -> Brain:
    """
    按配置构建提供者。配置指向的提供者不可用时**降级到 mock** 而不是崩溃：
    宠物宁可"说傻话"，也不能因为一个没填的 key 就整个起不来。
    """
    wanted = config.brain_provider()
    factory = _REGISTRY.get(wanted)
    if factory is None:
        log.error("未知的 brain 提供者 %r，可用：%s。已降级为 mock", wanted, sorted(_REGISTRY))
        return _REGISTRY["mock"](config, emotion)
    try:
        brain = factory(config, emotion)
        log.info("brain 提供者就绪：%s（model=%s）", brain.name, getattr(brain, "model", "-"))
        return brain
    except Exception as exc:  # noqa: BLE001 —— 配置错误不该让 sidecar 起不来
        log.error("brain 提供者 %r 初始化失败：%s。已降级为 mock", wanted, exc)
        return _REGISTRY["mock"](config, emotion)


def available_providers() -> list[str]:
    return sorted(_REGISTRY)


# --- 注册内置提供者（导入即注册） ----------------------------------------- #
# 放在文件底部是有意的：先定义好 register/build，再让子模块完成注册，
# 避免子模块 import 本包时拿到半初始化的模块对象。

from .mock import build_mock  # noqa: E402
from .openai_compat import build_openai  # noqa: E402

register("mock", build_mock)
register("openai", build_openai)

__all__ = [
    "Brain",
    "EmotionState",
    "LLMRequest",
    "StreamEvent",
    "Turn",
    "build_brain",
    "register",
    "available_providers",
]
