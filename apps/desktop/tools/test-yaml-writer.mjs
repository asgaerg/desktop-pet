
/**
 * YAML 写入器的回归测试：**裸标量 + 注释** 不能被写坏。
 *
 * 见文件头（tools/add-yaml-writer-test.py）里对那个真 bug 的说明。
 */
import { writeFileSync, readFileSync, mkdtempSync, mkdirSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const here = dirname(fileURLToPath(import.meta.url));
const requireCjs = createRequire(import.meta.url);
const target = (name) => join(here, '..', '.test-build', name + '.cjs');
const { readDeepScalar, writeDeepScalar } = requireCjs(target('personaStore'));

let passed = 0;
const failures = [];
function check(name, ok, detail) {
  if (ok) { passed += 1; console.log(`  OK   ${name}`); }
  else { failures.push(`${name} —— ${detail}`); console.log(`  FAIL ${name} —— ${detail}`); }
}

/*
 * ⚠️ 临时目录**不能用 `os.tmpdir()`** —— 这个环境里 %TEMP% 不可写：
 *
 *     EPERM: operation not permitted, mkdtemp
 *       'C:\Users\tianyi\AppData\Local\Temp\yaml-test-XXXXXX'
 *
 * 项目早先就踩过这个坑（Electron 写 %APPDATA% 也是 EPERM）。
 * 所以放在项目内的 `.test-build/tmp/`。
 */
const tmpBase = join(here, '..', '.test-build', 'tmp');
mkdirSync(tmpBase, { recursive: true });
const root = mkdtempSync(join(tmpBase, 'yaml-test-'));
mkdirSync(join(root, 'config'), { recursive: true });
const file = join(root, 'config', 'persona.yaml');

const CLEAN = [
  'providers:',
  '  push_to_talk:',
  '    enabled: true',
  '    vk: 165                 # 右 Alt',
  '    global_vk: 120          # F9',
  '',
  '  tts:',
  "    provider: 'openai'         # 云端（引号串）",
  '    rate: 0',
  '    label: hello                 # 无空格的中间值',
  '',
].join('\n');
writeFileSync(file, CLEAN, 'utf8');

/* ① 写一个**别的**键：不能碰坏邻居那行的注释 */
writeDeepScalar(root, ['providers', 'push_to_talk', 'global_vk'], 50);
let lines = readFileSync(file, 'utf8').split('\n');
let vkLine = lines.find((l) => /^\s{4}vk:/.test(l)) ?? '';
check(
  '写别的键不会碰坏邻居那行的「值 + 空格 + 注释」',
  /^    vk: 165\s+# 右 Alt$/.test(vkLine),
  `实际：${JSON.stringify(vkLine)} —— 值和 # 粘住了会让整行变成字符串`,
);

/* ② 写目标键本身：值与 # 之间必须仍有空白 */
let gvLine = lines.find((l) => /^\s{4}global_vk:/.test(l)) ?? '';
check(
  '被改的那一行：值与 # 之间仍有空白',
  /^    global_vk: 50\s+# F9$/.test(gvLine),
  `实际：${JSON.stringify(gvLine)} —— 「50# F9」在 YAML 里是字符串 "50# F9"`,
);

/* ③ 读回来类型不变（这是"静默变未设置"的直接检测） */
const vkBack = readDeepScalar(root, ['providers', 'push_to_talk', 'vk'], true);
check(
  '读回来仍是数字 165（不是字符串）',
  vkBack === 165,
  `实际：${JSON.stringify(vkBack)} —— Number('165# …') 是 NaN，会让键静默变未设置`,
);
const gvBack = readDeepScalar(root, ['providers', 'push_to_talk', 'global_vk'], true);
check('读回来仍是数字 50', gvBack === 50, `实际：${JSON.stringify(gvBack)}`);

/* ④ 连写 5 次不累积损坏（旧版有"每保存一次追加一段注释"的事故） */
for (let i = 0; i < 5; i += 1) {
  writeDeepScalar(root, ['providers', 'push_to_talk', 'global_vk'], 120 + i);
  writeDeepScalar(root, ['providers', 'push_to_talk', 'vk'], 163);
}
lines = readFileSync(file, 'utf8').split('\n');
vkLine = lines.find((l) => /^\s{4}vk:/.test(l)) ?? '';
gvLine = lines.find((l) => /^\s{4}global_vk:/.test(l)) ?? '';
check(
  '连写 10 次后注释没被追加/重复',
  (vkLine.match(/#/g) ?? []).length === 1 && (gvLine.match(/#/g) ?? []).length === 1,
  `vk=${JSON.stringify(vkLine)} / global_vk=${JSON.stringify(gvLine)}`,
);
check(
  '连写 10 次后两个值都对',
  readDeepScalar(root, ['providers', 'push_to_talk', 'vk'], true) === 163 &&
    readDeepScalar(root, ['providers', 'push_to_talk', 'global_vk'], true) === 124,
  `vk=${readDeepScalar(root, ['providers', 'push_to_talk', 'vk'], true)} ` +
    `global_vk=${readDeepScalar(root, ['providers', 'push_to_talk', 'global_vk'], true)}`,
);

/* ⑤ 引号串 + 注释（这条路径本来就对，作为对照） */
writeDeepScalar(root, ['providers', 'tts', 'provider'], 'siliconflow');
lines = readFileSync(file, 'utf8').split('\n');
const provLine = lines.find((l) => /^\s{4}provider:/.test(l)) ?? '';
check(
  "引号串 + 注释也正常（写成 'siliconflow' 且注释还在）",
  /^    provider: 'siliconflow'\s+# 云端/.test(provLine) || /^    provider: 'siliconflow'/.test(provLine),
  `实际：${JSON.stringify(provLine)}`,
);

/* ⑥ 无空格的中间值（label: hello   # …） */
writeDeepScalar(root, ['providers', 'tts', 'label'], 'world');
const labelBack = readDeepScalar(root, ['providers', 'tts', 'label'], true);
check('普通字符串键写入后能读回', labelBack === 'world', `实际：${JSON.stringify(labelBack)}`);

rmSync(root, { recursive: true, force: true });
console.log('');
if (failures.length) {
  console.error(`YAML 写入器测试未通过：${failures.length} 项失败 / ${passed + failures.length} 项`);
  for (const f of failures) console.error(`  - ${f}`);
  process.exit(1);
}
console.log(`YAML 写入器测试通过：${passed}/${passed} 项`);
