/**
 * 状态条窗口（2026-10-11 用户要求）。
 *
 * ## 用户原话与最终形态
 *
 * 「联网搜索中... 这个用对话框实现，将对话框设置成可最小化，变成靠在屏幕边缘
 *   可拖拽的亮灯显示状态的状态条，联网搜索时灯变黄色，表示搜索中」
 *
 * 然后收敛为：
 *
 * 「不用显示头像图标，改成颜色灯，下面跟上文字就好了」
 * 「可拖拽只在屏幕边缘拖拽，也就是左右两边的屏幕边缘就行」
 *
 * 所以最终是一个**贴着屏幕左/右边缘的竖条**：
 *
 *     ●     颜色灯（绿=空闲 / 黄=搜索中 / 红=出错）
 *     联
 *     网
 *     搜
 *     索
 *     中
 *
 * ## ⚠️ 为什么"只沿边缘拖"要在两个地方都拦
 *
 * 渲染层拦一次（`status.js` 只上报 `deltaY`，横向位移直接丢掉），
 * 主进程这里**再拦一次**（收到什么都不改 x）。两处都拦看起来冗余，但：
 *
 *   · 只拦渲染层 -> 将来有人"顺手"给渲染层加个 `deltaX` 就破了
 *   · 只拦主进程 -> 错的坐标已经在管道里跑了一圈，多一次出错机会
 *
 * 而这种"边界约束"一旦破了，表现是"状态条跑到屏幕中间飘着"——
 * 用户会觉得"这功能坏了"，但不会有任何报错。
 *
 * ## 位置怎么记
 *
 * 存 `{ edge: 'left'|'right', y: number }`，**不存 x** ——
 * x 永远由"贴哪条边 + 屏幕工作区宽度"算出来。
 * 存 x 的话，换分辨率或换显示器后它会飘到屏幕外。
 */

import { BrowserWindow, screen } from 'electron';
import {
  resolvePreload,
  resolveRendererPage,
  rendererUrl,
  STATUS_PAGE_FILENAME,
  STATUS_PRELOAD_FILENAME,
} from './assets.js';

/**
 * 状态条窗口尺寸。
 *
 * ## 🔴 2026-10-12：改成**设计稿的固定尺寸**
 *
 * 用户给了 `F:\美术资源\桌宠竖向状态栏.pdf`：「状态栏用这个搞，
 * 背景记得搞透明的，蓝色代表联网搜索」。
 *
 * 设计稿的**胶囊本体**量出来是 **40 x 76**（PDF 路径 `[0]`/`[4]`/`[8]`/`[12]`
 * 的包围盒，用 `probe-status-pdf-raw.py`（历史实测脚本）量的，**不是目测**）。
 *
 * ⚠️ **我第一轮量成了 56 x 88 —— 那是把描边路径也算进去了。**
 * 设计稿里每个胶囊有**两条**路径：
 *
 *     胶囊本体  drawings[0]  40 x 76   x0..40    y0..76    fill #1e2229
 *     描边      drawings[3]  42 x 78   x-1..41   y-1..77   fill #33383f
 *
 * 描边那圈比胶囊四周各大 1px（那是设计师表达「1px 边框」的方式），
 * 而 `get_drawings()` 把它们列成两条独立路径 —— 我当时只筛了
 * 「宽度较大的路径」，于是挑中 42 那条并误当成了胶囊本体。
 *
 * **教训：从设计稿量尺寸时要认「哪条是主体」** —— 描边、投影、光晕
 * 都是额外的路径，它们**比主体大**。
 *
 * ### 为什么原来是 17x130
 *
 * 那是**按内容撑**的思路：宽度只够放一个字（17px），高度给长文字留余地
 * （130px，够「联网搜索中…」7 个字）。
 *
 * ### 为什么现在改成固定的 40x76
 *
 * 设计稿给的是一个**形状**（胶囊），不是「按内容伸缩的容器」。
 * 按内容撑的话，显示「空闲」的胶囊会比「搜索」窄 —— 而用户要的是
 * 「一个固定的小胶囊，灯和字在里面变」。
 *
 * ### 三处**必须一起改**（前面那轮踩过"只改一处"的坑）
 *
 *   · 这里的窗口尺寸
 *   · `status.css` 的 `.bar`（`width/height` = 40/76、`border-radius: 20px`、
 *     `padding-top: 11px`、灯的 `10px`、
 *     文字 `font-size: 11px` + `letter-spacing: 4.3px`）
 *   · 主进程传进来的状态文字**必须收敛成 2 个字**
 *     （设计稿四个状态都是 2 字；「联网搜索中…」7 个字放不下）
 *
 * ⚠️ 只改 CSS 不改这里 -> 窗口太小，**胶囊被裁掉**；
 * 只改这里不改 CSS -> 胶囊小一圈，四周留一圈看不见的透明带
 * （那个透明带还会抢鼠标，拖拽手感会偏）。
 */
