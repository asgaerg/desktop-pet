/**
 * 设置窗口的前端逻辑。
 *
 * 这个文件**不能写 TypeScript 语法** —— 它和 `app.js` 一样是直接喂给 Chromium 的
 * 普通 ESM（渲染层只有 `sprite.ts` 走 esbuild）。工具链有门禁检查这一点。
 *
 * ---- 布局 ----
 *
 * 左侧分类 + 右侧面板（照参考桌宠应用的设置窗口）。切换分类走本地 DOM，
 * **不重新读设置** —— 切个标签就重新拉一遍状态会闪，而且没必要。
 */

const el = (id) => document.getElementById(id);
const ui = {
  // 分类导航
  nav: el('nav'),
  // AI
  brainVendor: el('brain-vendor'),
  brainMode: el('brain-mode'),
  brainModeHint: el('brain-mode-hint'),
  brainVendorState: el('brain-vendor-state'),
  provider: el('provider'),
  baseUrl: el('base-url'),
  /** 模型**下拉框**（选项来自 `GET /models`，用户要求"按 api 传来的可选模型自由选择"） */
  model: el('model'),
  /** 手填模型名（端点没有 `/models` 时的兜底） */
  modelManual: el('model-manual'),
  refreshModels: el('refresh-models'),
  modelsState: el('models-state'),
  apiKey: el('api-key'),
  keyState: el('key-state'),
  test: el('test'),
  save: el('save'),
  result: el('result'),
  /** 人格提示词 */
  personaPrompt: el('persona-prompt'),
  promptEnabled: el('prompt-enabled'),
  promptState: el('prompt-state'),
  reloadPrompt: el('reload-prompt'),
  savePrompt: el('save-prompt'),
  /** 提示词模板（动态渲染的字段容器） */
  promptFields: el('prompt-fields'),
  promptTplState: el('prompt-tpl-state'),
  /** 打开独立的记忆管理窗口 */
  openMemory: el('open-memory'),
  openMemoryFromPrompts: el('open-memory-from-prompts'),
  /** 记忆（最近窗口 + 备份） */
  modulesList: el('modules-list'),
  modulesState: el('modules-state'),
  modulesRefresh: el('modules-refresh'),
  memDays: el('mem-days'),
  memBudget: el('mem-budget'),
  memBudgetHint: el('mem-budget-hint'),
  memUsage: el('mem-usage'),
  // （原来的 `mem-window-value` <output> 已删：滑块旁边显示天数的同时，
  //   下面的 hint 又说一遍，是重复信息。值统一由 hint 交代。）
  memHint: el('mem-hint'),
  backupEnabled: el('backup-enabled'),
  backupDir: el('backup-dir'),
  pickBackupDir: el('pick-backup-dir'),
  backupKeep: el('backup-keep'),
  backupNow: el('backup-now'),
  backupState: el('backup-state'),
  /** 备份文件夹现状（有几份、共多少 MB） */
  backupInfo: el('backup-info'),
  /** 数据库真实路径（让用户知道"记忆存在哪个文件"，好自己复制备份） */
  dbPathLine: el('db-path-line'),
  // 隐私
  vision: el('vision'),
  /** 截图对话时念出"它看到了什么"（2026-10-17） */
  snapshotDesc: el('snapshot-desc'),
  /** 语音对话开关（麦克风）—— 立刻生效，不等「保存」 */
  voice: el('voice'),
  voiceState: el('voice-state'),
  /** 按住说话：开关 + 按键捕获（立刻生效） */
  pttEnabled: el('ptt-enabled'),
  pttState: el('ptt-state'),
  pttKeyState: el('ptt-key-state'),
  pttCapture: el('ptt-capture'),
  /*
   * **全局键**（窗口没焦点时用的切换式）—— 2026-10-10 加。
   *
   * ⚠️ 和上面那组是**两个不同的键**：聚焦键可以是修饰键（靠
   * `before-input-event`），全局键必须是普通键（accelerator 表达不了修饰键）。
   */
  pttGlobalState: el('ptt-global-state'),
  pttGlobalCapture: el('ptt-global-capture'),
  /** 语音识别：本地大模型 / 云端 API 切换 */
  asrMode: el('asr-mode'),
  asrState: el('asr-state'),
  asrLocalRow: el('asr-local-row'),
  asrLocalModel: el('asr-local-model'),
  asrCloudRow: el('asr-cloud-row'),
  asrCloudBase: el('asr-cloud-base'),
  asrCloudKeyRow: el('asr-cloud-key-row'),
  asrCloudKey: el('asr-cloud-key'),
  asrModelMirror: el('asr-model-mirror'),
  asrModelOfficial: el('asr-model-official'),
  asrModelDir: el('asr-model-dir'),
  asrModelState: el('asr-model-state'),
  asrOpenDir: el('asr-open-dir'),
  asrCopyCmd: el('asr-copy-cmd'),
  asrCloudKeyState: el('asr-cloud-key-state'),
  asrCloudModelHint: el('asr-cloud-model-hint'),
  asrSave: el('asr-save'),
  asrSaveState: el('asr-save-state'),
  /** 麦克风选择 */
  micState: el('mic-state'),
  /*
   * 文字转语音（用户要求 2026-10-09 从「隐私」页拉到独立「语音」页，
   * 并标成 ②）
   */
  ttsProvider: el('tts-provider'),
  ttsState: el('tts-state'),
  ttsCloudRow: el('tts-cloud-row'),
  ttsCloudVoice: el('tts-cloud-voice'),
  ttsCloudHint: el('tts-cloud-hint'),
  /*
   * edge-tts 那两行（2026-10-11 加）。
   *
   * ⚠️ 它和云端那一行**并存但互斥显示**（按 provider 切）。音色名格式**第三套**：
   *     云端 openai  -> `模型名:音色名`
   *     本地 windows -> SAPI 名字
   *     edge         -> `zh-CN-XiaoxiaoNeural`
   * 所以字段也是第三个：`edge_voice`（配置） / `cfg.edgeVoice`（读回）。
   */
  ttsEdgeRow: el('tts-edge-row'),
  ttsEdgeVoice: el('tts-edge-voice'),
  ttsEdgeHint: el('tts-edge-hint'),
  // ⚠️ 原来这里有 ttsLocalRow / ttsLocalVoice / ttsLocalNote / ttsVoicesState
  //    四个元素引用。2026-10-09 按用户要求**删掉了「本地」这个合成方式**
  //    （本地方案在这个环境确定用不了，统计过 0/28，见 docs 第六十二节），
  //    对应界面元素也删了，所以不再引用。
  ttsSave: el('tts-save'),
  ttsSaveState: el('tts-save-state'),
  ttsTest: el('tts-test'),
  // 音量（2026-10-19 用户要求「可调节声音大小」）—— 播放端增益，见 settings.html 的说明
  ttsVolume: el('tts-volume'),
  ttsVolumeValue: el('tts-volume-value'),
  ttsVolumeHint: el('tts-volume-hint'),
  // 快捷键一览（2026-10-12 从托盘菜单搬来，见 settings.html 里的说明）
  shortcutList: el('shortcut-list'),
  // 桌宠
  petScale: el('pet-scale'),
  // 开机自启（2026-10-12）
  loginItem: el('login-item'),
  loginItemState: el('login-item-state'),
  // 界面主题（2026-10-14）
  theme: el('theme'),
  themeState: el('theme-state'),
  /* 浏览器兜底（2026-10-13） */
  browserFallback: el('browser-fallback'),
  browserState: el('browser-state'),
  /* 清空宠物的浏览记录（2026-10-13） */
  browserDataSize: el('browser-data-size'),
  browserDataState: el('browser-data-state'),
  clearBrowserData: el('clear-browser-data'),
  /* 试读一个网址（2026-10-13） */
  browserProbeUrl: el('browser-probe-url'),
  browserProbe: el('browser-probe'),
  browserProbeState: el('browser-probe-state'),
  // （原 `petScaleValue` 已删：宠物大小从滑杆改成了**下拉框**，没有那个 output 了）
  edgePeek: el('edge-peek'),
  peekOffset: el('peek-offset'),
  bubbleLinger: el('bubble-linger'),
  /** 「自言自语」持续多久（分钟，2026-10-17 用户要求；0 = 不回落） */
  monologueDecay: el('monologue-decay'),
  bubbleParaGap: el('bubble-para-gap'),
  bubbleGapAbove: el('bubble-gap-above'),
  bubbleGapSide: el('bubble-gap-side'),
  bubbleCoreShrink: el('bubble-core-shrink'),
  // （原 `edgeSnap` / `edgeSnapValue` 已删 —— 那一项按用户要求去掉了）
  // 常规 / 关于
  status: el('status'),
  storePath: el('store-path'),
  settingsPath: el('settings-path'),
  showPet: el('show-pet'),
  quit: el('quit'),
  quitAll: el('quit-all'),
};

const api = window.petSettings;

/** 显示一条结果。ok=null 表示中性提示 */
function showResult(message, ok, detail) {
  ui.result.classList.remove('hidden', 'ok', 'bad');
  if (ok === true) ui.result.classList.add('ok');
  if (ok === false) ui.result.classList.add('bad');
  ui.result.textContent = message;
  if (detail) {
    const pre = document.createElement('pre');
    pre.textContent = detail;
    ui.result.appendChild(pre);
  }
}

/** 按钮忙碌态 */
function setBusy(button, busy, label) {
  if (busy) {
    button.dataset.label = button.textContent;
    button.textContent = label || '处理中…';
    button.disabled = true;
  } else {
    button.textContent = button.dataset.label || button.textContent;
    button.disabled = false;
  }
}

/* ------------------------------------------------------------------ 分类切换 */

function activateTab(target) {
  for (const button of ui.nav.querySelectorAll('.nav-item')) {
    button.classList.toggle('active', button.dataset.target === target);
  }
  for (const panel of document.querySelectorAll('.tab-panel')) {
    panel.classList.toggle('active', panel.id === target);
  }
}

if (ui.nav) {
  ui.nav.addEventListener('click', (event) => {
    const button = event.target.closest('.nav-item');
    if (button && button.dataset.target) activateTab(button.dataset.target);
  });
}

/* ------------------------------------------------------------------ 填状态 */

/** 把主进程给的状态填进表单 */
function applyState(state) {

  /*
   * 主进程探完本机 SAPI 后会把结果推过来（`settings:changed` 里带 ttsConfig）。
   *
   * ⚠️ 为什么需要这一条：探测是**延迟 3 秒**跑的（别拖慢启动），
   * 用户很可能先打开了设置页 —— 那时看到的是"还没探测完"。
   * 不主动推的话，界面永远停在"还没探测完"。
   */
  if (state && state.ttsConfig) {
    renderTts(state.ttsConfig);
  }

  if (!state) return;
  /*
   * 记忆备份回执（`memory.backup.done`）。
   *
   * 这条是**推送给设置窗口**的，不是普通的状态快照 —— 主进程把大脑的回执
   * 包成 `{ memoryBackup: ... }` 转发过来（见 `index.ts` 里 reqId 分支）。
   * 自动备份（启动时那次）没有调用方在等，只有推送能告诉用户结果。
   */
  switch (state.memoryBackup && state.memoryBackup.type) {
    case 'memory.backup.done': {
      const r = state.memoryBackup;
      const when = r.auto ? '启动自动备份' : '手动备份';
      ui.backupState.textContent = r.ok
        ? `✅ ${when}完成${r.path ? ` → ${r.path}` : ''}`
        : `⚠️ ${when}失败：${r.message || '未知原因'}`;
      break;
    }
    default:
      break;
  }

  /*
   * `memory.result` 在这里**不再处理**（2026-10-05）。
   *
   * 记忆管理已拆成**独立窗口**（memory.html + memory.js），它的回执由那边
   * 自己消费 —— 设置页不再渲染记忆列表，也就没有要刷新的东西。
   *
   * ⚠️ 但 `tools/check-protocol.mjs` 要求"每条下行消息都有前端处理"。
   * 那条检查的 `handled` 是从 **两个渲染层**（app.js / settings.js）里找
   * `case 'xxx':` —— 现在 memory.js 是第三个渲染层，检查器还不知道它。
   * 见 check-protocol.mjs 里补的第三个来源。
   */
  const brain = state.brain || {};
  if (typeof brain.provider === 'string') ui.provider.value = brain.provider;
  if (typeof brain.base_url === 'string') ui.baseUrl.value = brain.base_url;
  /*
   * 模型：把当前值**同时**填进下拉框和手填框。
   *
   * 为什么不只填下拉：列表可能还没拉（或端点没有 `/models`），那时下拉里没有这一项，
   * `select.value = 'deepseek-chat'` 会**静默变成空**（HTML select 的坑），
   * 于是界面显示"没选模型"而实际配置是对的。手填框作为权威显示就不会丢。
   */
  if (typeof brain.model === 'string') {
    ui.modelManual.value = brain.model;
    selectModelInList(brain.model);
  }
  if (ui.vision) ui.vision.checked = Boolean(state.vision && state.vision.enabled);

  // 密钥只显示"有没有配"和打码形式，明文永不回填到输入框
  if (state.apiKeyConfigured) {
    ui.keyState.textContent = `已配置：${state.apiKeyMasked}（来源：${state.apiKeySource}）。留空表示不修改。`;
  } else {
    ui.keyState.textContent = '尚未配置密钥。';
  }
  if (typeof state.credentialsPath === 'string') {
    ui.storePath.textContent = state.credentialsPath;
  }
  if (typeof state.status === 'string') ui.status.textContent = state.status;

  // 人格提示词（同样只填一次，不覆盖用户正在编辑的内容）
  if (state.persona && typeof state.persona.prompt === 'string' && !promptDirty) {
    ui.personaPrompt.value = state.persona.prompt;
    /*
     * 勾选框的初始值只在**首次**载入时填，之后不覆盖 —— 否则用户刚勾上，
     * 一次后台 `settings:changed` 推送就把它弹回去了（"改了没生效"的经典成因）。
     */
    if (!promptToggleTouched) {
      ui.promptEnabled.checked = state.persona.enabled !== false;
    }
    ui.promptState.textContent = state.persona.ok
      ? `来自 ${state.persona.path}${state.persona.name ? ` · 宠物名「${state.persona.name}」` : ''}`
      : `⚠️ ${state.persona.path}`;
  }
}

/** 用户动过勾选框之后，`load()` 不再覆盖它 */
let promptToggleTouched = false;

/** 用户在提示词框里改过东西之后，`load()` 不再覆盖它（否则正在写就被冲掉） */
let promptDirty = false;

/*
 * ---------------------------------------------------------------------------
 * 硅基流动的**已知免费**模型
 * ---------------------------------------------------------------------------
 *
 * ## 为什么是一份静态表
 *
 * 下拉框是**实时**从 `base_url + /models` 拉的，而那个接口**不返回价格**
 * （实测只有 `id` / `object` / `created` / `owned_by`）。
 * 定价页 `siliconflow.cn/pricing` 是**纯客户端 JS 渲染** ——
 * 我抓过：首页 72 万字符，还原后 47 万字符，**一个价格数字都没有**。
 *
 * 所以"哪个免费"只能靠静态表。这是没办法，不是偷懒。
 *
 * ## ⚠️ 两个诚信问题（都在界面上写清了）
 *
 * ① **会过期**：厂商随时能调价。所以带核对日期，超过 90 天就改口说
 *    "这份清单是 X 核对的，可能已变" —— 不硬说"免费"。
 * ② **免费要实名**：硅基流动的免费模型需要实名认证（支付宝人脸 + 证件），
 *    没认证**照样收费**。界面必须写清，别让人以为"选了就免费"。
 *
 * 核对地址：https://siliconflow.cn/pricing
 */
const FREE_MODELS_VERIFIED = '2026-10-08';
const FREE_MODELS = {
  'Qwen/Qwen3-8B': '8B · 支持思考 · 128K 上下文',
  'Qwen/Qwen3.5-4B': '4B · 更小更快',
  'Qwen/Qwen2.5-7B-Instruct': '7B · 老一代',
  'THUDM/GLM-4-9B-0414': '智谱 9B',
  'THUDM/GLM-Z1-9B-0414': '智谱 9B · 推理向',
  'deepseek-ai/DeepSeek-R1-0528-Qwen3-8B': 'DeepSeek R1 蒸馏 8B · 推理向',
  'XingChenAGI/Xing4.0-29B': '29B · 这批里最大的',
};

/** 这份表该不该提醒"可能过期了"（超过 90 天就提醒） */
function freeListStale(now = new Date()) {
  const verified = new Date(`${FREE_MODELS_VERIFIED}T00:00:00Z`);
  if (Number.isNaN(verified.getTime())) return true;
  return (now.getTime() - verified.getTime()) / 86400000 > 90;
}

/** 在下拉框里选中某个模型（没有这一项就追加进去，保证不丢当前值） */
function selectModelInList(id) {
  if (!ui.model || !id) return;
  const exists = Array.from(ui.model.options).some((o) => o.value === id);
  if (!exists) {
    const opt = document.createElement('option');
    opt.value = id;
    opt.textContent = `${id}（当前配置）`;
    ui.model.appendChild(opt);
  }
  ui.model.value = id;
}

/** 用户最终要用的模型名：手填框非空时**以手填为准**（那是明确意图） */
function effectiveModel() {
  return (ui.modelManual.value || '').trim() || (ui.model.value || '').trim();
}

