/**
 * 人格提示词的读写（`config/persona.yaml` 的 `persona.system_prompt`）。
 *
 * ## 为什么做**行级编辑**而不是"解析再序列化"
 *
 * 那个文件里有**大量注释**，而且是用户/我们手工维护的（每个字段上面都写着"为什么"）。
 * 用 YAML 解析再 dump 会**把所有注释抹掉** —— 那是不可接受的损失。
 *
 * 所以只做一件事：定位 `persona:` 段里的 `system_prompt: |` 块标量，
 * **替换它下面的缩进行**，其余内容原样保留（逐字节）。
 *
 * ## ⚠️ 保留 `{name}` 占位符
 *
 * Python 侧会把 `system_prompt` 里的 `{name}` 替换成宠物名字（见 `config.py`）。
 * 用户在界面上编辑时可能把它删掉 —— 那不是错误（只是名字不再自动插入），
 * 但我们**不能替用户加回去**，否则等于篡改他的输入。
 */

import { existsSync, readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';

export interface PersonaPrompt {
  /** 当前提示词（多行） */
  prompt: string;
  /** 宠物名字（`persona.name`），界面上只读展示 */
  name: string;
  /** 实际读写的文件路径（界面上显示，便于排查"改了没生效"） */
  path: string;
  /** 是否成功读到（false = 文件不存在，用的是兜底空串） */
  ok: boolean;
  /**
   * 是否**启用**这段提示词（`persona.system_prompt_enabled`）。
   *
   * 用户要求：「提示词用户可自由选择可选可不选，所以要加个勾选，确认是否启动提示词」。
   * 关掉时 Python 侧**一个字的提示词都不加**，完全按模型自己的性子说话。
   */
  enabled: boolean;
}

/** 定位 `config/persona.yaml`；找不到就返回 null */
function personaPath(projectRoot: string): string | null {
  const candidates = [
    join(projectRoot, 'config', 'persona.yaml'),
    // 打包后 `config/` 可能被复制到 resources 下；这两种都试
    join(projectRoot, 'resources', 'config', 'persona.yaml'),
    join(projectRoot, '..', 'config', 'persona.yaml'),
  ];
  for (const p of candidates) {
    if (existsSync(p)) return p;
  }
  return null;
}

/**
 * 从一个块标量里读内容。
 *
 * 支持 YAML 的 `|`（保留换行）与 `|-`（去掉末尾换行）。**不支持** `>`（折叠）——
 * 遇到就原样返回并把 `ok` 置 false，让界面提示"这个文件格式我们不认"，
 * 而不是猜错内容。
 */
function readBlockScalar(lines: string[], startIndex: number): { text: string; end: number } | null {
  const header = lines[startIndex];
  const m = /^(\s*)(system_prompt)\s*:\s*([|>][-+]?)\s*$/.exec(header);
  if (!m) return null;
  if (m[3].startsWith('>')) return null; // 折叠标量：不认

  const indent = m[1].length;
  const body: string[] = [];
  let i = startIndex + 1;
  for (; i < lines.length; i += 1) {
    const line = lines[i];
    if (line.trim() === '') {
      body.push('');
      continue;
    }
    // 缩进比 header 深 => 仍属于这个块标量
    const lineIndent = line.length - line.trimStart().length;
    if (lineIndent <= indent) break;
    body.push(line);
  }
  // 去掉每行公共缩进（块标量的语义）
  const nonEmpty = body.filter((l) => l.trim() !== '');
  const common = nonEmpty.length
    ? Math.min(...nonEmpty.map((l) => l.length - l.trimStart().length))
    : 0;
  const text = body.map((l) => (l.trim() === '' ? '' : l.slice(common))).join('\n');
  return { text, end: i };
}

/** 读当前提示词 */
export function readPersonaPrompt(projectRoot: string): PersonaPrompt {
  const empty: PersonaPrompt = { prompt: '', name: '', path: '', ok: false, enabled: true };
  const path = personaPath(projectRoot);
  if (!path) return { ...empty, path: '(未找到 config/persona.yaml)' };
  let raw: string;
  try {
    raw = readFileSync(path, 'utf8');
  } catch {
    return { ...empty, path };
  }
  const lines = raw.split(/\r?\n/);

  // 只在 `persona:` 段内找（避免误伤别处的同名字段）
  let inPersona = false;
  let personaIndent = -1;
  let name = '';
  let enabled = true; // 缺省视为开启（老配置文件没有这个字段）
  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i];
    if (/^\S/.test(line)) {
      inPersona = /^persona\s*:/.test(line);
      personaIndent = 0;
      continue;
    }
    if (!inPersona) continue;

    const lineIndent = line.length - line.trimStart().length;
    if (line.trim() !== '' && lineIndent <= personaIndent) {
      inPersona = false;
      continue;
    }

    const nameMatch = /^\s*name\s*:\s*(.+?)\s*$/.exec(line);
    if (nameMatch && !name) name = nameMatch[1].replace(/^["']|["']$/g, '');

    const enMatch = /^\s*system_prompt_enabled\s*:\s*(\S+)/.exec(line);
    if (enMatch) enabled = !/^(false|no|0|off)$/i.test(enMatch[1]);

    const block = readBlockScalar(lines, i);
    if (block) return { prompt: block.text, name, path, ok: true, enabled };
  }
  return { prompt: '', name, path, ok: false, enabled };
}

/**
 * 写"是否启用提示词"。
 *
 * 与提示词正文不同，这是一个**布尔字段**，所以：
 *   · 文件里已有该字段 -> 原地替换那一行（保留缩进与行尾注释）；
 *   · 没有 -> 插到 `persona:` 段的第一行（紧跟段首，便于人看）。
 *
 * ⚠️ 同样**不做解析再序列化** —— 那会抹掉文件里的全部注释（见文件顶部说明）。
 */
export function writePersonaEnabled(projectRoot: string, enabled: boolean): WritePromptResult {
  const path = personaPath(projectRoot);
  if (!path) return { ok: false, message: '未找到 config/persona.yaml', path: '' };

  let raw: string;
  try {
    raw = readFileSync(path, 'utf8');
  } catch (error) {
    return { ok: false, message: `读取失败：${(error as Error).message}`, path };
  }

  const eol = raw.includes('\r\n') ? '\r\n' : '\n';
  const lines = raw.split(/\r?\n/);
  const value = enabled ? 'true' : 'false';

  let inPersona = false;
  let personaIndent = -1;
  let insertAt = -1;
  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i];
    if (/^\S/.test(line)) {
      inPersona = /^persona\s*:/.test(line);
      if (inPersona) insertAt = i + 1;
      personaIndent = 0;
      continue;
    }
    if (!inPersona) continue;
    const lineIndent = line.length - line.trimStart().length;
    if (line.trim() !== '' && lineIndent <= personaIndent) {
      inPersona = false;
      continue;
    }
    const enMatch = /^(\s*)system_prompt_enabled\s*:\s*\S+(.*)$/.exec(line);
    if (enMatch) {
      // 保留缩进与行尾注释
      lines[i] = `${enMatch[1]}system_prompt_enabled: ${value}${enMatch[2]}`;
      try {
        writeFileSync(path, lines.join(eol), 'utf8');
      } catch (error) {
        return { ok: false, message: `写入失败：${(error as Error).message}`, path };
      }
      return { ok: true, message: '已保存', path };
    }
  }

  if (insertAt < 0) return { ok: false, message: '文件里没有 persona: 段', path };
  lines.splice(insertAt, 0, `  system_prompt_enabled: ${value}`);
  try {
    writeFileSync(path, lines.join(eol), 'utf8');
  } catch (error) {
    return { ok: false, message: `写入失败：${(error as Error).message}`, path };
  }
  return { ok: true, message: '已保存', path };
}

