/**
 * 协议一致性检查（`npm run check:protocol`）。
 *
 * 协议有三个"同一份定义的副本"：
 *   1. `packages/protocol/protocol.json` —— 人读的单一来源（带注释与 schema）
 *   2. `packages/protocol/src/index.ts`   —— 前端用的 TS 类型
 *   3. `apps/brain/pet/protocol.py`       —— sidecar 用的常量与解析
 *
 * 三者只要有一处不同步，症状就是最难查的那种：前端静默丢弃一条本该处理的消息，
 * 或者 sidecar 拒收一条合法请求。而且**两侧的测试都会通过**（各自自洽）。
 * 所以这里做跨文件的一致性断言。
 */

import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const nodeMajor = Number(process.versions.node.split('.')[0]);
if (Number.isFinite(nodeMajor) && nodeMajor < 20) {
  console.error(`需要 Node 20+，当前是 ${process.versions.node}。请用较新的 Node 运行。`);
  process.exit(1);
}

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.join(here, '..', '..', '..');

const protocolJson = JSON.parse(
  readFileSync(path.join(root, 'packages', 'protocol', 'protocol.json'), 'utf-8'),
);
const tsSource = readFileSync(
  path.join(root, 'packages', 'protocol', 'src', 'index.ts'),
  'utf-8',
);
const pySource = readFileSync(
  path.join(root, 'apps', 'brain', 'pet', 'protocol.py'),
  'utf-8',
);
const appSource = readFileSync(
  path.join(root, 'apps', 'desktop', 'src', 'renderer', 'app.js'),
  'utf-8',
);
// 设置窗口也是"前端"：有些下行消息是发给它而不是宠物窗口的（见第 5 项检查）
const settingsSource = readFileSync(
  path.join(root, 'apps', 'desktop', 'src', 'renderer', 'settings.js'),
  'utf-8',
);
const gatewaySource = readFileSync(
  path.join(root, 'apps', 'brain', 'pet', 'gateway.py'),
  'utf-8',
);
const preloadSource = readFileSync(
  path.join(root, 'apps', 'desktop', 'src', 'preload', 'index.ts'),
  'utf-8',
);

let passed = 0;
const failures = [];
const check = (name, condition, detail = '') => {
  if (condition) {
    passed += 1;
    console.log(`  OK   ${name}`);
  } else {
    failures.push(`${name}${detail ? ` —— ${detail}` : ''}`);
    console.error(`  FAIL ${name}${detail ? `\n       ${detail}` : ''}`);
  }
};

const declaredTypes = Object.keys(protocolJson.messages);
const downlink = declaredTypes.filter((t) => protocolJson.messages[t].direction === 'toUi');
const uplink = declaredTypes.filter((t) => protocolJson.messages[t].direction === 'toBrain');
  /*
   * 三个方向桶。含义是**「谁收」**，不是「谁发」：
   *
   *   uplink   (toBrain)  前端 -> Python    要进 Python 白名单
   *   downlink (toUi)     Python -> 前端    要进 TO_UI_TYPES + 前端要有 case
   *   toMain   (toMain)   Python -> 主进程  **两边都不适用**（主进程自己消费）
   *
   * ⚠️ 第三类是 2026-10-09 加的。在那之前 `tts.synthesize` / `voice.say`
   * 被硬塞进 `downlink`，于是检查 3b（「TO_UI_TYPES 的每一项都要在
   * `ToUiMsg` 联合里」）和它们的 interface（在 `ToBrainMsg` 那边，按发送方命名）
   * **互相打架** —— 两个都正确的约束因为分桶不对而冲突。
   */
  const toMain = declaredTypes.filter((t) => protocolJson.messages[t].direction === 'toMain');

  /*
   * 🔴 **对 `toMain` 请求的应答**（2026-10-13 加的这一档）。
   *
   * ## 为什么需要单独一档
   *
   * 回执（`web.browser.result`）的处境很尴尬：
   *
   *   · **必须**在 `TO_UI_TYPES` 里 —— 那是 `parseMessage()` 唯一的解析闸门，
   *     不在里面整条消息会被静默丢掉
   *   · 但它的 interface **不能**进 `ToUiMsg` 联合 —— 那个联合是
   *     "发给**渲染层**"的，而回执的终点是 **Python**
   *
   * 于是下面 3b 那条检查（"白名单里每种消息都在 ToUiMsg 里"）会**误报**。
   *
   * `tts.result` 当年是被硬塞进 `toBrain` 绕过去的（它的注释里写着
   * "别看名字叫 result 就以为是下行回执"）—— 能过检查，但**语义是错的**。
   * 所以新消息用新档，和 `toMain` 一样排除在 3b 之外。
   *
   * ⚠️ `toMainReply` 仍然要过 **2b**（"每个上行类型都在 Python 白名单里"）——
   * 回执确实要进 Python 白名单。只是不要求进 `ToUiMsg`。
   */
  const toMainReply = declaredTypes.filter(
    (t) => protocolJson.messages[t].direction === 'toMainReply',
  );
  /** 两边都不适用 `ToUiMsg` 的那两类合起来（3b 排除用） */
  const notForUi = [...toMain, ...toMainReply];

