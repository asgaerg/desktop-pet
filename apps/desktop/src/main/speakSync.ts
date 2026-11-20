/**
 * 文字等语音：让 `say.end` 等到 TTS 合成完再显示（2026-11-20 用户要求）。
 *
 * ## 用户原话
 *
 * > 「现在将文字和语音同步输出，在有宠物语音开始时，文字将会等待语音
 * >   合成完毕一起输出。」
 *
 * ## 改之前是什么样
 *
 * `pet.say.end` 一到，主进程**立刻**转发给气泡窗口 —— 文字马上出现。
 * 而 TTS 是 sidecar 里**另一个订阅者**，它收到同一条事件才开始合成，
 * 等主进程的 PowerShell 合成完、音频传到前端，已经过去 1~3 秒。
 *
 *     文字出现  ─────────────────────────►
 *     语音开始      ────── 1~3 秒后 ─────►
 *
 * 用户看到的是"字先说完了，声音才慢吞吞出来"。
 *
 * ## 为什么不能简单地"在 sidecar 里先等 TTS"
 *
 * TTS 本身**由 `say.end` 触发**（`VoicePipeline._on_pet_say_end_speak`
 * 订阅的就是这个事件）。在 `say.end` 之前等 TTS = 等一个还没开始的东西，
 * 直接死锁。
 *
 * ## 所以改在主进程：**先扣住，等信号再放**
 *
 *     say.end 到达
 *        ├── 语音没开 / provider=none  -> 立刻放（不拖慢纯文字用户）
 *        └── 语音开着
 *              ├── 扣住这条消息
 *              ├── 等 `voice.audio`（合成好了）或 `voice.tts_failed`（失败）
 *              └── 放出去 -> 文字和声音几乎同时出现
 *
 * ## 三个必须处理的边界
 *
 * ① **超时** —— 云端 TTS 可能一直不回应。扣住不放会让文字**永远不出现**，
 *    那比"不同步"糟糕得多。所以有一个上限（默认 6 秒）。
 * ② **失败** —— 用户明确要求「TTS 失败时立刻显示文字」。
 * ③ **新一轮回复** —— 上一句还扣着没放，下一句的 `say.start` 就来了。
 *    必须先把旧的放出去，否则文字会整段丢失。
 *
 * ⚠️ 这个类是**纯逻辑**（不碰窗口、不碰 IPC），方便单元测试。
 *    真正的转发由调用方在 `onRelease` 回调里做。
 */

/** 等待 TTS 的上限。超了就放弃等待、直接显示文字。 */
export const DEFAULT_TTS_WAIT_MS = 6000;

export interface PendingSayEnd {
  /** 被扣住的那条消息（原样保存，放出去时一字不改） */
  message: { type?: string };
  /** 扣住的时刻，用于超时判断与日志 */
  since: number;
}

export class SpeakSyncGate {
  private pending: PendingSayEnd | null = null;
  private timer: ReturnType<typeof setTimeout> | null = null;

  /**
   * 最近一次"这句不用等语音"的信号时刻（毫秒）。
   *
   * ## 为什么需要它（2026-11-20 实测踩到）
   *
   * `voice.tts_skipped` **可能比 `say.end` 先到**。实测日志：
   *
   *     16:36:04.614  收到 tts_skipped 信号：免打扰 / 静音时段
   *     16:36:04.635  扣住 say.end，等 TTS 合成完再显示文字   ← 晚了 21ms
   *     16:36:10.636  等了 6000ms 还没等到 TTS —— 放弃等待
   *
   * 也就是信号来的时候**还没有东西可放行**，白等 6 秒。
   *
   * 根因：sidecar 里 `speak()` 在 `pet.say.end` 的订阅链里跑，
   * 而总线是**串行 await 所有订阅者** —— 语音那个订阅者先跑完
   * （发了跳过信号），主进程转发 `say.end` 的订阅者在后面。
   *
   * 所以：收到跳过信号时先**记一笔**，`say.end` 来时看一眼，
   * 刚记过就直接放行，不再扣。
   */
  private lastSkipAt = 0;

  /**
   * 跳过信号的有效期（毫秒）。
   *
   * 太短（< 100ms）会漏掉上面那种 21ms 的时序；
   * 太长（> 5s）会让"上一句跳过了"错误地影响"下一句该等"。
   * 1 秒足够覆盖同一条回复内的时序，又不会跨到下一轮。
   */
  private static readonly SKIP_WINDOW_MS = 1000;

