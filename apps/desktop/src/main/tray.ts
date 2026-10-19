/**
 * 托盘：宠物的"总开关"。
 *
 * 一个常驻桌面的程序必须有不用去翻窗口的出口 —— 否则用户想让它安静或退出时
 * 只能去任务管理器。托盘图标在运行时用代码画（不引入二进制资源文件），
 * 这样仓库里不会多出一个来路不明的 .ico，也不会因为路径问题加载失败。
 *
 * ## 🔴 2026-10-16：菜单**大幅精简**到 5 项
 *
 * 用户原话：
 *
 * > 「托盘菜单现在只保留显示隐藏宠物、宠物大小、设置、退出、退出全部」
 *
 * 删掉 11 项（状态、隐藏气泡、聊天模式、隐藏状态条、免打扰、语音播报、
 * 允许看屏幕、麦克风、思考模式、联网搜索、活泼度）及两条分隔线。
 *
 * ### ✅ 删之前先确认了"功能没丢"
 *
 * ⚠️ 这几项以前被加进托盘的理由都是**同一个**：
 * 「宠物被隐藏时托盘是唯一入口」。所以精简前必须确认宠物右键菜单还留着它们：
 *
 * ```
 * 宠物右键菜单（petMenuTemplate.ts）里仍然有：
 *   沉浸模式 / 聊天模式 / 语音播报 / 允许看屏幕 / 宠物大小
 *   桌宠控制（思考模式、联网搜索、麦克风…）/ 活泼度 / 记忆管理 / 设置 / 退出
 * ```
 *
 * **没有一条功能被删掉，只是不再在托盘里重复一遍。**
 *
 * ### ⚠️ 唯一"必须留"的一项是**显示/隐藏宠物**
 *
 * 宠物窗口是 `skipTaskbar` 的（任务栏里没有按钮）。所以**藏起来之后，
 * 托盘里这一项是叫它回来的唯一途径** —— 删了它就再也点不出来了。
 * 这也是为什么精简后仍保留这一项，而不是连它一起砍成 4 项。
 *
 * ### ⚠️ 精简不等于"少写点代码"，是**删掉一整片状态**
 *
 * 那 11 项每一项都在这个类里带着自己的镜像状态（`bubbleVisible` /
 * `paused` / `thinking` / `webSearch` / …）和一串 `setXxx()` 同步方法。
 * 菜单项删了，这些状态就没用了 —— **留着就是"影子状态"**：
 * 主进程还在每次变更时把它们同步过来，而没有任何东西会读它们。
 *
 * 所以这次是把状态、setter、回调**一起删干净**，而不是只删菜单项。
 *
 * ⚠️ 唯一例外是 `status`：它原来是那个不可点的「状态：…」菜单头，
 * 现在改成**只更新托盘悬停提示**（`setToolTip`）—— 那里显示状态很自然，
 * 又不占菜单宽度。
 */

import { Menu, Tray, nativeImage, type NativeImage } from 'electron';
import { existsSync } from 'node:fs';
import path from 'node:path';
// 大小档位的唯一真源（设置页/托盘/右键菜单三处共用，避免清单漂移）。
// ⚠️ 这里是给 click 处理器把 label 对回档位用的 —— 菜单**渲染**在 trayTemplate.ts。
import { PET_SCALE_PRESETS } from './petLayout.js';
import {
  buildTrayMenuTemplate,
  type TrayActionId,
  type TrayMenuItem,
} from './trayTemplate.js';

/**
 * 托盘只需要这几个回调。
 *
 * ⚠️ 以前这里有 15 个（开关气泡/聊天模式/免打扰/语音播报/看屏幕/麦克风/
 * 思考模式/联网搜索/活泼度/日志/重读配置…）。菜单精简后不再需要 ——
 * 那些开关的入口在宠物右键菜单和设置页，托盘不再重复。
 *
 * ⚠️ 删回调时**同时删掉 index.ts 里对应的传参**。只删一边的话
 * TypeScript 会因为"多传了属性"报错（这正好是好事：它逼两边一致）。
 */
