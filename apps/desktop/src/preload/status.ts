/**
 * 状态条窗口的 preload（2026-10-11 用户要求）。
 *
 * ## 最小授权
 *
 * 这个窗口**只该做两件事**：显示状态、上报拖动。所以只开三条通道，
 * 不复用设置窗口那份（那份能改密钥、能退出程序）。
 *
 * ## 通道名为什么在这里也定义一份
 *
 * 与 `memory.ts` / `settings.ts` 同一条约定：**每个窗口的 preload 自带通道表**，
 * 而不是从一个共享常量里 import —— 共享的话，加通道时"谁都能用"，
 * 最小授权就形同虚设。
 *
 * ⚠️ 但**主进程侧的字符串必须和这里逐字一致**。这个项目踩过
 * "DTS 通道列表与 preload 白名单漂移"的坑（表现是静默失效）。
 */

import { contextBridge, ipcRenderer } from 'electron';

/** 与主进程约定的通道名。改名要**两边一起改**。 */
const CHANNEL = {
  /** 主进程 -> 状态条：`{ state, text }` */
  state: 'status:state',
  /** 状态条 -> 主进程：拖动中（只带纵向增量） */
  dragMove: 'status:drag-move',
  /** 状态条 -> 主进程：松手，请吸附到最近的边缘 */
  dragEnd: 'status:drag-end',
} as const;

export interface StatusApi {
  onState(cb: (payload: { state?: string; text?: string }) => void): void;
  dragMove(payload: { deltaY: number }): void;
  dragEnd(payload: { deltaY: number }): void;
}

contextBridge.exposeInMainWorld('petStatus', {
  onState(cb: (payload: { state?: string; text?: string }) => void): void {
    ipcRenderer.on(CHANNEL.state, (_event, payload) => cb(payload ?? {}));
    // 挂上监听后主动要一次当前状态 —— 否则"窗口刚开、状态还没变"时
    // 它会一直显示 HTML 里的初始文案（"空闲"），而真实状态可能是别的。
    ipcRenderer.send('status:hello');
  },

  dragMove(payload: { deltaY: number }): void {
    // ⚠️ 用 `send` 而不是 `invoke`：拖动是高频单向消息，
    // `invoke` 每帧都建一个 Promise 往返，纯属浪费。
    ipcRenderer.send(CHANNEL.dragMove, payload);
  },

  dragEnd(payload: { deltaY: number }): void {
    ipcRenderer.send(CHANNEL.dragEnd, payload);
  },
} satisfies StatusApi);
