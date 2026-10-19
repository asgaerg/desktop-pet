/**
 * 桌宠自身的偏好设置（不是密钥 —— 密钥在 credentials.ts）。
 *
 * 目前四项：**边缘探头时伸出去多少像素**、**靠边多少像素触发侧身**、
 * 边缘探头开关、宠物大小。
 *
 * ---- 为什么单独一个文件 ----
 *
 * 密钥走 `credentials.json`，普通偏好走 `settings.json`：
 *   · 密钥可能需要单独备份/清除，混在一起会很别扭；
 *   · 密钥文件权限要求更高，偏好不需要；
 *   · 用户问"我设置存哪了"时，这个文件可以直接打开看。
 *
 * ---- ⚠️ 落点为什么有一串候选（这是踩出来的）----
 *
 * 原来只用 `app.getPath('userData')`，而 `writeFileSync` 那里我写了
 * `catch { /* 忽略 *\/ }` —— 结果**写失败被静默吞掉**：设置窗口里滑杆动了、
 * 界面也回显了新值、函数还返回了"成功"，但磁盘上一个字节都没写。
 * 用户重启后发现全部回到默认（实测反馈："倾斜的参数和边缘判定的参数
 * 关闭应用后重新打开又变回默认"）。
 *
 * 所以现在：**候选路径逐个试，并如实返回到底写成功了没有、写在哪**。
 * 调用方（IPC）会把失败原因带回界面 —— 让人看见，而不是假装成功。
 */

import { existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { userDataDir } from './credentials.js';
import {
  clampPeekOffset,
  clampEdgeSnap,
  PEEK_OFFSET_DEFAULT_PX,
  EDGE_SNAP_DEFAULT_PX,
  BUBBLE_LINGER_DEFAULT_MS,
} from './petLayout.js';

export interface PetSettings {
  /** 边缘探头时窗口伸出屏幕的像素数 */
  peekOffsetPx: number;
  /**
   * 拖拽结束时，离屏幕边缘多少像素算"贴边"（触发侧身探头）。
   *
   * 用户要求："我好自己设置靠近屏幕边缘多少像素自动侧身"。
   * 调大 = 更容易触发（离得还远就侧身）；调小 = 要拖到很近才触发。
   */
  edgeSnapPx: number;
  /** 边缘探头开关 */
  edgePeekEnabled: boolean;
  /**
   * 状态条贴哪条边（2026-10-11 用户要求）。
   *
   * ⚠️ 只存 `edge` + `y`，**不存 x** —— x 永远由"贴哪条边 + 工作区宽度"
   * 算出来。存 x 的话，换分辨率或换显示器后它会飘到屏幕外，
   * 而且**不会有任何报错**（窗口照常存在，只是看不见）。
   */
  statusEdge?: 'left' | 'right';
  /**
   * 状态条顶部相对**工作区**顶部的 y（不是屏幕绝对坐标）。
   *
   * 存相对值的理由同 `statusEdge`。
   */
  statusY?: number;
  /** 状态条显不显示（默认显示） */
  statusVisible?: boolean;
  /** 宠物缩放（1 = 基准尺寸） */
  petScale: number;
  /**
   * 对话条被拖到哪了（2026-10-12 用户要求「输入文字的对话框可拖拽」）。
   *
   * `undefined` = 没拖过，主进程按默认规则钉在右下角。
   * **`null` = 显式清空**（复位到右下角时用，见 `writeSettings` 的说明）。
   *
   * ⚠️ 存**绝对坐标**而不是"相对某个角"：对话条不在窗口边缘，
   * 用户拖到哪就是哪，没有"吸附哪条边"可存。
   * （与状态条不同 —— 那个贴屏幕边缘，所以只存 `edge` + `y`。）
   */
  chatX?: number | null;
  chatY?: number | null;
  /**
   * 用户是否**亲手调过**"探头时的平移距离"。
   *
   * ## 为什么需要这个标记（2026-10-04）
   *
   * 用户要求把默认值改成 **68px**（「默认 68px，重启有效，但依然可以自行调节」）。
   * 但老配置里存的是旧默认 **0**，而读取逻辑是 `raw.peekOffsetPx ?? 默认` ——
   * 存过的 0 会**盖掉**新默认，于是"改了默认值却对老用户无效"。
   *
   * 而旧语义下 0 **不代表用户的选择**（那时 0 = "不用额外平移"，是内建行为），
   * 所以不能靠"值是不是 0"来判断用户意图。
   *
   * 于是加这个标记：**只有在设置里真拖过那个滑杆**才置 true。
   * 读取时若没有这个标记，就用新默认值覆盖旧的 0。
   */
  peekOffsetTuned?: boolean;
  /**
   * "允许看屏幕"的用户意愿值。
   *
   * 用户要求：菜单/设置里那个开关要「保存在本地，每次重新打开应用获取上次的状态」。
   *
   * ⚠️ 为什么必须**自己落盘**、不能只靠 `config/persona.yaml`：
   * sidecar 的 `config.update` 走的是 `Config.apply_patch()`，它**只改内存、
   * 不写文件**（见 `apps/brain/pet/config.py`）。而重启时 sidecar 会
   * `reload_from_disk()` 从 yaml 重载 —— 于是任何通过界面改的开关
   * 重启后都会回到 yaml 里的值。想让"上次改的"活过重启，就得在主进程侧存一份。
   *
   * 与 `activity` 的区别（那个**刻意不存**，见下面的注释）：activity 存了也会被
   * config 快照盖掉，所以是"假保存"；而这一项我们在 sidecar 就绪后**主动下发**，
   * 配置快照不会覆盖它，所以是真保存。
   */
  visionEnabled: boolean;
  /**
   * 对话框（对话条）是否显示。
   *
   * 用户要求：「点击隐藏对话框，对话框就隐藏……之后再点击可以显示对话框」，
   * 之后又报「关闭应用重新打开又出来了」—— 说明这份**用户意愿必须落盘**。
   *
   * 与 `visionEnabled` 同理：这类"界面上的开关"如果只存在内存里，
   * 重启就回默认，用户会以为保存失败（这个项目已经栽过好几次）。
   */
  chatVisible: boolean;
  /**
   * 文字弹窗（宠物上方的回应气泡）的存在时间，毫秒。默认 10 秒。
   *
   * 用户要求：「设置中可以设置文字弹窗的存在时间，默认 10 秒后消失」。
   * 与其它界面开关同理，**必须落盘**，否则重启回默认。
   */
  bubbleLingerMs: number;

  /**
   * 气泡段间距（px，0~24，默认 4）。用户要求「将气泡的间距设置放在设置里
   * 可以调节」（2026-10-17）。渲染层通过 `bubble:message` 的 `paraGap`
   * 字段拿到（与 lingerMs 同一条通道），改完立刻生效。
   */
  bubbleParaGap: number;

  /** 气泡与头顶间距（px，默认 6） */
  bubbleGapAbovePx: number;
  /** 气泡与左右立绘的间距（px，默认 10） */
  bubbleGapSidePx: number;
  /** 立绘核心框收缩比例（%，默认 25） */
  bubbleCoreShrinkPct: number;

  /**
   * 是否显示气泡（宠物上方的文字弹窗）。默认 **true**。
   *
   * ## 用户要求（2026-10-07）
   *
   * 「气泡改成默认不自动关闭，现在点击气泡或者宠物气泡就消失了，
   * 改成仅可通过气泡持续时间并且**可在宠物菜单设置隐藏和显示气泡**」
   *
   * 所以：
   *   · 隐藏/显示由**菜单**控制（右键或托盘），不走设置窗口；
   *   · 这是个**开关**（关掉之后不再自动弹），不是"当前可见性"——
   *     所以下面 `showBubble()` 要按它拦截；
   *   · **必须落盘**，否则重启后又冒出来（与对话框显隐同理）。
   */
  bubbleVisible: boolean;

  /**
   * 沉浸模式（用户 2026-10-09 要求）。
   *
   * 打开时**自动隐藏对话框和气泡**，只留语音 —— 适合打游戏/看片时不想被文字挡。
   *
   * ⚠️ **例外**：TTS 关了或合成失败时**仍然显示文字**。
   * 否则用户"既听不到、也没得看"，消息等于丢了（用户明确要求"显示错误文本"）。
   *
   * ⚠️ 和 `bubbleVisible` 的分工：那个是"气泡要不要弹"的**手动偏好**，
   * 这个是"临时进一个状态"。**优先级：沉浸模式 > bubbleVisible**。
   */
  immersiveMode: boolean;
  /**
   * 选定的麦克风 `deviceId`（空 = 用系统默认）。
   *
   * ## ⚠️ 为什么必须让用户能选（2026-10-08 实测）
   *
   * 用户的 USB 麦克风「说话没用」。实测（同一支 USB 麦，同样对人说话 3 秒）：
   *
   *     不给 deviceId（走 Chromium 的 default 别名）  peak 0.0205  判为语音  3/83  ❌
   *     指定真实 deviceId                            peak 0.1413  判为语音 19/86  ✅
   *
   * **差 7 倍。** 而 Chromium 的 `default` 别名在枚举里显示的名字和真实设备
   * **一模一样**（都是"Default - 麦克风 (USB PnP Sound Device)"）——
   * 用户从界面上完全看不出自己用的不是那一支。
   *
   * 所以：让用户从**真实设备列表**里挑，并把实际打开的设备名显示出来。
   */
  micDeviceId: string;
  /**
   * 界面主题（2026-10-14 用户要求「现在是浅色系的，加个深色系的」）。
   *
   * ⚠️ 只有两档，**没有"跟随系统"**：
   * 这个应用是贴在桌面上的透明浮层（宠物、气泡、对话条），
   * "跟随系统"会在系统主题变化时把浮层也一起翻掉 ——
   * 而用户看不到系统设置面板，只会觉得"它自己变了"。
   * 真要跟随系统是另一个需求（要监听 `nativeTheme.updated`），先不做。
   */
  theme: 'light' | 'dark';
  // ⚠️ **刻意不存 `activity`（活泼度）**。
  //
  // 它的真源是 `config/persona.yaml` 的 `activity.overall`，启动后会被
  // sidecar 的 config 快照覆盖。我一度把它也存进这里，结果是**存了但没用**：
  // 界面上勾了、文件里也写了，重启后仍然被 config 盖掉 ——
  // 这种"看起来保存了其实没有"最误导人。
  // 所以活泼度统一以 config 为准，设置窗口里改它只影响本次运行。
}

/**
 * 把"文字弹窗存在时间"夹进合理范围。
 *
 * 下限 1 秒（再短用户来不及读，看起来像闪一下）；上限 120 秒
 * （再长就等于常驻，那不是"弹窗"的意思了）。
 */
export const BUBBLE_LINGER_MIN_MS = 1000;
export const BUBBLE_LINGER_MAX_MS = 120_000;
/*
 * 默认值定义在 `petLayout`（`petWindow` 也要用，避免循环依赖）—— 这里转出去，
 * 保持"设置相关常量都从 settingsStore 拿"的既有习惯。
 */
export { BUBBLE_LINGER_DEFAULT_MS };

/**
 * 气泡段间距（px）：段落之间的空隙，「一点点」的那个（2026-10-17）。
 * 0 = 完全贴合，24 = 明显分段。默认 4。
 */
export const BUBBLE_PARA_GAP_MIN_PX = 0;
export const BUBBLE_PARA_GAP_MAX_PX = 24;
export const BUBBLE_PARA_GAP_DEFAULT_PX = 4;

/**
 * 气泡摆放的三个可调参数（2026-10-17/18，设置 → 桌宠 → 气泡位置）：
 *   · bubbleGapAbovePx —— 气泡与头顶的间距（默认 6）
 *   · bubbleGapSidePx  —— 气泡与左右立绘的间距（默认 10）
 *   · bubbleCoreShrinkPct —— 立绘核心框收缩比例（默认 25%，补偿动画帧间余量）
 * 持久化要求来自用户：「参数改变后应用重启，参数仍然是改变后的参数」。
 */
export const BUBBLE_GAP_ABOVE_DEFAULT_PX = 6;
export const BUBBLE_GAP_SIDE_DEFAULT_PX = 10;
export const BUBBLE_CORE_SHRINK_DEFAULT_PCT = 25;

export function clampBubbleGapAbove(value: unknown): number {
  const n = Number(value);
  if (!Number.isFinite(n)) return BUBBLE_GAP_ABOVE_DEFAULT_PX;
  return Math.round(Math.max(0, Math.min(120, n)));
}

export function clampBubbleGapSide(value: unknown): number {
  const n = Number(value);
  if (!Number.isFinite(n)) return BUBBLE_GAP_SIDE_DEFAULT_PX;
  return Math.round(Math.max(0, Math.min(120, n)));
}

export function clampBubbleCoreShrink(value: unknown): number {
  const n = Number(value);
  if (!Number.isFinite(n)) return BUBBLE_CORE_SHRINK_DEFAULT_PCT;
  return Math.round(Math.max(0, Math.min(40, n)));
}

export function clampBubbleParaGap(value: unknown): number {
  const n = Number(value);
  if (!Number.isFinite(n)) return BUBBLE_PARA_GAP_DEFAULT_PX;
  return Math.round(Math.max(BUBBLE_PARA_GAP_MIN_PX, Math.min(BUBBLE_PARA_GAP_MAX_PX, n)));
}

export function clampBubbleLinger(value: unknown): number {
  const n = Number(value);
  if (!Number.isFinite(n)) return BUBBLE_LINGER_DEFAULT_MS;
  return Math.round(Math.max(BUBBLE_LINGER_MIN_MS, Math.min(BUBBLE_LINGER_MAX_MS, n)));
}

const DEFAULTS: PetSettings = {
  peekOffsetPx: PEEK_OFFSET_DEFAULT_PX,
  edgeSnapPx: EDGE_SNAP_DEFAULT_PX,
  edgePeekEnabled: true,
  // 状态条默认贴**左**边、靠上一点（不与宠物默认位置打架）
  statusEdge: 'left',
  statusY: 120,
  statusVisible: true,
  petScale: 1,
  // 与 `config/persona.yaml` 的 `providers.vision.enabled: false` 保持一致 ——
  // 用户要求「改成默认关闭」。
  visionEnabled: false,
  // 默认显示（与 ChatWindow.create 的 ready-to-show 行为一致）
  chatVisible: true,
  // 默认 60 秒后消失（用户要求：「气泡默认时长改成 60 秒」）
  bubbleLingerMs: BUBBLE_LINGER_DEFAULT_MS,
  bubbleParaGap: BUBBLE_PARA_GAP_DEFAULT_PX,
  bubbleGapAbovePx: BUBBLE_GAP_ABOVE_DEFAULT_PX,
  bubbleGapSidePx: BUBBLE_GAP_SIDE_DEFAULT_PX,
  bubbleCoreShrinkPct: BUBBLE_CORE_SHRINK_DEFAULT_PCT,
  /**
   * 气泡默认**显示**（用户要求「可在宠物菜单设置隐藏和显示气泡」——
   * 默认状态是显示，由用户在菜单里关）。
   */
  bubbleVisible: true,
  immersiveMode: false,
  /**
   * 麦克风默认**空**（= 交给系统默认）。
   *
   * ⚠️ 但实测"系统默认"可能就是那个听不清的设备（见字段注释里的 7 倍差距）。
   * 所以设置页会**主动列出真实设备让用户挑**，而不是让这个空值一直用下去。
   */
  micDeviceId: '',
  // 默认**浅色**（用户 2026-10-14 时的现状就是浅色系，深色是新增选项）
  theme: 'light',
};

/** 写盘结果（如实告知成功与否，供界面显示） */
export interface SettingsWriteResult {
  settings: PetSettings;
  ok: boolean;
  /** 实际写入的路径（失败时是尝试过的第一个候选） */
  path: string;
  /** 失败原因（ok=false 时） */
  error?: string;
}

let dirOverride: string | undefined;
/** 保底落点（主进程传入，必须是**绝对路径**） */
let fallbackDir: string | undefined;

export function setSettingsDir(dir: string): void {
  dirOverride = dir;
}

/**
 * 设置保底落点（通常是入口文件所在目录，绝对路径）。
 *
 * 为什么需要：实测受限环境里 `userData` / 用户目录 / `%TEMP%` 会**全部**不可写，
 * 此时设置根本存不下来、重启回默认 —— 而用户看到的现象只是"参数自己变回去了"。
 */
export function setSettingsFallbackDir(dir: string): void {
  fallbackDir = dir;
}

/**
 * 落点**运行时实测**的结果缓存。
 *
 * ⚠️ 为什么不是"按固定顺序试写"：实测发现**能不能写取决于进程的启动环境**，
 * 而不是路径本身 ——
 *   · 从 DSH 的沙箱里启动时，`AppData\Roaming` 被拒绝（EPERM）；
 *   · 同一个路径从普通 shell 里写却完全正常。
 * 所以不能硬编码"userData 一定行"，只能**在目标目录真写一个探针文件**看结果。
 * （探针文件写完就删，不留垃圾。）
 */
let resolvedPath: string | null = null;

/** 用"真写一个探针文件"来判断目录可不可用（而不是猜） */
function canWrite(dir: string): boolean {
  const probe = join(dir, '.pet-write-probe');
  try {
    mkdirSync(dir, { recursive: true });
    writeFileSync(probe, 'ok', 'utf8');
    rmSync(probe, { force: true });
    return true;
  } catch {
    return false;
  }
}

/** 候选落点，按优先级排序 */
function candidateDirs(): string[] {
  const dirs: string[] = [];
  // ① 显式指定的（主进程传 app.getPath('userData')）
  if (dirOverride) dirs.push(dirOverride);
  // ② 用户目录 —— 与密钥、记忆同处，且**不受 Electron 路径配置影响**
  dirs.push(userDataDir());
  // ③ 系统临时目录
  const tmp = process.env.TEMP ?? process.env.TMP;
  if (tmp) dirs.push(join(tmp, 'desktop-pet'));
  // ④ **保底**：调用方指定的目录（入口文件旁边，绝对路径）。
  //
  // 为什么需要这一层：实测在受限环境里 ①②③ 会**全部**被拒绝
  // （AppData 与 %TEMP% 都不可写），于是设置根本存不下来、重启回默认 ——
  // 而用户看到的现象只是"参数自己变回去了"，完全看不出原因。
  // 这个目录和 startup.log 同处，一直是可靠的落点。
  if (fallbackDir) dirs.push(fallbackDir);
  return [...new Set(dirs)];
}

/** 当前实际生效的设置文件路径（首次调用时实测决定，之后缓存） */
export function settingsPath(): string {
  if (resolvedPath) {
    // 已解析过的路径仍要能读到"别的候选里的旧文件"（换了环境之后）
    return resolvedPath;
  }
  for (const dir of candidateDirs()) {
    const path = join(dir, 'settings.json');
    // 已有文件 -> 直接用它（保持前后一致，别把设置写到另一个地方去）
    if (existsSync(path)) {
      resolvedPath = path;
      return resolvedPath;
    }
  }
  // 都不存在 -> 用第一个**能写**的目录
  for (const dir of candidateDirs()) {
    if (canWrite(dir)) {
      resolvedPath = join(dir, 'settings.json');
      return resolvedPath;
    }
  }
  resolvedPath = join(candidateDirs()[0], 'settings.json');
  return resolvedPath;
}

/**
 * 读取偏好设置。
 *
 * 依次在候选路径里找**第一个存在的**；解析失败或没找到都回退默认值。
 * 配置文件坏了不该让程序起不来，也不该让宠物卡在一个奇怪的姿态上。
 */
export function readSettings(): PetSettings {
  // 依次在所有候选里找：先看已解析的路径，再看其它候选。
  // 这样"换了启动环境导致落点变化"时也能读回旧设置。
  const paths = [settingsPath(), ...candidateDirs().map((d) => join(d, 'settings.json'))];
  for (const path of [...new Set(paths)]) {
    if (!existsSync(path)) continue;
    try {
      const raw = JSON.parse(readFileSync(path, 'utf8')) as Partial<PetSettings>;
      return {
        /*
         * ⚠️ **只有用户亲手调过才用存下来的值**（见 `peekOffsetTuned` 的说明）。
         *
         * 否则老配置里的旧默认 `0` 会盖掉新默认 68 —— "改了默认值却对老用户无效"。
         */
        peekOffsetPx: clampPeekOffset(
          raw?.peekOffsetTuned === true ? (raw?.peekOffsetPx ?? DEFAULTS.peekOffsetPx) : DEFAULTS.peekOffsetPx,
        ),
        peekOffsetTuned: raw?.peekOffsetTuned === true,
        edgeSnapPx: clampEdgeSnap(raw?.edgeSnapPx ?? DEFAULTS.edgeSnapPx),
        edgePeekEnabled:
          typeof raw?.edgePeekEnabled === 'boolean' ? raw.edgePeekEnabled : DEFAULTS.edgePeekEnabled,
        // 状态条落位：只认合法值，其它一律退回默认（坏值不该让它跑到屏幕外）
        statusEdge: raw?.statusEdge === 'right' ? 'right' : 'left',
        statusVisible: typeof raw?.statusVisible === 'boolean' ? raw.statusVisible : true,
        statusY:
          Number.isFinite(Number(raw?.statusY)) && raw?.statusY !== undefined
            ? Math.max(0, Math.min(10000, Math.trunc(Number(raw.statusY))))
            : DEFAULTS.statusY,
        petScale:
          Number.isFinite(Number(raw?.petScale)) && raw?.petScale !== undefined
            ? Math.max(0.6, Math.min(2.4, Number(raw.petScale)))
            : DEFAULTS.petScale,
        /*
         * 对话条拖动后的位置。
         *
         * ⚠️ 这两个**没有默认值**（`undefined` = 没拖过），所以不能像别的项那样
         * `?? DEFAULTS.xxx` —— 那会给一个"默认坐标"，而默认行为是**钉右下角**，
         * 那个坐标要按屏幕工作区算，不是一个常量能表达的。
         */
        chatX:
          raw?.chatX === null
            ? null
            : Number.isFinite(Number(raw?.chatX))
              ? Math.trunc(Number(raw.chatX))
              : undefined,
        chatY:
          raw?.chatY === null
            ? null
            : Number.isFinite(Number(raw?.chatY))
              ? Math.trunc(Number(raw.chatY))
              : undefined,
        visionEnabled:
          typeof raw?.visionEnabled === 'boolean' ? raw.visionEnabled : DEFAULTS.visionEnabled,
        chatVisible:
          typeof raw?.chatVisible === 'boolean' ? raw.chatVisible : DEFAULTS.chatVisible,
        bubbleLingerMs: clampBubbleLinger(raw?.bubbleLingerMs ?? DEFAULTS.bubbleLingerMs),
        bubbleParaGap: clampBubbleParaGap(raw?.bubbleParaGap ?? DEFAULTS.bubbleParaGap),
        bubbleGapAbovePx: clampBubbleGapAbove(raw?.bubbleGapAbovePx ?? DEFAULTS.bubbleGapAbovePx),
        bubbleGapSidePx: clampBubbleGapSide(raw?.bubbleGapSidePx ?? DEFAULTS.bubbleGapSidePx),
        bubbleCoreShrinkPct: clampBubbleCoreShrink(raw?.bubbleCoreShrinkPct ?? DEFAULTS.bubbleCoreShrinkPct),
    bubbleVisible:
      typeof raw?.bubbleVisible === 'boolean' ? raw.bubbleVisible : DEFAULTS.bubbleVisible,

    immersiveMode:
      typeof raw?.immersiveMode === 'boolean' ? raw.immersiveMode : DEFAULTS.immersiveMode,
    micDeviceId: typeof raw?.micDeviceId === 'string' ? raw.micDeviceId : DEFAULTS.micDeviceId,
    // 只认 'dark'，其余一律浅色 —— 老配置文件里没有这个字段，走默认
    theme: raw?.theme === 'dark' ? 'dark' : DEFAULTS.theme,
      };
    } catch {
      // 这个候选坏了，试下一个
    }
  }
  return { ...DEFAULTS };
}

/**
 * 写入偏好设置。
 *
 * **逐个候选试写，并如实返回结果** —— 不再静默吞掉失败。
 * 写失败时 `ok=false` 且带 `error`，界面会把原因显示出来。
 */
export function writeSettings(patch: Partial<PetSettings>): SettingsWriteResult {
  const current = readSettings();
  const next: PetSettings = {
    peekOffsetPx:
      patch.peekOffsetPx !== undefined ? clampPeekOffset(patch.peekOffsetPx) : current.peekOffsetPx,
    /*
     * ⚠️ **用户一调这个值就把它标成"亲手调过"** —— 之后读取时就不再被新默认值覆盖。
     * 见 `peekOffsetTuned` 的说明。
     */
    peekOffsetTuned:
      patch.peekOffsetPx !== undefined ? true : (patch.peekOffsetTuned ?? current.peekOffsetTuned),
    edgeSnapPx: patch.edgeSnapPx !== undefined ? clampEdgeSnap(patch.edgeSnapPx) : current.edgeSnapPx,
    edgePeekEnabled:
      typeof patch.edgePeekEnabled === 'boolean' ? patch.edgePeekEnabled : current.edgePeekEnabled,
    statusVisible:
      typeof patch.statusVisible === 'boolean' ? patch.statusVisible : (current.statusVisible ?? true),
    statusEdge:
      patch.statusEdge === 'left' || patch.statusEdge === 'right'
        ? patch.statusEdge
        : (current.statusEdge ?? DEFAULTS.statusEdge),
    statusY:
      patch.statusY !== undefined && Number.isFinite(Number(patch.statusY))
        ? Math.max(0, Math.min(10000, Math.trunc(Number(patch.statusY))))
        : (current.statusY ?? DEFAULTS.statusY),
    petScale:
      patch.petScale !== undefined && Number.isFinite(Number(patch.petScale))
        ? Math.max(0.6, Math.min(2.4, Number(patch.petScale)))
        : current.petScale,
    /*
     * 对话条位置（没拖过就是 undefined -> 走默认钉角，见 chatX 的说明）。
     *
     * ⚠️ **`null` 表示"清空"**，要单独判 —— `writeSettings` 的合并规则是
     * `patch.x !== undefined ? patch.x : current.x`，所以传 `undefined` 等于
     * **"不修改"**，永远清不掉。这个坑一开始就踩了：复位功能写好之后
     * 落盘写的还是旧坐标，重启又跳回去。
     */
    chatX:
      patch.chatX === null
        ? null
        : patch.chatX !== undefined && Number.isFinite(Number(patch.chatX))
          ? Math.trunc(Number(patch.chatX))
          : current.chatX,
    chatY:
      patch.chatY === null
        ? null
        : patch.chatY !== undefined && Number.isFinite(Number(patch.chatY))
          ? Math.trunc(Number(patch.chatY))
          : current.chatY,
    visionEnabled:
      typeof patch.visionEnabled === 'boolean' ? patch.visionEnabled : current.visionEnabled,
    chatVisible:
      typeof patch.chatVisible === 'boolean' ? patch.chatVisible : current.chatVisible,
    bubbleLingerMs:
      patch.bubbleLingerMs !== undefined
        ? clampBubbleLinger(patch.bubbleLingerMs)
        : current.bubbleLingerMs,
    bubbleParaGap:
      patch.bubbleParaGap !== undefined
        ? clampBubbleParaGap(patch.bubbleParaGap)
        : current.bubbleParaGap,
    bubbleGapAbovePx:
      patch.bubbleGapAbovePx !== undefined
        ? clampBubbleGapAbove(patch.bubbleGapAbovePx)
        : current.bubbleGapAbovePx,
    bubbleGapSidePx:
      patch.bubbleGapSidePx !== undefined
        ? clampBubbleGapSide(patch.bubbleGapSidePx)
        : current.bubbleGapSidePx,
    bubbleCoreShrinkPct:
      patch.bubbleCoreShrinkPct !== undefined
        ? clampBubbleCoreShrink(patch.bubbleCoreShrinkPct)
        : current.bubbleCoreShrinkPct,
    bubbleVisible:
      typeof patch.bubbleVisible === 'boolean' ? patch.bubbleVisible : current.bubbleVisible,

    immersiveMode:
      typeof patch.immersiveMode === 'boolean' ? patch.immersiveMode : current.immersiveMode,
    micDeviceId: typeof patch.micDeviceId === 'string' ? patch.micDeviceId : current.micDeviceId,
    theme: patch.theme === 'dark' || patch.theme === 'light' ? patch.theme : current.theme,
  };

  const payload = `${JSON.stringify(next, null, 2)}\n`;
  // 先试已解析的落点，再试其它候选 —— 环境变了（沙箱/非沙箱）也能落到能写的地方
  const paths = [settingsPath(), ...candidateDirs().map((d) => join(d, 'settings.json'))];
  let lastError = '';
  for (const path of [...new Set(paths)]) {
    try {
      mkdirSync(dirname(path), { recursive: true });
      writeFileSync(path, payload, 'utf8');
      // 回读确认：写"成功"但文件没出现的情况实测发生过（原来那个 catch 吞掉了
      // EPERM，函数照样返回成功），所以用**回读**当判据，不相信 writeFileSync 没抛错。
      if (!existsSync(path)) throw new Error('写入后文件仍不存在');
      resolvedPath = path;
      return { settings: next, ok: true, path };
    } catch (error) {
      lastError = `${path}: ${(error as Error).message}`;
    }
  }
  return { settings: next, ok: false, path: paths[0], error: lastError };
}