/**
 * 把一个"改一下就生效"的参数接到滑杆上。
 *
 * 抽出来是因为三段逻辑完全一样（节流、回写、显示），复制三遍迟早会漏一处。
 * `read` 返回当前值，`write` 提交并返回**真正生效的值**（可能被夹进范围）。
 *
 * ⚠️ 只用于 `type="range"`。数字输入框请用 `bindNumberField`（提交时机不同，
 * 见那里的说明）。
 */
function bindLiveRange(input, output, read, write, format) {
  if (!input) return;
  let timer = 0;
  let pending = null;

  const commit = async (value) => {
    try {
      const result = await write(Number(value));
      const applied = Number(result && result.applied);
      if (Number.isFinite(applied) && String(applied) !== String(input.value)) {
        input.value = String(applied);
      }
      if (output) output.textContent = format(Number.isFinite(applied) ? applied : value);
      reportPersist(result);
    } catch (error) {
      ui.status.textContent = `保存失败：${error.message}`;
    }
  };

  // `input` 在拖动中会高频触发，每次都走 IPC + 写文件既浪费又会让窗口位置抖，
  // 所以拖动期间只更新数字，60ms 才真正提交一次；松手立刻提交最终值。
  input.addEventListener('input', () => {
    pending = input.value;
    if (output) output.textContent = format(Number(pending));
    if (timer) return;
    timer = setTimeout(() => {
      timer = 0;
      if (pending !== null) void commit(pending);
    }, 60);
  });
  input.addEventListener('change', () => {
    if (timer) {
      clearTimeout(timer);
      timer = 0;
    }
    void commit(input.value);
  });

  void (async () => {
    try {
      const value = Number(await read());
      if (Number.isFinite(value)) {
        input.value = String(value);
        if (output) output.textContent = format(value);
      }
    } catch {
      /* 读不到就保持默认显示，不影响其它设置 */
    }
  })();
}

/**
 * 报出"这次修改到底有没有落盘"。
 *
 * ## 为什么单独抽出来共用
 *
 * 这段是踩过坑的（见 `bindLiveRange` 里的注释）：曾经出现"界面一切正常、
 * 磁盘上什么都没写"，用户重启后参数全回默认而界面从没提示过。
 *
 * 数字框和滑杆**都必须**报这一句。复制一遍迟早只修一处 —— 所以抽出来。
 */
function reportPersist(result) {
  if (!ui.settingsPath || !result || !result.path) return;
  ui.settingsPath.textContent = result.saved === false
    ? `⚠ 无法保存到磁盘：${result.error ?? '未知原因'}（本次修改只在这次运行内有效）`
    : `已保存到 ${result.path}`;
}

/**
 * 把一个参数接到**数字输入框**上（右侧带单位后缀）。
 *
 * ## 与 `bindLiveRange` 的关键区别：提交时机
 *
 * 滑杆是"拖着调"的连续控件 —— `input` 事件高频触发，所以那里 60ms 节流，
 * 让用户拖着就能看到效果。
 *
 * 数字框是"打字"的 —— 在这里照搬节流会**伤害输入**：
 *
 *     想输 120：敲 "1" -> 60ms 后提交 1（真的去改配置了）
 *               敲 "2" -> 提交 12
 *               敲 "0" -> 提交 120
 *
 * 三次 IPC + 三次写盘 + 三次热更新（重建 brain），而前两次是**用户没打算要的中间值**。
 * 更糟的是回写逻辑会把夹取后的值填回输入框，光标位置被打断。
 *
 * 所以这里：**输入时只记着，失焦（`change`）或回车才提交一次**。
 *
 * ## `suffix` 参数
 *
 * 滑杆时代旁边有个 `<output>` 显示"10 秒"。数字框里值就在框里，
 * 再显示一遍是重复（用户之前就指出过这种重复）—— 所以单位改成静态后缀
 * （由 HTML 里那个 `.suffix` 元素承担），这个函数不再碰它。
 *
 * @param input  数字输入框
 * @param read   读当前值（返回数字或 Promise）
 * @param write  提交并返回 { applied, saved, path, error }
 */
function bindNumberField(input, read, write) {
  if (!input) return;

  const commit = async () => {
    const raw = Number(input.value);
    if (!Number.isFinite(raw)) return;
    // 夹进 min/max —— 用户可以直接敲超范围的数
    const lo = Number(input.min);
    const hi = Number(input.max);
    let value = Math.round(raw);
    if (Number.isFinite(lo)) value = Math.max(lo, value);
    if (Number.isFinite(hi)) value = Math.min(hi, value);

    try {
      const result = await write(value);
      const applied = Number(result && result.applied);
      const final = Number.isFinite(applied) ? Math.round(applied) : value;
      /*
       * ⚠️ 立刻写回输入框，**不要等 read() 的往返**。
       *
       * 与「时间窗口」那个坑同源：设置页读的是配置快照，而快照要等
       * sidecar 回报才更新 —— 中间任何一步慢，输入框就会显示上一个值，
       * 看起来像"我输了 120，它自己变回 30"。夹取规则就在本函数里，
       * `final` 就是最终生效值，直接写回去即可。
       */
      if (String(final) !== String(input.value)) input.value = String(final);
      reportPersist(result);
    } catch (error) {
      ui.status.textContent = `保存失败：${error.message}`;
    }
  };

  input.addEventListener('change', () => void commit());
  /*
   * 「边打边存」（2026-10-18 用户反馈"回落的数值重启失效"）。
   *
   * 根因：`change` 事件在 number 输入框上**只在失焦或回车时触发**。
   * 用户敲了数字、直接关窗口/重启 —— 那次 change 根本没发生，值当然没落盘。
   * 这里补一条**防抖 input**：停止输入 700ms 就提交一次。
   *
   * 代价是敲"45"时会先提交一次"4"（再提交 45，后者覆盖前者）。
   * 对"分钟数"这种参数无害：夹取和最后一次写入都走同一条 commit。
   */
  let typingTimer = 0;
  input.addEventListener('input', () => {
    if (typingTimer) clearTimeout(typingTimer);
    typingTimer = setTimeout(() => {
      typingTimer = 0;
      void commit();
    }, 700);
  });
  input.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') {
      event.preventDefault();
      if (typingTimer) clearTimeout(typingTimer);
      void commit();
      input.blur();
    }
  });
  // 关窗口/切页前兜底提交一次（防抖计时器还没到点就关窗）
  window.addEventListener('beforeunload', () => {
    if (typingTimer) {
      clearTimeout(typingTimer);
      typingTimer = 0;
      void commit();
    }
  });

  void (async () => {
    try {
      const value = Number(await read());
      if (Number.isFinite(value)) input.value = String(Math.round(value));
    } catch {
      /* 读不到就保持默认显示，不影响其它设置 */
    }
  })();
}

/* ------------------------------------------------------------------ 读取 */

async function load() {
  try {
    applyState(await api.load());
  } catch (error) {
    ui.status.textContent = `读取设置失败：${error.message}`;
  }
}

// 这三个参数平时由宠物窗口持有（改一下立刻生效），所以单独一对 IPC，
// 不走 settings:load 那份状态 —— 避免出现"两个地方都能改"。
/**
 * 宠物大小：**下拉框**（固定几档），不是滑杆。
 *
 * 用户要求：「宠物变大变小直接写几个标准大小吧，这样好修改维护，像它这样」。
 *
 * 为什么不用 `bindLiveRange`（滑杆那套）：
 *   · 档位清单来自主进程的 `petScale()`（真源是 `petLayout.PET_SCALE_PRESETS`），
 *     渲染层**不写死清单** —— 否则又变成"代码一份、界面一份"。
 *   · 老配置里可能是 1.2 这种非档位值，要吸附到最近的档位再显示。
 *
 * ⚠️ 包在函数里再用 `void` 调用，**不用顶层 await** ——
 * `tools/check-renderer.mjs` 用 `node --check` 逐个文件校验，而它对
 * `.js` 按 CommonJS 解析，顶层 await 直接被判语法错（实测）。
 * 文件里 `void load()` 也是同样的写法。
 */
async function initPetScaleSelect() {
  const select = ui.petScale;
  if (!select) return;
  // 主进程返回 { scale, presets, sizes }。**不在这里写兜底清单** ——
  // 兜底就等于又造了第二份真源，正是这次要消灭的东西。
  // 拿不到就明确报错，比"看起来能用但其实档位不对"好。
  const info = await api.petScale();
  const presets = info && Array.isArray(info.presets) ? info.presets : null;
  const sizes = info && Array.isArray(info.sizes) ? info.sizes : null;
  if (!presets || !sizes || presets.length !== sizes.length) {
    ui.status.textContent = '读取宠物大小档位失败（主进程没返回档位清单）';
    return;
  }
  const current = Number(info.scale) || 1;

  select.innerHTML = '';
  presets.forEach((scale, index) => {
    const option = document.createElement('option');
    option.value = String(scale);
    // 显示像素值：用户看得懂"240 px"，看不懂"1.0x"
    option.textContent = `${sizes[index]} px`;
    select.appendChild(option);
  });

  // 吸附到最近的档位（老配置可能是 1.2 这种非档位值）
  let nearestIndex = 0;
  let nearestDiff = Number.POSITIVE_INFINITY;
  presets.forEach((scale, index) => {
    const diff = Math.abs(scale - current);
    if (diff < nearestDiff) {
      nearestDiff = diff;
      nearestIndex = index;
    }
  });
  select.value = String(presets[nearestIndex]);

  select.addEventListener('change', async () => {
    const index = presets.indexOf(Number(select.value));
    try {
      const r = await api.setPetScale(Number(select.value));
      if (r && r.error) {
        ui.status.textContent = `保存宠物大小失败：${r.error}`;
      } else {
        ui.status.textContent = `宠物大小已改为 ${sizes[index] ?? '?'} px`;
      }
    } catch (error) {
      ui.status.textContent = `改宠物大小失败：${error.message}`;
    }
  });
}
void initPetScaleSelect();

/**
 * 界面主题下拉框（2026-10-14 加）。
 *
 * ## 为什么这里**不自己改** `<html data-theme>`
 *
 * 换肤的真源是主进程注入的那个属性（见 `index.ts` 的 `themeScript`）——
 * 连设置窗口自己也是被它染色的。所以这里只做两件事：
 * ① 把当前值回显到下拉框；② 把用户的选择报回去。
 * 报完之后主进程会立刻把属性刷到**所有**窗口，本窗口跟着一起变。
 *
 * ⚠️ 如果这里再自己设一次属性，就有**两份状态**了：主进程那份是重启后
 * 恢复用的，本窗口那份只在这次运行里对 —— 两者一旦不一致，
 * 表现就是"设置窗口是深色、菜单还是浅色"这类很难归因的怪象。
 *
 * ## ⚠️ 落盘失败要说出来
 *
 * 与 `loginItem` 同一个教训：主题只存在内存里的话，重启就回浅色，
 * 用户会以为"主题设置是假的"。所以主进程返回 `saved: false` 时
 * 必须显示原因，而不是静默。
 */
async function initThemeSelect() {
  const select = ui.theme;
  if (!select) return;

  const info = await api.theme();
  const current = info && info.theme === 'dark' ? 'dark' : 'light';
  select.value = current;

  select.addEventListener('change', async () => {
    const next = select.value === 'dark' ? 'dark' : 'light';
    try {
      const r = await api.setTheme(next);
      if (r && r.saved === false) {
        // 主题**已经**生效（主进程先刷窗口再落盘），只是存不下来 —— 如实说
        if (ui.themeState) {

          ui.themeState.textContent = `⚠️ 主题已切换，但保存失败（重启后会回到浅色）：${
            r.error || '未知原因'
          }`;
        }
      } else if (ui.themeState) {
        ui.themeState.textContent = '';
      }
    } catch (error) {
      if (ui.themeState) {
        ui.themeState.textContent = `⚠️ 切主题出错：${error.message}`;
      }
    }
  });
}
void initThemeSelect();

/*
 * 活泼度滑杆**已删除**（2026-10-17 用户要求：「现在用菜单调节就行」）。
 *
 * ⚠️ 删的时候四层一起删了：HTML 的 .setting、这里的 ui 映射、这条
 * `bindLiveRange`、以及 preload/main 的两条 IPC。只删 HTML 的话
 * `ui.activity` 是 null，`bindLiveRange` 读它就抛错 —— 整页设置白屏
 * （`edgeSnap` 当年就是这么坏的，见那段的注释）。
 *
 * 现在调活泼度只有一条路：宠物右键菜单 -> 活泼度 -> 四档。
 */

/*
 * 文字弹窗存在时间。
 *
 * ## 从滑杆改成**数字输入框**（2026-10-05 用户要求）
 *
 * 用户原话：「这两个也改改吧，改成输入框」—— 指「文字弹窗存在时间」和
 * 「探头时的平移距离」。理由与「时间窗口」那次相同：整数参数用滑杆拖不准。
 *
 * 单位用**秒**（用户看得懂"10 秒"），存储用**毫秒**（渲染层计时器直接用）。
 *
 * ⚠️ 换算只做一次且对称：`read()` 里 `/1000`，`write()` 里 `*1000`。
 *
 * 实测踩过：`read()` 直接返回 IPC 给的毫秒（10000），而滑杆的 `format()`
 * 按秒拼字符串，于是初始显示成 **「10000 秒」**（用户报的）。
 * 现在 `read()` 先换成秒，全程只有秒这一种单位在界面侧流动。
 */
bindNumberField(
  ui.bubbleLinger,
  // 读：毫秒 -> 秒（**这一处换算**，与下面 write 的 *1000 对称）
  async () => Number(await api.bubbleLinger()) / 1000,
  async (seconds) => {
    const r = await api.setBubbleLinger(Math.round(seconds * 1000));
    return {
      applied:
        r && Number.isFinite(Number(r.bubbleLingerMs)) ? Number(r.bubbleLingerMs) / 1000 : seconds,
      saved: r && r.saved,
      path: r && r.path,
      error: r && r.error,
    };
  },
);

/*
 * 「自言自语」持续多久（分钟，2026-10-17 用户要求）。
 *
 * ⚠️ 界面这一侧只有**分钟**一种单位：读出来是秒 -> 这里换成分钟，
 * 写回去再乘 60。换算只在这一处（`bubble-linger` 当年就是单位混用，
 * 界面显示成「10000 秒」被用户抓到）。
 *
 * ⚠️ 0 是**不回落**，不是"立刻回落" —— `tiers.decay_due()` 里 `<= 0` 直接 False。
 * 说明文字里写清楚了，别指望用户自己猜。
 */
bindNumberField(
  ui.monologueDecay,
  async () => Number(await api.monologueDecayMinutes()),
  async (minutes) => {
    const r = await api.setMonologueDecayMinutes(Math.round(minutes));
    return {
      applied:
        r && Number.isFinite(Number(r.minutes)) ? Number(r.minutes) : minutes,
      saved: r && r.saved,
      path: r && r.path,
      error: r && r.error,
    };
  },
);

/*
 * 探头时的平移距离（像素，**可以是负数**）。
 *
 * 负数表示"往回收、立绘退回屏幕里一些"，所以 `min` 是负的 ——
 * `bindNumberField` 的夹取按 `input.min/max` 走，负值不会被当成非法输入。
 */
/*
 * 气泡段间距（px，0~24，默认 4）。读写都是像素，没有单位换算。
 */
/*
 * 气泡摆放的三个可调参数（2026-10-18）：上方间距 / 左右间距 / 立绘贴合度。
 * 读写都是原值（px / px / %），没有单位换算。改完主进程立刻注入生效。
 */
bindNumberField(
  ui.bubbleGapAbove,
  async () => Number(await api.bubbleGapAbove()),
  async (px) => {
    const r = await api.setBubbleGapAbove(Math.round(px));
    return {
      applied: r && Number.isFinite(Number(r.bubbleGapAbovePx)) ? Number(r.bubbleGapAbovePx) : px,
      saved: r && r.saved,
      path: r && r.path,
      error: r && r.error,
    };
  },
);

bindNumberField(
  ui.bubbleGapSide,
  async () => Number(await api.bubbleGapSide()),
  async (px) => {
    const r = await api.setBubbleGapSide(Math.round(px));
    return {
      applied: r && Number.isFinite(Number(r.bubbleGapSidePx)) ? Number(r.bubbleGapSidePx) : px,
      saved: r && r.saved,
      path: r && r.path,
      error: r && r.error,
    };
  },
);

bindNumberField(
  ui.bubbleCoreShrink,
  async () => Number(await api.bubbleCoreShrink()),
  async (pct) => {
    const r = await api.setBubbleCoreShrink(Math.round(pct));
    return {
      applied: r && Number.isFinite(Number(r.bubbleCoreShrinkPct)) ? Number(r.bubbleCoreShrinkPct) : pct,
      saved: r && r.saved,
      path: r && r.path,
      error: r && r.error,
    };
  },
);

bindNumberField(
  ui.bubbleParaGap,
  async () => Number(await api.bubbleParaGap()),
  async (px) => {
    const r = await api.setBubbleParaGap(Math.round(px));
    return {
      applied: r && Number.isFinite(Number(r.bubbleParaGap)) ? Number(r.bubbleParaGap) : px,
      saved: r && r.saved,
      path: r && r.path,
      error: r && r.error,
    };
  },
);

