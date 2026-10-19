/**
 * 桌面宠物通信协议 v1（TypeScript 侧）
 *
 * 单一来源：packages/protocol/protocol.json
 * Python 对应实现：apps/brain/protocol.py
 *
 * 约定：所有消息都带 `type` 字段做判别式联合（discriminated union），
 * 这样 TS 的 switch 能自动收窄类型，Python 侧靠 dataclass 反序列化。
 */

export const PROTOCOL_VERSION = 1;

/** 默认 WebSocket 端口；被占用时由 sidecar 向上探测，通过端口文件告知前端 */
export const DEFAULT_PORT = 8931;

export type Emotion =
  | 'neutral'
  | 'happy'
  | 'curious'
  | 'sleepy'
  | 'annoyed'
  | 'excited';

export type Priority = 'low' | 'normal' | 'high';

export type ErrorLevel = 'info' | 'warn' | 'error';

/** 情绪三维度，取值 0~1。驱动立绘表情与自主行为倾向 */
export interface EmotionState {
  energy: number;
  mood: number;
  intimacy: number;
}

/** 活泼度参数组。overall 是 UI 总滑杆，其余为可由它联动的分项 */
export interface ActivityConfig {
  overall: number;
  idle_chatter_rate: number;
  screen_watch_prob: number;
  reaction_latency_ms: number;
  interrupt_prob: number;
  message_length: number;
  emoji_sticker_rate: number;
  quiet_hours: string[];
  focus_guard: boolean;
  /**
   * 「自言自语」档待多久自动回落到「安静」（**秒**，2026-10-17）。
   *
   * 用户可调范围 0~10800（0~3 小时）；**0 = 不回落**（会一直念叨）。
   * 设置页显示的是**分钟**，转换只发生在主进程那两个 IPC 里。
   */
  monologue_decay_s?: number;
}

/* ------------------------------------------------------------------ *
 * Electron -> Python
 * ------------------------------------------------------------------ */

export interface HelloMsg {
  type: 'hello';
  ts?: number;
  protocolVersion: number;
  client?: string;
}

export interface UserTextMsg {
  type: 'user.text';
  ts?: number;
  text: string;
  requestId?: string;
}

/**
 * 自主行为参数（P5）。决定"什么时候自己开口"，与 ActivityConfig（怎么说话）
 * 分工不同。
 */
export interface AutonomyConfig {
  enabled: boolean;
  tick_s: number;
  speak_threshold: number;
  threshold_floor: number;
  threshold_ceiling: number;
  cooldown_after_interaction_s: number;
  cooldown_after_speak_s: number;
  idle_after_s: number;
  idle_full_s: number;
  require_observation: boolean;
  observation_decay_s: number;
  max_idle_tokens: number;
  /** 用户手动免打扰。由托盘与界面按钮切换，走 config.update 热更新。 */
  muted: boolean;
}

/** 调度器的运行状态快照，随 ready 消息下发，用于界面显示"它现在为什么不说"。 */
export interface AutonomyStatus {
  enabled: boolean;
  muted: boolean;
  focus_guard_active: boolean;
  quiet_hours: string[];
  in_quiet_hours: boolean;
  threshold: number;
  spoken_count: number;
  skipped_count: number;
  last_reason: string | null;
  last_desire: number | null;
}

/**
 * 让宠物**读一句话**（设置页的「试听」按钮用）。
 *
 * ## 为什么不让设置页自己合成
 *
 * 「试听」必须走**和宠物真实说话完全相同**的链路，否则测通了也不代表真能出声 ——
 * 这个项目里已经吃过一次亏：本地 SAPI 在主进程里能「枚举」音色却取不到，
 * 直接在主进程测会得出误导性结论。
 *
 * 所以试听 = 发这条消息，由 sidecar 调它自己的 `VoiceSession.speak()`，
 * 合成 + 算口型包络 + 交给前端播放全都一样。
 */
export interface VoiceSayMsg {
  type: 'voice.say';
  text: string;
  ts?: number;
}

export interface UserInterruptMsg {
  type: 'user.interrupt';
  ts?: number;
  requestId?: string;
}

  /**
   * 「截图对话」请求（2026-10-17 用户要求）。
   *
   * 主进程 -> Python：立刻抓一张屏、看懂、把注意力拉到用户身上、顺着画面开口。
   *
   * ⚠️ **不带图片**：抓屏与看图全在 Python 侧，图片从采集到送模型都在
   * sidecar 内存里（不落盘、不过协议）。原因是协议上行有 256KB 总闸门
   * （`parse_incoming` 的 DoS 保护），而一张 768px 的 PNG 就有 90~400KB。
   */
  export interface VisionSnapMsg {
    type: 'vision.snap';
    requestId?: string;
  }

  /**
   * 「截图对话」回执（Python -> 主进程）。
   *
   * ⚠️ 失败也要回，而且要带 `message`：不回的话主进程只能等 60 秒超时，
   * 用户看到的是「看图超时」而不是「被隐私规则挡住了」。
   */
  export interface VisionSnapDoneMsg {
    type: 'vision.snap.done';
    requestId?: string;
    ok: boolean;
    message?: string;
    /** vlm = 视觉模型真看懂了；titles = 只拿到窗口标题（降级） */
    source?: string;
    width?: number;
    height?: number;
    elapsed_ms?: number;
    titles?: string[];
  }

