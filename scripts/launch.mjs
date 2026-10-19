/**
 * 启动器：`node scripts/launch.mjs [--dev] [--build]`
 *
 * 做三件事，每一件都对应一类真实踩过的坑：
 *
 * ① **清掉 `ELECTRON_RUN_AS_NODE`**（宿主环境常会注入）。
 *    它会让 Electron **完全不初始化应用层**：`require('electron')` 退化成 npm 包
 *    导出的路径字符串，app/window/tray 全是 undefined，而
 *    `process.versions.electron` 依然显示正常版本号 —— 报错堆栈全在 Node 内部，
 *    跟真正的原因毫无关系。两个必须注意的点：
 *      · 必须用 `delete env.XXX`，**不能**用 `env.XXX = ''`。空串会被 Chromium
 *        的取值逻辑当作"已设置"，Electron 仍然退化（实测验证过）。
 *      · 必须在启动 Electron **之前**清掉 —— 它在加载任何 JS 之前就读取这个变量。
 *
 * ② **前置检查 Node 版本**。PATH 里的 node 可能是 v14，而 Electron 32 与构建
 *    工具都要求 18+。不检查的话，用户看到的是一堆语法错误（`??`、`?.` 失败），
 *    完全看不出真正原因。
 *
 * ③ **把托管环境的 Python 路径转交给 sidecar**，这样 `sidecar.ts` 里就不必硬编码
 *    任何机器专属路径。
 */

