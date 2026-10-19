/**
 * 按住说话（push-to-talk）：检测一个**可配置的键**的按下与松开。
 *
 * ## 为什么必须另起一个进程
 *
 * Electron 的 `globalShortcut.register` **只有按下、没有松开** —— 它把
 * accelerator 当成"一个动作"来对待。而"按住说话"的核心语义就是
 * **按下开始录、松开结束**，所以它做不到。
 *
 * 也不能用 `-webkit-app-region` 那类窗口内方案：用户在别的程序里打字时
 * 也要能按住说话，那要求**全局**监听。
 *
 * ## 方案：PowerShell 常驻 + 轮询 GetAsyncKeyState
 *
 * 真正的探测在 `tools/probe-key.ps1`（独立文件，不是内嵌字符串 ——
 * 与 `fullscreenWatcher` 同一条理由：内嵌的长脚本在传参/引号/编码上
 * 反复出问题，独立文件可以被单独测）。
 *
 * ## ⚠️ 为什么不用 SetWindowsHookEx（低级键盘钩子）
 *
 * 那个方案更"正统"，但需要消息泵 + 跨线程回调委托，在 PowerShell 里
 * 委托很容易被 GC 回收 —— 表现是"钩子装上了但一个事件都收不到"，
 * 静默失效、极难查。而按住说话对延迟的容忍度是几十毫秒，
 * **15ms 轮询完全够用**，且实现简单、行为可预测。
 *
 * 实测（`tools/test-probe-key.ps1`，用合成按键自测）：
 *
 *     READY 65 / DOWN 65 / UP 65   ← 检测正常
 *
 * ## ⚠️ 虚拟键码（VK）而不是字符
 *
 * 同一个人按 A 键，在大写锁定/按住 Shift 时**字符**不同，而 VK 是**物理键**
 * 的标识，与修饰键无关 —— 这正是"按住说话"需要的语义。
 *
 * 本模块只负责"检测到按下/松开"，**不关心拿它做什么**（那是调用方的事）。
 * 这样它可以被单独测，也不会把语音逻辑渗进快捷键这一层。
 */

import { spawn, spawnSync, type ChildProcess } from 'node:child_process';
import {
  closeSync,
  existsSync,
  mkdirSync,
  openSync,
  readSync,
  rmSync,
  statSync,
} from 'node:fs';
import { resolve, dirname } from 'node:path';
import { platform } from 'node:process';

export interface PushToTalkOptions {
  /**
   * 要监听的**虚拟键码**（Windows VK）。
   *
   * `0` 表示未配置 -> 不启动监视器（而不是随便挑一个键）。
   * 见 `DEFAULT_PTT_VK`。
   */
  vk: number;
  /** 轮询间隔（毫秒）。太小费 CPU，太大手感迟钝。 */
  pollMs?: number;
  /** 键按下 */
  onPress: () => void;
  /** 键松开 */
  onRelease: () => void;
  /** 监视器起不来 / 中途退出时的日志回调（不致命，但要能看见） */
  onLog?: (line: string) => void;
}

/**
 * 默认键：**右 Alt**（VK 0xA5）。
 *
 * 为什么是它：
 *   · 左手拇指够得到，按住的姿势自然；
 *   · **不参与打字**（左 Alt 会触发菜单，右 Alt 在中文输入法下常是"字符切换"，
 *     所以仍建议用户按自己习惯改）；
 *   · 单独按它不会产生字符，不会污染输入。
 *
 * ⚠️ 不选空格/字母：那会与打字冲突 —— 按住说话的键必须是"平时不用"的。
 */
export const DEFAULT_PTT_VK = 0xa5;

/** VK -> 给人看的名字。查不到时退回十六进制，界面至少不是空白。 */
const VK_NAMES: Record<number, string> = {
  0x08: 'Backspace',
  0x09: 'Tab',
  0x0d: 'Enter',
  0x10: 'Shift',
  0x11: 'Ctrl',
  0x12: 'Alt',
  0x13: 'Pause',
  0x14: 'CapsLock',
  0x1b: 'Esc',
  0x20: '空格',
  0x21: 'PageUp',
  0x22: 'PageDown',
  0x23: 'End',
  0x24: 'Home',
  0x25: '←',
  0x26: '↑',
  0x27: '→',
  0x28: '↓',
  0x2c: 'PrintScreen',
  0x2d: 'Insert',
  0x2e: 'Delete',
  0x5b: '左 Win',
  0x5c: '右 Win',
  0x5d: '菜单键',
  0x90: 'NumLock',
  0x91: 'ScrollLock',
  0xa0: '左 Shift',
  0xa1: '右 Shift',
  0xa2: '左 Ctrl',
  0xa3: '右 Ctrl',
  0xa4: '左 Alt',
  0xa5: '右 Alt',
  0xc0: '` ~',
  0xba: '; :',
  0xbb: '= +',
  0xbc: ', <',
  0xbd: '- _',
  0xbe: '. >',
  0xbf: '/ ?',
  0xdb: '[ {',
  0xdc: '\\ |',
  0xdd: '] }',
  0xde: "' \"",
  /*
   * 下面这些是 2026-10-10 补的 —— 它们的 `vkToDomCode` 映射能用，
   * 但 `vkName` 之前会显示成 `VK 0x6A` 这种，设置页里根本认不出是哪个键。
   *
   * ⚠️ **映射能用但名字认不出**是个隐蔽的坑：用户会以为"这个键不支持"。
   * 所以两边必须一起补 —— 加新键时记得**同时改 `VK_NAMES` 和 `vkToDomCode`**。
   */
  0x6a: '小键盘 *',
  0x6b: '小键盘 +',
  0x6c: '小键盘 ,',
  0x6d: '小键盘 -',
  0x6e: '小键盘 .',
  0x6f: '小键盘 /',
  0xad: '静音键',
  0xae: '音量减',
  0xaf: '音量加',
  0xb0: '下一曲',
  0xb1: '上一曲',
  0xb2: '停止',
  0xb3: '播放/暂停',
};


/** 把 VK 变成界面上能显示的名字。 */
export function vkName(vk: number): string {
  if (!Number.isFinite(vk) || vk <= 0) return '未设置';
  if (VK_NAMES[vk]) return VK_NAMES[vk];
  if (vk >= 0x30 && vk <= 0x39) return String.fromCharCode(vk); // 0-9
  if (vk >= 0x41 && vk <= 0x5a) return String.fromCharCode(vk); // A-Z
  if (vk >= 0x60 && vk <= 0x69) return `小键盘 ${vk - 0x60}`;
  if (vk >= 0x70 && vk <= 0x87) return `F${vk - 0x6f}`; // F1-F24
  return `VK 0x${vk.toString(16).toUpperCase()}`;
}