export interface WritePromptResult {
  ok: boolean;
  message: string;
  path: string;
}

/**
 * 写回提示词（行级替换，保留文件其余部分**逐字节不变**）。
 *
 * 写完后需要让 sidecar 重新读盘 —— 由调用方负责（见 `index.ts` 的 `settings:set-prompt`）。
 */
export function writePersonaPrompt(projectRoot: string, prompt: string): WritePromptResult {
  const path = personaPath(projectRoot);
  if (!path) return { ok: false, message: '未找到 config/persona.yaml', path: '' };

  let raw: string;
  try {
    raw = readFileSync(path, 'utf8');
  } catch (error) {
    return { ok: false, message: `读取失败：${(error as Error).message}`, path };
  }

  const eol = raw.includes('\r\n') ? '\r\n' : '\n';
  const lines = raw.split(/\r?\n/);

  let inPersona = false;
  let personaIndent = -1;
  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i];
    if (/^\S/.test(line)) {
      inPersona = /^persona\s*:/.test(line);
      personaIndent = 0;
      continue;
    }
    if (!inPersona) continue;
    const lineIndent = line.length - line.trimStart().length;
    if (line.trim() !== '' && lineIndent <= personaIndent) {
      inPersona = false;
      continue;
    }

    const block = readBlockScalar(lines, i);
    if (!block) continue;

    const indent = ' '.repeat(lineIndent + 2); // 比 `system_prompt:` 深两级
    const bodyLines = prompt
      .replace(/\r\n/g, '\n')
      .split('\n')
      .map((l) => (l.trim() === '' ? '' : indent + l));

    const next = [...lines.slice(0, i + 1), ...bodyLines, ...lines.slice(block.end)];
    try {
      writeFileSync(path, next.join(eol), 'utf8');
    } catch (error) {
      return { ok: false, message: `写入失败：${(error as Error).message}`, path };
    }
    return { ok: true, message: '已保存', path };
  }

  return { ok: false, message: '文件里没有找到 persona.system_prompt', path };
}

/* ------------------------------------------------------------------ 通用标量编辑 */

/** 可编辑的标量类型：数字 / 布尔 / 字符串 */
export type ScalarValue = number | boolean | string;

/**
 * 块标量的起始标记：`key: |`、`key: |-`、`key: >`、`key: >-`。
 *
 * ## 为什么需要它（2026-10-05 实测 bug）
 *
 * 提示词模板搬到 `persona.yaml` 之后，多行的那条（事实提炼）写成了块标量：
 *
 *     extract: |
 *       从下面这段对话里，抽出……
 *
 * 而行级解析只看 `key: (.*)`，抓到的是那个 **`|`** 本身 ——
 * 于是设置页里那条提示词的编辑框显示的是**一个竖线字符**，
 * 用户保存一次就把 221 字的提示词覆盖成了 `|`。
 *
 * 这种 bug 的形态很典型：**读到了"语法标记"而不是"值"**，而且不报错。
 */
