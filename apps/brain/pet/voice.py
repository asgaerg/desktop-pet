"""
语音链路（P3）—— 端点检测、打断、状态机，以及可插拔的 ASR / TTS。

架构决定：**音频 I/O 不在 Python 侧**。

理由很实际：Electron 渲染层的 Web Audio API 原生支持麦克风采集与音频播放，
Chromium 已经把这些跑起来了；而 Python 侧要拿到同等能力得依赖 sounddevice /
pyaudio 这类需要编译的包，本机一个都没有，装起来还容易在换机器时炸掉。
所以约定是：

    Electron 渲染层  ——  采集麦克风 PCM、播放 TTS 音频、上报播放进度
          ↕ 二进制帧 + 控制消息
    Python sidecar   ——  端点检测、ASR、TTS 合成、打断判定、状态机

这样 Python 侧只处理 **PCM 字节**，纯标准库就能算，也就能用合成音频完整测试。
本模块里唯一"碰硬件"的东西是 `AudioInput` / `AudioOutput` 两个协议，
真实现由前端提供，测试用假实现。

这一层要解决的核心问题不是"识别文字"，而是**什么时候该开始听、什么时候算说完了、
被打断时怎么办**。这三件事做错，语音对话的体验就彻底崩掉：

  · 说话中间停顿一下就被当成说完了  -> 句子被切碎，回复答非所问
  · 环境噪音被当成说话              -> 宠物自己跟自己聊个没完
  · 宠物在说话时用户插话却停不下来  -> 抢话，最令人烦躁的失败模式
"""

from __future__ import annotations

import asyncio
import audioop
import json
import logging
import math
import os
import struct
import subprocess
import sys
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional, Protocol

from .bus import Bus
from .config import Config
from . import netutil
from .protocol import clamp01

log = logging.getLogger("pet.voice")

# 仓库根：apps/brain/pet/voice.py -> 上溯三级。
# 与 memory2.py 的 `_project_root()` 同一口径（那边是 parents[3]，
# 因为它算的是 `apps/brain/pet/x.py` 里再往上一层的用法；这里直接用
# `parents[3]` 会到仓库外，所以数清楚：pet -> brain -> apps -> 仓库根 = parents[3]）
PROJECT_ROOT = Path(__file__).resolve().parents[3]


# --------------------------------------------------------------------------- #
# 音频基本单位
# --------------------------------------------------------------------------- #

SAMPLE_RATE = 16000  # 语音识别的标准采样率；Whisper 与多数云 ASR 都按它最优
SAMPLE_WIDTH = 2  # 16 bit
CHANNELS = 1
FRAME_MS = 20  # 一帧 20ms：端点检测的经典粒度，兼顾延迟与稳定性

"""
引导 Whisper **用简体输出**的提示词（用户实测反馈 2026-10-08：
「转译语音竟然是繁体字」）。

放在模块级常量而不是各处硬编码：引擎默认用它、工厂也用它，
**一处定义**才不会漂移。

⚠️ 光靠它不保险（模型可能不听，唱歌/口音/噪声大时尤其），
所以还有 `T2S` 后处理兜底 —— 两道一起才稳。
"""
SIMPLIFIED_PROMPT = "以下是普通话的句子，请用简体中文转写。"
BYTES_PER_FRAME = SAMPLE_RATE * SAMPLE_WIDTH * CHANNELS * FRAME_MS // 1000

'''
"按住说话"松手时，音频短于这个时长就**当作误触，不送识别**（2026-10-10 加）。

## 为什么需要（用户报「我发出去的不对啊」）

聊天记录里出现「请用简体中文转写。」「优优独播剧场——YoYo Television
Series Exclusive」—— **全是 ASR 幻觉**，被当成用户说的话发给了大脑。
实测那批音频只有 **0.02~0.08 秒**。

## 为什么是 0.3 秒

  · 正常说话至少几百毫秒（说一个字都接近 200ms），不会误伤
  · 而 0.02~0.08 秒必然是"按了两下、根本没说话"——
    切换式 PTT 下这非常容易发生（想取消、按错、连着按两下）

⚠️ 只对 `force_end()` 那条路生效。`feed()` 那条走静音判定，
本来就有 `min_speech_ms` 兜着。
'''
MIN_FORCED_DURATION_S = 0.3


@dataclass(slots=True)
class AudioFrame:
    """固定时长的 PCM 帧。所有计算都基于它，保证端点检测的时序可预测。"""

    pcm: bytes
    captured_at: float = field(default_factory=time.monotonic)

    @property
    def samples(self) -> int:
        return len(self.pcm) // SAMPLE_WIDTH

    @property
    def duration_s(self) -> float:
        return self.samples / float(SAMPLE_RATE)

    def rms(self) -> float:
        """
        均方根能量，归一化到 0~1。

        用标准库 audioop 算：比自己写循环快得多，且不必引入 numpy
        （Python 侧不装 numpy，是这套 sidecar 的零依赖原则的一部分）。
        """
        if not self.pcm:
            return 0.0
        value = audioop.rms(self.pcm, SAMPLE_WIDTH)
        return min(1.0, value / 32768.0)

    def peak(self) -> float:
        if not self.pcm:
            return 0.0
        return min(1.0, audioop.max(self.pcm, SAMPLE_WIDTH) / 32768.0)

    def zero_crossing_rate(self) -> float:
        """
        过零率。用来区分"人声"与"稳态噪声/电流声"：
        人声有过零率波动，稳态噪声则非常规则。单独用能量判不准的场景靠它补。
        """
        if len(self.pcm) < 4:
            return 0.0
        return audioop.cross(self.pcm, SAMPLE_WIDTH) / max(1, self.samples)


