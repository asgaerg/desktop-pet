#!/usr/bin/env python3
"""验证语音播报门控（apps/brain/pet/voice.py 的 `_on_pet_say_end_speak`，2026-10-16 重建）。

## 被验证的规则（用户 2026-10-14 指定的语义）

    引擎是真实 TTS（非 Mock/none） AND providers.tts.enabled 为真（缺键视为真）
    AND reason 不是 interrupted/cancel —— 三者同时成立才合成语音。

配套主进程侧：菜单「语音播报」开关写 `providers.tts.enabled`（personaStore
落盘 + config.update 热更新），Python 侧**每次现读**而不是构造时固定。

## 实现：AST 抠方法体，不 import pet.voice

`pet.voice` 顶部 `import audioop`（Python 3.13 已移除）且拉起整条音频依赖链
—— 所以用 ast 从 voice.py 真源码里抠出 `_on_pet_say_end_speak` 方法编译成
独立函数，配上假 session 执行。**测的是磁盘上那份真代码**，任何 Python 都能跑。

## 五条规则

1. enabled=True + 真实引擎 + reason=done  -> 说话
2. enabled=False                          -> 不说话
3. 配置缺 enabled 键                      -> 说话（默认开）
4. MockTtsEngine（provider=none）         -> 不说话（合成也是静音，白费）
5. reason=interrupted                     -> 不说话（哪怕前面都满足）

用法：`python tools/check-tts-gate.py`
"""

from __future__ import annotations

import ast
import asyncio
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
VOICE_SRC = ROOT / "apps" / "brain" / "pet" / "voice.py"

FAILURES: list[str] = []


def check(name: str, cond: bool) -> None:
    print(f"  {'OK  ' if cond else 'FAIL'} {name}")
    if not cond:
        FAILURES.append(name)


def extract_gate_fn() -> Any:
    """从 voice.py 抠出 `_on_pet_say_end_speak`，编译成独立 async 函数。"""
    tree = ast.parse(VOICE_SRC.read_text(encoding="utf-8"), filename=str(VOICE_SRC))
    for cls in (n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "VoiceSession"):
        # ⚠️ 方法是 async def —— ast 里是 AsyncFunctionDef，不是 FunctionDef
        for fn in (
            n
            for n in cls.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "_on_pet_say_end_speak"
        ):
            mod = ast.Module(body=[fn], type_ignores=[])
            ns: dict[str, Any] = {
                "asyncio": asyncio,
                "logging": logging,
                # 方法体里的模块级 logger 名叫 `log`
                "log": logging.getLogger("check-tts-gate"),
            }
            exec(compile(mod, str(VOICE_SRC), "exec"), ns)  # noqa: S102 —— 有意为之
            return ns["_on_pet_say_end_speak"]
    raise SystemExit("voice.py 里没找到 VoiceSession._on_pet_say_end_speak（源码结构变了？）")


GATE = extract_gate_fn()


class FakeConfig:
    def __init__(self, tts_cfg: dict) -> None:
        self.providers = {"tts": tts_cfg}


class RealTtsEngine:  # 类名不是 MockTtsEngine 即视为真实引擎
    pass


class FakeSession:
    """假 VoiceSession：只提供门控用到的东西。"""

    def __init__(self, tts_engine: Any, tts_cfg: dict) -> None:
        self.tts = tts_engine
        self.config = FakeConfig(tts_cfg)
        self._speak_seq = 0
        self.spoken: list[tuple[str, str]] = []

    async def speak(self, text: str, say_id: str) -> None:
        self.spoken.append((text, say_id))


def run_gate(session: FakeSession, payload: dict) -> None:
    event = SimpleNamespace(payload=payload)
    asyncio.run(GATE(session, event))


def main() -> int:
    payload_done = {"id": "s1", "text": "你好呀，今天是晴天。", "reason": "done"}

    print("== 断言 ==")
    # 1. enabled=True + 真实引擎 -> 说话
    s = FakeSession(RealTtsEngine(), {"enabled": True, "provider": "openai"})
    run_gate(s, payload_done)
    check("enabled=True + 真实引擎 -> 说话", len(s.spoken) == 1)

    # 2. enabled=False -> 静音
    s = FakeSession(RealTtsEngine(), {"enabled": False, "provider": "openai"})
    run_gate(s, payload_done)
    check("enabled=False -> 不说话", not s.spoken)

    # 3. 缺 enabled 键 -> 默认开（老配置兼容）
    s = FakeSession(RealTtsEngine(), {"provider": "openai"})
    run_gate(s, payload_done)
    check("缺 enabled 键 -> 说话（默认开）", len(s.spoken) == 1)

    # 4. MockTtsEngine -> 不说话
    s = FakeSession(type("MockTtsEngine", (), {})(), {"enabled": True})
    run_gate(s, payload_done)
    check("MockTtsEngine（provider=none）-> 不说话", not s.spoken)

    # 5. interrupted -> 静音（哪怕其余全满足）
    s = FakeSession(RealTtsEngine(), {"enabled": True})
    run_gate(s, {"id": "s2", "text": "被打断的这句", "reason": "interrupted"})
    check("reason=interrupted -> 不说话", not s.spoken)

    print()
    if FAILURES:
        print(f"❌ {len(FAILURES)} 条挂了：{'、'.join(FAILURES)}")
        return 1
    print("✅ 五条全过 —— 语音播报门控行为正确")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
