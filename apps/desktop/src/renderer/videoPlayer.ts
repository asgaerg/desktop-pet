/**
 * 角色在源帧（640x360）里的**实测外接框** —— `cropRect()` 用它，而不是 `hitboxes.body`。
 *
 * 由 `tools/_measure-char-bounds.py` 量出：抽 8 个不同状态的视频、逐帧找前景、取并集，
 * 实测并集是 `[136, 36, 463, 341]`（宽 328、高 306）。
 *
 * ## ⚠️ 这里刻意做成**正方形**（300x300），不是紧贴并集
 *
 * 绘制是"裁切区**铺满**画布"（`scale = min(画布/裁切)`），所以裁切区的**宽高比**
 * 决定立绘能占多大：
 *
 *   · 裁切区 347x325（前一版）→ 缩放受**高度**限制 `240/325 = 0.738`，宽度浪费
 *   · 裁切区 300x300（现在）  → 缩放 `240/300 = 0.8`，**两个方向都用满**
 *
 * 而且画布本身是正方形 —— 方形裁切区铺满后**天然居中**，
 * 不会再出现"偏下偏左"（旧绘制是"横向居中、纵向贴底"）。
 *
 * 边长 300 的依据：角色本身高 306，留一点余量后取整。
 *
 * ⚠️ 别用 `hitboxes.body = [212,60,428,330]` 当裁切基准 —— 那只是**躯干**
 * （可拖拽区域），拿它加大留白会胀到接近整帧，立绘被一起缩小。
 * 完整推导见 `cropRect()` 的注释。
 *
 * ⚠️ **换素材后必须重跑测量脚本更新这个值**，否则立绘会偏小 / 被裁。
 */
export const ART_FIT_BOX: readonly [number, number, number, number] = [177, 43, 477, 343];

/**
 * webm 视频立绘播放器。
 *
 * 与 `SpritePlayer`（序列帧）**实现同一套对外接口**，所以 `app.js` 不需要区分
 * 两者 —— 由 `sprite.ts` 读清单里的 `kind` 字段决定委托给谁。
 *
 * ---- 为什么需要它 ----
 *
 * 这套素材（参考桌宠应用里的 shenshen）只有 webm，没有序列帧。
 * 89 段动作合计 44 MB，转成贴图会膨胀好几倍；视频由浏览器原生解码、按需加载。
 *
 * ---- 必须自己抠黑底（这是最大的坑）----
 *
 * ⚠️ 这些视频**没有 alpha 通道**，背景是**纯黑**。实测：
 *   · 像素格式 `yuv420p`（不是 `yuva420p`），canvas 读出的角落像素是 `[0,0,0,255]`
 *   · 所以不能直接放进 DOM —— 会带一块黑方块
 *
 * 我试过 `mix-blend-mode: screen`（黑色在 screen 下等于透明）：**不行** ——
 * 它会把角色的深色部分与页面底色混合，角色变成一层淡影。
 *
 * 正解是**逐帧把黑像素的 alpha 置 0**。好在黑底非常干净，存在清晰的阈值线：
 *   · 81.7% 的像素是纯 `(0,0,0)`
 *   · 每通道 ≤6 的过渡像素只占 **0.004%**（230400 个像素里 10 个）
 * 所以简单阈值就能抠干净，且不会在角色暗部打出洞。
 *
 * ---- 性能 ----
 *
 * 抠图是逐帧 `getImageData` + 写回，CPU 有开销。所以：
 *   · 只在视频尺寸下抠一次（640×360），再缩放画到显示画布；
 *   · 已经抠过的帧画到 canvas 后仍带 alpha，后续 `drawImage` 直接复用，
 *     不需要重复读像素。
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

/** 清单里的一段动画：给一组文件名，播放器随机挑一个 */
interface ClipSpec {
  files: string[];
  /** 是否循环。缺省按状态推断（见 LOOPING_STATES） */
  loop?: boolean;
}

export interface VideoManifest {
  kind?: string;
  basePath?: string;
  /** 黑底抠图阈值（每通道 ≤ 此值即判为背景） */
  blackKeyThreshold?: number;
  /** 命中框：用于区分点身体 / 摸头 */
  hitboxes?: { body?: number[]; head?: number[] };
  /** 含可读文字的动画，不参与水平镜像 */
  noMirror?: string[];
  animations?: Record<string, string[] | ClipSpec>;
  randomPool?: Record<string, string[]>;
}

