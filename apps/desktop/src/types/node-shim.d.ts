/**
 * 最小 Node 环境类型声明。
 *
 * 为什么需要它、而不是装 `@types/node`：
 *   本项目的依赖是用 pnpm 11 安装的，而系统里能用的 pnpm 是 10.6.1 ——
 *   两者用的仓库（store）布局不同（v11 vs v10），一执行 `pnpm add` 就会报
 *   `ERR_PNPM_UNEXPECTED_STORE`，正确做法是重装整个 workspace。
 *   为了加一个类型包而重装全部依赖，风险远大于收益。
 *
 * 那为什么还要做类型检查：**它抓的是我们自己代码的错误**（调了不存在的函数、
 * 联合类型里漏了一种消息、协议字段不同步）。这些用 esbuild 完全发现不了，
 * 而它们已经真实发生过好几次。所以这里只把外部 API 标成宽松类型，
 * 让检查专注于自家代码。
 *
 * 代价要说清楚：这些 `any` 会让"node API 用错参数"这类问题漏过去。
 * 想彻底解决，正确做法是用与安装时一致的 pnpm 版本重装依赖并装上
 * `@types/node`，然后删掉本文件。
 */

declare const process: {
  argv: string[];
  env: Record<string, string | undefined>;
  versions: Record<string, string>;
  platform: string;
  pid: number;
  exit(code?: number): never;
  on(event: string, listener: (...args: any[]) => void): void;
  once(event: string, listener: (...args: any[]) => void): void;
  cwd(): string;
  nextTick(callback: (...args: any[]) => void): void;
};

declare const __dirname: string;
declare const __filename: string;

declare const Buffer: {
  alloc(size: number, fill?: number): any;
  allocUnsafe(size: number): any;
  from(data: any, encoding?: string): any;
  concat(list: any[], totalLength?: number): any;
  isBuffer(value: unknown): boolean;
  byteLength(value: string, encoding?: string): number;
};

/** Buffer 实例：只声明本项目实际用到的方法。 */
interface Buffer {
  readonly length: number;
  toString(encoding?: string, start?: number, end?: number): string;
  readUInt16BE(offset?: number): number;
  readBigUInt64BE(offset?: number): bigint;
  writeInt16LE(value: number, offset?: number): number;
  subarray(start?: number, end?: number): Buffer;
  slice(start?: number, end?: number): Buffer;
  [index: number]: number;
}

declare const console: {
  log(...args: any[]): void;
  warn(...args: any[]): void;
  error(...args: any[]): void;
  info(...args: any[]): void;
  debug(...args: any[]): void;
};

declare function setTimeout(handler: (...args: any[]) => void, timeout?: number): any;
declare function clearTimeout(handle: any): void;
declare function setInterval(handler: (...args: any[]) => void, timeout?: number): any;
declare function clearInterval(handle: any): void;
declare function setImmediate(handler: (...args: any[]) => void): any;

declare namespace NodeJS {
  type Timeout = any;

  /**
   * 事件发射器。
   *
   * ⚠️ 这里**不能**简单写成 `type EventEmitter = any`。
   * `PetSocket` 与 `SidecarManager` 都是 `extends EventEmitter` 的，
   * 基类写成 any 之后，TS 就看不到 `on` / `emit` 这些方法了 ——
   * 于是 `this.emit(...)`、`socket.on(...)` 全变成"属性不存在"，
   * 一次报出三十多个假错误，把真正的问题淹掉。（踩过。）
   *
   * 所以给出结构化的最小声明：只要方法存在、签名够宽松即可。
   */
  class EventEmitter {
    on(event: string | symbol, listener: (...args: any[]) => void): this;
    once(event: string | symbol, listener: (...args: any[]) => void): this;
    off(event: string | symbol, listener: (...args: any[]) => void): this;
    addListener(event: string | symbol, listener: (...args: any[]) => void): this;
    removeListener(event: string | symbol, listener: (...args: any[]) => void): this;
    removeAllListeners(event?: string | symbol): this;
    emit(event: string | symbol, ...args: any[]): boolean;
    listenerCount(event: string | symbol): number;
  }

  interface ProcessEnv {
    [key: string]: string | undefined;
  }
}

// node: 内置模块一律宽松处理。它们的**用法**由运行期保证，类型检查在这里
// 帮不上什么忙，却会淹没真正有价值的错误。
declare module 'node:fs' {
  const value: any;
  export = value;
}
declare module 'node:fs/promises' {
  const value: any;
  export = value;
}
declare module 'node:path' {
  const value: any;
  export = value;
}
declare module 'node:os' {
  const value: any;
  export = value;
}
declare module 'node:net' {
  const value: any;
  export = value;
}
declare module 'node:crypto' {
  const value: any;
  export = value;
}
declare module 'node:events' {
  const value: any;
  export = value;
}
declare module 'node:stream' {
  const value: any;
  export = value;
}
declare module 'node:child_process' {
  const value: any;
  export = value;
}
declare module 'node:url' {
  const value: any;
  export = value;
}
declare module 'node:module' {
  const value: any;
  export = value;
}
