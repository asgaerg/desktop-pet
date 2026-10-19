/**
 * 浏览器兜底：用一个**隐藏的 Chromium 窗口**去读网页（2026-10-13）。
 *
 * ## 为什么需要它
 *
 * 用户问「起点和番茄你也帮我测测能不能行」时发现：起点**全站**都返回
 *
 * ```
 * HTTP 202
 * set-cookie: x-waf-captcha-referer=
 * 正文只有：<script src="/C2WF946J0/probe.js?v=vc1jasc"></script>
 * ```
 *
 * 那是「**执行 `probe.js` → 它算出结果 → 带上 cookie 重试**」的 WAF 挑战。
 * 纯 HTTP 客户端（Python 的 urllib）做不到 —— **但 Chromium 可以**。
 *
 * 而这个项目**本来就是 Electron 应用**，所以"用真浏览器读网页"
 * 是**零新增依赖**的。
 *
 * ## 🔴 为什么放在主进程而不是 Python 里
 *
 * 三个理由，每一个都是硬的：
 *
 * ① **我们本来就是 Electron** —— 主进程里 `new BrowserWindow()` 就行，
 *    Python 那边要装 Playwright + 一个 200MB 的 Chromium。
 * ② **Probe 实测能过**（`probe-qidian-browser.py`，历史实测脚本）：起点书页拿到
 *    22,161 字正文 + 真标题，cookie 里有 `_csrfToken` / `w_tsfp`。
 * ③ **不新增窗口** —— 用 `show: false` 的隐藏窗口，用户看不见也点不到。
 *
 * ## ⚠️ 它慢，而且**慢是本质的**
 *
 * 实测一次 14~23 秒：
 *
 * ```
 * 建窗口 + 加载 + 等 probe.js 跑完（3.5 秒）+ 读正文
 * ```
 *
 * 那 3.5 秒是 **WAF 挑战自己的耗时**，不是实现问题 ——
 * 想更快就只能不过 WAF（那就读不到）。
 *
 * 所以它**默认关闭**，由用户在设置里打开（`web.browser_fallback`）。
 *
 * ## ⚠️ 隐私口径：这是"以用户的身份浏览"
 *
 * 走浏览器意味着：会带 `User-Agent`、会存 cookie、会在对方站上留下
 * 一个真实浏览器会话。这**比匿名 HTTP 抓取的暴露面大**，
 * 所以必须由用户显式同意，不能默认开。
 *
 * ## 🔴 UA 必须伪装成真 Chrome
 *
 * Electron 的默认 UA 里带着 `Electron/32.3.3` —— 那是**明着告诉对方
 * "我是机器人"**。很多站（包括起点）会因此给降级页或直接挑战。
 *
 * ⚠️ 用**稳定**的版本号，不要 `app.getVersion()`：
 * 那样每次升级 Electron 都会换 UA，行为跟着变（今天是 32 换成 33 就变）。
 */

import { app, BrowserWindow, session, type Session } from 'electron';
import { readdir, stat } from 'node:fs/promises';
import { join } from 'node:path';

/**
 * 一个"看起来就是普通 Chrome"的 User-Agent。
 *
 * ⚠️ 故意**写死**而不是从 `process.versions.chrome` 拼 ——
 * 见文件头的说明。
 */
const CHROME_UA =
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 ' +
  '(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36';

/**
 * 用**持久化的 partition**。
 *
 * ## 为什么要持久化（而不是 `Date.now()` 那种一次性 partition）
 *
 * 两个好处：
 *
 * ① **WAF 通行证能复用** —— 起点过了一次挑战之后 cookie 里就有
 *    `_csrfToken` / `w_tsfp`，下一个页面就不用再过一遍。
 *    （实测：过完一次之后同 session 再读别的页明显快。）
 * ② **不用每次重建 session** —— 重建会丢掉那些 cookie。
 *
 * ⚠️ 代价是会在磁盘上留一份浏览器数据（cookies / localStorage）。
 * 但那是"一个只用来读网页的隔离 session"，不碰用户的日常浏览器，
 * 也不碰宠物窗口的 session。
 */
const PARTITION = 'persist:pet-browser-fallback';

