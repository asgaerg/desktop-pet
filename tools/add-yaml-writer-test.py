"""给 YAML 写入器加回归测试：**裸标量 + 注释** 的行不能被写坏。

## 为什么必须钉住

实测复现的 bug（2026-10-10）：

```
写入前： global_vk: 120          # F9
写入后： global_vk: 50# F9          <- 值和 # 粘在一起
```

`splitScalarLine` 取 `tail` 时**丢掉了值与 `#` 之间的空白**。而 YAML 里
`50# F9` **不是** `50` 加注释，而是**整个字符串 `"50# F9"`**。

**真实后果**（就发生在这个项目里）：

```
vk: 165                 # 右 Alt（0xA5）…
-> '165# 右 Alt（0xA5）…'

Number('165# 右 Alt…') === NaN
-> "按住说话"的键**静默变成未设置**，而配置文件看起来还在
```

它影响**所有**"裸标量 + 注释"的行（不只 push_to_talk）。而且这种损坏
**只在写过一次之后才出现** —— 不写测试就只能靠下次踩。

## 测什么

  ① 写入后，值与 `#` 之间**仍有空白**
  ② 读回来**类型不变**（数字还是数字，不是字符串）
  ③ 连写多次不累积损坏（旧版有过"每保存一次追加一段注释"的事故）
  ④ 引号串 + 注释也正常（那条路径本来就对，作为对照）
"""

import pathlib
import subprocess
import sys

sys.path.insert(0, ".")
from tool_bootstrap import ROOT, setup  # noqa: E402

setup(verbose=False)

NODE24 = r"C:\Users\tianyi\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\node\bin\node.exe"
T = ROOT / "apps" / "desktop" / "tools" / "test-yaml-writer.mjs"

SCRIPT = r'''
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

const root = mkdtempSync(join(tmpdir(), 'yaml-test-'));
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
'''

T.write_text(SCRIPT, encoding="utf-8")
print(f"  ✅ 已写 {T.name}")

r = subprocess.run([NODE24, str(T)], capture_output=True, text=True, encoding="utf-8", errors="replace")
print(r.stdout)
if r.stderr.strip():
    print("stderr:", r.stderr[:600])
print(f"  exit={r.returncode}")
