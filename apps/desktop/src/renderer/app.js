/**
 * 渲染层主逻辑：把协议事件翻译成"宠物在说话/在困/被吓到"的表现。
 *
 * 这一层**不做任何业务判断** —— 说什么、要不要开口，全由 Python 那边的
 * 人格与自主调度决定。渲染层只负责：
 *   · 流式文字怎么显示；
 *   · 状态机怎么切（idle/talk/blink/sleep）；
 *   · 口型与情绪怎么映射到立绘参数；
 *   · 点击穿透何时开、何时关。
 */

import { SpritePlayer } from './sprite.js';
import { VoiceIO } from './voice-io.js';

// 立绘的逻辑尺寸。
//
// ⚠️ **改尺寸要改这里，不是改 CSS / HTML 的 canvas 宽高。**
// sprite.ts 的 resize() 会把 canvas 的 width/height 属性**和** style 一起
// 按这个值重写，所以 CSS 里写的尺寸会被它覆盖掉（我改 CSS 时就被这一点骗过：
// 明明把 canvas 改成了 240，量出来还是 260）。
/*
 * ⚠️ **这个值只是"画出第一帧之前"的临时尺寸** —— 真实尺寸由主进程通过
 * `pet:set-pet-size` 下发（`petLayout.petSizeFor(scale)`，基准 250）。
 *
 * 踩过的坑（2026-10-04）：主进程**只在 `applyLayout` 里下发尺寸**，而那条
 * 路径启动时不一定跑 —— 于是窗口是 250x335、画布却停在这个 240，
 * 两者不一致导致所有"立绘在窗口内偏移"的计算都偏，用户报
 * 「宠物有时候能到边缘，有时候不能」（拖拽极限差 17px）。
 *
 * 现在主进程在 `did-finish-load` 后**主动发两次**（立刻 + 800ms），
 * 所以这个常量顶多影响最初那几百毫秒。
 */
const PET_SIZE = 240;
/** 说完话后气泡停留多久（毫秒）。太短看不清，太长糊在桌面上。 */
const BUBBLE_LINGER_MS = 12_000;

/**
 * 点击宠物的本地反应动画时长（毫秒）。
 *
 * 点击**不再产生 AI 内容**（用户 2026-10-07 要求），所以反馈必须来自本地
 * —— 这个时长就是"被摸一下"的表情该持续多久。太长会显得卡在表情上，
 * 太短看不见。800ms 约等于"愣了一下再回神"。
 */
const POKE_REACTION_MS = 800;

const $ = (id) => document.getElementById(id);

const els = {
  canvas: $('pet'),
  /** 画布的包装层：只承担**位移**（canvas 自己的 transform 被 rotate 占用） */
  canvasWrap: $('pet-wrap'),
  toast: $('toast'),
  /*
   * ⚠️ 文字弹窗（气泡）已移到**独立窗口** `bubble.html` / `bubble.js`。
   *
   * 宠物窗口里头顶以上只有 34px（画布贴窗口顶，加高也改变不了这一点），
   * 长回复必然压到宠物脸，所以气泡独立成窗口 —— 位置只受屏幕工作区约束。
   *
   * 这里刻意留成 `null` 而不是把字段删掉：下面还残留一批定位/尺寸函数
   * （`updateBubbleBottom` 等），它们会先判 `if (!els.bubble) return` 从而成为
   * **空操作**。删字段会让那些判断读到 `undefined`，而每个未加守卫的
   * `els.bubble.xxx` 都会抛 TypeError。
   *
   * 代价是契约检查会看到"声明了但 HTML 里没有"的元素 —— 这是**有意的**。
   */
  bubble: null,
  bubbleText: null,
  typing: null,
};

// --------------------------------------------------------------------------- //
// 立绘
// --------------------------------------------------------------------------- //

const sprite = new SpritePlayer({
  canvas: els.canvas,
  basePath: 'assets/avatar/default',
  width: PET_SIZE,
  height: PET_SIZE,
});

sprite.start();
/*
 * ⚠️ 启动时的画布对齐**不在这里调** —— 见文件末尾的 `startupSyncCanvasFallback()`。
 * 它读 `window.innerWidth`，要等布局稳定；早调也会踩下面的 TDZ。
 */
void sprite.load().then((mode) => {
  console.info(`[pet] 立绘模式：${mode === 'sprite' ? '序列帧素材' : '程序化占位'}`);
  /*
   * 注册"第一帧抠好并画到画布"的回调 —— 撤销首帧门闸、让立绘淡入。
   *
   * 用户要求：「干脆等立绘完全加载好再显示好了」。
   *
   * ⚠️ 必须**在 load() 之后**注册：`load()` 里才创建 videoPlayer
   * （`sprite.onFirstFrame` 是转发给它，创建之前调是空操作）。
   * 这也是为什么不能写在构造之后。
   */
  sprite.onFirstFrame(markArtReady);
});

/**
 * 把关键内部状态挂到 window 上。
 *
 * 两个用途，都不是"调试残留"：
 *   1. 测试台需要在没有界面的环境里断言真实状态（切到 talk 了没有、帧有没有走），
 *      否则界面逻辑就完全无法验证；
 *   2. 出问题时能在 devtools 里直接把 `__pet` 打出来看，比翻日志快得多。
 * 只暴露读取用的引用，没有任何写入口，不会成为绕过状态机的后门。
 */
window.__pet = {
  sprite,
  els,
  get audioStats() {
    return audio ? audio.stats : null;
  },
};

/*
 * ⚠️ 这里原来挂着 `window.__petDebug`（标定"探头旋转补偿"系数用）—— **已删除**。
 *
 * 那个补偿是为"探头时立绘往右下甩"加的，后来查明真正的原因是两个几何错误
 * （`petLayout.ts` 的 `rawY` 漏了立绘偏移、`styles.css` 的 `transform-origin`
 * 用了偏下 27px 的窗口中心）。根因修掉后补偿恒为 0，用户要求删掉。
 */

// --------------------------------------------------------------------------- //
// 气泡
// --------------------------------------------------------------------------- //
// 说话展示（气泡已按用户要求删除，这里先留空实现）
// --------------------------------------------------------------------------- //

/*
 * 说话展示：**气泡已移到独立窗口** `bubble.html` / `bubble.js`。
 *
 * ⚠️ 这里**保留同名空实现**，而不是把这些函数删掉。
 *
 * 原因：`pet.say.start / delta / end`、`pet.thinking` 的分发链挂在它们身上。
 * 只删 HTML 不删 JS，会让那些函数对 null 调 classList，每次回复都抛 TypeError
 * （这个坑本项目踩过一次，记在案）。
 *
 * 表现：宠物窗口不再显示任何文字；文字全部由独立的气泡窗口负责。
 */
function beginSay(id) {
  currentSayId = id;
  streaming = true;
}
function appendSay(id, text) {
  void id;
  void text;
}
function endSay(id, fallbackText) {
  if (currentSayId !== null && id && id !== currentSayId) return;
  currentSayId = null;
  streaming = false;
  void fallbackText;
}
function showThinking() {}


/**
 * 探头时把气泡**推回屏幕内侧** —— 数值由主进程算好下发。
 *
 * 用户要求：「当宠物处在探头状态时，文字不要超出屏幕」。
 *
 * 探头时窗口有一大半在屏幕外（立绘贴边），气泡若按窗口居中就会有一半看不见。
 * 主进程知道窗口在屏幕上的位置与那一侧留白，所以由它算，渲染层只套用。
 *
 * 写法注意：用 CSS 变量而不是直接改 `transform` —— 气泡的 transform 还承担
 * 居中与出场/退场动画，两处都写会互相覆盖（`.pet-wrap` 上踩过同样的坑）。
 */
/**
 * 主进程下发的**屏幕适配**位移（探头时窗口有一半在屏幕外，要用屏幕坐标算）。
 *
 * 它与 `baseBubbleShift`（窗口内让位）相加后写进 `--bubble-shift-x`。
 * 分成两段是因为两者知道的坐标系不同：渲染层只知道窗口内的布局，
 * 主进程才掌握窗口在屏幕上的位置。
 */
let screenBubbleShift = 0;
/** 窗口内的让位位移（上方放不下气泡时往侧面让，见 updateBubbleSide） */
let baseBubbleShift = 0;

function setBubbleShift(px) {
  if (!els.bubble) return;
  const value = Number(px);
  screenBubbleShift = Number.isFinite(value) ? value : 0;
  applyBubbleShift();
}

/**
 * 上报气泡在**屏幕上的实测矩形**（主进程据此把它钳进工作区）。
 *
 * 为什么要报：气泡现在是**按内容自适应**的（用户要求"随文字变大，不设固定大小"），
 * 宽度事先算不出来 —— 只能渲染后量。主进程拿到它才能判断"会不会伸出屏幕"。
 *
 * 用 `getBoundingClientRect()`：它返回的是**变换后**的矩形，所以
 * `translate(-50%)` 的居中、探头的额外平移都已经算进去了。
 *
 * 合并 80ms 内的多次请求：流式回复会连续改文字、每次都会触发布局。
 */
function reportBubbleRect() {
  const el = els.bubble;
  if (!el || !window.pet || typeof window.pet.reportBubbleRect !== 'function') return;
  const r = el.getBoundingClientRect();
  // 隐藏或还没内容 -> 不报（主进程会忽略宽度为 0 的）
  if (!(r.width > 0) || !(r.height > 0)) return;
  window.pet.reportBubbleRect({
    x: Math.round(r.left),
    y: Math.round(r.top),
    width: Math.round(r.width),
    height: Math.round(r.height),
  });
}

let bubbleRectTimer = null;
function scheduleBubbleRectReport() {
  if (bubbleRectTimer) clearTimeout(bubbleRectTimer);
  bubbleRectTimer = setTimeout(() => {
    bubbleRectTimer = null;
    reportBubbleRect();
  }, 80);
}

/**
 * 按窗口高度算气泡的高度上限。
 *
 * 用户要求：「只要长度高度不要超出屏幕即可」。
 *
 * 气泡贴窗口顶部（top: 6px），所以可用高度就是 `窗口高 - 6 - 一点余量`。
 * 超过之后由 `overflow: hidden` 裁掉 —— **不出滚动条**（用户明确不要）。
 *
 * 窗口尺寸变化（改宠物大小）时要重算，所以也在 `pet:set-pet-size` 里调。
 */
function updateBubbleMaxHeight() {
  if (!els.bubble) return;
  const h = window.innerHeight;
  if (!(h > 0)) return;
  // 画布顶部的让位量（窗口加高后多出的高度，全在上方）
  const offRaw = Number.parseFloat(els.canvasWrap.style.getPropertyValue('--canvas-offset-y'));
  const off = Number.isFinite(offRaw) && offRaw > 0 ? offRaw : 0;
  /*
   * 限高 = 画布区（含头顶那圈留白）能容纳的高度。
   *
   * 踩过：一开始按"头顶以上"限高，结果文字超过一行就被**压扁并裁掉**
   * （实测 16/30/60/120 字全被压成 40px 高）。用户要的是「底部始终在宠物头上」
   * +「顶部空间不够时往左/右压」—— 不靠"往下长"解决，所以这里放宽到画布底，
   * 由横向让位承担"不出屏"。
   */
  // 至少两行的高度：气泡底边钉在头顶时，若限高比一行还小，文字会被裁掉。
  // 一行约 32px（12.5px 字号 × 1.5 行高 + 上下内边距），两行约 51px。
  els.bubble.style.setProperty('--bubble-max-h', `${Math.max(56, h)}px`);
}

/**
 * 画布在窗口内**向下让位**的量。
 *
 * ---- ⚠️ 这里定过一版，用户实测反馈后改回来了 ----
 *
 * 窗口为给气泡留空间而加高（`pet * 1.34`）之后，我一度把多出来的高度
 * **放在上方**（画布下移 84px），理由是"立绘位置在屏幕上不变"。
 *
 * 但用户实测反馈：「靠下了，超出了一部分，顶上被挡住了一部分」。
 * 原因：窗口的**顶边**在屏幕上没动，把画布往下挪就等于**整只宠物下移**——
 * 宠物本来就贴着工作区底部（用户之前明确要求"下面有东西挡着"也要能压下去），
 * 于是下移直接把它推出屏幕下沿、同时上方被挡。
 *
 * 所以让位量必须是 **0**：画布仍贴窗口顶部，宠物在屏幕上的位置与加高前完全一致，
 * 多出来的高度全在**下方**（留白而已，不影响任何东西）。
 *
 * 代价：气泡只能往**上方**长（头顶以上约 33px），两行以上就要靠横向让位
 * （`updateBubbleSide`）—— 那是用户认过的方案。
 */
function updateCanvasOffset() {
  const wrap = els.canvasWrap;
  if (!wrap) return;
  wrap.style.setProperty('--canvas-offset-y', '0px');
  /*
   * ## 横向：常态 0，探头时**居中**（2026-10-04 修"左边被裁"）
   *
   * 常态窗口宽 = 画布宽，居中量自然是 0（与原 `left: 0` 等价）。
   * 探头窗口宽 385 而画布只有 250 —— 若画布仍贴左边，左侧零余量，
   * **旋转后的立绘左边会被窗口边界裁掉**（实测 89px）。
   *
   * 居中后两侧各留 `(385-250)/2 ≈ 67px`，旋转外接框（实测需要 354）正好放得下。
   */
  /*
   * ## ⚠️ 横向**什么都不做**（2026-10-04 回退）
   *
   * 我一度在这里算 `--canvas-offset-x`（先"画布居中"，再"按旋转后的立绘中心居中"，
   * 最后又叠了实测标定 58→25→34）。**结果是引入了连锁冲突**：
   *
   *   · 画布横向位置从"常量 0"变成了"随窗口宽与姿态变化"的自由变量；
   *   · 而 `artOffsetInWindow` / 碰撞箱 / 探头吸附 / 拖拽极限**全都假设画布贴窗口
   *     左上角**（`left: 0`），于是它们算出来的位置与实际不符；
   *   · 表现：立绘左上被切、外围出现白边（画布移出窗口后被 `overflow:hidden` 裁出硬边）。
   *
   * 用户指出这是**布局层的地基**，要求退回单一来源。所以这里只保留**纵向**的
   * `--canvas-offset-y`（那个是原本就有的、恒为 0 的让位量）。
   *
   * 探头时"旋转外接框比画布宽"的问题**不在这层解决** —— 应该改窗口尺寸或立绘缩放
   * （见 `peekWindowSizeFor` 与 `peekFitScaleFor`），而不是挪画布。
   */
  wrap.style.setProperty('--canvas-offset-x', '0px');
}

/**
 * 更新气泡的**横向**平移：上方空间不够时往宠物左边或右边压。
 *
 * 用户要求：「顶部空间不够时往宠物左边或右边压，这个看宠物在哪里按反方向设定就好了
 * （保证整体不出屏）」。
 *
 * 算法（在渲染层做，因为这里能直接量到气泡真实宽高）：
 *   · `availTop` = 立绘头顶以上的空间 —— 气泡底边钉在头顶时，它能长到这么高；
 *   · 如果气泡高度 > availTop，说明**上方放不下**，改往侧面压：
 *     宠物在屏幕左半边 -> 往右压；在右半边 -> 往左压（"反方向"）。
 *   · 压多少：把气泡推到窗口的对应一侧，让它整体落在屏幕上。
 *
 * 窗口本身在屏幕上的位置由主进程掌握（它还会考虑探头），所以这里只算
 * "窗口内该往哪边靠"，再叠加主进程下发的 `--bubble-shift-x`。
 */
function updateBubbleSide() {
  if (!els.bubble) return;
  const winH = window.innerHeight;
  const winW = window.innerWidth;
  if (!(winH > 0) || !(winW > 0)) return;
  const artH = lastArtBox.h > 0 ? lastArtBox.h : 0;
  const bubbleH = els.bubble.offsetHeight;
  const bubbleW = els.bubble.offsetWidth;
  if (artH <= 0 || !(bubbleH > 0)) {
    baseBubbleShift = 0;
    applyBubbleShift();
    return;
  }
  // 头顶以上可用空间（按画布位置算，见 artTopInWindow 的说明）
  const artTop = artTopInWindow();
  /*
   * 上方放得下就保持居中（最自然）。
   *
   * 放不下时**往侧面让** —— 用户要求：「顶部空间不够时往宠物左边或右边压，
   * 这个看宠物在哪里按反方向设定就好了（保证整体不出屏）」。
   *
   * 气泡宽度只有窗口宽的 95%，所以窗口内总有余量：把它贴到能放下它的那一侧。
   * 至于**屏幕**边界（探头时窗口有一半在屏幕外），由主进程用屏幕坐标再修一次 ——
   * 那需要显示器的位置信息，渲染层这边拿不到可靠值（DPI 缩放下
   * `screen.availWidth` 的单位不一定和 CSS 像素一致）。
   */
  if (bubbleH <= artTop) {
    baseBubbleShift = 0;
  } else {
    /*
     * 让位量按**实际左右边**算，不能按"窗口宽 - 气泡宽"估。
     *
     * 踩过：气泡宽度不足上限时，`left:50% + translate(-50%)` 的居中基准与窗口中心
     * 并不重合（绝对定位的包含块是 `#hud`，还受 `width: max-content` 影响），
     * 于是按估算值平移会偏出窗口 —— 实测 72 宽的气泡被推到 `84..156` 之外
     * （右边界 240 直接顶格越界）。
     */
    const rect = els.bubble.getBoundingClientRect();
    const overflowRight = rect.right - winW;
    const overflowLeft = -rect.left;
    if (overflowRight > 0) baseBubbleShift = -Math.ceil(overflowRight);
    else if (overflowLeft > 0) baseBubbleShift = Math.ceil(overflowLeft);
    else baseBubbleShift = 0;
  }
  applyBubbleShift();
}

