/**
 * 语音 I/O：麦克风采集与宠物语音播放。
 *
 * **这个文件里的东西在本机无法验证** —— 沙箱起不了 GUI，也没有麦克风。
 * 所以它写得特别直白、尽量少依赖浏览器特性，并把每一处易错点写在注释里，
 * 方便在真机上第一次运行时快速定位问题。
 *
 * 分工（见 apps/brain/pet/voice.py 顶部的说明）：
 *   渲染层  采集麦克风 PCM、重采样到 16kHz、推给主进程；解码并播放宠物语音
 *   Python  端点检测、识别、合成、打断判定
 *
 * 采样的关键约束：**ASR 需要 16kHz 单声道 16bit PCM**。麦克风通常给 44.1k/48k，
 * 所以必须重采样。这里用线性插值而不是引入重采样库 —— 对语音识别足够，
 * 而且省掉一个依赖。
 */

const TARGET_SAMPLE_RATE = 16000;
/** 每帧 20ms，与 Python 侧 FRAME_MS 对齐；不对齐会让端点检测的时序判断偏掉 */
const FRAME_MS = 20;
const SAMPLES_PER_FRAME = (TARGET_SAMPLE_RATE * FRAME_MS) / 1000; // 320

export class VoiceIO {
  constructor() {
    this.active = false;
    this.context = null;
    this.stream = null;
    this.node = null;
    this.source = null;
    /** 待发送的样本缓冲：凑满一帧再发，避免每几毫秒发一个小包 */
    this.pending = [];
    this.pendingCount = 0;
    this.framesSent = 0;
    this.samplesDropped = 0;
    /**
     * 采集闸门：`false` 时**根本不往外发帧**。
     *
     * ## 🔴 为什么必须有它（2026-10-10 修，这个是真 bug）
     *
     * 原来 `pushSamples()` **无条件**把每一帧推给主进程。而"按住说话"模式下
     * 闸门是由 sidecar 的 `_capture_enabled` 管的 —— 那只是让 sidecar
     * **不处理**这些帧，**帧本身照样在推**。
     *
     * 三个后果，第三个是致命的：
     *
     *   ① **白推**：50 帧/秒永远在推，`main.log` 里 `pet.ws 帧头…` 的
     *      DEBUG 日志占了 **97%**（实测 17046 行里 16637 行）。
     *   ② **白算**：主进程转发、sidecar 收包、入队、出队、丢弃 —— 全在做无用功。
     *   ③ 🔴 **队列里积着 2 秒旧音频**：`QueueAudioInput(max_frames=100)`，
     *      一帧 20ms -> 满队列正好 **2 秒**。闸门一开，端点/VAD 先吃到这 2 秒
     *      **闸门关闭期间**的音频，于是：
     *
     *          环境噪声被 VAD 当成语音 -> 噪声底被抬高
     *          -> 阈值 = max(0.012, 噪声底 × 3) 变高
     *          -> **用户真正说话的能量低于新阈值，被判成静音**
     *          -> 端点不攒句子 -> `force_end()` 返回 None -> 松手后没识别
     *
     *      表现就是用户报的「**第一次能用，之后按了没反应**」。
     *
     *      `voice.py` 里那条注释（"闸门关闭期间 VAD 仍在处理音频，
     *      噪声底 0.005 -> 0.011"）写的就是这个机理 —— 只是当时闸门在
     *      `vad.process()` 之后才检查。现在闸门提前了，但**队列积压**这条路
     *      仍然把旧音频送了进来。所以必须**从源头停掉**。
     *
     * ## 初值 `false` 为什么是安全的
     *
     * ⚠️ 免提模式下闸门该是 `true`，所以初值 `false` 看着像会"免提收不到音频"。
     * 实际上不会，因为 **`app.js` 会在麦克风打开的那一刻立刻修正它**：
     *
     *     // warmUpVoice()
     *     await audio.start(...)      <- 麦克风这一刻才开
     *     applyCaptureGate()          <- 紧接着就按当前开关算一次
     *
     * 而且 `applyCaptureGate()` 一共在**四个时机**都会调（预热后 / 开麦克风后 /
     * 收到「按住说话」配置时 / 按下松开时），**任何一条生效都够**。
     *
     * 在这之前 `active` 还是 `false`（麦克风没开），`onaudioprocess` 也不会触发
     * —— 所以"初值错一小会儿"**没有可观测后果**。
     *
     * ⚠️ 我一开始想在这里读配置算初值，**那是错的**：会把"按住说话开没开"
     * 这个状态复制到两个模块里，两边一旦不同步就是又一个"开关不生效"。
     * 状态只有一个源（`app.js` 的 `pushToTalkEnabled`）。
     */
    this.gate = false;
    /** 播放队列：同一时刻只播一段，避免多段语音叠在一起 */
    this.playback = null;
    /**
     * 实际打开的那支麦克风的名字。
     *
     * 给界面显示用 —— 用户要能确认"真的在用我选的那支"。
     * 这次的坑正是"名字看着对、实际不是同一个设备"，所以必须能看见。
     */
    this.deviceLabel = '';
  }

