/**
 * 对话条窗口的前端逻辑。
 *
 * 这个文件**不能写 TypeScript 语法** —— 它和 `app.js` 一样是直接喂给 Chromium 的
 * 普通 ESM（渲染层只有 `sprite.ts` 走 esbuild）。工具链有门禁检查这一点。
 *
 * ---- 它管什么 ----
 *
 * 只管道一根对话条：输入、发送、发图。
 * **窗口位置完全由主进程决定**（见 chatWindow.ts）—— 渲染层不参与定位，
 * 否则又会出现"两边都在算位置"的经典问题（这个项目在点击穿透上踩过一次）。
 *
 * 🔴 2026-11-04：状态点（`#dot`）已按用户要求删除。
 *    原来这里有 `paintDot()` 按状态文本给圆点着色，还有一条 `chat:status`
 *    订阅专为它服务 —— 两者**都删掉了**。
 *
 *    ⚠️ 为什么不把 `lastStatus` 留着"以后可能有用"：它只写不读，
 *       是纯粹的死后代码。留着会让人以为"这里还有状态显示"。
 *       真要再显示状态，去主进程要 `chat:status` 就是（通道还在）。
 */

const el = (id) => document.getElementById(id);
const input = el('input');
const send = el('send');
let busy = false;

function setBusy(next) {
  busy = next;
  send.disabled = next;
  send.textContent = next ? '…' : '发送';
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

/* --------------------------------------------------------------- 发图片 */
/*
 * 2026-11-02 用户要求：「可以用图片和用户聊天」。
 *
 * ## 三条输入路径，一个出口
 *
 *   ① 点按钮选文件
 *   ② 粘贴（**最常用** —— 截图之后直接粘）
 *   ③ 拖拽文件进来
 *
 * 三条都调 `sendImageFile(file)`，最终都走 `window.pet.sendImage()`。
 *
 * ## 为什么统一读成 dataURL
 *
 * 粘贴路径里图片在剪贴板、**没有文件路径**；拖拽进来的可能是临时文件。
 * 统一读成 dataURL 就没有这些差别了（代价是消息变大，主进程会挡上限）。
 */

const pickImage = el('pick-image');
const imageInput = el('image-input');

/** 上限 10 MB，和主进程一致（这里挡是为了即时反馈） */
const MAX_IMAGE_BYTES = 10 * 1024 * 1024;

/**
 * 把 File 读成 dataURL 并发出。
 *
 * ⚠️ 用 FileReader 而不是 `file.arrayBuffer()` + 手写 base64：
 *    后者对几 MB 的图会有明显的卡顿，而 FileReader 是浏览器原生异步实现。
 */
function sendImageFile(file) {
  if (!file || !String(file.type || '').startsWith('image/')) return;
  if (file.size > MAX_IMAGE_BYTES) {
    toast(`图片太大（${(file.size / 1024 / 1024).toFixed(1)} MB，上限 10 MB）`);
    return;
  }
  if (busy) return;

  const reader = new FileReader();
  reader.onload = () => {
    const dataUrl = String(reader.result || '');
    if (!dataUrl.startsWith('data:image/')) return;
    // 把输入框里已经打好的字一起带上 —— 用户常常"先打一句话再拖图进来"
    const text = input.value.trim();
    window.pet.sendImage({ dataUrl, name: String(file.name || ''), text });
    if (text) input.value = '';
    setBusy(true);
    setTimeout(() => setBusy(false), 15000); // 看图比打字慢，给久一点
  };
  reader.onerror = () => toast('这张图片读不出来');
  reader.readAsDataURL(file);
}

/** 极简提示：对话条没有说话气泡的位置，用状态点闪一下 + 临时改 placeholder */
function toast(text) {
  const old = input.placeholder;
  input.placeholder = text;
  setTimeout(() => {
    input.placeholder = old;
  }, 2500);
}

// ① 按钮 -> 系统文件选择器
if (pickImage && imageInput) {
  pickImage.addEventListener('click', () => imageInput.click());
  imageInput.addEventListener('change', () => {
    const file = imageInput.files && imageInput.files[0];
    if (file) sendImageFile(file);
    imageInput.value = ''; // 允许连着选同一张
  });
}

// ② 粘贴
/*
 * ⚠️ 监听在 **document** 上而不是 input 上：用户在对话条任何地方粘贴
 *    都应该生效（尤其刚点完按钮、焦点不在输入框时）。
 */
document.addEventListener('paste', (event) => {
  const items = (event.clipboardData && event.clipboardData.items) || [];
  for (const item of items) {
    if (String(item.type || '').startsWith('image/')) {
      const file = item.getAsFile();
      if (file) {
        event.preventDefault();
        sendImageFile(file);
        return;
      }
    }
  }
  // 不是图片就交给浏览器默认行为（普通文字照常粘进输入框）
});

// ③ 拖拽
/*
 * ⚠️ 必须 preventDefault 两个事件：不拦 `dragover` 的话
 *    浏览器会**拒绝** drop（Electron 里表现为"拖进来什么也没发生"）。
 */
document.addEventListener('dragover', (event) => {
  if (!event.dataTransfer) return;
  const hasFile = Array.from(event.dataTransfer.types || []).includes('Files');
  if (hasFile) {
    event.preventDefault();
    event.dataTransfer.dropEffect = 'copy';
  }
});

document.addEventListener('drop', (event) => {
  const files = (event.dataTransfer && event.dataTransfer.files) || [];
  for (const file of files) {
    if (String(file.type || '').startsWith('image/')) {
      event.preventDefault();
      sendImageFile(file);
      return;
    }
  }
});

// 收到宠物回复（或错误）就解除忙碌态
window.pet.on('pet:message', (message) => {
  const type = message && message.type;
  if (type === 'pet.reply' || type === 'error' || type === 'pet.toast') setBusy(false);
});

// 打开时就聚焦：用户点进来就是为了打字
input.focus();
