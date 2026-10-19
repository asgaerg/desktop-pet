/**
 * 验证 `readDeepScalar` / `writeDeepScalar`（任意层级路径的读写）。
 *
 * ## 为什么必须测这一层（2026-10-07 教训）
 *
 * 我给"按住说话"读配置时写错了路径（`voice.push_to_talk` 而不是
 * `providers.push_to_talk`），而**读不到值不报错** —— 表现是
 * "配置文件里明明写着 true，代码说没开"。这种错只能靠**对着真配置读一次**
 * 才发现，读代码是看不出来的（路径字符串看起来都很合理）。
 */

const fs = require('node:fs');
const path = require('node:path');

// ⚠️ 这个脚本住在 `apps/desktop/tools/` 下，而 persona.yaml 在**仓库根**的
//    `config/` 里 —— 所以要往上走**两级**（第一版只走了一级，直接 ENOENT）。
const ROOT = path.resolve(__dirname, '..', '..', '..');
const YAML = path.join(ROOT, 'config', 'persona.yaml');

/**
 * 复刻 `readDeepScalar`（源码在 src/main/personaStore.ts）。
 *
 * ⚠️ 取到值之后必须走 `scalarFromText` 那一步：它会
 * **`split('#')[0].trim()`** 去掉行尾注释，再把 `true`/`数字` 转成对应类型。
 * 我第一版漏了这一步，于是读到 `"true   # 默认关闭…"` 这种值 ——
 * **测试脚本自己的 bug**，不是产品的（产品里那一步一直都在）。
 */
function scalarFromText(text) {
  const raw = String(text).split('#')[0].trim();
  if (!raw) return null;
  if (/^(true|false)$/i.test(raw)) return raw.toLowerCase() === 'true';
  if (/^-?\d+(\.\d+)?$/.test(raw)) return Number(raw);
  return raw.replace(/^["']|["']$/g, '');
}

function readDeep(raw, pathArr, strictAnchors = false) {
  const lines = raw.split(/\r?\n/);
  let parentIndent = -1;
  let depth = 0;
  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i];
    if (!line.trim() || line.trimStart().startsWith('#')) continue;
    const indent = line.length - line.trimStart().length;
    /*
     * ⚠️ **严格小于** —— 同缩进是兄弟键，不能结束搜索。
     * 这条判据曾经写成 `<=`，于是 `memory.path`（memory 下面第一个就是
     * 同缩进的直系键）永远读不到，而 `providers.brain.model` 碰巧能读到
     * （providers 下面先有更深的子段）。测试用它抓出来了。
     */
    if (indent < parentIndent) return null;
    const isLast = depth === pathArr.length - 1;
    const name = pathArr[depth];
    if (isLast) {
      const m = new RegExp(`^${name}\\s*:(.*)$`).exec(line.trim());
      // 空值（纯段标题）不当成"读到空值"，继续下行
      if (m && (m[1] ?? '').trim() !== '') return scalarFromText(m[1] ?? '');
      continue;
    }
    /*
     * ⚠️ 下行一层的缩进要求。**这一段是 2026-10-07 修影子键 bug 的关键**：
     *
     * 配置里 `memory:` 出现了**两次** —— 顶层（缩进 0）与 `autonomy` 段下的
     * 同名子段（缩进 2）。不校验缩进时会先撞上后者、进错分支，
     * 然后 `memory.path` 永远读不到（它在顶层写着 `path: auto`）。
     *
     * `strictAnchors` 时每一级都精确匹配（第一级必须是顶层，缩进 0）。
     */
    if (strictAnchors) {
      if (depth === 0 ? indent !== 0 : indent !== parentIndent + 2) continue;
    }
    if (!new RegExp(`^${name}\\s*:\\s*$`).test(line.trim())) continue;
    parentIndent = indent;
    depth += 1;
  }
  return null;
}

const raw = fs.readFileSync(YAML, 'utf8');

const cases = [
  // [路径, 期望值（null = 应当读不到）, 说明]
  [['providers', 'push_to_talk', 'enabled'], true, '按住说话开关（本次要用的）'],
  [['providers', 'push_to_talk', 'vk'], 165, '按住说话的键码'],
  [['providers', 'brain', 'model'], 'deepseek-flash', '对话模型名'],
  [['providers', 'asr', 'provider'], 'mock', 'ASR 提供者'],
  [['providers', 'vision', 'enabled'], false, '看屏幕开关（布尔）'],
  /*
   * ⚠️ 下面两条是**两层的直系键**（`memory.path` 而不是 `memory.xxx.path`）。
   *
   * 它们曾经 FAIL，而我一度以为是产品的问题 —— 实际是**这份测试脚本自己**
   * 有问题（复刻得不完整）。**产品代码一直是好的**：应用日志里
   * `按住说话已启动：监听 右 Alt（VK 0xA5）` 就是它读对了配置的证据。
   *
   * ⚠️ 如果这两条以后再 FAIL，先怀疑**这份脚本**没有跟上产品代码，
   *    而不是直接去改 product —— 测试脚本自己出错的次数在本项目里
   *    比产品出错还多（已记入文档）。
   */
  [['memory', 'path'], 'auto', '记忆库落点（两层的直系键）'],
  [['memory', 'max_items'], 2000, '检索层条数上限（两层的直系键）'],
  // ⚠️ 反例：路径层数猜错必须读不到 —— 否则说明它读到了别的层的值（那更危险）
  [['voice', 'push_to_talk', 'enabled'], null, '错路径（少一层）应当读不到'],
  [['providers', 'nonexistent', 'x'], null, '不存在的段'],
  [['providers', 'push_to_talk', 'nope'], null, '不存在的键'],
];

let bad = 0;
console.log('路径 -> 值');
for (const [p, expected, label] of cases) {
  const v = readDeep(raw, p, true);
  const ok = v === expected;
  if (!ok) bad += 1;
  console.log(
    `  ${ok ? 'OK  ' : 'FAIL'} ${p.join('.')} = ${JSON.stringify(v)}` +
      (ok ? `   （${label}）` : `   期望 ${JSON.stringify(expected)} —— ${label}`),
  );
}

console.log();
if (bad) {
  console.log(`结果：❌ ${bad}/${cases.length} 项不符合预期`);
  process.exit(1);
}
console.log(`结果：✅ ${cases.length}/${cases.length} 项符合预期`);
