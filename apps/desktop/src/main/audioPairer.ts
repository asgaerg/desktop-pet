/**
 * 语音音频配对：把 sidecar 发来的"元信息"与"二进制帧"合成一条。
 *
 * 为什么需要单独一个模块：sidecar 发语音时是**两条通道**——
 * 先发一条 `voice.audio`（JSON，带 index），紧接着发同序号的二进制帧。
 * 一个 WebSocket 上文本与二进制互不保序（各自是独立的消息类型），
 * 所以必须靠**序号**配对，不能靠到达顺序去猜。
 *
 * 这段逻辑原先写在主进程的 socket 回调里，是一个闭包，因此无法单独测试 ——
 * 而它恰恰是最容易写错、错了又最难发现的那种（表现是"语音不出声"，
 * 而且只在真的配上 TTS 之后才暴露）。抽出来之后可以把各种到达顺序都验一遍。
 *
 * ⚠️ 这里有一个**曾经写错并流出**的坑，写下来避免重蹈：
 * 二进制的序号**必须**跳过"已经有元信息在等"的号，否则当元信息先到时会变成
 * `meta(1)` 之后 `binary` 被记成 2 —— 与 1 永远配不上对，语音全丢。
 */

export interface PairedAudio {
  /** 音频格式（'pcm' / 'mp3' …） */
  format: string;
  /** 音频字节 */
  bytes: Uint8Array;
  /** 该音频对应的文本（便于界面显示"它正在说什么"，可能为空） */
  text: string;
}

interface PendingEntry {
  bytes?: Uint8Array;
  format?: string;
  text?: string;
}

export class AudioPairer {
  private pending = new Map<number, PendingEntry>();
  private nextIndex = 1;

  /**
   * @param maxPending 同时最多挂起多少段。语音是实时的，迟到太久的音频
   *   没有播放价值，攒着只会占内存。
   */
  constructor(private readonly maxPending: number = 8) {}

  /**
   * 收到二进制帧（音频本体）。
   *
   * 序号选的是"下一个既不是已有二进制、也没有元信息在等的号" ——
   * 关键在于**跳过已有元信息的号**（见文件头的说明）。
   */
  acceptBinary(payload: Uint8Array): void {
    const index = this.nextFreeIndex();
    const entry = this.pending.get(index) ?? {};
    entry.bytes = payload;
    this.pending.set(index, entry);
    this.prune();
  }

  private nextFreeIndex(): number {
    let index = this.nextIndex;
    while (true) {
      const entry = this.pending.get(index);
      if (entry === undefined || entry.bytes === undefined) break;
      index += 1;
    }
    this.nextIndex = index + 1;
    return index;
  }

  /**
   * 收到元信息。返回此刻可以播放的音频（可能为空：二进制帧还没到）。
   * 不假设顺序：元信息可能先到，也可能因为分帧而晚到。
   */
  acceptMeta(index: number, format: string, text: string): PairedAudio[] {
    if (!Number.isFinite(index) || index <= 0) return [];
    const entry = this.pending.get(index) ?? {};
    entry.format = format;
    entry.text = text;
    this.pending.set(index, entry);
    // 注意：**不要**在这里推进 nextIndex。早期版本为了让"元信息先到"看起来
    // 正常，把 nextIndex 设成 index+1，结果紧随其后的二进制帧被记到了下一个号上，
    // 两边永远配不上对 —— 语音静默失效。二进制的取号只由 nextFreeIndex 决定。
    this.prune();
    return this.drain();
  }

  /** 取出所有"两边都到齐"的条目，按序号升序返回（顺序不能乱）。 */
  drain(): PairedAudio[] {
    const ready: PairedAudio[] = [];
    for (const index of [...this.pending.keys()].sort((a, b) => a - b)) {
      const entry = this.pending.get(index);
      if (!entry?.bytes || !entry.format) continue;
      ready.push({ format: entry.format, bytes: entry.bytes, text: entry.text ?? '' });
      this.pending.delete(index);
    }
    return ready;
  }

  /** 丢弃最旧的挂起项，避免永远配不上对的条目无限攒着。 */
  private prune(): void {
    while (this.pending.size > this.maxPending) {
      const oldest = Math.min(...this.pending.keys());
      this.pending.delete(oldest);
    }
  }

  get pendingCount(): number {
    return this.pending.size;
  }
}
