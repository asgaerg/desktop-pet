/**
 * 文字弹窗（气泡）—— **独立窗口**，浮在宠物上方。
 *
 * ---- 为什么必须独立成一个窗口（这段是被实测逼出来的） ----
 *
 * 用户要求「可以加高宠物窗口」来给气泡腾地方。做完发现：**加高不解决问题**。
 * 实测数据（窗口 240x447 vs 240x240）：
 *   · 立绘头顶到窗口顶的距离**恒为 34px**，跟窗口多高无关；
 *   · 原因：画布贴窗口左上角，`--art-shift-y` 只在**画布内**做居中，
 *     多出来的高度只能落在画布下方。
 *
 * 想在宠物窗口内增加"头顶以上的空间"只有两条路，都被否掉了：
 *   ① 画布/立绘在窗口里下移 —— 等于宠物在屏幕上更靠下，用户实测否掉；
 *   ② 把窗口整体上移 —— 宠物本就贴工作区下沿，上移受屏幕顶部限制。
 *
 * 所以改成独立窗口：它的位置只受**屏幕工作区**约束，
 * 与宠物窗口的宽高彻底解耦 —— 长回复也能完整显示在宠物头顶之上。
 *
 * ---- 定位规则 ----
 *
 *   · 水平：以宠物**立绘中心**为基准居中；
 *   · 垂直：**底边贴宠物立绘头顶**（用户要求：「将弹窗底部设置为始终在宠物头上」）；
 *   · 整体钳进工作区：上方实在放不下时往下压，左右越界时平移。
 *
 * 窗口本身给一个够大的固定尺寸，气泡在窗口内**底边贴底**、水平居中 ——
 * 这样气泡的高度变化不影响锚点：只要把窗口底边对准头顶即可。
 */

import { BrowserWindow, screen } from 'electron';
import { existsSync } from 'node:fs';
import {
  BUBBLE_PAGE_FILENAME,
  BUBBLE_PRELOAD_FILENAME,
  resolvePreload,
  resolveRendererPage,
  rendererUrl,
} from './assets.js';

/** 气泡窗口的尺寸：只受屏幕限制，给足余量（不够时再按工作区收紧） */
const BUBBLE_MAX_WIDTH = 520;
/*
 * 窗口高度上限。
 *
 * ## ⚠️ 这个数不能太小（2026-10-05 实测教训）
 *
 * 原来是 360。后果：气泡**被视口高度钳住** —— `.bubble` 是 `bottom: 0` +
 * `overflow: auto`，视口（窗口内容区）不够高时它的高度就停在视口高度上，
 * 而窗口高度又由"气泡上报的高度"决定 → **同一个闭环**（宽度那边也踩过：
 * 窗口窄 → 内容换行 → 量出的宽度也小 → 窗口更窄）。
 *
 * 实测现场（316 字长文）：
 *
 *     viewport      528x328     ← 窗口内容区
 *     气泡自然高度  320         ← 摘掉 max-height 也还是 320，被视口挡住
 *     内容真实需要  326         ← scrollHeight
 *     ⇒ 差 6px，最后一行被切、滚动条常驻
 *
 * 放开到 620 之后窗口有富余，内容能撑到自然高度，`scrollHeight == clientHeight`。
 *
 * 为什么是 620：屏幕工作区 816 高，留给"宠物立绘本身"约 200px 之后
 * 还剩 600 上下；取 620 让极长回复也能完整显示，而窗口本身是透明+穿透的，
 * 高一点不挡视线。真超过这个高度才轮到滚动条出场。
 */
const BUBBLE_MAX_HEIGHT = 620;
/**
 * 气泡与宠物头顶之间留的空隙（常态）。
 * 🔴 默认值 —— 实际值在**设置**里（设置 → 桌宠 → 气泡上方间距，2026-10-18），
 * 启动时由 index.ts 注入 `setPlacementTuning()`。
 */
const GAP_ABOVE_HEAD = 6;

/**
 * 探头时气泡要**再往下挪**多少。
 *
 * 用户要求：「把这个探头时候的气泡降低一些」。
 *
 * 为什么探头要单独降低：探头时立绘旋转 45°，**头歪向屏幕里侧**，
 * 视觉上的"头顶"比常态低一截。气泡还按常态的头顶位置摆，就显得悬在半空、
 * 离宠物很远。往下压一点才像贴在头上。
 */
const PEEK_EXTRA_DROP = 26;

/*
 * `MIN_GAP_ABOVE_HEAD`（原"至少离头顶 18px"）**已删除**（2026-10-04）。
 *
 * 它的用途是"上方放不下时，宁可压住宠物上半身也不要贴着糊在头上"。但新的定位逻辑
 * 改成**四方向候选、挑第一个完整可行的**（上方 -> 右 -> 左 -> 下方），
 * 根本不会走到"压在宠物身上"那条路 —— 那个常量就没有意义了。
 *
 * 保留这段说明是为了防止有人看到它在 git 历史里又加回来。
 */

/**
 * 气泡放在宠物**旁边**时，与立绘的水平间距（像素）。
 *
 * 用于"宠物贴屏幕顶、上方放不下"的情形 —— 那时改放旁边（见 `place()`）。
 * 取 10 是因为立绘的实测矩形含一圈阴影/描边，视觉上留 10px 就已经分得开。
 */
/**
 * 气泡放在宠物**旁边**时与立绘的水平间距。🔴 实际值在设置里（同上）。
 */
const SIDE_GAP = 10;

/** 核心框收缩的默认比例（%）与硬上限 —— 同样被设置里的「立绘贴合度」覆盖 */
const CORE_SHRINK_DEFAULT_PCT = 25;
const CORE_SHRINK_X_CAP = 80;
const CORE_SHRINK_Y_CAP = 60;