/**
 * 哪些状态该循环播放。
 *
 * `blink` / `happy` / `curious` 这类是**一次性反应**：播完要自动回到 idle，
 * 否则宠物会一直停在挥手动作上。
 */
const LOOPING_STATES: ReadonlySet<AvatarState> = new Set<AvatarState>([
  'idle',
  'talk',
  'drag',
  'sleep',
  // 探头是持续姿势，必须循环（否则播完一次就回到站姿，看起来像抽搐）
  'peek',
]);

/** 状态别名：素材清单里可能用了别的名字，这里做一次归一 */
const STATE_ALIASES: Record<string, AvatarState> = {
  idle: 'idle',
  blink: 'idle',
  talk: 'talk',
  drag: 'drag',
  sleep: 'sleep',
  // 趴着探头：别的应用全屏时的姿势，由主进程的全屏检测驱动
  peek: 'peek',
  happy: 'happy',
  curious: 'curious',
  // 素材里额外提供的语义状态，映射到最接近的通用状态
  shy: 'happy',
  angry: 'happy',
  turn: 'curious',
  work: 'talk',
  workDone: 'happy',
  workTired: 'sleep',
};

export interface VideoPlayerOptions {
  canvas: HTMLCanvasElement;
  basePath?: string;
  width: number;
  height: number;
}

export class VideoPlayer {
  private readonly canvas: HTMLCanvasElement;
  private readonly ctx: CanvasRenderingContext2D;
  /** 逻辑尺寸。可改 —— 见 setSize()（"可调整宠物大小"用的） */
  private width: number;
  private height: number;
  private readonly basePath: string;

  private manifest: VideoManifest | null = null;
  private videos = new Map<string, HTMLVideoElement>();
  private ready = false;

  /**
   * 第一帧**抠好并画到可见画布**之后回调一次。
   *
   * 用户要求：「干脆等立绘完全加载好再显示好了」。
   *
   * 为什么不能只靠 `readyState >= 2`（haveCurrentData）：
   * 那个状态下"当前帧"其实还没真正解出来 —— 编排到可见画布上可能是
   * 一块还没抠的原始画面（用户看到的就是这个："还是会有一些"）。
   * 所以这里以**真的画出一帧抠好的内容**为准，由 `drawKeyed()` 末尾触发。
   *
   * 只触发一次（`firstFrameNotified`），后续换片段不会反复触发门闸逻辑。
   */
  onFirstFrame: (() => void) | null = null;
  private firstFrameNotified = false;

  private state: AvatarState = 'idle';
  /** 当前正在播的文件名（用于 noMirror 判定与调试） */
  private currentFile = '';
  private currentVideo: HTMLVideoElement | null = null;
  private looping = true;

  private rafId = 0;
  private tickCount = 0;
  private lastTimestamp = 0;

  /** 抠图用的离屏画布：一次读像素，之后按帧率复用 */
  private work: HTMLCanvasElement | null = null;
  private workCtx: CanvasRenderingContext2D | null = null;
  private keyThreshold = 25;

  /**
   * 上一次抠图时的视频时间点。
   *
   * ⚠️ 存在的理由是**省掉重复劳动**：实测渲染循环跑在 143fps，而源视频只有 24fps
   * —— 也就是说同一个视频帧会被重复抠 6 遍。抠一次约 0.7ms（实测），
   * 142 次/秒的重复抠图纯属浪费 CPU 与电量。
   *
   * 用 `currentTime` 判断"视频进的这一帧是不是新的"：变了才重抠，
   * 没变就直接复用上次抠好的画布重绘（重绘是一次 `drawImage`，很便宜）。
   */
  private lastKeyedTime = -1;

  /** 裁切诊断已打印次数（只打前几帧，避免每帧刷屏） */
  private cropDiagCount = 0;

