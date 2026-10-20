/**
 * 宠物右键菜单的**纯结构**（不依赖 electron）。
 *
 * 拆出来的理由：菜单结构是纯数据，可以断言"有哪些项、层级对不对、
 * 置灰的是不是都标了未实现"。而 `petMenu.ts` 要 import electron，
 * 在纯 Node 测试里加载不了。
 */

// 大小档位的**唯一真源**：设置页下拉框、托盘菜单、这里三处共用。
// 改之前三处各写一份且互不一致（设置页连续滑杆 vs 菜单六档硬编码）。
import { PET_SCALE_PRESETS, petSizeFor } from './petLayout.js';

/**
 * 「未实现」后缀（2026-10-12 起**没有使用点了**，保留是为了测试的断言）。
 *
 * 菜单里原来有 5 个置灰占位项（拖拽物理 / 禁止移动 / 吸附回角落 / 开机自启 / 置顶），
 * 用户 2026-10-12 要求「这些未实现的功能都删除了」——
 * 保留它们的理由是"让用户看到以后会有这些"，但用户明确不要，所以删了。
 *
 * ⚠️ 常量本身**不要删**：`test-main.mjs` 用它断言两件仍然有意义的事 ——
 *   · 所有置灰项都必须标明「未实现」（将来再加占位项时不会漏标）
 *   · 标了「未实现」的必须真是置灰的（防"写了却说没实现"的矛盾）
 */
export const TODO_SUFFIX = '·未实现';

/** 菜单项结构（自己的类型，避免为了它去引 electron） */
export interface PetMenuItem {
  label?: string;
  type?: 'separator' | 'checkbox' | 'radio' | 'normal';
  checked?: boolean;
  enabled?: boolean;
  submenu?: PetMenuItem[];
  /** 点击时的动作标识；置灰项没有 */
  action?: string;
  /**
   * 图标名（2026-10-14）。
   *
   * ## 为什么图标名放在**结构**里而不是渲染层自己按 label 猜
   *
   * 自绘菜单（`src/renderer/petMenu.js`）需要给每一行配图标，而设计稿的图标
   * 是**逐项指定**的（月亮=沉浸、气泡=聊天、眼睛=看屏幕、爪=大小、
   * 手柄=桌宠控制、麦克风、星=活泼度、书=记忆、太阳=设置、电源=退出、
   * 复制=退出全部实例）。
   *
   * 如果让渲染层按 `label` 去猜，那**同一份菜单就有了第二个真相源** ——
   * 将来改文案（这个菜单改过很多次文案）会让图标悄悄错位或消失，
   * 而且不报错。放在这里，改菜单的人一定会看到它。
   *
   * 取值必须与 `src/renderer/petMenu.icons.css` 里的 `[data-icon=...]` 一致
   * （那份是 `tools/build-icon-css.py` 从设计稿 PDF 抠出来的**真图标**）。
   */
  icon?: string;
  /**
   * 右侧的**数值**（2026-10-14）。
   *
   * 设计稿里「活泼度」那行是「活泼度」+ 右侧 `0.60` + 箭头 —— 数值**单独占一格**、
   * 右对齐、灰色，而不是拼进标题里。所以这里给它一个字段，
   * 而不是继续把数字写进 `label`（写进 label 的话，渲染层只能靠正则去拆，
   * 那既脆弱又会把「宠物大小（300 px）」这类带数字的标签误伤）。
   */
  value?: string;
  /**
   * 分隔线是否为**强调色**（品牌蓝）。
   *
   * 设计稿里「设置…」上面那条是唯一一条蓝色的分隔线 —— 把上面"日常开关"
   * 与下面"设置 / 退出"分开。不还原的话整块菜单会显得没有分区。
   */
  accent?: boolean;
}

/**
 * 联网搜索的三种模式（2026-10-16）。
 *
 * 导出是因为 `index.ts` 的状态变量、`setWebSearchMode` 的参数校验
 * 都要用同一份定义 —— 字符串字面量写两份必然漂移。
 */
export type WebSearchMode = 'accurate' | 'fast' | 'off';

/** 三种模式的全集（子菜单按它生成，顺序即显示顺序）。 */
export const WEB_SEARCH_MODES: readonly WebSearchMode[] = ['accurate', 'fast', 'off'];