/** 把"窗口内让位"与"主进程按屏幕修正"两段位移合起来写进 CSS 变量 */
function applyBubbleShift() {
  if (!els.bubble) return;
  els.bubble.style.setProperty(
    '--bubble-shift-x',
    `${Math.round(baseBubbleShift + screenBubbleShift)}px`,
  );
  scheduleBubbleRectReport();
}

/**
 * 按**可用空间**给气泡定尺寸。
 *
 * 用户要求：「将弹窗底部设置为始终在宠物头上就好了」+「顶部空间不够时往宠物左边
 * 或右边压，这个看宠物在哪里按反方向设定就好了（保证整体不出屏）」。
 *
 * 两者一起成立的前提是：气泡**不能太高**——因为底边钉在头顶，高度超过头顶以上
 * 的空间就只能被压下去（底边就离开头顶了）。所以这里反过来解：
 *   先量"头顶以上还有多少竖向空间"和"能铺多宽"，再按**面积**算出理想宽度 ——
 *   文字总量不变时，铺得越宽就越矮。
 *
 *   · 文字少 -> 理想宽度很小 -> 用 `max-content` 贴着文字（短气泡）
 *   · 文字多 -> 理想宽度变大 -> 展开到整窗宽甚至更宽（配合横向让位）
 *
 * 这样"底边贴头顶 + 整体不出屏"就能同时满足，而不是让高度溢出。
 */
function updateBubbleSize() {
  const el = els.bubble;
  if (!el) return;
  const winW = window.innerWidth;
  const winH = window.innerHeight;
  if (!(winW > 0) || !(winH > 0)) return;
  const artH = lastArtBox.h > 0 ? lastArtBox.h : 0;
  if (artH <= 0) {
    el.style.removeProperty('--bubble-max-w');
    return;
  }
  const artTop = artTopInWindow();
  // 头顶以上的竖向空间（留 6px 余量）—— 气泡高度必须放进这里，
  // 否则底边会被下压、离开头顶（那正是要避免的）
  const availH = Math.max(24, artTop - 6);
  /*
   * 逐个候选宽度**实测**高度，在"放得下"的候选里挑**形状最好**的那个。
   *
   * ⚠️ 规则改过一次，值得记下来：
   *
   * 第一版是"挑放得下的**最窄**宽度"，理由是"短句不该被撑宽"。但窗口加高之后
   * 可用高度有 80px 左右，于是它变得很爱选窄的 —— 实测 8 字和 16 字**都选成
   * 84px**，16 字于是折成两行，看起来像"宽度没有自适应"（用户报的现象）。
   *
   * 现在改为对"放得下"的候选打分：以**宽高比接近 2:1** 为最优（横向铺开、
   * 少折行、矮一些），取分数最好的。这样：
   *   · 文字少 -> 窄候选本来就很矮，宽高比接近 2:1，自然选中（气泡小巧）；
   *   · 文字多 -> 窄候选会很高（比例差），宽候选胜出，横向铺开。
   *
   * 为什么不用公式估算宽度：`scrollWidth` 在内容换行后返回的是"当前布局宽度"，
   * 不是"不换行需要多宽"，拿它算出的理想宽度偏小。
   *
   * ⚠️ 宽度上界必须与 `updateBubbleSide` 用的值一致（窗口宽 95%）：
   * 两处不一致会造出宽到放不进窗口的气泡（实测 30 字算出 384px、横向 -72..312 出屏）。
   */
  const sideRoom = winW * 0.95;
  const candidates = [];
  for (let f = 1; f >= 0.28; f -= 0.06) candidates.push(Math.round(sideRoom * f));
  const prevApplied = el.style.getPropertyValue('--bubble-max-w');
  let best = 0;
  let bestScore = Infinity;
  for (const c of candidates) {
    if (c < 40) continue;
    el.style.setProperty('--bubble-max-w', `${c}px`);
    // 读 offsetHeight 会强制同步布局，量到的是真实高度
    const h = el.offsetHeight;
    if (h > availH) continue; // 放不下：直接淘汰（这是硬约束）
    // 打分：宽高比越接近 2:1 越好；再轻微偏好"更宽"，避免比例相同时选窄的
    const ratio = c / Math.max(1, h);
    const score = Math.abs(ratio - 2) - c / 2000;
    if (score < bestScore) {
      bestScore = score;
      best = c;
    }
  }
  // 一个都放不下（文字太多）-> 取最宽的，交给横向让位与限高处理
  if (!best) best = candidates[0];
  best = Math.max(40, best);
  el.style.setProperty('--bubble-max-w', `${best}px`);
  if (prevApplied !== `${best}px`) scheduleBubbleRectReport();
}

/**
 * 立绘头顶在窗口内的 y 坐标。
 *
 * ⚠️ 必须按**画布位置**算，不能用 `(窗口高 - 立绘高) / 2`。
 *
 * 踩过：窗口加高给气泡留空间之后，画布被下移了 `--canvas-offset-y`（84px），
 * 而这里仍按"窗口中心"算立绘头顶 —— 于是气泡被放到头顶上方 80 多像素的地方，
 * 中间空出一大块（用户看到"气泡和宠物离得很远"）。
 */
function artTopInWindow() {
  const wrap = els.canvasWrap;
  const canvasH = els.canvas ? els.canvas.clientHeight || 0 : 0;
  const offset = wrap
    ? Number.parseFloat(wrap.style.getPropertyValue('--canvas-offset-y')) || 0
    : 0;
  const artH = lastArtBox.h > 0 ? lastArtBox.h : 0;
  if (!(canvasH > 0)) return 0;
  return offset + (canvasH - artH) / 2;
}

/**
 * 让气泡**底边钉在立绘头顶**，文字变多时往上长（用户要求）。
 *
 * 顺序很重要：
 *   ① 先按可用空间定宽（`updateBubbleSize`）—— 铺宽能降低高度、少折行；
 *   ② 再定底边位置（钉在头顶）；
 *   ③ 最后按窗口内的溢出情况决定横向让位。
 */
function updateBubbleBottom() {
  if (!els.bubble) return;
  const winH = window.innerHeight;
  if (!(winH > 0)) return;
  const artH = lastArtBox.h > 0 ? lastArtBox.h : 0;
  if (artH <= 0) {
    els.bubble.style.setProperty('--bubble-bottom', '100%');
    return;
  }
  updateBubbleSize();
  const headTop = artTopInWindow();
  els.bubble.style.setProperty('--bubble-bottom', `${Math.round(winH - headTop + 4)}px`);
  updateBubbleMaxHeight();
  updateBubbleSide();
  requestAnimationFrame(clampBubbleToWindow);
}

/**
 * 把气泡整体压在窗口竖直范围内（横向交给 `updateBubbleSide` + 主进程）。
 *
 * 底边钉在头顶之后，文字太多时气泡会**向上长出窗口**。这里量到高度后，
 * 越界就把底边**往下压**最小需要的距离 —— 而不是切掉文字，
 * 这样"不超出屏幕"和"底边尽量贴头顶"能同时满足。
 *
 * ⚠️ 只往下压、不往上推：往上推会让底边离开头顶，那正是要避免的。
 */
function clampBubbleToWindow() {
  const el = els.bubble;
  if (!el) return;
  const winH = window.innerHeight;
  const h = el.offsetHeight;
  if (!(winH > 0) || !(h > 0)) return;
  const artH = lastArtBox.h > 0 ? lastArtBox.h : 0;
  if (artH <= 0) return;
  const artTop = artTopInWindow();
  // 理想：底边在头顶上方 4px
  let bottom = winH - artTop + 4;
  // 气泡顶 = winH - bottom - h，要求 >= 4（留 4px 余量）
  const maxAllowed = winH - h - 4;
  if (bottom > maxAllowed) bottom = maxAllowed;
  if (bottom < 0) bottom = 0;
  el.style.setProperty('--bubble-bottom', `${Math.round(bottom)}px`);
  updateBubbleMaxHeight();
}

/** 文字变化 -> 气泡尺寸变了 -> 重新上报（流式回复会连续触发） */
if (typeof ResizeObserver !== 'undefined' && els.bubble) {
  new ResizeObserver(() => {
    scheduleBubbleRectReport();
    updateBubbleSide();
    requestAnimationFrame(clampBubbleToWindow);
  }).observe(els.bubble);
}

// --------------------------------------------------------------------------- //

/**
 * 口型包络 -> 立绘。
 * P0 还没有音频，所以用一个基于时间的正弦包络驱动"说话时嘴在动"，
 * 这样状态机与渲染链路今天就能被验证；P3 接上 TTS 后把真实包络喂进 setMouth 即可。
 */
let mouthPhase = 0;

/*
 * ⚠️ 这两个状态在删气泡时被我**误删**过，而 `tickMouth` 还在用：
 * 当时报 ReferenceError: streaming is not defined，口型动画直接崩。
 * 下次删块时先搜一遍引用（这次是靠运行时测试抓到的，命中得很不容易）。
 *
 * streaming：当前是否在演示说话中，驱动口型包络。
 * currentSayId：当前发言的 id，用于串台保护（迟到的增量丢弃）。
 */
let streaming = false;
let currentSayId = null;

function tickMouth() {
  if (streaming) {
    mouthPhase += 0.42;
    const open = 0.25 + 0.55 * Math.abs(Math.sin(mouthPhase)) * (0.6 + Math.random() * 0.4);
    sprite.setMouth(open);
  } else {
    sprite.setMouth(0);
  }
}
setInterval(tickMouth, 60);

// --------------------------------------------------------------------------- //
// 主进程消息
// --------------------------------------------------------------------------- //

const pet = window.pet;

pet.on('pet:message', (message) => {
  switch (message.type) {
    case 'pet.thinking':
      showThinking();
      break;
    case 'pet.say.start':
      beginSay(message.id);
      break;
    case 'pet.say.delta':
      appendSay(message.id, message.text ?? '');
      break;
    case 'pet.say.end':
      endSay(message.id, message.text);
      if (message.reason === 'error') {
        showToast('这次没说成，看看日志', 'error');
      }
      break;
    case 'pet.mouth':
      // 真实音频包络；目前 sidecar 不发，先留着接口
      if (Array.isArray(message.envelope) && message.envelope.length) {
        const avg = message.envelope.reduce((a, b) => a + b, 0) / message.envelope.length;
        sprite.setMouth(avg);
      }
      break;
    case 'pet.emotion':
      // 情绪三维度 -> 立绘表情：心情映射到 -1~1，精力直接给
      sprite.setEmotion((message.state.mood - 0.5) * 2, message.state.energy);
      /*
       * 好奇表情（2026-10-17，截图对话）。
       *
       * sidecar 在用户把屏幕推过来时会带一个 expression: 'curious'。
       * 为什么要单独一档而不是靠 mood/energy 推：那两条是**连续量**，
       * 表达不出这一刻它在盯着你的屏幕看这种瞬时状态。
       *
       * ⚠️ play('curious') 是一次性反应（播完自动回 idle，见 sprite.js），
       * 正是想要的：好奇是抬头看一眼，不是永久姿势。
       */
      if (message.expression === 'curious' || message.expression === 'excited') {
        sprite.play(message.expression);
      }
      break;
    case 'pet.observe':
      console.info('[pet] 屏幕观察（P4）', message);
      break;
    case 'voice.state': {
      // 语音状态：只在宠物身上用姿态表达（原来是给"语音"按钮改文字）。
      // 输入框那边有状态点，这里不再重复显示文字。
      if (message.state === 'hearing') {
        // 用户在说话时让宠物有个"在听"的姿态，反馈更自然
        sprite.play('curious');
      } else if (message.state === 'listening' && sprite.currentState === 'curious') {
        sprite.play('idle');
      }

      /*
       * 🔴 **识别中要有持续可见的反馈**（2026-10-10 加）。
       *
       * ## 为什么加
       *
       * 用户报「按住 alt 按键没用」—— 查出来按键、闸门、音频全是对的，
       * 真正的问题是**识别慢**：实测中位数 6.5 秒、最慢 15.5 秒。
       *
       * 而这段时间里**没有任何可见反馈**：
       *   · 宠物窗口不聚焦时看不到姿态变化
       *   · `pet:voice-state` 只发给**设置窗口**（见 `index.ts`），宠物窗口没有
       *   · 唯一那条 `user.voice` 的 toast 是**识别完之后**才弹
       *
       * 用户等 6~15 秒什么都没看见，当然认为"没用"。
       *
       * ## 为什么用 toast 而不是新加指示器
       *
       * `#toast` 就是给这类提示的，复用它：
       *   · 零新 DOM、零新样式、零协议改动
       *   · 而且它在**宠物身上**，用户正在对着宠物说话，视线本来就在那儿
       *
       * ⚠️ 唯一的改动是"不自动消失"（见 `showToast` 的 `sticky` 参数）——
       * 默认 2.6 秒太短，盖不住 6~15 秒的识别。
       */
      if (message.state === 'thinking') {
        showToast('识别中…', 'info', { sticky: true });
      } else if (message.state === 'hearing') {
        // 按下就说清楚"在听你说"，这样松手前用户已经确认按键生效了
        showToast('在听你说…', 'info', { sticky: true });
      } else if (stickyToastShown) {        // 回到 listening / idle —— 把上面那条撤掉，别一直占着
        hideToast();
      }
      /*
       * ⚠️ 只在**我们**挂上去的那条还在时撤。
       * 否则会把 `user.voice` 刚弹的「听到：…」误撤掉 —— 那条正是用户
       * 最需要的确认（"它把我听成了什么"）。
       */
      break;
    }
    case 'memory.cleared':
      showToast(`忘了 ${message.removed} 条记忆`, 'info');
      break;
    case 'voice.audio':
      // 元信息（音频本体走 pet:voice-audio 独立通道，见下方订阅）
      console.info(`[voice] 宠物语音：${message.bytes} 字节 ${message.format}`);
      break;
    case 'user.voice':
      // 把识别结果回显给用户，否则他不知道自己被听成了什么
      showToast(`听到：${message.text}`, 'info');
      break;
    case 'system.error':
      showToast(message.message ?? '出错了', message.level ?? 'error');
      break;
    case 'ready':
      setStatus(`在线 · ${message.brain}`, 'ok');
      break;
    default:
      break;
  }
});

pet.on('pet:status', (payload) => {
  setStatus(payload.text, payload.level ?? 'info');
  if (payload.level === 'error') showToast(payload.text, 'error');
});

pet.on('pet:config', (payload) => {
  /*
   * ⚠️ 这个处理器**原来每收到一次消息就崩**（2026-10-04 从渲染层控制台日志抓到）：
   *
   *     Error: setActivityUI is not defined
   *     Error: renderAutonomy is not defined
   *
   * 三个函数（`setActivityUI` / `renderAutonomy` / `renderMemory`）**都不存在** ——
   * 它们是"活泼度 / 自主性 / 记忆"那套 UI 的遗留物，那套界面后来删了，
   * 但这里的调用没跟着删。于是处理器在第一次调用处就抛异常，
   * **后面的 `autonomy` / `memory` 分支从来没执行过**。
   *
   * 现在只做"记录下来备查"，不做任何 UI 动作 —— 渲染层已经**没有对应的展示位**。
   * 将来若要把活泼度/记忆重新显示出来，在设置窗口里做（那里是它该在的地方）。
   */
  if (payload.activity && typeof payload.activity.overall === 'number') {
    lastActivityOverall = payload.activity.overall;
  }
  if (payload.autonomy) {
    lastAutonomy = payload.autonomy;
  }
  if (payload.memory) {
    lastMemory = payload.memory;
  }
});

/**
 * 最近一次下发的"活泼度 / 自主性 / 记忆"（只作记录）。
 *
 * 渲染层**不再有对应的展示位**（那套 UI 已删）。留着是为了调试时能在控制台看到
 * 主进程推了什么值 —— 见 `pet:config` 处理器的说明。
 */
let lastActivityOverall = null;
let lastMemory = null;

// --------------------------------------------------------------------------- //
// 长期记忆（P2）
// --------------------------------------------------------------------------- //



els.canvas.addEventListener('contextmenu', (event) => {
  event.preventDefault();
  pet.openContextMenu();
});

// Esc 打断当前生成（对应协议里的 user.interrupt）。
// 原来挂在输入框上，输入框搬走之后挂在宠物上 —— 至少在宠物身上还按得到。
window.addEventListener('keydown', (event) => {
  if (event.key !== 'Escape') return;
  pet.interrupt();
  sprite.play('idle');
  endSay(currentSayId ?? '', '');
});

