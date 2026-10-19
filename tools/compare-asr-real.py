"""用**真语音**对比云端 vs 本地 GPU 的 ASR 延迟和准确率。

## 为什么上一版测错了

我上次用**纯静音**当输入 —— 那对 whisper 是最坏情况：
没有语音它不会提前停，会一路解码 + 温度回退，产生大量空段。
**"静音 3 秒要 13 秒"不代表"说话 3 秒要 13 秒"。**

文档第 38 节有真实的 GPU 实测（那时用真音频）：

```
large-v3-turbo   float16   short 2.32s -> 297ms
                           mid   6.73s -> 359ms
                           long 14.05s -> 656ms
small            187 / 406 / 1750ms
tiny             125 / 391 / 1531ms   （但中文错得离谱：桌面宠物 -> 桌面重物）
```

**RTF 0.057 —— 比实时快 17 倍。** 所以本地 GPU 确实可行。

## 这个脚本做什么

  ① 用 TTS 合成几段**真中文语音**（不同长度）
  ② 同一段音频分别喂：云端 SenseVoiceSmall、本地 GPU large-v3-turbo
  ③ 两边都比：**延迟** + **识别文字**（逐字看准不准）
  ④ 每个跑多次，看稳定性（云端的问题是波动 43×）

⚠️ 音频走 mp3 解码（TTS 返回 mp3），用 `av` —— 项目里已经有这个依赖
（faster-whisper 依赖它）。
"""

from __future__ import annotations

import asyncio
import pathlib
import statistics
import sys
import tempfile
import time

sys.path.insert(0, "apps/brain")
sys.path.insert(0, ".")

from tool_bootstrap import ROOT, setup  # noqa: E402

setup(verbose=False)

from pet.config import load_config  # noqa: E402
from pet.voice import (  # noqa: E402
    LocalAsrEngine,
    OpenAIAsrEngine,
    _asr_model_dir,
    _env_or,
    vendor_from_url,
)

CONFIG = ROOT / "config" / "persona.yaml"

# 三句真实场景的话（长度递增），故意用项目里出现过的词
SENTENCES = [
    "今天天气不错。",
    "桌面宠物的程序用的是 Python 和 Electron。",
    "我刚才说的那句话，你看看日期对不对，是不是昨天晚上说的。",
]


def decode_to_pcm(payload: bytes, fmt: str) -> "object":
    """把 TTS 输出解成 16kHz 单声道 float32（whisper 要这个）。"""
    import av  # faster-whisper 的依赖，一定有
    import numpy as np

    container = av.open(payload if hasattr(payload, "read") else _as_file(payload))
    resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
    chunks = []
    for frame in container.decode(audio=0):
        for out in resampler.resample(frame):
            chunks.append(out.to_ndarray().reshape(-1))
    container.close()
    if not chunks:
        raise RuntimeError("解码后没有音频帧")
    pcm16 = np.concatenate(chunks).astype(np.int16)
    return pcm16.view(np.int16).astype(np.float32) / 32768.0


def _as_file(data: bytes):
    f = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
    f.write(data)
    f.close()
    return f.name


