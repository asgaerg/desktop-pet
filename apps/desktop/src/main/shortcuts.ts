/**
 * 全局快捷键的**唯一定义源**。
 *
 * 为什么要单独一个模块：
 *   快捷键的名字会在两个地方出现 —— 一个地方**注册**它们（Electron 的
 *   globalShortcut），另一个地方**告诉用户**它们（界面上的提示文字）。
 *   两边各写一份，就一定会漂移。这个项目已经因此出过一次事故：
 *   界面写着「开启语音（Ctrl+Alt+M）」，而 `M` **从未注册过** ——
 *   用户按下去没有任何反应，也没人会发现，因为两边都是"看起来对"的字符串。
 *
 * 所以定义放这里，注册从它读、检查工具也比对它。不存在第二份副本。
 *
 * 约定：**只登记真的会被注册的快捷键**。想撤掉一个快捷键，就从这里删掉，
 * 并同步删掉界面上的提示 —— `tools/check-shortcuts.mjs` 会强制这一点。
 */

export interface ShortcutSpec {
  /** Electron 的 accelerator 字符串，直接交给 globalShortcut.register */
  accelerator: string;
  /** 这句话会出现在界面上。必须与真实行为一致。 */
  description: string;
  /** 界面上用来指代它的写法（检查工具按这个在渲染层里找引用） */
  display?: string;
}

export const SHORTCUTS: readonly ShortcutSpec[] = [
  {
    accelerator: 'CommandOrControl+Alt+P',
    description: '显示/隐藏宠物',
    display: 'Ctrl+Alt+P',
  },
  {
    accelerator: 'CommandOrControl+Alt+L',
    description: '聚焦输入框',
    display: 'Ctrl+Alt+L',
  },
  {
    accelerator: 'CommandOrControl+Alt+Q',
    description: '免打扰开关',
    display: 'Ctrl+Alt+Q',
  },
  {
    /*
     * **截图对话**（2026-10-17 用户要求）。
     *
     * 用户原话：「点击按钮或者按下快捷键，屏幕将自动截图并发给ai」。
     * 所以这个键和菜单里那一项是**同一个动作的两条入口**（行为在主进程
     * 的 `snapshotChat()` 里，只有一份实现）。
     *
     * ⚠️ 为什么不用 `PrintScreen`：Windows 上它常被系统/截图工具占用，
     * `globalShortcut.register` 会**静默失败**（返回 false 只写一行日志）。
     * 用 `Ctrl+Alt+S`（S = Screenshot）与现有那组 `Ctrl+Alt+?` 保持一致。
     */
    accelerator: 'CommandOrControl+Alt+S',
    description: '截图对话（看屏幕并聊这个画面）',
    display: 'Ctrl+Alt+S',
  },
  {
    /*
     * **把焦点给宠物窗口**（2026-10-10 加）。
     *
     * ## 为什么需要它
     *
     * "按住说话"有**两条路**，各自的有效条件不同：
     *
     *     聚焦时   按住那个键（默认右 Alt）-> 能拿到"松开"，真正的按住说
     *     不聚焦时 全局键（默认 F9）      -> 只有"按下"，切换式
     *
     * 而实测（2026-10-10）发现：**宠物窗口默认拿不到焦点**。
     * 它是鼠标穿透的（`setIgnoreMouseEvents(true, {forward:true})`），
     * 点击会**穿到下面的窗口** —— 所以"点一下宠物"并不会让它聚焦。
     * 实测当时前台窗口是「桌面宠物 · 设置」，宠物窗口一次都没收到键。
     *
     * 结论：**"按住说话"需要用户主动把焦点给宠物**，而这个动作必须有个入口。
     * 这就是它。
     *
     * ⚠️ 界面上要提示这个用法，否则用户不知道该先按一下。
     */
    accelerator: 'CommandOrControl+Alt+K',
    description: '聚焦宠物（之后可按住说话）',
    display: 'Ctrl+Alt+K',
  },
] as const;

/**
 * 界面上**不允许**出现的快捷键字样。
 *
 * 这是从真实事故里来的：`Ctrl+Alt+M` 曾被写在语音按钮的提示里，但它从未注册。
 * 与其靠"记得删掉提示"，不如把已撤销的快捷键登记在这里，让检查工具主动发现
 * 有人又把它写回去了。
 */
export const RETIRED_ACCELERATORS: readonly string[] = ['Ctrl+Alt+M', 'CommandOrControl+Alt+M'];

/** 供界面提示一行显示，例如 "Ctrl+Alt+P 显示/隐藏宠物 · Ctrl+Alt+L 聚焦输入框" */
export function describeShortcuts(separator = ' · '): string {
  return SHORTCUTS.map((item) => `${item.display ?? item.accelerator} ${item.description}`).join(
    separator,
  );
}