/**
 * WAF 挑战跑完需要多久 —— **实测**得出的值。
 *
 * 探测脚本里用的是 3500ms（能过）。这里留一点余量到 4500ms，
 * 因为窗口第一次创建时要初始化渲染进程，比后续加载慢。
 *
 * ⚠️ 这个值**不能省**：不等的话 `probe.js` 还没算出 cookie，
 * 我们读到的就是那个 209 字节的空壳（`<body></body>`）。
 */
const CHALLENGE_WAIT_MS = 4500;

/** 整体超时。含建窗口 + 加载 + 等挑战 + 读正文。 */
const DEFAULT_TIMEOUT_MS = 25_000;

/** 读回来的正文上限（和 `web.py` 的 `MAX_CONTENT_CHARS` 对齐）。 */
const MAX_TEXT_CHARS = 4000;

export interface BrowserFetchResult {
  ok: boolean;
  url: string;
  /** 最终 URL（可能被重定向过） */
  finalUrl?: string;
  title?: string;
  text?: string;
  /** `<meta name/property>` 里挑出来的，键是原始 name/property */
  metas?: Record<string, string>;
  /** 失败原因。⚠️ 一定要给得出，含糊的失败会让模型编造内容。 */
  reason?: string;
  /** 从开始到结束的毫秒数（排查"为什么这么慢"用） */
  elapsedMs?: number;
}

let sharedWindow: BrowserWindow | null = null;
let sharedSession: Session | null = null;
/** 串行化 —— 同时读两个页面会互相抢那个唯一的隐藏窗口。 */
let busy: Promise<unknown> = Promise.resolve();

function getSession(): Session {
  if (!sharedSession) {
    sharedSession = session.fromPartition(PARTITION);
    // ⚠️ 必须在**第一次加载之前**设 UA，否则请求已经发出去了
    sharedSession.setUserAgent(CHROME_UA);
  }
  return sharedSession;
}

async function getWindow(): Promise<BrowserWindow> {
  if (sharedWindow && !sharedWindow.isDestroyed()) return sharedWindow;
  getSession();
  sharedWindow = new BrowserWindow({
    // 隐藏：用户看不见、点不到、不进任务栏
    show: false,
    width: 1280,
    height: 900,
    webPreferences: {
      partition: PARTITION,
      // 读网页不需要 Node —— 更要**关掉**（少一个攻击面）
      nodeIntegration: false,
      contextIsolation: true,
      /*
       * ⚠️ 关掉硬件加速带来的一个副作用：有些站会因此走降级分支。
       * 但这里没关 —— 关的是整个 app 的（`app.disableHardwareAcceleration()`），
       * 探测脚本里关了、也能过起点，所以不是必需条件。
       */
      images: false,
    },
  });
  sharedWindow.on('closed', () => {
    sharedWindow = null;
  });
  return sharedWindow;
}

/**
 * 读一个网页的正文。**任何失败都返回 `ok: false` + 原因**。
 *
 * ⚠️ 调用方（Python）必须能处理"浏览器不可用" —— 那不该让整条链路失败，
 * 而是回退到普通抓取。
 */
export async function fetchPageViaBrowser(
  url: string,
  options: { timeoutMs?: number; waitMs?: number; log?: (line: string) => void } = {},
): Promise<BrowserFetchResult> {
  const say = options.log ?? (() => {});
  const timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;
  const waitMs = options.waitMs ?? CHALLENGE_WAIT_MS;
  const target = String(url ?? '').trim();

  if (!/^https?:\/\//i.test(target)) {
    return { ok: false, url: target, reason: '地址不是 http/https' };
  }

  /*
   * 串行化：把这次调用接到上一次后面。
   *
   * ⚠️ 为什么要串行：只有一个隐藏窗口。两个读取同时跑的话，
   * 第二个 `loadURL()` 会把第一个的页面顶掉，两边都拿到对方的内容 ——
   * 那是一种**很难查**的错（内容对不上，但没有任何报错）。
   */
  const run = busy.then(() => doFetch(target, { timeoutMs, waitMs, log: say }));
  busy = run.catch(() => {});
  return run;
}

