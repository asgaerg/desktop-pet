/**
 * 窗口尺寸与形态的**纯计算**（不依赖 electron，可测）。
 *
 * 抽出来的理由：窗口尺寸是这套 UI 里最容易算错、又最难在运行中看出问题的部分
 * —— 算小了立绘被裁，算大了多出一片透明区域**挡住下面应用的鼠标事件**
 * （透明窗口不会"看不见就不挡"，它照样吃掉点击）。所以这段必须有测试。
 */

/** 窗体的两种形态 */
export type PetForm = 'normal' | 'compact';

/** 立绘基准边长（scale = 1 时的像素数） */
export const PET_BASE_SIZE = 250;
/** 常规形态下输入卡片占的高度 */
export const DOCK_HEIGHT = 190;
/** 常规形态下左右留白 */
export const DOCK_PAD_X = 20;
/**
 * 紧凑形态（"边缘探头"）的留白。
 *
 * 这个形态是给"别的应用全屏时"用的 —— 用户正在看视频/打游戏，
 * 宠物要**贴着屏幕右下角**、只露一点点，尽量不挡内容。
 * 所以左右留白刻意很小。
 */
export const COMPACT_PAD_X = 8;
export const COMPACT_PAD_Y = 8;

/**
 * 紧凑形态下**露出屏幕的部分**占立绘的比例。
 *
 * 这是"探头"的关键：窗口大部分移出屏幕，只留一条露在边上，
 * 看起来像宠物从屏幕边缘探出半个身子。取值 0.62 ——
 * 再小就只剩一团颜色认不出，再大就不像"藏着"了。
 */
export const COMPACT_VISIBLE_RATIO = 0.62;

/**
 * 边缘探头：立绘倾斜角度（度）。
 *
 * 参考那个桌宠应用的设置里写明「以 45° 探头姿态窥视」，就照它来。
 */
export const PEEK_ANGLE_DEG = 45;

/**
 * 边缘探头时的**画布**尺寸 = **待机大小**（全程比例不变）。
 *
 * ⚠️ 这里必须返回 `petSizeFor(scale)`，**不能**写成 `windowSizeFor(...)` ——
 * 那会形成循环：`windowSizeFor` -> `peekWindowSizeFor` -> `peekCanvasSizeFor`
 * -> `windowSizeFor`，直接爆栈（实测 RangeError: Maximum call stack size exceeded）。
 *
 * 用户明确要求：「探头时立绘不用放大，整个动画时宠物全身比例不变，
 * 不追求它的变大效果」。所以画布就是待机那个尺寸，旋转与位移全交给 CSS。
 */
export function peekCanvasSizeFor(scale: number): number {
  return petSizeFor(scale);
}

/**
 * 探头（旋转 45°）时**窗口**需要的边长 —— 用精确几何算，不拍脑袋。
 *
 * ---- 为什么必须变大（用户报的现象 + 视频证据） ----
 *
 * 视频里探头时立绘右侧被切出一条**硬直的垂直边** —— 正常的旋转不会有直边，
 * 那是**窗口裁的**。
 *
 * 几何推算（scale = 1）：
 *   · 画布 240 贴窗口顶部，占 0..240；
 *   · 立绘盒 155×177 在画布内偏右下，中心约在画布坐标 (133, 132)；
 *   · 探头时画布绕**窗口中心**旋转 45°（渲染层在 canvas 上写 `rotate()`）；
 *   · 于是立绘相对窗口中心的偏移被旋转：dx=13, dy=12 -> 旋转后约 (0.7, 17.7)；
 *   · 立绘半对角 = √(155² + 177²) / 2 ≈ 117.6；
 *   · 需要的半边长 = 117.6 + 17.7 ≈ **135**（还要留一点余量）。
 *
 * 而常态窗口只有 240 宽 —— 半边长 120 < 135，左右各差约 15px。**必然被裁。**
 * 所以探头时窗口要放大到 2×135 + 余量 ≈ 280 以上。
 *
 * ⚠️ 这个函数**以前写好了却从未被调用**（`petWindow.ts` 只 import 了它），
 * 所以窗口一直用常态尺寸 —— 这就是裁剪的来源。
 */
export function peekWindowSizeFor(scale: number): { width: number; height: number } {
  const pet = petSizeFor(scale);
  // 立绘盒（逻辑像素）
  const artW = pet * ART_BOX_RATIO.w;
  const artH = pet * ART_BOX_RATIO.h;
  const artCx = pet * (ART_BOX_RATIO.x + ART_BOX_RATIO.w / 2);
  const artCy = pet * (ART_BOX_RATIO.y + ART_BOX_RATIO.h / 2);

  // 常态窗口：画布贴顶，窗口中心在 (pet/2, winH/2)
  const normal = windowSizeFor(scale, 'normal');
  const dx = artCx - pet / 2;
  const dy = artCy - normal.height / 2;

  // 绕窗口中心旋转 45° 后的偏移
  const rad = (PEEK_ANGLE_DEG * Math.PI) / 180;
  const rx = Math.abs(dx * Math.cos(rad) - dy * Math.sin(rad));
  const ry = Math.abs(dx * Math.sin(rad) + dy * Math.cos(rad));

  // 立绘的外接半径（半对角）+ 旋转后的偏移 = 需要的半边长（向上取整）
  const halfDiag = Math.sqrt(artW * artW + artH * artH) / 2;
  const needW = Math.ceil((halfDiag + rx) * 2) + PEEK_FIT_MARGIN;
  const needH = Math.ceil((halfDiag + ry) * 2) + PEEK_FIT_MARGIN;

  /*
   * ⚠️ 不能写成 `Math.max(normal.width, needW)` —— 会把余量吃掉。
   *
   * 实测 scale 1：立绘半对角 117.6 + 旋转偏移 17.7 = 135.3 -> 需要 271，
   * 而常态宽度 240 更大的一方若是常态宽度就变成 max(240, 271)=271（没问题），
   * 但如果算出的 needW 比常态**小**（比如小档位），max 取常态值本身是对的。
   * 真正要保证的是"**不小于需求值**"，所以直接取需要值，
   * 再确保不小于常态（不能因为旋转反而变小）。
   */
  return {
    width: Math.max(normal.width, needW),
    height: Math.max(normal.height, needH),
  };
}

/** 探头窗口比"刚好装下立绘"再多留的余量（像素） */
export const PEEK_FIT_MARGIN = 24;

/*
 * ⚠️ 这里曾经有一个 `PEEK_VERTICAL_COMPENSATION_RATIO = -0.375`
 * （抵消用户报的"探头时往下掉约 90px"）。**已删除**，原因见 `peekWindowBounds`
 * 里 rawY 的注释：那个下掉已被其他修复消掉，补偿反而抬过头，
 * 并在"恢复尺寸"时表现为拖拽结束往下跳。用户要求回退。
 */