/*
 * `FIRST_BUBBLE_DELAY_MS`（首条气泡延迟 1 秒显示）**已删除**（2026-10-04）。
 *
 * 当时是想验证"第一条气泡位置不对"是否因为"窗口还没就绪就先画了内容"。
 * **实测证明不是** —— 真根因是定位时用的是占位尺寸（520x360）而不是实测尺寸，
 * 所以窗口先出现在错位置、约 100ms 后才跳到位。修法见 `show()` 里那段
 * "尺寸还没量到时不要显示"。
 *
 * 延迟既然没解决问题，就不该留着（它会让第一条气泡白白慢 1 秒）。
 */

/**
 * 等实测尺寸的**超时兜底**（毫秒）。
 *
 * 正常情况下渲染层几十毫秒就报回实测尺寸；这个上限只是防止「页面出错导致气泡永远
 * 不显示」—— 超时后按占位尺寸定位并显示（位置可能不准，但至少可见）。
 */
const SIZE_WAIT_TIMEOUT_MS = 600;

/** 气泡左边相对立绘中心的偏移（正数 = 往右一点，避免盖住立绘正中） */
const LEFT_BIAS = 26;

export interface BubbleWindowOptions {
  distDir: string;
  devMode: boolean;
}

/** 宠物立绘在屏幕上的位置（用于把气泡钉在它头顶） */
export interface PetAnchor {
  /** 立绘中心 x（屏幕坐标） */
  centerX: number;
  /** 立绘顶边 y（屏幕坐标） */
  topY: number;
  /**
   * 立绘**底边** y（屏幕坐标）。
   *
   * 用途：宠物贴屏幕顶时上方放不下气泡，需要改放到别处（见 `place()`）。
   * 不传时退化为"只按顶边算"（旧行为）。
   */
  bottomY?: number;
  /** 立绘**左边** x（屏幕坐标）。旁边放气泡时要用 */
  leftX?: number;
  /** 立绘**右边** x（屏幕坐标） */
  rightX?: number;
  /**
   * 立绘**可见外接框**的左/右边界（**含旋转**）。
   *
   * ## ⚠️ 为什么和 `leftX/rightX` 分开（2026-10-04）
   *
   * 探头时立绘旋转 45°，两个矩形差别很大：
   *
   *   · `upright`（未旋转）：175x221，是"立绘自己的框" —— **竖直**位置准
   *     （头在哪就在哪），但**水平**边界不是视觉上的左右边；
   *   · `visible`（旋转外接框）：280x280 —— **水平**边界才是视觉上的左右边，
   *     但它的 `y` 偏高（四角里最上面那个点，头并不在那）。
   *
   * 所以：**竖直用 upright，水平用 visible**。混用会让"旁边"的位置算错。
   */
  visibleLeftX?: number;
  /** 立绘可见外接框的右边界（含旋转） */
  visibleRightX?: number;
  /**
   * 宠物当前是否处于**探头**姿态。
   *
   * 探头时立绘旋转 45°、头歪向屏幕里侧，视觉上的头顶比常态低一截，
   * 所以气泡要额外往下挪（用户要求「把这个探头时候的气泡降低一些」）。
   */
  peeking?: boolean;
}

export class BubbleWindow {
  private window: BrowserWindow | null = null;

  /**
   * 气泡的实际宽度/高度（由渲染层上报，布局像素）。
   *
   * 窗口宽度**跟着它走**：原来的做法是把窗口固定成 520 宽、再把窗口左边放在
   * 立绘中心稍右处 —— 短文字时气泡只有约 190 宽，而窗口仍是 520，
   * 被钳进工作区后窗口左边被推到屏幕很左侧，气泡（贴窗口左）就跟着飘到
   * 离宠物 380 多像素的地方（用户报的「太偏了吧」）。
   */
  private bubbleSize: { width: number; height: number } | null = null;
  /** 最近一次的宠物锚点（尺寸上报后要用它重新摆一次） */
  private lastAnchor: PetAnchor | null = null;

  constructor(
    private readonly options: BubbleWindowOptions,
    /** 诊断回调：写 startup.log（`log()` 在本环境会丢） */
    private readonly onDiagnostic?: (line: string) => void,
  ) {}

