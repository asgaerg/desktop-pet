/**
 * 重复声明 / 语法 门禁（`npm run check:dupes`）。
 *
 * ## 为什么需要它（真实事故）
 *
 * 排查探头动画的几轮里，同一个坑踩了**三次**，而现有门禁**一个都没抓到** ——
 * 它们都是"按模式扫源码"，不解析语义：
 *
 *   1. `applyPeekCompensation` 被声明了**两次** -> `SyntaxError`
 *      （`node --check` 才报，所有测试全绿）
 *   2. `.pet` 里 `transition` 写了**两次** -> 后一条静默覆盖前一条
 *      （我加的 `--peek-rotate-ms` 因此从未生效，白排查一个多小时）
 *   3. `.pet` 里 `transition` 又写两次（第二次犯，同一个位置）
 *
 * 这类问题**没有任何运行时报错**，只是"改了没效果"或"整个文件解析失败"，
 * 靠人眼 review 很容易漏。所以固化成门禁。
 *
 * ## 三道检查
 *
 *   A. **JS 语法**：对每个渲染层/主进程源码跑 `vm.Script` 纯语法编译。
 *      （`check-renderer.mjs` 也编译渲染层，但它会"剥掉 import/export 再编译"，
 *      重复函数声明那种错误恰好在剥之前就被它放过了 —— 这里做得更狠。）
 *   B. **CSS 同一选择器内重复属性**：同一块里写两次同名属性，后面的覆盖前面的。
 *   C. **JS/TS 顶层重复声明**：同一文件里 `function x` / `const x` / `let x` 重名。
 */

import { readFileSync, existsSync, readdirSync, statSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import vm from 'node:vm';

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.join(here, '..');

let failed = 0;
const fail = (msg) => {
  failed += 1;
  console.log(`  FAIL ${msg}`);
};
const pass = (msg) => console.log(`  OK   ${msg}`);

/** 递归收集指定扩展名的文件（跳过资源/node_modules/构建产物） */
function collect(dir, exts, out = []) {
  if (!existsSync(dir)) return out;
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    if (['assets', 'node_modules', 'dist', '.test-build'].includes(entry.name)) continue;
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) collect(full, exts, out);
    else if (exts.some((e) => entry.name.endsWith(e))) out.push(full);
  }
  return out;
}

const rel = (p) => path.relative(root, p).replace(/\\/g, '/');

/* ---------------------------------------------------------------- A. JS 语法 */

function checkSyntax() {
  console.log('\n[JS 语法]');
  // 渲染层是直接喂给 Chromium 的，主进程是 CommonJS/ESM —— 两类都编一遍
  const files = [
    ...collect(path.join(root, 'src', 'renderer'), ['.js', '.mjs']),
    ...collect(path.join(root, 'src', 'main'), ['.ts']).filter(() => false), // TS 由 tsc 管
    ...collect(path.join(root, 'tools'), ['.mjs']),
  ];

  let bad = 0;
  for (const file of files) {
    const code = readFileSync(file, 'utf8');
    /*
     * ⚠️ 判据不能只看 `import|export` —— 带**顶层 await** 的文件也是模块，
     * 但没有 import（实测 `tools/debug-snapshot.mjs` 就是这么误报的：
     * 被当成脚本编译，于是 "await is only valid in async functions"）。
     * `.mjs` 后缀本身就是 ESM 的确定标志。
     */
    const isModule = file.endsWith('.mjs') || /^\s*(import|export)\s/m.test(code);
    try {
      if (isModule) {
        // ESM：用 SourceTextModule 编译（纯语法，不执行）
        if (typeof vm.SourceTextModule === 'function') {
          new vm.SourceTextModule(code, { identifier: file });
        } else {
          // 退路：剥掉 import/export 语句后再当脚本编译。
          // ⚠️ 退路会放过"重复函数声明"（剥完仍是脚本），所以只在没有
          // SourceTextModule 时才用 —— 下面 C 段会独立兜住重复声明。
          const stripped = code
            .replace(/^\s*import\s[^;]*;?$/gm, '')
            .replace(/^\s*export\s+(default\s+)?/gm, '');
          new vm.Script(stripped, { filename: file });
        }
      } else {
        new vm.Script(code, { filename: file });
      }
    } catch (error) {
      bad += 1;
      fail(`语法 ${rel(file)}：${error.message.split('\n')[0]}`);
    }
  }
  if (!bad) pass(`语法编译通过：${files.length} 个文件`);
}

