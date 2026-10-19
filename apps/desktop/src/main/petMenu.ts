/**
 * 宠物本体的右键菜单（处理器表 + 两条弹出路径）。
 *
 * ## 现在有**两条**路径，说明为什么两条都留着（2026-10-14）
 *
 * ### 主路径：自绘窗口（`petMenuWindow.ts` + `renderer/petMenu.*`）
 *
 * 用户给了设计稿（`F:\美术资源\桌宠右键菜单 UI.pdf`）要求菜单长成那样。
 * 而 Electron 的 `MenuItemConstructorOptions` 只有 20 个属性
 * （`click` / `role` / `type` / `label` / `submenu` / `icon` / `checked` …），
 * **一个都不是样式** —— 原生菜单做不出卡片圆角、iOS 式开关、图标和分区线。
 * 所以只能换成无边框透明窗里自绘。
 *
 * ### 退路：原生菜单（本文件里的 `popupPetMenu` / `toElectronTemplate`）
 *
 * ⚠️ **这条不是死代码**，它有两个活着的用途：
 *
 *   ① **应急退路** —— 自绘窗口是"加载一个 HTML 页面"，而页面加载可能失败
 *      （路径错、资源缺失、权限）。那时如果只有自绘一条路，表现就是
 *      "右键点了没反应" —— 用户既看不到菜单、也得不到任何提示，
 *      而这个菜单是**改设置 / 退出程序唯一的人口**（宠物窗口没有关闭按钮）。
 *      `index.ts` 的 `openPetMenu()` 在自绘失败时会调它。
 *   ② **它决定了 `action` 的语义** —— 见下面 `dispatchPetMenuAction` 的说明。
 *
 * ## 结构定义在 `petMenuTemplate.ts`（纯数据，可测试）
 *
 * 这个文件负责"把结构变成调用"：一条路绑给 Electron 的菜单项，
 * 另一条路由 `dispatchPetMenuAction` 由 IPC 触发。
 */

import { Menu, type BrowserWindow } from 'electron';
import {
  buildPetMenuTemplate,
  type PetMenuState,
  type PetMenuItem,
} from './petMenuTemplate.js';