/*
 * ===========================================================================
 *  切换式全局热键（**当前默认**，2026-10-10 六轮实测后的结论）
 * ===========================================================================
 *
 * ## 为什么用它
 *
 * 六轮实测里 **`globalShortcut` 是唯一稳定收到按键的机制**：
 *
 *   · `GetAsyncKeyState` 轮询（PowerShell）—— 收到过，用一会儿静默失效
 *   · `WH_KEYBOARD_LL` 钩子（PowerShell）—— 只有第一次有反应
 *   · `uiohook-napi`（Node **和** Electron 都试了）—— 0 个事件
 *   · `iohook` —— 装不上（预编译最高 Electron 12，本机需 ABI 128；
 *     它是 NAN 插件，没有 Node-API 那种 ABI 稳定性）
 *   · `@hurdlegroup/robotjs` —— 能装能加载，但维护分支删了 `getKeyState`，
 *     **只能发键、不能读键**
 *
 * ## ⚠️ 代价：拿不到"松开"
 *
 * `globalShortcut` 只有按下回调 —— Chromium 的限制
 * （electron/electron#32071 一直没修）。而"获取全局松开"**必须有原生键盘钩子**，
 * 没有别的办法（那篇 Electron 全局键盘钩子文档与 StackOverflow 的答案一致）。
 *
 * **替代：切换式** —— 按一次开始、再按一次结束。
 *
 * ## 两个必要的补偿
 *
 *   · **防抖 400ms**：实测用户 **1.5 秒内按了 20 多次**（按下去没反馈、
 *     不知道生效没，于是又按），每按一次切一次状态，音频全变成
 *     0.02~0.08 秒被时长门槛拦掉 —— 又变成"按了没反应"。
 *   · **可听反馈**：`index.ts` 里按一下响一声（`shell.beep()`），
 *     让用户知道生效了，从根上减少重复按。
 */

export function vkToAccelerator(vk: number): string | null {
  if (!Number.isFinite(vk) || vk <= 0) return null;
  const v = Math.trunc(vk);

  // 字母 / 数字：Electron 直接收 'A' / '0'
  if (v >= 0x41 && v <= 0x5a) return String.fromCharCode(v);
  if (v >= 0x30 && v <= 0x39) return String.fromCharCode(v);

  // 功能键 F1-F24
  if (v >= 0x70 && v <= 0x87) return `F${v - 0x6f}`;

  /*
   * 修饰键：Electron 的 accelerator 里修饰键**不能单独成键**
   * （`'AltRight'` 不是合法 accelerator，`register` 会返回 false）。
   *
   * 所以右 Alt / 右 Ctrl / 右 Shift 这类**单独按的修饰键走不通** ——
   * 必须显式返回 null，让调用方换一个键，而不是静默失败。
   * 这正是本项目默认键的坑：`DEFAULT_PTT_VK = 0xA5`（右 Alt）。
   */
  const MODIFIERS: Record<number, string> = {
    0x10: 'Shift', 0xa0: 'Shift', 0xa1: 'Shift',
    0x11: 'Control', 0xa2: 'Control', 0xa3: 'Control',
    0x12: 'Alt', 0xa4: 'Alt', 0xa5: 'Alt',
    0x5b: 'Super', 0x5c: 'Super',
  };
  if (MODIFIERS[v]) return null; // 单独修饰键：accelerator 表达不了

  // 其余常用键
  const TABLE: Record<number, string> = {
    0x20: 'Space', 0x0d: 'Return', 0x1b: 'Escape', 0x09: 'Tab',
    0x08: 'Backspace', 0x2e: 'Delete', 0x2d: 'Insert',
    0x25: 'Left', 0x26: 'Up', 0x27: 'Right', 0x28: 'Down',
    0x24: 'Home', 0x23: 'End', 0x21: 'PageUp', 0x22: 'PageDown',
    0xc0: '`', 0xbd: '-', 0xbb: '=', 0xdb: '[', 0xdd: ']',
    0xdc: '\\', 0xba: ';', 0xde: "'", 0xbc: ',', 0xbe: '.', 0xbf: '/',
  };
  return TABLE[v] ?? null;
}

/**
 * 两次切换之间的**最小间隔**（防抖）。
 *
 * 见 `GlobalShortcutPtt.lastToggleAt` 的说明：实测用户 1.5 秒内按了 20 多次
 * （间隔 20~90ms），每按一次就切一次状态，成了"开关大战"。
 *
 * 400ms 的依据：
 *   · 人的"连击"最快也在 150ms 以上，而 20~90ms 必然是按键重复
 *   · 正常用法是"按一次 -> 说几秒 -> 再按一次"，间隔远大于 400ms
 */
export const TOGGLE_DEBOUNCE_MS = 800;

/** `GlobalShortcutPtt` 的回调 */
export interface GlobalShortcutPttOptions {
  /**
   * **独立的全局键的 VK**（和配置 `push_to_talk.global_vk` 一致）。
   *
   * ⚠️ 必须是**普通键**（`F9` / `A` / `Space`…），不能是单独的修饰键 ——
   * accelerator 语法要求"修饰键 + 恰好一个 key code"，单独修饰键会**抛异常**。
   *
   * 这与 `vk`（聚焦时按住用的、可以是右 Alt 那种修饰键）**是两个不同的键**：
   *
   *     vk         -> FocusedKeyPtt（`before-input-event`，能拿松开）→ 按住说
   *     globalVk   -> GlobalShortcutPtt（`globalShortcut`，只有按下）→ 切换式
   */
  globalVk?: number;
  /** 显式给的 accelerator 候选（优先于 `globalVk`，给测试和高级配置用） */
  accelerators?: string[];
  /** VK 码（聚焦时那个键；只在上面的都为空时用来兜底换算） */
  vk: number;
  /** 每次按下（切换）时调用 */
  onToggle: () => void;
  /** 日志 */
  onLog: (line: string) => void;
}

/**
 * **切换式**按住说话：靠 Electron `globalShortcut` 收键。
 *
 * ## 为什么不用 `PushToTalk`（PowerShell 轮询）
 *
 * 实测（2026-10-10）在本环境下 `GetAsyncKeyState` **读不到任何键**：
 *
 *     180 秒连续心跳（循环 100% 在跑）   TICK 544747 → … → 705860
 *     DOWN 0   UP 0                      <- 用户按了很多次
 *     时间线上最后一次真的收到是 18:47:47，之后跨所有轮转日志 0 条
 *
 * 而同一台机器上 Electron `globalShortcut` 注册的三个热键**全部成功**。
 * 所以换成它。
 *
 * ## 语义变了：按住 -> 切换
 *
 * `globalShortcut` **只有按下、没有松开**（这是它不能做"按住说话"的
 * 根本原因，见文件头）。所以交互改成：
 *
 *     按一次 -> 开始采集（闸门开，宠物身上显示「在听你说…」）
 *     再按一次 -> 结束采集 + 送去识别
 *
 * 对"打游戏时说一句"这个场景，切换式其实**更好用**（不用一直按着）。
 */