pet.on('pet:paused', (payload) => {
  document.body.classList.toggle('paused', Boolean(payload.paused));
  if (payload.paused) {
    setStatus(payload.minutes ? `免打扰 ${payload.minutes} 分钟` : '免打扰中', 'warn');
  } else {
    setStatus('在线', 'ok');
  }
});

pet.on('pet:toast', (payload) => showToast(payload.text, payload.level ?? 'info'));

// --------------------------------------------------------------------------- //
// 语音链路（P3）
// --------------------------------------------------------------------------- //

/**
 * 麦克风采集 + 音频播放。
 *
 * **为什么音频 I/O 在渲染层而不是 Python 侧**：Web Audio API 原生支持麦克风与播放，
 * Chromium 已经把它跑起来了；Python 侧要同等能力得依赖需要编译的 sounddevice /
 * pyaudio。所以约定是——渲染层负责"碰硬件"，Python 侧只处理 PCM 字节做端点检测与识别。
 */
let audio = null;

/**
 * 收到宠物语音的**音频本体**（走独立通道，避免 base64 膨胀）。
 * 播放交给 VoiceIO：它负责解码、打断上一段、并把同一时刻只播一段这件事管住。
 *
 * 注意：除这个二进制通道外，其余所有下行消息一律走 `pet:message` 统一分发。
 * 早期版本还额外订阅了 `voice.state` / `voice.audio` 这两个"原始协议类型名"通道，
 * 等于按类型名再分一次发 —— 与协议列表失去同步时就静默失效（而且会让
 * "订阅通道必须在 preload 白名单里"这条规则变得难以解释）。
 */
pet.on('pet:voice-audio', (payload) => {
  if (!audio) audio = new VoiceIO();
  /*
   * 🔴 终点回调**每次都要挂**（2026-10-17 实锤）：
   * `audio` 是懒创建的单例，麦克风预热（warmUpVoice）/设置页枚举麦克风
   * 等**别的路径**会先把它创建出来、且不带 onPlaybackEnded ——
   * 只在 `!audio` 分支里挂回调的话**永远挂不上**，宠物播完就没有
   * 「说话中 -> 空闲」的终点信号，状态条卡死在说话中。
   * 起点信号（notifyVoiceSpeaking）没这问题，因为它不依赖实例。
   */
  audio.onPlaybackEnded = () => window.pet?.notifyVoicePlayed?.();
  /*
   * 开始播放 -> 告诉主进程（2026-10-17 加）。
   * 这是状态条「说话中」的起点信号：渲染层是真正播音频的地方，
   * 它说了算 —— 主进程在转发点猜时机会漏（实测音频在播还挂「思考中」）。
   */
  window.pet?.notifyVoiceSpeaking?.();
  audio.play(payload.bytes, payload.format).catch((error) => {
    console.warn('[voice] 播放宠物语音失败', error);
  });
});

/**
 * 语音开关的**运行时状态** + 开关函数（2026-10-07 接上）。
 *
 * ## ⚠️ 这条链路之前是断的
 *
 * `voice-io.js` 的采集实现一直是完整的，但**没有任何地方调它的 `start()`**
 * —— 而且它最后一行调的 `window.pet.sendMicAudio` 在 preload 里也不存在。
 * 两处缺失叠加的结果是"麦克风从没被打开过"，而且**全程不报错**（`?.` 静默）。
 *
 * ## 开关必须做两件事，缺一不可
 *
 *   ① `VoiceIO.start()` / `.stop()`  —— 真正打开/关闭麦克风（碰硬件）
 *   ② `pet.setVoiceEnabled(...)`     —— 告诉 sidecar"我开始听了"
 *
 * 只做 ① 后端不知道要处理；只做 ② 后端干等音频。
 *
 * ## ⚠️ `getUserMedia` 需要"用户手势"
 *
 * 浏览器安全策略要求它在一个**用户手势的调用栈**里发起。所以这里不能
 * 页面加载就自动开 —— 必须由"点了开关"触发。设置页那个开关经主进程
 * 转成 `pet:set-voice` 消息进来，而它源于用户点击，所以能通过。
 *
 * ⚠️ **但"按住说话"打破了这个前提** —— 见 `warmUpVoice()` 的说明。
 */
let voiceOn = false;

/**
 * 预热麦克风：**开机就把流开好**，让"按住说话"按下那一刻零延迟。
 *
 * ## 🔴 为什么必须预热（2026-10-08 用户实测 + 日志定案）
 *
 * 用户反馈「按住说话没用」。日志把机理记得很精确（同一分钟内两次按键）：
 *
 * ```
 * 12:34:40.673  [语音] hearing 按住说话中…       <- 按下
 * 12:34:40.848  [语音] listening 采样率 48000Hz  <- 麦克风开了
 * 12:34:40.937  [按住说话] 松开                  <- 按了 0.26 秒 -> 成功
 *
 * 12:34:42.007  [语音] hearing 按住说话中…       <- 按下
 * 12:34:42.077  [按住说话] 松开                  <- 只按了 0.07 秒
 * （没有 listening）
 * 12:34:42.083  [语音] thinking 识别中…          <- 闸门开了，但零音频帧
 * ```
 *
 * **两次按键的唯一差别是按住的时长。** 因为：
 *
 *   · `sendVoicePtt('start')` 是**同步立刻**发的 —— sidecar 闸门当开就开
 *   · 开麦克风是**异步**的（`getUserMedia` -> `AudioContext` ->
 *     `ScriptProcessorNode`，正常也要 100~300ms）
 *   · 而且 `getUserMedia` 要**用户手势**，而**按 Alt 不算"点击"**
 *
 * 所以按得快 = 闸门开了、一帧音频都没有 = "按了没用"。
 *
 * ## 为什么预热是安全的
 *
 * 麦克风开着**不等于在听**：真正的闸门是 sidecar 的 `voice.ptt`
 * （`_capture_enabled`），没按住时它一帧都不处理。
 * 而且采集是**纯本地**的（离线 ASR），预热不产生任何网络流量。
 *
 * ## 失败怎么办
 *
 * 预热失败（权限被拒 / 没有设备）**不报错、不打扰** ——
 * 用户按 PTT 时那条兜底路径会再试一次，并把失败原因如实回报到界面。
 * 开机就弹一个红字提醒"麦克风用不了"，对不用语音的用户是噪音。
 */
async function warmUpVoice() {
  if (voiceOn) return;
  try {
    /*
     * ⚠️ `audio` 有可能是 **null**（2026-10-08 踩过）。
     *
     * 它是在别处**懒创建**的，启动时还没被赋值。第一版我直接写了
     * `audio.start(...)`，于是抛 `TypeError` -> 被下面的 catch 静默吞掉 ->
     * 预热**从来没生效过**，而日志里只有一行容易被忽略的 console.info。
     *
     * 这种"被 catch 吞掉的 TypeError"是最难查的一类：功能只是"没生效"，
     * 不报错、不留明显痕迹。所以下面**显式建实例**，而不是依赖别处已经建好。
     */
    if (!audio) audio = new VoiceIO();
    await withTimeout(audio.start(micDeviceId), MIC_OPEN_TIMEOUT_MS, '预热麦克风超时');
    voiceOn = true;
    pet.setVoiceEnabled(true);
    /*
     * 🔴 **预热之后必须同步一次闸门**（2026-10-10 修用户报的 bug）。
     *
     * 麦克风在这一刻就开了，而闸门默认是开的 —— 如果「按住说话」开着，
     * 这里不同步就等于"开机就一直听"，用户没按任何键也会被识别。
     */
    applyCaptureGate();
    /*
     * ⚠️ 不报 'listening'：预热不代表在听 —— 报出去会让用户以为宠物
     * 一直在听（那是隐私问题，也是语义错误）。
     */
    console.info('[voice] 麦克风已预热（按住说话零延迟）');
  } catch (error) {
    console.warn('[voice] 预热失败，按住说话时会再试一次：', error?.message ?? error);
    // 半开的资源要收掉（与 setVoice 的失败路径同理）
    try {
      audio?.stop();
    } catch {
      /* 收尾失败无所谓 */
    }
  }
}

/**
 * 诊断出口：把**采集侧的真实计数**暴露出来（与 `__lastSetVoice` 同一个惯例）。
 *
 * ⚠️ 定义在模块级（而不是只在 `setVoice` 成功后挂上）—— 因为预热路径也
 * 需要它，而预热不走 `setVoice`。第一版挂在 `setVoice` 里，导致
 * "预热成功但读不到状态"，排查时白绕一圈。
 */
window.__voiceStats = () => ({
  active: Boolean(audio?.active),
  framesSent: audio?.framesSent ?? -1,
  samplesDropped: audio?.samplesDropped ?? -1,
  pendingCount: audio?.pendingCount ?? -1,
  deviceLabel: audio?.deviceLabel ?? '',
  ctxState: audio?.context?.state ?? '(无)',
  sampleRate: audio?.context?.sampleRate ?? 0,
  voiceOn,
});

/**
 * 打开麦克风的超时上限（毫秒）。
 *
 * ⚠️ 实测：没有麦克风的机器上 `getUserMedia` **既不 resolve 也不 reject**，
 * 就那么挂着。没有超时的话，用户点开开关之后界面永远停在"正在请求权限…"，
 * 而且**没有任何错误可看** —— 见 `setVoice` 里那段说明。
 *
 * 8 秒：正常机器上 `getUserMedia` 在 100ms 内返回；系统权限弹窗需要用户
 * 点一下，那也不该超过几秒。8 秒足够，也不会让用户等得不耐烦。
 */
const MIC_OPEN_TIMEOUT_MS = 8000;

/** 给一个 Promise 加超时；超时抛错（消息里带上在做什么，好回报给用户）。 */
function withTimeout(promise, ms, what) {
  let timer = 0;
  return Promise.race([
    promise.finally(() => clearTimeout(timer)),
    new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error(`${what}（${Math.round(ms / 1000)} 秒无响应）`)), ms);
    }),
  ]);
}

/** 把语音开关的真实结果回报给设置页（它那边要显示状态/错误） */
function reportVoiceState(state, detail = '') {
  try {
    window.pet.reportVoiceState?.({ on: voiceOn, state, detail });
  } catch {
    /* 主进程没接也无所谓 */
  }
}

async function setVoice(on) {
  /*
   * 诊断标记：把"开关被触发"这件事记在 window 上。
   *
   * ⚠️ 为什么需要它：这条链上有 IPC（主进程 -> 宠物窗口）、preload 白名单、
   * 事件订阅、`getUserMedia` 四段，**任何一段断了表现都一样**（点了没反应）。
   * 有一个确定的位置标记，就能把"没触发"和"触发了但卡住/失败"分开。
   *
   * 排查手段：`window.__lastSetVoice` —— 有值说明事件到了这里；
   * 它还有 `stage` 字段，标明走到了哪一步。
   */
  window.__lastSetVoice = { at: Date.now(), on: Boolean(on), stage: 'enter' };
  if (!audio) audio = new VoiceIO();
  voiceOn = Boolean(on);
  if (voiceOn) {
    try {
      /*
       * ## ⚠️ 必须给 `getUserMedia` 加超时（2026-10-07 实测踩到）
       *
       * 实测：在**没有麦克风**的机器上，`getUserMedia` 既不 resolve 也不
       * reject —— 它就那么挂着。于是：
       *
       *     window.__lastSetVoice = { on: true, stage: 'enter' }   <- 进来了
       *     window.__errs = []                                     <- 没有错误
       *     pet.setVoiceEnabled  从未被调用                        <- 卡在 await 上
       *
       * 用户看到的是"开关打开了、然后什么也没发生" —— 而且**永远不会结束**。
       *
       * 浏览器不会替我们兜这个底（权限提示被系统静默忽略时就是这样），
       * 所以自己加一道：超时就当失败，走下面那条**如实回报**的路。
       */
      window.__lastSetVoice.stage = 'starting';
      const info = await withTimeout(audio.start(micDeviceId), MIC_OPEN_TIMEOUT_MS, '打开麦克风超时');
      window.__lastSetVoice.stage = 'started';
      pet.setVoiceEnabled(true);
      // 同上：麦克风开了就同步闸门（按住说话开着时这里必须是"关"）
      applyCaptureGate();
      window.__lastSetVoice.stage = 'notified-brain';
      reportVoiceState('listening', `采样率 ${info?.sampleRate ?? '?'}Hz`);
      window.__lastSetVoice.stage = 'reported';
      console.info('[voice] 麦克风已开启', info);
      /*
       * 诊断出口：把**采集侧的真实计数**暴露出来（与 `__lastSetVoice` 同一个惯例）。
       *
       * ## 为什么需要它（2026-10-08 排查"按下没用"）
       *
       * `__lastSetVoice.stage` 只说明 `setVoice()` 走到哪一步，**说明不了**
       * "音频帧有没有真的发出去"。而这次的问题恰恰是：
       *
       *     日志显示麦克风开了（listening 采样率 48000Hz）
       *     sidecar 也开了采集闸门
       *     但**没有任何识别结果**，`framesSent` 一直是 0
       *
       * 光看是否报错是看不出来的 —— 必须能读到**发了多少帧**。
       *
       * 字段里 `active` 尤其关键：`VoiceIO.start()` 开头有
       * `if (this.active) return ...` 的早退，所以"麦克风看着开着、
       * 其实回调没挂上"这种状态是可能的，而它**不报错**。
       *
       * ⚠️ 定义已挪到**模块级**（见上面 `warmUpVoice` 后面那份）——
       * 因为预热路径不走 `setVoice`，挂在这里会导致"预热成功但读不到状态"。
       * 这里不再重复挂载。
       */
    } catch (error) {
      /*
       * ⚠️ 失败要**如实回报**，不能只 console。
       * 权限被拒 / 没有设备 / 被占用 / 超时是几种不同的问题，用户需要知道是哪种。
       */
      voiceOn = false;
      // 半开的资源要收掉：超时之后 `start()` 仍可能成功，那会留下一个
      // 没人管的 AudioContext 与麦克风占用（用户会看到"系统说麦克风在用，
      // 但宠物没反应"）。
      try {
        audio.stop();
      } catch {
        /* 收尾失败无所谓 */
      }
      const message = error instanceof Error ? error.message : String(error);
      window.__lastSetVoice.stage = `error: ${message}`;
      reportVoiceState('error', message);
      showToast(`麦克风打不开：${message}`, 'warn');
      console.warn('[voice] 麦克风开启失败', error);
    }
  } else {
    audio.stop();
    pet.setVoiceEnabled(false);
    reportVoiceState('idle', '已关闭');
    window.__lastSetVoice.stage = 'stopped';
    console.info('[voice] 麦克风已关闭');
  }
}

pet.on('pet:set-voice', (payload) => {
  void setVoice(Boolean(payload?.enabled));
});

/* ---------------------------------------------------------- 麦克风选择 */

/**
 * 用户选定的麦克风 `deviceId`（空 = 系统默认）。
 *
 * ## ⚠️ 为什么必须能选（用户反馈 2026-10-08：「我用 usb 的麦克风说话好像没用」）
 *
 * 实测（同一支 USB 麦、同样对人说话 3 秒）：
 *
 *     不给 deviceId（走 Chromium 的 default 别名） peak 0.0205  判为语音  3/83  ❌
 *     指定真实 deviceId                           peak 0.1413  判为语音 19/86  ✅
 *
 * **差 7 倍。** 而 `enumerateDevices()` 里那个 default 别名**显示的名字和
 * 真实设备一模一样**（"Default - 麦克风 (USB PnP Sound Device)"）——
 * 用户完全看不出自己用的不是那一支。
 *
 * 所以 `voice-io.js` 的 `listMicrophones()` **刻意过滤掉 default/communications
 * 那两个别名**，只让用户从真实设备里挑。
 */
let micDeviceId = '';

/**
 * 供主进程调用（设置页请求枚举时走这里 —— 因为**麦克风权限在这个窗口**）。
 *
 * 挂在 `window` 上是刻意的：主进程用 `executeJavaScript('window.__listMics()')`
 * 调它。设置页不能自己枚举 —— 它没被授权过，拿到的 label 会是空的。
 */
window.__listMics = async () => {
  try {
    if (!audio) audio = new VoiceIO();
    return await audio.listMicrophones();
  } catch (error) {
    console.warn('[voice] 枚举麦克风失败', error);
    return [];
  }
};

pet.on('pet:set-mic', (payload) => {
  const next = String(payload?.deviceId ?? '');
  if (next === micDeviceId) return;
  micDeviceId = next;
  console.info(`[voice] 切换麦克风 -> ${next ? next.slice(0, 12) : '(系统默认)'}`);
  // 已经在听的话要**重新开流** —— 否则换了设备不生效（用户会以为"选了没用"）
  if (voiceOn) {
    void (async () => {
      try {
        audio?.stop();
      } catch {
        /* 收尾失败无所谓 */
      }
      await setVoice(true);
    })();
  }
});