/*
 * ⚠️ **42x78 而不是 40x76** —— 设计稿的胶囊本体是 40x76，
 * 但 CSS 的 order: 1px 会**长在盒外**（getBoundingClientRect 只报内容盒）。
 *
 * 实测踩到的表现：窗口 40 宽时胶囊的 arLeft 是 **0.8px**（被挤出去），
 * 而 orderTop 渲染成 **0.8px**（125% DPI 下 1 CSS px）。
 * 也就是**胶囊比窗口宽 2px，横向溢出**。
 *
 * 所以窗口要**含边框**：40 + 1 + 1 = 42、76 + 1 + 1 = 78。
 */
export const STATUS_WIDTH = 42;
export const STATUS_HEIGHT = 78;

/**
 * **按文字长度算窗口高度**（2026-10-16 用户要求把「准备/搜索」改成
 * 「语音准备中/联网搜索中」—— 5 个字，固定 78 的窗口装不下）。
 *
 * 竖排文字块高 = n×11px（字号）+ (n-1)×4.3px（字距）：
 * 2 个字 = 26.3，正好是原设计（见 status.css `.text` 那段的推倒过程）；
 * 每多 1 个字多 15.3px。窗口比胶囊大 2px（描边），照抄 STATUS_HEIGHT 的账。
 */
export function heightForLabel(label: string): number {
  const n = [...String(label ?? '')].length;
  return Math.round(STATUS_HEIGHT + Math.max(0, n - 2) * 15.3);
}

/**
 * 贴边时的**内缩**像素。
 *
 * 不是 0：完全贴死时窗口边框会和工作区边缘重合，某些缩放下看起来"被切掉一条"。
 * 留 2px 让它看起来是"贴着"而不是"嵌进去"。
 */
export const STATUS_EDGE_INSET = 2;

/** 拖动时与工作区上下边缘的最小留白（不让它跑出屏幕） */
export const STATUS_VERTICAL_MARGIN = 4;

export type StatusEdge = 'left' | 'right';

/**
 * 状态条的**显示状态**（一档 = 一个灯色 + 一个状态词）。
 *
 * ## 🔴 现在是**五档**（2026-10-17：'voice' 删除，用户拍板）
 *
 *     idle      空闲      绿 #34c759
 *     thinking  思考      紫 #bf7af0    模型在生成 / TTS 合成中（原 voice 并入）
 *     searching 联网搜索中 蓝 #2f86ed   ← 用户指定「联网搜索用蓝色」
 *     speaking  说话中    青 #32ade6    ← 2026-10-17 用户指定改青色（带呼吸）
 *     error     异常      红 #e5484d
 *
 * 历史账：voice（语音准备中，青）2026-10-16 晚加、10-17 删 ——
 * 「和思考中重复了」。合成期间并入 thinking；音频开播直接 speaking。
 * speaking 原是琥珀 #ffb300，2026-10-17 改青（接手原 voice 的呼吸动画）。
 *
 * ⚠️ **别只验"自己想起来的那几个"** ——
 * 判据要断言**完整集合**，多出来或少了任何一档都要报。
 *
 * ## `listening` 是**已删的第七档**
 *
 * ⚠️ 设计稿原本还有 `listening`（聆听，也是琥珀）。
 * **2026-10-16 用户要求彻底删除**：
 *
 * > 「聆听就不用黄色了，宠物自己会显示，就做说话显示黄色和文字就好了」
 * > 「彻底删除聆听」
 *
 * 所以类型、CSS、`statusKindFor` 的关键词、文档**全部删干净了**。
 *
 * ⚠️ **为什么只删显示、不删语音状态**：`VoiceState.LISTENING`
 * （麦克风在听）是**语音管线**的一部分，删了麦克风就不工作了。
 * 两件事名字像，但是不同的东西：
 *
 *     pet.voice.VoiceState.LISTENING   语音管线 —— **保留**
 *     pet.desktop.StatusKind           状态条显示 —— **删掉这一档**
 *
 * ## ⚠️ 蓝色只给搜索（用户 2026-10-12 改口）
 *
 * 用户原话：「**说话的时候就不蓝色了，只有搜索的时候用蓝色**」。
 * （2026-10-17 说话中改的青 #32ade6 不是搜索的蓝 #2f86ed。）
 *
 * ⚠️ 胶囊高度随文字长度伸缩（见 `heightForLabel`），状态词用全称
 * （联网搜索中 / 语音准备中 这类不再截断）。
 *
 * ⚠️ `thinking` / `searching` / `speaking` 都带 `pulse` 呼吸动画
 * （都是"正在进行中"的档）；`idle` / `error` 不闪。
 */