/*
 * 探头时的平移距离（像素，**可以是负数**）。
  () => api.peekOffset(),
  async (value) => {
    const r = await api.setPeekOffset(value);
    return {
      applied: Number(r && r.peekOffsetPx),
      saved: r && r.saved,
      path: r && r.path,
      error: r && r.error,
    };
  },
);

/*
 * ⚠️ 「探头旋转补偿」滑杆已**删除**（2026-10-04，用户要求「把这个删除了吧，现在不用这个了」）。
 *
 * 它当初是为"探头时立绘往右下甩"加的标定入口。后来查明真正的根因是两处几何错误
 * （见 `petLayout.ts` 的 `rawY` 与 `styles.css` 的 `transform-origin`），
 * 根因修掉之后补偿**恒为 0** —— 留着只会误导（用户看到滑杆会以为还需要调）。
 *
 * 一并删掉的东西：HTML 里的滑杆、`ui.peekComp*`、preload 的 `peekComp/setPeekComp`、
 * 主进程的 handler 与 `PetWindow.setPeekCompFactor`、渲染层的 `peekCompFactor`、
 * `peekRotationCompensation`、`__petDebug`、以及 CSS 的 `--peek-comp-*`。
 */

/*
 * ⚠️ 「靠边多少像素自动侧身」（`edge-snap`）的绑定已删除。
 *
 * 用户要求：「将第一项去除，因为现在靠近屏幕最边缘才触发」。
 * HTML 里的滑杆也一并删了 —— 只删 HTML 不删这里，`ui.edgeSnap` 会是 null，
 * `bindLiveRange` 再去读它就抛错，整页设置都刷不出来。
 * 主进程侧的 `edgeSnapPx` 与 IPC 保留（仍有默认值 40 在生效），只是不再暴露给用户调。
 */

// 开关：勾上/取消立刻生效
if (ui.edgePeek) {
  void (async () => {
    try {
      ui.edgePeek.checked = Boolean(await api.edgePeek());
    } catch {
      /* 保持默认 */
    }
  })();
  ui.edgePeek.addEventListener('change', async () => {
    try {
      const result = await api.setEdgePeek(ui.edgePeek.checked);
      if (result && typeof result.edgePeekEnabled === 'boolean') {
        ui.edgePeek.checked = result.edgePeekEnabled;
      }
    } catch (error) {
      ui.status.textContent = `保存失败：${error.message}`;
    }
  });
}

  /*
   * 开关：截图对话时念出"它看到了什么"（2026-10-17 用户要求）。
   *
   * ⚠️ 和 `edgePeek` 那类开关一样是**改动即生效**，不跟着下面的「保存」按钮走 ——
   * 那个按钮是给"接口 / 密钥"用的，混进去会让用户以为不点保存就不生效。
   */
  if (ui.snapshotDesc) {
    void (async () => {
      try {
        ui.snapshotDesc.checked = Boolean(await api.snapshotDescription());
      } catch {
        /* 读不到就保持默认（关） */
      }
    })();
    ui.snapshotDesc.addEventListener('change', async () => {
      try {
        const result = await api.setSnapshotDescription(ui.snapshotDesc.checked);
        if (result && typeof result.showDescription === 'boolean') {
          ui.snapshotDesc.checked = result.showDescription;
        }
        if (result && result.ok === false) {
          ui.status.textContent = `保存失败：${result.error ?? '未知原因'}`;
        }
      } catch (error) {
        ui.status.textContent = `保存失败：${error.message}`;
      }
    });
  }

/* ------------------------------------------------------------------ 交互 */

ui.test.addEventListener('click', async () => {
  setBusy(ui.test, true, '测试中…');
  try {
    const result = await api.test({
      baseUrl: ui.baseUrl.value,
      model: effectiveModel(),
      apiKey: ui.apiKey.value,
    });
    const ok = Boolean(result && result.ok);
    showResult(
      ok ? `连接成功${result.latencyMs ? `（${result.latencyMs}ms）` : ''}` : '连接失败',
      ok,
      result && result.detail,
    );
  } catch (error) {
    showResult(`测试出错：${error.message}`, false);
  } finally {
    setBusy(ui.test, false);
  }
});

/**
 * 拉取可用模型列表并填进下拉框。
 *
 * 用**当前界面上**（可能未保存）的 base_url 与密钥去拉 —— 允许"先拉列表看看，
 * 再决定存哪个"。拉不到不报错致命：端点可能没有 `/models`，那时保留手填框即可。
 */
async function refreshModels() {
  setBusy(ui.refreshModels, true, '拉取中…');
  ui.modelsState.textContent = '正在从接口拉取模型列表…';
  try {
    const result = await api.models({ baseUrl: ui.baseUrl.value, apiKey: ui.apiKey.value });
    const list = (result && result.models) || [];
    if (!result || !result.ok || list.length === 0) {
      ui.modelsState.textContent = `⚠️ ${(result && result.message) || '拉取失败'} —— 用下面的输入框手填模型名`;
      return;
    }
    const keep = effectiveModel();
    ui.model.innerHTML = '';
    for (const m of list) {
      const opt = document.createElement('option');
      opt.value = m.id;
      /*
       * 显示名带上有用的元信息（DeepSeek 的 `/models` 会返回这些）：
       *   · `name` 是人话名字（如"DeepSeek-V3"），比 id 好认；
       *   · 上下文窗口直接决定"能塞多长的记忆"，值得让用户看见；
       *   · 支持图片/思考的也标出来，免得选了才发现不能用。
       */
      const bits = [m.id];
      if (m.name && m.name !== m.id) bits.unshift(m.name);
      const meta = [];
      /*
       * ⚠️ **免费标注放最前面**（用户 2026-10-10 要求，附截图：
       * Qwen/Qwen3-8B 价格显示 ¥0.000000）。
       *
       * 放最前是因为它是**选模型时最关心的信息** —— 其他都是次要属性。
       * 用 `免费` 而不是 `¥0`：中文界面里"免费"一眼就懂。
       */
      const freeNote = FREE_MODELS[m.id];
      if (freeNote) meta.push('免费');
      if (m.contextWindow) meta.push(`上下文 ${Math.round(m.contextWindow / 1000)}k`);
      if (m.inputModalities && m.inputModalities.includes('image')) meta.push('可看图');
      if (m.thinking) meta.push('支持思考');
      let label = meta.length ? `${bits.join(' · ')}（${meta.join('，')}）` : bits.join(' · ');
      // 免费模型再补一句"它是什么"（用户才知道 8B 和 29B 的差别）
      if (freeNote) label += ` — ${freeNote}`;
      opt.textContent = label;
      ui.model.appendChild(opt);
    }
    selectModelInList(keep);
    /*
     * 状态行顺带说明"免费"的**两个前提**（用户 2026-10-10 要求标注免费模型）。
     *
     * ⚠️ 必须写清这两点，否则用户会以为"选了免费模型就真的不花钱"：
     *   ① 免费模型**需要实名认证**（支付宝人脸 + 大陆/港澳台证件），
     *      没认证照样收费
     *   ② 这份免费清单是**静态的、会过期**，超过 90 天就提醒自己核一下
     */
    const freeCount = list.filter((m) => FREE_MODELS[m.id]).length;
    const parts = [`✅ 取到 ${list.length} 个模型（来自 ${ui.baseUrl.value || '当前接口'}）`];
    if (freeCount) {
      parts.push(
        // ⚠️ 这里是 `textContent`，**markdown 星号不会被解析**（会原样显示成 `**实名认证**`）——
        //    强调只能靠措辞和括号，不要用 ** 包
        `其中 ${freeCount} 个标了「免费」。⚠️ 注意：需实名认证才真的免费，没认证照常计费。` +
          `免费清单核对于 ${FREE_MODELS_VERIFIED}${freeListStale() ? '，可能已变，建议自己核一下' : ''}`,
      );
    }
    ui.modelsState.textContent = parts.join('\n');
  } catch (error) {
    ui.modelsState.textContent = `⚠️ 拉取出错：${error.message} —— 用手填框`;
  } finally {
    setBusy(ui.refreshModels, false);
  }
}

ui.refreshModels.addEventListener('click', () => void refreshModels());

// 从下拉框选一个 -> 同步到手填框（手填框是权威显示，见 effectiveModel 的说明）
ui.model.addEventListener('change', () => {
  if (ui.model.value) ui.modelManual.value = ui.model.value;
});

if (ui.brainMode) {
  /*
   * 运行位置切换（2026-10-18）：**只做引导**，不许悄悄写配置。
   *
   * 因为真正落盘要合三样东西（provider/base_url/model/key）——
   * 悄悄写一半，用户看到的是"我什么都没点，它自己变了"。
   * 所以这里只填输入框，由用户点「保存」。
   */
  ui.brainMode.addEventListener('change', () => {
    const local = ui.brainMode.value === 'local';
    if (ui.brainModeHint) {
      ui.brainModeHint.textContent = local
        ? '本地模式：填好地址（Ollama 默认 http://127.0.0.1:11434/v1）和模型名，密钥可以留空。'
        : '云端模式：填接口地址与模型，密钥必填（或设环境变量）。';
    }
    if (ui.apiKey) {
      ui.apiKey.disabled = local;
      ui.apiKey.placeholder = local ? '本地服务不需要密钥' : 'sk-...';
      if (local) ui.apiKey.value = '';
    }
    if (local && ui.baseUrl) {
      if (!ui.baseUrl.value || !/127\.0\.0\.1|localhost/.test(ui.baseUrl.value)) {
        ui.baseUrl.value = 'http://127.0.0.1:11434/v1';
      }
      if (ui.modelManual && !ui.modelManual.value) ui.modelManual.value = 'qwen2.5:7b';
    }
  });
}

ui.save.addEventListener('click', async () => {
  setBusy(ui.save, true, '保存中…');
  try {
    const result = await api.save({
      provider: ui.provider.value,
      baseUrl: ui.baseUrl.value,
      model: effectiveModel(),
      apiKey: ui.apiKey.value,
      visionEnabled: ui.vision ? ui.vision.checked : undefined,
    });
    ui.apiKey.value = '';
    showResult(result && result.message ? result.message : '已保存', true);
    await load();
  } catch (error) {
    showResult(`保存失败：${error.message}`, false);
  } finally {
    setBusy(ui.save, false);
  }
});

ui.showPet.addEventListener('click', () => api.showPet());

ui.quit.addEventListener('click', () => {
  if (confirm('退出当前这个宠物实例？')) api.quit();
});

ui.quitAll.addEventListener('click', () => {
  if (confirm('退出所有正在运行的宠物实例？')) api.quitAll();
});

// 切换接口类型时给几个常见地址，省得用户去查
const PRESETS = {
  openai: { baseUrl: 'https://api.deepseek.com/v1', model: 'deepseek-chat' },
  mock: { baseUrl: '', model: '' },
};

ui.provider.addEventListener('change', () => {
  const preset = PRESETS[ui.provider.value];
  if (!preset) return;
  if (!ui.baseUrl.value.trim()) ui.baseUrl.value = preset.baseUrl;
  // 模型现在也填进**手填框**（下拉框先被清空，等"刷新模型列表"再填）
  if (!ui.modelManual.value.trim()) ui.modelManual.value = preset.model;
});

/* ------------------------------------------------------------- 人格提示词 */

ui.personaPrompt.addEventListener('input', () => {
  promptDirty = true;
  ui.promptState.textContent = '有未保存的修改';
});

ui.savePrompt.addEventListener('click', async () => {
  setBusy(ui.savePrompt, true, '保存中…');
  try {
    const result = await api.setPrompt(ui.personaPrompt.value);
    const ok = Boolean(result && result.ok);
    promptDirty = false;
    ui.promptState.textContent = ok
      ? `✅ 已保存到 ${result.path}（已通知大脑立即生效）`
      : `⚠️ 保存失败：${(result && result.message) || '未知错误'}`;
  } catch (error) {
    ui.promptState.textContent = `⚠️ 保存出错：${error.message}`;
  } finally {
    setBusy(ui.savePrompt, false);
  }
});

ui.reloadPrompt.addEventListener('click', async () => {
  if (promptDirty && !confirm('当前有未保存的修改，重新载入会丢掉它们。继续？')) return;
  try {
    const result = await api.getPrompt();
    if (result && typeof result.prompt === 'string') {
      ui.personaPrompt.value = result.prompt;
      ui.promptEnabled.checked = result.enabled !== false;
      promptDirty = false;
      promptToggleTouched = false;
      ui.promptState.textContent = result.ok
        ? `已重新载入（来自 ${result.path}）`
        : `⚠️ ${result.path}`;
    }
  } catch (error) {
    ui.promptState.textContent = `⚠️ 载入出错：${error.message}`;
  }
});

/*
 * 提示词开关：**勾上就立刻生效**，不用点保存。
 *
 * 为什么单独一对我而不是塞进「保存提示词」：这是一个"开/关"意图，
 * 用户勾完就想马上看到效果；而保存提示词是"提交我改的正文"。
 * 两者语义不同，混在一起会让人以为得先改文字才能关。
 */
ui.promptEnabled.addEventListener('change', async () => {
  promptToggleTouched = true;
  const want = ui.promptEnabled.checked;
  try {
    const result = await api.setPromptEnabled(want);
    const ok = Boolean(result && result.ok);
    if (!ok) ui.promptEnabled.checked = !want; // 失败就回滚，别骗用户
    ui.promptState.textContent = ok
      ? want
        ? '✅ 已启用提示词（已通知大脑）'
        : '✅ 已停用提示词 —— 现在完全按模型自己的性子说话'
      : `⚠️ 保存失败：${(result && result.message) || '未知错误'}`;
    // 停用时把输入框变灰，让"它现在不生效"一眼可见
    ui.personaPrompt.disabled = !want;
  } catch (error) {
    ui.promptEnabled.checked = !want;
    ui.promptState.textContent = `⚠️ 出错：${error.message}`;
  }
});

/* --------------------------------------------------------------- 记忆设置 */

/**
 * 把天数说成人话（"1 个月"比"30 天"好读）。
 *
 * 档位是**近似**的（30 天 ≈ 1 个月）：用户调的是"感觉上注入多久"，
 * 不是日历精度，所以不写精确换算表。
 */
function humanDays(days) {
  // `trimFixed`：整数就不显示小数点（避免出现「274.0 年」这种读着别扭的写法）。
  // 上限已经放到十万天（≈274 年），所以这里必须能处理很大的数。
  const trimFixed = (n, digits) => String(Number(n.toFixed(digits)));
  if (days >= 360) return `${trimFixed(days / 365, 1)} 年`;
  if (days >= 28) return days % 30 === 0 ? `${days / 30} 个月` : `约 ${trimFixed(days / 30, 1)} 个月`;
  if (days >= 7) return days % 7 === 0 ? `${days / 7} 周` : `约 ${trimFixed(days / 7, 1)} 周`;
  return `${days} 天`;
}

async function loadMemoryConfig() {
  try {
    const cfg = await api.memoryConfig();
    if (!cfg) return;
    if (ui.memDays) {
      // 上限来自配置（persona.yaml 的 HISTORY_DAYS_MAX）
      ui.memDays.max = String(cfg.historyDaysMax ?? 100000);
      const cur = Number(cfg.historyDays ?? 30);
      ui.memDays.value = String(cur);
      ui.memDays.dataset.saved = String(cur);
    }
    if (ui.backupEnabled) ui.backupEnabled.checked = Boolean(cfg.backupEnabled);
    if (ui.backupDir) ui.backupDir.value = String(cfg.backupDir || '');
    if (ui.backupKeep) ui.backupKeep.value = String(cfg.backupKeep ?? 0);
    if (ui.backupInfo) {
      // 文件夹现状（有几份、共多少）—— 让"保留份数"这个设置有的放矢
      const info = cfg.backupInfo || {};
      ui.backupInfo.textContent = info.text || '';
    }
    if (ui.dbPathLine) ui.dbPathLine.textContent = `数据库位置：${cfg.dbPath || '(未设置)'}`;
    // ⚠️ 先更新窗口缓存再算提示 —— `budgetHint` 依赖它算百分比
    brainWindow = {
      tokens: Number(cfg.brainContextWindow ?? 0) || 0,
      label: String(cfg.brainLabel || ''),
    };
    const budget = Number(cfg.historyTokenBudget ?? 16000);
    if (ui.memHint) ui.memHint.textContent = budgetHint(budget);
    /*
     * 预算是可编辑的（2026-10-18 用户要求）。填值时**不要覆盖用户正在敲的内容**：
     * 输入框有焦点时跳过，否则热更新回报会把"3000"打回"30000"。
     */
    if (ui.memBudget) {
      if (document.activeElement !== ui.memBudget) {
        ui.memBudget.value = String(budget);
        ui.memBudget.dataset.saved = String(budget);
      }
      if (ui.memBudgetHint) {
        const turns = Math.max(1, Math.round(budget / 150));
        ui.memBudgetHint.textContent =
          `安全上限：${budget.toLocaleString()} token（约 ${turns} 轮）。` +
          '超过它时只发最新的那部分 —— 离开输入框或回车生效。';
      }
    }
    // 那行实时数字要问 Python（半秒），所以异步填、不阻塞
    void refreshHistoryUsage(Number(cfg.historyDays ?? 30));
  } catch (error) {
    if (ui.memHint) ui.memHint.textContent = `⚠️ 读取记忆设置失败：${error.message}`;
  }
}

