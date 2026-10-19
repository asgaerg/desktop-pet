"""端到端测 `EdgeTtsEngine` —— 走**产品自己的** `build_voice_pipeline`。

## 为什么要走 build_voice_pipeline 而不是直接 new

因为要验的是**整条选择链**：

    config(tts.provider='edge') -> build_voice_pipeline -> EdgeTtsEngine
    -> synthesize() -> (mp3 bytes, 'mp3')

直接 `new EdgeTtsEngine()` 只能证明那个类能跑，**证明不了配置接对了**。
（这个项目里"配置项没接线/假开关"出过好几次，所以宁可多验一层。）

## 测什么

  ① `tts.provider: 'edge'` 能不能选中 `EdgeTtsEngine`（而不是掉进 mock）
  ② `edge_voice` 真的被用上（换一个音色，产物应该不同）
  ③ 合成耗时（体验关键）
  ④ 产物是**合法 mp3**（用 PyAV 解一遍 —— 前端要能播）
  ⑤ 空文本 / 坏音色时的**报错是否清楚**（不能静默返回空字节）
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import sys
import time

sys.path.insert(0, "apps/brain")
sys.path.insert(0, ".")

from tool_bootstrap import ROOT, setup  # noqa: E402

setup(verbose=False)

from pet.config import load_config  # noqa: E402
from pet.voice import EdgeTtsEngine, build_voice_pipeline  # noqa: E402

CONFIG = ROOT / "config" / "persona.yaml"
TMP = ROOT / "apps" / "desktop" / "pet-data" / "ptt-tmp"
TMP.mkdir(parents=True, exist_ok=True)

TEXT = "你好呀，我是桌面宠物，现在用免费语音说话啦。"


async def main() -> int:
    print("  ═══ ① 走产品链路构建（改内存里的配置，不写盘）═══")
    cfg = load_config(CONFIG)
    tts_cfg = cfg.providers.setdefault("tts", {})
    tts_cfg["provider"] = "edge"
    tts_cfg["edge_voice"] = "zh-CN-XiaoxiaoNeural"

    pipe = build_voice_pipeline(cfg, api_key=cfg.api_key() or "")
    engine = pipe.tts
    print(f"    选中引擎: {type(engine).__name__}  name={getattr(engine, 'name', '?')}")
    print(f"    日志说明: {pipe.tts_note}")

    ok_engine = isinstance(engine, EdgeTtsEngine)
    print(f"    {'✅' if ok_engine else '❌'} 选中了 EdgeTtsEngine（没掉进 mock）")
    if not ok_engine:
        return 1

    print()
    print("  ═══ ② 合成（计时）═══")
    results = {}
    for voice in ("zh-CN-XiaoxiaoNeural", "zh-CN-YunxiNeural"):
        engine.voice = voice  # type: ignore[attr-defined]
        t0 = time.monotonic()
        try:
            payload, fmt = await engine.synthesize(TEXT)  # type: ignore[attr-defined]
            dt = (time.monotonic() - t0) * 1000
            out = TMP / f"engine-{voice}.mp3"
            out.write_bytes(payload)
            results[voice] = (len(payload), dt, out)
            print(f"    ✅ {voice:<26} {len(payload):>6} 字节  {dt:>6.0f}ms  fmt={fmt}  -> {out.name}")
        except Exception as error:  # noqa: BLE001
            print(f"    ❌ {voice:<26} {type(error).__name__}: {str(error)[:90]}")

    print()
    print("  ═══ ③ 产物是不是合法 mp3（前端要能播）═══")
    try:
        import av

        for voice, (_, _, out) in results.items():
            c = av.open(str(out))
            st = c.streams.audio[0]
            dur = float(st.duration * st.time_base) if st.duration else 0
            print(f"    ✅ {out.name}: {dur:.2f}s · {st.rate}Hz · {st.channels}ch · {st.codec_context.name}")
            c.close()
    except Exception as error:  # noqa: BLE001
        print(f"    ❌ PyAV 解不开：{type(error).__name__}: {str(error)[:110]}")

    print()
    print("  ═══ ④ 换音色确实换了声音吗（字节数/内容应该不同）═══")
    if len(results) == 2:
        a, b = list(results.values())
        same = a[0] == b[0]
        print(f"    晓晓 {a[0]} 字节 / 云希 {b[0]} 字节 -> {'⚠️ 一样大（可能没换）' if same else '✅ 不同'}")

    print()
    print("  ═══ ⑤ 出错时报错清楚吗 ═══")
    for label, bad in [("空文本", "   "), ("坏音色", TEXT)]:
        t0 = time.monotonic()
        try:
            if label == "坏音色":
                engine.voice = "zh-CN-NotARealVoice"  # type: ignore[attr-defined]
            payload, _ = await engine.synthesize(bad)  # type: ignore[attr-defined]
            print(f"    ⚠️ {label}: 居然成功了（{len(payload)} 字节）—— 期望报错")
        except Exception as error:  # noqa: BLE001
            print(f"    ✅ {label}: {type(error).__name__}: {str(error)[:96]}")
        finally:
            engine.voice = "zh-CN-XiaoxiaoNeural"  # type: ignore[attr-defined]

    print()
    print("  ═══ 结论 ═══")
    print(f"    引擎接对了：{ok_engine}")
    print(f"    合成成功：{len(results)}/2")
    print("    下一步：把 config/persona.yaml 的 tts.provider 改成 edge 就能用")
    return 0 if (ok_engine and len(results) == 2) else 1


raise SystemExit(asyncio.run(main()))