export type StatusKind =
  | 'idle'
  | 'thinking'
  | 'searching'
  // 🔴 'voice'（语音准备中）2026-10-17 删 —— 和思考中重复（用户拍板）。
  //  合成期间并入 thinking；音频开播直接 'speaking'。
  | 'speaking'
  | 'error';

export interface StatusPlacement {
  edge: StatusEdge;
  /** 窗口顶部相对工作区顶部的 y（不是屏幕绝对坐标） */
  y: number;
}

/**
 * 「滑到边缘」动画时长（2026-10-12 用户要求）。
 *
 * 160ms + easeOut：够快（不拖沓），又看得出是"滑过去"而不是"跳过去"。
 * 60fps 下约 10 帧，每帧一次 `setBounds` —— 对这个尺寸的窗口开销可忽略。
 */
const SLIDE_DURATION_MS = 160;

export interface StatusWindowOptions {
  distDir: string;
  /** 诊断输出（定位问题全靠它 —— 位置错了不会有任何报错） */
  onDiagnostic?: (line: string) => void;
  /** 拖动结束后把新位置交出去落盘（主进程负责写 settings） */
  onPlacementChanged?: (placement: StatusPlacement) => void;
}

export class StatusWindow {
  private window: BrowserWindow | null = null;
  private edge: StatusEdge = 'left';
  /** 窗口顶部相对**工作区**顶部的 y */
  private y = 120;
  private kind: StatusKind = 'idle';
  private text = '空闲';
  /**
   * 窗口当前高度（随状态文字长度伸缩，见 `heightForLabel`）。
   * ⚠️ 永远从文字**算出来**而不是读回窗口尺寸 —— 高 DPI 下读回来的值
   * 再写回去会累积放大（见 `applyBounds` 的说明）。
   */
  private currentHeight: number = STATUS_HEIGHT;
  /** 拖动中的起点（`null` = 没在拖） */
  private dragStart: { y: number; pointerY: number } | null = null;
  /**
   * 正在跑的"滑到边缘"动画定时器（`null` = 没在滑）。
   *
   * ⚠️ 必须留着句柄才能**中途取消** —— 用户拖到一半松手、看到它开始滑、
   * 立刻又抓住拖，这时不取消的话动画会把窗口从用户手里拽走。
   */
  private slideTimer: ReturnType<typeof setInterval> | null = null;
  /**
   * "正在吸附/滑动"标志 —— 用来**打破 `moved` 的死循环**。
   *
   * `moved -> 吸附 -> setBounds -> moved -> 吸附 -> ...` 会无限递归。
   * 凡是"程序自己挪窗口"的动作都要在这个标志为真时进行。
   */
  private snapping = false;

  constructor(private readonly options: StatusWindowOptions) {}

  get browserWindow(): BrowserWindow | null {
    return this.window;
  }

  /** 当前落位（给设置持久化用） */
  get placement(): StatusPlacement {
    return { edge: this.edge, y: this.y };
  }

  /** 从设置里恢复上次的位置（启动时调一次） */
  restore(placement: Partial<StatusPlacement> | null | undefined): void {
    if (placement?.edge === 'left' || placement?.edge === 'right') {
      this.edge = placement.edge;
    }
    if (Number.isFinite(Number(placement?.y))) {
      this.y = Math.trunc(Number(placement?.y));
    }
  }

  /** 在宠物所在的那块屏幕上取工作区（多显示器下与宠物同屏） */
  private workAreaOf(reference?: { x: number; y: number }): Electron.Rectangle {
    const display = reference
      ? screen.getDisplayNearestPoint({ x: Math.round(reference.x), y: Math.round(reference.y) })
      : screen.getPrimaryDisplay();
    return display.workArea;
  }

