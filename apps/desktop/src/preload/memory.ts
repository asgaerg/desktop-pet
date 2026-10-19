/**
 * 记忆管理窗口的 preload。
 *
 * ---- 最小授权 ----
 *
 * 这个窗口**只需要记忆相关的通道**。不复用设置窗口那份 preload，是因为那份
 * 还暴露了 `setPrompt`（改人格提示词）、`writeCredentials`（写 API 密钥）、
 * `quitAll`（退出程序）—— 把能改密钥、能退出的接口交给一个只该读写记忆的
 * 页面，与项目一贯的做法相反（已有 4 份 preload 的理由都在这）。
 *
 * ---- 为什么只有一个 `action` 通道 ----
 *
 * 记忆管理会持续加操作（以后还有合并、导出、按标签筛选）。每个操作一条
 * IPC 通道，就要在 preload、主进程、协议三处各加一遍 —— 加得越多越容易漏，
 * 而漏了的表现是「点了没反应」，很难查。
 *
 * 一个 `memoryAction(action, payload)` 走天下，新增操作只改两处
 * （Python 的 action 分支 + 界面）。
 */

import { contextBridge, ipcRenderer } from 'electron';

/** 与主进程约定的通道名。改名要两边一起改。 */
const CHANNEL = {
  /** 记忆管理：`{ action, payload }` -> `MemoryResultMsg` */
  memoryAction: 'settings:memory-action',
  /** 语音朗读（Windows SAPI，走主进程） */
  speak: 'settings:speak',
  stopSpeak: 'settings:stop-speak',
} as const;

export interface MemoryApi {
  memoryAction(action: string, payload?: Record<string, unknown>): Promise<unknown>;
  speak(text: string, rate?: number): Promise<unknown>;
  stopSpeak(): Promise<unknown>;
}

contextBridge.exposeInMainWorld('petMemory', {
  memoryAction(action: string, payload?: Record<string, unknown>): Promise<unknown> {
    return ipcRenderer.invoke(CHANNEL.memoryAction, { action, payload: payload ?? {} });
  },

  /**
   * 朗读一段文字。
   *
   * ⚠️ 走**主进程的 Windows SAPI**，不是渲染层的 `speechSynthesis` ——
   * 后者在 Electron 里是空壳（实测 `getVoices()` 返回 0 个、`speak()` 无声）。
   * 详见主进程 `speakText` 的说明。
   */
  speak(text: string, rate = 0): Promise<unknown> {
    return ipcRenderer.invoke(CHANNEL.speak, { text, rate });
  },

  stopSpeak(): Promise<unknown> {
    return ipcRenderer.invoke(CHANNEL.stopSpeak);
  },
} satisfies MemoryApi);