/**
 * 活泼度档位**到期回落**（Python -> 主进程，2026-10-17）。
 *
 * 例：「自言自语」待满 20 分钟 -> 回到「安静」（用户原话：
 * 「情绪发泄完了，开始平静的不想说话了」）。
 *
 * 主进程收到后只做一件事：把内存里的 ctivity 改成 overall。
 * ⚠️ **不写** persona.yaml —— 活泼度是运行时状态（settingsStore 里
 * 刻意不存它），写文件会变成它半夜自己改配置。
 */
export interface ActivityTierMsg {
  type: 'activity.tier';
  ts?: number;
  /** 回落的源档位 key（如 monologue） */
  from?: string;
  fromName?: string;
  /** 目标档位 key（如 quiet） */
  to?: string;
  toName?: string;
  /** 目标档位对应的 activity.overall（0.1 等） */
  overall?: number;
  /** 待了多久才回落（秒） */
  seconds?: number;
}

export interface UserPokeMsg {
  type: 'user.poke';
  ts?: number;
  requestId?: string;
}

export interface ConfigUpdateMsg {
  type: 'config.update';
  ts?: number;
  patch: DeepPartial<AppConfig>;
}

export type ToBrainMsg =
  | HelloMsg
  | UserTextMsg
  | UserInterruptMsg
  | VoiceSayMsg
  | UserPokeMsg
  | VoiceControlMsg
  | VoicePttMsg
  | MemoryClearMsg
  | MemoryBackupMsg
  | MemoryQueryMsg
  | ModulesQueryMsg
  | ModulesSetMsg
  | ConfigUpdateMsg
  /*
   * ⚠️ `tts.result` 也在这边（Electron -> Python）—— 别看名字叫 result
   * 就以为是"下行回执"。
   *
   * 它是**对 `tts.synthesize` 的应答**，而那个请求是 **Python 发起的**
   * （Python 要说话时请求主进程合成），所以应答方向是 Electron -> Python。
   * 与 `memory.backup.done` 那种"主进程发起、Python 回执"正好相反。
   */
  | TtsResultMsg
  /**
   * ⚠️ `web.browser.result` 同理 —— 名字叫 result，实际是
   * **对 Python 发起的 `web.browser.fetch` 的应答**，方向 Electron -> Python。
   */
  | WebBrowserResultMsg
  | VisionSnapMsg;
/* ------------------------------------------------------------------ *
 * Python -> Electron
 * ------------------------------------------------------------------ */

/** 语音链路状态。前端据此显示"在听 / 在想 / 在说"。 */
export type VoiceState = 'idle' | 'listening' | 'hearing' | 'thinking' | 'speaking';

export interface VoiceStatus {
  enabled: boolean;
  state: VoiceState;
  asr: string;
  tts: string;
  utterances: number;
  transcripts: number;
  barge_ins: number;
  noise_floor: number;
  threshold: number;
  /** 语音不可用时的原因（缺密钥、缺依赖…），要能让用户看见 */
  asr_note?: string;
  tts_note?: string;
}

export interface VoiceStateMsg {
  type: 'voice.state';
  ts?: number;
  state: VoiceState;
}

/** 宠物正在播放的语音（音频本体走二进制帧，这条只带元信息） */
/**
 * TTS 合成失败（2026-10-15）。
 *
 * ## 为什么要这条消息
 *
 * 用户报「宠物发不了声音」+「也回复不了信息」，查出来是**两件事叠加**：
 *
 * `
 * ① edge-tts 被微软 403 挡了（合成实际失败）
 * ② 沉浸模式**故意不显示文字** —— 它按「TTS 配置开着」判断有没有语音
 * `
 *
 * **叠加起来才是完全没反应**：文字被主动隐藏，语音又失败了。
 *
 * ⚠️ 根因是主进程**不知道合成失败了** —— 它只能看配置
 * （	ts.enabled: true），于是继续认为有语音。这条消息补上那个信息。
 *
 * ⚠️ 它**不进渲染层**：终点是主进程自己（和 pet.tool 一样），
 * 只用来决定这一条要不要降级显示文字。
 */
export interface VoiceTtsFailedMsg {
  type: 'voice.tts_failed';
  ts?: number;
  /** 失败原因（给人看的，会进日志） */
  reason?: string;
}

export interface VoiceAudioMsg {
  type: 'voice.audio';
  ts?: number;
  format: string;
  bytes: number;
  text?: string;
}

export interface UserVoiceMsg {
  type: 'user.voice';
  ts?: number;
  /** 识别出的文字 */
  text: string;
  final?: boolean;
  duration_s?: number;
  confidence?: number;
  source?: string;
}

/** 麦克风开关（上行）。前端开启采集后才会推音频帧。 */
export interface VoiceControlMsg {
  type: 'voice.control';
  ts?: number;
  enabled: boolean;
}

/**
 * 按住说话（上行）。用户要求：「设置自定义按键，按下按键发声，停下结束」。
 *
 * ## 为什么它和 `voice.control` 是两条消息
 *
 * `voice.control` 管的是"**允不允许听**"（开关麦克风，会付权限提示与
 * AudioContext 创建的开销）；这条管的是"**这一句从哪到哪**"。
 *
 * 按住说话可能一秒按好几次 —— 如果每次都用 `voice.control` 开关麦克风，
 * 光是反复创建/销毁音频上下文就够卡了。所以麦克风在整个语音会话期间
 * **保持打开**，这条只标句子的边界。
 *
 * `phase`：
 *   · `start` —— 通知"开始说了"（界面可以显示"正在听"）。
 *   · `end`   —— **主动收尾**并送去识别。这是关键：平时的断句靠静音判定
 *     （默认 500ms），而按住说话的语义是"松开就说完了" —— 用户说两个字
 *     就松手时句尾可能只有 100ms 静音，静音判定**根本不会触发**。
 */