  /** 由 `edge` + `y` 算出真实窗口矩形 */
  private rectFor(workArea: Electron.Rectangle): Electron.Rectangle {
    const maxY = workArea.y + workArea.height - this.currentHeight - STATUS_VERTICAL_MARGIN;
    const minY = workArea.y + STATUS_VERTICAL_MARGIN;
    const top = Math.max(minY, Math.min(maxY, Math.round(workArea.y + this.y)));
    return {
      x:
        this.edge === 'left'
          ? Math.round(workArea.x + STATUS_EDGE_INSET)
          : Math.round(workArea.x + workArea.width - STATUS_WIDTH - STATUS_EDGE_INSET),
      y: top,
      width: STATUS_WIDTH,
      height: this.currentHeight,
    };
  }

  /** 创建（已存在则复用）。`reference` 用来选屏幕 */
  create(reference?: { x: number; y: number }): BrowserWindow {
    const existing = this.window;
    if (existing && !existing.isDestroyed()) return existing;

    const workArea = this.workAreaOf(reference);
    const rect = this.rectFor(workArea);
    const page = resolveRendererPage(this.options.distDir, STATUS_PAGE_FILENAME);

    const window = new BrowserWindow({
      ...rect,
      show: false,
      frame: false,
      transparent: true,
      resizable: false,
      minimizable: false,
      maximizable: false,
      fullscreenable: false,
      skipTaskbar: true,
      /*
       * 🔴 **去掉 Windows 的标准可调整边框**（2026-10-16 修「胶囊四周一圈灰」）。
       *
       * 对话条 2026-10-12 修过一模一样的问题（见 chatWindow.ts 的长注释）：
       * `thickFrame` 默认 true，Electron 仍给窗口加标准可调整边框 ——
       * 透明窗口下那圈 DWM 边框以**灰底**露出来，正好是用户截图里
       * 胶囊上下左右那圈灰。当时只改了对话条，状态条漏了 ——
       * 两天的间隔让用户以为是新问题，其实是同一处没修完。
       *
       * ⚠️ 代价：失去系统级"拖边框调整大小"。本窗口 `resizable: false`、
       * 拖动走 CSS 的 app-region，实际没有损失。
       */
      thickFrame: false,
      hasShadow: false,
      backgroundColor: '#00000000',
      /*
       * 🔴 **必须 `true`**（2026-10-12 用户实测发现「没焦点就拖不动」）。
       *
       * ## 原来的想法（错的）
       *
       * 它是常驻显示的状态条，我以为 `focusable: false` 能避免
       * "点一下它就把输入焦点从正在打字的窗口抢走"。
       *
       * ## 实测：`focusable: false` 会让**系统拖动整个失效**
       *
       * 用户原话：「我发现这个状态条没焦点就拖不动了」。
       *
       * CSS 的 `-webkit-app-region: drag` 走的是**系统的拖动循环**，
       * 而那个循环要求窗口是"可激活"的。`focusable: false` 的窗口拿不到它，
       * 于是鼠标按下去**什么都没发生**。
       *
       * 这也解释了之前"拖不动"的反复：我换了三种拖拽实现
       * （手写鼠标跟踪 → `setIgnoreMouseEvents` → `moved` 事件），
       * 每次看起来都修了，但**根因一直是这个窗口标志**。
       *
       * 旁证：同一个应用里**对话条能拖** —— 它 `focusable: true`。
       *
       * ## 抢焦点的问题怎么解决
       *
       * 不靠这个标志，靠 **`showInactive()`** —— 那才是"显示但不激活"
       * 的正确手段（见 `setVisible`）。用户点它时当然会获得焦点，
       * 但那是用户主动点的，符合预期。
       */
      focusable: true,
      webPreferences: {
        preload: resolvePreload(this.options.distDir, STATUS_PRELOAD_FILENAME),
        contextIsolation: true,
        nodeIntegration: false,
        sandbox: true,
        // 与宠物窗口同理：不在焦点的窗口也要保持响应（否则灯的呼吸动画会卡）
        backgroundThrottling: false,
      },
    });

    // 浮在普通窗口之上；用 'normal' 而不是 'screen-saver' ——
    // 后者会盖住任务栏（宠物窗口上踩过这个坑）。
    window.setAlwaysOnTop(true, 'normal');
    window.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });
    /*
     * 🔴 **强制走"分层窗口"路径**（2026-10-12，与对话条同一个修法）。
     *
     * 没有 `WS_EX_LAYERED` 时 Windows **不做逐像素 alpha 合成** ——
     * 窗口的透明区会以窗口底色（灰）露出来，表现就是
     * "圆角外面有一层灰"（用户在对话条上先发现的，状态条同理）。
     *
     * 实测（`GetWindowLongPtr` 查 `GWL_EXSTYLE`）：
     *
     *     宠物    WS_EX_LAYERED = True    <- 一直是对的
     *     对话条  WS_EX_LAYERED = False   <- 四角灰
     *     状态条  WS_EX_LAYERED = False   <- 同样的毛病
     *
     * Chromium 在 Windows 上会加这个样式的路径之一是**设置不透明度**
     * （半透明窗口必须靠分层窗口合成）。取 `0.999` 而不是 `1`：
     * 值等于 1 时可能被"没变化就不用管"跳过，而 0.999 与 1 的差别
     * **肉眼看不出来**（每通道 0.1%，8bit 下一般四舍五入回同一个值）。
     */
    window.setOpacity(0.999);

    /*
     * 显示窗口 —— 三重兜底（照抄 `MemoryWindow`）。
     *
     * `ready-to-show` 在页面渲染失败时不会到达，而窗口是 `show: false`，
     * 于是它就永远不可见，且**没有任何报错**。这是这个项目里反复出现的
     * "看不见的空窗口"故障，所以每个窗口都要三重兜底。
     */
    const showOnce = (why: string) => {
      if (window.isDestroyed() || window.isVisible()) return;
      window.show();
      this.options.onDiagnostic?.(`状态条已显示（${why}）`);
    };
    window.once('ready-to-show', () => showOnce('ready-to-show'));
    window.webContents.once('did-finish-load', () => {
      showOnce('did-finish-load');
      // 页面就绪后补推一次当前状态（渲染层可能在我们设置之前就挂好了监听）
      this.pushState();
    });
    window.webContents.on('did-fail-load', (_e, code, desc, url) => {
      this.options.onDiagnostic?.(`状态条页面加载失败 code=${code} ${desc} url=${url}`);
      showOnce('did-fail-load（加载失败，仍显示以便看到问题）');
    });

    window.on('closed', () => {
      this.window = null;
    });

    /*
     * 🔴 **松手后吸附到最近的边缘**（2026-10-12 改）。
     *
     * ## 用户原话
     *
     * 「我需要它在我拖住它移动到屏幕中心左右结束拖拽时会自动向最靠近的
     * 屏幕边缘依附」。
     *
     * ## 为什么用 `moved` 而不是渲染层上报
     *
     * 拖动现在走 **CSS 的 `-webkit-app-region: drag`**（系统接管）——
     * 拖动期间渲染层**收不到任何鼠标事件**，所以它没法上报"松手了"。
     * 而 `moved` 正是 Electron 在系统拖动结束时给的信号。
     *
     * 这也是从对话条那边学来的：它的抓手能拖，走的同一套机制。
     */
    window.on('moved', () => {
      if (window.isDestroyed()) return;
      /*
       * ⚠️ **自己移动时不能再吸附** —— 否则是死循环：
       *
       *     moved -> snapToNearestEdge -> applyBounds -> moved -> ...
       *
       * `applyBounds` / 动画每帧都会触发 `moved`，所以吸附期间必须屏蔽。
       * 动画结束（`cancelSlide`）时解除。
       */
      if (this.snapping) return;
      this.snapToNearestEdge();
    });

    void window.loadURL(rendererUrl(page));
    this.window = window;
    return window;
  }

  /** 显示 / 隐藏 */
  setVisible(visible: boolean): void {
    const window = this.window;
    if (!window || window.isDestroyed()) return;
    if (visible) {
      /*
       * ⚠️ **显示前先重新落一次位**（`applyPlacement` 会带上设计尺寸）。
       *
       * 这是一个**自愈**动作：万一窗口尺寸被哪里改大了（实测高 DPI 下
       * `getBounds`/`setBounds` 往返会累积放大，从 17x130 长到 100x382），
       * 每次显示都会把它纠正回来 —— 而不是让错误一直留在那儿。
       *
       * 代价可忽略：一次 `setBounds`。
       */
      this.applyPlacement();
      window.showInactive(); // ⚠️ `showInactive` 不抢焦点
    } else {
      window.hide();
    }
  }

  isVisible(): boolean {
    const window = this.window;
    return Boolean(window && !window.isDestroyed() && window.isVisible());
  }

  /**
   * 更新显示的状态与文字。
   *
   * ⚠️ 相同状态**不重复推** —— 搜索时会连着更新好几次（"搜索中" ->
   * "读网页中" -> …），每次都跨进程序列化一遍纯属浪费。
   *
   * ## ⚠️ 文字会被**截到 2 个字**（2026-10-12）
   *
   * 胶囊固定 40x76（⚠️ 这里以前写 56x88，那是把描边也算进去的**错误量法**），
   * 竖排最多放 2 个字（设计稿每一档都是 2 字）。
   * 给 7 个字的「联网搜索中…」会溢出 —— 而 `overflow: hidden` 只会
   * 把它**裁掉**，用户看到的是"文字缺了一半"，比什么都不显示更迷惑。
   *
   * 所以在这里兜底截断，而不是指望每个调用点都记得给 2 字词。
   * （调用点那边也应该给 2 字词，见 `index.ts` 的 `setStatus`。）
   */
  setStatus(kind: StatusKind, text: string): void {
    /*
     * ⚠️ 不再截 2 个字（2026-10-16）：用户要求状态文字用全称
     * （「语音准备中」「联网搜索中」），胶囊高度改为**随文字长度伸缩**
     * （见 `heightForLabel`）。窗口跟着长高/变矮，中心点保持不动。
     */
    const safe = String(text ?? '').trim() || '空闲';
    if (kind === this.kind && safe === this.text) return;
    const prevHeight = this.currentHeight;
    this.kind = kind;
    this.text = safe;
    this.currentHeight = heightForLabel(safe);
    const window = this.window;
    if (window && !window.isDestroyed() && this.currentHeight !== prevHeight) {
      const bounds = window.getBounds();
      const dy = Math.round((this.currentHeight - prevHeight) / 2);
      const workArea = this.workAreaOf({ x: bounds.x, y: bounds.y });
      const minY = workArea.y + STATUS_VERTICAL_MARGIN;
      const maxY = workArea.y + workArea.height - this.currentHeight - STATUS_VERTICAL_MARGIN;
      const y = Math.max(minY, Math.min(maxY, bounds.y - dy));
      window.setBounds({ x: bounds.x, y, width: STATUS_WIDTH, height: this.currentHeight });
      // 同步记住工作区偏移（restore / applyPlacement 用的是这个）
      this.y = y - workArea.y;
    }
    this.pushState();
  }

  /**
   * 统一的 `setBounds`：**每次都显式带上设计尺寸**（2026-10-12 修）。
   *
   * ## 🔴 为什么不能写 `{ ...bounds, y: top }`
   *
   * 用户报「每次拖拽都会导致周围留白变大」。实测窗口从设计的 **17x130**
   * 一路长到 **100x382**（`probe-status-growth.py`（历史实测脚本）量的）。
   *
   * 根因是**高 DPI 下读回来的尺寸不能原样写回去**：
   * `getBounds()` 拿到的是设备相关的值（这台机器 125% 缩放，
   * 17 逻辑像素读回来是 20），再 `setBounds` 回去又被乘一次 ——
   * 每拖一次大一点，**误差累积**。
   *
   * 所以移动窗口时**永远用常量尺寸**，不用读回来的值。
   * 这样即使某次读歪了，下一次也会被纠正回来（自愈）。
   */
  private applyBounds(rect: { x: number; y: number }): void {
    const window = this.window;
    if (!window || window.isDestroyed()) return;
    window.setBounds({
      x: Math.round(rect.x),
      y: Math.round(rect.y),
      width: STATUS_WIDTH,
      height: this.currentHeight,
    });
  }

  /** 把窗口移到当前 `edge` + `y` 对应的位置 */
  private applyPlacement(reference?: { x: number; y: number }): void {
    const workArea = this.workAreaOf(reference);
    const rect = this.rectFor(workArea);
    this.applyBounds({ x: rect.x, y: rect.y });
  }

  /** 把当前状态推给渲染层 */
  pushState(): void {
    const window = this.window;
    if (!window || window.isDestroyed()) return;
    window.webContents.send('status:state', { state: this.kind, text: this.text });
  }

  /**
   * 渲染层报来的拖动中事件。
   *
   * ## ⚠️ 用**指针绝对坐标**算，不用增量
   *
   * 第一版写的是增量，但"第一次移动只记起点、之后才用增量"这个分支会
   * **把第一次的增量丢掉** —— 表现为"拖了没反应，要拖第二次才动"。
   *
   * 现在：记下"按下时的窗口 y"和"按下时的指针 y"，之后每次都用
   * `按下窗口y + (当前指针y - 按下指针y)`。**幂等、不依赖调用次数**，
   * 也不会因为掉帧而累积误差。
   *
   * ⚠️ **横向刻意不看** —— 需求是"只沿屏幕左右边缘拖"，x 永远由 `edge` 算。
   * 即使有人往 payload 里塞了 `x`，这里也不会用。
   */
  handleDragMove(payload: { y?: number }): void {
    const window = this.window;
    if (!window || window.isDestroyed()) return;

    /*
     * ⚠️ 用户又抓住了 -> **立刻取消正在跑的滑动动画**。
     *
     * 不取消的话动画会继续把它往边缘拽，用户的手感和实际位置对不上
     * （"我明明往右拖，它却在往左跑"）。
     */
    this.cancelSlide();

    const pointerY = Number(payload?.y);
    if (!Number.isFinite(pointerY)) return;

    if (!this.dragStart) {
      /*
       * 第一次移动：记下**两个起点**。
       *
       * ⚠️ 这里刻意**不移动窗口** —— 第一次事件只是"建立基准"。
       * 如果第一次既记基准又移动，会用到还没定义的增量。
       */
      const bounds = window.getBounds();
      this.dragStart = { y: bounds.y, pointerY };
      return;
    }

    const bounds = window.getBounds();
    const rawTop = this.dragStart.y + (pointerY - this.dragStart.pointerY);
    const workArea = this.workAreaOf({ x: bounds.x, y: bounds.y });
    const minTop = workArea.y + STATUS_VERTICAL_MARGIN;
    const maxTop = workArea.y + workArea.height - this.currentHeight - STATUS_VERTICAL_MARGIN;
    const top = Math.max(minTop, Math.min(maxTop, Math.round(rawTop)));

    /*
     * ⚠️ 用 `applyBounds`（它会带上设计尺寸），**不要**写
     * `window.setBounds({ ...bounds, y: top })` —— 见 `applyBounds` 的说明：
     * 高 DPI 下把 `getBounds()` 读回来的尺寸原样写回去会**累积放大**，
     * 每拖一次窗口大一点（实测从 17x130 长到 100x382）。
     */
    if (top !== bounds.y) this.applyBounds({ x: bounds.x, y: top });
  }

  /**
   * 松手：吸附到**最近的左右边缘**，并**平滑滑过去**（2026-10-12 用户要求）。
   *
   * ## 用户原话
   *
   * 「我需要它在我拖住它移动到屏幕中心左右结束拖拽时会自动向最靠近的屏幕边缘依附」。
   *
   * 逻辑上"就近吸附"本来就有（屏幕中线为界），缺的是**手感**：
   * 原来是 `setBounds` 一步跳过去，看起来像"闪了一下"，
   * 用户不确定刚才发生了什么。
   *
   * ## 为什么自己做动画而不是用 `setBounds` 的 `animate` 参数
   *
   * Electron 的 `setBounds(rect, animate)` **只在 macOS 上有效**
   * （Windows/Linux 会忽略那个参数）。而这是个 Windows 为主的项目，
   * 所以只能自己插值。
   *
   * ## 参数怎么定的
   *
   * `160ms` + `easeOut`：够快（不拖沓），又看得出是"滑过去"而不是"跳过去"。
   * 60fps 下约 10 帧，每帧 `setBounds` 一次 —— 这个开销对这个尺寸的窗口可忽略。
   *
   * ⚠️ 换算成相对值再存 y，是因为绝对坐标在多显示器 / 改分辨率后
   * 可能落在屏幕外。存相对值 + 边缘，重启后一定能贴回可见的位置。
   */
  handleDragEnd(): void {
    this.dragStart = null;
    this.snapToNearestEdge();
  }

  /**
   * 把它吸附到**最近的左右边缘**，并平滑滑过去。
   *
   * 两个入口共用：
   *   · 窗口 `moved` 事件（CSS 拖拽，2026-10-12 起的**主路径**）
   *   · `handleDragEnd()`（老的渲染层上报路径，保留以兼容）
   *
   * 两者做同一件事，所以逻辑只有这一份 —— 分成两份迟早漂移。
   */
  private snapToNearestEdge(): void {
    const window = this.window;
    if (!window || window.isDestroyed()) return;
    /*
     * 🔴 置位 `snapping` —— 见 `moved` 处理里的说明。
     *
     * `applyBounds` / 动画每帧都触发 `moved`，不屏蔽就是
     * `moved -> snap -> applyBounds -> moved -> ...` 的**死循环**
     * （表现会是窗口疯狂抖动 + 日志刷屏）。
     */
    this.snapping = true;
    try {
      this.snapToNearestEdgeInner();
    } finally {
      /*
       * ⚠️ 用 `finally` 而不是在末尾置回：中间任何一步抛异常，
       * `snapping` 卡在 true 的话**这个窗口以后再也吸不动了**，
       * 而且不会报错 —— 正是这个项目里最难查的那类故障。
       *
       * ⚠️ 但动画是异步的，所以这里**先解除、再由动画自己维持**：
       * `animateToX` 会在启动时重新置位，结束时解除。
       */
      if (this.slideTimer === null) this.snapping = false;
    }
  }

  private snapToNearestEdgeInner(): void {
    const window = this.window;
    if (!window || window.isDestroyed()) return;
    const bounds = window.getBounds();
    const workArea = this.workAreaOf({ x: bounds.x, y: bounds.y });

    // 离哪条边近就贴哪条（屏幕中线为界）
    const centerX = workArea.x + workArea.width / 2;
    const windowCenterX = bounds.x + bounds.width / 2;
    const nextEdge: StatusEdge = windowCenterX < centerX ? 'left' : 'right';

    this.edge = nextEdge;
    this.y = bounds.y - workArea.y;

    /*
     * 先用 `rectFor` 算出**最终 x**，再把当前的 y 拼回去。
     *
     * ⚠️ 顺序要紧：`rectFor` 按 `this.y` + `this.edge` 算最终矩形，
     * 而动画需要的是"只改 x、y 保持当前值"的位移。
     */
    const target = this.rectFor(workArea);
    const fromX = bounds.x;
    const toX = target.x;

    const placement = this.placement;
    this.options.onDiagnostic?.(
      `状态条吸附 -> ${placement.edge}` +
        `（x ${fromX} -> ${toX}，y=${placement.y}）` +
        (fromX === toX ? '（已在边缘，不需滑动）' : ''),
    );
    this.options.onPlacementChanged?.(placement);

    if (fromX === toX) {
      // 已经在边缘上：不必做无谓的动画
      this.applyBounds({ x: toX, y: bounds.y });
      return;
    }
    this.animateToX(window, fromX, toX, bounds.y);
  }

  /**
   * 把窗口的 x 从 `fromX` 平滑插值到 `toX`（y / 尺寸不变）。
   *
   * ⚠️ **动画期间要允许用户再次拖动**：用户拖到中途松手、看到它开始滑、
   * 立刻又抓住拖 —— 这时必须**立刻取消动画**，否则动画会把窗口从用户手里拽走。
   * `handleDragMove` 里会调 `cancelSlide()`。
   */
  private animateToX(
    window: BrowserWindow,
    fromX: number,
    toX: number,
    currentY: number,
  ): void {
    this.cancelSlide();
    /*
     * 动画期间**一直**保持 `snapping`（每帧 `setBounds` 都会发 `moved`），
     * 由 `cancelSlide()` 在结束时解除 —— 那是唯一的出口，
     * 所以不会出现"忘了解除"的漏网情况。
     */
    this.snapping = true;
    const start = Date.now();
    const timer = setInterval(() => {
      if (window.isDestroyed()) {
        this.cancelSlide();
        return;
      }
      const t = Math.min(1, (Date.now() - start) / SLIDE_DURATION_MS);
      // easeOutCubic：起步快、收尾慢 —— 像被边缘"吸"过去
      const eased = 1 - Math.pow(1 - t, 3);
      const x = Math.round(fromX + (toX - fromX) * eased);
      /*
       * ⚠️ 走 `applyBounds`（带设计尺寸），不要 `setBounds({ ...bounds, x })`。
       * 这里是**最要命的一处**：动画每 16ms 写一次，一共约 10 次 ——
       * 每次放大一点的话，一次拖动就能把窗口撑大一大截。
       */
      this.applyBounds({ x, y: currentY });
      if (t >= 1) this.cancelSlide();
    }, 16);
    this.slideTimer = timer;
  }

  /** 停掉正在跑的滑动动画（再次拖动 / 窗口销毁时调） */
  private cancelSlide(): void {
    if (this.slideTimer !== null) {
      clearInterval(this.slideTimer);
      this.slideTimer = null;
    }
    /*
     * ⚠️ `snapping` 的**唯一解除点**。
     *
     * 动画结束、被用户打断、窗口销毁 —— 三条路都经过这里，
     * 所以不存在"标志卡在 true"的漏网情况
     * （卡住的后果是"这个窗口以后再也吸不动"，而且不报错）。
     */
    this.snapping = false;
  }

  destroy(): void {
    // 先停动画：留着的话它会在窗口销毁后继续调 setBounds（对已销毁的窗口无害，
    // 但定时器本身会一直跑下去，退出流程可能因此拖不完）
    this.cancelSlide();
    const window = this.window;
    this.window = null;
    this.dragStart = null;
    if (window && !window.isDestroyed()) window.destroy();
  }
}
