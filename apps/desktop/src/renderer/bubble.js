/**
 * 气泡窗口的渲染层：只负责显示宠物说的话。
 *
 * 三条用户要求的落点：
 *   ① 「宠物的回应文字或颜文字表情等显示在宠物的上方」
 *      —— 位置由主进程摆（窗口底边贴立绘头顶），这里不管定位；
 *   ② 「当宠物处在探头状态时，文字不要超出屏幕」
 *      —— 同样由主进程钳进工作区；
 *   ③ 「设置中可以设置文字弹窗的存在时间，默认 10 秒后消失，
 *      当 10 秒内出现下次回应时，上一个弹窗做动画过渡消失，随后新文字弹窗出现」
 *      —— 在这里实现（存在时间 + 新旧过渡）。
 *
 * 与宠物窗口里那份的最大区别：**宽度不受宠物窗口约束**，
 * 所以长回复能横向铺开，不会长成压住宠物脸的窄柱子。
 */

const bubble = document.getElementById('bubble');
const bubbleText = document.getElementById('bubble-text');
const typing = document.getElementById('typing');

/** 存在时间（毫秒），默认 10 秒；由主进程通过 bubble:message 的 lingerMs 下发 */
let lingerMs = 60_000;
/*
 * ⚠️ **存在时长的定时器不在这里了** —— 已搬到主进程（2026-10-04）。
 *
 * 用户要求：「隐藏的时间不计入气泡持续时间内」。渲染层收到消息时窗口可能还没画出来，
 * 从这里起算会把加载时间算进去。现在渲染层只负责用 `reportShown()` 报告
 * "真的显示出来了"，计时由主进程的 `BubbleWindow.scheduleHide()` 负责。
 *
 * `lingerMs` 保留：它仍从 `bubble:message` 下发（改设置后立刻生效），
 * 只是渲染层不再用它计时 —— 主进程自己读的是同一个设置值。
 */
/** 退场动画的定时器 */
let leaveTimer = null;
/** 是否正在显示（含正在退场的） */
let shown = false;
/** 当前发言 id，用于串台保护（迟到的增量丢弃） */
let currentId = null;

/** 退场动画时长，必须与 bubble.css 里 `.bubble.leaving` 的 transition 一致 */
const LEAVE_MS = 220;

/**
 * 上一次量到的自然宽度（见 `naturalBubbleWidth` 的单调保护说明）
 */
let lastNaturalWidth = 0;
/** 上一次上报时的文字内容（内容变了才重新量，避免无意义的布局） */
let lastMeasuredText = null;

/**
 * 直接量 `.bubble` 自己的自然宽度。
 *
 * ## 为什么放弃"专用测量元素"（我在这上面栽了两轮）
 *
 * 第一版想省 CPU：用一个隐藏的 `#bubble-measure` 元素量宽度，避免让真实气泡重排。
 * **结果它量出的值不稳定** —— 实测同一段文字在不同时刻量到 `35` / `1003` / `520`，
 * 而 `35` 会让主进程把窗口收窄到最窄，于是文字竖排、高度炸到几千像素。
 * 排查这个不稳定花的时间远超它省下的那点 CPU。
 *
 * ## 现在的做法：临时摘掉 `max-width`，量真实气泡
 *
 * ```js
 * bubble.style.maxWidth = 'none'   // 解除"窗口宽 - 8"的钳制
 * bubble.style.width    = 'max-content'
 * const w = bubble.offsetWidth     // 读它 -> 强制同步重排，拿到真实需求宽度
 * // 立刻还原
 * ```
 *
 * 代价是**一次全量文本布局**。但实测代价可接受（400 字 ≈ 0.5ms），
 * 而且下面有"内容没变就不重量"的短路，正常流式每几十毫秒才量一次。
 *
 * 换来的是**确定性**：量的就是真实渲染的那个元素，没有"另一个元素布局是否同步"的疑问。
 */