def silence(ms: int = FRAME_MS) -> bytes:
    return b"\x00\x00" * (SAMPLE_RATE * ms // 1000)


def tone(ms: int = FRAME_MS, freq: float = 220.0, amplitude: float = 0.3) -> bytes:
    """合成一段正弦波。测试里当"人声"用（能量与过零率都像浊音）。"""
    count = SAMPLE_RATE * ms // 1000
    peak = int(32767 * clamp01(amplitude, 0.3))
    out = bytearray()
    for index in range(count):
        out += struct.pack("<h", int(peak * math.sin(2 * math.pi * freq * index / SAMPLE_RATE)))
    return bytes(out)


def noise(ms: int = FRAME_MS, amplitude: float = 0.02, seed: int = 1) -> bytes:
    """合成一段伪随机噪声。测试里当"环境底噪"用。"""
    import random

    rng = random.Random(seed)
    count = SAMPLE_RATE * ms // 1000
    peak = int(32767 * clamp01(amplitude, 0.02))
    return b"".join(struct.pack("<h", rng.randint(-peak, peak)) for _ in range(count))


def frame_from(pcm: bytes) -> AudioFrame:
    return AudioFrame(pcm=pcm)


# --------------------------------------------------------------------------- #
# 语音活动检测
# --------------------------------------------------------------------------- #


@dataclass
class VadConfig:
    """
    端点检测参数。这些数字直接决定语音体验，所以每个都有明确意图：

    · `start_frames`：连续多少帧超阈值才算"开始说话"。防的是敲键盘、爆音这类
      瞬时噪声 —— 设小了会频繁误触发。
    · `end_silence_ms`：安静多久算"说完了"。**这是最影响体验的一个数**：
      太小会把句子切碎（用户换气就以为说完了），太大则每句话都要多等。
      500ms 是通话类应用的常见取值，对"说完停一下"很自然。
    · `min_speech_ms`：短于这个长度的"语音"直接丢弃（咳嗽、桌子响）。
    · `max_utterance_ms`：保险丝，防止对着麦克风长期出声导致缓冲区无限增长。
    """

    # 相对噪声底的能量倍数。用**相对**而不是绝对阈值：不同麦克风增益差很多，
    # 绝对阈值换台机器就失效，这是语音功能最常见的"在我这儿好好的"故障。
    #
    # 🔴 2026-10-28 从 3.0 降到 2.0。
    #
    # 实测（用户全屏打游戏时"说了话没反应"）：
    #
    #     环境底噪 energy ≈ 0.0247
    #     说话时 energy   ≈ 0.028     <- 只比底噪高 13%
    #     阈值（3.0x）     = 0.049     <- 底噪的 2 倍，说话根本够不到
    #
    # 根因有两层：① 麦克风选错了设备（已在前端修回退）；
    # ② 板载麦增益低，整条信号被压扁 —— 前端已加自动增益（`_applyAgc`）。
    #
    # 这里是**第三道保险**：即使增益还没收敛（AGC 需要几十秒），
    # 2.0 倍也比 3.0 更容易在早期就触发。
    #
    # ⚠️ 不能降到 1.5 以下：那会让环境噪声自己触发"语音"，
    #    表现为"宠物一直以为你在说话"。2.0 是"比底噪明显高一截"的下限。
    energy_ratio: float = 2.0
    # 绝对下限，避免在极安静环境里把电流噪声当成语音
    min_energy: float = 0.010
    # 噪声底自适应速度（每帧向当前能量靠拢的比例，仅在判为静音时更新）
    noise_adapt: float = 0.05
    start_frames: int = 3
    end_silence_ms: int = 500
    min_speech_ms: int = 250
    max_utterance_ms: int = 30_000
    # 过零率上限：高到离谱的多半是稳态噪声而不是语音
    max_zcr: float = 0.6


class VadState(str, Enum):
    SILENCE = "silence"
    SPEECH = "speech"


@dataclass
class VadResult:
    """一帧的判定结果。带上中间量，便于排查"为什么没识别到/为什么一直触发"。"""

    is_speech: bool
    energy: float
    noise_floor: float
    threshold: float
    zcr: float
    reason: str = ""


class VoiceActivityDetector:
    """
    基于能量 + 自适应噪声底的端点检测。

    为什么不用现成的 VAD 库：本机没有任何 VAD 依赖（webrtcvad / silero 都没有），
    而它们都需要编译或下载模型。能量法配合**自适应噪声底**足够应付"用户对着
    自己的麦克风说话"这个场景，而且完全可离线测试、行为可解释。

    局限要说清楚：它对**音乐、键盘声、多人说话**的区分能力远不如
    silero-vad。所以 `VoiceSession` 把 VAD 做成可替换的 —— 将来装上
    silero 时只要实现同样的接口即可，不用动其他代码。
    """

    def __init__(self, config: Optional[VadConfig] = None) -> None:
        self.config = config or VadConfig()
        # 噪声底初值取一个偏低的经验值，头几帧会快速自适应到真实环境
        self.noise_floor = 0.005
        self._speech_streak = 0
        self._last_result: Optional[VadResult] = None

    def threshold(self) -> float:
        return max(self.config.min_energy, self.noise_floor * self.config.energy_ratio)

    def process(self, frame: AudioFrame) -> VadResult:
        energy = frame.rms()
        zcr = frame.zero_crossing_rate()
        threshold = self.threshold()

        loud_enough = energy >= threshold
        # 过零率过高说明不是人的浊音（更像稳态噪声/电流声）
        plausible = zcr <= self.config.max_zcr
        is_speech = loud_enough and plausible

        if is_speech:
            self._speech_streak += 1
            reason = "speech"
        else:
            self._speech_streak = 0
            reason = "quiet" if not loud_enough else "noise_like"
            # 只在判为静音时更新噪声底：否则说话声会把底噪一起抬上去，
            # 越说越难触发（这是能量法最经典的坑）
            self.noise_floor = (1 - self.config.noise_adapt) * self.noise_floor + (
                self.config.noise_adapt * energy
            )

        # start_frames 的消抖在这里体现：报给上层的 is_speech 只在连续超阈值后为真
        confirmed = is_speech and self._speech_streak >= self.config.start_frames
        result = VadResult(
            is_speech=confirmed,
            energy=energy,
            noise_floor=self.noise_floor,
            threshold=threshold,
            zcr=zcr,
            reason=reason if confirmed else ("pending" if is_speech else reason),
        )
        self._last_result = result
        return result

    def reset(self) -> None:
        self._speech_streak = 0
        self._last_result = None

    @property
    def last(self) -> Optional[VadResult]:
        return self._last_result


# --------------------------------------------------------------------------- #
# 端点检测（把连续帧切成一句一句）
# --------------------------------------------------------------------------- #


class UtteranceState(str, Enum):
    IDLE = "idle"  # 还没开始说话
    SPEAKING = "speaking"  # 正在说
    ENDED = "ended"  # 刚说完，产出了一句话


@dataclass
class Utterance:
    """一句完整的话。"""

    pcm: bytes
    duration_s: float
    started_at: float
    ended_at: float
    # 触发结束的原因：silence（正常说完）/ max_length（超长截断）/ forced（手动结束）
    end_reason: str = "silence"

    def __len__(self) -> int:
        return len(self.pcm)


class Endpointer:
    """
    逐帧喂入，吐出"整句话"。

    状态机很简单，但每个转换条件都对应一个具体的失败模式：
      IDLE -> SPEAKING：需要 start_frames 连续确认为语音（防瞬时噪声）
      SPEAKING -> ENDED：需要 end_silence_ms 连续静音（防换气被当成说完）
      丢弃：总时长 < min_speech_ms（防咳嗽、敲桌子）
      截断：总时长 > max_utterance_ms（保险丝）
    """

    def __init__(self, config: Optional[VadConfig] = None) -> None:
        self.config = config or VadConfig()
        self.state = UtteranceState.IDLE
        self._buffer = bytearray()
        self._speech_ms = 0
        self._silence_ms = 0
        self._started_at = 0.0
        self._frames_in_utterance = 0
        # 最后一段语音结束时的缓冲区长度：用于把句尾静音剪掉
        self._last_speech_len = 0
        self.dropped = 0
        self.truncated = 0

    def process(self, frame: AudioFrame, vad: VadResult) -> Optional[Utterance]:
        """喂一帧。返回 Utterance 表示刚说完一句；返回 None 表示还在进行中。"""
        if self.state is UtteranceState.IDLE:
            if not vad.is_speech:
                return None
            self.state = UtteranceState.SPEAKING
            self._buffer = bytearray(frame.pcm)
            self._speech_ms = FRAME_MS
            self._silence_ms = 0
            self._started_at = frame.captured_at
            self._frames_in_utterance = 1
            self._last_speech_len = len(self._buffer)
            return None

        # SPEAKING
        self._buffer.extend(frame.pcm)
        self._frames_in_utterance += 1

        if vad.is_speech:
            self._speech_ms += FRAME_MS
            self._silence_ms = 0
            self._last_speech_len = len(self._buffer)
        else:
            self._silence_ms += FRAME_MS

        total_ms = self._frames_in_utterance * FRAME_MS

        # 保险丝：说得太久就别等了，先拿前半句去识别
        if total_ms >= self.config.max_utterance_ms:
            self.truncated += 1
            return self._finish(frame.captured_at, "max_length")

        if self._silence_ms >= self.config.end_silence_ms:
            uttered = self._finish(frame.captured_at, "silence")
            if uttered is None:
                # 太短，不算一句话：丢弃并回到 IDLE，等下一次真正的开口
                self.dropped += 1
            return uttered

        return None

    def _finish(self, ended_at: float, reason: str) -> Optional[Utterance]:
        """
        收尾并产出句子。

        **句尾静音会被剪掉**（保留一小段，让识别听起来自然）。这不是优化而是必要：
        端点检测为了确认"用户真的说完了"必须多等 end_silence_ms，如果把这 500ms
        静音一并交给 ASR，Whisper 这类模型在长尾静音下更容易产生"幻觉文字"，
        而且时长统计会系统性偏长。
        """
        raw = bytes(self._buffer)
        pcm = raw[: self._last_speech_len] if reason == "silence" else raw
        duration = len(pcm) / float(SAMPLE_RATE * SAMPLE_WIDTH)

        self.state = UtteranceState.IDLE
        self._buffer = bytearray()
        self._frames_in_utterance = 0
        speech_ms, self._speech_ms = self._speech_ms, 0
        self._silence_ms = 0
        self._last_speech_len = 0

        if reason == "silence" and speech_ms < self.config.min_speech_ms:
            return None  # 太短：咳嗽、敲键盘
        if not pcm:
            return None
        return Utterance(
            pcm=pcm,
            duration_s=duration,
            started_at=self._started_at,
            ended_at=ended_at,
            end_reason=reason,
        )

    def force_end(self, now: Optional[float] = None) -> Optional[Utterance]:
        """手动结束当前这句（松开按键说话、或用户主动截断）。"""
        if self.state is not UtteranceState.SPEAKING:
            return None
        return self._finish(now or time.monotonic(), "forced")

    def reset(self) -> None:
        self.state = UtteranceState.IDLE
        self._buffer = bytearray()
        self._speech_ms = 0
        self._silence_ms = 0
        self._frames_in_utterance = 0
        self._last_speech_len = 0


# --------------------------------------------------------------------------- #
# ASR / TTS 接口
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class Transcript:
    """一次语音识别结果。"""

    text: str = ""
    final: bool = True  # 流式识别时用于区分中间结果与最终结果
    confidence: float = 1.0
    elapsed_ms: float = 0.0
    source: str = ""
    error: str = ""

    def __bool__(self) -> bool:
        return bool(self.text.strip())


class AsrEngine(Protocol):
    """语音识别。实现者不得抛异常表达失败，返回带 error 的 Transcript 即可。"""

    name: str

    def available(self) -> bool:  # pragma: no cover
        ...

    async def transcribe(self, pcm: bytes, sample_rate: int = SAMPLE_RATE) -> Transcript:  # pragma: no cover
        ...


class TtsEngine(Protocol):
    """语音合成。返回 (编码后的音频字节, 格式名)。"""

    name: str

    def available(self) -> bool:  # pragma: no cover
        ...

    async def synthesize(self, text: str) -> tuple[bytes, str]:  # pragma: no cover
        ...


class MockAsrEngine:
    """
    零依赖的假 ASR。按音频长度与能量编造一句可预测的"识别结果"。

    存在的意义和 mock 大脑一样：让整条语音链路的**时序与状态机**在没有模型、
    没有麦克风的情况下也能被完整验证 —— 包括"识别出文字后交给对话"这一段。
    """

    name = "mock"

    def __init__(self, mapping: Optional[Callable[[bytes], str]] = None) -> None:
        self.mapping = mapping
        self.calls = 0

    def available(self) -> bool:
        return True

    async def transcribe(self, pcm: bytes, sample_rate: int = SAMPLE_RATE) -> Transcript:
        started = time.monotonic()
        self.calls += 1
        if self.mapping is not None:
            text = self.mapping(pcm)
        else:
            seconds = len(pcm) / float(sample_rate * SAMPLE_WIDTH)
            # 说得越久，"识别"出来的句子越长 —— 让测试能用时长推断结果
            if seconds < 0.5:
                text = "嗯"
            elif seconds < 1.5:
                text = "你在忙什么"
            else:
                text = "你刚才说的那段话我听清了，我们聊聊吧"
        return Transcript(
            text=text,
            source=self.name,
            elapsed_ms=(time.monotonic() - started) * 1000,
        )
class WindowsSapiTtsEngine:
    """
    本地语音合成 —— **实际由主进程（Node）执行**，这里只是客户端。

    ## 为什么不在 Python 侧起 PowerShell（2026-10-08 实测定位）

    本地合成走 Windows SAPI。实测四条链路：

        shell  -> powershell            OK   Voice.Name=[Microsoft Huihui Desktop]
        Node   -> powershell            OK   三种接法全部成功（WAV 60872 字节）
        Python -> powershell            FAIL Voice.Name=[]（空）、Speak 失败
        Node -> Python -> powershell    FAIL 同上

    报错是 SAPI 自己的话：

        No voice installed on the system or none available with the current
        security setting.

    但 `GetInstalledVoices()` **能列出 2 个音色**（含中文 Huihui、Enabled=True）
    —— **枚举得到、用不了**。同一台机器、同一个脚本、同一个 64 位 PowerShell，
    唯一变量是「谁起的进程」。

    已排除：编码（码点逐字符核对）、音色名（码点级相同）、位数（都 64 位）、
    `-NonInteractive`、`-STA`、`-Command` vs `-File`、TEMP 指向、stdio 接法。

    **生产里 Electron 是父进程，也就是 Node 那条路 —— 天然可用。**
    所以合成放在主进程，这里通过 `tts.synthesize` 请求它。

    ## 🔴 但实测：**当前环境里 SAPI 根本不可用**（2026-10-08 定案）

    把合成移到主进程之后**仍然失败**。用 settings:tts-probe（CDP 直调，在主进程里
    真跑一次）拿到了决定性数据：

        位数: 64 · 版本: 5.1.26100.9444 · 进程: System32 下的 powershell.exe
        枚举数: 2                                  <- GetInstalledVoices() 看得到
        选音色: 失败 -> 系统上未安装语音，或没有当前安全设置可用的语音。
        默认音色: []                                <- **空字符串**
        说话: 失败 -> 同一个错误

    也就是说 **SAPI 能枚举音色、但一个都取不到**（$synth.Voice.Name 是空串）。
    SelectVoice 和默认音色**两条路都断**，所以不是名字不对、不是编码、
    不是位数、不是 stdio —— 是这个环境里 SAPI 的语音对象整体不可用。

    ## 所以本地 TTS 在这个环境里做不成

    结论：**要出声就得用云端**（providers.tts.provider: openai +
    硅基流动 CosyVoice2，密钥已有）。本引擎保留着 —— 结构、重采样、口型链路
    全都写好了，在 SAPI 正常的环境里能用。

    ## 为什么交付的是**裸 PCM** 而不是 WAV

      · `VoiceSession` 要拿它算音量包络（做口型）
      · 前端 `voice-io.js` 的 `format='pcm'` 分支**写死按 16kHz 解**
      · SAPI 出的是 22050Hz -> 主进程已经重采样到 16kHz
        （不重采样的话音调会升高约 1.38 倍，就是"花栗鼠"效果）
    """

    name = "windows"

    def __init__(
        self,
        voice: str = "",
        rate: int = 0,
        volume: int = 100,
        # ⚠️ **必须大于主进程的超时**（`ttsLocal.ts` 的 `timeoutMs = 20000`）。
        #
        # 两边都是 20s 时会出现竞态（2026-10-09 实测）：
        #
        #     20:13:31.280  Python 20s 超时，抛 TimeoutError（消息为空）
        #     20:13:31.285  主进程的回执到达（消息='SAPI 没有可用音色'）—— 晚 5ms
        #
        # 真正的错误信息**已经拿到了、却被丢掉了**，用户只看到“没声音、也没原因”。
        # 收方等得比发方久，才能保证拿到发方的失败原因而不是自己造一个空超时。
        timeout_s: float = 40.0,
    ) -> None:
        self.voice = voice
        self.rate = rate
        self.volume = volume
        self.timeout_s = timeout_s
        self.calls = 0
        self.spoken: list[str] = []
        # 请求序号 + 等待中的 Future。用序号而不是"一次一个" ——
        # 将来并发合成时不用改结构（现在 VoiceSession 是串行的）。
        self._seq = 0
        self._pending: dict[int, asyncio.Future[tuple[bytes, str]]] = {}
        # 由 VoiceSession 注入的发送函数（它持有 gateway 的 send）
        self._send: Optional[Callable[[dict[str, Any]], None]] = None

    def attach(self, send: Callable[[dict[str, Any]], None]) -> None:
        """
        注入「把消息发给主进程」的函数。`VoiceSession.attach()` 时调用。

        用注入而不是让引擎自己拿 gateway：这个模块**不认识 Gateway**
        （文件头的设计原则），保持单向依赖。
        """
        self._send = send

    def available(self) -> bool:
        """有没有连上主进程。连不上就不出声，而不是抛异常炸掉对话。"""
        return self._send is not None

    async def synthesize(self, text: str) -> tuple[bytes, str]:
        if not text.strip():
            return b"", "pcm"
        if self._send is None:
            raise RuntimeError("语音合成引擎没接到主进程（attach 未调用）")

        self._seq += 1
        seq = self._seq
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[tuple[bytes, str]] = loop.create_future()
        self._pending[seq] = fut
        # 节点 A：请求发出去之前
        log.debug("合成请求 seq=%s 音色=%r 文字=%d 字", seq, self.voice, len(text))
        try:
            # ⚠️ sender 返回的可能是 coroutine（gateway 的 `broadcast` 就是 async）——
            # 照 `BroadcastAudioOutput.play` 的做法：是协程就 await。
            # 不这样的话会报 "coroutine was never awaited"，消息发不出去。
            result = self._send(
                {
                    "type": "tts.synthesize",
                    "seq": seq,
                    "text": text,
                    "voice": self.voice,
                    "rate": self.rate,
                    "volume": self.volume,
                }
            )
            if asyncio.iscoroutine(result):
                await result
            pcm, fmt = await asyncio.wait_for(fut, timeout=self.timeout_s)
            # 节点 B：真的拿到回执了
            log.debug("合成回执 seq=%s 拿到 %d 字节 / %s", seq, len(pcm), fmt)
            self.calls += 1
            self.spoken.append(text)
            return pcm, fmt
        except asyncio.TimeoutError:
            # ⚠️ 超时单独记（2026-10-09）。
            #
            # 之前超时和"主进程回了 ok=false 但消息为空"**长得一模一样**
            # （都是 `语音合成失败：` 后面空），所以完全分不出是哪种。
            # 这行日志把两者区分开：超时说"没收到回执"，其余说具体原因。
            log.warning(
                "合成超时：%.0fs 内没收到主进程回执（seq=%s，音色=%r）",
                self.timeout_s,
                seq,
                self.voice,
            )
            raise
        finally:
            self._pending.pop(seq, None)

    def resolve(self, seq: int, ok: bool, pcm_b64: str = "", message: str = "") -> None:
        """
        主进程回了 `tts.result` —— 唤醒对应的等待者。

        找不到 seq 就静静丢掉：可能是超时之后才到的迟到应答。
        为它打 error 会让日志充斥着无害的迟到消息。
        """
        # 节点 F：主进程的回执到了（哪怕没人等）
        log.debug("收到合成回执 seq=%s ok=%s 字节=%d 消息=%r", seq, ok, len(pcm_b64 or ""), message[:60])
        fut = self._pending.get(seq)
        if fut is None or fut.done():
            # ⚠️ **升级成 warning**（2026-10-09）。
            #
            # 这一条以前是 debug，于是“结果被丢掉”完全不可见 ——
            # 而那恰恰是超时竞态的唯一症状。丢掉一个**失败原因**是要让人知道的：
            # 否则用户只看到“没声音、也没原因”。
            log.warning(
                "合成回执 seq=%s 没有等待者（已超时被摘掉？）—— 本次结果被丢弃：ok=%s %s",
                seq,
                ok,
                (message or "(没有消息)")[:80],
            )
            return
        if not ok:
            fut.set_exception(RuntimeError(message or "主进程合成失败"))
            return
        try:
            import base64

            fut.set_result((base64.b64decode(pcm_b64 or ""), "pcm"))
        except Exception as exc:  # noqa: BLE001
            fut.set_exception(RuntimeError(f"解 base64 失败：{exc}"))


class MockTtsEngine:
    """假 TTS：产出一段与文字长度成正比的静音 PCM，供播放时序测试使用。"""

    name = "mock"

    def __init__(self, ms_per_char: float = 60.0) -> None:
        self.ms_per_char = ms_per_char
        self.calls = 0
        self.spoken: list[str] = []

    def available(self) -> bool:
        return True

    async def synthesize(self, text: str) -> tuple[bytes, str]:
        self.calls += 1
        self.spoken.append(text)
        ms = max(120.0, len(text) * self.ms_per_char)
        return silence(int(ms)), "pcm"


# --------------------------------------------------------------------------- #
# 音频 I/O 接口（真实现由 Electron 渲染层提供）
# --------------------------------------------------------------------------- #


class AudioInput(Protocol):
    """麦克风输入。真实现由前端通过 WebSocket 推送 PCM 帧。"""

    def read(self) -> Optional[AudioFrame]:  # pragma: no cover
        ...


class AudioOutput(Protocol):
    """音频播放。真实现由前端拿到字节后播放。"""

    async def play(self, payload: bytes, fmt: str, text: str) -> None:  # pragma: no cover
        ...

    async def stop(self) -> None:  # pragma: no cover
        ...


class NullAudioInput:
    """没有麦克风时的输入：永远返回 None。"""

    def read(self) -> Optional[AudioFrame]:
        return None


class QueueAudioInput:
    """
    来自前端的音频输入：一个**有界**队列。

    有界非常关键：如果前端推帧比我们处理得快（或处理被阻塞），无界队列会一路吃内存
    直到进程被杀。满了就丢最旧的帧 —— 语音是实时的，迟到的音频没有价值，
    宁可丢帧也不能拖垮内存。
    """

    def __init__(self, max_frames: int = 100) -> None:
        self._frames: list[AudioFrame] = []
        self.max_frames = max(10, max_frames)
        self.dropped = 0
        self.total = 0

    def push(self, pcm: bytes) -> None:
        self.total += 1
        self._frames.append(AudioFrame(pcm=pcm))
        while len(self._frames) > self.max_frames:
            self._frames.pop(0)
            self.dropped += 1

    def read(self) -> Optional[AudioFrame]:
        return self._frames.pop(0) if self._frames else None

    @property
    def pending(self) -> int:
        return len(self._frames)


class BroadcastAudioOutput:
    """
    把合成好的音频交给前端播放。

    播放是"发出去就算完成"、不等真正播完：Python 侧无法知道浏览器的播放进度，
    硬等只会让状态机卡住。真正的播放完成、以及"用户是否插话"，都由前端的
    音频事件与语音检测来驱动 —— 职责划清，不假装知道不知道的事。
    """

    def __init__(self, sink: Callable[[bytes, str, str], Any]) -> None:
        self._sink = sink
        self.played = 0

    async def play(self, payload: bytes, fmt: str, text: str) -> None:
        self.played += 1
        result = self._sink(payload, fmt, text)
        if asyncio.iscoroutine(result):
            await result

    async def stop(self) -> None:
        # 打断由前端负责真正停止播放（它才知道 <audio> 的状态）
        return None


class NullAudioOutput:
    """没有播放能力时的输出：不播，但立刻返回，不阻塞状态机。"""

    async def play(self, payload: bytes, fmt: str, text: str) -> None:
        return None

    async def stop(self) -> None:
        return None


# --------------------------------------------------------------------------- #
# 语音会话状态机
# --------------------------------------------------------------------------- #


class VoiceState(str, Enum):
    IDLE = "idle"  # 没在听（未开启语音）
    LISTENING = "listening"  # 在听，但用户还没开口
    HEARING = "hearing"  # 用户正在说话
    THINKING = "thinking"  # 已送去识别/生成
    SPEAKING = "speaking"  # 宠物正在说话


@dataclass
class VoiceConfig:
    """语音运行时参数。取自配置的 providers.asr / tts 段。"""

    enabled: bool = False
    sample_rate: int = SAMPLE_RATE
    # 麦克风增益不足时可用它放大；1.0 表示不动
    input_gain: float = 1.0
    # 宠物说话时是否允许用户打断（"barge-in"）。开着体验更好，但麦克风差的
    # 环境下容易被自己的外放声音触发，所以留了开关。
    allow_barge_in: bool = True
    # 判定"用户开始打断"所需的连续语音帧数。比正常起说更严格一点，
    # 避免宠物自己的尾音或环境噪声把它的发言打断。
    barge_in_frames: int = 5
    # 一句话最短时长，短于此不送识别（省一次调用）
    min_utterance_ms: int = 250


class VoiceSession:
    """
    语音会话的核心状态机。

    它把"连续音频流"变成"一句一句的话"，并处理三件事：

      1. **端点检测**：什么时候用户在说话、什么时候说完了。
      2. **打断（barge-in）**：宠物说话时用户开口 -> 立刻停止播放并开始听。
         这是语音对话里最影响观感的行为，做不好用户会觉得"它在跟我抢话"。
      3. **状态广播**：每一步都发布事件，前端据此显示"在听 / 在想 / 在说"。

    与其余模块一样，它只通过事件总线与外界交流，不认识 Gateway 也不认识大脑。
    """

    def __init__(
        self,
        config: Config,
        bus: Bus,
        asr: Optional[AsrEngine] = None,
        tts: Optional[TtsEngine] = None,
        audio_in: Optional[AudioInput] = None,
        audio_out: Optional[AudioOutput] = None,
        voice_config: Optional[VoiceConfig] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self.bus = bus
        self.asr = asr or MockAsrEngine()
        self.tts = tts or MockTtsEngine()
        self.audio_in = audio_in or NullAudioInput()
        self.audio_out = audio_out or NullAudioOutput()
        self.voice_config = voice_config or self._voice_config_from(config)
        self._clock = clock

        # 换引擎时用来记录"当前是哪一套配置"，避免无谓的重建（见 swap_engines / engine_signature）
        self._engine_signature: Optional[tuple[str, ...]] = None
        # 「发消息给主进程」的函数。由 `attach_sender` 注入，热切换引擎时要重放。
        self._sender: Optional[Callable[[dict[str, Any]], None]] = None
        # 说话用的自增序号（`pet.mouth` 的 id 字段，前端不读它，只为日志可读）
        self._speak_seq = 0

    # ------------------------------------------------------------ 热切换引擎

        asr_cfg_now = config.providers.get("asr", {})
        self.vad = VoiceActivityDetector(
            VadConfig(
                end_silence_ms=int(asr_cfg_now.get("end_silence_ms", 500)),
                # 注意这里传的是 min_speech_ms，不要顺手写成 end_silence_ms ——
                # 两者语义完全不同（最短语音 vs 判定说完的静音时长），曾写错过。
                min_speech_ms=self.voice_config.min_utterance_ms,
                # ---------------------------------------------------------------
                # 🔴 三个**原来没接线**的参数（2026-10-10 补上）
                #
                # 用户报「按了没反应」，实测日志暴露出阈值被推得过高：
                #
                #     背景噪声 energy ≈ 0.0137
                #     threshold  0.0208 → 0.0330 → 0.0373 → … → 0.0421（一路涨）
                #
                # 机理：`threshold = max(min_energy, noise_floor × energy_ratio)`，
                # 而 `noise_floor` 每帧以 `noise_adapt` 向当前能量靠拢 ——
                # **它把背景噪声学成了基准**，于是阈值 ≈ 背景噪声 × 3。
                # 用户轻声说话的能量如果落在那个区间，就**永远判静音**。
                #
                # ⚠️ 这三个原来**只有 dataclass 默认值**，YAML 里改了完全没用 ——
                # 与 `_voice_config_from` 那段注释里写的"假开关"是同一族 bug。
                # **判断方法仍然是：改一项配置，看行为变不变。**
                # ---------------------------------------------------------------
                energy_ratio=float(asr_cfg_now.get("energy_ratio", 2.0)),
                min_energy=float(asr_cfg_now.get("min_energy", 0.008)),
                noise_adapt=float(asr_cfg_now.get("noise_adapt", 0.02)),
                start_frames=int(asr_cfg_now.get("start_frames", 3)),
                max_zcr=float(asr_cfg_now.get("max_zcr", 0.6)),
            )
        )
        # ⚠️ `endpointer` 紧跟 `vad` —— 别把别的东西插到这两者之间。
        # （2026-10-08：我把引擎指纹插在这里，编辑时又误删了这行，导致
        #   `AttributeError: 'VoiceSession' object has no attribute 'endpointer'`，
        #   语音会话 / 打断 / 可关闭打断 三条自检全红。）
        self.endpointer = Endpointer(self.vad.config)
        self.state = VoiceState.IDLE
        self.utterances = 0
        self.transcripts = 0
        self.barge_ins = 0
        self.last_utterance: Optional[Utterance] = None
        self.last_transcript: Optional[Transcript] = None
        self._barge_streak = 0
        # 宠物是否正在说话（由 pet.say.start / pet.say.end 驱动）
        self._pet_speaking = False
        """
        **在跑的异步任务强引用集合**（2026-10-08 修，是个真 bug）。

        ## ⚠️ 为什么必须有它：asyncio 只保留 task 的**弱引用**

        `asyncio.get_running_loop().create_task(coro)` 的返回值如果不存下来，
        事件循环**不会**替你保住它 —— 那个 task 随时可能被 GC 回收，
        协程在中途被销毁，**既不报错也不完成**。

        ### 实测表现（写 `check-voice-flow.py`（历史实测脚本）时暴露）

            faster_whisper Processing audio with duration 00:01.840   <- 6.73s 的音频只处理了 1.84s
            等识别… 结果: []

        识别任务跑到一半被回收，`user.voice` 事件永远发不出来。
        三条测试音频里只有最短的那条侥幸跑完 —— 看起来像"识别不稳定"，
        实际是**任务被销毁**。

        ### 为什么以前没暴露

        单个短句的识别太快（几百毫秒），任务在被回收前就跑完了。
        只有音频长到一定程度、任务活过几轮 GC 才会犯。

        ### 修法

        加进这个集合保住强引用，`done` 回调里再移除（否则集合会一直涨）。
        """
        self._tasks: set[asyncio.Task[Any]] = set()
        """
        采集闸门（按住说话用）。

        ## ⚠️ 默认 `True` 是刻意的

        普通语音模式（不按住说话）下，麦克风一开就该一直听 —— 那才是
        "免提对话"。闸门只在**按住说话**启用时由前端切换（见 `set_capture`）。

        如果默认 `False`，那么"只开语音开关、没配按住说话"的用户会发现
        宠物**完全听不见** —— 而且没有任何报错，极难查。
        """
        self._capture_enabled = True
        # 识别串行锁（见 `_transcribe`：并发会让对话顺序错乱）
        self._transcribe_lock = asyncio.Lock()
        # 按住说话模式标志（见 set_capture：PTT 期间不用静音断句）
        self._ptt_mode = False
        # 记下配置里的静音阈值 —— PTT 会临时改成无穷大，松手时按它恢复
        self._silence_ms_default = int(getattr(self.endpointer.config, 'end_silence_ms', 500))

        # ⚠️ 引擎指纹**必须放在 `__init__` 的最后一行**（2026-10-08 踩过）。
        #
        # 它只读 `self.config`，所以我一开始随手插在了 `self.vad = ...` 前面 ——
        # 结果它**插进了后面的初始化序列中间**，把 `self.endpointer = ...` 等
        # 一批赋值挤到了一边（我编辑时还真的误删过一次）。实测症状：
        #
        #     AttributeError: 'VoiceSession' object has no attribute 'endpointer'
        #     -> 语音会话、打断、可关闭打断 三条自检全红
        #
        # 它是个纯读取 + 纯赋值，放最后既无依赖问题，也不会再打断任何东西。
        self._engine_signature = self.engine_signature()

    # ------------------------------------------------------------------ 状态


    def engine_signature(self) -> tuple[str, str, str, str]:
        """
        当前配置对应的"引擎指纹"：`(asr_provider, asr_model, tts_provider, tts_model)`。

        ⚠️ 用来判断**要不要真的重建**。缺少它的话，每次 `config.update`
        （改活泼度、改免打扰、改思考模式……都会发）都会把语音引擎重建一遍 ——
        本地 Whisper 重建一次要 **4 秒**（实测 `21:57:51 -> 21:57:55`），
        用户只是拖了个滑杆却要等 4 秒，而且会短暂听不到。
        """
        asr = self.config.providers.get("asr", {}) or {}
        tts = self.config.providers.get("tts", {}) or {}
        return (
            str(asr.get("provider") or "mock").lower(),
            str(asr.get("model") if asr.get("provider") == "local" else asr.get("cloud_model") or ""),
            str(tts.get("provider") or "none").lower(),
            str(tts.get("model") or ""),
            # ⚠️ **音色必须进指纹**（2026-10-09 修，用户实测「女声还是男声」）。
            #
            # 原来这里是 `str(tts.get("model") or tts.get("voice") or "")` ——
            # `model` 永远非空（云端模型名），所以 `voice` **从来没进过指纹**。
            # 于是换音色时指纹不变 -> 网关认为"引擎没变、不必重建"
            # -> `OpenAITtsEngine.voice` 还是旧值 -> **换了音色但声音没变**。
            #
            # 判据：**这个参数变了，合成结果会不会不一样？**
            #   · 云端音色 `voice`       会 -> 进
            #   · 本地音色 `local_voice` 会 -> 进（两条路线格式不同，分开存）
            #   · 语速 `rate`            会 -> 进
            str(tts.get("voice") or ""),
            str(tts.get("local_voice") or ""),
            str(tts.get("rate") or 0),
            # ⚠️ **edge 的四个参数也必须进指纹**（2026-10-11 修，用户实测
            # 「音色没变，有问题」）。
            #
            # 这是**同一个坑的第二次**：上面那段注释刚说过"这个参数变了、
            # 合成结果会不会不一样？会就进指纹"，而我加 `EdgeTtsEngine` 时
            # 只加了引擎、**忘了把它的参数加进指纹**。后果一模一样：
            #
            #     21:04:44  配置已热更新：{'providers.tts.edge_voice': 'zh-HK-HiuGaaiNeural'}
            #     21:05:41  配置已热更新：{'providers.tts.edge_voice': 'zh-CN-YunyangNeural'}
            #     21:06:14  配置已热更新：{'providers.tts.edge_voice': 'zh-HK-HiuGaaiNeural'}
            #
            # 配置改了、广播了、`EdgeTtsEngine.voice` 却还是旧值 ——
            # **换了三次音色，声音一次都没变**。
            #
            # 教训：**加一个新引擎时，除了实现它，还要把它的每个参数加进指纹**。
            # 否则新引擎的第一个可调参数就是假开关。
            str(tts.get("edge_voice") or ""),
            str(tts.get("edge_rate") or ""),
            str(tts.get("edge_volume") or ""),
            str(tts.get("edge_pitch") or ""),
        )

    def swap_engines(self, asr: AsrEngine, tts: TtsEngine) -> None:
        """
        运行中把 ASR/TTS 引擎换掉，并**释放旧引擎占的资源**。

        ## 🔴 为什么需要它（2026-10-08 用户实测发现）

        用户在设置页把语音识别从「本地」切到「云端（硅基流动）」。日志显示
        配置**确实热更新了**：

            21:01:48  配置已热更新：{'providers.asr.provider': 'openai'}

        但 `build_voice_pipeline()` **只在 Gateway 构造时调过一次**，
        而 `self.asr` 也只在 `__init__` 里赋值。所以：

            · 界面上写着「云端」，实际**还在用本地 Whisper 识别**
            · 本地 `large-v3-turbo` 继续占着 **~800MB 内存 + ~1.5-2GB 显存**
              （实测切换前后内存一模一样：801MB -> 801MB，一点没释放）

        这是典型的"假开关"——界面和实际行为不一致，而且**不报任何错**。

        ## ⚠️ 释放显存要显式做

        Python 的 GC 只回收对象；**CUDA 显存的缓存要 `torch.cuda.empty_cache()`**。
        CTranslate2 内部用的是它自己的 CUDA 上下文，所以这里：
          · 先 `del` 掉旧引擎并 `gc.collect()`
          · 再尽力 `torch.cuda.empty_cache()`（没装 torch / 没 GPU 就跳过）
        """
        old_asr = self.asr
        self.asr = asr
        self.tts = tts
        self._engine_signature = self.engine_signature()

        # ⚠️ **换完必须重新注入 sender**（2026-10-08 踩到）。
        #
        # `WindowsSapiTtsEngine` 靠 `attach(send)` 拿到"发消息给主进程"的函数，
        # 而这里换的是**全新的对象** —— 不重新注入的话，新引擎的 `_send` 是 None，
        # `synthesize()` 直接抛"没接到主进程"。
        #
        # 症状是：用户在设置页把 TTS 切到本地 -> 看着保存成功 -> **永远不出声**，
        # 而且只有一条 error 日志。典型的"假开关"。
        if self._sender is not None:
            for engine in (self.asr, self.tts):
                hook = getattr(engine, "attach", None)
                if callable(hook):
                    try:
                        hook(self._sender)
                    except Exception:  # noqa: BLE001
                        log.exception("换引擎后重新接入通道失败：%s", engine.__class__.__name__)

        # 旧引擎如果没有"关闭"钩子，就显式删引用 + 回收
        for old in (old_asr,):
            closer = getattr(old, "close", None) or getattr(old, "unload", None)
            if callable(closer):
                try:
                    closer()
                except Exception:  # noqa: BLE001 —— 释放失败不该影响新引擎
                    log.debug("旧 ASR 引擎 close() 失败", exc_info=True)
        del old_asr
        try:
            import gc

            gc.collect()
            # ⚠️ 只有真的装了 torch 才动它；CTranslate2 单独装时没有这个模块
            import torch  # type: ignore[import-not-found]

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
                log.info("已回收 CUDA 显存缓存（旧 ASR 引擎释放）")
        except Exception:  # noqa: BLE001
            log.debug("回收 CUDA 显存跳过（没有 torch 或没有 GPU）", exc_info=True)


    async def _set_state(self, state: VoiceState, **extra: Any) -> None:
        if state == self.state and not extra:
            return
        self.state = state
        await self.bus.publish("voice.state", {"state": state.value, **extra})

    @property
    def enabled(self) -> bool:
        return self.voice_config.enabled

    async def set_enabled(self, enabled: bool) -> None:
        self.voice_config.enabled = enabled
        self.endpointer.reset()
        self.vad.reset()
        await self._set_state(VoiceState.LISTENING if enabled else VoiceState.IDLE)

    @staticmethod
    def _voice_config_from(config: Config) -> VoiceConfig:
        """
        从 `providers.asr` 段构造 `VoiceConfig`。

        ## 🔴 这里原来只传了 `enabled`（2026-10-09 修，又是"假开关"）

        原来是：

            VoiceConfig(enabled=bool(config.providers.get("asr", {}).get("enabled", False)))

        —— `allow_barge_in` / `barge_in_frames` / `end_silence_ms` / `input_gain`
        **一个都没从配置读**，全用 dataclass 默认值。于是这几项在 YAML 里
        **改了完全没用**：用户把 `allow_barge_in` 改成 false 之后，
        启动日志照样打「打断=允许」。

        这类"配置项写了但没接线"的 bug 在这个项目里出现过多次
        （`history_token_budget` 没进 `brain_profile` 白名单是同一族）。
        **判断方法：改一项配置，看启动日志/行为变不变。不变就是没接线。**

        ⚠️ `allow_barge_in` 的默认值刻意是 **False**（用户 2026-10-09 的决定）：
        开着的时候宠物会把**电脑外放的声音**当作用户插话，不停打断自己。
        """
        asr = config.providers.get("asr", {}) or {}
        return VoiceConfig(
            enabled=bool(asr.get("enabled", False)),
            allow_barge_in=bool(asr.get("allow_barge_in", False)),
            barge_in_frames=int(asr.get("barge_in_frames") or 5),
            input_gain=float(asr.get("input_gain") or 1.0),
        )

    def attach(self) -> None:
        """订阅宠物发言事件，用于打断判定与状态显示。"""
        self.bus.subscribe("pet.say.start", self._on_pet_say_start)
        self.bus.subscribe("pet.say.end", self._on_pet_say_end)
        # ⚠️ 说话（TTS）也挂在这里（2026-10-08 才接上）
        self.bus.subscribe("pet.say.end", self._on_pet_say_end_speak)

    def attach_sender(self, send: Callable[[dict[str, Any]], None]) -> None:
        """
        把「发消息给主进程」的函数交给需要它的引擎。

        ## 为什么需要（2026-10-08）

        本地语音合成（Windows SAPI）**必须由主进程执行** —— 实测 Python 起的
        PowerShell 拿不到 SAPI 音色（`Voice.Name=[]`），而 Node 起的可以。
        所以 `WindowsSapiTtsEngine` 变成了客户端，要能发 `tts.synthesize`。

        `attach()` 里不直接做这件事，是因为 `VoiceSession` 不该知道
        「哪个引擎需要它」—— 有 `attach_sender` 方法的引擎才拿得到，
        别的引擎（mock / openai）完全不受影响。

        ⚠️ 顺手也接上 `tts.result` 的**回执入口**（见 `resolve_tts`）。
        """
        # 记下来 —— 热切换引擎时要重新注入（见 `swap_engines` 里那段说明）
        self._sender = send
        for engine in (self.asr, self.tts):
            hook = getattr(engine, "attach", None)
            if callable(hook):
                try:
                    hook(send)
                    log.info("语音引擎 %s 已接入主进程通道", engine.__class__.__name__)
                except Exception:  # noqa: BLE001 —— 接不上不该让语音瘫痪
                    log.exception("引擎 %s 接入主进程通道失败", engine.__class__.__name__)

    def resolve_tts(self, seq: int, ok: bool, pcm_b64: str = "", message: str = "") -> bool:
        """
        主进程回了 `tts.result` -> 转给当前 TTS 引擎。返回是否有人认领。

        ⚠️ 必须转给**当前**引擎（`self.tts`），而不是"建过的那个" ——
        用户可能刚把 TTS 从本地切到云端（`swap_engines` 换过对象），
        那时旧引擎的回执应该被丢掉（返回 False）。
        """
        handler = getattr(self.tts, "resolve", None)
        if not callable(handler):
            return False
        handler(seq, ok, pcm_b64, message)
        return True

    async def _on_pet_say_start(self, event: Any) -> None:
        self._pet_speaking = True
        await self._set_state(VoiceState.SPEAKING)

    async def _on_pet_say_end(self, event: Any) -> None:
        self._pet_speaking = False
        self._barge_streak = 0
        if self.voice_config.enabled:
            await self._set_state(VoiceState.LISTENING)

    async def _on_pet_say_end_speak(self, event: Any) -> None:
        """
        宠物说完一句话 -> **把它合成成语音播出来**（TTS 的入口）。

        ## ⚠️ 这个入口之前一直没接（2026-10-08 才发现）

        `speak()` 方法早就写好了（合成 + 算包络 + 交给前端播放），
        `OpenAITtsEngine` / `MockTtsEngine` 也都在 —— 但**全项目没有任何地方
        调用 `speak()`**。所以在这之前，宠物一直是"只显示文字、不出声"。

        接在 `pet.say.end` 上是因为它是**唯一带完整回复文本**的事件
        （`payload = {id, text, reason}`）。`pet.say.delta` 只有增量片段，
        拿它逐句合成会把一句话切得七零八落。

        ## 为什么整段合成而不是按句子流式

        SAPI 合成一次约 300ms，而宠物的回复通常就一两句 —— 整段合成足够快。
        按句流式需要维护"哪句已经合成过"的状态，收益却很小。

        ## 出错了不能影响别的订阅者

        `bus.publish` 里有异常隔离，但这条链上有网络（云端 TTS）和子进程
        （本地 SAPI），失败概率不低。所以这里**自己兜住**并记 warning ——
        用户看到的是"这次没出声"，而不是宠物整个卡住。
        """
        payload = dict(event.payload or {})
        text = str(payload.get("text") or "").strip()
        if not text:
            return
        # 打断/被取消的回复不读（`reason` 见 conversation 里那几处发布点）
        reason = str(payload.get("reason") or "")
        if reason in ("interrupted", "cancel"):
            log.debug("不读这句（reason=%s）：%s", reason, text[:20])
            return
        """
        ## ⚠️ 这里**不看** `self.enabled`（2026-10-08 定的）

        一开始我按"语音开关"来闸，结果宠物一声不吭 —— 而那个开关的语义是
        **麦克风**：

            允许看屏幕 / 语音对话（麦克风）
            「打开后可以直接对宠物说话，它听懂了就用语音回你」

        也就是说那个开关管的是**输入**（要麦克风权限、涉及隐私）。
        而宠物**出声不需要麦克风** —— 前端有扬声器就够了，不需要任何权限。

        所以出声只看 `providers.tts.provider`：
          · `none`     -> 引擎是 MockTtsEngine，合成出来是静音，**等于不出声**
          · `windows`  -> 本地 SAPI
          · `openai`   -> 云端
          · `edge`     -> edge-tts

        ## 🔴 2026-10-14：又多了一个开关 `providers.tts.enabled`

        用户要求：「把语音功能抽出来做成开关，放在允许看屏幕上面，开启时能
        把宠物回复的文字转为语音说出，语音功能在设置语音里设置好的」。

        也就是说：**引擎在设置页配好**（provider + 音色），菜单那个开关只负责
        「此刻说不说」。所以判据变成两件事**同时成立**：

            provider 是真实引擎  AND  tts.enabled 为真

        这与 `providers.vision.enabled`、免打扰是同一个套路（见 `config.py`
        里 muted 的说明）：做成配置而不是纯运行态，托盘与界面通过
        `config.update` 打开/关闭它，sidecar 与前端不会各存一份而失去同步。
        """
        if self.tts.__class__.__name__ == "MockTtsEngine":
            # `tts.provider: none` 或 `mock` —— 合成出来是等长静音，不必走一遍
            log.debug("不读这句：TTS 是 mock/none（providers.tts.provider）")
            return
        # 「语音播报」开关（2026-10-14）。⚠️ **每次现读**而不是构造时固定：
        # 用户在菜单里点了就要立刻生效，而 `config.update` 会替换
        # `self.config.providers` 这个 dict（见 `Gateway._thinking_flag`
        # 的说明：现读拿到的就是最新值）。
        if not (self.config.providers.get("tts", {}) or {}).get("enabled", True):
            log.debug("不读这句：语音播报开关已关（providers.tts.enabled=false）")
            return
        say_id = str(payload.get("id") or "")
        if not say_id:
            # ⚠️ `VoiceSession` 没有 `new_say_id`（那是 Conversation 的方法）。
            # 而 `say_id` 只用于 `pet.mouth` 消息的 `id` 字段，**前端其实不读它**
            # （它只用 `envelope` 数组）。所以这里给个自增 id 足够。
            self._speak_seq += 1
            say_id = f"tts{self._speak_seq}"
        try:
            await self.speak(text, say_id)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 —— 合成/播放失败不该影响对话
            log.warning("说话失败（这条不出声，对话继续）", exc_info=True)

    # ------------------------------------------------------------------ 单帧处理

    async def feed(self, frame: AudioFrame) -> Optional[Utterance]:
        """
        喂一帧音频。返回 Utterance 表示刚检测到一句话结束（已送去识别会另行发事件）。

        这是整条链路里被调用最频繁的方法（每秒 50 次），所以刻意保持同步计算、
        异步只在真正需要（识别、合成）时才用。
        """
        if not self.voice_config.enabled:
            return None

        # ---- 采集闸门：**必须在最前面** ----
        #
        # ## ⚠️ 位置很关键（2026-10-07 实测抓出来的）
        #
        # 我第一版把闸门放在"打断检测"之后，结果 `vad.process(frame)` 早就跑过了
        # —— 表现是闸门看着生效（端点不再累积），但 **VAD 的噪声底被持续带偏**：
        #
        #     闸门关闭期间 VAD 仍在处理音频（噪声底 0.005000 -> 0.011462）
        #
        # 噪声底是"什么算语音"的基准。它被非采集期的音频抬高之后，
        # **下一次真正按住说话时，用户的声音可能就不再被判定为语音了** ——
        # 一个延迟发作、极难归因的 bug。自检里那条断言就是为它写的。
        #
        # 语义上也应当如此："没按住的时候**一个字节都不要处理**"。
        #
        # ## 代价：没按住时不能用"开口"打断宠物
        #
        # 打断检测被一起跳过了。这是**刻意的取舍**：按住说话模式下，
        # 用户的意图由按键表达（松手就是说完），而不是靠抢话。
        # 想直接开口打断的用户不会开这个功能（见 `set_capture` 的说明）。
        if not self._capture_enabled:
            return None

        # 增益：麦克风太小的时候用户能自己调大，不必去改系统设置
        if self.voice_config.input_gain != 1.0:
            frame = AudioFrame(
                pcm=audioop.mul(frame.pcm, SAMPLE_WIDTH, self.voice_config.input_gain),
                captured_at=frame.captured_at,
            )

        vad = self.vad.process(frame)

        # ------------------------------------------------------------------
        # 🔍 采集期诊断（2026-10-10 临时加，排查「按住说话没反应」）
        # ------------------------------------------------------------------
        #
        # ## 为什么要加
        #
        # 用户报「按住说话没用」。已经排除了：闸门（日志里开/关都对）、
        # 音频帧（6602 条到了 sidecar）、`feed()` 的状态门控（2026-10-08 已修）。
        #
        # 剩下的嫌疑是 **VAD 没把语音判成语音** —— 端点一直 IDLE，
        # `force_end()` 返回 None，什么都不送识别。而这条路径**原来完全没日志**：
        # 「有没有在收帧、能量多少、判没判成语音」全看不见，只能靠猜。
        #
        # ## 为什么是每 300ms 一条而不是每帧
        #
        # 帧率约 60/s，每帧一条会把日志刷爆（main.log 已经被 WS 帧日志占了 97%）。
        # 300ms 一条既能看出"说话时能量有没有冲上去"，又不至于淹没别的日志。
        #
        # ⚠️ 只在闸门开着时记 —— 没按住时本来就不处理，记了没意义。
        # 所以这个日志**本身就会告诉你闸门状态对不对**。
        if self._capture_enabled:
            now_d = time.monotonic()
            if now_d - getattr(self, "_last_level_log", 0.0) >= 0.3:
                self._last_level_log = now_d
                log.info(
                    "采集电平：energy=%.5f threshold=%.5f %s | zcr=%.3f 噪声底=%.5f | "
                    "语音态=%s 门态=%s 已攒语音=%dms 丢弃=%d",
                    vad.energy,
                    self.vad.threshold(),
                    "语音" if vad.is_speech else "静音",
                    vad.zcr,
                    self.vad.noise_floor,
                    self.state.value,
                    self.endpointer.state.value,
                    self.endpointer._speech_ms,
                    self.endpointer.dropped,
                )

        # ---- 打断：宠物正在说话时用户开口 ----
        #
        # ⚠️ 打断**成功那一帧不要 `return None`**（2026-10-08 修）。
        #
        # 原来无论打断成不成功都 `return None`，于是：打断发生的那一帧被丢掉，
        # 而那一帧**正是用户开口的第一个音** —— 结果是"打断成功，但用户那句
        # 被掐掉了开头"，识别出来常是半句（"……你唱什么歌"）。
        #
        # 这跟本节修的那个 bug 是同一类错误：**把"状态切换"的瞬间当成
        # "这帧不要了"**。状态切完，这帧照常往下走。
        if self._pet_speaking and self.voice_config.allow_barge_in:
            if vad.is_speech:
                self._barge_streak += 1
            else:
                self._barge_streak = 0
            if self._barge_streak >= self.voice_config.barge_in_frames:
                self.barge_ins += 1
                log.info("检测到用户插话，打断宠物的发言")
                await self.bus.publish("user.interrupt", {"reason": "barge_in"})
                await self.audio_out.stop()
                self._pet_speaking = False
                self._barge_streak = 0
                await self._set_state(VoiceState.HEARING)
                # ⚠️ 这里**故意不 return** —— 让这一帧继续进端点检测
                # （它就是用户那句话的第一个音）。下面 `not self._pet_speaking`
                # 现在为真，会正常收下它。
            else:
                # 还没攒够连续的语音帧：这一帧丢弃，不喂端点
                # （避免宠物的声音被误当成用户开口）
                return None

        # ---- 正常端点检测 ----
        #
        # ## ⚠️ 这里**不能**用 `state` 当闸门（2026-10-08 用户实测暴露的 bug）
        #
        # 用户原话：「有 bug，有一次的语音没法过去，跟着下一次的语音一起过去了」。
        #
        # 原来是 `if self.state in (LISTENING, HEARING):`。看着合理，实际是错的：
        #
        #   `THINKING` 覆盖的不是"识别那一下"，而是**从松手/静音判定到模型答完**
        #   的整段时间（几秒）。而 `feed()` 在 `THINKING` 时**直接丢帧** ——
        #   于是用户在这期间说的每一句都被吞掉，只有已经进了端点缓冲的那点
        #   音频留下来，**跟下一句合并**成一句发过去。
        #
        #   表现正是用户描述的那样：**丢一句、下一句和它黏在一起**。
        #
        # ## 为什么"丢帧"是错误的抽象
        #
        # `state` 是**给界面看的**（口型、状态点：listening/hearing/thinking/
        # speaking）。把它当**采集闸门**用，就是把"显示什么"和"收不收音频"
        # 绑在一起 —— 而这两件事根本无关。
        #
        # 真正的闸门是 `_capture_enabled`（按住说话用），它**已经在上面**挡住了
        # （`if not self._capture_enabled: return None`，那个是对的，而且刻意
        # 连 VAD 都不喂，见它的说明）。
        #
        # 所以：**端点检测一直跑**。`THINKING` 只影响 `_set_state` 推给前端什么。
        #
        # ## 会不会和"宠物说话时不该偷听"冲突
        #
        # 不会，那是另一条分支：`_pet_speaking`（宠物真的在出声）时，
        # 要么按 `allow_barge_in` 打断，要么丢帧 —— 那条在下面，**保持不变**。
        # 而 `THINKING`（模型在生成、还没出声）时用户说话是**完全正常**的对话行为。
        # 到这里说明：空闲、或者在"识别/生成中"（THINKING）。
        #
        # ⚠️ **`THINKING` 也必须继续收帧**（原来会丢，是个真 bug）。
        # 宠物真正**出声**时才会走到下面那条分支（`_pet_speaking`），
        # 那里才该按 `allow_barge_in` 决定打断还是丢帧。
        if not self._pet_speaking:
            if vad.is_speech and self.state is VoiceState.LISTENING:
                await self._set_state(VoiceState.HEARING)
            utterance = self.endpointer.process(frame, vad)
            if utterance is not None:
                self.utterances += 1
                self.last_utterance = utterance
                await self._set_state(VoiceState.THINKING)
                # 识别是异步的：先返回，让调用方立刻继续读麦克风，不阻塞采集
                self._spawn(self._transcribe(utterance))
                return utterance
            return None

        # 宠物在说话且不允许打断：帧直接丢弃（不喂 VAD，避免状态被污染）
        return None

    async def force_end(self) -> Optional[Utterance]:
        """
        外部要求**立刻结束当前这句**并把已有的音频送去识别。

        ## 为什么需要它（用户要求「按下按键发声，停下结束」）

        平时的断句靠 `Endpointer` 的**静音判定**（`end_silence_ms`，默认 500ms）。
        但"按住说话"的语义是：**松开就是说完**，不该再等静音 ——
        用户按住 1 秒、说两个字、松手，那时句尾可能只有 100ms 静音，
        按静音判定根本不会触发，那句话就卡在缓冲里等下一次开口。

        ## ⚠️ 与 `feed` 的分工，以及**不能重复识别**

        松开时有两种情况：

          · 句子**还在缓冲里**（`Endpointer` 处于 SPEAKING）-> 这里主动收尾
          · 静音判定**已经抢先触发了**（用户说话中间停顿超过 `end_silence_ms`）
            -> `force_end()` 返回 None，此时**什么都不做**

        第二种情况尤其重要：如果不判断就强行再收一次，同一句话会被识别两遍，
        用户会看到宠物对同一句话回答两次。

        `_finish` 对 `reason="forced"` **不套用 `min_speech_ms`** —— 那个门槛是
        用来滤掉咳嗽/敲键盘的噪声（靠"静音判定 + 时长太短"两条一起），
        而"按住说话"里用户**明确表达了"这句说完了"**，是比时长更可靠的信号。
        所以按一下说一个"嗯"也该被识别（只是 ASR 可能返回空文本，
        那条路径在 `_transcribe` 里已经有处理）。
        """
        if not self.voice_config.enabled:
            # 🔍 2026-10-10：这条原来**静默返回 None**，排查时完全看不到。
            # 用户报"按了没反应"时，日志里只有"闸门关闭"一行，
            # 分不清是"语音总开关没开"还是"端点空"——两种完全不同的原因。
            log.warning("按住说话：松开收尾被跳过 —— 语音总开关没开（enabled=False）")
            return None
        utterance = self.endpointer.force_end()
        if utterance is None:
            # 静音判定已经收过尾（或本来就没在说）—— 不重复识别
            #
            # 🔍 2026-10-10：同样原来静默。这里把**端点的内部状态**打出来，
            # 否则"没反应"根本无从判断是哪种情况：
            #   · 门态=IDLE + 已攒=0    -> 从来没攒起过（VAD 一直判静音）
            #   · 门态=SPEAKING + 已攒>0 -> 攒着却被取走失败（不该发生，出现了就是 bug）
            log.info(
                "按住说话：松开收尾没拿到句子 —— 门态=%s 已攒语音=%dms（丢弃累计=%d）",
                self.endpointer.state.value,
                self.endpointer._speech_ms,
                self.endpointer.dropped,
            )
            return None
        self.utterances += 1
        self.last_utterance = utterance
        '''
        ## 🔴 太短就**别送识别**（2026-10-10 加，修用户报的"发出去的不对"）

        用户反馈：聊天记录里出现「请用简体中文转写。」「请用简体中文转发
        打赏支持明镜与点点栏目」「优优独播剧场——YoYo Television Series
        Exclusive」—— **全是 ASR 幻觉，被当成用户说的话发给了大脑**。

        实测日志（音频时长那一列是关键）：

            音频 1.98s  耗时  4688ms  「请用简体中文字幕。」
            音频 0.04s  耗时   625ms  「请用简体中文转写。」      <- prompt 本身
            音频 0.04s  耗时  1016ms  「请用简体中文转发 打赏支持明镜与点点栏目」
            音频 0.06s  耗时  1344ms  「请用简体中文转写。」
            音频 0.40s  耗时   828ms  「优优独播剧场——YoYo Television Series Exclusive」
            音频 0.06s  耗时  2718ms  「好」

        **0.02~0.08 秒的音频** —— 那只是按下/松开之间的几帧环境噪声。
        Whisper 对静音/极短音频的经典行为就是**吐出 prompt 或训练语料里的
        套话**（YouTube 字幕样式）。

        ## 为什么 `force_end()` 原来会放行这种音频

        `_finish()` 里对 `reason="forced"` **刻意不套 `min_speech_ms`** ——
        那是对的（"按住说话"里松手 = 用户明确说"我说完了"，比时长可靠）。

        但**切换式** PTT（按一次开始、再按一次结束，见 `GlobalShortcutPtt`）
        改变了这个前提：用户可能**按两下、根本没说话**。
        那种情况下"松手 = 说完了"这个信号**不成立**。

        所以这里补一道**只针对极端短音频**的门槛（0.3 秒）：
        · 正常说话至少几百毫秒，不会被误伤
        · 而 0.02~0.08 秒必然是"按了两下没说话"
        '''
        if utterance.duration_s < MIN_FORCED_DURATION_S:
            log.info(
                "按住说话：音频只有 %.2fs（< %.2fs）—— 当作误触，不送识别",
                utterance.duration_s,
                MIN_FORCED_DURATION_S,
            )
            if self.voice_config.enabled:
                await self._set_state(VoiceState.LISTENING)
            return None
        '''
        ## 🔴 第二个门槛：**整段太安静就不送**（2026-10-10 加）

        时长门槛只挡得住"按两下"（< 0.3s），**挡不住"按了但没说话"**。
        实测（`verify-asr-hallucination-fix.py`，历史实测脚本）：

            静音 0.05s  -> 「字幕志愿者 杨茜茜」          🔴
            静音 0.5s   -> 「优优独播剧场——YoYo…」       🔴
            静音 2.0s   -> 「优优独播剧场——YoYo…」       🔴
            真语音 4.1s -> 「桌面宠物的程序用的是Python和Electron」 ✅

        **关掉温度回退也没用** —— Whisper 对纯静音就是会造句子
        （它在训练语料里见过大量"静音 + 字幕"的配对）。

        所以只能**在送识别之前判断"这段到底有没有人说话"**。

        ## 怎么判断：用已有的 VAD 噪声底

        `self.vad` 一直在算**自适应噪声底**（`noise_floor`），阈值是
        `max(min_energy, noise_floor × energy_ratio)` —— 那就"什么算语音"
        的基准。这里直接用同一个基准判整段音频的 RMS：

            整段 RMS < max(min_energy, noise_floor × energy_ratio) -> 没人在说

        ⚠️ 用 `noise_floor` 而不是硬编码一个能量值：**环境噪声差异极大**
        （安静房间 vs 风扇旁），硬编码必然一边误杀一边漏过。

        ⚠️ **不搞"幻觉文本黑名单"** —— 那类过滤器迟早误杀真实内容
        （用户真说了"优优独播剧场"怎么办？）。从**音频**判断才是对的层级。
        '''
        import audioop as _audioop

        rms = _audioop.rms(utterance.pcm, SAMPLE_WIDTH) / 32768.0
        gate = max(self.vad.config.min_energy, self.vad.noise_floor * self.vad.config.energy_ratio)
        if rms < gate:
            log.info(
                "按住说话：整段太安静（RMS %.5f < 门槛 %.5f，音频 %.2fs）"
                "—— 没人在说，不送识别（Whisper 对静音会造句子）",
                rms,
                gate,
                utterance.duration_s,
            )
            if self.voice_config.enabled:
                await self._set_state(VoiceState.LISTENING)
            return None
        await self._set_state(VoiceState.THINKING)
        # 与 `feed` 里那条完全同路：异步识别，不阻塞采集
        self._spawn(self._transcribe(utterance))
        log.info("按住说话：松开 -> 主动收尾（%.2fs，%s）", utterance.duration_s, utterance.end_reason)
        return utterance

    async def set_capture(self, enabled: bool) -> None:
        """
        开关**采集闸门**（按住说话用）。

        ## ⚠️ 为什么必须有它（2026-10-07 用户实测暴露）

        用户按下说话、松开之后，日志里开始每 30 秒自己冒一条：

            语音识别（30.00s，耗时 0ms）：你刚才说的那段话我听清了，我们聊聊吧

        `30.00s` 正是 `Endpointer` 的**保险丝**（`max_utterance_ms`）。
        原因：松开按键**只是不再说话，麦克风并没有关** —— 端点在未按住的
        那 30 秒里把环境噪声（被 VAD 判成语音）攒满了缓冲，触发保险丝，
        于是一整段"根本没人在说"的音频被当成一句话发去识别。

        **语义上这也是错的**：按住说话应当是"**只有按住时才听**"。
        不按住的时候偷听、还自己冒话，用户会觉得宠物在自言自语。

        ## 与 `voice.control` 的分工

          · `voice.control`  -> 开关**麦克风**（会付权限提示与 AudioContext 开销）
          · 这个方法        -> 开关**采集闸门**（麦克风开着，但要不要这段音频）

        两者分开是刻意的：按住说话可能一秒按好几次，每次开关麦克风会很卡；
        而"这一段要不要"必须每按一次切换一次。

        ## 关闭时**丢弃**半句而不是收尾

        关闸门时如果端点正在 SPEAKING，那半句要**丢掉**、不发去识别。
        因为松开按键的那一刻已经 `force_end()` 过了 —— 这里再收一次就是
        刚刚警告过的"同一句话识别两遍"。丢弃 + `reset()` 让下一句从干净状态开始。

        ## ⚠️ 这里**不能**按"值没变就早退"（2026-10-08 实测抓到的真 bug）

        原来这里写的是：

            if enabled == self._capture_enabled:
                return

        看着像个无害的优化，实际是个真 bug：`_capture_enabled` **默认就是 `True`**
        （免提模式要一直听），所以**第一次按下按住说话时** `enabled == True`
        —— 早退，下面那整段 PTT 逻辑（关掉静音断句）**一次都不执行**。

        后果：PTT 模式下的断句行为和免提完全一样 —— 用户唱歌/念长句时
        照样被 500ms 静音切成好几段、各自回复（「消息好像传输了两次」）。

        **为什么它没被早发现**：早退是"静默"的 —— 不报错、不改状态，
        表现只是"这个优化没生效"，而那和"本来就没这功能"看起来一样。
        是 `check-ptt-segmentation.py`（历史实测脚本）把两种模式并排跑才暴露的
        （免提 5 句、PTT 也 5 句 —— 一模一样就说明改动没生效）。

        所以：**状态设置要幂等**（下面用 `_ptt_mode` 判断该不该改），
        而不是靠早退跳过。
        """
        self._capture_enabled = enabled
        """
        ## ⚠️ 这里**必须留一条 INFO 日志**（2026-10-10 加）

        用户报「没按按键他都能听见我的声音」时，我查了很久 ——
        因为**闸门的状态完全看不到**：它只在 DEBUG 级有痕迹，
        而正常运行时日志是 INFO，那条根本不出现。

        于是"闸门到底开没开"只能靠读代码推断，而这次恰恰是代码本身就错了
        （默认 `True` + 前端从不在启动时关它）。**错了的东西推断不出来。**

        有了这条，`python.log` 里能直接看到每次闸门变化 ——
        下次同类问题一眼就能定位。
        """
        log.info(
            "采集闸门：%s（%s）",
            "开 —— 会处理音频" if enabled else "关 —— 一个字节都不处理",
            "按住说话" if self._ptt_mode else "免提模式",
        )
        if enabled:
            # 开闸门前先复位：上一轮可能留下了半句残留（比如超时路径没走完）
            self.endpointer.reset()
            """
            ## ⚠️ 按住说话时**关掉"静音判定断句"**（2026-10-08 用户实测）

            用户唱歌时反馈「消息好像传输了两次」。查日志发现**不是重复发送**，
            而是**同一段演唱被切成了多句、各自识别、各自回复**：

                17:45:41  语音识别（2.16s）：要不来,要不要来一起唱歌啊?
                17:45:58  语音识别（4.02s）：白菜鸡毛菜,空心菜又卖菜。

            宠物把第二句听成了"菜市场顺口溜"，于是回「又是两遍…」。

            根因：`end_silence_ms` 默认 **500ms** —— 那个值是为"说完停一下"
            设计的，但**唱歌、念长句、思考性停顿**都会超过它，
            于是句子被**切碎**，每段各触发一次回复。而 `allow_barge_in`
            让后一段把前一段的回复**打断重说**，观感就是"说了两遍"。

            ## 修法：PTT 模式下只用"松手"断句

            `force_end()` 在松手时已经会主动收尾（那是**用户明确的意图**），
            比"猜他停顿了"可靠得多。所以按住说话期间：
              · 不再按静音切句（把 `end_silence_ms` 设成无穷大）
              · 只在松手 / 保险丝触发时才收尾

            ## ⚠️ 只影响 PTT 模式，不碰免提模式

            免提模式（不按键、直接说话）**必须**保留静音断句 —— 那里没有
            "松手"这个信号，只能靠静音。所以这里改的是**实例级**的端点配置，
            而不是全局默认值。
            """
            self._ptt_mode = True
            self.endpointer.config.end_silence_ms = 10**9
        else:
            # 关闸门时恢复静音断句（免提模式要用）
            if self._ptt_mode:
                self._ptt_mode = False
                self.endpointer.config.end_silence_ms = self._silence_ms_default
            partial = self.endpointer.force_end()
            if partial is not None:
                # 松手时漏收的半句（正常路径下 force_end 已经收过了，这里是兜底）
                self.utterances += 1
                self.last_utterance = partial
                log.info(
                    "按住说话：关闸门时补收半句（%.2fs）",
                    partial.duration_s,
                )
                self._spawn(self._transcribe(partial))
            # 无论有没有补收，都复位端点：下一轮从 IDLE 干净开始
            self.endpointer.reset()
        log.info("按住说话：采集闸门 %s", "打开" if enabled else "关闭")

    def _spawn(self, coro: Any) -> asyncio.Task[Any]:
        """
        起一个后台任务，并**保住强引用**（见 `_tasks` 那段说明）。

        ⚠️ 不要直接写 `asyncio.get_running_loop().create_task(...)` ——
        那样任务可能被 GC 回收、中途消失，且不报错。

        用这个包装函数是刻意的：把"必须保引用"这件事收在一处，
        以后加新的后台任务时不容易漏（漏了的代价是静默失效，最难查）。
        """
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def _transcribe(self, utterance: Utterance) -> None:
        """
        识别一句话并发布结果。失败只记日志，不影响下一次监听。

        ## ⚠️ **必须串行**：并发识别会让对话顺序错乱（2026-10-08 加）

        `feed()` 现在能在"上一句还在识别/模型还在生成"时继续切出下一句
        （那正是修掉"丢一句、和下句黏在一起"的关键）。但切出来的每一句都
        走 `_spawn(self._transcribe(...))` —— **异步任务**，谁先跑完谁先发布。

        于是：用户说"第一句"（长）、"第二句"（短），第二句可能**先**识别完，
        `user.voice` 先发第二条 —— 对话里顺序就反了：

            用户：第二句     ← 其实是后说的
            小灵：……
            用户：第一句     ← 其实是先说的

        对话历史、记忆库、情绪都会跟着错。**这是修 bug 引入的新风险**，
        跑 `check-ptt-segmentation.py`（历史实测脚本）时想到的（那里两句都切出来了，
        但没验顺序）。

        ## 为什么用锁串行而不是排队列

        队列要额外管"识别失败怎么办、任务丢了怎么办"；而本地 ASR 本来就有
        `threading.Lock` 串行（`LocalAsrEngine._lock`），云端 ASR 单句也就
        几百毫秒。**串行的代价接近零，换来的是顺序确定性** —— 这里显然该选确定性。
        """
        started = self._clock()
        """
        ## ⚠️ 识别**必须**有超时（2026-10-10 加）

        用户报「按住 alt 按键没用」。查下来按键、闸门、音频全是对的 ——
        真正的问题是**识别等太久**，实测：

            松开 -> 主动收尾（6.16s，forced）
            15.5 秒后 -> 语音识别（6.16s，耗时 15547ms）

        15 秒里**没有任何反馈**（宠物窗口不聚焦时连"识别中…"都看不到），
        用户当然认为坏了。

        ### 为什么原来没有超时

        云端 ASR 走 httpx，`ASR_TIMEOUT_S` 那一层有自己的连接超时 ——
        但它**只覆盖连接建立**，不覆盖"连上了、服务端慢慢算"。
        服务端排队时就是干等，实测见过 15.5 秒。

        ### 超时取多少

        `20s + 2×音频时长`，上限 60s：
          · 底 20s 给"短音频但服务端排队"
          · 长音频（用户按住说 30 秒）等比放宽 —— 硬编码一个固定值会让
            长音频必然超时
          · 上限 60s 兜底，避免真的卡死

        ### 超时之后做什么

        **如实报错**，不静默重试：
          · 重试会把同一句话识别两遍 -> 宠物回答两次（`force_end` 那段注释
            警告过的同一个坑）
          · 用户需要知道"这句没听清，再说一次"，而不是干等
        """
        timeout_s = min(60.0, 20.0 + 2.0 * max(0.0, utterance.duration_s))
        log.info(
            "开始识别：音频 %.2fs，超时上限 %.0fs",
            utterance.duration_s,
            timeout_s,
        )
        try:
            async with self._transcribe_lock:
                transcript = await asyncio.wait_for(
                    self.asr.transcribe(utterance.pcm, self.voice_config.sample_rate),
                    timeout=timeout_s,
                )
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            # ⚠️ 单独认出来：`str(TimeoutError())` 是**空字符串**，
            # 走下面那条通用分支会记成"语音识别异常，消息为空"——
            # 那正是 2026-10-09 在 TTS 侧踩过的同一个坑（见 `speak` 的注释）。
            log.warning(
                "识别超时：%.0fs 内没返回（音频 %.2fs）—— 这句丢弃，请再说一次",
                timeout_s,
                utterance.duration_s,
            )
            transcript = Transcript(
                source=getattr(self.asr, "name", "asr"),
                error=f"识别超时（{timeout_s:.0f} 秒没返回）",
            )
        except Exception as exc:  # noqa: BLE001 —— 识别失败不能拖垮会话
            log.exception("语音识别异常")
            transcript = Transcript(source=getattr(self.asr, "name", "asr"), error=str(exc))

        self.last_transcript = transcript
        if transcript.error:
            log.warning("识别失败：%s", transcript.error)
            await self.bus.publish(
                "system.error",
                {"level": "warn", "message": f"语音识别失败：{transcript.error}", "where": "asr"},
            )
            if self.voice_config.enabled:
                await self._set_state(VoiceState.LISTENING)
            return

        if not transcript.text.strip():
            # 识别出空文本（多为环境噪声）：安静回到监听，不要给用户一个空回复
            log.debug("识别结果为空，忽略（时长 %.2fs）", utterance.duration_s)
            if self.voice_config.enabled:
                await self._set_state(VoiceState.LISTENING)
            return

        self.transcripts += 1
        log.info(
            "语音识别（%.2fs，耗时 %.0fms）：%s",
            utterance.duration_s,
            (self._clock() - started) * 1000,
            transcript.text,
        )
        await self.bus.publish(
            "user.voice",
            {
                "text": transcript.text,
                "final": transcript.final,
                "duration_s": round(utterance.duration_s, 3),
                "confidence": transcript.confidence,
                "source": transcript.source,
            },
        )

        """
        ## ⚠️ 识别成功后**必须回到 LISTENING**（2026-10-08 修，是个真 bug）

        原来这里**没有**回到 LISTENING —— 只有"识别失败"和"识别出空文本"
        两条分支才回。于是 `VoiceSession.state` 在第一次成功识别后**永远停在
        THINKING**。

        后果非常严重，因为 `feed()` 开头有这么一句：

            if self.state in (VoiceState.LISTENING, VoiceState.HEARING):
                ...  # 端点检测、切句

        **状态停在 THINKING 时，之后所有音频帧被静默丢弃** ——
        表现就是"**说第一句话它听懂了，之后再也听不见**"。
        而且不报错、不记日志，极难归因。

        ### 怎么发现的

        写 `check-voice-flow.py`（历史实测脚本）（逐帧喂真实音频）时暴露的：
        三条音频里只有第一条被识别，后两条毫无反应。而单独跑端点检测，
        三条**都能正确切句**（切出 1 / 2 / 5 句）—— 两边一对照就锁定了
        "不是端点的问题，是 feed() 根本没被走到"。

        ### 为什么线上没暴露

        前端每次说话前会重新 `voice.control` 把 enabled 打开一次，
        `set_enabled()` 会 `_set_state(LISTENING)` —— 把状态顺带修正了。
        所以只在"一次启用、连续说多句"时会犯，比如**按住说话**那个模式
        （按一次开闸、说一句、松手，第二次按住时 state 可能还停在 THINKING）。

        ### 顺序：先回到 LISTENING，再交给对话

        放最后一行而不是中间：`user.voice` 一发布，对话那边就会开始生成，
        中途可能推 `voice.state`（SPEAKING）。如果我们在那之后才回 LISTENING，
        会把 SPEAKING 覆盖掉、口型状态就乱了。
        """
        if self.voice_config.enabled:
            await self._set_state(VoiceState.LISTENING)

    # ------------------------------------------------------------------ 主循环

    async def run_forever(self) -> None:
        """持续从音频输入读帧并处理。输入为空（没有麦克风）时退化为低速轮询。"""
        log.info(
            "语音会话启动：asr=%s tts=%s 打断=%s",
            getattr(self.asr, "name", "?"),
            getattr(self.tts, "name", "?"),
            "允许" if self.voice_config.allow_barge_in else "禁止",
        )
        while True:
            try:
                if not self.voice_config.enabled:
                    await asyncio.sleep(0.2)
                    continue
                frame = self.audio_in.read()
                if frame is None:
                    # 没有音频源：不要空转烧 CPU，也不要报错刷屏
                    await asyncio.sleep(0.05)
                    continue
                await self.feed(frame)
            except asyncio.CancelledError:
                log.info("语音会话已停止")
                raise
            except Exception:  # noqa: BLE001
                log.exception("语音单帧处理异常（已忽略，继续）")
                await asyncio.sleep(0.05)

    # ------------------------------------------------------------------ 合成（P3 的说话侧）

    async def speak(self, text: str, say_id: str) -> None:
        """
        合成并播放一句话，同时把音量包络发给前端做口型。

        包络是从音频算出来的**真实**数据，不是随机数 —— P1 的口型联动因此从
        "看起来在动"升级为"确实跟着音量在动"。
        """
        if not text.strip():
            return
        try:
            payload, fmt = await self.tts.synthesize(text)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            # ⚠️ 不能只打 `%s`（2026-10-09 踩到）。
            #
            # 本地路线失败时日志里只有一行 `语音合成失败：` —— 后面**什么都没有**，
            # 因为异常的消息是空的（`RuntimeError('')`）。那种日志等于没打：
            # 分不清是超时、连接断了、还是子进程返回了空。
            #
            # 所以：消息为空时至少要留下**异常类型名**，永远不留空行。
            detail = str(exc).strip() or f"（{type(exc).__name__}，没有附带消息）"
            log.warning("语音合成失败：%s", detail)
            """
            ## 🔴 合成失败要**告诉主进程**（2026-10-15 加）

            ### 为什么

            用户报「宠物发不了声音」+「也回复不了信息」，查出来是**叠加**：

            ```
            ① edge-tts 被微软 403 挡了（合成失败）
            ② 沉浸模式**故意不显示文字** —— 它按"TTS 开着"判断，以为有语音
               结果：文字被主动隐藏，语音又实际失败了 -> 两头都收不到
            ```

            ⚠️ 关键在于**主进程不知道合成失败了** —— 它只能看配置
            （`tts.enabled: true`），于是认为"有语音"，继续隐藏文字。

            所以这里要**发一个明确的失败信号**出去，让主进程据此把那一条
            降级成"显示文字"。

            ⚠️ 只发带原因的**文本消息**，不带音频 —— 失败时本来就没音频。
            ⚠️ 用 `broadcast`（终点是主进程自己），和 `voice.audio` 同一条路。
            """
            try:
                await self.bus.publish(
                    "voice.tts_failed", {"reason": detail[:200]}
                )
            except Exception:  # noqa: BLE001
                # 连上报都失败时**不能影响对话** —— 这只是一种降级信号
                log.debug("上报 TTS 失败信号时又出错（忽略）")
            return

        envelope = compute_envelope(payload, fmt)
        if envelope:
            await self.bus.publish("pet.mouth", {"id": say_id, "envelope": envelope})
        await self.audio_out.play(payload, fmt, text)

    def stats(self) -> dict[str, Any]:
        vad = self.vad.last
        return {
            "enabled": self.voice_config.enabled,
            "state": self.state.value,
            "asr": getattr(self.asr, "name", "?"),
            "tts": getattr(self.tts, "name", "?"),
            "utterances": self.utterances,
            "transcripts": self.transcripts,
            "barge_ins": self.barge_ins,
            "dropped": self.endpointer.dropped,
            "truncated": self.endpointer.truncated,
            "noise_floor": round(self.vad.noise_floor, 5),
            "threshold": round(self.vad.threshold(), 5),
            "last_energy": round(vad.energy, 5) if vad else None,
        }


def compute_envelope(payload: bytes, fmt: str, buckets: int = 24) -> list[float]:
    """
    从音频算出音量包络（0~1 的序列），供立绘做口型。

    只对裸 PCM 有意义：MP3 之类的压缩格式没法直接算，返回空列表让前端
    退回它自己的估算法（渲染层本来就有基于时间的占位实现）。
    这样"有 TTS 时用真包络、没有时用占位"是自动的，不需要额外开关。
    """
    if fmt != "pcm" or not payload:
        return []
    try:
        return [round(value, 3) for value in _rms_series(payload, buckets)]
    except Exception:  # noqa: BLE001
        return []


def _rms_series(pcm: bytes, buckets: int) -> list[float]:
    """把 PCM 切成若干段，每段算一个归一化 RMS。"""
    if buckets <= 0:
        return []
    limit = len(pcm) - (len(pcm) % SAMPLE_WIDTH)
    if limit <= 0:
        return []
    chunk = max(SAMPLE_WIDTH * 8, limit // buckets)
    values: list[float] = []
    for start in range(0, limit, chunk):
        piece = pcm[start : start + chunk]
        if len(piece) < SAMPLE_WIDTH:
            break
        values.append(min(1.0, audioop.rms(piece, SAMPLE_WIDTH) / 32768.0))
    return values[:buckets]


# --------------------------------------------------------------------------- #
# 引擎工厂：按配置挑实现，缺依赖时安全降级到 mock
# --------------------------------------------------------------------------- #


def _prepare_cuda_dll_dirs() -> list[str]:
    """
    把 pip 装的 NVIDIA 运行库加进 **DLL 搜索路径**（Windows 专用坑）。

    ## ⚠️ `cublas64_12.dll is not found` —— **装了但找不到**，不是没装

    `pip install nvidia-cublas-cu12` 把 DLL 放在

        .venv/Lib/site-packages/nvidia/cublas/bin/

    而 **Windows 默认不从那里找 DLL**（Linux 的 rpath 机制这边不存在）。
    于是 ctranslate2 报：

        RuntimeError: Library cublas64_12.dll is not found or cannot be loaded

    这个报错**看起来像"没装"**，很容易让人去重装一遍 —— 实测踩过。
    正确做法是 `os.add_dll_directory()`（Python 3.8+ 的官方途径）。

    ## ✅ 实测：**不需要 cuDNN**

    一开始判断"要连 cuDNN 一起装"（那就多 743MB）。实测只装 cuBLAS 就能跑：

        >>> WhisperModel('tiny', device='cuda', compute_type='float16')
        ✅ CUDA 加载成功

    原因：`faster-whisper` 用 **CTranslate2**，有自己实现的算子，
    不依赖 cuDNN 那套卷积库。
    """
    if os.name != "nt":  # pragma: no cover —— 非 Windows 不需要这一步
        return []
    base = Path(sys.prefix) / "Lib" / "site-packages" / "nvidia"
    if not base.is_dir():
        return []
    dirs = [str(d / "bin") for d in sorted(base.iterdir()) if (d / "bin").is_dir()]
    for d in dirs:
        try:
            os.add_dll_directory(d)
        except OSError:  # pragma: no cover
            pass
    if dirs:
        os.environ["PATH"] = os.pathsep.join(dirs) + os.pathsep + os.environ.get("PATH", "")
    return dirs


class LocalAsrEngine:
    """
    本地语音识别（`faster-whisper` + CTranslate2）。**离线、免费、语音不出本机。**

    ## 实测选型（RTX 2070 8GB / float16，三条中文音频 2.32s / 6.73s / 14.05s）

    | 模型 | 显存 | 2.3s | 6.7s | 14s | 中文质量 |
    |---|---|---|---|---|---|
    | tiny | 0.4GB | 125ms | 391ms | 1531ms | ❌ `桌面宠物`→`桌面**重物**` |
    | small | 0.9GB | 187ms | 406ms | 1750ms | ⚠️ 偶尔错字 |
    | medium | 1.5GB | 250ms | 516ms | 1172ms | ✅ 但输出繁体 |
    | **large-v3-turbo** | **1.6GB** | 297ms | **359ms** | **656ms** | ✅ **最好，且简体** |

    所以默认用 `large-v3-turbo` —— 它**又准又快**（比 small 快一倍）。

    **小模型中文根本不能用**（`桌面宠物` → `桌面重物`）—— 这是实测结论，
    不是推测。所以不要把默认值调小。

    ## ⚠️ 三条降级路径（都不抛异常，返回带 error 的 Transcript）

      ① `faster_whisper` 没装        -> 构造时抛错 -> 工厂降级为 mock
      ② CUDA 不可用（没显卡/没 DLL） -> 自动退 CPU
      ③ 模型权重不在本地            -> **真去下载**（首次会等，但那是对的）

    ## ⚠️ 为什么不用 `asyncio.to_thread` 之外的并发控制

    CTranslate2 的 `WhisperModel.transcribe` **不是线程安全的**，而且模型加载
    很贵（1.6GB）。所以这里：
      · 模型**懒加载一次**、常驻
      · 用一把 `threading.Lock` 串行化 —— 语音是"一句一句"的，
        天然不会并发，加锁只是防止意外重入把 CTranslate2 搞崩

    ## ⚠️ 首次识别会慢（要下模型）

    如果模型不在缓存里，第一次 `transcribe` 会去 huggingface 下 1.6GB。
    那期间**界面看起来像卡住**。所以：
      · `available()` 只检查依赖在不在（不检查模型）
      · 构造时如果模型没缓存，**记一条明显的日志**
      · `ensure_model_cached()` 给外部（设置页）一个"先下好"的入口
    """

    name = "local"

    def __init__(
        self,
        model: str = "large-v3-turbo",
        device: str = "auto",
        compute_type: str = "",
        model_dir: Optional[Path] = None,
        language: str = "zh",
        beam_size: int = 1,
        cpu_threads: int = 0,
        initial_prompt: str = SIMPLIFIED_PROMPT,
        convert_to_simplified: bool = True,
    ) -> None:
        self.model_name = model
        self.language = language
        # ⚠️ 贪心解码（beam=1）：按住说话要的是**快**，不是极致准。
        # 实测 beam=1 已经全对，没必要为边际精度付倍数的延迟。
        self.beam_size = max(1, beam_size)
        self.model_dir = Path(model_dir) if model_dir else None
        self.cpu_threads = max(0, cpu_threads)
        """
        `initial_prompt`：引导模型**用简体输出**。

        ## 为什么需要（用户实测反馈 2026-10-08）

        「转译语音竟然是繁体字」—— Whisper 系模型的中文输出会混繁体
        （训练语料里繁体占比高）。实测：

            medium  无 prompt -> '今天天氣不錯'
            medium  有 prompt -> '今天天气不错。'   ← 还顺带有了标点

        ## ⚠️ 但它**不保险**

        模型可能不听（唱歌、口音重、噪声大时尤其）。所以还有第二道
        `convert_to_simplified` 后处理兜底 —— **两道一起才稳**：
        提示词管"尽量别产生繁体"，后处理管"产生了也给你掰回来"。
        """
        self.initial_prompt = initial_prompt
        """
        `convert_to_simplified`：把识别结果里的繁体换成简体（确定性兜底）。

        用 Unicode Unihan 数据生成的表（`t2s_table.py`，6500+ 条），
        没有运行时依赖。转换极快（实测一句话 < 0.02ms），可忽略不计。
        """
        self.convert_to_simplified = convert_to_simplified
        self.calls = 0
        self._model: Any = None
        self._lock = threading.Lock()
        self._device = ""
        self._compute_type = ""
        # 用户/配置想要什么；实际用什么由 `_load()` 决定（可能要降级）
        self._want_device = device
        self._want_compute = compute_type

    # ------------------------------------------------------------------ 探测

    def _import(self) -> Any:
        """导入 faster_whisper；没装就抛（由工厂接住并降级）。"""
        import faster_whisper  # noqa: F401

        return faster_whisper

    def _cuda_usable(self) -> bool:
        """
        探测 CUDA 到底能不能用。

        ⚠️ 不能只看 `get_cuda_device_count() > 0` —— 那**只说明显卡在**，
        不代表 ctranslate2 能找到 cublas DLL。实测踩过：
        设备数报 1，但一 transcribe 就 `cublas64_12.dll is not found`。
        所以这里同时要求 **DLL 目录能建出来**（Windows 上的必要条件）。
        """
        try:
            import ctranslate2

            if ctranslate2.get_cuda_device_count() <= 0:
                return False
        except Exception:  # noqa: BLE001
            return False
        if os.name == "nt":
            dirs = _prepare_cuda_dll_dirs()
            # 有 nvidia 包但没加进路径 -> 一定失败；这里已经加过了，所以只要非空就认为可试
            return bool(dirs)
        return True

    def ensure_model_cached(self) -> bool:
        """
        模型权重在不在本地（**不触发下载**）。

        给界面用：切换模型前先问一句"这个模型下过吗"，没下过就提示首次要等。

        ## ⚠️ 不能硬编码发布者（实测踩到）

        我第一版写的是：

            models--Systran--faster-whisper-{model}

        但 huggingface 的缓存目录是 `models--<org>--<name>`，而**不同模型
        的 org 不一样**。实测：

            large-v3-turbo  ->  models--**mobiuslabsgmbh**--faster-whisper-large-v3-turbo
            small / medium  ->  models--**Systran**--faster-whisper-small

        硬编码 `Systran` 的结果是：turbo **明明下好了**却报"还没下载过" ——
        一个只会误导人的提示。

        ## 所以改成**按后缀匹配**

        只要目录名以 `--faster-whisper-<model>` 结尾就算命中，
        不关心 org 是谁。同时也支持用户填完整仓库名（`org/name`）的情况。
        """
        if self.model_dir is None or not self.model_dir.is_dir():
            return False
        want = self.model_name.strip()
        suffixes = {
            f"--faster-whisper-{want}",
            f"--{want}",
            f"--{want.replace('/', '--')}",
        }
        for entry in self.model_dir.iterdir():
            if not entry.is_dir() or not entry.name.startswith("models--"):
                continue
            if any(entry.name.endswith(s) for s in suffixes):
                # 光有目录不算，得真有快照内容（下到一半会留空目录）
                snapshots = entry / "snapshots"
                if snapshots.is_dir() and any(snapshots.iterdir()):
                    return True
        return False

    def available(self) -> bool:
        """只检查**依赖**在不在（不检查模型，也不加载 —— 加载很贵）。"""
        try:
            self._import()
            return True
        except Exception:  # noqa: BLE001
            return False

    # ------------------------------------------------------------------ 模型

    def _load(self) -> Any:
        """
        懒加载模型（只做一次）。

        ## 设备降级的顺序

          `auto` -> 先试 cuda，不行退 cpu
          `cuda` -> 试 cuda，不行**也退 cpu**（而不是让语音彻底不可用）
          `cpu`  -> 直接用 cpu

        **为什么 `cuda` 也要退**：用户的显卡出问题（驱动更新、显存被占）时，
        "语音完全不能用"比"慢一点但能用"糟糕得多。日志会说清楚降级了。
        """
        if self._model is not None:
            return self._model
        whisper = self._import()
        from faster_whisper import WhisperModel

        """
        ## ⚠️ 必须在构造模型**之前**设 `HF_HOME`（2026-10-08 修，卡了很久的坑）

        `WhisperModel(..., download_root=...)` **只管"下载到哪"，不管"去哪找"**。
        查找缓存用的是 `huggingface_hub` 的全局配置，默认 `~/.cache/huggingface`。
        于是：

          · 我们明明把模型下在了 `apps/desktop/pet-data/asr-models/`
          · 加载时它却去 `~/.cache/huggingface` 找 -> 找不到
          · 找不到就去**联网**拉 -> 这台机器 `huggingface.co` 不通
          · `ConnectTimeout`，而且**要卡几十秒才报错**

        实测症状极具迷惑性 —— `transcribe` 开始打印：

            Processing audio with duration 00:01.840   （6.73s 的音频只处理了一小段）

        然后**永远不返回**。看起来像"识别卡住"，实际在等网络超时。

        `tools/bench-asr.py` 一开始就设了 `HF_HOME`（所以那边一直好用），
        产品代码漏了 —— 典型的"**基准工具好用、产品不好用**"分叉。
        """
        if self.model_dir is not None:
            self.model_dir.mkdir(parents=True, exist_ok=True)
            os.environ["HF_HOME"] = str(self.model_dir)
            os.environ.setdefault("HUGGINGFACE_HUB_CACHE", str(self.model_dir / "hub"))
            # 真要下模型时走镜像（`HF_HUB_OFFLINE` 没设时才生效）
            os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

        """
        ## ⚠️⚠️ 关键：`HF_HUB_OFFLINE=1` —— 不然冷启动白等 27 秒

        实测（这台机器，模型**已经在本地缓存里**）：

            [_load] 27.09s     <- 加载
            [_run]   27.72s    <- 其中识别只占 0.6s
            第二次：[_load] 0.00s  [_run] 0.41s   ✅

        那 27 秒**不是加载模型**，是 `huggingface_hub` 在**联网检查
        "这个模型有没有新版本"** —— 而 `huggingface.co` 在这台机器上不通，
        于是它一路重试直到超时，然后才回退用本地缓存。

        表现是"**第一次说话要等半分钟才出字**"，第二次就好了 ——
        很容易被误判成"模型加载慢"或者"显卡冷启动"。

        `HF_HUB_OFFLINE=1` 让 huggingface_hub **完全跳过网络**、直接用缓存。
        这正是我们要的语义：**离线识别就不该联网**。

        ## 代价与补偿

        设了它之后，**没缓存过的模型就下不下来了**（会直接报"找不到"）。
        所以：
          · 只有 `ensure_model_cached()` 为真（模型已在本地）时才设 OFFLINE
          · 没缓存时保持联网（走 hf-mirror 下载），并保留那句"首次要下载"的提示

        这样"已下好的模型秒开"和"没下过的能下"两件事都成立。
        """
        if self.model_dir is not None and self.ensure_model_cached():
            os.environ["HF_HUB_OFFLINE"] = "1"
        else:
            os.environ.pop("HF_HUB_OFFLINE", None)

        want = (self._want_device or "auto").lower()
        attempts: list[tuple[str, str]] = []
        if want == "cpu":
            attempts.append(("cpu", self._want_compute or "int8"))
        else:
            if self._cuda_usable():
                attempts.append(("cuda", self._want_compute or "float16"))
            else:
                log.info("本地 ASR：CUDA 不可用（没显卡或缺 cublas DLL），将用 CPU")
            attempts.append(("cpu", self._want_compute or "int8"))

        last_error: Optional[Exception] = None
        for device, compute in attempts:
            try:
                kwargs: dict[str, Any] = {
                    "device": device,
                    "compute_type": compute,
                }
                if self.model_dir is not None:
                    self.model_dir.mkdir(parents=True, exist_ok=True)
                    kwargs["download_root"] = str(self.model_dir)
                if self.cpu_threads:
                    kwargs["cpu_threads"] = self.cpu_threads
                log.info(
                    "本地 ASR：正在加载 %s（%s / %s）…%s",
                    self.model_name,
                    device,
                    compute,
                    ""
                    if self.ensure_model_cached()
                    else f"（模型未缓存，将从 hf-mirror 下载到 {self.model_dir}，约 1.5GB，可能要几分钟）",
                )
                self._model = WhisperModel(self.model_name, **kwargs)
                self._device = device
                self._compute_type = compute
                log.info("本地 ASR：就绪 —— %s @ %s/%s", self.model_name, device, compute)
                return self._model
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                log.warning("本地 ASR：以 %s/%s 加载失败（%s），换下一种", device, compute, exc)
        raise RuntimeError(f"本地 ASR 加载失败：{last_error}") from last_error

    # ------------------------------------------------------------------ 识别

    async def transcribe(self, pcm: bytes, sample_rate: int = SAMPLE_RATE) -> Transcript:
        started = time.monotonic()
        try:
            text = await asyncio.to_thread(self._run, pcm, sample_rate)
        except Exception as exc:  # noqa: BLE001 —— 识别失败不能拖垮会话
            return Transcript(
                source=self.name,
                elapsed_ms=(time.monotonic() - started) * 1000,
                error=f"{type(exc).__name__}: {exc}",
            )
        self.calls += 1
        return Transcript(
            text=text.strip(),
            source=self.name,
            elapsed_ms=(time.monotonic() - started) * 1000,
        )

    def _run(self, pcm: bytes, sample_rate: int) -> str:
        import numpy as np

        # 裸 16bit PCM -> float32 [-1,1]。**必须传 ndarray**：
        # 传 list/bytes 时 faster-whisper 会当成**文件路径**交给 PyAV，
        # 报出一个完全无关的 `TypeError: open() got an unexpected keyword
        # argument 'metadata_errors'`（1.2.1 与 av 19 的版本不兼容）。
        audio = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0

        # 采样率不是 16k 时重采样（产品链路恒为 16k，这里是兜底）
        if sample_rate != SAMPLE_RATE:
            ratio = SAMPLE_RATE / float(sample_rate)
            out_len = int(len(audio) * ratio)
            idx = np.arange(out_len, dtype=np.float64) / ratio
            lo = np.floor(idx).astype(np.int64)
            hi = np.minimum(lo + 1, len(audio) - 1)
            frac = (idx - lo).astype(np.float32)
            audio = (audio[lo] * (1 - frac) + audio[hi] * frac).astype(np.float32)
        audio = np.ascontiguousarray(audio, dtype=np.float32)

        with self._lock:  # CTranslate2 的 transcribe 不是线程安全的
            model = self._load()
            # 🔴 关掉**温度回退**（2026-10-10 加，修 ASR 幻觉）
            #
            # faster-whisper 默认 `temperature=[0.0, 0.2, …, 1.0]` —— 解码置信度低时
            # 会**依次升温重试**，而升温解码正是"凭空造句"的主因（用户看到的
            # 「请用简体中文转写。」「优优独播剧场——YoYo Television Series
            # Exclusive」就是它造的）。
            #
            # 那段回退是为"有语音但听不清"设计的，而本项目的输入端**已经在前面
            # 用 VAD + 端点检测筛过了**（现在还有 `MIN_FORCED_DURATION_S`
            # 挡极短音频），剩下的都是"真的有人在说"。所以不需要它，
            # 而它的代价（幻觉）远大于收益。
            #
            # ⚠️ `compression_ratio_threshold` / `log_prob_threshold` /
            # `no_speech_threshold` 一起关掉：那三个是**配合温度回退**用的触发条件，
            # 温度只有一个值时它们没有意义 —— 留着反而会在"低置信度"时
            # 直接判失败、返回空文本（那是另一种"说了没反应"）。
            segments, _info = model.transcribe(
                audio,
                language=self.language or None,
                beam_size=self.beam_size,
                # 端点检测在前端/Endpointer 已经做过，别做两遍
                vad_filter=False,
                # 短句之间不该互相影响（这里每次都只送一句）
                condition_on_previous_text=False,
                temperature=0.0,
                compression_ratio_threshold=None,
                log_prob_threshold=None,
                no_speech_threshold=None,
                # `initial_prompt` 默认留空 —— 见 config/persona.yaml 里的长说明
                # （实测它会被原样吐回来当识别结果）
                initial_prompt=self.initial_prompt or None,
            )
            text = "".join(seg.text for seg in segments)

        if self.convert_to_simplified:
            # 确定性兜底：提示词可能没管住，这一步保证结果一定是简体
            try:
                from .t2s_table import to_simplified

                text = to_simplified(text)
            except Exception:  # noqa: BLE001 —— 转换失败不该让识别失败
                log.debug("繁->简转换失败（已忽略）", exc_info=True)
        return text


class OpenAIAsrEngine:
    """
    OpenAI 兼容的语音识别（/audio/transcriptions）。

    很多云厂商（OpenAI、硅基流动、火山的部分接口）都吃这个协议，
    所以写一份就能覆盖一批。上传的是 WAV（PCM 外面套个 44 字节头，
    标准库 wave 模块就能生成），不是在传我们自己内部的裸 PCM。
    """

    name = "openai"

    def __init__(self, base_url: str, model: str, api_key: str, timeout_s: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout_s = timeout_s
        self.calls = 0

    def available(self) -> bool:
        return bool(self.base_url and self.model and self.api_key)

    async def transcribe(self, pcm: bytes, sample_rate: int = SAMPLE_RATE) -> Transcript:
        started = time.monotonic()
        if not self.available():
            return Transcript(source=self.name, error="语音识别未配置完整（缺 base_url/model/key）")
        try:
            text = await asyncio.to_thread(self._request, pcm, sample_rate)
        except Exception as exc:  # noqa: BLE001
            return Transcript(
                source=self.name,
                elapsed_ms=(time.monotonic() - started) * 1000,
                error=f"{type(exc).__name__}: {exc}",
            )
        self.calls += 1
        return Transcript(
            text=text.strip(),
            source=self.name,
            elapsed_ms=(time.monotonic() - started) * 1000,
        )

    def _request(self, pcm: bytes, sample_rate: int) -> str:
        import io
        import json as _json
        import urllib.request
        import wave

        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(SAMPLE_WIDTH)
            handle.setframerate(sample_rate)
            handle.writeframes(pcm)
        audio = buffer.getvalue()

        # 手写 multipart/form-data：为了不引入 requests / httpx 这类依赖
        boundary = "----desktoppetboundary7f3a"
        parts: list[bytes] = []

        def field(name: str, value: str) -> None:
            parts.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
            )

        field("model", self.model)
        parts.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="file"; filename="speech.wav"\r\n'
                f"Content-Type: audio/wav\r\n\r\n"
            ).encode()
        )
        parts.append(audio)
        parts.append(f"\r\n--{boundary}--\r\n".encode())
        body = b"".join(parts)

        request = urllib.request.Request(
            f"{self.base_url}/audio/transcriptions",
            data=body,
            method="POST",
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Authorization": f"Bearer {self.api_key}",
                "User-Agent": "desktop-pet/0.1",
            },
        )
        with netutil.urlopen(request, timeout=self.timeout_s) as response:
            parsed = _json.loads(response.read().decode("utf-8", errors="replace"))
        return str(parsed.get("text") or "")


