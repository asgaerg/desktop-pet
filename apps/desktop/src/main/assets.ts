/**
 * 打包后资源路径的唯一来源。
 *
 * 为什么要单独一个模块：`petWindow.ts` 里用**相对路径**从 `dist/` 推算渲染层页面，
 * 而构建期的检查工具也必须验证**同一条**路径。早先检查工具自己复制了一份路径
 * 算式，结果它验证的是"复制品能不能解析"，跟真品毫无关系 —— 真品算错了它照样
 * 报通过。所以把算式放在这里，两边共用，不存在第二份实现。
 *
 * 另外这个模块**不依赖 electron**，所以能在纯 Node 里直接跑（检查工具要用）。
 *
 * ⚠️ 踩过的坑：这里曾经少算一层 `src`，解析成 `apps/desktop/renderer/index.html`
 * （不存在）。后果是 `loadFile` 静默失败，而透明窗的失败从外面看就是
 * "什么都看不见"：窗口存在、IsWindowVisible 为真、位置尺寸都对，里面却是空的。
 * 界面因此在 P0~P1 期间**从未真正加载过**，而当时所有自检、语法检查、契约检查、
 * 运行时测试全是绿的 —— 因为没有一个环节经过 loadFile。
 */

import { existsSync } from 'node:fs';
import path from 'node:path';

/** 渲染层目录（相对 apps/desktop） */
export const RENDERER_RELATIVE = path.join('src', 'renderer');

/** 主进程产物文件名 */
export const PRELOAD_FILENAME = 'preload.cjs';

/** 设置窗口的 preload 产物文件名（能力与主窗口不同，所以是两份） */
export const SETTINGS_PRELOAD_FILENAME = 'settings-preload.cjs';

/** 宠物主页面文件名 */
export const PET_PAGE_FILENAME = 'index.html';

/** 设置页面文件名（与宠物页面同目录） */
export const SETTINGS_PAGE_FILENAME = 'settings.html';

/** 对话条页面文件名（只有输入框 + 发送按钮，独立窗口） */
export const CHAT_PAGE_FILENAME = 'chat.html';

/** 对话条窗口的 preload 产物名（第三份，能力最小） */
export const CHAT_PRELOAD_FILENAME = 'chat-preload.cjs';

/**
 * 文字弹窗（气泡）页面文件名 —— 与宠物窗口**分离**的独立窗口。
 *
 * 用户要求「可以加高宠物窗口」，但实测发现：**加高根本不增加头顶以上的空间**
 * （画布贴窗口顶，多出来的高度只能落在下方），所以想在宠物头顶上方放气泡，
 * 靠调宠物窗口尺寸是做不到的。
 *
 * 于是改成独立窗口：它浮在宠物上方，尺寸只受**屏幕**限制，
 * 与宠物窗口宽高完全解耦 —— 长回复也能完整显示。
 */
export const BUBBLE_PAGE_FILENAME = 'bubble.html';

/** 气泡窗口的 preload 产物名（第四份，能力最小） */
export const BUBBLE_PRELOAD_FILENAME = 'bubble-preload.cjs';

/**
 * 记忆管理页面文件名。
 *
 * ## 为什么是**独立窗口**而不是设置页里的一个标签
 *
 * 用户要求：「设置单开一页记忆管理，里面采用 tab 页管理」。
 *
 * 除了"用户要的"之外，这里还有个实际理由：记忆管理是**看数据的界面**
 * （表格、分页、搜索），而设置是**填表单的界面**。两者同时需要屏幕空间，
 * 挤在一个窗口里两边都局促。而且记忆管理会被反复打开查看，
 * 独立窗口可以一直开着，设置填完就关了。
 */
export const MEMORY_PAGE_FILENAME = 'memory.html';

/**
 * 记忆管理窗口的 preload 产物名（第五份）。
 *
 * ⚠️ 为什么不复用设置窗口那份：设置 preload 暴露了 `setPrompt`、
 * `writeCredentials`、`quitAll` 等能力，而记忆管理**只需要记忆相关的几个通道**。
 * 复用等于把一个能改密钥、能退出程序的接口交给一个只该读写记忆的页面 ——
 * 与项目一贯的"每份 preload 只给该窗口要用的能力"相反（已有 4 份的理由都在这）。
 */
export const MEMORY_PRELOAD_FILENAME = 'memory-preload.cjs';

/**
 * 状态条页面文件名 —— 又一块**独立窗口**（2026-10-11 用户要求）。
 *
 * ## 为什么不是宠物窗口里的一块
 *
 * 用户要的是「靠在屏幕**边缘**可拖拽的状态条」，而宠物窗口是一个
 * 250×250 的方块、跟着宠物跑。贴屏幕边缘的东西放进它里面做不到
 * （这正是气泡当初被拆出去的同一条理由，见 `BUBBLE_PAGE_FILENAME`）。
 *
 * 而且它要能**单独拖动、单独记住位置**，那必须是自己的窗口。
 */