/**
 * 按住说话（push-to-talk）的**按下 / 松开**（用户要求：
 * 「设置自定义按键，按下按键发声，停下结束」）。
 *
 * ## ⚠️ 麦克风**保持打开**，不在这里开关
 *
 * 按下说话时麦克风一直在采（`VoiceIO` 的 `start()` 是语音开关那一步做的）。
 * 这条只做两件事：
 *   · 按下 -> 告诉后端"开始"（界面显示"正在听"）
 *   · 松开 -> 告诉后端"**这句说完了**"，由它主动收尾并送去识别
 *
 * 为什么不每次按下都 `VoiceIO.start()`：反复创建/销毁 AudioContext 有实打实的
 * 开销，而按住说话可能一秒按好几次，那样会卡。
 *
 * ## ⚠️ 松开不是"取消"
 *
 * 用户松开手的意思是"我说完了"，那句话要**被识别**。
 * 真正的取消是另一个动作（按 Esc / 关掉开关），这里不做。
 *
 * ## 权限提示
 *
 * 如果用户**没先开语音开关**就直接按这个键，麦克风还没打开 —— 那时要
 * 顺手把语音开起来（否则"按了没反应"）。这是"按了就该有反应"的合理预期。
 */
let pttActive = false;

/**
 * 「按住说话」在配置里开着吗（由主进程推送，见 `pet:ptt-config`）。
 *
 * ## 🔴 为什么要这个（2026-10-10 用户实测报的 bug）
 *
 * 用户问：「我现在没按按键他都能听见我的声音回答吗」—— **答案是"能"**。
 * 日志里全是他环境里的杂音被识别成对话：
 *
 *     语音识别（1.40s）：门口一个一个的开。
 *     语音识别（0.72s）：好接坏。
 *
 * 机理：闸门（`VoiceSession._capture_enabled`）**默认就是开的**，
 * 而前端只在**按键**时才发消息；麦克风却开机就预热常开 ——
 * 于是"不按住也一直听"。
 *
 * `push_to_talk` 原来只做了"按键时切一下断句模式"，**没做"不按住就不听"**，
 * 而那才是这个功能的语义。
 */
let pushToTalkEnabled = false;

/**
 * 按"闸门该不该开"的规则同步一次。
 *
 *     闸门 = 没开按住说话（免提，常开）
 *         或 正按着（PTT 的模式）
 *
 * ⚠️ 抽成一个函数是因为有**四个时机**都要算：
 *   ① 预热麦克风之后（原先漏的就是这里 —— 那段窗口里一直在听）
 *   ② `setVoice(true)` 之后
 *   ③ 主进程推来「按住说话」配置时（用户可能在设置页改了）
 *   ④ 按下 / 松开时
 *
 * 散着写必然漏一处，而漏掉的表现就是"开关不生效"。
 */
function applyCaptureGate() {
  const shouldListen = !pushToTalkEnabled || pttActive;
  /*
   * 🔍 诊断（2026-10-10 排查「推了配置但闸门没关」时加的）。
   *
   * ⚠️ **挂 `window` 而不是只 console** —— 渲染层 console 转发到主进程
   * 并不总是到得了（实测过），而从 CDP 读 `window.xxx` 是**确定的**。
   * 这次排查卡了很久，就是因为"看不到前端到底收到没"。
   */
  window.__captureGate = {
    shouldListen,
    pushToTalkEnabled,
    pttActive,
    at: Date.now(),
  };
  console.info(
    `[voice] 闸门 -> ${shouldListen ? 'listening' : 'gated'}` +
      `（按住说话=${pushToTalkEnabled}，正按着=${pttActive}）`,
  );
  /*
   * 🔴 **从源头停掉推帧**（2026-10-10 修，见 `voice-io.js` 里 `gate` 的说明）。
   *
   * 原来只把 `start`/`stop` 发给 sidecar —— 那只是让 sidecar **不处理**，
   * 而帧照样在推（50 帧/秒），于是队列里积着 2 秒旧音频。闸门一开，
   * 端点/VAD 先吃到这 2 秒旧音频 -> 噪声底被抬高 -> **用户真正说话被判成静音**
   * -> 松手后没识别。表现就是「第一次能用，之后按了没反应」。
   *
   * ⚠️ `audio` 可能还没建（`warmUpVoice()` 是异步的）—— 用 `?.` 兜住。
   * 那时 `gate` 还没开，等 `warmUpVoice` 成功后它会再调一次 `applyCaptureGate()`。
   */
  audio?.setGate?.(shouldListen);
  pet.sendVoicePtt?.(shouldListen ? 'start' : 'stop');
}

/*
 * 「按住说话」的**配置**（不是按键事件）—— 主进程在启动时、以及设置页改动后推。
 *
 * 🔴 这个监听就是用户报的 bug 的修复点（2026-10-10）：
 * 没有它的时候，前端**不知道**「按住说话」开着，于是闸门一直保持默认的"开"，
 * 麦克风又开机预热 —— 表现就是"不按按键也一直听、还把环境噪音识别成对话"。
 * 日志里能看到那些误识别：「门口一个一个的开。」「好接坏。」
 */
pet.on('pet:ptt-config', (payload) => {
  pushToTalkEnabled = Boolean(payload && payload.enabled);
  // 🔍 诊断：证明"配置真的到了渲染层"（见 applyCaptureGate 的说明）
  window.__pttConfigGot = { enabled: pushToTalkEnabled, payload, at: Date.now() };
  console.info(`[voice] 按住说话：${pushToTalkEnabled ? '开（只有按住时才听）' : '关（免提，一直在听）'}`);
  // 开关一变就要重算闸门：开了 -> 立刻关上（不再偷听）；关了 -> 立刻打开（恢复免提）
  applyCaptureGate();
});

pet.on('pet:ptt', (payload) => {
  const phase = payload?.phase;
  if (phase === 'start') {
    pttActive = true;
    /*
     * ## ⚠️ 麦克风**必须在按住之前就开着**（2026-10-08 用户实测暴露）
     *
     * 用户反馈「按住说话没用」，日志给出了精确机理（同一分钟内两次按键）：
     *
     *     12:34:40.673  [语音] hearing 按住说话中…       <- 按下
     *     12:34:40.848  [语音] listening 采样率 48000Hz  <- 麦克风**开了**
     *     12:34:40.937  [按住说话] 松开                  <- 按了 0.26 秒 -> 成功
     *
     *     12:34:42.007  [语音] hearing 按住说话中…       <- 按下
     *     12:34:42.077  [按住说话] 松开                  <- 只按了 0.07 秒
     *     （**没有 listening** —— 麦克风没来得及开）
     *     12:34:42.083  [语音] thinking 识别中…          <- 闸门开了但零音频帧
     *
     * 机理：`sendVoicePtt('start')` 是**同步立刻**发的（sidecar 闸门当开就开），
     * 而开麦克风是**异步**的（`getUserMedia` -> `AudioContext` ->
     * `ScriptProcessorNode`）。按得快一点，闸门开了却一个音频帧都没有。
     *
     * 而且 `getUserMedia` 还受**用户手势**约束（设置页那句提示就是它）——
     * **按 Alt 不算"点击"**，所以这条路本来就不该指望。
     *
     * 修法：`warmUpVoice()` 在启动时就**预先把麦克风开好**，
     * 按住的那一刻流已经在跑 —— 零延迟。
     *
     * ⚠️ 这里保留兜底：万一预热失败（权限被拒等），仍尝试开一次 ——
     * 用户至少能看到"权限被拒"的如实回报，而不是静默没反应。
     */
    if (!voiceOn) {
      void setVoice(true);
    }
    // 按下 -> 闸门开（走统一函数，避免规则散落）
    applyCaptureGate();
    reportVoiceState('hearing', '按住说话中…');
    console.info('[voice] 按住说话：开始');
  } else if (phase === 'stop') {
    if (!pttActive) return; // 没按过就松开（比如启动时键是压着的）—— 忽略
    pttActive = false;
    /*
     * ⚠️ 松开这条**必须发出去**，哪怕麦克风没开成 —— 后端那边可能是
     * "开了又失败"的中间态，漏发会让它一直停在 HEARING 等下一次静音判定。
     */
    pet.sendVoicePtt?.('stop');
    reportVoiceState('thinking', '识别中…');
    console.info('[voice] 按住说话：结束，送去识别');
  }
});

// --------------------------------------------------------------------------- //
// 交互 -> 主进程
// --------------------------------------------------------------------------- //

/**
 * 把状态同步到**对话条窗口**上的小圆点。
 *
 * 状态文本由主进程统一收敛（它会给 chat 窗口发 `chat:status`），
 * 这里只负责把"渲染层自己产生的"状态变化转告主进程 —— 否则
 * 宠物窗口和对话条会各说各话（这个项目在点击穿透上已经踩过一次
 * "两个决定者"的坑，不要在状态上重演）。
 */
function setStatus(text, level = 'info') {
  pet.reportStatus(text, level);
}

let toastTimer = null;
/**
 * 宠物身上那条提示。
 *
 * ## `sticky`（2026-10-10 加）
 *
 * 默认 2.6 秒自动消失 —— 那对「忘了 N 条记忆」这类一次性通知够用。
 * 但**语音识别中要 6~15 秒**（实测中位数 6.5s、最慢 15.5s），
 * 2.6 秒的提示盖不住，用户还是"什么都没看见就等在那儿"。
 *
 * 所以识别相关的提示传 `sticky: true`，由调用方在状态结束时
 * 调 `hideToast()` 撤掉。
 *
 * ⚠️ 挂上/撤下都要维护 `stickyToastShown` —— 它决定 `voice.state`
 * 回到 listening 时**该不该**撤。不维护的话会误撤
 * `user.voice` 刚弹的「听到：…」（那条是用户最需要的确认）。
 */
let stickyToastShown = false;

function showToast(text, level = 'info', opts = {}) {
  els.toast.textContent = text;
  els.toast.dataset.level = level;
  els.toast.classList.remove('hidden');
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = 0;
  stickyToastShown = Boolean(opts.sticky);
  if (!opts.sticky) {
    toastTimer = setTimeout(() => {
      els.toast.classList.add('hidden');
      stickyToastShown = false;
    }, 2600);
  }
}

/** 撤掉提示（`sticky` 那条用）。非 sticky 的也会被撤，效果一样。 */
function hideToast() {
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = 0;
  stickyToastShown = false;
  els.toast.classList.add('hidden');
}

// --------------------------------------------------------------------------- //
// 自主行为状态显示
// --------------------------------------------------------------------------- //

/** 最近一次收到的 autonomy 状态快照 */
let lastAutonomy = null;

/**
 * 把"为什么它现在不说话"显示出来。
 *
 * 这不是装饰：自主行为最容易让用户困惑的就是"它怎么不吭声了"。
 * 明示原因（免打扰/静音时段/专注保护/意愿不足）能省掉大量猜测。
 */
const REASON_TEXT = {
  muted: '免打扰中',
  quiet_hours: '静音时段',
  focus_guard: '检测到专注/全屏',
  'autonomy.disabled': '自主发言已关闭',
  need_observation: '还没看到值得说的',
  below_threshold: '正在攒开口的意愿',
  speak: '刚主动说了话',
  decided_quiet: '决定这次不说',
  manual_poke: '被你戳了一下',
};



// --------------------------------------------------------------------------- //
// 点击穿透：告诉主进程指针是否在交互区
// --------------------------------------------------------------------------- //

let interactiveNow = null;
/** 是否正在拖拽窗口。拖拽期间禁止关闭交互（见 reportInteractive 的说明）。 */
let draggingWindow = false;
/**
 * 恢复穿透的短延迟。
 *
 * 用途有限、但有价值：`setIgnoreMouseEvents` 是异步生效的，延迟一点能让
 * "指针刚离开交互区"与"用户按下"错开，减少按下落空。
 *
 * ⚠️ 注意：**"只能拖第一次"那个 bug 不是它造成的**，根因在主进程的
 * `drag-end` 里强行打开穿透（已修，见 petWindow.ts）。这里保留一个短延迟
 * 只是顺手降低竞态概率，设成 0 也不会再出现"只能拖一次"。
 */
let clickThroughTimer = null;
const CLICK_THROUGH_DELAY_MS = 120;

/**
 * 判定某点是否落在"用户想操作"的区域里。
 *
 * 向外放宽 4 像素：`setIgnoreMouseEvents` 是异步生效的，判定边界越紧，
 * "按下"落在穿透状态里的概率越高。放宽一点相当于**提前**关闭穿透。
 * 误判代价只是宠物周围几像素会拦住底下点击，而那本就是它的视觉范围。
 */
const GROW = 4;

function isInteractivePoint(x, y) {
  const element = document.elementFromPoint(x, y);
  if (element && element.closest('.interactive')) return true;

  // 兜底：周围放宽一圈
  for (const [dx, dy] of [
    [GROW, 0], [-GROW, 0], [0, GROW], [0, -GROW],
  ]) {
    const px = x + dx;
    const py = y + dy;
    if (px < 0 || py < 0 || px >= window.innerWidth || py >= window.innerHeight) continue;
    const near = document.elementFromPoint(px, py);
    if (near && near.closest('.interactive')) return true;
  }
  return false;
}

function reportInteractive(next) {
  // 拖拽期间**强制保持不穿透**。
  //
  // 不加这条守卫会怎样（实测踩过）：拖动时指针只要滑出宠物轮廓一点，
  // mousemove 就会判定"不在交互区"、把穿透打开，窗口随即开始忽略鼠标 ——
  // 后续事件不再送到渲染层，拖拽从中间断掉。表现就是"抓不住、只有从
  // 面积大的状态栏拖才勉强能动"。
  if (draggingWindow) next = true;
  if (next === interactiveNow) return;

  if (clickThroughTimer) {
    clearTimeout(clickThroughTimer);
    clickThroughTimer = null;
  }

  if (next) {
    // 变成"可交互"要**立刻**生效，否则指针已经进到宠物上了、窗口却还在穿透，
    // 那一瞬间的按下同样会被丢掉。
    interactiveNow = true;
    pet.setInteractive(true);
    return;
  }

  // 变成"穿透"要**延迟**，见上面 CLICK_THROUGH_DELAY_MS 的说明
  clickThroughTimer = setTimeout(() => {
    clickThroughTimer = null;
    interactiveNow = false;
    pet.setInteractive(false);
  }, CLICK_THROUGH_DELAY_MS);
}

// 主进程用 setIgnoreMouseEvents(true, { forward: true }) 保留了 mousemove 转发，
// 所以指针在穿透区移动时这里依然能收到事件 —— 这是能自动切换的前提。
window.addEventListener('mousemove', (event) => {
  reportInteractive(isInteractivePoint(event.clientX, event.clientY));
});

/**
 * 主进程复位穿透状态后，把本地记录**标为未知**。
 *
 * 为什么必须这么做：主进程在窗口失焦时会主动关闭穿透（自愈，见 petWindow.ts），
 * 而渲染层并不知道这件事。如果渲染层继续以为"我已经是可交互了"，
 * `reportInteractive` 就会因为 `next === interactiveNow` 提前 return、
 * 不再发送任何状态变化 —— 两边不同步，表现就是**拖着拖着就拖不动了**。
 *
 * 标成 null 而不是猜一个值：下次真正需要切换时一定会发出去，
 * 代价只是可能多一次冗余 IPC。
 */
pet.on('pet:interactive-reset', () => {
  interactiveNow = null;
});

/** 上一次测量时的画布像素尺寸（用于"尺寸变了就重算居中"） */
let measuredCanvasSize = null;

/**
 * **最近一次有效的立绘包围盒**（`measureArtBox` 成功时缓存）。
 *
 * ⚠️ 用途：探头期间 `lastArtBox` 可能被重置成 `0x0`（`measureArtBox` 在探头时不跑），
 * 而 `reportArtRect` 需要"立绘在画布内的中心与尺寸"来算锚点。
 * 拿全 0 去算会得到负数垃圾值 —— 实测就是这么把整个定位链路带偏的。
 *
 * 初值按"立绘在画布居中、约 155x177"给（素材的真实比例），
 * 比全 0 至少是个合理量级。
 */
let fallbackArtBox = { w: 155, h: 177, cx: 120, cy: 120 };

/**
 * 从**画布自己的像素**量出立绘的包围盒，并据此让立绘在窗口里居中。
 *
 * 为什么不预先算好常数：最早用 ART_BOX_RATIO（从早期截图量的）推算平移量，
 * 但布局改过几轮之后它就不准了 —— 表现是留白全堆到一侧、贴边判定跟着偏
 * （用户报的就是这个：调完大小之后留白变了）。
 *
 * 现在运行时实测：读一次 canvas 的 ImageData，取 alpha>200 的包围盒，
 * 反推要平移多少才居中。不依赖常数，缩放与换素材都自适应。
 * 开销：240x240 读一次约 1~2ms，只在尺寸变化后做一次，不是每帧。
 */
/** 上一次上报过的立绘包围盒（用于去抖，见 measureArtBox 里的说明） */
// `cx/cy` = 立绘中心在**画布内**的坐标（CSS 像素）。碰撞箱要用它铺矩形，
// 见 `reportArtRect` —— 光有宽高不知道立绘在画布里的哪个位置。
let lastArtBox = { w: 0, h: 0, cx: 0, cy: 0 };

/**
 * 上一次测量时的画布尺寸（`宽x高`）。
 *
 * 测量是**绝对式**的（从"零平移"出发算目标值），所以同一个尺寸被测量两次
 * 就会把偏移叠加两次。而 `measureArtBox` 有启动轮询和改尺寸定时器两个来源，
 * 可能撞在同一尺寸上 —— 用它去重。
 */
