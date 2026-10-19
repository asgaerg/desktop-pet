/**
 * Python sidecar 的生命周期管理。
 *
 * 职责：
 *   1. 找到可用的 Python 解释器（不假设用户配好了 PATH）；
 *   2. 拉起 `python -m pet`，从 stdout 读那行 `PET_READY {...}` 拿到真实端口；
 *   3. 端口被占用时 sidecar 会自动让位，所以**绝不能硬编码端口**；
 *   4. 崩溃时按退避重启，但限制在窗口期内，避免变成无限重启风暴；
 *   5. 退出时先礼后兵：SIGTERM/close，超时再 kill。
 *
 * 刻意不做的事：不解析 sidecar 的业务日志，不替它做重试决策 ——
 * 这里的唯一契约就是那行 READY JSON 与进程存活。
 */

import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process';
import { existsSync } from 'node:fs';
import path from 'node:path';
import { EventEmitter } from 'node:events';
import type { Readable } from 'node:stream';

const READY_PREFIX = 'PET_READY ';

/**
 * 让操作系统分配一个空闲端口（`--port 0`），而不是写死 8931。
 *
 * ⚠️ 为什么这件事必须做：**桌面宠物允许同时运行多个实例**（我们刻意去掉了
 * 单实例检查 —— 那个检查会因一把陈旧的锁文件就让程序完全打不开，而"可能开出
 * 两只"的代价小得多，且一眼可见）。
 *
 * 但一旦允许多实例，写死端口就会打架：第二个实例的 sidecar 绑不上 8931，
 * 于是走退出重启流程，一分钟内撞满三次限额后彻底放弃 —— 表现是"第二只宠物
 * 起来了但不会说话"。
 *
 * Python 侧其实早就为多实例准备好了：`ws_server.start()` 支持 `port=0`
 * 由系统分配，也支持被占用时向上探测（`port_scan`）。所以这里只要别写死就行。
 *
 * 端口不是配置项：真实端口由 sidecar 通过 `PET_READY {"port":...}` 回报，
 * 客户端连的是回报值，不是这里的值。
 */
const DEFAULT_PORT = 0;

export interface ReadyInfo {
  port: number;
  protocolVersion: number;
  pid: number;
  brain: string;
  version?: string;
}

export interface SidecarOptions {
  projectRoot: string;
  pythonPath?: string;
  /**
   * 冻结后的 sidecar（打包态）：`resources/pet/pet.exe`（2026-10-18）。
   *
   * 有值就直接跑它（参数只有 flags，没有 `-m pet`）；没值就走
   * `findPython()` + `python -m pet` 的开发路径。
   */
  sidecarExe?: string;
  /** 可写数据根（打包态 = %APPDATA% 下的 pet-data），通过环境变量告诉 Python */
  dataDir?: string;
  configPath?: string;
  port?: number;
  logFile?: string;
  verbose?: boolean;
  /** 启动超时：超时视为失败并重试 */
  readyTimeoutMs?: number;
}

/** 在常见位置里找一个能用的 Python。顺序即优先级。 */
export function findPython(projectRoot: string, explicit?: string): string {
  const candidates: string[] = [];
  if (explicit) candidates.push(explicit);
  // 显式指定优先：托管环境（如各类 harness）可以在这里注入自己的解释器
  for (const envName of ['PET_PYTHON', 'DSH_PYTHON', 'PYTHON']) {
    const value = process.env[envName];
    if (value && value.trim()) candidates.push(value.trim());
  }

  const isWin = process.platform === 'win32';
  // 项目内虚拟环境优先：用户装了依赖就应该用它
  candidates.push(
    path.join(projectRoot, '.venv', isWin ? 'Scripts/python.exe' : 'bin/python3'),
    path.join(projectRoot, 'venv', isWin ? 'Scripts/python.exe' : 'bin/python3'),
  );
  // 最后交给 PATH
  candidates.push(isWin ? 'python.exe' : 'python3', 'python');

  for (const candidate of candidates) {
    // 带路径分隔符的做存在性检查；裸命令名交给 PATH 解析
    if (candidate.includes(path.sep) || candidate.includes('/')) {
      if (existsSync(candidate)) return candidate;
    } else {
      return candidate;
    }
  }
  throw new Error(
    '找不到 Python 解释器。请任选一种方式：\n' +
      '  1. 安装 Python 3.12+ 并确保它在 PATH 中；\n' +
      '  2. 在项目根目录建虚拟环境（.venv 或 venv）；\n' +
      '  3. 设置环境变量 PET_PYTHON 指向 python.exe。',
  );
}

/**
 * 退避重启的决策（纯函数，便于单独测试）。
 *
 * 返回下一次重启前的等待毫秒数；返回 null 表示**应当放弃**。
 *
 * 为什么要限流而不只是退避：如果 Python 环境是坏的（比如缺依赖），sidecar 会
 * 起来就崩。纯指数退避会让它每隔几分钟崩一次、永远崩下去，日志被刷满、
 * CPU 被白烧，而用户看到的是"宠物时不时抽一下"。所以一分钟内超过
 * `maxRestarts` 次就停下来，把一个可读的错误交给界面。
 *
 * @param history 历史重启时间戳（毫秒），会被就地裁剪到窗口内
 * @param now 当前时间戳（毫秒）
 */
