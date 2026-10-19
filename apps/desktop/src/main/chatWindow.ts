/**
 * 对话条窗口（输入框 + 发送按钮）。
 *
 * ---- 为什么单独一个窗口 ----
 *
 * 用户要求：「仅保留对话的输入框和发送按钮设置在宠物下面，且不会超出电脑屏幕，
 * 无论是宠物边缘探头还是手动拖拽都不影响窗口以相反方向存在」。
 *
 * 挤在宠物窗口里做不到：
 *   · 边缘探头时宠物窗口会**贴到屏幕外**，输入框跟着出去；
 *   · 探头时立绘会**旋转 45°**，输入框要么跟着转、要么被推出窗口；
 *   · 拖到屏幕角落时，窗口内的输入框没有地方可挪。
 * 拆成两个窗口之后，"宠物在哪"和"输入框在哪"可以各自算，互不牵扯。
 *
 * ---- 定位规则 ----
 *
 * 永远在宠物**下面**且**完全在屏幕内**。放不下时按顺序退让：
 *   下方对齐 -> 屏幕内左右收 -> 上方 -> 屏幕内夹紧
 * 探头时宠物贴在屏幕边上，"下方"会超出屏幕，于是自动退到**另一侧**——
 * 这就是"以相反方向存在"。
 */

import { BrowserWindow, screen } from 'electron';
import {
  resolvePreload,
  resolveRendererPage,
  CHAT_PAGE_FILENAME,
  CHAT_PRELOAD_FILENAME,
  rendererUrl,
} from './assets.js';

const CHAT_WIDTH = 300;
const CHAT_HEIGHT = 44;
/** 与宠物之间的间距 */
const GAP = 6;

/**
 * 窗口尺寸的**缩放补偿系数**（2026-10-12 修「四角是灰的」）。
 *
 * ## 这圈灰到底是什么
 *
 * 用户截图：圆角对话条外面有一层灰框。量下来：
 *
 *     截图里的灰框        375 x 55
 *     GetWindowRect       300 x 51
 *     DWM 可见框架        375 x 64     <- 375 = 300 × 1.25
 *
 * **DWM 在窗口矩形外多铺了 1.25 倍**（这台机器是 125% 缩放）。
 * 而页面背景是透明的、CSS 里没有任何元素能画出它 ——
 * 所以那圈灰**不是我们画的**，是 DWM 铺的。
 *
 * ## 为什么宠物窗口"看起来"没事
 *
 *     宠物 GetWindowRect 252x338，DWM 可见 315x422 —— **同样多出 25%**。
 *
 * 只是它 250x250 的立绘把大部分盖住了，边角上不显眼；
 * 而对话条又细又长，**整条边都被这圈灰包着**，所以一眼就看到了。
 *
 * ## 所以退一步：把窗口调小
 *
 * 既然 DWM 一定会多铺 25%，那就让窗口矩形按 1/1.25 给 ——
 * 多铺出来的正好补回到设计尺寸。
 *
 * ⚠️ 这是**绕**，不是修。真正的修法是让 DWM 别多铺
 * （`hasShadow:false` + `transparent:true` 在这台机器上都没能阻止它）。
 * 记在这里，将来换机器/换 Electron 版本要重新量。
 */
const DPI_COMPENSATION = 1.25;


/**
 * 窗口**实际**该用的尺寸（设计尺寸 ÷ DWM 多铺的倍数）。
 *
 * 只在**创建 / 钉角**时用 —— 那是窗口矩形的入口。
 * CSS 里仍然是设计尺寸（300x44），因为 DWM 多铺的部分会把 CSS 尺寸
 * **放大回设计尺寸**。
 */
function windowWidth(): number {
  return Math.round(CHAT_WIDTH / DPI_COMPENSATION);
}

function windowHeight(): number {
  return Math.round(CHAT_HEIGHT / DPI_COMPENSATION);
}

