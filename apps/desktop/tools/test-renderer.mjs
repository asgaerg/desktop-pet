/**
 * 渲染层运行时测试台（Node 里的假 DOM）。
 *
 * 为什么需要它：`app.js` 与 `sprite.js` 里的**逻辑**（流式文本累积、状态机切换、
 * 口型与情绪联动、按钮行为）从来没有被执行过一次 —— 开发环境起不了 GUI。
 * 静态语法与契约检查只能保证"能加载、名字对得上"，抓不到"逻辑是错的"。
 *
 * 做法：用最小假 DOM + 假 canvas 上下文 + 假 pet API，把真实的 `app.js`
 * 作为模块跑起来，然后推动协议事件、检查真实的状态变化。
 * 不引入 jsdom —— 需要打桩的 API 很少，自己写反而更清楚、也更快。
 */

import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

/**
 * Node 版本前置检查。
 *
 * 为什么值得单独写一段：本机 PATH 里的 node 是 v14，而 Electron 32 与
 * 这些工具脚本都要求 Node 18+。版本不够时的表现是一堆不知所云的语法错误
 * （`??`、`?.`、`Object.hasOwn` 都会失败），排查起来很浪费时间。
 * 明确说一句"请用 Node 20+"能省掉这段弯路。
 */
const MIN_NODE_MAJOR = 20;
const nodeMajor = Number(process.versions.node.split('.')[0]);
if (Number.isFinite(nodeMajor) && nodeMajor < MIN_NODE_MAJOR) {
  console.error(
    `需要 Node ${MIN_NODE_MAJOR}+，当前是 ${process.versions.node}。\n` +
      `PATH 里的 node 可能太旧：请用较新的 Node 运行本脚本（例如 DSH 自带的运行时）。`,
  );
  process.exit(1);
}

const here = path.dirname(fileURLToPath(import.meta.url));
const rendererDir = path.join(here, '..', 'src', 'renderer');

/* ------------------------------------------------------------------ 断言 */

let passed = 0;
const failures = [];

function check(name, condition, detail = '') {
  if (condition) {
    passed += 1;
    console.log(`  OK   ${name}`);
  } else {
    failures.push(`${name}${detail ? ` —— ${detail}` : ''}`);
    console.error(`  FAIL ${name}${detail ? `\n       ${detail}` : ''}`);
  }
}

function finish() {
  console.log('');
  if (failures.length) {
    console.error(`渲染层运行时测试未通过：${failures.length} 项失败 / ${passed + failures.length} 项`);
    for (const item of failures) console.error(`  - ${item}`);
    process.exit(1);
  }
  console.log(`渲染层运行时测试通过：${passed}/${passed} 项`);
}

/* ------------------------------------------------------------------ 假 DOM */

/** 极简元素：支持 classList / dataset / textContent / style / 事件 */
class FakeElement {
  constructor(tag = 'div', id = '') {
    this.tagName = tag.toUpperCase();
    this.id = id;
    this.textContent = '';
    this.value = '';
    this.dataset = {};
    this.style = {
      setProperty(key, value) {
        this[key] = value;
      },
    };
    this.children = [];
    this.listeners = new Map();
    this._classes = new Set();
    this.classList = {
      add: (name) => this._classes.add(name),
      remove: (name) => this._classes.delete(name),
      contains: (name) => this._classes.has(name),
      toggle: (name, force) => {
        const want = force === undefined ? !this._classes.has(name) : Boolean(force);
        if (want) this._classes.add(name);
        else this._classes.delete(name);
        return want;
      },
    };
    // canvas 相关
    this.width = 0;
    this.height = 0;
    this._context = null;
  }

  addEventListener(type, handler) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(handler);
  }

  removeEventListener(type, handler) {
    const list = this.listeners.get(type) ?? [];
    this.listeners.set(type, list.filter((item) => item !== handler));
  }

  dispatch(type, event = {}) {
    for (const handler of this.listeners.get(type) ?? []) handler({ type, ...event });
  }

  /** canvas.getContext('2d') —— 记录所有绘制调用，便于断言"画了什么" */
  getContext(kind) {
    if (kind !== '2d') return null;
    if (!this._context) this._context = makeFakeContext();
    return this._context;
  }

  matches(selector) {
    // app.js 只用了 ':hover'
    return selector === ':hover' ? Boolean(this._hover) : false;
  }
}