export class GlobalShortcutPtt {
  private registered: string | null = null;
  private stopped = false;
  /**
   * 上次触发的时间（用于**防抖**）。
   *
   * ## 🔴 为什么必须有（2026-10-10 实测抓到的真问题）
   *
   * 用户实测日志里，**1.5 秒内按了 20 多次**（间隔 20~90ms）：
   *
   *     32.443  再按一次 -> 结束
   *     32.473  按下（F9）-> 开始      <- 25ms 后又按
   *     32.503  再按一次 -> 结束
   *     32.534  按下（F9）-> 开始
   *     …
   *
   * 原因：**按下去没有任何反馈**（听不到"开始录音"），用户不知道生效没，
   * 于是又按一下 —— 而每按一次就**切换一次状态**，成了"开关大战"。
   *
   * 20~90ms 的间隔**不可能是人有意按的**（那比连击还快），
   * 所以必须防抖。400ms 足够滤掉连击和按键重复，
   * 又不影响"说完再按一次"这个正常动作（那至少隔几百毫秒）。
   */
  private lastToggleAt = 0;
  /**
   * 上次"记了防抖日志"的那个 `lastToggleAt`。
   *
   * 用来**每个防抖窗口只记一条日志** —— 按住一个键时 Windows 每 ~30ms
   * 重发一次 keydown，若每次都记，几秒内能刷出几千行（实测把 `main.log` 淹了，
   * 有用的日志反而看不见）。靠它区分"同一段连发"和"新的一段连发"。
   */
  private lastBounceNoteAt = -1;

  constructor(private readonly options: GlobalShortcutPttOptions) {}

  /** 真的注册上了吗（设置页要能看出「配置开了但没生效」） */
  get running(): boolean {
    return this.registered !== null && !this.stopped;
  }

  /**
   * 注册。**失败不抛错** —— 按住说话是可选功能，
   * 它起不来不该让整个应用起不来（与 PowerShell 版同一条取舍）。
   *
   * ## 候选列表（2026-10-10 加）
   *
   * `options.accelerators` 给多个候选，**谁注册成功就用谁**。
   *
   * 为什么需要：热键**可能被别的程序占用**，而那时 `globalShortcut.register`
   * **返回 false 而不是抛异常** —— 静默失败。实测 `Ctrl+Alt+M` 就被占了。
   * 只有一个候选的话，遇到占用就彻底没有全局热键，而用户完全不知道为什么。
   *
   * ⚠️ 生效的那个**必须记进日志** —— 否则用户不知道按哪个键，
   * 表现又是"按了没反应"。
   */
  start(): boolean {
    // 候选：显式列表 > globalVk（独立普通键）> vk 换算（兼容旧配置）
    const candidates: string[] = [];
    for (const a of this.options.accelerators ?? []) {
      if (typeof a === 'string' && a.trim()) candidates.push(a.trim());
    }
    if (candidates.length === 0 && this.options.globalVk) {
      const accel = vkToAccelerator(this.options.globalVk);
      if (accel) candidates.push(accel);
    }
    if (candidates.length === 0) {
      const accel = vkToAccelerator(this.options.vk);
      if (accel) candidates.push(accel);
    }

    if (candidates.length === 0) {
      /*
       * ⚠️ 走到这里说明配的是**单独修饰键**（右 Alt / 右 Ctrl）——
       * 它们在 accelerator 体系里表达不了。
       *
       * 但**这不是错误**：那种键由 `FocusedKeyPtt`（`before-input-event`）
       * 负责，全局这条本来就该空着。所以说清楚"聚焦时仍可用"，
       * 而不是笼统报失败 —— 否则用户会以为整个功能坏了。
       */
      const shown = vkName(this.options.globalVk || this.options.vk);
      this.options.onLog(
        `按住说话：**${shown} 不能用作全局热键**` +
          '（单独的修饰键在 Electron 里表达不了）—— 聚焦时按住它仍然可用；' +
          '要"不聚焦时也能用"，请在设置里给全局键配一个**普通键**（如 F9）',
      );
      return false;
    }

    let electron: typeof import('electron');
    try {
      electron = require('electron') as typeof import('electron');
    } catch (error) {
      this.options.onLog(`按住说话：拿不到 electron 模块（${(error as Error).message}）`);
      return false;
    }
    const { globalShortcut } = electron;

    const onFire = () => {
      if (this.stopped) return;

      /*
       * 防抖（见 `lastToggleAt` 的说明）。
       *
       * ⚠️ **静默忽略，但要记日志** —— 用户需要能看出"我按了但它没切"。
       * 每个防抖窗口**只记一条**：按住一个键时 Windows 每 ~30ms 重发一次，
       * 每次都记会在几秒内刷出几千行（实测把 main.log 淹了）。
       */
      const now = Date.now();
      const gap = now - this.lastToggleAt;
      if (gap < TOGGLE_DEBOUNCE_MS) {
        if (this.lastBounceNoteAt !== this.lastToggleAt) {
          this.lastBounceNoteAt = this.lastToggleAt;
          this.options.onLog(
            `按住说话：按键重复被忽略（${gap}ms < ${TOGGLE_DEBOUNCE_MS}ms）` +
              `—— 按住不放会连发，正常现象`,
          );
        }
        return;
      }
      this.lastToggleAt = now;
      try {
        this.options.onToggle();
      } catch (error) {
        this.options.onLog(`按住说话 onToggle 抛错：${(error as Error).message}`);
      }
    };

    // 挨个试，第一个注册成功的就用它
    const tried: string[] = [];
    for (const accel of candidates) {
      try {
        // 先反注册，避免"重启时重复注册失败"
        try {
          globalShortcut.unregister(accel);
        } catch {
          /* 没注册过无所谓 */
        }
        if (globalShortcut.register(accel, onFire)) {
          this.registered = accel;
          this.attempts = tried;
          break;
        }
        tried.push(accel);
      } catch (error) {
        // 语法非法（比如单独修饰键、鼠标键）会走到这里
        tried.push(`${accel}（${(error as Error).message.slice(0, 40)}）`);
      }
    }

    if (this.registered === null) {
      this.options.onLog(
        `按住说话：全局热键一个都没注册上（试过：${tried.join(' / ')}）—— ` +
          '多半被别的程序占用了，请在配置里换一个组合',
      );
      return false;
    }

    this.stopped = false;
    this.options.onLog(
      `按住说话已启动（**全局切换式**）：${this.registered}` +
        ' —— 按一次开始说，再按一次结束' +
        (this.attempts.length ? `（前面 ${this.attempts.join(' / ')} 没成功）` : ''),
    );
    return true;
  }

