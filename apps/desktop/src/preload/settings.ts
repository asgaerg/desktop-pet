/**
 * 设置窗口的 preload。
 *
 * 与主窗口的 preload 分开，而不是共用一个：两边的能力**不应该一样**。
 * 设置窗口能读写 API 密钥、能改配置、能退出程序；宠物主窗口这些都不需要。
 * 把权限拆开是最小授权原则 —— 主窗口的渲染层即使被注入，也拿不到密钥相关的入口。
 *
 * 保持零依赖、只暴露明确的方法（不放宽到任意 ipcRenderer.invoke）。
 */

import { contextBridge, ipcRenderer } from 'electron';

/** 与主进程约定的通道名，集中在一处避免拼写漂移 */
const CHANNEL = {
  load: 'settings:load',
  test: 'settings:test',
  save: 'settings:save',
  showPet: 'settings:show-pet',
  quit: 'settings:quit',
  quitAll: 'settings:quit-all',
  changed: 'settings:changed',
  peekOffset: 'settings:peek-offset',
  setPeekOffset: 'settings:set-peek-offset',
  /** 文字弹窗（宠物上方的回应气泡）存在时间，默认 10 秒 */
  bubbleLinger: 'settings:bubble-linger',
  setBubbleLinger: 'settings:set-bubble-linger',
  /** 「自言自语」持续多久（2026-10-17，单位：分钟；0 = 不回落） */
  monologueDecay: 'settings:monologue-decay',
  setMonologueDecay: 'settings:set-monologue-decay',

  /** 气泡段间距（px，2026-10-17 加） */
  bubbleParaGap: 'settings:bubble-para-gap',
  setBubbleParaGap: 'settings:set-bubble-para-gap',
  /** 气泡摆放参数（上方/左右间距 + 贴合度，2026-10-18 加） */
  bubbleGapAbove: 'settings:bubble-gap-above',
  setBubbleGapAbove: 'settings:set-bubble-gap-above',
  bubbleGapSide: 'settings:bubble-gap-side',
  setBubbleGapSide: 'settings:set-bubble-gap-side',
  bubbleCoreShrink: 'settings:bubble-core-shrink',
  setBubbleCoreShrink: 'settings:set-bubble-core-shrink',
  edgeSnap: 'settings:edge-snap',
  setEdgeSnap: 'settings:set-edge-snap',
  edgePeek: 'settings:edge-peek',
  setEdgePeek: 'settings:set-edge-peek',
  petScale: 'settings:pet-scale',
  setPetScale: 'settings:set-pet-scale',
  /** 界面主题（浅色 / 深色，2026-10-14 加） */
  theme: 'settings:theme',
  setTheme: 'settings:set-theme',
  /** 列出端点当前可用的模型（`GET /models`） */
  models: 'settings:models',
  /** 读人格提示词（`config/persona.yaml` 的 `persona.system_prompt`） */
  getPrompt: 'settings:get-prompt',
  setPrompt: 'settings:set-prompt',
  /** 截图对话时念出"它看到了什么"（2026-10-17） */
  snapshotDescription: 'settings:snapshot-desc',
  setSnapshotDescription: 'settings:set-snapshot-desc',
  /** 开关「是否启用提示词」 */
  setPromptEnabled: 'settings:set-prompt-enabled',
  /** 记忆设置（最近窗口 + 备份） */
  memoryConfig: 'settings:memory-config',
  setMemoryConfig: 'settings:set-memory-config',
  modules: 'settings:modules',
  setModule: 'settings:set-module',
  /** 打开系统文件夹选择框 */
  chooseFolder: 'settings:choose-folder',
  /** 立刻备份一次 */
  backupNow: 'settings:backup-now',
  /** 提示词模板（要输入给 AI 的各段固定文案） */
  promptTemplates: 'settings:prompt-templates',
  setPromptTemplate: 'settings:set-prompt-template',
  /** 记忆管理（列表 / 珍藏 / 删除 / 改层级） */
  memoryAction: 'settings:memory-action',
  /** 对话历史的用量统计（设置页那行"实时数字"） */
  historyUsage: 'settings:history-usage',
  /** 语音合成诊断探针（在主进程里真跑一次 SAPI） */
  ttsConfig: 'settings:tts-config',
  /** 改语音合成配置（落盘 persona.yaml + 热更新） */
  ttsSet: 'settings:tts-set',
  /** 列出本机 SAPI 音色（**只用于展示** —— 能列出 ≠ 能用） */
  ttsVoices: 'settings:tts-voices',
  /**
   * 列出 edge-tts 的常用中文音色（2026-10-11 加）。
   *
   * ⚠️ 与 `ttsVoices` 的区别：那个是**本机 SAPI** 的（枚举得到 ≠ 能用），
   * 这个是 **edge 的静态清单**（主进程不联网，永远有东西可选）。
   */
  edgeVoices: 'settings:edge-voices',
  /** 快捷键清单（从 `shortcuts.ts` 的 SHORTCUTS 读，唯一真源） */
  shortcuts: 'settings:shortcuts',
  /** 开机自启（2026-10-12）：读状态 */
  loginItem: 'settings:login-item',
  /** 开机自启：设置 */
  setLoginItem: 'settings:set-login-item',
  /** 浏览器兜底（2026-10-13）：读状态（真相源是 config/persona.yaml） */
  browserFallback: 'settings:browser-fallback',
  /** 浏览器兜底：设置（落盘 + 热更新） */
  setBrowserFallback: 'settings:set-browser-fallback',
  /** 宠物那份浏览数据占多少（只读，用于界面显示） */
  browserDataSize: 'settings:browser-data-size',
  /** 清空宠物那份浏览数据（**不碰用户自己的浏览器**） */
  clearBrowserData: 'settings:clear-browser-data',
  /** 用浏览器读一个网址（诊断用；走和真实链路相同的判据） */
  browserProbe: 'settings:browser-probe',
  /** 试听：走真实链路（协议 voice.say -> sidecar 的 VoiceSession.speak()） */
  ttsTest: 'settings:tts-test',
  /** 打开独立的记忆管理窗口 */
  openMemory: 'settings:open-memory',
} as const;