/**
 * 换窗口尺寸时**保持立绘在屏幕上不动** —— 纯函数，便于自检。
 *
 * ## 参数是"立绘相对窗口的偏移"，不是"屏幕绝对位置"
 *
 * 直觉上"新窗口原点 = 立绘的屏幕位置"就完了。但那个值只在**上报那一刻**有效 ——
 * 窗口一旦被拖动就失效，而渲染层在拖拽期间**不上报**。
 * 实测用它做锚点会让窗口跳回旧位置（横向跳过 423px、493px 两次）。
 *
 * 相对偏移只描述"立绘在窗口内的位置"，正是换尺寸时要保住的东西：
 * 窗口原点按同样的偏移挪，立绘在屏幕上就不动。
 *
 * ⚠️ 还有一层：探头时这个偏移**会变**（渲染层给画布加了 `--peek-shift`，
 * 实测常态 0、探头 −50）。所以必须在**偏移已经切回常态之后**再调用 ——
 * 运行时通过 `pendingNormalResize` 等新上报到了才应用（见 petWindow）。
 *
 * @param current  变尺寸**之前**的窗口（屏幕坐标）
 * @param artOffset 立绘相对窗口原点的偏移（屏幕坐标差）；null 时按中心对齐兜底
 * @param target   目标窗口尺寸
 */
export function windowBoundsKeepingArt(
  current: { x: number; y: number; width: number; height: number },
  artOffset: { x: number; y: number } | null,
  target: { width: number; height: number },
): { x: number; y: number; width: number; height: number } {
  /*
   * ## ⚠️ 结论：**原点不动**（`delta = 0`）。这里来回错了三次，把推导写全。
   *
   * 我曾经在「加 offset」和「减 offset」之间来回改，两次都被用户实测打回：
   *   · 用「减」->「探头之后又往左上位移」（`offset = (51,47)`，往左上正是 `(-51,-47)`）
   *   · 用「加」-> 那次也有位移，但日志显示当时**尺寸也在变**（396 -> 323），
   *     说明那是"动画中途的脏 offset"造成，**不是符号问题** —— 我把两件事混了。
   *
   * ## 正确的推导（只用到两条已确认的事实）
   *
   * 事实①：**立绘贴窗口顶部**（HUD 实测：`canvas rect 0,0`、`artBox @ 120,134`）。
   * 事实②：**窗口比立绘高出来的部分全在下方**（给气泡留白）。
   *
   * 于是：窗口高度从 394 变到 323 时，立绘仍在窗口顶部 —— 它的屏幕位置只取决于
   * **窗口原点的 y**。要让它不动，`新原点.y = 旧原点.y`。
   * 横向同理（立绘横向居中，窗口宽度变化时原点跟着挪，立绘屏幕位置仍由原点决定）。
   *
   * **所以 delta 就是 0。** 之前一直在给一个"不该存在的修正量"调符号，
   * 自然怎么调都不对。
   *
   * ⚠️ 参数 `artOffset` 因此不再参与计算，**保留它只为两件事**：
   *   1. 判断"渲染层到底上报过没有"（没上报时退回中心对齐兜底）；
   *   2. 让调用方与日志能看出当时的姿态。
   * 将来若要重新引入位移修正，请先拿 HUD 复核"立绘屏幕"前后是否一致。
   */
  if (!artOffset) {
    /*
     * 还没测到立绘位置时，**也保持原点不动**。
     *
     * 曾经在这里退回"按中心对齐"（两轴各分一半）。但既然正确行为是"不动"，
     * 兜底就该同样是"不动" —— 两套行为只会在"测量到了/没测量到"之间制造分叉，
     * 而分叉正是这个函数反复出错的根源（同一件事有两套算法）。
     */
    return { x: current.x, y: current.y, width: target.width, height: target.height };
  }
  return { x: current.x, y: current.y, width: target.width, height: target.height };
}

/**
 * 边缘探头的**窗口定位** —— 纯函数，便于单测。
 *
 * ## 为什么要抽出来做纯函数
 *
 * 这块逻辑踩了三次坑（进探头飘 60px、出探头向下、探头立绘被压窄），
 * 每次都是"在 Electron 里手动试 → 看观感 → 猜结论"。观感不可复现、
 * 也没法回归测试，所以同样的错反复犯。
 *
 * 抽成纯函数之后可以断言**几何不变量**：
 *   · 换尺寸前后**立绘在屏幕上的位置不变**（这是"不飘"的定义）；
 *   · 探头窗口必须**装得下旋转后的立绘**（这是"不裁"的定义）。
 *
 * ## 钉住立绘的做法
 *
 * 只钉**交叉轴**：贴左右边时钉 Y（纵向不许动），贴上下边时钉 X。
 * 贴边那一维本来就该让立绘往屏幕外探出去，钉住它就不叫探头了。
 *
 * ⚠️ 不能假设"立绘在窗口里居中" —— 窗口为给气泡留空间而加高，
 * 立绘却**贴窗口顶部**，两者中心差着约 41px。前两次的 bug 全出在这个假设上。
 *
 * @param current    变尺寸**之前**的窗口
 * @param artInWindow 立绘在窗口内的左上角（含旋转/平移，来自渲染层实测）
 * @param target     目标窗口尺寸
 * @param side       贴哪条边
 * @param workArea   所在显示器的**整屏**边界（⚠️ 不是工作区，见下）
 * @param peekShift  贴边方向上要"让开留白"的量
 * @param overhang   贴上下边时，为让**立绘**够到屏幕边而需要的额外越界量
 *                   （= 当前窗口高 − 立绘高；见函数内注释）
 * @returns 变尺寸**之后**的窗口矩形
 *
 * ⚠️ `workArea` 传的是**屏幕边界**（`display.bounds`），不是工作区
 * （`display.workArea`，会排除任务栏）。
 *
 * 用户的关键提示：「有没有可能是屏幕上面宠物可以超出屏幕，下面宠物被压在任务栏上
 * 导致的」。实测证实：
 *
 *     常态: 立绘 top=718  bottom=895   工作区底=1040
 *           -> 立绘底边本来就离工作区底 **145px**（没贴到底）
 *     探头: 把窗口底边钉到工作区底 -> y=646
 *           -> 立绘被顶高 **72px**   ← 用户报的"变高"
 *
 * 根因：常态窗口**故意比立绘高**（给气泡留空间），那段透明区本来就该伸到任务栏
 * 下面。用工作区去钉/夹，那 72px 就被推回来。改用整屏之后，任务栏不再有特殊影响，
 * 上下两边也就对称了（"上面能超出、下面被压住"的不对称随之消失）。
 */