class OpenAITtsEngine:
    """
    OpenAI 兼容的语音合成（/audio/speech），返回 MP3。

    注意返回值是**压缩格式**：`compute_envelope` 只能对裸 PCM 算包络，
    所以这条路径下口型会退回前端的占位动效。想拿到真实口型包络，
    要么用返回 PCM 的本地 TTS，要么在前端解码后再算 —— 这是明确的取舍，
    不装作没这回事。
    """

    name = "openai"

    def __init__(
        self,
        base_url: str,
        model: str,
        voice: str,
        api_key: str,
        timeout_s: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.voice = voice
        self.api_key = api_key
        self.timeout_s = timeout_s
        self.calls = 0

    def available(self) -> bool:
        return bool(self.base_url and self.model and self.api_key)

    async def synthesize(self, text: str) -> tuple[bytes, str]:
        if not self.available():
            raise RuntimeError("语音合成未配置完整（缺 base_url/model/key）")
        return await asyncio.to_thread(self._request, text)

    def _request(self, text: str) -> tuple[bytes, str]:
        import json as _json
        import urllib.request

        body = _json.dumps(
            {"model": self.model, "input": text, "voice": self.voice, "format": "mp3"}
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/audio/speech",
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
                "User-Agent": "desktop-pet/0.1",
            },
        )
        with netutil.urlopen(request, timeout=self.timeout_s) as response:
            payload = response.read()
        self.calls += 1
        return payload, "mp3"


