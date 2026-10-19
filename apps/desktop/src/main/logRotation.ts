/**
 * 日志文件的**按大小轮转**（主进程与测试共用的一份实现）。
 *
 * ## 为什么单独一个模块
 *
 * 这段逻辑原来长在 `index.ts` 里，而 `index.ts` 被 electron 绑死、无法在
 * 测试里加载 —— 于是"轮转到底有没有生效"只能靠肉眼看磁盘。
 * 独立出来后 `test-main.mjs` 能真建临时文件、真触发轮转、真断言备份链。
 *
 * ## 🔴 2026-10-14：为什么必须"写入时"轮转
 *
 * 原来的实现只在**启动时**检查一次大小。而桌宠是**长期挂机**的程序：
 *
 *     实测 main.log.1 = 110 MB、main.log.2 = 27 MB、python.log.1 = 41 MB
 *     而上限设的是 20 MB —— 数字对不上，因为不重启就永远不轮转。
 *
 * 所以调用方（`index.ts` 的 `log()`）现在按字节**记账**，写之前判断会不会
 * 写超，超了当场轮转。本模块只提供"怎么转"，不关心"何时转"。
 */

import { existsSync, renameSync, statSync, unlinkSync } from 'node:fs';

/** 单个日志文件的上限。 */
export const LOG_MAX_BYTES = 20 * 1024 * 1024;
/** 保留的备份份数（`file.1` … `file.N`，最老的那份直接删）。 */
export const LOG_BACKUP_COUNT = 3;

export interface LogRotationOptions {
  maxBytes?: number;
  backups?: number;
}

/**
 * 无条件轮转：`file` -> `file.1` -> `file.2` -> … -> 删除最老一份。
 *
 * @returns 是否真的转成功。**失败不抛异常** —— 日志写不进去绝不能反过来
 *          把主流程带崩（文件被占用、磁盘满都要能继续跑）。
 */
export function rotateLogNow(file: string, options: LogRotationOptions = {}): boolean {
  const backups = options.backups ?? LOG_BACKUP_COUNT;
  try {
    // 从最老的开始往后挪，最后删掉最老那份
    const oldest = `${file}.${backups}`;
    if (existsSync(oldest)) unlinkSync(oldest);
    for (let i = backups - 1; i >= 1; i -= 1) {
      const src = `${file}.${i}`;
      if (existsSync(src)) renameSync(src, `${file}.${i + 1}`);
    }
    renameSync(file, `${file}.1`);
    return true;
  } catch {
    /* 轮转失败不能影响主流程（下次写满了再试） */
    return false;
  }
}

/**
 * 尺寸检查 + 轮转（首次选定日志文件时兜一道，把上次会话遗留的超大文件收掉）。
 *
 * @returns 是否发生了轮转。
 */
export function rotateLogIfNeeded(file: string, options: LogRotationOptions = {}): boolean {
  const maxBytes = options.maxBytes ?? LOG_MAX_BYTES;
  try {
    if (!existsSync(file) || statSync(file).size < maxBytes) return false;
  } catch {
    return false;
  }
  return rotateLogNow(file, options);
}

/** 读一个日志文件当前的字节数（读不到算 0）。 */
export function logFileBytes(file: string): number {
  try {
    return statSync(file).size;
  } catch {
    return 0;
  }
}

/** 一行日志写进文件后会占多少字节（UTF-8 + 换行），供调用方记账。 */
export function logLineBytes(line: string): number {
  return Buffer.byteLength(line, 'utf8') + 1;
}