  /** 启动时被跳过的候选（诊断用） */
  private attempts: string[] = [];

  stop(): void {
    this.stopped = true;
    const accel = this.registered;
    this.registered = null;
    if (!accel) return;
    try {
      const { globalShortcut } = require('electron') as typeof import('electron');
      globalShortcut.unregister(accel);
    } catch {
      /* 反注册失败无所谓，进程退出时会释放 */
    }
  }
}

export class PushToTalk {
  private child: ChildProcess | null = null;
  private stopped = false;
  /** 已经按下（用来吃掉重复的 DOWN —— 监视器本身会去重，这里再兜一层） */
  private down = false;
  /** 文件轮询定时器（见 `startTailing`：为什么不用管道） */
  private tailTimer: NodeJS.Timeout | null = null;
  /** 已经读到的字节偏移（只读新增部分） */
  private tailOffset = 0;
  /** 跨读取的残行缓冲（文件读到的边界不一定在行尾） */
  private tailBuffer = '';

  /**
   * 最后一次从这个监视器**读到新行**的时间。
   *
   * ## 🔴 为什么要它（2026-10-10 修「不聚焦时按键失效」）
   *
   * 用户报「要聚焦宠物窗口按键才能用」。实测抓到的现场：
   *
   *     PID 17704   CPU 用户 3.14s
   *     等 6 秒后   CPU 3.14s -> 3.14s（+0.00s）    <- 循环停了
   *     probe-key.out 最后写入距今 233 秒
   *
   * **进程还在、但 `while` 循环不转了。** 原来没有任何检测，
   * 于是按键**永久失效、不报错、不记日志** —— 用户只能得出
   * "要聚焦才能用"这种（错误的）结论。
   *
   * 靠 `probe-key.ps1` 每约 3 秒写的 `TICK` 心跳来刷新。
   */
  private lastAliveAt = 0;
  /** 看门狗定时器（见 `lastAliveAt`） */
  private watchdogTimer: NodeJS.Timeout | null = null;
  /**
   * 已经因为"僵住"重启过几次。
   *
   * ⚠️ 不能无限重启：如果监视器一起来就僵（环境根本不支持），
   * 无限重启会变成每 8 秒拉起一个 PowerShell 的**资源黑洞**，
   * 而且日志被刷爆。超过上限就放弃并**明确说出来**。
   */
  private restartCount = 0;

  /**
   * 看门狗重启用的参数（`start()` 里记下）。
   *
   * ⚠️ 必须提升成字段：原来这些是 `start()` 的局部变量，
   * 看门狗就只能"知道它僵了但拉不起来"。
   */
  private monitorScript: string | null = null;
  private monitorVk = 0;
  private monitorTempDir: string | null = null;
  private monitorOutFile = '';

  constructor(private readonly options: PushToTalkOptions) {}

  get running(): boolean {
    return this.child !== null && !this.stopped;
  }

  /**
   * 启动监视器。**失败不抛错** —— 按住说话是可选功能，
   * 它起不来不该让整个应用起不来（与 `fullscreenWatcher` 同一条取舍）。
   */
  start(): boolean {
    if (platform !== 'win32') {
      this.log('按住说话只支持 Windows（需要 GetAsyncKeyState），已跳过');
      return false;
    }
    const vk = Number(this.options.vk);
    if (!Number.isFinite(vk) || vk <= 0) {
      this.log('按住说话：没有配置按键，未启动');
      return false;
    }
    if (this.running) return true;

    const script = this.resolveScript();
    if (!script) {
      this.log('按住说话：找不到 tools/probe-key.ps1');
      return false;
    }

    this.stopped = false;
    this.down = false;

    /*
     * ## ⚠️ 必须把 PowerShell 的 TEMP 指到可写目录
     *
     * `Add-Type`（脚本里用来声明 `GetAsyncKeyState`）编译 C# 时要把中间产物
     * 写进 `%TEMP%`，而实测本环境 **`%TEMP%` 不可写**（EPERM），报的正是：
     *
     *     按住说话监视器 stderr：Add-Type : 对路径
     *     "C:\Users\…\AppData\Local\Temp\pqof3ovj.tmp" 的访问被拒绝。
     *     按住说话监视器退出了（code=1）
     *
     * **只把 .ps1 挪出 %TEMP% 是不够的** —— 那一点 `fullscreenWatcher` 已经
     * 查清并写在它的文件头里。所以要连 `TEMP`/`TMP` 一起改。
     *
     * 落点选项目内的 `pet-data/ptt-tmp`：与 `fullscreenWatcher` 的
     * `PET_PROBE_TMP`、SAPI 的临时文件同一个理由（项目内一定可写）。
     */
    const tempDir = this.ensureTempDir();

    /*
     * ## 🔴 为什么**不走管道**，改成"输出重定向到文件 + 轮询读文件"
     *
     * 实测（2026-10-08，三次独立复现）：
     *
     *     由 **Electron spawn**（管道 stdout）起的监视器：只收到 `READY`，
     *     **一次键都收不到**（`GetAsyncKeyState` 在它里面拿不到按下态）
     *     由 **PowerShell** 起的同一个脚本、同一 TEMP、同一会话：
     *     `READY / DOWN / UP` 全都有
     *
     * 已逐个排除：会话隔离（都在 SessionId=1）、`-NonInteractive`（带与不带
     * 都收到 7 次 DOWN）、读键能力本身（本进程发合成键读回 `0x8001` ✅、
     * 用户真实按键 ✅）、轮询是否在跑（3 秒 CPU 增量 31.2ms）。
     *
     * 剩下的差别只有"由 Electron 以 `windowsHide: true`（= Windows 的
     * `CREATE_NO_WINDOW`）起"。而这条路径的修法我**实测过是有效的**：
     * 让 PowerShell 的 stdout 走**文件**（`cmd /c ... > file`），
     * Node 侧改成轮询那个文件。
     *
     * ⚠️ 代价：不再是"事件驱动"，而是 80ms 轮询读文件。对一个只输出
     * "按键状态变化"行的监视器来说，这点开销可以忽略（文件通常 0 字节）。
     *
     * ⚠️ **不用 `windowsHide: true`**：既然它就是嫌疑，而且用 `cmd /c`
     * 加 `> file` 之后本来就没有控制台窗口要隐藏。
     */
    const outFile = this.outputFilePath(tempDir);
    try {
      rmSync(outFile, { force: true });
    } catch {
      /* 删不掉也无所谓，下面按偏移量读 */
    }

    /*
     * 记下这次启动的参数，供看门狗重启时复用。
     *
     * ⚠️ 原来这些是 `start()` 的局部变量，重启路径拿不到 —— 所以必须提升成字段。
     * 不这么做，看门狗就只能"知道它僵了但拉不起来"。
     */
    this.monitorScript = script;
    this.monitorVk = vk;
    this.monitorTempDir = tempDir;
    this.monitorOutFile = outFile;
    this.restartCount = 0;

    if (!this.spawnMonitor()) return false;

    this.startTailing(outFile);

    this.log(`按住说话已启动：监听 ${vkName(vk)}（VK 0x${vk.toString(16).toUpperCase()}）`);
    return true;
  }