class EdgeTtsEngine:
    """
    微软 Edge 的**在线**合成接口（`edge-tts` 包）—— **免费、无需 API key**。

    ## 为什么加它（2026-10-10 实测调研后的结论）

    这个项目在"出声"上有三条路，各有硬伤：

      · **云端**（硅基流动 CosyVoice2）—— 中文很好，但**要花钱、要上传文本**
      · **本地 SAPI**（`windows`）—— 免费离线，但**本机实测不可用**
        （能枚举音色、取不到、说话失败，见 `WindowsSapiTtsEngine` 的说明）
      · **mock** —— 不出声

    而 `edge-tts` 是第四条：**免费 + 中文音色好 + 零安装负担**
    （纯 Python 包、不需要模型、不需要编译、不需要 GPU）。

    ⚠️ 代价是它**仍然要联网** —— 所以它不是"本地合成"，
    而是"把付费云端换成免费云端"。这一点在日志里必须说清楚，
    不能让用户以为文本没出本机。

    ## 实测数据（2026-10-10，本机）

    ```
    端点         speech.platform.bing.com  TLS 1.3 握手 296ms
    中文音色     14 个（zh-CN / zh-HK / zh-TW / 东北话 / 陕西话）
    合成耗时     5.02 秒音频 -> 1656ms（晓晓）/ 1828ms（云希）
    输出格式     mp3float · 24kHz · 单声道
    ```

    ## ⚠️ 返回 mp3，所以**拿不到真实口型包络**

    与 `OpenAITtsEngine` 同一条取舍（`compute_envelope` 只能对裸 PCM 算）。
    这里不装作没这回事 —— 想要真实口型就得走返回 PCM 的本地路线。

    ## ⚠️ 它是"非官方接口"

    `edge-tts` 走的是 Edge 浏览器的朗读接口，**没有官方 SLA**，
    将来可能变。所以：

      · 合成失败时**抛错并说清原因**（由 `VoiceSession` 决定降级），
        而不是静默返回空字节 —— 空字节会让前端播出一段静音，很像"宠物哑了"
      · 配置里可以随时切回 `openai`
    """

    name = "edge"

    def __init__(
        self,
        voice: str = "zh-CN-XiaoxiaoNeural",
        rate: str = "",
        volume: str = "",
        pitch: str = "",
        timeout_s: float = 30.0,
    ) -> None:
        self.voice = voice or "zh-CN-XiaoxiaoNeural"
        """
        `rate` / `volume` / `pitch` 是 **edge-tts 的字符串格式**，不是数字。

        edge-tts 用相对量：`rate="+10%"` / `volume="-20%"` / `pitch="+5Hz"`，
        留空表示用默认。与 SAPI 的 `rate: -10..10`、`volume: 0..100` 是**两套**，
        所以不共用配置字段（同 `local_voice` vs `voice` 那条教训）。
        """
        self.rate = rate
        self.volume = volume
        self.pitch = pitch
        self.timeout_s = timeout_s
        self.calls = 0

    def available(self) -> bool:
        """只需要包在。**故意不在这里联网探测** —— 那会让启动变慢，
        而且网络抖动会被误判成"不可用"，把用户锁在 mock 上。
        真正失败时由 `synthesize` 抛错，日志里有明确原因。"""
        try:
            import edge_tts  # noqa: F401
        except Exception:  # noqa: BLE001
            return False
        return True

    async def synthesize(self, text: str) -> tuple[bytes, str]:
        if not text.strip():
            raise RuntimeError("要合成的文本是空的")
        return await asyncio.wait_for(
            self._stream(text), timeout=self.timeout_s
        )

    async def _stream(self, text: str) -> tuple[bytes, str]:
        try:
            import edge_tts
        except Exception as error:  # noqa: BLE001
            raise RuntimeError(
                f"edge-tts 没装上（{type(error).__name__}）—— "
                "请在 .venv 里 pip install edge-tts"
            ) from error

        kwargs: dict[str, Any] = {"voice": self.voice}
        # ⚠️ 留空表示"用默认"，**不能传空串** —— edge-tts 会把它当非法值
        if self.rate:
            kwargs["rate"] = self.rate
        if self.volume:
            kwargs["volume"] = self.volume
        if self.pitch:
            kwargs["pitch"] = self.pitch

        communicate = edge_tts.Communicate(text, **kwargs)
        chunks: list[bytes] = []
        async for chunk in communicate.stream():
            if chunk.get("type") == "audio":
                data = chunk.get("data")
                if data:
                    chunks.append(data)

        payload = b"".join(chunks)
        if not payload:
            raise RuntimeError(
                "edge-tts 没返回音频（接口可能变了，或文本被服务端拒绝）"
            )
        self.calls += 1
        return payload, "mp3"