export interface TrayCallbacks {
  /** 显示/隐藏宠物。⚠️ 托盘里**必须**有 —— 宠物藏起来后这是唯一入口 */
  onToggleVisible: () => void;
  /** 打开设置窗口 */
  onOpenSettings: () => void;
  /** 改宠物大小（倍数，1 = 基准尺寸） */
  onSetPetScale: (scale: number) => void;
  onQuit: () => void;
  /**
   * 退出**所有**实例。
   *
   * 宠物不做单实例锁，反复双击会堆出多个实例（实测同时跑过 11 个）。
   * 而一个个关对用户毫无意义 —— 他看得见一堆窗口，要关十几次。
   */
  onQuitAll: () => void;
}

export class PetTray {
  private tray: Tray | null = null;
  private visible = true;
  /**
   * 当前状态文字。
   *
   * ⚠️ 以前它渲染成菜单第一项那个不可点的「状态：在线 · DeepSeek」。
   * 菜单精简时那一项删了，但这个字段**留着有用**：改成喂托盘**悬停提示**。
   * 状态显示在 tooltip 里很自然，又不占菜单宽度。
   */
  private status = '连接中…';
  /** 当前宠物缩放倍数（1 = 基准尺寸） */
  private petScale = 1;

  constructor(private readonly callbacks: TrayCallbacks) {}

  create(): void {
    if (this.tray) return;
    this.tray = new Tray(loadIcon());
    this.tray.setToolTip('桌面宠物 · 右键可退出');
    this.refresh();
    this.tray.on('click', () => this.callbacks.onToggleVisible());
  }

  setPetScale(scale: number): void {
    this.petScale = scale;
    this.refresh();
  }

  /** 更新托盘悬停提示（菜单精简后，状态文字唯一的去处）。 */
  setStatus(status: string): void {
    if (this.status === status) return;
    this.status = status;
    this.refresh();
  }

  setVisible(visible: boolean): void {
    this.visible = visible;
    this.refresh();
  }

  /** 菜单每次重建：菜单项状态（勾选、启用）随运行状态变化，缓存起来反而容易不同步。 */
  private refresh(): void {
    if (!this.tray) return;
    /*
     * 🔴 2026-10-16：菜单**只剩 5 项**（用户要求）。
     *
     * > 「托盘菜单现在只保留显示隐藏宠物、宠物大小、设置、退出、退出全部」
     *
     * ## ⚠️ 为什么不再需要那一堆同步方法了
     *
     * 被删的 11 项每一项都在这个类里带着自己的镜像状态
     * （bubbleVisible / paused / thinking / webSearch / ttsSpeech /
     * visionWatching / statusVisible / chatVisible / micDevices / activity），
     * 而菜单项是**每次 rebuild 时读这些状态**来算勾和文案的。
     *
     * 菜单项没了 -> 没有任何东西读它们 -> 那些字段和 setter 就是影子状态：
     * 主进程还在每次配置变更时勤勤恳恳地同步过来，然后没人看。
     * 所以这次把它们**一起删掉**，而不是留着（留着将来只会让人以为还有用）。
     *
     * ## ⚠️ 模板在 `trayTemplate.ts`，不在这里内联
     *
     * 原来这里是内联的 `Menu.buildFromTemplate([...])` —— **测不了**
     * （测试环境里 `electron` 是 external，拿不到 `Menu`）。
     * 现在模板是纯函数，测试直接喂 state 断言结构；这里只负责
     * 把 action id 映射成真实回调。
     *
     * ## ⚠️ `handlers` 用 `Record<TrayActionId, …>`
     *
     * 这样**漏映射一个 action 会在类型检查时报错**（Record 要求键齐全）。
     * 用 `Partial<Record<…>>` 或普通对象就没这个保护 —— 点了没反应
     * 而这种错在运行时完全静默。
     */
    const handlers: Record<TrayActionId, (item: TrayMenuItem) => void> = {
      'toggle-visible': () => this.callbacks.onToggleVisible(),
      'pet-size': (item) => {
        /*
         * 子菜单项没有 id，所以从**点击的那一项的下标**反推档位。
         *
         * ⚠️ 顺序必须和 `trayTemplate` 生成子菜单的顺序一致 ——
         * 两边都是从 `PET_SCALE_PRESETS` 来的，所以天然一致。
         * 谁要是将来在模板里插一项，这里会立刻错位（而错位的表现是
         * "点 A 档变成了 B 档"，很难查）。所以下面用 label 对回来的
         * 方式更稳：从 label 里认出是哪个 preset。
         */
        const label = String(item.label ?? '');
        const hit = PET_SCALE_PRESETS.find(
          (preset) => label.startsWith(preset.label),
        );
        if (hit) this.callbacks.onSetPetScale(hit.scale);
      },
      settings: () => this.callbacks.onOpenSettings(),
      quit: () => this.callbacks.onQuit(),
      'quit-all': () => this.callbacks.onQuitAll(),
    };

    const attach = (items: TrayMenuItem[]): Electron.MenuItemConstructorOptions[] =>
      items.map((item) => {
        const base: Electron.MenuItemConstructorOptions = { type: item.type };
        if (item.label !== undefined) base.label = item.label;
        if (item.checked !== undefined) base.checked = item.checked;
        if (item.submenu) base.submenu = attach(item.submenu);
        if (item.id) {
          const handler = handlers[item.id];
          base.click = () => handler(item);
        }
        return base;
      });

    const menu = Menu.buildFromTemplate(
      attach(buildTrayMenuTemplate({ visible: this.visible, petScale: this.petScale })),
    );
    this.tray.setContextMenu(menu);
    // ⚠️ 状态不再进菜单（那一项删了），改喂悬停提示 —— 不占菜单宽度
    this.tray.setToolTip(`桌面宠物 · ${this.status}`);
  }

