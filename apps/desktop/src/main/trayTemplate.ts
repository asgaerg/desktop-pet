/**
 * 托盘菜单的**模板构造**（纯函数，不碰 Electron）。
 *
 * ## 为什么要单独一个文件
 *
 * 原来托盘菜单是在 `PetTray.refresh()` 里内联 `Menu.buildFromTemplate([...])` 的。
 * 那样**测不了**：跑断言就得先有一个真的 `Tray` 对象，而测试环境里
 * `electron` 是 external（`require('electron')` 在纯 Node 下拿不到 `Menu`）。
 *
 * 本项目的既有做法（见 `petMenuTemplate.ts`）是：**模板构造单独一个纯函数**，
 * 只吃一个 state 对象、吐一个数组；类那边负责把它交给 `Menu`。
 * 这样测试直接喂 state、断言数组，不需要 Electron。
 *
 * ⚠️ 所以这里的纪律和其它模板文件一样：
 *   **不要 import electron**，不要碰 `Tray`/`Menu`/窗口。
 *
 * ## ⚠️ 为什么用 `id` 标动作，而不是把 `click` 放进来
 *
 * 把回调塞进模板最直接，但那样**测试没法断言"点了会发生什么"**
 * （只能断言"有个函数"，等于没测），而且模板模块就得认识
 * `PetTrayCallbacks` 这个和它无关的类型。
 *
 * 用 `id` 之后：
 *   · 测试断言的是**动作清单**（`['toggle-visible', 'pet-size', …]`），
 *     而不是"某处有个函数"
 *   · `tray.ts` 里用一张 `Record<id, handler>` 把 id 映射到回调，
 *     **漏映射一个 id 会在类型检查时就报错**（Record 要求键齐全）
 */

import { PET_SCALE_PRESETS, petSizeFor } from './petLayout.js';

/**
 * 构造托盘菜单需要知道的全部状态。
 *
 * ⚠️ 只有 2 个字段 —— 精简后菜单只有 5 项，其中「宠物大小」要 `petScale`
 * 算勾和标题，「显示/隐藏宠物」要 `visible` 决定文案。
 *
 * 以前这里有 10 个（bubbleVisible / paused / thinking / webSearch / ttsSpeech /
 * visionWatching / statusVisible / chatVisible / micDevices / activity），
 * 因为菜单里有那 11 项。菜单删了，状态也就没了 —— 见 `tray.ts` 的说明。
 */
export interface TrayMenuState {
  /** 宠物当前可见吗（决定第一项写"隐藏宠物"还是"显示宠物"） */
  visible: boolean;
  /** 宠物缩放倍数（1 = 基准尺寸） */
  petScale: number;
}

/** 托盘菜单里的**动作名**（`id` 的另一半：`tray.ts` 用它查处理器）。 */
export type TrayActionId =
  | 'toggle-visible'
  | 'pet-size'
  | 'settings'
  | 'quit'
  | 'quit-all';

/** 一项菜单。`separator` 那种没有 `id`。 */
export interface TrayMenuItem {
  id?: TrayActionId;
  label?: string;
  type: 'separator' | 'radio' | 'normal';
  checked?: boolean;
  submenu?: TrayMenuItem[];
}

/**
 * 托盘菜单模板。
 *
 * 🔴 2026-10-16：用户要求精简到 5 项。
 *
 * > 「托盘菜单现在只保留显示隐藏宠物、宠物大小、设置、退出、退出全部」
 *
 * ## 结构（含 2 条分隔线，共 7 个数组元素）
 *
 * ```
 * 隐藏宠物 / 显示宠物            <- ⚠️ 唯一不能删的一项，见下
 * ─────────────
 * 宠物大小（220 px）  >          <- 子菜单，档位从 PET_SCALE_PRESETS 生成
 * ─────────────
 * 设置…
 * 退出
 * 退出全部实例
 * ```
 *
 * ## ⚠️ 为什么「显示/隐藏宠物」必须在
 *
 * 宠物窗口是 `skipTaskbar` 的（任务栏里没有按钮）。所以**藏起来之后，
 * 托盘里这一项是叫它回来的唯一途径** —— 删了它就再也点不出来了。
 * 这是本次精简中唯一"功能上不可替代"的一项。
 *
 * ## ⚠️ 为什么「宠物大小」的档位要从常量生成
 *
 * 以前这里硬编码过 0.6/0.8/1.0/1.3/1.6/2.0 六档，而设置页是连续滑杆 ——
 * "设置里调的档位，菜单里对不上"就是这么来的。现在三处共用
 * `PET_SCALE_PRESETS`。
 */
export function buildTrayMenuTemplate(state: TrayMenuState): TrayMenuItem[] {
  return [
    {
      id: 'toggle-visible',
      label: state.visible ? '隐藏宠物' : '显示宠物',
      type: 'normal',
    },
    { type: 'separator' },
    {
      id: 'pet-size',
      label: `宠物大小（${petSizeFor(state.petScale)} px）`,
      type: 'normal',
      // ⚠️ 档位**从这里生成**，不要在 `tray.ts` 里再写一份
      submenu: PET_SCALE_PRESETS.map((preset) => ({
        label: `${preset.label}（${petSizeFor(preset.scale)} px）`,
        type: 'radio' as const,
        checked: near(preset.scale, state.petScale),
      })),
    },
    { type: 'separator' },
    { id: 'settings', label: '设置…', type: 'normal' },
    { id: 'quit', label: '退出', type: 'normal' },
    { id: 'quit-all', label: '退出全部实例', type: 'normal' },
  ];
}

/** 浮点比较（档位是 0.6/0.8/1.0… 这种，不能用 `===`）。 */
function near(a: number, b: number): boolean {
  return Math.abs(a - b) < 0.05;
}
