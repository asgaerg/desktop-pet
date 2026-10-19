/**
 * 宠物右键菜单窗口（2026-10-14）。
 *
 * ## 它替代了什么，为什么
 *
 * 原来是 Electron 的原生菜单（`Menu.popup()`，`petMenu.ts` 里那份）。
 * 但 `MenuItemConstructorOptions` 的 20 个属性里**一个都不是样式** ——
 * 原生菜单做不出设计稿（`F:\美术资源\桌宠右键菜单 UI.pdf`）那种卡片、
 * 开关、图标、蓝色分区线。所以换成**自绘窗口**。
 *
 * ## 分工（重要）
 *
 *     渲染层（petMenu.js）  量尺寸、算子菜单往哪浮、算出窗口矩形
 *     这里                  只做四件事：建窗 / 收矩形并 setBounds / 显示聚焦 / 转点击
 *
 * ## ⚠️ 三个已知的坑，都在这里处理掉
 *
 * ① **页面还没加载完就推数据 = 数据被静默丢掉**
 *    `webContents.send` 在页面就绪前发的消息**不会排队**（这个项目在
 *    `pet:ptt-config` 上踩过：推了配置、渲染层没收到、什么都不报）。
 *    所以这里用一个 `pending` 先存着，`did-finish-load` 之后补发。
 *
 * ② **失焦 = 关闭**（点击别处要收起来）
 *    窗口是 `focusable: true`（**必须** —— 状态条那边踩过"不可聚焦的窗口
 *    连系统拖动都不给"的坑），所以点击别处会触发 `blur`，在那里收起来。
 *    这是菜单"点击外部关闭"的**唯一**实现方式：透明窗口之外的点击
 *    根本不会送到我们这儿。
 *
 * ③ **加载失败必须能被上层知道**
 *    透明窗加载失败的表现是"右键一点反应都没有"，最糟的失败方式。
 *    所以这里把失败记在 `loadFailed` 上，`show()` 返回 `false`，
 *    由调用方**退回原生菜单**（那条路一直留着，见 `petMenu.ts` 的说明）。
 */

import { BrowserWindow, screen } from 'electron';
import {
  MENU_PAGE_FILENAME,
  MENU_PRELOAD_FILENAME,
  rendererUrl,
  resolvePreload,
  resolveRendererPage,
} from './assets.js';
import type { PetMenuItem } from './petMenuTemplate.js';

/** 菜单里被点击的一项 —— `checked` 只对开关类项有值（切换后的状态） */
export interface PetMenuAction {
  action: string;
  checked?: boolean;
}

export interface PetMenuWindowOptions {
  distDir: string;
  /** 诊断输出（定位问题全靠它 —— 菜单没出来是不会有任何报错的） */
  onDiagnostic?: (line: string) => void;
}

export class PetMenuWindow {
  private window: BrowserWindow | null = null;
  private loaded = false;
  /** 页面就绪前收到的菜单数据，就绪后补发一次 */
  private pending: unknown = null;
  /** 加载失败过 —— 上层据此退回原生菜单 */
  private loadFailed = false;
  /** 已销毁（应用退出流程里不再建窗） */
  private destroyed = false;

  constructor(private readonly options: PetMenuWindowOptions) {}

  get browserWindow(): BrowserWindow | null {
    return this.window;
  }

  get isOpen(): boolean {
    const window = this.window;
    return Boolean(window && !window.isDestroyed() && window.isVisible());
  }

  /** 上一次加载是否失败（调用方用它决定要不要退回原生菜单） */
  get hasLoadFailed(): boolean {
    return this.loadFailed;
  }