function naturalBubbleWidth() {
  if (!bubble || !bubbleText) return 0;
  const text = bubbleText.textContent || '';
  // ⚠️ 必须在覆盖 `lastMeasuredText` **之前**记下上一次的长度 ——
  // 先覆盖再比较的话，"内容变短"永远为假，单调保护就只在涨、永不回落。
  const prevLen = lastMeasuredText === null ? 0 : lastMeasuredText.length;
  if (text === lastMeasuredText) return lastNaturalWidth; // 内容没变 -> 沿用上次
  if (!text) {
    lastMeasuredText = '';
    lastNaturalWidth = 0;
    return 0;
  }
  const savedMax = bubble.style.maxWidth;
  const savedWidth = bubble.style.width;
  bubble.style.maxWidth = 'none';
  bubble.style.width = 'max-content';
  // `offsetWidth` 读的是布局值，会强制同步重排 —— 所以必须立刻还原，别把样式留着
  const natural = Math.ceil(bubble.offsetWidth);
  bubble.style.maxWidth = savedMax;
  bubble.style.width = savedWidth;
  /*
   * ## 单调保护：文字变长时宽度只涨不落
   *
   * 同一个内容理论上每次都量出同样的值，但实测偶发量到偏小值（布局中间态）。
   * 偏小的后果极重（窗口收窄 → 文字竖排 → 高度几千像素），而偏大的代价只是右边留白。
   * 所以同一条回复内只允许上涨；**新一轮发言（文字变短）才允许回落**。
   */
  const shrank = text.length < prevLen;
  lastNaturalWidth = shrank ? natural : Math.max(natural, lastNaturalWidth);
  lastMeasuredText = text;
  return lastNaturalWidth;
}

/**
 * 窗口宽度上限（CSS 像素）。
 *
 * 与主进程 `bubbleWindow.ts` 的 `BUBBLE_MAX_WIDTH` **必须一致** —— 但这里刻意
 * 重复一份而不是从主进程拿：渲染层要能**独立**算出目标宽度，不依赖任何往返。
 * 不一致的后果只是窗口比需要的宽或窄一点（主进程还会再钳一次），不会坏。
 */
const MAX_WINDOW_WIDTH = 520;

/**
 * 算出**窗口该有多宽**。
 *
 * ## ⚠️ 为什么要在渲染层算（放过主进程算，踩了两轮）
 *
 * 原来主进程用 `naturalWidth` 决定窗口宽，而 `naturalWidth` 是渲染层量的 ——
 * 看着合理，实际是个**闭环**：
 *
 *     窗口窄 -> `.bubble` 的 `max-width` 小 -> 内容被强制换行 -> 量出来的"自然宽度"也小
 *
 * 实测这个环会收敛到最窄：窗口锁死在 160，394 字排成 2879px 高的一列
 * （用户报的「高度顶天了都，宽度才几个字」）。
 *
 * 闭环难修的原因是**测量本身受被测量对象影响**。所以改成单向：
 * 渲染层直接给出"最终要多大"，主进程**照用**。
 *
 * 取 `max(测量宽度, 当前渲染宽度)`：内容要的宽度不可能小于它当前已经占用的宽度
 * （文字已经排进去了）。这让上报值**只会高估不会低估** ——
 * 高估的代价只是窗口右边留白，低估的代价是文字竖排。
 */
function desiredWindowWidth(currentW) {
  const natural = naturalBubbleWidth();
  const base = Number.isFinite(natural) && natural > 0 ? natural : 0;
  // 加上内边距（`.bubble` 的 padding 8px 13px + 边框），再留一点余量
  const padded = Math.max(base, currentW) + 30;
  /*
   * 🔴 宽度上限按字数分档（2026-10-17 用户要求）：
   *   · >100 字 —— 维持原上限（MAX_WINDOW_WIDTH，520）
   *   · ≤100 字 —— 上限**减半**（260）
   * 否则六七十字的一句话会摊成 500 多像素的长带子，头顶飘着很突兀；
   * 砍半之后短回复自动折成两三行，形状更像聊天气泡。
   */
  const text = bubbleText ? bubbleText.textContent : '';
  // ≤100 字上限 300px（2026-10-17 用户定的数值）；>100 字维持 520
  const cap = text.length > 100 ? MAX_WINDOW_WIDTH : 300;
  return Math.max(160, Math.min(cap, Math.ceil(padded)));
}