/**
 * 说明**安全上限**（token 预算）—— 它不是用户直接调的旋钮，所以这里只是
 * 解释它是干什么的，顺便给出它相对模型窗口的占比。
 *
 * ## ⚠️ 定稿的分工（用户 2026-10-08）
 *
 *   记忆天数（用户调的那个） -> 决定**取多少**
 *   token 预算（这个）       -> 决定**最多发多少**（安全上限）
 *
 * 用户否掉了"只给一个 token 预算输入框"的方案 —— 天数才是人脑子里想的东西
 * （"我要它记得最近 7 天"）。所以这一页的主输入框是天数，
 * 而这段文字解释"还有个上限在兜着"。
 *
 * ## ⚠️ 窗口大小**从配置读**，不要硬编码
 *
 * 我原来在这里写死 `128000`。用户看到官方价目表写的是 **1M**，指出来了。
 * 硬编码的两个问题：
 *
 *   ① 数字会过时（官方现价目表：deepseek-flash 上下文长度 **1M**、输出最大 384K）
 *   ② **切到硅基流动时窗口完全不同**（32K 量级）—— 写死会让百分比彻底失真
 *
 * 所以按当前厂商读 `context_window`。读不到就**不显示百分比**，
 * 而不是编一个数 —— 宁可少说，不要说错。
 */
let brainWindow = { tokens: 0, label: '' };

function budgetHint(tokens) {
  const turns = Math.round(tokens / 150); // 一轮 ≈ user+assistant 各约 75 字
  let pct;
  if (brainWindow.tokens > 0) {
    const p = tokens / brainWindow.tokens;
    /*
     * ⚠️ 极小的比例要保留一位小数。
     *
     * 1M 窗口下 30000 只占 3% —— 四舍五入成 3% 还能看，
     * 但 3000 会变成 **0%**，那会让人以为"没配"。
     */
    const shown = p < 0.1 ? `${(p * 100).toFixed(1)}%` : `${Math.round(p * 100)}%`;
    pct = `，占 ${brainWindow.label || '当前厂商'} ${formatTokens(brainWindow.tokens)} 窗口的 ${shown}`;
  } else {
    pct = '（未读到当前厂商的上下文窗口，无法算占比）';
  }
  return (
    `安全上限：${formatTokens(tokens)} token（约 ${turns} 轮）。` +
    `天数取的量超过它时，只会发最新的那部分${pct}。`
  );
}

/** 1000000 -> "1M"，32768 -> "32.8K" —— 大数字给人看要短 */
function formatTokens(n) {
  if (n >= 1000000) return `${Math.round(n / 100000) / 10}M`;
  if (n >= 1000) return `${Math.round((n / 1000) * 10) / 10}K`;
  return String(n);
}

/** 把窗口/备份设置写回（落盘 persona.yaml + 热更新 sidecar） */
async function saveMemoryConfig(patch) {
  try {
    const r = await api.setMemoryConfig(patch);
    if (ui.backupState) {
      ui.backupState.textContent = r && r.ok ? `✅ ${r.message}` : `⚠️ ${(r && r.message) || '保存失败'}`;
    }
    return r;
  } catch (error) {
    if (ui.backupState) ui.backupState.textContent = `⚠️ 出错：${error.message}`;
    return null;
  }
}

/**
 * 问 Python 要"这个天数窗口里到底有多少内容"。
 *
 * ## 用户要求（2026-10-08）
 *
 * > 「最近记忆可以按照设置的天数自动搜索数据库取得字数，计算出总 token
 * >   消耗，再和云端的大模型上下文计算，看总占比。」
 *
 * ## ⚠️ 这个调用要 **~500ms**（起一个轻量 Python 进程）
 *
 * 所以：
 *   · **异步**填，不阻塞页面渲染
 *   · 只在"加载"和"改完天数"时调，**不要**放进 input 事件（那会每敲一个
 *     数字就起一个进程）
 *   · 加一个请求序号防竞态：快速连改天数时，只认最后一次的回复
 *     （否则慢的那次后到，会把新数字覆盖成旧的）
 */
let usageSeq = 0;

async function refreshHistoryUsage(days) {
  if (!ui.memUsage) return;
  const seq = ++usageSeq;
  ui.memUsage.textContent = '正在统计…（要半秒）';
  try {
    const r = await api.historyUsage({ days });
    if (seq !== usageSeq) return; // 有更新的请求了，丢弃这次
    if (!r || !r.ok) {
      ui.memUsage.textContent = `⚠️ 统计失败：${(r && r.message) || '未知错误'}`;
      return;
    }
    ui.memUsage.textContent = describeUsage(r);
  } catch (error) {
    if (seq !== usageSeq) return;
    ui.memUsage.textContent = `⚠️ 统计失败：${error.message}`;
  }
}

/** 把统计结果拼成一行人话 */
function describeUsage(r) {
  const label = r.days > 0 ? `最近 ${r.days} 天` : '不限天数';
  const parts = [
    `${label}：共 ${r.turns} 条 / ${r.chars} 字 / ≈${r.tokens} token`,
    `实际发送 ${r.sentTokens} token`,
  ];
  if (r.truncated) {
    parts.push(`⚠️ 已按预算截断（只发最新的那部分，调小天数或调大预算）`);
  }
  const pcts = [];
  if (r.budget && r.budgetPct !== null) pcts.push(`占预算 ${r.budgetPct}%`);
  if (r.window && r.windowPct !== null) pcts.push(`占窗口 ${r.windowPct}%`);
  return `${parts.join('；')}${pcts.length ? `（${pcts.join('、')}）` : ''}`;
}

if (ui.memDays) {
  /*
   * 天数输入框。落盘时机：**失焦或按回车**才写 —— 与滑杆/预算时代同理，
   * 边敲边写会在你输入 "3" 想打 "30" 时先存一次 3 天，然后立刻触发一次
   * 配置热更新。`input` 事件只更新提示文字。
   */
  const commitDays = async () => {
    const raw = Number(ui.memDays.value);
    if (!Number.isFinite(raw)) return;
    const hi = Number(ui.memDays.max) || 100000;
    const days = Math.max(0, Math.min(hi, Math.round(raw)));
    if (String(days) === ui.memDays.dataset.saved) return;

    /*
     * ⚠️ 立刻把夹取后的值写回 DOM，**不要等 `loadMemoryConfig`**。
     *
     * 踩过（实测）：输入 45 保存后，配置里确实是 45、提示也更新了，
     * 但输入框还显示旧值。根因是"写文件 -> sidecar -> 回报快照"这条链太长，
     * 中间任何一步慢，`loadMemoryConfig` 都会把旧快照填回来。
     */
    ui.memDays.value = String(days);
    ui.memDays.dataset.saved = String(days);

    await saveMemoryConfig({ historyDays: days });
    await loadMemoryConfig();
  };

  ui.memDays.addEventListener('input', () => {
    if (!ui.memHint) return;
    const days = Number(ui.memDays.value);
    if (!Number.isFinite(days)) return;
    ui.memHint.textContent = days > 0
      ? `灌回最近 ${Math.round(days)} 天的对话（离开输入框或回车后保存并统计）`
      : '不限天数 —— 灌回全部历史（仍受预算截断）（回车后保存）';
  });

  ui.memDays.addEventListener('change', () => void commitDays());
  ui.memDays.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      void commitDays();
      ui.memDays.blur();
    }
  });
}

if (ui.memBudget) {
  /*
   * 上下文上限（安全上限）输入框。
   *
   * 与「记忆天数」同一套落盘时机：change（失焦）/ Enter 提交，
   * 另外补一条 **700ms 防抖** 的 `input` 提交 —— 用户敲完直接关窗口时
   * `change` 不会触发（这正是「回落的数值重启失效」的根因，见 §138）。
   */
  let budgetTimer = 0;

  const commitBudget = async () => {
    if (budgetTimer) {
      clearTimeout(budgetTimer);
      budgetTimer = 0;
    }
    const raw = Number(ui.memBudget.value);
    if (!Number.isFinite(raw)) return;
    // 夹取上下限必须与主进程写入口一致（1000 ~ 100000）
    const budget = Math.max(1000, Math.min(100000, Math.round(raw)));
    ui.memBudget.value = String(budget);
    if (String(budget) === ui.memBudget.dataset.saved) return;
    ui.memBudget.dataset.saved = String(budget);

    await saveMemoryConfig({ historyTokenBudget: budget });
    // 重新统计：下面的「最近 N 天…占预算 x%」要跟着变
    await loadMemoryConfig();
    void refreshHistoryUsage(Number(ui.memDays?.value ?? cfg.historyDays ?? 30));
  };

  ui.memBudget.addEventListener('change', () => void commitBudget());
  ui.memBudget.addEventListener('input', () => {
    if (budgetTimer) clearTimeout(budgetTimer);
    budgetTimer = setTimeout(() => void commitBudget(), 700);
  });
  ui.memBudget.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      void commitBudget();
      ui.memBudget.blur();
    }
  });
  window.addEventListener('beforeunload', () => {
    if (budgetTimer) {
      clearTimeout(budgetTimer);
      budgetTimer = 0;
      void commitBudget();
    }
  });
}

if (ui.pickBackupDir) {
  ui.pickBackupDir.addEventListener('click', async () => {
    try {
      const picked = await api.chooseFolder();
      if (!picked || !picked.ok) return;
      ui.backupDir.value = picked.path;
      // 选完就存 —— 让"选文件夹"这一步自己完成，不必再点别的按钮
      await saveMemoryConfig({
        backupEnabled: ui.backupEnabled.checked,
        backupDir: picked.path,
      });
    } catch (error) {
      ui.backupState.textContent = `⚠️ 选择文件夹失败：${error.message}`;
    }
  });
}

if (ui.backupEnabled) {
  ui.backupEnabled.addEventListener('change', async () => {
    await saveMemoryConfig({
      backupEnabled: ui.backupEnabled.checked,
      backupDir: ui.backupDir.value,
    });
  });
}

if (ui.backupKeep) {
  ui.backupKeep.addEventListener('change', async () => {
    await saveMemoryConfig({ backupKeep: Number(ui.backupKeep.value) });
  });
}

if (ui.backupNow) {
  ui.backupNow.addEventListener('click', async () => {
    setBusy(ui.backupNow, true, '备份中…');
    ui.backupState.textContent = '正在备份…';
    try {
      const r = await api.backupNow();
      ui.backupState.textContent = r && r.ok ? `✅ ${r.message}` : `⚠️ ${(r && r.message) || '备份失败'}`;
    } catch (error) {
      ui.backupState.textContent = `⚠️ 备份出错：${error.message}`;
    } finally {
      setBusy(ui.backupNow, false);
    }
  });
}

/*
 * 右上角自绘的关闭按钮（`#close`）的处理已**删除**。
 *
 * 它属于无边框弹窗时期（那时没有系统标题栏，只能用页面按钮关闭）。
 * 现在设置窗口是**带系统边框的普通窗口**，标题栏自带的关闭按钮就够了。
 * Esc 也一并去掉 —— 普通窗口里 Esc 不该关窗（用户可能只是想取消某个输入）。
 */

/* --------------------------------------------------- 打开记忆管理窗口 */

/*
 * 记忆管理拆成独立窗口之后，设置页只留一个入口。
 *
 * 为什么不内嵌 iframe：那会让"记忆数据"和"设置表单"共享同一个渲染进程与
 * preload —— 而记忆管理有自己一份**最小授权**的 preload（只暴露记忆通道）。
 * 嵌进来就退回了"一个页面同时拿到两种能力"。
 */
if (ui.openMemory || ui.openMemoryFromPrompts) {
  /** 两个入口共用同一段逻辑（「记忆」标签一个、「提示词」标签一个） */
  const openIt = async (btn) => {
    const r = await api.openMemory().catch((e) => ({ ok: false, message: e.message }));
    if (r && r.ok === false && btn) {
      // 打开失败要说出来 —— 否则用户只看到"点了没反应"
      const original = btn.textContent;
      btn.textContent = `⚠️ ${r.message || '打开失败'}`;
      setTimeout(() => {
        btn.textContent = original;
      }, 2600);
    }
  };
  if (ui.openMemory) ui.openMemory.addEventListener('click', () => void openIt(ui.openMemory));
  if (ui.openMemoryFromPrompts) {
    ui.openMemoryFromPrompts.addEventListener('click', () =>
      void openIt(ui.openMemoryFromPrompts),
    );
  }
}

/* ------------------------------------------------------------- 控件外观统一 */

/**
 * 给每个 `<select>` 套一层 `.select-wrap`，好画自己的下拉箭头。
 *
 * ## 为什么在 JS 里包而不是改 HTML
 *
 * 三个 select 分散在不同标签页（桌宠大小 / 接口类型 / 模型），
 * 在 HTML 里逐处加 `<div class="select-wrap">` 有三个问题：
 *   · 每加一个新 select 都要记得手写这层（漏一个就退回系统箭头，很难注意到）；
 *   · 改 HTML 结构要同步检查 CSS 的 `.field` 两列网格会不会被撑坏；
 *   · 三处重复的包裹代码本身就是漂移的来源。
 *
 * 在 JS 里统一包一层，**新加的 select 自动获得同样的外观**。
 *
 * ## 为什么必须是包裹层而不是 `select` 自己的 `::after`
 *
 * `<select>` 是**替换元素**，规范上不支持 `::before`/`::after`
 * （浏览器实现也不给生成）。所以箭头只能画在它外面，靠父元素定位。
 */
function wrapSelects() {
  for (const sel of document.querySelectorAll('select')) {
    const parent = sel.parentElement;
    // 已经包过就跳过（重复调用不该套娃）
    if (parent && parent.classList.contains('select-wrap')) continue;
    const wrap = document.createElement('div');
    wrap.className = 'select-wrap';
    sel.replaceWith(wrap);
    wrap.appendChild(sel);
  }
}


/**
 * 渲染「提示词」标签页的字段。
 *
 * ## 为什么由主进程给清单
 *
 * 每条要中文标签、说明、可用占位符 —— 这些不该在渲染层再写一份
 * （两处各写一份必然漂移）。所以主进程返回数组，这里只负责画。
 *
 * ## 为什么用 `<details>` 折叠
 *
 * 9 条提示词全部摊开会占两三屏，用户找一条要滚很久。折叠之后一眼能看全清单，
 * 点开哪条改哪条。**默认全部折叠**（改提示词是低频操作）。
 */
async function renderPromptTemplates() {
  const host = ui.promptFields;
  if (!host) return;
  try {
    const list = await api.promptTemplates();
    if (!Array.isArray(list) || list.length === 0) {
      ui.promptTplState.textContent = '⚠️ 没有读到任何提示词模板';
      return;
    }
    host.innerHTML = '';
    for (const f of list) {
      const box = document.createElement('details');
      box.className = 'prompt-item';

      const sum = document.createElement('summary');
      const label = document.createElement('span');
      label.textContent = f.label;
      const meta = document.createElement('span');
      meta.className = 'prompt-meta';
      // 折叠状态下也能看到"这条现在长什么样"的开头，便于扫读
      const preview = String(f.value || '').replace(/\s+/g, ' ').slice(0, 34);
      meta.textContent = preview ? `${preview}${f.value.length > 34 ? '…' : ''}` : '（用内置默认）';
      sum.append(label, meta);

      const hint = document.createElement('p');
      hint.className = 'hint';
      hint.textContent = `${f.hint}　可用占位符：${f.placeholders}`;

      const input = document.createElement(f.multiline ? 'textarea' : 'input');
      input.className = 'prompt-input';
      input.value = String(f.value || '');
      if (!f.multiline) input.type = 'text';
      else input.rows = 3;
      input.spellcheck = false;

      const row = document.createElement('div');
      row.className = 'row buttons';
      const save = document.createElement('button');
      save.className = 'btn primary';
      save.textContent = '保存这一条';
      const state = document.createElement('span');
      state.className = 'prompt-state';

      save.addEventListener('click', async () => {
        save.disabled = true;
        state.textContent = '保存中…';
        try {
          const r = await api.setPromptTemplate({
            section: f.section,
            sub: f.sub,
            key: f.key,
            value: input.value,
          });
          state.textContent = r && r.ok ? '✅ 已保存并生效' : `⚠️ ${(r && r.message) || '保存失败'}`;
          if (r && r.ok) {
            const p = input.value.replace(/\s+/g, ' ').slice(0, 34);
            meta.textContent = p ? `${p}${input.value.length > 34 ? '…' : ''}` : '（空 → 用内置默认）';
          }
        } catch (error) {
          state.textContent = `⚠️ 出错：${error.message}`;
        } finally {
          save.disabled = false;
        }
      });

      row.append(save, state);
      box.append(sum, hint, input, row);
      host.appendChild(box);
    }
    ui.promptTplState.textContent = `共 ${list.length} 条。改完点「保存这一条」立刻生效。`;
  } catch (error) {
    ui.promptTplState.textContent = `⚠️ 读取提示词模板失败：${error.message}`;
  }
}

// 主进程主动推送的状态变化（sidecar 回报了新的生效值等）
api.onChanged((state) => applyState(state));

/* ---------------------------------------------------------- 语音对话开关 */