const BLOCK_SCALAR = /^([|>])([-+]?)$/;

/**
 * 把 `key: xxx` 的右半边解释成标量。
 *
 * 抽出来是因为 `readScalar` 与 `readNestedScalar` 原本各写了一份**完全相同**的
 * 判断（布尔 / 数字 / 去引号）—— 加块标量支持时只改一处的话，另一处就成了暗雷。
 */
function scalarFromText(text: string): ScalarValue | null {
  const raw = text.split('#')[0].trim();
  if (!raw) return null;
  if (/^(true|false)$/i.test(raw)) return raw.toLowerCase() === 'true';
  if (/^-?\d+(\.\d+)?$/.test(raw)) return Number(raw);
  return raw.replace(/^["']|["']$/g, '');
}

/**
 * 从 `lines[index]` 开始读一个值；遇到块标量就吞掉整个块。
 *
 * 返回 `{ value, next }`：
 *   · `value` 为 `null` 表示这一行不是有效标量；
 *   · `next` 是**下一个待检查的行号** —— 块标量会一次跳过好几行，
 *     调用方必须用它继续循环，否则块内容会被当成新的键值行去解析。
 *
 * `indentOf` 是父行的缩进，用来判断块何时结束。
 */
function scalarAt(
  lines: readonly string[],
  index: number,
  keyPattern: RegExp,
  parentIndent: number,
): { value: ScalarValue | null; next: number } {
  const m = keyPattern.exec(lines[index]);
  if (!m) return { value: null, next: index + 1 };

  const rhs = m[2] === undefined ? m[1] : m[2];
  const text = (rhs ?? '').trim();
  const block = BLOCK_SCALAR.exec(text);
  if (!block) return { value: scalarFromText(rhs ?? ''), next: index + 1 };

  /*
   * 块标量：读到**缩进小于等于父行**的第一个非空行为止。
   *
   * `|-` / `>-` 表示"不要结尾换行"，其余情况 YAML 会在末尾补一个换行 ——
   * 这里按同样的语义处理，让读到的值与 Python 侧解析出来的一致
   * （不一致会导致"读进来 221 字、存回去 222 字"这种静默漂移）。
   */
  const folded = block[1] === '>';
  const chomp = block[2];
  const body: string[] = [];
  let i = index + 1;
  for (; i < lines.length; i += 1) {
    const line = lines[i];
    if (line.trim() === '') {
      body.push('');
      continue;
    }
    const indent = line.length - line.trimStart().length;
    if (indent <= parentIndent) break;
    body.push(line);
  }
  // 去掉尾部空行（它们属于块与下一个键之间的间隔）
  while (body.length && body[body.length - 1] === '') body.pop();
  // 统一去掉块本身那层缩进
  const nonEmpty = body.filter((l) => l.trim() !== '');
  const base = nonEmpty.length ? Math.min(...nonEmpty.map((l) => l.length - l.trimStart().length)) : 0;
  const dedented = body.map((l) => l.slice(base));
  const joined = folded ? dedented.join(' ').replace(/\s+/g, ' ').trim() : dedented.join('\n');
  const value = chomp === '-' ? joined : `${joined}\n`;
  return { value, next: i };
}

function formatScalar(value: ScalarValue): string {
  if (typeof value === 'boolean') return value ? 'true' : 'false';
  if (typeof value === 'number') return String(value);
  // 字符串按 YAML 单引号引起来，避免含冒号/井号时被解析坏
  return `'${value.replace(/'/g, "''")}'`;
}

  /**
   * 把一行 `key: <值>` 拆成「值」与「值之后的尾巴（注释）」。
   *
   * ## 为什么不用正则（2026-10-09，我在同一条正则上栽了两次）
   *
   *     第一版 `^\S*(.*)$`        贪婪 -> 把**值自己的尾巴**当注释保留
   *                               -> 每保存一次追加一段
   *     第二版 `^\S*?(\s+#.*)?$`  非贪婪 + 可选注释 -> 匹配不上就整条不匹配
   *                               -> 掉进「插入新键」分支，写出了 `undefined`
   *
   * **根因不是正则写得不对，而是「拿正则解析结构化文本」这件事本身不行。**
   * YAML 的值可以是单引号串（内部 `''` 转义）、双引号串（内部 `\"` 转义）、
   * 裸标量（可能含 `:` `/`）、块标量，后面还可能跟注释。
   *
   * 所以改成**逐字符扫描**：先看首字符是不是引号，是就找配对的收尾引号；
   * 不是就吃到「空白 + #」或行尾。值里含什么字符都不会错。
   */
  function splitScalarLine(rest: string): { value: string; tail: string } | null {
    const text = rest;
    const n = text.length;
    let i = 0;
    while (i < n && (text[i] === ' ' || text[i] === '\t')) i += 1;
    if (i >= n) return { value: '', tail: '' };

    const first = text[i];

    // ---- 引号串：找配对的收尾引号（同种引号连写两个 = 一个转义引号）----
    if (first === "'" || first === '"') {
      const quote = first;
      let j = i + 1;
      let value = '';
      while (j < n) {
        const ch = text[j];
        if (ch === quote) {
          if (text[j + 1] === quote) {
            value += quote;
            j += 2;
            continue;
          }
          return { value, tail: text.slice(j + 1) };
        }
        if (quote === '"' && ch === '\\' && j + 1 < n) {
          value += text[j + 1];
          j += 2;
          continue;
        }
        value += ch;
        j += 1;
      }
      // 引号没闭合 —— 这行是坏的，当「没找到值」处理
      return null;
    }

    // ---- 裸标量：吃到「空白 + #」或行尾 ----
    let j = i;
    while (j < n) {
      if (text[j] === '#' && j > i && /\s/.test(text[j - 1])) break;
      j += 1;
    }
    /*
     * ⚠️ `tail` 必须**包含值与注释之间的空白**（2026-10-10 修的）。
     *
     * ## 原来的 bug（实测复现过）
     *
     * 原来写 `tail: text.slice(j)` —— `j` 停在 `#` 上，**前导空格被丢掉了**。
     * `writeDeepScalar` 用它拼回那一行时得到：
     *
     *     写入前： global_vk: 120          # F9
     *     写入后： global_vk: 50# F9          <- **值和 # 粘在一起**
     *
     * 而 YAML 里 `50# F9` **不是** `50` 加注释，而是**整个字符串 `"50# F9"`**。
     * 实测就这样丢掉过 `vk`：
     *
     *     vk: 165                 # 右 Alt（0xA5）…
     *     -> '165# 右 Alt（0xA5）…'
     *
     * 后果很隐蔽：`Number('165# 右 Alt…')` 是 `NaN`，
     * 于是"按住说话"的键**静默变成未设置**，而配置文件看起来还在。
     *
     * ## 修法
     *
     * 把 `trimEnd()` 掉的那段空白补回 `tail` 里 —— 值和注释之间必须有空白，
     * 否则 `#` 就不再是注释起始符。
     */
    const rawValue = text.slice(i, j);
    const value = rawValue.trimEnd();
    const gap = rawValue.slice(value.length); // 值与 # 之间的空白（可能为空）
    return { value, tail: gap + text.slice(j) };
  }


/**
 * 读某个段下的一个标量字段。
 *
 * `section` 是顶层段名（如 `memory`、`persona`），`key` 是段内字段名。
 * 只支持**一层嵌套**（`remember` 只用于 `memory.recent_window_days` 这类），
 * 够用且不会因为"支持任意路径"而写出难以验证的解析器。
 *
 * 支持块标量（`key: |`）—— 见 `BLOCK_SCALAR` 的说明。
 */
export function readScalar(
  projectRoot: string,
  section: string,
  key: string,
): ScalarValue | null {
  const path = personaPath(projectRoot);
  if (!path) return null;
  let raw: string;
  try {
    raw = readFileSync(path, 'utf8');
  } catch {
    return null;
  }
  const lines = raw.split(/\r?\n/);
  let inside = false;
  for (let i = 0; i < lines.length; ) {
    const line = lines[i];
    if (/^\S/.test(line)) {
      inside = new RegExp(`^${section}\\s*:`).test(line);
      i += 1;
      continue;
    }
    if (!inside) {
      i += 1;
      continue;
    }
    const indent = line.length - line.trimStart().length;
    // 捕获组 1 是不带缩进的键名，组 2 是右半边（可能没有）
    const r = scalarAt(lines, i, new RegExp(`^\\s*${key}\\s*:(.*)$`), indent);
    if (r.value !== null) return r.value;
    i = r.next;
  }
  return null;
}

/**
 * 写某个段下的一个标量字段（行级替换，**保留全部注释与格式**）。
 *
 * 文件里已有该字段 -> 原地替换那一行（保留缩进与**行尾注释**）；
 * 没有 -> 插到段首第一行。
 *
 * ## 多行值写成块标量（`key: |`）
 *
 * 提示词模板里有整段多行的（如"事实提炼"）。写成单引号 + 真实换行会让
 * YAML 解析直接报错（`缺少冒号 -> '规则：'` —— 踩过），所以多行值必须用块标量。
 *
 * ## ⚠️ 替换块标量时要**连内容一起删掉**
 *
 * 只替换 `extract: |` 这一行的话，下面那 6 行内容会变成**孤儿行** ——
 * 它们缩进比父行深，YAML 会把它们当成前一个键的延续或直接报错。
 * 所以这里先算出旧块的结束位置，再整体替换。
 *
 * ⚠️ 与 `writePersonaEnabled` 同样是"不做解析再序列化"的思路 ——
 * `persona.yaml` 里每个字段上面都写着"为什么"，用 YAML 库 dump 会把它们全部抹掉。
 */
export function writeScalar(
  projectRoot: string,
  section: string,
  key: string,
  value: ScalarValue,
): WritePromptResult {
  const path = personaPath(projectRoot);
  if (!path) return { ok: false, message: '未找到 config/persona.yaml', path: '' };

  let raw: string;
  try {
    raw = readFileSync(path, 'utf8');
  } catch (error) {
    return { ok: false, message: `读取失败：${(error as Error).message}`, path };
  }

  const eol = raw.includes('\r\n') ? '\r\n' : '\n';
  const lines = raw.split(/\r?\n/);
  const isMulti = typeof value === 'string' && value.includes('\n');

  let inside = false;
  let insertAt = -1;
  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i];
    if (/^\S/.test(line)) {
      inside = new RegExp(`^${section}\\s*:`).test(line);
      if (inside) insertAt = i + 1;
      continue;
    }
    if (!inside) continue;
    const m = new RegExp(`^(\\s*)${key}\\s*:\\s*(\\S*)(.*)$`).exec(line);
    if (!m) continue;

    const indent = m[1];
    // 旧值是块标量吗？是的话要把它的内容一并吞掉
    const wasBlock = BLOCK_SCALAR.test(m[2] ?? '');
    let end = i + 1;
    if (wasBlock) {
      const parentIndent = indent.length;
      while (end < lines.length) {
        const next = lines[end];
        if (next.trim() === '') {
          // 空行：看它后面还有没有更深缩进的内容；没有就停在这里
          let probe = end + 1;
          while (probe < lines.length && lines[probe].trim() === '') probe += 1;
          if (probe >= lines.length) break;
          const probeIndent = lines[probe].length - lines[probe].trimStart().length;
          if (probeIndent <= parentIndent) break;
          end = probe;
          continue;
        }
        const nextIndent = next.length - next.trimStart().length;
        if (nextIndent <= parentIndent) break;
        end += 1;
      }
    }

    const replacement = isMulti
      ? [
          `${indent}${key}: |`,
          ...String(value)
            .replace(/\n+$/, '')
            .split('\n')
            .map((l) => `${indent}  ${l}`),
        ]
      : // 单行值：保留行尾注释
        [`${indent}${key}: ${formatScalar(value)}${wasBlock ? '' : m[3]}`];

    lines.splice(i, end - i, ...replacement);
    try {
      writeFileSync(path, lines.join(eol), 'utf8');
    } catch (error) {
      return { ok: false, message: `写入失败：${(error as Error).message}`, path };
    }
    return { ok: true, message: '已保存', path };
  }

  // 字段不存在：插到段首。但要注意 **两级缩进**（memory.backup.dir 是嵌套的）
  if (insertAt < 0) return { ok: false, message: `文件里没有 ${section}: 段`, path };
  const nesting = 2; // 段内字段统一用两空格缩进
  const pad = ' '.repeat(nesting);
  /*
   * 新增字段也要支持多行：写成块标量，否则真实换行会破坏 YAML 结构
   * （报 `缺少冒号 -> '规则：'` 那种错）。
   */
  lines.splice(
    insertAt,
    0,
    ...(isMulti
      ? [`${pad}${key}: |`, ...String(value).replace(/\n+$/, '').split('\n').map((l) => `${pad}  ${l}`)]
      : [`${pad}${key}: ${formatScalar(value)}`]),
  );
  try {
    writeFileSync(path, lines.join(eol), 'utf8');
  } catch (error) {
    return { ok: false, message: `写入失败：${(error as Error).message}`, path };
  }
  return { ok: true, message: '已保存', path };
}