let measuredSizeKey = '';

/**
 * 当前是否处于探头（旋转）状态。
 *
 * ⚠️ `measureArtBox` 必须知道这个：它要读 canvas 的像素量包围盒，
 * 而**旋转过的 canvas 量出来是斜的**（包围盒会变大、中心也偏），
 * 拿它算出来的居中平移量是错的。
 * 所以探头期间不测、也不改居中量（探头本来就是绕中心转，不需要重新居中）。
 */
let peekingNow = false;

/**
 * 当前探头旋转角度（度；未探头时 0）。
 *
 * 为什么单独记一个：`reportArtRect()` 要把"立绘中心相对画布中心的偏移"
 * 按这个角度旋转过去（见那里的说明）。从内联样式的 `transform` 字符串里现解析
 * 也能做，但那样每次上报都要解一次矩阵，而且过渡动画期间拿到的是插值中的角度 ——
 * 记下来更直接、也稳。
 */
let currentPeekAngleDeg = 0;

/**
 * 立绘第一帧是否已经抠好（撤销"首帧门闸"用）。
 *
 * 用户报告：「在程序运行成功后，宠物会有几帧是之前宠物的图片」。
 * 实测录制：启动后约 0.1~0.2 秒，宠物窗口位置画的是一块**橙色**
 * （视频首帧 + 黑底抠图还没生效的样子），之后才变成正常立绘。
 *
 * 所以 `.pet` 初始 `opacity: 0`（见 styles.css 的"首帧门闸"），
 * 等第一帧抠好、且立绘覆盖率达标，才加上 `.pet-wrap.ready` 淡入。
 */
function markArtReady() {
  if (artReady) return;
  artReady = true;
  if (els.canvasWrap) els.canvasWrap.classList.add('ready');
  /*
   * ⚠️ 画布让位与气泡定位要挂在**这里**，而不是只挂在"兜底超时"分支里。
   *
   * 踩过：我一开始只加在下面的 `if (!artReady)` 兜底块里 —— 那条路径只在
   * **超过 5 秒还没量到**（异常情况）时才走，正常启动压根不经过，
   * 于是 `--canvas-offset-y` 一直是空的、画布仍被推到 `top = -20`。
   *
   * 这里才是正常成功路径：`sprite.onFirstFrame(markArtReady)` 在第一帧抠好时触发。
   */
  updateCanvasOffset();
  /*
   * ⚠️ 设完让位必须**立刻重测一次**立绘平移。
   *
   * 踩过：`measureArtBox` 在门闸轮询里先跑、`--canvas-offset-y` 后设，
   * 于是那次测量用的让位量还是 0，算出的 `--art-shift-y` 偏差了
   * 让位量的一半（实测 -100 而正确值是 -70）。
   * 后果是气泡的 `--bubble-bottom` 按错误的头顶算 —— 实测气泡底边比
   * 立绘头顶低了 **91px**，直接盖住宠物的脸（用户截图里的现象）。
   *
   * 让位量变了就重测，`measureArtBox` 内部有按尺寸去重的 `measuredSizeKey`，
   * 所以这里要先清掉它，否则会被"同尺寸不重测"挡掉。
   */
  measuredSizeKey = '';
  measureArtBox();
  updateBubbleBottom();
}

let artReady = false;

/*
 * 启动轮询：等第一帧抠好就撤销门闸。
 *
 * ⚠️ 为什么需要它：`measureArtBox` 原本只在**收到"改宠物大小"消息**时才调，
 * 而启动时没有任何人调它 —— 于是门闸永远打不开、宠物一直是隐藏的。
 * （这类"只挂在某个事件上"的函数很容易漏掉启动路径，踩过一次记下来。）
 *
 * 每 80ms 试一次，最多 5 秒；期间一旦达标就自己停掉，不留常驻定时器。
 */
let artReadyTries = 0;
const artReadyTimer = setInterval(() => {
  artReadyTries += 1;
  measureArtBox(); // 达标时它内部会调 markArtReady
  if (artReady || artReadyTries > 62) {
    clearInterval(artReadyTimer);
    if (!artReady) {
      // 兜底：超过 5 秒还没量到，就别让宠物一直隐身
      // （宁可显示可能不完美的画面，也不能让用户以为程序没起来）
      console.warn('[pet] 首帧门闸超时，强制显示立绘');
      markArtReady();
      // 启动时就要算：窗口比画布高（给气泡留的空间），那部分要全放上方，
      // 否则立绘按窗口居中会被推高 —— 等于宠物在屏幕上跳了一截。
      // 不能只放在 `pet:set-pet-size` 里：默认档位（scale 1）根本不会触发那条消息。
      updateCanvasOffset();
      updateBubbleBottom();
    }
  }
}, 80);

/**
 * 上报**立绘在窗口内的矩形**（CSS 像素，轴对齐，含旋转/平移/缩放）。
 *
 * 为什么要报它：主进程要靠这份矩形**自己按光标位置判定穿透**。
 * 原来穿透由渲染层的 `mousemove` 驱动，而那个事件依赖
 * `setIgnoreMouseEvents(true, { forward: true })` 的转发 ——
 * **窗口非前台时不可靠**。用户报的「拖到边缘探头后，打开别的应用就点不动宠物」
 * 就是卡在穿透态：看得见、点不到。
 *
 * 用 `getBoundingClientRect()` 拿到的是**变换后**的矩形（旋转也算进去了），
 * 正是主进程做命中判定需要的形状。
 */
/**
 * 上报**立绘实际占据的矩形**（窗口坐标 = 屏幕坐标 - 窗口位置）。
 *
 * 用户要求：「需要你将宠物的互动拖拽触碰限制在宠物本身的宽高上形成类似游戏的
 * 碰撞箱，这样点击宠物上面的多余窗口位置才不会互动拖拽宠物」。
 *
 * 主进程拿它做**碰撞箱**（决定指针是否"在宠物身上"、以及是否可以起拖拽），
 * 所以这里报的**不是画布**，而是**立绘的可见范围**：
 *   · 常态：以画布内立绘中心为中心、按实测尺寸铺开的矩形
 *     （画布比立绘大一圈，报画布会把留白也算成可点的）；
 *   · 探头：立绘旋转了 45°，轴对齐的可见范围 ≈ 边长的 √2 倍，
 *     绕同一个中心铺开即可（碰撞箱只需要大致贴合，判定再放宽几像素）。
 */
function reportArtRect() {
  const canvas = els.canvas;
  if (!canvas || !window.pet || typeof window.pet.reportArtRect !== 'function') return;
  const r = canvas.getBoundingClientRect();
  if (!(r.width > 0) || !(r.height > 0)) return;
  // 画布内立绘中心（**未平移、未旋转**前的坐标）
  /*
   * ⚠️ **包围盒无效时必须退回缓存值**，否则后面全在拿 `0` 算。
   *
   * 实测（用户 2026-10-04 的 HUD 截图）：
   *
   *     artBox 0x0 @ 0,0        ← 空的
   *     shift  0,0              ← 居中平移算不出来 => 立绘按原始位置画
   *     canvas rect -7,-50 339x339
   *
   * 探头期间 `measureArtBox` 直接 return（`if (peekingNow) return`），
   * 而探头**会重置** `lastArtBox` —— 于是整个探头期间它保持 0。
   * 拿 0 去算 `dy = 0 - r.height/2` 会得到 -169 这种垃圾，
   * 主进程收到的锚点从一开始就是错的。
   *
   * 这就是"改符号怎么改都不对"的原因：**输入坏了，输出不可能对**。
   */
  const boxValid = lastArtBox.w > 0 && lastArtBox.h > 0;
  const artCx = boxValid ? lastArtBox.cx : fallbackArtBox.cx;
  const artCy = boxValid ? lastArtBox.cy : fallbackArtBox.cy;
  const boxW = boxValid ? lastArtBox.w : fallbackArtBox.w;
  const boxH = boxValid ? lastArtBox.h : fallbackArtBox.h;

  /*
   * ⚠️ 探头时**必须把立绘中心也旋转过去**，不能只把尺寸乘 √2。
   *
   * 这里踩过一个很难查的坑（2026-10-03，交接文档第七节·0）：
   * 旧代码是"以**未旋转**的立绘中心为心，铺一个边长 ×√2 的框"。
   * 于是探头态下报出去的 `y` 是**旋转前**的位置，
   * 主进程拿它当锚点反推出"立绘顶部在窗口上方 50px"——几何上不可能
   * （立绘是贴窗口顶的），表现就是拖拽结束立绘往下跳 43px。
   *
   * 正确做法分两步：
   *   1. 取画布中心（= 旋转中心，`transform-origin: center`）；
   *   2. 把"立绘中心相对画布中心的偏移"按当前角度旋转过去。
   * 只旋转**相对偏移**，所以画布自身的位移（`--art-shift` / `--peek-shift`）
   * 已经通过 `r.left/r.top` 体现，不会重复计算。
   */
  const cx = r.left + r.width / 2;
  const cy = r.top + r.height / 2;
  /*
   * ⚠️ **旋转已经在 `.pet-wrap` 上了，`#pet` 永远不旋转** ——
   * 所以这里 `r` 恒为 `240x240`（干净的画布框），
   * "立绘在画布内的偏移" `dx/dy` 就是它在视口里的真实偏移，**不需要再旋转它**。
   *
   * 之前那套 `rotDx/rotDy`（把立绘中心按角度转过去）是**为了绕开
   * "#pet 被旋转后外接矩形变大"**才加的补丁。根因已经移除，补丁一起去掉。
   */
  const dx = artCx - r.width / 2;
  const dy = artCy - r.height / 2;
  /** 立绘在视口里的中心（未旋转的画布 + 未旋转的偏移 = 直接相加） */
  const artCxViewport = cx + dx;
  const artCyViewport = cy + dy;

  /*
   * 碰撞箱：**只把"立绘那 135x172"旋转，不含画布**。
   *
   * 旋转发生在 `.pet-wrap` 上，所以立绘的**可见**范围是"它自己的矩形绕画布中心
   * 转 θ 后的外接矩形"。旧代码是把**整个画布**乘 √2（`boxW * diag`），
   * 那会报出一个 339x339 的巨框 —— 判定太松，点到立绘旁边也算命中，正是
   * 用户要求「点击宠物上面的多余窗口位置才不会互动拖拽宠物」时反对的东西。
   *
   * 绕中心旋转后的外接尺寸（带符号，因为立绘中心不在旋转中心上）：
   *   W' = |w·cosθ| + |h·sinθ|
   *   H' = |w·sinθ| + |h·cosθ|
   */
  const rad = peekingNow ? (currentPeekAngleDeg * Math.PI) / 180 : 0;

  /*
   * ## ⚠️ 碰撞箱必须绕**和 CSS 同一个旋转中心**转（2026-10-04 查出的逻辑漏洞）
   *
   * CSS 里（`styles.css`）：
   *
   *     .pet-wrap { transform-origin: var(--art-origin-x, 120px) var(--art-origin-y, 134px); }
   *
   * 也就是**绕立绘自己的中心**转（`art-origin` = 实测立绘中心，约 `119,127`）。
   *
   * 而旧实现是"绕**画布中心**（约 `125,125`）转，只把尺寸按 AABB 公式放大"：
   *
   *     const cx = r.left + r.width / 2;          // 画布中心
   *     const rotDx = dx * cos - dy * sin;        // 绕画布中心
   *     visibleW = boxW*|cos| + boxH*|sin|
   *
   * 两者**中心不同**（纵向差约 27px），于是探头时算出来的碰撞箱
   * **位置和尺寸都会偏** —— 表现是"探头时点到立绘旁边也算命中 / 点到立绘反而没反应"。
   *
   * ## 正确做法：按 CSS 真实的方式转
   *
   * 把立绘矩形的**四个角**分别绕 `(originX, originY)` 旋转，再取外接框。
   * 这样角度、中心、宽高全都和浏览器的实际渲染一致。
   */
  // ⚠️ 单点定义：与调试叠层用**同一个** `computeHitBox`（见那里的长注释）
  const hit = computeHitBox({ left: r.left, top: r.top, width: r.width, height: r.height });
  const minX = hit.x;
  const minY = hit.y;
  const visibleW = hit.width;
  const visibleH = hit.height;

  /**
   * 立绘相对窗口原点的偏移 —— **姿态无关**，是换尺寸唯一该用的锚点。
   *
   * ## ⚠️ 这个值**不随旋转角变化**（关键，也是这轮修复的核心）
   *
   * 算法走的是**画布中心** `(r.left + r.width/2, r.top + r.height/2)`：
   * 它是**旋转中心**，转多少度都不变（`r` 的外接尺寸会变，但中心不变）。
   * 所以算出来的偏移只由"画布在窗口里的位置 + 立绘在画布里的位置"决定。
   *
   * ## 那"探头时偏移变成 68,12"是怎么回事？
   *
   * 那是**旧实现**的毛病：旧写法用 `r.left + box.cx - box.w/2`，
   * 其中 `r.left` 来自**旋转外接框**，会随角度漂 —— 于是偏移看起来"随旋转变"
   * （实测静止 `(39,14)`、探头 `(68,12)`）。
   * 主进程拿这种漂移值当锚点换尺寸，立绘就被挪走（实测跳变 `Δ79,-3`、`Δ-203,0`）。
   *
   * 现在它稳定了：**同一个窗口几何下，转不转都是同一个值**。
   */
  const sharedOffset = artOffsetInWindow();

  window.pet.reportArtRect({
    // ⚠️ 用四角旋转算出的外接框左上角，**不要**用 `中心 − 尺寸/2`
    // —— 旋转中心不是立绘中心时，那个写法是错的（见上面四角法的说明）。
    x: Math.round(minX),
    y: Math.round(minY),
    width: Math.round(visibleW),
    height: Math.round(visibleH),
    uprightX: sharedOffset.x,
    uprightY: sharedOffset.y,
    uprightWidth: Math.round(boxW),
    uprightHeight: Math.round(boxH),
    /*
     * **当前旋转角**（度）—— 主进程用它判断"这份几何是不是静止态的"。
     *
     * 加它的理由（2026-10-04）：吸附定位要一个**常量**左留白，而实测值只在
     * "旋转角为 0"时才稳定（旋转期间实测值会漂：`-30 → -2 → 8 → 27 → 29`）。
     * 主进程自己看不到角度（CSS 变量在渲染层），所以由这里报上去。
     */
    artRotationDeg: peekingNow ? currentPeekAngleDeg : 0,
    /*
     * **当前的探头位移**（`--peek-shift-x`，逻辑像素）。
     *
     * 主进程用它判断"这份几何是不是**真正的常态**"。为什么不能只看旋转角：
     *
     *     退出边缘探头（开始拖拽，先拉直）      <- 只拉直，edgePeek 仍为 true
     *     0,320 386x388 ... -> left（当前探头=true）   <- 窗口还是探头尺寸
     *
     * 那一刻**旋转已经是 0°**，但窗口仍是探头尺寸、位移也还在 ——
     * 只看旋转角会把这份"半探头"的几何当成常态缓存起来，于是吸附目标值漂移
     * （实测缓存从 33 变成 38，而 38 = (画布宽−立绘宽)/2 正是被污染的标志）。
     */
    artPeekShiftX: Number.parseFloat(
      els.canvasWrap ? getComputedStyle(els.canvasWrap).getPropertyValue('--peek-shift-x') : '0',
    ) || 0,
    /*
     * 立绘相对**窗口原点**的偏移（未旋转姿态）。
     *
     * ## ⚠️ 这里**不要**换算成屏幕坐标（踩过，代价是"探头结束闪现"）
     *
     * 旧代码报的是 `window.screenX + 视口坐标`，主进程再减回 `win.x`。
     * 看着对称，但 `cx/cy` 来自 `getBoundingClientRect()`（**视口**坐标），
     * 把它和 `window.screenX`（**屏幕**坐标）相加，只要窗口不在 (0,0) 附近、
     * 或视口原点与窗口左上角不一致，就会引入偏差。
     *
     * 实测后果（日志原文）：
     *
     *     恢复尺寸 -> 窗口 1189,477 387x388 => 1189,477 250x335
     *                 | 立绘屏幕 1401,446 -> 1401,446 | 偏移 {"x":212,"y":-31}
     *                 偏移 x=212，而真实值约 23  ← 混进了一次屏幕坐标
     *
     * 主进程拿这个脏偏移去算"新窗口原点 = 当前 − 偏移"，**窗口直接被搬走 212px** ——
     * 用户看到的就是"探头结束时闪一下"。
     *
     * 现在调用**单点定义的 `artOffsetInWindow()`**（用画布中心算，旋转期间也稳定），
     * 调试叠层显示的是同一个函数的结果 —— 两边永远一致。
     */
    artOffsetX: sharedOffset.x,
    artOffsetY: sharedOffset.y,
  });
}

let artRectTimer = null;
/** 合并短时间内的多次请求（resize/旋转都会连发几次） */
function scheduleArtRectReport() {
  if (artRectTimer) clearTimeout(artRectTimer);
  artRectTimer = setTimeout(() => {

    artRectTimer = null;
    reportArtRect();
  }, 80);
}