function makeFakeContext() {
  const calls = [];
  const record =
    (name) =>
    (...args) => {
      calls.push({ name, args });
    };
  return {
    calls,
    canvas: null,
    fillStyle: '',
    strokeStyle: '',
    lineWidth: 1,
    lineCap: '',
    font: '',
    globalAlpha: 1,
    imageSmoothingEnabled: true,
    setTransform: record('setTransform'),
    save: record('save'),
    restore: record('restore'),
    clearRect: record('clearRect'),
    beginPath: record('beginPath'),
    closePath: record('closePath'),
    ellipse: record('ellipse'),
    arc: record('arc'),
    moveTo: record('moveTo'),
    lineTo: record('lineTo'),
    fill: record('fill'),
    stroke: record('stroke'),
    fillText: record('fillText'),
    drawImage: record('drawImage'),
    createLinearGradient: () => ({ addColorStop() {} }),
  };
}

/** 建一套 index.html 里出现过的元素 */
function buildDocument() {
  // 这份清单必须与 src/renderer/index.html 里的元素 id 保持一致。
  // 漏掉一个，被测代码取到 null 就会抛 TypeError —— 这其实是好事：
  // 假 DOM 缺了元素会被立刻发现，而不是让测试悄悄通过。
  const ids = [
    'status', 'status-text', 'bubble', 'bubble-text', 'typing', 'pet', 'input', 'send',
    'activity', 'activity-value', 'pause', 'mic', 'settings', 'minimize', 'voice', 'memory', 'forget',
    'hint', 'toast',
  ];
  const elements = new Map();
  for (const id of ids) {
    const element = new FakeElement(id === 'pet' ? 'canvas' : 'div', id);
    element.value = id === 'activity' ? '0.5' : '';
    elements.set(id, element);
  }
  return {
    body: new FakeElement('body'),
    elements,
    getElementById: (id) => elements.get(id) ?? null,
    elementFromPoint: () => null,
    addEventListener() {},
    querySelector: () => null,
  };
}

/**
 * 把所有定时器换成假实现。
 *
 * 必须这么做：`app.js` 用 `setInterval` 驱动口型动效、`showToast` 用 `setTimeout`
 * 自动隐藏 —— 真定时器会让 Node 的事件循环**永不退出**，测试跑完也退不出去，
 * 而且管道下的输出会被缓冲住，表现成"什么都不打印、直接挂死"。
 * 收集起来还能顺便断言"定时器确实被安排了"。
 *
 * 注意：测试自己要等待时**必须用 realSetTimeout** —— 装了假定时器之后，
 * `await new Promise(r => setTimeout(r, 20))` 会永远等不到回调（踩过）。
 */
const realSetTimeout = globalThis.setTimeout;

function installFakeTimers() {
  const timers = { timeouts: 0, intervals: 0, pending: [] };
  const defineGlobal = (name, value) =>
    Object.defineProperty(globalThis, name, { value, writable: true, configurable: true });
  defineGlobal('setTimeout', (fn, ms) => {
    timers.timeouts += 1;
    const handle = { fn, ms, cleared: false };
    timers.pending.push(handle);
    return handle;
  });
  defineGlobal('clearTimeout', (handle) => {
    if (handle) handle.cleared = true;
  });
  defineGlobal('setInterval', (fn, ms) => {
    timers.intervals += 1;
    const handle = { fn, ms, cleared: false };
    timers.pending.push(handle);
    return handle;
  });
  defineGlobal('clearInterval', (handle) => {
    if (handle) handle.cleared = true;
  });
  return timers;
}

/* ------------------------------------------------------------------ 假 pet API */