/**
 * 按**任意层级**的路径读标量，例如 `['providers','push_to_talk','enabled']`。
 *
 * ## ⚠️ 为什么需要它（2026-10-07 实测踩到）
 *
 * `readNestedScalar` 只支持**两层**（`section.sub.key`）。而真实配置里
 * 不同东西的深度**不一样**：
 *
 *     memory.backup.dir              <- 两层
 *     providers.brain.model          <- 两层
 *     providers.push_to_talk.enabled <- 两层
 *     providers.voice.asr.provider   <- 三层（如果 asr 真在 voice 下）
 *
 * 我给"按住说话"写的路径是 `voice.push_to_talk`，而它在配置里实际位于
 * `providers.push_to_talk` —— **读出来永远是 null**，表现是"配置文件里
 * 明明写着 true，代码说没开"。这种错**不报错**，只是值读不到。
 *
 * 与其为每一层写一个函数，不如让调用方直接给路径 —— 层数由路径表达，
 * 函数不再假设深度。缩进判断用"上一级的缩进"做基准，与层数无关。
 *
 * ## 实现要点
 *
 * 逐级下行：维护"当前应该在的缩进"，遇到缩进 <= 上一级缩进的键
 * 就不再属于我们要找的那条路径。最后一级按 `key: value` 取值。
 */
