/**
 * 拖拽窗口的坐标计算。
 *
 * 为什么单独一个模块：这段算术是"拖拽准不准"的全部逻辑（窗口位置 = 按下时的
 * 窗口位置 + 指针位移），但它原先写在 `petWindow.ts` 里，依赖 electron 的
 * BrowserWindow —— 于是**无法测试**。抽成纯函数后可以用假窗口把各种情况跑一遍：
 * 往各个方向拖、连续移动、重复 begin 而不 end、非整数坐标。
 *
 * 坐标系：渲染层给的 `event.screenX/Y` 与 Electron 的 `getPosition()`/`setPosition()`
 * 都是**逻辑像素**（受同一套 DPI 缩放影响），所以直接相减即可，不需要乘缩放比。
 * 这一点很容易搞错：screenX 给的是"屏幕坐标"听起来像物理像素，其实不是。
 */

export interface DragOrigin {
  pointerX: number;
  pointerY: number;
  winX: number;
  winY: number;
  winWidth: number;
  winHeight: number;
}

/** 工作区矩形（逻辑像素），用于把窗口夹在屏幕内 */
export interface WorkArea {
  x: number;
  y: number;
  width: number;
  height: number;
}

/**
 * 把窗口位置夹进工作区：**窗口整体必须留在屏幕内**。
 *
 * 用户要求（原话）：「拖拽中判定宠物是否触碰屏幕左右边缘，触碰则不再增减
 * 窗口位置的定位，实现不超出屏幕」。
 *
 * 所以语义是**硬夹紧**：x 只能落在 `[workArea.x, workArea.x + width - winWidth]`。
 * 触到任一端就"不再增减"（继续往那个方向拖，位置保持不变）——
 * 这比"让屏幕切掉一部分"好：用户看到的是宠物停在边上，而不是缺了一块。
 *
 * 边界情况：窗口比工作区还大时（缩放调到最大 + 小屏），上限会小于下限，
 * 那时取 `workArea.x`，避免算出"反向区间"这种怪结果。
 */
/**
 * 把窗口位置夹进工作区。
 *
 * `clampMargin` 是**用户要求的关键旋钮**（原话）：
 *   「把随鼠标拖拽增减的极限增加窗口的留白就行了」
 *
 * 为什么需要它：窗口比立绘大一圈（立绘在窗口里居中，四周是透明留白）。
 * 若把窗口**死死夹在屏幕内**，立绘就永远离屏幕边差那圈留白 ——
 * 于是"拖到边缘自动探头"怎么调都触发不了，或者要靠调大阈值来糊过去，
 * 结果变成"还没到边就探头"。
 *
 * 正确做法：允许窗口**多走出去 `clampMargin`（= 那圈留白）**，
 * 正好让立绘的可见边缘贴到屏幕边。判定与视觉就一致了。
 *
 * 例（scale=1）：窗口 240、立绘可见 155 且居中 -> 每侧留白 ≈ 43px。
 * 传 `clampMargin=43` 后，窗口可以一直拖到 `x = 屏幕宽 - 240 + 43`，
 * 此时立绘右边正好压在屏幕右边缘上。
 */
