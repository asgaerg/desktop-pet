/**
 * 本地语音合成（Windows SAPI）—— **必须在主进程执行**。
 *
 * ## 🔴 为什么不能在 Python 侧做（2026-10-08 实测定位）
 *
 * 本地合成要起 PowerShell 调 `System.Speech`。实测了四种链路：
 *
 * ```
 *   shell  -> powershell            ✅ Voice.Name=[Microsoft Huihui Desktop]、WAV 60872 字节
 *   Node   -> powershell            ✅ 三种接法（execFileSync / spawn 管道 / spawn ignore）全部成功
 *   Python -> powershell            ❌ Voice.Name=[]（空）、Speak 失败
 *   Node -> Python -> powershell    ❌ 同上
 * ```
 *
 * 报错是 SAPI 自己的话：
 *
 *     No voice installed on the system or none available with the current security setting.
 *
 * 但 `GetInstalledVoices()` **能列出 2 个音色**（含中文 Huihui、Enabled=True）——
 * 也就是**枚举得到、用不了**。同一台机器、同一个脚本、同一个 64 位 PowerShell，
 * 唯一变量是"谁起的进程"。
 *
 * 已排除：编码（码点逐字符核对过）、音色名（码点级相同）、位数（都是 64 位）、
 * `-NonInteractive`、`-STA`、`-Command` vs `-File`、TEMP 指向、stdio 接法。
 *
 * **生产里 Electron 是父进程 —— 也就是 Node 那条路，天然可用。**
 * 所以合成这一步放在这里，Python 通过 `tts.synthesize` 请求它。
 *
 * ⚠️ 这个限制是 **DSH 沙箱环境特有的**，不是普通 Windows 的 Python 问题。
 * 但既然 Node 那条路在两种环境下都可用，走它就是**更稳**的选择。
 *
 * ## 输出协议
 *
 * 交付给 Python 的是**裸 PCM（16bit 单声道 16kHz）**，不是 WAV：
 *   · Python 侧要拿它算音量包络（做口型）、再原样转发给前端播放
 *   · 前端 `voice-io.js` 的 `format='pcm'` 分支**写死按 16kHz 解**
 *   · SAPI 出的是 **22050Hz**，所以这里必须重采样 —— 否则音调会升高约 1.38 倍
 *     （"花栗鼠"效果）
 *
 * ## 为什么用 `spawn` + `stdio: 'ignore'`，而不是 `execFile`
 *
 * `execFileSync` 在这个环境里会 EPERM（见 `pushToTalk.ts` 里那段长说明）。
 * 而 `spawn` + 忽略 stdio + 结果走**文件**是最稳的一条 —— 和按住说话监视器、
 * `pet.usage` 同一个模式。
 */

import { spawn } from 'node:child_process';
import { appendFileSync, existsSync, mkdirSync, readFileSync, rmSync, statSync } from 'node:fs';
import path from 'node:path';

/**
 * 排查用的诊断日志（**写到项目目录**，绕过 `log()`）。
 *
 * ## 为什么需要它
 *
 * 主进程的 `log()` 写到 `app.getPath('userData')/logs/pet.log`，
 * 而实际运行时那个目录**是空的** —— 写入静默失败了（`catch {}` 吞掉）。
 * 于是排查"本地语音为什么失败"时看不到任何东西，只能靠猜。
 *
 * 这个函数把关键信息写到 `pet-data/tts-diag.log`，
 * 不依赖 `log()`、不依赖 `userData`、不依赖 PowerShell 的输出流。
 */
function diag(workDir: string, message: string): void {
  try {
    const file = path.join(workDir, '..', 'tts-diag.log');
    const line = `${new Date().toISOString()} ${message}\n`;
    appendFileSync(file, line, 'utf-8');
  } catch {
    /* 诊断失败不能影响合成 */
  }
}

/** 交付给调用方的结果 */
export interface TtsSynthesis {
  ok: boolean;
  message?: string;
  /** 裸 PCM（16bit 单声道），仅 ok 时有值 */
  pcm?: Buffer;
  /** 固定 16000 */
  sampleRate?: number;
}

