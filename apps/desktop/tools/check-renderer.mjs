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

// --------------------------------------------------------------------------- //
// 「查看图片」按钮：蓝底可读性（2026-11-15 用户要求）
//
// 用户原话：「查看图片的ui不是很明显，深色和浅色状态都改成比较显眼的
//   颜色吧，深色主题下蓝色不错，浅色也蓝色算了」
//
// 为什么放进契约检查：这类问题**语法完全正确、运行不报错**，
// 只有"看起来看不清"这一个症状，而开发环境又没法目视验证。
// 静态算对比度能兜住底线。
//
// 实测踩到的真问题：第一版写「蓝底白字」，浅色 4.55:1 刚过线，
// 但深色主题的 accent 是**亮蓝** #6aa9ff —— 白字在上面只有 **2.40:1**，
// 远低于 WCAG AA 的 4.5。所以深色下必须换成深色文字。
// --------------------------------------------------------------------------- //
{
  const memoryCss = readFileSync(path.join(rendererDir, 'memory.css'), 'utf-8');
  const memoryJs = readFileSync(path.join(rendererDir, 'memory.js'), 'utf-8');

  // JS 里得真的用上这个 class
  report(
    memoryJs.includes('btn-view-image'),
    '「查看图片」按钮带专属 class',
    '不加专属 class 就只能改 .btn.mini，会把同一行的「修正」「加为固定」也染色',
  );

  // WCAG 相对亮度 / 对比度
  const lin = (c) => (c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4);
  const lum = (hex) => {
    let h = hex.replace('#', '');
    if (h.length === 3) h = [...h].map((x) => x + x).join('');
    const [r, g, b] = [0, 2, 4].map((i) => lin(parseInt(h.slice(i, i + 2), 16) / 255));
    return 0.2126 * r + 0.7152 * g + 0.0722 * b;
  };
  const ratio = (a, b) => {
    const [hi, lo] = [lum(a), lum(b)].sort((x, y) => y - x);
    return (hi + 0.05) / (lo + 0.05);
  };

  const lightAccent = /:root\s*\{[^}]*?--accent:\s*(#[0-9a-fA-F]{6})/s.exec(memoryCss)?.[1];
  const darkAccent =
    /html\[data-theme='dark'\]\s*\{[^}]*?--accent:\s*(#[0-9a-fA-F]{6})/s.exec(memoryCss)?.[1];

  // 每个主题实际用的文字色：浅色取基础规则，深色取覆盖规则
  //
  // ⚠️ 两个坑都踩过：
  //    · 用 `([^{}]*)` 卡选择器 -> 被前面的**块注释**带偏（注释里有大括号）；
  //    · 用 `indexOf('{')` 找规则体 -> 找到的是**注释里的** `{`。
  //    现在用 `matchAll` 精确匹配"规则名 + 花括号"，再检查选择器里有没有 data-theme。
  const pickColor = (withDark) => {
    for (const m of memoryCss.matchAll(/^([^\n{}]*\.btn-view-image[^\n{}]*)\s*\{([^}]*)\}/gm)) {
      const selector = m[1].trim();
      const isDarkRule = selector.includes('data-theme');
      if (isDarkRule !== withDark) continue;
      // ⚠️ 要同时认**三位简写** `#fff` 和六位 `#ffffff` ——
      //    第一版只写 `{6}`，于是浅色那条（`color: #fff`）匹配不到、
      //    测出来是 `? 字`，误报成"对比度不足"。
      const cm = /color:\s*(#[0-9a-fA-F]{3,8})\b/.exec(m[2]);
      if (cm) return cm[1];
    }
    return null;
  };
  const baseColor = pickColor(false);
  const darkColor = pickColor(true) ?? baseColor;

  for (const [label, accent, textColor] of [
    ['浅色', lightAccent, baseColor],
    ['深色', darkAccent, darkColor],
  ]) {
    // AA 正文 4.5；UI 组件 3.0 勉强够，但按钮上有文字，按 4.5 要求
    const r = accent && textColor ? ratio(accent, textColor) : 0;
    report(
      r >= 4.5,
      `「查看图片」按钮${label}对比度 >= 4.5:1（${accent ?? '?'} 底 + ${textColor ?? '?'} 字）`,
      `实际 ${r.toFixed(2)}:1。蓝底 + 文字要能分清：亮蓝底配白字会看不清（实测 2.4:1），` +
        `深色主题下应改用深色文字`,
    );
  }
}

// --------------------------------------------------------------------------- //
// 表情包显示后要重置消失计时器（2026-11-16 修）
//
// 用户报：「现在的表情包也受到气泡消失的限制，
//          你看看现在好像2分钟结束也没消失」
//
// 根因：`showSticker()` 里写的是 `window.bubble.notifyShown()`，
// 而 preload 暴露的方法名是 **`reportShown`** —— 名字对不上。
// 外面套了 `typeof === 'function'` 守卫，所以**不报错、静默跳过**：
//
//     图显示出来了 ✓
//     尺寸上报了   ✓
//     消失计时器**没重置** ✗
//
// 于是计时器从「上一次文字」起算，表情包出来不算一次"新的显示"，
// 到点就跟着整个气泡一起消失。
//
// 这条守的是：**跨模块调用的方法名，拼错了不会报错**（因为都有守卫），
// 只能靠静态检查兜住。
// --------------------------------------------------------------------------- //
{
  const bubbleSrc = readFileSync(path.join(rendererDir, 'bubble.js'), 'utf-8');
  const bubblePreload = readFileSync(
    path.join(rendererDir, '..', 'preload', 'bubble.ts'),
    'utf-8',
  );

  // 去掉注释再查，避免说明文字里的名字被误判
  const stripComments = (s) =>
    s.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '');
  const codeOnly = stripComments(bubbleSrc);

  report(
    /reportShown\(\): void/.test(bubblePreload),
    'preload 暴露了 reportShown（表情包重置计时器要用）',
    '没有它，表情包显示后不会重置消失计时器',
  );

  report(
    !/\.notifyShown\s*\(/.test(codeOnly),
    '渲染层不调用不存在的 notifyShown',
    '这个名字不存在（真实名字是 reportShown），而外面有 typeof 守卫，' +
      '所以不会报错、只会静默失效 —— 正是这个 bug 的成因',
  );

  report(
    /const settle = \(\) => \{[\s\S]{0,900}?reportShown\(\);/.test(codeOnly),
    '表情包解码完会 reportShown（重置消失计时）',
    '不重置的话，计时器从上次文字起算，图刚出来不久就跟着消失',
  );
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