  /**
   * 起一个监视器进程。**可重复调用**（看门狗重启时也走这里）。
   *
   * ## ⚠️ 为什么自己生成一个**包装脚本**，而不是 `cmd /c "...参数..." > file`
   *
   * 第一版我就是那么拼的 —— 结果监视器起不来：
   *
   *     按住说话监视器退出了（code=1）
   *
   * 原因：`cmd` 的引号规则很坑（嵌套引号、`>` 的归属、路径里的中文与空格），
   * 而且**坏得很难查** —— 只有一个 `code=1`，stderr 还被一起重定向进文件了。
   *
   * 改成：把参数**写成一个 .ps1 包装脚本**，让 cmd 只负责
   * "执行那个脚本 + 把输出重定向到文件"。引号问题彻底消失，
   * 而且出问题时**可以直接打开包装脚本看它到底要跑什么**。
   */
  private spawnMonitor(): boolean {
    const script = this.monitorScript;
    const vk = this.monitorVk;
    const tempDir = this.monitorTempDir;
    if (!script || !vk) {
      this.log('按住说话：启动参数没准备好，无法起监视器');
      return false;
    }

    let child: ChildProcess;
    try {
      const psArgs = [
        '-NoProfile',
        '-NonInteractive',
        '-ExecutionPolicy',
        'Bypass',
        '-File',
        script,
        '-Vk',
        String(vk),
        '-PollMs',
        String(this.options.pollMs ?? 15),
        '-OutFile',
        tempDir ? this.outputFilePath(tempDir) : '',
      ];
      child = spawn('powershell', psArgs, {
        // stdio 不接管道：输出已经由 `-OutFile` 写进文件了
        stdio: 'ignore',
        env: tempDir ? { ...process.env, TEMP: tempDir, TMP: tempDir } : process.env,
      });
    } catch (error) {
      this.log(`按住说话：启动监视器失败（${(error as Error).message}）`);
      return false;
    }

    this.child = child;

    /*
     * ⚠️ 按**行**解析，并且必须处理"一行被拆成两次读"的情况 ——
     * 文件是字节流，不保证一次读到的正好是整行。缓冲区攒着，遇到换行才处理。
     */
    child.on('exit', (code) => {
      this.child = null;
      // 退出时如果键还按着，要补一个"松开"，否则语音会一直卡在采集状态
      if (this.down) {
        this.down = false;
        try {
          this.options.onRelease();
        } catch {
          /* 回调失败无所谓 */
        }
      }
      if (!this.stopped) {
        /*
         * ⚠️ 这里**不再说"本次运行不可用"**（2026-10-10 改）。
         *
         * 现在有看门狗会把它拉起来，所以"退出了"只是个事件，
         * 不是终局。原来说"不可用"会让用户以为没救了。
         */
        this.log(`按住说话监视器退出了（code=${code}）—— 看门狗会在需要时重启`);
      }
    });

    return true;
  }

  stop(): void {
    this.stopped = true;
    this.stopTailing();
    const child = this.child;
    this.child = null;
    this.killTree(child);
  }

  /**
   * 杀掉监视器**连同它的整棵进程树**。
   *
   * ## 🔴 为什么不能只 `child.kill()`（2026-10-10 实测抓到的泄漏）
   *
   * 实测看到**孤儿监视器**：
   *
   *     PID 6260   父 8344（= Electron）   <- 当前那个，正常
   *     PID 2332   父 7084                 <- 8344 不是 Electron / 已不存在
   *
   * 而 pid 在两次查询之间还在变 —— 说明**看门狗在反复重启**，
   * 而每次 `child.kill()` 都**没杀掉真正在轮询的那个进程**。
   *
   * 原因：`spawn('powershell', …)` 起的是 PowerShell **宿主**，它下面还有
   * 真正跑 `while` 循环的那层。**Windows 上父进程终止不会带走子孙**
   * （没有 Linux 那种进程组语义），所以孙进程会变成孤儿继续轮询、
   * 继续写同一个输出文件 —— 两个监视器往一个文件里写，`DOWN`/`UP` 会错乱。
   *
   * 修法：`taskkill /F /T`（`/T` = 连子孙一起）。这是 Windows 上
   * 唯一可靠的"杀一棵树"手段。
   */
  private killTree(child: ChildProcess | null): void {
    if (!child) return;
    const pid = child.pid;
    if (pid === undefined) return;

    /*
     * 先试 `taskkill /T`（正常路径）。
     *
     * ⚠️ 同步执行：重启路径紧接着就要 `spawnMonitor()`，
     * 异步杀会和"起新的"抢同一个输出文件（旧的还在写）。
     * 代价是最多阻塞几十毫秒 —— 而这时本来就在恢复流程里，可以接受。
     */
    let killed = false;
    try {
      spawnSync('taskkill', ['/F', '/T', '/PID', String(pid)], {
        stdio: 'ignore',
        windowsHide: true,
      });
      killed = true;
    } catch {
      /* 落到下面的兜底 */
    }

    /*
     * 兜底：`child.kill()`。
     *
     * `taskkill` 也可能失败（进程刚好自己退了、或者权限问题），
     * 那就至少把直接子进程收掉。**不能因为 taskkill 失败就什么都不做**
     * —— 那正是泄漏的成因。
     */
    if (!killed && !child.killed) {
      try {
        child.kill();
      } catch {
        /* 杀不掉就算了，进程退出时会自己结束 */
      }
    }
  }

  // ---------------------------------------------------------------- 文件轮询

  /** 监视器输出文件（放 `pet-data/ptt-tmp`，与 TEMP 同处，确保可写） */
  private outputFilePath(tempDir: string | null): string {
    const dir = tempDir ?? resolve(__dirname, '..', 'pet-data', 'ptt-tmp');
    try {
      mkdirSync(dir, { recursive: true });
    } catch {
      /* 建不出来就用原路径，读不到自然没有输出 */
    }
    return resolve(dir, 'probe-key.out');
  }

