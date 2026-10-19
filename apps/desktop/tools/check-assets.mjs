/**
 * 资源路径检查（`npm run check:assets`）。
 *
 * 为什么需要这道检查：透明窗的失败从外面看永远是"什么都看不见"。`loadFile`
 * 一旦失败，页面根本没加载，而窗口本身照样存在、`IsWindowVisible` 为真、
 * 位置尺寸都对 —— 没有任何自动化检查会发现异常。
 *
 * 这个坑真实发生过：`petWindow.ts` 的路径少算了一层 `src`，解析成
 * `apps/desktop/renderer/index.html`（不存在），于是**界面在整个 P0~P1 期间
 * 从未真正加载过**。当时 Python 自检、渲染层语法检查、契约检查、运行时测试
 * **全是绿的**，因为没有一个环节经过 loadFile。
 *
 * 关键设计：路径算式**从 `src/main/assets.ts` 里 import**，而不是在这里复制一份。
 * 早先的版本复制了算式，结果它验证的是"复制品能不能解析"，真品算错了它照样报通过 ——
 * 那种检查比没有更糟，因为它给的是虚假的安心。
 *
 * 测试跑的是 `assets.ts` 的**编译产物**（`.test-build/assets.cjs`），所以连
 * "构建后还对不对"也一并验证了。
 */

