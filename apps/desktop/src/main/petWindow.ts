/**
 * 主窗口：透明、无边框、置顶的桌面 HUD。
 *
 * 关键设计（这些细节决定了它是不是"桌面宠物"而不是"一个窗口"）：
 *   · transparent + frameless + alwaysOnTop（默认 'normal' 层级：浮在普通窗口之上，
 *     但**不**盖住任务栏 —— 盖住任务栏会让用户点不到开始菜单与托盘）；
 *   · **点击穿透**：平时鼠标事件透传给下面的应用，只有落在自己的交互区
 *     （输入框、宠物本体）才拦截。实现方式是 setIgnoreMouseEvents(true, {forward:true})
 *     —— forward 让渲染进程仍能收到 mousemove，从而判断指针是否进入交互区。
 *     不开 forward 就再也收不到事件了，那样宠物会永远收不到点击。
 *   · skipTaskbar：宠物不该占任务栏；
 *   · 暂停时窗口整体可交互，方便用户拖拽和操作设置面板。
 */

import { BrowserWindow, screen, ipcMain } from 'electron';
import { DragTracker } from './dragTracker.js';
import { rendererUrl, resolvePreload, resolveRendererPage } from './assets.js';
import {
  anchorAtEdge,
  anchorBottomRight,
  clampScale,
  petCenterInWindow,
  peekShiftFor,
  PeekSide,
  clampPeekOffset,
  clampEdgeSnap,
  peekCanvasSizeFor,
  peekWindowSizeFor,
  /* 探头窗口定位（纯函数）—— 一份实现，测试与运行时共用 */
  peekWindowBounds,
  /* 换尺寸时保持立绘不动（纯函数）—— 同上 */
  windowBoundsKeepingArt,
  PEEK_OFFSET_DEFAULT_PX,
  EDGE_SNAP_DEFAULT_PX,
  BUBBLE_LINGER_DEFAULT_MS,
  petSizeFor,
  artSizeFor,
  artShiftFor,
  canvasBoxFor,
  peekFitScaleFor,
  sideAtEdge,
  windowSizeFor,
  PEEK_ANGLE_DEG,
  type PetForm,
} from './petLayout.js';

/**
 * 初始窗口尺寸。
 *
 * 用 `windowSizeFor` 算出来，而不是写死一份 —— 写死的话，
 * 改了立绘基准尺寸或卡片高度之后，**启动时的窗口和之后"改大小"算出来的
 * 窗口会不一致**，表现为一启动就有一片多余的空白。
 */
const INITIAL_SIZE = windowSizeFor(1, 'normal');
const HUD_WIDTH = INITIAL_SIZE.width;
const HUD_HEIGHT = INITIAL_SIZE.height;
const MARGIN = 16;

/** 贴边方向的显示名（诊断日志用） */
const PEEK_SIDE_TEXT: Record<PeekSide, string> = {
  left: '左',
  right: '右',
  top: '上',
  bottom: '下',
};
export class PetWindow {
  private window: BrowserWindow | null = null;
  private clickThrough = true;
  private interactive = false;
  /** 拖拽坐标计算（纯模块，见 dragTracker.ts）；真实窗口位置通过回调接进去 */
  private readonly dragTracker = new DragTracker(
    () => {
      const window = this.window;
      if (!window || window.isDestroyed()) return { x: 0, y: 0, width: 0, height: 0 };
      return window.getBounds();
    },
    (bounds) => {
      const window = this.window;
      if (!window || window.isDestroyed()) return;
      // 用 setBounds 而不是 setPosition：见 DragTracker 的注释 ——
      // 反复 setPosition 在 1.25 DPI 下会让窗口每次涨约 1px（"越拖越大"）。
      window.setBounds(bounds);
    },
    // 拖拽期间夹紧到**指针所在那块屏幕**的工作区，触碰边缘就不再增减。
    //
    // 用指针位置而不是窗口位置来选屏幕：窗口被夹住之后停在边缘，
    // 而指针可能已经继续移动到另一块屏幕上了，那时应该按新屏幕来夹。
    (pointerX, pointerY) => screen.getDisplayNearestPoint({ x: pointerX, y: pointerY }).workArea,
    // ⚠️ 拖拽极限**放宽"窗口里那圈留白"** —— 这是让立绘真正贴到屏幕边的关键。
    //
    // 用户原话：「把随鼠标拖拽增减的极限增加窗口的留白就行了」。
    //
    // 窗口比立绘大一圈（立绘居中、四周透明），若把窗口死死夹在屏幕内，
    // 立绘就永远差那圈留白到不了边 —— 于是"拖到边缘自动探头"要么触发不了、
    // 要么靠调大阈值糊过去，反而变成"还没到边就探头"。
    // 放宽这么多之后，窗口可以一直走到"立绘边上贴住屏幕边"。
    // 用回调：值随 petScale 变化，而且能避开字段初始化顺序问题
    // 用**实测**立绘尺寸算要放宽多少（缩放到多大都自适应）。
    // 横纵分开传：立绘不是正方形，两个方向的留白不一样。
    () => this.currentArtMarginX(),
    () => this.currentArtMarginY(),
    // 屏幕底（不是 workArea 底）：让窗口能压住任务栏。
    // 用户报的「宠物突破不了下面」「下面有东西挡着」就是这个 ——
    // workArea 排除了任务栏（实测 60px），只按它夹，立绘底边永远停在任务栏上沿。
    (px, py) => {
      const d = screen.getDisplayNearestPoint({ x: px, y: py });
      return d.bounds.y + d.bounds.height;
    },
    /*
     * 屏幕顶边 + 立绘在窗口内的 top（上留白）。
     *
     * 用户要求「宠物能超出屏幕也改一下」—— 截图里立绘上半身被屏幕顶切掉了。
     * 让立绘顶边能走到屏幕顶，用**上留白**（约 32px），
     * **不能**复用 marginY（那是下留白，约 113px，两者不相等）。
     */
    (px, py) => screen.getDisplayNearestPoint({ x: px, y: py }).bounds.y,
    () => this.currentArtMarginTop(),
    /**
     * **右留白**（立绘右边 → 窗口右边）—— 与左留白分开传（2026-10-04 实测）。
     *
     * 原来 `clampToWorkArea` 左右共用 `marginX`（= 左留白）。立绘在画布里不是
     * 左右对称的（左 33 / 右 42），于是**右边缘的拖拽极限差 9px**：
     * 用户报「宠物在边缘时不时被挡住」，左边正常、右边差一截。
     */
    () => this.currentArtMarginRight(),
    /**
     * 立绘**可见宽度** —— 拖拽极限要按"立绘的边"算，见 `clampToWorkArea` 的 `maxX`。
     *
     * 拿不到实测值就返回 undefined，让那边退回旧公式。
     */
    () => (this.measuredArt.width > 0 ? this.measuredArt.width : artSizeFor(this.petScale).width),
  );
  /** 置顶层级；见 create() 里的说明。恢复置顶时要用同一层级。 */
  private topLevel: 'normal' | 'screen-saver' = 'normal';
  /** 当前宠物缩放（菜单里的「大小」）。1 = 基准尺寸 */
  private petScale = 1;
  /**
   * 紧凑模式（"边缘探头"）。
   *
   * 检测到**别的应用全屏**时进入：只留宠物本体，隐藏输入卡片，
   * 并把窗口大半移出屏幕边缘，只露一条 —— 看起来像宠物从屏幕边上探出身子。
   *
   * 为什么必须靠"移出屏幕"实现：这套素材**没有趴着/躺着的姿势**
   * （抽过"原地小憩沉眠"等多段核对，全是站姿）。详见 anchorPeekRight。
   */
  private compact = false;
  /** 探头方向：宠物靠哪边就从哪边探 */
  private peekSide: 'right' | 'left' = 'right';

  /**
   * 进入紧凑模式（别人全屏）**之前**窗口在哪、以及当时是不是探头姿态。
   *
   * 用户要求（原话）：「取消全屏时要能变成之前的位置和状态」。
   * 不记的话退出全屏就只会回到默认的右下角，用户之前把宠物拖到哪儿就丢了。
   */
  private preCompact: { x: number; y: number; peeking: boolean; peekSide: PeekSide } | null = null;

  // ---- 边缘探头（手动拖到屏幕边缘触发）----

  /**
   * "边缘探头"是否启用（设置里的开关）。
   *
   * 参考应用把它做成可关闭的开关，理由很实际：有人受不了宠物变换姿态。
   */
  private edgePeekEnabled = true;
  /** 当前是否处于边缘探头姿态 */
  private edgePeek = false;
  /** 探头态下当前贴的是哪一边 */
  private edgePeekSide: PeekSide = 'right';
  /**
   * 贴边时窗口伸出屏幕的**像素数**（设置窗口里可调）。
   *
   * 用户要求："做一个参数，放在菜单里面，可以打开设置窗口，我自己调节斜着出去多少px"。
   * 用像素而不是比例：比例会随窗口大小变（改宠物大小后同一个"0.34"看起来不一样），
   * 而用户想调的是"出去多少像素"这个直接的感觉。
   */
  private peekOffsetPx = PEEK_OFFSET_DEFAULT_PX;
  /**
   * 拖拽结束时，离屏幕边缘多少像素算"贴边"（用户可调）。
   *
   * 用户要求："我好自己设置靠近屏幕边缘多少像素自动侧身"。
   */
  private edgeSnapPx = EDGE_SNAP_DEFAULT_PX;

  setEdgeSnap(px: number): void {
    this.edgeSnapPx = clampEdgeSnap(px);
  }

  get edgeSnap(): number {
    return this.edgeSnapPx;
  }

  setPeekOffset(px: number): void {
    this.peekOffsetPx = clampPeekOffset(px);
    // 正在探头就立刻重排，让用户边拖滑杆边看到效果
    if (this.edgePeek) this.repositionEdgePeek();
  }

  get peekOffset(): number {
    return this.peekOffsetPx;
  }

  /**
   * 重排探头窗口（用户调"贴边内缩"滑杆时实时生效）。
   *
   * 语义与 `enterEdgePeek` 一致：只改变**离边缘的内缩距离**与旋转后的窗口尺寸，
   * 窗口始终完整留在屏幕内。位置是**算出来的**而不是按位移量累加 ——
   * 累加会随滑杆来回拖而漂移（没有参照物可复位）。
   */
  private repositionEdgePeek(): void {
    const window = this.window;
    if (!window || window.isDestroyed()) return;
    if (!this.edgePeek) return;
    // 直接复用 enterEdgePeek 的定位逻辑：先临时清掉标志，让它重新算一遍。
    // 这样"探头定位"只有一个实现，不会两边算出不同结果。
    const side = this.edgePeekSide;
    this.edgePeek = false;
    this.enterEdgePeek(side);
  }
  /**
   * ⚠️ **没有自动退出定时器**。
   *
   * 用户明确要求：「不要 5 秒钟自动退出，拖拽才退出」。
   * 参考应用确实有"约 5 秒后自动退回"，但那会让人正准备看的时候它自己溜走。
   * 现在只有两个出口：把它拖离边缘，或者在右键菜单里关掉这个功能。
   */

  /** 开关边缘探头。关掉时如果正在探头，立刻收回。 */
  setEdgePeekEnabled(enabled: boolean): void {
    this.edgePeekEnabled = enabled;
    if (!enabled && this.edgePeek) this.exitEdgePeek('开关关闭');
  }

  get edgePeekEnabledValue(): boolean {
    return this.edgePeekEnabled;
  }

  get isEdgePeeking(): boolean {
    return this.edgePeek;
  }

  /**
   * 进入边缘探头姿态。
   *
   * 三件事：
   *   ① 窗口压到屏幕边缘（`anchorAtEdge`）；
   *   ② 立绘旋转 45°（渲染层做，因为旋转是绘制时的事）；
   *   ③ 卡片藏起来（`body.compact`），只留宠物。
   * 然后起一个 5 秒定时器自动收回 —— 否则它会一直挂着，用户还得手动拖走。
   */
  enterEdgePeek(side: PeekSide, cursor?: { x: number; y: number }): void {
    const window = this.window;
    if (!window || window.isDestroyed()) return;
    // 全屏模式下的定位由 setCompact 直接调 applyPeekLayout，
    // 不走这里 —— 否则会把 edgePeek 置 true，让 drag-end 误以为"用户拖出了探头"。
    if (this.compact) return;
    /*
     * ## ⚠️ 守卫必须**同时**检查"窗口尺寸对不对"（2026-10-04 探针实测）
     *
     * 旧写法只看标志：
     *
     *     if (this.edgePeek && this.edgePeekSide === side) return;
     *
     * 一旦出现"标志为 true、窗口却是常态尺寸"的**中间态**，就会**永久卡死**：
     *   · 想进探头 → 被这个守卫挡住（"已经在了"）；
     *   · 想退探头 → 没触发条件（判定说它已经贴边）。
     *
     * 探针实测的日志就是这个状态：
     *
     *     drag-end 1298,320 253x336；… -> right（**当前探头=true**）
     *     即将进入探头：窗口 1298,320 253x336      <- 进来了但被守卫 return 掉
     *     （此后窗口一直是常态尺寸，再也不探头）
     *
     * 用户报的「鼠标拖拽太快了会导致位置不够标准 / 有时不探头」就是它 ——
     * 快拖时 `drag-end` 排的"恢复常态尺寸"和这次进探头撞在一起，留下了中间态。
     *
     * 现在只有"标志对**且**尺寸已经是探头尺寸"才算真的在里面；否则重新应用布局，
     * 顺手把状态修回来。
     */
    const peekSize = peekWindowSizeFor(this.petScale);
    const win = this.window;
    const atPeekSize =
      win != null && !win.isDestroyed() && Math.abs(win.getBounds().width - peekSize.width) <= 4;
    if (this.edgePeek && this.edgePeekSide === side && atPeekSize) return;

    /*
     * 诊断：**进入探头前的窗口与锚点**。
     *
     * 排查"探头时窗口往下移 80px"时最需要的就是这四个数：
     * 进之前的窗口原点、进之前算出的立绘屏幕位置、以及进之后的结果。
     * 只看结果（HUD）永远推不出是哪一步改的。
     */
    const before = window.getBounds();
    const art = this.artOffsetInWindow;
    this.diagnostic(
      `进入探头（贴${side}）：窗口前 ${before.x},${before.y} ${before.width}x${before.height}` +
        ` | 偏移 ${art ? JSON.stringify(art) : 'null'}` +
        ` | 推算立绘屏幕 ${art ? `${before.x + art.x},${before.y + art.y}` : '?'}`,
    );

    this.edgePeek = true;
    this.edgePeekSide = side;
    /*
     * ⚠️ **进入探头时必须清掉"待换回常态尺寸"的请求**（2026-10-04 探针实测）。
     *
     * 否则会出现这种"刚探进去又被撤回"的竞态：
     *
     *     即将进入探头：窗口 1328,320 252x336
     *     进入探头后：  窗口 1328,320 384x387    <- 探头正常应用
     *     旋转动画结束（渲染层通知）              <- 陈旧的 pending 在这里生效
     *     最终          窗口 1328,320 253x336    <- 被恢复成常态，但 edgePeek=true
     *
     * 触发条件：**快速拖拽**（`drag-end` 先排了一个"恢复常态尺寸"，
     * 紧接着这次拖拽又判定贴边要进探头）。用户报的正是
     * 「鼠标拖拽太快了会导致位置不够标准」—— 里面混着这个状态错乱。
     *
     * 清掉之后，`tryApplyNormalResize` 的唯一触发源就只剩"真的离开探头"。
     */
    this.pendingNormalResize = false;
    this.peekOffsetDirty = false;
    if (this.normalResizeTimer) {
      clearTimeout(this.normalResizeTimer);
      this.normalResizeTimer = null;
    }
    // 旋转动画从这里开始 —— 在渲染层发 `pet:peek-transition-end` 之前，
    // 立绘在窗口内的偏移一直在漂，**不能**拿它当换尺寸的锚点。
    this.peekRotationDone = false;
    this.applyPeekLayout(side, cursor);

    const after = window.getBounds();
    this.diagnostic(
      `进入探头后：窗口 ${after.x},${after.y} ${after.width}x${after.height}` +
        ` | 推算立绘屏幕 ${art ? `${after.x + art.x},${after.y + art.y}` : '?'}` +
        `（进之前是 ${art ? `${before.x + art.x},${before.y + art.y}` : '?'}）`,
    );
  }

