/**
 * preload：渲染层与主进程之间唯一的通道。
 *
 * 安全基线：contextIsolation 打开、nodeIntegration 关闭、sandbox 打开。
 * 渲染层拿不到 require / process / fs，只能调用这里显式暴露的方法，
 * 而且要发的消息类型也被限定在协议白名单内 —— 桌宠会显示屏幕内容，
 * 渲染层一旦被注入脚本就能越权读文件，这是必须堵死的口子。
 */

import { contextBridge, ipcRenderer } from 'electron';

/**
 * 收集渲染层的错误，供主进程的截图探针读取。
 *
 * 为什么要这个：渲染层的致命错误（语法错误、模块加载失败）不会出现在主进程日志里，
 * 只在 devtools 控制台可见。上一轮立绘就是因此"静默失效"了整个阶段。
 * 把它挂到 window 上，主进程就能在自检时把真实的报错捞出来。
 */
declare global {
  interface Window {
    __petErrors?: string[];
  }
}

const errorSink: string[] = [];
window.__petErrors = errorSink;
for (const event of ['error', 'unhandledrejection'] as const) {
  window.addEventListener(event, (e) => {
    const detail =
      e instanceof ErrorEvent
        ? `${e.message} @ ${e.filename}:${e.lineno}`
        : `unhandledrejection: ${String((e as PromiseRejectionEvent).reason)}`;
    errorSink.push(detail);
  });
}

/** 只允许渲染层发这几类消息，其余一律丢弃（白名单而非黑名单）。 */
const ALLOWED_TO_BRAIN = new Set(['user.text', 'user.interrupt', 'user.poke']);