/* ------------------------------------------------- B. CSS 重复属性 */

/** 去掉 CSS 注释，避免注释里的 `prop:` 被当成声明 */
const stripCssComments = (css) => css.replace(/\/\*[\s\S]*?\*\//g, '');

/**
 * CSS 注释配对检查（2026-10-07 新增）。
 *
 * ## ⚠️ 为什么必须单独查这个
 *
 * **CSS 注释不闭合不会报错**：浏览器/Chromium 会把从注释开始符到下一个
 * 注释结束符之间的东西全当注释吃掉，规则**静默失效**。构建门禁原来也照样
 * 通过（`checkCssDuplicates` 只查"同一选择器内重复属性"）。
 *
 * 实测后果（真实事故）：`bubble.css` 里 `.bubble.hidden { display: none }`
 * 被一个多余的注释开始符吃掉了 —— 于是气泡**隐藏时只剩 `opacity: 0`**，
 * 元素仍有尺寸，宠物旁边一直留着一个约 33x17 的深色小盒子
 * （用户报的"宠物对话完上面这个东西不知道哪里来的"）。
 *
 * 顺手也查"多余的注释结束符"：它会把**下一条**规则的注释开始符吃掉，
 * 同样是连锁的静默失效。
 *
 * ⚠️ **写这个检查时我自己踩了同一个坑**：在 JSDoc 里直接写注释符号本身，
 * 结果那几个字面量被解析成真结束符，把注释截断在中间、整个文件语法报错
 * （11 个 `.mjs` 全被误判成语法失败）。所以下面一律用文字描述：
 * 「注释开始符」「注释结束符」。
 */
function checkCssComments() {  console.log('\n[CSS 注释配对]');
  const files = collect(path.join(root, 'src'), ['.css']);
  if (!files.length) {
    console.log('  提示：没找到 CSS');
    return;
  }

  let bad = 0;
  for (const file of files) {
    const lines = readFileSync(file, 'utf8').split(/\r?\n/);
    let depth = 0;
    const problems = [];

    for (let i = 0; i < lines.length; i += 1) {
      const line = lines[i];
      let j = 0;
      while (j < line.length) {
        if (line.startsWith('/*', j)) {
          depth += 1;
          j += 2;
        } else if (line.startsWith('*/', j)) {
          depth -= 1;
          if (depth < 0) {
            problems.push(`第 ${i + 1} 行有**多余的**注释结束符`);
            depth = 0; // 复位，继续找后面的问题
          }
          j += 2;
        } else {
          j += 1;
        }
      }
    }
    if (depth > 0) {
      problems.push(`文件结束时还有 ${depth} 个注释未闭合`);
    }

    const rel = path.relative(root, file);
    if (problems.length) {
      bad += problems.length;
      for (const p of problems) console.log(`  失败 ${rel}：${p}`);
    }
  }

  if (!bad) {
    console.log(`  通过：${files.length} 个 CSS 文件的注释都配对`);
  } else {
    console.log(`  提示：注释没配对时，被它包住的规则会**静默失效**（不报错）`);
    process.exitCode = 1;
  }
}

/**
 * 检查"尺寸属性和伪元素写进了同一个选择器组"（2026-10-07 新增）。
 *
 * ## ⚠️ 为什么这是个大坑
 *
 * `width` / `height` 在**伪元素**上和**宿主元素**上语义完全不同：
 *
 *     .bubble::-webkit-scrollbar { width: 8px }   -> 滚动条多粗
 *     .bubble { width: 8px }                      -> 气泡自己只有 8px 宽
 *
 * 一旦写成同一个选择器组，尺寸就会**同时作用到宿主元素**上。
 * 实测事故（真实）：
 *
 *     .bubble,
 *     .bubble::-webkit-scrollbar { width: 0; height: 0; }
 *
 * 气泡本体被压成 `height: 17.6px`，而文字有 60px —— 溢出后被
 * `body { overflow: hidden }` 切掉，用户看到的是"**一半都被剪切了**"。
 *
 * 这个 bug **不报错、不过任何现有门禁**，只能靠肉眼在实机看出来。
 *
 * ## 判据
 *
 * 选择器组里既有"带伪元素的选择器"、又有"不带伪元素的"，
 * 且声明里出现 `width`/`height`/`min-*`/`max-*`/`padding`/`margin` 之一
 * —— 就报出来（这类属性在两边的语义都不同）。
 */
function checkCssPseudoGroups() {
  console.log('\n[CSS 尺寸属性与伪元素共用选择器组]');
  const files = collect(path.join(root, 'src'), ['.css']);
  if (!files.length) {
    console.log('  提示：没找到 CSS');
    return;
  }

  /** 这些属性在"宿主元素"和"伪元素"上的语义不同，不能共用选择器组 */
  const sizeProps = [
    'width', 'height', 'min-width', 'min-height', 'max-width', 'max-height',
    'padding', 'padding-top', 'padding-bottom', 'padding-left', 'padding-right',
    'margin', 'margin-top', 'margin-bottom', 'margin-left', 'margin-right',
  ];

  let bad = 0;
  for (const file of files) {
    const css = stripCssComments(readFileSync(file, 'utf8'));
    const rel = path.relative(root, file);
    // 极简解析：选择器组 { 声明 }。CSS 里没有嵌套，够用。
    const re = /([^{}]+)\{([^{}]*)\}/g;
    let m;
    while ((m = re.exec(css)) !== null) {
      const selectorGroup = m[1].replace(/\s+/g, ' ').trim();
      const decls = m[2];
      if (!selectorGroup || selectorGroup.startsWith('@')) continue;
      const parts = selectorGroup.split(',').map((s) => s.trim()).filter(Boolean);
      if (parts.length < 2) continue;
      const hasPseudo = parts.some((s) => s.includes('::'));
      const hasPlain = parts.some((s) => !s.includes('::'));
      if (!hasPseudo || !hasPlain) continue;
      // 声明里有没有尺寸类属性
      const hit = sizeProps.filter((p) =>
        new RegExp(`(^|[;{\\s])${p}\\s*:`, 'i').test(decls),
      );
      if (hit.length) {
        bad += 1;
        console.log(`  失败 ${rel}：`);
        console.log(`       选择器组 ${selectorGroup}`);
        console.log(`       含尺寸属性 ${hit.join(', ')} —— 会同时作用到**宿主元素**上`);
      }
    }
  }

  if (!bad) {
    console.log('  通过：没有把尺寸属性和伪元素写进同一个选择器组');
  } else {
    console.log('  提示：把 `::-webkit-scrollbar` 那类规则**单独写一条**，不要和宿主元素并列');
    process.exitCode = 1;
  }
}

function checkCssDuplicates() {
  console.log('\n[CSS 同一选择器内重复属性]');
  const files = collect(path.join(root, 'src'), ['.css']);
  if (!files.length) {
    console.log('  提示：没找到 CSS');
    return;
  }

  let bad = 0;
  for (const file of files) {
    const css = stripCssComments(readFileSync(file, 'utf8'));
    const lines = css.split(/\r?\n/);

    let selector = null;
    let depth = 0;
    const seen = new Map(); // 属性名 -> 首次出现行号
    let sawDecl = false;
    /*
     * ⚠️ `@keyframes` 要特殊对待：它内部是 `0% { ... }` / `100% { ... }`
     * 这样的**多个独立帧**，同名属性在不同帧里重复是**正常且必须**的。
     * 不排除的话会误报（实测：`@keyframes bounce` 的 transform/opacity 被误报）。
     */
    let inKeyframes = 0;

    const flush = (endLine) => {
      if (selector && seen.size) {
        for (const [prop, firstLine] of seen) {
          // 只报"同一块里出现两次"的（seen 里记的是首次，重复时才 push）
          if (Array.isArray(firstLine)) {
            bad += 1;
            fail(
              `${rel(file)}：选择器 \`${selector}\` 内 \`${prop}\` 重复声明` +
                `（L${firstLine[0]} 与 L${firstLine[1]}）—— 后一条会静默覆盖前一条`,
            );
          }
        }
      }
      seen.clear();
      sawDecl = false;
      void endLine;
    };

    for (let i = 0; i < lines.length; i += 1) {
      const raw = lines[i];
      const line = raw.trim();
      if (!line) continue;

      // 选择器行：以 { 结尾；支持一行多个 { 的紧凑写法
      if (line.endsWith('{')) {
        if (depth === 0) {
          const sel = line.slice(0, -1).trim();
          if (sel.startsWith('@keyframes')) {
            inKeyframes += 1; // 整块跳过，见 inKeyframes 的说明
          } else {
            selector = sel;
            seen.clear();
          }
        }
        depth += line.split('{').length - 1;
        depth -= line.split('}').length - 1;
        if (inKeyframes && depth <= 0) {
          inKeyframes = 0;
          selector = null;
        }
        continue;
      }

      const closeCount = (line.match(/\}/g) || []).length;
      if (closeCount && !line.includes(':')) {
        depth -= closeCount;
        if (depth <= 0) {
          if (!inKeyframes) flush(i);
          selector = null;
          depth = 0;
          inKeyframes = 0;
        }
        continue;
      }

      // 属性声明（关键帧块内部不算 —— 那里的重复是正常的）
      const decl = line.match(/^([a-zA-Z-]+)\s*:/);
      if (decl && depth > 0 && !inKeyframes) {
        sawDecl = true;
        const prop = decl[1].toLowerCase();
        const prev = seen.get(prop);
        if (Array.isArray(prev)) prev[1] = i + 1;
        else if (typeof prev === 'number') seen.set(prop, [prev, i + 1]);
        else seen.set(prop, i + 1);
      }
    }
    flush(lines.length);
  }
  if (!bad) pass('未发现同一选择器内重复属性');
}

