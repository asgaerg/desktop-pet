/**
 * 状态条渲染层（2026-10-11 用户要求）。
 *
 * ## 职责（2026-10-12 简化后）
 *
 * **只剩一件事**：收到主进程推来的 `{ state, text }` -> 换灯的颜色、换文字。
 *
 * ## 🔴 为什么删掉了手写拖拽（2026-10-12）
 *
 * 第一版是自己做鼠标跟踪：`mousedown` 记起点 -> `mousemove` 上报指针 y
 * -> 主进程 `setBounds` -> `mouseup` 吸附。
 *
 * **实测这条路走不通**。在渲染层挂了探针
 * （`probe-status-drag-events.py`，历史实测脚本），用户拖过之后计数是：
 *
 *     mousedown 次数 : 0
 *     mousemove 次数 : 0
 *     mouseup   次数 : 0
 *
 * 也就是说**鼠标事件根本没到渲染层**，整条链从第一步就断了。
 *
 * 旁证：同一个应用里**对话条的抓手能拖** —— 它用的是 CSS 的 app-region，
 * 不走 JS 事件，所以完全不受这个影响。
 *
 * 所以改成**和对话条完全同一套机制**：整条设 `-webkit-app-region: drag`，
 * 由系统负责拖动，主进程监听 `moved` 事件拿落点 + 吸附。
 *
 * 好处不只是"能拖"：系统拖动自带多显示器处理、DPI 缩放、
 * 以及"拖到屏幕边缘"的原生手感，都不用自己实现。
 *
 * ⚠️ 代价：拖动期间**渲染层收不到任何事件**（被系统接管了）。
 * 所以"松手后吸附"只能由主进程的 `moved` 事件驱动，不能在这里做。
 */

const light = document.getElementById('light');
const text = document.getElementById('text');

/** 当前状态（避免重复写 DOM） */
let state = { state: 'idle', text: '空闲' };

function apply(next) {
  if (!next) return;
  const s = typeof next.state === 'string' ? next.state : state.state;
  const t = typeof next.text === 'string' ? next.text : state.text;
  if (s === state.state && t === state.text) return;
  state = { state: s, text: t };
  if (light) light.dataset.state = s;
  if (text) text.textContent = t;
}

/*
 * 主进程 -> 渲染层：状态变化。
 *
 * ⚠️ 通道名必须与 `src/preload/status.ts` 逐字一致 ——
 * 这个项目踩过"DTS 通道列表与 preload 白名单漂移"的坑，表现是静默失效。
 */
window.petStatus?.onState?.(apply);