@dataclass(slots=True)
class VoicePipeline:
    """把 ASR / TTS 两个引擎打包，便于一起构建与替换。"""

    asr: AsrEngine
    tts: TtsEngine
    asr_note: str = ""
    tts_note: str = ""


def build_voice_pipeline(
    config: Config,
    api_key: str,
) -> VoicePipeline:
    """
    按配置构建语音引擎。

    原则：**缺依赖/缺密钥一律降级到 mock，绝不让宠物起不来**。
    看屏幕是锦上添花，语音也是 —— 它们不该成为启动的硬门槛。

    引擎名 -> 实现：
      asr: mock（默认，本地假实现）/ openai（/audio/transcriptions）/ local（未装则降级）
      tts: none（不出声）/ mock（假音频）/ openai（/audio/speech）
    """
    asr_cfg = dict(config.providers.get("asr", {}))
    tts_cfg = dict(config.providers.get("tts", {}))
    brain = dict(config.providers.get("brain", {}))
    base_url = str(asr_cfg.get("base_url") or brain.get("base_url") or "")
    # ⚠️ 旧的平铺路线（没配 cloud_* 时）用的就是 brain 的密钥，
    # 所以厂商按 brain 当前生效的那家算
    asr_key = _env_or(
        asr_cfg.get("api_key_env"),
        api_key,
        vendor=config.brain_active(),
        config=config,
    )

    asr_note = ""
    asr_provider = str(asr_cfg.get("provider") or "mock").lower()
    if asr_provider in ("openai", "cloud"):
        """
        云端识别（OpenAI 兼容的 `/audio/transcriptions`）。

        ## 用户要求：本地与 API 要在设置里能切换

        所以云端这条路的参数**独立于 brain**（`cloud_base_url` / `cloud_model` /
        `cloud_api_key_env`）—— 语音识别的服务商和对话的服务商不必是同一家。
        实测最合适的是硅基流动（`FunAudioLLM/SenseVoiceSmall`，中文强、便宜）。

        ## ⚠️ 回退顺序（每一层都要能解释"为什么没生效"）

          ① `cloud_*` 三项都配了        -> 用它们（独立服务商）
          ② 只配了 `base_url`/`model`   -> 用旧键（与 brain 共用服务商）
          ③ 都没有                      -> 复用 brain 的 base_url + 密钥

        密钥按 `_env_or` 取：环境变量优先，取不到就用 brain 的密钥
        （硅基流动和 DeepSeek 是两家的 key，所以配了环境变量就必须能读到，
         读不到时要**说出来**而不是静默用错的 key）。
        """
        # 首选 cloud_* 那套（独立服务商）
        cloud_model = str(asr_cfg.get("cloud_model") or "").strip()
        cloud_base = str(asr_cfg.get("cloud_base_url") or "").strip()
        # ⚠️ vendor 从 cloud_base_url 推 —— 与主进程保存密钥时的槽位名一致
        cloud_key = _env_or(
            asr_cfg.get("cloud_api_key_env"),
            "",
            vendor=vendor_from_url(cloud_base),
            config=config,
        )
        if cloud_model and cloud_base:
            model = cloud_model
            use_base = cloud_base.rstrip("/")
            use_key = cloud_key or asr_key
            key_note = "" if cloud_key else "（⚠️ 没读到 cloud_api_key_env 指定的环境变量，回退用 brain 的密钥）"
        else:
            model = str(asr_cfg.get("model") or asr_cfg.get("model_fallback") or "whisper-1")
            use_base = base_url
            use_key = asr_key
            key_note = ""
        engine: AsrEngine = OpenAIAsrEngine(base_url=use_base, model=model, api_key=use_key)
        if not engine.available():  # type: ignore[attr-defined]
            asr_note = (
                "云端语音识别缺少密钥，已降级为 mock。"
                "请在设置页填「语音识别的 API 密钥」，或设好 cloud_api_key_env 指定的环境变量"
            )
            engine = MockAsrEngine()
        else:
            asr_note = f"使用云端语音识别：{model} @ {use_base}（⚠️ 语音会上传到该服务商）{key_note}"
    elif asr_provider == "local":
        """
        本地识别（faster-whisper）。用户要求「本地大模型和 api 的切换开关」。

        ## ⚠️ 三级降级，每一级的**原因都要说清楚**

        `build_voice_pipeline` 的原则是"缺依赖一律降级、绝不让宠物起不来"。
        但"降级成 mock"如果不说原因，用户看到的是**假识别结果**却以为是真的 ——
        那比直接报错还糟。所以这里把原因写进 `asr_note`，它会一路报到界面。

        ## ⚠️ 模型不在缓存时**不降级**，而是如实说"首次要下载"

        首次识别会去下 1.6GB。那不是错误，是正常的第一次 ——
        但要预先告诉用户，否则界面看起来像卡死。
        """
        engine = LocalAsrEngine(
            model=str(asr_cfg.get("model") or "large-v3-turbo"),
            device=str(asr_cfg.get("device") or "auto"),
            compute_type=str(asr_cfg.get("compute_type") or ""),
            model_dir=_asr_model_dir(asr_cfg),
            language=str(asr_cfg.get("language") or "zh"),
            beam_size=int(asr_cfg.get("beam_size") or 1),
            cpu_threads=int(asr_cfg.get("cpu_threads") or 0),
            # 默认引导简体（None -> 用内置默认；空串 -> 显式不要）
            initial_prompt=(
                str(asr_cfg["initial_prompt"])
                if "initial_prompt" in asr_cfg and asr_cfg.get("initial_prompt") is not None
                else SIMPLIFIED_PROMPT
            ),
            convert_to_simplified=bool(asr_cfg.get("convert_to_simplified", True)),
        )
        if not engine.available():  # type: ignore[attr-defined]
            asr_note = (
                "本地识别不可用 —— 没装 faster-whisper"
                "（在项目目录里跑：.venv\\Scripts\\python.exe -m pip install faster-whisper），已降级为 mock"
            )
            engine = MockAsrEngine()
        else:
            cached = engine.ensure_model_cached()  # type: ignore[attr-defined]
            dev = str(asr_cfg.get("device") or "auto")
            asr_note = f"使用本地识别：{engine.model_name}（device={dev}，离线、语音不出本机）"  # type: ignore[attr-defined]
            if not cached:
                asr_note += "。⚠️ 该模型还没下载过，第一次识别要先下模型（可能几分钟）"
    elif asr_provider == "mock":
        asr_note = "使用内置假识别（仅用于验证链路时序，不会真的听懂你说话）"
        engine = MockAsrEngine()
    else:
        asr_note = f"未知的 asr.provider={asr_provider}，已降级为 mock"
        engine = MockAsrEngine()

    tts_note = ""
    tts_vendor = vendor_from_url(str(tts_cfg.get("base_url") or base_url))
    tts_key = _env_or(
        tts_cfg.get("api_key_env"),
        api_key,
        vendor=tts_vendor,
        config=config,
    )
    tts_provider = str(tts_cfg.get("provider") or "none").lower()
    if tts_provider in ("openai", "cloud"):
        model = str(tts_cfg.get("model") or "tts-1")
        voice = str(tts_cfg.get("voice") or "alloy")
        tts_base = str(tts_cfg.get("base_url") or base_url)
        tts_engine: TtsEngine = OpenAITtsEngine(
            base_url=tts_base, model=model, voice=voice, api_key=tts_key
        )
        if not tts_engine.available():  # type: ignore[attr-defined]
            tts_note = "语音合成缺少密钥，暂不出声"
            tts_engine = MockTtsEngine()
    elif tts_provider == "windows":
        # 本地离线合成（Windows SAPI）。**实际由主进程执行** —— 见
        # WindowsSapiTtsEngine 的说明（Python 起的 PowerShell 拿不到 SAPI 音色）。
        # 所以这里不传脚本路径，只需要音色/语速/音量。
        # ⚠️ 本地用 **`local_voice`**，不是 `voice`（2026-10-09）。
        #
        # 两条路线的音色名格式完全不同（本地是 SAPI 名字、云端是 `模型名:音色名`），
        # 共用一个字段会互相污染：切到本地保存后，`voice` 变成 SAPI 的名字，
        # 再切回云端就 400 Invalid voice（实测）。
        #
        # 回退读 `voice` 是为了兼容旧配置（那时只有一个字段）。
        voice = str(tts_cfg.get("local_voice") or tts_cfg.get("voice") or "")
        tts_engine = WindowsSapiTtsEngine(
            voice=voice,
            rate=int(tts_cfg.get("rate") or 0),
            volume=int(tts_cfg.get("volume") or 100),
        )
        tts_note = (
            "使用 Windows 本地合成（离线、免费、零延迟）"
            f"{'，音色 ' + voice if voice else '，用系统默认音色'}"
            "。⚠️ 输出裸 PCM，可做真实口型（合成由主进程执行）"
            "。⚠️ 但当前环境的 SAPI 实测不可用（能枚举音色、取不到），"
            "要出声请把 providers.tts.provider 改成 openai（云端）"
        )
    elif tts_provider == "edge":
        # **免费在线合成**（微软 Edge 接口）—— 见 `EdgeTtsEngine` 的说明。
        #
        # ⚠️ 音色用**独立字段 `edge_voice`**，不共用 `voice` / `local_voice`。
        # 三条路线的音色名格式完全不同（云端 `模型名:音色名`、
        # 本地 SAPI 名字、edge 的 `zh-CN-XiaoxiaoNeural`），共用一个字段
        # 会互相污染 —— 这个坑在 `local_voice` vs `voice` 上已经踩过一次。
        edge_voice = str(tts_cfg.get("edge_voice") or "zh-CN-XiaoxiaoNeural")
        tts_engine = EdgeTtsEngine(
            voice=edge_voice,
            rate=str(tts_cfg.get("edge_rate") or ""),
            volume=str(tts_cfg.get("edge_volume") or ""),
            pitch=str(tts_cfg.get("edge_pitch") or ""),
        )
        if not tts_engine.available():  # type: ignore[attr-defined]
            tts_note = "edge-tts 没装上，暂不出声 —— 请在 .venv 里 pip install edge-tts"
            tts_engine = MockTtsEngine()
        else:
            tts_note = (
                f"使用 edge-tts 免费在线合成：{edge_voice}"
                "（⚠️ 免费、无需密钥，但**文本仍会发到微软的服务器**）"
                "。⚠️ 输出 mp3，口型会退回占位动效（拿不到真实包络）"
            )
    elif tts_provider == "mock":
        tts_note = "使用内置假合成（等长静音，仅用于验证播放时序）"
        tts_engine = MockTtsEngine()
    else:
        tts_note = "tts.provider=none，暂不出声（只显示文字）"
        tts_engine = MockTtsEngine()

    if asr_note:
        log.info("语音识别：%s", asr_note)
    if tts_note:
        log.info("语音合成：%s", tts_note)
    return VoicePipeline(asr=engine, tts=tts_engine, asr_note=asr_note, tts_note=tts_note)