function reportBubbleWidth() {
  if (!bubble || !window.bubble || typeof window.bubble.reportSize !== 'function') return;
  const w = Math.ceil(bubble.offsetWidth);
  /*
   * ## 高度上报：`scrollHeight` 要**扣掉上下内边距**（2026-10-05 实测）
   *
   * 与宽度那个闭环同源：`.bubble` 是 `bottom: 0` + `overflow-y: auto`，
   * 所以它**比视口（窗口内容区）高的时候会被视口钳住** ——
   * 而窗口高度又由这里上报的高度决定，于是会卡在一个"差几像素"的状态。
   *
   * ## ⚠️ 但不能直接用 `scrollHeight`
   *
   * 我第一版写的是 `max(offsetHeight, scrollHeight)`，结果**永远多报 8px**：
   * `scrollHeight` 的口径是"padding box 的内容高度 + 内边距"，
   * 对一个带 `padding: 8px 13px` 的元素它会比 `offsetHeight` 大 16px
   * （实测 326 vs 320，差 6、正好是"减去一半内边距"的边界值）。
   *
   * 后果不是切字，而是**假溢出**：气泡永远能滚 8px，而滚到底什么新内容都没有
   * （实测 `scrollTop 8 / maxScroll 8`，`lastCharVisible = true`）——
   * 但滚动条会一直显着，看着像"下面还有字没显示出来"。
   *
   * 扣掉上下内边距之后口径就一致了：内容真放不下时才大于 `offsetHeight`。
   */
  const padY =
    (Number.parseFloat(getComputedStyle(bubble).paddingTop) || 0) +
    (Number.parseFloat(getComputedStyle(bubble).paddingBottom) || 0);
  const h = Math.max(Math.ceil(bubble.offsetHeight), Math.ceil(bubble.scrollHeight - padY));
  if (!(w > 0)) return;
  const wantWindow = desiredWindowWidth(w);
  /*
   * 上报三个值，用途各不相同：
   *   · `width`         —— 气泡当前渲染宽度（可能被窗口压着）
   *   · `height`        —— 气泡**需要**的高度（含被视口钳住的那部分）
   *   · `windowWidth`   —— **窗口该多宽**（渲染层算好的，主进程照用）
   *
   * 主进程**不再自己推导宽度** —— 推导就会回到那个闭环里。
   */
  window.bubble.reportSize({ width: w, height: h, windowWidth: wantWindow });
}

let sizeTimer = null;
function scheduleSizeReport() {
  if (sizeTimer) clearTimeout(sizeTimer);
  sizeTimer = setTimeout(() => {
    sizeTimer = null;
    reportBubbleWidth();
  }, 60);
}

/**
 * 当前气泡里**有没有可看的东西**（文字，或"正在输入"的指示）。
 *
 * 判断"该不该显示"要用它。理由见 `beginSay` 的说明。
 */
function hasContent() {
  const text = bubbleText && bubbleText.textContent ? bubbleText.textContent.trim() : '';
  const typingVisible = Boolean(typing && !typing.classList.contains('hidden'));
  return Boolean(text) || typingVisible;
}

/**
 * 文字为空时给气泡本体加个标记，让 CSS 把它藏掉。
 *
 * ## 为什么要它（2026-10-07）
 *
 * `.bubble` 有 `padding: 8px 13px` + 背景 + 边框，宽度是 `max-content`。
 * 文字为空时它仍然会画成一个 **约 33x17 的深色小盒子** ——
 * 实测探针读到过 `DIV#bubble 33x17 bg=rgb(30, 28, 41)`。
 *
 * 那个小盒子在宠物头顶/旁边就是用户截图里那个"**黑色小胶囊**"。
 * `show()` 已经在"没内容"时不显示气泡了，但那挡不住**已经在显示中**
 * 而被清空的那一刻（`say.start` 会先清空文字）—— 那一帧它就露出来。
 *
 * 用一个类而不是 `:empty` 伪类：`#bubble` 里除了文字还有一个"正在输入"
 * 的 `<span>`，所以它**永远不是 `:empty`**，伪类判不出来。
 *
 * ## ⚠️ 判据必须和 `hasContent()` **完全一致**（2026-10-07 修）
 *
 * 原来这里只看"文字为空"，而 `hasContent()` 是"有文字**或**有指示器"。
 * 两者不一致就会出现自相矛盾的状态：`show()` 认为有内容（给了 `visible`），
 * 而这个函数同时给了 `no-text`（`visibility: hidden`）——
 * 结果"指示器亮着，气泡却是隐的"。
 *
 * 实测（点宠物后 500ms 采样，那一瞬正在等第一个 delta）：
 *
 *     t+0.5s  {"可见":false, 类:"bubble streaming no-text visible", 字数:0}
 *
 * 所以**统一走 `hasContent()`**：有指示器就算有内容，不加 `no-text`。
 */
function syncEmptyMark() {
  if (!bubble) return;
  bubble.classList.toggle('no-text', !hasContent());
}

