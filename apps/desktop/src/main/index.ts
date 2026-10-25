/**
 * Electron 主进程入口：把窗口、托盘、sidecar、socket 缝起来。
 *
 * 启动顺序很重要：
 *   窗口先起来（用户立刻看到反馈）→ 拉 sidecar → 连上后再开启转发。
 * 反过来做的话，sidecar 启动慢会让用户盯着一个空白桌面发呆好几秒。
 *
 * 这个文件只做编排与事件转发，业务逻辑一律不放在这里 ——
 * 前端协议细节在 petSocket，进程管理在 sidecar，窗口/托盘各有专责。
 */

// 必须放在最前：清掉宿主继承来的 ELECTRON_RUN_AS_NODE。
// 它会让 Electron 退化成普通 Node，所有 electron API 都是 undefined。
import './bootstrap.js';

import {
  app,
  BrowserWindow,
  dialog,
  globalShortcut,
  ipcMain,
  Menu,
  protocol,
  screen,
  shell,
} from 'electron';
import path from 'node:path';
import fs, {
  existsSync,
  readFileSync,
  readdirSync,
  statSync,
  writeFileSync,
  unlinkSync,
} from 'node:fs';
import { spawn, type ChildProcess, execFile } from 'node:child_process';
import { PetSocket } from './petSocket.js';
import { AudioPairer } from './audioPairer.js';
import { synthesizeSpeech } from './ttsLocal.js';
import { findPython, SidecarManager, type ReadyInfo } from './sidecar.js';
import { PetWindow } from './petWindow.js';
import { SettingsWindow } from './settingsWindow.js';
import { MemoryWindow } from './memoryWindow.js';
import { PetTray } from './tray.js';
import { SHORTCUTS } from './shortcuts.js';
import { FocusedKeyPtt, GlobalShortcutPtt, DEFAULT_PTT_VK, vkName } from './pushToTalk.js';
import { QuitCoordinator, setQuitStateDir } from './quitCoordinator.js';
import { SingleInstanceGuard, setStateDir } from './singleInstance.js';
import { decideBubble, decideBubblePlacement } from './bubbleDecision.js';
import { popupPetMenu, dispatchPetMenuAction, type PetMenuHandlers } from './petMenu.js';
import {
  buildPetMenuTemplate,
  WEB_SEARCH_LABEL,
  WEB_SEARCH_MODES,
  type PetMenuState,
  type WebSearchMode,
} from './petMenuTemplate.js';
import { PetMenuWindow, type PetMenuAction } from './petMenuWindow.js';
/**
 * 浏览器兜底（2026-10-13）。
 *
 * 普通抓取读不了的页面（起点那种 WAF 挑战）用隐藏的 Chromium 窗口去读。
 * **默认关闭**，由设置里的「浏览器兜底」开关打开 ——
 * 理由见 `browserFetch.ts` 的文件头（隐私口径 + 慢是本质的）。
 */
import {
  fetchPageViaBrowser,
  disposeBrowserFallback,
  browserDataInfo,
  clearBrowserData,
  formatBytes,
} from './browserFetch.js';
import { FullscreenWatcher, setFullscreenProbeDir } from './fullscreenWatcher.js';
import {
  readSettings,
  writeSettings,
  setSettingsDir,
  setSettingsFallbackDir,
} from './settingsStore.js';
import { ChatWindow, placeChatBar, preferOppositeSide } from './chatWindow.js';
import { BubbleWindow } from './bubbleWindow.js';
import { StatusWindow, type StatusKind } from './statusWindow.js';
import { brainStatusLabel } from './brainProvider.js';
// 宠物大小的**档位清单**与像素换算：设置页下拉框、托盘菜单、右键菜单
// 三处都从这里取，避免出现"三份硬编码清单互相不一致"（改之前就是这样）。
import { PET_SCALE_PRESETS, petSizeFor } from './petLayout.js';
import {
  LOG_MAX_BYTES,
  logFileBytes,
  logLineBytes,
  rotateLogIfNeeded,
  rotateLogNow,
} from './logRotation.js';
import { listModels, testConnection } from './settingsApi.js';
import {
  readNestedScalar,
  readDeepScalar,
  readList,
  readPersonaPrompt,
  readScalar,
  writeNestedScalar,
  writeDeepScalar,
  writeList,
  writePersonaEnabled,
  writePersonaPrompt,
  writeScalar,
} from './personaStore.js';
import {
  credentialsPath,
  activeCredentialsPath,
  describeCredentials,
  maskKey,
  readCredentials,
  setCredentialsFallbackDir,
  writeCredentials,
} from './credentials.js';
import { PROTOCOL_VERSION, type ToUiMsg, type ToBrainMsg } from '@pet/protocol';

/* ------------------------------------------------------------------ GPU / 沙箱 */

/**
 * 关于 GPU：**默认不动它**，而且这里不能再默认调
 * `app.disableHardwareAcceleration()`。
 *
 * ⚠️ 踩过的坑（花了很久才定位）：早先为了绕开 GPU 进程启动失败，我默认调用了
 * `disableHardwareAcceleration()` 并追加了 `--disable-gpu-compositing`。
 * 结果是**窗口彻底不可见** —— 不是黑底，是"存在、IsWindowVisible 为真、
 * 位置尺寸都对，但屏幕上什么都没有"。
 *
 * 原因是 Windows 上的透明窗依赖 DWM 合成，而"软件渲染 + transparent:true"
 * 这个组合会让窗口画不出来。所以对透明窗来说，关 GPU 的代价比收益大得多：
 * GPU 起不来时至少还有 `--no-sandbox` 可以救（它才是针对子进程创建的开关），
 * 而关掉 GPU 会让窗口直接消失。
 *
 * 需要关 GPU 时（比如某些老驱动）：设 `PET_GPU=0`。这时请同时把
 * `petWindow.ts` 里的 `transparent` 关掉，否则窗口会看不见 —— 二者不能共存。
 */
const gpuPreference = (process.env.PET_GPU ?? '').trim().toLowerCase();
if (gpuPreference === '0' || gpuPreference === 'off' || gpuPreference === 'false') {
  app.disableHardwareAcceleration();
  app.commandLine.appendSwitch('disable-gpu');
  process.env.PET_GPU_DISABLED = '1';
  // 提醒：透明窗在软件渲染下会不可见，所以这里有一条硬性建议
  console.warn(
    '[pet] 已按 PET_GPU=0 关闭硬件加速。注意：透明窗在软件渲染下会完全不可见，' +
      '需要同时关闭 petWindow.ts 里的 transparent。',
  );
}

// 主进程打成 CommonJS（`dist/main.cjs`）—— Electron 的 ESM 加载器无法对内置
// electron 模块做 CJS 具名导出解析，ESM 主进程会在启动时直接崩。
// 因此这里只能用 __dirname，**不要**用 import.meta.url（CJS 下它是 undefined，
// fileURLToPath 会抛 ERR_INVALID_ARG_TYPE）。
const DIST_DIR = __dirname; // apps/desktop/dist

/*
 * ⚠️ 打包后必须先定名字，再算任何 userData 路径（2026-10-18）。
 *
 * Electron 的 `app.getPath('userData')` = `%APPDATA%\<app name>`，
 * 而 app name 默认来自 package.json 的 `name`。本项目的 name 是 `@pet/desktop`，
 * electron-builder 打出来的包里没有 productName 字段 —— 于是会退化成
 * `%APPDATA%\Electron`：数据全写错地方，还和别的 Electron 应用混在一起。
 *
 * 必须在模块顶层调用：下面的 DATA_DIR 就是用它算的。
 */
if (app.isPackaged) app.setName('DesktopPet');

/*
 * 便携/测试模式：`PET_USER_DATA` 覆盖 userData（2026-10-18）。
 *
 * 为什么需要：本机 `%APPDATA%\Roaming` **不可写**（Electron 和 Python 都报
 * `WinError 5 拒绝访问`），打包版一启动就因为建不了 userData 直接退出。
 * 这既是环境限制，也是"用户把程序装在受限目录"时的真实场景。
 *
 * 真实用户不设这个变量，行为不变（`%APPDATA%\DesktopPet`）。
 */
if (process.env.PET_USER_DATA && process.env.PET_USER_DATA.trim()) {
  try {
    app.setPath('userData', path.resolve(process.env.PET_USER_DATA.trim()));
  } catch {
    // 设不了就退回默认，不阻塞启动
  }
}

const PROJECT_ROOT =
  process.env.PET_PROJECT_ROOT || path.resolve(DIST_DIR, '..', '..', '..'); // -> 仓库根

/*
 * ---------------------------------------------------------------- 运行时根
 * 2026-10-18 打包：把"一个根"拆成三个（见 tools 里的补丁说明）。
 *
 * 开发态三者都指向仓库，行为和以前完全一致；
 * 打包态分别指向 resources（只读）与 %APPDATA%\桌宠（可写）。
 *
 * ⚠️ 为什么配置要放用户目录：`writeDeepScalar` 会**写** persona.yaml
 * （模块开关、push-to-talk、activity 都写它）。放进 Program Files 就没有写权限，
 * 表现为"设置改了、重启又变回去"。
 */
const APP_ROOT = app.isPackaged ? process.resourcesPath : PROJECT_ROOT;
const DATA_DIR = app.isPackaged
  ? path.join(app.getPath('userData'), 'pet-data')
  : path.join(PROJECT_ROOT, 'apps', 'desktop', 'pet-data');
const CONFIG_ROOT = app.isPackaged ? app.getPath('userData') : PROJECT_ROOT;

// 启动自检：electron 模块必须真的可用。若宿主又塞回了 ELECTRON_RUN_AS_NODE，
// 这里会立刻给出可读原因，而不是等到某处 "reading 'getPath'" 才莫名其妙地崩。
if (!app || typeof app.getPath !== 'function') {
  fs.mkdirSync(DIST_DIR, { recursive: true });
  fs.writeFileSync(
    path.join(DIST_DIR, 'startup-error.txt'),
    'Electron 应用层未初始化：require("electron") 没有返回 API。\n' +
      '最常见原因是环境变量 ELECTRON_RUN_AS_NODE 被宿主设置成了 1\n' +
      '（src/main/bootstrap.ts 已在入口处清理，若仍出现请检查是否有其他注入方式）。\n',
    'utf-8',
  );
  process.exit(3);
}

const DEV_MODE = process.argv.includes('--dev');

/**
 * `--debug-port=9222` 打开远程调试端口。
 *
 * 为什么加这个：宠物窗口是**透明无边框**的，没法像普通窗口那样右键"检查"，
 * 而调 UI（旋转/位移/贴边判定）时最需要的是**开发者工具里的元素与样式面板** ——
 * 光看日志看不出 `transform` 到底算成了什么。
 *
 * 打开后用你自己的浏览器访问 `http://localhost:9222` 就能选目标调试
 * （宠物 / 对话条 / 设置三个页面都能看到）。
 *
 * ⚠️ 必须在 `app.whenReady()` **之前**加这个开关，之后再设无效。
 * 而且只在显式传参时启用 —— 调试端口不该在正常运行时开着。
 */
{
  const arg = process.argv.find((a) => a.startsWith('--debug-port'));
  if (arg) {
    const port = Number(arg.split('=')[1]) || 9222;
    app.commandLine.appendSwitch('remote-debugging-port', String(port));
    // 监听所有网卡可能被局域网访问，这里限定本机
    app.commandLine.appendSwitch('remote-debugging-address', '127.0.0.1');
    console.log(`[debug] 远程调试端口已打开：http://localhost:${port}`);
  }
}

/*
 * 主日志落点。
 *
 * ## ⚠️ 为什么不用 `app.getPath('userData')`（2026-10-09 实测）
 *
 * 原来是 `path.join(app.getPath('userData'), 'logs', 'pet.log')` ——
 * 而**这个 Electron 进程写不进去**，`startup.log` 里抓到了实证：
 *
 *     ⚠️ 主日志写入失败 ...\@pet\desktop\logs\pet.log：
 *        EPERM: operation not permitted, open '...'
 *
 * 注意：用 PowerShell 手写同一个路径是**成功**的（我验过）——
 * 所以不是权限配置问题，而是**这个环境下 Electron 进程写 %APPDATA% 被拒**。
 * 和之前踩过的那几个坑同类（SAPI 用不了、管道 stdio 的子进程失去能力、
 * `cmd` 起不来）。
 *
 * ## 落点选择
 *
 * `apps/desktop/dist/startup.log` **一直在正常写** —— 证明**项目目录下
 * Electron 主进程能写**。所以主日志也放项目目录：
 *
 *     pet-data/main.log     <- 首选（和 python.log 并排，随手能打开）
 *     dist/main.log         <- 兜底一（startup.log 证明可写）
 *     userData/logs/pet.log <- 兜底二（老路径，万一别的机器上这里才行）
 *
 * **顺序体现"可用性优先"：能落在哪儿就落哪儿，别把日志丢了。**
 */
const LOG_CANDIDATES = [
  path.join(DATA_DIR, 'main.log'),
  path.join(DIST_DIR, 'main.log'),
  path.join(app.getPath('userData'), 'logs', 'pet.log'),
];

/**
 * 主日志的**按大小轮转**（实现见 `logRotation.ts` —— 抽出去是为了可测）。
 *
 * 这里只保留"何时转"的判断：`log()` 按 `activeLogBytes` 记账，写之前发现会
 * 写超就当场轮转；首次选定文件时再兜一道 `rotateLogIfNeeded`，收掉上次会话
 * 遗留的超大文件。
 *
 * ## 🔴 2026-10-14：为什么不再"只在启动时检查一次"
 *
 * 用户报「日志占了几百 MB」。实测 `main.log.1` **110 MB**、`main.log.2` 27 MB，
 * 而上限写的是 20 MB —— 数字对不上，因为**桌宠是长期挂机的程序**：
 * 只要不重启就永远不轮转，一次会话就能写爆磁盘。
 */

/**
 * 失败标记：**启动最开始写下它，窗口真正显示出内容之后删掉**。
 *
 * 存在的唯一含义是"这一次没能显示出窗口"。启动器据此在下次启动时自动降级到
 * `--no-sandbox` 重试 —— 有些 Windows 环境不允许 Chromium 起子进程，表现为
 * GPU 进程反复启动失败然后整个应用直接退出，窗口一次都没出现。
 *
 * 为什么不用退出码判断：正常关窗和崩溃的退出码可能相同，猜不准。
 * 为什么"启动就写"：如果写成"成功才写"，那么崩溃时什么都留不下，启动器
 * 永远等不到那个标记，降级路径就永远不会触发（这个反逻辑我踩过一次）。
 */
const LAUNCH_MARKER = path.join(app.getPath('userData'), '.launch-failed');
try {
  fs.writeFileSync(LAUNCH_MARKER, String(process.pid), 'utf-8');
} catch {
  /* 写不了就算了，只是少一层自动降级 */
}

// --------------------------------------------------------------------------- //
// 启动诊断：任何"启动失败但一言不发"都是缺陷
//
// 这一层是为了解决一个具体的、真实发生过的现象：双击启动后进程以**退出码 0**
// 静默结束，屏幕上既没有窗口、日志里也没有任何记录 —— 用户完全无从下手。
//
// 最容易踩到的就是下面那条单实例检查：如果已经有一个实例在运行（或者存在一把
// 陈旧的 SingletonLock），`requestSingleInstanceLock()` 返回 false，代码直接
// `app.quit()`，退出码是 0，什么都不说。
//
// 所以这里在磁盘上留一份**启动过程记录**，供启动器在启动失败时读出来展示。
// 它不替代正常日志（那个要等窗口建好之后），而是覆盖"窗口之前"这一段空白。
// --------------------------------------------------------------------------- //

const STARTUP_LOG = path.join(DIST_DIR, 'startup.log');

function startupNote(message: string): void {
  try {
    fs.mkdirSync(DIST_DIR, { recursive: true });
    fs.appendFileSync(
      STARTUP_LOG,
      `${new Date().toISOString()} ${message}\n`,
      'utf-8',
    );
  } catch {
    /* 诊断日志写失败不能影响主流程 */
  }
}

function writeStartupFailure(where: string, error: unknown): void {
  const detail = error instanceof Error ? `${error.name}: ${error.message}` : String(error);
  const stack = error instanceof Error && error.stack ? `\n${error.stack}` : '';
  try {
    fs.mkdirSync(DIST_DIR, { recursive: true });
    fs.writeFileSync(
      STARTUP_LOG,
      `${new Date().toISOString()} 启动失败于 ${where}\n${detail}${stack}\n`,
      'utf-8',
    );
  } catch {
    /* 同上 */
  }
}

// 清掉上一次的痕迹，这样"文件里有内容"就一定是本次启动写的
try {
  fs.rmSync(STARTUP_LOG, { force: true });
} catch {
  /* 忽略 */
}

startupNote(`模块加载完成，electron=${process.versions.electron ?? '?'}`);

// 兜底：任何未捕获的异常都记下来，否则用户只会看到进程消失
process.on('uncaughtException', (error) => {
  writeStartupFailure('uncaughtException', error);
  log(`主进程未捕获异常：${(error as Error).message}`);
});
process.on('unhandledRejection', (reason) => {
  startupNote(`未处理的 Promise 拒绝：${String(reason)}`);
});

let petWindow: PetWindow | null = null;
let settingsWindow: SettingsWindow | null = null;

/**
 * 记忆管理窗口（独立于设置窗口）。
 *
 * 用户要求：「设置单开一页记忆管理，里面采用 tab 页管理」。
 * 它与设置窗口各自独立开关 —— 记忆管理要反复打开查看，可以一直开着。
 */
let memoryWindow: MemoryWindow | null = null;
let tray: PetTray | null = null;
/** sidecar 最近一次回报的**生效状态**，供设置窗口与托盘显示 */
let lastReady: {
  brain?: string;
  persona?: string;
  vision?: { enabled?: boolean; provider?: string; watching?: boolean };
  config?: Record<string, unknown>;
} | null = null;
let sidecar: SidecarManager | null = null;
/** 全屏检测轮询器（"别人全屏时宠物趴到右下角"） */
let fullscreenWatcher: FullscreenWatcher | null = null;
/** 对话条窗口（输入框 + 发送按钮，独立于宠物窗口） */
let chatWindow: ChatWindow | null = null;
/** 文字弹窗：独立窗口，浮在宠物头顶上方 */
let bubbleWindow: BubbleWindow | null = null;
/**
 * 状态条窗口（2026-10-11 用户要求）—— 贴屏幕左/右边缘的竖条。
 *
 * 显示「颜色灯 + 竖排文字」；联网搜索时灯变黄、文字变「联网搜索中...」。
 *
 * ⚠️ **和语音提示严格分开**：语音的「在听你说…」/「识别中…」仍在宠物窗口的
 * `#toast` 上（用户明确要求「语音的还是放在宠物身上执行」）。
 * 两块状态的归属不要混。
 */
let statusWindow: StatusWindow | null = null;

/**
 * 宠物右键菜单窗口（2026-10-14 用户要求按设计稿重做）。
 *
 * ## 为什么换了实现
 *
 * 原来是 Electron 原生菜单 —— 而它的 `MenuItemConstructorOptions` 里
 * **一个样式相关的属性都没有**，做不出设计稿那种卡片 / 开关 / 图标 / 分区线。
 * 所以换成无边框透明窗里自绘（`petMenuWindow.ts` + `renderer/petMenu.*`）。
 *
 * ⚠️ **常驻复用**：第一次右键建好，之后只 hide / show。
 *    每次右键都新建窗口的话，同一次右键要等"建窗 + 加载页面 + 量尺寸"
 *    才出现，菜单会明显慢半拍 —— 而菜单是必须"跟着指针立刻出现"的东西。
 */
let petMenuWindow: PetMenuWindow | null = null;

/**
 * 最近一次打开菜单用的**处理器表**。
 *
 * ## 为什么要有这个变量（而不是让 IPC 自己去构造一份）
 *
 * 处理器要读一堆主进程的实时状态（`chatVisible` / `paused` / `activity` /
 * `micDeviceCache` …），只有 `openPetMenu()` 那个作用域里能拿到。
 * 而点击是**之后**从渲染层通过 IPC 回来的，那时那个作用域早就不在了。
 *
 * ⚠️ 存"最近一次的"，而不是每个窗口各存一份：菜单同时只会有一份
 * （右键一次弹一个）。
 */
let petMenuHandlers: PetMenuHandlers | null = null;

/**
 * 说话类消息 -> 独立气泡窗口。
 *
 * 返回 `true` 表示这条消息**已被气泡窗口消费**，调用方就不该再往宠物窗口发了
 * （宠物窗口里那份气泡已经删掉）。
 *
 * 只转发"要显示文字"的四种消息，其余（表情、口型、状态机）仍归宠物窗口 ——
 * 那些是驱动立绘动画的，跟气泡无关。
 */
/**
 * TTS **最近一次真实合成**成功了没有（2026-10-15 加）。
 *
 * ## 🔴 为什么需要它（用户报障的直接原因）
 *
 * 用户报「宠物发不了声音」+「也回复不了信息」。查出来是**两件事叠加**：
 *
 * `
 * ① edge-tts 被微软 403 挡了（合成实际失败）
 * ② 沉浸模式**故意不显示文字** —— 它按「TTS 配置开着」判断有没有语音
 * `
 *
 * **叠加起来才是完全没反应**：文字被主动隐藏，语音又失败了。
 *
 * ⚠️ 根因是这里只知道配置（	ts.enabled: true）、**不知道合成失败了**。
 *
 * ## 怎么维护
 *
 * `
 * 收到 voice.tts_failed   -> false（并记日志）
 * 收到 voice.audio        -> true （真的产出音频了，恢复）
 * `
 *
 * ⚠️ 默认 	rue（**假定健康**）：没观测到失败时不改变原行为，
 * 否则这个改动会顺便改掉所有正常用户的体验。
 */
let ttsHealthy = true;

/**
 * 当前 TTS 播放音量（0..100），随每段语音带给渲染层。
 *
 * 2026-10-19 用户要求「可调节声音大小」。真源是 `persona.yaml` 的
 * `providers.tts.volume`（设置页「语音 → 音量」滑杆写它）。
 *
 * ⚠️ 为什么在**播放端**生效：三条合成路线的音量参数是**三套单位**
 * （edge 是 `'+10%'` 字符串、SAPI 是 0..100 整数、云端没有这个参数），
 * 在合成端调必然有一边是错的。统一乘在播放增益上，三条路线都听话。
 *
 * 缓存 2 秒：一句话可能分成多段发下来，没必要每段都去读一次 YAML；
 * 改完设置最迟 2 秒生效（保存时主动清缓存，见 `invalidateTtsVolume`）。
 */
let ttsVolumeCache: { at: number; value: number } | null = null;

/** 设置保存后调用：下一段语音立刻按新音量。 */
function invalidateTtsVolume(): void {
  ttsVolumeCache = null;
}

function currentTtsVolume(): number {
  const now = Date.now();
  if (ttsVolumeCache && now - ttsVolumeCache.at < 2000) return ttsVolumeCache.value;
  let value = 100;
  try {
    const raw = readDeepScalar(CONFIG_ROOT, ['providers', 'tts', 'volume'], true);
    value = raw === null || raw === undefined ? 100 : Number(raw);
  } catch {
    /* 读不到配置就按满音量，别让宠物突然没声 */
  }
  if (!Number.isFinite(value)) value = 100;
  value = Math.max(0, Math.min(100, Math.round(value)));
  ttsVolumeCache = { at: now, value };
  return value;
}

/**
 * 每次发言是不是"用户主动要的"，按 `say.id` 记住。
 *
 * ## 为什么需要记
 *
 * `userInitiated` 只有 `pet.say.start` 带（Python 侧在 `_generate` 里算），
 * 后面的 `say.delta` / `say.end` **都不带**。而"趴着（全屏）时要不要弹气泡"
 * 是按消息逐条判的 —— 只认 start 的话，start 过了、第一条 delta 就被判成
 * "不是用户要的"，气泡当场被 hide。
 *
 * 症状（2026-10-19 用户报的就是它）：按了截图对话，气泡闪一下就没了，
 * 字一个都没看到。
 *
 * 所以 start 时记下来，delta / end 沿用同一个 id 的值，end 之后稍后清掉。
 */
const userInitiatedBySay = new Map<string, boolean>();

/** 记一下这条发言的性质；同一个 id 只留最新的。 */
function rememberUserInitiated(id: string | undefined, userInitiated: boolean): void {
  if (!id) return;
  // 极端情况下（模型一直不 end）别让 Map 无限长
  if (userInitiatedBySay.size > 64) userInitiatedBySay.clear();
  userInitiatedBySay.set(id, userInitiated);
}

/** 这条消息属于"用户主动要的"吗（start 带值，delta/end 查表）。 */
function lookupUserInitiated(
  kind: string,
  id: string | undefined,
  explicit: boolean | undefined,
): boolean {
  if (kind === 'say.start') {
    const flag = Boolean(explicit);
    rememberUserInitiated(id, flag);
    return flag;
  }
  if (id && userInitiatedBySay.has(id)) return Boolean(userInitiatedBySay.get(id));
  return Boolean(explicit);
}

function forwardToBubble(message: { type?: string }): boolean {
  if (!bubbleWindow) return false;
  const type = message.type ?? '';
  const kind =
    type === 'pet.thinking'
      ? 'thinking'
      : type === 'pet.say.start'
        ? 'say.start'
        : type === 'pet.say.delta'
          ? 'say.delta'
          : type === 'pet.say.end'
            ? 'say.end'
            : null;
  if (!kind) return false;

  /*
   * 气泡被用户关掉时**整条不转发**（2026-10-07 用户要求）。
   *
   * ⚠️ 为什么在这里拦而不是在下面"跳过定位"那一段：
   * 那段只是不摆位置，消息**照样会发给气泡窗口** —— 于是窗口会拿旧位置
   * 显示出来（内容还是新的），等于开关没生效。
   * 要真的"不弹"，就得在**转发之前**返回。
   *
   * ⚠️ 仍然返回 `true`：这条消息**已被气泡消费**（只是丢弃），
   * 不能让它落到宠物窗口 —— 那边同一条路径会显示成旧的窗口内气泡，
   * 而那份气泡早就删掉了（见上面"宠物窗口里那份气泡已经删掉"）。
   */
  /*
   * 门控（2026-10-09 抽成纯函数 `decideBubble`，见 `bubbleDecision.ts`）。
   *
   * 三层，顺序有讲究：
   *   ① 气泡被用户关掉（bubbleVisible=false）  -> 一律不显示
   *   ② 沉浸模式且**有语音**                    -> 不显示（只听声音）
   *   ③ 沉浸模式但**没有语音**                  -> 显示（例外）
   *
   * ⚠️ ① 在 ② 前面是刻意的：`bubbleVisible` 是总开关，
   * 用户明确关掉之后连"沉浸模式的例外"也不该弹。
   *
   * 为什么要抽出去：这段原来是揉在这里的两段手写 if，而它正是沉浸模式的
   * 核心逻辑 —— **最该有测试**，可 `forwardToBubble` 在 `main()` 内部、
   * 测试碰不到。抽成纯函数后测试能直接喂参数打（见 `test-main.mjs`）。
   */
  let ttsEnabled = true;
  try {
    ttsEnabled = readTtsConfig().provider !== 'none';
  } catch {
    /* 读不到配置就当"有声"——宁可少弹文字，也别把沉浸模式弄失效 */
  }
  const decision = decideBubble({
    isTextKind: true, // 走到这里说明 kind 已经匹配上了（上面 !kind 就 return 了）
    // 模块开关叠加在用户开关之上（AND）：关掉「窗口/气泡」模块 = 气泡一律不弹
    bubbleVisible: bubbleVisible && moduleEnabled('ui.windows'),
    immersiveMode,
    reason: (message as { reason?: string }).reason,
    ttsEnabled,
    /*
     * 🔴 TTS **最近一次真实合成**的结果（2026-10-15 加）。
     *
     * ⚠️ 和 `ttsEnabled` 是两件事：那个是"配了没有"，这个是"**能不能用**"。
     *
     * 用户报「发不了声音 + 回复不了信息」时，`ttsEnabled` 是 `true`
     * （配置里开着）而合成**实际 403 失败** —— 于是沉浸模式认为"有语音"、
     * 隐藏了文字，而语音又没有，用户两头都收不到。
     *
     * 由 sidecar 的 `voice.tts_failed` 维护，见下面的 handler。
     */
    ttsHealthy,
  });
  if (!decision.show) {
    if (kind === 'say.start') startupNote(`[气泡] 不显示：${decision.why}`);
    return true;
  }
  if (immersiveMode && kind === 'say.start') {
    startupNote(`[气泡] ${decision.why} —— 本条显示文字`);
  }

  /*
   * 宠物不可见 / 趴着（别的应用全屏）时**默认**不弹气泡：那时候用户不在看桌面。
   *
   * 例外是"用户主动要的"那一句（`userInitiated`）：按了截图对话、打了字、说了话
   * 之后，回复必须给文字。2026-10-19 用户报的正是这个 ——
   * 「宠物控制状态明明是点击隐藏，可是气泡却没有出现」：他在全屏看漫画时按了
   * 截图对话，宠物趴着（compact=true）把气泡跳过了，于是只有声音没有字。
   *
   * 判定抽在 `decideBubblePlacement` 里，测试能直接喂参数打。
   */
  const pet = petWindow;
  const sayId = (message as { id?: string }).id;
  const userInitiated = lookupUserInitiated(
    kind,
    sayId,
    (message as { userInitiated?: boolean }).userInitiated,
  );
  // say.end 之后这条 id 的用途就结束了；延迟清，避免 delta 迟到时查不到。
  if (kind === 'say.end' && sayId) {
    setTimeout(() => userInitiatedBySay.delete(sayId), 10_000);
  }
  const placement = decideBubblePlacement({
    petVisible: Boolean(pet?.visible),
    compact: Boolean(pet?.isCompact),
    userInitiated,
  });
  if (!placement.show) {
    startupNote(
      `[气泡] 跳过定位：${placement.why}（visible=${String(pet?.visible)} compact=${String(
        pet?.isCompact,
      )} userInitiated=${String(userInitiated)}）`,
    );
    bubbleWindow.hide();
    return true;
  }

  // 每次显示前重新定位：宠物可能刚被拖过、或刚从探头状态回来
  const anchor = pet?.artScreenAnchor;
  if (anchor) {
    bubbleWindow.place(anchor);
    startupNote(
      `[气泡] 定位 anchor=(${Math.round(anchor.centerX)},${Math.round(anchor.topY)})`,
    );
    sendToBubble(bubbleWindow, message, kind);
    return true;
  }

  /*
   * ⚠️ 锚点还没就绪（立绘尺寸还没量到）时**不能直接显示**。
   *
   * 踩过：那时窗口还在构造初值 `x=0, y=0`，气泡会**闪在屏幕左上角**，
   * 而且要等到下一次说话才摆正 —— 用户看到的就是"气泡位置完全不对"。
   *
   * 所以等一小会儿（首帧测量通常在几百毫秒内完成），拿到锚点再摆 + 显示。
   * 上限 2 秒；超时也显示（宁可位置不完美，也不能让回复消失）。
   */
  startupNote('[气泡] 锚点未就绪，等待立绘测量完成…');
  let tries = 0;
  const waitTimer = setInterval(() => {
    tries += 1;
    const a = petWindow?.artScreenAnchor;
    if (a) {
      clearInterval(waitTimer);
      bubbleWindow?.place(a);
      startupNote(
        `[气泡] 迟到的定位 anchor=(${Math.round(a.centerX)},${Math.round(a.topY)})，等了 ${tries * 100}ms`,
      );
      if (bubbleWindow) sendToBubble(bubbleWindow, message, kind);
      return;
    }
    if (tries >= 20) {
      clearInterval(waitTimer);
      startupNote('[气泡] 等锚点超时（2s），仍显示，位置可能不准');
      if (bubbleWindow) sendToBubble(bubbleWindow, message, kind);
    }
  }, 100);
  return true;
}

/*
 * 气泡**最后一条真实消息**的缓存 —— `sendToBubble` 写入、
 * `toggleBubbleVisibility` 在「显示气泡」时重放。
 *
 * 形状对齐 `sendToBubble` 的参数：`message` 是 sidecar 的原始消息
 * （say.start / say.end 都可能，看最后发的是哪条），`kind` 原样带着。
 */
let lastBubbleMessage: { message: { type?: string }; kind: string } | null = null;

/** 把一条消息发给气泡窗口（与定位解耦，便于"等锚点"后复用） */
function sendToBubble(
  target: BubbleWindow,
  message: { type?: string },
  kind: string,
): void {
  /*
   * 记下**最后真正发出去的那条**（2026-10-14 加）。
   *
   * 为什么在发之前记：这是全项目发气泡消息的**唯一漏斗**（`forwardToBubble`
   * 的两条路都汇到这里），记在这里不会漏。`toggleBubbleVisibility` 在用户点
   * 「显示气泡」时把它原样重放 —— 否则那个开关就成了"单向门"：
   * 关了再开，什么都没有，用户报的正是这个。
   *
   * ⚠️ 只缓存**真正转发过**的：能走到这里说明气泡决策已放行
   * （`decideBubble` 的 show），被打断/错误/免打扰拦下的不会进来。
   */
  lastBubbleMessage = { message: { ...message }, kind };

  target.show();
  /*
   * ⚠️ **不要在这里开始计时**。
   *
   * 用户要求：「隐藏的时间不计入气泡持续时间内」。这一刻窗口可能还没画出来
   * （首次显示要等首帧，实测约 1.5s 的加载宽限），计时若从这里起算，
   * 那段时间就被算进"显示时长"了。
   *
   * 计时改为由 `bubble:shown`（渲染层报告"真的画出来了"）触发，见下面的 IPC。
   */
  const payload = {
    kind,
    id: (message as { id?: string }).id,
    text: (message as { text?: string }).text,
    // 存在时间（设置里可调）随消息下发 —— 渲染层改完立刻生效，不用额外通道
    lingerMs: petWindow?.bubbleLinger ?? readSettings().bubbleLingerMs,
    // 段间距同款套路（2026-10-17）：随消息下发，设置里改完立刻生效
    paraGap: readSettings().bubbleParaGap,
  };
  /*
   * **首次**气泡延后一点再显示内容 —— **已删除**（2026-10-04）。
   *
   * 曾用来验证"第一条气泡位置不对"是否因为窗口没就绪。实测证明不是那个原因
   * （真根因是定位用了占位尺寸，见 `BubbleWindow.show`），所以延迟撤掉了。
   */
  target.send('bubble:message', payload);
}
/** 防抖：宠物窗口连续移动时不要每一帧都重排对话条 */
let chatSyncTimer: NodeJS.Timeout | null = null;
let socket: PetSocket | null = null;

let quitting = false;

/**
 * "退出全部实例"的协调器。
 *
 * 放在这里（变量声明区）而不是函数定义之后：`quit()` 会用到它，声明在前
 * 读起来才顺。回调直接指向 `quit`（函数声明会提升，这里安全）。
 */
const quitCoordinator = new QuitCoordinator(() => void quit(), log);
let paused = false;
let pauseTimer: NodeJS.Timeout | null = null;
let activity = 0.5;
/**
 * 当前状态键（quiet / bored / curious / monologue），2026-10-23 加。
 *
 * ⚠️ 真源在 Python 的状态机（`pet/state.py`），这里只是**镜像**，
 * 由 sidecar 的 `pet.emotion` / `pet.state` 事件更新。
 * 菜单显示它，而不是拿 `activity` 数值去猜 —— 因为状态会自己变
 * （精力低、30 分钟没聊天、随机事件），数值猜出来可能是过期的。
 */
let petState = '';
let statusText = '启动中…';

// --------------------------------------------------------------------------- //
// 日志：写文件，避免用户遇到问题时无据可查
// --------------------------------------------------------------------------- //

// 只在第一次失败时记一条（避免把 startup.log 刷满）
let logFailReported = false;

/*
 * 真正在用的日志文件 —— 第一次写成功时定下来，之后一直用它。
 *
 * 为什么要缓存：`LOG_CANDIDATES` 有三个候选，每次写日志都从头试一遍
 * 会白白 `mkdirSync` + 抛异常（`%APPDATA%` 那个必然失败），又慢又吵。
 */
let activeLogFile: string | null = null;

/**
 * 当前日志文件的**已写字节数**（含历史内容）。
 *
 * 为什么记账而不是每次 `statSync`：`log()` 很热（消息收发都打日志），
 * 每次写前 stat 一次是白白的 syscall。记账只需要在"首次选定"时读一次
 * 真实大小，之后每次写加上本次行字节即可。轮转后归零。
 */
let activeLogBytes = 0;

/** 依次尝试候选落点，返回第一个**能写**的（并顺手轮转）。都写不了返回 null。 */
function pickLogFile(): string | null {
  for (const candidate of LOG_CANDIDATES) {
    try {
      fs.mkdirSync(path.dirname(candidate), { recursive: true });
      // ⚠️ 用 `appendFileSync` 试写一个空串：它既能验证可写，
      //    又不会留下垃圾内容（`open(..., 'a')` 不写东西就不改文件）。
      fs.appendFileSync(candidate, '', 'utf-8');
      // 首次选定文件时兜一道：把**上次会话**遗留的超大文件收掉
      // （写入时的轮转负责本次会话，见 `log()`）。
      rotateLogIfNeeded(candidate);
      activeLogBytes = logFileBytes(candidate);
      return candidate;
    } catch {
      /* 试下一个候选 */
    }
  }
  return null;
}

function log(message: string): void {
  const line = `${new Date().toISOString()} ${message}`;
  if (DEV_MODE) console.log(line);

  if (activeLogFile === null) {
    activeLogFile = pickLogFile() ?? '';
    if (activeLogFile) {
      startupNote(`主日志落点：${activeLogFile}`);
    } else if (!logFailReported) {
      logFailReported = true;
      startupNote(
        `⚠️ 主日志三处落点都写不了：${LOG_CANDIDATES.join(' / ')} —— 主进程日志将只进 stderr`,
      );
    }
  }
  if (!activeLogFile) return; // 全都写不了：已经报过一次，不再重复

  const file = activeLogFile;
  const bytes = logLineBytes(line);
  /*
   * 写之前先判断会不会写超（见 `logRotation.ts` 的说明）—— 必须在写之前，
   * 否则这一行落进旧文件、轮转把它整份挪走，行就"丢"在 .1 里了。
   * 轮转失败（比如文件被别的工具占用）就照常追加，这次不轮转。
   */
  if (activeLogBytes + bytes > LOG_MAX_BYTES) {
    if (rotateLogNow(file)) activeLogBytes = 0;
  }
  try {
    fs.appendFileSync(file, line + '\n', 'utf-8');
    activeLogBytes += bytes;
  } catch (error) {
    /*
     * ⚠️ **失败要留痕**（2026-10-09 改）。
     *
     * 原来这里是空的 `catch {}` —— 于是"日志写不进去"这件事**完全没有痕迹**：
     * `userData/logs/` 一直是空的，而我排查别的问题时反复看不到主进程日志，
     * 只能靠猜（查了很久才发现是写入失败，不是"没打日志"）。
     *
     * 现在把原因写进 `startupNote()`（它落 `dist/startup.log`，
     * 那个目录确定可写 —— 它一直在正常工作）。只在第一次失败时写，避免刷屏。
     *
     * ⚠️ 写失败后**不清掉 `activeLogFile`** —— 那会导致下一条日志重新走
     * `pickLogFile()`（又要建目录、试三个候选）。磁盘满了之类的临时故障
     * 恢复后，同一个路径会自己好；真换地方等下**下次启动**再说。
     */
    if (!logFailReported) {
      logFailReported = true;
      startupNote(
        `⚠️ 主日志写入失败 ${file}：${error instanceof Error ? error.message : String(error)}`,
      );
    }
  }
}

/**
 * 当前生效的日志文件路径 —— 给打开日志那类入口用。
 *
 * ⚠️ 别再用老常量 LOG_FILE（已删）：真实落点是 log() 第一次写成功时
 * **运行时定下来**的（pet-data/main.log / dist/main.log / userData 三选一，
 * 见 LOG_CANDIDATES）。硬编码一个路径会出现菜单打开的日志是空的。
 */
function currentLogFile(): string {
  if (activeLogFile) return activeLogFile;
  // 还没写过日志就叫这个（比如用户一进来就点打开日志）——先定一个再说
  activeLogFile = pickLogFile() ?? LOG_CANDIDATES[0];
  return activeLogFile;
}
// --------------------------------------------------------------------------- //
// 启动：单实例检查（"禁止多开"）
//
// **不用 `app.requestSingleInstanceLock()`** —— 实测它在本环境恒返回 false
// （空系统、--no-sandbox、指定 userData 都是 false），根本不在工作。
// 当年那行 `if (!lock) app.quit()` 会静默退出，根因就在这里；
// 而且 `app.quit()` 在沙箱下也没能真正终止进程（启动记录里能看到
// "启动失败"之后窗口照样被创建）。详见 singleInstance.ts 的实测数据。
//
// 现在用自己实现的锁文件 + PID 存活检查：能正确识别陈旧锁并清理
// （避免"崩溃后再也起不来"），也能把"第二次双击"变成"把窗口叫出来"。
// --------------------------------------------------------------------------- //

const singleInstanceGuard = new SingleInstanceGuard(
  () => {
    // 只会在窗口建好之后被触发（start() 在 main() 里、窗口之后调用）
    petWindow?.show();
  },
  (message: string) => log(`[单实例] ${message}`),
);

/*
 * 锁文件与请求文件放**项目内** `pet-data/instance-state/`。
 *
 * ## ⚠️ 为什么不放 `userData`（原来就是那么写的，实测不可写）
 *
 * 原注释的理由是"userData 是 Electron 保证可写的地方"。**实测不成立** ——
 * 在这台机器上，整个 `%APPDATA%\@pet\desktop`（= userData）在 Electron 主进程里
 * **完全不可写**：
 *
 *     ① 写 instance.lock.json    ❌ EPERM: operation not permitted, open
 *     ② 换 openSync('w')         ❌ EPERM
 *     ③ mkdir 子目录             ❌ EPERM      <- 连建目录都不行
 *     ④ probe.txt / probe.json   ❌ EPERM      <- 换文件名也不行
 *
 * 而**同一个目录**，PowerShell 手工写是**成功**的 —— 所以不是目录权限，
 * 是"Electron 主进程在这个路径上被拦"。改到项目内立刻就好：
 *
 *     ⑥ setStateDir(项目内) -> claim() 返回「已取得实例锁，本进程是唯一实例」✅
 *
 * （本项目其它地方早就为同一个坑做过同样的退让：`credentials.ts`、
 *   `settingsStore` 的 fallback、SAPI/fullscreen/PTT 各自的临时目录 —— 都挪进了
 *   `pet-data/`。这里以前漏了。）
 *
 * ## 🔴 这个 bug 的实际危害（不只是"少个锁"）
 *
 * 写不了锁 -> `claim()` **降级放行** -> **允许多开** -> 两个实例各跑一个
 * `probe-key` 监视器、各写一份 `startup.log`。
 *
 * 后果是排查时**看到的证据互相矛盾**：用户按键的那个实例日志里可能有记录，
 * 而我在看另一个实例的日志 —— 于是"按下没用/日志里没有按下"这种结论
 * 完全不可信。**这个 bug 直接浪费了一整轮排查。**
 */
const INSTANCE_STATE_DIR = path.join(DATA_DIR, 'instance-state');
try {
  fs.mkdirSync(INSTANCE_STATE_DIR, { recursive: true });
} catch (error) {
  // 建不出来也不能让启动失败 —— 退回原来的行为（降级放行），但**留下日志**
  log(
    `[单实例] 建状态目录失败（${INSTANCE_STATE_DIR}）：${(error as Error).message} —— ` +
      '将退回 userData（那边可能也不可写）',
  );
}
setStateDir(INSTANCE_STATE_DIR);
setQuitStateDir(INSTANCE_STATE_DIR);

const instanceClaim = singleInstanceGuard.claim();
startupNote(`单实例检查：${instanceClaim.detail}`);

if (!instanceClaim.isPrimary) {
  // 有活着的实例 -> 把"我想看宠物"这个意图转达给它，然后本进程退出。
  // 这不是错误，是用户双击快捷方式的正常结果。
  singleInstanceGuard.requestFocus();
  log(`[单实例] ${instanceClaim.detail}，已请求聚焦，本进程退出`);
  startupNote('已有实例在运行，已请求把它的窗口叫到最前，本进程退出');

  // ⚠️ 关键：这里**不能只调 app.quit()**。
  // 当年就是"app.quit() 之后 main() 继续跑、窗口和托盘照样建出来"，
  // 于是留下一个看不见的僵尸实例（启动记录里能看到这个现象）。
  // 所以：先给已有实例几百毫秒读走聚焦请求，然后**强制退出**。
  setTimeout(() => {
    try {
      app.exit(0);
    } catch {
      process.exit(0);
    }
  }, 600);
} else {
  startupNote(`单实例检查通过（${instanceClaim.detail}），继续启动`);
  // `second-instance` 依赖 Electron 那个已失效的 API，所以不用它；
  // 聚焦请求的轮询在 main() 里窗口建好之后启动（见 singleInstanceGuard.start()）。
}

// main() 自身是 async，抛出的异常会变成 unhandledRejection（上面已挂监听）。
// 这里再包一层，把失败点写得更明确。
main().catch((error) => {
  writeStartupFailure('main()', error);
  log(`启动流程失败：${error instanceof Error ? error.message : String(error)}`);
});

async function main(): Promise<void> {
  /*
   * ---------------------------------------------------------------------
   * `pet://` 协议：只读地服务本地文件，**并给 HTML 里的资源 URL 注入版本号**。
   *
   * ## 为什么需要它（2026-10-09，我在这上面浪费了好几轮）
   *
   * 设置页的加载早就带了缓存穿透：
   *
   *     loadURL('file://' + page + '?v=' + stamp)      // stamp = 页面 mtime
   *
   * 但版本号**只在页面 URL 上**，而 HTML 里的引用是相对路径、没有版本号：
   *
   *     <script type="module" src="settings.js"></script>
   *     <link rel="stylesheet" href="settings.css" />
   *
   * 于是 HTML 每次都是新的、**`settings.js` 一直用 Chromium 的缓存**。
   * 表现是：我改了 JS、重新构建、重启应用，界面行为**还是旧的**。
   * 有一轮"下拉框的值对了、但状态行永远显示模板默认文案「未启用」"就是这个 ——
   * 那次改的恰好是 HTML，而 JS 是几十分钟前的。
   *
   * ## 怎么解决
   *
   * 服务 `.html` 时，**把里面引用的本地资源 URL 都补上 `?v=<该文件 mtime>`**。
   * 源码一动 URL 就变，缓存自然失效 —— 不用手改 HTML，也不会漏。
   *
   * ## 只读 + 路径限制
   *
   * 只允许读 `apps/` 与 `packages/` 下的文件，且规范化后必须仍在允许的根里，
   * 防止 `pet://` 变成任意文件读取。
   * ---------------------------------------------------------------------
   */
  const PET_SCHEME = 'pet';
  /*
   * ⚠️ **只有一个根**：`PROJECT_ROOT`。
   *
   * 原来我在这里放了 `[<root>/apps, <root>/packages]` 两个根，
   * 然后在处理器里 `path.resolve(root, rel)` —— 但 `rel` 本身**已经含 `apps/`**
   * （`settingsWindow` 用 `path.relative(PROJECT_ROOT, page)` 算的），
   * 于是拼成 `F:\...\apps\apps\desktop\...`，**"apps" 两次** -> 404。
   *
   * 直接以 `PROJECT_ROOT` 为根 resolve 就对上了。
   */
  /*
   * pet:// 的**相对基准**必须和 `assets.ts: rendererUrl()` 一致：
   *
   *   开发态  __dirname = <repo>/apps/desktop/dist   -> 往上三级 = 仓库根
   *   打包态  __dirname = <安装目录>/resources/app.asar/dist -> 往上三级 = 安装根
   *
   * 于是打包后 URL 是 `pet://app/resources/app.asar/src/renderer/index.html`。
   * 原来这里用 APP_ROOT（打包态 = resources）去 resolve，会拼成
   * `resources\resources\app.asar\…`，页面直接 404「not found」——
   * 2026-10-19 用户报"打包出来啥也没有"，真因就是这个（窗口体里只有 not found）。
   */
  const PET_ROOT = app.isPackaged ? path.dirname(APP_ROOT) : APP_ROOT;
  const PET_DENY = [path.join(APP_ROOT, 'node_modules')];

  // ⚠️ 必须在 `app.whenReady()` **之前**声明为特权协议，
  //    之后注册就晚了（Chromium 已经不认这个 scheme）。
  protocol.registerSchemesAsPrivileged([
    {
      scheme: PET_SCHEME,
      privileges: { standard: true, secure: true, supportFetchAPI: true, stream: true },
    },
  ]);

  /** 文件的 mtime（作为版本号）；读不到就退回当前时间 */
  function stampOf(file: string): string {
    try {
      return String(Math.floor(fs.statSync(file).mtimeMs));
    } catch {
      return String(Date.now());
    }
  }

  /**
   * 给 HTML 里引用的**本地**资源补上 `?v=<mtime>`。
   *
   * 只碰 `href="..."` / `src="..."`，且只处理：
   *   · 不以 `/`、`http:`、`https:`、`data:`、`#`、`?` 开头的相对路径
   *   · 还没有 `?v=` 的
   */
  function injectStamps(html: string, dir: string): string {
    return html.replace(/\b(href|src)="([^"]+)"/g, (whole: string, attr: string, url: string) => {
      if (/^(?:[a-z]+:|\/\/|\/|#|\?)/i.test(url)) return whole;
      if (url.includes('?v=')) return whole;
      const target = path.join(dir, url.split(/[?#]/)[0]);
      return `${attr}="${url}?v=${stampOf(target)}"`;
    });
  }

  const PET_MIME: Record<string, string> = {
    '.html': 'text/html; charset=utf-8',
    '.js': 'text/javascript; charset=utf-8',
    '.mjs': 'text/javascript; charset=utf-8',
    '.css': 'text/css; charset=utf-8',
    '.json': 'application/json; charset=utf-8',
    '.svg': 'image/svg+xml',
    '.png': 'image/png',
    '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.gif': 'image/gif',
    '.webp': 'image/webp',
    '.woff2': 'font/woff2',
  };

  async function servePet(request: Request): Promise<Response> {
    let rel: string;
    try {
      rel = decodeURIComponent(new URL(request.url).pathname).replace(/^\/+/, '');
    } catch {
      return new Response('bad url', { status: 400 });
    }
    const candidate = path.resolve(PET_ROOT, rel);
    // ① 规范化后必须仍在 PET_ROOT 里 —— 防 `../` 穿越
    if (!candidate.startsWith(PET_ROOT + path.sep)) {
      return new Response('forbidden', { status: 403 });
    }
    // ② 不读 node_modules（体积大、也没必要）
    if (PET_DENY.some((d) => candidate.startsWith(d + path.sep))) {
      return new Response('forbidden', { status: 403 });
    }
    // ③ 必须是**普通文件**（不是目录、不是符号链接指向的外部）
    let file = '';
    try {
      if (fs.statSync(candidate).isFile()) file = candidate;
    } catch {
      /* 不存在 */
    }
    if (!file) return new Response('not found', { status: 404 });

    try {
      const ext = path.extname(file).toLowerCase();
      if (ext === '.html') {
        const html = await fs.promises.readFile(file, 'utf-8');
        return new Response(injectStamps(html, path.dirname(file)), {
          headers: { 'content-type': PET_MIME['.html'] },
        });
      }
      const buf = await fs.promises.readFile(file);
      return new Response(buf, {
        headers: { 'content-type': PET_MIME[ext] || 'application/octet-stream' },
      });
    } catch (error) {
      return new Response(`read failed: ${(error as Error).message}`, { status: 500 });
    }
  }

  startupNote('main() 开始，等待 app.whenReady()');
  await app.whenReady();
  startupNote('app.whenReady() 已就绪');
  // 打包后：可写目录 + 默认配置复制（开发态只建目录，什么都不复制）
  ensureRuntimeDirs();
  // 离线版的模型复制放后台，别让 1.5GB 挡住启动
  void ensureBundledModels();

  // 见上面 `pet://` 的说明：服务本地文件，并给 HTML 里的资源注入版本号
  protocol.handle(PET_SCHEME, servePet);
  app.dock?.hide?.(); // macOS：不在 Dock 里占位

  petWindow = new PetWindow(DIST_DIR, DEV_MODE);
  // 应用上次保存的偏好（目前是"边缘探头斜着出去多少像素"）。
  // 必须在窗口创建后立刻设置：拖到边缘时要用到它。
  {
    setSettingsDir(app.getPath('userData'));
    // 保底落点：入口文件旁边（绝对路径）。
    // 受限环境里 AppData 与 %TEMP% 会全部不可写，那时这里是唯一能落盘的地方；
    // 落不下盘的表现只是"设置重启就没了"，不说出来根本查不到。
    setSettingsFallbackDir(path.join(DATA_DIR));
    /*
     * 凭据也用**同一个兜底目录**。
     *
     * 实测踩到：受限环境（或杀软拦截）里写 `~/.desktop-pet/credentials.json` 会
     * `EPERM`，设置页点保存直接失败 —— 而"密钥存不下来"对用户是致命的
     * （看起来像"接口连不上"）。所以和设置文件走同一套兜底。
     */
    setCredentialsFallbackDir(path.join(DATA_DIR));
    /*
     * 把**实际存在的**凭据路径告诉 Python（写进 env，sidecar 拉起时继承）。
     *
     * ⚠️ 必须在 `startSidecarAndConnect()` **之前**：环境变量是进程启动时快照的，
     * 之后再改 `process.env` 不会传给已经拉起的 Python。
     *
     * 为什么需要：用户目录不一定可写（实测受限环境写 `~/.desktop-pet/` 会 EPERM，
     * 界面于是退到项目旁的 `pet-data/`）。若 Python 只认用户目录，就会出现
     * "界面显示已保存、大脑却报没密钥"这种最难查的不一致。
     */
    process.env.PET_CREDENTIALS_FILE = activeCredentialsPath();
    const stored = readSettings();
    petWindow.setPeekOffset(stored.peekOffsetPx);
    petWindow.setEdgeSnap(stored.edgeSnapPx);
    // 开关和活泼度也一起恢复 —— 以前只存在内存里，重启就回默认，
    // 用户会以为"设置没生效"。
    // 宠物大小不用在这里恢复：petWindow.setPetScale 一调就会重排窗口，
    // 而构造时窗口已经是基准尺寸，等渲染层就绪后再调更稳（见 ready-to-show）。
    petWindow.setEdgePeekEnabled(stored.edgePeekEnabled);
    // "允许看屏幕"的用户意愿值：先记到内存真源，等 sidecar 就绪后再下发给它
    // （现在发没人收 —— WebSocket 还没连上）。下发处见 ready 消息处理。
    visionEnabled = stored.visionEnabled;
    // 对话框是否显示的用户意愿（同上，重启要恢复）
    chatVisible = stored.chatVisible;
    // 气泡显隐开关（用户 2026-10-07 要求）——同样必须活过重启
    bubbleVisible = stored.bubbleVisible;
    // 沉浸模式（同上：这是"临时状态"，重启后恢复成用户上次的样子）
    immersiveMode = stored.immersiveMode;
    /*
     * 「语音播报」开关（2026-10-14）。
     *
     * ⚠️ 与上面那些不一样：它的真源是 **persona.yaml**（`providers.tts.enabled`），
     * 不是 settings.json —— 因为 sidecar 每次出声前都现读这个值
     * （见 `voice.py::_on_pet_say_end_speak`）。两边各存一份就会漂移，
     * 所以这里只从 yaml 读一次给菜单用，**不写 settings.json**。
     */
    try {
      ttsSpeechEnabled = Boolean(
        readDeepScalar(CONFIG_ROOT, ['providers', 'tts', 'enabled'], true),
      );
    } catch {
      /* 读不到就当"开"—— 宁可多出声，也别一升级就哑巴 */
    }
    /*
     * 联网搜索模式（2026-10-16，三选一）—— 同样从 persona.yaml 读初值，
     * 等 sidecar ready 后再按回报校准（见 `webSearchModeFrom`）。
     */
    try {
      const storedMode = String(
        readDeepScalar(CONFIG_ROOT, ['tools', 'web_search_mode'], true) ?? 'off',
      ).trim();
      if ((WEB_SEARCH_MODES as readonly string[]).includes(storedMode)) {
        webSearchMode = storedMode as WebSearchMode;
      }
    } catch {
      /* 读不到就保持 off —— 隐私默认的保守方向 */
    }
    // 界面主题（用户 2026-10-14 要求加深色）—— 同样要活过重启
    theme = stored.theme;
  }
  const window = petWindow.create();
  startupNote('浏览器窗口已创建');

  // 对话条是**独立窗口**（见 chatWindow.ts 的说明：挤在一个窗口里
  // 既躲不开屏幕边缘、又会被 45° 旋转带跑）。
  chatWindow = new ChatWindow(
    {
      distDir: DIST_DIR,
      devMode: DEV_MODE,
      /*
       * 用户拖动对话条后落盘（2026-10-12）。
       *
       * ⚠️ 走**构造参数**已经够了：窗口的 `moved` 事件在创建之后才可能发生，
       * 那时 this.options 早就在了。所以不需要"构造完再挂回调"那种两步写法
       * （那种写法还有个隐患：万一将来有人在构造和挂回调之间触发了 moved，
       * 拖动就静默不落盘）。
       */
      onPositionChanged: (position) => {
        const result = writeSettings({ chatX: position.x, chatY: position.y });
        if (!result.ok) {
          log(`对话条位置落盘失败：${result.error ?? '未知原因'}（路径 ${result.path}）`);
        }
      },
    },
    (line) => {
      log(line);
      startupNote(line);
    },
  );
  /*
   * 恢复对话条上次被拖到的位置（2026-10-12 用户要求可拖拽）。
   *
   * ⚠️ 必须在 `create()` **之前**恢复 —— `create()` 里会走一次定位，
   * 之后再恢复会让窗口先出现在右下角再"跳"一下。
   */
  {
    const chatStored = readSettings();
    /*
     * ⚠️ **不能用 `Number.isFinite(Number(x))` 判**。
     *
     * `Number(null)` 是 **0**、`Number.isFinite(0)` 是 **true** ——
     * 于是 `chatX: null`（复位后写下的值）会被当成"坐标 0"，
     * 对话条跑到屏幕左上角去。而 `null` 的语义是"**清空**"。
     *
     * 所以显式判 `typeof === 'number'`，只认真正的数字。
     */
    const { chatX, chatY } = chatStored;
    if (typeof chatX === 'number' && typeof chatY === 'number') {
      chatWindow.restorePosition({ x: chatX, y: chatY });
    }
  }
  chatWindow.create();
  /*
   * 文字弹窗：**独立窗口**，浮在宠物头顶上方。
   *
   * 用户要求「可以加高宠物窗口」给气泡腾空间，但实测加高**不增加头顶以上的
   * 空间**（画布贴窗口顶，多出来的高度只能落在下方）。所以气泡必须独立成窗口：
   * 它的位置只受屏幕工作区约束，长回复也能完整显示在宠物头顶之上。
   */
  bubbleWindow = new BubbleWindow({ distDir: DIST_DIR, devMode: DEV_MODE }, (line) => {
    log(line);
    startupNote(line);
  });
  bubbleWindow.create();
  // 启动即注入气泡摆放的三个可调参数（设置 → 桌宠，落盘在 settings.json）
  {
    const st = readSettings();
    bubbleWindow.setPlacementTuning({
      gapAbove: st.bubbleGapAbovePx,
      gapSide: st.bubbleGapSidePx,
      coreShrink: st.bubbleCoreShrinkPct,
    });
  }
  /*
   * **状态条窗口**（2026-10-11 用户要求）—— 贴屏幕左/右边缘的竖条。
   *
   * 显示「颜色灯 + 竖排文字」，联网搜索时灯变黄、文字变「联网搜索中...」。
   *
   * ⚠️ **它和语音提示是两回事**：语音的「在听你说…」/「识别中…」仍在
   * 宠物窗口的 `#toast` 上（用户明确要求「语音的还是放在宠物身上执行」）。
   * 所以两块状态的归属不要混。
   *
   * ⚠️ 位置只存 `edge` + `y`（不存 x，见 `settingsStore` 的说明）——
   * 换分辨率/换显示器后靠这两个值一定能贴回可见位置。
   */
  statusWindow = new StatusWindow({
    distDir: DIST_DIR,
    onDiagnostic: (line) => {
      log(line);
      startupNote(line);
    },
    // 拖动结束 -> 落盘。只有用户真拖过才会走到这里。
    onPlacementChanged: (placement) => {
      writeSettings({ statusEdge: placement.edge, statusY: placement.y });
    },
  });
  // 先恢复上次的落位，再创建 —— 否则会先出现在默认位置再"跳"一下
  //
  // ⚠️ 这里**自己读一次设置**，不使用别处的局部变量：
  // 那段 `const stored = readSettings()` 在另一个作用域里（实测拿不到）。
  // 读设置是纯文件读，代价可忽略，比跨作用域传值稳妥。
  const statusStored = readSettings();
  statusWindow.restore(
    statusStored.statusEdge === 'left' || statusStored.statusEdge === 'right'
      ? { edge: statusStored.statusEdge, y: statusStored.statusY }
      : null,
  );
  // 用宠物位置选屏幕：多显示器下状态条该跟宠物同屏（与气泡同一规则）
  statusWindow.create(petWindow ? { x: 0, y: 0 } : undefined);
  /*
   * 按用户上次的选择决定显不显示。
   *
   * ⚠️ 默认**显示** —— 它现在只有"联网搜索中"这一个用途，藏起来的话
   * 用户根本不知道有没有这个功能。等第 5 步（联网）做完，
   * `statusVisible` 这个开关也可以在菜单里关掉。
   */
  statusWindow.setVisible(statusStored.statusVisible !== false);

  /*
   * **右键菜单窗口**（2026-10-14 用户要求按设计稿重做）。
   *
   * ⚠️ 这里**只建对象、不建窗口** —— 真正的 BrowserWindow 在第一次右键
   * 时才创建（见 `PetMenuWindow.ensureWindow` 的说明）：启动时白白加载
   * 一个菜单页面既拖慢启动，也占一个常驻的透明窗。
   *
   * 为什么现在就把对象建好：`onAction` 回调要靠它把点击接回主进程，
   * 而这个回调必须在窗口建出来**之前**就位。
   */
  petMenuWindow = new PetMenuWindow({
    distDir: DIST_DIR,
    onDiagnostic: (line) => {
      log(line);
      startupNote(line);
    },
  });
  /*
   * 启动时补刷一次主题。
   *
   * 不是必需（上面的 `web-contents-created` 已经覆盖了每个窗口的 dom-ready），
   * 但如果某个窗口在**注册钩子之前**就已经 dom-ready（理论上不会，钩子在
   * 模块加载时就注册了），这一次就是兜底。注入是幂等的，多刷一次没有代价。
   */
  applyThemeToAllWindows();
  /*
   * **宠物一动，气泡跟着走**（2026-10-04）。
   *
   * 原来气泡只在"要说话"时定位一次（`showBubble` 里的 `place`），于是**显示期间
   * 宠物一拖、一探头，气泡就留在原地**。探针实测（气泡坐标完全没变）：
   *
   *     常态      立绘中心x 1394 | 气泡 1344,425
   *     探头(右)  立绘中心x 1509 | 气泡 1344,425
   *     探头(左)  立绘中心x   14 | 气泡 1344,425
   *
   * 用户报的「将探头时的气泡位置改低一些，这样语音聊天的时候文字显示正常」
   * 就落在这里 —— 语音聊天时气泡持续显示，而宠物会动。
   *
   * 节流与"可见才做"的判断都在 `followAnchor` 里（见那里说明）。
   */
  if (petWindow) petWindow.onArtRect = (anchor) => bubbleWindow?.followAnchor(anchor);
  /*
   * **气泡"真的画出来了"** -> 从这一刻开始计时。
   *
   * 用户要求：「隐藏的时间不计入气泡持续时间内」。渲染层在首帧画完（窗口可见）后
   * 发这条，主进程据此起 `lingerMs` 的计时器。每次新内容都会重发（重置计时）。
   */
  ipcMain.on('bubble:shown', () => {
    bubbleWindow?.scheduleHide(petWindow?.bubbleLinger ?? readSettings().bubbleLingerMs);
  });

  /*
   * 状态条的输入（2026-10-11）。
   *
   * ⚠️ 通道名必须与 `src/preload/status.ts` 里那张表**逐字一致** ——
   * 不一致的表现是**静默失效**（拖动没反应、状态不更新），
   * 而这个项目已经在"通道列表与 preload 白名单漂移"上踩过坑。
   */
  ipcMain.on('status:drag-move', (_event, payload: { y?: number }) => {
    statusWindow?.handleDragMove(payload ?? {});
  });
  ipcMain.on('status:drag-end', () => {
    statusWindow?.handleDragEnd();
  });
  /*
   * 渲染层挂好监听后会主动要一次当前状态。
   *
   * ⚠️ 必须有这条：状态条的初始文字在 HTML 里是「空闲」，而真实状态可能是
   * 「联网搜索中...」。如果只在"状态变化时"推，那么**窗口打开时正在搜索**
   * 就会一直显示「空闲」—— 直到下一次状态变化才纠正。
   */
  ipcMain.on('status:hello', () => {
    statusWindow?.pushState();
  });
  // 到点自动隐藏：清掉气泡窗口的可见状态（计时器已在 hide() 里清掉）
  if (bubbleWindow) {
    bubbleWindow.onAutoHide = () => bubbleWindow?.hide();
  }
  // 渲染层上报气泡实测尺寸 -> 窗口跟着伸缩（否则短文字时窗口仍是固定 520 宽，
  // 被钳进工作区后气泡会飘到离宠物很远的地方）
  ipcMain.on('bubble:size', (_event, size: { width?: unknown; height?: unknown; windowWidth?: unknown }) => {
    bubbleWindow?.setBubbleSize({
      width: Number(size?.width),
      height: Number(size?.height),
      windowWidth: Number(size?.windowWidth),
    });
  });
  /*
   * 恢复"对话框是否显示"这个**用户意愿**。
   *
   * 用户报：「隐藏了，但是关闭应用重新打开又出来了」。
   * 原因是这个开关只存在内存里（`chatVisible`），重启回默认 true。
   *
   * 放在 `ready-to-show` 之后再应用，而不是 create 之前：
   * `ChatWindow.create()` 内部无条件 `showInactive()`（那是刻意的，
   * 见 chatWindow.ts 的注释），所以只能在其后覆盖。代价是启动瞬间可能
   * 闪一帧，换取"真源唯一"——比在 create 里加分支要稳。
   */
  chatWindow.browserWindow?.once('ready-to-show', () => {
    if (!chatVisible) chatWindow?.setUserHidden(true);
  });
  // 语音等"宠物界面上的按钮"被删掉之后，输入框成了唯一入口，
  // 所以消息转发必须同时喂给对话条窗口。
  registerChatIpc();
  // 对话条**固定在屏幕右下角**（用户要求：「把这个对话框放在最右下角」）。
  //
  // 为什么不跟着宠物跑：原来它按宠物位置算"待在宠物下面"，结果宠物一进探头、
  // 一被拖拽，对话条就跟着满屏跑（实测飘到过 617,382 —— 屏幕中间偏上），
  // 既难找又打断输入。固定锚点之后，用户永远知道去哪打字。
  //
  // ⚠️ 诊断走 `startupNote` 而不是 `log()`：`log()` 写的是
  // `userData/logs/pet.log`，而本环境下 AppData 不可写、异常被 catch 吞掉，
  // 诊断会**全部丢失**（实测排查时被这个坑了很久）。`startupNote` 写
  // `dist/startup.log`，那个目录一直可写。
  const syncChat = (): void => {
    if (!petWindow || !chatWindow) {
      startupNote(`[chat] 跳过定位：pet=${Boolean(petWindow)} chat=${Boolean(chatWindow)}`);
      return;
    }
    chatWindow.anchorToCorner('bottom-right');
  };
  // 宠物进出探头时不再移动对话条，但**宽度**仍然跟着宠物缩放走 ——
  // 这样调大宠物时输入框不会显得过窄。
  petWindow.onLayoutChanged = (form) => {
    startupNote(`[chat] 布局变化 form=${form}，对话条保持右下角锚定`);
    syncChat();
  };
  chatWindow.setPeeking(false);
  window.on('resize', syncChat);
  syncChat();
  // `pet:quit-request` 是 petWindow 自己 emit 的**自定义事件**（不是 Electron 的
  // 标准窗口事件），TS 的 BrowserWindow 类型只列了标准事件名，所以这里显式转换。
  // 不加转换会报 "not assignable to parameter of type 'will-resize'"。
  (window as unknown as NodeJS.EventEmitter).on('pet:quit-request', () => void quit());

  // 窗口内部的诊断（渲染层自检、控制台报错、进程崩溃）接到日志里。
  // 透明窗的失败从外面看永远是"什么都看不见"，所以必须让程序自己说出来。
  //
  // ⚠️ 同时写进 **startup.log**（startupNote）：正常日志走 userData/logs，
  // 而受限环境下那里可能不可写（实测过），于是"诊断信息一个字都留不下来"。
  // startup.log 在项目目录里，是这套环境里**保证可写**的落点。
  petWindow.onDiagnostic = (line: string) => {
    log(line);
    startupNote(line);
  };

  // 现在窗口已经有了，才启动聚焦请求的轮询 ——
  // 提前启动的话，回调里的 petWindow?.show() 会作用在 null 上（静默无效）。
  singleInstanceGuard.start();

  // 全屏检测：别的应用全屏时，把宠物升到能盖住全屏的层级并"趴"到右下角。
  //
  // 为什么必须动态切换层级（而不是干脆一直用 screen-saver）：
  // `screen-saver` 层级**会连任务栏一起盖住**，用户就点不到开始菜单和托盘了。
  // 所以只在确实检测到全屏时才升上去。
  //
  // 检测本身要起 PowerShell（实测约 440ms/次），因此是低频轮询；失败一律
  // 当作"不是全屏"（见 FullscreenWatcher 的说明）。
  if (process.env.PET_FULLSCREEN_WATCH !== '0') {
    // 把探测脚本的落点设成 userData —— %TEMP% 在本环境不可写（实测 EPERM）。
    // 必须在第一次检测之前设好，否则会退化成有转义问题的 -Command 路径。
    setFullscreenProbeDir(app.getPath('userData'));
    fullscreenWatcher = new FullscreenWatcher({
      // 检测是静默的 —— 失败只会表现为"宠物永远不探头"，所以必须接日志。
      onDiagnostic: (line) => {
        log(line);
        startupNote(line);
      },
      /*
     * 去抖（2026-10-20）：探测单次读数会在全屏/非全屏之间闪一下
     * （真机 06:47 那次 4 秒里闪了两轮，每次都把探头姿态拉直了）。
     * 连续 2 次一致才认，见 FullscreenWatcherOptions.confirmSamples。
     */
    confirmSamples: 2,
    onChange: (info, fullscreen) => {
        startupNote(
          fullscreen
            ? `检测到全屏应用，宠物进入紧凑模式：${info.title || '(无标题)'}`
            : '已退出全屏，宠物恢复常规形态',
        );
        petWindow?.setCompact(fullscreen, info.title || '检测到全屏应用');
      },
    });
    fullscreenWatcher.start();
  }

  // 窗口真的把内容画出来了 —— 删掉失败标记。
  //
  // 用 `ready-to-show` 而不是创建完就删：创建成功不代表显示成功，而 GPU /
  // 渲染进程的问题恰恰是在"要画"的那一刻才暴露。
  window.once('ready-to-show', () => {
    try {
      fs.rmSync(LAUNCH_MARKER, { force: true });
      startupNote('窗口已显示（启动成功）');
      log('窗口已显示');
    } catch (error) {
      log(`清理启动标记失败（不影响运行）：${(error as Error).message}`);
    }

    // 恢复上次的宠物大小。
    // 放在这里而不是构造时：setPetScale 会 setBounds 并通知渲染层改画布，
    // 而那一刻渲染层可能还没加载完，发了也白发（画布不会跟着变）。
    // ready-to-show 之后渲染层已经能收事件了。
    try {
      const storedScale = readSettings().petScale;
      if (storedScale !== 1) {
        setPetScale(storedScale);
        log(`已恢复宠物大小 ${storedScale}x`);
      }
    } catch (error) {
      log(`恢复宠物大小失败（继续用默认）：${(error as Error).message}`);
    }

    // 恢复"文字弹窗存在时间"。同样放在 ready-to-show 之后 ——
    // 渲染层要先把消息处理链挂上，否则这条 `pet:set-bubble-linger` 会被丢掉。
    try {
      const storedLinger = readSettings().bubbleLingerMs;
      petWindow?.setBubbleLinger(storedLinger);
      // 用 startupNote 而不是 log：`log` 写 userData（本环境不可写、异常被吞），
      // 这条恰恰是排查"设置没生效"时最需要看到的
      startupNote(`文字弹窗存在时间 ${Math.round(storedLinger / 1000)}s`);
    } catch (error) {
      log(`恢复文字弹窗存在时间失败（继续用默认）：${(error as Error).message}`);
    }

    /*
     * ⚠️ 这里**原来**还想推「按住说话」配置 —— 实测**推了没用**（2026-10-10）。
     *
     * `ready-to-show`（首次绘制）**可能早于模块脚本求值完成**，
     * 而 `app.js` 是 `<script type="module">`（延迟执行）——
     * 那一刻 `pet.on('pet:ptt-config')` 还没挂上，而 `webContents.send`
     * **不排队**，消息直接丢。
     *
     * 实测证据：
     *
     *     startup.log  [按住说话] 已把开关状态推给渲染层：开
     *     python.log   采集闸门 …     <- 0 条，闸门从来没被关过
     *
     * 改挂到 `pet:status-report`（渲染层主动上报，那时监听一定挂好了），
     * 见下面的处理器。
     *
     * ⚠️ 别拿上面 `pet:set-bubble-linger` 当先例：那个调的是
     * `petWindow.setBubbleLinger()` —— **属性更新**，不依赖渲染层挂监听。
     * 两者不能类推。
     */
  });

  /*
   * 🔴 2026-10-16：托盘回调从 15 个减到 5 个（用户要求精简托盘菜单）。
   *
   * > 「托盘菜单现在只保留显示隐藏宠物、宠物大小、设置、退出、退出全部」
   *
   * ⚠️ 删回调时**必须同时删掉 tray.ts 里的菜单项**，反之亦然 ——
   * 只删一边的话，要么 TS 报"多传了属性"（这边多），要么菜单点了没反应
   * （那边多）。这次是两边一起改的。
   *
   * ⚠️ 那些被删的开关**功能都还在**，只是入口收拢到宠物右键菜单和设置页
   * （精简前逐条核对过 `petMenuTemplate.ts` 里都有）。详见 `tray.ts` 文件头。
   */
  tray = new PetTray({
    onToggleVisible: () => {
      petWindow?.toggleVisible();
      tray?.setVisible(petWindow?.visible ?? true);
    },
    onOpenSettings: () => void openSettings(),
    onSetPetScale: (scale: number) => setPetScale(scale),
    onQuit: () => void quit(),
    onQuitAll: () => void quitAll(),
  });
  // 托盘是用户**唯一**能关掉宠物的入口（窗口是 skipTaskbar，任务栏里没有按钮）。
  // 所以它建不出来是严重问题，不能静默：必须落盘 + 记日志 + 在界面上提醒。
  try {
    tray.create();
    /*
     * 🔴 2026-10-16：这里原来有三句"把落盘状态同步给托盘"——
     *
     *     tray.setBubbleVisible(bubbleVisible);     // 菜单文字 隐藏气泡/显示气泡
     *     tray.setChatVisible(chatVisible);         // 算「聊天模式」那个勾
     *     tray.setTtsSpeech(ttsSpeechEnabled);      // 语音播报的勾
     *
     * 托盘菜单精简之后，这三项都没有了，所以三句一起删。
     *
     * ⚠️ 它们原来解决的是**真问题**（"重启后托盘菜单里写的还是默认值，
     * 与真实状态不符"）。问题本身没消失，只是**搬到宠物右键菜单**去了 ——
     * 那边的菜单是每次右键时重建的，本来就直接读主进程状态，不需要这种同步。
     */
    startupNote('托盘图标已创建');
    log('托盘图标已创建');
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    writeStartupFailure('tray.create()', error);
    log(`托盘图标创建失败：${message}`);
    // 界面上直说，否则用户会以为"没有托盘图标"是正常的
    petWindow.sendToRenderer('pet:toast', {
      level: 'error',
      text: `托盘图标创建失败：${message}（这样就没有退出入口了，可运行 退出宠物.cmd）`,
    });
  }

  registerShortcuts();
  /*
   * 按住说话的键监视器放在**快捷键注册之后**：两者都是"全局按键"，
   * 放一起便于排查冲突（比如用户把按住说话配成了 Ctrl+Alt+P —— 那个组合
   * 已被显示/隐藏宠物占用，注册会失败）。监视器用的是 GetAsyncKeyState
   * 轮询，**不会**被 globalShortcut 抢占，但日志挨着打出来更容易看出问题。
   */
  restartPushToTalk();
  registerRendererIpc();
  registerSettingsIpc();

  // 开始监听"退出全部实例"的广播。不做这一步的话，托盘/设置里的
  // 「退出全部实例」只会关掉自己，其他实例收不到通知。
  quitCoordinator.start();

  // 调试用：启动后直接打开设置窗口（由启动器的 --settings 设置）
  if (process.env.PET_OPEN_SETTINGS === '1') {
    setTimeout(() => openSettings(), 600);
  }

  /*
   * 冒烟测试钩子：`PET_TEST_SAY=<文字>` 时，directly 调一次气泡转发。
   *
   * 存在的理由：气泡的**定位链路**（`petWindow.artScreenAnchor` -> `place()`）
   * 无法靠读代码验证，而说话消息平时只有 sidecar 会发 —— 自动化测试碰不到它。
   * 有这个钩子就能端到端验证"气泡是否真的贴在宠物头顶上"。
   *
   * 与 PET_SCREENSHOT 同一套路：不设变量就完全不进这条路径，生产运行不受影响。
   */
  if (process.env.PET_TEST_SAY) {
    // 延时可用 `PET_TEST_SAY_DELAY_MS` 覆盖：锚点要等立绘首帧量完才存在，
    // 太早触发会走到"等锚点"分支（那是刻意的保护，见 forwardToBubble）
    const sayDelay = Number(process.env.PET_TEST_SAY_DELAY_MS ?? 3000);
    setTimeout(() => {
      startupNote(`[测试] PET_TEST_SAY 触发气泡转发：${process.env.PET_TEST_SAY}`);
      forwardToBubble({ type: 'pet.say.start', id: 'test' } as never);
      forwardToBubble({
        type: 'pet.say.delta',
        id: 'test',
        text: process.env.PET_TEST_SAY,
      } as never);
      forwardToBubble({ type: 'pet.say.end', id: 'test', text: process.env.PET_TEST_SAY } as never);
    }, Math.max(0, sayDelay));
  }

  petWindow.sendToRenderer('pet:status', { text: '正在启动大脑…' });
  log(`主进程就绪，项目根目录：${PROJECT_ROOT}`);

  // 截图探针要在窗口内容画出来之后跑，所以不阻塞 sidecar 启动
  void runScreenshotProbe();

  await startSidecarAndConnect();
}

/**
 * 截图探针：把宠物窗口自己拍下来存成 PNG，然后退出。
 *
 * 存在的理由很实在：这个项目的界面部分**无法靠读代码验证**。上一轮就吃过亏 ——
 * 渲染层里混进了 TypeScript 语法，模块整体解析失败、立绘从未加载过，而 Python 侧
 * 自检全绿、主进程日志正常，从外部完全看不出问题。有了一张真实截图，
 * "界面到底画出来没有"就不再是猜的。
 *
 * 由环境变量 PET_SCREENSHOT 触发（值是输出路径），可选 PET_SCREENSHOT_DELAY_MS 延时。
 * 生产运行不受影响：不设变量就完全不进这条路径。
 */
async function runScreenshotProbe(): Promise<void> {
  const outPath = process.env.PET_SCREENSHOT;
  if (!outPath) return;
  const delay = Number(process.env.PET_SCREENSHOT_DELAY_MS ?? 4500);
  log(`截图探针已启用：${delay}ms 后写入 ${outPath}`);

  setTimeout(async () => {
    try {
      const window = petWindow?.browserWindow;
      if (!window || window.isDestroyed()) throw new Error('窗口不存在');
      const image = await window.webContents.capturePage();
      const buffer = image.toPNG();
      fs.mkdirSync(path.dirname(outPath), { recursive: true });
      fs.writeFileSync(outPath, buffer);
      // 顺带把渲染层控制台里的报错捞出来 —— SyntaxError 只在这里可见
      const consoleErrors = await window.webContents.executeJavaScript(
        'JSON.stringify(window.__petErrors ?? [])',
      );
      log(`截图已写入 ${outPath}（${buffer.length} 字节，${image.getSize().width}x${image.getSize().height}）`);
      log(`渲染层错误：${consoleErrors}`);
    } catch (error) {
      log(`截图失败：${(error as Error).message}`);
    } finally {
      await quit();
    }
  }, Math.max(0, delay));
}

// --------------------------------------------------------------------------- //
// sidecar + socket
// --------------------------------------------------------------------------- //

async function startSidecarAndConnect(): Promise<void> {
  sidecar = new SidecarManager({
    projectRoot: APP_ROOT,
    // 打包态跑冻结好的 pet.exe；开发态不传（走 .venv 的 python -m pet）
    sidecarExe: app.isPackaged ? path.join(APP_ROOT, 'pet', 'pet.exe') : undefined,
    dataDir: DATA_DIR,
    configPath: path.join(CONFIG_ROOT, 'config', 'persona.yaml'),
    logFile: currentLogFile(),
    verbose: DEV_MODE,
  });

  sidecar.on('log', (line: string) => log(line));

  /*
   * 🔴 **sidecar 重启后要重新连**（2026-10-15 修的真 bug）。
   *
   * ⚠️ 少了这一行，sidecar 崩溃一次就等于宠物**永久失联**：
   * 新进程换了端口（`--port 0` 由系统分配），而没人去连新端口，
   * `PetSocket` 只会拿老端口无限重试。日志里全是
   * `ECONNREFUSED 127.0.0.1:<老端口>`，看不出是"没人接新端口"。
   *
   * 详见 `onSidecarReady()` 的说明。
   */
  sidecar.on('ready', (info: ReadyInfo) => onSidecarReady(info));

  sidecar.on('gaveUp', () => {
    setStatus('大脑反复启动失败');
    petWindow?.sendToRenderer('pet:status', {
      text: '大脑启动失败，请查看日志',
      level: 'error',
    });
  });

  let info: ReadyInfo;
  try {
    info = await sidecar.start();
  } catch (error) {
    const message = (error as Error).message;
    log(`sidecar 启动失败：${message}`);
    setStatus('大脑启动失败');
    petWindow?.sendToRenderer('pet:status', { text: `大脑启动失败：${message}`, level: 'error' });
    return;
  }

  if (info.protocolVersion !== PROTOCOL_VERSION) {
    const message = `协议版本不匹配：sidecar=${info.protocolVersion}，前端=${PROTOCOL_VERSION}。请重新构建前端。`;
    log(message);
    setStatus('协议不匹配');
    petWindow?.sendToRenderer('pet:status', { text: message, level: 'error' });
    return;
  }

  log(`sidecar 就绪：port=${info.port} brain=${info.brain}`);
  connectSocket(info.port, info.brain);
}

/**
 * sidecar **重启后**要重新连（2026-10-15 修的真 bug）。
 *
 * ## 🔴 这个 bug 的症状就是「宠物不回应」
 *
 * 实测抓到的日志（一次 sidecar 崩溃之后）：
 *
 * ```
 * 18:48:22  sidecar 就绪：port=58547  ->  已连接到 sidecar      ✅
 * 18:49:03  read ECONNRESET                                   <- sidecar 崩了
 * 18:49:04  新 sidecar 起来，监听 60350                         <- 新端口
 * 18:49:04  ECONNREFUSED 127.0.0.1:58547                       <- 还在连老端口
 * ...
 * 18:51:35  ECONNREFUSED 127.0.0.1:58547                       <- 无限循环
 * ```
 *
 * ⚠️ **`sidecar 就绪：port=...` 那条日志只出现过一次** ——
 * 第二次及以后 `SidecarManager` 照样 `emit('ready', info)`，
 * 但**这里没有人监听那个事件**，所以新端口永远没人去连。
 *
 * 而 `PetSocket` 的重连逻辑（`onClose` -> `this.connect()`）用的
 * 是**构造时定死的 URL** —— 它只会拿老端口一直重试。
 *
 * ## 所以这里必须听 `ready`
 *
 * `SidecarManager.start()` 每次成功都发 `ready`：
 *
 * ```
 * 首次启动   emit('ready')  <- 下面那个 await start() 已经处理了
 * 崩溃重启   emit('ready')  <- **只有这个监听器能接住**
 * ```
 *
 * ⚠️ 首次会**重复调用**一次 `connectSocket`（`await start()` 之后一次，
 * 事件一次）。这没问题 —— 后一次把前一次那个 socket 关掉重建，
 * 结果是连上同一个端口。宁可多连一次，也不能在重启后失联。
 *
 * ⚠️ 协议版本也要在这里再查一遍：重启后的 sidecar 用的是同一份代码，
 * 但显式检查比"假设它没变"便宜。
 */
function onSidecarReady(info: ReadyInfo): void {
  if (info.protocolVersion !== PROTOCOL_VERSION) {
    log(
      `sidecar 重启后协议版本不匹配：sidecar=${info.protocolVersion}，前端=${PROTOCOL_VERSION}`,
    );
    return;
  }
  log(`sidecar 重启完成：port=${info.port} —— 重新建立连接`);
  connectSocket(info.port, info.brain);
}

function connectSocket(port: number, brain: string): void {
  /*
   * 🔴 先关掉上一个 socket（2026-10-15）。
   *
   * ⚠️ 不关的后果**比"连不上"更隐蔽**：旧 `PetSocket` 的 `onClose` 里有
   * 退避重连，`this.connect()` 用的是它自己那个**老 URL** ——
   * 于是旧 socket 会在后台**永远重试一个已经没人监听的端口**，
   * 而日志里只有一行行 `ECONNREFUSED`，看不出是"两个 socket 在打架"。
   *
   * （实测就是这个：58547 被重试了整整 2 分钟，而真实端口是 60350。）
   */
  const previous = socket;
  if (previous) {
    try {
      previous.close();
    } catch {
      /* 关闭阶段的异常无所谓 —— 目的只是让它别再重连 */
    }
  }

  const next = new PetSocket({ url: `ws://127.0.0.1:${port}` });
  socket = next;
  const isCurrent = (): boolean => socket === next;

  socket.on('log', (line: string) => log(line));

  /**
   * 语音音频配对：sidecar 先发 `voice.audio`（JSON，带 index）再发同序号二进制帧。
   * 一个 WebSocket 上文本与二进制是两条独立通道，必须靠序号配对。
   * 逻辑抽在 audioPairer 里，便于单独测试各种到达顺序。
   */
  const audioPairer = new AudioPairer();

  socket.on('binary', (payload: Buffer) => {
    /*
     * ⚠️ 守卫「不是当前 socket 就丢掉」（2026-10-15）。
     *
     * 换端口时会新建 socket，但旧对象**不会被 GC**（事件监听还挂着），
     * 它在关闭前可能又吐几帧出来。那些帧属于上一次连接 ——
     * 最坏是**放错批次的音频**（`audioPairer` 的序号是对齐的）。
     */
    if (!isCurrent()) return;
    audioPairer.acceptBinary(new Uint8Array(payload));
    for (const item of audioPairer.drain()) {
      petWindow?.sendToRenderer('pet:voice-audio', {
        bytes: item.bytes,
        format: item.format,
        // 音量随音频一起下发（2026-10-19「可调节声音大小」）
        volume: currentTtsVolume(),
      });
      /*
       * 🔴 语音**真的开始播了** ->「说话中」（2026-10-17 修）。
       *
       * ⚠️ 钩子必须挂在**这里**（二进制帧 drain），不能挂在 `voice.audio`
       * JSON 元信息那条路径 —— 元信息先到、二进制后到，`acceptMeta` 当时
       * drain 不出东西（2026-10-17 用户实测：状态条在语音播放时仍卡
       * 「语音准备中」，钩子挂错地方了）。
       *
       * 触发条件：回合还在"思考中"（= 文字回复已拿到、TTS 合成期间，见
       * pet.say.end 的新语义）或上一段语音还在播。别的时机收到音频
       * （朗读按钮之类）不动状态机。
       * 「说话中 -> 空闲」由渲染层播完后的 `pet:voice-played` 负责。
       * 状态条显示的是**宠物**的状态 —— "说话中"是宠物在说，不是用户。
       */
      if (turnThinking || voiceSpeaking) {
        turnThinking = false;
        voiceSpeaking = true;
        setStatus('说话中');
        log('[状态条] 语音开始播放 -> 说话中（青）');
      } else {
        // 诊断：转发点状态对不上时留痕（渲染层的 pet:voice-speaking 兜底触发）
        log(`[状态条] 音频帧到达但状态不在思考/说话（不切说话中）`);
      }
    }
  });

  socket.on('open', () => {
    if (!isCurrent()) return;
    log('已连接到 sidecar');
    /*
     * 状态行显示**服务商**，不是协议名（2026-10-12 修）。
     *
     * `brain` 是 sidecar 报的 `providers.brain.provider`，值是协议类型
     * （`openai` = 「任何 OpenAI 兼容接口」）—— 用户接 DeepSeek 时看到
     * 「在线 · openai」会以为配错了。改成按 `base_url` 认服务商：
     *
     *     api.deepseek.com    -> DeepSeek
     *     api.siliconflow.cn  -> 硅基流动
     *
     * 认不出来就退回原来的 `brain` 值（宁可显示得笨，也别显示空的）。
     */
    const providerLabel = brainStatusLabelNow(String(brain));
    const statusText = providerLabel || String(brain);
    setStatus(`在线 · ${statusText}`);
    /*
     * 把算出来的状态行**记进日志**。
     *
     * 托盘菜单是原生窗口、抓不到 DOM，所以"菜单里到底显示什么"只能靠
     * 用户截图确认 —— 而这个 bug（服务商名认不出来）已经因为"我改完以为好了"
     * 来回了两轮。留一行日志，下次一眼就能看出是哪一步读不到值。
     *
     * 日志同时打出**原始 base_url**：认得对不对要能对着它核。
     */
    log(
      `状态行：在线 · ${statusText}` +
        `（base_url=${currentBrainBaseUrl() || '读不到'}，协议=${String(brain)}）`,
    );
    send({ type: 'hello', protocolVersion: PROTOCOL_VERSION, client: `electron-${process.platform}` });
    petWindow?.sendToRenderer('pet:status', { text: '在线', level: 'ok', brain: statusText });

    /*
     * 🔴 **每次连上都要补报语音总开关**（2026-10-10 修，"按了没反应"的真凶）。
     *
     * ## 为什么
     *
     * sidecar 的 `VoiceSession.voice_config.enabled` **默认是 `False`**
     * （从配置的 `providers.asr.enabled` 读，而那个键没写 -> False），
     * 全靠前端发 `voice.control` 打开。
     *
     * 而前端**只在预热麦克风时发一次**（`app.js` 的 `warmUpVoice`）。时序上：
     *
     *     21:38:40  前端预热麦克风 -> 发 voice.control{enabled:true}
     *     21:38:41  sidecar 才就绪、才连上      <- **那条消息发早了，丢了**
     *               sidecar 的 enabled 停在 False
     *
     * 后果：**闸门能开、音频帧在推、本地 GPU 也正常，但 `feed()` 第一行
     * 就把所有帧丢了**：
     *
     *     if not self.voice_config.enabled: return None
     *
     * 表现正是用户报的「按了没反应」。实测日志：
     *
     *     21:51:27  按住说话：收到 phase='start'   -> 采集闸门：开
     *     21:51:28  按住说话：松开收尾被跳过 —— 语音总开关没开（enabled=False）
     *
     * ## 为什么在主进程补报而不是让渲染层再发一次
     *
     * 主进程**知道 socket 什么时候连上**（就是这个回调），渲染层不知道。
     * 让渲染层"猜时机再发一次"必然还有窗口期。这里补报是**确定的**：
     * 只要连上就一定报。
     *
     * ⚠️ **重连也走这里** —— sidecar 重启后同样会丢状态，而渲染层的
     * `voiceOn` 还是 true（它不会因为 socket 断了就改），所以必须补报。
     */
    send({ type: 'voice.control', enabled: micWantedOn });
    startupNote(`[语音] 已连上 sidecar，补报麦克风开关：${micWantedOn ? '开' : '关'}`);
  });

  socket.on('close', () => {
    /* ⚠️ 旧 socket 关闭时不要动状态 —— 新连接可能已经连上了（2026-10-15） */
    if (!isCurrent()) return;
    log('与 sidecar 的连接断开');
    setStatus('重连中…');
    petWindow?.sendToRenderer('pet:status', { text: '连接断开，正在重连…', level: 'warn' });
  });

  socket.on('message', (message: ToUiMsg) => {
    /*
     * ⚠️ 旧 socket 的消息不该由新连接处理（2026-10-15）。
     *
     * 换端口时旧 socket 会先被 `close()`，但关闭是异步的 ——
     * 这段时间里它仍可能吐出已经到达的消息。让它们进来会导致
     * **同一条消息被处理两次**（旧连接一次 + 新连接一次），
     * 而症状是"某个动作偶尔重复执行"这种极难复现的 bug。
     */
    if (!isCurrent()) return;

    /*
     * 带 `reqId` 的回执先交给等待者，**不再往下转发**。
     *
     * 这是主进程唯一一处"请求-等待回执"机制（见 `requestFromBrain`）。
     * 之所以需要：有些操作**只有 Python 能做** —— 比如备份必须用 `VACUUM INTO`，
     * 因为只有它持有打开的数据库连接，直接复制文件可能拿到半个库。
     * 而这类操作又要给用户一个明确结果（成功 / 失败原因），不能fire-and-forget。
     */
    const reqId = (message as { requestId?: unknown }).requestId;
    if (typeof reqId === 'string' && reqId) {
      /*
       * 除了交给等待者，**也要转给设置窗口**。
       *
       * 为什么两个都要：`requestFromBrain` 的调用方拿到的是"这次请求的结果"，
       * 而设置窗口需要持续显示"最近一次备份怎么样"（包括**启动时的自动备份** ——
       * 那次没有调用方在等，只有推送）。少任何一边，用户都会看到不完整的状态。
       *
       * ⚠️ **必须按类型分流**（2026-10-05）。原来是无条件发
       * `{ memoryBackup: message }`，于是**任何**带 requestId 的回执都会被
       * 设置窗口当成备份回执处理 —— 记忆管理界面（`memory.result`）的结果
       * 会跑到"备份状态"那一行去显示，而且因为它没有 `ok/path` 字段，
       * 显示出来的是「⚠️ 手动备份失败：未知原因」。
       *
       * 这类 bug 的形态：**消息送错了接收者，而接收者不校验类型就用**。
       */
      if (message.type === 'memory.backup.done') {
        settingsWindow?.send('settings:changed', { memoryBackup: message });
      } else if (message.type === 'memory.result') {
        settingsWindow?.send('settings:changed', { memoryResult: message });
      }
      const pending = pendingReplies.get(reqId);
      if (pending) {
        pendingReplies.delete(reqId);
        clearTimeout(pending.timer);
        pending.resolve(message as unknown as Record<string, unknown>);
        return;
      }
    }

    if (message.type === 'ready') {
      activity = message.activity?.overall ?? activity;
      // ⚠️ 2026-10-16：托盘不再有「活泼度」子菜单，所以没有 tray?.setActivity 了。
      // `activity` 这个变量本身还要用（下面发给渲染层）。
      // 免打扰的真相源在 sidecar：以它回报的 autonomy.muted 为准，
      // 这样托盘、界面、大脑三处不会各存一份而互相矛盾。
      if (message.autonomy && typeof message.autonomy.muted === 'boolean') {
        if (message.autonomy.muted !== paused) {
          paused = message.autonomy.muted;
          // ⚠️ 托盘不再有「免打扰」项（2026-10-16 精简），只通知渲染层。
          petWindow?.sendToRenderer('pet:paused', { paused, minutes: 0 });
        }
      }
      petWindow?.sendToRenderer('pet:config', {
        activity: message.activity,
        autonomy: message.autonomy,
        voice: message.voice,
        memory: message.memory,
        vision: message.vision,
        persona: message.persona,
        brain: message.brain,
      });
      /*
       * 思考模式：以 sidecar 回报的**生效值**为准。
       *
       * ⚠️ 与 vision 不同，这里**不需要**"先下发用户意愿"那一步：
       * thinking 直接写在 `config/persona.yaml` 里，Python 启动时自己就读到了。
       * 所以回报值就是真相，不会出现"重启被抹掉"的问题。
       *
       * ⚠️ 2026-10-16：托盘不再有「思考模式」项，所以只维护主进程的
       * `thinkingEnabled`（渲染层和宠物菜单还要读它），不推托盘。
       */
      const fromThinking = thinkingFrom(message.config);
      if (fromThinking !== null) {
        thinkingEnabled = fromThinking;
      }

      // 联网搜索同理：配置是真相源
      // 🔴 2026-10-16：真源改为三态模式（`fromSearchMode` 才是真源）
      const fromSearchMode = webSearchModeFrom(message.config);
      if (fromSearchMode !== null) {
        webSearchMode = fromSearchMode;
      }
      // 语音播报同理：真源是 persona.yaml 的 providers.tts.enabled，
      // sidecar 启动时自己就读到了，所以回报值就是真相（2026-10-14）
      // ⚠️ 2026-10-16：托盘不再有「语音播报」项，只维护主进程状态。
      const fromTtsSpeech = ttsSpeechFrom(message.config);
      if (fromTtsSpeech !== null) {
        ttsSpeechEnabled = fromTtsSpeech;
      }
      // ⚠️ 顺序很重要：**先**把用户上次保存的意愿下发给 sidecar，
      // **再**用回报值刷新本地状态。
      //
      // 为什么不能反过来：sidecar 的 `config.update` 只改内存、不写文件
      // （见 settingsStore.visionEnabled 的注释），所以重启后 sidecar 手里
      // 是 yaml 里的默认值（false）。如果直接采信它的回报，用户上次勾的
      // "开"就会被抹掉 —— 用户要求"每次重开记得上次的状态"。
      // 所以这里以**主进程存的用户意愿**为准，sidcar 收到后再回报校准。
      //
      // 🔴 2026-10-16：原来这里还有一句 `syncVisionStateFromConfig(message)`，
      // 它唯一的作用是给**托盘菜单**那一项同步勾选状态。托盘精简后那一项没了，
      // 所以调用点和函数一起删掉了。
      //
      // ⚠️ **隐私开关本身没有受影响**：`visionEnabled` 的真源刷新就在下面这几行
      // （第一次启动走 settingsStore、之后走 sidecar 回报），和那个函数无关。
      // 开关的界面入口在宠物右键菜单和设置页，两边共用同一个真源。
      if (!visionRestored) {
        visionRestored = true;
        const stored = readSettings();
        if (stored.visionEnabled !== visionEnabled) {
          // 理论上不会走到这里（启动时已赋过），留着以防将来改启动顺序
          visionEnabled = stored.visionEnabled;
        }
        if (visionEnabled) setVisionWatching(true);
      } else {
        const fromMessage = visionEnabledFrom(message.config);
        if (fromMessage !== null) visionEnabled = fromMessage;
      }
      // 记住最近一次生效配置，并把状态推给设置窗口
      lastConfigSnapshot = message.config ?? null;
      lastReady = {
        brain: message.brain,
        persona: message.persona,
        vision: message.vision,
        config: message.config,
      };
      /*
       * 🔴 **拿到配置快照后要重算一次状态行**（2026-10-12 修）。
       *
       * ## 这是个时序问题，症状是"改了但没生效"
       *
       * 状态行想显示**服务商**（DeepSeek / 硅基流动），而服务商是从
       * `providers.brain.base_url` 认出来的 —— 那个值在 `lastConfigSnapshot` 里。
       *
       * 但消息顺序是：
       *
       *     socket.on('open')   <- 这里设了状态行，此刻快照还是 null
       *     ready 消息          <- 这里才拿到 config
       *
       * 于是 `brainStatusLabelNow()` 读不到 base_url、只好退回协议名 `openai`。
       * 而**之后再没有任何地方重设状态行** —— 用户看到的就是永远修不好的 `openai`
       * （实测：改了代码、重启、菜单里还是 `在线 · openai`）。
       *
       * 所以这里补一次重算。**顺序敏感的东西不能只在一个地方设一次。**
       */
      if (lastReady.brain) {
        setStatus(`在线 · ${brainStatusLabelNow(String(lastReady.brain))}`);
      }
      void pushSettingsState();
    }
    /*
     * 语音合成：Python 请求，**主进程执行**（2026-10-08）。
     *
     * ## 🔴 为什么不能让 Python 自己做
     *
     * 本地合成走 Windows SAPI，要起 PowerShell。实测四条链路：
     *
     *     shell  -> powershell            ✅ Voice.Name=[Microsoft Huihui Desktop]
     *     Node   -> powershell            ✅ 三种接法全部成功（WAV 60872 字节）
     *     Python -> powershell            ❌ Voice.Name=[]（空）、Speak 失败
     *     Node -> Python -> powershell    ❌ 同上
     *
     * 报错是 SAPI 自己的话："No voice installed on the system or none available
     * with the current security setting." —— 但 `GetInstalledVoices()` 明明能列出
     * 2 个音色（含中文 Huihui、Enabled=True）。**枚举得到、用不了。**
     *
     * 已排除：编码、音色名、位数、-NonInteractive、-STA、-Command/-File、TEMP、stdio。
     * 唯一变量就是"谁起的进程"。而**生产里 Electron 就是父进程** —— 走 Node 天然可用。
     *
     * 详见 `ttsLocal.ts` 的文件头。
     */
    if (message.type === 'tts.synthesize') {
      const req = message as {
        seq?: number;
        text?: string;
        voice?: string;
        rate?: number;
        volume?: number;
      };
      const seq = Number(req.seq ?? 0);
      // 节点 C：主进程到底收没收到这个请求（之前这里没有日志，
      // 所以"没收到"和"收到了但处理时崩了"完全分不出来）
      log(
        `[tts] 收到合成请求 seq=${seq} 音色=${JSON.stringify(req.voice ?? '')} ` +
          `文字=${String(req.text ?? '').length} 字`,
      );
      void (async () => {
        /*
         * ⚠️ **必须整段包 try/catch**（2026-10-09 修）。
         *
         * 原来这里没有兜底：`synthesizeSpeech` 一旦**抛异常**（而不是返回
         * `{ok:false}`），下面的 `send()` 就永远不执行 —— 于是
         *
         *   · Python 侧**收不到任何回执**，只能等到 20s 超时
         *   · 超时抛的异常消息是空的 -> 日志里只有一行 `语音合成失败：`
         *   · 主进程日志里**一条记录都没有**（`log()` 在 send 之后）
         *
         * 三件事叠加的结果是「点了试听没反应，而且两边日志都查不出原因」。
         * 这就是用户报的那个问题。
         */
        let r: Awaited<ReturnType<typeof synthesizeSpeech>>;
        try {
          r = await synthesizeSpeech(String(req.text ?? ''), {
            script: path.join(APP_ROOT, 'apps', 'desktop', 'tools', 'win-tts.ps1'),
            voice: String(req.voice ?? ''),
            rate: Number(req.rate ?? 0),
            volume: Number(req.volume ?? 100),
            workDir: path.join(DATA_DIR, 'tts-tmp'),
            log: (line) => log(line),
          });
        } catch (error) {
          const msg = `合成时抛异常：${(error as Error)?.message || String(error)}`;
          log(`语音合成失败（seq=${seq}）：${msg}`);
          send({ type: 'tts.result', seq, ok: false, message: msg } as never);
          return;
        }
        send({
          type: 'tts.result',
          seq,
          ok: r.ok,
          message: r.message,
          // base64：JSON 里传二进制只能这样。Python 侧解回来就是裸 PCM。
          pcm: r.pcm ? r.pcm.toString('base64') : undefined,
          sampleRate: r.sampleRate,
        } as never);
        // 节点 D + E
        log(
          `[tts] 合成结束 seq=${seq} ok=${r.ok} pcm=${r.pcm ? r.pcm.length : 0} 字节` +
            `${r.ok ? '' : ` 原因=${r.message || '(没有消息)'}`}`,
        );
        if (!r.ok) log(`语音合成失败（seq=${seq}）：${r.message || '(没有消息)'}`);
      })();
    }
    if (message.type === 'voice.audio') {
      // 记下格式，等对应的二进制帧到达后一起交给渲染层
      const index = Number((message as { index?: number }).index ?? 0);
      /*
       * ⚠️ 真的产出音频了 -> TTS 恢复健康（2026-10-15）。
       *
       * 和下面的 `voice.tts_failed` 配对：一个降级、一个恢复。
       * 没有这一条的话，一次失败会把 `ttsHealthy` **永远钉在 false**，
       * 于是沉浸模式从此再也隐藏不了文字（那也是一种坏掉）。
       */
      if (ttsHealthy === false) {
        ttsHealthy = true;
        log('[语音] 合成恢复正常 —— 沉浸模式重新按「有语音」处理');
      }
      if (index > 0) {
        for (const item of audioPairer.acceptMeta(
          index,
          String(message.format ?? 'mp3'),
          String((message as { text?: string }).text ?? ''),
        )) {
          petWindow?.sendToRenderer('pet:voice-audio', {
            bytes: item.bytes,
            format: item.format,
            volume: currentTtsVolume(),
          });
          /*
           * ⚠️ 「语音开始播放 -> 说话中」的钩子**不在这里** —— 元信息先到、
           * 二进制帧后到，`acceptMeta` 此时 drain 不出东西（2026-10-17 实测）。
           * 真正的转发点在二进制帧的 `drain()` 循环里（socket.on('binary')）。
           */
        }
      }
    }

    /*
     * TTS 合成失败（2026-10-15）。
     *
     * ⚠️ **这条不进渲染层** —— 它是给主进程自己的，只用来把
     * `ttsHealthy` 置为 false，让沉浸模式**降级显示文字**。
     *
     * 为什么需要：用户报「发不了声音 + 回复不了信息」，根因是沉浸模式
     * 按「TTS 配置开着」判断有没有语音，而实际合成 403 失败 ——
     * 文字被主动隐藏、语音又没有，两头都收不到。
     */
    if (message.type === 'voice.tts_failed') {
      const reason = String((message as { reason?: string }).reason ?? '');
      if (ttsHealthy !== false) {
        ttsHealthy = false;
        log(
          `[语音] 合成失败 —— 沉浸模式下这一条及之后会**降级显示文字**：${reason || '(没有原因)'}`,
        );
      }
      /*
       * 🔴 TTS 失败 -> **异常态**（2026-10-16 用户指定：「TTS 失败报错，
       * 就进入异常状态也可以」）。比回空闲更诚实 —— 用户正等着听，
       * 结果没声音，红灯说明"出了问题"而不是"没事情"。
       * 下一回合的 pet.thinking 会把它带出异常态。
       */
      setStatus(reason ? `TTS 合成失败：${reason}` : 'TTS 合成失败');}
    /*
     * 说话状态 -> 状态条（2026-10-16 用户要求）。
     *
     * ## 用户原话
     *
     * > 「聆听就不用黄色了，宠物自己会显示，就做说话显示黄色和文字就好了」
     * > 「彻底删除聆听」
     *
     * 所以这里**只认 `speaking`**：
     *
     *   · `speaking`  -> 黄灯 + 「说话」
     *   · `idle`      -> 恢复「在线 · <服务商>」
     *   · 其他        -> **什么都不做**（见下）
     *
     * ## 🔴 `listening` / `hearing` 是**故意什么都不做**，不是漏了
     *
     * ⚠️ 别把它们当成"忘了处理"。理由有两条：
     *
     *   ① **用户明确不要**：宠物自己的姿态已经在表现"在听"了，
     *      状态条再来一个灯是重复。
     *   ② **更重要**：这里若 `setStatus('在线...')`，会**把「搜索」踩掉**。
     *      场景 —— 用户在宠物搜索时又说了句话 -> `hearing` 到达 ->
     *      状态条从「搜索」跳回「空闲」-> 用户看到的是"搜索没了"，
     *      而它其实还在搜。
     *
     * 所以聆听期间**保持现状**，由真正结束的那一方收尾
     * （`speaking` / `idle` / `pet.tool` 的 `end`）。
     *
     * ⚠️ **`listening` 这个显示档已经彻底删掉**（`StatusKind` 里没有了）。
     * 但 `voice.state` 的 `'listening'` 字符串**还在协议里** ——
     * 那是**语音管线**的状态（麦克风在听），删了麦克风就不工作。
     * 两者名字像，是两件事：
     *
     *     pet.voice.VoiceState.LISTENING   语音管线 —— **保留**
     *     pet.desktop.StatusKind           状态条显示 —— **已删**
     *
     * ## 🔴 为什么以前「说话」这一档是死的
     *
     * `StatusKind` 里 `'speaking'` 早就定义了、CSS 也写好了、
     * `statusKindFor` 里也留了「说话|生成|思考」的关键词 ——
     * **但主进程从来不处理 `voice.state`**（只处理 `voice.audio`）。
     *
     * 于是没有任何调用点会产出含"说话"的文字，那一档永远不亮。
     * 这是本项目那族「两边都写好了、中间那根线没接」的又一次。
     *
     * ## ⚠️ 为什么走 `setStatus` 而不是直接 `statusWindow.setStatus`
     *
     * 直接调会让**托盘**停留在上一次的文字（"在线 · DeepSeek"），
     * 状态条却说"说话"—— 两个地方说的不是一件事。
     * 走共用的 `setStatus()`，两处一起变。
     * （`pet.tool` 那里为了控制灯色额外直调了一次，见下面的说明。）
     *
     * ## ⚠️ 为什么"恢复"也是走 `setStatus`
     *
     * 和 `pet.tool` 的 `phase === 'end'` 同一个理由：**刻意重新算一遍**
     * 而不是存"上一个状态"—— 服务商可能刚被用户改过，
     * 存旧值会显示错的服务商名（第八十五节那个「状态行显示 openai」的坑）。
     */
    if (message.type === 'voice.state') {
      const voiceState = String((message as { state?: unknown }).state ?? '');
      if (voiceState === 'speaking') {
        /*
         * 🔴 2026-10-16：**不再**在这里显示"说话"、也不清"语音准备中"。
         *
         * 原因：Python 侧的 `voice.state speaking` 是在 `pet.say.start`
         * （**文字开始生成**）就发的（voice.py `_on_pet_say_start`，为打断
         * 检测服务），不是语音真的在播 —— 拿它当"说话"信号，状态条会在
         * 文字阶段跳"说话"，而真正播放/播完之后反而**没有任何事件**，
         * "语音准备中"一挂就是 90 秒看门狗超时（用户报的「语音开始、
         * 结束了还显示准备中，好久不回空闲」）。
         *
         * 真正的信号链在下面 `voice.audio`（音频转发 = 开始播）和
         * `pet:voice-played`（渲染层 `onended` = 播完了）。
         */
        log('[语音] sidecar 报 speaking（文字阶段信号，状态条不动）');
      } else if (voiceState === 'idle') {
        clearActivityStatus();
      }
      /*
       * `listening` / `hearing` / `thinking` 到这里就结束了 —— 不动状态条。
       * 理由见上面那段（用户不要 + 会踩掉「搜索」）。
       */
    }
    /*
     * 回合相位 -> 状态条（状态机见 `refreshActivityStatus`）。
     *
     *   pet.thinking / pet.say.start -> 思考中（紫）
     *   pet.say.end（TTS 还要说话）  -> 保持思考中（合成期间，2026-10-17 删"语音准备中"）
     *   音频开播 -> 说话中（青）；播完 -> 空闲
     *
     * ⚠️ 状态条**一共五档**（idle/thinking/searching/speaking/error）。
     *
     * ⚠️ sidecar **早就一直在发 `pet.thinking`**（每个回合开始都会发，
     * gateway 也早就登记了转发）—— 主进程一直没消费。这次终于接上了。
     */
    if (message.type === 'pet.thinking' || message.type === 'pet.say.start') {
      turnThinking = true;
      refreshActivityStatus();
    }
    if (message.type === 'pet.say.end') {
      const reason = String((message as { reason?: unknown }).reason ?? 'done');
      /*
       * 🔴 2026-10-17 用户拍板：**删除「语音准备中」这一档** —— 和思考中重复。
       *
       * 新语义：文字回复拿到后如果 TTS 还要说话（合成期间），**保持思考中**
       * （合成本来就是"还在干活"）；音频真正开播才转「说话中」；
       * 纯文字模式（TTS 关 / 引擎是 none / 被打断）直接回空闲。
       */
      turnThinking =
        reason === 'done' && readTtsConfig().provider !== 'none' && ttsSpeechEnabled;
      refreshActivityStatus();
    }
    /*
     * 工具执行状态 -> 状态条（2026-10-12 用户要求）。
     *
     * 用户原话：「宠物联网搜索的时候状态栏不会变，没有显示联网搜索中」，
     * 后来又指定了颜色：「**蓝色代表联网搜索**」。
     *
     * ## 为什么在这里处理而不是转发给渲染层
     *
     * 状态条是**主进程的窗口**（`StatusWindow`），它只认
     * `setStatus(kind, text)`。所以这条消息的终点是主进程自己，
     * 不是宠物窗口的渲染层 —— 转发过去那边不认识这个类型。
     *
     * ## ⚠️ `phase === 'end'` 时必须**恢复成在线**
     *
     * 只在 start 时改状态的话，搜完之后状态条会**永远停在「搜索」**——
     * 那比不改还糟（用户以为它卡住了）。
     *
     * 恢复成什么：走同一个 `setStatus()`，它自己会推导出
     * `在线 · <服务商>` 对应的灯色与 2 字词 ——
     * 这里**刻意重新算一遍**而不是存"上一个状态"：服务商可能刚被用户改过，
     * 存旧值会显示错的服务商名（第八十五节那个「状态行显示 openai」的坑）。
     */
    if (message.type === 'pet.tool') {
      const phase = String((message as { phase?: unknown }).phase ?? '');
      const tool = String((message as { tool?: unknown }).tool ?? '');
      const detail = String((message as { detail?: unknown }).detail ?? '');
      /*
       * 🔴 2026-10-16：改为**计数 + 集中刷新**（状态机见 refreshActivityStatus）。
       * 原来在 end 分支直接恢复"在线"—— 而回合往往还没结束（搜完还要继续生成），
       * 于是状态条 搜索 -> 在线 -> 说话 来回跳（用户报的「变来变去，很怪」）。
       * 现在：start 计数 +1、end 计数 -1，期间显示"联网搜索中"，
       * 回到 0 后由刷新函数决定回思考还是空闲。
       */
      if (phase === 'start') {
        searchDepth += 1;
        const verb = tool === 'web_fetch' ? '读网页' : '搜索';
        refreshActivityStatus();
        log(`[工具] ${verb}开始：${detail || tool}（进行中 ${searchDepth} 个）`);
      } else if (phase === 'end') {
        searchDepth = Math.max(0, searchDepth - 1);
        refreshActivityStatus();
        log(`[工具] ${tool} 结束（剩余 ${searchDepth} 个）`);
      }
    }

    /*
     * 浏览器兜底读网页（2026-10-13）。
     *
     * ## 方向：Python -> 主进程 -> Python
     *
     * 和 `tts.synthesize` 同构：Python 做不到的事交给主进程，
     * 结果用 `web.browser.result` 带**同一个 requestId** 回去。
     *
     * ## ⚠️ 为什么必须有这条回执，哪怕失败
     *
     * 没有回执的话 Python 只能等到超时（20 秒）—— 那是**纯粹的浪费**：
     * 失败信息（"窗口建不起来" / "页面是空的"）本来立刻就能知道。
     * 而含糊的失败会让模型**编造页面内容**（这个项目里已经栽过一次）。
     *
     * ## ⚠️ 开关的**两侧**都要判
     *
     * Python 那边已经判过一次（`web.browser_fallback`），这里是第二道。
     * 看起来冗余，但两边都可能单独被改（比如配置手改、Python 版本旧）——
     * 而这是**隐私开关**，多判一次的成本是零，漏判的代价是"用户没同意却走了浏览器"。
     */
    if (message.type === 'web.browser.fetch') {
      const req = message as {
        requestId?: unknown;
        url?: unknown;
        timeoutMs?: unknown;
      };
      const requestId = String(req.requestId ?? '');
      const url = String(req.url ?? '');
      const allowed = readDeepScalar(CONFIG_ROOT, ['tools', 'browser_fallback'], false) === true;
      log(`[浏览器兜底] 收到请求（开关=${allowed ? '开' : '关'}）：${url.slice(0, 80)}`);
      if (!allowed) {
        send({
          type: 'web.browser.result',
          requestId,
          ok: false,
          reason: '浏览器兜底没打开（设置 → 联网 → 浏览器兜底）',
        } as never);
      } else {
        void (async () => {
          /* 整段包 try/catch —— 抛异常会让 Python 等到超时（照 tts.synthesize 的教训） */
          try {
            const timeoutMs = Number(req.timeoutMs ?? 0);
            const result = await fetchPageViaBrowser(url, {
              ...(Number.isFinite(timeoutMs) && timeoutMs > 0 ? { timeoutMs } : {}),
              log: (line) => log(line),
            });
            send({
              type: 'web.browser.result',
              requestId,
              ok: result.ok,
              url: result.url,
              finalUrl: result.finalUrl,
              title: result.title,
              text: result.text,
              metas: result.metas,
              reason: result.reason,
              elapsedMs: result.elapsedMs,
            } as never);
          } catch (error) {
            const msg = `浏览器兜底抛异常：${(error as Error)?.message || String(error)}`;
            log(msg);
            send({ type: 'web.browser.result', requestId, ok: false, reason: msg } as never);
          }
        })();
      }
    }

    /*
     * 活泼度档位**到期回落**（2026-10-17 用户要求）。
     *
     * 用户原话：「此状态下注意力掉的很快，20分钟不到就会回到安静状态，
     *   因为情绪发泄完了，开始平静的不想说话了。」
     *
     * 大脑（Python）负责计时，到点发这条消息；主进程只做两件事：
     *
     *   ① 把内存里的 `activity` 改成回落值（菜单下次打开就显示「安静」）
     *   ② **不写 persona.yaml** —— 活泼度是运行时状态，和用户手动点菜单
     *      完全一样（`setActivity` 也不写文件，见 settingsStore 里的说明）
     *
     * ⚠️ 处理完**不要再往下转发**给渲染层：渲染层不认识这个类型，
     * 收不到也不报错，但日志里会多一行"丢弃无法识别的消息"，白噪声。
     */
    if (message.type === 'activity.tier') {
      const tier = message as { fromName?: unknown; toName?: unknown; overall?: unknown };
      const overall = Number(tier.overall);
      if (Number.isFinite(overall)) {
        /*
         * 2026-10-18 用户选择 B：**回落也落盘**。
         *
         * `setActivity` 内部统一负责写文件（手动切档与回落都走它），这里不重复写。
         * 之前只在内存里改，于是"自语 20 分钟后回落到安静 → 重启又变回自语"，
         * 看起来像设置没生效。
         */
        setActivity(overall);
        log(
          `活泼度：${String(tier.fromName ?? '?')} 到期回落 -> ${String(tier.toName ?? '?')}` +
            `（${overall.toFixed(2)}，已写入 persona.yaml）`,
        );
      } else {
        log(`活泼度：回落消息缺 overall，已忽略：${JSON.stringify(message).slice(0, 120)}`);
      }
      return;
    }

    /*
     * 状态镜像（2026-10-23）。
     *
     * ⚠️ 必须在**转发给渲染层之前**更新：菜单下次打开时读的就是 `petState`。
     *    如果只在渲染层处理，主进程这边永远是空串，菜单会退回按 activity 猜 ——
     *    而状态现在会自己变（精力低、30 分钟没聊天），猜出来的可能是过期的。
     */
    if (message.type === 'pet.emotion') {
      const snapshot = (message as { pet_state?: { state?: unknown } }).pet_state;
      const next = typeof snapshot?.state === 'string' ? snapshot.state : '';
      if (next && next !== petState) {
        const before = petState;
        petState = next;
        log(`状态：${before || '（未知）'} -> ${next}`);
      }
    }

    // 说话类消息**改发到独立的气泡窗口**（宠物窗口里那份气泡已删除）。
    //
    // 为什么独立成窗口：用户要求「可以加高宠物窗口」来给气泡腾空间，
    // 但实测加高**不增加头顶以上的空间**（画布贴窗口顶，多出来的高度只能落在
    // 下方），所以长回复在宠物窗口内必然压到脸。
    // 独立窗口的位置只受屏幕工作区约束，长回复也能完整显示。
    if (forwardToBubble(message)) {
      // 已由气泡窗口消费；不再往宠物窗口发（那里已经没有气泡了）
    } else {
      petWindow?.sendToRenderer('pet:message', message);
    }
  });

  socket.connect();
}

/**
 * 把一条消息发给大脑。**返回是否真的发出去了**。
 *
 * ⚠️ 之前是 `void`，把 `PetSocket.send()` 的返回值吞掉了（2026-10-09）。
 * 于是"连接断了"这件事对调用方完全不可见 —— 用户发了消息、界面毫无反应，
 * 而日志里也查不到（`PetSocket` 那条 `emit('log')` 根本没人接）。
 *
 * 现在透传返回值，调用方至少能提示用户"这条没发出去"。
 */
function send(message: ToBrainMsg): boolean {
  return socket?.send(message) ?? false;
}

/* --------------------------------------------------------------------------- *
 * 请求 - 等待回执
 *
 * 大部分下行消息是"推送"（宠物说话、状态变化），主进程不需要回应。
 * 但少数操作**只有 Python 能做**、而且必须给用户一个明确结果：
 *
 *   · 备份记忆库 —— 必须用 `VACUUM INTO`，因为只有 Python 持有打开的数据库连接；
 *     Electron 侧直接复制文件可能在 WAL 写入时拿到半个库。
 *
 * 于是用一个小的 reqId -> resolver 映射：请求带上 reqId，Python 回执原样带回。
 * 超时则 resolve(null)，调用方自己决定怎么说"没成功"。
 * --------------------------------------------------------------------------- */

interface PendingReply {
  resolve: (value: Record<string, unknown>) => void;
  timer: NodeJS.Timeout;
}

const pendingReplies = new Map<string, PendingReply>();

function requestFromBrain(
  message: Record<string, unknown>,
  timeoutMs = 15_000,
): Promise<Record<string, unknown> | null> {
  if (!socket) return Promise.resolve(null);
  const requestId = `r${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
  return new Promise((resolve) => {
    const timer = setTimeout(() => {
      pendingReplies.delete(requestId);
      log(`请求超时未收到回执：${String(message.type)}（requestId=${requestId}）`);
      resolve(null);
    }, timeoutMs);
    pendingReplies.set(requestId, { resolve, timer });
    /*
     * ⚠️ 字段名必须是 **`requestId`（驼峰）**，不能叫 `reqId`。
     *
     * 踩过：我一开始用的是 `reqId`，而 Python 侧的 `parse_incoming` 从
     * `obj["requestId"]` 取这个值（协议里已经有 `requestId` 这个惯例，见 user.text）。
     * 两边名字不一致 -> Python 回执里带的是空串 -> 这里永远匹配不到等待者 ->
     * **20 秒后超时**，用户看到的是"备份失败：大脑没有响应"。
     *
     * 这种错**不会报错、只会超时**，所以对齐字段名比加错误处理更重要。
     */
    socket?.send({ ...message, requestId } as unknown as ToBrainMsg);
  });
}

// --------------------------------------------------------------------------- //
// 截图对话（2026-10-17 用户要求）
// --------------------------------------------------------------------------- //

/**
 * 让宠物看一眼屏幕并顺着画面聊天。
 *
 * 用户原话：「点击按钮或者按下快捷键，屏幕将自动截图并发给ai，
 *   接受图片后，玩伴的注意力会直接被拉到好奇状态，玩伴会顺着图片和用户聊天。」
 *
 * ## 为什么这里只发一条消息
 *
 * 抓屏、看懂、拉注意力、开口**全在 sidecar**（见 `screen_chat.py` 与
 * `Gateway._snap_and_chat`）。放这边做的话，视觉模型调用、主观层、
 * 提示词、TTS 全都要在 TS 里再实现一遍 —— 那就有了两个真相源。
 *
 * ## 为什么要有 `snapInFlight`
 *
 * 看图要 1~15 秒（模型不同差很多）。用户在等待期间再按一次很常见
 * （"怎么没反应？"），并发两张截图会让它连着说两句，而且后一张可能
 * 覆盖前一张的结果。所以按着不放/连按都只跑一次。
 */
let snapInFlight = false;

async function snapshotChat(): Promise<void> {
  if (snapInFlight) {
    log('截图对话：上一次还在进行中，本次忽略');
    return;
  }
  if (paused) {
    petWindow?.sendToRenderer('pet:toast', {
      level: 'warn',
      text: '免打扰中，先取消免打扰再让它看屏幕',
    });
    return;
  }
  if (!socket) {
    petWindow?.sendToRenderer('pet:toast', {
      level: 'error',
      text: '和大脑的连接断了，没法截图对话',
    });
    return;
  }

  snapInFlight = true;
  // 立刻给一个可见反馈：抓屏 + 看图要几秒，没有反馈就像没反应
  petWindow?.sendToRenderer('pet:toast', { level: 'info', text: '看一眼屏幕…' });
  log('截图对话：开始');
  try {
    /*
     * 超时给到 60 秒。看图本身 1~15 秒，但首次请求要建连 + 模型冷启动，
     * 35 秒这个量级实测出现过（大模型 + 高清截图）。给短了会得到
     * "截图失败：大脑没有响应"，而其实是**算完了没人收**。
     */
    const reply = await requestFromBrain({ type: 'vision.snap' }, 60_000);
    if (!reply) {
      log('截图对话：没收到回执（超时）');
      petWindow?.sendToRenderer('pet:toast', {
        level: 'error',
        text: '看图超时了，再试一次？',
      });
      return;
    }
    const ok = Boolean(reply.ok);
    const message = String(reply.message ?? '');
    log(`截图对话：${ok ? '成功' : '失败'}（源=${String(reply.source ?? '?')}）${message}`);
    if (!ok && message) {
      petWindow?.sendToRenderer('pet:toast', { level: 'warn', text: message });
    }
  } catch (error) {
    log(`截图对话异常：${(error as Error).message}`);
    petWindow?.sendToRenderer('pet:toast', {
      level: 'error',
      text: `截图对话出错：${(error as Error).message}`,
    });
  } finally {
    snapInFlight = false;
  }
}

// --------------------------------------------------------------------------- //
// 渲染层 IPC
// --------------------------------------------------------------------------- //

function registerRendererIpc(): void {
  // 渲染层把用户输入交给主进程转发
  ipcMain.on('pet:user-text', (_event, text: string) => {
    if (typeof text !== 'string' || !text.trim()) return;
    if (paused) {
      log(`用户消息被免打扰挡下（${text.length} 字）`);
      petWindow?.sendToRenderer('pet:toast', { level: 'warn', text: '免打扰中，先取消免打扰再聊' });
      return;
    }
    /*
     * ⚠️ 记录**发送结果**（2026-10-09）。
     *
     * `send()` 在未连接时静默丢弃并只 emit 一条 log（那条 log 没人接），
     * 于是"用户发了消息但什么都没发生"会**完全无迹可查** ——
     * 排查时我只能在链路上反复猜。
     *
     * 用户消息是最重要的事件，必须留下到达记录。
     */
    const ok = send({ type: 'user.text', text });
    log(`用户消息 -> 大脑：${ok ? '已发送' : '❌ 未发送（连接断了）'}（${text.length} 字）`);
    if (!ok) {
      petWindow?.sendToRenderer('pet:toast', {
        level: 'error',
        text: '和大脑的连接断了，这条没发出去',
      });
    }
  });

  ipcMain.on('pet:interrupt', () => send({ type: 'user.interrupt' }));

  ipcMain.on('pet:poke', () => {
    if (paused) {
      petWindow?.sendToRenderer('pet:toast', { level: 'warn', text: '免打扰中，它现在不想说话' });
      return;
    }
    send({ type: 'user.poke' });
  });

  // 语音（P3）：渲染层负责采集，主进程只负责转发。
  // 音频走二进制帧，避免 base64 带来的体积与两次编解码开销。
  ipcMain.on('pet:voice-enabled', (_event, enabled: boolean) => {
    /*
     * 记下"麦克风该不该开"（2026-10-10 加）。
     *
     * `socket.on('open')` 要拿它补报给 sidecar —— 见那里的说明。
     * 渲染层的 `voiceOn` 主进程看不见，所以这里必须自己留一份。
     */
    micWantedOn = Boolean(enabled);
    send({ type: 'voice.control', enabled: Boolean(enabled) });
  });

  ipcMain.on('pet:mic-audio', (_event, payload: ArrayBuffer | Uint8Array) => {
    if (!payload) return;
    const buffer = Buffer.isBuffer(payload) ? payload : Buffer.from(payload as ArrayBuffer);
    if (buffer.length === 0) return;
    socket?.sendBinary(buffer);
  });

  /**
   * 按住说话的句子边界（宠物窗口 -> sidecar）。
   *
   * ⚠️ 这里必须**立刻转发**，不能攒着：`phase: 'end'` 的意思是"我说完了"，
   * 迟到几百毫秒就等于用户要多等几百毫秒才看到宠物开始反应。
   */
  ipcMain.on('pet:voice-ptt', (_event, payload: { phase?: string }) => {
    const phase = payload?.phase === 'end' ? 'end' : 'start';
    /*
     * 🔍 记一条（2026-10-10 加）。
     *
     * ## 为什么要记
     *
     * 用户报「按住 alt 按键没用」时，`main.log` 里出现过**孤立的 `end`**
     * （没有对应的 `start`）：
     *
     *     19:35:20,618  按住说话：收到 phase='end'
     *                   松开收尾没拿到句子 —— 门态=idle 已攒语音=0ms
     *     19:35:23,295  按住说话：收到 phase='start'      <- 3 秒后才来
     *
     * 孤立的 `end` 会把闸门关掉，**如果此时用户正在说话，音频就白说了**。
     *
     * 主进程的键监视器有 `if (!this.down) return` 去重，**不可能**发出孤立的
     * `UP`（见 `pushToTalk.ts`）。所以那条 `end` 只能来自**渲染层**——
     * 而渲染层有四个地方会调 `applyCaptureGate()`（预热后 / 开麦后 /
     * 收到配置时 / 按下松开时），每一个都可能发 `stop`。
     *
     * 这条日志把"渲染层主动发的 stop"和"真实按键松开"分开：
     * 后者前面一定有一行「按住说话 按下」。
     */
    startupNote(`[按住说话] 渲染层发来 ${phase}`);
    send({ type: 'voice.ptt', phase });
  });

  /*
   * 语音开关：**设置页点 -> 宠物窗口执行**（2026-10-07 接上）。
   *
   * ⚠️ 为什么不能让设置页自己开麦克风：
   *   `getUserMedia` 要在有界面的那个窗口里调，而麦克风的生命周期应当跟
   *   **宠物**走（设置页关了，语音不该跟着停）。所以设置页只发"我要开"，
   *   真正的 `VoiceIO.start()` 在宠物窗口执行。
   *
   * ⚠️ 那条消息进宠物窗口后，`setVoice()` 会**同时**做两件事：
   *   打开麦克风 + 发 `voice.control` 给 sidecar。见 `app.js` 的 `setVoice`。
   */
  ipcMain.on('settings:set-voice', (_event, enabled: boolean) => {
    petWindow?.sendToRenderer('pet:set-voice', { enabled: Boolean(enabled) });
  });

  /*
   * ------------------------------------------------------- 麦克风选择
   *
   * 用户反馈「我用 usb 的麦克风说话好像没用」（2026-10-08）。
   *
   * ## ⚠️ 为什么不能让设置页自己枚举设备
   *
   * 枚举 `enumerateDevices()` 需要**麦克风权限**，而权限是按"窗口"给的 ——
   * 麦克风只在**宠物窗口**里开过，设置页没有授权，枚举出来会是空 label
   * （甚至空列表）。所以由设置页**请求**、宠物窗口**执行枚举**、结果回传。
   *
   * 这条路径和语音开关、按住说话是同一套理由：
   * **碰硬件的事都在宠物窗口做**（它的生命周期最长、且已经握有权限）。
   */
  ipcMain.handle('settings:list-mics', async () => {
    if (!petWindow) return { ok: false, mics: [], message: '宠物窗口还没就绪' };
    const mics = await petWindow.evaluate<unknown[]>('window.__listMics ? window.__listMics() : []');
    if (mics === null) {
      return { ok: false, mics: [], message: '宠物窗口没有响应（它可能还在启动）' };
    }
    return { ok: true, mics: Array.isArray(mics) ? mics : [] };
  });

  /**
   * 选定麦克风（来自设置页）。
   *
   * ⚠️ **委派给 `setMicDevice()`**，不要在这儿再写一遍。
   *
   * 之前这里和 `setMicDevice()` 是两份**几乎一样但会分叉**的实现
   * （落盘、日志、通知渲染层、同步托盘）—— 托盘那一步就漏了。
   * 这个项目已经因为"两个真相源"出过快捷键的事故（见 `petMenu.ts` 里
   * ACTIONS_WITH_VALUE 的说明），所以这里收敛成一个入口。
   */
  ipcMain.on('settings:set-mic', (_event, deviceId: string) => setMicDevice(String(deviceId ?? '')));

  /**
   * 宠物窗口回报语音的**真实状态** -> 转给设置页显示。
   *
   * 失败原因（权限被拒 / 没有设备 / 被占用）必须让用户看见 ——
   * 否则"开了没反应"没法排查。
   */
  ipcMain.on('pet:voice-state', (_event, payload: { on?: boolean; state?: string; detail?: string }) => {
    lastVoiceState = {
      on: Boolean(payload?.on),
      state: String(payload?.state || 'unknown'),
      detail: String(payload?.detail || ''),
    };
    startupNote(`[语音] ${lastVoiceState.state} ${lastVoiceState.detail}`.trim());
    settingsWindow?.send('settings:voice-state', lastVoiceState);
  });

  /*
   * 渲染层报「一段语音播完了」（2026-10-16 加）。
   *
   * 「说话 → 空闲」的终点信号：渲染层 `<AudioBufferSource>.onended`
   * （voice-io.js）-> `pet.notifyVoicePlayed()` -> 这里。
   * 只在状态条确实是"说话"时才清 —— 别的时机收到都不动
   * （比如打断后残留的 onended、或朗读按钮那类不经状态机的播放）。
   */
  ipcMain.on('pet:voice-played', () => {
    if (voiceSpeaking) {
      log('[状态条] 语音播放结束 -> 空闲');
      clearActivityStatus();
    }
  });

  /*
   * 渲染层报「一段语音**开始播放**」（2026-10-17 加）。
   *
   * 「说话中」的起点信号，**无条件**转说话中：渲染层是真正播音频的地方，
   * 它说了算。原来靠主进程在转发点猜时机（`turnThinking` 状态时序），
   * 实测会漏 —— 音频在播状态条还挂「思考中」（用户截图报）。
   * 终点信号是上面的 `pet:voice-played`（onended）。
   */
  ipcMain.on('pet:voice-speaking', () => {
    voiceSpeaking = true;
    turnThinking = false;
    setStatus('说话中');
    log('[状态条] 渲染层开始播放 -> 说话中（青）');
  });

  /** 设置页打开时问一次当前状态（否则开关显示的是默认值，不是真实值） */
  ipcMain.handle('settings:voice-state', () => lastVoiceState);

  /*
   * ---------------------------------------------------------- 按住说话（P3）
   *
   * 用户要求：「设置隐私里面设置自定义按键，**按下按键发声，停下结束**」。
   *
   * ## ⚠️ 为什么不能用 globalShortcut
   *
   * Electron 的 `globalShortcut.register` **只有按下、没有松开** —— 而"按住
   * 说话"的核心语义就是**按下开始录、松开结束**。所以走一个常驻的
   * PowerShell 轮询 `GetAsyncKeyState`（见 `pushToTalk.ts` 与
   * `tools/probe-key.ps1`）。
   *
   * ## 它是**可选**功能
   *
   * 监视器起不来（找不到脚本 / PowerShell 被策略挡住 / 非 Windows）
   * **不该影响别的功能** —— 只记日志并在设置页显示原因。这与
   * `fullscreenWatcher` 是同一条取舍。
   */
  ipcMain.handle('settings:ptt-config', () => {
    const vk = Number(
      readDeepScalar(CONFIG_ROOT, ['providers', 'push_to_talk', 'vk'], true) ?? DEFAULT_PTT_VK,
    );
    /*
     * **独立的全局键**（不聚焦时的切换式）。
     *
     * ⚠️ 它和 `vk` 是**两个不同的键**，设置页必须分别显示/捕获：
     *
     *     vk        = 聚焦时按住说（可以是右 Alt 这类修饰键）
     *     globalVk  = 不聚焦时切换（**必须普通键**，accelerator 表达不了修饰键）
     *
     * 默认 F9 —— 实测 F9 能注册成功，而且平时打字/游戏都不会按到。
     */
    const globalVk = Number(
      readDeepScalar(CONFIG_ROOT, ['providers', 'push_to_talk', 'global_vk'], true) ?? 0x78,
    );
    return {
      enabled: Boolean(readDeepScalar(CONFIG_ROOT, ['providers', 'push_to_talk', 'enabled'], true)),
      vk,
      /** 给人看的名字（界面不用自己维护 VK 表） */
      name: vkName(vk),
      globalVk,
      globalName: vkName(globalVk),
      /** 监视器当前是否真的在跑 —— 界面要能看出"配置开了但没生效" */
      running: (focusedPtt?.running ?? false) || (globalPtt?.running ?? false),
      /** 分开报，界面才能说清"哪一条生效了、哪一条没生效" */
      focusedRunning: focusedPtt?.running ?? false,
      globalRunning: globalPtt?.running ?? false,
    };
  });

  ipcMain.handle(
    'settings:set-ptt',
    (_event, payload: { enabled?: boolean; vk?: number; globalVk?: number }) => {
      const patch: { enabled?: boolean; vk?: number; globalVk?: number } = {};
      if (typeof payload?.enabled === 'boolean') patch.enabled = payload.enabled;
      if (Number.isFinite(Number(payload?.vk))) {
        const vk = Math.trunc(Number(payload?.vk));
        // 范围守卫：0 表示"未设置"；超出 0..255 的不是合法 VK
        if (vk >= 0 && vk <= 0xff) patch.vk = vk;
      }
      if (Number.isFinite(Number(payload?.globalVk))) {
        const gvk = Math.trunc(Number(payload?.globalVk));
        // 0 表示"不要全局键"（合法，见上面的处理）
        if (gvk >= 0 && gvk <= 0xff) patch.globalVk = gvk;
      }
      const result: Record<string, unknown> = { ok: true };
      if (patch.enabled !== undefined) {
        const r = writeDeepScalar(CONFIG_ROOT, ['providers', 'push_to_talk', 'enabled'], patch.enabled);
        if (!r.ok) result.ok = false;
      }
      if (patch.vk !== undefined) {
        const r = writeDeepScalar(CONFIG_ROOT, ['providers', 'push_to_talk', 'vk'], patch.vk);
        if (!r.ok) result.ok = false;
      }
      if (patch.globalVk !== undefined) {
        const r = writeDeepScalar(CONFIG_ROOT, ['providers', 'push_to_talk', 'global_vk'], patch.globalVk);
        if (!r.ok) result.ok = false;
      }
      // 改完立刻重启 —— 否则用户要重启应用才生效（那很像"设置没保存"）
      restartPushToTalk();
      const vkNow = Number(readDeepScalar(CONFIG_ROOT, ['providers', 'push_to_talk', 'vk'], true) ?? 0);
      const gvkNow = Number(
        readDeepScalar(CONFIG_ROOT, ['providers', 'push_to_talk', 'global_vk'], true) ?? 0,
      );
      result.enabled = Boolean(readDeepScalar(CONFIG_ROOT, ['providers', 'push_to_talk', 'enabled'], true));
      result.vk = vkNow;
      result.name = vkName(vkNow);
      result.globalVk = gvkNow;
      result.globalName = vkName(gvkNow);
      result.running = (focusedPtt?.running ?? false) || (globalPtt?.running ?? false);
      result.focusedRunning = focusedPtt?.running ?? false;
      result.globalRunning = globalPtt?.running ?? false;
      return result;
    },
  );

  /**
   * 试听用：让设置页能"按一下试试"识别到的键码对不对。
   *
   * ⚠️ 这个功能**其实不需要主进程参与**：设置页聚焦时按下一个键，
   * `KeyboardEvent.keyCode` **就是 Windows 虚拟键码**（Chromium 在
   * Windows 上直接用它），设置页自己换算即可。
   *
   * 曾想在这里起一个临时监视器来"证实"，但那是多余的 —— 全局监视器与
   * 页面内监听的差异只在"窗口有没有聚焦"，而用户配置按键时**设置页
   * 必然是聚焦的**。少一层进程，少一处能出错的地方。
   */


  // 清空长期记忆。走 sidecar 而不是前端自己删文件 —— 记忆库归大脑管，
  // 前端不该知道它存在哪里，否则两边的认知会分叉。
  ipcMain.on('pet:memory-clear', () => {
    send({ type: 'memory.clear' });
  });

  /** 宠物界面上的「设置」按钮 */
  ipcMain.on('pet:open-settings', () => openSettings());

  /**
   * 渲染层运行时量到的**立绘包围盒** -> 交给 petWindow 做自适应。
   *
   * 贴边判定与"拖拽放宽多少"都依赖"窗口边到立绘可见边"的距离。
   * 这个距离以前靠主进程的测量常数推算，布局一改就不准（用户报的
   * "调完大小留白变了"）。现在以渲染层实测为准，缩放/换素材自适应。
   */
  ipcMain.on('pet:art-box', (_event, payload: { width?: unknown; height?: unknown }) => {
    const width = Number(payload?.width);
    const height = Number(payload?.height);
    if (!Number.isFinite(width) || !Number.isFinite(height) || width < 8 || height < 8) return;
    petWindow?.setMeasuredArtSize(width, height);
  });

  /** 宠物本体右键 -> 原生上下文菜单（结构见 petMenuTemplate.ts） */
  ipcMain.on('pet:context-menu', () => openPetMenu());

  /* ── 自绘菜单窗口的 IPC（2026-10-14）────────────────────────────────
   *
   * 三条通道，全部来自 `preload/menu.ts`：
   *
   *   menu:layout  渲染层量完算好的窗口矩形 -> **主进程只 setBounds + 显示**
   *   menu:action  某一行被点了（`action` 字符串 + 开关类的新状态）
   *   menu:close   渲染层请求关闭（Esc / 点完某项）
   *
   * ⚠️ 通道名必须与 `preload/menu.ts` 里的 CHANNEL **逐字一致** ——
   * 拼错的表现是"菜单能出来但点了没反应"，而这个项目已经踩过
   * 通道名漂移导致静默失效的坑。
   */
  ipcMain.on('menu:layout', (_event, rect: { x?: unknown; y?: unknown; width?: unknown; height?: unknown }) => {
    petMenuWindow?.handleLayout(rect ?? {});
  });

  ipcMain.on('menu:action', (_event, payload: Partial<PetMenuAction>) => {
    const action = typeof payload?.action === 'string' ? payload.action : '';
    if (!action) return;
    const handlers = petMenuHandlers;
    if (!handlers) {
      log(`[菜单] 收到点击但还没有处理器表（action=${action}）`);
      return;
    }
    const checked = typeof payload?.checked === 'boolean' ? payload.checked : undefined;
    /*
     * 找不到处理器时记一条 —— 那说明菜单里出现了"点了没反应"的项。
     * `petMenu.ts` 那边会把这类项**置灰**（自绘菜单里同样会灰掉），
     * 但仍然应该让人知道，否则永远没人去修。
     */
    if (!dispatchPetMenuAction(handlers, action, checked)) {
      log(`[菜单] 这个动作没有处理器：${action}`);
    }
  });

  ipcMain.on('menu:close', () => petMenuWindow?.hide());

  /**
   * 宠物窗口转告的状态文本 -> 对话条上的小圆点。
   *
   * 状态由主进程统一收敛（`statusText`），两个窗口都不自己推导一套 ——
   * 这个项目在点击穿透上踩过"两个决定者"的坑，状态上不重演。
   */
  /**
   * 渲染层**第一次**上报状态 = 它真的活着（`app.js` 已求值、监听已挂好）。
   *
   * 🔴 用它来推「按住说话」配置（2026-10-10 修用户报的 bug）。
   *
   * ## 为什么不能用 `ready-to-show`
   *
   * 实测：`ready-to-show`（首次绘制）**可能早于模块脚本求值完成**，
   * 而 `app.js` 是 `<script type="module">`（延迟执行）—— 那一刻
   * `pet.on('pet:ptt-config')` 还没挂上，而 `webContents.send`
   * **不排队**，消息直接丢。证据：
   *
   *     startup.log  [按住说话] 已把开关状态推给渲染层：开
   *     python.log   采集闸门 …     <- 0 条，闸门从来没被关过
   *
   * 渲染层自己上报的那一刻，它的监听一定挂好了 —— 这是**确定的**信号，
   * 比"猜某个窗口事件够不够晚"可靠。
   *
   * ⚠️ `status-report` 会**周期性**发（状态文字变一次发一次），
   * 所以加 `pttConfigPushed` 标记只推一次。
   */
  let pttConfigPushed = false;

  ipcMain.on('pet:status-report', (_event, payload: { text?: unknown; level?: unknown }) => {
    if (!pttConfigPushed) {
      pttConfigPushed = true;
      const pttEnabled = Boolean(
        readDeepScalar(CONFIG_ROOT, ['providers', 'push_to_talk', 'enabled'], true),
      );
      petWindow?.sendToRenderer('pet:ptt-config', { enabled: pttEnabled });
      startupNote(
        `[按住说话] 渲染层已就绪，把开关状态推过去：${
          pttEnabled ? '开（只有按住时才听）' : '关（免提常听）'
        }`,
      );
    }

    const text = String(payload?.text ?? '').trim();
    if (!text) return;
    statusText = text;
    syncChatStatus();
  });

  // ---- 边缘探头参数（设置窗口里可调）----
  ipcMain.handle('settings:peek-offset', () =>
    petWindow ? petWindow.peekOffset : readSettings().peekOffsetPx,
  );
  ipcMain.handle('settings:set-peek-offset', (_event, payload: { value?: unknown }) => {
    const result = writeSettings({ peekOffsetPx: Number(payload?.value) });
    petWindow?.setPeekOffset(result.settings.peekOffsetPx);
    // 回给设置窗口"真正生效的值"（可能被夹到范围内），
    // 否则滑杆显示的数和实际用的数会不一致
    return {
      peekOffsetPx: result.settings.peekOffsetPx,
      path: result.path,
      // 落盘失败必须让界面知道 —— 否则会变成"看起来保存了、重启却回默认"
      saved: result.ok,
      error: result.error,
    };
  });

  // ---- 语言层 / 性格层旋钮（2026-10-22）----
  //
  // 用户要求：「改为设置语言的表达意思，和情绪等主观层并列」。
  //
  // ## 两段分开走，但用同一套读写
  //
  //   expression   语言层  修辞 / 文化 / 词汇 / 温度·关心
  //   personality  性格    感性·理性（**属于主观层**，用户明确纠正过）
  //
  // 所以键名前缀决定写到 `persona.yaml` 的哪一段 —— 不做合并，
  // 免得以后有人以为"感性/理性是表达技巧"。
  //
  // ## 为什么用"读全量 + 写单个"而不是一个大 patch
  //
  // 滑杆是**边拖边存**的（和活泼度同一条规矩：改完立刻生效、重启也记住）。
  // 每次只写一个键，`writeScalar` 会就地改那一行，注释全都保留 ——
  // 而"读全量"让设置页一次拿到所有值回显，不用发 5 个 IPC。
  ipcMain.handle('settings:expression', () => readStyleKnobs());
  ipcMain.handle(
    'settings:set-expression',
    (_event, payload: { section?: unknown; key?: unknown; value?: unknown }) => {
      return writeStyleKnob(payload?.section, payload?.key, payload?.value);
    },
  );

  // ---- 文字弹窗存在时间（默认 10 秒）----
  //
  // 与 peek-offset 同一套模式：**改一下立刻生效 + 落盘**，
  // 所以单独一对 IPC，而不是等用户点"保存"。
  //
  // ⚠️ 读接口返回的是**数字（毫秒）**，不是对象。
  // 渲染层的 `bindLiveRange` 会做 `Number(await read())` —— 返回对象会变成
  // `NaN`，滑杆停在默认值上（"设置里显示的不是存的值"这种假象）。
  // 范围常量在渲染层是写死的滑杆 min/max，不需要从这里传。
  ipcMain.handle(
    'settings:bubble-linger',
    () => petWindow ? petWindow.bubbleLinger : readSettings().bubbleLingerMs,
  );
  ipcMain.handle('settings:set-bubble-linger', (_event, payload: { value?: unknown }) => {
    const result = writeSettings({ bubbleLingerMs: Number(payload?.value) });
    petWindow?.setBubbleLinger(result.settings.bubbleLingerMs);
    return {
      bubbleLingerMs: result.settings.bubbleLingerMs,
      saved: result.ok,
      path: result.path,
      error: result.error,
    };
  });

  // ---- 气泡摆放参数（上方间距 / 左右间距 / 立绘贴合度，2026-10-18）----
  // 改完立刻注入 bubbleWindow（当前挂着的气泡下次重摆就生效），并落盘。
  ipcMain.handle('settings:bubble-gap-above', () => readSettings().bubbleGapAbovePx);
  ipcMain.handle('settings:set-bubble-gap-above', (_event, payload: { value?: unknown }) => {
    const result = writeSettings({ bubbleGapAbovePx: Number(payload?.value) });
    bubbleWindow?.setPlacementTuning({ gapAbove: result.settings.bubbleGapAbovePx });
    return {
      bubbleGapAbovePx: result.settings.bubbleGapAbovePx,
      saved: result.ok,
      path: result.path,
      error: result.error,
    };
  });

  ipcMain.handle('settings:bubble-gap-side', () => readSettings().bubbleGapSidePx);
  ipcMain.handle('settings:set-bubble-gap-side', (_event, payload: { value?: unknown }) => {
    const result = writeSettings({ bubbleGapSidePx: Number(payload?.value) });
    bubbleWindow?.setPlacementTuning({ gapSide: result.settings.bubbleGapSidePx });
    return {
      bubbleGapSidePx: result.settings.bubbleGapSidePx,
      saved: result.ok,
      path: result.path,
      error: result.error,
    };
  });

  ipcMain.handle('settings:bubble-core-shrink', () => readSettings().bubbleCoreShrinkPct);
  ipcMain.handle('settings:set-bubble-core-shrink', (_event, payload: { value?: unknown }) => {
    const result = writeSettings({ bubbleCoreShrinkPct: Number(payload?.value) });
    bubbleWindow?.setPlacementTuning({ coreShrink: result.settings.bubbleCoreShrinkPct });
    return {
      bubbleCoreShrinkPct: result.settings.bubbleCoreShrinkPct,
      saved: result.ok,
      path: result.path,
      error: result.error,
    };
  });

  // ---- 气泡段间距（段落之间留多宽的缝，2026-10-17）----
  // 读接口返回数字（px），渲染层 bindNumberField 直接吃。
  ipcMain.handle('settings:bubble-para-gap', () => readSettings().bubbleParaGap);
  ipcMain.handle('settings:set-bubble-para-gap', (_event, payload: { value?: unknown }) => {
    const result = writeSettings({ bubbleParaGap: Number(payload?.value) });
    // 立刻推进气泡窗口（不等下一条消息）—— 当前正挂着的长回复也要跟着变
    bubbleWindow?.setParaGap(result.settings.bubbleParaGap);
    return {
      bubbleParaGap: result.settings.bubbleParaGap,
      saved: result.ok,
      path: result.path,
      error: result.error,
    };
  });

  // ---- 贴边判定距离（靠近边缘多少像素自动侧身）----
  ipcMain.handle('settings:edge-snap', () => petWindow ? petWindow.edgeSnap : readSettings().edgeSnapPx);
  ipcMain.handle('settings:set-edge-snap', (_event, payload: { value?: unknown }) => {
    const result = writeSettings({ edgeSnapPx: Number(payload?.value) });
    petWindow?.setEdgeSnap(result.settings.edgeSnapPx);
    return { edgeSnapPx: result.settings.edgeSnapPx, path: result.path, saved: result.ok, error: result.error };
  });

    // ---- 「自言自语」持续多久（2026-10-17 用户要求：0~3 小时，设置里调）----
    //
    // 用户原话：「将自言自语的回落到安静的时间设置成可设置吧，0-3小时，放在设置里面。」
    //
    // ## 单位：界面**分钟**，存储**秒**；换算只在这一层
    //
    // yaml 里是 `activity.monologue_decay_s`（秒）。渲染层只说分钟 ——
    // 换算放两处就会重演 `bubble-linger` 的"毫秒当秒用、界面显示 10000 秒"。
    //
    // ## 0 的含义是**不回落**
    //
    // `tiers.decay_due()` 里 `decay_s <= 0` 直接返回 False。界面的说明文字
    // 必须写清这一点，否则用户会以为"0 分钟 = 立刻回落到安静"，正好反了。
    ipcMain.handle('settings:monologue-decay', () => {
      // ⚠️ 第三个参数是 strictAnchors（布尔），不是默认值 —— 早先在这里
      // 传了 1200，类型检查当场报 TS2345。缺省/非法一律按 1200 秒（20 分钟）。
      const raw = readDeepScalar(CONFIG_ROOT, ['activity', 'monologue_decay_s']);
      const parsed = typeof raw === 'number' ? raw : Number(raw);
      const seconds = Number.isFinite(parsed) ? parsed : 1200;
      return Math.max(0, Math.min(180, Math.round(seconds / 60)));
    });

    ipcMain.handle('settings:set-monologue-decay', (_event, payload: { minutes?: unknown }) => {
      const raw = Number(payload?.minutes);
      // 夹到 0~180 分钟（用户要求的 0~3 小时）
      const minutes = Number.isFinite(raw) ? Math.max(0, Math.min(180, Math.round(raw))) : 20;
      const seconds = minutes * 60;
      const result = writeScalar(CONFIG_ROOT, 'activity', 'monologue_decay_s', seconds);
      if (!result.ok) {
        log(`设置：「自言自语」时长保存失败：${result.message}`);
        return { minutes, saved: false, ok: false, error: result.message };
      }
      // 热更新：scheduler 每次回落检查都现读这个值，立刻生效
      send({ type: 'config.update', patch: { activity: { monologue_decay_s: seconds } } });
      log(`设置：「自言自语」时长 -> ${minutes} 分钟${minutes === 0 ? '（不回落）' : ''}`);
      return { minutes, saved: true, ok: true, path: result.path };
    });

    // ---- 截图对话：念出"它看到了什么"（2026-10-17 用户要求，设置页开关）----
    //
    // 用户原话：「这个看看它看到了什么的按钮就是开启后，截图对话传来的第一次
    //   回复会控制在api说话的最后分一段带上图片的信息就行，这个按钮放在设置里面开启。」
    //
    // ## 真源是 `config/persona.yaml` 的 `screenshot.show_description`
    //
    // ⚠️ **读**直接读文件（`readDeepScalar`），**写**必须落盘（`writeScalar`）。
    //
    // 为什么读文件而不是读 sidecar 内存：内存里的值可能被某次 `config.update`
    // 改过却没落盘 —— 那时界面显示"开着"、重启却是"关着"。
    // 为什么写必须落盘：`config.update` 只改内存，sidecar 重启会
    // `reload_from_disk()` 回到 yaml。和「允许看屏幕」当年是同一个坑。
    ipcMain.handle('settings:snapshot-desc', () =>
      Boolean(readDeepScalar(CONFIG_ROOT, ['screenshot', 'show_description'], false)),
    );

    ipcMain.handle('settings:set-snapshot-desc', (_event, payload: { enabled?: unknown }) => {
      const enabled = Boolean(payload?.enabled);
      const result = writeScalar(CONFIG_ROOT, 'screenshot', 'show_description', enabled);
      if (!result.ok) {
        log(`设置：截图对话"它看到了什么"保存失败：${result.message}`);
        return { showDescription: !enabled, saved: false, ok: false, error: result.message };
      }
      // 热更新：sidecar 里的 screenshot 段立刻生效，不用重启
      send({ type: 'config.update', patch: { screenshot: { show_description: enabled } } });
      log(`设置：截图对话"它看到了什么" -> ${enabled ? '开' : '关'}`);
      return { showDescription: enabled, saved: true, ok: true, path: result.path };
    });

  // ---- 边缘探头开关（菜单里也有，两处必须同步）----
  ipcMain.handle('settings:edge-peek', () => petWindow?.edgePeekEnabledValue ?? true);
  ipcMain.handle('settings:set-edge-peek', (_event, payload: { enabled?: boolean }) => {
    const enabled = Boolean(payload?.enabled);
    petWindow?.setEdgePeekEnabled(enabled);
    const result = writeSettings({ edgePeekEnabled: enabled });
    return { edgePeekEnabled: enabled, saved: result.ok, path: result.path, error: result.error };
  });

  // ---- 宠物大小（菜单里也有）----
  //
  // 返回**档位清单**而不是只返回当前值：设置页是个下拉框，选项必须跟
  // `petLayout.PET_SCALE_PRESETS` 完全一致。让渲染层自己写一份清单迟早漂移
  // （"代码里 4 档、界面上 5 档"这种）。
  ipcMain.handle('settings:pet-scale', () => ({
    scale: petWindow?.currentPetScale ?? 1,
    presets: PET_SCALE_PRESETS.map((p) => p.scale),
    // 顺便把像素值也给了，渲染层不用自己乘基准尺寸（免得两边各算一次）
    sizes: PET_SCALE_PRESETS.map((p) => petSizeFor(p.scale)),
  }));
  ipcMain.handle('settings:set-pet-scale', (_event, payload: { value?: unknown }) => {
    // ⚠️ 落盘**不在这里** —— `setPetScale` 是所有入口共用的那一份实现，
    // 它自己负责落盘并返回结果（见那里的说明：曾经只有这条 IPC 落盘，
    // 菜单/托盘改的大小重启就丢）。
    const applied = setPetScale(Number(payload?.value));
    return {
      petScale: applied.applied,
      saved: applied.saved,
      path: applied.path,
      error: applied.error,
    };
  });

  // ---- 活泼度的设置页 IPC 已删除（2026-10-17）----
  //
  // 设置页那个滑杆没了，改由**宠物右键菜单**的四档调（安静/无聊/好奇/自言自语）。
  // 下面这条 `pet:set-activity` 是菜单那条路，**保留**。
  //
  // ⚠️ 活泼度仍然**只改内存、不落盘**：真源是 `config/persona.yaml`，
  // 启动后会被 config 快照覆盖。落盘会造出"保存了但没用"的假象。

  ipcMain.on('pet:set-activity', (_event, value: number) => setActivity(Number(value)));

  // ---- 界面主题（设置页「常规」里的下拉框，2026-10-14 加）----
  //
  // 返回的是**当前值**，不是整份设置：设置页只需要知道选中哪一项。
  // 选中态的真源是主进程内存里的 `theme`（启动时从 settings.json 恢复），
  // 不让渲染层自己去读文件 —— 渲染层读不到，而且会多一份会漂移的状态。
  ipcMain.handle('settings:theme', () => ({ theme }));
  ipcMain.handle('settings:set-theme', (_event, payload: { value?: unknown }) => {
    /*
     * ⚠️ 非法值一律**当浅色**（不是"保持原样"）：
     * 这里只有两档，收到别的东西说明渲染层有 bug。当浅色至少是个确定、
     * 看得见的状态；"保持原样"会让一次误操作没有任何反馈。
     */
    const next = payload?.value === 'dark' ? 'dark' : 'light';
    return setTheme(next);
  });

  ipcMain.on('pet:set-paused', (_event, value: boolean) => {
    setPausedInternal(Boolean(value), 0);
  });

  ipcMain.handle('pet:get-state', () => ({
    activity,
    /** 当前状态键（2026-10-23）。空串 = 后端还没回报过 */
    petState,
    paused,
    status: statusText,
    clickThrough: petWindow?.clickThroughEnabled ?? true,
    logFile: currentLogFile(),
  }));
}

/**
 * 改宠物大小（菜单 / 托盘 / 设置页**共用这一份实现**）。
 *
 * 真正的尺寸计算在 PetWindow.setPetScale 里（窗口 + 画布一起变，右下角锚定）。
 * 这里还负责同步托盘的勾选状态 —— 托盘菜单每次重建，
 * 不更新的话勾选会停在旧的那一档。
 *
 * ## 🔴 落盘也在这里（2026-10-14 修）
 *
 * 用户报：「宠物大小我设置之后还是会回到 250px，用户设置好大小之后每次重启
 * 打开宠物按照上次选择的大小显示」。
 *
 * 根因：这个函数原来**只改内存** —— 只有设置页那条 IPC
 * （`settings:set-pet-scale`）在调完它之后**自己**补了一次 `writeSettings`，
 * 而菜单（handler `setPetScale`）和托盘（`onSetPetScale`）的入口是直接调
 * 本函数的，**谁都不落盘**。于是：
 *
 *     菜单里改成 1.4x  ->  内存变了、窗口变了、settings.json 里还是 1
 *     重启              ->  ready-to-show 读回 1  ->  回到基准尺寸（250px）
 *
 * 修法把落盘放进这个**唯一实现**里，三处入口自动一致 ——
 * 以后新增入口也不会再漏（这正是"两处实现分叉"类 bug 的根治方式）。
 *
 * 返回值给设置页 IPC 用：它要把 `saved` / `error` 如实报给界面
 * （用户明确要求过这类"保存了没有"必须诚实）。
 */
function setPetScale(scale: number): {
  applied: number;
  saved: boolean;
  path?: string;
  error?: string;
} {
  const current = petWindow?.currentPetScale ?? 1;
  if (!Number.isFinite(scale)) return { applied: current, saved: true };

  petWindow?.setPetScale(scale);
  const applied = petWindow?.currentPetScale ?? scale;
  tray?.setPetScale(applied);

  /*
   * 盘上已经是这个值时**不重复写**：启动恢复（`ready-to-show`）也走本函数，
   * 每次启动都白写一遍既没必要、也会让"保存成功"的日志变噪音。
   */
  if (readSettings().petScale === applied) return { applied, saved: true };

  const result = writeSettings({ petScale: applied });
  if (!result.ok) {
    log(`宠物大小落盘失败：${result.error ?? '未知原因'}（路径 ${result.path}）`);
  } else {
    log(`宠物大小：${applied.toFixed(2)}x 已保存`);
  }
  return { applied, saved: result.ok, path: result.path, error: result.error };
}

/**
 * 对话条窗口的 IPC。
 *
 * 为什么单独一个函数：对话条的 preload 与宠物窗口的**不是同一份**
 * （最小授权，见 preload/chat.ts），所以它的通道要单独注册，
 * 不能指望宠物窗口那套处理器顺手接管。
 */
function registerChatIpc(): void {
  // 用户从对话条发文字
  ipcMain.on('chat:user-text', (_event, payload: { text?: unknown }) => {
    const text = String(payload?.text ?? '').trim();
    if (!text) return;
    if (paused) {
      petWindow?.sendToRenderer('pet:toast', { level: 'warn', text: '免打扰中，它现在不想说话' });
      return;
    }
    send({ type: 'user.text', text });
  });

  // 前端问"现在什么状态"（小圆点着色用）
  ipcMain.handle('chat:status', () => statusText);
}

/**
 * 同步运行状态给对话条上的小圆点。
 *
 * 状态文本由主进程统一收敛（`statusText`），渲染层只负责显示 ——
 * 两边各推导一套"连上没有"的逻辑必然会在某个边界上不一致。
 */
function syncChatStatus(): void {
  chatWindow?.sendStatus(statusText);
}

/** 把一条下行消息转给对话条窗口（回复、错误、toast 都要到那边去） */
function forwardToChat(channel: string, payload: unknown): void {
  chatWindow?.send(channel, payload);
}

/**
 * 弹出宠物本体的右键菜单。
 *
 * 结构照抄参考桌宠应用（见 petMenu.ts 的说明），**未实现的功能置灰**，
 * 而不是隐藏或做成"点了没反应"。
 *
 * ## ⚠️ 它是 `async` 的：麦克风列表要**现枚举**（2026-10-08）
 *
 * 用户要求「麦克风选项移到宠物菜单里面」。而枚举 `enumerateDevices()` 是
 * 异步的（而且必须由**宠物窗口**做 —— 麦克风权限在它那儿）。
 *
 * 所以流程是：**先把设备问出来，再拼菜单**。
 * - 用的是带缓存的 `refreshMicDevices()`：**枚举失败就退回上次的列表**，
 *   绝不让"枚举超时"变成"菜单弹不出来"（那是最糟的失败方式 ——
 *   用户右键没反应，还不知道为什么）。
 * - 菜单已经是异步弹出的了（`popup()`），所以这里多等一次枚举不影响结构。
 */
async function openPetMenu(): Promise<void> {
  await refreshMicDevices();
  const window = petWindow?.browserWindow;
  if (!window || window.isDestroyed()) return;

  /*
   * 处理器表：**自绘菜单与原生退路共用同一份** ——
   * 这样「菜单里每一项点了会做什么」永远只有一处定义。
   */
  const handlers: PetMenuHandlers = {
    /*
     * ⚠️ 原来这里是 `chat: () => void openSettings()` ——
     * **标签写着「聊天…」，实际打开的是设置窗口**（2026-10-12 发现的 bug）。
     * 用户要求把那一项换成「聊天模式」开关，正好把这个误导项删掉。
     * 设置窗口在菜单里本来就有「设置…」，不需要第二个入口。
     */
    // 切换对话框显隐。主进程自己维护 `chatVisible` 这个**用户意愿**
    // （菜单文字是"隐藏对话框"/"显示对话框"，就靠它决定），
    // 并把它交给 chatWindow —— 那边的 `userHidden` 会阻止
    // `focusInput()` 之类路径把对话框又拉出来（否则就是假开关）。
    toggleChat: () => {
      chatVisible = !chatVisible;
      chatWindow?.setUserHidden(!chatVisible);
      // **落盘**：用户报"重启又出来了"，说明这份意愿必须活过重启。
      // （与看屏幕开关同理：这类界面开关只放内存里必然被误解为保存失败。）
      const result = writeSettings({ chatVisible });
      if (!result.ok) {
        log(`对话框显隐落盘失败：${result.error ?? '未知原因'}（路径 ${result.path}）`);
      }
      log(`对话框：${chatVisible ? '已显示' : '已隐藏'}`);
    },
    /*
     * 气泡显隐开关（用户要求 2026-10-07：
     * 「可在宠物菜单设置隐藏和显示气泡」）。
     *
     * ⚠️ 真正的实现在 `toggleBubbleVisibility()` 里 —— 右键菜单和**托盘菜单**
     * 都要能调它（托盘是宠物被隐藏时唯一的入口），所以不能写在这里。
     */
    toggleBubble: () => toggleBubbleVisibility(),
    /*
     * 状态条显隐（2026-10-11）。**与气泡同理**：托盘里也要能调 ——
     * 托盘是宠物被隐藏时唯一的入口。
     */
    toggleStatus: () => toggleStatusVisibility(),

    /*
     * 对话条复位（2026-10-12）。用户能拖它之后就得能撤销 ——
     * 否则拖到屏幕角落、或换显示器后位置不合适，只能手动再拖回来。
     */
    resetChat: () => resetChatPosition(),
    // 沉浸模式（一级菜单，用户要求 2026-10-09）
    toggleImmersive: (enabled) => toggleImmersiveMode(enabled),
    /*
     * 聊天模式（2026-10-12 用户要求；2026-10-14 改为双向开关）。
     *
     * 用户原话：「将菜单中的聊天改成聊天模式，这是个开关，启动后将自动开启
     * 显示气泡、显示对话框」。原来是"只单向生效"，2026-10-14 用户要求
     * **关闭也要真的关掉**（开关拨到关、图标文字变灰）—— 见 `setChatMode`。
     */
    toggleChatMode: (enabled) => setChatMode(enabled),
    /*
     * 语音播报（2026-10-14 用户要求）：把宠物的回复念出来。
     * 引擎/音色在「设置 → 语音」里配好，这里只管开不开。
     */
    toggleTtsSpeech: (enabled) => setTtsSpeech(enabled),
    toggleVision: (enabled) => void setVisionWatching(enabled),
    /*
     * 思考模式（用户要求 2026-10-07：「做成开关，在宠物菜单界面显示，
     * 用户自己选择是否开启，默认开启」）。
     *
     * ⚠️ 真正的实现在 `setThinking()` 里 —— 与气泡同理，右键菜单和
     * **托盘菜单**都要能调它（托盘是宠物被隐藏时唯一的入口）。
     */
    toggleThinking: (enabled) => setThinking(enabled),

    /*
     * 联网搜索（2026-10-12 用户要求「在菜单里面打开」）。
     * 与思考模式同理：托盘里也要能调 —— 托盘是宠物被隐藏时唯一的入口。
     */
    toggleWebSearch: (enabled) => setWebSearch(enabled),
    // 联网搜索模式（2026-10-16）：radio 子菜单三选一（精准/快速/关闭）
    webSearchMode: (mode) => setWebSearchMode(mode),

    /*
     * 麦克风选择（用户要求 2026-10-08：「麦克风选项移到宠物菜单里面」）。
     *
     * ⚠️ 真正的实现在 \setMicDevice()\ 里 —— 与气泡同理，
     * 右键菜单和**托盘菜单**都要能调它（托盘是宠物被隐藏时唯一的入口）。
     */
    setMicDevice: (deviceId) => setMicDevice(deviceId),
    toggleVisible: () => petWindow?.toggleVisible(),
    togglePause: () => togglePause(),
    toggleEdgePeek: (enabled) => petWindow?.setEdgePeekEnabled(enabled),
    peekSettings: () => void openSettings(),
    setPetState: (state) => setPetState(state),
    setPetScale: (scale) => setPetScale(scale),
    openLogs: () => void shell.openPath(currentLogFile()),
    settings: () => void openSettings(),
    memory: () => openMemoryManager(),
    snapshotChat: () => void snapshotChat(),
    quit: () => void quit(),
    quitAll: () => void quitAll(),
  };
  /*
   * ⚠️ 存一份给 IPC 用 —— 点击是**之后**从渲染层回来的，
   * 那时这个作用域已经不在了（见 `petMenuHandlers` 的说明）。
   */
  petMenuHandlers = handlers;

  const state: PetMenuState = {
    visible: petWindow?.visible ?? true,
    paused,
    visionWatching: visionEnabled,
    activity,
    // 当前状态键（2026-10-23）。空串 = 后端还没回报过，菜单会退回按 activity 猜。
    petState,
    petScale: petWindow?.currentPetScale ?? 1,
    // 菜单文字（"隐藏对话框"/"显示对话框"）由它决定
    chatVisible,
    bubbleVisible,
    // 状态条显隐（2026-10-11）—— 菜单文字由它决定
    statusVisible: statusWindow?.isVisible() ?? false,
    immersiveMode,
    edgePeek: petWindow?.edgePeekEnabledValue ?? true,
    // 思考模式的勾选状态（用户要求菜单里能开关，默认开）
    thinking: thinkingEnabled,
    // 联网搜索模式（2026-10-16）—— 真源是 persona.yaml 的 tools.web_search_mode
    webSearchMode,
    // 语音播报的勾（2026-10-14）—— 真源是 persona.yaml 的 providers.tts.enabled
    ttsSpeechEnabled,
    // 麦克风（用户要求「麦克风选项移到宠物菜单里面」）
    micDeviceId: currentMicDeviceId(),
    micDevices: micDeviceCache,
  };
  /*
   * ⭐ 先走**自绘菜单**（设计稿那条路）。
   *
   * ⚠️ 它可能失败 —— 返回 `false` 表示窗口起不来（页面加载失败）。
   * 那时**必须退回原生菜单**：这个菜单是改设置 / 退出程序的唯一入口
   * （宠物窗口刻意没有关闭按钮）。静默失败的话用户右键点下去毫无反应，
   * 既看不到菜单、也得不到任何提示。
   */
  const items = buildPetMenuTemplate(state);
  const shown = petMenuWindow?.show(items, screen.getCursorScreenPoint());
  if (shown) return;

  /*
   * 退路：原生菜单。样式是系统的、与设计稿差很远，但**功能齐全** ——
   * 在「好看」和「能用」之间，这一步必须选能用。
   */
  log('[菜单] 自绘窗口不可用，退回原生菜单');
  const missing = popupPetMenu(window, handlers, state);
  if (missing.length) log(`[菜单] 这些动作没有处理器，已置灰：${missing.join(', ')}`);

  // 有 action 找不到处理器时记一条 —— 那说明菜单里出现了"点了没反应"的项。
  // petMenu.ts 已经把它置灰了，但仍然应该让人知道（否则永远没人去修）。
  if (missing.length) log(`[菜单] 这些动作没有处理器，已置灰：${missing.join(', ')}`);
}

function setActivity(value: number, options: { persist?: boolean } = {}): void {
  if (!Number.isFinite(value)) return;
  const clamped = Math.max(0, Math.min(1, value));
  activity = clamped;
  // ⚠️ 2026-10-16：托盘不再有「活泼度」子菜单，所以不推托盘了。
  // 这个函数本身仍然在用（设置页滑杆 / 宠物菜单的活泼度项都走它）。
  // 发给 sidecar 做热更新；它会回报实际生效值，前端据此校准滑杆
  send({ type: 'config.update', patch: { activity: { overall: clamped } } });
  petWindow?.sendToRenderer('pet:activity-local', { value: clamped });
  /*
   * 手动切档也要落盘（2026-10-18，用户选了"回落落盘"之后的连带修正）。
   *
   * 不这样做就会出现**不一致**：回落写文件、手选不写文件 ——
   * 用户手选「自言自语」、20 分钟后回落成「安静」写进文件，
   * 重启后档位是「安静」，可用户明明上次选的是「自言自语」。
   *
   * `options.persist === false` 给"程序内部同步"用（例如启动时把文件值读进内存），
   * 避免那条路径把文件又写一遍。
   */
  if (options.persist !== false) {
    const saved = writeScalar(CONFIG_ROOT, 'activity', 'overall', clamped);
    if (!saved.ok) log(`活泼度：写盘失败（${saved.message}），本次仅内存生效`);
  }
}

function togglePause(): void {
  setPausedInternal(!paused, paused ? 0 : 30);
}

/*
 * ## 状态切换（2026-10-23，替换"活泼度"菜单）
 *
 * 用户要求「将活泼度替换成状态」，四个状态：安静 / 无聊 / 好奇 / 自言自语。
 *
 * ## 为什么状态要同时写 `activity.overall`
 *
 * 自主调度器还在读 `activity.overall` 算"多久开口一次"的概率
 * （见 `autonomy.Scheduler`）。状态是"我现在想干什么"，
 * overall 是"我多常开口"—— 两者**语义不同但不能脱节**：
 * 切到"自言自语"却还按"安静"的频率调度，就会变成"它想说话却一直不开口"。
 *
 * 所以这里把状态映射到一个代表性数值一起写下去。这不是"退回活泼度"，
 * 而是让量化参数跟随状态走（单向：状态 -> 数值）。
 */
const STATE_TO_ACTIVITY: Record<string, number> = {
  quiet: 0.1,
  bored: 0.5,
  curious: 0.8,
  monologue: 1.0,
};

function setPetState(stateKey: string): void {
  const key = String(stateKey ?? '');
  const overall = STATE_TO_ACTIVITY[key];
  if (overall === undefined) {
    log(`状态：未知状态「${key}」，已忽略`);
    return;
  }
  // ① 让 sidecar 的状态机切（它才是状态的真源）
  send({ type: 'pet.set_state', state: key, reason: 'user' });
  // ② 量化参数跟着走（自主调度读它）
  setActivity(overall);
  log(`状态：用户切换 -> ${key}（overall ${overall}）`);
}

/*
 * ## 语言层旋钮（2026-10-22）
 *
 * 用户对结构的划分：
 *
 *     语言层（expression）    修辞 / 文化 / 词汇 / 温度·关心   —— 怎么把话说出来
 *     主观层                  精力 / 心情 / 亲近度 / 注意力 / 状态 —— 怎么认识世界
 *
 * ⚠️ 原来的 `personality`（感性·理性）**已废弃**（2026-10-22 用户决定不做）。
 *    旧配置里若还留着那一段，会被 Python 侧原样忽略，不会报错。
 *
 * ## 真源在 `persona.yaml`，不是 settings.json
 *
 * 这几个旋钮影响的是**提示词**，和人格提示词、活泼度同源，
 * 所以落盘到 `config/persona.yaml`（`writeScalar` 就地改那一行、保留注释）。
 * 放 settings.json 会让"同一个东西两处存"，迟早不一致。
 */
const STYLE_KNOBS: Record<string, { section: string; keys: readonly string[] }> = {
  // 语言层：她怎么说
  expression: { section: 'expression', keys: ['rhetoric', 'culture', 'vocabulary', 'warmth'] },
};

/** 读全部旋钮（给设置页回显）。缺失的键按 0.5（中性）。 */
function readStyleKnobs(): Record<string, Record<string, number>> {
  const out: Record<string, Record<string, number>> = {};
  for (const [group, spec] of Object.entries(STYLE_KNOBS)) {
    const bucket: Record<string, number> = {};
    for (const key of spec.keys) {
      const raw = readScalar(CONFIG_ROOT, spec.section, key);
      const value = Number(raw);
      bucket[key] = Number.isFinite(value) ? Math.max(0, Math.min(1, value)) : 0.5;
    }
    out[group] = bucket;
  }
  return out;
}

/**
 * 写一个旋钮：**落盘 + 热更新**。
 *
 * 与活泼度同一条规矩（改完立刻生效、重启也记住）：
 *   ① `writeScalar` 改 `persona.yaml` —— 重启后还在；
 *   ② 发 `config.update` —— sidecar 的 `apply_patch` 只改内存不写盘，
 *      所以必须两步都做（漏了①重启就丢，漏了②要重启才生效）。
 *
 * ⚠️ 夹取在**两边都做**：这里夹一次（立刻回给滑杆一个合法值），
 *    Python 的 `_validate_expression` 再夹一次（防止手改 yaml 写越界）。
 *    单一来源做不到，因为两条写入路径（界面 / 直接改文件）必须都安全。
 */
function writeStyleKnob(
  section: unknown,
  key: unknown,
  rawValue: unknown,
): { value: number; saved: boolean; error?: string } {
  const name = String(section ?? '');
  const spec = STYLE_KNOBS[name];
  if (!spec) return { value: 0.5, saved: false, error: `未知的分组：${name}` };
  const field = String(key ?? '');
  if (!spec.keys.includes(field)) {
    return { value: 0.5, saved: false, error: `${name} 里没有 ${field}` };
  }
  const num = Number(rawValue);
  if (!Number.isFinite(num)) {
    return { value: 0.5, saved: false, error: '值不是数字' };
  }
  const clamped = Math.max(0, Math.min(1, num));
  // ② 热更新
  send({ type: 'config.update', patch: { [spec.section]: { [field]: clamped } } });
  // ① 落盘
  const saved = writeScalar(CONFIG_ROOT, spec.section, field, clamped);
  if (!saved.ok) log(`${name}.${field}：写盘失败（${saved.message}），本次仅内存生效`);
  return { value: clamped, saved: saved.ok, error: saved.ok ? undefined : saved.message };
}

/**
 * 开关"看屏幕"。
 *
 * 只把意图发出去；**托盘的勾选状态不在这里改** —— 它由 sidecar 回报的生效值
 * （ready 消息里的 providers.vision.enabled）驱动。这样界面上显示的状态永远等于
 * 真正生效的状态，不会出现"勾上了但其实没生效"。这一点对隐私开关尤其重要：
 * 用户必须能信任那个显示。
 */
function setVisionWatching(enabled: boolean): void {
  log(`看屏幕：请求${enabled ? '开启' : '关闭'}`);
  // 先动本地真源：菜单/托盘的勾**立刻**跟着变，不等 sidecar 回报
  // （等回报再变的话，快速开关两次会看到勾"跳回去又跳过来"）
  visionEnabled = Boolean(enabled);
  // **落盘** —— 用户要求"每次重新打开应用获取上次的状态"。
  // sidecar 的 config.update 只改内存不写文件，所以这份必须由主进程存。
  const result = writeSettings({ visionEnabled });
  if (!result.ok) {
    // 存不下要说出来：否则用户以为记住了，重启又变回去（踩过同类坑）
    log(`看屏幕开关落盘失败：${result.error ?? '未知原因'}（路径 ${result.path}）`);
  }
  send({ type: 'config.update', patch: { providers: { vision: { enabled: Boolean(enabled) } } } });
}

/*
 * ⚠️ 原 `syncVisionStateFromConfig()` 已删除（2026-10-16）。
 *
 * 它唯一的作用就是 `tray?.setVisionWatching(vision.enabled)` —— 给托盘菜单的
 * 「允许看屏幕」那一项同步勾选状态。托盘精简后那一项没有了，所以函数空了。
 *
 * ## 删之前确认过「状态没丢」
 *
 * `visionEnabled` 是「用户意愿」的真源，追了一遍它的读写，**都不在这个函数里**：
 *
 *     L1009  启动时从 settingsStore 读
 *     L1908  `visionEnabledFrom(message.config)` 写（调用点那边，不在本函数内）
 *     L3173  `setVisionWatching()` 里写
 *
 * ## 隐私开关本身**没有丢**
 *
 * 宠物右键菜单里有「允许看屏幕」，设置页里也有，两边共用同一个真源。
 * 只是托盘不再重复一份。
 */

// --------------------------------------------------------------------------- //
// 设置窗口
// --------------------------------------------------------------------------- //

function openSettings(): void {
  try {
    if (!settingsWindow) {
      settingsWindow = new SettingsWindow(DIST_DIR);
      // 把诊断接到 startup.log —— "设置弹窗自己没了"这类现象只有这里能查
      settingsWindow.diagnostic = (line) => startupNote(`[设置窗口] ${line}`);
    }
    // 把宠物当前矩形传进去：设置现在是**弹窗**，要贴在宠物旁边
    // （用户要求「设置窗口改弹窗式」）。不传就退回右下角附近。
    const anchor = petWindow?.browserWindow?.getBounds();
    settingsWindow.open(anchor);
    // 打开时推一次当前状态，省得页面自己去要
    void pushSettingsState();
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    log(`打开设置窗口失败：${message}`);
    petWindow?.sendToRenderer('pet:toast', { level: 'error', text: `打开设置失败：${message}` });
  }
}

/**
 * 打开记忆管理窗口。
 *
 * 与 `openSettings` 同构（同一个 `showOnce` 兜底、同样的错误浮到宠物气泡上）。
 * 区别只在窗口尺寸与页面 —— 表格界面需要更宽。
 */
function openMemoryManager(): void {
  try {
    if (!memoryWindow) {
      memoryWindow = new MemoryWindow(DIST_DIR);
      memoryWindow.diagnostic = (line) => startupNote(`[记忆管理] ${line}`);
    }
    memoryWindow.open(petWindow?.browserWindow?.getBounds());
    log('打开记忆管理窗口');
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    log(`打开记忆管理窗口失败：${message}`);
    petWindow?.sendToRenderer('pet:toast', {
      level: 'error',
      text: `打开记忆管理失败：${message}`,
    });
  }
}

/** 组装给设置窗口显示的状态。**密钥只给打码形式，不给明文。** */
function buildSettingsState(): Record<string, unknown> {
  // 从 sidecar 回报的配置快照里读"当前生效值"（snapshot 已剔除密钥字段）。
  // 用防御式取值：这是跨进程数据，形状不能假定。
  const providers = (lastConfigSnapshot?.providers ?? {}) as Record<string, unknown>;
  const brainSection = (providers.brain ?? {}) as Record<string, unknown>;
  const envName = String(brainSection.api_key_env ?? 'DEEPSEEK_API_KEY');

  // 环境变量优先，与 Python 侧 api_key() 的优先级一致
  const fromEnv = (process.env[envName] ?? '').trim();
  const stored = readCredentials();
  const storedKey = Object.values(stored).find((value) => value.trim()) ?? '';
  const active = fromEnv || storedKey;

  return {
    brain: {
      provider: lastReady?.brain ?? 'mock',
      base_url: String(brainSection.base_url ?? ''),
      model: String(brainSection.model ?? ''),
    },
    /*
     * ⚠️ `enabled` 用主进程的 `visionEnabled`（**用户意愿**的真源，也是右键菜单
     * 勾选读的那一份），而不是 `lastReady.vision.enabled`。
     *
     * 原因：sidecar 的 config.update 只改内存、不写文件，重启后它手里是 yaml
     * 默认值；若设置页采信它，用户上次勾的"开"就会被抹掉。
     * 这样设置页开关与右键菜单的勾**必然一致**（用户要求两个入口联动）。
     */
    vision: {
      enabled: visionEnabled,
      provider: lastReady?.vision?.provider ?? 'none',
      watching: visionEnabled,
    },
    apiKeyConfigured: Boolean(active),
    apiKeyMasked: active ? maskKey(active) : '',
    apiKeySource: fromEnv ? `环境变量 ${envName}` : storedKey ? '本地凭据文件' : '未配置',
    credentialsPath: credentialsPath(),
    storedProviders: describeCredentials(),
    status: statusText,
    /*
     * 人格提示词（用户要求「支持用户自己设定提示词」）。
     *
     * 真源是 `config/persona.yaml` 的 `persona.system_prompt` —— **直接读文件**，
     * 不走 sidecar 快照。理由：sidecar 的 `config.update` 只改内存不写盘，
     * 而提示词必须"改一次就永久生效"，所以文件才是真源。
     */
    persona: readPersonaPrompt(CONFIG_ROOT),
  };
}

async function pushSettingsState(): Promise<void> {
  const win = settingsWindow?.browserWindow;
  if (!win || win.isDestroyed()) return;
  settingsWindow?.send('settings:changed', buildSettingsState());
}

/** 配置快照：sidecar 每次回报时更新，供设置窗口显示"当前生效值" */
let lastConfigSnapshot: Record<string, unknown> | null = null;

/**
 * "允许看屏幕"的**当前生效值** —— 右键菜单与托盘的勾选状态都读它。
 *
 * ---- 为什么单独存一份（踩过） ----
 *
 * 原来菜单读的是 `lastReady?.vision?.watching`，而 `lastReady` **只在启动时
 * 赋值一次**，之后永不更新。于是：
 *   · 启动时若 sidecar 还没回报，它是 false → 菜单永远显示"没勾"；
 *   · 用户在设置里改了开关，菜单的勾**不会跟着变**；
 *   · 反过来在菜单里勾上，下次打开菜单又变回旧值。
 * 用户要求这个开关"和设置里联动、每次重开都记得上次"，所以必须有一个
 * **单一且会更新**的真源。
 *
 * 取值顺序：sidecar 回报的生效配置优先（那是真的在读的东西），
 * 拿不到就退回磁盘上用户上次保存的意愿值。
 */
let visionEnabled = false;

/**
 * 思考模式（DeepSeek 的 `thinking` 参数）的当前值。
 *
 * 用户要求 2026-10-07：「把思考模式做成开关，在宠物菜单界面显示，
 * 用户自己选择是否开启，**默认开启**」。
 *
 * ## 与 `visionEnabled` 不同：它**不需要** settingsStore 那一份
 *
 * `visionEnabled` 要在桌面端单独存一份，是因为 `config.update` 只改内存、
 * 不写文件，重启后 sidecar 会退回 yaml 里的默认值 —— 用户勾的"开"就丢了。
 *
 * 而 thinking **直接写进 `config/persona.yaml`**（那是 Python 的真源，
 * 启动时自己会读）。所以不存在"重启丢状态"的问题，不必再存一份 ——
 * **多一份副本就多一次漂移的机会**。
 *
 * 这个变量只用于：① 菜单勾选立刻反馈（不等 sidecar 回报）
 * ② sidecar 尚未连接时也能显示上次的值。
 */
let thinkingEnabled = true;

/**
 * 联网搜索模式（2026-10-16 用户要求）：`accurate` / `fast` / `off`。
 *
 * ## 原来是布尔 `webSearchEnabled`，为什么改三态
 *
 * 用户要求把菜单里的联网搜索改成三选一（精准 free-search 约 10~20 秒 /
 * 快速 websearch-mcp 约 2 秒 / 关闭）—— 两种搜索耗时差 10 倍、质量差很多，
 * 二态开关表达不了"选哪种"。
 *
 * 真源是 `persona.yaml` 的 `tools.web_search_mode`（与 `setThinking` 同一套：
 * 落盘 + `config.update` 热更新）；Python 侧按模式切换搜索后端
 * （见 `tools.py` 的 `set_search_mode`）。默认 `off` —— 隐私口径不变。
 */
let webSearchMode: WebSearchMode = 'off';

/**
 * 「语音播报」开关（2026-10-14 用户要求）。
 *
 * 用户原话：「把宠物的语音功能抽出来放在允许看屏幕上面做成和上面两个一样的
 * 开关，开启时能将宠物回复的文字转为语音说出，语音功能在设置语音中设置好的」。
 *
 * ## 与 `ttsEnabled`（气泡决策用）不是一回事
 *
 *   · `ttsSpeechEnabled`（本变量）= **用户意愿**：菜单那个勾，真源是
 *     `persona.yaml` 的 `providers.tts.enabled`，改了立刻热更新给 sidecar
 *   · `ttsEnabled`（`forwardToBubble` 里那个）= **引擎在不在**：
 *     `provider !== 'none'`，只用来决定"没声音时要不要显示文字"
 *
 * 两件事必须分开：用户关掉播报开关只是"暂时不想听"，不代表要改用文字气泡 ——
 * 那个判断要看引擎还在不在。混成一个变量的话，关掉播报会让沉浸模式的
 * "无声时显示文字"规则一起失效。
 *
 * 默认 `true` 与 `config.py` 里 `providers.tts.enabled` 的缺省一致
 * （老配置没这个键，不能一升级就哑巴）。
 */
let ttsSpeechEnabled = true;

/**
 * 是否已经把"用户上次保存的看屏幕意愿"下发给 sidecar。
 *
 * 只做一次：`ready` 消息之后 sidecar 才连得上，那时把存的值推过去；
 * 之后就以 sidecar 的回报为准（用户点开关 → 回报 → 校准）。
 */
let visionRestored = false;

/**
 * 对话框（对话条）是否可见 —— 菜单文字与 `chatWindow` 的显隐共用这一个真源。
 *
 * 用户要求：「点击隐藏对话框，对话框就隐藏，然后文字就变成显示对话框，
 * 之后再点击可以显示对话框」。
 *
 * 默认 true（启动时对话框是显示的，见 ChatWindow.create 的 ready-to-show）。
 */
let chatVisible = true;

/* ══════════════════════════════════════════════════════════════════════
 * 界面主题（2026-10-14 新增）
 * ══════════════════════════════════════════════════════════════════════ */

/**
 * 当前主题。真源是 `settings.json` 的 `theme`，这里缓存一份 —— 用于
 * **给每个新窗口注入**（新窗口不可能自己去读文件：渲染层没有 fs，也过不了 CSP）。
 */
let theme: 'light' | 'dark' = 'light';

/**
 * 主进程 → 渲染层的**唯一契约**：`<html data-theme="...">`。
 *
 * ## 为什么用属性而不是给每个网页加一套 IPC
 *
 * 应用有 7 个独立窗口（宠物 / 对话条 / 气泡 / 设置 / 记忆 / 状态条 / 右键菜单），
 * 每个窗口一份 preload。要是让"每份 preload 各自去问主进程要主题"，
 * 就有 7 处要维护；而且 preload 里改动 DOM 的时机（DOMContentLoaded 之前
 * documentElement 可能还不存在）是个额外的坑。
 *
 * 所以改成**主进程统一注入一个属性**，各窗口的 CSS 只认这一个属性：
 *
 *     html[data-theme='dark'] { --bg: #1c1d24; ... }
 *
 * 换主题 = 改一次属性，CSS 变量层自动重算，渲染层一行 JS 都不用写。
 */
function themeScript(value: string): string {
  return `document.documentElement.dataset.theme = ${JSON.stringify(value)};`;
}

/**
 * 往一个 webContents 注入当前主题。
 *
 * ⚠️ 失败**不能抛**：窗口可能正在销毁，或者页面还没加载完。
 * 用 `catch` 吞掉是有意的 —— 主题注入失败不该让主进程崩，
 * 而且真正需要生效的时机（下面 `web-contents-created` 里的 `dom-ready`）
 * 还会再来一次。
 */
/**
 * 模块开关的状态脚本（2026-10-18，模块化阶段三）。
 *
 * 只做一件事：把 `modules.sprite.enabled` 变成 `<html data-mod-sprite>`，
 * 由 CSS 决定藏不藏立绘。和主题同一套注入方式 —— 不新增 IPC，也不碰 preload。
 */
function modulesScript(): string {
  const sprite = readDeepScalar(CONFIG_ROOT, ['modules', 'sprite', 'enabled'], true) !== false;
  return `document.documentElement.dataset.modSprite='${sprite ? '1' : '0'}';`;
}

/** 把模块开关刷到一个窗口 */
function injectModules(contents: Electron.WebContents): void {
  if (contents.isDestroyed()) return;
  contents.executeJavaScript(modulesScript()).catch((err: unknown) => {
    // ⚠️ 别静默吞掉：模块开关注入失败的症状是“点了关掉、立绘还在”，
    //    而这种“界面没反应”最难查。留一行日志，至少能看到真因。
    log(
      '模块开关注入失败：' +
        String((err as Error)?.message ?? err).slice(0, 160),
    );
  });
}

/** 把模块开关刷到**已经存在**的所有窗口（切换模块后调） */
function applyModulesToAllWindows(): void {
  for (const win of BrowserWindow.getAllWindows()) {
    injectModules(win.webContents);
  }
}

/** 某个模块现在开没开（读配置文件；读不到就当开 —— 不能因为读失败把界面藏了） */
function moduleEnabled(name: string): boolean {
  try {
    return readDeepScalar(CONFIG_ROOT, ['modules', name, 'enabled'], true) !== false;
  } catch {
    return true;
  }
}


/**
 * 首次启动：准备可写目录 + 复制默认配置（2026-10-18）。
 *
 * 打包后 `config/` 在 resources 里是**只读**的，而程序运行时会写 persona.yaml，
 * 所以第一次启动要把它复制一份到用户目录（`%APPDATA%\桌宠\config`）。
 *
 * 只在**目标不存在**时复制：用户改过的配置绝不能被覆盖（那比不复制糟得多）。
 */
function ensureRuntimeDirs(): void {
  try {
    fs.mkdirSync(DATA_DIR, { recursive: true });
  } catch (error) {
    log(`创建数据目录失败：${(error as Error).message}（尝试继续）`);
  }
  if (!app.isPackaged) return;
  const srcDir = path.join(APP_ROOT, 'config');
  const dstDir = path.join(CONFIG_ROOT, 'config');
  try {
    fs.mkdirSync(dstDir, { recursive: true });
    // ⚠️ 逐个文件判断，不要"persona.yaml 在就整体跳过"（2026-10-18）：
    // 那样后加的 `quotes-corpus.jsonl`（18206 条语料）永远补不进已装好的机器，
    // 表现是自言自语只念 26 句就循环。
    for (const name of ['persona.yaml', 'quotes.yaml', 'quotes-corpus.jsonl']) {
      const src = path.join(srcDir, name);
      const dst = path.join(dstDir, name);
      if (existsSync(src) && !existsSync(dst)) {
        fs.copyFileSync(src, dst);
        log(`首次启动：已复制 ${name} 到配置目录`);
      }
    }
  } catch (error) {
    log(`复制默认配置失败：${(error as Error).message}`);
  }

  /*
   * 语义嵌入模型（91MB）：**同步**复制。
   *
   * 为什么不能像 ASR 模型那样放后台：sidecar 启动时就要加载它，
   * 晚一步就会打一行"模型文件不全，已降级为哈希嵌入"，然后整个进程
   * 都用哈希嵌入 —— 检索质量悄悄变差，重启才好。91MB 同步复制约 0.3 秒。
   */
  const bgeSrc = path.join(APP_ROOT, 'apps', 'desktop', 'pet-data', 'models', 'bge-small-zh-v1.5');
  const bgeDst = path.join(DATA_DIR, 'models', 'bge-small-zh-v1.5');
  if (existsSync(bgeSrc) && !existsSync(bgeDst)) {
    try {
      fs.mkdirSync(path.dirname(bgeDst), { recursive: true });
      fs.cpSync(bgeSrc, bgeDst, { recursive: true });
      log('首次启动：语义嵌入模型已复制到位');
    } catch (error) {
      log(`复制语义嵌入模型失败：${(error as Error).message}（本轮会退化成哈希嵌入）`);
    }
  }
}


/**
 * 离线版：把出厂自带的 ASR 模型复制到可写目录（2026-10-18）。
 *
 * ## 为什么必须复制
 *
 * `faster-whisper` 会往 `asr-models/.locks/` 写锁文件。打包后模型在
 * `resources/`（只读），写失败 -> 加载器认为"没有这个模型" -> 退化成联网下载
 * -> 断网时表现为"本地识别用不了"，且日志里只有一句"还没下载过"。
 *
 * ## 为什么放后台
 *
 * 1.55 GB 的复制要几十秒。放在启动路径上用户会以为卡死。
 * 复制期间本地识别可能不可用（模型还没到位），但云端识别照常 ——
 * 所以不阻塞，只写日志。
 */
async function ensureBundledModels(): Promise<void> {
  if (!app.isPackaged) return;
  /*
   * 出厂自带的模型清单：[在 resources 里的相对路径, 说明, 大约多大]
   *
   * 两个都得复制：加载器会往模型目录旁边写 `.locks`，而 resources 是只读的。
   * · bge-small-zh-v1.5  本地语义嵌入（91MB）—— 不复制会退化成哈希嵌入
   * · faster-whisper-... 语音识别（1.55GB，只有离线版有）
   */
  const models: Array<[string, string, string]> = [
    [
      path.join('asr-models', 'models--mobiuslabsgmbh--faster-whisper-large-v3-turbo'),
      path.join(DATA_DIR, 'asr-models', 'models--mobiuslabsgmbh--faster-whisper-large-v3-turbo'),
      '语音识别模型（1.5GB，可能要几十秒）',
    ],
  ];
  for (const [rel, dst, label] of models) {
    const src = path.join(APP_ROOT, 'apps', 'desktop', 'pet-data', rel);
    if (!existsSync(src) || existsSync(dst)) continue; // 在线版没带 / 已经复制过
    try {
      log(`首次启动：正在把${label}复制到 ${dst}`);
      await fs.promises.mkdir(path.dirname(dst), { recursive: true });
      await fs.promises.cp(src, dst, { recursive: true, force: false });
      log(`${label}已就位`);
    } catch (error) {
      log(`复制${label}失败：${(error as Error).message}`);
    }
  }
}

function injectTheme(contents: Electron.WebContents): void {
  if (contents.isDestroyed()) return;
  contents.executeJavaScript(themeScript(theme)).catch(() => {
    /* 窗口已销毁 / 尚未就绪 —— 忽略，dom-ready 时还会再注入一次 */
  });
}

/** 把主题刷到**已经存在**的所有窗口（切换主题、或启动时窗口刚建好） */
function applyThemeToAllWindows(): void {
  for (const win of BrowserWindow.getAllWindows()) {
    injectTheme(win.webContents);
  }
}

/**
 * 设置主题：落盘 + 立刻刷到所有已开窗口。
 *
 * ⚠️ **必须落盘**：与 `chatVisible` / `bubbleVisible` 同理 ——
 * 这类界面偏好只放内存里，重启就回默认，用户会以为"设置没生效"
 * （这个项目已经因此栽过好几次）。
 */
function setTheme(next: 'light' | 'dark'): { theme: 'light' | 'dark'; saved: boolean; error?: string } {
  theme = next;
  applyThemeToAllWindows();
  const result = writeSettings({ theme });
  if (!result.ok) {
    log(`主题落盘失败：${result.error ?? '未知原因'}（路径 ${result.path}）`);
  } else {
    log(`界面主题：${next === 'dark' ? '深色' : '浅色'}`);
  }
  // 右键菜单每次打开都重建 DOM，这里不用额外通知它
  return { theme, saved: result.ok, error: result.error };
}

/**
 * 气泡是否允许显示（宠物菜单里的开关）。
 *
 * ## 用户要求（2026-10-07）
 *
 * 「气泡改成默认不自动关闭，现在点击气泡或者宠物气泡就消失了，
 * 改成仅可通过气泡持续时间并且**可在宠物菜单设置隐藏和显示气泡**」
 *
 * ⚠️ 语义是**开关**而非"当前可见性"：`false` 之后 `forwardToBubble()` 直接返回，
 * 宠物继续说话也不再弹。默认 true，由 `toggleBubbleVisibility()` 切换并**落盘**。
 */
let bubbleVisible = true;

/**
 * 沉浸模式（用户 2026-10-09 要求）。
 *
 * 打开时**自动隐藏对话框**、**不弹气泡**，只听语音 —— 适合打游戏/看片。
 *
 * ⚠️ **例外**：TTS 关了、或生成失败时**仍然显示文字**。
 * 判据是"**这条消息有没有语音**"—— 用户听不到声音就必须能看，
 * 否则消息等于丢了（见 `forwardToBubble` 里的说明）。
 *
 * ⚠️ 优先级 **沉浸模式 > bubbleVisible**：沉浸模式开着时气泡由它托管。
 * 由 `toggleImmersiveMode()` 切换并**落盘**（重启要恢复）。
 */
let immersiveMode = false;

/**
 * 语音（麦克风）的**最后一次已知状态**（2026-10-07 加）。
 *
 * ⚠️ 它存在主进程而不是只放设置页：因为麦克风的生命周期跟着**宠物窗口**走
 * （真正调 `getUserMedia` 的是那个窗口），而设置页可能随时开关。
 * 设置页每次打开都要问一次"现在到底开着没" —— 否则开关显示的是默认值，
 * 用户会以为"我明明开了"。
 */
let lastVoiceState: { on: boolean; state: string; detail: string } = {
  on: false,
  state: 'idle',
  detail: '',
};

/**
 * **麦克风该不该开**（前端最后一次 `pet.setVoiceEnabled()` 的值）。
 *
 * ## 为什么要单独留一份（2026-10-10）
 *
 * `socket.on('open')` 需要把它**补报**给 sidecar —— 因为 sidecar 的
 * `voice_config.enabled` 默认是 `False`，而前端只在预热时发一次
 * `voice.control`；sidecar 起来得晚一点，那条就丢了。
 *
 * 后果是**闸门能开、帧在推，但 `feed()` 第一行全丢** ——
 * 用户看到的就是"按了没反应"。
 *
 * ⚠️ 不能复用 `lastVoiceState.on`：那个是渲染层通过 `pet:voice-state`
 * **回报**的显示状态，而 `warmUpVoice()` 的成功路径**不回报**
 * （它刻意不报 'listening'，见那里的说明），所以它可能一直是 false。
 */
let micWantedOn = false;

/**
 * 按住说话的键监视器（用户要求：「设置隐私里面设置自定义按键，
 * 按下按键发声，停下结束」）。
 *
 * ⚠️ 它**独立于语音开关**：语音开关决定"允不允许听"，
 * 这个键决定"什么时候听"。两者都开才起作用（见 `restartPushToTalk`）。
 */
/** 聚焦时的真正按住说话（efore-input-event，能拿到松开） */
let focusedPtt: FocusedKeyPtt | null = null;
/** 不聚焦时的切换式（globalShortcut，只有按下） */
let globalPtt: GlobalShortcutPtt | null = null;

/**
 * （重新）启动按住说话的监视器。
 *
 * ## 三件事必须一致，缺一个就是"配置开了却没反应"
 *
 *   ① `voice.push_to_talk.enabled`  配置里开着
 *   ② `voice.push_to_talk.vk > 0`   配了具体的键
 *   ③ 脚本找得到、PowerShell 起得来
 *
 * 所以这里把三种"起不来"的原因**分别记日志**，
 * 而不是笼统地"启动失败" —— 用户按设置说的做了却没反应时，
 * 日志要能直接指出是哪一条。
 */
function restartPushToTalk(): void {
  focusedPtt?.stop();
  focusedPtt = null;
  globalPtt?.stop();
  globalPtt = null;

  const enabled = Boolean(readDeepScalar(CONFIG_ROOT, ['providers', 'push_to_talk', 'enabled'], true));

  /*
   * 🔴 **先把开关状态告诉宠物窗口**，再决定启不启动监视器（2026-10-10 修）。
   *
   * ## 为什么这一句必须在这里、而且是**无条件**的
   *
   * 用户报「没按按键他都能听见我的声音」—— 机理：
   *
   *     voice.py  闸门 `_capture_enabled` **默认就是开的**
   *     app.js    前端只在**按键**时才发消息
   *     app.js    但麦克风开机就预热常开
   *
   *     => 不按住也一直在听
   *
   * 前端要算对闸门就得知道这个开关。所以：
   *   ① 开着 -> 前端把闸门关上，等按键才开
   *   ② **关着也要推** —— 否则用户从"按住说话"切回免提后闸门还关着，
   *      表现变成"免提也不听了"（同一个 bug 的反面）
   *
   * ⚠️ 无条件推还有一层原因：这个函数在启动时、以及设置页改动后都会调，
   * 是**唯一的收敛点**。放在 `if (!enabled) return` 后面就会漏掉"关掉"那一半。
   */
  petWindow?.sendToRenderer('pet:ptt-config', { enabled });

  if (!enabled) {
    startupNote('[按住说话] 配置里是关的，未启动（已通知宠物窗口：免提常听）');
    return;
  }
  const vk = Number(
    readDeepScalar(CONFIG_ROOT, ['providers', 'push_to_talk', 'vk'], true) ?? DEFAULT_PTT_VK,
  );
  /*
   * **独立的全局键**（不聚焦时用的切换式）。
   *
   * ⚠️ 和 
k 是两个不同的键：
   *   vk        = 聚焦时按住说（可以是右 Alt 这种修饰键，靠 before-input-event）
   *   globalVk  = 不聚焦时切换（**必须是普通键**，accelerator 表达不了修饰键）
   *
   * 默认 F9：实测 F9 能注册成功，而且平时打字/游戏都不会按到。
   */
  const globalVk = Number(
    readDeepScalar(CONFIG_ROOT, ['providers', 'push_to_talk', 'global_vk'], true) ?? 0x78,
  );
  if (!Number.isFinite(vk) || vk <= 0) {
    startupNote('[按住说话] 没有配置按键（vk=0），未启动');
    return;
  }

  /*
   * ## 🎯 用 `GlobalShortcutPtt` —— **切换式**（2026-10-10 六轮实测后的结论）
   *
   * 用户要求：「用第一次按下算开始，第二次按下算结束这样试试」
   *
   * ## 为什么回到切换式（这不是退步，是实测结论）
   *
   * 为了做"按住说话"（keydown 开始 / keyup 结束），我把**六个**方案都试了一遍：
   *
   *   · `globalShortcut` —— ✅ **唯一稳定收到键**，但**只有按下、没有松开**
   *   · `GetAsyncKeyState` 轮询（PowerShell）—— ⚠️ 收到过，用一会儿静默失效
   *   · `WH_KEYBOARD_LL` 钩子（PowerShell）—— ⚠️ 只有第一次有反应
   *   · `uiohook-napi`（Node **和** Electron 都试了）—— ❌ 0 个事件
   *     （Electron 里 `REQUIRE-OK` / `START-OK` / `TICK × 17`，就是收不到键）
   *   · `iohook` —— ❌ 装不上：预编译最高 `electron-v87`（Electron 12），
   *     本机需要 ABI 128；它是 NAN 插件，没有 Node-API 那种 ABI 稳定性
   *   · `@hurdlegroup/robotjs` —— ⚠️ 能装能加载（NAPI 预编译），
   *     但**维护分支删了 `getKeyState`** —— 只能发键、不能读键
   *
   * 而且搜索确认：**"获取全局松开"必须有原生键盘钩子**，没有别的办法
   * （那篇 Electron 全局键盘钩子文档与 StackOverflow 的答案结论一致）。
   * 钩子在本机收不到键 -> **keyup 这条路是堵死的**。
   *
   * 所以：**用唯一稳定的机制 + 切换式语义**。
   *
   * ## 两个补偿（都必须有）
   *
   *   · **防抖 400ms**（在 `GlobalShortcutPtt` 里）：实测用户 **1.5 秒内按了
   *     20 多次** —— 按下去没反馈、不知道生效没，于是又按；每按一次切一次
   *     状态，音频全变成 0.02~0.08 秒被时长门槛拦掉 -> "按了没反应"。
   *   · **可听反馈**：按一下响一声（见 `onToggle` 里的 `shell.beep()`），
   *     让用户知道生效了，从根上减少重复按。
   */
  let saying = false; // 当前是不是"正在说"

  /**
   * 统一的"开始采集"，**两条路共用**。
   *
   * ⚠️ 抽出来是必须的：聚焦按键和全局热键都能触发，如果各写一份，
   * `saying` 和渲染层的闸门就会各走各的 —— 那正是"按了没反应"的温床。
   */
  const pttStart = (why: string) => {
    saying = true;
    try {
      /*
       * 🔔 按一下响一声 —— 让用户知道生效了。
       *
       * 实测用户按下去没反馈时会**反复按**（1.5 秒按了 20 多次），
       * 每按一次切一次状态，音频全变成 0.02~0.08 秒被门槛拦掉。
       * `shell.beep()` 走系统默认声音，不依赖 TTS、不占音频通道、
       * 不会盖住用户刚要说的话。
       */
      shell.beep();
    } catch {
      /* 没有默认提示音就静默 —— 不该因为放不出声就中断流程 */
    }
    startupNote(`[按住说话] 开始采集（${why}）`);
    petWindow?.sendToRenderer('pet:ptt', { phase: 'start' });
  };

  const pttStop = (why: string) => {
    saying = false;
    /*
     * ⚠️ 结束不是"取消" —— 那句要**被识别**。
     * 真正的"取消"是另一个动作（按 Esc / 点别处），这里不做。
     */
    startupNote(`[按住说话] 结束采集，送去识别（${why}）`);
    petWindow?.sendToRenderer('pet:ptt', { phase: 'stop' });
  };

  /*
   * ## 🎯 主路：**聚焦时按住** —— 真正的「按住说话」（2026-10-10 找到的答案）
   *
   * 用户要求用**右 Ctrl 或右 Alt**。实测（`tools/test-before-input-focused.py`）
   * 证明 `before-input-event` **同时给 keydown 和 keyup**，而且
   * `code` 把左右分得清清楚楚：
   *
   *     按住右 Ctrl：keyDown code="ControlRight" autoRepeat=false
   *                  ...
   *                  keyUp   code="ControlRight"              <- 松开 ✅
   *
   * **不需要任何原生钩子** —— 而那六个钩子方案全都在"拿不到松开"或
   * "收不到事件"上栽了。
   *
   * ⚠️ 唯一约束：**只在窗口聚焦时有效**（实测失焦 0 个事件）。
   * 所以下面还留了 `GlobalShortcutPtt` 管"不聚焦"那一段。
   */
  focusedPtt = new FocusedKeyPtt({
    vk,
    onPress: () => pttStart(`按住 ${vkName(vk)}`),
    onRelease: () => pttStop(`松开 ${vkName(vk)}`),
    onLog: (line) => {
      log(line);
      startupNote(line);
    },
  });

  /*
   * ⚠️ **失焦时必须复位状态机**。
   *
   * 如果用户按住键的同时点走了焦点，`keyup` 就送不到这个窗口 ——
   * `down` 会永远停在 true，下次按下会被 `if (this.down) return` 挡掉，
   * 表现又是"按了没反应"。所以失焦时清掉。
   */
  const petBrowserWindow = petWindow?.browserWindow ?? null;
  petBrowserWindow?.on('blur', () => {
    focusedPtt?.reset();
  });

  // 挂上监听（`attach` 内部会检查这个键是否受支持，不支持就明确记日志）
  const focusedOk = petBrowserWindow ? focusedPtt.attach(petBrowserWindow.webContents) : false;

  /*
   * ## 备路：不聚焦时的**切换式**（`globalShortcut`）
   *
   * 实测里 `globalShortcut` 是**唯一稳定收到按键**的机制，但它
   * **只有按下、没有松开**（Chromium 的限制）。所以不聚焦时只能切换式：
   * 按一次开始、再按一次结束。
   */
  globalPtt = new GlobalShortcutPtt({
    vk,
    globalVk,
    onToggle: () => {
      if (!saying) pttStart('全局热键');
      else pttStop('全局热键');
    },
    onLog: (line) => {
      log(line);
      startupNote(line);
    },
  });

  const ok = globalPtt.start();
  if (!ok) {
    // 起不来时把原因说清楚（start() 内部已经记了具体原因）
    startupNote('[按住说话] 全局热键未能注册（不聚焦时的切换式不可用）');
  }
  if (!focusedOk) {
    startupNote('[按住说话] 聚焦时的按住说话未能挂上');
  }
}

/**
 * 切换气泡显隐（右键菜单与**托盘菜单**共用）。
 *
 * ## ⚠️ 为什么必须抽成函数，而不是写在菜单处理器里
 *
 * 托盘是**宠物被隐藏时唯一的入口**（窗口 `skipTaskbar`，任务栏里没有按钮）。
 * 如果气泡开关只挂在宠物右键菜单上，用户"隐藏宠物 + 隐藏气泡"之后就
 * **再也点不到那个菜单**了 —— 只能去改配置文件。
 *
 * 所以右键与托盘两条路都调这里，行为保证一致。
 */
/**
 * 让对话条回到"自动钉右下角"的模式（2026-10-12）。
 *
 * ## 为什么需要它
 *
 * 用户能拖动对话条之后，就得有办法**撤销自己的拖动** ——
 * 否则拖到屏幕角落、或者换显示器之后位置不合适，用户只能手动再拖回来
 * （而"拖到一个已经看不见的地方"是拖拽功能最常见的死局）。
 *
 * 顺带清掉落盘的坐标，这样下次启动也走自动模式。
 */
function resetChatPosition(): void {
  chatWindow?.clearUserPosition();
  /*
   * ⚠️ 必须传 **`null`**，不能传 `undefined`。
   *
   * `writeSettings` 的合并规则是 `patch.x !== undefined ? patch.x : current.x` ——
   * 所以传 `undefined` 等于**"不修改"**，落盘的还是旧坐标，重启又跳回去。
   * （这个坑一开始就踩了：复位看起来生效了，重启后位置又回来了。）
   */
  const result = writeSettings({ chatX: null, chatY: null });
  if (!result.ok) {
    log(`对话条位置复位落盘失败：${result.error ?? '未知原因'}（路径 ${result.path}）`);
  }
  chatWindow?.anchorToCorner('bottom-right');
  log('对话条已复位到右下角（恢复自动定位）');
}

/**
 * 切换状态条的显隐（2026-10-11）。
 *
 * 与 `toggleBubbleVisibility()` 同一条取舍：
 *   · **右键菜单与托盘两条路都调它** —— 否则行为会漂移
 *   · 状态只放内存里的界面开关会被误解为"保存失败"，所以**落盘**
 */
function toggleStatusVisibility(): void {
  const next = !(readSettings().statusVisible ?? true);
  statusWindow?.setVisible(next);
  const result = writeSettings({ statusVisible: next });
  if (!result.ok) {
    log(`状态条显隐落盘失败：${result.error ?? '未知原因'}（路径 ${result.path}）`);
  }
  log(`状态条：${next ? '已显示' : '已隐藏'}`);
}

function toggleBubbleVisibility(): void {
  bubbleVisible = !bubbleVisible;
  /*
   * 关的时候顺手把当前那个藏掉 —— 否则"我明明关了它还在"很怪。
   *
   * 开的时候（2026-10-14 修）：**把上一条内容摆回去**。
   *
   * 用户报：「气泡显示之后，用宠物控制里的隐藏气泡隐藏了，但是再点击
   * 显示气泡，气泡却没有显示」。原来的行为是"开的时候不主动弹（没有内容
   * 可弹），等下一次说话自然出现" —— 这对**曾经有内容**的气泡是错的：
   * 用户刚把一条还挂着的气泡藏起来，点"显示"当然期待它**回来**，
   * 而不是等下次说话。
   *
   * 所以现在：有缓存（`lastBubbleMessage`，见 `sendToBubble` 的说明）就
   * 重放那条 —— `sendToBubble` 会重新 show + 发 `bubble:message`，
   * 渲染层画完报 `bubble:shown`，存在时间计时**重新起算**
   * （hide() 已清掉旧计时器，不会"刚显示就被旧计时器藏掉"）。
   *
   * 没有缓存（这条会话还从来没说过话）就维持老行为：等下一次说话。
   */
  if (bubbleVisible) {
    if (lastBubbleMessage && bubbleWindow) {
      // 重新定位：宠物可能刚被拖过（place 用的锚点要新鲜）
      const anchor = petWindow?.artScreenAnchor;
      if (anchor) bubbleWindow.place(anchor);
      sendToBubble(bubbleWindow, lastBubbleMessage.message, lastBubbleMessage.kind);
      log('气泡：已恢复显示上一条内容');
    } else {
      log('气泡：已允许显示（这条会话还没有内容，等下一次说话弹出）');
    }
  } else {
    bubbleWindow?.hide();
  }
  // **落盘**与对话框同理：这类界面开关只放内存里会被误解为保存失败。
  const result = writeSettings({ bubbleVisible });
  if (!result.ok) {
    log(`气泡显隐落盘失败：${result.error ?? '未知原因'}（路径 ${result.path}）`);
  }
  log(`气泡：${bubbleVisible ? '已显示（会随说话自动弹出）' : '已隐藏（不再自动弹出）'}`);
}

/**
 * 切换沉浸模式。
 *
 * ## 用户要求（2026-10-09）
 *
 * > 「设置个游戏或沉浸模式，自动隐藏对话框和气泡，只用语音来聊天回答，
 * >   不显示气泡，tts 失败或关了显示错误文本」
 *
 * ## 它动什么
 *
 *   ① **隐藏对话框** —— 复用 `chatVisible`（和菜单「隐藏对话框」同一真源），
 *      所以用户之后手动点「显示对话框」也能开回来，两边不打架
 *   ② **不弹气泡** —— 由 `forwardToBubble()` 里的门控负责（例外见那里）
 *
 * ## 关了之后**不自动恢复**对话框
 *
 * 用户可能是在沉浸模式里特意点了「显示对话框」，退出时再"恢复"
 * 会把他那个选择覆盖掉。**一个开关只管自己那一下**，别猜用户想要什么。
 * （要恢复就再点一次「显示对话框」，菜单文字是准的。）
 */
/**
 * 聊天模式（2026-10-12 用户要求；2026-10-14 改为**双向开关**）。
 *
 * ## 用户原话
 *
 * > 「将菜单中的聊天改成聊天模式，这是个开关，启动后将自动开启显示气泡、
 * >   显示对话框」
 *
 * 起初我按「开就将两个都显示，**关闭不动任何东西**」实现（用户当时点了确认）。
 *
 * ## 🔴 2026-10-14 用户推翻那一条
 *
 * > 「聊天模式关闭之后的右边按钮开关关闭效果和图标文字效果还是要搞的，
 * >   我之前说错了，这些前端效果还是要有的」
 *
 * 也就是：**点了就得能关**。旧语义在界面上是自相矛盾的 ——
 * 勾是从 `bubbleVisible && chatVisible` **现算**的，关闭时什么都不做，
 * 下次打开菜单勾又自己回来了，用户看到的就是"这个开关关不掉"。
 *
 * 所以现在是对称的双向开关：
 *
 *   · `enabled === true`  -> 气泡 + 对话框**都显示**
 *   · `enabled === false` -> 气泡 + 对话框**都隐藏**
 *
 * 各自复用与「隐藏气泡」「隐藏对话框」**同一个真源、同一套落盘字段**，
 * 所以菜单里的三处开关互相同步，不会打架。
 *
 * 界面上那个勾的含义因此是「两个是不是都已经显示」——
 * 用户单独关掉任意一个时勾会自己取消，那是诚实的：
 * 此刻它并不处于「聊天模式已开启」的状态。
 *
 * ## ⚠️ 两个「意愿」都要落盘
 *
 * `chatVisible` / `bubbleVisible` 都是**用户意愿**，各有自己的落盘字段
 * （见 `toggleChat` / `toggleBubbleVisibility` 里的说明：用户报过
 * 「重启又出来了」，所以这类界面开关只放内存里必然被误解为保存失败）。
 * 这里一次改两个，就**必须落盘两次** —— 少一次的表现是
 * 「重启后只恢复了一个」，很难归因。
 */
function setChatMode(enabled: boolean): void {
  if (!enabled) {
    // ① 对话框 —— 与菜单「隐藏对话框」同一真源（`chatVisible`）+ 同一落盘字段
    if (chatVisible) {
      chatVisible = false;
      chatWindow?.setUserHidden(true);
      const r1 = writeSettings({ chatVisible });
      if (!r1.ok) log(`聊天模式：对话框状态落盘失败：${r1.error ?? '未知原因'}`);
      log('聊天模式：隐藏对话框');
    }
    // ② 气泡 —— 与菜单「隐藏气泡」同一真源（`bubbleVisible`）+ 同一落盘字段
    if (bubbleVisible) {
      bubbleVisible = false;
      /*
       * 关的时候顺手把当前那个藏掉 —— 与 `toggleBubbleVisibility` 一致：
       * 否则"我明明关了它还在"很怪。开的时候**不主动弹**（没有内容可弹）。
       */
      bubbleWindow?.hide();
      const r2 = writeSettings({ bubbleVisible });
      if (!r2.ok) log(`聊天模式：气泡状态落盘失败：${r2.error ?? '未知原因'}`);
      log('聊天模式：隐藏气泡');
    }
    log('聊天模式：已关闭（气泡 + 对话框都隐藏）');
    return;
  }

  // ① 对话框
  if (!chatVisible) {
    chatVisible = true;
    chatWindow?.setUserHidden(false);
    const r1 = writeSettings({ chatVisible });
    if (!r1.ok) log(`聊天模式：对话框状态落盘失败：${r1.error ?? '未知原因'}`);
    log('聊天模式：显示对话框');
  }
  // ② 气泡
  if (!bubbleVisible) {
    bubbleVisible = true;
    /*
     * 开的时候**不主动弹**一个空气泡 —— 与 `toggleBubbleVisibility` 一致：
     * 没有内容可弹，等下一次说话自然出现。
     */
    const r2 = writeSettings({ bubbleVisible });
    if (!r2.ok) log(`聊天模式：气泡状态落盘失败：${r2.error ?? '未知原因'}`);
    log('聊天模式：显示气泡');
  }

  log(
    chatVisible && bubbleVisible
      ? '聊天模式：已开启（气泡 + 对话框都显示）'
      : '聊天模式：部分生效（有一项仍被沉浸模式托管）',
  );
}

/**
 * 切换沉浸模式。
 *
 * ## 🔴 为什么改成「吃 `enabled` 参数」（2026-10-12）
 *
 * 原来它是零参数的（内部自己 `immersiveMode = !immersiveMode`），
 * 而菜单项是 **checkbox**。于是它**一直点不动** ——
 * 用户没报，是这次新加的门禁断言抓到的：
 *
 *     menuItem 是 checkbox  ->  Electron 自己翻转勾
 *     click 回调走"无参数"分支 -> handler()  ->  enabled === undefined
 *     immersiveMode = !undefined  ->  **true**
 *
 * 结果：**点一次能开，再点还是 true** —— 永远关不掉。
 * 而菜单看着正常（勾会来回翻），只有状态不跟着走。
 *
 * 实测确认（`probe-menu-checkbox-semantics.py`，历史实测脚本）：
 * click 回调里 `menuItem.checked` 是**点击之后**的新状态
 * （before=false -> click 里 checked=true -> after=true）。
 *
 * 所以正确做法是**接受那个值**，而不是自己翻转。
 */
function toggleImmersiveMode(enabled?: boolean): void {
  const next = typeof enabled === 'boolean' ? enabled : !immersiveMode;
  // 重复设同一个值是空操作（否则同一状态会被反复落盘 + 刷日志）
  if (next === immersiveMode) return;
  immersiveMode = next;

  if (immersiveMode) {
    // 进去时把对话框收掉，并把当前那个气泡也藏了
    // （否则"我开了沉浸模式它还在"很怪）
    chatVisible = false;
    chatWindow?.setUserHidden(true);
    bubbleWindow?.hide();
    const r = writeSettings({ chatVisible });
    if (!r.ok) log(`沉浸模式：对话框状态落盘失败：${r.error ?? '未知原因'}`);
  }

  const result = writeSettings({ immersiveMode });
  if (!result.ok) {
    log(`沉浸模式落盘失败：${result.error ?? '未知原因'}（路径 ${result.path}）`);
  }
  log(
    immersiveMode
      ? '沉浸模式：开（隐藏对话框和气泡，只听语音；无声时仍显示文字）'
      : '沉浸模式：关（对话框和气泡恢复常规行为）',
  );
  // 菜单每次打开都重新构建（`openPetMenu`），所以不用在这里刷新勾选态
}

/** 从 sidecar 回报的配置里取 `providers.vision.enabled` */
function visionEnabledFrom(config: unknown): boolean | null {
  const providers = (config as { providers?: Record<string, unknown> } | null)?.providers;
  const vision = (providers?.vision ?? null) as { enabled?: unknown } | null;
  if (!vision || typeof vision.enabled !== 'boolean') return null;
  return vision.enabled;
}

/**
 * 读语音识别的当前配置（给设置页用）。
 *
 * ⚠️ 全部用 `readDeepScalar(..., true)`（严格层级）：配置里存在**同名键**
 * 出现在不同层级的情况（`memory:` 就出现过两次），不精确匹配缩进会读错段。
 *
 * ⚠️ 返回里**不含密钥明文** —— 只回"配没配、打码形式"。
 * 与对话密钥同一口径：明文永不回填到输入框。
 */
function readAsrConfig(): {
  mode: string;
  localModel: string;
  cloudModel: string;
  cloudBaseUrl: string;
  cloudKeyConfigured: boolean;
  cloudKeyMasked: string;
  cloudKeyEnv: string;
} {
  const read = (key: string): string => {
    const v = readDeepScalar(CONFIG_ROOT, ['providers', 'asr', key], true);
    return v === null || v === undefined ? '' : String(v);
  };
  const envName = read('cloud_api_key_env') || 'SILICONFLOW_API_KEY';
  const baseUrl = read('cloud_base_url') || 'https://api.siliconflow.cn/v1';
  // 密钥可能在环境变量里，也可能在凭据文件里（设置页填的）
  const fromEnv = (process.env[envName] ?? '').trim();
  const credName = providerNameFor(baseUrl, 'siliconflow');
  const fromFile = (readCredentials()[credName] ?? '').trim();
  const key = fromEnv || fromFile;
  return {
    mode: read('provider') || 'local',
    localModel: read('model') || 'large-v3-turbo',
    cloudModel: read('cloud_model') || 'FunAudioLLM/SenseVoiceSmall',
    cloudBaseUrl: baseUrl,
    cloudKeyConfigured: Boolean(key),
    cloudKeyMasked: key ? `${key.slice(0, 4)}…${key.slice(-4)}（共 ${key.length} 字符，来源：${fromEnv ? '环境变量' : '本地凭据文件'}）` : '',
    cloudKeyEnv: envName,
  };
}

  /**
   * 读语音合成配置（`providers.tts`）。
   *
   * ## 注意 `voice` 的含义**两条路线不一样**（2026-10-09 实测踩到）
   *
   *     本地 windows -> SAPI 的名字，如 `Microsoft Huihui Desktop`
   *     云端 openai  -> **`模型名:音色名`**，如 `FunAudioLLM/CosyVoice2-0.5B:alex`
   *
   * 实测（直接打 API）：
   *
   *     voice 写成 alex                     -> 400 Invalid voice (code 20047)
   *     voice 写成 模型名:alex              -> 200，17407 字节
   *     voice 不传                          -> 400 Voice or reference audio should be set
   *
   * 所以界面上要按当前 provider 给**不同**的提示与默认值，不能共用一个输入框。
   */
  /*
   * 本地 SAPI 可用性探测（启动后跑一次，结果缓存）。
   *
   * ## 为什么需要
   *
   * 本地路线的**代码链路全对**，但这个环境的 SAPI 取不到音色：
   *
   *     SAPI 合成失败：SAPI 没有可用音色（默认也取不到）。
   *     已安装：Microsoft Huihui Desktop / Microsoft Zira Desktop
   *
   * 也就是**枚举得到、一个都用不了**。让用户每次点「试听」都撞一次墙
   * 是不负责的 —— 界面上就该说明白。
   *
   * ## 为什么不在启动时同步跑
   *
   * 一次合成要几百毫秒，同步跑会拖慢启动。所以延迟 3 秒、后台跑；
   * 探测完成前按**不可用**处理（宁可保守）。
   *
   * ---------------------------------------------------------------------
   * ⚠️ **2026-10-09 起不再探测了**（下面这个函数不再被调用）。
   *
   * 原因：界面上**已经没有「本地」这个合成方式**（按用户要求删掉了），
   * 而本地方案在这个环境**确定用不了** —— 统计过 0/28（见 docs 第六十二节）。
   * 每次启动都起一个 PowerShell 去跑必然失败的探测，纯粹是浪费。
   *
   * 为什么**留着函数体**而不是删掉：
   *   · `tools/probe-sapi-*.mjs` 那套排查脚本还能用
   *   · 以后换机器/换 Electron 版本想重新启用时，把调用点加回来就行
   *     （先跑 `probe-sapi-stability.mjs`（历史实测脚本）看成功率是不是 100%）
   * ---------------------------------------------------------------------
   */
  let localTtsProbe: { usable: boolean; note: string; at: number } | null = null;

  // eslint-disable-next-line @typescript-eslint/no-unused-vars
  async function probeLocalTtsOnce(): Promise<void> {
    if (localTtsProbe) return;
    const script = path.join(APP_ROOT, 'apps', 'desktop', 'tools', 'win-tts.ps1');
    if (!existsSync(script)) {
      localTtsProbe = { usable: false, note: `找不到合成脚本 ${script}`, at: Date.now() };
      return;
    }
    const t0 = Date.now();
    const r = await synthesizeSpeech('可用性探测', {
      script,
      voice: 'Microsoft Huihui Desktop',
      workDir: path.join(DATA_DIR, 'tts-tmp'),
      timeoutMs: 8000,
      log: () => {},
    });
    const ms = Date.now() - t0;
    /*
     * ⚠️ 除了"成功/失败"，还要看**耗时**。
     *
     * 一次合成要几十秒的话，对"说话"这个场景等于不可用 ——
     * 用户体验是"点了没反应，过一会儿突然冒出一句"。超过 5 秒也算不可用。
     */
    const tooSlow = ms > 5000;
    localTtsProbe = {
      usable: r.ok && !tooSlow,
      note: r.ok
        ? tooSlow
          ? `本机合成一次要 ${(ms / 1000).toFixed(1)} 秒 —— 太慢，说话会明显滞后`
          : `本机可用（一次合成约 ${ms}ms）`
        : /*
                   * ⚠️ 失败文案要说清"**不是你的配置问题、也不是代码 bug**"。
                   *
                   * 2026-10-09 用户报「本地的 windows 的语音没用诶」，
                   * 我查了一整轮才敢下结论。这些结论直接写在这里，
                   * 免得下次还要重查一遍：
                   *
                   *   已排除：Node 版本（官网的 Node 20 是 8/8 成功）、
                   *           环境变量（最小环境照样失败）、
                   *           参数与脚本（argv 逐字正确、同脚本从别处跑成功）、
                   *           音色注册（CLSID/VoicePath/数据文件都齐全）、
                   *           参数格式（System.Speech 与 COM SAPI.SpVoice 都试过）。
                   *
                   *   共同点：从 shell 起的进程能用，应用进程树里的不能用。
                   *   所以是**本环境下子进程能力受限**，与已知的
                   *   "管道 stdio 的子进程失去某些能力"是同一类现象。
                   *
                   * 文案里给出**可行的那条路**（云端），用户才知道下一步做什么。
                   */
                  (r.message || '本机合成失败')
                    + ' —— 这个环境用不了本地合成（已排查：与参数、音色注册、Node 版本都无关，'
                    + '是应用进程树里的子进程能力受限）。请改用上面的「云端 API」。',
      at: Date.now(),
    };
    log(`本地 SAPI 探测：usable=${localTtsProbe.usable} ${localTtsProbe.note}`);
  }

  function readTtsConfig(): {
    provider: string;
    voice: string;
    rate: number;
    volume: number;
    localVoiceHint: string;
    cloudModel: string;
    cloudBaseUrl: string;
    cloudVoiceHint: string;
    localUsable: boolean;
    localNote: string;
    /** 本地音色（SAPI 名字）—— 与云端音色分开存 */
    localVoice: string;
    /** 云端音色（`模型名:音色名`） */
    cloudVoice: string;
    /**
     * edge 的音色（`zh-CN-XiaoxiaoNeural` 这种）—— **第三套格式**，2026-10-11 加。
     *
     * ⚠️ 三条路线的音色名**必须各存各的**：混用一个字段会出现
     * 「切到 edge 保存后 `voice` 变成 zh-CN-…，再切回 openai 就 400」。
     */
    edgeVoice: string;
  } {
    const read = (key: string): string => {
      const v = readDeepScalar(CONFIG_ROOT, ['providers', 'tts', key], true);
      return v === null || v === undefined ? '' : String(v);
    };
    const cloudModel = read('model') || 'FunAudioLLM/CosyVoice2-0.5B';
    const provider = read('provider') || 'none';
    return {
      provider,
      // 按 provider 给对应的音色值（三条路线格式不同，不能混用）
      voice:
        provider === 'windows'
          ? read('local_voice') || read('voice') || ''
          : provider === 'edge'
            ? read('edge_voice') || 'zh-CN-XiaoxiaoNeural'
            : read('voice') || '',
      localVoice: read('local_voice') || read('voice') || '',
      cloudVoice: read('voice') || '',
      /** edge 的音色（2026-10-11 加的**第三套**格式）—— 设置页要单独显示 */
      edgeVoice: read('edge_voice') || 'zh-CN-XiaoxiaoNeural',
      rate: Number(read('rate') || 0),
      volume: Number(read('volume') || 100),
      localVoiceHint: 'Microsoft Huihui Desktop',
      cloudModel,
      cloudBaseUrl: read('base_url') || 'https://api.siliconflow.cn/v1',
      /*
       * ⚠️ 提示要**反映当前配置的音色**，不能写死 `alex`（2026-10-09 修）。
       *
       * 原来这里是 `` `${cloudModel}:alex` `` —— 用户选了 `claire` 之后，
       * 界面上那行还写着 `:alex`，看起来像"没保存成功"。
       *
       * 展示的语义是「**实际会发给服务端的完整串**」，所以就该用配置里的值。
       * 配置里没值 / 值不像音色串（比如残留了本地那种）时，用 `alex` 当示例。
       */
      cloudVoiceHint: (() => {
        const v = read('voice');
        return v.includes(':') ? v : `${cloudModel}:alex`;
      })(),
      /*
       * 本地 SAPI 在**这台机器 / 这个环境**上实测不可用：
       * `GetInstalledVoices()` 能列出音色，但 `Voice.Name` 是空串、
       * `SelectVoice` 抛「没有当前安全设置可用的语音」。见 docs 第五十六节。
       *
       * 界面上要**明确告诉用户**，而不是等他点了试听没反应再来问。
       * 注意这和 `tts-voices` 能列出音色**不矛盾** ——
       * 恰恰是同一个现象：**枚举得到 ≠ 能用**。
       */
      /*
       * 探测结果（启动后 3 秒跑一次）。还没探完时按**不可用**处理 ——
       * 宁可保守，也不要让用户以为能用。
       */
      localUsable: localTtsProbe?.usable ?? false,
      localNote: localTtsProbe
        ? localTtsProbe.usable
          ? `✅ ${localTtsProbe.note}`
          : `⚠️ ${localTtsProbe.note}`
        : '⚠️ 还没探测完本机语音（启动后几秒内出结果）。',
    };
  }

/** 从 sidecar 回报的配置里取 `providers.brain.thinking` */
function thinkingFrom(config: unknown): boolean | null {
  const providers = (config as { providers?: Record<string, unknown> } | null)?.providers;
  const brain = (providers?.brain ?? null) as { thinking?: unknown } | null;
  if (!brain || typeof brain.thinking !== 'boolean') return null;
  return brain.thinking;
}

/**
 * 从 sidecar 回传的配置快照里读「联网搜索」开关（2026-10-12）。
 *
 * 与 `thinkingFrom` 同构 —— 都是「配置里一个布尔值 -> 界面上的勾」。
 * 放在它旁边是为了让"还有哪些配置项映射到菜单开关"一眼可见。
 *
 * 🔴 2026-10-16：真源改成三态 `web_search_mode`（见 `webSearchMode`）；
 * 这个函数保留给托盘的**二态勾**用（托盘空间小，只显示"开/关"，
 * 勾 = 模式不是 off）。
 */
function webSearchFrom(config: unknown): boolean | null {
  const tools = (config as { tools?: Record<string, unknown> } | null)?.tools;
  if (!tools || typeof tools.web_search !== 'boolean') return null;
  return tools.web_search;
}

/** 从 sidecar 回传的配置快照里读联网搜索模式；没这个键返回 null。 */
function webSearchModeFrom(config: unknown): WebSearchMode | null {
  const tools = (config as { tools?: Record<string, unknown> } | null)?.tools;
  const raw = String(tools?.web_search_mode ?? '').trim().toLowerCase();
  return (WEB_SEARCH_MODES as readonly string[]).includes(raw)
    ? (raw as WebSearchMode)
    : null;
}

/**
 * 从 sidecar 回传的配置快照里读「语音播报」开关（2026-10-14）。
 *
 * 真源是 `providers.tts.enabled`（见 `ttsSpeechEnabled` 的说明）。
 * 与 `thinkingFrom` 同构，同样"没这个键就返回 null、别乱覆盖界面"。
 */
function ttsSpeechFrom(config: unknown): boolean | null {
  const providers = (config as { providers?: Record<string, unknown> } | null)?.providers;
  const tts = (providers?.tts ?? null) as { enabled?: unknown } | null;
  if (!tts || typeof tts.enabled !== 'boolean') return null;
  return tts.enabled;
}

/**
 * 可用麦克风的**缓存**。
 *
 * ⚠️ 为什么是缓存而不是每次现枚举：枚举要走 IPC 到宠物窗口再回来，
 * 而右键菜单要**立刻**弹出来。有缓存就能"先用上次的列表把菜单弹出来，
 * 同时后台刷新" —— 用户感知不到延迟。
 *
 * ⚠️ 枚举失败时**保留旧值、不清空**：让"这次没问出来"变成
 * "列表是上次的"，而不是"菜单里空了/弹不出来"。
 */
let micDeviceCache: { deviceId: string; label: string }[] = [];

/** 读用户选定的麦克风（空 = 系统默认） */
function currentMicDeviceId(): string {
  try {
    return String(readSettings().micDeviceId || '');
  } catch {
    return '';
  }

}

/** 刷新麦克风列表（枚举由**宠物窗口**执行 —— 麦克风权限在它那儿） */
async function refreshMicDevices(): Promise<void> {
  if (!petWindow) return;
  const mics = await petWindow.evaluate<unknown[]>('window.__listMics ? window.__listMics() : []');
  if (!Array.isArray(mics)) return; // 失败：保留旧列表（见缓存说明）
  micDeviceCache = mics
    .filter((m): m is { deviceId: string; label: string } => {
      const o = m as { deviceId?: unknown; label?: unknown };
      return typeof o?.deviceId === 'string' && typeof o?.label === 'string';
    })
    .map((m) => ({ deviceId: m.deviceId, label: m.label }));
  if (micDeviceCache.length) log(`麦克风：枚举到 ${micDeviceCache.length} 支`);
}

/**
 * 选定麦克风（菜单里点的那一项）。
 *
 * 干三件事：**落盘**（下次启动还用这支）、**通知宠物窗口重开流**
 * （换了设备不重开不生效，用户会以为"选了没用"）、记日志。
 *
 * ## ⚠️ 为什么落盘而不是只发消息
 *
 * `micDeviceId` 是"用户意愿"，与气泡显隐一类 —— 只放内存里，
 * 重启就丢，用户会以为设置没保存。
 */
function setMicDevice(deviceId: string): void {
  /*
   * ⚠️ `'default'` 是菜单里的**哨兵值**，代表"系统默认" —— 见 `micOption` 的说明。
   * 菜单项不能直接带空 action（会拼成 `"mic:"`，被判为"找不到处理器"→ 置灰），
   * 所以在这里翻译回空串：空串才是"让浏览器自己选设备"的语义。
   */
  const id = deviceId === 'default' ? '' : String(deviceId ?? '');
  const result = writeSettings({ micDeviceId: id });
  if (!result.ok) log(`麦克风选择落盘失败：${result.error ?? '未知原因'}`);
  const name = micDeviceCache.find((m) => m.deviceId === id)?.label ?? (id ? id.slice(0, 12) : '系统默认');
  log(`麦克风：切换到「${name}」`);
  petWindow?.sendToRenderer('pet:set-mic', { deviceId: id });
}

/**
 * 切换思考模式（DeepSeek 的 `thinking` 参数）。
 *
 * 用户要求 2026-10-07：「把思考模式做成开关，在宠物菜单界面显示，
 * 用户自己选择是否开启，默认开启」。
 *
 * ## 做两件事，缺一不可
 *
 *   ① **写进 `config/persona.yaml`** —— 那是真相源。Python 启动时读它，
 *      所以重启后状态还在，不需要像 vision 那样在桌面端另存一份。
 *   ② **发 `config.update`** —— 让**当前这次运行**立刻生效。
 *      只写文件不发热更新的话，用户切换后要重启 sidecar 才有效果 ——
 *      而按下菜单那一刻期待的是立刻。
 *
 * ⚠️ 顺序是先写盘、再发热更新。反过来的话，热更新成功但写盘失败时，
 * 界面显示"已切换"而重启后变回去 —— 那种"设置没保存"最难查。
 *
 * ## ⚠️ 这是**真金白银**的开关
 *
 * 实测同一句话：思考开 completion_tokens=183、思考关=10（省 92% 的钱、快 47%）。
 * 所以切换时要落日志，方便事后回看"那段时间为什么贵"。
 */
function setThinking(enabled: boolean): void {
  const value = Boolean(enabled);
  log(`思考模式：请求${value ? '开启' : '关闭'}`);

  // 先动本地状态：菜单/托盘的勾**立刻**跟着变，不等 sidecar 回报
  // （等回报再变的话，快速开关两次会看到勾"跳回去又跳过来"）
  thinkingEnabled = value;

  // ① 落盘（真相源）
  const result = writeDeepScalar(
    CONFIG_ROOT,
    ['providers', 'brain', 'thinking'],
    value,
  );
  if (!result.ok) {
    // 存不下必须说出来：否则用户以为记住了，重启又变回去
    log(`思考模式落盘失败：${result.message}（路径 ${result.path}）`);
  }

  // ② 热更新（立刻生效）
  send({ type: 'config.update', patch: { providers: { brain: { thinking: value } } } });

  log(`思考模式：${value ? '已开启（回答更稳，但更慢更贵）' : '已关闭（快、省，回答更随性）'}`);
}

/**
 * 开关「联网搜索」（2026-10-12）。
 *
 * ## 用户原话
 *
 * 「联网功能**默认关闭**，在**菜单里面**打开之后如果我让宠物去网上搜索
 * 相关信息，它要**自己去搜索分辨出来再回答我**」。
 *
 * 所以三件事：默认关（已满足）、菜单里能开（这个函数）、开了之后它自己会用
 * （第 2 步的 agent loop + 工具描述里那句「只在这些情况用」）。
 *
 * ## 与 `setThinking` 完全同构
 *
 * ① 先动本地状态（菜单的勾立刻变，不等 sidecar 回报）
 * ② 落盘到 `config/persona.yaml`（真相源，重启后仍在）
 * ③ 热更新（`config.update` -> sidecar 的 `_on_config_tools` 清缓存 -> 立刻生效）
 *
 * ## ⚠️ 为什么"清缓存"那一步是必需的
 *
 * sidecar 侧 `_tools` 是懒加载缓存。不清的话：**界面上的勾打上了，
 * 但模型依然看不到 `web_search` 工具** —— 用户说「帮我查一下」，
 * 宠物只能凭训练数据瞎答。这正是这个项目里反复出现的**假开关**。
 * （那边的 `_on_config_tools` 就是在做这件事。）
 */
function setWebSearch(enabled: boolean): void {
  const value = Boolean(enabled);
  log(`联网搜索：请求${value ? '开启' : '关闭'}`);

  /*
   * 🔴 2026-10-16：真源从布尔改成三态模式（菜单是 radio 三选一）。
   * 托盘的二态勾映射：开 = 从 off 切到 fast（快速档最接近原行为）；
   * 已在精准档时点托盘保持精准。关 = off。
   */
  const nextMode: WebSearchMode = value
    ? webSearchMode === 'off'
      ? 'fast'
      : webSearchMode
    : 'off';
  setWebSearchMode(nextMode);
}

/**
 * 设置联网搜索模式（2026-10-16 用户要求：菜单三选一）。
 *
 * 用户原话见 `petMenuTemplate.ts` 里 `WEB_SEARCH_LABEL` 的说明。
 *
 * ## 与 `setThinking` 完全同构
 *
 * ① 先动本地状态（菜单的 radio 勾**立刻**跟着变，不等 sidecar 回报）
 * ② 落盘到 `config/persona.yaml` 的 `tools.web_search_mode`（真相源）
 * ③ 热更新（`config.update` -> sidecar 的 `_on_config_tools` 清缓存 -> 立刻生效）
 *
 * ⚠️ 顺带写一个布尔 `tools.web_search`（= 模式不是 off）：老配置没有
 * mode 键时会按它换算（见 gateway `_web_search_mode`），两份一起写
 * 才不会在"降级回旧版"时出现"菜单关了、老版还开着"的错位。
 */
function setWebSearchMode(mode: string): void {
  if (!(WEB_SEARCH_MODES as readonly string[]).includes(mode)) {
    log(`联网搜索：未知模式 ${mode}（忽略）`);
    return;
  }
  const value = mode as WebSearchMode;
  log(`联网搜索：请求切换到「${WEB_SEARCH_LABEL[value]}」`);

  webSearchMode = value;

  // ① 落盘（真相源）—— 模式 + 布尔影子各写一份
  const result = writeDeepScalar(CONFIG_ROOT, ['tools', 'web_search_mode'], value);
  if (!result.ok) {
    // 存不下必须说出来：否则用户以为记住了，重启又变回去
    log(`联网搜索模式落盘失败：${result.message}（路径 ${result.path}）`);
  }
  const shadow = writeDeepScalar(
    CONFIG_ROOT,
    ['tools', 'web_search'],
    value !== 'off',
  );
  if (!shadow.ok) {
    log(`联网搜索布尔影子落盘失败：${shadow.message}（路径 ${shadow.path}）`);
  }

  // ② 热更新（立刻生效 —— sidecar 那边会清掉工具缓存并切后端）
  send({ type: 'config.update', patch: { tools: { web_search_mode: value, web_search: value !== 'off' } } });

  log(
    value === 'off'
      ? '联网搜索：已关闭（它只能凭自己知道的回答）'
      : `联网搜索：已切换到「${WEB_SEARCH_LABEL[value]}」`,
  );
}

/**
 * 切换「语音播报」（2026-10-14 用户要求）。
 *
 * 用户原话：「把宠物的语音功能抽出来放在允许看屏幕上面做成和上面两个一样的
 * 开关，开启时能将宠物回复的文字转为语音说出，语音功能在设置语音中设置好的」。
 *
 * ## 与 `setThinking` / `setWebSearch` 完全同构
 *
 * ① 先动本地状态（菜单的勾**立刻**跟着变，不等 sidecar 回报 ——
 *    等回报的话，快速开关两次会看到勾"跳回去又跳过来"）
 * ② 落盘到 `config/persona.yaml` 的 `providers.tts.enabled`（真相源）
 * ③ 热更新（`config.update` -> sidecar 现读 -> 下一句就生效）
 *
 * ## ⚠️ 它只改"说不说"，**不碰引擎配置**
 *
 * provider / 音色仍然在「设置 → 语音」里配（那里才是引擎的真源）。
 * 这里刻意不写 `providers.tts.provider`：把开关实现成"切 provider 到 none"
 * 会把用户配好的引擎选择**弄丢**，再开还得去设置里重选一遍。
 *
 * ## 关掉之后文字照常显示
 *
 * sidecar 的门控见 `voice.py::_on_pet_say_end_speak`：`enabled=false`
 * 时直接 return，不合成。而气泡那边的 `ttsEnabled`（判"没声音时显不显示文字"）
 * 看的是 `provider !== 'none'` —— 引擎还在，所以**文字气泡不受影响**。
 * 这正是 `ttsSpeechEnabled` 与 `ttsEnabled` 必须分成两个变量的原因。
 */
function setTtsSpeech(enabled: boolean): void {
  const value = Boolean(enabled);
  log(`语音播报：请求${value ? '开启' : '关闭'}`);

  ttsSpeechEnabled = value;

  // ① 落盘（真相源）—— 与 setThinking 同一套 writeDeepScalar
  const result = writeDeepScalar(CONFIG_ROOT, ['providers', 'tts', 'enabled'], value);
  if (!result.ok) {
    // 存不下必须说出来：否则用户以为记住了，重启又变回去
    log(`语音播报落盘失败：${result.message}（路径 ${result.path}）`);
  }

  // ② 热更新（立刻生效 —— sidecar 每次出声前现读这个值）
  send({ type: 'config.update', patch: { providers: { tts: { enabled: value } } } });

  log(
    value
      ? '语音播报：已开启（宠物的回复会用设置里的音色念出来）'
      : '语音播报：已关闭（只显示文字，不出声）',
  );
}

/**
 * 浏览器兜底（2026-10-13 用户要求）。
 *
 * ## 和 `setWebSearch` 同一套做法（落盘 + 热更新，缺一不可）
 *
 * ① 写 `config/persona.yaml` 的 `tools.browser_fallback` —— 真相源，重启后还在
 * ② 发 `config.update` —— 让**当前这次运行**立刻生效。
 *    只写文件不热更新的话，用户勾上之后要重启 sidecar 才有效果。
 *
 * ⚠️ 顺序是**先写盘、再热更新**（和 `setThinking` 一致）：反过来的话，
 * 热更新成功但写盘失败时界面显示"已开启"而重启后变回去 ——
 * 那种"设置没保存"最难查。
 *
 * ## ⚠️ 为什么这个开关必须**在界面上解释代价**
 *
 * 它和"联网搜索"不一样：联网搜索只是"把查询发出去"，
 * 而这个开关会让宠物**以用户的身份浏览网站**（带真 UA、留 cookie）。
 * `settings.html` 里那段说明不是装饰 —— 用户点之前必须知道自己在同意什么。
 *
 * ⚠️ 另外它**很慢**（每次 15~25 秒），所以只在该用的时候用
 * （判据见 `tools.py` 里的 `looks_blocked`）。
 */
function setBrowserFallback(enabled: boolean): void {
  const value = Boolean(enabled);
  log(`浏览器兜底：请求${value ? '开启' : '关闭'}`);

  // ① 落盘（真相源）
  const result = writeDeepScalar(CONFIG_ROOT, ['tools', 'browser_fallback'], value);
  if (!result.ok) {
    log(`浏览器兜底落盘失败：${result.message}（路径 ${result.path}）`);
  }

  // ② 热更新（立刻生效 —— sidecar 那边会清掉工具缓存并重建）
  send({ type: 'config.update', patch: { tools: { browser_fallback: value } } });

  log(
    value
      ? '浏览器兜底：已开启（读不动的网页会用隐藏浏览器重试，每次多等 15~25 秒）'
      : '浏览器兜底：已关闭（有反爬验证的站读不了）',
  );
}

/**
 * 开机自启（2026-10-12）。
 *
 * ## 从"占位项"变成真功能
 *
 * 它原来是菜单里一个置灰的 `开机自启·未实现`。用户要求「开机自启动这个功能
 * 放在设置里面」—— 所以两件事：**删掉菜单里的占位项**（已完成），
 * **在设置页做一个真的**。
 *
 * ## 实现用 Electron 的登录项 API，不用写注册表
 *
 * `app.setLoginItemSettings()` 在三个平台上各自做对的事：
 *
 *     Windows  -> 注册表 `HKCU\...\Run`
 *     macOS    -> 登录项（LSSharedFileList / SMAppService）
 *     Linux    -> `~/.config/autostart/*.desktop`
 *
 * 自己写注册表的话只有 Windows 能用，而且会写错路径（带不带引号、
 * 是 exe 还是 Electron 运行时）。
 *
 * ## ⚠️ 开发模式下注册的是**Electron 自己**，不是"桌面宠物"
 *
 * `process.execPath` 在 `--dev` 下是 `node_modules/.../electron.exe`，
 * 所以开机自启会启动一个**裸 Electron**（没有我们的参数），那不是用户想要的。
 *
 * 所以：开发模式下**允许**设置（否则没法测），但返回值里带上
 * `devWarning`，界面据此提示"打包后才有意义"。
 * 不拦着是因为拦了就没法验证这条链路通不通。
 */
/**
 * 开机自启的登录项参数（2026-10-18）。
 *
 * ## 为什么要统一成一处
 *
 * Windows 上 `app.getLoginItemSettings()` **不带参数**时读的是"默认项"
 * （`process.execPath` 无 args），而开发模式我们注册的是
 * `electron.exe <appPath> --dev --no-sandbox` —— 这是**另一条**注册表值。
 * 于是出现实测过的假象：刚设成"开"，读回来却是 `false`
 * （写进去的是带 args 的项，读的是不带 args 的项）。
 *
 * 读写必须用**同一组** path/args，否则状态永远对不上。
 */
function loginItemOptions(): { path?: string; args?: string[] } {
  return DEV_MODE ? { path: process.execPath, args: [app.getAppPath(), '--dev', '--no-sandbox'] } : {};
}

function loginItemState(): { enabled: boolean; devWarning: boolean } {
  let enabled = false;
  try {
    const opts = loginItemOptions();
    enabled = app.getLoginItemSettings(Object.keys(opts).length ? opts : undefined).openAtLogin;
  } catch (error) {
    log(`读开机自启状态失败：${(error as Error).message}`);
  }
  return { enabled, devWarning: DEV_MODE };
}

function setLoginItem(enabled: boolean): { ok: boolean; enabled: boolean; message: string } {
  try {
    /*
     * 🔴 开发模式必须显式给 `path` + `args`（2026-10-18 补）。
     *
     * 实测注册表里躺着的就是这一行：
     *
     *     electron.app.Electron = F:\ai项目\apps\desktop\node_modules\...
     *                             \electron\dist\electron.exe
     *
     * 没有参数 —— 开机后 Windows 直接起一个**裸 Electron**（空窗口/什么都没有），
     * 而且这个路径还指向 `apps/desktop/node_modules` 下的一份拷贝，
     * 换个 node_modules 布局就失效。
     *
     * 打包后 `process.execPath` 就是 `桌宠.exe`，不需要参数，所以这段只在
     * DEV_MODE 生效 —— 目的是让"开机自启"这条链路今天就能真验证一次。
     */
    const opts = loginItemOptions();
    app.setLoginItemSettings({
      openAtLogin: enabled,
      // ⚠️ Windows 上 `path` 默认是 `process.execPath`。打包后那就是我们的 exe；
      // 开发模式下是 electron.exe —— 所以要补 args，见上。
      openAsHidden: false,
      ...opts,
    });
    const now = app.getLoginItemSettings(
      Object.keys(opts).length ? opts : undefined,
    ).openAtLogin;
    if (now !== enabled) {
      /*
       * ⚠️ **写完要读回来核对**。
       *
       * `setLoginItemSettings` 在某些环境下**静默失败**（权限、组策略、
       * 或这台机器上没有对应的注册表写权限）。不核对的话界面上的勾会
       * 一直显示"已设置"，而重启后什么都没发生 —— 典型的假开关。
       */
      return { ok: false, enabled: now, message: '系统没有接受这个设置（可能被组策略限制）' };
    }
    log(`开机自启：${enabled ? '已开启' : '已关闭'}`);
    return {
      ok: true,
      enabled: now,
      message: enabled
        ? DEV_MODE
          ? '已开启（⚠️ 开发模式下启动的是 Electron 本身，打包后才对）'
          : '已开启'
        : '已关闭',
    };
  } catch (error) {
    const detail = (error as Error).message;
    log(`设置开机自启失败：${detail}`);
    return { ok: false, enabled: false, message: `设置失败：${detail}` };
  }
}

function registerSettingsIpc(): void {
  ipcMain.handle('settings:load', () => buildSettingsState());

  // 开机自启（2026-10-12）：读 + 写。
  // ⚠️ 读和写分开两条：写要读回来核对（见 setLoginItem 的说明），
  //    而界面上打开设置页时读一次和点勾时写一次是两个时机。
  ipcMain.handle('settings:login-item', () => loginItemState());
  ipcMain.handle('settings:set-login-item', (_event, payload: { enabled?: unknown }) =>
    setLoginItem(Boolean(payload?.enabled)),
  );
  /*
   * 浏览器兜底（2026-10-13）。
   *
   * ⚠️ **读的那条不查内存、直接读 yaml** —— 因为真相源是
   * `config/persona.yaml`（Python 启动时读它），和"联网搜索"一样。
   * 查内存的话，用户在别处改了配置（或改的是文件），界面会显示旧值。
   *
   * （和 `loginItemState` 不同：那个必须读回系统状态，因为
   * `setLoginItemSettings` 可能被组策略静默拒绝。这里写文件会不会失败
   * 由 `writeDeepScalar` 的返回值告诉我们，不需要回读。）
   */
  ipcMain.handle('settings:browser-fallback', () =>
    Boolean(readDeepScalar(CONFIG_ROOT, ['tools', 'browser_fallback'], false)),
  );
  ipcMain.handle(
    'settings:set-browser-fallback',
    (_event, payload: { enabled?: unknown }) => {
      setBrowserFallback(Boolean(payload?.enabled));
      return { ok: true };
    },
  );
  /*
   * 清空宠物的浏览数据（2026-10-13 用户要求）。
   *
   * ## 用户的原话
   *
   * > 「清楚的是我**浏览器**上的缓存吗」→ 解释清楚归属之后 → 「**加**」
   *
   * ⚠️ **用户的担心正好暴露了这个功能的设计要点**：
   * 必须让人一眼确认"清的是宠物自己那份"。
   * 所以返回里带上 `path`（真实磁盘位置），界面上直接显示出来 ——
   * 说"不会动你的浏览器"不如把路径摆给他看。
   *
   * ⚠️ 两个通道分开：**查体积**（打开设置页时读一次）和**清空**（点按钮时）。
   * 合成一个的话，每次刷新界面都会清一次 —— 那是个灾难性的设计。
   */
  ipcMain.handle('settings:browser-data-size', async () => {
    const info = await browserDataInfo();
    return {
      totalBytes: info.totalBytes,
      recordBytes: info.recordBytes,
      overheadBytes: info.overheadBytes,
      cookies: info.cookies,
      path: info.path,
      text: formatBytes(info.totalBytes),
      recordText: formatBytes(info.recordBytes),
      overheadText: formatBytes(info.overheadBytes),
    };
  });
  /*
   * 「用浏览器读一个网址」—— 设置页的诊断按钮用（2026-10-13）。
   *
   * ## ⚠️ 为什么要有它（不是"顺便加的"）
   *
   * 我需要**确定性地**验"浏览器兜底 + cookie 计数 + 清空"这一整条链路。
   * 试过两条路，都不可靠：
   *
   * ```
   * ① 让模型去调 web_fetch    -> 模型可以选择不调（实测有一次调了、一次没调）
   * ② 想用 CDP 在隐藏窗口种 cookie -> `show: false` 的窗口不在 CDP target 列表里
   * ```
   *
   * ⚠️ **通过聊天驱动模型是不可靠的** —— 我为此白跑了三轮。
   * 而这条路是**确定的**：调一次就读一次，没有模型在中间做决定。
   *
   * ## ⚠️ 它和真实链路**走同一份逻辑**
   *
   * ```
   * 先普通抓取 -> 失败原因是 WAF/202 且开关打开 -> 才走浏览器
   * ```
   *
   * 和 `apps/brain/pet/tools.py` 里 `_web_fetch` 的判据**一致** ——
   * 不然这个按钮验的就不是真功能了。
   */
  ipcMain.handle('settings:browser-probe', async (_event, payload: { url?: unknown }) => {
    const url = String(payload?.url ?? '').trim();
    if (!/^https?:\/\//i.test(url)) {
      return { ok: false, reason: '地址不是 http/https', stage: 'input' };
    }
    const allowed = readDeepScalar(CONFIG_ROOT, ['tools', 'browser_fallback'], false) === true;
    if (!allowed) {
      return {
        ok: false,
        reason: '浏览器兜底没打开（设置 → 隐私 → 联网）',
        stage: 'config',
      };
    }

    /*
     * 第一步：**用和 Python 侧同样的方式**做一次普通抓取。
     *
     * ⚠️ 这里不复用 Python 的 `web.py`（主进程用不了 Python 的标准库逻辑）——
     * 用一个最小的 HTTP 请求拿**状态码**就够了，因为我们只关心
     * "是不是被 WAF 挡了"（202 / 头里有 waf）。
     */
    let status = 0;
    let headerBlob = '';
    try {
      const response = await fetch(url, {
        headers: {
          'User-Agent':
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 ' +
            '(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
        },
        redirect: 'follow',
      });
      status = response.status;
      headerBlob = [...response.headers.entries()]
        .map(([key, value]) => `${key}:${value}`)
        .join(' ')
        .toLowerCase();
    } catch (error) {
      status = -1;
      headerBlob = String((error as Error)?.message || error).toLowerCase();
    }

    const blocked =
      status === 202 || status === 403 || headerBlob.includes('waf') || headerBlob.includes('captcha');
    if (!blocked) {
      return {
        ok: false,
        reason: `普通抓取没被挡（HTTP ${status}）—— 不需要兜底，所以没走浏览器`,
        stage: 'plain',
        status,
      };
    }

    // 第二步：走浏览器
    const result = await fetchPageViaBrowser(url, { log: (line) => log(line) });
    return {
      ok: result.ok,
      reason: result.reason || '',
      stage: 'browser',
      status,
      title: result.title,
      elapsedMs: result.elapsedMs,
      metas: result.metas,
      textLength: (result.text || '').length,
    };
  });
  ipcMain.handle('settings:clear-browser-data', async () => {
    const result = await clearBrowserData();
    log(
      `[浏览器兜底] 清空浏览数据：${result.ok ? '成功' : '失败'} ` +
        `${formatBytes(result.bytesBefore)} 清了 ${result.cleared.join('/')} ${result.message}`,
    );
    return {
      ok: result.ok,
      bytesBefore: result.bytesBefore,
      cleared: result.cleared,
      message: result.message,
    };
  });
  ipcMain.handle(
    'settings:test',
    async (_event, payload: { baseUrl: string; model: string; apiKey: string }) => {
      // 允许用**未保存**的密钥测试：先测通再保存，避免落盘一个错的
      const key = (payload?.apiKey ?? '').trim() || currentApiKey();
      const result = await testConnection({
        baseUrl: String(payload?.baseUrl ?? ''),
        model: String(payload?.model ?? ''),
        apiKey: key,
      });
      log(`设置：测试连接 ${result.ok ? '成功' : '失败'} —— ${result.message}`);
      return result;
    },
  );

  ipcMain.handle(
    'settings:save',
    async (
      _event,
      payload: {
        provider?: string;
        baseUrl?: string;
        model?: string;
        apiKey?: string;
        visionEnabled?: boolean;
      },
    ) => {
      const provider = String(payload?.provider ?? 'mock');
      const baseUrl = String(payload?.baseUrl ?? '').trim();
      const model = String(payload?.model ?? '').trim();
      const apiKey = (payload?.apiKey ?? '').trim();

      /*
       * ⚠️ 加了多提供商之后，模型必须写进**当前厂商**的槽位
       * （`named.<active>.model`），不能只写平铺的 `brain.model`。
       *
       * 原因（2026-10-08 修）：Python 那边读的是 `brain_profile()` ——
       * 它把 `named.<active>` 合并到平铺字段**之上**，也就是
       * `named.siliconflow.model` 会**盖住** `brain.model`。
       * 只写平铺字段的话，用户在设置页改完模型、保存、看着"已保存"，
       * 实际用的还是配置里那个旧值 —— 一个不报错的静默失效。
       */
      const activeVendor = readBrainConfig().active;

      if (apiKey) {
        // 密钥落盘到用户目录，不进项目配置文件。
        // 槽位名与 Python 的 `_credential_names()` 第一个候选（厂商标）对齐。
        const stored = readCredentials();
        const name = providerNameFor(baseUrl, provider);
        stored[name] = apiKey;
        writeCredentials(stored);
      }

      // 模型也写一份到当前厂商的槽位（平铺那份保留，兼容旧配置）
      if (model) {
        const r = writeDeepScalar(
          CONFIG_ROOT,
          ['providers', 'brain', 'named', activeVendor, 'model'],
          model,
        );
        if (!r.ok) log(`模型写入 named.${activeVendor} 失败：${r.message}（已退回平铺字段）`);
      }

      /*
       * 热更新 —— 改完立刻生效，不必重启 sidecar。
       *
       * ## ⚠️ 必须发**完整的厂商 profile**，不能只发 `{model}`
       *
       * `config.update` 是**深合并进运行内存**的。如果 sidecar 启动时内存里
       * 没有 `named` 结构（旧进程、或配置文件当时还没这一块），只补一个
       * `{model: ...}` 会让 `named.<厂商>` 变成**不完整的块** ——
       * 而 Python 的 `brain_profile()` 会从平铺字段（DeepSeek 的地址）
       * 兜底 `base_url`，于是**切了厂商却还连旧地址**。
       *
       * 那种"看着切了、其实没切"最费时间，所以这里把该厂商该有的键都发全。
       */
      const after = readBrainConfig();
      const vendorProfile = (after.vendors ?? []).find((v) => v.name === after.active);
      send({
        type: 'config.update',
        patch: {
          providers: {
            brain: {
              active: after.active,
              ...(vendorProfile
                ? {
                    named: {
                      [after.active]: {
                        label: vendorProfile.label,
                        base_url: vendorProfile.baseUrl,
                        model: vendorProfile.model,
                      },
                    },
                  }
                : {}),
            },
            ...(typeof payload?.visionEnabled === 'boolean'
              ? { vision: { enabled: payload.visionEnabled } }
              : {}),
          },
        },
      });

      // 发给 sidecar 做热更新。gateway 会在 providers 变化时**重建 brain**，
      // 所以保存后立刻生效，不需要重启进程。
      /*
       * ⚠️ "允许看屏幕"还要走 `setVisionWatching`，**不能只挂在 config.update 上**。
       *
       * 原因：sidecar 的 `Config.apply_patch()` 只改内存、不写文件
       * （见 `apps/brain/pet/config.py`），重启时会 `reload_from_disk()`
       * 回到 yaml 里的值。用户要求这个开关"每次重新打开应用获取上次的状态"，
       * 所以必须由主进程落盘 —— `setVisionWatching` 会做这件事，
       * 同时把本地真源（右键菜单/托盘/设置页共用的那个）一起更新。
       */
      if (typeof payload?.visionEnabled === 'boolean') {
        setVisionWatching(payload.visionEnabled);
      }
      log(`设置：已保存 provider=${provider} model=${model || '(未改)'} 密钥=${apiKey ? '已更新' : '未改'}`);

      return {
        ok: true,
        message: apiKey
          ? '已保存。密钥存在本地凭据文件里，已通知大脑立即切换。'
          : '已保存并通知大脑。',
        detail: baseUrl ? `接口 ${baseUrl}${model ? ` · 模型 ${model}` : ''}` : undefined,
      };
    },
  );

  ipcMain.on('settings:show-pet', () => {
    petWindow?.show();
  });

  /*
   * 列出端点**当前可用**的模型（用户要求：「可以根据 api 传来的可选模型自由选择」）。
   *
   * 允许用**未保存**的密钥去拉：用户应当能"先填 key -> 拉到列表 -> 选模型 -> 再保存"，
   * 而不是被迫先落盘一个可能写错的密钥。
   */
  ipcMain.handle('settings:models', async (_event, payload: { baseUrl?: string; apiKey?: string }) => {
    const baseUrl = String(payload?.baseUrl ?? '').trim() || currentBrainBaseUrl();
    const key = (payload?.apiKey ?? '').trim() || currentApiKey();
    const result = await listModels({ baseUrl, apiKey: key });
    log(`设置：拉取模型列表 ${result.ok ? `成功（${result.models.length} 个）` : `失败 —— ${result.message}`}`);
    return result;
  });

  /*
   * ---------------------------------------------------------------- 语音识别
   *
   * 用户要求 2026-10-07：「把链接语音处理 api 的一起搞了，用硅基流动的 api，
   * 在设置里做了本地大模型和 api 的切换开关」。
   *
   * ## ⚠️ 为什么配置写在 `persona.yaml` 而不是 settingsStore
   *
   * 因为**用这个配置的是 Python**（它才知道要加载哪个识别引擎）。
   * 桌面端的 settingsStore 是给"界面自己的状态"用的（比如气泡显隐），
   * Python 读不到。放 persona.yaml 才是两边都能看见的真相源。
   *
   * ## ⚠️ 密钥不进 persona.yaml
   *
   * 那是项目文件、会被 git 带走。走与对话密钥同一套：
   * 存用户凭据文件（`PET_CREDENTIALS_FILE`），按服务商名分槽位。
   */
  /**
   * 本地 ASR 模型：目录 / 是否已有 / 下载地址（2026-10-19 用户要求）。
   *
   * 用户原话：「你在语音识别那里加上下载地方的路径，不是每个人都开 vpn 的
   * 我们要留个下载地址」。
   *
   * ## 判据必须和 Python 侧一致
   *
   * `voice.py` 的 `ensure_model_cached()` 认的是：
   *
   *     <模型目录>/models--*--faster-whisper-<model>/snapshots/<hash>/ 非空
   *
   * 所以这里也按同一套判据扫，否则会出现"界面说没有、程序却在用"的鬼故事。
   */
  ipcMain.handle('settings:asr-info', () => {
    const dir = path.join(DATA_DIR, 'asr-models');
    const model = String(readDeepScalar(CONFIG_ROOT, ['providers', 'asr', 'model']) ?? 'large-v3-turbo');
    const repo =
      model === 'large-v3-turbo'
        ? 'mobiuslabsgmbh/faster-whisper-large-v3-turbo'
        : `Systran/faster-whisper-${model}`;
    const found: string[] = [];
    try {
      for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
        if (!entry.isDirectory() || !entry.name.startsWith('models--')) continue;
        if (!entry.name.endsWith(`faster-whisper-${model}`)) continue;
        const snapshots = path.join(dir, entry.name, 'snapshots');
        try {
          if (fs.readdirSync(snapshots).length > 0) found.push(model);
        } catch {
          /* 没有 snapshots 目录 = 下了一半 */
        }
      }
    } catch {
      /* 目录还不存在，正常 */
    }
    return {
      dir,
      repo,
      model,
      present: found.length > 0,
      models: found,
      missingDir: !existsSync(dir),
      // 国内镜像摆前面：用户明确说"不是每个人都开 VPN"
      mirrorUrl: `https://hf-mirror.com/${repo}`,
      officialUrl: `https://huggingface.co/${repo}`,
      sizeText: '约 1.5GB',
    };
  });

  /**
   * 打开模型目录（不存在就先建出来）。
   *
   * 不建目录直接打开会报"找不到" —— 而用户点这个按钮的动机恰恰是
   * "我要往里放东西"，所以先建好再打开才对。
   */
  ipcMain.handle('settings:open-asr-dir', async () => {
    const dir = path.join(DATA_DIR, 'asr-models');
    try {
      fs.mkdirSync(dir, { recursive: true });
    } catch (error) {
      return { ok: false, message: `建目录失败：${(error as Error).message}` };
    }
    const error = await shell.openPath(dir);
    return error ? { ok: false, message: error } : { ok: true, dir };
  });

  ipcMain.handle('settings:asr-config', () => readAsrConfig());

  /*
   * ---------------------------------------------------------------- 语音合成
   *
   * 用户要求 2026-10-09：「你放在设置隐私里面的语音系统，统一拉出来放在
   * 设置语音里面，一个是语音识别，一个是文字转语音，这两个标号，
   * 还有涉及隐私的要提示」。
   *
   * 配置同样写 `persona.yaml`（Python 读它），密钥同样进凭据文件。
   */
  ipcMain.handle('settings:tts-config', () => readTtsConfig());

  /*
   * ⚠️ **不再在启动时探测本机 SAPI**（2026-10-09 去掉）。
   *
   * 原来这里延迟 3 秒跑一次探测、完了推给设置页。现在：
   *   · 界面上已经没有「本地」这个合成方式了（按用户要求删掉）
   *   · 本地方案在这个环境**确定用不了**（统计 0/28，见 docs 第六十二节）
   * 所以每次启动都起一个必然失败的 PowerShell，纯属浪费。
   *
   * 想重新启用时：把下面这段加回来，并先跑
   * `probe-sapi-stability.mjs`（历史实测脚本）确认成功率是 100%。
   *
   *   setTimeout(() => {
   *     void probeLocalTtsOnce().then(() => {
   *       settingsWindow?.send('settings:changed', { ttsConfig: readTtsConfig() });
   *     });
   *   }, 3000);
   */

  ipcMain.handle(
    'settings:tts-set',
    (_event, payload: { provider?: string; voice?: string; rate?: unknown; volume?: unknown }) => {
      const provider = String(payload?.provider ?? '').trim().toLowerCase();
      const patch: Record<string, unknown> = {};

      /*
       * ⚠️ `edge` 是 2026-10-11 加的（免费在线合成，见 `EdgeTtsEngine`）。
       * 漏写在这里的后果：设置页选了 edge，主进程**静默丢弃**这个值，
       * 用户看到"保存成功"但配置没变 —— 又一个"改了没用"的假开关。
       */
      if (
        provider === 'windows' ||
        provider === 'openai' ||
        provider === 'edge' ||
        provider === 'none' ||
        provider === 'mock'
      ) {
        patch.provider = provider;
      }
      /*
       * ⚠️ 音色写**哪个字段取决于 provider**（2026-10-09）。
       *
       * 两条路线的音色名格式完全不同，共用一个字段会互相污染：
       * 切到本地保存后 `voice` 变成 SAPI 的名字，再切回云端就 400。
       * 所以本地写 `local_voice`、云端写 `voice`，各记各的。
       */
      if (typeof payload?.voice === 'string' && payload.voice.trim()) {
        /*
         * ⚠️ 音色写**哪个字段取决于 provider**。三条路线的音色名格式完全不同，
         * 共用一个字段会互相污染（切到本地保存后 `voice` 变成 SAPI 的名字，
         * 再切回云端就 400 —— 实测过）：
         *
         *     云端 openai  -> `voice`        模型名:音色名
         *     本地 windows -> `local_voice`  SAPI 名字
         *     edge         -> `edge_voice`   zh-CN-XiaoxiaoNeural
         */
        const voiceKey =
          provider === 'windows' ? 'local_voice' : provider === 'edge' ? 'edge_voice' : 'voice';
        patch[voiceKey] = payload.voice.trim();
      }
      if (payload?.rate !== undefined && payload.rate !== null && String(payload.rate) !== '') {
        patch.rate = Math.max(-10, Math.min(10, Math.round(Number(payload.rate))));
      }
      if (payload?.volume !== undefined && payload?.volume !== null && String(payload.volume) !== '') {
        patch.volume = Math.max(0, Math.min(100, Math.round(Number(payload.volume))));
      }

      if (!Object.keys(patch).length) {
        return { ok: false, message: '没有要改的东西' };
      }

      // ① 落盘 persona.yaml（真相源 —— Python 启动/热更新都读它）
      for (const [key, value] of Object.entries(patch)) {
        const r = writeDeepScalar(CONFIG_ROOT, ['providers', 'tts', key], value as never);
        if (!r.ok) {
          log(`语音合成配置写入失败（${key}）：${r.message}`);
          return { ok: false, message: `写入失败：${r.message}` };
        }
      }

      // ② 热更新 —— 改完立刻生效（Python 会按指纹重建引擎）
      send({ type: 'config.update', patch: { providers: { tts: patch } } });
      // 音量缓存清掉：保存后下一句就按新音量，不用等 2 秒
      if (patch.volume !== undefined) invalidateTtsVolume();
      log(
        `语音合成：provider=${patch.provider ?? '(未改)'} voice=${patch.voice ?? '(未改)'} ` +
          `rate=${patch.rate ?? '(未改)'} volume=${patch.volume ?? '(未改)'}`,
      );

      return { ok: true, message: '已保存并立即生效。', config: readTtsConfig() };
    },
  );

  /**
   * 列出本机 SAPI 音色（只有本地路线需要）。
   *
   * ⚠️ 这个调用**只能用于"给用户看有哪些音色"**，不能用来判断"能不能用" ——
   * 实测这台机器上 `GetInstalledVoices()` 能列出 2 个，但**一个都取不到**
   * （`$synth.Voice.Name` 是空串、`SelectVoice` 抛"没有当前安全设置可用的语音"）。
   * 也就是**枚举得到 ≠ 能用**。真正的可用性判断只能靠 `settings:tts-test`。
   */
  ipcMain.handle('settings:tts-voices', async () => {
    const script = path.join(APP_ROOT, 'apps', 'desktop', 'tools', 'win-tts.ps1');
    if (!existsSync(script)) return { ok: false, message: `找不到 ${script}`, voices: [] };
    const out = await new Promise<string>((resolve) => {
      const p = spawn(
        'powershell',
        ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', script, '-ListVoices'],
        { stdio: ['ignore', 'pipe', 'pipe'] },
      );
      let text = '';
      p.stdout?.on('data', (d: Buffer) => (text += d.toString('utf8')));
      p.on('close', () => resolve(text));
      p.on('error', (e) => resolve(`__ERR__${e.message}`));
    });
    if (out.startsWith('__ERR__')) {
      return { ok: false, message: out.slice(7), voices: [] };
    }
    // TSV：Name \t Culture \t Gender \t Age \t Enabled
    const voices = out
      .split('\n')
      .map((line) => line.trim())
      .filter((line) => line.includes('\t'))
      .map((line) => {
        const [name, culture, gender, age, enabled] = line.split('\t');
        return {
          name: (name ?? '').trim(),
          culture: (culture ?? '').trim(),
          gender: (gender ?? '').trim(),
          age: (age ?? '').trim(),
          enabled: (enabled ?? '').trim() === '1',
        };
      })
      .filter((v) => v.name);
    return { ok: true, voices };
  });

  /**
   * edge-tts 的**中文音色清单**（2026-10-11 加）。
   *
   * ## ⚠️ 为什么是静态清单而不是现问服务端
   *
   * 清单本来可以调 `edge_tts.list_voices()` 现取，但那要：
   *   · 在**主进程**里起 Python（或发一次网络请求）
   *   · 每次设置页打开都要等一次往返（离线时直接失败，界面空着）
   *
   * 而这些音色是**微软侧的固定集合**，几个月才动一次。所以：
   *   · 静态清单兜底 —— 设置页永远有东西可选
   *   · 另外允许**手输任意音色名**（设置页那个输入框）——
   *     这样即使清单过时，用户也能用上新音色
   *
   * 清单来源：实测 `edge_tts.list_voices()` 返回的 14 个中文音色
   * （`tools/test-edge-tts.py` 会把它们全打出来），下面挑了常用的并给了中文说明。
   */
  ipcMain.handle('settings:edge-voices', () => ({
    ok: true,
    voices: [
      { name: 'zh-CN-XiaoxiaoNeural', label: '晓晓 · 女 · 最自然（推荐）' },
      { name: 'zh-CN-XiaoyiNeural', label: '晓伊 · 女 · 年轻' },
      { name: 'zh-CN-YunxiNeural', label: '云希 · 男 · 活泼' },
      { name: 'zh-CN-YunyangNeural', label: '云扬 · 男 · 播音腔' },
      { name: 'zh-CN-YunjianNeural', label: '云健 · 男 · 沉稳' },
      { name: 'zh-CN-YunxiaNeural', label: '云夏 · 男 · 少年' },
      { name: 'zh-CN-liaoning-XiaobeiNeural', label: '晓北 · 女 · 东北话' },
      { name: 'zh-CN-shaanxi-XiaoniNeural', label: '晓妮 · 女 · 陕西话' },
      { name: 'zh-HK-HiuGaaiNeural', label: '曉佳 · 女 · 粤语' },
      { name: 'zh-HK-HiuMaanNeural', label: '曉曼 · 女 · 粤语' },
      { name: 'zh-HK-WanLungNeural', label: '雲龍 · 男 · 粤语' },
      { name: 'zh-TW-HsiaoChenNeural', label: '曉臻 · 女 · 台湾' },
      { name: 'zh-TW-HsiaoYuNeural', label: '曉雨 · 女 · 台湾' },
      { name: 'zh-TW-YunJheNeural', label: '雲哲 · 男 · 台湾' },
    ],
  }));

  /**
   * 试听：**走真实链路**（协议 -> sidecar -> 合成 -> 播放）。
   *
   * 为什么不在这里直接合成：那样测的是"主进程能不能合成"，
   * 而用户要的是"宠物说话这条路通不通"。两者差别很大 ——
   * 本地 SAPI 在主进程里能枚举音色但取不到，直接测会得出误导性结论。
   *
   * `voice.say` 让 sidecar 调它自己的 `VoiceSession.speak()`，
   * 和宠物真实说话**同一个方法**。
   */
  /*
   * 快捷键清单（2026-10-12 加）。
   *
   * ## 为什么要有这条通道
   *
   * 用户要求「把快捷方式写进设置里面」—— 因为托盘菜单里那行长文字
   * **把菜单撑到了约 780px 宽**（Windows 菜单宽度取最长项），
   * 而它还是个不可点的说明项：占最多空间、最没用。
   *
   * ## ⚠️ 为什么从 `SHORTCUTS` 读，而不是页面里写死一份
   *
   * `shortcuts.ts` 的 `SHORTCUTS` 是快捷键的**唯一定义源**（注册也从它读）。
   * 页面自己抄一份就多了一处会漂移的副本 —— 而"界面写着 Ctrl+Alt+M、
   * 实际从没注册过"正是这个项目踩过的真事故：用户按下去毫无反应，
   * 也不报错，只会以为是自己操作错了或程序坏了。
   */
  ipcMain.handle('settings:shortcuts', () => ({
    ok: true,
    shortcuts: SHORTCUTS.map((spec) => ({
      accelerator: spec.accelerator,
      // 给用户看的写法（`CommandOrControl+Alt+P` -> `Ctrl+Alt+P`）
      display: spec.display ?? spec.accelerator,
      description: spec.description,
    })),
  }));

  ipcMain.handle('settings:tts-test', (_event, payload: { text?: unknown }) => {
    const text = String(payload?.text ?? '').trim() || '你好呀，我是小灵，现在能听到我说话吗？';
    const ok = send({ type: 'voice.say', text });
    log(`语音合成试听：${ok ? '已请求' : '❌ 未发送（连接断了）'}「${text.slice(0, 20)}」`);
    return ok
      ? { ok: true, message: '已请求合成，注意听声音（并看宠物嘴型）。' }
      : { ok: false, message: '和大脑的连接断了，发不出去' };
  });


  ipcMain.handle(
    'settings:asr-set',
    (_event, payload: { mode?: string; localModel?: string; cloudModel?: string; cloudBaseUrl?: string; apiKey?: string }) => {
      const mode = String(payload?.mode ?? '').trim().toLowerCase();
      const patch: Record<string, unknown> = {};

      if (mode === 'local' || mode === 'openai' || mode === 'mock') {
        patch.provider = mode;
      }
      if (typeof payload?.localModel === 'string' && payload.localModel.trim()) {
        patch.model = payload.localModel.trim();
      }
      if (typeof payload?.cloudModel === 'string' && payload.cloudModel.trim()) {
        patch.cloud_model = payload.cloudModel.trim();
      }
      if (typeof payload?.cloudBaseUrl === 'string' && payload.cloudBaseUrl.trim()) {
        patch.cloud_base_url = payload.cloudBaseUrl.trim();
      }

      // ① 落盘到 persona.yaml（真相源 —— Python 启动时读它）
      for (const [key, value] of Object.entries(patch)) {
        const r = writeDeepScalar(CONFIG_ROOT, ['providers', 'asr', key], value as never);
        if (!r.ok) {
          log(`语音识别配置写入失败（${key}）：${r.message}`);
          return { ok: false, message: `写入失败：${r.message}` };
        }
      }

      // ② 密钥存凭据文件（不进项目文件）
      const apiKey = String(payload?.apiKey ?? '').trim();
      if (apiKey) {
        const stored = readCredentials();
        // 按服务商分槽位：硅基流动的 key 不该覆盖 DeepSeek 的
        const name = providerNameFor(
          String(patch.cloud_base_url ?? readAsrConfig().cloudBaseUrl ?? ''),
          'siliconflow',
        );
        stored[name] = apiKey;
        writeCredentials(stored);
        log(`语音识别密钥已保存到凭据槽位 "${name}"`);
      }

      // ③ 热更新 —— 改完立刻生效，不必重启 sidecar
      send({ type: 'config.update', patch: { providers: { asr: patch } } });
      log(
        `语音识别：mode=${patch.provider ?? '(未改)'} localModel=${patch.model ?? '(未改)'} ` +
          `cloudModel=${patch.cloud_model ?? '(未改)'} 密钥=${apiKey ? '已更新' : '未改'}`,
      );

      return { ok: true, message: '已保存并立即生效。', config: readAsrConfig() };
    },
  );

  /**
   * 列出 `providers.brain.named` 里所有厂商标。
   *
   * ⚠️ `readDeepScalar` 只能取**标量**，拿不到"有哪些子键"，所以这里
   * **直接读 YAML 文本**抠缩进层级。为什么不引 YAML 库：项目里对
   * `persona.yaml` 一贯是文本级读写（见 `personaStore`），引一个 YAML 依赖
   * 只为枚举键不划算 —— 而且**写回时会把用户手写的注释全部抹掉**。
   *
   * 解析规则（`named:` 之后、**恰好深一层**的键）：
   *
   *     named:
   *       deepseek:        <- 取它
   *         base_url: ...
   *       siliconflow:     <- 取它
   *
   * 拿不准就返回 `[]` —— 调用方（`settings:brain-set`）会因此**拒绝切换**，
   * 而不是让 `active` 指向一个不存在的名字（那种"看着切了、其实没切"最难查）。
   */
  function brainVendorNames(): string[] {
    try {
      const text = fs.readFileSync(path.join(CONFIG_ROOT, 'config', 'persona.yaml'), 'utf-8');
      const lines = text.split(/\r?\n/);
      const start = lines.findIndex((l: string) => /^\s{4}named:\s*$/.test(l));
      if (start < 0) return [];
      const baseIndent = lines[start].match(/^\s*/)?.[0].length ?? 4;
      const names: string[] = [];
      for (let i = start + 1; i < lines.length; i += 1) {
        const line = lines[i];
        if (!line.trim() || line.trim().startsWith('#')) continue;
        const indent = line.match(/^\s*/)?.[0].length ?? 0;
        if (indent <= baseIndent) break; // 回到 `named:` 同级或更浅 -> 这段结束
        if (indent === baseIndent + 2) {
          const m = /^([A-Za-z0-9_-]+):/.exec(line.trim());
          if (m) names.push(m[1]);
        }
      }
      return names;
    } catch (error) {
      log(`枚举大模型提供商失败：${(error as Error).message}`);
      return [];
    }
  }

  /**
   * 读某个厂商的 `models:` 备选列表（设置页那个模型下拉用）。
   *
   * ⚠️ 同样是文本级解析 —— `readDeepScalar` 只处理**标量**，
   * 遇到 `models:` 下面的 `- xxx` 列表只会返回空。
   */
  function readBrainModels(vendor: string): string[] {
    try {
      const text = fs.readFileSync(path.join(CONFIG_ROOT, 'config', 'persona.yaml'), 'utf-8');
      const lines = text.split(/\r?\n/);
      const idx = lines.findIndex((l: string) => new RegExp(`^\\s{6}${vendor}:\\s*$`).test(l));
      if (idx < 0) return [];
      const out: string[] = [];
      for (let i = idx + 1; i < lines.length; i += 1) {
        const line = lines[i];
        if (!line.trim() || line.trim().startsWith('#')) continue;
        const indent = line.match(/^\s*/)?.[0].length ?? 0;
        if (indent <= 6) break; // 到了下一个厂商
        const m = /^\s*-\s*(.+?)\s*$/.exec(line);
        if (m) out.push(m[1]);
      }
      return out;
    } catch {
      return [];
    }
  }

  /** 读大模型提供商配置（**不含密钥明文**，见 `settings:brain-config` 的说明）。 */
  function readBrainConfig(): {
    ok: boolean;
    active: string;
    label: string;
    baseUrl: string;
    model: string;
    models: string[];
    supportsThinking: boolean;
    keyConfigured: boolean;
    keyMasked: string;
    vendors: { name: string; label: string; baseUrl: string; model: string; models: string[] }[];
  } {
    const names = brainVendorNames();
    const active =
      String(readDeepScalar(CONFIG_ROOT, ['providers', 'brain', 'active'], true) ?? '').trim() ||
      names[0] ||
      'deepseek';

    const flatModel = String(
      readDeepScalar(CONFIG_ROOT, ['providers', 'brain', 'model'], true) ?? '',
    ).trim();
    const flatBase = String(
      readDeepScalar(CONFIG_ROOT, ['providers', 'brain', 'base_url'], true) ?? '',
    ).trim();

    const vendors = names.map((name) => {
      const label =
        String(
          readDeepScalar(CONFIG_ROOT, ['providers', 'brain', 'named', name, 'label'], true) ?? '',
        ).trim() || name;
      const baseUrl =
        String(
          readDeepScalar(
            CONFIG_ROOT,
            ['providers', 'brain', 'named', name, 'base_url'],
            true,
          ) ?? '',
        ).trim() || flatBase;
      const model =
        String(
          readDeepScalar(CONFIG_ROOT, ['providers', 'brain', 'named', name, 'model'], true) ?? '',
        ).trim() || flatModel;
      return { name, label, baseUrl, model, models: readBrainModels(name) };
    });

    const current = vendors.find((v) => v.name === active);
    const creds = readCredentials();
    const key = String(creds[active] ?? '').trim();
    return {
      ok: true,
      active,
      label: current?.label ?? active,
      baseUrl: current?.baseUrl ?? flatBase,
      model: current?.model ?? flatModel,
      models: current?.models ?? [],
      /*
       * 只有 DeepSeek 认 `thinking` 参数（见 gateway 的 `_thinking_flag`）。
       * 界面上那个"思考模式"开关对别的厂商不生效 —— 如实告诉用户，
       * 而不是让开关看着能点、实际没作用。
       */
      supportsThinking: active === 'deepseek',
      keyConfigured: Boolean(key),
      keyMasked: key.length > 9 ? `${key.slice(0, 5)}…${key.slice(-4)}` : '',
      vendors,
    };
  }

  /**
   * 读大模型提供商配置（用户要求 2026-10-08：
   * 「两个都要，要和现有的 deepseek 有互切选项」）。
   *
   * ## ⚠️ 绝不返回密钥明文
   *
   * 只回 `keyConfigured`（布尔）与 `keyMasked`（`sk-58…` 这种）。   * 设置页那个输入框用 `type="password"`，而且是"填了才覆盖"的语义 ——
   * 把明文回给渲染层等于让它在 DevTools 里裸奔。
   */

  ipcMain.handle('settings:brain-config', () => readBrainConfig());

  /**
   * 改大模型提供商：**切换厂商 + 改模型 + 存密钥**，三件事一次做完。
   *
   * 与 `settings:asr-set` 同一套三步：**落盘 -> 存密钥 -> 热更新**。
   */
  ipcMain.handle(
    'settings:brain-set',
    (
      _event,
      payload: { active?: string; model?: string; apiKey?: string; apiKeyVendor?: string },
    ) => {
      const patch: Record<string, unknown> = {};

      /*
       * 切换厂商：写 `providers.brain.active`。
       *
       * ⚠️ 只接受**配置里真实存在的**厂商标（`named` 的键）——
       * 否则 `active` 会指向一个不存在的名字，而 Python 那边会**静默退回
       * 平铺字段**（连的还是 DeepSeek），用户以为切了其实没切。
       * 那种"看着切了、实际没切"最难查，所以在入口就挡住。
       */
      const active = String(payload?.active ?? '').trim();
      if (active) {
        const names = brainVendorNames();
        if (!names.includes(active)) {
          return { ok: false, message: `未知的提供商 "${active}"（可用：${names.join(' / ')}）` };
        }
        patch.active = active;
      }

      // 改当前厂商的模型（写进 `named.<厂商>.model`）
      const vendor = active || readBrainConfig().active;
      const model = String(payload?.model ?? '').trim();
      if (model) patch.model = model;

      if (Object.keys(patch).length > 0) {
        for (const [key, value] of Object.entries(patch)) {
          const path =
            key === 'model'
              ? ['providers', 'brain', 'named', vendor, 'model']
              : ['providers', 'brain', key];
          const r = writeDeepScalar(CONFIG_ROOT, path, value as never);
          if (!r.ok) {
            log(`大模型配置写入失败（${key}）：${r.message}`);
            return { ok: false, message: `写入失败：${r.message}` };
          }
        }
      }

      /*
       * 密钥：按**厂商名**分槽位存（`credentials.json` 的键就是厂商名）。
       *
       * ⚠️ 这与 Python 的 `_credential_names()` 必须**对齐** ——
       * 那边第一个候选名字就是 `brain_active()`（厂商标）。
       * 不对齐的话会"界面显示已保存、大脑却报没密钥"。
       */
      const apiKey = String(payload?.apiKey ?? '').trim();
      const keyVendor = String(payload?.apiKeyVendor ?? vendor).trim() || vendor;
      if (apiKey) {
        const stored = readCredentials();
        stored[keyVendor] = apiKey;
        writeCredentials(stored);
        log(`大模型密钥已保存到凭据槽位 "${keyVendor}"`);
      }

      // 热更新 —— 改完立刻生效，不必重启 sidecar
      const after = readBrainConfig();
      const activeProfile = (after.vendors ?? []).find((v) => v.name === after.active);
      send({
        type: 'config.update',
        patch: {
          providers: {
            brain: {
              active: after.active,
              ...(activeProfile
                ? {
                    named: {
                      [after.active]: {
                        label: activeProfile.label,
                        base_url: activeProfile.baseUrl,
                        model: activeProfile.model,
                      },
                    },
                  }
                : {}),
            },
          },
        },
      });
      log(
        `大模型：active=${after.active} model=${after.model || '(未改)'} ` +
          `密钥=${apiKey ? '已更新' : '未改'}`,
      );

      return { ok: true, message: '已保存并立即生效。', config: readBrainConfig() };
    },
  );
  /** 开关"是否启用提示词"（用户要求：「提示词用户可自由选择可选可不选，
   * 所以要加个勾选，确认是否启动提示词」）。
   *
   * 与写提示词正文同样两步：**落盘 + 通知 sidecar 热更新**。
   * 关掉之后 Python 侧一个字的提示词都不加，完全按模型自己的性子说话。
   */
  ipcMain.handle('settings:set-prompt-enabled', (_event, payload: { enabled?: unknown }) => {
    const enabled = Boolean(payload?.enabled);
    const result = writePersonaEnabled(CONFIG_ROOT, enabled);
    log(`设置：提示词开关 -> ${enabled ? '启用' : '停用'}（${result.ok ? '已保存' : result.message}）`);
    if (result.ok) {
      send({ type: 'config.update', patch: { persona: { system_prompt_enabled: enabled } } });
    }
    return result;
  });

  /** 打开记忆管理窗口（设置页的按钮 + 宠物右键菜单都走它） */
  ipcMain.handle('settings:open-memory', () => {
    openMemoryManager();
    return { ok: true };
  });

  /** 朗读一段文字（记忆管理的「朗读」按钮）—— 用 Windows SAPI，见 speakText */
  ipcMain.handle('settings:speak', (_event, payload: unknown) => {
    const p = (payload ?? {}) as { text?: unknown; rate?: unknown };
    return speakText(String(p.text ?? ''), Number(p.rate) || 0);
  });

  /** 停止朗读 */
  ipcMain.handle('settings:stop-speak', () => {
    stopSpeaking();
    return { ok: true };
  });

  /**
   * 记忆管理窗口的一站式入口（设置页的「记忆」「珍藏」标签）。
   *
   * 一次性转发给 sidecar 并由它执行 —— 记忆数据只在 Python 那边（它持有
   * SQLite 连接），主进程不做中转处理，免得"两份真相"。
   *
   * 超时 15s：列表/搜索是本地查询，正常几毫秒；给足余量是为了**首次**调用
   * （`VACUUM` / 索引构建之类可能慢一点）。
   */
  /**
 * 模块开关（2026-10-18，模块化阶段二）。
 *
 * 两道 IPC：
 *   `settings:modules`     查状态（转发给大脑 modules.query）
 *   `settings:set-module`  试切一个（先热切、成功才落盘）
 *
 * 允许热切的只有 5 个：voice / autonomy / monologue / screen / search。
 * 其余是地基或前端模块，界面上置灰 —— 也不在这里接受写入。
 */
const HOT_MODULES = new Set(['voice', 'autonomy', 'monologue', 'screen', 'search', 'sprite', 'ui.windows']);

/**
 * 写一个模块开关到 `persona.yaml`（2026-10-18）。
 *
 * ## 为什么不用 `writeDeepScalar`
 *
 * 实测它对 `modules.sprite.enabled` 返回 `ok: true` 却**没改文件**
 * （同一个函数写 `modules."ui.windows".enabled` 又成功了）。
 * 与其在这里猜通用写入器的缩进栈逻辑，不如给模块开关一个确定性的实现：
 *
 *   1. 找到顶层 `modules:` 行
 *   2. 在它下面找 `  <name>:`（缩进 2）
 *   3. 把紧接着的 `    enabled:` 改成目标值；没有就插一行
 *
 * 找不到模块就返回 ok:false —— **绝不假装成功**（那正是"假开关"的来源）。
 */
function writeModuleFlag(name: string, enabled: boolean): { ok: boolean; message?: string } {
  const file = path.join(CONFIG_ROOT, 'config', 'persona.yaml');
  let raw: string;
  try {
    raw = readFileSync(file, 'utf8');
  } catch (error) {
    return { ok: false, message: `读取失败：${(error as Error).message}` };
  }
  const eol = raw.includes('\r\n') ? '\r\n' : '\n';
  const lines = raw.split(/\r?\n/);

  const modStart = lines.findIndex((l) => /^modules:\s*$/.test(l));
  if (modStart < 0) return { ok: false, message: 'persona.yaml 里没有 modules: 段' };

  // 段尾：下一个顶层键（缩进 0）
  let modEnd = lines.length;
  for (let i = modStart + 1; i < lines.length; i += 1) {
    if (/^\S/.test(lines[i])) {
      modEnd = i;
      break;
    }
  }

  const keyLine = `  ${name}:`;
  const keyIdx = lines.findIndex((l, i) => i > modStart && i < modEnd && l.trimEnd() === keyLine);
  if (keyIdx < 0) return { ok: false, message: `modules 段里没有 ${name}` };

  const value = enabled ? 'true' : 'false';
  for (let i = keyIdx + 1; i < modEnd; i += 1) {
    const m = /^(\s{4})enabled:\s*(?:true|false)\s*$/.exec(lines[i]);
    if (m) {
      lines[i] = `${m[1]}enabled: ${value}`;
      writeFileSync(file, lines.join(eol), 'utf8');
      return { ok: true };
    }
    // 已经到下一个模块了（缩进 2 的键）-> 说明这个模块没有 enabled 行
    if (/^\s{2}\S/.test(lines[i])) break;
  }
  // 没有 enabled 行：插在模块名下
  lines.splice(keyIdx + 1, 0, `    enabled: ${value}`);
  writeFileSync(file, lines.join(eol), 'utf8');
  return { ok: true };
}

ipcMain.handle('settings:modules', async () => {
  const reply = await requestFromBrain({ type: 'modules.query' }, 8_000);
  if (!reply || reply.type !== 'modules.state') {
    return { ok: false, message: '大脑没有响应（可能未启动）', modules: [] };
  }
  return reply;
});

ipcMain.handle(
  'settings:set-module',
  async (_event, payload: { name?: unknown; enabled?: unknown }) => {
    const name = String(payload?.name ?? '').trim();
    const enabled = Boolean(payload?.enabled);
    if (!HOT_MODULES.has(name)) {
      return { ok: false, message: `「${name}」暂不支持热切换（重启后在配置文件里改）` };
    }
    // ① 先热切：失败就整件事没发生（不写盘）
    const reply = await requestFromBrain({ type: 'modules.set', name, enabled }, 15_000);
    if (!reply || reply.type !== 'modules.state') {
      return { ok: false, message: '大脑没有响应（可能未启动）' };
    }
    if (!reply.ok) return reply;
    // ② 成功才落盘
    const written = writeModuleFlag(name, enabled);
    if (!written.ok) return { ok: false, message: written.message, modules: reply.modules };
    // ③ 推补丁，让大脑重读注册表（热切的副作用已经做过了，这里只同步认知）
    send({ type: 'config.update', patch: { modules: { [name]: { enabled } } } });
    // 前端模块（立绘/气泡）立刻按新状态重刷，不用等重启
    applyModulesToAllWindows();
    return reply;
  },
);

ipcMain.handle('settings:memory-action', async (_event, payload: unknown) => {
    const p = (payload ?? {}) as { action?: unknown; payload?: unknown };
    const action = String(p.action ?? '').trim();
    if (!action) return { ok: false, message: '缺少 action' };
    const reply = await requestFromBrain(
      { type: 'memory.query', action, payload: p.payload ?? {} },
      15_000,
    );
    if (!reply) return { ok: false, message: '大脑没有响应（可能未启动）' };
    if (reply.type !== 'memory.result') {
      return { ok: false, message: `意外的回执：${reply.type}` };
    }
    return reply;
  });

  /**
   * 读全部提示词模板（供设置页渲染） */
  ipcMain.handle('settings:prompt-templates', () => readPromptTemplates());

  /**
   * 写一条提示词模板。
   *
   * 与提示词正文同样两步：**落盘 `persona.yaml` + 通知 sidecar 热更新**。
   * 只热更新 = 重启丢；只落盘 = 本次不生效。
   */
  ipcMain.handle(
    'settings:set-prompt-template',
    (
      _event,
      payload: { section?: unknown; sub?: unknown; key?: unknown; value?: unknown },
    ) => {
      const section = String(payload?.section ?? '').trim();
      const sub = String(payload?.sub ?? '').trim();
      const key = String(payload?.key ?? '').trim();
      const value = String(payload?.value ?? '');
      if (!section || !key) return { ok: false, message: '缺少 section/key' };

      const result = sub
        ? writeNestedScalar(CONFIG_ROOT, section, sub, key, value)
        : writeScalar(CONFIG_ROOT, section, key, value);
      log(
        `设置：提示词模板 ${section}${sub ? `.${sub}` : ''}.${key} -> ` +
          `${result.ok ? '已保存' : result.message}`,
      );
      if (result.ok) {
        // 下发时保持配置的层级形状（sidecar 的 apply_patch 按层合并）
        const patch: Record<string, unknown> = sub
          ? { [section]: { [sub]: { [key]: value } } }
          : { [section]: { [key]: value } };
        send({ type: 'config.update', patch });
      }
      return result;
    },
  );

  /**
   * 读取记忆相关设置（`persona.yaml` 的 `memory:` 段）。
   *
   * 这些参数**只能手改文件**直到现在 —— 用户要调试"最近记忆窗口"，
   * 所以把它和备份配置一起暴露到设置页。
   */
  ipcMain.handle('settings:memory-config', () => {
    const section = (lastConfigSnapshot?.memory ?? {}) as Record<string, unknown>;
    const backup = (section.backup ?? {}) as Record<string, unknown>;
    const clamp = (v: unknown, lo: number, hi: number, fallback: number): number => {
      const n = Number(v);
      if (!Number.isFinite(n)) return fallback;
      return Math.max(lo, Math.min(hi, Math.round(n)));
    };
    /*
     * ## ⚠️ 一律「**文件优先，快照兜底**」（2026-10-05 实测教训）
     *
     * 原来只有备份那几项读文件，窗口天数读的是 `lastConfigSnapshot`（sidecar 回传的）。
     * 而 sidecar 的回报是**异步**的，链路是：
     *
     *     写文件 -> 发给 sidecar -> sidecar 应用补丁 -> 回报快照
     *       -> 主进程更新 `lastConfigSnapshot` -> 这里才读到新值
     *
     * 结果实测：输入 45 天保存后，配置里确实是 45，**但界面读回 30**（上一次的值），
     * 而且等 8 秒都不变 —— 看起来像"我输了 45，它自己变回去了"。
     *
     * 设置页要显示的是"**文件里写着什么**"（那才是重启后生效的真源），
     * 快照只是"sidecar 现在手里是什么"。两者不一致时以前者为准。
     * 快照仅作为"文件里没这一项"时的兜底。
     */
    const fromFile = (key: string): unknown => readScalar(CONFIG_ROOT, 'memory', key);
    /*
     * ## ⚠️ 「最近记忆 · 时间窗口（天）」已换成「上下文预算（token）」
     *
     * 2026-10-08 用户要求：
     *
     *   「把它改成可记忆设置的上下文 token 吧，更易懂点」
     *
     * 旧的三项（`recent_window_days` / `_min_days` / `_max_days`）
     * **仍然从配置里读**（为了不让老配置文件报错），但设置页不再显示它们 ——
     * 那段提示词注入已经删掉了，改由 `providers.brain.history_token_budget`
     * 控制灌回多少历史。
     *
     * 新值的合法范围：1000 ~ 100000 token。
     * 上限 10 万 token 是为了**不把请求撑爆**（不是模型窗口的极限 ——
     * DeepSeek 现在是 1M，但每轮都发 10 万 token 的钱和延迟都不划算）。
     */
    const HISTORY_TOKEN_MIN = 1000;
    const HISTORY_TOKEN_MAX = 100000;
    /*
     * 天数窗口（用户 2026-10-08 定的设计）：
     * 「不如直接按照最近记忆，设置多少天的记忆内容，重启宠物时灌注最近的
     *   设置记忆内容即可」
     *
     * 与 token 预算的分工（用户已确认「天数为主，预算当安全上限」）：
     *   天数 -> 决定**取多少**
     *   预算 -> 决定**最多发多少**（超了只发最新那部分，界面提示"已截断"）
     */
    const HISTORY_DAYS_MAX = 100000;
    // ⚠️ `providers` 也要像 `section` 一样从快照取 —— 预算配在 `providers.brain` 下
    const providers = (lastConfigSnapshot?.providers ?? {}) as Record<string, unknown>;
    const brainSection = (providers.brain ?? {}) as Record<string, unknown>;
    const brainFromFile = (key: string): unknown =>
      readDeepScalar(CONFIG_ROOT, ['providers', 'brain', key], true);
    return {
      historyDays: clamp(
        fromFile('history_days') ?? section.history_days,
        0,
        HISTORY_DAYS_MAX,
        30,
      ),
      historyDaysMax: HISTORY_DAYS_MAX,
      historyTokenBudget: clamp(
        brainFromFile('history_token_budget') ?? brainSection.history_token_budget,
        HISTORY_TOKEN_MIN,
        HISTORY_TOKEN_MAX,
        16000,
      ),
      historyTokenMin: HISTORY_TOKEN_MIN,
      historyTokenMax: HISTORY_TOKEN_MAX,
      /*
       * 当前厂商的上下文窗口 —— 供设置页算"预算占窗口的百分比"。
       *
       * ⚠️ **不硬编码在界面里**（2026-10-08 用户指出官方已写 1M）。
       * 我之前在 `settings.js` 写死 `128000`，两个问题：
       *   · 数字会过时（现价目表是 1M）
       *   · **切到硅基流动时窗口完全不同**（32K 量级），写死会让百分比彻底失真
       * 所以放在 `named.<厂商>.context_window`，这里读出来传给界面。
       */
      brainContextWindow: (() => {
        const active = String(brainFromFile('active') ?? brainSection.active ?? 'deepseek');
        const named = (brainSection.named ?? {}) as Record<string, Record<string, unknown>>;
        const fromNamed = named[active]?.context_window;
        const n = Number(fromNamed);
        return Number.isFinite(n) && n > 0 ? n : 0;
      })(),
      brainLabel: (() => {
        const active = String(brainFromFile('active') ?? brainSection.active ?? 'deepseek');
        const named = (brainSection.named ?? {}) as Record<string, Record<string, unknown>>;
        return String(named[active]?.label ?? active);
      })(),
      // 备份配置同样文件优先（真源是文件 —— Python 启动时读它）
      backupEnabled: readScalar(CONFIG_ROOT, 'memory', 'enabled') !== null
        ? Boolean(readNestedScalar(CONFIG_ROOT, 'memory', 'backup', 'enabled'))
        : Boolean(backup.enabled),
      backupDir: String(readNestedScalar(CONFIG_ROOT, 'memory', 'backup', 'dir') ?? backup.dir ?? ''),
      backupKeep: clamp(
        readNestedScalar(CONFIG_ROOT, 'memory', 'backup', 'keep') ?? backup.keep,
        0,
        999,
        0,
      ),
      /*
       * 备份文件夹的**现状**（有几份、共多少 MB）。
       *
       * 单独给出来是因为"保留份数"这个设置只有看到实际占用才有意义 ——
       * 用户要能自己判断"该不该限制"。只给一个数字输入框的话，
       * 他没法知道设成 0（保留全部）到底会占多少。
       */
      backupInfo: describeBackupFolder(
        String(readNestedScalar(CONFIG_ROOT, 'memory', 'backup', 'dir') ?? backup.dir ?? ''),
      ),
      /*
       * `dbPath` 要给**真实路径**，不能把配置里的字面量 `auto` 原样抛给界面 ——
       * 用户点开设置是想知道"我的记忆到底存在哪个文件"，`auto` 对他毫无信息量，
       * 而且他也没法据此去备份/复制。
       */
      dbPath: (() => {
        const raw = String(section.path ?? '').trim();
        if (raw && raw !== 'auto') return raw;
        // 默认落点与 Python 侧 `store.default_db_path` 必须一致
        return path.join(
          DATA_DIR,
          'memory',
          'pet.db',
        );
      })(),
    };
  });

  /**
   * 读对话历史的用量统计（设置页那行"实时数字"）。
   *
   * ## 用户要求（2026-10-08）
   *
   * > 「最近记忆可以按照设置的天数自动搜索数据库取得字数，计算出总 token
   * >   消耗，再和云端的大模型上下文计算，看总占比。」
   *
   * ## ⚠️ 为什么走 `python -m pet.usage` 而不是在 Node 里直查
   *
   * `node:sqlite` 是 Node 22+ 才有的，而 **Electron 32 内置的是 Node 20** ——
   * 主进程里根本没有这个模块（实测过）。所以只能问 Python。
   *
   * 而 `pet.usage` 是**刻意做的轻量入口**：它不 import voice / brains /
   * gateway，所以实测 **~500ms**（走 `pet.selftest` 那条路要 3 秒，
   * 拖个输入框等 3 秒才出数字，体验很糟）。
   *
   * ⚠️ 用 `execFile`（异步）而不是 `execFileSync` —— 同步版会卡住整个主进程
   * （界面冻结），而这个调用要半秒。
   */
  ipcMain.handle('settings:history-usage', async (_event, payload: { days?: unknown }) => {
    const days = Math.max(0, Math.min(100000, Math.round(Number(payload?.days) || 0)));
    /*
     * ⚠️ 预算和窗口**从这里直接读配置**，不调用同目录里那几个读取函数。
     *
     * 试过抽公共函数，但 `HISTORY_TOKEN_*` 那几个常量是定义在
     * `settings:memory-config` 的**函数体内**的（局部作用域），
     * 抽出来会连带一串作用域问题 —— 而这里只用到两个数字。
     *
     * 两处都读 `persona.yaml`（而不是一个读文件、一个读快照），
     * 所以不会出现"设置页显示的和统计用的不是同一个值"。
     */
    const brainCfg = readBrainConfig();
    const budget = Math.max(
      0,
      Math.round(Number(readDeepScalar(CONFIG_ROOT, ['providers', 'brain', 'history_token_budget'], true) ?? 0)),
    );
    // 上下文窗口：从 named.<active>.context_window 直读（与设置页同源）
    const activeVendor = String(
      readDeepScalar(CONFIG_ROOT, ['providers', 'brain', 'active'], true) ?? 'deepseek',
    );
    const contextWindow =
      Math.round(
        Number(
          readDeepScalar(
            CONFIG_ROOT,
            ['providers', 'brain', 'named', activeVendor, 'context_window'],
            true,
          ) ?? 0,
        ),
      ) || 0;
    try {
      const python = findPython(PROJECT_ROOT);
      const out = await new Promise<string>((resolve, reject) => {
        execFile(
          python,
          [
            '-m',
            'pet.usage',
            '--config',
            path.join(CONFIG_ROOT, 'config', 'persona.yaml'),
            '--days',
            String(days),
            '--budget',
            String(budget),
            '--json',
          ],
          {
            cwd: PROJECT_ROOT,
            // `pet` 包在 apps/brain 下，靠 PYTHONPATH 才找得到（与 sidecar 同理）
            env: { ...process.env, PYTHONPATH: path.join(PROJECT_ROOT, 'apps', 'brain') },
            timeout: 15000,
            windowsHide: true,
          },
          (err, stdout) => (err ? reject(err) : resolve(stdout)),
        );
      });
      // 只取最后一行 JSON（前面可能有 warning）
      const line = out
        .split(/\r?\n/)
        .map((l) => l.trim())
        .filter((l) => l.startsWith('{'))
        .pop();
      if (!line) return { ok: false, message: '统计输出里没有 JSON', window: contextWindow };
      return { ok: true, ...(JSON.parse(line) as object) };
    } catch (error) {
      log(`统计对话用量失败：${(error as Error).message}`);
      return { ok: false, message: (error as Error).message, window: contextWindow };
    }
  });

  /**
   * 写记忆设置。
   *
   * 与提示词同样两步：**落盘 `persona.yaml` + 发 `config.update` 热更新**。
   * 只热更新 = 重启丢；只落盘 = 本次不生效（备份只在启动时做，所以落盘是必须的）。
   */
  ipcMain.handle(
    'settings:set-memory-config',
    (
      _event,
      payload: {
        historyDays?: unknown;
        historyTokenBudget?: unknown;
        backupEnabled?: unknown;
        backupDir?: unknown;
        backupKeep?: unknown;
      },
    ) => {
      const written: string[] = [];
      const failed: string[] = [];
      const patch: Record<string, unknown> = {};

      if (payload?.historyDays !== undefined) {
        // 下限 0 = 不限天数（仍受 token 预算截断）
        const days = Math.max(
          0,
          // ⚠️ 字面量而不是常量：HISTORY_DAYS_MAX 定义在
          // settings:memory-config 的函数体里（局部作用域），这里用不到。
          // 两处必须一致 —— 改一处要改两处。
          Math.min(100000, Math.round(Number(payload.historyDays) || 0)),
        );
        const r = writeScalar(CONFIG_ROOT, 'memory', 'history_days', days);
        r.ok ? written.push(days > 0 ? `记忆 ${days} 天` : '记忆不限天数') : failed.push(r.message);
        if (r.ok) patch.history_days = days;
      }

      if (payload?.historyTokenBudget !== undefined) {
        /*
         * ⚠️ 写入口的硬上限必须与读入口、输入框 `max`、`persona.yaml` **四处一致**。
         *
         * 原来这里是 3650 而输入框允许 100000 —— 后果是「界面接受了、文件里却写成
         * 3650」，用户看到的是"我填的值被偷偷改了"。所以这四处要一起改。
         *
         * ⚠️ 注意这笔写入落在 **`providers.brain`**，不是 `memory` ——
         * 这个预算控制的是"发给模型多少历史"（见 `Conversation.history_token_budget`），
         * 属于大脑配置。热更新的补丁路径也要跟着对。
         */
        const tokens = Math.max(
          1000,
          Math.min(100000, Math.round(Number(payload.historyTokenBudget) || 16000)),
        );
        const r = writeDeepScalar(
          CONFIG_ROOT,
          ['providers', 'brain', 'history_token_budget'],
          tokens,
        );
        r.ok ? written.push(`上下文预算 ${tokens} token`) : failed.push(r.message);
        if (r.ok) patch.history_token_budget = tokens;
      }
      if (payload?.backupEnabled !== undefined || payload?.backupDir !== undefined) {
        const enabled = Boolean(payload.backupEnabled);
        const dir = String(payload.backupDir ?? '').trim();
        const r1 = writeNestedScalar(CONFIG_ROOT, 'memory', 'backup', 'enabled', enabled);
        const r2 = writeNestedScalar(CONFIG_ROOT, 'memory', 'backup', 'dir', dir);
        r1.ok && r2.ok
          ? written.push(enabled ? `备份开启（${dir || '未选文件夹'}）` : '备份关闭')
          : failed.push(r1.message || r2.message);
        if (r1.ok && r2.ok) {
          patch.backup = { enabled, dir };
        }
      }
      if (payload?.backupKeep !== undefined) {
        const keep = Math.max(1, Math.min(200, Math.round(Number(payload.backupKeep) || 20)));
        const r = writeNestedScalar(CONFIG_ROOT, 'memory', 'backup', 'keep', keep);
        r.ok ? written.push(`保留 ${keep} 份`) : failed.push(r.message);
        if (r.ok) patch.backup = { ...(patch.backup as object), keep };
      }

      log(`设置：记忆配置 ${written.join('，') || '(无改动)'}${failed.length ? ` / 失败：${failed.join('；')}` : ''}`);
      /*
       * ⚠️ 补丁要**分成两棵树**发（2026-10-08）。
       *
       * `history_token_budget` 属于 `providers.brain`（管"发给模型多少历史"），
       * 备份那几项属于 `memory`。早先只有一个 `memory` 分支，我加预算时如果
       * 顺手塞进去，Python 那边会在 `memory` 下找一个根本不存在的键 ——
       * 表现为"保存成功但没生效"，最难查的那种。
       */
      if (Object.keys(patch).length) {
        const { history_token_budget: tokenBudget, ...memoryPatch } = patch;
        const fullPatch: Record<string, unknown> = {};
        if (Object.keys(memoryPatch).length) fullPatch.memory = memoryPatch;
        if (tokenBudget !== undefined) fullPatch.providers = { brain: { history_token_budget: tokenBudget } };
        send({ type: 'config.update', patch: fullPatch });
      }
      return {
        ok: failed.length === 0,
        message: failed.length ? failed.join('；') : written.join('，') || '已保存',
      };
    },
  );

  /**
   * 立刻做一次备份（不等下次启动）。
   *
   * 用户在设置页点"立即备份"时用 —— 让 sidecar 执行，因为**只有它持有打开的数据库连接**
   * （用 `VACUUM INTO` 才能拿到一致快照；在 Electron 侧直接复制文件可能拿到半个库）。
   */
  ipcMain.handle('settings:backup-now', async () => {
    const reply = await requestFromBrain({ type: 'memory.backup' }, 25_000);
    if (!reply) return { ok: false, message: '大脑没有响应（可能未启动）' };
    if (reply.type !== 'memory.backup.done') {
      return { ok: false, message: `意外的回执：${reply.type}` };
    }
    if (!reply.ok) return { ok: false, message: String(reply.message ?? '备份失败') };
    // 两段式：Python 只生成了快照（在它写得进去的地方），搬运由这里做
    if (reply.staged) return finishStagedBackup(reply);
    return { ok: true, message: String(reply.message ?? '已备份') };
  });

  /**
   * 打开系统"选择文件夹"对话框。
   *
   * 用 `showOpenDialog` 而不是让用户手打路径：Windows 路径又长又容易错，
   * 而备份文件夹选错地方是"用户以为有备份、其实没有"的典型成因。
   */
  ipcMain.handle('settings:choose-folder', async () => {
    const win = settingsWindow?.browserWindow;
    const options = {
      title: '选择记忆备份文件夹',
      properties: ['openDirectory', 'createDirectory'] as const,
      buttonLabel: '用这个文件夹',
    };
    const result = win
      ? await dialog.showOpenDialog(win, { ...options, properties: [...options.properties] })
      : await dialog.showOpenDialog({ ...options, properties: [...options.properties] });
    if (result.canceled || result.filePaths.length === 0) return { ok: false, path: '' };
    return { ok: true, path: result.filePaths[0] };
  });

  /**
   * 写人格提示词。
   *
   * 两步：① **落盘**到 `config/persona.yaml`（保证重启后仍在）；
   * ② 发 `config.update` 让 sidecar **热更新**（不然要重启才生效）。
   *
   * ⚠️ 必须是"落盘 + 热更新"两件都做：只热更新 = 重启丢；只落盘 = 本次不生效。
   * （这正是 `providers` 那套踩过的坑，见 `settings:save` 的注释。）
   */
  ipcMain.handle('settings:set-prompt', (_event, payload: { prompt?: string }) => {
    const prompt = String(payload?.prompt ?? '');
    const result = writePersonaPrompt(CONFIG_ROOT, prompt);
    log(`设置：保存人格提示词 ${result.ok ? '成功' : `失败 —— ${result.message}`}（${result.path}）`);
    if (result.ok) {
      // 从盘上读回"权威值"（含 `{name}` 占位符的原样内容）再下发，避免两端不一致
      const fresh = readPersonaPrompt(CONFIG_ROOT);
      send({ type: 'config.update', patch: { persona: { system_prompt: fresh.prompt } } });
    }
    return result;
  });

  /**
   * 最高条例：读 / 写（2026-10-25）。
   *
   * ## 🔴 为什么单独一对 IPC，而不是塞进 settings:save
   *
   * 用户原话：「条例写在提示词中，用户可自由更改，但要提醒用户条例的重要性，
   * 不亚于为宠物定义作为人的感受与思维方式。」
   *
   * 它是**提示词**，和人格底子同源（落 `persona.yaml`），
   * 但**不受 `system_prompt_enabled` 影响** —— 关掉人格不等于关掉思考方式。
   * 所以独立一对接口，语义清楚。
   *
   * ## 和 `settings:set-prompt` 一样两步
   *
   * ① 落盘（`writeList`）—— 重启后还在；
   * ② 发 `config.update` —— 热更新，不用重启。
   *
   * ⚠️ 空列表**是合法的**（用户清空 = 取消约束）。
   *    设置页会拦一下确认，但主进程不拦 —— 用户已经确认过了。
   */
  ipcMain.handle('settings:get-charter', () => readList(CONFIG_ROOT, 'charter'));

  ipcMain.handle('settings:set-charter', (_event, payload: { rules?: unknown }) => {
    const raw = payload?.rules;
    const rules = Array.isArray(raw) ? raw.map((item) => String(item ?? '').trim()).filter(Boolean) : [];
    const result = writeList(CONFIG_ROOT, 'charter', rules);
    log(
      `设置：保存最高条例 ${result.ok ? '成功' : `失败 —— ${result.message}`}` +
        `（${rules.length} 条，${result.path}）`,
    );
    if (result.ok) {
      // 读回权威值再下发，避免两端不一致（和人格提示词同一套做法）
      const fresh = readList(CONFIG_ROOT, 'charter');
      send({ type: 'config.update', patch: { charter: fresh } });
      return { ...result, rules: fresh };
    }
    return { ...result, rules };
  });

  ipcMain.on('settings:quit', () => void quit());
  ipcMain.on('settings:quit-all', () => void quitAll());
}

/** 当前可用的密钥（环境变量优先，与 Python 侧一致） */
function currentApiKey(): string {
  const providers = (lastConfigSnapshot?.providers ?? {}) as Record<string, unknown>;
  const brainSection = (providers.brain ?? {}) as Record<string, unknown>;
  const envName = String(brainSection.api_key_env ?? 'DEEPSEEK_API_KEY');
  const fromEnv = (process.env[envName] ?? '').trim();
  if (fromEnv) return fromEnv;
  const stored = readCredentials();
  return Object.values(stored).find((value) => value.trim()) ?? '';
}

/**
 * 复制文件，**遇到瞬时占用时退避重试**。
 *
 * ## 为什么需要重试（实测症状）
 *
 * 用户报「立即备份」间歇性失败，错误是 `EPERM: operation not permitted, copyfile`：
 *
 *     18:20:49  ✅ 成功
 *     18:24:20  ❌ EPERM        （同一个文件夹、同一份代码）
 *
 * `EPERM` 而不是 `EACCES` 是关键线索 —— 它表示**操作此刻不被允许**，
 * 典型原因是**杀毒软件/实时防护正在扫描刚写出的那个文件**
 * （备份文件是 Python 侧刚用 `VACUUM INTO` 生成的，落地才几毫秒）。
 * 扫描期间文件被短暂独占，复制就失败；等一下再试就好了。
 *
 * 为了"少一次瞬时占用就永久失败"这种蠢事，这里退避重试几次。
 * 每次间隔递增（60/150/300/600ms），总共最多约 1.1 秒 —— 对用户不可感知，
 * 但足以跨过多数扫描窗口。
 */
function copyWithRetry(src: string, dest: string, attempts = 5): void {
  let lastError: unknown = null;
  for (let i = 0; i < attempts; i += 1) {
    try {
      fs.copyFileSync(src, dest);
      return;
    } catch (error) {
      lastError = error;
      const code = (error as { code?: string }).code ?? '';
      // 只有"瞬时"类错误值得重试；路径不存在 / 磁盘满之类重试也没用
      if (code !== 'EPERM' && code !== 'EBUSY' && code !== 'EACCES') throw error;
      if (i < attempts - 1) {
        const waitMs = 60 * Math.pow(2, i) + Math.floor(Math.random() * 40);
        // 同步等待：这一步在 IPC 处理里，本来就要等结果，不必异步化
        const until = Date.now() + waitMs;
        while (Date.now() < until) {
          /* 自旋等待，避开 Node 里把简单流程异步化的复杂度 */
        }
      }
    }
  }
  throw lastError;
}

/**
 * 把 sidecar 生成的**快照**复制到目标文件夹（两段式备份的第二段）。
 *
 * ## 为什么需要这一段
 *
 * 实测：**Python 子进程对用户选的备份文件夹没有写权限，而主进程有** ——
 * 同一台机器、同一个文件夹，Python 报 `PermissionError`，主进程复制成功。
 * 所以 `VACUUM INTO` 先在 Python 写得进去的地方（项目内 `_staging`）生成一致快照，
 * 由这里搬过去。搬运工换了人，快照仍然一致完整。
 *
 * 搬完**删掉 staging 文件** —— 否则它会一直堆着，而用户以为备份都在他选的文件夹里。
 */
function finishStagedBackup(reply: Record<string, unknown>): { ok: boolean; message: string } {
  const staged = String(reply.path ?? '');
  const target = String(reply.target ?? '');
  if (!staged || !target) {
    return { ok: false, message: `快照路径缺失（staged=${staged} target=${target}）` };
  }
  const stamp = path.basename(staged);
  const dest = path.join(target, stamp);
  try {
    fs.mkdirSync(target, { recursive: true });
    copyWithRetry(staged, dest);
  } catch (error) {
    /*
     * 搬运失败要**把 staging 也删掉**，否则它会一直堆着 —— 而用户以为
     * "备份都在我选的文件夹里"，实际有一部分留在了项目内、越积越多。
     *
     * 快照本身是有价值的，但"用户看不见的备份"等于没有备份，
     * 还不如让他明确看到失败、再试一次。
     */
    try {
      fs.unlinkSync(staged);
    } catch {
      /* 删不掉也只能留着 */
    }
    const reason = (error as Error).message;
    const isPerm = (error as { code?: string }).code === 'EPERM' || reason.includes('EPERM');
    const hint = isPerm
      ? '（已重试 5 次仍被拒。常见原因是杀软正在扫描刚生成的文件；稍后再点一次通常就好。' +
        '也可以在杀软里给这个备份文件夹加白名单）'
      : '';
    return { ok: false, message: `复制到备份文件夹失败：${reason}${hint}` };
  }
  try {
    fs.unlinkSync(staged);
  } catch {
    /* 删不掉不影响"备份已生成"这个事实 */
  }
  const kb = Math.round((fs.existsSync(dest) ? fs.statSync(dest).size : 0) / 1024);
  return { ok: true, message: `已备份（${kb} KB）→ ${dest}` };
}

/**
 * 可编辑的提示词模板清单。
 *
 * ## 为什么是"固定清单"而不是"动态读 `prompts:` 段"
 *
 * 动态读的好处是"配置里加了新 key 就自动出现在界面上"，但代价是：
 *   · 没法给每条配**中文标签和说明**（用户看到 `fixed_header` 不知道那是什么）；
 *   · 没法标出**哪些占位符可用**（`{styles}` / `{days}` 各不相同）；
 *   · 顺序不可控（YAML 的键序虽然稳定，但不适合当 UI 顺序）。
 *
 * 这三样对"用户能改对"比"自动发现"重要得多，所以用固定清单。
 * 代价是新增模板要来这里补一行 —— 那是**有意的一步**：
 * 它逼着加模板的人同时想清楚"标签怎么写、说明怎么写"。
 *
 * ## ⚠️ 记忆相关的两条**故意不在这里**（2026-10-05 用户要求）
 *
 * 用户原话：「用记忆管理中的珍藏记忆管理来接管提示词中的记忆珍藏的功能，
 * 所以提示词中它只用写在记忆管理中的珍藏记忆管理即可，此处纯文本」
 * （固定注入开头同理）。
 *
 * 所以 `cherished_header` 与 `fixed_header` **不列在这里** —— 设置页只显示
 * 一句"在记忆管理页面中管理"的说明。它们仍然存在于 `persona.yaml` 里作为
 * 内置默认文案（`memory2.py` 的 `_render` 会兜底），只是不再是个可编辑项。
 *
 * 这样做的理由：那两段的内容**由记忆数据决定**（你珍藏了什么、固定注入了什么），
 * 而"开头那句话"几乎没人会改。把它摆在设置页只会让人以为"改了这里就能控制
 * 记住什么"，反而误导 —— 真正该去的地方是记忆管理页面。
 */
const PROMPT_FIELDS: readonly {
  key: string;
  section: string;
  sub?: string;
  label: string;
  hint: string;
  placeholders: string;
  multiline: boolean;
}[] = [
  {
    key: 'speech_style',
    section: 'prompts',
    label: '说话风格',
    hint: '由「说话风格」列表自动拼成，可用 {styles} 占位。',
    placeholders: '{styles}',
    multiline: false,
  },
  {
    key: 'idle_topics',
    section: 'prompts',
    label: '主动聊起的话题',
    hint: '由「主动话题」列表自动拼成，可用 {topics} 占位。',
    placeholders: '{topics}',
    multiline: false,
  },
  {
    key: 'emotion',
    section: 'prompts',
    label: '情绪状态',
    hint: '每轮注入的实时情绪。{energy} 精力 / {mood} 心情 / {intimacy} 亲近度（0~1）。',
    placeholders: '{energy} {mood} {intimacy}',
    multiline: true,
  },
  {
    key: 'idle_occasion',
    section: 'prompts',
    label: '主动开口的场合说明',
    hint: '⚠️ 只有它区分"主动说话"与"被问到"。删掉容易让宠物说出客服话。',
    placeholders: '（无）',
    multiline: true,
  },
  {
    key: 'extract',
    section: 'prompts',
    label: '事实提炼（决定记住什么）',
    hint:
      '每轮对话结束后，用这段提示词让模型把对话压成长期事实。' +
      '⚠️ 请保持「每行一条」的输出约定，否则解析会退化。',
    placeholders: '（无）',
    multiline: true,
  },
  /*
   * ⚠️ `fixed_header` 与 `cherished_header` **故意不在这里**。
   *
   * 用户要求：记忆的珍藏与固定注入改由**记忆管理窗口**接管，
   * 设置页这两项只留一句"在记忆管理页面中管理"的纯文本说明。
   * 详细理由见 `PROMPT_FIELDS` 上方那段注释。
   */
  {
    key: 'recent_header',
    section: 'prompts',
    sub: 'memory',
    label: '记忆 · 最近对话的开头',
    hint: '{days} 是时间窗口天数。',
    placeholders: '{days}',
    multiline: true,
  },
  {
    key: 'recall_header',
    section: 'prompts',
    sub: 'memory',
    label: '记忆 · 检索结果的开头',
    hint: '与当前话题相关的记忆那一段的引导句。',
    placeholders: '（无）',
    multiline: false,
  },
  {
    key: 'tail',
    section: 'prompts',
    sub: 'memory',
    label: '记忆 · 统一约束',
    hint: '接在所有记忆之后，防止模型生硬复述。',
    placeholders: '（无）',
    multiline: true,
  },
];

/** 读全部提示词模板（供设置页的「提示词」标签渲染） */
function readPromptTemplates(): Record<string, unknown>[] {  return PROMPT_FIELDS.map((f) => {
    const value = f.sub
      ? readNestedScalar(CONFIG_ROOT, f.section, f.sub, f.key)
      : readScalar(CONFIG_ROOT, f.section, f.key);
    return {
      id: f.sub ? `${f.section}.${f.sub}.${f.key}` : `${f.section}.${f.key}`,
      section: f.section,
      sub: f.sub ?? '',
      key: f.key,
      label: f.label,
      hint: f.hint,
      placeholders: f.placeholders,
      multiline: f.multiline,
      // 读不到就是"配置里没有这一条"（会用内置默认），界面显示成空
      value: typeof value === 'string' ? value : value === null ? '' : String(value),
    };
  });
}

/**
 * 语音朗读（记忆管理的「朗读」按钮）。
 *
 * ## ⚠️ 为什么用 Windows 的 SAPI，而不是 `speechSynthesis`
 *
 * 渲染层有 `window.speechSynthesis`，但**在 Electron 里它是空壳** ——
 * 实测 `getVoices()` 返回 0 个声音，`speak()` 返回成功却没有任何声音。
 * Chromium 的那套依赖 Google 的在线语音服务，Electron 里没有接。
 *
 * 而 Windows 自带 SAPI：实测装了 `Microsoft Huihui Desktop`（zh-CN，女声），
 * 通过 `System.Speech.Synthesis` 调用，**不需要网络、不需要 API key**。
 *
 * ## 为什么把文本写进临时文件而不是当参数传
 *
 * 中文（以及引号、换行）作为 PowerShell 参数传递时会被**编码搞坏** ——
 * 实测在这台机器上中文参数会变成乱码，朗读出来是错的。
 * 写文件让 PowerShell 用 `-Encoding UTF8` 读，就没有这个问题。
 *
 * ## 音频怎么走
 *
 * 直接由 PowerShell 进程的默认输出设备播放，**不经过渲染层**。
 * 代价是音量只能用系统音量调；好处是不用把 PCM 搬来搬去。
 * 将来接上宠物自己的 TTS（`voice.py` 的 `TtsEngine`）时，这里会换成
 * 走 sidecar 合成 + 渲染层播放。
 */
let speakingProc: ChildProcess | null = null;

function stopSpeaking(): void {
  if (speakingProc && !speakingProc.killed) {
    try {
      speakingProc.kill();
    } catch {
      /* 已经退出了 */
    }
  }
  speakingProc = null;
}

/**
 * 朗读一段文字。返回是否成功**启动**（不等待读完）。
 *
 * `rate` 是 SAPI 的语速（-10 ~ 10，0 是正常）。
 */
function speakText(text: string, rate = 0): { ok: boolean; message: string } {
  const body = String(text || '').trim();
  if (!body) return { ok: false, message: '没有可朗读的内容' };
  // 上限保护：一次念几千字会念很久，而且用户多半是误点
  if (body.length > 4000) return { ok: false, message: '内容太长（超过 4000 字），先删减再朗读' };

  stopSpeaking();

  /*
   * ⚠️ 临时文件写在**项目内**，不用系统 temp。
   *
   * 实测：写 `app.getPath('temp')`（`C:\Users\…\AppData\Local\Temp`）会
   * `EPERM: operation not permitted` —— 与备份功能遇到的是同一类限制
   * （这个进程对用户目录没有写权限）。
   *
   * 项目内的 pet-data 目录是**已验证可写**的（记忆库、日志、备份都在那儿）。
   */
  const tmpDir = path.join(DATA_DIR, 'tts');
  const tmpFile = path.join(tmpDir, `speak-${Date.now()}.txt`);
  try {
    fs.mkdirSync(tmpDir, { recursive: true });
    writeFileSync(tmpFile, body, 'utf8');
  } catch (error) {
    return { ok: false, message: `准备朗读失败：${(error as Error).message}` };
  }

  // 用 `System.Speech` 的同步 `Speak`（进程结束时自然结束，不必轮询状态）
  const script =
    `$ErrorActionPreference='Stop';` +
    `Add-Type -AssemblyName System.Speech;` +
    `$t=[IO.File]::ReadAllText('${tmpFile.replace(/'/g, "''")}',[Text.Encoding]::UTF8);` +
    `$s=New-Object System.Speech.Synthesis.SpeechSynthesizer;` +
    `$s.Rate=${Math.max(-10, Math.min(10, Math.round(rate)))};` +
    // 优先中文声音；没有就退回默认（总比不出声好）
    `$zh=$s.GetInstalledVoices()|Where-Object{$_.VoiceInfo.Culture.Name -like 'zh*'}|Select-Object -First 1;` +
    `if($zh){$s.SelectVoice($zh.VoiceInfo.Name)};` +
    `$s.Speak($t);$s.Dispose()`;

  try {
    const proc = spawn(
      'powershell.exe',
      ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-Command', script],
      { windowsHide: true, stdio: 'ignore' },
    );
    speakingProc = proc;
    proc.on('exit', () => {
      if (speakingProc === proc) speakingProc = null;
      // 念完删掉临时文件（里面是用户的对话内容，不该留在 temp 里）
      try {
        if (existsSync(tmpFile)) unlinkSync(tmpFile);
      } catch {
        /* ignore */
      }
    });
    proc.on('error', (error) => {
      log(`朗读进程启动失败：${error.message}`);
      speakingProc = null;
    });
  } catch (error) {
    return { ok: false, message: `朗读失败：${(error as Error).message}` };
  }
  return { ok: true, message: '开始朗读' };
}

/**
 * 描述备份文件夹的现状：有几份、共占多少。
 *
 * 为什么不让渲染层自己去数：渲染层没有文件系统权限（CSP + 最小授权），
 * 而 `readdir` 是主进程独有的能力。
 *
 * 文件夹不存在/为空/读不了都返回一句人话 —— 界面上要能看懂，
 * 而不是显示"0 份"让人以为备份没生效。
 */
function describeBackupFolder(dir: string): { ok: boolean; text: string; count: number; bytes: number } {
  const folder = dir.trim();
  if (!folder) return { ok: false, text: '还没选备份文件夹', count: 0, bytes: 0 };
  try {
    if (!existsSync(folder)) {
      return { ok: false, text: `文件夹不存在：${folder}`, count: 0, bytes: 0 };
    }
    const files = readdirSync(folder).filter((f) => f.startsWith('pet-') && f.endsWith('.db'));
    if (files.length === 0) {
      return { ok: true, text: '还没有备份（下次打开宠物时会自动生成一份）', count: 0, bytes: 0 };
    }
    let bytes = 0;
    for (const f of files) {
      try {
        bytes += statSync(path.join(folder, f)).size;
      } catch {
        /* 单个文件读不到不影响整体统计 */
      }
    }
    const mb = bytes / 1024 / 1024;
    const size = mb >= 1 ? `${mb.toFixed(1)} MB` : `${Math.round(bytes / 1024)} KB`;
    return { ok: true, text: `已有 ${files.length} 份备份，共 ${size}`, count: files.length, bytes };
  } catch (error) {
    return { ok: false, text: `读不了这个文件夹：${(error as Error).message}`, count: 0, bytes: 0 };
  }
}

/** 当前生效的 brain 接口地址（设置页填过就用它，否则用 sidecar 快照里的） */
function currentBrainBaseUrl(): string {
  const providers = (lastConfigSnapshot?.providers ?? {}) as Record<string, unknown>;
  const brainSection = (providers.brain ?? {}) as Record<string, unknown>;
  const fromSnapshot = String(brainSection.base_url ?? '').trim();
  if (fromSnapshot) return fromSnapshot;
  /*
   * ⚠️ **快照还没到时直接读配置文件**（2026-10-12 补）。
   *
   * 消息顺序是 `socket.on('open')` 先于 `ready`，所以那一刻
   * `lastConfigSnapshot` 还是 null。第一版只好退回协议名，于是菜单里
   * 一直显示 `在线 · openai` —— 用户报「还是这个诶」。
   *
   * 而 `providers.brain.base_url` 本来就在 `config/persona.yaml` 里，
   * 直接读文件就能拿到。**不为一个显示用的字符串容忍"读不到就先算了"。**
   */
  return String(
    readDeepScalar(CONFIG_ROOT, ['providers', 'brain', 'base_url'], true) ?? '',
  ).trim();
}

/**
 * 当前大脑**服务商**的可读名字（给用户看的，2026-10-12 加）。
 *
 * ## 为什么需要它
 *
 * 用户报：「这个 openai 在线搞错了吧，现在用的是 deepseek 的接口，
 * 应该是 deepseek，用硅基流动的时候显示硅基流动的 api 就好了」。
 *
 * 原来的状态行写的是 `providers.brain.provider` 的原始值 —— 那是
 * **协议类型**（`openai` = 「任何 OpenAI 兼容接口」），不是服务商。
 * 于是不管接 DeepSeek 还是硅基流动，都显示 `openai`：
 * 对用户来说这是个**看不懂又没有用**的词。
 *
 * ## 判断逻辑在 `brainProvider.ts` 里
 *
 * 抽成纯模块是因为它该被断言（漏服务商 / 匹配太宽都会出错），
 * 而这个文件是**副作用模块**，测试里 import 不了。
 */
function brainStatusLabelNow(protocol: string): string {
  return brainStatusLabel(currentBrainBaseUrl(), protocol);
}

/** 按接口域名给凭据起名，便于以后支持多个供应商并存 */
function providerNameFor(baseUrl: string, fallback: string): string {  try {
    const host = new URL(baseUrl).hostname;
    const parts = host.split('.').filter((p) => p && !['api', 'www', 'com', 'cn'].includes(p));
    if (parts.length) return parts[0];
  } catch {
    /* 地址不合法就退回提供者名 */
  }
  return fallback || 'default';
}

function setPausedInternal(value: boolean, minutes: number): void {
  paused = value;
  if (pauseTimer) {
    clearTimeout(pauseTimer);
    pauseTimer = null;
  }
  if (value && minutes > 0) {
    pauseTimer = setTimeout(() => setPausedInternal(false, 0), minutes * 60_000);
    log(`进入免打扰 ${minutes} 分钟`);
  } else {
    log(value ? '进入免打扰（手动取消前一直有效）' : '已取消免打扰');
  }

  // 关键：把免打扰**交给 sidecar**，而不是只在前端把气泡藏起来。
  // 早先的实现只在渲染层屏蔽宠物发言，结果是"界面看着安静了，大脑还在
  // 不停生成并推送" —— 既费 token，也让"免打扰"名不副实。
  // 现在它落成 autonomy.muted 配置项，调度器直接不再开口。
  send({ type: 'config.update', patch: { autonomy: { muted: value } } });

  petWindow?.sendToRenderer('pet:paused', { paused: value, minutes });
}

async function reloadConfig(): Promise<void> {
  log('请求重新读取配置：重启 sidecar 以重新加载 persona.yaml');
  petWindow?.sendToRenderer('pet:status', { text: '正在重载配置…' });
  socket?.close();
  socket = null;
  await sidecar?.stop();
  sidecar = null;
  await startSidecarAndConnect();
}

// --------------------------------------------------------------------------- //
// 快捷键
// --------------------------------------------------------------------------- //

function registerShortcuts(): void {
  /**
   * 行为从 accelerator 映射过来。
   *
   * 名字**只从 SHORTCUTS 读**（见 shortcuts.ts），不在这里另写一份字符串 ——
   * 那样就会有两个真相源，而它们已经漂移过一次（界面写着 Ctrl+Alt+M，
   * 但它从未注册）。
   *
   * 用 switch 而不是把处理函数放进 SHORTCUTS：shortcuts.ts 要能被检查工具
   * 在纯 Node 里 import，塞进 Electron 相关的闭包就不行了。行为留在这里。
   */
  const handlers: Record<string, () => void> = {
    'CommandOrControl+Alt+P': () => petWindow?.toggleVisible(),
    'CommandOrControl+Alt+L': () => {
      petWindow?.show();
      petWindow?.sendToRenderer('pet:focus-input', {});
    },
    'CommandOrControl+Alt+Q': () => togglePause(),
    'CommandOrControl+Alt+S': () => void snapshotChat(),
    /*
     * **把焦点给宠物窗口** —— "按住说话"的前提（2026-10-10 加）。
     *
     * ## 为什么必须有
     *
     * 实测：宠物窗口**默认拿不到焦点**。它是鼠标穿透的
     * （`setIgnoreMouseEvents(true, {forward:true})`），点击会**穿到下面的窗口**——
     * 所以"点一下宠物"并不会让它聚焦（当时前台窗口是「桌面宠物 · 设置」）。
     *
     * 而 `before-input-event`（"按住说话"用来拿 keyup 的那个事件）
     * **只对拥有焦点的窗口生效**（实测失焦 0 个事件）。所以没有这个入口的话，
     * 用户按住右 Alt 永远不会触发，而日志里一切正常 —— 又是一个
     * "按了没反应却查不出原因"。
     *
     * ## 三步都要
     *
     *   `show()`          —— 窗口被隐藏时先显示出来（否则聚焦一个隐藏窗口没意义）
     *   `setAlwaysOnTop`  —— 确保它在最上层（本来就有，这里不重复设置）
     *   `focus()`         —— 真正把键盘焦点给它
     *
     * ⚠️ Windows 有"前台锁"：别的进程正在前台时 `SetForegroundWindow` 可能被拒。
     * Electron 的 `focus()` 内部已经处理了这个（会配合 `setAlwaysOnTop` 提升）。
     * 如果实测发现偶尔失败，再考虑 `moveTop()` + 二次 `focus()`。
     */
    'CommandOrControl+Alt+K': () => {
      petWindow?.show();
      const win = petWindow?.browserWindow ?? null;
      if (!win) {
        startupNote('[聚焦宠物] 拿不到宠物窗口');
        return;
      }
      try {
        win.focus();
        startupNote('[聚焦宠物] 已把焦点给宠物窗口 —— 现在可以按住说话键了');
      } catch (error) {
        startupNote(`[聚焦宠物] 失败：${(error as Error).message}`);
      }
    },
  };

  for (const spec of SHORTCUTS) {
    const handler = handlers[spec.accelerator];
    if (!handler) {
      // 登记了却没有对应行为 —— 那就是"说了不做"，宁可不注册也不要挂个空壳
      log(`快捷键 ${spec.accelerator} 在 SHORTCUTS 里登记了，但没有对应行为，已跳过`);
      continue;
    }
    const ok = globalShortcut.register(spec.accelerator, handler);
    log(`注册快捷键 ${spec.accelerator}（${spec.description}）：${ok ? '成功' : '失败（可能被占用）'}`);
  }
}

// --------------------------------------------------------------------------- //
// 退出
// --------------------------------------------------------------------------- //

async function quit(): Promise<void> {
  if (quitting) return;
  quitting = true;
  log('开始退出…');
  // 清掉自己的退出请求文件，免得留下陈旧请求让下次启动的实例自杀
  quitCoordinator.clear();
  quitCoordinator.stop();
  // 先停全屏轮询：它每次都要起一个 PowerShell 进程，
  // 退出流程里再冒出来一个会拖慢关闭、也可能变成孤儿进程。
  fullscreenWatcher?.stop();
  fullscreenWatcher = null;
  globalShortcut.unregisterAll();
  socket?.close();
  try {
    await sidecar?.stop();
  } catch (error) {
    log(`停止 sidecar 出错：${(error as Error).message}`);
  }
  tray?.destroy();
  /*
   * ⚠️ **显式销毁所有辅助窗口**（用户报：「关闭应用的时候对话框不关闭」）。
   *
   * 原来这里直接 `app.quit()`，指望它收掉所有窗口。但那**不保证**：
   *   · 对话条 / 气泡 / 设置都是**独立窗口**（各自的 BrowserWindow）；
   *   · `app.quit()` 会先发 `before-quit` / `close` 事件，任何一处 `preventDefault`
   *     或异步等待都可能让它退化成"主进程没了、窗口还在"；
   *   · 而且「退出全部实例」是**多实例同时退**，各自留一个对话条就会看到
   *     "宠物没了但输入框还浮在屏幕上"。
   *
   * ## ⚠️ **必须连宠物窗口一起销毁**（2026-10-04 探针实测出来的）
   *
   * 第一版只销毁了对话条/气泡/设置，把宠物窗口留给 `app.quit()` —— **结果退出卡住**：
   *
   *     app.quit() 被调用时，还剩 1 个窗口：index.html     ← 宠物窗口没被收
   *     ❌ 还有窗口没关
   *
   * 原因：`app.quit()` 关宠物窗口时会触发 `close` → 转成 `before-quit` →
   * 那里有 `event.preventDefault()`（见文件末尾的 `before-quit` 处理器，
   * 它把"用户直接关窗口"重定向到 `quit()`）。于是**退出被自己取消**，
   * 表现就是"点了退出，部分窗口还留着"。
   *
   * `destroy()` 不走 `close` 事件，所以能绕开这个环。
   * 顺序放在 `app.quit()` 之前 —— 那时主进程还活着，调用一定生效。
   *
   * ⚠️ 三个辅助类的方法名**不统一**：`ChatWindow` / `BubbleWindow` 叫 `destroy()`，
   * 而 `SettingsWindow` 叫 `close()`（内部也是 `window?.destroy()`）。
   * 别照着一个名字套三个 —— TS 会直接报 `Property 'destroy' does not exist`
   * （实测踩过）。
   */
  for (const [name, win] of [
    ['宠物', petWindow?.browserWindow ?? null],
    ['对话条', chatWindow?.browserWindow ?? null],
    ['气泡', bubbleWindow?.browserWindow ?? null],
    // ⚠️ 状态条也必须显式销毁 —— 它 `skipTaskbar: true` 且贴屏幕边缘，
    // 漏掉的话会出现"宠物没了但屏幕边还挂着一条"（与对话条那条故障同型）
    ['状态条', statusWindow?.browserWindow ?? null],
    // ⚠️ 右键菜单窗口也要销毁：它 `skipTaskbar: true` 且**常驻复用**，
    // 漏掉的话会留下一个看不见的透明窗（下一次右键用的是新实例，
    // 而旧窗还在那儿接着鼠标事件）
    ['右键菜单', petMenuWindow?.browserWindow ?? null],
  ] as const) {
    try {
      if (win && !win.isDestroyed()) win.destroy();
    } catch (error) {
      log(`销毁${name}窗口出错：${(error as Error).message}`);
    }
  }
  /* 类里还要把它自己的状态清干净（`loaded` / `pending` / `destroyed`） */
  try {
    petMenuWindow?.destroy();
  } catch (error) {
    log(`销毁右键菜单窗口出错：${(error as Error).message}`);
  }
  try {
    settingsWindow?.close();
  } catch (error) {
    log(`关闭设置窗口出错：${(error as Error).message}`);
  }
  try {
    memoryWindow?.close();
  } catch (error) {
    log(`关闭记忆管理窗口出错：${(error as Error).message}`);
  }
  petWindow = null;
  chatWindow = null;
  bubbleWindow = null;
  statusWindow = null;
  settingsWindow = null;
  memoryWindow = null;
  petMenuWindow = null;
  petMenuHandlers = null;
  app.quit();
}

/**
 * 退出**所有**实例（不只是自己）。
 *
 * 为什么需要：宠物刻意不做单实例锁（做过的那个，Electron 的 userData
 * 单实例锁一旦陈旧会让程序静默退出 0）。代价是用户反复双击会堆出很多实例
 * —— 实测同时跑 11 个，屏幕上叠在一起，看起来像各种诡异 bug。
 *
 * 而"一次只关一个"对用户毫无意义：他看得见的是十几个窗口，得关十几次。
 */
async function quitAll(): Promise<void> {
  log('请求退出全部实例…');
  quitCoordinator.broadcast();
  // 给自己一点时间让其他实例看到请求，然后自己也退出。
  // 广播是"尽力而为"：即使某个实例没收到，用户再点一次即可。
  await new Promise((resolve) => setTimeout(resolve, 400));
  await quit();
}

/**
 * 收到**其他实例**的"全关"请求时，自己退出。
 *
 * 这个回调不能是 `quit()` 本身：`quit()` 会先清请求文件，而其他实例可能
 * 还没来得及读到它。所以走内部函数，由 quit 内部去清。
 */
function onQuitRequested(): void {
  void quit();
}

app.on('window-all-closed', () => {
  // 托盘程序不因窗口关闭而退出
  if (process.platform !== 'darwin' && quitting) app.quit();
});

app.on('before-quit', (event) => {
  if (quitting) return;
  event.preventDefault();
  void quit();
});

app.on('will-quit', () => {
  globalShortcut.unregisterAll();
  // 正常退出时把失败标记清掉（如果窗口显示过，它本来就已被清掉；这里是兜底：
  // 窗口还没显示就退出的极端情况，不该让下次启动误判）。
  try {
    fs.rmSync(LAUNCH_MARKER, { force: true });
  } catch {
    /* 忽略 */
  }
});

/**
 * 每个网页一就绪就把当前主题注入进去（2026-10-14 加）。
 *
 * ## 为什么挂在这一个事件上，而不是在 7 个窗口里各写一遍
 *
 * `web-contents-created` 是**所有** webContents 的统一入口（含之后才创建的
 * 窗口，比如设置窗口、右键菜单）。挂一次就覆盖全部，而且以后新增窗口
 * 也自动带上主题 —— 不用记得去改新窗口的创建代码。
 *
 * `dom-ready` 而不是 `did-finish-load`：前者在 DOMContentLoaded 时就到，
 * 早于图片/视频这些慢资源。主题只是给 `<html>` 挂个属性，
 * 越早挂上，越不容易看到"先浅色再跳成深色"的闪一下。
 *
 * ⚠️ 仍然**可能**有极短的一帧浅色：CSS 变量在样式表解析时就生效了，
 * 而属性注入在 DOM 构建完之后。要彻底消掉得用 `preload` 同步设置
 * （那就回到"7 份 preload 各写一遍"的路子上）。实测这一帧看不见，
 * 所以先按这套来。
 */
app.on('web-contents-created', (_event, contents) => {
  contents.on('dom-ready', () => {
    injectTheme(contents);
    injectModules(contents);
  });
});

/**
 * 状态条/托盘用的状态推导（2026-10-12，按设计稿重做）。
 *
 * ## 为什么要「收敛成 2 个字」
 *
 * 设计稿的胶囊很小（实测渲染 40x76），竖排放 2 个字。
 * 🔴 2026-10-16 起胶囊改为**随文字长度伸缩**（见 `heightForLabel`），
 * 但长句（在线 · DeepSeek 之类）仍走 `statusText` 给托盘/对话条，
 * 状态条只拿状态词 —— 这里保留原文（约束已被 heightForLabel 取代）。
 *
 * 而原来那些状态文字是给人看的长句：
 *
 *     '在线 · DeepSeek'      10 个字符
 *     '重连中…'              4 个
 *     '大脑反复启动失败'      8 个
 *
 * 直接塞进胶囊会被**裁掉**（`overflow: hidden`），用户看到"文字缺了一半"——
 * 比什么都不显示更迷惑。
 *
 * ## 所以分成两个东西
 *
 *   · **状态条**：`kind` + **2 字词**（胶囊里只有这两样）
 *   · **托盘 / 对话条**：长文字照旧（它们空间够，而且需要细节）
 *
 * 长文字仍然走 `statusText`（托盘和对话条用），状态条只拿短词。
 *
 * ## ⚠️ 为什么用 `includes` 而不是相等
 *
 * 调用点传进来的是一句人话（`在线 · DeepSeek`），不是枚举。
 * 硬要求它们传枚举会让每个调用点都得改，而且加一种服务商就要动一次。
 * 用关键词匹配，**匹配不上就退回「在线」**（保守但不会显示错的灯色）。
 *
 * ## ⚠️ 这里**故意没有**「聆听」那一行（2026-10-16 删）
 *
 * 原来有一条 `/聆听|在听|录音|识别/ -> 'listening'`。用户要求彻底删除
 * （「聆听就不用黄色了，宠物自己会显示」），所以连关键词一起删了：
 *
 *   · 留着它 = 留着一个**查不到的状态**。将来有人搜索"聆听"会搜到关键词
 *     却找不到对应的 `StatusKind`，白费时间
 *   · 而且它是**永远匹配不上**的死逻辑（没有调用点会产出含这些词的长文字）
 *
 * ⚠️ 删掉后，含「识别 / 录音」这类词的长文字会**落到最后那条兜底**，
 * 显示「空闲」而不是「聆听」—— 这是想要的（聆听不做了）。
 */
function statusKindFor(text: string): { kind: StatusKind; label: string } {
  if (/失败|错误|异常|不匹配/.test(text)) return { kind: 'error', label: '异常' };
  // 🔴 2026-10-16 用户指定：状态条文字用全称（原来是 2 字词「搜索/准备」）
  if (/搜索|读网页/.test(text)) return { kind: 'searching', label: '联网搜索中' };
  // 🔴 2026-10-16：「思考」原来被归进 speaking —— 生成回复时状态条显示
  // "说话"（还没真的说话），正是用户报的"变来变去"的一部分。
  if (/思考/.test(text)) return { kind: 'thinking', label: '思考' };
  // 「说话中」= **宠物**在说话（2026-10-17 用户强调：状态条全是宠物的状态，
  // 不是用户的）。用户侧的收音/识别有专门的（已删的）聆听档，与此无关。
  // ⚠️ 「语音准备中」一档 2026-10-17 已删（和思考中重复）—— 含"准备"的
  // 文字会落到这里后面的兜底，不会再到这个分支。
  if (/说话|生成/.test(text)) return { kind: 'speaking', label: '说话中' };
  if (/重连|连接|启动|离线/.test(text)) return { kind: 'idle', label: '重连' };
  return { kind: 'idle', label: '空闲' };
}

// 开发期禁用默认菜单，避免误触
Menu.setApplicationMenu(null);

/**
 * 设置全局状态（主进程是**唯一真相源**）。
 *
 * ## 🔴 2026-10-12 修：原来它**从不更新状态条**
 *
 * 用户报「宠物联网搜索的时候状态栏不会变，没有显示联网搜索中」。
 * 根因不是"没做搜索状态"，而是**状态条根本没有被设过状态**：
 *
 *   · `StatusWindow` 里 `setStatus(kind, text)` / `pushState()` 都写好了
 *   · `StatusKind` 里 `'searching'` 这一档也早就定义了
 *   · 但主进程这个共用的 `setStatus()` **只更新了托盘**（`tray?.setStatus`）
 *     —— `statusWindow?.setStatus()` **一次都没被调用过**
 *
 * 所以状态条的灯和文字**永远停在窗口创建时的初始值**。
 *
 * ⚠️ 而 `status:hello` 那条注释里已经写着「真实状态可能是『联网搜索中...』」——
 * 说明当时**以为**接通了。这正是本项目反复出现的那一族：
 * **两边各自都写好了，中间那根线没接**。
 *
 * ## 两个接收方拿到的东西**不一样**
 *
 *   · **托盘 / 对话条**：完整的长文字（它们空间够，而且需要细节，
 *     比如「在线 · DeepSeek」能看出用的是哪家服务商）
 *   · **状态条**：设计稿的**2 字词 + 灯色**（胶囊只有 40x76）
 *
 * 所以这里把长文字原样给前两者，另外推导一份短的给状态条。
 */
function setStatus(text: string): void {
  statusText = text;
  tray?.setStatus(text);
  const { kind, label } = statusKindFor(text);
  statusWindow?.setStatus(kind, label);
}

/* --------------------------------------------------------------------------- *
 * 状态条的活动状态机（2026-10-16）
 *
 * ## 用户报的现象
 *
 * 「联网搜索时状态栏变来变去，很怪」—— 一个回合内：说话（错，其实还在生成）
 * -> 搜索 -> 在线 -> 说话 …… 每个事件源各写各的，互相踩。
 *
 * ## 修法：**回合相位**由事件驱动、集中刷新
 *
 *     pet.thinking / pet.say.start -> 思考中（紫）  回合开始，模型在生成
 *     pet.tool start               -> 联网搜索中（蓝）工具执行期间盖过思考
 *     pet.tool end                 -> 回到思考中（回合还没结束，不跳"在线"）
 *     pet.say.end（done 且语音开着）-> 保持思考中（TTS 合成期间，2026-10-17 删"语音准备中"）
 *     音频二进制帧转发（binary drain）-> 说话中（青）语音真的开播了
 *     pet:voice-played（渲染层 onended）-> 空闲（绿）播完/被打断
 *     voice.tts_failed             -> 异常（红）
 *     voice.state idle             -> 空闲（绿）
 *
 * ## 「语音准备中」为什么删了（2026-10-17 用户拍板）
 *
 * 「和思考中重复了」—— 合成期间显示思考中就够了。原来那一档还带来
 * 一堆信号链问题（voice.state speaking 是文字阶段信号、播完没事件）。
 * 现在合成期并入思考中，播放/播完各有真事件。
 *
 * 刷新只有一个入口 `refreshActivityStatus()`，按优先级
 * 搜索 > 思考 计算当前档位 —— 事件只改状态变量，
 * 谁也不会把谁踩掉。每次档位变化都打一行日志（`[状态条] a -> b`），
 * 用户报"状态不对"时对着日志就能看出是哪一跳错了。
 *
 * ⚠️ 看门狗（90 秒无事件回空闲）**不是语义的一部分**，只是 end 事件
 * 丢失（sidecar 崩溃）时的兜底 —— 正常流程永远走不到它。
 * --------------------------------------------------------------------------- */

let turnThinking = false;
/** 并发工具计数（pet.tool start/end 配对） */
let searchDepth = 0;
let lastActivityAt = 0;
let activityWatchdog: NodeJS.Timeout | null = null;
/** 上一次刷新算出的档位 —— 只用来打迁移日志，不参与判断 */
let lastActivityKind: 'thinking' | 'searching' | 'idle' = 'idle';
/**
 * 语音**真的在播**（2026-10-16 加）。
 *
 * ⚠️ 不并进 `lastActivityKind`：那个只描述状态机算出的三档
 * （thinking/searching/idle），而"说话中"是音频转发时**直设**的
 * （不走 refreshActivityStatus）。`pet:voice-played`（渲染层播完回调）
 * 靠它判断"现在清状态条对不对"。
 */
let voiceSpeaking = false;

/** 回到空闲态：恢复"在线 · 服务商"（刻意重算，理由见 voice.state 分支的注释）。 */
function clearActivityStatus(): void {
  turnThinking = false;
  searchDepth = 0;
  voiceSpeaking = false;
  if (activityWatchdog) {
    clearInterval(activityWatchdog);
    activityWatchdog = null;
  }
  const label = lastReady?.brain ? brainStatusLabelNow(String(lastReady.brain)) : '';
  setStatus(label ? `在线 · ${label}` : '在线');
}

function refreshActivityStatus(): void {
  lastActivityAt = Date.now();
  const next: 'thinking' | 'searching' | 'idle' =
    searchDepth > 0 ? 'searching' : turnThinking ? 'thinking' : 'idle';

  if (next !== lastActivityKind) {
    log(`[状态条] ${lastActivityKind} -> ${next}`);
    lastActivityKind = next;
  }

  if (next === 'searching') {
    tray?.setStatus('联网搜索中');
    // 🔴 2026-10-16 用户指定：状态条文字用全称「联网搜索中」（原来是 2 字「搜索」）
    statusWindow?.setStatus('searching', '联网搜索中');
  } else if (next === 'thinking') {
    tray?.setStatus('思考中');
    statusWindow?.setStatus('thinking', '思考');
  } else {
    clearActivityStatus();
    return;
  }
  if (!activityWatchdog) {
    activityWatchdog = setInterval(() => {
      if (
        // voiceSpeaking 也在兜底范围：终点信号（onended/时长定时器）全丢时，
        // 最多 90 秒强制回空闲 —— 「说话中」不能永久卡死（2026-10-17）。
        (turnThinking || searchDepth > 0 || voiceSpeaking) &&
        Date.now() - lastActivityAt > 90_000
      ) {
        log('[状态条] 90 秒没有新事件 —— 判定回合已结束（end 事件丢失），回空闲');
        clearActivityStatus();
      }
    }, 15_000);
  }
}