  /**
   * 弹出菜单。
   *
   * `items` 是 `buildPetMenuTemplate()` 的结果 —— **菜单结构只有那一个真相源**，
   * 这里只是把它转给渲染层。
   *
   * 返回 `false` = 窗口起不来（加载失败），调用方应退回原生菜单。
   */
  show(items: PetMenuItem[], cursor: { x: number; y: number }): boolean {
    if (this.destroyed) return false;

    /*
     * 工作区**按指针所在的那块屏幕**取 —— 多显示器下宠物在主屏、
     * 指针在副屏时，夹到主屏的工作区会把菜单推到屏幕外。
     */
    const display = screen.getDisplayNearestPoint({
      x: Math.round(cursor.x),
      y: Math.round(cursor.y),
    });

    const payload = {
      items,
      cursor: { x: Math.round(cursor.x), y: Math.round(cursor.y) },
      workArea: display.workArea,
    };

    const window = this.ensureWindow();
    if (!window) return false;

    if (!this.loaded) {
      /* 见文件头 ①：先存着，页面就绪后补发 */
      this.pending = payload;
      return true;
    }

    this.sendShow(window, payload);
    return true;
  }

  /** 把菜单数据推给渲染层（并清掉 pending） */
  private sendShow(window: BrowserWindow, payload: unknown): void {
    this.pending = null;
    window.webContents.send('menu:show', payload);
  }

  /** 收起菜单（不销毁窗口 —— 下次右键复用它，省掉一次建窗+加载） */
  hide(): void {
    const window = this.window;
    if (!window || window.isDestroyed()) return;
    if (!window.isVisible()) return;
    window.hide();
    /* 告诉渲染层"这份菜单作废了" —— 免得它留着上一次的 hover/子菜单状态 */
    window.webContents.send('menu:hide');
  }

