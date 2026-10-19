/**
 * 菜单窗口的 preload（2026-10-14）。
 *
 * ## 最小授权
 *
 * 这个窗口只做三件事：
 *   ① 收菜单数据（主进程推来的 `buildPetMenuTemplate()` 结果）
 *   ② 报点击（`action`）
 *   ③ 报"窗口该摆在哪、多大"（`layout`）以及"我要关"（`close`）
 *
 * ⚠️ 所以它**拿不到**主窗口那套能力（发消息 / 清空记忆 / 开设置 / 拖宠物 /
 * 开麦克风）。菜单是本应用里唯一"由配置直接决定要显示什么"的界面，
 * 把主窗口的接口交给它没有任何必要。
 *
 * ## 通道名为什么在这里也定义一份
 *
 * 与 `status.ts` / `memory.ts` / `settings.ts` 同一条约定：**每个窗口的
 * preload 自带通道表**，不从共享常量 import —— 共享的话加通道时"谁都能用"，
 * 最小授权就形同虚设。
 *
 * ⚠️ 但**主进程侧的字符串必须和这里逐字一致**（这个项目踩过
 * "通道列表与 preload 白名单漂移"的坑，表现是静默失效）。
 */

import { contextBridge, ipcRenderer } from 'electron';

/** 与主进程约定的通道名。改名要**两边一起改**。 */
const CHANNEL = {
  /** 主进程 -> 菜单：一份新菜单（items + 指针位置 + 工作区） */
  show: 'menu:show',
  /** 主进程 -> 菜单：窗口要藏了（清掉当前这份，别留着旧状态） */
  hide: 'menu:hide',
  /** 菜单 -> 主进程：算好的窗口矩形（主进程据此 setBounds） */
  layout: 'menu:layout',
  /** 菜单 -> 主进程：某一行被点了 */
  action: 'menu:action',
  /** 菜单 -> 主进程：请求关闭（Esc / 点完某项 / 失焦） */
  close: 'menu:close',
} as const;

export interface MenuApi {
  onShow(cb: (payload: unknown) => void): void;
  onHidden(cb: () => void): void;
  /**
   * 上报窗口矩形。
   *
   * ⚠️ 用 `send` 而不是 `invoke`：这是单向的高频动作（每次打开一次），
   * 网页侧不需要等结果 —— 而 `invoke` 会为它建一个 Promise 往返。
   */
  layout(rect: { x: number; y: number; width: number; height: number }): void;
  action(payload: { action: string; checked?: boolean }): void;
  close(): void;
}

contextBridge.exposeInMainWorld('petMenu', {
  onShow(cb: (payload: unknown) => void): void {
    ipcRenderer.on(CHANNEL.show, (_event, payload) => cb(payload));
  },

  onHidden(cb: () => void): void {
    ipcRenderer.on(CHANNEL.hide, () => cb());
  },

  layout(rect: { x: number; y: number; width: number; height: number }): void {
    /*
     * ⚠️ 这里挡一道"明显不合法"的矩形（NaN / 0 尺寸 / 巨大）。
     * 主进程也会夹一次，但让**坏的输入在这一层就断掉**比让它在管道里
     * 跑一圈更省事 —— 而一个 NaN 的 setBounds 在 Windows 上会被静默忽略，
     * 表现是"菜单没出来"，没有任何报错。
     */
    if (!rect) return;
    const values = [rect.x, rect.y, rect.width, rect.height].map(Number);
    if (!values.every((v) => Number.isFinite(v))) return;
    if (values[2] < 40 || values[3] < 40) return;
    if (values[2] > 4096 || values[3] > 4096) return;
    ipcRenderer.send(CHANNEL.layout, {
      x: Math.round(values[0]),
      y: Math.round(values[1]),
      width: Math.round(values[2]),
      height: Math.round(values[3]),
    });
  },

  action(payload: { action: string; checked?: boolean }): void {
    if (!payload || typeof payload.action !== 'string' || !payload.action) return;
    ipcRenderer.send(CHANNEL.action, {
      action: payload.action,
      checked: typeof payload.checked === 'boolean' ? payload.checked : undefined,
    });
  },

  close(): void {
    ipcRenderer.send(CHANNEL.close);
  },
} satisfies MenuApi);