export interface VoicePttMsg {
  type: 'voice.ptt';
  ts?: number;
  phase: 'start' | 'end';
}

/** 清空长期记忆（上行）。隐私底线能力：用户必须能一键让它忘掉。 */
export interface MemoryClearMsg {
  type: 'memory.clear';
  ts?: number;
}

/**
 * 手动备份记忆库（上行）。
 *
 * 为什么必须由大脑执行、而不是主进程直接复制文件：
 * 只有 Python 侧持有**打开的数据库连接**，用 `VACUUM INTO` 才能拿到一致快照。
 * 在 Electron 侧直接复制可能在 WAL 写入过程中拿到半个库。
 */
export interface MemoryBackupMsg {
  type: 'memory.backup';
  ts?: number;
}

/** 备份结果（下行）。同样因为要 `VACUUM INTO`，只有大脑能给出真实结果。 */
export interface MemoryBackupDoneMsg {
  type: 'memory.backup.done';
  ok: boolean;
  message: string;
  path?: string;
  /**
   * 是不是"启动时自动备份"触发的。
   *
   * 设置页据此决定要不要弹提示 —— 自动备份成功不必打扰用户，
   * 失败才需要让他知道。
   */
  auto?: boolean;
  ts?: number;
}

/** 记忆库状态。前端据此显示"它记住了多少事"，并提供清空入口。 */
export interface MemoryStatus {
  enabled: boolean;
  items: number;
  facts: number;
  utterances: number;
  profile: number;
  rejected_sensitive: number;
  rejected_duplicate: number;
  pruned: number;
  embedder: string;
  dim: number;
  path: string | null;
  injections?: number;
}

/** 记忆已清空的回执 */
export interface MemoryClearedMsg {
  type: 'memory.cleared';
  ts?: number;
  removed: number;
  memory: MemoryStatus;
}

/* ------------------------------------------------------------------ 记忆管理 */

/**
 * 记忆管理界面能发起的操作。
 *
 * ## 为什么是一个联合类型而不是十个消息类型
 *
 * 记忆管理会持续加操作（以后还会有「合并」「导出」「按标签筛选」等）。
 * 每个操作一个消息类型要同步改三处（Python 白名单、这里、`protocol.json`），
 * 加得越多越容易漏 —— 而漏了的表现是「点了没反应」，很难查。
 *
 * 集中成一个 `memory.query` + `action`，新增操作只改这一个联合类型和
 * Python 里的一个分支。
 */
export type MemoryQueryAction =
  | 'list_raw'
  | 'raw_image'
  | 'search_raw'
  /** 取某条原文**上下 N 条**（标记珍藏时看上下文、扩充时用） */
  | 'raw_context'
  | 'list_cherished'
  /** 一段珍藏的完整内容（元信息 + 每一轮），用于复现对话 */
  | 'cherished_detail'
  | 'cherish'
  /** 把某条原文上下 N 条并入已有珍藏 */
  | 'expand_cherished'
  /** 从珍藏里移除某一轮（**不动原文记忆**） */
  | 'remove_cherished_turn'
  | 'update_cherished'
  | 'remove_cherished'
  | 'list_memory'
  | 'remove_memory'
  | 'set_memory_level'
  | 'fixed_list'
  | 'add_fixed'
  | 'remove_fixed'
  | 'stats';

/** 记忆管理的请求（上行）。`payload` 是各 action 自己的参数。 */
/** 一个模块的开关状态（下行 `modules.state` 里的一项）。 */
export interface ModuleState {
  name: string;
  label: string;
  enabled: boolean;
  /** 阶段一能不能真正关掉（false = 只登记，界面上要标"暂不生效"） */
  gate: boolean;
  /** 关着时的原因（人话）；开着是 null */
  reason: string | null;
  note?: string;
}

/** 查询模块状态（上行）。`requestId` 由主进程补，用来等回执。 */
export interface ModulesQueryMsg {
  type: 'modules.query';
  requestId?: string;
  ts?: number;
}

/** 试切一个模块（上行）。落盘由主进程做，这里负责"当场生效"。 */
export interface ModulesSetMsg {
  type: 'modules.set';
  name: string;
  enabled: boolean;
  requestId?: string;
  ts?: number;
}

/** 模块状态回执（下行）。 */
export interface ModulesStateMsg {
  type: 'modules.state';
  ok: boolean;
  requestId: string;
  modules: ModuleState[];
  /** 这次 set 真正执行的副作用（空 = 只是查询） */
  changed?: Record<string, boolean>;
  /** 必须重启才生效的模块名 */
  restartRequired?: string[];
  message?: string;
}

export interface MemoryQueryMsg {
  type: 'memory.query';
  action: MemoryQueryAction;
  payload?: Record<string, unknown>;
  ts?: number;
}

/** 一行原文对话（`raw_log`） */
export interface RawTurn {
  id: number;
  session_id: string;
  role: string;
  text: string;
  kind: string;
  created_at: string;
  ts: number;
}

/** 一段珍藏记忆（`cherished`） */
export interface CherishedEntry {
  id: number;
  title: string;
  text: string;
  session_id: string | null;
  src_from: number | null;
  src_to: number | null;
  /** 是否固定注入（每次对话无条件带上） */
  pinned: boolean;
  created_at: string;
  ts: number;
  /**
   * 这段里有几轮对话。
   *
   * 由 `cherished_turn` 子表数出来 —— 列表里显示"3 轮"比什么都不显示有用：
   * 一眼能看出哪条是单独一句、哪条是一整段对话。
   */
  turn_count?: number;
}