  /**
   * 开始轮询监视器的输出文件。
   *
   * ## ⚠️ 为什么是轮询而不是监听事件
   *
   * 因为**管道这条路在这个环境里不通**（见 `start()` 里那段长说明）：
   * Electron spawn 出来的、stdout 走管道的监视器**收不到任何键**。
   * 改成"输出重定向到文件"之后就得自己轮询文件 —— 这是代价，也是修法。
   *
   * ⚠️ 80ms 的间隔是权衡：按键反馈的容受度是几十毫秒，而文件通常只有
   * 几十字节（只在状态变化时才写一行）。用 `statSync` 先比大小，
   * 没变就**完全不读文件**。
   */
  private startTailing(file: string): void {
    this.stopTailing();
    /*
     * ⚠️ **不要重置 tailOffset**（2026-10-10 改）。
     *
     * 原来这里重置成 0 —— 但看门狗重启监视器时会再调一次本方法，
     * 而 `probe-key.ps1` 是**覆盖写**（`StreamWriter($OutFile, $false, …)`），
     * 文件会从头开始。重置 offset 反而会让新脚本的前几行被当成"旧数据"跳过。
     *
     * 正确做法：**不重置**，让 `size <= tailOffset` 那段自然处理
     * （新文件更小 -> 下次读到更多时才继续）。为了不丢那几行，
     * 重启路径会显式把 `tailOffset` 归零（见 `restartStale()`）。
     */
    this.tailTimer = setInterval(() => this.readNewOutput(file), 80);
    this.startWatchdog(file);
  }

  private stopTailing(): void {
    if (this.tailTimer) {
      clearInterval(this.tailTimer);
      this.tailTimer = null;
    }
    if (this.watchdogTimer) {
      clearInterval(this.watchdogTimer);
      this.watchdogTimer = null;
    }
  }

  /**
   * 看门狗：心跳断了就把监视器**杀掉重启**。
   *
   * ## 阈值怎么定
   *
   * 心跳约 3 秒一条（`probe-key.ps1` 里 `$heartbeatEvery=200` × 15ms）。
   * 取 **8 秒**——约 2.7 个心跳周期没来才算僵：
   *   · 太小（如 4 秒）会在机器卡顿、GC、系统繁忙时**误判重启**，
   *     而重启本身有代价（`Add-Type` 编译要几百毫秒，那期间按键无效）
   *   · 太大（如 30 秒）用户已经说完三句话了
   *
   * ## 为什么重启上限是 5 次
   *
   * 如果监视器一起来就僵（环境根本不支持），无限重启会变成
   * 每 8 秒拉起一个 PowerShell 的**资源黑洞**。5 次够覆盖偶发僵死，
   * 又不会失控；到顶后**明确记一条日志**说"该功能本次运行不可用"，
   * 而不是继续默默重试。
   */
  private startWatchdog(file: string): void {
    const STALE_MS = 8000;
    const MAX_RESTARTS = 5;
    this.lastAliveAt = Date.now();

    this.watchdogTimer = setInterval(() => {
      if (this.stopped) return;
      const idle = Date.now() - this.lastAliveAt;
      if (idle < STALE_MS) return;

      if (this.restartCount >= MAX_RESTARTS) {
        if (this.restartCount === MAX_RESTARTS) {
          this.restartCount += 1; // 只喊一次
          this.log(
            `按住说话监视器反复僵住（已重启 ${MAX_RESTARTS} 次）—— ` +
              '该功能本次运行不可用。重启应用可再试',
          );
        }
        return;
      }

      this.restartCount += 1;
      this.log(
        `🔴 按住说话监视器**僵住**了（${Math.round(idle / 1000)} 秒没有心跳）` +
          `—— 正在重启（第 ${this.restartCount} 次）`,
      );
      this.restartStale(file);
    }, 2000);
  }

  /**
   * 杀掉僵住的监视器并重新拉起。
   *
   * ⚠️ 顺序：**先 readNewOutput 再换进程**。
   * 万一僵住之前刚写了一个 `DOWN` 还没被读到，先读一次能把它捞回来 ——
   * 否则那次按键就白按了（用户会看到"按了没反应"）。
   */
  private restartStale(file: string): void {
    try {
      this.readNewOutput(file); // 抢救最后几行
    } catch {
      /* 读不到就算了 */
    }
    const old = this.child;
    this.child = null;
    this.killTree(old);
    // 新进程会**覆盖写**这个文件（见 probe-key.ps1），所以偏移要归零
    this.tailOffset = 0;
    this.tailBuffer = '';
    this.lastAliveAt = Date.now();

    if (!this.spawnMonitor()) {
      this.log('按住说话监视器重启失败');
    }
  }

  /** 只读文件里**新增**的那部分，按行交给 `consumeLine`。 */
  private readNewOutput(file: string): void {
    let size: number;
    try {
      size = statSync(file).size;
    } catch {
      return; // 文件还没建出来（监视器刚起）
    }
    if (size <= this.tailOffset) return;

    let chunk: string;
    try {
      // 用 fd 读到指定区间：`readFileSync` 不支持偏移，所以开个 fd
      const fd = openSync(file, 'r');
      try {
        const len = size - this.tailOffset;
        const buf = Buffer.allocUnsafe(len);
        const read = readSync(fd, buf, 0, len, this.tailOffset);
        chunk = buf.subarray(0, read).toString('utf8');
      } finally {
        closeSync(fd);
      }
    } catch {
      return;
    }
    this.tailOffset = size;
    /*
     * 🔴 **读到东西 = 监视器活着**（2026-10-10 加）。
     *
     * 这是看门狗唯一的存活信号来源。`probe-key.ps1` 每约 3 秒写一条
     * `TICK`，所以"8 秒没有任何新行"就说明循环停了 —— 见 `startWatchdog`。
     *
     * ⚠️ 放在这里（读成功之后），不是 `statSync` 之后：
     * 文件大小变化但读失败（权限/占用）不该算"活着"。
     */
    this.lastAliveAt = Date.now();

    this.tailBuffer += chunk;
    const lines = this.tailBuffer.split(/\r?\n/);
    // 最后一段可能是不完整的行，留到下次
    this.tailBuffer = lines.pop() ?? '';
    for (const line of lines) this.consumeLine(line.trim());
  }

  /**
   * 收一行监视器输出。
   *
   * ⚠️ **每一行都记日志**（2026-10-08 加）。原来"能识别的行"（READY/DOWN/UP）
   * 只在真的触发回调时才留痕，于是"收到了却没用上"这种情况**完全静默** ——
   * 排查时分不清"没收到"和"收到没处理"。加了这一行之后一眼可分：
   *
   *     日志里有 `监视器输出 <- DOWN 165` 但没有「按下」-> 收到了没处理
   *     连 `监视器输出 <-` 都没有                          -> 根本没收到
   */
  private consumeLine(raw: string): void {
    if (!raw) return;
    const ms = String(Date.now() % 100000).padStart(5, '0');
    this.log(`监视器输出 <- ${raw.slice(0, 80)}  [t=${ms}]`);
    this.handleLine(raw);
  }