const api = {
  /** 读取当前设置（密钥只返回"有没有配"，绝不返回明文） */
  load(): Promise<unknown> {
    return ipcRenderer.invoke(CHANNEL.load);
  },

  /**
   * 测试连通性。
   *
   * 传进来的字段可以**未保存**：用户应当能"先测通再保存"，
   * 而不是先落盘一个可能是错的密钥。
   */
  test(payload: { baseUrl: string; model: string; apiKey: string }): Promise<unknown> {
    return ipcRenderer.invoke(CHANNEL.test, payload);
  },

  /** 保存。密钥进用户目录的凭据文件，其余进运行期配置。 */
  save(payload: {
    provider: string;
    baseUrl: string;
    model: string;
    apiKey?: string;
    visionEnabled?: boolean;
  }): Promise<unknown> {
    return ipcRenderer.invoke(CHANNEL.save, payload);
  },

  /**
   * 列出端点**当前可用**的模型（用户要求「可以根据 api 传来的可选模型自由选择」）。
   *
   * 传进来的 baseUrl/apiKey 可以**未保存** —— 允许"先拉列表看看有哪些模型，
   * 再决定存哪个"，而不是被迫先落盘一个可能写错的密钥。
   * 拿不到列表（端点没有 `/models`，如自建 llama.cpp）时界面仍允许**手填模型名**。
   */
  models(payload: { baseUrl?: string; apiKey?: string }): Promise<unknown> {
    return ipcRenderer.invoke(CHANNEL.models, payload);
  },

  /** 读人格提示词（返回 `{ prompt, name, path, ok }`） */
  getPrompt(): Promise<unknown> {
    return ipcRenderer.invoke(CHANNEL.getPrompt);
  },

  /**
   * 写人格提示词。
   *
   * 主进程做两件事：落盘到 `config/persona.yaml` + 通知 sidecar 热更新。
   * 返回 `{ ok, message, path }`。
   */
  setPrompt(prompt: string): Promise<unknown> {
    return ipcRenderer.invoke(CHANNEL.setPrompt, { prompt });
  },

  /**
   * 开关「是否启用提示词」。
   *
   * 关掉 = **一个字的提示词都不加**，完全按模型自己的性子说话。
   * 用户要求：「提示词用户可自由选择可选可不选，所以要加个勾选」。
   */
  setPromptEnabled(enabled: boolean): Promise<unknown> {
    return ipcRenderer.invoke(CHANNEL.setPromptEnabled, { enabled });
  },

  /**
   * 记忆设置：最近记忆的时间窗口 + 自动备份。
   *
   * 这些参数以前**只能手改 `config/persona.yaml`** —— 用户要调试"注入多久的 history"，
   * 所以暴露到界面。读回 `{ recentWindowDays, backupEnabled, backupDir, backupKeep, dbPath }`。
   */
  memoryConfig(): Promise<unknown> {
    return ipcRenderer.invoke(CHANNEL.memoryConfig);
  },

  setMemoryConfig(payload: {
    historyDays?: number;
    historyTokenBudget?: number;
    backupEnabled?: boolean;
    backupDir?: string;
    backupKeep?: number;
  }): Promise<unknown> {
    return ipcRenderer.invoke(CHANNEL.setMemoryConfig, payload);
  },

  /** 模块开关状态（阶段二：11 个模块 + 关着时的原因） */
  modules(): Promise<unknown> {
    return ipcRenderer.invoke(CHANNEL.modules);
  },

  /** 试切一个模块（能热切的才允许） */
  setModule(payload: { name: string; enabled: boolean }): Promise<unknown> {
    return ipcRenderer.invoke(CHANNEL.setModule, payload);
  },

  /**
   * 打开系统的"选择文件夹"对话框，返回选中的路径（取消则返回 null）。
   *
   * 为什么必须走主进程：渲染层没有文件系统权限，而 `dialog.showOpenDialog`
   * 是主进程独有的 API。让用户手打路径不现实（Windows 路径又长又容易错）。
   */
  chooseFolder(): Promise<unknown> {
    return ipcRenderer.invoke(CHANNEL.chooseFolder);
  },

  /**
   * 立刻备份一次。
   *
   * ⚠️ 由 **sidecar** 执行，不是主进程复制文件 —— 只有它持有打开的数据库连接，
   * 能用 `VACUUM INTO` 拿到一致快照（WAL 模式下直接复制可能只拿到主库、
   * 丢掉还在 -wal 里的新数据）。
   */
  backupNow(): Promise<unknown> {
    return ipcRenderer.invoke(CHANNEL.backupNow);
  },

  /**
   * 读对话历史的用量统计（设置页那行"实时数字"）。
   *
   * 用户要求（2026-10-08）：「按设置的天数自动搜索数据库取得字数，计算出总
   * token 消耗，再和云端的大模型上下文计算，看总占比」。
   *
   * ⚠️ 这个调用要**半秒**（起一个 Python 进程），所以界面要异步填、
   * 不要阻塞渲染。只在用户改天数后调一次，别放进每帧路径。
   */
  historyUsage(payload: { days?: number }): Promise<unknown> {
    return ipcRenderer.invoke(CHANNEL.historyUsage, payload);
  },

  /**
   * 🔬 诊断：在主进程里直接跑一次 SAPI 合成。
   *
   * 排查宠物不出声时反馈链太长（Python -> 协议 -> 主进程 -> PowerShell -> SAPI），
   * 这个探针把整条链的结果一次返回：argv、退出码、脚本日志、WAV 字节数、本机音色。
   */
  promptTemplates(): Promise<unknown> {
    return ipcRenderer.invoke(CHANNEL.promptTemplates);
  },

  /** 写一条提示词模板（落盘 + 热更新） */
  setPromptTemplate(payload: {
    section: string;
    sub?: string;
    key: string;
    value: string;
  }): Promise<unknown> {
    return ipcRenderer.invoke(CHANNEL.setPromptTemplate, payload);
  },

  /**
   * 记忆管理：一个入口走全部操作。
   *
   * `action` 见 `packages/protocol` 的 `MemoryQueryAction`；`payload` 是各操作
   * 自己的参数。回执形状是 `MemoryResultMsg`。
   *
   * 为什么不做成一堆具名方法（`listCherished()` / `cherish()`…）：
   * 那要在 preload、主进程、协议三处各加一遍，而记忆管理还会持续加操作。
   * 一个通道 + action 让新增操作只改两处（Python 分支 + 界面）。
   */
  memoryAction(action: string, payload?: Record<string, unknown>): Promise<unknown> {
    return ipcRenderer.invoke(CHANNEL.memoryAction, { action, payload: payload ?? {} });
  },

  /** 打开独立的记忆管理窗口（表格界面，比设置页宽） */
  openMemory(): Promise<unknown> {
    return ipcRenderer.invoke(CHANNEL.openMemory);
  },

  showPet(): void {
    ipcRenderer.send(CHANNEL.showPet);
  },

  /**
   * 边缘探头：读 / 写「斜着出去多少像素」。
   *
   * 单独一对方法而不是塞进 `save`：这个参数**改一下就立刻生效**
   * （用户是边拖滑杆边看效果），而 `save` 是"填完一次提交"的语义，
   * 混在一起会让人以为得点保存才生效。
   * 返回值里带"真正生效的值"，因为主进程会把它夹进合法范围。
   */
  peekOffset(): Promise<unknown> {
    return ipcRenderer.invoke(CHANNEL.peekOffset);
  },

  setPeekOffset(value: number): Promise<unknown> {
    return ipcRenderer.invoke(CHANNEL.setPeekOffset, { value });
  },

  /**
   * 文字弹窗存在时间（毫秒）。默认 10 秒。
   *
   * 用户要求：「设置中可以设置文字弹窗的存在时间，默认 10 秒后消失」。
   * 读回 `{ ms, min, max, defaultMs }`，写用 `setBubbleLinger(ms)`。
   */
  bubbleLinger(): Promise<unknown> {
    return ipcRenderer.invoke(CHANNEL.bubbleLinger);
  },

  setBubbleLinger(value: number): Promise<unknown> {
    return ipcRenderer.invoke(CHANNEL.setBubbleLinger, { value });
  },

  /**
   * 「自言自语」持续多久，**单位分钟**（2026-10-17 用户要求，可设 0~180）。
   *
   * 真源是 `config/persona.yaml` 的 `activity.monologue_decay_s`（秒）。
   * ⚠️ 秒/分钟的换算**只在主进程**那两个 IPC 里做 —— 这里和渲染层都只谈分钟。
   * 0 表示**不回落**（会一直念叨），不是"立刻回落到安静"。
   */
  monologueDecayMinutes(): Promise<number> {
    return ipcRenderer.invoke(CHANNEL.monologueDecay);
  },

  /** 改它：**立刻落盘**（不落盘重启就丢）。返回夹取后真正生效的分钟数。 */
  setMonologueDecayMinutes(minutes: number): Promise<{
    minutes: number;
    saved: boolean;
    path?: string;
    error?: string;
  }> {
    return ipcRenderer.invoke(CHANNEL.setMonologueDecay, { minutes });
  },

  /** 气泡段间距（px，0~24）。读回数字，写用 setBubbleParaGap(px)。 */
  bubbleParaGap(): Promise<unknown> {
    return ipcRenderer.invoke(CHANNEL.bubbleParaGap);
  },

  setBubbleParaGap(value: number): Promise<unknown> {
    return ipcRenderer.invoke(CHANNEL.setBubbleParaGap, { value });
  },

  /** 气泡与头顶间距（px，默认 6）。 */
  bubbleGapAbove(): Promise<unknown> {
    return ipcRenderer.invoke(CHANNEL.bubbleGapAbove);
  },

  setBubbleGapAbove(value: number): Promise<unknown> {
    return ipcRenderer.invoke(CHANNEL.setBubbleGapAbove, { value });
  },

  /** 气泡与左右立绘的间距（px，默认 10）。 */
  bubbleGapSide(): Promise<unknown> {
    return ipcRenderer.invoke(CHANNEL.bubbleGapSide);
  },

  setBubbleGapSide(value: number): Promise<unknown> {
    return ipcRenderer.invoke(CHANNEL.setBubbleGapSide, { value });
  },

  /** 立绘核心框收缩比例（%，默认 25，越大贴得越近）。 */
  bubbleCoreShrink(): Promise<unknown> {
    return ipcRenderer.invoke(CHANNEL.bubbleCoreShrink);
  },

  setBubbleCoreShrink(value: number): Promise<unknown> {
    return ipcRenderer.invoke(CHANNEL.setBubbleCoreShrink, { value });
  },

  /** 贴边判定距离：离屏幕边缘多少像素算"贴边"并自动侧身 */
  edgeSnap(): Promise<unknown> {
    return ipcRenderer.invoke(CHANNEL.edgeSnap);
  },

  setEdgeSnap(value: number): Promise<unknown> {
    return ipcRenderer.invoke(CHANNEL.setEdgeSnap, { value });
  },

  /** 边缘探头开关（右键菜单里也有同一个，两处必须同步） */
  edgePeek(): Promise<unknown> {
    return ipcRenderer.invoke(CHANNEL.edgePeek);
  },

  /** 读「截图对话时说一句它看到了什么」（真源：persona.yaml 的 screenshot.show_description） */
  snapshotDescription(): Promise<boolean> {
    return ipcRenderer.invoke(CHANNEL.snapshotDescription);
  },

  /** 改它：**立刻落盘**（不落盘的话重启就丢，见 settings:set-snapshot-desc 的说明） */
  setSnapshotDescription(enabled: boolean): Promise<{
    showDescription: boolean;
    saved: boolean;
    path?: string;
    error?: string;
    ok?: boolean;
  }> {
    return ipcRenderer.invoke(CHANNEL.setSnapshotDescription, { enabled });
  },

  setEdgePeek(enabled: boolean): Promise<unknown> {
    return ipcRenderer.invoke(CHANNEL.setEdgePeek, { enabled });
  },

  /** 宠物大小（右键菜单里也有） */
  petScale(): Promise<unknown> {
    return ipcRenderer.invoke(CHANNEL.petScale);
  },

  setPetScale(value: number): Promise<unknown> {
    return ipcRenderer.invoke(CHANNEL.setPetScale, { value });
  },

  /**
   * 界面主题（2026-10-14 加）。
   *
   * ⚠️ 拿到的值只用来**回显下拉框**。真正换肤不是从这里来的 ——
   * 主进程把 `<html data-theme>` 注入到每个窗口，CSS 变量自己去响应。
   */
  theme(): Promise<unknown> {
    return ipcRenderer.invoke(CHANNEL.theme);
  },

  setTheme(value: 'light' | 'dark'): Promise<unknown> {
    return ipcRenderer.invoke(CHANNEL.setTheme, { value });
  },

  quit(): void {
    ipcRenderer.send(CHANNEL.quit);
  },

  /**
   * 退出**所有**实例。
   *
   * 存在的理由：宠物不做单实例锁，反复双击会堆出多个实例（实测 11 个）。
   * 一个个关对用户毫无意义，而这两者的区别在界面上必须写清楚。
   */
  quitAll(): void {
    ipcRenderer.send(CHANNEL.quitAll);
  },

  /** 主进程主动推送的状态变化（例如 sidecar 回报了新的生效值） */
  onChanged(listener: (payload: unknown) => void): () => void {
    const wrapped = (_event: unknown, payload: unknown) => listener(payload);
    ipcRenderer.on(CHANNEL.changed, wrapped);
    return () => ipcRenderer.off(CHANNEL.changed, wrapped);
  },

  /*
   * ---------------------------------------------------------------- 语音开关
   *
   * ## ⚠️ 为什么设置页**不能自己开麦克风**（2026-10-07）
   *
   * `getUserMedia` 要在有界面的窗口里调，而麦克风的生命周期应当跟**宠物**走
   * —— 设置页关掉了，语音不该跟着停。所以这里只发"我要开"，
   * 真正的 `VoiceIO.start()` 在宠物窗口执行（见 `app.js` 的 `setVoice`）。
   */
  setVoice(enabled: boolean): void {
    ipcRenderer.send('settings:set-voice', Boolean(enabled));
  },

  /** 问一次当前语音状态（设置页每次打开都要问，否则开关显示的是默认值） */
  voiceState(): Promise<unknown> {
    return ipcRenderer.invoke('settings:voice-state');
  },

  /** 订阅语音状态变化（打开成功/失败原因、当前是否在听） */
  onVoiceState(listener: (payload: unknown) => void): () => void {
    const wrapped = (_event: unknown, payload: unknown) => listener(payload);
    ipcRenderer.on('settings:voice-state', wrapped);
    return () => ipcRenderer.off('settings:voice-state', wrapped);
  },

  /*
   * ---------------------------------------------------------------- 按住说话
   *
   * 用户要求：「设置隐私里面设置自定义按键，**按下按键发声，停下结束**」。
   *
   * ⚠️ 按键的捕获**在设置页自己做**（不经过主进程）：设置页聚焦时按下一个键，
   * `KeyboardEvent.keyCode` **就是 Windows 虚拟键码**（Chromium 在 Windows 上
   * 直接用它）。而用户配置按键时设置页必然是聚焦的，所以够用 ——
   * 少一层进程，少一处能出错的地方。
   */
  /** 读按住说话的配置（含"监视器到底在不在跑"） */
  pttConfig(): Promise<unknown> {
    return ipcRenderer.invoke('settings:ptt-config');
  },

  /**
   * 改按住说话的配置。改完主进程会**立刻重启**，不用重启应用。
   *
   * ⚠️ `vk` 和 `globalVk` 是**两个不同的键**（2026-10-10 加 `globalVk`）：
   *
   *     vk        聚焦时按住说（可以是右 Alt 这类修饰键）
   *     globalVk  不聚焦时的全局切换（**必须普通键**，accelerator 表达不了修饰键）
   */
  setPtt(payload: { enabled?: boolean; vk?: number; globalVk?: number }): Promise<unknown> {
    return ipcRenderer.invoke('settings:set-ptt', payload);
  },

  /*
   * ------------------------------------------------------------ 语音识别
   *
   * 用户要求 2026-10-07：「把链接语音处理 api 的一起搞了，用硅基流动的 api，
   * 在设置里做了本地大模型和 api 的切换开关」。
   */
  /** 读当前的语音识别配置（**不含密钥明文**，只回"配没配"与打码形式） */
  asrConfig(): Promise<unknown> {
    return ipcRenderer.invoke('settings:asr-config');
  },

  /*
   * ---------------------------------------------------------------- 文字转语音
   *
   * 用户要求 2026-10-09：「一个是语音识别，一个是文字转语音，这两个标号」。
   */
  /** 读语音合成配置（`providers.tts`） */
  ttsConfig(): Promise<unknown> {
    return ipcRenderer.invoke(CHANNEL.ttsConfig);
  },

  /**
   * 改语音合成配置。落盘到 `persona.yaml` + 热更新，立刻生效。
   *
   * ⚠️ `voice` 的格式**两条路线不一样**：本地是 SAPI 的名字，
   * 云端必须是 `模型名:音色名`。界面上按 provider 给不同提示。
   */
  ttsSet(payload: {
    provider?: string;
    voice?: string;
    rate?: number;
    volume?: number;
  }): Promise<unknown> {
    return ipcRenderer.invoke(CHANNEL.ttsSet, payload);
  },

  /**
   * 列出本机 SAPI 音色。
   *
   * ⚠️ **只能用于展示**，不能用来判断"能不能用"：实测本机
   * `GetInstalledVoices()` 能列出 2 个，但一个都取不到（`Voice.Name` 是空串、
   * `SelectVoice` 抛"没有当前安全设置可用的语音"）。**枚举得到 ≠ 能用。**
   * 真正的可用性只能靠 `ttsTest`。
   */
  ttsVoices(): Promise<unknown> {
    return ipcRenderer.invoke(CHANNEL.ttsVoices);
  },

  /**
   * edge-tts 的中文音色清单（2026-10-11 加）。
   *
   * ## 与 `ttsVoices` 的区别
   *
   *     ttsVoices()   本机 SAPI 音色（**枚举得到 ≠ 能用**，见上面的说明）
   *     edgeVoices()  edge 的音色名清单（**静态表**，主进程不联网）
   *
   * ⚠️ edge 的音色名是**第三套格式**（`zh-CN-XiaoxiaoNeural`）：
   *     云端 openai  -> `模型名:音色名`
   *     本地 windows -> SAPI 名字
   *     edge         -> 裸名字，**不能带前缀**
   */
  edgeVoices(): Promise<unknown> {
    return ipcRenderer.invoke(CHANNEL.edgeVoices);
  },

  /**
   * 读快捷键清单（2026-10-12 加）。
   *
   * ## 为什么要从主进程读，而不是在页面里抄一份
   *
   * 快捷键的**唯一定义源**是 `src/main/shortcuts.ts` 的 `SHORTCUTS`，
   * 注册也从它读。设置页要是自己抄一份文案，就又多了一处会漂移的副本 ——
   * 而"界面写着 Ctrl+Alt+M、实际从没注册过"正是这个项目踩过的真事故
   * （用户按下去毫无反应，也不报错）。
   *
   * 所以这里把 SHORTCUTS 原样送过来，页面只负责显示。
   */
  shortcuts(): Promise<unknown> {
    return ipcRenderer.invoke(CHANNEL.shortcuts);
  },

  /**
   * 读开机自启状态（2026-10-12）。
   *
   * 返回 { enabled, devWarning } —— devWarning 为真时界面要提示
   * 「开发模式下启动的是 Electron 本身，打包后才对」
   * （见主进程 loginItemState 的说明）。
   */
  loginItem(): Promise<unknown> {
    return ipcRenderer.invoke(CHANNEL.loginItem);
  },

  /** 设置开机自启。返回 { ok, enabled, message } —— **要读回来核对过** */
  setLoginItem(enabled: boolean): Promise<unknown> {
    return ipcRenderer.invoke(CHANNEL.setLoginItem, { enabled });
  },

  /**
   * 读浏览器兜底状态（2026-10-13）。
   *
   * 返回 `boolean`。⚠️ 主进程是**直接读 yaml** 的（真相源），
   * 不是查内存 —— 所以在别处改了配置，这里也能读到最新值。
   */
  browserFallback(): Promise<unknown> {
    return ipcRenderer.invoke(CHANNEL.browserFallback);
  },

  /**
   * 设置浏览器兜底。落盘 + 热更新都做了才返回。
   *
   * ⚠️ 打开它会让读不动的网页改用**隐藏浏览器**去读 ——
   * 等于"以用户的身份浏览"，而且每次多等 15~25 秒。
   * 界面上必须解释这两条（见 `settings.html`）。
   */
  setBrowserFallback(enabled: boolean): Promise<unknown> {
    return ipcRenderer.invoke(CHANNEL.setBrowserFallback, { enabled });
  },

  /**
   * 宠物那份浏览数据占多少磁盘（2026-10-13）。
   *
   * 返回 `{ bytes, path, text }` —— `path` 是**真实磁盘位置**，
   * 界面上直接显示出来：说"不会动你的浏览器"不如把路径摆给用户看。
   */
  browserDataSize(): Promise<unknown> {
    return ipcRenderer.invoke(CHANNEL.browserDataSize);
  },

  /**
   * 清空宠物那份浏览数据。
   *
   * ⚠️ **只清 `persist:pet-browser-fallback` 那个 partition**，
   * 用户自己的 Chrome / Edge 一个字都不动（它们存在另外的目录）。
   *
   * 返回 `{ ok, bytesBefore, cleared, message }` —— `cleared` 列出清了哪些
   * 存储类型，界面/日志据实说出来（而不是笼统的"已清空"）。
   */
  clearBrowserData(): Promise<unknown> {
    return ipcRenderer.invoke(CHANNEL.clearBrowserData);
  },

  /**
   * 用浏览器读一个网址（诊断用，2026-10-13）。
   *
   * ⚠️ 走的是**和真实链路相同的判据**：
   *
   * ```
   * 先普通抓取 -> 只有被 WAF 挡住（HTTP 202 / 403）且开关打开时才用浏览器
   * ```
   *
   * 所以它能**如实反映**「浏览器兜底到底能不能用」——
   * 而不是"我直接调浏览器所以一定成功"。
   *
   * ## 为什么需要它
   *
   * 我要确定性地验「兜底 + cookie 计数 + 清空」这条链路。
   * 试过让模型去调 `web_fetch`，但**模型可以选择不调**（实测两次里一次没调）——
   * 那种测试失败时分不清是"功能坏了"还是"模型没配合"。
   */
  browserProbe(url: string): Promise<unknown> {
    return ipcRenderer.invoke(CHANNEL.browserProbe, { url });
  },

  /**
   * 试听。
   *
   * ⚠️ 刻意走**真实链路**（协议 `voice.say` -> sidecar 的
   * `VoiceSession.speak()`），和宠物真实说话是同一个方法。
   * 在主进程直接合成会得出误导性结论（见 `ttsVoices` 的说明）。
   */
  ttsTest(payload: { text?: string }): Promise<unknown> {
    return ipcRenderer.invoke(CHANNEL.ttsTest, payload);
  },

  /**
   * 本地 ASR 模型的下载信息（2026-10-19 用户要求）。
   *
   * 返回 `{ dir, mirrorUrl, officialUrl, repo, present, models, missingDir }`。
   * 用户要"不是每个人都开 VPN"，所以界面必须能显示国内镜像地址。
   */
  asrInfo(): Promise<unknown> {
    return ipcRenderer.invoke('settings:asr-info');
  },

  /** 在资源管理器里打开模型目录（不存在就建出来，免得用户面对"路径不存在"） */
  openAsrDir(): Promise<unknown> {
    return ipcRenderer.invoke('settings:open-asr-dir');
  },

  /** 改语音识别配置。落盘到 persona.yaml + 热更新，立刻生效。 */
  setAsr(payload: {
    mode?: string;
    localModel?: string;
    cloudModel?: string;
    cloudBaseUrl?: string;
    apiKey?: string;
  }): Promise<unknown> {
    return ipcRenderer.invoke('settings:asr-set', payload);
  },

  /*
   * ------------------------------------------------------------ 大模型提供商
   *
   * 用户要求 2026-10-08：「两个都要，要和现有的 deepseek 有互切选项」。
   *
   * ⚠️ 读回来的配置**不含密钥明文**（只有 `keyConfigured` / `keyMasked`）——
   * 密钥一旦进了渲染层就等于在 DevTools 里裸奔。
   */
  /** 读大模型配置（当前厂商、可选模型、密钥是否已配） */
  brainConfig(): Promise<unknown> {
    return ipcRenderer.invoke('settings:brain-config');
  },

  /** 切厂商 / 改模型 / 存密钥。落盘 + 热更新，立刻生效。 */
  setBrain(payload: {
    active?: string;
    model?: string;
    apiKey?: string;
    apiKeyVendor?: string;
  }): Promise<unknown> {
    return ipcRenderer.invoke('settings:brain-set', payload);
  },

  /*
   * ------------------------------------------------------------ 麦克风选择
   *
   * 用户反馈「我用 usb 的麦克风说话好像没用」（2026-10-08）。实测：同一支 USB 麦，
   * 走 Chromium 的 `default` 别名只有 peak 0.0205，指定真实 deviceId 有 0.1413
   * ——**差 7 倍**。所以必须让用户能显式选。
   *
   * ⚠️ 枚举**由宠物窗口执行**（主进程转一手），因为麦克风权限在那个窗口 ——
   * 设置页自己枚举拿到的 label 会是空的。
   */
  /** 请求列出可用麦克风（返回 `{ ok, mics: [{deviceId, label}], message? }`） */
  listMics(): Promise<unknown> {
    return ipcRenderer.invoke('settings:list-mics');
  },

  /** 选定麦克风（空字符串 = 系统默认）。落盘 + 立刻重新开流。 */
  setMic(deviceId: string): void {
    ipcRenderer.send('settings:set-mic', String(deviceId ?? ''));
  },
};

contextBridge.exposeInMainWorld('petSettings', api);

export type PetSettingsApi = typeof api;