const api = {
  /** 发送用户输入。主进程会再校验一次，这里只是第一道闸。 */
  sendUserText(text: string): void {
    if (typeof text !== 'string') return;
    const trimmed = text.trim();
    if (!trimmed || trimmed.length > 8000) return;
    ipcRenderer.send('pet:user-text', trimmed);
  },

  interrupt(): void {
    ipcRenderer.send('pet:interrupt');
  },

  /**
   * 戳一下宠物：跳过意愿评估与冷却，让它立刻主动说一句。
   * 既是"我想听它说话"的功能，也是验证自主链路最直接的手段。
   */
  poke(): void {
    ipcRenderer.send('pet:poke');
  },

  setActivity(value: number): void {
    if (typeof value !== 'number' || !Number.isFinite(value)) return;
    ipcRenderer.send('pet:set-activity', Math.max(0, Math.min(1, value)));
  },

  /**
   * 免打扰。注意它最终落成 sidecar 的 `autonomy.muted` 配置项 ——
   * 真相源只有一处，前端不自己维护一份状态，否则两边会不同步。
   */
  setPaused(paused: boolean): void {
    ipcRenderer.send('pet:set-paused', Boolean(paused));
  },

  /** 开关麦克风采集（语音链路，P3）。 */
  setVoiceEnabled(enabled: boolean): void {
    ipcRenderer.send('pet:voice-enabled', Boolean(enabled));
  },

  /**
   * 推送一帧麦克风 PCM（16kHz 单声道 16bit）。
   * 走独立通道与二进制传输：音频每秒 50 帧，走 JSON + base64 会白涨三分之一体积。
   *
   * ## ⚠️ 这条链路曾经"看着接好了、其实从没通"（2026-10-07 查清）
   *
   * 排查时我一度以为**这个方法不存在** —— 因为 `voice-io.js` 里调它用的是
   * `window.pet?.sendMicAudio?.(...)`，而 `?.` 会把"方法缺失"变成**静默无操作**：
   * 不报错、不打日志，采到的音频全被丢掉。
   *
   * 实际它一直都在（就是下面这个）。真正的缺口在**别处**：
   * 没有任何地方调 `VoiceIO.start()` —— 麦克风从没被打开过。
   *
   * 教训：`?.` 用在这里让"接口没接上"和"接上了但没人调"这两种完全不同的
   * 故障**看起来一模一样**。排查这类问题要**从调用点往回查**，不要假设。
   */
  sendMicAudio(buffer: ArrayBuffer): void {
    if (!(buffer instanceof ArrayBuffer) || buffer.byteLength === 0) return;
    ipcRenderer.send('pet:mic-audio', buffer);
  },

  /**
   * 清空长期记忆。
   * 这是隐私上的底线能力：用户必须能一句话让宠物忘掉听过的一切，
   * 而且要真的从磁盘删掉，不是只在界面上清个显示。
   */
  clearMemory(): void {
    ipcRenderer.send('pet:memory-clear');
  },

  /** 打开设置窗口（API 密钥、隐私开关、退出程序）。 */
  openSettings(): void {
    ipcRenderer.send('pet:open-settings');
  },

  /**
   * 最小化到任务栏。
   *
   * 无边框窗口（`frame: false`）没有系统提供的最小化按钮，必须自己补一个，
   * 否则用户没有任何办法把宠物收起来。
   */
  minimize(): void {
    ipcRenderer.send('pet:minimize');
  },

  /** 弹出宠物本体的右键菜单（原生菜单，结构见 main/petMenuTemplate.ts）。 */
  openContextMenu(): void {
    ipcRenderer.send('pet:context-menu');
  },

  /**
   * 把状态文本转告主进程。
   *
   * 状态最终显示在**对话条窗口**的小圆点上（输入框搬出去之后，
   * 宠物窗口里已经没有地方显示文字了）。所以这里只是转发，
   * 显示由主进程统一收敛后发给对话条 —— 避免两个窗口各推一套状态。
   */
  reportStatus(text: string, level: string): void {
    ipcRenderer.send('pet:status-report', { text, level });
  },

  /** 贴边探头状态变化（主进程据此把对话条换到对面/上方，保证不出屏） */
  reportPeekState(peeking: boolean): void {
    ipcRenderer.send('pet:peek-state', { peeking });
  },

  /**
   * 通知主进程：**旋转动画真正结束了**（`transitionend` 触发）。
   *
   * 主进程靠它来决定"什么时候可以安全地换回常态尺寸" —— 动画中途换会把立绘挪走
   * （见 `petWindow.scheduleNormalResizeAfterPeekExit` 的说明）。
   * 渲染层在调用它之前会先 `reportArtRect()` 补报一次，所以主进程拿到的是
   * **姿态已稳定**的偏移。
   */
  peekTransitionEnd(payload: { angle?: number }): void {
    ipcRenderer.send('pet:peek-transition-end', payload);
  },

  /**
   * 上报**运行时量到的立绘包围盒**（CSS 像素）。
   *
   * 为什么由渲染层报而不是主进程算：立绘在画布里的位置和尺寸只有视频层知道
   * （裁剪、缩放在 `drawKeyed` 里）。早期主进程用一组从截图量的常数
   * （`ART_BOX_RATIO`）推算，布局一改就不准了 —— 表现是留白偏移、
   * 贴边判定跟着偏（用户报的"调完大小留白变了"）。
   * 现在渲染层读一次 canvas 像素量出真实包围盒报上来，缩放/换素材都自适应。
   */
  reportArtBox(width: number, height: number): void {
    ipcRenderer.send('pet:art-box', { width, height });
  },

  /**
   * 上报**立绘在窗口内的矩形**（CSS 像素，轴对齐，含旋转）。
   *
   * 主进程用它做两件事：
   *   1. "指针是否在宠物身上"的命中判定（用 `x/y/width/height`，**含旋转**，
   *      因为探头时立绘是斜的）—— 从而不依赖窗口焦点就能正确切换点击穿透；
   *   2. 换窗口尺寸时的**锚点**（用 `upright*`，**未旋转**的立绘矩形）。
   *
   * ⚠️ 两者**必须分开传**：命中判定要的是旋转后的形状，
   * 而锚点要的是"立绘顶边到窗口顶边的距离"，掺进旋转就会算错
   * （2026-10-03 的"拖拽结束往下跳 43px"就是这个错，见交接文档第七节·0）。
   */
  reportArtRect(rect: {
    x: number;
    y: number;
    width: number;
    height: number;
    /** 未旋转时立绘在窗口内的矩形（给主进程做锚点用） */
    uprightX?: number;
    uprightY?: number;
    uprightWidth?: number;
    uprightHeight?: number;
    /*
     * 立绘左边/上边相对**窗口原点**的偏移。
     *
     * ⚠️ 刻意传"窗口内偏移"而不是"屏幕绝对位置"：主进程换尺寸时要靠它
     * 保住立绘不动，而绝对位置要经过 `window.screenX` 与视口坐标的来回换算，
     * 一旦混进一次屏幕坐标就会把窗口搬走一大截（实测偏移报成 212，
     * 真实约 23 —— 那就是"探头结束闪现"）。
     */
    artOffsetX?: number;
    artOffsetY?: number;
    /** 当前旋转角（度）—— 主进程据此判断"这份几何是不是静止态的" */
    artRotationDeg?: number;
  }): void {
    ipcRenderer.send('pet:art-rect', rect);
  },

  /**
   * 上报**气泡在屏幕上的实测矩形**。
   *
   * 气泡按内容自适应（用户要求"随文字变大，不设固定大小"），宽度事先算不出来，
   * 所以由渲染层量完上报；主进程据此把它钳进工作区（"不要超出屏幕"）。
   */
  reportBubbleRect(rect: { x: number; y: number; width: number; height: number }): void {
    ipcRenderer.send('pet:bubble-rect', rect);
  },

  /**
   * 拖拽窗口。
   *
   * 由渲染层驱动、主进程执行移动 —— **不用 CSS 的 `-webkit-app-region: drag`**：
   * 那个方案依赖 Chromium 的拖拽区命中测试，而窗口在穿透状态下带
   * `WS_EX_TRANSPARENT`，命中测试会被跳过，结果就是"抓不住"。
   *
   * 传的是 `event.screenX/Y`（逻辑像素），与主进程的 getPosition/setPosition 同一坐标系。
   */
  dragStart(screenX: number, screenY: number): void {
    ipcRenderer.send('pet:drag-start', { x: screenX, y: screenY });
  },
  dragMove(screenX: number, screenY: number): void {
    ipcRenderer.send('pet:drag-move', { x: screenX, y: screenY });
  },
  dragEnd(): void {
    ipcRenderer.send('pet:drag-end');
  },

  /**
   * 汇报指针是否位于交互区，主进程据此开关点击穿透。
   * 这是桌宠体验的关键：不做的话，宠物窗口会挡住下面所有应用的操作。
   *
   * 这里是**异步 send**，两条都试过，结论如下：
   *
   *   · `sendSync`（试过，必须撤回）：想把"主进程还没关掉穿透、用户的按下
   *     就已经到了"这个竞态用同步等待消掉。结果**整个点击链路彻底失效** ——
   *     实测日志里连一条"穿透切换"都不再出现。因为同步 IPC 阻塞渲染进程，
   *     而 `setIgnoreMouseEvents(true,{forward:true})` 的鼠标转发恰恰依赖
   *     渲染进程保持响应，一旦阻塞就形成死锁：再也收不到 mousemove。
   *
   *   · 异步 send + **延迟恢复穿透**（现在的做法）：恢复穿透时等 200ms，
   *     把"指针刚离开交互区"和"用户按下"这两件事错开。见 app.js 里的
   *     CLICK_THROUGH_DELAY_MS。
   */
  setInteractive(interactive: boolean): void {
    ipcRenderer.send('pet:interactive', Boolean(interactive));
  },

  getState(): Promise<{
    activity: number;
    paused: boolean;
    status: string;
    clickThrough: boolean;
    logFile: string;
  }> {
    return ipcRenderer.invoke('pet:get-state');
  },

  /**
   * 订阅主进程推送的单向事件，返回取消订阅函数。
   *
   * payload 用 `unknown` 而不是 `never`：`never` 意味着"不可能有值"，
   * 于是包装函数 `(_event, payload) => listener(payload)` 无法赋给
   * `(...args: any[])` 这类签名（any 不能赋给 never），typecheck 会报错。
   * 事件的真实形状由协议类型描述，这里只负责转发，所以 unknown 是诚实的标注。
   */
  on(channel: string, listener: (payload: unknown) => void): () => void {
    if (!ALLOWED_CHANNELS.has(channel)) {
      throw new Error(`不允许订阅的通道：${channel}`);
    }
    const wrapped = (_event: unknown, payload: unknown) => listener(payload);
    ipcRenderer.on(channel, wrapped);
    return () => ipcRenderer.off(channel, wrapped);
  },

  platform: process.platform,

  /**
   * 按住说话的**句子边界**（用户要求：「按下按键发声，停下结束」）。
   *
   * ⚠️ 与 `setVoiceEnabled` 是两件事，别混：
   *   · `setVoiceEnabled` 管"**允不允许听**"（开关麦克风，付权限提示与
   *     AudioContext 创建的开销）
   *   · 这一条管"**这一句从哪到哪**"
   *
   * 按住说话可能一秒按好几次，所以麦克风在整个语音会话期间保持打开，
   * 这条只标边界。`stop` 的意思是"**我说完了**"（不是取消）——
   * 后端据此主动收尾，不等静音判定。
   */
  sendVoicePtt(phase: 'start' | 'stop'): void {
    if (phase !== 'start' && phase !== 'stop') return;
    // 后端协议里叫 `end`（"这句结束了"），前端叫 `stop`（"松开按键"）——
    // 在**这一处**换算，别让两个词渗进各自的层。
    ipcRenderer.send('pet:voice-ptt', { phase: phase === 'stop' ? 'end' : 'start' });
  },

  /**
   * 回报语音开关的**真实结果**（开着没开着、失败原因是权限还是设备）。
   *
   * ⚠️ 为什么需要回报：`getUserMedia` 的失败有三种完全不同的原因
   * （权限被拒 / 没有设备 / 被别的程序占用），而它们对用户的**下一步动作**
   * 完全不同。只在 console 里打一行，用户就只知道"没反应"。
   */
  reportVoiceState(payload: { on: boolean; state: string; detail?: string }): void {
    ipcRenderer.send('pet:voice-state', payload);
  },

  /**
   * 报「一段宠物语音**播完了**」（2026-10-16 加）。
   *
   * 状态条「说话 -> 空闲」的终点信号：voice-io.js 的 `onended` ->
   * 这里 -> 主进程 `pet:voice-played`。没有它，状态条会在语音播完之后
   * 一直挂在"说话/准备中"（原来那条链上根本没有"播完"的事件）。
   */
  notifyVoicePlayed(): void {
    ipcRenderer.send('pet:voice-played');
  },

  /**
   * 报「一段宠物语音**开始播放**」（2026-10-17 加）。
   *
   * 「说话中」的起点信号：渲染层是**真正播音频的地方**，它收到
   * `pet:voice-audio` 要开播时喊一声 —— 比主进程在转发点猜时机可靠
   * （转发点的状态时序会漏，2026-10-17 用户实测：音频在播状态条还挂
   * 「思考中」）。没有这个的话「说话中」永远不亮。
   */
  notifyVoiceSpeaking(): void {
    ipcRenderer.send('pet:voice-speaking');
  },
};