  /**
   * 把窗口摆成"贴某条边的探头样子"，并把旋转/位移发给渲染层。
   *
   * 这是**唯一的探头定位实现**：手动贴边（`enterEdgePeek`）与
   * 全屏模式（`setCompact`）都调它，所以两者样子必然一致（用户要求）。
   *
   * 它**不碰** `edgePeek` 标志 —— 由调用方决定是否算"用户拖出来的探头"。
   */
  private applyPeekLayout(side: PeekSide, cursor?: { x: number; y: number }): void {
    const window = this.window;
    if (!window || window.isDestroyed()) return;

    const bounds = window.getBounds();
    const display = screen.getDisplayNearestPoint({ x: bounds.x, y: bounds.y });
    /*
     * ⚠️ 探头定位用**整屏边界**（`bounds`），**不是工作区**（`workArea`）。
     *
     * 用户的关键提示：「有没有可能是屏幕上面宠物可以超出屏幕，下面宠物被压在
     * 任务栏上导致的」。实测证实了：常态窗口故意比立绘高（给气泡留空间），
     * 那段透明区本来就会伸到任务栏下面；一旦用工作区去钉/夹，
     * 这段就会把立绘顶上去 72px —— 正是"边缘探头又变高了"。
     *
     * `workArea` 仍然给别处用（比如常态的右下角锚定），所以两个都取。
     */
    const wa = display.workArea;
    const screenBounds = display.bounds;

    /*
     * ⚠️ 探头时窗口要比常态**大一圈** —— 否则旋转后的立绘被窗口裁掉。
     *
     * 用户报了现象（视频里立绘右侧一条硬直边），根因是这里：
     * `peekWindowSizeFor` 早就按"装得下旋转后立绘"算好了尺寸，但**从来没被调用**，
     * 窗口一直用常态的 `windowSizeFor` —— 240 宽装不下需要 269 的旋转外接矩形。
     *
     * 换尺寸时**保持立绘在屏幕上的位置不动**：让**中心点**对齐，
     * 而不是让左上角对齐（后者会把宠物整体推走，用户会看到"跳一下"）。
     */
    const target = peekWindowSizeFor(this.petScale);
    // growX/growY、centerX/centerY、以及立绘锚点都已经搬进纯函数
    // `peekWindowBounds`（原来在这里内联，导致测试改了、运行时没改）。
    void 0;

    // 探头 = **原地旋转**，窗口尺寸不变。
    //
    // 用户原话：「直接把宠物设置无法超出屏幕，靠近左右边缘就自动旋转探头」，
    // 「探头时立绘不用放大，整个动画时宠物全身比例不变」，
    // 后来又补：「屏幕上下也加个」—— 所以四边都用同一套逻辑。
    //
    // 几何关系（最容易想错的一步，记下来）：
    //   · 窗口 = 正方形（边长 = 画布边长），尺寸恒定；
    //   · 立绘居中，四周留白 = (窗口 - 立绘)/2（旋转需要的几何差，消不掉）；
    //   · 要让**立绘**（不是窗口）贴到屏幕边，就得把那圈留白让开 =>
    //     窗口自身越出屏幕那么多（拖拽极限也同步放宽，见 dragTracker 的 marginX/Y）。
    // ---- 平移距离 = "让开留白" + 用户设定的额外平移 ----
    //
    // 用户要求：「第二项改成探头触发的平移距离，参数要可以保留和修改，重新打开也有用」。
    //
    // 为什么是**相加**而不是直接取 `peekOffsetPx`：
    //   · 要让**立绘**（不是窗口）贴到屏幕边，必须先把那圈留白让到屏幕外 ——
    //     那是几何上的必需量（`currentArtMarginX()`，scale 1 时约 43px）。
    //     只让一半，立绘就碰不到边，"探头"看起来像"悬浮在屏幕边上"。
    //   · 用户那一项是**在"贴到边"的基础上**再多探出去多少（调大 = 探得更外，
    //     调小 = 收敛回边上）。
    //   · 所以 0 = 正好贴边（默认，也是原先的固定行为），非 0 = 相对贴边位置的偏移。
    //
    // ⚠️ 这一项**以前完全没被消费**：`setPeekOffset` 只存了值、没人读，
    // 所以那个滑杆是"空滑杆"（拖了没反应）。现在接上了。
    const userShift = this.peekOffsetPx;
    /*
     * ## 横向 = 用户设定的值（已回退到这里）
     *
     * ⚠️ 我试过两个方向，**都被实测否决、已回退**，不要再走：
     *
     * **加"让开留白"** (`currentArtMarginX() + userShift`)：那圈留白是旧"立绘居中"
     * 布局的遗留，而吸附定位已经在**窗口层面**把"立绘边贴屏幕边"做完了 ——
     * 推两遍（实测右侧多推 29px、左侧多推 65px）。
     *
     * **改成恒 0**：想让"位置完全由窗口吸附决定"。但实测**反而更糟** ——
     * 探头窗口 384 而画布只有 250 且画布曾贴窗口左边，`--peek-shift-x` 归零后
     * 画布越界更多（贴左裁 106px），右侧也开始裁 27px。
     *
     * 所以回到**用户设定值**：它由设置里的「探头时的平移距离」控制，
     * 用户自己可以调到合适的位置（调 0 = 只贴边不探出）。
     *
     * 纵向仍用 `currentArtMarginY()`：上下边没有吸附逻辑，还得靠它让开留白。
     */
    const shiftX = userShift;
    const shiftY = this.currentArtMarginY() + userShift;

    /*
     * ---- 立绘的屏幕锚点：变尺寸时必须钉住它 ----
     *
     * 用户反馈过三次，都是同一件事没做对：
     *   · 进探头「只飘大概 60px」；
     *   · 出探头「向下也处理一下就好了」；
     *   · 「边缘探头又再次变高了」。
     *
     * 根因：窗口为给气泡留空间而加高（240x322），**立绘却贴窗口顶部**，
     * 于是"窗口中心"与"立绘中心"差约 41px。任何按窗口中心（或工作区夹紧）
     * 算的定位，都会让立绘整体上下平移。
     *
     * 定位逻辑现在**全在纯函数 `peekWindowBounds` 里**（`petLayout.ts`），
     * 运行时与自检共用同一份实现。这里只负责把实测到的立绘矩形喂给它。
     *
     * ⚠️ 喂的是**未旋转**的矩形（`uprightArtRect`）。用含旋转的那个会算出
     * "立绘顶部在窗口上方"这种不可能的结果（见交接文档第七节·0）。
     */
    const art = this.uprightArtRect;

    /*
     * 把定位交给**纯函数** `peekWindowBounds`（`petLayout.ts`）。
     *
     * ⚠️ 这里以前是内联的一份同样逻辑 —— 那样"测试里改好了、运行时还是旧的"，
     * 两者必然漂移（这次就踩了：自检抓到夹紧把立绘顶上去 72px，而运行时那份
     * 还带着同样的夹紧）。**一份实现、一处修改**。
     */
    /*
     * 吸附锚点（**静止态缓存**，见 `artInsetForSnap` 的说明）——
     * 声明在使用之前，因为 `peekWindowBounds` 的入参就要用它。
     */
    const snapInset = this.artInsetForSnap();
    const snapArtW = this.artVisibleWidth();

    /*
     * 光标吸附（2026-10-16）：把松手时的**光标位置**传下去，
     * 让立绘中心对齐到光标 —— 消除「探头形态随抓取点变」。
     *
     * ⚠️ 只在**拖拽结束**触发的那次传（见 enterEdgePeek 的调用方）；
     * 滑杆重排、全屏恢复等重入路径不传，保持"原点不动"——
     * 那些场景没有"用户刚松手"的语义，跳到光标处反而是惊吓。
     *
     * 立绘高度用实测值：吸附夹紧（立绘中心不出屏幕）要靠它。
     */
    const snapArtHeight = this.measuredArt.height > 0 ? this.measuredArt.height : undefined;

    const next = peekWindowBounds(
      bounds,
      /*
       * ⚠️ 用**静止态缓存**的左留白（`artInsetForSnap`），拿不到就返回 null
       * 让 `peekWindowBounds` 走"原点不动"分支 —— 宁可这次不吸附，
       * 也不要用错的留白（进探头那一刻现算的值实测差了 68px）。
       * 纵向偏移同理用静止态缓存（光标吸附要算立绘中心，见 restArtInsetY）。
       */
      snapInset != null
        ? {
            x: snapInset,
            y: this.restArtInsetY ?? (this.artOffsetInWindow ? this.artOffsetInWindow.y : 0),
          }
        : null,
      target,
      side,
      // 传**整屏**边界：贴边是"越出屏幕"，任务栏不该把窗口钉住
      { x: screenBounds.x, y: screenBounds.y, width: screenBounds.width, height: screenBounds.height },
      side === 'left' || side === 'right' ? shiftX : shiftY,
      /*
       * 贴上下边时的额外越界量。
       *
       * 用户提示：「宠物过不去，之前好像修的都是能过去的」—— 一针见血。
       * 窗口底边贴屏幕底**不等于**宠物能探出去：常态窗口比立绘高（下方留白约 82px），
       * 所以要让**立绘**越过屏幕边，窗口还得再往外多走这么多。
       *
       * 只在贴上下边时用（左右边的越界量由 `shiftX` 承担）。
       * 拿不到立绘高度时给 0，至少不会把窗口推到乱七八糟的地方。
       */
      Math.max(0, Math.round(bounds.height - (art ? art.height : 0))),
      /*
       * 立绘**可见宽度** —— 吸附到右边缘时要算"立绘右边贴屏幕右"。
       *
       * 用 `artVisibleWidth()`（与 `artInsetForSnap` 同源），保证
       * "左留白 + 立绘宽"这两个量出自同一份实测，不会一半新一半旧。
       */
      this.artVisibleWidth(),
      /*
       * ⚠️ `artOrigin` / `artSize` 两个参数**已不再传**（2026-10-04 回退）。
       *
       * 它们曾用来按"立绘矩形绕旋转中心转 45°"解析算外接框，想消掉"贴左时立绘
       * 左边被切"。但实测反而更差（`artBox` 是含阴影的包围盒、旋转中心是"立绘中心"，
       * 两个基准不同，先后差了 38px / 76px，窗口被推到屏幕外）。
       *
       * 参数本身留在 `peekWindowBounds` 签名里（缺省即退回"未旋转的边"），
       * 但这里不传 —— 保持"单一来源、不做旋转补偿"。
       * （`undefined` 占位的就是这两个参数；位置参数跳不过去。）
       */
      undefined,
      undefined,
      /*
       * 光标吸附（2026-10-16）：把松手时的光标位置与实测立绘高度传下去，
       * 立绘中心对齐光标 —— 消除「探头形态随抓取点变」（见 petLayout 的说明）。
       * 只在拖拽结束触发的那次有值；滑杆重排、全屏恢复等重入路径传 undefined，
       * 保持"原点不动"。
       */
      cursor,
      snapArtHeight,
    );
    window.setBounds(next);
    /*
     * 吸附决策日志 —— 排查"快拖时探头没进去 / 位置不对"用。
     *
     * 记下**输入与结果**：进探头前的窗口位置、姿态无关的左留白、立绘可见宽，
     * 以及算出的目标与结果。用户报的是「速度太快右边探头就这样了，没进去」，
     * 而程序化探针复现不出来（时序不同），所以靠这条从真实操作里读。
     */
    const snapTargetX =
      snapInset == null
        ? null
        : side === 'right'
          ? screenBounds.x + screenBounds.width - snapInset - snapArtW
          : screenBounds.x - snapInset;
    this.diagnostic(
      `吸附(${PEEK_SIDE_TEXT[side]})：窗口前 ${bounds.x},${bounds.y} ${bounds.width}x${bounds.height}` +
        ` | 左留白 ${snapInset} 立绘宽 ${snapArtW} | 目标 x=${snapTargetX}` +
        ` | 结果 ${next.x},${next.y} ${next.width}x${next.height}` +
        ` | 实测偏移 ${this.artOffsetInWindow ? JSON.stringify(this.artOffsetInWindow) : 'null'}`,
    );
    this.diagnostic(
      `探头定位（贴${PEEK_SIDE_TEXT[side]}边，原地旋转，比例不变）-> ` +
        `窗口 ${next.width}x${next.height}@${next.x},${next.y}，` +
        `留白 横${shiftX}/纵${shiftY} 让到屏幕外` +
        `（立绘 ${petSizeFor(this.petScale)} 恒定居中；工作区 ${wa.x},${wa.y} ${wa.width}x${wa.height}）`,
    );

    // 旋转方向要**朝屏幕里**倾，否则头往屏幕外歪，看起来像要掉出去。
    // CSS 里负角是逆时针，所以：
    //   贴右/下边 -> 负角（逆时针）；贴左/上边 -> 正角（顺时针）。
    // 上下边也用同一角度（45°）：效果是"斜着探进来"，和左右一致。
    const clockwise = side === 'left' || side === 'top';
    const angle = clockwise ? PEEK_ANGLE_DEG : -PEEK_ANGLE_DEG;
    // `offset` = 立绘要往屏幕边方向挪的像素（= 那一维的留白）。
    // 渲染层用它做 translate，让留白"让开"、立绘贴到边上。
    // 横向传 offsetX、纵向传 offsetY —— 立绘不是正方形，两个方向的留白不同。
    const offsetX = side === 'left' ? -shiftX : side === 'right' ? shiftX : 0;
    /*
     * ⚠️ 纵向**只在贴上下边时**才让位。
     *
     * 用户反馈（原话）：「探头的时候平移加向上，导致了出来的时候平移加向下，
     * 观感很怪，**不要加上下，平移旋转过去就好**」。
     *
     * 原来这里对四个方向都无条件算 `shiftY`，于是贴**左右**边时立绘也被纵向推走 ——
     * 进探头时往上、退出时往下，凭空多出一段上下运动，看着像在"飘"。
     *
     * 几何上本来也不该动：贴左右边要让开的是**横向**留白，纵向留白与这条边无关。
     */
    const offsetY = side === 'top' ? -shiftY : side === 'bottom' ? shiftY : 0;
    /*
     * 恒等式自检：**贴左右边时纵向偏移必须是 0**。
     *
     * 用户反馈：「探头的时候平移加向上，导致了出来的时候平移加向下，观感很怪，
     * 不要加上下，平移旋转过去就好」。
     *
     * 几何上贴左右边要避开的是**横向**留白，纵向留白与这条边无关 ——
     * 一旦纵向也挪，进探头时立绘往上、退出时往下，凭空多出一段上下运动。
     * 这里断言一下，将来改这段代码时若又引入纵向偏移，能立刻发现。
     */
    if ((side === 'left' || side === 'right') && offsetY !== 0) {
      this.diagnostic(`⚠ 贴左右边却算了纵向偏移 ${offsetY}（应当为 0）`);
    }
    /*
     * 文字弹窗（气泡）的横向平移 —— 用户要求：
     * 「当宠物处在探头状态时，文字不要超出屏幕」。
     *
     * 探头时窗口有一大半在屏幕外，而气泡按窗口居中 => 有一半也跑到屏幕外。
     * 所以要把它往**屏幕内侧**推。两条约束：
     *   ① 气泡不能超出窗口左右边（否则被窗口裁掉，根本看不见）；
     *   ② 气泡不能伸出屏幕（那正是用户报的问题）。
     * 求出同时满足两者的"气泡中心相对窗口中心"的范围，取离 0（窗口中心）最近的值。
     *
     * 用**气泡的最大宽度**（窗口宽 - 16）来算：气泡宽度是 fit-content 会变，
     * 按最大宽度算出的范围对任何内容都安全（宁可保守）。
     */
    const bubbleMax = Math.max(40, next.width - 16);
    const halfWin = next.width / 2;
    // 约束①：气泡中心相对窗口中心的允许范围
    const relMin = -halfWin + bubbleMax / 2;
    const relMax = halfWin - bubbleMax / 2;
    // 约束②：气泡中心的世界坐标必须落在 [wa.x + 半宽, wa.x + wa.width - 半宽]
    const clampLo = Math.max(relMin, wa.x + bubbleMax / 2 - next.x - halfWin);
    const clampHi = Math.min(relMax, wa.x + wa.width - bubbleMax / 2 - next.x - halfWin);
    // 区间为空（窗口比气泡还窄 / 屏幕太小）时取中点，至少不会偏得更远
    const bubbleShift = Math.round(
      clampLo <= clampHi ? Math.max(clampLo, Math.min(clampHi, 0)) : (clampLo + clampHi) / 2,
    );
    // 存下来作为**基线**：气泡宽度是按最大值估的，实际内容可能更窄。
    // `setMeasuredBubbleRect` 会用实测尺寸再精修一次。
    this.peekBubbleShiftBase = bubbleShift;
    window.webContents.send('pet:peek', {
      peeking: true,
      side,
      angle,
      offsetX,
      offsetY,
      bubbleShift,
    });
    // 贴哪边 —— 渲染层据此把画布推向窗口内侧
    window.webContents.send('pet:compact', { compact: true, side });
    // 画布恒为待机大小：比例不变，居中平移与旋转都交给 CSS。
    window.webContents.send('pet:set-pet-size', {
      size: petSizeFor(this.petScale),
      artShift: artShiftFor(this.petScale),
      peekScale: peekFitScaleFor(this.petScale),
      // 调试叠层（染色 + 实时坐标）：`PET_DEBUG_HUD=1` 时开。
      // 排查探头定位那串 bug 时加的 —— 用户截一张图就能给出全部关键数字。
      debugHud: process.env.PET_DEBUG_HUD === '1',
    });
  }