/**
 * 各模式在菜单里的文案。
 *
 * ⚠️ 预计时间写死在文案里是有意的：它们来自 2026-10-16 的 A/B 实测
 * （精准 10~20 秒、快速约 2 秒），是"给用户的承诺"而不是会变的配置 ——
 * 后端性能变了应该来这里改文案，而不是把变量塞进 UI。
 *
 * ⚠️ 刻意**不写引擎名**（free-search / websearch-mcp / DeepSeek）：子菜单面板宽度
 * 固定，长标签会被截成半截（用户截图里父项的"约 1…"就是教训）。
 * 唯一的例外是下面那条**灰色说明行**（"仅 DeepSeek API 可用"），
 * 它是用户 2026-10-19 明确要求标注的**前提条件**，不是实现细节。
 */
export const WEB_SEARCH_LABEL: Record<WebSearchMode, string> = {
  accurate: '精准搜索（约 10~20 秒）',
  fast: '快速搜索（约 2 秒）',
  off: '关闭联网搜索',
};

/**
 * 「快速搜索」下面那行**灰色说明**（2026-10-19 加）。
 *
 * 用户原话：「接入到联网搜索里面，代替第二个搜索，并且标注一下
 * 只有 deepseekapi 才能使用此功能」。
 *
 * ⚠️ 为什么用 disabled 菜单项而不是把字塞进 label：
 *   · label 变长会被窄面板截断（见上一条注释）
 *   · 独立一行、灰色、不可点 —— 语义上就是"这是备注，不是选项"
 *   · 渲染层已经把 `enabled: false` 处理成 `data-disabled`（不可点、变灰）
 */
export const WEB_SEARCH_FAST_NOTE = '仅 DeepSeek API 可用';

export interface PetMenuState {
  visible: boolean;
  paused: boolean;
  visionWatching: boolean;
  activity: number;
  petScale: number;
  /** "边缘探头"开关（拖到屏幕左/右边缘后倾斜 45° 窥视） */
  edgePeek: boolean;
  /**
   * 对话框（对话条）当前是否可见。
   *
   * 用户要求：菜单里能"点击隐藏对话框，文字变成显示对话框，再点击显示"。
   * 所以这一项是**文字会变的普通菜单项**（不是 checkbox）——
   * 用户描述的就是"点击后文字变"这个行为。
   */
  chatVisible: boolean;
  /**
   * 气泡是否允许显示（宠物菜单里的开关）。
   *
   * ⚠️ 语义是**开关**而非"当前可见"：`false` = 以后都不自动弹。
   */
  bubbleVisible: boolean;
  /**
   * 状态条是否显示（2026-10-11 加）。
   *
   * 状态条是贴屏幕左/右边缘的竖条，显示「颜色灯 + 竖排文字」；
   * 联网搜索时灯变黄、文字变「联网搜索中...」。
   */
  statusVisible: boolean;
  /**
   * 沉浸模式（用户 2026-10-09 要求）。
   *
   * 打开时**自动隐藏对话框和气泡**，只留语音 —— 适合打游戏/看片时不想被文字挡。
   *
   * ⚠️ **例外**：TTS 关了或合成失败时**仍然显示文字**
   * （用户明确要求"显示错误文本"）—— 否则用户既听不到也没得看。
   *
   * ⚠️ 这是 **checkbox**（和「边缘探头」一致）：它是真正的二态开关，
   * 用户要能一眼看出当前状态。别和"文字随状态变"那两个混为一谈。
   */
  immersiveMode: boolean;
  /**
   * 思考模式（DeepSeek 的 `thinking` 参数）。
   *
   * 用户要求 2026-10-07：「把思考模式做成开关，在宠物菜单界面显示，
   * 用户自己选择是否开启，**默认开启**」。
   *
   * ⚠️ 这是 **checkbox 而不是"文字会变"** —— 与气泡/对话框那两个刻意不同。
   * 理由：它是**真正的二态开关**（开/关），用户要能一眼看出"现在是开还是关"，
   * 而"隐藏气泡"那种文案切换在二态开关上反而绕。
   * 参考同一个菜单里的「边缘探头」也是 checkbox，形态一致。
   */
  thinking: boolean;
  /**
   * 联网搜索**模式**（2026-10-16 用户要求，三选一）。
   *
   * 用户原话：「联网搜索功能可以变成这种可显示选择模式的方式，第一种
   * 准确搜索以 free-search 为主来搜索，写上搜索预计时间，第二种是快速
   * 国内外搜索，使用 websearch-mcp 进行搜索，写上搜索预计时间，第三种
   * 是关闭联网搜索」。
   *
   * ## 为什么从 checkbox 改成 radio 子菜单
   *
   * 两种搜索的**耗时差 10 倍**（精准 10~20 秒 / 快速约 2 秒），结果质量
   * 也明显不同 —— 用户应该按当下需求选，而不是记住"哪种实现"这种细节。
   * 形态照抄「宠物大小」「麦克风」的 radio 子菜单（单选、当前项打勾）。
   *
   * 真源是 `persona.yaml` 的 `tools.web_search_mode`；Python 侧按模式
   * 切换搜索后端（见 `tools.py` 的 `set_search_mode`）。
   */
  webSearchMode: WebSearchMode;
  /**
   * 「语音播报」开关（2026-10-14 用户要求）。
   *
   * 真源是 `persona.yaml` 的 `providers.tts.enabled`；引擎/音色在
   * 「设置 → 语音」里配。关掉它宠物只显示文字，不出声。
   */
  ttsSpeechEnabled: boolean;
  /**
   * 当前选定的麦克风 `deviceId`（空 = 系统默认）。
   *
   * 用户要求 2026-10-08：「麦克风选项移到宠物菜单里面」。
   */
  micDeviceId: string;
  /**
   * 可用麦克风列表（用户能显式选，而不是只能靠"系统默认"）。
   *
   * ## ⚠️ 为什么必须能选（实测 2026-10-08）
   *
   * 用户的 USB 麦克风「说话没用」。实测（同一支 USB 麦、同样说话 3 秒）：
   *
   *     Chromium 的 default 别名   peak 0.0205  判为语音  3/83   ❌
   *     指定真实 deviceId          peak 0.1413  判为语音 19/86   ✅
   *
   * **差 7 倍**，而那个别名显示的名字和真设备**一模一样** ——
   * 用户从界面上完全看不出自己用的不是那一支。
   *
   * ⚠️ 列表可能为空：枚举要麦克风权限，而权限只在**宠物窗口**开过
   * （`listMicrophones` 就是在那儿调的）。空列表时菜单项会标「（未枚举）」，
   * 提示用户还没授权 —— 比给一个空子菜单好。
   */
  micDevices: { deviceId: string; label: string }[];
}