/** 珍藏里的一轮（复现对话用） */
export interface CherishedTurn {
  id: number;
  cherished_id: number;
  /** 段内顺序（0 起） */
  ordinal: number;
  /** user（右侧气泡）/ assistant（左侧气泡）—— 与微信一致 */
  role: string;
  text: string;
  /** **原文当时的时间**（不是收藏时间） */
  created_at: string;
  ts: number;
  /** 来自哪条原文（可空：手动写入的没有） */
  src_raw_id: number | null;
}

/** 一条检索记忆（`memory`）—— 唯一被提炼/检索的一层 */
export interface MemoryEntry {
  id: number;
  summary: string;
  /** fact / habit / persona / phrase / episode */
  kind: string;
  tags: string;
  /** fixed（固定注入）/ recent（按需检索）/ archived */
  level: string;
  importance: number;
  hits: number;
  created_at: string;
}

/** 记忆管理的回执（下行） */
export interface MemoryResultMsg {
  type: 'memory.result';
  ok: boolean;
  /** 原样带回请求的 action，便于前端丢弃过期响应 */
  action: string;
  message?: string;
  /** 列表类操作的返回数据（按 action 决定是哪一种） */
  items?: RawTurn[] | CherishedEntry[] | MemoryEntry[];
  /** `cherished_detail`：这段的元信息 */
  entry?: CherishedEntry;
  /** `cherished_detail`：这段的每一轮（复现对话用） */
  turns?: CherishedTurn[];
  /** 新增珍藏后的 id */
  newId?: number;
  stats?: unknown;
  ts?: number;
}

/*
 * ---------------------------------------------------------------- 语音合成
 *
 * ## ⚠️ 为什么 TTS 要"Python 请求、Node 执行"（2026-10-08 实测发现）
 *
 * 本地语音合成走 Windows SAPI，要起 PowerShell。实测（`tools/probe-*.mjs`）：
 *
 *     shell  -> powershell          ✅ Voice.Name=[Microsoft Huihui Desktop]
 *     Node   -> powershell          ✅ 三种接法全部成功、WAV 60872 字节
 *     Python -> powershell          ❌ Voice.Name=[]（空）、Speak 失败
 *     Node -> Python -> powershell  ❌ 同上
 *
 * **Python 起的 PowerShell 拿不到 SAPI 语音**（报 "No voice installed on the
 * system or none available with the current security setting"），而 Node 起的可以。
 * 生产里 Electron 是父进程 —— 也就是**该由 Node 起**。
 *
 * 所以合成这一步放在主进程，Python 通过这两个消息**请求**它。
 * 形状照抄上面 `memory.query` / `memory.result` 那一对（本项目已有的请求-响应模式）。
 */
export interface TtsSynthesizeMsg {
  type: 'tts.synthesize';
  /** 要合成的文字 */
  text: string;
  /** 音色名（空 = 系统默认） */
  voice?: string;
  /** 语速 -10..10 */
  rate?: number;
  /** 音量 0..100 */
  volume?: number;
  /** 请求序号：Python 用它把响应配回请求（可能同时有多条在飞） */
  seq: number;
  ts?: number;
}

export interface TtsResultMsg {
  type: 'tts.result';
  /** 原样带回请求的序号（Python 据此丢弃过期响应） */
  seq: number;
  ok: boolean;
  message?: string;
  /**
   * 合成好的音频，**base64**（JSON 里传二进制只能这样）。
   *
   * ⚠️ 是**裸 PCM**（16bit 单声道），不是 WAV ——
   * 让主进程剥掉 WAV 头，Python 侧拿到就能直接算包络、直接发前端。
   */
  pcm?: string;
  /** 采样率（当前恒为 16000，但带上它，将来换引擎不用改协议） */
  sampleRate?: number;
  ts?: number;
}

/**
 * 浏览器兜底读网页 —— **请求**（Python -> 主进程），2026-10-13 用户要求。
 *
 * ## 为什么存在
 *
 * 起点中文网全站返回 WAF 挑战：
 *
 * ```
 * HTTP 202
 * set-cookie: x-waf-captcha-referer=
 * 正文只有：<script src="/C2WF946J0/probe.js"></script>
 * ```
 *
 * 那是「执行 `probe.js` → 算出 cookie → 重试」的挑战，**纯 HTTP 客户端过不去**。
 * 而 Chromium 能过 —— 这个项目本来就是 Electron（实测
 * `probe-qidian-browser.py`（历史实测脚本）拿到了 22,161 字正文）。
 *
 * 形状照抄 `tts.synthesize` / `tts.result` 那一对（同样是"Python 请主进程帮忙"）。
 */
export interface WebBrowserFetchMsg {
  type: 'web.browser.fetch';
  /** 要读的网址 */
  url: string;
  /**
   * 原样带回的请求 id。
   *
   * ⚠️ 字段名必须是 **`requestId`（驼峰）** —— 协议里已有的惯例，
   * Python 侧 `parse_incoming` 从这个键取。叫 `reqId` 的话两边对不上，
   * 表现是**每次都超时**（这个坑在 `memory.backup` 上踩过一次）。
   */
  requestId: string;
  /** 超时（毫秒）。不传用主进程默认（25 秒） */
  timeoutMs?: number;
  ts?: number;
}