export function peekWindowBounds(
  current: { x: number; y: number; width: number; height: number },
  artInWindow: { x: number; y: number } | null,
  target: { width: number; height: number },
  side: PeekSide,
  workArea: { x: number; y: number; width: number; height: number },
  peekShift: number,
  overhang = 0,
  /**
   * 立绘的**可见宽度**（逻辑像素）。
   *
   * 贴右边缘时用来算"立绘右边贴屏幕右"的窗口 x —— 见下面 `rawX` 的推导。
   * 拿不到时该分支退回"原点不动"（至少不跳）。
   */
  artWidth?: number,
  /**
   * 实测的**旋转中心**（`--art-origin`，相对画布的像素坐标）。
   *
   * ## 为什么吸附要用它（2026-10-04 修"左边被裁"）
   *
   * 立绘旋转 45° 后，**可见范围会向一侧扩张** —— 扩张多少取决于旋转中心与
   * 立绘内容的相对位置。`artWidth`（未旋转的矩形）算出来的边**不等于**旋转后的边，
   * 于是"贴边"之后有一侧被窗口边界裁掉。实测（修复前）：
   *
   *     贴左：立绘可见像素 −89..187  ->  **左被裁 89px**
   *     贴右：立绘可见像素  −2..291  ->  正常
   *
   * 有了旋转中心 + 立绘矩形 + 角度，就能**解析算出旋转后的外接框**，
   * 吸附时按那个框对齐，两侧都不会越界。
   */
  artOrigin?: { x: number; y: number },
  /**
   * 立绘**未旋转**的矩形尺寸（宽高，逻辑像素）。
   *
   * 与 `artWidth` 的区别：`artWidth` 只宽度、给"贴右"用；这里是完整宽高，
   * 用来算旋转后的外接框。缺省时退回只用 `artWidth` 的旧算式。
   */
  artSize?: { width: number; height: number },
  /**
   * **光标吸附**（2026-10-16 新增，修「探头形态随抓取点变」）。
   *
   * ## 用户报的现象
   *
   * 「拖宠物头部到屏幕边缘旋转是一种样子，拖中部以下是另一种样子」。
   *
   * ## 根因（用户日志实锤）
   *
   * 横向早就有"固定位置"吸附（2026-10-04 用户要求），但**纵向是
   * `rawY = current.y`（原点不动）** —— 也就是松手时窗口在哪，探头就在哪。
   * 而松手时的窗口 y 由**抓取点 + 手的高度**共同决定：抓头部时头部跟着手走、
   * 抓中部时中部跟着手走，同一个"把宠物推到边上"的动作，立绘的纵向位置
   * 能差上百像素（日志实测结果 y 从 417 到 560）。倾斜 45° 之后再被屏幕
   * 底边裁一刀，观感就是两张完全不同的图。
   *
   * ## 语义
   *
   * 传了 `cursor` 时：**立绘中心对齐到光标松手点**（光标 Y 对贴左右边、
   * 光标 X 对贴上下边），并夹在屏幕内（立绘中心不出屏幕）。
   *
   * 为什么对光标而不是对窗口原点：光标位置与**抓哪里无关**（手在哪光标就在哪），
   * 抓取点带来的偏差在"窗口跟随光标"那一层已经发生了 —— 用光标做锚，
   * 同一次松手动作不管抓头部还是抓腹部，立绘中心都落在同一个高度。
   *
   * 不传（缺省）时保持旧行为（原点不动）—— 滑杆重排、全屏恢复等
   * 重入路径不该跳到光标处。
   */
  cursor?: { x: number; y: number } | null,
  /**
   * 立绘高度（未旋转、实测值）。光标吸附的纵向对齐要用；
   * 缺省（或 ≤0）时纵向退回"原点不动"。
   */
  artHeight?: number,
): { x: number; y: number; width: number; height: number } {
  const growX = Math.round((target.width - current.width) / 2);
  const growY = Math.round((target.height - current.height) / 2);

  // 立绘当前的屏幕位置（没有实测矩形时退回"窗口中心"，至少不会跳到左上角）
  const artScreenX = artInWindow ? current.x + artInWindow.x : null;
  const artScreenY = artInWindow ? current.y + artInWindow.y : null;
  const centerX = current.x + current.width / 2;
  // `centerY` 已无人使用 —— 纵向兜底改成了"原点不动"（见 rawY 的注释），
  // 探头的上下边路径也不需要它。留着会让 TS 报未用变量。
  void centerX;

  let rawX: number;
  let rawY: number;
  if (side === 'left' || side === 'right') {
    /*
     * ## 方案 A（2026-10-04 用户选定）：**原地转**，不让立绘"闪"出去
     *
     * 旧写法是
     *
     *     rawX = 右边 ? W - current.width + peekShift + growX : ...
     *
     * 展开看：窗口从 250x335 涨到 384x388（+134 宽），于是
     * `growX = +67`（把锚点从"窗口右边缘"改成"中心"，必然的几何）
     * 再加 `peekShift = +39` —— 实测窗口**一次往右搬 199px**
     * （日志：`进入探头后：窗口 1388,336`，而进之前 x≈1189）。
     *
     * 而**退出**探头时走的是"保持立绘不动"的路径，只回来 ~50px ——
     * **进 +199 / 出 -50 不对称**，观感就是"闪现"。用户的原话：
     * 「现在它探头结束会闪现」。
     *
     * ## 现在怎么算
     *
     * 目标：**立绘在进/出探头时屏幕位置不变**，进探头只做两件事 ——
     *   ① 转 45°（绕 `--art-origin-*` = 立绘中心）；
     *   ② 平移 `peekShift`（往屏幕边"探"一点，**由渲染层 `--peek-shift-x` 实现**）。
     *
     * 所以主进程这里只做两件修正，**不碰位移**：
     *   · `growX` —— 抵消"窗口变宽"（窗口 `left:0`，变宽会把它往右撑）；
     *   · 旋转修正项 —— 抵消"绕中心转 45° 后立绘中心的偏移"。
     *
     * ⚠️ 千万别在这里再加 `peekShift`：那会让位移**做两遍**
     * （渲染层一遍、窗口一遍）。实测残留 `Δ+52` 就是这么来的 ——
     * 详见下面 `rawX` 的注释。
     *
     * 拿不到 `artInWindow` 时退回"原点不动"（至少不跳）。
     */
    if (artInWindow) {
      /*
       * ## 吸附到**固定位置**（2026-10-04 用户要求）
       *
       * 用户原话：「鼠标拖拽太快了会导致位置不够标准，就设置鼠标拖拽到边缘的话
       * 就让宠物自动探头到**横向固定位置**，就是平移旋转到固定位置」。
       *
       * 也就是说：**不管松手时拖到哪**，只要判定为贴边，立绘就平移到同一个标准位置
       * —— 「立绘的可见边正好贴住屏幕边」。拖拽的快慢、松手的早晚都不影响结果。
       *
       * ## 算式（不需要任何旋转补偿，直接对齐边）
       *
       *     artInWindow.x / .y = 实测的"立绘左边/上边 → 窗口左边/上边"
       *     立绘左边(屏幕) = 窗口 x + artInWindow.x
       *
       *   贴左边：让立绘左边 = 屏幕左  =>  窗口 x = 屏幕左 − artInWindow.x
       *   贴右边：让立绘右边 = 屏幕右  =>  窗口 x = 屏幕右 − artInWindow.x − 立绘宽
       *
       * `artWidth` 拿不到时退回"原点不动"（至少不跳）。
       *
       * ## ⚠️ 左侧要加 `PEEK_LEFT_COMPENSATE_PX`（用户要求「加个补偿」）
       *
       * 立绘左右并不对称（旋转中心偏、外接框含阴影），于是**左侧会探得比右侧多**。
       * 实测（用未旋转矩形的基准中心算"离屏幕边多远"）：
       *
       *     右侧  中心离屏幕右 88.5px
       *     左侧  中心离屏幕左 55.5px    <- 多探出约 33px（视觉上约 7px）
       *
       * 用户观察到的就是这个：「左边的探头常态就是要比右边探的更多」。
       * 补偿量取 7（用户给的量级），把左侧窗口往回收一点。
       */
      const artW = artWidth != null && artWidth > 0 ? artWidth : 0;
      if (artW > 0) {
        /*
         * ## 吸附到**固定位置**（用户要求）
         *
         * 不管松手时拖到哪，只要判定为贴边，立绘就平移到同一个标准位置。
         *
         * ⚠️ 这里用**未旋转**的 `artInWindow.x` / `artW` 算边。
         *
         * 我曾改成"按立绘矩形绕旋转中心转 45° 算外接框"，想消掉"贴左时立绘左边
         * 被切"的问题。但实测**反而更差**：`artBox` 是含阴影的包围盒、旋转中心是
         * "立绘中心"，两个基准不同，解析值和实测先后差了 38px / 76px，
         * 窗口被推到屏幕外（HUD: `win -32 裁切 左58`）。用户决定回退。
         *
         * 探头时旋转外接框比画布宽，边缘会有少量越界 —— 那是**几何必然**，
         * 由渲染层的"画布居中"缓解（见 `updateCanvasOffset`），不在这里补偿。
         */
        rawX =
          side === 'right'
            ? workArea.x + workArea.width - artInWindow.x - artW + PEEK_X_COMPENSATE_RIGHT_PX
            : workArea.x - artInWindow.x + PEEK_X_COMPENSATE_LEFT_PX;
      } else {
        rawX = current.x;
      }
    } else {
      rawX = current.x;
    }
    void growX;
    /*
     * 纵向：**窗口原点不动**（`current.y`）。
     *
     * ## 用户要求：「不用加那个 33px，就先这样吧」
     *
     * 我曾试过 `artScreenY − artInWindow.y`（把"立绘屏幕位置"换算成窗口原点，
     * 即减掉立绘在窗口内的 34px 偏移）。用户否了 —— **不要动它**。
     *
     * 所以这里就是原点不动。它为什么是对的：
     *   · 立绘**贴窗口顶部**（`canvas rect 0,0`），窗口变高时多出来的部分
     *     全在**下方**（给气泡的留白）；
     *   · 所以"窗口原点 y 不变" ⇒ **立绘屏幕位置不变**。
     *
     * ## 而之前那个 bug 是另一回事
     *
     * 旧代码是 `artScreenY`（**没减偏移**，把立绘屏幕位置直接当原点），
     * 于是每个探头循环把窗口多推一个偏移量 —— 日志实测"每进一次 +33px"、越探越下。
     *
     * 也就是说：**`artScreenY` 是错的，"原点不动"是对的**，
     * 而 `artScreenY − 偏移` 是"数学上等价于原点不动"的另一种写法 ——
     * 用户选择了更直白的后者。
     *
     * ## 🔴 2026-10-16：传了 `cursor` 时改成**光标吸附**（见签名处的说明）
     *
     * 纵向"原点不动"意味着探头高度 = 松手高度 = **随抓取点变**。
     * 光标吸附后：立绘中心 = 光标松手点（夹在屏幕内），同一动作不管抓哪
     * 都是同一个探头姿态。
     */
    rawY =
      cursor != null && artInWindow != null && artHeight != null && artHeight > 0
        ? (() => {
            const centerInWindowY = artInWindow.y + artHeight / 2;
            // 立绘中心不许出屏幕（用传入的整屏边界夹；调用方贴边用的就是整屏）
            const lo = workArea.y + artHeight / 2;
            const hi = workArea.y + workArea.height - artHeight / 2;
            const desired = Math.min(Math.max(cursor.y, lo), hi);
            return desired - centerInWindowY;
          })()
        : current.y;
  } else {
    /*
     * 贴上下边：**窗口可以（也必须）越出屏幕**，否则宠物根本够不到那条边。
     *
     * ⚠️ 这里的关键点（用户提示「宠物过不去，之前好像修的都是能过去的」）：
     *
     *   窗口底边贴屏幕底 ≠ 宠物能探出去。
     *   因为常态窗口**比立绘高**（给气泡留空间），透明留白在**下方** ——
     *   实测 240x322 的窗口里，立绘只占上面约 240 高，下方留着约 82px 透明。
     *   所以要让**立绘**越过屏幕边，窗口底边必须到 `屏幕底 + 那 82px`。
     *
     * 越界量 = `overhang`（由调用方按"窗口高 − 立绘高"算出来）。
     * 实测确认 Windows **不会**把窗口夹回工作区（设定 y=776 能原样读回），
     * 所以只要算式给够，宠物就能压过任务栏。
     */
    rawY =
      side === 'bottom'
        ? workArea.y + workArea.height - current.height + peekShift + growY + overhang
        : workArea.y - peekShift - growY - overhang;
    /*
     * 横向同理：**窗口原点不动**。
     *
     * 立绘在窗口里也不是横向居中的（实测左留白约 51、右留白约 54），
     * 所以任何"按立绘位置反推原点"的写法都会引入偏差。
     * 旧代码 `rawX = artScreenX` 会每个循环多推一个横向偏移量。
     *
     * ## 🔴 2026-10-16：传了 `cursor` 时**光标吸附**（与左右边对称）
     *
     * 贴上下边时横向原跟随松手位置 —— 同样随抓取点变。改成
     * "立绘中心 X = 光标松手 X"（夹在屏幕内），理由见签名处的说明。
     */
    rawX =
      cursor != null && artInWindow != null && artWidth != null && artWidth > 0
        ? (() => {
            const centerInWindowX = artInWindow.x + artWidth / 2;
            const lo = workArea.x + artWidth / 2;
            const hi = workArea.x + workArea.width - artWidth / 2;
            const desired = Math.min(Math.max(cursor.x, lo), hi);
            return desired - centerInWindowX;
          })()
        : current.x;
  }

  return {
    /*
     * ⚠️ **交叉轴不夹紧**（贴左右边时不夹 Y、贴上下边时不夹 X）。
     *
     * 自检抓到的 bug（`标准240px：换尺寸前 y=718，换后推得 y=646`）：
     * 探头窗口比常态**高**（322 -> 394），贴屏幕底部时
     * `718 + 394 > 1040` 装不下，夹紧就把窗口顶到 `1040 - 394 = 646`
     * —— 立绘凭空上移 72px。这就是用户说的「边缘探头又再次变高了」。
     *
     * 道理上也不该夹：交叉轴与"贴哪条边"无关，夹它只会让立绘漂移；
     * 探头本来就是越出屏幕的姿态（贴边那一维本来就允许越界）。
     */
    x: Math.round(rawX),
    y: Math.round(rawY),
    width: target.width,
    height: target.height,
  };
}

