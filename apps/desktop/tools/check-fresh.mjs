/**
 * 构建新鲜度门禁（`npm run check:fresh`）。
 *
 * ## 为什么需要它（真实事故）
 *
 * `npm test` 只构建 `.test-build/`（测试用的中间产物），**不重建 `dist/`**，
 * 而**应用运行时用的是 `dist/`**。
 *
 * 于是出现过一个很难发现的错位：
 *
 *     dist/main.cjs   最后一次构建  22:16
 *     源码改动（探头补偿）          22:30   ← 比构建新 14 分钟
 *
 * 我跑了 `npm test`，全绿 —— 但那证明的是**测试产物**正确，
 * 而用户运行的那份**根本不含这些改动**。用户的反馈因此是"一点没变"，
 * 而我误以为是逻辑不对，又去改逻辑，浪费了好几轮。
 *
 * 这个门禁把"dist 比源码旧"变成**明确的报错**，而不是让人去猜。
 *
 * ⚠️ 只检查**参与打包的源码**（`src/main/**`、`src/preload/**`、
 * `src/renderer/**` 里会被 esbuild 处理的），不检查测试文件 ——
 * 测试文件改了不需要重建 dist。
 */

import { readFileSync, existsSync, readdirSync, statSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.join(here, '..');
const rel = (p) => path.relative(root, p).replace(/\\/g, '/');

/** 需要重建 dist 的源码目录 */
const SOURCE_DIRS = [path.join(root, 'src', 'main'), path.join(root, 'src', 'preload')];

/** dist 产物 -> 它由哪些源码目录产出 */
const ARTIFACTS = [
  { file: path.join(root, 'dist', 'main.cjs'), sources: [path.join(root, 'src', 'main')] },
  { file: path.join(root, 'dist', 'preload.cjs'), sources: [path.join(root, 'src', 'preload')] },
  /*
   * ⚠️ 菜单窗口的 preload 是**第二个**从 `src/preload` 打出来的产物
   * （2026-10-14 加了自绘菜单）。不列在这里的话，改了 `src/preload/menu.ts`
   * 而忘了 `build:preload:menu`，运行的就是旧代码 —— 而症状是
   * 「菜单能弹出来、点了没反应」这种最难查的一类（代码对不对都对不上）。
   *
   * ⚠️ 只列"已经在构建链里"的产物：还没构建过时它会跳过（见下面），
   * 不会因为某个可选产物不存在就报错。
   */
  { file: path.join(root, 'dist', 'menu-preload.cjs'), sources: [path.join(root, 'src', 'preload')] },
  /*
   * 🔴 `src/renderer/sprite.js` —— **它本身也是 esbuild 产物**（2026-10-15 补）。
   *
   * ## 为什么必须在这里
   *
   * `build:renderer` 干的活是：
   *
   * ```
   * esbuild src/renderer/sprite.ts --outfile=src/renderer/sprite.js
   * ```
   *
   * ⚠️ **产物落在 `src/` 里，不是 `dist/`** —— 所以它长得像源码、实际是产物。
   * 这是这个项目里唯一一个这样的文件，最容易搞混：
   *
   * ```
   * sprite.ts   源头（手写）      2026-10-04
   * sprite.js   **产物**（esbuild）每次 build:renderer 覆盖
   * ```
   *
   * ## 漏掉它的两个后果（都是真实风险）
   *
   * ① **改了 `sprite.ts` 忘了重建** -> 运行的是旧的 `sprite.js`，
   *    而 `npm test` 全绿（它不查这个产物）。症状是"改了没反应"。
   * ② **手改了 `sprite.js`** -> 下次 `build:renderer` **静默覆盖**，
   *    改动凭空消失，而且没有任何提示。
   *
   * ⚠️ 这个门禁只能拦住 ①（时间戳比对）。② 拦不住 ——
   * 所以注释在这里说明白：**别手改 `sprite.js`，改 `sprite.ts`**。
   *
   * （2026-10-15 实测确认：`sprite.js` 曾被手改过，重建后只有
   * esbuild 的源路径注释不同（`// src/...` vs `// apps/desktop/src/...`），
   * 功能代码没丢 —— 但那是运气好，不是保证。）
   */
  {
    file: path.join(root, 'src', 'renderer', 'sprite.js'),
    sources: [path.join(root, 'src', 'renderer', 'sprite.ts')],
  },
];

function newestMtime(dir, exts = ['.ts', '.js', '.mjs']) {
  let newest = 0;
  let newestFile = '';
  if (!existsSync(dir)) return { newest, newestFile };
  /*
   * ⚠️ 允许传**文件**（2026-10-15）。
   *
   * 原来只处理目录 —— 而 `sprite.js` 那个产物的源头是**单个文件**
   * `src/renderer/sprite.ts`（不是一个目录）。传文件进去时
   * `readdirSync` 会抛 ENOTDIR。
   *
   * ⚠️ 而且传文件时**不看扩展名过滤**：调用方点名要的就是那个文件。
   */
  try {
    if (statSync(dir).isFile()) {
      return { newest: statSync(dir).mtimeMs, newestFile: dir };
    }
  } catch {
    return { newest, newestFile };
  }
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      const sub = newestMtime(full, exts);
      if (sub.newest > newest) {
        newest = sub.newest;
        newestFile = sub.newestFile;
      }
    } else if (exts.some((e) => entry.name.endsWith(e))) {
      const m = statSync(full).mtimeMs;
      if (m > newest) {
        newest = m;
        newestFile = full;
      }
    }
  }
  return { newest, newestFile };
}

let failed = 0;

console.log('='.repeat(78));
console.log('构建新鲜度门禁（dist 是否比源码旧）');
console.log('='.repeat(78));
console.log('');

for (const { file, sources } of ARTIFACTS) {
  if (!existsSync(file)) {
    // 还没构建过时不算失败（比如刚 clone）—— 但要说出来
    console.log(`  跳过 ${rel(file)}：还没构建过`);
    continue;
  }
  const artifactTime = statSync(file).mtimeMs;
  let staleSource = null;
  let staleTime = 0;
  for (const dir of sources) {
    const { newest, newestFile } = newestMtime(dir);
    if (newest > staleTime) {
      staleTime = newest;
      staleSource = newestFile;
    }
  }

  if (staleSource && staleTime > artifactTime) {
    failed += 1;
    /*
     * ⚠️ 用**秒**而不是分钟（2026-10-15）。
     *
     * 踩过：验证这个门禁时我 	ouch 了源文件，它报「比源码旧 **0 分钟**」——
     * 差值只有几百毫秒，四舍五入成 0，看起来像门禁坏了。
     * 秒数在刚改完就构建这种真实场景里也更有用。
     */
    const seconds = Math.round((staleTime - artifactTime) / 1000);
    console.log(
      `  FAIL ${rel(file)} 比源码旧 ${seconds} 秒\n` +
        `       产物 ${new Date(artifactTime).toLocaleTimeString()}\n` +
        `       源码 ${rel(staleSource)}  ${new Date(staleTime).toLocaleTimeString()}\n` +
        `       -> 请跑 \`npm run build\`，否则运行的是旧代码`,
    );
  } else {
    console.log(`  OK   ${rel(file)} 是最新的`);
  }
}

console.log('');
if (failed) {
  console.log(`未通过：${failed} 个产物过期 —— 应用运行的是旧代码`);
  process.exit(1);
}
console.log('dist 与源码一致 ✅');
process.exit(0);