export function clampToWorkArea(
  x: number,
  y: number,
  winWidth: number,
  winHeight: number,
  workArea: WorkArea,
  /**
   * 允许窗口越出工作区的像素数（横纵各自）。
   *
   * 用户要求：「把随鼠标拖拽增减的极限增加窗口的留白就行了」。
   * 放宽之后窗口能多走出去那圈留白，**立绘**的可见边才真正碰到屏幕边。
   *
   * 横向放宽的量 = 窗口边到立绘左边的距离（见 petWindow.currentArtMarginX）。
   */
  marginX = 0,
  /**
   * 纵向放宽量 = 窗口边到立绘上/下边的距离。
   *
   * ⚠️ 纵向还额外需要"越过任务栏"（用户报的「宠物突破不了下面」、
   * 「下面有东西挡着」）。`workArea` 已经排除了任务栏（实测任务栏 60px，
   * workArea 底正好压在任务栏上沿），所以只按 workArea 夹的话，
   * 立绘底边永远只能停在任务栏上沿 —— 看起来就是被挡住、下不去。
   * 解决办法是把**纵向的下限换成屏幕底**（由调用方通过 `screenBottom` 传入）。
   */
  marginY = 0,
  /**
   * 屏幕底边（物理工作区之外的**完整屏幕**下沿，逻辑像素）。
   *
   * 不传就退回 workArea 底（老行为）。传了之后窗口可以一直走到屏幕最底，
   * 立绘就能压在任务栏上 —— 桌面宠物一般都会盖住一点任务栏。
   */
  screenBottom?: number,
  /**
   * 屏幕顶边（逻辑像素）+ 立绘在窗口内的 **top**（上留白）。
   *
   * 用于让**立绘顶边**能走到屏幕顶：截图里立绘上半身被屏幕顶切掉了
   * （用户要求「宠物能超出屏幕也改一下」）。
   *
   * ⚠️ 上留白与下留白**不相等**（立绘贴窗口顶：上约 32、下约 113），
   * 所以必须单独传，不能复用 `marginY`。
   */
  screenTop?: number,
  marginTop?: number,
  /**
   * **右留白**（立绘右边 → 窗口右边）。
   *
   * ## ⚠️ 必须和左留白分开（2026-10-04 探针实测出来的）
   *
   * 原来左右共用 `marginX`，而它其实是**左留白**。立绘在画布里**不是左右对称**的
   * （实测左 33、右 42），所以右侧的拖拽极限差 9px —— 症状正是用户报的
   * 「宠物在边缘**时不时被挡住**」（左边缘正常、右边缘差一截）。
   *
   * 探针实测（分步拖到边缘）：
   *
   *     拖到最左：窗口一路到 x=-34，松手后正常探头 ✅
   *     拖到最右：窗口卡在 x=1149 → 1181 就不动了，松手后**没探头** ❌
   *
   * 缺省时退回 `marginX`（左右对称的素材不受影响）。
   */
  marginRight?: number,
  /**
   * **立绘的可见宽度**（逻辑像素）。
   *
   * 用来把"立绘右边贴到屏幕右"直接换算成窗口的 x 极限 —— 见下面 `maxX` 的推导。
   * 缺省时退回旧的"窗口宽 + 留白"公式。
   */
  artWidth?: number,
): { x: number; y: number } {  const mx = Math.max(0, Math.round(marginX));
  const my = Math.max(0, Math.round(marginY));
  const mr = marginRight != null ? Math.max(0, Math.round(marginRight)) : mx;
  /*
   * ## ⚠️ 极限必须**按立绘的边**算，不能按"窗口宽 + 留白"（2026-10-04 修）
   *
   * 旧写法：
   *
   *     minX = 屏左 − 左留白                 （这个碰巧是对的）
   *     maxX = 屏右 − 窗口宽 + 右留白         （❌ 隐含"立绘在窗口里居中"）
   *
   * 探针实测（分步拖到右边缘）：
   *
   *     窗口停在 x=1228（常态宽 251），立绘右边 = 1472
   *     屏幕右边 1536 ⇒ **离边还差 64px，够不到**
   *     松手后 `贴边判定 … 右 = -44 → 不贴边` ⇒ **从不探头**
   *
   * 立绘在窗口里是**贴左**的（左留白 33、右留白 42；窗口比画布宽时差更多），
   * 所以"窗口宽 + 右留白"和"立绘右边到窗口右边"不是一回事。
   *
   * 正确写法**不需要留白概念**，直接让立绘的边走到屏幕边：
   *
   *     立绘左边 = 窗口 x + 左留白  ⇒  窗口 x = 屏左 − 左留白          （最左）
   *     立绘右边 = 窗口 x + 左留白 + 立绘宽  ⇒  窗口 x = 屏右 − 左留白 − 立绘宽  （最右）
   *
   * `artWidth` 拿不到时退回旧公式（保持向后兼容，至少不会更糟）。
   */
  const minX = workArea.x - mx;
  const maxX =
    artWidth != null && artWidth > 0
      ? workArea.x + workArea.width - mx - Math.round(artWidth)
      : workArea.x + workArea.width - winWidth + mr;
  /*
   * 纵向上界：让**立绘顶边**能到屏幕顶（用户要求「宠物能超出屏幕也改一下」，
   * 截图里立绘上半身被屏幕顶切掉了）。
   *
   * ⚠️ 上边要用**真实的上留白** `marginTop`（= 立绘在窗口内的 top，约 32px），
   * **不是** `marginY`（那是"立绘底到窗口底"约 113px）。两者不相等 ——
   * 立绘是贴窗口顶的，上下留白本来就不同。
   *
   * 用 `-marginTop` 的含义：窗口顶可以走到"立绘顶边正好在屏幕顶"的位置。
   * 再往上就不给了 —— 让整只宠物完全滑出屏幕会"找不回来"。
   */
  // ⚠️ 用 `!= null` 而不是 `Number.isFinite` —— 后者把 `undefined` 也当"不是有限数"，
  // 但 TS 的类型收窄不认，会报 "number | undefined 不能赋给 number"（实测踩过）。
  const topMargin = marginTop != null ? Math.max(0, Math.round(marginTop)) : my;
  const minY = (screenTop != null ? screenTop : workArea.y) - topMargin;
  // 纵向下界：优先用屏幕底（能盖任务栏），否则退回 workArea 底
  const bottomLimit = screenBottom != null ? screenBottom : workArea.y + workArea.height;
  const maxY = bottomLimit - winHeight + my;
  return {
    // 用 max(minX, ...) 兜底"窗口比工作区还大"的情形（否则会算出反向区间）
    x: Math.round(Math.max(minX, Math.min(Math.max(minX, maxX), x))),
    y: Math.round(Math.max(minY, Math.min(Math.max(minY, maxY), y))),
  };
}