export interface PetMenuHandlers {
  /**
   * ⚠️ `poke` 已随「戳一下」菜单项一起**删除**（用户要求）。
   *
   * 那个动作本身没消失 —— 直接点宠物本体走的是渲染层的同一条路径
   * （`pet:poke-request` → `pet.poke()`）。菜单里再放一项是重复。
   * 留着这个处理器类型只会让"菜单里有它吗"变得含混。
   */
  /**
   * ⚠️ **已删除**（2026-10-12）。
   *
   * 原来是「聊天…」那一项的处理器，而它的实现是
   * chat: () => void openSettings() —— **标签写着聊天、实际打开设置窗口**。
   * 用户要求把那一项换成「聊天模式」开关，所以这个 handler 一起删了。
   *
   * 留这段注释是为了让将来的人知道：菜单里曾经有过一个名字与行为不符的项，
   * 而那种看着对、实际不是正是本项目最难查的一类问题。
   */
  /** 切换对话框（对话条）的显隐 —— 用户要求菜单里能藏/显它 */
  toggleChat: () => void;
  /**
   * 对话条复位到右下角（2026-10-12）。
   *
   * ⚠️ **可选**（?）：老代码/测试里构造的 handlers 没有这一项，
   * 设成必填会让它们全部编译不过。
   */
  resetChat?: () => void;
  /** 切换气泡显隐（用户要求 2026-10-07：「可在宠物菜单设置隐藏和显示气泡」）*/
  toggleBubble: () => void;
  /**
   * 切换状态条显隐（2026-10-11）。
   *
   * ⚠️ **可选**（`?`）：老代码/测试里构造的 handlers 没有这一项，
   * 设成必填会让它们全部编译不过。缺省时菜单项点了没反应 ——
   * 可接受，因为它只是显隐一个附属窗口。
   */
  toggleStatus?: () => void;
  /** 沉浸模式（一级菜单，用户要求 2026-10-09） */
  /**
   * 沉浸模式。
   *
   * ⚠️ **要吃 `enabled`**（2026-10-12 修）。它原来是 `() => void` 且内部
   * 自己 `!immersiveMode`，而菜单项是 checkbox —— 于是走"无参数"分支，
   * `enabled === undefined`，`!undefined` 恒为 `true`，
   * 结果**只能开、关不掉**。
   */
  toggleImmersive: (enabled: boolean) => void;
  toggleVision: (enabled: boolean) => void;
  /**
   * 切换思考模式（DeepSeek 的 `thinking` 参数）。
   *
   * 用户要求 2026-10-07：「把思考模式做成开关，在宠物菜单界面显示，
   * 用户自己选择是否开启，默认开启」。
   */
  toggleThinking: (enabled: boolean) => void;
  /**
   * 聊天模式（2026-10-12 用户要求；2026-10-14 改为双向开关）。
   *
   * 用户原话：「将菜单中的聊天改成聊天模式，这是个开关，启动后将自动开启
   * 显示气泡、显示对话框」。起初按「开就将两个都显示，关闭不动任何东西」
   * 实现，但 2026-10-14 用户推翻了那一条：
   *
   * > 「聊天模式关闭之后的右边按钮开关关闭效果和图标文字效果还是要搞的，
   * >   我之前说错了，这些前端效果还是要有的」
   *
   * 所以现在**对称生效**：`true` -> 气泡+对话框都显示，`false` -> 都隐藏。
   * 因此它必须拿到 `enabled` 才能区分方向（见 `HANDLERS_TAKING_CHECKED_STATE`）。
   */
  toggleChatMode?: (enabled: boolean) => void;
  /**
   * 联网搜索模式（2026-10-16 用户要求：三选一 radio 子菜单）。
   *
   * ⚠️ 与 `scale`/`mic` 同类：action 是 `web-search-mode:<模式>`
   * （accurate / fast / off），分发时把冒号后的值传进来 ——
   * 处理器吃的是**模式字符串**，不是勾选状态。
   */
  webSearchMode?: (mode: string) => void;
  /**
   * 语音播报开关（2026-10-14 用户要求）。
   *
   * 用户原话：「把宠物的语音功能抽出来放在允许看屏幕上面做成和上面两个一样的
   * 开关，开启时能将宠物回复的文字转为语音说出，语音功能在设置语音中设置好的」。
   *
   * ⚠️ 与 `toggleChatMode` 一样**必须吃 `enabled`**：传 undefined 时
   * `Boolean(undefined) === false`，会把每次点击都当成"关闭"。
   */
  toggleTtsSpeech?: (enabled: boolean) => void;
  /**
   * 切换联网搜索（2026-10-12）。
   *
   * 用户要求「联网功能**默认关闭**，在**菜单里面**打开之后如果我让宠物去
   * 网上搜索相关信息，它要自己去搜索分辨出来再回答我」。
   *
   * ⚠️ **可选**（`?`）：老代码/测试里构造的 handlers 没有这一项，
   * 设成必填会让它们全部编译不过。缺省时那个勾点了没反应 ——
   * 可接受，因为它只是一个开关。
   */
  toggleWebSearch?: (enabled: boolean) => void;
  /**
   * 选定麦克风（用户要求 2026-10-08：「麦克风选项移到宠物菜单里面」）。
   *
   * `deviceId` 为空字符串 = 系统默认。
   */
  setMicDevice: (deviceId: string) => void;
  toggleVisible: () => void;
  togglePause: () => void;
  /** "边缘探头"开关 */
  toggleEdgePeek: (enabled: boolean) => void;
  /** 打开设置窗口去调探头参数 */
  peekSettings: () => void;
  setActivity: (value: number) => void;
  setPetScale: (scale: number) => void;
  /**
   * 「截图对话」（2026-10-17 用户要求）：
   * 「点击按钮或者按下快捷键，屏幕将自动截图并发给ai，
   *   接受图片后，玩伴的注意力会直接被拉到好奇状态，玩伴会顺着图片和用户聊天。」
   *
   * ⚠️ 它**不是**开关（不是 checkbox），所以**不要**登记到
   * `HANDLERS_TAKING_CHECKED_STATE` 里 —— 那是一张"点击后要吃勾选状态"的白名单，
   * 误登记会让处理器收到一个 `undefined`，行为随实现而定。
   */
  snapshotChat: () => void;
  openLogs: () => void;
  settings: () => void;
  /** 打开记忆管理窗口（独立窗口，与设置并列的入口） */
  memory: () => void;
  quit: () => void;
  quitAll: () => void;
}

