/**
 * 设置窗口（**弹窗式**）。
 *
 * ---- 为什么是弹窗而不是独立窗口 ----
 *
 * 用户要求（原话）：「设置窗口改弹窗式，右键菜单不用改」。
 *
 * 之前是带标题栏的独立窗口：它占任务栏条目、看起来像"另一个应用"，
 * 不像宠物的一部分。现在无边框 + 圆角 + 点外面就收起，
 * 但**位置仍然是屏幕居中、尺寸放大**（用户后续要求：
 * 「弹窗设置屏幕居中，放大一些」）—— 设置是表单，居中大一点更好填。
 *
 * 演化记录（避免以后又改回来时踩重复的坑）：
 *   ① 独立窗口（带标题栏）      —— 占任务栏、不像宠物的一部分
 *   ② 无边框 + 贴宠物左侧 640×440 —— 弹窗是"宠物的附属物"这个隐喻
 *   ③ 无边框 + **屏幕居中** 820×600 ← 当前：用户要求居中并放大
 *
 * ⚠️ 为什么**不设 parent**：
 * 设了 parent 之后窗口会始终附属于宠物窗口，而宠物窗口是透明置顶
 * （`screen-saver` 层级）的，会让设置窗口永远浮在最上层、难以操作。
 * 位置我们自己在 `centerIn()` 里算。
 *
 * 它同时仍是"能退出程序"的可靠入口 —— 宠物的任务栏/托盘入口都受过系统限制
 * （`skipTaskbar` 让窗口不占任务栏；Windows 11 又把新托盘图标收进溢出区），
 * 而这个窗口一定能被用户找到并关闭。
 */

import { BrowserWindow, screen } from 'electron';
import { statSync } from 'node:fs';
import path from 'node:path';
import {
  SETTINGS_PAGE_FILENAME,
  SETTINGS_PRELOAD_FILENAME,
  resolvePreload,
  rendererUrl,
  resolveRendererPage,
} from './assets.js';

/**
 * 仓库根目录。
 *
 * ⚠️ 用途只有一个：把页面路径换成 `pet://` 协议里的**相对路径**
 * （`pet://app/apps/desktop/src/renderer/settings.html`）。
 *
 * 和 `index.ts` 里那个同名常量的算法**必须一致** —— 协议处理器也用它来
 * 拼允许读的根。改一处要改两处。
 */
const PROJECT_ROOT = path.resolve(__dirname, '..', '..', '..');

/**
 * 弹窗尺寸。
 *
 * 用户要求：「弹窗设置屏幕居中，放大一些」。
 * 所以从贴宠物的 640×440 改成居中的 820×600（接近改弹窗之前的 820×560，
 * 但略高一点，因为无边框之后少了标题栏、内容区要补回来）。
 */
const POPUP_WIDTH = 820;
const POPUP_HEIGHT = 600;

export interface Rect {
  x: number;
  y: number;
  width: number;
  height: number;
}

/**
 * 把弹窗**居中**摆在工作区里（并保证完全落在工作区内）。
 *
 * 用户要求：「弹窗设置屏幕居中」。
 * 抽成纯函数是为了可测：小屏（工作区比弹窗还小）时必须夹住而不是算出负坐标 ——
 * 这种边界肉眼穷举不过来，而且出问题时表现是"弹窗被屏幕边裁掉、按钮点不到"。
 */
export function centerIn(
  popup: { width: number; height: number },
  workArea: Rect,
): Rect {
  const clamp = (v: number, lo: number, hi: number) => Math.max(lo, Math.min(Math.max(lo, hi), v));
  return {
    x: Math.round(clamp(workArea.x + (workArea.width - popup.width) / 2, workArea.x, workArea.x + workArea.width - popup.width)),
    y: Math.round(clamp(workArea.y + (workArea.height - popup.height) / 2, workArea.y, workArea.y + workArea.height - popup.height)),
    width: popup.width,
    height: popup.height,
  };
}

export class SettingsWindow {
  private window: BrowserWindow | null = null;

  /**
   * 诊断回调（由主进程注入，写 `dist/startup.log`）。
   *
   * 设置窗口本来就有用户看得见的交互（窗口会关），所以这里只记"**自动收起**"
   * 这类用户看不出原因的行为 —— 排查"弹窗自己没了"时全靠它。
   */
  diagnostic: ((line: string) => void) | null = null;

  constructor(private readonly distDir: string) {}