  destroy(): void {
    this.tray?.destroy();
    this.tray = null;
  }
}

/**
 * 托盘图标：**优先用角色素材生成的 .ico**，拿不到才退回代码画的占位图标。
 *
 * 为什么这里值得一个真图标：托盘菜单是用户**唯一**能关掉宠物的入口
 * （窗口是 skipTaskbar，任务栏里没有它的按钮）。而原先那个代码画的
 * 16×16 橙色笑脸跟角色毫无关系，在一堆托盘图标里根本认不出来 ——
 * 实际发生过用户点到隔壁 DeepSeek Harness 的图标上、以为宠物没有托盘菜单。
 *
 * 图标由 `tools/make-tray-icon.py` 从 sheet.png 生成，换立绘素材后重跑一次即可。
 */
function loadIcon(): NativeImage {
  // 打包后主进程在 dist/ 下，图标在 assets/ 下
  const candidates = [
    path.join(__dirname, '..', 'assets', 'tray.ico'),
    path.join(__dirname, '..', '..', 'assets', 'tray.ico'),
  ];
  for (const file of candidates) {
    if (!existsSync(file)) continue;
    const image = nativeImage.createFromPath(file);
    if (!image.isEmpty()) return image;
  }
  // 兜底：没有 .ico 也要能起托盘，否则用户连退出都没法点
  return buildIcon();
}

/**
 * 用代码画一个 16x16 的托盘图标。
 *
 * 形状：一个圆脸 + 两只眼睛（RGBA 直接写像素），避免引入 .ico 资源。
 * 返回的 NativeImage 通过 dataURL 构造，跨平台都可用。
 */
function buildIcon(): NativeImage {
  const size = 16;
  const pixels = Buffer.alloc(size * size * 4);

  const put = (x: number, y: number, r: number, g: number, b: number, a: number) => {
    if (x < 0 || y < 0 || x >= size || y >= size) return;
    const i = (y * size + x) * 4;
    pixels[i] = b;
    pixels[i + 1] = g;
    pixels[i + 2] = r;
    pixels[i + 3] = a;
  };

  const cx = 7.5;
  const cy = 7.5;
  const radius = 6.5;
  for (let y = 0; y < size; y += 1) {
    for (let x = 0; x < size; x += 1) {
      const dx = x - cx;
      const dy = y - cy;
      const dist = Math.sqrt(dx * dx + dy * dy);
      if (dist <= radius) {
        // 边缘一像素做柔化，托盘里看起来不那么毛糙
        const alpha = dist > radius - 1 ? 190 : 255;
        put(x, y, 0xff, 0x9e, 0x3d, alpha);
      }
    }
  }
  // 眼睛
  for (const [ex, ey] of [
    [5, 6],
    [10, 6],
  ]) {
    for (let dy = 0; dy < 2; dy += 1) {
      for (let dx = 0; dx < 2; dx += 1) put(ex + dx, ey + dy, 0x33, 0x22, 0x11, 255);
    }
  }
  // 嘴
  for (let dx = 0; dx < 3; dx += 1) put(6 + dx, 10, 0x33, 0x22, 0x11, 255);

  return nativeImage.createFromBuffer(pixels, { width: size, height: size });
}