/** 拖拽结束时，离屏幕边缘多近算"贴边"（逻辑像素） */
export const EDGE_SNAP_THRESHOLD = 40;

/** 贴边判定距离的默认值与可调范围（像素）。用户可在设置窗口里调。 */
export const EDGE_SNAP_DEFAULT_PX = EDGE_SNAP_THRESHOLD;
export const EDGE_SNAP_MIN_PX = 0;
export const EDGE_SNAP_MAX_PX = 200;

/** 把任意输入夹进贴边判定范围 */
export function clampEdgeSnap(value: unknown): number {
  const n = Number(value);
  if (!Number.isFinite(n)) return EDGE_SNAP_DEFAULT_PX;
  return Math.max(EDGE_SNAP_MIN_PX, Math.min(EDGE_SNAP_MAX_PX, Math.round(n)));
}

/** 缩放范围。低于 0.6 看不清，高于 2.4 会占掉小屏一大块。 */
export const MIN_SCALE = 0.6;
export const MAX_SCALE = 2.4;

/**
 * 「宠物大小」的可选档位 —— **唯一真源**。
 *
 * 用户要求：「宠物变大变小直接写几个标准大小吧，这样好修改维护，像它这样」，
 * 参考应用的写法是一个只列几个固定值的下拉框。
 *
 * ---- 为什么必须收敛成一份 ----
 *
 * 改这里之前，档位在**三处**各写了一遍，而且已经不一致：
 *   · 设置页：0.6~2.4 的**连续滑杆**（没有档位概念）
 *   · 托盘菜单：0.6 / 0.8 / 1.0 / 1.3 / 1.6 / 2.0（六档，硬编码）
 *   · 右键菜单：同上（第二份硬编码）
 * 结果就是"设置里调的档位，菜单里对不上"。现在三处都从这份清单生成。
 *
 * 为什么底层仍然用 **scale** 而不是直接存像素：
 *   · 存储格式不动 => 老配置（`petScale: 1.2` 之类）仍然有效，不需要迁移；
 *   · `petSizeFor(scale)` 已经在那里，像素是算出来的，两者不会对不上。
 * UI 上显示像素（用户看得懂"250 px"），菜单里显示倍率 + 像素都给。
 *
 * ## 档位（2026-10-04 用户指定：「150、220、250、300px 这个尺寸差不多」）
 *
 * 基准 `PET_BASE_SIZE = 250`，所以 1.0 正好是 250：
 *
 *   0.6 -> 150 · 0.88 -> 220 · 1.0 -> 250 · 1.2 -> 300
 *
 * ⚠️ 换基准会**改变老配置的实际大小**（老配置里 `petScale: 1` 原来 = 240，
 * 现在 = 250）。用户明确要求这套尺寸，所以接受这一次性变化。
 */