async function doFetch(
  target: string,
  options: { timeoutMs: number; waitMs: number; log: (line: string) => void },
): Promise<BrowserFetchResult> {
  const started = Date.now();
  const say = options.log;

  let window: BrowserWindow;
  try {
    window = await getWindow();
  } catch (error) {
    return {
      ok: false,
      url: target,
      reason: `建浏览器窗口失败：${(error as Error)?.message || String(error)}`,
    };
  }

  /*
   * ⚠️ **整段包超时**，不然一个卡住的页面会把工具调用挂到 30 秒上限
   * （那期间用户看到状态条一直"搜索中"）。
   */
  let timer: NodeJS.Timeout | undefined;
  try {
    const result = await Promise.race([
      (async (): Promise<BrowserFetchResult> => {
        await window.loadURL(target);
        /*
         * 等 WAF 挑战跑完（见 `CHALLENGE_WAIT_MS` 的说明）。
         * 不等的话读到的就是 209 字节的空壳。
         */
        await new Promise((resolve) => setTimeout(resolve, options.waitMs));

        /*
         * 一次 `executeJavaScript` 把**所有**要的东西取回来 ——
         * 每次调用都是一次 IPC 往返，取三次不如取一次。
         *
         * ⚠️ `executeJavaScript` 里返回的必须是**可结构化克隆**的值，
         * 所以 `JSON.stringify` 再 parse（直接返回对象也行，但这样两边
         * 的字段名在代码里是显式的，好读）。
         */
        const raw = (await window.webContents.executeJavaScript(`
          (function () {
            var metas = {};
            document.querySelectorAll('meta[property], meta[name]').forEach(function (m) {
              var key = m.getAttribute('property') || m.getAttribute('name') || '';
              if (!key) return;
              if (key.indexOf('og:') !== 0 && key !== 'description' && key !== 'keywords') return;
              metas[key] = String(m.content || '').slice(0, 600);
            });
            var text = document.body ? document.body.innerText : '';
            return JSON.stringify({
              title: document.title || '',
              text: text.slice(0, ${MAX_TEXT_CHARS}),
              bodyLen: text.length,
              metas: metas,
            });
          })()
        `)) as string;

        const parsed = JSON.parse(raw) as {
          title: string;
          text: string;
          bodyLen: number;
          metas: Record<string, string>;
        };

        /*
         * 🔴 判"到底读到没有"要看**正文长度**，不能看 HTTP 状态。
         *
         * 因为 WAF 挑战的响应是 **202 + 209 字空壳** ——
         * 状态码看起来"成功"，内容却是空的。
         */
        if (parsed.bodyLen < 80) {
          return {
            ok: false,
            url: target,
            finalUrl: window.webContents.getURL(),
            title: parsed.title,
            reason:
              '页面几乎是空的（' +
              parsed.bodyLen +
              ' 字）—— 多半是反爬挑战没过去，或者页面是纯 JS 渲染的。',
            elapsedMs: Date.now() - started,
          };
        }

        return {
          ok: true,
          url: target,
          finalUrl: window.webContents.getURL(),
          title: parsed.title,
          text: parsed.text,
          metas: parsed.metas,
          elapsedMs: Date.now() - started,
        };
      })(),
      new Promise<BrowserFetchResult>((resolve) => {
        timer = setTimeout(() => {
          resolve({
            ok: false,
            url: target,
            reason: `浏览器读超时（${options.timeoutMs}ms）`,
            elapsedMs: Date.now() - started,
          });
        }, options.timeoutMs);
      }),
    ]);

    say(
      `[浏览器兜底] ${result.ok ? '成功' : '失败'} 用时 ${result.elapsedMs ?? 0}ms ` +
        `${target.slice(0, 70)}${result.ok ? '' : ` 原因=${result.reason || ''}`}`,
    );
    return result;
  } catch (error) {
    const message = (error as Error)?.message || String(error);
    say(`[浏览器兜底] 抛异常：${message}（${target.slice(0, 70)}）`);
    return { ok: false, url: target, reason: `浏览器读取出错：${message}`, elapsedMs: Date.now() - started };
  } finally {
    if (timer) clearTimeout(timer);
  }
}