  /**
   * 立绘旋转角度（度）。
   *
   * "边缘探头"就是靠这个做的：把**站姿立绘整体转 45°**，
   * 看起来像探出身子往屏幕里看。
   *
   * 依据：参考那个桌宠应用的设置里写着
   * 「拖到屏幕左/右边缘后自动以 45° 探头姿态窥视；点击真实角色会拉直，
   * 约 5 秒后自动退回」—— 而素材里**没有 45° 的动画帧**，
   * 所以只能是程序旋转。
   *
   * ⚠️ 素材里确实有一段"趴着"的动画（藏在 `move/左转奔跑` 里，我用
   * tools/analyse-pose.py 找出来的），但用户看过觉得不好看，改用"站姿 + 旋转"。
   */
  private rotationDeg = 0;
  /** 承载 `<video>` 的隐藏容器（必须挂进 DOM，见 loadClip 的说明） */
  private readonly sink: HTMLElement;

  private mouthOpen = 0;
  private mood = 0.3;
  private energy = 0.7;

  /**
   * 立绘内容占画布的比例（默认 1 = 铺满画布）。
   *
   * ⚠️ 为什么需要这个旋钮：绘制逻辑原来永远是"裁剪区铺满画布"
   * （`scale = min(width/crop.w, height/crop.h)`），于是**画布尺寸 ≠ 立绘尺寸** ——
   * 画布 240×240 里立绘只占 155×177，还偏在底部。
   *
   * 而用户要求「窗口呈现长方形状贴合宠物宽高就好了」：窗口要贴合立绘，
   * 就必须让**画布 = 立绘**。做法是把内容按这个比例缩进去、居中：
   *   内容尺寸 = 画布 × contentScale
   * 主进程算出立绘的宽高比后传进来（立绘宽/画布宽、立绘高/画布高）。
   *
   * 设为 1 时行为与原来完全一致（向后兼容，老素材不受影响）。
   */
  private contentScaleX = 1;
  private contentScaleY = 1;

  /** 设置内容占比（0~1）。传 1,1 恢复"铺满画布"。 */
  setContentScale(x: number, y: number): void {
    this.contentScaleX = Number.isFinite(x) && x > 0 ? Math.min(1, x) : 1;
    this.contentScaleY = Number.isFinite(y) && y > 0 ? Math.min(1, y) : 1;
  }

  constructor(options: VideoPlayerOptions) {
    this.canvas = options.canvas;
    this.width = options.width;
    this.height = options.height;
    this.basePath = options.basePath ?? 'assets/avatar/default';

    const ctx = this.canvas.getContext('2d');
    if (!ctx) throw new Error('无法获取 2D 绘图上下文');
    this.ctx = ctx;
    this.resize();

    // 隐藏的视频容器。放在 body 末尾，1px、透明、不接收指针事件 ——
    // 只为满足"视频必须挂进 DOM"这个条件（否则会被省电策略暂停）。
    this.sink = document.createElement('div');
    this.sink.setAttribute('aria-hidden', 'true');
    this.sink.style.cssText =
      'position:fixed;left:-9999px;top:0;width:1px;height:1px;overflow:hidden;opacity:0;pointer-events:none;';
    document.body.appendChild(this.sink);
  }

  /**
   * 改立绘尺寸（"可调整宠物大小"）。
   *
   * 画布与 CSS 尺寸一起改，并重设变换矩阵（高分屏下按 dpr 放大）。
   * 裁剪与缩放是每帧按新尺寸算的，所以不需要重建视频或清单。
   */
  setSize(width: number, height: number): void {
    const w = Math.max(48, Math.round(width));
    const h = Math.max(48, Math.round(height));
    if (w === this.width && h === this.height) return;
    this.width = w;
    this.height = h;
    this.resize();
  }

  /**
   * 逻辑尺寸与实际像素分开：按**设备像素密度**放大，避免发虚。
   *
   * ## ⚠️ 试过 2x 超采样，**实测无效，已回退**（别再试）
   *
   * 想法是"让抠图与缩放发生在更高栅格上，最后交给 GPU 缩到屏幕"。
   * 但实测（统一归到屏幕上 300x300 物理像素再比锐度）：
   *
   *     源直出（300 -> 300，不缩放）            4948
   *     旧：300 -> 240 逻辑 (dpr1.25 => 300 物理)  2702
   *     新：300 -> 240 逻辑 @2x (480 物理) -> 300   2627   ← 反而略差
   *
   * 原因：多一次重采样就多一次损失，**提高中间栅格并不能补回
   * "源被缩小过一次"这个事实**。真正的瓶颈见 `ART_FIT_BOX` 的注释：
   * 角色是**竖向**的，塞进正方形画布必然要缩。
   */
  private static readonly SUPERSAMPLE = 1;