/**
 * 把 action 名（kebab-case）转成处理器名（camelCase）。
 *
 * ---- ⚠️ 为什么必须有这一步（踩过，而且测试没抓到） ----
 *
 * 模板里的 action 写的是 `toggle-vision` / `toggle-edge-peek` / `quit-all` /
 * `scale:0.6`，而 `PetMenuHandlers` 的键是 `toggleVision` / `toggleEdgePeek` /
 * `quitAll` / `setPetScale`。**直接拿 kebab 名去查 camelCase 的表，永远查不到**，
 * 于是 17 个 action 里有 14 个被置灰成「（未实现）」——
 * 用户看到的就是"菜单全灰、点了没反应"。
 *
 * 为什么测试没抓到：`test-main.mjs` 验的是**模板结构**（有哪些项、勾选数、
 * 置灰项是否都标了未实现），从来没有真的拿主进程那份 handlers 去跑一遍
 * "每个 action 都能找到处理器"。已经补上那条断言（见 test-main 的
 * 「每个 action 都能在主进程的 handlers 里找到」）。
 *
 * 所以这里兼容两种写法：先按原样查（`chat` / `settings` / `quit` 这类本来就同名的），
 * 查不到再把 kebab 转 camel 查一次。
 */
export function handlerNameFor(action: string): string {
  const name = action.split(':')[0];
  if (!name.includes('-')) return name;
  return name.replace(/-([a-z])/g, (_m, ch: string) => ch.toUpperCase());
}

/**
 * 哪些处理器要吃「点击后的勾选状态」。
 *
 * ## 🔴 这是一张**必须手动维护**的白名单（2026-10-12 踩到）
 *
 * 菜单里每个 `type: 'checkbox'` 的项，它的处理器**必须**登记在这里。
 * 忘了登记就落进"无参数"分支：
 *
 *     handler()           ->  toggleWebSearch(undefined)
 *     Boolean(undefined)  ->  false
 *
 * 于是**每次点都是「关闭」，永远打不开** —— 而菜单看着完全正常
 * （Electron 自己会翻转勾选，看起来像点上了）。
 *
 * 用户报的正是：「这个联网搜索的开关没用，我点了开不了」。
 *
 * 这与 `ACTIONS_WITH_VALUE` 是同一类问题（动作名与处理器行为对不上），
 * 所以 `test-main.mjs` 里加了断言：**菜单里每个 checkbox 都必须在表里**。
 * 将来再加 checkbox 忘了登记，门禁会红。
 */
export const HANDLERS_TAKING_CHECKED_STATE: ReadonlySet<string> = new Set([
  'toggleVision',
  'toggleEdgePeek',
  // ⚠️ 用户没报、是门禁断言抓到的：它也是 checkbox，
  //    漏登记的表现是「点一次能开、再点还是开」——永远关不掉
  'toggleWebSearch',
  'toggleImmersive',
  // ⚠️ 同样是门禁抓到的：它吃 enabled，却漏登记了 ——
  //    点一下传 undefined，setThinking(false) 会让「思考模式」**关不掉**
  'toggleThinking',
  // ⚠️ 它现在是**双向**开关（开=都显示，关=都隐藏），但无论如何都必须
  //    拿到 `enabled`：传 undefined 时 `Boolean(undefined) === false`，
  //    会把每次点击都当成"关闭"。
  'toggleChatMode',
  // ⚠️ 语音播报（2026-10-14）同理：不登记的话每次点击都是"关闭"
  'toggleTtsSpeech',
]);

/**
 * `scale:0.6` / `activity:0.5` 这类"带参数"的 action，冒号前是**动作类别**，
 * 而对应的处理器名并不等于类别名（`scale` -> `setPetScale`、
 * `activity` -> `setActivity`）。所以带参数的 action 走这张别名表。
 *
 * 导出是为了让 `test-main.mjs` 能对照它做断言（那个测试就是为了防
 * "action 名与处理器名对不上导致整菜单置灰"这类回归）。
 */
export const ACTIONS_WITH_VALUE: Record<string, string> = {
  scale: 'setPetScale',
  activity: 'setActivity',  /**
   * `mic:<deviceId>` -> `setMicDevice`
   *
   * ⚠️ 必须登记在这张别名表里：`mic` 这个类别名与处理器名 `setMicDevice`
   * 不一致（和 `scale` -> `setPetScale` 同理）。漏了的话菜单项会被
   * **置灰成「未实现」** —— 而这个项目已经因为 action↔处理器名对不上
   * 整菜单灰过一次（见 `handlerNameFor` 的说明）。
   */
  mic: 'setMicDevice',
};

/**
 * action -> 处理器名。**`toElectronTemplate` 用的就是这一个函数。**
 *
 * ⚠️ 单独抽出来是刻意的：第一版我把映射写在 `toElectronTemplate` 函数体里、
 * 测试却单独去调 `handlerNameFor` —— 结果做探针（故意退回 bug 写法）时
 * 测试**照样全绿**，因为我改的是函数体、而测试验的是另一个函数。
 * 那种"测试通过但没覆盖运行时路径"的测试等于没写。
 * 现在映射只有这一处实现，测试调它就真的覆盖了运行时那条路径。
 */