export function readDeepScalar(
  projectRoot: string,
  path: string[],
  /**
   * 是否要求**每一级的缩进都精确匹配**（含第一级必须是顶层键，缩进 0）。
   *
   * ## ⚠️ 为什么需要它：配置里有**同名的影子键**
   *
   * 实测 `memory:` 出现了**两次** —— 顶层的那个（缩进 0）和 `autonomy`
   * 段下的同名子段（缩进 2）。从 `parentIndent = -1` 出发时，缩进 2 也算
   * "合法的顶层键"，于是搜索先撞上影子键、进错分支：
   *
   *     readDeepScalar(root, ['memory','path'])  -> 进入 autonomy.memory -> null
   *     而 memory.path 在顶层明明写着 `path: auto`
   *
   * 精确匹配每一级缩进（0/2/4…）就没有歧义，但那要求调用方给的路径
   * **层数与文件完全一致**（少一层就找不到）。这两件事是矛盾的：
   * 要防影子键就得精确，要容错层数就不能精确。
   *
   * 所以做成开关：**新代码传 `true`**（路径写全、行为确定），
   * 老调用方不传，保持原行为不动它们。
   */
  strictAnchors = false,
): ScalarValue | null {
  if (!path.length) return null;
  const file = personaPath(projectRoot);
  if (!file) return null;
  let raw: string;
  try {
    raw = readFileSync(file, 'utf8');
  } catch {
    return null;
  }

  const lines = raw.split(/\r?\n/);
  /**
   * 当前这一级所在行的缩进。
   *
   * 循环从 **-1** 开始，这样第一段（段名）只要能匹配上"顶层键"就会被接受
   * （它的缩进是 0，0 > -1）。之后每进一层，`parentIndent` 更新为该层的
   * 实际缩进，于是下一段必须**恰好缩进 = parentIndent + 2**。
   *
   * ⚠️ 为什么要"恰好"：配置里 `memory:` **出现了两次**（顶层一次，
   * `autonomy` 段下还有一个同名子段）。只按名字匹配、不校验缩进时，
   * 先碰到的那个**影子键**会被当成目标 —— 实测就是它让 `memory.path`
   * 永远读不到（进错分支后在顶层键上按"缩进更浅"退出）。
   */
  let parentIndent = -1;
  let depth = 0;

  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i];
    if (!line.trim() || line.trimStart().startsWith('#')) continue;
    const indent = line.length - line.trimStart().length;
    /*
     * ⚠️ 缩进判据（2026-10-07 修，两条一起）：
     *
     *   ① **缩进比父级浅就退出** —— 出了这条分支，不必再找。
     *   ② **下行一层的缩进要求**：
     *        · `strictAnchors` 时：每一级都精确 `parentIndent + 2`
     *          （第一级要求 0，即必须是顶层键）—— 挡住影子键。
     *        · 否则：宽松（`> parentIndent`），容忍调用方少写一层。
     */
    if (indent < parentIndent) return null;
    const isLast = depth === path.length - 1;
    const name = path[depth];
    if (isLast) {
      const m = new RegExp(`^${name}\\s*:(.*)$`).exec(line.trim());
      // ⚠️ 复用 `scalarAt` 用的那个解析器（`scalarFromText`）——
      // 自己再写一遍引号/布尔/数字的解析规则，就又多一处会漂移的地方。
      //
      // ⚠️ 空值（`key:` 后面什么都没有）要**当成段名继续下行**，而不是
      // 当成"读到了空值"。见下面 `empty` 分支的说明。
      if (m && (m[1] ?? '').trim() !== '') return scalarFromText(m[1] ?? '');
    }
    /*
     * 中间层：必须是 `name:`（纯标题行，后面什么都没有）。
     *
     * ## ⚠️ 末级也可能是个"段"（2026-10-07 修）
     *
     * 路径的**最后一段有两种可能**：一个键（`key: value`），或者一个段
     * （`key:` 后面没值）。调用方不该被迫知道这件事 —— 那正是我踩的坑：
     *
     *     readDeepScalar(['providers','push_to_talk','enabled'])  <- enabled 是键 ✅
     *     readDeepScalar(['memory','path'])                       <- path 是键 ✅
     *     readDeepScalar(['memory','search'])                     <- search 是**段**
     *     readDeepScalar(['memory','search','min_score'])         <- 三层的键 ✅
     *
     * 所以判据不是"是不是最后一段"，而是**这一行长什么样**：
     *   · `name: 有值`  -> 取值（末级时）
     *   · `name:`      -> 下行（还有路径没走完时）
     * 两种都在这里处理，调用方只描述路径。
     */
    if (isLast) {
      // 末级若是个空值标题行，说明用户给的路径其实是"段"——下行一层无意义，
      // 直接返回 null（他要的值在更深处，应当把路径写全）
      continue;
    }
    /*
     * 下行一层。缩进要求见上面 ② 的说明。
     */
    if (strictAnchors) {
      // 每一级都精确：第一级必须是顶层（缩进 0），之后每级比父级深 2
      if (depth === 0 ? indent !== 0 : indent !== parentIndent + 2) continue;
    }
    if (!new RegExp(`^${name}\\s*:\\s*$`).test(line.trim())) continue;
    parentIndent = indent;
    depth += 1;
  }
  return null;
}