async def main() -> int:
    import numpy as np

    cfg = load_config(CONFIG)
    asr_cfg = dict(cfg.providers.get("asr") or {})
    tts_cfg = dict(cfg.providers.get("tts") or {})

    # ── 造音频 ────────────────────────────────────────────────────────
    print("  ═══ ① 合成真语音 ═══")
    from pet.voice import build_voice_pipeline

    pipe = build_voice_pipeline(cfg, api_key=cfg.api_key() or "")
    tts = getattr(pipe, "tts", None)
    if tts is None:
        print("     ❌ 拿不到 TTS 引擎")
        return 1
    print(f"     TTS：{getattr(tts, 'name', '?')}")

    samples: list[tuple[str, object, float]] = []
    for text in SENTENCES:
        try:
            payload, fmt = await tts.synthesize(text)
            audio = decode_to_pcm(payload, fmt)
            dur = len(audio) / 16000
            samples.append((text, audio, dur))
            print(f"     ✅ {dur:>5.2f}s  「{text}」")
        except Exception as e:  # noqa: BLE001
            print(f"     ❌ 合成失败：{type(e).__name__}: {str(e)[:60]}")

    if not samples:
        print("  ❌ 一段音频都没造出来")
        return 1

    # ── 云端 ──────────────────────────────────────────────────────────
    print()
    print("  ═══ ② 云端 SenseVoiceSmall ═══")
    cloud_base = str(asr_cfg.get("cloud_base_url") or "").rstrip("/")
    cloud_model = str(asr_cfg.get("cloud_model") or "")
    cloud_key = _env_or(
        asr_cfg.get("cloud_api_key_env"), "",
        vendor=vendor_from_url(cloud_base), config=cfg,
    )
    cloud = OpenAIAsrEngine(base_url=cloud_base, model=cloud_model, api_key=cloud_key)

    cloud_results: list[tuple[str, float, str]] = []
    for text, audio, dur in samples:
        times = []
        out = ""
        for i in range(3):
            t = time.monotonic()
            try:
                tr = await cloud.transcribe(
                    (audio * 32768.0).astype(np.int16).tobytes(), 16000
                )
                times.append((time.monotonic() - t) * 1000)
                out = (tr.text or "").strip()
            except Exception as e:  # noqa: BLE001
                print(f"     ❌ {type(e).__name__}: {str(e)[:50]}")
        if times:
            med = statistics.median(times)
            cloud_results.append((text, med, out))
            all_t = "/".join(f"{x:.0f}" for x in sorted(times))
            print(f"     {dur:>5.2f}s  中位 {med:>6.0f}ms  （三次 {all_t}ms）")
            print(f"             -> 「{out[:44]}」")

    # ── 本地 GPU ──────────────────────────────────────────────────────
    print()
    print("  ═══ ③ 本地 GPU large-v3-turbo ═══")
    model_dir = _asr_model_dir(asr_cfg)
    local = LocalAsrEngine(
        model=str(asr_cfg.get("model") or "large-v3-turbo"),
        device="cuda",
        compute_type="float16",
        model_dir=model_dir,
        language="zh",
        beam_size=1,
    )
    print(f"     模型目录：{model_dir}")

    local_results: list[tuple[str, float, str]] = []
    for text, audio, dur in samples:
        times = []
        out = ""
        for i in range(3):
            t = time.monotonic()
            try:
                tr = await local.transcribe(
                    (audio * 32768.0).astype(np.int16).tobytes(), 16000
                )
                times.append((time.monotonic() - t) * 1000)
                out = (tr.text or "").strip()
            except Exception as e:  # noqa: BLE001
                print(f"     ❌ 第 {i + 1} 次：{type(e).__name__}: {str(e)[:70]}")
                break
        if times:
            med = statistics.median(times)
            local_results.append((text, med, out))
            all_t = "/".join(f"{x:.0f}" for x in sorted(times))
            print(f"     {dur:>5.2f}s  中位 {med:>6.0f}ms  （三次 {all_t}ms）")
            print(f"             -> 「{out[:44]}」")

    # ── 对比 ──────────────────────────────────────────────────────────
    print()
    print("  ═══ ④ 对比 ═══")
    print(f"     {'原句 → 识别结果':<52} {'云端':>9} {'本地GPU':>9}  胜")
    for i, (text, _, _) in enumerate(samples):
        c = cloud_results[i] if i < len(cloud_results) else None
        l = local_results[i] if i < len(local_results) else None
        cs = f"{c[1]:.0f}ms" if c else "—"
        ls = f"{l[1]:.0f}ms" if l else "—"
        win = ""
        if c and l:
            win = "本地" if l[1] < c[1] else "云端"
        print(f"     {text[:26]:<28} {cs:>9} {ls:>9}  {win}")

    print()
    print("     识别文字（看准确率）：")
    for i, (text, _, _) in enumerate(samples):
        print(f"       原句：{text}")
        if i < len(cloud_results):
            print(f"       云端：{cloud_results[i][2]}")
        if i < len(local_results):
            print(f"       本地：{local_results[i][2]}")
        print()
    return 0


raise SystemExit(asyncio.run(main()))