  /**
   * 退出边缘探头。
   *
   * `straightenOnly` 为真时只"拉直"（立绘转回 0°）但**不收窗口**。
   */
  exitEdgePeek(reason: string, straightenOnly = false): void {
    const window = this.window;
    if (!window || window.isDestroyed()) return;
    if (!this.edgePeek) return;

    this.diagnostic(`退出边缘探头（${reason}${straightenOnly ? '，先拉直' : ''}）`);

    if (straightenOnly) {
      // 只拉直：立绘反向转回 0°，窗口仍在边缘、卡片仍隐藏。
      // `offset*: 0` 让位移一起滑回去（渲染层按它做 translate）。
      window.webContents.send('pet:peek', {
        peeking: false,
        side: this.edgePeekSide,
        angle: 0,
        offsetX: 0,
        offsetY: 0,
      });
      return;
    }

    this.edgePeek = false;
    // 反向旋转回正：渲染层收到 peeking:false 就去掉 rotate，
    // 由 CSS 过渡在 0.5s 内转回 0°（用户要求）。
    window.webContents.send('pet:peek', {
      peeking: false,
      side: this.edgePeekSide,
      angle: 0,
      offsetX: 0,
      offsetY: 0,
    });
    // ⚠️ **不要**在这里调 applyLayout 把窗口挪回右下角。
    //
    // 那正是"拖拽出探头时闪现"的原因：窗口会被猛地搬走，而反向旋转的动画
    // 还没演完，看起来就是闪一下。用户明确要求过渡方式是"反向旋转回来"，
    // 所以窗口**就地不动**（探头时它贴边，用户拖离后它本来就在合适位置）。
    window.webContents.send('pet:compact', { compact: false });
    /*
     * 尺寸保险（只有真的不一致时才收回去）走 `resizeKeepingArt`。
     *
     * ⚠️ 但**要等回正之后**再用 —— 见 `scheduleNormalResizeAfterPeekExit`。
     * 这里若立刻调，手里的偏移还是探头态的（实测 `y:-50`），窗口会多推几十像素。
     */
    if (!this.pendingNormalResize) {
      this.scheduleNormalResizeAfterPeekExit();
    }
    this.onLayoutChanged?.('normal');
  }

  /** 用户点了宠物（探头态下表示"别偷看了，拉直吧"） */
  handlePetClickWhilePeeking(): boolean {
    if (!this.edgePeek) return false;
    this.exitEdgePeek('用户点击', true);
    return true;
  }

  /**
   * 布局变化回调（由主进程接上，用于让**对话条窗口**跟着重排）。
   *
   * 为什么不靠渲染层回报：宠物窗口的位置/大小完全由主进程决定
   * （所有 `setBounds` 都在这一侧），渲染层只是被告知结果。让"谁动了窗口"
   * 和"谁通知对话条"分成两个进程，就会出现"消息没到 → 对话条不动"
   * 这类跨越 IPC 的静默失败（实测卡了很久都没定位到）。
   * 现在改成：**主进程改完布局，自己通知对话条**，链路上少一环。
   */
  /**
   * 当前窗口里**立绘可见部分的屏幕位置**（左上角）。
   *
   * 为什么用 `artScreenAnchor` 而不是 `artScreenRect`：后者只覆盖"矩形偏右且沿对角轴
   * 延伸"的那种矩形，拿不到时它会返回 null，而 `artScreenAnchor` 有退化估算兜底，
   * 不会漏掉。
   */
  /**
   * 换窗口尺寸，并保持**立绘在屏幕上不动**。
   *
   * ⚠️ 用户反馈过两次：
   *   · 进探头「只飘大概 60px」；
   *   · 出探头「向下也处理一下就好了」。
   *
   * 根因是同一个：窗口为给气泡留空间而加高，**立绘贴在窗口顶部**，
   * 所以"窗口中心"和"立绘中心"差了约 41px。只要按"窗口中心"换尺寸
   * （或按右下角重锚），那 41px 的差就会让立绘整体上下平移。
   *
   * 这里改成**按立绘自身的屏幕位置反推**新窗口原点：窗口怎么变大变小，
   * 立绘都待在原处。
   *
   * ⚠️ 与 `applyLayout` 的"右下角锚定"取向不同，取舍见那里的说明：
   * 进出探头的跨轴位置比"贴不贴右下角"重要得多。
   */
  private resizeKeepingArt(next: { width: number; height: number }): void {
    const window = this.window;
    if (!window || window.isDestroyed()) return;
    const current = window.getBounds();
    if (current.width === next.width && current.height === next.height) return;

    /*
     * 算式全在纯函数里（`windowBoundsKeepingArt`），运行时与自检**共用一份**。
     *
     * 传的是**立绘相对窗口的偏移**（不是屏幕绝对位置）——
     * 详见 `artOffsetInWindow` 的说明：绝对位置会在窗口移动后失效。
     */
    const bounds = windowBoundsKeepingArt(current, this.artOffsetInWindow, next);
    /*
     * 诊断：**换尺寸前后的立绘屏幕位置**要一起记。
     *
     * 用户怀疑「拖拽结束后进行碰撞箱/立绘的归正导致位移」—— 这条日志正好能验证：
     * 若 `立绘屏幕前` 与 `立绘屏幕后` 差得多，就是这一步把立绘挪走的；
     * 若两者相同而用户仍看到移动，那问题在别处（比如渲染层的旋转弧）。
     *
     * 计算方式：立绘屏幕位置 = 窗口原点 + 立绘在窗口内的偏移。
     * 换尺寸**前**的偏移就是 `artOffsetInWindow`；换尺寸**后**立绘在窗口内的
     * 相对位置不变（它贴顶、横向居中），所以用同一个偏移即可。
     */
    const before = this.artOffsetInWindow
      ? `${current.x + this.artOffsetInWindow.x},${current.y + this.artOffsetInWindow.y}`
      : '未知';
    const after = this.artOffsetInWindow
      ? `${bounds.x + this.artOffsetInWindow.x},${bounds.y + this.artOffsetInWindow.y}`
      : '未知';
    window.setBounds(bounds);
    this.diagnostic(
      `恢复尺寸 -> 窗口 ${current.x},${current.y} ${current.width}x${current.height}` +
        ` => ${bounds.x},${bounds.y} ${bounds.width}x${bounds.height}` +
        ` | 立绘屏幕 ${before} -> ${after}` +
        ` | 偏移 ${this.artOffsetInWindow ? JSON.stringify(this.artOffsetInWindow) : 'null'}`,
    );
  }

  /** 调试叠层是否开启（`PET_DEBUG_HUD=1`） */
  private get debugHud(): boolean {
    return process.env.PET_DEBUG_HUD === '1';
  }

  /**
   * 主动要一次"立绘在窗口内的矩形"上报。
   *
   * ⚠️ 为什么需要：`artOffsetInWindow` 只在渲染层**主动上报**时才有值，
   * 而拖拽期间它很少上报。等到 `drag-end` 要进探头时才发现是 `null` ——
   * 锚点拿不到就退到"窗口中心对齐"，而立绘不居中，于是窗口下移 33px。
   *
   * 渲染层收到这条消息会**同步**调 `reportArtRect()`，所以紧接着的
   * `pet:art-rect` 就会把值填上（同一次事件循环内，足够快）。
   */
  private requestArtRect(): void {
    if (!this.window || this.window.isDestroyed()) return;
    this.window.webContents.send('pet:request-art-rect', {});
  }

  /**
   * 探头转场动画时长（毫秒）—— 必须与 `styles.css` 的 `--peek-rotate-ms` 一致。
   *
   * ⚠️ 主进程读不到 CSS 变量，只能在这里镜像一份。**改一处要改两处**，
   * 所以两边都写了注释互相指认。不一致的后果见
   * `scheduleNormalResizeAfterPeekExit`：换尺寸会落在动画中途，立绘错位。
   */
  private static readonly PEEK_TRANSITION_MS_MIRROR = 1000;

  /** 等动画结束后再多留一点（渲染层上报 + 绘制落定） */
  private static readonly NORMAL_RESIZE_MARGIN_MS = 200;

  /**
   * 把调试叠层的开关注下发一次。
   *
   * ⚠️ 单独发而不是只挂在 `pet:set-pet-size` 上：那条消息在**默认档位下
   * 可能根本不发**（`applyLayout` 只在启动/改大小时跑），于是叠层不生效
   * （实测：`#debug-hud 存在 false`）。启动时明确发一次最稳。
   *
   * payload 只有 `debugHud`，渲染层会跳过尺寸相关的处理（见那里的注释）。
   */
  private sendDebugHud(): void {
    if (!this.window || this.window.isDestroyed()) return;
    this.window.webContents.send('pet:set-pet-size', { debugHud: this.debugHud });
  }

  /**
   * 把**画布尺寸**下发给渲染层。
   *
   * ## ⚠️ 为什么必须单独发（2026-10-04 实测查到）
   *
   * 渲染层启动时用的是**硬编码** `PET_SIZE = 240`（`app.js:21`）建画布，
   * 只有收到 `pet:set-pet-size` 才会改成真值。而 `applyLayout`（唯一会发这条的
   * 地方）在启动时**不一定跑** —— 结果是：
   *
   *     窗口 250x335   ← 主进程算的
   *     画布 240x240   ← 渲染层的硬编码，从来没被纠正
   *
   * 两者不一致会让**所有基于"立绘在窗口内偏移"的计算都偏**，最直接的表现是
   * **拖拽极限**：用户报「宠物有时候能到边缘，有时候不能」。
   *
   * 所以加载完成后主动发一次，和 `sendDebugHud` 同一个道理。
   */
  private sendPetSize(): void {
    if (!this.window || this.window.isDestroyed()) return;
    this.window.webContents.send('pet:set-pet-size', {
      size: petSizeFor(this.petScale),
      artShift: artShiftFor(this.petScale),
      peekScale: peekFitScaleFor(this.petScale),
      debugHud: this.debugHud,
    });
  }

  onLayoutChanged: ((form: PetForm) => void) | null = null;

  /**
   * 立绘在窗口内的**垂直偏移**（相对"画布顶 = 窗口顶"）。
   *
   * ---- 为什么需要它（加高窗口时踩的坑） ----
   *
   * 用户要求「可以加高宠物窗口」给气泡留空间后，窗口从 240 见方变成 240x325。
   * 如果仍按"窗口中心"摆立绘（`peekTargetCenterY` 那套原逻辑），加高出来的
   * 部分会全堆到**上方** —— 实测画布被推到 `top = -20`（跑出窗口外），
   * 宠物在屏幕上**跳了一大截**，而且顶部留白 104px、完全没法用。
   *
   * 所以改成：**立绘位置不动，多出来的高度全部加在下方**（`top = 0`）。
   * 这样：
   *   · 宠物在屏幕上的位置与加高前一致（用户感知不到跳变）；
   *   · 头顶以上那圈留白（原来就有的 33px）仍然可用于定位，
   *     而新增的空间在下方 —— 文字弹窗有地方放，且不会盖到宠物脸。
   */
  private artOffsetY = 0;

  /**
   * 重排窗口到当前 (scale, form)。
   *
   * 尺寸与锚点计算都在 `petLayout.ts`（纯函数、有测试）——
   * 这里只负责把它接到 Electron 窗口上。
   */
  private applyLayout(reason: string): void {
    const window = this.window;
    if (!window || window.isDestroyed()) return;

    const form: PetForm = this.compact ? 'compact' : 'normal';
    // ⚠️ 尺寸**恒定**（见 windowSizeFor 的说明）：常态与探头共用同一个窗口尺寸，
    // 所以这里只算"窗口该放哪"，不再改大小。改大小会让 `setBounds` 一帧硬跳，
    // 那正是"变身/收回不丝滑"的原因。
    const target = windowSizeFor(this.petScale, form);
    const current = window.getBounds();
    const { workArea } = screen.getDisplayNearestPoint({ x: current.x, y: current.y });

    // 立绘在窗口内的中心（常态/探头都是窗口正中）
    const petCenter = petCenterInWindow(this.petScale, form);

    let next: { x: number; y: number; width: number; height: number };
    if (form === 'compact') {
      // 紧凑（别人全屏）：**走和手动贴边完全相同的探头定位**。
      //
      // 用户要求：「全屏时固定成现在普通状态下在屏幕左右边最终位置的探头样子」。
      // 所以这里不再自己用 `anchorPeekRight` 算一个"只露一条"的位置 ——
      // 那和手动贴边的样子不一样（露出比例、留白、旋转都不同）。
      // 现在统一交给 `applyPeekLayout`（`enterEdgePeek` 用的也是它），
      // 两侧必然一致；"两处各算一次"是这个项目反复踩的坑。
      //
      // 边由 `pickNearestSide()` 按宠物当前水平位置决定（左半屏贴左、右半屏贴右）。
      const side = this.pickNearestSide();
      this.diagnostic(`紧凑模式（${reason}）：按当前位置选边 -> 贴${PEEK_SIDE_TEXT[side]}边`);
      this.applyPeekLayout(side);
      return;
    }
    {
      // 常态：窗口锚在右下角。
      //
      // ⚠️ 这里**不再自己算 inset**。之前写的是
      //     insetX = 窗口宽 - 立绘中心（相对窗口）
      // 那是"立绘偏右下、要靠 inset 补偿"的旧模型，和现在"立绘在窗口里居中"
      // 的模型冲突 —— 算出来是 (0,0)，窗口贴死在屏幕角，留白全堆在左/上，
      // 于是"调大小之后留白变了"（用户报的 bug）。
      //
      // 现在立绘在窗口里居中（由渲染层的居中平移保证），所以窗口直接锚右下角
      // 就等于立绘离屏幕右/下边各留"半个窗口留白"，是**对称**的、也与缩放无关。
      const { workArea: wa } = screen.getDisplayNearestPoint({ x: current.x, y: current.y });
      void petCenter;
      next = {
        x: Math.round(wa.x + wa.width - target.width),
        y: Math.round(wa.y + wa.height - target.height),
        width: target.width,
        height: target.height,
      };
    }

    // 尺寸恒定 -> 这一步只在窗口尺寸真的变了时才有效果（首次/缩放变化时）。
    // 用 setBounds 一次给全四个值：setPosition 在 1.25 DPI 下会让尺寸漂移。
    window.setBounds(next);
    this.diagnostic(
      `布局重排（${reason}）-> 窗口 ${next.width}x${next.height}@${next.x},${next.y}，` +
        `立绘 ${petSizeFor(this.petScale)}，形态=${form}`,
    );

    // 通知渲染层把画布也调成同一尺寸，否则立绘会与窗口不匹配。
    // 同时给"居中平移量"：立绘在画布里偏右下，必须平移才能让窗口贴边
    // 等于立绘贴边（见 petLayout.ts 的 ART_BOX_RATIO 说明）。
    window.webContents.send('pet:set-pet-size', {
      size: petSizeFor(this.petScale),
      artShift: artShiftFor(this.petScale),
      peekScale: peekFitScaleFor(this.petScale),
      // 调试叠层（染色 + 实时坐标）：`PET_DEBUG_HUD=1` 时开。
      // 排查探头定位那串 bug 时加的 —— 用户截一张图就能给出全部关键数字。
      debugHud: process.env.PET_DEBUG_HUD === '1',
    });

    // 布局改完了 -> 通知上层（对话条要跟着换位置）。
    // 放在**最后**：调用方收到回调时 `getBounds()` 已经是新值，
    // 它按实际位置算对话条才不会算到旧坐标。
    this.onLayoutChanged?.(form);
  }

