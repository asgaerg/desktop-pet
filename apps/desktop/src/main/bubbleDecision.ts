/** 气泡显隐的**决策逻辑**（纯函数，与 Electron 无关）。

## 为什么单独拆出来

原来的决策揉在 `index.ts` 的 `forwardToBubble()` 里 —— 那个函数在 `main()`
内部，**测试碰不到**。而它恰好是"沉浸模式"的核心逻辑（用户 2026-10-09 要求），
正是**最该有测试**的地方。

拆成纯函数之后：

  · 测试直接喂参数打（不用起 Electron、不用真说话）
  · 决策规则集中在一处，读代码不用在 4000 行的 `index.ts` 里翻
  · `index.ts` 只负责"拿状态、调它、按结果转发"

## 三层门控的顺序（**顺序有讲究**）

    ① 气泡被用户关掉（`bubbleVisible=false`）   -> 一律不显示
    ② 沉浸模式且**这条真的会有声音**            -> 不显示（只听声音）
    ③ 沉浸模式但没有声音                        -> 显示（例外）

⚠️ ① 必须在 ② 前面：`bubbleVisible` 是总开关，用户明确关掉之后
连"例外"也不该弹 —— 否则手滑开了沉浸模式就绕过了用户的关闭意愿。
（这条是刻意的：那两个开关的分工是"总开关"vs"临时状态"。）

## 🔴 2026-10-15：判据从「TTS 开着」改成「TTS 真的能出声」

### 用户报的问题

> 「宠物发不了声音」+「也回复不了信息」

查出来是**两件事叠加**，而第二件暴露了这个函数的一个**真缺陷**：

```
① edge-tts 被微软 403 挡了（7.2.8 -> 升级 7.3.1 修好）
② 沉浸模式**故意不显示文字**（它认为"有语音就够了"）
```

**叠加起来才是"完全没反应"** —— 沉浸模式把文字关了，
而语音那头其实**合成失败**，于是两头都收不到。

### 缺陷在哪

原来的判据是 `ttsEnabled`（配置里 `provider !== 'none'`）。但：

```
**「TTS 开着」≠「TTS 能用」。**
```

用户这次正是：配置 `tts.enabled: true` -> 流程认为"有语音" ->
而合成**实际 403 失败** -> **连兜底的文字都不显示**。

⚠️ 所以现在多收一个 `ttsHealthy`（**最近一次合成的真实结果**）：

```
ttsEnabled && ttsHealthy   -> 有语音，沉浸模式不显示文字（原效果）
ttsEnabled && !ttsHealthy  -> 以为有、实际没有 -> **降级显示文字**
```

⚠️ 默认值必须是 `true`（"假定健康"）：没观测到失败时不该改变原行为，
否则这个改动会**顺便改掉所有正常用户的体验**。
*/

/** 决策结果：`true` = 交给气泡窗口显示，`false` = 这条不显示文字。 */
export interface BubbleDecision {
  show: boolean;
  /**
   * 不显示 / 显示的原因，用于日志。
   * 写成短语而不是代码，因为它会直接进 `startup.log` 给人看。
   */
  why: string;
}

export interface BubbleDecisionInput {
  /** 用不用显示"要显示文字"的这类消息（`pet.thinking` / `say.*`） */
  isTextKind: boolean;
  /** 菜单里的气泡总开关 */
  bubbleVisible: boolean;
  /** 沉浸模式是否开着 */
  immersiveMode: boolean;
  /** 这条发言的收尾原因（`pet.say.end` 的 `reason`；别的消息没有） */
  reason?: string;
  /** TTS 是否开着（`providers.tts.provider !== 'none'`） */
  ttsEnabled: boolean;
  /**
   * TTS **最近一次真实合成的结果**（2026-10-15 加）。
   *
   * ⚠️ 和 `ttsEnabled` 是两件事：前者是"配了没有"，这个是"**能不能用**"。
   * 省略时按 `true`（假定健康）—— 没观测到失败就不改变原行为。
   */
  ttsHealthy?: boolean;
}