/*
 * 🔴 实时上报（2026-10-18 加，300ms 节流，**模块层**启动）：
 *
 * 锚点（立绘矩形）原来只在「改尺寸 / 旋转 / 启动」时上报一次，之后**全靠缓存**。
 * 但窗口是会被拖来拖去的 —— 拖到屏幕左上角后，主进程拿到的还是旧位置的框，
 * 气泡定位就按旧框算（实测：宠物已到 y388，框还报 y502，差 114px，
 * 气泡正好糊在宠物头上 —— 用户截图报）。
 *
 * 每 300ms 重发一次同样的 payload：主进程的锚点永远 ≤300ms 新鲜，
 * 气泡跟着宠物走（followAnchor 本来就是为这个设计的）。窗口静止时
 * 上报值不变，followAnchor 里 setBounds 等值调用无副作用。
 * document.hidden 时跳过（后台不占 CPU）。
 * ⚠️ 必须在**模块层**启动 —— 之前误塞进 scheduleArtRectReport 的 setTimeout
 *    回调里，结果要等一次 resize/rotate 才会启动（实测踩到）。
 */
if (!window.__artRectLiveTimer) {
  window.__artRectLiveTimer = setInterval(() => {
    if (document.hidden) return;
    try {
      reportArtRect();
    } catch (error) {
      console.warn('[art] 实时上报失败', error);
    }
  }, 300);
}

function measureArtBox() {
  // 探头态下不测：canvas 被旋转了，量出来的包围盒不代表立绘在画布里的位置
  if (peekingNow) return;

  if (!els.canvas || !els.canvasWrap) return;

  /*
   * **同一个尺寸只量一次**。
   *
   * 测量是**绝对式**的（从"零平移"出发算目标值），所以每多测一次就多叠加一份偏移。
   * 而 `measureArtBox` 有两个调用来源 —— 启动轮询（`artReadyTimer`）和
   * 改尺寸后的定时器 —— 它们可能撞在同一个尺寸上。去重之后两者各只生效一次。
   *
   * 尺寸变化时由 `pet:set-pet-size` 把 `measuredSizeKey` 清掉。
   */
  const sizeKey = `${els.canvas.clientWidth}x${els.canvas.clientHeight}`;
  if (measuredSizeKey === sizeKey) return;
  measuredSizeKey = sizeKey;

  try {
    // willReadFrequently: 告诉 Chromium 这块 canvas 会被反复读像素，
    // 让它走 CPU 后端、避免每次 getImageData 都从 GPU 回读。
    // （不加的话每次测量都会在控制台刷一条 Canvas2D 警告，实测。）
    const ctx = els.canvas.getContext('2d', { willReadFrequently: true });
    if (!ctx) return;
    const w = els.canvas.width;
    const h = els.canvas.height;
    /*
     * ⚠️ **画布尺寸变了**（探头时为容纳旋转后的内容会把它撑大）时，
     * 旧的 `--art-shift-*` 是按**旧尺寸**算的，必须先清掉再按新尺寸重算 ——
     * 否则立绘会停在"按旧尺寸居中"的位置，相对变大的画布**看起来就是跑到了中央**
     * （用户定位的现象：「画布一变大，它就闪现向变大后的画布中央」）。
     *
     * 清零放在**测量之前**，而且只清一次（这里已是测量入口）。
     * 调用方 `pet:peek` / `set-pet-size` 也做了清零，两处不冲突（幂等）。
     */
    if (measuredCanvasSize && (measuredCanvasSize[0] !== w || measuredCanvasSize[1] !== h)) {
      els.canvasWrap?.style.setProperty('--art-shift-x', '0px');
      els.canvasWrap?.style.setProperty('--art-shift-y', '0px');
    }
    measuredCanvasSize = [w, h];
    // 采样步长 2：立绘有几百像素宽，步长 2 足够准，还能省 4 倍开销
    const step = 2;
    const data = ctx.getImageData(0, 0, w, h).data;
    let minX = w;
    let minY = h;
    let maxX = -1;
    let maxY = -1;
    for (let y = 0; y < h; y += step) {
      for (let x = 0; x < w; x += step) {
        // alpha 阈值取高一点：drop-shadow 的羽化会和本体连通，阈值低会把阴影算进去
        if (data[(y * w + x) * 4 + 3] > 200) {
          if (x < minX) minX = x;
          if (x > maxX) maxX = x;
          if (y < minY) minY = y;
          if (y > maxY) maxY = y;
        }
      }
    }
    if (maxX < 0) return; // 还没画出来
    /*
     * ⚠️ 这里**不再**用像素覆盖率判断"能不能显示"。
     *
     * 用户第二次反馈：「还是会有一些」—— 说明覆盖率判据不够严：
     * 起手那几帧虽然覆盖率和正常立绘接近，但内容仍是没抠好的原始画面。
     *
     * 现在改成**由视频层自己报告第一帧就绪**（`sprite.onFirstFrame` ->
     * `videoPlayer.drawKeyed()` 末尾触发），不再靠猜。
     * 门闸的兜底超时由下面的 artReadyTimer 负责。
     */
    // 画布是 dpr 放大的，这里量到的是物理像素 -> 换回 CSS 像素
    const dpr = w / (els.canvas.clientWidth || w);
    /*
     * 平移量 = **残余**（从"零平移"出发，所以等于绝对目标值）。
     *
     * 调用方（`pet:set-pet-size`）会先把 `--art-shift` 清零，并且只在
     * transform 过渡（460ms）走完之后才调这里，所以：
     *   · 无历史累加 —— 每次都是同一个确定起点；
     *   · 无过渡中途值 —— 量到的是稳定后的位置。
     *
     * `getImageData` 读的是 canvas 像素缓冲，**不含** .pet-wrap 的 CSS 平移，
     * 所以「画布中心 - 盒中心」这个残余**与平移无关**（实测：shift 取
     * 0/-50/50/-120，盒中心恒为 133.2）。因此一次测量就是最终答案。
     */
    const shiftX = (w / 2 - (minX + maxX + 1) / 2) / dpr;
    /*
     * ⚠️ 纵向的目标中心是**窗口中心**，不是画布中心。
     *
     * 窗口为了给气泡留空间而加高（`--canvas-offset-y` 让画布下移），此时
     * "画布中心"与"窗口中心"差着让位量的一半。若仍按画布中心对齐，
     * 立绘会被 `--art-shift-y` 反向推回去 —— 实测 shiftY = -104、
     * 画布最终落在 `top = -20`（跑出窗口），等于让位白做了。
     */
    const canvasOffsetY = els.canvasWrap
      ? Number.parseFloat(els.canvasWrap.style.getPropertyValue('--canvas-offset-y')) || 0
      : 0;
    const targetCenterY = h / 2 + canvasOffsetY * dpr;
    const shiftY = (targetCenterY - (minY + maxY + 1) / 2) / dpr;
    els.canvasWrap.style.setProperty('--art-shift-x', `${shiftX.toFixed(1)}px`);
    els.canvasWrap.style.setProperty('--art-shift-y', `${shiftY.toFixed(1)}px`);
    // 把量到的立绘尺寸也报给主进程：贴边判定与拖拽放宽都要用它。
    // ⚠️ 只在真的变了才报：否则尺寸的每一点抖动都会触发主进程重排 +
    // 对话条重新定位，日志里会刷几十条重复记录（实测踩过）。
    const artW = Math.round((maxX - minX + 1) / dpr);
    const artH = Math.round((maxY - minY + 1) / dpr);
    // 缓存这次的有效包围盒 —— 探头期间 `lastArtBox` 可能被重置成 0，
    // 那时 `reportArtRect` 要退回这份值（见 fallbackArtBox 的说明）
    if (artW > 0 && artH > 0) {
      fallbackArtBox = { w: artW, h: artH, cx: (minX + maxX + 1) / 2 / dpr, cy: (minY + maxY + 1) / 2 / dpr };
      /*
       * 旋转中心 = **立绘自己的中心**（CSS 像素，相对 `.pet-wrap` 左上角）。
       *
       * `.pet-wrap` 铺满窗口（240x323），默认 `transform-origin: center` 是 `(120,161)`，
       * 而立绘中心在 `(120,134)` —— 低 27px。绕偏下的点转 45°，立绘会**画弧**：
       * 探针实测「探头时立绘屏幕 y 从 300 → 287」（往上 13px），
       * 用户看到的就是「向右上飘逸」。
       *
       * `fallbackArtBox.cx/cy` 是窗口内坐标（画布贴窗口左上角 + 立绘在画布内的中心），
       * 与 `transform-origin`（相对元素左上角）同一套，可直接用。
       */
      els.canvasWrap?.style.setProperty('--art-origin-x', `${fallbackArtBox.cx.toFixed(1)}px`);
      els.canvasWrap?.style.setProperty('--art-origin-y', `${fallbackArtBox.cy.toFixed(1)}px`);
    }
    /*
     * 立绘中心在**画布内**的坐标（CSS 像素）。
     *
     * 碰撞箱要用它：`reportArtRect` 以这个中心铺开一个"立绘大小"的矩形，
     * 而不是直接报整个画布 —— 画布比立绘大一圈，报画布会把留白也算成可点的
     * （用户要求：「点击宠物上面的多余窗口位置才不会互动拖拽宠物」）。
     */
    const artCx = (minX + maxX + 1) / 2 / dpr;
    const artCy = (minY + maxY + 1) / 2 / dpr;
    if (Math.abs(artW - lastArtBox.w) > 1 || Math.abs(artH - lastArtBox.h) > 1) {
      lastArtBox = { w: artW, h: artH, cx: artCx, cy: artCy };
      scheduleArtRectReport();
      /*
       * ⚠️ 是 `pet.reportArtBox`，**不是** `window.__pet.reportArtBox`。
       *
       * 这两个对象名字很像但完全不同：
       *   · `pet`（来自 preload 的 `window.pet`）—— 桥接 API，`reportArtBox` 在这里；
       *   · `window.__pet`（本文件里挂的）—— **调试对象**，只有 sprite / els / audioStats。
       *
       * 我原来写成了 `window.__pet.reportArtBox`，于是每次测量都抛
       * `TypeError: ... is not a function`，被 catch 吞掉。
       * 后果不止"少报一个尺寸"：`measuredSizeKey` 已经被置上，
       * **同一个尺寸再也不会重测** —— 立绘于是永远不居中（用户报的
       * 「宠物太小或太大会导致窗口中宠物身体不全」）。
       */
      window.pet.reportArtBox(artW, artH);
    }
  } catch (error) {
    console.warn('[pet] 量立绘包围盒失败（可能跨源污染画布）', error);
  }
}

/**
 * 菜单里改了「宠物大小」——主进程已经把窗口调好，这里把画布也调成同一尺寸。
 *
 * 两边必须同步：只改窗口会让立绘悬在中间一小块，只改画布会让窗口留一片空白。
 * 画布保持正方形（立绘是正方形素材），尺寸由主进程按同一份基准算出来。
 */
pet.on('pet:set-pet-size', (payload) => {
  /*
   * ⚠️ 调试叠层开关必须放在**最前面**（在所有 return 之前）。
   *
   * 踩过：一开始写在 `size` 校验之后，而这条消息在默认档位下
   * **只有 `debugHud` 一个字段**（没有 `size`），于是第 3 行就 `return` 了 ——
   * 叠层永远不生效（DOM 检查：`#debug-hud 存在 false`）。
   */
  setDebugHud(payload && payload.debugHud);

  const size = Number(payload && payload.size);
  if (!Number.isFinite(size) || size < 48) return;
  sprite.setSize(size, size);
  // 画布尺寸变了 -> 窗口与画布的高度差也变了，重算"画布向下让位"
  updateCanvasOffset();
  /*
   * 尺寸变了 -> **先把平移清零**，等过渡走完再量一次并居中。
   *
   * ---- 为什么必须清零（踩了很久） ----
   *
   * `--art-shift-x/y` 是**按当前画布尺寸**算出来的像素值。改了画布尺寸之后，
   * 上一尺寸的平移值就失去意义了；如果留着继续做"旧值 + 残余"，就会
   * **跨尺寸累加**。实测：每换一档，立绘的屏幕偏移就按上一步的量继续叠加
   * （偏移 -27 → -60 → -113 → -194），表现为"宠物越小/越大越偏、身体跑出窗口"。
   * 清零之后每次都从"没有平移"这个确定状态出发，不依赖历史。
   *
   * ---- 为什么只量一次就够 ----
   *
   * 实测确认：`getImageData` 读的是 canvas 像素缓冲，**不含** CSS 平移，
   * 所以「画布中心 - 包围盒中心」是一个**与平移无关的常量**（把 shift 设成
   * 0/-50/50/-120 测得盒中心恒为 133.2）。既然残余不随平移变化，
   * 从零出发测一次就是答案，重复测量不会更准，反而把动画帧漂移叠进去。
   *
   * 时机：`.pet-wrap` 的 transform 过渡是 **460ms**，必须等它走完再量；
   * 早量会读到"还在移动中"的位置。
   */
  // 允许对新尺寸重新测量（去重标记是按尺寸记的）
  measuredSizeKey = '';
  els.canvasWrap.style.setProperty('--art-shift-x', '0px');
  els.canvasWrap.style.setProperty('--art-shift-y', '0px');
  /*
   * ⚠️ 等待时长必须**读 CSS 变量**，不能写死。
   *
   * 原来写死 520ms，而当时过渡是 460ms —— 刚好够。
   * 后来过渡统一改成 `--peek-rotate-ms`（现在 1500ms），这里就**太早了**：
   * 会在动画途中量尺寸，量到的立绘宽高偏小/偏大，
   * 而 `measuredArt` 是**穿透命中判定**和**拖拽留白**的输入 —— 会连带一起偏。
   */
  const transitionMs =
    Number.parseFloat(
      getComputedStyle(els.canvasWrap).getPropertyValue('--peek-rotate-ms'),
    ) || 500;
  setTimeout(measureArtBox, Math.ceil(transitionMs) + 60);
  // 立绘矩形也要重报：主进程靠它做穿透命中判定
  scheduleArtRectReport();
  // 探头时的缩放倍率（用户要求旋转不缩小，所以主进程给 1）
  const fit = Number(payload && payload.peekScale);
  if (Number.isFinite(fit) && fit > 0 && els.canvasWrap) {
    els.canvasWrap.style.setProperty('--peek-scale', String(fit));
  }
});

/**
 * 右键菜单里的「戳一下」：与"点宠物"走**完全相同**的路径，
 * 这样免打扰判定、点击反应动画都一致，不会出现两套行为。
 *
 * ⚠️ 不再弹「你戳了它一下…」的提示（用户要求：「把戳了他一下这个弹窗移除」）。
 * 立绘的反应动画本身就是反馈，再弹一条文字反而吵。
 *
 * ⚠️ 与点宠物同理：**不让它开口**（2026-10-07 用户要求）——
 * 反馈由这个本地动画提供，不再产生一次 LLM 调用。
 */
pet.on('pet:poke-request', () => {
  pet.poke();
  if (sprite.currentState !== 'drag') {
    sprite.play('happy');
    setTimeout(() => {
      if (sprite.currentState === 'happy') sprite.play('idle');
    }, POKE_REACTION_MS);
  }
});

/**
 * 紧凑模式（"趴着"）。
 *
 * 主进程检测到**别的应用全屏**（打游戏、看全屏视频）时通知这里：
 * 隐藏输入卡片、只留宠物本体，并把窗口缩到只包住立绘。
 *
 * 为什么由主进程决定：窗口尺寸与层级都在主进程，渲染层无法自行判断
 * "别人是不是全屏"。渲染层只负责按通知换布局。
 */
pet.on('pet:compact', (payload) => {
  const compact = Boolean(payload && payload.compact);
  const side = payload && payload.side === 'left' ? 'left' : 'right';
  document.body.classList.toggle('compact', compact);
  // 贴哪一边 —— CSS 据此把画布推到窗口内侧，否则立绘会浮在窗口中间，
  // 离屏幕边缘留一条缝，看起来不像"贴着边探头"
  document.body.classList.toggle('peek-right', compact && side === 'right');
  document.body.classList.toggle('peek-left', compact && side === 'left');
  // 姿态本身由 pet:peek 控制（旋转 45°），这里**不动画**。
  //
  // 为什么不换成"趴着"的动画：素材里确实有一段侧躺（我从 `move/左转奔跑`
  // 里剪出来了），但用户看过觉得不好看，所以统一改成"站姿 + 旋转"。
  // 那个片段和 `peek` 状态保留着，想换回去只要在这里 play('peek') 即可。
});

/**
 * 边缘探头：拖到屏幕左/右边缘后，**整块画布**旋转 45° 窥视。
 *
 * 角度与方向都由主进程给（`petLayout.ts` 的 `PEEK_ANGLE_DEG` + 贴哪一边），
 * 渲染层只负责应用 —— 窗口怎么摆在主进程，姿态怎么转在渲染层，各管一半。
 *
 * ⚠️ **用 CSS transform 转画布，而不是让 canvas 自己画旋转**。
 * 这是用户的要求：「就用现在宠物待机的大小将画布旋转就好了」。
 * 两种做法的差别很实际：
 *   · canvas 自己转：得算旋转后的外接矩形，为了"装进画布"必须缩一次，
 *     宠物就变小了；不缩又会被画布裁掉 —— 两头不讨好（两个版本都试过）。
 *   · CSS 转整块画布：内容一个像素都不重画，**不缩小也不裁切**。
 * 代价是旋转后的画布会超出窗口边界，所以窗口尺寸必须按旋转后的外接矩形算
 * （见 main 侧的 `peekWindowSizeFor`），否则又会被窗口切掉。
 */

