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
    /**
     * 播放音量（0..1）与它对应的增益节点（2026-10-19 "可调节声音大小"）。
     *
     * ⚠️ 为什么增益放在**播放端**，不在合成端：
     * 三条合成路线的音量参数是**三套单位** ——
     *   edge   `'+10%'`（相对量字符串）
     *   SAPI   `0..100`（整数）
     *   云端   根本没有这个参数
     * 在播放端乘一个系数，三条路线就都听话了，也不会出现"两处各调一次"。
     */
    this.playbackVolume = 1;
    /** 采集链路的增益节点（`start()` 里创建，接 `this.node` 录音） */
    this.gain = null;
    /**
     * **播放**用的增益节点（2026-10-30 新增，与采集的 `this.gain` **分开**）。
     *
     * ## 🔴 为什么必须分开
     *
     * 原来两者共用一个字段，导致"开过麦克风之后，播放的音频被接到录音链路"，
     * 表现为**没声音**。完整分析见 `play()` 里那段注释。
     *
     * 简言之：`play()` 里的 `if (!this.gain)` 在采集已创建时**跳过创建**，
     * 于是 `source.connect(this.gain)` 接到了录音用的节点上 ——
     * 声音进了麦克风链路，而不是扬声器。
     */
    this.playbackGain = null;
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
    /**
     * 上次启动时**配置里那支麦克风已经不存在**的 id（2026-10-28）。
     *
     * 非空说明发生了回退：用户选的那支麦没了，这次用的是系统默认。
     * 设置页据此提示"请重新选一次麦克风" —— 否则用户会一直以为
     * 自己在用那支指定的麦，而实际录的是别的（本次故障正是这样）。
     */
    this.deviceMissing = '';
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
    /*
     * 🔴 指定设备**失效时回退到系统默认**（2026-10-28 修）。
     *
     * ## 实测故障
     *
     * 用户报「全屏打游戏的时候语音无法触发」。查日志：
     *
     *     采集电平：energy=0.02631 threshold=0.04900 静音
     *     ...370 帧全是静音，energy 只在 0.0208~0.0289 之间抖动
     *
     * energy 几乎没有波动 —— 那是**在录一个空设备**。
     *
     * 再枚举实际设备，配置里存的 `micDeviceId` 是
     * `e77e6333...`，而当前系统只有：
     *
     *     Default / Communications / 麦克风(Steam Streaming) / 麦克风(Realtek)
     *
     * **那个 id 已经不存在了**（换过设备/驱动重装/Steam 虚拟麦克风排序变了）。
     *
     * ## 为什么原来不报错
     *
     * `exact` 在设备不存在时抛 `OverconstrainedError`。上层 `app.js` 用
     * `withTimeout` 包着，异常被 catch 吞掉后只记一句"麦克风没开成"，
     * 而**采集闸门已经开了** —— 于是表现为"看着在听、其实什么都没录到"。
     *
     * ## 修法
     *
     * `exact` 失败 -> 记一条明确的警告（要能让用户看见"你选的那支麦没了"）
     * -> 退回系统默认重试一次。默认设备至少是"用户系统里当前有效的"，
     * 比彻底不录音好得多。
     *
     * ⚠️ 回退时**不清空配置**：用户的偏好还在，下次设备回来就又能用。
     */
    const open = async (constraints) => {
      this.stream = await navigator.mediaDevices.getUserMedia({ audio: constraints });
    };
    try {
      await open(audio);
    } catch (error) {
      const name = (error && error.name) || '';
      // 只有"设备约束不满足"这类才回退；权限被拒（NotAllowedError）不该硬来
      const retriable =
        name === 'OverconstrainedError' ||
        name === 'NotFoundError' ||
        name === 'DevicesNotFoundError';
      if (!deviceId || !retriable) throw error;
      this.deviceMissing = deviceId;
      console.warn(
        `[voice] 指定的麦克风已不存在（deviceId=${deviceId.slice(0, 12)}…，${name}），` +
          '回退到系统默认设备。请到设置里重新选一次麦克风。',
      );
      const fallback = { ...audio };
      delete fallback.deviceId;
      await open(fallback);
    }
    // 记下实际拿到的设备名，供界面显示（用户要能确认"真的在用那支"）
    this.deviceLabel = this.stream.getAudioTracks()[0]?.label || '';

    this.context = new AudioContext();
    // 有些浏览器创建后处于 suspended（需要用户手势），这里显式恢复
    if (this.context.state === 'suspended') await this.context.resume();

    this.source = this.context.createMediaStreamSource(this.stream);
    /*
     * 🔴 自动增益（2026-10-28 修「说了话它没反应」）。
     *
     * ## 为什么必须自己做
     *
     * 上面 `getUserMedia` 里显式关掉了 `autoGainControl` —— 那是**有意的**：
     * 浏览器自带的 AGC 是为通话设计的，会把语音动态压平，让能量法端点检测
     * 更难分辨"到底有没有在说话"。
     *
     * 但代价是：**麦克风增益低的机器上，人声进不来**。
     *
     * 实测（Realtek 板载麦，用户正常说话）：
     *
     *     环境底噪 energy ≈ 0.0247
     *     说话时 energy   ≈ 0.028    <- 只比底噪高 13%
     *     判定阈值         = 0.049     <- 底噪的 2 倍
     *
     * 也就是**说话声根本没有把能量抬起来**。原因不是"用户声音小"，
     * 而是系统输入音量 / 麦克风增益太低，整条信号被压扁了。
     *
     * ## 做法：慢速归一化增益
     *
     * 跟踪**近期峰值**，把信号放大到目标电平。关键约束：
     *
     *   · 增益**只在静音段更新**（否则说话声会把自己压下去，和 VAD 的
     *     噪声底自适应是同一个道理）
     *   · 增益有上下限（0.5x ~ 12x）：太低没用，太高会把底噪放大成"说话"
     *   · 变化**要慢**（每帧 2%）：突变会让 VAD 误判"突然出现语音"
     *
     * ⚠️ 这不是浏览器那个 AGC：它不压动态、不做降噪，只做**整体抬升**，
     *    所以 VAD 的"说话/静音"对比度反而变大了。
     */
    this.gain = this.context.createGain();
    this.gain.gain.value = 1.0;
    // 自动增益的运行态
    this._agcPeak = 0.02;   // 近期峰值估计（初值取一个偏低的经验值）
    this._agcGain = 1.0;    // 当前增益（与 gain.gain.value 同步）
    this._agcWant = 1.0;    // 目标增益（只在安静段更新）
    this._agcTarget = 0.12; // 目标峰值：让说话声落在 VAD 阈值之上
    this.agcEnabled = true; // 出问题时可关掉（排查"增益把噪声放大"时用）
    this.lastRms = 0;       // 最近一块的 RMS（放大后），供诊断
    this.lastGain = 1;      // 最近一块用的增益

    // ScriptProcessorNode 虽已标记废弃，但它在所有 Chromium 版本上都可用，
    // 而 AudioWorklet 需要额外的模块文件与加载时序。桌宠这种轻量采集用前者足够。
    const bufferSize = 2048;
    this.node = this.context.createScriptProcessor(bufferSize, 1, 1);
    const inputRate = this.context.sampleRate;

    this.node.onaudioprocess = (event) => {
      if (!this.active) return;
      const channel = event.inputBuffer.getChannelData(0);
      // 🔴 自动增益：**在这里乘**，不用 GainNode（见 `this._agc*` 的说明）
      //
      // ⚠️ 为什么不用 GainNode：`gain.gain.value` 的改动是**异步生效**的，
      //    而且它有平滑（setTargetAtTime），在 2048 样本的块上会出现
      //    "这一块按旧增益、下一块按新增益"的台阶，反而制造突变。
      //    直接乘是最可控的：每一块用当前 `_agcGain`，同时更新它。
      const gain = this._applyAgc(channel);
      let rms = 0;
      const scaled = gain === 1 ? channel : new Float32Array(channel.length);
      if (gain !== 1) {
        for (let i = 0; i < channel.length; i += 1) scaled[i] = channel[i] * gain;
      }
      const use = gain === 1 ? channel : scaled;
      for (let i = 0; i < use.length; i += 1) rms += use[i] * use[i];
      rms = Math.sqrt(rms / (use.length || 1));
      // 把放大后的 RMS 暴露出去（界面/日志能看见"增益救回来多少"）
      this.lastRms = rms;
      this.lastGain = gain;
      const resampled = resample(use, inputRate, TARGET_SAMPLE_RATE);
      this.pushSamples(resampled);
    };

    this.source.connect(this.gain);
    this.gain.connect(this.node);
    // 必须接到 destination，否则部分实现不会触发 onaudioprocess。
    // 用一个静音增益节点，避免把自己的麦克风声音外放出去（会形成啸叫）。
    const mute = this.context.createGain();
    mute.gain.value = 0;
    this.node.connect(mute);
    mute.connect(this.context.destination);

    this.active = true;
    return { sampleRate: inputRate };
  }

  /**
   * 自动增益：算这一块该乘多少（2026-10-28）。
   *
   * ## 为什么需要它
   *
   * 用户报「全屏打游戏时说了话它没反应」。实测（Realtek 板载麦）：
   *
   *     环境底噪 energy ≈ 0.0247
   *     说话时 energy   ≈ 0.028     <- 只比底噪高 13%
   *     VAD 阈值         = 0.049     <- 底噪的 2 倍
   *
   * **说话声根本没把能量抬起来**。不是用户声音小，是系统输入音量 /
   * 麦克风增益把整条信号压扁了。
   *
   * 而 `getUserMedia` 里**故意关掉了**浏览器的 `autoGainControl`
   * （它会把语音动态压平、让能量法端点检测更难判断），
   * 所以只能自己补一个**只抬整体、不压动态**的增益。
   *
   * ## 算法
   *
   * ```
   * 每块算 RMS
   *   若 RMS > 近期峰值 -> 峰值快速跟上（attack 0.5）
   *   否则            -> 峰值缓慢回落（release 0.002，约几十秒）
   * 目标增益 = _agcTarget / 峰值，夹在 [0.5, 12]
   * 当前增益向目标靠拢，每块 2%（避免突变让 VAD 误判）
   * ```
   *
   * ⚠️ **只在安静时更新目标**：说话声如果也参与"峰值"，它会把自己压下去
   *    —— 这和 VAD 噪声底"只在静音时更新"是同一条道理。
   *    这里用"当前 RMS 明显低于峰值"来近似判断"现在没人说话"。
   *
   * ⚠️ 增益**有上限 12x**：再高会把底噪也放大成"语音"，反而更糟。
   *    如果 12x 还不够，说明问题在系统音量，界面会提示用户去调。
   *
   * @returns {number} 这一块要乘的增益
   */
  _applyAgc(channel) {
    if (this.agcEnabled === false) return 1;
    let sum = 0;
    for (let i = 0; i < channel.length; i += 1) sum += channel[i] * channel[i];
    const rms = Math.sqrt(sum / (channel.length || 1));

    // 峰值跟踪
    if (rms > this._agcPeak) {
      this._agcPeak += (rms - this._agcPeak) * 0.5;   // attack：快
    } else {
      this._agcPeak += (rms - this._agcPeak) * 0.002; // release：慢
    }
    if (this._agcPeak < 1e-6) this._agcPeak = 1e-6;

    // 只在"安静"时更新目标增益（rms 明显低于峰值 = 没人说话）
    const quiet = rms < this._agcPeak * 0.6;
    if (quiet) {
      const want = this._agcTarget / this._agcPeak;
      this._agcWant = Math.max(0.5, Math.min(12, want));
    }
    const target = this._agcWant ?? 1;
    this._agcGain += (target - this._agcGain) * 0.02;  // 每块 2%，慢速靠拢
    return this._agcGain;
  }

  /** 把重采样后的浮点样本切成固定长度的 int16 帧发出去 */
  pushSamples(samples) {    /*
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
      this.gain?.disconnect();
    } catch {
      /* 断开失败无所谓 */
    }
    this.node = null;
    this.source = null;
    // ⚠️ 增益节点属于**那个已经关掉的 AudioContext**，必须一起丢掉：
    // 留着它，下一次 play() 会往一个已关闭的 context 上接节点（静音且不报错）。
    this.gain = null;
    this.stream?.getTracks().forEach((track) => track.stop());
    this.stream = null;
    // AudioContext 不关会一直占着音频设备
    this.context?.close?.().catch?.(() => {});
    this.context = null;
    this.pending = [];
    this.pendingCount = 0;
  }

  /**
   * 设置播放音量（0..100，100 = 原音量，0 = 静音）。
   *
   * 2026-10-19 用户要求「可调节声音大小」。只改增益，不动已解码的音频 ——
   * 所以正在播的那一句也会立刻变大小声。
   */
  setVolume(percent) {
    const raw = Number(percent);
    const clamped = Math.max(0, Math.min(100, Number.isFinite(raw) ? raw : 100));
    this.playbackVolume = clamped / 100;
    // ⚠️ 改的是**播放**增益（`playbackGain`），不是采集那个 `gain`（2026-10-30）
    if (this.playbackGain) {
      try {
        this.playbackGain.gain.value = this.playbackVolume;
      } catch {
        /* 节点已经被 context 释放 */
      }
    }
  }

  /**
   * 播放宠物语音。
   *
   * 目前 Python 侧默认不出声（tts.provider=none），所以这条路径通常不会走到；
   * 配上 TTS 之后它才是主路径。
   */
  async play(payload, format) {
    /*
     * 🔴 context 可能是**已关闭**的（2026-10-30 修）
     *
     * `stop()` 会 `this.context.close()` 并把 `this.context` 置 null ——
     * 但 `stopPlayback()`（上一段语音结束时调）**不关 context**。
     *
     * 问题在于：`this.playbackGain` 属于**创建它的那个 context**。
     * 如果 context 被换掉（新建），旧 `playbackGain` 还连着旧的 destination，
     * 声音就出不来了 —— 而且**不报错**。
     *
     * 所以这里检测 context 是否可用，不可用就重建并**丢掉旧的 playbackGain**。
     */
    if (!this.context || this.context.state === 'closed') {
      this.context = new AudioContext();
      // 旧 gain 属于旧 context，必须丢掉（否则 play 会往死节点上接）
      this.playbackGain = null;
    }
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
    /*
     * 音量（2026-10-19 加）：走一个 GainNode，而不是直接接 destination。
     *
     * 增益节点**复用**（不是每段语音新建一个）：这样 `setVolume()` 改一次，
     * 正在播的那一段也会立刻跟着变 —— 用户拖动滑杆时能马上听出区别。
     * 直接接 destination 的话，音量只能在"下一段语音"生效。
     */
    /*
     * 🔴 必须用**独立字段 `playbackGain`**，不能复用 `this.gain`（2026-10-30 修）
     *
     * ## 这个 bug 的形态
     *
     * `this.gain` 原来被**两个完全不同的用途共用**：
     *
     *     麦克风采集（`start()`）  this.gain -> this.node（录音链路）
     *     语音播放（`play()`）     this.gain -> context.destination（扬声器）
     *
     * 而这里写的是 `if (!this.gain)` —— **采集已经创建过它就跳过创建**。
     * 于是播放时音频被接到了**麦克风采集链路**（`this.node`）里，
     * 根本没连到 `destination`：
     *
     *     语音 -> gain(采集用的) -> node(录音用的) -> mute -> destination(0 增益)
     *                                                          ^^^^^^^^^^^^
     *                                                          这里是**静音**的
     *
     * ## 为什么"有时有声音、有时没有"
     *
     * 取决于**先开过麦克风没有**：
     *
     *     没开过麦克风 -> this.gain 是 null -> play() 新建并接 destination -> 有声 ✅
     *     开过麦克风   -> this.gain 已存在 -> 复用 -> 接到录音链路 -> **没声** ❌
     *
     * 用户报的"现在又没声音了"正是这个：他那次开着语音（麦克风开着），
     * 所以 `this.gain` 已经被采集链路占用了。
     *
     * ⚠️ 这条链路还经过 `mute`（采集时为防啸叫设的 **0 增益**节点），
     *    所以就算接对了 gain，声音也会被那个 mute 吃掉。
     */
    if (!this.playbackGain) {
      this.playbackGain = context.createGain();
      this.playbackGain.gain.value = this.playbackVolume;
      this.playbackGain.connect(context.destination);
    }
    this.playbackGain.gain.value = this.playbackVolume;
    source.connect(this.playbackGain);
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