export function readNestedScalar(
  projectRoot: string,
  section: string,
  sub: string,
  key: string,
): ScalarValue | null {
  const path = personaPath(projectRoot);
  if (!path) return null;
  let raw: string;
  try {
    raw = readFileSync(path, 'utf8');
  } catch {
    return null;
  }
  const lines = raw.split(/\r?\n/);
  let inSection = false;
  let inSub = false;
  /** 子段标题自身的缩进 —— 用它判断"什么时候出了这个子段" */
  let subIndent = -1;
  for (let i = 0; i < lines.length; ) {
    const line = lines[i];
    if (/^\S/.test(line)) {
      inSection = new RegExp(`^${section}\\s*:`).test(line);
      inSub = false;
      subIndent = -1;
      i += 1;
      continue;
    }
    if (!inSection) {
      i += 1;
      continue;
    }
    const subM = new RegExp(`^(\\s+)${sub}\\s*:\\s*$`).exec(line);
    if (subM) {
      inSub = true;
      subIndent = subM[1].length;
      i += 1;
      continue;
    }
    /*
     * 子段结束的判据：**缩进回到不深于子段标题**。
     *
     * ## ⚠️ 这里原来写死了「两空格」（2026-10-07 修）
     *
     *     if (inSub && /^ {0,2}\S/.test(line)) inSub = false;
     *
     * 它假设"段级缩进恒为两格"。但 YAML 里**不同顶层的子段缩进不同**：
     *
     *     voice:              <- 顶层
     *       asr:              <- 缩进 2
     *     memory:             <- 顶层
     *       backup:           <- 缩进 2
     *
     * 一旦某个顶层段的子段是**四格**（比如段内还有一层分组），那个判据
     * 就永远不成立，`inSub` 会一直留到下一个顶层键 —— 于是"子段"和
     * "子段里的子段"混在一起，读出来的值可能是隔壁段的。
     *
     * 用"子段标题自己的缩进"当基准就与层级无关了：
     * 缩进 <= 它，说明已经出了这个子段。
     */
    if (inSub) {
      const indent = line.trim() === '' ? Number.POSITIVE_INFINITY : line.length - line.trimStart().length;
      if (indent <= subIndent) {
        inSub = false;
        subIndent = -1;
      }
    }
    if (!inSub) {
      i += 1;
      continue;
    }
    const indent = line.length - line.trimStart().length;
    const r = scalarAt(lines, i, new RegExp(`^\\s*${key}\\s*:(.*)$`), indent);
    if (r.value !== null) return r.value;
    i = r.next;
  }
  return null;
}