function show() {
  if (!bubble) return;
  /*
   * ## ⚠️ 没有可看的东西时不显示（2026-10-07）
   *
   * 实测（点宠物触发新一轮回复时抓到的消息序列）：
   *
   *     thinking   text.len=0
   *     say.start  text.len=0
   *     say.end    text.len=0      <- **整个回复是空的**
   *
   * 而 `beginSay` 会清空文字并隐藏"正在输入"指示 —— 那一瞬气泡里什么都没有。
   * 如果这时把它显示出来，用户看到的就是"上一条消失了 / 气泡不见了"。
   *
   * 判据：有文字**或**有"正在输入"指示才显示。空回复由此天然不显示。
   */
  if (!hasContent()) {
    // 已经显示着就保持（别把上一秒还在的气泡闪掉）；没显示就不主动显示
    if (!shown) return;
  }
  if (leaveTimer) {
    clearTimeout(leaveTimer);
    leaveTimer = null;
  }
  bubble.classList.remove('hidden', 'leaving');
  bubble.classList.add('visible');
  shown = true;
  // 内容变了 -> 尺寸可能变了 -> 报给主进程调整窗口
  scheduleSizeReport();
}

/**
 * 退场（带过渡），动画结束后执行 `after`。
 *
 * 用户要求的"上一个弹窗做动画过渡消失，随后新文字弹窗出现"就靠它：
 * 退场**先完成**再显示新的，而不是直接换文字。
 */
function hideAnimated(after) {
  if (!bubble || !shown) {
    if (after) after();
    return;
  }
  if (leaveTimer) clearTimeout(leaveTimer);
  bubble.classList.remove('visible');
  bubble.classList.add('leaving');
  shown = false;
  leaveTimer = setTimeout(() => {
    leaveTimer = null;
    if (!shown) bubble.classList.add('hidden');
    if (after) after();
  }, LEAVE_MS);
}

/**
 * 通知主进程"气泡**真的画出来了**"-> 从这一刻开始计存在时长。
 *
 * ## ⚠️ 计时为什么搬到了主进程（2026-10-04）
 *
 * 用户要求：「让气泡正常该显示时才显示气泡，当然**隐藏的时间不计入气泡持续时间内**」。
 *
 * 原来计时就在这里（`scheduleHide` 用 `setTimeout`）。问题是它的起点是
 * **收到 `bubble:message`**，而那一刻窗口可能还没画出来（首次显示要等首帧，
 * 实测约 1.5s 的加载宽限）—— 那段时间被算进了"显示时长"，气泡实际只显示 8.5 秒。
 *
 * 现在主进程在收到这条消息后才起计时器，见 `index.ts` 的 `bubble:shown` 处理。
 * 渲染层这边只负责"什么时候算真的显示出来"。
 */
function reportShown() {
  // ⚠️ API 挂在 `window.bubble` 上（见 `src/preload/bubble.ts` 的 exposeInMainWorld）
  const api = window.bubble;
  if (!api || typeof api.reportShown !== 'function') return;
  api.reportShown();
}