def _asr_model_dir(asr_cfg: dict[str, Any]) -> Optional[Path]:
    """
    本地 ASR 的**模型缓存目录**。

    ## ⚠️ 默认放项目内（`apps/desktop/pet-data/asr-models`）

    不写进 `~/.cache/huggingface` —— 那是用户目录。本项目有一条明确约定：
    **能在项目内解决的，不要去动用户目录**（与"记忆数据全部留在本地项目"
    是同一条理由）。实测这台机器上用户目录也确实有不可写的历史。

    配置里可以覆盖：`providers.asr.model_dir`（绝对路径或相对仓库根）。
    """
    raw = str(asr_cfg.get("model_dir") or "").strip()
    if raw:
        p = Path(raw)
        return p if p.is_absolute() else (PROJECT_ROOT / p)
    # ② 数据目录里的 asr-models（2026-10-19）：打包态 PROJECT_ROOT 是只读的
    #    resources/，用户只能往 `%APPDATA%\DesktopPet\pet-data` 放模型。
    #    这里非空就优先用它 —— 用户把模型文件夹拷进去、重启，就是离线识别。
    legacy = PROJECT_ROOT / "apps" / "desktop" / "pet-data" / "asr-models"
    try:
        from . import paths

        data = paths.data_dir() / "asr-models"
        if data.is_dir() and any(data.iterdir()):
            return data
        # 离线版出厂把模型放在 resources 里（只读但能加载）
        if legacy.is_dir() and any(legacy.iterdir()):
            return legacy
        # 都没有：用数据目录 —— 可写，自动下载能落地
        return data
    except Exception:  # noqa: BLE001
        pass
    return legacy


