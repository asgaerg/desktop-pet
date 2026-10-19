/**
 * 列出**所有能当全局键的 VK** —— 给用户挑键用。
 *
 * ## 为什么要扫一遍
 *
 * `vkToAccelerator()` 是一张**有限的表** —— 表里没有的键会返回 `null`，
 * 而 `GlobalShortcutPtt` 那时会明确报"不能用作全局热键"。
 * 所以"哪些键能用"是**代码事实**，不该靠猜或翻文档。
 *
 * ## 顺带给出建议
 *
 * 全局键会被**系统级吃掉**（注册成功后，那个键不再传给任何程序）。
 * 所以选键的硬约束是：**平时打字/游戏都不会按到**。
 */

const { join, dirname } = require('node:path');
const { vkToAccelerator, vkName } = require(
  join(__dirname, '..', '.test-build', 'pushToTalk.cjs'),
);

const ok = [];
for (let vk = 1; vk <= 0xff; vk += 1) {
  const a = vkToAccelerator(vk);
  if (a) ok.push({ vk, a, n: vkName(vk) });
}

console.log(`  能当全局键的 VK 共 ${ok.length} 个：`);
console.log('');

const groups = [
  ['功能键（推荐）', (v) => v.vk >= 0x70 && v.vk <= 0x87],
  ['小键盘', (v) => v.vk >= 0x60 && v.vk <= 0x6f],
  [
    '编辑/导航',
    (v) =>
      [0x20, 0x0d, 0x1b, 0x09, 0x08, 0x2d, 0x2e, 0x24, 0x23, 0x21, 0x22, 0x25, 0x26, 0x27, 0x28].includes(
        v.vk,
      ),
  ],
  ['字母/数字（不要用）', (v) => v.vk >= 0x30 && v.vk <= 0x5a],
  ['符号/其他', (v) => true],
];

const shown = new Set();
for (const [title, pred] of groups) {
  const items = ok.filter((v) => !shown.has(v.vk) && pred(v));
  items.forEach((v) => shown.add(v.vk));
  if (!items.length) continue;
  console.log(`  【${title}】`);
  console.log('    ' + items.map((v) => `${v.n}=${v.a}(VK ${v.vk})`).join('  '));
  console.log('');
}

/*
 * 推荐清单：把"平时不会按到 + 有独立实体键"的挑出来。
 *
 * ⚠️ ScrollLock 不在这个列表里 —— 因为 `vkToAccelerator` 的表里没有它
 * （`0x91`），扫出来的结果就是它转不了 accelerator。这正是"必须扫一遍
 * 而不是凭印象推荐"的原因：我原以为它能用。
 */
console.log('  ═══ 推荐（平时打字/游戏都不会按到，而且有独立实体键）═══');
const RECOMMEND = [
  [0x78, 'F9', '最常见的选择，笔记本上通常有'],
  [0x77, 'F8', '同样好用'],
  [0x7a, 'F11', '浏览器全屏键，但游戏里很少用'],
  [0x7b, 'F12', '开发者工具键 —— 你可能不想占用它'],
  [0x79, 'F10', ''],
  [0x76, 'F7', ''],
];
for (const [vk, label, note] of RECOMMEND) {
  const a = vkToAccelerator(vk);
  const mark = a ? '✅' : '❌';
  console.log(`    ${mark} ${label.padEnd(5)} VK ${String(vk).padEnd(4)} -> ${String(a).padEnd(6)} ${note}`);
}

console.log('');
console.log('  ═══ 千万别选的 ═══');
console.log('    ✖ 单独数字/字母（如 2、A）—— 全局键会被系统吃掉，以后打不出这个字符');
console.log('    ✖ 空格 / Enter / Esc —— 会打乱所有程序的正常输入');
console.log('    ✖ 单独的 Ctrl / Alt / Shift —— accelerator 表达不了（会注册失败）');
console.log('    ✖ 鼠标侧键 —— 实测 accelerator 语法里没有鼠标键（抛 conversion failed）');