function beginSay(payload) {
  currentId = payload && payload.id ? payload.id : null;
  if (typeof payload?.lingerMs === 'number' && payload.lingerMs > 0) lingerMs = payload.lingerMs;
  /*
   * ⚠️ 文字必须**立刻**清空，不能等退场动画结束再清。
   *
   * 踩过：把清空放在退场回调里，于是淡出的 220ms 内旧文字还在，
   * 而新的第一个 delta 已经追加进来 —— 两条回复被**拼在一起**。
   */
  if (bubbleText) bubbleText.textContent = '';
  bubbleRaw = '';
  if (bubble) bubble.classList.add('streaming');
  /*
   * ## ⚠️ 清空文字的同时**要把"正在输入"亮起来**（2026-10-07 第二次修）
   *
   * 原来是 `if (typing) typing.classList.add('hidden')` —— 把指示器也关了。
   * 于是 `say.start` 到第一个 `delta` 之间，气泡里**什么都没有**：
   * 元素进 `no-text`（隐藏）状态，用户看到的就是"点一下宠物，气泡没了"。
   *
   * 实测（点宠物触发新一轮回复，每 500ms 采样一次）：
   *
   *     t+0.5s  {"可见":false, 类:"bubble visible streaming no-text", 字数:0}   <- 消失
   *     t+1.5s  {"可见":true,  字数:43}                                        <- 才回来
   *
   * 那 1 秒就是用户报的"点击宠物会导致气泡消失"。
   *
   * 修法：亮起"正在输入"三点。它本来就是为这个场景存在的
   * （`showThinking()` 会做的事），只是被这里无条件关掉了。
   * 于是气泡**全程可见**：`···` -> 文字 -> 保持 -> 计时器到点关闭。
   *
   * ⚠️ 空回复（interrupted/error）也别关指示器 —— 那时它会一直显示三个点，
   * 比"气泡突然消失"更容易理解（"它想说什么但没说出来"）。
   * `endSay` 里才把指示器收掉。
   */
  if (typing) typing.classList.remove('hidden');
  /*
   * ⚠️ 顺序：**先点亮指示器，再算空标记**（2026-10-07 修）
   *
   * `syncEmptyMark()` 的判据是"文字为空" —— 它不看指示器。
   * 所以如果先调它，气泡会被打上 `no-text`（隐藏），而 `show()` 的
   * 判据 `hasContent()` 又**要看指示器** —— 两者不一致就会出现
   * "指示器明明亮着，气泡却是隐藏的"。
   *
   * 实测症状（点宠物后 500ms 采样）：
   *
   *     t+0.5s  {"可见":false, 类:"bubble streaming no-text visible", 字数:0}
   *                                                                  ^^^^^^^
   *                        hasContent() 说是"有内容"所以给了 visible，
   *                        但 no-text 的 visibility:hidden 把它盖住了
   *
   * 修法：让两者口径一致 —— 下面 `syncEmptyMark()` 会把"有指示器"也算成有内容。
   */
  syncEmptyMark();

  /*
   * ## ⚠️ "有内容才退场"（2026-10-07 修）
   *
   * 原来是 `if (shown) hideAnimated(show); else show();` —— 无条件重新显示。
   * 而这一瞬文字刚被清空、"正在输入"也刚被藏起，于是：
   *
   *   · 气泡里**什么都没有**，却进入了显示流程；
   *   · 如果新回复本身是**空的**（实测：`say.start`/`say.end` 的 text 长度都是 0），
   *     那它永远不会被填上内容 —— 气泡就一直是空的 / 或者被 `endSay` 永久藏掉。
   *
   * 用户报的「点击气泡或者宠物气泡就消失了」就是这个：
   * **点宠物触发新一轮回复，而那一轮若为空，上一条气泡就此消失。**
   *
   * 改成：只在**已经有内容**时才走"退场 -> 显示"（那时内容来自紧随其后的 delta，
   * 或者已经在的"正在输入"指示）。没有内容就**什么都不做** ——
   * 让上一条气泡留在屏幕上，而不是先把它退场、再等一个可能永远不来的内容。
   */
  if (hasContent()) {
    if (shown) hideAnimated(show);
    else show();
    reportShown();
  }
}

/*
 * 段落化渲染（2026-10-17 定稿）：用户要「段落之间留一点点间距」——
 * 纯文本 + pre-line 做不到半行间距（\n\n 就是一整行空白，\n 就是零），
 * 所以改成**真正的段落元素**：
 *
 *   · 原始全文存在 `bubbleRaw`（分段换行原样保留，不做任何折叠）；
 *   · 渲染时按 `\n{2,}` 切成段落，每段一个 `.para`；
 *   · 段内单个 `\n` 交给容器的 pre-line 处理（原样换行）；
 *   · 段与段的"一点点间距"由 CSS `.para + .para { margin-top }` 控制，
 *     想调大小改那一处 CSS 就行。
 *
 * ⚠️ 每次追加都对**整段**重排（文本量最多几 KB，代价可忽略）：
 * 流式的 \n 可能跨块到达，拼完再切才不会漏。
 */
let bubbleRaw = '';

function renderParagraphs() {
  if (!bubbleText) return;
  const paras = bubbleRaw
    .split(/\n{2,}/)
    .map((s) => s.replace(/[ \t]+\n/g, '\n'))
    .filter((s) => s.length > 0);
  bubbleText.textContent = '';
  for (const p of paras) {
    const div = document.createElement('div');
    div.className = 'para';
    div.textContent = p;
    bubbleText.appendChild(div);
  }
}