export const PET_SCALE_PRESETS: readonly { scale: number; label: string }[] = [
  { scale: 0.6, label: '150px' },
  { scale: 0.88, label: '220px' },
  { scale: 1, label: '250px' },
  { scale: 1.2, label: '300px' },
];

/** 取离给定 scale 最近的档位（老配置里可能是 1.2 这种非档位值） */
export function nearestScalePreset(scale: number): number {
  const target = clampScale(scale);
  let best = PET_SCALE_PRESETS[0].scale;
  let bestDiff = Number.POSITIVE_INFINITY;
  for (const preset of PET_SCALE_PRESETS) {
    const diff = Math.abs(preset.scale - target);
    if (diff < bestDiff) {
      bestDiff = diff;
      best = preset.scale;
    }
  }
  return best;
}

export function clampScale(scale: number): number {
  if (!Number.isFinite(scale)) return 1;
  return Math.max(MIN_SCALE, Math.min(MAX_SCALE, scale));
}

/** 立绘边长 */
export function petSizeFor(scale: number): number {
  return Math.round(PET_BASE_SIZE * clampScale(scale));
}

/**
 * 立绘在画布里的**实际位置与尺寸**（占画布尺寸的比例）。
 *
 * ## 2026-10-04：换成**实测值**（原来是旧布局量出来的，早就不准）
 *
 * 旧值是 `x:96/240, y:114/240, w:155/240, h:177/240` —— 注释里自己也承认
 * "布局改过几轮之后已经不准了，不要再依赖它做定位"。但 `artSizeFor` 仍在用它
 * 算"拖拽要放宽多少"，所以一直是错的。
 *
 * 现在改用**当前链路下的实测值**（`canvas 240x240`、裁切区 300x300、缩放 0.8）：
 *
 *     立绘可见 195x245，位置 (22, 15)
 *     => 比例 x:0.092  y:0.062  w:0.812  h:1.021
 *
 * ⚠️ `h > 1` 是**正常的**：立绘比画布高（画布只显示到腰部以下就被截），
 * 因为源里角色是**竖向**的（328x306），而画布是正方形。
 *
 * 这也是"画布该不该改竖向"的依据 —— 竖向画布能让 `h/w` 更接近角色的天然比例，
 * 但**画布宽度受"窗口宽 = 画布宽"这条约束锁住**，所以单改宽高比收益很小
 * （实测只多 3%）。详见交接文档。
 *
 * 重新测量的方法：开 `PET_DEBUG_HUD=1` 看 HUD 的 `artBox` 一行，除以画布尺寸即可。
 */
export const ART_BOX_RATIO = {
  x: 22 / 240,
  y: 15 / 240,
  w: 195 / 240,
  h: 245 / 240,
} as const;

/**
 * 立绘实际可见内容的外接尺寸（逻辑像素）。
 *
 * 注意这**不是**画布尺寸：画布里有一大圈透明留白。
 */
export function artSizeFor(scale: number): { width: number; height: number } {
  const pet = petSizeFor(scale);
  return {
    width: Math.round(pet * ART_BOX_RATIO.w),
    height: Math.round(pet * ART_BOX_RATIO.h),
  };
}

/**
 * 要让立绘在窗口里**居中**，画布需要平移多少（逻辑像素，直接给 CSS translate）。
 *
 * 立绘可见盒在画布里的中心是 `(x + w/2, y + h/2)`，画布中心是 `pet/2`，
 * 差值就是要把画布挪多少。渲染层拿它做 `translate`。
 *
 * ⚠️ 不居中的后果很实在：实测立绘偏右下（画布 240 里 x0=96 y0=114），
 * 于是"窗口贴到屏幕边"时立绘还离边 96~114px，贴边判定怎么调都不对 ——
 * 用户为这个报了三次。
 */
export function canvasBoxFor(scale: number): {
  size: number;
  contentScaleX: number;
  contentScaleY: number;
} {
  const art = artSizeFor(scale);
  const size = Math.max(art.width, art.height);
  return {
    size,
    contentScaleX: size > 0 ? art.width / size : 1,
    contentScaleY: size > 0 ? art.height / size : 1,
  };
}

/**
 * 窗口尺寸 —— 正方形，边长 = 画布边长（= 立绘基准尺寸）。
 *
 * ⚠️ 走过一段弯路，把结论记清楚，免得以后又绕回去。
 *
 * 用户的要求是「让立绘能贴到屏幕边缘触发探头」，我一度以为要**缩小窗口**
 * （先试 240 正方形、又试 155×177 长方形贴合立绘）。那条路的问题：
 *   · 长方形窗口装不下旋转 45° 的立绘（155×177 旋转后需 235）-> 必须缩到 74%，
 *     旋转时立绘变小，与"全身比例不变"冲突；
 *   · 缩窗口还会让立绘被窗口裁，边缘出现难看的切边。
 *
 * **用户的解法更简单也更对**（原话）：
 *   「把随鼠标拖拽增减的极限增加窗口的留白就行了」
 * 窗口尺寸**不动**，只把**拖拽的夹紧极限放宽那圈留白** ——
 * 见 `peekShiftFor()` 与 `dragTracker` 的 `clampMargin`。
 * 这样立绘可见边能真正压到屏幕边上，而窗口、画布、旋转全都不用改。
 *
 * 尺寸**恒定**（两形态一致）是前提：`setBounds` 改尺寸是一帧硬跳、
 * 做不出过渡，形态切换只靠渲染层的 CSS transform 做动画。
 */
