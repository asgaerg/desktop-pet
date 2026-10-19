/**
 * 序列帧播放器 —— 立绘的核心，与素材格式无关的那一半。
 *
 * ⚠️ 这个文件是 **TypeScript**（`sprite.ts`），由 esbuild 打包成 `renderer/sprite.js`。
 * 不要直接把它当 JS 喂给浏览器：早期版本叫 sprite.js 却写着 `export interface`，
 * 浏览器解析模块时直接语法报错，导致立绘在整个 P1 阶段其实从未加载过 ——
 * 而 Python 侧的自检全绿，完全看不出问题。教训：渲染层必须有独立的语法/构建校验。
 *
 * 设计原则：**状态机在这里，素材在素材目录**。换一套立绘（不同作者、不同画风、
 * 甚至换成纯程序化绘制）只需要换 manifest 与图片，播放逻辑一行不改。
 *
 * 素材约定（`renderer/assets/avatar/<名字>/`）：
 *   avatar.json   {
 *                   "image": "sheet.png",
 *                   "frameWidth": 256, "frameHeight": 256,
 *                   "fps": 12,                      // 默认帧率
 *                   "animations": {
 *                     "idle":  { "frames": [0,1,2,3], "fps": 8, "loop": true },
 *                     "blink": { "frames": [4,5], "fps": 16, "loop": false },
 *                     "talk":  { "frames": [6,7,8,7], "fps": 12, "loop": true },
 *                     "drag":  { "frames": [9], "loop": true },
 *                     "sleep": { "frames": [10,11], "fps": 3, "loop": true }
 *                   }
 *                 }
 *   sheet.png     帧序列贴图（帧号 = 从左到右、再换行的下标）
 *
 * 缺素材时自动退化为"程序化绘制"的占位宠物 —— 保证在没有任何美术资源的情况下，
 * 状态机、口型、情绪联动依然可以被完整验证。
 */

import { VideoPlayer } from './videoPlayer.js';

/** 素材清单结构 */
export interface AvatarManifest {
  image?: string;
  frameWidth?: number;
  frameHeight?: number;
  fps?: number;
  /** 程序化占位宠物的配色（缺素材时生效） */
  palette?: {
    body?: string;
    belly?: string;
    eye?: string;
    cheek?: string;
  };
  animations?: Record<string, AnimationSpec>;
}

export interface AnimationSpec {
  frames: number[];
  fps?: number;
  loop?: boolean;
}

/**
 * 立绘状态。
 *
 * `peek` 是**别的应用全屏时的"趴着探头"** —— 它不是通用状态机的一部分，
 * 而是由主进程的全屏检测直接驱动（见 app.js 里 pet:compact 的处理）。
 * 素材来自 `move/左转奔跑` 里截出的侧躺段（见 tools/extract-clip.py）。
 */
export type AvatarState =
  | 'idle'
  | 'blink'
  | 'talk'
  | 'drag'
  | 'sleep'
  | 'happy'
  | 'curious'
  | 'peek';

/** 内置兜底动画：没有 manifest 时也能跑出完整状态机 */
const BUILTIN_ANIMATIONS: Record<AvatarState, AnimationSpec> = {
  idle: { frames: [0, 1, 2, 1], fps: 6, loop: true },
  blink: { frames: [3, 4], fps: 14, loop: false },
  talk: { frames: [5, 6, 7, 6], fps: 13, loop: true },
  drag: { frames: [8], fps: 4, loop: true },
  sleep: { frames: [9, 10], fps: 2.2, loop: true },
  happy: { frames: [11, 12, 11], fps: 10, loop: true },
  curious: { frames: [13, 14], fps: 5, loop: true },
};

export interface SpritePlayerOptions {
  canvas: HTMLCanvasElement;
  /** 素材目录，默认 assets/avatar/default */
  basePath?: string;
  /** 逻辑尺寸；实际像素按 devicePixelRatio 放大以保证清晰 */
  width: number;
  height: number;
}

export class SpritePlayer {
  private readonly canvas: HTMLCanvasElement;
  private readonly ctx: CanvasRenderingContext2D;
  /** 逻辑尺寸。可改 —— 见 setSize()（"可调整宠物大小"用的） */
  private width: number;
  private height: number;
  private readonly basePath: string;
  private readonly palette: NonNullable<AvatarManifest['palette']> = {};