function appendSay(payload) {
  const id = payload && payload.id ? payload.id : null;
  if (currentId !== null && id && id !== currentId) return;
  if (bubbleText) {
    bubbleRaw += (payload && payload.text) || '';
    renderParagraphs();
  }
  syncEmptyMark();
  show();
  /*
   * 流式过程中也要**重置**存在时长：否则长回复会"说到一半自己消失"。
   * 计时器现在在主进程，所以这里再报一次（它会清掉旧计时器重新起一个）。
   */
  reportShown();
}

function endSay(payload) {
  const id = payload && payload.id ? payload.id : null;
  if (currentId !== null && id && id !== currentId) return;
  currentId = null;
  if (bubble) bubble.classList.remove('streaming');
  if (typing) typing.classList.add('hidden');
  // 有些回复只有 end 带全文（没有 delta）——那时用 fallback 补上
  const fallback = payload && payload.text;
  if (bubbleText && !bubbleRaw && fallback) {
    bubbleRaw = fallback;
    renderParagraphs();
  }
  syncEmptyMark();
  if (bubbleText && bubbleText.textContent) {
    show();
    // 回复结束：从这一刻起算"再显示 lingerMs 后消失"（计时器在主进程）
    reportShown();
  } else {
    /*
     * ## ⚠️ 空回复**不再隐藏气泡**（2026-10-07 用户要求）
     *
     * 原来这里是 `hideAnimated()` —— 注释写的是"空回复（interrupted/error）
     * 不留一个空气泡"。看着合理，但实际后果是用户报的那个：
     *
     *     点宠物 -> 触发新一轮回复 -> 那一轮如果是空的
     *     -> **上一条气泡就此消失**
     *
     * 实测抓到的消息序列（那时还真会空，因为 `content=None` 被接口拒了）：
     *
     *     thinking   text.len=0
     *     say.start  text.len=0
     *     say.end    text.len=0    <- 整个回复为空
     *
     * 用户的要求是「**仅可通过气泡持续时间**」关闭 —— 所以这里什么都不做，
     * 由主进程的 `lingerMs` 计时器去关它。
     *
     * ⚠️ 这一轮没内容时气泡长什么样：**"正在输入"三个点**（`beginSay` 点亮的，
     * 而 `endSay` 上面那行会把它收掉）。所以这里是"文字为空、指示器也已收起"
     * 的状态 —— `syncEmptyMark()` 会给它加 `no-text`，气泡本体隐藏，
     * 不留一个空的深色框。
     *
     * 也就是说：**气泡会不会消失，取决于上一轮有没有内容**。
     * 上一轮有内容 -> 它留在屏幕上直到计时器到点；上一轮也没内容 -> 本来就没东西可显示。
     */
  }
}

function showThinking(payload) {
  if (typeof payload?.lingerMs === 'number' && payload.lingerMs > 0) lingerMs = payload.lingerMs;
  if (typing) typing.classList.remove('hidden');
  if (bubbleText && !bubbleText.textContent && bubble) bubble.classList.remove('streaming');
  show();
}

/**
 * 主进程：气泡消息分发。
 *
 * 消息形状与宠物协议一致（`kind` 是协议类型去掉 `pet.` 前缀），
 * 这样主进程转发时不用做一层映射 —— 少一处能写错的地方。
 */
/*
 * 段间距 -> CSS 变量（2026-10-17）。设置里可调（0~24px，默认 4），
 * 随 bubble:message 的 paraGap 字段下发，或设置改动时单独推 kind='config'。
 */
function applyParaGap(px) {
  const gap = Math.max(0, Math.min(24, Math.round(Number(px) || 0)));
  document.documentElement.style.setProperty('--para-gap', gap + 'px');
}

window.bubble.on('bubble:message', (payload) => {
  const kind = payload && payload.kind;
  if (kind === 'config') {
    // 设置里刚改完段间距 -> 立刻生效（只改 CSS 变量，不动显示状态）
    applyParaGap(payload && payload.paraGap);
    return;
  }
  if (payload && Number.isFinite(Number(payload.paraGap))) {
    // 顺带把最新段间距应用上（与 lingerMs 同一条通道的套路）
    applyParaGap(payload.paraGap);
  }
  switch (kind) {
    case 'thinking':
      showThinking(payload);
      break;
    case 'say.start':
      beginSay(payload);
      break;
    case 'say.delta':
      appendSay(payload);
      break;
    case 'say.end':
      endSay(payload);
      break;
    default:
      break;
  }
});

// 藏起来（宠物被隐藏 / 进入全屏趴着时）
window.bubble.on('bubble:hide', () => {
  hideAnimated();
});
