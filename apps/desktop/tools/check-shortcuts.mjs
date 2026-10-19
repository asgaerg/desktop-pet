/**
 * 快捷键一致性检查（`npm run check:shortcuts`）。
 *
 * 防的是这一类问题：**界面上承诺了一个不存在的快捷键。**
 *
 * 真实事故：语音按钮的提示写着「开启语音（Ctrl+Alt+M）」，而 `M` 从未在
 * `registerShortcuts` 里注册过。用户按下去毫无反应，也不会报错 ——
 * 界面上看着一切正常，功能却不存在。这类"说了不做"比功能缺失更糟，
 * 因为它让用户以为是自己操作错了，或者程序坏了。
 *
 * 检查方式：`src/main/shortcuts.ts` 是快捷键的**唯一定义源**，注册从它读。
 * 这里：
 *   ① 真品能被解析出来（否则说明定义源结构变了，检查本身失效）；
 *   ② 每一个登记项都能在界面提示里找到对应字样 —— 免得"实现了但没告诉用户"；
 *   ③ 界面上**不出现**任何未登记（含已撤销）的快捷键字样 —— 免得又写回假的；
 *   ④ 注册代码确实从 SHORTCUTS 读，而不是自己另写一份字符串。
 */

import { readFileSync, readdirSync, existsSync } from 'node:fs';
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
const buildDir = path.join(desktopDir, '.test-build');
const srcDir = path.join(desktopDir, 'src');

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

console.log('快捷键一致性检查\n');

// 真品：编译产物里的定义源（跑编译产物而不是源码，连"构建后还对不对"一起验证）
const shortcutsFile = path.join(buildDir, 'shortcuts.cjs');
if (!existsSync(shortcutsFile)) {
  console.error(`缺少 ${shortcutsFile}\n请先运行：npm run build:testtargets`);
  process.exit(1);
}
const { SHORTCUTS, RETIRED_ACCELERATORS } = createRequire(import.meta.url)(shortcutsFile);

// ① 定义源可解析
check(
  '快捷键定义源可解析',
  Array.isArray(SHORTCUTS) && SHORTCUTS.length > 0,
  `SHORTCUTS = ${JSON.stringify(SHORTCUTS)}`,
);

// 收集界面上的所有文案（渲染层 HTML/JS + 主进程的托盘菜单）
const rendererDir = path.join(srcDir, 'renderer');
const uiFiles = [
  ...readdirSync(rendererDir)
    .filter((name) => /\.(html|js)$/.test(name))
    .map((name) => path.join(rendererDir, name)),
  path.join(srcDir, 'main', 'tray.ts'),
].filter((file) => existsSync(file));

const uiText = uiFiles.map((file) => readFileSync(file, 'utf-8')).join('\n');
const uiTextWithoutComments = uiFiles
  .map((file) =>
    readFileSync(file, 'utf-8')
      // 注释里可以提到已撤销的快捷键（那是解释，不是承诺）
      .replace(/<!--[\s\S]*?-->/g, '')
      .replace(/\/\*[\s\S]*?\*\//g, '')
      .replace(/^\s*\/\/.*$/gm, ''),
  )
  .join('\n');

// ② 每个登记的快捷键都要在界面文案里出现
for (const spec of SHORTCUTS) {
  const display = spec.display ?? spec.accelerator;
  check(
    `界面提示里提到了 ${display}（${spec.description}）`,
    uiText.includes(display),
    '登记了快捷键但没告诉用户 —— 用户不会知道它存在',
  );
}

// ③ 界面上不能出现未登记/已撤销的快捷键字样，否则就是"说了不做"
{
  const mentioned = new Set(
    [...uiTextWithoutComments.matchAll(/(?:Ctrl|CommandOrControl|Cmd|Alt|Shift)[+\-](?:Alt|Ctrl|Shift|Command|[A-Z0-9])[+\-]?[A-Z0-9]?/g)].map(
      (m) => m[0],
    ),
  );
  const registered = new Set(
    SHORTCUTS.flatMap((spec) => [spec.accelerator, spec.display].filter(Boolean)),
  );
  const unexplained = [...mentioned].filter((text) => {
    // 归一化后比对：Ctrl 等价于 CommandOrControl
    const normalized = text.replace(/CommandOrControl/g, 'Ctrl').replace(/Cmd/g, 'Ctrl');
    return ![...registered].some(
      (known) => known.replace(/CommandOrControl/g, 'Ctrl').replace(/Cmd/g, 'Ctrl') === normalized,
    );
  });
  check(
    '界面文案里没有未登记的快捷键',
    unexplained.length === 0,
    unexplained.length
      ? `出现了没注册的：${unexplained.join(', ')}（按下去不会有反应）` +
        (RETIRED_ACCELERATORS.length ? `\n       已撤销的：${RETIRED_ACCELERATORS.join(', ')}` : '')
      : '所有出现的快捷键都已登记',
  );
}

// ④ 已撤销的快捷键不能出现在界面文案里
{
  // 去重：RETIRED_ACCELERATORS 里 Ctrl / CommandOrControl 两种写法归一化后是同一个键，
  // 不去重就会把同一条检查跑两遍（看起来像两项，其实是一项）。
  const retired = [
    ...new Set(RETIRED_ACCELERATORS.map((item) => item.replace(/CommandOrControl/g, 'Ctrl'))),
  ];
  const mentioned = [
    ...new Set(
      [...uiTextWithoutComments.matchAll(/(?:Ctrl|CommandOrControl)[+\-]Alt[+\-][A-Z0-9]/g)].map(
        (m) => m[0].replace(/CommandOrControl/g, 'Ctrl'),
      ),
    ),
  ];
  for (const item of retired) {
    check(
      `已撤销的 ${item} 没有出现在界面文案里`,
      !mentioned.includes(item),
      '它不会被注册，写出来就是假承诺',
    );
  }
}

// ⑤ 注册代码必须从 SHORTCUTS 读，而不是自己另写一份字符串
{
  const indexSource = readFileSync(path.join(srcDir, 'main', 'index.ts'), 'utf-8');
  const registerBody = /function registerShortcuts\(\)[\s\S]*?\n}/.exec(indexSource)?.[0] ?? '';
  check(
    '注册代码从 SHORTCUTS 读取（不另写字符串）',
    registerBody.includes('SHORTCUTS') && !/globalShortcut\.register\(\s*'/.test(registerBody),
    'registerShortcuts 里出现了硬编码的 accelerator —— 那就会有第二个真相源',
  );
}

console.log('');
if (failures.length) {
  console.error(`快捷键一致性检查未通过：${failures.length} 项 / ${passed + failures.length} 项`);
  for (const item of failures) console.error(`  - ${item}`);
  process.exit(1);
}
console.log(`快捷键一致性检查通过：${passed}/${passed} 项`);