  create(): BrowserWindow {
    if (this.window && !this.window.isDestroyed()) return this.window;

    const preload = resolvePreload(this.options.distDir, BUBBLE_PRELOAD_FILENAME);
    const page = resolveRendererPage(this.options.distDir, BUBBLE_PAGE_FILENAME);
    if (!existsSync(page)) {
      this.onDiagnostic?.(`[气泡] 页面不存在：${page}`);
    }

    const window = new BrowserWindow({
      width: BUBBLE_MAX_WIDTH,
      height: BUBBLE_MAX_HEIGHT,
      /*
       * 起手放在**屏幕外**，而不是 `0,0`。
       *
       * 为什么（2026-10-04 用户报「第一句气泡会在屏幕中央附近」）：
       * Windows 对"位置为 0,0 的窗口"会**居中摆放**。首帧没画完就 `showInactive()` 时，
       * 窗口会先以这个位置出现在**屏幕中央**，等 `setBounds` 生效才跳到位 ——
       * 用户看到的就是"气泡先在中间闪一下"。
       *
       * 配合 `firstShowPending`（首帧就绪前不显示），双保险。
       */
      x: -4000,
      y: -4000,
      // 起手隐藏：定位要在知道宠物位置之后才做
      show: false,
      frame: false,
      transparent: true,
      resizable: false,
      movable: false,
      minimizable: false,
      maximizable: false,
      fullscreenable: false,
      // 不占任务栏：它是个装饰性浮层，不是"应用窗口"
      skipTaskbar: true,
      hasShadow: false,
      // **不抢焦点**：气泡出现时不能把用户正在别的程序里的输入打断
      focusable: false,
      backgroundColor: '#00000000',
      webPreferences: {
        preload,
        contextIsolation: true,
        nodeIntegration: false,
        sandbox: true,
        // 与宠物窗口同理：非焦点窗口也要保持响应
        backgroundThrottling: false,
      },
    });

    // 始终在宠物之上：气泡被宠物挡住就没意义了
    window.setAlwaysOnTop(true, 'screen-saver');
    window.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });
    // 不接收鼠标：气泡只是"显示"，点它应该穿透到下面的东西上
    window.setIgnoreMouseEvents(true, { forward: false });

    void window.loadURL(rendererUrl(page));
    /*
     * 首帧就绪：**先定位、再显示**。
     *
     * `firstShowPending` 由 `show()` 置位（首帧未就绪时它会拦住显示请求），
     * 这里等就绪后统一补上 —— 顺序是"定好位 -> showInactive"，中间不会露出
     * 一个位置不对的窗口。
     */
    this.firstShowPending = true;
    window.once('ready-to-show', () => {
      this.firstShowPending = false;
      this.onDiagnostic?.('[气泡] 窗口就绪');
      if (this.anchorPendingShow && this.lastAnchor) this.place(this.lastAnchor);
      if (this.anchorPendingShow) {
        this.anchorPendingShow = false;
        if (!window.isDestroyed() && !window.isVisible()) window.showInactive();
      }
    });

    this.window = window;
    return window;
  }

  get browserWindow(): BrowserWindow | null {
    return this.window;
  }

  /**
   * 把气泡窗口摆到宠物头顶上方。
   *
   * ---- 水平定位：跟碰撞箱同一套锚点 ----
   *
   * 用户反馈：「太偏了吧，没多少文字的时候可以适当向右，和碰撞箱写一套算法就好了」。
   *
   * 原来按"窗口宽的一半居中"，而窗口固定 520 宽 —— 于是短文字的气泡**左边**
   * 被推到立绘左侧 260px 处（截图里气泡飘在离宠物很远的左上方）。
   *
   * 现在改成：**气泡左边从立绘中心稍往右开始、向右铺开**，越界再钳回工作区。
   *   · 短文字 -> 气泡紧贴宠物右上，不飘远；
   *   · 长文字 -> 向右铺开，铺到屏幕右边再自动换行（气泡在窗口内贴左）；
   *   · 贴屏幕右边缘时 -> 被钳回工作区内，不会出屏。
   *
   * 锚点与碰撞箱同源：都用渲染层实测的**立绘矩形**（`artScreenAnchor`），
   * 所以"碰撞箱在哪、气泡就锚在哪"，不会两套算法各自漂移。
   */
  place(anchor: PetAnchor): { left: number; top: number; width: number; height: number } | null {
    const window = this.window;
    if (!window || window.isDestroyed()) return null;

    this.lastAnchor = anchor;
    const display = screen.getDisplayNearestPoint({
      x: Math.round(anchor.centerX),
      y: Math.round(anchor.topY),
    });
    const wa = display.workArea;
    const peeking = Boolean(anchor.peeking);

    // 窗口尺寸：**优先用气泡实测宽度**，让它跟着文字长短伸缩；
    // 还没测到时给一个够用的初值。高度同理。
    //
    // ## ⚠️ 初值不要用 `BUBBLE_MAX_*`（2026-10-05 实测踩到）
    //
    // 原来初值是 `BUBBLE_MAX_WIDTH/HEIGHT`（520x620）—— 那是**上限**，不是
    // 典型值。620 高在 816 的工作区里意味着"上方/下方都塞不下"，
    // 于是**四个候选全部判不合格**，只好走兜底路径：
    //
    //     候选 0(1016,-150✗) 1(1487,476✗) 2(772,476✗) 3(1016,703✗)
    //     -> 兜底选出的位置和真实尺寸算出来的（1046,422）差了 400+ 像素
    //
    // 实测典型气泡约 `150~490 x 48`（"嗯。" 是 93x38，长回复到 490 宽）。
    // 用一个**典型值**当初值，四个候选的评价就接近真实情形，
    // 即使实测尺寸晚到 100ms，位置也不会差得离谱。
    const PLACEHOLDER_W = 300;
    const PLACEHOLDER_H = 90;
    const wantW = this.bubbleSize ? this.bubbleSize.width + 8 : PLACEHOLDER_W;
    const wantH = this.bubbleSize ? this.bubbleSize.height + 8 : PLACEHOLDER_H;
    /*
     * ## ⚠️ 宽度下限从 80 提到 160（2026-10-05 修"又高又窄"）
     *
     * 实测复现（394 字长回复）：
     *
     *     窗口 80x50    气泡 72x2879    文字 399 字
     *
     * 80 宽意味着**一行只放得下 1~2 个汉字**，于是文字全部竖排、高度炸到几千像素。
     *
     * 这个 80 原本是"防止窗口窄到看不见"的兜底，但它**太窄了**：短气泡最窄也有
     * 90px 左右（实测 `嗯。` 这种两个字是 93x38），所以 80 这个下限只在**出错时**
     * 才会被用到 —— 而它一出场就把排版彻底毁掉（比"窗口略宽一点"糟得多）。
     *
     * 160 是"两三个字 + 左右内边距"的舒适宽度，也是这个下限该有的语义：
     * **兜底值不能改变内容的排布方式**。
     */
    const MIN_W = 160;
    const width = Math.max(MIN_W, Math.min(wantW, Math.max(MIN_W, wa.width - 16)));
    const height = Math.max(48, Math.min(wantH, Math.max(80, wa.height - 16)));

    // 窗口位置（由下面的候选逻辑赋值）
    let x = Math.round(anchor.centerX + LEFT_BIAS);
    let y = Math.round(anchor.topY - height);

    /*
     * ## 位置：**四个候选，挑第一个装得下的**
     *
     * 优先级（用户要求演变而来，见下）：
     *   ① 头顶上方（常态）
     *   ② 右侧 ③ 左侧 —— 「宠物处在顶部的时候……改成**旁边**观感豪横多」
     *   ④ 下方 —— 最后兜底
     *
     * ## ⚠️ 为什么必须"判可行"而不是"先算再夹紧"（2026-10-04 视频实测）
     *
     * 旧写法是"先按头顶上方算 `y`，出屏了再夹回工作区"。问题是**夹紧后的位置可能
     * 正好压在宠物身上**，而且**部分越界时不会触发换位**：
     *
     *   视频里宠物在右上角，气泡「天黑了，要不要歇会儿?」出现在右上角、
     *   **一半在屏幕外**，而且紧贴宠物头顶 —— 用户报的"还在诶"就是这个。
     *
     * 所以改成：**每个候选先检查它是否完整落在工作区内**，不行就换下一个。
     * 四个都不行（宠物几乎占满屏幕）才退回夹紧，保证至少可见。
     */
    const gap = this.gapAbovePx + (peeking ? PEEK_EXTRA_DROP : 0);
    /*
     * ⚠️ 水平边界用**可见外接框**（含旋转），不是未旋转的 —— 见 `visibleLeftX` 的说明。
     * 拿不到时才退回未旋转那份（至少不会更糟）。
     */
    const edgeRight = anchor.visibleRightX ?? anchor.rightX;
    const edgeLeft = anchor.visibleLeftX ?? anchor.leftX;

    /**
     * 候选：先按 `preferX` 放（越界就往工作区里收），然后检查
     * **① 完整落在工作区内 ② 不与立绘矩形相交**。
     *
     * ## ⚠️ 为什么必须检查"与立绘相交"（2026-10-04 探针实测）
     *
     * 我原来只判断"候选是否完整在工作区内"，于是"贴立绘右边放"在
     * **立绘是旋转的**时候会压上去 —— 因为旋转后的立绘会**伸进**气泡的区域：
     *
     *     右侧探头   立绘 1394,232 280x280 | 气泡 1364,232 172x50 | 重叠 **142x50**
     *
     * 立绘外接框右边是 1674，气泡放在 1674+10 就出屏了；夹紧到 1364 之后
     * 正好压在外接框上 —— 而外接框的右下角**确实有立绘本体**（旋转后的身体）。
     *
     * 所以判据改成**矩形相交检测**：气泡矩形与立绘可见矩形只要有重叠就换下一个候选。
     * 这比"离外接框多远"更本质，也不用再维护"留多少像素"这类经验常量。
     */
    const art = {
      x0: anchor.visibleLeftX ?? anchor.centerX,
      x1: anchor.visibleRightX ?? anchor.centerX,
      y0: anchor.topY,
      y1: anchor.bottomY ?? anchor.topY,
    };
    /*
     * 🔴 侧向贴身用「核心框」（2026-10-18 用户报「左右的时候距离立绘近些」）：
     *
     * 立绘包围盒是从**某一帧**的像素扫出来的（measureArtBox 同一尺寸只量一次），
     * 而立绘是持续动画 —— 她摆回来之后，框的左右两侧就留着一段"量到了但此刻
     * 是透明"的余量。实测（main.log 10:48）：测量框 x1356..1599，立绘可见部分
     * 却从 ~1471 才开始 —— 左侧 115px 的空白，宠物越大这份余量也越大。
     *
     * 所以侧向候选的**定位与压立绘判定**都改用核心框：左右各收
     * `min(框宽 × 25%, 80px)`（随宠物大小等比缩放），气泡贴着核心框的边，
     * 视觉距离就近了。上方/下方候选仍用全框（头顶方向没有这个问题）。
     */
    const makeCandidate = (preferX: number, preferY: number, allowShiftX: boolean, box = art) => {
      let cx = Math.round(preferX);
      if (allowShiftX) {
        if (cx + width > wa.x + wa.width) cx = wa.x + wa.width - width;
        if (cx < wa.x) cx = wa.x;
      }
      const cy = Math.round(preferY);
      const inWorkArea =
        cx >= wa.x && cx + width <= wa.x + wa.width && cy >= wa.y && cy + height <= wa.y + wa.height;
      // 与立绘矩形相交？（用严格大于，边缘相接不算相交）
      const hitsArt =
        cx < box.x1 && cx + width > box.x0 && cy < box.y1 && cy + height > box.y0;
      return { cx, cy, ok: inWorkArea && !hitsArt, inWorkArea };
    };

    const candidates: { cx: number; cy: number; ok: boolean; inWorkArea: boolean }[] = [];
    /*
     * ## 候选顺序（🔴 2026-10-18 用户重定，覆盖 2026-10-04 的「探头先旁边」）
     *
     * 用户原话：「正常状态显示在宠物上面，只有在上面没位置时才显示在左边或
     * 右边，这个方向看宠物哪边空余位置多」。
     *
     *   ① **上方永远优先**（探头时也一样 —— 10-04 那条"探头先旁边"作废）
     *   ② 上方放不下才走旁边；旁边**先试空余位置多的一侧**
     *      （leftSpace = 立绘左缘到工作区左缘，rightSpace 同理），
     *      一侧不合格还能换另一侧
     *   ③ 下方兜底
     *
     * 每个候选仍要过 makeCandidate 的两道检查（完整在工作区内 + 不压立绘），
     * 后面的多级兜底（重叠面积最小 / 穷举贴边）全部保持不变。
     */
    // 边缘拿不到时退化用 centerX（至少不会更糟 —— 与 visibleLeftX 的兜底口径一致）
    const leftEdge = edgeLeft ?? anchor.centerX;
    const rightEdge = edgeRight ?? anchor.centerX;
    // 侧向用核心框（左右各收 25%、上限 80px）—— 贴近立绘，见 makeCandidate 的说明
    const shrinkX = Math.min(CORE_SHRINK_X_CAP, Math.round((art.x1 - art.x0) * (this.coreShrinkPct / 100)));
    const coreX = {
      x0: art.x0 + shrinkX,
      x1: art.x1 - shrinkX,
      y0: art.y0,
      y1: art.y1,
    };
    // 垂直方向同一帧扫描的透明余量也在头顶/脚下（2026-10-18 用户报
    // 「气泡距离下面的宠物距离也远了」）：上下各收 25%、上限 60px。
    // 上方候选锚 coreY 顶边、下方锚 coreY 底边；PEEK_EXTRA_DROP 保留
    // （探头时视觉上的头更低，10-04 的「改低一些」继续有效）。
    const shrinkY = Math.min(CORE_SHRINK_Y_CAP, Math.round((art.y1 - art.y0) * (this.coreShrinkPct / 100)));
    const coreY = {
      x0: art.x0,
      x1: art.x1,
      y0: art.y0 + shrinkY,
      y1: art.y1 - shrinkY,
    };
    const leftSpace = leftEdge - wa.x;
    const rightSpace = wa.x + wa.width - rightEdge;
    // ① 上方优先：锚垂直核心框的顶边（贴近头顶，见 coreY 说明）
    candidates.push(makeCandidate(anchor.centerX + LEFT_BIAS, coreY.y0 - gap - height, true, coreY));
    const pushSide = (side: 'left' | 'right') => {
      if (side === 'right') {
        candidates.push(makeCandidate(coreX.x1 + this.gapSidePx, anchor.topY, false, coreX));
      } else {
        candidates.push(makeCandidate(coreX.x0 - this.gapSidePx - width, anchor.topY, false, coreX));
      }
    };
    // 空余多的一侧先试，另一侧紧随其后（第一个失败还有第二次机会）
    if (rightSpace >= leftSpace) {
      pushSide('right');
      pushSide('left');
    } else {
      pushSide('left');
      pushSide('right');
    }

    // ③ 下方兜底：锚垂直核心框的底边
    if (anchor.bottomY != null) {
      candidates.push(makeCandidate(anchor.centerX + LEFT_BIAS, coreY.y1 + gap, true, coreY));
    }

    /*
     * ## 挑候选的优先级（2026-10-04 实测后定的）
     *
     *   ① **不压立绘且在屏幕内**的候选里，按 candidates 的顺序取第一个
     *      （顺序 = 上方 -> 右 -> 左 -> 下方，即用户要求的偏好）
     *   ② 没有 ① 时，取"在屏幕内"且**与立绘重叠面积最小**的
     *   ③ 连屏幕内都做不到（宠物几乎占满屏幕）才用第一个，靠下面夹紧保证可见
     *
     * ## ⚠️ 为什么必须"先比较是否压立绘"，而不是"按顺序取第一个可行的"
     *
     * 第一版是 `candidates.find(c => c.ok)` —— 而 `ok` 只看"是否在屏幕内"，
     * **上方候选在宠物贴顶时恰好"在屏幕内"**，于是它总被选中，即使它正压着立绘。
     * 实测日志（右侧探头，立绘 x1313..1593）：
     *
     *     候选 0(1364,234✓) 1(1603,314✗) 2(1131,314✓) 3(1364,567✓)
     *     选中 1364,234      <- 0 号压着立绘；**2 号才是干净的**（1131 在立绘左侧）
     *
     * 用户诉求的顺序是「**要能看得见气泡**」+「不能和宠物重合」，
     * 所以"不压立绘"这一档要**整体优先于**"上方"这个方向偏好。
     */
    const preferredOrder = candidates.filter((c) => c.ok);
    const inOrder = candidates.filter((c) => c.inWorkArea);
    const scoreOverlap = (c: { cx: number; cy: number }) => {
      const ovx = Math.max(0, Math.min(c.cx + width, art.x1) - Math.max(c.cx, art.x0));
      const ovy = Math.max(0, Math.min(c.cy + height, art.y1) - Math.max(c.cy, art.y0));
      return ovx * ovy;
    };

    /**
     * 兜底位置：**把候选夹进工作区之后再看谁压得最少**。
     *
     * ## ⚠️ 为什么不能直接用 `candidates[0]`（2026-10-05 实测定位到的 bug）
     *
     * 原来的三级兜底最后一级是 `: candidates[0]` —— 即"上方"那个候选，
     * **完全不看它落在哪**。而"上方"的 `y = topY - gap - height`：
     * 气泡越高，这个 y 越negative，**越容易掉出工作区上边**。
     *
     * 实测（占位尺寸 520x620 时四个候选全是 ✗）：
     *
     *     候选 0(1016,-150✗) 1(1487,476✗) 2(772,476✗) 3(1016,703✗)
     *     选中 1016,-150        <- y 是负的，气泡大半在屏幕外
     *     ↓ 夹紧后 y=0
     *     99ms 后实测尺寸到了：选中 1046,422
     *
     * 于是气泡**先从 y=0（屏幕顶）显示，再跳到 y=422** ——
     * 中间那 99ms 里它正好横在立绘所在的高度上，看着就是"压在宠物身上"。
     *
     * 修法：兜底时**先把候选夹进工作区**，再按"与立绘重叠面积"取最小的。
     * 这样即使四个都不合格，选的也是"在屏幕内、且压得最少"的那个位置，
     * 而不是一个需要靠夹紧去救的越界值。
     */
    const clampToWorkArea = (c: { cx: number; cy: number }) => {
      const cx = Math.max(wa.x, Math.min(c.cx, wa.x + wa.width - width));
      const cy = Math.max(wa.y, Math.min(c.cy, wa.y + wa.height - height));
      return { cx, cy };
    };

    /**
     * **穷举式兜底**：在工作区内找"与立绘零重叠"的位置（2026-10-07 加强）。
     *
     * ## 为什么不能只在 4 个候选里挑
     *
     * 4 个候选是**方向偏好**的产物（上方/右/左/下方，各一处对齐方式）。
     * 但"哪个方向没地方"和"哪些位置没重叠"是两件事 —— 完全可能四个候选
     * 都压着立绘，而屏幕另一侧有一大片空地。
     *
     * 实测（用户截图）：一条报错气泡很长，四个候选全不合格，于是它在
     * 4 个"都压着立绘"的位置里挑了压得最少的那个 —— **仍然压在宠物身上**。
     *
     * ## 做法：贴着立绘的四条边各试一个位置
     *
     * 零重叠的位置一定存在于这些"临界摆放"之一 —— 只要真有空地：
     *
     *   · 立绘**上方**：气泡底边贴 `art.y0 - gap`（水平居中并夹进工作区）
     *   · 立绘**下方**：气泡顶边贴 `art.y1 + gap`
     *   · 立绘**左侧**：气泡右边贴 `art.x0 - SIDE_GAP`
     *   · 立绘**右侧**：气泡左边贴 `art.x1 + SIDE_GAP`
     *
     * 每个都夹进工作区后再判一次是否真的零重叠 —— 夹紧可能把它推回去压住立绘，
     * 那就不算数。返回 null 表示**真的没地方放**（宠物几乎占满屏幕）。
     *
     * 这个搜索是 O(4)，可以随便调 —— 但它只在"常规候选全灭"时才跑，
     * 所以正常路径的开销没变。
     */
    const searchNonOverlapping = (): { cx: number; cy: number } | null => {
      const spots: { cx: number; cy: number }[] = [
        // 上方：水平居中偏右（与主路径同一个偏好），底边贴立绘顶边
        { cx: Math.round(art.x0 + (art.x1 - art.x0) / 2 + LEFT_BIAS), cy: art.y0 - gap - height },
        // 下方
        { cx: Math.round(art.x0 + (art.x1 - art.x0) / 2 + LEFT_BIAS), cy: art.y1 + gap },
        // 左侧
        { cx: art.x0 - SIDE_GAP - width, cy: art.y0 },
        // 右侧
        { cx: art.x1 + SIDE_GAP, cy: art.y0 },
      ];
      for (const spot of spots) {
        const c = clampToWorkArea(spot);
        const hitsArt = c.cx < art.x1 && c.cx + width > art.x0 && c.cy < art.y1 && c.cy + height > art.y0;
        if (!hitsArt) return c;
      }
      return null;
    };

    const fallback = candidates
      .map((c) => ({ ...clampToWorkArea(c), inWorkArea: true, ok: false }))
      .reduce((best, c) => (scoreOverlap(c) < scoreOverlap(best) ? c : best));

    /*
     * 三级选择（优先级从高到低）：
     *   ① 常规候选里"在屏幕内且不压立绘"的第一个（保留方向偏好）
     *   ② 常规候选里"在屏幕内"且重叠最小的（原来的行为）
     *   ③ **穷举搜一个零重叠的位置**；搜不到才退回"压得最少"的兜底
     *
     * ⚠️ ③ 是这次新加的：原来 ② 之后直接就是"压得最少"，于是"四个候选都压着
     * 立绘、但屏幕另一侧有地方"这种情况会**压着宠物**显示。用户截图就是这个。
     */
    const searched = preferredOrder.length === 0 && inOrder.length === 0 ? searchNonOverlapping() : null;
    const chosen =
      preferredOrder.length > 0
        ? preferredOrder[0]
        : inOrder.length > 0
          ? inOrder.reduce((best, c) => (scoreOverlap(c) < scoreOverlap(best) ? c : best))
          : (searched ?? fallback);
    x = chosen.cx;
    y = chosen.cy;
    /** 最终落点是否仍然压着立绘（用于诊断 —— 这时是真的没地方放了） */
    const finalHitsArt =
      x < art.x1 && x + width > art.x0 && y < art.y1 && y + height > art.y0;
    const usedFallback = preferredOrder.length === 0 && inOrder.length === 0;

    /*
     * 诊断：把**候选评估过程**打出来。
     *
     * 排查"气泡压在宠物身上 / 出屏"时，只看最终位置推不出是哪一步淘汰了候选。
     * 这条把立绘矩形、工作区、四个候选取值与其判定全部列出。
     */
    this.onDiagnostic?.(
      `[气泡] 选位 立绘 x${art.x0}..${art.x1} y${art.y0}..${art.y1} | 工作区 ${wa.width}x${wa.height}` +
        ` | 气泡 ${width}x${height}` +
        ` | 候选 ${candidates.map((c, i) => `${i}(${c.cx},${c.cy}${c.ok ? '✓' : c.inWorkArea ? '△' : '✗'})`).join(' ')}` +
        ` | 选中 ${chosen.cx},${chosen.cy}` +
        // 走了兜底才把"穷举结果"与"最终是否仍压立绘"打出来 —— 正常情况下不必刷屏
        (usedFallback
          ? ` | 走兜底 穷举${searched ? '找到空位' : '**没找到空位**'}${finalHitsArt ? ' **仍压立绘**' : ''}`
          : ''),
    );

    /*
     * 四个候选都不完整可行（宠物几乎占满屏幕）时的兜底：
     * 夹进工作区，保证**至少可见** —— 那时必然会压到宠物，两害相权取其轻。
     */
    if (x + width > wa.x + wa.width) x = wa.x + wa.width - width;
    x = Math.max(wa.x, x);
    y = Math.max(wa.y, Math.min(y, wa.y + wa.height - height));

    this.window?.setBounds({ x, y, width, height });
    return { left: x, top: y, width, height };
  }

  /**
   * 渲染层上报气泡的实测尺寸 -> 用新宽度重新摆一次窗口。
   *
   * 这是"窗口跟着气泡走"的关键一步：气泡宽度由内容决定，
   * 而窗口宽度决定锚点，所以每次内容变化都要重摆。
   */
  setBubbleSize(size: { width: number; height: number; windowWidth?: number }): void {
    const w = Number(size?.width);
    const h = Number(size?.height);
    if (!Number.isFinite(w) || !Number.isFinite(h) || w <= 0 || h <= 0) return;
    /*
     * ## 宽度**由渲染层给**，主进程不再自己推导
     *
     * 曾经是主进程用渲染层报的 `naturalWidth` 来算，结果是**闭环**：
     *
     *     窗口窄 -> `.bubble` 的 max-width 小 -> 内容换行 -> 量出的"自然宽度"也小 -> 窗口更窄
     *
     * 实测收敛到最窄：窗口锁死 160，394 字排成 2879px 高的一列
     * （用户报的「高度顶天了都，宽度才几个字」）。
     *
     * 闭环的病根是"测量受被测量对象影响"。所以改成**单向**：
     * 渲染层给出最终目标宽度，这里**只做范围保护**，不做推导。
     */
    const want = Number(size?.windowWidth);
    const wantW = Number.isFinite(want) && want > 0 ? Math.min(Math.ceil(want), BUBBLE_MAX_WIDTH) : w;
    const changed =
      !this.bubbleSize ||
      Math.abs(this.bubbleSize.width - wantW) > 1 ||
      Math.abs(this.bubbleSize.height - h) > 1;
    if (!changed) return;
    this.bubbleSize = { width: wantW, height: h };
    // 用最近的锚点重摆 —— 内容变了，窗口要跟着变宽/变高
    if (this.lastAnchor) this.place(this.lastAnchor);
    this.onDiagnostic?.(`[气泡] 尺寸 ${Math.round(w)}x${Math.round(h)} -> 窗口重摆`);
    /*
     * 有"等尺寸再显示"的请求时，**现在**定位完了才显示（见 `show` 的说明）。
     * 顺序很关键：先 `place()` 再 `showInactive()`，中间不露出错位置的窗口。
     *
     * ## ⚠️ 这里曾经有个**写反的条件**，导致气泡永不显示（2026-10-05 修）
     *
     * 原来的代码是：
     *
     *     if (win && !win.isDestroyed() && !win.isVisible()) win.showInactive();
     *                                      ^^^^^^^^^^^^^^^^^^^ 多余的判断
     *
     * 看着像"没显示才显示"的保护，其实**恰好把唯一该显示的场合挡掉了**：
     * `showWhenSized` 为真的语义就是"我们**延迟**了显示、窗口此刻**从未显示过**"，
     * 所以这里必然该调 `showInactive()`。
     *
     * 实测后果（日志原文）：
     *
     *     选位 … 气泡 520x360 | 选中 1016,111     ← 占位值算的位置
     *     选位 … 气泡 512x66  | 选中 1016,405     ← 真实值
     *     尺寸 512x58 -> 窗口重摆
     *     （窗口始终 visible=false，停在 -4000,-4000）
     *
     * 用户看到的就是"气泡该显示却不显示"。而 `place()` 明明跑了 4 次 ——
     * 只查 bounds 不看 `isVisible()` 的话，会误判成"定位没生效"。
     */
    if (this.showWhenSized) {
      this.showWhenSized = false;
      if (this.sizeWaitTimer) {
        clearTimeout(this.sizeWaitTimer);
        this.sizeWaitTimer = null;
      }
      this.sizeWaitExpired = true;
      const win = this.window;
      if (win && !win.isDestroyed()) win.showInactive();
    }
  }

  /**
   * 显示（不抢焦点）。
   *
   * ## ⚠️ **首次显示必须等定位完成**（2026-10-04 用户报「第一句气泡在屏幕中央附近」）
   *
   * 窗口是 `show: false` 起手的，但如果**首帧还没画完就 `showInactive()`**，窗口会先以
   * **创建时的位置**出现在屏幕上，`setBounds` 之后才跳到位 —— 观感就是"气泡先在屏幕
   * 中央闪一下"。用户看到的正是这个。
   *
   * 所以：首帧未就绪时**先记下请求**，等 `ready-to-show` 里定好位再显示（见 `create`）。
   * 已经显示过之后（热路径）直接显示，不再等待 —— 那时窗口早就就绪了。
   */
  show(): void {
    const window = this.window;
    if (!window || window.isDestroyed()) return;
    if (this.firstShowPending) {
      // 首帧还没就绪：记下"要显示"，等 ready-to-show 里定位完再显示
      this.anchorPendingShow = true;
      return;
    }
    /*
     * ## ⚠️ **尺寸还没量到时不要显示**（2026-10-04 实测定位到的根因）
     *
     * 气泡宽度由文字长短决定，要等渲染层量完再报回来。在那之前只能用占位尺寸
     * （`BUBBLE_MAX_WIDTH/HEIGHT` = 520x360），**按它算出来的位置和最终位置差很远**。
     *
     * 实测（第一条气泡）：
     *
     *     选位 … 气泡 **520x360** | 候选 0(1016,111✓) | 选中 1016,111
     *             ↓ 约 100ms 后渲染层报实测尺寸
     *     选位 … 气泡 153x48      | 选中 1383,423
     *
     * 于是窗口**先以 1016,111 显示**（错位置），100ms 后才跳到 1383,423 ——
     * 用户看到的就是"第一句气泡位置不对"。
     *
     * 所以：只有**拿到过实测尺寸**（或超时兜底）才允许显示；
     * 否则记下请求，等 `setBubbleSize` 里定位好再显示。
     */
    if (!this.bubbleSize && !this.sizeWaitExpired) {
      this.showWhenSized = true;
      if (!this.sizeWaitTimer) {
        // 兜底：万一渲染层没报尺寸（页面出错等），也要显示，不能永远不出来
        this.sizeWaitTimer = setTimeout(() => {
          this.sizeWaitTimer = null;
          this.sizeWaitExpired = true;
          if (this.showWhenSized) {
            this.showWhenSized = false;
            if (this.lastAnchor) this.place(this.lastAnchor);
            this.show();
          }
        }, SIZE_WAIT_TIMEOUT_MS);
      }
      return;
    }
    if (!window.isVisible()) window.showInactive();
  }

  /** 等到实测尺寸再显示的标记（见 `show` 的说明） */
  private showWhenSized = false;
  /** 尺寸等待是否已超时兜底（超时后不再等） */
  private sizeWaitExpired = false;
  private sizeWaitTimer: ReturnType<typeof setTimeout> | null = null;

  /** 首帧是否已就绪（`create` 里在 `ready-to-show` 之后置 false） */
  private firstShowPending = false;
  /** 首帧未就绪期间收到过"要显示"的请求 */
  private anchorPendingShow = false;

  /**
   * 这次是不是**第一次**要显示气泡？是的话返回 true 并销掉标记。
   *
   * ⚠️ 目前**没有调用方**了：曾经给"首次气泡延迟显示"用，那个延迟已撤销
   * （见文件顶部的说明）。保留这个方法是因为它在排查"首帧 vs 后续"差异时有用 ——
   * 如果将来又需要区分首次，直接接上即可。
   */
  takeFirstShowDelay(): boolean {
    if (!this.firstBubbleDone) {
      this.firstBubbleDone = true;
      return true;
    }
    return false;
  }

  private firstBubbleDone = false;

  hide(): void {
    const window = this.window;
    if (!window || window.isDestroyed()) return;
    if (this.hideTimer) {
      clearTimeout(this.hideTimer);
      this.hideTimer = null;
    }
    if (window.isVisible()) window.hide();
  }

  /**
   * ## 气泡的**存在时长在主进程计时**（2026-10-04）
   *
   * 用户要求：「让气泡正常该显示时才显示气泡，当然**隐藏的时间不计入气泡持续时间内**」。
   *
   * 原来计时在渲染层（`bubble.js` 的 `scheduleHide`），收到 `bubble:message` 就开始数。
   * 但那一刻窗口**可能还没画出来** —— 首次显示要等首帧（实测有约 1.5s 的加载宽限），
   * 于是那 1.5 秒被算进了"显示时长"，气泡实际只显示 8.5 秒。
   *
   * 所以改由这里计时，起点是**渲染层报告"真的画出来了"**（`bubble:shown`）。
   */
  scheduleHide(lingerMs: number): void {
    if (this.hideTimer) clearTimeout(this.hideTimer);
    const ms = Math.max(1000, Math.round(lingerMs));
    this.onDiagnostic?.(`[气泡] 起隐藏计时 ${ms}ms（旧计时器${this.hideTimer ? '有' : '无'}）`);
    this.hideTimer = setTimeout(() => {
      this.hideTimer = null;
      this.onDiagnostic?.('[气泡] 计时到 -> 隐藏');
      this.onAutoHide?.();
    }, ms);
  }

  private hideTimer: ReturnType<typeof setTimeout> | null = null;
  /** 到点自动隐藏时的回调（由 `index.ts` 接上"清掉气泡状态"的逻辑） */
  onAutoHide: (() => void) | null = null;

  isVisible(): boolean {
    return Boolean(this.window && !this.window.isDestroyed() && this.window.isVisible());
  }

  /**
   * **宠物移动后重新摆位**（气泡可见时才做）。
   *
   * ## 为什么需要它（2026-10-04 探针实测）
   *
   * 原来只有"要说话"时才调 `place()`（见 `index.ts` 的 `showBubble`），
   * 于是**气泡显示期间宠物一动，气泡就留在原地**。探针实测（气泡完全没动）：
   *
   *     常态      立绘中心x 1394 | 气泡 1344,425
   *     探头(右)  立绘中心x 1509 | 气泡 1344,425   <- 应该跟着右移
   *     探头(左)  立绘中心x   14 | 气泡 1344,425   <- 应该跟到屏幕左边
   *
   * 用户报的正是「将探头时的气泡位置改低一些，这样语音聊天的时候文字显示正常」
   * —— 语音聊天时气泡是**持续显示**的，而宠物会动，所以必须跟随。
   *
   * ## 节流
   *
   * 拖拽/旋转期间立绘矩形上报很频繁（约 25Hz），每次都 `setBounds` 会让气泡窗口
   * 抖。这里按时间节流（默认 60ms ≈ 16Hz），既跟得上又不会抖。
   */
  followAnchor(anchor: PetAnchor | null, minIntervalMs = 60): void {
    if (!anchor || !this.isVisible()) return;
    const now = Date.now();
    if (now - this.lastFollowAt < minIntervalMs) return;
    this.lastFollowAt = now;
    this.place(anchor);
  }

  private lastFollowAt = 0;

  /**
   * 段间距当前值（px）。`settings:set-bubble-para-gap` 改完推给渲染层；
   * 渲染层把它写进 CSS 变量 `--para-gap`。
   */
  private paraGapPx = 4;

  /**
   * 气泡摆放的三个可调参数（2026-10-18，设置 → 桌宠）：
   *   · gapAbovePx —— 气泡与头顶的间距（原 GAP_ABOVE_HEAD）
   *   · gapSidePx  —— 气泡与左右立绘的间距（原 SIDE_GAP）
   *   · coreShrinkPct —— 立绘核心框收缩比例（补偿动画帧间的透明余量，
   *     越大气泡贴得越近；对应 place() 里的 0.25）
   * 启动时由 index.ts 注入；设置里改动走 `setPlacementTuning()` 立即生效。
   */
  private gapAbovePx = GAP_ABOVE_HEAD;
  private gapSidePx = SIDE_GAP;
  private coreShrinkPct = CORE_SHRINK_DEFAULT_PCT;

  setPlacementTuning(tuning: { gapAbove?: number; gapSide?: number; coreShrink?: number }): void {
    if (Number.isFinite(Number(tuning.gapAbove))) {
      this.gapAbovePx = Math.max(0, Math.min(120, Math.round(Number(tuning.gapAbove))));
    }
    if (Number.isFinite(Number(tuning.gapSide))) {
      this.gapSidePx = Math.max(0, Math.min(120, Math.round(Number(tuning.gapSide))));
    }
    if (Number.isFinite(Number(tuning.coreShrink))) {
      this.coreShrinkPct = Math.max(0, Math.min(40, Math.round(Number(tuning.coreShrink))));
    }
  }

  setParaGap(px: number): void {
    const gap = Math.max(0, Math.min(24, Math.round(Number(px) || 0)));
    if (gap === this.paraGapPx) return;
    this.paraGapPx = gap;
    // 走 bubble:message 通道（kind='config'），渲染层只改 CSS 变量、不 show
    this.send('bubble:message', { kind: 'config', paraGap: gap });
  }

  send(channel: string, payload: unknown): void {
    const window = this.window;
    if (!window || window.isDestroyed()) return;
    window.webContents.send(channel, payload);
  }

  destroy(): void {
    const window = this.window;
    this.window = null;
    if (window && !window.isDestroyed()) window.destroy();
  }
}