export function computeRestartDelay(
  history: number[],
  now: number,
  options: { windowMs?: number; maxRestarts?: number; baseDelayMs?: number; maxDelayMs?: number } = {},
): number | null {
  const windowMs = options.windowMs ?? 60_000;
  const maxRestarts = options.maxRestarts ?? 3;
  const baseDelayMs = options.baseDelayMs ?? 500;
  const maxDelayMs = options.maxDelayMs ?? 30_000;

  // 只保留窗口内的记录
  for (let i = history.length - 1; i >= 0; i -= 1) {
    if (now - history[i] > windowMs) history.splice(i, 1);
  }
  if (history.length >= maxRestarts) return null;

  history.push(now);
  const attempt = history.length; // 第几次（从 1 开始）
  return Math.min(maxDelayMs, baseDelayMs * 2 ** (attempt - 1));
}

export class SidecarManager extends EventEmitter {
  private child: ChildProcessWithoutNullStreams | null = null;
  private ready: ReadyInfo | null = null;
  private stopping = false;
  private restarts: number[] = [];
  private readonly options: Required<
    Omit<SidecarOptions, 'pythonPath' | 'configPath' | 'logFile' | 'sidecarExe' | 'dataDir'>
  > &
    Pick<SidecarOptions, 'pythonPath' | 'configPath' | 'logFile' | 'sidecarExe' | 'dataDir'>;

  constructor(options: SidecarOptions) {
    super();
    this.options = {
      readyTimeoutMs: 20_000,
      port: DEFAULT_PORT,
      verbose: false,
      ...options,
    };
  }

  get readyInfo(): ReadyInfo | null {
    return this.ready;
  }

  get running(): boolean {
    return this.child !== null && this.child.exitCode === null;
  }

  /** 启动并等待 READY。失败抛错，由调用方决定是否重试。 */
  async start(): Promise<ReadyInfo> {
    if (this.running) throw new Error('sidecar 已在运行');
    /*
     * 打包态：直接跑冻结好的 `pet.exe`（机器上没有 Python）。
     * 开发态：老路径 `python -m pet`。
     */
    const frozen = this.options.sidecarExe;
    const useExe = Boolean(frozen && existsSync(frozen));
    const python = useExe ? (frozen as string) : findPython(this.options.projectRoot, this.options.pythonPath);
    const brainDir = path.join(this.options.projectRoot, 'apps', 'brain');

    /*
     * ## ⚠️ cwd 必须是 `simple` 词典所在目录，**不是** brain 目录
     *
     * cppjieba（`simple.dll` 内部用来做中文分词）**硬编码**用相对路径
     * `./dict/jieba.dict.utf8` 找词典 —— 实测确认过：
     *
     *     cwd = libsimple-windows-x64（dict/ 就在旁边） -> ✅ 分词正常
     *     cwd = 仓库根 / TEMP / dict 目录本身          -> ❌ 词典加载失败
     *
     * 而词典加载失败**不会报错**，只会静默退化成"逐字切"，
     * 于是「冰咖啡」会命中「冰美式咖啡」—— 搜索质量悄悄变差，不查日志根本不知道。
     *
     * ## 为什么改 cwd 是安全的
     *
     * brain 侧所有路径都用 `Path(__file__).resolve()` 推导（见 `main.py` 的
     * `_project_root`），**没有任何地方依赖 cwd**。所以 cwd 只用来满足这个 DLL，
     * 模块导入靠 `PYTHONPATH` 补回来。
     */
    const simpleDir = path.join(
      this.options.projectRoot,
      'apps',
      'desktop',
      'pet-data',
      'vendor',
      'simple',
      'libsimple-windows-x64',
    );
    const dictOk = existsSync(path.join(simpleDir, 'dict', 'jieba.dict.utf8'));
    const workDir = dictOk ? simpleDir : brainDir;

    const args = useExe ? ['--port', String(this.options.port)] : ['-m', 'pet', '--port', String(this.options.port)];
    if (this.options.configPath) args.push('--config', this.options.configPath);
    if (this.options.logFile) args.push('--log-file', this.options.logFile);
    if (this.options.verbose) args.push('-v');

    this.emit(
      'log',
      `启动 sidecar：${python} ${args.join(' ')}（cwd=${workDir}${dictOk ? '，中文分词词典已就位' : '，⚠️ 未找到分词词典，中文检索会退化为逐字'}）`,
    );

    const child = spawn(python, args, {
      cwd: workDir,
      env: {
        ...process.env,
        // 让 sidecar 的 stdout 无缓冲，READY 才能立刻被读到
        PYTHONUNBUFFERED: '1',
        PYTHONIOENCODING: 'utf-8',
        /*
         * cwd 已经被让给 DLL，所以 `-m pet` 要靠 PYTHONPATH 才能找到包。
         * 放在最前面，确保优先于其它 PYTHONPATH 项。
         */
        PYTHONPATH: brainDir + (process.env.PYTHONPATH ? `;${process.env.PYTHONPATH}` : ''),
        // 打包态：告诉 Python 只读资源根与可写数据根（见 pet/paths.py）
        ...(this.options.dataDir
          ? { PET_PROJECT_ROOT: this.options.projectRoot, PET_DATA_DIR: this.options.dataDir }
          : {}),
        /*
         * 凭据文件的**绝对路径** —— 由主进程告诉 Python，避免两边各自猜路径。
         *
         * 为什么需要：用户目录不一定可写（受限环境里写 ~/.desktop-pet/ 会 EPERM），
         * 那种情况下界面会把密钥退到项目旁的 pet-data/。如果 Python 只认用户目录，
         * 就会出现"界面显示已保存、大脑却报没密钥"这种最难查的不一致。
         */
        ...(process.env.PET_CREDENTIALS_FILE
          ? { PET_CREDENTIALS_FILE: process.env.PET_CREDENTIALS_FILE }
          : {}),
      },
      windowsHide: true,
    }) as ChildProcessWithoutNullStreams;
    this.child = child;
    this.stopping = false;

    // sidecar 的日志走 stderr，转成事件给上层写文件/控制台
    child.stderr.setEncoding('utf-8');
    child.stderr.on('data', (chunk: string) => {
      for (const line of chunk.split(/\r?\n/)) {
        if (line.trim()) this.emit('log', `[brain] ${line}`);
      }
    });

    const info = await this.waitForReady(child);
    this.ready = info;
    this.emit('ready', info);

    child.on('exit', (code, signal) => {
      this.child = null;
      this.ready = null;
      this.emit('exit', { code, signal });
      if (!this.stopping) this.scheduleRestart();
    });
    child.on('error', (error) => {
      this.emit('log', `sidecar 进程错误：${error.message}`);
    });

    return info;
  }

