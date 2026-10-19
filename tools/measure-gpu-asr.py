"""量**GPU** 上的 faster-whisper 延迟 —— 和 CPU / 云端对比。

## 硬件

    NVIDIA GeForce RTX 2070, 8192 MiB, 驱动 596.21（CUDA 13.2）
    ctranslate2 4.8.2，get_cuda_device_count() = 1
    CUDA 支持的计算类型：float16 / float32 / int8 / int8_float16 / int8_float32

## CPU 的结果（对照，已测）

    tiny             5891ms
    base            11688ms
    small           15563ms
    large-v3-turbo  13093ms

全部不可用 —— 而云端中位数 1235ms（但波动 43×，最慢 10s）。

## 这个脚本要回答

GPU 上 large-v3-turbo 热态要多久？
  · < 800ms  -> **比云端还快且稳定**，应当切过去
  · < 2000ms -> 与云端中位数相当但**不排队**，也值得切
  · > 3000ms -> GPU 也救不了，只能靠超时+反馈

## ⚠️ 用 3 秒静音测速度可以，但**准确率必须另外验**

速度对音频内容不敏感；准确率完全相反。所以这个脚本**只报速度**，
最后会提示怎么做准确率对比。
"""

from __future__ import annotations

import pathlib
import sys
import time

sys.path.insert(0, ".")
from tool_bootstrap import ROOT, setup  # noqa: E402

setup(verbose=False)

import numpy as np  # noqa: E402

MODELS_DIR = ROOT / "apps" / "desktop" / "pet-data" / "asr-models"

CANDIDATES = [
    ("tiny", "models--Systran--faster-whisper-tiny"),
    ("base", "models--Systran--faster-whisper-base"),
    ("small", "models--Systran--faster-whisper-small"),
    ("large-v3-turbo", "models--mobiuslabsgmbh--faster-whisper-large-v3-turbo"),
]

# 音频时长也要覆盖 —— 用户可能按住说很久（实测最长 11.94 秒）
DURATIONS = [3.0, 8.0]


def snapshot_dir(repo: str) -> pathlib.Path | None:
    snaps = MODELS_DIR / repo / "snapshots"
    if not snaps.is_dir():
        return None
    for d in snaps.iterdir():
        if (d / "model.bin").is_file():
            return d
    return None


from faster_whisper import WhisperModel  # noqa: E402

print("  ═══ GPU 上的加载 + 转写 ═══")
print("     计算类型 float16（RTX 2070 支持，显存占用约模型大小的一半）")
print()
print(f"     {'档位':<16} {'音频':>6} {'加载':>8} {'首次':>8} {'热态':>8}")

results: dict[tuple[str, float], float] = {}

for name, repo in CANDIDATES:
    d = snapshot_dir(repo)
    if d is None:
        print(f"     {name:<16} {'—':>6} 模型不在盘上")
        continue
    try:
        t0 = time.monotonic()
        model = WhisperModel(str(d), device="cuda", compute_type="float16")
        load_ms = (time.monotonic() - t0) * 1000
    except Exception as e:  # noqa: BLE001
        print(f"     {name:<16} {'—':>6} ❌ 上不了 GPU：{type(e).__name__}: {str(e)[:46]}")
        continue

    for dur in DURATIONS:
        audio = np.zeros(int(16000 * dur), dtype=np.float32)
        try:
            t1 = time.monotonic()
            list(model.transcribe(audio, language="zh", beam_size=1)[0])
            first_ms = (time.monotonic() - t1) * 1000

            # 热态：再跑两次取较好值（GPU 首次会有上下文初始化）
            warm = []
            for _ in range(2):
                t2 = time.monotonic()
                list(model.transcribe(audio, language="zh", beam_size=1)[0])
                warm.append((time.monotonic() - t2) * 1000)
            warm_ms = min(warm)

            results[(name, dur)] = warm_ms
            print(
                f"     {name:<16} {dur:>5.1f}s {load_ms:>7.0f}ms "
                f"{first_ms:>7.0f}ms {warm_ms:>7.0f}ms"
            )
        except Exception as e:  # noqa: BLE001
            print(f"     {name:<16} {dur:>5.1f}s ❌ 转写失败：{type(e).__name__}: {str(e)[:40]}")

    del model

print()
print("  ═══ 对比 ═══")
print("     云端（硅基流动）：中位数 1235ms，最慢 10203ms（波动 43×）")
print("     CPU 本地        ：tiny 5891ms / large-v3-turbo 13093ms（不可用）")
print()
if not results:
    print("  ❌ GPU 上一个都没跑起来")
    raise SystemExit(1)

for (name, dur), ms in sorted(results.items()):
    if ms < 800:
        v = "✅ 比云端中位数还快，而且**不排队**"
    elif ms < 2000:
        v = "✅ 与云端中位数相当，但**稳定**"
    elif ms < 3000:
        v = "⚠️ 可用但偏慢"
    else:
        v = "❌ 太慢"
    print(f"     {name:<16} {dur:.0f}s 音频 -> {ms:>7.0f}ms   {v}")

print()
best = min(results.items(), key=lambda kv: kv[1])
print(f"     最快：**{best[0][0]}**（{best[0][1]:.0f}s 音频 -> {best[1]:.0f}ms）")
print()
print("  ═══ ⚠️ 准确率必须单独验 ═══")
print("     这个脚本用**静音**测速度 —— 速度对内容不敏感，准确率完全相反。")
print("     真要切过去，需要同一段真人语音分别喂云端和本地，逐字对比。")
print("     现成工具：`tools/check-voice-flow.py`（逐帧喂真实音频）。")