export const STATUS_PAGE_FILENAME = 'status.html';

/** 状态条窗口的 preload 产物名（第六份，能力最小：显示状态 + 上报拖动） */
export const STATUS_PRELOAD_FILENAME = 'status-preload.cjs';

/**
 * 宠物右键菜单页面文件名 —— 又一块**独立窗口**（2026-10-14）。
 *
 * ## 为什么必须独立成一个窗口（而不是画在宠物窗口里）
 *
 * 原生菜单（`Menu.popup()`）的 20 个可配属性里**一个都不是样式**，
 * 所以设计稿那种卡片/开关/图标的菜单只能用**自绘窗口**做。
 *
 * 它还不能画在宠物窗口里，两个原因：
 *   · 宠物窗口只有 250×250、还跟着宠物跑，而菜单要按**指针位置**出现在
 *     屏幕任意处（包括宠物窗口范围之外）；
 *   · 宠物窗口平时是**点击穿透**的，在它里面画菜单要么被穿透掉、要么得
 *     为菜单临时关掉穿透（那会同时改变"能不能点桌面图标"的行为）。
 */
export const MENU_PAGE_FILENAME = 'petMenu.html';

/**
 * 菜单窗口的 preload 产物名（第七份）。
 *
 * ⚠️ 不复用主窗口那份：主窗口的 preload 能发用户消息、清空记忆、开设置、
 * 拖窗口、开麦克风 —— 菜单只需要"收菜单数据 / 报点击 / 报窗口矩形"。
 * 与项目一贯的"每份 preload 只给该窗口要用的能力"一致。
 */
export const MENU_PRELOAD_FILENAME = 'menu-preload.cjs';

/**
 * 由 dist 目录算出某个渲染层页面的路径，并断言它真实存在。
 *
 * 宁可大声失败，也不要交付一个"看不见的空窗口" —— 后者能骗过所有自动化检查。
 */
export function resolveRendererPage(
  distDir: string,
  filename: string = PET_PAGE_FILENAME,
): string {
  const pagePath = path.join(distDir, '..', RENDERER_RELATIVE, filename);
  if (!existsSync(pagePath)) {
    throw new Error(
      `渲染层页面不存在：${pagePath}\n` +
        '（dist 与 src/renderer 的相对位置变了？请检查 assets.ts 的 RENDERER_RELATIVE）',
    );
  }
  return pagePath;
}

/**
 * 把渲染层页面路径转成 `pet://` URL。
 *
 * ## 为什么所有窗口都该用它（2026-10-10）
 *
 * `loadFile(page)` **没有缓存穿透** —— Chromium 会缓存页面引用的 `.js`，
 * 于是"改了 `app.js`、重新构建、重启应用，界面行为还是旧的"。
 *
 * 这个坑踩过两次：
 *   ① 设置页（`settings.js` 一直是旧的）—— 当时只给设置页做了 `pet://`
 *   ② 宠物窗口（`app.js` 一直是旧的）—— 修「不按键也在听」时，
 *      新加的 `pet:ptt-config` 监听**根本没加载**，推了配置却没反应
 *
 * `pet://` 协议（见 `index.ts` 的 `servePet`）在服务 `.html` 时会**把里面
 * 引用的本地资源 URL 都补上 `?v=<该文件 mtime>`**，源码一动 URL 就变。
 *
 * ## 参数
 *
 * `pagePath` 必须是 `resolveRendererPage()` 返回的**绝对路径**。
 * 相对路径由本函数从 `apps/desktop/dist` 往上推 —— 和 `RENDERER_RELATIVE`
 * 用的是同一个基准，不会两处算歪。
 */
export function rendererUrl(pagePath: string): string {
  /*
   * `__dirname` 是 `apps/desktop/dist`，所以仓库根是它往上三级。
   * ⚠️ 和 `index.ts` 里 `PROJECT_ROOT` 的算法**必须一致** ——
   * 那里也用 `path.resolve(__dirname, '..', '..', '..')`。
   */
  const projectRoot = path.resolve(__dirname, '..', '..', '..');
  const rel = path.relative(projectRoot, pagePath).split(path.sep).join('/');
  return `pet://app/${rel}`;
}

/** 渲染层目录的绝对路径 */
export function rendererDir(distDir: string): string {
  return path.join(distDir, '..', RENDERER_RELATIVE);
}

/** preload 产物路径。默认主窗口那份，传 name 可取设置窗口那份。 */
export function resolvePreload(
  distDir: string,
  name: string = PRELOAD_FILENAME,
): string {
  return path.join(distDir, name);
}