export function windowSizeFor(scale: number, form: PetForm): { width: number; height: number } {
  void form;
  const pet = petSizeFor(scale);
  /*
   * 加高量：按比例给（随宠物大小缩放），这样"气泡空间/宠物高度"的关系恒定。
   *
   * ---- ⚠️ 关于"能不能靠加高给气泡腾出更多上方空间" ----
   *
   * 实测结论：**不能**。加高到 `0.85`（240 -> 447）之后，立绘头顶以上的空间
   * 仍然是 34px，与 240 见方时一模一样。原因是几何决定的：
   *   · 画布贴窗口左上角（`--art-shift-y` 只做画布内的居中）；
   *   · 多出来的高度只能落在**画布下方**；
   *   · 所以"头顶到窗口顶"的距离恒等于 34px，跟窗口多高无关。
   *
   * 想真正增加头顶以上的空间只有两条路，各有代价：
   *   ① 把画布/立绘在窗口里**下移** —— 等于宠物在屏幕上更靠下，
   *      而用户已实测否掉（「靠下了，超出了一部分，顶上被挡住了一部分」）；
   *   ② 把窗口**整体上移**（宠物屏幕位置不变，窗口顶伸到屏幕外更多）——
   *      宠物本来就贴着工作区下沿，上移受屏幕顶部限制，且会改变贴边判定。
   *
   * 所以这里给一个**刚好够用**的值：让气泡有一行的余地，
   * 更长的回复靠 `updateBubbleSide` 往侧面让（用户认过的方案）。
   */
  const extra = Math.round(pet * 0.34);
  return { width: pet, height: pet + extra };
}

/**
 * 探头（旋转 45°）时的缩放倍率 —— **恒为 1，不缩放**。
 *
 * 用户明确要求：「旋转不必缩小，你可以试试」以及「全身比例不变」。
 * 窗=画布=240 正方形时，立绘 155×177 旋转后的外接是 235 < 240，
 * 装得下、不会被裁 —— 这正是不把窗口贴成长方形的原因。
 */
export function peekFitScaleFor(scale: number): number {
  void scale;
  return 1;
}

/**
 * 立绘居中需要的画布平移量（逻辑像素）。
 *
 * 画布 240 见方、立绘可见盒偏右下（实测 x0=96 y0=114、155×177）。
 * 不平移的话"窗口贴边"与"立绘贴边"会差上百像素（用户为这个报过三次）。
 */
export function artShiftFor(scale: number): { x: number; y: number } {
  const pet = petSizeFor(scale);
  const cx = pet * (ART_BOX_RATIO.x + ART_BOX_RATIO.w / 2);
  const cy = pet * (ART_BOX_RATIO.y + ART_BOX_RATIO.h / 2);
  return { x: Math.round(cx - pet / 2), y: Math.round(cy - pet / 2) };
}

/**
 * 立绘在窗口内的**目标中心**（相对窗口左上角，逻辑像素）。
 *
 * ⚠️ 纵向**不能**用"窗口高的一半"。
 *
 * 用户要求「可以加高宠物窗口」之后，窗口变成 240x325，而加高的部分**只加在下方**
 * （见 `windowSizeFor` 的说明）。若这里仍返回 `win.height / 2`，加高出来的
 * 那部分就会被当成"上半空间"，把立绘整体往下推 —— 等价于宠物在屏幕上跳了半截，
 * 而顶部凭空多出一大块空白（实测画布 top=-20、下端留白 104px）。
 *
 * 所以纵向用**画布边长的一半**：画布是 `petSizeFor(scale)` 见方、贴窗口顶部，
 * 立绘在画布内居中 —— 与加高前完全一致，宠物位置不变。
 *
 * 抽成纯函数是为了可测：这个偏移算错的表现是"宠物偏到窗口一角"，
 * 而且很容易被误认成旋转逻辑的问题（查起来会绕远路）。
 */
export function petCenterInWindow(
  scale: number,
  form: PetForm,
): { x: number; y: number } {
  const win = windowSizeFor(scale, form);
  // 画布恒为 petSizeFor(scale) 见方、贴窗口左上角；立绘在画布内居中
  const canvas = petSizeFor(scale);
  return { x: Math.round(win.width / 2), y: Math.round(canvas / 2) };
}

/**
 * 探头时，立绘要从窗口中心往屏幕边方向挪多远（逻辑像素）。
 *
 * 长方形窗口下**这个值趋近 0** —— 窗口边就是立绘边，所以"窗口贴到屏幕边"
 * 已经等于"立绘贴到屏幕边"，不需要再挪。保留这个函数是为了：
 *   ① 主进程/渲染层的调用签名不变；
 *   ② 万一以后窗口比立绘大（换素材），位移量会自动算出来而不用改代码。
 */
export function peekShiftFor(scale: number): number {
  const win = windowSizeFor(scale, 'compact');
  const art = artSizeFor(scale);
  return Math.max(0, Math.round((win.width - art.width) / 2));
}

/**
 * 垂直方向同理（紧凑模式下贴上下边时用；目前探头只认左右，留着备用）。
 */
export function peekShiftYFor(scale: number): number {
  const win = windowSizeFor(scale, 'compact');
  const art = artSizeFor(scale);
  return Math.max(0, Math.round((win.height - art.height) / 2));
}

/**
 * 探头贴的是哪条屏幕边。
 *
 * 一开始只支持左右（参考应用只说「拖到屏幕左/右边缘」），用户后来要求
 * 「屏幕上下也加个」，于是扩成四条边 —— 上下也用同样的"贴边即旋转"逻辑。
 */
export type PeekSide = 'left' | 'right' | 'top' | 'bottom';
/**
 * 判断拖拽结束时是否"贴边"，以及贴的是哪一边。
 *
 * 只认左右边缘：参考应用说的是「拖到屏幕左/右边缘」，而上下边贴上去
 * 在视觉上不像"探头"（更像卡住）。
 *
 * ---- ⚠️ `artMargin` 是必须的，这是用户第二次报同一个问题 ----
 *
 * 窗口比立绘大一圈（旋转 45° 需要的几何差，见 `windowSizeFor`），
 * 立绘居中，所以窗口边缘到**立绘可见边缘**还有 `artMargin` 的距离。
 * 判定必须按**立绘**算，否则用户看到的是：
 *   「宠物离屏幕边还有好几十像素，它就自己探头了」（实测报过两次）。
 *
 * 也就是说：`窗口到屏幕边的距离 + artMargin <= threshold` 才该触发。
 * 等价于"立绘可见边缘到屏幕边的距离 <= threshold"。
 *
 * ⚠️ 符号别搞反（我第一版就写反了）：窗口比立绘大，**窗口边缘比立绘边缘
 * 更靠外**，所以"窗口 gap"**小于**"立绘 gap" —— 要加 margin 才能还原立绘。
 * 例：窗口 gap=10、margin=64 -> 立绘 gap=74。写反的话会得到 -54，
 * 变成"永远触发"，比不修还糟。
 */