/**
 * 语音开关**立刻生效**，不等「保存」。
 *
 * ## ⚠️ 为什么不能像别的开关那样等到保存
 *
 * `getUserMedia` 要求发起时**在用户手势的调用栈里**（浏览器安全策略）。
 * 如果等到点「保存」才去开，那次调用虽然也算点击 —— 但用户改了别的设置
 * 顺手点保存时也会触发开麦，那就不是他的意图了。
 *
 * 所以：**点这个开关 = 明确的意图**，当场生效。
 *
 * ## 状态显示
 *
 * 开麦可能失败（权限被拒 / 没有设备 / 被占用），三种原因对用户的下一步
 * 动作完全不同，所以主进程会把**真实结果**推回来显示在这里。
 */
function renderVoiceState(s) {
  if (!ui.voiceState) return;
  const label = {
    listening: '🎤 正在听',
    hearing: '🎤 听到你说话了',
    thinking: '🎤 识别中…',
    speaking: '🔊 正在回答',
    idle: '已关闭',
    error: '⚠️ 打不开',
  };
  const text = label[s?.state] || s?.state || '未知';
  ui.voiceState.textContent = s?.detail ? `${text} —— ${s.detail}` : text;
}

if (ui.voice) {
  ui.voice.addEventListener('change', () => {
    const want = !!ui.voice.checked;
    if (ui.voiceState) ui.voiceState.textContent = want ? '正在请求麦克风权限…' : '正在关闭…';
    api.setVoice(want);
  });
}
api.onVoiceState((s) => {
  // 失败时把开关拨回去 —— 否则界面显示"开着"而实际没开
  if (ui.voice && s?.state === 'error') ui.voice.checked = false;
  if (ui.voice && typeof s?.on === 'boolean') ui.voice.checked = !!s.on;
  renderVoiceState(s);
});

// 打开设置页时问一次**真实**状态（否则开关显示的是默认值）
void api
  .voiceState()
  .then((s) => {
    if (ui.voice && typeof s?.on === 'boolean') ui.voice.checked = !!s.on;
    renderVoiceState(s);
  })
  .catch(() => {});

/* ------------------------------------------------------------ 按住说话 */

/**
 * VK -> 名字。**必须与主进程 `pushToTalk.ts` 的 `vkName` 一致** ——
 * 两边各写一份就会漂移（本项目已经因为"两个真相源"出过快捷键的事故）。
 *
 * ⚠️ 这里只实现**设置页可能显示到**的那部分（用户能按出来的键）；
 * 主进程那份更全（它还要给日志用）。查不到时两边都退回 `VK 0x..` 写法，
 * 所以即使有不一致也不会显示错，只是名字不如主进程的详细。
 */
function vkLabel(vk) {
  const n = Number(vk);
  if (!Number.isFinite(n) || n <= 0) return '未设置';
  const named = {
    0x08: 'Backspace', 0x09: 'Tab', 0x0d: 'Enter', 0x10: 'Shift', 0x11: 'Ctrl',
    0x12: 'Alt', 0x14: 'CapsLock', 0x1b: 'Esc', 0x20: '空格',
    0x21: 'PageUp', 0x22: 'PageDown', 0x23: 'End', 0x24: 'Home',
    0x25: '←', 0x26: '↑', 0x27: '→', 0x28: '↓',
    0x2d: 'Insert', 0x2e: 'Delete', 0x5b: '左 Win', 0x5c: '右 Win',
    0xa0: '左 Shift', 0xa1: '右 Shift', 0xa2: '左 Ctrl', 0xa3: '右 Ctrl',
    0xa4: '左 Alt', 0xa5: '右 Alt',
    0xc0: '` ~', 0xba: '; :', 0xbb: '= +', 0xbc: ', <', 0xbd: '- _',
    0xbe: '. >', 0xbf: '/ ?', 0xdb: '[ {', 0xdc: '\\ |', 0xdd: '] }', 0xde: "' \"",
  };
  if (named[n]) return named[n];
  if (n >= 0x30 && n <= 0x39) return String.fromCharCode(n);
  if (n >= 0x41 && n <= 0x5a) return String.fromCharCode(n);
  if (n >= 0x60 && n <= 0x69) return `小键盘 ${n - 0x60}`;
  if (n >= 0x70 && n <= 0x87) return `F${n - 0x6f}`;
  return `VK 0x${n.toString(16).toUpperCase()}`;
}

function renderPtt(cfg) {
  if (!cfg) return;
  if (ui.pttEnabled) ui.pttEnabled.checked = !!cfg.enabled;
  if (ui.pttKeyState) {
    ui.pttKeyState.textContent = `当前：${vkLabel(cfg.vk)}（VK ${cfg.vk || 0}）`;
  }
  if (ui.pttState) {
    /*
     * ⚠️ 必须区分"配置开着"和"真的在监听" —— 两者不一致是**很常见**的
     * （键被别的程序独占 / accelerator 表达不了这个键 / 钩子收不到事件）。
     * 只显示配置值会让用户以为"我开了它就一定在听"。
     *
     * ⚠️ 现在还要区分**两条路各自的状态**（2026-10-10 加）：
     *
     *     聚焦时按住 -> before-input-event（能拿到松开，真正的按住说）
     *     不聚焦时   -> globalShortcut（只有按下，切换式）
     *
     * 它们可能一个生效一个不生效 —— 笼统说"没起来"会让用户不知道该改哪个。
     */
    if (!cfg.enabled) {
      ui.pttState.textContent = '未启用';
    } else {
      const parts = [];
      parts.push(
        cfg.focusedRunning
          ? `✅ 聚焦时：按住 ${vkLabel(cfg.vk)} 说话`
          : `⚠️ 聚焦时没生效（换一个键试试）`,
      );
      if (cfg.globalVk) {
        parts.push(
          cfg.globalRunning
            ? `✅ 不聚焦时：按 ${vkLabel(cfg.globalVk)} 切换`
            : `⚠️ 全局键没注册上（多半被别的程序占了，换一个）`,
        );
      } else {
        parts.push('（未设全局键，窗口没焦点时不可用）');
      }
      ui.pttState.textContent = parts.join('　·　');
    }
  }
  if (ui.pttGlobalState) {
    const g = Number(cfg.globalVk || 0);
    if (!g) {
      ui.pttGlobalState.textContent = '未设置（窗口没焦点时不能用）';
    } else {
      ui.pttGlobalState.textContent =
        `当前：${vkLabel(g)}（VK ${g}）` + (cfg.globalRunning ? '' : '　⚠️ 没注册上');
    }
  }
}

if (ui.pttEnabled) {
  ui.pttEnabled.addEventListener('change', () => {
    const want = !!ui.pttEnabled.checked;
    if (ui.pttState) ui.pttState.textContent = '正在应用…';
    void api
      .setPtt({ enabled: want })
      .then((r) => renderPtt(r))
      .catch((e) => {
        if (ui.pttState) ui.pttState.textContent = `⚠️ 保存失败：${e.message}`;
      });
  });
}

/**
 * 正在等用户按一个键。`null` = 没在捕获。
 *
 * ## ⚠️ 为什么要记"捕获哪个键"而不是一个布尔（2026-10-10 改）
 *
 * 界面上有**两个**按键控件：
 *
 *     pttCapture       -> 聚焦时按住说的键（可以是右 Alt 这类修饰键）
 *     pttGlobalCapture -> 不聚焦时的全局键（**必须普通键**）
 *
 * 原来只有一个 `capturingKey` 布尔。加了第二个控件后，布尔说不清
 * "现在该把这一下按键存到哪一路" —— 必然写错一路。
 */
let capturingKey = null;

/** 两个按键控件共用的 UI 复位 */
const CAPTURE_BUTTONS = {
  focused: { button: 'pttCapture', idle: '按键设置' },
  global: { button: 'pttGlobalCapture', idle: '全局键设置' },
};

function endCapture() {
  capturingKey = null;
  for (const { button, idle } of Object.values(CAPTURE_BUTTONS)) {
    const el = ui[button];
    if (el) {
      el.textContent = idle;
      el.classList.remove('primary');
    }
  }
}

function beginCapture(which) {
  capturingKey = which;
  const { button } = CAPTURE_BUTTONS[which];
  const el = ui[button];
  if (el) {
    el.textContent = '请按键…';
    el.classList.add('primary');
  }
  const hint =
    which === 'global'
      ? '现在按一下你要用的键（**普通键**，Esc 取消）'
      : '现在按一下你要用的键（Esc 取消）';
  if (which === 'global') {
    if (ui.pttGlobalState) ui.pttGlobalState.textContent = hint.replace(/\*\*/g, '');
  } else if (ui.pttKeyState) {
    ui.pttKeyState.textContent = hint;
  }
}

if (ui.pttCapture) {
  ui.pttCapture.addEventListener('click', () => beginCapture('focused'));
}
if (ui.pttGlobalCapture) {
  ui.pttGlobalCapture.addEventListener('click', () => beginCapture('global'));
}

/**
 * 捕获一个按键。
 *
 * ⚠️ 用 `keyCode` 而不是 `code`：`KeyboardEvent.keyCode` 在 **Windows 上就是
 * Windows 虚拟键码（VK）** —— 正是配置里存的那个数。
 * 用 `code`（如 `'KeyA'`）还得再维护一张映射表，多一处会漂移的地方。
 *
 * ⚠️ `keyCode` 已被标准废弃，但 Chromium **仍然提供**且语义稳定，
 * 而且它是这里唯一能直接拿到 VK 的来源。用它、并把这个理由写下来 ——
 * 比"为了不用废弃 API 而自己维护映射表"更可靠。
 */
window.addEventListener(
  'keydown',
  (event) => {
    if (!capturingKey) return;
    const which = capturingKey;
    event.preventDefault();
    event.stopPropagation();

    if (event.key === 'Escape') {
      endCapture();
      void api.pttConfig().then((c) => renderPtt(c));
      return;
    }

    const vk = Number(event.keyCode);
    /*
     * ⚠️ 修饰键单独按下时 `keyCode` 是 16/17/18（左 Shift/Ctrl/Alt），
     * 而**区分左右**的码在 `location` 里：DOM_KEY_LOCATION_RIGHT = 2。
     * 用户想配"右 Alt"时如果不换算，会得到左 Alt（VK 164）——
     * 而左 Alt 会触发窗口菜单，是**最不该**用来按住说话的键。
     */
    const RIGHT = 2;
    const base = { 16: 0xa0, 17: 0xa2, 18: 0xa4 };
    const rightBase = { 16: 0xa1, 17: 0xa3, 18: 0xa5 };
    const finalVk =
      base[vk] !== undefined && event.location === RIGHT ? rightBase[vk] : vk;

    if (!finalVk) return;
    endCapture();

    /*
     * ⚠️ **全局键必须是普通键** —— 在这里就拦住并说清原因。
     *
     * 实测 accelerator 解析：`Ctrl`(17) / `Alt`(18) / `ControlRight`(0xA3)
     * 这些修饰键**不是 key code**，注册时会**抛异常**（`conversion failed`）。
     * 让用户存下去只会得到"存了但不生效"，不如当场拒绝并 объясн 原因。
     */
    if (which === 'global' && base[finalVk] !== undefined) {
      if (ui.pttGlobalState) {
        ui.pttGlobalState.textContent =
          '⚠️ 这个键是修饰键，全局热键用不了 —— 请按一个普通键（F9 / ScrollLock / 媒体键…）';
      }
      return;
    }

    const payload = which === 'global' ? { globalVk: finalVk } : { vk: finalVk };
    void api
      .setPtt(payload)
      .then((r) => {
        renderPtt(r);
        if (!r) return;
        if (which === 'focused' && r.focusedRunning === false && r.enabled) {
          if (ui.pttState) ui.pttState.textContent += '　⚠️ 聚焦键没挂上（看日志）';
        }
        if (which === 'global' && r.globalRunning === false && r.enabled) {
          if (ui.pttGlobalState) {
            ui.pttGlobalState.textContent += '　⚠️ 没注册上，多半被别的程序占用了';
          }
        }
      })
      .catch((e) => {
        const target = which === 'global' ? ui.pttGlobalState : ui.pttKeyState;
        if (target) target.textContent = `⚠️ 保存失败：${e.message}`;
      });
  },
  // 捕获阶段：抢在别的处理函数之前拿到这个键（否则某些键会被页面先吃掉）
  true,
);

void api.pttConfig().then((c) => renderPtt(c)).catch(() => {});

/* --------------------------------------------------------- 大脑提供商（互切） */

/**
 * 大模型提供商互切（用户要求 2026-10-08：
 * 「两个都要，要和现有的 deepseek 有互切选项」）。
 *
 * ## 切厂商时要连带刷新四样东西
 *
 *   ① `#brain-vendor` 自己（保持选中）
 *   ② `#base-url`  —— 换成该厂商的地址
 *   ③ `#model`     —— 换成该厂商的模型列表，默认选它当前用的那个
 *   ④ `#api-key`   —— 该厂商**各自**的密钥槽位（DeepSeek 的 key 不该被
 *                     硅基流动的覆盖，反之亦然）
 *
 * ⚠️ 不刷新的话用户会看到"切到硅基流动了，但地址还是 DeepSeek 的" ——
 * 那种不一致比不能切更糟。
 */

/** 缓存最近一次读到的配置（切换时要用它算默认值） */
let brainCfgCache = null;

function renderBrain(cfg) {
  if (!cfg || !ui.brainVendor) return;
  brainCfgCache = cfg;

  // ① 提供商下拉（只在列表变了时重建，否则会打断用户的选择）
  const want = (cfg.vendors ?? []).map((v) => v.name).join(',');
  if (ui.brainVendor.dataset.vendors !== want) {
    ui.brainVendor.innerHTML = '';
    for (const v of cfg.vendors ?? []) {
      const o = document.createElement('option');
      o.value = v.name;
      o.textContent = v.label || v.name;
      ui.brainVendor.appendChild(o);
    }
    ui.brainVendor.dataset.vendors = want;
  }
  ui.brainVendor.value = cfg.active || '';

  // ②③ 地址与模型列表（顺带判断当前跑在本地还是云端）
  if (ui.baseUrl) ui.baseUrl.value = cfg.baseUrl || '';
  if (ui.brainMode) {
    const base = String(cfg.baseUrl || '');
    const local = /127\.0\.0\.1|localhost|::1/.test(base);
    ui.brainMode.value = local ? 'local' : 'cloud';
    if (ui.brainModeHint) {
      ui.brainModeHint.textContent = local
        ? '当前指向本机服务：不需要 API 密钥（Ollama 默认 http://127.0.0.1:11434/v1）。'
        : '当前走云端：需要在下面填接口地址与密钥。';
    }
  }
  const cur = (cfg.vendors ?? []).find((v) => v.name === cfg.active);
  fillModelSelect(ui.model, cur?.model || cfg.model || '', cur?.models ?? []);

  // ④ 密钥状态（**只显示掩码**，明文永远不回渲染层）
  if (ui.keyState) {
    ui.keyState.textContent = cfg.keyConfigured
      ? `已配置（${cfg.label}）：${cfg.keyMasked}。留空则不改动。`
      : `尚未为「${cfg.label}」配置密钥。填一个保存即可（密钥不写进项目文件）。`;
  }
  if (ui.apiKey) {
    ui.apiKey.value = '';
    ui.apiKey.placeholder = cfg.keyConfigured ? '已保存（留空不改动）' : 'sk-...';
  }

  // ⑤ 思考模式开关对非 DeepSeek 无效 —— 如实说明，别让开关看着能点却没作用
  if (ui.brainVendorState) {
    ui.brainVendorState.textContent = cfg.supportsThinking
      ? '当前提供商支持「思考模式」（宠物菜单里那个开关对它生效）。'
      : '当前提供商不支持「思考模式」参数 —— 宠物菜单里那个开关对它不生效'
        + '（避免传未知参数导致请求失败）。';
  }
}

/** 把某厂商的模型列表灌进 `#model` 下拉 */
function fillModelSelect(select, current, models) {
  if (!select) return;
  select.innerHTML = '';
  const list = [...new Set([current, ...(models ?? [])].filter(Boolean))];
  for (const m of list) {
    const o = document.createElement('option');
    o.value = m;
    o.textContent = m;
    select.appendChild(o);
  }
  if (current) select.value = current;
}

async function loadBrain() {
  try {
    renderBrain(await api.brainConfig());
  } catch (error) {
    if (ui.brainVendorState) ui.brainVendorState.textContent = `读取失败：${error.message}`;
  }
}

if (ui.brainVendor) {
  ui.brainVendor.addEventListener('change', () => {
    /*
     * ⚠️ 切换**立刻落盘**（不等用户点"保存"）。
     *
     * 理由：这是"我现在要用哪家"的选择，语义上就该即时生效；
     * 而地址/模型/密钥是**该家的参数**，改了要点保存才提交。
     * 混在一起会让用户以为"切了但没保存"而重复操作。
     */
    const active = ui.brainVendor.value;
    const vendor = brainCfgCache?.vendors?.find((v) => v.name === active);
    if (ui.baseUrl) ui.baseUrl.value = vendor?.baseUrl || '';
    fillModelSelect(ui.model, vendor?.model || '', vendor?.models ?? []);
    if (ui.brainVendorState) ui.brainVendorState.textContent = '正在切换…';
    void api
      .setBrain({ active })
      .then((r) => {
        if (r && r.config) renderBrain(r.config);
        else void loadBrain();
      })
      .catch((e) => {
        if (ui.brainVendorState) ui.brainVendorState.textContent = `⚠️ 切换失败：${e.message}`;
      });
  });
}