import { spawn, spawnSync } from 'node:child_process';
import { existsSync, readdirSync, readFileSync, rmSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const projectRoot = path.resolve(here, '..');
const desktopDir = path.join(projectRoot, 'apps', 'desktop');
const isWindows = process.platform === 'win32';

/** 本次启动是否要先构建（`pnpm dev` 会带上 --build） */
const needsBuild = process.argv.includes('--build');

/* ------------------------------------------------------------------ 找可用的 Node */

/**
 * 找版本足够新的 Node，并把它的目录**插到 PATH 最前面**。
 *
 * 为什么需要这个：启动器是被 `pnpm dev` 用 PATH 里的 node 拉起来的，而
 * `node_modules/.bin` 里的 esbuild 是 `#!/usr/bin/env node` 脚本 —— 它会**继承
 * 同一个 PATH**。所以只换启动器自己的解释器是不够的，必须把新版 Node 的目录
 * 顶到 PATH 最前，构建工具才会跟着用新版。
 *
 * 查找顺序（越靠前越优先）：
 *   ① 环境变量 `PET_NODE`          —— 临时覆盖
 *   ② 项目根的 `.pet-node` 文件     —— 一次配置，长期生效（写一行路径即可）
 *   ③ nvm 已装的其他版本            —— 自动挑一个够新的，**不需要管理员切链接**
 *   ④ 官方安装目录
 *
 * 第 ③ 条是关键：nvm-windows 的 `nvm use` 要改 D 盘根目录的符号链接，
 * 因此需要管理员权限。但 nvm 已经装好的各版本目录本身就是可执行的，
 * 直接把那个目录塞进 PATH 就够了 —— 不用动任何系统级设置。
 */
function findNewerNode() {
  /** @type {{executable: string, version: string, dir: string, major: number, confirmed: boolean}[]} */
  const candidates = [];

  const add = (executable, versionHint) => {
    if (!executable) return;
    const hint = String(versionHint ?? '').trim();
    const match = /v?(\d+)\.(\d+)\.(\d+)/.exec(hint);
    candidates.push({
      executable,
      version: match ? `v${match[1]}.${match[2]}.${match[3]}` : '(未知)',
      dir: path.dirname(executable),
      major: match ? Number(match[1]) : -1,
      confirmed: Boolean(match),
    });
  };

  if (process.env.PET_NODE) add(process.env.PET_NODE, process.env.PET_NODE);

  const marker = path.join(projectRoot, '.pet-node');
  if (existsSync(marker)) {
    for (const line of readFileSync(marker, 'utf-8').split(/\r?\n/)) {
      const trimmed = line.trim();
      if (trimmed && !trimmed.startsWith('#')) add(trimmed, trimmed);
    }
  }

  if (isWindows) {
    // nvm-windows：各版本目录名本身就是版本号，**不需要起子进程去问**。
    // 这一点很重要：某些受限环境禁止 spawn，靠 `node --version` 探测会全部失败。
    for (const nvmRoot of [
      process.env.NVM_HOME,
      'D:\\nvm',
      path.join(process.env.APPDATA ?? '', 'nvm'),
      path.join(process.env.USERPROFILE ?? '', 'AppData', 'Roaming', 'nvm'),
    ]) {
      if (!nvmRoot || !existsSync(nvmRoot)) continue;
      try {
        for (const entry of readdirSync(nvmRoot)) {
          if (!/^v\d+\.\d+\.\d+$/.test(entry)) continue;
          add(path.join(nvmRoot, entry, 'node.exe'), entry);
        }
      } catch {
        /* 读不到就继续找别的 */
      }
    }
    add('C:\\Program Files\\nodejs\\node.exe', '');
  } else {
    add('/usr/local/bin/node', '');
    add('/usr/bin/node', '');
  }

  // 托管运行时兜底（目录名里通常带版本号）
  if (process.env.DSH_NODE) add(process.env.DSH_NODE, process.env.DSH_NODE);

  const MIN_MAJOR = 18;
  candidates.sort((a, b) => b.major - a.major);

  for (const candidate of candidates) {
    if (!existsSync(candidate.executable)) continue;
    if (candidate.major >= 0 && candidate.major < MIN_MAJOR) continue;
    // 目录名给了版本号就直接采信；没有才去问（受限环境下问不出来也能用）
    if (candidate.major < 0) {
      let version = '';
      try {
        version = String(
          spawnSync(candidate.executable, ['--version'], { encoding: 'utf-8' }).stdout ?? '',
        ).trim();
      } catch {
        continue;
      }
      const major = Number(version.replace(/^v/, '').split('.')[0]);
      if (!Number.isFinite(major) || major < MIN_MAJOR) continue;
      candidate.version = version;
      candidate.major = major;
      candidate.confirmed = true;
    }
    return candidate;
  }
  return null;
}

// 当前解释器够新吗？不够就尝试换一个，并把它的目录顶到 PATH 最前。
const CURRENT_MAJOR = Number(process.versions.node.split('.')[0]);
const MIN_NODE_MAJOR = 18;
let effectiveEnv = { ...process.env };
let nodeSwitched = null;

if (CURRENT_MAJOR < MIN_NODE_MAJOR) {
  const newer = findNewerNode();
  if (newer) {
    nodeSwitched = newer;
    // 顺序很重要：用 `;` 分隔，新版在最前。这样 node / npm / npx 都会用新版。
    effectiveEnv.PATH = `${newer.dir}${path.delimiter}${effectiveEnv.PATH ?? ''}`;
    if (isWindows) effectiveEnv.Path = effectiveEnv.PATH; // Windows 上大小写两种写法都可能被读
    console.log(
      `[launch] 当前 Node ${process.versions.node} 偏旧，已改用 ${newer.version}（${newer.executable}）`,
    );
    console.log('[launch] 只为本次启动生效，没有改动你的系统设置');
  } else {
    console.error(
      [
        `需要 Node ${MIN_NODE_MAJOR}+，当前是 ${process.versions.node}，也没找到可用的新版本。`,
        '',
        '三条可选路径（任选其一）：',
        '  1. 用管理员 PowerShell 执行  nvm use 18.12.0   （切换需要管理员权限）',
        '  2. 在本文件旁边新建 .pet-node，写一行新版 node.exe 的完整路径',
        '  3. 设环境变量：$env:PET_NODE = "D:\\nvm\\v18.12.0\\node.exe"',
        '',
        '说明：PATH 里的 node 太旧时，报错会是一堆看不懂的语法错误，所以在这里提前拦下。',
      ].join('\n'),
    );
    process.exit(1);
  }
}

/* ------------------------------------------------------------------ 前置检查 */

const electronBinary = path.join(
  desktopDir,
  'node_modules',
  'electron',
  'dist',
  isWindows ? 'electron.exe' : 'Electron',
);

if (!existsSync(electronBinary)) {
  console.error(
    [
      `找不到 Electron 运行时：${electronBinary}`,
      '',
      '请先安装依赖：pnpm install',
      '（若卡在从 GitHub 下载二进制，先设镜像再装，实测能快两个数量级：',
      '  $env:ELECTRON_MIRROR = "https://registry.npmmirror.com/-/binary/electron/"）',
    ].join('\n'),
  );
  process.exit(1);
}

/**
 * 说明：这里**不检查 pnpm**。
 *
 * 早先的版本让 `dev` 通过 `pnpm run build` 来构建，于是整个启动过程被 pnpm
 * 的 Node 版本要求绑住 —— pnpm 10.x 要求 Node >= 18.12，而项目依赖可能正绑在
 * 旧版 Node 上，结果 `pnpm dev` 在启动器跑起来之前就退出了，报的还是
 * "This version of pnpm requires at least Node.js v18.12"，跟项目毫无关系。
 *
 * 现在构建直接调 esbuild 二进制（它不依赖 Node，也不依赖 pnpm），
 * 启动路径就只剩下"Node 18+ 与一份装好的 node_modules"。
 */

/* ------------------------------------------------------------------ 环境准备 */

const env = effectiveEnv;
if ('ELECTRON_RUN_AS_NODE' in env) {
  delete env.ELECTRON_RUN_AS_NODE;
  console.log('[launch] 已清除宿主注入的 ELECTRON_RUN_AS_NODE（否则 Electron 会退化成纯 Node）');
}

// 把托管环境的 Python 路径转交给 sidecar 的查找逻辑（见 sidecar.ts 的 findPython）
if (!env.PET_PYTHON) {
  const found = [env.DSH_PYTHON, env.PYTHON].find(
    (value) => typeof value === 'string' && value.trim(),
  );
  if (found) {
    env.PET_PYTHON = found.trim();
    console.log(`[launch] 使用解释器：${env.PET_PYTHON}`);
  }
}

/* ------------------------------------------------------------------ 可选构建 */

/**
 * 找 esbuild 可执行文件。
 *
 * esbuild 是**独立二进制**，不需要 Node、也不需要 npm —— 这一点很有用：
 * 它让 `pnpm dev` 的构建步骤不必依赖 pnpm（pnpm 自身有 Node 版本要求，
 * 而项目依赖可能正绑在旧版 Node 上，于是 `pnpm run build` 根本跑不起来）。
 */
function findEsbuild() {
  const names = isWindows ? ['esbuild.exe'] : ['esbuild'];
  /**
   * 本机对应的 esbuild 平台目录名（`@esbuild/<这个>`）。
   *
   * ## 🔴 为什么必须有这一层过滤（2026-10-12 修，用户报「一启动就报错」）
   *
   * 原来这里是**直接返回第一个存在的二进制**，靠 `readdirSync` 的顺序 ——
   * 而它按**字母序**，于是 `@esbuild+aix-ppc64`（AIX / PowerPC！）永远排在
   * `win32-x64` 前面，被当成可用的那个。
   *
   * 后果（用户实测的完整链条）：
   *
   *     桌面快捷方式 -> 启动宠物.vbs -> dev.cmd -> launch.mjs --build
   *       -> needsBuild=true -> findEsbuild() 拿到 aix-ppc64 的二进制
   *       -> spawnSync 跑不起来 -> status != 0
   *       -> "[launch] 构建失败：src/main/index.ts" -> exit 1
   *       -> 用户看到 "Launch failed (exit code 1)."
   *
   * ⚠️ 而**报错信息完全指向错的方向**：它说"构建失败：src/main/index.ts"，
   * 看起来像 TypeScript 有语法错，实际上是**二进制选错了**。
   * 这类"错误信息把人引向错误位置"的故障最费时间，所以这里同时把
   * 候选列表打进日志（见下面的 console.log）。
   *
   * ## 平台名怎么拼
   *
   * esbuild 的命名规则是 `<platform>-<arch>`，两边各有几处不按 Node 的写法：
   *   · `win32` + `x64`  -> `win32-x64`
   *   · `win32` + `arm64`-> `win32-arm64`
   *   · `linux` + `x64`  -> `linux-x64`
   *   · `darwin`+ `arm64`-> `darwin-arm64`
   * 其它组合（freebsd / openbsd / netbsd / sunos / aix）esbuild 也有，
   * 但整条链路上只有 Windows/macOS/Linux 三类需要照顾 ——
   * 实在没有匹配的就退回"不过滤"，让后面的报错自己浮出来。
   */
  const platformDirName = `${process.platform}-${process.arch}`;
  const roots = [
    path.join(desktopDir, 'node_modules'),
    path.join(projectRoot, 'node_modules'),
  ];
  for (const root of roots) {
    for (const name of names) {
      const direct = path.join(root, name);
      if (existsSync(direct)) return direct;
      // pnpm 的存储布局：node_modules/.pnpm/@esbuild+<平台>@<版本>/node_modules/@esbuild/<平台>/esbuild
      const pnpmDir = path.join(root, '.pnpm');
      if (existsSync(pnpmDir)) {
        try {
          const found = [];
          for (const entry of readdirSync(pnpmDir)) {
            if (!entry.startsWith('@esbuild+')) continue;
            const candidate = path.join(pnpmDir, entry, 'node_modules', '@esbuild');
            if (!existsSync(candidate)) continue;
            for (const platformDir of readdirSync(candidate)) {
              const exe = path.join(candidate, platformDir, name);
              if (existsSync(exe)) found.push({ platformDir, exe });
            }
          }
          // ① 先找**本机平台**的那个
          const exact = found.find((f) => f.platformDir === platformDirName);
          if (exact) return exact.exe;
          /*
           * ② 没有本机平台的 -> 退回原来的行为，但**把候选打出来**。
           *
           * 不直接报错退出：可能是非 pnpm 布局、或平台名对不上，
           * 那种情况下"试一下"比"直接拒绝启动"更有用。
           * 但必须留下日志 —— 否则又会变成一次"错误信息指向别处"的排查。
           */
          if (found.length > 0) {
            console.warn(
              `[launch] ⚠️ 没找到本机平台的 esbuild（要的是 ${platformDirName}），` +
                `可用的只有：${found.map((f) => f.platformDir).join(', ')}`,
            );
            console.warn('[launch] ⚠️ 将退用第一个，构建很可能失败 —— 试试重装依赖：pnpm install');
            return found[0].exe;
          }
        } catch {
          /* 读不到就继续找下一个位置 */
        }
      }
    }
  }
  return null;
}

if (needsBuild) {
  // 为什么把构建绑进启动：渲染层的 `sprite.ts` 要先编译成 `sprite.js` 才能被
  // 浏览器加载。忘了构建的症状是"立绘完全不显示"，而且控制台里只有一行
  // 模块加载失败 —— 曾经因为渲染层里混进 TypeScript 语法，立绘在整个阶段里
  // 其实从未加载过。让 `dev` 顺手构建一次，这类问题就不存在了。
  const esbuild = findEsbuild();
  if (esbuild === null) {
    console.warn('[launch] 找不到 esbuild，跳过构建（界面可能因缺少产物而空白）');
    console.warn('[launch] 装依赖即可解决：pnpm install');
  } else {
    console.log(`[launch] 构建前端产物（esbuild ${path.relative(projectRoot, esbuild)}）…`);
    const bundle = (entry, outfile, platform, extra = []) => {
      const args = [
        path.join(desktopDir, entry),
        '--bundle',
        `--platform=${platform}`,
        '--target=node20',
        '--format=cjs',
        '--external:electron',
        ...extra,
        `--outfile=${path.join(desktopDir, outfile)}`,
      ];
      // stdio: 'inherit' 是有意为之：受限环境禁止带管道的子进程，继承则没问题
      const result = spawnSync(esbuild, args, { stdio: 'inherit', env: effectiveEnv });
      if (result.status !== 0) {
        console.error(`[launch] 构建失败：${entry}`);
        process.exit(result.status ?? 1);
      }
    };

    bundle('src/main/index.ts', 'dist/main.cjs', 'node', ['--sourcemap']);
    bundle('src/preload/index.ts', 'dist/preload.cjs', 'node', ['--sourcemap']);
    // 渲染层是浏览器 ESM，参数与上面不同，单独调一次
    const rendererBuild = spawnSync(
      esbuild,
      [
        path.join(desktopDir, 'src', 'renderer', 'sprite.ts'),
        '--bundle',
        '--platform=browser',
        '--target=chrome120',
        '--format=esm',
        `--outfile=${path.join(desktopDir, 'src', 'renderer', 'sprite.js')}`,
      ],
      { stdio: 'inherit', env: effectiveEnv },
    );
    if (rendererBuild.status !== 0) {
      console.error('[launch] 构建失败：src/renderer/sprite.ts');
      process.exit(rendererBuild.status ?? 1);
    }
    console.log('[launch] 构建完成');
  }
}

/* ------------------------------------------------------------------ 启动 */

const baseArgs = ['.'];
if (process.argv.includes('--dev')) baseArgs.push('--dev');
if (process.argv.includes('--no-sandbox')) baseArgs.push('--no-sandbox');
// 需要看渲染层日志时才开调试器：`dev.cmd --devtools`
// 默认不开 —— 它会多出一个独立窗口，而"桌面上多了一个浏览器窗口"很容易被
// 当成程序出错。日常使用不需要它。
if (process.argv.includes('--devtools')) process.env.PET_DEVTOOLS = '1';
// 启动后直接打开设置窗口，便于调试：`dev.cmd --settings`
if (process.argv.includes('--settings')) process.env.PET_OPEN_SETTINGS = '1';

/**
 * 启动标记：主进程在**窗口真正画出来之后**会写这个文件并注册退出钩子。
 *
 * 为什么用文件而不是 exit code：正常关窗和崩溃的退出码都可能相同，
 * 靠退出码猜不准。而"有没有成功画出来过"是确定的 —— 主进程自己在
 * `ready-to-show` 之后写标记，并在退出时删掉它。所以：
 *   · 进程结束后标记还在 → 它成功显示过 → 退出是用户行为
 *   · 标记不在            → 它压根没起来过 → 是启动失败，值得重试
 */
/**
 * 失败标记：主进程在**启动最开始**写下它，并在窗口真正显示出内容之后删掉。
 *
 * 于是这个文件存在的唯一含义是："上一次启动没能显示出窗口"。
 * 用文件而不是退出码，是因为正常关窗和崩溃的退出码可能相同，靠它猜不准；
 * 而"失败标记还在不在"是确定的事实。
 *
 * ⚠️ 这里曾经把判断写反过（用一个"成功标记"去启用降级），结果是**死锁**：
 * 只有成功的启动才会写成功标记，而失败的启动永远等不到它，降级路径因此永远
 * 不触发 —— 恰恰在最需要它的时候失效。失败标记的写法没有这个问题，
 * 因为它是**启动时就写、成功后才删**。
 */
const launchMarker = path.join(
  process.env.APPDATA ?? process.env.HOME ?? projectRoot,
  '@pet',
  'desktop',
  '.launch-failed',
);

function previousLaunchFailed() {
  if (!existsSync(launchMarker)) return false;
  try {
    rmSync(launchMarker, { force: true });
  } catch {
    /* 删不掉也继续，顶多多一次无谓的重试 */
  }
  return true;
}

/**
 * 有些 Windows 环境（容器、受限令牌、部分安全软件）不允许 Chromium 起子进程，
 * 表现是 GPU 进程反复启动失败，然后
 * `FATAL: gpu_data_manager_impl_private.cc GPU process isn't usable. Goodbye.`
 * —— **整个应用直接退出**，窗口一次都没出现过。
 *
 * 这条路堵得很死：`app.disableHardwareAcceleration()`、`--disable-gpu`、
 * `--disable-gpu-compositing` 全都拦不住，因为问题不在 GPU，而在子进程创建
 * 本身被拒。`--no-sandbox` 才是对症的开关。
 *
 * 代价：它会关闭渲染进程沙箱。对一个本机运行、只连 `ws://127.0.0.1`、不上网
 * 的宠物来说可以接受；但确实降低了隔离强度，所以**只在检测到上次启动失败后
 * 才启用**，并且会明确告诉你。想禁用这条退路：`PET_NO_SANDBOX_FALLBACK=0`。
 */
const allowFallback = process.env.PET_NO_SANDBOX_FALLBACK !== '0';
const useFallback = allowFallback && previousLaunchFailed();

if (useFallback) {
  console.log('');
  console.log('[launch] 检测到上一次启动没能显示出窗口，这次加上 --no-sandbox 重试。');
  console.log('[launch] 原因通常是本机不允许 Chromium 起子进程（关 GPU 的三个开关都拦不住）。');
  console.log('[launch] 想关掉这个自动退路：设 PET_NO_SANDBOX_FALLBACK=0');
  console.log('');
}

const args = baseArgs.includes('--no-sandbox') || useFallback
  ? [...new Set([...baseArgs, '--no-sandbox'])]
  : baseArgs;

console.log(`[launch] ${electronBinary} ${args.join(' ')}`);

const child = spawn(electronBinary, args, { cwd: desktopDir, env, stdio: 'inherit' });
child.on('error', (error) => {
  console.error(`[launch] 无法启动 Electron：${error.message}`);
  process.exit(1);
});
child.on('exit', (code, signal) => {
  if (signal) console.log(`[launch] Electron 被信号 ${signal} 结束`);

  // 启动失败时把主进程留下的启动记录读出来。
  //
  // 为什么需要这个：Electron 有很多种"静默退出"——拿不到单实例锁、用户数据目录
  // 建不出来、GPU 进程反复失败……它们的共同点是**退出码是 0 且不留任何输出**，
  // 用户看到的只是"双击了没反应"。主进程会在 dist/startup.log 里写下每一步，
  // 这里在失败时把它展示出来，把"没反应"变成"知道卡在哪"。
  const startupLog = path.join(desktopDir, 'dist', 'startup.log');
  const displayed = existsSync(launchMarker);
  let notes = '';
  try {
    if (existsSync(startupLog)) notes = readFileSync(startupLog, 'utf-8').trim();
  } catch {
    /* 读不到就算了 */
  }
  const succeeded = notes.includes('窗口已显示');

  if (!succeeded) {
    console.log('');
    console.log('[launch] 这次没能显示出窗口。主进程留下的启动记录：');
    console.log('----------------------------------------');
    if (notes) {
      for (const line of notes.split(/\r?\n/)) console.log(`  ${line}`);
    } else {
      console.log('  （空 —— 说明 Electron 在加载主进程代码之前就退出了）');
    }
    console.log('----------------------------------------');
    console.log('[launch] 想立刻用 --no-sandbox 模式重试：dev.cmd --no-sandbox');
    console.log('[launch] 或者运行诊断脚本：诊断.cmd');
  }

  if (existsSync(launchMarker) && (code ?? 0) !== 0 && !succeeded) {
    console.log('[launch] 提示：再运行一次会自动加上 --no-sandbox。');
  }
  // 归一化：Electron 崩溃时会给一个很大的负数（STATUS_ACCESS_VIOLATION 之类），
  // 直接透出去在某些终端里会显示成奇怪的值。
  process.exit(succeeded ? 0 : 1);
});
