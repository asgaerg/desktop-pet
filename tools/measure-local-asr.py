"""量**本地** faster-whisper 的延迟 —— 和云端那 6~15 秒对比。

## 背景

云端（硅基流动 SenseVoiceSmall）实测波动 43 倍：235ms ~ 10203ms。
而本地模型**全都在盘上**（`pet-data/asr-models`，共 3.6 GB）：

    faster-whisper-large-v3-turbo   1542.9 MB
    faster-whisper-medium           1457.1 MB
    faster-whisper-small             461.1 MB
    faster-whisper-base              138.5 MB
    faster-whisper-tiny               72.0 MB

本地推理没有排队，延迟应当是**稳定**的。这个脚本量：
  · `faster_whisper` 装了没
  · 加载模型多久（一次性成本）
  · 转写 3 秒音频多久（每次成本）
  · 各档模型的速度对比

## 判据

  · 单次 < 1.5s  -> 比云端中位数还好，值得切
  · 单次 1.5-3s  -> 和云端中位数相当，但**稳定**（不排队），仍然更好
  · 单次 > 5s    -> 小档模型（small/base/tiny）才有意义
"""

from __future__ import annotations

import pathlib
import sys
import time

sys.path.insert(0, ".")
from tool_bootstrap import ROOT, setup  # noqa: E402

setup(verbose=False)

print("  ═══ ① faster_whisper 装了没 ═══")
try:
    import faster_whisper  # noqa: F401

    print(f"     ✅ 装了，版本 {getattr(faster_whisper, '__version__', '?')}")
except ImportError as e:
    print(f"     ❌ 没装：{e}")
    print("     （那本地路线就用不了，只能靠云端的超时+反馈兜住）")
    raise SystemExit(0)

MODELS_DIR = ROOT / "apps" / "desktop" / "pet-data" / "asr-models"

# 各档模型（用项目的目录名 -> 人话名）
CANDIDATES = [
    ("tiny", "models--Systran--faster-whisper-tiny"),
    ("base", "models--Systran--faster-whisper-base"),
    ("small", "models--Systran--faster-whisper-small"),
    ("large-v3-turbo", "models--mobiuslabsgmbh--faster-whisper-large-v3-turbo"),
]


def snapshot_dir(repo: str) -> pathlib.Path | None:
    """HuggingFace 缓存布局：<repo>/snapshots/<sha>/"""
    snaps = MODELS_DIR / repo / "snapshots"
    if not snaps.is_dir():
        return None
    for d in snaps.iterdir():
        if (d / "model.bin").is_file():
            return d
    return None


# 造一段 3 秒的测试音频（纯静音 —— 目的是量**速度**，不是准确率）
import numpy as np  # noqa: E402

audio = np.zeros(16000 * 3, dtype=np.float32)

print()
print("  ═══ ② 各档模型的加载 + 转写耗时 ═══")
print(f"     测试音频：3.0 秒（静音，只量速度）")
print()
print(f"     {'档位':<16} {'加载':>9} {'转写':>9}  说明")

results: list[tuple[str, float, float]] = []

from faster_whisper import WhisperModel  # noqa: E402

for name, repo in CANDIDATES:
    d = snapshot_dir(repo)
    if d is None:
        print(f"     {name:<16} {'—':>9} {'—':>9}  模型不在盘上")
        continue
    try:
        t0 = time.monotonic()
        model = WhisperModel(str(d), device="cpu", compute_type="int8")
        load_ms = (time.monotonic() - t0) * 1000

        # 第一次转写（含惰性初始化）
        t1 = time.monotonic()
        list(model.transcribe(audio, language="zh", beam_size=1)[0])
        first_ms = (time.monotonic() - t1) * 1000

        # 第二次（热态 —— 这才是用户每次按住的真实体验）
        t2 = time.monotonic()
        list(model.transcribe(audio, language="zh", beam_size=1)[0])
        warm_ms = (time.monotonic() - t2) * 1000

        results.append((name, load_ms, warm_ms))
        print(
            f"     {name:<16} {load_ms:>7.0f}ms {warm_ms:>7.0f}ms  "
            f"首次 {first_ms:.0f}ms"
        )
        del model
    except Exception as e:  # noqa: BLE001
        print(f"     {name:<16} {'—':>9} {'—':>9}  ❌ {type(e).__name__}: {str(e)[:40]}")

print()
if not results:
    print("  ❌ 一个都没跑起来")
    raise SystemExit(1)

print("  ═══ ③ 结论 ═══")
print(f"     云端实测（对照）：中位数 1235ms，但最慢 10203ms（波动 43×）")
print()
best = min(results, key=lambda r: r[2])
for name, load, warm in results:
    verdict = ""
    if warm < 1500:
        verdict = "✅ 比云端中位数还快"
    elif warm < 3000:
        verdict = "⚠️ 与云端中位数相当，但**稳定**（不排队）"
    else:
        verdict = "❌ 太慢"
    print(f"     {name:<16} 热态 {warm:>7.0f}ms  {verdict}")
print()
print(f"     最快的是 **{best[0]}**（{best[2]:.0f}ms）")
print()
print("     ⚠️ 但注意：本地只有**速度**优势，**准确率**要单独比。")
print("        large-v3-turbo 准确率应当不差于 SenseVoiceSmall，")
print("        small/base/tiny 会明显更差（中文尤其）。")
print("        真要切，建议 large-v3-turbo；嫌慢再退到 small 并实测准确率。")