void loadBrain();

/* --------------------------------------------------------- 语音识别方式 */

/**
 * 语音识别：本地大模型 vs 云端 API（用户要求 2026-10-07）。
 *
 * ## 这个开关为什么要做成**下拉框**而不是 checkbox
 *
 * 它不是二态 —— 还有个 `mock`（假识别，只在调试链路时用）。
 * 而且两个选项的**代价完全不同**（本地占显存、云端上传语音），
 * 下拉框能把选项文字写清楚，用户一眼看到取舍。
 *
 * ## ⚠️ 保存后是**立刻生效**的（不用重启）
 *
 * 主进程写 `persona.yaml` + 发 `config.update`，sidecar 会重建语音引擎。
 * 但**模型加载要时间**：本地的 1.6GB 首次要几十秒 —— 所以状态栏要提示。
 */
function renderAsr(cfg) {
  if (!cfg) return;
  if (ui.asrMode) ui.asrMode.value = cfg.mode || 'local';
  if (ui.asrLocalModel && cfg.localModel) ui.asrLocalModel.value = cfg.localModel;
  if (ui.asrCloudBase) ui.asrCloudBase.value = cfg.cloudBaseUrl || '';
  if (ui.asrCloudModelHint) ui.asrCloudModelHint.textContent = cfg.cloudModel || '';
  if (ui.asrCloudKeyState) {
    ui.asrCloudKeyState.textContent = cfg.cloudKeyConfigured
      ? `已配置：${cfg.cloudKeyMasked}`
      : `尚未配置。两种配法：设置页这里填，或设好环境变量 ${cfg.cloudKeyEnv}`;
  }
  syncAsrRows(cfg.mode);
  renderAsrState(cfg);
}

/** 只在相关模式下显示对应的行（本地的模型选择 / 云端的地址与密钥） */
function syncAsrRows(mode) {
  const m = mode || ui.asrMode?.value || 'local';
  const show = (row, on) => {
    if (row) row.style.display = on ? '' : 'none';
  };
  show(ui.asrLocalRow, m === 'local');
  show(ui.asrCloudRow, m === 'openai');
  show(ui.asrCloudKeyRow, m === 'openai');
}

function renderAsrState(cfg) {
  if (!ui.asrState) return;
  if (!cfg) {
    ui.asrState.textContent = '（读不到配置）';
    return;
  }
  if (cfg.mode === 'local') {
    ui.asrState.textContent = `当前：本地大模型 ${cfg.localModel}（离线、语音不出本机）`;
  } else if (cfg.mode === 'openai') {
    ui.asrState.textContent = cfg.cloudKeyConfigured
      ? `当前：云端 ${cfg.cloudModel}（⚠️ 语音会上传）`
      : '当前：云端，但**密钥还没配** —— 现在会退化成假识别';
  } else {
    ui.asrState.textContent = '当前：假识别（只用于调试链路时序，听不懂人话）';
  }
}

if (ui.asrMode) {
  ui.asrMode.addEventListener('change', () => syncAsrRows(ui.asrMode.value));
}

if (ui.asrSave) {
  ui.asrSave.addEventListener('click', () => {
    const payload = {
      mode: ui.asrMode ? ui.asrMode.value : undefined,
      localModel: ui.asrLocalModel ? ui.asrLocalModel.value : undefined,
      cloudBaseUrl: ui.asrCloudBase ? ui.asrCloudBase.value.trim() : undefined,
      apiKey: ui.asrCloudKey ? ui.asrCloudKey.value.trim() : undefined,
    };
    if (ui.asrSaveState) ui.asrSaveState.textContent = '保存中…';
    void api
      .setAsr(payload)
      .then((r) => {
        if (r && r.ok === false) {
          if (ui.asrSaveState) ui.asrSaveState.textContent = `⚠️ ${r.message || '保存失败'}`;
          return;
        }
        // 密钥输入框清空 —— 明文不该留在界面上
        if (ui.asrCloudKey) ui.asrCloudKey.value = '';
        renderAsr(r && r.config);
        loadAsrInfo();
        if (ui.asrSaveState) {
          const hint = payload.mode === 'local' ? '（本地模型首次加载要几十秒）' : '';
          ui.asrSaveState.textContent = `✅ 已保存并立即生效 ${hint}`;
        }
      })
      .catch((e) => {
        if (ui.asrSaveState) ui.asrSaveState.textContent = `⚠️ 保存失败：${e.message}`;
      });
  });
}

/**
 * 填"模型下载地址 / 放哪儿 / 现在有没有"这一块（2026-10-19 用户要求）。
 *
 * 为什么要显示**目录**而不只是地址：用户把模型下到 Downloads 里，
 * 程序永远找不到 —— 而他不知道自己放错了。所以把真实路径摆出来，
 * 再给一个"打开这个文件夹"的按钮。
 *
 * 判断"有没有"由主进程做（它才知道真实的 asr-models 在哪），
 * 渲染层只负责显示。
 */
function renderAsrInfo(info) {
  if (!info) return;
  if (ui.asrModelMirror && info.mirrorUrl) ui.asrModelMirror.textContent = info.mirrorUrl;
  if (ui.asrModelOfficial && info.officialUrl) ui.asrModelOfficial.textContent = info.officialUrl;
  if (ui.asrModelDir && info.dir) ui.asrModelDir.textContent = info.dir;
  if (ui.asrModelState) {
    if (info.present && info.models && info.models.length) {
      ui.asrModelState.textContent = `✅ 已找到本地模型：${info.models.join('、')}（不用再下载）`;
    } else if (info.missingDir) {
      ui.asrModelState.textContent = '还没有模型。首次用本地识别时会自动从镜像下载（约 1.5GB），也可以按上面的地址手动下。';
    } else {
      ui.asrModelState.textContent = '还没有模型。首次用本地识别时会自动从镜像下载（约 1.5GB）。';
    }
  }
}

function loadAsrInfo() {
  void api
    .asrInfo()
    .then((info) => renderAsrInfo(info))
    .catch(() => {
      if (ui.asrModelState) ui.asrModelState.textContent = '（读不到模型目录信息）';
    });
}

if (ui.asrOpenDir) {
  ui.asrOpenDir.addEventListener('click', () => {
    void api.openAsrDir().catch(() => {});
  });
}

if (ui.asrCopyCmd) {
  ui.asrCopyCmd.addEventListener('click', () => {
    // 命令里带 --cache-dir：那才会生成 models--…/snapshots/… 的缓存结构，
    // 用 --local-dir 下出来是平铺目录，程序会当成"没有模型"。
    const dir = ui.asrModelDir ? ui.asrModelDir.textContent.trim() : '';
    const cmd =
      `hf download mobiuslabsgmbh/faster-whisper-large-v3-turbo --cache-dir "${dir}"`;
    void navigator.clipboard
      .writeText(cmd)
      .then(() => {
        if (ui.asrModelState) ui.asrModelState.textContent = `已复制下载命令：${cmd}`;
      })
      .catch(() => {
        if (ui.asrModelState) ui.asrModelState.textContent = cmd;
      });
  });
}

void api.asrConfig().then((c) => renderAsr(c)).catch(() => {});
loadAsrInfo();

/* ------------------------------------------------------------ 文字转语音 */
/*
 * 用户要求 2026-10-09：「一个是语音识别，一个是文字转语音，这两个标号」。
 *
 * ## ⚠️ 音色名两条路线**完全不同**
 *
 *     本地 windows -> SAPI 的名字，如 `Microsoft Huihui Desktop`
 *     云端 openai  -> **`模型名:音色名`**，如 `FunAudioLLM/CosyVoice2-0.5B:alex`
 *
 * 实测（直接打 API）：只写 `alex` 会被拒（`400 Invalid voice`，code 20047）。
 * 所以这里按 provider 切换输入框，不能共用一个。
 */

/**
 * 把音量滑杆的值同步到右边的数字（以及"静音"提示）。
 *
 * 2026-10-19 加。为什么单独一个函数：`renderTts`（配置回填）和滑杆的
 * `input` 事件都要调它 —— 两处各写一遍迟早不一致。
 */
function syncTtsVolumeLabel() {
  if (!ui.ttsVolume) return;
  const raw = Number(ui.ttsVolume.value);
  const volume = Math.max(0, Math.min(100, Math.round(Number.isFinite(raw) ? raw : 100)));
  if (ui.ttsVolumeValue) ui.ttsVolumeValue.textContent = String(volume);
  if (ui.ttsVolumeHint) {
    ui.ttsVolumeHint.textContent = volume === 0 ? '静音：只显示文字，不出声' : '';
  }
}

function renderTts(cfg) {
  if (!cfg) return;
  // ⚠️ 用户动过之后就不再覆盖控件（见 ttsTouched 的说明）；
  // 但 state/note 这些只读文字照常刷新。
  if (!ttsTouched && ui.ttsProvider) {
    /*
     * ⚠️ **值不在选项里时要保留它**，不能静默变空（2026-10-09 修）。
     *
     * 给 `<select>` 赋一个**不存在**的值时，浏览器会把 `.value` 重置成空串
     * （和音色下拉框踩的是同一个坑，见 docs 第五十九节）。
     *
     * 现在界面上删掉了「本地」选项，所以配置里是 `windows` 时：
     *   · 下拉框显示**空白**（用户根本看不出当前是什么）
     *   · 状态行显示模板里的默认文案（误导）
     *   · 更糟：用户随便点一下保存就把 `provider` **静默改掉**了
     *
     * 所以补一个说明性的临时选项并选中它。
     */
    const want = cfg.provider || 'none';
    const has = [...ui.ttsProvider.options].some((o) => o.value === want);
    if (!has) {
      const o = document.createElement('option');
      o.value = want;
      o.textContent =
        want === 'windows'
          ? '本地（Windows 自带语音）—— 已不可用，请选云端'
          : `${want}（配置里的，不在选项列表）`;
      ui.ttsProvider.appendChild(o);
    }
    ui.ttsProvider.value = want;
  }
  if (ui.ttsCloudHint) ui.ttsCloudHint.textContent = cfg.cloudVoiceHint || '';
  /*
   * ⚠️ 两个输入框分别回填**各自**的值（2026-10-09）。
   *
   * 以前共用一个 `cfg.voice`，切 provider 时会把一个格式的名字填进另一个框，
   * 保存后云端就 400（实测）。现在主进程按 provider 返回对应的值。
   */
  if (!ttsTouched && ui.ttsCloudVoice) {
    /*
     * 配置里存的是完整格式 `模型名:音色名`，而下拉框的 option.value 只是
     * **音色名**（`alex`）。所以回填时要把模型前缀剥掉。
     *
     * ⚠️ 按**最后一个 `:`** 切：模型名里理论上可能带冒号（比如带版本标签），
     * 而音色名是固定的简单单词，从右边切最稳。
     */
    const full = cfg.cloudVoice || '';
    const idx = full.lastIndexOf(':');
    /*
     * ⚠️ 用**局部变量**承载目标值，不要"先赋值再读 `.value`"（2026-10-09 实测）。
     *
     * 给 `<select>` 赋一个**不存在**的值时，浏览器会把 `.value` 重置成空串
     * （`selectedIndex = -1`）。所以"先赋值、再拿 `.value` 判断"这条路是死的：
     * 判断时它已经是空串，临时选项**永远加不上**。
     *
     * 正确顺序：算值 -> 检查选项 -> 缺就补 -> 最后才赋值。
     */
    const want = idx >= 0 ? full.slice(idx + 1) : full;
    if (want) {
      const has = [...ui.ttsCloudVoice.options].some((o) => o.value === want);
      if (!has) {
        /*
         * 配置里的音色**不在预设列表**（用户手改过配置、或服务商换了音色）。
         *
         * 必须做成一个临时选项 —— 否则 select 会静默变成第一个选项，
         * 用户一点保存就把原来的音色**悄悄改掉**了（而且他看不出来）。
         */
        const o = document.createElement('option');
        o.value = want;
        o.textContent = `${want}（配置里的，不在预设列表）`;
        ui.ttsCloudVoice.appendChild(o);
      }
      ui.ttsCloudVoice.value = want;
    }
  }
  // 记住云端模型名 —— 保存时要用它拼 `模型名:音色名`
  ttsCloudModel = cfg.cloudModel || '';
  syncTtsRows(cfg);
  renderTtsState(cfg);
}

/** 只在相关模式下显示对应的行 */
function syncTtsRows(cfg) {
  const p = (cfg && cfg.provider) || ui.ttsProvider?.value || 'none';
  /*
   * ⚠️ 本机音色那一行已经删了（2026-10-09），所以只剩云端这一行要显隐。
   *
   * 原来这里有个 `show(...)` 局部函数，我在删"本机音色"那行时**把它一起删掉了**，
   * 但没删这一句调用 —— 于是 `show` 未定义、`syncTtsRows` 抛错，
   * **连带后面的 `renderTtsState` 也不执行**（状态行永远停在模板默认值「未启用」）。
   *
   * 这个 bug 特别难查，因为调用处是：
   *
   *     void api.ttsConfig().then((c) => { renderTts(c); }).catch(() => {});
   *
   * `.catch(() => {})` **把异常静默吞了** —— 控制台一条不报。
   * 我为此绕了好几轮（改 DOM、抓 scriptSource、装 error 钩子都没用），
   *
   * 教训：**排查用的 `.catch(() => {})` 会把真 bug 藏起来** ——
   * 至少写成 `.catch((e) => console.error(...))`。
   */
  if (ui.ttsCloudRow) ui.ttsCloudRow.style.display = p === 'openai' ? '' : 'none';
  /*
   * edge 那一行同理（2026-10-11 加）。
   *
   * ⚠️ 两个下拉框装着**格式完全不同**的音色名，同时显示会让用户
   * 把 `alex` 填进 edge（或把 `zh-CN-XiaoxiaoNeural` 填进云端），
   * 然后得到 `400 Invalid voice` —— 而用户完全不知道是格式问题。
   */
  if (ui.ttsEdgeRow) ui.ttsEdgeRow.style.display = p === 'edge' ? '' : 'none';
}

function renderTtsState(cfg) {
  if (!ui.ttsState) return;
  if (!cfg) {
    ui.ttsState.textContent = '（读不到配置）';
    return;
  }
  if (cfg.provider === 'openai') {
    // 隐私提示写在这里：用户看到"给谁说"比看到模型名更重要
    ui.ttsState.textContent = `当前：云端 ${cfg.cloudModel}（⚠️ 宠物要说的话会上传到硅基流动）`;
  } else if (cfg.provider === 'edge') {
    /*
     * ⚠️ 隐私提示**必须写**（2026-10-11 加）。
     *
     * `edge` 是"免费"，很容易被理解成"本地" —— 但它**仍然联网**，
     * 文本会发到微软。这个项目在别处（本地 ASR）明确标过"语音不出本机"，
     * 那这里就必须把相反的话说清楚，否则两处口径不一致、用户会误判。
     */
    ui.ttsState.textContent =
      `当前：edge-tts ${cfg.edgeVoice || ''}（免费，但⚠️ 要说的话会上传到微软）`;
  } else if (cfg.provider === 'windows') {
    /*
     * ⚠️ 界面上**已经没有「本地」这个选项**了（2026-10-09 按用户要求删掉），
     * 但配置里可能还留着 `provider: windows`（手改过 YAML、或从旧版本升级）。
     *
     * 这种情况必须**如实说明**：本地在这个环境用不了（统计过 0/28，
     * 见 docs 第六十二节），用户点试听会没声音。不写清楚他又要来问。
     */
    ui.ttsState.textContent =
      '当前：本地 Windows 语音 —— ⚠️ 这个环境用不了本地合成（选「云端 API」才有声音）';
  } else {
    ui.ttsState.textContent = '当前：不出声（只显示文字）';
  }

  // 音量（2026-10-19）：用户拖过之后就别再覆盖他的值，和其它控件一个规矩
  if (!ttsTouched && ui.ttsVolume) {
    const volume = Number.isFinite(Number(cfg.volume)) ? Number(cfg.volume) : 100;
    ui.ttsVolume.value = String(Math.max(0, Math.min(100, Math.round(volume))));
  }
  syncTtsVolumeLabel();
}

/**
 * 本机音色是否已经拉过、以及配置里的本地音色名。
 *
 * ⚠️ 这两个变量**已经不用了** —— 2026-10-09 删掉「本地」这个合成方式之后，
 * 枚举本机音色的 `loadTtsVoices()` 也一起删了。
 *
 * 这里保留这段记录是因为它记着一个**很隐蔽的坑**，值得以后的人知道：
 *
 * 原来 `let ttsVoicesLoaded` 写在 `ui.ttsProvider.addEventListener(...)`
 * **后面**，而监听器里引用了它 —— 那是 **TDZ（暂时性死区）**：`let` 不提升，
 * 事件触发时变量还没初始化，于是抛
 *
 *     ReferenceError: Cannot access 'ttsVoicesLoaded' before initialization
 *
 * 后果很隐蔽：**监听器一抛就中断**，所以同一个回调里排在它**前面**那句
 * `syncTtsRows(...)` 的显隐也没生效 —— 表现是「切了合成方式但下面的行不变」，
 * 看起来完全不像"变量声明顺序"问题。
 */