/**
 * 按**任意层级**的路径写标量，例如 `['providers','push_to_talk','vk'] = 165`。
 *
 * 与 `readDeepScalar` 成对：读和写必须用**同一套缩进判据**，否则会出现
 * "读得到、写不回"或者"写到隔壁段去了"这类极难查的不一致。
 *
 * ## ⚠️ 原 `writeNestedScalar` 的两个硬编码（2026-10-07）
 *
 * 它把两件事写死了，都只在"恰好两层"时才对：
 *
 *   ① `if (inSub && /^ {0,2}\S/.test(line))` —— 假设子段结束的缩进是两格
 *   ② 找不到键时插一行 `    ${key}: ...` —— 假设子段的子项缩进**恒为四格**
 *
 * 本项目里 `providers.push_to_talk` 的键缩进是**四格**（碰巧对上），
 * 但 `providers.brain` 的键也是四格、而 `providers` 的兄弟段可能是别的层数。
 * 一旦层数变了，② 会把键写到错的缩进上（YAML 里那意味着**换了个父级**，
 * 而且不报错）。
 *
 * 这里改成：缩进由**路径实际所在的层级**推出（父行缩进 + 2），插行位置也用它。
 */
/**
 * 取一行 YAML 的**键名**（行首到第一个 `:` 为止，去掉空白）。
 *
 * ## 为什么要它（2026-10-09，同一类 bug 栽了 4 次）
 *
 * 写入器原来用正则猜键名，而 `\s*` / `[ \t]*` 都**允许零个字符**，于是
 * `key = 'voice'` 会匹配上 `local_voice: '...'` —— 保存云端音色时把
 * `local_voice` 当成重复项删掉（或反过来），配置被写坏而函数返回成功。
 *
 * **解析出真实键名再精确比较**，比任何正则都可靠。
 */
function keyOf(line: string): string | null {
  const m = /^\s*([A-Za-z_][\w-]*)\s*:/.exec(line);
  return m ? m[1] : null;
}

/**
 * `key:` 里冒号之后的位置（用于 `slice` 取值）。
 *
 * ⚠️ 不能用 `key.length + 1` —— 走到那里的行可能是别的键
 * （`local_voice:` 也会经过），按 `voice.length` 切会切错位置。
 */
function nameEnd(line: string): number {
  const i = line.indexOf(':');
  return i < 0 ? line.length : i + 1;
}