  private resize(): void {
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const scale = Math.max(dpr, VideoPlayer.SUPERSAMPLE);
    this.canvas.width = Math.round(this.width * scale);
    this.canvas.height = Math.round(this.height * scale);
    this.canvas.style.width = `${this.width}px`;
    this.canvas.style.height = `${this.height}px`;
    this.ctx.setTransform(scale, 0, 0, scale, 0, 0);
    /*
     * 画布的**图像缩放质量**指定为高质量。
     *
     * `imageSmoothingQuality: 'high'` 让 `drawImage` 用更好的重采样核
     * （Chromium 下是多抽头滤波，而非默认双线性）。我们这里主要是**下采样**
     * （300 源 -> 240 逻辑），下采样最怕走样，高质量核能少丢细节。
     */
    this.ctx.imageSmoothingEnabled = true;
    this.ctx.imageSmoothingQuality = 'high';
  }

  /** 加载清单与首段视频。失败不抛错，交给上层退化。 */
  async load(): Promise<'video'> {
    const response = await fetch(`${this.basePath}/avatar.json`, { cache: 'no-cache' });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const manifest = (await response.json()) as VideoManifest;
    if (!manifest.animations || Object.keys(manifest.animations).length === 0) {
      throw new Error('清单缺少 animations');
    }
    this.manifest = manifest;
    this.keyThreshold = Number(manifest.blackKeyThreshold ?? 25);

    // 先把 idle 准备好，保证首帧就有画面
    const first = this.pickFile('idle');
    if (first) await this.loadClip(first);
    this.play('idle', true);
    this.ready = true;
    return 'video';
  }

  get mode(): 'video' {
    return 'video';
  }

  get currentState(): AvatarState {
    return this.state;
  }

  /** 当前帧序号（视频没有"帧号"概念，用已推进的 tick 代替，供测试断言"在动"） */
  get frame(): number {
    return Math.round((this.currentVideo?.currentTime ?? 0) * 24);
  }

  get debug(): {
    state: AvatarState;
    frame: number;
    mode: string;
    mouth: number;
    mood: number;
    energy: number;
    tick: number;
    clip: string;
    /** 立绘旋转角度（度）—— "边缘探头"时是 45 */
    rotation: number;
  } {
    return {
      state: this.state,
      frame: this.frame,
      mode: this.mode,
      mouth: this.mouthOpen,
      mood: this.mood,
      energy: this.energy,
      tick: this.tickCount,
      clip: this.currentFile,
      rotation: this.rotationDeg,
    };
  }

  // ------------------------------------------------------------------ 播放控制

  play(state: AvatarState, restart = false): void {
    const normalized = STATE_ALIASES[state] ?? state;
    if (normalized === this.state && !restart) return;
    this.state = normalized;

    const loop = LOOPING_STATES.has(normalized);
    // restart 为真时**换一个**片源（同类里随机），这样"再点一次"会换个反应，
    // 不会每次都播同一段。
    const file = this.pickFile(normalized, restart);
    if (!file) {
      // 该状态没有素材 —— 退回 idle，绝不静默什么都不画（那会让宠物凭空消失）
      console.info(`[avatar] 素材里没有 ${state} 的片段，回退到 idle`);
      this.state = 'idle';
      const fallback = this.pickFile('idle');
      if (fallback) void this.switchTo(fallback, true);
      return;
    }
    void this.switchTo(file, loop);
  }

  setMouth(open: number): void {
    this.mouthOpen = Math.max(0, Math.min(1, open));
  }

  /** 设置立绘旋转角度（度）。0 = 不旋转。用于"边缘探头"的 45° 姿态。 */
  setRotation(deg: number): void {
    const next = Number.isFinite(deg) ? deg : 0;
    if (next === this.rotationDeg) return;
    this.rotationDeg = next;
  }

  get rotation(): number {
    return this.rotationDeg;
  }

  setEmotion(mood: number, energy: number): void {
    this.mood = mood;
    this.energy = energy;
  }

  start(): void {
    if (this.rafId) return;
    const tick = (timestamp: number) => {
      this.rafId = requestAnimationFrame(tick);
      this.update(timestamp);
    };
    this.rafId = requestAnimationFrame(tick);
  }