/**
 * 浏览器兜底读网页 —— **回执**（主进程 -> Python）。
 *
 * ⚠️ **失败也必须回**，而且要带 `reason`。
 *
 * 不回的话 Python 只能等到超时（20 秒）—— 那是纯浪费：
 * "窗口建不起来" / "页面是空的" 本来立刻就能知道。
 * 而含糊的失败会让模型**编造页面内容**（这个项目里栽过一次）。
 */
export interface WebBrowserResultMsg {
  type: 'web.browser.result';
  /** 原样带回请求的 id */
  requestId: string;
  ok: boolean;
  url?: string;
  /** 最终 URL（可能被重定向过） */
  finalUrl?: string;
  title?: string;
  /** 页面正文（已截断） */
  text?: string;
  /**
   * 页面 `<meta>` 里挑出来的键值（`og:*` / `description` / `keywords`）。
   *
   * ⚠️ **这个字段比 `text` 有用得多**：书页的 `og:novel:*` 是站点自己生成的
   * 结构化数据（作者 / 分类 / 状态 / 最新章节），比从正文里正则抠可靠。
   */
  metas?: Record<string, string>;
  /** 失败原因（含糊的失败会让模型编造内容） */
  reason?: string;
  /** 从开始到结束的耗时（排查"为什么这么慢"用） */
  elapsedMs?: number;
  ts?: number;
}

export interface ReadyMsg {
  type: 'ready';
  ts?: number;
  protocolVersion: number;
  brain: string;
  persona: string;
  activity?: ActivityConfig;
  /** 调度器状态快照；老版本 sidecar 可能不带，故为可选 */
  autonomy?: AutonomyStatus;
  /** 语音链路状态；缺省表示这一端不支持语音 */
  voice?: VoiceStatus;
  /** 长期记忆状态；缺省表示未启用 */
  memory?: MemoryStatus;
  /**
   * 看屏幕的**生效状态**（不是配置里的原始值）。
   *
   * 托盘与设置界面的勾选状态以它为准 —— 隐私开关最不能出现的情况是
   * "界面显示关着、实际还在截屏"，所以状态必须来自真正执行采集的那一侧。
   */
  vision?: VisionStatus;
  /**
   * 完整配置快照（**已剔除 api_key_env 等敏感项**，见 Python 侧 `Config.snapshot()`）。
   *
   * 设置窗口靠它显示"当前生效的 base_url / model"。没有它的话，界面只能显示
   * 用户刚填的值，而用户分不清"填进去了"和"真的生效了" —— 这两件事在
   * 配置改了却没送到 sidecar 时会分叉。
   *
   * 形状由 Python 侧的 Config 决定，前端只读其中几项，所以用宽松对象类型。
   */
  config?: Record<string, unknown>;
}

/** 屏幕感知（P4）的运行状态 */
export interface VisionStatus {
  /** 用户意愿：是否允许看屏幕 */
  enabled: boolean;
  /** 采集实现：none / pillow / … */
  provider: string;
  /** 当前是否真的在采集（enabled 为真且未被暂停） */
  watching: boolean;
}

export interface PetThinkingMsg {
  type: 'pet.thinking';
  ts?: number;
  id: string;
  replyTo?: string;
}

export interface PetSayStartMsg {
  type: 'pet.say.start';
  ts?: number;
  id: string;
  emotion?: Emotion;
  priority?: Priority;
  /**
   * 这一句是不是**用户主动要的**（他按了截图对话 / 打了字 / 说了话）。
   *
   * 用来区分"自主开口"：全屏（紧凑）态下默认不弹气泡，因为那时用户不在看桌面；
   * 但用户自己触发的回复必须弹 —— 否则表现就是"我按了截图对话，只有声音没有字"。
   */
  userInitiated?: boolean;
}

export interface PetSayDeltaMsg {
  type: 'pet.say.delta';
  ts?: number;
  id: string;
  text: string;
}

export interface PetSayEndMsg {
  type: 'pet.say.end';
  ts?: number;
  id: string;
  text?: string;
  reason?: 'done' | 'interrupted' | 'error';
}

export interface PetMouthMsg {
  type: 'pet.mouth';
  ts?: number;
  id: string;
  envelope: number[];
}

export interface PetEmotionMsg {
  type: 'pet.emotion';
  ts?: number;
  state: EmotionState;
  /** 注意力（0 = 全在用户身上，1 = 全在自己身上）。2026-10-17 加，主观层用 */
  attention?: { value?: number; band?: string; [key: string]: unknown };
  /**
   * 瞬时表情提示（2026-10-17，截图对话）。
   *
   * state 里的 mood/energy 是**连续量**，表达不出这一刻它在盯着你的屏幕看。
   * 带上这个字段时前端会 sprite.play(expression)（一次性反应，播完回 idle）。
   */
  expression?: 'curious' | 'excited';
}

export interface PetObserveMsg {
  type: 'pet.observe';
  ts?: number;
  scene: string;
  summary?: string;
  freshness?: number;
}

/**
 * 工具执行状态（2026-10-12 用户要求）。
 *
 * 用户原话：「宠物联网搜索的时候状态栏不会变，没有显示联网搜索中」。
 *
 * ⚠️ **`detail` 是给人看的**（搜索词 / 网址），不是给模型看的。
 * 所以 sidecar 那边会截断到 28 字 —— 状态条只有 17px 宽、竖排显示，
 * 长了会溢出。
 *
 * ⚠️ **`phase: 'end'` 一定会来**：sidecar 用 `try/finally` 保证
 * （见 `tools.run_tool`）。少了它状态条会永远停在「搜索中」——
 * 那比不改还糟，因为用户会以为它卡住了。
 */