  private manifest: AvatarManifest | null = null;
  private sheet: HTMLImageElement | null = null;
  private sheetReady = false;

  /**
   * 视频立绘播放器。非 null 时**所有绘制与播放都由它负责**，
   * 本类退化为门面（见 load() 的说明）。
   */
  private video: VideoPlayer | null = null;
  /** 是否已被要求启动（用于 load() 在 start() 之后才完成的情况） */
  private videoStarted = false;

  private state: AvatarState = 'idle';
  private spec: AnimationSpec = BUILTIN_ANIMATIONS.idle;
  private frameIndex = 0;
  private elapsed = 0;
  private lastTimestamp = 0;
  private rafId = 0;
  /** 已推进的动画帧数（测试用来确认循环真的在动） */
  private tickCount = 0;

  /** 由口型事件驱动的嘴部开合（0~1），程序化模式下直接画出来 */
  private mouthOpen = 0;
  /** 由情绪事件驱动的心情（-1~1），影响眼睛与嘴的弧度 */
  private mood = 0.3;
  private energy = 0.7;

  private blinkCountdown = 2;

  constructor(options: SpritePlayerOptions) {
    this.canvas = options.canvas;
    this.width = options.width;
    this.height = options.height;
    this.basePath = options.basePath ?? 'assets/avatar/default';

    const ctx = this.canvas.getContext('2d');
    if (!ctx) throw new Error('无法获取 2D 绘图上下文');
    this.ctx = ctx;
    this.resize();
  }