  stop(): void {
    if (this.rafId) cancelAnimationFrame(this.rafId);
    this.rafId = 0;
    for (const video of this.videos.values()) video.pause();
  }

  // ------------------------------------------------------------------ 内部

  /** 从清单里取该状态的一个文件名；`reshuffle` 时尽量换一个 */
  private pickFile(state: AvatarState, reshuffle = false): string | null {
    const anims = this.manifest?.animations;
    if (!anims) return null;

    // 状态名可能是别名（shy/angry → happy 等），要把所有同义键的素材合并
    const keys = Object.keys(anims).filter((k) => (STATE_ALIASES[k] ?? k) === state);
    const pool: string[] = [];
    for (const key of keys) {
      const value = anims[key];
      if (Array.isArray(value)) pool.push(...value);
      else if (value && Array.isArray(value.files)) pool.push(...value.files);
    }
    if (pool.length === 0) return null;

    if (pool.length === 1) return pool[0];
    if (reshuffle && this.currentFile) {
      const others = pool.filter((f) => f !== this.currentFile);
      if (others.length) return others[Math.floor(Math.random() * others.length)];
    }
    return pool[Math.floor(Math.random() * pool.length)];
  }

  /**
   * 文件名 -> 可加载的 URL。
   *
   * 清单里存的就是**完整相对路径**（如 `idle/待机呼吸休闲`），这里只拼 basePath。
   * 为什么不让清单只写短名、由代码去猜目录：浏览器里**无法枚举目录**，
   * 猜就只能逐个试错加载（慢，而且会刷一屏 404 报错）。
   */
  private filePath(relative: string): string {
    return `${this.basePath}/${relative}.webm`;
  }

  private async switchTo(file: string, loop: boolean): Promise<void> {
    this.looping = loop;
    const video = await this.loadClip(file);
    if (!video) return;
    this.currentFile = file;
    this.currentVideo = video;
    video.loop = loop;
    video.currentTime = 0;
    try {
      await video.play();
    } catch (error) {
      // 自动播放被拒（理论上 muted 不会）—— 记下来，画面会停在首帧而不是崩
      console.info(`[avatar] 视频播放被拒：${(error as Error).message}`);
    }
  }

  private async loadClip(file: string): Promise<HTMLVideoElement | null> {
    const existing = this.videos.get(file);
    if (existing && existing.readyState >= 2) return existing;

    const video = existing ?? document.createElement('video');
    if (!existing) {
      video.src = this.filePath(file);
      video.muted = true;
      video.playsInline = true;
      video.loop = false;
      // ⚠️ **必须挂进 DOM**（哪怕看不见）。
      //
      // 实测踩过：只创建不插入 DOM 时，Chromium 会把它当作
      // "video-only background media" 并**暂停以省电**：
      //   The play() request was interrupted because video-only background media
      //   was paused to save power.
      // 后果是视频永远不播、抠图拿到的是空画布 —— 立绘**静默地什么都不显示**，
      // 而且只在控制台留一条线索。这是最难查的一类失败。
      //
      // 挂在容器里、用 opacity:0 + 1px 尺寸：既满足"在 DOM 中"，又不影响布局。
      video.style.position = 'absolute';
      video.style.left = '-9999px';
      video.style.top = '0';
      video.style.width = '1px';
      video.style.height = '1px';
      video.style.opacity = '0';
      video.style.pointerEvents = 'none';
      this.sink.appendChild(video);
      this.videos.set(file, video);
    }
    if (video.readyState >= 2) return video;

    try {
      await new Promise<void>((resolve, reject) => {
        const onReady = () => {
          cleanup();
          resolve();
        };
        const onError = () => {
          cleanup();
          reject(new Error(`无法加载 ${video.src}`));
        };
        const cleanup = () => {
          video.removeEventListener('loadeddata', onReady);
          video.removeEventListener('error', onError);
        };
        video.addEventListener('loadeddata', onReady);
        video.addEventListener('error', onError);
      });
      return video;
    } catch (error) {
      console.info(`[avatar] ${(error as Error).message}`);
      return null;
    }
  }