  /**
   * 建窗（已存在则复用）。
   *
   * ⚠️ 窗口是**常驻**的（第一次右键时建好，之后只 hide/show）：
   * 每次右键都新建一个窗口的话，同一次右键要等"建窗 + 加载页面 + 量尺寸"
   * 才能出现，菜单会明显慢半拍 —— 而菜单是要"跟着指针立刻出现"的东西。
   */
  private ensureWindow(): BrowserWindow | null {
    const existing = this.window;
    if (existing && !existing.isDestroyed()) return existing;

    const page = resolveRendererPage(this.options.distDir, MENU_PAGE_FILENAME);

    const window = new BrowserWindow({
      /*
       * 先给一个小矩形：真正的尺寸由渲染层**量完再报**（`menu:layout`）。
       * 写死一个"够大的猜测值"会让窗口在报之前就显示成一大片透明区，
       * 那一瞬间它会抢走鼠标（连桌面图标都点不动）。
       */
      x: -20000,
      y: -20000,
      width: 320,
      height: 420,
      show: false,
      frame: false,
      transparent: true,
      resizable: false,
      minimizable: false,
      maximizable: false,
      fullscreenable: false,
      skipTaskbar: true,
      /* 菜单自己画卡片和投影，系统再画一层阴影会露在圆角外面 */
      hasShadow: false,
      backgroundColor: '#00000000',
      /*
       * 🔴 `focusable: true` **必须**。
       *
       * 菜单要能做两件靠焦点的事：Esc / 方向键（键盘导航）、以及
       * "点别处就关掉"（`blur`）。`focusable: false` 的窗口两样都拿不到。
       * 状态条那边踩过同一个标志（表现是"拖动完全失效"），见 statusWindow.ts。
       */
      focusable: true,
      webPreferences: {
        preload: resolvePreload(this.options.distDir, MENU_PRELOAD_FILENAME),
        contextIsolation: true,
        nodeIntegration: false,
        sandbox: true,
        /*
         * 菜单页面没有动画，但**不能**让 Windows 因为"看不见"而节流它：
         * 节流会让 `menu:layout` 的回包晚一拍，菜单出现时先闪一下小尺寸。
         */
        backgroundThrottling: false,
      },
    });

    /*
     * 层级用 `pop-up-menu` 而不是 `normal`：
     * 宠物窗口自己也是 `normal` 的置顶窗，同为 normal 时谁在上面只取决于
     * 创建顺序 —— 那是脆的（将来任何一处提前建窗都会让菜单被宠物盖住）。
     * `pop-up-menu` 明确在 normal 之上，又**不会盖住任务栏**
     * （`screen-saver` 会，宠物窗口上踩过）。
     */
    window.setAlwaysOnTop(true, 'pop-up-menu');
    window.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });

    /*
     * 🔴 **强制走"分层窗口"路径**（与对话条/状态条同一个修法）。
     *
     * 没有 `WS_EX_LAYERED` 时 Windows **不做逐像素 alpha 合成** ——
     * 透明区会以窗口底色（灰）露出来，表现就是"卡片圆角外面有一块灰"。
     *
     * 取 `0.999` 而不是 `1`：值等于 1 时可能被"没变化就不用管"跳过，
     * 而 0.999 与 1 的差别**肉眼看不出来**（每通道 0.1%）。
     */
    window.setOpacity(0.999);

    window.once('closed', () => {
      this.window = null;
      this.loaded = false;
    });

    window.webContents.on('did-finish-load', () => {
      this.loaded = true;
      this.options.onDiagnostic?.('[菜单] 页面就绪');
      /* 见文件头 ①：把"页面就绪前"收到的那份菜单补发出去 */
      if (this.pending && !window.isDestroyed()) {
        this.sendShow(window, this.pending);
      }
    });

    window.webContents.on('did-fail-load', (_event, code, desc, url) => {
      this.loadFailed = true;
      this.options.onDiagnostic?.(
        `[菜单] 页面加载失败 code=${code} ${desc} url=${url} —— 将退回原生菜单`,
      );
    });

    /*
     * 见文件头 ②：**失焦即关闭**。
     *
     * ⚠️ 只 hide、不 destroy：下次右键复用这个窗口（建窗+加载要好几百毫秒，
     * 而菜单必须"立刻出现"）。
     */
    window.on('blur', () => {
      this.hide();
    });

    void window.loadURL(rendererUrl(page));
    this.window = window;
    return window;
  }

  /**
   * 渲染层报来的窗口矩形 —— **主进程只做校验和 setBounds**。
   *
   * ⚠️ 为什么布局在渲染层算：行高、文字实测宽度、子菜单实际高度只有 DOM 知道。
   * 主进程这边再夹一次是**安全网**（多显示器 / 改分辨率时渲染层算出的矩形
   * 可能落到屏幕外），不是主要逻辑 —— 所以夹的时候**只保证可见**，
   * 不重算布局（重算就是第二份布局实现，两边迟早不一致）。
   */
  handleLayout(rect: { x?: unknown; y?: unknown; width?: unknown; height?: unknown }): void {
    const window = this.window;
    if (!window || window.isDestroyed()) return;

    const values = [rect?.x, rect?.y, rect?.width, rect?.height].map(Number);
    if (!values.every((v) => Number.isFinite(v))) return;
    let [x, y, width, height] = values;
    if (width < 40 || height < 40) return;

    const display = screen.getDisplayNearestPoint({ x: Math.round(x), y: Math.round(y) });
    const work = display.workArea;
    width = Math.min(width, work.width);
    height = Math.min(height, work.height);
    x = Math.min(Math.max(Math.round(x), work.x), work.x + work.width - width);
    y = Math.min(Math.max(Math.round(y), work.y), work.y + work.height - height);

    window.setBounds({ x, y, width, height });

    /*
     * ⚠️ 顺序：**先 setBounds 再 show**。
     * 反过来会看到窗口先以旧尺寸（或 -20000 那个占位位置）闪一下。
     */
    if (!window.isVisible()) window.show();
    window.focus();
  }

  destroy(): void {
    this.destroyed = true;
    const window = this.window;
    this.window = null;
    this.loaded = false;
    this.pending = null;
    if (window && !window.isDestroyed()) window.destroy();
  }
}
