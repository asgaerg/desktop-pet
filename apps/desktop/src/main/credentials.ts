/**
 * API 密钥的本地存储。
 *
 * 为什么单独一个文件、放在 `~/.desktop-pet/` 而不是 config/persona.yaml：
 *
 *   · **密钥不能进仓库。** persona.yaml 是项目文件，会被 git 带走；
 *     一旦有人把密钥填进去再提交，密钥就泄露了。放在用户目录里从根本上避免这一点。
 *   · **密钥不该出现在日志里。** 这个模块只提供"读全量"和"读打码版"两个入口，
 *     调用方拿不到"顺手打印"的机会 —— 打码是默认行为。
 *   · 权限收紧到当前用户（Windows 上等价于只允许自己读写）。
 *
 * 密钥是**明文**存储的（这是一个诚实的取舍）：Windows 上真正的加密要靠 DPAPI，
 * 而 DPAPI 只保护"别的用户读不到"，对同用户下的任何程序都没有防护 ——
 * 对本场景（本机、单用户、桌面宠物）收益有限，不值得引入平台相关代码。
 * 所以这里只做"不进 git、不进日志、权限收紧"，不做假的安全感。
 */

import { existsSync, mkdirSync, readFileSync, writeFileSync, chmodSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';

/** 用户数据目录：与记忆文件同处，都属于"用户的东西"而不是项目 */
export function userDataDir(): string {
  const custom = process.env.PET_USER_DIR;
  if (custom && custom.trim()) return custom.trim();
  return path.join(os.homedir(), '.desktop-pet');
}

/**
 * 实际生效的凭据文件路径。
 *
 * 正常情况下就是 `~/.desktop-pet/credentials.json`；只有那次写入失败、
 * 走了兜底目录之后才会不同 —— 界面显示它，用户才知道"我的密钥到底存哪了"。
 */
let resolvedCredentialsPath: string | null = null;
/** 兜底目录（由主进程注入，与设置文件的兜底目录同一个） */
let fallbackDir: string | null = null;

/**
 * 注入兜底目录。与 `settingsStore.setSettingsFallbackDir` 同一套路：
 * 用户目录不可写时（沙箱、受限账户、杀软拦截）退到项目旁的 `pet-data/`。
 */
export function setCredentialsFallbackDir(dir: string): void {
  fallbackDir = dir;
}

function credentialsPath(): string {
  if (resolvedCredentialsPath) return resolvedCredentialsPath;
  if (fallbackDir) {
    const alt = path.join(fallbackDir, 'credentials.json');
    if (existsSync(alt)) return alt;
  }
  return path.join(userDataDir(), 'credentials.json');
}

export interface Credentials {
  /** 按提供者名分组，例如 { deepseek: 'sk-...' } */
  [provider: string]: string;
}

export function readCredentials(): Credentials {
  const file = credentialsPath();
  if (!existsSync(file)) return {};
  try {
    const parsed: unknown = JSON.parse(readFileSync(file, 'utf-8'));
    if (!parsed || typeof parsed !== 'object') return {};
    const out: Credentials = {};
    for (const [key, value] of Object.entries(parsed as Record<string, unknown>)) {
      if (typeof value === 'string') out[key] = value;
    }
    return out;
  } catch {
    // 文件坏了不能让应用起不来；当作没有密钥处理，用户重新填一次即可
    return {};
  }
}

export function writeCredentials(credentials: Credentials): void {
  const payload = JSON.stringify(credentials, null, 2);
  /*
   * ## 写入失败要**说人话**，并且**试一次兜底路径**（2026-10-05）
   *
   * 实测踩到：设置页点保存报
   * `EPERM: operation not permitted, open 'C:\Users\tianyi\.desktop-pet\credentials.json'`。
   * 用户看到这个完全不知道怎么办。
   *
   * ## ⚠️ 而 `accessSync(W_OK)` **不能**用来预判
   *
   * 实测（同一个受限进程）：三个目录 `W_OK` 全部"通过"，但真写全是 EPERM。
   * 所以只能**真写、捕获、再兜底**，不能提前"检查一下"。
   *
   * 兜底顺序：用户目录 -> 项目旁的 `pet-data/`（与设置文件同处）。
   * 两个都不行才抛错，且错误里带上"已试过哪些路径"。
   */
  const primary = userDataDir();
  const tried: string[] = [];

  for (const dir of [primary, fallbackDir]) {
    if (!dir) continue;
    const file = path.join(dir, 'credentials.json');
    tried.push(file);
    try {
      mkdirSync(dir, { recursive: true });
      writeFileSync(file, payload, 'utf-8');
      try {
        // 仅当前用户可读写（Windows 上尽力而为，失败不影响功能）
        chmodSync(file, 0o600);
      } catch {
        /* 设不上也没关系 */
      }
      resolvedCredentialsPath = file;
      return;
    } catch {
      /* 换下一个候选 */
    }
  }

  throw new Error(
    `凭据写入失败，已尝试：${tried.join(' / ')}。` +
      `请检查这些目录是否可写（或被杀毒软件拦截）。`,
  );
}

/**
 * 打码。**任何要展示或记录密钥的地方都必须走这里。**
 *
 * 保留前后各 4 位，中间用固定长度的点替换 —— 保留长度信息本身就是一种泄露，
 * 所以不用"和原串等长的点"。
 */
export function maskKey(key: string): string {
  const trimmed = key.trim();
  if (!trimmed) return '';
  if (trimmed.length <= 12) return '••••';
  return `${trimmed.slice(0, 4)}••••${trimmed.slice(-4)}`;
}

/** 供界面显示用：只给出"有没有配"，不给密钥本身 */
export function describeCredentials(): { provider: string; masked: string }[] {
  return Object.entries(readCredentials()).map(([provider, key]) => ({
    provider,
    masked: maskKey(key),
  }));
}

export { credentialsPath };

/**
 * **当前实际存在的**凭据文件路径（没有就返回正常落点）。
 *
 * 用途：主进程启动时把它写进环境变量 `PET_CREDENTIALS_FILE`，让 Python 侧读**同一个文件**
 * —— 而不是各自猜路径（用户目录可能不可写，那时文件在项目旁的兜底目录里）。
 *
 * 顺序：显式解析过的 -> 兜底目录里已存在的 -> 正常落点。
 */
export function activeCredentialsPath(): string {
  if (resolvedCredentialsPath) return resolvedCredentialsPath;
  if (fallbackDir) {
    const alt = path.join(fallbackDir, 'credentials.json');
    if (existsSync(alt)) return alt;
  }
  return path.join(userDataDir(), 'credentials.json');
}
