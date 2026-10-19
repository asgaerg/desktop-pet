/**
 * 气泡窗口的 preload —— 第四份，能力最小。
 *
 * 气泡只做一件事：**显示**宠物说的话。它不需要输入、不需要读写设置、
 * 不需要拖拽，所以这里只暴露"订阅消息"。
 *
 * 每多一份能力就多一条越权路径（渲染层被注入时能用它做什么），
 * 所以按窗口给最小权限，而不是共用宠物窗口那份。
 */
import { contextBridge, ipcRenderer } from 'electron';

/** 允许订阅的下行通道（多一个都要写在这里，便于审计） */
const ALLOWED_CHANNELS = new Set([
  // 宠物说的话：say.start / say.delta / say.end / thinking
  'bubble:message',
  // 藏起来（宠物被隐藏、进入全屏趴着等场景）
  'bubble:hide',
]);

const api = {
  /** 订阅气泡消息。返回取消订阅函数。 */
  on(channel: string, listener: (payload: unknown) => void): () => void {
    if (!ALLOWED_CHANNELS.has(channel)) {
      throw new Error(`气泡窗口不允许订阅通道：${channel}`);
    }
    const wrapped = (_event: unknown, payload: unknown) => listener(payload);
    ipcRenderer.on(channel, wrapped);
    return () => ipcRenderer.removeListener(channel, wrapped);
  },

  /**
   * 上报气泡的**实际尺寸** + **窗口该多宽**（布局像素，不含 transform）。
   *
   * 主进程据此调整窗口 —— 否则短文字时窗口仍是固定的 520 宽，
   * 被钳进工作区后气泡会飘到离宠物很远的地方（用户报的「太偏了吧」）。
   *
   * ## ⚠️ 宽度是**渲染层算好的**（`windowWidth`），主进程不再推导
   *
   * 曾经由主进程用渲染层报的"自然宽度"来推导，结果是个**闭环**：
   *
   *     窗口窄 -> `.bubble` 的 max-width 小 -> 内容换行 -> 量出的宽度也小 -> 窗口更窄
   *
   * 实测收敛到最窄（窗口锁死 160 宽，394 字排成 2879px 高的一列 ——
   * 用户报的「高度顶天了都，宽度才几个字」）。
   *
   * 改成单向之后，主进程只对数值做范围保护，不做任何推导 —— 闭环不存在了。
   */
  reportSize(size: { width: number; height: number; windowWidth?: number }): void {
    ipcRenderer.send('bubble:size', size);
  },

  /**
   * 上报"气泡**真的画出来了**"-> 主进程从这一刻开始计存在时长。
   *
   * ## 为什么要这条（2026-10-04）
   *
   * 用户要求：「隐藏的时间不计入气泡持续时间内」。
   *
   * 原来计时在渲染层、起点是"收到 `bubble:message`"，而那一刻窗口可能还没画出来
   * （首次显示要等首帧，实测约 1.5s 的加载宽限）—— 那段时间被算进了显示时长，
   * 气泡实际只显示 8.5 秒。
   *
   * 现在渲染层只负责"什么时候算真的显示出来"，计时器由主进程持有
   * （见 `index.ts` 的 `bubble:shown` 处理与 `BubbleWindow.scheduleHide`）。
   */
  reportShown(): void {
    ipcRenderer.send('bubble:shown');
  },
};

contextBridge.exposeInMainWorld('bubble', api);

export type BubbleApi = typeof api;