export interface ChatWindowOptions {
  distDir: string;
  devMode: boolean;
  /**
   * 用户把对话条拖到哪了（2026-10-12 加）。
   *
   * `null` = 没拖过，主进程按默认规则钉在右下角。
   *
   * ⚠️ 存**绝对坐标**而不是"相对某个角"：对话条不在窗口边缘，
   * 用户拖到哪就是哪，没有"吸附哪条边"可存。
   * （与状态条不同 —— 那个贴屏幕边缘，所以只存 edge + y。）
   */
  savedPosition?: { x: number; y: number } | null;
  /** 拖动结束后把新位置交出去落盘 */
  onPositionChanged?: (position: { x: number; y: number }) => void;
}

export interface Rect {
  x: number;
  y: number;
  width: number;
  height: number;
}

/**
 * 计算对话条该放在哪。
 *
 * 抽成**纯函数**是为了可测：定位这种几何逻辑最容易在边界上出错
 * （屏幕 1366 宽、宠物贴右边缘、输入框很长……），而肉眼很难穷举。
 */
export function placeChatBar(
  pet: Rect,
  chat: { width: number; height: number },
  workArea: Rect,
  preferBelow = true,
): Rect {
  const clamp = (value: number, min: number, max: number): number =>
    Math.max(min, Math.min(max, value));

  // ① 横向：先与宠物左对齐，再夹进工作区
  const maxX = workArea.x + workArea.width - chat.width;
  const x = clamp(pet.x, workArea.x, Math.max(workArea.x, maxX));

  // ② 纵向：优先放下面；下面放不下就放上面
  const below = pet.y + pet.height + GAP;
  const above = pet.y - chat.height - GAP;
  const fitsBelow = below + chat.height <= workArea.y + workArea.height;
  const fitsAbove = above >= workArea.y;

  let y: number;
  if (preferBelow) {
    y = fitsBelow ? below : fitsAbove ? above : clamp(below, workArea.y, workArea.y + workArea.height - chat.height);
  } else {
    y = fitsAbove ? above : fitsBelow ? below : clamp(above, workArea.y, workArea.y + workArea.height - chat.height);
  }

  return { x: Math.round(x), y: Math.round(y), width: chat.width, height: chat.height };
}

/**
 * 宠物贴在屏幕边缘时，对话条该靠哪一侧。
 *
 * 宠物贴右边时，对话条要**左对齐屏幕右边缘**（即"反过来"贴左边那侧），
 * 否则它会被推出屏幕。返回的是"更希望的水平锚点"。
 */
export function preferOppositeSide(pet: Rect, workArea: Rect): 'left' | 'right' {
  const rightGap = workArea.x + workArea.width - (pet.x + pet.width);
  const leftGap = pet.x - workArea.x;
  // 宠物贴右边 -> 对话条靠左；贴左边 -> 靠右；都不贴 -> 跟宠物左对齐
  if (rightGap <= leftGap) return 'right';
  return 'left';
}

/**
 * 把对话条钉到工作区的某个角（默认右下角）。
 *
 * 用户要求：「把这个对话框放在最右下角」。
 *
 * 为什么不继续"跟着宠物"：原来按宠物位置算"待在宠物下面"，
 * 宠物一进探头、一被拖到边缘，对话条就跟着满屏跑
 * （实测飘到 617,382 —— 屏幕中间偏上，既难找又打断输入）。
 * 固定锚点之后用户永远知道去哪打字，也不受宠物乱动影响。
 *
 * 边距用 `CORNER_MARGIN`，别贴死任务栏。
 */
export function cornerRect(
  workArea: Rect,
  chat: { width: number; height: number },
  corner: 'bottom-right' | 'bottom-left' | 'top-right' | 'top-left' = 'bottom-right',
  margin = CORNER_MARGIN,
): Rect {
  const left = corner.endsWith('left');
  const top = corner.startsWith('top');
  return {
    x: Math.round(left ? workArea.x + margin : workArea.x + workArea.width - chat.width - margin),
    y: Math.round(top ? workArea.y + margin : workArea.y + workArea.height - chat.height - margin),
    width: chat.width,
    height: chat.height,
  };
}