export function sideAtEdge(
  bounds: { x: number; y: number; width: number; height: number },
  workArea: { x: number; y: number; width: number; height: number },
  threshold = EDGE_SNAP_THRESHOLD,
  /**
   * 窗口边缘到立绘可见边缘的距离。
   *
   * 横向用 `(窗口宽 - 立绘宽)/2`，纵向用 `(窗口高 - 立绘高)/2` ——
   * 两者通常**不相等**（立绘是 155x177，不是正方形），所以分开传。
   */
  artMarginX = 0,
  /** 纵向的同理（见 artMarginX 的说明） */
  artMarginY = 0,
  /**
   * 渲染层实测的"立绘左边/上边到窗口原点的距离"（屏幕像素）。
   *
   * 有它就用"立绘真实边缘"判贴边（左右天然对称，见上面的长注释）；
   * 没有则退回旧的"窗口 gap + margin"写法。
   */
  artOffsetInWindow: { x: number; y: number } | null = null,
  /** 立绘可见尺寸（用于算右/下边）。缺省时退回旧写法。 */
  artSize: { width: number; height: number } | null = null,
): PeekSide | null {
  /*
   * ## ⚠️ 左右不对称的根因（2026-10-04 用户报的）
   *
   * 用户原话：「左边探头就是正常立绘碰到边缘就探头，但是右边的话是碰到窗口就
   * 过不去了，就开始探头了……应该还是和左边一样由立绘的碰撞判定的」。
   *
   * 旧写法是"窗口 gap **加** margin"：
   *
   *     const artLeft  = leftGap  + artMarginX;
   *     const artRight = rightGap + artMarginX;
   *
   * 那个 `+` 的前提是**窗口还在屏幕内**（窗口边比立绘边更靠外，加回去才还原立绘）。
   * 但贴右边缘时窗口**已经越界**（实测日志里 `rightGap` 是**负数**），
   * 于是 `+ margin` 让结果**更负** —— 方向反了，判据变成"离边缘负得足够多"，
   * 表现就是"窗口一碰到边界就探头"。左边 `leftGap` 是正的，所以只有右边错。
   *
   * ## 正确做法：直接用**立绘的真实边缘**，不做任何加减
   *
   * `artOffsetInWindow` 是渲染层实测上报的"立绘左/上边到窗口原点的距离"
   * （实测约 `(51,47)`，**不是** `(窗口−立绘)/2` —— 立绘在窗口里并不居中）。
   * 于是立绘四条边的屏幕坐标可以直接写出来：
   *
   *     立绘左 = bounds.x + offset.x
   *     立绘右 = 立绘左 + artWidth
   *
   * 这样不论窗口在不在屏幕内都成立，左右天然对称。
   *
   * 拿不到实测值时退回旧的"窗口 + margin"写法（至少不会全不触发）。
   */
  const offset = artOffsetInWindow;
  let artLeft: number;
  let artRight: number;
  if (offset && artSize && artSize.width > 0) {
    const left = bounds.x + offset.x;
    artLeft = left - workArea.x;
    artRight = workArea.x + workArea.width - (left + artSize.width);
  } else {
    const leftGap = bounds.x - workArea.x;
    const rightGap = workArea.x + workArea.width - (bounds.x + bounds.width);
    artLeft = leftGap + artMarginX;
    artRight = rightGap + artMarginX;
  }
  // ⚠️ **只判左右，上下不探头**。
  //
  // 用户原话：「上下不用探头，不超出屏幕边缘就好了」。
  // （他之前说"屏幕上下也加个"，我理解成上下也要旋转；实际意思是**上下也要能贴到
  // 屏幕边、别越界**。现在上下交给拖拽夹紧负责 —— 见 dragTracker 的 marginY。）
  //
  // 纵向留白仍要收下：拖拽夹紧要用它让窗口多走出去那一段，立绘底边才碰得到屏幕底。
  // 这里声明 `void` 表示"故意不用"，免得后来人以为漏了。
  void artMarginY;
  // 取**最近**的那条边，而且必须在阈值内。左右一起比：贴角时选更近的那条，
  // 避免"同时满足两条边"时行为不确定。
  let best: PeekSide | null = null;
  let bestGap = Number.POSITIVE_INFINITY;
  for (const [side, gap] of [
    ['left', artLeft],
    ['right', artRight],
  ] as const) {
    if (gap <= threshold && gap < bestGap) {
      best = side;
      bestGap = gap;
    }
  }
  return best;
}

/**
 * 边缘探头定位：把窗口压到屏幕边缘上，只留一部分露在里面。
 *
 * 与 `anchorPeekRight`（全屏时用的那个）不同，这个用于**手动贴边**的场景：
 * 用户把宠物拖到边缘后，它侧身贴着边窥视。所以窗口保持完整宽度（不裁掉大半），
 * 只是**紧贴边缘**，由渲染层负责把立绘旋转 45°。
 */
export function anchorAtEdge(
  workArea: { x: number; y: number; width: number; height: number },
  target: { width: number; height: number },
  side: PeekSide,
  /** 贴边时底边离工作区底部的距离（留一点，不要贴死任务栏） */
  bottomInset = 24,
  /**
   * 窗口伸出屏幕的**像素数**。
   *
   * 0 = 完全在屏幕内（立绘离边缘留一条缝，像浮在旁边）；
   * 越大 = 露出的部分越少。用户可以在设置窗口里自己调这个值。
   */
  offsetPx = Math.round(target.width * PEEK_EDGE_OVERLAP),
): { x: number; y: number; width: number; height: number } {
  // **故意让窗口伸出去一部分**，而不是刚好贴住边缘。
  //
  // 为什么：画布在窗口里是居中的，窗口若完全在屏幕内，立绘离边缘就会留一条缝，
  // 看起来像"浮在旁边"而不是"从边上探出来"。让窗口压出去一截，
  // 画面被屏幕边缘自然切断，才有探头的味道。
  const overhang = Math.max(0, Math.min(Math.round(offsetPx), target.width));
  const x = side === 'left'
    ? workArea.x - overhang
    : workArea.x + workArea.width - target.width + overhang;
  // **贴底**而不是居中：参考效果里宠物是趴在屏幕右下角探头的，
  // 垂直居中会让它悬在半空，看起来像"飘着"。
  const y = workArea.y + workArea.height - target.height - bottomInset;
  return { x: Math.round(x), y: Math.round(y), width: target.width, height: target.height };
}

/**
 * 贴边探头时窗口**伸出屏幕**的默认比例。
 *
 * 只在没设置过像素值、又拿不到窗口宽度时兜底用。
 * 真正生效的是用户可调的像素数（见 `PEEK_OFFSET_DEFAULT_PX`）。
 */
