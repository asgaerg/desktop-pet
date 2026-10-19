#!/usr/bin/env python
"""
faster-whisper **实测基准**：延迟、精度、显存占用。

## 为什么必须实测而不是查文档估算

"延迟多少"这个问题，公开基准给的都是 **RTF（实时率）** 这种相对数，
而我们真正要知道的是**用户说完一句话之后，等多久看到文字**。那个数取决于：
  ① 模型大小与量化方式
  ② 显卡（这台是 RTX 2070 8GB）
  ③ 是否走 GPU、首次是否要加载模型
  ④ 音频长度（短句固定开销占比大）

公开基准覆盖不了 ③④ 的组合，所以这里全部量出来。

## 用法

    python tools/bench-asr.py                  # 默认测 small + medium
    python tools/bench-asr.py --models tiny,base,small
    python tools/bench-asr.py --models large-v3-turbo --device cuda --compute float16

## ⚠️ 模型缓存放**项目内**（`pet-data/asr-models`）

不写进 `~/.cache/huggingface` —— 那是用户目录，本项目有一条明确约定：
**能在项目内解决的，不要去动用户目录**（也与"数据全部留在本地项目"一致）。
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import wave
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tool_bootstrap import ROOT, setup  # noqa: E402

setup(verbose=False)

BENCH_DIR = ROOT / "apps" / "desktop" / "pet-data" / "asr-bench"
MODEL_DIR = ROOT / "apps" / "desktop" / "pet-data" / "asr-models"

# 让 huggingface_hub 把模型下到项目内
os.environ.setdefault("HF_HOME", str(MODEL_DIR))
os.environ.setdefault("HUGGINGFACE_HUB_CACHE", str(MODEL_DIR / "hub"))
# 国内直连 huggingface.co 通常不通，走镜像
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")


def prepare_cuda_dlls() -> list[str]:
    """
    把 pip 装的 NVIDIA 运行库加进 **DLL 搜索路径**。

    ## ⚠️ 为什么必须显式加（Windows 的坑）

    `pip install nvidia-cublas-cu12` 会把 `cublas64_12.dll` 放到

        .venv/Lib/site-packages/nvidia/cublas/bin/

    而 **Windows 默认不从那里找 DLL**（Linux 的 rpath 机制在这边不存在）。
    于是 ctranslate2 报：

        RuntimeError: Library cublas64_12.dll is not found or cannot be loaded

    这个报错**看起来像没装**，实际是**装了但找不到** —— 很容易让人去重装一遍。
    修法是 `os.add_dll_directory()`（Python 3.8+ 的正确做法；往 PATH 里塞也行，
    但 `add_dll_directory` 才是官方途径，且改动只影响本进程）。

    ## ✅ 实测：**不需要 cuDNN**

    一开始我以为要连 cuDNN 一起装（743MB）。实测只装 cuBLAS 就能跑：

        >>> WhisperModel('tiny', device='cuda', compute_type='float16')
        ✅ CUDA 加载成功

    原因：`faster-whisper` 用的是 **CTranslate2**，它有自己实现的算子，
    不依赖 cuDNN 的那套卷积库。**省了 743MB 下载** ——
    这也是"先装最少的东西试一次"比"照着文档装全套"更值的地方。
    """
    base = Path(sys.prefix) / "Lib" / "site-packages" / "nvidia"
    if not base.is_dir():
        return []
    dirs = [str(d / "bin") for d in sorted(base.iterdir()) if (d / "bin").is_dir()]
    for d in dirs:
        try:
            os.add_dll_directory(d)
        except OSError:
            pass  # 非 Windows 或目录已失效
    if dirs:
        os.environ["PATH"] = os.pathsep.join(dirs) + os.pathsep + os.environ.get("PATH", "")
    return dirs

TARGET_RATE = 16000  # 产品链路的标准采样率（见 voice.py 的 SAMPLE_RATE）


def load_wav_16k(path: Path) -> tuple[Any, float]:
    """
    读 wav 并重采样到 16kHz 单声道，返回 (numpy float32 数组, 时长秒)。

    ## ⚠️ 必须返回 **numpy 数组**，不能是 list（实测踩到）

    `faster_whisper.transcribe()` 收到非数组的序列时，会把它当成**文件路径**
    交给 `decode_audio` -> `av.open(...)`，于是报出一个完全无关的错：

        TypeError: open() got an unexpected keyword argument 'metadata_errors'

    （那是 `faster-whisper 1.2.1` 与 `av 19.0.1` 的版本不兼容。
     我们不该走那条路 —— 产品链路里音频是**前端采好 16kHz PCM** 送进来的，
     本来就是数组，不经过任何解码器。）

    传 `np.ndarray` 会跳过整个解码分支 —— 既避开那个 bug，
    又**更接近产品真实路径**。

    ## ⚠️ 为什么手动重采样而不是用 av / librosa

    产品里音频是**前端按 16kHz 采好**再送过来的（`voice.py` 的
    `SAMPLE_RATE = 16000`），Python 侧拿到的已经是 16kHz PCM。
    这里也用 16kHz 喂进去，量的才是真实开销。

    重采样用线性插值：对"测延迟"够用（Whisper 内部还要再做一次 mel 变换），
    但**精度会有轻微损失**，所以下面的识别文本仅供参考。
    """
    with wave.open(str(path)) as w:
        sr = w.getframerate()
        ch = w.getnchannels()
        sw = w.getsampwidth()
        raw = w.readframes(w.getnframes())

    if sw != 2:
        raise ValueError(f"只支持 16bit wav，实际 {sw * 8}bit")

    import numpy as np

    samples = np.frombuffer(raw, dtype="<i2").astype(np.float32)
    if ch > 1:
        samples = samples[::ch]

    if sr != TARGET_RATE:
        # 线性插值重采样
        ratio = TARGET_RATE / sr
        out_len = int(len(samples) * ratio)
        idx = np.arange(out_len, dtype=np.float64) / ratio
        lo = np.floor(idx).astype(np.int64)
        hi = np.minimum(lo + 1, len(samples) - 1)
        frac = (idx - lo).astype(np.float32)
        samples = samples[lo] * (1 - frac) + samples[hi] * frac

    samples = samples / 32768.0
    return np.ascontiguousarray(samples, dtype=np.float32), len(samples) / TARGET_RATE


def main() -> int:
    ap = argparse.ArgumentParser(description="faster-whisper 实测基准")
    ap.add_argument("--models", default="small,medium", help="逗号分隔，默认 small,medium")
    ap.add_argument("--device", default="cuda", choices=["cuda", "cpu"])
    ap.add_argument("--compute", default="", help="留空则按设备自动选（GPU=float16 / CPU=int8）")
    ap.add_argument("--language", default="zh")
    args = ap.parse_args()

    wavs = sorted(BENCH_DIR.glob("*.wav"))
    if not wavs:
        print(f"  ❌ 没有测试音频：{BENCH_DIR}")
        print("     先生成：见 docs 里 ASR 那节（用 Windows SAPI 合成）")
        return 1

    print("=" * 78)
    print("faster-whisper 实测基准")
    print("=" * 78)
    print()
    print(f"  测试音频：{len(wavs)} 条（{BENCH_DIR.name}/）")
    print(f"  模型缓存：{MODEL_DIR}")
    print(f"  HF 镜像 ：{os.environ.get('HF_ENDPOINT')}")
    print(f"  设备     ：{args.device}")
    dlls = prepare_cuda_dlls()
    if args.device == "cuda":
        print(f"  CUDA DLL ：{len(dlls)} 个目录" + (f"（{', '.join(Path(d).parent.name for d in dlls)}）" if dlls else "（没找到，CUDA 会失败）"))
    print()

    from faster_whisper import WhisperModel

    compute = args.compute or ("float16" if args.device == "cuda" else "int8")

    for name in [m.strip() for m in args.models.split(",") if m.strip()]:
        print("─" * 78)
        print(f"  模型 {name}（{args.device} / {compute}）")
        print("─" * 78)

        # ① 加载耗时（含首次下载）—— 这一项对桌宠很关键：
        #    如果每次启动都要几十秒，那"打开就能说"就不成立
        started = time.monotonic()
        try:
            model = WhisperModel(name, device=args.device, compute_type=compute, download_root=str(MODEL_DIR))
        except Exception as exc:  # noqa: BLE001
            print(f"     ❌ 加载失败：{type(exc).__name__}: {str(exc)[:160]}")
            print()
            continue
        load_s = time.monotonic() - started
        print(f"     加载/下载耗时：{load_s:.1f}s")

        # ② 逐条识别（第一条含预热开销，单独标出来）
        print()
        print(f"     {'文件':<12}{'音频':>8}{'识别':>9}{'RTF':>8}   文本")
        print(f"     {'-' * 70}")
        total_audio = 0.0
        total_infer = 0.0
        for idx, wav in enumerate(wavs):
            audio, dur = load_wav_16k(wav)
            started = time.monotonic()
            segments, info = model.transcribe(
                audio,
                language=args.language,
                beam_size=1,  # 贪心：按住说话要的是快，不是极致准
                vad_filter=False,  # 端点检测我们已经在前端做了，别做两遍
                condition_on_previous_text=False,  # 短句之间不该互相影响
            )
            text = "".join(s.text for s in segments).strip()
            infer_s = time.monotonic() - started

            total_audio += dur
            total_infer += infer_s
            rtf = infer_s / dur if dur else 0
            label = wav.stem + ("*" if idx == 0 else "")
            print(f"     {label:<12}{dur:>7.2f}s{infer_s * 1000:>8.0f}ms{rtf:>8.3f}   {text[:34]}")

        print()
        if total_audio:
            print(f"     合计：音频 {total_audio:.2f}s / 识别 {total_infer * 1000:.0f}ms"
                  f"  ->  平均 RTF {total_infer / total_audio:.3f}")
            print(f"     平均延迟 {total_infer / len(wavs) * 1000:.0f}ms/句")
        print("     （带 * 的是第一条，含预热；真实使用中模型常驻，看后面的数）")
        print()

        del model

    print("=" * 78)
    print("怎么读这些数")
    print("=" * 78)
    print()
    print("  RTF = 识别耗时 / 音频时长。**小于 1 就是比实时快**。")
    print("  用户感知的延迟 = 识别延迟 + 断句等待（静音判定 500ms）+ 网络。")
    print("  所以「按住说话」模式下（松手即送）比免提模式少等那 500ms。")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
