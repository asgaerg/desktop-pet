/**
 * 抓两个窗口（宠物 + 对话条）的几何与状态快照。
 *
 * 用法：node tools/debug-snapshot.mjs [port]
 *
 * 为什么不直接读日志：日志只能看到主进程算出来的数值，
 * 看不到"渲染层实际把 transform 算成了什么"。贴边/旋转/定位这类问题
 * 恰恰出在这两边的差异上 —— 所以这里直接从渲染层取真实值。
 */

const port = Number(process.argv[2] || 9222);

async function targets() {
  const res = await fetch(`http://127.0.0.1:${port}/json/list`);
  return res.json();
}

/** 用 DevTools 的 Runtime.evaluate 在页面里取值 */
async function evaluate(wsUrl, expression) {
  const WebSocketImpl = globalThis.WebSocket;
  if (!WebSocketImpl) throw new Error('当前 Node 没有全局 WebSocket（需要 22+）');
  return new Promise((resolve, reject) => {
    const ws = new WebSocketImpl(wsUrl);
    const timer = setTimeout(() => {
      try {
        ws.close();
      } catch {
        /* ignore */
      }
      reject(new Error('超时'));
    }, 5000);
    ws.addEventListener('open', () => {
      ws.send(
        JSON.stringify({
          id: 1,
          method: 'Runtime.evaluate',
          params: { expression, returnByValue: true, awaitPromise: true },
        }),
      );
    });
    ws.addEventListener('message', (event) => {
      const msg = JSON.parse(event.data);
      if (msg.id !== 1) return;
      clearTimeout(timer);
      ws.close();
      if (msg.result?.exceptionDetails) {
        reject(new Error(msg.result.exceptionDetails.text));
        return;
      }
      resolve(msg.result?.result?.value);
    });
    ws.addEventListener('error', () => {
      clearTimeout(timer);
      reject(new Error('WebSocket 出错'));
    });
  });
}

const PET_EXPR = `(() => {
  const c = document.getElementById('pet');
  const w = document.getElementById('pet-wrap');
  const cs = getComputedStyle(c);
  const ws = getComputedStyle(w);
  const r = c.getBoundingClientRect();
  const wr = w.getBoundingClientRect();
  return {
    窗口内容区: [window.innerWidth, window.innerHeight],
    屏幕: [screen.width, screen.height],
    窗口屏幕坐标: [window.screenX, window.screenY],
    设备像素比: window.devicePixelRatio,
    画布属性: [c.width, c.height],
    画布CSS: [cs.width, cs.height],
    画布transform: cs.transform,
    画布矩形: [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)],
    包装transform: ws.transform,
    包装矩形: [Math.round(wr.x), Math.round(wr.y), Math.round(wr.width), Math.round(wr.height)],
    body类: document.body.className,
    片段: (window.__pet && window.__pet.sprite) ? window.__pet.sprite.debug.clip : null,
  };
})()`;

const CHAT_EXPR = `(() => {
  const bar = document.getElementById('bar');
  const inp = document.getElementById('input');
  const dot = document.getElementById('dot');
  const bs = bar ? getComputedStyle(bar) : null;
  const r = bar ? bar.getBoundingClientRect() : null;
  return {
    窗口内容区: [window.innerWidth, window.innerHeight],
    屏幕: [screen.width, screen.height],
    窗口屏幕坐标: [window.screenX, window.screenY],
    屏幕可用区: [screen.availLeft, screen.availTop, screen.availWidth, screen.availHeight],
    条矩形: r ? [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)] : null,
    条背景: bs ? bs.backgroundColor : null,
    条的appRegion: bs ? (bs.webkitAppRegion || bs.getPropertyValue('-webkit-app-region')) : null,
    状态点: dot ? { level: dot.dataset.level, title: dot.title } : null,
    输入框: inp ? { placeholder: inp.placeholder, disabled: inp.disabled } : null,
  };
})()`;

const list = await targets();
for (const [label, suffix, expr] of [
  ['宠物窗口', 'index.html', PET_EXPR],
  ['对话条窗口', 'chat.html', CHAT_EXPR],
]) {
  const target = list.find((t) => t.url.endsWith(suffix));
  if (!target) {
    console.log(`  ${label}：没找到`);
    continue;
  }
  try {
    const info = await evaluate(target.webSocketDebuggerUrl, expr);
    console.log(`  ${label}：`);
    for (const [k, v] of Object.entries(info)) {
      console.log(`    ${k}: ${JSON.stringify(v)}`);
    }
  } catch (error) {
    console.log(`  ${label}：取值失败（${error.message}）`);
  }
  console.log('');
}