/** 钉到角落时与屏幕边缘的间距 */
export const CORNER_MARGIN = 12;

export class ChatWindow {
  private window: BrowserWindow | null = null;
  /** 当前是否处于"探头"模式（决定放上面还是下面、靠哪边） */
  private peeking = false;
  /** 最近的宠物矩形，用于重排 */
  private lastPetRect: Rect | null = null;

  constructor(
    private readonly options: ChatWindowOptions,
    /** 诊断输出（定位问题全靠它 —— 位置错了不会有任何报错） */
    private readonly onDiagnostic?: (line: string) => void,
  ) {}

  get browserWindow(): BrowserWindow | null {
    return this.window;
  }

  /**
   * 用户拖动后落下的位置（`null` = 没拖过，仍按默认规则钉角）。
   *
   * ⚠️ 一旦有值，`anchorToCorner()` 就**不再生效** —— 否则：
   *   宠物一拖、一探头 -> 调 `reposition()` -> `anchorToCorner('bottom-right')`
   *   -> 用户刚摆好的位置被**瞬间覆盖**，表现是"拖了没用"。
   */
  private userPosition: { x: number; y: number } | null = null;

  /** 用户是否亲手摆过位置（决定要不要继续自动钉角） */
  get hasUserPosition(): boolean {
    return this.userPosition !== null;
  }

  /**
   * "现在这次移动是**程序**发起的"闸门（见 `moved` 处理里的说明）。
   *
   * ⚠️ 不加这个的话，`anchorToCorner()` 的 `setBounds()` 会触发 `moved`，
   * 于是**一启动就把默认位置记成"用户摆过的"** —— 自动钉角从此失效。
   */
  private programmaticMove = false;

  /** 自己挪窗口（`moved` 事件据此忽略），并在稍后解除闸门 */
  private moveProgrammatically(rect: Rect): void {
    const window = this.window;
    if (!window || window.isDestroyed()) return;
    this.programmaticMove = true;
    window.setBounds(rect);
    /*
     * 闸门什么时候解除：`moved` 是从系统消息泵里异步发出来的，
     * 所以 `setBounds` 返回时事件**还没到**。用一个短定时器兜住它。
     *
     * 120ms 是量出来的余量（`moved` 实测在 `setBounds` 后几毫秒内到达）；
     * 真要是超时了也不会有大问题 —— 那次点击最多被当成"用户拖动"记一次
     * 坐标，而那个坐标就是现在的真实位置，不是错的。
     */
    setTimeout(() => {
      this.programmaticMove = false;
    }, 120);
  }

  /** 从设置里恢复上次的位置（启动时调一次） */
  restorePosition(position: { x: number; y: number } | null | undefined): void {
    if (
      position &&
      Number.isFinite(Number(position.x)) &&
      Number.isFinite(Number(position.y))
    ) {
      this.userPosition = { x: Math.round(Number(position.x)), y: Math.round(Number(position.y)) };
    }
  }

  /** 退回"自动钉右下角"（菜单里的复位用） */
  clearUserPosition(): void {
    this.userPosition = null;
  }

