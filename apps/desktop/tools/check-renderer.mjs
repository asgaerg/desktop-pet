/**
 * 渲染层语法门禁（`npm run check:renderer`）。
 *
 * 为什么必须有这一步：`renderer/` 下的文件是直接喂给浏览器/Chromium 的，
 * 一旦混进 TypeScript 语法（哪怕只是一个 `export interface`），模块会**整体解析失败**，
 * 而且失败得非常安静 —— Python 侧的自检全绿、主进程正常、日志里也没有明显线索
 * （只在 devtools 控制台里有一行 SyntaxError）。早期 sprite.js 就是这么坏掉的，
 * 立绘在整个阶段里其实从未加载过。
 *
 * 这里用 `vm.Script` 做**纯语法**编译，不执行任何代码：
 *   · 脚本类文件（普通 script）直接用；
 *   · ESM 文件（含 import/export）用 SourceTextModule 不可用时退化为
 *     "剥掉 import/export 语句后再编译"，足以捕捉类型注解一类语法错误。
 */

import { readFileSync, existsSync, readdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import vm from 'node:vm';

const here = path.dirname(fileURLToPath(import.meta.url));
const rendererDir = path.join(here, '..', 'src', 'renderer');

/** 需要检查的文件：手写脚本 + 打包产物，但不含资源目录 */
function collect(dir) {
  const out = [];
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    if (entry.name === 'assets' || entry.name === 'node_modules') continue;
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) out.push(...collect(full));
    else if (entry.name.endsWith('.js') || entry.name.endsWith('.mjs')) out.push(full);
  }
  return out;
}

const files = collect(rendererDir);
if (files.length === 0) {
  console.error('未找到任何渲染层脚本，检查路径是否正确');
  process.exit(1);
}

let failed = 0;
for (const file of files) {
  const rel = path.relative(rendererDir, file);
  const source = readFileSync(file, 'utf-8');

  // 粗糙但足够：把顶层 import/export 语句替换成空白，保留行号便于报错定位。
  const stripped = source
    .replace(/^\s*import\s[^;\n]*;?/gm, (m) => ' '.repeat(m.length))
    .replace(/^\s*export\s+(default\s+)?/gm, (m) => ' '.repeat(m.length));

  try {
    new vm.Script(stripped, { filename: file });
    console.log(`  OK   ${rel}`);
  } catch (error) {
    failed += 1;
    const line = error.stack?.split('\n')[0] ?? String(error);
    console.error(`  FAIL ${rel}\n       ${line}`);
    if (/interface|Unexpected token ':'|Unexpected token 'type'/.test(String(error.message))) {
      console.error('       疑似混入了 TypeScript 语法：浏览器无法解析，请改为 .ts 并由 esbuild 打包。');
    }
  }
}

if (failed > 0) {
  console.error(`\n渲染层语法检查未通过：${failed}/${files.length} 个文件有问题`);
  process.exit(1);
}
console.log(`\n渲染层语法检查通过：${files.length} 个文件`);

// --------------------------------------------------------------------------- //
// 契约检查：渲染层与 DOM / preload 之间的一致性
//
// 为什么需要这一类检查（都是真实踩过的）：
//   · `sprite.js` 里混进 TypeScript 语法 -> 模块解析失败、立绘从未加载；
//   · `pet.on('voice.state')` 的通道不在 preload 白名单 -> preload 抛异常，
//     而那两行在模块顶层，**整个渲染层脚本直接不执行**（约等于界面全废）；
//   · `els.hint` 没在 els 对象里定义 -> 一调用就 TypeError。
//
// 这三类问题的共同点是：**语法完全正确**，静态语法检查一个都抓不到，
// 只有在真实运行时才暴露。而本项目的界面又无法在开发环境里目视验证，
// 所以必须靠这里的一致性检查兜住底线。
// --------------------------------------------------------------------------- //

const appSource = readFileSync(path.join(rendererDir, 'app.js'), 'utf-8');
const htmlSource = readFileSync(path.join(rendererDir, 'index.html'), 'utf-8');
const preloadSource = readFileSync(
  path.join(rendererDir, '..', 'preload', 'index.ts'),
  'utf-8',
);

let contractIssues = 0;
const report = (ok, label, detail) => {
  if (ok) {
    console.log(`  OK   ${label}`);
  } else {
    contractIssues += 1;
    console.error(`  FAIL ${label}\n       ${detail}`);
  }
};