/**
/**
 * 调试叠层：给画布染色 + 实时显示坐标。
 *
 * ## 为什么要它（省掉几轮无效排查）
 *
 * 探头定位那串 bug（立绘"往右下/往下跳"）排查了很多轮，方式是：
 * 我写探针 -> 用户录屏 -> 我抽帧量 -> 猜 -> 再改。
 * 每一轮都要用户配合，而且**看不到中间量**（窗口坐标、立绘偏移、
 * peek 位移、旋转角），只能从像素反推。
 *
 * 有了这个叠层，用户截一张图就能把全部关键数字给我，一步到位。
 *
 * 开关：环境变量 `PET_DEBUG_HUD=1`（主进程读它，通过 `pet:set-pet-size` 的
 * `debugHud` 字段传下来）。默认关闭，不影响正常观感。
 *
 * 染色用**半透明**，立绘仍可见（否则没法判断"立绘到底在哪"）。
 */
function ensureDebugHud() {
  if (debugHudEl) return debugHudEl;
  const el = document.createElement('div');
  el.id = 'debug-hud';
  /*
   * ⚠️ 位置选**左下角**，不是左上角。
   *
   * 实测：立绘**贴窗口顶部**（窗口 240x323 里立绘占 y 48..220），
   * 所以左上角的 HUD（约 120px 高）会**正好盖住宠物的头**，
   * 想看的细节全被挡住。左下角那块是"给气泡留的透明区"，
   * 基本没有内容，放那里不挡任何东西。
   */
  el.style.cssText = [
    'position:fixed', 'left:0', 'bottom:0', 'z-index:99999',
    'font:11px/1.35 ui-monospace,Consolas,monospace',
    'color:#eaf6ff', 'background:rgba(10,16,28,0.78)',
    'padding:4px 6px', 'white-space:pre', 'pointer-events:none',
    'border:1px solid rgba(120,190,255,0.5)', 'border-radius:3px',
  ].join(';');
  document.body.appendChild(el);
  debugHudEl = el;
  return el;
}

let debugHudEl = null;

/** 调试叠层是否开启（由主进程按 `PET_DEBUG_HUD` 环境变量传下来） */
let debugHudEnabled = false;
let debugHudTimer = 0;
/**
 * 上一次 HUD 刷新时的"立绘屏幕位置" —— 用来检测**跳变**（见 `updateDebugHud`）。
 *
 * 排查"探头结束闪现"用：主进程侧的日志已排除窗口搬运与换尺寸，
 * 所以跳变只可能来自渲染层，这里直接抓。
 */
let lastHudScreen = null;

/**
 * 当前的"旋转动画结束"监听器（见 `pet:peek` 里挂 `transitionend` 的地方）。
 *
 * 单独存一份是为了**挂新的之前先摘旧的** —— `pet:peek` 会被连续调用，
 * 不摘的话同一个 `transitionend` 会触发一堆叠加的回调。
 */
let peekTransitionListener = null;

/** 开启/关闭调试叠层 */
function setDebugHud(on) {
  const want = Boolean(on);
  if (want === debugHudEnabled) return;
  debugHudEnabled = want;
  setDebugTint(want);
  if (want) {
    updateDebugHud();
    // ⚠️ 用定时器刷新，不接进 sprite 的绘制循环 —— 那会和它抢画布状态。
    // 60ms 足够看，也不费。
    if (!debugHudTimer) debugHudTimer = window.setInterval(updateDebugHud, 60);
  } else {
    if (debugHudTimer) {
      window.clearInterval(debugHudTimer);
      debugHudTimer = 0;
    }
    debugHudEl?.remove();
    debugHudEl = null;
  }
}

/**
 * 算**碰撞箱**（命中判定的外接框）—— 视口坐标。
 *
 * ## ⚠️ 单点定义：`reportArtRect`（上报给主进程）和调试叠层都调它
 *
 * 以前是两处各算一遍，几何改了一次、另一处忘了改 —— "探头时碰撞箱偏"就是这么来的。
 *
 * ## 为什么必须**四角旋转**，不能只放大尺寸
 *
 * CSS 里 `.pet-wrap { transform-origin: var(--art-origin-x/y) }` —— 绕的是
 * **立绘自己的中心**（实测约 `119,127`），不是画布中心（约 `125,125`）。
 * 中心不同的两个旋转，外接框的**位置和尺寸都不一样**。
 *
 * 旧实现是"以画布中心为心、把尺寸按 `|cos|·w + |sin|·h` 放大" —— 绕错了中心，
 * 于是探头时碰撞箱整体偏（纵向约 27px），表现是
 * 「点到立绘旁边也算命中 / 点到立绘反而没反应」。
 *
 * 现在按 CSS 真实的方式：把立绘矩形四角绕 `transform-origin` 旋转，再取外接框。
 */
function computeHitBox(viewport) {
  const box = lastArtBox;
  const valid = box.w > 0 && box.h > 0;
  const w = valid ? box.w : fallbackArtBox.w;
  const h = valid ? box.h : fallbackArtBox.h;
  const bcx = valid ? box.cx : fallbackArtBox.cx;
  const bcy = valid ? box.cy : fallbackArtBox.cy;

  // 立绘中心（视口坐标）= 画布中心 + 立绘在画布内的偏移
  const acx = viewport.left + viewport.width / 2 + (bcx - viewport.width / 2);
  const acy = viewport.top + viewport.height / 2 + (bcy - viewport.height / 2);

  const rad = (peekingNow ? currentPeekAngleDeg : 0) * (Math.PI / 180);
  const cos = Math.cos(rad);
  const sin = Math.sin(rad);
  // 旋转中心 = CSS 的 transform-origin（相对 `.pet-wrap`，它铺满窗口 => 原点即窗口左上角）
  const wrapStyle = els.canvasWrap ? getComputedStyle(els.canvasWrap) : null;
  const cssNum = (name, fallback) => {
    if (!wrapStyle) return fallback;
    const v = Number.parseFloat(wrapStyle.getPropertyValue(name));
    return Number.isFinite(v) ? v : fallback;
  };
  const ox = viewport.left + cssNum('--art-origin-x', w / 2);
  const oy = viewport.top + cssNum('--art-origin-y', h / 2);

  let x0 = Infinity;
  let y0 = Infinity;
  let x1 = -Infinity;
  let y1 = -Infinity;
  for (const [px, py] of [
    [acx - w / 2, acy - h / 2],
    [acx + w / 2, acy - h / 2],
    [acx + w / 2, acy + h / 2],
    [acx - w / 2, acy + h / 2],
  ]) {
    const dx = px - ox;
    const dy = py - oy;
    const rx = ox + dx * cos - dy * sin;
    const ry = oy + dx * sin + dy * cos;
    if (rx < x0) x0 = rx;
    if (rx > x1) x1 = rx;
    if (ry < y0) y0 = ry;
    if (ry > y1) y1 = ry;
  }
  return { x: x0, y: y0, width: x1 - x0, height: y1 - y0 };
}


/**
 * 启动兜底：**只在主进程漏发尺寸时**才自己对齐一次画布。
 *
 * ## ⚠️ 为什么不能"启动时直接同步"（2026-10-04 诊断抓到的）
 *
 * 我一开始是无条件对齐，诊断日志立刻显示它**打断了正确流程**：
 *
 *     画布 240x240 -> 252x252   ← startupSyncCanvas（`window.innerWidth` 取到 **252**）
 *     画布 252x252 -> 250x250   ← pet:set-pet-size（主进程的正确值）
 *
 * 启动那一刻 `window.innerWidth` 还包含窗口的非客户区（252），而**画布应该等于
 * 客户区宽度（250）**。所以我"按 innerWidth 对齐"取到的是**错的值**，
 * 先跳一下、再被主进程纠正 —— 用户看到的就是"忽大忽小"。
 *
 * ## 正确做法
 *
 * **只信主进程**（`PET_SIZE` / `pet:set-pet-size` 都来自 `petSizeFor(scale)`，
 * 与窗口几何一致）。这里只做兜底：延迟一段时间后，若画布**还是那个临时默认值**
 * （`PET_SIZE`），说明主进程的消息确实漏了，才自己对齐。
 */
function startupSyncCanvasFallback() {
  /*
   * ⚠️ 用**全局 `setTimeout`**，不要用 `window.setTimeout` ——
   * 渲染层测试（`tools/test-renderer.mjs`）的 `window` 是**桩对象**，没有这个方法，
   * 会直接 `TypeError: window.setTimeout is not a function`（门禁抓到过）。
   * 浏览器里两者等价。
   */
  setTimeout(() => {
    // 主进程已经发过尺寸了（画布不是临时默认值）—— 什么都不做
    if (sprite.width !== PET_SIZE) return;
    const w = window.innerWidth;
    if (!Number.isFinite(w) || w < 48) return;
    /*
     * 走到这里说明**主进程没下发尺寸**（画布还是 `PET_SIZE` 这个临时默认值）——
     * 这是异常情况，值得告警。正常路径下这个回调什么都不做。
     */
    console.warn(`[pet] 主进程未下发尺寸，兜底按窗口宽度对齐：${sprite.width} -> ${w}`);
    sprite.setSize(w, w);
  }, 2500);
}

/**
 * 立绘左边/上边相对**窗口原点**的偏移 —— 上报给主进程当锚点的那个值。
 *
 * ## ⚠️ 单点定义：`reportArtRect` 和调试叠层都调它（别再各写一遍）
 *
 * ## ⚠️ 用**画布中心**，不要用 `r.left + box.cx`
 *
 * 踩过的坑（2026-10-04「探头结束闪现」）：旧写法是
 *
 *     x = r.left + box.cx - box.w / 2
 *
 * `box.cx` 是**画布内**坐标，而 `r`（`getBoundingClientRect()`）在旋转时
 * 返回的是**外接矩形** —— 尺寸会随角度变大（实测 `250 -> 271`，即 ×√2 过程中）。
 * 两者不同步，于是**回正动画期间偏移会漂移**（实测从 44,12 漂到 29,5）。
 * 主进程拿这个漂移中的偏移去定位 → 位移 41px。
 *
 * 画布中心 `(r.left + r.width/2, r.top + r.height/2)` 是**旋转中心**，
 * 无论转到什么角度它都等于画布的真实中心 —— 所以从它出发算偏移是稳定的。
 *
 * 推导：
 *   立绘中心(视口) = 画布中心(视口) + (box.cx - 画布边长/2, box.cy - 画布边长/2)
 *   立绘左上(视口) = 立绘中心 - box 的一半
 * 而 `box.cx/cy` 与画布边长都是**画布内**坐标，与旋转无关。
 */
function artOffsetInWindow() {
  const cv = els.canvas;
  if (!cv) return { x: 0, y: 0 };
  const r = cv.getBoundingClientRect();
  const box = lastArtBox;
  const size = cv.clientWidth || r.width;
  if (!(size > 0) || !(box.w > 0) || !(box.h > 0)) return { x: 0, y: 0 };
  const canvasCx = r.left + r.width / 2;
  const canvasCy = r.top + r.height / 2;
  return {
    x: Math.round(canvasCx + (box.cx - size / 2) - box.w / 2),
    y: Math.round(canvasCy + (box.cy - size / 2) - box.h / 2),
  };
}

/** 每帧刷新叠层内容（只有开启时才建 DOM） */
function updateDebugHud() {
  if (!debugHudEnabled) return;
  const el = ensureDebugHud();
  const cv = els.canvas;
  const wrap = els.canvasWrap;
  if (!cv || !wrap) return;
  const r = cv.getBoundingClientRect();
  const cs = getComputedStyle(wrap);
  const num = (name) => Number.parseFloat(cs.getPropertyValue(name)) || 0;

  // 立绘在画布内的实测包围盒（`measureArtBox` 的结果）
  const box = lastArtBox;
  // 立绘相对窗口原点的偏移（**单点定义**，与实际上报用同一个函数）
  const offsetInWindow = artOffsetInWindow();
  const screenX = window.screenX + offsetInWindow.x;
  const screenY = window.screenY + offsetInWindow.y;
  /*
   * 碰撞箱（命中判定用的外接框）—— **与上报给主进程的是同一个函数**。
   *
   * 之前这里自己又算了一遍几何，结果和 `reportArtRect` 各改各的、慢慢漂移
   * （本项目踩过多次这种"两处各算一遍"的坑）。现在单点定义（`computeHitBox`），
   * 所以 HUD 显示的就是主进程实际拿到的那一份。
   */
  const hitBox = computeHitBox({ left: r.left, top: r.top, width: r.width, height: r.height });


  /*
   * ⚠️ **立绘屏幕位置突变检测**（排查"探头结束闪现"用）。
   *
   * 主进程那边的日志已经证明：窗口没被搬、换尺寸前后立绘屏幕位置一致。
   * 那"闪现"就只能来自渲染层。这条检测在**位置每次跳变 >= 15px** 时打印
   * 全部相关量，一眼就能看出是 `shift` / `peek` / 旋转 / 画布尺寸 哪一项变的。
   *
   * 只打印，不影响任何行为。
   */
  if (lastHudScreen) {
    const dx = screenX - lastHudScreen.x;
    const dy = screenY - lastHudScreen.y;
    if (Math.abs(dx) >= 15 || Math.abs(dy) >= 15) {
      console.warn(
        `[跳变] 立绘屏幕 ${lastHudScreen.x},${lastHudScreen.y} -> ${screenX},${screenY}` +
          `（Δ${dx},${dy}）` +
          ` | win ${window.screenX},${window.screenY}` +
          ` | 偏移 ${offsetInWindow.x},${offsetInWindow.y}` +
          ` | shift ${num('--art-shift-x')},${num('--art-shift-y')}` +
          ` | peek ${num('--peek-shift-x')},${num('--peek-shift-y')} ${currentPeekAngleDeg}°` +
          ` | canvas ${cv.clientWidth}x${cv.clientHeight} backing ${cv.width}` +
          ` | rect ${Math.round(r.left)},${Math.round(r.top)} ${Math.round(r.width)}x${Math.round(r.height)}` +
          ` | artBox ${Math.round(box.w)}x${Math.round(box.h)} @ ${Math.round(box.cx)},${Math.round(box.cy)}`,
      );
    }
  }
  lastHudScreen = { x: screenX, y: screenY };

  const lines = [
    `win   ${window.screenX},${window.screenY}  ${window.innerWidth}x${window.innerHeight}`,
    `canvas client ${cv.clientWidth}x${cv.clientHeight}  backing ${cv.width}x${cv.height}`,
    `canvas rect   ${Math.round(r.left)},${Math.round(r.top)}  ${Math.round(r.width)}x${Math.round(r.height)}`,
    `artBox ${Math.round(box.w)}x${Math.round(box.h)} @ ${Math.round(box.cx)},${Math.round(box.cy)}`,
    `shift  ${num('--art-shift-x').toFixed(0)},${num('--art-shift-y').toFixed(0)}`,
    `peek   ${num('--peek-shift-x').toFixed(0)},${num('--peek-shift-y').toFixed(0)}  ${currentPeekAngleDeg}°`,
    // `comp` 那一行已删除 —— 旋转补偿整套去掉了（见 applyPeekCompensation 的删除说明）
    `旋转中心 ${num('--art-origin-x').toFixed(0)},${num('--art-origin-y').toFixed(0)}`,
    /*
     * 立绘位置：**窗口内偏移**（上报给主进程的锚点）与屏幕坐标各一行。
     *
     * 为什么要分开显示：主进程换窗口尺寸时靠"立绘在窗口内的偏移"保住立绘不动
     * （`新原点 = 当前原点 − 偏移`）。2026-10-04 的"探头结束闪现"就是这里报错了一版
     * —— 混进了屏幕坐标，偏移报成 212（真实约 23），窗口被搬走 212px。
     * 两行并排，一眼就能看出偏移是否离谱（正常量级 0~45 / -35~20）。
     */
    `偏移    ${offsetInWindow.x},${offsetInWindow.y}`,
    `立绘屏幕 ${Math.round(window.screenX + offsetInWindow.x)},` +
      `${Math.round(window.screenY + offsetInWindow.y)}`,
    /*
     * 碰撞箱（**命中判定的框**）—— 排查"点到立绘没反应 / 点到旁边却算命中"时看这行。
     *
     * 它由 `reportArtRect` 算出并上报，主进程用它决定"鼠标在不在宠物身上"
     * （见 `petWindow.startCursorWatch`）。这里显示的是**屏幕坐标**，
     * 与 `立绘屏幕` 那行对照即可看出是否对齐 —— 不旋转时两者应当基本重合。
     */
    `碰撞箱  ${Math.round(window.screenX + hitBox.x)},${Math.round(window.screenY + hitBox.y)}` +
      `  ${Math.round(hitBox.width)}x${Math.round(hitBox.height)}`,
  ];
  el.textContent = lines.join('\n');
}