import { existsSync, readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';
const nodeMajor = Number(process.versions.node.split('.')[0]);
if (Number.isFinite(nodeMajor) && nodeMajor < 18) {
  console.error(`需要 Node 18+，当前是 ${process.versions.node}。请用较新的 Node 运行。`);
  process.exit(1);
}

const here = path.dirname(fileURLToPath(import.meta.url));
const desktopDir = path.join(here, '..');
const distDir = path.join(desktopDir, 'dist');
const buildDir = path.join(desktopDir, '.test-build');

let passed = 0;
const failures = [];
const check = (name, ok, detail = '') => {
  if (ok) {
    passed += 1;
    console.log(`  OK   ${name}`);
  } else {
    failures.push(`${name}${detail ? ` —— ${detail}` : ''}`);
    console.error(`  FAIL ${name}${detail ? `\n       ${detail}` : ''}`);
  }
};

console.log('资源路径检查\n');

// 用**真实的路径模块**（编译产物），不复制算式
const assetsFile = path.join(buildDir, 'assets.cjs');
if (!existsSync(assetsFile)) {
  console.error(`缺少 ${assetsFile}\n请先运行：npm run build:testtargets`);
  process.exit(1);
}
const {
  resolveRendererPage,
  resolvePreload,
  rendererDir,
  SETTINGS_PAGE_FILENAME,
  SETTINGS_PRELOAD_FILENAME,
  CHAT_PAGE_FILENAME,
  CHAT_PRELOAD_FILENAME,
  MENU_PAGE_FILENAME,
  MENU_PRELOAD_FILENAME,
} = createRequire(import.meta.url)(assetsFile);

/**
 * 检查一个页面：能否解析到文件、它引用的本地资源是否都在。
 *
 * 抽成函数是因为现在有**三个页面**（宠物 index.html、设置 settings.html、
 * 对话条 chat.html）。早先只检查了宠物页面，设置页面是后来加的 ——
 * 不补上的话，页面的路径或引用写错时不会有人发现，
 * 而它的失败表现同样是"点了没反应"。
 */
function checkPage(label, filename) {
  let pagePath = null;
  let resolveError = null;
  try {
    pagePath = resolveRendererPage(distDir, filename);
  } catch (error) {
    resolveError = error.message;
  }
  check(`${label}：能解析到页面文件`, pagePath !== null, resolveError ?? '返回了空路径');

  if (pagePath === null) return;

  const renderer = rendererDir(distDir);
  const html = readFileSync(pagePath, 'utf-8');
  // ⚠️ 必须锚定 `src="` / `href="`，不能写成 `<script[^>]+src="`。
  // 后者会**跨标签**匹配：`<meta http-equiv=... content="...">` 里的
  // `content="..."` 后面若跟着 `<script ... src="x.js">`，`[^>]+` 会一路吃过去，
  // 于是把 CSP 字符串里的 default-src 之类当成页面引用，
  // 报出"对话条页面：引用的 app.js 存在"这种**张冠李戴**的检查项（实测踩过）。
  const refs = [
    ...[...html.matchAll(/\ssrc="([^"]+)"/g)].map((m) => m[1]),
    ...[...html.matchAll(/\shref="([^"]+)"/g)].map((m) => m[1]),
  ].filter((ref) => !/^(https?:)?\/\//.test(ref));

  check(`${label}：至少引用了一个本地资源`, refs.length > 0, '正则可能失效');
  for (const ref of refs) {
    const target = path.join(renderer, ref);
    check(`${label}：引用的 ${ref} 存在`, existsSync(target), target);
  }

  // 页面引用的每个 .js 都要做 ESM 相对导入检查 —— 写错会让整个脚本不执行
  for (const ref of refs.filter((r) => r.endsWith('.js'))) {
    const scriptPath = path.join(renderer, ref);
    if (!existsSync(scriptPath)) continue;
    const source = readFileSync(scriptPath, 'utf-8');
    const imports = [...source.matchAll(/^\s*import\s[^'"]*['"]([^'"]+)['"]/gm)].map((m) => m[1]);
    for (const spec of imports) {
      if (!spec.startsWith('.')) continue;
      const target = path.resolve(renderer, spec);
      check(`${label}：${ref} 导入的 ${spec} 存在`, existsSync(target), target);
    }
  }
}

checkPage('宠物页面', 'index.html');
checkPage('设置页面', SETTINGS_PAGE_FILENAME);
// 对话条是第三个页面：输入框搬出去之后，它是**发消息的唯一入口**
checkPage('对话条页面', CHAT_PAGE_FILENAME);
/*
 * 右键菜单页面（2026-10-14 换成自绘窗口）。
 *
 * ⚠️ 它比别的窗口更依赖这道检查：菜单是**改设置 / 退出程序的唯一入口**
 * （宠物窗口刻意没有关闭按钮），而自绘窗口"页面加载失败"的表现是
 * 「右键一点反应都没有」—— 不报错、不弹窗，用户只会以为程序坏了。
 * 这里先确认页面与它引用的两个 CSS / 一个 JS 都在盘上。
 */
checkPage('菜单页面', MENU_PAGE_FILENAME);

// preload 产物：三份能力不同的 preload，都要存在
const preloadPath = resolvePreload(distDir);
check('主窗口 preload 产物存在', existsSync(preloadPath), preloadPath);
const settingsPreload = resolvePreload(distDir, SETTINGS_PRELOAD_FILENAME);
check('设置窗口 preload 产物存在', existsSync(settingsPreload), settingsPreload);
const chatPreload = resolvePreload(distDir, CHAT_PRELOAD_FILENAME);
check('对话条 preload 产物存在', existsSync(chatPreload), chatPreload);
// ⚠️ 菜单窗口那份（2026-10-14）。漏了它的表现是「菜单弹不出来」，
// 而主进程会**静默退回原生菜单** —— 界面还能用，只是完全不是设计稿的样子。
const menuPreload = resolvePreload(distDir, MENU_PRELOAD_FILENAME);
check('菜单窗口 preload 产物存在', existsSync(menuPreload), menuPreload);

// 立绘素材
check(
  '默认立绘素材存在',
  existsSync(path.join(rendererDir(distDir), 'assets', 'avatar', 'default', 'avatar.json')),
  'renderer/assets/avatar/default/avatar.json',
);

console.log('');
if (failures.length) {
  console.error(`资源路径检查未通过：${failures.length} 项 / ${passed + failures.length} 项`);
  for (const item of failures) console.error(`  - ${item}`);
  process.exit(1);
}
console.log(`资源路径检查通过：${passed}/${passed} 项`);
