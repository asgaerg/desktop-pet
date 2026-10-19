"""实测 `edge-tts` —— 中文音色质量 + 延迟。

## 为什么它值得先试（针对本项目的具体痛点）

现状：

  · `tts.provider: 'openai'`（硅基流动 CosyVoice2）—— **要花钱、要联网**
  · 本地 windows(SAPI) 路线 —— **在这台机器上坏了**（枚举得出音色、取不到）
  · `onnxruntime` 只有 **CPU** provider —— piper/kokoro 那种 ONNX 路线会吃 CPU

而 `edge-tts`：

  · **纯 Python 包**，不需要模型、不需要编译、不需要 GPU
  · 走微软 Edge 的在线接口 —— **中文音色质量很好**（晓晓/云希等一整套）
  · **免费、无需 API key**
  · 实测端点可达：`speech.platform.bing.com` TLS 1.3 握手 296ms

**代价：要联网** —— 所以它不是"真本地"，而是"把付费云端换成免费云端"。
但如果目标是"**不花钱 + 中文好听**"，它是性价比最高的一步。

## 这个脚本测什么

  ① 装得上吗（pip install）
  ② 有哪些中文音色
  ③ **合成要多久**（这是体验的关键 —— 本项目对延迟很敏感）
  ④ 产出什么格式（**mp3 还是 pcm** 很重要：本项目靠 PCM 算口型包络，
     mp3 要先用 PyAV 解码 —— 代码里已经有那条路）
"""

from __future__ import annotations

import asyncio
import pathlib
import subprocess
import sys
import time

sys.path.insert(0, ".")
from tool_bootstrap import ROOT, setup  # noqa: E402

setup(verbose=False)

PY = str(ROOT / ".venv" / "Scripts" / "python.exe")
TMP = ROOT / "apps" / "desktop" / "pet-data" / "ptt-tmp"
TMP.mkdir(parents=True, exist_ok=True)

TEXT = "桌面宠物的语音换成本地合成啦，这样就不用花钱了。"

print("  ═══ ① 装 edge-tts ═══")
t0 = time.monotonic()
r = subprocess.run(
    [PY, "-m", "pip", "install", "--quiet", "--disable-pip-version-check", "edge-tts"],
    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600,
)
print(f"    exit={r.returncode}  耗时 {time.monotonic() - t0:.0f}s")
tail = ((r.stdout or "") + (r.stderr or "")).strip().split("\n")[-6:]
for l in tail:
    if l.strip():
        print(f"      {l[:110]}")
if r.returncode != 0:
    print("    ❌ 装不上，后面的测试跳过")
    raise SystemExit(1)

SCRIPT = r'''
import asyncio, pathlib, sys, time
import edge_tts

TEXT = sys.argv[1]
OUT = pathlib.Path(sys.argv[2])

async def main():
    # ① 列中文音色
    voices = await edge_tts.list_voices()
    zh = [v for v in voices if str(v.get("Locale", "")).startswith("zh-")]
    print("VOICES " + str(len(zh)))
    for v in zh[:14]:
        print("  " + v["ShortName"] + "  " + str(v.get("Gender")) + "  " + str(v.get("Locale")))

    # ② 合成 + 计时（挑最常用的中文女声/男声各一次）
    for name in ("zh-CN-XiaoxiaoNeural", "zh-CN-YunxiNeural"):
        t0 = time.monotonic()
        try:
            comm = edge_tts.Communicate(TEXT, name)
            audio = b""
            async for chunk in comm.stream():
                if chunk["type"] == "audio":
                    audio += chunk["data"]
            dt = (time.monotonic() - t0) * 1000
            p = OUT / ("edge-" + name + ".mp3")
            p.write_bytes(audio)
            print(f"SYNTH {name} {len(audio)} bytes {dt:.0f}ms -> {p.name}")
        except Exception as e:
            print(f"SYNTH-FAIL {name} {type(e).__name__}: {str(e)[:120]}")

asyncio.run(main())
'''

sjs = TMP / "_edge_probe.py"
sjs.write_text(SCRIPT, encoding="utf-8")

print()
print("  ═══ ② 列中文音色 + 合成（量延迟）═══")
r = subprocess.run(
    [PY, str(sjs), TEXT, str(TMP)],
    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
)
for l in (r.stdout or "").split("\n"):
    if l.strip():
        print(f"    {l.strip()[:112]}")
if (r.stderr or "").strip():
    print("    stderr:", r.stderr.strip()[:400])

print()
print("  ═══ ③ 产物 ═══")
made = sorted(TMP.glob("edge-*.mp3"))
for p in made:
    print(f"    {p.name}  {p.stat().st_size // 1024}KB")

print()
print("  ═══ ④ 和现有云端对比（本项目的取舍表）═══")
try:
    import json

    cfg = ROOT / "config" / "persona.yaml"
    print(f"    当前配置里 tts.provider 见 {cfg.name}")
except Exception:  # noqa: BLE001
    pass
print("""
    ┌──────────────┬────────┬────────┬──────────┬──────────────┐
    │ 方案         │ 花钱   │ 联网   │ 口型包络 │ 中文质量     │
    ├──────────────┼────────┼────────┼──────────┼──────────────┤
    │ 硅基流动(现) │ 要     │ 要     │ ✖ mp3    │ 很好         │
    │ edge-tts     │ 免费   │ 要     │ ✖ mp3    │ 很好（晓晓） │
    │ 本地 SAPI    │ 免费   │ 不要   │ ✅ pcm   │ 一般（本机坏）│
    │ piper        │ 免费   │ 不要   │ ✅ pcm   │ 一般         │
    │ kokoro-onnx  │ 免费   │ 不要   │ ✅ pcm   │ 好（CPU 慢） │
    └──────────────┴────────┴────────┴──────────┴──────────────┘
""")

for p in (sjs,):
    try:
        p.unlink()
    except OSError:
        pass