function buildPetApi(emit) {
  const calls = [];
  const record =
    (name) =>
    (...args) => {
      calls.push({ name, args });
      return undefined;
    };
  const api = {
    calls,
    on(channel, listener) {
      emit.listeners.push({ channel, listener });
      return () => {};
    },
    sendUserText: record('sendUserText'),
    interrupt: record('interrupt'),
    poke: record('poke'),
    setActivity: record('setActivity'),
    setPaused: record('setPaused'),
    setInteractive: record('setInteractive'),
    setVoiceEnabled: record('setVoiceEnabled'),
    sendMicAudio: record('sendMicAudio'),
    clearMemory: record('clearMemory'),
    // 输入框搬去独立窗口后新增的两个上报接口：
    // 状态文本转发给对话条的小圆点、探头状态转发给主进程用于定位对话条
    reportStatus: record('reportStatus'),
    reportPeekState: record('reportPeekState'),
    getState: async () => ({
      activity: 0.5,
      paused: false,
      status: '在线',
      clickThrough: true,
      logFile: 'x.log',
    }),
  };
  return api;
}

/* ------------------------------------------------------------------ 装载 */

async function loadRenderer() {
  const emit = { listeners: [] };
  const document = buildDocument();
  const petApi = buildPetApi(emit);
  const timers = installFakeTimers();
  const rafQueue = [];
  let time = 0;

  // Node 24 里 navigator 是只读 getter，直接赋值会抛 TypeError，必须用 defineProperty
  const defineGlobal = (name, value) => {
    Object.defineProperty(globalThis, name, {
      value,
      writable: true,
      configurable: true,
      enumerable: true,
    });
  };

  defineGlobal('window', {
    pet: petApi,
    devicePixelRatio: 2,
    addEventListener() {},
    confirm: () => true,
    requestAnimationFrame: (cb) => {
      rafQueue.push(cb);
      return rafQueue.length;
    },
    cancelAnimationFrame: () => {},
  });
  defineGlobal('document', document);
  defineGlobal('navigator', { mediaDevices: undefined });
  const performance = { now: () => time };
  /**
   * 让某次 `sprite.load()` 拿到"**没有 blink** 的清单"。
   *
   * 用于验证"素材自带眨眼时不要再叠加自动眨眼"这条规则 —— 要测它就必须有一个
   * 不含 blink 的 manifest，而 fetch 桩是共享的，所以用一个开关切换。
   */
  const manifestSwitches = { omitBlink: false };
  const fetchStub = async (url) => {
    if (String(url).includes('avatar.json')) {
      const base = {
        idle: { frames: [0, 1, 2, 1], fps: 8, loop: true },
        blink: { frames: [3, 4], fps: 14, loop: false },
        talk: { frames: [5, 6, 7, 6], fps: 13, loop: true },
        sleep: { frames: [8, 9], fps: 3, loop: true },
        curious: { frames: [10, 11], fps: 5, loop: true },
        /*
         * 情绪标签要用到的两个（2026-10-21）。
         *
         * ⚠️ 之前假清单里没有它们，于是 `play('angry')` 会**回退到 idle**，
         * 而断言只看"有没有调用 play" —— 调用确实发生了，状态却是 idle，
         * 测试绿着但实际什么都没演。加上这两个之后断言才能查 `dbg().state`。
         */
        happy: { frames: [10, 11], fps: 5, loop: false },
        angry: { frames: [8, 9], fps: 5, loop: false },
      };
      if (manifestSwitches.omitBlink) delete base.blink;
      return {
        ok: true,
        json: async () => ({
          image: 'sheet.png',
          frameWidth: 256,
          frameHeight: 256,
          fps: 8,
          animations: base,
        }),
      };
    }
    return { ok: false, json: async () => ({}) };
  };
  const ImageStub = class {
    constructor() {
      // 必须用 realSetTimeout：此时全局 setTimeout 已被换成假实现，
      // 而假实现只在测试显式 flush 时才触发。图片加载是"真实异步"，
      // 用假定时器会让 sprite.load() 永远不 settle（踩过）。
      realSetTimeout(() => {
        this.naturalWidth = 4096;
        this.naturalHeight = 256;
        this.onload?.();
      }, 0);
    }
  };

  /**
   * 假 window 的事件：**必须真的记住监听器**。
   *
   * 原来是空的 `addEventListener() {}` —— 那时 app.js 不给 window 挂任何事件，
   * 所以看不出问题。现在 Esc 打断挂在 window 上（输入框搬去独立窗口之后，
   * 宠物身上至少还按得到），空壳会让"按 Esc 没反应"这类回归完全测不出来。
   */
  const windowListeners = new Map();
  const window = {
    pet: petApi,
    devicePixelRatio: 2,
    addEventListener(type, handler) {
      if (!windowListeners.has(type)) windowListeners.set(type, []);
      windowListeners.get(type).push(handler);
    },
    removeEventListener(type, handler) {
      const list = windowListeners.get(type);
      if (list) windowListeners.set(type, list.filter((h) => h !== handler));
    },
    dispatch(type, event = {}) {
      for (const handler of windowListeners.get(type) ?? []) handler(event);
    },
    confirm: () => true,
    requestAnimationFrame: (cb) => {
      rafQueue.push(cb);
      return rafQueue.length;
    },
    cancelAnimationFrame: () => {},
  };
  const navigator = { mediaDevices: undefined };
  const rafStub = (cb) => window.requestAnimationFrame(cb);
  const cafStub = () => {};

  defineGlobal('window', window);
  defineGlobal('document', document);
  defineGlobal('navigator', navigator);
  defineGlobal('performance', performance);
  defineGlobal('requestAnimationFrame', rafStub);
  defineGlobal('cancelAnimationFrame', cafStub);
  defineGlobal('fetch', fetchStub);
  defineGlobal('Image', ImageStub);

  /** 推进 rAF 一帧 */
  const stepFrame = (deltaMs = 16) => {
    time += deltaMs;
    const pending = rafQueue.splice(0, rafQueue.length);
    for (const cb of pending) cb(time);
  };

  /**
   * 把渲染层的三个文件拼成一个模块来执行。
   *
   * 为什么不用 `import()` + loader hook：`node:module` 的 `register()` 需要
   * Node 20.6+，而 npm 脚本走的是 PATH 里的 node —— 这里恰好是 v14，
   * 于是 `register is not a function`（踩过）。拼接方式对 Node 版本没有要求，
   * 而且这几个文件本来就是按"顺序加载、共享作用域"的浏览器脚本。
   *
   * 处理细节：
   *   · 去掉 `export ` 关键字并补一句引用，让声明留在同一个作用域里
   *     （ESLint 之类不会用，但比依赖 hoisting 更清楚）；
   *   · 去掉 `import` 语句 —— 被导入的名字此时已经在作用域内了。
   *     "导入名是否真的存在"由契约检查负责，不在这里重复。
   */
  const read = (name) => readFileSync(path.join(rendererDir, name), 'utf-8');
  const toPlain = (source) =>
    source
      .replace(/^\s*import\s[^;\n]*;?\s*$/gm, '')
      // export class / export function / export const ...
      .replace(/^(\s*)export\s+(?=(class|function|const|let|var)\b)/gm, '$1')
      // export default ...
      .replace(/^(\s*)export\s+default\s+/gm, '$1')
      // export { A, B }; 这种形式直接删掉（名字已经在作用域里了）
      .replace(/^\s*export\s*\{[^}]*\}\s*;?\s*$/gm, '');

  const bundle = [
    toPlain(read('sprite.js')),
    toPlain(read('voice-io.js')),
    toPlain(read('app.js')),
  ].join('\n;\n');

  if (process.env.PET_TEST_VERBOSE) console.log('  [装载] 开始求值渲染层代码');
  const factory = new Function(
    'window',
    'document',
    'navigator',
    'performance',
    'fetch',
    'Image',
    'setTimeout',
    'clearTimeout',
    'setInterval',
    'clearInterval',
    'requestAnimationFrame',
    'cancelAnimationFrame',
    'console',
    'globalThis',
    bundle,
  );
  factory(
    window,
    document,
    navigator,
    performance,
    fetch,
    Image,
    setTimeout,
    clearTimeout,
    setInterval,
    clearInterval,
    requestAnimationFrame,
    cancelAnimationFrame,
    console,
    globalThis,
  );
  if (process.env.PET_TEST_VERBOSE) console.log('  [装载] 渲染层代码求值完成');
  return { emit, document, petApi, stepFrame, rafQueue, timers, manifestSwitches };
}