export class DragTracker {
  private origin: DragOrigin | null = null;

  constructor(
    /** 读取当前窗口 bounds（逻辑像素）。**必须含宽高**，见下面 move() 的说明。 */
    private readonly getBounds: () => { x: number; y: number; width: number; height: number },
    /**
     * 设置窗口 bounds。
     *
     * ⚠️ 必须用 `setBounds`（四个值一起给），**不能用 `setPosition`**。
     *
     * 实测（Windows 11 / scaleFactor 1.25）：反复 `setPosition` 会让窗口尺寸
     * **每次涨约 1px** —— 15 次拖动后从 640 涨到 655。原因是 DPI 分数缩放
     * （1.25×）下，设位置会连带重算非客户区，舍入误差累积。
     *
     * 对比（同样 15 次移动）：
     *     setPosition           640 -> 655   （+15，肉眼可见"越拖越大"）
     *     setBounds(锁宽高)      640 -> 641   （+1，可接受）
     *     setPosition+setSize   640 -> 656   （+16，更糟）
     *
     * 所以这里每次把宽高**原样**带上，避免尺寸被测距逻辑误改。
     */
    private readonly setBounds: (b: {
      x: number;
      y: number;
      width: number;
      height: number;
    }) => void,
    /**
     * 按**指针当前所在位置**取那块屏幕的工作区（逻辑像素）。
     *
     * 传指针坐标而不是窗口坐标，是为了多显示器：指针在哪块屏幕上，
     * 就用那块的工作区夹紧。不传这个回调时不做夹紧（保持纯计算、便于单测）。
     */
    private readonly getWorkArea?: (pointerX: number, pointerY: number) => WorkArea,
    /**
     * 允许窗口越出工作区的像素数（= 窗口边到立绘可见边的距离）。
     *
     * 由 `petLayout.peekShiftFor()` 算出来传给这里：**让拖拽极限放宽那圈留白**，
     * 立绘的可见边缘才能真正贴到屏幕边（用户明确要求）。
     * 不传时行为与原来完全一致（夹死在屏幕内）。
     *
     * 用**回调**而不是数字：值依赖当前的宠物缩放，而缩放随时可改
     * （菜单/设置里的「大小」）；写成构造参数会锁死在初始值上，
     * 而且 `petScale` 在字段初始化顺序上还可能"未初始化就使用"（实测报错）。
     */
    private readonly marginX: number | (() => number) = 0,
    /** 纵向放宽量（窗口边到立绘上/下边的距离） */
    private readonly marginY: number | (() => number) = 0,
    /**
     * 屏幕底边（逻辑像素）。用于让窗口能压住**任务栏** —— 用户报的
     * 「宠物突破不了下面」「下面有东西挡着」。
     * `workArea` 排除了任务栏，只按它夹的话立绘底边永远停在任务栏上沿。
     */
    private readonly getScreenBottom?: (pointerX: number, pointerY: number) => number,
    /** 屏幕顶边（回调版）；与 `getScreenBottom` 对应 */
    private readonly getScreenTop?: (pointerX: number, pointerY: number) => number,
    /** 立绘在窗口内的 **top**（上留白，回调版）；与 `marginY` 区分开，见 `clampToWorkArea` */
    private readonly getMarginTop?: () => number,
    /**
     * **右留白**（立绘右边 → 窗口右边，回调版）。
     *
     * ⚠️ 与 `marginX`（左留白）分开：立绘在画布里不是左右对称的（左 33 / 右 42），
     * 共用会让**右边缘的拖拽极限差 9px** —— 用户报「宠物在边缘时不时被挡住」。
     * 见 `clampToWorkArea` 的 `marginRight` 参数说明。
     */
    private readonly getMarginRight?: () => number,
    /**
     * 立绘**可见宽度**（回调版）。
     *
     * 用来把"立绘右边贴到屏幕右"直接换算成窗口的 x 极限 —— 见 `clampToWorkArea`
     * 里 `maxX` 的推导。缺省时退回旧的"窗口宽 + 留白"公式。
     */
    private readonly getArtWidth?: () => number,
  ) {}