export interface PetToolMsg {
  type: 'pet.tool';
  ts?: number;
  phase: 'start' | 'end';
  /** 工具名：`web_search` / `web_fetch` */
  tool: string;
  /** 给人看的一行说明（搜索词或网址，已截断） */
  detail?: string;
}

export interface SystemErrorMsg {
  type: 'system.error';
  ts?: number;
  level?: ErrorLevel;
  message: string;
  where?: string;
}

export type ToUiMsg =
  | ReadyMsg
  | PetThinkingMsg
  | PetSayStartMsg
  | PetSayDeltaMsg
  | PetSayEndMsg
  | PetMouthMsg
  | PetEmotionMsg
  | PetObserveMsg
  // 工具执行状态（2026-10-12）：侧车发给主进程，用来把状态条切成「搜索中」
  | PetToolMsg
  | SystemErrorMsg
  // 语音链路。这几个 interface 早就写好了，但**一直没进这个联合类型** ——
  // 于是 TS 认为 `case 'voice.audio'` 不可能发生（死分支），
  // 而 AppConfig 那边的 config.update 也因此对不上。补上是必须的。
  | VoiceStateMsg
  | VoiceAudioMsg
  | UserVoiceMsg
  | MemoryClearedMsg
  | MemoryBackupDoneMsg
  | MemoryResultMsg
  | ModulesStateMsg
  /*
   * ⚠️ `tts.synthesize` 在这边（Python -> Electron）。
   *
   * 它是 **Python 发起的请求**（"帮我合成这句话"），由主进程执行 ——
   * 因为本地 SAPI 合成只有 Node 起的 PowerShell 能用（见 `ttsLocal.ts`）。
   * 应答 `tts.result` 在对面那个联合类型里。
   */
  | TtsSynthesizeMsg
  // 设置页试听：Python -> Electron 的请求，由**主进程**转给 sidecar（不进渲染层）
  | VoiceSayMsg
  // 浏览器兜底：Python -> Electron 的请求（同上，读网页只有主进程做得到）
  | WebBrowserFetchMsg
  /*
   * TTS 合成失败（Python -> 主进程，2026-10-15）。
   *
   * ⚠️ 放这里（`ToUiMsg` 那个方向）是刻意的，**不进 `TO_UI_TYPES`**：
   * 它的终点是**主进程自己**（用来把 `ttsHealthy` 置 false），
   * 渲染层不认识也不需要它。
   *
   * ⚠️ 这和 `tts.synthesize` / `web.browser.fetch` 是同一类
   * （"Python 发起、主进程消费"）—— 那一类**不走 `parseMessage` 的
   * 渲染层闸门**，所以不进白名单。协议检查脚本按 `direction: toMain`
   * 分桶，两边能对上。
   */
  | VoiceTtsFailedMsg
  | VisionSnapDoneMsg
  | ActivityTierMsg;

export type AnyMsg = ToBrainMsg | ToUiMsg;

/* ------------------------------------------------------------------ *
 * 配置（config/persona.yaml + config/providers.yaml 的 TS 映射）
 * ------------------------------------------------------------------ */

export type DeepPartial<T> = {
  [K in keyof T]?: T[K] extends object ? DeepPartial<T[K]> : T[K];
};

export interface PersonaConfig {
  name: string;
  /**
   * 是否启用人格设定（用户要求：「提示词用户可自由选择可选可不选」）。
   *
   * 关掉时 Python 侧**一个字的提示词都不加**（`system_prompt` / `speech_style` /
   * `idle_topics` 全部跳过），只剩运行时状态（活泼度、情绪）。
   *
   * ⚠️ 可选：老配置文件没有这个字段，**缺省视为开启** —— 不能因为读不到就
   * 悄悄把用户的人设关掉。
   */
  system_prompt_enabled?: boolean;
  /** 系统提示词，决定人格底色 */
  system_prompt: string;
  /** 称呼用户的方式 */
  user_title: string;
  /** 说话风格补充约束 */
  speech_style: string[];
  /** 主动发言时可用的口吻示例，作为 few-shot 引导 */
  idle_topics: string[];
}

/**
 * 提供者配置。
 *
 * 为什么之前没有这个类型、现在必须有：界面上的"开关看屏幕"以及"切换大脑 API"
 * 都是通过 `config.update` 改 `providers.*` 的。类型里没有这一段，
 * `DeepPartial<AppConfig>` 就会拒绝 `{ providers: ... }` —— 而这个错误在
 * esbuild 转译时**不会报**（它不做类型检查），只会在运行时表现为"开关没反应"。
 *
 * 每个子段用宽松的可选字段：这些值最终由 Python 侧的 `_validate_*` 负责校验，
 * 前端只需要能把意图表达出来。
 */
export interface ProviderSection {
  provider?: string;
  model?: string;
  base_url?: string;
  api_key_env?: string;
  timeout_s?: number;
  [key: string]: unknown;
}

export interface ProvidersConfig {
  brain?: ProviderSection;
  asr?: ProviderSection;
  tts?: ProviderSection;
  vision?: ProviderSection;
  [key: string]: unknown;
}

/**
 * 记忆配置（`config/persona.yaml` 的 `memory:` 段）。
 *
 * ⚠️ 这个 interface **以前不存在** —— `AppConfig` 里根本没有 `memory`，
 * 所以从设置页发 `config.update { memory: ... }` 会被 TS 拒绝（类型对不上），
 * 只能靠 `as never` 之类的强转绕过。补上它让"改记忆配置"这条路有类型保障。
 *
 * 只列出**前端会改**的字段；Python 侧还有 max_items / min_score / top_k 等，
 * 那些目前只从文件读，不通过热更新下发（改了要重启，属于"配置文件级"参数）。
 */