  /**
   * 打开设置窗口。**已经开着就把它拿到最前**，不再"再点一次就关掉"。
   *
   * ---- ⚠️ 为什么不做 toggle（踩过） ----
   *
   * 用户报告：「点击设置，窗口有时候出不来」。
   *
   * 原来这里是 toggle：窗口已存在就 `close()` 再返回。问题有两层：
   *   ① `close()` 是**异步**的，窗口不会立刻消失。用户再点一次、或者手快点两下，
   *      就在"关闭中/已关闭"之间来回切 —— 表现就是**点了没反应**；
   *   ② 从用户视角看，"点设置"这个动作**应该把设置拿出来**，
   *      而不是把它收回去。想关它有标题栏的关闭按钮。
   *
   * 现在：已存在就 show + focus（顺带从最小化恢复），符合普通窗口的预期。
   *
   * `anchor` 是宠物当前矩形，**只用来选显示器**（多显示器下要跟宠物同屏）；
   * 位置本身是屏幕居中的（用户要求「弹窗设置屏幕居中」）。
   */
  open(anchor?: Rect): BrowserWindow {
    const existing = this.window;
    if (existing && !existing.isDestroyed()) {
      if (existing.isMinimized()) existing.restore();
      existing.show();
      existing.focus();
      return existing;
    }

    const page = resolveRendererPage(this.distDir, SETTINGS_PAGE_FILENAME);
    // 用**宠物所在那块屏**的工作区，而不是主屏 —— 多显示器下弹窗要跟宠物同屏
    const display = anchor
      ? screen.getDisplayNearestPoint({ x: anchor.x, y: anchor.y })
      : screen.getPrimaryDisplay();
    const { workArea } = display;
    const width = Math.min(POPUP_WIDTH, Math.max(360, workArea.width - 24));
    const height = Math.min(POPUP_HEIGHT, Math.max(320, workArea.height - 24));
    const rect = centerIn({ width, height }, workArea);

    const window = new BrowserWindow({
      ...rect,
      /*
       * ⚠️ **恢复成带系统边框的普通窗口**。
       *
       * 用户要求：「设置的窗口有问题，还是换成之前的窗口吧」。
       *
       * 演化记录（免得以后又绕回来）：
       *   ① 带边框的独立窗口 820×560、居中、占任务栏 —— 最初的样子
       *   ② 无边框 + 贴宠物左侧 640×440 —— "弹窗是宠物的附属物"
       *   ③ 无边框 + 屏幕居中 820×600 —— 用户要求居中并放大
       *   ④ **带边框 + 屏幕居中 820×620** ← 当前：用户说无边框版"有问题"，换回来
       *
       * 换回来的好处（都是②③踩出来的）：
       *   · 系统标题栏自带关闭按钮 → 页面不必自绘 ×，也不会有点别处就消失的怪行为；
       *   · 可以拖动、可以最小化，用户能把它挪开看宠物；
       *   · 不透明 + 有边框，不需要页面自己画圆角/阴影（那套还引出过
       *     "展开下拉框就被自动关掉"的 bug）。
       *
       * ⚠️ 因此**配套也要撤**：`settings.html` 里的自绘 × 按钮、`settings.css` 里的
       * 透明底/圆角/阴影、以及主进程里 `app.on('browser-window-focus')` 那套
       * "自动收起"。只改这里会让页面留着给无边框用的样式，看起来是个怪方框。
       */
      frame: true,
      title: '桌面宠物 · 设置',
      // ⚠️ 不透明：设置页是表单，桌面透上来会看不清（和宠物窗口相反）
      transparent: false,
      backgroundColor: '#1c1d24',
      // 普通窗口：可缩放、可最小化，占任务栏（用户能找回它）
      resizable: true,
      minimizable: true,
      maximizable: true,
      skipTaskbar: false,
      show: false,
      autoHideMenuBar: true,
      webPreferences: {
        preload: resolvePreload(this.distDir, SETTINGS_PRELOAD_FILENAME),
        contextIsolation: true,
        nodeIntegration: false,
        sandbox: true,
      },
    });

    /*
     * 显示窗口。
     *
     * ⚠️ 不能只靠 `ready-to-show`。
     *
     * 用户报告：「点击设置，窗口有时候出不来」。`ready-to-show` 在页面渲染失败、
     * 或者事件在监听器挂上之前就触发的情况下**不会到达**，而窗口是 `show: false`，
     * 于是它就永远不可见 —— 连个报错都没有。
     *
     * 所以加一道兜底：`did-finish-load`（页面真的加载完了）也显一次；
     * `did-fail-load` 时**照样显示**并记日志 —— 一个看得见的报错窗口
     * 远比"点了没反应"好排查。
     */
    const showOnce = (why: string) => {
      if (window.isDestroyed() || window.isVisible()) return;
      window.show();
      this.diagnostic?.(`设置窗口已显示（${why}）`);
    };
    window.once('ready-to-show', () => showOnce('ready-to-show'));
    window.webContents.once('did-finish-load', () => showOnce('did-finish-load'));
    window.webContents.on('did-fail-load', (_e, code, desc, url) => {
      this.diagnostic?.(`设置页面加载失败 code=${code} ${desc} url=${url}`);
      showOnce('did-fail-load（页面加载失败，仍显示以便看到问题）');
    });

    /*
     * ---- 自动收起：已**删除** ----
     *
     * 无边框弹窗时期这里有一整套"点别处就收起"，为此换过两次判据
     * （`window.on('blur')` → `document.hasFocus()` → `app.on('browser-window-focus')`），
     * 因为 `blur` 会被"展开 `<select>` 原生下拉"误触发，一点就关。
     *
     * 换回系统边框后这套就没必要了：窗口有标题栏和关闭按钮，行为应该像普通窗口
     * —— 用户切到别的应用时**不该**把设置窗口弄没（那正是"有问题"的来源之一）。
     */
    window.on('closed', () => {
      this.window = null;
    });

    /*
     * ⚠️ **必须带版本号加载**（2026-10-09 踩了很久）。
     *
     * 原来这里是 `loadFile(page)` —— 而 Chromium 会**缓存渲染层的 `.js`**。
     * 后果是「我改了 `settings.js`、重新构建、重启应用，界面行为却还是旧的」，
     * 而且 `location.reload()` 也未必换新（模块有自己的缓存）。
     *
     * 我在这上面浪费了大量时间：一路以为是逻辑写错了，实际跑的是几小时前的代码。
     * 具体表现是"点了保存但配置没变" —— 因为旧版代码里那段逻辑根本不同。
     *
     * 现在把页面的 mtime 作为查询串，源码一动 URL 就变，缓存自然失效。
     * （`memoryWindow` / `chatWindow` / `bubbleWindow` / `petWindow` 同理，
     *   这里只做设置页是因为它改动最频繁；其余按需照做。）
     */
    const stamp = (() => {
      try {
        return String(Math.floor(statSync(page).mtimeMs));
      } catch {
        return String(Date.now());
      }
    })();
    /*
     * ⚠️ **走 `pet://` 协议加载**，不是 `file://`（2026-10-09 第二次修缓存）。
     *
     * ## 第一版为什么不够
     *
     * 原来这里是 `loadURL('file://' + page + '?v=' + stamp)`（stamp = 页面 mtime）。
     * 版本号**只加在页面 URL 上**，而 HTML 里的引用是**相对路径、没有版本号**：
     *
     *     <script type="module" src="settings.js"></script>
     *     <link rel="stylesheet" href="settings.css" />
     *
     * 于是 HTML 每次都是新的、**`settings.js` 一直用 Chromium 的缓存**。
     * 表现：改了 JS、重新构建、重启应用，界面行为**还是旧的**。
     * 有一轮"下拉框的值对了（那次改的是 HTML）、但状态行永远显示模板默认
     * 文案「未启用」（JS 是旧的）"就是这个 —— 我一路以为是逻辑写错。
     *
     * ## 现在
     *
     * `pet://` 协议在服务 `.html` 时，会**把里面引用的本地资源 URL 都补上
     * `?v=<该文件 mtime>`**（见 `index.ts` 里 `injectStamps` 的说明）。
     * 源码一动 URL 就变，不用手改 HTML，也不会漏。
     *
     * 协议里只允许读 `apps/` 与 `packages/`，且防 `../` 穿越。
     */
    // ⚠️ 用**共享**的 `rendererUrl`（2026-10-10），别在这个文件里再拼一遍 ——
    //    5 个窗口各拼一遍迟早有一处算歪。
    void window.loadURL(rendererUrl(page));
    this.window = window;
    return window;
  }

  get browserWindow(): BrowserWindow | null {
    return this.window;
  }

  /** 主进程 -> 设置窗口 */
  send(channel: string, payload: unknown): void {
    const window = this.window;
    if (!window || window.isDestroyed()) return;
    window.webContents.send(channel, payload);
  }

  close(): void {
    this.window?.destroy();
    this.window = null;
  }
}