  /**
   * 渲染层实测的立绘可见尺寸（CSS 像素）。0 = 还没量到，退回按比例推算。
   *
   * 为什么要有这个：贴边判定与"拖拽放宽多少"都依赖
   * "窗口边到立绘可见边"的距离，而那个距离以前靠 `ART_BOX_RATIO`
   * 这组**从截图量的常数**推算 —— 布局一改就不准（用户报的
   * "调完大小留白变了，改成自适应"）。
   *
   * 现在渲染层从画布像素实测后报上来，主进程直接用实测值算，
   * 缩放变化、换素材都不用改代码。
   */
  private measuredArt = { width: 0, height: 0 };

  /** 渲染层上报实测立绘尺寸（见 app.js 的 measureArtBox） */
  setMeasuredArtSize(width: number, height: number): void {
    if (!Number.isFinite(width) || !Number.isFinite(height) || width < 8 || height < 8) return;
    // ⚠️ 去抖：渲染层会在尺寸变化后量若干次，而每一点抖动都会触发重排 +
    // 对话条重新定位（实测刷了几十条重复日志）。0.5px 以内的变化不当回事，
    // 而且**只有真变了才往下走**，避免无意义重排。
    const changed =
      Math.abs(this.measuredArt.width - width) > 0.5 ||
      Math.abs(this.measuredArt.height - height) > 0.5;
    if (!changed) return;
    const first = this.measuredArt.width === 0;
    this.measuredArt = { width, height };
    this.diagnostic(
      `${first ? '首次' : '更新'}实测立绘包围盒 ${Math.round(width)}x${Math.round(height)}` +
        ` -> 留白 横${this.currentArtMarginX()} 纵${this.currentArtMarginY()}`,
    );
  }

  /**
   * 当前的**左留白**（窗口左边 → 立绘可见左边），拖拽夹紧用。
   *
   * ## ⚠️ 不能再用"居中假设"（2026-10-04 修）
   *
   * 旧实现是 `(窗口宽 − 立绘宽) / 2` —— 那是**"立绘在窗口里居中"的假设**。
   * 同一个错误在纵向已经修过一次（见 `currentArtMarginY` 的注释：
   * 「不能用横向那种 `(窗口 − 立绘)/2`」），**但横向一直没改**。
   *
   * 而实测的真实横向偏移并不等于那个值：HUD 里 `artBox @ 119,129`、
   * 窗口 250x335 ⇒ 立绘左边在窗口内 `119 − 173/2 ≈ 33`，而居中假设给 39；
   * 更要紧的是**上下留白差得远**（15 vs 99），所以"居中"这个前提本身就不成立。
   *
   * 用户报的现象：「宠物有时候能到边缘，有时候不能」—— 拖拽极限由留白决定，
   * 留白算错多少，宠物就离屏幕边差多少。
   *
   * ## 现在用实测锚点
   *
   * `artOffsetInWindow` 就是渲染层上报的**"立绘左边到窗口左边的距离"**，
   * 正是这里要的量（不是"留白"）。拿不到时退回按比例推算。
   */
  /**
   * 立绘左边到窗口左边的距离 —— **姿态无关**的那个值，**吸附定位专用**。
   *
   * ## 为什么不能现算（2026-10-04 日志实测，这是"快拖探头位置不对"的根因）
   *
   * 进探头那一刻（`drag-end` 里同步调 `applyPeekLayout`）**渲染层还没上报稳定几何**：
   * `measuredArt` 是空的、`artOffsetInWindow` 还是拖拽中途的值。日志原文：
   *
   *     吸附(右)：窗口前 1360,285 387x388 | **左留白 38** 立绘宽 175 | 目标 x=1323
   *               | 实测偏移 {"x":-30,"y":21}
   *
   * 「左留白 38」是按比例推算的兜底值，而同一时刻实测偏移是 **-30** —— 差 68px，
   * 于是吸附到错位置。而偏移在拖拽期间会一路漂（实测 `-30 → -2 → 8 → 27 → 29`），
   * 所以**任何时候现算都不可靠**。
   *
   * ## 解法：只用**静止态缓存**
   *
   * `restArtInsetX` / `restArtWidth` 在"不在拖拽、不在探头"时由 `pet:art-rect`
   * 上报刷新（见那里的赋值）。那是唯一稳定的观测窗口。
   * 拿不到缓存时返回 null，让调用方**放弃吸附**（保持原位）——
   * 宁可这次不吸附，也不要按错的留白把立绘推到屏幕外。
   */
  private restArtInsetX: number | null = null;
  /** 静止态实测的立绘可见宽度（同上，一起缓存） */
  private restArtWidth: number | null = null;
  /**
   * 静止态实测的立绘**上边**到窗口上边的距离（2026-10-16 加）。
   *
   * 与 `restArtInsetX` 同一批、同一个 `atRest` 判据刷新。加它是因为
   * 「光标吸附」要算**立绘中心在窗口内的位置**（中心 y = 上边偏移 + 立绘高/2），
   * 拿现算的 `artOffsetInWindow.y` 会把拖拽/旋转中途的值当静止值用 ——
   * 正是横向当年"现算不可靠"的同一个坑，别再踩一遍。
   */
  private restArtInsetY: number | null = null;

  private artInsetForSnap(): number | null {
    return this.restArtInsetX;
  }

  /** 立绘可见宽度（吸附与拖拽极限共用；优先用静止态缓存） */
  private artVisibleWidth(): number {
    if (this.restArtWidth != null) return this.restArtWidth;
    if (this.measuredArt.width > 0) return this.measuredArt.width;
    return artSizeFor(this.petScale).width;
  }

  /**
   * **旋转中心**（`--art-origin`，相对画布的像素坐标）。
   *
   * 渲染层在 `pet:art-rect` 里一起报上来；没报过时为 null，那时吸附退回旧算式
   * （只用未旋转的边，会有一侧被窗口裁掉，但至少不跳）。
   */

  private currentArtMarginX(): number {
    if (this.artOffsetInWindow) return Math.max(0, Math.round(this.artOffsetInWindow.x));
    const win = windowSizeFor(this.petScale, 'normal');
    if (this.measuredArt.width > 0) {
      return Math.max(0, Math.round((win.width - this.measuredArt.width) / 2));
    }
    const art = artSizeFor(this.petScale);
    return Math.max(0, Math.round((win.width - art.width) / 2));
  }

  /**
   * **纵向**的"窗口底边到立绘底边"距离。
   *
   * ⚠️ 必须单独算，不能复用横向：立绘 155x177 不是正方形，两个方向留白不同。
   *
   * ⚠️⚠️ **也不能用横向那种 `(窗口 − 立绘)/2`**（那是"立绘居中"的假设）。
   * 实测：立绘**贴窗口顶部**，上留白 32、下留白 113 —— 两者**不相等**。
   * 用居中的 73 会让拖拽下界少算 40px，表现就是用户报的
   * 「宠物往屏幕最下面移...下不去」「离屏幕底还差 40px」。
   *
   * 拖拽下界要的是"立绘底边到窗口底边"的真实距离，所以这里按
   * `窗口高 − 立绘在窗口内的 top − 立绘高` 算。
   */
  /**
   * **右留白**（立绘右边 → 窗口右边），拖拽夹紧的右界用。
   *
   * ⚠️ 不能复用 `currentArtMarginX`（那是**左留白**）。立绘在画布里不是左右对称的：
   * 实测左 33、右 42（差 9px），共用会让右边缘差一截 ——
   * 用户报的「宠物在边缘时不时被挡住」就是这个（左边正常、右边卡住）。
   *
   * 算法：窗口宽 − 立绘左偏移 − 立绘宽。拿不到实测值就退回"左右相等"的旧假设。
   */
  private currentArtMarginRight(): number {
    const win = windowSizeFor(this.petScale, 'normal');
    const artWidth = this.measuredArt.width > 0 ? this.measuredArt.width : artSizeFor(this.petScale).width;
    if (this.artOffsetInWindow) {
      return Math.max(0, Math.round(win.width - this.artOffsetInWindow.x - artWidth));
    }
    return this.currentArtMarginX();
  }

  private currentArtMarginY(): number {
    /*
     * ⚠️ 优先用**实测锚点**（渲染层上报的"立绘上边到窗口上边的距离"）。
     *
     * 下面那套按比例推算在"画布尺寸/裁切区改过"之后就不准了 ——
     * 同一个毛病横向也有（见 `currentArtMarginX`）。
     */
    if (this.artOffsetInWindow) {
      const win = windowSizeFor(this.petScale, 'normal');
      const artHeight =
        this.measuredArt.height > 0 ? this.measuredArt.height : artSizeFor(this.petScale).height;
      // 下留白 = 窗口高 − 立绘上边偏移 − 立绘高
      return Math.max(0, Math.round(win.height - this.artOffsetInWindow.y - artHeight));
    }
    const win = windowSizeFor(this.petScale, 'normal');
    const artHeight = this.measuredArt.height > 0 ? this.measuredArt.height : artSizeFor(this.petScale).height;
    // 立绘贴窗口顶部，但画布（= 立绘尺寸的方框）内还有一层居中留白
    const artTopInWin = Math.max(0, Math.round((petSizeFor(this.petScale) - artHeight) / 2));
    return Math.max(0, Math.round(win.height - artTopInWin - artHeight));
  }

  /**
   * 立绘在窗口内的 **top**（上留白）。
   *
   * 与 `currentArtMarginY()`（下留白）**必须分开**：立绘贴窗口顶部，
   * 上留白约 32px、下留白约 113px，两者不相等。
   *
   * 用途：拖拽的**上界** —— 让立绘顶边能走到屏幕顶
   * （用户要求「宠物能超出屏幕也改一下」）。
   */
  private currentArtMarginTop(): number {
    const artHeight = this.measuredArt.height > 0 ? this.measuredArt.height : artSizeFor(this.petScale).height;
    return Math.max(0, Math.round((petSizeFor(this.petScale) - artHeight) / 2));
  }

  /**
   * 文字弹窗的存在时间（毫秒）。
   *
   * 用户要求：「设置中可以设置文字弹窗的存在时间，默认 10 秒后消失」。
   * 真源在 `settingsStore`（要落盘），这里只做**转发**给渲染层 ——
   * 渲染层的计时器才是实际执行者。
   */
  setBubbleLinger(ms: number): void {
    if (!Number.isFinite(ms) || ms <= 0) return;
    this.bubbleLingerMs = Math.round(ms);
    this.window?.webContents.send('pet:set-bubble-linger', { ms: this.bubbleLingerMs });
  }

  get bubbleLinger(): number {
    return this.bubbleLingerMs;
  }

  private bubbleLingerMs = BUBBLE_LINGER_DEFAULT_MS;

  /** 由主进程注入的诊断回调（写日志用） */
  onDiagnostic: ((line: string) => void) | null = null;

  /**
   * @param distDir 主进程产物所在目录（apps/desktop/dist）。
   *   由调用方传入而不是用 import.meta.url 自己算：主进程是 CommonJS，
   *   import.meta 在 CJS 下不可用。
   */
  constructor(
    private readonly distDir: string,
    private readonly devMode: boolean,
  ) {}