  private handleLine(line: string): void {
    if (!line) return;
    if (line.startsWith('READY')) {
      this.log(`按住说话监视器就绪（${line}）`);
      return;
    }
    /*
     * 心跳（2026-10-10 加）。
     *
     * **内容不看，只当"我还活着"的信号** —— 见 `probe-key.ps1` 里那段的说明。
     *
     * ⚠️ 不记日志：3 秒一条，一天下来几千行，而它对用户毫无意义。
     * 真正要留痕的是"**心跳断了**"那一刻（看门狗会记）。
     */
    if (line.startsWith('TICK')) return;
    if (line.startsWith('DOWN')) {
      if (this.down) return; // 去重
      this.down = true;
      try {
        this.options.onPress();
      } catch (error) {
        this.log(`按住说话 onPress 抛错：${(error as Error).message}`);
      }
      return;
    }
    if (line.startsWith('UP')) {
      if (!this.down) return;
      this.down = false;
      try {
        this.options.onRelease();
      } catch (error) {
        this.log(`按住说话 onRelease 抛错：${(error as Error).message}`);
      }
      return;
    }
    this.log(`按住说话监视器输出（未识别）：${line.slice(0, 160)}`);
  }

  /**
   * 找 `probe-key.ps1`。
   *
   * ⚠️ 试多个落点而不是只认一个：打包后 `__dirname` 会变，而开发时
   * cwd 可能是 `apps/desktop` 也可能是仓库根。**只看一个路径**就会
   * 出现"开发时好用、打包后静默失效"（`fullscreenWatcher` 踩过）。
   */
  private resolveScript(): string | null {
    const candidates = [
      resolve(dirname(process.argv[1] ?? '.'), '..', 'tools', 'probe-key.ps1'),
      resolve(process.cwd(), 'tools', 'probe-key.ps1'),
      resolve(__dirname, '..', 'tools', 'probe-key.ps1'),
    ];
    for (const candidate of candidates) {
      if (existsSync(candidate)) return candidate;
    }
    return null;
  }

  /**
   * PowerShell 的 TEMP 落点（`Add-Type` 需要它可写）。
   *
   * 与 `fullscreenWatcher.ensureTempDir` 同一个思路，但**不复用它** ——
   * 那个函数的生命周期绑在全屏轮询上（它会 rmSync 掉），两个功能共用一个
   * 目录时，一个停掉会把另一个正在用的临时目录删掉。各自一份更安全。
   */
  private ensureTempDir(): string | null {
    try {
      const dir = resolve(__dirname, '..', 'pet-data', 'ptt-tmp');
      mkdirSync(dir, { recursive: true });
      return dir;
    } catch {
      // 建不出来就退回系统 TEMP（那时 Add-Type 可能失败，但至少路径是对的）
      return null;
    }
  }

  private log(line: string): void {
    this.options.onLog?.(line);
  }
}


/*
 * ===========================================================================
 *  聚焦时的**真正按住说话**（`before-input-event`）—— 用户要的右 Ctrl / 右 Alt
 * ===========================================================================
 *
 * ## 实测依据（tools/test-before-input-focused.py）
 *
 * ```
 * 按住右 Ctrl：
 *   keyDown code="ControlRight" ctrl=true  autoRepeat=false   <- 按下
 *   keyDown ...                 autoRepeat=true  × 28          <- 自动重复
 *   keyUp   code="ControlRight" ctrl=false autoRepeat=false   <- 松开 ✅
 *
 * 按住右 Alt：
 *   keyDown code="AltRight"     alt=true   autoRepeat=false
 *   keyDown ...                 autoRepeat=true  × 28
 *   keyUp   code="AltRight"     alt=false  autoRepeat=false   <- 松开 ✅
 * ```
 *
 *  ① **keydown + keyup 都有** —— 真正的「按住说话」，**不需要原生钩子**
 *  ② **单个修饰键能识别**，`code` 把左右分得清清楚楚
 *     （`ControlRight` / `AltRight`）—— 这正是 accelerator 做不到的
 *  ③ **自动重复带 `autoRepeat` 标记** —— 去重白送，不用自己维护状态机
 *     （`WH_KEYBOARD_LL` 钩子那次一次按住发了 150+ 条 DOWN）
 *
 * ## ⚠️ 只在窗口聚焦时有效
 *
 * 实测：**失焦时 0 个事件**（与「只有在 Electron 窗口内才可以触发事件」一致）。
 * 所以它和 `GlobalShortcutPtt` **并存**：聚焦时用这个（真按住），
 * 不聚焦时用那个（切换式）。
 */

/** `before-input-event` 的 `input` 参数里我们用到的那几个字段 */
export interface BeforeInputLike {
  type: string;
  key: string;
  code: string;
  control: boolean;
  alt: boolean;
  shift: boolean;
  meta: boolean;
  isAutoRepeat: boolean;
}

/**
 * Windows VK -> DOM `code`。
 *
 * ## 为什么需要它
 *
 * 配置里存的是 **VK**（设置页的按键控件、`vkName()` 都按 VK 工作），
 * 而 `before-input-event` 给的是 **DOM `code`**（`ControlRight` / `F9`…）。
 * 换算只在这一处 —— **配置保持单一真源**。
 *
 * ## ⚠️ 左右修饰键**必须分开**（这正是这个方案的价值）
 *
 * `0xA3`（右 Ctrl）-> `ControlRight`，`0xA2`（左 Ctrl）-> `ControlLeft`。
 * 用位置无关的 `ctrl=true` 会让左右不分 —— 那用户按左 Ctrl 也会触发。
 *
 * ## ⚠️ 未映射的键返回 null，不猜
 *
 * 猜错的后果是"监听了一个用户没按的键"，表现是"按了没反应"，
 * 而日志里一切正常 —— 这个项目在这上面栽过太多次。
 */