/* ------------------------------------------------- C. JS/TS 顶层重复声明 */

function checkJsDuplicates() {
  console.log('\n[JS/TS 顶层重复声明]');
  const files = [
    ...collect(path.join(root, 'src', 'renderer'), ['.js', '.mjs']),
    ...collect(path.join(root, 'src', 'main'), ['.ts']),
    ...collect(path.join(root, 'tools'), ['.mjs']),
  ];

  let bad = 0;
  for (const file of files) {
    const code = readFileSync(file, 'utf8');
    const lines = code.split(/\r?\n/);
    const decls = new Map(); // 名字 -> [行号...]

    for (let i = 0; i < lines.length; i += 1) {
      if (lines[i].includes('*')) continue; // 跳过注释行（含 JSDoc）
      // ⚠️ 只匹配**行首**（列 0）的声明 —— 缩进的都在函数/块里，
      // 可能合法（不同作用域同名），匹配了会误报。
      const m =
        lines[i].match(/^(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)/) ||
        lines[i].match(/^(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=/);
      if (!m) continue;
      const name = m[1];
      const arr = decls.get(name) || [];
      arr.push(i + 1);
      decls.set(name, arr);
    }

    for (const [name, where] of decls) {
      if (where.length > 1) {
        bad += 1;
        fail(`${rel(file)}：顶层 \`${name}\` 声明了 ${where.length} 次（L${where.join(', L')}）`);
      }
    }
  }
  if (!bad) pass('未发现顶层重复声明');
}

/* ---------------------------------------------------------------- 主流程 */

console.log('='.repeat(78));
console.log('重复声明 / 语法 门禁');
console.log('='.repeat(78));

checkSyntax();
checkCssComments();
checkCssPseudoGroups();
checkCssDuplicates();
checkJsDuplicates();

console.log('');
if (failed) {
  console.log(`未通过：${failed} 项`);
  process.exit(1);
}
console.log('全部通过 ✅');
process.exit(0);