  /** 取当前应放宽的像素数（横纵各自求值） */
  private margins(): { x: number; y: number } {
    const read = (value: number | (() => number)): number => {
      const v = typeof value === 'function' ? value() : value;
      return Number.isFinite(v) && v > 0 ? Math.round(v) : 0;
    };
    return { x: read(this.marginX), y: read(this.marginY) };
  }

  /** 取**上/右**留白（这两个也用回调，且同样需要在拖拽开始时定住） */
  private marginsExtra(): { top: number; right: number } {
    const read = (value: (() => number) | undefined): number => {
      if (!value) return 0;
      const v = value();
      return Number.isFinite(v) && v > 0 ? Math.round(v) : 0;
    };
    return { top: read(this.getMarginTop), right: read(this.getMarginRight) };
  }

  /** 按下：记住指针与窗口的相对关系。重复调用会以最后一次为准。 */
  begin(pointerX: number, pointerY: number): void {
    const b = this.getBounds();
    this.origin = {
      pointerX,
      pointerY,
      winX: b.x,
      winY: b.y,
      winWidth: b.width,
      winHeight: b.height,
    };
    /*
     * ⚠️ **拖拽开始时把留白"定住"**（2026-10-04 实测查到）。
     *
     * 留白是回调现算的：`右留白 = 窗口宽 − 立绘偏移 − 立绘宽`。
     * 而**探头会改窗口宽**（254 → 387），于是留白在拖拽过程中变小 →
     * `maxX` 变大 → 窗口被推出去 → 姿态切换 → 又变小 …… **来回挣**。
     *
     * 探针实测（分步拖到最右）：
     *
     *     … 窗口 x=1188 → 1226 → **1193** → 1226 …   ← 右边缘反复横跳
     *     松手后 251x336（常态）—— **没探头**
     *
     * 拖拽那一刻的几何是**常态**（还没探头），所以那时取的留白最准。
     * 定住之后整个拖拽过程用同一套极限，就不会自己和自己打架。
     */
    this.frozenMargins = this.margins();
    this.frozenExtra = this.marginsExtra();
  }

  /** 拖拽开始时定住的留白（见 `begin` 的说明） */
  private frozenMargins: { x: number; y: number } | null = null;
  /** 同上，上/右留白 */
  private frozenExtra: { top: number; right: number } | null = null;

  /** 移动：按指针位移平移窗口。没在拖拽时什么都不做（幂等，便于直接接事件流）。 */
  move(pointerX: number, pointerY: number): void {
    const origin = this.origin;
    if (!origin) return;
    // 取整：setBounds 需要整数，而 screenX 在部分环境下是小数。
    // 不取整会让窗口逐步累积亚像素误差，长时间拖拽后出现偏移感。
    let x = origin.winX + (pointerX - origin.pointerX);
    let y = origin.winY + (pointerY - origin.pointerY);

    // 夹紧：**触碰屏幕边缘之后就不再增减**。
    // 注意夹的是"算完之后的位置"而不是"位移量" —— 这样即使拖得很快、
    // 指针一下跳到屏幕外，窗口也只会停在边上，不会冲出去。
    // 而且夹紧只影响位置、不影响 origin：往回拖时窗口立刻跟手，
    // 不需要先把指针"拖回"屏幕内（那样会有粘滞感）。
    const workArea = this.getWorkArea?.(pointerX, pointerY);
    if (workArea) {
      const m = this.frozenMargins ?? this.margins();
      const ex = this.frozenExtra ?? this.marginsExtra();
      const clamped = clampToWorkArea(
        x,
        y,
        origin.winWidth,
        origin.winHeight,
        workArea,
        m.x,
        m.y,
        this.getScreenBottom?.(pointerX, pointerY),
        this.getScreenTop?.(pointerX, pointerY),
        ex.top,
        ex.right,
        this.getArtWidth?.(),
      );
      x = clamped.x;
      y = clamped.y;
    }

    this.setBounds({
      x: Math.round(x),
      y: Math.round(y),
      // 宽高原样带上 —— 这是防止"越拖越大"的关键
      width: origin.winWidth,
      height: origin.winHeight,
    });
  }

  end(): void {
    this.origin = null;
  }

  get dragging(): boolean {
    return this.origin !== null;
  }
}