export function vkToDomCode(vk: number): string | null {
  if (!Number.isFinite(vk) || vk <= 0) return null;
  const v = Math.trunc(vk);

  // 字母：KeyA..KeyZ
  if (v >= 0x41 && v <= 0x5a) return `Key${String.fromCharCode(v)}`;
  // 数字：Digit0..Digit9
  if (v >= 0x30 && v <= 0x39) return `Digit${String.fromCharCode(v)}`;
  // 功能键：F1..F24（DOM code 就是 F1..F24，连续）
  if (v >= 0x70 && v <= 0x87) return `F${v - 0x6f}`;
  // 小键盘：Numpad0..Numpad9
  if (v >= 0x60 && v <= 0x69) return `Numpad${v - 0x60}`;

  const TABLE: Record<number, string> = {
    /*
     * 修饰键 —— **左右分开**。这是本方案相对 accelerator 的核心优势：
     * `Control` / `Alt` 这类修饰键在 accelerator 里不是 key code，
     * 但在这里有独立的 `code`。
     */
    0xa3: 'ControlRight',
    0xa2: 'ControlLeft',
    0x11: 'ControlLeft', // 通用 Ctrl 只能落到左（DOM 没有位置无关的 code）
    0xa5: 'AltRight',
    0xa4: 'AltLeft',
    0x12: 'AltLeft',
    0xa1: 'ShiftRight',
    0xa0: 'ShiftLeft',
    0x10: 'ShiftLeft',
    0x5c: 'MetaRight',
    0x5b: 'MetaLeft',
    // 编辑与控制
    0x20: 'Space',
    0x0d: 'Enter',
    0x1b: 'Escape',
    0x09: 'Tab',
    0x08: 'Backspace',
    0x2e: 'Delete',
    0x2d: 'Insert',
    0x24: 'Home',
    0x23: 'End',
    0x21: 'PageUp',
    0x22: 'PageDown',
    0x25: 'ArrowLeft',
    0x26: 'ArrowUp',
    0x27: 'ArrowRight',
    0x28: 'ArrowDown',
    // 符号（US 布局）
    0xc0: 'Backquote',
    0xbd: 'Minus',
    0xbb: 'Equal',
    0xdb: 'BracketLeft',
    0xdd: 'BracketRight',
    0xdc: 'Backslash',
    0xba: 'Semicolon',
    0xde: 'Quote',
    0xbc: 'Comma',
    0xbe: 'Period',
    0xbf: 'Slash',
    /*
     * 小键盘运算符：`0x6A-0x6F`（**和数字键 0x60-0x69 不连续**，所以不能用公式）。
     *
     * ⚠️ 顺带核对过：导航键（`0x21-0x28`、`0x2D-0x2E`）与
     * `0x60-0x69` 的**小键盘数字键不重叠** —— 所以上面那句范围判断
     * 不会误吞 Delete / PageDown。
     */
    0x6a: 'NumpadMultiply',
    0x6b: 'NumpadAdd',
    0x6c: 'NumpadSeparator',
    0x6d: 'NumpadSubtract',
    0x6e: 'NumpadDecimal',
    0x6f: 'NumpadDivide',
    // 锁定键（`0x91` ScrollLock 是个很好用的"按钮键"：平时完全不用）
    0x14: 'CapsLock',
    0x90: 'NumLock',
    0x91: 'ScrollLock',
    /*
     * 媒体键 —— **很值得推荐当按住说话的键**：
     * 平时打字/游戏都不会按到，而且键盘上通常有独立的实体键。
     */
    0xad: 'AudioVolumeMute',
    0xae: 'AudioVolumeDown',
    0xaf: 'AudioVolumeUp',
    0xb0: 'MediaTrackNext',
    0xb1: 'MediaTrackPrevious',
    0xb2: 'MediaStop',
    0xb3: 'MediaPlayPause',
  };
  return TABLE[v] ?? null;
}

/** `FocusedKeyPtt` 的回调 */
export interface FocusedKeyPttOptions {
  /** 要监听的 **VK**（与配置里存的、设置页显示的一致） */
  vk: number;
  /** 按下（只在**非**自动重复时触发） */
  onPress: () => void;
  /** 松开 */
  onRelease: () => void;
  onLog?: (line: string) => void;
}

/**
 * **聚焦时**的按住说话：监听宠物窗口的 `before-input-event`。
 *
 * 与 `GlobalShortcutPtt` 的分工见文件末尾那段说明 ——
 * 这个只在窗口聚焦时有效，但**能拿到松开**，所以是真正的"按住说话"。
 */
export class FocusedKeyPtt {
  private code: string | null = null;
  private down = false;
  private active = false;

  constructor(private readonly options: FocusedKeyPttOptions) {}

  /** 真的在监听吗（设置页据此显示状态） */
  get running(): boolean {
    return this.active;
  }

  /**
   * 把监听挂到某个 `webContents` 上。
   *
   * ⚠️ 参数用结构化类型而不是 `Electron.WebContents`：这样单测里能直接传
   * 一个假对象（`test-main.mjs` 就是这么测的），不必起 Electron。
   */
  attach(contents: {
    on(
      event: 'before-input-event',
      listener: (event: unknown, input: BeforeInputLike) => void,
    ): void;
  }): boolean {
    const code = vkToDomCode(this.options.vk);
    if (code === null) {
      this.options.onLog?.(
        `按住说话（聚焦）：该键不受支持（vk=${this.options.vk}）—— 请在设置页换一个键`,
      );
      return false;
    }
    this.code = code;

    contents.on('before-input-event', (_event, input) => {
      // 只认配置的那个键（按 DOM code 精确匹配 —— 左右修饰键分得清）
      if (input.code !== this.code) return;

      if (input.type === 'keyDown') {
        /*
         * ⚠️ **自动重复必须丢掉**。
         *
         * 按住一个键时 Chromium 每 ~30ms 重发一次 keyDown，只是带了
         * `isAutoRepeat: true`（实测一次按住 28 条）。不丢的话
         * 会反复触发"开始采集"。
         *
         * 这里的去重是**白送的**（事件自带标记）；而钩子那版要自己维护
         * `_isDown` 状态机 —— 这是本方案更简单的地方。
         */
        if (input.isAutoRepeat) return;
        if (this.down) return; // 双保险
        this.down = true;
        try {
          this.options.onPress();
        } catch (error) {
          this.options.onLog?.(`按住说话 onPress 抛错：${(error as Error).message}`);
        }
        return;
      }

      if (input.type === 'keyUp') {
        if (!this.down) return; // 没按过就松开（例如启动时键是压着的）
        this.down = false;
        try {
          this.options.onRelease();
        } catch (error) {
          this.options.onLog?.(`按住说话 onRelease 抛错：${(error as Error).message}`);
        }
      }
    });

    this.active = true;
    this.options.onLog?.(
      `按住说话（聚焦时）：监听 ${vkName(this.options.vk)}` +
        `（vk=${this.options.vk} -> code=${code}）—— 按住说，松开结束`,
    );
    return true;
  }

  /** 让状态机回到"没按下"（窗口失焦时要调，否则下次按下会被 `this.down` 挡掉） */
  reset(): void {
    this.down = false;
  }

  stop(): void {
    this.active = false;
    this.down = false;
  }
}