console.log('协议一致性检查\n');
console.log(`1. protocol.json 共声明 ${declaredTypes.length} 种消息（上行 ${uplink.length} / 下行 ${downlink.length}）`);

// 1) 每个声明的 type 在 TS 侧必须有同名 interface
{
  const missing = declaredTypes.filter((type) => {
    // system.error -> SystemErrorMsg
    const pascal = type
      .split(/[.\-_]/)
      .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
      .join('');
    return !new RegExp(`interface\\s+${pascal}Msg\\b`).test(tsSource);
  });
  check(
    'TS 侧每种消息都有对应 interface',
    missing.length === 0,
    `缺少：${missing.join(', ')}`,
  );
}

// 2) 上行的每种 type 必须出现在 Python 的白名单里
{
  /*
   * ⚠️ **用「配对括号扫描」而不是正则**（2026-10-09 修，第 3 次踩这个坑）。
   *
   * ## 原来的正则为什么坏
   *
   *     /INCOMING_TYPES = frozenset\(\s*\{?([\s\S]*?)\}?\s*\)/
   *
   * 非贪婪 `[\s\S]*?` + **可选的** `\}?` + `\)` —— 于是它在正文里
   * **遇到第一个 `)` 就停下**。我在注释里写了 `VoiceSession.speak(`，
   * 那个 `(` 本身无所谓，但同一行后面的 `)` 让匹配提前结束，
   * 白名单被**截断**，检查报：
   *
   *     缺少：config.update, memory.backup, memory.clear, memory.query
   *
   * 而那 4 个**明明就在白名单里**（运行时 `INCOMING_TYPES` 共 12 项，全都在）。
   *
   * ## 为什么这个假警报危险
   *
   * 真问题和假警报混在一起的报告最容易把人带偏 —— 上面的注释里已经记过
   * 两次同类教训。而且这次的具体后果更糟：它会**逼着人往白名单里重复加
   * 已经存在的项**（"既然说缺就加上吧"），越改越乱。
   *
   * ## 正确做法
   *
   * 从 `frozenset(` 之后**数括号配平**，取到真正闭合的那一个。
   * 这样注释里写什么都不影响 —— 这也是本次会话反复得到的教训：
   * **结构化代码不要用子串/正则去切，用配平或 AST。**
   */
  const head = pySource.indexOf('INCOMING_TYPES = frozenset(');
  let whitelistText = '';
  if (head >= 0) {
    const open = pySource.indexOf('(', head);
    let depth = 0;
    for (let i = open; i < pySource.length; i += 1) {
      const ch = pySource[i];
      if (ch === '(') depth += 1;
      else if (ch === ')') {
        depth -= 1;
        if (depth === 0) {
          whitelistText = pySource.slice(open + 1, i);
          break;
        }
      }
    }
  }
  const whitelist = whitelistText ? [null, whitelistText] : null;
  /*
   * ⚠️ 匹配前**必须剥掉 Python 注释**（`#` 和 `"""` 两种都要）。
   *
   * ## 两次踩坑
   *
   * ① 白名单块里写了中文注释解释设计取舍，注释里有 `"点了没反应"` 这种
   *    带引号的短语，正则把它们也当成了消息类型 —— 检查报
   *    「白名单里没有多余项 —— 多余：合并两条, 导出, 按标签筛选, 点了没反应」。
   *    那一版只剥了 `#`。
   *
   * ② 2026-10-07 又踩一次：加 `voice.ptt` 时我用了**文档字符串**写说明，
   *    里面提到"用户看到的是'松开之后没反应'"，于是检查报
   *    「多余：松开之后没反应, 识别不准, 音频没送过去, voice.ptt」。
   *    —— 只有 `voice.ptt` 是真多余（那是**上行**类型，我错当成下行登记了），
   *    其余三个都是我的说明文字。**真问题和假警报混在一起**，
   *    这种报告最容易把人带偏。
   *
   * ## 所以除了剥注释，还要**按形状过滤**
   *
   * 消息类型必然是 `小写字母/数字` 加点分段（`user.text`、`voice.ptt`）。
   * 带空格、逗号、中文的一律不是类型 —— 直接不参与比对。
   * 这样以后无论注释写成什么样，检查器都不会再被自己的说明文字绊倒。
   */
  const stripped = whitelist
    ? whitelist[1]
        .split('\n')
        .map((line) => line.replace(/#.*$/, ''))
        .join('\n')
        // 剥掉三引号文档字符串（含跨行）
        .replace(/"""[\s\S]*?"""/g, '')
        .replace(/'''[\s\S]*?'''/g, '')
    : '';
  /*
   * 按形状筛：`^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$`
   * 全小写、下划线、点分段 —— 这是本项目消息类型的实际形态。
   *
   * ⚠️ 点分段是**零次或多次**，不是"至少一次"：`hello` 就是单段的类型。
   *    我第一版写成 `+`（至少一段点分），结果把 `hello` 筛掉了，
   *    检查报「缺少：hello（会被直接拒收）」—— 一个**假警报**，
   *    而它和真问题（`voice.ptt` 被我错登记成下行）混在同一份报告里。
   */
  const TYPE_SHAPE = /^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$/;
  const listed = [...stripped.matchAll(/"([^"]+)"/g)]
    .map((m) => m[1])
    .filter((t) => TYPE_SHAPE.test(t));
  const missing = uplink.filter((type) => !listed.includes(type));
  check(
    'Python 白名单覆盖全部上行消息',
    missing.length === 0,
    `缺少：${missing.join(', ')}（会被直接拒收）`,
  );
  /*
   * ⚠️ `toMain` 不算多余（2026-10-09）。
   *
   * 这类消息 Python **发**、但主进程转发时 Python 也**收**，
   * 所以留在白名单里是对的。不排除的话，`voice.say` 会在同一份报告里
   * 被报两次 ——「缺少」一次、「多余」一次 —— 自相矛盾，最容易把人带偏。
   *
   * ⚠️ `toMainReply` 同理（2026-10-13）：它是**回执**，Python 必须收得到，
   * 所以必须留在白名单里。用 `notForUi`（两类合起来）而不是只排 `toMain` ——
   * 少排一类就会得到和上面一样的自相矛盾报告。
   */
  const extra = listed.filter((type) => !uplink.includes(type) && !notForUi.includes(type));
  check('白名单里没有多余项', extra.length === 0, `多余：${extra.join(', ')}`);
}

// 3) 下行的每种 type 必须出现在 TS 的 TO_UI_TYPES 里（否则会被静默丢弃）
{
  /*
   * ⚠️ **必须先把注释剥掉**（2026-10-12 修，一次很难查的假警报）。
   *
   * 原来的正则直接从 `TO_UI_TYPES = ...([` 抓到第一个 `])`。而注册项的
   * 注释里如果举了 payload 例子、里面有 `]`，正则就会**提前截断**：
   *
   *     //   { phase: 'start' | 'end', tool: 'web_search' | 'web_fetch',
   *     //     detail: '银松镇 游戏' }     <- 这一行之前就被切断了
   *
   * 于是提取出 `start` / `end` / `web_search` / `web_fetch` 四个
   * **"消息类型"**，然后报「不在联合里」—— 而真正的 `pet.tool`
   * 连扫都没扫到。
   *
   * 而 `start` / `end` 这种名字**长得就像合法类型**，所以靠形状过滤
   * （`TYPE_SHAPE`）挡不住 —— 只能从源头剥注释。
   *
   * 假警报比不报还费时间：我为此先怀疑过 `aliasFor` 的拼写规则，
   * 又怀疑过块级作用域，都不是。
   */
  const stripComments = (src) =>
    src.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/[^\n]*/g, '');
  const setBlock = /TO_UI_TYPES = new Set<string>\(\[([\s\S]*?)\]\)/.exec(
    stripComments(tsSource),
  );
  /*
   * ⚠️ **必须过滤掉非类型名的字符串**（2026-10-12 修，一次假警报）。
   *
   * 上面那段（Python 白名单）本来就有 `TYPE_SHAPE` 过滤，这里**漏了**。
   * 于是注释里举例用的词也被当成消息类型：
   *
   *     // payload:
   *     //   { phase: 'start' | 'end', tool: 'web_search' | 'web_fetch',
   *     //     detail: '银松镇 游戏' }
   *
   * 提取出 `start` / `end` / `web_search` / `web_fetch` / `银松镇 游戏`
   * 五个"类型"，然后报「不在联合里：start, end, web_search, ...」——
   * 而真正的 `pet.tool` 是**登记好的**。
   *
   * 假警报比不报还费时间：它让人去查一个根本不存在的问题
   * （我为此先怀疑了 `aliasFor` 的拼写规则）。
   *
   * `TYPE_SHAPE` 要求「小写字母开头 + 点分小写」，中文和驼峰名都过不了。
   */
  const listed = setBlock
    ? [...setBlock[1].matchAll(/'([^']+)'/g)]
        .map((m) => m[1])
        // ⚠️ `TYPE_SHAPE` 定义在**上面那个块**里（块级作用域），这里够不到 ——
        //    所以就地写一份。两个地方要改的话记得一起改。
        .filter((t) => /^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$/.test(t))
    : [];
  const missing = downlink.filter((type) => !listed.includes(type));
  check(
    'TS 下行白名单覆盖全部 toUi 消息',
    missing.length === 0,
    `缺少：${missing.join(', ')}（前端会静默丢弃）`,
  );

  // 3b) 白名单里的每一项都必须能对应到 ToUiMsg 联合类型里的某一个 interface。
  //
  // 这一条是补出来的：`voice.audio` / `voice.state` / `user.voice` / `memory.cleared`
  // 四个 interface 早就写好了、也进了白名单，却**不在 ToUiMsg 联合里**。
  // 后果是 TS 认为 `case 'voice.audio'` 不可能发生（死分支），
  // 而 esbuild 不做类型检查，所以一直没人发现 —— 直到真的加上类型检查。
  // 白名单和联合类型必须同时包含，否则"能收到"和"能处理"就分家了。
  const unionBlock = /export type ToUiMsg =([\s\S]*?);/.exec(tsSource);
  const unionMembers = unionBlock
    ? [...unionBlock[1].matchAll(/\|\s*([A-Za-z_]\w*)/g)].map((m) => m[1])
    : [];
  const aliasFor = (type) =>
    type
      .split(/[.\-_]/)
      .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
      .join('') + 'Msg';
  /*
   * ⚠️ 只对**真正的 downlink** 要求「在 `ToUiMsg` 联合里」。
   *
   * `toMain` 的类型（`tts.synthesize` / `voice.say`）也必须留在
   * `TO_UI_TYPES` 里 —— 那是 `parseMessage()` 唯一的解析闸门，
   * 不在里面整条消息会被丢掉。但它们的 interface 在 `ToBrainMsg`
   * 联合里（按**发送方**命名）。排除掉，两个约束就都成立了。
   */
  const notInUnion = listed.filter(
    (type) => !notForUi.includes(type) && !unionMembers.includes(aliasFor(type)),
  );
  check(
    '白名单里每种消息都在 ToUiMsg 联合类型里',
    notInUnion.length === 0 && unionMembers.length > 0,
    notInUnion.length
      ? `不在联合里：${notInUnion.join(', ')}（TS 会把对应的 case 判成死分支）`
      : 'ToUiMsg 解析失败',
  );
}

/*
 * ============================================================================
 * ⚠️ 这个脚本里 `direction` 的含义（和 TS 的联合类型命名**方向相反**）
 * ============================================================================
 *
 *   protocol.json 的 direction   本脚本的变量   实际含义
 *   ──────────────────────────   ────────────   ──────────────────────────────
 *   toUi                         uplink         **Python -> Electron**
 *                                               -> 要进 Python 白名单（检查 2）
 *                                               -> 要被主进程处理
 *   toBrain                      downlink       **Electron -> Python**
 *                                               -> 要进 TO_UI_TYPES（检查 3）
 *                                               -> 要进前端订阅（检查 6）
 *
 * 而 TS 的 `ToBrainMsg` / `ToUiMsg` 是按**发送方**命名的（ToBrain = 发给大脑的）。
 * 两套命名方向相反 —— 我第一次登记 `tts.synthesize` 时就搞反了。
 * **改这里之前先把这张表读一遍。**
 *
 * ----------------------------------------------------------------------------
 * `MAIN_ONLY`：只在 Python 与主进程之间走、**永远不经过渲染层**的消息
 * ----------------------------------------------------------------------------
 *
 * 现有的三类下行消息豁免机制：
 *
 *   · 推送类（`pet.say.delta`…）：主进程单方面发，**没人等** -> 必须有前端 case
 *   · 回执类（`memory.result`）：渲染层先请求 -> 在 `await` 里消费 -> 不需要 case
 *   · **主进程内部类**（`tts.synthesize`）：**Python 请求、主进程自己消费** ——
 *     既不进渲染层，也没有 `case`。下面两条检查对它不适用。
 *
 * 为什么不给它加一个假的 case 过门禁：那条注释已经写明了 ——
 * **那是死代码，比漏判更糟**（会让人以为"这条消息这里会处理"）。
 */
// 由 `toMain` 方向推导 —— 原来手写 `['tts.synthesize', 'voice.say']`，
// 容易和 protocol.json 脱节（我加 `voice.say` 时就漏改了一次）。
const MAIN_ONLY = new Set(toMain);
const downToFrontend = downlink.filter((t) => !MAIN_ONLY.has(t));

// 4) 下行的每种 type 必须真的会被发出去（bus -> wire 映射覆盖）
{
  const missing = downToFrontend.filter((type) => !gatewaySource.includes(`"${type}"`));
  check(
    'gateway 里登记了每种下行消息',
    missing.length === 0,
    `未登记：${missing.join(', ')}（大脑发了前端也收不到）`,
  );
}

// 5) 前端 switch 里处理了关键的下行消息
// 5) 每个**主动推送**的下行消息必须有前端处理
//
// ⚠️ 只检查"推送类"，不检查"请求-回执类"（2026-10-05 修）。
//
// 这两类的区别是**有没有调用方在等**：
//
//   · 推送类（`pet.say.delta`、`voice.state`、`memory.backup.done`…）：
//     主进程/大脑单方面发出来的，**没有任何人在等**。前端漏了处理 = 静默丢弃，
//     所以必须逐条盯着 —— 这正是这条检查的价值。
//
//   · 回执类（`memory.result`）：永远是"某个渲染层先发了请求，才收到这条"，
//     调用方在 `await` 里直接消费了返回值，**不需要额外的 case 分支**。
//     它由谁请求就由谁处理 —— 而现在请求方是记忆管理窗口（`memory.js`），
//     那个页面用的是 `await api.memoryAction(...)`，没有 `case` 语句。
//
// 不区分的话就会逼着人写一个**永远收不到**的 case 来骗过门禁 ——
// 那是死代码，比漏判更糟（它会让人以为"这条消息这里会处理"）。
{
  /** 请求-回执类：有调用方在等，由 `await` 消费，不需要 case */
  const REQUEST_REPLY = new Set(['memory.result']);
  // ⚠️ 用 `downToFrontend`（已排除 MAIN_ONLY）—— 主进程内部消息不进渲染层
  const critical = downToFrontend.filter((type) => type !== 'ready' && !REQUEST_REPLY.has(type));
  /*
   * ⚠️ 前端有**两个**渲染层，消息不一定都发给宠物窗口。
   *
   * 有些下行消息是给**设置窗口**的（例如 `memory.backup.done` —— 备份是用户在
   * 设置页里点的），主进程会把它转发到 settings.js 而不是 app.js。
   *
   * 所以这里两边一起看。只看 app.js 的话，要么漏判（消息没人处理却不报错），
   * 要么被迫在宠物窗口里写一个**永远收不到**的 case 来骗过门禁 —— 那是死代码，
   * 比漏判更糟（它会让人以为"这条消息宠物窗口会处理"）。
   */
  const handled = [
    ...appSource.matchAll(/case '([a-z_.]+)':/g),
    ...settingsSource.matchAll(/case '([a-z_.]+)':/g),
  ].map((m) => m[1]);
  const missing = critical.filter((type) => !handled.includes(type));
  check(
    '前端处理了全部**推送类**下行消息（回执类由 await 消费，不在此列）',
    missing.length === 0,
    `未处理：${missing.join(', ')}`,
  );
}

// 6) 前端订阅的通道与 sidecar 实际发出的消息类型一致
{
  const channels = [...new Set([...appSource.matchAll(/pet\.on\('([^']+)'/g)].map((m) => m[1]))];
  const petChannels = channels.filter((c) => !c.startsWith('pet:'));
  const unknown = petChannels.filter((c) => !downlink.includes(c));
  check(
    '前端订阅的消息类型都在协议里声明过',
    unknown.length === 0,
    `未声明：${unknown.join(', ')}`,
  );
}

// 7) 情绪枚举两侧一致
{
  const tsEmotions = /export type Emotion =([\s\S]*?);/.exec(tsSource);
  const tsList = tsEmotions ? [...tsEmotions[1].matchAll(/'([a-z]+)'/g)].map((m) => m[1]) : [];
  const pyEmotions = /EMOTIONS = \(([^)]*)\)/.exec(pySource);
  const pyList = pyEmotions ? [...pyEmotions[1].matchAll(/"([^"]+)"/g)].map((m) => m[1]) : [];
  check(
    '情绪枚举两侧完全一致',
    tsList.length > 0 && tsList.join(',') === pyList.join(','),
    `TS=[${tsList}] PY=[${pyList}]`,
  );
}

// 8) 协议版本号两侧一致
{
  const tsVersion = /PROTOCOL_VERSION = (\d+)/.exec(tsSource)?.[1];
  const pyVersion = /PROTOCOL_VERSION = (\d+)/.exec(pySource)?.[1];
  const jsonVersion = String(protocolJson.protocolVersion);
  check(
    '协议版本号三处一致',
    tsVersion === pyVersion && tsVersion === jsonVersion,
    `TS=${tsVersion} PY=${pyVersion} JSON=${jsonVersion}`,
  );
}

// 9) preload 的方法名与前端调用一致（防拼写漂移）
{
  const methods = [...new Set([...appSource.matchAll(/pet\.([a-zA-Z_]\w*)\(/g)].map((m) => m[1]))];
  const missing = methods.filter((m) => m !== 'on' && !new RegExp(`${m}\\s*\\(`).test(preloadSource));
  check('前端调用的每个 pet 方法都在 preload 里', missing.length === 0, `缺少：${missing.join(', ')}`);
}

console.log('');
if (failures.length) {
  console.error(`协议一致性检查未通过：${failures.length} 项 / ${passed + failures.length} 项`);
  for (const item of failures) console.error(`  - ${item}`);
  process.exit(1);
}
console.log(`协议一致性检查通过：${passed}/${passed} 项`);
