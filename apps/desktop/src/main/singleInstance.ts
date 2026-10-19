/**
 * 单实例检查（"禁止多开"）—— **不用 Electron 的 requestSingleInstanceLock**。
 *
 * ---- 为什么不用的（这是实测结论，不是偏好）----
 *
 * 项目里曾经有过 `if (!app.requestSingleInstanceLock()) app.quit()`，后来被删掉，
 * 因为它造成过：双击快捷方式后**什么都没发生**，退出码 0、日志一个字都没有。
 *
 * 这次把根因查清了。在这台机器上，`app.requestSingleInstanceLock()` **恒返回 false**：
 *
 *     空系统（没有任何实例在跑）        -> false
 *     调用两次                          -> false, false
 *     release 之后再请求                -> false
 *     加 --no-sandbox                   -> false
 *     指定项目 userData                 -> false
 *
 * 也就是说它**根本没在工作**，而不是"锁陈旧"。同时 `app.quit()` 在沙箱下
 * 也没能真正终止进程 —— 启动记录里能看到"启动失败于 single-instance"之后
 * 窗口和托盘照样被创建出来。两者叠加就是当年那个"看不见的静默失败"。
 *
 * 结论：**这个 API 在本环境不可依赖**，必须自己实现。而自己实现有个额外好处：
 * 逻辑能用纯 Node 测（项目里其它关键逻辑都做到了这一点）。
 *
 * ---- 做法 ----
 *
 *   锁文件（userData 下的 `instance.lock.json`）记录持有者的 PID；
 *   第二个实例读它，用 `process.kill(pid, 0)` 判断持有者是否真的活着：
 *
 *     · 没有锁 / 持有者已死  -> 清掉陈旧锁，自己成为主实例（**这是在解决"崩溃后再也起不来"**）
 *     · 持有者活着          -> 写一个"聚焦请求"，然后自己退出
 *
 *   主实例每秒看一次聚焦请求，收到就把窗口叫到最前。
 *
 * 为什么由调用方注入目录而不是写死：见 setStateDir 的注释 —— 放系统临时目录
 * 在受限环境下会 EPERM，导致程序第一次启动就失败。
 */

import { existsSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';

/** 聚焦请求的有效期：超过它就忽略并清理（防止陈旧文件让宠物无端弹出来） */
const FOCUS_REQUEST_TTL_MS = 15_000;

/**
 * 状态目录：锁文件与聚焦请求都放这里。
 *
 * ⚠️ 为什么不放 `os.tmpdir()`（我第一版就放在那里，实测被坑）：
 * 在受限环境下写系统临时目录会被拒绝 —— 实测报
 * `EPERM: operation not permitted, open 'C:\...\Temp\desktop-pet-instance.json'`，
 * 于是**程序第一次启动就失败**。
 *
 * 而 `userData` 是 Electron 保证可写的地方（它本来就在那儿维护 Cache、
 * Preferences、Local Storage）。放这里既可靠，也符合"应用的运行时状态"
 * 这个语义。由调用方注入，便于测试。
 */
let stateDirOverride: string | null = null;

/** 设置状态目录（主进程启动时用 app.getPath('userData') 调用一次） */
export function setStateDir(dir: string): void {
  stateDirOverride = dir;
}

function stateDir(): string {
  return stateDirOverride ?? os.tmpdir();
}

export function instanceLockPath(): string {
  return path.join(stateDir(), 'instance.lock.json');
}

export function focusRequestPath(): string {
  return path.join(stateDir(), 'focus-request.json');
}

/** 进程是否还活着。`kill(pid, 0)` 只做存在性与权限检查，不真的发信号。 */
export function isProcessAlive(pid: number): boolean {
  if (!Number.isFinite(pid) || pid <= 0) return false;
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    // EPERM = 进程存在但没权限（同用户进程不会发生）；ESRCH = 不存在
    return (error as NodeJS.ErrnoException).code === 'EPERM';
  }
}

export interface SingleInstanceResult {
  /** true = 本进程是主实例，可以继续启动 */
  isPrimary: boolean;
  /** 是否检测到一个**活着的**其他实例（false 且 isPrimary=false 表示异常） */
  otherAlive: boolean;
  /** 给人看的一句话 */
  detail: string;
}

export class SingleInstanceGuard {
  private timer: NodeJS.Timeout | null = null;

  constructor(
    private readonly onFocusRequested: () => void,
    private readonly log: (message: string) => void,
  ) {}