export function decideBubble(input: BubbleDecisionInput): BubbleDecision {
  if (!input.isTextKind) return { show: false, why: '不是文字类消息' };

  // ① 总开关：用户关掉之后连沉浸模式的"例外"也不弹
  if (!input.bubbleVisible) return { show: false, why: '气泡已被菜单关闭' };

  if (!input.immersiveMode) return { show: true, why: '常规' };

  /*
   * ② / ③ 沉浸模式
   *
   * 判据是"**这条消息到底会不会有声音**"（不是"reason 是不是 error"）：
   *
   * ```
   * 生成失败            -> 没有内容可念          -> 用户听不到 -> 必须能看
   * TTS 关着            -> 本来就不出声          -> 同上
   * TTS 开着但**合成失败** -> 以为有、实际没有     -> 同上（2026-10-15 加）
   * 其余（真有声音）     -> 不显示，那才是沉浸模式要的效果
   * ```
   *
   * ⚠️ 第三条是 2026-10-15 补的，**它是这次用户报障的直接原因**：
   * 配置里 `tts.enabled: true` -> 这里认为"有语音" -> 而 edge-tts 实际 403
   * -> **连兜底文字都没有**，用户看到的是一片沉默。
   *
   * ⚠️ `ttsHealthy` 省略时按 `true`（假定健康）——
   * 没观测到失败就不改变原行为，否则这个改动会顺便改掉所有正常用户的体验。
   */
  const isError = input.reason === 'error';
  const ttsHealthy = input.ttsHealthy !== false;
  const noVoice = isError || !input.ttsEnabled || !ttsHealthy;
  if (!noVoice) return { show: false, why: '沉浸模式：只听语音' };

  return {
    show: true,
    why: `沉浸模式例外：${
      isError ? '生成失败' : !input.ttsEnabled ? 'TTS 已关闭' : 'TTS 合成失败（改用文字）'
    }`,
  };
}

/** 气泡「摆不摆出来」的输入（比 `decideBubble` 更靠后：文字已经决定要显示了）。 */
export interface BubblePlacementInput {
  /** 宠物窗口当前可见吗（用户把它藏了就一律不弹） */
  petVisible: boolean;
  /**
   * 宠物是不是"趴着"（紧凑态：别的应用全屏时它缩到边上）。
   *
   * 2026-10-19 之前这里**无条件**把气泡跳过，理由是"全屏时用户不在看桌面"。
   * 对自主开口成立；对用户自己按的截图对话就不成立了 ——
   * 他刚按了快捷键、正等着看回复，结果只有声音没有字。
   */
  compact: boolean;
  /**
   * 这一句是不是用户主动要的（截图对话 / 打字 / 语音）。
   * 省略按 `false`，保持原来的行为。
   */
  userInitiated?: boolean;
}

/** 决策结果：`show=false` 时连定位都别做，直接 hide。 */
export interface BubblePlacementDecision {
  show: boolean;
  /** 给人看的短语（会进 startup.log） */
  why: string;
}

/**
 * 气泡要不要真的弹出来。
 *
 * ```
 * 宠物被藏起来                    -> 不弹（用户明确不想看它）
 * 趴着 + 自主开口                 -> 不弹（全屏时别打扰）
 * 趴着 + 用户主动要的（截图/打字） -> 弹  ← 2026-10-19 修的就是这条
 * 正常                            -> 弹
 * ```
 */
export function decideBubblePlacement(
  input: BubblePlacementInput,
): BubblePlacementDecision {
  if (!input.petVisible) return { show: false, why: '宠物被隐藏了' };
  if (input.compact && !input.userInitiated) {
    return { show: false, why: '宠物趴着（别的应用全屏）且不是用户要的' };
  }
  if (input.compact) return { show: true, why: '趴着，但这条是用户主动要的' };
  return { show: true, why: '常规' };
}