  /** 从 stdout 逐行找 READY；同时限时，避免永久挂起。 */
  private waitForReady(child: ChildProcessWithoutNullStreams): Promise<ReadyInfo> {
    return new Promise<ReadyInfo>((resolve, reject) => {
      let buffer = '';
      let settled = false;

      const timer = setTimeout(() => {
        if (settled) return;
        settled = true;
        cleanup();
        reject(new Error(`sidecar 启动超时（${this.options.readyTimeoutMs}ms 内未收到 PET_READY）`));
      }, this.options.readyTimeoutMs);

      const onData = (chunk: string | Buffer) => {
        buffer += chunk.toString();
        let index: number;
        while ((index = buffer.indexOf('\n')) >= 0) {
          const line = buffer.slice(0, index).trim();
          buffer = buffer.slice(index + 1);
          if (!line.startsWith(READY_PREFIX)) {
            if (line) this.emit('log', `[brain:stdout] ${line}`);
            continue;
          }
          if (settled) continue;
          settled = true;
          clearTimeout(timer);
          try {
            resolve(JSON.parse(line.slice(READY_PREFIX.length)) as ReadyInfo);
          } catch (error) {
            reject(new Error(`无法解析 PET_READY：${(error as Error).message}`));
          }
        }
      };

      const onExit = (code: number | null) => {
        if (settled) return;
        settled = true;
        cleanup();
        reject(new Error(`sidecar 在就绪前退出（code=${code}）`));
      };

      const cleanup = () => {
        child.stdout.off('data', onData);
        child.off('exit', onExit);
      };

      child.stdout.setEncoding('utf-8');
      child.stdout.on('data', onData);
      child.on('exit', onExit);
    });
  }

  /**
   * 崩溃重启：退避 + 窗口限流。决策在 `computeRestartDelay` 里（纯函数、可测）。
   */
  private scheduleRestart(): void {
    const delay = computeRestartDelay(this.restarts, Date.now());
    if (delay === null) {
      this.emit(
        'log',
        `sidecar 一分钟内已重启 ${this.restarts.length} 次，停止自动重启（请检查 Python 环境）`,
      );
      this.emit('gaveUp');
      return;
    }
    this.emit('log', `${delay}ms 后重启 sidecar（第 ${this.restarts.length} 次）`);
    setTimeout(() => {
      if (this.stopping) return;
      this.start().catch((error) => {
        this.emit('log', `重启失败：${(error as Error).message}`);
      });
    }, delay);
  }

  /** 平滑停止：先请求退出，超时才强杀。 */
  async stop(timeoutMs = 4000): Promise<void> {
    this.stopping = true;
    const child = this.child;
    if (!child || child.exitCode !== null) return;

    await new Promise<void>((resolve) => {
      const timer = setTimeout(() => {
        this.emit('log', 'sidecar 未在超时内退出，强制结束');
        child.kill('SIGKILL');
        resolve();
      }, timeoutMs);
      child.once('exit', () => {
        clearTimeout(timer);
        resolve();
      });
      // Windows 上 close() 走 stdin 关闭语义；SIGTERM 由 Node 模拟
      child.kill('SIGTERM');
    });
  }
}

/** 供上层读取 stdout 用（测试/工具）。 */
export type SidecarStdout = Readable;