  private update(timestamp: number): void {
    const delta = this.lastTimestamp ? timestamp - this.lastTimestamp : 0;
    this.lastTimestamp = timestamp;
    if (delta > 0) this.tickCount += 1;

    const video = this.currentVideo;
    if (!video) {
      this.ctx.clearRect(0, 0, this.width, this.height);
      return;
    }

    // 一次性动画播完 -> 回 idle。
    // 必须在**这里**做，而不是依赖 ended 事件：视频被暂停/切换时 ended 不一定触发。
    if (!this.looping && video.duration > 0 && video.currentTime >= video.duration - 0.03) {
      const back = this.pickFile('idle');
      if (back) void this.switchTo(back, true);
      return;
    }

    this.drawKeyed(video);
  }

  /**
   * 逐帧抠黑底。
   *
   * ---- 为什么不能用"单像素阈值"----
   *
   * 最初我用的判据是"每通道 ≤ 阈值就透明"。结果**睫毛、眼线、描边全被抠掉**
   * （用户报的"黑色的眼睫毛是透明的"）。
   *
   * 实测头部区域的像素分布说明了原因：
   *   纯黑 (0,0,0)            41.5%   ← 背景
   *   暗(lum<25) 且**有色**     382 px ← 睫毛/眼线，被误杀
   *   暗(lum<40) 且**有色**    1025 px
   * 单看一个像素，没法区分"背景的黑"和"角色的黑"。
   *
   * ---- 改用连通性 ----
   *
   * 两者有个结构差异：**背景的黑连到画面边缘，睫毛的黑被有色像素围住**。
   * 所以从画面四边洪泛，只穿过"近黑**且**无色"的像素：
   *   · 洪泛到不了的暗像素 = 角色的一部分（睫毛、眼线、描边）→ 保留
   *   · 洪泛到的 = 背景 → 透明
   *
   * 描边把角色的暗部挡住，洪泛进不去，所以不需要预先知道角色在哪。
   */
  private keyFrame(workCtx: CanvasRenderingContext2D, vw: number, vh: number): void {
    const frame = workCtx.getImageData(0, 0, vw, vh);
    const data = frame.data;
    const neck = this.keyThreshold;

    // 1) 标出"背景候选"：**近黑且无色**的像素才有可能是背景。
    //    有颜色的暗像素（睫毛、眼线、描边）一律不算候选 —— 这正是修掉
    //    "睫毛变透明"的关键：它们不会参与洪泛，因而一定会被保留。
    const isBg = new Uint8Array(vw * vh);
    for (let i = 0, p = 0; i < data.length; i += 4, p += 1) {
      const r = data[i];
      const g = data[i + 1];
      const b = data[i + 2];
      const dark = r <= neck && g <= neck && b <= neck;
      const sat = Math.max(r, g, b) - Math.min(r, g, b);
      // 饱和度高 = 有颜色 = 属于角色（肤色、头发、衣服、睫毛都算）
      isBg[p] = dark && sat <= 12 ? 1 : 0;
    }

    // 2) 从四边洪泛，只走 isBg 的像素
    const reachable = new Uint8Array(vw * vh);
    const stack: number[] = [];
    const push = (x: number, y: number): void => {
      const idx = y * vw + x;
      if (!reachable[idx] && isBg[idx]) {
        reachable[idx] = 1;
        stack.push(idx);
      }
    };
    for (let x = 0; x < vw; x += 1) {
      push(x, 0);
      push(x, vh - 1);
    }
    for (let y = 0; y < vh; y += 1) {
      push(0, y);
      push(vw - 1, y);
    }
    while (stack.length) {
      const idx = stack.pop() as number;
      const y = (idx / vw) | 0;
      const x = idx - y * vw;
      if (x > 0) push(x - 1, y);
      if (x < vw - 1) push(x + 1, y);
      if (y > 0) push(x, y - 1);
      if (y < vh - 1) push(x, y + 1);
    }

    // 3) 只有洪泛到的才算背景，置为透明
    for (let i = 0, p = 0; i < data.length; i += 4, p += 1) {
      if (reachable[p]) data[i + 3] = 0;
    }
    workCtx.putImageData(frame, 0, 0);
  }
  /** 把视频当前帧抠掉黑底、缩放居中画到显示画布 */
  private drawKeyed(video: HTMLVideoElement): void {
    const vw = video.videoWidth;
    const vh = video.videoHeight;
    if (!vw || !vh) return;

    if (!this.work || this.work.width !== vw || this.work.height !== vh) {
      this.work = document.createElement('canvas');
      this.work.width = vw;
      this.work.height = vh;
      const ctx = this.work.getContext('2d', { willReadFrequently: true });
      if (!ctx) return;
      this.workCtx = ctx;
    }
    const workCtx = this.workCtx;
    if (!workCtx) return;

    // 只有视频真的进了新的一帧才重新抠图（见 lastKeyedTime 的说明）。
    // 时间戳用 currentTime：它随播放推进，暂停/循环时会回到 0 —— 两种变化
    // 都意味着"画面内容变了"，正是需要重抠的时机。
    const stamp = video.currentTime;
    if (stamp !== this.lastKeyedTime) {
      workCtx.clearRect(0, 0, vw, vh);
      workCtx.drawImage(video, 0, 0, vw, vh);
      this.keyFrame(workCtx, vw, vh);
      this.lastKeyedTime = stamp;
    }

    // 缩放与定位：**按角色范围裁剪，而不是整帧适配**。
    //
    // 为什么必须裁：这套视频是 640×360，而角色只占其中一小块
    // （素材自带 body_box = [212,60,428,330]，宽只占 34%）。
    // 按整帧适配缩放时角色只画出 81×101 像素（画布 240×240）—— 小得看不清。
    // 按角色范围裁剪后能画到 192×240，基本占满。
    const crop = this.cropRect(vw, vh);
    // 内容按"裁剪区铺满画布"缩放（老行为，也是对的）：
    // 画布 240 里立绘占满高度，横向按裁剪区比例。
    //
    // ⚠️ 别再往这里加"内容占比"旋钮。我试过让内容缩进画布（`contentScaleX/Y`）
    // 以便把画布做成"恰好等于立绘"，但那样**窗口和画布都要跟着改**，
    // 而用户的解法是"窗口尺寸不动，只放宽拖拽极限"（见 petLayout.windowSizeFor
    // 的说明）。立绘在画布里的偏移改由**画布的 CSS 平移**处理（见 app.js 的
    // pet:set-pet-size 与 styles.css 的 `--art-shift-x/y`）。
    const scale = Math.min(this.width / crop.w, this.height / crop.h);
    const dw = crop.w * scale;
    const dh = crop.h * scale;
    // 横向居中、纵向贴底 —— 这是老布局的定位，配合"画布平移"一起把立绘
    // 摆到窗口正中。两者不要同时改，否则会互相打架（踩过）。
    const dx = (this.width - dw) / 2;
    const dy = this.height - dh;
    /*
     * 诊断：把"视频固有尺寸 / 裁切区 / 缩放 / 最终画多大"打出来（只打前几帧）。
     *
     * ⚠️ **必须放在 `dx/dy` 声明之后**。我第一版把它插在 `const dx` 之前，
     * 于是每帧抛 `ReferenceError: Cannot access 'dx' before initialization`
     * （TDZ）—— 渲染层整个报错，立绘画不出来，实测尺寸自然也不对。
     * 那次是主进程的 console 转发修好之后才从日志里看到这个报错的（见 petWindow
     * 的 `console-message` 注释）。
     */
    if (this.cropDiagCount < 4) {
      this.cropDiagCount += 1;
      // 用 `console.warn`：主进程只把 warn 及以上收进 startup.log
      console.warn(
        `[crop] 视频固有 ${video.videoWidth}x${video.videoHeight} | work ${vw}x${vh}` +
          ` | 裁切 ${crop.x},${crop.y} ${crop.w}x${crop.h}` +
          ` | 画布 ${this.width}x${this.height} | 缩放 ${scale.toFixed(3)}` +
          ` | 画到 ${Math.round(dw)}x${Math.round(dh)} @ ${Math.round(dx)},${Math.round(dy)}`,
      );
    }

    this.ctx.clearRect(0, 0, this.width, this.height);
    // 不管转不转，绘制方式都一样 —— 旋转由渲染层给画布加 CSS transform 做。
    // 这里不再需要分支，留个注释说明为什么（免得以后有人以为漏了旋转）。
    this.ctx.drawImage(this.work, crop.x, crop.y, crop.w, crop.h, dx, dy, dw, dh);

    // 第一帧**真的画到可见画布**了 -> 通知门闸放行（用户要求"等完全加载好再显示"）。
    //
    // 放在 drawImage **之后**是有意的：这一行跑完，可见画布上才是抠好的内容。
    // 放在前面的话，门闸会在"画布还是空/还是旧内容"的时候打开。
    if (!this.firstFrameNotified && this.ready) {
      this.firstFrameNotified = true;
      this.onFirstFrame?.();
    }
  }