/* ------------------------------------------------------------------ 测试 */

console.log('渲染层运行时测试（假 DOM 中执行真实 app.js）\n');

const { emit, document, petApi, stepFrame, timers, manifestSwitches } = await loadRenderer();
const els = document.elements;

const send = (type, payload = {}) => {
  for (const { listener } of emit.listeners) {
    // app.js 的消息总线是"每个通道各自回调"，这里按通道分发
  }
  for (const { channel, listener } of emit.listeners) {
    if (channel === 'pet:message') listener({ type, ...payload });
  }
};
const sendChannel = (channel, payload) => {
  for (const { channel: name, listener } of emit.listeners) {
    if (name === channel) listener(payload);
  }
};

// 等 sprite.load() 真正走完：它要 await fetch(manifest) 再 await 图片 onload，
// 两次宏任务跳转。固定 sleep 很容易不够（30ms 实测不够，会误判成"占位模式"），
// 所以改成轮询到状态就绪，最多等 2 秒。
{
  const deadline = Date.now() + 2000;
  while (Date.now() < deadline) {
    if (window.__pet.sprite.debug.mode === 'sprite') break;
    await new Promise((resolve) => realSetTimeout(resolve, 10));
  }
}
stepFrame(16);

console.log('1. 初始化');
check('订阅了 pet:message 通道', emit.listeners.some((l) => l.channel === 'pet:message'));
check('订阅了 pet:status 通道', emit.listeners.some((l) => l.channel === 'pet:status'));

