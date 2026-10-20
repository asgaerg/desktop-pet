/**
 * 全屏检测（"别人是不是在全屏"）—— "边缘探头"的触发条件。
 *
 * ---- 为什么需要它 ----
 *
 * 用户要求两件**互相冲突**的事：
 *   · 宠物要能显示在**任何应用之上**（包括全屏游戏、全屏视频）；
 *   · 但不能挡住任务栏 —— 挡住的话点不到开始菜单和托盘。
 *
 * 单一窗口层级做不到：`normal` 浮在普通窗口之上但**不盖全屏**；
 * `screen-saver` 能盖全屏但**也盖住任务栏**。
 *
 * 所以只能**按需切换**：检测到别人全屏时才升到 screen-saver 层级，
 * 回到桌面时再降回 normal。这个模块负责"检测"这一半。
 *
 * ---- 怎么检测：常驻 PowerShell 进程 ----
 *
 * Electron 没有"前台窗口是不是全屏"的接口，只能问 Win32 API。
 * 真正的探测逻辑在 `tools/probe-fullscreen.ps1`（独立文件，不是内嵌字符串，
 * 那里写了判据怎么来的、以及踩过的坑）。
 *
 * ⚠️ **常驻，而不是每次起一个新进程**。这是这一版最重要的改动：
 *   原来每次检测 spawn 一个 PowerShell（约 400ms，因为每次都要
 *   `Add-Type` 重新编译 C#），代价是
 *     · 空闲轮询吃掉 8~20% 单核；
 *     · **每次 spawn 都是一次新的失败机会** —— 实测在 Electron 里就一直失败，
 *       而在纯 Node 里却正常，查了很久没有结论；
 *     · 慢到没法高频轮询，"退出全屏后宠物迟钝"和"CPU 占用高"无法同时解决。
 *   改成常驻后：编译只做一次，单次查询约 1ms（实测 3 次共 62ms），
 *   于是可以高频轮询，两个问题一起消失。
 *
 * ⚠️ 为什么脚本必须带 UTF-8 BOM：PowerShell 5.1 对没有 BOM 的 .ps1 按 ANSI
 *   （本机 GBK）读取，中文注释被误读后可能产生破坏语法的字符，脚本直接报
 *   语法错误。同一个文件用 PowerShell 直接跑正常、被 Node 拉起来就报错，
 *   就是这个问题。
 *
 * ⚠️ 为什么要把 PowerShell 的 TEMP 指到别处：`Add-Type` 编译 C# 时要把中间
 *   产物写进 `%TEMP%`，而实测本环境 **`%TEMP%` 不可写**（EPERM），于是报
 *   「对路径 ...\AppData\Local\Temp\xxxx.tmp 的访问被拒绝」。
 *   只把 .ps1 挪出 %TEMP% 是**不够**的 —— 这一点查了很久才看清。
 *
 * ---- 失败策略 ----
 *
 * **检测失败一律当作"不是全屏"**：宁可让宠物被全屏盖住，
 * 也不要因为检测出错把层级升上去盖住任务栏（那会让用户点不到开始菜单）。
 */