export interface TtsOptions {
  /** `win-tts.ps1` 的路径 */
  script: string;
  /** 音色名（空 = 系统默认） */
  voice?: string;
  /** 语速 -10..10 */
  rate?: number;
  /** 音量 0..100 */
  volume?: number;
  /** 临时目录（WAV 落这儿） */
  workDir: string;
  timeoutMs?: number;
  /** 记日志用 */
  log?: (line: string) => void;
}

/**
 * 剥 WAV 头拿裸 PCM。**必须解析 `data` 块，不能硬跳 44 字节**。
 *
 * ⚠️ 用 `DataView` 而不是 `buf.readUInt32LE()`：
 * 这个 TS 版本里 `Buffer` 的多字节读方法签名对不上（报 "Property
 * 'readUInt32LE' does not exist on type 'Buffer<ArrayBufferLike>'"），
 * 而 `DataView` 是标准的、还要求**显式声明字节序** —— 正好更安全
 * （WAV 是小端，写 `true` 一眼可见）。
 */
function parseWav(buf: Buffer): { pcm: Buffer; sampleRate: number } | { error: string } {
  if (buf.length < 44 || buf.toString('ascii', 0, 4) !== 'RIFF') {
    return { error: '不是 WAV（缺 RIFF 头）' };
  }
  if (buf.toString('ascii', 8, 12) !== 'WAVE') return { error: '不是 WAVE 格式' };

  const view = new DataView(buf.buffer, buf.byteOffset, buf.byteLength);

  /*
   * ⚠️ 为什么要遍历 chunk 而不是"跳过前 44 字节"：
   *
   * 44 字节是**规范 WAV 的最短头**，但 SAPI 写出的文件可能带额外的
   * `LIST` / `fact` 块。硬跳 44 会从中间开始解 —— 结果是**噪音**，
   * 而且**不会报错**。这种"能播但是噪音"最难查。
   */
  let offset = 12;
  let sampleRate = 0;
  let channels = 0;
  let bits = 0;
  while (offset + 8 <= buf.length) {
    const id = buf.toString('ascii', offset, offset + 4);
    const size = view.getUint32(offset + 4, true); // true = 小端
    const body = offset + 8;
    if (id === 'fmt ') {
      channels = view.getUint16(body + 2, true);
      sampleRate = view.getUint32(body + 4, true);
      bits = view.getUint16(body + 14, true);
    } else if (id === 'data') {
      const end = Math.min(body + size, buf.length);
      if (channels !== 1 || bits !== 16) {
        return { error: `WAV 格式不支持：${channels} 声道 / ${bits} bit（需要单声道 16bit）` };
      }
      return { pcm: buf.subarray(body, end), sampleRate };
    }
    offset = body + size + (size % 2); // chunk 按偶数字节对齐
  }
  return { error: 'WAV 里没有 data 块' };
}

/**
 * 用线性插值把 16bit 单声道 PCM 重采样到目标采样率。
 *
 * ## 为什么不用更简单的"每 N 个丢一个"
 *
 * 22050 -> 16000 的比值是 **0.7256**，不是整数。丢样本会引入明显的
 * 混叠噪声（听起来沙沙的）。线性插值虽然也不是高保真，但对语音够用，
 * 而且**十几行纯 JS、没有依赖**。
 *
 * （Python 侧有 `audioop.ratecv`，但那正是我们绕开的原因。）
 */
function resamplePcm16(pcm: Buffer, from: number, to: number): Buffer {
  if (from === to) return pcm;
  const inCount = Math.floor(pcm.length / 2);
  const outCount = Math.floor((inCount * to) / from);
  const out = Buffer.allocUnsafe(outCount * 2);
  // 同样用 DataView：`Buffer` 的 readInt16LE 在这个 TS 版本里签名对不上
  const src = new DataView(pcm.buffer, pcm.byteOffset, pcm.byteLength);
  const dst = new DataView(out.buffer, out.byteOffset, out.byteLength);
  for (let i = 0; i < outCount; i += 1) {
    const pos = (i * from) / to;
    const i0 = Math.floor(pos);
    const i1 = Math.min(i0 + 1, inCount - 1);
    const frac = pos - i0;
    // ⚠️ `getInt16` 而不是 `getUint16` —— PCM 是有符号的，
    // 按无符号读会把负半周当成 +65535，波形直接废掉（听起来是爆音）
    const s0 = src.getInt16(i0 * 2, true);
    const s1 = src.getInt16(i1 * 2, true);
    const v = Math.max(-32768, Math.min(32767, Math.round(s0 + (s1 - s0) * frac)));
    dst.setInt16(i * 2, v, true);
  }
  return out;
}