export function resolveHandlerKey(action: string): string {
  const name = action.split(':')[0];
  return ACTIONS_WITH_VALUE[name] ?? handlerNameFor(action);
}



/**
 * 把纯结构翻译成 Electron 的菜单模板。
 *
 * 每个带 `action` 的项都要在 `handlers` 里有对应处理 —— **找不到就置灰**，
 * 而不是留一个点了没反应的项。这条是刻意的：这个项目已经出过好几次
 * "界面上写着、实际没实现"的问题，那种失败最难排查（不报错，只是什么都不发生）。
 */
/**
 * 把菜单结构翻译成 Electron 模板（**纯函数**）。
 *
 * ⚠️ 导出是**为了测试**：click 到底怎么调的（传数字？传字符串？还是不传）
 * 只有驱动这个函数才验得到。这条分支曾经吃掉过麦克风菜单 ——
 * mic:<deviceId> 的值不是数字，落进了无参数分支，
 * 于是**点任何一支麦克风都会跳回「系统默认」**（见下面那条 
awValue 分支的说明）。
 *
 * ⚠️ 真正的弹窗在 popupPetMenu 里：那一步需要真实的 BrowserWindow，
 * 在 Node 测试里跑不起来 —— 所以**结构**和**弹窗**要分开验。
 */
export function toElectronTemplate(
  items: PetMenuItem[],
  handlers: PetMenuHandlers,
  missing: string[],
): Electron.MenuItemConstructorOptions[] {
  return items.map((item) => {
    const out: Electron.MenuItemConstructorOptions = {};
    if (item.label !== undefined) out.label = item.label;
    if (item.type) out.type = item.type;
    if (item.checked !== undefined) out.checked = item.checked;
    if (item.enabled !== undefined) out.enabled = item.enabled;

    if (item.submenu) {
      out.submenu = toElectronTemplate(item.submenu, handlers, missing);
      return out;
    }

    const action = item.action;
    if (!action) return out;

    // action 形如 "scale:1.3" / "activity:0.5"，冒号后是参数
    const [name, rawValue] = action.split(':');
    // ⚠️ 查表用的是**处理器名**，不是 action 名：走 `resolveHandlerKey`
    // （kebab -> camel，且带参数的 action 走别名表）。它同时被测试调用，
    // 所以这里一旦写错，测试会立刻变红。
    const handlerKey = resolveHandlerKey(action);
    // 用 unknown 中转再查表：`PetMenuHandlers` 与 `Record<string, unknown>`
    // 形状不重叠，直接断言会被 TS 判为可疑转换。
    const handler = (handlers as unknown as Record<string, unknown>)[handlerKey];
    if (typeof handler !== 'function') {
      missing.push(action);
      out.enabled = false;
      out.label = `${item.label ?? name}（未实现）`;
      return out;
    }

    const value = rawValue === undefined ? undefined : Number(rawValue);
    if (value !== undefined && Number.isFinite(value)) {
      (out as { click: () => void }).click = () => (handler as (v: number) => void)(value);
    } else if (HANDLERS_TAKING_CHECKED_STATE.has(handlerKey)) {
      /*
       * ## ⚠️ checkbox 的点击要用**点击后的**勾选状态
       *
       * 不能取反当前值 —— Electron 在调 click **之前**已经把 `checked` 翻转了，
       * 所以 `menuItem.checked` 就是"点击之后的新状态"。
       *
       * ## 🔴 这是一张**必须手动维护**的白名单（2026-10-12 踩到）
       *
       * 新增 checkbox 时**忘了登记**，它就会落进最后那个"无参数"分支：
       *
       *     handler()        ->  setWebSearch(undefined)
       *     Boolean(undefined) -> false
       *
       * 于是**每次点都是"关闭"，永远打不开** —— 而菜单看着完全正常
       * （勾会被 Electron 自己翻转，看起来像点上了）。
       * 用户报的正是「这个联网搜索的开关没用，我点了开不了」。
       *
       * 这正是本项目最怕的那类故障：**不报错、只是不能用**。
       *
       * 所以抽成一张常量集合放在文件顶部，并在 `test-main.mjs` 里加断言：
       * **菜单里每个 checkbox 都必须在这张表里**。
       */
      (out as { click: (i: Electron.MenuItem) => void }).click = (menuItem: Electron.MenuItem) =>
        (handler as (v: boolean) => void)(menuItem.checked);
    } else if (rawValue !== undefined) {
      /*
       * ## ⚠️ 带值但**不是数字**的 action（2026-10-08 加，否则麦克风菜单整个失效）
       *
       * `mic:default` / `mic:<deviceId>` 里的值是**字符串**。
       * 落到下面那个"无参数"分支会怎样：
       *
       *     Number('default')  ->  NaN   // isFinite 为假 -> 不走数字分支
       *     handler()          ->  setMicDevice(undefined)
       *     deviceId === 'default' ? '' : String(undefined ?? '')  ->  ''
       *
       * 于是**点任何一支麦克风都会变成「系统默认」** ——
       * 菜单看着好好的、点了也"有反应"（勾选会跳到系统默认），
       * 但用户要的那支永远选不上。这正是本项目最怕的
       * "不报错、只是不能用"。
       *
       * 所以这里补一条：**原样把字符串传下去**。
       */
      (out as { click: () => void }).click = () => (handler as (v: string) => void)(String(rawValue));
    } else {
      (out as { click: () => void }).click = () => (handler as () => void)();
    }
    return out;
  });
}