  /**
   * 判定本进程能否成为主实例。
   *
   * `pid` 与 `now` 可注入，纯为了可测试（要构造"持有者已死"这类状态）。
   */
  claim(pid: number = process.pid, now: number = Date.now()): SingleInstanceResult {
    const file = instanceLockPath();

    if (existsSync(file)) {
      let holder: number | null = null;
      try {
        const parsed = JSON.parse(readFileSync(file, 'utf-8')) as { pid?: number };
        holder = Number(parsed.pid ?? 0) || null;
      } catch {
        holder = null; // 文件损坏 -> 当陈旧处理
      }

      if (holder && holder !== pid && isProcessAlive(holder)) {
        return {
          isPrimary: false,
          otherAlive: true,
          detail: `已有实例在运行（PID ${holder}）`,
        };
      }

      // 持有者已死、或是自己上次留下的、或文件损坏 —— 都属于陈旧
      this.log(
        `清理陈旧实例锁（${holder ? `持有者 ${holder} 已不存在` : '内容不可解析'}）`,
      );
      try {
        rmSync(file, { force: true });
      } catch {
        /* 删不掉也不影响下面的写入 */
      }
    }

    try {
      writeFileSync(file, JSON.stringify({ pid, at: now }), 'utf-8');
    } catch (error) {
      // ⚠️ 写不进去时**必须降级放行**，不能当致命错误。
      //
      // 我自己第一版就是这么写的：写不进锁文件 -> 报"无法启动" -> 退出。
      // 结果在受限环境下（整个 userData 都不可写）程序**第一次启动就失败** ——
      // 这正是当年"静默退出"那个坑的翻版：一个**体验优化项**（禁止多开）
      // 把程序本身搞得起不来了。
      //
      // 正确的取舍：禁止多开只是锦上添花，拿不到锁就允许本次多开，
      // 并留下一条日志。用户最多是看到两只宠物（一眼可见、点"退出全部实例"即可），
      // 而不是"程序完全打不开且不知道为什么"。
      this.log(
        `无法写入实例锁文件（${file}）：${(error as Error).message} —— ` +
          '本次放弃单实例检查，允许启动（可能出现多个实例）',
      );
      return {
        isPrimary: true,
        otherAlive: false,
        detail: '无法写入实例锁，已降级放行（本次不做多开限制）',
      };
    }

    return { isPrimary: true, otherAlive: false, detail: '已取得实例锁，本进程是唯一实例' };
  }

  /** 第二个实例调用：请求已有实例把窗口叫到最前 */
  requestFocus(pid: number = process.pid, now: number = Date.now()): void {
    try {
      writeFileSync(focusRequestPath(), JSON.stringify({ pid, at: now }), 'utf-8');
      this.log(`已请求把已有窗口叫到最前（来自 pid ${pid}）`);
    } catch (error) {
      this.log(`写入聚焦请求失败：${(error as Error).message}`);
    }
  }

  /** 主实例调用：开始轮询聚焦请求 */
  start(): void {
    if (this.timer) return;
    this.timer = setInterval(() => this.checkOnce(), 1000);
    this.timer.unref?.();
  }

  stop(): void {
    if (this.timer) {
      clearInterval(this.timer);
      this.timer = null;
    }
  }

  /** 退出时清理锁文件，让下次启动不会误判 */
  release(): void {
    this.stop();
    try {
      rmSync(instanceLockPath(), { force: true });
    } catch {
      /* ignore */
    }
  }

  /**
   * 检查一次聚焦请求。**公开是为了可测试**（要构造"请求过期""文件损坏"等状态）。
   */
  checkOnce(now: number = Date.now()): void {
    const file = focusRequestPath();
    if (!existsSync(file)) return;

    let payload: { pid?: number; at?: number };
    try {
      payload = JSON.parse(readFileSync(file, 'utf-8')) as typeof payload;
    } catch {
      return; // 正在被写（读到半个 JSON），下一轮再看
    }

    const at = Number(payload.at ?? 0);
    const from = Number(payload.pid ?? 0);

    // 过期请求：清掉并忽略。**关键安全阀** ——
    // 没有它，一个陈旧文件会让宠物在每次启动后无端弹出来。
    if (!at || now - at > FOCUS_REQUEST_TTL_MS) {
      try {
        rmSync(file, { force: true });
      } catch {
        /* ignore */
      }
      this.log('忽略过期的聚焦请求（已清理）');
      return;
    }

    if (from === process.pid) return; // 自己写的，忽略

    try {
      rmSync(file, { force: true });
    } catch {
      /* ignore */
    }
    this.log(`收到聚焦请求（来自 pid ${from}），把窗口叫到最前`);
    this.onFocusRequested();
  }
}