/**
 * 云端模型名（从配置读）。
 *
 * 服务端要求音色写成 `模型名:音色名`，而下拉框里只有音色名 ——
 * 保存时用它拼前缀。**不写死**：用户可能换服务商或换模型。
 */
let ttsCloudModel = '';

/**
 * 用户是否动过 TTS 的控件。
 *
 * ⚠️ 动过之后，**后台推送不再覆盖控件的值**（2026-10-09 修）。
 *
 * `renderTts` 会被 `settings:changed` 触发（主进程探完本机 SAPI 会推一次），
 * 而它原来无条件写 `ui.ttsProvider.value = cfg.provider`。于是：
 *
 *     用户切到「本地」-> 触发音色枚举（异步约 2s）
 *       -> 枚举回来调 renderTts -> 下拉框**弹回**配置里的旧值
 *       -> 点保存写回去的还是旧值  ->  "改了没生效"
 *
 * 和 `promptToggleTouched` 是同一类问题（那里已经有注释记过一次了）。
 * 状态**文字**照常刷新 —— 它是只读的，没有覆盖用户输入的风险。
 */
let ttsTouched = false;

if (ui.ttsProvider) {
  ui.ttsProvider.addEventListener('change', () => {
    ttsTouched = true;
    syncTtsRows({ provider: ui.ttsProvider.value });
    // （原来这里会在切到 'windows' 时枚举本机音色；本地选项已删，不需要了）
  });
}

/*
 * ⚠️ 这里原来有 `loadTtsVoices()` —— 枚举本机 Windows 音色填进下拉框。
 *
 * 2026-10-09 按用户要求删掉了「本地」这个合成方式，所以整块一起去掉。
 * 本地方案在这个环境**确定用不了**（统计过 0/28，见 docs 第六十二节），
 * 留着枚举列表反而误导（"能列出"不代表"能用"）。
 *
 * 主进程的 `settings:tts-voices` 通道还留着 —— 排查时有用，
 * 用 `tools/probe-sapi-*.mjs` 那类脚本调得到。
 */

/**
 * 把界面上的 TTS 设置存下去。返回**是否成功**。
 *
 * 抽成函数是因为**试听也要用它**（见 `ttsTest` 里的说明）：
 * 试听必须基于"已经保存的音色"，否则用户听到的是上一次的。
 */
/**
 * 把界面上的 TTS 设置存下去。返回**是否成功**。
 *
 * @param {{quiet?: boolean}} [opts] quiet = 不写"保存中…/已保存"那两行状态
 *   （试听时会自己写状态，两处一起写会闪）
 * @returns {Promise<boolean>}
 */
async function saveTts(opts) {
  const p = ui.ttsProvider ? ui.ttsProvider.value : 'none';
  const payload = { provider: p };
  if (p === 'openai') {
    /*
     * 下拉框里只有音色名（`alex`），但服务端**要求** `模型名:音色名` 的完整格式
     * （只写 `alex` 会 400 Invalid voice，实测）。
     * 模型名从主进程读回来的配置里拿（`cfg.cloudModel`），不写死。
     */
    const picked = ui.ttsCloudVoice ? ui.ttsCloudVoice.value.trim() : '';
    const model = ttsCloudModel || 'FunAudioLLM/CosyVoice2-0.5B';
    // 已经是完整格式就别重复加前缀
    payload.voice = picked.includes(':') ? picked : `${model}:${picked}`;
  } else if (p === 'edge') {
    /*
     * edge 的音色是**裸名字**（`zh-CN-XiaoxiaoNeural`），**不加前缀**。
     *
     * ⚠️ 刻意不复用上面那段"拼模型前缀"的逻辑 —— 两条路的格式**相反**：
     *     云端  **必须**带前缀（只写 `alex` -> 400 Invalid voice）
     *     edge  **必须不带**（写 `xxx:yyy` -> Invalid voice）
     * 共用一段代码必然错一边。
     */
    const picked = ui.ttsEdgeVoice ? ui.ttsEdgeVoice.value.trim() : '';
    if (picked) payload.voice = picked;
  }
  // （原来这里还有 `p === 'windows'` 分支取本机音色；本地选项已删，不需要了）

  /*
   * 音量（2026-10-19 加）。
   *
   * ⚠️ 无论哪种合成方式都带这个字段 —— 它是**播放端增益**，与合成引擎无关。
   * 主进程会把它夹到 0..100 再写进 persona.yaml 的 `providers.tts.volume`。
   */
  if (ui.ttsVolume) {
    const raw = Number(ui.ttsVolume.value);
    payload.volume = Math.max(0, Math.min(100, Math.round(Number.isFinite(raw) ? raw : 100)));
  }

  if (!opts?.quiet && ui.ttsSaveState) ui.ttsSaveState.textContent = '保存中…';
  try {
    const r = await api.ttsSet(payload);
    if (r && r.ok === false) {
      if (ui.ttsSaveState) ui.ttsSaveState.textContent = `⚠️ ${r.message || '保存失败'}`;
      return false;
    }
    // 保存成功后把"动过"的标记清掉 —— 让界面重新与配置对齐
    ttsTouched = false;
    renderTts(r && r.config);
    if (!opts?.quiet && ui.ttsSaveState) {
      ui.ttsSaveState.textContent = '✅ 已保存并立即生效';
    }
    return true;
  } catch (e) {
    if (ui.ttsSaveState) ui.ttsSaveState.textContent = `⚠️ 保存失败：${e.message}`;
    return false;
  }
}

if (ui.ttsSave) {
  ui.ttsSave.addEventListener('click', () => {
    void saveTts();
  });
}

/*
 * 音量滑杆（2026-10-19 加）。
 *
 * `input` 而不是 `change`：拖动时数字要跟着动（用户要求"可调节声音大小"，
 * 拖的过程中看不到数值就很难调准）。真正落盘还是靠「保存」按钮 ——
 * 每拖一格就写一次 persona.yaml 会把 YAML 写花（尤其是并行点击时）。
 */
if (ui.ttsVolume) {
  ui.ttsVolume.addEventListener('input', () => {
    ttsTouched = true;
    syncTtsVolumeLabel();
  });
}

if (ui.ttsTest) {
  ui.ttsTest.addEventListener('click', () => {
    /*
     * ⚠️ 试听走**真实链路**（协议 voice.say -> sidecar 的
     * `VoiceSession.speak()`），不是在这里直接合成。
     *
     * 原因：本地 SAPI 在主进程里「能枚举音色、却一个都取不到」。
     * 如果试听走主进程自己合成，会得出"能用"的**错误结论**。
     * 走真实链路的话，"试听成功"就等于"宠物真能出声"。
     *
     * 所以这里只负责发请求 + 提示"注意听"，结果由耳朵判断 ——
     * 合成失败的原因会出现在日志里（`语音合成失败：…`）。
     */
    /*
     * ⚠️ **先把当前选择存下去，再试听**（2026-10-09 修）。
     *
     * 试听只发**文字**，音色由引擎（`OpenAITtsEngine.voice`）在创建时从配置读。
     * 所以"选了新音色但没保存就点试听"会听到**旧音色** ——
     * 用户看到的现象又是"换了没变化"。
     *
     * 用户的自然操作顺序就是"选音色 -> 点试听"，所以这条路必须自己把保存补上。
     */
    if (ui.ttsSaveState) ui.ttsSaveState.textContent = '正在应用音色…';
    void (async () => {
      const saved = await saveTts({ quiet: true });
      if (!saved) return; // 保存失败时 saveTts 已经把原因写在状态行了

      /*
       * ⚠️ 等一小会儿再试听：`config.update` 是异步送到 Python 的，
       * 它要重建 `OpenAITtsEngine`（新建对象、读新配置）。
       * 立刻发 `voice.say` 会赶上**旧引擎**，听到的还是旧音色。
       *
       * 重建本身很快（云端引擎是纯 HTTP 客户端，不加载模型），600ms 足够；
       * 给太久会让用户觉得"点了没反应"。
       */
      await new Promise((done) => setTimeout(done, 600));

      if (ui.ttsSaveState) ui.ttsSaveState.textContent = '已请求合成，注意听声音…';
      try {
        /*
         * 试听文本里**带上音色名和特点**。
         *
         * 原来固定念「你好呀，我是小灵，现在能听到我说话吗？」——
         * 8 个音色听起来都差不多，用户没法对比。带上名字和特点之后，
         * 一听就知道我现在试的是哪个，也更容易判断喜不喜欢。
         */
        const label =
          ui.ttsCloudVoice && ui.ttsCloudVoice.selectedOptions.length
            ? ui.ttsCloudVoice.selectedOptions[0].textContent.trim()
            : '';
        const who = label || '这个音色';
        const r = await api.ttsTest({
          text: '你好呀，我是小灵。现在用的是' + who + '，你觉得好听吗？',
        });
        if (ui.ttsSaveState) {
          ui.ttsSaveState.textContent =
            r && r.ok === false
              ? `⚠️ ${r.message}`
              : '✅ 已发给宠物播放（合成失败的原因在日志里）';
        }
      } catch (e) {
        if (ui.ttsSaveState) ui.ttsSaveState.textContent = `⚠️ 试听失败：${e.message}`;
      }
    })();
  });
}

/**
 * 把 edge 音色清单填进下拉框（2026-10-11 加）。
 *
 * ## ⚠️ 为什么要单独一个函数
 *
 * `renderTts` 在用户动过控件后**刻意不再覆盖值**（`ttsTouched`）。
 * 而音色清单是**选项列表**、不是值 —— 它必须无条件填好，否则下拉框是空的、
 * 用户根本没东西可选。两件事的生命周期不同，所以分开。
 *
 * ## ⚠️ 当前配置的音色不在清单里时要补一个
 *
 * 给 `<select>` 赋一个**不存在**的值时，浏览器会把 `.value` 重置成空串
 * （`selectedIndex = -1`）。后果：
 *   · 界面显示为空 -> 用户以为没设置
 *   · 他一点保存，空值被提交 -> **配置被静默改掉**
 *
 * 这个坑在云端音色上下过（docs 第五十九节记得很细：连"先赋值再读 `.value`
 * 来判断"这条路都是死的，因为读的时候它已经被重置了）。所以这里也照同样的
 * 方式处理：**先算目标值，再补选项，最后赋值**。
 *
 * @param voices 主进程给的清单（可能失败/为空）
 * @param want   配置里当前的音色名
 */
function fillEdgeVoices(voices, want) {
  if (!ui.ttsEdgeVoice) return;
  const list = Array.isArray(voices?.voices) ? voices.voices : [];
  const hasOptions = ui.ttsEdgeVoice.options.length > 0;
  if (!hasOptions && list.length) {
    for (const v of list) {
      const o = document.createElement('option');
      o.value = String(v.name || '');
      o.textContent = String(v.label || v.name || '');
      if (o.value) ui.ttsEdgeVoice.appendChild(o);
    }
  }
  const target = String(want || '').trim() || 'zh-CN-XiaoxiaoNeural';
  const exists = [...ui.ttsEdgeVoice.options].some((o) => o.value === target);
  if (!exists) {
    const o = document.createElement('option');
    o.value = target;
    o.textContent = `${target}（配置里的，不在常用清单）`;
    ui.ttsEdgeVoice.appendChild(o);
  }
  ui.ttsEdgeVoice.value = target;
  if (ui.ttsEdgeHint) {
    ui.ttsEdgeHint.textContent = list.length
      ? `共 ${list.length} 个常用音色；也可以手改配置里的 edge_voice 用别的`
      : '（清单没读到 —— 下拉框里只有当前配置的那个音色）';
  }
}

void api
  .ttsConfig()
  .then((c) => {
    renderTts(c);
    /*
     * edge 音色清单（2026-10-11 加）。
     *
     * ⚠️ 和配置**分开加载**：音色清单来自主进程的静态表（拿不到也不该让
     * 配置渲染失败）。所以这里独立 `then`，失败只记日志。
     *
     * ⚠️ 清单里**没有**当前配置的音色时要补一个临时选项 ——
     * 否则 `<select>` 赋一个不存在的值会被浏览器重置成空串，
     * 用户看到"音色是空的"，一保存就把配置**静默改掉**了
     * （这个坑在云端音色上踩过，见 docs 第五十九节）。
     */
    return api
      .edgeVoices()
      .then((voices) => fillEdgeVoices(voices, c && c.edgeVoice))
      .catch((e) => console.error('[设置页] 读取 edge 音色清单失败：', e));
  })
  .catch((e) => {
    /*
     * ⚠️ **不要写成 `.catch(() => {})`**（2026-10-09 血的教训）。
     *
     * 这里原来是空 catch。而 `renderTts` 内部的 `syncTtsRows(cfg)` 抛了
     * 「`show` is not defined」（我删"本机音色"那行时把 `show` 一起删了、
     * 却留下调用）—— 异常被这个空 catch **静默吞掉**：
     *
     *   · 控制台一条不报
     *   · `renderTtsState` 因此不执行，状态行永远停在模板默认值「未启用」
     *   · 装 error 钩子、抓 scriptSource 全都查不到
     *
     * 我绕了好几轮，最后靠往函数里塞计数器（`window.__rt` / `window.__rts`）
     * 才定位到。**空 catch 会把真 bug 藏起来。**
     */
    console.error('[设置页] 读取语音合成配置失败：', e);
  });

/* -------------------------------------------------------------- 快捷键 */

/**
 * 渲染快捷键一览（2026-10-12）。
 *
 * ## 为什么从主进程读，而不是在这里写死
 *
 * `src/main/shortcuts.ts` 的 `SHORTCUTS` 是**唯一定义源**（注册也从它读）。
 * 页面自己抄一份就多一处会漂移的副本 —— 而"界面写着 Ctrl+Alt+M、
 * 实际从没注册过"是这个项目踩过的真事故：用户按下去毫无反应、也不报错，
 * 只会以为是自己操作错了或者程序坏了。
 *
 * ## 为什么单独一个请求，不并进配置那条链
 *
 * 快捷键是**静态信息**（不随配置变），拿不到也不该让配置渲染失败。
 * 与 edge 音色清单同一个取舍：独立加载，失败只记日志。
 */
function renderShortcuts(payload) {
  const list = ui.shortcutList;
  if (!list) return;
  const items = (payload && payload.shortcuts) || [];
  list.textContent = '';
  if (items.length === 0) {
    const empty = document.createElement('span');
    empty.className = 'shortcut-empty';
    empty.textContent = '（没读到快捷键清单）';
    list.appendChild(empty);
    return;
  }
  for (const item of items) {
    const key = document.createElement('span');
    key.className = 'shortcut-key';
    key.textContent = item.display || item.accelerator || '';
    const desc = document.createElement('span');
    desc.className = 'shortcut-desc';
    desc.textContent = item.description || '';
    // 顺序要紧：grid 是按 DOM 顺序排的，先键位后说明才会排成两列
    list.appendChild(key);
    list.appendChild(desc);
  }
}

api
  .shortcuts()
  .then(renderShortcuts)
  .catch((e) => console.error('[设置页] 读取快捷键清单失败：', e));

/* -------------------------------------------------------------- 开机自启 */

/**
 * 开机自启（2026-10-12）。
 *
 * ## 为什么"写完要读回来核对"
 *
 * `setLoginItemSettings` 在某些环境下**静默失败**（组策略、权限）。
 * 不核对的话界面上的勾会一直显示"已设置"，而重启后什么都没发生 ——
 * 典型的假开关。所以主进程那边核对过，这里据 `ok` 决定提示语。
 *
 * ## ⚠️ 开发模式的提示
 *
 * 开发模式下 `process.execPath` 是 `electron.exe`，所以开机自启会启动一个
 * **裸 Electron**。主进程会带 `devWarning` 回来，这里如实告诉用户，
 * 而不是让他以为"设好了但没用"。
 */
function renderLoginItem(payload) {
  const on = Boolean(payload && payload.enabled);
  if (ui.loginItem) ui.loginItem.checked = on;
  if (!ui.loginItemState) return;
  if (payload && payload.devWarning) {
    ui.loginItemState.textContent = on
      ? '已设到系统里。⚠️ 但这是开发模式，开机启动的是 Electron 本身（不是宠物）—— 打包后才对。'
      : '⚠️ 开发模式下这个开关只用于验证链路，打包后才有实际效果。';
  } else {
    ui.loginItemState.textContent = on ? '已开启：开机后会自动启动。' : '';
  }
}

api
  .loginItem()
  .then(renderLoginItem)
  .catch((e) => console.error('[设置页] 读开机自启状态失败：', e));

if (ui.loginItem) {
  ui.loginItem.addEventListener('change', () => {
    const want = ui.loginItem.checked;
    if (ui.loginItemState) ui.loginItemState.textContent = '正在设置…';
    api
      .setLoginItem(want)
      .then((result) => {
        // 以**系统实际的状态**为准回写勾 —— 不是以我们想设的值为准
        const payload = result || {};
        if (ui.loginItem) ui.loginItem.checked = Boolean(payload.enabled);
        if (ui.loginItemState) {
          const msg = String(payload.message || (payload.ok ? '已设置' : '设置失败'));
          /*
           * ⚠️ 失败要**显式说出来**（红字），不能只把勾改回去 ——
           * 用户会以为"点了没反应"。这个项目里"说了却没做到"比做不到更糟。
           */
          ui.loginItemState.textContent = payload.ok ? msg : `❌ ${msg}`;
        }
        if (!payload.ok) console.error('[设置页] 设置开机自启失败：', msg);
      })
      .catch((e) => {
        if (ui.loginItem) ui.loginItem.checked = !want;
        if (ui.loginItemState) ui.loginItemState.textContent = `❌ 设置失败：${e}`;
        console.error('[设置页] 设置开机自启失败：', e);
      });
  });
}