export interface MemoryConfig {
  enabled?: boolean;
  /** 数据库落点。`auto` = 项目内 apps/desktop/pet-data/memory/pet.db */
  path?: string;
  /**
   * 最近记忆的**时间窗口**（天）。
   *
   * 用户要求：「平常聊天的时候 ai 只完全注入 1 个月内的 history 记忆」，
   * 默认一个月（他要调试），可调到最高 1 年。
   */
  recent_window_days?: number;
  recent_window_min_days?: number;
  recent_window_max_days?: number;
  /** 每次注入最近记忆的条数上限（防止窗口设成 1 年时撑爆提示词） */
  recent_inject_limit?: number;
  /** 自动备份（启动时执行一次 `VACUUM INTO`） */
  backup?: {
    enabled?: boolean;
    dir?: string;
    keep?: number;
  };
}

export interface AppConfig {
  persona: PersonaConfig;
  activity: ActivityConfig;
  autonomy: AutonomyConfig;
  providers: ProvidersConfig;
  memory?: MemoryConfig;
  /**
   * 「截图对话」配置（config/persona.yaml 的 screenshot: 段，2026-10-17）。
   *
   * ⚠️ 和 providers.vision **是两套东西**：
   *     vision      宠物自己定期看屏幕（默认关，隐私敏感）
   *     screenshot  用户按下 Ctrl+Alt+S 之后那一次的抓屏 + 看懂
   *
   * 只列出前端会改的字段：show_description 是设置页那个开关
   * （「截图对话时说一句它看到了什么」）。其余（模型/尺寸/超时）
   * 属于配置文件级参数，不走热更新。
   */
  screenshot?: {
    enabled?: boolean;
    show_description?: boolean;
    [key: string]: unknown;
  };
  /**
   * 工具（function calling，2026-10-12）。
   *
   * 用户要求「联网功能默认关闭，在**菜单里面**打开之后……」，所以这个开关
   * 会经由 `config.update` 热更新到 sidecar（`patch: { tools: {...} }`）。
   *
   * ⚠️ **不加进这个接口，那条 patch 会被类型系统拒**：
   * `DeepPartial<AppConfig>` 只认这里声明过的键，报错是
   * 「'tools' does not exist in type 'DeepPartial<AppConfig>'」——
   * 看起来像"协议不支持这个功能"，实际只是这里漏了一行。
   */
  tools?: ToolsConfig;
  /**
   * 模块开关（2026-10-18，模块化阶段二）。
   *
   * `modules.<name>.enabled` —— 与 `apps/brain/pet/modules.py` 的 `MODULES` 同名。
   * ⚠️ 不加进这个接口，主进程推的 `config.update` 补丁会被类型系统拒
   * （`DeepPartial<AppConfig>` 只认声明过的键），报错看起来像"协议不支持"。
   */
  modules?: {
    [name: string]: { enabled?: boolean } | boolean | undefined;
  };
}

/**
 * 工具开关（2026-10-12）。
 *
 * 字段与 `apps/brain/pet/gateway.py` 里 `_tools_enabled()` / `_web_enabled()`
 * 读的键**必须一致** —— 不一致的表现是"界面上的勾打上了，宠物却不用工具"，
 * 而两边都不会报错。
 */
export interface ToolsConfig {
  /** 工具总开关。关掉 = 模型一个工具都看不到（完全回到纯聊天） */
  enabled?: boolean;
  /**
   * 联网搜索。**默认 false**。
   *
   * 打开后它想查的东西会发到搜索引擎 —— 与「本地 ASR 语音不出本机」
   * 是同一类隐私取舍，所以默认必须是保守的那一边。
   *
   * 🔴 2026-10-16：这是 `web_search_mode` 的**布尔影子**（= mode 不是 off），
   * 由主进程在切模式时一起写。兼容老配置的换算见 gateway `_web_search_mode`。
   */
  web_search?: boolean;
  /**
   * 联网搜索模式（2026-10-16 用户要求）：三选一的真源。
   *
   *   accurate  精准搜索（free-search 多引擎，约 10~20 秒）
   *   fast      快速搜索（websearch-mcp，约 2 秒）
   *   off       关闭联网搜索
   *
   * 缺键时按 `web_search` 换算（false->off / true->fast），见 gateway。
   */
  web_search_mode?: string;
  /** free-search-mcp（精准搜索后端）的端点。 */
  free_search_url?: string;
  /** free-search-mcp 的整条启动命令（端点不通时 sidecar 按它拉起）。 */
  free_search_cmd?: string;
  /**
   * 浏览器兜底（2026-10-13 用户要求）。**默认 false**。
   *
   * ## 它做什么
   *
   * 普通 HTTP 抓取读不了的页面（最典型是**起点中文网**那种 WAF 挑战：
   * `x-waf-captcha-referer` + `probe.js`），改用主进程的**隐藏 Chromium 窗口**
   * 去读 —— 真浏览器能执行那段挑战脚本，于是拿得到正文。
   *
   * ## ⚠️ 为什么默认关，而且必须由用户显式打开
   *
   * 走浏览器**等于"以你的身份浏览"**：
   *
   *   · 带真实的 `User-Agent`、真实的 TLS 指纹
   *   · 会在对方站上留下 cookie（持久 partition，见 `browserFetch.ts`）
   *   · 对方看到的是一个正常浏览器会话，而不是匿名抓取
   *
   * 这三条都比匿名 HTTP 抓取的暴露面大。**默认开着等于替用户做决定**，
   * 所以默认必须是保守的那一边。
   *
   * ## ⚠️ 它慢，而且慢是本质的
   *
   * 实测一次 **14~23 秒**（建窗口 + 加载 + 等 WAF 挑战跑完 + 读正文）。
   * 那几秒是**挑战本身的耗时**，不是实现问题 —— 想更快就只能不过 WAF。
   */
  browser_fallback?: boolean;
}