/** 浮点比较：滑杆给的值有误差，直接用 === 会让单选项全部不勾 */
export function near(a: number, b: number): boolean {
  return Math.abs(a - b) < 0.01;
}

/**
 * 构建菜单结构。
 *
 * 层级照抄参考那个桌宠应用（`F:\ref-pet\_internal\pet\menu_templates\`）：
 * 一级是常用动作，成组的项收进子菜单。
 *
 * **未实现的功能照原样列出但置灰**，理由是：
 *   · 结构照抄 —— 一眼能看出完整的产品形态；
 *   · 置灰而不是隐藏 —— 用户看到"以后会有这些"，而不是以为漏了；
 *   · 置灰而不是可点 —— **点了没反应比看不到更糟**（踩过坑：界面上写着却
 *     从未实现的东西，用户只会以为程序坏了）。
 */
export function buildPetMenuTemplate(state: PetMenuState): PetMenuItem[] {
  // 大小档位的唯一真源（设置页/托盘/右键菜单三处共用，避免清单漂移）
  const scaleOption = (label: string, value: number): PetMenuItem => ({
    label,
    type: 'radio',
    checked: near(state.petScale, value),
    action: `scale:${value}`,
  });

  /**
   * 活泼度的一个档位。
   *
   * `hint` 是**右侧灰字**（复用 `value` 那格）：档位名要短、要好选，
   * 塞不下的一两句解释放右边，比把名字拼成一长串好读。
   */
  /**
   * 活泼度四档 —— **权威定义**（2026-10-17 用户口述，原话要点）。
   *
   * ```
   * 安静    玩伴不怎么说话，也不会自主截图
   * 无聊    会自己想自己要干什么（看小说 / 和用户聊天打游戏 /
   *         看用户打游戏顺便解说），偶尔发呆
   * 好奇    部分注意力在用户身上，比较好奇用户在干什么，
   *         偶尔搭话或截图和用户聊天
   * 自言自语 神神叨叨，嘴里念着道经 / 圣经 / 佛经 / 时不时搜哲学句子学习体会；
   *         也会和用户搭话，但大部分时间在讲自己读了什么、觉得什么；
   *         注意力掉得很快，20 分钟不到就回到安静（情绪发泄完了）
   * ```
   *
   * ⚠️ 两处容易搞错：
   *   ① **「无聊」是 0.5 那档**（老文案写的「普通」），不是新的第五档。
   *   ② 「自言自语」的自主截图频率**和「好奇」差不多**（用户 2026-10-17 补），
   *      不是更频繁 —— 老设计说"更频繁"，作废。
   */
  const ACTIVITY_TIERS: Array<{ name: string; value: number }> = [
    { name: '安静', value: 0.1 },
    { name: '无聊', value: 0.5 },
    { name: '好奇', value: 0.8 },
    { name: '自言自语', value: 1.0 },
  ];

  const activityOption = (label: string, value: number, hint = ''): PetMenuItem => ({
    label,
    ...(hint ? { value: hint } : {}),
    type: 'radio',
    checked: near(state.activity, value),
    action: `activity:${value}`,
  });

  /**
   * 麦克风单选项（用户要求 2026-10-08：「麦克风选项移到宠物菜单里面」）。
   *
   * ⚠️ **用 `radio` 而不是 `checkbox`**：麦克风是**多选一**（同一时刻只能用一支），
   * checkbox 会让用户以为能同时开好几支。
   *
   * ⚠️ `action` 里带 `deviceId`（形如 `mic:<64 位十六进制>`）—— deviceId 不含
   * 冒号，所以按第一个冒号切分是安全的。
   *
   * ## ⚠️ 空值必须用**哨兵** `default`，不能留空（测试当场抓到的 bug）
   *
   * 第一版「系统默认」那项写的是 `mic:${''}` —— 拼出来就是 `"mic:"`，而
   * `resolveHandlerKey` 里是 `action.split(':')[0]`：
   *
   *     "mic:".split(':')  ->  ['mic', '']   // 看着没问题…
   *     "mic:".split(':')[0] -> 'mic'        // 也对啊？
   *
   * 但 `collectActions` / 断言那边拿到的是**整串** `"mic:"`，
   * 于是 `HANDLER_KEYS.includes('mic:')` 落空 -> 这一项被判为"找不到处理器"
   * -> **被置灰成「未实现」**。
   *
   * 用显式哨兵 `default` 之后，action 是 `mic:default`（非空、可读、可断言），
   * 处理器里再把 `default` 翻译成空串（= 让浏览器用系统默认设备）。
   */
  const micOption = (label: string, deviceId: string): PetMenuItem => ({
    label,
    type: 'radio',
    checked: state.micDeviceId === deviceId,
    action: `mic:${deviceId || 'default'}`,
  });

  return [
    // 「戳一下」已**删除**（用户要求）。
    // 它本来是个快捷键，但直接点宠物本体就是同一个动作（`pet:poke-request`
    // 走的就是渲染层那条路径），菜单里再放一项是重复。
    {
      /*
       * 沉浸模式（用户要求 2026-10-09）：
       * 「设置个游戏或沉浸模式，自动隐藏对话框和气泡，只用语音来聊天回答，
       *   不显示气泡，tts 失败或关了显示错误文本」
       *
       * ## 为什么放**一级菜单**
       *
       * 用户明确要求。而且语义上它对：这是"**临时进一个状态**"的开关 ——
       * 打游戏前想一键切，藏进二级菜单就多两步。
       * （「隐藏对话框」「显示气泡」是细粒度控制，留在「桌宠控制」里。）
       *
       * ## 为什么是 checkbox
       *
       * 和「边缘探头」一致 —— 真正的二态开关，勾选态一眼看出当前是不是沉浸中。
       * 它同时会**接管**对话框和气泡的显隐，所以"当前状态"很重要。
       *
       * ## ⚠️ 标签里写清"例外"
       *
       * 光写「沉浸模式」用户不知道它会动什么、也不知道语音失败时会怎样
       * （和「思考模式」那一项同理 —— 那个把"更慢更贵"写进了标签）。
       * 但标签不能太长（会撑宽整个菜单），所以例外放在这里、不在标签里。
       */
      label: '沉浸模式（隐藏文字，只听语音）',
      type: 'checkbox',
      checked: state.immersiveMode,
      action: 'toggle-immersive',
      // 设计稿的月亮图标（开启时是黄色，见 renderer 的 petMenu.css）
      icon: 'moon',
    },
    /*
     * 聊天模式（2026-10-12 用户要求；2026-10-14 改为双向开关）。
     *
     * ## 用户原话
     *
     * 「将菜单中的聊天改成聊天模式，这是个开关，启动后将自动开启
     * 显示气泡、显示对话框」。
     *
     * ## 🔴 2026-10-14 更正：不再是"只单向生效"
     *
     * 用户原话：「聊天模式关闭之后的右边按钮开关关闭效果和图标文字效果
     * 还是要搞的，我之前说错了，这些前端效果还是要有的」。
     *
     * 于是它变成**对称的双向开关**：
     *
     *   · 打勾（开）-> 气泡 + 对话框**都显示**
     *   · 取消勾选（关）-> 气泡 + 对话框**都隐藏**
     *
     * ⚠️ 旧代码里"关的时候什么都不做"当时是用户点的头，现在被推翻了 ——
     * 别再把 `setChatMode(false)` 改回直接 return，否则开关又会"关不掉"。
     *
     * ## 勾的状态 = "两个是不是都已经显示"
     *
     * 所以下面那两项被单独关掉任意一个时，这里的勾会**自己取消** ——
     * 那是诚实的：此刻它并不处于"聊天模式已开启"的状态。
     *
     * `checked: bubbleVisible && chatVisible` —— 两个都显示才算开着。
     *
     * ## ⚠️ 它原来叫「聊天…」，而且是个 bug
     *
     * 那两项的处理器是 `chat: () => void openSettings()` ——
     * **标签写着"聊天"，实际打开的是设置窗口**。用户这次要求换掉它，
     * 正好把这个误导性的项删了（设置窗口在菜单里本来就有「设置…」）。
     */
    {
      label: '聊天模式',
      type: 'checkbox',
      checked: state.bubbleVisible && state.chatVisible,
      action: 'toggle-chat-mode',
      icon: 'chat',
    },
    {
      /*
       * 语音播报（2026-10-14 用户要求）。
       *
       * ## 用户原话
       *
       * 「把宠物的语音功能抽出来放在允许看屏幕上面做成和上面两个一样的
       * 开关，开启时能将宠物回复的文字转为语音说出来，语音功能在设置
       * 语音中设置好的」。
       *
       * ## 位置与形态
       *
       * 按用户要求放在「允许看屏幕」**上面**，与沉浸模式 / 聊天模式同款
       * 二态开关（checkbox + 右侧开关）。
       *
       * ## ⚠️ 它只管"说不说"，引擎在设置里配
       *
       * 真源是 `persona.yaml` 的 `providers.tts.enabled`，与设置页的
       * 「合成方式」（provider / 音色）**正交**：那边选引擎，这边管开关。
       * 关掉只是"暂时不想听"，文字气泡照常显示（sidecar 的门控见
       * `voice.py::_on_pet_say_end_speak`）。
       */
      label: '语音播报',
      type: 'checkbox',
      checked: state.ttsSpeechEnabled,
      action: 'toggle-tts-speech',
      icon: 'speaker',
    },
    {
      // 标签刻意短：菜单宽度由**最长的那个标签**决定，括号里的说明会白白把
      // 整个菜单撑宽（Windows 原生菜单不换行、也不按内容自适应收窄）。
      // 完整含义放在托盘菜单的同名项里，那里空间够。
      //
      // 这一项是 checkbox，勾选状态 = 用户意愿（`settingsStore.visionEnabled`），
      // 与设置页的「允许偶尔截图」开关**同一个真源**，两边联动。
      //
      // ⚠️ 名字改过（2026-10-17）：老文案「允许看屏幕」听起来像"它能一直看"，
      // 实际语义是**允许它偶尔自己截一帧**（§115 第 1 条拍板的名字）。
      label: '允许偶尔截图',
      type: 'checkbox',
      checked: state.visionWatching,
      action: 'toggle-vision',
      icon: 'eye',
    },

    {
      // 「外观」改名「宠物大小」（用户要求）。
      // 原来下面还有个多余的「大小 xxx px」中间层，现在直接把档位放出来 ——
      // 鼠标停上去就能选，少点一层。
      //
      // ⚠️ 设计稿里这一组（宠物大小 / 桌宠控制）**紧跟在三个开关下面、
      //    上面没有分隔线**；分隔线只在「桌宠控制」和「麦克风」之间。
      //    原来这里多了一条 —— 照设计稿去掉，否则菜单被切成四段。
      label: '宠物大小',
      icon: 'paw',
      submenu: PET_SCALE_PRESETS.map((preset) =>
        scaleOption(`${preset.label}（${petSizeFor(preset.scale)} px）`, preset.scale),
      ),
    },

    {
      label: '桌宠控制',
      icon: 'pad',
      submenu: [
        { label: state.visible ? '隐藏宠物' : '显示宠物', action: 'toggle-visible' },
        {
          /*
           * 用户要求：「点击隐藏对话框，对话框就隐藏，然后文字就变成显示对话框，
           * 之后再点击可以显示对话框」。
           *
           * 所以这是**文字随状态切换的普通项**，刻意不是 checkbox ——
           * 用户描述的就是"文字会变"这个行为，加了勾反而多一层。
           *
           * ## ⚠️ 沉浸模式托管时置灰（2026-10-10 修，用户实测报的问题）
           *
           * 用户截图里出现「显示对话框」而沉浸模式刚把它隐藏了 ——
           * **标签和用户看到的实际不一致**，而且点了也没用
           * （沉浸模式的门控优先级更高，见 `decideBubble`）。
           *
           * 所以沉浸模式开着时：**置灰 + 说清"谁在管"**。
           *
           * ⚠️ 为什么置灰而不是**隐藏**：隐藏会让菜单项跳来跳去，
           * 用户找不到刚才点过的那个；置灰 + 明确文案 = "它被别的东西管着"，
           * 用户知道去哪儿改（关掉沉浸模式）。
           */
          label: state.immersiveMode
            ? '沉浸模式正在托管对话框'
            : state.chatVisible
              ? '隐藏对话框'
              : '显示对话框',
          enabled: !state.immersiveMode,
          action: 'toggle-chat',
        },
        {
          /*
           * 对话条复位（2026-10-12 用户要求可拖拽）。
           *
           * 用户能拖动对话条之后就得有办法**撤销自己的拖动** ——
           * 否则拖到屏幕角落、或者换显示器之后位置不合适，
           * 只能手动再拖回来。而"拖到一个已经看不见的地方"正是
           * 拖拽功能最常见的死局。
           *
           * 隐藏对话框时置灰：那时条都看不见，复位没有意义。
           */
          label: '对话条复位到右下角',
          enabled: state.chatVisible,
          action: 'reset-chat',
        },
        {
          /*
           * 气泡显隐（用户要求 2026-10-07）：
           * 「可在宠物菜单设置隐藏和显示气泡」
           *
           * ⚠️ 与"隐藏对话框"同一个形态：**文字随状态切换**，不是 checkbox。
           * 理由同上 —— 用户描述的就是"文字会变"。
           *
           * ⚠️ 语义是**开关**（关掉之后不再自动弹），不是"把当前这个藏起来"：
           * 关掉之后即使宠物继续说话也不会再弹气泡；重新打开才恢复。
           * 关的时候顺手把当前那个也藏了（否则"我关了它还在"很怪）。
           */
          /*
           * ⚠️ 沉浸模式托管时置灰（同上，用户实测报的问题）。
           *
           * 这一项尤其容易误导：沉浸模式下本来就不弹气泡，
           * 点「隐藏气泡」-> `bubbleVisible=false` -> **用户看不出任何变化**
           * （沉浸模式的门控优先级更高）。点了"没反应"比置灰更让人困惑。
           */
          label: state.immersiveMode
            ? '沉浸模式正在托管气泡'
            : state.bubbleVisible
              ? '隐藏气泡'
              : '显示气泡',
          enabled: !state.immersiveMode,
          action: 'toggle-bubble',
        },
        {
          /*
           * 状态条显隐（2026-10-11）。
           *
           * 与气泡同理：**托盘里也要有这一项**，否则用户"藏了宠物 + 藏了状态条"
           * 之后就再也点不到这个菜单，只能去改配置文件。
           *
           * ⚠️ 是**文字会变的普通项**（不是 checkbox）—— 与「隐藏气泡」同一风格：
           * 显示当前"点了会发生什么"，而不是"当前是什么状态"。
           */
          label: state.statusVisible ? '隐藏状态条' : '显示状态条',
          action: 'toggle-status',
        },
        {
          label: state.paused ? '恢复（取消免打扰）' : '免打扰 30 分钟',
          action: 'toggle-pause',
        },
        { type: 'separator' },
        {
          // 参考应用把它做成可关的开关：有人受不了宠物突然换姿态。
          // 放在「桌宠控制」里，因为它属于"宠物怎么动"这一类。
          label: '边缘探头（拖到屏幕边缘时侧身窥视）',
          type: 'checkbox',
          checked: state.edgePeek,
          action: 'toggle-edge-peek',
        },
        { type: 'separator' },
        {
          /*
           * 思考模式（用户要求 2026-10-07：「做成开关，在宠物菜单界面显示，
           * 用户自己选择是否开启，默认开启」）。
           *
           * ## ⚠️ 为什么标签要写这么长
           *
           * 光写「思考模式」用户不知道那是什么、也不知道代价。
           * 而这一项**实测差 92% 的钱**（每 1000 轮 ¥1.56 vs ¥0.12），
           * 所以必须让用户看得见"开着会慢、会贵"。
           *
           * 放在「桌宠控制」而不是新开一层：它和"宠物大小""边缘探头"一样，
           * 属于"宠物怎么表现"这一类设置。
           */
          label: state.thinking
            ? '思考模式：开（回答更稳，但更慢更贵）'
            : '思考模式：关（快、省，回答更随性）',
          type: 'checkbox',
          checked: state.thinking,
          action: 'toggle-thinking',
        },
        {
          // 打开设置窗口去调「斜着出去多少像素」。
          // 做成独立菜单项而不是藏在通用「设置…」里：用户是在探头效果不满意时
          // 才想调它，从这里直达比"先想清楚它在哪个分类下"省事。
          label: '边缘探头参数…',
          action: 'peek-settings',
        },
      ],
    },

    /*
     * ⚠️ 「桌宠控制」与「麦克风」之间那条分隔线是**设计稿的位置**
     * （把"宠物怎么表现"和"它对外的通道/活跃度"分开）。
     */
    { type: 'separator' },

    {
      label: `麦克风${state.micDevices.length ? '' : '（未枚举）'}`,
      icon: 'mic',
      submenu: [
        micOption('（系统默认）', ''),
        ...state.micDevices.map((m) => micOption(m.label, m.deviceId)),
      ],
    },

    {
      /*
       * 联网搜索（2026-10-16 用户要求）：三选一的 radio 子菜单。
       *
       * 用户原话：「第一种准确搜索以 free-search 为主来搜索，写上搜索
       * 预计时间，第二种是快速国内外搜索，使用 websearch-mcp 进行搜索，
       * 写上搜索预计时间，第三种是关闭联网搜索」。
       *
       * ## 🔴 为什么放在**一级**菜单（2026-10-16 实测改的）
       *
       * 第一版把它放在「桌宠控制」子菜单里 —— 即**两层嵌套**的子菜单。
       * 用户实测：二级面板能显示（有箭头），但**点不开第三层** ——
       * 自绘菜单的子面板展开对两层嵌套不工作（`petMenu.js` 的
       * `buildPanel` 是递归建面板的，但展开交互只为一层设计）。
       * 用户拍板：「实在搞不了就放在一级菜单里面吧」——
       * 与「宠物大小」「麦克风」「活泼度」同级，同一套展开逻辑必然可用。
       *
       * ## ⚠️ 预计时间必须写 —— 两种模式耗时差 10 倍
       *
       * 精准搜索要 10~20 秒（多引擎融合 + 等慢引擎），不写的话用户
       * 以为卡死了；快速搜索约 2 秒。数字来自 A/B 实测
       * （`probe-ab-search.py`（历史实测脚本），国内直连）。
       *
       * 隐私口径：打开之后它想查的东西会**发到搜索引擎** —— 每一项的
       * 代价要让用户看得见。真源是 `persona.yaml` 的
       * `tools.web_search_mode`；action 带模式值
       * （`web-search-mode:accurate`），分发到 `setWebSearchMode`。
       */
      label: '联网搜索',
      icon: 'globe',
      submenu: WEB_SEARCH_MODES.flatMap((mode) => {
        const item: PetMenuItem = {
          label: WEB_SEARCH_LABEL[mode],
          type: 'radio' as const,
          checked: state.webSearchMode === mode,
          action: `web-search-mode:${mode}`,
        };
        if (mode !== 'fast') return [item];
        /*
         * 「快速搜索」下面紧跟一行灰色说明（2026-10-19）。
         * 它在渲染层是不可点的（`enabled: false` → `data-disabled`），
         * 键盘/鼠标都点不动，也不会被当成"第四种模式"。
         */
        return [
          item,
          {
            label: WEB_SEARCH_FAST_NOTE,
            type: 'normal' as const,
            enabled: false,
          },
        ];
      }),
    },

    {
      /*
       * ⚠️ 数值从 label 里**挪到了 `value`**（2026-10-14）。
       *
       * 设计稿这一行是「活泼度」+ 右侧灰色 `0.60` + 箭头 —— 数值单独占一格。
       * 原来拼成 `活泼度 0.60` 的话，渲染层只能靠正则把它拆出来，
       * 而「宠物大小（300 px）」这类**标签里本来就带数字**的行会被误伤。
       */
      label: '活泼度',
      /*
       * 父行右侧显示**最近的档位名**，不是滑杆的原始数值（2026-10-17 用户要求）。
       *
       * 用户看到的是四个名字，一个 `0.60` 对他没意义 —— 而且 0.60 四个档位
       * 一个都不勾（`near` 容差 0.01），会让人以为"当前没有选中任何档"。
       */
      value: ACTIVITY_TIERS.reduce(
        (best, tier) =>
          Math.abs(tier.value - state.activity) < Math.abs(best.value - state.activity) ? tier : best,
      ).name,
      icon: 'spark',
      /*
       * 四档的名字与后缀（2026-10-17 用户要求）。
       *
       * ## 名字来自 §115 拍板，不能自己改
       *
       * 用户看的是**四个名字**，不是一个数字：安静 / 普通 / 好奇 / 自言自语。
       * 老文案是「活泼（0.8）」「话痨（1.0）」—— 那两个名字**没有拍板过**，
       * 是壳子时期的占位写法。
       *
       * ## 后缀在说两件不同的事
       *
       * `会自主截图`  这一档会**自己**抓屏（不是用户按 Ctrl+Alt+S 那种）。
       *               ⚠️ 它有前置条件：设置里的「允许偶尔截图」必须**由用户打开**。
       *               活泼度只决定频率，**永远不能自己把权限打开**（§115 的硬约束）。
       * `注意力…`     注意力在谁身上（0 = 全在你，1 = 全在它自己）。
       *               只标两端，中间两档标了反而噪音。
       *
       * ⚠️ 一句话说清区别：「会自主截图」= 它会自己看屏幕；
       *    「注意力在它自己身上」= 它不怎么理你、更多在琢磨自己的事。
       *    两者是**不同维度**，所以最高档两个后缀都带。
       */
      submenu: [
        activityOption(
          '安静 · 注意力在你身上，不截图',
          0.1,
        ),
        activityOption('无聊 · 自己找事做，偶尔发呆', 0.5),
        activityOption('好奇 · 会自主截图，偶尔搭话', 0.8),
        // 自言自语：用户 2026-10-17 定的写法 —— 三个要点一条说完，
        // **不要**右侧灰字（那条曾被挤成省略号，「会自…」根本读不出是什么）。
        activityOption('自言自语 · 注意力在自己，会自主截图，20 分钟后安静', 1.0),
      ],
    },

    // 「工具与帮助」整块已**删除**（用户要求）。
    // 里面「打开日志」是唯一可用的功能，一并去掉；日志仍可从
    // `dist/startup.log` 或托盘菜单打开，不算丢失入口。

    /*
     * 「记忆管理」放在「设置…」**上面**。
     *
     * 理由：它是一个独立窗口（表格界面），打开频率比设置高得多 ——
     * 用户会经常去看看"它记住了什么"。放在设置上面符合"常用的在上"。
     */
    /*
     * 「截图对话」（2026-10-17 用户要求）。
     *
     * ## 为什么放在这个位置
     *
     * 它是**高频的一次性动作**（想给它看什么的时候按一下），不是设置项；
     * 和「记忆管理…」一样属于"日常会用"，所以放在设置与退出之上。
     *
     * ## 为什么标签里带快捷键
     *
     * 用户的原话是「点击按钮**或者按下快捷键**」—— 两条路都要能走。
     * 而在标签里写出来是**唯一**让用户发现快捷键的方式（`check-shortcuts`
     * 也要求登记的快捷键必须出现在界面文案里，否则就是"实现了但没人知道"）。
     *
     * ⚠️ 显示写法必须与 `shortcuts.ts` 里的 `display` 完全一致（`Ctrl+Alt+S`）。
     */
    {
      label: '截图对话（Ctrl+Alt+S）',
      action: 'snapshot-chat',
      icon: 'eye',
    },

    { label: '记忆管理…', action: 'memory', icon: 'memory' },

    /*
     * ⚠️ 设计稿里这**唯一一条蓝色分隔线**就在「设置…」上面 ——
     * 它是"日常开关"与"设置 / 退出"之间的分区标记，缺了整块菜单会显得没头没尾。
     */
    { type: 'separator', accent: true },

    { label: '设置…', action: 'settings', icon: 'sun' },
    { label: '退出', action: 'quit', icon: 'power' },
    { label: '退出全部实例', action: 'quit-all', icon: 'copy' },
  ];
}

/** 递归收集所有带 action 的项（测试用来确认"可点的都能对上处理器"） */
export function collectActions(items: PetMenuItem[]): string[] {
  const out: string[] = [];
  const walk = (list: PetMenuItem[]): void => {
    for (const item of list) {
      if (item.action) out.push(item.action);
      if (item.submenu) walk(item.submenu);
    }
  };
  walk(items);
  return out;
}

/** 递归收集所有置灰项（测试用来确认它们都标了「未实现」） */
export function collectDisabled(items: PetMenuItem[]): PetMenuItem[] {
  const out: PetMenuItem[] = [];
  const walk = (list: PetMenuItem[]): void => {
    for (const item of list) {
      if (item.enabled === false) out.push(item);
      if (item.submenu) walk(item.submenu);
    }
  };
  walk(items);
  return out;
}