  create(): BrowserWindow {
    const { workArea } = screen.getPrimaryDisplay();
    const x = Math.max(workArea.x, workArea.x + workArea.width - HUD_WIDTH - MARGIN);
    const y = Math.max(workArea.y, workArea.y + workArea.height - HUD_HEIGHT - MARGIN);

    const window = new BrowserWindow({
      width: HUD_WIDTH,
      height: HUD_HEIGHT,
      x,
      y,
      transparent: true,
      frame: false,
      resizable: false,
      movable: true,
      hasShadow: false,
      // 是否在任务栏里占一个按钮。
      //
      // ## 2026-10-12 改回**不占**（用户要求）
      //
      // 用户原话：「现在把这个图标隐藏吧，现在有托盘了，不需要显示这个了」。
      //
      // ## 之前为什么改成占（这段历史别丢）
      //
      // 这里原本写死 `skipTaskbar: true`，理由是"宠物不该占任务栏"。但实践
      // 证明当时那是个坏前提：窗口不占任务栏，**托盘图标就成了唯一出口**，
      // 而 Windows 11 会把新托盘图标收进溢出区（`^` 里），用户根本找不到它 ——
      // 结果就是"宠物关不掉"。这个坑真实发生过，且反复出现。
      // 所以后来默认改成**占任务栏**：右键任务栏按钮就能关，最符合 Windows 习惯。
      //
      // ## 为什么现在能改回来
      //
      // 那个坏前提**不成立了**：
      //   · 托盘图标已确认正常显示（用户实测「托盘现在有了」）
      //   · 托盘菜单里有完整的「隐藏宠物 / 退出 / 退出全部实例」
      //   · 宠物被隐藏时**窗口本来就不显示**，那时任务栏按钮也没意义
      //   · 设置窗口里还有一个「显示宠物」按钮兜底
      //
      // ## ⚠️ 万一又找不到出口
      //
      // 设环境变量 `PET_TASKBAR=1` 就能把任务栏按钮要回来（不用改代码、不用重新构建）。
      // 这条退路是刻意留的 —— 上一个版本的教训就是"唯一出口丢了"。
      skipTaskbar: process.env.PET_TASKBAR !== '1',
      alwaysOnTop: true,
      fullscreenable: false,
      maximizable: false,
      minimizable: false,
      // 透明窗必须显式给全透明背景，否则某些驱动下会出现黑底
      backgroundColor: '#00000000',
      webPreferences: {
        preload: resolvePreload(this.distDir),
        contextIsolation: true,
        nodeIntegration: false,
        sandbox: true,
        // 渲染层不发网络请求，一切走主进程转发的本机 socket
        webSecurity: true,
        // ⚠️ 必须关掉后台节流。
        //
        // 宠物窗口大部分时间**不是焦点窗口**，而 Chromium 默认会对后台页面
        // 降频 rAF、并暂停"只有视频没有音频"的媒体（实测报错原文：
        // "video-only background media was paused to save power"）。
        // 后果是立绘停住不动、甚至整块空白 —— 而桌宠恰恰要求"一直动着"。
        backgroundThrottling: false,
      },
    });

    // 置顶层级。
    //
    // ⚠️ 这里曾经用 'screen-saver'，理由是"能压住任务栏"。实际用起来才发现
    // 那正是问题：**任务栏图标会被这个窗口盖住**（窗口有 460×640，底边贴近
    // 屏幕底部，而窗口的透明区域同样参与层级覆盖），用户点不到开始菜单与托盘。
    //
    // 对桌面宠物来说压住任务栏是过度的：它的诉求只是"浮在普通窗口之上"，
    // 而 'normal' 层级正好满足这一点，同时把任务栏让出来。
    //
    // 需要盖住任务栏（例如全屏游戏时也要可见）时设 PET_ALWAYS_ON_TOP=screen-saver。
    const topLevel = (process.env.PET_ALWAYS_ON_TOP ?? '').trim().toLowerCase();
    const level: 'normal' | 'screen-saver' = topLevel === 'screen-saver' ? 'screen-saver' : 'normal';
    this.topLevel = level;
    window.setAlwaysOnTop(true, level);
    window.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });

    // 默认穿透：鼠标落在非交互区时事件透给下面的应用
    window.setIgnoreMouseEvents(true, { forward: true });
    this.clickThrough = true;

    void window.loadURL(rendererUrl(resolveRendererPage(this.distDir)));

    // 调试器默认**不开**：它会多出一个独立窗口，而"桌面上莫名多了个浏览器窗口"
    // 很容易被当成程序出错。要看渲染层日志时用 `dev.cmd --devtools`。
    if (this.devMode && process.env.PET_DEVTOOLS === '1') {
      window.webContents.openDevTools({ mode: 'detach' });
    }

    // 渲染层自检：把页面的真实状态写进主进程日志。
    //
    // 为什么需要它：透明窗有一个很坑的性质 —— **渲染层整个挂掉和正常渲染在
    // 外面看起来一模一样**（都是"什么都看不见"）。而这台机器上无法目视调试，
    // 所以必须在启动时把可判定的事实记下来：DOM 有没有建出来、app.js 有没有
    // 执行、立绘是精灵图还是程序化占位、有没有报错。
    window.webContents.once('did-finish-load', () => {
      setTimeout(() => void this.probeRenderer(), 1500);
      /*
       * 调试叠层：加载完成后立刻下发一次开关。
       *
       * ⚠️ 也延后一点再发一次：渲染层是**先加载、后注册 IPC 监听**的，
       * 太早发会丢（这类"消息比监听早"的坑本项目踩过多次）。
       */
      this.sendDebugHud();
      // ⚠️ **画布尺寸也必须在启动时主动发一次** —— 渲染层用的是硬编码 240，
      // 不发就永远和窗口（250）不一致，见 `sendPetSize` 的说明。
      this.sendPetSize();
      setTimeout(() => {
        this.sendDebugHud();
        this.sendPetSize();
      }, 800);
    });
    window.webContents.on('render-process-gone', (_event, details) => {
      this.onDiagnostic?.(`渲染进程退出：reason=${details.reason} exitCode=${details.exitCode}`);
    });
    window.webContents.on('console-message', (...args: unknown[]) => {
      /*
       * ⚠️ **Electron 32 的签名变了**：`level` 从数字（0/1/2/3）变成了**字符串**
       * （`'debug' | 'info' | 'warning' | 'error'`）。
       *
       * 旧代码写的是 `if (level >= 2)` —— 拿字符串和数字比，**恒为 false**，
       * 于是渲染层的 console **基本从没进过日志**。排查时我加了 `console.log`
       * 却怎么都看不到，就是因为这个（绕了好一会儿）。
       *
       * 这里把两种签名都兼容：数字按老规则，字符串按新规则。
       */
      const second = args[1];
      const third = args[2];
      let isWarn: boolean;
      let message: string;
      if (typeof second === 'string') {
        isWarn = second === 'warning' || second === 'error';
        message = String(typeof third === 'string' ? third : args[3] ?? '');
      } else {
        isWarn = Number(second) >= 2;
        message = String(third ?? '');
      }
      if (isWarn) this.onDiagnostic?.(`渲染层控制台：${message}`);
    });

    window.on('closed', () => {
      this.window = null;
    });

    // 诊断：窗口焦点变化。
    //
    // 加它的理由：用户报"必须点任务栏图标才能拖动宠物"，而点任务栏图标
    // 会让窗口获得焦点。所以必须把**焦点变化**与**收到 drag-start** 的
    // 时间序都记下来，才能判断根因 —— 否则只能靠猜（我已经猜错好几次了）。
    window.on('focus', () => this.diagnostic('窗口获得焦点'));
    // 窗口失焦：**保持穿透，只把渲染层的状态标记复位**。
    //
    // ---- 这条改过三次，把结论写清楚 ----
    //
    // 起因（第一次）：点一下别的窗口/桌面之后宠物就拖不动了。
    // 当时的推断是"失焦后 Windows 不再转发 mousemove，渲染层发现不了指针进入"，
    // 于是失焦时**主动关掉穿透**（`setInteractive(true)`）来自愈。
    //
    // ⚠️ 那次自愈造成了更大的问题（用户第二次反馈）：
    // **「点击浏览器中的页面按钮后就无法和宠物互动了」**。
    // 原因：关掉穿透之后，**整个 240×240 透明窗口都成了鼠标挡板** ——
    // 立绘只占中间一块，四周透明区照样吃掉点击。用户点浏览器按钮时，
    // 只要按钮落在宠物那块矩形里，点击就被宠物吞掉。
    //
    // 真正的根因其实是**渲染层的 `interactiveNow` 陈旧**：
    // 它以为自己是"可交互"，于是 `reportInteractive(true)` 被
    // `if (next === interactiveNow) return;` 挡掉，主进程收不到重新开启的请求 ——
    // 表现就是"点不动"，而**不需要**关穿透。
    //
    // 所以现在只做一件事：发复位消息（渲染层把标记置 null，下次上报必然发出）。
    // 穿透状态**保持不动**（默认就是开的），窗口继续对桌面/浏览器透明。
    window.on('blur', () => {
      this.diagnostic('窗口失去焦点 —— 复位渲染层交互标记（保持穿透，不挡底下）');
      this.window?.webContents.send('pet:interactive-reset');
    });

    this.window = window;
    this.registerIpc();
    return window;
  }

  /**
   * 读取渲染层的真实状态并汇报。
   *
   * 用 executeJavaScript 而不是靠用户开 DevTools：这台机器上没人能目视调试，
   * 必须让程序自己把答案写出来。返回的每一项都对应一个具体的失败假设。
   */
  private async probeRenderer(): Promise<void> {
    const window = this.window;
    if (!window || window.isDestroyed()) return;
    try {
      const report = (await window.webContents.executeJavaScript(
        `(() => {
          const pet = window.__pet;
          const canvas = document.getElementById('pet');
          const ctx = canvas && canvas.getContext('2d');
          let painted = null;
          if (ctx && canvas.width > 0) {
            try {
              const data = ctx.getImageData(0, 0, canvas.width, canvas.height).data;
              let opaque = 0;
              for (let i = 3; i < data.length; i += 4) if (data[i] > 8) opaque += 1;
              painted = { pixels: data.length / 4, opaque, ratio: +(opaque / (data.length / 4)).toFixed(4) };
            } catch (e) { painted = { error: String(e) }; }
          }
          return {
            href: location.href,
            readyState: document.readyState,
            hasHud: !!document.getElementById('hud'),
            hasDock: !!document.getElementById('dock'),
            hasCanvas: !!canvas,
            canvasSize: canvas ? [canvas.width, canvas.height] : null,
            petApiPresent: !!window.pet,
            petDebugPresent: !!(pet && pet.sprite),
            sprite: pet && pet.sprite ? pet.sprite.debug : null,
            errors: (window.__petErrors || []).slice(0, 5),
            painted,
          };
        })()`,
        true,
      )) as Record<string, unknown>;
      this.onDiagnostic?.(`渲染层自检：${JSON.stringify(report)}`);
    } catch (error) {
      this.onDiagnostic?.(`渲染层自检失败：${(error as Error).message}`);
    }
  }

  /**
   * 渲染层汇报的"立绘在窗口内的矩形"（CSS 像素，**已含旋转与平移**）。
   *
   * 用途见下面的 `startCursorWatch`：主进程靠它自己判断"指针是否在宠物身上"，
   * 不再依赖窗口焦点下的 `mousemove` 转发。
   */
  private artScreenRect: { x: number; y: number; width: number; height: number } | null = null;

  /**
   * **未旋转**时立绘在窗口内的矩形（渲染层上报，`reportArtRect` 的 `upright*`）。
   *
   * 用途：换窗口尺寸时当锚点（"立绘顶边到窗口顶边的距离"）。
   *
   * ⚠️ 为什么不能用 `artScreenRect`：它**含旋转**、且探头时尺寸被乘了 √2，
   * 拿它反推立绘在窗口里的位置会算出负数 —— 几何上不可能（立绘贴窗口顶），
   * 表现就是拖拽结束立绘往下跳 43px（见交接文档第七节·0）。
   */
  private uprightArtRect: { x: number; y: number; width: number; height: number } | null = null;

  /**
   * 立绘相对**窗口原点**的偏移（渲染层上报时算出来的）。
   *
   * 换窗口尺寸时用它做锚点："新窗口原点 = 当前窗口原点 + 这个偏移"。
   *
   * ⚠️ 为什么不用"立绘的屏幕绝对位置"：那个值只在**上报那一刻**有效，
   * 窗口一旦被拖动就失效，而渲染层在拖拽期间是**不上报**的。
   * 实测用它当锚点会让窗口跳回旧位置（横向跳过 423px、493px 两次）。
   * 相对偏移只描述"立绘在窗口内的位置"，正是换尺寸时要保住的东西。
   */
  private artOffsetInWindow: { x: number; y: number } | null = null;

  /**
   * 待应用的"恢复常态尺寸"。
   *
   * ## 为什么要**延迟**到下一次上报才应用
   *
   * 换尺寸的锚点是"立绘当前的屏幕位置"，而它由渲染层上报。
   * 拖拽过程中渲染层**不会**上报（它在忙着跟指针），所以那时手里的锚点是**陈旧**的。
   *
   * 实测踩过（2026-10-04 探针）：
   *
   *     拖拽末  窗口 921,265   立绘屏幕 y=259
   *     松手后  窗口 1344,216  立绘屏幕 y=216   ← 窗口跳回探头位置（x 差 423！）
   *
   * 原因就是用了拖拽前的旧锚点。
   *
   * 所以：先让渲染层"回正 + 重报"（`pet:peek { peeking:false }` 已经会触发
   * `reportArtRect`），**等那一份新坐标到了再换尺寸** —— 见 `pet:art-rect` 的处理。
   */
  private pendingNormalResize = false;

  /**
   * 退出探头后，等**回正动画彻底结束**再恢复常态尺寸。
   *
   * ## 为什么延时必须是"动画时长 + 余量"（这里错过一次）
   *
   * 原来写的是 **250ms**，理由（当时的想法）是"只要等渲染层把 `--peek-shift`
   * 清掉并重新上报就够，那个上报是同步发出的"。
   *
   * **但那个理由是错的**：立绘在窗口里的**相对位置**不只受 `--peek-shift` 影响，
   * 还受**旋转角**影响（旋转会把它绕画布中心甩出去）。所以只要换尺寸发生在
   * **旋转动画还没演完**的时候，锚点取到的就是"动画中途"的值 ——
   * 视觉上就是那段"向右下移动"。
   *
   * ⚠️ 而动画时长是 `--peek-rotate-ms` = **1000ms**，250ms 差了 4 倍。
   * 用户自己也定位到了：「我估计是最后拖拽出来后的自动归位导致的位置移动」。
   *
   * 用户拖拽通常比 1 秒快，所以"从退出探头那一刻起算"也偏早 ——
   * 见 `scheduleNormalResizeAfterPeekExit` 里的重新计时。
   */
  private scheduleNormalResizeAfterPeekExit(extraDelayMs = 0): void {
    if (this.normalResizeTimer) clearTimeout(this.normalResizeTimer);
    // 标脏：让 art-rect 处理器知道"现在这份偏移是按探头姿态采的，不能拿来做锚点"
    this.peekOffsetDirty = true;
    const wait =
      PetWindow.PEEK_TRANSITION_MS_MIRROR + PetWindow.NORMAL_RESIZE_MARGIN_MS + Math.max(0, extraDelayMs);
    this.normalResizeTimer = setTimeout(() => {
      /*
       * ⚠️ **拖拽还没结束就再等一轮**。
       *
       * 回正动画是在**拖拽开始**时启动的（`drag-start` 里 `straightenOnly`），
       * 而用户拖拽往往比 1 秒动画更快结束 —— 于是从"退出探头"起算的定时器
       * 可能仍在动画中途。拖拽期间窗口正被指针驱动、位置一直在变，
       * 这时候换尺寸必然对不上。
       */
      if (this.dragging) {
        this.scheduleNormalResizeAfterPeekExit(0);
        return;
      }
      this.normalResizeTimer = null;
      this.tryApplyNormalResize();
    }, wait);
  }

  /**
   * 探头位移是否还没从上报里消失（= 手里的偏移是"探头姿态"下的脏值）。
   *
   * 判据：静止/常态时立绘贴窗口原点，偏移应该是 `(0, 0)`；
   * 而探头时画布被 `--peek-shift` 推过，偏移会是 `(±43..57, 0)` 量级。
   * 所以纵向非零、或横向幅度大于 20，就认为还没回到常态。
   */
  private peekOffsetDirty = false;

  /**
   * 应用"恢复常态尺寸"。
   *
   * ## ⚠️ 曾经在这里加过"偏移必须接近 (0,0) 才应用"的守卫 —— **删了**
   *
   * 假设是"常态下立绘贴窗口原点 => 偏移应为 (0,0)，否则就是探头姿态的脏值"。
   * **这个假设是错的**。用户给的日志（2026-10-04）证明**常态偏移本来就是 (51,47)**：
   *
   *     art-rect 上报：偏移 {"x":51,"y":47} 立绘屏幕 659,271 窗口 608,224
   *     （659−608 = 51，271−224 = 47 —— 自洽）
   *
   * 与早先 HUD 截图吻合：`artBox 135x172 @ 120,134` + 画布 240
   * => 立绘左上角在画布内 `(53,48)`。也就是说**立绘并不是贴在窗口原点上的**，
   * 它周围本来就有约 50px 的留白（素材自带的手位）。
   *
   * 守卫的后果：把**每一份**上报都判成脏值 -> 尺寸永远不恢复 ->
   * 立绘一直留在探头那圈大窗口里。
   *
   * ## 为什么不再需要"脏值"概念
   *
   * 公式是 `新原点 = 旧原点 − 偏移`，而屏幕位置 = `原点 + 偏移`，
   * 所以**无论偏移是多少，屏幕位置都被保住**（数学恒等）。
   * 即使采到动画中途的偏移，也只是让"保住的位置"取在那个瞬间 ——
   * 不会有"多加一个偏移量"那种量级错误（那是符号写反造成的，已修）。
   */
  /**
   * 应用"恢复常态尺寸"。
   *
   * ## ⚠️ 现在由**渲染层的 `transitionend`** 触发（不再猜延时）
   *
   * 见 `scheduleNormalResizeAfterPeekExit` 的说明：换尺寸必须等旋转动画**真正结束**，
   * 否则手里的偏移是动画中途值，会把立绘挪走。渲染层在动画结束时发
   * `pet:peek-transition-end`，这里才换 —— 这是精确时刻，不靠猜。
   */
  /**
   * 应用"恢复常态尺寸" —— **只在旋转动画真的结束、且没在拖拽时**才做。
   *
   * 见 `pet:peek-transition-end` 的说明（用户自己定位到：「我是在它还没探头旋转
   * 结束时候拖拽才会漂移的」）。这里做最后一道把关：
   *
   *   · 还在拖 -> 等松手（`drag-end` 会再来调）；
   *   · 旋转没结束 -> 标记 pending，等 `transitionend` 通知。
   */
  private tryApplyNormalResize(): void {
    if (this.dragging) {
      this.pendingNormalResize = true;
      this.diagnostic('换尺寸：仍在拖拽 -> 推迟到松手之后');
      return;
    }
    if (!this.peekRotationDone) {
      this.pendingNormalResize = true;
      this.diagnostic('换尺寸：旋转动画未结束 -> 推迟到 transitionend');
      return;
    }
    this.peekOffsetDirty = false;
    this.pendingNormalResize = false;
    this.resizeKeepingArt(windowSizeFor(this.petScale, 'normal'));
  }

  /**
   * 旋转动画是否已经结束（由渲染层的 `pet:peek-transition-end` 置位）。
   *
   * 初值 `true`：启动时本来就是常态姿态，没有动画在跑。
   * 进入探头时置 `false`（动画开始），回到 0° 时由通知置回 `true`。
   */
  private peekRotationDone = true;

  private normalResizeTimer: NodeJS.Timeout | null = null;

  /**
   * 按**光标位置**决定穿透 —— 不依赖窗口是否前台。
   *
   * ---- 为什么必须换成这个（用户报的 bug） ----
   *
   * 用户报告：「将宠物拖动至边缘探头后，打开别的应用之后就不能点击宠物互动了」。
   *
   * 根因：`setIgnoreMouseEvents(true, { forward: true })` 的 mousemove 转发
   * **在窗口非前台时并不可靠**（Windows 行为）。而渲染层判断"指针进入交互区
   * → 关闭穿透"完全依赖那些转发的 mousemove —— 一旦不转发，
   * 窗口就**卡在穿透态**，看得见却点不到。
   *
   * 我一度用"失焦时主动关掉穿透"来自愈，但那会让整个透明窗口变成鼠标挡板
   * （用户报过"点了浏览器按钮后无法和宠物互动"，就是它）。
   * 所以这里换成主进程**轮询光标 + 自己算命中**：
   *   · 不需要 mousemove 转发 → 与焦点无关；
   *   · 只在指针真的落在立绘矩形内时才吃鼠标 → 不会挡住底下的应用。
   *
   * 轮询只在**拿到立绘矩形之后**才启动；没拿到就仍走渲染层汇报那条老路，
   * 避免两套机制同时改穿透状态（那样会互相打架）。
   */
  private startCursorWatch(): void {
    if (this.cursorWatch) return;
    // 40ms ≈ 25Hz：比人眼能感知的迟滞快，又不至于让主线程太忙
    this.cursorWatch = setInterval(() => {
      const window = this.window;
      const rect = this.artScreenRect;
      if (!window || window.isDestroyed() || !rect) return;
      // 拖拽中不抢决策权：那时渲染层强制保持可交互（见 reportInteractive 的守卫）
      if (this.dragging) return;
      const bounds = window.getBounds();
      const cursor = screen.getCursorScreenPoint();
      const pad = 4; // 给边缘一点宽容，免得贴边时抖动
      const inside =
        cursor.x >= bounds.x + rect.x - pad &&
        cursor.x <= bounds.x + rect.x + rect.width + pad &&
        cursor.y >= bounds.y + rect.y - pad &&
        cursor.y <= bounds.y + rect.y + rect.height + pad;
      this.setInteractive(inside);
    }, 40);
  }

  private cursorWatch: ReturnType<typeof setInterval> | null = null;

  /** 探头时气泡的**基线**平移（按气泡最大宽度估的，见 applyPeekLayout） */
  private peekBubbleShiftBase = 0;
  /** 当前已下发给渲染层的气泡平移（去重用，避免刷 IPC） */
  private bubbleShiftSent: number | null = null;
  /**
   * 气泡在屏幕上的实测矩形（由渲染层上报，CSS 像素）。
   *
   * 用户要求：「改成随文字变大，不设固定大小，只要长度高度不要超出屏幕即可」。
   *
   * 气泡现在是**按内容自适应**的，宽度事先不知道 —— 所以不能再只靠
   * "按最大宽度估算"。这里拿实测矩形把它钳进工作区，超了就往回推。
   */
  private bubbleScreenRect: { x: number; y: number; width: number; height: number } | null = null;

  /**
   * 按**实测**气泡矩形把它钳进工作区。
   *
   * 算法：在"基线平移"之上，再找一个额外偏移，使气泡完整落在工作区内；
   * 如果气泡比工作区还宽，就退化为"左对齐工作区"（至少左边界可见）。
   * 只在值真的变化时才下发（否则每次测量都刷一条 IPC）。
   */
  private setMeasuredBubbleRect(rect: {
    x: number;
    y: number;
    width: number;
    height: number;
  }): void {
    const window = this.window;
    if (!window || window.isDestroyed()) return;
    this.bubbleScreenRect = rect;
    if (!this.edgePeek) {
      // 非探头：气泡本来就随着窗口走，不需要额外偏移
      this.sendBubbleShift(0);
      return;
    }
    const bounds = window.getBounds();
    const display = screen.getDisplayNearestPoint({
      x: bounds.x + Math.round(bounds.width / 2),
      y: bounds.y + Math.round(bounds.height / 2),
    });
    const wa = display.workArea;
    const base = this.peekBubbleShiftBase;
    // 应用基线之后气泡的位置
    const left = rect.x + base;
    const right = left + rect.width;
    let extra = 0;
    if (left < wa.x) extra = wa.x - left;
    else if (right > wa.x + wa.width) extra = wa.x + wa.width - right;
    // 气泡比工作区还宽时，上面那两下会互相抵消 —— 退化为左对齐
    if (rect.width > wa.width) extra = wa.x - left;
    this.sendBubbleShift(base + Math.round(extra));
  }

  /** 下发气泡平移（去重：值没变就不发，避免刷屏） */
  private sendBubbleShift(shift: number): void {
    const window = this.window;
    if (!window || window.isDestroyed()) return;
    if (this.bubbleShiftSent === shift) return;
    this.bubbleShiftSent = shift;
    /*
     * ⚠️ 用**独立通道** `pet:bubble-shift`，不要复用 `pet:peek`。
     *
     * 我第一版复用了 `pet:peek`，结果那条消息把 `angle/offset` 都写成了 0 ——
     * 而 `pet:peek` 是**姿态**消息，渲染层收到就会把旋转设成 0：
     * 探头时立绘会**被"扶正"**（斜身效果消失）。这种"复用一条消息"的冲突
     * 在 `.pet-wrap` 的 transform 上也踩过一次（两处都写会互相覆盖）。
     */
    window.webContents.send('pet:bubble-shift', { shift });
    this.diagnostic?.(
      `气泡平移 -> ${shift}px（气泡 ${Math.round(this.bubbleScreenRect?.width ?? 0)}px 宽）`,
    );
  }

  /** 渲染层汇报"指针是否在交互区"，主进程据此切换穿透状态。 */
  private registerIpc(): void {
    // 用 ipcMain.on 接 sendSync：Electron 会把返回值回给渲染层。
    // 返回 undefined 即可 —— 这里不需要给渲染层任何数据，
    // 要的只是"主进程已经执行完 setIgnoreMouseEvents"这个**时序保证**。
    ipcMain.on('pet:interactive', (_event, isInteractive: boolean) => {
      // 已经有立绘矩形时，穿透由光标轮询独家决定 —— 忽略渲染层的汇报，
      // 否则两套机制会互相覆盖（表现为指针一动穿透就闪）。
      if (this.artScreenRect) return;
      this.setInteractive(Boolean(isInteractive));
    });

    /*
     * 渲染层通知：**旋转动画真正结束了**（`transitionend`）。
     *
     * ⚠️ 但这**只是两个前提之一**。换尺寸必须同时满足：
     *   ① 旋转动画已结束（本条通知）；
     *   ② **用户没在拖拽**（`this.dragging`）。
     *
     * 用户 2026-10-04 自己定位到的场景：「我是在它还没探头旋转结束时候拖拽才会漂移的」
     * —— 在动画中途开始拖、动画结束时手还没松。这时若照单换尺寸，
     * 窗口正被指针驱动，锚点必然是漂移值。
     *
     * 所以这里只**标记"旋转已完成"**，不强行换。真正换尺寸由下面两条任一触发：
     *   · 拖拽结束（`drag-end`）时若"旋转已完成 + 有 pending"，立刻换；
     *   · 否则由 `scheduleNormalResizeAfterPeekExit` 的兜底计时器处理。
     */
    ipcMain.on('pet:peek-transition-end', () => {
      this.peekRotationDone = true;
      this.diagnostic(
        `旋转动画结束（渲染层通知）；拖拽中=${this.dragging}，` +
          `待换尺寸=${this.pendingNormalResize || this.peekOffsetDirty}，` +
          `锚点偏移 ${this.artOffsetInWindow ? JSON.stringify(this.artOffsetInWindow) : 'null'}`,
      );
      if (this.dragging) return; // 等松手
      if (!this.pendingNormalResize && !this.peekOffsetDirty) return;
      this.pendingNormalResize = false;
      this.tryApplyNormalResize();
    });

    // 渲染层汇报"立绘在窗口内的矩形"（含旋转与平移）-> 启动光标轮询
    ipcMain.on(
      'pet:art-rect',
      (
        _event,
        payload: {
          x?: unknown;
          y?: unknown;
          width?: unknown;
          height?: unknown;
          uprightX?: unknown;
          uprightY?: unknown;
          uprightWidth?: unknown;
          uprightHeight?: unknown;
          /** 立绘左边相对**窗口原点**的偏移（视口坐标相减，同一套坐标系） */
          artOffsetX?: unknown;
          /** 立绘上边相对窗口原点的偏移 */
          artOffsetY?: unknown;
          /** 当前旋转角（度）—— 判断"这份几何是不是静止态的" */
          artRotationDeg?: unknown;
          /** 当前探头位移（`--peek-shift-x`）—— 静止判据还要看它 */
          artPeekShiftX?: unknown;
          /** 旋转中心（`--art-origin`，相对画布）—— 吸附算旋转外接框要用 */
        },
      ) => {
        const x = Number(payload?.x);
        const y = Number(payload?.y);
        const width = Number(payload?.width);
        const height = Number(payload?.height);
        if (![x, y, width, height].every((v) => Number.isFinite(v)) || width < 8 || height < 8) {
          return;
        }
        this.artScreenRect = { x, y, width, height };

        /*
         * **未旋转**的立绘矩形 —— 换窗口尺寸时的锚点用它。
         *
         * ⚠️ 不能用上面那个 `artScreenRect`：它含旋转、尺寸还乘过 √2，
         * 拿它反推"立绘顶边在窗口里的位置"会算出负数（几何上不可能），
         * 表现就是拖拽结束立绘往下跳 43px（2026-10-03 交接文档第七节·0）。
         */
        const ux = Number(payload?.uprightX);
        const uy = Number(payload?.uprightY);
        const uw = Number(payload?.uprightWidth);
        const uh = Number(payload?.uprightHeight);
        this.uprightArtRect =
          [ux, uy, uw, uh].every((v) => Number.isFinite(v)) && uw >= 8 && uh >= 8
            ? { x: ux, y: uy, width: uw, height: uh }
            : null;

        /*
         * 立绘相对**窗口原点**的偏移（= 上报那一刻的值）。
         *
         * ## ⚠️ 直接用渲染层给的窗口内偏移，**不要再做屏幕坐标换算**（踩过）
         *
         * 旧代码：渲染层报 `window.screenX + 视口坐标`（屏幕绝对位置），
         * 这里再 `asl - win.x` 减回去。看着对称，但 `cx/cy` 来自
         * `getBoundingClientRect()`（**视口**坐标），和 `window.screenX`（屏幕坐标）
         * 相加会引入偏差。
         *
         * 实测后果（日志原文）：
         *
         *     恢复尺寸 -> 窗口 1189,477 387x388 => 1189,477 250x335
         *                 | 立绘屏幕 1401,446 -> 1401,446 | 偏移 {"x":212,"y":-31}
         *                 偏移 x=212，真实值约 23   ← 混进了一次屏幕坐标
         *
         * `resizeKeepingArt` 用"新原点 = 当前原点 − 偏移"算，于是**窗口被搬走 212px**
         * —— 用户看到的就是「探头结束会闪现」。
         *
         * 现在渲染层直接报**窗口内偏移**（纯视口坐标相减，同一套坐标系），
         * 这里原样收下即可。
         */
        const aox = Number(payload?.artOffsetX);
        const aoy = Number(payload?.artOffsetY);
        /*
         * ⚠️ **数值健全性检查**：偏移必须是"立绘在窗口内"的合理量。
         *
         * 立绘在窗口里最多偏出一点点（画布比窗口小时可能为负），实测量级是
         * `x 0..45`、`y -35..20`。一旦超出这个量级，说明上报链路出了错
         * （2026-10-04 就报出过 `x:212` —— 混进了屏幕坐标），
         * 而 `resizeKeepingArt` 会**照单全收**地把窗口搬走同样多。
         *
         * 所以这里直接丢弃不合理值：宁可退回"原点不动"（那至少不位移），
         * 也不要用一个离谱的锚点去搬窗口。
         */
        const sane =
          Number.isFinite(aox) && Number.isFinite(aoy) && Math.abs(aox) <= 120 && Math.abs(aoy) <= 120;
        if (!sane && (Number.isFinite(aox) || Number.isFinite(aoy))) {
          this.diagnostic(
            `art-rect 上报的偏移不合理（${aox},${aoy}）-> **忽略**，保留上一份 ${this.artOffsetInWindow ? JSON.stringify(this.artOffsetInWindow) : 'null'}`,
          );
          return;
        }
        this.artOffsetInWindow =
          Number.isFinite(aox) && Number.isFinite(aoy)
            ? { x: Math.round(aox), y: Math.round(aoy) }
            : null;

        /*
         * ## 只在**真正的静止态**刷新"吸附锚点缓存"（2026-10-04 修）
         *
         * 判据要看**旋转角**，不能只看 `!edgePeek`：
         *
         *   · 探头稳态时 `edgePeek` 已经是 true —— 所以旧判据下缓存**只在进探头之前**
         *     写过一次，之后再也不更新，于是左右两侧取到了不同时机的那份值
         *     （用户报「左右两边不平均」就是这么来的）；
         *   · 旋转期间实测值会一路漂（日志：`-30 → -2 → 8 → 27 → 29`）。
         *
         * 只有"没在拖 + 没在探头 + **旋转角为 0**"时那份才是稳定的常量。
         *
         * ⚠️ 用 `uprightX`（渲染层给的**未旋转**姿态值），不是 `artOffsetInWindow.x`
         * ——后者是当前姿态的，会随着旋转变。
         */
        const rotDeg = Number(payload?.artRotationDeg);
        const peekShift = Number(payload?.artPeekShiftX);
        /*
         * ⚠️ 判据必须**同时**满足：没在拖、没在探头、**旋转 0°**、**探头位移 0**。
         *
         * 只看前三个会漏掉一个状态：
         *
         *     退出边缘探头（开始拖拽，先拉直）      <- 只拉直，edgePeek 仍为 true
         *     0,320 386x388 ... -> left（当前探头=true）   <- 窗口还是探头尺寸
         *
         * 那一刻旋转已是 0°，但窗口仍是探头尺寸、位移也还在 —— 会把"半探头"的几何
         * 当常态缓存，吸附目标值于是漂移（实测缓存从 33 变成 38，
         * 而 38 = (画布宽 − 立绘宽)/2 正是被污染的标志）。
         */
        const atRest =
          !this.dragging &&
          !this.edgePeek &&
          !this.compact &&
          rotDeg === 0 &&
          Math.abs(peekShift) < 0.5 &&
          this.measuredArt.width > 0;
        if (atRest && Number.isFinite(ux)) {
          this.restArtInsetX = Math.round(ux);
          this.restArtWidth = this.measuredArt.width;
          // 纵向偏移一起缓存（光标吸附要算立绘中心；见 restArtInsetY 的说明）
          if (Number.isFinite(uy)) this.restArtInsetY = Math.round(uy);
        }
        /*
         * **通知外面"宠物动了"** —— 气泡要跟着走（见 `BubbleWindow.followAnchor`）。
         *
         * 为什么挂在这里：这条上报是**立绘几何变化最及时的信号**（拖拽/旋转/换尺寸
         * 都会触发）。原来气泡只在"要说话"时定位一次，于是显示期间宠物一动它就
         * 留在原地（探针实测：宠物中心从 1394 走到 14，气泡始终 1344,425）。
         *
         * 节流在 `followAnchor` 里做，这里不判断可见性。
         */
        this.onArtRect?.(this.artScreenAnchor);
        /*
         * 诊断：把每次上报的偏移都记下来。
         *
         * 排查"探头结束闪现"时就是靠这条定位的 —— 日志里出现过
         * `偏移 {"x":212,...}`（真实值约 23），一眼看出是屏幕坐标混了进来。
         * 正常情况下偏移应当稳定在 `(0..45, -35..20)` 这个量级。
         */
        this.diagnostic(
          `art-rect 上报：偏移 ${this.artOffsetInWindow ? JSON.stringify(this.artOffsetInWindow) : 'null'}`,
        );

        /*
         * 有"待恢复常态尺寸"的请求时，**现在**才应用 —— 锚点刚刷新过。
         *
         * ⚠️ 但在**离开探头之后**要格外小心：回正动画期间渲染层会连发几次上报，
         * 其中前几次的偏移仍是"探头姿态"的脏值（含 `--peek-shift`）。
         * `tryApplyNormalResize` 里做了守卫，脏值会被丢弃、等下一次上报。
         */
        if (this.pendingNormalResize || this.peekOffsetDirty) {
          this.pendingNormalResize = false;
          this.tryApplyNormalResize();
        }

        this.startCursorWatch();
      },
    );

    // 渲染层汇报"气泡在屏幕上的实测矩形" -> 按它把气泡钳进工作区
    ipcMain.on(
      'pet:bubble-rect',
      (_event, payload: { x?: unknown; y?: unknown; width?: unknown; height?: unknown }) => {
        const x = Number(payload?.x);
        const y = Number(payload?.y);
        const width = Number(payload?.width);
        const height = Number(payload?.height);
        // 宽度为 0 表示气泡隐藏中 —— 那时不需要钳制
        if (![x, y, width, height].every((v) => Number.isFinite(v)) || width < 4) return;
        this.setMeasuredBubbleRect({ x, y, width, height });
      },
    );

    // 显式拖拽：渲染层发屏幕坐标，主进程移动窗口。
    // 选择"渲染层驱动"而不是 app-region，理由见 beginDrag 的注释。
    ipcMain.on('pet:drag-start', (_event, point: { x: number; y: number }) => {
      const ok = this.beginDrag(Number(point?.x ?? 0), Number(point?.y ?? 0));
      this.diagnostic(
        `收到 drag-start @${Math.round(Number(point?.x ?? 0))},${Math.round(Number(point?.y ?? 0))}` +
          ` ok=${String(ok)} clickThrough=${String(this.clickThrough)}`,
      );
      if (!ok) return;

      /*
       * ⚠️ 一开始拖就**立即回正**。
       *
       * 用户要求：「改成探头时拖拽宠物时就立即让它回正就好了，
       * 松开还在屏幕边缘又自动探头就行了」。
       *
       * 原来的行为是"要等完全脱离屏幕边缘才转回来"，拖的过程中立绘一直歪着，
       * 手感很别扭。现在一按下就转回 0°，松手时再由 `drag-end` 的贴边判定
       * 决定要不要重新探头 —— 与用户描述的两步完全一致。
       *
       * ⚠️ 用 `straightenOnly = true`：**只回正、不动窗口尺寸**。
       * 拖拽中改窗口尺寸会打乱 `DragTracker` 记下的"指针与窗口的偏移"
       * （窗口一变，立绘相对窗口的位置就变了），拖起来会跳。
       * 窗口缩回常态交给松手后的 `applyLayout`。
       */
      if (this.edgePeek) this.exitEdgePeek('开始拖拽', true);

      // 拖拽期间保持不穿透：否则指针一旦滑出宠物本体，窗口就开始穿透，
      // 后续的 mousemove 会透给桌面、拖拽中途断掉。
      this.setInteractive(true);
    });

    ipcMain.on('pet:drag-move', (_event, point: { x: number; y: number }) => {
      if (!this.dragging) {
        // 诊断：只有第一次拖动有效时，这条能告诉我们"后续 move 有没有到"
        this.diagnostic('收到 drag-move 但当前不在拖拽状态（已忽略）');
        return;
      }
      this.updateDrag(Number(point?.x ?? 0), Number(point?.y ?? 0));
    });

    ipcMain.on('pet:drag-end', () => {
      if (!this.dragging) {
        this.diagnostic('收到 drag-end 但当前不在拖拽状态（已忽略）');
        return;
      }
      const bounds = this.window?.getBounds();
      this.diagnostic(
        `收到 drag-end，窗口最终位置 ${bounds ? `${bounds.x},${bounds.y} ${bounds.width}x${bounds.height}` : '未知'}` +
          `；旋转已完成=${this.peekRotationDone}，待换尺寸=${this.pendingNormalResize || this.peekOffsetDirty}`,
      );
      this.endDrag();

      /*
       * ⚠️ **松手后：若"旋转已完成 + 有 pending"，现在就是换尺寸的时刻。**
       *
       * 用户自己定位的场景：「我是在它还没探头旋转结束时候拖拽才会漂移的」
       * —— 动画中途开始拖、动画结束时手还没松。那种情况下
       * `pet:peek-transition-end` 会因"正在拖拽"而推迟（见那里的说明），
       * 于是换尺寸只能落到这里。
       *
       * 反之若旋转还没结束，就继续等 `transitionend`（`tryApplyNormalResize`
       * 内部会再判一次并保持 pending）。
       */
      if ((this.pendingNormalResize || this.peekOffsetDirty) && this.peekRotationDone) {
        this.diagnostic('drag-end：旋转已完成且有待换尺寸 -> 立即换回常态尺寸');
        this.tryApplyNormalResize();
      }

      /*
       * ⚠️ **拖拽结束时刷新一次立绘偏移 —— 这是最干净的时机。**
       *
       * 实测（2026-10-04 日志）：
       *
       *     进入探头（贴right）：窗口前 1336,356 244x328 | 偏移 null | 推算立绘屏幕 ?
       *     探头定位 -> 窗口 277x394@1352,323
       *
       * `偏移 null` 意味着 `applyPeekLayout` 拿不到锚点，只好退到
       * "窗口中心对齐"的兜底 —— 而立绘**贴窗口顶部、并不居中**，
       * 于是窗口 y 从 356 变成 323（**下移 33px**），立绘跟着往下。
       * 这就是用户一路报的"探头时向下移动"。
       *
       * 为什么是 null：偏移只在渲染层上报 `pet:art-rect` 时才赋值，
       * 而用户拖得快，拖拽期间可能**一次都没报过**。
       *
       * 拖拽刚结束这一刻：姿态是**常态**（没有 peek 位移）、窗口也刚停稳，
       * 所以这时上报的值最干净 —— 主动要一次，别等它自己来。
       */
      this.requestArtRect();

      // 拖到屏幕左/右边缘 -> 进入"边缘探头"（45° 倾斜窥视）。
      //
      // 依据参考应用的说明：「拖到屏幕左/右边缘后自动以 45° 探头姿态窥视；
      // 点击真实角色会拉直，约 5 秒后自动退回。」
      if (bounds && this.edgePeekEnabled && !this.compact) {
        const display = screen.getDisplayNearestPoint({ x: bounds.x, y: bounds.y });
        const wa = display.workArea;
        // 把判定的**输入**也记下来 —— 贴边判断出错时，光看"进/出探头"
        // 完全看不出为什么（实测卡在这个环节很久）。
        const leftGap = bounds.x - wa.x;
        const rightGap = wa.x + wa.width - (bounds.x + bounds.width);
        // 纵向 gap 也要（用户要求「屏幕上下也加个」）
        const topGap = bounds.y - wa.y;
        const bottomGap = wa.y + wa.height - (bounds.y + bounds.height);
        // 探头态的窗口是"旋转外接正方形 + 故意伸出屏幕"，但它的 left/right gap
        // 依然如实反映"宠物贴在屏幕哪一侧"（实测：rightGap = -108 仍判 right），
        // 所以**不需要先拉直**，直接按当前 bounds 判断即可。
        //
        // ⚠️ 判定必须**扣掉立绘之外那圈留白**，按立绘可见边缘算。
        // 窗口比立绘大一圈（旋转 45° 的几何差，见 windowSizeFor），
        // 按窗口算会让宠物离屏幕边还有几十像素就自己探头 ——
        // 用户为这个报过两次「还没到屏幕边缘就自动探头」。
        const artMarginX = this.currentArtMarginX();
        const artMarginY = this.currentArtMarginY();
        const side = sideAtEdge(
          bounds,
          wa,
          this.edgeSnapPx,
          artMarginX,
          artMarginY,
          // ⚠️ 传实测的"立绘左边到窗口原点"与立绘尺寸 —— 贴边判据要用**立绘真实边缘**，
          // 而不是"窗口 gap + margin"（后者在窗口越界时方向会反，左右不对称）。
          this.artOffsetInWindow,
          this.measuredArt.width > 0 ? this.measuredArt : null,
        );
        this.diagnostic(
          `贴边判定：bounds ${bounds.x},${bounds.y} ${bounds.width}x${bounds.height}；` +
            `工作区 ${wa.x},${wa.y} ${wa.width}x${wa.height}；` +
            `留白 横${artMarginX} 纵${artMarginY}；` +
            `立绘 gap 左=${leftGap + artMarginX} 右=${rightGap + artMarginX} ` +
            `上=${topGap + artMarginY} 下=${bottomGap + artMarginY}，` +
            `阈值=${this.edgeSnapPx} -> ${side ?? '不贴边'}（当前探头=${this.edgePeek}）`,
        );
        if (side) {
          /*
           * 诊断：记录**进入探头前的窗口原点**。
           *
           * 留着它是因为"进探头到底移动了多少"只能靠前后对比看出来 ——
           * 排查右侧探头"一次跳 199px"时就是靠这条 + `进入探头后` 两行定位的。
           */
          this.diagnostic(
            `即将进入探头：窗口 ${bounds.x},${bounds.y} ${bounds.width}x${bounds.height}`,
          );
          /*
           * 光标吸附（2026-10-16）：松手那一刻的光标位置就是"用户想让宠物
           * 待的高度/横向位置"，把它传给吸附 —— 立绘中心对齐光标，
           * 探头形态不再随抓取点变（抓头部 vs 抓腹部结果一致）。
           * 这是**唯一**传光标的调用点；其余重入路径保持"原点不动"。
           */
          this.enterEdgePeek(side, screen.getCursorScreenPoint());
        } else if (this.edgePeek) {
          /*
           * 拖离边缘：退出探头（回正 + 恢复常态尺寸）。
           *
           * ⚠️ 顺序很关键，踩过三次：
           *   1. `exitEdgePeek` 发 `pet:peek { peeking:false }` —— 渲染层清掉
           *      `--peek-shift` 并重新上报立绘位置；
           *   2. **等回正稳定**（下面的定时器），拿到**回正之后**的偏移；
           *   3. 再用那份偏移换尺寸。
           *
           * 之前是"用当前偏移立刻换"——那份偏移还是探头态的（实测 `y:-50`、
           * `(55,55)`），于是窗口被多推几十像素，立绘跟着跳。
           * 也试过"等下一次上报"，但下一次上报来的仍是回正前的。
           */
          this.scheduleNormalResizeAfterPeekExit();
          this.exitEdgePeek('离开边缘');
        }
        /*
         * ⚠️ **不要**在这里加"恢复常态尺寸"。
         *
         * 试过三次，每次都引入新的位移（-42 / +45 / -43px），原因是同一个：
         * **拿不到一个"换尺寸时立绘在窗口里到底在哪"的可信值**。
         *
         * 决定性数据（2026-10-04 探针）：
         *
         *     拖拽末   窗口 921,265   画布顶 259
         *     松手后   窗口 913,214   画布顶 214     ← 掉 45px
         *
         * 探头时渲染层给画布加了 `--peek-shift`（实测 −50），立绘被推走 50px；
         * 而拖拽过程中画布又在回正，等我们算的时候它已经只偏差 5px 了 ——
         * **锚点值本身误差就有 45px**，算式再精确也救不回来。
         *
         * 代价（已知、可接受）：从探头拖到屏幕中间松手后，窗口仍是探头那圈
         * 放大尺寸（240→277 宽），多出的透明区会吃点击。
         *
         * 真要修，得让渲染层在**偏移稳定之后**（回正动画结束）主动上报一次，
         * 再在那份上报里完成换尺寸 —— 而不是像现在这样在拖拽结束时抢着算。
         */
      }

      // ⚠️ **这里绝对不能强行 `setInteractive(false)`。**
      //
      // 这就是"只能拖第一次"的确切原因（实测定位）：
      // 拖拽结束时如果主进程单方面打开穿透，而渲染层的 `interactiveNow`
      // 仍然是 true（它以为窗口可交互），两边就**对不上了**：
      //
      //     用户第二次把指针移到宠物上 → 渲染层判定"仍在交互区" →
      //     因为 interactiveNow 已经是 true，reportInteractive 提前 return，
      //     **不再发送状态变化** → 主进程的穿透一直没人关掉 → 按下被丢。
      //
      // 穿透状态必须**只有一个决定者**：渲染层（它知道指针在哪）。
      // 主进程只管执行。所以这里什么都不做 —— 渲染层在 mouseup 里
      // 自己会调 reportInteractive() 给出正确的状态。
    });

    ipcMain.on('pet:quit', () => {
      // 交给 index.ts 统一处理退出流程
      this.window?.emit('pet:quit-request');
    });

    // 最小化。
    //
    // 为什么需要专门做一个入口：窗口是 `frame: false`（无边框），
    // 所以**系统没有提供最小化按钮** —— 用户根本没办法把宠物收起来。
    // 而任务栏按钮在 2026-10-12 之后**默认不存在了**（见 `skipTaskbar` 那段说明），
    // 于是"从任务栏恢复"这条路也没有了，最小化入口更加必要。
    // 无边框窗口的这类系统能力，必须由应用自己补上。
    ipcMain.on('pet:minimize', () => {
      const window = this.window;
      if (!window || window.isDestroyed()) return;
      this.diagnostic('收到最小化请求');
      window.minimize();
    });
  }

  /** 切换"指针是否在交互区"，主进程据此开关穿透。 */
  setInteractive(interactive: boolean): void {
    if (interactive === this.interactive) return;
    const previous = this.interactive;
    this.interactive = interactive;
    const window = this.window;
    if (!window || window.isDestroyed()) return;

    // 交互区：不穿透；非交互区：穿透且继续转发鼠标移动
    window.setIgnoreMouseEvents(!interactive, { forward: true });
    this.clickThrough = !interactive;

    // 诊断：穿透切换是"点不动宠物"这类问题的关键线索，留一条可排查的记录。
    // 只在真的变化时打（setInteractive 已经做了去重），不会刷屏。
    this.diagnostic?.(
      `穿透切换 clickThrough=${String(this.clickThrough)}（${previous ? '可交互' : '穿透'} -> ${
        interactive ? '可交互' : '穿透'
      }）`,
    );
  }

  /** 诊断输出通道（`onDiagnostic` 是 L47 已声明的属性，这里只是简写调用）。 */
  private diagnostic(line: string): void {
    this.onDiagnostic?.(line);
  }

  // ------------------------------------------------------------------ 拖拽

  /**
   * 显式拖拽窗口。
   *
   * 为什么不用 CSS 的 `-webkit-app-region: drag`：它在**透明 + 穿透**的窗口上不可靠。
   * app-region 依赖 Chromium 内部的拖拽区命中测试，而窗口带着 `WS_EX_TRANSPARENT`
   * （`setIgnoreMouseEvents(true)` 的效果）时命中测试会被跳过 —— 结果就是"抓不住"。
   * 这套组合的时序很难调稳，而显式移动窗口是确定的。
   *
   * 坐标算术抽在 `dragTracker.ts` 里（纯模块、可测试），这里只接上真实窗口。
   */
  beginDrag(screenX: number, screenY: number): boolean {
    const window = this.window;
    if (!window || window.isDestroyed()) return false;
    this.dragTracker.begin(screenX, screenY);
    return true;
  }

  /** 拖拽中：把窗口移到"指针相对起点移动了多少"的位置 */
  updateDrag(screenX: number, screenY: number): void {
    this.dragTracker.move(screenX, screenY);
  }

  endDrag(): void {
    this.dragTracker.end();
  }

  get dragging(): boolean {
    return this.dragTracker.dragging;
  }

  /**
   * 改宠物大小（菜单里的「大小」）。
   *
   * 窗口和画布**必须一起变**：只改画布会让窗口留一片空白，只改窗口会让立绘
   * 悬在中间一小块。所以这里按同一份尺寸算出窗口宽高，再让渲染层把画布也调过去。
   *
   * 锚点用**右下角**：宠物默认待在右下角，放大时自然向左上长，而不是把窗口
   * 推出屏幕。同时夹在 workArea 内，免得放大后有一部分跑到屏幕外。
   */
  setPetScale(scale: number): void {
    const window = this.window;
    if (!window || window.isDestroyed()) return;

    const clamped = clampScale(scale);
    this.petScale = clamped;
    this.applyLayout(`大小 ${clamped.toFixed(2)}x`);
  }

  get currentPetScale(): number {
    return this.petScale;
  }

  get isCompact(): boolean {
    return this.compact;
  }

  /**
   * 进入 / 退出紧凑模式（"别人全屏时趴着"）。
   *
   * 两件事一起做：
   *   ① 窗口层级切到 `screen-saver` —— 只有这个层级能浮在**全屏**应用之上。
   *      平时不能用它，因为它**也会盖住任务栏**，让用户点不到开始菜单；
   *   ② 窗口缩成只包住立绘，并通知渲染层隐藏输入卡片。
   *
   * 退出时反向恢复。
   */
  setCompact(compact: boolean, reason = ''): void {
    const window = this.window;
    if (!window || window.isDestroyed()) return;
    if (this.compact === compact) return;

    // ① 进全屏前先记住状态 —— 用户要求「取消全屏时要能变成之前的位置和状态」。
    if (compact) {
      const b = window.getBounds();
      this.preCompact = {
        x: b.x,
        y: b.y,
        peeking: this.edgePeek,
        peekSide: this.edgePeekSide,
      };
      this.diagnostic(
        `进入全屏模式（${reason || '检测到全屏'}），记住原位置 ${b.x},${b.y}，` +
          `原状态 ${this.edgePeek ? `探头·贴${PEEK_SIDE_TEXT[this.edgePeekSide]}边` : '常态'}`,
      );
    }

    this.compact = compact;

    // ② 层级
    const level: 'normal' | 'screen-saver' = compact ? 'screen-saver' : this.topLevel;
    window.setAlwaysOnTop(true, level);
    // 全屏应用会创建自己的全屏空间，必须同时声明"在所有工作区可见"
    window.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });

    // ③ 布局
    if (compact) {
      // 全屏：固定成"普通状态下贴到左右边缘的探头样子"（用户要求）。
      //
      // 走**和手动贴边完全相同的定位函数**（`applyPeekLayout`），不另写一套 ——
      // 否则角度/露出宽度/留白迟早不一致（这个项目因为"两处各算一次"踩过几次）。
      //
      // 但**不置 `edgePeek = true`**（不算"用户拖出来的探头"），
      // 否则 drag-end 的贴边判定会把全屏误当成探头态。
      const side = this.pickNearestSide();
      this.applyPeekLayout(side);
      window.webContents.send('pet:compact', { compact: true, side });
    } else {
      // 退出全屏：先**回正姿态**，再恢复基准布局，最后回到记住的坐标。
      //
      // ⚠️ 必须显式发这条 `pet:peek` 回正消息。
      //
      // 我原来以为 `applyLayout` 会顺手清掉旋转 —— **它不会**。
      // 渲染层的旋转是主进程通过 `pet:peek` 下发的（`app.js` 里写 canvas 的
      // `style.transform`），没人发"peeking:false"它就一直斜着。
      // 用户报的「切换回来之后探头旋转姿态没回正」就是这个。
      window.webContents.send('pet:peek', {
        peeking: false,
        side: this.peekSide,
        angle: 0,
        offsetX: 0,
        offsetY: 0,
      });
      this.applyLayout('退出紧凑模式');
      const saved = this.preCompact;
      this.preCompact = null;
      if (saved) {
        const want = windowSizeFor(this.petScale, 'normal');
        window.setBounds({
          x: Math.round(saved.x),
          y: Math.round(saved.y),
          width: want.width,
          height: want.height,
        });
        this.diagnostic(
          `退出全屏，恢复位置 ${Math.round(saved.x)},${Math.round(saved.y)}` +
            `（原状态 ${saved.peeking ? `探头·贴${PEEK_SIDE_TEXT[saved.peekSide]}边` : '常态'}，` +
            `按当前设置不自动恢复探头）`,
        );
      }
      window.webContents.send('pet:compact', { compact: false });
      this.onLayoutChanged?.('normal');
    }
  }

  /** 按宠物当前水平位置选**最近的**一侧（左/右） */
  private pickNearestSide(): PeekSide {
    const window = this.window;
    if (!window || window.isDestroyed()) return 'right';
    const b = window.getBounds();
    const { workArea } = screen.getDisplayNearestPoint({ x: b.x, y: b.y });
    const centerX = b.x + b.width / 2;
    return centerX < workArea.x + workArea.width / 2 ? 'left' : 'right';
  }

  /** 让窗口临时整体可交互（打开设置面板时用）。 */
  setModal(modal: boolean): void {
    this.setAlwaysOnTop(modal);
    if (modal) this.setInteractive(true);
  }

  setAlwaysOnTop(onTop: boolean): void {
    const window = this.window;
    if (!window || window.isDestroyed()) return;
    // 用与创建时相同的层级。早先这里写死了 'screen-saver'，
    // 于是"临时取消置顶再恢复"会把窗口抬到能盖住任务栏的层级 —— 与默认行为不一致。
    window.setAlwaysOnTop(onTop, this.topLevel);
  }

  /**
   * 隐藏前记住窗口位置，显示时还原。
   *
   * 为什么需要：`hide()`/`show()` 之间窗口位置理论上不变，但如果用户在隐藏期间
   * 通过别的路径（改缩放、切全屏）触发了重排，位置可能跑掉。记一份更稳，
   * 而且**这是"找回来"体验的一部分** —— 恢复后应该出现在原来的地方。
   */
  private hiddenAt: { x: number; y: number } | null = null;

  show(): void {
    const window = this.window;
    if (!window || window.isDestroyed()) return;
    if (window.isMinimized()) window.restore();
    window.showInactive();
    // 恢复隐藏前的位置（见 hiddenAt 的说明）
    if (this.hiddenAt) {
      const size = window.getBounds();
      window.setBounds({
        x: Math.round(this.hiddenAt.x),
        y: Math.round(this.hiddenAt.y),
        width: size.width,
        height: size.height,
      });
      this.hiddenAt = null;
    }
    // 显示后重新确认任务栏按钮状态（见 `skipTaskbar` 与 `hide()` 的说明）
    window.setSkipTaskbar(this.skipTaskbarWanted());
  }

  /**
   * 用户到底要不要任务栏按钮。
   *
   * 只有**显式设了** `PET_TASKBAR=1` 才要 —— 那是"万一找不到出口"的退路开关
   * （见构造函数里 `skipTaskbar` 那段说明）。
   *
   * ⚠️ 抽成方法是因为它现在有**三个调用点**（构造、`show()`、`hide()`），
   * 三处各写一遍 `process.env.PET_TASKBAR !== '1'` 迟早会漂移出不一致 ——
   * 而这个项目的 `skipTaskbar` 恰恰是"宠物找不回来"那类事故的来源。
   */
  private skipTaskbarWanted(): boolean {
    return process.env.PET_TASKBAR !== '1';
  }

  /**
   * 隐藏宠物窗口。
   *
   * ---- ⚠️ 这里是"宠物消失后找不回来"的根因，改动都记下来 ----
   *
   * 用户报告：「点击隐藏宠物，宠物就消失了，任务栏也没有程序的影子」。
   *
   * `BrowserWindow.hide()` 在 Windows 上会**连同任务栏按钮一起移除**。
   * 而这个项目的托盘图标在 Windows 11 上会被收进溢出区（`^` 里），
   * 于是"隐藏"实际上变成了**没有任何入口能把它弄回来** ——
   * 只剩 `Ctrl+Alt+P` 这个全局快捷键，而用户不一定记得。
   *
   * 所以当时：隐藏**之前**显式保留任务栏按钮（`setSkipTaskbar(false)`）。
   *
   * ---- 2026-10-12 改：不再强行保留 ----
   *
   * 用户要求「把任务栏那个图标隐藏吧，现在有托盘了」。而"隐藏时硬留一个按钮"
   * 正好是用户要去掉的东西 —— 留着的话，「隐藏宠物」看起来**没生效**
   * （宠物没了，任务栏里却还有它）。
   *
   * 找回入口现在是这几条（都不依赖任务栏）：
   *   · **托盘图标 + 菜单**（用户已确认正常显示）
   *   · `Ctrl+Alt+P`（SHORTCUTS 里登记的显示/隐藏）
   *   · 设置窗口的「显示宠物」按钮
   *   · 兜底开关 `PET_TASKBAR=1`（把任务栏按钮要回来，不用改代码）
   */
  hide(): void {
    const window = this.window;
    if (!window || window.isDestroyed()) return;
    // 记下位置，恢复时回到原处（见 hiddenAt 的说明）
    const bounds = window.getBounds();
    this.hiddenAt = { x: bounds.x, y: bounds.y };
    window.setSkipTaskbar(this.skipTaskbarWanted());
    window.hide();
  }

  toggleVisible(): void {
    const window = this.window;
    if (!window || window.isDestroyed()) return;
    if (window.isVisible()) this.hide();
    else this.show();
  }

  get visible(): boolean {
    return this.window?.isVisible() ?? false;
  }

  /**
   * 立绘几何变化时的回调（由 `index.ts` 接到气泡的跟随逻辑上）。
   *
   * 用回调而不是让 PetWindow 直接持有 BubbleWindow：气泡是**可选的**独立窗口，
   * 两者耦合会让"只起宠物、不起气泡"的场景（测试/全屏）多出一堆判空。
   */
  onArtRect:
    | ((anchor: { centerX: number; topY: number; bottomY: number; leftX: number; rightX: number; visibleLeftX?: number; visibleRightX?: number; peeking: boolean } | null) => void)
    | null = null;

  /**
   * 立绘在**屏幕坐标**里的位置，供独立的气泡窗口定位用。
   *
   * ## ⚠️ 用**未旋转**的矩形（`uprightArtRect`），不要用含旋转的（2026-10-04）
   *
   * `artScreenRect` 是**旋转后的外接框**（45° 时宽高都乘过 ≈√2），它的 `y` 是
   * "旋转四角里最上面那个点"。而**立绘的头并不在那个点**（头在旋转中心附近）。
   *
   * 拿它当锚点的后果（探针实测）：
   *
   *     常态      气泡底边离立绘顶边   4px    ✅
   *     探头(右)  气泡底边离立绘顶边 **113px** ❌ 气泡被顶到屏幕上方
   *
   * 用户报的「将探头时的气泡位置改低一些」就是这 113px —— 气泡看着高得离谱。
   *
   * 未旋转矩形的 `y` 是立绘真实的顶边（姿态无关），拿它当锚点，
   * 探头时气泡只会因为 `PEEK_EXTRA_DROP` 而略微下移，不会飞上去。
   *
   * `centerX` 仍可用旋转外接框的中心 —— 中心在旋转下不变，两者等价；
   * 但为了一致性也统一走未旋转那份（拿不到时才退回）。
   */
  get artScreenAnchor(): { centerX: number; topY: number; bottomY: number; leftX: number; rightX: number; visibleLeftX?: number; visibleRightX?: number; peeking: boolean } | null {
    const window = this.window;
    if (!window || window.isDestroyed()) return null;
    const bounds = window.getBounds();
    // `peeking` 让气泡知道要不要额外往下挪（见 bubbleWindow 的 PEEK_EXTRA_DROP）。
    // 探头 = 用户拖出来的边缘探头，或"别人全屏"触发的紧凑形态 —— 两者姿态一致。
    const peeking = this.edgePeek || this.compact;

    // 优先：**未旋转**的实测矩形（姿态无关，头在哪就是哪）
    const up = this.uprightArtRect;
    if (up && up.width > 0 && up.height > 0) {
      return {
        centerX: bounds.x + up.x + up.width / 2,
        topY: bounds.y + up.y,
        // 底边/左右边界同样用未旋转的 —— 上方放不下时气泡要改放旁边或下方
        bottomY: bounds.y + up.y + up.height,
        leftX: bounds.x + up.x,
        rightX: bounds.x + up.x + up.width,
        // 可见外接框（含旋转）—— 水平定位用它，见 bubbleWindow 的 visibleLeftX 说明
        visibleLeftX: this.artScreenRect ? bounds.x + this.artScreenRect.x : undefined,
        visibleRightX: this.artScreenRect ? bounds.x + this.artScreenRect.x + this.artScreenRect.width : undefined,
        peeking,
      };
    }

    // 退回：含旋转的外接框（旧行为，位置会偏高，但至少能用）
    if (this.artScreenRect) {
      return {
        centerX: bounds.x + this.artScreenRect.x + this.artScreenRect.width / 2,
        topY: bounds.y + this.artScreenRect.y,
        bottomY: bounds.y + this.artScreenRect.y + this.artScreenRect.height,
        leftX: bounds.x + this.artScreenRect.x,
        rightX: bounds.x + this.artScreenRect.x + this.artScreenRect.width,
        peeking,
      };
    }
    if (this.measuredArt.width <= 0 || this.measuredArt.height <= 0) return null;
    // 退化估算：立绘在画布内居中，画布与窗口左上角对齐
    const winSize = windowSizeFor(this.petScale, 'normal');
    const canvasW = Math.min(winSize.width, petSizeFor(this.petScale));
    const canvasH = Math.min(winSize.height, petSizeFor(this.petScale));
    return {
      centerX: bounds.x + canvasW / 2,
      topY: bounds.y + (canvasH - this.measuredArt.height) / 2,
      bottomY: bounds.y + (canvasH + this.measuredArt.height) / 2,
      leftX: bounds.x + (canvasW - this.measuredArt.width) / 2,
      rightX: bounds.x + (canvasW + this.measuredArt.width) / 2,
      peeking,
    };
  }

  get browserWindow(): BrowserWindow | null {
    return this.window;
  }

  /** 主进程 -> 渲染层：转发一条协议消息 */
  /**
   * 在宠物窗口里跑一段 JS 并取回结果（**给设置页的麦克风枚举用**）。
   *
   * ## ⚠️ 为什么这件事必须由宠物窗口做
   *
   * `navigator.mediaDevices.enumerateDevices()` 的 `label` **需要麦克风权限**
   * 才会填上。而权限是**按窗口**给的 —— 麦克风只在宠物窗口开过，
   * 设置页自己枚举拿到的是一堆空名字（用户根本没法选）。
   *
   * ## 为什么不直接暴露 `webContents`
   *
   * 那会让调用方拿到整个 `BrowserWindow`（能加载任意 URL、能开 DevTools…），
   * 而这里只需要"跑一段表达式、拿回一个可序列化的值"。收窄接口是刻意的。
   *
   * 返回 `null` 表示窗口不可用或执行失败 —— 调用方按"枚举不到设备"处理，
   * 不抛异常（枚举失败不该让设置页崩）。
   */
  async evaluate<T = unknown>(expression: string): Promise<T | null> {
    const window = this.window;
    if (!window || window.isDestroyed()) return null;
    try {
      return (await window.webContents.executeJavaScript(expression)) as T;
    } catch {
      return null;
    }
  }

  sendToRenderer(channel: string, payload: unknown): void {
    const window = this.window;
    if (!window || window.isDestroyed()) return;
    window.webContents.send(channel, payload);
  }

  get clickThroughEnabled(): boolean {
    return this.clickThrough;
  }
}