/**
 * 画布染色（调试用）。
 *
 * ⚠️ 试过"往画布上刷一层半透明蓝"（`source-atop` 合成），但那要**重绘画布**，
 * 会和 sprite 自己的绘制循环打架（谁后画谁赢，立绘会被刷掉）。
 *
 * 改成**给画布元素加半透明背景色**：画布的透明像素会被染上颜色，
 * 而**立绘本身保持原色** —— 一眼就能看出"画布边界在哪、立绘占哪一块"，
 * 正是排查需要的。纯 CSS，零副作用。
 */
function setDebugTint(on) {
  document.body.classList.toggle('debug-hud', Boolean(on));
}

pet.on('pet:peek', (payload) => {
  const peeking = Boolean(payload && payload.peeking);
  const angle = peeking ? Number(payload && payload.angle) || 0 : 0;
  // 记下角度供 `reportArtRect` 旋转立绘中心（见 currentPeekAngleDeg 的说明）
  currentPeekAngleDeg = angle;
  // 位移量由主进程给（= 窗口里那圈留白的宽度）。
  // 探头时把立绘往屏幕边缘推这么远，让留白"让开" —— 立绘本身**不缩放**，
  // 全程比例不变（用户要求：「整个动画时宠物全身比例不变，不追求变大效果」）。
// 位移分横纵两个方向：立绘不是正方形，两个方向的留白不同（155x177）。
  // 贴左右边时用 offsetX、贴上下边时用 offsetY。
  const offsetX = peeking ? Number(payload && payload.offsetX) || 0 : 0;
  const offsetY = peeking ? Number(payload && payload.offsetY) || 0 : 0;
  // 先记状态再改样式：measureArtBox 会读这个标志决定要不要测
  // （探头时 canvas 是斜的，量出来的包围盒不对 —— 见 peekingNow 的说明）
  peekingNow = peeking;
  document.body.classList.toggle('peek', peeking);
  /*
   * ⚠️ 这里**不再**调 `updateCanvasOffset`（2026-10-04 回退）。
   *
   * 我一度在 peek 消息到达后延迟重算"画布横向居中量"。但画布横向位置现在是常量 0
   * （见 `styles.css` 的说明），探头不再改变它，**这两行 setTimeout 只会白跑**，
   * 而且如果将来有人把画布位置做回变量，这两行会成为"时机不确定的隐式写入"
   * （0ms 与 400ms 两次，和窗口 setBounds 的时序交织）。
   */
  // 旋转还是写内联 transform（CSS 那里只负责给 transition）。
  // ⚠️ 别忘了 `measureArtBox` 也会写 canvas 的样式 —— 它必须先看 peekingNow，
  // 否则会把这里设的 rotate 覆盖掉（实测：探头时 transform 变成 none，立绘根本不转）。
  /*
   * ⚠️ **旋转写在这一层（`.pet-wrap`），不是 `#pet` 上**。
   *
   * 这是排查十几轮后的结构性修正。原来旋转加在画布上，导致**画布自己的外接矩形
   * 随角度变大**（240x240 -> 339x339），而本项目所有定位逻辑都假设"画布没被
   * transform 动过" —— 于是全偏，只能层层加补偿，而每加一层就多一处对不上。
   *
   * 用户诊断得很准：「画布一变大，它就闪现向变大后的画布中央」。
   *
   * 现在：`#pet` 永远不被旋转 -> `getBoundingClientRect()` 恒为 240x240 ->
   * `measureArtBox` / `reportArtRect` / 锚点 / 碰撞箱全部干净。
   *
   * ⚠️ 不要再往 `els.canvas.style.transform` 写东西（别问，问就是踩过）。
   */
  els.canvasWrap?.style.setProperty('--pet-rotate', `${angle}deg`);
  // 画布自身的 transform 保持清空（它是"永不旋转"的那一层）
  els.canvas.style.transform = '';
  /*
   * ⚠️ **旋转动画真正结束时通知主进程** —— 这才是"换尺寸"该等的时刻。
   *
   * ## 为什么必须由渲染层通知，不能靠主进程猜延时
   *
   * 实测（日志原文）：旋转动画期间，立绘在窗口内的**偏移会一路漂移**：
   *
   *     art-rect 上报：偏移 {"x":39,"y":15}    ← 静止（真实稳态）
   *     art-rect 上报：偏移 {"x":57,"y":39}
   *     art-rect 上报：偏移 {"x":71,"y":45}
   *     art-rect 上报：偏移 {"x":106,"y":59}   ← 动画中途
   *
   * 主进程换尺寸靠"新原点 = 当前原点 − 偏移"，取到中途值（实测用到 `88,58`，
   * 稳态是 `39,15`）就把立绘挪走 49px —— 用户看到的就是「探头结束闪现」。
   *
   * 之前靠 `PEEK_TRANSITION_MS_MIRROR`（1000ms）+ 余量去**猜**，猜不准
   * （动画 1s，但拖拽/排队会让实际结束时刻漂）。`transitionend` 是**精确**的。
   */
  if (angle === 0 && els.canvasWrap) {
    const wrap = els.canvasWrap;
    const onDone = (event) => {
      if (event.target !== wrap || event.propertyName !== 'transform') return;
      wrap.removeEventListener('transitionend', onDone);
      peekTransitionListener = null;
      // 再等一帧：让按新姿态重算的 `--art-shift` / `--art-origin-*` 也落定
      requestAnimationFrame(() => {
        reportArtRect(); // 立刻补报一次，主进程就能拿到**姿态已稳定**的锚点
        pet.peekTransitionEnd?.({ angle });
      });
    };
    /*
     * ⚠️ 先摘掉上一次的监听再挂 —— `pet:peek` 会被调用多次，
     * 不摘的话回调会叠加（同一个 transitionend 触发 N 次）。
     */
    if (peekTransitionListener) wrap.removeEventListener('transitionend', peekTransitionListener);
    peekTransitionListener = onDone;
    wrap.addEventListener('transitionend', onDone);
  }
  if (els.canvasWrap) {
    // 位移走 CSS 变量，由样式表叠到"居中平移"之上 ——
    // 不在这里直接写 transform，否则会把居中量覆盖掉（两处都写会互相打架）。
    // 横纵各一个变量：贴左右边用 X、贴上下边用 Y。
    els.canvasWrap.style.setProperty('--peek-shift-x', `${offsetX}px`);
    els.canvasWrap.style.setProperty('--peek-shift-y', `${offsetY}px`);
  }
  /*
   * ⚠️ 这里原来调 `applyPeekCompensation(angle)`（旋转位移补偿）—— **已删除**。
   *
   * 补偿是为"探头时立绘往右下甩"加的。真正原因是两处几何错误：
   *   · `petLayout.ts` 的 `rawY` 把"立绘屏幕位置"当成了窗口原点（漏掉立绘偏移）；
   *   · `styles.css` 的 `transform-origin` 用了窗口中心（比立绘中心低 27px）⇒ 画弧。
   * 根因修掉后补偿恒为 0，用户要求删除（「把这个删除了吧，现在不用这个了」）。
   *
   * 旋转中心现在由 `--art-origin-x/y` 负责（见 `measureArtBox`）。
   */
  // 转告主进程：探头时宠物贴在屏幕外，对话条要换到**对面或上方**才不出屏
  pet.reportPeekState(peeking);
  // 气泡也要跟着避开屏幕外那一边（用户要求"探头时文字不要超出屏幕"）。
  // 平移量由主进程算（它知道窗口在屏幕上的位置），这里只套用。
  setBubbleShift(peeking ? Number(payload && payload.bubbleShift) || 0 : 0);
  // 探头会改位移与旋转 -> 立绘矩形变了，主进程的命中判定要跟着更新。
  //
  // ⚠️ 这里必须**立刻**重报，不能只 `scheduleArtRectReport()`（它有 80ms 去抖）。
  //
  // 踩过（用户报「拖拽范围偏离宠物中心，向右偏了很多」）：
  // `reportArtRect` 在探头时把碰撞箱按 √2 放大（旋转 45° 的外接矩形），
  // 而**退出探头**时旧的放大矩形还留在主进程那边 —— 期间点宠物，
  // 命中判定用的还是那个偏大的框，多出来的部分偏向一侧，手感就是"抓不准"。
  // 立刻重报一次，退出探头那一帧碰撞箱就恢复成常态尺寸。
  reportArtRect();
  // 旋转/位移的 CSS 过渡（500ms）走完后再报一次：过渡期间 `--art-shift-*`
  // 可能被 `measureArtBox` 重算，立绘在窗口内的位置会跟着变，
  // 碰撞箱也要跟上（立即报的那次只是为了"不留旧框"）。
  setTimeout(reportArtRect, 620);
});

/**
 * 设置里改了"文字弹窗存在时间"。
 *
 * 用户要求：「设置中可以设置文字弹窗的存在时间，默认 10 秒后消失」。
 * 改完**立刻生效**，并且对正在显示的气泡重新计时 ——
 * 否则要等下一次回应才生效，用户会以为没改成功。
 */
pet.on('pet:set-bubble-linger', (payload) => {
  /*
   * ⚠️ 这里原来操作 `bubbleLingerMs` / `bubbleShown` / `scheduleBubbleHide`
   * —— **三个标识符在渲染层都不存在**（2026-10-04 从日志抓到
   * `Error: bubbleLingerMs is not defined`）。那段气泡逻辑是
   * **"气泡已移到独立窗口 `bubble.html`"之后的遗留物**，删气泡时漏掉了这里。
   *
   * 现在正确处理：**什么都不用做**。气泡的显示时长由 `bubble.html` 那边自己管
   * （主进程把 `bubble:set-linger` 发给气泡窗口），宠物窗口里没有气泡可计时。
   *
   * 保留这个监听是为了：① 记录日志便于排查；② 将来若把气泡搬回宠物窗口，
   * 这里有现成的挂点。
   */
  const ms = Number(payload && payload.ms);
  if (Number.isFinite(ms) && ms >= 1000) {
    console.info(`[pet] 文字弹窗时长改为 ${ms}ms（由气泡窗口自行应用）`);
  }
});

/**
 * 探头时气泡的平移量（主进程按**实测气泡矩形**算好）。
 *
 * 走独立通道而不是 `pet:peek`：后者是姿态消息，复用会把旋转覆盖掉。
 */
pet.on('pet:bubble-shift', (payload) => {
  setBubbleShift(Number(payload && payload.shift) || 0);
});

// 指针离开窗口：立刻恢复穿透，避免"窗口挡住了桌面却收不到事件"的僵死状态
window.addEventListener('mouseout', (event) => {
  if (!event.relatedTarget) reportInteractive(false);
});

// 拖拽宠物 = 移动窗口。
//
// **不用 CSS 的 `-webkit-app-region: drag`。** 那个方案依赖 Chromium 的拖拽区
// 命中测试，而窗口在穿透状态下带 `WS_EX_TRANSPARENT`，命中测试会被跳过 ——
// 表现就是"抓不住宠物、窗口不动"。改成显式移动：按下记住起点，移动时把
// 屏幕坐标发给主进程，由它 setPosition。
let dragStart = null;

els.canvas.addEventListener('mousedown', (event) => {
  if (event.button !== 0) return;
  dragStart = { x: event.screenX, y: event.screenY };
  draggingWindow = true;
  reportInteractive(true);
  pet.dragStart(event.screenX, event.screenY);
  sprite.play('drag', true);
  event.preventDefault();
});

// 拖拽中的移动：挂在 window 上而不是 canvas 上 —— 指针很容易滑出宠物本体，
// 挂在 canvas 上会在滑出的瞬间丢掉后续事件，拖拽中途卡住。
window.addEventListener('mousemove', (event) => {
  if (!dragStart) return;
  pet.dragMove(event.screenX, event.screenY);
});

/**
 * 单击（而非拖动）宠物 = 戳它一下，让它主动说一句。
 *
 * 为什么用位移阈值来区分：canvas 同时是拖动窗口的把手，如果按下即触发，
 * 每次挪动窗口都会顺带让它说一句话，非常烦人。
 */
window.addEventListener('mouseup', (event) => {
  const wasDragging = dragStart !== null;
  if (wasDragging) {
    pet.dragEnd();
    if (sprite.currentState === 'drag') sprite.play('idle');
  }
  if (!dragStart) {
    // 拖拽已结束但要清掉守卫，让穿透恢复正常
    if (wasDragging) {
      draggingWindow = false;
      // 按指针当前位置重新判定，而不是无条件恢复穿透 ——
      // 松手时指针可能仍停在宠物或输入栏上，那时应当保持可交互。
      reportInteractive(isInteractivePoint(event.clientX, event.clientY));
    }
    return;
  }
  const moved = Math.hypot(event.screenX - dragStart.x, event.screenY - dragStart.y);
  dragStart = null;
  draggingWindow = false;
  reportInteractive(isInteractivePoint(event.clientX, event.clientY));
  if (moved > 6) return;
  if (!els.canvas.matches(':hover')) return;
  /*
   * 点击宠物本体 = 摸它一下。
   *
   * ## ⚠️ 现在**只做互动，不让它说话**（2026-10-07 用户要求）
   *
   * 用户原话：「现在主动点击**不要发送空内容、只是点击而已**」
   *
   * 原来这里是 `pet.poke()` —— 主进程发 `user.poke`，后端据此让宠物立刻开口。
   * 那条路的问题：**每点一下 = 一次真实的 LLM 调用**，而点击并不携带任何
   * 用户输入，于是 `user_text` 是 `None`（自主发言），接口以
   * `content should be a string or a list` 拒掉 —— 用户看到的是一条报错气泡。
   *
   * 现在：
   *   · **播一个本地反应动画**（`happy`）—— 反应立刻可见，不等网络、不花钱；
   *   · 仍然发 `pet.poke()`（后端只记一次"互动"，不再生成内容）。
   *
   * ## 为什么自己播动画而不是等后端
   *
   * 后端现在**不产生任何内容**了，所以反馈必须来自本地 —— 否则点击会毫无反应。
   * `happy` 是素材里已有的状态（`sprite.js` 的 `STATE_ALIASES` 里
   * `shy`/`angry` 也都映射到它），不用新增资源。
   */
  pet.poke();
  if (sprite.currentState !== 'drag') {
    sprite.play('happy');
    setTimeout(() => {
      // 期间如果被别的状态接管（说话、拖拽、睡觉），不要抢回来
      if (sprite.currentState === 'happy') sprite.play('idle');
    }, POKE_REACTION_MS);
  }
  /*
   * 诊断标记：把"点击被识别为摸一下"这件事记在 window 上。
   *
   * ⚠️ 为什么需要它：这条处理链上有若干早退分支（`dragStart` 为空、
   * 位移超过阈值、指针不在 canvas 上），出问题时**看不出是哪一个挡的** ——
   * 表现都一样："点了没反应"。
   *
   * 排查手段（`webContents.executeJavaScript`）：
   *
   *     window.__lastPoke            // { at, moved, sprite }
   *
   * 有值 = 走到了这里；没值 = 在前面就被挡了，去查那三道守卫。
   */
  window.__lastPoke = { at: Date.now(), moved, sprite: sprite.currentState };
});

// 指针在窗口外松开时，mouseup 不会送到这里 —— 补一道兜底，
// 否则拖拽状态会一直挂着，之后动一下鼠标窗口就跟着跑。
window.addEventListener('blur', () => {
  if (!dragStart) return;
  dragStart = null;
  draggingWindow = false;
  pet.dragEnd();
  if (sprite.currentState === 'drag') sprite.play('idle');
});

// 初始化状态（只同步宠物自己的表现 + 转告主进程更新对话条的小圆点）
void pet.getState().then((state) => {
  document.body.classList.toggle('paused', Boolean(state.paused));
  setStatus(state.status ?? '在线', 'info');
});

/*
 * ⚠️ **画布尺寸兜底**（见上面 `startupSyncCanvasFallback` 的说明）。
 *
 * 只在主进程漏发 `pet:set-pet-size`（画布仍是临时默认值 `PET_SIZE`）时才对
 * 齐一次。正常路径下它什么都不做 —— 因为"按 `window.innerWidth` 对齐"取到的是
 * 含非客户区的值（实测 252，而正确值是 250），主动去对齐反而会**制造**一次跳动。
 */
startupSyncCanvasFallback();

/*
 * ⚠️ **预热麦克风**（2026-10-08，见 `warmUpVoice()` 的说明）。
 *
 * 放在初始化最后、且**不 await** —— 它要几百毫秒，不该拖住界面绘制。
 *
 * 为什么要在开机就做：`getUserMedia` 是异步的（100~300ms），而"按住说话"
 * 按下那一刻就同步发了 `voice.ptt start`。不预热的话，按快一点就是
 * "闸门开了但一个音频帧都没有" —— 用户看到的是"按了没用"。
 *
 * ⚠️ 这里的调用**不在用户手势里**。能通过是因为权限**已经授权过**
 * （Chromium 对已授权设备不强制手势）；首次安装还没授权时它会失败，
 * 那条路径由 `setVoice()` 在用户点击开关时兜住（那里有手势）。
 */
void warmUpVoice();