/**
 * 在宠物窗口上弹出菜单。
 *
 * 返回"没有处理器的 action 列表"，便于调用方记日志（正常情况下应为空）。
 */
export function popupPetMenu(
  window: BrowserWindow,
  handlers: PetMenuHandlers,
  state: PetMenuState,
): string[] {
  const missing: string[] = [];
  const template = toElectronTemplate(buildPetMenuTemplate(state), handlers, missing);
  const menu = Menu.buildFromTemplate(template);
  menu.popup({ window });
  return missing;
}

/* ══════════════════════════════════════════════════════════════════════════
 * 自绘菜单的点击分发（2026-10-14）
 *
 * ## 为什么需要它
 *
 * 菜单从原生菜单换成了**自绘窗口**（`petMenuWindow.ts` + `renderer/petMenu.*`）。
 * 原生菜单那边，`toElectronTemplate` 会把 `click` 直接绑到处理器上；
 * 现在点击只能从渲染层通过 IPC 报回来一个 `action` 字符串，
 * 于是**需要一处把 `action` 翻回处理器调用**的地方。
 *
 * ## ⚠️ 判据必须与 `toElectronTemplate` **完全一致**
 *
 * 两个函数做的是同一件事（"把 action 变成一次处理器调用"），只是一个
 * 绑给 Electron 的菜单项、一个由 IPC 触发。它们**必须对所有 action 给出
 * 同样的结论** —— 否则会出现"原生菜单能点、自绘菜单点了没反应"这种
 * 只在其中一条路上出现的故障（而这类"两条路不一致"的问题查起来最费劲）。
 *
 * 所以两者共用同一套判据（`resolveHandlerKey` / `HANDLERS_TAKING_CHECKED_STATE`
 * / `ACTIONS_WITH_VALUE`），且 `test-main.mjs` 里对**同一个 action 集合**
 * 同时驱动这两个函数、断言结论一致。
 *
 * 返回 `false` = 这个 action 没有对应处理器（调用方应记日志）。
 */
export function dispatchPetMenuAction(
  handlers: PetMenuHandlers,
  action: string,
  checked?: boolean,
): boolean {
  const handlerKey = resolveHandlerKey(action);
  const handler = (handlers as unknown as Record<string, unknown>)[handlerKey];
  if (typeof handler !== 'function') return false;

  const [, rawValue] = action.split(':');
  const value = rawValue === undefined ? undefined : Number(rawValue);

  /*
   * 三个分支的**顺序与 `toElectronTemplate` 一致**，不能换：
   * 数字值 -> 开关类（吃 checked）-> 非数字的字符串值 -> 无参数。
   */
  if (value !== undefined && Number.isFinite(value)) {
    (handler as (v: number) => void)(value);
  } else if (HANDLERS_TAKING_CHECKED_STATE.has(handlerKey)) {
    /*
     * ⚠️ 开关类必须**显式**吃到"切换后的状态"。
     * 渲染层会把它一起报上来；万一没报（undefined），`Boolean(undefined)`
     * 恒为 `false` —— 那会让开关**只能关不能开**（原生菜单那一版真的踩过，
     * 见 `HANDLERS_TAKING_CHECKED_STATE` 的说明）。
     */
    (handler as (v: boolean) => void)(Boolean(checked));
  } else if (rawValue !== undefined) {
    /* `mic:default` / `mic:<deviceId>`：值不是数字，要**原样**传下去 */
    (handler as (v: string) => void)(String(rawValue));
  } else {
    (handler as () => void)();
  }
  return true;
}