// 状态与活泼度都不再显示在宠物窗口里（输入卡片已拆成独立窗口），
// 所以改成断言**它们被转发给主进程** —— 那是现在唯一有意义的检查。
check(
  '初始状态被转发给主进程（对话条的小圆点据此着色）',
  petApi.calls.some((c) => c.name === 'reportStatus' && String(c.args[0] ?? '').length > 0),
  `实际调用：${JSON.stringify(petApi.calls.filter((c) => c.name === 'reportStatus'))}`,
);

console.log('\n3. 串台保护：迟到/错配的增量必须被丢弃');
send('pet.say.start', { id: 'm2' });
send('pet.say.delta', { id: 'm2', text: '第一句' });
send('pet.say.delta', { id: 'm1', text: '【不该出现】' });
check(
  '不属于当前发言的增量被丢弃',
  !els.get('bubble-text').textContent.includes('不该出现'),
  `实际：${JSON.stringify(els.get('bubble-text').textContent)}`,
);
send('pet.say.end', { id: 'm2', text: '第一句' });

console.log('\n7. 立绘状态机（真实 sprite.js）');
const dbg = () => window.__pet.sprite.debug;
check(
  '成功加载序列帧素材（走的是精灵图路径而非占位绘制）',
  dbg().mode === 'sprite',
  `实际模式：${dbg().mode}`,
);
check('初始为 idle 状态', dbg().state === 'idle', `实际：${dbg().state}`);

// 帧推进：idle 有 4 帧，推进若干帧后帧号应当变化过
const framesSeen = new Set();
const ticksBefore = dbg().tick;
for (let i = 0; i < 40; i += 1) {
  stepFrame(40);
  framesSeen.add(dbg().frame);
}
check('动画帧在推进（不是卡住不动）', dbg().tick > ticksBefore, `tick ${ticksBefore} -> ${dbg().tick}`);
check(
  'idle 动画用到了多帧',
  framesSeen.size > 1,
  `40 次推进只见到帧号 ${[...framesSeen].join(',')}`,
);

// 发言 -> talk
send('pet.say.start', { id: 'm3' });

