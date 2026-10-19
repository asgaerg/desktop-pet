/**
 * "退出全部实例"的协调器。
 *
 * 为什么需要它：宠物窗口刻意**不做单实例锁**（早先做过，但 Electron 的
 * userData 单实例锁一旦陈旧就会让程序静默退出 0）。代价是用户反复双击
 * 桌面快捷方式会堆出很多个实例 —— 实测发生过**同时跑 11 个**，屏幕上叠在
 * 一起，看起来像各种诡异 bug（"越拖越大"就有它的一份）。
 *
 * 而"一次只关一个"对用户毫无意义：他看得见的是十几个窗口，得关十几次。
 *
 * 做法：用一个**共享的请求文件**当广播通道。
 *   · 想全关时：写入 `{ requester, at }`；
 *   · 每个实例每秒看一眼这个文件；发现请求且 `requester` 不是自己 → 自己退出。
 *
 * 为什么用文件而不是别的方式：
 *   · 单实例锁已经证明"陈旧状态"是这套环境里的真实风险。请求文件带时间戳，
 *     **过期的请求会被忽略并清理**，不会让下次启动的实例刚起来就自杀。
 *   · 不需要开端口、不需要额外依赖 —— 与项目"零依赖"的做法一致。
 *
 * 放在 `userData` 而不是 `os.tmpdir()`：受限环境下写系统临时目录会 EPERM
 * （实测让程序第一次启动就失败），而 userData 是 Electron 保证可写的地方。
 */

import { existsSync, readFileSync, rmSync, statSync, writeFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';

/** 请求文件的有效期：超过这个时间的请求视为过期（防止陈旧文件导致自杀） */
const REQUEST_TTL_MS = 20_000;

let stateDirOverride: string | null = null;

/** 设置状态目录（主进程启动时用 app.getPath('userData') 调用一次） */
export function setQuitStateDir(dir: string): void {
  stateDirOverride = dir;
}

function stateDir(): string {
  return stateDirOverride ?? os.tmpdir();
}

export function quitRequestPath(): string {
  return path.join(stateDir(), 'quit-all-request.json');
}

export class QuitCoordinator {
  private timer: NodeJS.Timeout | null = null;

  constructor(
    /** 收到"别人要求全关"时调用 */
    private readonly onQuitRequested: () => void,
    private readonly log: (message: string) => void,
  ) {}

  /**
   * 广播"全部实例退出"。
   *
   * 只写文件、不等待 —— 每个实例（包括自己）都会在下一轮轮询里看到并退出。
   * 自己也会退出，所以这里不必特判。
   */
  broadcast(): void {
    try {
      writeFileSync(
        quitRequestPath(),
        JSON.stringify({ requester: process.pid, at: Date.now() }),
        'utf-8',
      );
      this.log(`已广播退出请求（pid ${process.pid}）`);
    } catch (error) {
      this.log(`写入退出请求失败：${(error as Error).message}`);
    }
  }

  /** 开始轮询（每秒一次，开销可以忽略） */
  start(): void {
    if (this.timer) return;
    this.timer = setInterval(() => this.checkOnce(), 1000);
    // 不要因为这个定时器让进程无法退出
    this.timer.unref?.();
  }

  stop(): void {
    if (this.timer) {
      clearInterval(this.timer);
      this.timer = null;
    }
  }

  /** 清掉请求文件（自己退出前调用，免得留下陈旧请求） */
  clear(): void {
    try {
      rmSync(quitRequestPath(), { force: true });
    } catch {
      /* 清不掉也不影响 */
    }
  }

  /**
   * 检查一次请求文件。**公开是为了可测试** —— 逻辑全在这里，而它需要
   * 构造"别人广播了""请求过期了""文件损坏了"等状态才能验证。
   * 生产路径由 start() 的定时器每秒调用一次。
   */
  checkOnce(): void {
    const file = quitRequestPath();
    if (!existsSync(file)) return;

    let payload: { requester?: number; at?: number };
    try {
      payload = JSON.parse(readFileSync(file, 'utf-8')) as typeof payload;
    } catch {
      // 文件正在被写（读到半个 JSON）—— 下一轮再看
      return;
    }

    const at = Number(payload.at ?? 0);
    const requestFrom = Number(payload.requester ?? 0);

    // 过期的请求：清掉并忽略。这条是**关键的安全阀** ——
    // 没有它，一次崩溃留下的请求文件会让之后每次启动都立刻自杀。
    if (!at || Date.now() - at > REQUEST_TTL_MS) {
      this.clear();
      this.log('忽略过期的退出请求（已清理）');
      return;
    }

    // 自己发的请求：由 broadcast 的调用方自行退出，这里不重复触发
    if (requestFrom === process.pid) return;

    this.log(`收到其他实例的退出请求（来自 pid ${requestFrom}），退出`);
    this.stop();
    this.clear();
    this.onQuitRequested();
  }
}

/** 文件是否存在的便捷判断（给"要不要提示用户"用） */
export function hasPendingQuitRequest(): boolean {
  const file = quitRequestPath();
  if (!existsSync(file)) return false;
  try {
    const { at } = JSON.parse(readFileSync(file, 'utf-8')) as { at?: number };
    return Boolean(at) && Date.now() - Number(at) <= REQUEST_TTL_MS;
  } catch {
    return false;
  }
}

/** 文件修改时间（调试用） */
export function quitRequestAgeMs(): number | null {
  const file = quitRequestPath();
  if (!existsSync(file)) return null;
  try {
    return Date.now() - statSync(file).mtimeMs;
  } catch {
    return null;
  }
}