  /**
   * 立绘旋转角度（度）。
   *
   * ⚠️ **现在只记录、不再用于绘制**。旋转改由渲染层给 `<canvas>` 加
   * CSS `transform: rotate(...)`（见 app.js 的 `pet:peek` 处理 + styles.css 的
   * `body.peek`）—— 用户的要求是「就用现在宠物待机的大小将画布旋转就好了」：
   * 整块画布一起转，内容**不重画、不缩小、不裁切**。
   *
   * 为什么不用 canvas 自己转：那样得算旋转后的外接矩形，为了"装进画布"
   * 必须缩一次，宠物就变小了；不缩又会被画布裁掉。CSS 旋转没有这个矛盾。
   *
   * 这个 getter 仍然保留，供调试与测试断言"当前姿态角度"。
   */
  get rotation(): number {
    return this.rotationDeg;
  }

  /**
   * 计算角色在源帧里的裁剪框 —— **用实测的角色外接框，不用 `hitboxes.body`**。
   *
   * ## 为什么改成固定框（用户：「我们用的是他们的素材，可是比他们小还没他们高清」）
   *
   * 素材**逐字节相同**（`待机呼吸休闲.webm` MD5 `0CE28219…` 两边一致，640x360 VP9），
   * 所以"小 + 糊"只能是显示方式造成的。病灶就在旧实现：
   *
   *     hitboxes.body = [212,60,428,330]   bw=216 bh=270
   *     padX = 75.6, padTop = 121.5, padBottom = 32.4
   *     y = 60 - 121.5 = -61.5   -> 钳到 0
   *     h = 270 + 153.9 = 424    -> 钳到 vh=360
   *     => 裁切区 367x360 ≈ **整帧 640x360**
   *
   * 整帧缩进 240 画布，而角色只占画面中间 51% 宽，于是被一起缩没了：
   *
   *     缩放 240/640 = 0.375  =>  立绘屏上只有 240 x 0.512 ≈ 123 逻辑像素
   *     （HUD 实测 `artBox 135x172`，印证）
   *
   * ⚠️ 关键误解：`hitboxes.body` **不是角色外接框**，它只是"能点到身体的区域"
   * （躯干）。拿它当裁切基准 + 大留白，必然胀到整帧。
   *
   * ## 实测的真实范围（`tools/_measure-char-bounds.py`，8 个状态抽帧取并集）
   *
   *     [136, 36, 463, 341]   宽高 328x306   （占源 51.2% x 85.0%）
   *
   * ## 效果
   *
   * 裁切区 `367x360` -> `ART_FIT_BOX`（347x314），立绘放大约 **1.85 倍**；
   * 同时源 640 宽在屏上只占约 360 —— 是**下采样**而非放大，
   * 所以**既变大又变清楚**（不需要超分）。
   *
   * ## 为什么用固定框而不是逐条自适应
   *
   * 逐条贴着角色裁能让每条都填满画布，但不同动作的包围盒差很多
   * （实测 idle 244 宽 vs move 328 宽），逐条适配会让立绘在动作之间**大小跳变**。
   * 固定框保证所有动作比例一致 —— 观感更重要。
   *
   * ⚠️ 换素材后要重跑 `tools/_measure-char-bounds.py` 更新 `ART_FIT_BOX`。
   */
  private cropRect(vw: number, vh: number): { x: number; y: number; w: number; h: number } {
    const [bx0, by0, bx1, by1] = ART_FIT_BOX;
    const x = Math.max(0, Math.min(Math.max(0, vw - 1), Math.floor(bx0)));
    const y = Math.max(0, Math.min(Math.max(0, vh - 1), Math.floor(by0)));
    const w = Math.max(1, Math.min(vw - x, Math.ceil(bx1 - bx0)));
    const h = Math.max(1, Math.min(vh - y, Math.ceil(by1 - by0)));
    return { x, y, w, h };
  }
}
