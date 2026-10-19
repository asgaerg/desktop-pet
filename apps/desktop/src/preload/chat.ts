/**
 * 对话条窗口的 preload。
 *
 * 与宠物窗口的 preload 分开（最小授权）：这一侧只需要"能发文字、能收状态"，
 * 不需要拖拽、穿透、设置这些能力。拆开之后即使这个窗口被注入，
 * 也拿不到那些入口。
 *
 * 与设置窗口的 preload 也分开：那边要读写密钥，权限更高。
 */

import { contextBridge, ipcRenderer } from 'electron';

/** 允许订阅的下行通道（白名单，防止渲染层监听任意通道） */
const ALLOWED_CHANNELS = new Set<string>([
  'pet:message',
  'chat:focus-input',
  'chat:status',
]);

const api = {
  /** 发一条用户输入 */
  sendText(text: string): void {
    ipcRenderer.send('chat:user-text', { text });
  },

  /** 订阅下行消息，返回取消订阅函数 */
  on(channel: string, listener: (payload: unknown) => void): () => void {
    if (!ALLOWED_CHANNELS.has(channel)) return () => {};
    const wrapped = (_event: unknown, payload: unknown) => listener(payload);
    ipcRenderer.on(channel, wrapped);
    return () => ipcRenderer.removeListener(channel, wrapped);
  },
};

contextBridge.exposeInMainWorld('pet', api);