  /** 按设备像素比调整画布，避免高分屏发虚 */
  private resize(): void {
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    this.canvas.width = Math.round(this.width * dpr);
    this.canvas.height = Math.round(this.height * dpr);
    this.canvas.style.width = `${this.width}px`;
    this.canvas.style.height = `${this.height}px`;
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  /** 加载素材；失败不抛错，退化为程序化绘制。 */
  async load(): Promise<'sprite' | 'video' | 'procedural'> {
    // ---- 先探一下清单类型：`kind: "video"` 就整体交给视频播放器 ----
    //
    // 做成门面而不是让 app.js 自己选，是因为两者**对外接口完全一致**
    // （play / setMouth / setEmotion / start / stop / currentState / debug），
    // 换立绘形态不该让调用方跟着改。
    try {
      const probe = await fetch(`${this.basePath}/avatar.json`, { cache: 'no-cache' });
      if (probe.ok) {
        const peek = (await probe.json()) as { kind?: string };
        if (peek.kind === 'video') {
          const player = new VideoPlayer({
            canvas: this.canvas,
            basePath: this.basePath,
            width: this.width,
            height: this.height,
          });
          await player.load();
          this.video = player;
          this.videoStarted = Boolean(this.rafId);
          player.start();
          this.state = player.currentState;
          return 'video';
        }
      }
    } catch (error) {
      // 视频路径失败不要直接放弃 —— 下面还有序列帧与程序化两条回退
      console.info(`[avatar] 视频立绘加载失败，尝试序列帧：${(error as Error).message}`);
    }

    try {
      const response = await fetch(`${this.basePath}/avatar.json`, { cache: 'no-cache' });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const manifest = (await response.json()) as AvatarManifest;
      if (!manifest.image) throw new Error('manifest 缺少 image 字段');
      this.manifest = manifest;
      Object.assign(this.palette, manifest.palette ?? {});

      const image = new Image();
      image.src = `${this.basePath}/${manifest.image}`;
      await new Promise<void>((resolve, reject) => {
        image.onload = () => resolve();
        image.onerror = () => reject(new Error(`无法加载贴图 ${image.src}`));
      });
      this.sheet = image;
      this.sheetReady = true;
      this.play('idle', true);
      return 'sprite';
    } catch (error) {
      // 素材缺失是常态（接真美术之前根本没有素材），不该刷错误吓人
      console.info(`[avatar] 使用程序化占位宠物（未加载到序列帧素材：${(error as Error).message}）`);
      this.sheetReady = false;
      this.play('idle', true);
      return 'procedural';
    }
  }

  get mode(): 'sprite' | 'video' | 'procedural' {
    if (this.video) return 'video';
    return this.sheetReady ? 'sprite' : 'procedural';
  }

  get currentState(): AvatarState {
    return this.state;
  }

  /**
   * 切换动画。
   * `restart` 为真时即使同名也从头播（用于"再眨一次眼"这种重复触发）。
   */
  play(state: AvatarState, restart = false): void {
    // 视频模式：整体委托。两边状态要保持同步，否则 `currentState` 会说谎
    // （app.js 会用它判断"现在是不是在 drag"，不同步会让拖拽表现错乱）。
    if (this.video) {
      this.video.play(state, restart);
      this.state = this.video.currentState;
      return;
    }
    if (state === this.state && !restart) return;
    this.state = state;
    this.spec = this.resolveSpec(state);
    this.frameIndex = 0;
    this.elapsed = 0;
  }

  /**
   * 决定某个状态该播哪一段。
   *
   * 回退顺序（**改了**，原来有 bug）：
   *   ① manifest 里该状态自己的帧
   *   ② manifest 里的 idle
   *   ③ 内置 idle（只有完全没有素材时才走到这里）
   *
   * 原来的链是 `manifest?.[state] ?? BUILTIN_ANIMATIONS[state] ?? …idle` ——
   * 中间那步是错的：`BUILTIN_ANIMATIONS` 是**程序化占位宠物**用的帧号，
   * 而真素材的贴图上根本没有那些格子。所以"加载了真素材、但缺少某个状态"
   * 时（比如素材只有待机，没有专门的眨眼），它会去取占位宠物的帧号，
   * 在真贴图上画出一块错位的图 —— 看起来像抽搐或花屏。
   *
   * 正确做法是缺状态就回到**本素材的 idle**，宁可动得单调，也不要画错。
   */
  private resolveSpec(state: AvatarState): AnimationSpec {
    const anims = this.manifest?.animations;
    const own = anims?.[state];
    if (own) return own;
    const idle = anims?.idle;
    if (idle) {
      if (state !== 'idle') {
        console.info(`[avatar] 素材里没有 ${state} 的帧，回退到 idle`);
      }
      return idle;
    }
    return BUILTIN_ANIMATIONS[state] ?? BUILTIN_ANIMATIONS.idle;
  }

  /** 口型：由 pet.mouth 事件的包络驱动，取值 0~1 */
  setMouth(open: number): void {
    this.mouthOpen = Math.max(0, Math.min(1, open));
    this.video?.setMouth(this.mouthOpen);
  }

  /** 情绪：影响表情。mood 越大越开心，energy 越低越困。 */
  setEmotion(mood: number, energy: number): void {
    this.mood = Math.max(-1, Math.min(1, mood));
    this.energy = Math.max(0, Math.min(1, energy));
    this.video?.setEmotion(this.mood, this.energy);
    if (this.video) return;
    if (energy < 0.3 && this.state === 'idle') this.play('sleep');
    else if (energy >= 0.3 && this.state === 'sleep') this.play('idle');
  }

  start(): void {
    if (this.video) {
      // 视频模式下由视频播放器自己驱动 rAF；这里只记住"已经被要求启动过"，
      // 因为 load() 可能在 start() 之后才完成（它要等清单与视频就绪）。
      this.videoStarted = true;
      this.video.start();
      return;
    }
    if (this.rafId) return;
    this.lastTimestamp = performance.now();
    const tick = (timestamp: number): void => {
      const delta = Math.min(timestamp - this.lastTimestamp, 250) / 1000;
      this.lastTimestamp = timestamp;
      this.update(delta);
      this.draw();
      this.rafId = requestAnimationFrame(tick);
    };
    this.rafId = requestAnimationFrame(tick);
  }

  stop(): void {
    if (this.video) {
      this.videoStarted = false;
      this.video.stop();
      return;
    }
    if (this.rafId) cancelAnimationFrame(this.rafId);
    this.rafId = 0;
  }

  /** 设置立绘旋转角度（度）。用于"边缘探头"的 45° 姿态。 */
  setRotation(deg: number): void {
    this.video?.setRotation(deg);
  }

  /**
   * 改立绘尺寸（"可调整宠物大小"）。
   *
   * 视频模式委托给播放器；序列帧模式重设画布与变换（绘制时按
   * `min(width/frameWidth, height/frameHeight)` 等比缩放，所以改尺寸不会变形）。
   */
  setSize(width: number, height: number): void {
    const w = Math.max(48, Math.round(width));
    const h = Math.max(48, Math.round(height));
    if (this.video) {
      this.video.setSize(w, h);
      return;
    }
    if (w === this.width && h === this.height) return;
    this.width = w;
    this.height = h;
    this.resize();
  }

  /**
   * 转发"立绘内容占画布的比例"给视频实现。
   *
   * 只有视频模式需要它：序列帧模式本来就按 `min(w/frameW, h/frameH)` 等比
   * 适配，内容占比是算出来的。视频模式的 `drawKeyed` 原来是"铺满画布"，
   * 于是画布尺寸 ≠ 立绘尺寸 —— 而窗口要贴合立绘，就必须让两者相等。
   */
  setContentScale(x: number, y: number): void {
    this.video?.setContentScale(x, y);
  }

  /**
   * 注册"第一帧抠好并画到画布"的回调（视频模式才用得上）。
   *
   * 用户要求：「干脆等立绘完全加载好再显示好了」。
   * 由 `app.js` 传进来 —— 它会据此撤销"首帧门闸"、让立绘淡入
   * （见 `styles.css` 里 `.pet` 的 `opacity: 0`）。
   *
   * 序列帧模式下没有这个概念（位图直接解码就有正确画面），所以直接返回。
   */
  onFirstFrame(cb: () => void): void {
    if (this.video) this.video.onFirstFrame = cb;
  }

  // ------------------------------------------------------------------ 更新

  private update(delta: number): void {
    this.tickCount += 1;
    const fps = this.spec.fps ?? this.manifest?.fps ?? 10;
    const frameDuration = 1 / Math.max(1, fps);
    this.elapsed += delta;
    while (this.elapsed >= frameDuration) {
      this.elapsed -= frameDuration;
      const last = this.spec.frames.length - 1;
      if (this.frameIndex >= last) {
        if (this.spec.loop === false) {
          this.play('idle'); // 一次性动画结束：回到 idle（眨眼就是这条路径）
          break;
        }
        this.frameIndex = 0;
      } else {
        this.frameIndex += 1;
      }
    }

    // 自动眨眼：只在 idle 时随机触发，让宠物看起来是活的。
    //
    // ⚠️ 只有当**素材真的提供了独立的 blink 帧**时才这么做。
    // 否则会与素材自带的眨眼叠加：这套素材的待机循环里本来就有一段闭眼
    // （帧 32-43），而这个计时器又每 2.4~5.6 秒切一次 blink —— 两套独立跑，
    // 看起来就是"眨眼眨个不停"。（用户报过这个现象。）
    //
    // 素材自带眨眼时，让循环自己眨就够了：一次循环眨一次，节奏自然。
    if (this.state === 'idle' && this.hasOwnBlink) {
      this.blinkCountdown -= delta;
      if (this.blinkCountdown <= 0) {
        this.blinkCountdown = 2.4 + Math.random() * 3.2;
        this.play('blink', true);
      }
    }
  }

  /**
   * 素材是否自带独立的 blink 帧。
   *
   * 用于决定"要不要再叠加一层自动眨眼"。素材没给 blink 时叠加，
   * 素材给了就别叠加 —— 否则两套眨眼会互相穿插，显得过密。
   */
  private get hasOwnBlink(): boolean {
    const frames = this.manifest?.animations?.blink?.frames;
    return Array.isArray(frames) && frames.length > 0;
  }

  /** 当前应绘制的帧号 */
  get frame(): number {
    return this.spec.frames[Math.min(this.frameIndex, this.spec.frames.length - 1)] ?? 0;
  }

  /**
   * 内部状态快照（只读）。
   *
   * 存在的理由不只是调试：这个播放器的逻辑**无法在开发环境里目视验证**，
   * 所以测试台需要能够断言"切到 talk 了没有""帧有没有往前走"这类事实。
   * 只暴露读取、不提供写入口，避免它变成绕过状态机的后门。
   */
  get debug(): {
    state: AvatarState;
    frame: number;
    mode: 'sprite' | 'video' | 'procedural';
    mouth: number;
    mood: number;
    energy: number;
    tick: number;
    /** 视频模式下正在播的片源路径，便于排查"怎么播的是这个" */
    clip?: string;
  } {
    if (this.video) {
      // 视频模式下把 tick 也算上：测试要能断言"动画在动"，
      // 而视频没有帧号概念（frame 由 currentTime 换算，见 VideoPlayer.frame）。
      const v = this.video.debug;
      return {
        state: v.state,
        frame: v.frame,
        mode: 'video',
        mouth: v.mouth,
        mood: v.mood,
        energy: v.energy,
        tick: v.tick,
        clip: v.clip,
      };
    }
    return {
      state: this.state,
      frame: this.frame,
      mode: this.mode,
      mouth: this.mouthOpen,
      mood: this.mood,
      energy: this.energy,
      tick: this.tickCount,
    };
  }

  // ------------------------------------------------------------------ 绘制

  private draw(): void {
    const { ctx, width, height } = this;
    ctx.clearRect(0, 0, width, height);
    if (this.sheetReady && this.sheet && this.manifest) this.drawSpriteFrame();
    else this.drawProcedural();
  }

  private drawSpriteFrame(): void {
    const manifest = this.manifest!;
    const sheet = this.sheet!;
    const frameWidth = manifest.frameWidth ?? sheet.naturalHeight;
    const frameHeight = manifest.frameHeight ?? sheet.naturalHeight;

    // 按容器等比缩放，保持素材原始长宽比
    const scale = Math.min(this.width / frameWidth, this.height / frameHeight);
    const drawWidth = frameWidth * scale;
    const drawHeight = frameHeight * scale;
    const dx = (this.width - drawWidth) / 2;
    const dy = this.height - drawHeight;

    // 贴图可以是多行：按帧宽推列数，帧号先横后纵
    const columns = Math.max(1, Math.floor(sheet.naturalWidth / frameWidth));
    const frame = this.frame;
    const sx = (frame % columns) * frameWidth;
    const sy = Math.floor(frame / columns) * frameHeight;

    this.ctx.imageSmoothingEnabled = true;
    this.ctx.drawImage(sheet, sx, sy, frameWidth, frameHeight, dx, dy, drawWidth, drawHeight);
  }

  /**
   * 程序化占位宠物：一只会呼吸、会眨眼、说话时嘴会动的小家伙。
   *
   * 它不是为了好看，而是为了**在没有美术资源时让状态机可被验证**：
   * 口型包络、情绪、睡眠、拖拽都能立刻看出效果。
   */
  private drawProcedural(): void {
    const { ctx, width, height } = this;
    const body = this.palette.body ?? '#ffb35c';
    const belly = this.palette.belly ?? '#ffe0b8';
    const eye = this.palette.eye ?? '#3a2a1a';
    const cheek = this.palette.cheek ?? '#ff8a8a';

    const breathSpeed = this.state === 'sleep' ? 0.9 : 2.2;
    const breath = Math.sin((performance.now() / 1000) * breathSpeed) * (this.state === 'sleep' ? 3 : 2);
    const cx = width / 2;
    const cy = height * 0.62 + breath;
    const radius = Math.min(width, height) * 0.29;

    // 影子
    ctx.save();
    ctx.globalAlpha = 0.16;
    ctx.fillStyle = '#000';
    ctx.beginPath();
    ctx.ellipse(cx, cy + radius * 1.02, radius * 0.85, radius * 0.16, 0, 0, Math.PI * 2);
    ctx.fill();
    ctx.restore();

    // 身体
    ctx.beginPath();
    ctx.ellipse(cx, cy, radius * 0.92, radius, 0, 0, Math.PI * 2);
    const gradient = ctx.createLinearGradient(cx, cy - radius, cx, cy + radius);
    gradient.addColorStop(0, body);
    gradient.addColorStop(1, shade(body, -18));
    ctx.fillStyle = gradient;
    ctx.fill();
    ctx.lineWidth = 2;
    ctx.strokeStyle = 'rgba(90,60,20,0.35)';
    ctx.stroke();

    // 肚子
    ctx.beginPath();
    ctx.ellipse(cx, cy + radius * 0.3, radius * 0.52, radius * 0.44, 0, 0, Math.PI * 2);
    ctx.fillStyle = belly;
    ctx.globalAlpha = 0.75;
    ctx.fill();
    ctx.globalAlpha = 1;

    // 耳朵
    for (const side of [-1, 1]) {
      ctx.beginPath();
      ctx.moveTo(cx + side * radius * 0.52, cy - radius * 0.72);
      ctx.lineTo(cx + side * radius * 0.74, cy - radius * 1.34);
      ctx.lineTo(cx + side * radius * 0.2, cy - radius * 0.96);
      ctx.closePath();
      ctx.fillStyle = shade(body, -8);
      ctx.fill();
      ctx.strokeStyle = 'rgba(90,60,20,0.3)';
      ctx.stroke();
    }

    // 眼睛：眨眼时压扁成一条线，说话/好奇时睁大
    const blinking = this.state === 'blink';
    const eyeOpen = this.state === 'sleep' ? 0.12 : blinking ? 0.1 : this.state === 'curious' ? 1.25 : 1;
    const eyeY = cy - radius * 0.16;
    const eyeDx = radius * 0.34;
    const eyeR = radius * 0.13;

    for (const side of [-1, 1]) {
      const ex = cx + side * eyeDx;
      ctx.beginPath();
      ctx.ellipse(ex, eyeY, eyeR, eyeR * eyeOpen, 0, 0, Math.PI * 2);
      ctx.fillStyle = eye;
      ctx.fill();
      if (eyeOpen > 0.5) {
        ctx.beginPath();
        ctx.arc(ex - eyeR * 0.3, eyeY - eyeR * 0.3, eyeR * 0.32, 0, Math.PI * 2);
        ctx.fillStyle = 'rgba(255,255,255,0.9)';
        ctx.fill();
      }
    }

    // 腮红：心情好时才明显
    if (this.mood > 0.15 && this.state !== 'sleep') {
      ctx.globalAlpha = Math.min(0.55, 0.2 + this.mood * 0.35);
      for (const side of [-1, 1]) {
        ctx.beginPath();
        ctx.ellipse(
          cx + side * radius * 0.58,
          cy + radius * 0.12,
          radius * 0.16,
          radius * 0.1,
          0,
          0,
          Math.PI * 2,
        );
        ctx.fillStyle = cheek;
        ctx.fill();
      }
      ctx.globalAlpha = 1;
    }

    // 嘴：口型事件直接驱动张开高度；没有口型数据时按情绪画一个静态嘴
    const mouthY = cy + radius * 0.28;
    const open = this.mouthOpen;
    ctx.beginPath();
    if (open > 0.04) {
      ctx.ellipse(
        cx,
        mouthY + open * radius * 0.06,
        radius * (0.16 + open * 0.08),
        radius * (0.06 + open * 0.2),
        0,
        0,
        Math.PI * 2,
      );
      ctx.fillStyle = '#8c3b3b';
      ctx.fill();
    } else if (this.state === 'happy' || this.mood > 0.5) {
      ctx.arc(cx, mouthY - radius * 0.08, radius * 0.2, 0.15 * Math.PI, 0.85 * Math.PI);
      ctx.lineWidth = 2.4;
      ctx.strokeStyle = eye;
      ctx.stroke();
    } else {
      ctx.moveTo(cx - radius * 0.12, mouthY);
      ctx.lineTo(cx + radius * 0.12, mouthY);
      ctx.lineWidth = 2.4;
      ctx.lineCap = 'round';
      ctx.strokeStyle = eye;
      ctx.stroke();
    }

    // 困了画个 Z
    if (this.state === 'sleep' || this.energy < 0.25) {
      ctx.font = `${Math.round(radius * 0.34)}px system-ui, sans-serif`;
      ctx.fillStyle = 'rgba(80,80,120,0.75)';
      const t = (performance.now() / 1000) % 3;
      for (let i = 0; i < 3; i += 1) {
        const phase = (t + i * 0.6) % 3;
        ctx.globalAlpha = Math.max(0, 1 - phase / 3);
        ctx.fillText('Z', cx + radius * 0.7 + phase * 6, cy - radius * (0.7 + phase * 0.45));
      }
      ctx.globalAlpha = 1;
    }
  }
}

/** 把 #rrggbb 调亮/调暗，用于给纯色身体做渐变。 */
function shade(hex: string, amount: number): string {
  const match = /^#([0-9a-f]{6})$/i.exec(hex.trim());
  if (!match) return hex;
  const value = parseInt(match[1], 16);
  const clamp = (n: number): number => Math.max(0, Math.min(255, n));
  const r = clamp(((value >> 16) & 0xff) + amount);
  const g = clamp(((value >> 8) & 0xff) + amount);
  const b = clamp((value & 0xff) + amount);
  return `rgb(${r},${g},${b})`;
}