/*
 * 防回归：素材自带眨眼时，不要再叠加自动眨眼计时器。
 *
 * 背景：播放器原本**无条件**每 2.4~5.6 秒切一次 blink。而这类素材的待机循环里
 * 本来就含一段闭眼，于是"循环自己眨 + 计时器再眨"两套叠加 ——
 * 表现是眨眼眨个不停（用户实际报过）。
 *
 * 现在只有素材真的提供了独立 blink 帧时才叠加。这里切换到"不含 blink 的清单"、
 * 重新 load，再推进 6 秒（远超 5.6 秒的上限），状态应当始终是 idle。
 */
{
  manifestSwitches.omitBlink = true;
  await window.__pet.sprite.load();
  check(
    '切换成不含 blink 的清单后，加载成功',
    dbg().mode === 'sprite',
    `实际模式：${dbg().mode}`,
  );
  window.__pet.sprite.play('idle', true);
  const seenStates = new Set();
  for (let i = 0; i < 200; i += 1) {
    stepFrame(30);
    seenStates.add(dbg().state);
  }
  check(
    '素材没有独立 blink 时不再叠加自动眨眼',
    !seenStates.has('blink'),
    `推进 6 秒期间出现过这些状态：${[...seenStates].join(', ')}（不该出现 blink）`,
  );

  // 还原，免得影响后续测试
  manifestSwitches.omitBlink = false;
  await window.__pet.sprite.load();
  window.__pet.sprite.play('idle', true);
  stepFrame(16);
}

// 精力极低 -> sleep
send('pet.emotion', { state: { energy: 0.1, mood: 0.5, intimacy: 0.5 } });
stepFrame(16);
check('精力极低时进入 sleep', dbg().state === 'sleep', `实际：${dbg().state}`);
// 恢复精力 -> 回到 idle
send('pet.emotion', { state: { energy: 0.9, mood: 0.5, intimacy: 0.5 } });
stepFrame(16);
check('精力恢复后回到 idle', dbg().state === 'idle', `实际：${dbg().state}`);

/*
 * 情绪标签 -> 一次性表情（2026-10-21「把情绪接完」）。
 *
 * 背景：后端 `EmotionState.dominant` 早就算好了离散标签（sleepy/excited/happy/
 * annoyed/curious/neutral），但 `as_dict()` 没往外传，渲染层只能自己从 mood 反推 ——
 * 于是 prompt 里写"你现在有点不爽"，立绘还在笑。
 *
 * 这里用**真实事件**驱动 `app.js`，并给 `sprite.play` 装监听来看它到底播了什么。
 * 不测"源码里有没有那段代码"，测"消息进来之后播了哪个素材"。
 */
{
  const sprite = window.__pet.sprite;
  const originalPlay = sprite.play;
  const played = [];
  sprite.play = function (state, force) {
    played.push(state);
    return originalPlay.call(this, state, force);
  };

  send('pet.emotion', {
    state: { energy: 0.9, mood: 0.9, intimacy: 0.5, dominant: 'happy' },
  });
  stepFrame(16);
  check(
    'dominant=happy 时播 happy 素材',
    played.includes('happy') && dbg().state === 'happy',
    `实际播放：${played.join(', ') || '无'}；状态 ${dbg().state}`,
  );

  /*
   * ⚠️ 同一标签的**周期广播**不能重播。
   *
   * `pet.emotion` 是周期性发出来的（情绪随时间漂移），每次都 play 的话
   * 一次性表情会被反复打断、永远播不完（表现像"抽搐"）。
   */
  const before = played.length;
  send('pet.emotion', {
    state: { energy: 0.9, mood: 0.92, intimacy: 0.5, dominant: 'happy' },
  });
  stepFrame(16);
  check(
    '同一标签重复广播不重播（一次性表情不会被反复打断）',
    played.length === before,
    `播放次数 ${before} -> ${played.length}（多播了 ${played.slice(before).join(', ')}）`,
  );

  // 标签变了就要跟着换：annoyed 在素材里叫 angry
  send('pet.emotion', {
    state: { energy: 0.9, mood: 0.1, intimacy: 0.5, dominant: 'annoyed' },
  });
  stepFrame(16);
  check(
    'dominant=annoyed 映射到 angry 素材',
    played.includes('angry') && dbg().state === 'angry',
    `实际播放：${played.join(', ')}；状态 ${dbg().state}`,
  );

  // excited 在素材里没有，映射到最接近的 happy
  send('pet.emotion', {
    state: { energy: 0.9, mood: 0.99, intimacy: 0.5, dominant: 'excited' },
  });
  stepFrame(16);
  check(
    'dominant=excited 映射到 happy 素材（素材里没有 excited）',
    played[played.length - 1] === 'happy',
    `最后一次播放：${played[played.length - 1] ?? '无'}`,
  );

  // sleepy / neutral 属于**持续态**，不该当一次性表情播
  const beforeQuiet = played.length;
  for (const label of ['neutral', 'sleepy']) {
    send('pet.emotion', {
      state: { energy: 0.9, mood: 0.5, intimacy: 0.5, dominant: label },
    });
    stepFrame(16);
  }
  check(
    'neutral / sleepy 不触发一次性表情',
    played.length === beforeQuiet,
    `多播了：${played.slice(beforeQuiet).join(', ') || '无'}`,
  );

  // 还原，免得影响后续用例
  sprite.play = originalPlay;
  send('pet.emotion', { state: { energy: 0.9, mood: 0.5, intimacy: 0.5, dominant: 'neutral' } });
  sprite.play('idle', true);
  stepFrame(16);
}