/* ------------------------------------------------------------------ *
 * 运行时校验：不信任来自另一进程的任何输入
 * ------------------------------------------------------------------ */

const TO_UI_TYPES = new Set<string>([
  'ready',
  'pet.thinking',
  'pet.say.start',
  'pet.say.delta',
  'pet.say.end',
  'pet.mouth',
  'pet.emotion',
  'pet.observe',
  /*
   * 工具执行状态（2026-10-12 用户要求）。
   *
   * 用户原话：「宠物联网搜索的时候状态栏不会变，没有显示联网搜索中」。
   *
   * payload:
   *   { phase: 'start' | 'end', tool: 'web_search' | 'web_fetch',
   *     detail: '银松镇 游戏' }     // 搜索词 / 网址，给人看的
   *
   * ⚠️ **只有联网那两个工具会发**。沙箱工具是本地读写、毫秒级完成，
   * 状态条闪一下反而像故障。所以由 sidecar 那边决定发不发（见 `tools.py`）。
   */
  'pet.tool',
  'system.error',
  // 语音链路（P3）
  'voice.state',
  'voice.audio',
  'user.voice',
  // 记忆（P2）
  'memory.cleared',
  'memory.backup.done',
  // 记忆管理界面（设置页的「记忆」「珍藏」标签）
  'memory.result',
  // 模块开关回执（2026-10-18，模块化阶段二）。
  // ⚠️ 这个集合是**运行时白名单**：漏了就是"大脑回了、主进程自己丢掉"，
  // 日志是一句"丢弃无法识别的消息" + "请求超时"，看起来像大脑没响应。
  'modules.state',
  // 语音合成请求：**Python 发给主进程的**（主进程起 PowerShell 执行，
  // 见 ttsLocal.ts）。它不转发给任何渲染层 —— 但仍然要在这里，
  // 因为这个集合是「上行消息的运行时白名单」，缺了会被静默丢弃。
  'tts.synthesize',
  // 设置页试听：让宠物读一句话（走真实合成链路）
  'voice.say',
  /*
   * 浏览器兜底读网页（2026-10-13 用户要求）。
   *
   * ## 方向：**Python -> 主进程**
   *
   * 和 `tts.synthesize` 一样 —— Python 做不到的事由主进程做，
   * 结果用另一条消息回去。这里也有**两条**：
   *
   *   web.browser.fetch    请求（带 requestId）
   *   web.browser.result   回执（原样带回 requestId）
   *
   * ## 为什么需要它
   *
   * 起点全站返回 WAF 挑战（`x-waf-captcha-referer` + `probe.js`），
   * 纯 HTTP 客户端过不去，**但 Chromium 可以** —— 而这个项目本来就是 Electron。
   *
   * ⚠️ **默认关闭**（`web.browser_fallback`）：走浏览器意味着带 UA、
   * 留 cookie，比匿名抓取的暴露面大，必须由用户显式同意。
   */
  'web.browser.fetch',
  'web.browser.result',
  /*
   * 「截图对话」回执（2026-10-17，Python -> 主进程）。
   *
   * ⚠️ **必须在这个集合里** —— 即使主进程自己消费它、渲染层也不认识它。
   * 这个集合是 `parseMessage()` 唯一的解析闸门，缺了它整条消息会被
   * `petSocket.ts` 记一句「丢弃无法识别的消息」然后扔掉，而
   * `requestFromBrain()` 只能**等满超时**。
   *
   * 实测症状（2026-10-17）：后端 2.6 秒就看懂了图并发出回执，日志里却是
   * 「丢弃无法识别的消息：{...vision.snap.done...}」；60 秒后主进程报
   * 「没收到回执（超时）」，界面弹「看图超时了」——
   * 一个**纯粹由解析白名单漏登记**造成的假故障。
   */
  'vision.snap.done',
  /** 活泼度档位回落（2026-10-17）：主进程接住，不再转发给渲染层 */
  'activity.tier',
]);

/**
 * 把一行 WebSocket 文本解析成消息对象；无法识别时返回 null。
 * 前端永远不因一条坏消息崩溃。
 */
export function parseMessage(raw: string): AnyMsg | null {
  let obj: unknown;
  try {
    obj = JSON.parse(raw);
  } catch {
    return null;
  }
  if (typeof obj !== 'object' || obj === null) return null;
  const type = (obj as { type?: unknown }).type;
  if (typeof type !== 'string' || !TO_UI_TYPES.has(type)) return null;
  return obj as AnyMsg;
}

/** 该消息是否应由前端消费（而非回声） */
export function isToUi(msg: AnyMsg): msg is ToUiMsg {
  return TO_UI_TYPES.has(msg.type);
}

/** 补一个单调递增的发言 id，避免前端自己拼 */
let saySeq = 0;
export function nextSayId(): string {
  saySeq += 1;
  return `m${saySeq}-${Date.now().toString(36)}`;
}