import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process';
import { existsSync, mkdtempSync, readdirSync, rmSync, statSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';

export interface ForegroundInfo {
  /** 前台窗口是否全屏 */
  fullscreen: boolean;
  /** 前台窗口标题（调试与日志用） */
  title: string;
  /** 前台窗口矩形 */
  rect: { x: number; y: number; width: number; height: number } | null;
  /** 该窗口所在屏幕的矩形 */
  screen: { x: number; y: number; width: number; height: number } | null;
  /** 检测是否成功（false 时 fullscreen 恒为 false） */
  ok: boolean;
  /** 出错信息（ok=false 时用来排查） */
  error?: string;
}

/**
 * 解析探测脚本的输出行：`full|rect|screen|title|caption|className`
 * 例：`1|0,0,1536,864|0,0,1536,864|YouTube|False|Chrome_WidgetWin_1`
 *
 * `SHELL:` 前缀出现在 className 位置时表示"这是系统外壳窗口"（桌面、任务栏、
 * 开始菜单等），必须**当作非全屏** —— 桌面本身无边框且铺满整屏，
 * 不排除的话会被判成全屏，表现是宠物在紧凑/常规之间反复抖动。
 */
export function parseProbeOutput(stdout: string): ForegroundInfo {
  const fail: ForegroundInfo = {
    fullscreen: false,
    title: '',
    rect: null,
    screen: null,
    ok: false,
  };
  const line = stdout.trim().split(/\r?\n/).filter(Boolean).pop() ?? '';
  if (!line) return { ...fail, error: '探测脚本没有输出' };
  if (line.startsWith('0|||ERR:')) return { ...fail, error: line.slice(8) };

  const parts = line.split('|');
  const rectRaw = parts[1];
  const screenRaw = parts[2];
  if (rectRaw === undefined || screenRaw === undefined) {
    return { ...fail, error: `输出格式不对：${line.slice(0, 120)}` };
  }
  // 没有前台窗口时脚本输出 `0|||`（没有矩形）—— 这不算失败
  if (!rectRaw || !screenRaw) return { ...fail, ok: true, title: parts[3] ?? '' };

  const nums = (text: string): number[] => text.split(',').map((v) => Number(v));
  const [rl, rt, rr, rb] = nums(rectRaw);
  const [sx, sy, sw, sh] = nums(screenRaw);
  if (![rl, rt, rr, rb, sx, sy, sw, sh].every((v) => Number.isFinite(v))) {
    return { ...fail, error: `矩形解析失败：${rectRaw} / ${screenRaw}` };
  }

  // 字段：0=full 1=rect 2=screen 3=title 4=caption 5=className
  // 最后**两个**字段一定是 caption 和 className（脚本固定这么输出），
  // 标题里可能含 `|`，所以标题取它们之间的部分 —— 别用"到末尾"，
  // 那会把 caption / className 一起并进标题（实测出现过
  // `title="某窗口|True"` 这种带后缀的怪值）。
  const className = parts.length >= 6 ? parts[parts.length - 1] : '';
  const titleEnd = parts.length >= 6 ? parts.length - 2 : parts.length;
  const title = parts.slice(3, titleEnd).join('|').trim();
  const isShell = className.startsWith('SHELL:');

  return {
    // 外壳窗口一律不算全屏 —— 桌面/任务栏本来就"铺满屏幕且没有标题栏"
    fullscreen: !isShell && (parts[0] ?? '0').trim() === '1',
    title,
    rect: { x: rl, y: rt, width: rr - rl, height: rb - rt },
    screen: { x: sx, y: sy, width: sw, height: sh },
    ok: true,
  };
}

/** 探测脚本路径（dev 与打包后都是 tools/ 下的同一份） */
export function resolveProbeScript(): string | null {
  const candidates = [
    resolve(dirname(process.argv[1] ?? '.'), '..', 'tools', 'probe-fullscreen.ps1'),
    resolve(process.cwd(), 'tools', 'probe-fullscreen.ps1'),
    resolve(__dirname, '..', 'tools', 'probe-fullscreen.ps1'),
  ];
  // 打包后脚本在 resources/apps/desktop/tools/（见 electron-builder 的 extraResources）。
  // 前三个都以 dist / cwd 推理，在 asar 里必然落空 —— 少了这条会每秒刷一行警告。
  if (process.resourcesPath) {
    candidates.push(
      resolve(process.resourcesPath, 'apps', 'desktop', 'tools', 'probe-fullscreen.ps1'),
    );
  }
  for (const path of candidates) {
    try {
      if (existsSync(path)) return path;
    } catch {
      /* 试下一个 */
    }
  }
  return null;
}

let preferredDir: string | undefined;
let probeTempDir: string | null = null;
/** 诊断输出（模块级，因为临时目录在类实例之外确定） */
let diagSink: ((line: string) => void) | null = null;

/**
 * 设置可写的工作目录（**必须调用**）。
 *
 * 实测本环境 `%TEMP%` 不可写，而 `app.getPath('userData')` 可写。
 * 这里建一个子目录同时充当 PowerShell 的 TEMP（Add-Type 要往那里写）。
 */
export function setFullscreenProbeDir(dir: string): void {
  preferredDir = dir;
  probeTempDir = null;
}

const PROBE_DIR_PREFIX = 'pet-fs-';
/**
 * 多久以前的探测临时目录算"陈旧残留"。
 *
 * ⚠️ 不能见到 `pet-fs-*` 就删：开发/测试时**另一个实例**可能正在用它
 * （`test:e2e`、自检都会另起进程）。按 mtime 划线，只动一小时以前的 ——
 * 陈旧残留必删，活跃目录一定不动。
 */
const PROBE_DIR_STALE_MS = 60 * 60 * 1000;

/**
 * 清扫**上一次进程**留下的探测临时目录（2026-10-14 加）。
 *
 * ## 为什么需要
 *
 * `mkdtempSync` 每个会话建一个 `pet-fs-xxxx`，正常退出时在 `stop()` 里删掉。
 * 但进程**被杀/崩溃**（开发时天天发生）就删不掉 —— 实测 `apps/desktop/` 下
 * 累积了 **178 个**空目录，把工作目录搞得没法看。
 *
 * 清扫时机放在**建新目录之前**（`ensureTempDir` 里），并且只删 mtime 超过
 * `PROBE_DIR_STALE_MS` 的，理由见常量注释。
 *
 * ⚠️ 导出是给 `test-main.mjs` 做断言的（"陈旧删、活跃留、非本前缀不动"），
 * 这几条规则错一条就是误删活数据或者清理形同虚设。
 */
export function sweepStaleProbeDirs(bases: readonly string[]): number {
  let removed = 0;
  for (const base of new Set(bases)) {
    let entries: string[];
    try {
      entries = readdirSync(base);
    } catch {
      continue; // 目录不存在/不可读：不是错误
    }
    for (const entry of entries) {
      if (!entry.startsWith(PROBE_DIR_PREFIX)) continue;
      const full = join(base, entry);
      try {
        const info = statSync(full);
        if (!info.isDirectory()) continue;
        if (Date.now() - info.mtimeMs < PROBE_DIR_STALE_MS) continue;
        rmSync(full, { recursive: true, force: true });
        removed += 1;
      } catch {
        /* 正被占用 / 不可删：跳过，不影响探测本身 */
      }
    }
  }
  if (removed > 0) diagSink?.(`已清扫 ${removed} 个陈旧的全屏探测临时目录`);
  return removed;
}

function ensureTempDir(): string | null {
  if (probeTempDir) return probeTempDir;
  // 按"最可能可写"的顺序试。
  // ⚠️ 必须有多个候选：实测 `userData` 在**普通 shell 里可写**，但从 Electron
  // 进程里 `mkdtempSync` 会失败（沙箱限制），而一旦失败就会一路退化到
  // `%TEMP%` —— 那里更不可写，表现为 Add-Type 报 EPERM，
  // 而日志里只看到"检测失败"，完全看不出是临时目录的问题。
  const candidates = [
    preferredDir,
    tmpdir(),
    // 最后兜底：项目目录。这套环境里它是**保证可写**的落点
    // （startup.log 一直写在这里，从没失败过）。
    dirname(process.argv[1] ?? __filename),
    process.cwd(),
  ].filter((v): v is string => Boolean(v));

  // 建新的之前先扫一遍旧残留（见 `sweepStaleProbeDirs`）
  sweepStaleProbeDirs(candidates);

  for (const base of candidates) {
    try {
      probeTempDir = mkdtempSync(join(base, 'pet-fs-'));
      diagSink?.(`全屏探测的临时目录：${probeTempDir}`);
      return probeTempDir;
    } catch {
      /* 试下一个 */
    }
  }
  // 全部失败也要说出来 —— 否则只会看到后续的 Add-Type 报 EPERM，
  // 完全看不出根因在临时目录（这个坑实际踩过）。
  diagSink?.(`全屏探测找不到可写的临时目录（试过：${candidates.join(' / ')}）`);
  return null;
}

/**
 * 常驻探测进程。
 *
 * 一次只允许一个在途请求（`pending`），因为协议是"一问一答"：
 * 并发提问会让回复错位。轮询器本身就是串行的，所以够用。
 */
export class FullscreenProbeProcess {
  private child: ChildProcessWithoutNullStreams | null = null;
  private ready = false;
  private buffer = '';
  private pending: ((line: string) => void) | null = null;
  private waiters: Array<(line: string) => void> = [];
  private stopped = false;
  private lastError: string | null = null;
  /** 最近一次原始输出（排查用） */
  private lastRaw = '';

  constructor(private readonly onDiagnostic?: (line: string) => void) {}

  get isRunning(): boolean {
    return Boolean(this.child) && this.ready && !this.stopped;
  }

  get error(): string | null {
    return this.lastError;
  }

  get lastOutput(): string {
    return this.lastRaw;
  }

  /**
   * 启动常驻进程并等待 READY。
   * 返回是否启动成功（失败时调用方应当继续按"不是全屏"处理）。
   */
  start(timeoutMs = 15000): Promise<boolean> {
    if (this.child) return Promise.resolve(this.ready);
    const script = resolveProbeScript();
    if (!script) {
      this.lastError = '找不到 tools/probe-fullscreen.ps1';
      this.onDiagnostic?.(`全屏检测无法启动：${this.lastError}`);
      return Promise.resolve(false);
    }
    const temp = ensureTempDir();

    return new Promise((resolvePromise) => {
      let settled = false;
      const finish = (ok: boolean): void => {
        if (settled) return;
        settled = true;
        resolvePromise(ok);
      };

      try {
        this.child = spawn(
          'powershell.exe',
          ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', script],
          {
            windowsHide: true,
            env: { ...process.env, ...(temp ? { PET_PROBE_TMP: temp, TEMP: temp, TMP: temp } : {}) },
          },
        );
      } catch (error) {
        this.lastError = `spawn 失败：${(error as Error).message}`;
        this.onDiagnostic?.(`全屏检测无法启动：${this.lastError}`);
        finish(false);
        return;
      }

      const timer = setTimeout(() => {
        this.lastError = '等待 READY 超时';
        this.onDiagnostic?.(`全屏检测启动超时（${timeoutMs}ms）：${this.lastError}`);
        this.stop();
        finish(false);
      }, timeoutMs);

      this.child.stdout.setEncoding('utf8');
      this.child.stdout.on('data', (chunk: string) => this.onData(chunk));
      this.child.stderr.setEncoding('utf8');
      this.child.stderr.on('data', (chunk: string) => {
        // stderr 是排查的唯一线索，必须留下来（踩过"只报失败、没有原因"的坑）
        const text = String(chunk).trim();
        if (text) {
          this.lastError = text.slice(0, 400);
          this.onDiagnostic?.(`全屏探测进程报错：${this.lastError}`);
        }
      });
      this.child.on('error', (error) => {
        this.lastError = error.message;
        this.onDiagnostic?.(`全屏探测进程出错：${error.message}`);
        finish(false);
      });
      this.child.on('exit', (code) => {
        clearTimeout(timer);
        this.ready = false;
        this.child = null;
        const waiter = this.pending ?? this.waiters.shift();
        if (waiter) waiter('');
        this.pending = null;
        if (!this.stopped) {
          this.onDiagnostic?.(`全屏探测进程退出（code=${code}），下次检测会重启`);
        }
        finish(false);
      });

      // READY 到达才算启动成功（Add-Type 编译完才 ready）
      const checkReady = setInterval(() => {
        if (this.ready) {
          clearInterval(checkReady);
          clearTimeout(timer);
          finish(true);
        }
      }, 20);
      this.child.on('exit', () => clearInterval(checkReady));
    });
  }

  private onData(chunk: string): void {
    this.buffer += chunk;
    let index = this.buffer.indexOf('\n');
    while (index >= 0) {
      const line = this.buffer.slice(0, index).replace(/\r$/, '');
      this.buffer = this.buffer.slice(index + 1);
      this.handleLine(line);
      index = this.buffer.indexOf('\n');
    }
  }

  private handleLine(line: string): void {
    if (!this.ready) {
      if (line.trim() === 'READY') {
        this.ready = true;
        this.onDiagnostic?.('全屏检测已就绪（常驻探测进程）');
      }
      return;
    }
    const waiter = this.pending ?? this.waiters.shift();
    this.pending = null;
    if (waiter) waiter(line);
  }

  /** 查询一次。失败返回 `ok:false` 的 ForegroundInfo（调用方按"不是全屏"处理）。 */
  async query(timeoutMs = 3000): Promise<ForegroundInfo> {
    if (!this.child || !this.ready) {
      const ok = await this.start();
      if (!ok) {
        return {
          fullscreen: false,
          title: '',
          rect: null,
          screen: null,
          ok: false,
          error: this.lastError ?? '探测进程未就绪',
        };
      }
    }
    const child = this.child;
    if (!child) {
      return {
        fullscreen: false,
        title: '',
        rect: null,
        screen: null,
        ok: false,
        error: this.lastError ?? '探测进程不存在',
      };
    }

    const line = await new Promise<string>((resolvePromise) => {
      const timer = setTimeout(() => {
        this.pending = null;
        resolvePromise('');
      }, timeoutMs);
      const wrapper = (value: string): void => {
        clearTimeout(timer);
        resolvePromise(value);
      };
      this.pending = wrapper;
      try {
        child.stdin.write('probe\n');
      } catch (error) {
        this.lastError = (error as Error).message;
        wrapper('');
      }
    });

    this.lastRaw = line;
    if (!line) {
      return {
        fullscreen: false,
        title: '',
        rect: null,
        screen: null,
        ok: false,
        error: this.lastError ?? '探测没有回复（超时或进程已退出）',
      };
    }
    const info = parseProbeOutput(line);
    if (!info.ok && info.error) this.lastError = info.error;
    return info;
  }

  stop(): void {
    this.stopped = true;
    this.ready = false;
    const child = this.child;
    this.child = null;
    if (!child) return;
    try {
      child.stdin.end(); // 脚本 ReadLine 收到 EOF 会自己退出
    } catch {
      /* 已经关了 */
    }
    const killer = setTimeout(() => {
      try {
        child.kill();
      } catch {
        /* 已经退了 */
      }
    }, 1500);
    child.on('exit', () => clearTimeout(killer));
  }
}

export interface FullscreenWatcherOptions {
  /**
   * 轮询间隔（普通状态）。
   *
   * 常驻探测下单次查询约 1ms，所以这里可以放心设小 ——
   * 但也没必要太快：用户切到全屏后晚 1 秒被发现完全无感。
   */
  idleIntervalMs?: number;
  /**
   * 全屏时的轮询间隔。比 idle 更短：退出全屏时希望宠物马上收回屏幕内。
   */
  fullscreenIntervalMs?: number;
  /**
   * 状态翻转要**连续几次**一致才认（去抖，2026-10-20 加）。
   *
   * ## 为什么需要（真机日志，06:47）
   *
   * 用户的 B 站标签页**本来就不是全屏**（`全屏检测：非全屏，前台=…bilibili…Edge`），
   * 探测却连着报了两次「全屏」：
   *
   *     06:47:41.273  进入全屏模式（…bilibili…Edge），原状态 探头·贴右边
   *     06:47:42.501  退出全屏，恢复位置 1390,568（…不自动恢复探头）
   *     06:47:43.519  全屏检测：非全屏
   *     06:47:44.527  进入全屏模式（…同一条），原状态 探头·贴右边
   *     06:47:45.761  退出全屏，恢复位置 1389,568
   *
   * 4 秒里「进/退」闪了两下，每次「退出」都会把宠物**拉直**并收回常态尺寸 ——
   * 用户看到的正是「退出全屏后探头旋转没了」。
   *
   * 探测判据是"窗口矩形铺满屏幕 + 没有标题栏"。浏览器静止时也可能有一瞬间
   * 报告"无标题栏"（全屏切换的过渡帧、Win32 样式位更新时序），
   * 单次读数**不可信**，所以要求连续 N 次一致才翻转。
   *
   * 代价：真实变化最多晚 `N × 轮询间隔` 被确认（默认 2 次，用户无感）。
   * 设成 1 = 关掉去抖（行为与 10-20 之前一致，测试用）。
   */
  confirmSamples?: number;
  /** 状态变化回调（只在变化时触发） */
  onChange: (info: ForegroundInfo, fullscreen: boolean) => void;
  /**
   * 诊断日志回调。**必须接上** —— 检测是静默的，失败了只会"什么都不发生"，
   * 没有日志就完全查不动（这个功能已经因此查了很久）。
   */
  onDiagnostic?: (line: string) => void;
}

/**
 * 全屏状态轮询器。
 *
 * 只在**状态变化**时回调（不然调用方每几秒被叫一次，容易写成反复重设窗口层级）。
 * `start()` 会立刻检测一次，不等第一个间隔。
 */
export class FullscreenWatcher {
  private timer: NodeJS.Timeout | null = null;
  private running = false;
  private fullscreen = false;
  private probing = false;
  private stopped = false;
  private probes = 0;
  /** 连续几次"看起来要翻转"（去抖计数，见 `confirmSamples`） */
  private pendingFlip = 0;
  private readonly probe: FullscreenProbeProcess;

  constructor(private readonly options: FullscreenWatcherOptions) {
    diagSink = options.onDiagnostic ?? null;
    this.probe = new FullscreenProbeProcess(options.onDiagnostic);
  }

  get isFullscreen(): boolean {
    return this.fullscreen;
  }

  get probeCount(): number {
    return this.probes;
  }

  start(): void {
    if (this.running) return;
    this.running = true;
    this.stopped = false;
    void this.tick();
  }

  stop(): void {
    this.stopped = true;
    this.running = false;
    if (this.timer) clearTimeout(this.timer);
    this.timer = null;
    this.probe.stop();
    // 清掉探测用的临时目录（Add-Type 的编译产物）。不清的话每跑一次
    // 就在磁盘上留一个 pet-fs-xxxx 空目录，日积月累。
    if (probeTempDir) {
      try {
        rmSync(probeTempDir, { recursive: true, force: true });
      } catch {
        /* 清理失败不影响功能 */
      }
      probeTempDir = null;
    }
  }

  /** 检测一次并按需回调；无论成功失败都会安排下一次 */
  async probeOnce(): Promise<ForegroundInfo> {
    const info = await this.probe.query();
    this.probes += 1;
    if (!info.ok) {
      // 检测失败必须说出来：否则表现只是"宠物永远不探头"，看不出原因。
      this.options.onDiagnostic?.(
        `全屏检测失败（第 ${this.probes} 次）：${info.error ?? '未知原因'}`,
      );
      return info;
    }
    /*
     * 去抖（见 `confirmSamples`）：单次读数与当前状态不一致时**先不动**，
     * 只有连续 N 次都一致才认这个翻转。
     *
     * ⚠️ 候选值一旦被推翻就清零，不能累加 —— 否则 4 次里出现 3 次真 1 次假
     * 也会被凑够"连续 3 次"，去抖等于没做。
     */
    const need = Math.max(1, Math.floor(this.options.confirmSamples ?? 1));
    const seen = info.fullscreen;
    if (seen === this.fullscreen) {
      this.pendingFlip = 0;
      if (this.probes % 15 === 0) {
        // 周期性汇报"它现在看到什么"。
        // 只记状态变化是不够的：如果探测一直返回同一个值（比如因为前台窗口
        // 始终是宠物自己），日志里就什么都没有，完全看不出"在跑但看错了"。
        this.options.onDiagnostic?.(
          `全屏检测进行中（第 ${this.probes} 次）：${info.fullscreen ? '全屏' : '非全屏'}，` +
            `前台=${info.title || '(无标题)'}`,
        );
      }
      return info;
    }
    this.pendingFlip += 1;
    if (this.pendingFlip < need) {
      // 还没到阈值：记一笔（排查"抖动被吃掉"时要知道它曾经想过翻转）
      this.options.onDiagnostic?.(
        `全屏检测：疑似${seen ? '进入全屏' : '退出全屏'}（${this.pendingFlip}/${need}，未确认）` +
          `，前台=${info.title || '(无标题)'}`,
      );
      return info;
    }
    this.pendingFlip = 0;
    this.fullscreen = seen;
    this.options.onChange(info, this.fullscreen);
    return info;
  }

  private async tick(): Promise<void> {
    if (this.stopped || this.probing) return;
    this.probing = true;
    try {
      await this.probeOnce();
    } finally {
      this.probing = false;
    }
    if (this.stopped) return;
    const delay = this.fullscreen
      ? (this.options.fullscreenIntervalMs ?? 400)
      : (this.options.idleIntervalMs ?? 1000);    this.timer = setTimeout(() => void this.tick(), delay);
  }
}