/* -------------------------------------------------------------- 浏览器兜底 */

/**
 * 浏览器兜底（2026-10-13 用户要求）。
 *
 * ## 它做什么
 *
 * 有些站（最典型是**起点中文网**）用「要执行 JS 的 WAF 挑战」挡住抓取。
 * 打开这一项后，宠物读不动的页面会改用**一个隐藏的浏览器窗口**去读。
 *
 * ## ⚠️ 为什么要在这里解释代价，而不只是一个勾
 *
 * 它和「联网搜索」不是一个量级的东西：
 *
 * ```
 * 联网搜索   把查询发给搜索引擎          （信息出去）
 * 浏览器兜底 **以你的身份浏览那个网站**   （带真 UA、留 cookie、留会话）
 * ```
 *
 * 用户点这个勾之前必须知道自己在同意什么 —— 所以 `settings.html` 里
 * 那两段说明是**功能的一部分**，不是文案装饰。
 *
 * ⚠️ 还有一条代价是**慢**：每次读不动都要多等 15~25 秒
 * （等 WAF 挑战跑完 —— 那是挑战本身的耗时，不是实现问题）。
 * 状态提示里要写上，否则用户会觉得"开了之后它变卡了"。
 */
function renderBrowserFallback(on) {
  const value = Boolean(on);
  if (ui.browserFallback) ui.browserFallback.checked = value;
  if (ui.browserState) {
    ui.browserState.textContent = value
      ? '已开启：读不动的网页会用隐藏浏览器重试（每个页面多等 15~25 秒）。'
      : '已关闭：有反爬验证的网站（比如起点）读不了。';
  }
}

api
  .browserFallback()
  .then(renderBrowserFallback)
  .catch((e) => console.error('[设置页] 读浏览器兜底状态失败：', e));

if (ui.browserFallback) {
  ui.browserFallback.addEventListener('change', () => {
    const want = ui.browserFallback.checked;
    if (ui.browserState) ui.browserState.textContent = '正在保存…';
    api
      .setBrowserFallback(want)
      .then(() => {
        renderBrowserFallback(want);
      })
      .catch((e) => {
        // ⚠️ 失败要把勾改回去**并说出来** —— 只改勾的话用户会以为"点了没反应"
        if (ui.browserFallback) ui.browserFallback.checked = !want;
        if (ui.browserState) ui.browserState.textContent = `❌ 保存失败：${e}`;
        console.error('[设置页] 设置浏览器兜底失败：', e);
      });
  });
}


/* ----------------------------------------------------- 清空宠物的浏览记录 */

/**
 * 清空宠物那份浏览数据（2026-10-13 用户要求）。
 *
 * ## 用户的原话
 *
 * > 「清楚的是我**浏览器**上的缓存吗」
 *
 * 那个疑问就是界面的验收标准：**必须让人一眼确认清的是宠物自己那份。**
 * 所以这里做三件事（而不是只弹个"确定吗"）：
 *
 * ```
 * ① 显示**真实磁盘路径** —— 说"不会动你的浏览器"不如把路径摆出来
 * ② 显示**实际占用体积** —— 让用户知道在清多少
 * ③ 清完**列出清了什么** —— 不是笼统的"已清空"
 * ```
 *
 * ## ⚠️ 为什么"查体积"和"清空"必须是两次调用
 *
 * 合成一次的话，**每次打开设置页都会清一次** —— 那是个灾难性的设计
 * （用户只是来看看，结果浏览记录没了、下次读网页又慢回 15 秒）。
 */
function renderBrowserDataSize(payload) {
  const info = payload || {};
  const total = String(info.text || '0 B');
  const cookies = Number(info.cookies || 0);
  const path = String(info.path || '');
  if (!ui.browserDataSize) return;
  /*
   * 🔴 主信号是 **cookie 条数**，不是体积（2026-10-13，被实测逼出来的）。
   *
   * ## 为什么
   *
   * 第一版我显示"浏览记录 308 KB（框架开销 2.2 MB）"—— 那**误导人**：
   *
   * ```
   * ② 已清空（还剩 0 个站点的 cookie）
   * ③ 体积 2.52 MB -> 2.53 MB   ⚠️ "没变小"
   * ```
   *
   * 体积没变小，是因为那个**隐藏窗口一直活着**（空闲页面），
   * Chromium 会不断重建缓存块 —— 那 300 KB 是**噪音**，不是残留的记录。
   * 而"还剩几个站点的 cookie"是**真实的、干净的**信号：清完就是 0。
   *
   * ⚠️ 所以：**报一个用户能验证的信号，而不是一个看起来精确的数字。**
   * 体积只作为"磁盘占用"的背景信息放在括号里。
   */
  ui.browserDataSize.textContent =
    cookies > 0
      ? `还留着 ${cookies} 个站点的 cookie　（磁盘占用 ${total}）`
      : `没有留下 cookie　（磁盘占用 ${total}，其中大部分是浏览器的固定开销）`;
  ui.browserDataSize.title = path ? `存在：${path}` : '';
}

api
  .browserDataSize()
  .then(renderBrowserDataSize)
  .catch((e) => {
    if (ui.browserDataSize) ui.browserDataSize.textContent = '（读不到占用）';
    console.error('[设置页] 读浏览数据占用失败：', e);
  });

if (ui.clearBrowserData) {
  ui.clearBrowserData.addEventListener('click', () => {
    /*
     * ⚠️ 清空是**不可撤销**的，所以先问一句 ——
     * 而且问题里说清"只清宠物那份"，那正是用户最担心的地方。
     */
    const yes = window.confirm(
      '清空宠物自己那份浏览记录？\n\n' +
        '· 只清宠物用隐藏浏览器读网页时留下的 cookie 和缓存\n' +
        '· 你平时用的 Chrome / Edge 完全不受影响（它们存在另外的目录）\n' +
        '· 清完之后，下次读那类网站要重新过一次验证（会慢一点）',
    );
    if (!yes) return;

    if (ui.browserDataState) ui.browserDataState.textContent = '正在清空…';
    if (ui.clearBrowserData) ui.clearBrowserData.disabled = true;
    api
      .clearBrowserData()
      .then((payload) => {
        const result = payload || {};
        const message = String(result.message || (result.ok ? '已清空' : '清空失败'));
        const cleared = Array.isArray(result.cleared) ? result.cleared.join('、') : '';
        if (ui.browserDataState) {
          /*
           * ⚠️ 失败要**显式说出来**（红字），成功也要说清"清了什么" ——
           * 笼统的"已清空"没法验证，而这个功能的价值就在于"看得见"。
           */
          ui.browserDataState.textContent = result.ok
            ? `✅ ${message}${cleared ? `　清了：${cleared}` : ''}`
            : `❌ ${message}`;
        }
        // 清完刷新占用显示（那才是"真的清了"的证据）
        return api.browserDataSize().then(renderBrowserDataSize);
      })
      .catch((e) => {
        if (ui.browserDataState) ui.browserDataState.textContent = `❌ 清空失败：${e}`;
        console.error('[设置页] 清空浏览数据失败：', e);
      })
      .finally(() => {
        if (ui.clearBrowserData) ui.clearBrowserData.disabled = false;
      });
  });
}


/* ------------------------------------------------- 试读一个网址（诊断） */

/**
 * 试读一个网址（2026-10-13）。
 *
 * ## 为什么这个按钮对用户有用
 *
 * 「浏览器兜底」这个开关的**效果取决于目标站** —— 有的站能过、有的过不了。
 * 光看那个勾，用户没法知道"它对我关心的站到底行不行"。
 *
 * 而这个按钮走的是**和宠物真实读网页完全相同的判据**：
 *
 * ```
 * 先普通抓取 -> 只有被 WAF 挡住（202/403）才用浏览器
 * ```
 *
 * 所以结果可信：显示"普通抓取没被挡"就说明那个站本来就读得了，
 * 显示"用浏览器读到了"就说明兜底真的起作用了。
 *
 * ⚠️ 它也顺带回答了"到底该不该开这个开关" ——
 * 如果试读结果总是"普通抓取没被挡"，那这个开关对他没用，可以关掉。
 */
if (ui.browserProbe) {
  ui.browserProbe.addEventListener('click', () => {
    const url = String((ui.browserProbeUrl && ui.browserProbeUrl.value) || '').trim();
    if (!url) {
      if (ui.browserProbeState) ui.browserProbeState.textContent = '先填一个网址。';
      return;
    }
    if (ui.browserProbeState) {
      ui.browserProbeState.textContent = '正在试读…（要浏览器兜底的话，这一步会等 5~25 秒）';
    }
    ui.browserProbe.disabled = true;
    api
      .browserProbe(url)
      .then((payload) => {
        const result = payload || {};
        const stage = String(result.stage || '');
        if (!ui.browserProbeState) return;
        if (result.ok) {
          /*
           * ⚠️ 报**实际用时**和**正文长度** —— 那才是"真的读到了"的证据。
           * 只说"成功"的话没法区分"读到了 3000 字"和"读到了一个空壳"。
           */
          ui.browserProbeState.textContent =
            `✅ 用浏览器读到了《${String(result.title || '(无标题)')}》` +
            `　正文 ${result.textLength || 0} 字　用时 ${result.elapsedMs || 0} 毫秒` +
            `（普通抓取时是 HTTP ${result.status}）`;
          // 顺带刷新占用（读网页会让 cookie/缓存涨）
          return api.browserDataSize().then(renderBrowserDataSize);
        }
        const reason = String(result.reason || '未知原因');
        ui.browserProbeState.textContent =
          stage === 'plain'
            ? `ℹ️ ${reason}`
            : `❌ 读不了：${reason}`;
      })
      .catch((e) => {
        if (ui.browserProbeState) ui.browserProbeState.textContent = `❌ 试读出错：${e}`;
        console.error('[设置页] 试读失败：', e);
      })
      .finally(() => {
        if (ui.browserProbe) ui.browserProbe.disabled = false;
      });
  });
}


// 打开设置页就枚举一次麦克风（否则下拉框里只有系统默认，用户没法选）
/* -------------------------------------------------------------- 麦克风 */

/**
 * 麦克风选择**已移到宠物右键菜单**（用户要求 2026-10-08：
 * 「麦克风选项移到宠物菜单里面」）。
 *
 * ⚠️ 所以设置页**不再枚举、不再有下拉框** —— 只显示"系统里有几支"，
 * 并告诉用户该去哪儿换。
 *
 * ## 为什么不在两处都放
 *
 * 两个入口各维护一份状态，很快就会分叉（这个项目已经因为"两个真相源"
 * 出过快捷键的事故）。而菜单是更合适的那个入口 —— 麦克风是**宠物**的事，
 * 放在宠物身上的菜单里，用户右键就换，比"进设置 → 找隐私 → 滚到麦克风"
 * 少两步。
 */
async function renderMicState() {
  if (!ui.micState) return;
  try {
    const r = await api.listMics();
    const mics = (r && r.mics) || [];
    ui.micState.textContent = mics.length
      ? `系统里有 ${mics.length} 支麦克风 —— 当前用哪支、要换哪支，都在菜单里选。`
      : '还没枚举到麦克风：先打开上面的语音开关授权一次。';
  } catch {
    ui.micState.textContent = '';
  }
}

void renderMicState();

void load();
// 记忆设置（最近窗口 + 备份）单独拉一次：它来自 persona.yaml，不在通用状态快照里
void loadMemoryConfig();
// 提示词模板同样来自 persona.yaml（清单由主进程给）
void renderPromptTemplates();
// 下拉框套一层（好画自己的箭头）—— 放在最后，确保动态生成的 select 也已存在
wrapSelects();

/* --------------------------------------------------------------- 模块开关 */
/*
 * 阶段二（2026-10-18）：把后端 modules.py 的 11 个模块摆成开关。
 *
 * ⚠️ 两条规矩：
 *   ① 后端说 `gate: false` 的（地基/前端模块）**置灰**，并写清为什么 ——
 *      让用户看到"这个不能关"比让他点了没反应好。
 *   ② 后端说需要重启的（voice），开关照切，但旁边写「需重启」，
 *      不许假装已经生效（这个项目吃过太多次"假开关"的亏）。
 */
async function loadModules() {
  if (!ui.modulesList) return;
  try {
    const reply = await api.modules();
    const list = Array.isArray(reply && reply.modules) ? reply.modules : [];
    if (!reply || !reply.ok) {
      ui.modulesList.textContent = (reply && reply.message) || '拿不到模块状态（大脑可能未启动）';
      return;
    }
    ui.modulesList.innerHTML = '';
    /*
     * 🔴 2026-10-18 重设计：卡片 + 分组 + 自绘开关（旧版是 .field 两列网格 +
     * 原生 checkbox，长提示被挤进窄列、换行稀碎）。
     *   · 「可热切」组：卡片带自绘开关，点了立刻切换（失败回滚，别骗人）；
     *   · 「地基与前端」组：置灰卡片 + 「地基 · 常驻」徽标，没开关可点 ——
     *     让用户看到"这个不能关"比让他点了没反应好（规矩①不变）；
     *   · voice 的「需重启」做成琥珀徽标挂在标题旁（规矩②不变：不许假装生效）。
     * 浅深主题全走 settings.css 的角色变量，不需要单独配色。
     */
    const hot = list.filter((m) => m.gate);
    const base = list.filter((m) => !m.gate);

    const moduleCard = (m, locked) => {
      const card = document.createElement('div');
      card.className = 'module-card' + (locked ? ' locked' : '');
      card.dataset.module = m.name;

      const head = document.createElement('div');
      head.className = 'module-head';

      const name = document.createElement('span');
      name.className = 'module-name';
      name.textContent = m.label || m.name;
      head.appendChild(name);

      if (locked) {
        const badge = document.createElement('span');
        badge.className = 'module-badge';
        badge.textContent = '地基 · 常驻';
        head.appendChild(badge);
      } else if (m.name === 'voice') {
        const badge = document.createElement('span');
        badge.className = 'module-badge restart';
        badge.textContent = '需重启';
        head.appendChild(badge);
      }

      const sw = document.createElement('button');
      sw.type = 'button';
      sw.className = 'module-switch' + (m.enabled ? ' on' : '');
      sw.setAttribute('role', 'switch');
      sw.setAttribute('aria-checked', String(Boolean(m.enabled)));
      sw.disabled = Boolean(locked);
      sw.title = locked ? '地基或前端模块，不可关闭' : '点击切换';
      head.appendChild(sw);

      card.appendChild(head);

      const desc = document.createElement('p');
      desc.className = 'module-desc';
      const bits = [];
      if (m.reason) bits.push(m.reason);
      if (m.note) bits.push(m.note);
      if (!locked && m.name === 'voice') bits.push('切换后需重启程序才真正生效');
      if (bits.length) {
        desc.textContent = bits.join('；');
        card.appendChild(desc);
      }

      if (!locked) {
        sw.addEventListener('click', async () => {
          const next = !m.enabled;
          sw.classList.toggle('on', next);
          sw.setAttribute('aria-checked', String(next));
          if (ui.modulesState) ui.modulesState.textContent = `正在切换「${m.label || m.name}」…`;
          const r = await api.setModule({ name: m.name, enabled: next });
          if (r && r.ok) {
            m.enabled = next;
            const need = Array.isArray(r.restartRequired) ? r.restartRequired : [];
            if (ui.modulesState) {
              ui.modulesState.textContent = need.length
                ? `已切换，但「${need.join('、')}」需重启程序后才真正生效`
                : `已切换「${m.label || m.name}」`;
            }
          } else {
            sw.classList.toggle('on', m.enabled);   // 失败要回滚，别让界面骗人
            sw.setAttribute('aria-checked', String(m.enabled));
            if (ui.modulesState) ui.modulesState.textContent = `切换失败：${(r && r.message) || '未知原因'}`;
          }
        });
      }
      return card;
    };

    const makeGroup = (title, items, locked) => {
      if (!items.length) return;
      const head = document.createElement('div');
      head.className = 'module-group-title';
      head.textContent = `${title} · ${items.length}`;
      ui.modulesList.appendChild(head);
      const grid = document.createElement('div');
      grid.className = 'module-grid';
      for (const m of items) grid.appendChild(moduleCard(m, locked));
      ui.modulesList.appendChild(grid);
    };

    makeGroup('可热切 · 改完立刻生效', hot, false);
    makeGroup('地基与前端 · 常驻不可关', base, true);
    if (ui.modulesState) ui.modulesState.textContent = `共 ${list.length} 个模块`;
  } catch (error) {
    if (ui.modulesList) ui.modulesList.textContent = `读取失败：${error.message}`;
  }
}

if (ui.modulesList) {
  void loadModules();
  if (ui.modulesRefresh) ui.modulesRefresh.addEventListener('click', () => void loadModules());
  // 切到「模块」标签时刷新一次（状态可能被配置文件改过）
  document.querySelectorAll('.nav-item').forEach((btn) => {
    btn.addEventListener('click', () => {
      if (btn.dataset.target === 'tab-modules') void loadModules();
    });
  });
}