console.log('\n8. 口型包络');
send('pet.mouth', { id: 'm3', envelope: [0.2, 0.8, 0.4] });
stepFrame(16);
check('口型开合被设置（不是零）', dbg().mouth > 0, `实际：${dbg().mouth}`);
send('pet.mouth', { id: 'm3', envelope: [] });
stepFrame(16);
check('空包络不会出错，口型仍为有效值', dbg().mouth >= 0 && dbg().mouth <= 1);

/**
 * 触发口型定时器一次。
 *
 * app.js 用 `setInterval(..., 60)` 驱动口型；定时器已被换成假实现，
 * 所以这里找出那个 interval 手动触发 —— 这正是"可测试性"的价值：
 * 真实实现用了基于挂钟时间与随机数的动效，硬等真实时间既慢又不确定。
 */
const tickMouthOnce = () => {
  for (const handle of timers.pending) {
    if (handle.ms === 60 && !handle.cleared && typeof handle.fn === 'function') {
      handle.fn();
      return true;
    }
  }
  return false;
};
check('口型定时器已注册（说话时嘴才会动）', tickMouthOnce(), '没找到 60ms 的 interval');

// 说话期间：口型应被驱动到非零值
send('pet.say.start', { id: 'm4' });
const mouthSamples = [];
for (let i = 0; i < 20; i += 1) {
  tickMouthOnce();
  mouthSamples.push(dbg().mouth);
}
check(
  '说话时口型被驱动到非零',
  mouthSamples.every((value) => value > 0),
  `采样：${mouthSamples.slice(0, 5).map((v) => v.toFixed(2)).join(',')}`,
);
check(
  '口型始终在 0~1 合法范围内',
  mouthSamples.every((value) => value >= 0 && value <= 1),
  `越界样本：${mouthSamples.filter((v) => v < 0 || v > 1)}`,
);
// 说明：真实实现用随机数让包络有变化，所以这里不断言"数值必须不同"
// （那会变成不可靠的测试）。只断言"被驱动到合法非零值"。
send('pet.say.end', { id: 'm4', text: 'x' });
tickMouthOnce();
check('不说话时口型归零', dbg().mouth === 0, `实际：${dbg().mouth}`);

console.log('\n9. 错误与状态提示');
send('system.error', { level: 'error', message: '测试错误' });
check('错误信息显示在提示区', els.get('toast').textContent === '测试错误');
check('提示不再隐藏', !els.get('toast').classList.contains('hidden'));
// 状态文字/级别不再有界面元素可断言（输入卡片已拆走），
// 改成断言它被转告主进程 —— 主进程再统一发给对话条的小圆点。
petApi.calls.length = 0;
sendChannel('pet:status', { text: '连接断开', level: 'warn' });
check(
  '状态变化被转发给主进程',
  petApi.calls.some((c) => c.name === 'reportStatus' && c.args[0] === '连接断开'),
  JSON.stringify(petApi.calls.filter((c) => c.name === 'reportStatus')),
);

console.log('\n13. 中断与快捷键');
petApi.calls.length = 0;
// Esc 现在挂在 window 上（输入框搬走之后，至少宠物身上还按得到）
window.dispatch('keydown', { key: 'Escape' });
check('Esc 触发中断', petApi.calls.some((c) => c.name === 'interrupt'));

finish();