/** 退出时收摊（主进程 `will-quit` 里调）。 */
export function disposeBrowserFallback(): void {
  if (sharedWindow && !sharedWindow.isDestroyed()) {
    sharedWindow.destroy();
  }
  sharedWindow = null;
}

/* --------------------------------------------------------------------------- *
 * 清空浏览数据（2026-10-13 用户要求）
 *
 * ## 用户的原话
 *
 * > 「清楚的是我浏览器上的缓存吗」→ 解释清楚之后 → 「**加**」
 *
 * ## 🔴 只动**我们那个 partition**，绝不碰 defaultSession
 *
 * 这一点是这个功能**最重要的约束**。用户担心的正是"会不会删我 Chrome 的东西"——
 * 而 `Electron` 的 partition 本来就是隔离存储：
 *
 * ```
 * 宠物的   %APPDATA%\@pet\desktop\Partitions\pet-browser-fallback\
 * 用户的   %LOCALAPPDATA%\Google\Chrome\...  /  Microsoft\Edge\...
 * ```
 *
 * 它们是**两个不相干的目录**。所以这里原则上只用
 * `session.fromPartition(PARTITION)`，一行 `defaultSession` 都不能出现。
 *
 * ⚠️ 所以 `defaultSession` 在这个文件里**刻意不 import** ——
 * 不是忘了，是不给自己留"手滑清错"的机会。
 * --------------------------------------------------------------------------- */

/**
 * 那份 session 占用多少磁盘。**分成"浏览记录"和"框架开销"两块。**
 *
 * ## 🔴 为什么要分两块（是被实测逼出来的）
 *
 * 清空之后体积**不会归零** —— 实测 8.89 MB 清成 2.5 MB，剩下的全是：
 *
 * ```
 * GPUCache / DawnGraphiteCache / DawnWebGPUCache   各 270 KB
 * Cache\Cache_Data\index                            525 KB
 * Cookies（**空库**，22:54:46 新建的）                20 KB
 * ```
 *
 * 那些是 Chromium **空 profile 的固定开销** —— 只要那个后台隐藏窗口活着
 * 就会重建。如果界面上只报一个总数，用户会以为"没清干净"或者"按钮没生效"。
 *
 * 所以分成：
 *
 * ```
 * records   浏览记录（cookie 里的**真实条目**、缓存的数据块）—— 按钮清的就是它
 * overhead  框架开销（GPU 缓存、空库文件）—— 清不掉，也不该清
 * ```
 *
 * ⚠️ "真实条目"数是**从 Cookies 库里数出来的**（见 `countCookies`），
 * 不是靠"文件大小"猜的 —— 空库也有 20 KB，光看大小分不出来。
 */
export interface BrowserDataInfo {
  totalBytes: number;
  /** 浏览记录（可清的）：cookie 条数 + 缓存数据块 */
  recordBytes: number;
  /** 框架开销（清不掉的）：GPU 缓存、空库、配额管理文件 */
  overheadBytes: number;
  /** cookie 的**真实条数**（用 Electron 的 API 数的） */
  cookies: number;
  path: string;
}

export async function browserDataInfo(): Promise<BrowserDataInfo> {
  const target = partitionDir();
  const empty: BrowserDataInfo = {
    totalBytes: 0,
    recordBytes: 0,
    overheadBytes: 0,
    cookies: 0,
    path: target,
  };
  if (!target) return empty;

  let total = 0;
  let overhead = 0;
  try {
    /* 手写递归（`readdir` 的 `recursive` 要 Node 20+ —— 保险起见自己走） */
    const walk = async (dir: string, depth: number): Promise<void> => {
      if (depth > 6) return; // 防病态目录结构把主进程拖死
      let entries: import('node:fs').Dirent[];
      try {
        entries = await readdir(dir, { withFileTypes: true });
      } catch {
        return;
      }
      for (const entry of entries) {
        const full = join(dir, entry.name);
        if (entry.isDirectory()) {
          await walk(full, depth + 1);
          continue;
        }
        if (!entry.isFile()) continue;
        let size = 0;
        try {
          size = (await stat(full)).size;
        } catch {
          continue;
        }
        total += size;
        /* 这些目录/文件是**框架固定开销**，不含用户浏览记录 */
        if (OVERHEAD_PATTERNS.some((pattern) => pattern.test(full))) {
          overhead += size;
        }
      }
    };
    await walk(target, 0);
  } catch {
    return empty;
  }

  const cookies = await countCookies();
  return {
    totalBytes: total,
    recordBytes: Math.max(0, total - overhead),
    overheadBytes: overhead,
    cookies,
    path: target,
  };
}