  constructor(
    private readonly opts: {
      /** 多久还没等到 TTS 就放弃等待 */
      waitMs?: number;
      /** 放出一条被扣住的消息（调用方在这里真正转发给气泡窗口） */
      onRelease: (message: { type?: string }, why: string) => void;
      /** 诊断日志（测试里可以传一个收集器） */
      log?: (line: string) => void;
    },
  ) {}

  private get waitMs(): number {
    return this.opts.waitMs ?? DEFAULT_TTS_WAIT_MS;
  }

  private log(line: string): void {
    this.opts.log?.(line);
  }

  /**
   * 判断这条 `say.end` 要不要扣住。
   *
   * @param shouldWait 调用方算好的条件（语音开着 = true）
   * @returns true = **已扣住**，调用方不要转发；false = 立刻转发
   */
  hold(message: { type?: string }, shouldWait: boolean): boolean {
    /*
     * 不等待时，先**把上一条扣着的放掉**。
     *
     * 否则会出现这种坏情况：上一条还在等 TTS（比如云端正卡着），
     * 用户关了语音、下一句来了 —— 上一句就永远留在缓冲里消失掉。
     */
    if (!shouldWait) {
      this.flush('轮到新的一句，且这次不等语音');
      return false;
    }

    /*
     * 🔴 刚收到过"跳过"信号 -> 立刻放，不扣（2026-11-20 实测）
     *
     * 时序：sidecar 的 `speak()` 在 say.end 的订阅链里先跑，发了
     * `voice.tts_skipped`；主进程转发 say.end 的订阅者在后面到。
     * 于是信号**先到**、`say.end` **后到**，不查这一下就会白等超时。
     */
    const skipAge = Date.now() - this.lastSkipAt;
    if (this.lastSkipAt > 0 && skipAge <= SpeakSyncGate.SKIP_WINDOW_MS) {
      this.log(
        `[语音同步] 刚收到跳过信号（${skipAge}ms 前），这句直接显示，不扣`,
      );
      return false;
    }

    // 上一句还没放 -> 先放它（否则文字整段丢失）
    if (this.pending) {
      this.flush('新的一句到了，先把旧的放出去');
    }

    this.pending = { message, since: Date.now() };
    this.log('[语音同步] 扣住 say.end，等 TTS 合成完再显示文字');

    this.timer = setTimeout(() => {
      this.log(`[语音同步] 等了 ${this.waitMs}ms 还没等到 TTS —— 放弃等待，直接显示文字`);
      this.flush('等待超时');
    }, this.waitMs);
    // 不阻止进程退出（否则测试和关窗会被这个计时器拖住）
    this.timer.unref?.();

    return true;
  }

  /**
   * TTS 有结果了（成功或失败）—— 放行。
   *
   * ⚠️ 重复调用是安全的（先到的那个生效，后面的什么都不做）。
   *    因为 `voice.audio` 和 `voice.tts_failed` 可能都到。
   */
  release(why: string): void {
    if (!this.pending) return;
    this.flush(why);
  }

  /**
   * 收到"这句不会有声音"的信号（`voice.tts_skipped`）。
   *
   * 和 `release` 的区别：**先记时刻**，再尝试放行。
   *
   * ⚠️ 必须记时刻 —— 这个信号可能比 `say.end` **先到**（实测差 21ms），
   *    那时还没有东西可放行。记下来，等 `say.end` 到了在 `hold()` 里查。
   */
  notifySkipped(why: string): void {
    this.lastSkipAt = Date.now();
    this.log(`[语音同步] 记下跳过信号（${why}）`);
    if (this.pending) this.flush(why);
  }

  /** 当前是否有被扣住的消息（测试用） */
  get isHolding(): boolean {
    return this.pending !== null;
  }

  /** 被扣住多久了（毫秒）；没有则 0。测试与日志用。 */
  get heldForMs(): number {
    return this.pending ? Date.now() - this.pending.since : 0;
  }

  /** 丢掉扣住的消息且**不转发**（只在窗口销毁等极端情况下用） */
  drop(reason: string): void {
    if (!this.pending) return;
    this.log(`[语音同步] 丢弃扣住的文字（${reason}）`);
    this.clearTimer();
    this.pending = null;
  }

  private flush(why: string): void {
    const item = this.pending;
    if (!item) return;
    this.clearTimer();
    this.pending = null;
    this.log(`[语音同步] 放出文字（${why}，已等 ${Date.now() - item.since}ms）`);
    this.opts.onRelease(item.message, why);
  }

  private clearTimer(): void {
    if (this.timer) {
      clearTimeout(this.timer);
      this.timer = null;
    }
  }
}