export function writeDeepScalar(
  projectRoot: string,
  path: string[],
  value: ScalarValue,
): WritePromptResult {
  if (!path.length) return { ok: false, message: '路径为空', path: '' };
  const file = personaPath(projectRoot);
  if (!file) return { ok: false, message: '未找到 config/persona.yaml', path: '' };

  let raw: string;
  try {
    raw = readFileSync(file, 'utf8');
  } catch (error) {
    return { ok: false, message: `读取失败：${(error as Error).message}`, path: file };
  }

  const eol = raw.includes('\r\n') ? '\r\n' : '\n';
  const lines = raw.split(/\r?\n/);
  const text = formatScalar(value);

  /*
   * ## 用「缩进栈」走文件，和读取器同一套路径模型
   *
   * 这是重写（2026-10-09）的核心。旧版用"猜"：找不到键就插在段首、
   * 循环还会扫到别的段 —— 结果写 A 删 B，而且返回 `ok: true`。
   *
   * 新版的规则很简单：
   *   · 缩进栈 = 当前路径。遇到"纯标题行"（`key:` 后面没值）就入栈，
   *     遇到缩进不深于栈顶的就弹栈
   *   · 路径**正好等于** `path` 的父路径时，锁定"段"的范围 `[segStart, segEnd)`
   *   · 在段里找**深度正好是段深 + 1**、且键名**完全相等**的行 -> 改值
   *   · 只改那一行；找不到才插，插在**段内最后一个直接子键之后**
   *   · **任何情况下都不删除别人的行**
   */
  const stack: { indent: number; key: string }[] = [];
  let segStart = -1; // 段内第一个直接子键的行号
  let segEnd = -1; // 段结束（不含）
  let hit = -1; // 目标键的行号
  const parentPath = path.slice(0, -1);
  const want = path[path.length - 1];

  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i];
    if (!line.trim() || line.trimStart().startsWith('#')) continue;
    const indent = line.length - line.trimStart().length;
    const cur = keyOf(line);
    if (cur === null) continue;

    // 弹栈：缩进不深于栈顶的都出栈
    while (stack.length && indent <= stack[stack.length - 1].indent) stack.pop();

    const here = stack.map((s) => s.key);
    const isTitle = line.trim().replace(/^[^:]*:/, '').trim() === '';

    if (here.length === parentPath.length && here.every((k, n) => k === parentPath[n])) {
      // 就在目标段里（栈顶就是父路径）
      if (indent === stack[stack.length - 1].indent + 2) {
        if (segStart < 0) segStart = i;
        segEnd = i + 1;
        if (cur === want) hit = i;
      }
    }

    if (isTitle) stack.push({ indent, key: cur });
  }

  // ① 命中 -> 只改这一行（保留它自己的注释尾巴）
  if (hit >= 0) {
    const line = lines[hit];
    const indent = line.length - line.trimStart().length;
    const split = splitScalarLine(line.trim().slice(nameEnd(line.trim())));
    // 值是坏的（引号没闭合）也照样改写；`tail` 取不到就不保留尾巴
    lines[hit] = `${' '.repeat(indent)}${want}: ${text}${split ? split.tail : ''}`;
    try {
      writeFileSync(file, lines.join(eol), 'utf8');
    } catch (error) {
      return { ok: false, message: `写入失败：${(error as Error).message}`, path: file };
    }
    return { ok: true, message: '已保存', path: file };
  }

  // ② 没命中 -> 必须在**目标段已存在**的前提下才插（否则报错，不瞎猜位置）
  if (segStart < 0) {
    return { ok: false, message: `文件里没有 ${path.join('.')} 这一段`, path: file };
  }
  const parentIndent = stack.length
    ? stack[stack.length - 1].indent
    : // 段就是顶层时（parentPath 为空），缩进 0
      0;
  const insertAt = segEnd;
  lines.splice(insertAt, 0, `${' '.repeat(parentIndent + 2)}${want}: ${text}`);
  try {
    writeFileSync(file, lines.join(eol), 'utf8');
  } catch (error) {
    return { ok: false, message: `写入失败：${(error as Error).message}`, path: file };
  }
  return { ok: true, message: '已保存', path: file };
}

/** 写嵌套段下的标量（`section.sub.key`，如 `memory.backup.dir`）。 */
export function writeNestedScalar(
  projectRoot: string,
  section: string,
  sub: string,
  key: string,
  value: ScalarValue,
): WritePromptResult {
  const path = personaPath(projectRoot);
  if (!path) return { ok: false, message: '未找到 config/persona.yaml', path: '' };

  let raw: string;
  try {
    raw = readFileSync(path, 'utf8');
  } catch (error) {
    return { ok: false, message: `读取失败：${(error as Error).message}`, path };
  }

  const eol = raw.includes('\r\n') ? '\r\n' : '\n';
  const lines = raw.split(/\r?\n/);
  const text = formatScalar(value);

  let inSection = false;
  let inSub = false;
  let subInsertAt = -1;
  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i];
    if (/^\S/.test(line)) {
      inSection = new RegExp(`^${section}\\s*:`).test(line);
      inSub = false;
      continue;
    }
    if (!inSection) continue;
    const subM = new RegExp(`^(\\s+)${sub}\\s*:\\s*$`).exec(line);
    if (subM) {
      inSub = true;
      subInsertAt = i + 1;
      continue;
    }
    if (inSub && /^ {0,2}\S/.test(line)) {
      inSub = false;
      continue;
    }
    if (!inSub) continue;
    const m = new RegExp(`^(\\s+)${key}\\s*:\\s*\\S*(.*)$`).exec(line);
    if (m) {
      lines[i] = `${m[1]}${key}: ${text}${m[2]}`;
      try {
        writeFileSync(path, lines.join(eol), 'utf8');
      } catch (error) {
        return { ok: false, message: `写入失败：${(error as Error).message}`, path };
      }
      return { ok: true, message: '已保存', path };
    }
  }

  if (subInsertAt < 0) {
    return { ok: false, message: `文件里没有 ${section}.${sub} 段`, path };
  }
  lines.splice(subInsertAt, 0, `    ${key}: ${text}`);
  try {
    writeFileSync(path, lines.join(eol), 'utf8');
  } catch (error) {
    return { ok: false, message: `写入失败：${(error as Error).message}`, path };
  }
  return { ok: true, message: '已保存', path };
}