/**
 * 框架固定开销的路径特征。
 *
 * ⚠️ 宁可**少算开销**（把开销算成记录）也不能多算 ——
 * 多算的话界面上会显示"记录 0 B"而实际还有 cookie，那是**骗人**。
 * 所以这里只列**确定不含用户数据**的：
 *
 * ```
 * GPUCache / Dawn*Cache     纯 GPU 渲染缓存，不含任何站点数据
 * Cache_Data\index          缓存索引（数据块才算记录）
 * Shared Dictionary\db      压缩字典，Chromium 自己维护的
 * QuotaManager              配额管理元数据
 * Trust Tokens              反欺诈令牌（站点相关但**不是浏览记录**）
 * ```
 */
const OVERHEAD_PATTERNS: RegExp[] = [
  /[\\/]GPUCache[\\/]/i,
  /[\\/]Dawn(Graphite|WebGPU)?Cache[\\/]/i,
  /[\\/]Cache_Data[\\/]index$/i,
  /[\\/]Shared Dictionary[\\/]/i,
  /[\\/]QuotaManager$/i,
  /[\\/]Trust Tokens/i,
  /[\\/]Cookies(-journal)?$/i, // 空库也占 20 KB；**条目数**由 countCookies 单独数
];

/**
 * 数那份 session 里**真实有多少条 cookie**。
 *
 * ## 🔴 第一版是错的（被测试抓到的）
 *
 * 我第一版**去抠 `Cookies` 文件的字节**（数里面的主机名串），
 * 因为"这个项目后端一直坚持只用标准库"。
 *
 * ⚠️ 但那是**后端**的约束，这里是 **Electron 主进程** ——
 * 而 Electron **本来就有现成的 API**：
 *
 * 测试结果（`test-cookie-count.py`，历史实测脚本）：
 *
 * ```
 * 真去读了一次起点（走浏览器兜底、WAF 也过了）
 *   -> countCookies() 数出 0 条   ❌ 显然是错的
 * ```
 *
 * SQLite 的页结构 + WAL 让"抠字节"根本不可靠。**用官方 API**。
 *
 * ## 为什么是"条"而不是"个站点"
 *
 * `cookies.get({})` 返回的是**每一条 cookie**（同一站点可能有好几条）。
 * 界面显示"还剩 N 条"比"几个站点"更准确 —— 用户能直接对照
 * "清完应该就是 0 条"。
 */
async function countCookies(): Promise<number> {
  try {
    const list = await getSession().cookies.get({});
    return Array.isArray(list) ? list.length : 0;
  } catch {
    return 0;
  }
}

/** 那份 session 的目录（Electron 的 partition 存储位置）。读不到返回空串。 */
function partitionDir(): string {
  try {
    /*
     * ⚠️ 用 Electron 自己的 API 问路径，不手拼 `%APPDATA%`。
     *
     * 手拼的话就**写死了 Windows 的路径约定** ——
     * macOS 是 `~/Library/Application Support/...`，
     * Linux 是 `~/.config/...`。用 `getPath('userData')` 三平台都对。
     */
    const userData = app.getPath('userData');
    /*
     * partition 名是 `persist:pet-browser-fallback` ——
     * 磁盘上的目录名**去掉 `persist:` 前缀**（实测确认：
     * `...\@pet\desktop\Partitions\pet-browser-fallback`）。
     */
    const name = PARTITION.replace(/^persist:/, '');
    return join(userData, 'Partitions', name);
  } catch {
    return '';
  }
}

export interface ClearBrowserDataResult {
  ok: boolean;
  /** 清之前有多少字节（界面上显示"清掉了多少"） */
  bytesBefore: number;
  /** 清掉了哪些存储类型（写日志 + 给界面看） */
  cleared: string[];
  message: string;
}