def vendor_from_url(base_url: str, fallback: str = "") -> str:
    """
    从服务地址推出**厂商名**（= `credentials.json` 里的槽位名）。

    ⚠️ 这套规则必须和 **Electron 主进程的 `providerNameFor()`** 一致 ——
    保存密钥和读取密钥用的是同一个名字，不一致就会出现
    "界面显示已保存、程序却报没密钥"（这个坑本项目踩过多次）。

        规则：取 hostname 里去掉 api/www/com/cn 之后的第一段
        https://api.siliconflow.cn/v1   -> siliconflow
        https://api.deepseek.com/v1     -> deepseek
    """
    raw = str(base_url or "").strip()
    if not raw:
        return fallback
    try:
        host = urllib.parse.urlsplit(raw).hostname or ""
    except ValueError:
        return fallback
    parts = [p for p in host.split(".") if p and p not in ("api", "www", "com", "cn")]
    return parts[0] if parts else fallback


def _env_or(env_name: Any, fallback: str, *, vendor: str = "", config: Any = None) -> str:
    """
    解析语音引擎的 API 密钥。

    ## 🔴 为什么不再只是"读环境变量"（2026-10-08 用户实测报 401）

    用户把语音识别切到云端（硅基流动）后，宠物弹出：

        语音识别失败：HTTPError: HTTP Error 401: Unauthorized

    而 `credentials.json` 里**明明有** `siliconflow` 那个密钥（51 字符）。
    原因是这个函数**只读环境变量**，完全不看凭据文件：

        环境变量 SILICONFLOW_API_KEY  -> 没设 -> 读不到
        -> 回退用 brain 的密钥（DeepSeek 的）
        -> 拿 DeepSeek 的 key 请求 api.siliconflow.cn -> **401**

    ## 现在的查找顺序（与 `Config.api_key()` 对齐）

      ① 环境变量（脚本化 / CI / 临时试用）
      ② 凭据文件里**该厂商自己的**槽位（设置页保存的就是它）

    ⚠️ **不跨厂商兜底**：`siliconflow` 读不到时**不会**退回 DeepSeek 的密钥。
    借别家的 key 只会换来一个含糊的 401，让人去怀疑"密钥写错了 / 服务商挂了"，
    而不是"我还没配这一家的密钥"。缺了就返回空，由调用方给出明确提示。

    `fallback` 只在**没有指定 vendor** 时使用（老的两条路共用一个密钥的情形）。
    """
    name = str(env_name or "").strip()
    if name:
        value = os.environ.get(name, "").strip()
        if value:
            return value

    # ② 凭据文件里这个厂商的槽位
    vend = str(vendor or "").strip().lower()
    if vend and config is not None:
        try:
            # 复用 `Config.api_key()` 的查找路径逻辑（PET_CREDENTIALS_FILE 等），
            # 但**针对指定厂商**取值 —— 这样两边永远看同一组文件。
            for cred_path in config._credential_paths():
                if not cred_path.exists():
                    continue
                data = json.loads(cred_path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    continue
                got = data.get(vend)
                if isinstance(got, str) and got.strip():
                    return got.strip()
        except (OSError, ValueError):
            log.debug("读取语音引擎密钥失败（当作没有）", exc_info=True)
        # ⚠️ 指定了厂商但没找到 —— **不借用别家的**，直接空
        return ""

    return fallback