  /**
   * 列出可选的麦克风。
   *
   * ⚠️ **必须过滤掉 `default` / `communications` 两个别名**（2026-10-08 实测）。
   *
   * Chromium 在枚举结果里会塞两个别名设备：
   *   · `deviceId: 'default'`        -> "Default - 麦克风 (USB PnP Sound Device)"
   *   · `deviceId: 'communications'` -> "Communications - 麦克风 (…) "
   *
   * 它们**显示的名字和真实设备一模一样**，但用它们开流**拿到的可能不是同一个设备**。
   * 实测（同一支 USB 麦，同样对人说话 3 秒）：
   *
   *     不给 deviceId（走 default 别名）  peak 0.0205   判定语音  3/83  ❌
   *     指定真实 deviceId                peak 0.1413   判定语音 19/86  ✅
   *
   * **差 7 倍** —— 用户的表现就是"对着 USB 麦克风说话没用"。
   *
   * 所以：**只用真实 deviceId**，让用户从真实设备里挑。
   */
  async listMicrophones() {
    if (!navigator.mediaDevices?.enumerateDevices) return [];
    const all = await navigator.mediaDevices.enumerateDevices();
    const ALIASES = new Set(['default', 'communications']);
    const seen = new Set();
    const out = [];
    for (const d of all) {
      if (d.kind !== 'audioinput') continue;
      if (ALIASES.has(d.deviceId) || seen.has(d.deviceId)) continue;
      seen.add(d.deviceId);
      out.push({ deviceId: d.deviceId, label: d.label || '（未命名麦克风）' });
    }
    return out;
  }