  create(): BrowserWindow {
    if (this.window && !this.window.isDestroyed()) return this.window;

    const window = new BrowserWindow({
      // ⚠️ 用**补偿后**的尺寸 —— 见 DPI_COMPENSATION 的说明
      width: windowWidth(),
      height: windowHeight(),
      show: false,
      frame: false,
      transparent: true,
      resizable: false,
      /*
       * 🔴 **必须 `true`**（2026-10-12 实测修正）。
       *
       * 这里原本写 `movable: false`，我以为它只管"程序化移动"和 macOS 的标题栏，
       * 而用户拖拽由 `-webkit-app-region: drag` 负责。
       *
       * **实测是错的**：`movable: false` 会让 app-region 拖拽**整个失效**
       * （渲染层量得清清楚楚 —— 抓手在、22x28、`app-region: drag` 正常，
       * 但鼠标按住拖就是不动）。用户报「拖不动」两次，第二次才定位到这里。
       *
       * 教训：Electron 的窗口 flag 与 CSS 拖拽区**不是两个独立开关**，
       * `movable` 是总闸。凡是"我以为不影响"的地方，都得实测一遍。
       */
      movable: true,
      /*
       * 🔴 **去掉 Windows 的标准可调整边框**（2026-10-12 修「四角是灰的」）。
       *
       * ## 现象
       *
       * 用户截图：圆角对话条**外面**还有一层灰（四角 + 上下边）。
       * 而 `html/body` 是 `background: transparent`、`.bar` 有 `border-radius: 12px`
       * —— 页面本身**没有任何东西**能画出那层灰（已用
       * `probe-chat-bg.py`（历史实测脚本）逐个元素量过计算样式确认）。
       *
       * ## 为什么怀疑 `thickFrame`
       *
       * 这个窗口是 `frame: false` + `transparent: true`，但 **`thickFrame`
       * 默认是 `true`** —— Electron 仍会给它加**标准的可调整边框**
       * （Windows 上表现为一圈不可见热区 + DWM 边框）。
       * 不透明窗口看不出差别，而**透明窗口下那圈边框会以灰底露出来**。
       *
       * 关掉之后窗口就是一个干净矩形，圆角外面直接透出桌面。
       *
       * ⚠️ 代价：失去系统级的"拖边框调整大小"和贴边手感。
       * 但本窗口本来就 `resizable: false`，拖拽走 CSS 的 app-region ——
       * 所以实际上一点都没损失。
       */
      thickFrame: false,
      minimizable: false,
      maximizable: false,
      fullscreenable: false,
      skipTaskbar: true,
      hasShadow: false,
      // 不抢焦点：输入框在需要时由主进程单独聚焦，
      // 否则每次重排窗口都会把用户正在别的程序里的输入打断。
      focusable: true,
      backgroundColor: '#00000000',
      webPreferences: {
        preload: resolvePreload(this.options.distDir, CHAT_PRELOAD_FILENAME),
        contextIsolation: true,
        nodeIntegration: false,
        sandbox: true,
        // 与宠物窗口同理：不是在焦点的窗口也要保持响应（否则输入框会卡）
        backgroundThrottling: false,
      },
    });

    /*
     * 🔴 **强制窗口走"分层窗口"路径**（2026-10-12 修「四角是灰的」）。
     *
     * ## 现状与证据
     *
     * 用户截图：圆角对话条**外面**有一层灰（四角 + 上下边）。
     * 但页面上没有任何元素能画出它（`probe-chat-bg.py`（历史实测脚本）逐个量过
     * 计算样式：只有 `.bar` 有背景，其余全透明）。
     *
     * 用 Win32 查窗口扩展样式，差异一目了然：
     *
     *     宠物    WS_EX_LAYERED = **True**    <- 透明正常（用户说它没问题）
     *     对话条  WS_EX_LAYERED = **False**   <- 四角是灰的
     *     状态条  WS_EX_LAYERED = **False**
     *
     * **`WS_EX_LAYERED` 是 Windows 做逐像素 alpha 合成的前提**。
     * 没有它，窗口的透明区不会被合成掉，而是以窗口底色（灰）露出来。
     *
     * ## 为什么"调用 `setIgnoreMouseEvents(false)`"没用
     *
     * 我一开始以为差异来自宠物窗口会调 `setIgnoreMouseEvents(true, {forward:true})`
     * —— 试了 `setIgnoreMouseEvents(false)`，**样式没变**（说明 Chromium 只在
     * `true` 且需要转发时才加那个样式）。所以那条推断是错的，已废弃。
     *
     * ## 用 `setOpacity` 走另一条路
     *
     * Chromium 在 Windows 上还有另一条会加 `WS_EX_LAYERED` 的路径：
     * **设置不透明度**（半透明窗口必须靠分层窗口合成）。
     *
     * 取 `0.999` 而不是 `1`：值等于 1 时 Chromium 可能直接跳过这一步
     * （"没变化就不用管"），而 0.999 与 1 的视觉差别**完全看不出来**
     * （每通道差 0.1%，在 8bit 色深下通常四舍五入回同一个值）。
     */
    window.setOpacity(0.999);

    // 始终在宠物之上：对话条被宠物挡住会很难用
    window.setAlwaysOnTop(true, 'screen-saver');
    window.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });

    /*
     * 🔴 **让窗口变成 `WS_EX_LAYERED`**（2026-10-12 修「四角是灰的」）。
     *
     * ## 现象与根因
     *
     * 用户截图：圆角对话条**外面**有一层灰（四角 + 上下边）。
     * 但 `html/body` 是 `transparent`、`.bar` 有 `border-radius` ——
     * 页面上**没有任何元素**能画出那层灰（已用 `probe-chat-bg.py`（历史实测脚本）
     * 逐个元素量过计算样式确认）。
     *
     * 用 Win32 查窗口样式，差异一目了然：
     *
     *     宠物    WS_EX_LAYERED = **True**    <- 透明正常（用户说它没问题）
     *     对话条  WS_EX_LAYERED = **False**   <- 四角是灰的
     *     状态条  WS_EX_LAYERED = **False**
     *
     * **`WS_EX_LAYERED` 是 Windows 做逐像素 alpha 合成的前提**。
     * 没有它，窗口的透明区不会被合成掉，而是以窗口底色（灰）呈现。
     *
     * ## 为什么宠物窗口有、对话条没有
     *
     * 三个窗口的**创建参数完全一样**（`frame:false` + `transparent:true` +
     * `hasShadow:false` + `backgroundColor:'#00000000'`），所以差异来自
     * **运行时的调用**。唯一的内建差异：
     *
     *     宠物窗口会调 `setIgnoreMouseEvents(true, { forward: true })`
     *     （它要鼠标穿透），而对话条**一次都没调过**。
     *
     * 在 Windows 上 `setIgnoreMouseEvents` 会让 Electron 给窗口加上
     * `WS_EX_LAYERED` —— 于是宠物窗口"顺带"拿到了正确的透明，
     * 而对话条没有。
     *
     * ## 所以这里显式补一次
     *
     * 调 `setIgnoreMouseEvents(false)`：**不改变鼠标行为**（对话条本来就要
     * 接收鼠标事件，`false` 正是它想要的），只是把那个扩展样式带出来。
     */
    window.setIgnoreMouseEvents(false);

    /*
     * 用户拖动结束 -> 记住位置并落盘（2026-10-12 用户要求可拖拽）。
     *
     * ## 拖拽本身早就通了，缺的是"记住"
     *
     * `chat.css` 里一直有 `-webkit-app-region: drag`（整条对话条可拖，
     * 输入框/按钮单独 `no-drag`）—— 所以"能不能拖"从来不是问题。
     * 问题是**拖完没人记**：`anchorToCorner()` 会在下一次重排时把它钉回右下角
     * （宠物一拖、一探头就触发），用户看到的是"拖了没用"。
     *
     * ## ⚠️ 为什么用 `moved` 而不是让渲染层上报
     *
     * CSS 拖拽是**系统接管**的 —— 渲染层在拖动期间收不到 mousemove
     * （鼠标事件被拖拽吃掉了），所以它**根本没法上报坐标**。
     * 而主进程的 `moved` 事件是 Electron 在拖动结束时给的，正好是我们要的时机。
     *
     * ## 🔴 但 `moved` **程序化移动时也会触发**（实测踩到）
     *
     * 第一次重启后就发现 `settings.json` 里被写进了 `chatX: 1224, chatY: 760`
     * —— 那正是**右下角的默认位置**，用户根本没拖过。
     *
     * 原因：`anchorToCorner()` 调 `setBounds()` 时 Electron 照样发 `moved`。
     * 于是一启动就把"默认位置"记成了"用户摆过的位置"，
     * 日志里出现误导性的 `对话条定位 -> 用户设定的 1224,760（跳过钉角）`。
     *
     * 危害不只是脏数据：一旦被标成"用户位置"，`anchorToCorner()` 就走
     * 早退分支，**自动钉角的逻辑永远不再执行** —— 换屏幕、换分辨率之后
     * 它会停在旧坐标上（可能已经在屏幕外）。
     *
     * 所以加 `programmaticMove` 闸门：自己动的那些不算数。
     */
    window.on('moved', () => {
      if (window.isDestroyed()) return;
      if (this.programmaticMove) return;
      const bounds = window.getBounds();
      this.userPosition = { x: bounds.x, y: bounds.y };
      this.options.onPositionChanged?.({ x: bounds.x, y: bounds.y });
      this.onDiagnostic?.(`对话条被拖动 -> ${bounds.x},${bounds.y}（已记住，不再自动钉角）`);
    });

    void window.loadURL(
      rendererUrl(resolveRendererPage(this.options.distDir, CHAT_PAGE_FILENAME)),
    );
    window.once('ready-to-show', () => {
      // ⚠️ 这里**必须无条件显示**，不能按 devMode 分支。
      //
      // 我原来写的是 `if (!this.options.devMode) window.showInactive()` ——
      // 结果是 `--dev` 启动时对话条**永远不显示**（实测：visible=false），
      // 而开发时正是最需要看到它的时候。这种"逻辑写反"不会报错，
      // 只会让人以为布局算错了，然后去查定位代码（我确实先查错了方向）。
      window.showInactive();
    });

    // ⚠️ **必须存下来**。这里原来漏了 `this.window = window` ——
    // 后果是所有依赖 `this.window` 的方法（`anchorToCorner` / `reposition` /
    // `show` / `hide` / `send` / `sendStatus` / `focusInput`）**全部静默 return**：
    //   `if (!window || window.isDestroyed()) return;`
    // 表现就是：对话条从创建那一刻起**再没被定位过**，一直停在构造函数给的
    // 初始位置（实测 617,382 —— 屏幕中间偏上），而且日志里连一条定位诊断都没有。
    // 这类"字段没赋值 -> 方法静默早退"最难查：链路上每一环看起来都对。
    this.window = window;
    return window;
  }

  /** 开发模式也显示（方便调试），生产环境由主进程按需 show */
  show(): void {
    const window = this.window;
    if (!window || window.isDestroyed()) return;
    if (!window.isVisible()) window.showInactive();
  }

  hide(): void {
    const window = this.window;
    if (!window || window.isDestroyed()) return;
    if (window.isVisible()) window.hide();
  }

  isVisible(): boolean {
    return Boolean(this.window && !this.window.isDestroyed() && this.window.isVisible());
  }

  /** 切换探头模式（决定放宠物上方还是下方、靠哪边） */
  setPeeking(peeking: boolean): void {
    this.peeking = peeking;
    if (this.lastPetRect) this.reposition(this.lastPetRect);
  }

  /**
   * 把对话条钉到某个角（默认右下角）。
   *
   * 这是现在的**唯一**定位入口 —— 不再按宠物位置算。
   * 选哪块屏幕：跟着宠物所在那块走（多显示器下用户把宠物拖到副屏，
   * 对话条也该在那块屏的右下角，而不是主屏）。
   */
  anchorToCorner(corner: 'bottom-right' | 'bottom-left' | 'top-right' | 'top-left' = 'bottom-right'): void {
    const window = this.window;
    if (!window || window.isDestroyed()) return;
    /*
     * ⚠️ **用户亲手摆过位置就不再自动钉角**（2026-10-12）。
     *
     * 不加这条守卫的话，"可拖拽"就是个假功能：宠物一拖、一探头会触发
     * `reposition()` -> `anchorToCorner('bottom-right')`，用户刚摆好的位置
     * 被**瞬间覆盖**，表现是"拖了没用"。
     *
     * 想回到自动模式就用菜单里的「复位到右下角」——
     * 那条路会先 `clearUserPosition()` 再调这里。
     */
    if (this.userPosition) {
      /*
       * ⚠️ 尺寸用**补偿后**的 `windowWidth()` / `windowHeight()`，
       * 不是设计值 `CHAT_WIDTH` / `CHAT_HEIGHT`。
       *
       * 这个坑我踩了一次：创建时用的是补偿值（240x35），
       * 但这条路径写的是设计值（300x44）—— 于是**启动时它又把窗口改回 300**，
       * 补偿整个失效（实测视口仍是 300x46，灰框照旧）。
       *
       * 教训：**同一个量有多条写入路径时，改一处不算改**。
       * 窗口尺寸现在有 3 条路径（创建 / 钉角 / 用户位置），必须全用同一个函数。
       */
      this.moveProgrammatically({
        x: this.userPosition.x,
        y: this.userPosition.y,
        width: windowWidth(),
        height: windowHeight(),
      });
      this.onDiagnostic?.(
        `对话条定位 -> 用户设定的 ${this.userPosition.x},${this.userPosition.y}（跳过钉角 ${corner}）`,
      );
      return;
    }
    // 有宠物矩形就用它选屏幕；没有就退化为主屏
    const ref = this.lastPetRect ?? { x: 0, y: 0, width: 0, height: 0 };
    const display = screen.getDisplayNearestPoint({
      x: ref.x + Math.round(ref.width / 2),
      y: ref.y + Math.round(ref.height / 2),
    });
    const rect = cornerRect(display.workArea, { width: windowWidth(), height: windowHeight() }, corner);
    this.moveProgrammatically(rect);
    this.onDiagnostic?.(
      `对话条定位 -> ${corner} ${rect.x},${rect.y} ${rect.width}x${rect.height}` +
        `（工作区 ${display.workArea.x},${display.workArea.y} ${display.workArea.width}x${display.workArea.height}）`,
    );
  }

  /**
   * 按宠物窗口的当前位置重排对话条。
   *
   * @deprecated 保留只是为了兼容旧调用；现在用 `anchorToCorner`。
   * 按宠物位置算会让对话条随宠物满屏跑（用户已明确否掉）。
   */
  reposition(petRect: Rect): void {
    this.lastPetRect = petRect;
    this.anchorToCorner('bottom-right');
  }

  /** 让渲染层把输入框聚焦（Ctrl+Alt+L / 点宠物说话） */
  focusInput(): void {
    const window = this.window;
    if (!window || window.isDestroyed()) return;
    // ⚠️ 用户显式隐藏了对话框时**不要**把它拉出来。
    //
    // 不加这条守卫的话，"隐藏对话框"就成了假开关：用户一说话（或按 Ctrl+Alt+L），
    // 它又冒出来了。用户要的是"我不想看它"，所以这个意愿必须优先。
    // （想让它回来就再用菜单切换一次。）
    if (!this.userHidden) this.show();
    window.webContents.send('chat:focus-input');
  }

  /**
   * 用户在菜单里切换"隐藏/显示对话框"。
   *
   * 这是**用户意愿**，与内部的临时显隐区分开：`userHidden` 为真时，
   * `focusInput()` 之类"为了交互临时显示"的路径都不该把它拉出来。
   */
  setUserHidden(hidden: boolean): void {
    this.userHidden = hidden;
    if (hidden) this.hide();
    else this.show();
  }

  get hiddenByUser(): boolean {
    return this.userHidden;
  }

  private userHidden = false;

  /** 运行状态 -> 那个小圆点 */
  sendStatus(text: string): void {
    const window = this.window;
    if (!window || window.isDestroyed()) return;
    window.webContents.send('chat:status', { text });
  }

  /** 转发下行消息（回复、错误、toast） */
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
