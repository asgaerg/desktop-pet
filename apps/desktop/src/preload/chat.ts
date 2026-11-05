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

  /**
   * 告诉主进程"鼠标进到对话条窗口里了"（2026-11-05）。
   *
   * ## 为什么需要这条
   *
   * 对话条闲置 2 分钟后会滑出屏幕，只留左边 22px 的抓手。
   * 而窗口是 `showInactive` 开的 —— 鼠标移上去**不会触发 focus**，
   * 得真点一下才行。没有这条的话，用户看到一个竖条、点了才回来。
   *
   * ⚠️ 只传一个信号，不带任何数据：主进程只关心"要不要滑回"，
   *    而这个判断完全在它那边（只在滑出去状态下才理）。
   */
  notifyActivity(): void {
    ipcRenderer.send('chat:pointer-enter');
  },

  /**
   * 发一张图片（2026-11-02 用户要求「可以用图片和用户聊天」）。
   *
   * ## 为什么传 base64 而不是文件路径
   *
   * 三条输入路径都要能用：选文件、Ctrl+V 粘贴、拖拽。
   * 其中**粘贴**根本没有路径（图片在剪贴板里，`File.path` 在 Electron
   * 新版里也已经不给了），所以统一在渲染层读成 dataURL 再传。
   *
   * ## 上限在这里先挡一道
   *
   * 主进程和 Python 那边都有上限（10 MB），但在这里先挡能给出**即时**
   * 反馈，而不是让用户等一次往返才知道太大。
   */
  sendImage(payload: { dataUrl: string; name?: string; text?: string }): void {
    const url = String((payload && payload.dataUrl) || '');
    // 只接受图片 dataURL —— 防止被注入一段任意 data: 走这条路
    if (!url.startsWith('data:image/')) return;
    ipcRenderer.send('chat:user-image', {
      dataUrl: url,
      name: String((payload && payload.name) || ''),
      text: String((payload && payload.text) || ''),
    });
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