  /**
   * 开启麦克风。返回实际使用的采集参数，供界面显示。
   * 失败时抛错，由调用方翻译成用户能看懂的话（权限被拒 ≠ 没有设备）。
   *
   * ⚠️ `deviceId` 传真实设备 id（见 `listMicrophones` 的说明）；
   * 传空则交给系统默认 —— 但那**可能不是用户在系统设置里选的那个**。
   */
  async start(deviceId = '') {
    if (this.active) return { sampleRate: this.context?.sampleRate ?? 0 };

    if (!navigator.mediaDevices?.getUserMedia) {
      throw new Error('此环境不支持麦克风采集（mediaDevices 不可用）');
    }
    // 关掉浏览器的自动处理：降噪/自动增益是为通话设计的，会把语音的动态压平，
    // 反而让能量法端点检测更难判断"用户到底有没有在说话"。
    const audio = {
      channelCount: 1,
      echoCancellation: false,
      noiseSuppression: false,
      autoGainControl: false,
    };
    if (deviceId) {
      // ⚠️ 用 `exact` 而不是裸 id：裸 id 在设备不存在时会**静默退回默认设备**，
      // 而用户以为自己在用指定的那支麦 —— 正是这次踩的坑。
      audio.deviceId = { exact: deviceId };
    }
    this.stream = await navigator.mediaDevices.getUserMedia({ audio });
    // 记下实际拿到的设备名，供界面显示（用户要能确认"真的在用那支"）
    this.deviceLabel = this.stream.getAudioTracks()[0]?.label || '';

    this.context = new AudioContext();
    // 有些浏览器创建后处于 suspended（需要用户手势），这里显式恢复
    if (this.context.state === 'suspended') await this.context.resume();

    this.source = this.context.createMediaStreamSource(this.stream);
    // ScriptProcessorNode 虽已标记废弃，但它在所有 Chromium 版本上都可用，
    // 而 AudioWorklet 需要额外的模块文件与加载时序。桌宠这种轻量采集用前者足够。
    const bufferSize = 2048;
    this.node = this.context.createScriptProcessor(bufferSize, 1, 1);
    const inputRate = this.context.sampleRate;

    this.node.onaudioprocess = (event) => {
      if (!this.active) return;
      const channel = event.inputBuffer.getChannelData(0);
      const resampled = resample(channel, inputRate, TARGET_SAMPLE_RATE);
      this.pushSamples(resampled);
    };

    this.source.connect(this.node);
    // 必须接到 destination，否则部分实现不会触发 onaudioprocess。
    // 用一个静音增益节点，避免把自己的麦克风声音外放出去（会形成啸叫）。
    const mute = this.context.createGain();
    mute.gain.value = 0;
    this.node.connect(mute);
    mute.connect(this.context.destination);

    this.active = true;
    return { sampleRate: inputRate };
  }

  /** 把重采样后的浮点样本切成固定长度的 int16 帧发出去 */
  pushSamples(samples) {
    /*
     * 闸门关着 -> **一个字节都不发**（见构造里 `gate` 的说明）。
     *
     * ⚠️ 放在**最前面**：连 `pending` 都不要攒。
     * 攒了的话闸门一开会先吐出一批"闸门关闭期间"的旧音频 —— 那正是要避免的。
     */
    if (!this.gate) return;

    const pending = this.pending;
    for (let i = 0; i < samples.length; i += 1) pending.push(samples[i]);
    this.pendingCount = pending.length;

    while (this.pendingCount >= SAMPLES_PER_FRAME) {
      const frame = new Int16Array(SAMPLES_PER_FRAME);
      for (let i = 0; i < SAMPLES_PER_FRAME; i += 1) {
        // 浮点 -1~1 -> int16。夹取一下，防止重采样过冲造成回绕（会变成刺耳噪声）
        const value = Math.max(-1, Math.min(1, pending[i]));
        frame[i] = value < 0 ? value * 0x8000 : value * 0x7fff;
      }
      pending.splice(0, SAMPLES_PER_FRAME);
      this.pendingCount = pending.length;
      this.framesSent += 1;
      window.pet?.sendMicAudio?.(frame.buffer);
    }

    // 缓冲上限：如果主进程迟迟不收（比如 socket 断了），不要无限增长
    const maxPending = SAMPLES_PER_FRAME * 25; // 约 500ms
    if (this.pendingCount > maxPending) {
      const excess = this.pendingCount - maxPending;
      pending.splice(0, excess);
      this.pendingCount = pending.length;
      this.samplesDropped += excess;
    }
  }

  /**
   * 开关采集闸门（见构造里 `gate` 的说明）。
   *
   * ## 为什么开闸时要**清空残留样本**
   *
   * `pending` 是"凑满一帧再发"的缓冲（最多 25 帧 ≈ 500ms）。如果关闸期间
   * 不清，开闸的瞬间会先吐出一批**关闸期间**的旧音频 —— 而那正是要避免的
   * （VAD 噪声底被旧音频抬高 -> 用户真正说话被判成静音）。
   *
   * 清掉之后，开闸后发的**第一帧就是当前这一刻的声音**。
   */
  setGate(on) {
    const next = Boolean(on);
    if (next === this.gate) return;
    this.gate = next;
    // 无论开关都清一次：关的时候清是为了不攒，开的时候清是为了不吐旧的
    this.pending = [];
    this.pendingCount = 0;
  }