/** 渲染层只能订阅这些通道；拼错通道名会立刻抛错而不是静默收不到消息。 */
const ALLOWED_CHANNELS = new Set([
  'pet:message',
  'pet:status',
  'pet:config',
  'pet:paused',
  'pet:toast',
  // 主进程在窗口失焦时主动复位穿透状态后，用它通知渲染层"你记的状态不准了"。
  // 没有它，渲染层会继续以为自己可交互，从而不再发送状态变化 ——
  // 那正是"只能拖一次"的成因（两边不同步）。
  'pet:interactive-reset',
  // 菜单里改「宠物大小」后，主进程用它通知渲染层把画布也调过去
  'pet:set-pet-size',
  // 检测到别的应用全屏 —— 渲染层收到后隐藏输入卡片，只留宠物本体（"趴着"）
  'pet:compact',
  // 边缘探头（拖到屏幕左/右边缘）—— 渲染层据此把立绘旋转 45°
  'pet:peek',
  // 设置里改了"文字弹窗存在时间"（默认 10 秒）—— 渲染层据此重新计时
  'pet:set-bubble-linger',
  // 探头时把气泡推回屏幕内侧。**独立通道**，不要复用 `pet:peek` ——
  // 那条是姿态消息，复用会把旋转覆盖成 0（立绘被"扶正"，踩过）。
  'pet:bubble-shift',
  // 对话条窗口的聚焦请求（Ctrl+Alt+L / 点击宠物）走的不是这条通道，
  // 这里列出的是**宠物窗口**能收到的下行消息。
  'pet:focus-input',
  // 右键菜单里的「戳一下」——交给渲染层走同一条点击路径（先看免打扰、
  // 再播放点击反应），避免主进程里再实现一遍同样的判定
  'pet:poke-request',
  'pet:activity-local',
  'pet:focus-input',
  // 语音音频的**本体**（二进制，不能塞进 JSON）。除它之外所有下行消息都走
  // pet:message —— 不再出现"通道名等于协议类型名"的混用，那样两边很容易失去同步。
  'pet:voice-audio',
  // 设置页开关语音 -> 主进程转给宠物窗口（麦克风只有那个窗口能开）
  'pet:set-voice',
  // 按住说话的"按下/松开"（主进程的全局键监视器 -> 宠物窗口）
  'pet:ptt',
  /*
   * 「按住说话」是否在配置里开着（主进程 -> 宠物窗口）。
   *
   * 🔴 为什么需要它（2026-10-10 修用户报的 bug「没按键也能听见」）：
   * 闸门（`VoiceSession._capture_enabled`）**默认就是开的**，而前端只在
   * 按键时才发消息、麦克风却开机就预热常开 —— 于是"不按住也一直听"。
   *
   * 前端知道这个开关之后才能算对：
   *
   *     闸门 = 没开按住说话（免提常开） 或 正按着
   */
  'pet:ptt-config',
  // 设置页选定麦克风 -> 主进程转给宠物窗口（**权限与采集都在那个窗口**）
  'pet:set-mic',
]);

contextBridge.exposeInMainWorld('pet', api);

export type PetApi = typeof api;
export { ALLOWED_TO_BRAIN };