/**
 * 清空那份 session 的浏览数据。**只动我们的 partition。**
 *
 * ## 🔴 顺带把隐藏窗口也重建
 *
 * 因为窗口还持有磁盘上的缓存文件 —— 不清掉窗口的话，
 * Windows 上那些文件**删不掉**（被占用），清了等于没清。
 * 而 `clearStorageData()` 是 Electron 官方 API，能处理这个。
 *
 * 之后下一次读网页会**重新过一次 WAF**（因为通行证 cookie 没了）——
 * 那是"清干净"的必然代价，界面上要说明。
 */
export async function clearBrowserData(): Promise<ClearBrowserDataResult> {
  const before = await browserDataInfo();
  const cleared: string[] = [];

  /*
   * ⚠️ 先销毁隐藏窗口 —— 它持有缓存文件的句柄。
   * 不销毁的话 `clearStorageData()` 在 Windows 上会删不干净
   * （文件被占用），而它**不报错** —— 表现是"清了但体积没变"。
   */
  if (sharedWindow && !sharedWindow.isDestroyed()) {
    sharedWindow.destroy();
  }
  sharedWindow = null;

  try {
    const target = getSession();
    /*
     * 显式列出要清的存储类型（不用默认值）——
     * 这样"清了什么"是**代码里写死的**，日志和界面能如实说出来。
     *
     * ⚠️ 故意**不含** `'cookies'` 之外的登录类数据 —— 我们本来就没有
     * 登录态，列全是为了"清干净"，不是要保留什么。
     */
    const storages = [
      'cookies',
      'localstorage',
      'indexdb',
      'cachestorage',
      'serviceworkers',
      'shadercache',
      'websql',
    ] as const;
    await target.clearStorageData({ storages: [...storages] });
    cleared.push(...storages);

    // 磁盘缓存要单独清（它不在 storages 那组里）
    await target.clearCache();
    cleared.push('cache');

    /*
     * 顺手断开 HTTP 连接池（否则可能还挂着旧连接）。
     *
     * ⚠️ **特性检测**：`closeAllConnections()` 是较新的 API，
     * 老版本 Electron 上没有这个方法 —— 直接调会抛 `TypeError`，
     * 而那个异常会把整个"清理成功"变成"清理失败"（前面明明清掉了）。
     */
    const closer = (target as unknown as { closeAllConnections?: () => Promise<void> })
      .closeAllConnections;
    if (typeof closer === 'function') {
      await closer.call(target);
      cleared.push('connections');
    }
  } catch (error) {
    return {
      ok: false,
      bytesBefore: before.totalBytes,
      cleared,
      message: `清空失败：${(error as Error)?.message || String(error)}`,
    };
  }

  const after = await browserDataInfo();
  return {
    ok: true,
    bytesBefore: before.totalBytes,
    cleared,
    /*
     * ⚠️ 消息以 **cookie 条数** 为主，体积只作背景。
     *
     * 第一版只报体积，结果是「2.5 MB → 2.5 MB」—— 看起来像**没清干净**。
     * 实际是那个隐藏窗口一直活着、Chromium 不断重建缓存块，
     * 所以**体积根本不会归零**（也不该指望它归零）。
     * 而"还剩 0 个站点的 cookie"才是真实、可验证的信号。
     */
    message:
      after.cookies > 0
        ? `已清空，但还留着 ${after.cookies} 个站点的 cookie（占用 ${formatBytes(after.totalBytes)}）`
        : `已清空 —— 没有留下 cookie（磁盘占用 ${formatBytes(after.totalBytes)}，` +
          `其中 ${formatBytes(after.overheadBytes)} 是浏览器的固定开销，清不掉也不该清）`,
  };
}

/** 字节数变成人看得懂的形式（界面上显示"8.9 MB"而不是 9328123）。 */
export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB'];
  let value = bytes;
  let index = 0;
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024;
    index += 1;
  }
  return `${value >= 100 || index === 0 ? Math.round(value) : value.toFixed(1)} ${units[index]}`;
}