// 1) app.js 取的元素 id 必须在 index.html 里存在
{
  const ids = [...appSource.matchAll(/\$\('([^']+)'\)/g)].map((match) => match[1]);
  const missing = [...new Set(ids)].filter((id) => !htmlSource.includes(`id="${id}"`));
  report(
    missing.length === 0,
    `元素 id 引用（${new Set(ids).size} 个）`,
    `index.html 中不存在：${missing.join(', ')}`,
  );
}

// 2) app.js 订阅的通道必须在 preload 白名单里，否则 preload 会抛异常
{
  const channels = [...new Set([...appSource.matchAll(/pet\.on\('([^']+)'/g)].map((m) => m[1]))];
  const allowBlock = /ALLOWED_CHANNELS = new Set\(\[([\s\S]*?)\]\)/.exec(preloadSource);
  const allowed = allowBlock ? [...allowBlock[1].matchAll(/'([^']+)'/g)].map((m) => m[1]) : [];
  const bad = channels.filter((channel) => !allowed.includes(channel));
  report(
    bad.length === 0 && allowed.length > 0,
    `订阅通道（${channels.length} 个）都在 preload 白名单内`,
    bad.length
      ? `不在白名单：${bad.join(', ')}（pet.on 会直接抛错，模块顶层抛错会让整个脚本不执行）`
      : 'preload 的 ALLOWED_CHANNELS 解析失败',
  );
}

// 3) app.js 调用的 pet.* 方法必须在 preload 里暴露
{
  const methods = [...new Set([...appSource.matchAll(/pet\.([a-zA-Z_]\w*)\(/g)].map((m) => m[1]))];
  const missing = methods.filter((method) => !new RegExp(`${method}\\s*\\(`).test(preloadSource));
  report(
    missing.length === 0,
    `pet API 调用（${methods.length} 个）都已暴露`,
    `preload 未暴露：${missing.join(', ')}`,
  );
}

// 5) 设置页面：元素 id 与 preload API 也要对得上
//
// 设置页面是后加的，用的是 `el('id')` 与 `window.petSettings.*`（独立 preload）。
// 不检查的话，写错一个 id 或调一个没暴露的方法，表现同样是"点了没反应" ——
// 而这正是这个项目反复踩到的坑。设置页面是唯一能改 API 密钥与退出程序的地方，
// 它静默失效的代价尤其高。
{
  const settingsJs = readFileSync(path.join(rendererDir, 'settings.js'), 'utf-8');
  const settingsHtml = readFileSync(path.join(rendererDir, 'settings.html'), 'utf-8');
  const settingsPreload = readFileSync(
    path.join(rendererDir, '..', 'preload', 'settings.ts'),
    'utf-8',
  );

  const ids = [...settingsJs.matchAll(/el\('([^']+)'\)/g)].map((m) => m[1]);
  const missingIds = [...new Set(ids)].filter((id) => !settingsHtml.includes(`id="${id}"`));
  report(
    missingIds.length === 0 && new Set(ids).size > 0,
    `设置页面元素 id 引用（${new Set(ids).size} 个）`,
    missingIds.length ? `settings.html 中不存在：${missingIds.join(', ')}` : '未解析到任何 id',
  );

  const methods = [...new Set([...settingsJs.matchAll(/api\.([a-zA-Z_]\w*)\(/g)].map((m) => m[1]))];
  const missingMethods = methods.filter(
    (name) => !new RegExp(`\\b${name}\\s*\\(`).test(settingsPreload),
  );
  report(
    missingMethods.length === 0 && methods.length > 0,
    `设置页面 API 调用（${methods.length} 个）都在 preload 里`,
    missingMethods.length ? `未暴露：${missingMethods.join(', ')}` : '未解析到任何调用',
  );
}

// 6) 对话条页面（chat.js + chat.html + preload/chat.ts）
//
// 输入框搬出宠物窗口之后，对话条成了**发消息的唯一入口** ——
// 它静默失效的代价和设置页面一样高（用户打字没反应，却看不出哪里坏了）。
// 所以同样要校验 id 与 preload 暴露的方法。
{
  const chatJs = readFileSync(path.join(rendererDir, 'chat.js'), 'utf-8');
  const chatHtml = readFileSync(path.join(rendererDir, 'chat.html'), 'utf-8');
  const chatPreload = readFileSync(path.join(rendererDir, '..', 'preload', 'chat.ts'), 'utf-8');

  const ids = [...chatJs.matchAll(/el\('([^']+)'\)/g)].map((m) => m[1]);
  const missingIds = [...new Set(ids)].filter((id) => !chatHtml.includes(`id="${id}"`));
  report(
    missingIds.length === 0 && new Set(ids).size > 0,
    `对话条元素 id 引用（${new Set(ids).size} 个）`,
    missingIds.length ? `chat.html 中不存在：${missingIds.join(', ')}` : '未解析到任何 id',
  );

  // window.pet.* 是这份 preload 暴露的
  const methods = [
    ...new Set([...chatJs.matchAll(/window\.pet\.([a-zA-Z_]\w*)\(/g)].map((m) => m[1])),
  ];
  const missingMethods = methods.filter(
    (name) => !new RegExp(`\\b${name}\\s*\\(`).test(chatPreload),
  );
  report(
    missingMethods.length === 0 && methods.length > 0,
    `对话条 API 调用（${methods.length} 个）都在 preload 里`,
    missingMethods.length ? `未暴露：${missingMethods.join(', ')}` : '未解析到任何调用',
  );
}

// 7) 记忆管理页面（memory.js + memory.html + preload/memory.ts）
//
// 这个页面是**独立窗口**，有自己的一份 preload（只暴露记忆相关通道）。
// 与设置页、对话条同理：id 写错或调一个没暴露的方法，表现都是"点了没反应"，
// 而它管的是**用户唯一能编辑记忆的地方** —— 静默失效的代价很高。
{
  const memJs = readFileSync(path.join(rendererDir, 'memory.js'), 'utf-8');
  const memHtml = readFileSync(path.join(rendererDir, 'memory.html'), 'utf-8');
  const memPreload = readFileSync(path.join(rendererDir, '..', 'preload', 'memory.ts'), 'utf-8');

  const ids = [...memJs.matchAll(/el\('([^']+)'\)/g)].map((m) => m[1]);
  const missingIds = [...new Set(ids)].filter((id) => !memHtml.includes(`id="${id}"`));
  report(
    missingIds.length === 0 && new Set(ids).size > 0,
    `记忆管理元素 id 引用（${new Set(ids).size} 个）`,
    missingIds.length ? `memory.html 中不存在：${missingIds.join(', ')}` : '未解析到任何 id',
  );

  // `api.memoryAction(...)` 是这份 preload 暴露的
  const methods = [...new Set([...memJs.matchAll(/api\.([a-zA-Z_]\w*)\(/g)].map((m) => m[1]))];
  const missingMethods = methods.filter(
    (name) => !new RegExp(`\\b${name}\\s*\\(`).test(memPreload),
  );
  report(
    missingMethods.length === 0 && methods.length > 0,
    `记忆管理 API 调用（${methods.length} 个）都在 preload 里`,
    missingMethods.length ? `未暴露：${missingMethods.join(', ')}` : '未解析到任何调用',
  );

  /*
   * ⚠️ 这个页面调的 `action` 必须是后端**真的认识**的那批。
   *
   * 写错一个 action 的表现是「点了没反应」—— 后端返回
   * `{ok:false, message:"未知操作：'xxx'"}`，而如果界面只判 `ok` 不显示 message，
   * 用户就只看到什么都没发生。所以在这里对着 gateway 的分支清单核一遍。
   */
  const gatewaySrc = readFileSync(
    path.join(rendererDir, '..', '..', '..', 'brain', 'pet', 'gateway.py'),
    'utf-8',
  );
  const known = new Set(
    [...gatewaySrc.matchAll(/action == "([a-z_]+)"/g)].map((m) => m[1]),
  );
  const used = [...new Set([...memJs.matchAll(/call\('([a-z_]+)'/g)].map((m) => m[1]))];
  const unknown = used.filter((a) => !known.has(a));
  report(
    unknown.length === 0 && used.length > 0,
    `记忆管理用的 action 后端都认识（${used.length} 个）`,
    unknown.length ? `后端没有这些分支：${unknown.join(', ')}` : '未解析到任何 action',
  );
}

// 9) 菜单页面（petMenu.js + petMenu.html + preload/menu.ts）—— 2026-10-14
//
// 右键菜单换成了自绘窗口，于是它有了两条新的静默失效路径：
//   · 页面加载失败 -> 主进程**静默退回原生菜单**（界面还能用，只是完全不是设计稿）
//   · 通道名拼错   -> 「菜单弹出来了、点了没反应」
// 而它是改设置 / 退出程序的**唯一入口**（宠物窗口刻意没有关闭按钮）。
// 所以照设置页 / 对话条 / 记忆管理那三套，校验 id 与 preload 方法。
{
  const menuJs = readFileSync(path.join(rendererDir, 'petMenu.js'), 'utf-8');
  const menuHtml = readFileSync(path.join(rendererDir, 'petMenu.html'), 'utf-8');
  const menuPreload = readFileSync(path.join(rendererDir, '..', 'preload', 'menu.ts'), 'utf-8');

  const ids = [...menuJs.matchAll(/getElementById\('([^']+)'\)/g)].map((m) => m[1]);
  const missingIds = [...new Set(ids)].filter((id) => !menuHtml.includes(`id="${id}"`));
  report(
    missingIds.length === 0 && new Set(ids).size > 0,
    `菜单页面元素 id 引用（${new Set(ids).size} 个）`,
    missingIds.length ? `petMenu.html 中不存在：${missingIds.join(', ')}` : '未解析到任何 id',
  );

  const methods = [...new Set([...menuJs.matchAll(/\bapi\.([a-zA-Z_]\w*)\(/g)].map((m) => m[1]))];
  const missingMethods = methods.filter(
    (name) => !new RegExp(`\\b${name}\\s*\\(`).test(menuPreload),
  );
  report(
    missingMethods.length === 0 && methods.length > 0,
    `菜单页面 API 调用（${methods.length} 个）都在 preload 里`,
    missingMethods.length ? `未暴露：${missingMethods.join(', ')}` : '未解析到任何调用',
  );
}

// 10) 菜单图标名：**主进程给的图标名必须在渲染层的图标表里存在**
//
// ## 为什么非要跨文件查这一条
//
// 图标是 `mask-image` 的 data URI（`petMenu.icons.css`，由脚本从设计稿 PDF 生成）。
// 名字写错时**什么都不会发生**：mask 取不到图 -> 那一格空白。
// 不报错、不占位、也没有任何日志 —— 用户看到的是"这一行没有图标"，
// 而那是"设计如此"还是"写错了"完全看不出来。
//
// 而图标名写在 `petMenuTemplate.ts`（主进程）、形状在渲染层的 CSS 里，
// 两侧分开，正是最容易漂移的结构。
{
  const template = readFileSync(
    path.join(rendererDir, '..', 'main', 'petMenuTemplate.ts'),
    'utf-8',
  );
  const iconCss = readFileSync(path.join(rendererDir, 'petMenu.icons.css'), 'utf-8');

  const used = [...new Set([...template.matchAll(/icon:\s*'([^']+)'/g)].map((m) => m[1]))];
  const known = new Set([...iconCss.matchAll(/\[data-icon='([^']+)'\]/g)].map((m) => m[1]));
  const unknown = used.filter((name) => !known.has(name));
  report(
    unknown.length === 0 && used.length > 0,
    `菜单图标名（${used.length} 个）都能在图标表里找到`,
    unknown.length
      ? `petMenu.icons.css 里没有：${unknown.join(', ')}（那一格会空白，且不报错）`
      : '未解析到任何图标名 / 图标表解析失败',
  );
}

// 8) els.* 用到的属性都必须在 els 对象里定义
{
  const elsBlock = /const els = \{([\s\S]*?)\};/.exec(appSource);
  const defined = elsBlock ? [...elsBlock[1].matchAll(/^\s*(\w+):/gm)].map((m) => m[1]) : [];
  const used = [...new Set([...appSource.matchAll(/els\.([a-zA-Z_]\w*)/g)].map((m) => m[1]))];
  const undeclared = used.filter((key) => !defined.includes(key));
  report(
    undeclared.length === 0 && defined.length > 0,
    `els 属性（用到 ${used.length} 个）都已定义`,
    undeclared.length ? `未定义：${undeclared.join(', ')}（调用时 TypeError）` : 'els 对象解析失败',
  );
}

// 5) 渲染层文件不能带 UTF-8 BOM
//
// BOM 属于"语法正确但可能让模块挂掉"那一类：浏览器/Node 的 ESM 加载器对它处理
// 不一致，而编辑器里完全看不出来。某些命令行的写入操作（例如 PowerShell 5.1 的
// `Set-Content -Encoding UTF8`）会**默认加上 BOM**，所以这道检查很有必要。
{
  const files = [...collect(rendererDir), path.join(rendererDir, 'index.html')];
  const withBom = files.filter((file) => {
    const bytes = readFileSync(file);
    return bytes.length >= 3 && bytes[0] === 0xef && bytes[1] === 0xbb && bytes[2] === 0xbf;
  });
  report(
    withBom.length === 0,
    `渲染层文件无 UTF-8 BOM（${files.length} 个）`,
    `含 BOM：${withBom.map((f) => path.relative(rendererDir, f)).join(', ')}`,
  );
}

// 采集闸门：**推帧必须受闸门控制**
//
// ## 为什么要单独立一条（2026-10-10）
//
// 这个 bug 藏了很久，因为**它不报错、也不缺任何东西** ——
// `voice-io.js` 只是"无条件把每一帧推出去"，看着完全正常。
//
// 后果是链条式的：
//
//   前端一直推（50 帧/秒）
//     -> sidecar 的 `QueueAudioInput(max_frames=100)` 积着 **2 秒**旧音频
//     -> 闸门一开，端点/VAD 先吃到这 2 秒旧音频
//     -> 环境噪声把 VAD 的噪声底抬高
//     -> 阈值 = max(0.012, 噪声底 × 3) 变高
//     -> **用户真正说话的能量低于新阈值，被判成静音**
//     -> 端点不攒句子 -> `force_end()` 返回 None -> 松手后没识别
//
// 用户看到的正是「**第一次能用，之后按了没反应**」。
//
// 静态检查能抓住的正是"闸门检查还在不在 `pushSamples` 最前面" ——
// 有人顺手删掉那一行（比如为了"修"识别不灵），这个 bug 就原样复活。
{
  const voiceIoSource = readFileSync(path.join(rendererDir, 'voice-io.js'), 'utf-8');

  // ① pushSamples 的**第一句**必须是闸门检查
  const push = /pushSamples\s*\([^)]*\)\s*\{([\s\S]*?)\n  \}/.exec(voiceIoSource);
  const firstStatement = push
    ? push[1].replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/.*$/gm, '').trim().split('\n')[0]
    : '';
  report(
    Boolean(push) && /if\s*\(\s*!\s*this\.gate\s*\)\s*return/.test(firstStatement),
    'pushSamples 第一句是闸门检查（不通过就一帧都不发）',
    push
      ? `实际第一句：${firstStatement.slice(0, 80)}\n` +
        '       闸门必须在最前面 —— 放在后面会让 `pending` 攒下"闸门关闭期间"的样本，' +
        '开闸时先吐旧音频，VAD 噪声底被抬高 -> 用户说话被判成静音'
      : '找不到 pushSamples，源码结构变了？',
  );

  // ② setGate 必须存在，且开关两个方向都清 pending
  report(
    /setGate\s*\(\s*\w+\s*\)\s*\{[\s\S]*?this\.gate\s*=/.test(voiceIoSource) &&
      /setGate[\s\S]{0,600}?this\.pending\s*=\s*\[\]/.test(voiceIoSource),
    'setGate 存在且会清空 pending（开闸时不吐旧样本）',
    '见 voice-io.js 里 setGate 的说明：不清的话开闸瞬间会先发出关闸期间的旧音频',
  );

  // ③ app.js 必须在算闸门时把 VoiceIO 也一起切
  //
  // ⚠️ 只发 `start`/`stop` 给 sidecar 是**不够**的 —— 那只是让 sidecar
  // "不处理"，帧照样在推。必须两边都切。
  report(
    /applyCaptureGate[\s\S]{0,900}?audio\?\.setGate\?\.\(/.test(appSource),
    'applyCaptureGate 同时切 VoiceIO.setGate（不只通知 sidecar）',
    '只发 start/stop 给 sidecar 的话，帧照样在推，队列照样积 2 秒旧音频',
  );
}

// 按住说话监视器：**必须带心跳 + UTF-8 BOM**
//
// ## 为什么单独立一条（2026-10-10）
//
// 用户报「要聚焦宠物窗口按键才能用」。实测抓到的现场：
//
//     PID 17704   CPU 用户 3.14s
//     等 6 秒后   3.14s -> 3.14s（+0.00s）   <- 循环停了
//     probe-key.out 最后写入距今 233 秒
//
// **进程还在、但 `while` 循环不转了。** 而脚本原来只在 DOWN/UP 时写文件，
// 所以"循环停了"完全看不出来，Node 侧也没有任何检测 —— 按键永久失效、
// 不报错、不记日志。修法是让脚本每约 3 秒写一条 `TICK`，主进程据此续命、
// 超时则杀掉重启（见 `pushToTalk.ts` 的 `startWatchdog`）。
//
// 心跳一旦被删掉，看门狗会**每 8 秒误判一次并无限重启监视器** ——
// 比原来的 bug 还糟。所以这条检查必须有。
{
  const probeKey = path.join(here, '..', 'tools', 'probe-key.ps1');
  if (!existsSync(probeKey)) {
    report(false, 'probe-key.ps1 存在', `找不到 ${probeKey}`);
  } else {
    const raw = readFileSync(probeKey);
    const src = raw.toString('utf-8');

    report(
      /Write-Line\s*\(\s*"TICK/.test(src) || /"TICK \{0\}"/.test(src),
      'probe-key.ps1 写心跳（TICK），供主进程判断监视器是否僵住',
      '没有心跳 -> `pushToTalk.ts` 的看门狗无法区分"没按键"和"循环停了"，' +
        '会在僵住后永远不重启（就是用户报的那个 bug）',
    );

    /*
     * ⚠️ **必须是 UTF-8 BOM**。
     *
     * Windows PowerShell 5.1 对**没有 BOM** 的 `.ps1` 按 **ANSI** 解析，
     * 脚本里的中文注释会变成乱码；乱码里可能含引号/括号之类字符，
     * 把解析器带偏 —— 实测报的是完全无关的
     * `Unexpected token ')' in expression or statement`（行号还指向 param 块），
     * 极难归因。
     *
     * 这个坑踩过多次，而**编辑工具默认会剥掉 BOM**，所以每次改完都要检查。
     */
    const hasBom = raw.length >= 3 && raw[0] === 0xef && raw[1] === 0xbb && raw[2] === 0xbf;
    const hasNonAscii = raw.some((b) => b > 127);
    report(
      hasBom || !hasNonAscii,
      'probe-key.ps1 带 UTF-8 BOM（PowerShell 5.1 按 ANSI 读会乱码）',
      '中文注释变乱码后可能被解析器当成代码，报一个和真实原因无关的语法错误。' +
        '修法：写入时前置 EF BB BF 三个字节',
    );
  }
}

if (contractIssues > 0) {
  console.error(`\n渲染层契约检查未通过：${contractIssues} 项`);
  process.exit(1);
}
console.log('渲染层契约检查通过（DOM 元素 / preload 通道 / pet API / els 属性 / 采集闸门 / PTT 心跳）');

// 素材存在性顺带确认（不对就早点说，别等运行时才发现是占位宠物）
//
// ⚠️ 素材现在有**两种形态**，检查要跟着 `avatar.json` 的 `kind` 走：
//   · "video" -> 立绘是 webm 视频，靠 animations 里的路径清单加载；
//   · 否则    -> 序列帧贴图，靠 sheet.png。
// 原来这里无条件检查 sheet.png —— 换成视频素材之后它必然报"缺失"，
// 而那是**检查过时**、不是素材坏了。假警报会让人开始忽略检查输出，
// 那比没有检查更糟。
const avatarDir = path.join(rendererDir, 'assets', 'avatar', 'default');
if (existsSync(avatarDir)) {
  const manifestPath = path.join(avatarDir, 'avatar.json');
  console.log(`  ${existsSync(manifestPath) ? 'OK  ' : '缺失'} 素材 avatar.json`);
  if (existsSync(manifestPath)) {
    try {
      const manifest = JSON.parse(readFileSync(manifestPath, 'utf-8'));
      if (manifest.kind === 'video') {
        // 抽查一部分片源是否真的在盘上（清单有一百多条，全查没必要）
        const anims = manifest.animations ?? {};
        const names = Object.values(anims).flat().filter((v) => typeof v === 'string');
        const sample = names.slice(0, 6);
        const missing = sample.filter((rel) => !existsSync(path.join(avatarDir, `${rel}.webm`)));
        console.log(
          `  ${missing.length ? '缺失' : 'OK  '} 视频素材（抽查 ${sample.length} 个 / 共 ${names.length} 个片源）` +
            (missing.length ? `：${missing.join(', ')}` : ''),
        );
      } else {
        const p = path.join(avatarDir, 'sheet.png');
        console.log(`  ${existsSync(p) ? 'OK  ' : '缺失'} 素材 sheet.png`);
      }
    } catch (error) {
      console.log(`  警告 素材清单解析失败：${error.message}`);
    }
  }
} else {
  console.warn('  提示：未找到默认素材目录，将使用程序化占位宠物');
}