/** 临时文件序号：并发时各自一个文件，避免互相覆盖 */
let synthSeq = 0;

/**
 * 合成一次。**串行调用方自己保证**（`synthesize` 里用 Promise 链排了队）。
 */
export async function synthesizeSpeech(text: string, opts: TtsOptions): Promise<TtsSynthesis> {
  const log = opts.log ?? (() => {});
  if (!text.trim()) return { ok: false, message: '要合成的文字是空的' };
  if (!existsSync(opts.script)) {
    return { ok: false, message: `找不到 SAPI 脚本：${opts.script}` };
  }

  try {
    mkdirSync(opts.workDir, { recursive: true });
  } catch (error) {
    return { ok: false, message: `建临时目录失败：${(error as Error).message}` };
  }

  const out = path.join(opts.workDir, `sapi-${process.pid}-${(synthSeq += 1)}.wav`);
  try {
    rmSync(out, { force: true });
  } catch {
    /* 删不掉也无所谓 —— 脚本自己会覆盖 */
  }

  const timeoutMs = opts.timeoutMs ?? 20000;

  /*
   * ⚠️ **重定向交给脚本自己**（`-LogFile`），不要在 Node 侧拼 shell 重定向。
   *
   * ## 为什么（2026-10-08，试了三种都不行）
   *
   * | 写法 | shell 里手测 | **Electron 里** |
   * |---|---|---|
   * | `spawn('cmd', ['/c', '... > file'])` | ✅ WAV 67480 | ❌ `exit=4294770688`（= -65536，**spawn 自身失败**：找不到 `cmd`） |
   * | 同上但 `cmd /c` 拼成整串 | ✅ | ❌ 输出文件根本没生成 |
   * | `spawn('powershell', ['-Command', '"... *> file"'])` | ✅ WAV 67480 | ❌ `exit=1`，日志文件为空 |
   *
   * 最后一种尤其能说明问题：**同一个命令字符串**，在 shell 里跑成功、
   * 经 Node 的 argv 传进 Windows 就被二次加引号拼坏了（`spawn` 会把整个
   * `-Command` 参数再包一层，PS 收到的就变了）。
   *
   * ⚠️ 顺带：`pushToTalk.ts` 里那处 `spawn('cmd', ...)` 也是同类风险 ——
   * 它现在能跑，但那次 `exit=1` 我当时归咎于引号，很可能就是"找不到 cmd"。
   *
   * 所以改成：**让 `win-tts.ps1` 自己写日志**，Node 只读那个文件。
   * PowerShell 的原始错误信息一定拿得到，完全不依赖 shell 级重定向。
   */
  const errFile = path.join(opts.workDir, `sapi-err-${process.pid}-${synthSeq}.txt`);
  try {
    rmSync(errFile, { force: true });
  } catch {
    /* ignore */
  }

  let lastArgs = '';
  const t0 = Date.now();
  const code = await new Promise<number>((resolve) => {
    // `-File` + **参数逐个传**（不拼命令行）—— 这是唯一在两边都稳的形式
    const psArgs = [
      '-NoProfile',
      '-NonInteractive',
      '-ExecutionPolicy',
      'Bypass',
      '-File',
      opts.script,
      '-Text',
      text,
      '-OutFile',
      out,
      '-Rate',
      String(opts.rate ?? 0),
      '-Volume',
      String(opts.volume ?? 100),
      '-LogFile',
      errFile,
    ];
    if (opts.voice) psArgs.push('-Voice', opts.voice);

    /*
     * ⚠️ 把**真实 argv** 记下来（2026-10-08）。
     *
     * 排查时我反复在猜“主进程传了什么参数”，而一条照抄这里拼法的独立探针
     * （`probe-sapi-mainlike.mjs`，历史实测脚本）3/3 成功、应用里却报 `exit=3`
     * （SAPI 没有可用音色）—— 两者矛盾，说明**实际参数和我以为的不一样**。
     * 那就别猜：失败时把它整条打进日志。
     */
    lastArgs = psArgs.join(' ');

    /*
     * ⚠️ 把**真实 argv** 记进日志（2026-10-08）。
     *
     * 排查时我反复在"我猜主进程传了什么参数"上打转，而实测一条独立探针
     * （`probe-sapi-mainlike.mjs`（历史实测脚本），照抄这里的拼法）**3/3 成功**，
     * 应用里却报 `exit=3`（SAPI 没有可用音色）。两者矛盾说明
     * **实际参数和我以为的不一样** —— 那就别猜，打出来。
     *
     * 只在失败时打（成功时每句话都打会把日志刷满）。
     */
    lastArgs = psArgs.join(' ');

    // ⚠️ `stdio: 'ignore'` + 不加 `windowsHide: true`
    // （`windowsHide` 会加 CREATE_NO_WINDOW，而 pushToTalk 那段实测过
    //  它是可疑变量 —— 这里不读键盘，但保持最小惊讶）
    const child = spawn('powershell', psArgs, { stdio: 'ignore' });
    const timer = setTimeout(() => {
      try {
        child.kill();
      } catch {
        /* 杀不掉就算了 */
      }
    }, timeoutMs);
    child.on('close', (c) => {
      clearTimeout(timer);
      log('[tts] spawn 结束 耗时=' + (Date.now() - t0) + 'ms exit=' + c);
      resolve(c ?? -1);
    });
    child.on('error', (e) => {
      clearTimeout(timer);
      // spawn 自身失败（ENOENT 等）—— 不记的话只会看到一个负数退出码
      log(`spawn powershell 失败：${e.message}`);
      resolve(-65536);
    });
  });

  // 读回 PowerShell 的输出（成功时通常是空的）
  let scriptLog = '';
  try {
    if (existsSync(errFile)) scriptLog = readFileSync(errFile, 'utf8').trim();
  } catch {
    /* 读不到就算了 */
  } finally {
    try {
      rmSync(errFile, { force: true });
    } catch {
      /* ignore */
    }
  }

  if (code !== 0) {
    try {
      rmSync(out, { force: true });
    } catch {
      /* ignore */
    }
    const why = scriptLog ? `：${scriptLog.split('\n')[0].slice(0, 160)}` : '（PowerShell 没有输出，说明它根本没跑起来）';
    log(`SAPI 合成失败 exit=${code}${why}`);
    log(`SAPI argv: ${lastArgs}`);
    diag(
      opts.workDir,
      `FAIL exit=${code} argv=[${lastArgs}] scriptLog=${JSON.stringify(scriptLog.slice(0, 400))}`,
    );
    return {
      ok: false,
      message:
        'SAPI 合成失败（exit=' + code + ' 耗时' + (Date.now() - t0) + 'ms）' + why,
    };
  }

  let raw: Buffer;
  try {
    raw = readFileSync(out);
  } catch (error) {
    return { ok: false, message: `读合成结果失败：${(error as Error).message}` };
  } finally {
    try {
      rmSync(out, { force: true });
    } catch {
      /* ignore */
    }
  }

  diag(opts.workDir, `OK exit=${code} argv=[${lastArgs}] wav=${raw.length}B`);
  const parsed = parseWav(raw);
  if ('error' in parsed) return { ok: false, message: parsed.error };

  const pcm = resamplePcm16(parsed.pcm, parsed.sampleRate, 16000);
  log(
    `语音合成：${text.length} 字 -> ${parsed.sampleRate}Hz ${parsed.pcm.length} 字节` +
      ` -> 16kHz ${pcm.length} 字节`,
  );
  return { ok: true, pcm, sampleRate: 16000 };
}

/** 文件大小便于日志（失败时也可能想看它） */
export function fileSizeOrZero(p: string): number {
  try {
    return existsSync(p) ? statSync(p).size : 0;
  } catch {
    return 0;
  }
}
