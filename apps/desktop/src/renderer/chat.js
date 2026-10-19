/**
 * 对话条窗口的前端逻辑。
 *
 * 这个文件**不能写 TypeScript 语法** —— 它和 `app.js` 一样是直接喂给 Chromium 的
 * 普通 ESM（渲染层只有 `sprite.ts` 走 esbuild）。工具链有门禁检查这一点。
 *
 * ---- 它管什么 ----
 *
 * 只管道一根对话条：输入、发送、一个状态点。
 * **窗口位置完全由主进程决定**（见 chatWindow.ts）—— 渲染层不参与定位，
 * 否则又会出现"两边都在算位置"的经典问题（这个项目在点击穿透上踩过一次）。
 */

const el = (id) => document.getElementById(id);
const input = el('input');
const send = el('send');
const dot = el('dot');

/** 由 pet.on('pet:message') 收到的运行状态，用来给状态点着色 */
let lastStatus = '正在启动…';
let busy = false;

function setBusy(next) {
  busy = next;
  send.disabled = next;
  send.textContent = next ? '…' : '发送';
}

function paintDot() {
  if (!dot) return;
  // 免打扰 / 未连接 都从状态文本里判断 —— 主进程已经把状态收敛成一个字符串，
  // 再让渲染层自己推导一套状态机只会两边不一致。
  const text = String(lastStatus || '');
  let level = 'info';
  if (/在线|已连接|就绪|mock/.test(text)) level = 'ok';
  else if (/失败|错误|断开|离线/.test(text)) level = 'bad';
  else if (/免打扰|暂停|连接中/.test(text)) level = 'warn';
  dot.dataset.level = level;
  dot.title = text;
}

function submit() {
  const text = input.value.trim();
  if (!text || busy) return;
  window.pet.sendText(text);
  input.value = '';
  setBusy(true);
  // 主进程会在收到回执或超时后解除忙碌；这里只做即时反馈。
  // 超时兜底：万一回执丢了，按钮不能永远卡在"…"上。
  setTimeout(() => setBusy(false), 8000);
}

send.addEventListener('click', submit);

input.addEventListener('keydown', (event) => {
  if (event.key === 'Enter' && !event.shiftKey) {
    event.preventDefault();
    submit();
  }
  if (event.key === 'Escape') input.blur();
});

// 主进程让它聚焦输入框（Ctrl+Alt+L / 点击宠物）
window.pet.on('chat:focus-input', () => {
  input.focus();
});

// 运行状态：只用来着色那个小圆点
window.pet.on('chat:status', (payload) => {
  lastStatus = String((payload && payload.text) || '');
  paintDot();
});

// 收到宠物回复（或错误）就解除忙碌态
window.pet.on('pet:message', (message) => {
  const type = message && message.type;
  if (type === 'pet.reply' || type === 'error' || type === 'pet.toast') setBusy(false);
  if (type === 'ready' || type === 'pet.status') {
    lastStatus = String((message.payload && message.payload.status) || lastStatus);
    paintDot();
  }
});

paintDot();
// 打开时就聚焦：用户点进来就是为了打字
input.focus();