  stop() {
    this.active = false;
    try {
      this.node?.disconnect();
      this.source?.disconnect();
    } catch {
      /* 断开失败无所谓 */
    }
    this.node = null;
    this.source = null;
    this.stream?.getTracks().forEach((track) => track.stop());
    this.stream = null;
    // AudioContext 不关会一直占着音频设备
    this.context?.close?.().catch?.(() => {});
    this.context = null;
    this.pending = [];
    this.pendingCount = 0;
  }

  /**
   * 播放宠物语音。
   *
   * 目前 Python 侧默认不出声（tts.provider=none），所以这条路径通常不会走到；
   * 配上 TTS 之后它才是主路径。
   */
  async play(payload, format) {
    if (!this.context) this.context = new AudioContext();
    const context = this.context;
    if (context.state === 'suspended') await context.resume();

    // 同一时刻只播一段：后到的语音先打断前一段，避免叠音
    await this.stopPlayback();

    const bytes = payload instanceof ArrayBuffer ? payload : new Uint8Array(payload).buffer;
    let buffer;
    if (format === 'pcm') {
      buffer = context.createBuffer(1, bytes.byteLength / 2, TARGET_SAMPLE_RATE);
      const channel = buffer.getChannelData(0);
      const view = new Int16Array(bytes);
      for (let i = 0; i < view.length; i += 1) channel[i] = view[i] / 0x8000;
    } else {
      // mp3 等压缩格式交给浏览器解码
      buffer = await context.decodeAudioData(bytes.slice(0));
    }

    const source = context.createBufferSource();
    source.buffer = buffer;
    source.connect(context.destination);
    source.start();
    this.playback = source;
    /*
     * 一段语音真的播完了（2026-10-16 加）：给主进程当「说话中 -> 空闲」的
     * 终点信号。注意 source.stop()（打断）也会触发 onended —— 那正是想要的：
     * 被打断 = 不在说话了，状态条同样该回空闲。
     *
     * 🔴 2026-10-17 加**时长兜底定时器**：实测 onended 有概率不触发
     * （用户报「语音完了还挂说话中」，main.log 里终点信号始终没来）。
     * buffer.duration 是音频自带的精确时长，+250ms 容差后兜底 ——
     * 两条路谁先到都算数（report 幂等）。
     */
    let reported = false;
    const report = () => {
      if (reported) return;
      reported = true;
      if (this.playback === source) this.playback = null;
      if (typeof this.onPlaybackEnded === 'function') {
        try {
          this.onPlaybackEnded();
        } catch (error) {
          console.warn('[voice] onPlaybackEnded 回调出错', error);
        }
      }
    };
    source.onended = () => report();
    setTimeout(report, buffer.duration * 1000 + 250);
  }

  async stopPlayback() {
    const source = this.playback;
    this.playback = null;
    if (!source) return;
    try {
      source.stop();
    } catch {
      /* 已经停了 */
    }
  }

  get stats() {
    return {
      active: this.active,
      framesSent: this.framesSent,
      samplesDropped: this.samplesDropped,
      inputRate: this.context?.sampleRate ?? 0,
    };
  }
}

/**
 * 线性插值重采样。对语音识别足够（它要的是频谱包络而不是高保真），
 * 而且省掉一个重采样库依赖。
 */
export function resample(input, inputRate, outputRate) {
  if (inputRate === outputRate) return input;
  const ratio = inputRate / outputRate;
  const length = Math.max(1, Math.floor(input.length / ratio));
  const output = new Float32Array(length);
  for (let i = 0; i < length; i += 1) {
    const position = i * ratio;
    const index = Math.floor(position);
    const frac = position - index;
    const a = input[index] ?? 0;
    const b = input[index + 1] ?? a;
    output[i] = a + (b - a) * frac;
  }
  return output;
}
