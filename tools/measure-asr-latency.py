"""直接量硅基流动 ASR 的延迟：同一个音频多打几次，看波动。

## 为什么要单独量

从日志看识别耗时 594ms / 2500ms / 6531ms / 15547ms —— **波动 26 倍**。
这不像"模型慢"，更像"**服务端排队**"。这个区别决定怎么修：

  · 模型本身慢  -> 换更小的模型
  · 服务端排队  -> 换模型没用，得加超时（已加）或换供应商
  · 冷启动      -> 第一次慢、后续快，那就预热

## 怎么量

项目里有 `build_voice_pipeline`（真正的工厂），但这里只要 ASR 引擎，
所以直接建 `OpenAIAsrEngine`。

音频**合成**一段：用 TTS 生成中文语音当输入。这样量的是**纯 ASR 耗时**
（不含采集、端点检测）。

⚠️ 合成一次、反复用 —— 避免把 TTS 耗时混进来。
"""

from __future__ import annotations

import asyncio
import pathlib
import statistics
import sys
import time

sys.path.insert(0, "apps/brain")
sys.path.insert(0, ".")

from tool_bootstrap import ROOT, setup  # noqa: E402

setup(verbose=False)

from pet.config import load_config  # noqa: E402
from pet.voice import OpenAIAsrEngine  # noqa: E402

CONFIG = ROOT / "config" / "persona.yaml"
TEST_TEXT = "今天天气不错，我们出去走走吧。"


async def main() -> int:
    cfg = load_config(CONFIG)
    asr_cfg = dict(cfg.providers.get("asr") or {})

    base = str(asr_cfg.get("cloud_base_url") or "").rstrip("/")
    model = str(asr_cfg.get("cloud_model") or "")
    key = ""
    try:
        from pet.voice import _env_or, vendor_from_url

        key = _env_or(
            asr_cfg.get("cloud_api_key_env"),
            cfg.api_key(),
            vendor=vendor_from_url(base),
            config=cfg,
        )
    except Exception as e:  # noqa: BLE001
        print(f"  ❌ 取密钥失败：{e}")
        return 1

    print(f"  端点  : {base}/audio/transcriptions")
    print(f"  模型  : {model}")
    print(f"  密钥  : {key[:10]}…")
    print()

    asr = OpenAIAsrEngine(base_url=base, model=model, api_key=key)

    # ── 造一段音频：用 TTS 合成 ────────────────────────────────────────
    #
    # ⚠️ 必须 pcm —— ASR 端点收 raw PCM。TTS 若返回 mp3 就没法直接喂，
    #    那种情况如实说，不硬凑。
    print("  ═══ 准备音频 ═══")
    payload: bytes | None = None
    tts_cfg = dict(cfg.providers.get("tts") or {})
    if str(tts_cfg.get("provider") or "") != "none":
        try:
            from pet.voice import WindowsSapiTtsEngine  # 不可能用（本环境 SAPI 坏）

            raise ImportError("skip")
        except Exception:  # noqa: BLE001
            pass
        try:
            from pet.voice import build_voice_pipeline

            print("     （用 build_voice_pipeline 拿 TTS 引擎）")
            pipe = build_voice_pipeline(cfg) if callable(build_voice_pipeline) else None
            tts = getattr(pipe, "tts", None) if pipe else None
            if tts is not None:
                p, fmt = await tts.synthesize(TEST_TEXT)
                print(f"     TTS 返回 {fmt}，{len(p)} 字节")
                if fmt == "pcm":
                    payload = p
                else:
                    print(f"     ⚠️ 不是 pcm（{fmt}）—— 需要解码才能喂 ASR，跳过测量")
        except Exception as e:  # noqa: BLE001
            print(f"     ⚠️ 造音频失败：{type(e).__name__}: {str(e)[:70]}")

    if payload is None:
        # 退路：合成一段**静音**也能测出"网络+服务端"的耗时下界
        # （识别结果会是空文本，但耗时数据有效）
        print("     退路：用 3 秒静音 PCM 测网络+服务端耗时（结果会是空文本）")
        payload = b"\x00\x00" * (16000 * 3)

    print(f"     音频：{len(payload)} 字节 ≈ {len(payload) / 32000:.1f} 秒（16k/16bit 单声道）")
    print()

    print("  ═══ 连打 6 次 ═══")
    times: list[float] = []
    for i in range(6):
        t = time.monotonic()
        try:
            tr = await asr.transcribe(payload, 16000)
            ms = (time.monotonic() - t) * 1000
            times.append(ms)
            txt = (tr.text or "").strip()[:24] or "（空）"
            err = f"   ⚠️ {tr.error}" if tr.error else ""
            print(f"     第 {i + 1} 次：{ms:>7.0f}ms   「{txt}」{err}")
        except Exception as e:  # noqa: BLE001
            ms = (time.monotonic() - t) * 1000
            print(f"     第 {i + 1} 次：{ms:>7.0f}ms   ❌ {type(e).__name__}: {str(e)[:56]}")
        await asyncio.sleep(0.4)

    if not times:
        print("  ❌ 全部失败")
        return 1

    print()
    print("  ═══ 统计 ═══")
    print(f"     次数   {len(times)}")
    print(f"     最快   {min(times):.0f}ms")
    print(f"     最慢   {max(times):.0f}ms")
    print(f"     中位数 {statistics.median(times):.0f}ms")
    print(f"     平均   {statistics.mean(times):.0f}ms")
    if len(times) > 1:
        spread = max(times) / max(1.0, min(times))
        print(f"     波动   最慢/最快 = {spread:.1f}×")
        print()
        if spread > 4:
            print("  🔴 **波动大（>4×）—— 服务端排队的特征**")
            print("     换模型没用；该做的是超时 + 明确反馈（已加），或换供应商。")
        elif min(times) > 4000:
            print("  ⚠️ 稳定但慢 —— 模型本身重，考虑换更小的。")
        else:
            print("  ✅ 延迟稳定且可接受")
            print("     那么日志里 6.5s / 15.5s 是**偶发**的，不是常态。")
    return 0


raise SystemExit(asyncio.run(main()))
