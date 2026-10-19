/**
 * 记忆管理窗口。
 *
 * ---- 为什么是一个**独立窗口** ----
 *
 * 用户要求（原话）：「设置单开一页记忆管理，里面采用 tab 页管理，
 * 三页分别是原文记忆管理、珍藏记忆管理、固定注入提示词选择」。
 *
 * 除了「用户要的」之外还有实际理由：记忆管理是**看数据的界面**
 * （表格、分页、搜索），设置是**填表单的界面**。两者都需要屏幕空间，
 * 挤在一个窗口里两边都局促；而记忆管理会被反复打开查看，
 * 独立窗口可以一直开着，设置填完就关了。
 *
 * ---- 与设置窗口的差异 ----
 *
 * 设置窗口是「居中 820×600」（表单，不用很大）。记忆管理是表格 ——
 * 表格的价值就在于**一屏能看到多少行**，所以给得更大（1080×720），
 * 并且允许最大化。
 *
 * 结构上与 `SettingsWindow` 保持一致（带系统边框、不透明、可缩放、
 * 占任务栏、`showOnce` 三重兜底），这样用户对两个窗口的行为预期一致 ——
 * 而 `SettingsWindow` 那套兜底是踩出来的（见那边的注释：`ready-to-show`
 * 在页面渲染失败时不会到达，窗口就永远不可见，连报错都没有）。
 */

import { BrowserWindow, screen } from 'electron';
import {
  MEMORY_PAGE_FILENAME,
  MEMORY_PRELOAD_FILENAME,
  resolvePreload,
  resolveRendererPage,
  rendererUrl,
} from './assets.js';
import { centerIn, type Rect } from './settingsWindow.js';

/**
 * 窗口尺寸。
 *
 * 表格要一屏多看几行，所以比设置窗口（820×600）大一圈。
 * 仍然**以工作区为上限**收窄 —— 小屏上开一个比屏幕还大的窗口，
 * 用户连关闭按钮都点不到。
 */
const MEMORY_WIDTH = 1080;
const MEMORY_HEIGHT = 720;

export class MemoryWindow {
  private window: BrowserWindow | null = null;

  /** 诊断回调（由主进程注入，写 `dist/startup.log`） */
  diagnostic: ((line: string) => void) | null = null;

  constructor(private readonly distDir: string) {}

  /**
   * 打开记忆管理。**已经开着就拿到最前**，不做 toggle。
   *
   * 理由与 `SettingsWindow.open` 完全相同（那里记了完整的踩坑过程）：
   * `close()` 是异步的，toggle 会在"关闭中/已关闭"之间来回切，
   * 表现就是"点了没反应"。而用户点「记忆管理」时想要的是**把它拿出来**。
   */
  open(anchor?: Rect): BrowserWindow {
    const existing = this.window;
    if (existing && !existing.isDestroyed()) {
      if (existing.isMinimized()) existing.restore();
      existing.show();
      existing.focus();
      return existing;
    }

    const page = resolveRendererPage(this.distDir, MEMORY_PAGE_FILENAME);
    // 多显示器下跟宠物同屏（与设置窗口同规则）
    const display = anchor
      ? screen.getDisplayNearestPoint({ x: anchor.x, y: anchor.y })
      : screen.getPrimaryDisplay();
    const { workArea } = display;
    const width = Math.min(MEMORY_WIDTH, Math.max(480, workArea.width - 24));
    const height = Math.min(MEMORY_HEIGHT, Math.max(360, workArea.height - 24));
    const rect = centerIn({ width, height }, workArea);

    const window = new BrowserWindow({
      ...rect,
      frame: true,
      title: '桌面宠物 · 记忆管理',
      transparent: false,
      backgroundColor: '#1c1d24',
      resizable: true,
      minimizable: true,
      // 表格界面最大化很常用（一屏多看几十行），所以明确允许
      maximizable: true,
      skipTaskbar: false,
      show: false,
      autoHideMenuBar: true,
      webPreferences: {
        preload: resolvePreload(this.distDir, MEMORY_PRELOAD_FILENAME),
        contextIsolation: true,
        nodeIntegration: false,
        sandbox: true,
      },
    });

    /*
     * 显示窗口 —— 三重兜底，理由见 `SettingsWindow`：
     * `ready-to-show` 在页面渲染失败时不会到达，而窗口是 `show: false`，
     * 于是它就永远不可见，且没有任何报错。
     */
    const showOnce = (why: string) => {
      if (window.isDestroyed() || window.isVisible()) return;
      window.show();
      this.diagnostic?.(`记忆管理窗口已显示（${why}）`);
    };
    window.once('ready-to-show', () => showOnce('ready-to-show'));
    window.webContents.once('did-finish-load', () => showOnce('did-finish-load'));
    window.webContents.on('did-fail-load', (_e, code, desc, url) => {
      this.diagnostic?.(`记忆管理页面加载失败 code=${code} ${desc} url=${url}`);
      showOnce('did-fail-load（页面加载失败，仍显示以便看到问题）');
    });

    window.on('closed', () => {
      this.window = null;
    });

    void window.loadURL(rendererUrl(page));
    this.window = window;
    return window;
  }

  get browserWindow(): BrowserWindow | null {
    return this.window;
  }

  /** 主进程 -> 记忆管理窗口 */
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