export const PEEK_EDGE_OVERLAP = 0.34;

/**
 * 气泡**存在时长**的默认值（毫秒）。
 *
 * 用户要求：「气泡默认时长改成 60 秒」。
 *
 * ⚠️ 放在 `petLayout` 而不是 `settingsStore`：`petWindow` 也要用它当字段初值，
 * 而 `petWindow` 导入 `settingsStore` 会形成循环依赖。这里是与"窗口/气泡布局"
 * 相关的常量集中地（`PEEK_OFFSET_DEFAULT_PX` 也在这），两边都从这里引。
 */
export const BUBBLE_LINGER_DEFAULT_MS = 60_000;

/*
 * ## ⚠️ 探头横向补偿 —— **已回退为 0**（2026-10-04）
 *
 * 用户报「左边的探头比右边探得更多」时，我试过用窗口位置补偿去修，一路加到 58，
 * 结果**引入了新 bug**：补偿把窗口推出屏幕，立绘左边被屏幕边界切掉
 * （HUD 实测 `裁切 左58`）。用户最后决定回退。
 *
 * 回退到 0 之后，位置完全由"吸附到 `artInWindow.x` 决定的固定位置"给出，
 * 左右都不做额外补偿。**不要再往这里加数** —— 需要调整请走实测闭环，
 * 并同时验证"窗口没有跑出屏幕"（那个 bug 就是这么来的）。
 */
export const PEEK_X_COMPENSATE_LEFT_PX = 0;
export const PEEK_X_COMPENSATE_RIGHT_PX = 0;

/**
 * 用户在探头时**额外平移**的像素数（设置里的「探头时的平移距离」）。
 *
 * ⚠️ 语义又变过一次，两次都记下来免得再混：
 *   ① 最初："窗口伸出屏幕多少"（那时靠把窗口挪出去做探头效果）。
 *   ② 中间：`peekOffsetPx` 曾按"探头时离边缘内缩"给用户看，
 *      但那个值**根本没被任何地方消费**（`setPeekOffset` 只存不读），
 *      所以滑杆拖了没反应 —— 一个空滑杆。
 *   ③ 中间：它是**在"立绘正好贴到屏幕边"的基础上再偏移多少**。
 *      0 = 正好贴边；调大 = 探出去更多。
 *      当时的位移 = 让开留白所需量 + 这个值（见 `petWindow.applyPeekLayout`）。
 *   ④ 现在（用户要求「探头时的平移距离**默认 68px**，重启有效，但依然可以自行调节」）：
 *      **吸附定位已经在窗口层面把"贴边"做完了**，所以这里不再叠加"让开留白"，
 *      这一项就是**纯粹往外探出去多少**。默认 68。
 *
 * ⚠️ 默认值从 0 改成 68 会**改变老配置的表现**（老配置里存的是 0）。
 * 由 `settingsStore` 的 `peekOffsetDefaultMigrated` 标记做一次性迁移 ——
 * 见那里的说明。
 */
export const PEEK_OFFSET_DEFAULT_PX = 68;
/**
 * 可调范围（像素）。
 *
 * 用户要求：「探头时的平移距离加个负数，这样就能前后调节了」。
 *
 *   · **上限 160**：位移量 = 让开留白(约43) + 这个值，
 *     再大立绘会整个跑到屏幕外、只剩一小条，失去"探头"的意思。
 *   · **下限 -60**：负数表示**往回拉**，也就是比"正好贴边"更收敛 ——
 *     立绘退回到屏幕里一点，露出的部分更多。
 *     取 -60 而不是跟上限对称：再往回拉就基本看不出是"探头"了
 *     （留白的让开量只有约 43，拉过 -43 就完全退回常态位置）。
 */
export const PEEK_OFFSET_MIN_PX = -60;
export const PEEK_OFFSET_MAX_PX = 160;

/** 把任意输入夹进合法范围（设置窗口是跨进程数据，不能假定形状） */
export function clampPeekOffset(value: unknown): number {
  const n = Number(value);
  if (!Number.isFinite(n)) return PEEK_OFFSET_DEFAULT_PX;
  return Math.max(PEEK_OFFSET_MIN_PX, Math.min(PEEK_OFFSET_MAX_PX, Math.round(n)));
}

/**
 * 右下角锚定地重排窗口位置。
 *
 * 宠物默认待在右下角，所以放大或切换到紧凑形态时应当**向左上方向长**，
 * 而不是把窗口整体推出屏幕。最后再夹进 workArea。
 *
 * 用右下角而不是左上角，是因为"宠物在右下角"是这个产品的基本形态：
 * 用户把窗口拖到别处后，改大小仍应保持他放的那个角。
 */
export function anchorBottomRight(
  current: { x: number; y: number; width: number; height: number },
  target: { width: number; height: number },
  workArea: { x: number; y: number; width: number; height: number },
): { x: number; y: number; width: number; height: number } {
  const right = current.x + current.width;
  const bottom = current.y + current.height;
  const x = Math.max(
    workArea.x,
    Math.min(right - target.width, workArea.x + workArea.width - target.width),
  );
  const y = Math.max(
    workArea.y,
    Math.min(bottom - target.height, workArea.y + workArea.height - target.height),
  );
  return { x: Math.round(x), y: Math.round(y), width: target.width, height: target.height };
}

/**
 * "边缘探头"定位：窗口**大半移出屏幕右侧**，只留一条露在边上。
 *
 * 为什么是"移出屏幕"而不是"缩小"：
 *   这套素材（106 段 webm）**没有任何趴着/躺着的姿势** —— 我抽了好几段
 *   名字像的（原地小憩沉眠、打瞌睡被惊醒、垂头叹气）逐帧看过，全是站着的，
 *   只是闭眼或眯眼。所以"探头"只能靠**程序把窗口挪出屏幕**做出来，
 *   靠动画是做不到的。
 *
 * 为什么要用 setBounds 而不是 setPosition：
 *   1.25 DPI 下 setPosition 每次会让窗口涨约 1px（踩过，拖 15 次涨 15px）。
 *   而且移出屏幕后 `setPosition` 拿到的坐标可能已被系统夹过，算不回原值。
 *
 * 注意：**必须允许 x 超过 workArea 右边缘**，否则夹回工作区就露不出探头效果。
 */
export function anchorPeekRight(
  workArea: { x: number; y: number; width: number; height: number },
  target: { width: number; height: number },
  visibleRatio = COMPACT_VISIBLE_RATIO,
  /**
   * 从哪一侧探头。默认右侧（宠物默认待右下角）。
   * 左边是给用户把宠物拖到屏幕左侧时用的。
   */
  side: 'right' | 'left' = 'right',
): { x: number; y: number; width: number; height: number } {
  const visible = Math.round(target.width * Math.max(0.2, Math.min(0.95, visibleRatio)));
  const x =
    side === 'right'
      ? workArea.x + workArea.width - visible
      : workArea.x - (target.width - visible);
  // 纵向贴底（含任务栏区域，因为全屏时用户看不到任务栏）
  const y = workArea.y + workArea.height - target.height;
  return { x: Math.round(x), y: Math.round(y), width: target.width, height: target.height };
}
